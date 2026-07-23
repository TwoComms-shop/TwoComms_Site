"""Regression tests for the Instagram bot daemon/watchdog boundary."""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from management.management.commands.run_instagram_bot import (
    DAEMON_LOCK_KEY,
    HB_KEY,
    MANAGE_PY_PATH,
    PROJECT_ROOT,
    Command,
    _daemon_alive,
    _process_lock_held,
    _run_work_cycle,
)
from management.models import InstagramBotSettings
from management.services import instagram_bot as bot
from management.services.ig_maintenance import (
    MaintenanceLeaseConflict,
    activate_maintenance,
    deactivate_maintenance,
    maintenance_status,
    notification_send_boundary,
)


class DaemonPathTests(SimpleTestCase):
    def test_watchdog_uses_absolute_project_manage_path(self):
        self.assertTrue(os.path.isabs(MANAGE_PY_PATH))
        self.assertTrue(MANAGE_PY_PATH.endswith(os.path.join("twocomms", "manage.py")))
        self.assertEqual(PROJECT_ROOT, os.path.dirname(MANAGE_PY_PATH))

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=True)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=False)
    def test_ensure_spawns_from_project_root_with_absolute_manage_path(self, _held, _wait, popen):
        command = Command()

        with patch.object(command, "stdout") as stdout:
            command._ensure()

        args, kwargs = popen.call_args
        self.assertEqual(args[0][:3], [os.sys.executable, MANAGE_PY_PATH, "run_instagram_bot"])
        self.assertEqual(kwargs["cwd"], PROJECT_ROOT)
        self.assertTrue(os.path.isabs(args[0][1]))
        stdout.write.assert_called()

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._wait_for_lock", return_value=True)
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=False)
    def test_ensure_replaces_old_worker_after_restart_sentinel(
        self, _current, _held, _wait, popen
    ):
        command = Command()
        with patch.object(command, "stdout") as stdout:
            command._ensure()
        popen.assert_called_once()
        stdout.write.assert_called()

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._process_lock_held", return_value=True)
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=True)
    def test_ensure_does_not_spawn_over_current_process_lock(self, _current, _held, popen):
        command = Command()
        with patch.object(command, "stdout") as stdout:
            command._ensure()
        popen.assert_not_called()
        stdout.write.assert_called_with("daemon alive — ok")

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
    def test_ensure_fails_when_child_never_acquires_daemon_lock(self, _held, _wait, popen):
        with self.assertRaisesMessage(CommandError, "exited before acquiring"):
            Command()._ensure()
        popen.assert_called_once()

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
with patch.object(runner.subprocess, 'Popen', side_effect=fake_spawn), patch.object(runner, '_wait_for_lock', return_value=True), patch.object(runner.bot, 'log'):
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


class DaemonHeartbeatTests(SimpleTestCase):
    @patch("management.management.commands.run_instagram_bot.cache.get", return_value={"at": 100.0})
    @patch("management.management.commands.run_instagram_bot.time.time", return_value=110.0)
    def test_dict_heartbeat_is_supported(self, _time, _get):
        self.assertTrue(_daemon_alive())

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
        "management.management.commands.run_instagram_bot.maintenance_status",
        return_value={"active": True},
    )
    def test_running_daemon_exits_before_work_when_maintenance_appears(
        self, _maintenance, _log, _refresher, work_cycle
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "management.management.commands.run_instagram_bot.PID_FILE",
                os.path.join(temp_dir, "daemon.pid"),
            ):
                Command()._forever_locked()
        work_cycle.assert_not_called()


class DaemonStatusTests(TestCase):
    def tearDown(self):
        cache.delete(HB_KEY)
        cache.delete(DAEMON_LOCK_KEY)
        super().tearDown()

    def test_fresh_database_heartbeat_without_daemon_heartbeat_is_not_running(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.heartbeat_at = timezone.now() - timedelta(seconds=10)
        settings.save(update_fields=["is_enabled", "heartbeat_at"])
        cache.delete(HB_KEY)

        snapshot = bot.status_snapshot()

        self.assertFalse(snapshot["daemon_online"])
        self.assertTrue(snapshot["db_heartbeat_fresh"])
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["state"], "worker_error")

    @patch("management.services.instagram_bot.cache.get", return_value={"at": 100.0})
    @patch("management.services.instagram_bot.time.time", return_value=110.0)
    def test_status_snapshot_accepts_structured_daemon_heartbeat(self, _time, _get):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.heartbeat_at = timezone.now()
        settings.save(update_fields=["is_enabled", "heartbeat_at"])

        snapshot = bot.status_snapshot()

        self.assertTrue(snapshot["daemon_online"])
        self.assertEqual(snapshot["state"], "running")

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
