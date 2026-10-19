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
import hashlib
import secrets
from urllib.parse import urlparse
from decimal import Decimal
from datetime import timedelta

from django.shortcuts import redirect
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.dateparse import parse_datetime
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.contrib import messages

import requests

from base64_utils import InvalidBase64, strict_b64decode
from ..models import Product, PromoCode
from storefront.custom_print_creation import CreationValidationError, custom_print_checkout_payload
from orders.services.delivery_payment import DeliveryPaymentError, build_custom_print_delivery_contract
from orders.nova_poshta_data import apply_nova_poshta_refs
from orders.nova_poshta_documents import normalize_checkout_phone
from orders.models import Order as OrderModel, OrderItem, PaymentAttempt
from orders.payment_attempts import materialize_payment_attempt, PaymentAttemptConversionError
from storefront.services.promo_composition import session_promo_ids
from orders.promo_reservations import (
    PromoReservationError,
    release_payment_attempt_promo,
    reserve_promos_for_checkout,
)
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
    _reset_monobank_session,
    get_validated_cart_from_session,
    _get_color_variant_safe,
    _normalize_order_pay_type,
)


# Loggers
monobank_logger = logging.getLogger('storefront.monobank')
cart_logger = logging.getLogger('storefront.cart')
CLIENT_TRACKING_ALLOWED_KEYS = frozenset({'fbp', 'fbc'})
INVOICE_CREATION_LEASE_DURATION = timedelta(minutes=5)


def _invoice_creation_lease_expired(event_state, *, now=None):
    """Return whether an unfinished provider call is no longer owned safely."""
    raw_expires_at = (event_state or {}).get('invoice_creation_lease_expires_at')
    if not raw_expires_at:
        return False
    expires_at = parse_datetime(str(raw_expires_at))
    if expires_at is None:
        return True
    if timezone.is_naive(expires_at):
        expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
    return expires_at <= (now or timezone.now())


def _ambiguous_payment_attempt_response():
    return JsonResponse({
        'success': False,
        'provider_ambiguous': True,
        'retry_after_ms': 5000,
        'error_code': 'provider_ambiguous',
        'error': _('Платіж уже передано банку. Не повторюйте оплату, ми перевіряємо статус.'),
    }, status=409)


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
    return request.build_absolute_uri(reverse('cart'))


def _schedule_missing_add_payment_info(order, request=None):
    """Ensure reused PaymentAttempt invoices retain their durable side effects."""
    if not isinstance(order, PaymentAttempt):
        return
    source_url = _capi_checkout_source_url(request) if request is not None else None
    from orders.payment_side_effects import enqueue_attempt_invoice_side_effects

    enqueue_attempt_invoice_side_effects(order.pk, source_url=source_url)

