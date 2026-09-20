"""Reviewable ledger classification and funding operations."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import (
    Account, BalanceReconciliation, ClassificationReview, FundingAllocation, FundingSource,
    InternalTransferMatch, LedgerClassification, LedgerClassificationEvent,
    RefundLink, Transaction,
)
from . import transactions as transactions_service


TERMINAL_CASH_INCOME_KINDS = (
    'sale', 'investment', 'grant_inflow', 'debt_repayment', 'expense_refund',
    'personal_transfer', 'pension_income', 'adjustment',
)

# Keep classification semantics in one service-level place so callers do not
# need to know which reports treat a kind as personal, operational, or a
# transfer.  The model choices remain the source of valid persisted values.
PERSONAL_INCOME_KINDS = frozenset({'pension_income'})
TRANSFER_FEE_KIND = 'transfer_fee'
INTERNAL_TRANSFER_KINDS = frozenset({'internal_transfer', 'owner_draw', 'personal_transfer'})


def classification_semantics(economic_kind):
    """Return stable, API-independent semantics for an economic kind."""
    return {
        'is_personal_income': economic_kind in PERSONAL_INCOME_KINDS,
        'is_transfer_fee': economic_kind == TRANSFER_FEE_KIND,
        'is_internal_transfer': economic_kind in INTERNAL_TRANSFER_KINDS,
    }


def calculate_transfer_fee(source_amount, destination_amount):
    """Return the non-negative amount lost between debit and credit rows.

    Amounts are normalized through ``Decimal(str(...))`` so this helper is
    safe for values arriving from JSON/API payloads as well as model fields.
    A larger credit is not treated as a fee; that is a separate reconciliation
    problem and must not create a negative expense.
    """
    source = Decimal(str(source_amount))
    destination = Decimal(str(destination_amount))
    if source < 0 or destination < 0:
        raise ValueError('Суми переказу не можуть бути від’ємними')
    return max(source - destination, Decimal('0'))

_TERMINAL_RE = re.compile(r'\b(?:terminal|термінал|терминал)\b', re.IGNORECASE)
_MONO_RE = re.compile(r'\b(?:mono|monobank|монобанк)\b', re.IGNORECASE)
_CITY24_RE = re.compile(r'\b(?:city[\s-]?24|сіті[\s-]?24|сити[\s-]?24)\b', re.IGNORECASE)
_REFUND_RE = re.compile(r'повернен|refund', re.IGNORECASE)
_RENT_RE = re.compile(r'оренд|комунал|rent|utility', re.IGNORECASE)
_VIKTOR_RE = re.compile(r'віктор|виктор|viktor', re.IGNORECASE)


def _terminal_text_values(value, path):
    """Yield string values from bank metadata with their stable JSON paths."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _terminal_text_values(child, f'{path}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _terminal_text_values(child, f'{path}[{index}]')
    elif isinstance(value, str) and value.strip():
        yield path, value


def terminal_cash_evidence(txn):
    """Return terminal-provider evidence without changing the imported row."""
    values = []
    if txn.comment:
        values.append(('comment', txn.comment))
    values.extend(_terminal_text_values(txn.external_data or {}, 'external_data'))

    matched_fields = {'terminal': [], 'monobank': [], 'city24': []}
    for path, value in values:
        if _TERMINAL_RE.search(value):
            matched_fields['terminal'].append(path)
        if _MONO_RE.search(value):
            matched_fields['monobank'].append(path)
        if _CITY24_RE.search(value):
            matched_fields['city24'].append(path)

    providers = []
    if matched_fields['monobank']:
        providers.append('monobank')
    if matched_fields['city24']:
        providers.append('city24')
    is_candidate = bool(matched_fields['city24'] or (
        matched_fields['terminal'] and matched_fields['monobank']
    ))
    return {
        'is_candidate': is_candidate,
        'providers': providers,
        'matched_fields': {key: value for key, value in matched_fields.items() if value},
    }


