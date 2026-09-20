"""Розділ «Платежі»: журнал операцій + JSON-API для модалок і таблиці."""
from __future__ import annotations

import json
from decimal import Decimal

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from ..models import (
    Account, Category, Counterparty, Project, Tag, Transaction, get_default_company,
)
from ..permissions import finance_access_required
from ..services import filters as filter_service
from ..services import payloads as payload_service
from ..services import serializers as ser
from ..services import transactions as txn_service


def _body(request):
    """POST-дані з form-urlencoded або JSON."""
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body or '{}')
        except (ValueError, TypeError):
            return {}
    return request.POST


_MAX_ATTACHMENT = 10 * 1024 * 1024  # 10 МБ


def _attach_files(request, txn):
    """Створює Attachment з request.FILES і прив'язує до операції (макс. 10 МБ)."""
    from ..models import Attachment
    files = request.FILES.getlist('attachments') or request.FILES.getlist('file')
    company = get_default_company()
    for f in files:
        if f.size > _MAX_ATTACHMENT:
            continue
        att = Attachment.objects.create(
            company=company, file=f, original_name=f.name[:255],
            content_type=getattr(f, 'content_type', '') or '', size=f.size,
            uploaded_by=request.user if request.user.is_authenticated else None,
        )
        txn.attachments.add(att)


# ----------------------------- Журнал -----------------------------

_PER_PAGE_OPTIONS = [10, 25, 50, 100]
_PER_PAGE_DEFAULT = 10


def _resolve_per_page(raw):
    """Скільки операцій показувати на сторінці. 'all' → без розбивки, інакше зі списку."""
    if str(raw).lower() == 'all':
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return _PER_PAGE_DEFAULT
    return val if val in _PER_PAGE_OPTIONS else _PER_PAGE_DEFAULT


