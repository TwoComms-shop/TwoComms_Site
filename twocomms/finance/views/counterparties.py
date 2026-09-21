"""Розділ «Контрагенти»: список зі статистикою, детальна сторінка, CRUD.

Контрагенти — клієнти, постачальники, співробітники, держоргани. Тут бізнес
бачить усіх контрагентів, керує ними (створення/редагування/видалення),
вносить реквізити й контакти, дивиться статистику співпраці, історію операцій
та журнал дій по кожному.
"""
from __future__ import annotations

import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from ..models import Category, Counterparty, CounterpartyClassificationPolicy, Transaction, get_default_company
from ..permissions import finance_access_required
from ..services import audit as audit_service
from ..services import counterparty as cp_service
from ..services import serializers as ser


def _body(request):
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body or '{}')
        except (ValueError, TypeError):
            return {}
    return request.POST


# ----------------------------- Сторінки -----------------------------

@finance_access_required
def counterparties(request):
    company = get_default_company()
    search = (request.GET.get('q') or '').strip()
    type_filter = (request.GET.get('type') or '').strip()
    sort = (request.GET.get('sort') or 'turnover').strip()

    data = cp_service.counterparties_overview(
        company, search=search, type_filter=type_filter, sort=sort)

    return render(request, 'finance/counterparties/list.html', {
        'active_tab': 'counterparties',
        'rows': data['rows'],
        'count': data['count'],
        'totals': data['totals'],
        'types': data['types'],
        'q': search,
        'type_filter': type_filter,
        'sort': sort,
        'dropdowns': ser.serialize_dropdowns(company),
        'v2_accounts': [
            {'id': a.id, 'name': a.name, 'balance': str(a.current_balance), 'currency': a.currency}
            for a in company.accounts.filter(is_active=True, is_archived=False).order_by('sort_order', 'id')
        ],
    })


@finance_access_required
def counterparty_detail_page(request, counterparty_id):
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    data = cp_service.counterparty_detail(company, cp)
    return render(request, 'finance/counterparties/detail.html', {
        'active_tab': 'counterparties',
        'cp': cp,
        'cp_type_display': cp.get_type_display(),
        'contacts': cp.contacts or {},
        'data': data,
        'totals': data['totals'],
        'stats': data['stats'],
        'accounts': data['accounts'],
        'resellers': data['resellers'],
        'audit': data['audit'],
        'categories': data['categories'],
        'transactions': data['transactions'],
        'actual_transactions': data['actual_transactions'],
        'obligations': data['obligations'],
        'cards': data['cards'],
        'chart_data': json.dumps({
            'monthly': data['monthly'],
            'categories': [{'name': c['name'], 'turnover': c['turnover'],
                            'color': c['color']} for c in data['categories'][:8]],
        }),
        'types': Counterparty.TYPE_CHOICES,
        'initials': cp_service._initials(cp.name),
        'color': cp_service._color_for(cp.name),
        'dropdowns': ser.serialize_dropdowns(company),
        'classification_policy': CounterpartyClassificationPolicy.objects.filter(
            company=company, counterparty=cp,
        ).select_related('category').first(),
    })


# ----------------------------- CRUD API -----------------------------

@finance_access_required(api=True)
@require_POST
def counterparty_create_api(request):
    company = get_default_company()
    data = _body(request)
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'ok': False, 'error': 'Вкажіть назву контрагента'}, status=400)

    cp = Counterparty.objects.create(
        company=company,
        name=name,
        type=data.get('type') or 'client',
        group=(data.get('group') or '').strip(),
        edrpou=(data.get('edrpou') or '').strip(),
        iban=(data.get('iban') or '').strip(),
        country=(data.get('country') or '').strip(),
        address=(data.get('address') or '').strip(),
        contacts=_parse_contacts(data.get('contacts')),
    )
    audit_service.log_action(
        request.user, 'create', 'counterparty', cp.id,
        summary=f'Створено контрагента «{cp.name}»', company=company)
    return JsonResponse({'ok': True, 'id': cp.id, 'name': cp.name})


