"""Purchase-scoped ordinary follow-ups through the real claim/send owners."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TransactionTestCase, override_settings

from management import tests_ig_revision_followups as fixtures
from management.models import (
    IgCommercialEpisode, IgDeal, IgFollowUpTask, IgPaymentProjection,
    InstagramBotMessage,
)
from management.services import bot_followups as policy
from management.services import instagram_bot
from management.services.bot_payment_truth import (
    client_has_confirmed_purchase, current_payment_confirmation,
)
from management.tests_support import AnalysisPrivacyCleanupMixin


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class FollowupEpisodeDispatchTests(AnalysisPrivacyCleanupMixin, TransactionTestCase):
    reset_sequences = True
    _payload = fixtures.RevisionNormalFollowupTests._payload
    _plan = fixtures.RevisionNormalFollowupTests._plan
    _sent = fixtures.RevisionNormalFollowupTests._sent
    _kwargs = fixtures.RevisionNormalFollowupTests._kwargs
    _schedule = fixtures.RevisionNormalFollowupTests._schedule

    def setUp(self):
        if connection.vendor == "mysql":
            self.assertRegex(str(connection.settings_dict.get("NAME") or ""), r"^test_twocomms_[A-Za-z0-9_]+$")
        self.clock = datetime(2026, 10, 9, 11, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        clock = patch("django.utils.timezone.now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        create_message = InstagramBotMessage.objects.create

        def inquiry_source(**kwargs):
            if kwargs.get("mid") == "revision-delivery-source":
                if self._testMethodName == "test_explicit_selection_keeps_ninety_minute_offset":
                    kwargs["text"] = "Допоможіть підібрати розмір"
                elif self._testMethodName == "test_missing_size_without_selection_request_never_creates_chase":
                    kwargs["text"] = "Привіт!"
            return create_message(**kwargs)

        with patch.object(InstagramBotMessage.objects, "create", side_effect=inquiry_source):
            fixtures.RevisionNormalFollowupTests.setUp(self)
        self.historical_deal = IgDeal.objects.create(
            client=self.client_row, status="paid", payment_status="paid",
            payment_truth="confirmed", paid_at=self.clock - timedelta(days=10), amount=900,
        )
        self.historical_projection = IgPaymentProjection.objects.create(
            client=self.client_row, deal=self.historical_deal, truth="confirmed",
            gross_amount=900, paid_at=self.historical_deal.paid_at,
            provider_modified_at=self.historical_deal.paid_at,
        )
        self.historical_episode = IgCommercialEpisode.objects.create(
            client=self.client_row, sequence=2, open_slot=None,
            materialization_key=f"dispatch-history:{self.historical_deal.pk}",
            deal=self.historical_deal, state="fulfilled",
        )
        self.assertTrue(client_has_confirmed_purchase(self.client_row))
        self.assertFalse(current_payment_confirmation(self.client_row)["confirmed"])
        self.history_before = (
            IgDeal.objects.filter(pk=self.historical_deal.pk).values().get(),
            IgPaymentProjection.objects.filter(pk=self.historical_projection.pk).values().get(),
            IgCommercialEpisode.objects.filter(pk=self.historical_episode.pk).values().get(),
        )

    def scheduled_task(self):
        self._sent()
        result = self._schedule(now=self.clock)
        self.assertEqual(result.reason, "normal_followup_scheduled", result)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        offset = timedelta(minutes=90) if task.event_payload["purpose"] == "requested_selection" else timedelta(hours=3)
        self.assertEqual(task.due_at, self.clock + offset)
        self.assertEqual(task.event_payload["commerce_binding"]["episode_id"], self.episode.pk)
        self.clock = task.due_at
        return task

    def dispatch(self, *, outcome="sent", before_boundary=None):
        physical = []

        def transport(_settings, recipient, text, **kwargs):
            # The transport alone is mocked. Run both real send boundaries
            # after preparation, exactly where the actual transport enters them.
            if before_boundary is not None:
                before_boundary()
            with kwargs["permission_boundary_factory"]() as permission:
                if not permission:
                    return instagram_bot.ProviderDeliveryReceipt(False, "cancelled", "permission_epoch_changed", "")
                with kwargs["provider_request_boundary_factory"](
                    delivered_chunk_count=0, planned_chunk_count=1,
                ) as allowed:
                    if not allowed:
                        return instagram_bot.ProviderDeliveryReceipt(False, "cancelled", allowed.reason, "")
                    physical.append((recipient, text))
                    if outcome == "unknown":
                        return instagram_bot.ProviderDeliveryReceipt(False, "unknown", "timeout", "")
                    return instagram_bot.ProviderDeliveryReceipt(True, "", "", "episode-followup-confirmed")

        with patch.object(instagram_bot, "send_text", side_effect=transport) as sender:
            sent = policy.process_due_followups(self.settings, now=self.clock, limit=10)
        return sent, physical, sender.call_count

    def frozen_task_with_payload(self, template, payload):
        """Insert an existing malformed/legacy row without editing its boundary.

        Keep the correctly scheduled row as cancelled audit history. Negative
        fixtures represent tasks created with these payloads, rather than an
        impossible mutation of an already published task.
        """
        boundary_before = deepcopy({
            name: getattr(template, name) for name in template._EVENT_BOUNDARY_FIELDS
        })
        values = {
            field.attname: deepcopy(getattr(template, field.attname))
            for field in template._meta.concrete_fields
            if not field.primary_key and field.name not in {"created_at", "updated_at"}
        }
        template.status = IgFollowUpTask.Status.CANCELLED
        template.skip_reason = "negative_fixture_superseded"
        template.save(update_fields=["status", "skip_reason", "updated_at"])
        template.refresh_from_db()
        self.assertEqual({
            name: getattr(template, name) for name in template._EVENT_BOUNDARY_FIELDS
        }, boundary_before)
        return IgFollowUpTask.objects.create(**{
            **values,
            "event_key": f"{template.event_key}:negative-fixture",
            "event_payload": deepcopy(payload),
        })

    def assert_history_preserved(self):
        self.assertEqual((
            IgDeal.objects.filter(pk=self.historical_deal.pk).values().get(),
            IgPaymentProjection.objects.filter(pk=self.historical_projection.pk).values().get(),
            IgCommercialEpisode.objects.filter(pk=self.historical_episode.pk).values().get(),
        ), self.history_before)

    def assert_blocked(self, task, reason):
        sent, physical, calls = self.dispatch()
        self.assertEqual((sent, physical, calls), (0, [], 0))
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertEqual(task.skip_reason, reason, task.last_error)
        self.assertEqual(task.provider_message_id, "")
        self.assertFalse(task.sent_message_id)
        self.assert_history_preserved()

    def test_genuine_historical_paid_new_episode_reaches_receipt_once(self):
        task = self.scheduled_task()
        self.assertIsNone(task.deal_id)
        sent, physical, calls = self.dispatch()
        self.assertEqual((sent, calls), (1, 1))
        self.assertEqual(physical, [(self.client_row.igsid, policy.compose_followup(task, now=self.clock))])
        task.refresh_from_db()
        self.assertEqual(task.status, "sent", task.last_error)
        self.assertEqual(task.attempt_count, 1)
        self.assertEqual(task.provider_message_id, "episode-followup-confirmed")
        self.assertEqual(task.sent_message.provider_message_id, task.provider_message_id)
        self.assertEqual(task.sent_message.source, "followup")
        self.assertEqual(task.sent_at, self.clock)
        self.assertEqual(self.dispatch(), (0, [], 0))
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.client_row, kind="thinking").count(), 1)
        self.assertFalse(policy._schedule_next_policy_step(task, self.client_row, now=self.clock))
        self.assert_history_preserved()

    def test_busy_lease_keeps_bound_new_episode_pending(self):
        task = self.scheduled_task()
        self.client_row.automation_lease_token = "another-worker"
        self.client_row.automation_lease_until = self.clock + timedelta(minutes=5)
        self.client_row.save(update_fields=["automation_lease_token", "automation_lease_until"])
        self.assertEqual(self.dispatch(), (0, [], 0))
        task.refresh_from_db()
        self.assertEqual((task.status, task.skip_reason, task.claim_token), ("pending", "", ""))
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.automation_lease_token, "another-worker")
        self.assert_history_preserved()

    def test_explicit_selection_keeps_ninety_minute_offset(self):
        task = self.scheduled_task()
        self.assertEqual(task.reason, "ordinary_requested_selection")
        self.assertEqual(task.due_at - task.policy_started_at, timedelta(minutes=90))
        sent, physical, calls = self.dispatch()
        self.assertEqual((sent, len(physical), calls), (1, 1, 1))
        task.refresh_from_db()
        self.assertEqual(task.status, "sent", task.last_error)
        self.assertEqual(self.dispatch(), (0, [], 0))
        self.assert_history_preserved()

    def test_missing_size_without_selection_request_never_creates_chase(self):
        self.assertEqual(self.client_row.current_size, "")
        self._sent()
        result = self._schedule(now=self.clock)
        self.assertEqual(result.reason, "current_purpose_not_followup_eligible")
        self.assertEqual(result.task_id, 0)
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.assert_history_preserved()

    def test_real_claim_renewal_rejects_changed_canonical_source(self):
        task = self.scheduled_task()
        claimed = policy._claim_due_followup(task.pk, now=self.clock, automation=instagram_bot)
        self.assertIsNotNone(claimed)
        claimed_task, client, lease_token = claimed
        self.source.text = "Змінений запит"
        self.source.save(update_fields=["text"])
        try:
            self.assertIsNone(policy._renew_due_followup_claim(
                task.pk, client.pk, lease_token, task_claim_token=claimed_task.claim_token,
                now=self.clock, automation=instagram_bot,
            ))
        finally:
            instagram_bot.release_client_automation_lease(client.pk, lease_token)
        task.refresh_from_db()
        self.assertEqual((task.status, task.skip_reason), ("skipped", "followup_commerce_binding_changed"))
        self.assertEqual(task.attempt_count, 0)
        self.assert_history_preserved()

    def test_current_bound_purchase_paid_after_schedule_still_blocks(self):
        current = IgDeal.objects.create(client=self.client_row, status="draft", amount=1090)
        self.episode.deal = current
        self.episode.save(update_fields=["deal"])
        task = self.scheduled_task()
        self.assertEqual(task.deal_id, current.pk)
        IgPaymentProjection.objects.create(
            client=self.client_row, deal=current, truth="confirmed", gross_amount=1090,
            paid_at=self.clock, provider_modified_at=self.clock,
        )
        self.assert_blocked(task, "already_converted")

    def test_missing_ordinary_binding_never_falls_back_to_lifetime_policy(self):
        template = self.scheduled_task()
        task = self.frozen_task_with_payload(template, {
            key: value for key, value in template.event_payload.items() if key != "commerce_binding"
        })
        self.assert_blocked(task, "followup_commerce_binding_missing")

    def test_nonobject_ordinary_binding_fails_closed(self):
        template = self.scheduled_task()
        task = self.frozen_task_with_payload(template, {
            **template.event_payload, "commerce_binding": [self.episode.pk],
        })
        self.assert_blocked(task, "followup_commerce_binding_missing")

    def test_forged_episode_binding_is_checked_against_canonical_source(self):
        template = self.scheduled_task()
        binding = {**template.event_payload["commerce_binding"], "episode_id": self.historical_episode.pk}
        task = self.frozen_task_with_payload(template, {**template.event_payload, "commerce_binding": binding})
        self.assert_blocked(task, "followup_commerce_binding_changed")

    def test_legacy_task_cannot_use_arbitrary_payload_binding_to_bypass_paid_guard(self):
        template = self.scheduled_task()
        task = self.frozen_task_with_payload(template, {**template.event_payload, "origin": "legacy"})
        self.assert_blocked(task, "already_converted")

    def test_changed_current_episode_blocks_before_transport(self):
        task = self.scheduled_task()
        self.episode.open_slot = None
        self.episode.save(update_fields=["open_slot"])
        replacement = IgCommercialEpisode.objects.create(
            client=self.client_row, sequence=3, materialization_key="dispatch-next-episode",
            opened_watermark_message_id=self.source.pk,
        )
        self.client_row.current_commercial_episode = replacement
        self.client_row.save(update_fields=["current_commercial_episode"])
        self.assert_blocked(task, "followup_commerce_binding_changed")

    def test_new_customer_statement_cancels_optional_send(self):
        task = self.scheduled_task()
        InstagramBotMessage.objects.create(
            client=self.client_row, sender_id=self.client_row.igsid, role="user",
            text="Дякую, більше не потрібно", status="done", source="webhook",
            provider_namespace=self.source.provider_namespace, provider_created_at=self.clock,
        )
        self.assert_blocked(task, "new_customer_statement")

    def test_client_cooldown_remains_global_above_new_episode(self):
        task = self.scheduled_task()
        IgFollowUpTask.objects.create(
            client=self.client_row, deal=self.historical_deal, due_at=self.clock - timedelta(hours=1),
            kind="thinking", status="sent", sent_at=self.clock - timedelta(hours=1),
            reason="historical-confirmed-touch", provider_message_id="old-touch",
        )
        self.assert_blocked(task, "frequency_limit")

    def test_closed_inbound_window_never_renews_from_bot_reply(self):
        task = self.scheduled_task()
        self.clock = task.meta_window_deadline
        self.assert_blocked(task, "meta_window_closed")

    def test_manager_takeover_after_scheduling_blocks(self):
        task = self.scheduled_task()
        self.client_row.manager_takeover = True
        self.client_row.save(update_fields=["manager_takeover"])
        self.assert_blocked(task, "manager_takeover")

    def test_pause_after_scheduling_blocks(self):
        task = self.scheduled_task()
        self.client_row.bot_paused = True
        self.client_row.save(update_fields=["bot_paused"])
        self.assert_blocked(task, "manager_takeover")

    def test_unknown_substantive_reply_never_creates_timer(self):
        self._sent(parts=2, unknown_last=True)
        result = self._schedule(now=self.clock)
        self.assertEqual(result.reason, "whole_substantive_reply_not_sent")
        self.assertFalse(result.ready)
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.assert_history_preserved()

    def test_unknown_followup_delivery_keeps_one_attempt_without_retry(self):
        task = self.scheduled_task()
        sent, physical, calls = self.dispatch(outcome="unknown")
        self.assertEqual((sent, len(physical), calls), (0, 1, 1))
        task.refresh_from_db()
        self.assertEqual((task.status, task.attempt_count), ("ambiguous", 1))
        self.assertEqual(task.provider_message_id, "")
        self.assertFalse(task.sent_message_id)
        self.assertTrue(IgFollowUpTask.objects.filter(delivery_review_for=task, kind="manager_task").exists())
        self.assertEqual(self.dispatch(), (0, [], 0))
        self.assert_history_preserved()

    def test_source_change_during_transport_preparation_is_rechecked(self):
        task = self.scheduled_task()

        def change_source():
            self.source.text = "Інший товар"
            self.source.save(update_fields=["text"])

        sent, physical, calls = self.dispatch(before_boundary=change_source)
        self.assertEqual((sent, physical, calls), (0, [], 1))
        task.refresh_from_db()
        self.assertEqual((task.status, task.skip_reason), ("skipped", "followup_commerce_binding_changed"))
        self.assertFalse(task.sent_message_id)
        self.assert_history_preserved()
