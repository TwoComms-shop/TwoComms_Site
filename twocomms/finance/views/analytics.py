"""Розділ «Аналітика»: вітрина звітів + детальні звіти + експорт XLSX."""
from __future__ import annotations

import datetime as dt
import json
import calendar as calendar_lib
import colorsys
from decimal import Decimal

from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from ..models import AuditLog, BudgetPlan, FinancialMetric, Transaction, get_default_company
from ..permissions import finance_access_required
from ..services import reports as rep
from ..services import reports_debt as repd
from ..services import serializers as ser


REPORT_CARDS = [
    {'kind': 'cashflow', 'title': 'Гроші / Cash flow', 'desc': 'Рух грошових коштів'},
    {'kind': 'pnl', 'title': 'P&L', 'desc': 'Прибутки та збитки'},
    {'kind': 'forecast', 'title': 'Прогноз балансу', 'desc': 'Прогноз на 6 місяців вперед'},
    {'kind': 'owner_drawings', 'title': 'Вивід на особисте', 'desc': 'Розподіл прибутку власником'},
    {'kind': 'personal_expenses', 'title': 'Особисті витрати', 'desc': 'Куди йдуть гроші поза бізнесом'},
    {'kind': 'receivables', 'title': 'Дебіторка', 'desc': 'Хто винен компанії'},
    {'kind': 'payables', 'title': 'Кредиторка', 'desc': 'Кому винна компанія'},
    {'kind': 'statement', 'title': 'Виписка за рахунком', 'desc': 'Рух по рахунку'},
    {'kind': 'projects', 'title': 'Проекти', 'desc': 'Прибутковість проектів'},
    {'kind': 'warehouse_dynamics', 'title': 'Динаміка складу', 'desc': 'Рух товарів по днях'},
    {'kind': 'warehouse_structure', 'title': 'Структура складу', 'desc': 'Розподіл за категоріями'},
    {'kind': 'warehouse_turnover', 'title': 'Оборотність складу', 'desc': 'Швидкість обороту товарів'},
    {'kind': 'resellers', 'title': 'Магазини під реалізацію', 'desc': 'Борги та заморожені товари по магазинах'},
    {'kind': 'audit', 'title': 'Історія дій', 'desc': 'Журнал змін'},
    {'kind': 'balance', 'title': 'Баланс', 'desc': 'Активи та пасиви'},
    {'kind': 'plan_fact', 'title': 'План/Факт', 'desc': 'Порівняння плану і факту'},
    {'kind': 'metrics', 'title': 'Фінансові показники', 'desc': 'EBITDA, маржа, KPI'},
]


def _m(company, v, signed=False):
    return ser.money(v, company.base_currency, signed=signed)


def _breakdown(company, rows, total):
    """Категорійна розбивка з відсотками для таблиць під графіками."""
    out = []
    for r in rows:
        amt = Decimal(str(r['total']))
        pct = round(float(amt / total * 100), 1) if total else 0.0
        out.append({'name': r['name'], 'amount': _m(company, amt), 'pct': pct})
    return out


def _pnl_previous_period(period, start, end):
    """Return the immediately preceding period for the comparison overlay."""
    if period == 'all' or not start or not end:
        return None
    span = (end - start).days + 1
    previous_end = start - dt.timedelta(days=1)
    return previous_end - dt.timedelta(days=span - 1), previous_end


