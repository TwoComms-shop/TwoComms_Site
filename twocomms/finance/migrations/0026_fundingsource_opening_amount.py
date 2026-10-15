from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('finance', '0025_counterparty_classification_policies')]

    operations = [
        migrations.AddField(
            model_name='fundingsource', name='opening_amount',
            field=models.DecimalField(
                decimal_places=2, max_digits=18, default=Decimal('0'),
                help_text='Підтверджені кошти гранту на початок обліку; не новий транш',
            ),
        ),
        migrations.AlterField(
            model_name='fundingallocation', name='allocation_type',
            field=models.CharField(max_length=16, default='spent', choices=[
                ('received', 'Поступление'), ('spent', 'Расход'), ('reserved', 'Резерв'),
                ('returned', 'Повернення виведених коштів'),
            ]),
        ),
    ]
