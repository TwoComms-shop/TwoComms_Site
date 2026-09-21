"""ЭА.14 / ЭА.15 — progress-pulse демона и изоляция клиентской полосы.

Что здесь проверяется и почему именно так.

ЭА.14. Долгая работа внутри цикла не имеет права выглядеть смертью процесса.
Прямой тест «поспать 60 секунд» бесполезен: он проверял бы часы, а не контракт.
Поэтому окно живости в тесте сжимается до долей секунды, а «долгая операция»
пропорционально короче. Свойство остаётся тем же: работа длиннее окна живости.
В каждом таком тесте есть КОНТРОЛЬ — тот же сценарий без пульса, — иначе тест
проходил бы и на сломанном коде.

ЭА.15. Обслуживающие задачи не имеют права стоять перед обработкой входящих.
Контроль здесь — прежний порядок цикла за выключенным флагом: он обязан
показывать ровно тот дефект, который исправляется.
"""
import threading
import time
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.management.commands import run_instagram_bot as runner
from management.management.commands.run_instagram_bot import (
    CUSTOMER_LANE,
    HB_KEY,
    MAIN_PROGRESS_KEY,
    SERVICE_LANE_BUDGET_SECONDS,
    SERVICE_LANE_DEFER_CYCLES,
    SERVICE_LANE_NOTIFICATIONS,
    SERVICE_LANE_PROFILES,
    Command,
    _INFLIGHT,
    _analysis_worker,
    _publish_process_pulse,
    _run_work_cycle,
    daemon_supervision_verdict,
    observe_daemon_supervision,
    operation_pulse,
    reset_inflight_operations,
    reset_service_lanes,
    service_lane_timings,
)
from management.models import IgWorkerLaneState
from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot as bot
from management.services.ig_task_health import (
    DAEMON_ACTION_ESCALATE,
    DAEMON_ACTION_NONE,
    DAEMON_ACTION_SPAWN,
    DAEMON_STATE_CHILD_EXITED,
    DAEMON_STATE_LOCK_STALE,
    DAEMON_STATE_NO_PROGRESS,
    DAEMON_STATE_PROGRESSING,
    DAEMON_SUPERVISION_STATES,
    classify_daemon_supervision,
    daemon_no_progress_after_seconds,
    escalate_daemon_supervision,
    operational_lease_seconds,
    operational_reclaim_age_seconds,
    operational_reclaim_lease_enabled,
    processing_lease_expired,
)


class AnalysisWorkerOwnershipTests(SimpleTestCase):
    def test_worker_stops_terminally_after_lane_renewal_is_rejected(self):
        class StopEvent:
            def __init__(self):
                self.stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, _seconds):
                self.stopped = True

        stop_event = StopEvent()
        with (
            patch("management.management.commands.run_instagram_bot.close_old_connections"),
            patch("management.management.commands.run_instagram_bot.require_database_ready"),
            patch("management.services.ig_analysis_lane.renew_owner", return_value=False),
            patch("management.management.commands.run_instagram_bot.maintenance_status", return_value={"active": False}),
            patch("management.services.bot_conversation_analysis.process_due_analysis") as process,
            patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs") as reconcile,
            patch("management.services.ig_analysis_events.process_due_analysis_events") as events,
        ):
            _analysis_worker(stop_event, "lost-owner", 7)

        self.assertTrue(stop_event.stopped)
        process.assert_not_called()
        reconcile.assert_not_called()
        events.assert_not_called()


def _settings(**kwargs):
    """Unsaved settings row: цикл не обязан иметь БД, кроме polling-телеметрии."""
    defaults = {"is_enabled": True, "receive_via_poll": False}
    defaults.update(kwargs)
    return InstagramBotSettings(**defaults)


class _CycleHarness:
    """Собрать порядок и тайминги шагов одного цикла без реального провайдера."""

    def __init__(self):
        self.events = []
        self.lanes = {}

    def record(self, name):
        self.events.append((name, time.monotonic()))
        self.lanes[name] = _INFLIGHT.snapshot()["inflight_operation"]

    def order(self):
        return [name for name, _ in self.events]

    def at(self, name):
        for recorded, moment in self.events:
            if recorded == name:
                return moment
        raise AssertionError(f"step {name!r} never ran")


