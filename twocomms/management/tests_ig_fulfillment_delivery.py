"""Native effect recovery and truthful bounded manager updates.

The signed decision adapter is mocked here; its source/cart admission has its
own integration tests. These tests use actual task/message rows and native
receipt callbacks, never a provider network connection.
"""
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch
import os
import json

from django.test import SimpleTestCase, TestCase
from management.models import IgClient, IgFollowUpTask, InstagramBotMessage, InstagramBotSettings
from management.services import ig_fulfillment_delivery as delivery
from management.services import ig_fulfillment_reviews as reviews
from management.services.instagram_bot import ProviderDeliveryReceipt


def projection(locale="uk", count=2):
    anchor = "2026-10-12T09:00:00+00:00"
    return {"ready": True, "proof": {"request": "signed-request", "decision": "signed-v1"},
        "current_request": {"id": "request-1", "client_id": 1, "source_id": 1, "scope": "cart", "locale": locale,
            "customer_requirements": [], "lines": [{"line_id": str(index), "label": f"Item {index}"} for index in range(1, count + 1)]},
        "decision": {"id": "decision-1", "version": 1, "decided_at": anchor, "split_offer": None,
            "lines": [{"line_id": str(index), "availability": "available" if index == 1 else "wait",
                "preparation": {"min_days": 1 if index == 1 else 7, "max_days": 3 if index == 1 else 10,
                    "unit": "calendar", "anchor_at": anchor}, "dispatch": None,
                "alternative": "", "clarification": ""} for index in range(1, count + 1)]}}


