"""
Monobank payment integration - Интеграция платежей Monobank.

Содержит views и helper функции для:
- Создания инвойсов (invoice API)
- Финализации инвойсов (invoice finalize API)
- Checkout API (быстрые платежи)
- Обработки webhooks
- Проверки статусов платежей
- Работы с Monobank API

Monobank документация: https://api.monobank.ua/docs/
API финализации: https://monobank.ua/api-docs/acquiring/methods/ia/post--api--merchant--invoice--finalize
"""

import logging
import json
import base64
import hashlib
import threading
from urllib.parse import urlparse
from decimal import Decimal
from datetime import timedelta

from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.utils import timezone
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.contrib import messages

import requests

from ..models import Product, PromoCode
from orders.nova_poshta_data import apply_nova_poshta_refs
from orders.nova_poshta_documents import normalize_checkout_phone
from orders.models import Order as OrderModel, OrderItem, PaymentAttempt
from orders.payment_attempts import materialize_payment_attempt, PaymentAttemptConversionError
from orders.nova_poshta_checkout import NovaPoshtaSelectionError, resolve_delivery_selection
from orders.facebook_conversions_service import get_facebook_conversions_service
from accounts.payment import normalize_pay_type
from storefront.services.checkout_capture import mark_checkout_capture_converted
from ..utm_tracking import (
    ensure_order_purchase_action,
    ensure_request_session_key,
    link_order_to_utm,
    record_initiate_checkout,
    record_lead,
)
from .utils import (
    _dispatch_post_payment_events,
    _reset_monobank_session,
    get_validated_cart_from_session,
    _get_color_variant_safe,
    _normalize_order_pay_type,
)


# Loggers
monobank_logger = logging.getLogger('storefront.monobank')
cart_logger = logging.getLogger('storefront.cart')
CLIENT_TRACKING_ALLOWED_KEYS = frozenset({'fbp', 'fbc'})


def _capi_checkout_source_url(request):
    """Return the checkout page URL, never the invoice API endpoint."""
    referer = (request.META.get('HTTP_REFERER') or '').strip()
    if referer:
        try:
            parsed = urlparse(referer)
            if parsed.scheme in {'http', 'https'} and parsed.netloc == request.get_host():
                return referer
        except ValueError:
            pass
    return request.build_absolute_uri('/cart/')


def _schedule_missing_add_payment_info(order, request=None):
    """Retry AddPaymentInfo after an invoice is reused if the first send failed."""
    payload = getattr(order, 'payment_payload', None)
    if payload is None:
        payload = getattr(order, 'event_state', None)
    if not isinstance(payload, dict) or payload.get('fb_capi_add_payment_info'):
        return

    event_id = getattr(order, 'get_add_payment_event_id', lambda: None)()
    payment_amount = getattr(order, 'payment_amount', None)
    if not payment_amount:
        if getattr(order, 'payment_status', None) == 'prepaid':
            payment_amount = getattr(order, 'get_prepayment_amount', lambda: 0)()
        else:
            payment_amount = getattr(order, 'final_total', None) or getattr(order, 'total_sum', 0)
    source_url = _capi_checkout_source_url(request) if request is not None else None

    def runner():
        from django.db import close_old_connections
        try:
            close_old_connections()
            get_facebook_conversions_service().send_add_payment_info_event(
                order=order,
                payment_amount=float(payment_amount or 0),
                event_id=event_id,
                source_url=source_url,
            )
        except Exception:
            monobank_logger.warning(
                'Failed retrying AddPaymentInfo for %s',
                getattr(order, 'order_number', getattr(order, 'reference', getattr(order, 'pk', '?'))),
                exc_info=True,
            )
        finally:
            close_old_connections()

    transaction.on_commit(
        lambda: threading.Thread(
            target=runner,
            daemon=True,
            name=f'add-payment-info-retry-{getattr(order, "pk", "unknown")}',
        ).start()
    )

# Константы статусов Monobank
MONOBANK_SUCCESS_STATUSES = {'success', 'hold'}
MONOBANK_PENDING_STATUSES = {'processing'}
MONOBANK_FAILURE_STATUSES = {
    'failure', 'expired', 'rejected', 'canceled', 'cancelled', 'reversed'
}

# API настройки
MONOBANK_API_BASE = 'https://api.monobank.ua'
MONOBANK_PUBLIC_KEY_CACHE_KEY = 'monobank_public_key'
MONOBANK_PUBLIC_KEY_CACHE_TTL = 3600  # 1 час


# ==================== HELPER FUNCTIONS ====================
# _reset_monobank_session moved to utils.py to avoid duplication

def _drop_pending_monobank_order(request):
    """
    Отменяет pending Monobank заказ и очищает сессию.
    """
    _reset_monobank_session(request, drop_pending=True)


def _notify_monobank_order(order, method_label):
    """
    Отправляет уведомление о новом Monobank заказе.

    Args:
        order: Объект заказа
        method_label: Название метода оплаты
    """
    try:
        from orders.telegram_notifications import send_order_notification
        send_order_notification(order, method_label=method_label)
    except Exception as e:
        monobank_logger.warning(
            'Failed to send Telegram notification for order %s: %s',
            order.id, e
        )


def _split_custom_cart_entries(custom_cart):
    from storefront.models import CustomPrintLead, CustomPrintModerationStatus

    approved_leads = []
    approved_keys = []
    pending_keys = []
    missing_price_leads = []

    if not isinstance(custom_cart, dict) or not custom_cart:
        return approved_leads, approved_keys, pending_keys, missing_price_leads

    key_to_lead_id = {
        key: value.get('lead_id')
        for key, value in custom_cart.items()
        if isinstance(value, dict) and value.get('lead_id')
    }
    lead_ids = [lead_id for lead_id in key_to_lead_id.values() if lead_id]
    leads_by_id = {
        lead.pk: lead
        for lead in CustomPrintLead.objects.filter(pk__in=lead_ids)
    } if lead_ids else {}

    for key, lead_id in key_to_lead_id.items():
        lead = leads_by_id.get(lead_id)
        if not lead or lead.moderation_status != CustomPrintModerationStatus.APPROVED:
            pending_keys.append(key)
            continue
        try:
            final_price = Decimal(str(lead.final_price_value))
        except Exception:
            final_price = Decimal('0.00')
        if final_price <= 0:
            missing_price_leads.append(lead)
            pending_keys.append(key)
            continue
        approved_leads.append(lead)
        approved_keys.append(key)

    return approved_leads, approved_keys, pending_keys, missing_price_leads


