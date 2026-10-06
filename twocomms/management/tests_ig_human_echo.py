"""Exact human echo attribution; no provider I/O except explicitly mocked HTTP."""
from datetime import timedelta
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from management.ig_human_reply_models import HumanReplyPart
from management.models import IgClient, IgDeferredEcho, IgPermissionTransitionJob, InstagramBotMessage
from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command
from management.services.ig_human_reply_delivery import claim_next_human_part, record_human_part_started, settle_human_part
from management.services.ig_human_reply_transport import checkpoint_human_part_receipt
from management.services.ig_revision_echo import acknowledge_human_echo, observe_revision_echo, revision_echo_blocks
from management.services.ig_revision_echo_integration import (
    RevisionEchoDeferred, observe_and_project_echo, reconcile_human_part_echoes,
    reconcile_pending_revision_echoes, uses_revision_echo_scope,
)
from management.tests_ig_human_reply_delivery import _HumanStoreFixture


@override_settings(IG_REVISION_EXECUTION_ENABLED=False)
class HumanEchoStoreTests(_HumanStoreFixture):
    def observe(self, mid="human-echo-mid", **kwargs):
        return observe_revision_echo(settings_id=self.settings_row.pk, namespace=self.namespace,
            recipient=self.customer.igsid, mid=mid, text=kwargs.pop("text", "Вітаю!"), **kwargs)

    def project(self, mid="human-echo-mid", **kwargs):
        return observe_and_project_echo(self.settings_row, namespace=self.namespace,
            recipient=self.customer.igsid, mid=mid, text=kwargs.pop("text", "Вітаю!"), **kwargs)

    def test_early_echo_waits_for_exact_human_checkpoint_without_extra_takeover(self):
        command, _, claim = self.started()
        observed = self.observe()
        event = IgDeferredEcho.objects.get(pk=observed.event_id)
        self.assertEqual((event.state, event.competing_human_part_ids), ("waiting_receipt", [claim.part.pk]))
        self.assertTrue(revision_echo_blocks(self.customer.pk, self.namespace))
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        with patch("management.services.instagram_bot._enqueue_memory_source_event") as memory, self.captureOnCommitCallbacks(execute=True):
            checkpoint_human_part_receipt(claim.part.pk, claim.token,
                provider_namespace=self.namespace, provider_message_id="human-echo-mid")
        event.refresh_from_db()
        self.assertEqual((event.state, event.matched_human_part_id), ("human_applied", claim.part.pk))
        self.assertFalse(event.permission_transition_id)
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.assertEqual(memory.call_count, 1)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.reply_permission_epoch, command.permission_epoch)
        self.assertFalse(revision_echo_blocks(self.customer.pk, self.namespace))

    def test_cached_historical_human_mid_never_becomes_model_history(self):
        _, _, claim = self.started()
        self.confirm(claim, "cached-human-mid")
        with patch("management.services.ig_outgoing_registry.is_our_outgoing", return_value=True) as registry:
            self.assertTrue(self.project("cached-human-mid", historical=True))
            self.assertTrue(self.project("cached-human-mid", historical=True))
        registry.assert_not_called()
        messages = list(InstagramBotMessage.objects.filter(role="manager"))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].source, "human_reply")
        self.assertFalse(InstagramBotMessage.objects.filter(role="model").exists())
        self.assertEqual(IgDeferredEcho.objects.get().state, "human_applied")

    def test_known_exact_human_mid_needs_no_echo_text_to_preserve_author(self):
        _, _, claim = self.started()
        self.confirm(claim, "empty-human-echo")
        self.assertTrue(self.project("empty-human-echo", text="", historical=True))
        self.assertEqual(InstagramBotMessage.objects.get(role="manager").text, claim.part.text)
        self.assertEqual(IgDeferredEcho.objects.get().payload["text"], "")
        self.assertFalse(InstagramBotMessage.objects.filter(role="model").exists())

    def test_unknown_matching_text_and_time_never_supply_a_receipt(self):
        _, _, claim = self.started()
        settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace, transport_outcome="timeout")
        started_at = HumanReplyPart.objects.get(pk=claim.part.pk).provider_started_at
        self.assertTrue(self.project(text=claim.part.text, received_at=started_at))
        reconcile_pending_revision_echoes(self.settings_row)
        part = HumanReplyPart.objects.get(pk=claim.part.pk)
        self.assertEqual(part.state, "unknown")
        self.assertIsNone(part.provider_message_id)
        self.assertEqual(IgDeferredEcho.objects.get().state, "ambiguous")
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_exact_late_receipt_preserves_original_operation_after_source_reset(self):
        from management.ig_bot_models import IgFunnelResetAudit
        _, _, claim = self.started()
        self.observe()
        settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace, transport_outcome="timeout")
        reconcile_human_part_echoes(claim.part.pk)
        self.assertEqual(IgDeferredEcho.objects.get().state, "ambiguous")
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk, reason="new context")
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="different source")
        self.actor.delete()
        with self.captureOnCommitCallbacks(execute=True):
            checkpoint_human_part_receipt(claim.part.pk, claim.token,
                provider_namespace=self.namespace, provider_message_id="human-echo-mid")
        message = InstagramBotMessage.objects.get(role="manager")
        self.assertEqual(message.send_idempotency_key, f"human:{claim.part.operation_id_snapshot}:part:{claim.part.ordinal}")
        self.assertEqual((message.text, message.source), (claim.part.text, "human_reply"))
        self.assertEqual(IgDeferredEcho.objects.get().state, "human_applied")
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_foreign_recipient_exact_human_identity_is_finite_even_with_cached_mid(self):
        _, _, claim = self.started()
        self.confirm(claim, "foreign-human-mid")
        other = IgClient.objects.create(igsid="foreign-human-echo")
        self.assertTrue(uses_revision_echo_scope(self.namespace, other.igsid, mid="foreign-human-mid"))
        with patch("management.services.ig_outgoing_registry.is_our_outgoing", return_value=True) as registry:
            with self.assertRaises(RevisionEchoDeferred) as rejected:
                observe_and_project_echo(self.settings_row, namespace=self.namespace, recipient=other.igsid,
                    mid="foreign-human-mid", text="same words", historical=True)
        self.assertEqual(rejected.exception.reason, "echo_receipt_identity_mismatch")
        self.assertFalse(rejected.exception.retryable)
        registry.assert_not_called()
        self.assertFalse(IgDeferredEcho.objects.exists())

    def test_namespace_mismatch_does_not_bind_another_account(self):
        _, _, claim = self.started()
        self.confirm(claim)
        result = observe_revision_echo(settings_id=self.settings_row.pk, namespace="instagram_login:other",
            recipient=self.customer.igsid, mid="human-mid-1", text=claim.part.text)
        self.assertEqual((result.accepted, result.reason), (False, "echo_namespace_mismatch"))
        self.assertFalse(IgDeferredEcho.objects.exists())

    def test_missing_candidate_keeps_evidence_ambiguous_instead_of_manager_projection(self):
        _, _, claim = self.started()
        self.observe()
        part_id = claim.part.pk
        claim.part.delete()
        self.assertEqual(reconcile_human_part_echoes(part_id), 0)
        reconcile_pending_revision_echoes(self.settings_row)
        event = IgDeferredEcho.objects.get()
        self.assertEqual((event.state, event.reason), ("ambiguous", "echo_effect_evidence_missing"))
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_definite_own_failure_allows_real_external_manager_echo(self):
        _, _, claim = self.started()
        self.observe("external-manager-mid", text="different Inbox reply")
        settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace, http_status=403)
        reconcile_pending_revision_echoes(self.settings_row)
        self.assertEqual(IgDeferredEcho.objects.get().state, "manager_applied")
        self.assertEqual(InstagramBotMessage.objects.get(role="manager").source, "echo")
        self.assertEqual(IgPermissionTransitionJob.objects.count(), 1)

    def test_human_ack_requires_full_existing_transcript_proof(self):
        _, _, claim = self.started()
        self.confirm(claim, "proof-mid")
        self.assertTrue(self.project("proof-mid"))
        event = IgDeferredEcho.objects.get()
        InstagramBotMessage.objects.filter(pk=event.manager_message_id).update(text="wrong text")
        rejected = acknowledge_human_echo(event_id=event.pk, settings_id=self.settings_row.pk,
            human_part_id=claim.part.pk, manager_message_id=event.manager_message_id)
        self.assertEqual((rejected.accepted, rejected.reason), (False, "echo_human_projection_mismatch"))
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_projection_failure_remains_pending_and_periodic_retries_without_http(self):
        from management.services.ig_human_reply_transport import HumanProjectionResult
        _, _, claim = self.started()
        self.confirm(claim, "projection-mid")
        with patch("management.services.ig_human_reply_transport.project_human_part_receipt",
                return_value=HumanProjectionResult(reason="human_transcript_temporarily_unavailable")):
            with self.assertRaises(RevisionEchoDeferred):
                self.project("projection-mid")
        self.assertEqual(IgDeferredEcho.objects.get().state, "human_pending")
        self.assertEqual(reconcile_pending_revision_echoes(self.settings_row), 1)
        self.assertEqual(IgDeferredEcho.objects.get().state, "human_applied")
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)

    def test_erasure_blocks_echo_and_late_hook_without_recreating_history(self):
        _, _, claim = self.started()
        self.observe()
        self.confirm(claim, "human-echo-mid")
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=self.now)
        self.assertTrue(self.project())
        self.assertEqual(reconcile_human_part_echoes(claim.part.pk), 0)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.customer.delete()
        self.assertEqual(reconcile_human_part_echoes(claim.part.pk), 0)
        self.assertFalse(IgDeferredEcho.objects.exists())


