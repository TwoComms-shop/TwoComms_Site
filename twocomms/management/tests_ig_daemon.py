"""Regression tests for the Instagram bot daemon/watchdog boundary."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.management.commands.run_instagram_bot import (
    ANALYSIS_RECONCILE_BATCH,
    ANALYSIS_RECONCILE_EVERY,
    CONV_REFRESH_EVERY,
    CONV_REFRESH_PROGRESS_EVERY,
    DAEMON_LOCK_FILE,
    DAEMON_START_WAIT_SECONDS,
    DAEMON_LOCK_KEY,
    HB_KEY,
    MAIN_PROGRESS_KEY,
    MANAGE_PY_PATH,
    PROJECT_ROOT,
    RELOAD_LOCK_WAIT_SECONDS,
    Command,
    _daemon_alive,
    _daemon_runtime_hooks,
    _ai_reply_recovery_worker,
    _analysis_worker,
    _permission_transition_worker,
    _conversation_refresh_wait_seconds,
    _process_lock_held,
    _publish_main_progress,
    _publish_process_pulse,
    _reconcile_commercial_episodes_after_reload,
    _run_work_cycle,
)
from management.management.commands import run_instagram_bot as runner
from management.models import IgClient, InstagramBotSettings
from management.services import instagram_bot as bot
from management.services.ig_maintenance import (
    MaintenanceLeaseConflict,
    activate_maintenance,
    deactivate_maintenance,
    maintenance_status,
    notification_send_boundary,
)
from management.services.ig_reply_boundary import (
    ReplyBoundaryTimeout,
    ReplyPermission,
    customer_send_boundary,
    pause_reply_boundary,
    reply_execution_boundary,
)


class DaemonPathTests(SimpleTestCase):
    @override_settings(DEBUG=False)
    def test_production_sqlite_refuses_every_execution_mode_before_work(self):
        with (
            patch.object(runner, "connection", SimpleNamespace(vendor="sqlite"), create=True),
            patch.object(bot, "poll_once") as poll_once,
            patch.object(runner.InstagramBotSettings, "load", return_value=object()),
            patch.object(Command, "_ensure") as ensure,
            patch.object(Command, "_forever") as forever,
            patch.object(runner, "task_heartbeat", return_value=nullcontext()) as heartbeat,
        ):
            modes = {
                "--once": {"once": True},
                "--ensure": {"ensure": True},
                "--forever": {"forever": True},
            }
            common = {
                "once": False,
                "ensure": False,
                "forever": False,
                "maintenance_on": None,
                "maintenance_off": None,
                "maintenance_lease_id": None,
                "maintenance_wait_seconds": None,
            }
            for option, mode in modes.items():
                options = {**common, **mode}
                with self.subTest(option=option), self.assertRaisesMessage(
                    CommandError,
                    "SQLite",
                ):
                    Command().handle(**options)

        poll_once.assert_not_called()
        ensure.assert_not_called()
        forever.assert_not_called()
        heartbeat.assert_not_called()

    @override_settings(DEBUG=True)
    def test_debug_sqlite_allows_execution_mode(self):
        with (
            patch.object(runner, "connection", SimpleNamespace(vendor="sqlite"), create=True),
            patch.object(Command, "_ensure") as ensure,
            patch.object(runner, "task_heartbeat", return_value=nullcontext()) as heartbeat,
        ):
            Command().handle(
                once=False,
                ensure=True,
                forever=False,
                maintenance_on=None,
                maintenance_off=None,
                maintenance_lease_id=None,
                maintenance_wait_seconds=None,
            )

        ensure.assert_called_once()
        heartbeat.assert_called_once_with("ig_daemon_watchdog")

    def test_watchdog_uses_absolute_project_manage_path(self):
        self.assertTrue(os.path.isabs(MANAGE_PY_PATH))
        self.assertTrue(MANAGE_PY_PATH.endswith(os.path.join("twocomms", "manage.py")))
        self.assertEqual(PROJECT_ROOT, os.path.dirname(MANAGE_PY_PATH))

    def test_release_runtime_root_routes_maintenance_and_daemon_files_to_live_tmp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = os.path.join(temp_dir, "live")
            os.makedirs(runtime_root)
            with open(os.path.join(runtime_root, "manage.py"), "w", encoding="utf-8"):
                pass
            env = os.environ.copy()
            env["TWC_IG_RUNTIME_ROOT"] = runtime_root
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [PROJECT_ROOT, env.get("PYTHONPATH", "")])
            )
            child_code = """
