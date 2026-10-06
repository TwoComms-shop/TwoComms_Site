"""Overview evidence: liveness, obligations and capacity stay independent."""
from datetime import datetime, timedelta, timezone as dt_timezone
import json
from unittest.mock import Mock, patch

from django.core.cache import caches
from django.db import DatabaseError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.services import ig_overview_read_model as overview
from management.services.gemini_health import DISPLAY_MODELS, SLOT_IDS


NOW = datetime(2026, 10, 6, 12, tzinfo=dt_timezone.utc)


def owner_components():
    counts = dict.fromkeys(overview.BUCKETS, 0)
    return {
        "daemon": {"process_online": True, "main_healthy": True, "main_state": "idle",
            "process_age_seconds": 1, "main_age_seconds": 2, "alive_window_seconds": 150},
        "lanes": {"available": True, "bot_state": "running", "lanes": {
            "customer_revisions": {"available": True, "healthy": True, "state": "observed",
                "counts": counts, "sample_size": 0, "sample_limit": 3, "has_more": False,
                "risk_coverage_complete": True, "attention_total": 0, "attention_total_exact": True}}},
        "tasks": {"available": True, "tasks": []},
        "attention": {"available": True, "required": False, "count": 0, "exact": True},
        "memory_generation": {"available": True, "generation_enabled": False,
            "provider_admission_accepted": False, "exact": True,
            "counts": {"total": 3, "processing": 0, "due_unclaimed": 2, "deferred": 1, "unknown": 0}},
        "quotas": {"accounting": {"nonlive_admission_mode": "enforce", "nonlive_enforcement_active": True}, "models": []},
        "routes": {"routes": [{"task_class": "ordinary_live", "lane": "live",
            "effective_chain": [DISPLAY_MODELS[0]], "base_chain": [DISPLAY_MODELS[0]], "deadline_ms": 12000}]},
    }


