"""Stage 0 contracts for the Django 6.1 runtime and CI baseline."""

from __future__ import annotations

import json
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "twocomms"
PYTHON_PIN = ROOT / ".python-version"
GENERAL_WORKFLOW = ROOT / ".github" / "workflows" / "django61-gate.yml"
MARIADB_WORKFLOW = ROOT / ".github" / "workflows" / "instagram-bot-mariadb-gate.yml"
STAGE0_RUNBOOK = ROOT / "docs" / "operations" / "django61-stage0-runbook.md"
OPS_DOC = ROOT / "twocomms" / "docs" / "OPS.md"


class ProjectRuntimeContractTests(unittest.TestCase):
    def test_tracked_python_pin_is_exact(self):
        self.assertEqual(PYTHON_PIN.read_text(encoding="utf-8"), "3.14.6\n")

    def test_runtime_validator_rejects_any_version_drift(self):
        from scripts.verify_project_runtime import RuntimeMismatch, validate_runtime

        expected = {
            "python": "3.14.6",
            "django": "6.1",
            "djangorestframework": "3.18.0",
            "mysqlclient": "2.2.8",
        }
        self.assertEqual(validate_runtime(expected), expected)
        for name, value in (
            ("python", "3.14.5"),
            ("django", "5.2.11"),
            ("djangorestframework", "3.17.0"),
            ("mysqlclient", "2.2.7"),
        ):
            with self.subTest(name=name), self.assertRaises(RuntimeMismatch):
                validate_runtime({**expected, name: value})

    def test_runtime_cli_emits_sanitized_exact_versions(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "verify_project_runtime.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["python"], "3.14.6")
        self.assertEqual(payload["django"], "6.1")
        self.assertEqual(payload["djangorestframework"], "3.18.0")
        self.assertEqual(payload["mysqlclient"], "2.2.8")
        self.assertNotIn("executable", payload)


class NonDtfSettingsContractTests(unittest.TestCase):
    def _probe(self, settings_module: str) -> dict[str, object]:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("DB_") and name != "DATABASE_URL"
        }
        environment.update(
            {
                "DJANGO_SETTINGS_MODULE": settings_module,
                "SECRET_KEY": "stage0-settings-contract",
                "PYTHONPATH": str(APP_ROOT),
            }
        )
        statement = (
            "import json,django; django.setup(); "
            "from django.apps import apps; from django.conf import settings; "
            "print(json.dumps({"
            "'apps':[c.name for c in apps.get_app_configs()],"
            "'databases':sorted(settings.DATABASES),"
            "'routers':list(settings.DATABASE_ROUTERS),"
            "'migration_modules':settings.MIGRATION_MODULES,"
            "'network_policy':settings.TEST_NETWORK_POLICY"
            "},sort_keys=True))"
        )
        result = subprocess.run(
            [sys.executable, "-c", statement],
            cwd=APP_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_migration_profile_has_real_graph_and_no_dtf_surface(self):
        payload = self._probe("test_settings_migrations_non_dtf")

        self.assertNotIn("dtf", payload["apps"])
        self.assertEqual(payload["databases"], ["default"])
        self.assertEqual(payload["routers"], [])
        self.assertIsInstance(payload["migration_modules"], dict)
        self.assertEqual(
            payload["migration_modules"]["warehouse"],
            "test_support.warehouse_migrations_non_dtf",
        )
        self.assertEqual(payload["network_policy"], "deny-external")

    def test_warehouse_shadow_preserves_non_dtf_mariadb_repairs(self):
        sys.path.insert(0, str(APP_ROOT))
        try:
            shadow = importlib.import_module(
                "test_support.warehouse_migrations_non_dtf."
                "0012_mariadb_native_uuid_compatibility"
            )
            real = importlib.import_module(
                "warehouse.migrations.0012_mariadb_native_uuid_compatibility"
            )
        finally:
            sys.path.remove(str(APP_ROOT))

        self.assertEqual(
            shadow.NON_DTF_UUID_COLUMNS,
            tuple(
                item
                for item in real.LEGACY_UUID_COLUMNS
                if not item[0].casefold().startswith("dtf_")
            ),
        )
        self.assertEqual(shadow.WAREHOUSE_TABLES, real.WAREHOUSE_TABLES)
        operation = shadow.Migration.operations[0]
        self.assertIsNot(operation.code, operation.noop)

    def test_synthetic_model_drift_makes_migration_gate_red(self):
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("DB_") and name != "DATABASE_URL"
        }
        environment.update(
            {
                "SECRET_KEY": "synthetic-migration-drift",
                "PYTHONPATH": str(APP_ROOT),
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "manage.py",
                "makemigrations",
                "migration_drift_probe",
                "--check",
                "--dry-run",
                "--noinput",
                "--settings=test_settings_migration_drift_probe",
            ],
            cwd=APP_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Create model SyntheticMigrationDrift", result.stdout)


class ManagementCommandSmokeContractTests(unittest.TestCase):
    def test_command_inventory_requires_exact_baseline_count(self):
        from scripts.check_management_commands import validate_command_count

        self.assertEqual(validate_command_count(176), [])
        for count in (174, 175, 177):
            with self.subTest(count=count):
                failures = validate_command_count(count)
                self.assertEqual(failures[0]["error"], "CommandCountMismatch")

    def test_parser_smoke_blocks_database_connections(self):
        sys.path.insert(0, str(APP_ROOT))
        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE", "test_settings_no_network_non_dtf"
        )
        os.environ.setdefault("SECRET_KEY", "command-db-guard-contract")
        import django

        django.setup()
        from django.core.management.base import BaseCommand
        from django.db import connections
        from scripts import check_management_commands

        class DatabaseOpeningCommand(BaseCommand):
            def add_arguments(self, parser):
                connections["default"].ensure_connection()

        fake_module = type("FakeModule", (), {"Command": DatabaseOpeningCommand})
        with (
            mock.patch.object(
                check_management_commands,
                "discover_command_modules",
                return_value=["example.management.commands.opens_database"],
            ),
            mock.patch.object(
                check_management_commands.importlib,
                "import_module",
                return_value=fake_module,
            ),
        ):
            payload = check_management_commands.check_commands()

        connections.close_all()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failed"][0]["error"], "RuntimeError")

    def test_all_project_commands_import_and_build_parser_without_dtf(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "commands.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "check_management_commands.py"),
                    "--output",
                    str(evidence),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(evidence.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["command_count"], 176)
        self.assertEqual(payload["failed"], [])
        self.assertEqual(
            payload["modules"].count(
                "task_runtime.management.commands.run_durable_tasks"
            ),
            1,
        )
        self.assertFalse(any("dtf" in module.casefold() for module in payload["modules"]))
        self.assertEqual(evidence.name, "commands.json")


class Django61WorkflowContractTests(unittest.TestCase):
    def test_general_gate_covers_normal_non_dtf_changes(self):
        source = GENERAL_WORKFLOW.read_text(encoding="utf-8")
        trigger_block = source.split("permissions:", 1)[0]
        self.assertIn("push:", trigger_block)
        self.assertIn("pull_request:", trigger_block)
        self.assertEqual(trigger_block.count("paths-ignore:"), 2)
        self.assertNotRegex(trigger_block, r"(?m)^\s+paths:\s*$")
        for marker in (
            "python-version-file: .python-version",
            "scripts/verify_project_runtime.py",
            "scripts/verify_locked_requirements.py",
            "check --database=default --settings=test_settings_no_network_non_dtf",
            "test_settings_migrations_non_dtf",
            "scripts/check_management_commands.py",
            "scripts/run_django_warning_gate.py",
            "scripts/run_static_gate.py",
            "scripts/build_non_dtf_inventory.py",
            "actions/upload-artifact@",
        ):
            self.assertIn(marker, source)
        for excluded_path in (
            '"twocomms/dtf/**"',
            '"twocomms/twocomms/urls_dtf.py"',
            '"specs/dtf-codex/**"',
        ):
            self.assertEqual(trigger_block.count(excluded_path), 2)

        runtime_step = source.split(
            "- name: Verify exact runtime and lock", 1
        )[1].split("- name:", 1)[0]
        self.assertIn("python scripts/verify_project_runtime.py", runtime_step)
        self.assertNotIn("continue-on-error", runtime_step)

    def test_general_gate_archives_non_blocking_full_non_dtf_smoke(self):
        source = GENERAL_WORKFLOW.read_text(encoding="utf-8")
        smoke_step = source.split(
            "- name: Capture full non-DTF smoke", 1
        )[1].split("- name:", 1)[0]

        self.assertIn("if: github.event_name == 'workflow_dispatch'", smoke_step)
        self.assertIn("id: full_non_dtf_smoke", smoke_step)
        self.assertIn("continue-on-error: true", smoke_step)
        self.assertIn("scripts/run_non_dtf_test_suite.py", smoke_step)
        self.assertIn("--settings test_settings_no_network_non_dtf", smoke_step)
        self.assertIn("--output full-non-dtf-smoke.log", smoke_step)

        upload_step = source.split("- name: Upload Stage 0 evidence", 1)[1]
        self.assertIn("if: always()", upload_step)
        self.assertIn("full-non-dtf-smoke.log", upload_step)
        self.assertIn("docs/qa/django61-full-ab-baseline.json", upload_step)

    def test_general_gate_validates_tracked_ab_artifacts_without_rerunning_them(self):
        source = GENERAL_WORKFLOW.read_text(encoding="utf-8")
        validation_step = source.split(
            "- name: Validate tracked Django A/B evidence", 1
        )[1].split("- name:", 1)[0]

        self.assertIn("--validate docs/qa/django61-full-ab-baseline.json", validation_step)
        self.assertIn(
            "--validate docs/qa/django61-targeted-ab-baseline.json",
            validation_step,
        )
        self.assertNotIn("run_non_dtf_test_suite.py", validation_step)

        upload_step = source.split("- name: Upload Stage 0 evidence", 1)[1]
        self.assertIn("docs/qa/django61-targeted-ab-baseline.json", upload_step)

    def test_general_gate_keeps_pull_requests_fast_and_docs_pushes_quiet(self):
        source = GENERAL_WORKFLOW.read_text(encoding="utf-8")
        push_block = source.split("push:", 1)[1].split("pull_request:", 1)[0]
        pull_request_block = source.split("pull_request:", 1)[1].split(
            "permissions:", 1
        )[0]

        for docs_path in (
            '"docs/operations/**"',
            '"docs/plans/**"',
            '"docs/qa/*.md"',
            '"dj6_update_all.md"',
        ):
            self.assertIn(docs_path, push_block)
            self.assertNotIn(docs_path, pull_request_block)

        smoke_step = source.split(
            "- name: Capture full non-DTF smoke", 1
        )[1].split("- name:", 1)[0]
        comparison_step = source.split(
            "- name: Compare full smoke with tracked Django 6.1 baseline", 1
        )[1].split("- name:", 1)[0]
        self.assertIn("if: github.event_name == 'workflow_dispatch'", smoke_step)
        self.assertIn(
            "if: always() && github.event_name == 'workflow_dispatch'",
            comparison_step,
        )

    def test_mariadb_gate_uses_exact_runtime_and_database_check(self):
        source = MARIADB_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python-version-file: .python-version", source)
        self.assertIn("scripts/verify_project_runtime.py", source)
        self.assertNotIn('python-version: "3.14"', source)
        for settings_path in (
            '"twocomms/twocomms/settings.py"',
            '"twocomms/twocomms/production_settings.py"',
        ):
            self.assertEqual(source.count(settings_path), 2)
        runner = (ROOT / "scripts" / "run_mariadb_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"--database=default"', runner)
        # Django warnings fail the command before the explicit warning-policy
        # classifier provides its defense-in-depth validation.
        self.assertIn('"--fail-level=WARNING"', runner)
        self.assertNotIn('"--fail-level=ERROR"', runner)
        self.assertIn("classify_database_check_warnings", runner)


class Django61Stage0DocumentationContractTests(unittest.TestCase):
    def test_server_matrix_uses_repository_relative_script_path(self):
        runbook = STAGE0_RUNBOOK.read_text(encoding="utf-8")
        ops = OPS_DOC.read_text(encoding="utf-8")

        for phase in ("preflight", "post-deploy"):
            expected = f"python ../scripts/run_django61_live_matrix.py server --phase {phase}"
            self.assertIn(expected, runbook)
            self.assertIn(expected, ops)
            self.assertNotIn(
                f"python scripts/run_django61_live_matrix.py server --phase {phase}",
                runbook,
            )
            self.assertNotIn(
                f"python scripts/run_django61_live_matrix.py server --phase {phase}",
                ops,
            )


if __name__ == "__main__":
    unittest.main()