def _pnl_detail_rows(company, rows, previous_rows, start, end):
    """Prepare the compact expense table used by the P&L dashboard."""
    previous = {row['name']: Decimal(str(row['total'])) for row in previous_rows}
    budget_rows = (BudgetPlan.objects.filter(
        company=company, period_start__lte=end, period_end__gte=start,
    ).values('category__name').annotate(total=Sum('planned_expense')))
    budgets = {row['category__name'] or 'Без категорії': row['total'] or Decimal('0')
               for row in budget_rows}
    icons = {
        'Готівка та фінанси': '▣', 'Кафе та ресторани': '♜', 'Продукти': '◉',
        'Їжа та продукти': '◉', 'Транспорт': '▣', 'Без категорії': '▤',
        'Логістика та доставка': '◌', 'Комісія за переказ': '▤',
    }
    out = []
    for index, row in enumerate(rows, 1):
        amount = Decimal(str(row['total']))
        prior = previous.get(row['name'], Decimal('0'))
        budget = budgets.get(row['name'], Decimal('0'))
        change = ((amount - prior) / prior * 100) if prior else None
        ratio = (amount / budget * 100) if budget else None
        if ratio is None:
            status, status_class = 'Немає плану', 'muted'
            progress = 0
        elif ratio > 100:
            status, status_class = 'Перевищено', 'danger'
            progress = 100
        elif ratio >= 90:
            status, status_class = 'У межах', 'ok'
            progress = round(float(ratio))
        else:
            status, status_class = 'Нижче плану', 'ok'
            progress = round(float(ratio))
        out.append({
            'index': index, 'name': row['name'], 'icon': icons.get(row['name'], '◌'),
            'amount': _m(company, amount), 'amount_value': float(amount),
            'pct': round(float(amount / Decimal(str(sum(float(r['total']) for r in rows))) * 100), 1) if rows else 0,
            'change': round(float(change), 1) if change is not None else None,
            'budget': _m(company, budget) if budget else '—',
            'budget_value': float(budget), 'progress': progress,
            'status': status, 'status_class': status_class,
        })
    return out


def _pnl_heat_color(tone, heat):
    """Return a solid, perceptually stepped color for a calendar cell.

    The scale deliberately changes both lightness and saturation. That keeps
    neighboring ranked values distinguishable on the dark finance surface,
    even when a period contains a very large outlier.
    """
    if tone == 'neutral':
        return '#3a4658'
    value = max(0.0, min(1.0, float(heat)))
    hue = 164 / 360 if tone == 'positive' else 350 / 360
    saturation = 0.30 + value * 0.58
    lightness = 0.31 + value * 0.25
    rgb = tuple(round(channel * 255) for channel in colorsys.hls_to_rgb(
        hue, lightness, saturation,
    ))
    return '#{:02x}{:02x}{:02x}'.format(*rgb)