class OverviewProjectionTests(SimpleTestCase):
    def compose(self, components=None, *, now=NOW):
        return overview.compose_overview_payload(components=components or owner_components(), captured_at=NOW, now=now)

    def test_fresh_healthy_daemon_coexists_with_real_unanswered_debt(self):
        components = owner_components()
        components["attention"].update(required=True, count=4, since=NOW - timedelta(hours=2))
        result = self.compose(components)
        self.assertTrue(result["components"]["daemon"]["data"]["healthy"])
        debt = result["components"]["attention"]["data"]
        self.assertTrue(debt["required"])
        self.assertEqual((debt["count"], debt["owner"], debt["waiting_age_seconds"]), (4, "manager", 7200))
        self.assertNotIn("healthy", result)
        self.assertEqual(result["components"]["daemon"]["data"]["technical_debt"]["state"], "not_requested")
        self.assertFalse(result["components"]["daemon"]["data"]["technical_debt"]["coverage_complete"])

    def test_bucket_meanings_and_incomplete_risk_do_not_become_queue_totals(self):
        components = owner_components()
        lane = components["lanes"]["lanes"]["customer_revisions"]
        lane.update(healthy=False, state="coverage_incomplete", has_more=True, sample_size=3,
            risk_coverage_complete=False, attention_total_exact=False, attention_total=0,
            risk_sample_size=3, risk_sample_limit=3, risk_has_more=True)
        lane["counts"].update(manager_owned=1, deferred=1, unknown=1)
        result = self.compose(components)["components"]["lanes"]["data"]["lanes"]["customer_revisions"]
        self.assertEqual(result["counts"]["runnable"], 0)
        self.assertEqual(result["counts"]["manager_owned"], 1)
        self.assertEqual(result["counts"]["deferred"], 1)
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(result["counts"]["historical"], 0)
        self.assertEqual(result["count_authority"], "sample")
        self.assertFalse(result["healthy"])
        self.assertFalse(result["attention_total_exact"])
        self.assertTrue(result["risk_has_more"])

    def test_disabled_memory_retains_dirty_backlog_without_health_or_admission_claim(self):
        result = self.compose()["components"]["memory_generation"]["data"]
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(result["counts"]["due_unclaimed"], 2)
        self.assertEqual(result["counts"]["total"], 3)
        self.assertIsNone(result["healthy"])
        self.assertIsNone(result["runnable"])
        self.assertFalse(result["due_unclaimed_is_admitted"])

    def test_six_slots_zero_usage_uncalibrated_tpm_is_no_generation_or_pooled_capacity(self):
        components = owner_components()
        model = DISPLAY_MODELS[0]
        metric = {"used": 0, "remaining": 999999, "limit": 1000, "complete": True,
            "reserved": 0, "uncertain": 0}
        pairs = [{"slot_id": slot, "configured": True, "identity_mapping": "explicit",
            "status": "available_assumed", "rpm": metric, "rpd": metric,
            "input_tpm": {**metric, "headroom_known": False, "observed_usage_known": True,
                "usage_source": "no_observed_usage", "calibration": "uncalibrated"},
            "nonlive_profile": {"calibration": "uncalibrated", "runtime_profile_binding": "matched",
                "eligible_prerequisites": False}} for slot in SLOT_IDS]
        components["quotas"]["models"] = [{"model": model, "projects": pairs,
            "rpm": {"remaining": 6000}, "rpd": {"remaining": 6000}}]
        capacity = self.compose(components)["components"]["quotas"]["data"]
        self.assertEqual(len(capacity["projects"]), 6)
        self.assertNotIn("rpm", capacity)
        self.assertNotIn("rpd", capacity)
        self.assertFalse(capacity["capacity_is_additive"])
        self.assertFalse(capacity["guaranteed_headroom"])
        for pair in capacity["projects"]:
            self.assertEqual(pair["input_tpm"]["used"], 0)
            self.assertIsNone(pair["input_tpm"]["remaining"])
            self.assertFalse(pair["input_tpm"]["headroom_known"])
            self.assertFalse(pair["generation_evidence_present"])
            self.assertIsNone(pair["last_generation_success"])
            self.assertEqual(pair["candidate_task_classes"], ["ordinary_live"])

    def test_policy_chain_and_last_actual_generation_remain_separate(self):
        components = owner_components()
        actual_model = DISPLAY_MODELS[-1]
        components["quotas"]["models"] = [{"model": actual_model, "projects": [{
            "slot_id": SLOT_IDS[0], "last_real_evidence": {"at": NOW - timedelta(minutes=10), "success": True}}]}]
        result = self.compose(components)
        route = result["components"]["routes"]["data"]["routes"][0]
        pair = result["components"]["quotas"]["data"]["projects"][0]
        self.assertEqual(route["effective_chain"], [DISPLAY_MODELS[0]])
        self.assertEqual(pair["model"], actual_model)
        self.assertEqual(pair["last_generation_age_seconds"], 600)
        self.assertTrue(pair["last_generation_success"])

    def test_cached_observation_ages_advance_without_refreshing_capture_or_source(self):
        components = owner_components()
        components["daemon"].update(process_age_seconds=145, main_age_seconds=146)
        original = self.compose(components)
        result = overview._refresh_payload(original, NOW + timedelta(seconds=10))
        self.assertEqual(result["captured_at"], original["captured_at"])
        self.assertEqual(result["components"]["daemon"]["observed_at"], NOW.isoformat())
        self.assertEqual(result["components"]["daemon"]["observation_age_seconds"], 10)
        self.assertEqual(result["components"]["daemon"]["data"]["main_age_seconds"], 156)
        self.assertFalse(result["components"]["daemon"]["data"]["healthy"])
        self.assertTrue(original["components"]["daemon"]["data"]["healthy"])

    def test_owner_failure_and_private_tainted_fields_are_unavailable_not_green_zero(self):
        components = owner_components()
        private = "SECRET private note customer 0501234567"
        components["lanes"] = {"available": False, "reason": private, "client_name": private}
        components["tasks"]["tasks"] = [{"key": "ig_checkout_reconcile", "state": "failed",
            "last_error_kind": private, "label": private, "manager_note": private}]
        components["daemon"].update(stalled_reason=private, process_pid=private)
        result = self.compose(components)
        self.assertNotIn(private, json.dumps(result))
        lane = result["components"]["lanes"]
        self.assertFalse(lane["available"])
        self.assertEqual(lane["reason"], "observation_unavailable")
        self.assertEqual(lane["data"], {})

    def test_missing_owner_does_not_mean_empty_or_disabled(self):
        result = overview.compose_overview_payload(components={}, captured_at=NOW)
        for component in result["components"].values():
            self.assertFalse(component["available"])
            self.assertEqual(component["data"], {})


