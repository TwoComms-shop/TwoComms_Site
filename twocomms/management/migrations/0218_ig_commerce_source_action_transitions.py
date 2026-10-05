from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("management", "0217_backfill_reviewed_no_reply_owner")]

    operations = [
        migrations.AlterField(
            model_name="igcommerceselectiontransition",
            name="source_message",
            field=models.ForeignKey(
                db_constraint=False,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="commerce_selection_transitions",
                to="management.instagrambotmessage",
            ),
        ),
    ]
