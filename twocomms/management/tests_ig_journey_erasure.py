from django.test import TransactionTestCase
from django.utils import timezone

from management.models import (
    IgClient, IgConversationRouteDecision, IgCustomerTurn, IgJourneyTraceSnapshot,
    IgTurnMessage, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_conversation_routes import (
    RevisionRouteSource, accept_customer_routes,
)
from management.services.ig_turn_revisions import create_collecting_revision
from management.services.ig_typed_memory import purge_client_analysis_memory
from management.services.ig_journey_trace_contract import normalize_journey_trace
from management.services.ig_journey_trace_store import record_journey_trace, JourneyTraceStoreRejected


class JourneyErasureTests(TransactionTestCase):
    reset_sequences = True

    def _trace_input(self, client):
        source = InstagramBotMessage.objects.filter(client=client).latest("id")
        by_id = {source.pk: {"message_id": source.pk, "role": source.role, "text": source.text}}
        trace = normalize_journey_trace({"schema_version": 1, "steps": [{
            "from_node": "inbound", "to_node": "information_question", "kind": "progress",
            "reason_code": "entered", "confidence": .9,
            "evidence": [{"message_id": source.pk, "quote": source.text}],
        }], "current_node": "information_question"}, by_id=by_id, watermark=source.pk)
        return dict(client_id=client.pk, episode_id=None, watermark=source.pk,
                    normalized_trace=trace, by_id=by_id, prompt_version="trace-test.v1",
                    analysis_model="test-model", analyzed_at=timezone.now())

    def test_trace_purge_is_fenced_selective_and_cannot_resurrect(self):
        target, _, _ = self._journal("e")
        foreign, _, _ = self._journal("f")
        target_input = self._trace_input(target)
        target_trace = record_journey_trace(**target_input)
        foreign_trace = record_journey_trace(**self._trace_input(foreign))
        with self.assertRaises(ValueError):
            purge_client_analysis_memory([target.pk])
        self.assertTrue(IgJourneyTraceSnapshot.objects.filter(pk=target_trace.pk).exists())
        IgClient.objects.filter(pk=target.pk).update(privacy_erasure_started_at=timezone.now())
        outcome = purge_client_analysis_memory([target.pk])
        self.assertEqual(outcome["tables"]["management_igjourneytracesnapshot"], 1)
        self.assertFalse(IgJourneyTraceSnapshot.objects.filter(pk=target_trace.pk).exists())
        self.assertTrue(IgJourneyTraceSnapshot.objects.filter(pk=foreign_trace.pk).exists())
        with self.assertRaises(JourneyTraceStoreRejected):
            record_journey_trace(**target_input)

    def _journal(self, suffix):
        client = IgClient.objects.create(igsid=f"journey-erasure-{suffix}")
        source = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            mid=f"journey-erasure-{suffix}-source",
            text="private customer intent",
            status=InstagramBotMessage.Status.PENDING,
        )
        now = timezone.now()
        turn = IgCustomerTurn.objects.create(
            client=client,
            primary_source_message=source,
            window_started_at=now,
            window_deadline=now,
        )
        IgTurnMessage.objects.create(
            turn=turn, message=source, ordinal=1, role=source.role
        )
        revision = create_collecting_revision(
            turn, [source], now=now, bypass_quiet=True
        ).revision
        decision = IgConversationRouteDecision.objects.create(
            client=client,
            revision=revision,
            decision_key=(suffix * 64)[:64],
            sequence=1,
            reset_floor=1,
            watermark_message_id=source.pk,
            input_digest="a" * 64,
            interpretation_digest="b" * 64,
            decision_digest="c" * 64,
            source_binding={"private": suffix},
            interpretation={"private": suffix},
            active_intents=[{"key": "employment:none"}],
            transitions=[{"evidence_message_ids": [source.pk]}],
            occurred_at=now,
        )
        return client, revision, decision

    def test_unfenced_client_rejects_purge_without_deleting_any_journal(self):
        target, _revision, target_decision = self._journal("a")
        foreign, _revision, foreign_decision = self._journal("b")

        with self.assertRaisesRegex(ValueError, "requires every client fence"):
            purge_client_analysis_memory([target.pk])

        self.assertTrue(
            IgConversationRouteDecision.objects.filter(pk=target_decision.pk).exists()
        )
        self.assertTrue(
            IgConversationRouteDecision.objects.filter(pk=foreign_decision.pk).exists()
        )
        self.assertIsNone(foreign.privacy_erasure_started_at)

    def test_committed_fence_purges_exact_journal_and_blocks_resurrection(self):
        target, revision, target_decision = self._journal("c")
        _foreign, _revision, foreign_decision = self._journal("d")
        fence_at = timezone.now()
        IgClient.objects.filter(pk=target.pk).update(
            privacy_erasure_started_at=fence_at,
            updated_at=fence_at,
        )

        outcome = purge_client_analysis_memory([target.pk])

        self.assertEqual(
            outcome["tables"]["management_igconversationroutedecision"], 1
        )
        self.assertFalse(
            IgConversationRouteDecision.objects.filter(pk=target_decision.pk).exists()
        )
        self.assertTrue(
            IgConversationRouteDecision.objects.filter(pk=foreign_decision.pk).exists()
        )

        settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True)
        acceptance = accept_customer_routes(
            RevisionRouteSource(
                client_id=target.pk,
                settings_id=settings_row.pk,
                revision_id=revision.pk,
                revision_token="not-authority",
                expected_source_digest="e" * 64,
            ),
            expected_previous_decision_id=None,
        )
        self.assertEqual(acceptance.reason_code, "client_erasure_changed")
        self.assertFalse(
            IgConversationRouteDecision.objects.filter(client_id=target.pk).exists()
        )
