"""Content validation stops do not become repeated unchanged outage requests."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_recovery as recovery_fixtures
from management.models import GeminiRequest, GeminiRequestAttempt, IgCustomerTurnRevision, IgFollowUpTask, InstagramBotMessage
from management.services.ig_revision_recovery import (
    prepare_outage_recovery, schedule_revision_recovery, terminal_semantic_failure_reason,
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class SemanticRecoveryTests(TransactionTestCase):
    setUp = recovery_fixtures.RevisionRecoveryTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _admit = recovery_fixtures.RevisionRecoveryTests._admit
    _graph = recovery_fixtures.RevisionRecoveryTests._graph

    def _failure(self, *, kind="local_semantic_rejection", terminal="local_semantic_rejection", http=200):
        self._admit()
        graph = self._graph()
        graph.terminal_reason = terminal
        graph.save(update_fields=["terminal_reason", "updated_at"])
        GeminiRequestAttempt.objects.create(
            request_graph=graph, request_id=graph.request_id, role="chat", key_name="GEMINI_API",
            model="gemini-3.7-flash", attempt_index=1, candidate_index=1,
            logical_turn_id=graph.logical_turn_id, client_id=self.customer.pk, source_message_id=self.source.pk,
            lane="live", accounting_mode="shadow", outcome="failed", fsm_state="failed",
            failure_kind=kind, http_code=http, error_detail="catalog_selector_missing",
            provider_started_at=timezone.now(), finished_at=timezone.now(), dispatch_pacific_day=timezone.now().date(),
        )
        return graph

    def test_completed_semantic_rejection_parks_immediately_without_waiting_for_original_deadline(self):
        self._failure()
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            result = schedule_revision_recovery(self.revision.pk)
        provider.assert_not_called()
        self.assertEqual((result.state, result.reason), ("manual", "generation_semantic_rejected"))
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertIsNone(self.revision.recovery_due_at)
        self.assertIsNone(self.revision.lease_until)
        self.assertEqual(self.source.status, "pending")
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_existing_waiting_semantic_failure_cannot_spawn_a_later_unchanged_child(self):
        self._failure()
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(
            recovery_state="waiting", recovery_code="recovery_due",
            recovery_due_at=timezone.now() + timedelta(minutes=5),
        )
        for _ in range(3):
            result = prepare_outage_recovery(self.revision.pk)
            self.assertEqual(result.state, "manual")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_manager_answer_after_failed_selection_cancels_waiting_lineage_without_new_debt(self):
        self._failure()
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(
            recovery_state="waiting", recovery_code="recovery_due",
            recovery_due_at=timezone.now() + timedelta(minutes=5),
        )
        InstagramBotMessage.objects.create(
            client=self.customer, sender_id="owner-1", role="manager", source="human_reply",
            provider_namespace=self.source.provider_namespace, text="Відповідь менеджера на вибір клієнта",
            status="done", send_state="sent", provider_message_id="confirmed-manager-answer",
        )
        for operation in (schedule_revision_recovery, prepare_outage_recovery):
            result = operation(self.revision.pk)
            self.assertEqual(result.state, "cancelled")
            if operation is schedule_revision_recovery:
                self.assertEqual(result.reason, "recovery_manager_answered")
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.count(), 1)

    def test_real_execute_fallback_denials_immediately_create_one_manager_debt_and_never_regenerate(self):
        from management.services.ig_revision_input import RevisionInputDecision
        from management.services.ig_revision_live import execute_claimed_revision

        self._failure()
        with (
            patch("management.services.ig_revision_input.record_source_preference_fallback",
                  return_value=RevisionInputDecision(reason="fallback_current_purpose_ineligible")),
            patch("management.services.ig_revision_holding.record_technical_holding",
                  return_value=RevisionInputDecision(reason="holding_current_purpose_ineligible")),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("same failed graph must not regenerate")) as provider,
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no admitted customer reply")) as http,
        ):
            first = execute_claimed_revision(self.revision.pk, self.token, self.settings)
            execute_claimed_revision(self.revision.pk, self.token, self.settings)
        provider.assert_not_called()
        http.assert_not_called()
        self.assertIn("generation_semantic_rejected", first.reasons)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_code, "generation_semantic_rejected")
        self.assertIsNone(self.revision.recovery_due_at)
        self.assertIsNone(self.revision.lease_until)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_transport_failure_retains_bounded_outage_recovery(self):
        self._failure(kind="http_5xx", terminal="provider_outage", http=503)
        self.assertEqual(terminal_semantic_failure_reason(self.revision), "")
        result = schedule_revision_recovery(self.revision.pk, now=self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual(result.state, "waiting")
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_failed_graph_without_confirmed_actual_semantic_http_cannot_assert_this_disposition(self):
        self._admit()
        graph = self._graph()
        graph.terminal_reason = "local_semantic_rejection"
        graph.save(update_fields=["terminal_reason"])
        self.assertEqual(terminal_semantic_failure_reason(self.revision), "")

    def test_new_fact_refresh_does_not_inherit_prior_graph_semantic_stop(self):
        from management.services.ig_turn_revisions import create_refresh_successor

        self._failure()
        successor = create_refresh_successor(self.revision.pk, self.token, reason="publication_changed")
        self.assertTrue(successor.created, successor.reason)
        self.assertEqual(terminal_semantic_failure_reason(successor.revision), "")

    def test_already_spawned_unchanged_outage_child_without_own_graph_inherits_terminal_stop(self):
        from copy import deepcopy
        from management.services.ig_revision_provider_execution import provider_execution_reference
        from management.services.ig_turn_revisions import _copied_source_payloads, _create_source_rows

        self._failure()
        parent = self.revision
        parent.active_slot = None
        parent.state = "superseded"
        parent.save(update_fields=["active_slot", "state"])
        now = timezone.now()
        child = IgCustomerTurnRevision.objects.create(
            client=self.customer, turn=parent.turn, parent=parent, revision=parent.revision + 1,
            origin="outage_recovery", state="sealed", active_slot=1,
            recovery_state="waiting", recovery_code="recovery_due", recovery_due_at=now + timedelta(minutes=5),
            quiet_started_at=now, quiet_deadline=now, quiet_cap_at=now, overall_deadline=now + timedelta(seconds=45),
            source_count=parent.source_count, permission_epoch=parent.permission_epoch,
            bundle_snapshot=deepcopy(parent.bundle_snapshot), snapshot_digest=parent.snapshot_digest,
            action_receipts={
                "recovery_lineage": {"root_revision_id": parent.pk, "round": 1, "source_snapshot_digest": parent.snapshot_digest},
                "provider_execution_reference": provider_execution_reference(parent),
            },
        )
        _create_source_rows(child, _copied_source_payloads(list(parent.sources.order_by("ordinal", "id"))))
        self.assertEqual(terminal_semantic_failure_reason(child), "generation_semantic_rejected")
        outcome = prepare_outage_recovery(child.pk)
        self.assertEqual((outcome.state, outcome.reason), ("manual", "generation_semantic_rejected"))
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 2)
        self.assertEqual(GeminiRequest.objects.count(), 1)
