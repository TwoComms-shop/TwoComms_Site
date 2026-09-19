"""Component-level obligation summaries and atomic allocations."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import ObligationComponent, ObligationComponentSettlement, ObligationGroup, ObligationSettlement


def component_summary(component):
    planned = component.fixed_amount or component.forecast_amount or Decimal('0')
    paid = component.settlements.aggregate(v=Sum('amount'))['v'] or Decimal('0')
    remaining = max(planned - paid, Decimal('0'))
    due = component.group.recurrence_rule.next_occurrence if component.group.recurrence_rule_id else None
    overdue = bool(due and due < timezone.localdate() and remaining > 0)
    return {'id': component.id, 'name': component.name, 'planned': planned, 'paid': paid,
            'remaining': remaining, 'overdue': overdue,
            'recipient_card_id': component.recipient_card_id,
            'purpose': component.payment_purpose_template}


def group_summary(group):
    rows = [component_summary(c) for c in group.components.filter(is_active=True)]
    remaining = sum((r['remaining'] for r in rows), Decimal('0'))
    if remaining <= 0 and rows:
        status = 'paid'
    elif any(r['paid'] for r in rows):
        status = 'partial'
    elif any(r['overdue'] for r in rows):
        status = 'overdue'
    else:
        status = 'planned'
    if group.status != status:
        ObligationGroup.objects.filter(pk=group.pk).update(status=status)
        group.status = status
    return {'id': group.id, 'title': group.title, 'status': status,
            'counterparty_id': group.counterparty_id, 'components': rows,
            'remaining': remaining}


@db_transaction.atomic
def allocate_settlement(*, settlement, allocations):
    total = Decimal('0')
    for component_id, raw_amount in allocations.items():
        component = ObligationComponent.objects.select_for_update().get(pk=component_id)
        amount = Decimal(str(raw_amount))
        if amount <= 0 or component.group.company_id != settlement.company_id:
            raise ValueError('Некорректная часть обязательства')
        already = component.settlements.aggregate(v=Sum('amount'))['v'] or Decimal('0')
        planned = component.fixed_amount or component.forecast_amount or Decimal('0')
        if already + amount > planned:
            raise ValueError(f'Сумма превышает остаток компонента «{component.name}»')
        ObligationComponentSettlement.objects.update_or_create(
            settlement=settlement, component=component, defaults={'amount': amount})
        total += amount
    if total != settlement.amount:
        raise ValueError('Сумма распределения не совпадает с settlement')
    return total
