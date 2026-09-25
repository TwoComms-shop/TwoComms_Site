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
from decimal import Decimal, InvalidOperation

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
from orders.nova_poshta_documents import normalize_checkout_phone
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
    if payment_status in ('prepaid', 'partial'):
        return 'prepaid_200'
    if pay_type == 'prepayment' and payment_payload.get('manager_confirmed_amount'):
        return 'manager_prepayment'
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
        if item.is_custom or not item.product_id:
            items.append({
                'kind': 'custom',
                'item_id': item.id,
                'title': item.title,
                'unit_price': float(item.unit_price or 0),
                'qty': item.qty,
                'size': item.size or '',
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
                'image': image,
            })
    delivery_display = get_order_nova_poshta_point(order)
    return {
        'id': order.id,
        'order_number': order.order_number,
        'full_name': order.full_name or '',
        'phone': order.phone or '',
        'sale_source': order.sale_source or '',
        'manager_comment': order.manager_comment or '',
        'payment_preset': _preset_key_for_order(order),
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
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value.quantize(Decimal('0.01'))


def _review_quoted_total(review):
    if not review:
        return None
    if review.deal_id:
        amount = _decimal_or_none(review.deal.amount)
        if amount is not None and amount > 0:
            return amount
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get('order_draft') if isinstance(evidence.get('order_draft'), dict) else {}
    amount = _decimal_or_none(draft.get('quoted_total'))
    return amount if amount is not None and amount > 0 else None


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
            return json.loads(request.body or '{}')
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

    if delivery_method == 'manual':
        city = str(data.get('city') or '').strip()[:100]
        np_office = str(data.get('np_office') or '').strip()[:200]
        if not city or not np_office:
            raise _DeliveryError('Вкажіть місто та адресу/відділення доставки.', field='np_office')
        return {
            'city': city,
            'np_office': np_office,
            'refs': {'np_settlement_ref': '', 'np_city_ref': '', 'np_warehouse_ref': ''},
            'display': ', '.join(p for p in (city, np_office) if p),
        }

    # Нова Пошта
    try:
        selection = resolve_delivery_selection(data)
    except NovaPoshtaSelectionError as exc:
        raise _DeliveryError(exc.message, field=exc.field)
    return {
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
        if str(raw_item.get('kind') or 'catalog').strip() == 'custom':
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
            fit = draft_item.get("fit") or ""
            catalog = draft_item.get("catalog") if isinstance(draft_item.get("catalog"), dict) else {}
            try:
                product_id = int(draft_item.get("product_id") or catalog.get("product_id"))
            except (TypeError, ValueError):
                product_id = 0
            product = products.get(product_id)
            fit_code, fit_label = _normalize_review_fit(product, fit)
            title = (getattr(product, "title", "") or catalog.get("title") or draft_item.get("title") or "Товар з переписки")
            if not product and fit and "не ідентифіковано" not in title.lower():
                title = f"{title} · товар потребує вибору"
            image = ""
            if product:
                try:
                    image = getattr(getattr(product, "display_image", None), "url", "") or ""
                except Exception:
                    image = ""
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
                "option_values": draft_item.get("option_values") if isinstance(draft_item.get("option_values"), dict) else {},
                "option_labels": draft_item.get("option_labels") if isinstance(draft_item.get("option_labels"), dict) else {},
                "color_name": "",
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
            "review_id": review.pk,
            "quoted_total": quoted_total,
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
    preset = PAYMENT_PRESETS.get(preset_key, PAYMENT_PRESETS[DEFAULT_PAYMENT_PRESET])
    sale_source = str(data.get('sale_source') or '').strip()[:120]
    manager_comment = str(data.get('manager_comment') or '').strip()
    try:
        with transaction.atomic():
            manager_decision = None
            if payment_review:
                from management.ig_bot_models import IgPaymentConfirmationReview

                payment_review = (
                    IgPaymentConfirmationReview.objects.select_for_update()
                    .select_related("deal", "order")
                    .filter(
                        pk=payment_review.pk,
                        status=IgPaymentConfirmationReview.Status.CONFIRMED,
                        client__hidden_at__isnull=True,
                    )
                    .first()
                )
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
                    if manager_confirmed_amount < total_sum
                    else 'online_full'
                )
                effective_preset_key = (
                    'manager_prepayment'
                    if effective_pay_type == 'prepayment'
                    else 'unpaid_full'
                )
            order_defaults = {
                'user': None,
                'full_name': full_name[:200],
                'phone': phone,
                'city': delivery['city'],
                'np_office': delivery['np_office'],
                'pay_type': effective_pay_type,
                'payment_status': preset['payment_status'],
                'status': 'new',
                'source': 'manual',
                'created_by': request.user,
                'sale_source': sale_source,
                'manager_comment': manager_comment,
                'payment_payload': {
                    'manual_payment_preset': effective_preset_key,
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
            if order_created:
                apply_nova_poshta_refs(order, delivery['refs'])
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
    preset = PAYMENT_PRESETS.get(preset_key, PAYMENT_PRESETS[DEFAULT_PAYMENT_PRESET])
    sale_source = str(data.get('sale_source') or '').strip()[:120]
    manager_comment = str(data.get('manager_comment') or '').strip()

    try:
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            before_snapshot = snapshot_order(locked)
            locked.full_name = full_name[:200]
            locked.phone = phone
            locked.pay_type = preset['pay_type']
            locked.payment_status = preset['payment_status']
            locked.sale_source = sale_source
            locked.manager_comment = manager_comment
            payment_payload = dict(locked.payment_payload or {})
            payment_payload['manual_payment_preset'] = preset_key
            locked.payment_payload = payment_payload

            if delivery is not None:
                locked.city = delivery['city']
                locked.np_office = delivery['np_office']
                apply_nova_poshta_refs(locked, delivery['refs'])
                delivery_display = delivery['display']
            else:
                delivery_display = ', '.join(p for p in (locked.city, locked.np_office) if p)

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
            OrderItem.objects.bulk_create(order_items)
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
