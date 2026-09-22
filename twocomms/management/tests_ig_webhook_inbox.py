import hashlib
import hmac
import json
import os
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgWebhookInboxEvent, InstagramBotMessage, InstagramBotSettings
from management.services.ig_webhook_inbox import (
    _queue_identity_reconciliation_notification,
    drain_webhook_inbox,
    has_pending_ingress,
    inbox_status,
)


@override_settings(ROOT_URLCONF="twocomms.urls_management", IG_WEBHOOK_INBOX_ENABLED=True)
class WebhookInboxTests(TestCase):
    secret = "inbox-secret"

    def setUp(self):
        settings_obj = InstagramBotSettings.load()
        settings_obj.page_id = "owner-1"
        settings_obj.save(update_fields=["page_id"])

    def _post(self, payload):
        raw = json.dumps(payload).encode()
        digest = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"IG_APP_SECRET": self.secret}, clear=True), patch(
            "management.bot_webhook.bot.record_raw_event"
        ):
            return self.client.post(
                "/bot/webhook/", data=raw, content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=f"sha256={digest}",
            )

    @staticmethod
    def _event(sender, mid):
        return {"sender": {"id": sender}, "recipient": {"id": "owner-1"}, "message": {"mid": mid, "text": "Hi"}}

    def test_accepts_once_and_dedupes_by_namespace_and_provider_mid(self):
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [self._event("user-1", "mid-1")]}]}

        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(IgWebhookInboxEvent.objects.count(), 1)
        row = IgWebhookInboxEvent.objects.get()
        self.assertEqual(row.decision, "accepted")
        self.assertEqual(row.customer_igsid, "user-1")
        self.assertTrue(has_pending_ingress(InstagramBotSettings.load(), "user-1"))
        self.assertFalse(has_pending_ingress(InstagramBotSettings.load(), "other-user"))
        row.decision = IgWebhookInboxEvent.Decision.BLOCKED
        row.save(update_fields=["decision"])
        self.assertTrue(has_pending_ingress(InstagramBotSettings.load(), "user-1"))
        self.assertFalse(IgClient.objects.filter(igsid="user-1").exists())

    def test_wrong_owner_is_durable_rejection_without_client(self):
        payload = {"object": "instagram", "entry": [{"id": "other-owner", "messaging": [self._event("foreign-user", "foreign-mid")]}]}

        self.assertEqual(self._post(payload).status_code, 200)
        row = IgWebhookInboxEvent.objects.get()
        self.assertEqual(row.decision, "rejected")
        self.assertEqual(row.reason, "owner_mismatch")
        self.assertFalse(IgClient.objects.filter(igsid="foreign-user").exists())

    def test_mixed_batch_commits_valid_and_rejected_receipts_together(self):
        payload = {"object": "instagram", "entry": [
            {"id": "owner-1", "messaging": [self._event("valid-user", "valid-mid")]},
            {"id": "wrong", "messaging": [self._event("foreign-user", "foreign-mid")]},
        ]}

        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(IgWebhookInboxEvent.objects.filter(decision="accepted").count(), 1)
        self.assertEqual(IgWebhookInboxEvent.objects.filter(decision="rejected").count(), 1)
        self.assertFalse(IgClient.objects.filter(igsid="valid-user").exists())

    def test_database_failure_returns_retryable_503_without_receipt(self):
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [self._event("user-2", "mid-2")]}]}
        raw = json.dumps(payload).encode()
        digest = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"IG_APP_SECRET": self.secret}, clear=True), patch(
            "management.models.IgWebhookInboxEvent.objects.get_or_create",
            side_effect=DatabaseError,
        ):
            response = self.client.post(
                "/bot/webhook/", data=raw, content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=f"sha256={digest}",
            )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(IgWebhookInboxEvent.objects.exists())

    def test_malformed_nested_event_is_typed_400(self):
        payload = {"object": "instagram", "entry": [{
            "id": "owner-1", "messaging": [{"sender": {"id": "user"}, "recipient": {"id": "owner-1"}, "message": "bad"}],
        }]}

        self.assertEqual(self._post(payload).status_code, 400)
        self.assertFalse(IgWebhookInboxEvent.objects.exists())

    def test_invalid_participant_is_typed_rejection_without_client_or_receipt(self):
        payload = {"object": "instagram", "entry": [{
            "id": "owner-1", "messaging": [{
                "sender": {"id": ""}, "recipient": {"id": "owner-1"},
                "message": {"mid": "invalid-participant", "text": "Hi"},
            }],
        }]}

        response = self._post(payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"invalid_participant")
        self.assertFalse(IgWebhookInboxEvent.objects.exists())
        self.assertFalse(IgClient.objects.exists())

    def test_no_mid_event_key_is_stable_when_batch_order_changes(self):
        first = {"sender": {"id": "user-a"}, "recipient": {"id": "owner-1"}, "timestamp": 1, "message": {"text": "A"}}
        second = {"sender": {"id": "user-b"}, "recipient": {"id": "owner-1"}, "timestamp": 2, "message": {"text": "B"}}
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [first, second]}]}
        reordered = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [second, first]}]}

        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(self._post(reordered).status_code, 200)
        self.assertEqual(IgWebhookInboxEvent.objects.count(), 2)

    def test_drain_materializes_once_then_marks_the_receipt_processed(self):
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [self._event("drain-user", "drain-mid")]}]}
        self.assertEqual(self._post(payload).status_code, 200)

        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 1)
        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 0)
        self.assertEqual(IgWebhookInboxEvent.objects.get().processed_at is not None, True)
        self.assertEqual(IgClient.objects.filter(igsid="drain-user").count(), 1)

    def test_failed_materialization_keeps_receipt_for_backoff_retry(self):
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [self._event("retry-user", "retry-mid")]}]}
        self.assertEqual(self._post(payload).status_code, 200)
        with patch("management.services.instagram_bot.handle_webhook_payload", side_effect=RuntimeError("db write failed")):
            self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 0)

        row = IgWebhookInboxEvent.objects.get()
        self.assertIsNone(row.processed_at)
        self.assertEqual(row.attempts, 1)
        self.assertIsNotNone(row.next_attempt_at)

    def test_erased_customer_referral_is_not_restored_or_identity_blocked(self):
        customer = IgClient.objects.create(
            igsid="erased-user", privacy_erasure_started_at=timezone.now(),
        )
        event = self._event(customer.igsid, "erased-mid")
        event["referral"] = {
            "ref": "synthetic-ref", "ad_id": "synthetic-ad",
            "ads_context_data": {"ad_title": "Synthetic campaign"},
        }
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [event]}]}
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 1)
        customer.refresh_from_db()
        self.assertFalse(customer.ad_ref)
        self.assertFalse(customer.ad_id)
        self.assertFalse(customer.referral_payload)
        self.assertFalse(InstagramBotMessage.objects.filter(mid="erased-mid").exists())
        receipt = IgWebhookInboxEvent.objects.get()
        self.assertEqual(receipt.decision, "accepted")
        self.assertIsNotNone(receipt.processed_at)

    def test_referral_is_bound_to_the_materialized_revision_source(self):
        from management.models import IgTurnRevisionSource

        event = self._event("referred-user", "referred-mid")
        event["referral"] = {"ref": "synthetic-ref", "ad_id": "synthetic-ad"}
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [event]}]}
        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 1)
        source = IgTurnRevisionSource.objects.get(message__mid="referred-mid")
        self.assertEqual(source.referral.get("ad_id"), "synthetic-ad")

    def test_owner_echo_uses_owner_sender_and_customer_recipient(self):
        echo = {
            "sender": {"id": "owner-1"},
            "recipient": {"id": "echo-customer"},
            "message": {"mid": "echo-mid", "text": "Manager reply", "is_echo": True},
        }
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [echo]}]}

        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 1)
        message = InstagramBotMessage.objects.get(mid="echo-mid")
        self.assertEqual(message.role, "manager")
        self.assertEqual(message.client.igsid, "echo-customer")

    def test_our_registered_echo_is_processed_without_fake_manager_message(self):
        from management.services.ig_outgoing_registry import register_outgoing

        register_outgoing("our-echo-mid", recipient_id="echo-customer")
        echo = {
            "sender": {"id": "owner-1"},
            "recipient": {"id": "echo-customer"},
            "message": {"mid": "our-echo-mid", "text": "Our reply", "is_echo": True},
        }
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [echo]}]}

        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 1)
        self.assertFalse(InstagramBotMessage.objects.filter(mid="our-echo-mid").exists())
        self.assertEqual(IgWebhookInboxEvent.objects.get().decision, "accepted")
        self.assertIsNotNone(IgWebhookInboxEvent.objects.get().processed_at)

    def test_story_only_manager_echo_is_materialized_for_known_client(self):
        IgClient.objects.create(igsid="story-customer")
        echo = {
            "sender": {"id": "owner-1"},
            "recipient": {"id": "story-customer"},
            "message": {
                "mid": "story-echo-mid",
                "is_echo": True,
                "reply_to": {
                    "story": {
                        "id": "story-reference-id",
                        "url": "https://lookaside.fbsbx.com/story-reference",
                    },
                },
            },
        }
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [echo]}]}

        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 1)
        message = InstagramBotMessage.objects.get(mid="story-echo-mid")
        self.assertEqual(message.role, "manager")
        self.assertEqual(message.text, "(відповідь менеджера на сторіс)")
        self.assertIn("https://lookaside.fbsbx.com/story-reference", message.attachments)
        self.assertEqual(message.reply_to_provider_message_id, "story-reference-id")
        receipt = IgWebhookInboxEvent.objects.get()
        self.assertEqual(receipt.decision, "accepted")
        self.assertIsNotNone(receipt.processed_at)

    def test_unknown_story_only_echo_remains_quarantined(self):
        echo = {
            "sender": {"id": "owner-1"},
            "recipient": {"id": "unknown-story-customer"},
            "message": {
                "mid": "unknown-story-echo-mid",
                "is_echo": True,
                "reply_to": {"story": {"id": "story-reference-id"}},
            },
        }
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [echo]}]}

        self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(drain_webhook_inbox(InstagramBotSettings.load(), limit=1), 0)
        receipt = IgWebhookInboxEvent.objects.get()
        self.assertEqual(receipt.decision, "blocked")
        self.assertEqual(receipt.reason, "provider_mid_namespace_unproven")
        self.assertFalse(InstagramBotMessage.objects.filter(mid="unknown-story-echo-mid").exists())

    def test_two_blocked_rows_create_one_outbox_task_and_redacted_status(self):
        namespace = "legacy_page:owner-1"
        for reason in ("provider_mid_namespace_unproven", "provider_mid_namespace_unproven"):
            IgWebhookInboxEvent.objects.create(
                namespace=namespace,
                event_key=f"blocked:{reason}:{IgWebhookInboxEvent.objects.count()}",
                owner_id="owner-1",
                customer_igsid="",
                decision=IgWebhookInboxEvent.Decision.BLOCKED,
                reason=reason,
                payload={},
                payload_digest="a" * 64,
            )
            _queue_identity_reconciliation_notification(namespace)

        status = inbox_status(InstagramBotSettings.load())
        from management.models import IgBotNotification

        self.assertEqual(status["blocked"], 2)
        self.assertNotIn("namespace", status)
        self.assertEqual(len(status["namespace_fingerprint"]), 16)
        self.assertEqual(status["blocked_reasons"], [{"reason": "provider_mid_namespace_unproven", "count": 2}])
        self.assertEqual(IgBotNotification.objects.filter(dedupe_key__startswith="ig-webhook-inbox-identity:").count(), 1)