@finance_access_required
def payments(request):
    from django.core.paginator import Paginator

    company = get_default_company()
    qs = filter_service.filter_transactions(company, request.GET)
    actual_qs, planned_qs = filter_service.split_actual_planned(qs)

    total_count = actual_qs.count()
    per_page = _resolve_per_page(request.GET.get('per_page', _PER_PAGE_DEFAULT))

    # Пагінація фактичних операцій (планові — окрема згорнута секція, не діляться).
    page_obj = None
    if per_page is None:
        page_items = list(actual_qs[:2000])
        page_start = 1 if page_items else 0
        page_end = len(page_items)
    else:
        paginator = Paginator(actual_qs, per_page)
        page_obj = paginator.get_page(request.GET.get('page') or 1)
        page_items = list(page_obj.object_list)
        page_start = page_obj.start_index()
        page_end = page_obj.end_index()

    # Компактний список сторінок для пагінатора: 1 … (n-1) n (n+1) … last.
    # None означає роздільник «…». Рахуємо у в'ю, щоб шаблон лишався простим.
    page_range_display = []
    if page_obj and page_obj.paginator.num_pages > 1:
        last = page_obj.paginator.num_pages
        cur = page_obj.number
        wanted = {1, last, cur, cur - 1, cur + 1}
        shown = sorted(n for n in wanted if 1 <= n <= last)
        prev = 0
        for n in shown:
            if n - prev > 1:
                page_range_display.append(None)
            page_range_display.append(n)
            prev = n

    actual_rows = [ser.serialize_transaction(t) for t in page_items]

    # Бейджі «погашення зобов'язання» (кредиторка/дебіторка) для рядків журналу.
    # Пріоритет: явні ObligationSettlement (точна привʼязка, мультимісяць). Інакше
    # повторюваний ФАКТИЧНИЙ платіж = виконання планового зобовʼязання (напр.
    # «Повернення за оренду» від Віктора) — теж позначаємо, з його місяцем.
    _settle_map = ser.settlement_labels_for(company, [r['id'] for r in actual_rows])
    _by_id = {t.id: t for t in page_items}
    for r in actual_rows:
        s = _settle_map.get(r['id'])
        if s:
            r['settled'] = True
            r['settled_title'] = s['title']
            r['settled_period'] = s['period_label']
            r['settled_periods'] = s['periods']
        elif r.get('recurrence_rule_id') and not r['is_planned']:
            t = _by_id.get(r['id'])
            r['settled'] = True
            r['settled_title'] = r.get('recurrence_title') or 'план'
            r['settled_period'] = (timezone.localtime(t.date_actual).strftime('%m.%Y')
                                   if t and t.date_actual else '')
            r['settled_periods'] = 1

    # Скільки фільтрів реально застосовано — для бейджа на згорнутій кнопці «Фільтри».
    gp = request.GET
    active_filter_count = 0
    if gp.get('period') and gp.get('period') != 'all':
        active_filter_count += 1
    for _k in ('search', 'amount_min', 'amount_max', 'scope', 'mcc_group',
               'date_from', 'date_to', 'accounts', 'categories', 'counterparties',
               'tags', 'types', 'tab'):
        if gp.get(_k):
            active_filter_count += 1

    # Планові згортаємо в один рядок на серію (повторюване правило / розстрочка
    # магазину), щоб не дублювати «платіж 1/6, 2/6…». Показуємо найближчий
    # екземпляр із позначкою періодичності та лічильником решти серії.
    from ..services import obligations as obligations_service
    planned_total = planned_qs.count()

    # Таймлайн планових для нижнього блоку: прострочено → цей місяць → майбутні
    # по місяцях (зрозуміла структура замість хаотичного плоского списку).
    timeline = obligations_service.planned_timeline(company)
    cur0 = company.base_currency

    def _fmt_obl(g):
        g['per_amount_display'] = ser.money(g['per_amount'], g.get('currency') or cur0)
        if g.get('remaining_amount') is not None:
            g['remaining_amount_display'] = ser.money(g['remaining_amount'], g.get('currency') or cur0)
        else:
            g['remaining_amount_display'] = ''
        return g

    planned_timeline = []
    v2_groups = list(company.obligation_groups.filter(is_active=True).only('id', 'title'))

    def _attach_v2_group(g):
        title = (g.get('title') or '').lower()
        match = None
        if 'подат' in title or 'налог' in title:
            match = next((item for item in v2_groups if 'налог' in item.title.lower()), None)
        elif 'комун' in title or 'кварт' in title or 'влад' in title:
            match = next((item for item in v2_groups if 'кварт' in item.title.lower()), None)
        elif 'офис' in title or 'оренд' in title or 'аренд' in title:
            match = next((item for item in v2_groups if 'офис' in item.title.lower()), None)
        if match and g.get('type') == Transaction.TYPE_EXPENSE:
            g['v2_group_id'] = match.id
            g['v2_group_title'] = match.title
        return g

    for seg in timeline['segments']:
        planned_timeline.append({
            'key': seg['key'], 'label': seg['label'], 'tone': seg['tone'],
            'items': [_attach_v2_group(_fmt_obl(g)) for g in seg['items']],
            'count': len(seg['items']),
            'income_sum': ser.money(seg['income_sum'], cur0),
            'expense_sum': ser.money(seg['expense_sum'], cur0),
            'has_income': seg['income_sum'] > 0,
            'has_expense': seg['expense_sum'] > 0,
        })

    context = {
        'active_tab': 'payments',
        'rows': actual_rows,
        'planned_timeline': planned_timeline,
        'planned_count': planned_total,
        'total_count': total_count,
        'page_obj': page_obj,
        'page_range_display': page_range_display,
        'page_start': page_start,
        'page_end': page_end,
        'per_page': 'all' if per_page is None else per_page,
        'per_page_options': _PER_PAGE_OPTIONS,
        'active_filter_count': active_filter_count,
        'dropdowns': ser.serialize_dropdowns(company),
        'v2_accounts': [
            {'id': a.id, 'name': a.name, 'balance': str(a.current_balance), 'currency': a.currency}
            for a in company.accounts.filter(is_active=True, is_archived=False).order_by('sort_order', 'id')
        ],
        'current_filters': {
            'period': request.GET.get('period', 'all'),
            'search': request.GET.get('search', ''),
            'amount_min': request.GET.get('amount_min', ''),
            'amount_max': request.GET.get('amount_max', ''),
            'scope': request.GET.get('scope', ''),
            'mcc_group': request.GET.get('mcc_group', ''),
        },
    }
    return render(request, 'finance/payments.html', context)


@finance_access_required(api=True)
@require_GET
def dropdowns_api(request):
    company = get_default_company()
    return JsonResponse({'ok': True, **ser.serialize_dropdowns(company)})


