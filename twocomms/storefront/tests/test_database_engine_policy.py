from pathlib import Path

from django.test import SimpleTestCase


class DatabaseEnginePolicyTests(SimpleTestCase):
    def test_mysql_connections_default_new_tables_to_innodb(self):
        project_root = Path(__file__).resolve().parents[2]
        for relative_path in (
            "twocomms/settings.py",
            "twocomms/production_settings.py",
        ):
            source = (project_root / relative_path).read_text(encoding="utf-8")
            self.assertIn(
                "default_storage_engine=INNODB",
                source,
                f"Missing InnoDB default in {relative_path}",
            )