def is_known_rent_refund(txn) -> bool:
    """Return True only for the stable Viktor rent/utility refund pattern.

    Imported terminal rows can inherit a recurring rule or counterparty, so the
    provider/source exclusions are part of the identity and prevent a false
    positive from being silently classified as a refund.
    """
    if (txn.status != Transaction.STATUS_ACTUAL or txn.type != Transaction.TYPE_INCOME
            or txn.economic_kind != 'unknown' or txn.source not in {'manual', 'recurring'}
            or txn.external_id or terminal_cash_evidence(txn)['is_candidate']):
        return False
    rule = getattr(txn, 'recurrence_rule', None)
    counterparty = getattr(txn, 'counterparty', None)
    category = getattr(txn, 'category', None)
    if not rule or rule.template_type != Transaction.TYPE_INCOME:
        return False
    if not counterparty or rule.template_counterparty_id != counterparty.id:
        return False
    if not _VIKTOR_RE.search(counterparty.name or ''):
        return False
    rule_text = ' '.join((rule.title or '', rule.template_comment or ''))
    if not _RENT_RE.search(rule_text) or not _REFUND_RE.search(rule_text):
        return False
    if not _REFUND_RE.search((category.name if category else '') or ''):
        return False
    return bool(_REFUND_RE.search(txn.comment or '') and _RENT_RE.search(txn.comment or ''))


@db_transaction.atomic
def classify_known_rent_refund(txn, *, user=None):
    """Apply the deterministic Viktor refund rule to one actualized row."""
    txn = Transaction.objects.select_for_update().select_related(
        'recurrence_rule', 'counterparty', 'category', 'account',
    ).get(pk=txn.pk)
    if not is_known_rent_refund(txn):
        return None
    if not txn.is_business:
        txn.is_business = True
        txn.save(update_fields=['is_business'])
    return classify_transaction(
        txn, user=user, ownership_scope='business', economic_kind='expense_refund',
        confidence=Decimal('100'), source='rule',
        note='Автоматично визначено: повернення оренди/комунальних від Віктора Викторовича.',
    )


def _assert_terminal_cash_transaction(txn):
    if txn.status != Transaction.STATUS_ACTUAL or txn.type != Transaction.TYPE_INCOME:
        raise ValueError('Нужна фактическая входящая операция')
    evidence = terminal_cash_evidence(txn)
    if not evidence['is_candidate']:
        raise ValueError('Операция не похожа на пополнение через терминал Mono или City24')
    if not txn.account_id:
        raise ValueError('У входящей операции не указан счет получателя')
    return evidence


def ensure_terminal_cash_decision_review(txn):
    """Queue a newly imported terminal top-up without changing its meaning.

    This is deliberately a decision marker, rather than a cash-transfer
    proposal: bank text identifies a terminal, not the person who supplied
    the money. The UI replaces it with an explicit reviewed decision.
    """
    try:
        evidence = _assert_terminal_cash_transaction(txn)
    except ValueError:
        return None
    existing = ClassificationReview.objects.filter(
        company=txn.company, transaction=txn, status='pending',
    ).filter(proposal__kind__startswith='terminal_cash_').first()
    if existing:
        return existing
    return ClassificationReview.objects.create(
        company=txn.company,
        transaction=txn,
        proposal={'kind': 'terminal_cash_decision', 'terminal_evidence': evidence},
        reason='Термінальне поповнення: вкажіть походження коштів. Історія та баланси не змінені.',
        impact={'pnl': str(txn.amount), 'cashflow': str(txn.amount), 'requires_decision': True},
        confidence=Decimal('100'),
    )


