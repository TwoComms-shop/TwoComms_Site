"""
Дополнительные middleware для TwoComms
"""

from django.http import HttpResponsePermanentRedirect, HttpResponse
from django.conf import settings
from django.core import signing
from django.utils.deprecation import MiddlewareMixin
from django.core.cache import cache
from django.core.exceptions import DisallowedHost
from django.db import DatabaseError
from django.contrib.redirects.middleware import RedirectFallbackMiddleware
from django.utils.crypto import constant_time_compare
import os
import re
import time


class ForceHTTPSMiddleware(MiddlewareMixin):
    """
    Middleware для принудительного редиректа на HTTPS
    """

    def process_request(self, request):
        # Следуем явной настройке, а не побочно завязаны на DEBUG.
        if not getattr(settings, 'SECURE_SSL_REDIRECT', False):
            return None

        # Service worker registration on some production frontends can reach Django
        # without the usual proxy HTTPS markers, which creates a self-redirect loop.
        # The worker still requires a secure browser context, so skip middleware
        # canonicalization here and let the dedicated view answer directly.
        if request.path in {'/sw.js', '/static/sw.js'}:
            return None

        if request.path.startswith('/tg-manager/webhook/'):
            return None

        if request.path.startswith('/bot/webhook/'):
            return None

        # Проверяем, что запрос идет по HTTP
        if not request.is_secure():
            try:
                # Создаем HTTPS URL
                https_url = request.build_absolute_uri().replace('http://', 'https://', 1)
            except DisallowedHost:
                # Let Django return a regular DisallowedHost response without noisy middleware traceback.
                return None

            # Выполняем постоянный редирект (301)
            return HttpResponsePermanentRedirect(https_url)

        return None


class WWWRedirectMiddleware(MiddlewareMixin):
    """
    Middleware для редиректа с www на основной домен
    """

    def process_request(self, request):
        # Проверяем, что мы не в режиме отладки
        if settings.DEBUG:
            return None

        try:
            host = request.get_host()
        except DisallowedHost:
            return None

        # Проверяем, что запрос идет с www
        if host.startswith('www.'):
            # Создаем URL без www
            non_www_url = request.build_absolute_uri().replace('://www.', '://', 1)

            # Выполняем постоянный редирект (301)
            return HttpResponsePermanentRedirect(non_www_url)

        return None


SOCIAL_AUTH_STATE_COOKIE_MAX_AGE = 10 * 60
SOCIAL_AUTH_STATE_COOKIE_SALT = "twocomms.social-auth-state.v1"
SOCIAL_AUTH_STATE_COOKIE_PREFIX = "twc_oauth_state_"
SOCIAL_AUTH_STATE_PATH_RE = re.compile(
    r"^/(?:oauth|social)/(?P<action>login|complete)/(?P<backend>[-\w]+)/?$"
)


def _social_auth_state_cookie_name(backend: str) -> str:
    safe_backend = re.sub(r"[^A-Za-z0-9_]", "_", backend or "")
    return f"{SOCIAL_AUTH_STATE_COOKIE_PREFIX}{safe_backend}"


def build_social_auth_state_cookie(backend: str, state: str) -> str:
    return signing.dumps(
        {"backend": backend, "state": state},
        salt=SOCIAL_AUTH_STATE_COOKIE_SALT,
    )


