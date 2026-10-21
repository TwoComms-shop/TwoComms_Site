"""Source-owned pre-provider preparation failures retain exact reply debt."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_reply_resilience as resilience_fixtures
from management.models import GeminiRequest, IgCommercialEpisode, IgDeal, IgFollowUpTask, IgTurnMessage
from management.services.ig_revision_input import RevisionInputDecision
from management.services.ig_revision_live import RevisionLiveResult, execute_claimed_revision, process_pending_revisions
from management.services.ig_revision_recovery import RecoveryResult, prepare_outage_recovery, schedule_revision_recovery
from management.services.ig_turn_intelligence import TurnContextError
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False,
                   IG_TURN_CONTEXT_MODE="unified")
class RetailPreparationLivenessTests(TransactionTestCase):
    reset_sequences = True
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute
    _generate = live_fixtures.RevisionLiveTests._generate
    _selected_product = resilience_fixtures.RevisionReplyResilienceTests._selected_product

    def setUp(self):
        # Reuse the publication/client/source fixture, then form the first
        # native revision only after this scenario's final sources exist.
        # Revisions are append-only on MariaDB, including fixture setup.
        with patch.object(live_fixtures, "create_collecting_revision",
            return_value=SimpleNamespace(revision=None)):
            live_fixtures.RevisionLiveTests.setUp(self)

    def test_selected_product_and_size_enter_provider_with_captured_context(self):
        product, _ = self._selected_product()
        deal = IgDeal.objects.create(client=self.customer)
        episode = IgCommercialEpisode.objects.create(client=self.customer, deal=deal, sequence=1,
            open_slot=1, materialization_key="retail-liveness:episode")
        self.customer.current_commercial_episode = episode
        self.customer.save(update_fields=["current_commercial_episode"])
        product.title = "Футболка «Локальний Принт»"
        product.save(update_fields=["title"])
        self.source.text = product.title
        self.source.save(update_fields=["text"])
        size = self._message("2XL", "selected-size")
        IgTurnMessage.objects.create(turn=self.turn, message=size, ordinal=2, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source, size], bypass_quiet=True).revision
        self._prepare()
        self.parsed = {"reply_text": "You selected size XXL. Which color would you like?", "controls": []}
        result, generate, _http = self._execute()
        self.assertEqual(generate.call_count, 1, result)

    def _english_question(self):
        self.source.text = "Is my selected shirt and size clear? Please reply in English."
        self.source.save(update_fields=["text"])
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()

    def _context_budget_failure(self):
        return patch("management.services.ig_turn_integration.prepare_revision_turn_context",
            side_effect=TurnContextError("required_context_budget_exceeded"))

    def test_required_context_failure_gets_owned_native_ack_without_provider_manifest(self):
        self._english_question()
        with (self._context_budget_failure(),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("no model dispatch")) as generate,
            patch("management.services.instagram_bot.get_page_token", return_value="local-test-token"),
            patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "context-fault-ack"}))) as http,
            patch("management.services.instagram_bot._register_outgoing_message")):
            outcome = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1, outcome)
        self.assertEqual(outcome.reasons, ("technical_holding_sent",))
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        proof = self.revision.action_receipts["policy_readiness_failure"]
        self.assertEqual(proof["code"], "required_context_budget_exceeded")
        self.assertEqual(proof["source_message_ids"], [self.source.pk])
        self.assertIs(proof["provider_dispatched"], False)
        self.assertNotIn("provider_execution_manifest", self.revision.action_receipts)
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertEqual(self.source.status, "pending")
        effect = self.revision.delivery_effects.get()
        self.assertEqual((effect.purpose, effect.state), ("technical_holding", "sent"))
        self.assertIn("recorded", effect.payload["message"]["text"])
        self.assertEqual(self.revision.action_receipts["technical_holding"]["substantive_obligation"], "open_manager_reply")

    def test_suppressed_ack_retains_exact_context_failure_and_immediate_manager_debt(self):
        self._english_question()
        with (self._context_budget_failure(),
            patch("management.services.ig_revision_holding.record_policy_fault_ack",
                return_value=RevisionInputDecision(reason="holding_continuous_wait_already_notified")),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("no model dispatch")) as generate,
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no duplicate acknowledgement")) as http):
            outcome = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        generate.assert_not_called()
        http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(outcome.reasons, ("required_context_budget_exceeded",))
        self.assertEqual((self.revision.recovery_state, self.revision.recovery_code),
            ("manual", "required_context_budget_exceeded"))
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertFalse(GeminiRequest.objects.exists())
        task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{self.revision.pk}")
        self.assertEqual(task.event_payload["reason"], "required_context_budget_exceeded")

    def test_changed_source_cannot_use_context_failure_as_send_authority(self):
        self._english_question()

        def changed_source(*args, **kwargs):
            self.source.text = "A changed source has separate authority."
            self.source.save(update_fields=["text"])
            raise TurnContextError("required_context_budget_exceeded")

        with (patch("management.services.ig_turn_integration.prepare_revision_turn_context", side_effect=changed_source),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("no model dispatch")) as generate,
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("changed source cannot send")) as http):
            outcome = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        generate.assert_not_called()
        http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(outcome.state, "blocked")
        self.assertNotIn("policy_readiness_failure", self.revision.action_receipts)
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_unexpected_zero_graph_fault_retains_only_finite_durable_diagnostic_and_debt(self):
        self._english_question()
        with (patch("management.services.ig_revision_live._execute_claimed_revision",
                side_effect=ValueError("private prompt and customer detail must never persist")),
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("crash cannot send")) as http):
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        http.assert_not_called()
        self.assertEqual(result.reasons, ("local_preprovider_fault",))
        self.revision.refresh_from_db()
        task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{self.revision.pk}")
        proof = task.manager_context["local_preprovider_fault"]
        self.assertEqual((proof["stage"], proof["exception_type"]), ("revision_execution", "ValueError"))
        self.assertNotIn("private prompt", json.dumps(proof))
        self.assertIs(proof["provider_graph_present"], False)
        self.assertEqual((self.revision.recovery_state, self.revision.recovery_code), ("manual", "local_preprovider_fault"))
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertEqual(self.revision.action_receipts["response_debt"]["task_id"], task.pk)

    def test_database_fault_is_not_reclassified_as_local_zero_graph_failure(self):
        self._english_question()
        with patch("management.services.ig_revision_live._execute_claimed_revision", side_effect=DatabaseError("db unavailable")):
            with self.assertRaises(DatabaseError):
                execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.revision.refresh_from_db()
        self.assertNotIn("local_preprovider_fault", self.revision.action_receipts)

    def test_existing_provider_graph_retains_its_owner_after_local_exception(self):
        from management import tests_ig_revision_recovery as recovery_fixtures

        self._english_question()
        graph = recovery_fixtures.RevisionRecoveryTests._graph(self)
        with patch("management.services.ig_revision_live._execute_claimed_revision", side_effect=ValueError("post-graph failure")):
            with self.assertRaises(ValueError):
                execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.revision.refresh_from_db()
        graph.refresh_from_db()
        self.assertNotIn("local_preprovider_fault", self.revision.action_receipts)
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_any_finalization_quarantine_blocks_recovery_and_execution_without_new_case(self):
        from management import tests_ig_revision_wait_policy as wait_fixtures
        from management.services.ig_revision_finalization_faults import record_finalization_proof_fault

        self._english_question()
        # The quarantine owner stores its proof in the exact existing debt
        # task; only the immutable allowed source/task link lives on revision.
        effect = wait_fixtures.RevisionWaitPolicyTests._effect(self, state="sent",
            purpose="normal_reply", group="substantive_text")
        self.assertTrue(record_finalization_proof_fault(self.revision, [effect], code="delivery_plan_proof_invalid"))
        tasks_before = IgFollowUpTask.objects.count()
        with (patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("quarantine cannot send")) as http,
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("quarantine cannot generate")) as generate):
            for result in (schedule_revision_recovery(self.revision.pk), prepare_outage_recovery(self.revision.pk)):
                self.assertEqual(result.reason, "finalization_proof_quarantined")
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        http.assert_not_called()
        generate.assert_not_called()
        self.assertEqual(result.reasons, ("finalization_proof_quarantined",))
        self.revision.refresh_from_db()
        self.assertEqual(IgFollowUpTask.objects.count(), tasks_before)

    def _worker_patches(self, stack):
        from management.services import ig_revision_recovery as recovery
        from management.services import instagram_bot as bot
        stack.enter_context(patch("management.services.ig_revision_live.process_revision_finalizations", return_value=0))
        stack.enter_context(patch("management.services.ig_revision_rollout.revision_execution_rollout",
            return_value=SimpleNamespace(enabled=True, cutover_at=timezone.now() - timedelta(days=1))))
        stack.enter_context(patch.object(recovery, "due_recovery_revision_ids", return_value=[]))
        stack.enter_context(patch.object(recovery, "due_preparation_recovery_ids", return_value=[]))
        stack.enter_context(patch("management.services.ig_revision_holding.parked_collaboration_revision_ids", return_value=[]))
        stack.enter_context(patch("management.services.ig_revision_execution.due_revision_ids", return_value=[self.revision.pk]))
        stack.enter_context(patch.object(bot, "maintenance_status", return_value={"active": False}))
        stack.enter_context(patch.object(bot, "acquire_client_automation_lease", return_value=(self.customer, "worker-lease")))
        stack.enter_context(patch.object(bot, "release_client_automation_lease"))
        stack.enter_context(patch("management.services.ig_revision_live._reclaim_execution", return_value=(self.revision, self.token)))
        return stack.enter_context(patch("management.services.ig_revision_live.execute_claimed_revision",
            return_value=RevisionLiveResult(self.revision.pk, "completed")))

    def test_one_expiry_bookkeeping_exception_does_not_hide_fresh_customer_execution(self):
        from contextlib import ExitStack

        self._english_question()
        with ExitStack() as stack:
            execute = self._worker_patches(stack)
            stack.enter_context(patch("management.services.ig_revision_execution.expired_revision_debt_ids", return_value=[901, 902]))
            scheduled = stack.enter_context(patch("management.services.ig_revision_recovery.schedule_revision_recovery",
                side_effect=[ValueError("one malformed historical item"), RecoveryResult(902, "cancelled", "old")]))
            self.assertEqual(process_pending_revisions(self.settings), 1)
        self.assertEqual(scheduled.call_count, 2)
        execute.assert_called_once()

    def test_one_recovery_classification_exception_does_not_hide_fresh_customer_execution(self):
        from contextlib import ExitStack

        self._english_question()
        with ExitStack() as stack:
            execute = self._worker_patches(stack)
            stack.enter_context(patch("management.services.ig_revision_execution.expired_revision_debt_ids", return_value=[]))
            # First discovery is the classification pass; later discovery is
            # the execution queue, which must not retry this malformed item.
            stack.enter_context(patch("management.services.ig_revision_recovery.due_recovery_revision_ids", side_effect=[[901, 902], []]))
            classified = stack.enter_context(patch("management.services.ig_revision_recovery.prepare_outage_recovery",
                side_effect=[ValueError("one malformed historical item"), RecoveryResult(902, "cancelled", "old")]))
            self.assertEqual(process_pending_revisions(self.settings), 1)
        self.assertEqual(classified.call_count, 2)
        execute.assert_called_once()

    def test_database_error_in_expiry_classification_is_not_swallowed(self):
        from contextlib import ExitStack

        self._english_question()
        with ExitStack() as stack:
            execute = self._worker_patches(stack)
            stack.enter_context(patch("management.services.ig_revision_execution.expired_revision_debt_ids", return_value=[901]))
            stack.enter_context(patch("management.services.ig_revision_recovery.schedule_revision_recovery", side_effect=DatabaseError("db unavailable")))
            with self.assertRaises(DatabaseError):
                process_pending_revisions(self.settings)
        execute.assert_not_called()
