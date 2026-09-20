from django.db import migrations, models
import django.db.models.deletion


def set_known_uvf_program_metadata(apps, schema_editor):
    FundingSource = apps.get_model('finance', 'FundingSource')
    FundingSource.objects.filter(
        name='УВФ', program_total_amount__isnull=True,
    ).update(program_total_amount='1500000.00', stage_label='Етап 4')


def unset_known_uvf_program_metadata(apps, schema_editor):
    FundingSource = apps.get_model('finance', 'FundingSource')
    FundingSource.objects.filter(
        name='УВФ', program_total_amount='1500000.00', stage_label='Етап 4',
    ).update(program_total_amount=None, stage_label='')


class Migration(migrations.Migration):
    dependencies = [('finance', '0020_finance_v2_classification_audit')]

    operations = [
        migrations.AddField(
            model_name='fundingsource', name='program_total_amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fundingsource', name='stage_label',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='fundingsource', name='receipt_transaction',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='funding_source_receipts', to='finance.transaction'),
        ),
        migrations.RunPython(set_known_uvf_program_metadata, unset_known_uvf_program_metadata),
    ]
