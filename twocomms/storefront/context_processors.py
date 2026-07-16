from cache_utils import get_cache
from django.conf import settings
from django.urls import NoReverseMatch, reverse
from django.templatetags.static import static

from accounts.models import UserProfile
from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
from storefront.services.web_push import is_web_push_configured


def orders_processing_count(request):
    """
    Контекстный процессор для добавления счетчика заказов в обработке
    """
    try:
        host = request.get_host().split(':')[0].lower()
    except Exception:
        host = ""
    if host.startswith('dtf.'):
        return {'orders_processing_count': 0}

    if request.user.is_authenticated and request.user.is_staff:
        try:
            cache_key = 'orders_processing_count'
            cache_backend = get_cache('fragments')
            processing_count = cache_backend.get(cache_key)
            if processing_count is None:
                from orders.models import Order
                processing_count = Order.get_processing_count()
                cache_backend.set(cache_key, processing_count, 60)
        except Exception:
            processing_count = 0
    else:
        processing_count = 0

    return {
        'orders_processing_count': processing_count
    }


def user_state_hint(request):
    """
    Дёшевый маркер «есть ли что синхронизировать»: если у анонимного пользователя
    нет ни cart, ни session favorites, клиентские fetch-и /cart/summary/ и
    /favorites/count/ бессмысленны (вернут 0). Фронт читает `window.__TC_SYNC_BADGES`
    и пропускает запросы, если маркер = false. После любого действия (add to cart/fav)
    сессия заполнится → следующая страница разрешит fetch.

    Нет DB-запросов. Для авторизованных — всегда true (у них может быть избранное в БД).
    """
    has_cart = False
    has_favs = False
    try:
        cart = request.session.get('cart')
        if isinstance(cart, dict) and cart:
            has_cart = True
        if not has_cart:
            custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY)
            if not custom_cart:
                custom_cart = request.session.get('custom_cart')
            if isinstance(custom_cart, dict) and custom_cart:
                has_cart = True
    except Exception:
        has_cart = True  # не мешаем работе при ошибке
    try:
        if request.user.is_authenticated:
            has_favs = True
        else:
            session_favs = request.session.get('favorites')
            has_favs = bool(session_favs)
    except Exception:
        has_favs = True

    return {
        'sync_cart_badge': has_cart,
        'sync_favorites_badge': has_favs,
    }


def analytics_settings(request):
    """
    Контекстный процессор для добавления настроек аналитики в шаблоны
    """
    import os
    # Получаем TikTok Pixel ID из settings, если не задан - используем дефолтный
    tiktok_pixel_id = getattr(settings, 'TIKTOK_PIXEL_ID', None)
    if not tiktok_pixel_id:
        tiktok_pixel_id = 'D43L7DBC77UA61AHLTVG'

    # Получаем TikTok Test Event Code из environment или settings
    # Используется для тестирования событий в TikTok Events Manager
    tiktok_test_event_code = (
        os.environ.get('TIKTOK_TEST_EVENT_CODE') or
        getattr(settings, 'TIKTOK_TEST_EVENT_CODE', None)
    )

    return {
        'TIKTOK_PIXEL_ID': tiktok_pixel_id,
        'TIKTOK_TEST_EVENT_CODE': tiktok_test_event_code,
        'META_PIXEL_ID': getattr(settings, 'META_PIXEL_ID', ''),
    }


def site_urls(request):
    """Canonical public origin for SEO, social previews, and structured data."""
    return {
        "site_base_url": (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
    }


def web_push_settings(request):
    base_url = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    icon_path = getattr(settings, "WEB_PUSH_ICON_PATH", "") or static("img/favicon-192x192.png")
    badge_path = getattr(settings, "WEB_PUSH_BADGE_PATH", "") or static("img/favicon-192x192.png")
    push_enabled = is_web_push_configured()
    current_user = getattr(request, "user", None)
    is_authenticated = bool(getattr(current_user, "is_authenticated", False))
    viewer_preferences = {
        "marketingEnabled": True,
        "orderUpdatesEnabled": True,
    }

    try:
        subscribe_url = reverse("push_subscribe")
        unsubscribe_url = reverse("push_unsubscribe")
        profile_url = reverse("profile_setup")
    except NoReverseMatch:
        subscribe_url = ""
        unsubscribe_url = ""
        profile_url = ""
        push_enabled = False

    if is_authenticated:
        try:
            profile = current_user.userprofile
        except UserProfile.DoesNotExist:
            profile = None
        if profile is not None:
            viewer_preferences = {
                "marketingEnabled": bool(getattr(profile, "push_marketing_enabled", True)),
                "orderUpdatesEnabled": bool(getattr(profile, "push_order_updates_enabled", True)),
            }

    def _abs_url(path):
        if not path:
            return ""
        if path.startswith(("http://", "https://")):
            return path
        return f"{base_url}{path}"

    return {
        "web_push_config": {
            "enabled": push_enabled,
            "vapidPublicKey": getattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", ""),
            "serviceWorkerUrl": getattr(settings, "WEB_PUSH_SERVICE_WORKER_PATH", "/sw.js"),
            "subscribeUrl": subscribe_url,
            "unsubscribeUrl": unsubscribe_url,
            "promptDelayMs": int(getattr(settings, "WEB_PUSH_PROMPT_DELAY_MS", 12000) or 12000),
            "siteBaseUrl": base_url,
            "defaultIconUrl": _abs_url(icon_path),
            "defaultBadgeUrl": _abs_url(badge_path),
            "viewer": {
                "isAuthenticated": is_authenticated,
                "profileUrl": profile_url,
                "preferences": viewer_preferences,
            },
            "strategy": {
                "repeatVisitMin": int(getattr(settings, "WEB_PUSH_PROMPT_MIN_REPEAT_VISITS", 2) or 2),
                "pageViewMin": int(getattr(settings, "WEB_PUSH_PROMPT_MIN_PAGE_VIEWS", 3) or 3),
                "warmupMs": int(getattr(settings, "WEB_PUSH_PROMPT_WARMUP_MS", 45000) or 45000),
                "visitGapMs": int(getattr(settings, "WEB_PUSH_PROMPT_VISIT_GAP_MS", 21600000) or 21600000),
                "cartPromptDelayMs": int(getattr(settings, "WEB_PUSH_CART_PROMPT_DELAY_MS", 4000) or 4000),
                "orderSuccessPromptDelayMs": int(
                    getattr(settings, "WEB_PUSH_ORDER_SUCCESS_PROMPT_DELAY_MS", 5000) or 5000
                ),
                "subscriptionSyncIntervalMs": int(
                    getattr(settings, "WEB_PUSH_SUBSCRIPTION_SYNC_INTERVAL_MS", 86400000)
                    or 86400000
                ),
            },
        }
    }
