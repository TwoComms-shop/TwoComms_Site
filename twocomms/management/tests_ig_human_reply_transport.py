"""Real human consumer + real send_text, with only physical HTTP mocked."""
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart
from management.models import AdminAuditLog, IgClient, IgFollowUpTask, InstagramBotMessage, InstagramBotSettings
from management.services.ig_human_reply import HumanReplyRejected, _window_deadline, create_human_reply_command, dispatch_human_reply_command
from management.services.ig_human_reply_delivery import PLAN_KEY, HumanDeliveryResult, reap_human_part
from management.services.ig_human_reply_transport import (
    HumanProjectionResult, checkpoint_human_part_receipt, project_human_part_receipt,
)


class _ProcessCrash(BaseException):
    pass


class HumanReplyTransportTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.actor = get_user_model().objects.create_superuser(username="human-transport", email="human-transport@example.test", password="x")
        self.settings_row = InstagramBotSettings.objects.create(pk=1, ig_user_id="owner-1", page_id="owner-1", is_enabled=True)
        self.customer = IgClient.objects.create(igsid="human-physical-customer")
        self.namespace = "instagram_login:owner-1"
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", provider_namespace=self.namespace, text="Підкажіть розмір",
            mid="human-transport-source", provider_created_at=self.now - timedelta(minutes=2), status="done")
        env = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        self.token = patch("management.services.instagram_bot.get_page_token", return_value="local-test-token").start()
        self.addCleanup(patch.stopall)
        self.memory = patch("management.services.instagram_bot._enqueue_memory_source_event", return_value=True).start()
        self.bot_marker = patch("management.services.instagram_bot._mark_bot_sent").start()
        self.bot_registry = patch("management.services.instagram_bot._register_outgoing_message").start()
        patch("management.services.instagram_bot._activate_link_send_circuit").start()
        self.http = patch("management.services.instagram_bot._provider_http", side_effect=self.physical_http).start()
        self.bodies = []
        self.responses = []
        self.physical_hook = None

    def physical_http(self, settings, url, *, token, data):
        payload = json.loads(data)
        self.bodies.append(payload)
        active = HumanReplyPart.objects.get(state="provider_started")
        self.assertEqual(active.payload, payload)
        self.assertIsNotNone(active.provider_started_at)
        self.assertTrue(active.claim_token)
        self.assertTrue(all(getattr(block, "_from_testcase", False) for block in connection.atomic_blocks),
            "provider request held an uncommitted application transaction")
        if self.physical_hook:
            self.physical_hook(active)
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return 200, json.dumps({"message_id": "physical-mid-" + str(len(self.bodies))})

    def command(self, text="Вітаю!", **kwargs):
        return create_human_reply_command(self.customer.pk, actor=self.actor, text=text,
            context_message_id=self.source.pk, now=self.now, **kwargs).command

    def dispatch(self, command):
        return dispatch_human_reply_command(command.pk, now=self.now)

    def tearDown(self):
        self.bot_marker.assert_not_called()
        self.bot_registry.assert_not_called()
        super().tearDown()

    def test_new_command_captures_plan_inside_takeover_and_duplicate_click_reuses_it(self):
        operation_id = "22222222-2222-2222-2222-222222222222"
        command = self.command("A" * 1500, operation_id=operation_id)
        duplicate = self.command("A" * 1500, operation_id=operation_id)
        self.assertEqual(command.pk, duplicate.pk)
        self.assertIn(PLAN_KEY, command.operation_context)
        self.assertEqual(HumanReplyPart.objects.count(), 2)
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").count(), 1)
        self.http.assert_not_called()

    def test_plan_failure_rolls_back_takeover_command_and_audit(self):
        with patch("management.services.ig_human_reply_delivery.plan_human_command", return_value=HumanDeliveryResult(reason="context_changed")):
            with self.assertRaises(HumanReplyRejected) as caught:
                self.command()
        self.assertEqual(caught.exception.code, "context_changed")
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.manager_takeover)
        self.assertFalse(self.customer.bot_paused)
        self.assertEqual(self.customer.reply_permission_epoch, 0)
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.assertFalse(HumanReplyPart.objects.exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").exists())

    def test_all_parts_checkpoint_exact_receipt_before_next_physical_post(self):
        command = self.command("A" * 2500)
        seen = []

        def inspect(active):
            previous = list(HumanReplyPart.objects.filter(command=command, ordinal__lt=active.ordinal).order_by("ordinal"))
            self.assertTrue(all(part.state == "sent" and part.provider_message_id for part in previous))
            self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), active.ordinal)
            seen.append(active.pk)

        self.physical_hook = inspect
        with self.captureOnCommitCallbacks(execute=True):
            result = self.dispatch(command)
        self.assertEqual(result.state, "sent")
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(len(seen), 3)
        self.assertEqual(result.provider_message_ids, ["physical-mid-1", "physical-mid-2", "physical-mid-3"])
        messages = list(InstagramBotMessage.objects.filter(role="manager").order_by("pk"))
        self.assertEqual([message.text for message in messages], [body["message"]["text"] for body in self.bodies])
        self.assertTrue(all(message.provider_namespace == self.namespace and message.send_state == "sent" for message in messages))
        self.assertEqual(self.memory.call_count, 3)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.bot_paused)

    def test_repeated_dispatch_and_receipt_projection_enqueue_memory_once(self):
        command = self.command()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.dispatch(command).state, "sent")
            self.assertEqual(self.dispatch(command).state, "sent")
            part = HumanReplyPart.objects.get(command=command)
            first = project_human_part_receipt(part.pk)
            second = project_human_part_receipt(part.pk)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.memory.call_count, 1)
        self.assertFalse(first.created)
        self.assertEqual(first.message.pk, second.message.pk)
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)

    def test_partial_unknown_preserves_first_receipt_cancels_tail_and_never_reposts(self):
        command = self.command("A" * 2500)
        self.responses = [(200, '{"message_id":"first-exact"}'), TimeoutError("fake timeout")]
        with self.captureOnCommitCallbacks(execute=True):
            result = self.dispatch(command)
            replay = self.dispatch(command)
        self.assertEqual((result.state, replay.state), ("unknown", "unknown"))
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(result.provider_message_ids, ["first-exact"])
        parts = list(HumanReplyPart.objects.filter(command=command).order_by("ordinal"))
        self.assertEqual([part.state for part in parts], ["sent", "unknown", "cancelled"])
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertEqual(self.memory.call_count, 1)
        task = IgFollowUpTask.objects.get(event_key=f"human-reply-unknown:{command.pk}")
        self.assertEqual([item["part_id"] for item in task.event_payload["parts"]], [part.pk for part in parts])
        self.assertEqual(task.event_payload["parts"][0]["provider_message_id"], "first-exact")
        self.assertFalse(task.manager_context["automatic_http_retry"])

    def test_http_200_without_mid_is_unknown_and_not_a_transcript_source(self):
        command = self.command()
        self.responses = [(200, '{}')]
        self.assertEqual(self.dispatch(command).state, "unknown")
        self.assertEqual(self.dispatch(command).state, "unknown")
        self.assertEqual(self.http.call_count, 1)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.memory.assert_not_called()

    def test_http_500_is_unknown_without_implicit_second_post(self):
        command = self.command()
        self.responses = [(500, '{"error":{"code":1}}')]
        self.assertEqual(self.dispatch(command).state, "unknown")
        self.dispatch(command)
        self.assertEqual(self.http.call_count, 1)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_definite_403_failure_cancels_tail_without_second_post(self):
        command = self.command("A" * 1500)
        self.responses = [(403, '{"error":{"code":10}}')]
        self.assertEqual(self.dispatch(command).state, "definite_failed")
        self.dispatch(command)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(list(HumanReplyPart.objects.filter(command=command).order_by("ordinal").values_list("state", flat=True)),
            ["definite_failed", "cancelled"])

    def test_missing_token_is_definite_preflight_failure_with_no_start_or_http(self):
        command = self.command()
        self.token.return_value = ""
        self.assertEqual(self.dispatch(command).state, "definite_failed")
        self.dispatch(command)
        self.http.assert_not_called()
        part = HumanReplyPart.objects.get(command=command)
        self.assertIsNone(part.provider_started_at)
        self.assertIsNone(part.provider_message_id)

    def test_checkpoint_failure_rolls_back_receipt_projection_and_stops_tail(self):
        command = self.command("A" * 1500)
        with patch("management.services.ig_human_reply_transport.project_human_part_receipt",
                   return_value=HumanProjectionResult(reason="human_transcript_conflict")):
            with self.captureOnCommitCallbacks(execute=True):
                result = self.dispatch(command)
        self.assertEqual(result.state, "unknown")
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(list(HumanReplyPart.objects.filter(command=command).order_by("ordinal").values_list("state", flat=True)),
            ["unknown", "cancelled"])
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.memory.assert_not_called()
        self.dispatch(command)
        self.assertEqual(self.http.call_count, 1)

    def test_receipt_identity_collision_cannot_confirm_a_second_part(self):
        first = self.command()
        self.responses = [(200, '{"message_id":"same-mid"}')]
        self.assertEqual(self.dispatch(first).state, "sent")
        second = self.command("Друга відповідь")
        self.responses = [(200, '{"message_id":"same-mid"}')]
        self.assertEqual(self.dispatch(second).state, "unknown")
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertIsNone(HumanReplyPart.objects.get(command=second).provider_message_id)

    def test_context_change_during_transport_preflight_denies_physical_request(self):
        command = self.command()

        def change_source(settings):
            InstagramBotMessage.objects.filter(pk=self.source.pk).update(quick_reply_payload="new-size-L")
            return "local-test-token"

        self.token.side_effect = change_source
        self.assertEqual(self.dispatch(command).state, "cancelled")
        self.http.assert_not_called()
        self.assertIsNone(HumanReplyPart.objects.get(command=command).provider_started_at)

    def test_namespace_rotation_before_send_denies_physical_request(self):
        command = self.command()

        def rotate(settings):
            InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(ig_user_id="other-owner")
            return "local-test-token"

        self.token.side_effect = rotate
        self.assertEqual(self.dispatch(command).state, "cancelled")
        self.http.assert_not_called()

    def test_aba_account_snapshot_must_match_part_even_when_current_database_matches_again(self):
        from django.db.models.query import QuerySet
        command = self.command()
        first = QuerySet.first
        snapshots = []

        def capture_rotated_snapshot(queryset):
            # claim/boundary use FOR UPDATE; only the actual unlocked transport
            # snapshot is read as B. DB returns to A before physical guards run.
            if queryset.model is InstagramBotSettings and not queryset._for_write and not snapshots:
                InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(ig_user_id="aba-account-B")
                snapshot = first(queryset)
                snapshots.append(snapshot)
                InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(ig_user_id="owner-1")
                return snapshot
            return first(queryset)

        with patch.object(QuerySet, "first", new=capture_rotated_snapshot):
            result = self.dispatch(command)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].ig_user_id, "aba-account-B")
        self.settings_row.refresh_from_db()
        self.assertEqual(self.settings_row.ig_user_id, "owner-1")
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.failure_code, "human_transport_namespace_changed")
        part = HumanReplyPart.objects.get(command=command)
        self.assertEqual(part.provider_namespace, self.namespace)
        self.assertEqual(part.state, "cancelled")
        self.assertIsNone(part.provider_started_at)
        self.assertIsNone(part.provider_message_id)
        self.http.assert_not_called()
        self.memory.assert_not_called()
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_epoch_change_before_physical_send_denies_request(self):
        command = self.command()

        def change_epoch(settings):
            IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=command.permission_epoch + 1)
            return "local-test-token"

        self.token.side_effect = change_epoch
        self.assertEqual(self.dispatch(command).state, "cancelled")
        self.http.assert_not_called()

    def test_scope_changes_after_first_post_preserve_receipt_and_stop_later_parts(self):
        command = self.command("A" * 1500)

        def change_after_post(part):
            IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=command.permission_epoch + 1)
            InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(ig_user_id="new-owner")
            InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="changed after physical request")

        self.physical_hook = change_after_post
        result = self.dispatch(command)
        self.assertEqual(result.state, "unknown")
        self.assertEqual(self.http.call_count, 1)
        message = InstagramBotMessage.objects.get(role="manager")
        self.assertEqual(message.provider_namespace, self.namespace)
        self.assertEqual(message.provider_message_id, "physical-mid-1")
        self.assertEqual(HumanReplyPart.objects.get(command=command, ordinal=1).state, "cancelled")

    def test_crash_after_start_reaper_and_late_exact_receipt_never_resend(self):
        command = self.command()
        self.responses = [_ProcessCrash()]
        with self.assertRaises(_ProcessCrash):
            self.dispatch(command)
        part = HumanReplyPart.objects.get(command=command)
        self.assertEqual(part.state, "provider_started")
        token = part.claim_token
        self.assertTrue(reap_human_part(part.pk, now=self.now + timedelta(minutes=2)).ready)
        command.refresh_from_db()
        self.assertEqual(command.state, "unknown")
        self.dispatch(command)
        self.assertEqual(self.http.call_count, 1)
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=command.permission_epoch + 10)
        InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(ig_user_id="later-owner")
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="later customer correction")
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            late = checkpoint_human_part_receipt(part.pk, token, provider_namespace=self.namespace, provider_message_id="late-exact")
            checkpoint_human_part_receipt(part.pk, token, provider_namespace=self.namespace, provider_message_id="late-exact")
        self.assertTrue(late.ready)
        command.refresh_from_db()
        self.assertEqual(command.state, "sent")
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.memory.call_count, 1)
        self.assertEqual(InstagramBotMessage.objects.get(role="manager").provider_namespace, self.namespace)

    def test_concurrent_dispatcher_cannot_post_a_started_part_again(self):
        command = self.command()
        seen = []

        def competing(part):
            seen.append(self.dispatch(command).state)

        self.physical_hook = competing
        self.assertEqual(self.dispatch(command).state, "sent")
        self.assertEqual(seen, ["provider_started"])
        self.assertEqual(self.http.call_count, 1)

    def test_projection_after_erasure_does_not_recreate_private_text(self):
        command = self.command()
        self.responses = [_ProcessCrash()]
        with self.assertRaises(_ProcessCrash):
            self.dispatch(command)
        part = HumanReplyPart.objects.get(command=command)
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=self.now)
        with self.captureOnCommitCallbacks(execute=True):
            result = checkpoint_human_part_receipt(part.pk, part.claim_token, provider_namespace=self.namespace,
                provider_message_id="known-after-erasure")
        self.assertTrue(result.ready)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.memory.assert_not_called()
        self.assertEqual(project_human_part_receipt(part.pk).reason, "client_unavailable")

    def test_dispatch_requires_accepted_transaction_to_commit_before_provider(self):
        command = self.command()
        with transaction.atomic():
            with self.assertRaises(HumanReplyRejected) as caught:
                self.dispatch(command)
        self.assertEqual(caught.exception.code, "human_dispatch_requires_commit")
        self.http.assert_not_called()
        self.assertEqual(HumanReplyPart.objects.get(command=command).state, "planned")

    def test_legacy_pending_command_uses_existing_whole_send_without_backfill(self):
        legacy = HumanReplyCommand.objects.create(client=self.customer, actor=self.actor, context_message=self.source,
            recipient_igsid=self.customer.igsid, provider_namespace=self.namespace, text="Legacy manager reply",
            permission_epoch=self.customer.reply_permission_epoch,
            window_deadline=_window_deadline(self.source), context_revision=str(self.source.pk))
        with patch("management.services.instagram_bot.send_text", return_value=SimpleNamespace(ok=True, kind="", hint="",
                  provider_message_ids=("legacy-exact",))) as send:
            result = self.dispatch(legacy)
            self.dispatch(legacy)
        self.assertEqual(result.state, "sent")
        self.assertEqual(send.call_count, 1)
        self.assertFalse(HumanReplyPart.objects.filter(command=legacy).exists())
        self.http.assert_not_called()

    def test_malformed_plan_never_falls_through_to_legacy_whole_send(self):
        command = self.command()
        command.operation_context[PLAN_KEY] = {"version": "broken"}
        command.save(update_fields=["operation_context"])
        with patch("management.services.instagram_bot.send_text") as send:
            result = self.dispatch(command)
            self.dispatch(command)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.failure_code, "human_plan_changed")
        send.assert_not_called()
        self.http.assert_not_called()

    def draft(self, text="Збережений серверний текст", kind="reply_draft"):
        from management.services.ig_human_reply_delivery import create_private_document
        return create_private_document(self.customer.pk, actor=self.actor, kind=kind, text=text,
            context_message_id=self.source.pk, provider_namespace=self.namespace, now=self.now)

    def from_draft(self, doc, operation_id="33333333-3333-3333-3333-333333333333", **kwargs):
        from management.services.ig_human_reply import create_human_reply_command_from_draft
        return create_human_reply_command_from_draft(kwargs.pop("client_id", self.customer.pk),
            actor=kwargs.pop("actor", self.actor), operation_id=operation_id, document_id=doc.document_id,
            expected_version=kwargs.pop("expected_version", doc.version), expected_hash=kwargs.pop("expected_hash", doc.text_hash),
            now=self.now, **kwargs)

    def assert_draft_rejected(self, code, doc, **kwargs):
        with self.assertRaises(HumanReplyRejected) as caught:
            self.from_draft(doc, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_saved_draft_consumes_server_version_with_one_barrier_and_no_provider(self):
        from management.services.ig_human_reply import _human_takeover_transition
        from management.services.ig_human_reply_delivery import update_private_document
        draft = self.draft()
        saved = update_private_document(draft.document_id, actor=self.actor, expected_version=1,
            expected_hash=draft.text_hash, text="Остання збережена версія", now=self.now)
        with patch("management.services.ig_human_reply._human_takeover_transition", wraps=_human_takeover_transition) as barrier:
            result = self.from_draft(saved)
        barrier.assert_called_once()
        saved.refresh_from_db()
        self.assertEqual((saved.state, saved.version), ("consumed", 3))
        self.assertEqual(saved.consumed_command_id, result.command.pk)
        self.assertEqual(result.command.text, "Остання збережена версія")
        self.assertEqual(result.command.context_message_id, self.source.pk)
        self.assertEqual(result.command.provider_namespace, self.namespace)
        self.assertEqual(result.command.operation_context["private_draft"]["source_version"], 2)
        self.assertIn(PLAN_KEY, result.command.operation_context)
        self.http.assert_not_called()
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_draft_bridge_signature_rejects_browser_body_override(self):
        draft = self.draft()
        with self.assertRaises(TypeError):
            self.from_draft(draft, text="browser override")
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.http.assert_not_called()

    def test_same_consumed_operation_replay_is_readonly_and_other_operation_conflicts(self):
        draft = self.draft()
        first = self.from_draft(draft)
        self.customer.refresh_from_db()
        epoch = self.customer.reply_permission_epoch
        replay = self.from_draft(draft)
        self.assertTrue(replay.idempotent)
        self.assertEqual(replay.command.pk, first.command.pk)
        self.assertEqual(HumanReplyCommand.objects.count(), 1)
        self.assertEqual(HumanReplyPart.objects.count(), 1)
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").count(), 1)
        self.assert_draft_rejected("operation_conflict", draft, operation_id="44444444-4444-4444-4444-444444444444")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.reply_permission_epoch, epoch)
        self.http.assert_not_called()

    def test_stale_draft_cas_never_exposes_takeover_or_command(self):
        draft = self.draft()
        with patch("management.services.ig_permission_transitions.cancel_client_unstarted_automation") as cleanup:
            self.assert_draft_rejected("private_document_stale", draft, expected_version=2)
            self.assert_draft_rejected("private_document_stale", draft, expected_hash="a" * 64)
        cleanup.assert_not_called()
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.manager_takeover)
        self.assertEqual(self.customer.reply_permission_epoch, 0)
        self.assertFalse(HumanReplyCommand.objects.exists())
        draft.refresh_from_db()
        self.assertEqual((draft.state, draft.version), ("open", 1))

    def test_stale_context_epoch_and_namespace_reject_before_takeover(self):
        draft = self.draft()
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(quick_reply_payload="mutated-selection")
        self.assert_draft_rejected("private_context_changed", draft)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(quick_reply_payload="")
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=2)
        self.assert_draft_rejected("permission_epoch_changed", draft)
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=0)
        InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(ig_user_id="foreign-owner")
        self.assert_draft_rejected("provider_namespace_changed", draft)
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").exists())

    def test_internal_note_cannot_be_consumed_as_send(self):
        note = self.draft(kind="internal_note")
        self.assert_draft_rejected("private_note_not_sendable", note)
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.http.assert_not_called()

    def test_foreign_client_or_actor_cannot_consume_private_draft(self):
        draft = self.draft()
        other_client = IgClient.objects.create(igsid="foreign-draft-client")
        other_actor = get_user_model().objects.create_superuser(username="foreign-draft-admin", email="foreign@example.test", password="x")
        self.assert_draft_rejected("private_document_missing", draft, client_id=other_client.pk)
        self.assert_draft_rejected("private_document_owned_by_other_actor", draft, actor=other_actor)
        self.assertFalse(HumanReplyCommand.objects.exists())

    def test_actor_revocation_during_draft_barrier_wait_denies_consumption(self):
        draft = self.draft()
        original = IgClient.objects.select_for_update

        def revoke(*args, **kwargs):
            get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
            return original(*args, **kwargs)

        with patch.object(IgClient.objects, "select_for_update", side_effect=revoke):
            self.assert_draft_rejected("actor_not_authorized", draft)
        self.assertFalse(HumanReplyCommand.objects.exists())
        draft.refresh_from_db()
        self.assertEqual((draft.state, draft.version), ("open", 1))

    def test_bind_failure_rolls_back_command_plan_takeover_and_document(self):
        draft = self.draft()
        with patch("management.services.ig_human_reply_delivery.bind_private_draft_command",
                   side_effect=HumanReplyRejected("private_context_changed")):
            self.assert_draft_rejected("private_context_changed", draft)
        self.customer.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual((draft.state, draft.version), ("open", 1))
        self.assertIsNone(draft.consumed_command_id)
        self.assertFalse(self.customer.manager_takeover)
        self.assertEqual(self.customer.reply_permission_epoch, 0)
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.assertFalse(HumanReplyPart.objects.exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").exists())
        self.http.assert_not_called()

    def test_open_draft_cannot_adopt_preexisting_normal_operation(self):
        draft = self.draft()
        command = self.command(draft.text, operation_id="33333333-3333-3333-3333-333333333333")
        self.assert_draft_rejected("operation_conflict", draft)
        draft.refresh_from_db()
        self.assertEqual(draft.state, "open")
        self.assertIsNone(draft.consumed_command_id)
        self.assertEqual(HumanReplyCommand.objects.get().pk, command.pk)

    def test_outer_transaction_rejected_before_draft_takeover(self):
        draft = self.draft()
        with transaction.atomic():
            self.assert_draft_rejected("human_draft_requires_commit", draft)
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.http.assert_not_called()

    def test_consumed_retry_preserves_original_binding_after_current_source_changes(self):
        draft = self.draft()
        accepted = self.from_draft(draft)
        original = accepted.command.operation_context[PLAN_KEY]["context_binding"]
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="later correction", quick_reply_payload="later-L")
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=accepted.command.permission_epoch + 5)
        replay = self.from_draft(draft)
        self.assertTrue(replay.idempotent)
        self.assertEqual(replay.command.operation_context[PLAN_KEY]["context_binding"], original)
        self.http.assert_not_called()

    def unknown_case(self):
        command = self.command()
        self.responses = [(200, '{}')]
        self.dispatch(command)
        return command, IgFollowUpTask.objects.get(event_key=f"human-reply-unknown:{command.pk}")

    def resolve(self, task, outcome="handled"):
        from management.services.ig_human_reply import resolve_unknown_human_reply
        return resolve_unknown_human_reply(task.pk, client_id=self.customer.pk, actor=self.actor,
            outcome=outcome, note="Оператор звірив", now=self.now)

    def test_operator_resolution_preserves_unknown_and_same_outcome_replay(self):
        command, task = self.unknown_case()
        first = self.resolve(task)
        replay = self.resolve(task)
        conflict = self.resolve(task, outcome="delivered")
        self.assertTrue(first["ok"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(conflict["error"], "resolution_conflict")
        command.refresh_from_db()
        self.assertEqual(command.state, "unknown")
        self.assertEqual(command.provider_message_ids, [])
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.assertEqual(self.http.call_count, 1)

    def test_operator_resolution_locks_client_command_parts_before_task(self):
        _, task = self.unknown_case()
        order = []
        originals = [(IgClient.objects, "client"), (HumanReplyCommand.objects, "command"),
            (HumanReplyPart.objects, "parts"), (IgFollowUpTask.objects, "task")]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for manager, label in originals:
                original = manager.select_for_update

                def track(*args, _original=original, _label=label, **kwargs):
                    order.append(_label)
                    return _original(*args, **kwargs)

                stack.enter_context(patch.object(manager, "select_for_update", side_effect=track))
            self.assertTrue(self.resolve(task)["ok"])
        self.assertEqual(order, ["client", "command", "parts", "task"])

    def test_operator_revocation_during_lock_wait_is_rechecked(self):
        _, task = self.unknown_case()
        original = IgClient.objects.select_for_update

        def revoke(*args, **kwargs):
            get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
            return original(*args, **kwargs)

        with patch.object(IgClient.objects, "select_for_update", side_effect=revoke):
            with self.assertRaises(HumanReplyRejected) as caught:
                self.resolve(task)
        self.assertEqual(caught.exception.code, "actor_not_authorized")
        task.refresh_from_db()
        self.assertNotIn("resolution", task.manager_context)
        self.assertEqual(task.status, "skipped")

    def test_operator_task_binding_mutation_after_locator_is_rejected(self):
        _, task = self.unknown_case()
        original = IgClient.objects.select_for_update

        def mutate(*args, **kwargs):
            context = {**task.manager_context, "operation_id": "44444444-4444-4444-4444-444444444444"}
            IgFollowUpTask.objects.filter(pk=task.pk).update(manager_context=context)
            return original(*args, **kwargs)

        with patch.object(IgClient.objects, "select_for_update", side_effect=mutate):
            result = self.resolve(task)
        self.assertEqual(result["error"], "resolution_binding_changed")
        task.refresh_from_db()
        self.assertNotIn("resolution", task.manager_context)
        self.assertEqual(task.status, "skipped")


    def test_partial_late_receipt_refresh_preserves_immutable_case_and_operator_resolution(self):
        from copy import deepcopy
        from management.services.ig_human_reply_transport import ensure_planned_human_reconciliation
        command = self.command("A" * 1500)
        self.responses = [TimeoutError("fake first-part timeout")]
        self.assertEqual(self.dispatch(command).state, "unknown")
        part = HumanReplyPart.objects.get(command=command, ordinal=0)
        task = IgFollowUpTask.objects.get(event_key=f"human-reply-unknown:{command.pk}")
        original = deepcopy(task.event_payload)
        self.assertTrue(self.resolve(task)["ok"])
        task.refresh_from_db()
        resolution = deepcopy(task.manager_context["resolution"])
        checkpoint_human_part_receipt(part.pk, part.claim_token, provider_namespace=self.namespace,
            provider_message_id="late-first-exact")
        command.refresh_from_db()
        self.assertEqual(command.state, "unknown")
        refreshed = ensure_planned_human_reconciliation(command, now=self.now)
        self.assertEqual(refreshed.event_payload, original)
        self.assertEqual(refreshed.manager_context["resolution"], resolution)
        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.manager_context["latest_delivery"]["provider_message_ids"], ["late-first-exact"])
        self.assertEqual(refreshed.manager_context["latest_delivery"]["parts"][0]["state"], "sent")
        self.assertEqual(refreshed.manager_context["latest_delivery"]["parts"][1]["state"], "cancelled")
        self.assertEqual(self.http.call_count, 1)


    def test_early_echo_hook_runs_after_receipt_projection_without_application_locks(self):
        command = self.command()
        seen = []

        def after_commit(part_id):
            part = HumanReplyPart.objects.get(pk=part_id)
            self.assertEqual(part.state, "sent")
            self.assertTrue(InstagramBotMessage.objects.filter(provider_namespace=part.provider_namespace,
                provider_message_id=part.provider_message_id, role="manager", send_state="sent").exists())
            self.assertTrue(all(getattr(block, "_from_testcase", False) for block in connection.atomic_blocks))
            seen.append(part.pk)

        with patch("management.services.ig_revision_echo_integration.reconcile_human_part_echoes", side_effect=after_commit):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.dispatch(command).state, "sent")
        self.assertEqual(seen, [HumanReplyPart.objects.get(command=command).pk])
        self.assertEqual(self.http.call_count, 1)

    def test_after_commit_echo_failure_preserves_exact_receipts_and_cannot_enable_resend(self):
        command = self.command("A" * 1500)
        with patch("management.services.ig_revision_echo_integration.reconcile_human_part_echoes",
                   side_effect=RuntimeError("deferred DB-only echo projection")) as hook:
            with patch("django.test.testcases.logger.error"):
                with self.captureOnCommitCallbacks(execute=True):
                    self.assertEqual(self.dispatch(command).state, "sent")
                    self.assertEqual(self.dispatch(command).state, "sent")
        command.refresh_from_db()
        self.assertEqual(command.state, "sent")
        self.assertEqual(hook.call_count, 2)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(command.provider_message_ids, ["physical-mid-1", "physical-mid-2"])