import json
import django
django.setup()
from management.services import ig_maintenance as maintenance
from management.management.commands import run_instagram_bot as runner
print(json.dumps({
    'maintenance': maintenance.MAINTENANCE_FILE,
    'maintenance_lock': maintenance.MAINTENANCE_LOCK_FILE,
    'notification_lock': maintenance.NOTIFICATION_SEND_LOCK_FILE,
    'daemon_lock': runner.DAEMON_LOCK_FILE,
    'spawn_lock': runner.SPAWN_LOCK_FILE,
    'starting': runner.STARTING_FILE,
    'pid': runner.PID_FILE,
}))
"""
            result = subprocess.run(
                [sys.executable, "-c", child_code],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            paths = json.loads(result.stdout)
            self.assertEqual(
                set(paths.values()),
                {
                    os.path.realpath(os.path.join(runtime_root, "tmp", "ig_bot_maintenance.json")),
                    os.path.realpath(os.path.join(runtime_root, "tmp", "ig_bot_maintenance.lock")),
                    os.path.realpath(os.path.join(runtime_root, "tmp", "ig_bot_notification_send.lock")),
                    os.path.realpath(os.path.join(runtime_root, "tmp", "ig_bot_daemon.lock")),
                    os.path.realpath(os.path.join(runtime_root, "tmp", "ig_bot_spawn.lock")),
                    os.path.realpath(os.path.join(runtime_root, "tmp", "ig_bot_starting.json")),
                    os.path.realpath(os.path.join(runtime_root, "tmp", "ig_bot.pid")),
                },
            )

    def test_starting_marker_distinguishes_current_and_stale_child(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "management.management.commands.run_instagram_bot.STARTING_FILE",
            os.path.join(temp_dir, "starting.json"),
        ), patch(
            "management.management.commands.run_instagram_bot._restart_sentinel_mtime",
            return_value=5.0,
        ), patch(
            "management.management.commands.run_instagram_bot.time.time",
            return_value=100.0,
        ):
            runner._record_starting_child(os.getpid())

            self.assertEqual(runner._starting_child_state(now=110.0), "current")
            self.assertEqual(runner._starting_child_state(now=221.0), "stale")

    def test_release_runtime_root_rejects_relative_paths(self):
        env = os.environ.copy()
        env["TWC_IG_RUNTIME_ROOT"] = "relative/live"
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [PROJECT_ROOT, env.get("PYTHONPATH", "")])
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from management.services import ig_maintenance",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TWC_IG_RUNTIME_ROOT", result.stderr)

    def test_conversation_refresh_backs_off_when_provider_is_degraded(self):
        settings = InstagramBotSettings(conversation_discovery_cursor="CURSOR")

        with patch.object(
            bot,
            "_current_ingress_degradation",
            return_value={"state": "conversation_refresh_failed"},
        ):
            self.assertEqual(
                _conversation_refresh_wait_seconds(settings),
                CONV_REFRESH_EVERY,
            )

    def test_conversation_refresh_progresses_quickly_without_degradation(self):
        settings = InstagramBotSettings(conversation_discovery_cursor="CURSOR")

        with patch.object(bot, "_current_ingress_degradation", return_value=None):
            self.assertEqual(
                _conversation_refresh_wait_seconds(settings),
                CONV_REFRESH_PROGRESS_EVERY,
            )

    @patch("django.core.management.call_command")
    def test_new_daemon_reconciles_release_window_episodes_before_work(self, call_command):
        _reconcile_commercial_episodes_after_reload()

        call_command.assert_called_once_with(
            "reconcile_ig_commercial_episodes",
            passes=3,
        )

    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=True)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot.activate_maintenance")
    def test_maintenance_on_uses_explicit_bounded_lock_wait(
        self, activate, _held, wait_for_lock
    ):
        activate.return_value = {
            "lease_id": "deploy-test",
            "expires_at": 9999999999,
        }

        Command()._maintenance_on(
            900,
            requested_lease_id="deploy-test",
            wait_seconds=120,
        )

        wait_for_lock.assert_called_once_with(
            DAEMON_LOCK_FILE,
            held=False,
            timeout=120,
        )

    def test_maintenance_on_rejects_wait_above_hard_bound(self):
        with self.assertRaisesMessage(CommandError, "maintenance lock wait"):
            Command()._maintenance_on(900, wait_seconds=301)

    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=False)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    def test_maintenance_on_cleans_owned_lease_when_daemon_drain_times_out(
        self, _held, _wait_for_lock
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = os.path.join(temp_dir, "maintenance.json")
            with patch(
                "management.management.commands.run_instagram_bot.MAINTENANCE_FILE",
                marker,
            ):
                with self.assertRaisesMessage(CommandError, "daemon did not stop"):
                    Command()._maintenance_on(
                        900,
                        requested_lease_id="deploy-timeout",
                        wait_seconds=0,
                    )

            self.assertFalse(os.path.exists(marker))

    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    def test_maintenance_timeout_cleanup_preserves_replacement_lease(self, _held):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = os.path.join(temp_dir, "maintenance.json")

            def replace_owned_lease(*_args, **_kwargs):
                with open(marker, "w", encoding="utf-8") as marker_file:
                    json.dump(
                        {
                            "version": 1,
                            "lease_id": "foreign-replacement",
                            "started_at": time.time(),
                            "expires_at": time.time() + 900,
                            "actor": "other-deploy",
                        },
                        marker_file,
                    )
                return False

            with (
                patch(
                    "management.management.commands.run_instagram_bot.MAINTENANCE_FILE",
                    marker,
                ),
                patch(
                    "management.management.commands.run_instagram_bot._wait_for_lock",
                    side_effect=replace_owned_lease,
                ),
                patch(
                    "management.management.commands.run_instagram_bot.deactivate_maintenance",
                    wraps=deactivate_maintenance,
                ) as cleanup,
            ):
                with self.assertRaisesMessage(CommandError, "daemon did not stop"):
                    Command()._maintenance_on(
                        900,
                        requested_lease_id="deploy-owned",
                        wait_seconds=0,
                    )

            cleanup.assert_called_once_with(
                path=marker,
                lease_id="deploy-owned",
            )
            self.assertEqual(
                maintenance_status(path=marker).get("lease_id"),
                "foreign-replacement",
            )

    @patch("management.management.commands.run_instagram_bot._wait_for_daemon_ready", return_value=True)
    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=True)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_ensure_spawns_from_project_root_with_absolute_manage_path(self, _held, _wait, popen, _ready):
        command = Command()

        with patch.object(command, "stdout") as stdout:
            command._ensure()

        args, kwargs = popen.call_args
        self.assertEqual(args[0][:3], [os.sys.executable, MANAGE_PY_PATH, "run_instagram_bot"])
        self.assertEqual(kwargs["cwd"], PROJECT_ROOT)
        self.assertTrue(os.path.isabs(args[0][1]))
        _wait.assert_called_once_with(
            DAEMON_LOCK_FILE,
            held=True,
            timeout=DAEMON_START_WAIT_SECONDS,
        )
        stdout.write.assert_called()

    @patch("management.management.commands.run_instagram_bot._wait_for_daemon_ready", return_value=True)
    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=True)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=False)
    def test_ensure_replaces_old_worker_after_restart_sentinel(
        self, _current, _held, _wait, popen, _ready
    ):
        command = Command()
        with patch.object(command, "stdout") as stdout:
            command._ensure()
        popen.assert_called_once()
        stdout.write.assert_called()

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_alive", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=True)
    def test_ensure_does_not_spawn_over_current_process_lock(self, _current, _alive, _held, popen):
        command = Command()
        with patch.object(command, "stdout") as stdout:
            command._ensure()
        popen.assert_not_called()
        stdout.write.assert_called_with("daemon alive — ok")

    @patch("management.management.commands.run_instagram_bot._wait_for_daemon_ready", return_value=True)
    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=True)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_alive", return_value=False)
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=True)
    def test_ensure_restarts_when_current_code_has_stale_heartbeat(
        self, _current, _alive, _held, wait, popen, _ready
    ):
        command = Command()

        with patch.object(command, "stdout"):
            command._ensure()

        self.assertEqual(wait.call_args_list[0].args, (DAEMON_LOCK_FILE,))
        self.assertEqual(wait.call_args_list[0].kwargs, {
            "held": False,
            "timeout": RELOAD_LOCK_WAIT_SECONDS,
        })
        self.assertEqual(wait.call_args_list[1].args, (DAEMON_LOCK_FILE,))
        self.assertEqual(wait.call_args_list[1].kwargs, {
            "held": True,
            "timeout": DAEMON_START_WAIT_SECONDS,
        })
        popen.assert_called_once()

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=False)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_alive", return_value=False)
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=True)
    def test_ensure_fails_closed_when_stale_worker_keeps_lock(
        self, _current, _alive, _held, wait, popen
    ):
        command = Command()

        with self.assertRaisesMessage(CommandError, "did not release singleton lock"):
            command._ensure()

        wait.assert_called_once_with(
            DAEMON_LOCK_FILE,
            held=False,
            timeout=RELOAD_LOCK_WAIT_SECONDS,
        )
        popen.assert_not_called()

    def test_process_lock_is_exclusive_across_real_processes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = os.path.join(temp_dir, "daemon.lock")
            child_code = (
                "import fcntl,sys,time; "
                "f=open(sys.argv[1],'a+'); "
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
                "print('locked', flush=True); time.sleep(10)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, lock_path],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                self.assertTrue(_process_lock_held(lock_path))
            finally:
                child.terminate()
                child.wait(timeout=5)
            self.assertFalse(_process_lock_held(lock_path))


    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=False)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_ensure_records_live_child_as_starting_instead_of_false_failure(self, _held, _wait, popen):
        popen.return_value.pid = 4321
        popen.return_value.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "management.management.commands.run_instagram_bot.STARTING_FILE",
            os.path.join(temp_dir, "starting.json"),
        ):
            command = Command()
            with patch.object(command, "stdout") as stdout:
                command._ensure()
            stdout.write.assert_called_with("daemon starting — pending")
        popen.assert_called_once()

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch(
        "management.management.commands.run_instagram_bot._starting_child_state",
        return_value="current",
    )
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_ensure_does_not_duplicate_a_live_starting_child(self, _held, _state, popen):
        command = Command()

        with patch.object(command, "stdout") as stdout:
            command._ensure()

        popen.assert_not_called()
        stdout.write.assert_called_with("daemon starting — pending")

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch(
        "management.management.commands.run_instagram_bot._starting_child_state",
        return_value="stale",
    )
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_ensure_fails_without_duplicate_when_starting_child_is_stale(
        self, _held, _state, popen
    ):
        with self.assertRaisesMessage(CommandError, "startup exceeded"):
            Command()._ensure()

        popen.assert_not_called()

    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch(
        "management.management.commands.run_instagram_bot._wait_for_daemon_ready",
        return_value=False,
    )
    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=True)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_lock_without_heartbeat_remains_pending_inside_startup_window(
        self, _held, _wait, popen, _ready, log
    ):
        popen.return_value.pid = os.getpid()
        popen.return_value.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "management.management.commands.run_instagram_bot.STARTING_FILE",
            os.path.join(temp_dir, "starting.json"),
        ):
            command = Command()
            with patch.object(command, "stdout") as stdout:
                command._ensure()
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "starting.json")))
            stdout.write.assert_called_with("daemon starting — pending")

        log.assert_not_called()

    @patch(
        "management.management.commands.run_instagram_bot._try_process_lock",
        return_value=nullcontext(object()),
    )
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch(
        "management.management.commands.run_instagram_bot._reconcile_commercial_episodes_after_reload",
        side_effect=RuntimeError("reconcile unavailable"),
    )
    def test_starting_marker_survives_reconciliation_failure(
        self, _reconcile, _maintenance, _lock
    ):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "management.management.commands.run_instagram_bot.STARTING_FILE",
            os.path.join(temp_dir, "starting.json"),
        ), patch(
            "management.management.commands.run_instagram_bot._restart_sentinel_mtime",
            return_value=5.0,
        ):
            runner._record_starting_child(os.getpid())

            with self.assertRaisesMessage(RuntimeError, "reconcile unavailable"):
                Command()._forever()

            self.assertTrue(os.path.exists(os.path.join(temp_dir, "starting.json")))

    @patch("management.management.commands.run_instagram_bot._daemon_alive", return_value=False)
    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=False)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_ensure_reports_exited_child_return_code(
        self, _held, _wait, popen, _alive
    ):
        popen.return_value.poll.return_value = 7
        with self.assertRaisesMessage(CommandError, "exited with code 7"):
            Command()._ensure()

    @patch("management.management.commands.run_instagram_bot._daemon_alive", return_value=True)
    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=False)
    @patch(
        "management.management.commands.run_instagram_bot._process_lock_held",
        side_effect=[False, True],
    )
    def test_ensure_accepts_healthy_concurrent_winner(
        self, _held, _wait, popen, _alive
    ):
        popen.return_value.poll.return_value = 0
        command = Command()
        with patch.object(command, "stdout") as stdout:
            command._ensure()
        stdout.write.assert_called_with("daemon alive — ok")

    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=False)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=False)
    def test_ensure_fails_when_stale_daemon_does_not_release_lock(self, _current, _held, _wait):
        with self.assertRaisesMessage(CommandError, "did not release"):
            Command()._ensure()

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen", side_effect=OSError("fork failed"))
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_ensure_fails_when_process_spawn_fails(self, _held, popen):
        with self.assertRaisesMessage(CommandError, "spawn failed"):
            Command()._ensure()
        popen.assert_called_once()

    def test_two_real_ensure_processes_enter_spawn_boundary_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spawn_lock = os.path.join(temp_dir, "spawn.lock")
            daemon_lock = os.path.join(temp_dir, "daemon.lock")
            marker = os.path.join(temp_dir, "spawned.txt")
            child_code = """
