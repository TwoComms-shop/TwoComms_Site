import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from management.services.ig_engine_health import IG_RUNTIME_TABLES


class Command(BaseCommand):
    help = "Read-only audit of transactional storage engines for IG runtime tables."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        rows = []
        if connection.vendor == "mysql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ("
                    + ",".join(["%s"] * len(IG_RUNTIME_TABLES))
                    + ") ORDER BY TABLE_NAME",
                    list(IG_RUNTIME_TABLES),
                )
                actual = {name: engine for name, engine in cursor.fetchall()}
            rows = [
                {
                    "table": table,
                    "engine": actual.get(table, "missing"),
                    "healthy": str(actual.get(table, "")).lower() == "innodb",
                }
                for table in IG_RUNTIME_TABLES
            ]
        else:
            rows = [
                {"table": table, "engine": connection.vendor, "healthy": True}
                for table in IG_RUNTIME_TABLES
            ]
        report = {
            "read_only": True,
            "vendor": connection.vendor,
            "required_engine": "InnoDB" if connection.vendor == "mysql" else connection.vendor,
            "table_count": len(rows),
            "unhealthy_count": sum(not row["healthy"] for row in rows),
            "tables": rows,
        }
        if options["as_json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            self.stdout.write(
                f"IG engine audit: {report['table_count']} tables; "
                f"unhealthy={report['unhealthy_count']}"
            )
        if report["unhealthy_count"]:
            raise CommandError("IG runtime tables are not fully transactional")
