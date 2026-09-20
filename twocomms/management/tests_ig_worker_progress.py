"""Independent worker progress cannot be borrowed from a fresh main loop."""
from unittest.mock import patch

from django.test import SimpleTestCase

from management.services.ig_daemon_health import daemon_runtime_health_snapshot
from management.services.ig_worker_progress import (
    WorkerProgressRegistry, WORKER_LIMITS, worker_health_snapshot, worker_iteration,
    worker_claim_admission,
)


class WorkerProgressTests(SimpleTestCase):
    def setUp(self):
        self.now = 1000.0
        self.registry = WorkerProgressRegistry(clock=lambda: self.now)
        self.registry.reset()

    def test_another_worker_and_main_progress_cannot_hide_stuck_analysis(self):
        self.registry.begin("analysis")
        self.now += 601
        for name in WORKER_LIMITS:
            if name != "analysis":
                self.registry.begin(name)
                self.registry.finish(name)
        payload = self.registry.snapshot()
        with patch("management.services.ig_daemon_health._cache_observation", side_effect=[
            ({"at": self.now, "worker_lanes": payload, "sentinel": 123.0}, 0),
            ({"at": self.now, "state": "idle"}, 0),
        ]):
            result = daemon_runtime_health_snapshot(now_epoch=self.now)
        self.assertTrue(result["main_healthy"])
        self.assertTrue(result["process_online"])
        self.assertFalse(result["workers_healthy"])
        self.assertEqual(result["worker_lanes"]["analysis"]["state"], "stalled")
        self.assertEqual(result["worker_lanes"]["reply_recovery"]["state"], "idle")

    def test_snapshot_publication_never_advances_worker_progress(self):
        self.registry.begin("analysis")
        before = self.registry.snapshot()
        self.now += 100
        self.assertEqual(self.registry.snapshot(), before)
        # Returned observations cannot mutate registry ownership/state.
        before["analysis"]["progress_at"] = self.now
        self.assertEqual(self.registry.snapshot()["analysis"]["progress_at"], 1000)

    def test_idle_polling_wait_is_healthy_but_dead_thread_eventually_stalls(self):
        self.registry.begin("conversation_refresh")
        self.registry.finish("conversation_refresh")
        self.now += 120
        state = worker_health_snapshot(self.registry.snapshot(), now=self.now)
        self.assertTrue(state["lanes"]["conversation_refresh"]["healthy"])
        self.now += 181
        state = worker_health_snapshot(self.registry.snapshot(), now=self.now)
        self.assertEqual(state["lanes"]["conversation_refresh"]["state"], "stalled")

    def test_iteration_failure_retains_only_class_and_propagates(self):
        with patch("management.services.ig_worker_progress.WORKERS", self.registry):
            with self.assertRaisesMessage(RuntimeError, "private customer text"):
                with worker_iteration("analysis"):
                    raise RuntimeError("private customer text")
        snapshot = self.registry.snapshot()
        self.assertEqual(snapshot["analysis"]["state"], "failed")
        self.assertNotIn("private customer text", str(snapshot))
        self.registry.begin("analysis")
        self.registry.finish("analysis")
        self.assertEqual(self.registry.snapshot()["analysis"]["state"], "idle")

    def test_missing_or_invalid_observations_are_not_healthy(self):
        for bad in (None, {}, {"analysis": {"state": "running", "progress_at": float("nan")}},
                    {"analysis": {"state": [], "progress_at": self.now}},
                    {"analysis": {"state": "idle", "progress_at": self.now + 10000}}):
            result = worker_health_snapshot(bad, now=self.now)
            self.assertFalse(result["healthy"])
            self.assertEqual(result["lanes"]["analysis"]["state"], "unobserved")

    def test_observation_deadline_cannot_be_extended_by_payload(self):
        self.registry.begin("analysis")
        payload = self.registry.snapshot()
        payload["analysis"]["deadline_at"] = self.now + 1000000
        self.assertEqual(worker_health_snapshot(payload, now=self.now + 601)["lanes"]["analysis"]["state"], "stalled")

    def test_stalled_lane_freezes_new_claims_without_touching_existing_state(self):
        self.registry.begin("analysis")
        self.now += 601
        with patch("management.services.ig_worker_progress.WORKERS", self.registry):
            self.assertFalse(worker_claim_admission("analysis"))
        self.assertEqual(self.registry.snapshot()["analysis"]["state"], "running")

    def test_shared_daemon_pulse_freezes_uninitialized_one_shot_process(self):
        from django.core.cache import cache

        cache.set(
            "ig_bot_daemon_hb",
            {
                "at": self.now,
                "owner": "instagram_daemon",
                "worker_lanes": {
                    "analysis": {
                        "state": "running",
                        "progress_at": self.now - 601,
                    }
                },
            },
            600,
        )
        one_shot = WorkerProgressRegistry(clock=lambda: self.now)
        with patch("management.services.ig_worker_progress.WORKERS", one_shot), patch(
            "management.services.ig_worker_progress.time.time", return_value=self.now
        ):
            self.assertFalse(worker_claim_admission("analysis"))
        cache.delete("ig_bot_daemon_hb")

    def test_shared_daemon_pulse_allows_fresh_lane(self):
        from django.core.cache import cache

        cache.set(
            "ig_bot_daemon_hb",
            {
                "at": self.now,
                "owner": "instagram_daemon",
                "worker_lanes": {
                    "analysis": {"state": "idle", "progress_at": self.now}
                },
            },
            600,
        )
        one_shot = WorkerProgressRegistry(clock=lambda: self.now)
        with patch("management.services.ig_worker_progress.WORKERS", one_shot), patch(
            "management.services.ig_worker_progress.time.time", return_value=self.now
        ):
            self.assertTrue(worker_claim_admission("analysis"))
        cache.delete("ig_bot_daemon_hb")
