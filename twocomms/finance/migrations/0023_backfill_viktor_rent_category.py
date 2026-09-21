from django.db import migrations


def backfill_viktor_rent(apps, schema_editor):
    Category = apps.get_model('finance', 'Category')
    Transaction = apps.get_model('finance', 'Transaction')

    rent_by_company = {}
    for company_id in Transaction.objects.values_list('company_id', flat=True).distinct():
        category = Category.objects.filter(
            company_id=company_id, name__iexact='Оренда', type__in=('expense', 'both'),
        ).first()
        if category is None:
            category = Category.objects.create(
                company_id=company_id, name='Оренда', type='expense', is_system=True,
            )
        rent_by_company[company_id] = category

    for txn in Transaction.objects.filter(
        status='actual', type='expense', recurrence_rule__isnull=False,
    ).select_related('counterparty', 'recurrence_rule'):
        counterparty = txn.counterparty
        rule = txn.recurrence_rule
        if not counterparty or not rule or rule.template_type != 'expense':
            continue
        text = ' '.join((counterparty.name or '', rule.title or '', rule.template_comment or '', txn.comment or '')).casefold()
        if 'віктор' not in text and 'виктор' not in text and 'viktor' not in text:
            continue
        if not any(word in text for word in ('оренд', 'аренд', 'rent', 'utility', 'комунал')):
            continue
        txn.category_id = rent_by_company[txn.company_id].id
        txn.save(update_fields=['category'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [('finance', '0022_pension_transfer_fee')]
    operations = [migrations.RunPython(backfill_viktor_rent, noop_reverse)]
