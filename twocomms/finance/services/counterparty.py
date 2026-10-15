"""Історія та аналітика по контрагентах.

Дозволяє бізнесу подивитися по конкретному контрагенту — що і коли з ним було,
через які рахунки проходили кошти, скільки ще заплановано (борг/зобов'язання),
яка статистика співпраці та які дії над ним виконувались (журнал аудиту).
Дані беруться з тих самих транзакцій (єдине джерело правди), плюс рахунки,
прив'язані до контрагента (``Account.counterparty``), магазини-реалізатори
(``Reseller.counterparty``) та журнал аудиту (``AuditLog``).
"""
from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from ..models import Account, AuditLog, Counterparty, Transaction
from . import serializers as ser


# --------------------------------------------------------------------------
# Список контрагентів зі зведеною статистикою (для сторінки «Контрагенти»).
# --------------------------------------------------------------------------

def counterparties_overview(company, *, search='', type_filter='', sort='turnover') -> dict:
    """Повертає список контрагентів з агрегатами для таблиці/карток.

    Для кожного контрагента рахуємо одним проходом по транзакціях:
      received  — фактично отримано (доходи від контрагента);
      paid      — фактично сплачено (витрати на контрагента);
      planned_in/out — заплановані (планові) суми;
      txn_count — кількість операцій;
      last_date — дата останньої операції.
    """
    qs = Counterparty.objects.filter(company=company)
    if search:
        from .cards import normalize_alias
        qs = qs.filter(Q(name__icontains=search) | Q(group__icontains=search)
                       | Q(edrpou__icontains=search)
                       | Q(aliases__normalized=normalize_alias(search), aliases__is_active=True)).distinct()
    if type_filter:
        qs = qs.filter(type=type_filter)

    # Агрегати по транзакціях одним запитом (групування по контрагенту).
    agg = {}
    txn_qs = (Transaction.objects
              .filter(company=company, counterparty__isnull=False)
              .exclude(excluded_from_reports=True)
              .values('counterparty_id', 'type', 'status')
              .annotate(total=Sum('amount_base'), cnt=Count('id')))
    for row in txn_qs:
        cid = row['counterparty_id']
        slot = agg.setdefault(cid, {
            'received': Decimal('0'), 'paid': Decimal('0'),
            'planned_in': Decimal('0'), 'planned_out': Decimal('0'),
            'txn_count': 0,
        })
        amount = row['total'] or Decimal('0')
        slot['txn_count'] += row['cnt']
        if row['status'] == Transaction.STATUS_ACTUAL:
            if row['type'] == Transaction.TYPE_INCOME:
                slot['received'] += amount
            elif row['type'] == Transaction.TYPE_EXPENSE:
                slot['paid'] += amount
        elif row['status'] == Transaction.STATUS_PLANNED:
            if row['type'] == Transaction.TYPE_INCOME:
                slot['planned_in'] += amount
            elif row['type'] == Transaction.TYPE_EXPENSE:
                slot['planned_out'] += amount

    # Дати останньої операції одним запитом.
    last_dates = {}
    for row in (Transaction.objects
                .filter(company=company, counterparty__isnull=False)
                .values('counterparty_id')
                .annotate(last=Max('date_actual'))):
        last_dates[row['counterparty_id']] = row['last']

    cur = company.base_currency
    rows = []
    totals = {'received': Decimal('0'), 'paid': Decimal('0'),
              'planned_in': Decimal('0'), 'planned_out': Decimal('0')}
    for cp in qs:
        a = agg.get(cp.id, {
            'received': Decimal('0'), 'paid': Decimal('0'),
            'planned_in': Decimal('0'), 'planned_out': Decimal('0'), 'txn_count': 0,
        })
        turnover = a['received'] + a['paid']
        net = a['received'] - a['paid']
        last_dt = last_dates.get(cp.id)
        totals['received'] += a['received']
        totals['paid'] += a['paid']
        totals['planned_in'] += a['planned_in']
        totals['planned_out'] += a['planned_out']
        rows.append({
            'id': cp.id,
            'name': cp.name,
            'type': cp.type,
            'type_display': cp.get_type_display(),
            'group': cp.group or '',
            'edrpou': cp.edrpou or '',
            'received': a['received'], 'received_d': ser.money(a['received'], cur),
            'paid': a['paid'], 'paid_d': ser.money(a['paid'], cur),
            'net': net, 'net_d': ser.money(net, cur, signed=True),
            'planned_in': a['planned_in'], 'planned_in_d': ser.money(a['planned_in'], cur),
            'planned_out': a['planned_out'], 'planned_out_d': ser.money(a['planned_out'], cur),
            'turnover': turnover, 'turnover_d': ser.money(turnover, cur),
            'txn_count': a['txn_count'],
            'last_date': ser.date_label(last_dt) if last_dt else '',
            'last_date_raw': last_dt.isoformat() if last_dt else '',
            'initials': _initials(cp.name),
            'color': _color_for(cp.name),
        })

    # Сортування.
    if sort == 'name':
        rows.sort(key=lambda r: r['name'].lower())
    elif sort == 'net':
        rows.sort(key=lambda r: r['net'], reverse=True)
    elif sort == 'recent':
        rows.sort(key=lambda r: r['last_date_raw'] or '', reverse=True)
    else:  # turnover (default)
        rows.sort(key=lambda r: r['turnover'], reverse=True)

    return {
        'rows': rows,
        'count': len(rows),
        'totals': {
            'received': ser.money(totals['received'], cur),
            'paid': ser.money(totals['paid'], cur),
            'planned_in': ser.money(totals['planned_in'], cur),
            'planned_out': ser.money(totals['planned_out'], cur),
            'net': ser.money(totals['received'] - totals['paid'], cur, signed=True),
        },
        'types': Counterparty.TYPE_CHOICES,
    }


