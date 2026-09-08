from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_delivery as delivery_fixtures
from management.models import IgBotNotification, IgDeal, IgFollowUpTask, IgWebhookInboxEvent
from management.services import bot_followups as policy
from management.services.ig_revision_followups import (
    cancel_revision_sales_timers, schedule_revision_normal_followups,
)
from management.services.ig_revision_input import decide_revision_input
from management.services.ig_revision_outbox import claim_next_effect, finish_effect, mark_provider_started


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class RevisionNormalFollowupTests(TransactionTestCase):
    reset_sequences = True
    _payload = delivery_fixtures.RevisionDeliveryTests._payload
    _plan = delivery_fixtures.RevisionDeliveryTests._plan

    def setUp(self):
        delivery_fixtures.RevisionDeliveryTests.setUp(self)
        self.settings.ai_enabled = False
        self.settings.trigger_text = self.source.text
        self.settings.reply_text = "Thanks for your message."
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        decision = decide_revision_input(self.revision.pk, self.revision_token, settings_id=self.settings.pk)
        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(decision.origin, "static_reply")
        self.revision.refresh_from_db()

    def _kwargs(self, **overrides):
        kwargs = dict(settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.binding)
        kwargs.update(overrides)
        return kwargs

    def _cancel(self, **overrides):
        return cancel_revision_sales_timers(self.revision.pk, self.revision_token, **self._kwargs(**overrides))

    def _schedule(self, **overrides):
        return schedule_revision_normal_followups(self.revision.pk, self.revision_token, **self._kwargs(**overrides))

    def _sent(self, *, parts=1, unknown_last=False):
        self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload(f"Confirmed reply {index}")} for index in range(parts)])
        for index in range(parts):
            claim = claim_next_effect(self.revision.pk, self.revision_token, "substantive_text")
            self.assertIsNotNone(claim.effect)
            started = mark_provider_started(claim.effect.pk, claim.token, self.revision_token)
            self.assertEqual(started.reason, "provider_started")
            unknown = unknown_last and index == parts - 1
            finish_effect(claim.effect.pk, claim.token, provider_namespace="instagram_login:owner-1", http_status=None if unknown else 200, provider_message_id="" if unknown else f"followup-reply-{index}", transport_outcome="timeout" if unknown else "response")

    def test_manager_prize_and_parcel_cases_survive_actual_user_input(self):
        manager = IgFollowUpTask.objects.create(client=self.client_row, kind="manager_task", status="skipped", reason="revision_case:manager_handoff", due_at=timezone.now(), manager_approval_status="pending")
        prize = IgFollowUpTask.objects.create(client=self.client_row, kind="manager_task", status="skipped", reason="prize_review:one", due_at=timezone.now(), manager_approval_status="pending")
        parcel = IgFollowUpTask.objects.create(client=self.client_row, kind="manager_task", trigger="reactive", reason="parcel_reminder:1", due_at=timezone.now())
        sales = IgFollowUpTask.objects.create(client=self.client_row, kind="qualification", trigger="time", reason="first_reply_silence", due_at=timezone.now())
        IgFollowUpTask.objects.filter(pk=sales.pk).update(created_at=self.source.created_at - timedelta(minutes=1))
        cancelled = self._cancel()
        self.assertTrue(cancelled.ready, cancelled.reason)
        self.assertEqual(cancelled.cancelled_ids, (sales.pk,))
        for task, status in ((manager, "skipped"), (prize, "skipped"), (parcel, "pending"), (sales, "cancelled")):
            task.refresh_from_db()
            self.assertEqual(task.status, status)
        self.assertFalse(self._cancel().cancelled_ids)
        self._sent()
        scheduled = self._schedule()
        self.assertTrue(scheduled.ready, scheduled.reason)
        self.assertEqual(scheduled.reason, "pending_manager_case")
        self.assertEqual(scheduled.task_id, 0)

    def test_existing_first_reply_policy_is_source_anchored_and_once_only(self):
        self._sent(parts=2)
        anchor = self.source.provider_created_at or self.source.created_at
        expected_due = policy.next_allowed_send_at(anchor + timedelta(hours=2), deadline=anchor + policy.META_REPLY_WINDOW)
        result = self._schedule()
        self.assertTrue(result.ready, result.reason)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        self.assertEqual(task.reason, "first_reply_silence")
        self.assertEqual(task.kind, "qualification")
        self.assertEqual(task.level, 0)
        self.assertEqual(task.due_at, expected_due)
        self.assertEqual(task.policy_started_at, anchor)
        self.assertEqual(task.meta_window_deadline, anchor + timedelta(hours=23))
        again = self._schedule(now=timezone.now() + timedelta(seconds=1))
        self.assertTrue(again.replayed, again.reason)
        self.assertEqual(again.receipt, result.receipt)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(self._cancel().reason, "reply_followups_already_recorded")
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.due_at, expected_due)

    def test_unknown_partial_reply_never_schedules(self):
        self._sent(parts=2, unknown_last=True)
        result = self._schedule()
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "whole_substantive_reply_not_sent")
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.revision.refresh_from_db()
        self.assertNotIn("normal_followups", self.revision.action_receipts)

    def test_planned_reply_without_confirmed_send_never_schedules(self):
        self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("Not sent")}])
        self.assertFalse(self._schedule().ready)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_paused_pending_new_inbound_and_expired_window_have_no_writes(self):
        self._sent()
        self.client_row.bot_paused = True
        self.client_row.save(update_fields=["bot_paused"])
        self.assertEqual(self._schedule().reason, "client_blocked")
        self.client_row.bot_paused = False
        self.client_row.save(update_fields=["bot_paused"])
        inbound = IgWebhookInboxEvent.objects.create(namespace="instagram_login:owner-1", event_key="followup-new-source", owner_id="owner-1", customer_igsid=self.client_row.igsid, decision="accepted", payload={}, payload_digest="a" * 64)
        self.assertEqual(self._schedule().reason, "pending_inbound")
        inbound.processed_at = timezone.now()
        inbound.save(update_fields=["processed_at"])
        self.assertFalse(self._schedule(now=timezone.now() + timedelta(hours=24)).ready)
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.revision.refresh_from_db()
        self.assertNotIn("normal_followups", self.revision.action_receipts)

    def test_new_head_claim_token_cannot_schedule_or_cancel_old_timers(self):
        self._sent()
        stale = schedule_revision_normal_followups(self.revision.pk, "different-token", **self._kwargs())
        self.assertFalse(stale.ready)
        self.assertEqual(stale.reason, "revision_not_current")
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_paid_missing_delivery_creates_only_deferred_human_case(self):
        deal = IgDeal.objects.create(client=self.client_row, status="paid", amount=900)
        obsolete = IgFollowUpTask.objects.create(client=self.client_row, deal=deal, kind="fulfillment", reason="paid_missing_delivery", trigger="time", due_at=timezone.now(), message_text="Надішліть повну адресу в Direct")
        self._sent()
        with patch("management.services.instagram_bot._deliver_manager_notification") as deliver:
            result = self._schedule()
        deliver.assert_not_called()
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.reason, "paid_fulfillment_case")
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        self.assertEqual(task.kind, "manager_task")
        self.assertEqual(task.status, "skipped")
        self.assertEqual(task.manager_approval_status, "pending")
        self.assertEqual(task.reason, "revision_case:paid_fulfillment")
        obsolete.refresh_from_db()
        self.assertEqual(obsolete.status, "cancelled")
        self.assertFalse(IgFollowUpTask.objects.filter(kind="fulfillment", status="pending").exists())
        self.assertEqual(IgBotNotification.objects.filter(client=self.client_row).count(), 1)
        again = self._schedule()
        self.assertTrue(again.replayed, again.reason)
        self.assertEqual(again.task_id, task.pk)
        self.assertEqual(IgBotNotification.objects.filter(client=self.client_row).count(), 1)
        deal.refresh_from_db()
        self.assertIsNone(deal.order_id)
        self.assertEqual(deal.np_city, "")

    def test_receipt_failure_rolls_back_new_timer(self):
        self._sent()
        original = type(self.revision).save

        def reject(instance, *args, **kwargs):
            if "normal_followups" in (instance.action_receipts or {}):
                raise ValueError("forced receipt failure")
            return original(instance, *args, **kwargs)

        with patch.object(type(self.revision), "save", reject):
            result = self._schedule()
        self.assertFalse(result.ready)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_source_replay_preserves_newer_timers_and_event_expiry(self):
        newer = IgFollowUpTask.objects.create(
            client=self.client_row, kind="qualification", trigger="time",
            reason="first_reply_silence", due_at=timezone.now() + timedelta(hours=2),
            policy_started_at=self.source.created_at,
        )
        expiry = IgFollowUpTask.objects.create(
            client=self.client_row, kind="payment", trigger="event",
            reason="payment_link_unpaid", due_at=timezone.now() + timedelta(hours=12),
            event_key="existing-expiry-event", event_occurred_at=timezone.now(),
        )
        IgFollowUpTask.objects.filter(pk=expiry.pk).update(created_at=self.source.created_at - timedelta(hours=1))
        result = self._cancel()
        self.assertTrue(result.ready, result.reason)
        self.assertFalse(result.cancelled_ids)
        newer.refresh_from_db()
        expiry.refresh_from_db()
        self.assertEqual(newer.status, "pending")
        self.assertEqual(expiry.status, "pending")

    @override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
    def test_current_v2_checkout_does_not_get_first_reply_sales_timer(self):
        from management.services.ig_checkout import create_or_update_proposal
        from storefront.models import Category, Product

        category = Category.objects.create(name="Normal followup", slug="normal-followup")
        product = Product.objects.create(title="Current product", slug="normal-followup-product", category=category, price=900, status="published")
        proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full", item_specs=[{"product_id": product.pk, "qty": 1, "size": "M"}], evidence={"message_ids": [self.source.pk]}, allow_promo=True)
        self.assertTrue(proposal.assisted_checkout_v2)
        self._sent()
        before = IgFollowUpTask.objects.count()
        result = self._schedule()
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.reason, "hosted_checkout_v2_owns_followups")
        self.assertEqual(result.task_id, 0)
        self.assertEqual(IgFollowUpTask.objects.count(), before)
