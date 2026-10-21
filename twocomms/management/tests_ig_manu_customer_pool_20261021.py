"""Bounded daemon customer slots; transport is never cancelled by a wait."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import time
from unittest.mock import Mock, patch

from django.db import DatabaseError
from django.test import SimpleTestCase

from management.management.commands import run_instagram_bot as runner
from management.services.ig_customer_execution_pool import (
    CustomerExecutionFailed, CustomerExecutionPool, customer_job_budget_seconds,
    run_one_customer_job, runnable_customer_work,
)
from management.services.ig_db_circuit import DbCircuitOpen
from management.services.ig_worker_progress import WORKERS, WORKER_LIMITS, worker_health_snapshot


class CustomerPoolTests(SimpleTestCase):
    def setUp(self):
        recorder = patch("management.services.ig_db_circuit.record_db_failure", return_value=False)
        recorder.start()
        self.addCleanup(recorder.stop)

    def pool(self, **kwargs):
        stop = kwargs.pop("stop", threading.Event())
        kwargs.setdefault("capacity", lambda: 2)
        kwargs.setdefault("maintenance", lambda: {"active": False})
        kwargs.setdefault("has_work", lambda: True)
        pool = CustomerExecutionPool(stop, **kwargs)
        self.addCleanup(lambda: pool.drain(timeout=2))
        return pool

    def wait_done(self, pool):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if all(future.done() for future in pool.futures.values()):
                return
            threading.Event().wait(0.005)
        self.fail("bounded fake worker did not finish")

    def test_blocked_customer_does_not_block_other_slot_and_no_queue_grows(self):
        slow_started, healthy_done, release = threading.Event(), threading.Event(), threading.Event()
        order, lock = [], threading.Lock()

        def work():
            with lock:
                index = len(order)
                order.append(index)
            if index == 0:
                slow_started.set()
                release.wait(2)
            else:
                healthy_done.set()
            return 1

        pool = self.pool(run_one=work)
        try:
            pool.tick()
            self.assertTrue(slow_started.wait(1))
            self.assertTrue(healthy_done.wait(1), "healthy slot must run while slow provider is blocked")
            for _ in range(5):
                # Existing completed jobs may be reaped, but each admission
                # remains bounded by two outstanding futures, never a backlog.
                self.assertLessEqual(pool.tick()["in_flight"], 2)
            self.assertFalse(pool.drain(timeout=0.01))
            self.assertFalse(pool.closed)
        finally:
            release.set()
        self.assertTrue(pool.drain(timeout=1))

    def test_idle_and_paused_admission_start_no_jobs_or_capacity_probe(self):
        work, capacity = Mock(), Mock(return_value=2)
        pool = self.pool(run_one=work, has_work=lambda: False, capacity=capacity)
        self.assertEqual(pool.tick()["in_flight"], 0)
        pool.tick(admit=False)
        work.assert_not_called()
        capacity.assert_not_called()
        for slot in range(2):
            self.assertEqual(WORKERS.snapshot()[f"customer_execution_{slot}"]["state"], "idle")

    def test_receipt_only_due_work_admits_native_finalization_without_model_probe(self):
        query = Mock()
        query.filter.return_value = query
        query.exclude.return_value = query
        query.exists.return_value = False
        with (patch("management.models.IgCustomerTurnRevision.objects", query),
            patch("management.services.ig_revision_rollout.revision_execution_rollout",
                return_value=SimpleNamespace(enabled=True, cutover_at=None)),
            patch("management.services.ig_revision_execution.finalization_due_ids", return_value=[42]) as finalizations,
            patch("management.services.ig_revision_execution.due_revision_ids", side_effect=AssertionError("receipt admission short-circuits")),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("no provider health I/O")) as provider):
            self.assertTrue(runnable_customer_work())
        finalizations.assert_called_once()
        provider.assert_not_called()

    def test_existing_db_capacity_can_reduce_admission_to_one_without_backlog(self):
        started, release = threading.Event(), threading.Event()
        pool = self.pool(run_one=lambda: (started.set(), release.wait(2)), capacity=lambda: 1)
        try:
            self.assertEqual(pool.tick()["in_flight"], 1)
            self.assertTrue(started.wait(1))
            self.assertEqual(pool.tick()["in_flight"], 1)
        finally:
            release.set()

    def test_maintenance_closes_new_admission_and_wait_timeout_is_not_cancellation(self):
        started, release = threading.Event(), threading.Event()
        active = {"active": False}
        pool = self.pool(run_one=lambda: (started.set(), release.wait(2)), maintenance=lambda: active)
        try:
            pool.tick()
            self.assertTrue(started.wait(1))
            active["active"] = True
            self.assertLessEqual(pool.tick()["in_flight"], 2)
            self.assertFalse(pool.drain(timeout=0.01))
            self.assertTrue(pool.snapshot()["closing"])
            self.assertGreater(pool.snapshot()["in_flight"], 0)
        finally:
            release.set()
        self.assertTrue(pool.drain(timeout=1))

    def test_future_exception_is_reaped_and_stops_admission(self):
        pool = self.pool(run_one=Mock(side_effect=ValueError("not printed")))
        pool.tick()
        self.wait_done(pool)
        with self.assertRaises(CustomerExecutionFailed):
            pool.tick()
        self.assertTrue(pool.stop_event.is_set())
        self.assertTrue(pool.snapshot()["closing"])

    def test_database_failure_is_surfaced_as_circuit_deferral(self):
        pool = self.pool(run_one=Mock(side_effect=DatabaseError("db lost")))
        with patch("management.services.ig_db_circuit.record_db_failure", return_value=True):
            pool.tick()
            self.wait_done(pool)
            with self.assertRaises(DbCircuitOpen):
                pool.tick()
        self.assertFalse(pool.stop_event.is_set())

    def test_circuit_reporting_failure_cannot_drop_other_running_future(self):
        first_started, release = threading.Event(), threading.Event()
        calls, lock = [], threading.Lock()

        def work():
            with lock:
                number = len(calls)
                calls.append(number)
            if number == 0:
                first_started.set()
                release.wait(2)
                return 1
            raise ValueError("fake failure")

        pool = self.pool(run_one=work)
        try:
            pool.tick()
            self.assertTrue(first_started.wait(1))
            deadline = time.monotonic() + 1
            while not any(future.done() for future in pool.futures.values()) and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            with patch("management.services.ig_db_circuit.record_db_failure", side_effect=RuntimeError("cache unavailable")):
                self.assertFalse(pool.drain(timeout=0.01))
            self.assertGreater(pool.snapshot()["in_flight"], 0)
        finally:
            release.set()
        self.assertTrue(pool.drain(timeout=1))

    def test_worker_owns_fresh_settings_and_closes_thread_connections_even_on_error(self):
        settings = SimpleNamespace(is_enabled=True)
        with (patch("management.services.ig_customer_execution_pool.close_old_connections") as opened,
            patch("management.services.ig_customer_execution_pool.connections.close_all") as closed,
            patch("management.services.ig_db_circuit.require_database_ready"),
            patch("management.models.InstagramBotSettings.load", return_value=settings) as loaded,
            patch("management.services.ig_revision_live.revision_execution_enabled", return_value=True),
            patch("management.services.instagram_bot.maintenance_status", return_value={"active": False}),
            patch("management.services.instagram_bot.process_pending", side_effect=ValueError("fake body error")) as process):
            with self.assertRaises(ValueError):
                run_one_customer_job()
        opened.assert_called_once()
        closed.assert_called_once()
        loaded.assert_called_once()
        process.assert_called_once_with(settings, max_items=1)

    def test_optional_health_slots_absent_without_pool_and_cover_native_transport_budget(self):
        self.assertGreaterEqual(WORKER_LIMITS["customer_execution_0"], customer_job_budget_seconds())
        snapshot = {name: {"state": "idle", "progress_at": 100, "deadline_at": 1000}
            for name in WORKER_LIMITS if not name.startswith("customer_execution_")}
        health = worker_health_snapshot(snapshot, now=101)
        self.assertNotIn("customer_execution_0", health["lanes"])
        self.assertEqual(len(health["lanes"]), 8)

    def test_admission_precedes_poll_then_refills_without_serial_pending(self):
        events = []
        settings = SimpleNamespace(is_enabled=True, receive_via_poll=True, poll_interval_seconds=2, pk=None)
        pool = Mock()
        pool.tick.side_effect = lambda **_kwargs: (events.append("submit") or {"in_flight": 2})
        with (patch.object(runner, "_CUSTOMER_EXECUTION_POOL", pool),
            patch.object(runner, "maintenance_status", return_value={"active": False}),
            patch.object(runner.bot, "poll_ingest", side_effect=lambda _settings, **kwargs:
                (self.assertEqual(kwargs, {"max_seconds": 2}), events.append("poll"))[1]),
            patch.object(runner.bot, "process_pending") as serial,
            patch.object(runner, "_drain_customer_followups", side_effect=lambda _settings: events.append("followups")),
            patch("management.services.ig_revision_live.revision_execution_enabled", return_value=True)):
            enabled, _last_poll, interrupted = runner._customer_lane(settings, 0.0)
        self.assertTrue(enabled)
        self.assertFalse(interrupted)
        serial.assert_not_called()
        self.assertEqual(events, ["submit", "poll", "submit", "followups"])

    def test_poll_time_budget_default_clamp_and_invalid_fail_before_io(self):
        from management.services import instagram_bot as bot

        settings = SimpleNamespace(receive_via_poll=True, ig_user_id="owner", page_id="",
            last_error="", reply_after=None, last_started_at=None)
        cursor = SimpleNamespace(excluded_at=None, participant_igsid="customer", next_attempt_at=None,
            last_message_id="", last_message_at=None)

        class CapturedDeadline(Exception):
            pass

        for supplied, expected in ((None, 20), (2, 2), (200, 20)):
            with (
                self.subTest(budget=supplied),
                patch.object(bot, "_provider_account_id", return_value="owner"),
                patch.object(bot, "get_page_token", return_value="fake"),
                patch.object(bot, "get_conv_ids_cached", return_value=["conv"]),
                patch.object(bot, "provider_transport", return_value="facebook_login"),
                patch.object(bot, "_poll_cursor_map", return_value={"conv": cursor}),
                patch.object(bot, "_poll_conversation_order", return_value=(["conv"], 0)),
                patch.object(bot.time, "monotonic", return_value=100),
                patch.object(bot, "_fetch_polled_conversation", side_effect=CapturedDeadline) as fetched,
            ):
                with self.assertRaises(CapturedDeadline):
                    bot.poll_ingest(settings, **({} if supplied is None else {"max_seconds": supplied}))
                self.assertEqual(fetched.call_args.kwargs["deadline"], 100 + expected)
        for invalid in (True, False, 0, -1, float("inf"), float("nan"), "bad", object()):
            with self.subTest(invalid=repr(invalid)), patch.object(bot, "get_page_token") as token:
                with self.assertRaises(ValueError):
                    bot.poll_ingest(settings, max_seconds=invalid)
                token.assert_not_called()

    def test_rollout_off_retains_original_poll_default(self):
        settings = SimpleNamespace(is_enabled=True, receive_via_poll=True, poll_interval_seconds=2, pk=None)
        pool = Mock()
        pool.tick.return_value = {"in_flight": 0}
        with (patch.object(runner, "_CUSTOMER_EXECUTION_POOL", pool),
            patch.object(runner, "maintenance_status", return_value={"active": False}),
            patch.object(runner.bot, "poll_ingest") as poll,
            patch.object(runner.bot, "process_pending"),
            patch.object(runner, "_drain_customer_followups"),
            patch("management.services.ig_revision_live.revision_execution_enabled", return_value=False)):
            runner._customer_lane(settings, 0.0)
        poll.assert_called_once_with(settings)

    def test_real_poll_fetch_caps_request_timeout_to_remaining_budget(self):
        from management.services import instagram_bot as bot

        with (patch.object(bot, "provider_transport", return_value="instagram_login"),
            patch.object(bot, "_provider_url", return_value="https://fake.invalid/conv"),
            patch.object(bot.time, "monotonic", return_value=100),
            patch.object(bot, "_provider_http", return_value=(503, "")) as http):
            result = bot._fetch_polled_conversation(SimpleNamespace(), "conv", "fake",
                cursor_at=None, cursor_id="", deadline=102, request_limit=40)
        self.assertFalse(result["complete"])
        self.assertEqual(http.call_args.kwargs["timeout"], 2)

    def test_background_drain_keeps_process_lock_despite_reporting_errors(self):
        started, release, report_failed, drained = (threading.Event() for _ in range(4))
        worker = threading.Thread(target=lambda: (started.set(), release.wait(3)), daemon=True)
        worker.start()
        self.assertTrue(started.wait(1))
        errors = []

        def fail_report(*args, **kwargs):
            report_failed.set()
            raise RuntimeError("cache unavailable")

        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "background-daemon.lock")

            def owner():
                try:
                    with runner._try_process_lock(path) as held:
                        self.assertIsNotNone(held)
                        runner._drain_background_workers((worker,), owner="test",
                            start_sentinel=0, cycle=1)
                        drained.set()
                except BaseException as exc:
                    errors.append(exc)

            with (patch.object(runner, "_publish_process_pulse", side_effect=fail_report),
                patch.object(runner, "_publish_main_progress", side_effect=fail_report),
                patch.object(runner.bot, "log", side_effect=fail_report),
                patch("management.services.ig_worker_progress.WORKER_LIMITS", {"fake": 0})):
                owner_thread = threading.Thread(target=owner)
                owner_thread.start()
                try:
                    self.assertTrue(report_failed.wait(2))
                    with runner._try_process_lock(path) as competing:
                        self.assertIsNone(competing)
                    self.assertFalse(drained.is_set())
                finally:
                    release.set()
                    owner_thread.join(2)
            self.assertFalse(owner_thread.is_alive())
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors, errors)
            self.assertTrue(drained.is_set())

    def test_drain_keeps_real_process_lock_despite_cache_and_logging_failures(self):
        started, release, reporting_failed, drained = (threading.Event() for _ in range(4))
        pool = self.pool(run_one=lambda: (started.set(), release.wait(3)))
        pool.tick()
        self.assertTrue(started.wait(1))
        errors = []

        def failed_report(*args, **kwargs):
            reporting_failed.set()
            raise RuntimeError("cache unavailable")

        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "customer-daemon.lock")

            def owner():
                try:
                    with runner._try_process_lock(path) as held:
                        self.assertIsNotNone(held)
                        runner._drain_customer_execution(pool, owner="test", start_sentinel=0, cycle=1)
                        drained.set()
                except BaseException as exc:
                    errors.append(exc)

            with (patch.object(runner, "_publish_process_pulse", side_effect=failed_report),
                patch.object(runner, "_publish_main_progress", side_effect=failed_report),
                patch.object(runner.bot, "log", side_effect=failed_report),
                patch("management.services.ig_customer_execution_pool.customer_job_budget_seconds", return_value=0)):
                thread = threading.Thread(target=owner)
                thread.start()
                try:
                    self.assertTrue(reporting_failed.wait(2))
                    with runner._try_process_lock(path) as competing:
                        self.assertIsNone(competing, "cache/log failure must not release live owner")
                    self.assertFalse(drained.is_set())
                finally:
                    release.set()
                    thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
            self.assertTrue(drained.is_set())
            with runner._try_process_lock(path) as after:
                self.assertIsNotNone(after)
