"""
Функции для отслеживания действий пользователей в системе UTM-аналитики.

Используется для записи событий в воронке конверсий:
- Просмотры страниц и товаров
- Добавление/удаление товаров из корзины
- Начало оформления заказа
- Лиды (предоплата)
- Покупки (полная оплата)
"""

import logging
from typing import Optional
from .analytics_exclusions import is_request_excluded
from .models import UTMSession, SiteSession, UserAction
from .utm_utils import calculate_action_points

logger = logging.getLogger(__name__)


def record_user_action(
    request,
    action_type: str,
    product_id: Optional[int] = None,
    product_name: Optional[str] = None,
    cart_value: Optional[float] = None,
    order_id: Optional[int] = None,
    order_number: Optional[str] = None,
    metadata: Optional[dict] = None,
    **kwargs
) -> Optional[UserAction]:
    """
    Записывает действие пользователя для UTM-аналитики.

    Args:
        request: Django request object
        action_type: Тип действия (из UserAction.ACTION_TYPES)
        product_id: ID товара (для product_view, add_to_cart)
        product_name: Название товара
        cart_value: Сумма корзины или заказа
        order_id: ID заказа (для lead, purchase)
        order_number: Номер заказа
        metadata: Дополнительные метаданные (dict)
        **kwargs: Дополнительные параметры

    Returns:
        UserAction или None
    """
    try:
        # Skip writes for explicitly excluded entities (admin office, staff users, etc.).
        if is_request_excluded(request):
            return None

        # Получаем session_key
        session_key = request.session.session_key
        if not session_key:
            request.session.save()
            session_key = request.session.session_key

        if not session_key:
            logger.warning("Could not get session_key for user action")
            return None

        # Получаем UTM сессию
        utm_session = None
        try:
            utm_session = UTMSession.objects.get(session_key=session_key)
        except UTMSession.DoesNotExist:
            logger.debug(f"No UTM session found for session_key: {session_key}")

        # Получаем Site сессию
        site_session = None
        try:
            site_session = SiteSession.objects.get(session_key=session_key)
        except SiteSession.DoesNotExist:
            logger.debug(f"No Site session found for session_key: {session_key}")

        # Получаем пользователя
        user = request.user if request.user.is_authenticated else None

        # Рассчитываем баллы за действие
        points = calculate_action_points(
            action_type,
            cart_value=cart_value,
            order_value=cart_value,
            **kwargs
        )

        base_metadata = dict(metadata or {})
        visitor_id = getattr(request, 'analytics_visitor_id', None)
        if visitor_id and 'visitor_id' not in base_metadata:
            base_metadata['visitor_id'] = visitor_id
        if hasattr(request, 'analytics_first_touch_data') and request.analytics_first_touch_data and 'first_touch' not in base_metadata:
            base_metadata['first_touch'] = request.analytics_first_touch_data

        # Создаем запись действия
        action = UserAction.objects.create(
            utm_session=utm_session,
            site_session=site_session,
            user=user,
            action_type=action_type,
            page_path=request.path[:512] if hasattr(request, 'path') else None,
            product_id=product_id,
            product_name=product_name[:255] if product_name else None,
            cart_value=cart_value,
            order_id=order_id,
            order_number=order_number[:20] if order_number else None,
            metadata=base_metadata,
            points_earned=points,
        )

        logger.info(f"Recorded user action: {action_type} (points: {points})")
        return action

    except Exception as e:
        logger.error(f"Error recording user action: {e}", exc_info=True)
        return None


def record_page_view(request, page_path: Optional[str] = None):
    """Записывает просмотр страницы"""
    return record_user_action(
        request,
        action_type='page_view',
        metadata={'page_path': page_path or request.path}
    )


def record_product_view(request, product_id: int, product_name: Optional[str] = None):
    """Записывает просмотр товара"""
    return record_user_action(
        request,
        action_type='product_view',
        product_id=product_id,
        product_name=product_name
    )


def record_add_to_cart(request, product_id: int, product_name: Optional[str] = None, cart_value: Optional[float] = None):
    """Записывает добавление товара в корзину"""
    return record_user_action(
        request,
        action_type='add_to_cart',
        product_id=product_id,
        product_name=product_name,
        cart_value=cart_value
    )


