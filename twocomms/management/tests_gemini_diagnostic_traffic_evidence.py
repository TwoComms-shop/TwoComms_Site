"""Manual generation diagnostics consume quota without recovering traffic."""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from management import tests_gemini_health as health_fixtures, tests_gemini_v2_read_api as v2_fixtures
from management.models import GeminiQuotaState, GeminiRequest, GeminiRequestAttempt
from management.services import gemini_accounting_contract as contract, gemini_health as health, gemini_v2_read_model as v2


class DiagnosticTrafficPureTests(SimpleTestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)

    def row(self, *, index=1, role="chat", lane="live", success=False, at=None, failure="http_5xx"):
        return dict(id=index, request_id=f"traffic-{index}", key_name="GEMINI_API3", model="gemini-3.7-flash",
            role=role, lane=lane, outcome="succeeded" if success else "failed", fsm_state="succeeded" if success else "failed",
            provider_started_at=at or self.now, created_at=at or self.now, failure_kind="" if success else failure,
            http_code=200 if success else 503, candidate_index=1, winner_claimed=success)

    def test_diagnostic_role_and_lane_cannot_replace_traffic_failure_or_claim_runtime_recovery(self):
        failure = self.row(at=self.now - dt.timedelta(minutes=1))
        for role, lane in (("diagnostic", "live"), ("chat", "diagnostic"), ("health_metadata", "metadata_probe")):
            diagnostic = self.row(index=2, role=role, lane=lane, success=True)
            self.assertFalse(health.is_generation_traffic_evidence(diagnostic))
            self.assertEqual(health._runtime_live_state([failure, diagnostic], self.now), ("DEGRADED", None))
            self.assertIsNone(health._runtime_live_state([diagnostic], self.now))
            self.assertEqual(v2._last_evidence([failure, diagnostic])["http_code"], 503)
            self.assertIsNone(v2._last_evidence([diagnostic]))

    def test_customer_success_after_actual_failure_still_recovers_and_same_time_later_failure_wins(self):
        failure = self.row(at=self.now - dt.timedelta(minutes=1))
        success = self.row(index=2, success=True)
        self.assertEqual(health._runtime_live_state([failure, success], self.now), ("LIVE", "gemini-3.7-flash"))
        self.assertTrue(v2._last_evidence([failure, success])["success"])
        state = SimpleNamespace(in_flight_count=0, last_failure_kind="", last_http_code=200,
            accounting_status="available", last_success_at=self.now, last_failure_at=None)
        args = dict(accounting_active=True, slot=SimpleNamespace(configured=True, mapping_state="explicit"),
            profile=object(), state=state, blocks=[], now=self.now)
        self.assertEqual(v2._pair_status(**args, traffic_rows=[success, self.row(index=3)]), "provider_degraded")
        self.assertEqual(v2._pair_status(**args, traffic_rows=[failure, success]), "confirmed_recent_success")