@override_settings(IG_REVISION_EXECUTION_ENABLED=False)
class HumanEchoPhysicalTests(TestCase):
    def setUp(self):
        from management.models import InstagramBotSettings
        self.actor = get_user_model().objects.create_superuser(username="human-echo-physical", password="x")
        self.settings_row = InstagramBotSettings.objects.create(pk=1, ig_user_id="owner-1", page_id="owner-1", is_enabled=True)
        self.customer = IgClient.objects.create(igsid="human-echo-physical")
        self.namespace = "instagram_login:owner-1"
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", text="question", provider_namespace=self.namespace,
            provider_created_at=timezone.now() - timedelta(minutes=2), mid="physical-echo-source", status="done")
        env = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)

    def test_echo_during_real_http_before_checkpoint_waits_and_projects_once(self):
        from management.services import instagram_bot as bot
        command = create_human_reply_command(self.customer.pk, actor=self.actor, text="Actual reply",
            context_message_id=self.source.pk).command

        def http(settings, url, *, token, data):
            self.assertEqual(json.loads(data)["message"]["text"], command.text)
            self.assertTrue(bot._handle_echo(self.customer.igsid, command.text,
                mid="during-http-mid", provider_namespace=self.namespace))
            self.assertEqual(IgDeferredEcho.objects.get().state, "waiting_receipt")
            self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
            return 200, '{"message_id":"during-http-mid"}'

        with patch.object(bot, "get_page_token", return_value="local-test-token"), patch.object(bot, "_provider_http", side_effect=http) as network, patch.object(bot, "_enqueue_memory_source_event") as memory, self.captureOnCommitCallbacks(execute=True):
            result = dispatch_human_reply_command(command.pk)
            self.assertEqual(result.state, "sent")
        self.assertEqual(network.call_count, 1)
        self.assertEqual(memory.call_count, 1)
        self.assertEqual(IgDeferredEcho.objects.get().state, "human_applied")
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.reply_permission_epoch, command.permission_epoch)
        with patch.object(bot, "_provider_http") as forbidden:
            self.assertTrue(bot._handle_echo(self.customer.igsid, command.text,
                mid="during-http-mid", provider_namespace=self.namespace))
            self.assertTrue(bot._handle_polled_page_side(self.settings_row, {
                "id": "during-http-mid", "from": {"id": "owner-1"},
                "to": {"data": [{"id": self.customer.igsid}]}, "message": command.text,
                "created_time": timezone.now().isoformat()}, historical=True))
        forbidden.assert_not_called()
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertFalse(InstagramBotMessage.objects.filter(role="model").exists())


