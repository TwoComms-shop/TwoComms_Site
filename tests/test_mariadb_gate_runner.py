import io
import os
import stat
import subprocess
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
            "engines": "3_myisam",
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
            "engines=3_myisam generated_columns=2_verified "
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
                    [(table, "MyISAM") for table in tables],
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
            ("reviews_reviewvote", "MyISAM"),
            ("storefront_productfitoption", "MyISAM"),
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
                ") ENGINE=MyISAM"
            ),
            "storefront_productfitoption": (
                "CREATE TABLE `storefront_productfitoption` ("
                "`default_product_identity` bigint GENERATED ALWAYS AS (case when `is_default` = 1 then `product_id` else NULL end) STORED,"
                "UNIQUE KEY `fit_product_code_uniq` (`product_id`,`code`),"
                "UNIQUE KEY `uniq_default_fit_product` (`default_product_identity`)"
                ") ENGINE=MyISAM"
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
                "engines": "3_myisam",
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
            ("reviews_reviewvote", "MyISAM"),
            ("storefront_productfitoption", "MyISAM"),
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
