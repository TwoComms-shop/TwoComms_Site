"""Component-level obligation summaries and atomic allocations."""
from __future__ import annotations

from decimal import Decimal
import re

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import ObligationComponent, ObligationComponentSettlement, ObligationGroup, ObligationSettlement


def payment_period(value=None):
    """Validate the explicit billing month used by the component payment UI."""
    value = str(value or timezone.localdate().strftime('%Y-%m'))
    if not re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])', value) or value.startswith('0000'):
        raise ValueError('Некоректний період оплати (РРРР-ММ)')
    return value


def component_summary(component, *, period_key=None):
    if component.group.forecast_policy.get('kind') == 'grant_replenishment':
        period_key = None
    planned = component.fixed_amount or component.forecast_amount or Decimal('0')
    variable = not component.fixed_amount or component.fixed_amount <= 0
    settlements = component.settlements.all()
    if period_key is not None:
        settlements = settlements.filter(settlement__period_key=period_key)
    paid = settlements.aggregate(v=Sum('amount'))['v'] or Decimal('0')
    # A forecast is not a bill. Closing a variable component records this
    # period's actual amount without turning it into next month's fixed cost.
    if variable and paid > 0:
        planned = paid
    remaining = max(planned - paid, Decimal('0'))
    needs_amount = planned <= 0 and paid <= 0 and component.is_active
    due = component.group.recurrence_rule.next_occurrence if component.group.recurrence_rule_id else None
    overdue = bool(due and due < timezone.localdate() and remaining > 0)
    return {'id': component.id, 'name': component.name, 'planned': planned, 'paid': paid,
            'remaining': remaining, 'needs_amount': needs_amount, 'overdue': overdue,
            'is_variable': variable, 'period_key': period_key,
            'recipient_card_id': component.recipient_card_id,
            'purpose': component.payment_purpose_template}


def group_summary(group, *, period_key=None):
    if group.forecast_policy.get('kind') == 'grant_replenishment':
        period_key = None
    rows = [component_summary(c, period_key=period_key) for c in group.components.filter(is_active=True)]
    remaining = sum((r['remaining'] for r in rows), Decimal('0'))
    if remaining <= 0 and rows and not any(r['needs_amount'] for r in rows):
        status = 'paid'
    elif any(r['paid'] for r in rows):
        status = 'partial'
    elif any(r['overdue'] for r in rows):
        status = 'overdue'
    else:
        status = 'planned'
    if group.status != status and (period_key is None or period_key == payment_period()):
        ObligationGroup.objects.filter(pk=group.pk).update(status=status)
        group.status = status
    return {'id': group.id, 'title': group.title, 'status': status,
            'counterparty_id': group.counterparty_id, 'components': rows,
            'remaining': remaining, 'period_key': period_key}


@db_transaction.atomic
def allocate_settlement(*, settlement, allocations, allow_grant_replenishment=False):
    total = Decimal('0')
    for component_id, raw_amount in allocations.items():
        component = ObligationComponent.objects.select_for_update().get(pk=component_id)
        if component.group.forecast_policy.get('kind') == 'grant_replenishment' and not allow_grant_replenishment:
            raise ValueError('Повернення грантових коштів підтверджується лише у картці зобовʼязання')
        amount = Decimal(str(raw_amount))
        if amount <= 0 or component.group.company_id != settlement.company_id:
            raise ValueError('Некорректная часть обязательства')
        settlements = component.settlements.exclude(settlement=settlement)
        if settlement.period_key:
            settlements = settlements.filter(settlement__period_key=settlement.period_key)
        already = settlements.aggregate(v=Sum('amount'))['v'] or Decimal('0')
        if component.fixed_amount and component.fixed_amount > 0 and already + amount > component.fixed_amount:
            raise ValueError(f'Сумма превышает остаток компонента «{component.name}»')
        ObligationComponentSettlement.objects.update_or_create(
            settlement=settlement, component=component, defaults={'amount': amount})
        total += amount
    if total != settlement.amount:
        raise ValueError('Сумма распределения не совпадает с settlement')
    return total
