import ast
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeAdmin:
    def __init__(
        self, *, host="127.0.0.1", port="3306", fail_at=None,
        fail_cleanup=False, fail_database_cleanup=False,
        identity=("11.4.12-MariaDB", "mariadb.org binary distribution"),
        residue=(False, False),
        existing=(False, False),
    ):
        self.calls = []
        self.host = host
        self.port = port
        self.fail_at = fail_at
        self.fail_cleanup = fail_cleanup
        self.fail_database_cleanup = fail_database_cleanup
        self.identity = identity
        self.residue = residue
        self.existing = existing

    def server_identity(self):
        self.calls.append(("server_identity",))
        return self.identity

    def create_database(self, name):
        self.calls.append(("create_database", name))
        if self.fail_at == "create_database":
            raise RuntimeError("create database failed")

    def create_user(self, username, password):
        self.calls.append(("create_user", username, password))
        if self.fail_at == "create_user":
            raise RuntimeError("create user failed")

    def grant_schema(self, username, database):
        self.calls.append(("grant_schema", username, database))
        if self.fail_at == "grant_schema":
            raise RuntimeError("grant schema failed")

    def ensure_namespace_absent(self, database, username):
        self.calls.append(("ensure_namespace_absent", database, username))
        if any(self.existing):
            from scripts.run_mariadb_gate import GateError

            objects = []
            if self.existing[0]:
                objects.append("database")
            if self.existing[1]:
                objects.append("user")
            raise GateError(
                "Refusing MariaDB gate: generated "
                + " and ".join(objects)
                + " already exists"
            )

    def drop_user(self, username):
        self.calls.append(("drop_user", username))
        if self.fail_cleanup:
            raise RuntimeError("drop user failed")

    def drop_database(self, database):
        self.calls.append(("drop_database", database))
        if self.fail_database_cleanup:
            raise RuntimeError("drop database failed")

    def verify_cleanup(self, database, username):
        self.calls.append(("verify_cleanup", database, username))
        return self.residue

    def verify_release_schema(self, database):
        self.calls.append(("verify_release_schema", database))
        if self.fail_at == "verify_release_schema":
            raise RuntimeError("release schema mismatch")
        return {
            "migration": "management.0156_ig_order_event_delivery_receipts",
            "provider_message_id": "varchar(255)",
            "delivery_provider_message_ids": "longtext+json_valid",
        }

    def prepare_database_warning_contract(self, database):
        self.calls.append(("prepare_database_warning_contract", database))
        if self.fail_at == "prepare_database_warning_contract":
            raise RuntimeError("warning contract staging failed")
        return {"warning_contract_engines": "3_myisam"}

    def capture_migration_prerequisites(self, database):
        self.calls.append(("capture_migration_prerequisites", database))
        if self.fail_at == "capture_migration_prerequisites":
            raise RuntimeError("prerequisite capture failed")

    def restore_migration_prerequisites(self, database):
        self.calls.append(("restore_migration_prerequisites", database))
        if self.fail_at == "restore_migration_prerequisites":
            raise RuntimeError("prerequisite restoration failed")
        return "restored_original"

    def verify_database_warning_contract_reverse(self, database):
        self.calls.append(("verify_database_warning_contract_reverse", database))
        if self.fail_at == "verify_database_warning_contract_reverse":
            raise RuntimeError("warning contract reverse mismatch")
        return {
            "warning_contract_reverse": "generated_removed+endpoint_hash_preserved"
        }

    def verify_follow_ugc_schema(self, database):
        self.calls.append(("verify_follow_ugc_schema", database))
        if self.fail_at == "verify_follow_ugc_schema":
            raise RuntimeError("follow UGC schema mismatch")
        return {
            "follow_ugc_migration": "management.0166_ig_ugc_reward_lifecycle",
            "guest_promo_migration": "storefront.0095_promocode_guest_ugc",
            "follow_ugc_tables": "12_innodb",
            "follow_ugc_unique_indexes": "verified",
            "follow_ugc_foreign_keys": "orm_only",
            "follow_ugc_lifecycle": "3_columns+2_indexes",
            "follow_ugc_lifecycle_job": "target_check+5_indexes",
        }

    def verify_database_warning_contract(self, database):
        self.calls.append(("verify_database_warning_contract", database))
        if self.fail_at == "verify_database_warning_contract":
            raise RuntimeError("database warning contract mismatch")
        return {
            "engines": "reviewvote_fit_innodb+push_myisam",
            "generated_columns": "2_verified",
            "unique_indexes": "3_new+1_preserved",
            "endpoint_unique": "hash_preserved",
            "show_create_tables": "verified",
        }

    def close(self):
        self.calls.append(("close",))


class FakeCommandRunner:
    def __init__(
        self,
        *,
        returncode=0,
        stdout="",
        stderr="migration failed",
        fail_command=None,
    ):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.fail_command = fail_command

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        command = args[2] if len(args) > 2 else None
        returncode = (
            self.returncode
            if self.fail_command is None or self.fail_command == command
            else 0
        )
        stderr = "" if returncode == 0 else self.stderr
        return subprocess.CompletedProcess(
            args, returncode, self.stdout, stderr
        )


