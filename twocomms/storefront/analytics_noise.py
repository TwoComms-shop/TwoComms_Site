from __future__ import annotations

from django.db.models import Q


TRACKING_NOISE_EXACT_PATHS = frozenset(
    {
        "/api",
        "/csp-report/",
        "/buyme-feed.xml",
        "/buyme.xml",
        "/favicon.ico",
        "/favorites/count/",
        "/google-merchant-feed-v2.xml",
        "/google-merchant-feed.xml",
        "/google_merchant_feed.xml",
        "/kasta-feed.xml",
        "/kasta.xml",
        "/manifest.json",
        "/manifest.webmanifest",
        "/merchant/product-feed",
        "/products_feed.xml",
        "/prom-feed.xml",
        "/robots.txt",
        "/rozetka-feed.xml",
        "/rozetka.xml",
        "/.well-known/llms.txt",
        "/llms.txt",
        "/.well-known/llms-full.txt",
        "/llms-full.txt",
        "/sitemap.xml",
        "/site.webmanifest",
        "/static/robots.txt",
        "/sw.js",
        "/xmlrpc.php",
    }
)

TRACKING_NOISE_PATH_PREFIXES = (
    "/__debug__",
    "/accounts/telegram/",
    "/activity/pulse/",
    "/admin",
    "/admin-panel",
    "/api/",
    "/cart/summary/",
    "/favorites/check/",
    "/favorites/toggle/",
    "/invoices/api/",
    "/media/",
    "/parsing/api/",
    "/payments/monobank/webhook/",
    "/reminders/feed/",
    "/static/",
)


def normalize_tracking_path(path: str | None) -> str:
    return (path or "").strip() or "/"


def is_analytics_noise_path(path: str | None) -> bool:
    normalized = normalize_tracking_path(path)
    return normalized in TRACKING_NOISE_EXACT_PATHS or any(
        normalized.startswith(prefix) for prefix in TRACKING_NOISE_PATH_PREFIXES
    )


def analytics_noise_q(field_name: str = "path") -> Q:
    query = Q(**{f"{field_name}__in": tuple(TRACKING_NOISE_EXACT_PATHS)})
    for prefix in TRACKING_NOISE_PATH_PREFIXES:
        query |= Q(**{f"{field_name}__startswith": prefix})
    return query
