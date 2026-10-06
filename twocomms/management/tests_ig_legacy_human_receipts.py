"""Exact legacy receipt proof; no sender, echo backfill or takeover fixtures."""
from copy import deepcopy
from datetime import timedelta
import hashlib
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, DatabaseError
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.ig_bot_models import HumanReplyCommand
from management.ig_human_reply_models import HumanReplyPart, human_payload_digest
from management.models import IgClient, InstagramBotMessage
from management.services.ig_legacy_human_receipts import (
    find_legacy_human_receipt, legacy_human_wait_reason, legacy_projection_matches, uses_legacy_human_receipt_scope,
)


class LegacyHumanProofPureTests(SimpleTestCase):
    def setUp(self):
        self.scope = dict(client_id=1, namespace="instagram_login:owner-1", recipient="customer-1", mid="legacy-4")
        self.command = SimpleNamespace(pk=7, client_id=1, recipient_igsid="customer-1",
            provider_namespace="instagram_login:owner-1", purpose="human_reply", state="sent",
            operation_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            provider_message_ids=["legacy-1", "legacy-2", "legacy-3", "legacy-4"],
            provider_started_at=timezone.now(), terminal_at=timezone.now(), failure_code="",
            reply_message_id=8, operation_context={}, text="Actual retained command text",
            draft_hash=hashlib.sha256(b"Actual retained command text").hexdigest())
        self.message = SimpleNamespace(pk=8, client_id=1, sender_id="customer-1", provider_namespace="",
            role="manager", source="human_reply", status="done", send_state="sent",
            send_idempotency_key=f"human:{self.command.operation_id}", text=self.command.text,
            provider_message_id="legacy-1", delivery_provider_message_ids=list(self.command.provider_message_ids),
            delivery_failure_boundary="", delivery_planned_chunk_count=0, delivery_delivered_chunk_count=0)

    def test_every_actual_mid_can_prove_same_whole_transcript_without_chunk_invention(self):
        for mid in self.command.provider_message_ids:
            self.assertTrue(legacy_projection_matches(self.command, self.message, **{**self.scope, "mid": mid}))
        self.message.provider_namespace = self.scope["namespace"]
        self.assertTrue(legacy_projection_matches(self.command, self.message, **self.scope))

    def test_scope_receipt_and_operation_variants_fail_closed(self):
        for field, value in (("client_id", 9), ("recipient_igsid", "other"), ("provider_namespace", "legacy_page:owner-1"),
            ("state", "unknown"), ("provider_started_at", None), ("terminal_at", None), ("purpose", "bot"),
            ("failure_code", "degraded"), ("reply_message_id", 9), ("operation_id", uuid.UUID(int=0)),
            ("provider_message_ids", []), ("provider_message_ids", ["legacy-4", "legacy-4"]),
            ("provider_message_ids", ["legacy-4", "bad mid"]), ("provider_message_ids", ["legacy-4"] * 5),
            ("operation_context", {"human_delivery": None}), ("draft_hash", "0" * 64)):
            with self.subTest(field=field, value=value):
                command = deepcopy(self.command); setattr(command, field, value)
                self.assertFalse(legacy_projection_matches(command, self.message, **self.scope))
        for scope in ({"mid": "not-a-receipt"}, {"mid": " legacy-4"}, {"client_id": True}, {"client_id": "01"},
            {"namespace": "instagram_login:other-owner"}, {"recipient": "other-customer"}):
            self.assertFalse(legacy_projection_matches(self.command, self.message, **{**self.scope, **scope}))

    def test_operator_marked_sent_or_resembling_text_is_never_a_receipt(self):
        command = deepcopy(self.command); command.provider_message_ids = []
        self.assertFalse(legacy_projection_matches(command, self.message, **self.scope))
        for field, value in (("text", "Degraded actual send text"), ("role", "model"), ("source", "echo"),
            ("provider_namespace", "instagram_login:other-owner"), ("send_idempotency_key", "human:other-operation"),
            ("status", "failed"), ("send_state", "unknown"), ("provider_message_id", "legacy-4"),
            ("delivery_provider_message_ids", list(reversed(self.command.provider_message_ids))),
            ("delivery_failure_boundary", "delivery_unknown")):
            message = deepcopy(self.message); setattr(message, field, value)
            self.assertFalse(legacy_projection_matches(self.command, message, **self.scope))


