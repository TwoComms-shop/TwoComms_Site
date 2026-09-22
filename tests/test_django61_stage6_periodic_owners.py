import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/qa/django61-stage6-periodic-owners.json"
VALIDATOR = ROOT / "scripts/verify_django61_stage6_periodic_owners.py"


class Stage6PeriodicOwnerTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.jobs = self.manifest["jobs"]
        self.blocks = {}
        for job in self.jobs:
            self.blocks.setdefault(job["managed_block"], []).append(job)

    def crontab(self, jobs=None):
        jobs = (
            [job for job in self.jobs if job.get("active", True)]
            if jobs is None
            else jobs
        )
        blocks = {}
        for job in jobs:
            blocks.setdefault(job["managed_block"], []).append(job)
        lines = []
        for marker, marker_jobs in blocks.items():
            lines.append(marker)
            for job in marker_jobs:
                timeout = f" {job['timeout']}" if job["timeout_required"] else ""
                command = job["command"]
                if " --" not in command:
                    command = f"{command} --limit 1"
                environment = " ".join(job.get("environment", []))
                if environment:
                    environment += " "
                lines.append(
                    f"{job['cadence']} cd /srv/twocomms && {environment}{job['flock']} /srv/twocomms/{job['lock_path']}"
                    f"{timeout} /srv/twocomms/.venv/bin/python {command}"
                )
            lines.append(marker.replace("# BEGIN", "# END"))
        return "\n".join(lines) + "\n"

    def invoke_validator(self, crontab, manifest=MANIFEST, repo_root=ROOT):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(crontab)
            crontab_path = Path(handle.name)
        try:
            return subprocess.run(
                [sys.executable, str(VALIDATOR), "--manifest", str(manifest), "--crontab", str(crontab_path), "--repo-root", str(repo_root)],
                text=True, capture_output=True, check=False,
            )
        finally:
            crontab_path.unlink()

    def test_valid_non_dtf_contract(self):
        active_jobs = [job for job in self.jobs if job.get("active", True)]
        active_crontab = self.crontab(active_jobs)
        result = self.invoke_validator(active_crontab)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            len(payload["jobs"]),
            len(active_jobs),
        )
        durable_line = next(line for line in active_crontab.splitlines() if "run_durable_tasks" in line)
        self.assertIn("# BEGIN TWOCOMMS DJANGO61 DURABLE TASKS", active_crontab)
        self.assertIn("tmp/twocomms_heavy_background.lock", durable_line)
        self.assertIn("exec /usr/bin/flock -n -E 75", durable_line)
        self.assertIn("/usr/bin/timeout --signal=TERM --kill-after=15s 240s", durable_line)
        self.assertIn("--worker-id=cron-no-send", durable_line)
        self.assertIn("DJANGO_ENV=production", durable_line)
        self.assertIn("DJANGO_SETTINGS_MODULE=twocomms.production_settings", durable_line)

    def test_planned_job_does_not_require_owner_or_managed_block(self):
        planned_manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        planned_job = next(
            job
            for job in planned_manifest["jobs"]
            if job["id"] == "product_catalog_image_jobs"
        )
        planned_job["active"] = False
        active_jobs = [
            job for job in planned_manifest["jobs"] if job.get("active", True)
        ]
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(planned_manifest, handle)
            manifest_path = Path(handle.name)
        try:
            result = self.invoke_validator(
                self.crontab(active_jobs), manifest=manifest_path
            )
        finally:
            manifest_path.unlink()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            [job["id"] for job in payload["jobs"]],
            [job["id"] for job in active_jobs],
        )

    def test_inactive_job_block_or_owner_fails_closed(self):
        active_jobs = [job for job in self.jobs if job.get("active", True)]
        inactive_job = next(job for job in self.jobs if not job.get("active", True))

        with self.subTest("managed block"):
            result = self.invoke_validator(self.crontab(self.jobs))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inactive", result.stderr.lower())

        with self.subTest("loose owner line"):
            crontab = self.crontab(active_jobs) + (
                f"* * * * * /srv/twocomms/.venv/bin/python {inactive_job['command']}\n"
            )
            result = self.invoke_validator(crontab)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inactive", result.stderr.lower())

    def test_duplicate_loose_owner_fails_closed(self):
        result = self.invoke_validator(self.crontab() + self.crontab().splitlines()[1] + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one owner", result.stderr)

    def test_duplicate_managed_block_fails_closed(self):
        result = self.invoke_validator(self.crontab() + self.crontab())
        self.assertNotEqual(result.returncode, 0)

    def test_missing_timeout_fails_closed(self):
        lines = self.crontab().splitlines()
        index = next(i for i, line in enumerate(lines) if "run_instagram_periodic_jobs" in line)
        lines[index] = lines[index].replace("/usr/bin/timeout --signal=TERM --kill-after=15s 600s", "")
        result = self.invoke_validator("\n".join(lines) + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bounded timeout", result.stderr)

    def test_durable_owner_requires_the_proven_production_environment(self):
        crontab = self.crontab().replace(
            "DJANGO_ENV=production DJANGO_SETTINGS_MODULE=twocomms.production_settings ",
            "",
        )
        result = self.invoke_validator(crontab)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment contract", result.stderr)

    def test_dtf_is_rejected(self):
        result = self.invoke_validator(self.crontab() + "# DTF must remain excluded\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DTF", result.stderr)

    def test_unknown_twocomms_managed_block_is_rejected(self):
        unknown = (
            "# BEGIN TWOCOMMS UNKNOWN JOB\n"
            "* * * * * /srv/unknown\n"
            "# END TWOCOMMS UNKNOWN JOB\n"
        )
        result = self.invoke_validator(unknown + self.crontab())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown", result.stderr.lower())

    def test_rollback_path_is_required(self):
        broken = json.loads(MANIFEST.read_text(encoding="utf-8"))
        broken["rollback"]["path"] = "scripts/does-not-exist.sh"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            json.dump(broken, handle)
            path = Path(handle.name)
        try:
            result = self.invoke_validator(self.crontab(), manifest=path)
        finally:
            path.unlink()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback path", result.stderr)

    def test_rollback_contract_is_pre_revert_and_executable(self):
        rollback = self.manifest["rollback"]
        self.assertIn("--rollback", rollback["action"])
        self.assertIn("--check-rollback", rollback["action"])
        self.assertIn("only then revert code", rollback["action"])
        self.assertTrue(
            (ROOT / "scripts/install_instagram_periodic_jobs_cron.sh").is_file()
        )
        self.assertTrue(
            (ROOT / "scripts/install_instagram_bot_watchdog_cron.sh").is_file()
        )

    def test_owner_script_is_required(self):
        broken = json.loads(MANIFEST.read_text(encoding="utf-8"))
        broken["jobs"][0]["owner_path"] = "scripts/does-not-exist.sh"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            json.dump(broken, handle)
            path = Path(handle.name)
        try:
            result = self.invoke_validator(self.crontab(), manifest=path)
        finally:
            path.unlink()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner script", result.stderr)

    def test_every_heavy_django_owner_shares_one_account_wide_lock(self):
        heavy_jobs = [
            job
            for job in self.jobs
            if job.get("active", True) and job.get("runtime", "django") == "django"
        ]
        self.assertEqual(
            {job["lock_path"] for job in heavy_jobs},
            {self.manifest["global_heavy_process_lock"]},
        )

        broken = json.loads(MANIFEST.read_text(encoding="utf-8"))
        next(
            job for job in broken["jobs"] if job["id"] == "nova_poshta_tracking"
        )["lock_path"] = "tmp/nova_poshta_tracking.lock"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(broken, handle)
            path = Path(handle.name)
        try:
            result = self.invoke_validator(self.crontab(), manifest=path)
        finally:
            path.unlink()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("global admission lock", result.stderr)

    def test_coordinator_manifest_bounds_every_provided_lane(self):
        coordinator = next(
            job
            for job in self.jobs
            if job["id"] == "instagram_periodic_coordinator"
        )
        self.assertEqual(
            set(coordinator["provides_lanes"]),
            set(coordinator["lane_deadlines_seconds"]),
        )
        self.assertLessEqual(
            sum(coordinator["lane_deadlines_seconds"].values()),
            540,
        )

        broken = json.loads(MANIFEST.read_text(encoding="utf-8"))
        broken_coordinator = next(
            job
            for job in broken["jobs"]
            if job["id"] == "instagram_periodic_coordinator"
        )
        broken_coordinator["lane_deadlines_seconds"].pop("ig_deal_payments")
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False
        ) as handle:
            json.dump(broken, handle)
            path = Path(handle.name)
        try:
            result = self.invoke_validator(self.crontab(), manifest=path)
        finally:
            path.unlink()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bound every provided lane", result.stderr)


if __name__ == "__main__":
    unittest.main()
