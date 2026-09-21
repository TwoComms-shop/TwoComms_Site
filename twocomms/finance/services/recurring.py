"""Сервіс повторюваних платежів (recurring) — БЛОК 3 + керування зобов'язаннями.

Архітектурний принцип (без дублювання логіки): повторюваний платіж — це
``RecurrenceRule`` + матеріалізовані планові ``Transaction`` (status=planned,
source='recurring', recurrence_rule=...). Планові транзакції автоматично
потрапляють у прогноз, календар і дебіторку/кредиторку. У списках платежів вони
**групуються в один рядок** за правилом (див. ``services.obligations``), тож
бізнес бачить одне зобов'язання («Комуналка, щомісяця, лишилось безстроково»),
а не копію за кожен місяць.

Життєвий цикл:
  create_rule_from_transaction → materialize (наперед на горизонт) →
  settle_occurrence (планова → фактична з рахунком/контрагентом) →
  materialize (підтягнути наступну) … або update_plan (змінили суму/назву).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from ..models import RecurrenceRule, Transaction, get_default_company
from . import transactions as txn_service
from .timeutil import day_end, day_start

# Наскільки наперед матеріалізуємо планові для безстрокових/довгих правил.
DEFAULT_HORIZON_DAYS = 120


def calculate_next_occurrence(rule: RecurrenceRule, from_date: dt.date = None) -> dt.date | None:
    """Розраховує наступну дату спрацювання правила після ``from_date``.

    Повертає None, якщо правило завершене (вийшло за end_date або вичерпало
    задану кількість повторень count).
    """
    if from_date is None:
        from_date = timezone.localdate()

    # Межа за датою.
    if rule.end_date and from_date > rule.end_date:
        return None

    # Межа за кількістю: рахуємо всі НЕ скасовані транзакції правила (планові +
    # вже проведені), бо count означає «повторити рівно N разів за весь час».
    if rule.count:
        generated_count = (rule.transactions
                           .exclude(status=Transaction.STATUS_CANCELLED)
                           .count())
        if generated_count >= rule.count:
            return None

    if rule.frequency == 'daily':
        next_date = from_date + dt.timedelta(days=rule.interval)
    elif rule.frequency == 'weekly':
        next_date = from_date + dt.timedelta(weeks=rule.interval)
    elif rule.frequency == 'monthly':
        month = from_date.month + rule.interval
        year = from_date.year
        while month > 12:
            month -= 12
            year += 1
        if rule.by_month_day:
            day = min(rule.by_month_day, 28)
        else:
            day = from_date.day
        try:
            next_date = dt.date(year, month, day)
        except ValueError:
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            next_date = dt.date(year, month, last_day)
    elif rule.frequency == 'yearly':
        try:
            next_date = dt.date(from_date.year + rule.interval, from_date.month, from_date.day)
        except ValueError:
            next_date = dt.date(from_date.year + rule.interval, from_date.month, 28)
    else:
        return None

    if rule.end_date and next_date > rule.end_date:
        return None
    return next_date


@db_transaction.atomic
def generate_planned_transaction(rule: RecurrenceRule, for_date: dt.date, *, user) -> Transaction | None:
    """Створює одну планову транзакцію за правилом на дату ``for_date``.

    Ідемпотентно: якщо на цю дату вже є планова транзакція правила — нічого не
    робить. Переносить усі поля шаблону (рахунок, категорія, контрагент, проєкт,
    валюта, бізнес-прапор, коментар/назва).
    """
    if not rule.is_active:
        return None

    existing = Transaction.objects.filter(
        company=rule.company,
        recurrence_rule=rule,
        date_actual__gte=day_start(for_date),
        date_actual__lte=day_end(for_date),
    ).exclude(status=Transaction.STATUS_CANCELLED).exists()
    if existing:
        return None

    currency = (rule.template_currency
                or (rule.template_account.currency if rule.template_account else 'UAH'))
    comment = rule.template_comment or rule.title or f'Повторюваний платіж: {rule}'

    txn = txn_service.create_transaction(
        user=user,
        type=rule.template_type,
        amount=rule.template_amount or Decimal('0'),
        account=rule.template_account,
        currency=currency,
        category=rule.template_category,
        counterparty=rule.template_counterparty,
        project=rule.template_project,
        comment=comment,
        date_actual=timezone.make_aware(dt.datetime.combine(for_date, dt.time(12, 0))),
        status=Transaction.STATUS_PLANNED,
        recurrence_rule=rule,
        source='recurring',
        is_business=rule.template_is_business,
        amount_is_estimated=rule.amount_is_estimated,
    )

    rule.last_generated_at = timezone.now()
    rule.save(update_fields=['last_generated_at'])
    return txn


def generate_upcoming_transactions(rule: RecurrenceRule, *, user, days_ahead: int = 60) -> list[Transaction]:
    """Матеріалізує планові транзакції правила на ``days_ahead`` днів уперед.

    Для правил з фіксованою кількістю (count) генерує доти, доки не вичерпано
    ліміт, навіть якщо це виходить за горизонт (щоб «скільки лишилось» було
    коректним і прогноз містив усі майбутні платежі).
    """
    if not rule.is_active or not rule.auto_create:
        return []

    today = timezone.localdate()
    horizon = today + dt.timedelta(days=days_ahead)
    created = []

    # Старт: перша незматеріалізована дата. Беремо max(next_occurrence, start),
    # але не раніше за start_date.
    current_date = rule.next_occurrence or rule.start_date
    guard = 0
    while current_date and guard < 1200:
        guard += 1
        within_horizon = current_date <= horizon
        # Для count-режиму ігноруємо горизонт — добиваємо до ліміту.
        if not within_horizon and rule.end_mode != RecurrenceRule.END_COUNT:
            break
        txn = generate_planned_transaction(rule, current_date, user=user)
        if txn:
            created.append(txn)
        nxt = calculate_next_occurrence(rule, current_date)
        if nxt is None:
            current_date = None
            break
        current_date = nxt

    if current_date:
        rule.next_occurrence = current_date
        rule.save(update_fields=['next_occurrence'])
    return created


def materialize(rule: RecurrenceRule, *, user, horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[Transaction]:
    """Зручна обгортка: підтягнути планові на горизонт (за замовчуванням 120 дн)."""
    return generate_upcoming_transactions(rule, user=user, days_ahead=horizon_days)


def process_all_recurring_rules(*, user, days_ahead: int = 60) -> dict:
    """Обробляє всі активні recurring-правила компанії (для cron-команди)."""
    company = get_default_company()
    rules = RecurrenceRule.objects.filter(company=company, is_active=True)
    stats = {'rules_processed': 0, 'transactions_created': 0}
    for rule in rules:
        created = generate_upcoming_transactions(rule, user=user, days_ahead=days_ahead)
        stats['rules_processed'] += 1
        stats['transactions_created'] += len(created)
    return stats


# ----------------------------- Життєвий цикл зобов'язання -----------------------------

_FREQ_VALID = {'daily', 'weekly', 'monthly', 'yearly'}


@db_transaction.atomic
def create_rule_from_transaction(txn: Transaction, *, user, frequency: str, interval: int = 1,
                                 end_mode: str = RecurrenceRule.END_NEVER,
                                 end_date: dt.date | None = None,
                                 count: int | None = None,
                                 title: str = '',
                                 amount_is_estimated: bool = False) -> RecurrenceRule:
    """Робить транзакцію першим екземпляром повторюваного зобов'язання.

    Створює ``RecurrenceRule`` з полів транзакції-шаблону, прив'язує саму
    транзакцію (recurrence_rule), матеріалізує наступні екземпляри наперед.
    """
    if frequency not in _FREQ_VALID:
        raise ValueError('Невідома періодичність')
    interval = max(1, int(interval or 1))

    start = txn.date_actual.date() if txn.date_actual else timezone.localdate()

    rule = RecurrenceRule.objects.create(
        company=txn.company,
        title=(title or txn.comment or '').strip()[:255],
        frequency=frequency,
        interval=interval,
        by_month_day=start.day if frequency == 'monthly' else None,
        start_date=start,
        end_mode=end_mode if end_mode in dict(RecurrenceRule.END_CHOICES) else RecurrenceRule.END_NEVER,
        end_date=end_date if end_mode == RecurrenceRule.END_UNTIL else None,
        count=int(count) if (end_mode == RecurrenceRule.END_COUNT and count) else None,
        template_amount=txn.amount,
        amount_is_estimated=bool(amount_is_estimated),
        template_account=txn.account,
        template_category=txn.category,
        template_counterparty=txn.counterparty,
        template_project=txn.project,
        template_currency=txn.currency or 'UAH',
        template_type=txn.type,
        template_comment=txn.comment or '',
        template_is_business=txn.is_business,
        is_active=True,
        auto_create=True,
    )

    # Прив'язуємо вихідну транзакцію як перший екземпляр (вона вже існує на start).
    txn.recurrence_rule = rule
    txn.is_recurring = True
    txn.source = txn.source or 'recurring'
    txn.amount_is_estimated = bool(amount_is_estimated)
    txn.save(update_fields=['recurrence_rule', 'is_recurring', 'source', 'amount_is_estimated'])

    # Наступна дата після першого екземпляра — звідси й матеріалізуємо.
    rule.next_occurrence = calculate_next_occurrence(rule, start)
    rule.save(update_fields=['next_occurrence'])
    materialize(rule, user=user)
    return rule


def occurrences_until(rule: RecurrenceRule, *, from_date: dt.date, to_date: dt.date) -> int:
    """Скільки спрацювань правила потрапляє в (from_date, to_date]."""
    count = 0
    cursor = calculate_next_occurrence(rule, from_date)
    guard = 0
    while cursor and cursor <= to_date and guard < 1200:
        count += 1
        cursor = calculate_next_occurrence(rule, cursor)
        guard += 1
    return count


def remaining(rule: RecurrenceRule) -> dict:
    """Скільки платежів і на яку суму ще лишилось за правилом.

    Повертає:
      mode        — 'never' | 'until' | 'count'
      done        — скільки вже проведено (фактичних) екземплярів
      left        — скільки ще лишилось (None = безстроково)
      left_amount — орієнтовна сума, що лишилась (None якщо безстроково)
      label       — людський підпис («безстроково», «лишилось 4 з 6», «до 31.12»)
    """
    done = (rule.transactions.filter(status=Transaction.STATUS_ACTUAL).count())
    amount = rule.template_amount or Decimal('0')

    if rule.end_mode == RecurrenceRule.END_COUNT and rule.count:
        left = max(0, rule.count - done)
        return {
            'mode': 'count', 'done': done, 'left': left,
            'left_amount': amount * left,
            'label': f'лишилось {left} з {rule.count}',
        }
    if rule.end_mode == RecurrenceRule.END_UNTIL and rule.end_date:
        today = timezone.localdate()
        # Рахуємо ще не проведені спрацювання від сьогодні до межі.
        left = occurrences_until(rule, from_date=today - dt.timedelta(days=1), to_date=rule.end_date)
        return {
            'mode': 'until', 'done': done, 'left': left,
            'left_amount': amount * left,
            'label': f'до {rule.end_date.strftime("%d.%m.%Y")}',
        }
    return {
        'mode': 'never', 'done': done, 'left': None,
        'left_amount': None, 'label': 'безстроково',
    }


def _reschedule(rule: RecurrenceRule, *, user) -> None:
    """Перебудовує майбутні планові екземпляри під новий графік правила.

    Викликається, коли змінилися періодичність/кратність/межа повторення. Щоб
    не лишалося «осиротілих» копій на старому графіку (напр. місячні після
    переходу на тижневі), видаляємо всі ще НЕ проведені планові від сьогодні,
    перераховуємо ``by_month_day`` та ``next_occurrence`` від якоря (остання
    проведена дата або старт) і матеріалізуємо наперед заново. Проведені
    (фактичні) екземпляри не чіпаємо — історія лишається правдивою.
    """
    today = timezone.localdate()

    # Прибрати ще не проведені планові від сьогодні (старий графік).
    for ftxn in rule.transactions.filter(
            status=Transaction.STATUS_PLANNED, date_actual__date__gte=today):
        txn_service.delete_transaction(ftxn, user=user)

    # Якір — остання проведена дата; якщо проведених немає, стартова дата.
    last_actual = (rule.transactions
                   .filter(status=Transaction.STATUS_ACTUAL)
                   .order_by('-date_actual').first())

    # Для місячних тримаємо день місяця синхронним зі стартом (якщо не задано).
    if rule.frequency == 'monthly' and not rule.by_month_day and rule.start_date:
        rule.by_month_day = rule.start_date.day
        rule.save(update_fields=['by_month_day'])

    if last_actual and last_actual.date_actual:
        rule.next_occurrence = calculate_next_occurrence(rule, last_actual.date_actual.date())
    else:
        # Ще нічого не проводили — генеруємо від стартової дати включно.
        rule.next_occurrence = rule.start_date
    rule.save(update_fields=['next_occurrence'])
    materialize(rule, user=user)


@db_transaction.atomic
def update_plan(rule: RecurrenceRule, *, user, amount=None, amount_is_estimated=None,
                title=None, category=None,
                counterparty=None, project=None, account=None,
                frequency=None, interval=None, end_mode=None, end_date=None, count=None,
                apply_to_future: bool = True) -> RecurrenceRule:
    """Редагує план зобов'язання (напр. «комуналка виросла» або «тепер раз на 2 міс»).

    Оновлює поля-шаблон правила. Якщо змінився графік (періодичність/кратність/
    межа повторення) — повністю перебудовує майбутні планові екземпляри під
    новий графік (див. ``_reschedule``), тож зміна реально застосовується, а не
    лишає старі копії. Якщо змінилися лише реквізити/сума — переносить їх на ще
    НЕ проведені екземпляри. Уже проведені фактичні транзакції не чіпаються.
    """
    # Знімок графіка ДО змін — щоб зрозуміти, чи треба перебудовувати екземпляри.
    old_schedule = (rule.frequency, rule.interval, rule.end_mode,
                    rule.end_date, rule.count)

    fields = []
    if amount is not None:
        rule.template_amount = Decimal(str(amount)); fields.append('template_amount')
    if amount_is_estimated is not None:
        rule.amount_is_estimated = bool(amount_is_estimated); fields.append('amount_is_estimated')
    if title is not None:
        rule.title = str(title)[:255]; fields.append('title')
    if category is not None:
        rule.template_category = category; fields.append('template_category')
    if counterparty is not None:
        rule.template_counterparty = counterparty; fields.append('template_counterparty')
    if project is not None:
        rule.template_project = project; fields.append('template_project')
    if account is not None:
        rule.template_account = account; fields.append('template_account')
    if frequency in _FREQ_VALID:
        # Зміна періодичності → день місяця наново від старту (для monthly).
        if frequency != rule.frequency:
            rule.by_month_day = (rule.start_date.day if frequency == 'monthly'
                                 and rule.start_date else None)
            fields.append('by_month_day')
        rule.frequency = frequency; fields.append('frequency')
    if interval is not None:
        rule.interval = max(1, int(interval)); fields.append('interval')
    if end_mode in dict(RecurrenceRule.END_CHOICES):
        rule.end_mode = end_mode; fields.append('end_mode')
        rule.end_date = end_date if end_mode == RecurrenceRule.END_UNTIL else None
        rule.count = int(count) if (end_mode == RecurrenceRule.END_COUNT and count) else None
        fields += ['end_date', 'count']
    if fields:
        rule.save(update_fields=list(set(fields)))

    schedule_changed = (rule.frequency, rule.interval, rule.end_mode,
                        rule.end_date, rule.count) != old_schedule

    if schedule_changed:
        # Перебудовуємо майбутні екземпляри під новий графік (з перенесенням
        # актуальних реквізитів/суми, бо template вже оновлено).
        _reschedule(rule, user=user)
        return rule

    if apply_to_future:
        today = timezone.localdate()
        future = rule.transactions.filter(
            status=Transaction.STATUS_PLANNED,
            date_actual__date__gte=today,
        )
        for ftxn in future:
            upd = {}
            if amount is not None:
                upd['amount'] = rule.template_amount
            if amount_is_estimated is not None:
                upd['amount_is_estimated'] = rule.amount_is_estimated
            if category is not None:
                upd['category'] = rule.template_category
            if counterparty is not None:
                upd['counterparty'] = rule.template_counterparty
            if project is not None:
                upd['project'] = rule.template_project
            if account is not None:
                upd['account'] = rule.template_account
            if upd:
                txn_service.update_transaction(ftxn, user=user, **upd)

    materialize(rule, user=user)
    return rule


@db_transaction.atomic
def settle_occurrence(txn: Transaction, *, user, account=None, counterparty=None,
                      date_actual=None, amount=None, link_account_cp: bool = False) -> Transaction:
    """Проводить планову транзакцію як фактичну з вибором рахунку та контрагента.

    Для доходу ``account`` — рахунок, КУДИ надійшли кошти; для витрати — рахунок,
    З ЯКОГО списано. ``counterparty`` фіксує, з ким була операція; за згодою
    рахунок прив'язується до контрагента (для авто-розпізнавання надалі та
    історії по контрагенту). ``amount`` — фактична сума (для орієнтовних платежів,
    де планували приблизно, а платимо точно); після уведення фактичної суми
    позначка «орієнтовна» знімається. Після проведення підтягуємо наступний
    екземпляр повторюваного правила, щоб зобов'язання лишалося «живим».
    """
    fields = {'status': Transaction.STATUS_ACTUAL,
              'date_actual': date_actual or timezone.now()}
    if amount is not None:
        fields['amount'] = Decimal(str(amount))
        fields['amount_is_estimated'] = False
    if account is not None:
        fields['account'] = account
    if counterparty is not None:
        fields['counterparty'] = counterparty
    txn_service.update_transaction(txn, user=user, **fields)

    # Stable rent/utility refunds are a known recurring business return. Apply
    # the rule only after the occurrence is actual and only for the strict
    # Viktor pattern; ambiguous incoming operations remain reviewable.
    if txn.type == Transaction.TYPE_INCOME:
        from . import ledger_v2
        ledger_v2.classify_known_rent_refund(txn, user=user)
    elif txn.type == Transaction.TYPE_EXPENSE:
        from . import ledger_v2
        ledger_v2.classify_known_rent_expense(txn, user=user)

    # Прив'язка рахунку до контрагента (історія платежів по контрагенту).
    if (link_account_cp and counterparty is not None and txn.account_id
            and not txn.account.counterparty_id):
        txn.account.counterparty = counterparty
        txn.account.save(update_fields=['counterparty'])

    # Підтягнути наступний екземпляр повторюваного правила.
    if txn.recurrence_rule_id and txn.recurrence_rule.is_active:
        rule = txn.recurrence_rule
        if rule.next_occurrence is None or rule.next_occurrence <= txn.date_actual.date():
            rule.next_occurrence = calculate_next_occurrence(rule, txn.date_actual.date())
            rule.save(update_fields=['next_occurrence'])
        materialize(rule, user=user)
    return txn


@db_transaction.atomic
def stop_rule(rule: RecurrenceRule, *, user, delete_future: bool = True) -> None:
    """Зупиняє повторення: деактивує правило та (опційно) прибирає майбутні планові."""
    rule.is_active = False
    rule.save(update_fields=['is_active'])
    if delete_future:
        today = timezone.localdate()
        for ftxn in rule.transactions.filter(
                status=Transaction.STATUS_PLANNED, date_actual__date__gte=today):
            txn_service.delete_transaction(ftxn, user=user)
