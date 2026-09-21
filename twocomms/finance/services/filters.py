"""Фільтрація та вибірка операцій для журналу платежів (ТЗ §3)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from ..models import Transaction
from .timeutil import day_end, day_start


def period_range(period: str, date_from=None, date_to=None):
    """Повертає (start_date, end_date) за назвою періоду.

    Підтримує: all, today, yesterday, week, month, last_month, quarter, year, custom.
    """
    today = timezone.localdate()
    if period == 'today':
        return today, today
    if period == 'yesterday':
        y = today - dt.timedelta(days=1)
        return y, y
    if period == 'week':
        start = today - dt.timedelta(days=today.weekday())
        return start, today
    if period == 'month':
        return today.replace(day=1), today
    if period == 'last_month':
        first_this = today.replace(day=1)
        last_prev = first_this - dt.timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if period == 'quarter':
        q_start_month = 3 * ((today.month - 1) // 3) + 1
        return today.replace(month=q_start_month, day=1), today
    if period == 'year':
        return today.replace(month=1, day=1), today
    if period == 'custom':
        return (_parse_date(date_from), _parse_date(date_to))
    return None, None  # all


def _parse_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_decimal(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value).replace(' ', '').replace(',', '.'))
    except (InvalidOperation, ValueError):
        return None


def _csv_ids(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = str(value).split(',')
    out = []
    for v in raw:
        v = str(v).strip()
        if v.isdigit():
            out.append(int(v))
    return out


def _scope_q(scope):
    """Prefer a confirmed ledger scope and fall back to legacy row metadata."""
    if scope == 'business':
        return Q(ownership_scope='business') | Q(ownership_scope='unknown', is_business=True)
    if scope == 'personal':
        return Q(ownership_scope='personal') | Q(ownership_scope='unknown', is_business=False)
    return Q()


def filter_transactions(company, params, *, include_planned=True):
    """Будує queryset операцій за параметрами запиту (GET/dict).

    Не включає дочірні частини split? — навпаки: показуємо дітей,
    приховуємо батьків, що розділені (excluded_from_reports).
    """
    qs = (Transaction.objects.filter(company=company)
          .select_related('account', 'to_account', 'category', 'counterparty', 'project')
          .prefetch_related('tags'))

    # Період (за датою грошей).
    period = params.get('period', 'all')
    start, end = period_range(period, params.get('date_from'), params.get('date_to'))
    if start:
        qs = qs.filter(date_actual__gte=day_start(start))
    if end:
        qs = qs.filter(date_actual__lte=day_end(end))

    # Пошук.
    search = (params.get('search') or params.get('q') or '').strip()
    if search:
        qs = qs.filter(
            Q(comment__icontains=search)
            | Q(account__name__icontains=search)
            | Q(to_account__name__icontains=search)
            | Q(counterparty__name__icontains=search)
            | Q(category__name__icontains=search)
            | Q(project__name__icontains=search)
            | Q(tags__name__icontains=search)
            | Q(external_id__icontains=search)
            | Q(linked_invoice__number__icontains=search)
        ).distinct()

    # Сума (за абсолютним значенням).
    amount_min = _parse_decimal(params.get('amount_min'))
    amount_max = _parse_decimal(params.get('amount_max'))
    if amount_min is not None:
        qs = qs.filter(amount__gte=amount_min)
    if amount_max is not None:
        qs = qs.filter(amount__lte=amount_max)

    # Розширені фільтри (мультивибір через CSV id).
    acc_ids = _csv_ids(params.get('accounts'))
    if acc_ids:
        qs = qs.filter(Q(account_id__in=acc_ids) | Q(to_account_id__in=acc_ids))
    cat_ids = _csv_ids(params.get('categories'))
    if cat_ids:
        qs = qs.filter(category_id__in=cat_ids)
    proj_ids = _csv_ids(params.get('projects'))
    if proj_ids:
        qs = qs.filter(project_id__in=proj_ids)
    cp_ids = _csv_ids(params.get('counterparties'))
    if cp_ids:
        qs = qs.filter(counterparty_id__in=cp_ids)
    tag_ids = _csv_ids(params.get('tags'))
    if tag_ids:
        qs = qs.filter(tags__id__in=tag_ids).distinct()

    types = [t for t in (params.get('types') or '').split(',') if t]
    if types:
        qs = qs.filter(type__in=types)

    statuses = [s for s in (params.get('statuses') or '').split(',') if s]
    if statuses:
        qs = qs.filter(status__in=statuses)
    else:
        # За замовчуванням журнал НЕ показує скасовані операції (напр. планові
        # екземпляри, погашені реальним платежем через привʼязку). Вони лишаються
        # у БД для аудиту, але не засмічують журнал і не впливають на баланс.
        qs = qs.exclude(status=Transaction.STATUS_CANCELLED)
        if not include_planned:
            qs = qs.exclude(status=Transaction.STATUS_PLANNED)

    # Бізнес / особисте.
    scope = (params.get('scope') or '').strip()
    if scope in {'business', 'personal'}:
        qs = qs.filter(_scope_q(scope))

    # Фільтр за групою MCC (продукти, паливо, кафе...). Резолвимо у діапазони MCC.
    mcc_group = (params.get('mcc_group') or '').strip()
    if mcc_group:
        from . import mcc as mcc_mod
        ranges = [(lo, hi) for (lo, hi), key in mcc_mod._RANGES if key == mcc_group]
        if ranges:
            q = Q()
            for lo, hi in ranges:
                q |= Q(mcc__gte=lo, mcc__lte=hi)
            qs = qs.filter(q)

    # Приховати оригінали розділених платежів (показуємо дітей).
    qs = qs.filter(excluded_from_reports=False)

    return qs


def split_actual_planned(qs):
    """Розділяє queryset на фактичні та планові для журналу.

    Фактичні — лише ACTUAL (скасовані/чернетки не показуємо в журналі).
    """
    actual = qs.filter(status=Transaction.STATUS_ACTUAL)
    planned = qs.filter(status=Transaction.STATUS_PLANNED)
    return actual, planned
