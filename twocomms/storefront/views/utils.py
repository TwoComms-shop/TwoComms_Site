"""
Утилиты и helper функции для views модуля storefront.

Содержит общие функции, которые используются в разных view модулях.
"""

import hashlib
import time
from urllib.parse import urlencode
from functools import wraps

from django.core.cache import cache
from django.db import transaction
from django.utils.encoding import iri_to_uri

from base64_utils import InvalidBase64, strict_b64decode
from twocomms.db_resilience import retry_mysql_read


# A cold anonymous page can be requested by several browser connections at
# once (navigation plus preload, crawler bursts, or a shared link).  Keep one
# request rendering the page while the others wait briefly for the cache entry.
# The lease is intentionally short so a killed worker cannot block the route.
_ANON_PAGE_CACHE_LOCK_TTL = 30
_ANON_PAGE_CACHE_LOCK_WAIT = 6.0
_ANON_PAGE_CACHE_LOCK_POLL = 0.025


def _build_query_string(querydict):
    if not querydict:
        return ''
    parts = []
    for key, values in sorted(querydict.lists()):
        if key == 'color':
            from ..services.color_filter import normalise_color_slugs

            values = [','.join(normalise_color_slugs(values))]
            if not values[0]:
                continue
        for value in values:
            parts.append((key, value))
    return urlencode(parts, doseq=True)


def _build_anon_cache_key(request, view_func, key_prefix=None, query_string=None):
    path = iri_to_uri(request.path)
    query = _build_query_string(request.GET) if query_string is None else query_string
    language = str(getattr(request, 'LANGUAGE_CODE', '') or '').strip().lower()
    locale_identity = language or str(
        request.META.get('HTTP_ACCEPT_LANGUAGE', '') or ''
    ).strip().lower()
    scheme = getattr(request, 'scheme', '') or 'http'
    try:
        host = request.get_host().lower()
    except Exception:
        host = str(request.META.get('HTTP_HOST') or request.META.get('SERVER_NAME') or '').lower()
    if callable(key_prefix):
        prefix = key_prefix(request, view_func)
    else:
        prefix = key_prefix or f"{view_func.__module__}.{view_func.__name__}"
    fingerprint = f"{scheme}://{host}{path}?{query}|{locale_identity}"
    digest = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
    return f"anon-page:{prefix}:{digest}"