def record_remove_from_cart(request, product_id: int, product_name: Optional[str] = None, cart_value: Optional[float] = None):
    """Записывает удаление товара из корзины"""
    return record_user_action(
        request,
        action_type='remove_from_cart',
        product_id=product_id,
        product_name=product_name,
        cart_value=cart_value
    )


def record_initiate_checkout(request, cart_value: float):
    """Записывает начало оформления заказа"""
    return record_user_action(
        request,
        action_type='initiate_checkout',
        cart_value=cart_value
    )


def record_lead(request, order_id: int, order_number: str, cart_value: float):
    """
    Записывает лид (предоплата).
    Также помечает UTM-сессию как конверсионную.
    """
    action = record_user_action(
        request,
        action_type='lead',
        order_id=order_id,
        order_number=order_number,
        cart_value=cart_value
    )

    # Помечаем UTM-сессию как конверсионную
    try:
        session_key = request.session.session_key
        if session_key:
            utm_session = UTMSession.objects.get(session_key=session_key)
            utm_session.mark_as_converted(conversion_type='lead')
            logger.info(f"Marked UTM session as converted (lead): {utm_session}")
    except UTMSession.DoesNotExist:
        pass
    except Exception as e:
        logger.error(f"Error marking UTM session as converted: {e}")

    return action


# NOTE (W2-2/TECH-061): мёртвый record_purchase(request, ...) удалён —
# у него было 0 call-sites. Purchase-события записываются через
# record_order_action('purchase', order, ...), который работает и из
# вебхуков (без request) и сам помечает UTM-сессию конверсионной.


def record_search(request, query: str):
    """Записывает поисковый запрос"""
    return record_user_action(
        request,
        action_type='search',
        metadata={'query': query}
    )


def record_custom_print_event(request, action_type: str, *, lead=None, step_key: Optional[str] = None, metadata: Optional[dict] = None):
    """Records a custom-print lifecycle event."""
    payload = dict(metadata or {})
    if lead is not None:
        payload.setdefault('lead_id', getattr(lead, 'pk', None))
        payload.setdefault('lead_number', getattr(lead, 'lead_number', ''))
        payload.setdefault('product_type', getattr(lead, 'product_type', ''))
        payload.setdefault('client_kind', getattr(lead, 'client_kind', ''))
        payload.setdefault('source', getattr(lead, 'source', ''))
    if step_key:
        payload['step_key'] = step_key
    return record_user_action(request, action_type=action_type, metadata=payload)


def record_survey_event(request, action_type: str, *, session=None, question_id: Optional[str] = None, metadata: Optional[dict] = None):
    """Records a survey lifecycle event."""
    payload = dict(metadata or {})
    if session is not None:
        payload.setdefault('survey_session_id', getattr(session, 'pk', None))
        payload.setdefault('survey_key', getattr(session, 'survey_key', ''))
        payload.setdefault('survey_status', getattr(session, 'status', ''))
    if question_id:
        payload['question_id'] = question_id
    return record_user_action(request, action_type=action_type, metadata=payload)


def record_order_action(
    action_type: str,
    order,
    *,
    request=None,
    cart_value: Optional[float] = None,
    metadata: Optional[dict] = None,
) -> Optional[UserAction]:
    """Records an order-level action even when payment confirmation arrives from a webhook."""
    try:
        session_key = None
        if request is not None:
            session_key = request.session.session_key
            if not session_key:
                request.session.save()
                session_key = request.session.session_key
        session_key = session_key or getattr(order, 'session_key', None)

        utm_session = getattr(order, 'utm_session', None)
        if utm_session is None and session_key:
            utm_session = UTMSession.objects.filter(session_key=session_key).first()

        site_session = SiteSession.objects.filter(session_key=session_key).first() if session_key else None
        user = getattr(order, 'user', None)
        if user is None and request is not None and getattr(request.user, 'is_authenticated', False):
            user = request.user

        points = calculate_action_points(
            action_type,
            cart_value=cart_value,
            order_value=cart_value,
        )

        base_metadata = dict(metadata or {})
        if request is not None:
            visitor_id = getattr(request, 'analytics_visitor_id', None)
            first_touch = getattr(request, 'analytics_first_touch_data', None)
        else:
            visitor_id = getattr(site_session, 'visitor_id', None) or getattr(utm_session, 'visitor_id', None)
            first_touch = getattr(site_session, 'first_touch_data', None)

        if visitor_id and 'visitor_id' not in base_metadata:
            base_metadata['visitor_id'] = visitor_id
        if first_touch and 'first_touch' not in base_metadata:
            base_metadata['first_touch'] = first_touch

        action = UserAction.objects.create(
            utm_session=utm_session,
            site_session=site_session,
            user=user,
            action_type=action_type,
            page_path=request.path[:512] if request is not None and hasattr(request, 'path') else None,
            cart_value=cart_value if cart_value is not None else getattr(order, 'total_sum', None),
            order_id=getattr(order, 'pk', None),
            order_number=(getattr(order, 'order_number', None) or '')[:20] or None,
            metadata=base_metadata,
            points_earned=points,
        )

        if utm_session is not None and action_type in {'lead', 'purchase'}:
            utm_session.mark_as_converted(conversion_type=action_type)

        logger.info("Recorded order action: %s for order %s", action_type, getattr(order, 'order_number', getattr(order, 'pk', None)))
        return action
    except Exception as e:
        logger.error(f"Error recording order action: {e}", exc_info=True)
        return None