class OverviewReadBoundaryTests(SimpleTestCase):
    def test_empty_primary_sample_cannot_hide_incomplete_secondary_risk_scan(self):
        from management.services.ig_lane_health import _sample
        result = _sample([], lambda row: ("runnable", NOW), now=NOW, threshold=300,
            observation_limit=3, risk_scan_complete=False, attention_total_exact=False)
        self.assertEqual(result["sample_size"], 0)
        self.assertEqual(result["attention_total"], 0)
        self.assertFalse(result["has_more"])
        self.assertFalse(result["healthy"])
        self.assertFalse(result["risk_coverage_complete"])
        self.assertEqual(result["state"], "coverage_incomplete")

    def test_budget_stops_before_sql64_plus_one(self):
        budget = overview._ReadBudget()
        execute = Mock(return_value=None)
        for name, limit in overview.QUERY_BUDGETS.items():
            budget.component = name
            for _ in range(limit):
                budget(execute, "SELECT 1", (), False, {})
        self.assertEqual(budget.total, 64)
        with self.assertRaisesMessage(DatabaseError, "query_budget_exceeded"):
            budget(execute, "SELECT 1", (), False, {})
        self.assertEqual(execute.call_count, 64)

    def test_no_effect_or_lock_sql_reaches_database(self):
        budget = overview._ReadBudget()
        budget.component = "attention"
        execute = Mock()
        for sql in ("UPDATE private SET x=1", "DELETE FROM private", "INSERT INTO private VALUES(1)",
                    "SELECT * FROM private FOR UPDATE", "CREATE TABLE private (x int)"):
            with self.assertRaisesMessage(DatabaseError, "read_only_violation"):
                budget(execute, sql, (), False, {})
        execute.assert_not_called()

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "overview-pure-cache"}})
    def test_global_cache_does_not_recollect_or_refresh_evidence_timestamp(self):
        caches["default"].clear()
        self.addCleanup(caches["default"].clear)
        with patch.object(overview, "_collect_components", return_value=(owner_components(), {"executed": 0, "limit": 64})) as collect:
            first = overview.build_overview_payload(now=NOW)
            second = overview.build_overview_payload(now=NOW + timedelta(seconds=5))
            third = overview.build_overview_payload(now=NOW + timedelta(seconds=31))
        self.assertEqual(collect.call_count, 2)
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["captured_at"], first["captured_at"])
        self.assertEqual(second["components"]["daemon"]["data"]["main_age_seconds"], 7)
        self.assertEqual(third["captured_at"], (NOW + timedelta(seconds=31)).isoformat())

    def test_optional_daemon_inventory_omission_is_explicit_and_default_unchanged(self):
        from management.services.ig_daemon_health import daemon_runtime_health_snapshot
        with patch("management.services.ig_daemon_health.technical_debt_snapshot", return_value={"coverage_complete": True}) as debt:
            compact = daemon_runtime_health_snapshot(now_epoch=NOW.timestamp(), include_technical_debt=False)
            debt.assert_not_called()
            self.assertEqual(compact["technical_debt"]["state"], "not_requested")
            self.assertFalse(compact["technical_debt"]["coverage_complete"])
            original = daemon_runtime_health_snapshot(now_epoch=NOW.timestamp())
            debt.assert_called_once_with(limit=100)
            self.assertTrue(original["technical_debt"]["coverage_complete"])


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_MEMORY_GENERATION_ENABLED=False,
                   IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=False, GEMINI_ACCOUNTING_V2_MODE="off")
