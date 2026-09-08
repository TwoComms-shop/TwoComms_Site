import hashlib
import json

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from management.models import (
    IgClient,
    IgCustomerTurn,
    IgRevisionDeliveryEffect,
    IgTurnMessage,
    InstagramBotMessage,
)
from management.services.ig_revision_execution import (
    complete_revision_from_effects,
    due_revision_ids,
    prepare_next_due_revision,
)
from management.services.ig_turn_revisions import create_collecting_revision
from management.services.ig_turn_revisions import (
    claim_revision_preparation,
    seal_revision,
)


class RevisionExecutionTests(TransactionTestCase):
    reset_sequences = True

    def _source(self, sender, mid, text="question"):
        client, _created = IgClient.objects.get_or_create(igsid=sender)
        return InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text=text,
            mid=mid,
            status=InstagramBotMessage.Status.PENDING,
        )

    def _collecting_revision(self, sources, *, now=None):
        now = now or timezone.now()
        turn = IgCustomerTurn.objects.create(
            client=sources[0].client,
            primary_source_message=sources[0],
            window_started_at=now,
            window_deadline=now,
        )
        for ordinal, source in enumerate(sources, start=1):
            IgTurnMessage.objects.create(
                turn=turn, message=source, ordinal=ordinal, role=source.role
            )
        revision = create_collecting_revision(
            turn, sources, now=now, bypass_quiet=True
        ).revision
        return turn, revision

    def _ready_revision(self, sender="revision-completion"):
        source = self._source(sender, f"{sender}-source")
        _turn, revision = self._collecting_revision([source])
        prepared = prepare_next_due_revision(lambda **_kwargs: None)
        self.assertTrue(prepared.ready, prepared.reason)
        self.assertEqual(prepared.revision_id, revision.pk)
        revision.refresh_from_db()
        return source, revision, prepared.execution_token

    def _effect(self, revision, source, *, index, state, group="substantive_text"):
        payload = {
            "recipient": {"id": source.sender_id},
            "message": {"text": f"part {index}"},
        }
        digest = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        return IgRevisionDeliveryEffect.objects.create(
            revision=revision,
            source_message=source,
            effect_key=f"execution-effect-{revision.pk}-{index}",
            actor="bot",
            purpose="normal_reply",
            group=group,
            kind="text" if group == "substantive_text" else "fallback",
            order_index=index,
            part_index=index if group == "substantive_text" else 0,
            part_count=2 if group == "substantive_text" else 1,
            plan_digest="a" * 64,
            payload=payload,
            payload_digest=digest,
            recipient_igsid=source.sender_id,
            provider_namespace=source.provider_namespace,
            settings_id_snapshot=1,
            settings_permission_epoch=0,
            client_permission_epoch=revision.permission_epoch,
            revision_snapshot_digest=revision.snapshot_digest,
            publication_id=1,
            publication_version=1,
            publication_hash="b" * 64,
            authority_context_digest="c" * 64,
            state=state,
        )

    def test_due_preparation_calls_each_exact_source_outside_atomic_and_returns_snapshot(self):
        first = self._source("revision-prepare", "prepare-first", "caption")
        second = self._source("revision-prepare", "prepare-second", "audio")
        turn, revision = self._collecting_revision([first, second])
        calls = []

        def capture(**kwargs):
            calls.append({**kwargs, "atomic": connection.in_atomic_block})

        self.assertEqual(due_revision_ids(), [revision.pk])
        prepared = prepare_next_due_revision(capture)

        self.assertTrue(prepared.ready, prepared.reason)
        self.assertEqual(prepared.source_message_ids, (first.pk, second.pk))
        self.assertTrue(prepared.execution_token)
        self.assertEqual(
            [call["message_id"] for call in calls], [first.pk, second.pk]
        )
        self.assertTrue(all(not call["atomic"] for call in calls))
        self.assertTrue(all(call["remaining_seconds"] > 0 for call in calls))
        self.assertEqual(
            [item["message_id"] for item in prepared.snapshot["sources"]],
            [first.pk, second.pk],
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, InstagramBotMessage.Status.PENDING)
        self.assertEqual(second.status, InstagramBotMessage.Status.PENDING)
        self.assertEqual(turn.primary_source_message_id, first.pk)

    def test_due_selector_excludes_legacy_terminal_shadow(self):
        source = self._source("revision-terminal", "terminal-source")
        turn, revision = self._collecting_revision([source])
        turn.claim_state = IgCustomerTurn.ClaimState.PROCESSED
        turn.terminal_reason = IgCustomerTurn.TerminalReason.REPLIED
        turn.processed_at = timezone.now()
        turn.save(update_fields=[
            "claim_state", "terminal_reason", "processed_at", "updated_at",
        ])

        self.assertNotIn(revision.pk, due_revision_ids())
        self.assertEqual(
            prepare_next_due_revision(lambda **_kwargs: None).reason,
            "no_due_revision",
        )

    def test_sealed_after_crash_is_claimed_without_capture_or_snapshot_rewrite(self):
        source = self._source("revision-sealed", "sealed-source")
        _turn, revision = self._collecting_revision([source])
        preparation = claim_revision_preparation(revision.pk)
        sealed = seal_revision(revision.pk, preparation.token)
        self.assertTrue(sealed.sealed, sealed.reason)
        original_digest = sealed.revision.snapshot_digest
        calls = []

        self.assertIn(revision.pk, due_revision_ids())
        prepared = prepare_next_due_revision(lambda **kwargs: calls.append(kwargs))

        self.assertTrue(prepared.ready, prepared.reason)
        self.assertEqual(calls, [])
        revision.refresh_from_db()
        self.assertEqual(revision.state, revision.State.CLAIMED)
        self.assertEqual(revision.snapshot_digest, original_digest)
        self.assertEqual(
            hashlib.sha256(json.dumps(
                prepared.snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest(),
            original_digest,
        )

    def test_prepare_rejects_caller_transaction_without_capture_or_claim(self):
        source = self._source("revision-atomic", "atomic-source")
        _turn, revision = self._collecting_revision([source])
        calls = []

        with transaction.atomic():
            prepared = prepare_next_due_revision(
                lambda **kwargs: calls.append(kwargs)
            )

        self.assertEqual(prepared.reason, "caller_transaction_active")
        self.assertEqual(calls, [])
        revision.refresh_from_db()
        self.assertEqual(revision.state, revision.State.COLLECTING)
        self.assertEqual(revision.claim_token, "")

    def test_completion_is_idempotent_only_for_sent_or_cancelled_aggregate(self):
        source, revision, token = self._ready_revision()
        self._effect(
            revision,
            source,
            index=0,
            state=IgRevisionDeliveryEffect.State.SENT,
        )
        self._effect(
            revision,
            source,
            index=1,
            state=IgRevisionDeliveryEffect.State.CANCELLED,
            group="template_fallback",
        )

        completed = complete_revision_from_effects(revision.pk, token)
        repeated = complete_revision_from_effects(revision.pk, token)

        self.assertTrue(completed.completed)
        self.assertEqual(completed.reason, "delivered")
        self.assertTrue(repeated.completed)
        revision.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(revision.state, revision.State.PROCESSED)
        self.assertEqual(source.status, InstagramBotMessage.Status.PENDING)
        self.assertEqual(source.attempts, 0)

    def test_unknown_and_partial_definite_failure_remain_reconciliation_debt(self):
        source, revision, token = self._ready_revision("revision-unknown")
        self._effect(
            revision,
            source,
            index=0,
            state=IgRevisionDeliveryEffect.State.UNKNOWN,
        )
        unknown = complete_revision_from_effects(revision.pk, token)
        self.assertFalse(unknown.completed)
        self.assertTrue(unknown.reconciliation_required)
        self.assertEqual(unknown.reason, "delivery_unknown")
        revision.refresh_from_db()
        self.assertEqual(revision.state, revision.State.CLAIMED)

        second_source, second_revision, second_token = self._ready_revision(
            "revision-partial"
        )
        self._effect(
            second_revision,
            second_source,
            index=0,
            state=IgRevisionDeliveryEffect.State.SENT,
        )
        self._effect(
            second_revision,
            second_source,
            index=1,
            state=IgRevisionDeliveryEffect.State.DEFINITE_FAILED,
        )
        partial = complete_revision_from_effects(second_revision.pk, second_token)
        self.assertFalse(partial.completed)
        self.assertTrue(partial.reconciliation_required)
        self.assertEqual(partial.reason, "partial_definite_failure")
        second_revision.refresh_from_db()
        self.assertEqual(second_revision.state, second_revision.State.CLAIMED)

    def test_cancelled_owed_part_after_a_sent_part_is_not_complete(self):
        source, revision, token = self._ready_revision("revision-cancelled-part")
        self._effect(revision, source, index=0, state=IgRevisionDeliveryEffect.State.SENT)
        self._effect(revision, source, index=1, state=IgRevisionDeliveryEffect.State.CANCELLED)

        outcome = complete_revision_from_effects(revision.pk, token)

        self.assertFalse(outcome.completed)
        self.assertTrue(outcome.reconciliation_required)
        self.assertEqual(outcome.reason, "partial_cancelled_delivery")
        revision.refresh_from_db()
        self.assertEqual(revision.state, revision.State.CLAIMED)
        self.assertEqual(revision.claim_token, token)