class InCycleProgressPulseTests(SimpleTestCase):
    """ЭА.14 — операция длиннее окна живости не должна выглядеть смертью."""

    databases = {"default"}

    def setUp(self):
        reset_inflight_operations()
        reset_service_lanes()
        cache.delete(HB_KEY)
        cache.delete(MAIN_PROGRESS_KEY)
        self.addCleanup(cache.delete, HB_KEY)
        self.addCleanup(cache.delete, MAIN_PROGRESS_KEY)
        self.addCleanup(reset_inflight_operations)
        self.addCleanup(reset_service_lanes)

    # Окно живости в тесте — доли секунды. «Долгая операция» пропорционально
    # короче реальных 60 с, но соотношение сохранено: работа длиннее окна.
    TEST_ALIVE_WINDOW = 0.4
    TEST_LONG_OPERATION = 0.9

    def _run_long_cycle(self, *, with_pulse: bool):
        settings_obj = _settings()
        stop_event = threading.Event()
        pulse_thread = None
        if with_pulse:
            pulse_thread = threading.Thread(
                name="test-process-pulse",
                target=runner._progress_pulse,
                args=(stop_event, "test-owner", 1.0),
                daemon=True,
            )
        _publish_process_pulse(
            owner="test-owner", start_sentinel=1.0, state="starting"
        )
        published_at = time.time()
        with patch.object(runner, "HB_PULSE_INTERVAL", 0.02):
            if pulse_thread is not None:
                pulse_thread.start()
            try:
                with (
                    patch.object(
                        runner.bot,
                        "process_pending",
                        side_effect=lambda *_: time.sleep(self.TEST_LONG_OPERATION),
                    ),
                    patch.object(runner.bot_followups, "process_due_followups"),
                    patch.object(runner.bot, "drain_manager_notifications"),
                    patch.object(runner.cache, "add", return_value=False),
                ):
                    _run_work_cycle(settings_obj, 0.0)
                # Дать потоку пульса один тик после цикла: в production он
                # продолжает работать и публикует итоговое состояние. Без этой
                # паузы тест ловил бы момент между шагом операции и его
                # публикацией, то есть проверял бы планировщик, а не контракт.
                time.sleep(0.08)
            finally:
                stop_event.set()
                if pulse_thread is not None:
                    pulse_thread.join(timeout=2)
        return cache.get(HB_KEY), published_at

    def test_long_in_cycle_operation_keeps_process_pulse_fresh(self):
        """Пульс, обновляемый ВНУТРИ операции, остаётся свежим."""
        pulse, _published_at = self._run_long_cycle(with_pulse=True)
        age = time.time() - float(pulse["at"])
        self.assertLess(
            age,
            self.TEST_ALIVE_WINDOW,
            "пульс устарел при живом процессе — ровно дефект ЭА.14",
        )

    def test_control_without_in_cycle_pulse_the_same_work_looks_dead(self):
        """КОНТРОЛЬ: без пульса внутри операции та же работа выглядит смертью.

        Без этого контроля предыдущий тест проходил бы и на прежнем коде, где
        heartbeat обновлялся только после возврата из `_run_work_cycle()`.
        """
        pulse, _published_at = self._run_long_cycle(with_pulse=False)
        age = time.time() - float(pulse["at"])
        self.assertGreater(age, self.TEST_ALIVE_WINDOW)

    def test_long_operation_is_classified_as_progressing_not_as_death(self):
        """Итоговый контракт: надзор видит «жив и двигается», а не «мёртв»."""
        pulse, _published_at = self._run_long_cycle(with_pulse=True)
        verdict = classify_daemon_supervision(
            pulse=pulse,
            progress=cache.get(MAIN_PROGRESS_KEY),
            lock_held=True,
            alive_window_seconds=self.TEST_ALIVE_WINDOW,
            no_progress_after_seconds=self.TEST_ALIVE_WINDOW * 2,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_PROGRESSING)
        self.assertEqual(verdict.action, DAEMON_ACTION_NONE)

    def test_pulse_thread_alone_never_fabricates_progress(self):
        """Фоновый пульс не вправе подтверждать прогресс основной работы.

        Иначе свежий пульс скрывал бы зависший цикл — то есть исправление ЭА.14
        создало бы дефект хуже исходного.
        """
        _publish_process_pulse(owner="o", start_sentinel=1.0, state="running")
        first = cache.get(HB_KEY)
        time.sleep(0.05)
        _publish_process_pulse(owner="o", start_sentinel=1.0, state="running")
        second = cache.get(HB_KEY)

        self.assertGreater(float(second["at"]), float(first["at"]))
        self.assertEqual(second["progress_at"], first["progress_at"])

    def test_completed_cycle_and_operation_steps_do_advance_progress(self):
        _publish_process_pulse(owner="o", start_sentinel=1.0, state="running")
        before = cache.get(HB_KEY)["progress_at"]
        with operation_pulse("unit_test_lane") as pulse:
            pulse.beat()
        _INFLIGHT.note_completed_cycle()
        _publish_process_pulse(owner="o", start_sentinel=1.0, state="running")
        after = cache.get(HB_KEY)

        self.assertGreater(float(after["progress_at"]), float(before))
        self.assertGreater(float(after["last_completed_cycle_at"]), 0.0)

    def test_inflight_operation_name_is_published_while_work_runs(self):
        with operation_pulse(CUSTOMER_LANE):
            _publish_process_pulse(owner="o", start_sentinel=1.0, state="running")
            during = cache.get(HB_KEY)
        _publish_process_pulse(owner="o", start_sentinel=1.0, state="running")
        after = cache.get(HB_KEY)

        self.assertEqual(during["inflight_operation"], CUSTOMER_LANE)
        self.assertEqual(after["inflight_operation"], "")

    def test_nested_entry_into_same_lane_does_not_lose_reporting(self):
        with operation_pulse(CUSTOMER_LANE):
            with operation_pulse(CUSTOMER_LANE):
                pass
            self.assertEqual(
                _INFLIGHT.snapshot()["inflight_operation"], CUSTOMER_LANE
            )
        self.assertEqual(_INFLIGHT.snapshot()["inflight_operation"], "")

    def test_operation_pulse_never_swallows_the_caller_exception(self):
        with self.assertRaisesMessage(RuntimeError, "provider down"):
            with operation_pulse("unit_test_lane"):
                raise RuntimeError("provider down")
        self.assertEqual(_INFLIGHT.snapshot()["inflight_operation"], "")


