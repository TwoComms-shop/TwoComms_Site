from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.contrib.staticfiles.storage import staticfiles_storage
from django.views.generic.base import RedirectView
from storefront import views as storefront_views
from storefront.feeds import BlogRssFeed


# W3-5 (TD-024): API-документация — только для staff. Публичный Swagger
# раскрывал всю API-поверхность (эндпоинты, параметры, схемы) любому
# анонимному посетителю — готовая карта для атак.
def _staff_only(view_callable):
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_authenticated and request.user.is_staff):
            from django.http import Http404
            raise Http404
        return view_callable(request, *args, **kwargs)
    return wrapped


@_staff_only
def spectacular_schema_view(request, *args, **kwargs):
    from drf_spectacular.views import SpectacularAPIView

    return SpectacularAPIView.as_view()(request, *args, **kwargs)


@_staff_only
def spectacular_swagger_view(request, *args, **kwargs):
    from drf_spectacular.views import SpectacularSwaggerView

    return SpectacularSwaggerView.as_view(url_name='api-schema')(request, *args, **kwargs)


@_staff_only
def spectacular_redoc_view(request, *args, **kwargs):
    from drf_spectacular.views import SpectacularRedocView

    return SpectacularRedocView.as_view(url_name='api-schema')(request, *args, **kwargs)

# W3-2 (TECH-041): кастомный handler500 — инцидент-код в user-facing
# сообщении + маппинг кода на трейсбек в логах/Telegram-алерте.
handler500 = 'twocomms.error_views.server_error'

