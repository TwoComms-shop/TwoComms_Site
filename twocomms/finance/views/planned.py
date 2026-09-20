"""Розділ «Планові» — керування плановими платежами (доходи й витрати).

Бізнес-дашборд зобов'язань: повторювані (комуналка, оренда), розстрочки боргів
магазинів і разові планові — усе згорнуто в один рядок на зобов'язання з
періодичністю, сумою за період, «скільки лишилось», датою найближчого платежу
та позначкою прострочення. Звідси ж погашають/проводять платіж із вибором
рахунку та контрагента, редагують план і дивляться історію по контрагенту.
"""
from __future__ import annotations

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from ..models import Counterparty, Transaction, get_default_company
from ..permissions import finance_access_required
from ..services import cards as cards_service
from ..services import counterparty as cp_service
from ..services import ledger_v2
from ..services import obligations as obligations_service
from ..services import obligations_v2
from ..services import payables as payables_service
from ..services import payloads as payload_service
from ..services import serializers as ser


def _fmt_obligation(company, g):
    """Форматує суми зобов'язання у відображувані рядки (raw → money)."""
    cur = g.get('currency') or company.base_currency
    g['per_amount_display'] = ser.money(g['per_amount'], cur)
    g['planned_sum_display'] = ser.money(g['planned_sum'], cur)
    if g.get('remaining_amount') is not None:
        g['remaining_amount_display'] = ser.money(g['remaining_amount'], cur)
    else:
        g['remaining_amount_display'] = ''
    return g


@finance_access_required
def planned(request):
    company = get_default_company()
    data = obligations_service.planned_obligations(company)

    income = [_fmt_obligation(company, g) for g in data['income']]
    expense = [_fmt_obligation(company, g) for g in data['expense']]

    # Таймлайн: прострочено → цей місяць → майбутні по місяцях (зрозуміле подання).
    timeline = obligations_service.planned_timeline(company)
    cur0 = company.base_currency
    tl_segments = []
    for seg in timeline['segments']:
        tl_segments.append({
            'key': seg['key'], 'label': seg['label'], 'tone': seg['tone'],
            'items': [_fmt_obligation(company, g) for g in seg['items']],
            'count': len(seg['items']),
            'income_sum': ser.money(seg['income_sum'], cur0),
            'expense_sum': ser.money(seg['expense_sum'], cur0),
            'has_income': seg['income_sum'] > 0,
            'has_expense': seg['expense_sum'] > 0,
        })

    cur = company.base_currency
    status_labels = {
        'planned': 'Запланировано', 'partial': 'Частично оплачено',
        'paid': 'Оплачено', 'overdue': 'Просрочено',
    }
    v2_group_summaries = []
    for group in company.obligation_groups.filter(is_active=True).prefetch_related('components'):
        summary = obligations_v2.group_summary(group)
        summary['status_label'] = status_labels.get(summary['status'], summary['status'])
        summary['remaining_display'] = ser.money(summary['remaining'], company.base_currency)
        for component in summary['components']:
            component['planned_display'] = ser.money(component['planned'], company.base_currency)
            component['paid_display'] = ser.money(component['paid'], company.base_currency)
            component['remaining_display'] = ser.money(component['remaining'], company.base_currency)
        v2_group_summaries.append(summary)

    def attach_v2_group(row):
        title = (row.get('title') or '').lower()
        match = None
        if 'подат' in title or 'налог' in title:
            match = next((g for g in v2_group_summaries if 'налог' in g['title'].lower()), None)
        elif 'комун' in title or 'кварт' in title or 'влад' in title:
            match = next((g for g in v2_group_summaries if 'кварт' in g['title'].lower()), None)
        elif 'офис' in title or 'оренд' in title or 'аренд' in title:
            match = next((g for g in v2_group_summaries if 'офис' in g['title'].lower()), None)
        if match and row.get('type') == Transaction.TYPE_EXPENSE:
            row['v2_group_id'] = match['id']
            row['v2_group_title'] = match['title']
        return row

    for segment in tl_segments:
        segment['items'] = [attach_v2_group(row) for row in segment['items']]
    income = [attach_v2_group(row) for row in income]
    expense = [attach_v2_group(row) for row in expense]

    context = {
        'active_tab': 'planned',
        'timeline': tl_segments,
        'income': income,
        'expense': expense,
        'income_sum': ser.money(data['income_sum'], cur),
        'expense_sum': ser.money(data['expense_sum'], cur),
        'net': ser.money(data['net'], cur, signed=True),
        'net_positive': data['net'] >= 0,
        'overdue_count': data['overdue_count'],
        'overdue_income': ser.money(data['overdue_income'], cur),
        'overdue_expense': ser.money(data['overdue_expense'], cur),
        'income_count': data['income_count'],
        'expense_count': data['expense_count'],
        'dropdowns': ser.serialize_dropdowns(company),
        'v2_groups': v2_group_summaries,
        'v2_accounts': [
            {'id': a.id, 'name': a.name, 'balance': str(a.current_balance), 'currency': a.currency}
            for a in company.accounts.filter(is_active=True, is_archived=False).order_by('sort_order', 'id')
        ],
        'v2_funding': [
            {
                'id': source.id,
                'name': source.name,
                'source_type': source.source_type,
                **{key: str(value) for key, value in ledger_v2.funding_summary(source).items()},
                'pending_review_count': source.classified_transactions.filter(economic_kind='unknown').count(),
            }
            for source in company.funding_sources.filter(is_active=True).order_by('-received_at', 'name')
        ],
    }
    return render(request, 'finance/planned.html', context)


