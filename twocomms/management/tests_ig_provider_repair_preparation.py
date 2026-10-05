"""Client352 repair eligibility and local semantic failure regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from threading import Barrier
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import connection, connections
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.models import GeminiRequest, GeminiRequestAttempt
from management.services import call_ai_analysis as ai
from management.services.ig_provider_dispatch_budget import ValidationDecision
from management.services.ig_revision_provider_execution import (
    REPAIR_KEY, REPAIR_DIAGNOSTIC_KEY, inspect_revision_provider_execution,
    record_provider_repair_preparation,
)
from management import tests_ig_provider_dispatch_budget as pool
from management import tests_ig_revision_provider_execution as ledger


@override_settings(GEMINI_ACCOUNTING_V2_MODE="off", GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="")
class RepairPreparationTests(SimpleTestCase):
    _patch_pool = pool.ValidatedChatDispatchTests._patch_pool

    def _run(self, *, factory, observer, responses=None, reason="unverified_price"):
        candidates = [pool._candidate(1, pool.PRIMARY), pool._candidate(2, pool.FALLBACK)]
        patches = self._patch_pool(candidates, observer=observer)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
             patch.object(ai.requests, "post", side_effect=responses or [pool._Response("bad")]) as post:
            with self.assertRaises(ai.CallAIAnalysisError) as error:
                ai.gemini_generate_text(pool._payload(), role="chat",
                    model_chain_override=[pool.PRIMARY, pool.FALLBACK], max_actual_dispatches=8,
                    result_validator=lambda *_args, **_kwargs: ValidationDecision(False, (reason,)),
                    repair_payload_factory=factory)
        return post, error.exception

    def test_predictable_factory_refusal_does_not_reserve_or_rotate_model(self):
        observer = pool._Observer()
        observer.reserve_provider_repair = Mock(return_value=True)
        post, error = self._run(factory=lambda *_args: None, observer=observer)
        self.assertEqual(post.call_count, 1)
        observer.reserve_provider_repair.assert_not_called()
        self.assertEqual(observer.boundaries[0].failure_kind, "local_semantic_rejection")
        self.assertEqual(error.failure_kind, "local_semantic_rejection")

    def test_unsupported_control_refusal_does_not_repeat_contradictory_prompt(self):
        observer = pool._Observer()
        observer.reserve_provider_repair = Mock(return_value=True)
        post, error = self._run(factory=lambda *_args: None, observer=observer,
            reason="revision_cart_selection_unsupported")
        self.assertEqual(post.call_count, 1)
        observer.reserve_provider_repair.assert_not_called()
        self.assertEqual(error.failure_kind, "local_semantic_rejection")
        self.assertEqual(len(observer.boundaries), 1)

    def test_preparation_exception_does_not_reserve_or_rotate(self):
        observer = pool._Observer()
        observer.reserve_provider_repair = Mock(return_value=True)
        factory = Mock(side_effect=ValueError("predictable local refusal"))
        post, _error = self._run(factory=factory, observer=observer)
        self.assertEqual(post.call_count, 1)
        observer.reserve_provider_repair.assert_not_called()

    def test_factory_preparation_precedes_reservation_and_only_one_correction_runs(self):
        observer = pool._Observer()
        order = []
        observer.reserve_provider_repair = Mock(side_effect=lambda **_kwargs: order.append("reserved") or True)
        def factory(payload, *_args):
            order.append("prepared")
            payload["contents"].append({"role": "user", "parts": [{"text": "Use verified price only"}]})
            return payload
        post, _error = self._run(factory=factory, observer=observer,
            responses=[pool._Response("bad"), pool._Response("still bad")])
        self.assertEqual(order, ["prepared", "reserved"])
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(row.failure_kind == "local_semantic_rejection" for row in observer.boundaries))

    def test_unserializable_correction_fails_before_reservation(self):
        observer = pool._Observer()
        observer.reserve_provider_repair = Mock(return_value=True)
        def factory(payload, *_args):
            payload["contents"][0]["parts"].append({"text": object()})
            return payload
        post, _error = self._run(factory=factory, observer=observer)
        self.assertEqual(post.call_count, 1)
        observer.reserve_provider_repair.assert_not_called()


@override_settings(**ledger.SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class DurableRepairPreparationTests(TransactionTestCase):
    _message = ledger.RevisionProviderExecutionTests._message
    _prepare = ledger.RevisionProviderExecutionTests._prepare
    _begin = ledger.RevisionProviderExecutionTests._begin
    _attempt = ledger.RevisionProviderExecutionTests._attempt
    _refresh = ledger.RevisionProviderExecutionTests._refresh
    setUp = ledger.RevisionProviderExecutionTests.setUp

    def test_local_semantic_rejection_does_not_retire_model_or_other_projects(self):
        self.raw_plan[1] = dict(self.raw_plan[1], key_name="GEMINI_API2", model=self.raw_plan[0]["model"], project_identity="other-project")
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="local_semantic_rejection")
        continuation = inspect_revision_provider_execution(self.revision)
        self.assertFalse(continuation.candidate_plan[0]["skip_reason"])
        self.assertFalse(continuation.candidate_plan[1]["skip_reason"])
        self.assertTrue(continuation.repair_remaining)
        self.assertTrue(observer.reserve_provider_repair(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1))
        self.assertFalse(observer.reserve_provider_repair(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1))

    def test_refused_preparation_is_visible_without_a_reserved_slot(self):
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="local_semantic_rejection")
        record_provider_repair_preparation(observer, key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1,
            state="preparation_refused", reason="repair_factory_refused")
        state = inspect_revision_provider_execution(self.revision)
        self.assertTrue(state.repair_remaining)
        self.assertEqual(state.repair_diagnostics["state"], "preparation_refused")
        self.assertNotIn(REPAIR_KEY, GeminiRequest.objects.get(pk=observer.graph_id).candidate_outcomes)

    def test_reserved_and_started_unresolved_are_distinct_and_survive_crash(self):
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="local_semantic_rejection")
        self.assertTrue(observer.reserve_provider_repair(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1))
        state = inspect_revision_provider_execution(self.revision)
        self.assertEqual(state.repair_diagnostics["state"], "reserved")
        boundary = observer.attempt(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1)
        self.assertTrue(boundary.before_provider(serialized_bytes=100))
        state = inspect_revision_provider_execution(self.revision)
        self.assertEqual(state.repair_diagnostics["state"], "http_started")
        self.assertTrue(state.repair_diagnostics["unresolved"])
        self.assertFalse(state.repair_remaining)
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 2)
        boundary.manual_result(succeeded=False, http_code=200, failure_kind="local_semantic_rejection")
        self.assertFalse(inspect_revision_provider_execution(self.revision).repair_diagnostics["unresolved"])

    def test_supersession_before_dispatch_cannot_start_or_refund_prepared_repair(self):
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="local_semantic_rejection")
        self.assertTrue(observer.reserve_provider_repair(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1))
        boundary = observer.attempt(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1)
        self._refresh()
        self.assertFalse(boundary.before_provider(serialized_bytes=100))
        self.assertFalse(inspect_revision_provider_execution(self.revision).repair_remaining)
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 1)

    def test_stale_worker_cannot_reserve_after_supersession(self):
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="local_semantic_rejection")
        self._refresh()
        self.assertFalse(observer.reserve_provider_repair(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1))
        self.assertTrue(inspect_revision_provider_execution(self.revision).repair_remaining)

    @skipUnless(connection.vendor == "mysql", "Concurrent row-lock proof requires isolated MariaDB")
    def test_two_workers_reserve_exactly_one_lineage_repair(self):
        observer = self._begin()
        self._attempt(observer, 0, code=200, failure="local_semantic_rejection")
        rendezvous = Barrier(2)
        def reserve(worker):
            connections.close_all()
            try:
                rendezvous.wait(timeout=10)
                return worker.reserve_provider_repair(key_name="GEMINI_API", model=self.raw_plan[0]["model"], candidate_index=1)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(reserve, copy(observer)) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(results), [False, True])
        marker = GeminiRequest.objects.get(pk=observer.graph_id).candidate_outcomes[REPAIR_KEY]
        self.assertEqual(marker["candidate_index"], 1)
        self.assertFalse(inspect_revision_provider_execution(self.revision).repair_remaining)