@db_transaction.atomic
def prepare_terminal_cash_review(company, txn_id, *, action, user=None,
                                 source_cash_account_id=None, economic_kind=None,
                                 ownership_scope='unknown'):
    """Create or update one pending, non-destructive terminal-cash review."""
    txn = Transaction.objects.select_for_update().select_related('account').get(
        id=txn_id, company=company,
    )
    evidence = _assert_terminal_cash_transaction(txn)
    if ownership_scope not in dict(LedgerClassification.SCOPE_CHOICES):
        raise ValueError('Некорректная принадлежность операции')

    if action == 'cash_transfer':
        source_cash = Account.objects.select_for_update().filter(
            id=source_cash_account_id, company=company, is_active=True,
            is_archived=False,
        ).first()
        if source_cash is None:
            raise ValueError('Оберіть активний рахунок «Готівка»')
        if source_cash.type != 'cash':
            raise ValueError('Для цього підтвердження джерелом може бути лише рахунок «Готівка»')
        if source_cash.id == txn.account_id:
            raise ValueError('Рахунок-джерело та рахунок отримувача мають відрізнятися')
        if source_cash.currency != txn.currency:
            raise ValueError('Валюта рахунку-джерела має збігатися з валютою поповнення')
        if source_cash.current_balance < txn.amount:
            raise ValueError(
                f'Недостатньо готівки для підтвердження: доступно {source_cash.current_balance} '
                f'{source_cash.currency}, потрібно {txn.amount} {txn.currency}. '
                'Спочатку внесіть або звірте залишок готівки.'
            )
        proposal = {
            'kind': 'terminal_cash_transfer',
            'source_cash_account_id': source_cash.id,
            'destination_account_id': txn.account_id,
            'terminal_evidence': evidence,
        }
        reason = 'Поповнення через термінал: підтвердьте переказ на власну картку'
        impact = {
            'cash_source': str(-txn.amount), 'cash_destination': str(txn.amount), 'pnl': '0',
        }
    elif action == 'income':
        if economic_kind not in TERMINAL_CASH_INCOME_KINDS:
            raise ValueError('Укажите допустимый экономический вид дохода')
        proposal = {
            'kind': 'terminal_cash_income',
            'economic_kind': economic_kind,
            'ownership_scope': ownership_scope,
            'terminal_evidence': evidence,
        }
        reason = 'Поповнення через термінал: підтвердьте класифікацію надходження'
        impact = {'cash_source': '0', 'cash_destination': str(txn.amount), 'pnl': str(txn.amount)}
    else:
        raise ValueError('Неизвестное решение для пополнения через терминал')

    pending = list(ClassificationReview.objects.select_for_update().filter(
        company=company, transaction=txn, status='pending',
    ))
    review = next(
        (item for item in pending if (item.proposal or {}).get('kind', '').startswith('terminal_cash_')),
        None,
    )
    if review is None:
        review = ClassificationReview.objects.create(
            company=company, transaction=txn, proposal=proposal, reason=reason,
            impact=impact, confidence=Decimal('100'),
        )
    else:
        review.proposal = proposal
        review.reason = reason
        review.impact = impact
        review.confidence = Decimal('100')
        review.save(update_fields=['proposal', 'reason', 'impact', 'confidence'])
    return review


