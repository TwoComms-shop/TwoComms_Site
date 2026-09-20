"""Серіалізація операцій та довідників у JSON для журналу/модалок."""
from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from ..models import Transaction

_SYMBOLS = {'UAH': '₴', 'USD': '$', 'EUR': '€', 'PLN': 'zł', 'GBP': '£'}

_MONTHS_UK = ['', 'січ', 'лют', 'бер', 'кві', 'тра', 'чер', 'лип', 'сер', 'вер', 'жов', 'лис', 'гру']


def currency_symbol(code: str) -> str:
    return _SYMBOLS.get(code, code or '')


def money(amount, currency='UAH', signed=False) -> str:
    """Форматування суми: пробіл-роздільник тисяч, символ валюти."""
    if amount is None:
        amount = Decimal('0')
    amount = Decimal(amount)
    sign = ''
    if signed and amount > 0:
        sign = '+'
    elif amount < 0:
        sign = '−'
    whole = f'{abs(amount):,.2f}'.replace(',', ' ')
    # Прибираємо .00 для цілих.
    if whole.endswith('.00'):
        whole = whole[:-3]
    return f'{sign}{whole} {currency_symbol(currency)}'.strip()


def date_label(value):
    if not value:
        return ''
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return f'{local.day} {_MONTHS_UK[local.month]} {local.year}'


def time_label(value):
    if not value:
        return ''
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return local.strftime('%H:%M')


def serialize_transaction(txn: Transaction, *, running_balance=None) -> dict:
    """Рядок журналу/дані для drawer."""
    is_income = txn.type == Transaction.TYPE_INCOME
    is_transfer = txn.type == Transaction.TYPE_TRANSFER

    if is_transfer:
        amount_display = money(txn.amount, txn.currency)
        amount_class = 'transfer'
    elif is_income:
        amount_display = money(txn.amount, txn.currency, signed=True)
        amount_class = 'pos'
    else:
        amount_display = money(-txn.amount, txn.currency, signed=True)
        amount_class = 'neg'

    account_name = txn.account.name if txn.account else '—'
    if is_transfer and txn.to_account:
        account_name = f'{account_name} → {txn.to_account.name}'

    return {
        'id': txn.id,
        'type': txn.type,
        'type_display': txn.get_type_display(),
        'status': txn.status,
        'status_display': txn.get_status_display(),
        'is_planned': txn.is_planned,
        'amount': str(txn.amount),
        'currency': txn.currency,
        'amount_display': amount_display,
        'amount_class': amount_class,
        'date': date_label(txn.date_actual),
        'time': time_label(txn.date_actual),
        'date_actual': timezone.localtime(txn.date_actual).strftime('%Y-%m-%dT%H:%M') if txn.date_actual else '',
        'date_agreement': timezone.localtime(txn.date_agreement).strftime('%Y-%m-%dT%H:%M') if txn.date_agreement else '',
        'account_id': txn.account_id,
        'to_account_id': txn.to_account_id,
        'account_name': account_name,
        'running_balance': money(running_balance, txn.account.currency if txn.account else 'UAH') if running_balance is not None else '',
        'counterparty_id': txn.counterparty_id,
        'counterparty': txn.counterparty.name if txn.counterparty else '',
        'category_id': txn.category_id,
        'category': txn.category.name if txn.category else '',
        'project_id': txn.project_id,
        'project': txn.project.name if txn.project else '',
        'comment': txn.comment,
        'tags': [{'id': t.id, 'name': t.name} for t in txn.tags.all()],
        'attachments': [{'id': a.id, 'name': a.original_name or 'файл',
                         'url': (a.file.url if a.file else '')} for a in txn.attachments.all()],
        'source': txn.source,
        'is_business': txn.is_business,
        'ownership_scope': txn.ownership_scope,
        'economic_kind': txn.economic_kind,
        'funding_source_id': txn.funding_source_id,
        'funding_source': txn.funding_source.name if txn.funding_source else '',
        'requires_classification': bool(
            txn.status == Transaction.STATUS_ACTUAL
            and txn.type == Transaction.TYPE_INCOME
            and txn.economic_kind == 'unknown'
        ),
        'mcc': txn.mcc,
        'mcc_label': _mcc_label(txn.mcc) if txn.mcc else '',
        'cashback': _cashback_display(txn),
        'counter_name': (txn.external_data or {}).get('counter_name', ''),
        'counter_iban': (txn.external_data or {}).get('counter_iban', ''),
        'card_transfer': _is_card_transfer(txn),
        'card_transfer_label': _card_transfer_label(txn),
        'to_amount': str(txn.to_amount) if txn.to_amount is not None else '',
        'is_recurring': bool(txn.recurrence_rule_id),
        'recurrence_rule_id': txn.recurrence_rule_id,
        'recurrence_label': (txn.recurrence_rule.frequency_label if txn.recurrence_rule_id else ''),
        # Компактний індикатор межі повторення (∞ безстроково / ×N разів / до ДД.ММ)
        # — щоб у журналі планові показувались так само красиво, як ×6 у магазинів.
        'recurrence_badge': (txn.recurrence_rule.repeat_badge if txn.recurrence_rule_id else ''),
        # Поточні налаштування правила — щоб модалка редагування показувала
        # реальний стан повторення (а не порожній бланк).
        'recurrence_frequency': (txn.recurrence_rule.frequency if txn.recurrence_rule_id else ''),
        'recurrence_interval': (txn.recurrence_rule.interval if txn.recurrence_rule_id else 1),
        'recurrence_end_mode': (txn.recurrence_rule.end_mode if txn.recurrence_rule_id else ''),
        'recurrence_end_date': (txn.recurrence_rule.end_date.isoformat()
                                if txn.recurrence_rule_id and txn.recurrence_rule.end_date else ''),
        'recurrence_count': (txn.recurrence_rule.count if txn.recurrence_rule_id else None),
        'recurrence_title': (txn.recurrence_rule.title if txn.recurrence_rule_id else ''),
        'amount_is_estimated': bool(txn.amount_is_estimated),
        'reseller_id': txn.reseller_id,
    }