# ----------------------------- Експорт відфільтрованих платежів -----------------------------

def _export_rows(company, params):
    """Список рядків (dict) поточної відфільтрованої вибірки журналу."""
    qs = filter_service.filter_transactions(company, params)
    rows = []
    for t in qs.select_related('account', 'to_account', 'category', 'counterparty', 'project')[:10000]:
        rows.append({
            'date': timezone.localtime(t.date_actual).strftime('%Y-%m-%d %H:%M') if t.date_actual else '',
            'type': t.get_type_display(),
            'status': t.get_status_display(),
            'amount': str(t.amount),
            'currency': t.currency,
            'account': t.account.name if t.account else '',
            'to_account': t.to_account.name if t.to_account else '',
            'category': t.category.name if t.category else '',
            'counterparty': t.counterparty.name if t.counterparty else '',
            'project': t.project.name if t.project else '',
            'comment': t.comment or '',
        })
    return rows


_EXPORT_COLS = [
    ('date', 'Дата'), ('type', 'Тип'), ('status', 'Статус'), ('amount', 'Сума'),
    ('currency', 'Валюта'), ('account', 'Рахунок'), ('to_account', 'На рахунок'),
    ('category', 'Категорія'), ('counterparty', 'Контрагент'), ('project', 'Проект'),
    ('comment', 'Коментар'),
]