def _initials(name: str) -> str:
    parts = [p for p in (name or '').split() if p]
    if not parts:
        return '—'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


_PALETTE = ['#34d399', '#6f95ff', '#fbbf24', '#f472b6', '#a78bfa', '#38bdf8',
            '#fb7185', '#4ade80', '#facc15', '#22d3ee']


def _color_for(name: str) -> str:
    if not name:
        return _PALETTE[0]
    return _PALETTE[sum(ord(c) for c in name) % len(_PALETTE)]


# --------------------------------------------------------------------------
# Детальна історія + статистика по одному контрагенту.
# --------------------------------------------------------------------------

def counterparty_history(company, counterparty: Counterparty, *, limit: int = 200) -> dict:
    """Зведена історія по контрагенту (для модалки/детальної сторінки)."""
    qs = (Transaction.objects
          .filter(company=company, counterparty=counterparty)
          .select_related('account', 'to_account', 'category', 'project',
                          'counterparty', 'recurrence_rule', 'reseller')
          .order_by('-date_actual', '-id'))

    received = Decimal('0')
    paid = Decimal('0')
    planned_in = Decimal('0')
    planned_out = Decimal('0')
    account_ids = set()

    rows = []
    for t in qs[:limit]:
        rows.append(ser.serialize_transaction(t))
        if t.account_id:
            account_ids.add(t.account_id)
        base = t.amount_base or t.amount or Decimal('0')
        if t.status == Transaction.STATUS_ACTUAL:
            if t.type == Transaction.TYPE_INCOME:
                received += base
            elif t.type == Transaction.TYPE_EXPENSE:
                paid += base
        elif t.status == Transaction.STATUS_PLANNED:
            if t.type == Transaction.TYPE_INCOME:
                planned_in += base
            elif t.type == Transaction.TYPE_EXPENSE:
                planned_out += base

    linked = Account.objects.filter(company=company, counterparty=counterparty)
    for a in linked:
        account_ids.add(a.id)

    accounts = []
    for a in Account.objects.filter(company=company, id__in=account_ids):
        accounts.append({
            'id': a.id, 'name': a.name, 'currency': a.currency,
            'linked': a.counterparty_id == counterparty.id,
            'balance': ser.money(a.current_balance, a.currency),
        })

    cur = company.base_currency
    return {
        'counterparty': {
            'id': counterparty.id, 'name': counterparty.name,
            'type': counterparty.get_type_display(),
        },
        'transactions': rows,
        'accounts': accounts,
        'totals': {
            'received': ser.money(received, cur),
            'paid': ser.money(paid, cur),
            'net': ser.money(received - paid, cur, signed=True),
            'planned_in': ser.money(planned_in, cur),
            'planned_out': ser.money(planned_out, cur),
        },
    }


