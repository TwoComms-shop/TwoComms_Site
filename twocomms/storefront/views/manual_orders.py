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
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from orders.models import Order, OrderItem
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
from storefront.models import Product, ProductStatus
from storefront.services.size_guides import resolve_product_sizes
from storefront.utm_tracking import (
    ensure_order_purchase_action,
    remove_order_purchase_action,
)

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_ORDER = 50
MAX_QTY_PER_ITEM = 999

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
    'free': {
        'label': 'Безкоштовно / подарунок',
        'pay_type': 'online_full',
        'payment_status': 'paid',
    },
}
DEFAULT_PAYMENT_PRESET = 'cod'


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
                'product_id': item.product_id,
                'color_variant_id': item.color_variant_id or '',
                'title': item.title,
                'unit_price': float(item.unit_price or 0),
                'qty': item.qty,
                'size': item.size or '',
                'image': image,
            })
    return {
        'id': order.id,
        'order_number': order.order_number,
        'full_name': order.full_name or '',
        'phone': order.phone or '',
        'sale_source': order.sale_source or '',
        'manager_comment': order.manager_comment or '',
        'payment_preset': _preset_key_for_order(order),
        'delivery_text': ', '.join(p for p in (order.city, order.np_office) if p),
        'has_tracking': bool(order.tracking_number),
        'items': items,
    }


def _build_products_payload():
    """Серіалізує товари каталогу для вибору в формі (inline JSON).

    Товарів у магазині небагато (десятки), тож віддаємо одразу на сторінку
    і фільтруємо на клієнті — без додаткових запитів до сервера. Кожен
    товар містить ``category`` (id + назва) для групування в пікері.
    """
    products = (
        Product.objects.exclude(status=ProductStatus.ARCHIVED)
        .select_related('category')
        .prefetch_related('color_variants__color', 'color_variants__images')
        .order_by('category__order', 'category__name', 'title')
    )
    payload = []
    for product in products:
        image = product.display_image
        image_url = getattr(image, 'url', '') if image else ''

        variants = []
        for variant in product.color_variants.all():
            color = getattr(variant, 'color', None)
            variants.append({
                'id': variant.id,
                'name': (getattr(color, 'name', '') or '').strip() or 'Колір',
                'primary_hex': getattr(color, 'primary_hex', '') or '',
                'secondary_hex': getattr(color, 'secondary_hex', '') or '',
            })

        try:
            sizes = list(resolve_product_sizes(product))
        except Exception:  # pragma: no cover - захист від нестандартних сіток
            sizes = []

        category = getattr(product, 'category', None)
        payload.append({
            'id': product.id,
            'title': product.title,
            'price': int(product.final_price or 0),
            'image': image_url,
            'sizes': sizes,
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


def _coerce_int(raw, *, default=1, minimum=1, maximum=MAX_QTY_PER_ITEM):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _resolve_fit_payload(product, requested_code):
    """Повертає (code, label) обраного крою товару, як у дропшип-формі."""
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
    selected = next((o for o in options if o.code == requested), None)
    if selected is None:
        selected = next((o for o in options if o.is_default), options[0])
    return selected.code, selected.label


def _build_order_item(raw_item, *, order, products_map, variants_map):
    """Будує (без збереження) OrderItem з одного запису форми.

    Кидає ``ValueError`` з людським повідомленням при некоректних даних.
    """
    kind = str(raw_item.get('kind') or 'catalog').strip()
    qty = _coerce_int(raw_item.get('qty'), default=1)
    size = str(raw_item.get('size') or '').strip()[:16]
    unit_price = _decimal_or_none(raw_item.get('unit_price'))

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

    fit_code, fit_label = _resolve_fit_payload(product, raw_item.get('fit_option_code') or raw_item.get('fit_option'))

    return OrderItem(
        order=order,
        product=product,
        color_variant=variant,
        title=product.title[:200],
        size=size,
        fit_option_code=fit_code,
        fit_option_label=fit_label,
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


def _build_ig_review_initial(review):
    """Build an editable manual-order draft from a confirmed IG review."""
    deal = review.deal
    if not deal:
        return {"review_id": review.pk, "items": [], "delivery_method": "manual"}
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
            "color_name": "",
            "image": getattr(getattr(product, "display_image", None), "url", "") if product else "",
            "sizes": sizes,
            "variants": variants,
        })
    return {
        "review_id": review.pk,
        "full_name": deal.np_full_name or deal.client.display_name or deal.client.username or "",
        "phone": deal.np_phone or deal.client.phone or "",
        "delivery_method": "manual",
        "city": deal.np_city or "",
        "np_office": deal.np_office or "",
        "payment_preset": "paid_full",
        "sale_source": "Instagram",
        "manager_comment": "Платіж підтверджено менеджером через CRM; дані перевірити перед створенням.",
        "items": items,
    }


def _form_context(*, order=None, prefill=None):
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
    }
    return context