class WatchdogFourStateTests(SimpleTestCase):
    """ЭА.14 — четыре состояния и РАЗНЫЕ действия по каждому."""

    def test_all_four_states_are_reachable_and_named(self):
        self.assertEqual(
            set(DAEMON_SUPERVISION_STATES),
            {
                DAEMON_STATE_PROGRESSING,
                DAEMON_STATE_NO_PROGRESS,
                DAEMON_STATE_LOCK_STALE,
                DAEMON_STATE_CHILD_EXITED,
            },
        )

    def test_progress_present_means_do_nothing(self):
        verdict = classify_daemon_supervision(
            pulse={"at": 1000.0},
            progress={"at": 990.0},
            lock_held=True,
            now=1000.0,
            alive_window_seconds=60,
            no_progress_after_seconds=120,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_PROGRESSING)
        self.assertEqual(verdict.action, DAEMON_ACTION_NONE)
        self.assertFalse(verdict.requires_spawn)

    def test_alive_but_stuck_escalates_and_never_spawns(self):
        """Перезапуск зависшего живого процесса запрещён: он делает доставку неизвестной."""
        verdict = classify_daemon_supervision(
            pulse={"at": 1000.0},
            progress={"at": 700.0},
            lock_held=True,
            now=1000.0,
            alive_window_seconds=60,
            no_progress_after_seconds=120,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_NO_PROGRESS)
        self.assertEqual(verdict.action, DAEMON_ACTION_ESCALATE)
        self.assertFalse(verdict.requires_spawn)

    def test_fresh_db_deferred_state_suppresses_generic_stall_escalation(self):
        verdict = classify_daemon_supervision(
            pulse={"at": 1000.0},
            progress={
                "at": 1000.0,
                "progress_at": 700.0,
                "last_completed_cycle_at": 700.0,
                "state": "db_deferred",
            },
            lock_held=True,
            now=1000.0,
            alive_window_seconds=60,
            no_progress_after_seconds=120,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_PROGRESSING)
        self.assertEqual(verdict.action, DAEMON_ACTION_NONE)
        self.assertEqual(verdict.reason, "db_deferred")

    def test_stale_db_deferred_publication_still_escalates(self):
        verdict = classify_daemon_supervision(
            pulse={"at": 1000.0},
            progress={
                "at": 700.0,
                "progress_at": 700.0,
                "state": "db_deferred",
            },
            lock_held=True,
            now=1000.0,
            alive_window_seconds=60,
            no_progress_after_seconds=120,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_NO_PROGRESS)
        self.assertEqual(verdict.action, DAEMON_ACTION_ESCALATE)

    def test_no_provable_owner_spawns_even_while_lock_file_is_held(self):
        """Удерживаемый lock доказывает существование процесса, но не его работу."""
        verdict = classify_daemon_supervision(
            pulse={"at": 500.0},
            progress={"at": 999.0},
            lock_held=True,
            now=1000.0,
            alive_window_seconds=60,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_LOCK_STALE)
        self.assertEqual(verdict.action, DAEMON_ACTION_SPAWN)

    def test_absent_pulse_spawns(self):
        verdict = classify_daemon_supervision(
            pulse=None, progress=None, lock_held=False, now=1000.0
        )
        self.assertEqual(verdict.state, DAEMON_STATE_LOCK_STALE)
        self.assertEqual(verdict.reason, "pulse_absent")
        self.assertEqual(verdict.action, DAEMON_ACTION_SPAWN)

    def test_child_exit_evidence_outranks_a_fresh_pulse(self):
        """Прямое свидетельство exit-а сильнее свежести кэша (кэш может отставать)."""
        verdict = classify_daemon_supervision(
            pulse={"at": 1000.0},
            progress={"at": 1000.0},
            lock_held=True,
            child_exit_code=7,
            now=1000.0,
            alive_window_seconds=60,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_CHILD_EXITED)
        self.assertEqual(verdict.action, DAEMON_ACTION_SPAWN)
        self.assertEqual(verdict.child_exit_code, 7)

    def test_child_signal_evidence_is_also_a_child_exit(self):
        verdict = classify_daemon_supervision(
            pulse={"at": 1000.0},
            progress={"at": 1000.0},
            lock_held=True,
            child_exit_signal=9,
            now=1000.0,
            alive_window_seconds=60,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_CHILD_EXITED)
        self.assertEqual(verdict.child_exit_signal, 9)

    def test_inflight_operation_progress_counts_as_progress(self):
        """Прогресс полосы «в полёте» — законное доказательство хода."""
        verdict = classify_daemon_supervision(
            pulse={"at": 1000.0, "progress_at": 995.0, "inflight_operation": CUSTOMER_LANE},
            progress={"at": 500.0},
            lock_held=True,
            now=1000.0,
            alive_window_seconds=60,
            no_progress_after_seconds=120,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_PROGRESSING)
        self.assertEqual(verdict.inflight_operation, CUSTOMER_LANE)

    def test_no_progress_threshold_is_derived_from_the_declared_turn_budget(self):
        """Независимое число здесь разошлось бы с бюджетом хода при первой правке."""
        from management.services.ig_turn_budget import heartbeat_alive_window_seconds

        self.assertEqual(
            daemon_no_progress_after_seconds(),
            max(60, heartbeat_alive_window_seconds() * 2),
        )

    def test_malformed_pulse_payload_is_treated_as_no_owner(self):
        verdict = classify_daemon_supervision(
            pulse={"at": "not-a-number"},
            progress={"at": None},
            lock_held=True,
            now=1000.0,
        )
        self.assertEqual(verdict.state, DAEMON_STATE_LOCK_STALE)

    @override_settings(IG_DAEMON_SUPERVISION_STATES=False)
    def test_disabled_flag_makes_the_watchdog_behave_as_before(self):
        self.assertIsNone(observe_daemon_supervision(lock_held=True))


