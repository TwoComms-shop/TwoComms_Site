import subprocess
import unittest
from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/instagram-bot-mariadb-gate.yml"


class MariaDbWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.source = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.source)
        self.suite_job = self.workflow["jobs"]["suites"]

    def test_uses_pinned_mariadb_11_4_service_and_healthcheck(self):
        self.assertIn(
            "image: mariadb:11.4.12@sha256:"
            "67873d30a17f6a9c331f06363b2fa15f38abca415529966d67c84f87f82439fe",
            self.source,
        )
        self.assertIn("healthcheck.sh --connect --innodb_initialized", self.source)
        self.assertIn("MARIADB_ROOT_PASSWORD: gate-root-password", self.source)
        self.assertIn('MARIADB_ROOT_HOST: "%"', self.source)
        self.assertIn("timeout-minutes: 30", self.source)

    def test_runs_external_gate_without_production_database_variables(self):
        self.assertIn('--server-mode external --suite "${{ matrix.suite }}"', self.source)
        self.assertEqual(self.source.count("python scripts/run_mariadb_gate.py"), 1)
        self.assertIn("MARIADB_ADMIN_PASSWORD: gate-root-password", self.source)
        self.assertNotRegex(self.source, r"(?m)^\s+DB_PASSWORD:")
        self.assertNotIn("qlknpodo_MySQL_DB", self.source)

    def test_fixed_suites_run_independently_without_fail_fast(self):
        self.assertEqual(self.suite_job["strategy"], {
            "fail-fast": False,
            "max-parallel": 5,
            "matrix": {"suite": ["lifecycle", "checkout-concurrency", "follow-ugc-concurrency",
                "lease-concurrency", "conversion-context", "dialogue-quality"]},
        })
        self.assertEqual(self.suite_job["runs-on"], "ubuntu-24.04")
        self.assertEqual(self.suite_job["timeout-minutes"], 30)
        self.assertIn("mariadb", self.suite_job["services"])
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertNotIn("continue-on-error", self.source)

    def test_every_native_matrix_entry_has_a_runner_suite_and_coverage_is_not_duplicated(self):
        from scripts.run_mariadb_gate import SUITES

        matrix = self.suite_job["strategy"]["matrix"]["suite"]
        self.assertEqual(len(matrix), 6)
        self.assertEqual(len(set(matrix)), len(matrix))
        self.assertTrue(all(SUITES[name] for name in matrix))
        self.assertFalse(set(SUITES["dialogue-quality"]) & set(SUITES["conversion-context"]))

    def test_lifecycle_check_rejects_failed_cancelled_and_skipped_matrix(self):
        aggregate = self.workflow["jobs"]["lifecycle"]
        self.assertEqual(aggregate["name"], "lifecycle")
        self.assertEqual(aggregate["needs"], ["suites"])
        self.assertEqual(aggregate["if"], "${{ always() }}")
        self.assertEqual(len(aggregate["steps"]), 1)
        guard = aggregate["steps"][0]
        self.assertEqual(guard["env"], {"SUITES_RESULT": "${{ needs.suites.result }}"})
        for result in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(result=result):
                completed = subprocess.run(["bash", "-e", "-c", guard["run"]],
                    env={"SUITES_RESULT": result}, capture_output=True, check=False)
                self.assertEqual(completed.returncode == 0, result == "success")

    def test_runs_its_own_contracts_and_uploads_sanitized_evidence(self):
        self.assertIn(
            "tests.test_mariadb_gate_runner tests.test_mariadb_workflow_contract",
            self.source,
        )
        self.assertIn(
            "python -m unittest management.tests_test_settings_mariadb -v",
            self.source,
        )
        self.assertIn("working-directory: twocomms", self.source)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", self.source)
        self.assertIn("if: always()", self.source)
        upload = next(step for step in self.suite_job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["name"], "mariadb-${{ matrix.suite }}-evidence")
        self.assertEqual(upload["with"]["path"], "mariadb-${{ matrix.suite }}-evidence.txt")
        self.assertIn('tee "mariadb-${{ matrix.suite }}-evidence.txt"', self.source)

    def test_installs_mysqlclient_build_headers_before_locked_dependencies(self):
        self.assertIn("sudo apt-get install --yes libmariadb-dev", self.source)
        self.assertLess(
            self.source.index("sudo apt-get install --yes libmariadb-dev"),
            self.source.index("python -m pip install --require-hashes -r requirements.lock"),
        )

    def test_path_filters_cover_every_task_6a_input(self):
        for path in (
            "twocomms/requirements.lock",
            "twocomms/management/**",
            "twocomms/orders/promo_reservations.py",
            "twocomms/storefront/models.py",
            "twocomms/storefront/migrations/0095_promocode_guest_ugc.py",
            "twocomms/storefront/services/size_guide_authority.py",
            "twocomms/storefront/services/size_guides.py",
            "twocomms/storefront/views/product.py",
            "twocomms/storefront/tests/test_size_guide_authority_20261022.py",
            "twocomms/product_catalog/size_grid_services.py",
            "tests/test_mariadb_workflow_contract.py",
        ):
            self.assertIn(path, self.source)
