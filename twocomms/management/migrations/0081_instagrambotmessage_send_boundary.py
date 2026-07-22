from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0080_alter_instagrambotsettings_gemini_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="instagrambotmessage",
            name="send_state",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="instagrambotmessage",
            name="send_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="instagrambotmessage",
            name="send_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
