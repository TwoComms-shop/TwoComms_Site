from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0049_orderitem_option_values_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='checkout_idempotency_key',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                null=True,
                unique=True,
            ),
        ),
    ]