def cache_page_for_anon(timeout, key_prefix=None, *, cache_identity=None, cache_condition=None):
    """
    Кэширует страницу только для анонимных пользователей.

    Избегаем проблем с кэшированием персональных данных для авторизованных пользователей.
    Для authenticated пользователей кэширование отключается.

    IMPORTANT: When serving a cached response, we force Django to set the
    CSRF cookie via ``get_token(request)``.  Without this, anonymous
    visitors who land on a cached page never receive a ``csrftoken``
    cookie, causing all subsequent AJAX POST requests (e.g. survey
    start) to fail with a 403 CSRF error.

    Args:
        timeout (int): Время кэширования в секундах

    Returns:
        decorator: Декоратор для view функции

    Usage:
        @cache_page_for_anon(300)  # 5 минут
        def product_list(request):
            ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.method not in ('GET', 'HEAD') or request.user.is_authenticated:
                return view_func(request, *args, **kwargs)

            if cache_condition is not None and not cache_condition(request):
                return view_func(request, *args, **kwargs)

            query_string = cache_identity(request) if cache_identity is not None else None
            cache_key = _build_anon_cache_key(
                request,
                view_func,
                key_prefix,
                query_string=query_string,
            )
            cached_response = cache.get(cache_key)
            if cached_response is not None:
                # W3-3/W3-4: раньше здесь принудительно вызывался get_token()
                # → Set-Cookie: csrftoken на КАЖДОМ cache-hit → LiteSpeed
                # page cache выключался (SEO-010, TTFB 8-18s). Теперь
                # csrftoken выдаётся лениво через /api/bootstrap/ (дергается
                # из base.html, если cookie отсутствует) — кэшированный ответ
                # остаётся чистым от Set-Cookie.
                return cached_response

            lock_digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
            lock_key = f"anon-page-render-lock:{lock_digest}"
            owns_render_lock = False
            try:
                owns_render_lock = cache.add(
                    lock_key,
                    True,
                    timeout=_ANON_PAGE_CACHE_LOCK_TTL,
                )
                if not owns_render_lock:
                    deadline = time.monotonic() + _ANON_PAGE_CACHE_LOCK_WAIT
                    while time.monotonic() < deadline:
                        time.sleep(_ANON_PAGE_CACHE_LOCK_POLL)
                        cached_response = cache.get(cache_key)
                        if cached_response is not None:
                            return cached_response
            except Exception:
                # Cache failures are already configured to fail open in
                # production. Rendering must remain available if the lock
                # backend is unavailable or read-only.
                owns_render_lock = False

            try:
                response = view_func(request, *args, **kwargs)

                if getattr(response, 'streaming', False):
                    return response
                if response.status_code != 200:
                    return response

                cache.set(cache_key, response, timeout)
                return response
            finally:
                if owns_render_lock:
                    try:
                        cache.delete(lock_key)
                    except Exception:
                        pass
        return _wrapped_view
    return decorator


def public_product_listing_cache_prefix(request, view_func):
    """
    Versioned cache prefix for anonymous pages that render public product lists.

    A dedicated version lets admin drag-and-drop immediately affect homepage and
    catalog pages without clearing unrelated cached responses.
    """
    from ..services.catalog_helpers import (
        get_public_category_version,
        get_public_product_order_version,
    )

    product_version = get_public_product_order_version()
    category_version = get_public_category_version()
    return (
        f"{view_func.__module__}.{view_func.__name__}"
        f":product-order-v{product_version}:category-v{category_version}"
        ":card-v20260814-locale-brand-cache-identity"
    )


def unique_slugify(model, base_slug):
    """
    Створює унікальний slug на основі base_slug для заданої моделі.

    Якщо slug вже існує, додає числовий суфікс (-2, -3, і т.д.) 
    до тих пір, поки не знайде унікальне значення.

    Args:
        model: Django модель (клас, не інстанс)
        base_slug (str): Базовий slug для генерації

    Returns:
        str: Унікальний slug

    Example:
        >>> unique_slugify(Product, 'my-product')
        'my-product'
        >>> unique_slugify(Product, 'my-product')  # якщо вже існує
        'my-product-2'
    """
    slug = base_slug or 'item'
    # Видаляємо зайві дефіси по краям
    slug = slug.strip('-') or 'item'

    uniq = slug
    i = 2

    # Перевіряємо унікальність, якщо вже існує - додаємо номер
    while model.objects.filter(slug=uniq).exists():
        uniq = f"{slug}-{i}"
        i += 1

    return uniq


MAX_CART_ITEM_QTY = 50
MAX_CART_ID = 2_147_483_647
MAX_CART_ITEMS = 100


def normalize_cart_session(
    raw_cart,
    *,
    max_qty=MAX_CART_ITEM_QTY,
    max_items=MAX_CART_ITEMS,
):
    """Drop malformed cart rows and coerce trusted fields before ORM use."""
    if not isinstance(raw_cart, dict):
        return {}, True

    cleaned = {}
    changed = False
    for key, raw_item in raw_cart.items():
        if len(cleaned) >= max_items:
            changed = True
            break
        if not isinstance(raw_item, dict):
            changed = True
            continue
        try:
            product_id = int(raw_item.get('product_id'))
        except (TypeError, ValueError, OverflowError):
            changed = True
            continue
        if product_id <= 0 or product_id > MAX_CART_ID:
            changed = True
            continue

        variant_value = raw_item.get('color_variant_id')
        variant_id = None
        if variant_value not in (None, '', 0, '0'):
            try:
                variant_id = int(variant_value)
            except (TypeError, ValueError, OverflowError):
                changed = True
                continue
            if variant_id <= 0 or variant_id > MAX_CART_ID:
                changed = True
                continue

        try:
            qty = int(raw_item.get('qty', 1))
        except (TypeError, ValueError, OverflowError):
            qty = 1
            changed = True
        qty = max(1, min(qty, max_qty))

        item = dict(raw_item)
        item['product_id'] = product_id
        item['color_variant_id'] = variant_id
        item['qty'] = qty
        for field in ('size', 'fit', 'fit_option_code', 'fit_option_label', 'fit_label'):
            if field in item and item[field] is not None:
                value = str(item[field]).strip()
                item[field] = value[:100]
        for field in ('option_values', 'option_labels'):
            raw_options = item.get(field)
            if raw_options is None:
                item[field] = {}
                continue
            if not isinstance(raw_options, dict) or len(raw_options) > 12:
                item[field] = {}
                changed = True
                continue
            normalized_options = {}
            for option_key, option_value in raw_options.items():
                key_text = str(option_key or '').strip()[:100]
                value_text = str(option_value or '').strip()[:100]
                if key_text and value_text:
                    normalized_options[key_text] = value_text
            if normalized_options != raw_options:
                changed = True
            item[field] = normalized_options
        normalized_key = str(key)
        if normalized_key in cleaned:
            # Preserve one bounded row when a malformed session has duplicate keys.
            changed = True
            continue
        cleaned[normalized_key] = item
        if item != raw_item:
            changed = True
    return cleaned, changed


def filter_cart_variant_ownership(cart, variants):
    """Drop rows whose variant is missing or belongs to another product."""
    cleaned = {}
    changed = False
    for key, item in cart.items():
        variant_id = item.get('color_variant_id')
        if variant_id:
            variant = variants.get(variant_id)
            if variant is None:
                changed = True
                continue
            variant_product_id = (
                variant.get('product_id')
                if isinstance(variant, dict)
                else getattr(variant, 'product_id', None)
            )
            if variant_product_id != item['product_id']:
                changed = True
                continue
        cleaned[key] = item
    return cleaned, changed


def get_cart_from_session(request):
    """
    Извлекает корзину из сессии.

    Args:
        request: Django request object

    Returns:
        dict: Словарь с данными корзины
    """
    raw_cart = request.session.get('cart', {})
    cart, changed = normalize_cart_session(raw_cart)
    if changed:
        request.session['cart'] = cart
        request.session.modified = True
    return cart


def get_validated_cart_from_session(request):
    """Return a typed cart with variant ownership verified in one bulk query."""
    cart = get_cart_from_session(request)
    variant_ids = [
        item['color_variant_id']
        for item in cart.values()
        if item.get('color_variant_id')
    ]
    if not variant_ids:
        return cart

    from productcolors.models import ProductColorVariant

    variants = retry_mysql_read(
        lambda: (
            ProductColorVariant.objects.order_by()
            .values('product_id')
            .in_bulk(variant_ids)
        )
    )
    cart, changed = filter_cart_variant_ownership(cart, variants)
    if changed:
        request.session['cart'] = cart
        request.session.modified = True
        _reset_monobank_session(request, drop_pending=True)
    return cart


def save_cart_to_session(request, cart):
    """
    Сохраняет корзину в сессию.

    Args:
        request: Django request object
        cart (dict): Данные корзины
    """
    request.session['cart'] = cart
    request.session.modified = True


def calculate_cart_total(cart):
    """
    Рассчитывает общую стоимость товаров в корзине.

    ВАЖНО: Цена ВСЕГДА берется из Product.final_price, а НЕ из сессии!
    Это обеспечивает актуальность цен и предотвращает манипуляции.

    Args:
        cart (dict): Данные корзины из сессии

    Returns:
        Decimal: Общая сумма
    """
    from decimal import Decimal
    from ..models import Product
    from productcolors.models import ProductColorVariant
    from product_catalog.services import effective_cart_unit_price

    cart, _ = normalize_cart_session(cart)
    if not cart:
        return Decimal('0')

    # Получаем все товары одним запросом
    ids = [item['product_id'] for item in cart.values()]
    variant_ids = [item.get('color_variant_id') for item in cart.values() if item.get('color_variant_id')]
    products, variants = retry_mysql_read(
        lambda: (
            Product.objects.in_bulk(ids),
            ProductColorVariant.objects.in_bulk(variant_ids),
        )
    )
    cart, _ = filter_cart_variant_ownership(cart, variants)

    total = Decimal('0')
    for item in cart.values():
        product = products.get(item['product_id'])
        if product:
            qty = item['qty']
            variant_id = item.get('color_variant_id')
            try:
                variant = variants.get(int(variant_id)) if variant_id else None
            except (TypeError, ValueError):
                variant = None
            fit_code = str(
                item.get('fit_option_code') or item.get('fit') or ''
            ).strip().lower()
            total += effective_cart_unit_price(
                product,
                variant,
                fit_code=fit_code,
                option_values=item.get('option_values') or {},
            ) * qty

    return total


def get_favorites_from_session(request):
    """
    Получает избранные товары из сессии (для анонимных пользователей).

    Args:
        request: Django request object

    Returns:
        list: Список ID избранных товаров
    """
    return request.session.get('favorites', [])


def save_favorites_to_session(request, favorites):
    """
    Сохраняет избранные товары в сессию.

    Args:
        request: Django request object
        favorites (list): Список ID товаров
    """
    request.session['favorites'] = favorites
    request.session.modified = True


# Константы
HOME_PRODUCTS_PER_PAGE = 8
PRODUCTS_PER_PAGE = 16
SEARCH_RESULTS_PER_PAGE = 20


# ==================== MONOBANK & CART HELPERS ====================

import logging
from accounts.payment import normalize_pay_type

monobank_logger = logging.getLogger('storefront.monobank')


def _normalize_order_pay_type(value):
    """
    Возвращает каноническое значение pay_type для заказа.

    Всегда приводит строку к нижнему регистру и убирает пробелы, чтобы
    поддерживать устаревшие/локализованные значения.
    """
    return normalize_pay_type(value)


def _reset_monobank_session(request, drop_pending=False):
    """
    Сбрасывает связанные с Mono checkout данные в сессии.

    Args:
        request: HTTP request
        drop_pending: Если True, отменяет pending заказ в БД
    """
    safe_to_clear_attempt_identity = True
    if drop_pending:
        pending_id = request.session.get('monobank_pending_order_id')
        if pending_id:
            try:
                from orders.models import Order
                qs = Order.objects.select_related('user').filter(
                    id=pending_id,
                    payment_provider__in=('monobank', 'monobank_checkout', 'monobank_pay')
                )
                if qs.exists():
                    qs.update(status='cancelled', payment_status='unpaid')
            except Exception:
                monobank_logger.debug(
                    'Failed to cancel pending Monobank order %s',
                    pending_id,
                    exc_info=True
                )
        attempt_id = request.session.get('monobank_pending_attempt_id') or request.session.get('monobank_attempt_id')
        if attempt_id:
            try:
                from management.services.ig_checkout_terminalization import (
                    SAFE_BROWSER_CLEAR_OUTCOMES,
                    terminalize_payment_attempt,
                )
                from orders.models import PaymentAttempt

                outcome = terminalize_payment_attempt(
                    attempt_id,
                    terminal_status=PaymentAttempt.Status.CANCELLED,
                    reason='checkout_cancelled',
                    source='checkout_session_reset',
                )
                safe_to_clear_attempt_identity = (
                    outcome.outcome in SAFE_BROWSER_CLEAR_OUTCOMES
                )
            except Exception:
                safe_to_clear_attempt_identity = False
                monobank_logger.debug(
                    'Failed to cancel payment attempt %s', attempt_id, exc_info=True
                )

    attempt_identity_keys = {
        'monobank_invoice_id',
        'monobank_pending_attempt_id',
        'monobank_attempt_id',
    }
    for key in (
        'monobank_pending_order_id',
        'monobank_invoice_id',
        'monobank_order_id',
        'monobank_order_ref',
        'monobank_pending_attempt_id',
        'monobank_attempt_id',
    ):
        if not safe_to_clear_attempt_identity and key in attempt_identity_keys:
            continue
        if key in request.session:
            request.session.pop(key, None)

    request.session.modified = True


def _normalize_color_variant_id(raw):
    """
    Приводит значение идентификатора цветового варианта к int либо None.
    Отсекает плейсхолдеры вида 'default', 'null', 'None', 'false', 'undefined'.
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    try:
        value = str(raw).strip()
    except Exception:
        return None
    if not value:
        return None
    lowered = value.lower()
    if lowered in {'default', 'none', 'null', 'false', 'undefined'}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_color_variant_safe(color_variant_id):
    """
    Возвращает экземпляр ProductColorVariant либо None, не выбрасывая ошибок.
    """
    normalized_id = _normalize_color_variant_id(color_variant_id)
    if not normalized_id:
        return None
    try:
        from productcolors.models import ProductColorVariant
        return ProductColorVariant.objects.get(id=normalized_id)
    except (ProductColorVariant.DoesNotExist, ValueError, TypeError):
        return None
    except ImportError:
        return None


