"""Картки контрагентів: розпізнавання за платежем + збереження (upsert).

Дані отримувача P2P-переказу беремо з ``external_data`` (counter_iban,
counter_name з Monobank) та з маски картки в коментарі. Повного PAN майже ніколи
немає — є маска/останні цифри/IBAN/імʼя. Тому:

- **Сильний матч** (впевнено той самий контрагент): збіг IBAN або повної маски.
- **Слабкий матч** (тільки останні 4 цифри): можливі колізії між людьми →
  НЕ привʼязуємо автоматично, повертаємо кандидатів і перепитуємо в UI.
"""
from __future__ import annotations

import re
import unicodedata

from django.utils import timezone

from ..models import Counterparty, CounterpartyAlias, CounterpartyCard
from .serializers import bank_from_iban

# 4-6 цифр + зірочки + 2-4 цифри (маска monobank), або групи цифр із хвостом.
_MASK_RE = re.compile(r'\b(\d{4,6}\*{2,}\d{2,4})\b')
_TAIL_RE = re.compile(r'(\d{4})\b')


def normalize_alias(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value or '').casefold())


def assign_recognized_counterparty(txn):
    """Assign only an unambiguous saved recipient, preserving explicit choices."""
    if txn.counterparty_id or txn.type not in ('income', 'expense') or txn.status != 'actual':
        return
    match = match_counterparty_by_payment(txn.company, txn)
    if match['strong'] is not None:
        txn.counterparty = match['strong']


def extract_card_hint(txn) -> dict:
    """Витягує реквізити отримувача з платежу.

    Повертає {'pan_mask','last4','bank','iban','counter_name'} — порожні рядки,
    якщо даних немає.
    """
    ed = txn.external_data or {}
    iban = (ed.get('counter_iban') or '').strip()
    counter_name = (ed.get('counter_name') or '').strip()
    bank = bank_from_iban(iban) if iban else ''

    pan_mask = ''
    last4 = ''
    comment = txn.comment or ''
    m = _MASK_RE.search(comment)
    if m:
        pan_mask = m.group(1)
        digits = ''.join(ch for ch in pan_mask if ch.isdigit())
        last4 = digits[-4:] if len(digits) >= 4 else ''
    elif comment:
        # Маски немає — пробуємо знайти хвіст із 4 цифр у тексті переказу.
        tails = _TAIL_RE.findall(comment)
        if tails:
            last4 = tails[-1]

    if not bank and ed.get('counter_bank'):
        bank = ed.get('counter_bank')

    return {'pan_mask': pan_mask, 'last4': last4, 'bank': bank,
            'iban': iban, 'counter_name': counter_name}


def recipient_index(company):
    index = {key: {} for key in ('iban', 'mask', 'last4', 'alias')}
    for card in CounterpartyCard.objects.filter(company=company, counterparty__company=company).select_related('counterparty'):
        for key, value in (('iban', card.iban), ('mask', card.pan_mask), ('last4', card.last4)):
            if value:
                index[key].setdefault(value, {})[card.counterparty_id] = card.counterparty
    for cp in Counterparty.objects.filter(company=company).exclude(iban=''):
        index['iban'].setdefault(cp.iban, {})[cp.id] = cp
    for alias in CounterpartyAlias.objects.filter(company=company, counterparty__company=company, is_active=True).select_related('counterparty'):
        index['alias'].setdefault(alias.normalized, {})[alias.counterparty_id] = alias.counterparty
    return index


def match_counterparty_by_payment(company, txn, *, index=None) -> dict:
    """Підбирає контрагента за реквізитами платежу.

    Повертає {'strong': Counterparty|None, 'candidates': [Counterparty,...],
    'reason': str}. ``strong`` — впевнений збіг (IBAN/повна маска). ``candidates``
    — слабкі збіги за останніми 4 цифрами (перепитати в UI).
    """
    hint = extract_card_hint(txn)
    index = recipient_index(company) if index is None else index

    # Сильний матч: IBAN.
    if hint['iban']:
        people = index['iban'].get(hint['iban'], {})
        if people:
            return {'strong': next(iter(people.values())) if len(people) == 1 else None,
                    'candidates': list(people.values()) if len(people) > 1 else [], 'reason': 'iban'}
    # Сильний матч: повна маска.
    if hint['pan_mask']:
        people = index['mask'].get(hint['pan_mask'], {})
        if people:
            return {'strong': next(iter(people.values())) if len(people) == 1 else None,
                    'candidates': list(people.values()) if len(people) > 1 else [], 'reason': 'mask'}

    # Exact aliases are saved only after a person confirms the bank name.
    # Do not match substrings such as another person with the same first name.
    names = {normalize_alias(hint['counter_name']), normalize_alias(txn.comment)} - {''}
    people = {cp_id: cp for name in names for cp_id, cp in index['alias'].get(name, {}).items()}
    if people:
        return {'strong': next(iter(people.values())) if len(people) == 1 else None,
                'candidates': list(people.values()) if len(people) > 1 else [], 'reason': 'confirmed_alias'}

    # Слабкий матч: останні 4 цифри. Колізії можливі → кандидати.
    if hint['last4']:
        cps = list(index['last4'].get(hint['last4'], {}).values())
        if len(cps) == 1:
            # Один кандидат за last4 — все одно слабкий, але зручно підказати.
            return {'strong': None, 'candidates': cps, 'reason': 'last4_single'}
        if cps:
            return {'strong': None, 'candidates': cps, 'reason': 'last4_multi'}

    return {'strong': None, 'candidates': [], 'reason': 'none'}


def upsert_card(counterparty, *, pan_mask='', last4='', bank='', iban='', label='',
                make_primary=False, when=None) -> CounterpartyCard:
    """Створює або оновлює картку контрагента.

    Дедуп за IBAN або повною маскою (надійні ідентифікатори). За одним лише
    last4 НЕ дедупимо — це можуть бути різні картки. ``when`` оновлює last_used_at.
    """
    company = counterparty.company
    last4 = last4 or (''.join(ch for ch in pan_mask if ch.isdigit())[-4:] if pan_mask else '')
    if not bank and iban:
        bank = bank_from_iban(iban) or ''

    card = None
    if iban:
        card = CounterpartyCard.objects.filter(company=company, counterparty=counterparty,
                                               iban=iban).first()
    if card is None and pan_mask:
        card = CounterpartyCard.objects.filter(company=company, counterparty=counterparty,
                                               pan_mask=pan_mask).first()

    if card is None:
        card = CounterpartyCard(company=company, counterparty=counterparty)

    # Оновлюємо лише непорожніми значеннями (не затираємо наявні дані).
    if pan_mask:
        card.pan_mask = pan_mask
    if last4:
        card.last4 = last4
    if bank:
        card.bank = bank
    if iban:
        card.iban = iban
    if label:
        card.label = label
    if make_primary:
        card.is_primary = True
    card.last_used_at = when or timezone.now()
    card.save()

    if make_primary:
        CounterpartyCard.objects.filter(company=company, counterparty=counterparty)\
            .exclude(pk=card.pk).update(is_primary=False)
    return card


def cards_for(counterparty) -> list:
    """Список карток контрагента для UI (полоски-картки)."""
    out = []
    for c in counterparty.cards.all():
        out.append({
            'id': c.id,
            'display': c.display,
            'bank': c.bank,
            'last4': c.tail,
            'pan_mask': c.pan_mask,
            'iban': c.iban,
            'label': c.label,
            'is_primary': c.is_primary,
            'incomplete': not (c.iban or c.pan_mask),
        })
    return out
