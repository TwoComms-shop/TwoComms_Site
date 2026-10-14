"""Enable owned customer-selected quotes without changing released39 columns.

The original full-quote guards remain the default of the shared installer.
Reversal reinstalls them after removing the additive scope CHECK, while all
immutable history and canonical paid winner evidence remain untouched.
"""
from importlib import import_module

from django.db import migrations, models

from ._resumable_schema import _constraints

_assisted = import_module("management.migrations.0233_assisted_commerce")
_split = import_module("management.migrations.0232_split_shipping")


class ResumableAssistedAddConstraint(migrations.AddConstraint):
    """Verify the exact CHECK after interrupted non-atomic MariaDB DDL."""

    def _existing(self, schema_editor, model):
        existing = _constraints(schema_editor, model._meta.db_table).get(self.constraint.name)
        if existing is None:
            return False
        actual = _split._check_clause(schema_editor, model._meta.db_table, self.constraint.name)
        expected_sql = str(self.constraint.constraint_sql(model, schema_editor))
        expected = expected_sql[expected_sql.upper().index("CHECK") + 5:].strip()
        if not existing.get("check") or actual is None or _split._normalized_check(actual) != _split._normalized_check(expected):
            raise RuntimeError(f"Assisted partial CHECK shape mismatch: {self.constraint.name}")
        return True

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model) and not self._existing(schema_editor, model):
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model) and self._existing(schema_editor, model):
            super().database_backwards(app_label, schema_editor, from_state, to_state)


def enable_partial_guards(apps, schema_editor):
    _assisted.ensure_transactions(apps, schema_editor)
    _assisted.install_guards(apps, schema_editor, allow_partial=True)


def restore_full_guards(apps, schema_editor):
    _assisted.ensure_transactions(apps, schema_editor)
    _assisted.install_guards(apps, schema_editor)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0233_assisted_commerce")]
    operations = [
        # Reversal runs this last, after SQLite's constraint-table rebuild.
        migrations.RunPython(migrations.RunPython.noop, restore_full_guards),
        migrations.AlterField(
            model_name="igassistedcommerceexecution", name="scope",
            field=models.CharField(max_length=16,
                choices=[("full", "Full intention"), ("partial", "Customer-accepted selection")]),
        ),
        ResumableAssistedAddConstraint(
            model_name="igassistedcommerceexecution",
            constraint=models.CheckConstraint(
                condition=models.Q(scope="full") | models.Q(scope="partial", choice_source__isnull=False),
                name="ig_assist_execution_scope",
            ),
        ),
        migrations.RunPython(enable_partial_guards, migrations.RunPython.noop),
    ]
