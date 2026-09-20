from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('finance', '0021_fundingsource_program_receipt')]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='economic_kind',
            field=models.CharField(
                choices=[
                    ('sale', 'Продажа'), ('operating_expense', 'Операционный расход'),
                    ('investment', 'Инвестиция'), ('grant_inflow', 'Грант'),
                    ('internal_transfer', 'Внутренний перевод'), ('owner_draw', 'Вывод владельцу'),
                    ('debt_repayment', 'Погашение долга'), ('expense_refund', 'Возврат расхода'),
                    ('pension_income', 'Пенсійна виплата'), ('transfer_fee', 'Комісія за переказ'),
                    ('personal_transfer', 'Личный перевод'), ('adjustment', 'Корректировка'),
                    ('unknown', 'Не классифицировано'),
                ], db_index=True, default='unknown', max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name='ledgerclassification',
            name='economic_kind',
            field=models.CharField(
                choices=[
                    ('sale', 'Продажа'), ('operating_expense', 'Операционный расход'),
                    ('investment', 'Инвестиция'), ('grant_inflow', 'Грант'),
                    ('internal_transfer', 'Внутренний перевод'), ('owner_draw', 'Вывод владельцу'),
                    ('debt_repayment', 'Погашение долга'), ('expense_refund', 'Возврат расхода'),
                    ('pension_income', 'Пенсійна виплата'), ('transfer_fee', 'Комісія за переказ'),
                    ('personal_transfer', 'Личный перевод'), ('adjustment', 'Корректировка'),
                    ('unknown', 'Не классифицировано'),
                ], db_index=True, default='unknown', max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='internaltransfermatch', name='fee_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=18),
        ),
        migrations.AddField(
            model_name='internaltransfermatch', name='fee_transaction',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='transfer_fee_matches', to='finance.transaction'),
        ),
    ]