def resolve_utm_session(request, order=None):
    """
    W2-1 (TECH-060): fallback-цепочка поиска UTM-сессии.

    1. session_key (текущий или сохранённый в заказе);
    2. visitor_id — переживает `cycle_key()` при логине (кука twc_vid живёт
       год, а session_key ротируется);
    3. None — вызывающий код может добрать UTM из session['utm_data'].
    """
    session_key = None
    if request is not None:
        session_key = request.session.session_key
    if not session_key and order is not None:
        session_key = getattr(order, 'session_key', None)

    if session_key:
        utm_session = UTMSession.objects.filter(session_key=session_key).first()
        if utm_session is not None:
            return utm_session

    visitor_id = getattr(request, 'analytics_visitor_id', None) if request is not None else None
    if visitor_id:
        utm_session = (
            UTMSession.objects.filter(visitor_id=visitor_id)
            .order_by('-last_seen')
            .first()
        )
        if utm_session is not None:
            return utm_session

    return None


def link_order_to_utm(request, order):
    """
    Связывает заказ с UTM-сессией.
    Копирует UTM-параметры в заказ для быстрого доступа.

    W2-1: lookup больше не завязан ЖЁСТКО на session_key — используется
    fallback-цепочка session_key → visitor_id → session['utm_data'], иначе
    логин (`cycle_key()`) рвал связь и заказ оставался без атрибуции.
    """
    try:
        # Гарантируем session_key на заказе (для record_order_action и вебхуков)
        if not getattr(order, 'session_key', None) and request.session.session_key:
            order.session_key = request.session.session_key
            order.save(update_fields=['session_key'])

        utm_session = resolve_utm_session(request, order)

        if utm_session is not None:
            order.utm_session = utm_session
            order.utm_source = utm_session.utm_source
            order.utm_medium = utm_session.utm_medium
            order.utm_campaign = utm_session.utm_campaign
            order.utm_content = utm_session.utm_content
            order.utm_term = utm_session.utm_term
            order.save(update_fields=[
                'utm_session', 'utm_source', 'utm_medium',
                'utm_campaign', 'utm_content', 'utm_term'
            ])
            logger.info(f"Linked order {order.order_number} to UTM session: {utm_session}")
            return

        # Fallback 3: UTM из сессии (utm_middleware пишет session['utm_data']
        # даже когда UTMSession-строка не создалась/потерялась).
        utm_data = {}
        try:
            utm_data = request.session.get('utm_data') or {}
        except Exception:
            utm_data = {}
        if utm_data:
            order.utm_source = utm_data.get('utm_source')
            order.utm_medium = utm_data.get('utm_medium')
            order.utm_campaign = utm_data.get('utm_campaign')
            order.utm_content = utm_data.get('utm_content')
            order.utm_term = utm_data.get('utm_term')
            order.save(update_fields=[
                'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'
            ])
            logger.info(f"Linked order {order.order_number} to session utm_data (no UTMSession row)")
            return

        logger.debug("No UTM attribution found for order %s", getattr(order, 'order_number', order.pk))

    except Exception as e:
        logger.error(f"Error linking order to UTM: {e}", exc_info=True)


