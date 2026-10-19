"""Retry-safe advertisement context and asynchronous metadata observation."""
from importlib import import_module

from django.db import migrations, models

from ._resumable_schema import _columns, _validate_field


class AdContextField(migrations.AddField):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        field = model._meta.get_field(self.name)
        columns = _columns(schema_editor, model._meta.db_table)
        if field.column in columns:
            _validate_field(schema_editor, model._meta.db_table, field, columns[field.column])
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


def ensure_observation_engine(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    table = apps.get_model("management", "IgAdMetadataRecord")._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {schema_editor.quote_name(table)} ENGINE=InnoDB")


CreateModel = import_module("management.migrations.0233_assisted_commerce").ResumableAssistedCreateModel


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("management", "0236_checkout_amendment_resolution")]
    operations = [
        AdContextField(model_name="botadcampaign", name="context", field=models.JSONField(default=dict, blank=True)),
        CreateModel(name="IgAdMetadataRecord", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("namespace", models.CharField(max_length=128)),
            ("ad_id", models.CharField(max_length=64)),
            ("state", models.CharField(max_length=16, default="pending", db_index=True)),
            ("reason", models.CharField(max_length=64, blank=True, default="")),
            ("api_version", models.CharField(max_length=16, blank=True, default="")),
            ("snapshot", models.JSONField(default=dict, blank=True)),
            ("snapshot_digest", models.CharField(max_length=64, blank=True, default="")),
            ("requested_at", models.DateTimeField(auto_now_add=True)),
            ("fetched_at", models.DateTimeField(null=True, blank=True)),
            ("next_attempt_at", models.DateTimeField(null=True, blank=True, db_index=True)),
            ("attempts", models.PositiveSmallIntegerField(default=0)),
            ("claim_token", models.CharField(max_length=64, blank=True, default="")),
            ("lease_until", models.DateTimeField(null=True, blank=True)),
        ], options={
            "constraints": [models.UniqueConstraint(fields=["namespace", "ad_id"], name="ig_ad_metadata_identity")],
            "indexes": [models.Index(fields=["state", "next_attempt_at"], name="ig_ad_metadata_due")],
        }),
        migrations.RunPython(ensure_observation_engine, migrations.RunPython.noop),
    ]