class WatchdogDoesNotDuplicateLiveDaemonTests(SimpleTestCase):
    """ЭА.14 — OS-lock и pulse согласованы: второго демона над живым нет."""

    def setUp(self):
        cache.delete(HB_KEY)
        cache.delete(MAIN_PROGRESS_KEY)
        reset_inflight_operations()
        self.addCleanup(cache.delete, HB_KEY)
        self.addCleanup(cache.delete, MAIN_PROGRESS_KEY)
        self.addCleanup(reset_inflight_operations)

    def _publish_live_daemon(self):
        _INFLIGHT.note_completed_cycle()
        _publish_process_pulse(owner="live", start_sentinel=1.0, state="running")
        runner._publish_main_progress(
            owner="live", start_sentinel=1.0, cycle=3, state="idle"
        )

    def test_live_progressing_daemon_is_never_replaced(self):
        self._publish_live_daemon()
        with (
            patch.object(runner, "_process_lock_held", return_value=True),
            patch.object(runner, "_daemon_code_current", return_value=True),
            patch.object(runner, "_supervisor_active", return_value=False),
            patch.object(runner.subprocess, "Popen") as popen,
        ):
            command = Command()
            with patch.object(command, "stdout") as stdout:
                command._ensure()

        popen.assert_not_called()
        stdout.write.assert_called_with("daemon alive — ok")

    def test_live_daemon_verdict_is_progressing(self):
        self._publish_live_daemon()
        verdict = daemon_supervision_verdict(lock_held=True)
        self.assertEqual(verdict.state, DAEMON_STATE_PROGRESSING)

    def test_live_but_stuck_daemon_is_escalated_instead_of_replaced(self):
        """Живой, но не двигающийся демон → алерт, а не второй процесс."""
        stale_moment = time.time() - (daemon_no_progress_after_seconds() + 60)
        _publish_process_pulse(owner="stuck", start_sentinel=1.0, state="running")
        pulse = cache.get(HB_KEY)
        pulse["progress_at"] = stale_moment
        pulse["last_completed_cycle_at"] = stale_moment
        cache.set(HB_KEY, pulse, 600)
        cache.set(
            MAIN_PROGRESS_KEY,
            {"at": stale_moment, "progress_at": stale_moment, "cycle": 3},
            600,
        )

        with (
            patch.object(runner, "_process_lock_held", return_value=True),
            patch.object(runner, "_daemon_code_current", return_value=True),
            patch.object(runner, "_supervisor_active", return_value=False),
            patch.object(runner, "_daemon_alive", return_value=True),
            patch.object(runner.subprocess, "Popen") as popen,
            patch.object(runner.bot, "log") as log,
            patch.object(bot, "notify_manager") as notify,
        ):
            command = Command()
            with patch.object(command, "stdout"):
                command._ensure()

        popen.assert_not_called()
        self.assertTrue(
            any(call.args[1] == "daemon_no_progress" for call in log.call_args_list),
            log.call_args_list,
        )
        notify.assert_called_once()


