from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0097_analysis_reconcile_rollout_cutoff"),
    ]

    operations = [
        migrations.AddField(
            model_name="instagrambotsettings",
            name="reply_permission_epoch",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="igclient",
            name="reply_permission_epoch",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
