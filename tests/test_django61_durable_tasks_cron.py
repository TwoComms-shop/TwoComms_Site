import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_django61_durable_tasks_cron.sh"
PERIODIC_OWNERS_MANIFEST = REPO_ROOT / "docs" / "qa" / "django61-stage6-periodic-owners.json"
PERIODIC_OWNERS_VALIDATOR = REPO_ROOT / "scripts" / "verify_django61_stage6_periodic_owners.py"
BEGIN_MARKER = "# BEGIN TWOCOMMS DJANGO61 DURABLE TASKS"


class InstallDjango61DurableTasksCronTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.crontab_file = self.root / "crontab"
        self.django_root = self.root / "TwoComms_Site" / "twocomms"
        self.django_root.mkdir(parents=True)
        (self.django_root / "manage.py").write_text("#!/bin/sh\n", encoding="utf-8")
        self.cloudlinux_wrapper = self.root / "lve-manager" / "utils" / "python_wrapper"
        self.cloudlinux_wrapper.parent.mkdir(parents=True)
        self.cloudlinux_wrapper.write_text(
            """#!/usr/bin/env bash
set -eu
if [ "${TWC_FAKE_PYTHON_FAIL:-0}" = 1 ]; then
  echo 'private DB_PASSWORD=must-not-leak' >&2
  exit 19
fi
if [ "${1:-}" = "-c" ]; then
  case "${2:-}" in
    *"SELECT VERSION()"*) ;;
    *) exit 23 ;;
  esac
  if [ "${TWC_FAKE_PYTHON_ENGINE:-mysql}" = "sqlite" ]; then
    printf '%s\\n' '{"conn_max_age":0,"engine":"django.db.backends.sqlite3","mariadb":false,"task_runtime_ready":false}'
  else
    printf '%s\\n' '{"conn_max_age":0,"engine":"django.db.backends.mysql","mariadb":true,"task_runtime_ready":'"${TWC_FAKE_TASK_RUNTIME_READY:-true}"'}'
  fi
fi
""",
            encoding="utf-8",
        )
        self.cloudlinux_wrapper.chmod(0o700)
        self.python = self.root / "venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(self.cloudlinux_wrapper)
        self.flock = self.root / "flock"
        self.timeout = self.root / "timeout"
        for path in (self.flock, self.timeout):
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self._write_executable(
            "crontab",
            """#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "-l" ]; then
  if [ -f "$FAKE_CRONTAB_FILE" ]; then cat "$FAKE_CRONTAB_FILE"; exit 0; fi
  echo 'no crontab for test' >&2
  exit 1
fi
cp "$1" "$FAKE_CRONTAB_FILE"
""",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _env(self, include_paths=True):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "FAKE_CRONTAB_FILE": str(self.crontab_file),
                "TWC_DJANGO_ROOT": str(self.django_root),
                "TWC_PYTHON": str(self.python),
                "TWC_CLOUDLINUX_PYTHON_WRAPPER": str(self.cloudlinux_wrapper),
                "TWC_CRONTAB_BIN": "crontab",
            }
        )
        if include_paths:
            env.update({"TWC_FLOCK_BIN": str(self.flock), "TWC_TIMEOUT_BIN": str(self.timeout)})
        return env

    def _run(self, mode, *, env=None):
        return subprocess.run(
            ["bash", str(INSTALL_SCRIPT), mode],
            env=env or self._env(),
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_install_is_idempotent_and_preserves_unrelated_cron(self):
        self.crontab_file.write_text("17 4 * * * /opt/other-job\n", encoding="utf-8")

        first = self._run("--install")
        first_content = self.crontab_file.read_text(encoding="utf-8")
        second = self._run("--install")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.crontab_file.read_text(encoding="utf-8"), first_content)
        self.assertIn("17 4 * * * /opt/other-job", first_content)
        self.assertEqual(first_content.count(BEGIN_MARKER), 1)
        self.assertIn("run_durable_tasks --limit 25 --lease-seconds 60", first_content)
        self.assertIn(str(self.django_root / "manage.py"), first_content)
        self.assertIn("flock", first_content)
        self.assertIn("timeout", first_content)
        self.assertIn("--kill-after=15s", first_content)
        self.assertIn("DJANGO_ENV=production", first_content)
        self.assertIn("DJANGO_SETTINGS_MODULE=twocomms.production_settings", first_content)
        self.assertIn(" exec ", first_content)

    def test_installed_cron_block_satisfies_periodic_owner_contract(self):
        install = self._run("--install")

        self.assertEqual(install.returncode, 0, install.stderr)
        manifest = json.loads(PERIODIC_OWNERS_MANIFEST.read_text(encoding="utf-8"))
        durable_job = next(job for job in manifest["jobs"] if job["id"] == "django61_durable_tasks")
        durable_job["flock"] = f"exec {self.flock} -n -E 75"
        durable_job["timeout"] = f"{self.timeout} --signal=TERM --kill-after=15s 240s"
        manifest["jobs"] = [durable_job]
        manifest_path = self.root / "periodic-owners.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(PERIODIC_OWNERS_VALIDATOR),
                "--manifest",
                str(manifest_path),
                "--crontab",
                str(self.crontab_file),
                "--repo-root",
                str(REPO_ROOT),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preflight_rejects_missing_durable_task_schema_without_writing_crontab(self):
        original = "17 4 * * * /opt/other-job\\n"
        self.crontab_file.write_text(original, encoding="utf-8")
        env = self._env()
        env["TWC_FAKE_TASK_RUNTIME_READY"] = "false"

        result = self._run("--install", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DurableTask", result.stderr)
        self.assertEqual(self.crontab_file.read_text(encoding="utf-8"), original)

    def test_preflight_rejects_sqlite_without_writing_crontab(self):
        original = "17 4 * * * /opt/other-job\n"
        self.crontab_file.write_text(original, encoding="utf-8")
        env = self._env()
        env["TWC_FAKE_PYTHON_ENGINE"] = "sqlite"

        result = self._run("--install", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MySQL", result.stderr)
        self.assertEqual(self.crontab_file.read_text(encoding="utf-8"), original)

    def test_preflight_failure_is_sanitized_and_runs_for_check(self):
        original = "17 4 * * * /opt/other-job\n"
        self.crontab_file.write_text(original, encoding="utf-8")
        env = self._env()
        env["TWC_FAKE_PYTHON_FAIL"] = "1"

        result = self._run("--check", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("preflight", result.stderr.lower())
        self.assertNotIn("DB_PASSWORD", result.stderr)
        self.assertEqual(self.crontab_file.read_text(encoding="utf-8"), original)

    def test_selected_python_must_be_cloudlinux_wrapper_symlink(self):
        self.python.unlink()
        self.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.python.chmod(0o700)

        result = self._run("--install")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CloudLinux", result.stderr)

    def test_existing_durable_owner_outside_managed_block_is_rejected(self):
        original = "* * * * * /srv/twocomms/manage.py run_durable_tasks --limit 1\n"
        self.crontab_file.write_text(original, encoding="utf-8")

        result = self._run("--install")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside", result.stderr)
        self.assertEqual(self.crontab_file.read_text(encoding="utf-8"), original)

    def test_check_detects_missing_or_drifted_block(self):
        self.assertNotEqual(self._run("--check").returncode, 0)
        self.assertEqual(self._run("--install").returncode, 0)
        self.assertEqual(self._run("--check").returncode, 0)
        self.crontab_file.write_text("# drift\n", encoding="utf-8")
        self.assertNotEqual(self._run("--check").returncode, 0)

    def test_missing_runtime_environment_fails_closed(self):
        env = self._env()
        env.pop("TWC_PYTHON")
        result = self._run("--install", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TWC_PYTHON is required", result.stderr)

    def test_missing_manage_or_tool_fails_closed(self):
        (self.django_root / "manage.py").unlink()
        result = self._run("--install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manage.py", result.stderr)

        (self.django_root / "manage.py").write_text("#!/bin/sh\n", encoding="utf-8")
        self.flock.unlink()
        result = self._run("--install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("flock", result.stderr)


if __name__ == "__main__":
    unittest.main()