@finance_access_required
@require_GET
def payments_export(request):
    """Експорт поточного відфільтрованого журналу у XLSX або XML (?format=)."""
    company = get_default_company()
    fmt = (request.GET.get('format') or 'xlsx').lower()
    rows = _export_rows(company, request.GET)
    stamp = timezone.localtime(timezone.now()).strftime('%Y%m%d_%H%M')

    if fmt == 'xml':
        import xml.etree.ElementTree as ET
        from xml.dom import minidom
        root = ET.Element('payments', {'exported': stamp, 'count': str(len(rows))})
        for r in rows:
            el = ET.SubElement(root, 'payment')
            for key, _label in _EXPORT_COLS:
                child = ET.SubElement(el, key)
                child.text = str(r.get(key, ''))
        raw = ET.tostring(root, encoding='utf-8')
        pretty = minidom.parseString(raw).toprettyxml(indent='  ', encoding='utf-8')
        resp = HttpResponse(pretty, content_type='application/xml; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="payments_{stamp}.xml"'
        return resp

    # XLSX за замовчуванням.
    try:
        from openpyxl import Workbook
    except ImportError:
        return JsonResponse({'ok': False, 'error': 'openpyxl недоступний'}, status=500)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Платежі'
    ws.append([label for _key, label in _EXPORT_COLS])
    for r in rows:
        ws.append([(float(r['amount']) if _key == 'amount' and r['amount'] else r.get(_key, ''))
                   for _key, _label in _EXPORT_COLS])
    resp = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = f'attachment; filename="payments_{stamp}.xlsx"'
    wb.save(resp)
    return resp


# ----------------------------- CRUD операцій -----------------------------

@finance_access_required(api=True)
@require_POST
def transaction_create_api(request):
    data = _body(request)
    txn_type = data.get('type')
    if txn_type not in (Transaction.TYPE_INCOME, Transaction.TYPE_EXPENSE, Transaction.TYPE_TRANSFER):
        return JsonResponse({'ok': False, 'error': 'Невірний тип операції'}, status=400)
    try:
        kwargs = payload_service.parse_transaction_payload(data, txn_type=txn_type)
        recurrence = (payload_service.parse_recurrence_payload(data)
                      if txn_type != Transaction.TYPE_TRANSFER else None)
    except payload_service.PayloadError as e:
        return JsonResponse({'ok': False, 'error': str(e), 'field': e.field}, status=400)

    txn = txn_service.create_transaction(user=request.user, **kwargs)
    if request.FILES:
        _attach_files(request, txn)
    # Повторюваний платіж: робимо транзакцію першим екземпляром правила і
    # матеріалізуємо наступні наперед (у журналі групуються в один рядок).
    if recurrence:
        from ..services import recurring as recurring_service
        recurring_service.create_rule_from_transaction(txn, user=request.user, **recurrence)
    return JsonResponse({'ok': True, 'transaction': ser.serialize_transaction(txn)})


@finance_access_required(api=True)
@require_GET
def transaction_detail_api(request, txn_id):
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    return JsonResponse({'ok': True, 'transaction': ser.serialize_transaction(txn)})


@finance_access_required(api=True)
@require_POST
def transaction_update_api(request, txn_id):
    from ..services import recurring as recurring_service
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    data = _body(request)
    txn_type = data.get('type', txn.type)
    try:
        kwargs = payload_service.parse_transaction_payload(data, txn_type=txn_type)
        recurrence = (payload_service.parse_recurrence_payload(data)
                      if txn_type != Transaction.TYPE_TRANSFER else None)
    except payload_service.PayloadError as e:
        return JsonResponse({'ok': False, 'error': str(e), 'field': e.field}, status=400)

    tags = kwargs.pop('tags', None)
    txn_service.update_transaction(txn, user=request.user, tags=tags, **kwargs)
    if request.FILES:
        _attach_files(request, txn)

    # Повторюваність при РЕДАГУВАННІ (раніше ігнорувалась — «Зробити повторюваним»
    # на наявному платежі не спрацьовував). Вмикання, оновлення або вимикання:
    if recurrence:
        if txn.recurrence_rule_id:
            # Уже повторюваний → оновити графік/назву наявного правила.
            # ВАЖЛИВО (інваріант стабільності оцінки): сума шаблону (template_amount)
            # НЕ змінюється автоматично з txn.amount. Інакше разове редагування
            # суми одного екземпляра (або повернення нетипової суми) ламало б план
            # на всі наступні місяці й аналітику (кейс «13000 → 17000»). Плановану
            # суму міняємо ЛИШЕ якщо явно передано plan_amount (модалка «Редагувати
            # план»).
            plan_amount = None
            raw_plan_amount = data.get('plan_amount')
            if raw_plan_amount not in (None, ''):
                try:
                    plan_amount = payload_service._decimal(raw_plan_amount, 'plan_amount')
                except payload_service.PayloadError as e:
                    return JsonResponse({'ok': False, 'error': str(e), 'field': e.field}, status=400)
            recurring_service.update_plan(
                txn.recurrence_rule, user=request.user,
                amount=plan_amount, amount_is_estimated=recurrence['amount_is_estimated'],
                title=(recurrence['title'] or None),
                frequency=recurrence['frequency'], interval=recurrence['interval'],
                end_mode=recurrence['end_mode'], end_date=recurrence['end_date'],
                count=recurrence['count'],
            )
        else:
            # Перетворюємо разовий платіж на повторюване зобов'язання.
            recurring_service.create_rule_from_transaction(txn, user=request.user, **recurrence)
    # Вимкнення повторення тут НЕ робимо автоматично (щоб редагування суми не
    # знищило правило) — для цього є кнопка «Зупинити повторення» в «Планових».

    # Операцію могли перебудувати (re-materialize) — повертаємо свіжий стан.
    txn = Transaction.objects.filter(id=txn_id, company=company).first()
    if txn is None:
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': True, 'transaction': ser.serialize_transaction(txn)})


@finance_access_required(api=True)
@require_POST
def transaction_delete_api(request, txn_id):
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    txn_service.delete_transaction(txn, user=request.user)
    return JsonResponse({'ok': True})


@finance_access_required(api=True)
@require_POST
def transaction_duplicate_api(request, txn_id):
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    new = txn_service.duplicate_transaction(txn, user=request.user)
    return JsonResponse({'ok': True, 'transaction': ser.serialize_transaction(new)})


@finance_access_required(api=True)
@require_POST
def transaction_convert_transfer_api(request, txn_id):
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    data = _body(request)
    to_account = company.accounts.filter(id=data.get('to_account')).first()
    if not to_account:
        return JsonResponse({'ok': False, 'error': 'Оберіть рахунок отримувача'}, status=400)
    if to_account.id == txn.account_id:
        return JsonResponse({'ok': False, 'error': 'Рахунки мають відрізнятися'}, status=400)
    txn_service.convert_to_transfer(txn, user=request.user, to_account=to_account,
                                    to_amount=data.get('to_amount'))
    return JsonResponse({'ok': True, 'transaction': ser.serialize_transaction(txn)})


@finance_access_required(api=True)
@require_POST
def transaction_split_api(request, txn_id):
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    data = _body(request)
    raw_parts = data.get('parts') or []
    if isinstance(raw_parts, str):
        try:
            raw_parts = json.loads(raw_parts)
        except ValueError:
            raw_parts = []
    parts = []
    for p in raw_parts:
        parts.append({
            'amount': p.get('amount'),
            'category': company.categories.filter(id=p.get('category')).first() if p.get('category') else None,
            'project': company.projects.filter(id=p.get('project')).first() if p.get('project') else None,
            'comment': p.get('comment', ''),
        })
    try:
        children = txn_service.split_transaction(txn, user=request.user, parts=parts)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    return JsonResponse({'ok': True, 'children': [ser.serialize_transaction(c) for c in children]})


@finance_access_required(api=True)
@require_POST
def transaction_mark_actual_api(request, txn_id):
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    txn_service.mark_planned_actual(txn, user=request.user)
    return JsonResponse({'ok': True, 'transaction': ser.serialize_transaction(txn)})


# ----------------------------- Погашення планових + повторення -----------------------------

@finance_access_required(api=True)
@require_POST
def transaction_settle_api(request, txn_id):
    """Погасити/провести планову операцію з вибором рахунку та контрагента.

    Дохід → рахунок, КУДИ надійшли кошти; витрата → рахунок, З ЯКОГО списано.
    Контрагент фіксує, з ким операція; за згодою рахунок прив'язується до
    контрагента. Якщо операція повторювана — наступний екземпляр підтягується.
    """
    from ..services import recurring as recurring_service
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company)
    data = _body(request)

    account = None
    if data.get('account_id'):
        account = company.accounts.filter(id=data.get('account_id')).first()
        if account is None:
            return JsonResponse({'ok': False, 'error': 'Рахунок не знайдено'}, status=400)
    counterparty = None
    if data.get('counterparty_id'):
        counterparty = company.counterparties.filter(id=data.get('counterparty_id')).first()

    date_actual = None
    if data.get('date'):
        date_actual = payload_service._parse_dt(str(data.get('date')))

    amount = None
    if data.get('amount') not in (None, ''):
        try:
            amount = payload_service._decimal(data.get('amount'), 'amount')
        except payload_service.PayloadError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)
        if amount is not None and amount <= 0:
            return JsonResponse({'ok': False, 'error': 'Сума має бути більшою за 0'}, status=400)

    recurring_service.settle_occurrence(
        txn, user=request.user, account=account, counterparty=counterparty,
        date_actual=date_actual, amount=amount,
        link_account_cp=str(data.get('link_account_cp', '')).lower() in ('1', 'true', 'on', 'yes'),
    )
    return JsonResponse({'ok': True, 'transaction': ser.serialize_transaction(txn)})