@db_transaction.atomic
def confirm_terminal_cash_transfer(txn, *, source_cash_account_id, user=None, note=''):
    """Materialize a confirmed terminal top-up as cash -> own-card transfer."""
    txn = Transaction.objects.select_for_update().select_related('account').get(pk=txn.pk)
    _assert_terminal_cash_transaction(txn)
    destination = txn.account
    source_cash = Account.objects.select_for_update().filter(
        id=source_cash_account_id, company=txn.company, is_active=True,
        is_archived=False,
    ).first()
    if source_cash is None:
        raise ValueError('Оберіть активний рахунок «Готівка»')
    if source_cash.type != 'cash':
        raise ValueError('Для цього підтвердження джерелом може бути лише рахунок «Готівка»')
    if source_cash.id == destination.id:
        raise ValueError('Рахунок-джерело та рахунок отримувача мають відрізнятися')
    if source_cash.currency != txn.currency:
        raise ValueError('Валюта рахунку-джерела має збігатися з валютою поповнення')
    if source_cash.current_balance < txn.amount:
        raise ValueError(
            f'Недостатньо готівки для підтвердження: доступно {source_cash.current_balance} '
            f'{source_cash.currency}, потрібно {txn.amount} {txn.currency}. '
            'Спочатку внесіть або звірте залишок готівки.'
        )

    external_data = dict(txn.external_data or {})
    external_data['terminal_cash_transfer'] = {
        'source_cash_account_id': source_cash.id,
        'destination_account_id': destination.id,
        'confirmed_at': timezone.now().isoformat(),
    }
    transfer = transactions_service.update_transaction(
        txn, user=user, type=Transaction.TYPE_TRANSFER, account=source_cash,
        to_account=destination, to_amount=txn.amount, category=None, counterparty=None,
        external_data=external_data,
    )
    is_personal_destination = not bool(destination.is_business)
    classify_transaction(
        transfer, user=user,
        ownership_scope='personal' if is_personal_destination else 'business',
        economic_kind='owner_draw' if is_personal_destination else 'internal_transfer',
        confidence=Decimal('100'), source='review', note=note,
    )
    return transfer


def classify_transaction(txn, *, user=None, ownership_scope='unknown', economic_kind='unknown',
                         confidence=Decimal('100'), note='', source='manual', funding_source=None):
    """Upsert explicit meaning without changing the original bank row."""
    if economic_kind in PERSONAL_INCOME_KINDS and ownership_scope == 'unknown':
        ownership_scope = 'personal'
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


def ensure_grant_account_review(txn, *, source=None):
    """Queue a confirmation for a new operation on the dedicated grant card.

    The account is only a strong signal for the review, never an automatic
    classification. Historical rows are handled by an explicit management
    command after inspection.
    """
    if (txn.status != Transaction.STATUS_ACTUAL or not txn.account_id
            or txn.account.name.strip().casefold() != 'грантова'
            or txn.economic_kind != 'unknown'):
        return None
    source = source or FundingSource.objects.filter(
        company=txn.company, name__iexact='УВФ', is_active=True,
    ).first()
    if source is None:
        return None
    kind = 'grant_inflow' if txn.type == Transaction.TYPE_INCOME else 'operating_expense'
    review, _ = ClassificationReview.objects.get_or_create(
        company=txn.company, transaction=txn,
        status='pending',
        defaults={
            'proposal': {
                'kind': kind, 'economic_kind': kind,
                'ownership_scope': 'business', 'funding_source_id': source.id,
                'allocation_amount': str(txn.amount),
            },
            'reason': 'Операція на рахунку «Грантова» потребує підтвердження грантового призначення.',
            'impact': {'funding_source_id': source.id, 'funding_source': source.name},
            'confidence': Decimal('85'),
        },
    )
    return review


@db_transaction.atomic
def confirm_grant_account_history(account, source, *, user=None):
    """Confirm existing actual history for a dedicated grant account.

    This is intentionally explicit and idempotent. Planned/future operations
    are excluded so they remain reviewable when they become actual.
    """
    rows = list(Transaction.objects.select_for_update().filter(
        company=account.company, account=account, status=Transaction.STATUS_ACTUAL,
    ).order_by('date_actual', 'id'))
    changed = []
    for txn in rows:
        kind = 'grant_inflow' if txn.type == Transaction.TYPE_INCOME else 'operating_expense'
        if txn.economic_kind == 'unknown' or txn.funding_source_id != source.id:
            classify_transaction(
                txn, user=user, ownership_scope='business', economic_kind=kind,
                confidence=Decimal('100'), source='grant_history',
                funding_source=source,
                note='Підтверджено як історична операція грантового рахунку.',
            )
            ClassificationReview.objects.filter(
                transaction=txn, status='pending',
                proposal__funding_source_id=source.id,
            ).update(status='accepted', reviewed_by=user if getattr(user, 'is_authenticated', False) else None,
                    reviewed_at=timezone.now())
            changed.append(txn)
        if txn.type == Transaction.TYPE_EXPENSE:
            if not FundingAllocation.objects.filter(
                    funding_source=source, transaction=txn, allocation_type='spent').exists():
                allocate_funding(
                    source=source, txn=txn, amount=txn.amount,
                    allocation_type='spent', user=user,
                    note='Історична витрата з рахунку «Грантова».',
                )
    return changed