class HumanBotReceiptConflictTests(TestCase):
    def setUp(self):
        from management.tests_ig_revision_echo import RevisionEchoTests
        RevisionEchoTests.setUp(self)
        self.actor = get_user_model().objects.create_superuser(username="human-bot-conflict", password="x")

    def start_bot(self):
        from management.tests_ig_revision_echo import RevisionEchoTests
        from management.tests_ig_revision_delivery import RevisionDeliveryTests
        self._payload = RevisionDeliveryTests._payload.__get__(self)
        self._plan = RevisionDeliveryTests._plan.__get__(self)
        return RevisionEchoTests._start(self)

    def start_human(self):
        command = create_human_reply_command(self.client_row.pk, actor=self.actor, text="Human reply",
            context_message_id=self.source.pk).command
        claim = claim_next_human_part(command.pk)
        self.assertTrue(claim.ready, claim.reason)
        self.assertTrue(record_human_part_started(claim.part.pk, claim.token).ready)
        return claim

    def test_cross_bot_human_exact_receipt_conflict_never_selects_an_author(self):
        from management.services.ig_revision_outbox import finish_effect
        bot = self.start_bot()
        finish_effect(bot.effect.pk, bot.token, provider_namespace=self.namespace, http_status=200, provider_message_id="cross-owned-mid")
        human = self.start_human()
        settle_human_part(human.part.pk, human.token, provider_namespace=self.namespace, http_status=200, provider_message_id="cross-owned-mid")
        result = observe_revision_echo(settings_id=self.settings.pk, namespace=self.namespace,
            recipient=self.client_row.igsid, mid="cross-owned-mid", text="any text")
        self.assertEqual((result.accepted, result.reason), (False, "echo_receipt_owner_conflict"))
        self.assertFalse(IgDeferredEcho.objects.exists())
        self.assertFalse(InstagramBotMessage.objects.filter(role__in=("model", "manager")).exists())

    def test_shared_candidate_cap_and_exact_receipt_resolution(self):
        self.start_bot()
        human = self.start_human()
        with patch("management.services.ig_revision_echo.MAX_COMPETING", 1):
            result = observe_revision_echo(settings_id=self.settings.pk, namespace=self.namespace,
                recipient=self.client_row.igsid, mid="overflow-human-mid", text="Human reply")
        self.assertEqual((result.classification, result.reason), ("ambiguous", "echo_competing_limit"))
        event = IgDeferredEcho.objects.get()
        self.assertTrue(event.candidate_overflow)
        self.assertEqual(len(event.competing_effect_ids) + len(event.competing_human_part_ids), 1)
        settle_human_part(human.part.pk, human.token, provider_namespace=self.namespace, http_status=200, provider_message_id="overflow-human-mid")
        reconcile_human_part_echoes(human.part.pk)
        event.refresh_from_db()
        self.assertEqual((event.state, event.matched_human_part_id), ("human_applied", human.part.pk))
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
