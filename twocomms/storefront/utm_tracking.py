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

from django.db import models, transaction

from .analytics_exclusions import is_request_excluded
from .models import UTMSession, SiteSession, UserAction
from .utm_utils import calculate_action_points, normalize_utm_source, sanitize_utm_param

logger = logging.getLogger(__name__)


def ensure_request_session_key(request) -> str:
    """Return a durable Django session key or fail before order creation.

    Anonymous sessions are lazy. Reading ``request.session.session_key`` alone
    can therefore yield ``None`` even though the request already contains cart
    data. Paid/COD writers must establish the row before copying the key into
    ``Order``; creating it later from analytics leaves an unjoinable order.
    """
    session = getattr(request, 'session', None)
    if session is None:
        raise RuntimeError('Checkout request has no Django session')

    session_key = session.session_key
    if not session_key:
        session.create()
        session_key = session.session_key
    if not session_key:
        raise RuntimeError('Could not establish a durable checkout session')
    return session_key


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

        # W2-4 (AN-035/TECH-063): единый бот-фильтр на записи UserAction.
        # Раньше product_view/add_to_cart писались для любых UA —
        # краулеры и синтетика раздували числитель воронки view→ATC.
        user_agent = request.META.get('HTTP_USER_AGENT', '') if hasattr(request, 'META') else ''
        from .tracking import is_bot as _is_bot_ua, is_trackable_navigation_request
        if _is_bot_ua(user_agent):
            return None

        # W2-4 (AN-004): staff-пользователи не попадают в аналитику.
        req_user = getattr(request, 'user', None)
        if req_user is not None and getattr(req_user, 'is_authenticated', False) and getattr(req_user, 'is_staff', False):
            return None

        # F-076: a server-side product_view must describe the same human page
        # navigation accepted by SimpleAnalyticsMiddleware. Previously HEAD,
        # no-cors/subresource and new anonymous non-HTML requests skipped the
        # PageView writer but still manufactured a SiteSession + product_view.
        if action_type == 'product_view':
            if not is_trackable_navigation_request(request):
                return None
            if not getattr(request, '_analytics_pageview_recorded', False):
                return None

        # Получаем session_key
        session_key = ensure_request_session_key(request)

        # W2-4: дедуп product_view — 30 минут на пару (session, product).
        # Reload/переход по вариантам цвета-размера не считается новым
        # просмотром; иначе числитель view→ATC завышен в разы.
        if action_type == 'product_view' and product_id:
            from django.utils import timezone as _tz
            from datetime import timedelta as _td
            window_start = _tz.now() - _td(minutes=30)
            already = UserAction.objects.filter(
                action_type='product_view',
                product_id=product_id,
                timestamp__gte=window_start,
            ).filter(
                models.Q(site_session__session_key=session_key)
                | models.Q(metadata__visitor_id=getattr(request, 'analytics_visitor_id', None) or '__none__')
            ).exists()
            if already:
                logger.debug(
                    "Dedup: product_view for product %s within 30min window (session %s)",
                    product_id, session_key,
                )
                return None

        # Получаем UTM сессию
        utm_session = None
        try:
            utm_session = UTMSession.objects.get(session_key=session_key)
        except UTMSession.DoesNotExist:
            logger.debug(f"No UTM session found for session_key: {session_key}")

        # Получаем Site сессию.
        # W2-4: раньше — .get() c DoesNotExist → 96,2% product_view без
        # site_session (middleware мог не создать строку: не-HTML accept,
        # cycle_key при логине и т.п.). Теперь создаём при отсутствии.
        site_session = None
        try:
            site_session, _created = SiteSession.objects.get_or_create(
                session_key=session_key,
                defaults={
                    'visitor_id': getattr(request, 'analytics_visitor_id', None) or '',
                    'user': req_user if (req_user is not None and getattr(req_user, 'is_authenticated', False)) else None,
                    'user_agent': user_agent,
                    'is_bot': False,
                    'last_path': (request.path or '')[:512] if hasattr(request, 'path') else '',
                    'pageviews': 0,
                    'first_touch_data': getattr(request, 'analytics_first_touch_data', {}) or {},
                },
            )
        except Exception:
            logger.debug(
                "Could not get_or_create Site session for session_key: %s",
                session_key,
                exc_info=True,
            )
            # A product view without this link is not analytically useful and
            # was the direct source of the historical F-076 inflation. Other
            # action types retain their existing best-effort fallback.
            if action_type == 'product_view':
                return None

        if action_type == 'product_view':
            tracked_session_id = getattr(request, '_analytics_site_session_id', None)
            if (
                site_session is None
                or site_session.pk != tracked_session_id
                or (site_session.pageviews or 0) <= 0
            ):
                return None

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
    """
    Записывает поисковый запрос.

    W2-10/AN-039: запрос обрезается до 200 символов и маскируются
    PII-паттерны (email, телефон, номер карты) — раньше писался сырым.
    """
    import re
    cleaned = (query or '')[:200]
    # email → [email]
    cleaned = re.sub(r'[\w.+-]+@[\w-]+\.[\w.]+', '[email]', cleaned)
    # 12-19 цифр подряд (карта) → [card]
    cleaned = re.sub(r'\b\d[\d\s-]{10,17}\d\b', '[number]', cleaned)
    return record_user_action(
        request,
        action_type='search',
        metadata={'query': cleaned}
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


def _persist_order_action(
    *,
    action_type,
    order_id,
    defaults,
    base_metadata,
    occurred_at,
    utm_session,
):
    """Persist and convert inside a savepoint-safe atomic block."""
    with transaction.atomic():
        if order_id is not None:
            action, created = UserAction.objects.get_or_create(
                action_type=action_type,
                order_id=order_id,
                defaults=defaults,
            )
        else:
            action = UserAction.objects.create(
                action_type=action_type,
                order_id=None,
                **defaults,
            )
            created = True

        if not created:
            update_fields = []
            for field, value in (
                ('utm_session', utm_session),
                ('site_session', defaults['site_session']),
                ('user', defaults['user']),
                ('page_path', defaults['page_path']),
                ('cart_value', defaults['cart_value']),
                ('order_number', defaults['order_number']),
            ):
                if field in {'utm_session', 'site_session', 'user'}:
                    current_value = getattr(action, f'{field}_id', None)
                    target_value = getattr(value, 'pk', None)
                else:
                    current_value = getattr(action, field)
                    target_value = value
                if current_value is None and target_value is not None:
                    setattr(action, field, value)
                    update_fields.append(field)

            merged_metadata = dict(base_metadata)
            merged_metadata.update(action.metadata or {})
            if merged_metadata != (action.metadata or {}):
                action.metadata = merged_metadata
                update_fields.append('metadata')
            if update_fields:
                action.save(update_fields=update_fields)

        if created and occurred_at is not None:
            UserAction.objects.filter(pk=action.pk).update(timestamp=occurred_at)
            action.timestamp = occurred_at

        conversion_session = action.utm_session if action.utm_session_id else utm_session
        if conversion_session is not None and action_type in {'lead', 'purchase'}:
            conversion_session.mark_as_converted(
                conversion_type=action_type,
                converted_at=occurred_at,
            )

    return action, created


def record_order_action(
    action_type: str,
    order,
    *,
    request=None,
    cart_value: Optional[float] = None,
    metadata: Optional[dict] = None,
    occurred_at=None,
    raise_errors: bool = False,
) -> Optional[UserAction]:
    """Record one idempotent order-level action.

    ``action_type`` + ``order_id`` is a database-backed idempotency key. This
    is essential for payment and delivery retries: a repeated webhook must be
    able to heal a missing conversion link without creating another purchase.
    """
    try:
        order_id = getattr(order, 'pk', None)
        with transaction.atomic():
            # Serialize attribution repair and live conversion writers on the
            # Order row.  If a writer held a stale Python object while a
            # reconciliation committed, it must read the current DB linkage
            # after acquiring this lock instead of persisting a null UTM.
            locked_order = None
            if order_id is not None:
                locked_order = (
                    type(order)._default_manager.select_for_update()
                    .only('session_key', 'utm_session')
                    .get(pk=order_id)
                )

            session_key = None
            if request is not None:
                session_key = ensure_request_session_key(request)
            session_key = (
                session_key
                or getattr(order, 'session_key', None)
                or getattr(locked_order, 'session_key', None)
            )

            utm_session = getattr(order, 'utm_session', None)
            if (
                utm_session is None
                and locked_order is not None
                and locked_order.utm_session_id is not None
            ):
                utm_session = locked_order.utm_session
            if utm_session is None and session_key:
                utm_session = UTMSession.objects.filter(session_key=session_key).first()

            site_session = getattr(utm_session, 'session', None) if utm_session else None
            if site_session is None and session_key:
                site_session = SiteSession.objects.filter(session_key=session_key).first()
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

            defaults = {
                'utm_session': utm_session,
                'site_session': site_session,
                'user': user,
                'page_path': request.path[:512] if request is not None and hasattr(request, 'path') else None,
                'cart_value': cart_value if cart_value is not None else getattr(order, 'total_sum', None),
                'order_number': (getattr(order, 'order_number', None) or '')[:20] or None,
                'metadata': base_metadata,
                'points_earned': points,
            }
            action, created = _persist_order_action(
                action_type=action_type,
                order_id=order_id,
                defaults=defaults,
                base_metadata=base_metadata,
                occurred_at=occurred_at,
                utm_session=utm_session,
            )

        logger.info(
            "%s order action: %s for order %s",
            "Recorded" if created else "Reused",
            action_type,
            getattr(order, 'order_number', getattr(order, 'pk', None)),
        )
        return action
    except Exception as e:
        logger.error(f"Error recording order action: {e}", exc_info=True)
        if raise_errors:
            raise
        return None


CONFIRMED_PURCHASE_STATUSES = frozenset({'paid', 'prepaid', 'partial'})


def ensure_order_purchase_action(
    order,
    *,
    request=None,
    metadata: Optional[dict] = None,
    occurred_at=None,
    raise_errors: bool = False,
) -> Optional[UserAction]:
    """Ensure a confirmed, non-free order has exactly one purchase action."""
    payment_status = str(getattr(order, 'payment_status', '') or '').strip().lower()
    if payment_status not in CONFIRMED_PURCHASE_STATUSES:
        return None

    payment_payload = getattr(order, 'payment_payload', None)
    if isinstance(payment_payload, dict) and payment_payload.get('manual_payment_preset') == 'free':
        return None

    payload = dict(metadata or {})
    payload.setdefault('payment_status', payment_status)
    return record_order_action(
        'purchase',
        order,
        request=request,
        cart_value=float(getattr(order, 'total_sum', 0) or 0),
        metadata=payload,
        occurred_at=occurred_at,
        raise_errors=raise_errors,
    )


def remove_order_purchase_action(order) -> int:
    """Remove a reclassified manual purchase and rebuild affected UTM state.

    This is intentionally separate from refunds/cancellations. It is used
    only when staff explicitly changes a manually created order to the
    ``free`` preset, meaning the original internal purchase was invalid.
    """
    with transaction.atomic():
        actions = list(
            UserAction.objects.select_for_update().filter(
                action_type='purchase',
                order_id=getattr(order, 'pk', None),
            )
        )
        session_ids = {
            action.utm_session_id
            for action in actions
            if action.utm_session_id is not None
        }
        order_utm_session_id = getattr(order, 'utm_session_id', None)
        if order_utm_session_id is not None:
            session_ids.add(order_utm_session_id)

        deleted_count = len(actions)
        if actions:
            UserAction.objects.filter(pk__in=[action.pk for action in actions]).delete()

        for session_id in sorted(session_ids):
            session = UTMSession.objects.select_for_update().get(pk=session_id)
            strongest_action = (
                UserAction.objects.filter(
                    utm_session_id=session_id,
                    action_type='purchase',
                )
                .order_by('timestamp', 'pk')
                .first()
            )
            if strongest_action is None:
                strongest_action = (
                    UserAction.objects.filter(
                        utm_session_id=session_id,
                        action_type='lead',
                    )
                    .order_by('timestamp', 'pk')
                    .first()
                )

            session.is_converted = strongest_action is not None
            session.conversion_type = (
                strongest_action.action_type if strongest_action is not None else None
            )
            session.converted_at = (
                strongest_action.timestamp if strongest_action is not None else None
            )
            session.save(
                update_fields=['is_converted', 'conversion_type', 'converted_at'],
            )

    return deleted_count


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


def _rebuild_utm_session_from_attribution(request, order, utm_data, platform_data=None):
    """Rebuild the durable attribution row from session/cookie data.

    The first-touch cookie outlives both a rotated Django session and an
    accidentally missing ``UTMSession`` row.  Orders must not lose campaign
    attribution merely because that intermediate row disappeared.
    """
    utm_data = dict(utm_data or {})
    platform_data = dict(platform_data or {})
    utm_fields = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')
    click_fields = ('fbclid', 'gclid', 'ttclid')

    clean_utm = {}
    for field in utm_fields:
        value = sanitize_utm_param(str(utm_data.get(field) or '').strip())
        if value:
            clean_utm[field] = value
    if clean_utm.get('utm_source'):
        clean_utm['utm_source'] = normalize_utm_source(clean_utm['utm_source'])

    clean_platform = {}
    for field in click_fields:
        value = str(platform_data.get(field) or utm_data.get(field) or '').strip()[:255]
        if value:
            clean_platform[field] = value
    for field in ('fbc', 'fbp'):
        value = str(platform_data.get(field) or '').strip()[:255]
        if value:
            clean_platform[field] = value

    if not clean_utm and not clean_platform:
        return None

    session_key = (
        getattr(order, 'session_key', None)
        or ensure_request_session_key(request)
    )

    site_session = SiteSession.objects.filter(session_key=session_key).first()
    defaults = {
        **clean_utm,
        **clean_platform,
        'visitor_id': getattr(request, 'analytics_visitor_id', None),
        'referrer': str(utm_data.get('referrer') or '')[:512] or None,
        'landing_page': str(utm_data.get('landing_path') or request.path or '')[:512] or None,
    }
    if site_session is not None and not UTMSession.objects.filter(session=site_session).exists():
        defaults['session'] = site_session

    utm_session, created = UTMSession.objects.get_or_create(
        session_key=session_key,
        defaults=defaults,
    )
    if not created:
        updated_fields = []
        for field, value in defaults.items():
            if value and not getattr(utm_session, f'{field}_id' if field == 'session' else field, None):
                setattr(utm_session, field, value)
                updated_fields.append(field)
        if updated_fields:
            utm_session.save(update_fields=updated_fields)
    return utm_session


def _apply_utm_session_to_order(order, utm_session):
    update_fields = [
        'utm_session', 'utm_source', 'utm_medium',
        'utm_campaign', 'utm_content', 'utm_term',
    ]
    if not getattr(order, 'session_key', None) and utm_session.session_key:
        order.session_key = utm_session.session_key
        update_fields.append('session_key')
    order.utm_session = utm_session
    order.utm_source = utm_session.utm_source
    order.utm_medium = utm_session.utm_medium
    order.utm_campaign = utm_session.utm_campaign
    order.utm_content = utm_session.utm_content
    order.utm_term = utm_session.utm_term
    order.save(update_fields=update_fields)


def link_order_to_utm(request, order):
    """
    Связывает заказ с UTM-сессией.
    Копирует UTM-параметры в заказ для быстрого доступа.

    W2-1: lookup больше не завязан ЖЁСТКО на session_key — используется
    fallback-цепочка session_key → visitor_id → session['utm_data'], иначе
    логин (`cycle_key()`) рвал связь и заказ оставался без атрибуции.
    """
    try:
        # Защитный инвариант для будущих web-writer'ов: даже если caller не
        # сделал ensure до Order.save(), UTM-link не оставляет пустой join key.
        session_key = ensure_request_session_key(request)
        if not getattr(order, 'session_key', None):
            order.session_key = session_key
            order.save(update_fields=['session_key'])

        utm_session = resolve_utm_session(request, order)

        if utm_session is not None:
            _apply_utm_session_to_order(order, utm_session)
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
            utm_session = _rebuild_utm_session_from_attribution(
                request,
                order,
                utm_data,
                request.session.get('platform_data') or {},
            )
            if utm_session is not None:
                _apply_utm_session_to_order(order, utm_session)
                logger.info(f"Rebuilt and linked UTM session for order {order.order_number} from session data")
                return

        # Fallback 4 (F-071): the durable first-touch cookie survives session
        # rotation and is therefore the final source of truth for attribution.
        first_touch = getattr(request, 'analytics_first_touch_data', None) or {}
        if first_touch:
            utm_session = _rebuild_utm_session_from_attribution(
                request,
                order,
                first_touch,
                request.session.get('platform_data') or {},
            )
            if utm_session is not None:
                _apply_utm_session_to_order(order, utm_session)
                logger.info(f"Rebuilt and linked UTM session for order {order.order_number} from first-touch data")
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
