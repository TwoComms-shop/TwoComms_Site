"""The existing points ledger and issued rights must commit together."""
from django.db import migrations


def ensure_transactional_rewards(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "sqlite":
        return
    if connection.vendor != "mysql":
        raise RuntimeError("Points rewards require transactional MariaDB or SQLite")
    # Authentication is only an identity dependency. Do not mutate auth_user or
    # invent a second ledger; these are the three actual reward write owners.
    for app, model in (("accounts", "UserPoints"), ("accounts", "PointsHistory"), ("storefront", "UserPromoCode")):
        table = apps.get_model(app, model)._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute("SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            row = cursor.fetchone()
            if not row or row[1] != "BASE TABLE" or row[0] not in {"InnoDB", "MyISAM"}:
                raise RuntimeError(f"Unsupported reward table: {table}")
            if row[0] != "InnoDB":
                cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} ENGINE=InnoDB")
            cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
            if cursor.fetchone() != ("InnoDB",):
                raise RuntimeError(f"Reward transaction engine unavailable: {table}")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [
        ("management", "0237_ad_context_observations"),
        ("accounts", "0030_alter_telegram_verification_purpose"),
        ("storefront", "0100_customprintlead_telegram_delivery"),
    ]
    operations = [migrations.RunPython(ensure_transactional_rewards, migrations.RunPython.noop)]