class OverviewCanonicalReaderTests(TestCase):
    def setUp(self):
        from management.models import IgClient, InstagramBotSettings
        self.person = IgClient.objects.create(igsid="overview-private-client")
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True)

    def test_compact_job_sample_materialization_and_sql_are_bounded_default_retained(self):
        from management.models import IgBotNotification
        from management.services.ig_lane_health import _job_lane, SAMPLE_LIMIT
        for index in range(8):
            IgBotNotification.objects.create(dedupe_key=f"overview-fixture-{index}",
                event_type="test", payload={"text": "private"}, next_attempt_at=NOW + timedelta(days=1))
        kwargs = {"now": NOW, "statuses": ("pending",), "progress_field": "sent_at"}
        with CaptureQueriesContext(connection) as compact_queries:
            compact = _job_lane(IgBotNotification, **kwargs, observation_limit=3, include_progress=False)
        with CaptureQueriesContext(connection) as default_queries:
            original = _job_lane(IgBotNotification, **kwargs)
        self.assertEqual(len(compact_queries), 2)
        self.assertEqual(len(default_queries), 3)
        self.assertEqual(compact["sample_size"], 3)
        self.assertTrue(compact["has_more"])
        self.assertEqual(compact["counts"]["deferred"], 3)
        self.assertEqual(original["sample_limit"], SAMPLE_LIMIT)
        self.assertEqual(original["counts"]["deferred"], 8)
        self.assertTrue(any("LIMIT 4" in row["sql"].upper() for row in compact_queries))
        self.assertFalse(any("MAX(" in row["sql"].upper() for row in compact_queries))

    def test_manual_risk_overflow_can_have_zero_attention_but_never_claim_complete(self):
        from management.models import IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgTurnMessage, InstagramBotMessage
        from management.services.ig_lane_health import _revision_lane
        from management.services.ig_turn_revisions import create_collecting_revision
        for index in range(5):
            person = self.person if index == 0 else IgClient.objects.create(igsid=f"overview-risk-{index}")
            source = InstagramBotMessage.objects.create(client=person, sender_id=person.igsid,
                role="user", text="private", mid=f"overview-source-{index}")
            turn = IgCustomerTurn.objects.create(client=person, primary_source_message=source,
                window_started_at=NOW - timedelta(minutes=1), window_deadline=NOW)
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
            built = create_collecting_revision(turn, [source], now=NOW, bypass_quiet=True)
            self.assertIsNotNone(built.revision, built.reason)
            revision = built.revision
            IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(recovery_state="manual")
        with patch("management.services.ig_revision_recovery.execution_resume_is_current", return_value=True) as current:
            with CaptureQueriesContext(connection) as queries:
                compact = _revision_lane(now=NOW, settings_row=self.settings_row, allowed=set(), observation_limit=3, include_progress=False)
            compact_checks = current.call_count
            original = _revision_lane(now=NOW, settings_row=self.settings_row, allowed=set())
        self.assertEqual(compact["attention_total"], 0)
        self.assertFalse(compact["attention_total_exact"])
        self.assertFalse(compact["risk_coverage_complete"])
        self.assertFalse(compact["healthy"])
        self.assertEqual(compact["risk_sample_size"], 3)
        self.assertTrue(compact["risk_has_more"])
        self.assertLessEqual(compact_checks, 6)
        self.assertEqual(sum("LIMIT 4" in row["sql"].upper() for row in queries), 2)
        self.assertTrue(original["attention_total_exact"])
        self.assertTrue(original["risk_coverage_complete"])
        self.assertTrue(original["healthy"])
        self.assertEqual(original["sample_size"], 5)

    def test_compact_permission_reader_is_one_exists_without_hidden_detail_inventory(self):
        from management.models import IgPermissionTransitionJob
        from management.services.ig_permission_transitions import permission_transition_snapshot
        for index in range(6):
            IgPermissionTransitionJob.objects.create(kind=IgPermissionTransitionJob.Kind.GLOBAL_PAUSE,
                settings_id=self.settings_row.pk, dedupe_key=f"overview-permission-{index}",
                last_error_kind="PRIVATE owner error")
        with CaptureQueriesContext(connection) as queries:
            compact = permission_transition_snapshot(compact=True)
        self.assertEqual(len(queries), 1)
        self.assertIn("LIMIT 1", queries[0]["sql"].upper())
        self.assertTrue(compact["global_pause_pending"])
        self.assertIsNone(compact["pending"])
        self.assertIsNone(compact["error_kinds"])
        self.assertFalse(compact["detail_coverage_complete"])
        original = permission_transition_snapshot()
        self.assertEqual(original["pending"], 6)
        self.assertEqual(original["error_kinds"], ["PRIVATE owner error"])

    def test_real_facade_select_only_retains_reply_debt_and_disabled_generation_backlog(self):
        from management.models import IgFollowUpTask, IgClient
        from management.services.ig_response_debt import DEBT_REASON
        IgFollowUpTask.objects.create(client=self.person, kind="manager_task", reason=DEBT_REASON, due_at=NOW,
            event_occurred_at=NOW - timedelta(hours=1), event_key="overview-private-event",
            manager_context={"manager_note": "PRIVATE manager 0501234567"})
        IgClient.objects.filter(pk=self.person.pk).update(memory_dirty_at=NOW - timedelta(hours=2), memory_due_at=NOW)
        with (patch("requests.sessions.Session.post", side_effect=AssertionError("provider IO forbidden")) as provider,
              patch("management.services.instagram_bot.ingress_status", return_value={"healthy": True}),
              patch("management.services.instagram_bot.allowed_sender_ids", return_value=set()),
              patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False}),
              CaptureQueriesContext(connection) as queries):
            result = overview.build_overview_payload(now=NOW, use_cache=False)
        provider.assert_not_called()
        self.assertLessEqual(len(queries), 64)
        self.assertEqual(result["queries"]["executed"], len(queries))
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT ") for row in queries))
        self.assertTrue(result["components"]["attention"]["available"], result)
        self.assertEqual(result["components"]["attention"]["data"]["count"], 1)
        self.assertEqual(result["components"]["memory_generation"]["data"]["state"], "disabled")
        self.assertEqual(result["components"]["memory_generation"]["data"]["counts"]["due_unclaimed"], 1)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn(self.person.igsid, json.dumps(result))

    def test_caught_owner_budget_error_still_reports_unavailable_not_false_healthy(self):
        def excessive_reader(*, now):
            try:
                with connection.cursor() as cursor:
                    for _ in range(3):
                        cursor.execute("SELECT 1")
            except DatabaseError:
                return {"available": True, "healthy": True, "tasks": []}

        with (patch("management.services.ig_task_health.task_health_snapshot", side_effect=excessive_reader),
              patch("management.services.instagram_bot.ingress_status", return_value={"healthy": True}),
              patch("management.services.instagram_bot.allowed_sender_ids", return_value=set()),
              patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False})):
            result = overview.build_overview_payload(now=NOW, use_cache=False)
        self.assertFalse(result["components"]["tasks"]["available"])
        self.assertEqual(result["components"]["tasks"]["reason"], "query_budget_exceeded")
        self.assertEqual(result["queries"]["by_component"]["tasks"], 2)

    def test_human_unknown_uses_unresolved_case_predicate_not_unsettled_transport_or_source_age(self):
        from management.models import IgClient, IgFollowUpTask
        active = IgFollowUpTask.objects.create(client=self.person, kind="manager_task",
            reason="human_reply:delivery_unknown", due_at=NOW, event_key="overview-human-active")
        IgFollowUpTask.objects.create(client=self.person, kind="manager_task",
            reason="human_reply:delivery_unknown", due_at=NOW,
            event_key="overview-human-completed", status=IgFollowUpTask.Status.COMPLETED)
        erased = IgClient.objects.create(igsid="overview-erased", privacy_erasure_started_at=NOW)
        IgFollowUpTask.objects.create(client=erased, kind="manager_task",
            reason="human_reply:delivery_unknown", due_at=NOW, event_key="overview-human-erased")
        with CaptureQueriesContext(connection) as queries:
            captured = overview._reply_attention(NOW)
        self.assertEqual(len(queries), 2)
        self.assertEqual(captured["count"], 0)
        self.assertEqual(captured["human_unknown_cases"]["count"], 1)
        components = owner_components()
        components["attention"] = captured
        data = overview.compose_overview_payload(components=components, captured_at=NOW)["components"]["attention"]["data"]
        self.assertTrue(data["required"])
        self.assertFalse(data["reply_debt_required"])
        self.assertIsNone(data["waiting_age_seconds"])
        self.assertEqual(data["human_unknown_cases"]["oldest_created_at"], active.created_at.isoformat())
        self.assertNotIn("source_age_seconds", data["human_unknown_cases"])
        self.assertTrue(all("ig_humanreplycommand" not in row["sql"].lower() for row in queries))
        active.status = IgFollowUpTask.Status.COMPLETED
        active.save(update_fields=["status"])
        self.assertEqual(overview._reply_attention(NOW)["human_unknown_cases"]["count"], 0)
