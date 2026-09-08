from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def ensure_innodb(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    table = "management_igdeferredecho"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Deferred echo table is missing")
        if str(row[0]).casefold() != "innodb":
            schema_editor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} ENGINE=InnoDB")
        cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
        if str(cursor.fetchone()[0]).casefold() != "innodb":
            raise RuntimeError("Deferred echo requires InnoDB")


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0202_ig_source_action_receipt")]
    operations = [
        migrations.CreateModel(
            name="IgDeferredEcho",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("settings_id_snapshot", models.PositiveIntegerField()),
                ("provider_namespace", models.CharField(max_length=128)),
                ("recipient_igsid", models.CharField(max_length=64)),
                ("provider_message_id", models.CharField(max_length=255)),
                ("payload", models.JSONField(default=dict)),
                ("event_digest", models.CharField(max_length=64)),
                ("provider_created_at", models.DateTimeField(blank=True, null=True)),
                ("observed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("competing_effect_ids", models.JSONField(default=list)),
                ("candidate_overflow", models.BooleanField(default=False)),
                ("state", models.CharField(choices=[("waiting_receipt", "Очікує квитанцію"), ("ambiguous", "Потрібна звірка"), ("own", "Підтверджене власне повідомлення"), ("manager_pending", "Очікує запису менеджера"), ("manager_applied", "Менеджера зафіксовано")], default="waiting_receipt", max_length=20)),
                ("reason", models.CharField(blank=True, default="", max_length=64)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="deferred_echoes", to="management.igclient")),
                ("matched_effect", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deferred_echoes", to="management.igrevisiondeliveryeffect")),
                ("manager_message", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deferred_echoes", to="management.instagrambotmessage")),
                ("permission_transition", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deferred_echoes", to="management.igpermissiontransitionjob")),
                ("notification", models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deferred_echoes", to="management.igbotnotification")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("provider_namespace", "provider_message_id"), name="ig_echo_namespace_mid")],
                "indexes": [models.Index(fields=["client", "provider_namespace", "state"], name="ig_echo_client_scope_state"), models.Index(fields=["state", "observed_at", "id"], name="ig_echo_state_observed")],
            },
        ),
        migrations.RunPython(ensure_innodb, migrations.RunPython.noop),
    ]