def _hex_to_name(hex_value: str):
    """Конвертирует hex цвета в украинское название."""
    if not hex_value:
        return None
    h = hex_value.strip().lstrip('#').upper()
    mapping = {
        '000000': 'чорний',
        'FFFFFF': 'білий',
        'FAFAFA': 'білий',
        'F5F5F5': 'білий',
        'FF0000': 'червоний',
        'C1382F': 'бордовий',
        'FFA500': 'помаранчевий',
        'FFFF00': 'жовтий',
        '00FF00': 'зелений',
        '0000FF': 'синій',
        '808080': 'сірий',
        'A52A2A': 'коричневий',
        '800080': 'фіолетовий',
    }
    return mapping.get(h)


def _translate_color_to_ukrainian(color_name):
    """Переводит название цвета на украинский."""
    if not color_name:
        return color_name
    # Простой маппинг, можно расширить
    translations = {
        'black': 'чорний',
        'white': 'білий',
        'red': 'червоний',
        'blue': 'синій',
        'green': 'зелений',
        'yellow': 'жовтий',
        'orange': 'помаранчевий',
        'purple': 'фіолетовий',
        'pink': 'рожевий',
        'gray': 'сірий',
        'grey': 'сірий',
        'brown': 'коричневий',
    }
    lower_name = color_name.lower()
    return translations.get(lower_name, color_name)


def _color_label_from_variant(color_variant):
    """
    Возвращает текстовую метку цвета из варианта.
    """
    if not color_variant:
        return None
    color = getattr(color_variant, 'color', None)
    if not color:
        return None
    name = (getattr(color, 'name', '') or '').strip()
    if name:
        return _translate_color_to_ukrainian(name)
    primary = (getattr(color, 'primary_hex', '') or '').strip()
    secondary = (getattr(color, 'secondary_hex', '') or '').strip()
    if secondary:
        label = _translate_color_to_ukrainian(
            '/'.join(filter(None, [_hex_to_name(primary), _hex_to_name(secondary)]))
        )
        if label:
            return label
        return f'{primary}+{secondary}'
    if primary:
        label = _hex_to_name(primary)
        if label:
            return label
        return primary
    return None


# ==================== MONOBANK HELPER FUNCTIONS ====================

# Константы статусов Monobank
MONOBANK_SUCCESS_STATUSES = {'success', 'hold'}
MONOBANK_PENDING_STATUSES = {'processing'}
MONOBANK_FAILURE_STATUSES = {
    'failure', 'expired', 'rejected', 'canceled', 'cancelled', 'reversed'
}


def _record_monobank_status(order, payload, source='api'):
    """
    Записывает статус платежа Monobank в заказ с блокировкой записи.

    Args:
        order: Объект заказа
        payload: Данные от Monobank API
        source: Источник данных ('api' или 'webhook')
    """
    if not payload or not order or not getattr(order, 'pk', None):
        return

    from orders.models import Order

    try:
        with transaction.atomic():
            locked_order = (
                Order.objects.select_for_update()
                .select_related('user')
                .get(pk=order.pk)
            )
            result = _record_monobank_status_locked(locked_order, payload, source)
    except Order.DoesNotExist:
        monobank_logger.error(
            'Failed to record Monobank status: order %s not found',
            getattr(order, 'pk', None),
        )
        return

    try:
        order.refresh_from_db()
    except Exception:
        # В большинстве случаев order передается только для идентификатора
        pass

    return result


def _record_monobank_status_locked(order, payload, source='api'):
    """Реализация логики записи статуса под транзакционной блокировкой."""
    from django.utils import timezone

    if not payload:
        return

    status = payload.get('status')
    payment_payload = order.payment_payload or {}
    history = payment_payload.get('history', [])
    history.append({
        'status': status,
        'data': payload,
        'source': source,
        'received_at': timezone.now().isoformat()
    })
    payment_payload['history'] = history[-20:]
    payment_payload['last_status'] = status
    payment_payload['last_update_source'] = source
    payment_payload['last_update_at'] = timezone.now().isoformat()
    order.payment_payload = payment_payload

    update_fields = ['payment_payload']

    def _save_status_fields(fields, reason):
        try:
            order.save(update_fields=fields)
        except Exception:
            monobank_logger.exception(
                'Order %s: failed to save Monobank status fields %s (%s)',
                getattr(order, 'order_number', order.pk),
                fields,
                reason,
            )
            raise

    if status in MONOBANK_SUCCESS_STATUSES:
        previous_status = order.payment_status
        normalized_previous = 'prepaid' if previous_status == 'partial' else previous_status

        raw_pay_type = getattr(order, 'pay_type', None)
        pay_type = _normalize_order_pay_type(raw_pay_type)
        target_status = 'prepaid' if pay_type == 'prepay_200' else 'paid'

        if normalized_previous == target_status:
            monobank_logger.info(
                f'⚠️ Order {order.order_number}: webhook повторно получен, '
                f'статус уже {target_status} (pay_type_raw={raw_pay_type}, normalized={pay_type}). '
                f'Пропускаем обновление.'
            )
            _save_status_fields(['payment_payload'], 'duplicate_success_payload')
            from storefront.utm_tracking import ensure_order_purchase_action
            ensure_order_purchase_action(
                order,
                metadata={'source': source, 'monobank_status': status},
            )
            return

        if normalized_previous == 'paid' and target_status == 'prepaid':
            monobank_logger.warning(
                f'⚠️ Order {order.order_number}: pay_type={pay_type} требует статус prepaid, '
                f'но в заказе было {previous_status}. Исправляем.'
            )
        elif normalized_previous == 'prepaid' and target_status == 'paid':
            monobank_logger.warning(
                f'⚠️ Order {order.order_number}: pay_type={pay_type} требует статус paid, '
                f'но в заказе было {previous_status}. Исправляем.'
            )

        order.payment_status = target_status
        update_fields.append('payment_status')

        if target_status == 'prepaid':
            monobank_logger.info(
                f'✅ Order {order.order_number}: prepayment successful → payment_status=prepaid '
                f'(pay_type_raw={raw_pay_type}, normalized={pay_type}, previous_status={previous_status})'
            )
        else:
            monobank_logger.info(
                f'✅ Order {order.order_number}: full payment successful → payment_status=paid '
                f'(pay_type_raw={raw_pay_type}, normalized={pay_type}, previous_status={previous_status})'
            )

        _save_status_fields(update_fields, 'success_transition')

        from storefront.utm_tracking import ensure_order_purchase_action
        ensure_order_purchase_action(
            order,
            metadata={'source': source, 'monobank_status': status},
        )

        # Persist the external delivery intent under the same order lock. The
        # existing cron consumer performs provider I/O after this transaction.
        if previous_status != order.payment_status:
            from orders.payment_side_effects import enqueue_order_post_payment_side_effect

            enqueue_order_post_payment_side_effect(
                order.pk,
                previous_status=normalized_previous or 'unpaid',
                pay_type=pay_type,
            )

        return

    if status in MONOBANK_PENDING_STATUSES:
        order.payment_status = 'checking'
        update_fields.append('payment_status')
    elif status in MONOBANK_FAILURE_STATUSES:
        order.payment_status = 'unpaid'
        update_fields.append('payment_status')

    _save_status_fields(update_fields, 'non_success_transition')


