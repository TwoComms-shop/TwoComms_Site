"""Explicit repayment of a temporary grant withdrawal, without new grant income."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    FundingAllocation, FundingSource, ObligationComponent, ObligationGroup,
    ObligationSettlement, Transaction,
)
from . import ledger_v2, obligations_v2


def protected_meaning(txn):
    returned = txn.funding_allocations.filter(allocation_type='returned').first()
    if returned:
        return 'internal_transfer', returned.funding_source_id
    claim = txn.company.obligation_groups.filter(
        forecast_policy__kind='grant_replenishment', forecast_policy__withdrawal_id=txn.pk,
    ).first()
    if claim:
        return 'owner_draw', claim.forecast_policy['source_id']
    return None


def protect_bank_edit(txn, fields=None, *, deleting=False):
    if not protected_meaning(txn):
        return
    error = 'Операція повʼязана з поверненням гранту: суму, рахунки, дату та статус змінювати не можна'
    if deleting:
        raise ValueError(error)
    for key in ('type', 'status', 'amount', 'amount_base', 'currency', 'exchange_rate', 'account',
                'account_id', 'to_account', 'to_account_id', 'to_amount', 'date_actual', 'excluded_from_reports'):
        if key not in fields:
            continue
        old, new = getattr(txn, key), fields[key]
        if key == 'date_actual' and old and new and old.replace(second=0, microsecond=0) == new.replace(second=0, microsecond=0):
            fields[key] = old
            continue
        if old != new:
            raise ValueError(error)


def _candidates(group):
    policy = group.forecast_policy
    amount = sum((c.fixed_amount or Decimal('0') for c in group.components.all()), Decimal('0'))
    withdrawal = Transaction.objects.get(pk=policy['withdrawal_id'])
    return Transaction.objects.filter(
        Q(type=Transaction.TYPE_TRANSFER, to_account_id=policy['account_id'], to_amount=amount)
        | Q(type=Transaction.TYPE_INCOME, account_id=policy['account_id'], amount=amount),
        company=group.company, status=Transaction.STATUS_ACTUAL,
        currency=withdrawal.currency,
        date_actual__gte=withdrawal.date_actual,
    ).exclude(pk=withdrawal.pk).exclude(settlements__isnull=False).exclude(
        economic_kind='grant_inflow',
    ).select_related('account', 'to_account').order_by('-date_actual', '-id')


def pending_rows(company, *, source=None):
    groups = company.obligation_groups.filter(
        is_active=True, forecast_policy__kind='grant_replenishment',
    ).exclude(status='paid')
    if source:
        groups = groups.filter(forecast_policy__source_id=source.id)
    rows = []
    for group in groups.prefetch_related('components'):
        rows.append({'id': group.id, 'title': group.title,
                     'amount': sum((c.fixed_amount or Decimal('0') for c in group.components.all()), Decimal('0')),
                     'candidates': list(_candidates(group))})
    return rows


@transaction.atomic
def confirm_return(group, payment, *, user=None):
    group = ObligationGroup.objects.select_for_update().get(pk=group.pk)
    policy = group.forecast_policy
    if policy.get('kind') != 'grant_replenishment':
        raise ValueError('Це не зобовʼязання з повернення грантових коштів')
    payment = Transaction.objects.select_for_update().get(pk=payment.pk)
    if group.status == 'paid':
        if policy.get('repayment_id') == payment.id:
            return group
        raise ValueError('Повернення вже підтверджене')
    if not _candidates(group).filter(pk=payment.pk).exists():
        raise ValueError('Оберіть фактичне повернення повної суми на грантовий рахунок після виведення')
    source = FundingSource.objects.select_for_update().get(pk=policy['source_id'], company=group.company)
    components = list(group.components.filter(is_active=True))
    amount = sum((c.fixed_amount or Decimal('0') for c in components), Decimal('0'))
    settlement = ObligationSettlement.objects.create(
        company=group.company, payment=payment, amount=amount, currency=payment.currency,
        period_key=timezone.localtime(payment.date_actual).strftime('%Y-%m'),
        period_label='Повернення коштів гранту',
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    obligations_v2.allocate_settlement(
        settlement=settlement, allocations={c.id: c.fixed_amount for c in components},
        allow_grant_replenishment=True,
    )
    FundingAllocation.objects.create(
        funding_source=source, transaction=payment, amount=amount, allocation_type='returned',
        note=f'Повернення тимчасового виведення #{policy["withdrawal_id"]}',
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    ledger_v2.classify_transaction(
        payment, user=user, economic_kind='internal_transfer', funding_source=source,
        note='Повернення тимчасово виведених коштів гранту; не новий транш і не дохід',
    )
    group.status = 'paid'
    group.forecast_policy = {**policy, 'repayment_id': payment.id}
    group.save(update_fields=['status', 'forecast_policy'])
    return group


@transaction.atomic
def register_withdrawal(source, withdrawal, *, user=None):
    withdrawal = Transaction.objects.select_for_update().get(pk=withdrawal.pk)
    if (withdrawal.company_id != source.company_id or withdrawal.status != Transaction.STATUS_ACTUAL
            or withdrawal.type != Transaction.TYPE_TRANSFER or withdrawal.economic_kind != 'owner_draw'
            or withdrawal.account_id == withdrawal.to_account_id):
        raise ValueError('Потрібне підтверджене виведення грантових коштів на власний рахунок')
    if source.receipt_transaction_id and source.receipt_transaction.account_id != withdrawal.account_id:
        raise ValueError('Виведення має бути з рахунку цього гранту')
    group, _ = ObligationGroup.objects.get_or_create(
        company=source.company, forecast_policy__kind='grant_replenishment',
        forecast_policy__withdrawal_id=withdrawal.id,
        defaults={'title': f'Повернення тимчасово виведених коштів {source.name}', 'type': 'income', 'role': 'refund',
                  'forecast_policy': {'kind': 'grant_replenishment', 'source_id': source.id,
                                      'withdrawal_id': withdrawal.id, 'account_id': withdrawal.account_id},
                  'notes': 'Дата повернення ще не визначена. Закривається лише підтвердженням фактичного повернення на грантовий рахунок.'},
    )
    ObligationComponent.objects.get_or_create(
        group=group, name='Повернути на грантовий рахунок', defaults={'fixed_amount': withdrawal.amount},
    )
    FundingAllocation.objects.update_or_create(
        funding_source=source, transaction=withdrawal, allocation_type='reserved',
        defaults={'amount': withdrawal.amount, 'note': 'Тимчасово виведені кошти, очікується повернення'},
    )
    ledger_v2.classify_transaction(
        withdrawal, user=user, economic_kind='owner_draw', ownership_scope='personal', funding_source=source,
        note='Тимчасове виведення грантових коштів; створено зобовʼязання повернення',
    )
    return group