urlpatterns = [
    # Phase 17a — language switcher endpoint (POST). Stays outside i18n_patterns
    # so it is reachable from any prefix.
    path("i18n/", include("django.conf.urls.i18n")),

    # PWA sw.js served directly to keep scope stable across browsers.
    path("sw.js", storefront_views.service_worker_script, name="service_worker_js"),
    path("site.webmanifest", storefront_views.web_manifest, name="site_webmanifest"),

    # Phase 22d (2026-05-13) — CSP violation report endpoint. Browsers
    # POST here when a script/img/frame violates the policy. Used as a
    # tripwire to detect third-party injections that slip past code review.
    path("csp-report/", storefront_views.csp_report, name="csp_report"),

    # Root-level crawler/platform files must be resolved before storefront routes.
    path('sitemap.xml', storefront_views.custom_sitemap, name='django.contrib.sitemaps.views.sitemap'),
    # Phase 4 — sitemap-index children. /sitemap.xml is the index that points here.
    path('sitemap-static.xml', storefront_views.sitemap_section_static, name='sitemap_static'),
    path('sitemap-products.xml', storefront_views.sitemap_section_products, name='sitemap_products'),
    path('sitemap-product-variants.xml', storefront_views.sitemap_section_product_variants, name='sitemap_product_variants'),
    path('sitemap-categories.xml', storefront_views.sitemap_section_categories, name='sitemap_categories'),
    path('sitemap-blog.xml', storefront_views.sitemap_section_blog, name='sitemap_blog'),
    # RSS блогу — для Google Discover, агрегаторів і AI-краулерів (2026-06-11).
    path('blog/rss.xml', BlogRssFeed(), name='blog_rss'),
    path('sitemap-color-categories.xml', storefront_views.sitemap_section_color_categories, name='sitemap_color_categories'),
    path('sitemap-thematic.xml', storefront_views.sitemap_section_thematic, name='sitemap_thematic'),
    path('sitemap-images.xml', storefront_views.sitemap_images, name='sitemap_images'),
    path("favicon.ico", RedirectView.as_view(
        url=staticfiles_storage.url("img/favicon.ico"), permanent=False
    )),
    path("robots.txt", storefront_views.robots_txt, name="robots_txt"),
    # Fallback на случай, если где-то закешировался старый редирект на /static/robots.txt.
    path("static/robots.txt", storefront_views.robots_txt),
    path("llms.txt", storefront_views.llms_txt, name="llms_txt"),
    path(".well-known/llms.txt", storefront_views.llms_txt, name="well_known_llms_txt"),
    # llmstxt.org "full" tier — extended brand context for AI retrieval.
    # Previously /llms-full.txt returned 404 even though 404.html emitted
    # ``hreflang ru-UA`` / ``hreflang en-UA`` pointing back here, sending
    # AI crawlers in a 404 loop. Audit P0-1 (2026-05-16).
    path("llms-full.txt", storefront_views.llms_full_txt, name="llms_full_txt"),
    path(".well-known/llms-full.txt", storefront_views.llms_full_txt, name="well_known_llms_full_txt"),
    path("494cb80b2da94b4395dbbed566ab540d.txt", storefront_views.static_verification_file,
         name="static_verification_file"),
    re_path(r"^(?P<key>[A-Za-z0-9-]{8,128})\.txt$", storefront_views.indexnow_key_file, name="indexnow_key_file"),

    # ==================== API (Django REST Framework) ====================
    # REST API endpoints
    path("api/", include("storefront.api_urls")),
    # OpenAPI 3 Schema
    path('api/schema/', spectacular_schema_view, name='api-schema'),
    # Swagger UI (интерактивная документация)
    path('api/docs/', spectacular_swagger_view, name='api-docs'),
    # ReDoc (альтернативная документация)
    path('api/redoc/', spectacular_redoc_view, name='api-redoc'),
    # Browsable API auth (login/logout для DRF)
    path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),

    # Social auth - только для OAuth путей
    path('oauth/', include('social_django.urls', namespace='social')),
    path('social/', include(('social_django.urls', 'social'), namespace='social_fallback')),  # fallback для старых ссылок
    path("admin/", admin.site.urls),

    # Дропшип редирект
    path("dropshipper/", RedirectView.as_view(url="/orders/dropshipper/", permanent=False), name="dropshipper_redirect"),

    # Google Merchant feed
    path('google_merchant_feed.xml', storefront_views.google_merchant_feed, name='google_merchant_feed'),
    path('google-merchant-feed-v2.xml', storefront_views.google_merchant_feed, name='google_merchant_feed_v2_root'),
    path('merchant/product-feed', storefront_views.google_merchant_feed, name='google_merchant_feed_plain'),
    path('rozetka-feed.xml', storefront_views.rozetka_feed_xml, name='rozetka_feed_xml_root'),
    path('rozetka.xml', storefront_views.rozetka_feed_xml, name='rozetka_feed_xml_short_root'),
    path('kasta-feed.xml', storefront_views.kasta_feed_xml, name='kasta_feed_xml_root'),
    path('kasta.xml', storefront_views.kasta_feed_xml, name='kasta_feed_xml_short_root'),
    path('buyme-feed.xml', storefront_views.buyme_feed_xml, name='buyme_feed_xml_root'),
    path('buyme.xml', storefront_views.buyme_feed_xml, name='buyme_feed_xml_short_root'),
]

# Phase 17a — locale-prefixed routes. Ukrainian (LANGUAGE_CODE) stays at /,
# Russian at /ru/, English at /en/. Sitemaps, robots, feeds, API, OAuth and the
# Django admin remain unprefixed (above).
urlpatterns += i18n_patterns(
    # Storefront pages + actions (catalog/product/cart/checkout/static pages…)
    path("", include("storefront.urls")),
    # Auth + profile
    path("accounts/", include("accounts.urls")),
    # Orders (incl. dropship)
    path("orders/", include("orders.urls")),
    # Reviews — POST submit + vote endpoints.
    path("reviews/", include("reviews.urls", namespace="reviews")),
    prefix_default_language=False,
)

# Добавляем обработку медиа-файлов для разработки и продакшена
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # Для продакшена на PythonAnywhere
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
