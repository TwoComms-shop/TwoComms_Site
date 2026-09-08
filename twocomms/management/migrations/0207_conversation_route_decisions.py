from django.db import migrations, models
import django.db.models.deletion


def ensure_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    table = "management_igconversationroutedecision"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Conversation route journal table is missing")
        if str(row[0]).casefold() != "innodb":
            schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} ENGINE=InnoDB")
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        if str(cursor.fetchone()[0]).casefold() != "innodb":
            raise RuntimeError("Conversation route journal requires InnoDB")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0206_revision_recovery_schedule")]

    operations = [migrations.CreateModel(
        name="IgConversationRouteDecision",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("decision_key", models.CharField(max_length=64, unique=True)),
            ("sequence", models.PositiveBigIntegerField()),
            ("reset_floor", models.PositiveBigIntegerField()),
            ("watermark_message_id", models.PositiveBigIntegerField()),
            ("input_digest", models.CharField(max_length=64)),
            ("interpretation_digest", models.CharField(max_length=64)),
            ("decision_digest", models.CharField(max_length=64)),
            ("source_binding", models.JSONField()),
            ("interpretation", models.JSONField()),
            ("active_intents", models.JSONField(default=list)),
            ("focus_key", models.CharField(blank=True, default="", max_length=64)),
            ("transitions", models.JSONField(default=list)),
            ("reason_code", models.CharField(default="customer_intent", max_length=32)),
            ("schema_version", models.CharField(default="customer-route.v1", max_length=32)),
            ("producer_version", models.CharField(default="conversation-route.v1", max_length=32)),
            ("occurred_at", models.DateTimeField()),
            ("recorded_at", models.DateTimeField(auto_now_add=True)),
            ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="conversation_route_decisions", to="management.igclient")),
            ("revision", models.ForeignKey(db_constraint=False, null=True,
                on_delete=django.db.models.deletion.DO_NOTHING, related_name="route_decisions",
                to="management.igcustomerturnrevision")),
            ("analysis_result", models.ForeignKey(db_constraint=False, null=True,
                on_delete=django.db.models.deletion.DO_NOTHING, related_name="route_decisions",
                to="management.igconversationanalysisresult")),
            ("previous", models.ForeignKey(db_constraint=False, null=True,
                on_delete=django.db.models.deletion.DO_NOTHING, related_name="successors",
                to="management.igconversationroutedecision")),
        ],
        options={
            "indexes": [models.Index(fields=["client", "reset_floor", "-sequence"], name="ig_route_current_scope")],
            "constraints": [
                models.CheckConstraint(condition=(
                    models.Q(revision__isnull=False, analysis_result__isnull=True)
                    | models.Q(revision__isnull=True, analysis_result__isnull=False)), name="ig_route_exact_source"),
                models.UniqueConstraint(fields=("client", "reset_floor", "sequence"), name="ig_route_scope_sequence"),
                models.UniqueConstraint(fields=("client", "reset_floor", "input_digest", "interpretation_digest"), name="ig_route_input_interpretation"),
                models.CheckConstraint(condition=models.Q(sequence__gte=1)
                    & models.Q(reset_floor__gte=1) & models.Q(watermark_message_id__gte=1), name="ig_route_positive_scope"),
            ],
        },
    ), migrations.RunPython(ensure_innodb, migrations.RunPython.noop)]