# Константы статусов Monobank
MONOBANK_SUCCESS_STATUSES = {'success'}
# A hold is not captured money in the current one-stage contract.
MONOBANK_PENDING_STATUSES = {'processing', 'hold'}
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
    pricing_snapshot=None,
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
        'pricing_snapshot': pricing_snapshot,
    }
    promo_ids = sorted(session_promo_ids(request.session))
    if promo_ids:
        state['promo_code_id'] = promo_ids[0]
    if len(promo_ids) > 1:
        state['promo_code_ids'] = promo_ids
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
            'error_code': 'payment_in_progress',
            'error': _('Платіж уже створюється. Зачекайте кілька секунд.'),
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
            'error_code': 'payment_in_progress',
            'error': _('Платіж уже створюється. Зачекайте кілька секунд.'),
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

        try:
            signature_bytes = strict_b64decode(signature)
        except InvalidBase64:
            monobank_logger.warning('Monobank signature rejected: invalid Base64')
            return False

        # Получаем публичный ключ
        public_key_pem = _get_monobank_public_key(token=token, cache_key=cache_key)
        if not public_key_pem:
            monobank_logger.error('Failed to get Monobank public key for verification')
            return False

        # Получаем тело запроса
        body = request.body

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
                pem_bytes = strict_b64decode(raw)
            except InvalidBase64:
                monobank_logger.warning('Failed to decode Monobank public key')
                return False
            if b'-----BEGIN' not in pem_bytes:
                monobank_logger.warning('Failed to load Monobank public key')
                return False

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
    """Classified API failure; ambiguous means the provider may have acted."""

    def __init__(self, message, *, ambiguous=False):
        super().__init__(message)
        self.ambiguous = bool(ambiguous)


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
            raise MonobankAPIError(
                f'Monobank API error: {error_msg}',
                ambiguous=response.status_code >= 500,
            )

        return data

    except requests.exceptions.Timeout:
        raise MonobankAPIError(
            'Timeout при з\'єднанні з Monobank',
            ambiguous=True,
        )
    except requests.exceptions.RequestException as e:
        raise MonobankAPIError(
            f'Помилка з\'єднання з Monobank: {str(e)}',
            ambiguous=True,
        )


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
            'error_code': 'custom_price_required',
            'error': _('Для погодженого кастомного виробу ще не зафіксована фінальна ціна.'),
        }, status=400)
    if not cart and not approved_leads:
        return JsonResponse({
            'success': False,
            'error_code': 'cart_empty',
            'error': _('Кошик порожній. Додайте товари перед оплатою.'),
        }, status=400)

    try:
        delivery = resolve_delivery_selection(body)
    except NovaPoshtaSelectionError as exc:
        return JsonResponse({
            'success': False,
            'error_code': 'delivery_selection',
            'field': exc.field,
            'error': _(exc.message),
        }, status=400)

    if request.user.is_authenticated:
        try:
            profile = request.user.userprofile
        except Exception:
            return JsonResponse({
                'success': False,
                'error_code': 'profile_required',
                'error': _('Будь ласка, заповніть профіль доставки.'),
            }, status=400)

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
        return JsonResponse({
            'success': False,
            'error_code': 'invalid_payment_type',
            'field': 'pay_type',
            'error': _('Оберіть коректний тип оплати.'),
        }, status=400)
    if pay_type != 'online_full':
        from storefront.services.brigade_commerce import products_require_full_payment, brigade_payment_error
        policy_products = Product.objects.select_related('category').filter(
            pk__in=[item['product_id'] for item in cart.values()]
        )
        if products_require_full_payment(policy_products):
            return JsonResponse({
                'success': False,
                'error_code': 'brigade_full_payment_required',
                'field': 'pay_type',
                'error': brigade_payment_error(),
            }, status=400)
    if pay_type == 'cod':
        return JsonResponse({
            'success': False,
            'error_code': 'cod_unsupported',
            'field': 'pay_type',
            'error': _('Оплата при отриманні недоступна. Оберіть повну онлайн-оплату або передплату.'),
        }, status=400)
    if pay_type not in {'online_full', 'prepay_200'}:
        return JsonResponse({
            'success': False,
            'error_code': 'payment_type_unavailable',
            'field': 'pay_type',
            'error': _('Оберіть доступний спосіб оплати.'),
        }, status=400)
    if approved_leads and pay_type == 'prepay_200':
        return JsonResponse({
            'success': False,
            'error_code': 'custom_prepay_unsupported',
            'error': _('Передплата 200 грн недоступна для кастомного принта.'),
        }, status=400)
    if raw_phone and not phone:
        return JsonResponse({
            'success': False,
            'error_code': 'invalid_phone',
            'field': 'phone',
            'error': _('Вкажіть коректний український номер телефону. Можна без +380.'),
        }, status=400)
    if not all([full_name, phone, delivery.city, delivery.np_office]):
        return JsonResponse({
            'success': False,
            'error_code': 'required_fields',
            'error': _('Будь ласка, заповніть всі обовʼязкові поля.'),
        }, status=400)

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
    products = Product.objects.select_related('category').in_bulk(ids)
    if any(not products.get(item['product_id']) for item in cart.values()):
        return JsonResponse({
            'success': False,
            'error_code': 'products_unavailable',
            'error': _('Деякі товари більше недоступні. Оновіть кошик.'),
        }, status=400)

    from productcolors.models import ProductColorVariant
    from product_catalog.services import effective_cart_unit_price, variant_allows_purchase
    variant_ids = [item.get('color_variant_id') for item in cart.values() if item.get('color_variant_id')]
    variants = ProductColorVariant.objects.in_bulk(variant_ids)
    from storefront.services.brigade_commerce import calculate_brigade_cart_pricing, brigade_payment_error
    brigade_pricing = calculate_brigade_cart_pricing(cart, products, variants)
    if brigade_pricing.full_payment_only and pay_type != 'online_full':
        return JsonResponse({'success': False, 'error_code': 'brigade_full_payment_required', 'field': 'pay_type', 'error': brigade_payment_error()}, status=400)
    snapshot_items = []
    gross = brigade_pricing.subtotal
    for item_key, item in cart.items():
        product = products[int(item['product_id'])]
        variant = variants.get(int(item['color_variant_id'])) if item.get('color_variant_id') else None
        if item.get('color_variant_id') and (
            variant is None or variant.product_id != product.pk or not variant_allows_purchase(
                product, variant, fit_code=item.get('fit_option_code') or item.get('fit') or '',
                size=item.get('size') or '', option_values=item.get('option_values') or {},
            )
        ):
            return JsonResponse({
                'success': False,
                'error_code': 'variant_unavailable',
                'error': _('Обраний варіант товару більше недоступний.'),
            }, status=400)
        line_price = brigade_pricing.lines[item_key]
        for part in line_price.frozen_price_parts():
            qty, unit = part['qty'], Decimal(part['unit_price'])
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
                'line_total': str(unit * qty),
                'brigade_pricing': line_price.public_metadata(),
                'bundle_pricing': line_price.bundle_metadata(),
                'pricing_rule': part['rule'],
                'promo_eligible': part['promo_eligible'],
            })
    gross += sum((Decimal(str(lead.final_price_value)) for lead in approved_leads), Decimal('0.00'))
    try:
        custom_creation_data = custom_print_checkout_payload(approved_leads)
    except CreationValidationError:
        return JsonResponse({'success': False, 'error_code': 'custom_print_delivery_conflict', 'field': 'gift_delivery',
                             'error': 'Подарункові опції та доставку потрібно узгодити з менеджером перед оплатою.'}, status=400)
    if gross <= 0:
        return JsonResponse({
            'success': False,
            'error_code': 'invalid_amount',
            'error': _('Сума замовлення повинна бути більше 0.'),
        }, status=400)

    try:
        promo_ids = session_promo_ids(request.session)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error_code': 'promo_invalid',
                             'error': _('Оновіть промокоди в кошику.')}, status=400)
    delivery_refs = {
        'np_settlement_ref': delivery.settlement_ref,
        'np_city_ref': delivery.city_ref,
        'np_warehouse_ref': delivery.warehouse_ref,
    }
    fingerprint = _build_checkout_idempotency_key(
        request, cart=cart, approved_custom_leads=approved_leads, full_name=full_name,
        phone=phone, email=normalized_email, delivery_refs=delivery_refs,
        city=delivery.city, np_office=delivery.np_office, pay_type=pay_type,
        pricing_snapshot=snapshot_items,
    )
    try:
        with transaction.atomic():
            _lock_checkout_identity(request)
            attempt = PaymentAttempt.objects.select_for_update().filter(fingerprint=fingerprint).first()
            if attempt and attempt.order_id:
                return _existing_payment_attempt_response(attempt, approved_keys, pending_keys, request)
            if attempt and (attempt.event_state or {}).get('invoice_creation_ambiguous'):
                return _ambiguous_payment_attempt_response()
            if attempt and attempt.status in {PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.PROCESSING}:
                if attempt.invoice_url:
                    return _existing_payment_attempt_response(attempt, approved_keys, pending_keys, request)
                if (
                    attempt.status == PaymentAttempt.Status.PROCESSING
                    and _invoice_creation_lease_expired(attempt.event_state)
                ):
                    event_state = dict(attempt.event_state or {})
                    event_state['invoice_creation_ambiguous'] = True
                    event_state.pop('invoice_creation_lease', None)
                    event_state.pop('invoice_creation_lease_expires_at', None)
                    PaymentAttempt.objects.filter(pk=attempt.pk).update(
                        status=PaymentAttempt.Status.PROCESSING,
                        event_state=event_state,
                        error_reason='invoice_creation_ambiguous:stale_lease',
                        last_status_at=timezone.now(),
                    )
                    return _ambiguous_payment_attempt_response()
                return _existing_payment_attempt_response(attempt, approved_keys, pending_keys, request)
            if attempt:
                fingerprint = hashlib.sha256(f'{fingerprint}:{timezone.now().timestamp()}'.encode()).hexdigest()

            promo = None
            discount = Decimal('0.00')
            promo_event_state = {}
            if promo_ids and brigade_pricing.promo_eligible_subtotal > 0:
                reservation = reserve_promos_for_checkout(
                    promo_ids=promo_ids,
                    user=request.user,
                    total_amount=brigade_pricing.promo_eligible_subtotal,
                )
                promo = reservation.promo
                discount = reservation.discount
                promo_event_state = reservation.event_state
            payable = max(gross - discount, Decimal('0.00'))
            if payable <= 0:
                raise PromoReservationError('invalid_amount')
            payment_amount = min(Decimal('200.00'), payable) if pay_type == 'prepay_200' else payable
            build_custom_print_delivery_contract(gross_total=gross, discount_amount=discount, creation_data=custom_creation_data)
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
                    'brigade_commerce': brigade_pricing.public_metadata(),
                    'custom_print_lead_ids': [lead.pk for lead in approved_leads],
                    'custom_print_creation': custom_creation_data,
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
                event_state=promo_event_state,
            )
            invoice_lease_state = dict(attempt.event_state or {})
            invoice_lease_state['invoice_creation_lease'] = secrets.token_urlsafe(24)
            invoice_lease_state['invoice_creation_lease_expires_at'] = (
                timezone.now() + INVOICE_CREATION_LEASE_DURATION
            ).isoformat()
            attempt.status = PaymentAttempt.Status.PROCESSING
            attempt.event_state = invoice_lease_state
            attempt.last_status_at = timezone.now()
            attempt.save(update_fields=['status', 'event_state', 'last_status_at', 'updated'])
    except DeliveryPaymentError:
        return JsonResponse({'success': False, 'error_code': 'custom_print_delivery_conflict', 'field': 'gift_delivery',
                             'error': 'Сума або спосіб доставки подарунка потребують погодження менеджера.'}, status=400)
    except PromoReservationError:
        return JsonResponse({
            'success': False,
            'error_code': 'promo_invalid',
            'field': 'promo_code',
            'error': _('Промокод недійсний, неактивний або вже зарезервований.'),
        }, status=400)

    record_initiate_checkout(request, float(payable))
    description = (
        f'Передплата 200 грн для {attempt.reference}. Повна сума: {payable:.2f} грн.'
        if pay_type == 'prepay_200'
        else f'Оплата замовлення {attempt.reference} на суму {payable:.2f} грн.'
    )
    basket = [
        {'name': item['title'], 'qty': item['qty'], 'sum': int(Decimal(item['line_total']) * 100), 'unit': 'шт'}
        for item in snapshot_items
    ]
    for lead in approved_leads:
        basket.append({'name': f'Кастомний виріб {lead.lead_number}', 'qty': int(getattr(lead, 'quantity', 0) or 1), 'sum': int(Decimal(str(lead.final_price_value)) * 100), 'unit': 'шт'})
    if discount > 0:
        basket.append({'name': f'Знижка по промокоду {promo.code}', 'qty': 1, 'sum': -int(discount * 100), 'unit': 'шт'})
    if pay_type == 'prepay_200':
        basket = [{'name': description, 'qty': 1, 'sum': int(payment_amount * 100), 'unit': 'шт'}]
    elif len(basket) > 30:
        basket = [{'name': description, 'qty': 1, 'sum': int(payment_amount * 100), 'unit': 'шт'}]
    payload = {
        'amount': int(payment_amount * 100), 'ccy': 980,
        'merchantPaymInfo': {'reference': attempt.reference, 'destination': description, 'basketOrder': basket},
        'redirectUrl': request.build_absolute_uri(reverse('monobank_return')),
        'webHookUrl': request.build_absolute_uri('/payments/monobank/webhook/'),
    }
    try:
        creation = _monobank_api_request('POST', '/api/merchant/invoice/create', json_payload=payload)
        result = creation.get('result') or creation
        invoice_id, invoice_url = result.get('invoiceId'), result.get('pageUrl')
        if not invoice_id or not invoice_url:
            raise MonobankAPIError('Monobank returned an invalid invoice')
    except Exception as exc:
        event_state = dict(attempt.event_state or {})
        event_state.pop('invoice_creation_lease', None)
        event_state.pop('invoice_creation_lease_expires_at', None)
        ambiguous = bool(getattr(exc, 'ambiguous', True))
        if ambiguous:
            event_state['invoice_creation_ambiguous'] = True
        else:
            event_state.pop('invoice_creation_ambiguous', None)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            status=PaymentAttempt.Status.PROCESSING if ambiguous else PaymentAttempt.Status.FAILED,
            event_state=event_state,
            error_reason=(f'invoice_creation_ambiguous:{exc}' if ambiguous else str(exc))[:500],
            last_status_at=timezone.now(),
        )
        if not ambiguous:
            release_payment_attempt_promo(attempt, reason='invoice_creation_failed')
        return JsonResponse({
            'success': False,
            'error_code': 'provider_error',
            'error': _('Не вдалося створити платіж. Спробуйте ще раз.'),
        }, status=200 if isinstance(exc, MonobankAPIError) and not ambiguous else 502)

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
    event_state = dict(attempt.event_state or {})
    event_state.pop('invoice_creation_lease', None)
    event_state.pop('invoice_creation_lease_expires_at', None)
    event_state.pop('invoice_creation_ambiguous', None)
    from orders.payment_side_effects import enqueue_attempt_invoice_side_effects

    source_url = _capi_checkout_source_url(request)
    with transaction.atomic():
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            monobank_invoice_id=invoice_id,
            invoice_url=invoice_url,
            invoice_payload={'request': payload, 'create': creation},
            tracking_payload=tracking,
            invoice_expires_at=timezone.now() + timedelta(hours=24),
            event_state=event_state,
            status=PaymentAttempt.Status.PROCESSING,
            last_status_at=timezone.now(),
        )
        enqueue_attempt_invoice_side_effects(
            attempt.pk,
            source_url=source_url,
        )
    attempt.refresh_from_db()
    request.session['monobank_invoice_id'] = invoice_id
    request.session['monobank_pending_attempt_id'] = attempt.pk
    request.session['monobank_attempt_id'] = attempt.pk
    request.session['monobank_approved_custom_keys'] = approved_keys
    request.session['monobank_pending_custom_keys'] = pending_keys
    request.session.modified = True

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
    request.session.pop('promo_code_ids', None)
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
            # order lock. Provider I/O is owned by the durable cron consumer.
            _record_promo_usage_for_order(order)
            from orders.payment_side_effects import enqueue_order_post_payment_side_effect

            enqueue_order_post_payment_side_effect(
                order.pk,
                previous_status=old_payment_status or 'unpaid',
                pay_type=canonical_pay_type,
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


def _request_owns_payment_attempt(request, attempt):
    """Authorize browser return handling from server-owned session/user facts."""
    session_attempt_ids = {
        str(value)
        for value in (
            request.session.get('monobank_pending_attempt_id'),
            request.session.get('monobank_attempt_id'),
        )
        if value not in (None, '')
    }
    session_invoice = request.session.get('monobank_invoice_id')
    session_key = request.session.session_key
    return bool(
        str(attempt.pk) in session_attempt_ids
        or (
            session_invoice
            and str(session_invoice) == str(attempt.monobank_invoice_id or '')
        )
        or (session_key and attempt.session_key and session_key == attempt.session_key)
        or (
            request.user.is_authenticated
            and attempt.user_id
            and request.user.pk == attempt.user_id
        )
    )


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
    snapshot = attempt.cart_snapshot if isinstance(attempt.cart_snapshot, dict) else {}
    if (snapshot.get('checkout_surface') == 'instagram_proposal'
            and (attempt.order_id or Decimal(attempt.paid_amount or 0) > 0)
            and status_lower in {'success', 'reversed'}):
        # Paid receipts use the canonical gross/net gate. An initial preview
        # may be stale; its unmodified receipt fields survive below for locked
        # dispatch to recheck whether conversion committed while it waited.
        return status_lower, status_payload
    if status_lower in MONOBANK_SUCCESS_STATUSES:
        snapshot = attempt.cart_snapshot if isinstance(attempt.cart_snapshot, dict) else {}
        if snapshot.get('checkout_surface') == 'instagram_proposal':
            from management.services.ig_checkout_payment import _provider_aliases_match
            if not _provider_aliases_match(attempt, status_payload, status=status_lower):
                monobank_logger.error(
                    'Assisted attempt %s provider identity/currency mismatch -> checking',
                    attempt.pk,
                )
                status_payload = {
                    **status_payload,
                    "_twc_reconciliation_reason": "identity_currency_mismatch",
                }
                return 'processing', status_payload
        expected = attempt.payment_amount
        observed_amounts = []
        if isinstance(status_payload, dict):
            for field in ('paidAmount', 'finalAmount', 'amount'):
                paid = status_payload.get(field)
                if paid is None:
                    continue
                from management.services.ig_checkout_payment import _settlement_minor
                paid_minor = _settlement_minor(paid)
                if paid_minor is None:
                    observed_amounts = None
                    break
                observed_amounts.append(paid_minor)
        if observed_amounts == []:
            status_payload = {
                **status_payload,
                "_twc_reconciliation_reason": "amount_missing",
            }
            return 'processing', status_payload
        if observed_amounts is None:
            status_payload = {
                **status_payload,
                "_twc_reconciliation_reason": "amount_malformed",
            }
            return 'processing', status_payload
        if len(set(observed_amounts)) != 1:
            status_payload = {
                **status_payload,
                "_twc_reconciliation_reason": "amount_conflict",
            }
            return 'processing', status_payload
        paid_minor = observed_amounts[0]
        try:
            if paid_minor != int(Decimal(str(expected)) * 100):
                status_payload = {
                    **status_payload,
                    "_twc_reconciliation_reason": "amount_mismatch",
                }
                return 'processing', status_payload
        except (TypeError, ValueError, ArithmeticError):
            status_payload = {
                **status_payload,
                "_twc_reconciliation_reason": "amount_malformed",
            }
            return 'processing', status_payload
    return status_lower, status_payload


@transaction.atomic
def _apply_payment_attempt_status(attempt, status, payload=None, source='webhook'):
    status = (status or '').lower()
    from management.services.ig_checkout_generation import generation_for_attempt

    if generation_for_attempt(attempt.pk) is not None:
        from management.services.ig_checkout_generation import (
            apply_generation_provider_status,
        )

        return apply_generation_provider_status(
            attempt.pk,
            status,
            payload=payload,
            source=source,
        )
    from management.services.ig_checkout_payment import (
        _lock_attempt_proposal_graph,
    )

    attempt, _deal, _proposal = _lock_attempt_proposal_graph(attempt.pk)
    if (_proposal is not None and status == 'processing' and isinstance(payload, dict)
            and payload.get("_twc_reconciliation_reason")
            and (attempt.order_id or Decimal(attempt.paid_amount or 0) > 0)):
        raw_status = str(payload.get('status') or payload.get('statusCode') or '').lower()
        if raw_status in {'success', 'reversed'}:
            status = raw_status
    if (_proposal is not None and status in {'success', 'reversed'}
            and (attempt.order_id or Decimal(attempt.paid_amount or 0) > 0)):
        from management.services.ig_checkout_payment import project_attempt_settlement
        project_attempt_settlement(attempt.pk, status=status, payload=payload, source=source)
        return (attempt.order if status == 'success' and attempt.order_id else None), False
    if _proposal is not None and status == 'reversed':
        from management.services.ig_checkout_payment import _provider_aliases_match
        if not _provider_aliases_match(attempt, payload, status=status, require_complete=False):
            payload = {**(payload or {}), "_twc_reconciliation_reason": "identity_currency_mismatch"}
            status = 'processing'
    if _proposal is not None and status in MONOBANK_SUCCESS_STATUSES:
        from management.services.ig_checkout_payment import _initial_hosted_payment_reason
        reason = _initial_hosted_payment_reason(attempt, payload, status=status)
        if reason:
            payload = {**(payload or {}), "_twc_reconciliation_reason": reason}
            status = 'processing'
    if status in MONOBANK_SUCCESS_STATUSES:
        local_terminal = dict(
            (attempt.event_state or {}).get("local_terminalization") or {}
        )
        if local_terminal and _proposal is None:
            from management.services.ig_checkout_terminalization import (
                record_orphan_provider_observation,
            )

            record_orphan_provider_observation(
                attempt.pk,
                status=status,
                payload=payload,
                source=source,
            )
            return None, False
        from management.services.ig_checkout_payment import (
            record_late_local_payment_for_review,
        )

        if record_late_local_payment_for_review(
            attempt.pk,
            payload=payload,
            source=source,
        ):
            return None, False
        if not attempt.order_id and attempt.status in {
            PaymentAttempt.Status.FAILED,
            PaymentAttempt.Status.EXPIRED,
            PaymentAttempt.Status.CANCELLED,
        }:
            # A later pull-verified success is stronger truth than an earlier
            # terminal observation. Re-open only while no Order exists.
            attempt.status = PaymentAttempt.Status.PROCESSING
            attempt.error_reason = ''
            attempt.last_status_at = timezone.now()
            attempt.save(update_fields=[
                'status', 'error_reason', 'last_status_at', 'updated',
            ])
        try:
            order, created = materialize_payment_attempt(
                attempt.pk, status=status, payload=payload, source=source,
            )
        except PaymentAttemptConversionError as exc:
            event_state = dict(attempt.event_state or {})
            if getattr(exc, "retryable", False):
                marker = str(getattr(exc, "marker", "") or "").strip()
                if marker:
                    event_state[marker] = True
            PaymentAttempt.objects.filter(pk=attempt.pk).update(
                status=(
                    PaymentAttempt.Status.PROCESSING
                    if getattr(exc, "retryable", False)
                    else PaymentAttempt.Status.FAILED
                ),
                event_state=event_state,
                error_reason=str(exc)[:500],
                last_status_at=timezone.now(),
            )
            monobank_logger.error('Payment attempt %s conversion failed: %s', attempt.pk, exc)
            return None, False
        # A proposal-backed attempt uses the same canonical Order materializer
        # as the storefront, then projects the verified payment back into the
        # Instagram commercial episode.  This is deliberately idempotent and
        # runs on repeated webhook/return deliveries too.
        try:
            from management.services.ig_checkout_payment import bind_verified_payment

            bind_verified_payment(attempt.pk, order)
        except Exception:
            monobank_logger.exception(
                'Failed to bind Instagram checkout attempt %s to order %s',
                attempt.pk,
                getattr(order, 'pk', None),
            )
        if created:
            try:
                mark_checkout_capture_converted(order.session_key)
            except Exception:
                monobank_logger.debug('Failed to mark checkout capture converted for attempt %s', attempt.pk, exc_info=True)
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
        if attempt.order_id or attempt.status in {
            PaymentAttempt.Status.PAID,
            PaymentAttempt.Status.PREPAID,
            PaymentAttempt.Status.CONVERTED,
        }:
            return None, False
        attempt.status = terminal_status
        attempt.error_reason = (status or 'failed')[:500]
        attempt.last_status_at = timezone.now()
        attempt.save(update_fields=[
            'status', 'error_reason', 'last_status_at', 'updated',
        ])
        try:
            from management.services.ig_checkout_payment import (
                project_terminal_payment,
                release_attempt_promo,
            )
            from management.services.ig_inventory import release_attempt_inventory

            release_attempt_inventory(attempt, reason=f"provider_{status or 'failed'}")
            release_attempt_promo(attempt, reason=f"provider_{status or 'failed'}")
            project_terminal_payment(
                attempt.pk,
                status=status,
                payload=payload,
                source=source,
            )
        except Exception:
            monobank_logger.exception(
                'Failed to project Instagram checkout terminal status for attempt %s',
                attempt.pk,
            )
    elif status in MONOBANK_PENDING_STATUSES:
        if attempt.order_id or attempt.status in {
            PaymentAttempt.Status.PAID, PaymentAttempt.Status.PREPAID, PaymentAttempt.Status.CONVERTED,
        }:
            return None, False
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
        if not _request_owns_payment_attempt(request, attempt):
            messages.error(request, _('Платіж не знайдено. Спробуйте ще раз.'))
            return redirect('cart')
        try:
            proposal = attempt.instagram_checkout_proposal
        except Exception:
            try:
                proposal = attempt.instagram_checkout_generation.proposal
            except Exception:
                proposal = None
        proposal_id = getattr(proposal, "public_id", None) or request.session.get(
            "ig_checkout_proposal_id"
        )
        status_value, status_payload = _resolve_attempt_invoice_status(
            attempt, invoice_id or attempt.monobank_invoice_id,
        )
        if status_value:
            order, _created = _apply_payment_attempt_status(
                attempt, status_value, payload=status_payload, source='return'
            )
            if order:
                from storefront.views.checkout import remember_order_in_session
                remember_order_in_session(request, order)
                request.session['ig_checkout_paid_attempt_id'] = attempt.pk
                _cleanup_after_success(request)
                if proposal_id:
                    return redirect("ig_checkout_proposal", proposal_id=proposal_id)
                return redirect('order_success', order_id=order.pk)
        messages.info(request, _('Платіж ще не підтверджено. Спробуйте ще раз після завершення оплати.'))
        if proposal_id:
            return redirect("ig_checkout_proposal", proposal_id=proposal_id)
        return redirect('cart')

    order_ref = request.GET.get('orderRef')
    order_id = request.GET.get('orderId') or request.session.get('monobank_pending_order_id')

    order = _get_order_by_payment_refs(invoice_id=invoice_id, order_ref=order_ref, order_id=order_id)
    if not order:
        messages.error(request, _('Замовлення не знайдено. Спробуйте ще раз.'))
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
        messages.success(request, _('Оплата успішно пройшла!'))
        return redirect('order_success', order_id=order.id)

    if applied_status in MONOBANK_PENDING_STATUSES:
        messages.info(request, _('Платіж обробляється. Ми повідомимо, щойно отримаємо підтвердження.'))
        return redirect('order_success', order_id=order.id)

    messages.error(request, _('Платіж не пройшов. Спробуйте ще раз або оберіть інший спосіб оплати.'))
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
