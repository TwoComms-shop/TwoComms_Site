from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0218_ig_commerce_source_action_transitions")]

    operations = [
        migrations.AddField("instagrambotsettings", "memory_reconcile_cursor", models.PositiveBigIntegerField(default=0)),
        migrations.AddField("igclient", "memory_version", models.PositiveBigIntegerField(default=0)),
        migrations.AddField("igclient", "memory_provenance", models.JSONField(blank=True, default=dict)),
        migrations.AddField("igclient", "memory_producer_state", models.JSONField(blank=True, default=dict)),
        migrations.AddField("igclient", "memory_dirty_at", models.DateTimeField(blank=True, null=True)),
        migrations.AddField("igclient", "memory_due_at", models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField("igclient", "memory_claim_token", models.CharField(blank=True, default="", max_length=40)),
        migrations.AddField("igclient", "memory_claim_until", models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField("igclient", "memory_claim_snapshot", models.JSONField(blank=True, default=dict)),
        migrations.AddField("igclient", "memory_attempts", models.PositiveSmallIntegerField(default=0)),
    ]
