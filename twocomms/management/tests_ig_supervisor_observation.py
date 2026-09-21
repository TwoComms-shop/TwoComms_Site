import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from management.services.ig_supervisor_observation import (
    MAX_STATE_BYTES,
    read_supervisor_observation,
)


class SupervisorObservationTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "tmp").mkdir()
        self.state_path = self.root / "tmp" / "ig_bot_supervisor_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_state(self, payload):
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_projects_allowlisted_metadata_and_pid_correspondence(self):
        self.write_state(
            {
                "version": 1,
                "event": "child_started",
                "observed_at": 100.0,
                "supervisor_release_sha": "A" * 40,
                "child_release_sha": "b" * 12,
                "restart_count": 3,
                "child_pid": 421,
                "child_start_ticks": 999,
                "supervisor_pid": 9999,
                "free_text": "do not expose",
            }
        )

        observation = read_supervisor_observation(
            self.root,
            expected_child_pid=421,
            now=110.0,
        )

        self.assertEqual(
            observation,
            {
                "available": True,
                "status": "current",
                "observed_at": 100.0,
                "last_ensure_seen_at": None,
                "supervisor_release_sha": "a" * 40,
                "child_release_sha": "b" * 12,
                "restart_count": 3,
                "child_pid": 421,
                "child_pid_matches_expected": True,
            },
        )
        self.assertNotIn("free_text", observation)
        self.assertNotIn("child_start_ticks", observation)

    def test_stale_observation_is_available_but_not_current(self):
        self.write_state({"observed_at": 100.0, "last_ensure_seen_at": 250.0, "child_pid": 421})

        observation = read_supervisor_observation(self.root, now=281.0)

        self.assertTrue(observation["available"])
        self.assertEqual(observation["status"], "current")
        self.assertEqual(observation["child_pid"], 421)
        self.assertEqual(observation["last_ensure_seen_at"], 250.0)
        self.assertIsNone(observation["child_pid_matches_expected"])

    def test_stale_ensure_heartbeat_remains_stale_even_with_recent_child_event(self):
        self.write_state({"observed_at": 275.0, "last_ensure_seen_at": 100.0, "child_pid": 421})

        observation = read_supervisor_observation(self.root, now=281.0)

        self.assertTrue(observation["available"])
        self.assertEqual(observation["status"], "stale")
        self.assertEqual(observation["observed_at"], 275.0)
        self.assertEqual(observation["last_ensure_seen_at"], 100.0)

    def test_legacy_state_without_ensure_heartbeat_uses_event_time(self):
        self.write_state({"observed_at": 100.0, "child_pid": 421})

        observation = read_supervisor_observation(self.root, now=281.0)

        self.assertEqual(observation["status"], "stale")
        self.assertIsNone(observation["last_ensure_seen_at"])

    def test_expected_pid_mismatch_wins_over_current_age(self):
        self.write_state({"observed_at": 100.0, "child_pid": 421})

        observation = read_supervisor_observation(
            self.root,
            expected_child_pid=422,
            now=101.0,
        )

        self.assertEqual(observation["status"], "mismatch")
        self.assertFalse(observation["child_pid_matches_expected"])

    def test_missing_malformed_and_oversized_state_are_unobserved(self):
        self.assertEqual(read_supervisor_observation(self.root)["status"], "unobserved")

        self.state_path.write_text("not-json", encoding="utf-8")
        self.assertFalse(read_supervisor_observation(self.root)["available"])

        self.write_state(
            {
                "observed_at": "nan",
                "child_pid": True,
                "restart_count": -1,
                "supervisor_release_sha": "secret-token",
            }
        )
        observation = read_supervisor_observation(self.root, expected_child_pid=1)
        self.assertEqual(observation["status"], "unobserved")
        self.assertFalse(observation["available"])

        self.state_path.write_bytes(b"{" + b"a" * MAX_STATE_BYTES + b"}")
        self.assertEqual(read_supervisor_observation(self.root)["status"], "unobserved")

    def test_invalid_optional_values_are_sanitized_without_exposing_input(self):
        self.write_state(
            {
                "observed_at": 100.0,
                "supervisor_release_sha": "not-a-sha",
                "child_release_sha": "../tmp/private-token",
                "restart_count": "3",
                "child_pid": "421",
            }
        )

        observation = read_supervisor_observation(self.root, now=101.0, expected_child_pid=421)

        self.assertTrue(observation["available"])
        self.assertEqual(observation["status"], "mismatch")
        self.assertIsNone(observation["supervisor_release_sha"])
        self.assertIsNone(observation["child_release_sha"])
        self.assertIsNone(observation["restart_count"])
        self.assertIsNone(observation["child_pid"])
        self.assertFalse(observation["child_pid_matches_expected"])
