"""Regression tests for the Instagram bot daemon/watchdog boundary."""

import os
import subprocess
import sys
import tempfile
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
