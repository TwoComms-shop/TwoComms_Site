"""Regression tests for the Instagram bot daemon/watchdog boundary."""

import os
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from management.management.commands.run_instagram_bot import (
    DAEMON_LOCK_KEY,
    HB_KEY,
    MANAGE_PY_PATH,
    PROJECT_ROOT,
    Command,
    _daemon_alive,
)
from management.models import InstagramBotSettings
from management.services import instagram_bot as bot


class DaemonPathTests(SimpleTestCase):
    def test_watchdog_uses_absolute_project_manage_path(self):
        self.assertTrue(os.path.isabs(MANAGE_PY_PATH))
        self.assertTrue(MANAGE_PY_PATH.endswith(os.path.join("twocomms", "manage.py")))
        self.assertEqual(PROJECT_ROOT, os.path.dirname(MANAGE_PY_PATH))

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._daemon_alive", return_value=False)
    @patch("management.management.commands.run_instagram_bot.cache.add", return_value=True)
    def test_ensure_spawns_from_project_root_with_absolute_manage_path(self, _add, _alive, popen):
        command = Command()

        with patch.object(command, "stdout") as stdout:
            command._ensure()

        args, kwargs = popen.call_args
        self.assertEqual(args[0][:3], [os.sys.executable, MANAGE_PY_PATH, "run_instagram_bot"])
        self.assertEqual(kwargs["cwd"], PROJECT_ROOT)
        self.assertTrue(os.path.isabs(args[0][1]))
        stdout.write.assert_called()

    @patch("management.management.commands.run_instagram_bot.subprocess.Popen")
    @patch("management.management.commands.run_instagram_bot._daemon_code_current", return_value=False)
    @patch("management.management.commands.run_instagram_bot._daemon_alive", return_value=True)
    @patch("management.management.commands.run_instagram_bot.cache.add", return_value=True)
    def test_ensure_replaces_old_worker_after_restart_sentinel(
        self, _add, _alive, _current, popen
    ):
        command = Command()
        with patch.object(command, "stdout") as stdout:
            command._ensure()
        popen.assert_called_once()
        stdout.write.assert_called()


class DaemonHeartbeatTests(SimpleTestCase):
    @patch("management.management.commands.run_instagram_bot.cache.get", return_value={"at": 100.0})
    @patch("management.management.commands.run_instagram_bot.time.time", return_value=110.0)
    def test_dict_heartbeat_is_supported(self, _time, _get):
        self.assertTrue(_daemon_alive())


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