@finance_access_required(api=True)
@require_POST
def recurrence_update_api(request, rule_id):
    """Редагувати план повторюваного зобов'язання (напр. комуналка виросла).

    Зміни переносяться на майбутні (ще не проведені) екземпляри; історія
    фактичних платежів лишається без змін.
    """
    from ..models import RecurrenceRule
    from ..services import recurring as recurring_service
    company = get_default_company()
    rule = get_object_or_404(RecurrenceRule, id=rule_id, company=company)
    data = _body(request)

    fields = {}
    if data.get('amount') not in (None, ''):
        try:
            fields['amount'] = payload_service._decimal(data.get('amount'), 'amount')
        except payload_service.PayloadError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    if 'amount_is_estimated' in data:
        fields['amount_is_estimated'] = (str(data.get('amount_is_estimated')).lower()
                                         in ('1', 'true', 'on', 'yes'))
    if 'title' in data:
        fields['title'] = data.get('title') or ''
    if data.get('category_id'):
        fields['category'] = company.categories.filter(id=data.get('category_id')).first()
    if data.get('counterparty_id'):
        fields['counterparty'] = company.counterparties.filter(id=data.get('counterparty_id')).first()
    if data.get('account_id'):
        fields['account'] = company.accounts.filter(id=data.get('account_id')).first()
    if data.get('frequency'):
        fields['frequency'] = data.get('frequency')
    if data.get('interval'):
        fields['interval'] = data.get('interval')
    if data.get('end_mode'):
        fields['end_mode'] = data.get('end_mode')
        if data.get('end_mode') == RecurrenceRule.END_UNTIL and data.get('end_date'):
            import datetime as _dt
            try:
                fields['end_date'] = _dt.date.fromisoformat(str(data.get('end_date'))[:10])
            except (ValueError, TypeError):
                pass
        if data.get('end_mode') == RecurrenceRule.END_COUNT and data.get('count'):
            fields['count'] = data.get('count')

    recurring_service.update_plan(rule, user=request.user, **fields)
    return JsonResponse({'ok': True})


