import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from management.services.ig_engine_health import IG_RUNTIME_TABLES


class IgEngineAuditTests(TestCase):
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
