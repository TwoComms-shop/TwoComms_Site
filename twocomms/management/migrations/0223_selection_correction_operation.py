from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("management", "0222_human_reply_parts_private_documents")]

    operations = [
        migrations.AddField(
            model_name="igcommerceselectiontransition",
            name="correction_operation_id",
            field=models.UUIDField(blank=True, db_default=None, default=None, null=True, unique=True),
        ),
    ]
