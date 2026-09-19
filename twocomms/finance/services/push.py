"""Відправка web-push повідомлень фінансового кабінету.

Переюзає VAPID-конфіг проєкту (WEB_PUSH_VAPID_*), що вже налаштований для
основного сайту. Підписки зберігаються у finance.PushSubscription, логи —
у finance.NotificationLog. Звіти будуються з рушія здоров'я та P&L.

Кожне повідомлення:
  • логуються ДО відправки (щоб id потрапив у payload → клік відкриває
    конкретний звіт у модалці на сайті);
  • має ``dedup_key`` (один субʼєкт за період) — рознос дедуплікації з cron,
    щоб не було спаму навіть за невдалої доставки;
  • несе багаті ``report_data`` для красивого модального звіту.

Усе деградує безпечно: якщо pywebpush недоступний або VAPID не налаштований,
функції повертають структуру з помилкою, не валлячи виклик.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from importlib import import_module

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

try:
    from pywebpush import WebPushException, webpush
except ModuleNotFoundError:
    WebPushException = RuntimeError
    webpush = None

ZERO = Decimal('0')

# Дні тижня українською (Python weekday(): 0=Пн).
_WEEKDAYS_UK = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']
_MONTHS_UK_GEN = ['', 'січня', 'лютого', 'березня', 'квітня', 'травня', 'червня',
                  'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня']


def is_configured() -> bool:
    return bool(getattr(settings, 'WEB_PUSH_ENABLED', False) and webpush is not None)


def has_active_subscription(user) -> bool:
    from ..models_settings import PushSubscription
    return PushSubscription.objects.filter(user=user, is_active=True).exists()


def _resolve_private_key(private_key):
    """Підтримка як raw-ключа, так і PEM (як у storefront)."""
    normalized = (private_key or '').strip()
    if not normalized:
        return None
    if '-----BEGIN' not in normalized:
        return normalized
    try:
        vapid_module = import_module('py_vapid')
    except ModuleNotFoundError:
        return None
    vapid_class = getattr(vapid_module, 'Vapid01', None) or getattr(vapid_module, 'Vapid', None)
    if vapid_class is None:
        return None
    try:
        return vapid_class.from_pem(normalized.encode('utf-8'))
    except Exception:
        return None


def _vapid_claims():
    subject = (getattr(settings, 'WEB_PUSH_VAPID_SUBJECT', '') or '').strip()
    return {'sub': subject} if subject else {'sub': 'mailto:admin@twocomms.shop'}


def _abs(path):
    base = (getattr(settings, 'SITE_BASE_URL', '') or '').rstrip('/')
    if not path:
        return base
    if path.startswith(('http://', 'https://')):
        return path
    return f'{base}/{path.lstrip("/")}'


def _with_report_param(url: str, report_id: int) -> str:
    """Додає ?fin_report=<id> до URL, щоб сторінка відкрила модалку звіту."""
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}fin_report={report_id}'


# Стандартні кнопки під повідомленням: відкрити звіт + «Ознайомився».
_DEFAULT_ACTIONS = [
    {'action': 'open', 'title': '📄 Відкрити звіт'},
    {'action': 'ack', 'title': '✓ Ознайомився'},
]


def send_to_user(user, title: str, body: str, *, url: str = '/', tag: str = 'finance',
                 notification_type: str = 'custom', report_data=None,
                 dedup_key: str = '', actions=None, require_interaction: bool = False) -> dict:
    """Надсилає push усім активним підпискам користувача. Повертає підсумок.

    Лог створюється ДО відправки, щоб його id потрапив у payload (клік по
    повідомленню відкриє саме цей звіт). ``dedup_key`` зберігається у лог —
    cron перевіряє існування запису, тож повтор не відправляється.
    """
    from ..models_settings import PushSubscription, NotificationLog

    if not is_configured():
        return {'ok': False, 'error': 'web_push_not_configured', 'sent': 0, 'failed': 0}

    private_key = _resolve_private_key(getattr(settings, 'WEB_PUSH_VAPID_PRIVATE_KEY', ''))
    if private_key is None:
        return {'ok': False, 'error': 'vapid_private_key_invalid', 'sent': 0, 'failed': 0}

    # 1) Лог наперед — щоб мати id для deep-link у модалку.
    log = NotificationLog.objects.create(
        user=user, notification_type=notification_type, title=title, body=body,
        success=False, dedup_key=dedup_key or '', report_data=report_data)

    subscriptions = PushSubscription.objects.filter(user=user, is_active=True)
    icon = _abs(getattr(settings, 'WEB_PUSH_ICON_PATH', '/static/img/favicon-192x192.png'))
    deep_url = _with_report_param(url, log.id)
    payload = json.dumps({
        'title': title,
        'body': body,
        'icon': icon,
        'badge': icon,
        'url': deep_url,
        'report_id': log.id,
        'tag': tag,
        'requireInteraction': require_interaction,
        'actions': actions if actions is not None else _DEFAULT_ACTIONS,
        'timestamp': int(timezone.now().timestamp() * 1000),
    })

    sent = 0
    failed = 0
    last_error = ''
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=payload,
                vapid_private_key=private_key,
                vapid_claims=_vapid_claims(),
                ttl=86400,
                timeout=10,
            )
            sent += 1
            sub.last_used = timezone.now()
            sub.save(update_fields=['last_used'])
        except WebPushException as exc:
            failed += 1
            last_error = str(exc)[:255]
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (404, 410):
                sub.is_active = False
                sub.save(update_fields=['is_active'])
        except Exception as exc:
            failed += 1
            last_error = str(exc)[:255]

    # 2) Оновлюємо статус логу.
    try:
        log.success = (failed == 0 and sent > 0)
        log.error_message = last_error
        log.save(update_fields=['success', 'error_message'])
    except Exception:
        pass

    return {'ok': sent > 0, 'sent': sent, 'failed': failed,
            'log_id': log.id, 'error': last_error if failed else ''}


# --------------------------------------------------------------------------
# Дедуп-ключі
# --------------------------------------------------------------------------

def dedup_daily(day) -> str:
    return f'daily:{day.isoformat()}'


def dedup_debts(day) -> str:
    return f'debts:{day.isoformat()}'


def dedup_health(day) -> str:
    return f'health:{day.isoformat()}'


def dedup_planned(day) -> str:
    return f'planned:{day.isoformat()}'


def dedup_weekly(day) -> str:
    iso = day.isocalendar()
    return f'weekly:{iso[0]}-W{iso[1]:02d}'


# --------------------------------------------------------------------------
# Допоміжні розрахунки
# --------------------------------------------------------------------------

def _day_qs(company, day):
    """Фактичні операції за добу (без переказів та виключених)."""
    from ..models import Transaction
    from .timeutil import day_start, day_end
    return (Transaction.objects.filter(
                company=company, status=Transaction.STATUS_ACTUAL,
                date_actual__gte=day_start(day), date_actual__lte=day_end(day))
            .exclude(excluded_from_reports=True))


def _top_by_category(qs, ttype, currency, limit=3):
    from ..models import Transaction
    from . import serializers as ser
    rows = (qs.filter(type=ttype).values('category__name')
            .annotate(s=Sum('amount_base')).order_by('-s')[:limit])
    out = []
    for r in rows:
        amt = r['s'] or ZERO
        if amt <= 0:
            continue
        out.append({'category': r['category__name'] or 'Без категорії',
                    'amount': float(amt), 'amount_display': ser.money(amt, currency)})
    return out


def _debts_paid_today(company, day, currency):
    """Борги, погашені саме сьогодні (евристика: actual з контрагентом і
    ознакою боргу — інвойс/реалізатор/повторення/джерело інвойс/повторення)."""
    from ..models import Transaction
    from . import serializers as ser
    qs = _day_qs(company, day).exclude(type=Transaction.TYPE_TRANSFER).filter(
        counterparty__isnull=False).filter(
        _q_debt_like())
    result = {}
    for key, ttype in (('receivable', Transaction.TYPE_INCOME),
                       ('payable', Transaction.TYPE_EXPENSE)):
        sub = qs.filter(type=ttype).select_related('counterparty')[:20]
        items = []
        total = ZERO
        for t in sub:
            amt = t.amount_base or t.amount or ZERO
            total += amt
            items.append({'name': t.counterparty.name if t.counterparty else '—',
                          'amount_display': ser.money(amt, currency)})
        result[key] = {'count': len(items), 'sum': float(total),
                       'sum_display': ser.money(total, currency), 'items': items}
    return result


def _q_debt_like():
    """Q-фільтр «схоже на погашення боргу»."""
    from django.db.models import Q
    return (Q(linked_invoice__isnull=False) | Q(reseller__isnull=False)
            | Q(recurrence_rule__isnull=False)
            | Q(source__in=['invoice', 'recurring']))


def _unpaid_debts_summary(company, currency):
    """Зведення непогашених боргів (дебіторка/кредиторка) з найближчим/простроченим."""
    from . import reports_debt
    today = timezone.localdate()

    def _side(data):
        rows = data['rows']
        overdue = [r for r in rows if r.get('overdue')]
        overdue_sum = sum((Decimal(str(r['amount'])) for r in overdue), ZERO)
        nearest = None
        # Найбільш «гарячий»: спершу прострочені (за давністю), потім найближчі.
        def _days(r):
            if not r.get('date'):
                return 10 ** 6
            try:
                d = dt.date.fromisoformat(r['date'])
            except ValueError:
                return 10 ** 6
            return (d - today).days
        if rows:
            srt = sorted(rows, key=lambda r: (_days(r),))
            r0 = srt[0]
            days = _days(r0)
            nearest = {
                'name': r0.get('counterparty') or '—',
                'amount_display': _fmt(r0['amount'], currency),
                'days': days if days < 10 ** 6 else None,
                'overdue': bool(r0.get('overdue')),
            }
        from . import serializers as ser
        return {
            'total': float(data['total']), 'total_display': ser.money(data['total'], currency),
            'count': len(rows), 'overdue_count': len(overdue),
            'overdue_sum_display': ser.money(overdue_sum, currency),
            'nearest': nearest,
            'rows': [{
                'name': r.get('counterparty') or '—',
                'amount_display': _fmt(r['amount'], currency),
                'date': r.get('date') or '',
                'days': _days(r) if r.get('date') else None,
                'overdue': bool(r.get('overdue')),
            } for r in rows[:12]],
        }

    recv = _side(reports_debt.receivables(company))
    pay = _side(reports_debt.payables(company))
    return {'receivable': recv, 'payable': pay}


def _fmt(amount, currency):
    from . import serializers as ser
    return ser.money(Decimal(str(amount)), currency)


def _human_days(days) -> str:
    if days is None:
        return ''
    if days < 0:
        n = abs(days)
        return f'прострочено {n} дн.'
    if days == 0:
        return 'сьогодні'
    if days == 1:
        return 'завтра'
    return f'через {days} дн.'


# --------------------------------------------------------------------------
# Контент звітів
# --------------------------------------------------------------------------

def build_daily_report(company) -> dict:
    """Щоденний звіт: повна зведення за день + борги + поради."""
    from ..models import Transaction
    from . import balances as balance_service
    from . import serializers as ser

    day = timezone.localdate()
    cur = company.base_currency
    qs = _day_qs(company, day)
    flow = qs.exclude(type=Transaction.TYPE_TRANSFER)
    inc = flow.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount_base'))['s'] or ZERO
    exp = flow.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount_base'))['s'] or ZERO
    profit = inc - exp
    ops_count = flow.count()
    transfers_count = qs.filter(type=Transaction.TYPE_TRANSFER).count()
    balance = balance_service.total_actual_balance(company)

    top_expenses = _top_by_category(flow, Transaction.TYPE_EXPENSE, cur)
    top_income = _top_by_category(flow, Transaction.TYPE_INCOME, cur)
    paid = _debts_paid_today(company, day, cur)
    unpaid = _unpaid_debts_summary(company, cur)
    tips = _daily_tips(inc, exp, profit, ops_count, unpaid)

    # Заголовок/тіло пушу.
    title = f'Фінанси за {day.day} {_MONTHS_UK_GEN[day.month]}'
    if ops_count == 0 and transfers_count == 0:
        body = f'Тихий день — операцій не було. Баланс: {ser.money(balance, cur)}.'
    else:
        chunks = []
        if inc:
            chunks.append(f'дохід {ser.money(inc, cur)}')
        if exp:
            chunks.append(f'витрати {ser.money(exp, cur)}')
        if transfers_count and not chunks:
            chunks.append(f'{transfers_count} переказ(ів)')
        prefix = '; '.join(chunks).capitalize() if chunks else 'Рух коштів'
        body = f'{prefix}. Заробіток {ser.money(profit, cur, signed=True)}, баланс {ser.money(balance, cur)}.'

    data = {
        'kind': 'daily',
        'date': day.isoformat(),
        'date_label': f'{day.day} {_MONTHS_UK_GEN[day.month]} {day.year}',
        'currency': ser.currency_symbol(cur),
        'income': float(inc), 'income_display': ser.money(inc, cur),
        'expense': float(exp), 'expense_display': ser.money(exp, cur),
        'profit': float(profit), 'profit_display': ser.money(profit, cur, signed=True),
        'ops_count': ops_count, 'transfers_count': transfers_count,
        'balance': float(balance), 'balance_display': ser.money(balance, cur),
        'top_expenses': top_expenses, 'top_income': top_income,
        'debts_paid_today': paid,
        'debts_unpaid': unpaid,
        'tips': tips,
    }
    return {'title': title, 'body': body, 'data': data}


def _daily_tips(inc, exp, profit, ops_count, unpaid) -> list:
    tips = []
    if profit < 0:
        tips.append('Сьогодні витрати перевищили дохід. Перевірте найбільші статті '
                    'та чи всі вони обовʼязкові.')
    rec_overdue = unpaid['receivable']['overdue_count']
    pay_overdue = unpaid['payable']['overdue_count']
    if rec_overdue:
        tips.append(f'{rec_overdue} прострочен(их) надходжень — нагадайте контрагентам '
                    'про оплату, щоб не заморожувати оборот.')
    if pay_overdue:
        tips.append(f'{pay_overdue} прострочен(их) ваших платежів — погасьте, щоб '
                    'уникнути штрафів і зберегти репутацію.')
    if ops_count == 0:
        tips.append('Записуйте навіть дрібні готівкові витрати — інакше звіт неповний, '
                    'а рішення приймаються «наосліп».')
    if not tips:
        tips.append('Тримайте «подушку» ≥1 місяця обовʼязкових витрат — це головний '
                    'захист від касового розриву.')
    return tips[:3]


def build_weekly_report(company) -> dict:
    """Тижневий звіт: розбивка по днях (для графіка) + метрики + здоровʼя."""
    from ..models import Transaction
    from . import serializers as ser
    from . import health as health_service

    today = timezone.localdate()
    start = today - dt.timedelta(days=6)  # 7 днів включно з сьогодні
    cur = company.base_currency

    days = []
    week_inc = ZERO
    week_exp = ZERO
    best_day = None
    for i in range(7):
        d = start + dt.timedelta(days=i)
        dq = _day_qs(company, d).exclude(type=Transaction.TYPE_TRANSFER).filter(is_business=True)
        di = dq.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount_base'))['s'] or ZERO
        de = dq.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount_base'))['s'] or ZERO
        week_inc += di
        week_exp += de
        dp = di - de
        days.append({'label': _WEEKDAYS_UK[d.weekday()], 'date': d.isoformat(),
                     'income': float(di), 'expense': float(de), 'profit': float(dp)})
        if best_day is None or dp > best_day['profit']:
            best_day = {'label': _WEEKDAYS_UK[d.weekday()], 'profit': float(dp)}

    profit = week_inc - week_exp
    avg_exp = week_exp / 7 if week_exp else ZERO
    margin = (profit / week_inc * 100) if week_inc else ZERO

    # Топ-категорії витрат за тиждень.
    from .timeutil import day_start, day_end
    period_qs = (Transaction.objects.filter(
                    company=company, status=Transaction.STATUS_ACTUAL, is_business=True,
                    date_actual__gte=day_start(start), date_actual__lte=day_end(today))
                 .exclude(excluded_from_reports=True).exclude(type=Transaction.TYPE_TRANSFER))
    top_categories = []
    cat_rows = (period_qs.filter(type=Transaction.TYPE_EXPENSE).values('category__name')
                .annotate(s=Sum('amount_base')).order_by('-s')[:5])
    for r in cat_rows:
        amt = r['s'] or ZERO
        if amt <= 0:
            continue
        pct = float(amt / week_exp * 100) if week_exp else 0.0
        top_categories.append({'name': r['category__name'] or 'Без категорії',
                               'amount_display': ser.money(amt, cur), 'pct': round(pct)})

    health = health_service.calculate_health(company)
    score = health['scores']['portfolio_final']
    label = health['scores'].get('label', '')

    metrics = [
        {'label': 'Дохід за тиждень', 'value_display': ser.money(week_inc, cur), 'hint': 'усі бізнес-надходження'},
        {'label': 'Витрати за тиждень', 'value_display': ser.money(week_exp, cur), 'hint': 'усі бізнес-витрати'},
        {'label': 'Прибуток', 'value_display': ser.money(profit, cur, signed=True), 'hint': 'дохід − витрати'},
        {'label': 'Маржа', 'value_display': f'{round(float(margin))}%', 'hint': 'частка прибутку в доході'},
        {'label': 'Середні витрати/день', 'value_display': ser.money(avg_exp, cur), 'hint': 'рівномірність трат'},
        {'label': 'Найкращий день', 'value_display': (best_day['label'] if best_day else '—'),
         'hint': 'найбільший прибуток'},
    ]
    tips = _weekly_tips(profit, margin, score, health)

    period_label = f'{start.day}–{today.day} {_MONTHS_UK_GEN[today.month]}'
    title = 'Підсумок тижня'
    body = (f'Дохід {ser.money(week_inc, cur)}, прибуток {ser.money(profit, cur, signed=True)}, '
            f'маржа {round(float(margin))}%. Здоровʼя: {score}/100.')
    data = {
        'kind': 'weekly',
        'period_label': period_label,
        'currency': ser.currency_symbol(cur),
        'income': float(week_inc), 'income_display': ser.money(week_inc, cur),
        'expense': float(week_exp), 'expense_display': ser.money(week_exp, cur),
        'profit': float(profit), 'profit_display': ser.money(profit, cur, signed=True),
        'days': days,
        'top_categories': top_categories,
        'health_score': score, 'health_label': label,
        'metrics': metrics,
        'tips': tips,
    }
    return {'title': title, 'body': body, 'data': data}


def _weekly_tips(profit, margin, score, health) -> list:
    tips = []
    if profit < 0:
        tips.append('Тиждень закрито у мінус. Зафіксуйте 1–2 найбільші статті витрат '
                    'і сплануйте, як скоротити їх наступного тижня.')
    elif float(margin) < 10:
        tips.append('Маржа нижча за 10% — закладено замало запасу. Перегляньте ціни '
                    'або собівартість ключових позицій.')
    # З рекомендацій рушія здоровʼя — найвагоміша.
    for rec in (health.get('recommendations') or [])[:1]:
        act = rec.get('action')
        if isinstance(act, (list, tuple)) and act:
            tips.append(f'{rec.get("title", "Порада")}: {act[0]}')
        elif isinstance(act, str) and act:
            tips.append(act)
    if not tips:
        tips.append('Плануйте наступний тиждень: рознесіть великі виплати після '
                    'очікуваних надходжень, щоб уникнути касового розриву.')
    return tips[:3]


def build_debts_report(company) -> dict | None:
    """Щоденне зведення по боргах (08:00). Лише непогашені; погашені зникають."""
    from . import serializers as ser
    cur = company.base_currency
    unpaid = _unpaid_debts_summary(company, cur)
    recv = unpaid['receivable']
    pay = unpaid['payable']
    if recv['count'] == 0 and pay['count'] == 0:
        return None  # боргів немає — не турбуємо

    day = timezone.localdate()
    tips = []
    if recv['overdue_count']:
        tips.append(f'Прострочена дебіторка: {recv["overdue_sum_display"]}. Нагадайте '
                    'контрагентам — це ваші заморожені гроші.')
    if pay['overdue_count']:
        tips.append(f'Прострочена кредиторка: {pay["overdue_sum_display"]}. Сплануйте '
                    'погашення, щоб уникнути штрафів.')
    if not tips:
        tips.append('Боргів без прострочення — добре. Тримайте календар платежів під '
                    'контролем, щоб не пропустити дати.')

    title = '💼 Борги: зведення на сьогодні'
    parts = []
    if recv['count']:
        parts.append(f'вам винні {recv["total_display"]}')
    if pay['count']:
        parts.append(f'ви винні {pay["total_display"]}')
    body = '; '.join(parts).capitalize() + '.'

    data = {
        'kind': 'debts',
        'date_label': f'{day.day} {_MONTHS_UK_GEN[day.month]} {day.year}',
        'currency': ser.currency_symbol(cur),
        'receivable': recv,
        'payable': pay,
        'tips': tips[:3],
    }
    return {'title': title, 'body': body, 'data': data}


def build_health_alert_report(company) -> dict | None:
    """Звіт-сповіщення про критичні ризики (лише якщо є critical alerts)."""
    from . import health as health_service
    health = health_service.calculate_health(company)
    alerts = [a for a in health['critical_alerts'] if a['severity'] in ('critical', 'high')]
    if not alerts:
        return None
    top = alerts[0]
    extra = f' (+{len(alerts) - 1} ще)' if len(alerts) > 1 else ''
    return {'title': f'⚠️ {top["title"]}',
            'body': f'{top["message"]}{extra}',
            'data': {
                'kind': 'health',
                'score': health['scores']['portfolio_final'],
                'label': health['scores'].get('label', ''),
                'alerts': [{'title': a['title'], 'message': a['message'],
                            'severity': a['severity']} for a in alerts],
                'tips': [r.get('action')[0] if isinstance(r.get('action'), (list, tuple)) and r.get('action')
                         else r.get('action') for r in (health.get('recommendations') or [])[:3]
                         if r.get('action')],
            }}


def build_planned_reminder_report(company) -> dict | None:
    """Нагадування про планові платежі на завтра (та прострочені сьогодні)."""
    from ..models import Transaction
    from . import serializers as ser
    from .timeutil import day_start, day_end

    today = timezone.localdate()
    tomorrow = today + dt.timedelta(days=1)
    qs = (Transaction.objects.filter(
            company=company, status=Transaction.STATUS_PLANNED,
            date_actual__gte=day_start(today), date_actual__lte=day_end(tomorrow))
          .exclude(excluded_from_reports=True))
    if not qs.exists():
        return None
    cur = company.base_currency
    inc = qs.filter(type=Transaction.TYPE_INCOME).aggregate(s=Sum('amount_base'))['s'] or ZERO
    exp = qs.filter(type=Transaction.TYPE_EXPENSE).aggregate(s=Sum('amount_base'))['s'] or ZERO
    count = qs.count()
    parts = []
    if exp:
        parts.append(f'до сплати {ser.money(exp, cur)}')
    if inc:
        parts.append(f'надходжень {ser.money(inc, cur)}')
    body = f'{count} планов(их) платеж(ів) на найближчу добу: {", ".join(parts) or "перевірте календар"}.'
    return {'title': 'Нагадування про платежі', 'body': body,
            'data': {'kind': 'planned', 'count': count,
                     'income': float(inc), 'expense': float(exp),
                     'income_display': ser.money(inc, cur),
                     'expense_display': ser.money(exp, cur)}}


def build_ledger_alert_report(company) -> dict | None:
    """Actionable v2 alerts kept separate from the legacy health score."""
    from ..models import ClassificationReview, PaymentIntent, Transaction
    from . import obligations_v2

    actual = company.transactions.filter(status=Transaction.STATUS_ACTUAL)
    unclassified = actual.filter(economic_kind='unknown')
    pending_reviews = ClassificationReview.objects.filter(company=company, status='pending').count()
    stalled_intents = PaymentIntent.objects.filter(
        company=company, status__in=['awaiting_confirmation', 'submitted', 'detected'],
    ).count()
    overdue = []
    for group in company.obligation_groups.filter(is_active=True).prefetch_related('components'):
        if obligations_v2.group_summary(group)['status'] == 'overdue':
            overdue.append(group.title)
    parts = []
    if overdue:
        parts.append(f'просрочено: {", ".join(overdue[:3])}')
    if unclassified.exists():
        parts.append(f'не классифицировано: {unclassified.count()} операций')
    if pending_reviews:
        parts.append(f'на подтверждение: {pending_reviews}')
    if stalled_intents:
        parts.append(f'ожидают банк: {stalled_intents}')
    if not parts:
        return None
    return {
        'title': 'Нужно внимание к учету',
        'body': '; '.join(parts),
        'data': {'kind': 'ledger_alert', 'overdue_groups': overdue,
                 'unclassified_count': unclassified.count(),
                 'pending_reviews': pending_reviews, 'stalled_intents': stalled_intents},
    }
