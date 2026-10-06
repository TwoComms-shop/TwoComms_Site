"""Ручне створення замовлень адміністратором.

Дозволяє оператору створити повноцінний :class:`orders.models.Order`
з продажів поза сайтом (Instagram, Bezet, офлайн тощо), переюзовуючи
всю інфраструктуру автоматичних замовлень:

* Нова Пошта — той самий вибір міста/відділення через підписані токени
  (:func:`orders.nova_poshta_checkout.resolve_delivery_selection`), тож
  згодом можна автоматично створити ТТН і вона буде скануватись разом з
  рештою (``NovaPoshtaService.update_all_tracking_statuses``).
* Telegram — те саме сповіщення адмінам з кнопками
  «Створити ТТН / Відправлено / Списати зі складу»
  (:meth:`orders.telegram_notifications.TelegramNotifier.send_new_order_notification`).
* Склад, нарахування балів, статуси — без змін, бо це звичайний ``Order``.

Замовлення позначається ``source='manual'`` + ``created_by`` — щоб у
кастомній адмінці було видно, що воно створене вручну.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.db import transaction
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from orders.models import Order, OrderItem
from orders.delivery_display import get_order_nova_poshta_point
from orders.nova_poshta_checkout import (
    NovaPoshtaSelectionError,
    format_delivery_selection_address,
    resolve_delivery_selection,
)
from orders.nova_poshta_data import apply_nova_poshta_refs
from orders.nova_poshta_documents import build_order_payment_snapshot, normalize_checkout_phone
from orders.order_edit_diff import build_order_edit_diff, snapshot_order
from orders.telegram_notifications import telegram_notifier
from productcolors.models import ProductColorVariant
from storefront.models import Product, ProductFitOption, ProductStatus
from storefront.services.size_guides import resolve_product_sizes
from storefront.utm_tracking import (
    ensure_order_purchase_action,
    remove_order_purchase_action,
)

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_ORDER = 50
MAX_QTY_PER_ITEM = 999
IG_PRICE_OVERRIDE_CODES = {
    'manager_repriced_after_review',
    'customer_changed_configuration',
    'catalog_price_exception',
}
IG_MANUAL_CONTEXT_SALT = "storefront.manual-order.ig-client"
IG_MANUAL_CONTEXT_MAX_AGE = 60 * 60 * 4

# Пресети «способу оплати» для UI. Кожен мапиться на канонічну пару
# (pay_type, payment_status), яку розуміють снапшот оплати для ТТН і
# решта системи. Завдяки цьому ТТН/післяплата рахуються коректно.
PAYMENT_PRESETS = {
    'cod': {
        'label': 'Накладений платіж (оплата при отриманні)',
        'pay_type': 'cod',
        'payment_status': 'unpaid',
    },
    'prepaid_200': {
        'label': 'Передплата 200 грн внесена',
        'pay_type': 'prepay_200',
        'payment_status': 'prepaid',
    },
    'partial_manual': {
        'label': 'Часткова оплата (довільна сума)',
        'pay_type': 'prepayment',
        'payment_status': 'prepaid',
    },
    'paid_full': {
        'label': 'Оплачено повністю',
        'pay_type': 'online_full',
        'payment_status': 'paid',
    },
    'unpaid_full': {
        'label': 'Повна оплата, очікується',
        'pay_type': 'online_full',
        'payment_status': 'unpaid',
    },
    'manager_prepayment': {
        'label': 'Передплата за підтвердженою менеджером сумою',
        'pay_type': 'prepayment',
        'payment_status': 'unpaid',
    },
    'provider_prepayment': {
        'label': 'Передплата підтверджена платіжним провайдером',
        'pay_type': 'prepayment',
        'payment_status': 'prepaid',
    },
    'free': {
        'label': 'Безкоштовно / подарунок',
        'pay_type': 'online_full',
        'payment_status': 'paid',
    },
}
DEFAULT_PAYMENT_PRESET = 'cod'


def _sizes_after_variant_rules(sizes, size_rules, fit_code):
    """Apply colour-level size rules when a variant has no assigned size grid."""
    from product_catalog.size_grid_services import normalize_size_value

    wanted_fit = str(fit_code or '').strip().lower()
    general_rules = {}
    fit_rules = {}
    for rule in size_rules or []:
        rule_fit = str(rule.get('fit_code') or '').strip().lower()
        if rule_fit not in ('', wanted_fit):
            continue
        normalized_size = normalize_size_value(rule.get('size'))
        if not normalized_size:
            continue
        target = fit_rules if rule_fit == wanted_fit and wanted_fit else general_rules
        target[normalized_size] = rule

    available = []
    for size in sizes:
        rule = fit_rules.get(normalize_size_value(size)) or general_rules.get(
            normalize_size_value(size)
        )
        if rule is None or (
            rule.get('is_enabled', True)
            and (rule.get('stock') is None or rule.get('stock') > 0)
        ):
            available.append(size)
    return available


def _preset_key_for_order(order):
    """Зворотний маппінг (pay_type, payment_status) → ключ пресету для UI."""
    payment_payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    stored_preset = payment_payload.get('manual_payment_preset')
    if stored_preset in PAYMENT_PRESETS:
        return stored_preset

    payment_status = (order.payment_status or '').strip()
    pay_type = (order.pay_type or '').strip()
    if payment_status == 'paid':
        return 'paid_full'
    if pay_type == 'prepayment' and _provider_payment_authorized(order, payment_payload):
        return 'provider_prepayment'
    if pay_type == 'prepayment' and payment_payload.get('manager_confirmed_amount'):
        return 'manager_prepayment'
    if payment_status in ('prepaid', 'partial'):
        if pay_type == 'prepayment':
            return 'partial_manual'
        return 'prepaid_200'
    if pay_type == 'cod':
        return 'cod'
    if pay_type in ('online_full', 'full'):
        return 'unpaid_full'
    return DEFAULT_PAYMENT_PRESET


def _build_order_initial(order):
    """Серіалізує існуюче замовлення для пре-заповнення форми редагування."""
    items = []
    for item in order.items.all():
        image = ''
        try:
            img = item.product_image
            image = getattr(img, 'url', '') if img else ''
        except Exception:
            image = ''
        if item.is_dtf_film:
            items.append({
                'kind': 'dtf_film', 'item_kind': 'dtf_film', 'item_id': item.id,
                'title': item.title, 'film_length_m': f'{item.film_length_m:.2f}',
                'unit_price': float(item.unit_price or 0), 'qty': 1,
                'line_total': f'{item.line_total:.2f}', 'image': '',
            })
        elif item.is_custom or not item.product_id:
            items.append({
                'kind': 'custom',
                'item_id': item.id,
                'title': item.title,
                'unit_price': float(item.unit_price or 0),
                'qty': item.qty,
                'size': item.size or '',
                'fit_option_code': item.fit_option_code or '',
                'fit_option_label': item.fit_option_label or '',
                'option_values': item.option_values or {},
                'option_labels': item.option_labels or {},
                'color_name': item.color_name_custom or '',
                'image': image,
            })
        else:
            items.append({
                'kind': 'catalog',
                'item_id': item.id,
                'product_id': item.product_id,
                'color_variant_id': item.color_variant_id or '',
                'title': item.title,
                'unit_price': float(item.unit_price or 0),
                'qty': item.qty,
                'size': item.size or '',
                'fit_option_code': item.fit_option_code or '',
                'fit_option_label': item.fit_option_label or '',
                'option_values': item.option_values or {},
                'option_labels': item.option_labels or {},
                'image': image,
            })
    delivery_display = get_order_nova_poshta_point(order)
    payment_snapshot = build_order_payment_snapshot(order)
    shipping_initial = _shipping_initial(order)
    merchandise_paid = Decimal(payment_snapshot['paid_amount'])
    if shipping_initial['delivery_payment_mode'] == 'customer_prepaid' and not shipping_initial['delivery_payment_requires_manual']:
        merchandise_paid = max(merchandise_paid - Decimal(shipping_initial['delivery_charge_amount']), Decimal('0'))
    return {
        'id': order.id,
        'order_number': order.order_number,
        'full_name': order.full_name or '',
        'phone': order.phone or '',
        'sale_source': order.sale_source or '',
        'manager_comment': order.manager_comment or '',
        'payment_preset': _preset_key_for_order(order),
        'discount_amount': str(order.discount_amount or '0.00'),
        'paid_amount': payment_snapshot['paid_amount'],
        'merchandise_paid_amount': f'{merchandise_paid:.2f}',
        'delivery_payer_type': payment_snapshot['delivery_payer_type'],
        'delivery_payment_method': payment_snapshot.get('delivery_payment_method', 'Cash'),
        'cod_enabled': payment_snapshot.get('cod_enabled', payment_snapshot['cod_amount_value'] > 0),
        'delivery_method': order.delivery_method,
        'handover_details': order.handover_details,
        'city': order.city or '',
        'np_office': order.np_office or '',
        'delivery_text': ', '.join(p for p in (order.city, order.np_office) if p),
        'delivery_display': {
            'icon': delivery_display.icon,
            'kind': delivery_display.kind,
            'kind_label': delivery_display.kind_label,
            'number': delivery_display.number,
            'title': delivery_display.title,
            'city': delivery_display.city,
            'address': delivery_display.address,
        },
        'has_tracking': bool(order.tracking_number or order.nova_poshta_document_ref),
        'items': items,
        **shipping_initial,
    }


def _build_products_payload():
    """Серіалізує товари каталогу для вибору в формі (inline JSON).

    Товарів у магазині небагато (десятки), тож віддаємо одразу на сторінку
    і фільтруємо на клієнті — без додаткових запитів до сервера. Кожен
    товар містить ``category`` (id + назва) для групування в пікері.
    """
    variant_queryset = (
        ProductColorVariant.objects
        .select_related('color', 'color__product_catalog_profile', 'product_catalog_details')
        .prefetch_related(
            'images',
            'product_catalog_details__i18n',
            'product_catalog_fit_rules',
            'product_catalog_size_rules',
            'product_catalog_size_grid_assignments__size_grid__product_catalog_profile',
            'product_catalog_combinations',
            'product_catalog_combinations__i18n',
            'product_catalog_faqs',
        )
    )
    products = (
        Product.objects.exclude(status=ProductStatus.ARCHIVED)
        .select_related('category', 'catalog', 'size_grid')
        .prefetch_related(
            'catalog__options__values',
            'catalog__size_grids__product_catalog_profile',
            'fit_options',
            'product_catalog_fit_notes',
            'product_catalog_size_grid_assignments__size_grid__product_catalog_profile',
            'product_catalog_size_rules',
            'product_catalog_option_profiles',
            'product_catalog_option_profiles__i18n',
            Prefetch('color_variants', queryset=variant_queryset),
        )
        .order_by('category__order', 'category__name', 'title')
    )
    payload = []
    for product in products:
        image = product.display_image
        image_url = getattr(image, 'url', '') if image else ''
        product_variants = list(product.color_variants.all())

        fit_options = [option for option in product.fit_options.all() if option.is_active]
        try:
            default_sizes = list(resolve_product_sizes(product))
        except Exception:  # pragma: no cover - захист від нестандартних сіток
            default_sizes = []
        sizes_by_fit = {option.code: list(default_sizes) for option in fit_options}
        variant_sizes_by_fit = {variant.id: {} for variant in product_variants}
        if fit_options:
            from product_catalog.size_grid_services import build_size_grid_comparison

            for comparison in build_size_grid_comparison(product, variants=product_variants):
                option_key = str(comparison.get('option_key') or '')
                if option_key.startswith('fit='):
                    fit_code = option_key.removeprefix('fit=')
                    sizes_by_fit[fit_code] = list(
                        comparison.get('available_sizes') or []
                    )
                    for variant_payload in comparison.get('variants') or []:
                        variant_id = variant_payload.get('variant_id')
                        if variant_id in variant_sizes_by_fit:
                            variant_sizes_by_fit[variant_id][fit_code] = list(
                                variant_payload.get('available_sizes') or []
                            )

        default_fit = next(
            (option for option in fit_options if option.is_default),
            fit_options[0] if fit_options else None,
        )
        fits = [
            {
                'code': option.code,
                'label': option.label,
                'is_default': option == default_fit,
            }
            for option in fit_options
        ]

        variants = []
        for variant in product_variants:
            color = getattr(variant, 'color', None)
            from product_catalog.services import effective_cart_unit_price, variant_public_context

            public_context = variant_public_context(variant)
            available_fit_codes = list(public_context.get('available_fit_codes') or [])
            is_thermo = bool(public_context.get('is_thermo'))
            prices_by_fit = {
                code: int(effective_cart_unit_price(product, variant, fit_code=code))
                for code in available_fit_codes
            }
            size_rules = public_context.get('size_rules') or []
            resolved_variant_sizes = {
                code: list(
                    variant_sizes_by_fit[variant.id].get(
                        code,
                        _sizes_after_variant_rules(
                            sizes_by_fit.get(code, default_sizes),
                            size_rules,
                            code,
                        ),
                    )
                )
                for code in available_fit_codes
            }
            variants.append({
                'id': variant.id,
                'name': (getattr(color, 'name', '') or '').strip() or 'Колір',
                'primary_hex': getattr(color, 'primary_hex', '') or '',
                'secondary_hex': getattr(color, 'secondary_hex', '') or '',
                'available_fit_codes': available_fit_codes,
                'is_thermo': is_thermo,
                'prices_by_fit': prices_by_fit,
                'sizes_by_fit': resolved_variant_sizes,
            })

        category = getattr(product, 'category', None)
        payload.append({
            'id': product.id,
            'title': product.title,
            'price': int(product.final_price or 0),
            'image': image_url,
            'sizes': default_sizes,
            'fits': fits,
            'default_fit_code': default_fit.code if default_fit else '',
            'sizes_by_fit': sizes_by_fit,
            'variants': variants,
            'category_id': getattr(category, 'id', 0) or 0,
            'category_name': (getattr(category, 'name', '') or 'Інше').strip() or 'Інше',
        })
    return payload


def _build_categories_payload():
    """Список активних категорій (для табів пікера), у порядку показу."""
    from storefront.models import Category

    return [
        {'id': c.id, 'name': c.name}
        for c in Category.objects.filter(is_active=True).order_by('order', 'name')
    ]


def _decimal_or_none(raw):
    if raw in (None, ''):
        return None
    try:
        value = Decimal(str(raw).replace(',', '.'))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0 or value > Decimal('9999999999.99'):
        return None
    try:
        return value.quantize(Decimal('0.01'))
    except InvalidOperation:
        return None


def _strict_decimal(raw, *, label, maximum=Decimal('9999999999.99'), positive=False):
    text = str(raw if raw is not None else '').strip()
    if not re.fullmatch(r'\d+(?:[.,]\d{1,2})?', text):
        raise ValueError(f'{label}: введіть число з максимум двома знаками після коми.')
    try:
        value = Decimal(text.replace(',', '.'))
        if not value.is_finite() or value > maximum or (positive and value <= 0):
            raise ValueError
        return value.quantize(Decimal('.01'))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label}: значення поза допустимими межами.')


def _boolean_control(raw):
    if isinstance(raw, bool):
        return raw
    if raw in (0, '0', 'false', 'False'):
        return False
    if raw in (1, '1', 'true', 'True'):
        return True
    raise ValueError('Оберіть, чи потрібен накладений платіж.')


def _provider_payment_authorized(order, payload):
    amount = _decimal_or_none(payload.get('paid_value'))
    if not amount or amount <= 0:
        return False
    if payload.get('provider_payment_confirmed'):
        return True
    if not (order.payment_provider and order.payment_invoice_id):
        return False
    provider_status = payload.get('monobank_status')
    if provider_status is not None:
        return provider_status == 'success'
    return order.payment_status in {'paid', 'prepaid', 'partial'}


def _manual_payment_controls(data, *, order, preset_key, merchandise_total):
    """Persist staff payment controls without replacing Instagram/provider evidence."""
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    if preset_key in {'manager_prepayment', 'provider_prepayment'}:
        authorized = (
            payload.get('manual_payment_evidence_confirmed')
            and _decimal_or_none(payload.get('manager_confirmed_amount'))
        ) if preset_key == 'manager_prepayment' else (
            _provider_payment_authorized(order, payload)
        )
        if not authorized:
            raise ValueError('Цей спосіб оплати потребує наявного підтвердження менеджера або провайдера.')
        return None
    # Reviewed delivery and payment remain owned by their original evidence.
    canonical_shipping = payload.get('delivery_payment')
    reviewed_shipping = isinstance(canonical_shipping, dict) and canonical_shipping.get('authority') == 'payment_review'
    if reviewed_shipping or any(payload.get(key) for key in (
        'instagram_delivery_contract', 'instagram_payment_review_id',
        'manager_payment_decision_id', 'manual_payment_evidence_confirmed',
        'provider_payment_confirmed', 'ig_payment_reconciliation',
    )) or _provider_payment_authorized(order, payload):
        if preset_key == 'partial_manual':
            raise ValueError('Змініть підтверджену оплату через її перевірку, щоб зберегти платіжні докази.')
        return None

    preset = PAYMENT_PRESETS[preset_key]
    stored = payload.get('manual_shipment_payment')
    stored = stored if isinstance(stored, dict) and payload.get('manual_payment_preset') == preset_key else {}
    if order.pk and preset_key != 'partial_manual' and not stored and not any(
        key in data for key in ('delivery_payer_type', 'delivery_payment_method', 'cod_enabled', 'paid_amount')
    ):
        return None
    mode = data.get('delivery_payment_mode')
    if mode is None and isinstance(canonical_shipping, dict):
        canonical_mode = canonical_shipping.get('mode')
        canonical_payer = 'Recipient' if canonical_mode == 'carrier_recipient' else 'Sender'
        if 'delivery_payer_type' not in data or data.get('delivery_payer_type') == canonical_payer:
            mode = canonical_mode
    derived_payer = 'Recipient' if mode in {'carrier_recipient', 'clientcarrier'} else 'Sender' if mode in {'customer_prepaid', 'merchant_free'} else None
    payer = str(derived_payer or data.get('delivery_payer_type') or stored.get('payer_type') or 'Recipient').strip()
    method = str(data.get('delivery_payment_method') or stored.get('payment_method') or 'Cash').strip()
    if payer not in {'Sender', 'Recipient'}:
        raise ValueError('Оберіть платника доставки: відправник або отримувач.')
    if method not in {'Cash', 'NonCash'}:
        raise ValueError('Оберіть спосіб оплати доставки: готівковий або безготівковий.')

    if preset_key == 'partial_manual':
        paid = _strict_decimal(data.get('paid_amount', stored.get('paid_amount')), label='Внесена сума', positive=True)
        if paid >= merchandise_total:
            raise ValueError('Часткова оплата має бути меншою за суму товарів. Для повної оплати оберіть відповідний спосіб.')
    else:
        paid = (
            merchandise_total if preset['payment_status'] == 'paid'
            else min(Decimal('200.00'), merchandise_total) if preset_key == 'prepaid_200'
            else Decimal('0.00')
        )
        if preset_key == 'prepaid_200' and mode == 'customer_prepaid':
            fee = _decimal_or_none(data.get('delivery_charge_amount', canonical_shipping.get('delivery_amount', '0') if isinstance(canonical_shipping, dict) else '0'))
            if fee is None or fee > Decimal('200.00'):
                raise ValueError('Оплата 200 грн не покриває підтверджену суму доставки.')
            paid = min(Decimal('200.00') - fee, merchandise_total)
        if data.get('paid_amount') not in (None, ''):
            submitted = _strict_decimal(data['paid_amount'], label='Внесена сума')
            if submitted != paid:
                raise ValueError('Внесена сума не відповідає способу оплати. Оберіть часткову оплату для довільної суми.')
    if paid > merchandise_total:
        raise ValueError('Внесена сума не може перевищувати суму товарів.')
    default_cod = preset_key in {'cod', 'prepaid_200', 'partial_manual'} and paid < merchandise_total
    cod = _boolean_control(data['cod_enabled']) if 'cod_enabled' in data else stored.get('cod_enabled', default_cod)
    if cod and paid >= merchandise_total:
        raise ValueError('Накладений платіж неможливий для повністю оплаченого замовлення.')
    if order.delivery_method == 'handover':
        if 'cod_enabled' in data and cod:
            raise ValueError('Для передачі / самовивозу вимкніть накладений платіж.')
        cod = False
    return {
        'payer_type': payer, 'payment_method': method,
        'cod_enabled': bool(cod), 'paid_amount': f'{paid:.2f}',
    }


def _apply_delivery(order, delivery):
    if delivery['delivery_method'] == 'handover' and (order.tracking_number or order.nova_poshta_document_ref):
        raise ValueError('Для замовлення вже створено ТТН. Перед переходом на передачу / самовивіз скасуйте її.')
    order.delivery_method = delivery['delivery_method']
    order.handover_details = delivery['handover_details']
    order.city, order.np_office = delivery['city'], delivery['np_office']
    apply_nova_poshta_refs(order, delivery['refs'])
    if order.delivery_method == 'handover':
        order.nova_poshta_recipient_ref = order.nova_poshta_recipient_contact_ref = None


def _validate_order_total(total):
    if total > Decimal('9999999999.99'):
        raise ValueError('Загальна сума замовлення завелика.')


def _review_quoted_total(review):
    if not review:
        return None
    if review.deal_id:
        amount = _decimal_or_none(review.deal.amount)
        if amount is not None and amount > 0:
            return amount
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get('order_draft') if isinstance(evidence.get('order_draft'), dict) else {}
    amount = _decimal_or_none(draft.get('merchandise_total') or draft.get('quoted_total'))
    return amount if amount is not None and amount > 0 else None


def _review_shipping_policy(review):
    evidence = review.evidence if review and isinstance(review.evidence, dict) else {}
    draft = evidence.get('order_draft') if isinstance(evidence.get('order_draft'), dict) else {}
    agreement = draft.get('agreement') if isinstance(draft.get('agreement'), dict) else {}
    policy = draft.get('shipping_payment') or agreement.get('shipping_payment')
    return policy if isinstance(policy, dict) else {}


def _shipping_initial(order):
    from orders.services.delivery_payment import delivery_payment_snapshot
    snapshot = delivery_payment_snapshot(order, item_rows=list(order.items.all()))
    return {
        'delivery_payment_mode': snapshot['mode'],
        'delivery_charge_amount': f"{snapshot['delivery_amount']:.2f}",
        'delivery_payment_locked': snapshot['source_locked'],
        'delivery_payment_requires_manual': snapshot['requires_manual'],
        'delivery_payer_type': snapshot['payer_type'],
    }


def _review_shipping_initial(review):
    policy = _review_shipping_policy(review)
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get('order_draft') or {}
    delivery = draft.get('delivery_amount') or draft.get('delivery_total') or '0'
    mode = policy.get('mode') or ('customer_prepaid' if (_decimal_or_none(delivery) or 0) > 0 else 'carrier_recipient')
    return {
        'delivery_payment_mode': mode,
        'delivery_charge_amount': str(policy.get('customer_charge_amount', delivery)),
        'delivery_payment_locked': bool(policy and mode != 'unknown' or not policy and (_decimal_or_none(delivery) or 0) > 0),
        'delivery_payment_requires_manual': mode == 'unknown',
    }


def _manual_delivery_contract(data, *, merchandise_total, actor, preset_key, item_rows, existing=None, confirmed_amount=None, review=None, discount_amount='0', existing_snapshot=None):
    from orders.services.delivery_payment import build_delivery_payment_contract, delivery_payment_snapshot
    canonical_explicit = 'delivery_payment_mode' in data or 'delivery_charge_amount' in data
    explicit = canonical_explicit or 'delivery_payer_type' in data
    if not explicit:
        return None
    mode = str(data.get('delivery_payment_mode') or '').strip()
    if not canonical_explicit:
        legacy_payer = str(data.get('delivery_payer_type') or '').strip()
        if legacy_payer not in {'Sender', 'Recipient'}:
            raise ValueError('Оберіть коректного платника доставки.')
        mode = 'merchant_free' if legacy_payer == 'Sender' else 'carrier_recipient'
    if mode == 'clientcarrier':
        mode = 'carrier_recipient'
    amount = _decimal_or_none(data.get('delivery_charge_amount', '0'))
    if amount is None or amount < 0:
        raise ValueError('Вкажіть коректну суму доставки.')
    current = None
    if existing is not None:
        current = existing_snapshot or delivery_payment_snapshot(existing, item_rows=list(existing.items.all()))
        if not canonical_explicit and current['valid'] is True and data.get('delivery_payer_type') == current['payer_type']:
            mode, amount = current['mode'], current['delivery_amount']
        if current['source_locked']:
            if not canonical_explicit:
                return None
            if mode != current['mode'] or amount != current['delivery_amount']:
                raise ValueError('Доставку підтверджено з переписки; для зміни потрібна нова перевірка домовленості.')
            return None
    allocated = None
    if mode == 'customer_prepaid':
        if preset_key == 'free':
            raise ValueError('Безкоштовне замовлення не підтверджує оплату доставки; оберіть доставку коштом продавця.')
        if confirmed_amount is None:
            if review is None and preset_key not in {'manager_prepayment', 'provider_prepayment'}:
                payload = existing.payment_payload if existing and isinstance(existing.payment_payload, dict) else {}
                controls = payload.get('manual_shipment_payment')
                controls = controls if isinstance(controls, dict) else {}
                carriage_confirmed = (
                    _boolean_control(data['delivery_paid_confirmed']) if 'delivery_paid_confirmed' in data else False
                ) or bool(current and current['valid'] is True and current['mode'] == 'customer_prepaid' and current['delivery_amount'] == amount)
                if preset_key == 'paid_full':
                    confirmed_amount = merchandise_total + amount
                elif preset_key == 'prepaid_200':
                    # Legacy fixed preset is a 200 UAH transfer in total;
                    # prepaid carriage consumes part of it before goods.
                    confirmed_amount = Decimal('200.00')
                elif preset_key == 'partial_manual' and carriage_confirmed:
                    goods_paid = _strict_decimal(data.get('paid_amount', controls.get('paid_amount', payload.get('paid_value'))), label='Внесена сума', positive=True)
                    if goods_paid >= merchandise_total:
                        raise ValueError('Часткова оплата має бути меншою за суму товарів.')
                    confirmed_amount = goods_paid + amount
                elif carriage_confirmed:
                    confirmed_amount = amount
            elif existing is not None:
                payload = existing.payment_payload if isinstance(existing.payment_payload, dict) else {}
                confirmed_amount = _decimal_or_none(payload.get('manager_confirmed_amount') or payload.get('paid_value'))
        # Explicit staff selection allocates the verified payment to carriage;
        # the builder still rejects an allocation larger than that payment.
        allocated = amount
    return build_delivery_payment_contract(
        mode=mode, merchandise_total=merchandise_total, delivery_amount=amount,
        actor_id=actor.pk, authority='manual_manager', confirmed_amount=confirmed_amount,
        allocated_delivery_amount=allocated, item_rows=item_rows,
        discount_amount=discount_amount,
        review_id=getattr(review, 'pk', None),
        decision_id=getattr(_authoritative_manager_payment_decision(review), 'pk', None) if review else None,
    )


def _legacy_delivery_alias(contract):
    return {key: contract.get(key) for key in (
        'merchandise_total', 'delivery_amount', 'payable_total', 'payer_type',
        'review_id', 'decision_id', 'actor_id', 'evidence_message_ids',
    )} | {'prepaid': contract.get('mode') == 'customer_prepaid'}


def _assert_delivery_contract_compatible(order, contract):
    if contract is None:
        return
    from orders.services.delivery_payment import delivery_payment_snapshot
    current = delivery_payment_snapshot(order, item_rows=list(order.items.all()))
    if current['valid'] is not True or any(
        str(current[key]) != str(contract[key]) if key in {'mode', 'payer_type'}
        else Decimal(str(current[key])) != Decimal(str(contract[key]))
        for key in ('mode', 'payer_type', 'merchandise_total', 'delivery_amount', 'payable_total')
    ):
        raise ValueError('Існуюче замовлення має інші або непідтверджені умови оплати доставки.')


def _review_delivery_contract(review, *, merchandise_total, actor, item_rows=None, agreement_verified=False):
    """Keep reviewed prepaid carriage separate from garment prices."""
    if review is None:
        return None
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get('order_draft') if isinstance(evidence.get('order_draft'), dict) else {}
    policy = _review_shipping_policy(review)
    mode = policy.get('mode')
    if mode == 'unknown':
        raise ValueError('Потрібно уточнити, хто сплачує доставку та яку суму включено в оплату.')
    delivery = _decimal_or_none(policy.get('customer_charge_amount') if policy else draft.get('delivery_amount') or draft.get('delivery_total'))
    if not policy and (delivery is None or delivery <= 0):
        return None
    if delivery is None or delivery < 0:
        raise ValueError('Суму доставки з переписки не підтверджено.')
    if policy and not agreement_verified:
        from orders.services.ig_review_order_builder import _current_agreement_error
        if _current_agreement_error(review.client, draft):
            raise ValueError('Домовленість про доставку більше не підтверджена поточною перепискою.')
    mode = mode or 'customer_prepaid'
    quoted_merchandise = _decimal_or_none(draft.get('merchandise_total') or draft.get('quoted_total'))
    quoted_payable = _decimal_or_none(draft.get('payable_total'))
    if quoted_merchandise is None or quoted_payable != quoted_merchandise + delivery:
        raise ValueError('Сума товарів, доставки та повної оплати потребує звірки.')
    decision = _authoritative_manager_payment_decision(review)
    payable = merchandise_total + delivery
    evidence_ids = {
        int(value) for value in (draft.get('amount_evidence_message_ids') or [])
        if str(value).isdigit()
    }
    for key in ('amount_source_message_id', 'delivery_source_message_id'):
        if str(draft.get(key) or '').isdigit():
            evidence_ids.add(int(draft[key]))
    evidence_ids.update(value for value in policy.get('evidence_message_ids', []) if type(value) is int and value > 0)
    if not evidence_ids:
        raise ValueError('Для суми доставки потрібне повідомлення-джерело з переписки.')
    from orders.services.delivery_payment import build_delivery_payment_contract
    return build_delivery_payment_contract(
        mode=mode, merchandise_total=merchandise_total, delivery_amount=delivery,
        actor_id=actor.pk, authority='payment_review',
        confirmed_amount=getattr(decision, 'confirmed_amount', None),
        review_id=review.pk, decision_id=getattr(decision, 'pk', None),
        evidence_message_ids=sorted(evidence_ids), item_rows=item_rows,
    )


def _price_override_payload(data, *, quoted_total, actual_total, actor, review):
    if quoted_total is None or quoted_total == actual_total:
        return None
    code = str(data.get('price_override_code') or '').strip().lower()
    reason = str(data.get('price_override_reason') or '').strip()
    if code not in IG_PRICE_OVERRIDE_CODES:
        raise ValueError(
            f'Сума позицій {actual_total:.2f} грн не збігається з узгодженою '
            f'сумою {quoted_total:.2f} грн; оберіть структуровану причину зміни.'
        )
    if not reason:
        raise ValueError('Додайте пояснення до структурованої зміни узгодженої суми.')
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get('order_draft') if isinstance(evidence.get('order_draft'), dict) else {}
    evidence_ids = []
    source_message_id = draft.get('amount_source_message_id')
    if str(source_message_id).isdigit():
        evidence_ids.append(int(source_message_id))
    return {
        'quoted_total': f'{quoted_total:.2f}',
        'actual_order_total': f'{actual_total:.2f}',
        'code': code,
        'reason': reason[:500],
        'actor_id': actor.pk,
        **({'review_id': review.pk, 'evidence_message_ids': evidence_ids} if evidence_ids else {}),
    }


def _coerce_int(raw, *, default=1, minimum=1, maximum=MAX_QTY_PER_ITEM):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _resolve_fit_payload(product, requested_code, *, variant=None, allow_unavailable=False):
    """Повертає (code, label) обраного крою товару, як у дропшип-формі."""
    requested = str(requested_code or '').strip().lower()
    options = list(
        product.fit_options
        .filter(is_active=True)
        .order_by('order', 'id')
        .only('code', 'label', 'is_default')
    )
    selected = next((o for o in options if o.code == requested), None)
    if requested and selected is None and allow_unavailable:
        selected = (
            product.fit_options
            .filter(code=requested)
            .only('code', 'label', 'is_default')
            .first()
        )
    if not options and selected is None:
        return '', ''
    if requested and selected is None:
        raise ValueError(f'Невірна посадка для товару «{product.title}».')

    if variant is not None:
        from product_catalog.services import variant_allows_fit

        allowed_options = [
            option for option in options
            if variant_allows_fit(variant, option.code)
        ]
        if selected is not None and selected not in allowed_options and not allow_unavailable:
            raise ValueError(
                f'Посадка «{selected.label}» недоступна для вибраного кольору товару «{product.title}».'
            )
    else:
        allowed_options = options

    if selected is None:
        selected = next(
            (option for option in allowed_options if option.is_default),
            allowed_options[0] if allowed_options else None,
        )
    if selected is None:
        raise ValueError(f'Для вибраного кольору товару «{product.title}» немає доступної посадки.')
    return selected.code, selected.label


def _build_order_item(
    raw_item,
    *,
    order,
    products_map,
    variants_map,
    historical_items_by_id=None,
):
    """Будує (без збереження) OrderItem з одного запису форми.

    Кидає ``ValueError`` з людським повідомленням при некоректних даних.
    """
    kind = str(raw_item.get('kind') or 'catalog').strip()
    if kind == 'dtf_film':
        length = _strict_decimal(
            raw_item.get('film_length_m'), label='Довжина DTF плівки',
            maximum=Decimal('999999.99'), positive=True,
        )
        price = _strict_decimal(
            raw_item.get('unit_price') if raw_item.get('unit_price') not in (None, '') else '320',
            label='Ціна DTF плівки за метр',
        )
        total = (length * price).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if total > Decimal('9999999999.99'):
            raise ValueError('Сума позиції DTF плівки завелика.')
        return OrderItem(
            order=order, item_kind='dtf_film', film_length_m=length,
            title='DTF плівка TwoComms', unit_price=price, line_total=total,
            qty=1, is_custom=True,
        )
    if kind not in {'catalog', 'custom'}:
        raise ValueError('Невідомий тип позиції замовлення.')
    qty = _coerce_int(raw_item.get('qty'), default=1)
    size = str(raw_item.get('size') or '').strip()[:16]
    unit_price = _decimal_or_none(raw_item.get('unit_price'))
    option_values = raw_item.get('option_values') if isinstance(raw_item.get('option_values'), dict) else {}
    option_labels = raw_item.get('option_labels') if isinstance(raw_item.get('option_labels'), dict) else {}

    if kind == 'custom':
        title = str(raw_item.get('title') or '').strip()
        if not title:
            raise ValueError('Вкажіть назву товару поза каталогом.')
        if unit_price is None:
            raise ValueError(f'Вкажіть коректну ціну для «{title}».')
        color_name = str(raw_item.get('color_name') or '').strip()[:100]
        return OrderItem(
            order=order,
            product=None,
            color_variant=None,
            title=title[:200],
            size=size,
            fit_option_code=str(raw_item.get('fit_option_code') or '')[:50],
            fit_option_label=str(raw_item.get('fit_option_label') or '')[:100],
            option_values=option_values,
            option_labels=option_labels,
            qty=qty,
            unit_price=unit_price,
            line_total=unit_price * qty,
            is_custom=True,
            color_name_custom=color_name,
        )

    # Каталожна позиція
    try:
        product_id = int(raw_item.get('product_id'))
    except (TypeError, ValueError):
        raise ValueError('Оберіть товар зі списку або додайте позицію вручну.')

    product = products_map.get(product_id)
    if product is None:
        raise ValueError('Обраний товар не знайдено. Оновіть сторінку і спробуйте ще раз.')

    variant = None
    variant_id = raw_item.get('color_variant_id')
    if variant_id:
        try:
            variant = variants_map.get(int(variant_id))
        except (TypeError, ValueError):
            variant = None
        if variant is None or variant.product_id != product.id:
            raise ValueError(f'Невірний колір для товару «{product.title}».')

    if unit_price is None:
        unit_price = Decimal(str(product.final_price or 0)).quantize(Decimal('0.01'))

    requested_fit_code = str(
        raw_item.get('fit_option_code') or raw_item.get('fit_option') or ''
    ).strip().lower()
    historical_key = (
        product.id,
        variant.id if variant is not None else None,
        requested_fit_code,
        size,
    )
    try:
        submitted_item_id = int(raw_item.get('item_id'))
    except (TypeError, ValueError):
        submitted_item_id = None
    allow_historical = (
        submitted_item_id is not None
        and (historical_items_by_id or {}).get(submitted_item_id) == historical_key
    )
    fit_code, fit_label = _resolve_fit_payload(
        product,
        requested_fit_code,
        variant=variant,
        allow_unavailable=allow_historical,
    )
    if variant is not None:
        from product_catalog.services import variant_allows_purchase

        if not allow_historical and not variant_allows_purchase(
            product,
            variant,
            fit_code=fit_code,
            size=size,
        ):
            raise ValueError(
                f'Розмір «{size}» недоступний для вибраного кольору та посадки '
                f'товару «{product.title}».'
            )

    return OrderItem(
        order=order,
        product=product,
        color_variant=variant,
        title=product.title[:200],
        size=size,
        fit_option_code=fit_code,
        fit_option_label=fit_label,
        option_values=option_values,
        option_labels=option_labels,
        qty=qty,
        unit_price=unit_price,
        line_total=unit_price * qty,
        is_custom=False,
    )


def _parse_request_payload(request):
    if request.content_type and 'application/json' in request.content_type:
        try:
            data = json.loads(request.body or '{}')
            return data if isinstance(data, dict) else None
        except (ValueError, TypeError):
            return None
    return request.POST


def _resolve_delivery(data, *, allow_keep=False):
    """Резолвить доставку з payload.

    Повертає dict {city, np_office, refs, display} АБО кидає ``_DeliveryError``
    з готовою JsonResponse-помилкою. Якщо ``allow_keep`` і method=='keep' —
    повертає None (означає «не змінювати доставку»).
    """
    delivery_method = str(data.get('delivery_method') or 'np').strip()

    if allow_keep and delivery_method == 'keep':
        return None

    if delivery_method == 'handover':
        details = str(data.get('handover_details') or '').strip()
        if not details or len(details) > 2000:
            raise _DeliveryError('Вкажіть, кому та як передати замовлення (до 2000 символів).', field='handover_details')
        return {
            'delivery_method': 'handover', 'handover_details': details,
            'city': '', 'np_office': '',
            'refs': {'np_settlement_ref': '', 'np_city_ref': '', 'np_warehouse_ref': ''},
            'display': f'Передача / самовивіз: {details}',
        }

    if delivery_method == 'manual':
        city = str(data.get('city') or '').strip()[:100]
        np_office = str(data.get('np_office') or '').strip()[:200]
        if not city or not np_office:
            raise _DeliveryError('Вкажіть місто та адресу/відділення доставки.', field='np_office')
        return {
            'delivery_method': 'manual', 'handover_details': '',
            'city': city,
            'np_office': np_office,
            'refs': {'np_settlement_ref': '', 'np_city_ref': '', 'np_warehouse_ref': ''},
            'display': ', '.join(p for p in (city, np_office) if p),
        }

    if delivery_method not in {'np', 'nova_poshta'}:
        raise _DeliveryError('Оберіть коректний спосіб доставки.', field='delivery_method')
    # Нова Пошта
    try:
        selection = resolve_delivery_selection(data)
    except NovaPoshtaSelectionError as exc:
        raise _DeliveryError(exc.message, field=exc.field)
    return {
        'delivery_method': 'nova_poshta', 'handover_details': '',
        'city': selection.city,
        'np_office': selection.np_office,
        'refs': {
            'np_settlement_ref': selection.settlement_ref,
            'np_city_ref': selection.city_ref,
            'np_warehouse_ref': selection.warehouse_ref,
        },
        'display': format_delivery_selection_address(selection),
    }


class _DeliveryError(Exception):
    def __init__(self, message, *, field=''):
        super().__init__(message)
        self.message = message
        self.field = field


def _collect_items(raw_items):
    """Нормалізує список позицій з payload, повертає (raw_items, products_map, variants_map)
    або кидає ValueError."""
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except (ValueError, TypeError):
            raw_items = None
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError('Додайте хоча б один товар до замовлення.')
    if len(raw_items) > MAX_ITEMS_PER_ORDER:
        raise ValueError('Забагато позицій у замовленні.')

    product_ids = []
    variant_ids = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError('Некоректна позиція замовлення.')
        if str(raw_item.get('kind') or 'catalog').strip() in {'custom', 'dtf_film'}:
            continue
        try:
            product_ids.append(int(raw_item.get('product_id')))
        except (TypeError, ValueError):
            continue
        if raw_item.get('color_variant_id'):
            try:
                variant_ids.append(int(raw_item.get('color_variant_id')))
            except (TypeError, ValueError):
                pass

    kinds = {str(item.get('kind') or 'catalog').strip() for item in raw_items}
    if 'dtf_film' in kinds and kinds != {'dtf_film'}:
        raise ValueError('Одяг і DTF-плівку оформіть окремими замовленнями: вони мають різне пакування.')

    products_map = Product.objects.in_bulk(product_ids) if product_ids else {}
    variants_map = (
        ProductColorVariant.objects.select_related('color').in_bulk(variant_ids) if variant_ids else {}
    )
    return raw_items, products_map, variants_map


def _authoritative_manager_payment_decision(review):
    from management.ig_bot_models import IgPaymentReviewDecision

    decision = review.decisions.order_by("-id").first()
    if not decision:
        return None
    if decision.decision != IgPaymentReviewDecision.Decision.MANAGER_VERIFIED:
        return None
    if decision.verification_source != "manager":
        return None
    if decision.actor_source not in {
        IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
        IgPaymentReviewDecision.ActorSource.TELEGRAM_USER,
    }:
        return None
    if not str(decision.actor_external_id or "").strip():
        return None
    if not decision.confirmed_amount or decision.confirmed_amount <= 0:
        return None
    if not str(decision.currency or '').strip():
        return None
    if decision.verification_scope not in {
        IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
        IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
    }:
        return None
    return decision


def _payment_review_needs_reconciliation(review, *, projection=None):
    from management.services.ig_commercial_episodes import payment_truth_snapshot

    return payment_truth_snapshot(
        deal=review.deal if review and review.deal_id else None,
        review=review,
        order=review.order if review and review.order_id else None,
        projection=projection,
    )["needs_reconciliation"]


def _normalize_review_fit(product, raw_fit):
    value = str(raw_fit or "").strip()
    if not value:
        return "", ""
    folded = value.casefold()
    aliases = {
        "оверсайз": "oversize",
        "оверсайзний": "oversize",
        "oversized": "oversize",
        "класика": "classic",
        "класичний": "classic",
        "класична": "classic",
        "классика": "classic",
        "классический": "classic",
        "regular": "classic",
    }
    candidates = {folded, aliases.get(folded, "")}
    if product:
        fit = next(
            (
                option
                for option in ProductFitOption.objects.filter(product=product, is_active=True)
                if option.code.casefold() in candidates or option.label.casefold() in candidates
            ),
            None,
        )
        if fit:
            return fit.code, fit.label
    return aliases.get(folded, folded)[:50], value[:100]


def _build_ig_review_initial(review):
    """Build an editable manual-order draft from a confirmed IG review."""
    deal = review.deal
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get('order_draft') if isinstance(evidence.get('order_draft'), dict) else {}
    decision = _authoritative_manager_payment_decision(review)
    confirmed = Decimal(decision.confirmed_amount or 0) if decision else Decimal('0')
    merchandise = _decimal_or_none(draft.get('merchandise_total') or draft.get('quoted_total')) or Decimal(getattr(deal, 'amount', 0) or 0)
    carriage = _decimal_or_none(draft.get('delivery_amount') or draft.get('delivery_total')) or Decimal('0')
    payable = merchandise + carriage
    fully_confirmed = bool(decision and decision.verification_scope == 'full_payment' and confirmed == payable)
    payment_initial = {
        'paid_amount': f'{confirmed:.2f}',
        'merchandise_paid_amount': f'{max(confirmed - carriage, Decimal("0")) if fully_confirmed else confirmed:.2f}',
        'delivery_payer_type': 'Sender' if carriage > 0 and fully_confirmed else 'Recipient',
        'delivery_payment_method': 'Cash',
        'cod_enabled': bool(confirmed < payable),
        'handover_details': '',
    }
    if deal:
        evidence = review.evidence if isinstance(review.evidence, dict) else {}
        matches = evidence.get("catalog_matches") if isinstance(evidence.get("catalog_matches"), list) else []
        product_ids = {
            int(match.get("product_id"))
            for match in matches
            if isinstance(match, dict) and str(match.get("product_id") or "").isdigit()
        }
        try:
            from management.services.ig_payment_review import _is_review_deal_compatible

            if not _is_review_deal_compatible(deal, product_ids):
                deal = None
        except Exception:
            deal = None
    if not deal:
        evidence = review.evidence if isinstance(review.evidence, dict) else {}
        draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
        delivery = draft.get("delivery") if isinstance(draft.get("delivery"), dict) else {}
        raw_draft_items = [item for item in (draft.get("items") or []) if isinstance(item, dict)]
        product_ids = []
        for draft_item in raw_draft_items:
            catalog = draft_item.get("catalog") if isinstance(draft_item.get("catalog"), dict) else {}
            raw_product_id = draft_item.get("product_id") or catalog.get("product_id")
            try:
                product_ids.append(int(raw_product_id))
            except (TypeError, ValueError):
                continue
        products = Product.objects.in_bulk(product_ids) if product_ids else {}
        items = []
        for draft_item in raw_draft_items:
            fit = draft_item.get("fit_option_code") or draft_item.get("fit") or ""
            catalog = draft_item.get("catalog") if isinstance(draft_item.get("catalog"), dict) else {}
            try:
                product_id = int(draft_item.get("product_id") or catalog.get("product_id"))
            except (TypeError, ValueError):
                product_id = 0
            product = products.get(product_id)
            fit_code, fit_label = _normalize_review_fit(product, fit)
            title = (getattr(product, "title", "") or catalog.get("title") or draft_item.get("title") or "Товар з переписки")
            if (
                not product and fit and "не ідентифіковано" not in title.lower()
                and draft_item.get("identity_kind") != "offsite_named"
            ):
                title = f"{title} · товар потребує вибору"
            image = ""
            if product:
                try:
                    image = getattr(getattr(product, "display_image", None), "url", "") or ""
                except Exception:
                    image = ""
            reference_ids = {
                int(value) for value in (
                    draft_item.get("reference_message_ids")
                    or draft_item.get("product_reference_message_ids") or []
                ) if str(value).isdigit()
            }
            for key in ("source_message_id", "reference_message_id", "acceptance_message_id"):
                if str(draft_item.get(key) or "").isdigit():
                    reference_ids.add(int(draft_item[key]))
            option_values = dict(draft_item.get("option_values") or {}) if isinstance(draft_item.get("option_values"), dict) else {}
            if reference_ids:
                option_values["_reference_message_ids"] = sorted(reference_ids)
            if draft_item.get("garment_type"):
                option_values["garment_type"] = draft_item["garment_type"]
            raw_price = draft_item.get("unit_price")
            price_requires_input = raw_price in (None, "")
            items.append({
                "kind": "catalog" if product else "custom",
                "product_id": product.pk if product else "",
                "color_variant_id": (draft_item.get("color_variant_id") or catalog.get("color_variant_id") or "") if product else "",
                "title": title,
                "unit_price": float(raw_price) if not price_requires_input else "",
                "price_requires_input": price_requires_input,
                "qty": int(draft_item.get("qty") or 1),
                "size": draft_item.get("size") or "",
                "fit_option_code": fit_code,
                "fit_option_label": fit_label,
                "option_values": option_values,
                "option_labels": draft_item.get("option_labels") if isinstance(draft_item.get("option_labels"), dict) else {},
                "color_name": draft_item.get("color_name") or draft_item.get("color_name_custom") or draft_item.get("color") or "",
                "image": image,
                "product_url": catalog.get("url") or (f"https://twocomms.shop/product/{product.slug}/" if product else ""),
            })
        quoted_total = draft.get("quoted_total") or ""
        reasons = draft.get("uncertainty_reasons") or []
        reason_text = "; ".join({
            "catalog_product_not_identified": "товар не зіставлено з каталогом — виберіть його вручну",
            "conversation_price_not_found": "ціну з переписки не знайдено",
            "conversation_price_allocation_required": "загальну суму з переписки потрібно розподілити між позиціями вручну",
            "conversation_price_not_authorized": "ціну не підтверджено менеджером — перевірте її вручну",
        }.get(reason, reason) for reason in reasons)
        comment = "Платіж підтверджується менеджером через CRM. Дані перевірити перед створенням."
        if quoted_total:
            comment += f" Сума з переписки: {quoted_total} грн."
        if reason_text:
            comment += f" Потрібно уточнити: {reason_text}."
        if draft.get("packaging_preference"):
            comment += f" Пакування: {draft['packaging_preference']}."
        return {
            **payment_initial,
            "review_id": review.pk,
            "quoted_total": quoted_total,
            "merchandise_total": draft.get("merchandise_total") or quoted_total,
            "delivery_amount": draft.get("delivery_amount") or draft.get("delivery_total") or "",
            "payable_total": draft.get("payable_total") or quoted_total,
            "uncertainty_reasons": reasons,
            "full_name": delivery.get("full_name") or "",
            "phone": delivery.get("phone") or "",
            "delivery_method": "manual",
            "city": delivery.get("city") or "",
            "np_office": delivery.get("office") or "",
            "payment_preset": "unpaid_full",
            "sale_source": "Instagram",
            "manager_comment": comment,
            "items": items,
            **_review_shipping_initial(review),
        }
    items = []
    for item in deal.items.select_related("product", "color_variant").all():
        product = item.product
        variants = []
        sizes = []
        if product:
            try:
                sizes = list(resolve_product_sizes(product))
            except Exception:
                sizes = []
            for variant in product.color_variants.select_related("color").all():
                color = getattr(variant, "color", None)
                variants.append({
                    "id": variant.id,
                    "name": (getattr(color, "name", "") or "Колір").strip() or "Колір",
                })
        items.append({
            "kind": "catalog" if product else "custom",
            "product_id": item.product_id,
            "color_variant_id": item.color_variant_id or "",
            "title": item.title,
            "unit_price": float(item.unit_price or 0),
            "qty": item.qty,
            "size": item.size or "",
            "fit_option_code": item.fit_option_code or "",
            "fit_option_label": item.fit_option_label or "",
            "option_values": item.option_values or {},
            "option_labels": item.option_labels or {},
            "color_name": "",
            "image": getattr(getattr(product, "display_image", None), "url", "") if product else "",
            "sizes": sizes,
            "variants": variants,
        })
    return {
        **payment_initial,
        "review_id": review.pk,
        "quoted_total": str(deal.amount or ""),
        "uncertainty_reasons": [],
        "full_name": deal.np_full_name or deal.client.display_name or deal.client.username or "",
        "phone": deal.np_phone or deal.client.phone or "",
        "delivery_method": "manual",
        "city": deal.np_city or "",
        "np_office": deal.np_office or "",
        "payment_preset": "unpaid_full",
        "sale_source": "Instagram",
        "manager_comment": "Платіж підтверджено менеджером через CRM; дані перевірити перед створенням.",
        "items": items,
        **_review_shipping_initial(review),
    }


def _resolve_ig_client_context(raw_id, raw_token=""):
    """Validate the staff-only Instagram context without trusting a URL id."""
    if raw_id in (None, ""):
        return None
    try:
        client_id = int(str(raw_id).strip())
    except (TypeError, ValueError):
        raise ValueError("Некоректний Instagram-клієнт.")
    try:
        payload = signing.loads(
            str(raw_token or ""),
            salt=IG_MANUAL_CONTEXT_SALT,
            max_age=IG_MANUAL_CONTEXT_MAX_AGE,
        )
    except signing.BadSignature as exc:
        raise ValueError("Посилання на Instagram-клієнта застаріло. Відкрийте форму ще раз.") from exc
    if int(payload.get("client_id") or 0) != client_id:
        raise ValueError("Контекст Instagram-клієнта не збігається.")
    from management.ig_bot_models import IgClient

    client = IgClient.objects.filter(pk=client_id, hidden_at__isnull=True).first()
    if client is None:
        raise ValueError("Instagram-клієнта не знайдено або його приховано.")
    return client


def _form_context(*, order=None, prefill=None, ig_client=None):
    order_initial = _build_order_initial(order) if order is not None else None
    context = {
        'products_json': json.dumps(_build_products_payload(), ensure_ascii=False),
        'categories_json': json.dumps(_build_categories_payload(), ensure_ascii=False),
        'payment_presets': PAYMENT_PRESETS,
        'default_payment_preset': DEFAULT_PAYMENT_PRESET,
        'sale_source_presets': Order.SALE_SOURCE_PRESETS,
        'is_edit': order is not None,
        'edit_order_id': order.id if order is not None else None,
        'order_initial_json': json.dumps(order_initial or prefill, ensure_ascii=False) if (order_initial or prefill) else '',
        'ig_client_id': ig_client.pk if ig_client is not None else '',
        'ig_client_token': (
            signing.dumps({'client_id': ig_client.pk}, salt=IG_MANUAL_CONTEXT_SALT)
            if ig_client is not None else ''
        ),
        'ig_client_label': (
            ig_client.display_name or ig_client.username or ig_client.igsid
            if ig_client is not None else ''
        ),
    }
    return context


@contextmanager
def _manual_order_mutation(payment_review, ig_client):
    """Acquire the Instagram barrier before this entry point takes DB locks."""
    if payment_review is not None:
        from management.services.ig_payment_review import _payment_review_mutation
        with _payment_review_mutation(payment_review):
            yield
    elif ig_client is not None:
        from management.models import IgClient
        from management.services.ig_commercial_episodes import commercial_episode_client_lock
        with commercial_episode_client_lock(ig_client.pk), transaction.atomic():
            current = IgClient.objects.select_for_update().get(pk=ig_client.pk)
            if current.hidden_at is not None:
                raise ValueError('Instagram-клієнт недоступний.')
            yield
    else:
        with transaction.atomic():
            yield


@staff_member_required
@require_http_methods(["GET", "POST"])
def manual_order_create(request):
    if request.method == 'GET':
        prefill = None
        try:
            ig_client = _resolve_ig_client_context(
                request.GET.get('ig_client'),
                request.GET.get('ig_client_token'),
            )
        except ValueError:
            ig_client = None
        review_id = request.GET.get('ig_payment_review')
        if review_id:
            from management.ig_bot_models import IgPaymentConfirmationReview

            review = (
                IgPaymentConfirmationReview.objects.select_related("deal", "client")
                .filter(pk=review_id, status=IgPaymentConfirmationReview.Status.CONFIRMED, client__hidden_at__isnull=True)
                .first()
            )
            if review and _authoritative_manager_payment_decision(review):
                prefill = _build_ig_review_initial(review)
        return render(request, 'pages/admin_manual_order.html', _form_context(prefill=prefill, ig_client=ig_client))

    # POST — створення замовлення
    data = _parse_request_payload(request)
    if data is None:
        return JsonResponse({'success': False, 'message': 'Некоректний формат запиту.'}, status=400)

    try:
        ig_client = _resolve_ig_client_context(
            data.get('ig_client_id'),
            data.get('ig_client_token'),
        )
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)

    payment_review = None
    review_id = data.get('payment_review_id')
    if review_id:
        from management.ig_bot_models import IgPaymentConfirmationReview

        payment_review = (
            IgPaymentConfirmationReview.objects.select_related('deal', 'client')
            .filter(pk=review_id, status=IgPaymentConfirmationReview.Status.CONFIRMED, client__hidden_at__isnull=True)
            .first()
        )
        if not payment_review or not _authoritative_manager_payment_decision(payment_review):
            return JsonResponse({
                'success': False,
                'message': 'Підтвердження оплати не має авторизованого рішення менеджера.',
            }, status=409)
        if _payment_review_needs_reconciliation(payment_review):
            return JsonResponse({
                'success': False,
                'message': 'Потрібна звірка сум оплати перед створенням замовлення.',
            }, status=409)
        if ig_client is not None and payment_review.client_id != ig_client.pk:
            return JsonResponse({
                'success': False,
                'message': 'Instagram-клієнт не збігається з перевіркою оплати.',
            }, status=409)
        if payment_review.order_id:
            existing = payment_review.order
            return JsonResponse({
                'success': True,
                'message': f'Для цієї перевірки вже створено замовлення #{existing.order_number}.',
                'order_id': existing.id,
                'order_number': existing.order_number,
                'redirect_url': f"{reverse('admin_panel')}?section=orders",
            })

    full_name = str(data.get('full_name') or '').strip()
    raw_phone = str(data.get('phone') or '').strip()
    if not full_name:
        return JsonResponse({'success': False, 'message': 'Вкажіть ПІБ клієнта.'}, status=422)

    phone = normalize_checkout_phone(raw_phone)
    if not phone:
        return JsonResponse(
            {'success': False, 'message': 'Вкажіть коректний український номер телефону. Можна без +380.'},
            status=422,
        )

    try:
        delivery = _resolve_delivery(data, allow_keep=False)
    except _DeliveryError as exc:
        return JsonResponse({'success': False, 'message': exc.message, 'field': exc.field}, status=422)

    try:
        raw_items, products_map, variants_map = _collect_items(data.get('items'))
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)

    preset_key = str(data.get('payment_preset') or DEFAULT_PAYMENT_PRESET).strip()
    if payment_review:
        # A manager-confirmed screenshot authorizes order preparation, not paid
        # revenue. Provider/manual-ledger truth must transition payment later.
        preset_key = 'unpaid_full'
    if preset_key not in PAYMENT_PRESETS:
        return JsonResponse({'success': False, 'message': 'Оберіть коректний спосіб оплати.'}, status=422)
    preset = PAYMENT_PRESETS[preset_key]
    sale_source = str(data.get('sale_source') or '').strip()[:120]
    manager_comment = str(data.get('manager_comment') or '').strip()
    try:
        with _manual_order_mutation(payment_review, ig_client):
            manager_decision = None
            if payment_review:
                from management.ig_bot_models import IgPaymentConfirmationReview

                from management.services.ig_payment_review import _lock_payment_review
                payment_review = _lock_payment_review(payment_review)
                if (
                    payment_review.status != IgPaymentConfirmationReview.Status.CONFIRMED
                    or payment_review.client.hidden_at is not None
                ):
                    raise ValueError('Підтвердження оплати недоступне.')
                manager_decision = (
                    _authoritative_manager_payment_decision(payment_review)
                    if payment_review
                    else None
                )
                if not payment_review or not manager_decision:
                    raise ValueError(
                        'Підтвердження оплати не має авторизованого рішення менеджера.'
                    )
                projection = None
                if payment_review.deal_id:
                    from management.ig_bot_models import IgPaymentProjection

                    projection = IgPaymentProjection.objects.select_for_update().filter(
                        deal_id=payment_review.deal_id
                    ).first()
                if _payment_review_needs_reconciliation(
                    payment_review,
                    projection=projection,
                ):
                    raise ValueError(
                        'Потрібна звірка сум оплати перед створенням замовлення.'
                    )
                if payment_review.order_id:
                    existing = payment_review.order
                    return JsonResponse({
                        'success': True,
                        'message': f'Для цієї перевірки вже створено замовлення #{existing.order_number}.',
                        'order_id': existing.id,
                        'order_number': existing.order_number,
                        'redirect_url': f"{reverse('admin_panel')}?section=orders",
                    })
            provisional_order = Order()
            order_items = []
            total_sum = Decimal('0.00')
            for raw_item in raw_items:
                item = _build_order_item(
                    raw_item,
                    order=provisional_order,
                    products_map=products_map,
                    variants_map=variants_map,
                )
                order_items.append(item)
                total_sum += item.line_total
            total_sum = total_sum.quantize(Decimal('0.01'))
            _validate_order_total(total_sum)
            quoted_total = _review_quoted_total(payment_review)
            price_override = (
                _price_override_payload(
                    data,
                    quoted_total=quoted_total,
                    actual_total=total_sum,
                    actor=request.user,
                    review=payment_review,
                )
                if payment_review
                else None
            )
            policy = _review_shipping_policy(payment_review)
            explicit_shipping = 'delivery_payment_mode' in data or 'delivery_charge_amount' in data
            expected = _review_shipping_initial(payment_review) if payment_review else {}
            if expected.get('delivery_payment_locked') and explicit_shipping:
                if (
                    data.get('delivery_payment_mode') != expected['delivery_payment_mode']
                    or _decimal_or_none(data.get('delivery_charge_amount')) != _decimal_or_none(expected['delivery_charge_amount'])
                ):
                    raise ValueError('Доставка має відповідати підтвердженій домовленості з переписки.')
            if payment_review and policy.get('mode') != 'unknown':
                delivery_contract = _review_delivery_contract(
                    payment_review, merchandise_total=total_sum, actor=request.user, item_rows=order_items,
                )
                if delivery_contract is None and explicit_shipping:
                    delivery_contract = _manual_delivery_contract(
                        data, merchandise_total=total_sum, actor=request.user, preset_key=preset_key,
                        item_rows=order_items, review=payment_review,
                        confirmed_amount=getattr(manager_decision, 'confirmed_amount', None),
                    )
            else:
                delivery_contract = _manual_delivery_contract(
                    data, merchandise_total=total_sum, actor=request.user, preset_key=preset_key,
                    item_rows=order_items, review=payment_review,
                    confirmed_amount=getattr(manager_decision, 'confirmed_amount', None),
                )
                if payment_review and policy.get('mode') == 'unknown' and delivery_contract is None:
                    raise ValueError('Уточніть спосіб оплати доставки перед створенням замовлення.')
            payable_total = total_sum + (
                Decimal(delivery_contract['delivery_amount']) if delivery_contract else Decimal('0.00')
            )
            payment_episode = None
            if payment_review:
                from management.services.ig_commercial_episodes import ensure_episode_for_review

                payment_episode = ensure_episode_for_review(payment_review)
            manager_confirmed_amount = (
                Decimal(manager_decision.confirmed_amount).quantize(Decimal('0.01'))
                if manager_decision is not None
                else Decimal('0.00')
            )
            manager_payment_payload = {}
            if manager_decision is not None:
                manager_payment_payload = {
                    'manager_payment_decision_id': manager_decision.pk,
                    'manager_confirmed_amount': f'{manager_confirmed_amount:.2f}',
                    'manager_verification_scope': manager_decision.verification_scope,
                    'manager_verification_source': manager_decision.verification_source,
                    'manager_amount_source': manager_decision.amount_source or '',
                    'manager_amount_evidence_message_ids': (
                        manager_decision.amount_evidence_message_ids or []
                    ),
                    'manager_payment_currency': manager_decision.currency or 'UAH',
                    'effective_confirmed_amount': f'{manager_confirmed_amount:.2f}',
                    'negotiated_order_total': (
                        f'{quoted_total:.2f}' if quoted_total is not None else f'{total_sum:.2f}'
                    ),
                }
            effective_pay_type = preset['pay_type']
            effective_preset_key = preset_key
            if manager_decision is not None:
                effective_pay_type = (
                    'prepayment'
                    if manager_confirmed_amount < payable_total
                    else 'online_full'
                )
                effective_preset_key = (
                    'manager_prepayment'
                    if effective_pay_type == 'prepayment'
                    else 'unpaid_full'
                )
            provisional_order.delivery_method = delivery['delivery_method']
            shipment_payment = (
                _manual_payment_controls(
                    data, order=provisional_order, preset_key=preset_key,
                    merchandise_total=total_sum,
                )
                if not payment_review else None
            )
            order_defaults = {
                'user': None,
                'full_name': full_name[:200],
                'phone': phone,
                'city': delivery['city'],
                'np_office': delivery['np_office'],
                'delivery_method': delivery['delivery_method'],
                'handover_details': delivery['handover_details'],
                'pay_type': effective_pay_type,
                'payment_status': preset['payment_status'],
                'status': 'new',
                'source': 'manual',
                'created_by': request.user,
                'sale_source': sale_source,
                'manager_comment': manager_comment,
                'payment_payload': {
                    'manual_payment_preset': effective_preset_key,
                    **({'delivery_payment': delivery_contract} if delivery_contract else {}),
                    **({'instagram_delivery_contract': _legacy_delivery_alias(delivery_contract)} if delivery_contract and payment_review and delivery_contract.get('mode') == 'customer_prepaid' else {}),
                    **({'manual_shipment_payment': shipment_payment} if shipment_payment else {}),
                    **({'paid_value': shipment_payment['paid_amount']} if preset_key == 'partial_manual' and shipment_payment else {}),
                    **({'manual_payment_action': {
                        'actor_id': request.user.pk,
                        'source': 'management_user',
                        'payment_status': preset['payment_status'],
                        'payment_preset': preset_key,
                        'recorded_at': timezone.now().isoformat(),
                    }} if not payment_review and preset['payment_status'] in {'paid', 'prepaid'} else {}),
                    **({
                        'instagram_payment_review_id': payment_review.pk,
                        'instagram_commercial_episode_id': payment_episode.pk,
                        'manual_payment_evidence_confirmed': True,
                        'provider_payment_confirmed': False,
                        **manager_payment_payload,
                        **({'instagram_price_override': price_override} if price_override else {}),
                    } if payment_review else {}),
                },
                'total_sum': total_sum,
            }
            if payment_episode:
                order, order_created = Order.objects.get_or_create(
                    checkout_idempotency_key=f'ig-episode:{payment_episode.pk}',
                    defaults=order_defaults,
                )
            else:
                order = Order(**order_defaults)
                order_created = True
            if not order_created and payment_episode:
                from orders.services.order_builder import (
                    assert_order_matches_commercial_contract,
                )

                assert_order_matches_commercial_contract(
                    order,
                    expected_fields={
                        'full_name': full_name[:200],
                        'phone': phone,
                        'city': delivery['city'],
                        'np_office': delivery['np_office'],
                    },
                    expected_items=order_items,
                    declared_total=total_sum,
                )
                _assert_delivery_contract_compatible(order, delivery_contract)
            if order_created:
                _apply_delivery(order, delivery)
                order.save()
            elif payment_review:
                from management.services.ig_order_links import create_order_attribution

                payment_review.order = order
                payment_review.save(update_fields=['order', 'updated_at'])
                create_order_attribution(
                    order,
                    client=payment_review.client,
                    deal=payment_review.deal,
                    review=payment_review,
                    manager_decision=manager_decision,
                    creation_mode="manager_review",
                    payment_source="manager_verified",
                    created_by=request.user,
                )
            if order_created:
                for item in order_items:
                    item.order = order
                OrderItem.objects.bulk_create(order_items)
            order.total_sum = total_sum
            if price_override:
                payload = dict(order.payment_payload or {})
                payload['instagram_price_override'] = price_override
                order.payment_payload = payload
                order.save(update_fields=['total_sum', 'payment_payload'])
            else:
                order.save(update_fields=['total_sum'])
            ensure_order_purchase_action(
                order,
                metadata={
                    'source': 'manual_admin',
                    'payment_preset': preset_key,
                },
                raise_errors=True,
            )
            if payment_review and payment_review.deal_id:
                from management.ig_bot_models import IgDeal
                from management.services.ig_payment_review import _is_review_deal_compatible

                deal = IgDeal.objects.select_for_update().get(pk=payment_review.deal_id)
                product_ids = {item.product_id for item in order_items if item.product_id}
                if _is_review_deal_compatible(deal, product_ids):
                    deal.order = order
                    deal.status = IgDeal.Status.ORDER_CREATED
                    deal.order_truth_updated_at = timezone.now()
                    deal.save(update_fields=['order', 'status', 'order_truth_updated_at', 'updated_at'])
                else:
                    # A historical/paid/conflicting deal must never absorb a
                    # new manual order; keep the review-level order link as
                    # the sole idempotency key for this payment evidence.
                    payment_review.deal = None
                    payment_review.save(update_fields=['deal', 'updated_at'])
            if payment_review:
                from management.services.ig_order_links import create_order_attribution

                manager_decision = _authoritative_manager_payment_decision(payment_review)
                payment_review.order = order
                payment_review.save(update_fields=['order', 'updated_at'])
                create_order_attribution(
                    order,
                    client=payment_review.client,
                    deal=payment_review.deal,
                    review=payment_review,
                    manager_decision=manager_decision,
                    creation_mode="manager_review",
                    payment_source="manager_verified",
                    created_by=request.user,
                )
            if ig_client is not None:
                from management.ig_bot_models import (
                    IgOrderAssignment,
                    IgOrderAssignmentEvent,
                )
                from management.services.ig_order_assignments import (
                    link_order_to_client,
                )

                link_order_to_client(
                    order,
                    client=ig_client,
                    actor=request.user,
                    source=IgOrderAssignment.Source.MANAGER_CREATED,
                    actor_source=IgOrderAssignmentEvent.ActorSource.MANAGEMENT_USER,
                    reason_code='manual_order_context',
                    reason='Order created from the Instagram client workspace',
                )
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    except Exception:
        logger.exception('Failed to create manual order')
        return JsonResponse(
            {'success': False, 'message': 'Не вдалося створити замовлення через внутрішню помилку.'},
            status=500,
        )

    if order_created:
        try:
            telegram_notifier.send_new_order_notification(order)
        except Exception:
            logger.exception('Failed to send Telegram notification for manual order %s', order.pk)

    return JsonResponse({
        'success': True,
        'message': f'Замовлення #{order.order_number} створено.',
        'order_id': order.id,
        'order_number': order.order_number,
        'delivery_address': delivery['display'],
        'redirect_url': f"{reverse('admin_panel')}?section=orders",
    })


@staff_member_required
@require_http_methods(["GET", "POST"])
def manual_order_edit(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related('items__product', 'items__color_variant__color'),
        pk=order_id,
    )

    if request.method == 'GET':
        # Редагування виконується через drawer у списку замовлень
        # (кнопка «Редагувати» на картці). Глибокий лінк відкриває drawer.
        return redirect(f"{reverse('admin_panel')}?section=orders&edit_order={order.id}")

    # POST — оновлення замовлення
    data = _parse_request_payload(request)
    if data is None:
        return JsonResponse({'success': False, 'message': 'Некоректний формат запиту.'}, status=400)

    full_name = str(data.get('full_name') or '').strip()
    raw_phone = str(data.get('phone') or '').strip()
    if not full_name:
        return JsonResponse({'success': False, 'message': 'Вкажіть ПІБ клієнта.'}, status=422)

    phone = normalize_checkout_phone(raw_phone)
    if not phone:
        return JsonResponse(
            {'success': False, 'message': 'Вкажіть коректний український номер телефону. Можна без +380.'},
            status=422,
        )

    try:
        delivery = _resolve_delivery(data, allow_keep=True)  # None означає «не змінювати»
    except _DeliveryError as exc:
        return JsonResponse({'success': False, 'message': exc.message, 'field': exc.field}, status=422)

    try:
        raw_items, products_map, variants_map = _collect_items(data.get('items'))
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)

    preset_key = str(data.get('payment_preset') or _preset_key_for_order(order)).strip()
    if preset_key not in PAYMENT_PRESETS:
        return JsonResponse({'success': False, 'message': 'Оберіть коректний спосіб оплати.'}, status=422)
    preset = PAYMENT_PRESETS[preset_key]
    sale_source = str(data.get('sale_source') or '').strip()[:120]
    manager_comment = str(data.get('manager_comment') or '').strip()

    try:
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            before_snapshot = snapshot_order(locked)
            from orders.services.delivery_payment import delivery_payment_snapshot
            shipping_before = delivery_payment_snapshot(locked, item_rows=list(locked.items.all()))
            old_payment_payload = dict(locked.payment_payload or {})
            old_pay_type, old_payment_status = locked.pay_type, locked.payment_status
            locked.full_name = full_name[:200]
            locked.phone = phone
            locked.pay_type = preset['pay_type']
            locked.payment_status = preset['payment_status']
            locked.sale_source = sale_source
            locked.manager_comment = manager_comment
            payment_payload = dict(locked.payment_payload or {})
            payment_payload['manual_payment_preset'] = preset_key
            if preset['payment_status'] in {'paid', 'prepaid'}:
                payment_payload['manual_payment_action'] = {
                    'actor_id': request.user.pk,
                    'source': 'management_user',
                    'payment_status': preset['payment_status'],
                    'payment_preset': preset_key,
                    'recorded_at': timezone.now().isoformat(),
                }
            locked.payment_payload = payment_payload

            if delivery is not None:
                _apply_delivery(locked, delivery)
                delivery_display = delivery['display']
            else:
                delivery_display = locked.handover_details if locked.delivery_method == 'handover' else ', '.join(p for p in (locked.city, locked.np_office) if p)

            # Пересоздаём позиции
            historical_items_by_id = {
                existing.id: (
                    existing.product_id,
                    existing.color_variant_id,
                    existing.fit_option_code,
                    existing.size,
                )
                for existing in locked.items.all()
                if existing.product_id and existing.color_variant_id and existing.fit_option_code
            }
            locked.items.all().delete()
            order_items = []
            total_sum = Decimal('0')
            for raw_item in raw_items:
                item = _build_order_item(
                    raw_item,
                    order=locked,
                    products_map=products_map,
                    variants_map=variants_map,
                    historical_items_by_id=historical_items_by_id,
                )
                order_items.append(item)
                total_sum += item.line_total
            _validate_order_total(total_sum)
            # Use the original preset when considering retained shipment controls.
            locked.payment_payload = old_payment_payload
            locked.pay_type, locked.payment_status = old_pay_type, old_payment_status
            shipment_payment = _manual_payment_controls(
                data, order=locked, preset_key=preset_key,
                merchandise_total=max(total_sum - Decimal(locked.discount_amount or 0), Decimal('0.00')),
            )
            if shipment_payment:
                payment_payload['manual_shipment_payment'] = shipment_payment
                if preset_key == 'partial_manual':
                    payment_payload['paid_value'] = shipment_payment['paid_amount']
                elif old_payment_payload.get('manual_payment_preset') == 'partial_manual':
                    payment_payload.pop('paid_value', None)
            else:
                payment_payload.pop('manual_shipment_payment', None)
            if old_payment_payload.get('manual_payment_preset') == 'partial_manual' and preset_key != 'partial_manual':
                payment_payload.pop('paid_value', None)
            locked.payment_payload = payment_payload
            locked.pay_type, locked.payment_status = preset['pay_type'], preset['payment_status']
            OrderItem.objects.bulk_create(order_items)
            merchandise_total = max(total_sum - Decimal(locked.discount_amount or 0), Decimal('0.00'))
            delivery_contract = _manual_delivery_contract(
                data, merchandise_total=merchandise_total, actor=request.user, preset_key=preset_key,
                item_rows=order_items, existing=locked, discount_amount=locked.discount_amount or 0,
                existing_snapshot=shipping_before,
            )
            if delivery_contract is not None:
                payment_payload = dict(locked.payment_payload or {})
                payment_payload['delivery_payment'] = delivery_contract
                # A new explicit audited choice supersedes the old bridge.
                payment_payload.pop('instagram_delivery_contract', None)
                locked.payment_payload = payment_payload
            locked.total_sum = total_sum
            locked.save()
            if preset_key == 'free':
                # This is an explicit staff reclassification, not a refund:
                # any internal purchase for this manual order is invalid.
                remove_order_purchase_action(locked)
            else:
                ensure_order_purchase_action(
                    locked,
                    metadata={
                        'source': 'manual_admin',
                        'payment_preset': preset_key,
                    },
                    raise_errors=True,
                )
            order = locked
            after_snapshot = snapshot_order(order)
    except ValueError as exc:
        return JsonResponse({'success': False, 'message': str(exc)}, status=422)
    except Exception:
        logger.exception('Failed to edit order %s', order_id)
        return JsonResponse(
            {'success': False, 'message': 'Не вдалося зберегти зміни через внутрішню помилку.'},
            status=500,
        )

    edit_diff = build_order_edit_diff(before_snapshot, after_snapshot)

    # Оновлюємо існуюче Telegram-повідомлення (а не шлемо нове).
    try:
        telegram_notifier.update_order_notification_message(order)
    except Exception:
        logger.exception('Failed to update Telegram notification for order %s', order.pk)

    # Окреме сповіщення адмінам зі списком змін (лише якщо реально щось змінилось).
    changed_by = request.user.get_full_name() or request.user.get_username()
    try:
        telegram_notifier.send_order_edit_notification(order, edit_diff, changed_by=changed_by)
    except Exception:
        logger.exception('Failed to send order edit diff notification for order %s', order.pk)

    return JsonResponse({
        'success': True,
        'message': f'Замовлення #{order.order_number} оновлено.',
        'order_id': order.id,
        'order_number': order.order_number,
        'delivery_address': delivery_display,
        'redirect_url': f"{reverse('admin_panel')}?section=orders",
    })


@staff_member_required
@require_http_methods(["GET"])
def manual_order_edit_data(request, order_id):
    """JSON-дані для drawer редагування замовлення в кастомній адмінці.

    Повертає поточний стан замовлення (позиції, клієнт, доставка, оплата)
    разом із каталогом товарів — щоб drawer міг ліниво підвантажити все
    одним запитом при відкритті, не роздуваючи список замовлень.
    """
    order = get_object_or_404(
        Order.objects.prefetch_related('items__product', 'items__color_variant__color'),
        pk=order_id,
    )
    return JsonResponse({
        'success': True,
        'order': _build_order_initial(order),
        'products': _build_products_payload(),
        'categories': _build_categories_payload(),
        'payment_presets': {key: preset['label'] for key, preset in PAYMENT_PRESETS.items()},
        'default_payment_preset': DEFAULT_PAYMENT_PRESET,
        'current_payment_preset': _preset_key_for_order(order),
        'sale_source_presets': list(Order.SALE_SOURCE_PRESETS),
    })
