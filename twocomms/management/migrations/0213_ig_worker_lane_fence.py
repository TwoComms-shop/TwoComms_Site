from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0212_human_reply_command"),
    ]

    operations = [
        migrations.CreateModel(
            name="IgWorkerLaneState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("lane_key", models.CharField(max_length=64, unique=True)),
                ("owner_kind", models.CharField(blank=True, default="", max_length=32)),
                ("owner_token", models.CharField(blank=True, default="", max_length=64)),
                ("generation", models.PositiveBigIntegerField(default=0)),
                ("owner_started_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("lease_until", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("claim_frozen", models.BooleanField(default=False)),
                ("freeze_generation", models.PositiveBigIntegerField(default=0)),
                ("frozen_at", models.DateTimeField(blank=True, null=True)),
                ("freeze_reason", models.CharField(blank=True, default="", max_length=128)),
                ("recovery_deadline_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["lane_key"]},
        ),
        migrations.AddField(
            model_name="igconversationanalysisjob",
            name="claim_generation",
            field=models.PositiveBigIntegerField(default=0, db_index=True),
        ),
    ]
