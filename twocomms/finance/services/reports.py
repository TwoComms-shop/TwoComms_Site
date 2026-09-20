"""Аналітичні звіти: будуються з тих самих транзакцій (ТЗ 06).

Cash Flow — за датою руху грошей (date_actual), фактичні, перекази виключені.
P&L — за датою угоди (date_agreement, фолбек date_actual), фактичні, без переказів.
Дебіторка/кредиторка — планові операції + неоплачені інвойси.
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import Coalesce

from ..models import Invoice, Transaction
from . import filters as filter_service
from .timeutil import day_end, day_start

# Назви місяців українською (називний відмінок) для підписів у звітах.
_UK_MONTHS_NOM = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                  'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень']


def _uk_month_year(d) -> str:
    """Назва місяця українською + рік (напр. «Червень 2026») для UI прогнозу."""
    return f'{_UK_MONTHS_NOM[d.month]} {d.year}'


def resolve_period(params):
    period = params.get('period', 'month')
    start, end = filter_service.period_range(period, params.get('date_from'), params.get('date_to'))
    if start is None:
        # 'all' → з найранішої операції до сьогодні.
        from django.utils import timezone
        start = dt.date(2000, 1, 1)
        end = timezone.localdate()
    return start, end


def _apply_dim_filters(qs, params):
    if params.get('accounts'):
        ids = [i for i in str(params['accounts']).split(',') if i.isdigit()]
        if ids:
            qs = qs.filter(account_id__in=ids)
    if params.get('projects'):
        ids = [i for i in str(params['projects']).split(',') if i.isdigit()]
        if ids:
            qs = qs.filter(project_id__in=ids)
    if params.get('categories'):
        ids = [i for i in str(params['categories']).split(',') if i.isdigit()]
        if ids:
            qs = qs.filter(category_id__in=ids)
    if params.get('counterparties'):
        ids = [i for i in str(params['counterparties']).split(',') if i.isdigit()]
        if ids:
            qs = qs.filter(counterparty_id__in=ids)
    if params.get('economic_kind'):
        kinds = [i.strip() for i in str(params['economic_kind']).split(',') if i.strip()]
        if kinds:
            qs = qs.filter(economic_kind__in=kinds)
    if params.get('funding_source'):
        ids = [i for i in str(params['funding_source']).split(',') if i.isdigit()]
        if ids:
            qs = qs.filter(funding_source_id__in=ids)
    # Бізнес / особисте — наскрізний зріз для всіх звітів.
    scope = (params.get('scope') or '').strip()
    if scope == 'business':
        qs = qs.filter(is_business=True)
    elif scope == 'personal':
        qs = qs.filter(is_business=False)
    return qs


def _actual(company):
    return (Transaction.objects.filter(company=company, status=Transaction.STATUS_ACTUAL)
            .exclude(excluded_from_reports=True))


# ----------------------------- Cash Flow -----------------------------

def cash_flow(company, params):
    start, end = resolve_period(params)
    qs = _apply_dim_filters(_actual(company), params).filter(
        date_actual__gte=day_start(start), date_actual__lte=day_end(end))

    # A confirmed classification changes the management view without deleting
    # or rewriting the bank movement.  Unknown legacy rows remain visible.
    excluded_management = ['internal_transfer', 'owner_draw', 'personal_transfer',
                            'debt_repayment', 'grant_inflow', 'adjustment']
    management_qs = qs.exclude(economic_kind__in=excluded_management)
    cash_in = management_qs.filter(type=Transaction.TYPE_INCOME).exclude(
        economic_kind='expense_refund').aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
    cash_out = management_qs.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
    targeted_in = qs.filter(economic_kind='grant_inflow', type=Transaction.TYPE_INCOME).aggregate(
        s=Sum('amount_base'))['s'] or Decimal('0')
    internal_in = qs.filter(economic_kind__in=['internal_transfer', 'personal_transfer'],
                            type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
    internal_out = qs.filter(economic_kind__in=['internal_transfer', 'owner_draw', 'personal_transfer'],
                             type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
    refunds = qs.filter(economic_kind='expense_refund', type=Transaction.TYPE_INCOME).aggregate(
        s=Sum('amount_base'))['s'] or Decimal('0')

    # Серія по днях/місяцях.
    # Гранти залишаються у фактичному русі рахунку, але не спотворюють
    # операційний графік: окремий targeted_in показує цільове фінансування.
    by_period = _series_by_period(
        management_qs.exclude(type=Transaction.TYPE_TRANSFER), start, end,
    )
    in_by_cat = _group_by_category(management_qs.filter(type=Transaction.TYPE_INCOME).exclude(economic_kind='expense_refund'))
    out_by_cat = _group_by_category(management_qs.filter(type=Transaction.TYPE_EXPENSE))

    return {
        'cash_in': cash_in,
        'cash_out': cash_out,
        'net': cash_in - cash_out,
        'series': by_period,
        'income_by_category': in_by_cat,
        'expense_by_category': out_by_cat,
        'targeted_in': targeted_in,
        'internal_in': internal_in,
        'internal_out': internal_out,
        'expense_refunds': refunds,
        'period': (start.isoformat(), end.isoformat()),
    }


def _series_by_period(qs, start, end):
    """Групування сум по днях (якщо період <= 62 днів) або місяцях."""
    span = (end - start).days
    buckets = OrderedDict()
    use_month = span > 62
    for t in qs.only('date_actual', 'type', 'amount_base'):
        d = t.date_actual.date()
        key = f'{d.year}-{d.month:02d}' if use_month else d.isoformat()
        if key not in buckets:
            buckets[key] = {'in': Decimal('0'), 'out': Decimal('0')}
        if t.type == Transaction.TYPE_INCOME:
            buckets[key]['in'] += t.amount_base
        elif t.type == Transaction.TYPE_EXPENSE:
            buckets[key]['out'] += t.amount_base
    return [{'label': k, 'in': float(v['in']), 'out': float(v['out'])}
            for k, v in sorted(buckets.items())]


def _group_by_category(qs):
    rows = (qs.values('category__name')
            .annotate(total=Coalesce(Sum('amount_base'), Decimal('0')))
            .order_by('-total'))
    return [{'name': r['category__name'] or 'Без категорії', 'total': float(r['total'])}
            for r in rows if r['total']]


# ----------------------------- P&L -----------------------------

def pnl(company, params):
    start, end = resolve_period(params)
    # За датою угоди; фолбек на date_actual робимо через Coalesce у фільтрі.
    qs = _apply_dim_filters(_actual(company), params).exclude(type=Transaction.TYPE_TRANSFER)
    qs = qs.filter(
        Q_or_date(start, end)
    )
    excluded_management = ['internal_transfer', 'owner_draw', 'personal_transfer',
                           'debt_repayment', 'grant_inflow', 'adjustment', 'expense_refund']
    management_qs = qs.exclude(economic_kind__in=excluded_management)
    income = management_qs.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
    expenses = management_qs.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
    refunds = qs.filter(type=Transaction.TYPE_INCOME, economic_kind='expense_refund').aggregate(
        s=Sum('amount_base'))['s'] or Decimal('0')
    expenses = max(Decimal('0'), expenses - refunds)
    return {
        'income': income,
        'expenses': expenses,
        'profit': income - expenses,
        'margin': (float((income - expenses) / income * 100) if income else 0.0),
        'income_by_category': _group_by_category(management_qs.filter(type=Transaction.TYPE_INCOME)),
        'expense_by_category': _group_by_category(management_qs.filter(type=Transaction.TYPE_EXPENSE)),
        'expense_refunds': refunds,
        'unclassified_count': qs.filter(economic_kind='unknown').count(),
        'unclassified_amount': qs.filter(economic_kind='unknown').aggregate(s=Sum('amount_base'))['s'] or Decimal('0'),
        'series': _series_by_period(qs, start, end),
        'period': (start.isoformat(), end.isoformat()),
    }


def Q_or_date(start, end):
    """P&L діапазон: date_agreement у межах, або (немає угоди) date_actual у межах."""
    from django.db.models import Q
    return (
        (Q(date_agreement__gte=day_start(start)) & Q(date_agreement__lte=day_end(end)))
        | (Q(date_agreement__isnull=True) & Q(date_actual__gte=day_start(start)) & Q(date_actual__lte=day_end(end)))
    )


# ----------------------------- Виписка за рахунком -----------------------------

def account_statement(company, account, params):
    from .calendar import opening_total  # повторне використання логіки залишку
    start, end = resolve_period(params)
    qs = (_actual(company).filter(date_actual__gte=day_start(start), date_actual__lte=day_end(end))
          .filter(account=account).select_related('category', 'counterparty', 'project')
          .order_by('date_actual'))
    # Початковий залишок рахунку: initial + рух до start (тільки цей рахунок).
    prior = _actual(company).filter(account=account, date_actual__lt=day_start(start))
    prior_in = prior.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    prior_out = prior.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    prior_tin = (_actual(company).filter(to_account=account, date_actual__lt=day_start(start))
                 .aggregate(s=Sum('to_amount'))['s'] or Decimal('0'))
    prior_tout = prior.filter(type=Transaction.TYPE_TRANSFER).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    opening = account.initial_balance + prior_in - prior_out + prior_tin - prior_tout

    period_in = qs.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    period_out = qs.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    return {
        'account': account,
        'opening': opening,
        'income': period_in,
        'expense': period_out,
        'closing': opening + period_in - period_out,
        'transactions': qs,
        'period': (start.isoformat(), end.isoformat()),
    }


# ----------------------------- Проекти -----------------------------

def projects_report(company, params):
    start, end = resolve_period(params)
    qs = _actual(company).exclude(type=Transaction.TYPE_TRANSFER).filter(
        date_actual__gte=day_start(start), date_actual__lte=day_end(end))
    total_income = qs.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')

    rows = []
    projects = list(company.projects.all()) + [None]
    for proj in projects:
        pqs = qs.filter(project=proj) if proj else qs.filter(project__isnull=True)
        income = pqs.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
        expense = pqs.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')
        if income == 0 and expense == 0:
            continue
        profit = income - expense
        rows.append({
            'name': proj.name if proj else 'Без проекта',
            'income': income, 'expense': expense, 'profit': profit,
            'margin': float(profit / income * 100) if income else 0.0,
            'share': float(income / total_income * 100) if total_income else 0.0,
        })

    # Сортуємо за прибутком (найприбутковіші зверху).
    rows.sort(key=lambda r: r['profit'], reverse=True)
    return {
        'rows': rows,
        'total_income': total_income,
        'period': (start.isoformat(), end.isoformat()),
    }


# ----------------------------- Owner's Drawings -----------------------------

def owner_drawings_report(company, params):
    """Звіт по виводах власника (Owner's Drawings) — розподіл прибутку.

    Owner's Drawings — це перекази між власними рахунками (ФОП → особиста картка),
    які не є витратою бізнесу, а розподілом прибутку. Виключаються з P&L.
    """
    start, end = resolve_period(params)

    # Знаходимо системну категорію "Вивід на особисте"
    owner_cat = company.categories.filter(
        is_system=True, name='Вивід на особисте'
    ).first()

    # Всі перекази з цією категорією
    qs = (_actual(company).filter(
            type=Transaction.TYPE_TRANSFER,
            date_actual__gte=day_start(start),
            date_actual__lte=day_end(end))
          .select_related('account', 'to_account'))

    if owner_cat:
        qs = qs.filter(category=owner_cat)

    # Також включаємо перекази без категорії, але з is_business=False
    # (старі перекази до створення категорії)
    qs_legacy = (_actual(company).filter(
            type=Transaction.TYPE_TRANSFER,
            is_business=False,
            category__isnull=True,
            date_actual__gte=day_start(start),
            date_actual__lte=day_end(end))
          .select_related('account', 'to_account'))

    # Об'єднуємо
    all_transfers = list(qs) + list(qs_legacy)

    # Рахуємо загальну суму виведень
    total_withdrawn = sum((t.amount for t in all_transfers), Decimal('0'))

    # Групуємо по місяцях для тренду
    by_month = {}
    for t in all_transfers:
        month_key = t.date_actual.strftime('%Y-%m')
        if month_key not in by_month:
            by_month[month_key] = Decimal('0')
        by_month[month_key] += t.amount

    # Рахуємо бізнес-прибуток за той самий період для порівняння
    business_qs = (_actual(company).filter(
            is_business=True,
            date_actual__gte=day_start(start),
            date_actual__lte=day_end(end))
          .exclude(type=Transaction.TYPE_TRANSFER))

    business_income = business_qs.filter(
        type=Transaction.TYPE_INCOME
    ).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')

    business_expense = business_qs.filter(
        type=Transaction.TYPE_EXPENSE
    ).aggregate(s=Sum('amount_base'))['s'] or Decimal('0')

    business_profit = business_income - business_expense

    # Відсоток виведення від прибутку
    withdrawal_percent = float(total_withdrawn / business_profit * 100) if business_profit > 0 else 0.0

    return {
        'total_withdrawn': total_withdrawn,
        'business_profit': business_profit,
        'withdrawal_percent': withdrawal_percent,
        'transfers': all_transfers,
        'by_month': sorted(by_month.items()),
        'period': (start.isoformat(), end.isoformat()),
    }


# ----------------------------- Personal Expenses -----------------------------

def personal_expenses_report(company, params):
    """Звіт по особистих витратах — куди йдуть гроші поза бізнесом.

    Показує детальну розбивку особистих витрат по категоріях,
    відсоток від загального доходу, тренд по місяцях.
    """
    start, end = resolve_period(params)

    # Тільки особисті витрати (is_business=False)
    qs = (_actual(company).filter(
            is_business=False,
            type=Transaction.TYPE_EXPENSE,
            date_actual__gte=day_start(start),
            date_actual__lte=day_end(end)))

    # Загальна сума особистих витрат
    total_personal = qs.aggregate(s=Sum('amount_base'))['s'] or Decimal('0')

    # Групування по категоріях
    by_category = (qs.values('category__name', 'category__icon', 'category__color')
                   .annotate(total=Sum('amount_base'))
                   .order_by('-total'))

    categories = []
    for row in by_category:
        amount = row['total'] or Decimal('0')
        pct = float(amount / total_personal * 100) if total_personal else 0.0
        categories.append({
            'name': row['category__name'] or 'Без категорії',
            'icon': row['category__icon'] or '💳',
            'color': row['category__color'] or '#6b7280',
            'amount': amount,
            'pct': pct,
        })

    # Тренд по місяцях
    by_month = {}
    for t in qs.values('date_actual', 'amount_base'):
        month_key = t['date_actual'].strftime('%Y-%m')
        if month_key not in by_month:
            by_month[month_key] = Decimal('0')
        by_month[month_key] += t['amount_base']

    # Загальний дохід за період (для розрахунку відсотка)
    all_income = (_actual(company).filter(
            type=Transaction.TYPE_INCOME,
            date_actual__gte=day_start(start),
            date_actual__lte=day_end(end))
          .aggregate(s=Sum('amount_base'))['s'] or Decimal('0'))

    # Відсоток особистих витрат від доходу
    personal_percent = float(total_personal / all_income * 100) if all_income > 0 else 0.0

    # Бізнес-витрати для порівняння
    business_expenses = (_actual(company).filter(
            is_business=True,
            type=Transaction.TYPE_EXPENSE,
            date_actual__gte=day_start(start),
            date_actual__lte=day_end(end))
          .aggregate(s=Sum('amount_base'))['s'] or Decimal('0'))

    return {
        'total_personal': total_personal,
        'total_income': all_income,
        'business_expenses': business_expenses,
        'personal_percent': personal_percent,
        'categories': categories,
        'by_month': sorted(by_month.items()),
        'period': (start.isoformat(), end.isoformat()),
    }



# ----------------------------- Balance Forecast -----------------------------

def balance_forecast_report(company, months: int = 6):
    """Прогноз балансу на N місяців вперед з урахуванням recurring платежів.

    Показує:
    - Поточний баланс
    - Заплановані надходження по місяцях
    - Заплановані витрати по місяцях (включно з recurring)
    - Прогнозований баланс на кінець кожного місяця
    - Попередження якщо баланс може стати негативним
    """
    import calendar
    from django.utils import timezone
    from . import balances as balance_service

    today = timezone.localdate()
    current_balance = balance_service.total_actual_balance(company)

    forecast = []
    running_balance = current_balance

    for month_offset in range(months):
        # Розраховуємо діапазон місяця
        # Include the complete current calendar month.  Starting at ``today``
        # silently dropped planned items earlier in the same month.
        month_start = dt.date(today.year, today.month, 1) + dt.timedelta(days=32 * month_offset)
        month_start = month_start.replace(day=1)

        # The first forecast bucket is a rolling month from today.  This keeps
        # a payment due in the next few weeks visible even late in a calendar
        # month; following buckets use calendar months.
        if month_offset == 0:
            month_end = today + dt.timedelta(days=31)
        else:
            last_day = calendar.monthrange(month_start.year, month_start.month)[1]
            month_end = dt.date(month_start.year, month_start.month, last_day)

        # Планові операції за цей місяць
        planned_qs = Transaction.objects.filter(
            company=company,
            status=Transaction.STATUS_PLANNED,
            date_actual__gte=day_start(month_start),
            date_actual__lte=day_end(month_end),
        )

        planned_income = planned_qs.filter(
            type=Transaction.TYPE_INCOME
        ).aggregate(s=Sum("amount_base"))["s"] or Decimal("0")

        planned_expense = planned_qs.filter(
            type=Transaction.TYPE_EXPENSE
        ).aggregate(s=Sum("amount_base"))["s"] or Decimal("0")

        # Прогнозований баланс на кінець місяця
        month_balance = running_balance + planned_income - planned_expense

        forecast.append({
            "month": month_start.strftime("%Y-%m"),
            "month_name": _uk_month_year(month_start),
            "planned_income": planned_income,
            "planned_expense": planned_expense,
            "net_change": planned_income - planned_expense,
            "ending_balance": month_balance,
            "is_negative": month_balance < 0,
            "is_warning": month_balance < Decimal("10000"),  # Попередження якщо < 10k
        })

        running_balance = month_balance

    return {
        "current_balance": current_balance,
        "forecast": forecast,
        "final_balance": running_balance,
        "total_planned_income": sum((m["planned_income"] for m in forecast), Decimal("0")),
        "total_planned_expense": sum((m["planned_expense"] for m in forecast), Decimal("0")),
    }
