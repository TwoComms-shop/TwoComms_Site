from django.db import migrations, models


def ensure_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    table = "management_igpresencecapability"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            [table],
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Instagram presence capability table is missing")
        if str(row[0]).casefold() != "innodb":
            schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} ENGINE=InnoDB")
        cursor.execute(
            "SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            [table],
        )
        verified = cursor.fetchone()
        if not verified or str(verified[0]).casefold() != "innodb":
            raise RuntimeError("Instagram presence capabilities require InnoDB")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("management", "0208_journey_trace_snapshots"),
    ]
    operations = [
        migrations.CreateModel(
            name="IgPresenceCapability",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("transport", models.CharField(max_length=32)),
                ("graph_version", models.CharField(max_length=16)),
                ("account_key", models.CharField(max_length=64)),
                ("capability", models.CharField(max_length=32)),
                ("config_fingerprint", models.CharField(max_length=64)),
                ("status", models.CharField(choices=[("unknown", "Невідомо"), ("verified", "Перевірено"), ("denied", "Відмовлено"), ("invalidated", "Відкликано")], db_index=True, default="unknown", max_length=16)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("verified_by_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("evidence_kind", models.CharField(blank=True, default="", max_length=64)),
                ("evidence_ref", models.CharField(blank=True, default="", max_length=255)),
                ("denied_at", models.DateTimeField(blank=True, null=True)),
                ("denied_until", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("denied_error_kind", models.CharField(blank=True, default="", max_length=32)),
                ("denied_error_code", models.CharField(blank=True, default="", max_length=64)),
                ("invalidated_at", models.DateTimeField(blank=True, null=True)),
                ("invalidated_by_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("invalidation_reason", models.CharField(blank=True, default="", max_length=255)),
                ("last_error_kind", models.CharField(blank=True, default="", max_length=32)),
                ("last_error_code", models.CharField(blank=True, default="", max_length=64)),
                ("last_error_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [models.Index(fields=["status", "expires_at"], name="ig_presence_cap_status")],
                "constraints": [models.UniqueConstraint(fields=("transport", "graph_version", "account_key", "capability"), name="ig_presence_capability_identity")],
            },
        ),
        migrations.RunPython(ensure_innodb, migrations.RunPython.noop),
    ]
