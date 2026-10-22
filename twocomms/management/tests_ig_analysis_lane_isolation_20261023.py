"""Native durable-owner regressions: analysis coordination cannot halt replies."""
from contextlib import ExitStack
from contextvars import Context
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.db import OperationalError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from management.management.commands import run_instagram_bot as runner
from management.models import IgWorkerLaneState
from management.services import ig_analysis_lane as lane
from management.services import ig_worker_progress as progress


class BoundedEvent:
    def __init__(self, attempts=1, on_wait=None):
        self.remaining = attempts
        self.waits = []
        self.set_calls = 0
        self.on_wait = on_wait

    def is_set(self):
        return self.remaining <= 0

    def set(self):
        self.set_calls += 1
        self.remaining = 0

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait:
            self.on_wait()
        self.remaining -= 1
        return self.is_set()


class AnalysisLaneIsolationTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.registry = progress.WorkerProgressRegistry()
        self.registry.reset()
        self.stack.enter_context(patch.object(runner, "WORKERS", self.registry))
        self.stack.enter_context(patch.object(progress, "WORKERS", self.registry))
        for name in ("close_old_connections", "require_database_ready", "task_heartbeat"):
            self.stack.enter_context(patch.object(runner, name))
        self.stack.enter_context(patch.object(runner, "maintenance_status", return_value={"active": False}))
        self.stack.enter_context(patch.object(runner.bot, "log"))
        self.process = self.stack.enter_context(patch("management.services.bot_conversation_analysis.process_due_analysis"))
        self.reconcile = self.stack.enter_context(patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs", return_value={}))
        self.events = self.stack.enter_context(patch("management.services.ig_analysis_events.process_due_analysis_events", return_value={}))
        self.observations = self.stack.enter_context(patch("management.services.ig_payment_observation.drain_payment_observations"))
        self.typed = self.stack.enter_context(patch("management.services.ig_typed_memory.reconcile_typed_memory"))
        self.sources = self.stack.enter_context(patch("management.services.ig_memory_producer.reconcile_memory_sources"))
        self.memory = self.stack.enter_context(patch.object(runner, "_memory_background_tick"))
        self.metadata = self.stack.enter_context(patch.object(runner, "_ad_metadata_background_tick"))
        self.units = (self.process, self.reconcile, self.events, self.observations, self.typed, self.sources, self.memory, self.metadata)

    def assert_no_units(self):
        for unit in self.units:
            unit.assert_not_called()

    def assert_waiting(self, reason="owner_busy"):
        health = progress.worker_health_snapshot(self.registry.snapshot(), now=timezone.now().timestamp())
        self.assertTrue(health["healthy"])
        row = health["lanes"]["analysis"]
        self.assertEqual(row["state"], "waiting_owner")
        self.assertTrue(row["healthy"])
        self.assertFalse(row["ready"])
        self.assertFalse(row["claim_admission"])
        self.assertEqual(row["blocked_reason"], reason)
        self.assertFalse(progress.worker_claim_admission("analysis"))
        self.assertTrue(self.registry.admission_allowed("conversation_refresh"))
        self.assertIsNone(lane.current_owner())

    def test_manual_owner_pauses_only_analysis_with_bounded_waits(self):
        manual = lane.acquire_owner(owner_kind="manual")
        event = BoundedEvent(attempts=2)
        runner._analysis_worker(event)
        self.assertEqual(event.waits, [5, 5])
        self.assertEqual(event.set_calls, 0)
        self.assert_no_units()
        self.assert_waiting()
        self.assertEqual(lane.owner_snapshot()["owner_token"], manual["owner_token"])

    def test_frozen_owner_is_never_unfrozen_or_stolen(self):
        manual = lane.acquire_owner(owner_kind="manual")
        lane.freeze_claims(owner_token=manual["owner_token"], generation=manual["generation"], reason="manual-recovery")
        runner._analysis_worker(BoundedEvent())
        self.assert_no_units()
        self.assert_waiting()
        row = lane.owner_snapshot()
        self.assertTrue(row["claim_frozen"])
        self.assertEqual(row["generation"], manual["generation"])
        self.assertEqual(row["owner_token"], manual["owner_token"])

    def test_standalone_acquires_binds_and_releases_exact_owner_on_shutdown(self):
        captured = []
        def process(**_kwargs):
            owner = lane.current_owner()
            self.assertTrue(lane.claim_admission(owner_token=owner["owner_token"], generation=owner["generation"]))
            captured.append(owner)
        self.process.side_effect = process
        event = BoundedEvent()
        runner._analysis_worker(event)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["owner_kind"], "daemon")
        self.assertEqual(event.set_calls, 0)
        self.assertIsNone(lane.current_owner())
        self.assertEqual(lane.owner_snapshot()["owner_token"], "")

    def test_two_interleaved_workers_cannot_share_mutation_authority(self):
        captured = []
        def process(**_kwargs):
            captured.append(lane.current_owner())
            with patch("management.services.bot_conversation_analysis.process_due_analysis") as second:
                Context().run(runner._analysis_worker, BoundedEvent())
                second.assert_not_called()
            self.assertEqual(lane.current_owner(), captured[0])
            self.assertEqual(lane.owner_snapshot()["owner_token"], captured[0]["owner_token"])
        self.process.side_effect = process
        runner._analysis_worker(BoundedEvent())
        self.assertEqual(len(captured), 1)
        self.assertEqual(lane.owner_snapshot()["owner_token"], "")

    def test_retry_acquires_after_manual_release(self):
        manual = lane.acquire_owner(owner_kind="manual")
        def release():
            lane.release_owner(owner_token=manual["owner_token"], generation=manual["generation"])
        event = BoundedEvent(attempts=2, on_wait=release)
        runner._analysis_worker(event)
        self.process.assert_called_once_with(limit=1)
        self.assertEqual(event.set_calls, 0)
        self.assertGreater(lane.owner_snapshot()["generation"], manual["generation"])
        self.assertIsNone(lane.current_owner())

    def test_all_six_renewal_rejections_are_local_including_between_slow_consumers(self):
        real_renew = lane.renew_owner
        for rejected_at in range(1, 7):
            with self.subTest(rejected_at=rejected_at):
                IgWorkerLaneState.objects.all().delete()
                for unit in self.units:
                    unit.reset_mock()
                calls = []
                def renew(**kwargs):
                    calls.append(kwargs)
                    return False if len(calls) == rejected_at else real_renew(**kwargs)
                event = BoundedEvent()
                with patch.object(lane, "renew_owner", side_effect=renew):
                    runner._analysis_worker(event)
                self.assertEqual(event.set_calls, 0)
                self.assertEqual(event.waits, [5])
                self.assertEqual(len(calls), rejected_at)
                self.assert_waiting("owner_lost")
                if rejected_at == 1:
                    self.assert_no_units()
                if rejected_at <= 2:
                    self.observations.assert_not_called()
                if rejected_at <= 3:
                    self.process.assert_not_called()
                if rejected_at <= 4:
                    self.events.assert_not_called()
                if rejected_at <= 5:
                    self.memory.assert_not_called()

    def test_shutdown_during_analysis_starts_no_events_and_releases_owner(self):
        event = BoundedEvent()
        self.process.side_effect = lambda **kwargs: event.set()
        with patch.object(lane, "release_owner", wraps=lane.release_owner) as release:
            runner._analysis_worker(event)
            release.assert_called_once()
        self.process.assert_called_once_with(limit=1)
        self.events.assert_not_called()
        self.memory.assert_not_called()
        self.assertEqual(lane.owner_snapshot()["owner_token"], "")
        self.assertIsNone(lane.current_owner())

    def test_expired_owner_cannot_run_or_release_successor(self):
        old = lane.acquire_owner(owner_kind="daemon", now=timezone.now()-timedelta(seconds=91))
        successor = lane.acquire_owner(owner_kind="manual")
        runner._analysis_worker(BoundedEvent(), old["owner_token"], old["generation"])
        self.assert_no_units()
        self.assert_waiting("owner_lost")
        self.assertEqual(lane.owner_snapshot()["owner_token"], successor["owner_token"])
        self.assertEqual(lane.owner_snapshot()["generation"], successor["generation"])

    def test_acquire_db_error_is_failed_not_healthy_wait(self):
        with (patch.object(lane, "acquire_owner", side_effect=OperationalError("offline")),
              patch.object(runner, "_database_disconnected", return_value=True)):
            event = BoundedEvent()
            runner._analysis_worker(event)
        self.assert_no_units()
        self.assertEqual(event.set_calls, 0)
        health = progress.worker_health_snapshot(self.registry.snapshot(), now=timezone.now().timestamp())
        self.assertEqual(health["lanes"]["analysis"]["state"], "failed")
        self.assertFalse(health["healthy"])

    def test_waiting_requires_fresh_attempts_and_shared_claims_fail_closed(self):
        self.registry.waiting("analysis")
        payload = self.registry.snapshot()
        at = payload["analysis"]["progress_at"]
        with patch.object(progress.cache, "get", return_value={"owner": progress.DAEMON_OWNER, "at": at, "worker_lanes": payload}):
            self.assertFalse(progress._shared_worker_admission("analysis"))
        health = progress.worker_health_snapshot(payload, now=at+progress.WORKER_LIMITS["analysis"])
        self.assertEqual(health["lanes"]["analysis"]["state"], "stalled")
        self.assertFalse(health["healthy"])

    def test_manual_analysis_lease_cannot_prevent_customer_daemon_startup(self):
        manual = lane.acquire_owner(owner_kind="manual")
        threads = []
        def make_thread(**kwargs):
            thread = Mock()
            threads.append((kwargs, thread))
            return thread
        with TemporaryDirectory() as temp, ExitStack() as stack:
            stack.enter_context(patch.object(runner, "PID_FILE", f"{temp}/pid"))
            stack.enter_context(patch.object(runner.threading, "Thread", side_effect=make_thread))
            stack.enter_context(patch.object(runner, "maintenance_status", side_effect=[{"active": False}, {"active": True}]))
            stack.enter_context(patch.object(runner, "_reconcile_commercial_episodes_after_reload"))
            stack.enter_context(patch.object(runner, "_drain_background_workers"))
            stack.enter_context(patch.object(runner, "_drain_customer_execution"))
            pool = stack.enter_context(patch("management.services.ig_customer_execution_pool.CustomerExecutionPool"))
            acquire = stack.enter_context(patch.object(lane, "acquire_owner", side_effect=AssertionError("main must not acquire analysis")))
            runner.Command()._forever_locked()
            acquire.assert_not_called()
            pool.assert_called_once()
        self.assertIn("ig-analysis", [data["name"] for data, _ in threads])
        self.assertIn("ig-conversation-refresh", [data["name"] for data, _ in threads])
        for data, thread in threads:
            thread.start.assert_called_once()
            if data["name"] == "ig-analysis":
                self.assertEqual(len(data["args"]), 1)
        self.assertEqual(lane.owner_snapshot()["owner_token"], manual["owner_token"])