@finance_access_required(api=True)
@require_GET
def planned_obligations_api(request):
    """JSON-версія зобов'язань (для динамічного оновлення без перезавантаження)."""
    company = get_default_company()
    data = obligations_service.planned_obligations(company)
    income = [_fmt_obligation(company, g) for g in data['income']]
    expense = [_fmt_obligation(company, g) for g in data['expense']]
    cur = company.base_currency

    def _clean(items):
        out = []
        for g in items:
            out.append({k: v for k, v in g.items()
                        if k not in ('per_amount', 'planned_sum', 'remaining_amount', 'next_due')})
        return out

    return JsonResponse({
        'ok': True,
        'income': _clean(income),
        'expense': _clean(expense),
        'income_sum': ser.money(data['income_sum'], cur),
        'expense_sum': ser.money(data['expense_sum'], cur),
        'net': ser.money(data['net'], cur, signed=True),
        'overdue_count': data['overdue_count'],
    })


@finance_access_required(api=True)
@require_GET
def counterparty_history_api(request, counterparty_id):
    """Історія по контрагенту: операції, рахунки, підсумки."""
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    return JsonResponse({'ok': True, **cp_service.counterparty_history(company, cp)})


# ----------------------------- Точне погашення зобов'язань -----------------------------

def _account_options(company, counterparty):
    """Рахунки для модалки: привʼязані до контрагента — першими."""
    out = []
    for a in company.accounts.filter(is_active=True, is_archived=False).order_by('sort_order'):
        out.append({
            'id': a.id, 'name': a.name, 'currency': a.currency,
            'balance': str(a.current_balance),
            'linked': bool(counterparty and a.counterparty_id == counterparty.id),
        })
    out.sort(key=lambda x: (not x['linked'],))
    return out


@finance_access_required(api=True)
@require_GET
def obligation_settle_context_api(request, txn_id):
    """Контекст для модалки погашення: контрагент, картки, рахунки, кандидати-платежі."""
    company = get_default_company()
    planned = get_object_or_404(Transaction, id=txn_id, company=company,
                                status=Transaction.STATUS_PLANNED)
    cp = planned.counterparty
    candidates = payables_service.payable_candidates(
        company, ttype=planned.type, counterparty=cp)
    # Скільки періодів наперед можна оплатити (кількість планових екземплярів
    # цього ж правила, але не більше 12 — для степпера «за N місяців»).
    max_periods = 1
    if planned.recurrence_rule_id:
        max_periods = min(12, planned.recurrence_rule.transactions.filter(
            status=Transaction.STATUS_PLANNED).count()) or 1
    return JsonResponse({
        'ok': True,
        'ttype': planned.type,
        'title': planned.comment or (planned.recurrence_rule.title if planned.recurrence_rule_id else ''),
        'per_amount': str(planned.amount),
        'per_amount_display': ser.money(planned.amount, planned.currency),
        'estimated': bool(planned.amount_is_estimated),
        'is_recurring': bool(planned.recurrence_rule_id),
        'max_periods': max_periods,
        'counterparty': ({'id': cp.id, 'name': cp.name} if cp else None),
        'cards': cards_service.cards_for(cp) if cp else [],
        'candidates': candidates,
        'accounts': _account_options(company, cp),
    })


