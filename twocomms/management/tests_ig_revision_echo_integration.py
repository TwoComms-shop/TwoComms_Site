import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_echo as fixtures
from management.models import IgDeferredEcho, IgPermissionTransitionJob, IgWebhookInboxEvent, InstagramBotMessage
from management.services import instagram_bot as bot
from management.services.ig_revision_echo_integration import reconcile_pending_revision_echoes
from management.services.ig_revision_outbox import finish_effect
from management.services.ig_webhook_inbox import accept_webhook, drain_webhook_inbox


@override_settings(IG_REVISION_EXECUTION_ENABLED=True, SITE_BASE_URL="https://twocomms.test")
class RevisionEchoIntegrationTests(TransactionTestCase):
    _payload = fixtures.RevisionEchoTests._payload
    _plan = fixtures.RevisionEchoTests._plan
    _start = fixtures.RevisionEchoTests._start
    setUp = fixtures.RevisionEchoTests.setUp

    def _poll(self, mid, *, text="", image=False):
        result = {
            "id": mid, "from": {"id": "owner-1"},
            "to": {"data": [{"id": self.client_row.igsid}]},
            "message": text, "created_time": timezone.now().isoformat(),
        }
        if image:
            result["attachments"] = {"data": [{"type": "image", "image_data": {"url": "https://example.test/manager-photo.jpg"}}]}
        return result

    def test_early_echo_is_durable_inbox_materialization_without_manager(self):
        claim = self._start()
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [{
            "sender": {"id": "owner-1"}, "recipient": {"id": self.client_row.igsid},
            "message": {"mid": "early-inbox", "is_echo": True, "text": "Canonical bot reply"},
        }]}]}
        accepted = accept_webhook(json.dumps(payload).encode(), self.settings)
        self.assertEqual(accepted.accepted, 1)
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        inbox = IgWebhookInboxEvent.objects.get()
        self.assertIsNotNone(inbox.processed_at)
        self.assertEqual(inbox.decision, "accepted")
        self.assertEqual(IgDeferredEcho.objects.get().state, "waiting_receipt")
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, http_status=200, provider_message_id="early-inbox")
        reconcile_pending_revision_echoes(self.settings)
        self.assertEqual(IgDeferredEcho.objects.get().state, "own")
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", provider_message_id="early-inbox").count(), 1)
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_real_manager_photo_is_staged_even_during_automation(self):
        with patch.object(bot, "client_automation_busy", return_value=True), patch(
            "management.services.ig_permission_transitions.attempt_permission_transition"
        ) as immediate, patch.object(bot, "_provider_http") as network:
            bot._handle_echo(self.client_row.igsid, "", mid="real-manager-photo", provider_namespace=self.namespace,
                attachments=[{"url": "https://example.test/manager-photo.jpg", "type": "image"}], persistence_only=True)
        immediate.assert_not_called()
        network.assert_not_called()
        self.assertEqual(InstagramBotMessage.objects.get(mid="real-manager-photo").role, "manager")
        self.assertEqual(IgPermissionTransitionJob.objects.filter(kind="manager_takeover").count(), 1)
        self.assertEqual(IgDeferredEcho.objects.get().state, "manager_applied")

    def test_historical_manager_photo_never_creates_current_takeover(self):
        row = self._poll("historical-manager", image=True)
        self.assertTrue(bot._handle_polled_page_side(self.settings, row, historical=True))
        history = InstagramBotMessage.objects.get(mid="historical-manager")
        self.assertEqual((history.role, history.source), ("manager", "poll_history"))
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.client_row.refresh_from_db()
        self.assertFalse(self.client_row.manager_takeover)

    def test_unknown_historical_echo_is_not_manager_context(self):
        claim = self._start()
        finish_effect(claim.effect.pk, claim.token, provider_namespace=self.namespace, transport_outcome="timeout")
        row = self._poll("historical-unknown", text="Unattributed provider message")
        self.assertTrue(bot._handle_polled_page_side(self.settings, row, historical=True))
        self.assertEqual(IgDeferredEcho.objects.get().state, "ambiguous")
        self.assertFalse(InstagramBotMessage.objects.filter(mid="historical-unknown").exists())
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        from management.services.ig_reply_boundary import capture_reply_permission

        permission = capture_reply_permission(self.settings.pk, self.client_row.pk)
        self.assertFalse(permission.allowed)
        self.assertEqual(permission.reason, "echo_attribution_pending")

    def test_early_own_echo_does_not_cancel_second_physical_part(self):
        from management.services.ig_revision_delivery import drain_group
        from management.services.ig_revision_transport import build_provider_part_callback

        self._plan([
            {"group": "substantive_text", "kind": "text", "payload": self._payload("First part")},
            {"group": "substantive_text", "kind": "text", "payload": self._payload("Second part")},
        ])
        callback = build_provider_part_callback(
            self.settings, expected_namespace=self.namespace,
            expected_recipient=self.client_row.igsid, access_token="local-fixture-token",
        )
        calls = []

        def provider(*args, **kwargs):
            calls.append(kwargs["data"])
            mid = f"physical-{len(calls)}"
            if len(calls) == 1:
                bot._handle_echo(self.client_row.igsid, "First part", mid=mid,
                    provider_namespace=self.namespace, persistence_only=True)
                self.assertEqual(IgDeferredEcho.objects.get().state, "waiting_receipt")
            return 200, {"message_id": mid}

        with patch.object(bot, "_provider_http", side_effect=provider):
            result = drain_group(self.revision.pk, self.revision_token, "substantive_text", callback)
        self.assertEqual(len(calls), 2, result.reason)
        self.assertEqual(len(result.sent_parts), 2)
        self.assertEqual(IgDeferredEcho.objects.get().state, "own")
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