def counterparty_detail(company, counterparty: Counterparty) -> dict:
    """Повна аналітика по контрагенту для детальної сторінки.

    Включає: підсумки, помісячний оборот (для графіка), розбивку за категоріями,
    задіяні рахунки, пов'язані магазини-реалізатори, журнал дій (audit) і
    серіалізовані операції.
    """
    cur = company.base_currency
    txns = (Transaction.objects
            .filter(company=company, counterparty=counterparty)
            .select_related('account', 'to_account', 'category', 'project',
                            'counterparty', 'recurrence_rule', 'reseller')
            .order_by('-date_actual', '-id'))

    received = paid = planned_in = planned_out = Decimal('0')
    actual_count = planned_count = 0
    first_date = last_date = None
    by_month = OrderedDict()       # 'YYYY-MM' -> {'in':, 'out':}
    by_category = {}               # name -> {'in':, 'out':, 'icon':, 'color'}
    account_ids = set()

    txn_list = list(txns)
    for t in txn_list:
        base = t.amount_base or t.amount or Decimal('0')
        if t.account_id:
            account_ids.add(t.account_id)
        if t.date_actual:
            d = t.date_actual.date()
            if first_date is None or d < first_date:
                first_date = d
            if last_date is None or d > last_date:
                last_date = d
            mkey = f'{d.year}-{d.month:02d}'
            slot = by_month.setdefault(mkey, {'in': Decimal('0'), 'out': Decimal('0')})
            if t.status == Transaction.STATUS_ACTUAL:
                if t.type == Transaction.TYPE_INCOME:
                    slot['in'] += base
                elif t.type == Transaction.TYPE_EXPENSE:
                    slot['out'] += base
        if t.status == Transaction.STATUS_ACTUAL:
            actual_count += 1
            if t.type == Transaction.TYPE_INCOME:
                received += base
            elif t.type == Transaction.TYPE_EXPENSE:
                paid += base
            if t.type in (Transaction.TYPE_INCOME, Transaction.TYPE_EXPENSE):
                cname = t.category.name if t.category else 'Без категорії'
                cslot = by_category.setdefault(cname, {
                    'in': Decimal('0'), 'out': Decimal('0'),
                    'icon': (t.category.icon if t.category else '') or '💳',
                    'color': (t.category.color if t.category else '') or '#6b7280',
                })
                if t.type == Transaction.TYPE_INCOME:
                    cslot['in'] += base
                else:
                    cslot['out'] += base
        elif t.status == Transaction.STATUS_PLANNED:
            planned_count += 1
            if t.type == Transaction.TYPE_INCOME:
                planned_in += base
            elif t.type == Transaction.TYPE_EXPENSE:
                planned_out += base

    # Рахунки: задіяні + прив'язані.
    linked_ids = set(Account.objects.filter(company=company, counterparty=counterparty)
                     .values_list('id', flat=True))
    account_ids |= linked_ids
    accounts = []
    for a in Account.objects.filter(company=company, id__in=account_ids):
        accounts.append({
            'id': a.id, 'name': a.name, 'currency': a.currency,
            'linked': a.id in linked_ids,
            'balance': ser.money(a.current_balance, a.currency),
        })

    # Магазини-реалізатори, прив'язані до контрагента.
    resellers = []
    try:
        for r in counterparty.resellers.all():
            resellers.append({'id': r.id, 'name': r.name,
                              'status': r.get_status_display()})
    except Exception:
        pass

    # Помісячний оборот (хронологічно, для графіка).
    months = sorted(by_month.keys())
    monthly = [{'month': m, 'in': float(by_month[m]['in']),
                'out': float(by_month[m]['out'])} for m in months]

    # Розбивка за категоріями (за оборотом).
    cat_rows = []
    for name, v in by_category.items():
        turnover = v['in'] + v['out']
        cat_rows.append({
            'name': name, 'icon': v['icon'], 'color': v['color'],
            'in': float(v['in']), 'out': float(v['out']),
            'turnover': float(turnover),
            'in_d': ser.money(v['in'], cur), 'out_d': ser.money(v['out'], cur),
        })
    cat_rows.sort(key=lambda x: x['turnover'], reverse=True)

    # Журнал дій по контрагенту (audit).
    audit = []
    for log in (AuditLog.objects.filter(company=company, entity_type='counterparty',
                                        entity_id=str(counterparty.id))
                .select_related('user').order_by('-created_at')[:50]):
        audit.append({
            'action': log.action,
            'summary': log.summary,
            'user': (log.user.get_username() if log.user else 'система'),
            'date': ser.date_label(log.created_at),
            'time': ser.time_label(log.created_at),
        })

    transactions = [ser.serialize_transaction(t) for t in txn_list[:200]]

    # --- Покращення картки контрагента ---
    # 1) Згорнуті планові зобов'язання цього контрагента (а не «стіна» планових
    #    рядків): «наступний платіж — дата, сума, ≈/точно».
    from . import cards as cards_service
    from . import obligations as obligations_service
    _all = obligations_service.planned_obligations(company)
    obligations = []
    for g in (_all['income'] + _all['expense']):
        if g.get('counterparty_id') != counterparty.id:
            continue
        g['per_amount_display'] = ser.money(g['per_amount'], g.get('currency') or cur)
        if g.get('remaining_amount') is not None:
            g['remaining_amount_display'] = ser.money(g['remaining_amount'], g.get('currency') or cur)
        else:
            g['remaining_amount_display'] = ''
        obligations.append(g)
    obligations.sort(key=lambda x: (not x['overdue'], x['next_due_iso'] or '9999'))

    # 2) Картки контрагента (полоски).
    cp_cards = cards_service.cards_for(counterparty)

    # 3) Історія — лише фактичні операції (планові показані як зобов'язання вище),
    #    з позначкою «у рахунок: <зобов'язання> <період>» за ObligationSettlement.
    from ..models import ObligationSettlement
    actual_txn_ids = [t.id for t in txn_list if t.status == Transaction.STATUS_ACTUAL]
    settled_map = {}
    for s in (ObligationSettlement.objects
              .filter(company=company, payment_id__in=actual_txn_ids)
              .select_related('rule')):
        label = s.rule.title if (s.rule_id and s.rule.title) else 'зобов\'язання'
        settled_map[s.payment_id] = f'{label} · {s.period_label}'
    actual_transactions = []
    for row in transactions:
        if row['status'] != Transaction.STATUS_ACTUAL:
            continue
        if row['id'] in settled_map:
            row['settled_label'] = settled_map[row['id']]
        actual_transactions.append(row)

    net = received - paid
    turnover = received + paid
    months_active = len(months)
    avg_check = (turnover / actual_count) if actual_count else Decimal('0')

    return {
        'counterparty': counterparty,
        'totals': {
            'received': ser.money(received, cur),
            'paid': ser.money(paid, cur),
            'net': ser.money(net, cur, signed=True),
            'net_positive': net >= 0,
            'turnover': ser.money(turnover, cur),
            'planned_in': ser.money(planned_in, cur),
            'planned_out': ser.money(planned_out, cur),
            'avg_check': ser.money(avg_check, cur),
        },
        'stats': {
            'actual_count': actual_count,
            'planned_count': planned_count,
            'months_active': months_active,
            'first_date': ser.date_label(_dt(first_date)) if first_date else '',
            'last_date': ser.date_label(_dt(last_date)) if last_date else '',
        },
        'monthly': monthly,
        'categories': cat_rows,
        'accounts': accounts,
        'resellers': resellers,
        'audit': audit,
        'transactions': transactions,
        'actual_transactions': actual_transactions,
        'obligations': obligations,
        'cards': cp_cards,
    }


def _dt(d):
    """date → aware datetime для ser.date_label (який очікує datetime)."""
    import datetime as _datetime
    if d is None:
        return None
    return timezone.make_aware(_datetime.datetime.combine(d, _datetime.time()))