class DiagnosticHealthSnapshotTests(TestCase):
    def setUp(self):
        health_fixtures.GeminiHealthSnapshotTests.setUp(self)

    _attempt = health_fixtures.GeminiHealthSnapshotTests._attempt

    def build(self):
        with patch.object(health.gemini_keys, "pool_status", return_value=self.pool):
            return health.build_snapshot(now=self.now)

    def key(self, payload):
        return next(row for row in payload["keys"] if row["slot_id"] == health.SLOT_BY_ALIAS["GEMINI_API"])

    def test_actual_503_followed_by_generation_diagnostic_success_keeps_real_failure_freshness(self):
        failed_at = self.now - dt.timedelta(minutes=5)
        self._attempt(request_id="customer-503", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="failed", at=failed_at, failure_kind="http_5xx")
        row = self._attempt(request_id="explicit-diagnostic", key_name="GEMINI_API", model="gemini-3.7-flash",
            outcome="succeeded", at=self.now, role="diagnostic")
        # Fixture identity is selected at insertion; no production identity
        # fields are rewritten by the public immutable accounting manager.
        key = self.key(self.build())
        self.assertEqual(key["live_state"], "DEGRADED"); self.assertEqual(key["last_generation_at"], failed_at.isoformat())
        self.assertEqual(key["generation_models"]["gemini-3.7-flash"]["observations"], 1)
        self.assertEqual(GeminiRequestAttempt.objects.get(pk=row.pk).role, "diagnostic")

    def test_diagnostic_only_and_old_traffic_never_create_fresh_useful_generation(self):
        self._attempt(request_id="diagnostic-only", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="succeeded", at=self.now, role="diagnostic")
        key = self.key(self.build()); self.assertEqual(key["source"], "none")
        self.assertEqual(key["live_state"], "STALE"); self.assertIsNone(key["last_generation_at"])
        self._attempt(request_id="old-customer", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="failed",
            at=self.now - dt.timedelta(hours=5), failure_kind="http_5xx")
        key = self.key(self.build()); self.assertEqual(key["live_state"], "STALE")

    def test_legacy_role_chat_in_diagnostic_lane_and_metadata_do_not_recover_traffic(self):
        failed_at = self.now - dt.timedelta(minutes=5)
        self._attempt(request_id="customer-auth", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="failed", at=failed_at, failure_kind="invalid_key")
        lane_row = GeminiRequestAttempt.objects.create(request_id="legacy-diagnostic-lane", key_name="GEMINI_API", model="gemini-3.7-flash",
            role="chat", lane="diagnostic", outcome="succeeded")
        GeminiRequestAttempt._base_manager.filter(pk=lane_row.pk).update(created_at=self.now)
        self._attempt(request_id="metadata-success", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="succeeded", at=self.now, role="health_metadata")
        key = self.key(self.build()); self.assertEqual(key["live_state"], "OFFLINE")
        self.assertEqual(key["last_generation_at"], failed_at.isoformat())
        self.assertEqual(key["metadata_models"]["gemini-3.7-flash"]["observations"], 1)

    def test_health_read_does_not_mutate_attempts_or_call_metadata_or_generation(self):
        self._attempt(request_id="explicit-diagnostic", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="succeeded", at=self.now, role="diagnostic")
        with CaptureQueriesContext(connection) as queries, patch("management.services.gemini_probe.probe_key") as generation, patch(
            "management.services.gemini_probe.probe_key_metadata") as metadata:
            self.build(); self.build()
        generation.assert_not_called(); metadata.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))


