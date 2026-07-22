from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("management", "0082_igfollowuptask_retry_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="IgPollCursor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("conversation_id", models.CharField(max_length=255, unique=True)),
                ("last_message_id", models.CharField(blank=True, default="", max_length=255)),
                ("last_message_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "IG polling cursor",
                "verbose_name_plural": "IG polling cursors",
            },
        ),
    ]
