import gzip
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_mysql.sh"


class BackupMySQLScriptTests(unittest.TestCase):
    def test_repository_entry_point_is_executable(self):
        self.assertTrue(os.access(BACKUP_SCRIPT, os.X_OK))

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.backup_root = self.home / "db_backups"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir(parents=True)
        self.home.mkdir()
        self.defaults_file = self.home / ".my.cnf"
        self.defaults_file.write_text("[client]\nuser=test\npassword=test\n", encoding="utf-8")
        self.defaults_file.chmod(0o600)
        self.dump_log = self.root / "dump.log"
        self.started_file = self.root / "dump.started"
        self._write_executable(
            "mysqldump",
            """#!/usr/bin/env bash
set -eu
db="${!#}"
printf '%s\n' "$db" >> "$FAKE_DUMP_LOG"
if [ -n "${FAKE_STARTED_FILE:-}" ]; then
  : > "$FAKE_STARTED_FILE"
fi
if [ "${FAKE_FAIL_DB:-}" = "$db" ]; then
  exit 42
fi
if [ "${FAKE_DUMP_SLEEP:-0}" != "0" ]; then
  sleep "$FAKE_DUMP_SLEEP"
fi
printf '%s\n' 'CREATE TABLE sample (id integer);'
for i in $(seq 1 900); do
  printf 'INSERT INTO sample VALUES (%s);\n' "$i"
done
""",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _env(self, **overrides):
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "TWC_BACKUP_DIR": str(self.backup_root),
                "TWC_MYSQL_DEFAULTS_FILE": str(self.defaults_file),
                "TWC_MIN_DUMP_BYTES": "100",
                "TWC_BACKUP_STAMP": "20260716",
                "TWC_BACKUP_DOW": "4",
                "FAKE_DUMP_LOG": str(self.dump_log),
                "FAKE_STARTED_FILE": str(self.started_file),
            }
        )
        env.update(overrides)
        return env

    def _run(self, *databases, **env_overrides):
        return subprocess.run(
            ["bash", str(BACKUP_SCRIPT), *databases],
            env=self._env(**env_overrides),
            text=True,
            capture_output=True,
            timeout=20,
        )

    def test_requires_explicit_database_configuration(self):
        result = self._run()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("database", result.stderr.lower())
        self.assertEqual(list(self.backup_root.rglob("*.sql.gz")), [])

    def test_rejects_empty_database_tokens_from_environment(self):
        for configured_names in (",", "main_db,", ",dtf_db", "main_db,,dtf_db"):
            with self.subTest(configured_names=configured_names):
                result = self._run(TWC_DB_NAMES=configured_names)

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(self.backup_root.rglob("*.sql.gz")), [])
                self.assertFalse((self.backup_root / "last_success").exists())

    def test_rejects_option_like_database_names(self):
        for configured_names in ("--all-databases", "-main_db"):
            with self.subTest(positional=configured_names):
                result = self._run(configured_names)
                self.assertNotEqual(result.returncode, 0)
            with self.subTest(environment=configured_names):
                result = self._run(TWC_DB_NAMES=configured_names)
                self.assertNotEqual(result.returncode, 0)

    def test_accepts_comma_separated_database_environment(self):
        result = self._run(TWC_DB_NAMES="main_db,dtf_db")

        self.assertEqual(result.returncode, 0, result.stderr)
        archives = sorted((self.backup_root / "daily").glob("*.sql.gz"))
        self.assertEqual(len(archives), 2)

    def test_backs_up_every_configured_database_with_private_permissions(self):
        result = self._run("main_db", "dtf_db")

        self.assertEqual(result.returncode, 0, result.stderr)
        archives = sorted((self.backup_root / "daily").glob("*.sql.gz"))
        self.assertEqual(
            [path.name for path in archives],
            ["dtf_db-20260716.sql.gz", "main_db-20260716.sql.gz"],
        )
        for archive in archives:
            with gzip.open(archive, "rt", encoding="utf-8") as dump:
                self.assertIn("CREATE TABLE sample", dump.read())
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backup_root.stat().st_mode), 0o700)
        self.assertEqual(self.dump_log.read_text(encoding="utf-8").splitlines(), ["main_db", "dtf_db"])
        self.assertTrue((self.backup_root / "last_success").is_file())

    def test_one_database_failure_publishes_none_of_the_batch(self):
        daily = self.backup_root / "daily"
        daily.mkdir(parents=True)
        old_main = daily / "main_db-20260716.sql.gz"
        old_dtf = daily / "dtf_db-20260716.sql.gz"
        old_main.write_bytes(b"old-main")
        old_dtf.write_bytes(b"old-dtf")

        result = self._run("main_db", "dtf_db", FAKE_FAIL_DB="dtf_db")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(old_main.read_bytes(), b"old-main")
        self.assertEqual(old_dtf.read_bytes(), b"old-dtf")

    def test_corrupt_temporary_archive_never_replaces_last_good_dump(self):
        self._write_executable(
            "gzip",
            """#!/usr/bin/env bash
if [ "${1:-}" = "-t" ]; then
  exit 1
fi
cat
""",
        )
        daily = self.backup_root / "daily"
        daily.mkdir(parents=True)
        final_dump = daily / "main_db-20260716.sql.gz"
        final_dump.write_bytes(b"last-known-good")

        result = self._run("main_db", TWC_DB_NAME="main_db")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(final_dump.read_bytes(), b"last-known-good")

    def test_terminated_run_cleans_temporary_files_without_publishing(self):
        process = subprocess.Popen(
            ["bash", str(BACKUP_SCRIPT), "main_db"],
            env=self._env(FAKE_DUMP_SLEEP="2"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not self.started_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.started_file.exists(), "dump did not start")

        process.terminate()
        stdout, stderr = process.communicate(timeout=10)

        self.assertNotEqual(process.returncode, 0, f"{stdout}\n{stderr}")
        self.assertEqual(list(self.backup_root.rglob("*.sql.gz")), [])
        self.assertEqual(list(self.backup_root.rglob("*.tmp.*")), [])

    def test_failed_success_marker_publish_leaves_no_marker_temp(self):
        self._write_executable(
            "mv",
            """#!/usr/bin/env bash
if [[ "$*" == *".last_success.tmp."* ]]; then
  exit 42
fi
exec /bin/mv "$@"
""",
        )

        result = self._run("main_db")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.backup_root.glob(".last_success.tmp.*")), [])

    @unittest.skipUnless(shutil.which("flock"), "flock is verified on the production Linux host")
    def test_concurrent_run_is_rejected_while_first_dump_holds_lock(self):
        first = subprocess.Popen(
            ["bash", str(BACKUP_SCRIPT), "main_db"],
            env=self._env(FAKE_DUMP_SLEEP="2"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 5
            while not self.started_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.started_file.exists(), "first dump did not start")

            second = self._run("main_db")

            self.assertEqual(second.returncode, 75, second.stderr)
            self.assertIn("already running", second.stderr.lower())
        finally:
            stdout, stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, f"{stdout}\n{stderr}")
