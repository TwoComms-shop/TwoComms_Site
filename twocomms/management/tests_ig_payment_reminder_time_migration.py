"""Real 0243 reversible SQLite DDL on a private in-memory connection.

This proves schema/guard preservation only. Native business receipt and privacy
behavior belong to the producer-backed storage suite, not these empty tables.
"""
from contextlib import contextmanager
from importlib import import_module

from django.db import connections
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, override_settings
from django.utils.connection import ConnectionDoesNotExist


PARENT = ("management", "0242_restock_followup_receipts")
TARGET = ("management", "0243_payment_reminder_time_dialog")
TABLE = "management_igpaymentremindertimerequest"
TIME_GUARDS = {"ig_paytime_insert", "ig_paytime_update", "ig_paytime_delete"}


class PaymentReminderTimeMigrationSQLiteTests(SimpleTestCase):
    """Use historical ProjectState and the actual migration's apply/unapply."""

    def setUp(self):
        self.alias = "isolated_payment_reminder_time_ddl"
        self.assertNotIn(self.alias, connections)
        self.connection = DatabaseWrapper({
            "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
            "ATOMIC_REQUESTS": False, "AUTOCOMMIT": True, "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": False, "OPTIONS": {}, "TIME_ZONE": None,
            "USER": "", "PASSWORD": "", "HOST": "", "PORT": "",
        }, alias=self.alias)
        self.addCleanup(self.close_isolated_connection)
        # test_settings disables migrations; loading the disk DAG here is pure
        # state work and must not use or open the configured default database.
        with override_settings(MIGRATION_MODULES={}):
            loader = MigrationLoader(None)
            self.before = loader.project_state(PARENT)
            self.migration = loader.graph.nodes[TARGET]
            self.assertIn(PARENT, self.migration.dependencies)
            self.assertEqual(loader.graph.forwards_plan(TARGET)[-1], TARGET)
        self.assertNotIn(("management", "igpaymentremindertimerequest"), self.before.models)
        # Only the historical tables referenced by the two original guard
        # installers are materialized. No application fixture or receipt is
        # fabricated, and no historical migration is reversed on the test DB.
        model_names = (
            ("management", "IgClient"),
            ("management", "InstagramBotMessage"),
            ("management", "IgCustomerTurnRevision"),
            ("management", "IgTurnRevisionSource"),
            ("management", "IgDeal"),
            ("management", "IgCheckoutProposal"),
            ("management", "IgCommercialEpisode"),
            ("orders", "PaymentAttempt"),
            ("management", "IgCheckoutInvoiceGeneration"),
            ("management", "IgRevisionDeliveryEffect"),
            ("management", "IgFollowUpTask"),
            ("management", "IgRestockInvitation"),
            ("management", "IgPaymentReminderInvitation"),
            ("management", "IgPaymentReminderReceipt"),
            ("management", "IgPaymentReminderAnswer"),
            ("management", "IgRestockReceipt"),
        )
        with self.connection.schema_editor(atomic=False) as editor:
            for app, name in model_names:
                editor.create_model(self.before.apps.get_model(app, name))
            import_module("management.migrations.0229_agreed_payment_reminders").install_reminder_guards(
                self.before.apps, editor)
            import_module("management.migrations.0242_restock_followup_receipts").receipt_guard(
                self.before.apps, editor)
        self.original_schema = self.schema()
        self.original_guards = self.guards()
        self.assertEqual(set(self.original_guards), {
            *(f"ig_payrem_{kind}_{event}" for kind in ("inv", "rec", "ans")
              for event in ("insert", "update", "delete")),
            "ig_ful_rec_insert",
        })

    def close_isolated_connection(self):
        # Django deliberately ignores close() for in-memory SQLite test DBs;
        # this private handle has no test-runner owner to close it for us.
        if self.connection.connection is not None:
            self.connection.connection.close()
            self.connection.connection = None

    @contextmanager
    def registered_connection(self):
        # Migration.allow_migrate_model resolves an alias via connections even
        # though schema_editor already owns the exact private SQLite handle.
        # Register only that handle, without adding an alias to DATABASES or
        # changing the configured default database. Restore thread-local state
        # even if apply, an assertion, or reverse raises.
        absent = object()
        try:
            previous = connections[self.alias]
        except ConnectionDoesNotExist:
            previous = absent
        connections[self.alias] = self.connection
        try:
            self.assertIs(connections[self.alias], self.connection)
            self.assertNotIn(self.alias, connections.settings)
            yield
        finally:
            if previous is absent:
                del connections[self.alias]
            else:
                connections[self.alias] = previous

    def rows(self, sql, params=()):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def schema(self):
        return {(kind, name): sql for kind, name, sql in self.rows(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger') ORDER BY type,name")}

    def guards(self):
        return dict(self.rows("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))

    def assert_original_schema_preserved(self):
        current = self.schema()
        for identity, sql in self.original_schema.items():
            with self.subTest(existing_object=identity):
                self.assertIn(identity, current)
                self.assertEqual(current[identity], sql)
        self.assertEqual({key: value for key, value in self.guards().items()
                          if key not in TIME_GUARDS}, self.original_guards)
        self.assertNotIn(self.alias, connections.settings)

    def target_schema(self):
        return dict(self.rows("SELECT name,sql FROM sqlite_master WHERE tbl_name=%s ORDER BY type,name", [TABLE]))

    def assert_target_installed(self, state):
        model = state.apps.get_model("management", "IgPaymentReminderTimeRequest")
        self.assertEqual(model._meta.db_table, TABLE)
        columns = {row[1] for row in self.rows(f'PRAGMA table_info("{TABLE}")')}
        self.assertEqual(columns, {
            "id", "envelope", "envelope_hmac", "created_at", "expires_at", "window_deadline",
            "locale", "body", "client_id", "request_source_id", "revision_id",
        })
        self.assertEqual(TIME_GUARDS & set(self.guards()), TIME_GUARDS)
        self.assertEqual([row[2] for row in self.rows('PRAGMA index_info("ig_paytime_client_expiry")')],
            ["client_id", "expires_at"])
        unique_columns = {
            tuple(column[2] for column in self.rows(
                f"PRAGMA index_info({self.connection.ops.quote_name(index[1])})"))
            for index in self.rows(f'PRAGMA index_list("{TABLE}")') if index[2]
        }
        self.assertIn(("request_source_id",), unique_columns)
        self.assert_original_schema_preserved()

    def test_actual_0243_apply_unapply_reapply_preserves_prior_guards_and_indexes(self):
        with self.registered_connection():
            with self.connection.schema_editor(atomic=False) as editor:
                applied = self.migration.apply(self.before.clone(), editor)
            self.assert_target_installed(applied)
            first_target = self.target_schema()
            # Migration.unapply takes the state BEFORE the migration, not the
            # mutated state returned by apply. Reverse removes only this table.
            with self.connection.schema_editor(atomic=False) as editor:
                self.migration.unapply(self.before.clone(), editor)
            self.assertEqual(self.target_schema(), {})
            self.assertFalse(TIME_GUARDS & set(self.guards()))
            self.assertEqual(self.schema(), self.original_schema)
            self.assert_original_schema_preserved()
            with self.connection.schema_editor(atomic=False) as editor:
                reapplied = self.migration.apply(self.before.clone(), editor)
            self.assert_target_installed(reapplied)
            self.assertEqual(self.target_schema(), first_target)