class CustomerLanePriorityTests(SimpleTestCase):
    """ЭА.15 — обслуживающие задачи не стоят перед обработкой входящих."""

    databases = {"default"}

    def setUp(self):
        reset_service_lanes()
        reset_inflight_operations()
        self.addCleanup(reset_service_lanes)
        self.addCleanup(reset_inflight_operations)

    SLOW_DRAIN_SECONDS = 0.3

    def _run_cycle_with_slow_drain(self):
        harness = _CycleHarness()

        def slow_drain(*_args, **_kwargs):
            harness.record("drain_start")
            time.sleep(self.SLOW_DRAIN_SECONDS)
            harness.record("drain_end")

        def process_pending(*_args, **_kwargs):
            harness.record("inbound")

        with (
            patch.object(runner.bot, "drain_manager_notifications", side_effect=slow_drain),
            patch.object(runner.bot, "process_pending", side_effect=process_pending),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(runner.cache, "add", return_value=False),
            patch.object(runner, "_reclaim_lease_guard"),
        ):
            _run_work_cycle(_settings(), 0.0)
        return harness

    def test_long_manager_notification_drain_does_not_delay_inbound(self):
        harness = self._run_cycle_with_slow_drain()

        self.assertEqual(harness.order()[0], "inbound")
        self.assertLess(harness.at("inbound"), harness.at("drain_start"))

    @override_settings(IG_BOT_SERVICE_TASK_ISOLATION=False)
    def test_control_legacy_order_makes_inbound_wait_for_the_drain(self):
        """КОНТРОЛЬ: прежний порядок показывает исправляемый дефект.

        Без этого контроля предыдущий тест проходил бы и на прежнем коде.
        """
        harness = self._run_cycle_with_slow_drain()

        self.assertEqual(harness.order()[0], "drain_start")
        self.assertGreaterEqual(
            harness.at("inbound") - harness.at("drain_start"),
            self.SLOW_DRAIN_SECONDS,
        )

    def test_each_lane_reports_its_own_pulse(self):
        """Свой pulse у каждой полосы: иначе ЭА.14 видел бы прогресс не там, где он есть."""
        harness = _CycleHarness()

        with (
            patch.object(
                runner.bot,
                "drain_manager_notifications",
                side_effect=lambda *a, **k: harness.record("drain"),
            ),
            patch.object(
                runner.bot,
                "process_pending",
                side_effect=lambda *a, **k: harness.record("inbound"),
            ),
            patch.object(
                runner.bot,
                "refresh_profiles_batch",
                side_effect=lambda *a, **k: harness.record("profiles"),
            ),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(runner.cache, "add", return_value=True),
            patch.object(runner, "_reclaim_lease_guard"),
        ):
            _run_work_cycle(_settings(), 0.0)

        self.assertEqual(harness.lanes["inbound"], CUSTOMER_LANE)
        self.assertEqual(harness.lanes["drain"], SERVICE_LANE_NOTIFICATIONS)
        self.assertEqual(harness.lanes["profiles"], SERVICE_LANE_PROFILES)

    def test_service_lane_runs_on_the_cycle_thread_so_it_cannot_race_recovery(self):
        """Уведомления и recovery оба пишут `IgBotNotification`.

        Поэтому drain НЕ вынесен в свой поток: иначе появился бы второй писатель
        одной строки. Тест фиксирует именно это структурное свойство — вынос
        обслуживающей полосы не создал новой конкуренции за запись.
        """
        observed = {}

        with (
            patch.object(
                runner.bot,
                "drain_manager_notifications",
                side_effect=lambda *a, **k: observed.update(
                    thread=threading.current_thread().name
                ),
            ),
            patch.object(runner.bot, "process_pending"),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(runner.cache, "add", return_value=False),
            patch.object(runner, "_reclaim_lease_guard"),
        ):
            _run_work_cycle(_settings(), 0.0)

        self.assertEqual(observed["thread"], threading.current_thread().name)

    def test_maintenance_inside_customer_lane_stops_before_service_lanes(self):
        drained = []
        with (
            patch.object(
                runner.bot,
                "drain_manager_notifications",
                side_effect=lambda *a, **k: drained.append(1),
            ),
            patch.object(runner.bot, "process_pending"),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(
                runner, "maintenance_status", return_value={"active": True}
            ),
            patch.object(runner.cache, "add", return_value=False),
        ):
            enabled, last_poll = _run_work_cycle(_settings(), 17.0)

        self.assertTrue(enabled)
        self.assertEqual(last_poll, 17.0)
        self.assertEqual(drained, [])


