import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/instagram-bot-mariadb-gate.yml"


class MariaDbWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.source = WORKFLOW.read_text(encoding="utf-8")

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
        self.assertIn("--server-mode external --suite lifecycle", self.source)
        self.assertIn("--server-mode external --suite checkout-concurrency", self.source)
        self.assertIn("--server-mode external --suite follow-ugc-concurrency", self.source)
        self.assertIn("--server-mode external --suite lease-concurrency", self.source)
        self.assertIn("MARIADB_ADMIN_PASSWORD: gate-root-password", self.source)
        self.assertNotRegex(self.source, r"(?m)^\s+DB_PASSWORD:")
        self.assertNotIn("qlknpodo_MySQL_DB", self.source)

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
        self.assertIn("mariadb-gate-evidence.txt", self.source)
        self.assertIn("mariadb-follow-ugc-evidence.txt", self.source)
        self.assertIn("mariadb-lease-concurrency-evidence.txt", self.source)

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
            "tests/test_mariadb_workflow_contract.py",
        ):
            self.assertIn(path, self.source)
