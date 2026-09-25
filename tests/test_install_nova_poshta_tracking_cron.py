import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_nova_poshta_tracking_cron.sh"
BEGIN_MARKER = "# BEGIN TWOCOMMS NOVA POSHTA TRACKING"
END_MARKER = "# END TWOCOMMS NOVA POSHTA TRACKING"
LEGACY_MARKER = "# codex:nova-poshta-tracking"


class RetireNovaPoshtaTrackingCronTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.crontab_file = self.root / "crontab"
        self.project_root = self.root / "TwoComms_Site"
        self.django_root = self.project_root / "twocomms"
        self.django_root.mkdir(parents=True)
        self.python = self.root / "python"
        self.python.write_text("", encoding="utf-8")
        self.python.chmod(0o700)
        self._write_executable("crontab", """#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "-l" ]; then
  if [ -f "$FAKE_CRONTAB_FILE" ]; then cat "$FAKE_CRONTAB_FILE"; exit 0; fi
  echo 'no crontab for test' >&2; exit 1
fi
cp "$1" "$FAKE_CRONTAB_FILE"
""")
        for name in ("flock", "timeout", "nice"):
            self._write_executable(name, "#!/usr/bin/env bash\nexit 0\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _env(self):
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            "FAKE_CRONTAB_FILE": str(self.crontab_file),
            "TWC_PROJECT_ROOT": str(self.project_root),
            "TWC_DJANGO_ROOT": str(self.django_root),
            "TWC_PYTHON": str(self.python),
            "TWC_FLOCK_BIN": str(self.fake_bin / "flock"),
            "TWC_TIMEOUT_BIN": str(self.fake_bin / "timeout"),
            "TWC_NICE_BIN": str(self.fake_bin / "nice"),
        })
        return env

    def _run(self, mode):
        return subprocess.run(["bash", str(INSTALL_SCRIPT), mode],
                              env=self._env(), text=True, capture_output=True, timeout=10)

    def _coordinator(self):
        return f"* * * * * cd {self.django_root} && {self.python} manage.py run_instagram_periodic_jobs --budget-seconds 540\n"

    def _tracking(self, *, wait=False):
        lock = self.django_root / "tmp/twocomms_heavy_background.lock"
        log = self.django_root / "logs/nova_poshta_cron.log"
        flock_mode = "-w 50" if wait else "-n"
        return (
            f"*/5 * * * * cd {self.django_root} && DJANGO_ENV=production "
            "DJANGO_SETTINGS_MODULE=twocomms.production_settings "
            f"{self.fake_bin / 'flock'} {flock_mode} -E 75 {lock} "
            f"{self.fake_bin / 'timeout'} --signal=TERM --kill-after=15s 240s "
            f"{self.fake_bin / 'nice'} -n 10 {self.python} manage.py "
            f"update_tracking_statuses >> {log} 2>&1"
        )

    def test_retire_managed_owner_preserves_unrelated_and_is_idempotent(self):
        self.crontab_file.write_text(
            "MAILTO=ops@example.test\n17 4 * * * /opt/other-job\n"
            + self._coordinator()
            + f"{BEGIN_MARKER}\n{LEGACY_MARKER}\n{self._tracking()}\n{END_MARKER}\n",
            encoding="utf-8",
        )
        first = self._run("--retire")
        retired = self.crontab_file.read_bytes()
        second = self._run("--retire")
        check = self._run("--check-retired")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(self.crontab_file.read_bytes(), retired)
        self.assertIn(b"17 4 * * * /opt/other-job", retired)
        self.assertIn(b"run_instagram_periodic_jobs", retired)
        self.assertNotIn(b"update_tracking_statuses", retired)
        self.assertNotIn(BEGIN_MARKER.encode(), retired)

    def test_retire_known_loose_and_waiting_owner(self):
        for wait in (False, True):
            with self.subTest(wait=wait):
                self.crontab_file.write_text(self._coordinator() + self._tracking(wait=wait) + "\n", encoding="utf-8")
                result = self._run("--retire")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.crontab_file.read_text(), self._coordinator())

    def test_retire_requires_replacement_coordinator(self):
        self.crontab_file.write_text(
            f"{BEGIN_MARKER}\n{LEGACY_MARKER}\n{self._tracking()}\n{END_MARKER}\n",
            encoding="utf-8",
        )
        before = self.crontab_file.read_bytes()
        result = self._run("--retire")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_unknown_owner_and_malformed_block_fail_without_writes(self):
        cases = (
            self._coordinator() + f"{BEGIN_MARKER}\n{LEGACY_MARKER}\n/bin/unknown\n{END_MARKER}\n",
            self._coordinator() + f"{BEGIN_MARKER}\n{LEGACY_MARKER}\n{self._tracking()}\n",
            self._coordinator() + f"* * * * * {self.python} manage.py update_tracking_statuses\n",
        )
        for original in cases:
            with self.subTest(original=original):
                self.crontab_file.write_text(original, encoding="utf-8")
                before = self.crontab_file.read_bytes()
                result = self._run("--retire")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.crontab_file.read_bytes(), before)

    def test_stale_install_cannot_recreate_second_owner(self):
        self.crontab_file.write_text(self._coordinator(), encoding="utf-8")
        before = self.crontab_file.read_bytes()
        self.assertNotEqual(self._run("--install").returncode, 0)
        self.assertNotEqual(self._run("--check").returncode, 0)
        self.assertEqual(self.crontab_file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
