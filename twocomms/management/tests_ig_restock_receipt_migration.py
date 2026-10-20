"""SQLite trigger-preservation regression without a Django database."""
from contextlib import contextmanager
from importlib import import_module
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase


TABLE = "management_igrestockreceipt"
OLD = "(id integer primary key, effect_id integer NOT NULL UNIQUE, invitation_id integer UNIQUE)"
NULLABLE = "(id integer primary key, effect_id integer UNIQUE, invitation_id integer UNIQUE)"
NATIVE = "(id integer primary key, effect_id integer UNIQUE, invitation_id integer UNIQUE, followup_task_id integer UNIQUE)"
CHECKED = NATIVE[:-1] + (
    ", CONSTRAINT ig_restock_receipt_one_origin CHECK "
    "((effect_id IS NOT NULL AND followup_task_id IS NULL) "
    "OR (effect_id IS NULL AND followup_task_id IS NOT NULL)))"
)


class IsolatedCursor:
    def __init__(self, raw):
        self.cursor = raw.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, params=()):
        return self.cursor.execute(sql.replace("%s", "?"), params)

    def fetchall(self):
        return self.cursor.fetchall()


class IsolatedConnection:
    vendor = "sqlite"
    alias = "isolated_restock_receipt_ddl"
    ops = SimpleNamespace(quote_name=lambda name: '"' + name.replace('"', '""') + '"')

    def __init__(self, raw):
        self.raw = raw

    def cursor(self):
        return IsolatedCursor(self.raw)


class IsolatedSchemaEditor:
    """Execute Django's relevant remake sequence on a private sqlite3 handle."""

    def __init__(self, raw):
        self.connection = IsolatedConnection(raw)
        self.calls = []
        self.savepoint_count = 0
        self.fail_after_drop = False

    @contextmanager
    def atomic(self, *, using):
        # Only the private connection is adapted. Any accidental Django DB
        # transaction is a failure, rather than a fixture silently being used.
        if using != self.connection.alias:
            raise AssertionError("Unexpected Django database transaction")
        self.savepoint_count += 1
        name = f"receipt_guard_{self.savepoint_count}"
        raw = self.connection.raw
        raw.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            raw.execute(f"ROLLBACK TO {name}")
            raw.execute(f"RELEASE {name}")
            raise
        else:
            raw.execute(f"RELEASE {name}")

    def remake(self, definition):
        raw = self.connection.raw
        raw.execute(f"CREATE TABLE new__{TABLE} {definition}")
        old_columns = {row[1] for row in raw.execute(f"PRAGMA table_info({TABLE})")}
        columns = [row[1] for row in raw.execute(f"PRAGMA table_info(new__{TABLE})")]
        sources = [column if column in old_columns else "NULL" for column in columns]
        raw.execute(
            f"INSERT INTO new__{TABLE} ({','.join(columns)}) "
            f"SELECT {','.join(sources)} FROM {TABLE}"
        )
        raw.execute(f"DROP TABLE {TABLE}")
        if self.fail_after_drop:
            raise RuntimeError("Injected failure after receipt DROP")
        raw.execute(f"ALTER TABLE new__{TABLE} RENAME TO {TABLE}")

    def alter_field(self, model, old_field, new_field):
        self.calls.append("alter_field")
        self.remake(NULLABLE if new_field.null else OLD)

    def add_field(self, model, field):
        self.calls.append("add_field")
        self.remake(NATIVE)

    def remove_field(self, model, field):
        self.calls.append("remove_field")
        self.remake(NULLABLE)

    def add_constraint(self, model, constraint):
        self.calls.append("add_constraint")
        self.remake(CHECKED)

    def remove_constraint(self, model, constraint):
        self.calls.append("remove_constraint")
        self.remake(NATIVE)


def isolated_state(*, nullable=False, native=False):
    fields = {"effect": SimpleNamespace(null=nullable)}
    if native:
        fields["followup_task"] = SimpleNamespace(null=True)
    model = SimpleNamespace(_meta=SimpleNamespace(
        app_label="management", model_name="igrestockreceipt",
        can_migrate=lambda alias: True, get_field=fields.__getitem__,
    ))
    return SimpleNamespace(apps=SimpleNamespace(get_model=lambda app, name: model))


