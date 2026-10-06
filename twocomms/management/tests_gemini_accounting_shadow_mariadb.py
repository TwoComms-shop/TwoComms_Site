"""Disposable MariaDB concurrency proof for Gemini S3b shadow accounting.

Run only through ``test_settings_mariadb`` and an explicitly disposable
``test_twocomms_*`` database.  The normal SQLite suite skips this module.
"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from management.models import (
    GeminiQuotaProfile,
    GeminiQuotaState,
    GeminiRequest,
    GeminiRequestAttempt,
    IgClient,
    InstagramBotMessage,
)
from management.services import call_ai_analysis as ai
from management.services import gemini_accounting_runtime as runtime
from management.services.gemini_routing import TurnFacts, classify_live_turn, persist_decision
from management.services.ig_turn_lineage import Lane, turn_lineage


@skipUnless(connection.vendor == "mysql", "Disposable MariaDB-only S3b proof")
@override_settings(
    GEMINI_ACCOUNTING_V2_MODE="shadow",
    GEMINI_NONLIVE_ADMISSION_MODE="shadow",
    GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T00:00:00-07:00",
    GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY="maria-shadow-test-hmac-key",
    GEMINI_KEY_PROJECT_GROUPS={"GEMINI_API": "gemini-project-race"},
)
class GeminiShadowMariaDbConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        import datetime as dt

        observed_at = dt.datetime(
            2026, 8, 29, 17, 18, 56, tzinfo=dt.timezone.utc
        )
        GeminiQuotaProfile.objects.get_or_create(
            profile_version=runtime.OWNER_PROFILE_VERSION,
            model="gemini-3.7-flash",
            defaults={
                "rpm_limit": 5,
                "input_tpm_limit": 250_000,
                "rpd_limit": 20,
                "permit_limit": 1,
                "estimator_version": "shadow-calibration-required",
                "source": GeminiQuotaProfile.Source.OWNER_OBSERVED,
                "source_reference": "maria_test_fixture",
                "observed_at": observed_at,
                "effective_from": observed_at,
            },
        )

    def test_database_is_explicitly_disposable(self):
        self.assertRegex(
            str(connection.settings_dict.get("NAME") or ""),
            r"^test_twocomms_[A-Za-z0-9_]+$",
        )

    def test_two_real_dispatches_survive_last_permit_race_and_settle(self):
        model = "gemini-3.7-flash"
        start = Barrier(2)
        dispatched = Barrier(2)

        def worker(index: int):
            close_old_connections()
            try:
                observer = runtime.begin_request(
                    request_id=f"maria-shadow-race-{index}",
                    role="management",
                    reasoning_task="customer_intelligence",
                    candidate_plan=[{
                        "candidate_index": 1,
                        "key_name": "GEMINI_API",
                        "project_identity": "gemini-project-race",
                        "identity_status": "known",
                        "model": model,
                        "skip_reason": "",
                    }],
                    lane="analysis",
                )
                boundary = observer.attempt(
                    key_name="GEMINI_API", model=model, candidate_index=1
                )
                start.wait(timeout=10)
                boundary.before_provider(serialized_bytes=128, inline_count=0)
                self.assertIsNotNone(boundary.attempt_id)
                dispatched.wait(timeout=10)
                boundary.manual_result(
                    succeeded=True,
                    http_code=200,
                    usage={
                        "promptTokenCount": 12,
                        "candidatesTokenCount": 2,
                        "totalTokenCount": 14,
                    },
                )
                return boundary.attempt_id
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            attempt_ids = list(executor.map(worker, (1, 2)))

        self.assertEqual(len(set(attempt_ids)), 2)
        rows = GeminiRequestAttempt.objects.filter(pk__in=attempt_ids)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            rows.filter(fsm_state=GeminiRequestAttempt.FsmState.SUCCEEDED).count(),
            2,
        )
        state = GeminiQuotaState.objects.get(
            project_identity="gemini-project-race", model=model
        )
        self.assertEqual(state.rpd_dispatched, 2)
        self.assertEqual(state.in_flight_count, 0)
        # The second dispatch is observed even if the one-per-model shadow
        # permit would have denied it; shadow never changes provider behavior.
        self.assertEqual(
            rows.filter(
                shadow_decision=GeminiRequestAttempt.ShadowDecision.DENY,
                shadow_deny_reason="permit_exhausted",
            ).count(),
            1,
        )

    def test_two_threads_create_one_source_lane_graph_and_one_dispatch(self):
        model = "gemini-3.7-flash"
        client = IgClient.get_or_create_for_sender("maria-source-lane-race")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="maria-source-lane-race-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        decision = classify_live_turn(TurnFacts(has_image=True))
        persist_decision(message, decision)
        plan = [{
            "candidate_index": 1,
            "key_name": "GEMINI_API",
            "project_identity": "gemini-project-race",
            "identity_status": "known",
            "model": model,
            "skip_reason": "",
        }]
        start = Barrier(2)

        def worker(index: int):
            close_old_connections()
            try:
                with turn_lineage(
                    lane=Lane.LIVE,
                    client_id=client.pk,
                    source_message_id=message.pk,
                    logical_turn_id=f"t{client.pk}:{message.pk}",
                ):
                    start.wait(timeout=10)
                    observer = runtime.begin_request(
                        request_id=f"maria-source-lane-race-{index}",
                        role="chat",
                        reasoning_task="media_analysis",
                        candidate_plan=plan,
                        deadline_seconds=45,
                        routing_decision=decision,
                    )
                boundary = observer.attempt(
                    key_name="GEMINI_API",
                    model=model,
                    candidate_index=1,
                )
                if observer.provider_blocked:
                    return ("blocked", boundary.before_provider(
                        serialized_bytes=128,
                        inline_count=1,
                    ))
                admitted = boundary.before_provider(
                    serialized_bytes=128,
                    inline_count=1,
                )
                if admitted:
                    boundary.manual_result(
                        succeeded=True,
                        http_code=200,
                        usage={
                            "promptTokenCount": 12,
                            "candidatesTokenCount": 2,
                            "totalTokenCount": 14,
                        },
                    )
                return ("canonical", admitted)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, (1, 2)))

        self.assertEqual(sorted(outcomes), [("blocked", False), ("canonical", True)])
        self.assertEqual(
            GeminiRequest.objects.filter(
                source_message_id=message.pk,
                lane=Lane.LIVE,
            ).count(),
            1,
        )
        graph = GeminiRequest.objects.get(
            source_message_id=message.pk,
            lane=Lane.LIVE,
        )
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertIsNotNone(graph.winner_attempt_id)
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                request_graph=graph,
                fsm_state=GeminiRequestAttempt.FsmState.SUCCEEDED,
            ).count(),
            1,
        )

    def test_admission_and_settlement_share_lock_order_without_deadlock(self):
        model = "gemini-3.7-flash"
        for iteration in range(8):
            client = IgClient.get_or_create_for_sender(
                f"maria-admit-settle-{iteration}"
            )
            message = InstagramBotMessage.objects.create(
                sender_id=client.igsid,
                client=client,
                role=InstagramBotMessage.Role.USER,
                text="bounded",
                mid=f"maria-admit-settle-{iteration}-mid",
                status=InstagramBotMessage.Status.PROCESSING,
            )
            decision = classify_live_turn(TurnFacts(has_image=True))
            persist_decision(message, decision)
            plan = [
                {
                    "candidate_index": candidate_index,
                    "key_name": "GEMINI_API",
                    "project_identity": "gemini-project-race",
                    "identity_status": "known",
                    "model": model,
                    "skip_reason": "",
                }
                for candidate_index in (1, 2)
            ]
            with turn_lineage(
                lane=Lane.LIVE,
                client_id=client.pk,
                source_message_id=message.pk,
                logical_turn_id=f"t{client.pk}:{message.pk}",
            ):
                observer = runtime.begin_request(
                    request_id=f"maria-admit-settle-{iteration}",
                    role="chat",
                    reasoning_task="media_analysis",
                    candidate_plan=plan,
                    deadline_seconds=45,
                    routing_decision=decision,
                )
            first = observer.attempt(
                key_name="GEMINI_API",
                model=model,
                candidate_index=1,
            )
            self.assertTrue(first.before_provider(
                serialized_bytes=128,
                inline_count=1,
            ))
            second = observer.attempt(
                key_name="GEMINI_API",
                model=model,
                candidate_index=2,
            )
            start = Barrier(2)

            def settle_first():
                close_old_connections()
                try:
                    start.wait(timeout=10)
                    first.manual_result(
                        succeeded=True,
                        http_code=200,
                        usage={
                            "promptTokenCount": 12,
                            "candidatesTokenCount": 2,
                            "totalTokenCount": 14,
                        },
                    )
                finally:
                    close_old_connections()

            def admit_and_settle_second():
                close_old_connections()
                try:
                    start.wait(timeout=10)
                    admitted = second.before_provider(
                        serialized_bytes=128,
                        inline_count=1,
                    )
                    if admitted:
                        second.manual_result(
                            succeeded=False,
                            http_code=503,
                            failure_kind="http_5xx",
                        )
                    return admitted
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as executor:
                finish_future = executor.submit(settle_first)
                admit_future = executor.submit(admit_and_settle_second)
                finish_future.result(timeout=20)
                admitted = admit_future.result(timeout=20)

            graph = GeminiRequest.objects.get(pk=observer.graph_id)
            first_row = GeminiRequestAttempt.objects.get(pk=first.attempt_id)
            self.assertEqual(
                first_row.fsm_state,
                GeminiRequestAttempt.FsmState.SUCCEEDED,
            )
            self.assertEqual(graph.winner_attempt_id, first_row.pk)
            if admitted:
                second_row = GeminiRequestAttempt.objects.get(pk=second.attempt_id)
                self.assertEqual(
                    second_row.fsm_state,
                    GeminiRequestAttempt.FsmState.FAILED,
                )
            state = GeminiQuotaState.objects.get(
                project_identity="gemini-project-race",
                model=model,
            )
            self.assertEqual(state.in_flight_count, 0)

    def test_expired_reaper_and_late_success_converge_without_deadlock(self):
        model = "gemini-3.7-flash"
        observer = runtime.begin_request(
            request_id="maria-reaper-late-success",
            role="management",
            reasoning_task="conversation_reanalysis",
            candidate_plan=[{
                "candidate_index": 1,
                "key_name": "GEMINI_API",
                "project_identity": "gemini-project-race",
                "identity_status": "known",
                "model": model,
                "skip_reason": "",
            }],
            deadline_seconds=1,
            lane="analysis",
        )
        boundary = observer.attempt(
            key_name="GEMINI_API",
            model=model,
            candidate_index=1,
        )
        self.assertTrue(boundary.before_provider(
            serialized_bytes=128,
            inline_count=0,
        ))
        boundary.failed(ai._GeminiTransient("timeout: ambiguous provider result"))
        expired_now = runtime.timezone.now()
        GeminiRequest._base_manager.filter(pk=observer.graph_id).update(
            deadline_at=expired_now - dt.timedelta(seconds=1),
        )
        start = Barrier(2)

        def reconcile_graph():
            close_old_connections()
            try:
                start.wait(timeout=10)
                return runtime.reconcile_expired_request_graphs(
                    now=expired_now,
                    limit=10,
                )
            finally:
                close_old_connections()

        def settle_late_success():
            close_old_connections()
            try:
                start.wait(timeout=10)
                boundary.succeeded({
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 14,
                })
                return True
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            reconcile_future = executor.submit(reconcile_graph)
            success_future = executor.submit(settle_late_success)
            reconciled = reconcile_future.result(timeout=20)
            self.assertTrue(success_future.result(timeout=20))

        self.assertIn(reconciled, {0, 1})
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        state = GeminiQuotaState.objects.get(pk=boundary.state_id)
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(graph.winner_attempt_id, attempt.pk)
        self.assertTrue(attempt.winner_claimed)
        self.assertEqual(
            attempt.fsm_state,
            GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
        )
        self.assertEqual(state.rpd_dispatched, 1)
        self.assertEqual(state.rpd_uncertain, 0)
        self.assertEqual(state.in_flight_count, 0)