# MFO (код банку у позиціях 4:10 українського IBAN) → назва банку.
_BANK_BY_MFO = {
    '322001': 'monobank',
    '305299': 'ПриватБанк',
    '300465': 'Ощадбанк',
    '320478': 'Райффайзен',
    '300335': 'Райффайзен',
    '334851': 'ПУМБ',
    '322313': 'А-Банк',
    '307770': 'OTP Bank',
    '380805': 'Sense Bank',
    '300023': 'Укрексімбанк',
    '320649': 'Sense Bank',
}


def bank_from_iban(iban: str) -> str:
    """Назва банку за MFO в IBAN (UA + 2 контрольні + 6 MFO). '' якщо невідомо."""
    if not iban or len(iban) < 10 or not iban.upper().startswith('UA'):
        return ''
    return _BANK_BY_MFO.get(iban[4:10], '')


def _is_card_transfer(txn) -> bool:
    """Витрата, що фактично є переказом на чужу картку/рахунок (P2P).

    Ознака: є counter_iban або counter_name (Monobank P2P), тип — витрата
    (внутрішні перекази між власними рахунками — окремий тип transfer).
    """
    if txn.type != Transaction.TYPE_EXPENSE:
        return False
    ed = txn.external_data or {}
    if ed.get('counter_iban') or ed.get('counter_name'):
        return True
    # Перекази на чужу картку monobank часто не мають counter_iban/name —
    # розпізнаємо за MCC грошових переказів або за маскою картки в коментарі.
    from . import mcc as mcc_mod
    if txn.mcc is not None and mcc_mod.group_for_mcc(txn.mcc) == 'cash_finance':
        return True
    if _card_mask_in_comment(txn.comment):
        return True
    return False


_CARD_MASK_RE = None