class SocialAuthStateCookieMiddleware(MiddlewareMixin):
    """Short-lived double-submit cookie fallback for OAuth state.

    python-social-auth stores OAuth ``state`` only in the Django session.
    If the browser loses or rotates that session during the Google handoff,
    the callback raises AuthStateMissing before our merge pipeline can run.
    The signed cookie is not a login credential; it only lets us restore the
    exact random state value when it matches the callback parameter.
    """

    def _match_social_auth_path(self, request):
        return SOCIAL_AUTH_STATE_PATH_RE.match(getattr(request, "path", "") or "")

    def process_request(self, request):
        match = self._match_social_auth_path(request)
        if not match or match.group("action") != "complete":
            return None
        if not hasattr(request, "session"):
            return None

        backend = match.group("backend")
        session_key = f"{backend}_state"
        if request.session.get(session_key):
            return None

        request_state = request.GET.get("state") or request.GET.get("redirect_state")
        if not request_state:
            return None

        cookie_name = _social_auth_state_cookie_name(backend)
        signed_value = request.COOKIES.get(cookie_name)
        if not signed_value:
            return None

        try:
            payload = signing.loads(
                signed_value,
                salt=SOCIAL_AUTH_STATE_COOKIE_SALT,
                max_age=SOCIAL_AUTH_STATE_COOKIE_MAX_AGE,
            )
        except signing.BadSignature:
            return None

        cookie_backend = payload.get("backend") if isinstance(payload, dict) else ""
        cookie_state = payload.get("state") if isinstance(payload, dict) else ""
        if (
            cookie_backend == backend
            and cookie_state
            and constant_time_compare(str(cookie_state), str(request_state))
        ):
            request.session[session_key] = cookie_state
            request.session.modified = True
        return None

    def process_response(self, request, response):
        match = self._match_social_auth_path(request)
        if not match:
            return response
        if not hasattr(request, "session"):
            return response

        backend = match.group("backend")
        cookie_name = _social_auth_state_cookie_name(backend)
        cookie_domain = getattr(settings, "SESSION_COOKIE_DOMAIN", None)
        cookie_samesite = getattr(settings, "SESSION_COOKIE_SAMESITE", "Lax")

        if match.group("action") == "login":
            state = request.session.get(f"{backend}_state")
            if state:
                response.set_cookie(
                    cookie_name,
                    build_social_auth_state_cookie(backend, state),
                    max_age=SOCIAL_AUTH_STATE_COOKIE_MAX_AGE,
                    path="/",
                    domain=cookie_domain,
                    secure=getattr(settings, "SESSION_COOKIE_SECURE", False),
                    httponly=True,
                    samesite=cookie_samesite,
                )
        elif match.group("action") == "complete":
            response.delete_cookie(
                cookie_name,
                path="/",
                domain=cookie_domain,
                samesite=cookie_samesite,
            )
        return response


class SubdomainURLRoutingMiddleware(MiddlewareMixin):
    """
    Middleware для маршрутизации поддоменов.
    Если запрос приходит на main.domain.com, переключаем urlconf на специальный конфиг.
    """

    def process_request(self, request):
        try:
            host = request.get_host().split(":")[0].lower()
        except DisallowedHost:
            return None

        # DTF subdomain should run its own site/urlconf.
        if host.startswith('dtf.'):
            request.urlconf = 'twocomms.urls_dtf'
            return None

        # Если это management поддомен
        if host.startswith('management.'):
            request.urlconf = 'twocomms.urls_management'
            return None

        # Финансовый кабинет (fin.twocomms.shop)
        if host.startswith('fin.'):
            request.urlconf = 'twocomms.urls_fin'
            return None

        # Storage / warehouse поддомен
        if host.startswith('storage.'):
            request.urlconf = 'twocomms.urls_storage'
            return None

        # Продолжаем обычную обработку
        return None


class SubdomainRedirectFallbackMiddleware(RedirectFallbackMiddleware):
    """
    RedirectFallbackMiddleware only for the primary storefront host.

    Routed subdomains should not hit django_redirect lookups for each 404 path
    probe. Besides adding avoidable database work, that lookup can turn an
    otherwise harmless subdomain 404 into a 500 while the database is down.
    """

    PRIMARY_HOST = "twocomms.shop"

    def _is_routed_subdomain(self, host):
        return bool(host) and host.endswith(f".{self.PRIMARY_HOST}")

    def process_response(self, request, response):
        try:
            host = request.get_host().split(":")[0].lower()
        except Exception:
            raw_host = request.META.get("HTTP_HOST") or request.META.get(
                "SERVER_NAME", ""
            )
            host = raw_host.split(":")[0].lower()
        if self._is_routed_subdomain(host):
            return response
        try:
            return super().process_response(request, response)
        except DatabaseError:
            # Redirect lookups are optional; a missing URL must remain a 404
            # while the database is unavailable.
            return response