class FulfillmentUpdateCopyTests(SimpleTestCase):
    def test_zero_day_readiness_is_anchored_to_manager_check_not_future_send_day(self):
        for locale in ("uk", "ru", "en"):
            data = projection(locale, count=1)
            data["decision"]["lines"][0]["preparation"].update(min_days=0, max_days=0)
            text = delivery.render_manager_update(data)
            self.assertIn(delivery.COPY[locale]["same_day"], text)
            self.assertIn("12.10.2026", text)
            self.assertNotIn("0 " + delivery.COPY[locale]["calendar"], text)
            self.assertIn(delivery.COPY[locale]["arrival"], text)

    def test_plain_available_confirmation_only_warns_about_delivery_when_relevant(self):
        for locale in ("uk", "ru", "en"):
            data = projection(locale, count=1)
            data["decision"]["lines"][0]["preparation"] = None
            self.assertNotIn(delivery.COPY[locale]["arrival"], delivery.render_manager_update(data))
            data["current_request"]["customer_requirements"] = [{"date_status": "resolved"}]
            self.assertIn(delivery.COPY[locale]["arrival"], delivery.render_manager_update(data))

    def test_locales_preserve_phase_range_unit_manager_day_and_mixed_availability(self):
        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale):
                data = projection(locale)
                data["decision"]["lines"][1]["preparation"]["unit"] = "business"
                text = delivery.render_manager_update(data)
                self.assertIn("1–3", text)
                self.assertIn("7–10", text)
                self.assertIn("12.10.2026", text)
                self.assertIn(delivery.COPY[locale]["preparation"], text)
                self.assertIn(delivery.COPY[locale]["business"], text)
                self.assertIn(delivery.COPY[locale]["wait"], text)
                self.assertIn(delivery.COPY[locale]["arrival"], text)
                self.assertLessEqual(len(text.encode()), 950)

    def test_many_lines_group_only_identical_complete_typed_facts(self):
        data = projection(count=20)
        for row in data["current_request"]["lines"]:
            row["label"] = "Довга назва товару " * 5
        text = delivery.render_manager_update(data)
        self.assertIn("поз. 1:", text)
        self.assertIn("поз. 2, 3, 4", text)
        self.assertIn("1–3", text)
        self.assertIn("7–10", text)
        self.assertLessEqual(len(text.encode()), 950)

    def test_heterogeneous_dates_are_never_merged_or_truncated(self):
        data = projection(count=20)
        for index, row in enumerate(data["decision"]["lines"], 1):
            row["preparation"]["min_days"] = index
            row["preparation"]["max_days"] = index + 1
        self.assertEqual(delivery.render_manager_update(data), "")

    def test_invalid_unanchored_unit_or_duplicate_line_identity_fails_closed(self):
        for bad in ({"anchor_at": None}, {"min_days": True}, {"unit": "weeks"}, {"max_days": 0}):
            data = projection()
            data["decision"]["lines"][0]["preparation"].update(bad)
            self.assertEqual(delivery.render_manager_update(data), "")
        data = projection()
        data["decision"]["lines"][1]["line_id"] = "1"
        self.assertEqual(delivery.render_manager_update(data), "")

    def test_new_frozen_copy_is_bounded_and_exact_for_three_languages_and_two_to_six_lines(self):
        for locale in ("uk", "ru", "en"):
            for count in range(2, 7):
                with self.subTest(locale=locale, lines=count):
                    data = projection(locale, count=count)
                    text = delivery.render_manager_update(data, copy_version=2)
                    self.assertTrue(text)
                    self.assertLessEqual(len(text.encode("utf-8")), 950)
                    task = SimpleNamespace(pk=count, client_id=1, event_key=f"native-copy:{locale}:{count}",
                        event_payload={"request_id": "request-1", "decision_id": "decision-1"}, manager_context={})
                    # Use the actual production keyring and signer. A frozen
                    # valid body survives a copy-template change; changing a
                    # byte without a new signature cannot become valid copy.
                    delivery._freeze_task_update(task, data, text)
                    self.assertEqual(delivery.render_task_update(task, data), text)
                    task.manager_context[delivery.CUSTOMER_COPY_KEY]["body"]["text"] += "!"
                    self.assertEqual(delivery.render_task_update(task, data), "")

    def test_dispatch_is_distinct_and_split_never_asserts_price_or_payer(self):
        data = projection("en", count=1)
        data["decision"]["lines"][0]["dispatch"] = data["decision"]["lines"][0].pop("preparation")
        data["decision"]["split_offer"] = {"feasible": True, "groups": [["1"]], "extra_delivery_amount": "123.45", "payer": "customer"}
        data["current_request"]["customer_requirements"] = [{"date_status": "resolved"}]
        text = delivery.render_manager_update(data)
        self.assertIn("dispatch 1–3", text)
        self.assertIn("Would you like", text)
        self.assertIn("required date", text)
        self.assertNotIn("123.45", text)
        self.assertNotIn("customer pays", text)

    def test_raw_manager_prose_and_urls_are_not_sent(self):
        data = projection("en", count=1)
        data["current_request"]["lines"][0]["label"] = "https://private.invalid/token"
        data["decision"]["lines"][0]["alternative"] = "Guaranteed delivery tomorrow https://secret.invalid"
        data["decision"]["lines"][0]["clarification"] = "internal-provider-MID"
        text = delivery.render_manager_update(data)
        self.assertNotIn("http", text)
        self.assertNotIn("Guaranteed", text)
        self.assertNotIn("internal-provider", text)


