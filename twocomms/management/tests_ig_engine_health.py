import json
import importlib
from io import StringIO
from unittest.mock import Mock

from django.core.management import call_command
from django.test import TestCase

from management.services.ig_engine_health import IG_RUNTIME_TABLES


class IgEngineAuditTests(TestCase):
    def test_schema_and_non_atomic_engine_conversion_are_separate(self):
        schema_migration = importlib.import_module(
            "management.migrations.0093_notification_review_and_innodb"
        )
        engine_migration = importlib.import_module(
            "management.migrations.0094_notification_outbox_innodb"
        )

        self.assertTrue(getattr(schema_migration.Migration, "atomic", True))
        self.assertFalse(engine_migration.Migration.atomic)
        self.assertFalse(any(
            operation.__class__.__name__ == "RunPython"
            for operation in schema_migration.Migration.operations
        ))
        self.assertEqual(
            [operation.__class__.__name__ for operation in engine_migration.Migration.operations],
            ["RunPython"],
        )

    def test_engine_conversion_skips_tables_already_innodb(self):
        migration = importlib.import_module(
            "management.migrations.0094_notification_outbox_innodb"
        )
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        cursor.fetchone.side_effect = [("InnoDB",), ("InnoDB",)]
        schema_editor = Mock()
        schema_editor.connection.vendor = "mysql"
        schema_editor.connection.cursor.return_value = cursor
        schema_editor.quote_name.side_effect = lambda value: f"`{value}`"

        migration.convert_outbox_to_innodb(None, schema_editor)

        schema_editor.execute.assert_not_called()

    def test_notification_outbox_tables_are_part_of_transactional_contract(self):
        self.assertIn("management_igbotnotification", IG_RUNTIME_TABLES)
        self.assertIn("management_igbotnotificationaudit", IG_RUNTIME_TABLES)

    def test_read_only_engine_audit_reports_every_runtime_table(self):
        out = StringIO()

        call_command("audit_ig_table_engines", "--json", stdout=out)

        report = json.loads(out.getvalue())
        self.assertTrue(report["read_only"])
        self.assertEqual(report["table_count"], len(IG_RUNTIME_TABLES))
        self.assertEqual(report["unhealthy_count"], 0)
        self.assertEqual(
            {row["table"] for row in report["tables"]},
            set(IG_RUNTIME_TABLES),
        )
