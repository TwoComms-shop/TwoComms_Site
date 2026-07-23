"""Fail-closed tests for the production-only Instagram CRM verifier."""

from types import SimpleNamespace
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from management.management.commands import verify_ig_production_contract as verifier


class _FakeCursor:
    def __init__(self, rows):
        self.rows = iter(rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return next(self.rows)


class ProductionDatabaseGuardTests(SimpleTestCase):
    def test_rejects_sqlite_before_opening_cursor(self):
        fake_connection = SimpleNamespace(
            vendor="sqlite",
            settings_dict={"NAME": "production"},
        )
        with patch.object(verifier, "connection", fake_connection):
            with self.assertRaisesMessage(CommandError, "requires MySQL/MariaDB"):
                verifier._assert_production_database("production")

    def test_rejects_test_database_before_opening_cursor(self):
        fake_connection = SimpleNamespace(
            vendor="mysql",
            settings_dict={"NAME": "test_production"},
        )
        with patch.object(verifier, "connection", fake_connection):
            with self.assertRaisesMessage(CommandError, "test database is forbidden"):
                verifier._assert_production_database("test_production")

    def test_rejects_expected_database_mismatch_before_opening_cursor(self):
        fake_connection = SimpleNamespace(
            vendor="mysql",
            settings_dict={"NAME": "actual_production"},
        )
        with patch.object(verifier, "connection", fake_connection):
            with self.assertRaisesMessage(CommandError, "database identity mismatch"):
                verifier._assert_production_database("wrong_production")

    def test_rejects_server_selected_database_mismatch(self):
        fake_connection = SimpleNamespace(
            vendor="mysql",
            settings_dict={"NAME": "production"},
            cursor=lambda: _FakeCursor([("other_database",), (0,)]),
        )
        with patch.object(verifier, "connection", fake_connection):
            with self.assertRaisesMessage(CommandError, "server-selected database mismatch"):
                verifier._assert_production_database("production")

    def test_rejects_visible_test_schema(self):
        fake_connection = SimpleNamespace(
            vendor="mysql",
            settings_dict={"NAME": "production"},
            cursor=lambda: _FakeCursor([("production",), (1,)]),
        )
        with patch.object(verifier, "connection", fake_connection):
            with self.assertRaisesMessage(CommandError, "test database schemas detected"):
                verifier._assert_production_database("production")

    @patch.object(verifier, "maintenance_status", return_value={"active": False})
    def test_rollback_fixtures_require_maintenance_before_database_write(self, _status):
        with self.assertRaisesMessage(CommandError, "requires an active maintenance lease"):
            verifier._run_rollback_fixtures()