class SecurityHeadersMiddleware(MiddlewareMixin):
    """
    Middleware для установки Content-Security-Policy и связанных security-заголовков.
    """

    def process_response(self, request, response):
        csp = getattr(settings, "CONTENT_SECURITY_POLICY", None)
        if csp and not response.has_header("Content-Security-Policy"):
            response["Content-Security-Policy"] = csp

        x_xss = getattr(settings, "X_XSS_PROTECTION", None)
        if x_xss and not response.has_header("X-XSS-Protection"):
            response["X-XSS-Protection"] = x_xss

        referrer_policy = getattr(settings, "SECURE_REFERRER_POLICY", None)
        if referrer_policy and not response.has_header("Referrer-Policy"):
            response["Referrer-Policy"] = referrer_policy

        return response


class RequestTraceMiddleware(MiddlewareMixin):
    """
    Lightweight request tracing for DTF diagnostics.

    Enabled per-request via header: X-DTF-Debug: 1
    """

    def process_request(self, request):
        request._twc_trace_start = time.perf_counter()
        return None

    def process_response(self, request, response):
        started = getattr(request, "_twc_trace_start", None)
        if started is None:
            return response

        duration_ms = (time.perf_counter() - started) * 1000.0

        debug_header = request.META.get("HTTP_X_DTF_DEBUG", "")
        if str(debug_header).strip() != "1":
            return response

        try:
            host = request.get_host().split(":")[0].lower()
        except Exception:
            host = ""
        if not host.startswith("dtf."):
            return response

        response["X-App-Pid"] = str(os.getpid())
        response["X-App-Django-Ms"] = f"{duration_ms:.2f}"
        existing_server_timing = response.get("Server-Timing")
        trace_value = f"django;dur={duration_ms:.2f}"
        response["Server-Timing"] = (
            f"{existing_server_timing}, {trace_value}"
            if existing_server_timing
            else trace_value
        )
        return response


class SimpleRateLimitMiddleware(MiddlewareMixin):
    """
    Simple rate limiting middleware to prevent abuse.
    Limits requests per IP address.
    """

    def process_request(self, request):
        # Skip rate limiting in DEBUG mode
        if settings.DEBUG or not getattr(settings, 'SIMPLE_RATE_LIMIT_ENABLED', True):
            return None

        # DTF public pages are read-heavy and already protected by endpoint-level limits.
        # Skip generic IP limiter for safe methods to reduce cache I/O in hot path.
        try:
            host = request.get_host().split(':')[0].lower()
        except Exception:
            host = ''
        if host.startswith('dtf.') and request.method in ('GET', 'HEAD', 'OPTIONS'):
            return None

        # Skip static and media files (double check, though WhiteNoise handles them first now)
        path = request.path
        if path.startswith(settings.STATIC_URL) or path.startswith(settings.MEDIA_URL):
            return None

        # W3-5 (TD-023): ключ лимитера — REMOTE_ADDR, а НЕ клиентский
        # X-Forwarded-For. XFF спуфается тривиально (curl -H) → атакующий
        # получал бесконечный лимит, а подставляя чужие IP — банил жертв.
        # На проде (LiteSpeed+Passenger) REMOTE_ADDR = реальный IP клиента.
        # XFF используем ТОЛЬКО если REMOTE_ADDR — приватный адрес
        # (доверенный локальный прокси-hop), и берём последний hop.
        ip = request.META.get('REMOTE_ADDR', '')
        if ip and (ip.startswith('10.') or ip.startswith('192.168.') or
                   ip.startswith('127.') or ip.startswith('172.')):
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                # Последний элемент = добавлен ближайшим (доверенным) прокси
                ip = x_forwarded_for.split(',')[-1].strip() or ip

        # Skip if no IP
        if not ip:
            return None

        # Rate limit: 100 requests per minute per IP
        cache_key = f'ratelimit:ip:{ip}'
        current_time = int(time.time())
        window_key = f'{cache_key}:{current_time // 60}'  # 1-minute window

        try:
            request_count = cache.get(window_key, 0)

            # Check if limit exceeded
            if request_count >= 100:
                response = HttpResponse(
                    'Rate limit exceeded. Please try again later.',
                    status=429
                )
                response['Retry-After'] = '60'
                return response

            # Increment counter
            cache.set(window_key, request_count + 1, 120)  # Store for 2 minutes

        except Exception:
            # W3-5: fail-open оставлен осознанно (нельзя ронять весь сайт
            # из-за Redis), но теперь с алертным логом вместо молчания.
            import logging
            logging.getLogger('twocomms.ratelimit').warning(
                'Rate limiter cache unavailable — failing open (TD-012)'
            )

        return None