def _update_order_telegram_notification_state(order_pk, **changes):
    """Merge Telegram delivery state without clobbering other payment markers."""
    from orders.models import Order

    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order_pk)
        payload = dict(current.payment_payload) if isinstance(current.payment_payload, dict) else {}
        notifications = dict(payload.get('telegram_notifications') or {})
        notifications.update(changes)
        payload['telegram_notifications'] = notifications
        current.payment_payload = payload
        current.save(update_fields=['payment_payload'])
    return current


_POST_PAYMENT_CHANNEL_NAMES = (
    "telegram",
    "meta_purchase",
    "tiktok_purchase",
    "receipt_email",
    "instagram_lifecycle",
)


def _initialize_post_payment_channels(order_pk):
    """Create missing channel rows while preserving existing delivery facts."""
    from django.utils import timezone
    from orders.models import Order

    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order_pk)
        payload = dict(current.payment_payload) if isinstance(current.payment_payload, dict) else {}
        channels = dict(payload.get("post_payment_channels") or {})
        changed = False
        now = timezone.now().isoformat()
        for channel in _POST_PAYMENT_CHANNEL_NAMES:
            if channel not in channels:
                channels[channel] = {"state": "pending", "updated_at": now}
                changed = True
        if changed:
            payload["post_payment_channels"] = channels
            current.payment_payload = payload
            current.save(update_fields=["payment_payload"])
    return current


def _sync_instagram_lifecycle_channel(order_pk):
    """Project the durable IG event state without making it its trigger."""
    try:
        from management.ig_bot_models import IgLifecycleEvent
        from management.services.ig_lifecycle import LIFECYCLE_PROJECTION_STAGE

        event = (
            IgLifecycleEvent.objects.filter(
                order_id=order_pk,
            )
            .order_by("-id")
            .first()
        )
        if event is None:
            _record_post_payment_channel(
                order_pk,
                "instagram_lifecycle",
                state="skipped",
                error="no_instagram_lifecycle_event",
            )
            return
        state_map = {
            IgLifecycleEvent.State.SENT: "sent",
            IgLifecycleEvent.State.AMBIGUOUS: "ambiguous",
            IgLifecycleEvent.State.FAILED: "failed",
            IgLifecycleEvent.State.CANCELLED: "disabled",
            IgLifecycleEvent.State.MANAGER_REVIEW: "pending",
            IgLifecycleEvent.State.WAITING_WINDOW: "pending",
        }
        state = state_map.get(event.state, "pending")
        _record_post_payment_channel(
            order_pk,
            "instagram_lifecycle",
            state=state,
            error=event.last_error,
            metadata={
                "provider_message_id": event.provider_message_id,
                "lifecycle_event_id": event.pk,
                "kind": event.kind,
                "event_key": event.event_key,
                "lifecycle_stage": LIFECYCLE_PROJECTION_STAGE.get(event.kind, 0),
                "lifecycle_event_updated_at": event.updated_at.isoformat(),
            },
            monotonic_metadata_key="lifecycle_event_id",
            monotonic_stage_key="lifecycle_stage",
            monotonic_revision_key="lifecycle_event_updated_at",
        )
    except Exception:
        monobank_logger.exception(
            "Failed to project Instagram lifecycle channel for order %s", order_pk
        )


def _normalize_telegram_delivery_outcome(value):
    """Accept the report API while keeping old bool-returning test doubles valid."""
    outcome = getattr(value, "outcome", None)
    if outcome in {"sent", "failed", "ambiguous"}:
        return outcome
    if value is True:
        return "sent"
    if value is False or value is None:
        return "failed"
    if str(value) in {"sent", "failed", "ambiguous"}:
        return str(value)
    return "failed"


def _claim_order_telegram_delivery(order_pk):
    """Claim one missing paid-order delivery with an ambiguity-safe lease."""
    from datetime import timedelta

    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    from orders.models import Order

    now = timezone.now()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_pk)
        payload = dict(order.payment_payload) if isinstance(order.payment_payload, dict) else {}
        notifications = dict(payload.get('telegram_notifications') or {})
        order_sent = bool(notifications.get('order_notification_sent'))
        order_ambiguous = bool(notifications.get('order_notification_ambiguous'))
        payment_ambiguous = bool(notifications.get('payment_status_update_ambiguous'))
        payment_marker_present = 'payment_status_update_sent' in notifications
        payment_sent = bool(notifications.get('payment_status_update_sent'))
        if (order_sent or order_ambiguous) and (
            payment_sent or payment_ambiguous or not payment_marker_present
        ):
            if order_ambiguous or payment_ambiguous:
                return None, 'ambiguous'
            return None, 'already_sent'
        notifications.setdefault('payment_status_update_sent', False)

        lease_until = parse_datetime(str(notifications.get('delivery_retry_lease_until') or ''))
        if lease_until is not None and timezone.is_naive(lease_until):
            lease_until = timezone.make_aware(lease_until, timezone.get_current_timezone())
        if lease_until and lease_until > now:
            return None, 'leased'

        payment_phase_expired = bool(
            notifications.get('payment_status_update_send_started_at')
            and not notifications.get('payment_status_update_sent')
            and not notifications.get('payment_status_update_ambiguous')
        )
        order_phase_expired = bool(
            notifications.get('order_notification_send_started_at')
            and not notifications.get('order_notification_sent')
            and not notifications.get('order_notification_ambiguous')
        )
        if payment_phase_expired:
            notifications['payment_status_update_ambiguous'] = True
            notifications['payment_status_update_ambiguous_at'] = now.isoformat()
            notifications['delivery_last_failed_at'] = now.isoformat()
            notifications['delivery_last_error'] = 'telegram_payment_status_update_ambiguous'
            payment_ambiguous = True
        if order_phase_expired:
            notifications['order_notification_ambiguous'] = True
            notifications['order_notification_ambiguous_at'] = now.isoformat()
            notifications['order_notification_pending'] = False
            notifications['delivery_retry_lease_until'] = None
            notifications['delivery_last_failed_at'] = now.isoformat()
            notifications['delivery_last_error'] = 'telegram_order_notification_ambiguous'
            order_ambiguous = True

        if (order_sent or order_ambiguous) and (payment_sent or payment_ambiguous):
            payload['telegram_notifications'] = notifications
            order.payment_payload = payload
            order.save(update_fields=['payment_payload'])
            return None, 'ambiguous' if order_ambiguous or payment_ambiguous else 'already_sent'

        notifications['order_notification_pending'] = not (order_sent or order_ambiguous)
        notifications.setdefault('order_notification_pending_at', now.isoformat())
        notifications['delivery_attempt_count'] = int(
            notifications.get('delivery_attempt_count') or 0
        ) + 1
        notifications['delivery_last_attempt_at'] = now.isoformat()
        notifications['delivery_retry_lease_until'] = (now + timedelta(minutes=5)).isoformat()
        payload['telegram_notifications'] = notifications
        order.payment_payload = payload
        order.save(update_fields=['payment_payload'])
    return order, 'claimed'