class FulfillmentNativeDeliveryTests(TestCase):
    def setUp(self):
        coordination = patch("management.services.ig_fulfillment_notification_coordination.cart_update_coordination", return_value={"reason": ""})
        coordination.start()
        self.addCleanup(coordination.stop)
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=datetime_timezone.utc)
        self.settings, _created = InstagramBotSettings.objects.update_or_create(pk=1, defaults={"is_enabled": True, "ig_user_id": "owner-1"})
        self.client_row = IgClient.objects.create(igsid="fulfillment-delivery-client", reply_permission_epoch=3)
        self.source = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="user", source="webhook", status="done", text="Please check these items", mid="fulfillment-source",
            provider_namespace="instagram_login:owner-1", provider_created_at=self.now)
        self.data = projection()
        self.data["current_request"].update(client_id=self.client_row.pk, source_id=self.source.pk, source_namespace=self.source.provider_namespace)
        self.data["proof"].update(recipient=self.client_row.igsid, source_namespace=self.source.provider_namespace)
        self.task = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            reason=delivery.ORIGIN, kind="fulfillment", trigger="event", event_key="ig-fulfillment-update:request-1:1",
            event_payload={"origin": delivery.ORIGIN, "request_id": "request-1", "decision_id": "decision-1",
                "decision_version": 1})
        # Only the separately tested manager admission projection is mocked.
        # Frozen customer copy uses the production keyring and signer, so
        # recovery and physical-boundary tests retain real HMAC verification.
        self.core = SimpleNamespace(_enabled=lambda: True, _keys=reviews._keys, _sign=reviews._sign,
            validated_update_task=lambda *args, **kwargs: deepcopy(self.data),
            validated_update_history=lambda task: {**deepcopy(self.data), "recipient": self.client_row.igsid,
                "source_namespace": "instagram_login:owner-1"})
        env = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        core = patch.object(delivery, "_core", return_value=self.core)
        core.start()
        self.addCleanup(core.stop)
        for target, value in (("acquire_client_automation_lease", (self.client_row, "lease")),
                ("renew_client_automation_lease", True), ("release_client_automation_lease", None)):
            mocked = patch("management.services.instagram_bot." + target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        permission = patch("management.services.ig_reply_boundary.capture_reply_permission", return_value={})
        permission.start()
        self.addCleanup(permission.stop)

    def run_sender(self, implementation, *, now=None):
        with patch("management.services.instagram_bot.send_text", side_effect=implementation) as sender:
            result = delivery.process_due_fulfillment_updates(self.settings, now=now or self.now)
        self.task.refresh_from_db()
        return result, sender

    def successful_sender(self, settings, recipient, text, **kwargs):
        self.assertEqual(recipient, self.client_row.igsid)
        self.assertTrue(kwargs["return_receipt"])
        self.assertFalse(kwargs["allow_url_fallback"])
        with kwargs["provider_request_boundary_factory"](planned_chunk_count=1, delivered_chunk_count=0) as allowed:
            self.assertTrue(allowed, getattr(allowed, "reason", ""))
            self.task.refresh_from_db()
            self.assertEqual((self.task.status, self.task.attempt_count), ("processing", 1))
            self.assertTrue(kwargs["provider_message_callback"]("fulfillment-native-mid"))
            self.task.refresh_from_db()
            self.assertEqual(self.task.status, "sent")
            self.assertIsNone(self.task.sent_message_id)
        return ProviderDeliveryReceipt(True, "", provider_message_id="fulfillment-native-mid",
            provider_message_ids=("fulfillment-native-mid",), planned_chunk_count=1, delivered_chunk_count=1, request_text=text)

    def test_native_mid_checkpoint_precedes_history_and_second_drain_never_replays(self):
        result, sender = self.run_sender(self.successful_sender)
        self.assertEqual(result, 1)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.task.sent_message.provider_message_id, "fulfillment-native-mid")
        self.assertEqual(self.task.sent_message.text, self.task.message_text)
        self.assertEqual(self.task.sent_message.provider_namespace, self.source.provider_namespace)
        result, sender = self.run_sender(self.successful_sender)
        self.assertEqual(result, 0)
        sender.assert_not_called()

    def test_real_sender_has_one_physical_request_with_native_checkpoint(self):
        physical = []
        def provider(settings, url, *, data, **kwargs):
            self.task.refresh_from_db()
            self.assertEqual((self.task.status, self.task.attempt_count), ("processing", 1))
            payload = json.loads(data)
            physical.append(payload)
            return 200, json.dumps({"message_id": "fulfillment-real-native-mid"})
        with patch("management.services.instagram_bot.get_page_token", return_value="synthetic-test-token"), \
                patch("management.services.instagram_bot._provider_http", side_effect=provider), \
                patch("management.services.ig_reply_boundary.customer_send_boundary", return_value=nullcontext(True)):
            self.assertEqual(delivery.process_due_fulfillment_updates(self.settings, now=self.now), 1)
            self.assertEqual(delivery.process_due_fulfillment_updates(self.settings, now=self.now), 0)
        self.task.refresh_from_db()
        self.assertEqual(len(physical), 1)
        self.assertEqual(physical[0]["recipient"]["id"], self.client_row.igsid)
        self.assertEqual(physical[0]["message"]["text"], self.task.message_text)
        self.assertEqual(self.task.provider_message_id, "fulfillment-real-native-mid")
        self.assertEqual(self.task.sent_message.provider_message_id, "fulfillment-real-native-mid")

    def test_real_sender_missing_native_mid_is_unknown_without_memory_or_replay(self):
        with patch("management.services.instagram_bot.get_page_token", return_value="synthetic-test-token"), \
                patch("management.services.instagram_bot._provider_http", return_value=(200, "{}")) as provider, \
                patch("management.services.ig_reply_boundary.customer_send_boundary", return_value=nullcontext(True)):
            self.assertEqual(delivery.process_due_fulfillment_updates(self.settings, now=self.now), 0)
            self.assertEqual(delivery.process_due_fulfillment_updates(self.settings, now=self.now + timedelta(minutes=16)), 0)
        self.task.refresh_from_db()
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(self.task.status, "ambiguous")
        self.assertIsNone(self.task.sent_message_id)

    def test_crash_after_checkpoint_repairs_memory_without_second_http_outside_window(self):
        with patch.object(delivery, "_history", return_value=False):
            result, _ = self.run_sender(self.successful_sender)
        self.assertEqual(result, 1)
        self.assertIsNone(self.task.sent_message_id)
        result, sender = self.run_sender(self.successful_sender, now=self.now + timedelta(days=2))
        self.assertEqual(result, 0)
        sender.assert_not_called()
        self.assertIsNotNone(self.task.sent_message_id)

    def test_unknown_after_admission_is_durable_and_never_retryable(self):
        def unknown(settings, recipient, text, **kwargs):
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=1) as allowed:
                self.assertTrue(allowed)
            raise RuntimeError("connection lost after dispatch")
        result, _ = self.run_sender(unknown)
        self.assertEqual(result, 0)
        self.assertEqual(self.task.status, "ambiguous")
        self.assertIsNone(self.task.sent_message_id)
        _, sender = self.run_sender(unknown, now=self.now + timedelta(minutes=16))
        sender.assert_not_called()

    def test_mutated_decision_at_physical_boundary_is_cancelled_without_http(self):
        physical = []
        def changed(settings, recipient, text, **kwargs):
            self.data["proof"]["decision"] = "different-signed-version"
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=1) as allowed:
                if allowed:
                    physical.append(True)
                else:
                    return ProviderDeliveryReceipt(False, "cancelled", failure_boundary="chunk1:provider_request_rejected", planned_chunk_count=1)
        self.run_sender(changed)
        self.assertEqual(physical, [])
        self.assertEqual(self.task.status, "cancelled")
        self.assertEqual(self.task.skip_reason, "physical_message_changed")

    def test_window_uses_actual_provider_clock_and_waits_without_changing_due_policy(self):
        self.source.provider_created_at = self.now - timedelta(hours=23)
        self.source.save(update_fields=["provider_created_at"])
        self.client_row.last_user_message_at = self.now
        self.client_row.save(update_fields=["last_user_message_at"])
        _, sender = self.run_sender(self.successful_sender)
        sender.assert_not_called()
        self.assertEqual((self.task.status, self.task.skip_reason), ("pending", "waiting_window"))
        self.assertEqual(self.task.due_at, self.now)
        self.assertEqual(self.task.attempt_count, 0)

    def test_quiet_hours_and_new_pending_customer_message_wait_without_http(self):
        _, sender = self.run_sender(self.successful_sender, now=self.now + timedelta(hours=10))
        sender.assert_not_called()
        self.assertEqual(self.task.skip_reason, "quiet_hours")
        self.task.next_attempt_at = None
        self.task.save(update_fields=["next_attempt_at"])
        InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="user", source="webhook", status="pending", text="Change size", mid="fulfillment-new-source",
            provider_namespace=self.source.provider_namespace, provider_created_at=self.now)
        _, sender = self.run_sender(self.successful_sender)
        sender.assert_not_called()
        self.assertEqual(self.task.skip_reason, "pending_inbound")

    def test_processing_crash_and_untrusted_tuple_success_never_create_memory(self):
        self.task.status, self.task.claim_until = "processing", self.now - timedelta(seconds=1)
        self.task.save(update_fields=["status", "claim_until"])
        _, sender = self.run_sender(self.successful_sender)
        sender.assert_not_called()
        self.assertEqual(self.task.status, "ambiguous")
        self.assertIsNone(self.task.sent_message_id)

    def test_untrusted_tuple_after_admission_is_unknown_not_success(self):
        def legacy(settings, recipient, text, **kwargs):
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=1) as allowed:
                self.assertTrue(allowed)
            return (True, "", "")
        self.run_sender(legacy)
        self.assertEqual(self.task.status, "ambiguous")
        self.assertIsNone(self.task.sent_message_id)

    def test_native_receipt_identity_change_after_checkpoint_cannot_create_memory(self):
        def changed_receipt(settings, recipient, text, **kwargs):
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=1) as allowed:
                self.assertTrue(allowed)
                kwargs["provider_message_callback"]("checkpoint-mid")
            return ProviderDeliveryReceipt(True, "", provider_message_ids=("different-mid",),
                planned_chunk_count=1, delivered_chunk_count=1, request_text=text)
        result, _ = self.run_sender(changed_receipt)
        self.assertEqual(result, 0)
        self.assertEqual((self.task.status, self.task.last_error), ("ambiguous", "native_receipt_identity_changed"))
        self.assertIsNone(self.task.sent_message_id)

    def test_known_permission_rejection_before_physical_request_is_retryable_hold(self):
        def denied(settings, recipient, text, **kwargs):
            return ProviderDeliveryReceipt(False, "cancelled", planned_chunk_count=1,
                failure_boundary="chunk:1:permission_epoch_changed")
        self.run_sender(denied)
        self.assertEqual((self.task.status, self.task.attempt_count), ("pending", 0))
        self.assertIsNone(self.task.sent_message_id)

    def test_pending_manager_admission_does_not_claim_or_send(self):
        self.core.validated_update_task = lambda *args, **kwargs: {"ready": False, "reason": "manager_review_pending"}
        _, sender = self.run_sender(self.successful_sender)
        sender.assert_not_called()
        self.assertEqual((self.task.status, self.task.skip_reason, self.task.attempt_count), ("pending", "manager_review_pending", 0))

    def test_sent_memory_repair_refuses_changed_text_or_erased_signed_binding(self):
        with patch.object(delivery, "_history", return_value=False):
            self.run_sender(self.successful_sender)
        self.task.message_text = "Guaranteed arrival tomorrow"
        self.task.save(update_fields=["message_text"])
        _, sender = self.run_sender(self.successful_sender)
        sender.assert_not_called()
        self.assertIsNone(self.task.sent_message_id)

    def test_sent_memory_repair_refuses_changed_customer_copy_hmac_without_replay(self):
        with patch.object(delivery, "_history", return_value=False):
            result, _sender = self.run_sender(self.successful_sender)
        self.assertEqual(result, 1)
        self.assertIsNone(self.task.sent_message_id)
        context = deepcopy(self.task.manager_context)
        context[delivery.CUSTOMER_COPY_KEY]["hmac"] = "0" * 64
        self.task.manager_context = context
        self.task.save(update_fields=["manager_context"])
        _result, sender = self.run_sender(self.successful_sender, now=self.now + timedelta(days=2))
        sender.assert_not_called()
        self.assertIsNone(self.task.sent_message_id)
        self.assertEqual(self.task.provider_message_id, "fulfillment-native-mid")

    def test_disabled_consumer_standing_restock_and_stale_admission_have_no_http(self):
        self.core._enabled = lambda: False
        _, sender = self.run_sender(self.successful_sender)
        sender.assert_not_called()
        self.core._enabled = lambda: True
        self.data["current_request"]["scope"] = "restock"
        _, sender = self.run_sender(self.successful_sender)
        sender.assert_not_called()
        self.assertEqual(self.task.status, "cancelled")
