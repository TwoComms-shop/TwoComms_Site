"""Actual native restock interest remains visible only in its own manager case."""
from datetime import timedelta
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
from management.models import IgFunnelResetAudit, IgFollowUpTask
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_restock_context import capture_restock_interest
from management.services import ig_restock_subscriptions as restock
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeRestockContextTests(_RestockFixture, TestCase):
    def interest(self, *, ack=False):
        invitation, subscription = self.accepted(ack=ack)
        request = IgFulfillmentReviewRequest.objects.select_related("client").get(pk=invitation.envelope["manager_review"]["request_id"])
        now = subscription.accepted_answer.answered_at
        cart = capture_current_selection_lines(self.client_row.pk, now=now)
        self.assertEqual(cart["status"], "captured", cart)
        return invitation, subscription, request, cart, now

    def captured(self, request, cart, now):
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            result = capture_restock_interest(request, self.settings, cart, now)
        provider.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertLessEqual(len(queries), 8, [row["sql"] for row in queries])
        return result

    def test_real_native_accept_is_visible_after_answer_watermark_and_has_no_extra_authority(self):
        invitation, subscription, request, cart, now = self.interest()
        result = self.captured(request, cart, now)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(result["target"], invitation.target)
        self.assertEqual(result["accepted_at"], subscription.accepted_answer.answered_at.isoformat())
        self.assertEqual(result["notification_state"], "pending")
        self.assertEqual(result["native_future_capability"], "unverified")
        self.assertEqual({ref["id"] for ref in result["source_refs"]}, {invitation.request_source_id, subscription.accepted_answer.source_id})
        self.assertEqual(set(result), {"status", "target", "label", "accepted_at", "expires_at", "source_refs", "notification_state", "notification_receipt", "native_future_capability"})

    def test_pre_answer_capture_cannot_see_future_agreement(self):
        invitation, subscription, request, _, now = self.interest()
        cart = capture_current_selection_lines(self.client_row.pk, now=now - timedelta(seconds=1))
        result = self.captured(request, cart, now - timedelta(seconds=1))
        self.assertIn(result["status"], {"unverified"})
        self.assertIsNone(result["accepted_at"])

    def test_neutral_later_source_keeps_same_owned_interest_without_new_purchase_leak(self):
        _, _, request, _, now = self.interest()
        self.source("Дякую", at=now + timedelta(seconds=1))
        cart = capture_current_selection_lines(self.client_row.pk, now=now + timedelta(seconds=1))
        self.assertEqual(self.captured(request, cart, now + timedelta(seconds=1))["status"], "accepted")
        self.canonical("измените количество на 2", at=now + timedelta(seconds=2))
        changed = capture_current_selection_lines(self.client_row.pk, now=now + timedelta(seconds=2))
        self.assertIsNone(self.captured(request, changed, now + timedelta(seconds=2)))

    def test_reset_or_altered_actual_answer_source_never_reuses_acceptance(self):
        _, subscription, request, cart, now = self.interest()
        answer = subscription.accepted_answer
        answer.source.text = "підроблений зміст"
        answer.source.save(update_fields=["text"])
        self.assertEqual(self.captured(request, cart, now)["status"], "unverified")
        IgFunnelResetAudit.objects.create(client=self.client_row, reset_after_message_id=answer.source_id)
        reset_cart = capture_current_selection_lines(self.client_row.pk, now=now)
        self.assertIsNone(self.captured(request, reset_cart, now))

    def test_bare_sent_runtime_flag_never_fabricates_native_receipt(self):
        _, subscription, request, cart, now = self.interest()
        type(subscription).objects.filter(pk=subscription.pk).update(state="sent")
        sent = self.captured(request, cart, now)
        self.assertEqual(sent["status"], "accepted")
        self.assertEqual(sent["notification_state"], "unknown")
    def test_bare_revoked_runtime_flag_never_fabricates_native_cancellation(self):
        _, subscription, request, cart, now = self.interest()
        type(subscription).objects.filter(pk=subscription.pk).update(state="revoked")
        self.assertEqual(self.captured(request, cart, now)["status"], "unverified")

    def test_native_cancellation_needs_actual_delivered_ack_card(self):
        invitation, subscription, request, _, now = self.interest(ack=True)
        result, source, _, _ = self.reply(invitation, action="revoke", at=now + timedelta(seconds=1))
        self.assertEqual(result.reason, "revoke", result)
        cart = capture_current_selection_lines(self.client_row.pk, now=source.provider_created_at)
        result = self.captured(request, cart, source.provider_created_at)
        self.assertEqual(result["status"], "revoked", result)
        self.assertIn(source.pk, {ref["id"] for ref in result["source_refs"]})

    def test_historical_unversioned_ack_cancellation_remains_visible_read_only(self):
        original_seal = restock._seal

        def old_seal(row, kind, **proof):
            proof.pop("ack_copy_version", None)
            return original_seal(row, kind, **proof)

        def old_copy(inv, kind, **kwargs):
            return restock._copy(inv.locale, kind, inv.envelope["label"])

        with patch.object(restock, "_seal", side_effect=old_seal), \
                patch.object(restock, "_copy_for", side_effect=old_copy):
            invitation, subscription, request, _, now = self.interest(ack=True)
        self.assertNotIn("ack_copy_version", subscription.accepted_answer.envelope)
        result, source, _, _ = self.reply(invitation, action="revoke", at=now + timedelta(seconds=1))
        self.assertEqual(result.reason, "revoke", result)
        cart = capture_current_selection_lines(self.client_row.pk, now=source.provider_created_at)
        result = self.captured(request, cart, source.provider_created_at)
        self.assertEqual(result["status"], "revoked", result)
        self.assertIn(source.pk, {ref["id"] for ref in result["source_refs"]})

    def bad_signed_version(self, marker):
        original_seal = restock._seal

        def changed_seal(row, kind, **proof):
            if kind == "answer":
                proof["ack_copy_version"] = marker
            return original_seal(row, kind, **proof)

        def old_copy(inv, kind, **kwargs):
            return restock._copy(inv.locale, kind, inv.envelope["label"])

        with patch.object(restock, "_seal", side_effect=changed_seal), \
                patch.object(restock, "_copy_for", side_effect=old_copy):
            _invitation, subscription, request, cart, now = self.interest()
        self.assertTrue(restock._verified(subscription.accepted_answer, "answer"))
        self.assertEqual(self.captured(request, cart, now)["status"], "unverified")

    def test_unknown_signed_ack_version_is_unverified_in_read_model(self):
        self.bad_signed_version("unknown-future-version")

    def test_null_signed_ack_version_is_unverified_in_read_model(self):
        self.bad_signed_version(None)

    def test_expired_native_agreement_remains_informational_without_future_send_grant(self):
        _, subscription, request, _, _ = self.interest()
        later = subscription.expires_at + timedelta(seconds=1)
        cart = capture_current_selection_lines(self.client_row.pk, now=later)
        result = self.captured(request, cart, later)
        self.assertEqual(result["status"], "expired", result)
        self.assertEqual(result["native_future_capability"], "unverified")


