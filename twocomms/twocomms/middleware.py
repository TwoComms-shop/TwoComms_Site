"""
Дополнительные middleware для TwoComms
"""

from django.http import HttpResponsePermanentRedirect, HttpResponse
from django.conf import settings
from django.core import signing
from django.utils.deprecation import MiddlewareMixin
from django.core.cache import caches
from django.core.cache.backends.filebased import FileBasedCache
from django.core.exceptions import DisallowedHost
from django.db import DatabaseError
from django.contrib.redirects.middleware import RedirectFallbackMiddleware
from django.utils.crypto import constant_time_compare
import hashlib
import ipaddress
import logging
import os
import re
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - production and supported dev hosts are POSIX.
    fcntl = None


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


_RATE_LIMIT_DEFAULTS = {
    'auth': 20,
    'webhook': 1200,
    'telemetry': 1200,
    'staff_write': 600,
    'commerce_write': 120,
    'expensive': 120,
    'catalog': 600,
    'read': 600,
}
_RATE_LIMIT_AUTH_PATHS = {
    '/login/',
    '/register/',
    '/accounts/ajax/login/',
    '/accounts/ajax/register/',
    '/api-auth/login/',
    '/admin/login/',
}
_RATE_LIMIT_CATALOG_PREFIXES = (
    '/catalog/',
    '/product/',
    '/load-more-products/',
    '/api/products/',
    '/api/categories/',
    '/search/',
    '/blog/',
    '/sitemap',
)
_RATE_LIMIT_EXPENSIVE_READ_PREFIXES = (
    '/google-merchant',
    '/merchant/product-feed',
    '/products_feed.xml',
    '/rozetka',
    '/kasta',
    '/buyme',
    '/prom-feed.xml',
    '/media/prom-feed.xml',
)
_RATE_LIMIT_DTF_EXPENSIVE_PREFIXES = (
    '/api/quote/',
    '/status/',
    '/constructor/sessions/',
    '/cabinet/',
    '/admin-panel/',
)
_RATE_LIMIT_WEBHOOK_PREFIXES = (
    '/payments/monobank/webhook/',
    '/wholesale/payment-webhook/',
    '/accounts/telegram/webhook/',
    '/orders/dropshipper/monobank/callback/',
    '/tg-manager/webhook/',
    '/bot/webhook/',
    '/data-deletion/request/',
    '/bot/data-deletion/request/',
    '/binotel/webhook/',
    '/hooks/mono/',
    '/tg/webhook/',
)
_RATE_LIMIT_TELEMETRY_PATHS = {
    '/api/rum/',
    '/api/track-event/',
    '/api/client-error/',
    '/csp-report/',
    '/checkout/capture/',
    '/push/events/',
}
_RATE_LIMIT_STAFF_PATH_PREFIXES = (
    '/admin/',
    '/admin-panel/',
)
_RATE_LIMIT_HOST_GROUPS = {
    'twocomms.shop': 'storefront',
    'www.twocomms.shop': 'storefront',
    'dtf.twocomms.shop': 'dtf',
    'www.dtf.twocomms.shop': 'dtf',
    'management.twocomms.shop': 'management',
    'www.management.twocomms.shop': 'management',
    'fin.twocomms.shop': 'finance',
    'www.fin.twocomms.shop': 'finance',
    'storage.twocomms.shop': 'storage',
    'www.storage.twocomms.shop': 'storage',
}
_RATE_LIMIT_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
_RATE_LIMIT_STAFF_HOST_GROUPS = {'management', 'finance', 'storage'}
_RATE_LIMIT_LOGGER = logging.getLogger('twocomms.ratelimit')
_rate_limit_warning_after = 0.0
_rate_limit_warning_lock = threading.Lock()


def _rate_limit_host_group(host):
    return _RATE_LIMIT_HOST_GROUPS.get(host.lower().rstrip('.'), 'other')


def _log_rate_limit_warning(message, *, exc_info=False):
    global _rate_limit_warning_after

    now = time.monotonic()
    interval = float(getattr(settings, 'SIMPLE_RATE_LIMIT_WARNING_INTERVAL', 60))
    with _rate_limit_warning_lock:
        if now < _rate_limit_warning_after:
            return
        _rate_limit_warning_after = now + max(interval, 0)
    _RATE_LIMIT_LOGGER.warning(message, exc_info=exc_info)


def _route_rate_limit_name(request, host):
    path = re.sub(r'^/(?:uk|ru|en)(?=/)', '', request.path)
    host_group = _rate_limit_host_group(host)
    if path in _RATE_LIMIT_AUTH_PATHS and request.method not in _RATE_LIMIT_SAFE_METHODS:
        return 'auth'
    if path.startswith(_RATE_LIMIT_WEBHOOK_PREFIXES):
        return 'webhook'
    if path in _RATE_LIMIT_TELEMETRY_PATHS:
        return 'telemetry'
    if request.method not in _RATE_LIMIT_SAFE_METHODS:
        if host_group == 'dtf' and path == '/api/preflight/':
            return 'expensive'
        if (
            host_group in _RATE_LIMIT_STAFF_HOST_GROUPS
            or path.startswith(_RATE_LIMIT_STAFF_PATH_PREFIXES)
        ):
            return 'staff_write'
        return 'commerce_write'
    if host_group == 'dtf' and path.startswith(_RATE_LIMIT_DTF_EXPENSIVE_PREFIXES):
        return 'expensive'
    if path.startswith(_RATE_LIMIT_EXPENSIVE_READ_PREFIXES):
        return 'expensive'
    if path.startswith(_RATE_LIMIT_CATALOG_PREFIXES):
        return 'catalog'
    return 'read'