class LegacyHumanReceiptReadTests(TestCase):
    def setUp(self):
        self.namespace = "instagram_login:owner-1"
        self.customer = IgClient.objects.create(igsid="legacy-human-customer")
        self.actor = get_user_model().objects.create_user(username="legacy-human-actor")
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", text="Synthetic previous customer source", mid="legacy-human-source")
        self.command, self.message = self.receipt()

    def receipt(self, *, customer=None, namespace=None, ids=None, text="Synthetic retained human reply"):
        customer = customer or self.customer; namespace = namespace or self.namespace
        ids = ["legacy-mid-1", "legacy-mid-2", "legacy-mid-3", "legacy-mid-4"] if ids is None else ids
        operation = uuid.uuid4(); now = timezone.now()
        message = InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid,
            provider_namespace="", role="manager", source="human_reply", text=text, status="done", send_state="sent",
            send_idempotency_key=f"human:{operation}", provider_message_id=ids[0] if ids else "",
            delivery_provider_message_ids=ids)
        command = HumanReplyCommand.objects.create(client=customer, actor=self.actor, context_message=self.source,
            operation_id=operation, recipient_igsid=customer.igsid, provider_namespace=namespace,
            text=text, draft_hash=hashlib.sha256(text.encode()).hexdigest(), operation_context={},
            reply_message=message, state="sent", provider_started_at=now - timedelta(days=200), terminal_at=now - timedelta(days=200),
            provider_message_ids=ids)
        return command, message

    def find(self, mid="legacy-mid-4", **overrides):
        return find_legacy_human_receipt(**{**dict(client_id=self.customer.pk, namespace=self.namespace,
            recipient=self.customer.igsid, mid=mid), **overrides})

    def part(self, *, command=None, mid="part-only", namespace=None):
        command = command or self.command
        payload = {"recipient": {"id": command.recipient_igsid}, "message": {"text": command.text}}
        return HumanReplyPart.objects.create(command=command, client_id=command.client_id,
            operation_id_snapshot=command.operation_id, actor_id_snapshot=self.actor.pk,
            context_message_id_snapshot=self.source.pk, permission_epoch=0, recipient_igsid=command.recipient_igsid,
            provider_namespace=namespace or command.provider_namespace, ordinal=0, part_count=1,
            plan_digest="a" * 64, payload=payload, payload_digest=human_payload_digest(payload),
            state="sent", provider_message_id=mid, provider_started_at=timezone.now())

    def test_four_mids_read_only_same_binding_and_no_new_part_transcript_or_takeover(self):
        old_client = IgClient.objects.values().get(pk=self.customer.pk)
        old_command = HumanReplyCommand.objects.values().get(pk=self.command.pk)
        old_message = InstagramBotMessage.objects.values().get(pk=self.message.pk)
        with CaptureQueriesContext(connection) as queries, patch("management.models.InstagramBotSettings.load") as bootstrap, patch(
            "management.services.ig_human_reply.dispatch_human_reply_command") as dispatch, patch(
            "management.services.instagram_bot.send_text") as send:
            for mid in self.command.provider_message_ids:
                proof = self.find(mid)
                self.assertTrue(proof.accepted, proof.reason)
                self.assertEqual((proof.command_id, proof.manager_message_id, proof.operation_id),
                    (self.command.pk, self.message.pk, str(self.command.operation_id)))
            self.assertTrue(uses_legacy_human_receipt_scope(self.namespace, self.customer.igsid))
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        for producer in (bootstrap, dispatch, send): producer.assert_not_called()
        self.assertFalse(HumanReplyPart.objects.exists())
        self.assertEqual(InstagramBotMessage.objects.filter(role="manager").count(), 1)
        self.assertEqual(old_client, IgClient.objects.values().get(pk=self.customer.pk))
        self.assertEqual(old_command, HumanReplyCommand.objects.values().get(pk=self.command.pk))
        self.assertEqual(old_message, InstagramBotMessage.objects.values().get(pk=self.message.pk))

    def test_history_replay_requires_original_command_and_transcript_ids(self):
        self.assertTrue(self.find(command_id=self.command.pk, manager_message_id=self.message.pk).accepted)
        self.assertEqual(self.find(command_id=self.command.pk + 1).classification, "ambiguous")
        self.assertEqual(self.find(manager_message_id=self.message.pk + 1).classification, "ambiguous")
        self.assertEqual(self.find(command_id="01").classification, "blocked")

    def test_foreign_duplicate_and_duplicate_own_command_defer_without_first_row_wins(self):
        foreign = IgClient.objects.create(igsid="foreign-legacy-customer")
        self.assertEqual(self.find(client_id=foreign.pk, recipient=foreign.igsid).reason, "legacy_human_receipt_foreign")
        self.assertTrue(uses_legacy_human_receipt_scope(self.namespace, foreign.igsid, mid="legacy-mid-4"))
        second, _ = self.receipt(customer=foreign)
        self.assertEqual(self.find().reason, "legacy_human_receipt_conflict")
        second.delete()
        self.receipt()
        self.assertEqual(self.find(command_id=self.command.pk).reason, "legacy_human_receipt_conflict")

    def test_namespace_rollover_and_missing_receipt_cannot_fall_back_to_text(self):
        self.assertFalse(self.find(namespace="instagram_login:other-owner").accepted)
        self.assertFalse(uses_legacy_human_receipt_scope("instagram_login:other-owner", self.customer.igsid))
        self.assertFalse(self.find("missing-but-text-similar").accepted)
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(provider_message_ids=[])
        self.assertFalse(self.find("legacy-mid-1").accepted)

    def test_degraded_transcript_or_malformed_receipt_list_stays_ambiguous(self):
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(text="Actually degraded sender text")
        self.assertEqual(self.find().reason, "legacy_human_projection_mismatch")
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(text=self.command.text)
        for ids in (["legacy-mid-4", "legacy-mid-4"], ["legacy-mid-4"] * 5, ["legacy-mid-4", "bad mid"]):
            HumanReplyCommand.objects.filter(pk=self.command.pk).update(provider_message_ids=ids)
            self.assertEqual(self.find().reason, "legacy_human_projection_mismatch")

    def test_operator_state_assertion_without_matching_checkpoint_never_proves_send(self):
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="unknown")
        self.assertEqual(self.find().classification, "ambiguous")
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="sent")
        InstagramBotMessage.objects.filter(pk=self.message.pk).update(delivery_provider_message_ids=[])
        self.assertEqual(self.find().classification, "ambiguous")

    def test_actor_deletion_old_epoch_and_source_reset_do_not_invalidate_known_history(self):
        self.actor.delete()
        self.source.delete()
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=200, bot_paused=True, manager_takeover=True)
        self.assertTrue(self.find().accepted)

    def test_hidden_erased_deleted_client_or_deleted_transcript_export_no_receipt(self):
        for field in ("hidden_at", "privacy_erasure_started_at"):
            IgClient.objects.filter(pk=self.customer.pk).update(**{field: timezone.now()})
            proof = self.find(); self.assertEqual(proof.reason, "legacy_human_client_unavailable")
            self.assertEqual((proof.command_id, proof.manager_message_id, proof.operation_id), (0, 0, ""))
            IgClient.objects.filter(pk=self.customer.pk).update(**{field: None})
        self.message.delete()
        self.assertEqual(self.find().classification, "ambiguous")
        self.customer.delete()
        self.assertEqual(self.find().classification, "blocked")

    def test_fence_committed_between_initial_owner_read_and_candidate_lookup_blocks_final_proof(self):
        from management.services import ig_legacy_human_receipts as finder
        original = finder._legacy_commands
        def fenced(namespace):
            IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
            return original(namespace)
        with patch.object(finder, "_legacy_commands", side_effect=fenced):
            self.assertEqual(self.find().reason, "legacy_human_client_unavailable")

    def test_new_plan_key_even_malformed_and_actual_parts_are_never_legacy(self):
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(operation_context={"human_delivery": None})
        self.assertFalse(uses_legacy_human_receipt_scope(self.namespace, self.customer.igsid))
        self.assertFalse(self.find().accepted)
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(operation_context={})
        self.part()
        self.assertFalse(uses_legacy_human_receipt_scope(self.namespace, self.customer.igsid))
        self.assertFalse(self.find().accepted)

    def test_other_part_or_revision_effect_with_same_namespace_mid_conflicts(self):
        second, _ = self.receipt(ids=["other-command-mid"])
        self.part(command=second, mid="legacy-mid-4")
        self.assertEqual(self.find().reason, "legacy_human_cross_lane_conflict")
        HumanReplyPart.objects.filter(command=second).delete()
        with patch("management.models.IgRevisionDeliveryEffect.objects.filter") as effects:
            effects.return_value.exists.return_value = True
            self.assertEqual(self.find().reason, "legacy_human_cross_lane_conflict")
            effects.assert_called_once_with(provider_namespace=self.namespace, provider_message_id="legacy-mid-4")

    def test_lookup_rows_are_bounded_and_db_failure_does_not_grant_own(self):
        for _ in range(4): self.receipt()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.find().classification, "ambiguous")
        command_queries = [row["sql"] for row in queries if "FROM \"management_humanreplycommand\"" in row["sql"] or "FROM `management_humanreplycommand`" in row["sql"]]
        self.assertEqual(len(command_queries), 1)
        self.assertIn("LIMIT 3", command_queries[0].upper())
        with patch("management.services.ig_legacy_human_receipts._legacy_commands", side_effect=DatabaseError("private raw error")):
            proof = self.find()
            self.assertEqual(proof.reason, "legacy_human_receipt_unavailable")
            self.assertNotIn("private", repr(proof))
            self.assertTrue(uses_legacy_human_receipt_scope(self.namespace, self.customer.igsid))

    def test_inflight_unknown_wait_reason_is_exact_scoped_and_never_an_ownership_proof(self):
        scope = dict(client_id=self.customer.pk, namespace=self.namespace, recipient=self.customer.igsid)
        self.assertEqual(legacy_human_wait_reason(**scope), "")
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="provider_started", provider_message_ids=[])
        self.assertEqual(legacy_human_wait_reason(**scope), "legacy_human_waiting_receipt")
        self.assertFalse(self.find("unrecorded-mid").accepted)
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(state="unknown")
        self.assertEqual(legacy_human_wait_reason(**scope), "legacy_human_provider_result_unknown")
        self.assertEqual(legacy_human_wait_reason(**{**scope, "namespace": "instagram_login:other-owner"}), "")
        self.assertEqual(legacy_human_wait_reason(**{**scope, "recipient": "other-customer"}), "")
        HumanReplyCommand.objects.filter(pk=self.command.pk).update(operation_context={"human_delivery": None})
        self.assertEqual(legacy_human_wait_reason(**scope), "")