def _card_mask_in_comment(comment: str) -> str:
    """Повертає маску картки з коментаря (напр. '414960****7321') або ''."""
    global _CARD_MASK_RE
    if not comment:
        return ''
    if _CARD_MASK_RE is None:
        import re
        # 4-6 цифр + зірочки + 2-4 цифри (маска картки monobank P2P).
        _CARD_MASK_RE = re.compile(r'\b(\d{4,6}\*{2,}\d{2,4})\b')
    m = _CARD_MASK_RE.search(comment)
    return m.group(1) if m else ''


def _card_transfer_label(txn) -> str:
    """Підпис отримувача P2P-переказу: банк (за IBAN) або ім'я контрагента/маска."""
    if not _is_card_transfer(txn):
        return ''
    ed = txn.external_data or {}
    bank = bank_from_iban(ed.get('counter_iban', ''))
    name = ed.get('counter_name', '')
    if bank and name:
        return f'{bank} · {name}'
    if bank or name:
        return bank or name
    # Без IBAN/імені — маска картки з коментаря.
    mask = _card_mask_in_comment(txn.comment)
    if mask:
        return f'картка {mask}'
    return 'переказ на картку'


def _mcc_label(mcc):
    from . import mcc as mcc_mod
    return mcc_mod.label_for_mcc(mcc)


def _cashback_display(txn):
    """Кешбек у грн з external_data (Monobank віддає копійки)."""
    cb = (txn.external_data or {}).get('cashback_amount')
    if not cb:
        return ''
    try:
        return money(Decimal(int(cb)) / Decimal('100'), txn.currency)
    except (TypeError, ValueError):
        return ''


def settlement_labels_for(company, txn_ids) -> dict:
    """Мапа payment_id → {title, period_label, periods} для фактичних платежів,
    що погасили зобов'язання. Для бейджа «погашення кредиторки/дебіторки» у журналі.

    Один запит на всі id сторінки (без N+1).
    """
    from ..models import ObligationSettlement
    if not txn_ids:
        return {}
    grouped = {}
    qs = (ObligationSettlement.objects
          .filter(company=company, payment_id__in=list(txn_ids))
          .select_related('rule'))
    for s in qs:
        grouped.setdefault(s.payment_id, []).append(s)
    out = {}
    for pid, items in grouped.items():
        title = next((s.rule.title for s in items if s.rule_id and s.rule.title), '') or "зобов'язання"
        labels = sorted({s.period_label for s in items if s.period_label})
        if not labels:
            period_label = ''
        elif len(labels) == 1:
            period_label = labels[0]
        else:
            period_label = f'{labels[0]}…{labels[-1]}'
        out[pid] = {'title': title, 'period_label': period_label, 'periods': len(items)}
    return out


def serialize_dropdowns(company) -> dict:
    """Довідники для модалок (рахунки, категорії, проєкти, контрагенти, теги)."""
    accounts = [{'id': a.id, 'name': a.name, 'currency': a.currency,
                 'balance': str(a.current_balance)}
                for a in company.accounts.filter(is_active=True, is_archived=False).order_by('sort_order')]
    income_cats = [{'id': c.id, 'name': c.name, 'parent': c.parent_id}
                   for c in company.categories.filter(is_active=True).exclude(type='expense')]
    expense_cats = [{'id': c.id, 'name': c.name, 'parent': c.parent_id}
                    for c in company.categories.filter(is_active=True).exclude(type='income')]
    projects = [{'id': p.id, 'name': p.name} for p in company.projects.all().order_by('sort_order')]
    counterparties = [{'id': c.id, 'name': c.name, 'group': c.group or c.get_type_display()}
                      for c in company.counterparties.all().order_by('name')]
    tags = [{'id': t.id, 'name': t.name} for t in company.tags.all().order_by('name')]
    funding_sources = [
        {'id': source.id, 'name': source.name}
        for source in company.funding_sources.filter(is_active=True).order_by('name')
    ]
    return {
        'accounts': accounts,
        'income_categories': income_cats,
        'expense_categories': expense_cats,
        'projects': projects,
        'counterparties': counterparties,
        'tags': tags,
        'funding_sources': funding_sources,
    }
