from __future__ import annotations

import json
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from .analytics_exclusions import is_request_excluded
from .analytics_noise import is_analytics_noise_path
from .models import PageView, SiteSession
from .utm_utils import get_client_ip, sanitize_utm_param


BOT_SIGNALS = (
    # Generic crawler tokens.
    'bot', 'crawl', 'spider', 'crawler', 'slurp', 'preview',
    # Specific crawlers commonly hitting the site.
    'baiduspider', 'petalbot', 'duckduckbot', 'semrush', 'ahrefs', 'mj12', 'dotbot',
    'googlebot', 'bingbot', 'yandex', 'facebookexternalhit', 'facebot', 'embedly',
    'applebot', 'twitterbot', 'linkedinbot', 'pinterestbot', 'whatsapp', 'telegrambot',
    # Headless / synthetic agents that we treat as non-human traffic.
    'headless', 'phantomjs', 'puppeteer', 'playwright', 'selenium', 'wget', 'curl',
    'python-requests', 'go-http-client', 'pytest', 'lighthouse', 'pagespeed',
)

VISITOR_COOKIE_NAME = 'twc_vid'
FIRST_TOUCH_COOKIE_NAME = 'twc_ft'
COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def is_bot(ua: str | None) -> bool:
    if not ua:
        return False
    ua_l = ua.lower()
    return any(sig in ua_l for sig in BOT_SIGNALS)


def _is_internal_referrer(request, referrer: str | None) -> bool:
    if not referrer:
        return False
    try:
        ref_host = urlparse(referrer).hostname or ''
        req_host = (request.get_host() or '').split(':', 1)[0]
        return bool(ref_host) and ref_host.lower() == req_host.lower()
    except Exception:
        return False


def _build_first_touch_snapshot(request) -> dict:
    referrer = (request.META.get('HTTP_REFERER') or '')[:512]
    snapshot = {
        'landing_path': (request.path or '')[:512],
        'referrer': referrer,
        'created_at': timezone.now().isoformat(),
    }
    for key in ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'):
        value = sanitize_utm_param((request.GET.get(key) or '').strip())
        if value:
            snapshot[key] = value

    if request.GET.get('gclid'):
        snapshot['gclid'] = (request.GET.get('gclid') or '')[:255]
    if request.GET.get('fbclid'):
        snapshot['fbclid'] = (request.GET.get('fbclid') or '')[:255]
    if request.GET.get('ttclid'):
        snapshot['ttclid'] = (request.GET.get('ttclid') or '')[:255]
    return snapshot


