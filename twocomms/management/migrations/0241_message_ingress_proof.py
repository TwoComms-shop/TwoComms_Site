from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0240_current_hoodie_fit_storage")]
    operations = [migrations.AddField(
        model_name="instagrambotmessage", name="ingress_proof",
        field=models.JSONField(default=dict, db_default={}, blank=True),
    )]
