"""Точне погашення зобов'язань (кредиторка/дебіторка) з прив'язкою до реального платежу.

Дзеркало ``consignment.register_payment`` для загальних зобов'язань (повторювані +
разові планові). Два режими:
  - ``new_payment`` — створити новий фактичний платіж (expense/income) з рахунку;
  - ``pick_txn``    — прив'язати вже наявний фактичний платіж (НЕ дублюючи його,
                      тож баланс не змінюється — гроші вже пішли).

ІНВАРІАНТИ:
  • Погашення фактом НІКОЛИ не змінює ``template_amount`` (оцінку зобов'язання).
    Тому наступний місяць показує ту саму орієнтовну суму, а не фактично сплачену.
  • Повне погашення періоду повторюваного зобов'язання просуває його на наступний
    період (нагадування «перестрибує» на наступний місяць). Часткове — лишає
    залишок плановим на тому ж періоді.
  • Кожне погашення фіксується ``ObligationSettlement`` (молекулярна точність:
    яка сума, яким платежем, з якого рахунку, за який період).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction as db_transaction
from django.utils import timezone

from ..models import ObligationSettlement, RecurrenceRule, Transaction
from . import audit as audit_service
from . import cards as cards_service
from . import recurring as recurring_service
from . import transactions as txn_service
from .timeutil import day_start


def _period_for(planned_txn) -> tuple[str, str]:
    """('YYYY-MM', 'MM.YYYY') періоду планового екземпляра."""
    d = planned_txn.date_actual.date() if planned_txn.date_actual else timezone.localdate()
    return f'{d.year}-{d.month:02d}', d.strftime('%m.%Y')


def _advance_rule(rule, *, user, from_date) -> None:
    """Просуває повторюване правило на наступний період (як settle_occurrence).

    next_occurrence лишається попереду (його виставив initial materialize), тож
    скасований поточний екземпляр НЕ регенерується (генерація йде лише вперед від
    next_occurrence). Викликаємо materialize, щоб тримати горизонт заповненим.
    """
    if rule is None or not rule.is_active:
        return
    if rule.next_occurrence is None or rule.next_occurrence <= from_date:
        nxt = recurring_service.calculate_next_occurrence(rule, from_date)
        if nxt is not None:
            rule.next_occurrence = nxt
            rule.save(update_fields=['next_occurrence'])
    recurring_service.materialize(rule, user=user)


@db_transaction.atomic
def settle_obligation(*, user, planned_txn, mode, amount=None, payment_txn=None,
                      account=None, date=None, counterparty=None,
                      full_period=None, remember_card=False, card_hint=None,
                      periods=1) -> dict:
    """Гасить зобов'язання, представлене найближчим плановим екземпляром.

    ``periods`` > 1 — оплата за кілька періодів наперед (напр. оренда на 3 місяці):
    покриваємо найближчі N планових екземплярів одним платежем, таймер
    «перестрибує» на N періодів. Повертає {'payment','settlement','full_period','periods'}.
    """
    if planned_txn.status != Transaction.STATUS_PLANNED:
        raise ValueError("Гасити можна лише планове зобов'язання")

    periods = max(1, int(periods or 1))
    rule = planned_txn.recurrence_rule
    if periods > 1 and rule is not None:
        return _settle_multi(
            user=user, planned_txn=planned_txn, rule=rule, mode=mode, amount=amount,
            payment_txn=payment_txn, account=account, date=date,
            counterparty=counterparty, periods=periods,
            remember_card=remember_card, card_hint=card_hint)

    company = planned_txn.company
    ttype = planned_txn.type
    plan_amount = planned_txn.amount or Decimal('0')
    counterparty = counterparty or planned_txn.counterparty
    when = date or timezone.now()
    period_key, period_label = _period_for(planned_txn)
    planned_date = planned_txn.date_actual.date() if planned_txn.date_actual else timezone.localdate()

    # --- Валідація режиму та суми ---
    if mode == 'new_payment':
        if amount in (None, ''):
            amount = plan_amount
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError('Сума має бути більшою за 0')
        if account is None:
            raise ValueError('Оберіть рахунок списання/зарахування')
    elif mode == 'pick_txn':
        if payment_txn is None:
            raise ValueError('Не обрано платіж')
        if payment_txn.status != Transaction.STATUS_ACTUAL:
            raise ValueError('Платіж має бути фактичним')
        if payment_txn.type != ttype:
            raise ValueError('Тип платежу не збігається із зобов\'язанням')
        if amount in (None, ''):
            amount = payment_txn.amount
        amount = Decimal(str(amount))
        if amount <= 0 or amount > payment_txn.amount:
            raise ValueError('Сума погашення невірна')
    else:
        raise ValueError(f'Невідомий режим: {mode}')

    if full_period is None:
        full_period = amount >= plan_amount

    # --- 1) Отримати/створити фактичний платіж ---
    if mode == 'new_payment':
        if full_period:
            # Провести саме цей плановий екземпляр як фактичний (advance + count
            # для count-режиму). template_amount settle_occurrence НЕ чіпає.
            payment = recurring_service.settle_occurrence(
                planned_txn, user=user, account=account, counterparty=counterparty,
                date_actual=when, amount=amount)
        else:
            payment = txn_service.create_transaction(
                user=user, type=ttype, status=Transaction.STATUS_ACTUAL,
                amount=amount, account=account,
                currency=account.currency if account else company.base_currency,
                counterparty=counterparty, date_actual=when,
                comment=(planned_txn.comment or (rule.title if rule else '')),
                source='manual', is_business=planned_txn.is_business)
    else:  # pick_txn — використовуємо наявний платіж, нічого не дублюємо.
        payment = payment_txn
        upd = {}
        if counterparty and not payment.counterparty_id:
            upd['counterparty'] = counterparty
        if rule and full_period and not payment.recurrence_rule_id:
            # Прив'язка до правила, щоб «скільки лишилось» (count) рахувало платіж.
            upd['recurrence_rule'] = rule
        if upd:
            txn_service.update_transaction(payment, user=user, **upd)

    # --- 2) Застосувати до планового екземпляра ---
    if full_period:
        if mode == 'pick_txn':
            # Плановий виконано наявним фактичним → скасувати, просунути правило.
            txn_service.update_transaction(planned_txn, user=user,
                                           status=Transaction.STATUS_CANCELLED)
            _advance_rule(rule, user=user, from_date=planned_date)
        # для new_payment full — settle_occurrence уже скасував/просунув.
    else:
        # Частково: зменшити плановий на сплачене (залишок лишається плановим).
        remainder = plan_amount - amount
        if remainder <= 0:
            txn_service.update_transaction(planned_txn, user=user,
                                           status=Transaction.STATUS_CANCELLED)
            _advance_rule(rule, user=user, from_date=planned_date)
            full_period = True
        else:
            txn_service.update_transaction(planned_txn, user=user, amount=remainder)

    # --- 3) Зв'язок-погашення (молекулярна точність) ---
    # planned_txn-посилання лишаємо лише коли плановий ще існує як окремий обʼєкт
    # (частково або pick_txn). Для new_payment full плановий СТАВ платежем.
    keep_planned_ref = not (full_period and mode == 'new_payment')
    settlement = ObligationSettlement.objects.create(
        company=company, payment=payment, rule=rule,
        planned_txn=(planned_txn if keep_planned_ref else None),
        period_key=period_key, period_label=period_label,
        amount=amount, currency=payment.currency,
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )

    # --- 4) Запам'ятати картку контрагента (за згодою) ---
    if remember_card and counterparty and card_hint:
        cards_service.upsert_card(
            counterparty, when=when,
            pan_mask=card_hint.get('pan_mask', ''), last4=card_hint.get('last4', ''),
            bank=card_hint.get('bank', ''), iban=card_hint.get('iban', ''))

    return {'payment': payment, 'settlement': settlement, 'full_period': full_period}


def payable_candidates(company, *, ttype, counterparty=None, limit=60) -> list:
    """Кандидати — фактичні платежі потрібного типу, ще не прив'язані до зобов'язань.

    Пріоритет: контрагент платежу збігається > рахунок прив'язаний до контрагента
    > решта. Підтягуємо суму/рахунок/дату для модалки погашення.
    """
    from .serializers import serialize_transaction
    qs = (Transaction.objects
          .filter(company=company, type=ttype, status=Transaction.STATUS_ACTUAL)
          .filter(settlements__isnull=True)
          .select_related('account', 'account__counterparty', 'counterparty')
          .order_by('-date_actual'))
    # Retrieve known recipients before applying the recent-history window.
    # Otherwise a busy account can hide an older matching payment entirely.
    known = list(qs.filter(counterparty=counterparty)[:limit]) if counterparty else []
    rows = {t.pk: t for t in [*known, *qs[:200]]}
    index = cards_service.recipient_index(company) if counterparty else None
    out = []
    for t in rows.values():
        prio = 0
        if counterparty is not None:
            if t.counterparty_id == counterparty.id:
                prio = 4
            elif not t.counterparty_id and cards_service.match_counterparty_by_payment(company, t, index=index)['strong'] == counterparty:
                prio = 3
            elif t.account_id and t.account.counterparty_id == counterparty.id:
                prio = 2
        data = serialize_transaction(t)
        data['_priority'] = prio
        out.append(data)
    out.sort(key=lambda d: (d['_priority'], d['date_actual'] or ''), reverse=True)
    return out[:limit]


def reverse_link_candidates(company, payment_txn) -> dict:
    """Для свіжого фактичного платежу — відкриті зобов'язання того ж контрагента.

    Повертає {'obligations': [...], 'counterparty_match': {...}}. Контрагента
    шукаємо за прямим counterparty платежу та за карткою (IBAN/маска/last4).
    """
    from . import obligations as obligations_service
    data = obligations_service.planned_obligations(company)
    side = (data['expense'] if payment_txn.type == Transaction.TYPE_EXPENSE
            else data['income'])

    match = cards_service.match_counterparty_by_payment(company, payment_txn)
    cp_ids = set()
    if payment_txn.counterparty_id:
        cp_ids.add(payment_txn.counterparty_id)
    if match['strong'] is not None:
        cp_ids.add(match['strong'].id)
    for c in match['candidates']:
        cp_ids.add(c.id)

    obligations = [g for g in side if g.get('counterparty_id') in cp_ids]
    return {'obligations': obligations, 'counterparty_match': match}


@db_transaction.atomic
def attach_payment_to_obligation(*, user, payment_txn, planned_txn,
                                 full_period=True, remember_card=False,
                                 card_hint=None) -> dict:
    """Обернений потік: прив'язати наявний платіж до зобов'язання (= pick_txn)."""
    return settle_obligation(
        user=user, planned_txn=planned_txn, mode='pick_txn',
        payment_txn=payment_txn, amount=payment_txn.amount,
        counterparty=planned_txn.counterparty, full_period=full_period,
        remember_card=remember_card, card_hint=card_hint)


def _split(total: Decimal, n: int) -> list:
    """Ділить суму на n частин по копійці (остача — в останню), сума зберігається."""
    total = Decimal(str(total))
    if n <= 1:
        return [total]
    base = (total / n).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    parts = [base] * (n - 1)
    parts.append(total - base * (n - 1))
    return parts


def _next_occurrences(rule, planned_txn, periods):
    """Найближчі `periods` планових екземплярів правила від дати planned_txn."""
    from_date = (planned_txn.date_actual.date() if planned_txn.date_actual
                 else timezone.localdate())
    occ = list(rule.transactions
               .filter(status=Transaction.STATUS_PLANNED,
                       date_actual__gte=day_start(from_date))
               .order_by('date_actual')[:periods])
    if planned_txn not in occ:
        occ = [planned_txn] + [o for o in occ if o.id != planned_txn.id]
        occ = occ[:periods]
    return occ


@db_transaction.atomic
def _settle_multi(*, user, planned_txn, rule, mode, amount, payment_txn, account,
                  date, counterparty, periods, remember_card, card_hint) -> dict:
    """Оплата зобов'язання за кілька періодів наперед (мультимісяць)."""
    company = planned_txn.company
    ttype = planned_txn.type
    counterparty = counterparty or planned_txn.counterparty
    when = date or timezone.now()

    occ = _next_occurrences(rule, planned_txn, periods)
    n = len(occ)
    planned_total = sum((o.amount or Decimal('0')) for o in occ)

    if mode == 'new_payment':
        total = Decimal(str(amount)) if amount not in (None, '') else planned_total
        if total <= 0:
            raise ValueError('Сума має бути більшою за 0')
        if account is None:
            raise ValueError('Оберіть рахунок списання/зарахування')
        payment = txn_service.create_transaction(
            user=user, type=ttype, status=Transaction.STATUS_ACTUAL,
            amount=total, account=account,
            currency=account.currency if account else company.base_currency,
            counterparty=counterparty, date_actual=when,
            comment=(planned_txn.comment or rule.title or ''),
            source='manual', is_business=planned_txn.is_business,
            recurrence_rule=rule)
    elif mode == 'pick_txn':
        if payment_txn is None or payment_txn.status != Transaction.STATUS_ACTUAL:
            raise ValueError('Оберіть фактичний платіж')
        if payment_txn.type != ttype:
            raise ValueError('Тип платежу не збігається із зобов\'язанням')
        total = Decimal(str(amount)) if amount not in (None, '') else payment_txn.amount
        payment = payment_txn
        upd = {}
        if counterparty and not payment.counterparty_id:
            upd['counterparty'] = counterparty
        if not payment.recurrence_rule_id:
            upd['recurrence_rule'] = rule
        if upd:
            txn_service.update_transaction(payment, user=user, **upd)
    else:
        raise ValueError(f'Невідомий режим: {mode}')

    amounts = _split(total, n)
    last_date = occ[0].date_actual.date() if occ[0].date_actual else timezone.localdate()
    settlements = []
    for o, amt in zip(occ, amounts):
        pk, pl = _period_for(o)
        settlements.append(ObligationSettlement.objects.create(
            company=company, payment=payment, rule=rule, planned_txn=None,
            period_key=pk, period_label=pl, amount=amt, currency=payment.currency,
            created_by=user if getattr(user, 'is_authenticated', False) else None))
        if o.date_actual:
            last_date = o.date_actual.date()
        txn_service.update_transaction(o, user=user, status=Transaction.STATUS_CANCELLED)

    _advance_rule(rule, user=user, from_date=last_date)

    if remember_card and counterparty and card_hint:
        cards_service.upsert_card(
            counterparty, when=when,
            pan_mask=card_hint.get('pan_mask', ''), last4=card_hint.get('last4', ''),
            bank=card_hint.get('bank', ''), iban=card_hint.get('iban', ''))

    return {'payment': payment, 'settlement': settlements[0], 'settlements': settlements,
            'full_period': True, 'periods': n}