def _mark_telegram_delivery_ambiguous(order_pk, phase):
    from django.utils import timezone

    now = timezone.now().isoformat()
    if phase == 'payment_status_update':
        changes = {
            'payment_status_update_ambiguous': True,
            'payment_status_update_ambiguous_at': now,
            'delivery_last_failed_at': now,
            'delivery_last_error': 'telegram_payment_status_update_ambiguous',
        }
    else:
        changes = {
            'order_notification_ambiguous': True,
            'order_notification_ambiguous_at': now,
            'order_notification_pending': False,
            'delivery_retry_lease_until': None,
            'delivery_last_failed_at': now,
            'delivery_last_error': 'telegram_order_notification_ambiguous',
        }
    return _update_order_telegram_notification_state(order_pk, **changes)


def deliver_pending_order_telegram_notifications(
    order_pk,
    *,
    previous_status='unpaid',
    pay_type=None,
):
    """Deliver the paid status and full order card with durable retry state."""
    from django.utils import timezone

    try:
        order, claim_status = _claim_order_telegram_delivery(order_pk)
    except Exception:
        monobank_logger.exception('Failed to claim Telegram delivery for order %s', order_pk)
        return 'failed'
    if claim_status != 'claimed':
        return claim_status

    active_phase = None
    try:
        from orders.telegram_notifications import TelegramNotifier

        notifier = TelegramNotifier()
        payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
        notifications = payload.get('telegram_notifications') or {}
        if notifications.get('payment_status_update_sent'):
            payment_outcome = 'sent'
        elif notifications.get('payment_status_update_ambiguous'):
            payment_outcome = 'ambiguous'
        else:
            payment_outcome = None

        if payment_outcome is None:
            order = _update_order_telegram_notification_state(
                order.pk,
                payment_status_update_send_started_at=timezone.now().isoformat(),
            )
            active_phase = 'payment_status_update'
            try:
                payment_result = notifier.send_admin_payment_status_update(
                    order,
                    old_status=previous_status or 'unpaid',
                    new_status=order.payment_status,
                    pay_type=pay_type or order.pay_type,
                    return_outcome=True,
                )
            except Exception:
                monobank_logger.exception(
                    'Ambiguous Telegram payment alert for paid order %s',
                    order.order_number,
                )
                order = _mark_telegram_delivery_ambiguous(order.pk, active_phase)
                payment_outcome = 'ambiguous'
                active_phase = None
            else:
                payment_outcome = _normalize_telegram_delivery_outcome(payment_result)

            if payment_outcome == 'ambiguous' and active_phase:
                order = _mark_telegram_delivery_ambiguous(order.pk, active_phase)
                active_phase = None
            elif payment_outcome == 'sent':
                active_phase = None
                order = _update_order_telegram_notification_state(
                    order.pk,
                    payment_status_update_sent=True,
                    payment_status_update_sent_at=timezone.now().isoformat(),
                    payment_status_update_send_started_at=None,
                )
            elif payment_outcome == 'failed':
                active_phase = None
                order = _update_order_telegram_notification_state(
                    order.pk,
                    payment_status_update_send_started_at=None,
                    payment_status_update_last_failed_at=timezone.now().isoformat(),
                )

        payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
        notifications = payload.get('telegram_notifications') or {}
        if notifications.get('order_notification_sent'):
            order_outcome = 'sent'
        elif notifications.get('order_notification_ambiguous'):
            order_outcome = 'ambiguous'
        else:
            order_outcome = None
        if order_outcome is None:
            order = _update_order_telegram_notification_state(
                order.pk,
                order_notification_send_started_at=timezone.now().isoformat(),
            )
            active_phase = 'order_notification'
            try:
                order_result = notifier.send_new_order_notification(
                    order,
                    return_outcome=True,
                    delivery_claimed=True,
                )
            except Exception:
                monobank_logger.exception(
                    'Ambiguous Telegram order card for paid order %s',
                    order.order_number,
                )
                _mark_telegram_delivery_ambiguous(order.pk, active_phase)
                return 'ambiguous'
            order_outcome = _normalize_telegram_delivery_outcome(order_result)
            if order_outcome == 'ambiguous':
                _mark_telegram_delivery_ambiguous(order.pk, active_phase)
                return 'ambiguous'
            active_phase = None
            if order_outcome == 'sent':
                order = _update_order_telegram_notification_state(
                    order.pk,
                    order_notification_sent=True,
                    order_notification_sent_at=timezone.now().isoformat(),
                    order_notification_status=order.payment_status,
                    order_notification_send_started_at=None,
                )
            else:
                order = _update_order_telegram_notification_state(
                    order.pk,
                    order_notification_send_started_at=None,
                    order_notification_last_failed_at=timezone.now().isoformat(),
                )

        if order_outcome in {'sent', 'ambiguous'} and payment_outcome in {
            'sent',
            'ambiguous',
        }:
            _update_order_telegram_notification_state(
                order.pk,
                order_notification_pending=False,
                delivery_retry_lease_until=None,
                delivery_last_error=None,
            )
            if 'ambiguous' in {order_outcome, payment_outcome}:
                return 'ambiguous'
            return 'sent'

        _update_order_telegram_notification_state(
            order.pk,
            order_notification_pending=True,
            delivery_retry_lease_until=None,
            delivery_last_failed_at=timezone.now().isoformat(),
            delivery_last_error='telegram_delivery_failed',
        )
        return 'failed'
    except Exception:
        monobank_logger.exception('Failed Telegram delivery for paid order %s', order.order_number)
        try:
            if active_phase:
                _mark_telegram_delivery_ambiguous(order.pk, active_phase)
                return 'ambiguous'
            _update_order_telegram_notification_state(
                order.pk,
                order_notification_pending=True,
                delivery_retry_lease_until=None,
                delivery_last_failed_at=timezone.now().isoformat(),
                delivery_last_error='telegram_delivery_exception',
            )
        except Exception:
            monobank_logger.exception(
                'Failed to persist Telegram delivery failure for order %s', order.pk
            )
        return 'failed'


def _dispatch_post_payment_events(order_pk, previous_status, pay_type):
    """Compatibility adapter that now persists intent instead of spawning."""
    from orders.payment_side_effects import enqueue_order_post_payment_side_effect

    return enqueue_order_post_payment_side_effect(
        order_pk,
        previous_status=previous_status,
        pay_type=pay_type,
    )


