import django.utils.timezone
from django.db import migrations, models


def quarantine_pre_cutoff_reconcile_jobs(apps, schema_editor):
    Settings = apps.get_model("management", "InstagramBotSettings")
    Job = apps.get_model("management", "IgConversationAnalysisJob")
    settings_obj = Settings.objects.order_by("pk").first()
    if not settings_obj:
        return
    Job.objects.filter(
        trigger="reconcile",
        revision__gt=models.F("analyzed_revision"),
        created_at__lt=settings_obj.analysis_reconcile_after,
        status__in=["pending", "processing", "failed"],
    ).update(
        status="skipped",
        skip_reason="historical_backfill_blocked",
        lease_token="",
        lease_until=None,
        claimed_watermark_message_id=0,
        claimed_revision=0,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0096_analysis_tables_innodb"),
    ]

    operations = [
        migrations.AddField(
            model_name="instagrambotsettings",
            name="analysis_backfill_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="instagrambotsettings",
            name="analysis_reconcile_after",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="igdeal",
            name="order_truth_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            quarantine_pre_cutoff_reconcile_jobs,
            migrations.RunPython.noop,
        ),
    ]
