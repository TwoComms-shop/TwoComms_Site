"""Actual nonlive facade admission and native concurrent accounting proofs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import OperationalError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import GeminiQuotaProfile, GeminiQuotaState, GeminiRequest, GeminiRequestAttempt
from management.services import call_ai_analysis as ai, gemini_accounting_runtime as runtime
from management import tests_gemini_accounting_shadow as fixtures


MODEL = "gemini-3.6-flash"
ENFORCE = {**fixtures.SHADOW, "GEMINI_NONLIVE_ADMISSION_MODE": "enforce"}
KEYS = {f"GEMINI_API{index if index > 1 else ''}": f"nonlive-test-key-{index}" for index in range(1, 7)}
PAYLOAD = {"contents": [{"role": "user", "parts": [{"text": "summarize permitted sources"}]}]}


@override_settings(**ENFORCE)
class NonliveFinalAdmissionTests(TransactionTestCase):
    def setUp(self):
        self.profile_sequence = 0
        self.profile = self._profile()

    def _profile(self, **overrides):
        self.profile_sequence += 1
        values = dict(profile_version=f"nonlive-test.v1.{self.profile_sequence}", model=MODEL,
            rpm_limit=5, input_tpm_limit=250000, rpd_limit=20, permit_limit=1,
            estimator_version=runtime.ACTIVE_ESTIMATOR_VERSION,
            source=GeminiQuotaProfile.Source.OWNER_OBSERVED, source_reference="nonlive_test_fixture",
            observed_at=timezone.now(), effective_from=timezone.now() - timedelta(days=1))
        values.update(overrides)
        return GeminiQuotaProfile.objects.create(**values)

    def _state(self, project="gemini-project-3", **values):
        return GeminiQuotaState.objects.create(project_identity=project, model=MODEL,
            quota_profile=self.profile, pacific_day=timezone.now().astimezone(runtime.PT).date(), **values)

    def _observer(self, *, alias="GEMINI_API3", project="gemini-project-3", role="management"):
        return runtime.begin_request(request_id=None, role=role, reasoning_task="customer_chat" if role == "chat" else "memory_summary",
            candidate_plan=[dict(candidate_index=1, key_name=alias, model=MODEL,
                project_identity=project, identity_status="known", skip_reason="")],
            lane="live" if role == "chat" else "analysis", deadline_seconds=30)

    @contextmanager
    def _facade(self, *, aliases=("GEMINI_API3",), response=None):
        candidates = [(alias, KEYS[alias], MODEL) for alias in aliases]
        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", KEYS, clear=False))
            stack.enter_context(patch.object(ai.gemini_keys, "task_model_chain", return_value=[MODEL]))
            stack.enter_context(patch.object(ai.gemini_keys, "iter_attempts", side_effect=lambda *_args, **_kwargs: iter(candidates)))
            stack.enter_context(patch.object(ai.gemini_keys, "acquire_key_lease", return_value="test-lease"))
            stack.enter_context(patch.object(ai.gemini_keys, "release_key_lease", return_value=True))
            stack.enter_context(patch.object(ai.gemini_quota, "try_reserve", return_value=True))
            cancel = stack.enter_context(patch.object(ai.gemini_quota, "cancel_reservation"))
            post = stack.enter_context(patch.object(ai.requests, "post", return_value=response or fixtures._Response()))
            yield post, cancel

    def _generate(self, **kwargs):
        return ai.gemini_generate_text(PAYLOAD, role="management", reasoning_task="memory_summary", **kwargs)

    def test_final_quota_denials_never_start_http_or_add_spend(self):
        for reason in ("provider_block", "rpd_exhausted", "rpm_exhausted", "permit_exhausted", "tpm_exhausted"):
            with self.subTest(reason=reason):
                GeminiRequestAttempt.objects.all().delete()
                GeminiRequest.objects.all().delete()
                GeminiQuotaState.objects.all().delete()
                self.profile = self._profile(**({"rpm_limit": 1} if reason == "rpm_exhausted"
                    else {"input_tpm_limit": 1} if reason == "tpm_exhausted" else {}))
                state = self._state()
                if reason == "provider_block":
                    state.provider_blocks = {"rpd": {"until": (timezone.now() + timedelta(minutes=1)).isoformat()}}
                elif reason == "rpd_exhausted":
                    state.rpd_dispatched = self.profile.rpd_limit
                elif reason == "permit_exhausted":
                    state.in_flight_count = self.profile.permit_limit
                state.save()
                if reason == "rpm_exhausted":
                    GeminiRequestAttempt.objects.create(request_id="previous-rpm", project_identity=state.project_identity,
                        model=MODEL, provider_started_at=timezone.now(), dispatch_pacific_day=state.pacific_day,
                        outcome="failed", fsm_state="failed")
                before = (state.rpd_dispatched, state.rpd_uncertain, state.in_flight_count)
                with self._facade() as (post, cancel):
                    with self.assertRaises(ai.CallAIAnalysisError):
                        self._generate()
                post.assert_not_called()
                cancel.assert_called_once()
                graph = GeminiRequest.objects.get()
                row = GeminiRequestAttempt.objects.get(request_graph=graph, candidate_index=1)
                self.assertEqual(row.shadow_deny_reason, reason)
                self.assertEqual(row.failure_kind, "provider_admission_denied")
                self.assertEqual(row.accounting_mode, "enforced")
                self.assertIsNone(row.provider_started_at)
                self.assertIsNone(graph.provider_phase_started_at)
                state.refresh_from_db()
                self.assertEqual((state.rpd_dispatched, state.rpd_uncertain, state.in_flight_count), before)

    def test_state_changed_after_planning_is_denied_at_actual_facade_boundary(self):
        state = self._state()
        validate = runtime.RequestObserver._validate_boundary
        def become_exhausted(observer, boundary):
            valid = validate(observer, boundary)
            GeminiQuotaState.objects.filter(pk=state.pk).update(rpd_dispatched=self.profile.rpd_limit)
            return valid
        with self._facade() as (post, _cancel), patch.object(runtime.RequestObserver, "_validate_boundary", become_exhausted):
            with self.assertRaises(ai.CallAIAnalysisError):
                self._generate()
        post.assert_not_called()
        self.assertEqual(GeminiRequestAttempt.objects.filter(shadow_deny_reason="rpd_exhausted").count(), 1)

    def test_unknown_project_missing_profile_and_uncalibrated_estimator_deny_http(self):
        for reason in ("unknown_project", "missing_profile", "estimator_uncalibrated"):
            with self.subTest(reason=reason), ExitStack() as stack:
                GeminiRequestAttempt.objects.all().delete()
                GeminiRequest.objects.all().delete()
                GeminiQuotaState.objects.all().delete()
                if reason == "unknown_project":
                    stack.enter_context(override_settings(GEMINI_KEY_PROJECT_GROUPS={}))
                elif reason == "missing_profile":
                    stack.enter_context(patch.object(runtime.RequestObserver, "_active_profile", return_value=None))
                else:
                    self.profile = self._profile(estimator_version="unknown-estimator")
                with self._facade() as (post, _cancel):
                    with self.assertRaises(ai.CallAIAnalysisError):
                        self._generate()
                post.assert_not_called()
                row = GeminiRequestAttempt.objects.get(request_graph=GeminiRequest.objects.get(), candidate_index=1)
                self.assertEqual(row.shadow_deny_reason, reason)
                self.assertEqual(row.failure_kind, "provider_admission_unknown")
                self.assertIsNone(row.provider_started_at)
                self.assertFalse(GeminiQuotaState.objects.filter(rpd_dispatched__gt=0).exists())
                self.profile = self._profile()

    def test_accounting_failure_is_zero_http_and_keeps_exact_fatal_attribution(self):
        with self._facade() as (post, _cancel), \
             patch.object(runtime.RequestObserver, "_before_provider_once", side_effect=OperationalError("synthetic accounting failure")), \
             patch.object(runtime, "DB_RETRY_DELAYS", (0,)):
            with self.assertRaises(ai.CallAIAnalysisError) as error:
                self._generate()
        post.assert_not_called()
        self.assertEqual(error.exception.failure_kind, "provider_accounting_unavailable")
        self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).exists())
        row = GeminiRequestAttempt.objects.get(request_graph=GeminiRequest.objects.get(), candidate_index=1)
        self.assertEqual(row.failure_kind, "provider_accounting_unavailable")

    def test_calibrated_allow_uses_real_facade_and_settles_once(self):
        with self._facade() as (post, cancel):
            result = self._generate()
        self.assertEqual(result["parsed"], "ok")
        post.assert_called_once()
        cancel.assert_not_called()
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.accounting_mode, "enforced")
        self.assertTrue(all(row.accounting_mode == "enforced" for row in graph.attempts.all()))
        row = GeminiRequestAttempt.objects.get(request_graph=graph, provider_started_at__isnull=False)
        self.assertEqual(row.shadow_decision, "allow")
        state = GeminiQuotaState.objects.get()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count), (1, 0))

    def test_denied_project_does_not_block_other_independent_project(self):
        self._state(rpd_dispatched=self.profile.rpd_limit)
        with self._facade(aliases=("GEMINI_API3", "GEMINI_API4")) as (post, _cancel):
            result = self._generate()
        self.assertEqual(result["parsed"], "ok")
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], KEYS["GEMINI_API4"])
        self.assertEqual(GeminiQuotaState.objects.get(project_identity="gemini-project-3").rpd_dispatched, self.profile.rpd_limit)
        self.assertEqual(GeminiQuotaState.objects.get(project_identity="gemini-project-4").rpd_dispatched, 1)

    def test_existing_model_chain_skips_uncalibrated_profiles_and_reaches_calibrated_model(self):
        for model in ("gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash"):
            self._profile(model=model, estimator_version="shadow-calibration-required")
        with patch.dict("os.environ", KEYS, clear=False), \
             patch.object(ai.gemini_keys, "acquire_key_lease", return_value="test-lease"), \
             patch.object(ai.gemini_keys, "release_key_lease", return_value=True), \
             patch.object(ai.gemini_quota, "try_reserve", return_value=True), \
             patch.object(ai.gemini_quota, "cancel_reservation"), \
             patch.object(ai.requests, "post", return_value=fixtures._Response()) as post:
            result = self._generate(model_override="gemini-3.7-flash")
        self.assertEqual(result["model"], MODEL)
        post.assert_called_once()
        graph = GeminiRequest.objects.get()
        denied = graph.attempts.filter(shadow_deny_reason="estimator_uncalibrated")
        self.assertTrue(denied.exists())
        self.assertFalse(denied.filter(provider_started_at__isnull=False).exists())
        self.assertFalse(graph.attempts.filter(key_name__in=("GEMINI_API", "GEMINI_API2"), provider_started_at__isnull=False).exists())
        self.assertEqual(graph.attempts.filter(provider_started_at__isnull=False, model=MODEL).count(), 1)

    def test_six_project_ledger_does_not_merge_one_exhausted_project_with_other_five(self):
        self._state(rpd_dispatched=self.profile.rpd_limit)
        with self._facade() as (post, _cancel):
            for index in (1, 2, 4, 5, 6):
                alias = f"GEMINI_API{index if index > 1 else ''}"
                observer = self._observer(alias=alias, project=f"gemini-project-{index}", role="chat")
                boundary = observer.attempt(key_name=alias, model=MODEL, candidate_index=1)
                ai._gemini_call_once(MODEL, PAYLOAD, KEYS[alias], parse=False, attempt_boundary=boundary)
        self.assertEqual(post.call_count, 5)
        self.assertEqual(GeminiQuotaState.objects.count(), 6)
        for state in GeminiQuotaState.objects.all():
            self.assertEqual(state.rpd_dispatched, self.profile.rpd_limit if state.project_identity == "gemini-project-3" else 1)

    def test_aliases_of_same_project_do_not_double_quota_or_retry_denied_project(self):
        self._state(rpd_dispatched=self.profile.rpd_limit)
        groups = {**fixtures.SHADOW["GEMINI_KEY_PROJECT_GROUPS"], "GEMINI_API4": "gemini-project-3"}
        with override_settings(GEMINI_KEY_PROJECT_GROUPS=groups), self._facade(aliases=("GEMINI_API3", "GEMINI_API4")) as (post, _cancel):
            with self.assertRaises(ai.CallAIAnalysisError):
                self._generate()
        post.assert_not_called()
        self.assertEqual(GeminiRequestAttempt.objects.filter(failure_kind="provider_admission_denied").count(), 1)
        self.assertEqual(GeminiQuotaState.objects.count(), 1)

    def test_source_guard_refusal_exception_invalid_value_and_stale_claim_are_zero_http(self):
        for guard, reason in ((lambda: False, "source_admission_denied"),
                (lambda: "memory_reset_changed", "memory_reset_changed"),
                (lambda: "rpd_exhausted", "rpd_exhausted"),
                (lambda: 1, "source_admission_denied"),
                (Mock(side_effect=ValueError("private source")), "source_admission_unavailable")):
            with self.subTest(reason=reason):
                GeminiRequestAttempt.objects.all().delete()
                GeminiRequest.objects.all().delete()
                GeminiQuotaState.objects.all().delete()
                with self._facade(aliases=("GEMINI_API3", "GEMINI_API4")) as (post, _cancel):
                    with self.assertRaises(ai.CallAIAnalysisError) as error:
                        self._generate(pre_dispatch_guard=guard)
                self.assertEqual(error.exception.failure_kind, reason)
                post.assert_not_called()
                self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).exists())
                row = GeminiRequestAttempt.objects.get(request_graph=GeminiRequest.objects.get(), candidate_index=1)
                self.assertEqual(row.shadow_deny_reason, reason)
                self.assertTrue(row.failure_kind.startswith("source_admission_"))
                self.assertFalse(GeminiQuotaState.objects.filter(rpd_dispatched__gt=0).exists())

    def test_source_guard_runs_under_atomic_boundary_after_quota_allow(self):
        observations = []
        def guard():
            observations.append((connection.in_atomic_block, GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count()))
            return True
        with self._facade() as (post, _cancel):
            self._generate(pre_dispatch_guard=guard)
        post.assert_called_once()
        self.assertEqual(observations, [(True, 0)])

    def test_source_became_stale_after_planning_is_denied_before_http(self):
        captured_source = {"fresh": True}
        validate = runtime.RequestObserver._validate_boundary
        def superseded(observer, boundary):
            valid = validate(observer, boundary)
            captured_source["fresh"] = False
            return valid
        def guard():
            return True if captured_source["fresh"] else "source_changed"
        self.assertIs(guard(), True)
        with self._facade() as (post, _cancel), patch.object(runtime.RequestObserver, "_validate_boundary", superseded):
            with self.assertRaises(ai.CallAIAnalysisError):
                self._generate(pre_dispatch_guard=guard)
        post.assert_not_called()
        self.assertEqual(GeminiRequestAttempt.objects.filter(shadow_deny_reason="source_changed", provider_started_at__isnull=True).count(), 1)

    def test_management_facade_honors_remaining_total_deadline(self):
        with self._facade() as (post, _cancel):
            self._generate(deadline_seconds=1)
        post.assert_called_once()
        self.assertEqual(GeminiRequest.objects.get().deadline_ms, 1000)

    def test_expired_after_validation_is_denied_before_provider_started_or_spend(self):
        state = self._state()
        validate = runtime.RequestObserver._validate_boundary
        def expire(observer, boundary):
            valid = validate(observer, boundary)
            GeminiRequest._base_manager.filter(pk=observer.graph_id).update(deadline_at=timezone.now() - timedelta(seconds=1))
            return valid
        with self._facade() as (post, cancel), patch.object(runtime.RequestObserver, "_validate_boundary", expire):
            with self.assertRaises(ai.CallAIAnalysisError) as error:
                self._generate()
        self.assertEqual(error.exception.failure_kind, "provider_deadline_expired")
        post.assert_not_called()
        cancel.assert_called_once()
        row = GeminiRequestAttempt.objects.get(candidate_index=1, request_graph=GeminiRequest.objects.get())
        self.assertEqual(row.failure_kind, "provider_deadline_expired")
        self.assertIsNone(row.provider_started_at)
        state.refresh_from_db()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count), (0, 0))

    def test_source_guard_elapsed_deadline_is_rechecked_before_admission_spend(self):
        clock = [100.0]
        def guard():
            clock[0] += 2
            return True
        with self._facade() as (post, _cancel), patch.object(ai.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(ai.CallAIAnalysisError) as error:
                self._generate(deadline_seconds=1, pre_dispatch_guard=guard)
        self.assertEqual(error.exception.failure_kind, "provider_deadline_expired")
        post.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).exists())
        self.assertFalse(GeminiQuotaState.objects.filter(rpd_dispatched__gt=0).exists())

    def test_expired_after_admission_stops_http_without_returning_recorded_spend(self):
        clock = [100.0]
        def delay_before_http():
            clock[0] += 2
        with self._facade() as (post, cancel), \
             patch.object(ai.time, "monotonic", side_effect=lambda: clock[0]), \
             patch("management.services.ig_db_circuit.release_idle_connection", side_effect=delay_before_http):
            with self.assertRaises(ai.CallAIAnalysisError):
                self._generate(deadline_seconds=1)
        post.assert_not_called()
        cancel.assert_not_called()
        row = GeminiRequestAttempt.objects.get(provider_started_at__isnull=False)
        self.assertEqual((row.fsm_state, row.failure_kind), ("failed", "provider_deadline_expired"))
        state = GeminiQuotaState.objects.get()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count), (1, 0))

    def test_http_timeout_is_rebounded_after_preparation_and_admission(self):
        clock = [100.0]
        with self._facade() as (post, _cancel), \
             patch.object(ai.time, "monotonic", side_effect=lambda: clock[0]), \
             patch("management.services.ig_db_circuit.release_idle_connection", side_effect=lambda: clock.__setitem__(0, 100.5)):
            self._generate(deadline_seconds=1)
        post.assert_called_once()
        self.assertLessEqual(sum(post.call_args.kwargs["timeout"]), 0.500001)

    def test_true_shadow_observes_denial_without_changing_live_policy(self):
        self._state(rpd_dispatched=self.profile.rpd_limit)
        with override_settings(GEMINI_NONLIVE_ADMISSION_MODE="shadow"), self._facade() as (post, _cancel):
            self._generate()
        post.assert_called_once()
        row = GeminiRequestAttempt.objects.get(provider_started_at__isnull=False)
        self.assertEqual((row.accounting_mode, row.shadow_decision), ("shadow", "deny"))

    def test_background_pool_and_manual_override_cannot_take_chat_reserves(self):
        with patch.dict("os.environ", KEYS, clear=False), override_settings(GEMINI_ROLE_KEY_POOLS={"management": {"own": ["GEMINI_API", "GEMINI_API2"]}}):
            pool = ai.gemini_keys.role_key_pools()["management"]
            self.assertTrue(set(pool["own"] + pool["borrow"]).isdisjoint({"GEMINI_API", "GEMINI_API2"}))
            self.assertFalse(ai.gemini_keys.manual_key_allowed("management", KEYS["GEMINI_API"]))
            self.assertEqual(len(set(ai.gemini_keys.role_key_pools()["chat"]["own"] + ai.gemini_keys.role_key_pools()["chat"]["borrow"])), 6)

    def test_reaper_terminalizes_shadow_and_enforced_graphs_exactly_once(self):
        now = timezone.now()
        graphs = []
        for mode in ("shadow", "enforce"):
            with override_settings(GEMINI_NONLIVE_ADMISSION_MODE=mode):
                observer = self._observer()
            GeminiRequest._base_manager.filter(pk=observer.graph_id).update(deadline_at=now - timedelta(seconds=1))
            graphs.append(observer.graph_id)
        self.assertEqual(runtime.reconcile_expired_request_graphs(now=now), 2)
        self.assertEqual(runtime.reconcile_expired_request_graphs(now=now), 0)
        for graph in GeminiRequest.objects.filter(pk__in=graphs):
            self.assertEqual((graph.terminal_resolution, graph.terminal_reason), ("failed", "expired_reconcile"))
            self.assertTrue(all(row.accounting_mode == graph.accounting_mode for row in graph.attempts.all()))

    def test_denial_persists_old_expired_permit_without_refunding_dispatched_spend(self):
        state = self._state(rpd_dispatched=self.profile.rpd_limit, in_flight_count=1)
        row = GeminiRequestAttempt.objects.create(request_id="crashed-old", project_identity=state.project_identity,
            model=MODEL, outcome="provider_started", fsm_state="provider_started", provider_started_at=timezone.now() - timedelta(minutes=5),
            dispatch_pacific_day=state.pacific_day, permit_expires_at=timezone.now() - timedelta(seconds=1))
        with self._facade() as (post, _cancel):
            with self.assertRaises(ai.CallAIAnalysisError):
                self._generate()
        post.assert_not_called()
        state.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual((state.rpd_dispatched, state.rpd_uncertain, state.in_flight_count), (self.profile.rpd_limit, 1, 0))
        self.assertEqual(row.fsm_state, "timeout_ambiguous")


@skipUnless(connection.vendor == "mysql", "Final admission race requires isolated MariaDB")
@override_settings(**ENFORCE)
class NonliveNativeAdmissionTests(TransactionTestCase):
    setUp = NonliveFinalAdmissionTests.setUp
    _profile = NonliveFinalAdmissionTests._profile
    _state = NonliveFinalAdmissionTests._state
    _observer = NonliveFinalAdmissionTests._observer
    _facade = NonliveFinalAdmissionTests._facade
    _generate = NonliveFinalAdmissionTests._generate

    def _disposable(self):
        self.assertRegex(str(connection.settings_dict.get("NAME") or ""), r"^test_twocomms_[A-Za-z0-9_]+$")

    def test_quota_lock_wait_crosses_deadline_with_zero_http_and_zero_added_spend(self):
        self._disposable()
        state = self._state()
        held, release, admission = Event(), Event(), Event()
        before = runtime.RequestObserver._before_provider_once
        def entering(observer, boundary, **kwargs):
            admission.set()
            return before(observer, boundary, **kwargs)
        def hold_pair():
            close_old_connections()
            try:
                with transaction.atomic():
                    GeminiQuotaState.objects.select_for_update().get(pk=state.pk)
                    held.set()
                    if not release.wait(timeout=10):
                        raise AssertionError("Pair lock was not released")
            finally:
                close_old_connections()
        def generate():
            close_old_connections()
            try:
                try:
                    self._generate(deadline_seconds=1.5)
                except ai.CallAIAnalysisError as error:
                    return error.failure_kind
                raise AssertionError("Expired candidate was sent")
            finally:
                close_old_connections()
        with self._facade() as (post, _cancel), patch.object(runtime.RequestObserver, "_before_provider_once", entering):
            with ThreadPoolExecutor(max_workers=2) as workers:
                lock = workers.submit(hold_pair)
                self.assertTrue(held.wait(timeout=5))
                call = workers.submit(generate)
                try:
                    self.assertTrue(admission.wait(timeout=5))
                    self.assertFalse(call.done())
                    graph = GeminiRequest.objects.get()
                    Event().wait(timeout=max(0, (graph.deadline_at - timezone.now()).total_seconds()) + 0.1)
                finally:
                    release.set()
                lock.result(timeout=5)
                self.assertEqual(call.result(timeout=5), "provider_deadline_expired")
        post.assert_not_called()
        state.refresh_from_db()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count), (0, 0))
        self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).exists())

    def test_two_planned_facades_compete_for_last_permit_with_one_http(self):
        self._disposable()
        self._state()
        planned = Barrier(2)
        denied = Event()
        release_provider = Event()
        validate = runtime.RequestObserver._validate_boundary
        def planned_together(observer, boundary):
            valid = validate(observer, boundary)
            planned.wait(timeout=10)
            return valid
        def provider(*_args, **_kwargs):
            if not release_provider.wait(timeout=15):
                raise AssertionError("Second planned worker did not finish its final denial")
            return fixtures._Response()
        def worker():
            close_old_connections()
            try:
                self._generate()
                return "sent"
            except ai.CallAIAnalysisError:
                denied.set()
                return "denied"
            finally:
                close_old_connections()
        with self._facade() as (post, _cancel), patch.object(runtime.RequestObserver, "_validate_boundary", planned_together):
            post.side_effect = provider
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(worker) for _ in range(2)]
                try:
                    self.assertTrue(denied.wait(timeout=15), "Last-permit loser was not denied")
                finally:
                    release_provider.set()
                results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(results), ["denied", "sent"])
        post.assert_called_once()
        rows = GeminiRequestAttempt.objects.filter(candidate_index=1)
        self.assertEqual(rows.filter(provider_started_at__isnull=False).count(), 1)
        self.assertEqual(rows.filter(shadow_deny_reason="permit_exhausted", provider_started_at__isnull=True).count(), 1)
        state = GeminiQuotaState.objects.get()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count, state.rpd_uncertain), (1, 0, 0))

    def test_two_reapers_actually_terminalize_enforced_graph_once(self):
        self._disposable()
        observer = self._observer()
        now = timezone.now()
        GeminiRequest._base_manager.filter(pk=observer.graph_id).update(deadline_at=now - timedelta(seconds=1))
        start = Barrier(2)
        def reconcile():
            close_old_connections()
            try:
                start.wait(timeout=10)
                return runtime.reconcile_expired_request_graphs(now=now)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _index: reconcile(), range(2)))
        self.assertEqual(sorted(results), [0, 1])
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual((graph.accounting_mode, graph.terminal_reason), ("enforced", "expired_reconcile"))
        self.assertEqual(graph.attempts.filter(candidate_index=1).count(), 1)

    def test_forced_reaper_before_late_success_conserves_spend_and_restores_winner(self):
        self._disposable()
        observer = self._observer()
        boundary = observer.attempt(key_name="GEMINI_API3", model=MODEL, candidate_index=1)
        self.assertTrue(boundary.before_provider(serialized_bytes=128))
        boundary.failed(ai._GeminiTransient("timeout: ambiguous result"))
        now = timezone.now()
        GeminiRequest._base_manager.filter(pk=observer.graph_id).update(deadline_at=now - timedelta(seconds=1))
        reaped = Event()
        def reconcile():
            close_old_connections()
            try:
                result = runtime.reconcile_expired_request_graphs(now=now)
                reaped.set()
                return result
            finally:
                close_old_connections()
        def late_success():
            close_old_connections()
            try:
                if not reaped.wait(timeout=10):
                    raise AssertionError("Enforced graph reaper did not run first")
                boundary.succeeded({"promptTokenCount": 3, "totalTokenCount": 4})
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(reconcile)
            second = workers.submit(late_success)
            self.assertEqual(first.result(timeout=15), 1)
            second.result(timeout=15)
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        row = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        state = GeminiQuotaState.objects.get(pk=boundary.state_id)
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(graph.winner_attempt_id, row.pk)
        self.assertEqual(row.fsm_state, "succeeded_late")
        self.assertEqual((state.rpd_dispatched, state.rpd_uncertain, state.in_flight_count), (1, 0, 0))
        self.assertEqual(runtime.reconcile_expired_request_graphs(now=now), 0)