def create_transfer_suggestion(company, source_txn, destination_txn, *, confidence=Decimal('75'), note=''):
    """Create or return a transfer pair after validating imported rows."""
    with db_transaction.atomic():
        source_txn, destination_txn = Transaction.objects.select_for_update().select_related(
            'account', 'company',
        ).get(pk=source_txn.pk), Transaction.objects.select_for_update().select_related(
            'account', 'company',
        ).get(pk=destination_txn.pk)
        _validate_transfer_rows(company, source_txn, destination_txn)
        duplicate = InternalTransferMatch.objects.select_for_update().filter(
            source_transaction__in=(source_txn, destination_txn),
            destination_transaction__in=(source_txn, destination_txn),
            status__in=('suggested', 'confirmed'),
        ).exclude(source_transaction=source_txn, destination_transaction=destination_txn).first()
        if duplicate:
            raise ValueError('Одна з операцій вже пов’язана з іншим переказом')
        existing = InternalTransferMatch.objects.select_for_update().filter(
            source_transaction=source_txn, destination_transaction=destination_txn,
        ).first()
        if existing and existing.status == 'confirmed':
            return existing
        amount = min(source_txn.amount, destination_txn.amount)
        fee_amount = calculate_transfer_fee(source_txn.amount, destination_txn.amount)
        match, _ = InternalTransferMatch.objects.update_or_create(
            source_transaction=source_txn,
            destination_transaction=destination_txn,
            defaults={
                'company': company,
                'source_account': source_txn.account,
                'destination_account': destination_txn.account,
                'amount': amount,
                'fee_amount': fee_amount,
                'confidence': Decimal(str(confidence)),
                'status': 'suggested',
            },
        )
        return match


def _validate_transfer_rows(company, source_txn, destination_txn):
    if source_txn.company_id != company.id or destination_txn.company_id != company.id:
        raise ValueError('Операції повинні належати одній компанії')
    if source_txn.id == destination_txn.id:
        raise ValueError('Оберіть дві різні операції')
    if source_txn.type != Transaction.TYPE_EXPENSE or destination_txn.type != Transaction.TYPE_INCOME:
        raise ValueError('Переказ має поєднувати витрату з доходом')
    if source_txn.status != Transaction.STATUS_ACTUAL or destination_txn.status != Transaction.STATUS_ACTUAL:
        raise ValueError('Для переказу доступні лише фактичні операції')
    if source_txn.excluded_from_reports or destination_txn.excluded_from_reports:
        raise ValueError('Виключені або видалені операції не можна пов’язати')
    if not source_txn.account_id or not destination_txn.account_id:
        raise ValueError('Для обох операцій потрібен рахунок')
    if source_txn.account_id == destination_txn.account_id:
        raise ValueError('Операції повинні бути на різних рахунках')
    if source_txn.currency != destination_txn.currency:
        raise ValueError('Валюта рахунків повинна збігатися')
    if not source_txn.account.is_active or source_txn.account.is_archived:
        raise ValueError('Рахунок витрати неактивний')
    if not destination_txn.account.is_active or destination_txn.account.is_archived:
        raise ValueError('Рахунок доходу неактивний')
    if source_txn.amount <= 0 or destination_txn.amount <= 0:
        raise ValueError('Суми операцій повинні бути більшими за нуль')
    if source_txn.amount < destination_txn.amount:
        raise ValueError('Витрата переказу не може бути меншою за отримання')


