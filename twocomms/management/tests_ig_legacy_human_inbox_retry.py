"""Real inbox retries against retained exact human receipts; no provider I/O."""
import json
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_legacy_human_echo_integration as fixtures
from management.models import IgClient, IgDeferredEcho, IgFollowUpTask, IgWebhookInboxEvent, InstagramBotMessage
from management.services import ig_legacy_human_receipts as adapter
from management.services import instagram_bot as bot
from management.services.ig_revision_echo import observe_revision_echo, reconcile_revision_echoes
from management.services.ig_revision_echo_integration import RevisionEchoDeferred, observe_and_project_echo
from management.services.ig_webhook_inbox import accept_webhook, drain_webhook_inbox


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class LegacyHumanInboxRetryTests(TransactionTestCase):
    setUp = fixtures.LegacyHumanEchoIntegrationTests.setUp
    command_row = fixtures.LegacyHumanEchoIntegrationTests.command_row
    checkpoint_fixture = fixtures.LegacyHumanEchoIntegrationTests.checkpoint_fixture
    assert_no_echo_side_effects = fixtures.LegacyHumanEchoIntegrationTests.assert_no_echo_side_effects

    def accept(self, mid="legacy-echo-4"):
        payload = {"object": "instagram", "entry": [{"id": "owner-1", "messaging": [{
            "sender": {"id": "owner-1"}, "recipient": {"id": self.customer.igsid},
            "timestamp": int(self.now.timestamp() * 1000),
            "message": {"mid": mid, "is_echo": True, "text": ""},
        }]}]}
        self.assertEqual(accept_webhook(json.dumps(payload).encode(), self.settings).accepted, 1)
        return payload

    def fail_receipt_lookup_once(self):
        """Exercise the actual adapter DatabaseError handler once, not a fake flag."""
        original = adapter.find_legacy_human_receipt
        state = {"failed": False}

        def lookup(**kwargs):
            if not state["failed"]:
                state["failed"] = True
                with patch.object(adapter, "_legacy_commands", side_effect=DatabaseError("private DB details")):
                    return original(**kwargs)
            return original(**kwargs)

        return lookup, state

    def observe(self):
        return observe_revision_echo(settings_id=self.settings.pk, namespace=self.namespace,
            recipient=self.customer.igsid, mid="legacy-echo-4", text="")

    def test_actual_inbox_db_error_once_then_fourth_mid_owns_existing_message(self):
        self.checkpoint_fixture()
        original_client = IgClient.objects.values().get(pk=self.customer.pk)
        payload = self.accept()
        lookup, state = self.fail_receipt_lookup_once()
        with patch.object(adapter, "find_legacy_human_receipt", side_effect=lookup), patch.object(
            bot, "_provider_http") as http, patch.object(bot, "send_text") as send, patch.object(
            bot, "_enqueue_memory_source_event") as memory, patch(
            "management.services.ig_permission_transitions.create_permission_transition") as takeover:
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
            inbox = IgWebhookInboxEvent.objects.get()
            self.assertTrue(state["failed"])
            self.assertEqual((inbox.decision, inbox.last_error, inbox.attempts),
                ("accepted", "legacy_human_receipt_unavailable", 1))
            self.assertIsNone(inbox.processed_at)
            self.assertIsNotNone(inbox.next_attempt_at)
            self.assertEqual(inbox.payload, payload)
            self.assertFalse(IgDeferredEcho.objects.exists())
            self.assert_no_echo_side_effects(original_client)

            IgWebhookInboxEvent.objects.filter(pk=inbox.pk).update(next_attempt_at=timezone.now())
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
            inbox.refresh_from_db()
            self.assertEqual((inbox.decision, inbox.last_error, inbox.attempts), ("accepted", "", 2))
            self.assertIsNotNone(inbox.processed_at)
            self.assertIsNone(inbox.next_attempt_at)
            result = self.observe()
            self.assertTrue(result.accepted, result.reason)
            self.assertEqual(result.classification, "own")
            self.assertEqual(result.manager_message_id, self.message.pk)
            self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
        for producer in (http, send, memory, takeover):
            producer.assert_not_called()
        self.assert_no_echo_side_effects(original_client)
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.message.refresh_from_db()
        self.assertEqual(self.message.provider_namespace, "")
        self.assertFalse(self.message.mid)

    def test_lookup_failure_propagates_retry_through_observe_and_project(self):
        self.checkpoint_fixture()
        lookup, _state = self.fail_receipt_lookup_once()
        with patch.object(adapter, "find_legacy_human_receipt", side_effect=lookup):
            with self.assertRaises(RevisionEchoDeferred) as caught:
                observe_and_project_echo(self.settings, namespace=self.namespace,
                    recipient=self.customer.igsid, mid="legacy-echo-4")
            self.assertEqual(caught.exception.reason, "legacy_human_receipt_unavailable")
            self.assertTrue(caught.exception.retryable)
            self.assertTrue(observe_and_project_echo(self.settings, namespace=self.namespace,
                recipient=self.customer.igsid, mid="legacy-echo-4"))
        self.assertFalse(IgDeferredEcho.objects.exists())

    def test_existing_waiting_event_lookup_failure_retries_without_losing_owner(self):
        initial = self.observe()
        self.assertEqual(initial.classification, "waiting_receipt")
        self.checkpoint_fixture()
        original_client = IgClient.objects.values().get(pk=self.customer.pk)
        lookup, _state = self.fail_receipt_lookup_once()
        kwargs = dict(settings_id=self.settings.pk, client_id=self.customer.pk, namespace=self.namespace)
        with patch.object(adapter, "find_legacy_human_receipt", side_effect=lookup):
            failed = reconcile_revision_echoes(**kwargs)[0]
            self.assertFalse(failed.accepted)
            self.assertEqual(failed.reason, "legacy_human_receipt_unavailable")
            self.assertTrue(failed.retryable)
            self.assertEqual(IgDeferredEcho.objects.get(pk=initial.event_id).state, "waiting_receipt")
            recovered = reconcile_revision_echoes(**kwargs)[0]
            self.assertEqual(recovered.classification, "own")
            self.assertEqual(recovered.manager_message_id, self.message.pk)
        self.assert_no_echo_side_effects(original_client)

    def test_inflight_command_is_waiting_materialization_not_exact_own(self):
        original_client = IgClient.objects.values().get(pk=self.customer.pk)
        self.accept()
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 1)
        inbox = IgWebhookInboxEvent.objects.get()
        self.assertIsNotNone(inbox.processed_at)
        self.assertEqual(IgDeferredEcho.objects.get().state, "waiting_receipt")
        self.assert_no_echo_side_effects(original_client)

    def test_invalid_retained_projection_stays_deterministically_blocked(self):
        self.checkpoint_fixture()
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(text="Different retained command")
        original_client = IgClient.objects.values().get(pk=self.customer.pk)
        self.accept()
        self.assertEqual(drain_webhook_inbox(self.settings, limit=1), 0)
        inbox = IgWebhookInboxEvent.objects.get()
        self.assertEqual(inbox.decision, "blocked")
        self.assertEqual(inbox.reason, "provider_mid_namespace_unproven")
        self.assertIsNone(inbox.processed_at)
        self.assertIsNone(inbox.next_attempt_at)
        self.assert_no_echo_side_effects(original_client)

    def test_privacy_and_invalid_namespace_denials_do_not_become_retryable(self):
        self.checkpoint_fixture()
        IgClient.objects.filter(pk=self.customer.pk).update(hidden_at=self.now)
        result = self.observe()
        self.assertFalse(result.accepted)
        self.assertFalse(result.retryable)
        IgClient.objects.filter(pk=self.customer.pk).update(hidden_at=None, privacy_erasure_started_at=self.now)
        result = self.observe()
        self.assertFalse(result.accepted)
        self.assertFalse(result.retryable)
        invalid = observe_revision_echo(settings_id=self.settings.pk, namespace="invalid",
            recipient=self.customer.igsid, mid="legacy-echo-4", text="")
        self.assertFalse(invalid.accepted)
        self.assertFalse(invalid.retryable)
