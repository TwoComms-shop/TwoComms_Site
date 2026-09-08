from datetime import timedelta
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import (
    GeminiRequest,
    GeminiRequestAttempt,
    IgClient,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import gemini_accounting_runtime as runtime
from management.services import ig_ai_reply_recovery as recovery
from management.services import ig_legacy_provider_execution as legacy
from management.services.gemini_accounting_contract import canonical_candidate_plan_digest
from management.services.gemini_quota import pacific_day
from management.services.ig_turn_lineage import Lane, resolve_logical_turn_key, turn_lineage


SHADOW = {
    "GEMINI_ACCOUNTING_V2_MODE": "shadow",
    "GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM": "2026-08-29T00:00:00-07:00",
    "GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY": "legacy-provider-test-key",
}
LITE = "gemini-3.5-flash-lite"
SCARCE = "gemini-3.7-flash"


def _candidate(index, model=LITE, *, alias=None, identity=None):
    identity = identity or f"project-{index}"
    return {
        "candidate_index": index,
        "key_name": alias or f"LEGACY_KEY_{index}",
        "key_value": f"secret-{index}",
        "model": model,
        "project_identity": identity,
        "identity_status": "known",
        "skip_reason": "",
    }


@override_settings(**SHADOW)
class LegacyProviderExecutionTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["is_enabled", "ai_enabled"])
        self.client = IgClient.get_or_create_for_sender("legacy-provider-client")
        self.source = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text="original customer turn",
            status=InstagramBotMessage.Status.PROCESSING,
            send_state="sent",
            processing_started_at=timezone.now(),
        )
        self.logical_turn_id = resolve_logical_turn_key(self.source)
        self.plan = [_candidate(1), _candidate(2, SCARCE)]
        safe_plan = runtime.sanitize_candidate_plan(self.plan)
        self.root = GeminiRequest.objects.create(
            request_id="legacy-live-root",
            lane="live",
            task_class="ordinary_live",
            reasoning_task="customer_chat",
            logical_turn_id=self.logical_turn_id,
            source_message_id=self.source.pk,
            client_id=self.client.pk,
            candidate_plan=safe_plan,
            candidate_plan_digest=canonical_candidate_plan_digest(safe_plan),
            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
        )
        self.recovery_token = "recovery-owned-token"
        self.automation_token = "automation-owned-token"
        now = timezone.now()
        IgClient.objects.filter(pk=self.client.pk).update(
            automation_lease_token=self.automation_token,
            automation_lease_until=now + timedelta(minutes=5),
        )
        root_execution, continuation = legacy.initialize_legacy_provider_root(
            graph_id=self.root.pk,
            automation_token=self.automation_token,
            candidate_plan=self.plan,
            now=now,
        )
        self.assertIsNotNone(root_execution)
        self.assertTrue(continuation.ready, continuation.reason)
        GeminiRequest.objects.filter(pk=self.root.pk).update(
            terminal_resolution="failed", terminal_reason="provider_outage",
        )
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(
            status=InstagramBotMessage.Status.DONE,
            processing_started_at=None,
        )
        self.source.refresh_from_db()
        self.root.refresh_from_db()
        self.job = recovery.schedule_recovery(self.source)
        type(self.job).objects.filter(pk=self.job.pk).update(
            status=self.job.Status.PROCESSING,
            lease_token=self.recovery_token,
            lease_until=now + timedelta(minutes=5),
            attempts=1,
            response_window_deadline=now + timedelta(hours=1),
        )
        self.job.refresh_from_db()

    def _execution(self):
        execution, reason = legacy.resolve_legacy_execution(
            job_id=self.job.pk,
            recovery_token=self.recovery_token,
            automation_token=self.automation_token,
            client_id=self.client.pk,
            source_message_id=self.source.pk,
            logical_turn_id=self.logical_turn_id,
        )
        self.assertEqual(reason, "")
        self.assertIsNotNone(execution)
        return execution

    def _attempt(self, graph, *, index=1, model=LITE, alias="LEGACY_KEY_1",
                 failure_kind="transport", seconds_ago=60):
        at = timezone.now() - timedelta(seconds=seconds_ago)
        return GeminiRequestAttempt.objects.create(
            request_id=graph.request_id,
            request_graph=graph,
            role="chat",
            key_name=alias,
            project_identity=f"project-{index}",
            project_group=f"project-{index}",
            model=model,
            outcome="failed",
            fsm_state=GeminiRequestAttempt.FsmState.FAILED,
            accounting_mode="shadow",
            failure_kind=failure_kind,
            logical_turn_id=self.logical_turn_id,
            source_message_id=self.source.pk,
            client_id=self.client.pk,
            lane=graph.lane,
            attempt_index=GeminiRequestAttempt.objects.count() + 1,
            candidate_index=index,
            provider_started_at=at,
            dispatch_pacific_day=pacific_day(at),
            finished_at=at,
            settled_at=at,
        )

    def test_live_opt_in_freezes_before_http_and_allows_one_repair(self):
        client = IgClient.get_or_create_for_sender("legacy-live-opt-in")
        source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="fresh live turn",
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
        )
        token = "live-owned-token"
        IgClient.objects.filter(pk=client.pk).update(
            automation_lease_token=token,
            automation_lease_until=timezone.now() + timedelta(minutes=5),
        )
        plan = [_candidate(1)]
        channel_deadline = timezone.now() + timedelta(minutes=5)
        with patch(
            "management.services.ig_ai_reply_recovery._window_deadline",
            return_value=channel_deadline,
        ), turn_lineage(
            lane=Lane.LIVE, client_id=client.pk,
            source_message_id=source.pk,
            logical_turn_id=resolve_logical_turn_key(source),
        ) as lineage:
            lineage["automation_token"] = token
            observer = runtime.begin_request(
                request_id="legacy-live-opt-in-root",
                role="chat", reasoning_task="customer_chat",
                candidate_plan=plan, deadline_seconds=35,
                legacy_provider_root=True,
            )
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertTrue(observer.provider_continuation.ready)
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual(
            graph.candidate_outcomes[legacy.MANIFEST_KEY]["origin"],
            "live_pre_dispatch",
        )
        self.assertEqual(
            timezone.datetime.fromisoformat(
                graph.candidate_outcomes[legacy.MANIFEST_KEY]["horizon_at"],
            ),
            channel_deadline,
        )
        self.assertFalse(GeminiRequestAttempt.objects.filter(
            request_graph=graph, provider_started_at__isnull=False,
        ).exists())

        first = observer.attempt(
            key_name="LEGACY_KEY_1", model=LITE, candidate_index=1,
        )
        self.assertTrue(first.before_provider(serialized_bytes=100))
        first.manual_result(
            succeeded=False, http_code=200,
            failure_kind="invalid_response",
        )
        self.assertTrue(observer.reserve_provider_repair(
            key_name="LEGACY_KEY_1", model=LITE, candidate_index=1,
        ))
        self.assertFalse(observer.reserve_provider_repair(
            key_name="LEGACY_KEY_1", model=LITE, candidate_index=1,
        ))
        repaired = observer.attempt(
            key_name="LEGACY_KEY_1", model=LITE, candidate_index=1,
        )
        self.assertTrue(repaired.before_provider(serialized_bytes=100))
        repaired.manual_result(
            succeeded=False, http_code=200,
            failure_kind="invalid_response",
        )
        self.assertEqual(GeminiRequestAttempt.objects.filter(
            request_graph=graph, provider_started_at__isnull=False,
        ).count(), 2)

    def test_live_opt_in_keeps_valid_root_when_continuation_must_wait(self):
        from management.services.ig_revision_provider_execution import (
            ProviderContinuation,
        )

        client = IgClient.get_or_create_for_sender("legacy-live-wait")
        source = InstagramBotMessage.objects.create(
            sender_id=client.igsid, client=client,
            role=InstagramBotMessage.Role.USER,
            text="wait for provider", status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
        )
        token = "live-wait-token"
        IgClient.objects.filter(pk=client.pk).update(
            automation_lease_token=token,
            automation_lease_until=timezone.now() + timedelta(minutes=5),
        )
        due = timezone.now() + timedelta(seconds=45)
        horizon = timezone.now() + timedelta(minutes=5)

        def frozen_wait(*, graph_id, **_kwargs):
            manifest = {"horizon_at": horizon.isoformat()}
            GeminiRequest.objects.filter(pk=graph_id).update(
                candidate_outcomes={legacy.MANIFEST_KEY: manifest},
            )
            return legacy.LegacyRootExecution(
                automation_token=token, client_id=client.pk,
                source_message_id=source.pk,
                logical_turn_id=resolve_logical_turn_key(source),
                root_graph_id=graph_id,
            ), ProviderContinuation(
                reason="provider_wait", root_revision_id=graph_id,
                manifest=manifest,
                candidate_plan=tuple(self.plan), http_remaining=8,
                scarce_remaining=2, repair_remaining=True,
                next_due_at=due,
            )

        with patch(
            "management.services.ig_legacy_provider_execution.initialize_legacy_provider_root",
            side_effect=frozen_wait,
        ), turn_lineage(
            lane=Lane.LIVE, client_id=client.pk,
            source_message_id=source.pk,
            logical_turn_id=resolve_logical_turn_key(source),
        ) as lineage:
            lineage["automation_token"] = token
            observer = runtime.begin_request(
                request_id="legacy-live-wait-root", role="chat",
                reasoning_task="customer_chat", candidate_plan=self.plan,
                deadline_seconds=35, legacy_provider_root=True,
            )

        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertEqual(observer.provider_continuation.reason, "provider_wait")
        self.assertEqual(observer.provider_continuation.next_due_at, due)
        self.assertTrue(GeminiRequest.objects.filter(
            request_id="legacy-live-wait-root",
        ).exists())
        self.assertEqual(
            GeminiRequest.objects.get(
                request_id="legacy-live-wait-root",
            ).candidate_outcomes[legacy.MANIFEST_KEY]["horizon_at"],
            horizon.isoformat(),
        )

    def test_recovery_never_retrofits_missing_manifest(self):
        GeminiRequest.objects.filter(pk=self.root.pk).update(candidate_outcomes={})
        execution = self._execution()
        continuation = legacy.legacy_provider_continuation(
            execution, candidate_plan=self.plan,
        )
        self.assertFalse(continuation.ready)
        self.assertEqual(continuation.reason, "legacy_manifest_missing")
        self.root.refresh_from_db()
        self.assertNotIn(legacy.MANIFEST_KEY, self.root.candidate_outcomes)

    def test_failure_disposition_waits_only_with_due_before_root_horizon(self):
        now = timezone.now()
        due = now + timedelta(seconds=45)
        horizon = now + timedelta(minutes=5)
        waiting = legacy.classify_legacy_provider_failure(
            "provider_wait", next_due_at=due, horizon_at=horizon, now=now,
        )
        self.assertEqual(waiting.state, "wait")
        self.assertFalse(waiting.consume_attempt)
        self.assertEqual(waiting.retry_at, due)
        self.assertEqual(
            legacy.classify_legacy_provider_failure(
                "provider_wait", next_due_at=horizon,
                horizon_at=horizon, now=now,
            ).state,
            "terminal",
        )
        self.assertEqual(
            legacy.classify_legacy_provider_failure(
                "provider_dispatch_budget", now=now,
            ).state,
            "terminal",
        )

    def test_runtime_creates_parented_recovery_graph_from_exact_root(self):
        with turn_lineage(
            lane=Lane.RECOVERY,
            client_id=self.client.pk,
            source_message_id=self.source.pk,
            logical_turn_id=self.logical_turn_id,
            recovery_job_id=self.job.pk,
        ) as lineage:
            lineage["recovery_token"] = self.recovery_token
            lineage["automation_token"] = self.automation_token
            observer = runtime.begin_request(
                request_id="legacy-recovery-child",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=self.plan,
                deadline_seconds=35,
            )

        self.assertTrue(observer.enabled)
        child = GeminiRequest.objects.get(request_id=observer.request_id)
        self.assertEqual(child.parent_request_id, self.root.pk)
        self.assertEqual(child.source_message_id, self.source.pk)
        self.assertTrue(child.source_execution_key.startswith("ig-recovery:"))
        self.assertNotIn(self.recovery_token, child.source_execution_key)
        self.root.refresh_from_db()
        stored = json.dumps(self.root.candidate_outcomes, sort_keys=True)
        self.assertNotIn("secret-1", stored)
        self.assertNotIn("original customer turn", stored)

    def test_newer_source_never_reuses_old_root(self):
        newer = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text="independent newer turn",
            status=InstagramBotMessage.Status.DONE,
        )
        type(self.job).objects.filter(pk=self.job.pk).update(
            source_message_id=newer.pk,
        )
        self.job.refresh_from_db()
        execution, reason = legacy.resolve_legacy_execution(
            job_id=self.job.pk,
            recovery_token=self.recovery_token,
            automation_token=self.automation_token,
            client_id=self.client.pk,
            source_message_id=self.source.pk,
            logical_turn_id=self.logical_turn_id,
        )
        self.assertIsNone(execution)
        self.assertEqual(reason, "legacy_source_changed")

        execution, reason = legacy.resolve_legacy_execution(
            job_id=self.job.pk,
            recovery_token=self.recovery_token,
            automation_token=self.automation_token,
            client_id=self.client.pk,
            source_message_id=newer.pk,
            logical_turn_id=resolve_logical_turn_key(newer),
        )
        self.assertIsNone(execution)
        self.assertEqual(reason, "legacy_root_missing")

    def test_common_started_ledger_allows_only_eighth_dispatch(self):
        execution = self._execution()
        continuation = legacy.legacy_provider_continuation(
            execution, candidate_plan=self.plan,
        )
        self.assertTrue(continuation.ready)
        for _index in range(7):
            self._attempt(self.root)
        continuation = legacy.legacy_provider_continuation(execution)
        self.assertEqual(continuation.http_remaining, 1)

        with turn_lineage(
            lane=Lane.RECOVERY, client_id=self.client.pk,
            source_message_id=self.source.pk,
            logical_turn_id=self.logical_turn_id,
            recovery_job_id=self.job.pk,
        ) as lineage:
            lineage["recovery_token"] = self.recovery_token
            lineage["automation_token"] = self.automation_token
            observer = runtime.begin_request(
                request_id="legacy-eighth-child", role="chat",
                reasoning_task="customer_chat", candidate_plan=self.plan,
                deadline_seconds=35,
            )
        boundary = observer.attempt(
            key_name="LEGACY_KEY_1", model=LITE, candidate_index=1,
        )
        self.assertTrue(boundary.validate_ownership())
        self.assertTrue(boundary.before_provider(serialized_bytes=100))
        boundary.failed(Exception("transport"), failure_kind="transport")
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                request_graph__in=[self.root, GeminiRequest.objects.get(request_id="legacy-eighth-child")],
                provider_started_at__isnull=False,
            ).count(),
            8,
        )

        type(self.job).objects.filter(pk=self.job.pk).update(
            attempts=2, lease_token="recovery-second-token",
        )
        self.recovery_token = "recovery-second-token"
        with turn_lineage(
            lane=Lane.RECOVERY, client_id=self.client.pk,
            source_message_id=self.source.pk,
            logical_turn_id=self.logical_turn_id,
            recovery_job_id=self.job.pk,
        ) as lineage:
            lineage["recovery_token"] = self.recovery_token
            lineage["automation_token"] = self.automation_token
            blocked = runtime.begin_request(
                request_id="legacy-ninth-child", role="chat",
                reasoning_task="customer_chat", candidate_plan=self.plan,
                deadline_seconds=35,
            )
        self.assertTrue(blocked.provider_blocked)
        self.assertEqual(blocked.block_reason, "provider_dispatch_budget")
        self.assertFalse(GeminiRequest.objects.filter(request_id="legacy-ninth-child").exists())

    def test_scarce_and_repair_budgets_are_shared_on_root(self):
        execution = self._execution()
        continuation = legacy.legacy_provider_continuation(
            execution, candidate_plan=self.plan,
        )
        self.assertTrue(continuation.repair_remaining)
        self._attempt(self.root, index=2, model=SCARCE, alias="LEGACY_KEY_2")
        self._attempt(self.root, index=2, model=SCARCE, alias="LEGACY_KEY_2")
        continuation = legacy.legacy_provider_continuation(execution)
        self.assertEqual(continuation.scarce_remaining, 0)
        scarce = next(row for row in continuation.candidate_plan if row["model"] == SCARCE)
        lite = next(row for row in continuation.candidate_plan if row["model"] == LITE)
        self.assertEqual(scarce["skip_reason"], "scarce_model_budget")
        self.assertEqual(lite["skip_reason"], "")
        self.assertFalse(continuation.repair_remaining)
