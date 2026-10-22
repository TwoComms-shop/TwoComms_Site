from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0246_live_communication_core_default")]

    operations = [
        migrations.AddField(
            model_name="igconversationanalysissnapshot",
            name="conversion_audit",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