def _client_rate_limit_ip(request):
    remote_addr = (request.META.get('REMOTE_ADDR') or '').strip()
    if not remote_addr:
        return ''

    try:
        remote_ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return remote_addr

    trusted_networks = []
    for cidr in getattr(settings, 'SIMPLE_RATE_LIMIT_TRUSTED_PROXY_CIDRS', ()):
        try:
            trusted_networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue

    if not any(remote_ip in network for network in trusted_networks):
        return str(remote_ip)

    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if not forwarded:
        return str(remote_ip)

    forwarded_ips = []
    try:
        for value in forwarded.split(','):
            forwarded_ips.append(ipaddress.ip_address(value.strip()))
    except ValueError:
        return str(remote_ip)

    for candidate in reversed(forwarded_ips):
        if not any(candidate in network for network in trusted_networks):
            return str(candidate)
    return str(remote_ip)


def _file_rate_limit_lock(backend, key):
    if fcntl is None:
        raise RuntimeError('fcntl is required for atomic file-cache rate limiting')

    configured_dir = getattr(settings, 'SIMPLE_RATE_LIMIT_LOCK_DIR', '')
    lock_dir = (
        Path(configured_dir)
        if configured_dir
        else Path(backend._dir).parent / 'ratelimit_locks'
    )
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_dir.chmod(0o700)

    stripe = int(hashlib.sha256(key.encode('utf-8')).hexdigest()[:8], 16) % 64
    lock_path = lock_dir / f'counter-{stripe:02d}.lock'
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _increment_rate_limit_counter(key, *, timeout):
    cache_alias = getattr(settings, 'SIMPLE_RATE_LIMIT_CACHE_ALIAS', 'default')
    backend = caches[cache_alias]
    if isinstance(backend, FileBasedCache):
        descriptor = _file_rate_limit_lock(backend, key)
        locked = False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            value = backend.get(key)
            value = 1 if value is None else value + 1
            backend.set(key, value, timeout=timeout)
            return value
        finally:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    if backend.add(key, 1, timeout=timeout):
        return 1

    # A fixed-window key can expire between add() and incr(). Retry the create
    # once instead of turning that harmless boundary race into a 500/429.
    try:
        value = backend.incr(key)
    except ValueError:
        if backend.add(key, 1, timeout=timeout):
            return 1
        value = backend.incr(key)

    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeError('rate-limit cache increment did not return a counter')
    return value


class SimpleRateLimitMiddleware(MiddlewareMixin):
    """
    Route-aware fixed-window rate limiting per IP address.
    """

    def process_request(self, request):
        # Skip rate limiting in DEBUG mode
        if settings.DEBUG or not getattr(settings, 'SIMPLE_RATE_LIMIT_ENABLED', True):
            return None

        try:
            host = request.get_host().split(':')[0].lower()
        except Exception:
            host = ''
        # Skip static and media files (double check, though WhiteNoise handles them first now)
        path = request.path
        normalized_path = re.sub(r'^/(?:uk|ru|en)(?=/)', '', path)
        is_dynamic_media = normalized_path == '/media/prom-feed.xml'
        if (
            path.startswith(settings.STATIC_URL)
            or (path.startswith(settings.MEDIA_URL) and not is_dynamic_media)
        ):
            return None

        # XFF is only accepted through explicitly configured proxy CIDRs.
        ip = _client_rate_limit_ip(request)
        if not ip:
            return None

        current_time = int(time.time())
        window_seconds = int(getattr(settings, 'SIMPLE_RATE_LIMIT_WINDOW', 60))
        route_name = _route_rate_limit_name(request, host)
        configured_limits = getattr(settings, 'SIMPLE_RATE_LIMITS', {})
        route_limit = int(configured_limits.get(route_name, _RATE_LIMIT_DEFAULTS[route_name]))
        if window_seconds <= 0 or route_limit <= 0:
            _log_rate_limit_warning(
                'Rate limiter configuration invalid - failing open',
            )
            return None

        window_number = current_time // window_seconds
        retry_after = window_seconds - (current_time % window_seconds)
        host_group = _rate_limit_host_group(host)
        identity = f'{host_group}|{ip}'
        ip_digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
        window_key = f'ratelimit:{route_name}:{ip_digest}:{window_number}'

        try:
            request_count = _increment_rate_limit_counter(
                window_key,
                timeout=retry_after + 1,
            )
            if request_count > route_limit:
                response = HttpResponse(
                    'Rate limit exceeded. Please try again later.',
                    status=429
                )
                response['Retry-After'] = str(retry_after)
                return response
        except Exception:
            # W3-5: fail-open оставлен осознанно (нельзя ронять весь сайт
            # из-за Redis), но теперь с алертным логом вместо молчания.
            _log_rate_limit_warning(
                'Rate limiter cache unavailable - failing open (TD-012)',
                exc_info=True,
            )

        return None
