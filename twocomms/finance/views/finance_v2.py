"""JSON API for the reviewable finance ledger and composite obligations.

The API intentionally keeps historical imports immutable until a user confirms
the proposed classification.  It is also the boundary used by the planned
payments UI for the Monobank fallback flow.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from ..models import (
    Account, Category, Counterparty, CounterpartyCard, FundingSource,
    ClassificationReview, InternalTransferMatch, LedgerClassification,
    ObligationComponent, ObligationGroup, ObligationSettlement, PaymentIntent,
    Transaction, get_default_company,
)
from ..permissions import finance_access_required
from ..services import ledger_v2, obligations_v2, payment_intents
from ..services import payables as payables_service


def _body(request):
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body or '{}')
        except (TypeError, ValueError):
            return {}
    return request.POST


def _decimal(value, field='amount'):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'Некорректное поле {field}')
    if result <= 0:
        raise ValueError(f'{field} должен быть больше нуля')
    return result


def _error(exc, status=400):
    return JsonResponse({'ok': False, 'error': str(exc)}, status=status)


def _classification_row(c):
    return {
        'id': c.id, 'transaction_id': c.transaction_id,
        'ownership_scope': c.ownership_scope, 'economic_kind': c.economic_kind,
        'confidence': str(c.confidence), 'source': c.source, 'note': c.note,
        'updated_at': c.updated_at.isoformat(),
    }


def _terminal_cash_review_row(review):
    return {
        'id': review.id, 'status': review.status, 'proposal': review.proposal,
        'reason': review.reason, 'impact': review.impact,
        'confidence': str(review.confidence),
    }


def _cash_account_row(account):
    return {
        'id': account.id, 'name': account.name, 'currency': account.currency,
        'current_balance': str(account.current_balance),
    }


@finance_access_required(api=True)
@require_GET
def terminal_cash_candidates_api(request):
    """Read-only terminal top-up candidates and own accounts usable for review."""
    company = get_default_company()
    try:
        limit = min(500, max(1, int(request.GET.get('limit') or 100)))
    except (TypeError, ValueError):
        return _error('Некорректный limit')
    pending_by_txn = {}
    for review in ClassificationReview.objects.filter(company=company, status='pending').select_related('transaction'):
        if (review.proposal or {}).get('kind', '').startswith('terminal_cash_'):
            pending_by_txn[review.transaction_id] = review

    candidates = []
    incoming = Transaction.objects.filter(
        company=company, status=Transaction.STATUS_ACTUAL, type=Transaction.TYPE_INCOME,
        economic_kind='unknown',
    ).select_related('account').order_by('-date_actual', '-id')
    for txn in incoming:
        evidence = ledger_v2.terminal_cash_evidence(txn)
        if not evidence['is_candidate']:
            continue
        review = pending_by_txn.get(txn.id)
        candidates.append({
            'transaction': {
                'id': txn.id, 'amount': str(txn.amount), 'currency': txn.currency,
                'date_actual': txn.date_actual.isoformat(), 'account_id': txn.account_id,
                'account_name': txn.account.name if txn.account else '', 'comment': txn.comment,
            },
            'evidence': evidence,
            'review': _terminal_cash_review_row(review) if review else None,
        })
        if len(candidates) >= limit:
            break
    own_accounts = company.accounts.filter(
        is_active=True, is_archived=False,
    ).order_by('type', 'sort_order', 'id')
    # Cash is the safe default. Other own accounts are possible sources too.
    own_accounts = sorted(own_accounts, key=lambda account: (account.type != 'cash', account.sort_order, account.id))
    return JsonResponse({
        'ok': True, 'candidates': candidates, 'count': len(candidates),
        'cash_accounts': [_cash_account_row(account) for account in own_accounts],
        'income_economic_kinds': list(ledger_v2.TERMINAL_CASH_INCOME_KINDS),
    })


@finance_access_required(api=True)
@require_POST
def terminal_cash_review_api(request, txn_id):
    """Store a review proposal; no imported transaction changes before acceptance."""
    company = get_default_company()
    data = _body(request)
    try:
        review = ledger_v2.prepare_terminal_cash_review(
            company, txn_id, user=request.user, action=data.get('action') or '',
            source_cash_account_id=data.get('source_cash_account_id'),
            economic_kind=data.get('economic_kind'),
            ownership_scope=data.get('ownership_scope') or 'unknown',
        )
    except Transaction.DoesNotExist:
        return _error('Операция не найдена', 404)
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'review': _terminal_cash_review_row(review)}, status=201)


@finance_access_required(api=True)
@require_http_methods(['GET', 'POST'])
def classification_api(request, txn_id):
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    if request.method == 'GET':
        row = getattr(txn, 'ledger_classification', None)
        return JsonResponse({'ok': True, 'classification': _classification_row(row) if row else None,
                             'transaction': {'id': txn.id, 'amount': str(txn.amount),
                                             'type': txn.type, 'comment': txn.comment}})
    data = _body(request)
    try:
        obj = ledger_v2.classify_transaction(
            txn, user=request.user,
            ownership_scope=data.get('ownership_scope') or 'unknown',
            economic_kind=data.get('economic_kind') or 'unknown',
            confidence=data.get('confidence') or 100,
            note=data.get('note') or '', source='manual',
            funding_source=company.funding_sources.filter(id=data.get('funding_source_id')).first()
            if data.get('funding_source_id') else None,
        )
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'classification': _classification_row(obj)})


@finance_access_required(api=True)
@require_GET
def review_list_api(request):
    company = get_default_company()
    qs = ClassificationReview.objects.filter(company=company)
    status = request.GET.get('status') or 'pending'
    if status != 'all':
        qs = qs.filter(status=status)
    rows = []
    for review in qs.select_related('transaction').order_by('-created_at')[:200]:
        rows.append({'id': review.id, 'transaction_id': review.transaction_id,
                     'amount': str(review.transaction.amount), 'type': review.transaction.type,
                     'comment': review.transaction.comment, 'proposal': review.proposal,
                     'reason': review.reason, 'impact': review.impact,
                     'confidence': str(review.confidence), 'status': review.status})
    return JsonResponse({'ok': True, 'reviews': rows, 'count': len(rows)})


@finance_access_required(api=True)
@require_POST
def review_action_api(request, review_id):
    company = get_default_company()
    data = _body(request)
    action = data.get('action') or 'accept'
    try:
        with db_transaction.atomic():
            review = get_object_or_404(
                ClassificationReview.objects.select_for_update(), id=review_id, company=company,
            )
            if review.status != 'pending':
                if review.status == 'accepted' and action == 'accept':
                    return JsonResponse({'ok': True, 'status': review.status})
                return _error('Предложение уже обработано', 409)
            if action == 'accept':
                proposal = review.proposal or {}
                kind = proposal.get('kind') or proposal.get('economic_kind')
                if kind == 'terminal_cash_transfer':
                    ledger_v2.confirm_terminal_cash_transfer(
                        review.transaction,
                        source_cash_account_id=proposal.get('source_cash_account_id'),
                        user=request.user, note=review.reason,
                    )
                elif kind == 'terminal_cash_income':
                    ledger_v2.classify_transaction(
                        review.transaction, user=request.user,
                        ownership_scope=proposal.get('ownership_scope') or 'unknown',
                        economic_kind=proposal.get('economic_kind') or 'unknown',
                        confidence=review.confidence, note=review.reason, source='review',
                    )
                elif kind == 'internal_transfer':
                    match = InternalTransferMatch.objects.filter(
                        company=company,
                        source_transaction_id=proposal.get('source_transaction_id'),
                        destination_transaction_id=proposal.get('destination_transaction_id'),
                    ).first()
                    if not match:
                        source = get_object_or_404(Transaction, id=proposal.get('source_transaction_id'), company=company)
                        dest = get_object_or_404(Transaction, id=proposal.get('destination_transaction_id'), company=company)
                        match = ledger_v2.create_transfer_suggestion(company, source, dest)
                    ledger_v2.confirm_transfer(match, user=request.user)
                else:
                    txn = review.transaction
                    funding = company.funding_sources.filter(id=proposal.get('funding_source_id')).first()
                    ledger_v2.classify_transaction(
                        txn, user=request.user, ownership_scope=proposal.get('ownership_scope') or 'unknown',
                        economic_kind=kind or 'unknown', confidence=review.confidence,
                        note=proposal.get('note') or review.reason, source='review', funding_source=funding,
                    )
                    if funding and kind == 'grant_inflow' and not funding.receipt_transaction_id:
                        funding.receipt_transaction = txn
                        funding.save(update_fields=['receipt_transaction'])
                review.status = 'accepted'
            elif action in {'reject', 'rejected'}:
                review.status = 'rejected'
            elif action == 'skip':
                review.status = 'skipped'
            else:
                return _error('Неизвестное действие')
            review.reviewed_by = request.user
            from django.utils import timezone
            review.reviewed_at = timezone.now()
            review.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
    except (ValueError, TypeError) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'status': review.status})


@finance_access_required(api=True)
@require_POST
def transfer_suggestions_api(request):
    company = get_default_company()
    data = _body(request)
    try:
        reviews = ledger_v2.build_transfer_reviews(company, days=int(data.get('days') or 180),
                                                   limit=min(500, int(data.get('limit') or 100)))
    except (TypeError, ValueError):
        return _error('Некорректные параметры поиска')
    return JsonResponse({'ok': True, 'review_ids': [r.id for r in reviews], 'count': len(reviews)})


def _funding_row(source):
    summary = ledger_v2.funding_summary(source)
    return {'id': source.id, 'name': source.name, 'source_type': source.source_type,
            'received_amount': str(source.received_amount),
            'program_total_amount': (str(source.program_total_amount)
                                     if source.program_total_amount is not None else None),
            'stage_label': source.stage_label,
            'receipt_transaction_id': source.receipt_transaction_id,
            **{k: str(v) for k, v in summary.items()},
            'project_id': source.project_id, 'received_at': source.received_at.isoformat() if source.received_at else None}


@finance_access_required(api=True)
@require_http_methods(['GET', 'POST'])
def funding_sources_api(request):
    company = get_default_company()
    if request.method == 'GET':
        return JsonResponse({'ok': True, 'sources': [_funding_row(s) for s in company.funding_sources.all()]})
    data = _body(request)
    try:
        source = FundingSource.objects.create(
            company=company, name=(data.get('name') or '').strip(),
            source_type=data.get('source_type') or 'grant',
            received_amount=Decimal(str(data.get('received_amount') or '0')),
            received_at=data.get('received_at') or None, valid_until=data.get('valid_until') or None,
            project_id=data.get('project_id') or None, notes=data.get('notes') or '',
            program_total_amount=(Decimal(str(data['program_total_amount']))
                                  if data.get('program_total_amount') not in (None, '') else None),
            stage_label=(data.get('stage_label') or '').strip(),
        )
        if not source.name:
            raise ValueError('Укажите название источника')
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'source': _funding_row(source)}, status=201)


@finance_access_required(api=True)
@require_POST
def funding_allocate_api(request, source_id):
    company = get_default_company()
    source = get_object_or_404(FundingSource, id=source_id, company=company)
    data = _body(request)
    txn = get_object_or_404(Transaction, id=data.get('transaction_id'), company=company)
    try:
        allocation = ledger_v2.allocate_funding(
            source=source, txn=txn, amount=_decimal(data.get('amount')),
            allocation_type=data.get('allocation_type') or 'spent', user=request.user,
            note=data.get('note') or '',
        )
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'allocation_id': allocation.id, 'summary': _funding_row(source)})


def _component_row(component):
    row = obligations_v2.component_summary(component)
    for key in ('planned', 'paid', 'remaining'):
        row[key] = str(row[key])
    return row


def _group_row(group):
    row = obligations_v2.group_summary(group)
    row['remaining'] = str(row['remaining'])
    for component in row['components']:
        for key in ('planned', 'paid', 'remaining'):
            component[key] = str(component[key])
    return row


@finance_access_required(api=True)
@require_http_methods(['GET', 'POST'])
def obligation_groups_api(request):
    company = get_default_company()
    if request.method == 'GET':
        groups = company.obligation_groups.filter(is_active=True).prefetch_related('components')
        return JsonResponse({'ok': True, 'groups': [_group_row(g) for g in groups]})
    data = _body(request)
    try:
        group = ObligationGroup.objects.create(
            company=company, title=(data.get('title') or '').strip(), type=data.get('type') or 'expense',
            counterparty_id=data.get('counterparty_id') or None,
            recurrence_rule_id=data.get('recurrence_rule_id') or None,
            due_day=data.get('due_day') or None, role=data.get('role') or 'charge',
            forecast_policy=data.get('forecast_policy') or {}, notes=data.get('notes') or '',
        )
        if not group.title:
            raise ValueError('Укажите название обязательства')
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'group': _group_row(group)}, status=201)


@finance_access_required(api=True)
@require_GET
def obligation_group_api(request, group_id):
    company = get_default_company()
    group = get_object_or_404(ObligationGroup, id=group_id, company=company)
    return JsonResponse({'ok': True, 'group': _group_row(group)})


@finance_access_required(api=True)
@require_POST
def obligation_component_api(request, group_id):
    company = get_default_company()
    group = get_object_or_404(ObligationGroup, id=group_id, company=company)
    data = _body(request)
    try:
        card = CounterpartyCard.objects.filter(id=data.get('recipient_card_id'), company=company).first() \
            if data.get('recipient_card_id') else None
        component = ObligationComponent.objects.create(
            group=group, name=(data.get('name') or '').strip(),
            category_id=data.get('category_id') or None, recipient_card=card,
            fixed_amount=Decimal(str(data['fixed_amount'])) if data.get('fixed_amount') not in (None, '') else None,
            forecast_amount=Decimal(str(data['forecast_amount'])) if data.get('forecast_amount') not in (None, '') else None,
            forecast_policy=data.get('forecast_policy') or {},
            payment_purpose_template=data.get('payment_purpose_template') or '',
            sort_order=int(data.get('sort_order') or 0),
        )
        if not component.name:
            raise ValueError('Укажите название компонента')
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'component': _component_row(component)}, status=201)


@finance_access_required(api=True)
@require_POST
def component_payment_intent_api(request, component_id):
    company = get_default_company()
    component = get_object_or_404(ObligationComponent, id=component_id, group__company=company)
    data = _body(request)
    account = get_object_or_404(Account, id=data.get('account_id'), company=company)
    amount = _decimal(data.get('amount') or component.fixed_amount or component.forecast_amount, 'amount')
    card = component.recipient_card
    try:
        intent, created = payment_intents.create_intent(
            company=company, account=account, amount=amount, component=component,
            recipient_card=card, purpose=data.get('purpose') or component.payment_purpose_template,
            comment=data.get('comment') or '', user=request.user,
            idempotency_key=data.get('idempotency_key') or None,
        )
        if intent.status == 'draft':
            payment_intents.transition(intent, 'awaiting_confirmation', user=request.user)
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'created': created,
                         'intent': payment_intents.instruction_payload(intent)})


@finance_access_required(api=True)
@require_GET
def component_payment_context_api(request, component_id):
    """Return existing unlinked payments and saved recipient details."""
    company = get_default_company()
    component = get_object_or_404(
        ObligationComponent.objects.select_related('group', 'group__counterparty', 'recipient_card'),
        id=component_id, group__company=company,
    )
    group = component.group
    ttype = Transaction.TYPE_INCOME if group.type == 'income' else Transaction.TYPE_EXPENSE
    candidates = payables_service.payable_candidates(
        company, ttype=ttype, counterparty=group.counterparty, limit=60,
    )
    card = component.recipient_card
    return JsonResponse({
        'ok': True,
        'component': obligations_v2.component_summary(component),
        'counterparty': ({'id': group.counterparty_id, 'name': group.counterparty.name}
                         if group.counterparty_id else None),
        'recipient_card': ({
            'id': card.id, 'label': card.label, 'iban': card.iban,
            'pan_mask': card.pan_mask, 'bank': card.bank,
        } if card else None),
        'candidates': candidates,
    })


@finance_access_required(api=True)
@require_POST
def component_settle_existing_api(request, component_id):
    """Attach an already imported payment to one component atomically."""
    from django.utils import timezone

    company = get_default_company()
    component = get_object_or_404(
        ObligationComponent.objects.select_related('group', 'group__counterparty'),
        id=component_id, group__company=company,
    )
    data = _body(request)
    payment = get_object_or_404(
        Transaction.objects.select_related('account'),
        id=data.get('transaction_id'), company=company, status=Transaction.STATUS_ACTUAL,
    )
    expected_type = Transaction.TYPE_INCOME if component.group.type == 'income' else Transaction.TYPE_EXPENSE
    if payment.type != expected_type:
        return _error('Тип операции не совпадает с обязательством')
    try:
        amount = _decimal(data.get('amount') or payment.amount)
        planned = component.fixed_amount or component.forecast_amount or Decimal('0')
        already = component.settlements.aggregate(v=Sum('amount'))['v'] or Decimal('0')
        if amount > payment.amount or amount > max(planned - already, Decimal('0')):
            raise ValueError('Сумма превышает остаток компонента или операцию')
        if payment.settlements.exists():
            raise ValueError('Операция уже привязана к обязательству')
        with db_transaction.atomic():
            if component.group.counterparty_id and not payment.counterparty_id:
                payment.counterparty = component.group.counterparty
                payment.save(update_fields=['counterparty', 'updated_at'])
            settlement = ObligationSettlement.objects.create(
                company=company, payment=payment, rule=component.group.recurrence_rule,
                period_key=timezone.localdate().strftime('%Y-%m'),
                period_label=component.name, amount=amount, currency=payment.currency,
                created_by=request.user if request.user.is_authenticated else None,
            )
            obligations_v2.allocate_settlement(
                settlement=settlement, allocations={str(component.id): str(amount)},
            )
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'settlement_id': settlement.id,
                         'component': obligations_v2.component_summary(component)})


def _intent_row(intent):
    payload = payment_intents.instruction_payload(intent)
    payload['events'] = list(intent.events.order_by('created_at').values('from_status', 'to_status', 'created_at'))
    for event in payload['events']:
        event['created_at'] = event['created_at'].isoformat()
    return payload


@finance_access_required(api=True)
@require_GET
def payment_intent_api(request, intent_id):
    company = get_default_company()
    intent = get_object_or_404(PaymentIntent, id=intent_id, company=company)
    return JsonResponse({'ok': True, 'intent': _intent_row(intent)})


@finance_access_required(api=True)
@require_POST
def payment_intent_transition_api(request, intent_id):
    company = get_default_company()
    intent = get_object_or_404(PaymentIntent, id=intent_id, company=company)
    data = _body(request)
    try:
        intent = payment_intents.transition(intent, data.get('status'), user=request.user,
                                            payload=data.get('payload') or {})
    except ValueError as exc:
        return _error(exc, 409)
    return JsonResponse({'ok': True, 'intent': _intent_row(intent)})


@finance_access_required(api=True)
@require_POST
def payment_intent_match_api(request, intent_id):
    company = get_default_company()
    intent = get_object_or_404(PaymentIntent, id=intent_id, company=company)
    data = _body(request)
    txn = get_object_or_404(Transaction, id=data.get('transaction_id'), company=company)
    try:
        intent = payment_intents.attach_statement(intent, txn, user=request.user)
    except ValueError as exc:
        return _error(exc, 409)
    return JsonResponse({'ok': True, 'intent': _intent_row(intent)})


@finance_access_required(api=True)
@require_POST
def settlement_components_api(request, settlement_id):
    company = get_default_company()
    settlement = get_object_or_404(ObligationSettlement, id=settlement_id, company=company)
    data = _body(request)
    allocations = data.get('allocations') or {}
    try:
        total = obligations_v2.allocate_settlement(settlement=settlement, allocations=allocations)
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'total': str(total), 'settlement_id': settlement.id})


@finance_access_required(api=True)
@require_POST
def refund_link_api(request):
    company = get_default_company()
    data = _body(request)
    refund = get_object_or_404(Transaction, id=data.get('refund_transaction_id'), company=company)
    original = get_object_or_404(Transaction, id=data.get('original_transaction_id'), company=company)
    try:
        link = ledger_v2.link_refund(refund_transaction=refund, original_transaction=original,
                                     amount=data.get('amount') or None, kind=data.get('kind') or 'expense_refund',
                                     user=request.user, note=data.get('note') or '')
    except (ValueError, TypeError, InvalidOperation) as exc:
        return _error(exc)
    return JsonResponse({'ok': True, 'refund_link_id': link.id})


@finance_access_required(api=True)
@require_GET
def finance_v2_health_api(request):
    company = get_default_company()
    actual = company.transactions.filter(status=Transaction.STATUS_ACTUAL)
    unclassified = actual.filter(economic_kind='unknown')
    pending_reviews = ClassificationReview.objects.filter(company=company, status='pending')
    pending_intents = PaymentIntent.objects.filter(company=company, status__in=['awaiting_confirmation', 'submitted', 'detected'])
    groups = company.obligation_groups.filter(is_active=True).prefetch_related('components')
    overdue = [g.id for g in groups if obligations_v2.group_summary(g)['status'] == 'overdue']
    grant_rows = [_funding_row(s) for s in company.funding_sources.filter(is_active=True)]
    return JsonResponse({'ok': True, 'health': {
        'free_money': str(sum((a.current_balance for a in company.accounts.filter(is_active=True)), Decimal('0'))),
        'unclassified_count': unclassified.count(),
        'unclassified_amount': str(sum((t.amount for t in unclassified), Decimal('0'))),
        'pending_reviews': pending_reviews.count(), 'pending_payment_intents': pending_intents.count(),
        'overdue_group_ids': overdue, 'funding_sources': grant_rows,
    }})
