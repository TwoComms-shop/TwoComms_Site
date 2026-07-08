"""
Cart views - Корзина покупок.

Содержит views для:
- Просмотра корзины
- Добавления товаров
- Обновления количества
- Удаления товаров
- Очистки корзины
- Применения промокодов
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.cache import never_cache
from django.contrib import messages
from django.utils.translation import gettext as _
from django_ratelimit.decorators import ratelimit
from decimal import Decimal, ROUND_HALF_UP
import logging
import json

from ..models import Product, PromoCode, CustomPrintLead, CustomPrintModerationStatus
from productcolors.models import ProductColorVariant
from accounts.models import UserProfile
from orders.nova_poshta_data import apply_nova_poshta_refs
from orders.nova_poshta_lookup import (
    NovaPoshtaDirectoryService,
    NovaPoshtaLookupError,
    NovaPoshtaLookupUnavailable,
)
from orders.nova_poshta_documents import normalize_checkout_phone, normalize_phone
from orders.nova_poshta_checkout import (
    NovaPoshtaSelectionError,
    resolve_delivery_selection,
    serialize_city_choice,
    serialize_warehouse_choice,
)
from storefront.custom_print_config import (
    ADDON_LABELS,
    FABRIC_LABELS,
    FIT_LABELS,
    PRODUCT_LABELS,
    SERVICE_LABELS,
    SESSION_CUSTOM_CART_KEY,
    TRIAGE_LABELS,
    ZONE_LABELS,
)
from storefront.custom_print_notifications import notify_custom_print_moderation_request
from storefront.services.size_guides import normalize_requested_size
from .utils import (
    get_cart_from_session,
    save_cart_to_session,
    calculate_cart_total,
    _reset_monobank_session,
    _color_label_from_variant,
)
from ..utm_tracking import record_add_to_cart, record_remove_from_cart, record_user_action

# Logger для корзины
cart_logger = logging.getLogger('storefront.cart')
monobank_logger = logging.getLogger('storefront.monobank')

# Константы статусов Monobank
MONOBANK_SUCCESS_STATUSES = {'success', 'hold'}
MONOBANK_PENDING_STATUSES = {'processing'}
MONOBANK_FAILURE_STATUSES = {'failure', 'expired', 'rejected', 'canceled', 'cancelled', 'reversed'}
CUSTOM_PRINT_SIZE_MODE_LABELS = {
    'single': 'Один розмір',
    'mixed': 'Мікс розмірів',
}

LOOKUP_RATE_LIMIT = '60/m'

# W1-13 (NEW-508): верхний предел количества одной позиции в корзине
MAX_CART_ITEM_QTY = 50


# ==================== CART VIEWS ====================


def _calculate_original_subtotal(cart):
    """
    Подсчитывает сумму корзины по базовым ценам (без скидок сайта).
    """
    if not cart:
        return Decimal('0')
    ids = [item.get('product_id') for item in cart.values() if item.get('product_id')]
    products = Product.objects.in_bulk(ids)
    total = Decimal('0')
    for item in cart.values():
        product = products.get(item.get('product_id'))
        if not product:
            continue
        try:
            qty = int(item.get('qty', 1))
        except (TypeError, ValueError):
            qty = 1
        total += Decimal(product.price) * qty
    return total


def _resolve_product_fit_payload(product, requested_code):
    """
    Resolve an editable product fit option for cart/order snapshots.

    The fit selector exists only for products with active ProductFitOption rows.
    We store both code and label: code keeps machine identity, label keeps the
    historical order display stable if an admin renames the option later.
    """
    try:
        options = list(
            product.fit_options
            .filter(is_active=True)
            .order_by('order', 'id')
            .only('code', 'label', 'is_default')
        )
    except Exception:
        options = []

    if not options:
        return '', ''

    requested = str(requested_code or '').strip().lower()
    selected = next((option for option in options if option.code == requested), None)
    if selected is None:
        selected = next((option for option in options if option.is_default), options[0])

    return selected.code, selected.label


def _fit_display_from_cart_item(item_data):
    code = str(item_data.get('fit_option_code') or item_data.get('fit') or '').strip()
    label = str(item_data.get('fit_option_label') or item_data.get('fit_label') or '').strip()
    return code, label or code


def _safe_decimal(value, default='0') -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except Exception:
        return Decimal(str(default))


def _unique_strings(values) -> list[str]:
    result = []
    for value in values or []:
        normalized = str(value or '').strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _format_custom_placement_descriptor(spec: dict) -> str:
    placement_key = spec.get('placement_key') or spec.get('zone')
    label = spec.get('label') or ZONE_LABELS.get(placement_key, placement_key or '—')
    parts = [label]
    if spec.get('size_preset'):
        parts.append(str(spec['size_preset']).upper())
    elif spec.get('zone') == 'sleeve':
        parts.append('Текст' if spec.get('mode') == 'full_text' else 'A6')
    if spec.get('text'):
        parts.append(str(spec['text']).strip())
    return ' · '.join(part for part in parts if part)


def _custom_print_status_label(status: str) -> str:
    return {
        CustomPrintModerationStatus.DRAFT: _('Передаємо менеджеру на перевірку'),
        CustomPrintModerationStatus.AWAITING_REVIEW: _('На перевірці менеджера'),
        CustomPrintModerationStatus.APPROVED: _('Погоджено менеджером'),
        CustomPrintModerationStatus.REJECTED: _('Відхилено менеджером'),
    }.get(status, _('На перевірці менеджера'))


def _promote_legacy_custom_draft(lead, session_item: dict | None) -> tuple[str, bool]:
    if not lead or lead.moderation_status != CustomPrintModerationStatus.DRAFT:
        return (lead.moderation_status if lead else CustomPrintModerationStatus.DRAFT), False
    if (lead.source or '') != 'custom_print_cart':
        return lead.moderation_status, False

    lead.ensure_moderation_token()
    lead.moderation_status = CustomPrintModerationStatus.AWAITING_REVIEW
    lead.reviewed_at = None
    lead.save(update_fields=['moderation_status', 'moderation_token', 'reviewed_at'])

    if isinstance(session_item, dict):
        session_item['moderation_status'] = CustomPrintModerationStatus.AWAITING_REVIEW

    try:
        notify_custom_print_moderation_request(lead)
    except Exception:
        cart_logger.exception('notify_custom_print_moderation_request failed for legacy draft lead %s', lead.pk)

    return CustomPrintModerationStatus.AWAITING_REVIEW, True


def _build_custom_cart_entry_payload(key: str, item: dict, lead=None) -> tuple[dict, dict]:
    snapshot = {}
    if lead and isinstance(getattr(lead, 'config_draft_json', None), dict):
        snapshot = lead.config_draft_json or {}

    product_payload = snapshot.get('product') or {}
    print_payload = snapshot.get('print') or {}
    artwork_payload = snapshot.get('artwork') or {}
    order_payload = snapshot.get('order') or {}

    placement_specs = item.get('placement_specs') or getattr(lead, 'placement_specs_json', None) or []
    placement_specs = [spec for spec in placement_specs if isinstance(spec, dict)]
    placement_descriptors = _unique_strings([_format_custom_placement_descriptor(spec) for spec in placement_specs])

    zone_labels = item.get('zone_labels') or []
    if not zone_labels:
        for spec in placement_specs:
            placement_key = spec.get('placement_key') or spec.get('zone')
            label = spec.get('label') or ZONE_LABELS.get(placement_key, placement_key or '')
            if label:
                zone_labels.append(label)
    if not zone_labels:
        zone_labels = [ZONE_LABELS.get(zone, zone) for zone in (item.get('zones') or print_payload.get('zones') or getattr(lead, 'placements', None) or [])]
    zone_labels = _unique_strings(zone_labels)

    size_breakdown = item.get('size_breakdown') or order_payload.get('size_breakdown') or {}
    size_parts = [f'{size}×{count}' for size, count in size_breakdown.items() if count]
    sizes_note = (item.get('sizes_note') or order_payload.get('sizes_note') or getattr(lead, 'sizes_note', '') or '').strip()
    size_breakdown_display = ', '.join(size_parts) or sizes_note

    gift_enabled = bool(item.get('gift_enabled'))
    gift_text = (item.get('gift_text') or '').strip()
    if not gift_enabled and isinstance(order_payload.get('gift'), dict):
        gift_enabled = bool((order_payload.get('gift') or {}).get('enabled'))
        gift_text = gift_text or ((order_payload.get('gift') or {}).get('text') or '').strip()
    elif not gift_enabled and order_payload.get('gift'):
        gift_enabled = True
        gift_text = gift_text or (order_payload.get('gift_text') or '').strip()

    quantity = item.get('quantity') or order_payload.get('quantity') or getattr(lead, 'quantity', 1) or 1
    try:
        quantity = max(int(quantity), 1)
    except (TypeError, ValueError):
        quantity = 1

    moderation_status = getattr(lead, 'moderation_status', '') or item.get('moderation_status') or CustomPrintModerationStatus.DRAFT
    if lead:
        moderation_status, _promoted = _promote_legacy_custom_draft(lead, item)
    is_draft = moderation_status == CustomPrintModerationStatus.DRAFT
    is_awaiting = moderation_status == CustomPrintModerationStatus.AWAITING_REVIEW
    is_approved = moderation_status == CustomPrintModerationStatus.APPROVED
    is_rejected = moderation_status == CustomPrintModerationStatus.REJECTED
    is_pending = is_draft or is_awaiting

    final_total = getattr(lead, 'final_price_value', None) if lead is not None else None
    final_total = _safe_decimal(final_total if final_total is not None else item.get('final_total'))
    unit_total = _safe_decimal(item.get('unit_total'))
    if unit_total <= 0:
        pricing_snapshot = getattr(lead, 'pricing_snapshot_json', None) if lead is not None else {}
        if isinstance(pricing_snapshot, dict):
            unit_total = _safe_decimal(pricing_snapshot.get('unit_total') or pricing_snapshot.get('base_price'))
    if unit_total <= 0 and quantity > 0 and final_total > 0:
        unit_total = (final_total / Decimal(quantity)).quantize(Decimal('0.01'))

    product_type = item.get('product_type') or product_payload.get('type') or getattr(lead, 'product_type', '')
    fit_value = item.get('fit') or product_payload.get('fit') or getattr(lead, 'fit', '') or ''
    fabric_value = item.get('fabric') or product_payload.get('fabric') or getattr(lead, 'fabric', '') or ''
    color_value = item.get('color') or product_payload.get('color') or getattr(lead, 'color_choice', '') or ''
    mode_value = item.get('mode') or snapshot.get('mode') or getattr(lead, 'client_kind', '') or 'personal'
    service_kind = item.get('service_kind') or artwork_payload.get('service_kind') or getattr(lead, 'service_kind', '') or ''
    file_triage_status = item.get('file_triage_status') or artwork_payload.get('triage_status') or getattr(lead, 'file_triage_status', '') or ''
    placement_note = (item.get('placement_note') or print_payload.get('placement_note') or getattr(lead, 'placement_note', '') or '').strip()
    add_on_values = _unique_strings(item.get('add_ons') or print_payload.get('add_ons') or [])
    add_on_labels = _unique_strings(item.get('add_on_labels') or [ADDON_LABELS.get(value, value) for value in add_on_values])

    payload = {
        'key': key,
        'lead_id': item.get('lead_id'),
        'lead_number': item.get('lead_number') or getattr(lead, 'lead_number', '') or '',
        'label': item.get('label') or _('Кастомний виріб'),
        'product_type': product_type,
        'product_label': item.get('product_label') or (getattr(lead, 'pricing_snapshot_json', {}) or {}).get('product_label') or PRODUCT_LABELS.get(product_type, ''),
        'placements_display': ', '.join(placement_descriptors or zone_labels),
        'zones_display': ', '.join(zone_labels),
        'quantity': quantity,
        'size_mode': item.get('size_mode') or order_payload.get('size_mode') or getattr(lead, 'size_mode', '') or 'single',
        'size_mode_label': CUSTOM_PRINT_SIZE_MODE_LABELS.get(item.get('size_mode') or order_payload.get('size_mode') or getattr(lead, 'size_mode', ''), ''),
        'size_breakdown_display': size_breakdown_display,
        'gift_enabled': gift_enabled,
        'gift_text': gift_text,
        'fit': fit_value,
        'fit_label': FIT_LABELS.get(fit_value, fit_value),
        'fabric': fabric_value,
        'fabric_label': FABRIC_LABELS.get(fabric_value, fabric_value),
        'color': color_value,
        'mode': mode_value,
        'b2b_discount_per_unit': item.get('b2b_discount_per_unit') or 0,
        'service_kind': service_kind,
        'service_kind_label': SERVICE_LABELS.get(service_kind, service_kind),
        'file_triage_status': file_triage_status,
        'file_triage_label': TRIAGE_LABELS.get(file_triage_status, file_triage_status),
        'placement_note': placement_note,
        'add_on_labels': add_on_labels,
        'unit_total': unit_total.quantize(Decimal('0.01')),
        'line_total': final_total.quantize(Decimal('0.01')),
        'final_total': final_total.quantize(Decimal('0.01')),
        'moderation_status': moderation_status,
        'moderation_status_label': _custom_print_status_label(moderation_status),
        'is_draft': is_draft,
        'is_awaiting_review': is_awaiting,
        'is_approved': is_approved,
        'is_rejected': is_rejected,
        'is_pending': is_pending,
        'manager_note': getattr(lead, 'manager_note', '') if lead is not None else '',
        'approved_price': getattr(lead, 'approved_price', None) if lead is not None else None,
        'included_in_payment': is_approved,
        'price_caption': _('Орієнтовна ціна') if is_pending else (_('Фінальна ціна') if is_approved else _('Ціна')),
        'payment_note': _('Не входить до оплати зараз') if is_pending else (_('Додано до рахунку') if is_approved else ''),
        'pending_price_note': _('Ціна узгоджується після модерації') if is_pending else '',
        'show_manager_contact': is_pending,
    }

    session_snapshot = {
        'lead_id': payload['lead_id'],
        'lead_number': payload['lead_number'],
        'label': payload['label'],
        'product_type': payload['product_type'],
        'product_label': payload['product_label'],
        'fit': fit_value,
        'fabric': fabric_value,
        'color': color_value,
        'zones': list(item.get('zones') or print_payload.get('zones') or getattr(lead, 'placements', None) or []),
        'zone_labels': zone_labels,
        'placement_specs': placement_specs,
        'quantity': quantity,
        'size_mode': payload['size_mode'],
        'size_breakdown': size_breakdown,
        'sizes_note': sizes_note,
        'gift_enabled': gift_enabled,
        'gift_text': gift_text,
        'unit_total': str(payload['unit_total']),
        'final_total': str(payload['final_total']),
        'mode': mode_value,
        'b2b_discount_per_unit': item.get('b2b_discount_per_unit') or 0,
        'service_kind': service_kind,
        'file_triage_status': file_triage_status,
        'add_ons': add_on_values,
        'add_on_labels': add_on_labels,
        'placement_note': placement_note,
        'moderation_status': moderation_status,
    }
    return payload, session_snapshot


def _collect_custom_cart_state(request) -> dict:
    custom_cart_raw = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
    if not isinstance(custom_cart_raw, dict):
        custom_cart_raw = {}

    leads_map = {}
    lead_ids = [item.get('lead_id') for item in custom_cart_raw.values() if isinstance(item, dict) and item.get('lead_id')]
    if lead_ids:
        leads_map = {lead.pk: lead for lead in CustomPrintLead.objects.filter(pk__in=lead_ids)}

    rejected_keys = []
    session_changed = False
    any_awaiting_review = False
    any_rejected = False
    any_pending_review = False
    has_draft_items = False
    has_custom_items = False
    all_approved = True
    custom_items_total = Decimal('0')
    approved_custom_total = Decimal('0')
    custom_items_qty = 0
    custom_items = []

    for key, item in list(custom_cart_raw.items()):
        if not isinstance(item, dict):
            rejected_keys.append(key)
            continue

        lead_id = item.get('lead_id')
        lead = leads_map.get(lead_id) if lead_id else None
        if lead is not None and lead.moderation_status == CustomPrintModerationStatus.REJECTED:
            rejected_keys.append(key)
            continue

        payload, session_snapshot = _build_custom_cart_entry_payload(key, item, lead=lead)
        if item != {**item, **session_snapshot}:
            merged = dict(item)
            merged.update(session_snapshot)
            custom_cart_raw[key] = merged
            session_changed = True

        custom_items.append(payload)
        custom_items_total += payload['final_total']
        custom_items_qty += payload['quantity']
        has_custom_items = True
        if payload['is_pending']:
            any_pending_review = True
        if payload['is_draft']:
            has_draft_items = True
        if payload['is_awaiting_review']:
            any_awaiting_review = True
        if payload['is_rejected']:
            any_rejected = True
        if payload['is_approved']:
            approved_custom_total += payload['final_total']
        else:
            all_approved = False

    if rejected_keys:
        for key in rejected_keys:
            custom_cart_raw.pop(key, None)
        session_changed = True

    if session_changed:
        request.session[SESSION_CUSTOM_CART_KEY] = custom_cart_raw
        request.session.modified = True

    return {
        'custom_items': custom_items,
        'custom_items_total': custom_items_total.quantize(Decimal('0.01')),
        'approved_custom_total': approved_custom_total.quantize(Decimal('0.01')),
        'custom_items_qty': custom_items_qty,
        'has_custom_items': has_custom_items,
        'any_pending_review': any_pending_review,
        'any_awaiting_review': any_awaiting_review,
        'any_rejected': any_rejected,
        'all_approved': all_approved if has_custom_items else False,
        'has_draft_items': has_draft_items,
        'rejected_removed': bool(rejected_keys),
    }


@never_cache
def view_cart(request):
    """
    Страница просмотра корзины.

    Context:
        cart_items: Список товаров в корзине с деталями
        subtotal: Сумма без скидки
        discount: Размер скидки (если применен промокод)
        total: Итоговая сумма
        grand_total: Итоговая сумма (алиас для total)
        total_points: Общее количество баллов за заказ
        promo_code: Примененный промокод (если есть)
        applied_promo: Алиас для promo_code
    """
    if request.method == 'POST':
        form_type = request.POST.get('form_type')
        if form_type == 'update_profile':
            is_autosave = request.POST.get('ajax_autosave') == '1'
            if request.user.is_authenticated:
                try:
                    profile = request.user.userprofile
                except UserProfile.DoesNotExist:
                    profile = UserProfile.objects.create(user=request.user)

                try:
                    delivery_selection = resolve_delivery_selection(request.POST)
                    profile.full_name = (request.POST.get('full_name') or '').strip()
                    raw_phone = request.POST.get('phone') or ''
                    profile.phone = normalize_checkout_phone(raw_phone)
                    if not profile.phone:
                        if is_autosave:
                            return JsonResponse({'ok': False, 'reason': 'phone'}, status=200)
                        messages.error(
                            request,
                            _('Вкажіть коректний український номер телефону. Можна без +380.')
                        )
                        return redirect('cart')
                    profile.city = delivery_selection.city
                    profile.np_office = delivery_selection.np_office
                    # Email опціонально: зберігаємо у профіль для майбутнього предзаповнення
                    posted_email = (request.POST.get('email') or '').strip()
                    if posted_email:
                        try:
                            from django.core.validators import validate_email as _validate_email
                            from django.core.exceptions import ValidationError as _ValidationError
                            try:
                                _validate_email(posted_email)
                                profile.email = posted_email
                            except _ValidationError:
                                pass
                        except Exception:
                            pass
                    apply_nova_poshta_refs(
                        profile,
                        {
                            'np_settlement_ref': delivery_selection.settlement_ref,
                            'np_city_ref': delivery_selection.city_ref,
                            'np_warehouse_ref': delivery_selection.warehouse_ref,
                        },
                    )

                    pay_type_raw = request.POST.get('pay_type')
                    if pay_type_raw:
                        # Импортируем функцию нормализации типа оплаты из старого views
                        try:
                            import storefront.views as old_views
                            if hasattr(old_views, '_normalize_pay_type'):
                                profile.pay_type = old_views._normalize_pay_type(pay_type_raw)
                            else:
                                # Fallback: если функция не найдена, используем значение напрямую или по умолчанию
                                profile.pay_type = pay_type_raw if pay_type_raw in ['online_full', 'prepay_200'] else 'online_full'
                        except Exception as e:
                            cart_logger.error('Error normalizing pay_type: %s', e)
                            # В случае ошибки не обновляем pay_type

                    profile.save()
                    if is_autosave:
                        return JsonResponse({'ok': True})
                    messages.success(request, _('Дані доставки успішно оновлено!'))
                except NovaPoshtaSelectionError as exc:
                    if is_autosave:
                        return JsonResponse({'ok': False, 'reason': 'delivery'}, status=200)
                    messages.error(request, _message_for_delivery_selection_error(exc))
                except Exception as e:
                    cart_logger.error('Error saving profile: %s', e, exc_info=True)
                    if is_autosave:
                        return JsonResponse({'ok': False, 'reason': 'error'}, status=200)
                    messages.error(request, _('П��милка при ��береженні даних. Спробуйте ще раз.'))
            else:
                if is_autosave:
                    return JsonResponse({'ok': False, 'reason': 'auth'}, status=200)
                messages.error(request, _('Будь ласка, увійдіть, щоб зберегти дані доставки.'))
        elif form_type == 'guest_order':
            from storefront import views as legacy_views
            return legacy_views.create_order(request)
        elif form_type == 'order_create':
            from storefront import views as legacy_views
            return legacy_views.order_create(request)

    cart = get_cart_from_session(request)
    cart_items = []
    subtotal = Decimal('0')
    original_subtotal = Decimal('0')
    total_points = 0
    content_ids = []
    contents = []
    total_quantity = 0

    # Optimization: Fetch all products at once to avoid N+1
    product_ids = [item_data.get('product_id') for item_data in cart.values() if item_data.get('product_id')]
    products_map = Product.objects.select_related('category').prefetch_related('color_variants__images').in_bulk(product_ids)

    # Optimization: Fetch all color variants at once
    color_variant_ids = [item_data.get('color_variant_id') for item_data in cart.values() if item_data.get('color_variant_id')]
    from productcolors.models import ProductColorVariant
    color_variants_map = ProductColorVariant.objects.select_related('color').in_bulk(color_variant_ids)

    for item_key, item_data in cart.items():
        try:
            product_id = item_data.get('product_id')
            # product = Product.objects.select_related('category').get(id=product_id) # OLD N+1
            product = products_map.get(int(product_id))

            if not product:
                continue

            # Цена всегда берется из Product (актуальная цена)
            price = product.final_price
            original_price = Decimal(product.price)
            qty = int(item_data.get('qty', 1))
            line_total = price * qty
            original_line_total = original_price * qty
            site_line_discount = original_line_total - line_total
            if site_line_discount < 0:
                site_line_discount = Decimal('0.00')

            # Информация о цвете из color_variant_id
            # color_variant = _get_color_variant_safe(item_data.get('color_variant_id')) # OLD N+1 risk
            color_variant_id = item_data.get('color_variant_id')
            color_variant = color_variants_map.get(int(color_variant_id)) if color_variant_id else None

            color_label = _color_label_from_variant(color_variant)
            size_value = normalize_requested_size(product, item_data.get('size'))
            fit_option_code, fit_option_label = _fit_display_from_cart_item(item_data)
            # color_variant_id = color_variant.id if color_variant else None # Already have it
            offer_id = product.get_offer_id(color_variant_id, size_value, color_name=color_label)
            content_ids.append(offer_id)
            contents.append({
                'id': offer_id,
                'quantity': qty,
                'item_price': float(price),
                'item_name': product.title,
                'item_category': product.category.name if getattr(product, 'category', None) else '',
                'brand': 'TwoComms'
            })
            total_quantity += qty

            # Считаем баллы за товар
            try:
                if getattr(product, 'points_reward', 0):
                    total_points += int(product.points_reward) * qty
            except Exception:
                pass

            cart_items.append({
                'key': item_key,
                'product': product,
                'price': price,  # Для совместимости
                'unit_price': price,  # Шаблон ожидает unit_price!
                'original_unit_price': original_price,
                'qty': qty,
                'line_total': line_total,
                'original_line_total': original_line_total,
                'site_discount_amount': site_line_discount,
                'size': size_value,
                'fit_option_code': fit_option_code,
                'fit_option_label': fit_option_label,
                'fit_label': fit_option_label,
                'color_variant': color_variant,
                'color_label': color_label,  # ДОБАВЛЕНО: для отображения цвета
                'offer_id': offer_id,
            })

            subtotal += line_total
            original_subtotal += original_line_total

        except Product.DoesNotExist:
            # Товар удален из БД - пропускаем
            continue

    # Проверяем промокод
    promo_code = None
    discount = Decimal('0')
    promo_code_id = request.session.get('promo_code_id')

    if promo_code_id:
        try:
            promo_code = PromoCode.objects.get(id=promo_code_id)
            if promo_code.can_be_used():
                discount = promo_code.calculate_discount(subtotal)
            else:
                # Промокод больше не валиден
                del request.session['promo_code_id']
                promo_code = None
        except PromoCode.DoesNotExist:
            del request.session['promo_code_id']

    total = subtotal - discount
    site_discount_total = (original_subtotal - subtotal).quantize(Decimal('0.01')) if original_subtotal >= subtotal else Decimal('0.00')
    subtotal = subtotal.quantize(Decimal('0.01'))
    original_subtotal = original_subtotal.quantize(Decimal('0.01'))
    discount = discount.quantize(Decimal('0.01'))
    total = total.quantize(Decimal('0.01'))
    total_savings = (site_discount_total + discount).quantize(Decimal('0.01'))

    # Определяем начальное значение для отображения "До сплати"
    # Если выбран prepay_200, показываем 200 грн, иначе полную сумму
    pay_now_amount = None
    # Предвыбранный способ оплаты для плиток выбора (по умолчанию — повна оплата).
    selected_pay_type = 'online_full'
    if request.user.is_authenticated:
        try:
            import storefront.views as old_views
            prof = request.user.userprofile
            # Нормализуем pay_type для корректного сравнения
            # В UserProfile pay_type может быть 'partial' или 'full', нужно нормализовать
            if hasattr(old_views, '_normalize_pay_type') and prof.pay_type:
                normalized_pay_type = old_views._normalize_pay_type(prof.pay_type)
            else:
                normalized_pay_type = prof.pay_type if prof.pay_type else None
            if normalized_pay_type == 'prepay_200':
                pay_now_amount = Decimal('200.00')
                selected_pay_type = 'prepay_200'
            elif normalized_pay_type == 'online_full':
                selected_pay_type = 'online_full'
        except Exception as e:
            cart_logger.warning('Could not determine pay_now_amount from user profile: %s', e)

    checkout_payload = None
    checkout_payload_ids = '[]'
    checkout_payload_contents = '[]'
    checkout_payload_value = float(total)
    checkout_payload_num_items = total_quantity
    if cart_items:
        checkout_payload = {
            'content_ids': content_ids,
            'contents': contents,
            'content_type': 'product',
            'currency': 'UAH',
            'value': float(total),
            'num_items': total_quantity
        }
        checkout_payload_ids = json.dumps(content_ids, ensure_ascii=False)
        checkout_payload_contents = json.dumps(contents, ensure_ascii=False)

    custom_cart_state = _collect_custom_cart_state(request)
    custom_items = custom_cart_state['custom_items']
    custom_items_total = custom_cart_state['custom_items_total']
    custom_items_qty = custom_cart_state['custom_items_qty']
    has_custom_items = custom_cart_state['has_custom_items']
    any_pending_review = custom_cart_state['any_pending_review']
    any_awaiting_review = custom_cart_state['any_awaiting_review']
    any_rejected = custom_cart_state['any_rejected']
    all_approved = custom_cart_state['all_approved']
    has_draft_items = custom_cart_state['has_draft_items']
    approved_total = (total + custom_cart_state['approved_custom_total']).quantize(Decimal('0.01'))

    if custom_cart_state['rejected_removed']:
        try:
            from django.contrib import messages as dj_messages
            dj_messages.info(
                request,
                _("Кастомну позицію відхилено менеджером — її видалено з кошика. За потреби ви можете створити нову заявку.")
            )
        except Exception:
            pass

    # Payment gating: if there are custom items, payment allowed only when ALL are approved.
    # Prepay is disabled whenever custom items exist (manager decides final price).
    if has_custom_items:
        payment_allowed = all_approved
        prepay_allowed = False
    else:
        payment_allowed = True
        prepay_allowed = True

    combined_total = (total + custom_items_total).quantize(Decimal('0.01'))
    has_payable_items = approved_total > 0
    if pay_now_amount is not None:
        if approved_total <= 0:
            pay_now_amount = Decimal('0.00')
        else:
            pay_now_amount = min(pay_now_amount, approved_total)

    # Если передплата недоступна (у кошику є кастомний друк) — не лишаємо її обраною.
    if not prepay_allowed and selected_pay_type == 'prepay_200':
        selected_pay_type = 'online_full'

    return render(
        request,
        'pages/cart.html',
        {
            'items': cart_items,  # Шаблон ожидает 'items', а не 'cart_items'!
            'cart_items': cart_items,  # Оставляем для совместимости
            'custom_items': custom_items,
            'custom_items_total': custom_items_total,
            'custom_items_qty': custom_items_qty,
            'combined_total': combined_total,
            'approved_total': approved_total,
            'has_any_items': bool(cart_items) or bool(custom_items),
            'has_payable_items': has_payable_items,
            'has_custom_items': has_custom_items,
            'any_pending_review': any_pending_review,
            'any_awaiting_review': any_awaiting_review,
            'any_rejected': any_rejected,
            'all_approved': all_approved,
            'has_draft_items': has_draft_items,
            'payment_allowed': payment_allowed,
            'prepay_allowed': prepay_allowed,
            'selected_pay_type': selected_pay_type,
            'subtotal': subtotal,
            'discount': discount,
            'total': total,
            'grand_total': total,  # ДОБАВЛЕНО: алиас для шаблона
            'original_subtotal': original_subtotal,
            'site_discount_total': site_discount_total,
            'total_savings': total_savings,
            'pay_now_amount': pay_now_amount,  # НОВОЕ: начальное значение для "До сплати" (200 грн если prepay_200)
            'total_points': total_points,  # ДОБАВЛЕНО: баллы за заказ
            'promo_code': promo_code,
            'applied_promo': promo_code,  # ДОБАВЛЕНО: алиас для шаблона
            'cart_count': len(cart_items) + len(custom_items),
            'items_total_qty': total_quantity + custom_items_qty,
            'checkout_payload': checkout_payload,
            'checkout_payload_ids': checkout_payload_ids,
            'checkout_payload_contents': checkout_payload_contents,
            'checkout_payload_value': checkout_payload_value,
            'checkout_payload_num_items': checkout_payload_num_items
        }
    )


@require_POST
def add_to_cart(request):
    """
    Добавляет товар в корзину (сессия) с учётом размера и цвета, возвращает JSON с количеством и суммой.
    ВОССТАНОВЛЕНА РАБОЧАЯ ЛОГИКА из старого views.py
    """
    pid = request.POST.get('product_id')
    size = None
    color_variant_id = request.POST.get('color_variant_id')
    try:
        qty = int(request.POST.get('qty') or '1')
    except ValueError:
        qty = 1
    # W1-13 (NEW-508): верхний cap, иначе qty=999999 => гигантский инвойс
    qty = min(max(qty, 1), MAX_CART_ITEM_QTY)

    product = get_object_or_404(Product, pk=pid)
    size = normalize_requested_size(product, request.POST.get('size'))
    fit_option_code, fit_option_label = _resolve_product_fit_payload(product, request.POST.get('fit_option'))
    price = product.final_price
    if not isinstance(price, Decimal):
        price = Decimal(str(price))

    cart = request.session.get('cart', {})
    key = f"{product.id}:{size}:{color_variant_id or 'default'}"
    if fit_option_code:
        key = f"{key}:{fit_option_code}"
    item = cart.get(key, {
        'product_id': product.id,
        'size': size,
        'color_variant_id': color_variant_id,
        'fit_option_code': fit_option_code,
        'fit_option_label': fit_option_label,
        'qty': 0
    })
    item['fit_option_code'] = fit_option_code
    item['fit_option_label'] = fit_option_label
    # W1-13: cap применяется и к накопленному количеству позиции
    item['qty'] = min(item['qty'] + qty, MAX_CART_ITEM_QTY)
    cart[key] = item
    if qty <= 0:
        _reset_monobank_session(request, drop_pending=True)
    request.session['cart'] = cart
    request.session.modified = True

    _reset_monobank_session(request, drop_pending=True)

    try:
        color_variant_int = int(color_variant_id) if color_variant_id else None
    except (TypeError, ValueError):
        color_variant_int = None
    offer_id = product.get_offer_id(color_variant_int, size)
    item_value = (price * qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    ids = [i['product_id'] for i in cart.values()]
    prods = Product.objects.in_bulk(ids)
    total_qty = sum(i['qty'] for i in cart.values())
    total_sum = Decimal('0')
    for i in cart.values():
        p = prods.get(i['product_id'])
        if p:
            price_decimal = p.final_price if isinstance(p.final_price, Decimal) else Decimal(str(p.final_price))
            total_sum += Decimal(i['qty']) * price_decimal

    cart_total = total_sum.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # UTM Tracking: записываем добавление в корзину
    try:
        record_add_to_cart(
            request,
            product_id=product.id,
            product_name=product.title,
            cart_value=float(cart_total)
        )
    except Exception as e:
        cart_logger.warning(f"Failed to record add_to_cart action: {e}")

    return JsonResponse({
        'ok': True,
        'count': total_qty,
        'total': float(cart_total),
        'cart_total': float(cart_total),
        'item': {
            'product_id': product.id,
            'offer_id': offer_id,
            'quantity': qty,
            'item_price': float(price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
            'value': float(item_value),
            'currency': 'UAH',
            'size': size,
            'color_variant_id': color_variant_id,
            'fit_option_code': fit_option_code,
            'fit_option_label': fit_option_label,
            'fit_label': fit_option_label,
            'product_title': product.title,
            'product_category': product.category.name if getattr(product, 'category', None) else ''
        }
    })


@require_POST
def update_cart(request):
    """
    Обнов��ение количества товара в корзине (AJAX).

    POST params:
        cart_key: Ключ товара в корзине
        qty: Новое количество

    Returns:
        JsonResponse: ok, line_total, subtotal, total
    """
    try:
        cart_key = request.POST.get('cart_key')
        qty = int(request.POST.get('qty', 1))

        if qty < 1:
            return JsonResponse({
                'success': False,
                'error': _('Кількість має бути не менше 1')
            }, status=400)

        # W1-13 (NEW-508): верхний cap количества
        qty = min(qty, MAX_CART_ITEM_QTY)

        cart = get_cart_from_session(request)

        if cart_key not in cart:
            return JsonResponse({
                'success': False,
                'error': _('Товар не знайдено у кошику')
            }, status=404)

        # Обновляем количество
        cart[cart_key]['qty'] = qty
        save_cart_to_session(request, cart)

        # ИСПРАВЛЕНО: Получаем цену из Product, а не из сессии (в сессии нет поля 'price')
        product_id = cart[cart_key]['product_id']
        try:
            product = Product.objects.get(id=product_id)
            price = product.final_price
        except Product.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': _('Товар не знайдено')
            }, status=404)

        # Рассчитываем новые суммы
        line_total = price * qty
        subtotal = calculate_cart_total(cart)

        # Учитываем промокод
        discount = Decimal('0')
        promo_code_id = request.session.get('promo_code_id')
        if promo_code_id:
            try:
                promo_code = PromoCode.objects.get(id=promo_code_id)
                if promo_code.can_be_used():
                    discount = promo_code.calculate_discount(subtotal)
            except PromoCode.DoesNotExist:
                pass

        total = subtotal - discount

        return JsonResponse({
            'ok': True,
            'line_total': float(line_total),
            'subtotal': float(subtotal),
            'discount': float(discount),
            'total': float(total)
        })

    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


@require_POST
def remove_from_cart(request):
    """
    Удаление позиции из корзины: поддерживает key="productId:size:color" и (product_id + size).
    ВОССТАНОВЛЕНА РАБОЧАЯ ЛОГИКА из старого cart_remove
    """
    cart = request.session.get('cart', {})

    key = (request.POST.get('key') or '').strip()
    pid = request.POST.get('product_id')
    size = (request.POST.get('size') or '').strip()

    removed = []

    def remove_key_exact(k: str) -> bool:
        if k in cart:
            cart.pop(k, None)
            removed.append(k)
            return True
        return False

    # 1) Пытаемся удалить по exact key
    if key:
        if not remove_key_exact(key):
            # 1.1) case-insensitive сравнение ключей
            target = key.lower()
            for kk in list(cart.keys()):
                if kk.lower() == target:
                    remove_key_exact(kk)
                    break
            # 1.2) если всё ещё не нашли, попробуем удалить все варианты этого товара
            if not removed and ":" in key:
                pid_part = key.split(":", 1)[0]
                for kk in list(cart.keys()):
                    if kk.split(":", 1)[0] == pid_part:
                        remove_key_exact(kk)
    # 2) Либо удаляем по product_id (+optional size)
    elif pid:
        try:
            pid_str = str(int(pid))
        except (ValueError, TypeError):
            pid_str = str(pid)
        if size:
            candidate = f"{pid_str}:{size}"
            if not remove_key_exact(candidate):
                # case-insensitive
                target = candidate.lower()
                for kk in list(cart.keys()):
                    if kk.lower() == target:
                        remove_key_exact(kk)
                        break
        else:
            for kk in list(cart.keys()):
                if kk.split(":", 1)[0] == pid_str:
                    remove_key_exact(kk)

    # Сохраняем изменения
    request.session['cart'] = cart
    request.session.modified = True

    # Пересчёт сводки с использованием Decimal и учетом промокодов
    ids = [i['product_id'] for i in cart.values()]
    prods = Product.objects.in_bulk(ids)
    total_qty = sum(i['qty'] for i in cart.values())
    total_sum = Decimal('0')
    for i in cart.values():
        p = prods.get(i['product_id'])
        if p:
            price_decimal = p.final_price if isinstance(p.final_price, Decimal) else Decimal(str(p.final_price))
            total_sum += Decimal(i['qty']) * price_decimal

    # Учитываем промокод при расчете итоговой суммы
    subtotal = total_sum.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    discount = Decimal('0')
    promo_code_id = request.session.get('promo_code_id')
    if promo_code_id:
        try:
            promo_code = PromoCode.objects.get(id=promo_code_id)
            if promo_code.can_be_used():
                discount = promo_code.calculate_discount(subtotal)
        except PromoCode.DoesNotExist:
            pass

    total = (subtotal - discount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # UTM Tracking: записываем удаление из корзины
    if removed:
        try:
            # Получаем информацию о удаленном товаре для логирования
            for removed_key in removed:
                # Парсим ключ чтобы получить product_id
                if ':' in removed_key:
                    product_id_str = removed_key.split(':')[0]
                    try:
                        product_id = int(product_id_str)
                        product = Product.objects.get(id=product_id)
                        record_remove_from_cart(
                            request,
                            product_id=product.id,
                            product_name=product.title,
                            cart_value=float(total)
                        )
                    except (ValueError, Product.DoesNotExist):
                        pass
        except Exception as e:
            cart_logger.warning(f"Failed to record remove_from_cart action: {e}")

    return JsonResponse({
        'ok': True,
        'count': total_qty,
        'subtotal': float(subtotal),
        'discount': float(discount),
        'total': float(total),
        'removed': removed,
        'keys': list(cart.keys())
    })

    return redirect('cart')


@require_POST
def clear_cart(request):
    """
    Полна�� очистка корзины и промокода.

    Для обычного запроса выполняет redirect в корзину, для AJAX возвращает JSON.
    """
    request.session['cart'] = {}
    request.session.pop('promo_code_id', None)
    request.session.pop('promo_code_data', None)
    request.session.modified = True
    _reset_monobank_session(request, drop_pending=True)

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if is_ajax:
        return JsonResponse({
            'ok': True,
            'count': 0,
            'subtotal': 0.0,
            'discount': 0.0,
            'total': 0.0,
        })

    return redirect('cart')


@never_cache  # W3-4 (CRO-032): персональные данные — не должны попасть в page cache
def get_cart_count(request):
    """
    AJAX endpoint для получения количества товаров в корзине.

    Returns:
        JsonResponse: cart_count
    """
    cart = get_cart_from_session(request)
    cart_count = sum(item.get('qty', 0) for item in cart.values())

    return JsonResponse({
        'cart_count': cart_count
    })


@require_POST
def apply_promo_code(request):
    """
    Применение промокода к корзине (AJAX) с проверкой авторизации и групповых ограничений.

    POST params:
        promo_code: Код промокода

    Returns:
        JsonResponse: success, discount, total, message, auth_required (если нужна авторизация)
    """
    try:
        code = request.POST.get('promo_code', '').strip().upper()

        if not code:
            return JsonResponse({
                'success': False,
                'error': _('Введіть промокод')
            }, status=400)

        # Ищем промокод
        try:
            promo_code = PromoCode.objects.get(code=code)
        except PromoCode.DoesNotExist:
            return JsonResponse({
                'success': False,
                'error': _('Промокод не знайдено')
            }, status=404)

        # НОВАЯ ЛОГИКА: Проверяем авторизацию и права пользователя
        if not request.user.is_authenticated:
            return JsonResponse({
                'success': False,
                'auth_required': True,
                'error': _('Промокоди доступні тільки для зареєстрованих користувачів'),
                'message': _('Будь ласка, увійдіть в акаунт або зареєструйтесь, щоб використати промокод')
            }, status=403)

        # Проверяем, может ли пользователь использовать этот промокод
        can_use, error_message = promo_code.can_be_used_by_user(request.user)

        if not can_use:
            return JsonResponse({
                'success': False,
                'error': error_message
            }, status=400)

        # Рассчитываем скидку
        cart = get_cart_from_session(request)
        subtotal = calculate_cart_total(cart)
        original_subtotal = _calculate_original_subtotal(cart)
        discount = promo_code.calculate_discount(subtotal)

        if discount <= 0:
            # Проверяем причину
            if promo_code.min_order_amount and subtotal < promo_code.min_order_amount:
                return JsonResponse({
                    'success': False,
                    'error': _('Мінімальна сума замовлення для цього промокоду: %(amount)s грн') % {'amount': promo_code.min_order_amount}
                }, status=400)
            return JsonResponse({
                'success': False,
                'error': _('Промокод не застосовується до цього замовлення')
            }, status=400)

        # Сохраняем промокод в сессию (временно, до создания заказа)
        request.session['promo_code_id'] = promo_code.id
        request.session['promo_code_data'] = {
            'code': promo_code.code,
            'discount': float(discount),
            'discount_type': promo_code.discount_type,
            'promo_type': promo_code.promo_type
        }
        request.session.modified = True

        # W1-4д (TECH-023): аналитическое событие применения промокода
        try:
            record_user_action(
                request,
                'coupon_apply',
                cart_value=float(subtotal),
                metadata={
                    'promo_code': promo_code.code,
                    'promo_type': promo_code.promo_type,
                    'discount': float(discount),
                },
            )
        except Exception:
            cart_logger.warning('Failed to record coupon_apply event', exc_info=True)

        total = subtotal - discount
        site_discount_total = (original_subtotal - subtotal).quantize(Decimal('0.01')) if original_subtotal >= subtotal else Decimal('0.00')
        subtotal = subtotal.quantize(Decimal('0.01'))
        original_subtotal = original_subtotal.quantize(Decimal('0.01'))
        discount = discount.quantize(Decimal('0.01'))
        total = total.quantize(Decimal('0.01'))
        total_savings = (site_discount_total + discount).quantize(Decimal('0.01'))

        # Формируем сообщение с учетом типа промокода
        promo_type_label = dict(PromoCode.PROMO_TYPES).get(promo_code.promo_type, '')
        message = _('Промокод застосовано! Знижка: %(discount)s грн') % {'discount': discount}

        if promo_code.promo_type == 'voucher':
            message = _('Ваучер застосовано! Знижка: %(discount)s грн') % {'discount': discount}
        elif promo_code.group:
            message = _('Промокод з групи "%(group)s" застосовано! Знижка: %(discount)s грн') % {
                'group': promo_code.group.name, 'discount': discount,
            }

        return JsonResponse({
            'ok': True,
            'success': True,
            'discount': float(discount),
            'total': float(total),
            'message': message,
            'promo_code': promo_code.code,
            'subtotal': float(subtotal),
            'original_subtotal': float(original_subtotal),
            'site_discount_total': float(site_discount_total),
            'total_savings': float(total_savings),
        })

    except Exception as e:
        cart_logger.error(
            'promo_code_error: user=%s code=%s error=%s',
            request.user.id if request.user.is_authenticated else None,
            code if 'code' in locals() else 'unknown',
            str(e)
        )
        return JsonResponse({
            'success': False,
            'error': _('Помилка при застосуванні промокоду')
        }, status=400)


@require_POST
def remove_promo_code(request):
    """
    Удаление промокода из корзины (AJAX).

    Returns:
        JsonResponse: success, total, message
    """
    try:
        # Удаляем все данные промокода из сессии
        removed_code = None
        if 'promo_code_data' in request.session:
            removed_code = request.session['promo_code_data'].get('code')
            del request.session['promo_code_data']

        if 'promo_code_id' in request.session:
            del request.session['promo_code_id']

            request.session.modified = True

        cart = get_cart_from_session(request)
        subtotal = calculate_cart_total(cart)
        # После удаления промокода скидка = 0
        total = subtotal

        return JsonResponse({
            'ok': True,
            'success': True,
            'subtotal': float(subtotal),
            'discount': 0.0,
            'total': float(total),
            'message': _('Промокод видалено')
        })

    except Exception as e:
        cart_logger.error(
            'promo_code_remove_error: user=%s error=%s',
            request.user.id if request.user.is_authenticated else None,
            str(e)
        )
        return JsonResponse({
            'success': False,
            'error': _('Помилка при видаленні промокоду')
        }, status=400)


# ==================== HELPER FUNCTIONS ====================
# Moved to utils.py to avoid duplication


# ==================== AJAX ENDPOINTS ====================

@never_cache  # W3-4 (CRO-032): персональные данные — не должны попасть в page cache
def cart_summary(request):
    """
    Краткая сводка корзины для обновления бейджа (AJAX).

    Returns:
        JsonResponse: {ok, count, total}
    """
    cart = request.session.get('cart', {})
    custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}

    # Custom cart items count
    custom_qty = 0
    custom_total = Decimal('0')
    if isinstance(custom_cart, dict):
        for item in custom_cart.values():
            if not isinstance(item, dict):
                continue
            try:
                custom_qty += int(item.get('quantity') or 1)
            except (TypeError, ValueError):
                custom_qty += 1
            try:
                custom_total += Decimal(str(item.get('final_total') or 0))
            except Exception:
                pass

    if not cart:
        return JsonResponse({
            'ok': True,
            'count': custom_qty,
            'total': float(custom_total),
        })

    ids = [i['product_id'] for i in cart.values()]
    prods = Product.objects.in_bulk(ids)

    # Проверяем, какие товары найдены
    found_products = set(prods.keys())
    missing_products = set(ids) - found_products

    if missing_products:
        # Удаляем несуществующие товары из корзины
        cart_to_clean = dict(cart)
        for key, item in cart_to_clean.items():
            if item['product_id'] in missing_products:
                cart.pop(key, None)
        _reset_monobank_session(request, drop_pending=True)
        request.session['cart'] = cart
        request.session.modified = True

    # Пересчитываем с очищенной корзиной
    total_qty = sum(i['qty'] for i in cart.values())
    total_sum = Decimal('0')
    for i in cart.values():
        p = prods.get(i['product_id'])
        if p:
            total_sum += Decimal(str(i['qty'])) * Decimal(str(p.final_price))

    total_sum += custom_total
    total_qty += custom_qty

    return JsonResponse({'ok': True, 'count': total_qty, 'total': float(total_sum)})


def cart_mini(request):
    """
    HTML для мини‑корзины (выпадающая панель).

    Returns:
        HttpResponse: Rendered HTML partial
    """
    cart_sess = request.session.get('cart', {})
    if not isinstance(cart_sess, dict):
        cart_sess = {}

    ids = [i['product_id'] for i in cart_sess.values()]
    prods = Product.objects.in_bulk(ids)

    # Optimization: Fetch color variants
    variant_ids = [i.get('color_variant_id') for i in cart_sess.values() if i.get('color_variant_id')]
    variants_map = ProductColorVariant.objects.select_related('color').in_bulk(variant_ids)

    # Проверяем, какие товары найдены
    found_products = set(prods.keys())
    missing_products = set(ids) - found_products

    if missing_products:
        # Удаляем несуществующие товары из корзины
        cart_to_clean = dict(cart_sess)
        for key, item in cart_to_clean.items():
            if item['product_id'] in missing_products:
                cart_sess.pop(key, None)
        _reset_monobank_session(request, drop_pending=True)
        request.session['cart'] = cart_sess
        request.session.modified = True

    items = []
    total = 0
    total_points = 0

    for key, it in cart_sess.items():
        p = prods.get(it['product_id'])
        if not p:
            continue

        # Получаем информацию о цвете
        # Получаем информацию о цвете
        variant_id = it.get('color_variant_id')
        color_variant = variants_map.get(int(variant_id)) if variant_id else None

        unit = p.final_price
        line = unit * it['qty']
        total += line

        # Баллы за товар, если предусмотрены
        try:
            if getattr(p, 'points_reward', 0):
                total_points += int(p.points_reward) * int(it['qty'])
        except Exception:
            pass

        size_value = normalize_requested_size(p, it.get('size'))
        fit_option_code, fit_option_label = _fit_display_from_cart_item(it)
        color_variant_id = color_variant.id if color_variant else None
        offer_id = p.get_offer_id(color_variant_id, size_value)

        items.append({
            'key': key,
            'product': p,
            'size': size_value,
            'fit_option_code': fit_option_code,
            'fit_option_label': fit_option_label,
            'fit_label': fit_option_label,
            'color_variant': color_variant,
            'color_label': _color_label_from_variant(color_variant),
            'qty': it['qty'],
            'unit_price': unit,
            'line_total': line,
            'offer_id': offer_id,
        })

    custom_cart_state = _collect_custom_cart_state(request)
    custom_items = custom_cart_state['custom_items']
    custom_items_total = custom_cart_state['custom_items_total']
    custom_items_qty = custom_cart_state['custom_items_qty']

    combined_total = total + float(custom_items_total)

    return render(request, 'partials/mini_cart.html', {
        'items': items,
        'total': total,
        'total_points': total_points,
        'custom_items': custom_items,
        'custom_items_total': custom_items_total,
        'custom_items_qty': custom_items_qty,
        'combined_total': combined_total,
        'has_any_items': bool(items) or bool(custom_items),
    })


@require_POST
def contact_manager(request):
    """
    Обработка формы связи с менеджером из модального окна.

    Отправляет Telegram сообщение администратору с:
    - Контактными данными клиента (ПІБ, телефон, Telegram, WhatsApp)
    - Содержимым корзины

    POST params:
        full_name: ПІБ клиента
        phone: Телефон (обязательно)
        telegram: Telegram login (опционально)
        whatsapp: WhatsApp (опционально)

    Returns:
        JsonResponse: {'success': True/False, 'error': 'message'}
    """
    try:
        # Получаем данные из формы
        full_name = request.POST.get('full_name', '').strip()
        phone = normalize_phone(request.POST.get('phone', ''))
        telegram = request.POST.get('telegram', '').strip()
        whatsapp = request.POST.get('whatsapp', '').strip()

        # Валидация обязательных полей
        if not full_name or len(full_name) < 3:
            return JsonResponse({
                'success': False,
                'error': _('ПІБ повинно містити мінімум 3 символи')
            })

        if not phone:
            return JsonResponse({
                'success': False,
                'error': _('Введіть коректний номер телефону')
            })

        # Получаем корзину
        cart = get_cart_from_session(request)

        if not cart:
            return JsonResponse({
                'success': False,
                'error': _('Кошик порожній')
            })

        # Получаем товары из БД
        ids = [item['product_id'] for item in cart.values()]
        products = Product.objects.in_bulk(ids)

        # Формируем сообщение для Telegram
        message = f"""📞 <b>ЗАПИТ ЗВ'ЯЗКУ З МЕНЕДЖЕРОМ</b>