def _record_post_payment_channel(
    order_pk,
    channel,
    state,
    *,
    error="",
    metadata=None,
    monotonic_metadata_key="",
    monotonic_stage_key="",
    monotonic_revision_key="",
):
    """Merge one post-payment channel state without touching sibling markers."""
    from django.utils import timezone
    from orders.models import Order

    try:
        with transaction.atomic():
            current = Order.objects.select_for_update().get(pk=order_pk)
            payload = current.payment_payload if isinstance(current.payment_payload, dict) else {}
            channels = payload.get("post_payment_channels")
            channels = dict(channels) if isinstance(channels, dict) else {}
            entry = channels.get(channel)
            entry = dict(entry) if isinstance(entry, dict) else {}
            if monotonic_metadata_key and isinstance(metadata, dict):
                try:
                    incoming_marker = int(metadata.get(monotonic_metadata_key) or 0)
                except (TypeError, ValueError):
                    incoming_marker = 0
                try:
                    current_marker = int(entry.get(monotonic_metadata_key) or 0)
                except (TypeError, ValueError):
                    current_marker = 0
                if monotonic_stage_key:
                    try:
                        incoming_stage = int(
                            metadata.get(monotonic_stage_key) or 0
                        )
                    except (TypeError, ValueError):
                        incoming_stage = 0
                    try:
                        current_stage = int(
                            entry.get(monotonic_stage_key) or 0
                        )
                    except (TypeError, ValueError):
                        current_stage = 0
                else:
                    incoming_stage = current_stage = 0
                incoming_revision = current_revision = None
                if monotonic_revision_key:
                    from django.utils.dateparse import parse_datetime

                    incoming_revision = parse_datetime(
                        str(metadata.get(monotonic_revision_key) or "")
                    )
                    current_revision = parse_datetime(
                        str(entry.get(monotonic_revision_key) or "")
                    )
                    if incoming_revision is not None and timezone.is_naive(
                        incoming_revision
                    ):
                        incoming_revision = timezone.make_aware(
                            incoming_revision,
                            timezone.get_default_timezone(),
                        )
                    if current_revision is not None and timezone.is_naive(
                        current_revision
                    ):
                        current_revision = timezone.make_aware(
                            current_revision,
                            timezone.get_default_timezone(),
                        )
                if (
                    incoming_stage < current_stage
                    or (
                        incoming_stage == current_stage
                        and incoming_marker < current_marker
                    )
                    or (
                        incoming_stage == current_stage
                        and incoming_marker == current_marker
                        and current_revision is not None
                        and (
                            incoming_revision is None
                            or incoming_revision < current_revision
                        )
                    )
                ):
                    return False
            entry.update({
                "state": str(state or "unknown")[:32],
                "updated_at": timezone.now().isoformat(),
            })
            if error:
                entry["error"] = str(error)[:500]
            else:
                entry.pop("error", None)
            if isinstance(metadata, dict):
                entry.update({str(key)[:64]: value for key, value in metadata.items()})
            channels[channel] = entry
            payload["post_payment_channels"] = channels
            current.payment_payload = payload
            current.save(update_fields=["payment_payload"])
        return True
    except Exception:
        monobank_logger.exception(
            "Failed to persist post-payment channel state order=%s channel=%s",
            order_pk,
            channel,
        )
        return False