class DiagnosticQuotaEvidenceTests(TestCase):
    def setUp(self):
        v2_fixtures.GeminiV2ReadApiTests.setUp(self)
        self.alias = "GEMINI_API3"; self.model = "gemini-3.7-flash"
        self.identity = self.groups[self.alias]
        self.profile = self.profiles[self.model]
        self.state = GeminiQuotaState.objects.create(project_identity=self.identity, model=self.model,
            quota_profile=self.profile, pacific_day=self.now.astimezone(v2.PT).date(), rpd_dispatched=2,
            accounting_status="available", last_success_at=self.now, last_http_code=200)

    def attempt(self, index, *, diagnostic=False, success=False, failure_kind="http_5xx", at=None):
        lane = "diagnostic" if diagnostic else "live"
        plan = [{"candidate_index": 1, "project_identity": self.identity, "model": self.model,
            "identity_status": "known", "initial_skip_reason": ""}]
        graph = GeminiRequest.objects.create(request_id=f"purpose-{index}", lane=lane, task_class="ordinary_live",
            reasoning_task="health_probe" if diagnostic else "customer_chat", logical_turn_id=f"purpose-turn-{index}",
            routing_policy_version="test-v1", accounting_policy_version="test-v1", quota_profile_version="read-api-profile-v1",
            candidate_plan=plan, candidate_plan_digest=contract.canonical_candidate_plan_digest(plan),
            deadline_ms=35000, accounting_mode="shadow")
        started = at or self.now
        return GeminiRequestAttempt.objects.create(request_graph=graph, request_id=graph.request_id,
            role="diagnostic" if diagnostic else "chat", lane=lane, key_name=self.alias, project_group=self.identity,
            project_identity=self.identity, model=self.model, logical_turn_id=graph.logical_turn_id,
            quota_profile=self.profile, accounting_mode="shadow", attempt_index=1, candidate_index=1,
            outcome="succeeded" if success else "failed", fsm_state="succeeded" if success else "failed",
            failure_kind="" if success else failure_kind, http_code=200 if success else 503,
            provider_started_at=started, dispatch_pacific_day=started.astimezone(v2.PT).date(),
            finished_at=started + dt.timedelta(milliseconds=10), settled_at=started + dt.timedelta(milliseconds=10),
            permit_released_at=started + dt.timedelta(milliseconds=10), prompt_tokens=100, reserved_prompt_tokens=120)

    def pair(self):
        payload = v2.build_quotas_payload(now=self.now)
        return next(project for row in payload["models"] if row["model"] == self.model
            for project in row["projects"] if project["slot_id"] == health.SLOT_BY_ALIAS[self.alias])

    def test_diagnostic_success_still_counts_rpm_rpd_usage_but_not_customer_recovery(self):
        failed_at = self.now - dt.timedelta(seconds=20)
        self.attempt(1, at=failed_at); self.attempt(2, diagnostic=True, success=True)
        pair = self.pair()
        self.assertEqual(pair["status"], "provider_degraded"); self.assertEqual(pair["rpm"]["used"], 2)
        self.assertEqual(pair["rpd"]["used"], 2); self.assertEqual(pair["usage_by_lane"], {"live": 1, "diagnostic": 1})
        self.assertEqual(pair["input_tpm"]["used"], 200)
        self.assertFalse(pair["last_real_evidence"]["success"]); self.assertEqual(pair["last_real_evidence"]["http_code"], 503)
        self.assertEqual(pair["last_failure_kind"], "http_5xx"); self.assertEqual(pair["last_http_code"], 503)
        self.assertIsNone(pair["last_success_at"])

    def test_diagnostic_only_is_assumed_not_confirmed_and_customer_success_restores_traffic(self):
        self.attempt(1, diagnostic=True, success=True)
        pair = self.pair(); self.assertEqual(pair["status"], "available_assumed")
        self.assertIsNone(pair["last_real_evidence"]); self.assertIsNone(pair["last_success_at"])
        self.attempt(2, success=True)
        self.assertEqual(self.pair()["status"], "confirmed_recent_success")

    def test_customer_auth_and_local_semantic_failure_survive_later_diagnostic_success(self):
        self.attempt(1, at=self.now - dt.timedelta(seconds=20), failure_kind="invalid_key")
        self.attempt(2, diagnostic=True, success=True)
        self.assertEqual(self.pair()["status"], "auth_failed")
        self.attempt(3, at=self.now - dt.timedelta(seconds=10), failure_kind="local_semantic_rejection")
        self.assertEqual(self.pair()["status"], "local_validation_failed")

    def test_existing_unknown_quota_block_and_credential_incident_precedence_remain(self):
        self.attempt(1, diagnostic=True, success=True)
        GeminiQuotaState.objects.filter(pk=self.state.pk).update(provider_blocks={"unknown": {"metric": "unknown", "until": ""}}, accounting_status="blocked")
        self.assertEqual(self.pair()["status"], "accounting_unknown")
        GeminiQuotaState.objects.filter(pk=self.state.pk).update(provider_blocks={}, accounting_status="degraded", last_failure_kind="invalid_key", last_http_code=401)
        self.assertEqual(self.pair()["status"], "auth_failed")

    def test_state_only_failure_truth_survives_diagnostic_success_without_claiming_customer_recovery(self):
        self.attempt(1, diagnostic=True, success=True)
        GeminiQuotaState.objects.filter(pk=self.state.pk).update(
            accounting_status="degraded", last_failure_at=self.now - dt.timedelta(minutes=2),
            last_failure_kind="http_5xx", last_http_code=503,
        )
        pair = self.pair()
        self.assertEqual(pair["status"], "provider_degraded")
        self.assertIsNone(pair["last_real_evidence"])
        self.assertIsNone(pair["last_success_at"])
        GeminiQuotaState.objects.filter(pk=self.state.pk).update(last_failure_at=self.now - dt.timedelta(hours=25))
        self.assertEqual(self.pair()["status"], "available_assumed")

    def test_read_preserves_economic_rows_and_permits_without_any_provider_io(self):
        self.attempt(1, diagnostic=True, success=True)
        before = GeminiQuotaState.objects.values().get(pk=self.state.pk)
        with CaptureQueriesContext(connection) as queries, patch("management.services.gemini_probe.probe_key") as generation, patch(
            "management.services.gemini_probe.probe_key_metadata") as metadata:
            self.pair(); self.pair()
        generation.assert_not_called(); metadata.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertEqual(before, GeminiQuotaState.objects.values().get(pk=self.state.pk))