@finance_access_required(api=True)
@require_POST
def recurrence_stop_api(request, rule_id):
    """Зупинити повторення: деактивувати правило та прибрати майбутні планові."""
    from ..models import RecurrenceRule
    from ..services import recurring as recurring_service
    company = get_default_company()
    rule = get_object_or_404(RecurrenceRule, id=rule_id, company=company)
    data = _body(request)
    delete_future = str(data.get('delete_future', '1')).lower() in ('1', 'true', 'on', 'yes')
    recurring_service.stop_rule(rule, user=request.user, delete_future=delete_future)
    return JsonResponse({'ok': True})


# ----------------------------- Масові дії -----------------------------

@finance_access_required(api=True)
@require_POST
def transactions_bulk_api(request):
    company = get_default_company()
    data = _body(request)
    ids = data.get('ids') or []
    if isinstance(ids, str):
        ids = [i for i in ids.split(',') if i.strip().isdigit()]
    action = data.get('action')
    qs = Transaction.objects.filter(company=company, id__in=ids)
    if not qs.exists():
        return JsonResponse({'ok': False, 'error': 'Не обрано операцій'}, status=400)

    affected = 0
    if action == 'delete':
        for t in list(qs):
            txn_service.delete_transaction(t, user=request.user)
            affected += 1
    elif action == 'set_category':
        cat = company.categories.filter(id=data.get('value')).first()
        for t in qs:
            txn_service.update_transaction(t, user=request.user, category=cat)
            affected += 1
    elif action == 'set_project':
        proj = company.projects.filter(id=data.get('value')).first()
        for t in qs:
            txn_service.update_transaction(t, user=request.user, project=proj)
            affected += 1
    elif action == 'set_counterparty':
        cp = company.counterparties.filter(id=data.get('value')).first()
        for t in qs:
            txn_service.update_transaction(t, user=request.user, counterparty=cp)
            affected += 1
    elif action == 'add_tag':
        tag = company.tags.filter(id=data.get('value')).first()
        if tag:
            for t in qs:
                t.tags.add(tag)
                affected += 1
    elif action == 'mark_actual':
        for t in qs.filter(status=Transaction.STATUS_PLANNED):
            txn_service.mark_planned_actual(t, user=request.user)
            affected += 1
    elif action == 'set_business':
        # value приходить рядком '1'/'0' — bool('0') == True, тож парсимо явно.
        flag = str(data.get('value', '')).strip().lower() in ('1', 'true', 'on', 'yes')
        affected = qs.update(is_business=flag)
    else:
        return JsonResponse({'ok': False, 'error': 'Невідома дія'}, status=400)

    return JsonResponse({'ok': True, 'affected': affected})


# ----------------------------- Швидке створення сутностей -----------------------------

@finance_access_required(api=True)
@require_POST
def quick_create_entity_api(request):
    """Створення проєкту/контрагента/категорії/тегу з дропдауна (ТЗ 12 §11)."""
    company = get_default_company()
    data = _body(request)
    kind = data.get('kind')
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'ok': False, 'error': 'Вкажіть назву'}, status=400)

    if kind == 'project':
        obj = Project.objects.create(company=company, name=name)
    elif kind == 'counterparty':
        obj = Counterparty.objects.create(company=company, name=name,
                                          type=data.get('type', 'client'))
    elif kind == 'category':
        ctype = data.get('type', 'expense')
        obj = Category.objects.create(company=company, name=name, type=ctype)
    elif kind == 'tag':
        obj = Tag.objects.create(company=company, name=name)
    else:
        return JsonResponse({'ok': False, 'error': 'Невідомий тип'}, status=400)

    return JsonResponse({'ok': True, 'id': obj.id, 'name': obj.name})


# Горизонти прогнозу для перемикача планових платежів у сайдбарі.
_PLANNED_HORIZONS = {
    'week': (7, '7 дн'),
    'month': (30, '30 дн'),
    'quarter': (90, '90 дн'),
    'year': (365, 'рік'),
    'all': (3650, 'весь час'),
}


