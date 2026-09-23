import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_instagram_periodic_jobs_cron.sh"
BEGIN_MARKER = "# BEGIN TWOCOMMS INSTAGRAM PERIODIC JOBS v1"
END_MARKER = "# END TWOCOMMS INSTAGRAM PERIODIC JOBS v1"


class InstallInstagramPeriodicJobsCronTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.crontab_file = self.root / "crontab"
        self.django_root = self.root / "TwoComms_Site" / "twocomms"
        self.django_root.mkdir(parents=True)
        self.python = self.root / "python"
        self.python.write_text("", encoding="utf-8")
        self.python.chmod(0o700)
        self._write_executable(
            "crontab",
            """#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "-l" ]; then
  if [ -f "$FAKE_CRONTAB_FILE" ]; then cat "$FAKE_CRONTAB_FILE"; exit 0; fi
  echo 'no crontab for test' >&2; exit 1
fi
cp "$1" "$FAKE_CRONTAB_FILE"
""",
        )
        self._write_executable("flock", "#!/usr/bin/env bash\nexit 0\n")
        self._write_executable("timeout", "#!/usr/bin/env bash\nexit 0\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _env(self):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "FAKE_CRONTAB_FILE": str(self.crontab_file),
                "TWC_DJANGO_ROOT": str(self.django_root),
                "TWC_PYTHON": str(self.python),
                "TWC_FLOCK_BIN": str(self.fake_bin / "flock"),
                "TWC_TIMEOUT_BIN": str(self.fake_bin / "timeout"),
            }
        )
        return env

    def _run(self, mode, extra_env=None):
        env = self._env()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(INSTALL_SCRIPT), mode],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def _legacy_lines(self):
        root = self.django_root
        python = self.python
        return (
            f"*/2 * * * * cd {root} && /usr/bin/flock -n {root}/tmp/order.lock {python} manage.py reconcile_order_telegram_notifications --max-age-hours 168 --min-age-seconds 60 --limit 50 >> {root}/logs/order.log 2>&1",
            f"*/2 * * * * cd {root} && /usr/bin/flock -n {root}/tmp/checkout.lock {python} manage.py reconcile_ig_checkout --limit 100 >> {root}/logs/checkout.log 2>&1",
            f"*/2 * * * * cd {root} && /usr/bin/flock -n {root}/tmp/fulfillment.lock {python} manage.py reconcile_ig_order_fulfillment --limit 100 >> {root}/logs/fulfillment.log 2>&1",
            f"*/4 * * * * cd {root} && /usr/bin/flock -n {root}/tmp/payments.lock {python} manage.py poll_ig_deal_payments --limit 50 >> {root}/logs/payments.log 2>&1",
            f"*/5 * * * * cd {root} && /usr/bin/flock -n {root}/tmp/call.lock {python} manage.py run_call_ai_analyses --limit 1 >> {root}/logs/call.log 2>&1",
            f"0 * * * * cd {root} && /usr/bin/flock -n {root}/tmp/gemini.lock {python} manage.py check_ig_gemini_metadata_health >> {root}/logs/gemini.log 2>&1",
        )

    def test_install_collapses_legacy_fanout_and_removes_metadata_schedule(self):
        self.crontab_file.write_text(
            "17 4 * * * /opt/unrelated\n" + "\n".join(self._legacy_lines()) + "\n",
            encoding="utf-8",
        )

        first = self._run("--install")
        installed = self.crontab_file.read_bytes()
        second = self._run("--install")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.crontab_file.read_bytes(), installed)
        content = installed.decode()
        self.assertEqual(content.count(BEGIN_MARKER), 1)
        self.assertEqual(content.count("manage.py run_instagram_periodic_jobs"), 1)
        self.assertNotIn("manage.py check_ig_gemini_metadata_health", content)
        for legacy_command in (
            "reconcile_order_telegram_notifications",
            "reconcile_ig_checkout",
            "reconcile_ig_order_fulfillment",
            "poll_ig_deal_payments",
            "run_call_ai_analyses",
        ):
            self.assertNotIn(f"manage.py {legacy_command}", content)
        self.assertIn("tmp/twocomms_heavy_background.lock", content)
        self.assertIn("flock -w 50 -E 75", content)
        self.assertIn("--kill-after=15s 600s", content)
        self.assertIn("17 4 * * * /opt/unrelated", content)

    def test_install_migrates_known_legacy_fanout_inside_managed_block(self):
        self.crontab_file.write_text(
            f"{BEGIN_MARKER}\n"
            + "\n".join(self._legacy_lines())
            + f"\n{END_MARKER}\n",
            encoding="utf-8",
        )

        result = self._run("--install")

        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.crontab_file.read_text(encoding="utf-8")
        self.assertEqual(content.count("manage.py run_instagram_periodic_jobs"), 1)
        self.assertNotIn("manage.py check_ig_gemini_metadata_health", content)

    def test_install_upgrades_nonblocking_coordinator_to_bounded_wait(self):
        self.assertEqual(self._run("--install").returncode, 0)
        content = self.crontab_file.read_text(encoding="utf-8")
        self.crontab_file.write_text(
            content.replace("flock -w 50 -E 75", "flock -n -E 75"),
            encoding="utf-8",
        )

        result = self._run("--install")

        self.assertEqual(result.returncode, 0, result.stderr)
        upgraded = self.crontab_file.read_text(encoding="utf-8")
        self.assertIn("flock -w 50 -E 75", upgraded)
        self.assertNotIn(
            "flock -n -E 75 "
            + str(self.django_root / "tmp/twocomms_heavy_background.lock"),
            upgraded,
        )

    def test_check_detects_content_drift_without_writing(self):
        self.assertEqual(self._run("--install").returncode, 0)
        content = self.crontab_file.read_text(encoding="utf-8")
        self.crontab_file.write_text(
            content.replace("--budget-seconds 540", "--budget-seconds 539"),
            encoding="utf-8",
        )
        before = self.crontab_file.read_bytes()

        result = self._run("--check")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_duplicate_lane_owner_is_rejected_without_writes(self):
        duplicate = self._legacy_lines()[1]
        self.crontab_file.write_text(f"{duplicate}\n{duplicate}\n", encoding="utf-8")
        before = self.crontab_file.read_bytes()

        result = self._run("--install")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_unknown_loose_owner_variant_is_rejected_without_writes(self):
        original = (
            f"*/2 * * * * cd {self.django_root} && {self.python} "
            "manage.py reconcile_ig_checkout --limit 1 >/tmp/alternate.log 2>&1\n"
        )
        self.crontab_file.write_text(original, encoding="utf-8")
        before = self.crontab_file.read_bytes()

        result = self._run("--install")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_unsupported_managed_version_is_rejected(self):
        self.crontab_file.write_text(
            "# BEGIN TWOCOMMS INSTAGRAM PERIODIC JOBS v2\n/unknown\n",
            encoding="utf-8",
        )
        before = self.crontab_file.read_bytes()
        result = self._run("--install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_managed_block_with_unknown_command_is_not_silently_replaced(self):
        self.crontab_file.write_text(
            f"{BEGIN_MARKER}\n/bin/unknown --mutate\n{END_MARKER}\n",
            encoding="utf-8",
        )
        before = self.crontab_file.read_bytes()
        result = self._run("--install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_managed_block_and_loose_owner_cannot_coexist(self):
        self.assertEqual(self._run("--install").returncode, 0)
        content = self.crontab_file.read_text(encoding="utf-8")
        self.crontab_file.write_text(content + self._legacy_lines()[0] + "\n", encoding="utf-8")
        before = self.crontab_file.read_bytes()
        result = self._run("--install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_duplicate_and_reversed_markers_fail_closed(self):
        candidates = (
            f"{BEGIN_MARKER}\n{END_MARKER}\n{BEGIN_MARKER}\n{END_MARKER}\n",
            f"{END_MARKER}\n{BEGIN_MARKER}\n",
        )
        for content in candidates:
            with self.subTest(content=content):
                self.crontab_file.write_text(content, encoding="utf-8")
                before = self.crontab_file.read_bytes()
                result = self._run("--install")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_unsafe_or_missing_paths_are_rejected_before_crontab_write(self):
        cases = (
            {"TWC_DJANGO_ROOT": "relative/root"},
            {"TWC_DJANGO_ROOT": str(self.root / "unsafe;root")},
            {"TWC_PYTHON": "python"},
            {"TWC_FLOCK_BIN": str(self.root / "missing-flock")},
            {"TWC_TIMEOUT_BIN": "relative/timeout"},
        )
        for extra_env in cases:
            with self.subTest(extra_env=extra_env):
                try:
                    self.crontab_file.unlink()
                except FileNotFoundError:
                    pass
                result = self._run("--install", extra_env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.crontab_file.exists())

    def test_crontab_read_failure_is_not_treated_as_an_empty_crontab(self):
        self._write_executable(
            "crontab",
            """#!/usr/bin/env bash
if [ "${1:-}" = "-l" ]; then echo 'permission denied' >&2; exit 1; fi
exit 99
""",
        )
        result = self._run("--install")
        self.assertEqual(result.returncode, 69)
        self.assertFalse(self.crontab_file.exists())

    def test_installed_owner_has_explicit_environment_and_single_global_lock(self):
        self.assertEqual(self._run("--install").returncode, 0)
        owner_lines = [
            line
            for line in self.crontab_file.read_text(encoding="utf-8").splitlines()
            if "manage.py run_instagram_periodic_jobs" in line
        ]
        self.assertEqual(len(owner_lines), 1)
        owner = owner_lines[0]
        self.assertIn("DJANGO_ENV=production", owner)
        self.assertIn("DJANGO_SETTINGS_MODULE=twocomms.production_settings", owner)
        self.assertEqual(owner.count("tmp/twocomms_heavy_background.lock"), 1)
        self.assertLess(owner.index("flock -w 50 -E 75"), owner.index("manage.py"))

    def test_coordinator_marker_outside_managed_block_is_rejected(self):
        self.assertEqual(self._run("--install").returncode, 0)
        content = self.crontab_file.read_text(encoding="utf-8")
        content = content.replace("# codex:instagram-periodic-coordinator\n", "")
        content += "# codex:instagram-periodic-coordinator\n"
        self.crontab_file.write_text(content, encoding="utf-8")
        before = self.crontab_file.read_bytes()
        result = self._run("--install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_pre_revert_rollback_restores_five_legacy_owners_without_metadata(self):
        self.crontab_file.write_text("17 4 * * * /opt/unrelated\n", encoding="utf-8")
        self.assertEqual(self._run("--install").returncode, 0)

        rollback = self._run("--rollback")
        content = self.crontab_file.read_text(encoding="utf-8")

        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        self.assertEqual(self._run("--check-rollback").returncode, 0)
        self.assertNotEqual(self._run("--check").returncode, 0)
        self.assertNotIn("manage.py run_instagram_periodic_jobs", content)
        self.assertNotIn("manage.py check_ig_gemini_metadata_health", content)
        for command in (
            "reconcile_order_telegram_notifications",
            "reconcile_ig_checkout",
            "reconcile_ig_order_fulfillment",
            "poll_ig_deal_payments",
            "run_call_ai_analyses",
        ):
            self.assertEqual(content.count(f"manage.py {command}"), 1)
        self.assertIn("17 4 * * * /opt/unrelated", content)

        self.assertEqual(self._run("--install").returncode, 0)
        restored = self.crontab_file.read_text(encoding="utf-8")
        self.assertEqual(restored.count("manage.py run_instagram_periodic_jobs"), 1)


if __name__ == "__main__":
    unittest.main()
