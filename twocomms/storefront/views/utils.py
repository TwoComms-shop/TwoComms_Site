"""
Утилиты и helper функции для views модуля storefront.

Содержит общие функции, которые используются в разных view модулях.
"""

import hashlib
from urllib.parse import urlencode
from functools import wraps

from django.core.cache import cache
from django.db import transaction
from django.utils.encoding import iri_to_uri


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


def _build_anon_cache_key(request, view_func, key_prefix=None):
    path = iri_to_uri(request.path)
    query = _build_query_string(request.GET)
    accept_lang = request.META.get('HTTP_ACCEPT_LANGUAGE', '')
    language = getattr(request, 'LANGUAGE_CODE', '')
    scheme = getattr(request, 'scheme', '') or 'http'
    try:
        host = request.get_host().lower()
    except Exception:
        host = str(request.META.get('HTTP_HOST') or request.META.get('SERVER_NAME') or '').lower()
    if callable(key_prefix):
        prefix = key_prefix(request, view_func)
    else:
        prefix = key_prefix or f"{view_func.__module__}.{view_func.__name__}"
    fingerprint = f"{scheme}://{host}{path}?{query}|{language}|{accept_lang}"
    digest = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()
    return f"anon-page:{prefix}:{digest}"


def cache_page_for_anon(timeout, key_prefix=None):
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

            cache_key = _build_anon_cache_key(request, view_func, key_prefix)
            cached_response = cache.get(cache_key)
            if cached_response is not None:
                # W3-3/W3-4: раньше здесь принудительно вызывался get_token()
                # → Set-Cookie: csrftoken на КАЖДОМ cache-hit → LiteSpeed
                # page cache выключался (SEO-010, TTFB 8-18s). Теперь
                # csrftoken выдаётся лениво через /api/bootstrap/ (дергается
                # из base.html, если cookie отсутствует) — кэшированный ответ
                # остаётся чистым от Set-Cookie.
                return cached_response

            response = view_func(request, *args, **kwargs)

            if getattr(response, 'streaming', False):
                return response
            if response.status_code != 200:
                return response

            cache.set(cache_key, response, timeout)
            return response
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
            if variant is None or variant.product_id != item['product_id']:
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

    variants = ProductColorVariant.objects.in_bulk(variant_ids)
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
    from fable5.services import effective_cart_unit_price

    cart, _ = normalize_cart_session(cart)
    if not cart:
        return Decimal('0')

    # Получаем все товары одним запросом
    ids = [item['product_id'] for item in cart.values()]
    products = Product.objects.in_bulk(ids)
    variant_ids = [item.get('color_variant_id') for item in cart.values() if item.get('color_variant_id')]
    variants = ProductColorVariant.objects.in_bulk(variant_ids)
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
                from orders.models import PaymentAttempt
                PaymentAttempt.objects.filter(
                    pk=attempt_id,
                    status__in=(PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.PROCESSING),
                ).update(
                    status=PaymentAttempt.Status.CANCELLED,
                    error_reason='checkout_cancelled',
                )
            except Exception:
                monobank_logger.debug(
                    'Failed to cancel payment attempt %s', attempt_id, exc_info=True
                )

    for key in (
        'monobank_pending_order_id',
        'monobank_invoice_id',
        'monobank_order_id',
        'monobank_order_ref',
        'monobank_pending_attempt_id',
        'monobank_attempt_id',
    ):
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
        source: Источник данных ('api' и������и 'webhook')
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

        # W2-7 (AN-011/DB-009): внешние HTTP-вызовы (Telegram, Meta CAPI,
        # TikTok) раньше выполнялись ЗДЕСЬ — внутри select_for_update()
        # транзакции, удерживая row-lock на заказ до ~25-40s. Теперь они
        # откладываются через transaction.on_commit и выполняются ПОСЛЕ
        # снятия блокировки.
        if previous_status != order.payment_status:
            order_pk = order.pk
            prev_for_notify = normalized_previous or 'unpaid'
            pay_type_for_notify = pay_type
            transaction.on_commit(
                lambda: _dispatch_post_payment_events(order_pk, prev_for_notify, pay_type_for_notify)
            )

        return

    if status in MONOBANK_PENDING_STATUSES:
        order.payment_status = 'checking'
        update_fields.append('payment_status')
    elif status in MONOBANK_FAILURE_STATUSES:
        order.payment_status = 'unpaid'
        update_fields.append('payment_status')

    _save_status_fields(update_fields, 'non_success_transition')


def _dispatch_post_payment_events(order_pk, previous_status, pay_type):
    """
    W2-9 (AN-011): запуск post-payment событий в фоновом daemon-потоке.

    Meta CAPI retry использует блокирующий time.sleep (до ~3.5s суммарно
    на 3 попытки с backoff) — в on_commit это держало воркер и задерживало
    ответ вебхуку Monobank. Поток снимает блокировку с request-цикла;
    DB-соединение потока закрывается в finally (иначе утечка коннектов).
    В тестах (Django TestCase) поток не используется — иначе фоновые
    записи ломают транзакционную изоляцию тестов.
    """
    import sys
    import threading

    # В тестовом раннере выполняем синхронно (транзакционная изоляция)
    if 'test' in sys.argv:
        _send_post_payment_events(order_pk, previous_status, pay_type)
        return

    def _runner():
        from django.db import connection
        try:
            _send_post_payment_events(order_pk, previous_status, pay_type)
        finally:
            try:
                connection.close()
            except Exception:
                pass

    threading.Thread(target=_runner, daemon=True, name=f'post-payment-{order_pk}').start()