class ServiceLaneBudgetTests(SimpleTestCase):
    """ЭА.15 — одна обслуживающая полоса не может занять цикл целиком."""

    def setUp(self):
        reset_service_lanes()
        reset_inflight_operations()
        self.addCleanup(reset_service_lanes)
        self.addCleanup(reset_inflight_operations)

    def _cycle(self, drain):
        with (
            patch.object(runner.bot, "drain_manager_notifications", side_effect=drain),
            patch.object(runner.bot, "process_pending"),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(runner.cache, "add", return_value=False),
            patch.object(runner, "_reclaim_lease_guard"),
            patch.object(runner.bot, "log"),
        ):
            _run_work_cycle(_settings(), 0.0)

    def test_overrunning_lane_loses_the_next_cycles(self):
        calls = []
        tiny_budget = dict(SERVICE_LANE_BUDGET_SECONDS)
        tiny_budget[SERVICE_LANE_NOTIFICATIONS] = 0.05

        def slow_drain(*_args, **_kwargs):
            calls.append(1)
            time.sleep(0.12)

        with patch.dict(
            runner.SERVICE_LANE_BUDGET_SECONDS, tiny_budget, clear=True
        ):
            self._cycle(slow_drain)
            self.assertEqual(len(calls), 1)
            for _ in range(SERVICE_LANE_DEFER_CYCLES - 1):
                self._cycle(slow_drain)
                self.assertEqual(len(calls), 1, "полоса не отложена после перерасхода")
            self._cycle(slow_drain)
            self.assertEqual(len(calls), 2, "полоса не вернулась после отсрочки")

    def test_lane_within_budget_runs_every_cycle(self):
        calls = []
        self._cycle(lambda *a, **k: calls.append(1))
        self._cycle(lambda *a, **k: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_overrun_is_logged_once_with_the_lane_name(self):
        tiny_budget = dict(SERVICE_LANE_BUDGET_SECONDS)
        tiny_budget[SERVICE_LANE_NOTIFICATIONS] = 0.01
        with (
            patch.dict(runner.SERVICE_LANE_BUDGET_SECONDS, tiny_budget, clear=True),
            patch.object(runner.bot, "drain_manager_notifications", side_effect=lambda *a, **k: time.sleep(0.05)),
            patch.object(runner.bot, "process_pending"),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(runner.cache, "add", return_value=False),
            patch.object(runner, "_reclaim_lease_guard"),
            patch.object(runner.bot, "log") as log,
        ):
            _run_work_cycle(_settings(), 0.0)

        overruns = [
            call for call in log.call_args_list
            if call.args[1] == "service_lane_overrun"
        ]
        self.assertEqual(len(overruns), 1)
        self.assertIn(SERVICE_LANE_NOTIFICATIONS, overruns[0].args[2])

    def test_lane_failure_never_stops_the_customer_lane(self):
        pending = []
        with (
            patch.object(
                runner.bot,
                "drain_manager_notifications",
                side_effect=RuntimeError("outbox unavailable"),
            ),
            patch.object(
                runner.bot,
                "process_pending",
                side_effect=lambda *a, **k: pending.append(1),
            ),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(runner.cache, "add", return_value=False),
            patch.object(runner, "_reclaim_lease_guard"),
            patch.object(runner.bot, "log") as log,
        ):
            enabled, last_poll = _run_work_cycle(_settings(), 23.0)

        self.assertTrue(enabled)
        self.assertEqual(last_poll, 23.0)
        self.assertEqual(pending, [1])
        self.assertEqual(
            [call.args[1] for call in log.call_args_list], ["notification_outbox"]
        )

    def test_lane_durations_are_measurable(self):
        """Замер ЭА.15: длительность каждой полосы наблюдаема без чтения логов."""
        with (
            patch.object(runner.bot, "drain_manager_notifications"),
            patch.object(runner.bot, "process_pending"),
            patch.object(runner.bot_followups, "process_due_followups"),
            patch.object(runner.cache, "add", return_value=False),
            patch.object(runner, "_reclaim_lease_guard"),
        ):
            _run_work_cycle(_settings(), 0.0)

        timings = service_lane_timings()
        self.assertIn(SERVICE_LANE_NOTIFICATIONS, timings["lane_ms"])
        self.assertEqual(timings["lane_deferred"], [])


class OperationalLeaseAuthorityTests(SimpleTestCase):
    """ЭА.14 — длительность lease выведена, а не назначена независимо."""

    def test_lease_is_derived_from_the_turn_lease_authority(self):
        from django.conf import settings as django_settings

        from management.services.ig_customer_turns import turn_lease_seconds
        from management.services.ig_task_health import (
            OPERATIONAL_LEASE_MARGIN_SECONDS,
        )

        expected = max(
            60,
            int(turn_lease_seconds() + OPERATIONAL_LEASE_MARGIN_SECONDS),
            int(getattr(django_settings, "IG_BOT_AUTOMATION_LEASE_SECONDS", 0)),
        )
        self.assertEqual(operational_lease_seconds(), expected)

    def test_reclaim_age_is_strictly_later_than_lease_expiry(self):
        """Reclaim раньше истечения lease отобрал бы строку у живого владельца."""
        self.assertGreater(operational_reclaim_age_seconds(), operational_lease_seconds())

    @override_settings(
        IG_BOT_OPERATIONAL_RECLAIM_LEASE=False,
        IG_BOT_STALE_PROCESSING_SECONDS=300,
    )
    def test_disabled_flag_restores_the_absolute_threshold(self):
        self.assertFalse(operational_reclaim_lease_enabled())
        self.assertEqual(operational_reclaim_age_seconds(), 300)

    def test_owner_that_keeps_renewing_never_loses_the_row(self):
        now = timezone.now()
        self.assertFalse(
            processing_lease_expired(
                claimed_at=now - timedelta(seconds=5000),
                progress_at=now - timedelta(seconds=10),
                now=now,
                lease_seconds=120,
            )
        )

    def test_owner_that_stopped_renewing_loses_the_row(self):
        now = timezone.now()
        self.assertTrue(
            processing_lease_expired(
                claimed_at=now - timedelta(seconds=5000),
                progress_at=now - timedelta(seconds=500),
                now=now,
                lease_seconds=120,
            )
        )

    def test_row_without_a_claim_marker_has_no_owner_to_protect(self):
        self.assertTrue(processing_lease_expired(claimed_at=None))


class ReclaimRaceTests(TestCase):
    """ЭА.14 — старый worker не должен дописать результат после reclaim."""

    def setUp(self):
        reset_service_lanes()
        reset_inflight_operations()
        self.addCleanup(reset_service_lanes)
        self.addCleanup(reset_inflight_operations)
        self.client_row = IgClient.objects.create(igsid="race-1")

    def _processing_row(self, *, processing_age_seconds, client=None):
        row = InstagramBotMessage.objects.create(
            sender_id="race-1",
            role=InstagramBotMessage.Role.USER,
            text="привіт",
            status=InstagramBotMessage.Status.PROCESSING,
            attempts=1,
            client=client,
        )
        InstagramBotMessage.objects.filter(pk=row.pk).update(
            processing_started_at=timezone.now()
            - timedelta(seconds=processing_age_seconds)
        )
        row.refresh_from_db()
        return row

    def test_stale_worker_cannot_write_a_result_after_reclaim(self):
        """Гонка: worker прочитал строку, reclaim её отобрал, worker пишет результат.

        Это и есть опасный порядок: без fencing-токена отобранная строка получила
        бы `DONE` от прежнего владельца, а новый владелец успел бы ответить клиенту
        второй раз. Проверяется, что запись прежнего владельца не проходит.
        """
        row = self._processing_row(processing_age_seconds=600)
        stale_worker_view = InstagramBotMessage.objects.get(pk=row.pk)

        self.assertEqual(bot.reclaim_stale_processing(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.PENDING)
        self.assertIsNone(row.processing_started_at)

        written = bot._own_processing_claim(stale_worker_view).update(
            status=InstagramBotMessage.Status.DONE,
            processed_at=timezone.now(),
        )

        self.assertEqual(written, 0, "прежний владелец дописал результат после reclaim")
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.PENDING)

    def test_new_owner_claim_invalidates_the_previous_owner_token(self):
        """После повторного claim прежний токен тоже недействителен."""
        row = self._processing_row(processing_age_seconds=600)
        stale_worker_view = InstagramBotMessage.objects.get(pk=row.pk)

        self.assertEqual(bot.reclaim_stale_processing(), 1)
        row.refresh_from_db()
        reclaimed = bot._claim_exact_row(row)
        self.assertIsNotNone(reclaimed)

        self.assertEqual(
            bot._own_processing_claim(stale_worker_view).update(
                status=InstagramBotMessage.Status.DONE
            ),
            0,
        )
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.PROCESSING)

    def test_live_operational_lease_protects_the_row_from_reclaim(self):
        """Reclaim ключуется на операционный lease, а не только на часы.

        Строка старше абсолютного порога, но владелец продлевает lease клиента —
        отбирать нельзя.
        """
        row = self._processing_row(
            processing_age_seconds=600, client=self.client_row
        )
        IgClient.objects.filter(pk=self.client_row.pk).update(
            automation_lease_token="live-owner",
            automation_lease_until=timezone.now() + timedelta(seconds=120),
        )

        self.assertEqual(bot.reclaim_stale_processing(), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.PROCESSING)

    def test_expired_operational_lease_releases_the_row(self):
        row = self._processing_row(
            processing_age_seconds=600, client=self.client_row
        )
        IgClient.objects.filter(pk=self.client_row.pk).update(
            automation_lease_token="dead-owner",
            automation_lease_until=timezone.now() - timedelta(seconds=1),
        )

        self.assertEqual(bot.reclaim_stale_processing(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.PENDING)

    def test_lease_guard_reports_rows_the_absolute_threshold_would_steal(self):
        """Guardrail: расхождение абсолютного порога и живого lease должно быть видно."""
        self._processing_row(processing_age_seconds=600, client=self.client_row)
        IgClient.objects.filter(pk=self.client_row.pk).update(
            automation_lease_token="live-owner",
            automation_lease_until=timezone.now(),
        )

        with patch.object(runner.bot, "log") as log:
            runner._reclaim_lease_guard()

        conflicts = [
            call for call in log.call_args_list
            if call.args[1] == "reclaim_lease_conflict"
        ]
        self.assertEqual(len(conflicts), 1)

    def test_lease_guard_stays_silent_when_no_row_is_at_risk(self):
        self._processing_row(processing_age_seconds=600, client=self.client_row)

        with patch.object(runner.bot, "log") as log:
            runner._reclaim_lease_guard()

        self.assertEqual(log.call_args_list, [])


class DaemonStartupObservabilityTests(SimpleTestCase):
    """ЭА.14 — окно старта наблюдаемо: демон держит lock и уже доказывает живость."""

    # The daemon now acquires the durable analysis-lane owner before startup
    # reconciliation, so these startup contract tests must permit that read.
    databases = {"default"}

    def setUp(self):
        IgWorkerLaneState.objects.update(
            owner_kind="", owner_token="", lease_until=None,
            claim_frozen=False, frozen_at=None, recovery_deadline_at=None,
        )
        cache.delete(HB_KEY)
        cache.delete(MAIN_PROGRESS_KEY)
        reset_inflight_operations()
        self.addCleanup(cache.delete, HB_KEY)
        self.addCleanup(cache.delete, MAIN_PROGRESS_KEY)
        self.addCleanup(reset_inflight_operations)

    def test_release_reconcile_runs_under_a_published_pulse(self):
        """Пульс публикуется ДО реконсиляции релизного окна, а не после неё.

        Раньше процесс уже держал singleton-lock, но не публиковал ничего до
        возврата из реконсиляции: `_daemon_alive()` ложен, `_daemon_code_current()`
        ложен — watchdog уходил в ветку замены владельца. Окно старта выглядело
        зависшим демоном.
        """
        observed = {}

        def reconcile():
            observed["pulse"] = cache.get(HB_KEY)
            observed["inflight"] = _INFLIGHT.snapshot()["inflight_operation"]
            raise RuntimeError("stop the daemon right after reconcile")

        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(
                    runner, "PID_FILE", f"{temp_dir}/ig_bot.pid"
                ),
                patch.object(
                    runner,
                    "_reconcile_commercial_episodes_after_reload",
                    side_effect=reconcile,
                ),
                patch.object(
                    runner, "maintenance_status", return_value={"active": False}
                ),
                patch.object(runner.bot, "log"),
            ):
                with self.assertRaisesMessage(RuntimeError, "stop the daemon"):
                    Command()._forever_locked()

        self.assertIsInstance(observed["pulse"], dict)
        self.assertEqual(observed["pulse"]["state"], "starting")
        self.assertEqual(observed["inflight"], "release_reconcile")

    def test_failed_reconcile_does_not_leak_the_pulse_thread(self):
        import tempfile

        before = {thread.name for thread in threading.enumerate()}
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(runner, "PID_FILE", f"{temp_dir}/ig_bot.pid"),
                patch.object(
                    runner,
                    "_reconcile_commercial_episodes_after_reload",
                    side_effect=RuntimeError("reconcile unavailable"),
                ),
                patch.object(
                    runner, "maintenance_status", return_value={"active": False}
                ),
                patch.object(runner.bot, "log"),
            ):
                with self.assertRaisesMessage(RuntimeError, "reconcile unavailable"):
                    Command()._forever_locked()

        time.sleep(0.05)
        leaked = {
            thread.name
            for thread in threading.enumerate()
            if thread.name == "ig-process-pulse"
        } - before
        self.assertEqual(leaked, set())