@db_transaction.atomic
def skip_occurrence(*, user, planned_txn, mode='move_end') -> dict:
    """Пропустити поточний місяць зобов'язання (без втрати боргу).

    Скасовує поточний плановий екземпляр і просуває правило — нагадування
    переходить на наступний місяць. Для правил з фіксованою кількістю (count)
    скасований екземпляр НЕ зараховується, тож materialize автоматично додає
    один платіж у хвіст (борг зберігається, лише переноситься). Фіксуємо подію
    в журналі дій. ``mode``='drop' (лише для count) — зменшує загальну кількість
    (борг за цей період анулюється).
    """
    rule = planned_txn.recurrence_rule
    when = (planned_txn.date_actual.date() if planned_txn.date_actual
            else timezone.localdate())
    period_label = _period_for(planned_txn)[1]

    if (mode == 'drop' and rule is not None
            and rule.end_mode == RecurrenceRule.END_COUNT and rule.count):
        rule.count = max(0, rule.count - 1)
        rule.save(update_fields=['count'])

    txn_service.update_transaction(planned_txn, user=user,
                                   status=Transaction.STATUS_CANCELLED)
    if rule is not None:
        _advance_rule(rule, user=user, from_date=when)
        audit_service.log_action(
            user, 'update', 'recurrence', rule.id,
            summary=(f'Пропущено період {period_label} '
                     f'({"анульовано" if mode == "drop" else "перенесено"})'),
            company=planned_txn.company)
    return {'skipped_period': period_label, 'mode': mode}
