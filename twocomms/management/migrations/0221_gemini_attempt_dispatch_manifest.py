from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0220_bot_instruction_reviewed_source")]

    operations = [
        migrations.AddField(
            model_name="geminirequestattempt",
            name="dispatch_manifest",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
    ]