import os, sys, time
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'test_settings')
import django
django.setup()
from unittest.mock import patch
from management.management.commands import run_instagram_bot as runner
runner.SPAWN_LOCK_FILE, runner.DAEMON_LOCK_FILE = sys.argv[1], sys.argv[2]
def fake_spawn(*args, **kwargs):
    with open(sys.argv[3], 'a') as marker_file:
        marker_file.write('spawned\\n')
    time.sleep(0.5)
with patch.object(runner.subprocess, 'Popen', side_effect=fake_spawn), patch.object(runner, '_wait_for_lock', return_value=True), patch.object(runner, '_wait_for_daemon_ready', return_value=True), patch.object(runner.bot, 'log'):
    runner.Command()._ensure()
"""
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [PROJECT_ROOT, env.get("PYTHONPATH", "")])
            )
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", child_code, spawn_lock, daemon_lock, marker],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            results = [child.communicate(timeout=10) for child in children]
            self.assertEqual([child.returncode for child in children], [0, 0], results)
            with open(marker) as marker_file:
                self.assertEqual(marker_file.read().splitlines(), ["spawned"])


class _BoundedWorkerEvent:
    def __init__(self, cycles):
        self.cycles = cycles
        self.wait_calls = 0

    def is_set(self):
        return self.wait_calls >= self.cycles

    def wait(self, _seconds):
        self.wait_calls += 1
        return self.is_set()


class AnalysisWorkerTests(SimpleTestCase):
    @patch(
        "management.services.ig_analysis_events.process_due_analysis_events",
        return_value={
            "applied": 0,
            "already_applied": 0,
            "already_rejected": 0,
            "rejected": 0,
            "retry_scheduled": 0,
            "failed": 0,
            "missing": 0,
        },
    )
    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch("management.services.bot_conversation_analysis.process_due_analysis")
    @patch(
        "management.services.bot_conversation_analysis.reconcile_analysis_jobs",
        return_value={"request_graphs_reconciled": 3},
    )
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch(
        "management.management.commands.run_instagram_bot.time.monotonic",
        return_value=100.0,
    )
    def test_analysis_worker_logs_reconciled_graph_count_without_graph_details(
        self,
        _monotonic,
        _maintenance,
        _close,
        reconcile,
        _process,
        log,
        _process_events,
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        reconcile.assert_called_once_with(limit=ANALYSIS_RECONCILE_BATCH)
        log.assert_any_call(
            "info",
            "gemini_request_graphs_reconciled",
            "reconciled=3",
        )

    @patch(
        "management.services.ig_analysis_events.process_due_analysis_events",
        return_value={
            "applied": 0,
            "already_applied": 0,
            "already_rejected": 0,
            "rejected": 2,
            "retry_scheduled": 1,
            "failed": 0,
            "missing": 0,
        },
    )
    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch("management.services.bot_conversation_analysis.process_due_analysis")
    @patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs")
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch("management.management.commands.run_instagram_bot.time.monotonic", return_value=100.0)
    def test_analysis_worker_logs_terminal_event_outcomes(
        self,
        _monotonic,
        _maintenance,
        _close,
        _reconcile,
        _process,
        log,
        _process_events,
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        log.assert_any_call(
            "warning",
            "conversation_analysis_events_terminal",
            "rejected=2 failed=0",
        )

    @patch(
        "management.services.ig_analysis_events.process_due_analysis_events",
        return_value={
            "applied": 0,
            "already_applied": 0,
            "already_rejected": 0,
            "rejected": 0,
            "retry_scheduled": 0,
            "failed": 1,
            "missing": 0,
        },
    )
    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch("management.services.bot_conversation_analysis.process_due_analysis")
    @patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs")
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch("management.management.commands.run_instagram_bot.time.monotonic", return_value=100.0)
    def test_analysis_worker_logs_exhausted_event_as_error(
        self,
        _monotonic,
        _maintenance,
        _close,
        _reconcile,
        _process,
        log,
        _process_events,
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        log.assert_any_call(
            "error",
            "conversation_analysis_events_terminal",
            "rejected=0 failed=1",
        )

    @patch(
        "management.services.ig_analysis_events.process_due_analysis_events",
        return_value={
            "applied": 0,
            "already_applied": 0,
            "already_rejected": 0,
            "rejected": 0,
            "retry_scheduled": 1,
            "failed": 0,
            "missing": 0,
        },
    )
    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch("management.services.bot_conversation_analysis.process_due_analysis")
    @patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs")
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch("management.management.commands.run_instagram_bot.time.monotonic", return_value=100.0)
    def test_analysis_worker_does_not_log_retry_as_terminal(
        self,
        _monotonic,
        _maintenance,
        _close,
        _reconcile,
        _process,
        log,
        _process_events,
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        terminal_calls = [
            item
            for item in log.call_args_list
            if len(item.args) > 1
            and item.args[1] == "conversation_analysis_events_terminal"
        ]
        self.assertEqual(terminal_calls, [])

    @patch("management.services.ig_analysis_events.process_due_analysis_events")
    @patch("management.services.bot_conversation_analysis.process_due_analysis")
    @patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs")
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch("management.management.commands.run_instagram_bot.time.monotonic", return_value=100.0)
    def test_analysis_worker_reconciles_immediately_after_start(
        self, _monotonic, _maintenance, _close, reconcile, process, process_events
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        reconcile.assert_called_once_with(limit=ANALYSIS_RECONCILE_BATCH)
        process.assert_called_once_with(limit=1)
        process_events.assert_called_once_with(limit=1)

    @patch("management.services.ig_analysis_events.process_due_analysis_events")
    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch(
        "management.services.bot_conversation_analysis.process_due_analysis"
    )
    @patch(
        "management.services.bot_conversation_analysis.reconcile_analysis_jobs",
        side_effect=RuntimeError("reconcile unavailable"),
    )
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch("management.management.commands.run_instagram_bot.time.monotonic", return_value=100.0)
    def test_reconciliation_failure_does_not_block_due_job_drain(
        self, _monotonic, _maintenance, _close, reconcile, process, log, process_events
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        reconcile.assert_called_once_with(limit=ANALYSIS_RECONCILE_BATCH)
        process.assert_called_once_with(limit=1)
        process_events.assert_called_once_with(limit=1)
        log.assert_any_call(
            "error",
            "conversation_analysis_reconcile",
            "RuntimeError('reconcile unavailable')",
        )

    @patch("management.services.ig_analysis_events.process_due_analysis_events")
    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch(
        "management.services.bot_conversation_analysis.process_due_analysis",
        side_effect=RuntimeError("analysis unavailable"),
    )
    @patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs")
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch("management.management.commands.run_instagram_bot.time.monotonic", return_value=100.0)
    def test_analysis_failure_does_not_block_owned_event_drain(
        self, _monotonic, _maintenance, _close, reconcile, process, log, process_events
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        reconcile.assert_called_once_with(limit=ANALYSIS_RECONCILE_BATCH)
        process.assert_called_once_with(limit=1)
        process_events.assert_called_once_with(limit=1)
        log.assert_any_call(
            "error",
            "conversation_analysis_due",
            "RuntimeError('analysis unavailable')",
        )

    @patch("management.services.ig_analysis_events.process_due_analysis_events")
    @patch("management.services.bot_conversation_analysis.process_due_analysis")
    @patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs")
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": True},
    )
    def test_analysis_worker_does_nothing_during_maintenance(
        self, _maintenance, _close, reconcile, process, process_events
    ):
        _analysis_worker(_BoundedWorkerEvent(cycles=1))

        reconcile.assert_not_called()
        process.assert_not_called()
        process_events.assert_not_called()

    @patch("management.services.ig_analysis_events.process_due_analysis_events")
    @patch("management.services.bot_conversation_analysis.process_due_analysis")
    @patch("management.services.bot_conversation_analysis.reconcile_analysis_jobs")
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    @patch(
        "management.management.commands.run_instagram_bot.time.monotonic",
        side_effect=[100.0, 101.0, 100.0 + ANALYSIS_RECONCILE_EVERY],
    )
    def test_analysis_worker_reconciles_only_at_bounded_interval(
        self, _monotonic, _maintenance, _close, reconcile, process, process_events
    ):
        event = _BoundedWorkerEvent(cycles=3)
        ticks = (100.0, 101.0, 100.0 + ANALYSIS_RECONCILE_EVERY)
        # The DB circuit reads the same process clock. Advance time per work
        # cycle, not per incidental monotonic() call inside that cycle.
        _monotonic.side_effect = lambda: ticks[min(event.wait_calls, 2)]
        _analysis_worker(event)

        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(process.call_count, 3)
        self.assertEqual(process_events.call_count, 3)


class AiReplyRecoveryWorkerTests(SimpleTestCase):
    @patch("management.services.ig_ai_reply_recovery.process_due_recoveries", return_value=1)
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    def test_recovery_worker_drains_one_due_job_independently(
        self, _maintenance, _close, process_due
    ):
        _ai_reply_recovery_worker(_BoundedWorkerEvent(cycles=1))

        process_due.assert_called_once_with(limit=1)


class PermissionTransitionWorkerTests(SimpleTestCase):
    @patch(
        "management.services.ig_permission_transitions.process_due_permission_transitions",
        return_value=1,
    )
    @patch("management.management.commands.run_instagram_bot.close_old_connections")
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": False},
    )
    def test_permission_worker_drains_one_due_job_independently(
        self, _maintenance, _close, process_due
    ):
        _permission_transition_worker(_BoundedWorkerEvent(cycles=1))

        process_due.assert_called_once_with(limit=1)


class DaemonMaintenanceTests(SimpleTestCase):
    def test_active_lease_blocks_watchdog_spawn(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            activate_maintenance(path=lease_path, duration_seconds=60, actor="test")
            command = Command()
            with (
                patch("management.management.commands.run_instagram_bot.MAINTENANCE_FILE", lease_path),
                patch("management.management.commands.run_instagram_bot.subprocess.Popen") as popen,
                patch.object(command, "stdout") as stdout,
            ):
                command._ensure()
            popen.assert_not_called()
            stdout.write.assert_called_with("maintenance active — watchdog skip")

    def test_stale_lease_does_not_block_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            activate_maintenance(path=lease_path, duration_seconds=1, actor="test", now=100)
            self.assertFalse(maintenance_status(path=lease_path, now=131)["active"])

    def test_malformed_lease_fails_safe_but_expires_from_mtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            with open(lease_path, "w", encoding="utf-8") as lease_file:
                lease_file.write("not-json")
            os.utime(lease_path, (100, 100))
            active = maintenance_status(path=lease_path, now=101, max_seconds=30)
            stale = maintenance_status(path=lease_path, now=131, max_seconds=30)
            self.assertTrue(active["active"])
            self.assertEqual(active["state"], "malformed_active")
            self.assertFalse(stale["active"])
            self.assertEqual(stale["state"], "malformed_stale")

    def test_future_dated_valid_json_is_bounded_by_file_mtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            with open(lease_path, "w", encoding="utf-8") as lease_file:
                json.dump(
                    {
                        "lease_id": "future",
                        "started_at": 4_000_000_000,
                        "expires_at": 4_000_000_060,
                    },
                    lease_file,
                )
            os.utime(lease_path, (100, 100))
            self.assertTrue(
                maintenance_status(path=lease_path, now=101, max_seconds=30)["active"]
            )
            self.assertFalse(
                maintenance_status(path=lease_path, now=131, max_seconds=30)["active"]
            )

    def test_activation_is_atomic_and_deactivation_is_exact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            payload = activate_maintenance(
                path=lease_path,
                duration_seconds=60,
                actor="deploy",
                now=100,
            )
            with open(lease_path, encoding="utf-8") as lease_file:
                stored = json.load(lease_file)
            self.assertEqual(stored, payload)
            self.assertTrue(
                deactivate_maintenance(lease_id=payload["lease_id"], path=lease_path)
            )
            self.assertFalse(os.path.exists(lease_path))

    def test_activation_honors_a_requested_lease_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            payload = activate_maintenance(
                path=lease_path,
                duration_seconds=60,
                actor="deploy",
                requested_lease_id="deploy-request-123",
                now=100,
            )

            self.assertEqual(payload["lease_id"], "deploy-request-123")

    def test_active_owner_cannot_be_shortened_or_released_by_another_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            lock_path = os.path.join(temp_dir, "maintenance.lock")
            send_lock_path = os.path.join(temp_dir, "send.lock")
            payload = activate_maintenance(
                path=lease_path,
                lock_path=lock_path,
                send_lock_path=send_lock_path,
                duration_seconds=300,
                actor="first",
                now=100,
            )
            with self.assertRaises(MaintenanceLeaseConflict):
                activate_maintenance(
                    path=lease_path,
                    lock_path=lock_path,
                    send_lock_path=send_lock_path,
                    duration_seconds=30,
                    actor="second",
                    now=101,
                )
            with self.assertRaises(MaintenanceLeaseConflict):
                deactivate_maintenance(
                    lease_id="wrong-owner",
                    path=lease_path,
                    lock_path=lock_path,
                )
            self.assertEqual(
                maintenance_status(path=lease_path, now=102)["lease_id"],
                payload["lease_id"],
            )

    def test_notification_boundary_refuses_send_during_maintenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            lock_path = os.path.join(temp_dir, "maintenance.lock")
            send_lock_path = os.path.join(temp_dir, "send.lock")
            activate_maintenance(
                path=lease_path,
                lock_path=lock_path,
                send_lock_path=send_lock_path,
                duration_seconds=60,
                actor="test",
            )
            with notification_send_boundary(
                lease_path=lease_path,
                send_lock_path=send_lock_path,
            ) as allowed:
                self.assertFalse(allowed)

    def test_two_processes_cannot_own_same_maintenance_lease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lease_path = os.path.join(temp_dir, "maintenance.json")
            lock_path = os.path.join(temp_dir, "maintenance.lock")
            send_lock_path = os.path.join(temp_dir, "send.lock")
            child_code = """
