from django.db import migrations


def _category(apps, company, name, category_type='expense'):
    Category = apps.get_model('finance', 'Category')
    return (Category.objects.filter(company_id=company.id, name__iexact=name,
                                    type__in=(category_type, 'both'))
            .order_by('id').first() or Category.objects.create(
                company_id=company.id, name=name, type=category_type, is_system=True,
            ))


def _move_category(apps, source, target):
    if not source or not target or source.id == target.id:
        return
    Category = apps.get_model('finance', 'Category')
    Transaction = apps.get_model('finance', 'Transaction')
    RecurrenceRule = apps.get_model('finance', 'RecurrenceRule')
    BudgetPlan = apps.get_model('finance', 'BudgetPlan')
    AutomationRule = apps.get_model('finance', 'AutomationRule')

    Transaction.objects.filter(category_id=source.id).update(category_id=target.id)
    RecurrenceRule.objects.filter(template_category_id=source.id).update(template_category_id=target.id)
    BudgetPlan.objects.filter(category_id=source.id).update(category_id=target.id)
    Category.objects.filter(parent_id=source.id).update(parent_id=target.id)

    # Existing rule actions store category ids as strings in JSON.
    for rule in AutomationRule.objects.filter(company_id=source.company_id):
        actions = list(rule.actions or [])
        changed = False
        for action in actions:
            if (action.get('action') == 'set_category'
                    and str(action.get('value')) == str(source.id)):
                action['value'] = str(target.id)
                changed = True
        if changed:
            rule.actions = actions
            rule.save(update_fields=['actions', 'updated_at'])

    FinancialMetric = apps.get_model('finance', 'FinancialMetric')
    for metric in FinancialMetric.objects.filter(company_id=source.company_id):
        if metric.income_categories.filter(pk=source.id).exists():
            metric.income_categories.remove(source)
            metric.income_categories.add(target)
        if metric.expense_categories.filter(pk=source.id).exists():
            metric.expense_categories.remove(source)
            metric.expense_categories.add(target)
    source.delete()


def _merge_punctuation_duplicate(apps, company, canonical_name, aliases):
    """Merge only exact name variants caused by apostrophe/typography drift."""
    Category = apps.get_model('finance', 'Category')
    target = (Category.objects.filter(company_id=company.id, name__iexact=canonical_name)
              .order_by('id').first())
    if not target:
        return
    for alias in aliases:
        source = (Category.objects.filter(company_id=company.id, name=alias)
                  .exclude(pk=target.id).order_by('id').first())
        if source:
            _move_category(apps, source, target)


def _novapay_counterparty(apps, company):
    Counterparty = apps.get_model('finance', 'Counterparty')
    # Prefer a previously created NovaPay record, while avoiding the unrelated
    # Nova Poshta logistics counterparty used by the old rule.
    cp = (Counterparty.objects.filter(company_id=company.id)
          .filter(name__in=('NovaPay', 'Novapay', 'НоваПей', 'Нова Пей'))
          .order_by('id').first())
    if cp:
        return cp
    return Counterparty.objects.create(
        company_id=company.id, name='NovaPay', type='marketplace',
    )


def normalize_categories_and_novapay(apps, schema_editor):
    Company = apps.get_model('finance', 'Company')
    Category = apps.get_model('finance', 'Category')
    Transaction = apps.get_model('finance', 'Transaction')
    AutomationRule = apps.get_model('finance', 'AutomationRule')

    for company in Company.objects.all():
        # One business category covers both office rent and utilities.  Keep
        # «Оренда» as the stable label used by existing reports and rules.
        rent = _category(apps, company, 'Оренда', 'expense')
        for alias in ('Комунальні послуги', 'Комуналка', 'Аренда',
                      'Оренда та комунальні послуги'):
            source = Category.objects.filter(company_id=company.id, name__iexact=alias).exclude(pk=rent.id).first()
            if source:
                _move_category(apps, source, rent)

        # «Їжа та продукти» and «Продукти» represented the same bucket.
        products = _category(apps, company, 'Продукти', 'expense')
        for alias in ('Їжа та продукти', 'Їжа і продукти', 'Еда и продукты'):
            source = Category.objects.filter(company_id=company.id, name__iexact=alias).exclude(pk=products.id).first()
            if source:
                _move_category(apps, source, products)

        # Merge the two common apostrophe spellings of the same category.
        _merge_punctuation_duplicate(apps, company, "Здоров'я", ("Здоровʼя", "Здоров’я"))

        sales = _category(apps, company, 'Продажі', 'income')
        novapay = _novapay_counterparty(apps, company)
        matching_rules = []
        for rule in AutomationRule.objects.filter(company_id=company.id):
            conditions = rule.conditions or []
            text = ' '.join(str(c.get('value') or '') for c in conditions).casefold()
            if not any(token in text for token in ('новапей', 'novapay', 'nova pay')):
                continue
            matching_rules.append(rule)
            actions = list(rule.actions or [])
            has_category = False
            has_counterparty = False
            for action in actions:
                if action.get('action') == 'set_category':
                    action['value'] = str(sales.id)
                    has_category = True
                if action.get('action') == 'set_counterparty':
                    action['value'] = str(novapay.id)
                    has_counterparty = True
            if not has_category:
                actions.append({'action': 'set_category', 'value': str(sales.id), 'overwrite': True})
            if not has_counterparty:
                actions.append({'action': 'set_counterparty', 'value': str(novapay.id), 'overwrite': True})
            rule.transaction_type = 'income'
            rule.actions = actions
            rule.save(update_fields=['transaction_type', 'actions', 'updated_at'])

        if not matching_rules:
            AutomationRule.objects.create(
                company_id=company.id,
                name='NovaPay → Продажі',
                transaction_type='income',
                conditions=[{'field': 'comment', 'operator': 'contains', 'value': 'НоваПей'}],
                actions=[
                    {'action': 'set_category', 'value': str(sales.id), 'overwrite': True},
                    {'action': 'set_counterparty', 'value': str(novapay.id), 'overwrite': True},
                ],
                priority=50,
            )

        # Repair historical rows as part of the same rollout.  Matching uses
        # the bank description and raw provider fields, because older imports
        # did not consistently persist a separate NovaPay provider marker.
        for txn in Transaction.objects.filter(company_id=company.id, type='income').select_related('category'):
            external = txn.external_data or {}
            raw = ' '.join(str(value) for value in external.values() if isinstance(value, (str, int, float)))
            text = f'{txn.comment or ""} {raw}'.casefold()
            if not any(token in text for token in ('новапей', 'novapay', 'nova pay')):
                continue
            txn.category_id = sales.id
            txn.counterparty_id = novapay.id
            txn.economic_kind = 'sale'
            txn.ownership_scope = 'business'
            txn.save(update_fields=['category', 'counterparty', 'economic_kind', 'ownership_scope'])


class Migration(migrations.Migration):
    dependencies = [('finance', '0023_backfill_viktor_rent_category')]
    operations = [migrations.RunPython(normalize_categories_and_novapay, migrations.RunPython.noop)]
