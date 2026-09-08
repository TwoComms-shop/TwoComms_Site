from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_checkout as checkout_fixtures
from management.models import (
    IgCheckoutProposal, IgClient, IgDeal, IgFollowUpTask, IgLifecycleEvent,
    IgOrderAttribution, IgWebhookInboxEvent, InstagramBotMessage,
)
from management.services.ig_revision_postbacks import apply_revision_postback
from management.services.ig_revision_outbox import PublicationBinding
from orders.models import Order


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop")
class RevisionPostbackTests(TransactionTestCase):
    reset_sequences = True
    postback_action = "got"

    def setUp(self):
        checkout_fixtures.RevisionCheckoutTests.setUp(self)
        self.task = IgFollowUpTask.objects.create(
            client=self.client_row, deal=self.deal, kind="manager_task",
            reason=f"parcel_reminder:{self.order.pk}", due_at=timezone.now() + timedelta(hours=1),
        )

    def _source(self, mid):
        self.order = Order.objects.create(
            full_name="Parcel buyer", phone="+380501112233", city="Kyiv", np_office="1",
            total_sum=Decimal("900.00"), payment_status="prepaid", status="ship",
            tracking_number="test-tracking", tracking_status_code=7,
        )
        self.deal = IgDeal.objects.create(client=self.client_row, order=self.order, amount=self.order.total_sum)
        raw = f"twc:1:parcel:{self.postback_action}:{self.order.pk}"
        return InstagramBotMessage.objects.create(
            client=self.client_row, sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            text=getattr(self, "source_text", "Забрав ✅"),
            quick_reply_payload=getattr(self, "payload_override", raw),
            mid=mid, status="pending", provider_created_at=timezone.now() - timedelta(minutes=3),
        )

    def _apply(self, **overrides):
        kwargs = dict(source_message_id=self.source.pk, settings_id=self.settings.pk,
                      settings_permission_epoch=self.settings.reply_permission_epoch,
                      publication=self.publication_binding)
        kwargs.update(overrides)
        return apply_revision_postback(self.revision.pk, self.revision_token, **kwargs)

    def _event(self, order, deal):
        from management.services.ig_commercial_episodes import ensure_episode_for_deal

        episode = ensure_episode_for_deal(deal)
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal, commercial_episode=episode, catalog_total=order.total_sum,
            quoted_total=order.total_sum, requested_payment_amount=order.total_sum,
            items_digest="a" * 64,
        )
        from orders.models import PaymentAttempt
        attempt = PaymentAttempt.objects.create(
            fingerprint=f"{order.pk:064x}", full_name=order.full_name,
            phone=order.phone, city=order.city, np_office=order.np_office,
            pay_type="online_full", status="converted", order=order,
            gross_amount=order.total_sum, payable_amount=order.total_sum,
            payment_amount=order.total_sum,
        )
        proposal.payment_attempt = attempt
        proposal.save(update_fields=["payment_attempt", "updated_at"])
        attribution = IgOrderAttribution.objects.create(
            order=order, client=self.client_row, deal=deal,
            creation_mode="linked_existing", payment_source="unknown",
        )
        return IgLifecycleEvent.objects.create(
            event_key=f"postback-test-parcel:{order.pk}", kind="parcel_arrived",
            client=self.client_row, order=order, deal=deal, proposal=proposal,
            commercial_episode=episode, attribution=attribution,
        )

    def test_got_cancels_only_exact_owned_order_reminders_without_carrier_claim(self):
        own_event = self._event(self.order, self.deal)
        other = Order.objects.create(full_name="Other parcel", phone=self.order.phone, city="Kyiv", np_office="2", total_sum=400)
        other_deal = IgDeal.objects.create(client=self.client_row, order=other, amount=400)
        other_event = self._event(other, other_deal)
        other_task = IgFollowUpTask.objects.create(client=self.client_row, deal=other_deal, reason=f"parcel_reminder:{other.pk}", due_at=timezone.now())
        before = (self.order.status, self.order.payment_status, self.order.tracking_status_code)
        result = self._apply()
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.origin, "postback")
        self.assertEqual(result.receipt["customer_assertion"], "parcel_picked_up")
        self.task.refresh_from_db()
        own_event.refresh_from_db()
        other_event.refresh_from_db()
        other_task.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.task.status, "cancelled")
        self.assertEqual(own_event.state, "cancelled")
        self.assertEqual(other_task.status, "pending")
        self.assertEqual(other_event.state, "pending")
        self.assertEqual((self.order.status, self.order.payment_status, self.order.tracking_status_code), before)
        self.assertNotIn("позначаю", result.reply_text)
        self.assertEqual(self.revision.delivery_effects.count(), 0)
        again = self._apply()
        self.assertTrue(again.replayed, again.reason)
        self.assertEqual(again.receipt, result.receipt)

    def test_foreign_order_even_with_same_phone_has_zero_effects(self):
        foreign = IgClient.objects.create(igsid="foreign-parcel-owner")
        self.deal.client = foreign
        self.deal.save(update_fields=["client", "updated_at"])
        result = self._apply()
        self.assertTrue(result.handled)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "postback_order_not_owned")
        self.task.refresh_from_db()
        self.revision.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
        self.assertNotIn("postback_decision", self.revision.action_receipts)
        self.assertEqual(self.revision.delivery_effects.count(), 0)

    def test_current_assignment_and_explicit_unlink_override_historical_edges(self):
        from management.services.ig_order_assignments import link_order_to_client, unlink_order_from_client

        assignment = link_order_to_client(self.order, client=self.client_row)
        unlink_order_from_client(self.order, client=self.client_row, expected_version=assignment.version, reason_code="fixture_unlink", reason="Explicit test unlink")
        result = self._apply()
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "postback_order_not_owned")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_paused_new_inbound_and_publication_drift_block_before_mutation(self):
        self.client_row.bot_paused = True
        self.client_row.save(update_fields=["bot_paused"])
        self.assertEqual(self._apply().reason, "client_blocked")
        self.client_row.bot_paused = False
        self.client_row.save(update_fields=["bot_paused"])
        event = IgWebhookInboxEvent.objects.create(
            namespace="instagram_login:owner-1", event_key="postback-new-inbound",
            owner_id="owner-1", customer_igsid=self.client_row.igsid,
            decision="accepted", payload={}, payload_digest="a" * 64,
        )
        self.assertEqual(self._apply().reason, "pending_inbound")
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at"])
        wrong = PublicationBinding(self.publication.pk, self.publication.version + 1, self.publication.snapshot_hash)
        self.assertEqual(self._apply(publication=wrong).reason, "publication_changed")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
        self.revision.refresh_from_db()
        self.assertFalse(self.revision.action_receipts)

    def test_receipt_failure_rolls_back_reminder_cancellation(self):
        original = type(self.revision).save

        def fail_receipt(instance, *args, **kwargs):
            if "action_receipts" in (kwargs.get("update_fields") or ()):
                raise ValueError("forced receipt failure")
            return original(instance, *args, **kwargs)

        with patch.object(type(self.revision), "save", fail_receipt):
            result = self._apply()
        self.assertFalse(result.ready)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_global_got_receipt_does_not_cancel_later_source_reminder_on_replay(self):
        from management.models import IgSourceActionReceipt, IgTurnMessage
        from management.services.ig_turn_revisions import create_collecting_revision
        from management.services.ig_revision_execution import prepare_revision

        first = self._apply()
        self.assertTrue(first.ready, first.reason)
        later_source = InstagramBotMessage.objects.create(
            client=self.client_row, sender_id=self.client_row.igsid, role="user", source="webhook",
            provider_namespace=self.source.provider_namespace, mid="later-source", text="Нагадати пізніше",
            quick_reply_payload=f"twc:1:parcel:later:{self.order.pk}", status="pending", provider_created_at=timezone.now(),
        )
        IgTurnMessage.objects.create(turn=self.turn, message=later_source, ordinal=2, role="user")
        child = create_collecting_revision(self.turn, [self.source, later_source], bypass_quiet=True).revision
        prepared = prepare_revision(child.pk, lambda **_kwargs: None)
        args = dict(settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.publication_binding)
        later = apply_revision_postback(child.pk, prepared.execution_token, source_message_id=later_source.pk, **args)
        self.assertTrue(later.ready, later.reason)
        self.assertTrue(later.requires_model)
        reminder = IgFollowUpTask.objects.get(pk=later.receipt["reminder_id"])
        replayed = apply_revision_postback(child.pk, prepared.execution_token, source_message_id=self.source.pk, **args)
        self.assertTrue(replayed.ready, replayed.reason)
        self.assertTrue(replayed.replayed)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "pending")
        self.assertEqual(IgSourceActionReceipt.objects.count(), 2)
        original = IgSourceActionReceipt.objects.get(source_message=self.source)
        original.outcome = {**original.outcome, "changed": True}
        with self.assertRaises(ValueError):
            original.save()

    def test_multilingual_ack_only_asserts_reminder_operation(self):
        from management.services.ig_revision_postbacks import _copy
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth

        for language, marker in (("uk", "скасовано"), ("ru", "отменены"), ("en", "cancelled")):
            reply = _copy("parcel_got", language)
            self.assertIn(marker, reply)
            self.assertTrue(validate_reply_truth(reply, context=ReplyTruthContext()).valid)
            self.assertNotIn("marking the order", reply)
            self.assertNotIn("как полученный", reply)
            self.assertNotIn("як отримане", reply)


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop")
class RevisionPostbackLaterTests(TransactionTestCase):
    reset_sequences = True
    postback_action = "later"
    setUp = RevisionPostbackTests.setUp
    _source = RevisionPostbackTests._source
    _apply = RevisionPostbackTests._apply

    def test_later_due_is_source_anchored_and_replay_never_moves_it(self):
        result = self._apply()
        self.assertTrue(result.ready, result.reason)
        reminder = IgFollowUpTask.objects.get(pk=result.receipt["reminder_id"])
        due = self.source.provider_created_at + timedelta(hours=20)
        self.assertEqual(reminder.due_at, due)
        self.assertEqual(reminder.trigger, "reactive")
        self.assertEqual(reminder.event_payload["source_message_id"], self.source.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "cancelled")
        later = self._apply(now=timezone.now() + timedelta(seconds=1))
        self.assertTrue(later.replayed, later.reason)
        reminder.refresh_from_db()
        self.assertEqual(reminder.due_at, due)
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=reminder.event_key).count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.client_row, status="pending").count(), 1)
        self.assertNotIn("у відділенні", reminder.message_text)


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop")
class RevisionPostbackInertTests(TransactionTestCase):
    reset_sequences = True
    postback_action = "got"
    payload_override = "twc:1:diagnostic:inout:1"
    setUp = RevisionPostbackTests.setUp
    _source = RevisionPostbackTests._source
    _apply = RevisionPostbackTests._apply

    def test_diagnostic_has_only_execution_receipt_no_business_mutation(self):
        result = self._apply()
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.receipt["action"], "diagnostic")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(self.revision.delivery_effects.count(), 0)

    def test_mixed_question_is_never_consumed_as_only_button_ack(self):
        from management.services.ig_revision_postbacks import _mixed_sources

        selected = self.revision.bundle_snapshot["sources"][0]
        other = {"message_id": 999, "role": "user", "text": "А які розміри є?", "quick_reply_payload": "", "media_parts": []}
        self.assertTrue(_mixed_sources([other, selected], selected))
        other["text"] = "👍"
        self.assertFalse(_mixed_sources([other, selected], selected))

    def test_unknown_payment_payload_and_wrong_version_remain_not_handled(self):
        from management.services.ig_revision_postbacks import _candidate

        self.assertIsNone(_candidate({"quick_reply_payload": "twc:v1:checkout_payment:prepay_200_cod"}, self.client_row.pk))
        self.assertIsNone(_candidate({"quick_reply_payload": "twc:2:parcel:got:1"}, self.client_row.pk))
        self.assertIsNone(_candidate({"quick_reply_payload": "twc:1:diagnostic:inout:99"}, self.client_row.pk))
        self.assertEqual(_candidate({"text": f"twc:1:preview:buttons:size_m:{self.client_row.pk}"}, self.client_row.pk)["action"], "preview")


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop")
class RevisionPostbackPreviewTests(TransactionTestCase):
    reset_sequences = True
    postback_action = "got"
    payload_override = "twc:1:preview:buttons:size_m:1"
    setUp = RevisionPostbackTests.setUp
    _source = RevisionPostbackTests._source
    _apply = RevisionPostbackTests._apply

    def test_preview_only_records_inert_receipt(self):
        result = self._apply()
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.receipt["action"], "preview")
        self.assertEqual(result.receipt["variant"], "buttons")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
        self.assertEqual(IgFollowUpTask.objects.count(), 1)


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.shop")
class RevisionPostbackUnknownTests(TransactionTestCase):
    reset_sequences = True
    postback_action = "got"
    payload_override = "twc:v1:checkout_payment:prepay_200_cod"
    setUp = RevisionPostbackTests.setUp
    _source = RevisionPostbackTests._source
    _apply = RevisionPostbackTests._apply

    def test_payment_choice_continues_ordinary_checkout_source_path(self):
        result = self._apply()
        self.assertFalse(result.handled)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "not_handled")
        self.revision.refresh_from_db()
        self.assertFalse(self.revision.action_receipts)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
