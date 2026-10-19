"""Default-fit changes must commit together on the actual production engine."""
from django.db import migrations


def ensure_transactional_fit_flags(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "sqlite":
        return
    if connection.vendor != "mysql":
        raise RuntimeError("Fit flag transactions require MariaDB or SQLite")
    table = apps.get_model("storefront", "ProductFitOption")._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute("SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        row = cursor.fetchone()
        if not row or row[1] != "BASE TABLE" or row[0] not in {"InnoDB", "MyISAM"}:
            raise RuntimeError("Unsupported product fit flag storage")
        if row[0] == "MyISAM":
            cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} ENGINE=InnoDB")
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        if cursor.fetchone() != ("InnoDB",):
            raise RuntimeError("Transactional product fit flags unavailable")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0239_current_core_policy_default")]
    operations = [migrations.RunPython(ensure_transactional_fit_flags, migrations.RunPython.noop)]
