import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def preserve_snapshot_analysis_time(apps, schema_editor):
    Snapshot = apps.get_model("management", "IgConversationAnalysisSnapshot")
    Snapshot.objects.filter(analyzed_at__isnull=True).update(
        analyzed_at=models.F("created_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0094_notification_outbox_innodb"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="instagrambotsettings",
            name="analysis_reconcile_cursor",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="igclient",
            name="opt_out_message_id",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="igclient",
            name="opted_in_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="igclient",
            name="opted_in_by",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ig_manual_opt_ins",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="igclient",
            name="opted_out_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="analyzed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(
            preserve_snapshot_analysis_time,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="igconversationanalysissnapshot",
            name="analyzed_at",
            field=models.DateTimeField(db_index=True, default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="required_state_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="key_alias",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="reasoning_level",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="reasoning_policy_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="thoughts_tokens",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="candidates_tokens",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="IgConversationAnalysisJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("watermark_message_id", models.PositiveBigIntegerField(default=0)),
                ("analyzed_watermark_message_id", models.PositiveBigIntegerField(default=0)),
                ("revision", models.PositiveBigIntegerField(default=0)),
                ("analyzed_revision", models.PositiveBigIntegerField(default=0)),
                ("claimed_watermark_message_id", models.PositiveBigIntegerField(default=0)),
                ("claimed_revision", models.PositiveBigIntegerField(default=0)),
                ("status", models.CharField(choices=[("pending", "Очікує аналізу"), ("processing", "Аналізується"), ("done", "Проаналізовано"), ("failed", "Помилка аналізу"), ("skipped", "Аналіз пропущено")], db_index=True, default="pending", max_length=16)),
                ("due_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("next_attempt_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("lease_token", models.CharField(blank=True, default="", max_length=40)),
                ("lease_until", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, default="", max_length=1000)),
                ("skip_reason", models.CharField(blank=True, default="", max_length=64)),
                ("trigger", models.CharField(blank=True, default="message", max_length=32)),
                ("analysis_model", models.CharField(blank=True, default="", max_length=80)),
                ("analysis_prompt_version", models.CharField(blank=True, default="", max_length=40)),
                ("required_state_fingerprint", models.CharField(blank=True, default="", max_length=64)),
                ("key_alias", models.CharField(blank=True, default="", max_length=32)),
                ("reasoning_task", models.CharField(blank=True, default="", max_length=64)),
                ("reasoning_level", models.CharField(blank=True, default="", max_length=16)),
                ("reasoning_policy_version", models.CharField(blank=True, default="", max_length=32)),
                ("thoughts_tokens", models.PositiveIntegerField(default=0)),
                ("candidates_tokens", models.PositiveIntegerField(default=0)),
                ("analysis_latency_ms", models.PositiveIntegerField(default=0)),
                ("analyzed_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.OneToOneField(db_constraint=False, on_delete=django.db.models.deletion.CASCADE, related_name="analysis_job", to="management.igclient")),
            ],
            options={
                "verbose_name": "Завдання аналізу IG-діалогу",
                "verbose_name_plural": "Завдання аналізу IG-діалогів",
                "ordering": ["due_at", "id"],
                "indexes": [models.Index(fields=["status", "next_attempt_at", "due_at"], name="ig_analysis_job_due")],
            },
        ),
    ]
