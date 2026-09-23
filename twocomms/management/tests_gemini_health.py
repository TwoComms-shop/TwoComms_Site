import datetime
import json
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import GeminiRequestAttempt
from management.services import gemini_health


UTC = datetime.timezone.utc


class GeminiHealthSnapshotTests(TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

        self.pool = [
            {
                "key_name": key_name,
                "present": key_name in {"GEMINI_API", "GEMINI_API2", "GEMINI_API3"},
                "health_state": "available" if key_name != "GEMINI_API3" else "cooldown",
                "role": "chat",
                "project_group": "",
                "project_identity_known": False,
                "available": key_name != "GEMINI_API3",
                "cooldown_until": None,
                "cooldown_scope": "",
                "seconds_remaining": 0,
                "requests_today": 0,
                "last_status": "",
                "needs_topup": False,
                "last_ok_at": None,
                "last_probe_at": None,
                "last_probe_status": "",
                "last_probe_model": "",
                "last_probe_latency_ms": 0,
                "last_probe_finish_reason": "",
                "last_probe_http_code": None,
                "last_probe_error": "",
            }
            for key_name in gemini_health.KEY_ALIASES
        ]

    def _attempt(self, *, request_id, key_name, model, outcome, at, failure_kind="", role="chat", **kwargs):
        row = GeminiRequestAttempt.objects.create(
            request_id=request_id,
            role=role,
            key_name=key_name,
            model=model,
            outcome=outcome,
            failure_kind=failure_kind,
            http_code=kwargs.get("http_code"),
            provider_reason=kwargs.get("provider_reason", ""),
            decision=kwargs.get("decision", ""),
            latency_ms=kwargs.get("latency_ms", 0),
            candidate_index=kwargs.get("candidate_index", 0),
            winner_claimed=kwargs.get("winner_claimed", False),
            reply_message_id=kwargs.get("reply_message_id"),
            not_attempted_reason=kwargs.get("not_attempted_reason", ""),
        )
        # Historical fixture setup intentionally bypasses the public immutable
        # V2 manager; production writers may not rewrite attempt identity time.
        GeminiRequestAttempt._base_manager.filter(pk=row.pk).update(created_at=at)
        row.refresh_from_db()
        return row

    def _build(self):
        with patch.object(gemini_health.gemini_keys, "pool_status", return_value=self.pool):
            return gemini_health.build_snapshot(now=self.now)

    def _status_row(self, *, row_id, request_id, outcome, at):
        return {
            "id": row_id,
            "request_id": request_id,
            "key_name": "GEMINI_API",
            "model": "gemini-3.7-flash",
            "outcome": outcome,
            "failure_kind": "read_timeout" if outcome == "failed" else "",
            "latency_ms": 10,
            "created_at": at,
        }

    def test_status_is_terminal_when_a_later_failure_follows_success(self):
        rows = [
            self._status_row(
                row_id=1,
                request_id="first-success",
                outcome="succeeded",
                at=self.now - datetime.timedelta(minutes=10),
            ),
            self._status_row(
                row_id=2,
                request_id="later-failure",
                outcome="failed",
                at=self.now - datetime.timedelta(minutes=5),
            ),
        ]

        self.assertEqual(gemini_health._status_for_attempts(rows), "terminal")

    def test_runtime_live_state_keeps_transient_provider_failure_degraded(self):
        rows = [{
            "id": 1,
            "request_id": "fresh-503",
            "key_name": "GEMINI_API",
            "model": "gemini-3.7-flash",
            "outcome": "failed",
            "failure_kind": "http_5xx",
            "created_at": self.now - datetime.timedelta(minutes=1),
        }]

        self.assertEqual(
            gemini_health._runtime_live_state(rows, self.now),
            ("DEGRADED", None),
        )

    def test_runtime_live_state_keeps_terminal_failure_offline(self):
        rows = [{
            "id": 1,
            "request_id": "bad-key",
            "key_name": "GEMINI_API",
            "model": "gemini-3.7-flash",
            "outcome": "failed",
            "failure_kind": "invalid_key",
            "created_at": self.now - datetime.timedelta(minutes=1),
        }]

        self.assertEqual(
            gemini_health._runtime_live_state(rows, self.now),
            ("OFFLINE", None),
        )

    def test_success_then_failure_in_one_request_is_terminal(self):
        rows = [
            self._status_row(
                row_id=1,
                request_id="same-request",
                outcome="succeeded",
                at=self.now - datetime.timedelta(minutes=10),
            ),
            self._status_row(
                row_id=2,
                request_id="same-request",
                outcome="failed",
                at=self.now - datetime.timedelta(minutes=5),
            ),
        ]

        self.assertEqual(gemini_health._status_for_attempts(rows), "terminal")

    def test_failure_then_success_in_one_request_is_recovered(self):
        rows = [
            self._status_row(
                row_id=1,
                request_id="same-request",
                outcome="failed",
                at=self.now - datetime.timedelta(minutes=10),
            ),
            self._status_row(
                row_id=2,
                request_id="same-request",
                outcome="succeeded",
                at=self.now - datetime.timedelta(minutes=5),
            ),
        ]

        self.assertEqual(gemini_health._status_for_attempts(rows), "recovered")

    def test_separate_failure_and_success_recover_only_in_observation_order_and_bucket(self):
        failed_at = self.now - datetime.timedelta(minutes=10)
        success_at = self.now - datetime.timedelta(minutes=5)
        recovered_rows = [
            self._status_row(
                row_id=1,
                request_id="failure-request",
                outcome="failed",
                at=failed_at,
            ),
            self._status_row(
                row_id=2,
                request_id="success-request",
                outcome="succeeded",
                at=success_at,
            ),
        ]
        self.assertEqual(gemini_health._status_for_attempts(recovered_rows), "recovered")

        reversed_rows = [
            self._status_row(
                row_id=1,
                request_id="success-request",
                outcome="succeeded",
                at=failed_at,
            ),
            self._status_row(
                row_id=2,
                request_id="failure-request",
                outcome="failed",
                at=success_at,
            ),
        ]
        self.assertEqual(gemini_health._status_for_attempts(reversed_rows), "terminal")

    def test_failure_and_success_in_different_buckets_are_not_recovery(self):
        rows = [
            self._status_row(
                row_id=1,
                request_id="old-failure",
                outcome="failed",
                at=self.now - datetime.timedelta(hours=2),
            ),
            self._status_row(
                row_id=2,
                request_id="new-success",
                outcome="succeeded",
                at=self.now - datetime.timedelta(hours=1),
            ),
        ]

        self.assertEqual(gemini_health._status_for_attempts(rows), "success")

    def test_ordered_recovery_marks_both_cross_bucket_history_segments_amber(self):
        window_start = self.now - datetime.timedelta(hours=24)
        rows = [
            self._status_row(
                row_id=1,
                request_id="cross-bucket-request",
                outcome="failed",
                at=window_start + datetime.timedelta(hours=10, minutes=59),
            ),
            self._status_row(
                row_id=2,
                request_id="cross-bucket-request",
                outcome="succeeded",
                at=window_start + datetime.timedelta(hours=11, minutes=1),
            ),
        ]

        snapshot = gemini_health._model_snapshot(rows, window_start)

        self.assertEqual(snapshot["status"], "recovered")
        self.assertEqual(snapshot["history"][10]["status"], "recovered")
        self.assertEqual(snapshot["history"][11]["status"], "recovered")

    def test_blank_request_ids_cannot_prove_a_fallback_sequence(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="read_timeout",
            at=at,
        )
        self._attempt(
            request_id="",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="succeeded",
            at=at + datetime.timedelta(seconds=1),
        )

        self.assertIsNone(self._build()["fallback"])

    def test_summary_uses_the_same_rolling_bucket_boundaries_as_history(self):
        window_start = datetime.datetime(2026, 8, 17, 12, 30, tzinfo=UTC)
        rows = [
            self._status_row(
                row_id=1,
                request_id="separate-failure",
                outcome="failed",
                at=window_start + datetime.timedelta(hours=10, minutes=15),
            ),
            self._status_row(
                row_id=2,
                request_id="separate-success",
                outcome="succeeded",
                at=window_start + datetime.timedelta(hours=10, minutes=45),
            ),
        ]

        summary = gemini_health._summary([], rows, window_start=window_start)

        self.assertEqual(summary["models"]["gemini-3.7-flash"]["status"], "recovered")

    def test_model_recovered_count_requires_failure_before_success_with_id_tiebreak(self):
        at = self.now - datetime.timedelta(minutes=5)
        rows = [
            self._status_row(
                row_id=10,
                request_id="success-then-failure",
                outcome="succeeded",
                at=at,
            ),
            self._status_row(
                row_id=11,
                request_id="success-then-failure",
                outcome="failed",
                at=at,
            ),
            self._status_row(
                row_id=20,
                request_id="failure-then-success",
                outcome="failed",
                at=at,
            ),
            self._status_row(
                row_id=21,
                request_id="failure-then-success",
                outcome="succeeded",
                at=at,
            ),
        ]

        snapshot = gemini_health._model_snapshot(
            rows,
            self.now - datetime.timedelta(hours=24),
        )

        self.assertEqual(snapshot["recovered"], 1)

    def test_summary_latest_observation_uses_attempt_sort_key(self):
        first_at = datetime.datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
        second_at = datetime.datetime(2026, 8, 18, 11, 0, tzinfo=UTC)
        rows = [
            self._status_row(
                row_id=1,
                request_id="lower-id",
                outcome="succeeded",
                at=first_at,
            ),
            self._status_row(
                row_id=2,
                request_id="higher-id",
                outcome="succeeded",
                at=second_at,
            ),
        ]

        def marker(value):
            return "higher-id" if value is second_at else "lower-id"

        with patch.object(gemini_health, "_iso", side_effect=marker):
            summary = gemini_health._summary([], rows)

        self.assertEqual(
            summary["models"]["gemini-3.7-flash"]["last_observation_at"],
            "higher-id",
        )

    def test_empty_pool_is_stable_six_rows_with_gray_24_bucket_histories(self):
        snapshot = self._build()

        self.assertEqual(snapshot["schema_version"], 5)
        self.assertEqual(snapshot["window"]["hours"], 24)
        self.assertEqual(snapshot["window"]["bucket_count"], 24)
        self.assertEqual([row["alias"] for row in snapshot["keys"]], [
            "API key 1", "API key 2", "API key 3",
            "API key 4", "API key 5", "API key 6",
        ])
        self.assertEqual(len(snapshot["keys"]), 6)
        for row in snapshot["keys"]:
            for model in gemini_health.DISPLAY_MODELS:
                model_snapshot = row["models"][model]
                self.assertEqual(model_snapshot["status"], "no_observation")
                self.assertEqual(
                    [bucket["status"] for bucket in model_snapshot["history"]],
                    ["no_observation"] * 24,
                )
        self.assertEqual(
            snapshot["summary"]["models"]["gemini-3.7-flash"]["status"],
            "insufficient_observations",
        )
        self.assertEqual(
            snapshot["summary"]["models"]["gemini-3.6-flash"]["status"],
            "insufficient_observations",
        )

    def test_success_recovered_retry_and_terminal_failure_have_distinct_statuses(self):
        self._attempt(
            request_id="success-request",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="succeeded",
            at=self.now - datetime.timedelta(minutes=20),
            latency_ms=111,
        )
        retry_at = self.now - datetime.timedelta(hours=2, minutes=10)
        self._attempt(
            request_id="recovered-request",
            key_name="GEMINI_API2",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="read_timeout",
            at=retry_at,
        )
        self._attempt(
            request_id="recovered-request",
            key_name="GEMINI_API2",
            model="gemini-3.7-flash",
            outcome="succeeded",
            at=retry_at + datetime.timedelta(minutes=1),
            latency_ms=222,
        )
        self._attempt(
            request_id="terminal-request",
            key_name="GEMINI_API3",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="quota_429",
            at=self.now - datetime.timedelta(hours=3),
        )

        snapshot = self._build()
        by_alias = {row["alias"]: row for row in snapshot["keys"]}
        self.assertEqual(
            by_alias["API key 1"]["models"]["gemini-3.7-flash"]["status"],
            "success",
        )
        self.assertEqual(
            by_alias["API key 2"]["models"]["gemini-3.7-flash"]["status"],
            "recovered",
        )
        self.assertEqual(
            by_alias["API key 3"]["models"]["gemini-3.7-flash"]["status"],
            "terminal",
        )

    def test_fallback_uses_neutral_timeout_reason_for_actual_models(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="proven-fallback",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="read_timeout",
            http_code=503,
            provider_reason="provider secret detail must not escape",
            at=at,
        )
        self._attempt(
            request_id="proven-fallback",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="succeeded",
            at=at + datetime.timedelta(seconds=2),
        )
        snapshot = self._build()
        fallback = snapshot["fallback"]
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback["from_model"], "gemini-3.7-flash")
        self.assertEqual(fallback["to_model"], "gemini-3.6-flash")
        self.assertEqual(fallback["reason"], "model timeout")
        self.assertEqual(fallback["http_code"], 503)
        serialized = json.dumps(snapshot)
        self.assertNotIn("provider secret detail", serialized)
        self.assertEqual(
            snapshot["latest_route"]["steps"][0]["failure_kind"],
            "read_timeout",
        )
        self.assertNotIn("GEMINI_API", serialized)
        self.assertNotIn("secret", serialized)

    def test_fallback_tie_break_uses_persisted_attempt_id_order(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="tie-break",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="read_timeout",
            at=at,
        )
        self._attempt(
            request_id="tie-break",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="succeeded",
            at=at,
        )

        self._attempt(
            request_id="later-tie-break",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="quota_429",
            at=at,
        )
        self._attempt(
            request_id="later-tie-break",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="succeeded",
            at=at,
        )

        snapshot = self._build()
        self.assertEqual(snapshot["fallback"]["reason"], "quota cooldown")

    def test_manual_key_attempts_are_excluded_from_all_six_key_evidence(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="manual-fallback",
            key_name="(manual)",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="read_timeout",
            at=at,
        )
        self._attempt(
            request_id="manual-fallback",
            key_name="(manual)",
            model="gemini-3.6-flash",
            outcome="succeeded",
            at=at + datetime.timedelta(seconds=1),
        )

        snapshot = self._build()
        self.assertEqual(snapshot["summary"]["observations"], 0)
        self.assertIsNone(snapshot["fallback"])
        for row in snapshot["keys"]:
            for model in gemini_health.DISPLAY_MODELS:
                self.assertEqual(row["models"][model]["observations"], 0)

    def test_window_and_query_cap_ignore_old_and_excess_rows(self):
        self._attempt(
            request_id="old",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="succeeded",
            at=self.now - datetime.timedelta(hours=25),
        )
        for index in range(4):
            self._attempt(
                request_id=f"recent-{index}",
                key_name="GEMINI_API2",
                model="gemini-3.7-flash",
                outcome="succeeded",
                at=self.now - datetime.timedelta(minutes=index + 1),
            )

        with patch.object(gemini_health, "ATTEMPT_QUERY_CAP", 2):
            snapshot = self._build()

        by_alias = {row["alias"]: row for row in snapshot["keys"]}
        self.assertEqual(
            by_alias["API key 1"]["models"]["gemini-3.7-flash"]["status"],
            "no_observation",
        )
        self.assertEqual(
            by_alias["API key 2"]["models"]["gemini-3.7-flash"]["observations"],
            2,
        )

    def test_runtime_query_cap_cannot_evict_fresh_metadata_evidence(self):
        self._attempt(
            request_id="metadata-ready-before-chat-burst",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="succeeded",
            role="health_metadata",
            at=self.now - datetime.timedelta(minutes=10),
        )
        for index in range(3):
            self._attempt(
                request_id=f"newer-runtime-{index}",
                key_name="GEMINI_API2",
                model="gemini-3.7-flash",
                outcome="succeeded",
                at=self.now - datetime.timedelta(minutes=index + 1),
            )

        with patch.object(gemini_health, "ATTEMPT_QUERY_CAP", 2):
            snapshot = self._build()

        key = snapshot["keys"][0]
        self.assertEqual(key["live_state"], "READY")
        self.assertEqual(
            key["metadata_models"]["gemini-3.7-flash"]["observations"],
            1,
        )

    def test_snapshot_is_json_safe_and_pool_state_is_not_secret_bearing(self):
        snapshot = self._build()
        json.dumps(snapshot)
        serialized = json.dumps(snapshot)
        self.assertNotIn("key_value", serialized)
        self.assertNotIn("api_key", serialized.lower())

    def test_public_projection_recursively_redacts_keys_and_nested_values(self):
        projected = gemini_health.public_projection({
            "route_GEMINI_API": [
                {"reason": "timeout on gemini_api2"},
                {"status": {"GEMINI_API3": "failed GEMINI_API4"}},
            ],
        })

        serialized = json.dumps(projected, sort_keys=True)
        upper_serialized = serialized.upper()
        for env_alias in gemini_health.KEY_ALIASES:
            self.assertNotIn(env_alias, upper_serialized)
        for env_alias in gemini_health.KEY_ALIASES[:4]:
            self.assertIn(
                gemini_health.public_key_reference(env_alias),
                serialized,
            )

    def test_public_projection_preserves_values_when_redacted_keys_collide(self):
        safe_key = gemini_health.public_key_reference("GEMINI_API")

        projected = gemini_health.public_projection({
            "GEMINI_API": "from internal alias",
            safe_key: "already public",
        })

        self.assertEqual(len(projected), 2)
        self.assertEqual(set(projected.values()), {
            "from internal alias",
            "already public",
        })
        self.assertIn(safe_key, projected)
        self.assertIn(f"{safe_key} [2]", projected)

    def test_manual_attempt_has_no_public_route_step_or_seventh_slot(self):
        row = {
            "key_name": "(manual)",
            "model": "gemini-3.5-flash-lite",
            "outcome": "succeeded",
            "failure_kind": "",
            "not_attempted_reason": "",
            "candidate_index": 1,
        }

        self.assertIsNone(gemini_health._public_route_step(row))

    def test_metadata_failure_then_secondary_success_is_degraded(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="metadata-fallback",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="timeout",
            role="health_metadata",
            at=at,
        )
        self._attempt(
            request_id="metadata-fallback",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="succeeded",
            role="health_metadata",
            at=at + datetime.timedelta(seconds=1),
        )

        row = self._build()["keys"][0]

        self.assertEqual(row["live_state"], "DEGRADED")
        self.assertEqual(row["active_model"], "gemini-3.6-flash")
        self.assertEqual(row["models"]["gemini-3.7-flash"]["observations"], 0)
        self.assertEqual(row["models"]["gemini-3.6-flash"]["observations"], 0)
        self.assertEqual(row["metadata_models"]["gemini-3.7-flash"]["observations"], 1)
        self.assertEqual(row["metadata_models"]["gemini-3.6-flash"]["observations"], 1)
        self.assertFalse(row["generation_quota_proven"])

    def test_runtime_primary_failure_then_secondary_success_is_degraded(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="runtime-fallback",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            at=at,
        )
        self._attempt(
            request_id="runtime-fallback",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="succeeded",
            role="chat",
            at=at + datetime.timedelta(seconds=1),
        )

        row = self._build()["keys"][0]

        self.assertEqual(row["live_state"], "DEGRADED")
        self.assertEqual(row["active_model"], "gemini-3.6-flash")
        self.assertTrue(row["generation_quota_proven"])

    def test_ordinary_chain_lite_failure_then_flash_winner_is_degraded(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="ordinary-model-fallback",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=1,
            at=at,
        )
        self._attempt(
            request_id="ordinary-model-fallback",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=7,
            winner_claimed=True,
            at=at + datetime.timedelta(seconds=1),
        )

        snapshot = self._build()
        winner_row = snapshot["keys"][1]

        self.assertEqual(winner_row["live_state"], "DEGRADED")
        self.assertEqual(winner_row["active_model"], "gemini-3.5-flash")
        self.assertEqual(snapshot["fallback"]["from_model"], "gemini-3.5-flash-lite")
        self.assertEqual(snapshot["fallback"]["to_model"], "gemini-3.5-flash")
        self.assertEqual(snapshot["fallback"]["reason"], "model timeout")

    def test_complex_chain_37_failure_then_lite_winner_is_degraded(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="complex-model-fallback",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=1,
            at=at,
        )
        self._attempt(
            request_id="complex-model-fallback",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            role="chat",
            candidate_index=19,
            winner_claimed=True,
            at=at + datetime.timedelta(seconds=1),
        )

        winner_row = self._build()["keys"][1]

        self.assertEqual(winner_row["live_state"], "DEGRADED")
        self.assertEqual(winner_row["active_model"], "gemini-3.5-flash-lite")

    def test_analysis_chain_36_failure_then_35_winner_is_degraded(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="analysis-model-fallback",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="failed",
            failure_kind="quota_429",
            role="analysis",
            candidate_index=1,
            at=at,
        )
        self._attempt(
            request_id="analysis-model-fallback",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash",
            outcome="succeeded",
            role="analysis",
            candidate_index=7,
            winner_claimed=True,
            at=at + datetime.timedelta(seconds=1),
        )

        winner_row = self._build()["keys"][1]

        self.assertEqual(winner_row["live_state"], "DEGRADED")
        self.assertEqual(winner_row["active_model"], "gemini-3.5-flash")

    def test_same_model_project_rotation_keeps_winner_live(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="same-model-rotation",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed",
            failure_kind="quota_429",
            role="chat",
            candidate_index=1,
            at=at,
        )
        self._attempt(
            request_id="same-model-rotation",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            role="chat",
            candidate_index=2,
            winner_claimed=True,
            at=at + datetime.timedelta(seconds=1),
        )

        snapshot = self._build()
        winner_row = snapshot["keys"][1]

        self.assertEqual(winner_row["live_state"], "LIVE")
        self.assertEqual(winner_row["active_model"], "gemini-3.5-flash-lite")
        self.assertIsNone(snapshot["fallback"])

    def test_new_same_model_rotation_does_not_inherit_older_model_fallback(self):
        old_at = self.now - datetime.timedelta(minutes=12)
        self._attempt(
            request_id="older-model-fallback",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=1,
            at=old_at,
        )
        self._attempt(
            request_id="older-model-fallback",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=7,
            winner_claimed=True,
            at=old_at + datetime.timedelta(seconds=1),
        )
        current_at = self.now - datetime.timedelta(minutes=2)
        self._attempt(
            request_id="newer-project-rotation",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed",
            failure_kind="quota_429",
            role="chat",
            candidate_index=1,
            at=current_at,
        )
        self._attempt(
            request_id="newer-project-rotation",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            role="chat",
            candidate_index=2,
            winner_claimed=True,
            at=current_at + datetime.timedelta(seconds=1),
        )

        snapshot = self._build()

        self.assertEqual(
            snapshot["latest_route"]["request_ref"],
            gemini_health.public_request_reference("newer-project-rotation"),
        )
        self.assertEqual(
            {step["model"] for step in snapshot["latest_route"]["steps"]},
            {"gemini-3.5-flash-lite"},
        )
        self.assertIsNone(snapshot["fallback"])

    def test_old_late_failed_and_success_losers_cannot_replace_newer_winner(self):
        old_at = self.now - datetime.timedelta(minutes=12)
        self._attempt(
            request_id="older-with-late-losers",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=1,
            at=old_at,
        )
        self._attempt(
            request_id="older-with-late-losers",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=7,
            winner_claimed=True,
            at=old_at + datetime.timedelta(seconds=1),
        )
        new_at = self.now - datetime.timedelta(minutes=5)
        self._attempt(
            request_id="newer-logical-request",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            role="chat",
            candidate_index=1,
            winner_claimed=True,
            at=new_at,
        )
        self._attempt(
            request_id="older-with-late-losers",
            key_name="GEMINI_API2",
            model="gemini-3.6-flash",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=13,
            at=self.now - datetime.timedelta(minutes=2),
        )
        self._attempt(
            request_id="older-with-late-losers",
            key_name="GEMINI_API2",
            model="gemini-3.7-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=19,
            at=self.now - datetime.timedelta(minutes=1),
        )

        snapshot = self._build()
        winner_row = snapshot["keys"][1]

        self.assertEqual(
            snapshot["latest_route"]["request_ref"],
            gemini_health.public_request_reference("newer-logical-request"),
        )
        self.assertEqual(
            [step["model"] for step in snapshot["latest_route"]["steps"]],
            ["gemini-3.5-flash-lite"],
        )
        self.assertIsNone(snapshot["fallback"])
        self.assertEqual(winner_row["live_state"], "LIVE")
        self.assertEqual(
            winner_row["active_model"],
            "gemini-3.5-flash-lite",
        )

    def test_linked_request_graph_created_at_is_the_primary_order_anchor(self):
        old_graph_created = self.now - datetime.timedelta(minutes=12)
        new_graph_created = self.now - datetime.timedelta(minutes=5)
        rows = [
            {
                "id": 1,
                "request_id": "old-linked",
                "request_graph_id": 10,
                "request_graph__created_at": old_graph_created,
                "request_graph__provider_phase_started_at": None,
                "provider_started_at": None,
                "key_name": "GEMINI_API",
                "created_at": self.now - datetime.timedelta(minutes=1),
            },
            {
                "id": 2,
                "request_id": "new-linked",
                "request_graph_id": 11,
                "request_graph__created_at": new_graph_created,
                "request_graph__provider_phase_started_at": None,
                "provider_started_at": None,
                "key_name": "GEMINI_API",
                "created_at": self.now - datetime.timedelta(minutes=4),
            },
        ]

        latest = gemini_health._latest_request_rows(rows)
        global_latest = gemini_health._latest_global_request(rows)

        self.assertEqual(latest[0]["request_id"], "new-linked")
        self.assertEqual(global_latest[0], "new-linked")

    def test_claimed_winner_beats_a_later_successful_loser(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="hedged-winner",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=1,
            at=at,
        )
        self._attempt(
            request_id="hedged-winner",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=7,
            winner_claimed=True,
            at=at + datetime.timedelta(seconds=1),
        )
        self._attempt(
            request_id="hedged-winner",
            key_name="GEMINI_API2",
            model="gemini-3.6-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=13,
            at=at + datetime.timedelta(seconds=2),
        )

        snapshot = self._build()
        winner_row = snapshot["keys"][1]

        self.assertEqual(winner_row["live_state"], "DEGRADED")
        self.assertEqual(winner_row["active_model"], "gemini-3.5-flash")
        self.assertEqual(snapshot["fallback"]["from_model"], "gemini-3.5-flash-lite")
        self.assertEqual(snapshot["fallback"]["to_model"], "gemini-3.5-flash")

    def test_claimed_winner_beats_a_later_failed_loser_on_same_project(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="winner-before-late-failure",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=1,
            at=at,
        )
        self._attempt(
            request_id="winner-before-late-failure",
            key_name="GEMINI_API2",
            model="gemini-3.5-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=7,
            winner_claimed=True,
            at=at + datetime.timedelta(seconds=1),
        )
        self._attempt(
            request_id="winner-before-late-failure",
            key_name="GEMINI_API2",
            model="gemini-3.6-flash",
            outcome="failed",
            failure_kind="read_timeout",
            role="chat",
            candidate_index=13,
            at=at + datetime.timedelta(seconds=2),
        )

        winner_row = self._build()["keys"][1]

        self.assertEqual(winner_row["live_state"], "DEGRADED")
        self.assertEqual(winner_row["active_model"], "gemini-3.5-flash")

    def test_metadata_primary_failure_without_observed_fallback_is_stale(self):
        self._attempt(
            request_id="metadata-partial-deadline",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="failed",
            failure_kind="timeout",
            role="health_metadata",
            at=self.now - datetime.timedelta(minutes=4),
        )

        row = self._build()["keys"][0]

        self.assertEqual(row["live_state"], "STALE")
        self.assertIsNone(row["active_model"])
        self.assertFalse(row["generation_quota_proven"])

    def test_not_needed_is_gray_and_not_a_failure(self):
        at = self.now - datetime.timedelta(minutes=4)
        self._attempt(
            request_id="metadata-primary-ok",
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            outcome="skipped",
            failure_kind="not_needed",
            role="health_metadata",
            at=at,
        )

        row = self._build()["keys"][0]
        model = row["metadata_models"]["gemini-3.6-flash"]

        self.assertEqual(model["status"], "not_needed")
        self.assertEqual(model["failures"], 0)
        self.assertEqual(model["skipped"], 1)
        self.assertEqual(set(bucket["status"] for bucket in model["history"]), {"no_observation", "not_needed"})

    def test_metadata_success_is_ready_for_every_model_and_quota_unproven(self):
        for model in gemini_health.DISPLAY_MODELS:
            with self.subTest(model=model):
                GeminiRequestAttempt.objects.all().delete()
                self._attempt(
                    request_id=f"metadata-ready-{model}",
                    key_name="GEMINI_API",
                    model=model,
                    outcome="succeeded",
                    role="health_metadata",
                    at=self.now - datetime.timedelta(minutes=4),
                )

                row = self._build()["keys"][0]

                self.assertEqual(row["live_state"], "READY")
                self.assertEqual(row["active_model"], model)
                self.assertEqual(row["models"][model]["observations"], 0)
                self.assertEqual(row["metadata_models"][model]["observations"], 1)
                self.assertFalse(row["generation_quota_proven"])

    def test_fresh_metadata_does_not_mask_stale_generation_state(self):
        self._attempt(
            request_id="old-generation",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            role="chat",
            at=self.now - datetime.timedelta(hours=3),
        )
        self._attempt(
            request_id="fresh-metadata",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="succeeded",
            role="health_metadata",
            at=self.now - datetime.timedelta(minutes=4),
        )

        row = self._build()["keys"][0]

        self.assertEqual(row["live_state"], "STALE")
        self.assertEqual(row["source"], "generation")
        self.assertEqual(row["freshness"], "stale")
        self.assertFalse(row["generation_quota_proven"])
        self.assertEqual(
            row["metadata_models"]["gemini-3.7-flash"]["successes"],
            1,
        )

    def test_legacy_health_probe_is_capability_only_and_lite_generation_is_visible(self):
        self._attempt(
            request_id="manual-capability",
            key_name="GEMINI_API",
            model="gemini-3.7-flash",
            outcome="succeeded",
            role="health_probe",
            at=self.now - datetime.timedelta(minutes=2),
        )
        self._attempt(
            request_id="real-lite-generation",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            role="chat",
            at=self.now - datetime.timedelta(minutes=3),
        )

        row = self._build()["keys"][0]

        self.assertEqual(row["live_state"], "LIVE")
        self.assertEqual(row["active_model"], "gemini-3.5-flash-lite")
        self.assertEqual(row["source"], "generation")
        self.assertTrue(row["generation_quota_proven"])
        self.assertEqual(
            row["other_model_usage"]["gemini-3.5-flash-lite"]["successes"],
            1,
        )
        self.assertEqual(
            row["metadata_models"]["gemini-3.7-flash"]["successes"],
            1,
        )

    def test_success_then_audited_winner_remainder_stays_live(self):
        succeeded_at = self.now - datetime.timedelta(minutes=3)
        self._attempt(
            request_id="winner-with-plan",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            role="chat",
            at=succeeded_at,
        )
        remainder = self._attempt(
            request_id="winner-with-plan",
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="not_attempted",
            role="chat",
            at=succeeded_at + datetime.timedelta(seconds=1),
        )
        remainder.not_attempted_reason = "winner_found"
        remainder.save(update_fields=["not_attempted_reason"])

        row = self._build()["keys"][0]

        self.assertEqual(row["live_state"], "LIVE")
        self.assertEqual(row["active_model"], "gemini-3.5-flash-lite")
        model = row["other_model_usage"]["gemini-3.5-flash-lite"]
        self.assertEqual(model["successes"], 1)
        self.assertEqual(model["failures"], 0)
        self.assertEqual(model["skipped"], 1)

    def test_latest_route_preserves_full_cross_key_model_chain(self):
        at = self.now - datetime.timedelta(minutes=3)
        rows = (
            ("GEMINI_API", "gemini-3.7-flash", "failed", "quota_429", 1),
            ("GEMINI_API2", "gemini-3.7-flash", "failed", "permission_denied", 2),
            ("GEMINI_API3", "gemini-3.5-flash-lite", "succeeded", "", 3),
        )
        for offset, (key, model, outcome, failure, candidate_index) in enumerate(rows):
            self._attempt(
                request_id="cross-key-chain",
                key_name=key,
                model=model,
                outcome=outcome,
                failure_kind=failure,
                role="chat",
                candidate_index=candidate_index,
                at=at + datetime.timedelta(seconds=offset),
            )

        snapshot = self._build()

        self.assertEqual(
            [step["model"] for step in snapshot["latest_route"]["steps"]],
            [
                "gemini-3.7-flash",
                "gemini-3.7-flash",
                "gemini-3.5-flash-lite",
            ],
        )
        self.assertEqual(
            [step["slot_id"] for step in snapshot["latest_route"]["steps"]],
            list(gemini_health.SLOT_IDS[:3]),
        )
        self.assertEqual(snapshot["fallback"]["from_model"], "gemini-3.7-flash")
        self.assertEqual(snapshot["fallback"]["to_model"], "gemini-3.5-flash-lite")

    def test_health_route_projects_request_and_untrusted_enums_safely(self):
        request_id = "route-GEMINI_API2-private"
        at = self.now - datetime.timedelta(minutes=3)
        self._attempt(
            request_id=request_id,
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            outcome="failed_GEMINI_API3",
            failure_kind="read_timeout_GEMINI_API4",
            role="chat",
            candidate_index=1,
            at=at,
        )
        self._attempt(
            request_id=request_id,
            key_name="GEMINI_API2",
            model="gemini-3.5-flash",
            outcome="succeeded",
            role="chat",
            candidate_index=7,
            winner_claimed=True,
            at=at + datetime.timedelta(seconds=1),
        )
        self._attempt(
            request_id=request_id,
            key_name="GEMINI_API3",
            model="gemini-3.5-flash",
            outcome="not_attempted",
            failure_kind="not_needed",
            not_attempted_reason="winner_GEMINI_API5",
            role="chat",
            candidate_index=8,
            at=at + datetime.timedelta(seconds=2),
        )

        snapshot = self._build()
        route = snapshot["latest_route"]

        self.assertNotIn("request_id", route)
        self.assertEqual(
            route["request_ref"],
            gemini_health.public_request_reference(request_id),
        )
        self.assertTrue(route["request_ref"].startswith("greq_"))
        self.assertNotIn(request_id, json.dumps(snapshot, sort_keys=True))
        first, _winner, skipped = route["steps"]
        self.assertEqual(first["outcome"], "unknown")
        self.assertEqual(first["failure_kind"], "other")
        self.assertEqual(first["reason"], "unknown")
        self.assertEqual(skipped["outcome"], "not_attempted")
        self.assertEqual(skipped["failure_kind"], "not_needed")
        self.assertEqual(skipped["not_attempted_reason"], "other")
        self.assertEqual(skipped["reason"], "other")
        serialized = json.dumps(snapshot, sort_keys=True)
        for env_alias in gemini_health.KEY_ALIASES:
            self.assertNotIn(env_alias, serialized)

    def test_snapshot_reports_latest_metadata_batch_completeness(self):
        completed_at = self.now - datetime.timedelta(minutes=3)
        request_suffixes = ("I", "2", "3", "4", "5", "6")
        for index, (suffix, key_name) in enumerate(zip(request_suffixes, gemini_health.KEY_ALIASES, strict=True), start=1):
            self._attempt(
                request_id=f"meta-2026081812-a1b2c3d4-{suffix}",
                key_name=key_name,
                model="gemini-3.7-flash",
                outcome="succeeded",
                role="health_metadata",
                at=completed_at + datetime.timedelta(seconds=index),
            )

        snapshot = self._build()

        self.assertEqual(
            snapshot["latest_metadata_batch"],
            {
                "checked_aliases": 6,
                "expected_aliases": 6,
                "complete": True,
                "completed_at": (completed_at + datetime.timedelta(seconds=6)).isoformat(),
            },
        )