class MarkerInspectingCommandRunner(FakeCommandRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.marker_snapshots = []

    def __call__(self, args, **kwargs):
        marker_value = kwargs["env"].get("TEST_REVIEW_WRITE_FREEZE_MARKER")
        if marker_value:
            marker = Path(marker_value)
            marker_stat = os.lstat(marker)
            self.marker_snapshots.append(
                {
                    "path": marker,
                    "absolute": marker.is_absolute(),
                    "mode": stat.S_IMODE(marker_stat.st_mode),
                    "uid": marker_stat.st_uid,
                    "content": marker.read_bytes(),
                }
            )
        else:
            self.marker_snapshots.append(None)
        return super().__call__(args, **kwargs)


class MariaDbGateRunnerTests(unittest.TestCase):
    def setUp(self):
        from scripts import run_mariadb_gate

        self.runner = run_mariadb_gate

    def test_prerequisite_restoration_uses_only_original_captured_values(self):
        database = "test_twocomms_ig_0123456789ab"
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [(name,) for name in ("id", "slug", "name", "parent_id")]
        original = (7, "veteran-fund", "Captured original category", 3)
        parent = (3, "news", "Captured original parent", None)
        cursor.fetchone.side_effect = [original, parent]
        cursor.fetchall.return_value = []
        with mock.patch.object(admin, "_connect", return_value=connection):
            admin.capture_migration_prerequisites(database)
            self.assertEqual(admin.restore_migration_prerequisites(database), "restored_original")
        inserts = cursor.execute.call_args_list[-2:]
        for insert, expected in zip(inserts, (parent, original), strict=True):
            self.assertIn(f"INSERT INTO `{database}`.`storefront_blogcategory`", insert.args[0])
            self.assertEqual(insert.args[1], expected)
            self.assertNotIn(expected[2], insert.args[0])
        connection.commit.assert_called_once()
        self.assertEqual(connection.close.call_count, 2)

    def test_prerequisite_restore_retains_present_identity_without_update(self):
        database = "test_twocomms_ig_0123456789ab"
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        admin._migration_prerequisite_snapshot = (database, ("id", "slug", "parent_id"), ((7, "veteran-fund", None),))
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(7, "veteran-fund", None)]
        with mock.patch.object(admin, "_connect", return_value=connection):
            self.assertEqual(admin.restore_migration_prerequisites(database), "retained")
        self.assertEqual(cursor.execute.call_count, 1)
        self.assertTrue(cursor.execute.call_args.args[0].startswith("SELECT"))

    def test_prerequisite_restore_rejects_conflicting_fixture_identity(self):
        database = "test_twocomms_ig_0123456789ab"
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        admin._migration_prerequisite_snapshot = (database, ("id", "slug", "parent_id"), ((7, "veteran-fund", None),))
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(7, "another-fixture", None)]
        with mock.patch.object(admin, "_connect", return_value=connection):
            with self.assertRaisesRegex(self.runner.GateError, "identity conflicts"):
                admin.restore_migration_prerequisites(database)
        self.assertEqual(cursor.execute.call_count, 1)
        connection.rollback.assert_called_once()

    def test_prerequisite_snapshot_cannot_cross_owned_schemas_or_production(self):
        database = "test_twocomms_ig_0123456789ab"
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        admin._migration_prerequisite_snapshot = (database, ("id", "slug", "parent_id"), ((7, "veteran-fund", None),))
        with mock.patch.object(admin, "_connect") as connect:
            for target in ("production", "test_twocomms_ig_abcdef012345"):
                with self.assertRaises(self.runner.GateError):
                    admin.restore_migration_prerequisites(target)
            with self.assertRaisesRegex(self.runner.GateError, "not gate-owned"):
                admin.capture_migration_prerequisites("production")
        connect.assert_not_called()

    def test_missing_original_prerequisite_fails_instead_of_inventing_category(self):
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.description = [("id",), ("slug",)]
        cursor.fetchone.return_value = None
        with mock.patch.object(admin, "_connect", return_value=connection):
            with self.assertRaisesRegex(self.runner.GateError, "original migration prerequisite"):
                admin.capture_migration_prerequisites("test_twocomms_ig_0123456789ab")
        self.assertEqual(cursor.execute.call_count, 1)

    def test_original_prerequisite_ancestry_rejects_missing_cycle_and_depth(self):
        for ancestors, message, bound in (
            ([(7, "veteran-fund", 3), None], "ancestor is missing", 16),
            ([(7, "veteran-fund", 3), (3, "news", 7)], "contains a cycle", 16),
            ([(7, "veteran-fund", 3), (3, "news", 1)], "depth bound", 2),
        ):
            admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
            connection = mock.MagicMock()
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.description = [("id",), ("slug",), ("parent_id",)]
            cursor.fetchone.side_effect = ancestors
            with mock.patch.object(admin, "_connect", return_value=connection), \
                 mock.patch.object(self.runner, "MAX_PREREQUISITE_CATEGORY_DEPTH", bound):
                with self.assertRaisesRegex(self.runner.GateError, message):
                    admin.capture_migration_prerequisites("test_twocomms_ig_0123456789ab")
            self.assertIsNone(admin._migration_prerequisite_snapshot)
            self.assertTrue(all(call.args[0].startswith("SELECT") for call in cursor.execute.call_args_list))

    def test_prerequisite_restore_preflights_every_identity_before_inserting_any_row(self):
        database = "test_twocomms_ig_0123456789ab"
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        admin._migration_prerequisite_snapshot = (
            database, ("id", "slug", "parent_id"), ((3, "news", None), (7, "veteran-fund", 3)),
        )
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [[], [(7, "conflicting-fixture", None)]]
        with mock.patch.object(admin, "_connect", return_value=connection):
            with self.assertRaisesRegex(self.runner.GateError, "identity conflicts"):
                admin.restore_migration_prerequisites(database)
        self.assertTrue(all(call.args[0].startswith("SELECT") for call in cursor.execute.call_args_list))
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_prerequisite_restore_does_not_overwrite_present_original_parent(self):
        database = "test_twocomms_ig_0123456789ab"
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        child = (7, "veteran-fund", 3)
        admin._migration_prerequisite_snapshot = (
            database, ("id", "slug", "parent_id"), ((3, "news", None), child),
        )
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [[(3, "news", None)], []]
        with mock.patch.object(admin, "_connect", return_value=connection):
            self.assertEqual(admin.restore_migration_prerequisites(database), "restored_original")
        inserts = [call for call in cursor.execute.call_args_list if call.args[0].startswith("INSERT")]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0].args[1], child)

    def test_safe_error_identity_never_renders_messages_rows_or_dynamic_names(self):
        error_type = type("IntegrityError", (Exception,), {})
        secret = "password=provider-secret customer=Olena row=(7,private)"
        error = error_type(1452, secret)
        rendered = self.runner._safe_error_identity(stage="restore_migration_prerequisites", error=error)
        self.assertEqual(rendered, "MariaDB gate exception: stage=restore_migration_prerequisites "
                         "class=IntegrityError database_errno=1452\n")
        dynamic = type("SensitiveCustomerError", (Exception,), {})(secret)
        rendered += self.runner._safe_error_identity(stage=secret, error=dynamic)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("SensitiveCustomerError", rendered)
        self.assertIn("stage=unknown class=unclassified_exception", rendered)
        self.assertNotIn("database_errno", self.runner._safe_error_identity(stage="test_suite", error=RuntimeError(1452)))

    def test_admin_error_diagnostic_keeps_primary_exception_and_cleanup_without_secrets(self):
        error_type = type("IntegrityError", (Exception,), {})
        primary = error_type(1452, "password=provider-secret customer=Olena private-row")
        admin = FakeAdmin()
        evidence = io.StringIO()
        with mock.patch.object(admin, "restore_migration_prerequisites", side_effect=primary):
            with self.assertRaises(self.runner.GateError) as caught:
                self.runner.run_gate(server_mode="external", admin=admin, command_runner=FakeCommandRunner(),
                    project_root=PROJECT_ROOT, environ={"MARIADB_ADMIN_PASSWORD": "root-secret"}, output=evidence)
        self.assertIs(caught.exception.primary_error, primary)
        self.assertIn("stage=restore_migration_prerequisites class=IntegrityError database_errno=1452", evidence.getvalue())
        for private in ("provider-secret", "root-secret", "Olena", "private-row"):
            self.assertNotIn(private, evidence.getvalue())
        self.assertEqual([call[0] for call in admin.calls[-4:]],
                         ["drop_user", "drop_database", "verify_cleanup", "close"])

    def test_command_runner_decode_error_is_safely_distinguished_from_test_failure(self):
        command = FakeCommandRunner()
        admin, evidence = FakeAdmin(), io.StringIO()
        def decode_failure(args, **kwargs):
            if "test" in args:
                raise UnicodeDecodeError("utf-8", b"\xffprivate-row", 0, 1, "provider-secret")
            return command(args, **kwargs)
        with self.assertRaises(self.runner.GateError):
            self.runner.run_gate(server_mode="external", admin=admin, command_runner=decode_failure,
                project_root=PROJECT_ROOT, environ={"MARIADB_ADMIN_PASSWORD": "root-secret"}, output=evidence)
        self.assertIn("stage=test_suite class=UnicodeDecodeError", evidence.getvalue())
        self.assertNotIn("provider-secret", evidence.getvalue())
        self.assertNotIn("private-row", evidence.getvalue())

    def test_gate_restoration_failure_still_cleans_generated_namespace(self):
        admin = FakeAdmin(fail_at="restore_migration_prerequisites")
        with self.assertRaises(self.runner.GateError):
            self.runner.run_gate(server_mode="external", admin=admin,
                command_runner=FakeCommandRunner(), project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"})
        self.assertEqual([call[0] for call in admin.calls[-4:]],
                         ["drop_user", "drop_database", "verify_cleanup", "close"])

    def test_manages_generated_schema_and_user_and_runs_from_project_root(self):
        admin = FakeAdmin()
        command = FakeCommandRunner()
        evidence = io.StringIO()

        result = self.runner.run_gate(
            server_mode="external",
            suite="lifecycle",
            admin=admin,
            command_runner=command,
            environ={
                "MARIADB_ADMIN_PASSWORD": "root-secret",
                "DB_NAME": "production-name",
                "DB_HOST": "production.database.example",
                "DB_PASSWORD": "production-password",
                "DB_PASSWORD_DTF": "production-dtf-password",
                "MANAGER_TG_BOT_TOKEN": "provider-secret",
                "META_ACCESS_TOKEN": "meta-secret",
                "PYTHONPATH": "/untrusted/import/path",
            },
            project_root=PROJECT_ROOT,
            output=evidence,
        )

        self.assertEqual(result["status"], "passed")
        created_db = next(call[1] for call in admin.calls if call[0] == "create_database")
        created_user = next(call[1] for call in admin.calls if call[0] == "create_user")
        self.assertRegex(created_db, r"^test_twocomms_ig_[a-f0-9]{12}$")
        self.assertRegex(created_user, r"^twc_ig_[a-f0-9]{12}$")
        self.assertEqual(created_db, next(call[2] for call in admin.calls if call[0] == "grant_schema"))
        self.assertEqual(created_user, next(call[1] for call in admin.calls if call[0] == "grant_schema"))
        self.assertEqual(
            [call[0] for call in admin.calls[-4:]],
            ["drop_user", "drop_database", "verify_cleanup", "close"],
        )
        self.assertTrue(command.calls)
        self.assertEqual(command.calls[0][0][2:5], ["migrate", "storefront", "0096"])
        self.assertEqual(command.calls[1][0][2:5], ["migrate", "reviews", "0001"])
        self.assertEqual(
            command.calls[2][0][1],
            str(PROJECT_ROOT / "twocomms" / "manage.py"),
        )
        self.assertTrue(Path(command.calls[2][0][1]).is_file())
        self.assertEqual(command.calls[2][1]["cwd"], str(PROJECT_ROOT / "twocomms"))
        self.assertEqual(command.calls[3][0][2:5], ["migrate", "reviews", "0001"])
        self.assertEqual(command.calls[4][0][2:5], ["migrate", "storefront", "0096"])
        self.assertEqual(command.calls[5][0][2], "migrate")
        self.assertEqual(command.calls[6][0][2], "check")
        self.assertIn("--database=default", command.calls[6][0])
        self.assertIn("--fail-level=WARNING", command.calls[6][0])
        child_env = command.calls[0][1]["env"]
        self.assertEqual(child_env["TEST_MARIADB_NAME"], created_db)
        self.assertNotIn("DB_NAME", child_env)
        self.assertNotIn("DB_PASSWORD", child_env)
        self.assertNotIn("DB_PASSWORD_DTF", child_env)
        self.assertNotIn("META_ACCESS_TOKEN", child_env)
        self.assertNotIn("PYTHONPATH", child_env)
        self.assertEqual(child_env["MANAGER_TG_BOT_TOKEN"], "")
        self.assertNotIn("TEST_MARIADB_REMOTE_ALLOWED", child_env)
        self.assertNotIn("root-secret", evidence.getvalue())
        self.assertNotIn("provider-secret", evidence.getvalue())
        self.assertIn("version=11.4.12-MariaDB", evidence.getvalue())
        self.assertIn(f"database={created_db}", evidence.getvalue())
        self.assertIn("cleanup=verified", evidence.getvalue())
        self.assertIn(
            "MariaDB database check: alias=default status=passed",
            evidence.getvalue(),
        )

    def test_child_commands_receive_owned_ephemeral_review_write_freeze_marker(self):
        command = MarkerInspectingCommandRunner()

        result = self.runner.run_gate(
            server_mode="external",
            suite="lifecycle",
            admin=FakeAdmin(),
            command_runner=command,
            environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            project_root=PROJECT_ROOT,
            output=io.StringIO(),
        )

        self.assertEqual(result["status"], "passed")
        self.assertTrue(command.marker_snapshots)
        self.assertNotIn(None, command.marker_snapshots)
        marker_paths = {snapshot["path"] for snapshot in command.marker_snapshots}
        self.assertEqual(len(marker_paths), 1)
        for snapshot in command.marker_snapshots:
            self.assertTrue(snapshot["absolute"])
            self.assertEqual(snapshot["mode"], 0o600)
            self.assertEqual(snapshot["uid"], os.geteuid())
            self.assertEqual(snapshot["content"], b"review-write-freeze-v1\n")
        self.assertFalse(next(iter(marker_paths)).exists())

    def test_review_write_freeze_marker_is_removed_after_child_failure(self):
        command = MarkerInspectingCommandRunner(
            returncode=1,
            fail_command="test",
        )

        with self.assertRaises(self.runner.GateError):
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=FakeAdmin(),
                command_runner=command,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
                project_root=PROJECT_ROOT,
                output=io.StringIO(),
            )

        self.assertTrue(command.marker_snapshots)
        self.assertNotIn(None, command.marker_snapshots)
        marker_paths = {snapshot["path"] for snapshot in command.marker_snapshots}
        self.assertEqual(len(marker_paths), 1)
        self.assertFalse(next(iter(marker_paths)).exists())


    def test_database_check_requires_zero_warnings(self):
        known = """WARNINGS:
reviews.ReviewVote: (models.W036) conditional unique unsupported
reviews.ReviewVote: (models.W036) conditional unique unsupported
storefront.ProductFitOption: (models.W036) conditional unique unsupported
storefront.WebPushDeviceSubscription.endpoint: (mysql.W003) long unique char
"""
        classified = self.runner.classify_database_check_warnings(known)
        self.assertEqual(classified["allowed_count"], 0)
        self.assertEqual(
            classified["blocked"],
            [
                "reviews.ReviewVote:models.W036",
                "storefront.ProductFitOption:models.W036",
                "storefront.WebPushDeviceSubscription.endpoint:mysql.W003",
            ],
        )
        self.assertEqual(classified["missing"], [])
        self.assertEqual(classified["policies"], {})

        clean = self.runner.classify_database_check_warnings("")
        self.assertEqual(clean["allowed_count"], 0)
        self.assertEqual(clean["blocked"], [])
        self.assertEqual(clean["missing"], [])
        self.assertEqual(clean["policies"], {})

        objectless = self.runner.classify_database_check_warnings(
            "?: (mysql.W002) strict mode warning\n"
        )
        self.assertEqual(objectless["blocked"], ["?:mysql.W002"])

    def test_objectless_warning_is_red_and_cleanup_is_verified(self):
        class ObjectlessWarningRunner(FakeCommandRunner):
            def __call__(self, args, **kwargs):
                self.calls.append((list(args), kwargs))
                stdout = (
                    "?: (mysql.W002) strict mode warning\n"
                    if len(args) > 2 and args[2] == "check"
                    else ""
                )
                return subprocess.CompletedProcess(args, 0, stdout, "")

        admin = FakeAdmin()
        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=ObjectlessWarningRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertEqual(
            str(raised.exception.primary_error),
            "database check warning policy failed",
        )
        self.assertEqual(
            [call[0] for call in admin.calls[-4:]],
            ["drop_user", "drop_database", "verify_cleanup", "close"],
        )

    def test_runner_requires_zero_database_warnings(self):
        class WarningRunner(FakeCommandRunner):
            def __call__(self, args, **kwargs):
                self.calls.append((list(args), kwargs))
                return subprocess.CompletedProcess(args, 0, "", "")

        evidence = io.StringIO()
        result = self.runner.run_gate(
            server_mode="external",
            suite="lifecycle",
            admin=FakeAdmin(),
            command_runner=WarningRunner(),
            project_root=PROJECT_ROOT,
            environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            output=evidence,
        )

        self.assertEqual(
            result["database_check"],
            "default:passed;allowed_warnings=0",
        )
        self.assertIn("allowed_warnings=0", evidence.getvalue())

    def test_success_verifies_release_schema_before_cleanup_and_emits_proof(self):
        admin = FakeAdmin()
        evidence = io.StringIO()

        self.runner.run_gate(
            server_mode="external",
            suite="lifecycle",
            admin=admin,
            command_runner=FakeCommandRunner(),
            project_root=PROJECT_ROOT,
            environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            output=evidence,
        )

        call_names = [call[0] for call in admin.calls]
        self.assertIn("verify_release_schema", call_names)
        self.assertLess(
            call_names.index("verify_release_schema"),
            call_names.index("drop_user"),
        )
        self.assertIn(
            "MariaDB schema proof: "
            "migration=management.0156_ig_order_event_delivery_receipts "
            "provider_message_id=varchar(255) "
            "delivery_provider_message_ids=longtext+json_valid\n",
            evidence.getvalue(),
        )
        self.assertIn(
            "MariaDB warning contract proof: "
            "engines=reviewvote_fit_innodb+push_myisam generated_columns=2_verified "
            "unique_indexes=3_new+1_preserved endpoint_unique=hash_preserved "
            "show_create_tables=verified\n",
            evidence.getvalue(),
        )

    def test_admin_stages_all_warning_tables_as_myisam_before_target_migrations(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        tables = (
            "reviews_reviewvote",
            "storefront_productfitoption",
            "storefront_webpushdevicesubscription",
        )
        statements = []
        with (
            mock.patch.object(admin, "_query_one", side_effect=[(0,), (0,)]),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[
                    [(table, "InnoDB") for table in tables],
                    [
                        ("reviews_reviewvote", "reviews_vote_review_fk"),
                        (
                            "storefront_pushnotificationdelivery",
                            "push_delivery_subscription_fk",
                        ),
                    ],
                    [],
                    [(table, "MyISAM") for table in tables],
                ],
            ),
            mock.patch.object(admin, "_sql", side_effect=statements.append),
        ):
            proof = admin.prepare_database_warning_contract(
                "test_twocomms_ig_0123456789ab"
            )

        self.assertEqual(proof, {"warning_contract_engines": "3_myisam"})
        self.assertEqual(len(statements), 5)
        self.assertTrue(
            all("DROP FOREIGN KEY" in statement for statement in statements[:2])
        )
        self.assertTrue(
            all("ENGINE=MyISAM" in statement for statement in statements[2:])
        )

    def test_admin_reverse_proof_preserves_endpoint_hash_and_removes_new_objects(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        tables = (
            "reviews_reviewvote",
            "storefront_productfitoption",
            "storefront_webpushdevicesubscription",
        )
        endpoint_index = [
            (
                "storefront_webpushdevicesubscription",
                "endpoint",
                "HASH",
                "endpoint",
            )
        ]
        with (
            mock.patch.object(admin, "_query_one", side_effect=[(0,), (0,)]),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[
                    [(table, "InnoDB" if table == "storefront_productfitoption" else "MyISAM")
                     for table in tables],
                    [],
                    endpoint_index,
                ],
            ),
        ):
            proof = admin.verify_database_warning_contract_reverse(
                "test_twocomms_ig_0123456789ab"
            )

        self.assertEqual(
            proof,
            {
                "warning_contract_reverse": (
                    "generated_removed+endpoint_hash_preserved"
                )
            },
        )

    def test_admin_warning_contract_proves_generated_columns_and_real_constraints(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        unique_rows = [
            ("reviews_reviewvote", "rev_vote_unique_user", "BTREE", "review_id,user_id"),
            ("reviews_reviewvote", "rev_vote_unique_anon", "BTREE", "review_id,anon_identity"),
            ("storefront_productfitoption", "fit_product_code_uniq", "BTREE", "product_id,code"),
            ("storefront_productfitoption", "uniq_default_fit_product", "BTREE", "default_product_identity"),
            ("storefront_webpushdevicesubscription", "endpoint", "HASH", "endpoint"),
        ]
        engine_rows = [
            ("reviews_reviewvote", "InnoDB"),
            ("storefront_productfitoption", "InnoDB"),
            ("storefront_webpushdevicesubscription", "MyISAM"),
        ]
        generated_rows = [
            (
                "reviews_reviewvote",
                "anon_identity",
                "STORED GENERATED",
                "case when `user_id` is null then `anon_key` else NULL end",
            ),
            (
                "storefront_productfitoption",
                "default_product_identity",
                "STORED GENERATED",
                "case when `is_default` = 1 then `product_id` else NULL end",
            ),
        ]
        create_rows = {
            "reviews_reviewvote": (
                "CREATE TABLE `reviews_reviewvote` ("
                "`anon_identity` varchar(64) GENERATED ALWAYS AS (case when `user_id` is null then `anon_key` else NULL end) STORED,"
                "UNIQUE KEY `rev_vote_unique_user` (`review_id`,`user_id`),"
                "UNIQUE KEY `rev_vote_unique_anon` (`review_id`,`anon_identity`),"
                "CONSTRAINT `rev_vote_user_or_anon_required` CHECK ((`user_id` is not null) or (`anon_key` <> ''))"
                ") ENGINE=InnoDB"
            ),
            "storefront_productfitoption": (
                "CREATE TABLE `storefront_productfitoption` ("
                "`default_product_identity` bigint GENERATED ALWAYS AS (case when `is_default` = 1 then `product_id` else NULL end) STORED,"
                "UNIQUE KEY `fit_product_code_uniq` (`product_id`,`code`),"
                "UNIQUE KEY `uniq_default_fit_product` (`default_product_identity`)"
                ") ENGINE=InnoDB"
            ),
            "storefront_webpushdevicesubscription": (
                "CREATE TABLE `storefront_webpushdevicesubscription` ("
                "`endpoint` varchar(1000) NOT NULL,"
                "UNIQUE KEY `endpoint` (`endpoint`) USING HASH"
                ") ENGINE=MyISAM"
            ),
        }
        query_one = mock.Mock(
            side_effect=[(0,), (0,), (0,), (0,), ("varchar", 1000)]
        )
        with (
            mock.patch.object(admin, "_query_one", query_one),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[engine_rows, unique_rows, generated_rows],
            ),
            mock.patch.object(
                admin,
                "_show_create_table",
                side_effect=lambda _database, table: create_rows[table],
            ),
        ):
            proof = admin.verify_database_warning_contract(
                "test_twocomms_ig_0123456789ab"
            )

        self.assertEqual(
            proof,
            {
                "engines": "reviewvote_fit_innodb+push_myisam",
                "generated_columns": "2_verified",
                "unique_indexes": "3_new+1_preserved",
                "endpoint_unique": "hash_preserved",
                "show_create_tables": "verified",
            },
        )
        executed_sql = "\n".join(
            call.args[0] for call in query_one.call_args_list
        )
        self.assertIn("GROUP BY `review_id`, `user_id`", executed_sql)
        self.assertIn("GROUP BY `review_id`, `anon_key`", executed_sql)
        self.assertIn("GROUP BY `product_id`", executed_sql)
        self.assertIn("GROUP BY `endpoint`", executed_sql)

    def test_admin_warning_contract_fails_closed_on_duplicate_rows(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        with mock.patch.object(admin, "_query_one", return_value=(1,)):
            with self.assertRaisesRegex(
                self.runner.GateError,
                "duplicate scan failed",
            ):
                admin.verify_database_warning_contract(
                    "test_twocomms_ig_0123456789ab"
                )

    def test_warning_contract_requires_review_and_fit_innodb_but_preserves_push_myisam(self):
        admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
        for rows in (
            [("reviews_reviewvote", "MyISAM"), ("storefront_productfitoption", "InnoDB"),
             ("storefront_webpushdevicesubscription", "MyISAM")],
            [("reviews_reviewvote", "InnoDB"), ("storefront_productfitoption", "MyISAM"),
             ("storefront_webpushdevicesubscription", "MyISAM")],
            [("reviews_reviewvote", "InnoDB"), ("storefront_productfitoption", "InnoDB"),
             ("storefront_webpushdevicesubscription", "InnoDB")],
        ):
            with mock.patch.object(admin, "_query_one", return_value=(0,)), \
                 mock.patch.object(admin, "_query_all", return_value=rows):
                with self.assertRaisesRegex(self.runner.GateError, "released transactional table policy"):
                    admin.verify_database_warning_contract("test_twocomms_ig_0123456789ab")

    def test_admin_warning_contract_fails_when_generated_constraints_are_missing(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        unique_rows = [
            ("reviews_reviewvote", "rev_vote_unique_per_user", "BTREE", "review_id,user_id"),
            ("storefront_productfitoption", "fit_product_code_uniq", "BTREE", "product_id,code"),
            ("storefront_webpushdevicesubscription", "endpoint", "HASH", "endpoint"),
        ]
        engine_rows = [
            ("reviews_reviewvote", "InnoDB"),
            ("storefront_productfitoption", "InnoDB"),
            ("storefront_webpushdevicesubscription", "MyISAM"),
        ]
        with (
            mock.patch.object(admin, "_query_one", side_effect=[(0,), (0,), (0,), (0,)]),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[engine_rows, unique_rows],
            ),
        ):
            with self.assertRaisesRegex(
                self.runner.GateError,
                "unsupported conditional constraint was created|generated unique constraint is missing",
            ):
                admin.verify_database_warning_contract(
                    "test_twocomms_ig_0123456789ab"
                )

    def test_release_schema_mismatch_is_red_and_still_cleans_namespace(self):
        admin = FakeAdmin(fail_at="verify_release_schema")
        evidence = io.StringIO()

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
                output=evidence,
            )

        self.assertEqual(
            str(raised.exception.primary_error),
            "release schema mismatch",
        )
        self.assertEqual(
            [call[0] for call in admin.calls[-4:]],
            ["drop_user", "drop_database", "verify_cleanup", "close"],
        )
        self.assertNotIn("MariaDB gate passed", evidence.getvalue())

    def test_admin_release_schema_accepts_mariadb_json_alias_metadata(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        with (
            mock.patch.object(admin, "_query_one", return_value=(1,)),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[
                    [
                        ("provider_message_id", "varchar", "varchar(255)", 255),
                        (
                            "delivery_provider_message_ids",
                            "longtext",
                            "longtext",
                            4294967295,
                        ),
                    ],
                    [("json_valid(`delivery_provider_message_ids`)",)],
                ],
            ),
        ):
            proof = admin.verify_release_schema(
                "test_twocomms_ig_0123456789ab"
            )

        self.assertEqual(
            proof["delivery_provider_message_ids"],
            "longtext+json_valid",
        )

    def test_admin_release_schema_rejects_non_positive_json_constraints(self):
        invalid_clauses = (
            "NOT JSON_VALID(`delivery_provider_message_ids`)",
            "JSON_VALID(`delivery_provider_message_ids`) = 0",
            "JSON_VALID(`delivery_provider_message_ids_backup`)",
            "'JSON_VALID(`delivery_provider_message_ids`)'",
        )

        for clause in invalid_clauses:
            with self.subTest(clause=clause):
                admin = self.runner.AdminClient(
                    host="127.0.0.1",
                    port="3306",
                    user="root",
                    password="",
                )
                with (
                    mock.patch.object(admin, "_query_one", return_value=(1,)),
                    mock.patch.object(
                        admin,
                        "_query_all",
                        side_effect=[
                            [
                                (
                                    "provider_message_id",
                                    "varchar",
                                    "varchar(255)",
                                    255,
                                ),
                                (
                                    "delivery_provider_message_ids",
                                    "longtext",
                                    "longtext",
                                    4294967295,
                                ),
                            ],
                            [(clause,)],
                        ],
                    ),
                ):
                    with self.assertRaises(self.runner.GateError):
                        admin.verify_release_schema(
                            "test_twocomms_ig_0123456789ab"
                        )

    def test_follow_ugc_delivery_contract_matches_latest_migration_state(self):
        environment = {
            name: value for name, value in os.environ.items()
            if not name.startswith("DB_") and name != "DATABASE_URL"
        }
        environment.update({
            "DJANGO_SETTINGS_MODULE": "test_settings_migrations_non_dtf",
            "PYTHONPATH": str(PROJECT_ROOT / "twocomms"),
            "SECRET_KEY": "migration-state-contract-only",
        })
        statement = """
import json, django
django.setup()
from django.db.migrations.loader import MigrationLoader
loader = MigrationLoader(None)
state = loader.project_state()
model = state.apps.get_model('management', 'IgUgcRewardDelivery')
print(json.dumps({
    'reward_unique': model._meta.get_field('reward').unique,
    'columns': [model._meta.get_field(name).column for constraint in model._meta.constraints
                if constraint.name == 'ig_ugc_delivery_generation' for name in constraint.fields],
    'has_0227': ('management', '0227_ugc_review_delivery_generations') in loader.graph.nodes,
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", statement], cwd=PROJECT_ROOT / "twocomms",
            env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(state["has_0227"])
        self.assertFalse(state["reward_unique"])
        self.assertEqual(state["columns"], ["reward_id", "generation"])
        self.assertEqual(
            self.runner._FOLLOW_UGC_UNIQUE_COLUMNS["management_igugcrewarddelivery"],
            {tuple(state["columns"])},
        )

    def test_follow_ugc_delivery_rejects_missing_or_wrong_unique_contract(self):
        table = "management_igugcrewarddelivery"
        for label, delivery_rows in (
            ("missing", []),
            ("old_reward_only", [(table, "old_reward_unique", "reward_id")]),
            ("generation_only", [(table, "wrong_unique", "generation")]),
            ("reordered", [(table, "wrong_order", "generation,reward_id")]),
            ("non_unique_only", []),
        ):
            with self.subTest(label=label):
                admin = self.runner.AdminClient(host="127.0.0.1", port="3306", user="root", password="")
                unique_rows = [
                    (other, "test_unique", ",".join(columns))
                    for other, columns_sets in self.runner._FOLLOW_UGC_UNIQUE_COLUMNS.items()
                    if other != table for columns in columns_sets
                ] + delivery_rows
                with (
                    mock.patch.object(admin, "_query_one", side_effect=[(1,), (1,)]),
                    mock.patch.object(admin, "_query_all", side_effect=[
                        [(name, "InnoDB") for name in self.runner._FOLLOW_UGC_TABLES], unique_rows,
                    ]) as query,
                    self.assertRaisesRegex(self.runner.GateError,
                        "table=management_igugcrewarddelivery expected_columns=reward_id,generation"),
                ):
                    admin.verify_follow_ugc_schema("test_twocomms_ig_0123456789ab")
                self.assertIn("NON_UNIQUE = 0", query.call_args_list[1].args[0])

    def test_admin_follow_ugc_schema_proves_engines_indexes_and_lifecycle(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        engine_rows = [(table, "InnoDB") for table in self.runner._FOLLOW_UGC_TABLES]
        unique_rows = []
        for table, column_sets in self.runner._FOLLOW_UGC_UNIQUE_COLUMNS.items():
            for index, columns in enumerate(sorted(column_sets)):
                unique_rows.append((table, f"test_unique_{index}", ",".join(columns)))
        lifecycle_columns = [
            ("lifecycle_state", "varchar", "varchar(16)", 16, "NO"),
            ("lifecycle_reason", "varchar", "varchar(64)", 64, "NO"),
            ("lifecycle_updated_at", "datetime", "datetime(6)", None, "NO"),
        ]
        lifecycle_indexes = [
            ("test_lifecycle_state", "lifecycle_state"),
            ("test_lifecycle_updated", "lifecycle_updated_at"),
        ]
        lifecycle_job_indexes = [
            ("test_job_order", "order_id"),
            ("test_job_client", "client_id"),
            ("test_job_due_at", "due_at"),
            ("test_job_created", "created_at"),
            ("ig_ugc_life_job_due", "due_at,id"),
        ]
        lifecycle_job_checks = [
            ("ig_ugc_life_job_target", "order_id is not null or client_id is not null"),
        ]
        with (
            mock.patch.object(admin, "_query_one", side_effect=[(1,), (1,)]),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[
                    engine_rows,
                    unique_rows,
                    [],
                    lifecycle_columns,
                    lifecycle_indexes,
                    lifecycle_job_indexes,
                    lifecycle_job_checks,
                ],
            ),
        ):
            proof = admin.verify_follow_ugc_schema(
                "test_twocomms_ig_0123456789ab"
            )

        self.assertEqual(
            proof,
            {
                "follow_ugc_migration": "management.0166_ig_ugc_reward_lifecycle",
                "guest_promo_migration": "storefront.0095_promocode_guest_ugc",
                "follow_ugc_tables": "12_innodb",
                "follow_ugc_unique_indexes": "verified",
                "follow_ugc_foreign_keys": "orm_only",
                "follow_ugc_lifecycle": "3_columns+2_indexes",
                "follow_ugc_lifecycle_job": "target_check+5_indexes",
            },
        )

    def test_follow_ugc_schema_proves_the_lifecycle_job_table_engine(self):
        self.assertIn(
            "management_igugcrewardlifecyclejob",
            self.runner._FOLLOW_UGC_TABLES,
        )

    def test_admin_follow_ugc_schema_rejects_a_missing_lifecycle_index(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        engine_rows = [(table, "InnoDB") for table in self.runner._FOLLOW_UGC_TABLES]
        unique_rows = [
            (table, f"test_unique_{index}", ",".join(columns))
            for table, column_sets in self.runner._FOLLOW_UGC_UNIQUE_COLUMNS.items()
            for index, columns in enumerate(sorted(column_sets))
        ]
        lifecycle_columns = [
            ("lifecycle_state", "varchar", "varchar(16)", 16, "NO"),
            ("lifecycle_reason", "varchar", "varchar(64)", 64, "NO"),
            ("lifecycle_updated_at", "datetime", "datetime(6)", None, "NO"),
        ]
        with (
            mock.patch.object(admin, "_query_one", side_effect=[(1,), (1,)]),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[
                    engine_rows,
                    unique_rows,
                    [],
                    lifecycle_columns,
                    [("test_lifecycle_state", "lifecycle_state")],
                ],
            ),
        ):
            with self.assertRaisesRegex(
                self.runner.GateError,
                "lifecycle index is missing",
            ):
                admin.verify_follow_ugc_schema(
                    "test_twocomms_ig_0123456789ab"
                )

    def test_admin_follow_ugc_schema_rejects_a_missing_lifecycle_job_index(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        engine_rows = [(table, "InnoDB") for table in self.runner._FOLLOW_UGC_TABLES]
        unique_rows = [
            (table, f"test_unique_{index}", ",".join(columns))
            for table, column_sets in self.runner._FOLLOW_UGC_UNIQUE_COLUMNS.items()
            for index, columns in enumerate(sorted(column_sets))
        ]
        lifecycle_columns = [
            ("lifecycle_state", "varchar", "varchar(16)", 16, "NO"),
            ("lifecycle_reason", "varchar", "varchar(64)", 64, "NO"),
            ("lifecycle_updated_at", "datetime", "datetime(6)", None, "NO"),
        ]
        lifecycle_indexes = [
            ("test_lifecycle_state", "lifecycle_state"),
            ("test_lifecycle_updated", "lifecycle_updated_at"),
        ]
        with (
            mock.patch.object(admin, "_query_one", side_effect=[(1,), (1,)]),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[
                    engine_rows,
                    unique_rows,
                    [],
                    lifecycle_columns,
                    lifecycle_indexes,
                    [("ig_ugc_life_job_due", "due_at,id")],
                    [("ig_ugc_life_job_target", "order_id is not null or client_id is not null")],
                ],
            ),
        ):
            with self.assertRaisesRegex(
                self.runner.GateError,
                "lifecycle-job index is missing",
            ):
                admin.verify_follow_ugc_schema(
                    "test_twocomms_ig_0123456789ab"
                )

    def test_admin_follow_ugc_schema_rejects_missing_lifecycle_job_target_check(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1",
            port="3306",
            user="root",
            password="",
        )
        engine_rows = [(table, "InnoDB") for table in self.runner._FOLLOW_UGC_TABLES]
        unique_rows = [
            (table, f"test_unique_{index}", ",".join(columns))
            for table, column_sets in self.runner._FOLLOW_UGC_UNIQUE_COLUMNS.items()
            for index, columns in enumerate(sorted(column_sets))
        ]
        lifecycle_columns = [
            ("lifecycle_state", "varchar", "varchar(16)", 16, "NO"),
            ("lifecycle_reason", "varchar", "varchar(64)", 64, "NO"),
            ("lifecycle_updated_at", "datetime", "datetime(6)", None, "NO"),
        ]
        lifecycle_indexes = [
            ("test_lifecycle_state", "lifecycle_state"),
            ("test_lifecycle_updated", "lifecycle_updated_at"),
        ]
        lifecycle_job_indexes = [
            ("test_job_order", "order_id"),
            ("test_job_client", "client_id"),
            ("test_job_due_at", "due_at"),
            ("test_job_created", "created_at"),
            ("ig_ugc_life_job_due", "due_at,id"),
        ]
        with (
            mock.patch.object(admin, "_query_one", side_effect=[(1,), (1,)]),
            mock.patch.object(
                admin,
                "_query_all",
                side_effect=[
                    engine_rows,
                    unique_rows,
                    [],
                    lifecycle_columns,
                    lifecycle_indexes,
                    lifecycle_job_indexes,
                    [],
                ],
            ),
        ):
            with self.assertRaisesRegex(
                self.runner.GateError,
                "lifecycle-job target check is missing",
            ):
                admin.verify_follow_ugc_schema(
                    "test_twocomms_ig_0123456789ab"
                )

    def test_failure_still_cleans_schema_and_user(self):
        admin = FakeAdmin()
        command = FakeCommandRunner(returncode=1, fail_command="test")

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=command,
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertEqual(str(raised.exception.primary_error), "lifecycle command failed (1)")
        self.assertEqual(
            [call[0] for call in admin.calls][-4:],
            ["drop_user", "drop_database", "verify_cleanup", "close"],
        )

    def test_failure_emits_bounded_sanitized_django_test_summary(self):
        admin = FakeAdmin()
        evidence = io.StringIO()
        secret = "super-secret-token"
        command = FakeCommandRunner(
            returncode=1,
            fail_command="test",
            stdout="customer payload must stay hidden\n",
            stderr=(
                "Creating test database for alias 'default'...\n"
                "ERROR: test_checkout (management.tests.CheckoutTests.test_checkout)\n"
                "Traceback (most recent call last):\n"
                '  File "/workspace/management/tests.py", line 10, in test_checkout\n'
                "RuntimeError: buyer@example.com +380501112233 "
                f"token={secret} Private customer note\n"
                "pymysql.err.OperationalError: (1213, 'buyer@example.com "
                f"token={secret} private database detail')\n"
                "Ran 1 test in 2.345s\n"
                "FAILED (errors=1)\n"
                + "ignored diagnostic noise\n" * 1000
            ),
        )

        with self.assertRaises(self.runner.GateError):
            self.runner.run_gate(
                server_mode="external",
                suite="checkout-concurrency",
                admin=admin,
                command_runner=command,
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
                output=evidence,
            )

        summary = evidence.getvalue()
        self.assertIn(
            "MariaDB gate child failed: suite=checkout-concurrency exit=1",
            summary,
        )
        self.assertIn("child_output: stdout=present stderr=present", summary)
        self.assertIn("traceback=yes", summary)
        self.assertIn("ERROR: test_failed", summary)
        self.assertIn("exception:", summary)
        self.assertIn("exception_kind: runtime", summary)
        self.assertIn("database_error: errno=1213", summary)
        self.assertIn("Ran 1 test in 2.345s", summary)
        self.assertIn("FAILED (errors=1)", summary)
        self.assertNotIn("Traceback", summary)
        self.assertNotIn("/workspace/management/tests.py", summary)
        self.assertNotIn("customer payload", summary)
        self.assertNotIn("buyer@example.com", summary)
        self.assertNotIn("+380501112233", summary)
        self.assertNotIn(secret, summary)
        self.assertNotIn("Private customer note", summary)
        self.assertLessEqual(len(summary), self.runner.MAX_FAILURE_SUMMARY_CHARS)

    def test_failure_summary_never_retains_free_form_test_or_subtest_details(self):
        completed = subprocess.CompletedProcess(
            args=["python", "manage.py", "test"],
            returncode=1,
            stdout="",
            stderr=(
                "ERROR: checkout failed for Olena at 12 Shevchenka Street, "
                "order UUID deadbeef-dead-beef-dead-beefdeadbeef, "
                "note=do not publish\n"
                "FAIL: test_checkout "
                "(management.tests.CheckoutTests.test_checkout) "
                "(customer='Olena', address='12 Shevchenka Street', "
                "order_id='deadbeef-dead-beef-dead-beefdeadbeef', "
                "note='do not publish')\n"
                "Ran 2 tests in 0.123s\n"
                "FAILED (failures=1, errors=1)\n"
                "FAILED (failures=1, customer='Olena', note='do not publish')\n"
            ),
        )

        summary = self.runner._failure_summary(
            suite="checkout-concurrency",
            completed=completed,
        )

        self.assertIn("ERROR: test_failed", summary)
        self.assertIn("FAIL: test_failed", summary)
        self.assertIn("FAILED (failures=1, errors=1)", summary)
        for private_detail in (
            "Olena",
            "12 Shevchenka Street",
            "deadbeef-dead-beef-dead-beefdeadbeef",
            "do not publish",
            "customer=",
            "address=",
            "order_id=",
            "note=",
        ):
            self.assertNotIn(private_detail, summary)

    def test_failure_summary_reads_sanitized_markers_from_stdout(self):
        completed = subprocess.CompletedProcess(
            args=["python", "manage.py", "test"],
            returncode=1,
            stdout=(
                "pymysql.err.OperationalError: (1205, 'private detail')\n"
                "Ran 1 test in 0.123s\n"
                "FAILED (errors=1)\n"
            ),
            stderr="",
        )

        summary = self.runner._failure_summary(
            suite="lifecycle",
            completed=completed,
        )

        self.assertIn("database_error: errno=1205", summary)
        self.assertIn("Ran 1 test in 0.123s", summary)
        self.assertIn("FAILED (errors=1)", summary)
        self.assertNotIn("private detail", summary)

    def test_failure_summary_never_retains_free_form_result_details(self):
        completed = subprocess.CompletedProcess(
            args=["python", "manage.py", "test"],
            returncode=1,
            stdout="",
            stderr=(
                "FAILED (customer='Olena', address='12 Shevchenka Street', "
                "order_id='deadbeef-dead-beef-dead-beefdeadbeef')\n"
            ),
        )

        summary = self.runner._failure_summary(
            suite="checkout-concurrency",
            completed=completed,
        )

        self.assertIn("FAILED (test_failed)", summary)
        for private_detail in (
            "Olena",
            "12 Shevchenka Street",
            "deadbeef-dead-beef-dead-beefdeadbeef",
            "customer=",
            "address=",
            "order_id=",
        ):
            self.assertNotIn(private_detail, summary)

    def test_failure_summary_replaces_dynamic_exception_type_names(self):
        customer_error = type("SensitiveCustomerError", (Exception,), {})
        database_error = type("SensitiveOrderError", (Exception,), {})
        completed = subprocess.CompletedProcess(
            args=["python", "manage.py", "test"],
            returncode=1,
            stdout="",
            stderr=(
                f"{customer_error.__name__}: customer=Olena\n"
                f"myapp.{database_error.__name__}: (1213, 'order=deadbeef')\n"
            ),
        )

        summary = self.runner._failure_summary(
            suite="checkout-concurrency",
            completed=completed,
        )

        self.assertIn("exception:", summary)
        self.assertIn("database_error: errno=1213", summary)
        self.assertNotIn(customer_error.__name__, summary)
        self.assertNotIn(database_error.__name__, summary)
        self.assertNotIn("Olena", summary)
        self.assertNotIn("deadbeef", summary)

    def test_main_replaces_unexpected_dynamic_exception_type_name(self):
        unexpected_error = type("SensitiveFallbackError", (Exception,), {})
        stderr = io.StringIO()

        with (
            mock.patch.object(
                self.runner,
                "run_gate",
                side_effect=unexpected_error("customer=Olena"),
            ),
            mock.patch.object(self.runner.sys, "stderr", stderr),
        ):
            self.assertEqual(self.runner.main(["--server-mode", "external"]), 1)

        rendered = stderr.getvalue()
        self.assertEqual(rendered, "MariaDB gate failed: unexpected_error\n")
        self.assertNotIn(unexpected_error.__name__, rendered)
        self.assertNotIn("Olena", rendered)

    def test_cleanup_failure_is_red_without_hiding_primary_error(self):
        admin = FakeAdmin(fail_cleanup=True)
        command = FakeCommandRunner(returncode=1, fail_command="test")

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=command,
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertEqual(str(raised.exception.primary_error), "lifecycle command failed (1)")
        self.assertEqual(str(raised.exception.cleanup_error), "drop user failed")
        self.assertEqual(len(raised.exception.cleanup_errors), 1)
        self.assertNotIn("MariaDB gate passed", raised.exception.args[0])

    def test_schema_cleanup_is_attempted_when_user_cleanup_fails(self):
        admin = FakeAdmin(fail_cleanup=True)

        with self.assertRaises(self.runner.GateError):
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertEqual(
            [call[0] for call in admin.calls][-4:],
            ["drop_user", "drop_database", "verify_cleanup", "close"],
        )

    def test_all_cleanup_failures_are_retained_with_the_primary_error(self):
        admin = FakeAdmin(fail_cleanup=True, fail_database_cleanup=True)

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(returncode=1, fail_command="test"),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertEqual(str(raised.exception.primary_error), "lifecycle command failed (1)")
        self.assertEqual(
            [str(error) for error in raised.exception.cleanup_errors],
            ["drop user failed", "drop database failed"],
        )

    def test_cleanup_error_summary_is_allowlisted_and_does_not_leak_driver_details(self):
        admin = FakeAdmin(fail_cleanup=True)
        cleanup_error = type("SensitiveCustomerCleanupError", (Exception,), {})
        admin.drop_user = mock.Mock(
            side_effect=cleanup_error("access denied for user='gate' password=top-secret")
        )
        evidence = io.StringIO()

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
                output=evidence,
            )

        rendered = str(raised.exception)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("access denied", rendered)
        self.assertIn("cleanup_error=exception", rendered)
        self.assertNotIn(cleanup_error.__name__, rendered)

    def test_partial_provisioning_still_attempts_idempotent_cleanup(self):
        admin = FakeAdmin(fail_at="create_user")

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertEqual(str(raised.exception.primary_error), "create user failed")
        self.assertEqual(
            [call[0] for call in admin.calls[-4:]],
            ["drop_user", "drop_database", "verify_cleanup", "close"],
        )

    def test_existing_generated_database_is_never_dropped(self):
        admin = FakeAdmin(existing=(True, False))

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertIn("database already exists", str(raised.exception))
        self.assertFalse(any(call[0].startswith("create_") for call in admin.calls))
        self.assertFalse(any(call[0].startswith("drop_") for call in admin.calls))

    def test_existing_generated_user_is_never_dropped(self):
        admin = FakeAdmin(existing=(False, True))

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertIn("user already exists", str(raised.exception))
        self.assertFalse(any(call[0].startswith("create_") for call in admin.calls))
        self.assertFalse(any(call[0].startswith("drop_") for call in admin.calls))

    def test_remote_external_host_requires_explicit_opt_in_before_provisioning(self):
        admin = FakeAdmin(host="mariadb.example.test")

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertIn("TEST_MARIADB_REMOTE_ALLOWED=1", str(raised.exception))
        self.assertFalse(any(call[0].startswith("create_") for call in admin.calls))

    def test_explicit_remote_opt_in_is_preserved_for_the_django_child(self):
        admin = FakeAdmin(host="mariadb.example.test")
        command = FakeCommandRunner()

        self.runner.run_gate(
            server_mode="external",
            suite="lifecycle",
            admin=admin,
            command_runner=command,
            project_root=PROJECT_ROOT,
            environ={
                "MARIADB_ADMIN_PASSWORD": "root-secret",
                "TEST_MARIADB_REMOTE_ALLOWED": "1",
            },
        )

        self.assertEqual(
            command.calls[0][1]["env"]["TEST_MARIADB_REMOTE_ALLOWED"],
            "1",
        )

    def test_admin_namespace_checks_cover_every_mariadb_user_host(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1", port="3306", user="root", password="secret"
        )
        queries = []

        def query(statement):
            queries.append(statement)
            return (0,)

        admin._query_one = query
        admin.ensure_namespace_absent("test_twocomms_ig_collision", "twc_ig_collision")

        self.assertIn("WHERE User = 'twc_ig_collision'", queries[1])
        self.assertNotIn("Host =", queries[1])

    def test_admin_cleanup_drops_only_the_gate_owned_wildcard_account(self):
        admin = self.runner.AdminClient(
            host="127.0.0.1", port="3306", user="root", password="secret"
        )
        statements = []
        admin._sql = statements.append

        admin.drop_user("twc_ig_owned")

        self.assertEqual(
            statements,
            ["DROP USER IF EXISTS 'twc_ig_owned'@'%'"],
        )

    def test_native_start_preserves_startup_error_when_cleanup_also_fails(self):
        native = self.runner.NativeMariaDb(
            binaries={"mariadbd": "/missing/mariadbd", "mariadb-install-db": "/missing/install"},
            command_runner=FakeCommandRunner(returncode=1),
            project_root=PROJECT_ROOT,
        )
        with mock.patch.object(native, "close", side_effect=RuntimeError("cleanup failed")):
            with self.assertRaises(self.runner.GateError) as raised:
                native.start()

        self.assertIn("native MariaDB initialization failed", str(raised.exception.primary_error))
        self.assertEqual(str(raised.exception.cleanup_error), "cleanup failed")

    def test_refuses_a_configured_production_host_even_with_remote_opt_in(self):
        admin = FakeAdmin(host="production.database.example")

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="lifecycle",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={
                    "MARIADB_ADMIN_PASSWORD": "root-secret",
                    "TEST_MARIADB_REMOTE_ALLOWED": "1",
                    "DB_NAME": "qlknpodo_MySQL_DB",
                    "DB_HOST": "production.database.example",
                },
            )

        self.assertIn("configured production database host", str(raised.exception))
        self.assertFalse(any(call[0].startswith("create_") for call in admin.calls))

    def test_rejects_wrong_engine_or_version_before_provisioning(self):
        identities = (
            ("9.4.0", "MySQL Community Server"),
            ("11.3.2-MariaDB", "mariadb.org binary distribution"),
        )
        for identity in identities:
            with self.subTest(identity=identity):
                admin = FakeAdmin(identity=identity)
                with self.assertRaises(self.runner.GateError) as raised:
                    self.runner.run_gate(
                        server_mode="external",
                        suite="lifecycle",
                        admin=admin,
                        command_runner=FakeCommandRunner(),
                        project_root=PROJECT_ROOT,
                        environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
                    )
                self.assertIn("MariaDB 11.4", str(raised.exception))
                self.assertFalse(any(call[0].startswith("create_") for call in admin.calls))

    def test_task_6b_advertises_the_narrow_checkout_concurrency_suite(self):
        self.assertEqual(self.runner.DEFAULT_SUITE, "lifecycle")
        self.assertEqual(
            self.runner.SUITES["lifecycle"],
            (
                "management.tests_ig_mariadb_lifecycle",
                "storefront.tests.test_mariadb_constraints."
                "WebPushEndpointUniquenessTests",
            ),
        )
        self.assertEqual(
            self.runner.SUITES["checkout-concurrency"],
            (
                "management.tests_ig_checkout_models."
                "IgCheckoutProposalConcurrencyTests."
                "test_concurrent_replacement_creation_serializes_on_deal",
            ),
        )
        with self.assertRaises(self.runner.GateError):
            self.runner._validate_suite("full")

    def test_task_15_advertises_the_follow_ugc_concurrency_suite(self):
        self.assertEqual(
            self.runner.SUITES["follow-ugc-concurrency"],
            ("management.tests_ig_mariadb_follow_ugc",),
        )

    def test_advertises_the_narrow_mariadb_lease_concurrency_suite(self):
        self.assertEqual(
            self.runner.SUITES["lease-concurrency"],
            ("management.tests_ig_daemon_progress.MariaDbLeaseContractTests",),
        )

    def test_legacy_conversion_coverage_is_preserved_when_dialogue_work_moves(self):
        self.assertEqual(
            self.runner.SUITES["conversion-context"],
            (
                "management.tests_ig_manu_catalog_locale_20261021",
                "management.tests_ig_manu_catalog_poll_20261021",
                "management.tests_ig_manu_customer_pool_mariadb_20261021",
                "management.tests_ig_manu_purpose_poll_20261021",
                "management.tests_ig_manu_console_faults_20261021",
                "management.tests_ig_manu_customer_pool_20261021",
                "management.tests_ig_manu_deadline_successor_20261021",
                "management.tests_ig_manu_queue_fairness_20261021",
                "management.tests_ig_manu_recruitment_20261021",
                "management.tests_ig_operator_reply_drafts",
                "management.tests_ig_manu_context_budget_20261021",
                "management.tests_ig_manu_business_delivery_20261021",
                "management.tests_ig_manu_deadline_20261021",
                "management.tests_ig_manu_deadline_policy_20261021",
                "management.tests_ig_manu_finalization_faults_20261021",
                "management.tests_ig_manu_language_20261021",
                "management.tests_ig_manu_liveness_20261021",
                "management.tests_ig_manu_manager_wait_20261021",
                "management.tests_ig_manu_recipient_20261021",
                "management.tests_ig_manu_timing_authority_20261021",
                "management.tests_ig_conversion_mariadb",
                "finance.tests_migration_storage",
                "management.tests_ig_rate_handoff_qa_20261020",
                "management.tests_ig_ingress_reply_liveness",
                "management.tests_ig_opaque_inbound",
                "management.tests_ig_unanswered_obligations",
                "management.tests_ig_checkout_journey_qa_20261020",
                "management.tests_ig_handoff_journey_qa_20261020",
                "management.tests_ig_knowledge_journey_qa_20261020",
                "management.tests_ig_policy_fault_ack",
                "management.tests_ig_fulfillment_stock_api.FulfillmentStockApiTests.test_native_receipt_rewrite_and_delete_are_denied_and_privacy_purge_preserves_inventory",
                "management.tests_ig_restock_delivery.NativeRestockDeliveryTests.test_ninety_day_subscription_expiry_cannot_be_extended_by_runtime_cache",
            ),
        )

    def test_dialogue_quality_contains_all_new_dialogue_and_planned_audit_modules(self):
        modules = self.runner.SUITES["dialogue-quality"]
        expected = {
            "management.tests_ig_analysis_lane_isolation_20261023",
            "management.tests_ig_analysis_lane",
            "management.tests_ig_worker_progress",
            "management.tests_ig_daemon",
            "management.tests_ig_daemon_health",
            "management.tests_ig_payment_observation_worker",
            "management.tests_ig_memory_lifecycle.MemoryDaemonOwnershipTests",
            "management.tests_ig_communication_policy_20261022",
            "management.tests_ig_conversation_activity",
            "management.tests_ig_conversion_audit_analysis_20261022",
            "management.tests_ig_conversion_audit_schema_20261022",
            "management.tests_ig_conversion_audit_prompt_20261023",
            "management.tests_ig_conversion_audit_stats_20261022",
            "management.tests_ig_product_weight_20261022",
            "management.tests_ig_live_burst_fairness_20261022",
            "management.tests_ig_live_conversation_contract_20261022",
            "management.tests_ig_live_identity_echo_20261022",
            "management.tests_ig_manager_case_continuity_20261022",
            "management.tests_ig_reply_truth_seams_20261022",
            "management.tests_ig_response_guard",
            "management.tests_ig_media_response",
            "management.tests_bot_catalog",
            "management.tests_ig_size_aliases_20261022",
            "management.tests_ig_size_grid_authority_20261022",
            "management.tests_ig_standard_funnel_20261022",
            "storefront.tests.test_size_guide_authority_20261022",
        }
        self.assertEqual(set(modules), expected)
        self.assertEqual(len(modules), len(expected))
        self.assertEqual(self.runner._validate_suite("dialogue-quality"), modules)
        discovered = {
            "management." + path.stem
            for path in (PROJECT_ROOT / "twocomms/management").glob("tests_ig_*20261022.py")
        }
        self.assertFalse(discovered - expected)

    def test_dialogue_quality_runs_the_complete_native_command_and_cleanup_contract(self):
        self.assertEqual(self.runner._validate_suite("selection-idempotence"),
                         ("management.tests_ig_revision_selection_noop_20261023",))
        for suite in ("dialogue-quality", "selection-idempotence"):
            with self.subTest(suite=suite):
                admin = FakeAdmin()
                command = FakeCommandRunner()
                result = self.runner.run_gate(
                    server_mode="external", suite=suite, admin=admin,
                    command_runner=command, project_root=PROJECT_ROOT,
                    environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
                    output=io.StringIO(),
                )
                test_calls = [args for args, _kwargs in command.calls if args[2] == "test"]
                self.assertEqual(test_calls, [[
                    sys.executable, str(PROJECT_ROOT / "twocomms/manage.py"), "test",
                    *self.runner.SUITES[suite],
                    "--settings=test_settings_mariadb", "--noinput", "--keepdb",
                ]])
                self.assertEqual(result["suite"], suite)
                self.assertEqual([call[0] for call in admin.calls][-4:],
                                 ["drop_user", "drop_database", "verify_cleanup", "close"])

    def test_stock_authority_covers_current_rules_and_bounded_existing_consumers(self):
        labels = (self.runner._validate_suite("stock-authority")
                  + self.runner._validate_suite("stock-authority-consumers"))
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(len(self.runner.STOCK_AUTHORITY_ACCEPTANCE_LABELS), 47)
        self.assertEqual(set(labels), set(self.runner.STOCK_AUTHORITY_ACCEPTANCE_LABELS))
        self.assertEqual(len(self.runner.STOCK_AUTHORITY_CORE_MODULES), 9)
        self.assertEqual(set(self.runner.SUITES["stock-authority"]),
                         self.runner.STOCK_AUTHORITY_CORE_MODULES)
        for suffix in ("authority_20261023", "catalog_copy_20261023", "claim_guard_20261023",
                       "request_scope_20261023"):
            self.assertIn("management.tests_ig_stock_" + suffix, labels)
        self.assertIn("management.tests_ig_palette_intent_20261023", labels)
        self.assertIn("management.tests_ig_source_cart_catalog", labels)
        self.assertIn("management.tests_ig_standard_funnel_qa", labels)
        for module in ("fulfillment_reviews", "fulfillment_timing", "fulfillment_truth",
                       "fulfillment_delivery", "fulfillment_checkout", "fulfillment_stock_api",
                       "fulfillment_requirements", "restock_waiting_list_api", "restock_context",
                       "restock_waiting_list_context", "restock_consent_boundary", "restock_subscriptions"):
            self.assertIn("management.tests_ig_" + module, labels)
        for boundary in (
            "fulfillment_reuse.NativeInvitationReviewReuseTests.test_quantity_amendment_cannot_borrow_original_checked_quantity",
            "fulfillment_reuse.NativeInvitationReviewReuseTests.test_privacy_erasure_cannot_authorize_reuse",
            "fulfillment_stock_sync.NativeFulfillmentStockSyncTests.test_real_preview_is_readonly_and_no_dialogue_decision_changes_stock",
            "fulfillment_stock_sync.NativeFulfillmentStockSyncTests.test_same_operation_replays_receipt_without_adding_capacity_or_another_checkout",
            "fulfillment_stock_sync.NativeFulfillmentStockSyncTests.test_two_real_customer_recounts_of_one_unit_have_one_checkout_winner",
            "fulfillment_integration.FulfillmentNativeIntegrationTests.test_api_permission_foreign_case_csrf_and_digest_cas_do_not_write_decisions",
            "restock_waiting_list.NativeRestockWaitingListTests.test_actual_all_checked_rows_send_once_without_creating_stock_reservation_or_order",
            "restock_waiting_list.NativeRestockWaitingListTests.test_source_edit_invalidates_accepted_list_without_a_provider_call",
            "restock_waiting_list.NativeRestockWaitingListTests.test_expired_physical_manager_check_is_rechecked_at_actual_socket_boundary",
        ):
            self.assertIn("management.tests_ig_" + boundary, labels)

        for label in labels:
            parts = label.split(".")
            source = PROJECT_ROOT / "twocomms" / parts[0] / (parts[1] + ".py")
            with self.subTest(label=label):
                self.assertTrue(source.is_file())
                if len(parts) == 4:
                    tree = ast.parse(source.read_text())
                    case = next(node for node in tree.body
                                if isinstance(node, ast.ClassDef) and node.name == parts[2])
                    self.assertIn(parts[3], [node.name for node in case.body
                                            if isinstance(node, ast.FunctionDef)])

    def test_stock_partitions_discover_every_current_test_id_without_overlap_or_database_io(self):
        groups = {
            "acceptance": self.runner.STOCK_AUTHORITY_ACCEPTANCE_LABELS,
            "authority": self.runner.SUITES["stock-authority"],
            "consumers": self.runner.SUITES["stock-authority-consumers"],
        }
        # Load the current full modules as well as selected cases. Test bodies
        # never run, and any accidental database connection fails discovery.
        script = """