import sys
from management.services.ig_maintenance import activate_maintenance, MaintenanceLeaseConflict
try:
    activate_maintenance(path=sys.argv[1], lock_path=sys.argv[2], send_lock_path=sys.argv[3], duration_seconds=60)
    print('owned')
except MaintenanceLeaseConflict:
    print('conflict')
"""
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [PROJECT_ROOT, env.get("PYTHONPATH", "")])
            )
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", child_code, lease_path, lock_path, send_lock_path],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            results = [child.communicate(timeout=10) for child in children]
            self.assertEqual([child.returncode for child in children], [0, 0], results)
            self.assertEqual(sorted(stdout.strip() for stdout, _stderr in results), ["conflict", "owned"])


class ReplyBoundaryLockTests(SimpleTestCase):
    @patch("management.services.instagram_bot._clear_client_delivery_error")
    @patch("management.services.instagram_bot._clear_send_error")
    @patch("management.services.instagram_bot._http", return_value=(200, "{}"))
    @patch("management.services.instagram_bot.get_page_token", return_value="token")
    def test_each_meta_chunk_revalidates_permission(
        self, _token, http, _clear_settings, _clear_client
    ):
        decisions = iter((True, False))
        boundary_calls = []

        def boundary_factory():
            boundary_calls.append(True)
            return nullcontext(next(decisions))

        ok, kind, hint = bot.send_text(
            InstagramBotSettings(page_id="page"),
            "recipient",
            "a" * 1200,
            permission_boundary_factory=boundary_factory,
        )

        self.assertFalse(ok)
        self.assertEqual(kind, "unknown")
        self.assertIn("часткова доставка", hint)
        self.assertEqual(len(boundary_calls), 2)
        self.assertEqual(http.call_count, 1)

    def test_two_clients_can_hold_generation_boundaries_concurrently(self):
        first_entered = threading.Event()
        second_entered = threading.Event()

        def capture(settings_id, client_id):
            return ReplyPermission(settings_id, 4, client_id, 2, True)

        def generate(client_id):
            with reply_execution_boundary(1, client_id) as permission:
                self.assertTrue(permission)
                if client_id == 9:
                    first_entered.set()
                    return second_entered.wait(1)
                if not first_entered.wait(1):
                    return False
                second_entered.set()
                return True

        with patch(
            "management.services.ig_reply_boundary.capture_reply_permission",
            side_effect=capture,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(generate, (9, 10)))
        self.assertEqual(results, [True, True])

    @patch(
        "management.services.ig_reply_boundary.capture_reply_permission",
        return_value=ReplyPermission(1, 4, 9, 2, True),
    )
    def test_generation_boundary_does_not_hold_permission_lock(self, _capture):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = os.path.join(temp_dir, "reply.lock")
            with reply_execution_boundary(1, 9, lock_path=lock_path) as permission:
                started = time.monotonic()
                with pause_reply_boundary(lock_path=lock_path):
                    pass
                self.assertLess(time.monotonic() - started, 0.25)
                self.assertTrue(permission)

    @patch(
        "management.services.ig_reply_boundary.capture_reply_permission",
        return_value=ReplyPermission(1, 5, 9, 2, True),
    )
    def test_send_boundary_aborts_when_global_epoch_changed(self, _capture):
        stale = ReplyPermission(1, 4, 9, 2, True)
        with tempfile.TemporaryDirectory() as temp_dir:
            with customer_send_boundary(
                1,
                9,
                stale,
                lock_path=os.path.join(temp_dir, "reply.lock"),
            ) as allowed:
                self.assertFalse(allowed)

    @patch(
        "management.services.ig_reply_boundary.capture_reply_permission",
        return_value=ReplyPermission(1, 4, 9, 3, True),
    )
    def test_send_boundary_aborts_when_client_epoch_changed(self, _capture):
        stale = ReplyPermission(1, 4, 9, 2, True)
        with tempfile.TemporaryDirectory() as temp_dir:
            with customer_send_boundary(
                1,
                9,
                stale,
                lock_path=os.path.join(temp_dir, "reply.lock"),
            ) as allowed:
                self.assertFalse(allowed)

    @patch(
        "management.services.ig_reply_boundary.capture_reply_permission",
        return_value=ReplyPermission(1, 4, 9, 2, True),
    )
    def test_send_boundary_accepts_matching_epochs(self, _capture):
        permission = ReplyPermission(1, 4, 9, 2, True)
        with tempfile.TemporaryDirectory() as temp_dir:
            with customer_send_boundary(
                1,
                9,
                permission,
                lock_path=os.path.join(temp_dir, "reply.lock"),
            ) as allowed:
                self.assertTrue(allowed)

    def test_pause_waits_for_real_inflight_process_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = os.path.join(temp_dir, "reply.lock")
            child_code = """