def _send_post_payment_events(order_pk, previous_status, pay_type, *, only_channel=None):
    """
    W2-7: отправка внешних событий (Telegram, Meta CAPI, TikTok) ПОСЛЕ
    коммита транзакции — row-lock на заказ уже снят. Дедуп-флаги
    (purchase_sent/lead_sent/order_notification_sent) сохраняются в
    payment_payload, как и раньше.
    """
    from django.utils import timezone

    if only_channel is not None:
        if only_channel not in {
            "telegram",
            "meta_purchase",
            "tiktok_purchase",
            "receipt_email",
        }:
            raise ValueError(f"unsupported post-payment channel: {only_channel}")
        return _send_post_payment_channel_events(
            order_pk,
            previous_status,
            pay_type,
            only_channel,
        )

    from orders.models import Order

    try:
        order = Order.objects.select_related('user').get(pk=order_pk)
    except Order.DoesNotExist:
        monobank_logger.error('Post-payment events: order %s not found', order_pk)
        return

    _initialize_post_payment_channels(order.pk)

    # Heal the internal purchase ledger before provider delivery; this DB-only
    # operation is idempotent across retries by the bounded outbox consumer.
    try:
        from storefront.utm_tracking import ensure_order_purchase_action

        ensure_order_purchase_action(
            order,
            metadata={
                'source': 'post_payment_dispatch',
                'payment_status': order.payment_status,
            },
        )
    except Exception:
        monobank_logger.exception(
            'Failed to persist Purchase action for order %s', order.order_number
        )

    # 1. Telegram is attempted by the bounded consumer. Its own send-phase
    # markers prevent blind replay when the consumer stops during provider I/O.
    telegram_result = deliver_pending_order_telegram_notifications(
        order.pk,
        previous_status=previous_status,
        pay_type=pay_type,
    )
    _record_post_payment_channel(
        order.pk,
        "telegram",
        {
            "sent": "sent",
            "failed": "failed",
            "leased": "pending",
            "already_sent": "sent",
            "ambiguous": "ambiguous",
        }.get(telegram_result, "unknown"),
        error="telegram_delivery_failed" if telegram_result == "failed" else "",
    )
    if telegram_result == 'sent':
        monobank_logger.info(
            'Telegram notification sent for order %s (status: %s -> %s)',
            order.order_number,
            previous_status,
            order.payment_status,
        )
    elif telegram_result == 'failed':
        monobank_logger.warning(
            'Telegram notification remains pending for order %s',
            order.order_number,
        )

    # The delivery helper writes payment_payload independently. Reload it so
    # later Meta/TikTok/email saves cannot overwrite Telegram retry markers.
    order.refresh_from_db()

    # 2. Facebook событие
    try:
        from orders.facebook_conversions_service import get_facebook_conversions_service
        fb_service = get_facebook_conversions_service()
        payment_payload = order.payment_payload or {}
        facebook_events = payment_payload.get('facebook_events', {})

        if fb_service.enabled:
            # Any verified money movement is a Purchase, including the 200
            # UAH prepayment. Do not emit a second Lead for the same payment.
            # COD remains only as a historical compatibility path; current
            # checkout rejects it and it is excluded from active KPI planning.
            if order.payment_status in ('paid', 'prepaid', 'partial'):
                event_key = 'purchase_sent'
                send_event = fb_service.send_purchase_event
                event_label = 'Purchase'
            else:
                event_key = None
                send_event = None
                event_label = None

            if event_key and not facebook_events.get(event_key, False):
                # Stamp the actual verified payment transition; the service
                # uses this for Meta event_time instead of order.created.
                facebook_events.setdefault(
                    'purchase_event_time',
                    int(timezone.now().timestamp()),
                )
                payment_payload['facebook_events'] = facebook_events
                order.payment_payload = payment_payload
                order.save(update_fields=['payment_payload'])
                event_success = send_event(order)
                if event_success:
                    facebook_events[event_key] = True
                    facebook_events[f'{event_key}_at'] = timezone.now().isoformat()
                    order.payment_payload = payment_payload
                    order.save(update_fields=['payment_payload'])
                    monobank_logger.info(
                        f'✅ Facebook {event_label} event sent for order {order.order_number} '
                        f'(payment_status={order.payment_status})'
                    )
                    _record_post_payment_channel(
                        order.pk,
                        "meta_purchase",
                        "sent",
                        metadata={
                            "event_id": (
                                ((order.payment_payload or {}).get("fb_conversions_api") or {}).get("event_id")
                                or facebook_events.get("purchase_event_id", "")
                                or order.get_purchase_event_id()
                            )
                        },
                    )
                else:
                    monobank_logger.warning(
                        f'⚠️ Failed to send Facebook {event_label} event for order {order.order_number}'
                    )
                    _record_post_payment_channel(order.pk, "meta_purchase", "failed", error="provider_rejected")
            elif event_key:
                # A concurrent retry may have persisted the CAPI envelope
                # after this dispatcher loaded its snapshot. Re-read before
                # recording the ledger so the operational event ID is never
                # replaced by an empty fallback.
                order.refresh_from_db()
                payment_payload = order.payment_payload or {}
                facebook_events = payment_payload.get('facebook_events', {})
                _record_post_payment_channel(
                    order.pk,
                    "meta_purchase",
                    "sent",
                    metadata={
                        "already_sent": True,
                        "event_id": (
                            ((payment_payload.get("fb_conversions_api") or {}).get("event_id"))
                            or facebook_events.get("purchase_event_id", "")
                            or order.get_purchase_event_id()
                        ),
                    },
                )
        else:
            _record_post_payment_channel(order.pk, "meta_purchase", "disabled", error="capi_disabled")
            monobank_logger.warning(f'⚠️ Facebook Conversions API not enabled, skipping event')
    except Exception as e:
        monobank_logger.exception(f'Failed to send Facebook event for order {order.order_number}: {e}')
        _record_post_payment_channel(order.pk, "meta_purchase", "unknown", error=repr(e))

    # Meta and the channel ledger both persist into the shared JSON envelope.
    # Reload before TikTok so its legacy marker save cannot restore a stale
    # snapshot and erase the preceding channel state on MariaDB.
    order.refresh_from_db()

    # 3. TikTok Events API
    try:
        from orders.tiktok_events_service import get_tiktok_events_service
        tiktok_service = get_tiktok_events_service()

        if tiktok_service.enabled:
            if order.payment_status in ('paid', 'prepaid', 'partial'):
                # Full payment and prepayment are both completed Purchase
                # conversions. Lead is intentionally not emitted here.
                payment_payload = order.payment_payload or {}
                tiktok_events = payment_payload.get('tiktok_events', {})

                # W2-3: pre-check purchase_sent — раньше Purchase мог уйти повторно
                if tiktok_events.get('purchase_sent', False):
                    monobank_logger.info(f'📈 TikTok Purchase event already sent for order {order.order_number}, skipping')
                    _record_post_payment_channel(order.pk, "tiktok_purchase", "sent", metadata={"already_sent": True})
                else:
                    purchase_success = tiktok_service.send_purchase_event(order)
                    if purchase_success:
                        if 'tiktok_events' not in payment_payload:
                            payment_payload['tiktok_events'] = {}
                        payment_payload['tiktok_events']['purchase_sent'] = True
                        payment_payload['tiktok_events']['purchase_sent_at'] = timezone.now().isoformat()
                        order.payment_payload = payment_payload
                        order.save(update_fields=['payment_payload'])
                        monobank_logger.info(f'✅ TikTok Purchase event sent for order {order.order_number} (payment confirmed)')
                        _record_post_payment_channel(order.pk, "tiktok_purchase", "sent")
                    else:
                        monobank_logger.warning(f'⚠️ Failed to send TikTok Purchase event for order {order.order_number}')
                        _record_post_payment_channel(order.pk, "tiktok_purchase", "failed", error="provider_rejected")
        else:
            _record_post_payment_channel(order.pk, "tiktok_purchase", "disabled", error="tiktok_disabled")
            monobank_logger.warning('⚠️ TikTok Events API not enabled, skipping events')
    except ImportError:
        monobank_logger.debug('TikTok Events service module not found, skipping')
        _record_post_payment_channel(order.pk, "tiktok_purchase", "disabled", error="service_unavailable")
    except Exception as e:
        monobank_logger.exception(f'Failed to send TikTok event for order {order.order_number}: {e}')
        _record_post_payment_channel(order.pk, "tiktok_purchase", "unknown", error=repr(e))

    # 4. Customer receipt email. The sender itself persists an idempotency flag
    # in payment_payload, so webhook + return races cannot duplicate the email.
    try:
        if getattr(order, 'email', None):
            from orders.email_receipt import send_order_receipt_email
            receipt_result = send_order_receipt_email(order)
            if isinstance(receipt_result, tuple) and len(receipt_result) >= 2:
                sent, error = receipt_result[0], receipt_result[1]
            else:
                # The canonical sender returns ``(ok, error)``. Treat any
                # legacy/mocked shape as unknown rather than claiming sent.
                sent, error = False, "delivery_unknown"
            _record_post_payment_channel(
                order.pk,
                "receipt_email",
                "sent" if sent else ("unknown" if error == "delivery_unknown" else "failed"),
                error=error or "",
            )
        else:
            _record_post_payment_channel(order.pk, "receipt_email", "skipped", error="no_valid_email")
    except Exception:
        monobank_logger.exception(
            'Failed to send receipt email for order %s', order.pk
        )
        _record_post_payment_channel(order.pk, "receipt_email", "unknown", error="sender_exception")

    _sync_instagram_lifecycle_channel(order.pk)
    return telegram_result