class MariaDbLeaseContractTests(TestCase):
    """ЭА.14 — контракт lease на реальном engine, а не только на SQLite.

    Пропускается на SQLite: он не воспроизводит ни `SELECT ... FOR UPDATE`, ни
    поведение InnoDB при конкурентном обновлении. Запускается в MariaDB-профиле.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor != "mysql":
            raise __import__("unittest").SkipTest(
                "lease contract requires MariaDB/InnoDB, not SQLite"
            )

    def test_conditional_claim_update_is_atomic_under_innodb(self):
        client_row = IgClient.objects.create(igsid="lease-mysql")
        row = InstagramBotMessage.objects.create(
            sender_id="lease-mysql",
            role=InstagramBotMessage.Role.USER,
            text="привіт",
            status=InstagramBotMessage.Status.PROCESSING,
            attempts=1,
            client=client_row,
        )
        InstagramBotMessage.objects.filter(pk=row.pk).update(
            processing_started_at=timezone.now() - timedelta(seconds=600)
        )
        row.refresh_from_db()
        stale_view = InstagramBotMessage.objects.get(pk=row.pk)

        self.assertEqual(bot.reclaim_stale_processing(), 1)
        self.assertEqual(
            bot._own_processing_claim(stale_view).update(
                status=InstagramBotMessage.Status.DONE
            ),
            0,
        )
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.PENDING)
