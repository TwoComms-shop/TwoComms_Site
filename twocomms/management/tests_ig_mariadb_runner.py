"""Disposable MariaDB uses full migrations and retains immutable SQL guards."""
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, call, patch, sentinel

from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.runner import DiscoverRunner

from test_support import ig_mariadb_runner as runner_module


class InstagramMariaDbRunnerTests(SimpleTestCase):
    def database(self, *, vendor="mysql", name="test_twocomms_ig_runner"):
        database = MagicMock()
        database.vendor = vendor
        database.settings_dict = {"NAME": name, "TEST": {"NAME": name}}
        database.ops.quote_name.side_effect = lambda name: f"`{name}`"
        return database

    def test_setup_delegates_full_graph_and_options_to_standard_django_runner(self):
        database = self.database()
        runner = runner_module.InstagramMariaDbGateRunner(verbosity=0, keepdb=True)
        with patch.object(runner_module, "connection", database), patch.object(
            DiscoverRunner, "setup_databases", return_value=sentinel.database_state,
        ) as parent_setup:
            result = runner.setup_databases(aliases={"default"}, serialized_aliases=set())
        self.assertIs(result, sentinel.database_state)
        parent_setup.assert_called_once_with(aliases={"default"}, serialized_aliases=set())
        database.cursor.assert_not_called()
        database._nodb_cursor.assert_not_called()

    def test_setup_rejects_wrong_vendor_database_and_test_destination_before_parent(self):
        for vendor, name, test_name in (
            ("sqlite", "test_twocomms_ig_runner", "test_twocomms_ig_runner"),
            ("mysql", "application", "test_twocomms_ig_runner"),
            ("mysql", "test_twocomms_ig_runner", "application"),
            ("mysql", "test_twocomms_ig_runner", None),
        ):
            with self.subTest(vendor=vendor, name=name, test_name=test_name):
                database = self.database(vendor=vendor, name=name)
                database.settings_dict["TEST"]["NAME"] = test_name
                with patch.object(runner_module, "connection", database), patch.object(
                    DiscoverRunner, "setup_databases",
                ) as parent_setup:
                    with self.assertRaisesRegex(RuntimeError, "requires disposable MariaDB"):
                        runner_module.InstagramMariaDbGateRunner(verbosity=0).setup_databases()
                parent_setup.assert_not_called()
                database.cursor.assert_not_called()

    def test_suite_adapts_only_default_transaction_cleanup_and_preserves_custom_hooks(self):
        from management.tests_ig_mariadb_follow_ugc import _MariaDbConcurrencyCase

        class DefaultCase(TransactionTestCase):
            def _reset_sequences(self, database):
                return sentinel.original_sequences

        class CustomCase(DefaultCase):
            def _fixture_teardown(self):
                return sentinel.custom_cleanup

        default, repeated = DefaultCase(), DefaultCase()
        custom, native = CustomCase(), _MariaDbConcurrencyCase()
        atomic, simple = TestCase(), SimpleTestCase()
        suite = unittest.TestSuite([default, unittest.TestSuite([repeated, custom, native, atomic, simple])])
        with patch.object(DiscoverRunner, "build_suite", return_value=suite):
            result = runner_module.InstagramMariaDbGateRunner(verbosity=0).build_suite(["example"])

        self.assertIs(result, suite)
        self.assertIsNot(type(default), DefaultCase)
        self.assertIs(type(default), type(repeated))
        self.assertIs(type(default)._fixture_teardown, runner_module._truncate_disposable)
        self.assertIs(type(default)._reset_sequences, DefaultCase._reset_sequences)
        self.assertIs(DefaultCase._fixture_teardown, TransactionTestCase._fixture_teardown)
        self.assertIs(type(custom), CustomCase)
        self.assertIs(custom._fixture_teardown(), sentinel.custom_cleanup)
        self.assertIs(type(native), _MariaDbConcurrencyCase)
        self.assertIs(type(atomic), TestCase)
        self.assertIs(type(simple), SimpleTestCase)
        with patch.object(default, "_databases_names", return_value=("default",)) as aliases, patch.object(
            runner_module, "_truncate_aliases",
        ) as truncate:
            default._fixture_teardown()
        aliases.assert_called_once_with(include_mirrors=False)
        truncate.assert_called_once_with(("default",))

    def test_disposable_cleanup_truncates_tables_retains_migration_history_and_reseeds(self):
        database = self.database()
        database.introspection.django_table_names.return_value = ["django_migrations", "signed_journal", "auth_user"]
        cursor = database.cursor.return_value.__enter__.return_value
        with patch.object(runner_module, "connections", {"default": database}), patch.object(
            runner_module, "emit_post_migrate_signal",
        ) as reseed:
            runner_module._truncate_aliases(("default",))
        self.assertEqual(cursor.execute.call_args_list, [
            call("SET FOREIGN_KEY_CHECKS=0"),
            call("TRUNCATE TABLE `signed_journal`"),
            call("TRUNCATE TABLE `auth_user`"),
            call("SET FOREIGN_KEY_CHECKS=1"),
        ])
        reseed.assert_called_once_with(verbosity=0, interactive=False, db="default")

    def test_cleanup_rejects_wrong_vendor_or_namespace_without_any_sql(self):
        for vendor, name in (("sqlite", "test_twocomms_ig_runner"), ("mysql", "application")):
            with self.subTest(vendor=vendor, name=name):
                database = self.database(vendor=vendor, name=name)
                with patch.object(runner_module, "connections", {"default": database}):
                    with self.assertRaisesRegex(RuntimeError, "requires its disposable MariaDB"):
                        runner_module._truncate_aliases(("default",))
                database.cursor.assert_not_called()
                database.introspection.django_table_names.assert_not_called()

    def test_cleanup_restores_foreign_key_checks_if_truncate_fails(self):
        database = self.database()
        database.introspection.django_table_names.return_value = ["signed_journal"]
        cursor = database.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = [None, RuntimeError("cleanup failed"), None]
        with patch.object(runner_module, "connections", {"default": database}), patch.object(
            runner_module, "emit_post_migrate_signal",
        ) as reseed:
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                runner_module._truncate_aliases(("default",))
        self.assertEqual(cursor.execute.call_args_list[-1], call("SET FOREIGN_KEY_CHECKS=1"))
        reseed.assert_not_called()

    def test_mariadb_profile_selects_this_runner_after_its_existing_fail_closed_checks(self):
        from management.tests_test_settings_mariadb import MariaDbTestSettingsContractTests, PROJECT_ROOT

        profile_fixture = MariaDbTestSettingsContractTests()
        profile_fixture.setUp()
        self.addCleanup(profile_fixture.tearDown)
        result = subprocess.run([
            sys.executable, "-c",
            "import test_settings_mariadb as settings; "
            "assert settings.TEST_RUNNER == 'test_support.ig_mariadb_runner.InstagramMariaDbGateRunner'",
        ], cwd=PROJECT_ROOT, env=profile_fixture._environment(), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
