"""Сервіс операцій: створення/редагування/видалення з перерахунком балансів,
аудитом, дублюванням, розділенням і перетворенням у переказ.

Єдина точка проходження всіх грошових операцій (ТЗ §10): після будь-якої
зміни перераховуємо баланси задіяних рахунків і пишемо AuditLog.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from ..models import Account, Transaction, get_default_company
from . import audit as audit_service
from . import balances as balance_service
from . import currency as currency_service


def _affected_accounts(txn: Transaction):
    """Рахунки, чий баланс залежить від операції."""
    accounts = set()
    if txn.account_id:
        accounts.add(txn.account_id)
    if txn.to_account_id:
        accounts.add(txn.to_account_id)
    return accounts


def _recalc_accounts(account_ids):
    for acc in Account.objects.filter(id__in=account_ids):
        acc.recalc_balance(save=True)


def _compute_amount_base(company, txn: Transaction) -> Decimal:
    on_date = txn.date_actual.date() if txn.date_actual else None
    return currency_service.convert(company, txn.amount, txn.currency,
                                    company.base_currency, on_date)


@db_transaction.atomic
def create_transaction(*, user, type, amount, account=None, to_account=None,
                       currency=None, status=Transaction.STATUS_ACTUAL,
                       date_actual=None, date_agreement=None, category=None,
                       counterparty=None, project=None, tags=None, comment='',
                       to_amount=None, exchange_rate=None, source='manual',
                       linked_invoice=None, recurrence_rule=None, external_id='',
                       is_demo=False, is_business=None, external_data=None, mcc=None,
                       amount_is_estimated=False) -> Transaction:
    """Створення операції з повним перерахунком."""
    company = get_default_company()
    if date_actual is None:
        date_actual = timezone.now()
    if currency is None:
        currency = account.currency if account else company.base_currency
    # Бізнес/особисте: явне значення або успадкування від рахунку-джерела.
    if is_business is None:
        is_business = bool(account.is_business) if account is not None else False

    txn = Transaction(
        company=company, type=type, status=status, amount=Decimal(amount),
        currency=currency, account=account, to_account=to_account,
        to_amount=Decimal(to_amount) if to_amount is not None else None,
        exchange_rate=Decimal(exchange_rate) if exchange_rate is not None else None,
        date_actual=date_actual, date_agreement=date_agreement,
        category=category, counterparty=counterparty, project=project,
        comment=comment or '', source=source, linked_invoice=linked_invoice,
        recurrence_rule=recurrence_rule, is_recurring=bool(recurrence_rule),
        external_id=external_id or '', is_demo=is_demo, is_business=is_business,
        external_data=external_data or {}, mcc=mcc,
        amount_is_estimated=bool(amount_is_estimated),
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    txn.amount_base = _compute_amount_base(company, txn)
    txn.save()
    if tags:
        txn.tags.set(tags)

    if status == Transaction.STATUS_ACTUAL:
        _recalc_accounts(_affected_accounts(txn))
    if linked_invoice is not None:
        linked_invoice.recalc_status(save=True)

    audit_service.log_action(
        user, 'create', 'transaction', txn.id,
        summary=f'{txn.get_type_display()} {txn.amount} {txn.currency}',
        after=_snapshot(txn), source=source, company=company,
    )
    if status == Transaction.STATUS_ACTUAL:
        try:
            from .ledger_v2 import ensure_grant_account_review, process_counterparty_policy
            ensure_grant_account_review(txn)
            process_counterparty_policy(txn, user=user)
        except Exception:  # noqa: BLE001 — review creation must not block imports
            pass
    return txn


@db_transaction.atomic
def update_transaction(txn: Transaction, *, user, **fields) -> Transaction:
    """Оновлення: відкат впливу старої операції → застосування нової (§6)."""
    company = txn.company
    before = _snapshot(txn)
    old_accounts = _affected_accounts(txn)
    old_invoice = txn.linked_invoice

    tags = fields.pop('tags', None)
    for key, value in fields.items():
        setattr(txn, key, value)
    txn.is_recurring = bool(txn.recurrence_rule_id)
    txn.amount_base = _compute_amount_base(company, txn)
    txn.updated_by = user if getattr(user, 'is_authenticated', False) else None
    txn.save()
    if tags is not None:
        txn.tags.set(tags)

    # Перерахунок усіх задіяних рахунків (старих і нових).
    _recalc_accounts(old_accounts | _affected_accounts(txn))
    for inv in {old_invoice, txn.linked_invoice}:
        if inv is not None:
            inv.recalc_status(save=True)

    audit_service.log_action(
        user, 'update', 'transaction', txn.id,
        summary=f'Зміна операції #{txn.id}', before=before, after=_snapshot(txn),
        company=company,
    )
    if txn.status == Transaction.STATUS_ACTUAL:
        try:
            from .ledger_v2 import process_counterparty_policy
            process_counterparty_policy(txn, user=user)
        except Exception:  # noqa: BLE001 - optional review must not block updates
            pass
    return txn


@db_transaction.atomic
def delete_transaction(txn: Transaction, *, user) -> None:
    """Видалення з перерахунком балансів."""
    company = txn.company
    before = _snapshot(txn)
    accounts = _affected_accounts(txn)
    invoice = txn.linked_invoice
    txn_id = txn.id
    # Видаляємо також дочірні частини (split).
    txn.children.all().delete()
    txn.delete()
    _recalc_accounts(accounts)
    if invoice is not None:
        invoice.recalc_status(save=True)
    audit_service.log_action(
        user, 'delete', 'transaction', txn_id,
        summary=f'Видалено операцію #{txn_id}', before=before, company=company,
    )


@db_transaction.atomic
def duplicate_transaction(txn: Transaction, *, user) -> Transaction:
    """Копія операції як нова (ТЗ §7)."""
    new = create_transaction(
        user=user, type=txn.type, amount=txn.amount, account=txn.account,
        to_account=txn.to_account, currency=txn.currency, status=txn.status,
        date_actual=timezone.now(), category=txn.category,
        counterparty=txn.counterparty, project=txn.project,
        tags=list(txn.tags.all()), comment=txn.comment, to_amount=txn.to_amount,
        exchange_rate=txn.exchange_rate, source='manual',
    )
    return new


@db_transaction.atomic
def convert_to_transfer(txn: Transaction, *, user, to_account, to_amount=None) -> Transaction:
    """Перетворення доходу/витрати у переказ (ТЗ §5)."""
    return update_transaction(
        txn, user=user, type=Transaction.TYPE_TRANSFER, to_account=to_account,
        to_amount=Decimal(to_amount) if to_amount is not None else txn.amount,
        category=None, counterparty=None,
    )


@db_transaction.atomic
def split_transaction(txn: Transaction, *, user, parts) -> list:
    """Розділення на частини; сума частин = сумі оригіналу (ТЗ §7).

    parts: список dict(amount, category, project, comment).
    Оригінал стає parent і виключається зі звітів (його замінюють діти).
    """
    total = sum((Decimal(str(p['amount'])) for p in parts), Decimal('0'))
    if total != txn.amount:
        raise ValueError('Сума частин має дорівнювати сумі платежу')

    group = uuid.uuid4().hex[:16]
    children = []
    for p in parts:
        child = create_transaction(
            user=user, type=txn.type, amount=p['amount'], account=txn.account,
            currency=txn.currency, status=txn.status, date_actual=txn.date_actual,
            date_agreement=txn.date_agreement, category=p.get('category'),
            counterparty=txn.counterparty, project=p.get('project'),
            comment=p.get('comment', txn.comment), source='manual',
        )
        child.parent_transaction = txn
        child.split_group = group
        child.save(update_fields=['parent_transaction', 'split_group'])
        children.append(child)

    txn.is_split = True
    txn.split_group = group
    txn.excluded_from_reports = True
    txn.save(update_fields=['is_split', 'split_group', 'excluded_from_reports'])
    _recalc_accounts(_affected_accounts(txn))
    audit_service.log_action(user, 'split', 'transaction', txn.id,
                             summary=f'Розділено на {len(children)} частин', company=txn.company)
    return children


@db_transaction.atomic
def mark_planned_actual(txn: Transaction, *, user, date_actual=None) -> Transaction:
    """Підтвердження планової операції як фактичної (впливає на баланс)."""
    return update_transaction(
        txn, user=user, status=Transaction.STATUS_ACTUAL,
        date_actual=date_actual or timezone.now(),
    )


def _snapshot(txn: Transaction) -> dict:
    """Серіалізація стану операції для аудиту."""
    return {
        'type': txn.type, 'status': txn.status, 'amount': str(txn.amount),
        'currency': txn.currency, 'account': txn.account_id,
        'to_account': txn.to_account_id,
        'date_actual': txn.date_actual.isoformat() if txn.date_actual else None,
        'category': txn.category_id, 'counterparty': txn.counterparty_id,
        'project': txn.project_id, 'comment': txn.comment,
    }