import sys, time
from management.services.ig_reply_boundary import pause_reply_boundary
with pause_reply_boundary(lock_path=sys.argv[1]):
    print('entered', flush=True)
    time.sleep(1.5)
"""
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [PROJECT_ROOT, env.get("PYTHONPATH", "")])
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, lock_path],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "entered")
                started = time.monotonic()
                from management.services.ig_reply_boundary import pause_reply_boundary

                with pause_reply_boundary(lock_path=lock_path):
                    waited = time.monotonic() - started
                self.assertGreaterEqual(waited, 1.0)
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_web_transition_times_out_while_another_process_holds_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = os.path.join(temp_dir, "reply.lock")
            child_code = """
import sys, time
from management.services.ig_reply_boundary import pause_reply_boundary
with pause_reply_boundary(lock_path=sys.argv[1]):
    print('entered', flush=True)
    time.sleep(2)
"""
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [PROJECT_ROOT, env.get("PYTHONPATH", "")])
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, lock_path],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "entered")
                started = time.monotonic()
                with self.assertRaises(ReplyBoundaryTimeout):
                    with pause_reply_boundary(
                        lock_path=lock_path,
                        timeout_seconds=0.1,
                    ):
                        pass
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                child.terminate()
                child.wait(timeout=5)


class ReplyPermissionEpochModelTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.save(update_fields=["is_enabled"])
        self.client = IgClient.objects.create(igsid="epoch-client")

    def test_global_stop_invalidates_captured_permission(self):
        from management.services.ig_reply_boundary import capture_reply_permission

        before = capture_reply_permission(self.settings.pk, self.client.pk)
        bot.stop_bot()
        after = capture_reply_permission(self.settings.pk, self.client.pk)
        self.assertTrue(before)
        self.assertFalse(after)
        self.assertNotEqual(before.settings_epoch, after.settings_epoch)

    def test_client_pause_epoch_invalidates_only_that_client(self):
        from management.services.ig_reply_boundary import capture_reply_permission

        other = IgClient.objects.create(igsid="epoch-other")
        before = capture_reply_permission(self.settings.pk, self.client.pk)
        other_before = capture_reply_permission(self.settings.pk, other.pk)
        self.client.bot_paused = True
        self.client.reply_permission_epoch += 1
        self.client.save(update_fields=["bot_paused", "reply_permission_epoch", "updated_at"])
        after = capture_reply_permission(self.settings.pk, self.client.pk)
        other_after = capture_reply_permission(self.settings.pk, other.pk)
        self.assertFalse(after)
        self.assertTrue(other_after)
        self.assertNotEqual(before.client_epoch, after.client_epoch)
        self.assertEqual(other_before.client_epoch, other_after.client_epoch)


class DaemonMaintenanceDrainTests(SimpleTestCase):
    def test_pending_drain_checks_maintenance_before_claiming_next_row(self):
        settings = InstagramBotSettings(is_enabled=True)
        first_row = object()
        with (
            patch.object(bot, "_send_rate_limit_backoff_active", return_value=False),
            patch.object(bot, "_gemini_backoff_active", return_value=False),
            patch.object(bot, "reclaim_stale_processing"),
            patch("management.services.ig_revision_live.process_revision_finalizations", return_value=0),
            patch("management.services.ig_revision_live.process_pending_revisions", return_value=0),
            patch("management.services.ig_revision_live.revision_execution_enabled", return_value=False),
            patch.object(bot, "maintenance_status", side_effect=[{"active": False}, {"active": True}]),
            patch.object(bot, "_claim_next", return_value=first_row) as claim_next,
            patch.object(bot, "_process_one", return_value=True) as process_one,
        ):
            handled = bot.process_pending(settings, max_items=2)

        self.assertEqual(handled, 1)
        claim_next.assert_called_once_with()
        process_one.assert_called_once_with(settings, first_row)


class DaemonHeartbeatTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        # This class isolates heartbeat/service scheduling. Receipt/inbox
        # ownership has separate database-backed tests and must not do I/O here.
        for target in (
            "management.management.commands.run_instagram_bot._revision_receipt_tick",
            "management.services.ig_webhook_inbox.drain_webhook_inbox",
        ):
            mocked = patch(target)
            mocked.start()
            self.addCleanup(mocked.stop)

    @patch("management.management.commands.run_instagram_bot.cache.get", return_value={"at": 100.0})
    @patch("management.management.commands.run_instagram_bot.time.time", return_value=110.0)
    def test_dict_heartbeat_is_supported(self, _time, _get):
        self.assertTrue(_daemon_alive())

    def test_process_pulse_and_main_progress_are_independent_contracts(self):
        cache.delete(HB_KEY)
        cache.delete(MAIN_PROGRESS_KEY)
        self.addCleanup(cache.delete, HB_KEY)
        self.addCleanup(cache.delete, MAIN_PROGRESS_KEY)

        _publish_main_progress(
            owner="owner",
            start_sentinel=1.0,
            cycle=7,
            state="running",
        )
        main_before = cache.get(MAIN_PROGRESS_KEY)
        _publish_process_pulse(
            owner="owner",
            start_sentinel=1.0,
            state="running",
        )

        self.assertEqual(cache.get(MAIN_PROGRESS_KEY), main_before)
        self.assertEqual(cache.get(HB_KEY)["kind"], "process_pulse")
        self.assertEqual(main_before["kind"], "main_progress")
        self.assertEqual(main_before["cycle"], 7)

    def test_signal_hook_requests_clean_stop_without_raising(self):
        stop_event = threading.Event()
        with _daemon_runtime_hooks(stop_event) as outcome:
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(stop_event.wait(1))
        self.assertEqual(outcome["signal"], signal.SIGTERM)

    @patch("management.management.commands.run_instagram_bot.bot_followups.process_due_followups")
    @patch("management.management.commands.run_instagram_bot.bot.process_pending")
    @patch("management.management.commands.run_instagram_bot.bot.drain_manager_notifications")
    def test_disabled_reply_gate_still_drains_operational_outbox(self, drain, pending, followups):
        settings = InstagramBotSettings(is_enabled=False, receive_via_poll=False)

        enabled, last_poll = _run_work_cycle(settings, 17.0)

        self.assertFalse(enabled)
        self.assertEqual(last_poll, 17.0)
        drain.assert_called_once_with(limit=10)
        pending.assert_not_called()
        followups.assert_not_called()

    @patch("management.management.commands.run_instagram_bot.bot.refresh_profiles_batch")
    @patch("management.management.commands.run_instagram_bot.cache.add", return_value=True)
    @patch("management.management.commands.run_instagram_bot.bot_followups.process_due_followups")
    @patch("management.management.commands.run_instagram_bot.bot.process_pending")
    @patch("management.management.commands.run_instagram_bot.bot.drain_manager_notifications")
    def test_profile_sync_is_independent_from_reply_enabled_gate(
        self, _drain, _pending, _followups, cache_add, refresh_profiles
    ):
        settings = InstagramBotSettings(is_enabled=False, receive_via_poll=False, page_id="page")

        _run_work_cycle(settings, 17.0)

        cache_add.assert_called_once()
        refresh_profiles.assert_called_once_with(settings)

    @patch("management.management.commands.run_instagram_bot.bot.refresh_profiles_batch")
    @patch("management.management.commands.run_instagram_bot.cache.add", return_value=False)
    @patch("management.management.commands.run_instagram_bot.bot_followups.process_due_followups")
    @patch("management.management.commands.run_instagram_bot.bot.process_pending")
    @patch("management.management.commands.run_instagram_bot.bot.drain_manager_notifications")
    def test_enabled_daemon_leaves_periodic_lanes_to_coordinator(
        self,
        _drain,
        _pending,
        _followups,
        _cache_add,
        refresh_profiles,
    ):
        settings = InstagramBotSettings(
            pk=1,
            is_enabled=True,
            receive_via_poll=False,
        )

        with (
            patch(
                "management.services.bot_payments.poll_pending_deals_locked"
            ) as payment_poll,
            patch(
                "management.services.ig_order_fulfillment.reconcile_order_customer_events"
            ) as fulfillment,
        ):
            _run_work_cycle(settings, 17.0)

        payment_poll.assert_not_called()
        fulfillment.assert_not_called()
        refresh_profiles.assert_not_called()

    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": True},
    )
    @patch("management.management.commands.run_instagram_bot.cache.add", return_value=False)
    @patch("management.management.commands.run_instagram_bot.bot_followups.process_due_followups")
    @patch("management.management.commands.run_instagram_bot.bot.process_pending")
    @patch("management.management.commands.run_instagram_bot.bot.drain_manager_notifications")
    def test_cycle_stops_after_inflight_reply_observes_maintenance(
        self,
        _drain,
        process_pending,
        followups,
        _cache_add,
        _maintenance,
    ):
        settings = InstagramBotSettings(is_enabled=True, receive_via_poll=False)

        enabled, last_poll = _run_work_cycle(settings, 17.0)

        self.assertTrue(enabled)
        self.assertEqual(last_poll, 17.0)
        process_pending.assert_called_once_with(settings)
        followups.assert_not_called()

    @patch("management.management.commands.run_instagram_bot.time.time", return_value=100.0)
    @patch("management.management.commands.run_instagram_bot.bot.poll_ingest")
    @patch("management.management.commands.run_instagram_bot.bot_followups.process_due_followups")
    @patch("management.management.commands.run_instagram_bot.bot.process_pending")
    @patch("management.management.commands.run_instagram_bot.bot.drain_manager_notifications")
    def test_disabled_reply_gate_still_polls_for_observation(
        self, _drain, pending, followups, poll_ingest, _time
    ):
        settings = InstagramBotSettings(is_enabled=False, receive_via_poll=True)

        enabled, last_poll = _run_work_cycle(settings, 0.0)

        self.assertFalse(enabled)
        self.assertEqual(last_poll, 100.0)
        poll_ingest.assert_called_once_with(settings)
        pending.assert_not_called()
        followups.assert_not_called()

    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch("management.management.commands.run_instagram_bot.bot_followups.process_due_followups")
    @patch("management.management.commands.run_instagram_bot.bot.process_pending")
    @patch(
        "management.management.commands.run_instagram_bot.bot.drain_manager_notifications",
        side_effect=RuntimeError("outbox unavailable"),
    )
    def test_outbox_failure_does_not_block_customer_work(self, drain, pending, followups, log):
        settings = InstagramBotSettings(is_enabled=True, receive_via_poll=False)

        enabled, last_poll = _run_work_cycle(settings, 23.0)

        self.assertTrue(enabled)
        self.assertEqual(last_poll, 23.0)
        drain.assert_called_once_with(limit=10)
        pending.assert_called_once_with(settings)
        followups.assert_called_once_with(settings)
        log.assert_called_once()

    @patch("management.management.commands.run_instagram_bot._run_work_cycle")
    @patch("management.management.commands.run_instagram_bot._conv_refresher")
    @patch("management.management.commands.run_instagram_bot.bot.log")
    @patch(
        "management.management.commands.run_instagram_bot._reconcile_commercial_episodes_after_reload"
    )
    @patch(
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": True},
    )
    def test_running_daemon_exits_before_work_when_maintenance_appears(
        self, _maintenance, _reconcile, _log, _refresher, work_cycle
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "management.management.commands.run_instagram_bot.PID_FILE",
                os.path.join(temp_dir, "daemon.pid"),
            ):
                Command()._forever_locked()
        _reconcile.assert_not_called()
        work_cycle.assert_not_called()



class DaemonStatusTests(TestCase):
    def tearDown(self):
        cache.delete(HB_KEY)
        cache.delete(MAIN_PROGRESS_KEY)
        cache.delete(DAEMON_LOCK_KEY)
        super().tearDown()

    def test_recent_database_heartbeat_without_daemon_pulse_is_recovering(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.heartbeat_at = timezone.now() - timedelta(seconds=10)
        settings.save(update_fields=["is_enabled", "heartbeat_at"])
        cache.delete(HB_KEY)

        snapshot = bot.status_snapshot()

        self.assertFalse(snapshot["daemon_online"])
        self.assertTrue(snapshot["db_heartbeat_fresh"])
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["state"], "worker_recovering")
        self.assertTrue(snapshot["recovery_expected"])

    def test_database_heartbeat_without_daemon_after_grace_is_worker_error(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.heartbeat_at = timezone.now() - timedelta(seconds=45)
        settings.save(update_fields=["is_enabled", "heartbeat_at"])
        cache.delete(HB_KEY)

        snapshot = bot.status_snapshot()

        self.assertFalse(snapshot["daemon_online"])
        self.assertTrue(snapshot["db_heartbeat_fresh"])
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["state"], "worker_error")
        self.assertTrue(snapshot["recovery_expected"])

    @patch("management.services.ig_daemon_health.cache.get", return_value={"at": 100.0, "state": "idle"})
    @patch("management.services.ig_daemon_health.time.time", return_value=110.0)
    def test_status_snapshot_accepts_structured_daemon_heartbeat(self, _time, _get):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.heartbeat_at = timezone.now()
        settings.save(update_fields=["is_enabled", "heartbeat_at"])

        with patch.dict(os.environ, {"IG_APP_SECRET": "test-secret"}, clear=True):
            snapshot = bot.status_snapshot()

        self.assertTrue(snapshot["daemon_online"])
        self.assertEqual(snapshot["state"], "running")
        self.assertTrue(snapshot["main_progress_healthy"])

    def test_fresh_process_pulse_does_not_hide_stale_main_progress(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.heartbeat_at = timezone.now()
        settings.save(update_fields=["is_enabled", "heartbeat_at"])
        now = time.time()
        cache.set(HB_KEY, {"at": now, "kind": "process_pulse"}, 600)
        cache.set(
            MAIN_PROGRESS_KEY,
            {"at": now - 300, "kind": "main_progress", "state": "running"},
            600,
        )

        with patch.dict(os.environ, {"IG_APP_SECRET": "test-secret"}, clear=True):
            snapshot = bot.status_snapshot()

        self.assertTrue(snapshot["daemon_online"])
        self.assertTrue(snapshot["main_progress_stale"])
        self.assertEqual(snapshot["main_progress_state"], "running")
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["state"], "worker_stalled")
        self.assertTrue(snapshot["operator_attention_required"])

    def test_fresh_process_pulse_without_main_progress_is_not_running(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.heartbeat_at = timezone.now()
        settings.save(update_fields=["is_enabled", "heartbeat_at"])
        cache.set(HB_KEY, {"at": time.time(), "kind": "process_pulse"}, 600)
        cache.delete(MAIN_PROGRESS_KEY)

        with patch.dict(os.environ, {"IG_APP_SECRET": "test-secret"}, clear=True):
            snapshot = bot.status_snapshot()

        self.assertTrue(snapshot["daemon_online"])
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["state"], "worker_stalled")
        self.assertEqual(
            snapshot["main_progress_stalled_reason"],
            "main_progress_missing",
        )

    @patch("management.services.ig_daemon_health.cache.get", return_value={"at": 100.0, "state": "idle"})
    @patch("management.services.ig_daemon_health.time.time", return_value=110.0)
    def test_live_daemon_without_a_working_ingress_is_reported_as_degraded(self, _time, _get):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.receive_via_poll = False
        settings.heartbeat_at = timezone.now()
        settings.save(update_fields=["is_enabled", "receive_via_poll", "heartbeat_at"])

        with patch.dict(os.environ, {}, clear=True):
            snapshot = bot.status_snapshot()

        self.assertTrue(snapshot["daemon_online"])
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["state"], "ingress_degraded")
        self.assertFalse(snapshot["ingress"]["healthy"])
        self.assertEqual(snapshot["ingress"]["state"], "unavailable")
        self.assertEqual(snapshot["ingress"]["webhook"]["state"], "missing_secret")
        self.assertEqual(snapshot["ingress"]["polling"]["state"], "disabled")

    @patch("management.management.commands.run_instagram_bot.time.time", return_value=100.0)
    @patch("management.management.commands.run_instagram_bot.bot.poll_ingest", return_value={"ok": True})
    @patch("management.management.commands.run_instagram_bot.bot_followups.process_due_followups")
    @patch("management.management.commands.run_instagram_bot.bot.process_pending")
    @patch("management.management.commands.run_instagram_bot.bot.drain_manager_notifications")
    def test_poll_cycle_persists_last_poll_telemetry(
        self, _drain, _pending, _followups, _poll_ingest, _time
    ):
        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.receive_via_poll = True
        settings.last_poll_at = None
        settings.save(update_fields=["is_enabled", "receive_via_poll", "last_poll_at", "updated_at"])

        _run_work_cycle(settings, 0.0)

        settings.refresh_from_db()
        self.assertIsNotNone(settings.last_poll_at)

    @patch("management.services.ig_daemon_health.cache.get", return_value={"at": 100.0, "state": "idle"})
    @patch("management.services.ig_daemon_health.time.time", return_value=110.0)
    @patch("management.services.instagram_bot.resolve_direct_token", return_value="page-token")
    def test_poll_provider_error_is_exposed_as_degraded_ingress(
        self, _token, _time, _get
    ):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.receive_via_poll = True
        settings.heartbeat_at = timezone.now()
        settings.last_poll_at = timezone.now()
        settings.last_error = "polling:provider_unavailable"
        settings.save(update_fields=[
            "is_enabled",
            "receive_via_poll",
            "heartbeat_at",
            "last_poll_at",
            "last_error",
            "updated_at",
        ])

        with patch.dict(os.environ, {}, clear=True):
            snapshot = bot.status_snapshot()

        self.assertEqual(snapshot["state"], "ingress_degraded")
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["ingress"]["polling"]["state"], "degraded")

    @patch("management.services.instagram_bot.resolve_direct_token", return_value="page-token")
    def test_fresh_poll_timestamp_cannot_hide_independent_ingress_degradation(self, _token):
        settings = InstagramBotSettings.load()
        settings.page_id = "page"
        settings.is_enabled = True
        settings.receive_via_poll = True
        settings.heartbeat_at = timezone.now()
        settings.last_poll_at = timezone.now()
        settings.last_error = ""
        settings.save(update_fields=[
            "page_id",
            "is_enabled",
            "receive_via_poll",
            "heartbeat_at",
            "last_poll_at",
            "last_error",
            "updated_at",
        ])
        cache.set(HB_KEY, {"at": time.time()}, 60)
        cache.set(MAIN_PROGRESS_KEY, {"at": time.time(), "state": "idle"}, 60)
        signals = (
            ("ig_bot_ingress_refresh_degraded:page", "conversation_refresh_failed"),
            ("ig_bot_ingress_poll_degraded:page", "message_poll_failed"),
        )
        for cache_key, signal_state in signals:
            with self.subTest(signal_state=signal_state):
                cache.set(
                    cache_key,
                    {"state": signal_state, "reason": "provider_unavailable", "at": time.time()},
                    600,
                )
                self.addCleanup(cache.delete, cache_key)

                with patch.dict(os.environ, {}, clear=True):
                    snapshot = bot.status_snapshot()

                self.assertEqual(snapshot["state"], "ingress_degraded")
                self.assertFalse(snapshot["running"])
                self.assertFalse(snapshot["ingress"]["polling"]["healthy"])
                self.assertEqual(
                    snapshot["ingress"]["polling"]["degradation"]["state"],
                    signal_state,
                )
                cache.delete(cache_key)

    @patch("management.services.instagram_bot.resolve_direct_token", return_value="page-token")
    def test_malformed_degradation_timestamp_does_not_break_ingress_status(self, _token):
        settings = InstagramBotSettings.load()
        settings.page_id = "page"
        settings.receive_via_poll = True
        settings.last_poll_at = timezone.now()
        settings.save(update_fields=["page_id", "receive_via_poll", "last_poll_at", "updated_at"])
        cache_key = "ig_bot_ingress_refresh_degraded:page"
        cache.set(
            cache_key,
            {
                "state": "conversation_refresh_failed",
                "reason": "provider_unavailable",
                "at": "not-a-timestamp",
            },
            600,
        )
        self.addCleanup(cache.delete, cache_key)

        status = bot.ingress_status(settings)

        self.assertFalse(status["polling"]["healthy"])
        self.assertEqual(status["polling"]["state"], "degraded")

    def test_disabled_bot_is_not_reported_as_recovery_required(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.heartbeat_at = None
        settings.save(update_fields=["is_enabled", "heartbeat_at"])
        cache.delete(HB_KEY)

        snapshot = bot.status_snapshot()

        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["state"], "disabled")
        self.assertFalse(snapshot["recovery_expected"])


class CanonicalReceiptIngressIndependenceTests(SimpleTestCase):
    @override_settings(IG_WEBHOOK_INBOX_ENABLED=False)
    def test_paused_poll_only_still_runs_receipt_tick(self):
        settings_obj = InstagramBotSettings(is_enabled=False, receive_via_poll=True)
        with (
            patch.object(runner, "require_database_ready"),
            patch.object(runner, "_revision_receipt_tick") as receipts,
            patch.object(runner, "_service_task_isolation_enabled", return_value=False),
            patch.object(runner, "_run_legacy_work_cycle", return_value=(False, 12.0)),
        ):
            self.assertEqual(runner._run_work_cycle(settings_obj, 12.0), (False, 12.0))
        receipts.assert_called_once_with(settings_obj)

    def test_gemini_backoff_does_not_block_canonical_work(self):
        settings_obj = InstagramBotSettings(is_enabled=True)
        with (
            patch.object(bot, "_maybe_purge_expired_private_media"),
            patch.object(bot, "_send_rate_limit_backoff_active", return_value=False),
            patch.object(bot, "_gemini_backoff_active", return_value=True),
            patch("management.services.ig_revision_live.process_revision_finalizations", return_value=0),
            patch("management.services.ig_revision_live.process_pending_revisions", return_value=1) as canonical,
            patch("management.services.ig_revision_live.revision_execution_enabled", return_value=True),
            patch.object(bot, "_claim_next") as legacy,
        ):
            self.assertEqual(bot.process_pending(settings_obj, max_items=1), 1)
        canonical.assert_called_once_with(settings_obj, max_items=1)
        legacy.assert_not_called()
