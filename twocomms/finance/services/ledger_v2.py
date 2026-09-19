"""Reviewable ledger classification and funding operations."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import (
    BalanceReconciliation, ClassificationReview, FundingAllocation, FundingSource,
    InternalTransferMatch, LedgerClassification, LedgerClassificationEvent,
    RefundLink, Transaction,
)


def classify_transaction(txn, *, user=None, ownership_scope='unknown', economic_kind='unknown',
                         confidence=Decimal('100'), note='', source='manual', funding_source=None):
    """Upsert explicit meaning without changing the original bank row."""
    obj, created = LedgerClassification.objects.get_or_create(
        transaction=txn,
        defaults={
            'ownership_scope': ownership_scope,
            'economic_kind': economic_kind,
            'confidence': Decimal(str(confidence)),
            'note': note or '', 'source': source,
            'updated_by': user if getattr(user, 'is_authenticated', False) else None,
        },
    )
    previous = {
        'ownership_scope': obj.ownership_scope, 'economic_kind': obj.economic_kind,
        'note': obj.note, 'source': obj.source,
        'funding_source_id': obj.transaction.funding_source_id,
    }
    if not created:
        obj.ownership_scope = ownership_scope
        obj.economic_kind = economic_kind
        obj.confidence = Decimal(str(confidence))
        obj.note = note or ''
        obj.source = source
        obj.updated_by = user if getattr(user, 'is_authenticated', False) else None
        obj.save()
    txn.ownership_scope = ownership_scope
    txn.economic_kind = economic_kind
    txn.funding_source = funding_source
    txn.save(update_fields=['ownership_scope', 'economic_kind', 'funding_source'])
    LedgerClassificationEvent.objects.create(
        classification=obj, previous={} if created else previous,
        current={'ownership_scope': ownership_scope, 'economic_kind': economic_kind,
                 'confidence': str(confidence), 'note': note or '', 'source': source,
                 'funding_source_id': getattr(funding_source, 'id', None)},
        changed_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    return obj


def create_transfer_suggestion(company, source_txn, destination_txn, *, confidence=Decimal('75'), note=''):
    if source_txn.company_id != company.id or destination_txn.company_id != company.id:
        raise ValueError('Операции должны принадлежать одной компании')
    if source_txn.id == destination_txn.id or source_txn.amount <= 0 or destination_txn.amount <= 0:
        raise ValueError('Некорректные операции для перевода')
    amount = min(source_txn.amount, destination_txn.amount)
    return InternalTransferMatch.objects.update_or_create(
        source_transaction=source_txn,
        destination_transaction=destination_txn,
        defaults={
            'company': company,
            'source_account': source_txn.account,
            'destination_account': destination_txn.account,
            'amount': amount,
            'confidence': Decimal(str(confidence)),
            'status': 'suggested',
        },
    )[0]


@db_transaction.atomic
def confirm_transfer(match, *, user=None):
    if match.status == 'confirmed':
        return match
    match.status = 'confirmed'
    match.confirmed_by = user if getattr(user, 'is_authenticated', False) else None
    match.confirmed_at = timezone.now()
    match.save(update_fields=['status', 'confirmed_by', 'confirmed_at'])
    classify_transaction(match.source_transaction, user=user, ownership_scope='unknown', economic_kind='internal_transfer', source='review')
    classify_transaction(match.destination_transaction, user=user, ownership_scope='unknown', economic_kind='internal_transfer', source='review')
    return match


def funding_summary(source):
    received = source.allocations.filter(allocation_type='received').aggregate(v=Sum('amount'))['v'] or Decimal('0')
    spent = source.allocations.filter(allocation_type='spent').aggregate(v=Sum('amount'))['v'] or Decimal('0')
    reserved = source.allocations.filter(allocation_type='reserved').aggregate(v=Sum('amount'))['v'] or Decimal('0')
    received = max(received, source.received_amount)
    return {'received': received, 'spent': spent, 'reserved': reserved,
            'available': received - spent - reserved}


def allocate_funding(*, source, txn, amount, allocation_type='spent', user=None, note=''):
    amount = Decimal(str(amount))
    if amount <= 0 or amount > txn.amount:
        raise ValueError('Сумма распределения должна быть положительной и не превышать операцию')
    allocated = source.allocations.filter(transaction=txn).aggregate(v=Sum('amount'))['v'] or Decimal('0')
    if allocated + amount > txn.amount:
        raise ValueError('Распределение превышает сумму операции')
    existing = FundingAllocation.objects.filter(
        funding_source=source, transaction=txn, allocation_type=allocation_type,
    ).first()
    if existing:
        existing.amount += amount
        existing.save(update_fields=['amount'])
        return existing
    return FundingAllocation.objects.create(
        funding_source=source, transaction=txn, amount=amount,
        allocation_type=allocation_type,
        created_by=user if getattr(user, 'is_authenticated', False) else None,
        note=note or '',
    )


@db_transaction.atomic
def link_refund(*, refund_transaction, original_transaction, amount=None, kind='expense_refund',
                user=None, note=''):
    if refund_transaction.company_id != original_transaction.company_id:
        raise ValueError('Операции должны принадлежать одной компании')
    if refund_transaction.type != Transaction.TYPE_INCOME:
        raise ValueError('Возврат должен быть входящей операцией')
    value = Decimal(str(amount if amount is not None else refund_transaction.amount))
    if value <= 0 or value > refund_transaction.amount or value > original_transaction.amount:
        raise ValueError('Некорректная сумма возврата')
    link = RefundLink.objects.create(
        refund_transaction=refund_transaction, original_transaction=original_transaction,
        amount=value, kind=kind, note=note or '',
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    classify_transaction(refund_transaction, user=user, ownership_scope='business',
                         economic_kind='expense_refund', source='review', note=note)
    return link


def build_transfer_reviews(company, *, days=180, limit=100):
    """Create review-only suggestions for likely cash/card transfers."""
    since = timezone.now() - dt.timedelta(days=days)
    incoming = list(Transaction.objects.filter(
        company=company, status=Transaction.STATUS_ACTUAL,
        type=Transaction.TYPE_INCOME, date_actual__gte=since,
    ).select_related('account'))
    outgoing = list(Transaction.objects.filter(
        company=company, status=Transaction.STATUS_ACTUAL,
        type=Transaction.TYPE_EXPENSE, date_actual__gte=since,
    ).select_related('account'))
    results = []
    for inc in incoming:
        for exp in outgoing:
            if inc.account_id == exp.account_id or inc.amount != exp.amount:
                continue
            if abs((inc.date_actual - exp.date_actual).total_seconds()) > 7 * 86400:
                continue
            if InternalTransferMatch.objects.filter(source_transaction=exp, destination_transaction=inc).exists():
                continue
            source = exp.account
            target = inc.account
            if not source or not target:
                continue
            note = f'Совпала сумма {inc.amount} и дата в пределах 7 дней'
            review = ClassificationReview.objects.create(
                company=company, transaction=inc,
                proposal={'kind': 'internal_transfer', 'source_transaction_id': exp.id,
                          'destination_transaction_id': inc.id},
                reason=note, confidence=Decimal('70'),
                impact={'cash_source': str(-inc.amount), 'cash_destination': str(inc.amount), 'pnl': '0'},
            )
            results.append(review)
            if len(results) >= limit:
                return results
    return results


def reconciliation_for_account(account, *, observed_balance, as_of=None, user=None, note=''):
    as_of = as_of or timezone.now()
    account.recalc_balance(save=False)
    calculated = account.current_balance
    observed = Decimal(str(observed_balance))
    return BalanceReconciliation.objects.create(
        account=account, as_of=as_of, observed_balance=observed,
        calculated_balance=calculated, delta=observed - calculated,
        created_by=user if getattr(user, 'is_authenticated', False) else None,
        note=note or '',
    )
