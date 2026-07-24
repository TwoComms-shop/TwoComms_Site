from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0098_reply_permission_epochs"),
    ]

    operations = [
        migrations.AddField(
            model_name="instagrambotsettings",
            name="opt_out_backfill_cursor",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