def _build_checkout_idempotency_key(
    request,
    *,
    cart,
    approved_custom_leads,
    full_name,
    phone,
    email,
    delivery_refs,
    city,
    np_office,
    pay_type,
):
    """Stable identity for one logical Monobank checkout attempt."""
    if request.user.is_authenticated:
        buyer_identity = f"user:{request.user.pk}"
    else:
        buyer_identity = f"session:{request.session.session_key}"

    state = {
        'version': 1,
        'buyer': buyer_identity,
        'cart': cart or {},
        'custom': [
            {'id': lead.pk, 'price': str(lead.final_price_value)}
            for lead in sorted(approved_custom_leads, key=lambda item: item.pk)
        ],
        'customer': {
            'full_name': (full_name or '').strip(),
            'phone': phone or '',
            'email': (email or '').strip().lower(),
        },
        'delivery': {
            'city': city or '',
            'np_office': np_office or '',
            **delivery_refs,
        },
        'pay_type': pay_type,
        'promo_code_id': request.session.get('promo_code_id'),
    }
    serialized = json.dumps(state, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _lock_checkout_identity(request):
    """Serialize matching checkout writers before the unique-key check."""
    if request.user.is_authenticated:
        request.user.__class__.objects.select_for_update().only('pk').get(pk=request.user.pk)
        return

    from django.contrib.sessions.models import Session

    Session.objects.select_for_update().only('session_key').get(
        session_key=request.session.session_key,
    )


def _invoice_url_from_order(order):
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    if payload.get('invoice_url'):
        return payload['invoice_url']
    creation = payload.get('create') if isinstance(payload.get('create'), dict) else {}
    result = creation.get('result') if isinstance(creation.get('result'), dict) else creation
    return result.get('pageUrl') or ''


def _existing_checkout_response(request, order, approved_custom_keys, pending_custom_keys):
    invoice_url = _invoice_url_from_order(order)
    payment_complete = order.payment_status in ('paid', 'prepaid')
    if not invoice_url and not payment_complete:
        return JsonResponse({
            'success': False,
            'in_progress': True,
            'retry_after_ms': 500,
            'error': 'Платіж уже створюється. Зачекайте кілька секунд.',
        }, status=409)

    request.session['monobank_invoice_id'] = order.payment_invoice_id
    request.session['monobank_pending_order_id'] = order.pk
    request.session['monobank_approved_custom_keys'] = approved_custom_keys
    request.session['monobank_pending_custom_keys'] = pending_custom_keys
    request.session.modified = True

    if payment_complete:
        from storefront.views.checkout import remember_order_in_session
        remember_order_in_session(request, order)

    _schedule_missing_add_payment_info(order, request)

    tracking = (
        order.payment_payload.get('tracking', {})
        if isinstance(order.payment_payload, dict)
        else {}
    )
    return JsonResponse({
        'success': True,
        'reused': True,
        'payment_complete': payment_complete,
        'redirect_url': reverse('order_success', kwargs={'order_id': order.pk}) if payment_complete else '',
        'invoice_url': invoice_url,
        'invoice_id': order.payment_invoice_id,
        'order_id': order.pk,
        'order_ref': order.order_number,
        'add_payment_event_id': tracking.get('add_payment_event_id') or order.get_add_payment_event_id(),
    })


def _existing_payment_attempt_response(attempt, approved_custom_keys, pending_custom_keys, request=None):
    """Return the existing invoice without creating a second checkout record."""
    if not attempt.invoice_url:
        return JsonResponse({
            'success': False,
            'in_progress': True,
            'retry_after_ms': 500,
            'error': 'Платіж уже створюється. Зачекайте кілька секунд.',
        }, status=409)
    _schedule_missing_add_payment_info(attempt, request)
    return JsonResponse({
        'success': True,
        'reused': True,
        'payment_complete': bool(attempt.order_id),
        'invoice_url': attempt.invoice_url,
        'invoice_id': attempt.monobank_invoice_id,
        'attempt_id': attempt.pk,
        'attempt_ref': attempt.reference,
        'order_id': attempt.order_id,
        'order_ref': attempt.order.order_number if attempt.order_id else '',
        'add_payment_event_id': attempt.add_payment_event_id,
    })


def _cleanup_expired_monobank_orders():
    """
    Очищает истекшие Monobank заказы (старше 24 часов).
    """
    try:
        cutoff = timezone.now() - timedelta(hours=24)
        expired = OrderModel.objects.filter(
            payment_provider__in=('monobank', 'monobank_checkout', 'monobank_pay'),
            status='new',
            payment_status__in=('unpaid', 'checking'),
            created__lt=cutoff,
        )
        count = expired.update(
            status='cancelled',
            payment_status='unpaid',
            checkout_idempotency_key=None,
        )
        if count > 0:
            monobank_logger.info(f'Cleaned up {count} expired Monobank orders')
    except Exception as e:
        monobank_logger.error(f'Error cleaning expired orders: {e}', exc_info=True)


def _get_monobank_public_key(token=None, cache_key=None):
    """
    Получает публичный ключ Monobank для проверки подписей.

    Каждый мерчант (storefront-токен и acquiring `mono_hrefs`) имеет свой
    публичный ключ, поэтому кэшируем отдельно по cache_key.

    Returns:
        str: Публичный ключ или None
    """
    token = token or settings.MONOBANK_TOKEN
    cache_key = cache_key or MONOBANK_PUBLIC_KEY_CACHE_KEY
    # Проверяем кеш
    cached_key = cache.get(cache_key)
    if cached_key:
        return cached_key

    try:
        # Запрашиваем у API
        response = requests.get(
            f'{MONOBANK_API_BASE}/api/merchant/pubkey',
            headers={'X-Token': token},
            timeout=10
        )
        response.raise_for_status()
        public_key = response.json().get('key')

        # Кешируем
        if public_key:
            cache.set(cache_key, public_key, MONOBANK_PUBLIC_KEY_CACHE_TTL)

        return public_key
    except Exception as e:
        monobank_logger.error(f'Failed to get Monobank public key: {e}', exc_info=True)
        return None


def _invalidate_monobank_public_key():
    """Инвалидирует кеш публичного ключа Monobank."""
    cache.delete(MONOBANK_PUBLIC_KEY_CACHE_KEY)


def _verify_monobank_signature(request, token=None, cache_key=None):
    """
    Проверяет подпись Monobank webhook запроса.

    Args:
        request: HTTP request с заголовком X-Sign
        token: X-Token мерчанта (для выбора правильного публичного ключа)
        cache_key: ключ кэша публичного ключа

    Returns:
        bool: True если подпись валидна, False иначе
    """
    try:
        signature = request.headers.get('X-Sign')
        if not signature:
            monobank_logger.warning('Missing X-Sign header in Monobank webhook')
            return False

        # Получаем публичный ключ
        public_key_pem = _get_monobank_public_key(token=token, cache_key=cache_key)
        if not public_key_pem:
            monobank_logger.error('Failed to get Monobank public key for verification')
            return False

        # Получаем тело запроса
        body = request.body

        # Декодируем подпись из base64
        signature_bytes = base64.b64decode(signature)

        if _verify_signature_with_key(public_key_pem, signature_bytes, body):
            return True

        # Ключ мог ротироваться — сбрасываем кеш и пробуем один раз со свежим.
        cache.delete(cache_key or MONOBANK_PUBLIC_KEY_CACHE_KEY)
        fresh_key = _get_monobank_public_key(token=token, cache_key=cache_key)
        if fresh_key and fresh_key != public_key_pem:
            if _verify_signature_with_key(fresh_key, signature_bytes, body):
                return True

        monobank_logger.warning('Monobank signature verification failed')
        return False

    except Exception as e:
        monobank_logger.error(
            f'Error verifying Monobank signature: {e}',
            exc_info=True
        )
        return False


def _verify_signature_with_key(public_key_raw, signature_bytes, body):
    """
    Проверяет подпись X-Sign данным публичным ключом.

    W1-3 (CRO-043): Monobank подписывает webhook ECDSA (SHA-256), а ключ из
    /api/merchant/pubkey приходит base64-кодированным PEM. Старая реализация
    проверяла RSA PKCS1v15 поверх сырой строки — и всегда падала.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, padding
        from cryptography.hazmat.backends import default_backend

        if isinstance(public_key_raw, bytes):
            raw = public_key_raw
        else:
            raw = public_key_raw.encode()

        # Ключ приходит base64-кодированным PEM; поддерживаем и «голый» PEM.
        pem_bytes = raw
        if b'-----BEGIN' not in raw:
            try:
                decoded = base64.b64decode(raw)
                if b'-----BEGIN' in decoded:
                    pem_bytes = decoded
            except Exception:
                pass

        public_key = serialization.load_pem_public_key(pem_bytes, backend=default_backend())

        try:
            if isinstance(public_key, ec.EllipticCurvePublicKey):
                public_key.verify(signature_bytes, body, ec.ECDSA(hashes.SHA256()))
            else:
                public_key.verify(signature_bytes, body, padding.PKCS1v15(), hashes.SHA256())
            return True
        except Exception:
            return False
    except Exception as load_error:
        monobank_logger.warning(f'Failed to load Monobank public key: {load_error}')
        return False


def _validate_checkout_payload(raw_payload):
    """
    Валидирует полезную нагрузку для checkout API.

    Args:
        raw_payload: dict с данными запроса

    Returns:
        tuple: (bool success, str error_message or None)
    """
    try:
        if not raw_payload.get('product_id'):
            return False, 'Missing product_id'

        qty = raw_payload.get('qty', 1)
        if not isinstance(qty, int) or qty < 1:
            return False, 'Invalid qty'

        return True, None
    except Exception as e:
        return False, str(e)


# ==================== MONOBANK API REQUESTS ====================

class MonobankAPIError(Exception):
    """Ошибка API Monobank"""


def _monobank_api_request(method, endpoint, json_payload=None, params=None, token=None):
    """
    Выполняет запрос к API Monobank.

    Args:
        method (str): HTTP метод ('GET' или 'POST')
        endpoint (str): API endpoint (напр. '/api/merchant/invoice/create')
        json_payload (dict): JSON данные для POST запроса
        token (str): X-Token для запроса. По умолчанию settings.MONOBANK_TOKEN
            (storefront-корзина). Для ссылок на оплату накладних менеджерів
            передаётся settings.MONOBANK_ACQUIRING_TOKEN (отдельный `mono_hrefs`).

    Returns:
        dict: Ответ от API

    Raises:
        MonobankAPIError: При ошибке API
    """
    token = token or getattr(settings, 'MONOBANK_TOKEN', None)
    if not token:
        raise MonobankAPIError('Monobank API token не налаштований')

    base_url = getattr(settings, 'MONOBANK_API_BASE', 'https://api.monobank.ua').rstrip('/')
    url = f"{base_url}{endpoint}"

    headers = {
        'X-Token': token,
        'Content-Type': 'application/json'
    }

    try:
        if method.upper() == 'POST':
            response = requests.post(url, json=json_payload, params=params, headers=headers, timeout=30)
        else:
            response = requests.get(url, params=params, headers=headers, timeout=30)

        data = response.json()
        monobank_logger.info(f'Monobank API {method} {endpoint}: status={response.status_code}')

        if response.status_code >= 400:
            error_msg = data.get('errText', data.get('errorDescription', 'Unknown error'))
            raise MonobankAPIError(f'Monobank API error: {error_msg}')

        return data

    except requests.exceptions.Timeout:
        raise MonobankAPIError('Timeout при з\'єднанні з Monobank')
    except requests.exceptions.RequestException as e:
        raise MonobankAPIError(f'Помилка з\'єднання з Monobank: {str(e)}')


# ==================== MONOBANK CREATE INVOICE ====================

def _create_payment_attempt_invoice(request):
    """Create/reuse a PaymentAttempt and its Monobank invoice."""
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    cart = get_validated_cart_from_session(request)
    ensure_request_session_key(request)
    from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY

    custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
    approved_leads, approved_keys, pending_keys, missing_price_leads = _split_custom_cart_entries(custom_cart)
    if missing_price_leads:
        return JsonResponse({
            'success': False,
            'error': 'Для погодженого кастомного виробу ще не зафіксована фінальна ціна.',
        }, status=400)
    if not cart and not approved_leads:
        return JsonResponse({'success': False, 'error': 'Кошик порожній. Додайте товари перед оплатою.'}, status=400)

    try:
        delivery = resolve_delivery_selection(body)
    except NovaPoshtaSelectionError as exc:
        return JsonResponse({'success': False, 'field': exc.field, 'error': exc.message}, status=400)

    if request.user.is_authenticated:
        try:
            profile = request.user.userprofile
        except Exception:
            return JsonResponse({'success': False, 'error': 'Будь ласка, заповніть профіль доставки.'}, status=400)

        def value_or_profile(name, fallback=''):
            value = body.get(name)
            if value is None:
                value = fallback
            return value.strip() if isinstance(value, str) else value

        full_name = value_or_profile('full_name', profile.full_name or request.user.username)
        raw_phone = value_or_profile('phone', profile.phone)
        email = value_or_profile('email', getattr(profile, 'email', '') or request.user.email or '')
        pay_type_raw = body.get('pay_type') or profile.pay_type or 'online_full'
    else:
        full_name = (body.get('full_name') or '').strip()
        raw_phone = body.get('phone') or ''
        email = (body.get('email') or '').strip()
        pay_type_raw = body.get('pay_type') or 'online_full'

    phone = normalize_checkout_phone(raw_phone)
    try:
        pay_type = normalize_pay_type(pay_type_raw, default=None)
    except ValueError:
        return JsonResponse({'success': False, 'field': 'pay_type', 'error': 'Оберіть коректний тип оплати.'}, status=400)
    if pay_type == 'cod':
        return JsonResponse({
            'success': False,
            'field': 'pay_type',
            'error': 'Оплата при отриманні недоступна. Оберіть повну онлайн-оплату або передплату.',
        }, status=400)
    if pay_type not in {'online_full', 'prepay_200'}:
        return JsonResponse({'success': False, 'field': 'pay_type', 'error': 'Оберіть доступний спосіб оплати.'}, status=400)
    if approved_leads and pay_type == 'prepay_200':
        return JsonResponse({'success': False, 'error': 'Передплата 200 грн недоступна для кастомного принта.'}, status=400)
    if raw_phone and not phone:
        return JsonResponse({'success': False, 'field': 'phone', 'error': 'Вкажіть коректний український номер телефону.'}, status=400)
    if not all([full_name, phone, delivery.city, delivery.np_office]):
        return JsonResponse({'success': False, 'error': 'Будь ласка, заповніть всі обовʼязкові поля.'}, status=400)

    normalized_email = ''
    if email:
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError
        try:
            validate_email(email)
            normalized_email = email
        except ValidationError:
            normalized_email = ''

    ids = [int(item['product_id']) for item in cart.values()]
    products = Product.objects.in_bulk(ids)
    if any(not products.get(item['product_id']) for item in cart.values()):
        return JsonResponse({'success': False, 'error': 'Деякі товари більше недоступні. Оновіть кошик.'}, status=400)

    from productcolors.models import ProductColorVariant
    from fable5.services import effective_cart_unit_price, variant_allows_purchase
    variant_ids = [item.get('color_variant_id') for item in cart.values() if item.get('color_variant_id')]
    variants = ProductColorVariant.objects.in_bulk(variant_ids)
    snapshot_items = []
    gross = Decimal('0.00')
    for item in cart.values():
        product = products[int(item['product_id'])]
        variant = variants.get(int(item['color_variant_id'])) if item.get('color_variant_id') else None
        if item.get('color_variant_id') and (
            variant is None or variant.product_id != product.pk or not variant_allows_purchase(
                product, variant, fit_code=item.get('fit_option_code') or item.get('fit') or '',
                size=item.get('size') or '', option_values=item.get('option_values') or {},
            )
        ):
            return JsonResponse({'success': False, 'error': 'Обраний варіант товару більше недоступний.'}, status=400)
        qty = int(item.get('qty') or 1)
        unit = effective_cart_unit_price(
            product, variant, fit_code=item.get('fit_option_code') or item.get('fit') or '',
            option_values=item.get('option_values') or {},
        )
        line_total = unit * qty
        gross += line_total
        snapshot_items.append({
            'product_id': product.pk,
            'title': product.title,
            'qty': qty,
            'size': item.get('size', ''),
            'fit_option_code': item.get('fit_option_code') or item.get('fit') or '',
            'fit_option_label': item.get('fit_option_label') or item.get('fit_label') or '',
            'color_variant_id': variant.pk if variant else None,
            'option_values': item.get('option_values') or {},
            'option_labels': item.get('option_labels') or {},
            'unit_price': str(unit),
            'line_total': str(line_total),
        })
    gross += sum((Decimal(str(lead.final_price_value)) for lead in approved_leads), Decimal('0.00'))
    if gross <= 0:
        return JsonResponse({'success': False, 'error': 'Сума замовлення повинна бути більше 0.'}, status=400)

    promo = None
    discount = Decimal('0.00')
    promo_id = request.session.get('promo_code_id')
    if promo_id:
        try:
            promo = PromoCode.objects.get(pk=promo_id)
            can_use = promo.can_be_used()
            if can_use and request.user.is_authenticated:
                can_use, _ = promo.can_be_used_by_user(request.user)
            if can_use:
                discount = min(Decimal(str(promo.calculate_discount(gross))), gross)
        except Exception:
            promo = None
            discount = Decimal('0.00')

    payable = max(gross - discount, Decimal('0.00'))
    payment_amount = min(Decimal('200.00'), payable) if pay_type == 'prepay_200' else payable
    delivery_refs = {
        'np_settlement_ref': delivery.settlement_ref,
        'np_city_ref': delivery.city_ref,
        'np_warehouse_ref': delivery.warehouse_ref,
    }
    fingerprint = _build_checkout_idempotency_key(
        request, cart=cart, approved_custom_leads=approved_leads, full_name=full_name,
        phone=phone, email=normalized_email, delivery_refs=delivery_refs,
        city=delivery.city, np_office=delivery.np_office, pay_type=pay_type,
    )
    with transaction.atomic():
        _lock_checkout_identity(request)
        attempt = PaymentAttempt.objects.select_for_update().filter(fingerprint=fingerprint).first()
        if attempt and attempt.order_id:
            return _existing_payment_attempt_response(attempt, approved_keys, pending_keys, request)
        if attempt and attempt.status in {PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.PROCESSING} and attempt.invoice_url:
            return _existing_payment_attempt_response(attempt, approved_keys, pending_keys, request)
        if attempt:
            fingerprint = hashlib.sha256(f'{fingerprint}:{timezone.now().timestamp()}'.encode()).hexdigest()
        attempt = PaymentAttempt.objects.create(
            fingerprint=fingerprint,
            user=request.user if request.user.is_authenticated else None,
            session_key=request.session.session_key,
            full_name=full_name,
            phone=phone,
            email=normalized_email or None,
            city=delivery.city,
            np_office=delivery.np_office,
            np_settlement_ref=delivery_refs['np_settlement_ref'],
            np_city_ref=delivery_refs['np_city_ref'],
            np_warehouse_ref=delivery_refs['np_warehouse_ref'],
            pay_type=pay_type,
            cart_snapshot={
                'cart': snapshot_items,
                'custom_print_lead_ids': [lead.pk for lead in approved_leads],
                'custom_print_leads': [
                    {'lead_number': lead.lead_number, 'price': str(lead.final_price_value), 'qty': int(getattr(lead, 'quantity', 0) or 1)}
                    for lead in approved_leads
                ],
            },
            gross_amount=gross,
            discount_amount=discount,
            payable_amount=payable,
            payment_amount=payment_amount,
            promo_code=promo,
        )

    record_initiate_checkout(request, float(payable))
    description = (
        f'Передплата 200 грн для {attempt.reference}. Повна сума: {payable:.2f} грн.'
        if pay_type == 'prepay_200'
        else f'Оплата замовлення {attempt.reference} на суму {payable:.2f} грн.'
    )
    basket = [
        {'name': item['title'], 'qty': item['qty'], 'sum': int(Decimal(item['line_total']) * 100), 'unit': 'шт'}
        for item in snapshot_items[:10]
    ]
    for lead in approved_leads:
        basket.append({'name': f'Кастомний виріб {lead.lead_number}', 'qty': int(getattr(lead, 'quantity', 0) or 1), 'sum': int(Decimal(str(lead.final_price_value)) * 100), 'unit': 'шт'})
    if discount > 0:
        basket.append({'name': f'Знижка по промокоду {promo.code}', 'qty': 1, 'sum': -int(discount * 100), 'unit': 'шт'})
    if pay_type == 'prepay_200':
        basket = [{'name': description, 'qty': 1, 'sum': int(payment_amount * 100), 'unit': 'шт'}]
    payload = {
        'amount': int(payment_amount * 100), 'ccy': 980,
        'merchantPaymInfo': {'reference': attempt.reference, 'destination': description, 'basketOrder': basket},
        'redirectUrl': request.build_absolute_uri('/payments/monobank/return/'),
        'webHookUrl': request.build_absolute_uri('/payments/monobank/webhook/'),
    }
    try:
        creation = _monobank_api_request('POST', '/api/merchant/invoice/create', json_payload=payload)
        result = creation.get('result') or creation
        invoice_id, invoice_url = result.get('invoiceId'), result.get('pageUrl')
        if not invoice_id or not invoice_url:
            raise MonobankAPIError('Monobank returned an invalid invoice')
    except Exception as exc:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status=PaymentAttempt.Status.FAILED,
            error_reason=str(exc)[:500],
            last_status_at=timezone.now(),
        )
        return JsonResponse({'success': False, 'error': f'Помилка створення платежу: {exc}'}, status=502)

    # Capture the same first-party attribution context used by paid orders:
    # fbp/fbc, click ids, stable external id, client IP and user agent. The
    # attempt is the durable source of truth until it becomes an Order.
    try:
        from storefront.utm_tracking import build_order_tracking_context
        tracking = build_order_tracking_context(request, attempt)
    except Exception:
        tracking = {}
    if isinstance(body.get('tracking'), dict):
        # Keep server-observed cookies, IP and UA authoritative. The browser
        # may contribute non-sensitive attribution fields, but must not be
        # able to replace trusted fbp/fbc or identity data in CAPI payloads.
        for key, value in body['tracking'].items():
            if key not in CLIENT_TRACKING_ALLOWED_KEYS:
                continue
            if key in {'event_id', 'lead_event_id'} or value is None:
                continue
            if key in tracking:
                continue
            tracking[key] = value
    tracking['external_id'] = tracking.get('external_id') or (
        f'user:{request.user.pk}' if request.user.is_authenticated else f'session:{request.session.session_key}'
    )
    tracking['add_payment_event_id'] = attempt.add_payment_event_id
    PaymentAttempt.objects.filter(pk=attempt.pk).update(
        monobank_invoice_id=invoice_id,
        invoice_url=invoice_url,
        invoice_payload={'request': payload, 'create': creation},
        tracking_payload=tracking,
        invoice_expires_at=timezone.now() + timedelta(hours=24),
        status=PaymentAttempt.Status.PROCESSING,
        last_status_at=timezone.now(),
    )
    attempt.refresh_from_db()
    request.session['monobank_invoice_id'] = invoice_id
    request.session['monobank_pending_attempt_id'] = attempt.pk
    request.session['monobank_attempt_id'] = attempt.pk
    request.session['monobank_approved_custom_keys'] = approved_keys
    request.session['monobank_pending_custom_keys'] = pending_keys
    request.session.modified = True

    try:
        get_facebook_conversions_service().send_add_payment_info_event(
            order=attempt, payment_amount=float(payment_amount), event_id=attempt.add_payment_event_id,
            source_url=_capi_checkout_source_url(request),
        )
    except Exception:
        monobank_logger.warning('Failed to send AddPaymentInfo for attempt %s', attempt.pk, exc_info=True)
    try:
        from orders.telegram_notifications import TelegramNotifier
        notifier = TelegramNotifier()
        if not (attempt.notification_state or {}).get('started_sent'):
            delivered = notifier.send_payment_attempt_notification(attempt)
            if delivered:
                state = dict(attempt.notification_state or {})
                state['started_sent'] = True
                state['started_sent_at'] = timezone.now().isoformat()
                PaymentAttempt.objects.filter(pk=attempt.pk).update(notification_state=state)
    except Exception:
        monobank_logger.warning('Failed to send Telegram attempt notification %s', attempt.pk, exc_info=True)
    return JsonResponse({
        'success': True, 'reused': False, 'payment_complete': False,
        'invoice_url': invoice_url, 'invoice_id': invoice_id,
        'attempt_id': attempt.pk, 'attempt_ref': attempt.reference,
        'add_payment_event_id': attempt.add_payment_event_id,
    })

@require_POST
def monobank_create_invoice(request):
    """
    Создание MonoPay инвойса для оплаты заказа из корзины.

    Поддерживает два типа оплаты:
    1. prepay_200 - предоплата 200 грн (остальное при получении)
    2. online_full - полная оплата онлайн

    POST params (JSON или из профиля):
        full_name: ПІБ клиента
        phone: Телефон
        city: Город
        np_office: Отделение Новой Почты
        pay_type: Тип оплаты ('prepay_200' или 'online_full')

    Returns:
        JsonResponse: 
            success=True: {invoice_url, invoice_id, order_id, order_ref}
            success=False: {error: 'message'}
    """
    return _create_payment_attempt_invoice(request)
    monobank_logger.info(f'=== monobank_create_invoice called ===')
    monobank_logger.info(f'User authenticated: {request.user.is_authenticated}')

    # Получаем данные из POST (для гостей) или из профиля (для зарегистрированных)
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    monobank_logger.info(
        'Monobank request received: keys=%s pay_type=%s',
        sorted(body.keys()) if isinstance(body, dict) else [],
        body.get('pay_type') if isinstance(body, dict) else None,
    )

    # Извлекаем tracking данные из body (отправляет клиент для дедупликации)
    client_tracking = body.get('tracking', {})
    if client_tracking:
        monobank_logger.info(
            'Client tracking received: allowed_keys=%s fbp=%s fbc=%s',
            sorted(set(client_tracking).intersection(CLIENT_TRACKING_ALLOWED_KEYS)) if isinstance(client_tracking, dict) else [],
            bool(client_tracking.get('fbp')) if isinstance(client_tracking, dict) else False,
            bool(client_tracking.get('fbc')) if isinstance(client_tracking, dict) else False,
        )

    # Получаем cart
    cart = get_validated_cart_from_session(request)

    ensure_request_session_key(request)

    # Approved custom-print items must join the paid order. Pending items stay in
    # the custom cart until moderation is complete.
    from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
    custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
    approved_custom_leads, approved_custom_keys, pending_custom_keys, missing_price_leads = _split_custom_cart_entries(custom_cart)
    has_custom_items = isinstance(custom_cart, dict) and bool(custom_cart)

    if missing_price_leads:
        lead_numbers = ", ".join(lead.lead_number for lead in missing_price_leads[:3] if lead.lead_number)
        suffix = f" ({lead_numbers})" if lead_numbers else ""
        return JsonResponse({
            'success': False,
            'error': f'Для погодженого кастомного виробу ще не зафіксована фінальна ціна{suffix}. Вкажіть ціну в адмінці та спробуйте знову.'
        }, status=400)

    if not cart and not approved_custom_leads:
        return JsonResponse({
            'success': False,
            'error': 'Кошик порожній. Додайте товари перед оплатою.'
        })

    if has_custom_items and not cart and not approved_custom_leads:
        return JsonResponse({
            'success': False,
            'error': 'Кастомний принт ще очікує на погодження менеджера. Оплата стане доступною після модерації.'
        })

    try:
        delivery_selection = resolve_delivery_selection(body)
    except NovaPoshtaSelectionError as exc:
        return JsonResponse({
            'success': False,
            'field': exc.field,
            'error': exc.message,
        }, status=400)

    delivery_refs = {
        'np_settlement_ref': delivery_selection.settlement_ref,
        'np_city_ref': delivery_selection.city_ref,
        'np_warehouse_ref': delivery_selection.warehouse_ref,
    }

    # Получаем данные клиента
    if request.user.is_authenticated:
        try:
            prof = request.user.userprofile
        except Exception as e:
            monobank_logger.error(f'Error getting user profile: {e}')
            return JsonResponse({
                'success': False,
                'error': 'Будь ласка, заповніть профіль доставки!'
            })

        def _body_override(field, default_value):
            value = body.get(field)
            if value is None:
                return default_value
            if isinstance(value, str):
                cleaned = value.strip()
                return cleaned or default_value
            return value or default_value

        full_name = _body_override('full_name', prof.full_name or request.user.username)
        raw_phone = _body_override('phone', prof.phone)
        phone = normalize_checkout_phone(raw_phone)
        city = delivery_selection.city
        np_office = delivery_selection.np_office
        # Email опціонально: з форми → з профілю → з акаунту
        customer_email = _body_override('email', getattr(prof, 'email', '') or (request.user.email or ''))

        pay_type_raw = (body.get('pay_type') or prof.pay_type or 'online_full')
        pay_type = pay_type_raw

        monobank_logger.info(
            f"Auth user: pay_type raw={body.get('pay_type')}, profile={prof.pay_type}, normalized={pay_type}"
        )
    else:
        # Для гостей - из POST body
        full_name = body.get('full_name', '').strip()
        raw_phone = body.get('phone', '')
        phone = normalize_checkout_phone(raw_phone)
        city = delivery_selection.city
        np_office = delivery_selection.np_office
        pay_type = body.get('pay_type', 'online_full')
        customer_email = (body.get('email') or '').strip()
        monobank_logger.info(f'Guest user: pay_type={pay_type}')

        # Валидация для гостей
        if not all([full_name, city, np_office]):
            return JsonResponse({
                'success': False,
                'error': 'Будь ласка, заповніть всі обов\'язкові поля!'
            })

    try:
        pay_type = normalize_pay_type(pay_type, default=None)
    except ValueError:
        return JsonResponse({
            'success': False,
            'field': 'pay_type',
            'error': 'Оберіть коректний тип оплати.',
        }, status=400)
    if pay_type == 'cod':
        # This endpoint always creates an online invoice. A COD profile default
        # therefore falls back to full online payment, never hidden prepayment.
        pay_type = 'online_full'

    if has_custom_items and pay_type == 'prepay_200':
        return JsonResponse({
            'success': False,
            'error': 'Передплата 200 грн недоступна, коли у кошику є кастомний принт. Оберіть повну онлайн-оплату.'
        }, status=400)

    if raw_phone and not phone:
        return JsonResponse({
            'success': False,
            'field': 'phone',
            'error': 'Вкажіть коректний український номер телефону. Можна без +380.',
        }, status=400)

    monobank_logger.info(f'Customer data: full_name={full_name}, pay_type={pay_type}')

    if not all([full_name, phone, city, np_office]):
        return JsonResponse({
            'success': False,
            'error': 'Будь ласка, заповніть всі обов\'язкові поля!'
        }, status=400)

    # Email опціональний. Якщо вказано некоректний — просто ігноруємо (не блокуємо оплату).
    normalized_email = ''
    if customer_email:
        try:
            from django.core.validators import validate_email as _validate_email
            from django.core.exceptions import ValidationError as _ValidationError
            try:
                _validate_email(customer_email)
                normalized_email = customer_email
            except _ValidationError:
                monobank_logger.info(f'Ignoring invalid checkout email: {customer_email!r}')
        except Exception:
            normalized_email = ''

    checkout_idempotency_key = _build_checkout_idempotency_key(
        request,
        cart=cart,
        approved_custom_leads=approved_custom_leads,
        full_name=full_name,
        phone=phone,
        email=normalized_email,
        delivery_refs=delivery_refs,
        city=city,
        np_office=np_office,
        pay_type=pay_type,
    )

    # Создаем заказ в транзакции
    try:
        with transaction.atomic():
            _lock_checkout_identity(request)
            existing_order = (
                OrderModel.objects.select_related('user')
                .filter(checkout_idempotency_key=checkout_idempotency_key)
                .first()
            )
            if existing_order:
                invoice_cutoff = timezone.now() - timedelta(hours=24)
                if (
                    existing_order.payment_status not in ('paid', 'prepaid')
                    and existing_order.created < invoice_cutoff
                ):
                    existing_order.status = 'cancelled'
                    existing_order.payment_status = 'unpaid'
                    existing_order.checkout_idempotency_key = None
                    existing_order.save(update_fields=[
                        'status',
                        'payment_status',
                        'checkout_idempotency_key',
                        'updated',
                    ])
                    existing_order = None

            if existing_order:
                # A worker that died before attaching an invoice must not block
                # checkout forever. Normal API calls finish well inside 2 min.
                orphan_cutoff = timezone.now() - timedelta(minutes=2)
                if not existing_order.payment_invoice_id and existing_order.created < orphan_cutoff:
                    existing_order.delete()
                else:
                    return _existing_checkout_response(
                        request,
                        existing_order,
                        approved_custom_keys,
                        pending_custom_keys,
                    )

            # Получаем товары из БД
            ids = [item['product_id'] for item in cart.values()]
            prods = Product.objects.in_bulk(ids)

            # W1-5а (CRO-047): исчезнувший товар раньше молча выбрасывался —
            # покупатель платил за заказ без части позиций. Теперь: явная
            # ошибка, заказ и инвойс НЕ создаются.
            if any(not prods.get(it['product_id']) for it in cart.values()):
                return JsonResponse({
                    'success': False,
                    'error': 'Деякі товари з кошика більше недоступні. Оновіть кошик і спробуйте ще раз.'
                })

            from productcolors.models import ProductColorVariant
            from fable5.services import effective_cart_unit_price

            variant_ids = [
                item.get('color_variant_id')
                for item in cart.values()
                if item.get('color_variant_id')
            ]
            variants_map = ProductColorVariant.objects.in_bulk(variant_ids)

            def _line_variant(item, product):
                raw_variant_id = item.get('color_variant_id')
                try:
                    variant = variants_map.get(int(raw_variant_id)) if raw_variant_id else None
                except (TypeError, ValueError):
                    variant = None
                if variant is not None and variant.product_id != product.pk:
                    return None
                return variant

            from fable5.services import variant_allows_purchase
            for item in cart.values():
                product = prods.get(item['product_id'])
                raw_variant_id = item.get('color_variant_id')
                variant = _line_variant(item, product)
                if raw_variant_id and (
                    variant is None
                    or not variant_allows_purchase(
                        product,
                        variant,
                        fit_code=item.get('fit_option_code') or item.get('fit') or '',
                        size=item.get('size') or '',
                        option_values=item.get('option_values') or {},
                    )
                ):
                    return JsonResponse({
                        'success': False,
                        'error': 'Обраний колір, посадка або розмір більше недоступні. Оновіть кошик.'
                    })

            # Подсчитываем общую сумму
            total_sum = Decimal('0')
            for key, it in cart.items():
                p = prods.get(it['product_id'])
                if not p:
                    continue
                unit = effective_cart_unit_price(
                    p,
                    _line_variant(it, p),
                    fit_code=it.get('fit_option_code') or it.get('fit') or '',
                    option_values=it.get('option_values') or {},
                )
                line = unit * it['qty']
                total_sum += line

            approved_custom_total = Decimal('0')
            for lead in approved_custom_leads:
                approved_custom_total += Decimal(str(lead.final_price_value))
            total_sum += approved_custom_total

            if total_sum <= 0:
                return JsonResponse({
                    'success': False,
                    'error': 'Сума замовлення повинна бути більше 0'
                })

            record_initiate_checkout(request, float(total_sum))

            # Создаем Order
            order = OrderModel.objects.create(
                user=request.user if request.user.is_authenticated else None,
                full_name=full_name,
                phone=phone,
                email=normalized_email or None,
                city=city,
                np_office=np_office,
                session_key=request.session.session_key,
                pay_type=pay_type,
                total_sum=total_sum,
                status='new',
                payment_status='unpaid',
                payment_provider='monobank_pay',
                checkout_idempotency_key=checkout_idempotency_key,
            )
            apply_nova_poshta_refs(order, delivery_refs)
            order.save(update_fields=['np_settlement_ref', 'np_city_ref', 'np_warehouse_ref'])
            link_order_to_utm(request, order)

            monobank_logger.info(f'Order created: {order.order_number} (ID: {order.id})')
            monobank_logger.info(f'🔍 Order.pay_type = {order.pay_type}')
            monobank_logger.info(f'🔍 Order.total_sum = {order.total_sum}')

            # Создаем OrderItem'ы
            order_items = []
            for key, it in cart.items():
                p = prods.get(it['product_id'])
                if not p:
                    continue

                color_variant = _line_variant(it, p)
                unit = effective_cart_unit_price(
                    p,
                    color_variant,
                    fit_code=it.get('fit_option_code') or it.get('fit') or '',
                    option_values=it.get('option_values') or {},
                )
                line = unit * it['qty']

                order_item = OrderItem(
                    order=order,
                    product=p,
                    color_variant=color_variant,
                    title=p.title,
                    size=it.get('size', ''),
                    fit_option_code=(it.get('fit_option_code') or it.get('fit') or ''),
                    fit_option_label=(it.get('fit_option_label') or it.get('fit_label') or ''),
                    option_values=it.get('option_values') or {},
                    option_labels=it.get('option_labels') or {},
                    qty=it['qty'],
                    unit_price=unit,
                    line_total=line
                )
                order_items.append(order_item)

            OrderItem.objects.bulk_create(order_items)
            monobank_logger.info(f'Created {len(order_items)} order items')

            for lead in approved_custom_leads:
                lead.order = order
                lead.save(update_fields=['order'])

            # Применяем промокод если есть (W1-4)
            promo_code_id = request.session.get('promo_code_id')
            if promo_code_id:
                try:
                    promo = PromoCode.objects.get(id=promo_code_id)
                    can_use = promo.can_be_used()
                    if can_use and request.user.is_authenticated:
                        can_use, _promo_reason = promo.can_be_used_by_user(request.user)
                    if can_use:
                        discount = promo.calculate_discount(total_sum)
                        if discount > 0:
                            order.discount_amount = discount
                            order.promo_code = promo
                            # W1-4г: лимит НЕ сжигаем при создании инвойса —
                            # record_usage() вызывается при УСПЕШНОЙ оплате
                            # (см. _record_promo_usage_for_order).
                            order.save(update_fields=['discount_amount', 'promo_code'])
                            monobank_logger.info(f'Promo code applied: {promo.code}, discount={discount}')
                except Exception as e:
                    monobank_logger.warning(f'Error applying promo code: {e}')

        # W1-5в (CRO-047/DB-009): транзакция ЗАКРЫТА — заказ и позиции
        # закоммичены. Всё внешнее (Monobank invoice/create, Facebook CAPI,
        # Telegram) выполняется ВНЕ atomic, чтобы не держать row-lock на
        # время HTTP-вызовов (wait_timeout=60 на shared-хостинге).
        # При ошибке до получения invoice_id заказ удаляется (см. except ниже).

        # Определяем сумму для оплаты в зависимости от pay_type
        monobank_logger.info(f'🔍 Determining payment amount. pay_type={pay_type}, order.pay_type={order.pay_type}')

        if pay_type == 'prepay_200':
            monobank_logger.info(f'✅ pay_type is prepay_200! Calculating prepayment...')

            # КРИТИЧЕСКАЯ ТОЧКА: Вызов get_prepayment_amount()
            monobank_logger.info(f'🔍 Calling order.get_prepayment_amount()...')
            monobank_logger.info(f'🔍 order.pay_type before call: {order.pay_type}')
            payment_amount = order.get_prepayment_amount()
            monobank_logger.info(f'🔍 order.get_prepayment_amount() returned: {payment_amount}')
            monobank_logger.info(f'🔍 Type: {type(payment_amount)}, Value: {payment_amount}')

            # Формируем описание для предоплаты с номером заказа.
            # W1-4в: total_sum хранится БЕЗ вычета скидки, поэтому
            # к оплате = total_sum - discount_amount, а остаток =
            # к оплате - предоплата (раньше остаток был завышен на скидку).
            payable_total = order.total_sum - (order.discount_amount or Decimal('0'))
            remaining_amount = payable_total - payment_amount
            payment_description = (
                f'Передплата 200 грн для замовлення {order.order_number}. '
                f'Повна сума: {payable_total:.2f} грн. '
                f'Залишок {remaining_amount:.2f} грн оплачується при отриманні через Нову Пошту.'
            )
            monobank_logger.info(f'✅ Prepayment amount set to: {payment_amount} UAH')
            monobank_logger.info(f'✅ Payment description: {payment_description}')
        else:
            monobank_logger.info(f'✅ pay_type is NOT prepay_200 (it is {pay_type}). Using full amount.')
            # Полная оплата
            payment_amount = order.total_sum - order.discount_amount
            payment_description = f'Оплата замовлення {order.order_number}'
            monobank_logger.info(f'✅ Full payment amount: {payment_amount} UAH')

        monobank_logger.info(f'🔍 FINAL payment_amount: {payment_amount} (pay_type={pay_type})')
        monobank_logger.info(f'🔍 payment_amount in kopecks: {int(payment_amount * 100)}')

        # Формируем basket для Monobank
        monobank_logger.info(f'🔍 Building basket entries for pay_type={pay_type}')
        basket_entries = []

        # Для предоплаты показываем товары с полными ценами отдельными позициями
        if pay_type == 'prepay_200':
            total_items_sum = Decimal('0')

            # Вычисляем остаток к доплате заранее (W1-4в: со скидкой)
            payable_total = order.total_sum - (order.discount_amount or Decimal('0'))
            remaining_amount = payable_total - payment_amount

            # Добавляем все товары с их полными ценами
            items_to_show = order_items[:10]  # Максимум 10 товаров
            items_count = len(items_to_show)

            for idx, item in enumerate(items_to_show):
                try:
                    # Получаем URL изображения
                    icon_url = ''
                    if item.product.main_image:
                        icon_url = request.build_absolute_uri(item.product.main_image.url)

                    # Используем полную стоимость товара (line_total)
                    item_total_kopecks = int(item.line_total * 100)
                    total_items_sum += item.line_total

                    # Формируем название товара
                    item_name = item.title
                    if item.size:
                        item_name += f' ({item.size})'
                    if item.fit_label:
                        item_name += f' / {item.fit_label}'

                    monobank_logger.info(f'🔍 PREPAY mode: Adding item with FULL price')
                    monobank_logger.info(f'🔍 - name: {item_name}')
                    monobank_logger.info(f'🔍 - qty: {item.qty}')
                    monobank_logger.info(f'🔍 - sum: {item_total_kopecks} kopecks ({item.line_total} UAH)')

                    item_entry = {
                        'name': item_name,
                        'qty': item.qty,
                        'sum': item_total_kopecks,  # полная цена товара в копейках
                        'icon': icon_url,
                        'unit': 'шт'
                    }

                    # Для последнего товара добавляем описание с информацией о предоплате
                    if idx == len(items_to_show) - 1:
                        if items_count > 1:
                            item_entry['description'] = f'Передплата 200 грн за {items_count} товарів. Залишок {remaining_amount:.2f} грн — при отриманні на Новій Пошті'
                        else:
                            item_entry['description'] = f'Передплата 200 грн. Залишок {remaining_amount:.2f} грн — при отриманні на Новій Пошті'

                    basket_entries.append(item_entry)
                except Exception as e:
                    monobank_logger.warning(f'Error processing item for prepay basket: {e}')

            # Добавляем позицию "Предоплата" с суммой, которая делает общую сумму basket = 200
            # Если сумма товаров уже больше 200, добавляем отрицательную позицию для баланса
            prepay_kopecks = int(payment_amount * 100)
            current_basket_sum = int(total_items_sum * 100)

            if current_basket_sum > prepay_kopecks:
                # Добавляем отрицательную позицию для баланса
                balance_kopecks = prepay_kopecks - current_basket_sum
                monobank_logger.info(f'🔍 PREPAY mode: Adding balance entry')
                monobank_logger.info(f'🔍 - balance: {balance_kopecks} kopecks')

                basket_entries.append({
                    'name': f'Часткова оплата (замовлення {order.order_number}). Залишок {remaining_amount:.2f} грн при отриманні через Нову Пошту',
                    'qty': 1,
                    'sum': balance_kopecks,  # отрицательная сумма для баланса
                    'icon': '',
                    'unit': 'шт'
                })
            elif current_basket_sum < prepay_kopecks:
                # Добавляем позицию "Предоплата" с остаточной суммой
                remaining_prepay = prepay_kopecks - current_basket_sum
                monobank_logger.info(f'🔍 PREPAY mode: Adding prepayment entry')
                monobank_logger.info(f'🔍 - prepay: {remaining_prepay} kopecks')

                basket_entries.append({
                    'name': f'Передплата (замовлення {order.order_number}). Залишок {remaining_amount:.2f} грн при отриманні через Нову Пошту',
                    'qty': 1,
                    'sum': remaining_prepay,
                    'icon': '',
                    'unit': 'шт'
                })
            else:
                # Суммы совпадают - описание уже добавлено к последнему товару
                monobank_logger.info(f'🔍 PREPAY mode: Sums match, description already added to last item')
        else:
            # Для полной оплаты показываем все товары отдельными позициями
            for item in order_items[:10]:  # Максимум 10 товаров
                try:
                    # Получаем URL изображения
                    icon_url = ''
                    if item.product.main_image:
                        icon_url = request.build_absolute_uri(item.product.main_image.url)

                    basket_sum_kopecks = int(item.line_total * 100)

                    # Добавляем информацию о промокоде к названию товара
                    item_name = f'{item.title} {item.size}'.strip()
                    if item.fit_label:
                        item_name = f'{item_name} / {item.fit_label}'.strip()
                    if order.promo_code:
                        item_name += f' [з промокодом {order.promo_code.code}]'

                    monobank_logger.info(f'🔍 FULL mode: Adding item {item_name}')
                    monobank_logger.info(f'🔍 - qty: {item.qty}')
                    monobank_logger.info(f'🔍 - sum: {basket_sum_kopecks} kopecks ({item.line_total} UAH)')

                    basket_entries.append({
                        'name': item_name,
                        'qty': item.qty,
                        'sum': basket_sum_kopecks,  # в копейках
                        'icon': icon_url,
                        'unit': 'шт'
                    })
                except Exception as e:
                    monobank_logger.warning(f'Error formatting basket item: {e}')

            for lead in approved_custom_leads:
                try:
                    basket_entries.append({
                        'name': f'Кастомний виріб {lead.lead_number}',
                        'qty': int(getattr(lead, 'quantity', 0) or 1),
                        'sum': int(Decimal(str(lead.final_price_value)) * 100),
                        'icon': '',
                        'unit': 'шт',
                    })
                except Exception as e:
                    monobank_logger.warning(f'Error formatting custom print basket item: {e}')

            # Добавляем позицию со скидкой если есть промокод
            if order.promo_code and order.discount_amount > 0:
                discount_kopecks = int(order.discount_amount * 100)
                monobank_logger.info(f'🔍 Adding discount entry: {discount_kopecks} kopecks')
                basket_entries.append({
                    'name': f'Знижка по промокоду {order.promo_code.code}',
                    'qty': 1,
                    'sum': -discount_kopecks,  # отрицательная сумма
                    'icon': '',
                    'unit': 'шт'
                })

        if not basket_entries:
            basket_entries.append({
                'name': payment_description,
                'qty': 1,
                'sum': int(payment_amount * 100),
                'icon': '',
                'unit': 'шт'
            })

        # Создаем Monobank инвойс
        payload = {
            'amount': int(payment_amount * 100),  # сумма в копейках
            'ccy': 980,  # UAH
            'merchantPaymInfo': {
                'reference': order.order_number,
                'destination': payment_description,
                'basketOrder': basket_entries
            },
            'redirectUrl': request.build_absolute_uri('/payments/monobank/return/'),
            'webHookUrl': request.build_absolute_uri('/payments/monobank/webhook/'),
        }

        monobank_logger.info(f'Creating Monobank invoice, payload: {json.dumps(payload, indent=2, ensure_ascii=False)}')

        try:
            creation_data = _monobank_api_request('POST', '/api/merchant/invoice/create', json_payload=payload)
            monobank_logger.info(f'Monobank response: {creation_data}')
        except MonobankAPIError as exc:
            monobank_logger.error(f'Monobank API error: {exc}')
            # Удаляем созданный заказ при ошибке
            order.delete()
            return JsonResponse({
                'success': False,
                'error': f'Помилка створення платежу: {str(exc)}'
            })

        # Извлекаем данные из ответа
        result = creation_data.get('result') or creation_data
        invoice_id = result.get('invoiceId')
        invoice_url = result.get('pageUrl')

        if not invoice_id or not invoice_url:
            monobank_logger.error(f'Invalid Monobank response: {creation_data}')
            order.delete()
            return JsonResponse({
                'success': False,
                'error': 'Не вдалося створити платіж. Спробуйте пізніше.'
            })

        # Детерминированный event_id для дедупликации AddPaymentInfo
        add_payment_event_id = order.get_add_payment_event_id()

        # Собираем tracking данные для Facebook/TikTok Conversions API
        tracking_context = {}

        # FBP Cookie (Facebook Browser Pixel)
        try:
            fbp_cookie = request.COOKIES.get('_fbp')
        except Exception:
            fbp_cookie = None
        if fbp_cookie:
            tracking_context['fbp'] = fbp_cookie

        # FBC Cookie (Facebook Click ID)
        try:
            fbc_cookie = request.COOKIES.get('_fbc')
        except Exception:
            fbc_cookie = None
        if fbc_cookie:
            tracking_context['fbc'] = fbc_cookie

        # TikTok Click ID
        try:
            ttclid_cookie = request.COOKIES.get('ttclid')
        except Exception:
            ttclid_cookie = None
        if ttclid_cookie:
            tracking_context['ttclid'] = ttclid_cookie

        # Дополняем tracking_context данными от клиента (если есть)
        if isinstance(client_tracking, dict) and client_tracking:
            for key, value in client_tracking.items():
                if key not in CLIENT_TRACKING_ALLOWED_KEYS:
                    continue
                if value is None:
                    continue
                # Игнорируем event_id и lead_event_id - они генерируются при отправке событий
                if key in ('event_id', 'lead_event_id'):
                    continue
                # Не перезаписываем server-side значения если они уже есть
                if key in tracking_context:
                    continue
                tracking_context[key] = value

        # Сохраняем event_id для AddPaymentInfo, чтобы браузер и CAPI использовали одинаковое значение
        tracking_context['add_payment_event_id'] = add_payment_event_id

        # КРИТИЧНО: External ID должен ВСЕГДА быть определен
        external_source = tracking_context.get('external_id')
        if request.user.is_authenticated:
            external_source = external_source or f"user:{request.user.id}"
        else:
            # Пытаемся получить session_key
            try:
                session_key = request.session.session_key
                if not session_key:
                    # Создаем сессию если еще не создана
                    request.session.create()
                    session_key = request.session.session_key
                if session_key:
                    external_source = external_source or f"session:{session_key}"
            except Exception:
                pass

            # Если нет session_key, используем order_number
            if not external_source and order.order_number:
                external_source = f"order:{order.order_number}"

            # Если нет order_number, используем order.id
            if not external_source and order.id:
                external_source = f"order:{order.id}"

        # ГАРАНТИРУЕМ что external_id ВСЕГДА определен
        if not external_source:
            import time
            external_source = f"order:unknown_{int(time.time())}"

        tracking_context['external_id'] = external_source

        # Добавляем Client IP Address для улучшения атрибуции
        try:
            # Получаем реальный IP (учитываем проксирование)
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                client_ip = x_forwarded_for.split(',')[0].strip()
            else:
                client_ip = request.META.get('REMOTE_ADDR')

            if client_ip:
                tracking_context['client_ip_address'] = client_ip
        except Exception:
            pass

        # Добавляем User Agent для улучшения атрибуции
        try:
            user_agent = request.META.get('HTTP_USER_AGENT', '')
            if user_agent:
                tracking_context['client_user_agent'] = user_agent
        except Exception:
            pass

        # Сохраняем данные платежа в Order
        payment_payload = {
            'request': payload,
            'create': creation_data,
            'history': [],
            'tracking': tracking_context,
            'custom_print_lead_ids': [lead.pk for lead in approved_custom_leads],
            'invoice_url': invoice_url,
        }

        # Добавляем client_ip_address и client_user_agent на верхний уровень для совместимости
        if 'client_ip_address' in tracking_context:
            payment_payload['client_ip_address'] = tracking_context['client_ip_address']
        if 'client_user_agent' in tracking_context:
            payment_payload['client_user_agent'] = tracking_context['client_user_agent']

        order.payment_invoice_id = invoice_id
        order.payment_payload = payment_payload
        order.payment_status = 'checking'
        order.save(update_fields=['payment_invoice_id', 'payment_payload', 'payment_status'])
        try:
            mark_checkout_capture_converted(order.session_key)
        except Exception:
            monobank_logger.warning(
                'Failed to mark checkout capture converted for order %s',
                order.pk,
                exc_info=True,
            )
        record_lead(request, order.id, order.order_number, float(payment_amount))

        monobank_logger.info(f'Order {order.order_number}: Saved tracking context: external_id={external_source}, fbp={bool(fbp_cookie)}, fbc={bool(fbc_cookie)}')

        monobank_logger.info(f'Order {order.order_number} updated with invoice_id={invoice_id}')

        # Сохраняем в сессию
        request.session['monobank_invoice_id'] = invoice_id
        request.session['monobank_pending_order_id'] = order.id
        request.session['monobank_approved_custom_keys'] = approved_custom_keys
        request.session['monobank_pending_custom_keys'] = pending_custom_keys
        request.session.modified = True

        # Отправляем AddPaymentInfo через CAPI для дедупликации с пикселем
        try:
            facebook_service = get_facebook_conversions_service()
            facebook_service.send_add_payment_info_event(
                order=order,
                payment_amount=float(payment_amount),
                event_id=add_payment_event_id,
                source_url=_capi_checkout_source_url(request),
            )
        except Exception as capi_err:
            monobank_logger.warning(f'⚠️ Failed to send AddPaymentInfo to Facebook CAPI: {capi_err}')

        # НЕ очищаем корзину здесь - корзина будет очищена ТОЛЬКО после успешной оплаты
        # в monobank_return или через webhook

        # Отправляем Telegram уведомление
        try:
            from orders.telegram_notifications import TelegramNotifier
            notifier = TelegramNotifier()
            notifier.send_new_order_notification(order)
        except Exception as e:
            monobank_logger.warning(f'Failed to send Telegram notification: {e}')

        monobank_logger.info(f'✅ Invoice created successfully: {invoice_url}')

        return JsonResponse({
            'success': True,
            'reused': False,
            'payment_complete': False,
            'invoice_url': invoice_url,
            'invoice_id': invoice_id,
            'order_id': order.id,
            'order_ref': order.order_number,
            'add_payment_event_id': add_payment_event_id
        })

    except Exception as e:
        monobank_logger.error(f'Error creating order/invoice: {e}', exc_info=True)
        # W1-5в: транзакция уже закоммичена — если инвойс так и не был
        # привязан, подчищаем осиротевший заказ (best-effort).
        try:
            _orphan = locals().get('order')
            if _orphan is not None and _orphan.pk and not _orphan.payment_invoice_id:
                _orphan.delete()
                monobank_logger.info('Orphan order without invoice deleted after failure')
        except Exception:
            monobank_logger.warning('Failed to clean up orphan order', exc_info=True)
        return JsonResponse({
            'success': False,
            'error': 'Сталася помилка. Спробуйте ще раз.'
        })


# ==================== MONOBANK FINALIZE INVOICE ====================

def _monobank_finalize_invoice(order, request=None):
    """
    Финализирует Monobank инвойс после отправки товаров.

    ВАЖНО: Финализация НЕ списывает деньги! Деньги уже списаны при оплате.

    Финализация нужна ТОЛЬКО для:
    - Фискализации детальных данных в чеках Monobank
    - Добавления детальной информации о товарах, промокодах, предоплатах
    - Обновления налоговой отчетности

    Args:
        order: Объект Order
        request: HTTP request (опционально, для build_absolute_uri)

    Returns:
        dict: Результат финализации или None при ошибке
    """
    if not order.payment_invoice_id:
        monobank_logger.warning(f'Order {order.order_number} has no invoice_id, skipping finalization')
        return None

    # Проверяем что заказ оплачен
    if order.payment_status not in ['paid', 'prepaid']:
        monobank_logger.warning(f'Order {order.order_number} payment_status={order.payment_status}, skipping finalization')
        return None

    try:
        # Формируем items для финализации
        items = []

        # Добавляем товары
        for item in order.items.all():
            item_name = item.title
            if item.size:
                item_name += f' ({item.size})'
            if item.fit_label:
                item_name += f' / {item.fit_label}'

            items.append({
                'name': item_name,
                'qty': item.qty,
                'sum': int(item.line_total * 100),  # в копейках
                'icon': '',
                'unit': 'шт'
            })

        # Добавляем позицию со скидкой если есть промокод
        if order.promo_code and order.discount_amount > 0:
            items.append({
                'name': f'Знижка по промокоду {order.promo_code.code}',
                'qty': 1,
                'sum': -int(order.discount_amount * 100),  # отрицательная сумма
                'icon': '',
                'unit': 'шт'
            })

        # Добавляем комментарий о prepayment если есть
        if order.pay_type == 'prepay_200':
            prepay_amount = order.get_prepayment_amount()
            remaining = order.total_sum - order.discount_amount - prepay_amount
            items.append({
                'name': f'Передплата 200 грн. Залишок {remaining:.2f} грн при отриманні через Нову Пошту',
                'qty': 1,
                'sum': 0,  # информационная позиция
                'icon': '',
                'unit': 'шт'
            })

        # Определяем финальную сумму
        final_amount = order.total_sum - order.discount_amount

        # Для prepayment финализируем только 200 грн (или факт. списанную сумму)
        if order.pay_type == 'prepay_200':
            # Используем ту сумму, которая была фактически оплачена
            if order.payment_status == 'prepaid':
                final_amount = order.get_prepayment_amount()
            else:
                final_amount = order.total_sum - order.discount_amount

        payload = {
            'invoiceId': order.payment_invoice_id,
            'amount': int(final_amount * 100),
            'items': items
        }

        monobank_logger.info(f'Finalizing invoice {order.payment_invoice_id} for order {order.order_number}')
        monobank_logger.info(f'Final amount: {final_amount} UAH, items count: {len(items)}')

        try:
            result = _monobank_api_request('POST', '/api/merchant/invoice/finalize', json_payload=payload)

            # Сохраняем результат в payment_payload
            if order.payment_payload:
                if 'finalize' not in order.payment_payload:
                    order.payment_payload['finalize'] = []
                order.payment_payload['finalize'].append({
                    'timestamp': timezone.now().isoformat(),
                    'payload': payload,
                    'result': result
                })
                order.save(update_fields=['payment_payload'])

            monobank_logger.info(f'✅ Invoice {order.payment_invoice_id} finalized successfully')
            return result

        except MonobankAPIError as e:
            monobank_logger.error(f'Monobank finalize error for invoice {order.payment_invoice_id}: {e}')
            return None

    except Exception as e:
        monobank_logger.error(f'Error finalizing invoice {order.payment_invoice_id}: {e}', exc_info=True)
        return None


# ==================== MONOBANK RETURN & WEBHOOK ====================

def _ensure_payment_payload(order):
    """Guarantee payment_payload has a history list."""
    if not order.payment_payload or not isinstance(order.payment_payload, dict):
        order.payment_payload = {}
    if 'history' not in order.payment_payload or not isinstance(order.payment_payload.get('history'), list):
        order.payment_payload['history'] = []


def _append_payment_history(order, status, payload, source):
    _ensure_payment_payload(order)
    try:
        order.payment_payload['history'].append({
            'ts': timezone.now().isoformat(),
            'status': status,
            'source': source,
            'payload': payload,
        })
    except Exception:
        # keep it safe even if payload is not JSON-serialisable
        order.payment_payload['history'].append({
            'ts': timezone.now().isoformat(),
            'status': status,
            'source': source,
            'payload': str(payload)[:1000],
        })


def _cleanup_after_success(request):
    """Clear cart/promo and Mono session keys after successful payment."""
    approved_custom_keys = request.session.pop('monobank_approved_custom_keys', None) or []
    if approved_custom_keys:
        try:
            from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY

            custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
            if isinstance(custom_cart, dict):
                for key in approved_custom_keys:
                    custom_cart.pop(key, None)
                request.session[SESSION_CUSTOM_CART_KEY] = custom_cart
        except Exception:
            monobank_logger.warning('Failed to cleanup approved custom cart entries after successful payment')

    pending_order_id = request.session.get('monobank_pending_order_id')
    if pending_order_id:
        OrderModel.objects.filter(pk=pending_order_id).update(checkout_idempotency_key=None)

    request.session.pop('cart', None)
    request.session.pop('promo_code', None)
    request.session.pop('promo_code_id', None)
    request.session.pop('promo_code_data', None)
    request.session.pop('monobank_invoice_id', None)
    request.session.pop('monobank_pending_order_id', None)
    request.session.pop('monobank_pending_attempt_id', None)
    request.session.pop('monobank_attempt_id', None)
    request.session.pop('monobank_pending_custom_keys', None)
    request.session.modified = True


def _record_promo_usage_for_order(order):
    """
    W1-4б (CRO-046): фиксирует использование промокода при УСПЕШНОЙ оплате.

    Идемпотентно: повторный вебхук/return не создаёт дубликатов и не сжигает
    лимит дважды. Для гостевых заказов инкрементируется только счётчик
    current_uses (персональная история требует пользователя).
    """
    try:
        promo = order.promo_code
        if not promo:
            return
        from storefront.models import PromoCodeUsage
        if PromoCodeUsage.objects.filter(order=order).exists():
            return
        user = getattr(order, 'user', None)
        if user and getattr(user, 'is_authenticated', False):
            promo.record_usage(user, order)
        else:
            promo.use()
    except Exception:
        monobank_logger.warning(
            'Failed to record promo usage for order %s', getattr(order, 'pk', None), exc_info=True
        )


def _apply_monobank_status(order, status_value, payload=None, source='webhook'):
    """
    Apply Monobank status once under a row lock and dispatch external events
    only after the database transaction commits.
    """
    status_lower = (status_value or '').lower()
    with transaction.atomic():
        order = (
            OrderModel.objects.select_for_update()
            .select_related('user', 'promo_code')
            .get(pk=order.pk)
        )
        _append_payment_history(order, status_lower, payload, source)

        updated_fields = ['payment_payload', 'updated']
        old_payment_status = order.payment_status
        canonical_pay_type = _normalize_order_pay_type(getattr(order, 'pay_type', None))
        target_payment_status = 'prepaid' if canonical_pay_type == 'prepay_200' else 'paid'

        if status_lower in MONOBANK_SUCCESS_STATUSES:
            order.payment_status = target_payment_status
            updated_fields.append('payment_status')
        elif status_lower in MONOBANK_PENDING_STATUSES:
            order.payment_status = 'checking'
            updated_fields.append('payment_status')
        elif status_lower in MONOBANK_FAILURE_STATUSES:
            order.payment_status = 'unpaid'
            order.checkout_idempotency_key = None
            updated_fields.extend(['payment_status', 'checkout_idempotency_key'])
        else:
            # Unknown status, keep history only.
            order.save(update_fields=['payment_payload'])
            return status_lower

        order.save(update_fields=list(set(updated_fields)))

        if order.payment_status in ('paid', 'prepaid'):
            # Heal the internal funnel even when this is a repeated success
            # delivery for an order that became paid before UserAction existed.
            ensure_order_purchase_action(
                order,
                metadata={
                    'monobank_status': status_lower,
                    'source': source,
                },
            )

        if order.payment_status in ('paid', 'prepaid') and order.payment_status != old_payment_status:
            # Transactional, idempotent DB side-effects remain under the same
            # order lock. Network calls are delegated after commit below.
            _record_promo_usage_for_order(order)
            transaction.on_commit(
                lambda order_pk=order.pk,
                previous=old_payment_status or 'unpaid',
                pay_type=canonical_pay_type: _dispatch_post_payment_events(
                    order_pk,
                    previous,
                    pay_type,
                )
            )

    return status_lower


def _get_order_by_payment_refs(invoice_id=None, order_ref=None, order_id=None):
    """
    Locate an order using invoice_id or order_number/id.
    """
    qs = OrderModel.objects.select_related('user')
    if invoice_id:
        order = qs.filter(payment_invoice_id=invoice_id).order_by('-created').first()
        if order:
            return order
    if order_ref:
        order = qs.filter(order_number=order_ref).order_by('-created').first()
        if order:
            return order
    if order_id:
        try:
            return qs.get(id=order_id)
        except OrderModel.DoesNotExist:
            return None
    return None


def _get_payment_attempt_by_refs(invoice_id=None, attempt_ref=None, attempt_id=None):
    qs = PaymentAttempt.objects.select_related('user', 'order')
    if invoice_id:
        attempt = qs.filter(monobank_invoice_id=invoice_id).order_by('-created').first()
        if attempt:
            return attempt
    if attempt_ref:
        attempt = qs.filter(reference=attempt_ref).first()
        if attempt:
            return attempt
    if attempt_id:
        try:
            return qs.get(pk=attempt_id)
        except (PaymentAttempt.DoesNotExist, ValueError, TypeError):
            return None
    return None


def _resolve_attempt_invoice_status(attempt, invoice_id, fallback_status=None):
    status_payload = None
    status_value = None
    if invoice_id:
        try:
            status_payload = _monobank_api_request(
                'GET', '/api/merchant/invoice/status', params={'invoiceId': invoice_id}
            )
            if isinstance(status_payload.get('result'), dict):
                status_payload = status_payload['result']
            status_value = status_payload.get('status') or status_payload.get('statusCode')
        except MonobankAPIError as exc:
            monobank_logger.warning('Failed to pull attempt status for %s: %s', invoice_id, exc)
    if status_value is None:
        fallback = (fallback_status or '').lower()
        return (fallback if fallback in MONOBANK_PENDING_STATUSES | MONOBANK_FAILURE_STATUSES else None), status_payload
    status_lower = str(status_value).lower()
    if status_lower in MONOBANK_SUCCESS_STATUSES:
        expected = attempt.payment_amount
        paid = None
        if isinstance(status_payload, dict):
            paid = status_payload.get('paidAmount')
            if paid is None:
                paid = status_payload.get('finalAmount')
            if paid is None:
                paid = status_payload.get('amount')
        if paid is not None:
            try:
                if int(paid) < int(Decimal(str(expected)) * 100):
                    return 'processing', status_payload
            except (TypeError, ValueError, ArithmeticError):
                return 'processing', status_payload
    return status_lower, status_payload


def _apply_payment_attempt_status(attempt, status, payload=None, source='webhook'):
    status = (status or '').lower()
    if status in MONOBANK_SUCCESS_STATUSES:
        try:
            order, created = materialize_payment_attempt(
                attempt.pk, status=status, payload=payload, source=source,
            )
        except PaymentAttemptConversionError as exc:
            PaymentAttempt.objects.filter(pk=attempt.pk).update(
                status=PaymentAttempt.Status.FAILED,
                error_reason=str(exc)[:500],
                last_status_at=timezone.now(),
            )
            monobank_logger.error('Payment attempt %s conversion failed: %s', attempt.pk, exc)
            return None, False
        if created:
            try:
                mark_checkout_capture_converted(order.session_key)
            except Exception:
                monobank_logger.debug('Failed to mark checkout capture converted for attempt %s', attempt.pk, exc_info=True)
            transaction.on_commit(
                lambda order_pk=order.pk, pay_type=order.pay_type: _dispatch_post_payment_events(
                    order_pk, 'unpaid', _normalize_order_pay_type(pay_type)
                )
            )
        return order, created

    terminal_status = {
        'failure': PaymentAttempt.Status.FAILED,
        'rejected': PaymentAttempt.Status.FAILED,
        'canceled': PaymentAttempt.Status.CANCELLED,
        'cancelled': PaymentAttempt.Status.CANCELLED,
        'reversed': PaymentAttempt.Status.FAILED,
        'expired': PaymentAttempt.Status.EXPIRED,
    }.get(status)
    if terminal_status:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status=terminal_status,
            error_reason=(status or 'failed')[:500],
            last_status_at=timezone.now(),
        )
    elif status in MONOBANK_PENDING_STATUSES:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status=PaymentAttempt.Status.PROCESSING,
            last_status_at=timezone.now(),
        )
    return None, False


def monobank_return(request):
    """
    Handle user return from Monobank payment page: fetch status, update order, redirect to thank you.
    """
    invoice_id = request.GET.get('invoiceId') or request.session.get('monobank_invoice_id')
    attempt_ref = request.GET.get('reference') or request.GET.get('attemptRef')
    attempt_id = request.GET.get('attemptId') or request.session.get('monobank_pending_attempt_id')
    attempt = _get_payment_attempt_by_refs(invoice_id=invoice_id, attempt_ref=attempt_ref, attempt_id=attempt_id)
    if attempt:
        status_value, status_payload = _resolve_attempt_invoice_status(
            attempt, invoice_id or attempt.monobank_invoice_id,
        )
        if status_value:
            order, _ = _apply_payment_attempt_status(
                attempt, status_value, payload=status_payload, source='return'
            )
            if order:
                from storefront.views.checkout import remember_order_in_session
                remember_order_in_session(request, order)
                _cleanup_after_success(request)
                return redirect('order_success', order_id=order.pk)
        messages.info(request, 'Оплату ще не підтверджено. Спробуйте ще раз після завершення платежу.')
        return redirect('cart')

    order_ref = request.GET.get('orderRef')
    order_id = request.GET.get('orderId') or request.session.get('monobank_pending_order_id')

    order = _get_order_by_payment_refs(invoice_id=invoice_id, order_ref=order_ref, order_id=order_id)
    if not order:
        messages.error(request, 'Замовлення не знайдено. Спробуйте ще раз.')
        return redirect('cart')

    # W1-3 (CRO-043): статус берём ТОЛЬКО из pull-истины (invoice/status API)
    # со сверкой суммы (W1-12). Убран unsafe fallback `or 'success'` — редирект
    # без подтверждения от API больше не переводит заказ в paid.
    status_value, status_payload = _resolve_retail_invoice_status(
        order, invoice_id or getattr(order, 'payment_invoice_id', None)
    )
    if status_value:
        applied_status = _apply_monobank_status(order, status_value, payload=status_payload, source='return')
    else:
        # Pull не удался — не трогаем платёжный статус, ждём webhook.
        applied_status = 'processing'

    # W1-2 (CRO-044): order_success now enforces ownership; make sure the
    # returning buyer's session is allowed to open the page. Only trust
    # SESSION-derived evidence here — GET params are attacker-controllable,
    # so they must not grant access to arbitrary orders.
    session_invoice = request.session.get('monobank_invoice_id')
    session_order_id = request.session.get('monobank_pending_order_id')
    owns_order = (
        (session_order_id and order.id == session_order_id)
        or (session_invoice and getattr(order, 'payment_invoice_id', None) == session_invoice)
        or (order.session_key and order.session_key == request.session.session_key)
        or (request.user.is_authenticated and order.user_id == request.user.id)
    )
    if owns_order:
        from storefront.views.checkout import remember_order_in_session
        remember_order_in_session(request, order)

    if applied_status in MONOBANK_SUCCESS_STATUSES:
        _cleanup_after_success(request)
        messages.success(request, 'Оплату успішно отримано!')
        return redirect('order_success', order_id=order.id)

    if applied_status in MONOBANK_PENDING_STATUSES:
        messages.info(request, 'Платіж обробляється. Ми повідомимо, щойно отримаємо підтвердження.')
        return redirect('order_success', order_id=order.id)

    messages.error(request, 'Оплату не завершено. Ви можете повторити спробу або обрати інший спосіб оплати.')
    return redirect('cart')


@csrf_exempt
def _webhook_signature_ok(request):
    """
    W1-3/W1-9 (CRO-043, NEW-501): единая проверка X-Sign для вебхуков Monobank.

    Один endpoint принимает вебхуки двух мерчантов (storefront-корзина и
    acquiring `mono_hrefs` для накладних), у каждого свой публичный ключ —
    поэтому пробуем оба.
    """
    if _verify_monobank_signature(request):
        return True
    try:
        from management.services.invoice_payments import (
            ACQUIRING_PUBKEY_CACHE_KEY,
            acquiring_token,
        )
        acq_token = acquiring_token()
        if acq_token and acq_token != getattr(settings, 'MONOBANK_TOKEN', None):
            return _verify_monobank_signature(
                request, token=acq_token, cache_key=ACQUIRING_PUBKEY_CACHE_KEY
            )
    except Exception:
        pass
    return False


def _resolve_retail_invoice_status(order, invoice_id, fallback_status=None):
    """
    W1-12 (NEW-506): pull-истина для retail-заказов.

    Статус подтверждаем ТОЛЬКО запросом /api/merchant/invoice/status, а для
    успешных статусов сверяем фактически оплаченную сумму с ожидаемой.
    Расхождение суммы → 'processing' (заказ уходит в checking, НЕ в paid).

    Returns:
        tuple(status_value, status_payload)
    """
    status_value = None
    status_payload = None
    if invoice_id:
        try:
            status_payload = _monobank_api_request(
                'GET', '/api/merchant/invoice/status', params={'invoiceId': invoice_id}
            )
            if isinstance(status_payload.get('result'), dict):
                status_payload = status_payload['result']
            status_value = status_payload.get('status') or status_payload.get('statusCode')
        except MonobankAPIError as exc:
            monobank_logger.warning('Failed to pull invoice status for %s: %s', invoice_id, exc)

    if status_value is None:
        # Pull не удался — деньги не подтверждаем. Максимум pending.
        fallback_lower = (fallback_status or '').lower()
        if fallback_lower in MONOBANK_PENDING_STATUSES or fallback_lower in MONOBANK_FAILURE_STATUSES:
            return fallback_lower, status_payload
        if fallback_lower in MONOBANK_SUCCESS_STATUSES:
            monobank_logger.warning(
                'Invoice %s: success status from untrusted source without pull confirmation -> checking',
                invoice_id,
            )
            return 'processing', status_payload
        return None, status_payload

    status_lower = (status_value or '').lower()
    if status_lower in MONOBANK_SUCCESS_STATUSES and isinstance(status_payload, dict):
        try:
            pay_type = _normalize_order_pay_type(getattr(order, 'pay_type', None))
            if pay_type == 'prepay_200':
                expected = order.get_prepayment_amount()
            else:
                expected = (order.total_sum or 0) - (order.discount_amount or 0)
            expected_minor = int((Decimal(str(expected)) * 100).to_integral_value())

            paid_minor = status_payload.get('paidAmount')
            if paid_minor is None:
                paid_minor = status_payload.get('finalAmount')
            if paid_minor is None:
                paid_minor = status_payload.get('amount')

            if paid_minor is not None and expected_minor > 0 and int(paid_minor) < expected_minor:
                monobank_logger.error(
                    'Invoice %s (order %s): paid amount %s < expected %s minor units -> checking, NOT paid',
                    invoice_id, order.pk, paid_minor, expected_minor,
                )
                return 'processing', status_payload
        except Exception:
            monobank_logger.warning(
                'Invoice %s (order %s): amount reconciliation failed -> checking',
                invoice_id, order.pk, exc_info=True,
            )
            return 'processing', status_payload

    return status_lower, status_payload


def monobank_webhook(request):
    """
    Receive status updates from Monobank webhook.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)

    # W1-3 (CRO-043): без валидной подписи X-Sign body недоверенный → 400.
    if not _webhook_signature_ok(request):
        monobank_logger.warning('Monobank webhook rejected: invalid or missing X-Sign')
        return HttpResponse(status=400)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return HttpResponse(status=400)

    invoice_id = payload.get('invoiceId') or payload.get('invoice_id')
    result = payload.get('result') or payload
    order_ref = result.get('orderRef') or result.get('order_ref')
    order_id = result.get('orderId') or result.get('order_id')
    attempt_ref = (
        result.get('reference')
        or result.get('attemptRef')
        or result.get('attempt_ref')
        or payload.get('reference')
    )

    attempt = _get_payment_attempt_by_refs(invoice_id=invoice_id, attempt_ref=attempt_ref)
    if attempt:
        body_status = result.get('status') or payload.get('status')
        verified_status, verified_payload = _resolve_attempt_invoice_status(
            attempt,
            invoice_id or attempt.monobank_invoice_id,
            fallback_status=body_status,
        )
        if verified_status:
            _apply_payment_attempt_status(
                attempt,
                verified_status,
                payload=verified_payload or payload,
                source='webhook',
            )
        return JsonResponse({'ok': True})

    order = _get_order_by_payment_refs(invoice_id=invoice_id, order_ref=order_ref, order_id=order_id)
    if not order:
        # Also handle Management wholesale invoices paid via Monobank
        if invoice_id:
            try:
                from orders.models import WholesaleInvoice
            except Exception:
                WholesaleInvoice = None

            inv = None
            if WholesaleInvoice is not None:
                try:
                    inv = WholesaleInvoice.objects.filter(monobank_invoice_id=invoice_id).select_related('created_by', 'created_by__userprofile').first()
                except Exception:
                    inv = None

            if inv:
                # Гроші підтверджуємо ТІЛЬКИ pull-істиною через acquiring-токен
                # (захист від підробки webhook). Делегуємо в management-сервіс.
                try:
                    from management.services.invoice_payments import process_webhook as _mgmt_process_webhook
                    old_payment_status = inv.payment_status
                    _mgmt_process_webhook(inv, payload, request=request)
                    inv.refresh_from_db()
                    if old_payment_status != inv.payment_status:
                        monobank_logger.info(
                            'WholesaleInvoice %s payment_status %s -> %s via webhook (pull-verified)',
                            inv.id, old_payment_status, inv.payment_status,
                        )
                except Exception as exc:
                    monobank_logger.warning('Management invoice webhook processing failed for %s: %s', inv.id, exc)
                return JsonResponse({'ok': True})

            # IG-бот: invoice угоди (замовлення ще не створене — Q2). Pull-verify.
            try:
                from management.services import bot_payments
                if bot_payments.handle_webhook_invoice(invoice_id, payload, request=request):
                    return JsonResponse({'ok': True})
            except Exception as exc:
                monobank_logger.warning('IG deal webhook processing failed for %s: %s', invoice_id, exc)

        monobank_logger.warning('Webhook received for unknown invoice/order: %s / %s', invoice_id, order_ref)
        return JsonResponse({'ok': True})

    # W1-12 (NEW-506): retail-путь подтверждает деньги ТОЛЬКО pull-истиной
    # + сверкой суммы, как это уже делают wholesale/IG-ветки выше.
    body_status = result.get('status') or payload.get('status')
    verified_status, verified_payload = _resolve_retail_invoice_status(
        order,
        invoice_id or getattr(order, 'payment_invoice_id', None),
        fallback_status=body_status,
    )
    if verified_status:
        _apply_monobank_status(order, verified_status, payload=verified_payload or payload, source='webhook')
    return JsonResponse({'ok': True})
