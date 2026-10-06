from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('orders', '0058_paymentattempt_checkout_series')]

    operations = [
        migrations.AddField(
            model_name='order', name='delivery_method',
            field=models.CharField(
                choices=[('nova_poshta', 'Нова Пошта'), ('manual', 'Адреса вручну'), ('handover', 'Передача / самовивіз')],
                default='nova_poshta', max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='order', name='handover_details',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='orderitem', name='item_kind',
            field=models.CharField(
                choices=[('clothing', 'Одяг'), ('dtf_film', 'DTF плівка')],
                default='clothing', max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='orderitem', name='film_length_m',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True),
        ),
    ]