@db_transaction.atomic
def create_missing_transfer_counterpart(company, anchor_txn, counterpart_account, *, fee_amount=Decimal('0'), counterpart_date=None, user=None):
    """Create the missing opposite bank row and its confirmed transfer link."""
    anchor = Transaction.objects.select_for_update().select_related('account').get(pk=anchor_txn.pk)
    account = Account.objects.select_for_update().get(pk=counterpart_account.pk)
    if anchor.company_id != company.id or account.company_id != company.id:
        raise ValueError('Операція та рахунок повинні належати одній компанії')
    if anchor.type not in (Transaction.TYPE_INCOME, Transaction.TYPE_EXPENSE):
        raise ValueError('Оберіть дохід або витрату')
    if anchor.status != Transaction.STATUS_ACTUAL or anchor.excluded_from_reports:
        raise ValueError('Доступні лише фактичні операції без виключення з обліку')
    if not anchor.account_id or anchor.account_id == account.id:
        raise ValueError('Оберіть інший активний рахунок')
    if not account.is_active or account.is_archived or account.currency != anchor.currency:
        raise ValueError('Рахунок-кореспондент неактивний або має іншу валюту')
    try:
        fee = Decimal(str(fee_amount))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Некоректна сума комісії')
    if fee < 0:
        raise ValueError('Комісія не може бути від’ємною')

    existing = InternalTransferMatch.objects.select_for_update().filter(
        status__in=('suggested', 'confirmed'),
    ).filter(source_transaction=anchor) if anchor.type == Transaction.TYPE_EXPENSE else InternalTransferMatch.objects.select_for_update().filter(
        status__in=('suggested', 'confirmed'), destination_transaction=anchor,
    )
    existing = existing.select_related('source_transaction', 'destination_transaction').first()
    if existing:
        return existing, (existing.destination_transaction if anchor.type == Transaction.TYPE_EXPENSE
                          else existing.source_transaction), False

    if anchor.type == Transaction.TYPE_INCOME:
        counterpart_type = Transaction.TYPE_EXPENSE
        counterpart_amount = anchor.amount + fee
        source_account, destination_account = account, anchor.account
    else:
        counterpart_type = Transaction.TYPE_INCOME
        counterpart_amount = anchor.amount - fee
        if counterpart_amount <= 0:
            raise ValueError('Комісія повинна бути меншою за витрату')
        source_account, destination_account = anchor.account, account
    counterpart_date = counterpart_date or anchor.date_actual
    counterpart = Transaction.objects.create(
        company=company, type=counterpart_type, status=Transaction.STATUS_ACTUAL,
        amount=counterpart_amount, amount_base=counterpart_amount,
        currency=anchor.currency, account=account,
        date_actual=counterpart_date, date_agreement=counterpart_date,
        comment=f'Внутрішній переказ: створено пару до операції #{anchor.id}',
        source='manual', created_by=user if getattr(user, 'is_authenticated', False) else None,
        is_business=account.is_business,
    )
    source, destination = (counterpart, anchor) if anchor.type == Transaction.TYPE_INCOME else (anchor, counterpart)
    _validate_transfer_rows(company, source, destination)
    match = InternalTransferMatch.objects.create(
        company=company, source_transaction=source, destination_transaction=destination,
        source_account=source_account, destination_account=destination_account,
        amount=min(source.amount, destination.amount), fee_amount=fee,
        confidence=Decimal('100'), status='suggested',
    )
    match = confirm_transfer(match, user=user)
    return match, counterpart, True