class CustomerDeliveryDuringBusyAnalysisTests(TransactionTestCase):
    """Compose native sealing/outbox/delivery with a live foreign lane lease."""
    def test_busy_analysis_wait_does_not_block_one_native_sent_receipt(self):
        from management.tests_ig_manu_fixture_helpers import setup_revision_delivery_fixture
        from management.services.ig_revision_delivery import drain_group, ProviderPartResult
        from management.models import IgRevisionDeliveryEffect
        setup_revision_delivery_fixture(self)
        manual = lane.acquire_owner(owner_kind="manual")
        registry = progress.WorkerProgressRegistry()
        registry.reset()
        calls = []
        def deliver():
            self.assertFalse(event.is_set())
            self.assertEqual(event.set_calls, 0)
            self.assertEqual(registry.snapshot()["analysis"]["state"], "waiting_owner")
            self.assertTrue(registry.admission_allowed("conversation_refresh"))
            self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("Your answer") }])
            def fake_http(payload):
                calls.append(payload)
                return ProviderPartResult("instagram_login:owner-1", 200, "customer-receipt-while-analysis-busy")
            result = drain_group(self.revision.pk, self.revision_token, "substantive_text", fake_http)
            self.assertEqual(result.state, "sent")
        event = BoundedEvent(on_wait=deliver)
        with (patch.object(runner, "WORKERS", registry), patch.object(progress, "WORKERS", registry),
              patch.object(runner, "close_old_connections"), patch.object(runner, "require_database_ready"),
              patch("management.services.bot_conversation_analysis.process_due_analysis") as analysis,
              patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs") as reconcile):
            runner._analysis_worker(event)
            analysis.assert_not_called()
            reconcile.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(IgRevisionDeliveryEffect.objects.filter(state=IgRevisionDeliveryEffect.State.SENT).count(), 1)
        self.assertEqual(event.set_calls, 0)
        self.assertEqual(lane.owner_snapshot()["owner_token"], manual["owner_token"])
        self.assertIsNone(lane.current_owner())

    def _plan(self, specs):
        from management.tests_ig_revision_delivery import RevisionDeliveryTests
        return RevisionDeliveryTests._plan(self, specs)

    def _payload(self, text):
        from management.tests_ig_revision_delivery import RevisionDeliveryTests
        return RevisionDeliveryTests._payload(self, text)
