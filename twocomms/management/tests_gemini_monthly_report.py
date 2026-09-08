import datetime as dt
import io
import json

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import GeminiRequest, GeminiRequestAttempt
from management.services import gemini_monthly_report


UTC = dt.timezone.utc


class GeminiMonthlyReportTests(TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    def _attempt(self, suffix, *, created_at, model="gemini-3.7-flash", lane="live",
                 fsm="succeeded", failure_kind="", http_code=200, started=True,
                 latency_ms=100):
        graph = GeminiRequest.objects.create(request_id=f"monthly-private-{suffix}")
        row = GeminiRequestAttempt.objects.create(
            request_id=graph.request_id,
            request_graph=graph,
            role="chat",
            key_name="PRIVATE_KEY_DO_NOT_OUTPUT",
            project_identity="private-project-do-not-output",
            model=model,
            outcome=fsm,
            fsm_state=fsm,
            lane=lane,
            failure_kind=failure_kind,
            http_code=http_code,
            latency_ms=latency_ms,
            not_attempted_reason="winner_found" if fsm == "not_attempted" else "",
            provider_started_at=created_at if started else None,
            dispatch_pacific_day=created_at.date() if started else None,
            finished_at=(
                created_at
                if started and fsm in gemini_monthly_report._TERMINAL_STATES
                else None
            ),
        )
        # The ledger deliberately protects immutable evidence through its ORM
        # contract.  This test-only timestamp fixture needs explicit SQL so it
        # can exercise UTC reporting boundaries without weakening that contract.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE management_geminirequestattempt SET created_at = %s WHERE id = %s",
                [created_at, row.pk],
            )
        return row

    def test_utc_half_open_window_includes_current_day_and_excludes_old_and_now(self):
        self._attempt("old", created_at=dt.datetime(2026, 9, 7, 23, 59, 59, tzinfo=UTC))
        self._attempt("first", created_at=dt.datetime(2026, 9, 8, 0, 0, tzinfo=UTC))
        self._attempt("now", created_at=self.now)
        payload = gemini_monthly_report.build_monthly_payload(days=1, now=self.now)
        self.assertEqual(payload["coverage"]["attempts"], 1)
        self.assertEqual(payload["window"]["start"], "2026-09-08T00:00:00+00:00")
        self.assertEqual(payload["window"]["end_exclusive"], "2026-09-08T12:00:00+00:00")

    def test_aggregates_actual_started_skipped_failed_success_and_latency_bands(self):
        at = self.now - dt.timedelta(minutes=1)
        self._attempt("success", created_at=at, latency_ms=900)
        self._attempt("failure", created_at=at, fsm="failed", failure_kind="http_5xx", http_code=503, latency_ms=2000)
        self._attempt("timeout", created_at=at, fsm="timeout_ambiguous", failure_kind="read_timeout", http_code=408, latency_ms=6000)
        self._attempt("skip", created_at=at, fsm="not_attempted", started=False)
        self._attempt("executing", created_at=at, fsm="provider_started", latency_ms=0)
        payload = gemini_monthly_report.build_monthly_payload(days=1, now=self.now)
        row = payload["daily"][0]
        self.assertEqual({key: row[key] for key in ("attempts", "started", "skipped", "succeeded", "failed")}, {
            "attempts": 5, "started": 4, "skipped": 1, "succeeded": 1, "failed": 2,
        })
        self.assertEqual(row["latency_samples"], 3)
        self.assertEqual(row["latency_unknown_count"], 1)
        self.assertEqual(row["latency_ms_total"], 8900)
        self.assertEqual(row["latency_ms_min"], 900)
        self.assertEqual(row["latency_ms_average"], round(8900 / 3, 2))
        self.assertEqual((row["latency_le_1000ms"], row["latency_1001_to_5000ms"], row["latency_gt_5000ms"]), (1, 1, 1))
        self.assertEqual({(item["failure_kind"], item["http_bucket"]) for item in payload["failures"]}, {("http_5xx", "5xx"), ("read_timeout", "4xx")})

    def test_unknown_values_are_bucketed_before_grouping_and_private_values_do_not_escape(self):
        self._attempt("private", created_at=self.now - dt.timedelta(seconds=1), model="secret-model-name", lane="secret-lane", fsm="failed", failure_kind="secret-error", http_code=418)
        payload = gemini_monthly_report.build_monthly_payload(days=1, now=self.now)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertIn('"model": "other"', serialized)
        self.assertIn('"lane": "other"', serialized)
        self.assertIn('"failure_kind": "other"', serialized)
        for private in ("monthly-private-private", "PRIVATE_KEY_DO_NOT_OUTPUT", "private-project-do-not-output", "secret-model-name", "secret-lane", "secret-error"):
            self.assertNotIn(private, serialized)

    def test_empty_coverage_is_explicit_without_fabricated_health(self):
        payload = gemini_monthly_report.build_monthly_payload(days=1, now=self.now)
        self.assertEqual(payload["coverage"], {"attempts": 0, "first_observed_at": None, "last_observed_at": None})
        self.assertEqual(payload["daily"], [])
        self.assertEqual(payload["failures"], [])
        self.assertEqual(payload["retention"]["automated_purge"], "not_configured")
        self.assertNotIn("stable", json.dumps(payload))

    def test_command_matches_builder_and_is_read_only(self):
        self._attempt("command", created_at=timezone.now() - dt.timedelta(seconds=1))
        with CaptureQueriesContext(connection) as captured:
            expected = gemini_monthly_report.build_monthly_payload(days=30)
        selects = [entry["sql"] for entry in captured.captured_queries]
        self.assertEqual(len(selects), 3)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT") for statement in selects))
        forbidden_columns = (
            "request_id", "client_id", "source_message_id", "key_name",
            "project_identity", "error_detail", "provider_reason",
        )
        self.assertTrue(
            all(
                all(column not in statement for column in forbidden_columns)
                for statement in selects
            )
        )
        output = io.StringIO()
        call_command("gemini_monthly_report", "--days", "30", stdout=output)
        actual = json.loads(output.getvalue())
        self.assertEqual(actual["coverage"]["attempts"], expected["coverage"]["attempts"])
        self.assertEqual(actual["daily"], expected["daily"])
        with self.assertRaisesMessage(Exception, "1 through 31"):
            call_command("gemini_monthly_report", "--days", "32", stdout=io.StringIO())
        for invalid in (True, 1.9, "1.9"):
            with self.assertRaisesMessage(ValueError, "1 through 31"):
                gemini_monthly_report.build_monthly_payload(days=invalid, now=self.now)
