from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("management", "0101_ig_payment_confirmation_review"),
        ("orders", "0051_paymentattempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="igpaymentconfirmationreview",
            name="order",
            field=models.OneToOneField(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="instagram_payment_review",
                to="orders.order",
            ),
        ),
    ]
