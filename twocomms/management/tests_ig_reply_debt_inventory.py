import hashlib
import json
from collections import Counter
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgClient,
    IgCustomerTurn,
    IgCustomerTurnRevision,
    IgFollowUpTask,
    IgRevisionDeliveryEffect,
    IgTurnRevisionSource,
    InstagramBotMessage,
)
from management.services.ig_reply_debt_inventory import inventory_reply_debt


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ReplyDebtInventoryTests(TestCase):
    def _source(
        self, client, *, source_id=None, send_state="", planned=0, delivered=0,
        failure="", provider_ids=None, text="",
    ):
        values = {
            "client": client,
            "sender_id": client.igsid,
            "provider_namespace": "fixture-account",
            "role": InstagramBotMessage.Role.USER,
            "status": InstagramBotMessage.Status.PENDING,
            "text": text,
            "send_state": send_state,
            "delivery_planned_chunk_count": planned,
            "delivery_delivered_chunk_count": delivered,
            "delivery_failure_boundary": failure,
            "delivery_provider_message_ids": provider_ids or [],
        }
        if source_id is not None:
            values["id"] = source_id
        return InstagramBotMessage.objects.create(**values)

    def _revision(
        self, client, source, *, revision_number, parent=None, active_slot=1,
        turn=None, timeline=None, source_count=1,
    ):
        now = timezone.now()
        if turn is None:
            turn = IgCustomerTurn.objects.create(
                client=client,
                primary_source_message=source,
                window_started_at=now,
                window_deadline=now + timedelta(minutes=1),
            )
        timeline = timeline or {
            "quiet_started_at": now,
            "quiet_deadline": now + timedelta(seconds=1),
            "quiet_cap_at": now + timedelta(seconds=2),
            "overall_deadline": now + timedelta(minutes=1),
        }
        return IgCustomerTurnRevision.objects.create(
            client=client,
            turn=turn,
            parent=parent,
            revision=revision_number,
            active_slot=active_slot,
            quiet_started_at=timeline["quiet_started_at"],
            quiet_deadline=timeline["quiet_deadline"],
            quiet_cap_at=timeline["quiet_cap_at"],
            overall_deadline=timeline["overall_deadline"],
            source_count=source_count,
        )

    def _revision_source(self, revision, source, *, ordinal=1):
        return IgTurnRevisionSource.objects.create(
            revision=revision,
            message=source,
            ordinal=ordinal,
            role="user",
            source_namespace="fixture-account",
            source_digest="a" * 64,
        )

    def _canonical_revision(self, client, sources, *, snapshot_valid=True):
        revision = self._revision(
            client, sources[0], revision_number=1, source_count=len(sources)
        )
        source_rows = [
            self._revision_source(revision, source, ordinal=ordinal)
            for ordinal, source in enumerate(sources, start=1)
        ]
        snapshot = {
            "sources": [
                {
                    "message_id": row.message_id,
                    **({
                        "source_digest": row.source_digest,
                        "media_parts": [],
                    } if snapshot_valid else {}),
                }
                for row in source_rows
            ]
        }
        revision.bundle_snapshot = snapshot
        revision.snapshot_digest = _digest(snapshot)
        revision.save(update_fields=["bundle_snapshot", "snapshot_digest"])
        return revision

    def _effects(
        self, revision, source, states, *, purpose="normal_reply",
        missing_receipt=False,
    ):
        rows = []
        for index, state in enumerate(states):
            payload = {
                "recipient": {"id": source.sender_id},
                "message": {"text": f"fixture-{index}"},
            }
            sent = state == IgRevisionDeliveryEffect.State.SENT
            rows.append(IgRevisionDeliveryEffect.objects.create(
                revision=revision,
                source_message=source,
                effect_key=f"inventory:{revision.pk}:{index}",
                actor="bot",
                purpose=purpose,
                group="substantive_text",
                kind="text",
                order_index=index,
                part_index=index,
                part_count=len(states),
                plan_digest="b" * 64,
                payload=payload,
                payload_digest=_digest(payload),
                recipient_igsid=source.sender_id,
                provider_namespace=source.provider_namespace,
                settings_id_snapshot=1,
                settings_permission_epoch=0,
                client_permission_epoch=revision.permission_epoch,
                revision_snapshot_digest=revision.snapshot_digest,
                publication_id=1,
                publication_version=1,
                publication_hash="c" * 64,
                authority_context_digest="d" * 64,
                state=state,
                provider_message_id=(
                    "" if missing_receipt or not sent else f"inventory-mid-{revision.pk}-{index}"
                ),
                terminal_at=(None if missing_receipt or not sent else timezone.now()),
            ))
        return rows

    def _transfer(self, client, source, *, broken=False):
        predecessor = self._revision(client, source, revision_number=1, active_slot=None)
        previous_source = self._revision_source(predecessor, source)
        successor = self._revision(
            client,
            source,
            revision_number=2,
            parent=predecessor,
            turn=predecessor.turn,
            timeline={
                "quiet_started_at": predecessor.quiet_started_at,
                "quiet_deadline": predecessor.quiet_deadline,
                "quiet_cap_at": predecessor.quiet_cap_at,
                "overall_deadline": predecessor.overall_deadline,
            },
        )
        self._revision_source(successor, source)
        receipt = {
            "version": "revision-source-transfer-v1",
            "outcome": "transferred",
            "client_id": client.pk,
            "root_revision_id": predecessor.pk,
            "predecessor_revision_id": predecessor.pk,
            "successor_revision_id": successor.pk,
            "permission_epoch": predecessor.permission_epoch,
            "source_message_ids": [source.pk],
            "source_refs": [{
                "message_id": source.pk,
                "source_digest": previous_source.source_digest,
                "predecessor_source_id": previous_source.pk,
            }],
            "successor_source_message_ids": [source.pk],
            "first_unanswered_at": predecessor.quiet_started_at.isoformat(),
            "quiet_cap_at": predecessor.quiet_cap_at.isoformat(),
            "overall_deadline": predecessor.overall_deadline.isoformat(),
        }
        receipt["digest"] = _digest(receipt)
        predecessor.action_receipts = {"source_transfer_out": receipt}
        successor_receipt = receipt
        if broken:
            successor_receipt = {**receipt, "outcome": "broken"}
        successor.action_receipts = {"source_transfer_in": successor_receipt}
        predecessor.save(update_fields=["action_receipts"])
        successor.save(update_fields=["action_receipts"])
        return predecessor, successor

    def test_source_2948_is_manual_and_inventory_never_writes(self):
        client = IgClient.objects.create(igsid="inventory-2948")
        self._source(client, source_id=2948)
        before = Counter({
            "messages": InstagramBotMessage.objects.count(),
            "revisions": IgCustomerTurnRevision.objects.count(),
        })
        with CaptureQueriesContext(connection) as queries:
            report = inventory_reply_debt(source_message_ids=[2948])
        self.assertEqual(report["items"][0]["classification"], "manual")
        self.assertEqual(report["items"][0]["evidence"]["reason"], "exact_source_unresolved")
        self.assertEqual(before["messages"], InstagramBotMessage.objects.count())
        self.assertEqual(before["revisions"], IgCustomerTurnRevision.objects.count())
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries.captured_queries))

    def test_complete_delivery_covers_but_unknown_delivery_stays_manual(self):
        client = IgClient.objects.create(igsid="inventory-delivery")
        complete = self._source(
            client,
            send_state="sent",
            planned=1,
            delivered=1,
            provider_ids=["PROVIDER-ID-MUST-NOT-LEAK"],
            text="CUSTOMER-TEXT-MUST-NOT-LEAK",
        )
        unknown = self._source(client, send_state="unknown", planned=1, delivered=0)
        report = inventory_reply_debt(source_message_ids=[unknown.pk, complete.pk])
        by_id = {item["source_message_id"]: item for item in report["items"]}
        self.assertEqual(by_id[complete.pk]["classification"], "coverage")
        self.assertEqual(by_id[unknown.pk]["classification"], "manual")
        self.assertEqual(by_id[unknown.pk]["evidence"]["reason"], "delivery_uncertain")
        encoded = json.dumps(report)
        self.assertNotIn("CUSTOMER-TEXT-MUST-NOT-LEAK", encoded)
        self.assertNotIn("PROVIDER-ID-MUST-NOT-LEAK", encoded)

    def test_canonical_whole_sent_revision_covers_every_exact_source(self):
        client = IgClient.objects.create(igsid="inventory-multi-source")
        first = self._source(client)
        second = self._source(client)
        revision = self._canonical_revision(client, [first, second])
        # A physical reply has one anchor, but its immutable bundle covers both
        # source messages in the logical customer turn.
        self._effects(revision, first, [IgRevisionDeliveryEffect.State.SENT])
        with CaptureQueriesContext(connection) as queries:
            report = inventory_reply_debt(source_message_ids=[second.pk, first.pk])
        second_only = inventory_reply_debt(source_message_ids=[second.pk])
        self.assertEqual([item["classification"] for item in report["items"]], ["coverage", "coverage"])
        self.assertEqual(second_only["items"][0]["classification"], "coverage")
        self.assertTrue(all(
            item["evidence"]["reason"] == "canonical_revision_delivery_complete"
            for item in report["items"]
        ))
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for query in queries.captured_queries
        ))

    def test_canonical_sent_without_provider_terminal_proof_is_unsupported(self):
        client = IgClient.objects.create(igsid="inventory-missing-receipt")
        source = self._source(client)
        revision = self._canonical_revision(client, [source])
        self._effects(
            revision,
            source,
            [IgRevisionDeliveryEffect.State.SENT],
            missing_receipt=True,
        )
        item = inventory_reply_debt(source_message_ids=[source.pk])["items"][0]
        self.assertEqual(item["classification"], "unsupported")
        self.assertEqual(item["evidence"]["reason"], "delivery_receipt_proof_invalid")

    def test_sibling_unknown_blocks_coverage_and_technical_holding_is_manual(self):
        client = IgClient.objects.create(igsid="inventory-uncertain-sibling")
        source = self._source(client)
        revision = self._canonical_revision(client, [source])
        self._effects(
            revision,
            source,
            [IgRevisionDeliveryEffect.State.SENT, IgRevisionDeliveryEffect.State.UNKNOWN],
        )
        uncertain = inventory_reply_debt(source_message_ids=[source.pk])["items"][0]
        self.assertEqual(uncertain["classification"], "manual")
        self.assertEqual(uncertain["evidence"]["reason"], "delivery_unknown")

        partial_client = IgClient.objects.create(igsid="inventory-partial")
        partial_source = self._source(partial_client)
        partial_revision = self._canonical_revision(partial_client, [partial_source])
        self._effects(
            partial_revision,
            partial_source,
            [
                IgRevisionDeliveryEffect.State.SENT,
                IgRevisionDeliveryEffect.State.DEFINITE_FAILED,
            ],
        )
        partial = inventory_reply_debt(source_message_ids=[partial_source.pk])["items"][0]
        self.assertEqual(partial["classification"], "manual")
        self.assertEqual(partial["evidence"]["reason"], "partial_definite_failure")

        holding_client = IgClient.objects.create(igsid="inventory-holding")
        holding_source = self._source(holding_client)
        holding_revision = self._canonical_revision(holding_client, [holding_source])
        self._effects(
            holding_revision,
            holding_source,
            [IgRevisionDeliveryEffect.State.SENT],
            purpose="technical_holding",
        )
        holding = inventory_reply_debt(source_message_ids=[holding_source.pk])["items"][0]
        self.assertEqual(holding["classification"], "manual")
        self.assertEqual(holding["evidence"]["reason"], "technical_holding_sent")

    def test_malformed_snapshot_cannot_be_coverage(self):
        client = IgClient.objects.create(igsid="inventory-bad-snapshot")
        source = self._source(client)
        revision = self._canonical_revision(client, [source], snapshot_valid=False)
        self._effects(revision, source, [IgRevisionDeliveryEffect.State.SENT])
        item = inventory_reply_debt(source_message_ids=[source.pk])["items"][0]
        self.assertEqual(item["classification"], "unsupported")
        self.assertEqual(item["evidence"]["reason"], "revision_source_snapshot_invalid")

    def test_paired_transfer_requires_a_valid_mirrored_receipt(self):
        client = IgClient.objects.create(igsid="inventory-transfer")
        source = self._source(client)
        self._transfer(client, source)
        valid = inventory_reply_debt(source_message_ids=[source.pk])
        self.assertEqual(valid["items"][0]["classification"], "transfer")
        broken_client = IgClient.objects.create(igsid="inventory-transfer-broken")
        broken_source = self._source(broken_client)
        self._transfer(broken_client, broken_source, broken=True)
        broken = inventory_reply_debt(source_message_ids=[broken_source.pk])
        self.assertEqual(broken["items"][0]["classification"], "unsupported")
        self.assertEqual(broken["items"][0]["evidence"]["reason"], "source_transfer_malformed")

    def test_cross_client_revision_linkage_is_unsupported(self):
        source_client = IgClient.objects.create(igsid="inventory-source-owner")
        revision_client = IgClient.objects.create(igsid="inventory-other-owner")
        source = self._source(source_client)
        revision = self._revision(revision_client, source, revision_number=1)
        self._revision_source(revision, source)
        item = inventory_reply_debt(source_message_ids=[source.pk])["items"][0]
        self.assertEqual(item["classification"], "unsupported")
        self.assertEqual(item["evidence"]["reason"], "source_cross_client")

    def test_missing_task_96_is_stably_unsupported_and_ids_are_deduplicated_sorted(self):
        client = IgClient.objects.create(igsid="inventory-order")
        self._source(client, source_id=2948)
        first = inventory_reply_debt(source_message_ids=[2948, 9, 2948], task_ids=[96, 3, 96])
        second = inventory_reply_debt(source_message_ids=[9, 2948], task_ids=[3, 96])
        self.assertEqual(first, second)
        self.assertEqual(first["requested"], {"source_message_ids": [9, 2948], "task_ids": [3, 96]})
        self.assertEqual(
            [(item["target_type"], item.get("source_message_id", item.get("task_id"))) for item in first["items"]],
            [("source_message", 9), ("source_message", 2948), ("task", 3), ("task", 96)],
        )
        missing = next(item for item in first["items"] if item.get("task_id") == 96)
        self.assertEqual(missing["classification"], "unsupported")
        self.assertEqual(missing["evidence"], {"reason": "task_missing"})

    def test_legacy_and_owned_task_linkage_preserve_status_and_review_state(self):
        client = IgClient.objects.create(igsid="inventory-task-linkage")
        source = self._source(client)
        revision = self._revision(client, source, revision_number=1)
        self._revision_source(revision, source)
        base = {
            "client": client,
            "due_at": timezone.now(),
            "kind": IgFollowUpTask.Kind.MANAGER_TASK,
            "reason": "revision_case:execution_debt",
            "status": IgFollowUpTask.Status.SKIPPED,
            "event_key": f"ig-revision-debt:{revision.pk}",
            "event_payload": {
                "revision_id": revision.pk,
                "source_message_ids": [source.pk],
                "effect_ids": [],
            },
        }
        legacy = IgFollowUpTask.objects.create(
            id=90,
            manager_context={
                "case_kind": "revision_execution_debt",
                "revision_id": revision.pk,
                "automatic_http_retry": False,
            },
            **base,
        )
        malformed_client = IgClient.objects.create(igsid="inventory-task-malformed")
        malformed_source = self._source(malformed_client)
        malformed_revision = self._revision(
            malformed_client, malformed_source, revision_number=1
        )
        self._revision_source(malformed_revision, malformed_source)
        malformed = IgFollowUpTask.objects.create(
            id=91,
            client=client,
            due_at=timezone.now(),
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="revision_case:execution_debt",
            status=IgFollowUpTask.Status.SKIPPED,
            event_key=f"ig-revision-debt:{malformed_revision.pk}",
            event_payload={
                "revision_id": malformed_revision.pk,
                "source_message_ids": [malformed_source.pk],
                "effect_ids": [],
            },
            manager_context={
                "case_kind": "revision_execution_debt",
                "revision_id": malformed_revision.pk,
                "owner": "manager",
                "automatic_http_retry": False,
            },
        )
        owned_client = IgClient.objects.create(igsid="inventory-task-owned")
        owned_source = self._source(owned_client)
        owned_revision = self._revision(owned_client, owned_source, revision_number=1)
        self._revision_source(owned_revision, owned_source)
        owned = IgFollowUpTask.objects.create(
            id=100,
            client=owned_client,
            due_at=timezone.now(),
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="revision_case:execution_debt",
            status=IgFollowUpTask.Status.CANCELLED,
            event_key=f"ig-revision-debt:{owned_revision.pk}",
            event_payload={
                "revision_id": owned_revision.pk,
                "source_message_ids": [owned_source.pk],
                "effect_ids": [],
            },
            manager_context={
                "case_kind": "revision_execution_debt",
                "revision_id": owned_revision.pk,
                "owner": "manager",
                "automatic_http_retry": False,
                "operator_review": {"outcome": "reviewed_no_reply"},
            },
        )
        report = inventory_reply_debt(task_ids=[legacy.pk, malformed.pk, owned.pk])
        by_id = {item["task_id"]: item for item in report["items"]}
        self.assertEqual(by_id[90]["evidence"]["reason"], "legacy_owner_unverified")
        self.assertEqual(by_id[90]["evidence"]["task_status"], "skipped")
        self.assertFalse(by_id[90]["evidence"]["operator_review_present"])
        self.assertEqual(by_id[91]["classification"], "unsupported")
        self.assertEqual(by_id[91]["evidence"]["reason"], "task_linkage_malformed")
        self.assertEqual(by_id[100]["classification"], "manual")
        self.assertEqual(by_id[100]["evidence"]["reason"], "task_source_manual_review")
        self.assertEqual(by_id[100]["evidence"]["task_status"], "cancelled")
        self.assertTrue(by_id[100]["evidence"]["operator_review_present"])

    def test_command_rejects_mutating_flags_and_emits_only_json(self):
        with self.assertRaises(CommandError):
            call_command("inventory_ig_reply_debt", "--source-message-id", "1", "--apply")
        with self.assertRaises(CommandError):
            call_command("inventory_ig_reply_debt", "--source-message-id", "0")
