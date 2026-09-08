from django.db import migrations, models
import django.db.models.deletion


def ensure_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    table = "management_igjourneytracesnapshot"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Journey trace snapshot table is missing")
        if str(row[0]).casefold() != "innodb":
            schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} ENGINE=InnoDB")
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        if str(cursor.fetchone()[0]).casefold() != "innodb":
            raise RuntimeError("Journey trace snapshots require InnoDB")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0207_conversation_route_decisions")]
    operations = [migrations.CreateModel(
        name="IgJourneyTraceSnapshot",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("snapshot_key", models.CharField(max_length=64, unique=True)),
            ("watermark_message_id", models.PositiveBigIntegerField()),
            ("source_digest", models.CharField(max_length=64)),
            ("trace", models.JSONField()),
            ("trace_digest", models.CharField(max_length=64)),
            ("schema_version", models.CharField(default="journey-trace.v1", max_length=32)),
            ("prompt_version", models.CharField(max_length=32)),
            ("producer_version", models.CharField(default="journey-trace-store.v1", max_length=32)),
            ("analysis_model", models.CharField(max_length=80)),
            ("analyzed_at", models.DateTimeField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="journey_trace_snapshots", to="management.igclient")),
            ("commercial_episode", models.ForeignKey(blank=True, db_constraint=False, null=True,
                on_delete=django.db.models.deletion.DO_NOTHING, related_name="journey_trace_snapshots",
                to="management.igcommercialepisode")),
        ],
        options={
            "indexes": [models.Index(fields=["client", "commercial_episode", "-id"], name="ig_trace_client_episode")],
            "constraints": [models.CheckConstraint(condition=models.Q(watermark_message_id__gte=1), name="ig_trace_positive_watermark")],
        },
    ), migrations.RunPython(ensure_innodb, migrations.RunPython.noop)]
