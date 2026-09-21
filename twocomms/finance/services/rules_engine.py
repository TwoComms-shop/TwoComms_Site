"""Двигун автоправил: умови (AND) → дії над операцією (ТЗ 10).

Правила застосовуються при імпорті, ручному створенні (як підказка),
масово до існуючих операцій. Кожне застосування пишеться в RuleApplication.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction

from ..models import AutomationRule, RuleApplication, get_default_company

# Поля операції, доступні в умовах.
FIELD_CHOICES = [
    ('comment', 'Коментар'),
    ('amount', 'Сума'),
    ('account', 'Рахунок'),
    ('counterparty', 'Контрагент'),
    ('category', 'Категорія'),
    ('project', 'Проект'),
    ('source', 'Джерело'),
    ('provider', 'Банк / провайдер'),
    ('currency', 'Валюта'),
    ('type', 'Тип операції'),
]

OPERATOR_CHOICES = [
    ('contains', 'містить'),
    ('not_contains', 'не містить'),
    ('equals', 'дорівнює'),
    ('not_equals', 'не дорівнює'),
    ('starts_with', 'починається з'),
    ('ends_with', 'закінчується на'),
    ('gt', 'більше'),
    ('lt', 'менше'),
    ('is_empty', 'порожнє'),
    ('is_not_empty', 'не порожнє'),
]

ACTION_CHOICES = [
    ('set_category', 'Встановити категорію'),
    ('set_project', 'Встановити проект'),
    ('set_counterparty', 'Встановити контрагента'),
    ('add_tag', 'Додати тег'),
    ('set_comment', 'Змінити коментар'),
    ('mark_planned', 'Позначити плановою'),
    ('mark_actual', 'Позначити фактичною'),
    ('exclude_from_reports', 'Виключити зі звітів'),
]


def _field_value(txn, field):
    if field == 'comment':
        return txn.comment or ''
    if field == 'amount':
        return txn.amount
    if field == 'account':
        return txn.account_id
    if field == 'counterparty':
        return txn.counterparty_id
    if field == 'category':
        return txn.category_id
    if field == 'project':
        return txn.project_id
    if field == 'source':
        return txn.source or ''
    if field == 'provider':
        external = txn.external_data or {}
        provider = external.get('provider') or external.get('provider_name')
        if provider:
            return provider
        account = getattr(txn, 'account', None)
        integration = getattr(account, 'integration', None) if account else None
        return integration.provider if integration else ''
    if field == 'currency':
        return txn.currency or ''
    if field == 'type':
        return txn.type or ''
    return None


def _to_decimal(value):
    try:
        return Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _check_condition(txn, cond):
    field = cond.get('field')
    op = cond.get('operator')
    value = cond.get('value', '')
    actual = _field_value(txn, field)

    if op == 'is_empty':
        return actual in (None, '', 0)
    if op == 'is_not_empty':
        return actual not in (None, '', 0)

    if field in ('amount',):
        a = _to_decimal(actual)
        b = _to_decimal(value)
        if a is None or b is None:
            return False
        if op == 'gt':
            return a > b
        if op == 'lt':
            return a < b
        if op == 'equals':
            return a == b
        if op == 'not_equals':
            return a != b
        return False

    # Поля-ідентифікатори (account/category/...) порівнюємо як рядки id.
    actual_str = str(actual or '').lower()
    value_str = str(value or '').lower()
    if op == 'contains':
        return value_str in actual_str
    if op == 'not_contains':
        return value_str not in actual_str
    if op == 'equals':
        return actual_str == value_str
    if op == 'not_equals':
        return actual_str != value_str
    if op == 'starts_with':
        return actual_str.startswith(value_str)
    if op == 'ends_with':
        return actual_str.endswith(value_str)
    return False


def rule_matches(txn, rule):
    if rule.transaction_type and rule.transaction_type != txn.type:
        return False
    conditions = rule.conditions or []
    if not conditions:
        return False
    # AND між умовами (MVP).
    return all(_check_condition(txn, c) for c in conditions)


def _apply_actions(txn, rule, *, user, source):
    company = txn.company
    before = {'category': txn.category_id, 'project': txn.project_id,
              'counterparty': txn.counterparty_id, 'comment': txn.comment,
              'status': txn.status, 'excluded': txn.excluded_from_reports}
    changed = False
    tags_to_add = []
    for action in (rule.actions or []):
        a = action.get('action')
        value = action.get('value')
        overwrite = action.get('overwrite', True)
        if a == 'set_category':
            if overwrite or not txn.category_id:
                txn.category = company.categories.filter(id=value).first()
                changed = True
        elif a == 'set_project':
            if overwrite or not txn.project_id:
                txn.project = company.projects.filter(id=value).first()
                changed = True
        elif a == 'set_counterparty':
            if overwrite or not txn.counterparty_id:
                txn.counterparty = company.counterparties.filter(id=value).first()
                changed = True
        elif a == 'add_tag':
            tag = company.tags.filter(id=value).first()
            if tag:
                tags_to_add.append(tag)
        elif a == 'set_comment':
            if overwrite or not txn.comment:
                txn.comment = value or ''
                changed = True
        elif a == 'mark_planned':
            txn.status = 'planned'; changed = True
        elif a == 'mark_actual':
            txn.status = 'actual'; changed = True
        elif a == 'exclude_from_reports':
            txn.excluded_from_reports = True; changed = True

    if changed:
        txn.save()
    for tag in tags_to_add:
        txn.tags.add(tag)
    if changed or tags_to_add:
        after = {'category': txn.category_id, 'project': txn.project_id,
                 'counterparty': txn.counterparty_id, 'comment': txn.comment,
                 'status': txn.status, 'excluded': txn.excluded_from_reports}
        RuleApplication.objects.create(rule=rule, transaction=txn, before=before,
                                       after=after, source=source,
                                       created_by=user if getattr(user, 'is_authenticated', False) else None)
        AutomationRule.objects.filter(pk=rule.pk).update(applied_count=rule.applied_count + 1)
        return True
    return False


def apply_rules_to_transaction(txn, *, user=None, source='auto'):
    """Застосовує всі активні правила за пріоритетом до однієї операції."""
    company = txn.company
    rules = company.automation_rules.filter(is_enabled=True).order_by('priority', 'id')
    applied = []
    for rule in rules:
        if rule_matches(txn, rule):
            if _apply_actions(txn, rule, user=user, source=source):
                applied.append(rule.id)
    return applied


def preview_apply_to_existing(rule, period_qs):
    """Передогляд: які операції зміняться (без збереження)."""
    preview = []
    for txn in period_qs[:500]:
        if rule_matches(txn, rule):
            preview.append({
                'id': txn.id,
                'comment': txn.comment,
                'amount': str(txn.amount),
                'old_category': txn.category.name if txn.category else '—',
            })
    return preview


@db_transaction.atomic
def apply_to_existing(rule, period_qs, *, user):
    count = 0
    for txn in period_qs:
        if rule_matches(txn, rule):
            if _apply_actions(txn, rule, user=user, source='bulk'):
                count += 1
    return count