@override_settings(IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeRestockReceiptContextTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    # Reuse actual admitted-source/daemon transport fixtures, without inheriting
    # another TestCase and accidentally running all of its tests a second time.
    from management import tests_ig_fulfillment_notification_coordination as _fixtures
    complete_input = _fixtures.NativeAvailabilityCoordinationTests.complete_input
    decide = _fixtures.NativeAvailabilityCoordinationTests.decide
    pair = _fixtures.NativeAvailabilityCoordinationTests.pair
    send = _fixtures.NativeAvailabilityCoordinationTests.send

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="native-restock-context-manager",
            email="context-manager@example.test", password="test")

    def receipt_context(self, request, *, at=None):
        now = at or self.now + timedelta(seconds=4)
        request = type(request).objects.select_related("client").get(pk=request.pk)
        cart = capture_current_selection_lines(request.client_id, now=now)
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            result = capture_restock_interest(request, self.settings, cart, now)
        provider.assert_not_called()
        self.assertLessEqual(len(queries), 8, [row["sql"] for row in queries])
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        return result

    def delegated(self):
        _, subscription, request, original, event = self.pair()
        self.assertEqual(self.send("cart")[0], 1)
        self.assertEqual(self.send("restock", at=self.now + timedelta(seconds=4)), (0, []))
        original.refresh_from_db()
        event.task.refresh_from_db()
        subscription.refresh_from_db()
        return subscription, request, original, event

    def test_delegated_delivery_reports_original_actual_native_producer_without_copying_mid(self):
        subscription, request, original, event = self.delegated()
        result = self.receipt_context(request)
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(result["notification_state"], "sent", result)
        self.assertEqual(result["notification_receipt"], {"purpose": "fulfillment_manager_update",
            "task_id": original.pk, "sent_at": original.sent_at.isoformat(), "native_receipt_confirmed": True})
        self.assertEqual(event.task.provider_message_id, "")
        self.assertIsNone(event.task.sent_message_id)
        self.assertNotIn("native-coordination-cart", str(result))

    def test_tampered_delegation_or_changed_original_native_body_is_unknown(self):
        _, request, original, event = self.delegated()
        context = deepcopy(event.task.manager_context)
        changed = deepcopy(context)
        changed["delegated_delivery"]["hmac"] = "0" * 64
        IgFollowUpTask.objects.filter(pk=event.task_id).update(manager_context=changed)
        self.assertEqual(self.receipt_context(request)["notification_state"], "unknown")
        IgFollowUpTask.objects.filter(pk=event.task_id).update(manager_context=context)
        IgFollowUpTask.objects.filter(pk=original.pk).update(message_text="unrelated native text")
        result = self.receipt_context(request)
        self.assertEqual(result["notification_state"], "unknown")
        self.assertIsNone(result["notification_receipt"])

    def test_standalone_sent_requires_actual_exact_body_attempt_and_after_acceptance_date(self):
        _, subscription, request, _, event = self.pair(original_decision=False)
        self.assertEqual(self.send("restock")[0], 1)
        result = self.receipt_context(request)
        self.assertEqual(result["notification_state"], "sent", result)
        self.assertEqual(result["notification_receipt"]["purpose"], "subscribed_restock")
        event.task.refresh_from_db()
        saved = {"attempt_count": event.task.attempt_count, "sent_at": event.task.sent_at, "message_text": event.task.message_text}
        for changes in ({"attempt_count": 0}, {"sent_at": subscription.accepted_answer.answered_at - timedelta(seconds=1)},
                {"message_text": "unrelated receipt body"}):
            with self.subTest(changes=changes):
                IgFollowUpTask.objects.filter(pk=event.task_id).update(**{**saved, **changes})
                result = self.receipt_context(request)
                self.assertEqual(result["notification_state"], "unknown")
                self.assertIsNone(result["notification_receipt"])
