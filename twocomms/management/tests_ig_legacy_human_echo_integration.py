"""Legacy exact echo adapter, including actual whole-command four-MID send."""
from datetime import timedelta
import hashlib
import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart, human_payload_digest
from management.models import IgClient, IgDeferredEcho, IgPermissionTransitionJob, InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot as bot
from management.services.ig_human_reply import _window_deadline, create_human_reply_command, dispatch_human_reply_command
from management.services.ig_revision_echo import (
    LEGACY_HUMAN_RECEIPT_REASON, _prune_confirmed, observe_revision_echo, revision_echo_blocks,
)
from management.services.ig_revision_echo_integration import (
    RevisionEchoDeferred, _project_manager_event, observe_and_project_echo,
    reconcile_pending_revision_echoes, uses_revision_echo_scope,
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class LegacyHumanEchoIntegrationTests(TransactionTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"}); env.start(); self.addCleanup(env.stop)
        self.now = timezone.now(); self.namespace = "instagram_login:owner-1"
        self.actor = get_user_model().objects.create_superuser(username="legacy-echo-manager", password="x")
        self.settings = InstagramBotSettings.objects.create(pk=1, ig_user_id="owner-1", page_id="owner-1", is_enabled=True)
        self.customer = IgClient.objects.create(igsid="legacy-echo-customer", bot_paused=True,
            manager_takeover=True, reply_permission_epoch=2)
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace=self.namespace, role="user", source="webhook", status="done",
            mid="legacy-echo-source", text="Synthetic customer context", provider_created_at=self.now - timedelta(minutes=2))
        self.command = self.command_row()
        self.message = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace="", role="manager", source="human_reply", text=self.command.text,
            status="processing", send_state="sending", send_idempotency_key=f"human:{self.command.operation_id}")
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(reply_message=self.message)
        self.command.reply_message = self.message

    def command_row(self, *, text="Synthetic saved manual reply", state="provider_started"):
        return HumanReplyCommand.objects.create(client=self.customer, actor=self.actor, context_message=self.source,
            operation_id=uuid.uuid4(), recipient_igsid=self.customer.igsid, provider_namespace=self.namespace,
            text=text, draft_hash=hashlib.sha256(text.encode()).hexdigest(), operation_context={},
            permission_epoch=self.customer.reply_permission_epoch, window_deadline=_window_deadline(self.source),
            state=state, provider_started_at=self.now if state == "provider_started" else None)

    def checkpoint_fixture(self, mids=("legacy-echo-1", "legacy-echo-2", "legacy-echo-3", "legacy-echo-4")):
        # Retained historical records for replay tests. Actual physical receipts
        # are tested separately below with the real sender and mocked HTTP.
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="sent", provider_message_ids=list(mids),
            provider_started_at=self.now, terminal_at=self.now, failure_code="")
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(status="done", send_state="sent",
            provider_message_id=mids[0], delivery_provider_message_ids=list(mids), text=self.command.text)

    def observe(self, mid="legacy-echo-4", **overrides):
        return observe_revision_echo(**{**dict(settings_id=self.settings.pk, namespace=self.namespace,
            recipient=self.customer.igsid, mid=mid, text="", now=self.now), **overrides})

    def project(self, mid="legacy-echo-4", **overrides):
        return observe_and_project_echo(self.settings, **{**dict(namespace=self.namespace,
            recipient=self.customer.igsid, mid=mid, text=""), **overrides})

    def assert_no_echo_side_effects(self, original):
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertFalse(InstagramBotMessage.objects.filter(source__in=["echo", "poll_history"]).exists())
        self.assertFalse(HumanReplyPart.objects.exists()); self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.assertEqual(original, IgClient.objects.values().get(pk=self.customer.pk))

    def test_four_legacy_mids_reuse_original_manager_transcript_with_flag_off_and_empty_echo(self):
        self.checkpoint_fixture(); original = IgClient.objects.values().get(pk=self.customer.pk)
        with patch("management.services.ig_outgoing_registry.is_our_outgoing", return_value=True) as shortcut, patch.object(
            bot, "_enqueue_memory_source_event") as memory, patch.object(bot, "_provider_http") as http, patch(
            "management.services.ig_permission_transitions.create_permission_transition") as takeover:
            for index in range(1, 5):
                mid = f"legacy-echo-{index}"
                self.assertTrue(uses_revision_echo_scope(self.namespace, self.customer.igsid, mid=mid))
                result = self.observe(mid)
                self.assertTrue(result.accepted, result.reason); self.assertEqual(result.classification, "own")
                self.assertEqual(result.manager_message_id, self.message.pk); self.assertEqual(result.reason, LEGACY_HUMAN_RECEIPT_REASON)
                self.assertTrue(self.project(mid)); self.assertTrue(self.project(mid, historical=True, text="Different poll chunk text"))
            for producer in (shortcut, memory, http, takeover): producer.assert_not_called()
        self.assert_no_echo_side_effects(original)
        self.message.refresh_from_db(); self.assertEqual(self.message.provider_namespace, "")
        self.assertEqual(self.message.delivery_planned_chunk_count, 0)

    def test_early_echo_waits_then_late_exact_receipt_owns_and_replay_deduplicates(self):
        original = IgClient.objects.values().get(pk=self.customer.pk)
        first = self.observe(); self.assertEqual(first.classification, "waiting_receipt")
        self.assertTrue(revision_echo_blocks(self.customer.pk, self.namespace))
        self.assertTrue(self.project()); self.assert_no_echo_side_effects(original)
        self.checkpoint_fixture()
        with patch.object(bot, "_enqueue_memory_source_event") as memory, patch.object(bot, "_provider_http") as http:
            self.assertEqual(reconcile_pending_revision_echoes(self.settings), 1)
            replay = self.observe(); self.assertTrue(replay.replayed); self.assertEqual(replay.classification, "own")
            self.assertTrue(self.project(historical=True))
        memory.assert_not_called(); http.assert_not_called()
        event = IgDeferredEcho.objects.get(pk=first.event_id)
        self.assertEqual(event.manager_message_id, self.message.pk); self.assertFalse(event.matched_effect_id)
        self.assertFalse(event.matched_human_part_id); self.assertFalse(event.permission_transition_id)
        self.assertFalse(revision_echo_blocks(self.customer.pk, self.namespace)); self.assert_no_echo_side_effects(original)

    def test_still_started_or_unknown_never_applies_a_manager_transition(self):
        original = IgClient.objects.values().get(pk=self.customer.pk)
        self.assertTrue(self.project())
        with patch("management.services.ig_revision_echo._technical_case") as alert:
            HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="unknown")
            self.assertEqual(reconcile_pending_revision_echoes(self.settings), 0)
            event = IgDeferredEcho.objects.get(); self.assertEqual(event.state, "ambiguous")
            self.assertEqual(event.reason, "legacy_human_provider_result_unknown"); alert.assert_called_once()
        self.assertTrue(revision_echo_blocks(self.customer.pk, self.namespace)); self.assert_no_echo_side_effects(original)

    def test_late_known_receipt_rechecks_manager_pending_without_second_projection(self):
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="definite_failed")
        pending = self.observe(text="Physical echo chunk"); self.assertEqual(pending.classification, "manager_pending")
        self.checkpoint_fixture(); original = IgClient.objects.values().get(pk=self.customer.pk)
        self.assertFalse(_project_manager_event(pending.event_id))
        event = IgDeferredEcho.objects.get(); self.assertEqual(event.state, "own")
        self.assertEqual(event.manager_message_id, self.message.pk); self.assert_no_echo_side_effects(original)

    def test_legacy_owned_terminal_replay_and_prune_revalidate_exact_graph(self):
        initial = self.observe(); self.checkpoint_fixture(); self.assertEqual(reconcile_pending_revision_echoes(self.settings), 1)
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(text="Wrong retained projection")
        result = self.observe(); self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "legacy_human_projection_mismatch")
        self.assertFalse(_prune_confirmed(IgDeferredEcho.objects.filter(pk=initial.event_id), self.customer))
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(text=self.command.text)
        self.assertTrue(_prune_confirmed(IgDeferredEcho.objects.filter(pk=initial.event_id), self.customer))
        self.assertFalse(IgDeferredEcho.objects.exists()); self.assertTrue(self.project())
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)

    def test_privacy_hidden_foreign_owner_and_wrong_namespace_never_project(self):
        self.checkpoint_fixture()
        for field in ("hidden_at", "privacy_erasure_started_at"):
            IgClient.objects.filter(pk=self.customer.pk).update(**{field: self.now})
            result = self.observe(); self.assertFalse(result.accepted)
            IgClient.objects.filter(pk=self.customer.pk).update(**{field: None})
        result = self.observe(namespace="instagram_login:other-owner", text="same text")
        self.assertFalse(result.accepted); self.assertEqual(result.reason, "echo_namespace_mismatch")
        foreign = IgClient.objects.create(igsid="legacy-foreign-echo")
        with patch("management.services.ig_revision_echo._technical_case"):
            result = self.observe(recipient=foreign.igsid)
        self.assertEqual(result.classification, "ambiguous"); self.assertEqual(result.reason, "legacy_human_receipt_foreign")
        self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)

    def test_cross_lane_exact_mid_is_ambiguous_even_when_human_part_receipt_exists(self):
        self.checkpoint_fixture()
        other = self.command_row(text="Other saved manual reply", state="sent")
        payload = {"recipient": {"id": self.customer.igsid}, "message": {"text": other.text}}
        HumanReplyPart.objects.create(command=other, client=self.customer, operation_id_snapshot=other.operation_id,
            actor_id_snapshot=self.actor.pk, context_message_id_snapshot=self.source.pk, permission_epoch=2,
            recipient_igsid=self.customer.igsid, provider_namespace=self.namespace, ordinal=0, part_count=1,
            plan_digest="a" * 64, payload=payload, payload_digest=human_payload_digest(payload),
            state="sent", provider_message_id="legacy-echo-4", provider_started_at=self.now)
        with patch("management.services.ig_revision_echo._technical_case"):
            result = self.observe()
        self.assertEqual(result.classification, "ambiguous"); self.assertEqual(result.reason, "legacy_human_cross_lane_conflict")
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertFalse(IgPermissionTransitionJob.objects.exists())

    def test_final_manager_ack_rejects_receipt_arriving_after_projection_precheck_and_rolls_back_staging(self):
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="definite_failed")
        initial = self.observe(text="Early physical echo chunk")
        original = bot._stage_permission_message
        def late_receipt(**kwargs):
            value = original(**kwargs)
            self.checkpoint_fixture()
            return value
        with patch.object(bot, "_stage_permission_message", side_effect=late_receipt), patch.object(bot, "_enqueue_memory_source_event") as memory:
            with self.assertRaises(RevisionEchoDeferred) as caught:
                _project_manager_event(initial.event_id)
        self.assertEqual(caught.exception.reason, "echo_legacy_manager_projection_changed")
        memory.assert_not_called(); self.assertFalse(IgPermissionTransitionJob.objects.exists())
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertFalse(InstagramBotMessage.objects.filter(mid="legacy-echo-4").exists())

    def test_actual_legacy_dispatch_four_physical_receipts_then_echo_and_poll_do_not_resend(self):
        # Preserve an actual legacy command with no plan/parts; do not call the
        # new-command creator, which correctly uses the new HumanPart lane.
        self.message.delete(); self.command.delete()
        text = ("A " * 1600).strip()
        command = self.command_row(text=text, state="pending")
        bodies = []
        def physical_http(settings, url, *, token, data):
            body = json.loads(data); bodies.append(body)
            self.assertEqual(body["recipient"], {"id": self.customer.igsid})
            self.assertEqual(HumanReplyCommand.objects.get(pk=command.pk).state, "provider_started")
            return 200, json.dumps({"message_id": f"actual-legacy-mid-{len(bodies)}"})
        with patch.object(bot, "get_page_token", return_value="synthetic-token"), patch.object(
            bot, "_provider_http", side_effect=physical_http) as http, patch.object(bot, "_enqueue_memory_source_event"), patch.object(
            bot, "_mark_bot_sent") as marker, patch.object(bot, "_register_outgoing_message") as registry:
            sent = dispatch_human_reply_command(command.pk, now=self.now)
        self.assertEqual(sent.state, "sent"); self.assertEqual(http.call_count, 4)
        self.assertEqual(sent.provider_message_ids, [f"actual-legacy-mid-{index}" for index in range(1, 5)])
        self.assertFalse(HumanReplyPart.objects.exists()); marker.assert_not_called(); registry.assert_not_called()
        self.message = InstagramBotMessage.objects.get(pk=sent.reply_message_id)
        original = IgClient.objects.values().get(pk=self.customer.pk)
        with patch.object(bot, "_provider_http") as network, patch.object(bot, "_enqueue_memory_source_event") as memory:
            for index, body in enumerate(bodies, 1):
                mid = f"actual-legacy-mid-{index}"
                self.assertTrue(self.project(mid, text=body["message"]["text"]))
                self.assertTrue(self.project(mid, text=body["message"]["text"], historical=True))
                self.assertTrue(self.project(mid))
        network.assert_not_called(); memory.assert_not_called(); self.assert_no_echo_side_effects(original)

    def test_new_planned_human_receipt_preserves_precedence_and_original_part_projection(self):
        self.checkpoint_fixture()
        command = create_human_reply_command(self.customer.pk, actor=self.actor, text="New planned manual reply",
            context_message_id=self.source.pk, now=self.now).command
        with patch.object(bot, "get_page_token", return_value="synthetic-token"), patch.object(
            bot, "_provider_http", return_value=(200, json.dumps({"message_id": "new-human-part-mid"}))), patch.object(bot, "_enqueue_memory_source_event"):
            self.assertEqual(dispatch_human_reply_command(command.pk, now=self.now).state, "sent")
        part = HumanReplyPart.objects.get(command=command)
        self.assertTrue(self.project("new-human-part-mid"))
        event = IgDeferredEcho.objects.get(provider_message_id="new-human-part-mid")
        self.assertEqual(event.state, "human_applied"); self.assertEqual(event.matched_human_part_id, part.pk)
        self.assertEqual(event.manager_message_id, command.__class__.objects.get(pk=command.pk).reply_message_id)
        self.assertFalse(event.permission_transition_id)
