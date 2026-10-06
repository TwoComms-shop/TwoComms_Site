"""Human store contract. Transport is forbidden; dispatcher integration is separate."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand, IgFunnelResetAudit
from management.ig_human_reply_models import HumanReplyPart, HumanReplyPrivateDocument, human_payload_digest
from management.models import AdminAuditLog, IgClient, InstagramBotMessage, InstagramBotSettings
from management.services.ig_human_reply import HumanReplyRejected, create_human_reply_command
from management.services.ig_human_reply_delivery import (
    PLAN_KEY, bind_private_draft_command, claim_next_human_part, classify_human_receipt,
    create_private_document, human_reaper_candidates, plan_human_command, reap_human_part,
    record_human_part_started, settle_human_part, update_private_document,
)


class HumanReceiptClassificationTests(SimpleTestCase):
    def test_only_exact_success_mid_is_confirmed(self):
        self.assertEqual(classify_human_receipt(http_status=200, provider_message_id="m-1").state, "sent")
        for mid in ("", "bad\nmid", "x" * 256):
            with self.subTest(mid=mid[:10]):
                self.assertEqual(classify_human_receipt(http_status=200, provider_message_id=mid).state, "unknown")

    def test_ambiguous_http_and_transport_never_grant_retry(self):
        for status in (None, 202, 408, 425, 429, 500, True):
            self.assertEqual(classify_human_receipt(http_status=status).state, "unknown")
        self.assertEqual(classify_human_receipt(http_status=200, provider_message_id="mid", transport_outcome="timeout").state, "unknown")
        self.assertEqual(classify_human_receipt(http_status=403).state, "definite_failed")
        self.assertEqual(classify_human_receipt(transport_outcome="definite_rejection").state, "definite_failed")


class _HumanStoreFixture(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.env = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transport = patch("management.services.instagram_bot.send_text", side_effect=AssertionError("store called transport"))
        self.transport_mock = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.actor = get_user_model().objects.create_superuser(username="part-owner", password="x", email="part@example.test")
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="owner-1", page_id="owner-1")
        self.customer = IgClient.objects.create(igsid="human-store-customer")
        self.namespace = "instagram_login:owner-1"
        self.source = self.inbound("Підкажіть розмір")

    def inbound(self, text, *, namespace=None, client=None):
        owner = client or self.customer
        return InstagramBotMessage.objects.create(client=owner, sender_id=owner.igsid, role="user", source="webhook",
            provider_namespace=namespace or self.namespace, text=text, mid="human-store-" + uuid.uuid4().hex,
            provider_created_at=self.now - timedelta(minutes=2), status="done")

    def command(self, text="Вітаю!", **kwargs):
        return create_human_reply_command(self.customer.pk, actor=self.actor, text=text,
            context_message_id=self.source.pk, now=self.now, **kwargs).command

    def plan(self, text="Вітаю!"):
        command = self.command(text)
        result = plan_human_command(command.pk, now=self.now)
        self.assertTrue(result.ready, result.reason)
        return command, result.parts

    def started(self, text="Вітаю!"):
        command, rows = self.plan(text)
        claim = claim_next_human_part(command.pk, now=self.now)
        self.assertTrue(claim.ready, claim.reason)
        started = record_human_part_started(claim.part.pk, claim.token, now=self.now)
        self.assertTrue(started.ready, started.reason)
        return command, rows, claim

    def confirm(self, claim, mid="human-mid-1", **kwargs):
        return settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace,
            http_status=200, provider_message_id=mid, now=self.now, **kwargs)

    def draft(self, text="Вітаю!", kind="reply_draft", **kwargs):
        return create_private_document(self.customer.pk, actor=self.actor, kind=kind, text=text,
            context_message_id=kwargs.pop("context_message_id", self.source.pk),
            provider_namespace=kwargs.pop("provider_namespace", self.namespace), now=self.now, **kwargs)

    def reject(self, code, fn, *args, **kwargs):
        with self.assertRaises(HumanReplyRejected) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def tearDown(self):
        self.transport_mock.assert_not_called()
        super().tearDown()


class HumanDeliveryStoreTests(_HumanStoreFixture):
    def test_plan_replay_preserves_payload_and_existing_operation_context(self):
        command = self.command("A" * 1500)
        original = deepcopy(command.operation_context)
        original.pop(PLAN_KEY, None)
        first = plan_human_command(command.pk, now=self.now)
        second = plan_human_command(command.pk, now=self.now)
        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertFalse(second.changed)
        self.assertEqual([part.pk for part in first.parts], [part.pk for part in second.parts])
        self.assertEqual(len(first.parts), 2)
        command.refresh_from_db()
        self.assertEqual({key: value for key, value in command.operation_context.items() if key != PLAN_KEY}, original)
        self.assertEqual(first.parts[0].payload["recipient"], {"id": self.customer.igsid})
        self.assertEqual(first.parts[0].payload_digest, human_payload_digest(first.parts[0].payload))
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_payload_cannot_change_via_save_or_queryset(self):
        _, rows = self.plan()
        part = rows[0]
        part.payload = {"recipient": {"id": part.recipient_igsid}, "message": {"text": "changed"}}
        part.payload_digest = human_payload_digest(part.payload)
        with self.assertRaises(ValueError):
            part.save()
        with self.assertRaises(ValueError):
            HumanReplyPart.objects.filter(pk=part.pk).update(payload={})
        with self.assertRaises(ValueError):
            HumanReplyPart.objects.bulk_update([part], ["payload"])

    def test_legacy_started_command_is_not_backfilled_into_new_attempt(self):
        # Persist the actual pre-part command shape, without a new plan.
        command = HumanReplyCommand.objects.create(client=self.customer, actor=self.actor,
            context_message=self.source, recipient_igsid=self.customer.igsid, provider_namespace=self.namespace,
            text="Вітаю!", permission_epoch=self.customer.reply_permission_epoch,
            window_deadline=self.now + timedelta(hours=23), context_revision=str(self.source.pk),
            draft_hash=hashlib.sha256("Вітаю!".encode()).hexdigest())
        command.state, command.provider_started_at = command.State.UNKNOWN, self.now
        command.save(update_fields=["state", "provider_started_at"])
        result = plan_human_command(command.pk, now=self.now)
        self.assertEqual(result.reason, "legacy_human_delivery_owned")
        self.assertFalse(HumanReplyPart.objects.exists())

    def test_competing_claim_and_stale_token_cannot_start(self):
        command, _ = self.plan()
        first = claim_next_human_part(command.pk, now=self.now)
        second = claim_next_human_part(command.pk, now=self.now)
        self.assertEqual(second.reason, "human_part_already_claimed")
        self.assertEqual(record_human_part_started(first.part.pk, "foreign-token", now=self.now).reason, "human_part_claim_lost")
        self.assertTrue(record_human_part_started(first.part.pk, first.token, now=self.now).ready)
        self.assertEqual(record_human_part_started(first.part.pk, first.token, now=self.now).reason, "human_part_claim_lost")

    def test_changed_command_text_cannot_change_planned_send(self):
        command, _ = self.plan()
        HumanReplyCommand.objects.filter(pk=command.pk).update(text="changed")
        self.assertEqual(claim_next_human_part(command.pk, now=self.now).reason, "human_plan_changed")

    def test_private_and_delivery_bind_same_versioned_context_digest(self):
        draft = self.draft()
        command, _ = self.plan(draft.text)
        command.refresh_from_db()
        binding = command.operation_context[PLAN_KEY]["context_binding"]
        self.assertEqual(binding["schema_version"], "human-context.v1")
        self.assertEqual(binding["source_digest"], draft.context_digest)
        self.assertEqual(binding["reset_floor"], draft.reset_floor)
        self.assertEqual(binding["message_id"], draft.context_message_id)

    def test_owned_source_text_quick_reply_and_reply_to_changes_block_claim(self):
        command, _ = self.plan()
        mutations = {"text": "changed input", "quick_reply_payload": "changed-size-L",
            "reply_to_provider_message_id": "changed-reference",
            "attachments": "https://example.test/changed-image.jpg",
            "attachment_media": [{"type": "image", "url": "https://example.test/changed-media.jpg"}]}
        for field, value in mutations.items():
            with self.subTest(field=field):
                original = getattr(self.source, field)
                InstagramBotMessage.objects.filter(pk=self.source.pk).update(**{field: value})
                self.assertEqual(claim_next_human_part(command.pk, now=self.now).reason, "human_context_changed")
                self.assertIsNone(HumanReplyPart.objects.get(command=command).provider_started_at)
                InstagramBotMessage.objects.filter(pk=self.source.pk).update(**{field: original})
        self.assertTrue(claim_next_human_part(command.pk, now=self.now).ready)

    def test_owned_source_mutation_during_claim_blocks_provider_start(self):
        mutations = {"text": "changed input", "quick_reply_payload": "changed-size-L",
            "reply_to_provider_message_id": "changed-reference",
            "attachments": "https://example.test/changed-image.jpg",
            "attachment_media": [{"type": "image", "url": "https://example.test/changed-media.jpg"}]}
        for field, value in mutations.items():
            with self.subTest(field=field):
                command, _ = self.plan()
                claim = claim_next_human_part(command.pk, now=self.now)
                original = getattr(self.source, field)
                InstagramBotMessage.objects.filter(pk=self.source.pk).update(**{field: value})
                result = record_human_part_started(claim.part.pk, claim.token, now=self.now)
                self.assertEqual(result.reason, "human_context_changed")
                claim.part.refresh_from_db()
                self.assertIsNone(claim.part.provider_started_at)
                self.assertEqual(claim.part.state, "cancelled")
                InstagramBotMessage.objects.filter(pk=self.source.pk).update(**{field: original})

    def test_plan_tampering_is_rejected_before_claim(self):
        command, _ = self.plan()
        command.refresh_from_db()
        command.operation_context[PLAN_KEY]["parts"][0]["payload"]["message"]["text"] = "tampered"
        command.save(update_fields=["operation_context"])
        self.assertEqual(claim_next_human_part(command.pk, now=self.now).reason, "human_plan_changed")

    def test_bot_pause_allows_authorized_human_send(self):
        command, _, claim = self.started()
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.bot_paused)
        self.assertTrue(self.customer.manager_takeover)
        self.assertTrue(self.confirm(claim).ready)
        command.refresh_from_db()
        self.assertEqual(command.state, "sent")

    def test_epoch_change_after_claim_blocks_start(self):
        command, _ = self.plan()
        claim = claim_next_human_part(command.pk, now=self.now)
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=command.permission_epoch + 1)
        self.assertEqual(record_human_part_started(claim.part.pk, claim.token, now=self.now).reason, "permission_epoch_changed")
        claim.part.refresh_from_db()
        self.assertIsNone(claim.part.provider_started_at)

    def test_newer_inbound_after_claim_blocks_start(self):
        command, _ = self.plan()
        claim = claim_next_human_part(command.pk, now=self.now)
        self.inbound("Ще одне питання")
        self.assertEqual(record_human_part_started(claim.part.pk, claim.token, now=self.now).reason, "newer_inbound")

    def test_reset_after_claim_blocks_old_context(self):
        command, _ = self.plan()
        claim = claim_next_human_part(command.pk, now=self.now)
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk, reason="manual test reset")
        self.assertEqual(record_human_part_started(claim.part.pk, claim.token, now=self.now).reason, "context_before_reset")

    def test_terminal_command_cannot_start_claimed_part(self):
        command, _ = self.plan()
        claim = claim_next_human_part(command.pk, now=self.now)
        HumanReplyCommand.objects.filter(pk=command.pk).update(state="cancelled")
        self.assertEqual(record_human_part_started(claim.part.pk, claim.token, now=self.now).reason, "human_command_terminal")

    def test_revoked_actor_or_namespace_blocks_start(self):
        command, _ = self.plan()
        claim = claim_next_human_part(command.pk, now=self.now)
        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        self.assertEqual(record_human_part_started(claim.part.pk, claim.token, now=self.now).reason, "actor_not_authorized")

    def test_provider_namespace_change_blocks_start(self):
        command, _ = self.plan()
        claim = claim_next_human_part(command.pk, now=self.now)
        InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(ig_user_id="other-owner")
        self.assertEqual(record_human_part_started(claim.part.pk, claim.token, now=self.now).reason, "provider_namespace_changed")

    def test_receipt_requires_started_original_token_and_namespace(self):
        command, _ = self.plan()
        claim = claim_next_human_part(command.pk, now=self.now)
        self.assertEqual(self.confirm(claim).reason, "human_part_receipt_not_owned")
        record_human_part_started(claim.part.pk, claim.token, now=self.now)
        self.assertEqual(settle_human_part(claim.part.pk, "other", provider_namespace=self.namespace, http_status=200,
            provider_message_id="mid").reason, "human_part_receipt_not_owned")
        self.assertEqual(settle_human_part(claim.part.pk, claim.token, provider_namespace="instagram_login:other", http_status=200,
            provider_message_id="mid").reason, "human_receipt_namespace_changed")
        self.assertTrue(self.confirm(claim).ready)

    def test_exact_receipt_is_idempotent_and_single_assignment(self):
        command, _, claim = self.started()
        self.assertTrue(self.confirm(claim).changed)
        replay = self.confirm(claim)
        self.assertTrue(replay.ready)
        self.assertFalse(replay.changed)
        self.assertEqual(self.confirm(claim, "different-mid").reason, "human_receipt_conflict")
        claim.part.refresh_from_db()
        claim.part.provider_message_id = "different-mid"
        with self.assertRaises(ValueError):
            claim.part.save()
        with self.assertRaises(ValueError):
            HumanReplyPart.objects.filter(pk=claim.part.pk).update(provider_message_id="different-mid")
        command.refresh_from_db()
        self.assertEqual(command.provider_message_ids, ["human-mid-1"])

    def test_checkpoint_each_part_before_next_start(self):
        command, rows, first = self.started("A" * 1500)
        self.assertEqual(claim_next_human_part(command.pk, now=self.now).reason, "human_provider_result_unresolved")
        self.assertTrue(self.confirm(first, "mid-first").ready)
        command.refresh_from_db()
        self.assertEqual(command.provider_message_ids, ["mid-first"])
        second = claim_next_human_part(command.pk, now=self.now)
        self.assertEqual(second.part.ordinal, 1)
        self.assertTrue(record_human_part_started(second.part.pk, second.token, now=self.now).ready)
        self.assertTrue(self.confirm(second, "mid-second").ready)
        command.refresh_from_db()
        self.assertEqual(command.state, "sent")
        self.assertEqual(command.provider_message_ids, ["mid-first", "mid-second"])

    def test_mid_cannot_belong_to_two_parts_in_same_namespace(self):
        command, _, first = self.started("A" * 1500)
        self.confirm(first, "same-mid")
        second = claim_next_human_part(command.pk, now=self.now)
        record_human_part_started(second.part.pk, second.token, now=self.now)
        self.assertEqual(self.confirm(second, "same-mid").reason, "human_receipt_identity_conflict")
        second.part.refresh_from_db()
        self.assertEqual(second.part.state, "provider_started")
        self.assertIsNone(second.part.provider_message_id)

    def test_partial_unknown_cancels_unstarted_parts_without_retry(self):
        command, rows, claim = self.started("A" * 2500)
        self.confirm(claim, "mid-first")
        second = claim_next_human_part(command.pk, now=self.now)
        record_human_part_started(second.part.pk, second.token, now=self.now)
        result = settle_human_part(second.part.pk, second.token, provider_namespace=self.namespace, transport_outcome="timeout", now=self.now)
        self.assertFalse(result.ready)
        command.refresh_from_db()
        self.assertEqual(command.state, "unknown")
        self.assertEqual(command.provider_message_ids, ["mid-first"])
        self.assertEqual(HumanReplyPart.objects.get(pk=rows[2].pk).state, "cancelled")
        self.assertEqual(claim_next_human_part(command.pk, now=self.now).reason, "human_command_terminal")
        self.assertFalse(human_reaper_candidates(now=self.now + timedelta(minutes=5)))

    def test_definite_failure_cancels_future_parts(self):
        command, rows, claim = self.started("A" * 1500)
        result = settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace, http_status=403, now=self.now)
        self.assertEqual(result.reason, "provider_rejected")
        command.refresh_from_db()
        self.assertEqual(command.state, "definite_failed")
        self.assertEqual(HumanReplyPart.objects.get(pk=rows[1].pk).state, "cancelled")
        self.assertEqual(claim_next_human_part(command.pk, now=self.now).reason, "human_command_terminal")

    def test_expired_unstarted_claim_can_be_reclaimed_with_new_token(self):
        command, _ = self.plan()
        first = claim_next_human_part(command.pk, now=self.now, lease_seconds=1)
        after = self.now + timedelta(seconds=2)
        self.assertEqual(human_reaper_candidates(now=after, limit=1), (first.part.pk,))
        self.assertTrue(reap_human_part(first.part.pk, now=after).ready)
        second = claim_next_human_part(command.pk, now=after)
        self.assertTrue(second.ready)
        self.assertNotEqual(second.token, first.token)
        self.assertEqual(record_human_part_started(first.part.pk, first.token, now=after).reason, "human_part_claim_lost")

    def test_started_crash_becomes_unknown_and_late_exact_receipt_closes_original(self):
        command, _, claim = self.started()
        later = self.now + timedelta(minutes=2)
        self.assertTrue(reap_human_part(claim.part.pk, now=later).ready)
        command.refresh_from_db()
        self.assertEqual(command.state, "unknown")
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=command.permission_epoch + 10,
            privacy_erasure_started_at=later)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="mutated after physical start",
            quick_reply_payload="later-selection", reply_to_provider_message_id="later-parent")
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk, reason="after start reset")
        self.actor.delete()
        late = settle_human_part(claim.part.pk, claim.token, provider_namespace=self.namespace, http_status=200,
            provider_message_id="late-exact-mid", now=later)
        self.assertTrue(late.ready, late.reason)
        command.refresh_from_db()
        self.assertEqual(command.state, "sent")
        self.assertEqual(command.provider_message_ids, ["late-exact-mid"])
        self.assertFalse(claim_next_human_part(command.pk, now=later).ready)

    def test_late_partial_receipt_never_resumes_cancelled_remaining_part(self):
        command, rows, claim = self.started("A" * 1500)
        reap_human_part(claim.part.pk, now=self.now + timedelta(minutes=2))
        self.assertTrue(self.confirm(claim, "late-first").ready)
        command.refresh_from_db()
        self.assertEqual(command.state, "unknown")
        self.assertEqual(HumanReplyPart.objects.get(pk=rows[1].pk).state, "cancelled")
        self.assertEqual(claim_next_human_part(command.pk, now=self.now).reason, "human_command_terminal")

    def test_deleted_client_late_receipt_does_not_recreate_data(self):
        _, _, claim = self.started()
        self.customer.delete()
        self.assertEqual(self.confirm(claim).reason, "human_part_missing")
        self.assertFalse(HumanReplyPart.objects.exists())
        self.assertFalse(HumanReplyCommand.objects.exists())


class HumanPrivateDocumentTests(_HumanStoreFixture):
    def test_save_and_same_id_replay_have_no_takeover_or_send_intent(self):
        identity = uuid.uuid4()
        doc = self.draft(document_id=identity)
        replay = self.draft(document_id=identity)
        self.assertEqual(doc.pk, replay.pk)
        self.assertEqual(doc.version, 1)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.manager_takeover)
        self.assertFalse(self.customer.bot_paused)
        self.assertEqual(self.customer.reply_permission_epoch, 0)
        self.assertFalse(HumanReplyCommand.objects.exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_bot.human_reply_command_created").exists())
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        self.reject("private_document_conflict", self.draft, text="other text", document_id=identity)

    def test_two_tabs_cas_rejects_stale_version_and_hash(self):
        doc = self.draft()
        original_hash = doc.text_hash
        updated = update_private_document(doc.document_id, actor=self.actor, expected_version=1, expected_hash=original_hash,
            text="Новий текст", now=self.now)
        self.assertEqual(updated.version, 2)
        self.reject("private_document_stale", update_private_document, doc.document_id, actor=self.actor,
            expected_version=1, expected_hash=original_hash, text="Другий таб", now=self.now)
        self.reject("private_document_stale", update_private_document, doc.document_id, actor=self.actor,
            expected_version=2, expected_hash=original_hash, text="Другий таб", now=self.now)
        updated.refresh_from_db()
        self.assertEqual(updated.text, "Новий текст")

    def test_note_cannot_bind_to_customer_command(self):
        note = self.draft(kind="internal_note")
        command = self.command(note.text)
        self.reject("private_note_not_sendable", bind_private_draft_command, note.document_id, command.pk,
            actor=self.actor, expected_version=note.version, expected_hash=note.text_hash)
        note.refresh_from_db()
        self.assertEqual(note.state, "open")

    def test_actual_takeover_audit_binds_original_draft_epoch_and_idempotent_click(self):
        draft = self.draft()
        command = self.command(draft.text, expected_permission_epoch=draft.source_permission_epoch)
        self.assertEqual(command.permission_epoch, draft.source_permission_epoch + 1)
        consumed = bind_private_draft_command(draft.document_id, command.pk, actor=self.actor,
            expected_version=1, expected_hash=draft.text_hash, now=self.now)
        replay = bind_private_draft_command(draft.document_id, command.pk, actor=self.actor,
            expected_version=1, expected_hash=draft.text_hash, now=self.now)
        self.assertEqual(consumed.pk, replay.pk)
        self.assertEqual(consumed.version, 2)
        self.assertEqual(consumed.state, "consumed")
        self.assertEqual(consumed.consumed_command_id, command.pk)
        self.reject("private_document_closed", update_private_document, draft.document_id, actor=self.actor,
            expected_version=2, expected_hash=draft.text_hash, text="edited", now=self.now)

    def test_missing_or_stale_original_epoch_audit_cannot_bind(self):
        doc = self.draft()
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=2)
        command = self.command(doc.text)
        self.reject("permission_epoch_changed", bind_private_draft_command, doc.document_id, command.pk, actor=self.actor,
            expected_version=1, expected_hash=doc.text_hash, now=self.now)
        doc.refresh_from_db()
        self.assertEqual(doc.state, "open")

    def test_owner_and_capabilities_are_required(self):
        doc = self.draft()
        other = get_user_model().objects.create_superuser(username="other-manager", password="x", email="other@example.test")
        self.reject("private_document_owned_by_other_actor", update_private_document, doc.document_id, actor=other,
            expected_version=1, expected_hash=doc.text_hash, text="other", now=self.now)
        other.is_superuser, other.is_staff = False, False
        other.save(update_fields=["is_superuser", "is_staff"])
        self.reject("actor_not_authorized", create_private_document, self.customer.pk, actor=other, kind="reply_draft", text="hi",
            context_message_id=self.source.pk, provider_namespace=self.namespace, now=self.now)

    def test_actor_revocation_is_read_from_current_row(self):
        doc = self.draft()
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        self.reject("actor_not_authorized", update_private_document, doc.document_id, actor=self.actor,
            expected_version=1, expected_hash=doc.text_hash, text="changed", now=self.now)

    def test_revocation_while_waiting_client_lock_denies_create_update_and_bind(self):
        for operation in ("create", "update", "bind"):
            with self.subTest(operation=operation):
                get_user_model().objects.filter(pk=self.actor.pk).update(is_active=True)
                doc = self.draft()
                command = self.command(doc.text) if operation == "bind" else None
                original_lock = IgClient.objects.select_for_update

                def revoke_after_initial_actor_read(*args, **kwargs):
                    get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
                    return original_lock(*args, **kwargs)

                with patch.object(IgClient.objects, "select_for_update", side_effect=revoke_after_initial_actor_read):
                    if operation == "create":
                        self.reject("actor_not_authorized", self.draft)
                    elif operation == "update":
                        self.reject("actor_not_authorized", update_private_document, doc.document_id, actor=self.actor,
                            expected_version=1, expected_hash=doc.text_hash, text="changed", now=self.now)
                    else:
                        self.reject("actor_not_authorized", bind_private_draft_command, doc.document_id, command.pk,
                            actor=self.actor, expected_version=1, expected_hash=doc.text_hash, now=self.now)
                doc.refresh_from_db()
                self.assertEqual((doc.version, doc.state), (1, "open"))
                self.assertIsNone(doc.consumed_command_id)

    def test_document_disappears_after_locator_before_locked_read(self):
        for operation in ("update", "bind"):
            with self.subTest(operation=operation):
                doc = self.draft()
                command = self.command(doc.text) if operation == "bind" else None
                original_lock = HumanReplyPrivateDocument.objects.select_for_update

                def delete_after_locator(*args, **kwargs):
                    HumanReplyPrivateDocument.objects.filter(pk=doc.pk).delete()
                    return original_lock(*args, **kwargs)

                with patch.object(HumanReplyPrivateDocument.objects, "select_for_update", side_effect=delete_after_locator):
                    if operation == "update":
                        self.reject("private_document_missing", update_private_document, doc.document_id, actor=self.actor,
                            expected_version=1, expected_hash=doc.text_hash, text="changed", now=self.now)
                    else:
                        self.reject("private_document_missing", bind_private_draft_command, doc.document_id, command.pk,
                            actor=self.actor, expected_version=1, expected_hash=doc.text_hash, now=self.now)

    def test_client_purge_during_lock_wait_has_finite_result(self):
        for operation in ("update", "bind"):
            with self.subTest(operation=operation):
                doc = self.draft()
                command = self.command(doc.text) if operation == "bind" else None
                original_lock = IgClient.objects.select_for_update

                def purge_after_locator(*args, **kwargs):
                    IgClient.objects.filter(pk=self.customer.pk).delete()
                    return original_lock(*args, **kwargs)

                with patch.object(IgClient.objects, "select_for_update", side_effect=purge_after_locator):
                    if operation == "update":
                        self.reject("client_unavailable", update_private_document, doc.document_id, actor=self.actor,
                            expected_version=1, expected_hash=doc.text_hash, text="changed", now=self.now)
                    else:
                        self.reject("client_unavailable", bind_private_draft_command, doc.document_id, command.pk,
                            actor=self.actor, expected_version=1, expected_hash=doc.text_hash, now=self.now)

    def test_mutated_context_and_newer_inbound_invalidate_draft(self):
        doc = self.draft()
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="changed current input")
        self.reject("private_context_changed", update_private_document, doc.document_id, actor=self.actor,
            expected_version=1, expected_hash=doc.text_hash, text="edit", now=self.now)
        self.inbound("Нове питання")
        self.reject("newer_inbound", update_private_document, doc.document_id, actor=self.actor,
            expected_version=1, expected_hash=doc.text_hash, text="edit", now=self.now)

    def test_reset_and_erasure_invalidate_private_context(self):
        doc = self.draft()
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk, reason="reset")
        self.reject("context_before_reset", update_private_document, doc.document_id, actor=self.actor,
            expected_version=1, expected_hash=doc.text_hash, text="edit", now=self.now)
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=self.now)
        self.reject("client_unavailable", update_private_document, doc.document_id, actor=self.actor,
            expected_version=1, expected_hash=doc.text_hash, text="edit", now=self.now)

    def test_foreign_namespace_and_client_context_are_rejected(self):
        self.reject("context_namespace_changed", self.draft, provider_namespace="instagram_login:other")
        other = IgClient.objects.create(igsid="private-other")
        foreign = self.inbound("hello", client=other)
        self.reject("context_changed", create_private_document, self.customer.pk, actor=self.actor, kind="reply_draft", text="hi",
            context_message_id=foreign.pk, provider_namespace=self.namespace, now=self.now)

    def test_archive_is_versioned_and_queryset_cannot_reassign_owner(self):
        doc = self.draft()
        archived = update_private_document(doc.document_id, actor=self.actor, expected_version=1, expected_hash=doc.text_hash,
            archive=True, now=self.now)
        self.assertEqual((archived.version, archived.state), (2, "archived"))
        for mutation in ({"text": "bypass"}, {"state": "open"}, {"actor_id": self.actor.pk + 1}, {"consumed_command_id": 99}):
            with self.assertRaises(ValueError):
                HumanReplyPrivateDocument.objects.filter(pk=doc.pk).update(**mutation)

    def test_actor_context_delete_set_null_and_client_purge_cascades(self):
        doc = self.draft(kind="internal_note")
        self.actor.delete()
        self.source.delete()
        doc.refresh_from_db()
        self.assertIsNone(doc.actor_id)
        self.assertIsNone(doc.context_message_id)
        self.assertEqual(doc.text, "Вітаю!")
        self.customer.delete()
        self.assertFalse(HumanReplyPrivateDocument.objects.exists())
