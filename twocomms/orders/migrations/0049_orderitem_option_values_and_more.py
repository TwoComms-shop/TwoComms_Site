from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0048_checkout_tables_innodb"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderitem",
            name="option_labels",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="option_values",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="dropshipperorderitem",
            name="option_labels",
            field=models.JSONField(blank=True, default=dict, verbose_name="Назви опцій"),
        ),
        migrations.AddField(
            model_name="dropshipperorderitem",
            name="option_values",
            field=models.JSONField(blank=True, default=dict, verbose_name="Опції товару"),
        ),
    ]