class AnalyticsIdentityMiddleware(MiddlewareMixin):
    """
    Assigns stable first-party analytics cookies used to stitch anonymous visits.
    """

    def process_request(self, request):
        # W3-3: /api/bootstrap/ — специальный endpoint ленивой выдачи кук,
        # он ДОЛЖЕН пройти через identity-логику несмотря на noise-префикс /api/.
        is_bootstrap = request.path == '/api/bootstrap/'
        if not is_bootstrap and is_analytics_noise_path(request.path):
            request.analytics_visitor_id = ''
            request.analytics_first_touch_data = {}
            return None

        visitor_id = (request.COOKIES.get(VISITOR_COOKIE_NAME) or '').strip()
        if not visitor_id:
            visitor_id = uuid.uuid4().hex
            request._analytics_set_visitor_cookie = True
        request.analytics_visitor_id = visitor_id



        first_touch = {}
        first_touch_raw = request.COOKIES.get(FIRST_TOUCH_COOKIE_NAME)
        if first_touch_raw:
            try:
                parsed = json.loads(first_touch_raw)
                if isinstance(parsed, dict):
                    first_touch = parsed
            except Exception:
                first_touch = {}

        has_utm = any((request.GET.get(key) or '').strip() for key in ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'))
        has_click_id = any((request.GET.get(key) or '').strip() for key in ('gclid', 'fbclid', 'ttclid'))
        external_referrer = bool(request.META.get('HTTP_REFERER')) and not _is_internal_referrer(request, request.META.get('HTTP_REFERER'))
        if not first_touch and (has_utm or has_click_id or external_referrer):
            first_touch = _build_first_touch_snapshot(request)
            request._analytics_set_first_touch_cookie = True

        # W3-3 (SEO-010): Set-Cookie на обычном анонимном GET выключает
        # LiteSpeed page cache (холодный TTFB 8-18s). Куки разрешено ставить
        # только когда: (1) не-GET запрос; (2) bootstrap-endpoint (ленивая
        # выдача из base.html при отсутствии csrftoken); (3) landing с
        # utm/click-id/внешним referrer — там first-touch атрибуция важнее
        # кэша, и такие URL уникальны по query-string.
        request._analytics_cookie_allowed = (
            request.method not in ('GET', 'HEAD')
            or request.path == '/api/bootstrap/'
            or has_utm
            or has_click_id
            or external_referrer
        )

        request.analytics_first_touch_data = first_touch
        return None

    def process_response(self, request, response):
        # W3-3: на кэшируемых анонимных GET куки не ставим (см. process_request);
        # выдача произойдёт лениво через /api/bootstrap/.
        if not getattr(request, '_analytics_cookie_allowed', True):
            return response

        if getattr(request, '_analytics_set_visitor_cookie', False):
            response.set_cookie(
                VISITOR_COOKIE_NAME,
                getattr(request, 'analytics_visitor_id', ''),
                max_age=COOKIE_MAX_AGE,
                secure=bool(not settings.DEBUG),
                samesite='Lax',
                httponly=True,
            )

        if getattr(request, '_analytics_set_first_touch_cookie', False):
            try:
                payload = json.dumps(getattr(request, 'analytics_first_touch_data', {}), ensure_ascii=False, separators=(',', ':'))
            except Exception:
                payload = '{}'
            response.set_cookie(
                FIRST_TOUCH_COOKIE_NAME,
                payload,
                max_age=COOKIE_MAX_AGE,
                secure=bool(not settings.DEBUG),
                samesite='Lax',
                httponly=True,
            )
        return response


class SimpleAnalyticsMiddleware(MiddlewareMixin):
    """Очень лёгкая мидлварь для учёта сессий и просмотров страниц."""

    def process_request(self, request):
        try:
            host = request.get_host().split(':')[0].lower()
            # DTF subdomain has its own product analytics and should avoid
            # write-heavy storefront tracking on every page view.
            if host.startswith('dtf.'):
                return None

            # Не трекаем ботов и служебные пути
            ua = request.META.get('HTTP_USER_AGENT', '')
            path = request.path or ''
            fetch_mode = (request.META.get('HTTP_SEC_FETCH_MODE') or '').strip().lower()
            if (
                is_bot(ua)
                or is_analytics_noise_path(path)
                or request.method != 'GET'
                or (fetch_mode and fetch_mode not in {'navigate', 'nested-navigate'})
            ):
                return None  # Пропускаем ботов дальше по цепочке middleware

            # Skip writes for IPs / users / agents listed in AnalyticsExclusion.
            if is_request_excluded(request, user_agent=ua, path=path):
                return None

            # Для анонимов создаём сессию на первом «человеческом» переходе
            # (боты/служебные пути уже отфильтрованы выше). Без этого весь
            # анонимный трафик без корзины оставался невидимым для аналитики.
            # Защита от мусорны�� клиентов (feed-пуллеры, json-клиенты):
            # новую сессию создаём только для HTML-навигации.
            if not request.user.is_authenticated and not request.session.session_key:
                accept = (request.META.get('HTTP_ACCEPT') or '').lower()
                if 'text/html' not in accept:
                    return None

            # На этом этапе сессия может существовать (или пользователь авторизован)
            if not request.session.session_key:
                request.session.save()
            session_key = request.session.session_key

            ip = get_client_ip(request)
            bot = is_bot(ua)
            visitor_id = getattr(request, 'analytics_visitor_id', None)
            first_touch_data = getattr(request, 'analytics_first_touch_data', {}) or {}

            with transaction.atomic():
                sess, _ = SiteSession.objects.select_for_update().get_or_create(
                    session_key=session_key,
                    defaults={
                        'visitor_id': visitor_id,
                        'user': request.user if request.user.is_authenticated else None,
                        'ip_address': ip,
                        'user_agent': ua,
                        'is_bot': bot,
                        'last_path': path,
                        'pageviews': 0,
                        'first_touch_data': first_touch_data,
                    }
                )
                # Обновляем
                sess.last_seen = timezone.now()
                if request.user.is_authenticated and sess.user_id != request.user.id:
                    sess.user = request.user
                if visitor_id and sess.visitor_id != visitor_id:
                    sess.visitor_id = visitor_id
                if ip and sess.ip_address != ip:
                    sess.ip_address = ip
                if first_touch_data and not sess.first_touch_data:
                    sess.first_touch_data = first_touch_data
                sess.last_path = path
                sess.pageviews = (sess.pageviews or 0) + 1
                sess.is_bot = sess.is_bot or bot
                sess.save(update_fields=['visitor_id', 'user', 'ip_address', 'last_seen', 'last_path', 'pageviews', 'is_bot', 'first_touch_data'])

                PageView.objects.create(
                    session=sess,
                    user=request.user if request.user.is_authenticated else None,
                    path=path,
                    referrer=request.META.get('HTTP_REFERER', '')[:512],
                    is_bot=bot,
                )
        except Exception as e:
            # Никогда не ломаем страницу из-за аналитики
            pass