@finance_access_required(api=True)
@require_GET
def planned_totals_api(request):
    """Планові доходи/витрати + прогноз балансу за обраний горизонт (сайдбар).

    Включає прострочені (ще не проведені) планові платежі (date_from=None).
    Додатково повертає дату найближчого планового доходу/витрати.
    """
    from ..services import balances as balance_service

    company = get_default_company()
    period = request.GET.get('period', 'month')
    days, label = _PLANNED_HORIZONS.get(period, _PLANNED_HORIZONS['month'])

    today = timezone.localdate()
    horizon = today + timezone.timedelta(days=days)
    total = balance_service.total_actual_balance(company)
    planned = balance_service.planned_totals(company, None, horizon)
    forecast = total + planned['income'] + planned['expense']
    next_income, next_expense = _next_planned_dates(company, horizon)

    return JsonResponse({
        'ok': True,
        'period': period,
        'label': label,
        'income': ser.money(planned['income'], company.base_currency, signed=True),
        'expense': ser.money(planned['expense'], company.base_currency, signed=True),
        'forecast': ser.money(forecast, company.base_currency),
        'next_income_date': next_income,
        'next_expense_date': next_expense,
    })


def _next_planned_dates(company, horizon):
    """Дати найближчого планового доходу та витрати (рядок ДД.ММ або '')."""
    from django.utils import timezone as _tz
    from ..services.timeutil import day_end

    def _nearest(ttype):
        t = (Transaction.objects.filter(
                company=company, status=Transaction.STATUS_PLANNED, type=ttype,
                date_actual__lte=day_end(horizon))
             .exclude(excluded_from_reports=True)
             .order_by('date_actual').first())
        if t is None or not t.date_actual:
            return ''
        return _tz.localtime(t.date_actual).strftime('%d.%m')

    return _nearest(Transaction.TYPE_INCOME), _nearest(Transaction.TYPE_EXPENSE)


# ----------------------------- Обернений потік: переказ → у рахунок зобов'язання -----------------------------

@finance_access_required(api=True)
@require_GET
def payment_reverse_candidates_api(request, txn_id):
    """Для фактичного платежу — відкриті зобов'язання того ж контрагента/картки.

    Використовується після P2P-переказу: «Цей переказ у рахунок кредиторки?».
    """
    from ..services import payables as payables_service
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company,
                            status=Transaction.STATUS_ACTUAL)
    res = payables_service.reverse_link_candidates(company, txn)
    match = res['counterparty_match']
    return JsonResponse({
        'ok': True,
        'obligations': res['obligations'],
        'counterparty_strong': (
            {'id': match['strong'].id, 'name': match['strong'].name}
            if match['strong'] else None),
        'counterparty_candidates': [
            {'id': c.id, 'name': c.name} for c in match['candidates']],
        'match_reason': match['reason'],
    })


@finance_access_required(api=True)
@require_POST
def payment_attach_obligation_api(request, txn_id):
    """Прив'язати наявний фактичний платіж до зобов'язання (повне/часткове)."""
    from ..services import payables as payables_service
    company = get_default_company()
    txn = get_object_or_404(Transaction, id=txn_id, company=company,
                            status=Transaction.STATUS_ACTUAL)
    data = _body(request)
    planned = company.transactions.filter(
        id=data.get('planned_txn_id'), status=Transaction.STATUS_PLANNED).first()
    if planned is None:
        return JsonResponse({'ok': False, 'error': "Зобов'язання не знайдено"}, status=400)
    full_period = str(data.get('full_period', '1')).lower() in ('1', 'true', 'on', 'yes')
    remember_card = str(data.get('remember_card', '')).lower() in ('1', 'true', 'on', 'yes')
    card_hint = None
    if remember_card:
        from ..services import cards as cards_service
        card_hint = cards_service.extract_card_hint(txn)
    try:
        res = payables_service.attach_payment_to_obligation(
            user=request.user, payment_txn=txn, planned_txn=planned,
            full_period=full_period, remember_card=remember_card, card_hint=card_hint)
    except ValueError as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=400)
    return JsonResponse({'ok': True, 'full_period': res['full_period'],
                         'settlement_id': res['settlement'].id})