@staff_member_required
@require_http_methods(["GET", "POST"])
def manual_order_create(request):
    if request.method == 'GET':
        prefill = None
        review_id = request.GET.get('ig_payment_review')
        if review_id:
            from management.ig_bot_models import IgPaymentConfirmationReview

            review = (
                IgPaymentConfirmationReview.objects.select_related("deal", "client")
                .filter(pk=review_id, status=IgPaymentConfirmationReview.Status.CONFIRMED, client__hidden_at__isnull=True)
                .first()
            )
            if review:
                prefill = _build_ig_review_initial(review)
        return render(request, 'pages/admin_manual_order.html', _form_context(prefill=prefill))

    # POST — створення замовлення
    data = _parse_request_payload(request)
    if data is None:
        return JsonResponse({'success': False, 'message': 'Некоректний формат запиту.'}, status=400)

    payment_review = None
    review_id = data.get('payment_review_id')
    if review_id:
        from management.ig_bot_models import IgPaymentConfirmationReview

        payment_review = (
            IgPaymentConfirmationReview.objects.select_related('deal', 'client')
            .filter(pk=review_id, status=IgPaymentConfirmationReview.Status.CONFIRMED, client__hidden_at__isnull=True)
            .first()
        )
        if not payment_review:
            return JsonResponse({'success': False, 'message': 'Підтвердження оплати недійсне або вже скасоване.'}, status=409)
        if payment_review.deal_id and payment_review.deal.order_id:
            return JsonResponse({
                'success': True,
                'message': f'Для цієї угоди вже створено замовлення #{payment_review.deal.order.order_number}.',
                'order_id': payment_review.deal.order_id,
                'order_number': payment_review.deal.order.order_number,
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
    preset = PAYMENT_PRESETS.get(preset_key, PAYMENT_PRESETS[DEFAULT_PAYMENT_PRESET])
    sale_source = str(data.get('sale_source') or '').strip()[:120]
    manager_comment = str(data.get('manager_comment') or '').strip()

    try:
        with transaction.atomic():
            order = Order(
                user=None,
                full_name=full_name[:200],
                phone=phone,
                city=delivery['city'],
                np_office=delivery['np_office'],
                pay_type=preset['pay_type'],
                payment_status=preset['payment_status'],
                status='new',
                source='manual',
                created_by=request.user,
                sale_source=sale_source,
                manager_comment=manager_comment,
                payment_payload={'manual_payment_preset': preset_key},
            )
            apply_nova_poshta_refs(order, delivery['refs'])
            order.save()

            order_items = []
            total_sum = Decimal('0')
            for raw_item in raw_items:
                item = _build_order_item(raw_item, order=order, products_map=products_map, variants_map=variants_map)
                order_items.append(item)
                total_sum += item.line_total

            OrderItem.objects.bulk_create(order_items)
            order.total_sum = total_sum
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

                deal = IgDeal.objects.select_for_update().get(pk=payment_review.deal_id)
                if deal.order_id and deal.order_id != order.id:
                    raise ValueError('Для цієї перевірки вже створено замовлення.')
                deal.order = order
                deal.status = IgDeal.Status.ORDER_CREATED
                deal.order_truth_updated_at = timezone.now()
                deal.save(update_fields=['order', 'status', 'order_truth_updated_at', 'updated_at'])
                payment_review.save(update_fields=['updated_at'])
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
            locked.items.all().delete()
            order_items = []
            total_sum = Decimal('0')
            for raw_item in raw_items:
                item = _build_order_item(raw_item, order=locked, products_map=products_map, variants_map=variants_map)
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