@finance_access_required(api=True)
@require_GET
def counterparty_get_api(request, counterparty_id):
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    return JsonResponse({
        'ok': True,
        'counterparty': {
            'id': cp.id, 'name': cp.name, 'type': cp.type,
            'group': cp.group or '', 'edrpou': cp.edrpou or '',
            'iban': cp.iban or '', 'country': cp.country or '',
            'address': cp.address or '', 'contacts': cp.contacts or {},
        },
    })


@finance_access_required(api=True)
@require_POST
def counterparty_update_api(request, counterparty_id):
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    data = _body(request)
    before = {'name': cp.name, 'type': cp.type, 'group': cp.group}

    if 'name' in data:
        name = (data.get('name') or '').strip()
        if not name:
            return JsonResponse({'ok': False, 'error': 'Назва не може бути порожньою'}, status=400)
        cp.name = name
    if 'type' in data:
        cp.type = data.get('type') or cp.type
    if 'group' in data:
        cp.group = (data.get('group') or '').strip()
    if 'edrpou' in data:
        cp.edrpou = (data.get('edrpou') or '').strip()
    if 'iban' in data:
        cp.iban = (data.get('iban') or '').strip()
    if 'country' in data:
        cp.country = (data.get('country') or '').strip()
    if 'address' in data:
        cp.address = (data.get('address') or '').strip()
    if 'contacts' in data:
        cp.contacts = _parse_contacts(data.get('contacts'))
    cp.save()

    audit_service.log_action(
        request.user, 'update', 'counterparty', cp.id,
        summary=f'Оновлено контрагента «{cp.name}»',
        before=before, after={'name': cp.name, 'type': cp.type, 'group': cp.group},
        company=company)
    return JsonResponse({'ok': True})


def _policy_row(policy):
    if policy is None:
        return None
    return {
        'id': policy.id, 'is_enabled': policy.is_enabled,
        'transaction_type': policy.transaction_type or '',
        'category_id': policy.category_id,
        'category': policy.category.name if policy.category_id else '',
        'economic_kind': policy.economic_kind or 'unknown',
        'ownership_scope': policy.ownership_scope or 'unknown',
        'require_confirmation': policy.require_confirmation,
        'prompt': policy.prompt or '',
    }


@finance_access_required(api=True)
@require_GET
def counterparty_policy_get_api(request, counterparty_id):
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    policy = CounterpartyClassificationPolicy.objects.filter(
        company=company, counterparty=cp,
    ).select_related('category').first()
    return JsonResponse({'ok': True, 'policy': _policy_row(policy)})


@finance_access_required(api=True)
@require_POST
def counterparty_policy_save_api(request, counterparty_id):
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    data = _body(request)
    transaction_type = (data.get('transaction_type') or '').strip()
    if transaction_type not in ('', Transaction.TYPE_INCOME, Transaction.TYPE_EXPENSE):
        return JsonResponse({'ok': False, 'error': 'Оберіть напрямок операції'}, status=400)
    category = None
    if data.get('category_id'):
        category = Category.objects.filter(company=company, id=data.get('category_id'), is_active=True).first()
        if category is None:
            return JsonResponse({'ok': False, 'error': 'Категорію не знайдено'}, status=400)
        if transaction_type and category.type not in (Category.TYPE_BOTH, transaction_type):
            return JsonResponse({'ok': False, 'error': 'Категорія не відповідає напрямку операції'}, status=400)
    enabled = str(data.get('is_enabled', '')).lower() in ('1', 'true', 'on', 'yes')
    if enabled and category is None:
        return JsonResponse({'ok': False, 'error': 'Оберіть рекомендовану категорію'}, status=400)
    economic_kind = (data.get('economic_kind') or 'unknown').strip()
    valid_kinds = {key for key, _label in Transaction.ECONOMIC_KIND_CHOICES}
    if economic_kind not in valid_kinds:
        return JsonResponse({'ok': False, 'error': 'Оберіть коректний вид операції'}, status=400)
    ownership_scope = (data.get('ownership_scope') or 'unknown').strip()
    if ownership_scope not in {'business', 'personal', 'mixed', 'unknown'}:
        return JsonResponse({'ok': False, 'error': 'Оберіть коректний контур обліку'}, status=400)
    policy, _created = CounterpartyClassificationPolicy.objects.get_or_create(company=company, counterparty=cp)
    policy.is_enabled = enabled
    policy.transaction_type = transaction_type
    policy.category = category
    policy.economic_kind = economic_kind
    policy.ownership_scope = ownership_scope
    policy.require_confirmation = str(data.get('require_confirmation', '')).lower() in ('1', 'true', 'on', 'yes')
    policy.prompt = (data.get('prompt') or '').strip()[:255]
    policy.save()
    audit_service.log_action(
        request.user, 'update', 'counterparty', cp.id,
        summary=f'Оновлено політику класифікації для «{cp.name}»', company=company,
    )
    return JsonResponse({'ok': True, 'policy': _policy_row(policy)})