def _send_post_payment_events(order_pk, previous_status, pay_type):
    """
    W2-7: отправка внешних событий (Telegram, Meta CAPI, TikTok) ПОСЛЕ
    коммита транзакции — row-lock на заказ уже снят. Дедуп-флаги
    (purchase_sent/lead_sent/order_notification_sent) сохраняются в
    payment_payload, как и раньше.
    """
    from django.utils import timezone

    from orders.models import Order

    try:
        order = Order.objects.select_related('user').get(pk=order_pk)
    except Order.DoesNotExist:
        monobank_logger.error('Post-payment events: order %s not found', order_pk)
        return

    # Уведомление админу о смене статуса оплаты
    try:
        from orders.telegram_notifications import TelegramNotifier
        notifier = TelegramNotifier()
        notifier.send_admin_payment_status_update(
            order,
            old_status=previous_status or 'unpaid',
            new_status=order.payment_status,
            pay_type=pay_type,
        )
    except Exception:
        monobank_logger.exception(
            f'Failed to send admin payment status update for order {order.order_number}'
        )

    # Проверяем что Telegram уведомление еще не отправлено (защита от дублирования)
    payment_payload = order.payment_payload or {}
    telegram_notifications = payment_payload.get('telegram_notifications', {})
    telegram_sent = telegram_notifications.get('order_notification_sent', False)

    # 1. Telegram уведомление (только если еще не отправлено)
    if not telegram_sent:
        try:
            from orders.telegram_notifications import TelegramNotifier
            notifier = TelegramNotifier()
            delivered = notifier.send_new_order_notification(order)

            if delivered:
                # Сохраняем в payment_payload что уведомление отправлено
                if 'telegram_notifications' not in payment_payload:
                    payment_payload['telegram_notifications'] = {}
                payment_payload['telegram_notifications']['order_notification_sent'] = True
                payment_payload['telegram_notifications']['order_notification_sent_at'] = timezone.now().isoformat()
                payment_payload['telegram_notifications']['order_notification_status'] = order.payment_status
                order.payment_payload = payment_payload
                order.save(update_fields=['payment_payload'])

                monobank_logger.info(
                    f'📱 Telegram notification sent for order {order.order_number} '
                    f'(status: {previous_status} → {order.payment_status})'
                )
            else:
                monobank_logger.warning(
                    'Telegram notification delivery failed for order %s',
                    order.order_number,
                )
        except Exception as e:
            monobank_logger.exception(f'Failed to send Telegram notification for order {order.order_number}: {e}')
    else:
        monobank_logger.info(
            f'⚠️ Order {order.order_number}: Telegram notification already sent '
            f'(status changed: {previous_status} → {order.payment_status}), skipping duplicate'
        )

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
                event_success = send_event(order)
                if event_success:
                    payment_payload['facebook_events'] = facebook_events
                    facebook_events[event_key] = True
                    facebook_events[f'{event_key}_at'] = timezone.now().isoformat()
                    order.payment_payload = payment_payload
                    order.save(update_fields=['payment_payload'])
                    monobank_logger.info(
                        f'✅ Facebook {event_label} event sent for order {order.order_number} '
                        f'(payment_status={order.payment_status})'
                    )
                else:
                    monobank_logger.warning(
                        f'⚠️ Failed to send Facebook {event_label} event for order {order.order_number}'
                    )
        else:
            monobank_logger.warning(f'⚠️ Facebook Conversions API not enabled, skipping event')
    except Exception as e:
        monobank_logger.exception(f'Failed to send Facebook event for order {order.order_number}: {e}')

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
                    else:
                        monobank_logger.warning(f'⚠️ Failed to send TikTok Purchase event for order {order.order_number}')
        else:
            monobank_logger.warning('⚠️ TikTok Events API not enabled, skipping events')
    except ImportError:
        monobank_logger.debug('TikTok Events service module not found, skipping')
    except Exception as e:
        monobank_logger.exception(f'Failed to send TikTok event for order {order.order_number}: {e}')

    # 4. Customer receipt email. The sender itself persists an idempotency flag
    # in payment_payload, so webhook + return races cannot duplicate the email.
    try:
        if getattr(order, 'email', None):
            from orders.email_receipt import send_order_receipt_email
            send_order_receipt_email(order)
    except Exception:
        monobank_logger.exception(
            'Failed to send receipt email for order %s', order.pk
        )


def _verify_monobank_signature(request):
    """
    Проверяет подпись Monobank webhook запроса.
    ВОССТАНОВЛЕНА РАБОЧАЯ ЛОГИКА из старого views.py

    Args:
        request: HTTP request с заголовком X-Sign

    Returns:
        bool: True если подпись валидна, False иначе
    """
    import base64
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
        signature_bytes = base64.b64decode(signature)

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