👤 <b>Клієнт:</b> {full_name}
📱 <b>Телефон:</b> {phone}"""

        if telegram:
            message += f"\n💬 <b>Telegram:</b> @{telegram}"

        if whatsapp:
            message += f"\n📲 <b>WhatsApp:</b> {whatsapp}"

        message += "\n\n🛒 <b>КОШИ��:</b>\n"

        total_sum = Decimal('0')

        # Добавляем товары
        for key, item_data in cart.items():
            product = products.get(item_data['product_id'])
            if not product:
                continue

            qty = item_data.get('qty', 1)
            unit_price = product.final_price
            line_total = unit_price * qty
            total_sum += line_total

            # Информация о размере и цвете
            size_info = f" ({item_data.get('size')})" if item_data.get('size') else ""
            _, fit_label = _fit_display_from_cart_item(item_data)
            fit_info = f" / {fit_label}" if fit_label else ""

            message += f"• {product.title}{size_info}{fit_info} x {qty} шт = {line_total} грн\n"

        message += f"\n💰 <b>Всього:</b> {total_sum} грн"
        message += "\n\n<i>Клієнт очікує на зв'язок менеджера!</i>"

        # Отправляем в Telegram
        try:
            from orders.telegram_notifications import TelegramNotifier
            notifier = TelegramNotifier()
            notifier.send_admin_message(message)

            return JsonResponse({'success': True})

        except Exception as telegram_error:
            cart_logger.error(
                f"Failed to send Telegram notification for contact request: {telegram_error}",
                exc_info=True
            )
            return JsonResponse({
                'success': False,
                'error': _('Не вдалося відправити повідомлення. Спробуйте пізніше')
            })

    except Exception as e:
        cart_logger.error(f"Error processing contact manager request: {e}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': _('Сталася помилка. Спробуйте ще раз')
        })


@never_cache
def cart_items_api(request):
    """
    AJAX endpoint для получения списка товаров в корзине (JSON).
    Используется для автоматического обновления списка товаров на странице корзины.

    Returns:
        JsonResponse: {
            'ok': True,
            'items': [...],  # Список товаров с данными
            'subtotal': float,
            'discount': float,
            'total': float,
            'grand_total': float,
            'total_points': int,
            'cart_count': int
        }
    """
    cart = get_cart_from_session(request)
    cart_items = []
    subtotal = Decimal('0')
    original_subtotal = Decimal('0')
    total_points = 0
    total_quantity = 0

    # Optimization: Fetch all products and variants at once
    product_ids = [item.get('product_id') for item in cart.values() if item.get('product_id')]
    products_map = Product.objects.select_related('category').prefetch_related('color_variants__images').in_bulk(product_ids)

    color_variant_ids = [item.get('color_variant_id') for item in cart.values() if item.get('color_variant_id')]
    variants_map = ProductColorVariant.objects.select_related('color').prefetch_related('images').in_bulk(color_variant_ids)

    for item_key, item_data in cart.items():
        try:
            product_id = item_data.get('product_id')
            product = products_map.get(int(product_id))
            if not product:
                continue

            price = product.final_price
            original_price = Decimal(product.price)
            qty = int(item_data.get('qty', 1))
            line_total = price * qty
            original_line_total = original_price * qty
            site_line_discount = original_line_total - line_total
            if site_line_discount < 0:
                site_line_discount = Decimal('0.00')
            total_quantity += qty

            variant_id = item_data.get('color_variant_id')
            color_variant = variants_map.get(int(variant_id)) if variant_id else None
            color_label = _color_label_from_variant(color_variant)
            size_value = normalize_requested_size(product, item_data.get('size'))
            fit_option_code, fit_option_label = _fit_display_from_cart_item(item_data)
            color_variant_id = color_variant.id if color_variant else None
            offer_id = product.get_offer_id(color_variant_id, size_value)

            # Баллы за товар
            try:
                if getattr(product, 'points_reward', 0):
                    total_points += int(product.points_reward) * qty
            except Exception:
                pass

            # Подготовка изображения (полный URL)
            image_url = None
            if color_variant and color_variant.images.exists():
                image_url = request.build_absolute_uri(color_variant.images.first().image.url)
            elif product.main_image:
                image_url = request.build_absolute_uri(product.main_image.url)

            cart_items.append({
                'key': item_key,
                'product_id': product.id,
                'product_title': product.title,
                'product_slug': product.slug,
                'unit_price': float(price),
                'original_unit_price': float(original_price),
                'line_total': float(line_total),
                'original_line_total': float(original_line_total),
                'site_discount_amount': float(site_line_discount),
                'qty': qty,
                'size': size_value,
                'fit_option_code': fit_option_code,
                'fit_option_label': fit_option_label,
                'fit_label': fit_option_label,
                'color_variant_id': item_data.get('color_variant_id'),
                'color_label': color_label,
                'image_url': image_url,
                'points_reward': int(getattr(product, 'points_reward', 0) or 0),
                'offer_id': offer_id,
                'item_value': float(line_total),
                'product_category': product.category.name if getattr(product, 'category', None) else '',
                'discount_percent': product.discount_percent or 0,
            })

            subtotal += line_total
            original_subtotal += original_line_total

        except Product.DoesNotExist:
            continue

    promo_code = None
    discount = Decimal('0')
    promo_code_id = request.session.get('promo_code_id')

    if promo_code_id:
        try:
            promo_code = PromoCode.objects.get(id=promo_code_id)
            if promo_code.can_be_used():
                discount = promo_code.calculate_discount(subtotal)
            else:
                del request.session['promo_code_id']
                promo_code = None
        except PromoCode.DoesNotExist:
            del request.session['promo_code_id']

    total = subtotal - discount
    site_discount_total = (original_subtotal - subtotal).quantize(Decimal('0.01')) if original_subtotal >= subtotal else Decimal('0.00')
    subtotal = subtotal.quantize(Decimal('0.01'))
    original_subtotal = original_subtotal.quantize(Decimal('0.01'))
    discount = discount.quantize(Decimal('0.01'))
    total = total.quantize(Decimal('0.01'))
    total_savings = (site_discount_total + discount).quantize(Decimal('0.01'))

    custom_cart_state = _collect_custom_cart_state(request)
    custom_items_total = custom_cart_state['custom_items_total']
    custom_items_qty = custom_cart_state['custom_items_qty']
    has_custom_items = custom_cart_state['has_custom_items']
    any_awaiting_review = custom_cart_state['any_awaiting_review']
    any_rejected = custom_cart_state['any_rejected']
    all_approved = custom_cart_state['all_approved']
    combined_total = (total + custom_items_total).quantize(Decimal('0.01'))
    approved_total = (total + custom_cart_state['approved_custom_total']).quantize(Decimal('0.01'))

    custom_items_payload = []
    for ci in custom_cart_state['custom_items']:
        payload = dict(ci)
        payload['unit_total'] = float(ci['unit_total'])
        payload['line_total'] = float(ci['line_total'])
        payload['final_total'] = float(ci['final_total'])
        custom_items_payload.append(payload)

    return JsonResponse({
        'ok': True,
        'items': cart_items,
        'custom_items': custom_items_payload,
        'custom_items_total': float(custom_items_total),
        'custom_items_qty': custom_items_qty,
        'has_custom_items': has_custom_items,
        'any_awaiting_review': any_awaiting_review,
        'any_rejected': any_rejected,
        'all_approved': all_approved,
        'combined_total': float(combined_total),
        'approved_total': float(approved_total),
        'prepay_allowed': not has_custom_items,
        'payment_allowed': (not has_custom_items) or all_approved,
        'subtotal': float(subtotal),
        'original_subtotal': float(original_subtotal),
        'site_discount_total': float(site_discount_total),
        'discount': float(discount),
        'total': float(total),
        'grand_total': float(total),
        'total_points': total_points,
        'cart_count': total_quantity + custom_items_qty,
        'items_count': total_quantity + custom_items_qty,
        'positions_count': len(cart_items) + len(custom_items_payload),
        'applied_promo': promo_code.code if promo_code else None,
        'total_savings': float(total_savings),
    })


def _coerce_positive_int(raw_value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _lookup_rate_limited_response() -> JsonResponse:
    return JsonResponse(
        {
            'ok': False,
            'error': _('Забагато запитів. Спробуйте ще раз за хвилину.'),
        },
        status=429,
    )


def _message_for_delivery_selection_error(exc: NovaPoshtaSelectionError) -> str:
    return exc.message or _('Оберіть коректні дані доставки зі списку Нової пошти.')


@require_GET
@ratelimit(key='user_or_ip', rate=LOOKUP_RATE_LIMIT, method='GET', block=False)
def nova_poshta_city_search(request):
    if getattr(request, 'limited', False):
        return _lookup_rate_limited_response()

    query = (request.GET.get('q') or '').strip()
    limit = _coerce_positive_int(request.GET.get('limit'), default=10, minimum=1, maximum=20)

    if len(query) < 2:
        return JsonResponse({'ok': True, 'items': []})

    service = NovaPoshtaDirectoryService()
    try:
        items = service.search_settlements(query, limit=limit)
    except NovaPoshtaLookupUnavailable as exc:
        return JsonResponse(
            {
                'ok': False,
                'available': False,
                'error': str(exc),
            },
            status=503,
        )
    except NovaPoshtaLookupError as exc:
        cart_logger.warning('Nova Poshta city lookup failed for query=%r: %s', query, exc)
        return JsonResponse(
            {
                'ok': False,
                'error': _('Не вдалося отримати список міст Нової пошти.'),
            },
            status=502,
        )

    return JsonResponse({'ok': True, 'items': [serialize_city_choice(item) for item in items]})


@require_GET
@ratelimit(key='user_or_ip', rate=LOOKUP_RATE_LIMIT, method='GET', block=False)
def nova_poshta_warehouse_search(request):
    if getattr(request, 'limited', False):
        return _lookup_rate_limited_response()

    settlement_ref = (request.GET.get('settlement_ref') or '').strip()
    city_ref = (request.GET.get('city_ref') or '').strip()
    query = (request.GET.get('q') or '').strip()
    kind = (request.GET.get('kind') or 'all').strip().lower()
    limit = _coerce_positive_int(request.GET.get('limit'), default=20, minimum=1, maximum=50)

    if not settlement_ref and not city_ref:
        return JsonResponse(
            {
                'ok': False,
                'error': _('Спочатку оберіть місто зі списку Нової пошти.'),
            },
            status=400,
        )

    service = NovaPoshtaDirectoryService()
    try:
        items = service.search_warehouses(
            settlement_ref=settlement_ref,
            city_ref=city_ref,
            query=query,
            kind=kind,
            limit=limit,
        )
    except NovaPoshtaLookupUnavailable as exc:
        return JsonResponse(
            {
                'ok': False,
                'available': False,
                'error': str(exc),
            },
            status=503,
        )
    except NovaPoshtaLookupError as exc:
        cart_logger.warning(
            'Nova Poshta warehouse lookup failed for settlement_ref=%r city_ref=%r: %s',
            settlement_ref,
            city_ref,
            exc,
        )
        return JsonResponse(
            {
                'ok': False,
                'error': _('Не вдалося отримати список відділень Нової пошти.'),
            },
            status=502,
        )

    return JsonResponse(
        {
            'ok': True,
            'items': [
                serialize_warehouse_choice(item, fallback_city_ref=city_ref or settlement_ref)
                for item in items
            ],
        }
    )