class RestockReceiptMigrationSQLiteTests(SimpleTestCase):
    def setUp(self):
        self.migration = import_module("management.migrations.0242_restock_followup_receipts")
        self.raw = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.raw.close)
        self.editor = IsolatedSchemaEditor(self.raw)
        self.raw.execute("CREATE TABLE owner (erased integer)")
        self.raw.execute("INSERT INTO owner VALUES (0)")
        self.raw.execute(f"CREATE TABLE {TABLE} {OLD}")
        self.raw.execute(f"INSERT INTO {TABLE} VALUES (1,7,9)")
        self.raw.execute("CREATE TABLE answers (receipt_id integer)")
        for sql in (
            f"CREATE TRIGGER ig_ful_rec_insert BEFORE INSERT ON {TABLE} "
            "WHEN NEW.id<0 BEGIN SELECT RAISE(ABORT,'insert guard'); END",
            f"CREATE TRIGGER ig_ful_rec_update BEFORE UPDATE ON {TABLE} "
            "BEGIN SELECT RAISE(ABORT,'immutable'); END",
            f"CREATE TRIGGER ig_ful_rec_delete BEFORE DELETE ON {TABLE} "
            "WHEN NOT EXISTS (SELECT 1 FROM owner WHERE erased=1) "
            "BEGIN SELECT RAISE(ABORT,'privacy'); END",
            "CREATE TRIGGER ig_ful_ans_insert BEFORE INSERT ON answers "
            f"WHEN NOT EXISTS (SELECT 1 FROM {TABLE} WHERE id=NEW.receipt_id) "
            "BEGIN SELECT RAISE(ABORT,'answer source'); END",
        ):
            self.raw.execute(sql)
        self.original_triggers = self.trigger_sql()

    def trigger_sql(self):
        return self.raw.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()

    def assert_original_guards_and_data(self):
        self.assertEqual(self.trigger_sql(), self.original_triggers)
        self.assertEqual(self.raw.execute(
            f"SELECT id,effect_id,invitation_id FROM {TABLE}"
        ).fetchall(), [(1, 7, 9)])
        for sql, message in (
            (f"UPDATE {TABLE} SET effect_id=8", "immutable"),
            (f"DELETE FROM {TABLE}", "privacy"),
            (f"INSERT INTO {TABLE} (id,effect_id,invitation_id) VALUES (-1,8,10)", "insert guard"),
            ("INSERT INTO answers VALUES (99)", "answer source"),
        ):
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, message):
                self.raw.execute(sql)

    def test_unprotected_remake_fails_on_external_answer_trigger(self):
        with self.assertRaisesRegex(sqlite3.OperationalError, "ig_ful_ans_insert: no such table"):
            self.editor.remake(NULLABLE)

    def test_real_operations_preserve_exact_guards_through_forward_and_reverse(self):
        operations = self.migration.Migration.operations[:3]
        self.assertEqual([type(op) for op in operations], [
            self.migration.PreserveReceiptAlterField,
            self.migration.PreserveReceiptAddField,
            self.migration.PreserveReceiptAddConstraint,
        ])
        states = [isolated_state(), isolated_state(nullable=True),
            isolated_state(nullable=True, native=True), isolated_state(nullable=True, native=True)]
        with patch.object(self.migration.transaction, "atomic", side_effect=self.editor.atomic):
            for index, operation in enumerate(operations):
                with self.subTest(direction="forward", operation=type(operation).__name__):
                    operation.database_forwards("management", self.editor, states[index], states[index + 1])
                    self.assert_original_guards_and_data()
            for index in reversed(range(3)):
                operation = operations[index]
                with self.subTest(direction="reverse", operation=type(operation).__name__):
                    operation.database_backwards("management", self.editor, states[index + 1], states[index])
                    self.assert_original_guards_and_data()
        self.assertEqual(self.editor.calls, [
            "alter_field", "add_field", "add_constraint", "remove_constraint", "remove_field", "alter_field",
        ])
        self.raw.execute("UPDATE owner SET erased=1")
        self.raw.execute(f"DELETE FROM {TABLE}")
        self.assertEqual(self.raw.execute(f"SELECT id FROM {TABLE}").fetchall(), [])

    def test_remake_failure_restores_original_table_and_triggers(self):
        self.editor.fail_after_drop = True
        operation = self.migration.Migration.operations[0]
        with patch.object(self.migration.transaction, "atomic", side_effect=self.editor.atomic):
            with self.assertRaisesRegex(RuntimeError, "Injected failure after receipt DROP"):
                operation.database_forwards(
                    "management", self.editor, isolated_state(), isolated_state(nullable=True),
                )
        self.assert_original_guards_and_data()
        self.assertEqual(self.raw.execute(
            "SELECT name FROM sqlite_master WHERE name=?", [f"new__{TABLE}"],
        ).fetchall(), [])

    def test_mysql_helper_passthrough_never_uses_sqlite_transaction_or_cursor(self):
        editor = SimpleNamespace(connection=SimpleNamespace(vendor="mysql"))
        with patch.object(self.migration.transaction, "atomic") as atomic:
            with self.migration.preserve_sqlite_receipt_triggers(editor):
                pass
        atomic.assert_not_called()