def _pnl_calendar(company, period, start):
    """Build a month grid from actual daily P&L values for the dashboard."""
    from django.utils import timezone

    anchor = timezone.localdate() if period == 'all' else start
    month_start = anchor.replace(day=1)
    month_end = anchor.replace(day=calendar_lib.monthrange(anchor.year, anchor.month)[1])
    daily = rep.pnl(company, {
        'period': 'custom', 'date_from': month_start.isoformat(), 'date_to': month_end.isoformat(),
    })
    by_day = {row['label']: row for row in daily['series']}
    cells = [{'day': None} for _ in range(month_start.weekday())]
    for day in range(1, month_end.day + 1):
        key = f'{anchor.year}-{anchor.month:02d}-{day:02d}'
        row = by_day.get(key, {'in': 0, 'out': 0})
        income = Decimal(str(row.get('in', 0)))
        expense = Decimal(str(row.get('out', 0)))
        profit = income - expense
        cells.append({
            'day': day, 'income': float(income), 'expense': float(expense), 'profit': float(profit),
            'income_display': _m(company, income), 'expense_display': _m(company, expense),
            'profit_display': _m(company, profit, signed=True),
            'tone': 'positive' if profit > 0 else 'negative' if profit < 0 else 'neutral',
        })
    def ranked_heats(tone):
        """Give every distinct result its own stable position on the scale."""
        values = sorted({
            abs(Decimal(str(cell['profit'])))
            for cell in cells
            if cell.get('day') and cell['tone'] == tone
        })
        if not values:
            return {}
        if len(values) == 1:
            return {values[0]: 1.0}
        last = len(values) - 1
        return {value: round(index / last, 6) for index, value in enumerate(values)}

    positive_heats = ranked_heats('positive')
    negative_heats = ranked_heats('negative')

    for cell in cells:
        if cell.get('day'):
            if cell['tone'] == 'positive':
                cell['heat'] = positive_heats[abs(Decimal(str(cell['profit'])))]
            elif cell['tone'] == 'negative':
                cell['heat'] = negative_heats[abs(Decimal(str(cell['profit'])))]
            else:
                cell['heat'] = 0.0
            cell['background'] = _pnl_heat_color(cell['tone'], cell['heat'])
    while len(cells) % 7:
        cells.append({'day': None})
    month_names = ('Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                   'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень')
    return {
        'label': f'{month_names[anchor.month - 1]} {anchor.year}',
        'year': anchor.year, 'month': anchor.month, 'cells': cells,
        'income': _m(company, daily['income']), 'expense': _m(company, daily['expenses']),
        'profit': _m(company, daily['profit'], signed=True),
    }


@finance_access_required
def analytics(request):
    company = get_default_company()
    return render(request, 'finance/analytics.html', {
        'active_tab': 'analytics',
        'cards': REPORT_CARDS,
    })


def _smart_default_period(company):
    """Дефолтний період звіту: 'month', але якщо за поточний місяць немає
    фактичних операцій — 'all' (щоб не показувати порожньо, коли дані за інший
    період, напр. на початку місяця)."""
    from django.utils import timezone
    from .. models import Transaction
    from ..services.timeutil import day_start
    month_start = timezone.localdate().replace(day=1)
    has_this_month = (Transaction.objects.filter(
        company=company, status=Transaction.STATUS_ACTUAL,
        date_actual__gte=day_start(month_start)).exists())
    return 'month' if has_this_month else 'all'


@finance_access_required
def report(request, kind):
    company = get_default_company()
    # Якщо період не заданий явно — обираємо розумний дефолт.
    period = request.GET.get('period') or _smart_default_period(company)
    ctx = {'active_tab': 'analytics', 'kind': kind,
           'dropdowns': ser.serialize_dropdowns(company),
           'period': period}

    if kind == 'cashflow':
        data = rep.cash_flow(company, request.GET)
        bi = _breakdown(company, data['income_by_category'], data['cash_in'])
        be = _breakdown(company, data['expense_by_category'], data['cash_out'])
        net = data['net']
        insights = [f"За період надійшло {_m(company, data['cash_in'])}, списано {_m(company, data['cash_out'])}."]
        if net >= 0:
            insights.append(f"Чистий потік додатний: {_m(company, net, signed=True)} — приплив перевищує відплив.")
        else:
            insights.append(f"Чистий потік від'ємний: {_m(company, net, signed=True)} — витрати перевищили надходження.")
        if bi:
            insights.append(f"Найбільше надходжень — «{bi[0]['name']}» ({bi[0]['pct']}%).")
        if be:
            insights.append(f"Найбільша стаття списань — «{be[0]['name']}» ({be[0]['pct']}%).")
        ctx.update({
            'title': 'Cash flow', 'data': data,
            'cash_in': _m(company, data['cash_in']),
            'cash_out': _m(company, data['cash_out']),
            'net': _m(company, data['net'], signed=True),
            'targeted_in': _m(company, data['targeted_in']),
            'net_positive': net >= 0,
            'breakdown_income': bi,
            'breakdown_expense': be,
            'insights': insights,
            'chart_data': json.dumps({
                'series': data['series'],
                'income_by_category': data['income_by_category'],
                'expense_by_category': data['expense_by_category'],
            }),
        })
        return render(request, 'finance/reports/cashflow.html', ctx)

    if kind == 'pnl':
        data = rep.pnl(company, request.GET)
        bi = _breakdown(company, data['income_by_category'], data['income'])
        be = _breakdown(company, data['expense_by_category'], data['expenses'])
        profit = data['profit']
        start = dt.date.fromisoformat(data['period'][0])
        end = dt.date.fromisoformat(data['period'][1])
        previous_period = _pnl_previous_period(period, start, end)
        previous_data = rep.pnl(company, {
            'period': 'custom',
            'date_from': previous_period[0].isoformat(),
            'date_to': previous_period[1].isoformat(),
        }) if previous_period else None
        detail_rows = _pnl_detail_rows(
            company, data['expense_by_category'],
            previous_data['expense_by_category'] if previous_data else [], start, end,
        )
        top_expense = detail_rows[0] if detail_rows else None
        expense_change = None
        if previous_data and previous_data['expenses']:
            expense_change = float((data['expenses'] - previous_data['expenses']) / previous_data['expenses'] * 100)
        income_change = None
        if previous_data and previous_data['income']:
            income_change = float((data['income'] - previous_data['income']) / previous_data['income'] * 100)
        recommendations = []
        if top_expense:
            recommendations.append({
                'icon': '⚠', 'tone': 'danger', 'title': f'Скоротити витрати в категорії «{top_expense["name"]}»',
                'body': f'Найбільша стаття займає {top_expense["pct"]}% усіх витрат. Перевірте повторювані платежі.',
            })
        if top_expense and top_expense['change'] is not None and top_expense['change'] > 10:
            recommendations.append({
                'icon': '◉', 'tone': 'warning', 'title': f'Оптимізувати «{top_expense["name"]}»',
                'body': f'Витрати зросли на {top_expense["change"]:.1f}% проти попереднього періоду.',
            })
        if detail_rows and not any(row['budget_value'] for row in detail_rows):
            recommendations.append({
                'icon': '◌', 'tone': 'violet', 'title': 'Додайте бюджети категорій',
                'body': 'План-факт стане точнішим, коли для основних категорій буде задано бюджет.',
            })
        if income_change is not None and income_change > 0:
            recommendations.append({
                'icon': '↗', 'tone': 'success', 'title': 'Доходи зростають',
                'body': f'Приріст доходів становить {income_change:.1f}% проти попереднього періоду.',
            })
        if not recommendations:
            recommendations.append({
                'icon': '✓', 'tone': 'success', 'title': 'Дані готові до аналізу',
                'body': 'Додайте кілька періодів, щоб побачити більше рекомендацій.',
            })
        insights = [f"Доходи {_m(company, data['income'])}, витрати {_m(company, data['expenses'])}."]
        if profit >= 0:
            insights.append(f"Прибуток {_m(company, profit, signed=True)} за маржі {round(data['margin'], 1)}% — бізнес у плюсі.")
        else:
            insights.append(f"Збиток {_m(company, profit, signed=True)} — витрати перевищили доходи за період.")
        if be:
            insights.append(f"Найбільша стаття витрат — «{be[0]['name']}» ({be[0]['pct']}% витрат).")
        if bi:
            insights.append(f"Основне джерело доходу — «{bi[0]['name']}» ({bi[0]['pct']}%).")
        ctx.update({
            'title': 'P&L', 'data': data,
            'income': _m(company, data['income']),
            'expenses': _m(company, data['expenses']),
            'profit': _m(company, data['profit'], signed=True),
            'margin': round(data['margin'], 1),
            'profit_positive': profit >= 0,
            'breakdown_income': bi,
            'breakdown_expense': be,
            'detail_rows': detail_rows,
            'budget_rows': detail_rows[:8],
            'recommendations': recommendations[:4],
            'date_from': start.strftime('%-d %b %Y'),
            'date_to': end.strftime('%-d %b %Y'),
            'income_change': income_change,
            'expense_change': expense_change,
            'previous_period_label': 'попереднім місяцем' if period in ('month', 'last_month') else 'попереднім періодом',
            'pnl_calendar': _pnl_calendar(company, period, start),
            'insights': insights,
            'chart_data': json.dumps({
                'series': data['series'],
                'previous_series': previous_data['series'] if previous_data else [],
                'income_by_category': data['income_by_category'],
                'expense_by_category': data['expense_by_category'],
            }),
        })
        return render(request, 'finance/reports/pnl.html', ctx)

    if kind == 'owner_drawings':
        data = rep.owner_drawings_report(company, request.GET)
        insights = []
        if data['total_withdrawn'] > 0:
            insights.append(f"За період виведено {_m(company, data['total_withdrawn'])} на особисті потреби.")
            if data['business_profit'] > 0:
                insights.append(f"Бізнес-прибуток за період: {_m(company, data['business_profit'])}.")
                insights.append(f"Виведено {data['withdrawal_percent']:.1f}% від прибутку — {'помірний' if data['withdrawal_percent'] < 50 else 'високий'} рівень.")
            else:
                insights.append(f"Увага: виведення при збитковості бізнесу ({_m(company, data['business_profit'], signed=True)}).")
        else:
            insights.append("За період не було виведень на особисті потреби.")

        ctx.update({
            'title': 'Вивід на особисте',
            'data': data,
            'total_withdrawn': _m(company, data['total_withdrawn']),
            'business_profit': _m(company, data['business_profit'], signed=True),
            'withdrawal_percent': round(data['withdrawal_percent'], 1),
            'insights': insights,
            'transfers': [ser.serialize_transaction(t) for t in data['transfers']],
            'chart_data': json.dumps({
                'by_month': [{'month': m, 'amount': float(a)} for m, a in data['by_month']],
            }),
        })
        return render(request, 'finance/reports/owner_drawings.html', ctx)

    if kind == 'personal_expenses':
        data = rep.personal_expenses_report(company, request.GET)
        insights = []
        if data['total_personal'] > 0:
            insights.append(f"За період витрачено {_m(company, data['total_personal'])} на особисті потреби.")
            if data['total_income'] > 0:
                insights.append(f"Це {data['personal_percent']:.1f}% від загального доходу.")
            if data['business_expenses'] > 0:
                ratio = float(data['total_personal'] / data['business_expenses'] * 100)
                insights.append(f"Співвідношення особисті/бізнес витрати: {ratio:.1f}%.")
            if data['categories']:
                top_cat = data['categories'][0]
                insights.append(f"Найбільша стаття витрат — «{top_cat['name']}» ({top_cat['pct']:.1f}%).")
        else:
            insights.append("За період не було особистих витрат.")

        ctx.update({
            'title': 'Особисті витрати',
            'data': data,
            'total_personal': _m(company, data['total_personal']),
            'total_income': _m(company, data['total_income']),
            'business_expenses': _m(company, data['business_expenses']),
            'personal_percent': round(data['personal_percent'], 1),
            'insights': insights,
            'chart_data': json.dumps({
                'categories': [{'name': c['name'], 'amount': float(c['amount']),
                               'color': c['color'], 'icon': c['icon']}
                              for c in data['categories']],
                'by_month': [{'month': m, 'amount': float(a)} for m, a in data['by_month']],
            }),
        })
        return render(request, 'finance/reports/personal_expenses.html', ctx)

    if kind == 'forecast':
        months = int(request.GET.get('months', 6))
        data = rep.balance_forecast_report(company, months=months)

        insights = []
        if data['final_balance'] < 0:
            insights.append(f"⚠️ Увага! Прогнозований баланс через {months} міс. буде негативним: {_m(company, data['final_balance'], signed=True)}.")
        elif data['final_balance'] < Decimal('10000'):
            insights.append(f"⚠️ Попередження: прогнозований баланс через {months} міс. буде низьким: {_m(company, data['final_balance'])}.")
        else:
            insights.append(f"✅ Прогнозований баланс через {months} міс.: {_m(company, data['final_balance'])} — стабільна ситуація.")

        negative_months = [m for m in data['forecast'] if m['is_negative']]
        if negative_months:
            first_neg = negative_months[0]
            insights.append(f"⚠️ Перший негативний баланс очікується в {first_neg['month_name']}.")

        if data['total_planned_income'] > 0:
            insights.append(f"Заплановано надходжень: {_m(company, data['total_planned_income'])}.")
        if data['total_planned_expense'] > 0:
            insights.append(f"Заплановано витрат: {_m(company, data['total_planned_expense'])}.")

        ctx.update({
            'title': 'Прогноз балансу',
            'data': data,
            'months': months,
            'current_balance': _m(company, data['current_balance']),
            'final_balance': _m(company, data['final_balance'], signed=True),
            'balance_change': _m(company, data['final_balance'] - data['current_balance'], signed=True),
            'insights': insights,
            'chart_data': json.dumps({
                'forecast': [{'month': m['month'], 'balance': float(m['ending_balance']),
                             'income': float(m['planned_income']), 'expense': float(m['planned_expense'])}
                            for m in data['forecast']],
            }),
        })
        return render(request, 'finance/reports/forecast.html', ctx)

    if kind == 'receivables':
        data = repd.receivables(company)
        # Розбивка дебіторки за віком (aging) — переюзаємо логіку рушія здоров'я.
        from django.utils import timezone as _tz
        today = _tz.localdate()
        aging = {'not_due': Decimal('0'), 'd0_30': Decimal('0'), 'd31_60': Decimal('0'),
                 'd61_90': Decimal('0'), 'd90_plus': Decimal('0')}
        for r in data['rows']:
            amt = Decimal(str(r['amount']))
            due = r.get('date')
            overdue_days = 0
            if due:
                try:
                    due_d = dt.date.fromisoformat(str(due)[:10])
                    overdue_days = (today - due_d).days
                except (ValueError, TypeError):
                    overdue_days = 0
            if overdue_days <= 0:
                aging['not_due'] += amt
            elif overdue_days <= 30:
                aging['d0_30'] += amt
            elif overdue_days <= 60:
                aging['d31_60'] += amt
            elif overdue_days <= 90:
                aging['d61_90'] += amt
            else:
                aging['d90_plus'] += amt
        total_ar = data['total'] or Decimal('1')
        aging_rows = [
            {'label': 'Не прострочено', 'value': _m(company, aging['not_due']),
             'pct': round(float(aging['not_due'] / total_ar * 100), 1), 'risk': 'ok'},
            {'label': '1–30 днів', 'value': _m(company, aging['d0_30']),
             'pct': round(float(aging['d0_30'] / total_ar * 100), 1), 'risk': 'low'},
            {'label': '31–60 днів', 'value': _m(company, aging['d31_60']),
             'pct': round(float(aging['d31_60'] / total_ar * 100), 1), 'risk': 'mid'},
            {'label': '61–90 днів', 'value': _m(company, aging['d61_90']),
             'pct': round(float(aging['d61_90'] / total_ar * 100), 1), 'risk': 'high'},
            {'label': '90+ днів', 'value': _m(company, aging['d90_plus']),
             'pct': round(float(aging['d90_plus'] / total_ar * 100), 1), 'risk': 'crit'},
        ]
        ctx.update({'title': 'Дебіторка', 'rows': _fmt_debt(company, data['rows']),
                    'total': _m(company, data['total']), 'aging_rows': aging_rows})
        return render(request, 'finance/reports/debt.html', ctx)

    if kind == 'payables':
        data = repd.payables(company)
        ctx.update({'title': 'Кредиторка', 'rows': _fmt_debt(company, data['rows']),
                    'total': _m(company, data['total']), 'is_payable': True})
        return render(request, 'finance/reports/debt.html', ctx)

    if kind == 'statement':
        acc_id = request.GET.get('account')
        account = company.accounts.filter(id=acc_id).first() or company.accounts.first()
        if not account:
            ctx.update({'title': 'Виписка за рахунком', 'no_account': True})
            return render(request, 'finance/reports/statement.html', ctx)
        data = rep.account_statement(company, account, request.GET)
        ctx.update({
            'title': f'Виписка: {account.name}', 'account': account,
            'opening': _m(company, data['opening']), 'closing': _m(company, data['closing']),
            'income': _m(company, data['income']), 'expense': _m(company, data['expense']),
            'rows': [ser.serialize_transaction(t) for t in data['transactions']],
        })
        return render(request, 'finance/reports/statement.html', ctx)

    if kind == 'projects':
        data = rep.projects_report(company, request.GET)
        for r in data['rows']:
            r['income_d'] = _m(company, r['income'])
            r['expense_d'] = _m(company, r['expense'])
            r['profit_d'] = _m(company, r['profit'], signed=True)
        ctx.update({'title': 'Проекти', 'rows': data['rows']})
        return render(request, 'finance/reports/projects.html', ctx)

    if kind == 'audit':
        logs = AuditLog.objects.filter(company=company).select_related('user')[:300]
        ctx.update({'title': 'Історія дій', 'logs': logs})
        return render(request, 'finance/reports/audit.html', ctx)

    if kind == 'balance':
        data = repd.balance_sheet(company)
        for side in ('assets', 'liabilities'):
            for r in data[side]:
                r['amount_d'] = _m(company, r['amount'])
        ctx.update({
            'title': 'Баланс', 'data': data,
            'total_assets': _m(company, data['total_assets']),
            'total_liabilities': _m(company, data['total_liabilities']),
            'difference': _m(company, data['difference'], signed=True),
        })
        return render(request, 'finance/reports/balance.html', ctx)

    if kind == 'plan_fact':
        data = repd.plan_fact(company, request.GET)
        for r in data['rows']:
            r['plan_d'] = _m(company, r['plan'])
            r['fact_d'] = _m(company, r['fact'])
            r['deviation_d'] = _m(company, r['deviation'], signed=True)
            r['completion'] = round(r['completion'], 1)
        ctx.update({'title': 'План/Факт', 'rows': data['rows']})
        return render(request, 'finance/reports/plan_fact.html', ctx)

    if kind == 'metrics':
        data = repd.metrics(company, request.GET)
        for r in data['base']:
            r['value_d'] = (f"{r['value']}%" if r.get('is_percent') else _m(company, r['value']))
        for r in data['custom']:
            r['value_d'] = _m(company, r['value'])
        ctx.update({'title': 'Фінансові показники', 'data': data,
                    'income_categories': company.categories.exclude(type='expense'),
                    'expense_categories': company.categories.exclude(type='income')})
        return render(request, 'finance/reports/metrics.html', ctx)

    # БЛОК 5: Аналітика складу
    if kind == 'warehouse_dynamics':
        from finance.services import warehouse_analytics as wa
        data = wa.warehouse_dynamics(days=int(request.GET.get('days', 90)))
        ctx.update({
            'title': 'Динаміка складу',
            'data': data,
            'current_value': _m(company, data['current_value']),
            'chart_data': json.dumps({
                'dates': data['dates'],
                'added': data['added_value'],
                'sold': data['sold_value'],
                'written_off': data['written_off_value'],
            }),
        })
        return render(request, 'finance/reports/warehouse_dynamics.html', ctx)

    if kind == 'warehouse_structure':
        from finance.services import warehouse_analytics as wa
        data = wa.warehouse_structure()
        ctx.update({
            'title': 'Структура складу',
            'data': data,
            'total_value': _m(company, data['total_value']),
            'chart_data': json.dumps({
                'categories': [{'name': c['name'], 'value': float(c['value'])} for c in data['by_category']],
                'consumables': [{'name': c['name'], 'value': float(c['value'])} for c in data['by_consumable_category']],
            }),
        })
        return render(request, 'finance/reports/warehouse_structure.html', ctx)

    if kind == 'warehouse_turnover':
        from finance.services import warehouse_analytics as wa
        data = wa.warehouse_turnover()
        ctx.update({
            'title': 'Оборотність складу',
            'data': data,
            'avg_days': data['avg_days_in_stock'],
        })
        return render(request, 'finance/reports/warehouse_turnover.html', ctx)

    if kind == 'resellers':
        from finance.services import consignment as cons
        from ..models import Reseller
        rows = []
        total_debt = Decimal('0')
        total_frozen = Decimal('0')
        for r in Reseller.objects.filter(company=company).select_related('counterparty'):
            debt = cons.reseller_debt(r)
            frozen = cons.reseller_frozen(r)
            total_debt += debt
            total_frozen += frozen
            rows.append({
                'id': r.id, 'name': r.name,
                'counterparty': r.counterparty.name if r.counterparty else '—',
                'status': r.get_status_display(),
                'debt': _m(company, debt), 'debt_raw': debt,
                'frozen': _m(company, frozen), 'frozen_raw': frozen,
                'overdue_days': cons.reseller_overdue_days(r),
            })
        rows.sort(key=lambda x: x['debt_raw'] + x['frozen_raw'], reverse=True)
        ctx.update({
            'title': 'Магазини під реалізацію',
            'rows': rows,
            'total_debt': _m(company, total_debt),
            'total_frozen': _m(company, total_frozen),
        })
        return render(request, 'finance/reports/resellers.html', ctx)

    ctx.update({'title': 'Звіт', 'section_subtitle': 'Невідомий звіт'})
    return render(request, 'finance/coming_soon.html',
                  {'section_title': 'Звіт', 'active_tab': 'analytics'})


def _fmt_debt(company, rows):
    for r in rows:
        r['amount_d'] = _m(company, r['amount'])
    return rows


# ----------------------------- Експорт XLSX -----------------------------

@finance_access_required
def report_export(request, kind):
    company = get_default_company()
    try:
        from openpyxl import Workbook
    except ImportError:
        return JsonResponse({'ok': False, 'error': 'openpyxl недоступний'}, status=500)

    wb = Workbook()
    ws = wb.active
    ws.title = kind[:31]

    if kind == 'projects':
        data = rep.projects_report(company, request.GET)
        ws.append(['Проект', 'Дохід', 'Витрата', 'Прибуток', 'Маржа %', 'Частка %'])
        for r in data['rows']:
            ws.append([r['name'], float(r['income']), float(r['expense']),
                       float(r['profit']), round(r['margin'], 1), round(r['share'], 1)])
    elif kind == 'plan_fact':
        data = repd.plan_fact(company, request.GET)
        ws.append(['Категорія', 'План', 'Факт', 'Відхилення', 'Виконання %'])
        for r in data['rows']:
            ws.append([r['category'], float(r['plan']), float(r['fact']),
                       float(r['deviation']), round(r['completion'], 1)])
    elif kind in ('receivables', 'payables'):
        data = repd.receivables(company) if kind == 'receivables' else repd.payables(company)
        ws.append(['Контрагент', 'Сума', 'Дата', 'Проект', 'Статус', 'Коментар'])
        for r in data['rows']:
            ws.append([r['counterparty'], float(r['amount']), r['date'],
                       r['project'], r['status'], r['comment']])
    else:
        # Загальний експорт операцій періоду.
        from .. services import filters as fs
        qs = fs.filter_transactions(company, request.GET)
        ws.append(['Дата', 'Тип', 'Сума', 'Валюта', 'Рахунок', 'Категорія', 'Контрагент', 'Проект', 'Коментар'])
        for t in qs[:5000]:
            ws.append([
                t.date_actual.strftime('%Y-%m-%d %H:%M') if t.date_actual else '',
                t.get_type_display(), float(t.amount), t.currency,
                t.account.name if t.account else '', t.category.name if t.category else '',
                t.counterparty.name if t.counterparty else '', t.project.name if t.project else '',
                t.comment,
            ])

    resp = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="finance_{kind}.xlsx"'
    wb.save(resp)
    return resp


# ----------------------------- KPI + дії боргів -----------------------------

@finance_access_required(api=True)
@require_POST
def metric_create_api(request):
    company = get_default_company()
    data = json.loads(request.body or '{}')
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'ok': False, 'error': 'Вкажіть назву показника'}, status=400)
    metric = FinancialMetric.objects.create(company=company, name=name,
                                             formula='revenue_minus_variable')
    inc = [i for i in (data.get('income_categories') or []) if str(i).isdigit()]
    exp = [i for i in (data.get('expense_categories') or []) if str(i).isdigit()]
    if inc:
        metric.income_categories.set(company.categories.filter(id__in=inc))
    if exp:
        metric.expense_categories.set(company.categories.filter(id__in=exp))
    return JsonResponse({'ok': True, 'id': metric.id})


@finance_access_required(api=True)
@require_POST
def debt_settle_api(request, txn_id):
    """Погасити планову операцію (дебіторка/кредиторка) → стає фактичною."""
    from ..services import transactions as txn_service
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    txn_service.mark_planned_actual(txn, user=request.user)
    return JsonResponse({'ok': True})