def build_order_tracking_context(request, order, utm_session=None):
    """
    W2-1 (TECH-060): собирает click-ID контекст (fbp/fbc/ttclid/gclid,
    external_id, ip, ua) для payment_payload['tracking'] ЛЮБОГО заказа —
    раньше это делал только monobank-путь, и COD-заказы уходили в CAPI
    без атрибуции.

    fbc синтезируется из fbclid (формат fb.1.{ts_ms}.{fbclid}), если куки
    _fbc нет — стандартное поведение Meta CAPI.
    """
    import time as _time

    tracking = {}
    cookies = getattr(request, 'COOKIES', {}) or {}

    fbp = cookies.get('_fbp')
    if fbp:
        tracking['fbp'] = fbp

    fbc = cookies.get('_fbc')

    # Источники fbclid по убыванию свежести: URL → first-touch кука → UTMSession
    first_touch = getattr(request, 'analytics_first_touch_data', None) or {}
    fbclid = (
        (request.GET.get('fbclid') if hasattr(request, 'GET') else None)
        or first_touch.get('fbclid')
        or (getattr(utm_session, 'fbclid', None) if utm_session is not None else None)
    )
    if not fbc and utm_session is not None:
        fbc = getattr(utm_session, 'fbc', None)
    if not fbc and fbclid:
        fbc = f"fb.1.{int(_time.time() * 1000)}.{fbclid}"
    if fbc:
        tracking['fbc'] = fbc
    if fbclid:
        tracking['fbclid'] = fbclid

    ttclid = (
        cookies.get('ttclid')
        or first_touch.get('ttclid')
        or (getattr(utm_session, 'ttclid', None) if utm_session is not None else None)
    )
    if ttclid:
        tracking['ttclid'] = ttclid

    gclid = (
        (request.GET.get('gclid') if hasattr(request, 'GET') else None)
        or first_touch.get('gclid')
        or (getattr(utm_session, 'gclid', None) if utm_session is not None else None)
    )
    if gclid:
        tracking['gclid'] = gclid

    # external_id: user → session → order (тот же приоритет, что в monobank-пути)
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        tracking['external_id'] = f"user:{user.id}"
    elif request.session.session_key:
        tracking['external_id'] = f"session:{request.session.session_key}"
    elif getattr(order, 'order_number', None):
        tracking['external_id'] = f"order:{order.order_number}"
    elif getattr(order, 'pk', None):
        tracking['external_id'] = f"order:{order.pk}"

    meta = getattr(request, 'META', {}) or {}
    xff = meta.get('HTTP_X_FORWARDED_FOR')
    client_ip = xff.split(',')[0].strip() if xff else meta.get('REMOTE_ADDR')
    if client_ip:
        tracking['client_ip_address'] = client_ip
    ua = meta.get('HTTP_USER_AGENT')
    if ua:
        tracking['client_user_agent'] = ua

    return tracking


def attach_tracking_to_order(request, order):
    """
    Сохраняет click-ID контекст в order.payment_payload['tracking'], не
    перезаписывая уже существующие значения (например, от monobank-пути).
    """
    try:
        tracking = build_order_tracking_context(request, order, getattr(order, 'utm_session', None))
        if not tracking:
            return
        payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
        existing = payload.get('tracking') if isinstance(payload.get('tracking'), dict) else {}
        merged = {**tracking, **existing}  # существующие значения приоритетнее
        payload['tracking'] = merged
        order.payment_payload = payload
        order.save(update_fields=['payment_payload'])
        logger.info(
            "Attached tracking context to order %s (fbp=%s, fbc=%s)",
            getattr(order, 'order_number', order.pk), bool(merged.get('fbp')), bool(merged.get('fbc')),
        )
    except Exception as e:
        logger.error(f"Error attaching tracking context to order: {e}", exc_info=True)


def mark_user_registered(request):
    """
    Отмечает в UTM-сессии, что пользователь зарегистрировался.
    Вызывается после успешной регистрации.
    """
    try:
        session_key = request.session.session_key
        if not session_key:
            logger.warning("No session_key to mark user registration")
            return

        utm_session = UTMSession.objects.get(session_key=session_key)
        utm_session.mark_user_registered()
        logger.info(f"Marked user as registered in UTM session: {utm_session}")

    except UTMSession.DoesNotExist:
        logger.debug(f"No UTM session found for session_key: {session_key}")
    except Exception as e:
        logger.error(f"Error marking user registration in UTM: {e}", exc_info=True)