def _bool(v):
    return str(v).strip().lower() in ('1', 'true', 'on', 'yes')


@finance_access_required(api=True)
@require_POST
def obligation_settle_api(request, txn_id):
    """Гасить зобов'язання (планове) реальним платежем — новим або наявним."""
    from .payments import _body
    company = get_default_company()
    planned = get_object_or_404(Transaction, id=txn_id, company=company,
                                status=Transaction.STATUS_PLANNED)
    data = _body(request)
    mode = data.get('mode') or 'new_payment'

    account = None
    if data.get('account_id'):
        account = company.accounts.filter(id=data.get('account_id')).first()
    payment_txn = None
    if data.get('payment_txn_id'):
        payment_txn = company.transactions.filter(
            id=data.get('payment_txn_id'), status=Transaction.STATUS_ACTUAL).first()
        if payment_txn is None:
            return JsonResponse({'ok': False, 'error': 'Платіж не знайдено'}, status=400)

    amount = None
    if data.get('amount') not in (None, ''):
        try:
            amount = payload_service._decimal(data.get('amount'), 'amount')
        except payload_service.PayloadError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)

    date = payload_service._parse_dt(str(data.get('date'))) if data.get('date') else None
    full_period = _bool(data.get('full_period', '1')) if 'full_period' in data else None
    remember_card = _bool(data.get('remember_card', ''))
    try:
        periods = max(1, int(data.get('periods') or 1))
    except (TypeError, ValueError):
        periods = 1

    card_hint = None
    if remember_card:
        if payment_txn is not None:
            card_hint = cards_service.extract_card_hint(payment_txn)
        elif data.get('card_mask') or data.get('card_iban'):
            card_hint = {'pan_mask': data.get('card_mask', ''), 'last4': '',
                         'bank': data.get('card_bank', ''), 'iban': data.get('card_iban', '')}

    try:
        res = payables_service.settle_obligation(
            user=request.user, planned_txn=planned, mode=mode, amount=amount,
            payment_txn=payment_txn, account=account, date=date,
            full_period=full_period, remember_card=remember_card, card_hint=card_hint,
            periods=periods)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)

    return JsonResponse({
        'ok': True,
        'full_period': res['full_period'],
        'periods': res.get('periods', 1),
        'payment_id': res['payment'].id,
        'settlement_id': res['settlement'].id,
    })


@finance_access_required(api=True)
@require_POST
def obligation_move_current_api(request, txn_id):
    """Перенести найближчий плановий екземпляр зобов'язання на поточний місяць.

    Зручність: оплатити майбутній період уже зараз — переносимо дату планового
    на сьогодні, тож воно зʼявляється в сегменті «Цей місяць» з кнопкою оплати.
    Сам графік правила (next_occurrence) не чіпаємо.
    """
    import datetime as _dt
    from .payments import _body  # noqa: F401  (узгодженість, не використовується)
    company = get_default_company()
    planned = get_object_or_404(Transaction, id=txn_id, company=company,
                                status=Transaction.STATUS_PLANNED)
    now = timezone.localtime(timezone.now())
    old = planned.date_actual
    keep_time = timezone.localtime(old).time() if old else now.time()
    # The legacy API contract reads ``date_actual.date()`` directly. Store the
    # selected calendar date in UTC; the UI converts it to local time when it
    # renders the timestamp.
    new_dt = timezone.make_aware(_dt.datetime.combine(now.date(), keep_time), _dt.timezone.utc)
    from ..services import transactions as txn_service
    txn_service.update_transaction(planned, user=request.user, date_actual=new_dt)
    return JsonResponse({'ok': True})


@finance_access_required(api=True)
@require_POST
def obligation_skip_api(request, txn_id):
    """Пропустити місяць зобов'язання (перенести в кінець / анулювати період)."""
    from .payments import _body
    company = get_default_company()
    planned = get_object_or_404(Transaction, id=txn_id, company=company,
                                status=Transaction.STATUS_PLANNED)
    data = _body(request)
    mode = data.get('mode') or 'move_end'
    if mode not in ('move_end', 'drop'):
        mode = 'move_end'
    try:
        res = payables_service.skip_occurrence(user=request.user, planned_txn=planned, mode=mode)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    return JsonResponse({'ok': True, **res})
