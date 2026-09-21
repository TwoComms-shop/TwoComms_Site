"""Розділ «Автоправила»: список, конструктор, застосування до історії."""
from __future__ import annotations

import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from ..models import AutomationRule, Transaction, get_default_company
from ..permissions import finance_access_required
from ..services import rules_engine
from ..services import serializers as ser


def _body(request):
    if request.content_type and 'application/json' in request.content_type:
        try:
            return json.loads(request.body or '{}')
        except (ValueError, TypeError):
            return {}
    return request.POST


@finance_access_required
def rules(request):
    company = get_default_company()
    rule_rows = []
    for r in company.automation_rules.all().order_by('priority', 'id'):
        rule_payload = {
            'id': r.id,
            'name': r.name,
            'transaction_type': r.transaction_type,
            'priority': r.priority,
            'is_enabled': r.is_enabled,
            'conditions': r.conditions or [],
            'actions': r.actions or [],
        }
        rule_rows.append({
            'id': r.id, 'name': r.name, 'is_enabled': r.is_enabled,
            'transaction_type': r.transaction_type, 'priority': r.priority,
            'conditions_count': len(r.conditions or []),
            'actions_count': len(r.actions or []),
            'applied_count': r.applied_count,
            'conditions': r.conditions, 'actions': r.actions,
            'rule_json': json.dumps(rule_payload, ensure_ascii=False, separators=(',', ':')),
        })
    return render(request, 'finance/rules.html', {
        'active_tab': 'rules',
        'rules': rule_rows,
        'dropdowns': ser.serialize_dropdowns(company),
        'fields': rules_engine.FIELD_CHOICES,
        'operators': rules_engine.OPERATOR_CHOICES,
        'actions': rules_engine.ACTION_CHOICES,
    })


@finance_access_required(api=True)
@require_POST
def rule_save_api(request, rule_id=None):
    company = get_default_company()
    data = _body(request)
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'ok': False, 'error': 'Вкажіть назву правила'}, status=400)
    if rule_id:
        rule = get_object_or_404(AutomationRule, id=rule_id, company=company)
    else:
        rule = AutomationRule(company=company)
    rule.name = name
    rule.transaction_type = data.get('transaction_type', '') or ''
    rule.is_enabled = bool(data.get('is_enabled', True))
    rule.priority = int(data.get('priority') or 100)
    rule.conditions = data.get('conditions') or []
    rule.actions = data.get('actions') or []
    rule.apply_to_existing = bool(data.get('apply_to_existing'))
    rule.save()

    applied = 0
    if rule.apply_to_existing:
        qs = Transaction.objects.filter(company=company)
        applied = rules_engine.apply_to_existing(rule, qs, user=request.user)
    return JsonResponse({'ok': True, 'id': rule.id, 'applied': applied})


@finance_access_required(api=True)
@require_POST
def rule_toggle_api(request, rule_id):
    company = get_default_company()
    rule = get_object_or_404(AutomationRule, id=rule_id, company=company)
    rule.is_enabled = not rule.is_enabled
    rule.save(update_fields=['is_enabled'])
    return JsonResponse({'ok': True, 'is_enabled': rule.is_enabled})


@finance_access_required(api=True)
@require_POST
def rule_delete_api(request, rule_id):
    company = get_default_company()
    rule = get_object_or_404(AutomationRule, id=rule_id, company=company)
    rule.delete()
    return JsonResponse({'ok': True})


@finance_access_required(api=True)
@require_POST
def rule_preview_api(request, rule_id):
    company = get_default_company()
    rule = get_object_or_404(AutomationRule, id=rule_id, company=company)
    qs = Transaction.objects.filter(company=company)
    preview = rules_engine.preview_apply_to_existing(rule, qs)
    return JsonResponse({'ok': True, 'count': len(preview), 'preview': preview})


@finance_access_required(api=True)
@require_POST
def rule_apply_api(request, rule_id):
    company = get_default_company()
    rule = get_object_or_404(AutomationRule, id=rule_id, company=company)
    qs = Transaction.objects.filter(company=company)
    count = rules_engine.apply_to_existing(rule, qs, user=request.user)
    return JsonResponse({'ok': True, 'applied': count})