def _send_post_payment_channel_events(order_pk, previous_status, pay_type, channel):
    """Deliver exactly one outbox-owned channel and persist its ledger state."""
    from django.utils import timezone
    from orders.models import Order

    try:
        order = Order.objects.select_related("user").get(pk=order_pk)
    except Order.DoesNotExist:
        monobank_logger.error("Post-payment channel: order %s not found", order_pk)
        return "failed"

    _initialize_post_payment_channels(order.pk)
    try:
        from storefront.utm_tracking import ensure_order_purchase_action

        ensure_order_purchase_action(
            order,
            metadata={
                "source": "post_payment_dispatch",
                "payment_status": order.payment_status,
            },
        )
    except Exception:
        monobank_logger.exception(
            "Failed to persist Purchase action for order %s",
            order.order_number,
        )
    order.refresh_from_db()

    if channel == "telegram":
        outcome = deliver_pending_order_telegram_notifications(
            order.pk,
            previous_status=previous_status,
            pay_type=pay_type,
        )
        _record_post_payment_channel(
            order.pk,
            "telegram",
            {
                "sent": "sent",
                "failed": "failed",
                "leased": "pending",
                "already_sent": "sent",
                "ambiguous": "ambiguous",
            }.get(outcome, "unknown"),
            error="telegram_delivery_failed" if outcome == "failed" else "",
        )
        return outcome

    if channel == "meta_purchase":
        try:
            from orders.provider_delivery import ProviderDeliveryAmbiguous
            from orders.facebook_conversions_service import (
                get_facebook_conversions_service,
            )

            service = get_facebook_conversions_service()
            payment_payload = order.payment_payload or {}
            facebook_events = payment_payload.get("facebook_events", {})
            if not service.enabled:
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "disabled",
                    error="capi_disabled",
                )
                return "disabled"
            if order.payment_status not in ("paid", "prepaid", "partial"):
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "skipped",
                    error="payment_not_verified",
                )
                return "skipped"
            if facebook_events.get("purchase_sent", False):
                order.refresh_from_db()
                payment_payload = order.payment_payload or {}
                facebook_events = payment_payload.get("facebook_events", {})
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "sent",
                    metadata={
                        "already_sent": True,
                        "event_id": (
                            ((payment_payload.get("fb_conversions_api") or {}).get("event_id"))
                            or facebook_events.get("purchase_event_id", "")
                            or order.get_purchase_event_id()
                        ),
                    },
                )
                return "sent"

            facebook_events.setdefault(
                "purchase_event_time",
                int(timezone.now().timestamp()),
            )
            payment_payload["facebook_events"] = facebook_events
            order.payment_payload = payment_payload
            order.save(update_fields=["payment_payload"])
            sent = service.send_purchase_event(order)
            if not sent:
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "failed",
                    error="provider_rejected",
                )
                return "failed"
            facebook_events["purchase_sent"] = True
            facebook_events["purchase_sent_at"] = timezone.now().isoformat()
            order.payment_payload = payment_payload
            order.save(update_fields=["payment_payload"])
            _record_post_payment_channel(
                order.pk,
                channel,
                "sent",
                metadata={
                    "event_id": (
                        ((order.payment_payload or {}).get("fb_conversions_api") or {}).get("event_id")
                        or facebook_events.get("purchase_event_id", "")
                        or order.get_purchase_event_id()
                    )
                },
            )
            return "sent"
        except ProviderDeliveryAmbiguous as exc:
            _record_post_payment_channel(
                order.pk,
                channel,
                "ambiguous",
                error=str(exc),
            )
            return "ambiguous"
        except Exception as exc:
            monobank_logger.exception(
                "Failed to send Facebook event for order %s",
                order.order_number,
            )
            _record_post_payment_channel(
                order.pk,
                channel,
                "unknown",
                error=repr(exc),
            )
            return "ambiguous"

    if channel == "tiktok_purchase":
        try:
            from orders.provider_delivery import ProviderDeliveryAmbiguous
            from orders.tiktok_events_service import get_tiktok_events_service

            service = get_tiktok_events_service()
            if not service.enabled:
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "disabled",
                    error="tiktok_disabled",
                )
                return "disabled"
            if order.payment_status not in ("paid", "prepaid", "partial"):
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "skipped",
                    error="payment_not_verified",
                )
                return "skipped"
            payment_payload = order.payment_payload or {}
            tiktok_events = payment_payload.get("tiktok_events", {})
            if tiktok_events.get("purchase_sent", False):
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "sent",
                    metadata={"already_sent": True},
                )
                return "sent"
            sent = service.send_purchase_event(order)
            if not sent:
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    "failed",
                    error="provider_rejected",
                )
                return "failed"
            payment_payload.setdefault("tiktok_events", {})
            payment_payload["tiktok_events"]["purchase_sent"] = True
            payment_payload["tiktok_events"]["purchase_sent_at"] = (
                timezone.now().isoformat()
            )
            order.payment_payload = payment_payload
            order.save(update_fields=["payment_payload"])
            _record_post_payment_channel(order.pk, channel, "sent")
            return "sent"
        except ProviderDeliveryAmbiguous as exc:
            _record_post_payment_channel(
                order.pk,
                channel,
                "ambiguous",
                error=str(exc),
            )
            return "ambiguous"
        except ImportError:
            _record_post_payment_channel(
                order.pk,
                channel,
                "disabled",
                error="service_unavailable",
            )
            return "disabled"
        except Exception as exc:
            monobank_logger.exception(
                "Failed to send TikTok event for order %s",
                order.order_number,
            )
            _record_post_payment_channel(
                order.pk,
                channel,
                "unknown",
                error=repr(exc),
            )
            return "ambiguous"

    if channel == "receipt_email":
        try:
            if getattr(order, "email", None):
                from orders.email_receipt import send_order_receipt_email

                result = send_order_receipt_email(order)
                if isinstance(result, tuple) and len(result) >= 2:
                    sent, error = result[0], result[1]
                else:
                    sent, error = False, "delivery_unknown"
                state = (
                    "sent"
                    if sent
                    else ("unknown" if error == "delivery_unknown" else "failed")
                )
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    state,
                    error=error or "",
                )
            else:
                state = "skipped"
                _record_post_payment_channel(
                    order.pk,
                    channel,
                    state,
                    error="no_valid_email",
                )
        except Exception:
            monobank_logger.exception(
                "Failed to send receipt email for order %s",
                order.pk,
            )
            state = "unknown"
            _record_post_payment_channel(
                order.pk,
                channel,
                state,
                error="sender_exception",
            )
        _sync_instagram_lifecycle_channel(order.pk)
        return state

    raise ValueError(f"unsupported post-payment channel: {channel}")


def _verify_monobank_signature(request):
    """
    Проверяет подпись Monobank webhook запроса.
    ВОССТАНОВЛЕНА РАБОЧАЯ ЛОГИКА из старого views.py

    Args:
        request: HTTP request с заголовком X-Sign

    Returns:
        bool: True если подпись валидна, False иначе
    """
    from django.core.cache import cache
    from django.conf import settings

    try:
        signature = request.headers.get('X-Sign')
        if not signature:
            monobank_logger.warning('Missing X-Sign header in Monobank webhook')
            return False

        # Получаем публичный ключ из кеша или API
        MONOBANK_PUBLIC_KEY_CACHE_KEY = 'monobank_public_key'
        cached_key = cache.get(MONOBANK_PUBLIC_KEY_CACHE_KEY)

        if not cached_key:
            # Запрашиваем у API
            import requests
            response = requests.get(
                'https://api.monobank.ua/api/merchant/pubkey',
                headers={'X-Token': settings.MONOBANK_TOKEN},
                timeout=10
            )
            response.raise_for_status()
            cached_key = response.json().get('key')

            if cached_key:
                cache.set(MONOBANK_PUBLIC_KEY_CACHE_KEY, cached_key, 3600)

        if not cached_key:
            monobank_logger.error('Failed to get Monobank public key for verification')
            return False

        # Получаем тело запроса
        body = request.body

        # Проверяем подпись
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.backends import default_backend

        # Загружаем публичный ключ
        public_key = serialization.load_pem_public_key(
            cached_key.encode(),
            backend=default_backend()
        )

        # Декодируем подпись из base64
        try:
            signature_bytes = strict_b64decode(signature)
        except InvalidBase64:
            monobank_logger.warning('Monobank signature rejected: invalid Base64')
            return False

        # Проверяем
        try:
            public_key.verify(
                signature_bytes,
                body,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            return True
        except Exception as verify_error:
            monobank_logger.warning(f'Monobank signature verification failed: {verify_error}')
            return False

    except Exception as e:
        monobank_logger.error(f'Error verifying Monobank signature: {e}', exc_info=True)
        return False


def _update_order_from_checkout_result(order, result, source='api'):
    """
    Обновляет заказ из результата Monobank checkout.

    Args:
        order: Объект заказа
        result: Результат от Monobank checkout API
        source: Источник данных ('api' или 'webhook')
    """
    # Преобразуем result в формат payload для _record_monobank_status
    payload = {
        'status': result.get('status', 'unknown'),
        'result': result
    }
    _record_monobank_status(order, payload, source=source)


def clear_cart(request):
    """
    Очистка корзины.

    Удаляет все товары из корзины и сбрасывает промокод.
    """
    request.session['cart'] = {}
    if 'promo_code_id' in request.session:
        del request.session['promo_code_id']
    if 'promo_code_data' in request.session:
        del request.session['promo_code_data']
    request.session.modified = True


def get_liqpay_context(request):
    """
    Get LiqPay context for payment.
    """
    return {}