import json, sys, unittest
from unittest.mock import patch
import django
def ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from ids(item)
        else:
            yield item.id()
with patch('django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection',
           side_effect=AssertionError('test discovery must not access a database')):
    django.setup()
    loader = unittest.TestLoader()
    result = {name: list(ids(loader.loadTestsFromNames(labels)))
              for name, labels in json.loads(sys.stdin.read()).items()}
    assert not loader.errors, loader.errors
print(json.dumps(result))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script], input=json.dumps(groups), text=True,
            capture_output=True, timeout=30, cwd=PROJECT_ROOT / "twocomms",
            env={**os.environ, "DJANGO_SETTINGS_MODULE": "test_settings_no_network_non_dtf",
                 "GOOGLE_INDEXING_ENABLED": "0", "INDEXNOW_ENABLED": "0"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        discovered = json.loads(completed.stdout)
        self.assertGreaterEqual(len(discovered["acceptance"]), 401)
        for group in discovered.values():
            self.assertTrue(group)
            self.assertEqual(len(group), len(set(group)))
        self.assertFalse(set(discovered["authority"]) & set(discovered["consumers"]))
        self.assertEqual(set(discovered["authority"]) | set(discovered["consumers"]),
                         set(discovered["acceptance"]))

    def test_stock_authority_uses_the_native_gate_command_and_verified_cleanup(self):
        for suite in ("stock-authority", "stock-authority-consumers"):
            with self.subTest(suite=suite):
                admin = FakeAdmin()
                command = FakeCommandRunner()
                result = self.runner.run_gate(
                    server_mode="external", suite=suite, admin=admin,
                    command_runner=command, project_root=PROJECT_ROOT,
                    environ={"MARIADB_ADMIN_PASSWORD": "root-secret"}, output=io.StringIO(),
                )
                test_calls = [args for args, _kwargs in command.calls if args[2] == "test"]
                self.assertEqual(test_calls, [[
                    sys.executable, str(PROJECT_ROOT / "twocomms/manage.py"), "test",
                    *self.runner.SUITES[suite],
                    "--settings=test_settings_mariadb", "--noinput", "--keepdb",
                ]])
                self.assertEqual(result["suite"], suite)
                self.assertEqual(result["cleanup"], "verified")
                call_names = [call[0] for call in admin.calls]
                for check in ("prepare_database_warning_contract", "verify_database_warning_contract_reverse",
                              "verify_release_schema", "verify_database_warning_contract"):
                    self.assertIn(check, call_names)
                    self.assertLess(call_names.index(check), call_names.index("drop_user"))
                self.assertEqual(call_names[-4:],
                                 ["drop_user", "drop_database", "verify_cleanup", "close"])

    def test_follow_ugc_suite_verifies_latest_schema_before_cleanup(self):
        admin = FakeAdmin()
        evidence = io.StringIO()

        result = self.runner.run_gate(
            server_mode="external",
            suite="follow-ugc-concurrency",
            admin=admin,
            command_runner=FakeCommandRunner(),
            project_root=PROJECT_ROOT,
            environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            output=evidence,
        )

        call_names = [call[0] for call in admin.calls]
        self.assertIn("verify_release_schema", call_names)
        self.assertIn("verify_follow_ugc_schema", call_names)
        self.assertLess(
            call_names.index("verify_follow_ugc_schema"),
            call_names.index("drop_user"),
        )
        self.assertEqual(result["follow_ugc_tables"], "12_innodb")
        self.assertIn(
            "MariaDB follow/UGC schema proof: "
            "migration=management.0166_ig_ugc_reward_lifecycle "
            "guest_promo=storefront.0095_promocode_guest_ugc "
            "tables=12_innodb unique_indexes=verified foreign_keys=orm_only "
            "lifecycle=3_columns+2_indexes lifecycle_job=target_check+5_indexes",
            evidence.getvalue(),
        )

    def test_follow_ugc_schema_mismatch_is_red_and_cleanup_is_verified(self):
        admin = FakeAdmin(fail_at="verify_follow_ugc_schema")

        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="external",
                suite="follow-ugc-concurrency",
                admin=admin,
                command_runner=FakeCommandRunner(),
                project_root=PROJECT_ROOT,
                environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
            )

        self.assertEqual(
            str(raised.exception.primary_error),
            "follow UGC schema mismatch",
        )
        self.assertEqual(
            [call[0] for call in admin.calls[-4:]],
            ["drop_user", "drop_database", "verify_cleanup", "close"],
        )

    def test_missing_django_entrypoint_fails_before_database_side_effects(self):
        admin = FakeAdmin()
        with tempfile.TemporaryDirectory() as empty_root:
            with self.assertRaises(self.runner.GateError) as raised:
                self.runner.run_gate(
                    server_mode="external",
                    suite="lifecycle",
                    admin=admin,
                    command_runner=FakeCommandRunner(),
                    project_root=Path(empty_root),
                    environ={"MARIADB_ADMIN_PASSWORD": "root-secret"},
                )

        self.assertIn("twocomms/manage.py", str(raised.exception))
        self.assertEqual(admin.calls, [])

    def test_native_mode_fails_closed_when_provisioner_is_missing(self):
        with self.assertRaises(self.runner.GateError) as raised:
            self.runner.run_gate(
                server_mode="native",
                suite="lifecycle",
                project_root=PROJECT_ROOT,
                environ={},
                command_runner=FakeCommandRunner(),
                native_binaries={"mariadbd": None, "mariadb-install-db": None},
            )

        self.assertIn("native MariaDB provisioning", str(raised.exception))

    def test_native_children_receive_only_the_sanitized_environment(self):
        source = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "DB_PASSWORD": "production-secret",
            "DB_PASSWORD_DTF": "production-dtf-secret",
            "META_ACCESS_TOKEN": "provider-secret",
            "GEMINI_API_KEY": "gemini-secret",
            "PYTHONPATH": "/untrusted/import/path",
        }
        init_runner = FakeCommandRunner()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        readiness_admin = mock.Mock()
        readiness_admin._sql.return_value = None

        with (
            mock.patch.object(self.runner.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(self.runner, "AdminClient", return_value=readiness_admin),
            mock.patch.object(self.runner, "_free_port", return_value=33307),
        ):
            server = self.runner.NativeMariaDb(
                binaries={
                    "mariadbd": "/fake/mariadbd",
                    "mariadb-install-db": "/fake/mariadb-install-db",
                },
                command_runner=init_runner,
                environment=self.runner._process_environment(source),
                project_root=PROJECT_ROOT,
            ).start()
            server.close()

        init_env = init_runner.calls[0][1]["env"]
        server_env = popen.call_args.kwargs["env"]
        init_args = init_runner.calls[0][0]
        server_args = popen.call_args.args[0]
        self.assertEqual(init_args[0], "/fake/mariadb-install-db")
        self.assertEqual(init_args[1], "--no-defaults")
        self.assertEqual(server_args[0], "/fake/mariadbd")
        self.assertEqual(server_args[1], "--no-defaults")
        self.assertEqual(init_env, server_env)
        for forbidden in (
            "DB_PASSWORD", "DB_PASSWORD_DTF", "META_ACCESS_TOKEN",
            "GEMINI_API_KEY", "PYTHONPATH",
        ):
            self.assertNotIn(forbidden, init_env)
        self.assertEqual(init_env["PYTHONNOUSERSITE"], "1")
        process.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
