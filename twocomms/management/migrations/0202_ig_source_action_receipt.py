from django.db import migrations, models
import django.db.models.deletion


def ensure_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    table = "management_igsourceactionreceipt"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Source action receipt table is missing")
        if str(row[0]).casefold() != "innodb":
            schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} ENGINE=InnoDB")
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        if str(cursor.fetchone()[0]).casefold() != "innodb":
            raise RuntimeError("Source action receipts require InnoDB")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0201_policy_erasure_transaction_engines")]
    operations = [
        migrations.CreateModel(
            name="IgSourceActionReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(max_length=32)),
                ("source_digest", models.CharField(max_length=64)),
                ("payload_digest", models.CharField(max_length=64)),
                ("outcome", models.JSONField(default=dict)),
                ("outcome_digest", models.CharField(max_length=64)),
                ("first_revision_id", models.PositiveBigIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="source_action_receipts", to="management.igclient")),
                ("source_message", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="source_action_receipts", to="management.instagrambotmessage")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("source_message", "kind"), name="ig_source_action_kind")]},
        ),
        migrations.RunPython(ensure_innodb, migrations.RunPython.noop),
    ]