@finance_access_required(api=True)
@require_POST
def counterparty_delete_api(request, counterparty_id):
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    data = _body(request)
    force = str(data.get('force', '')).lower() in ('1', 'true', 'on', 'yes')

    txn_count = Transaction.objects.filter(company=company, counterparty=cp).count()
    if txn_count and not force:
        return JsonResponse({
            'ok': False,
            'needs_force': True,
            'error': f'У контрагента {txn_count} операцій. Видалення відв\'яже їх '
                     f'(операції залишаться без контрагента). Підтвердіть видалення.',
        }, status=400)

    name = cp.name
    # SET_NULL на Transaction.counterparty відв'яже операції автоматично.
    cp.delete()
    audit_service.log_action(
        request.user, 'delete', 'counterparty', counterparty_id,
        summary=f'Видалено контрагента «{name}»', company=company)
    return JsonResponse({'ok': True})


def _parse_contacts(raw):
    """Нормалізує контакти у dict {'phone','email','telegram','responsible','note'}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        return {}
    allowed = ('phone', 'email', 'telegram', 'responsible', 'website', 'note')
    return {k: str(raw.get(k, '')).strip() for k in allowed if raw.get(k)}


# ----------------------------- Картки контрагента -----------------------------

@finance_access_required(api=True)
@require_GET
def counterparty_cards_api(request, counterparty_id):
    """Список карток контрагента (полоски у профілі)."""
    from ..services import cards as cards_service
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    return JsonResponse({'ok': True, 'cards': cards_service.cards_for(cp)})


@finance_access_required(api=True)
@require_POST
def counterparty_card_save_api(request, counterparty_id):
    """Додати/оновити картку контрагента вручну (банк, маска/останні 4, IBAN, мітка)."""
    from ..services import cards as cards_service
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    data = _body(request)
    pan_mask = (data.get('pan_mask') or '').strip()
    last4 = (data.get('last4') or '').strip()
    iban = (data.get('iban') or '').strip()
    if not (pan_mask or last4 or iban):
        return JsonResponse({'ok': False, 'error': 'Вкажіть номер картки або IBAN'}, status=400)
    card = cards_service.upsert_card(
        cp, pan_mask=pan_mask, last4=last4, iban=iban,
        bank=(data.get('bank') or '').strip(), label=(data.get('label') or '').strip(),
        make_primary=str(data.get('is_primary', '')).lower() in ('1', 'true', 'on', 'yes'))
    audit_service.log_action(request.user, 'update', 'counterparty', cp.id,
                             summary=f'Картка контрагента «{cp.name}»', company=company)
    return JsonResponse({'ok': True, 'id': card.id})


@finance_access_required(api=True)
@require_POST
def counterparty_card_delete_api(request, counterparty_id, card_id):
    """Видалити картку контрагента."""
    from ..models import CounterpartyCard
    company = get_default_company()
    cp = get_object_or_404(Counterparty, id=counterparty_id, company=company)
    CounterpartyCard.objects.filter(id=card_id, counterparty=cp, company=company).delete()
    return JsonResponse({'ok': True})
