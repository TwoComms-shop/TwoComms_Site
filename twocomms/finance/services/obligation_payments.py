"""Recipient identity can be known while the purpose still needs confirmation."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import ClassificationReview, ObligationComponent, Transaction
from . import cards, obligations_v2

REVIEW_KIND = 'obligation_payment'


def context(txn, *, period_key=None):
    period = obligations_v2.payment_period(period_key or timezone.localtime(txn.date_actual).strftime('%Y-%m'))
    if (txn.status != 'actual' or txn.type != 'expense' or txn.settlements.exists()
            or txn.economic_kind in {'internal_transfer', 'owner_draw', 'personal_transfer'}):
        return None
    cp = txn.counterparty or cards.match_counterparty_by_payment(txn.company, txn)['strong']
    if cp is None:
        return None
    period_start, period_end = obligations_v2.payment_period_bounds(period)
    options = []
    for component in ObligationComponent.objects.filter(
        group__company=txn.company, group__counterparty=cp, group__is_active=True,
        group__type='expense', is_active=True,
    ).select_related('group'):
        row = obligations_v2.component_summary(component, period_key=period)
        if not row['needs_amount'] and row['remaining'] <= 0:
            continue
        if not row['is_variable'] and txn.amount > row['remaining']:
            continue
        plans = Transaction.objects.filter(
            company=txn.company, counterparty=cp, type='expense', status='planned',
            date_actual__gte=period_start, date_actual__lt=period_end,
        )
        if component.group.recurrence_rule_id:
            plans = plans.filter(recurrence_rule_id=component.group.recurrence_rule_id)
        plan_ids = list(plans.values_list('id', flat=True)[:2])
        options.append({'component_id': component.id, 'name': component.name,
                        'group': component.group.title, 'remaining': str(row['remaining']),
                        'is_variable': row['is_variable'],
                        'suggested': not row['is_variable'] and txn.amount == row['remaining'],
                        'planned_txn_id': plan_ids[0] if len(plan_ids) == 1 else None})
    options.sort(key=lambda r: (not r['suggested'], r['group'], r['component_id']))
    return {'counterparty': {'id': cp.id, 'name': cp.name}, 'period_key': period,
            'amount': str(txn.amount), 'currency': txn.currency, 'options': options}


@transaction.atomic
def ensure_review(txn, *, period_key=None):
    if txn.type != 'expense' or txn.status != 'actual':
        return None
    Transaction.objects.select_for_update().get(pk=txn.pk)
    existing = txn.classification_reviews.filter(proposal__kind=REVIEW_KIND).first()
    if existing:
        return existing if existing.status == 'pending' else None
    data = context(txn, period_key=period_key)
    if data is None or not data['options']:
        return None
    return ClassificationReview.objects.create(
        company=txn.company, transaction=txn,
        proposal={'kind': REVIEW_KIND, 'counterparty_id': data['counterparty']['id']},
        reason=f'Підтвердьте призначення витрати для «{data["counterparty"]["name"]}»',
        confidence=Decimal('100'),
    )


def resolve_review(txn, component, settlement, *, user=None):
    for review in txn.classification_reviews.filter(status='pending', proposal__kind=REVIEW_KIND):
        review.status = 'accepted'
        review.proposal = {**review.proposal, 'component_id': component.id,
                           'period_key': settlement.period_key, 'settlement_id': settlement.id}
        review.reviewed_by = user if getattr(user, 'is_authenticated', False) else None
        review.reviewed_at = timezone.now()
        review.save(update_fields=['status', 'proposal', 'reviewed_by', 'reviewed_at'])