@db_transaction.atomic
def record_transfer_fee(match, *, fee_amount=None, fee_transaction=None, user=None):
    """Persist the fee split for a transfer match idempotently.

    When no explicit amount is supplied, the fee is derived from the source
    debit and destination credit.  A linked fee row is optional: imported
    statements commonly contain only the unequal debit/credit pair, while a
    separate bank commission row can be attached when available.
    """
    match = InternalTransferMatch.objects.select_for_update().select_related(
        'source_transaction', 'destination_transaction', 'fee_transaction',
    ).get(pk=match.pk)
    calculated = calculate_transfer_fee(
        match.source_transaction.amount, match.destination_transaction.amount,
    )
    amount = calculated if fee_amount is None else Decimal(str(fee_amount))
    if amount < 0 or amount > match.source_transaction.amount:
        raise ValueError('Некоректна сума комісії переказу')
    if amount != calculated:
        raise ValueError('Сума комісії має дорівнювати різниці дебету та кредиту')
    if fee_transaction is not None:
        fee_transaction = Transaction.objects.get(pk=fee_transaction.pk)
        if fee_transaction.company_id != match.company_id:
            raise ValueError('Комісійна операція має належати тій самій компанії')
        if fee_transaction.type != Transaction.TYPE_EXPENSE:
            raise ValueError('Комісійна операція має бути витратою')
        if amount and fee_transaction.amount != amount:
            raise ValueError('Сума комісійної операції не збігається з комісією')

    changed = []
    if match.fee_amount != amount:
        match.fee_amount = amount
        changed.append('fee_amount')
    if match.fee_transaction_id != getattr(fee_transaction, 'id', None):
        match.fee_transaction = fee_transaction
        changed.append('fee_transaction')
    if changed:
        match.save(update_fields=changed)
    if fee_transaction is not None and amount:
        classify_transaction(
            fee_transaction, user=user, ownership_scope='business',
            economic_kind=TRANSFER_FEE_KIND, source='review',
            note='Комісія за підтверджений внутрішній переказ.',
        )
    return match


@db_transaction.atomic
def confirm_transfer(match, *, user=None):
    original_match = match
    match = InternalTransferMatch.objects.select_for_update().get(pk=match.pk)
    if match.status == 'confirmed':
        # Older confirmed matches may predate fee persistence.  Fill only the
        # missing derived value; repeating confirmation remains idempotent.
        if match.fee_amount == 0:
            match = record_transfer_fee(match, user=user)
        original_match.status = match.status
        original_match.fee_amount = match.fee_amount
        original_match.fee_transaction_id = match.fee_transaction_id
        return original_match
    match = record_transfer_fee(match, user=user)
    match.status = 'confirmed'
    match.confirmed_by = user if getattr(user, 'is_authenticated', False) else None
    match.confirmed_at = timezone.now()
    match.save(update_fields=['status', 'confirmed_by', 'confirmed_at'])
    classify_transaction(match.source_transaction, user=user, ownership_scope='unknown', economic_kind='internal_transfer', source='review')
    classify_transaction(match.destination_transaction, user=user, ownership_scope='unknown', economic_kind='internal_transfer', source='review')
    original_match.status = match.status
    original_match.fee_amount = match.fee_amount
    original_match.fee_transaction_id = match.fee_transaction_id
    original_match.confirmed_by_id = match.confirmed_by_id
    original_match.confirmed_at = match.confirmed_at
    return original_match


def funding_summary(source):
    received = source.allocations.filter(allocation_type='received').aggregate(v=Sum('amount'))['v'] or Decimal('0')
    spent = source.allocations.filter(allocation_type='spent').aggregate(v=Sum('amount'))['v'] or Decimal('0')
    reserved = source.allocations.filter(allocation_type='reserved').aggregate(v=Sum('amount'))['v'] or Decimal('0')
    received = max(received, source.received_amount)
    return {'received': received, 'spent': spent, 'reserved': reserved,
            'available': received - spent - reserved}


def allocate_funding(*, source, txn, amount, allocation_type='spent', user=None, note='', replace=False):
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
        if replace:
            existing.amount = amount
            existing.note = note or existing.note
            existing.save(update_fields=['amount', 'note'])
            return existing
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
