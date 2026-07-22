from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0081_instagrambotmessage_send_boundary"),
    ]

    operations = [
        migrations.AddField(
            model_name="igfollowuptask",
            name="attempt_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="igfollowuptask",
            name="next_attempt_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="igfollowuptask",
            name="last_error",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
