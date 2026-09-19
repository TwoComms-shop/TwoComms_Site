from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class SetSessionStorageEngine(Operation):
    """Keep this migration compatible with legacy MyISAM finance tables."""

    reduces_to_sql = False
    reversible = True

    def __init__(self, engine):
        self.engine = engine

    def state_forwards(self, app_label, state):
        pass

    def state_backwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'mysql':
            schema_editor.execute(f'SET SESSION default_storage_engine={self.engine}')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == 'mysql':
            previous = 'INNODB' if self.engine.upper() == 'MYISAM' else 'MYISAM'
            schema_editor.execute(f'SET SESSION default_storage_engine={previous}')

    def describe(self):
        return f'Set session storage engine to {self.engine}'


class Migration(migrations.Migration):
    dependencies = [('finance', '0019_finance_v2_ledger')]

    operations = [
        SetSessionStorageEngine('MYISAM'),
        migrations.AddField(
            model_name='transaction', name='ownership_scope',
            field=models.CharField(
                choices=[('business', 'Бизнес'), ('personal', 'Личное'), ('mixed', 'Смешанное'),
                         ('unknown', 'Не определено')], db_index=True, default='unknown', max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='transaction', name='economic_kind',
            field=models.CharField(
                choices=[('sale', 'Продажа'), ('operating_expense', 'Операционный расход'),
                         ('investment', 'Инвестиция'), ('grant_inflow', 'Грант'),
                         ('internal_transfer', 'Внутренний перевод'), ('owner_draw', 'Вывод владельцу'),
                         ('debt_repayment', 'Погашение долга'), ('expense_refund', 'Возврат расхода'),
                         ('personal_transfer', 'Личный перевод'), ('adjustment', 'Корректировка'),
                         ('unknown', 'Не классифицировано')], db_index=True, default='unknown', max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='transaction', name='funding_source',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='classified_transactions', to='finance.fundingsource'),
        ),
        migrations.CreateModel(
            name='LedgerClassificationEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('previous', models.JSONField(blank=True, default=dict)),
                ('current', models.JSONField(blank=True, default=dict)),
                ('reason', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 to=settings.AUTH_USER_MODEL)),
                ('classification', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                     related_name='events', to='finance.ledgerclassification')),
            ],
        ),
        migrations.CreateModel(
            name='RefundLink',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18,
                                               validators=[django.core.validators.MinValueValidator(Decimal('0.01'))])),
                ('kind', models.CharField(choices=[('expense_refund', 'Возврат расхода'),
                                                   ('debt_repayment', 'Погашение долга'),
                                                   ('rent_refund', 'Возврат аренды'),
                                                   ('supplier_refund', 'Возврат поставщика')],
                                          default='expense_refund', max_length=24)),
                ('note', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 to=settings.AUTH_USER_MODEL)),
                ('original_transaction', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                           related_name='refunds_received', to='finance.transaction')),
                ('refund_transaction', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                            related_name='refund_link', to='finance.transaction')),
            ],
        ),
        SetSessionStorageEngine('INNODB'),
    ]
