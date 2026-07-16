"""
Static Pages views - Статические страницы и служебные файлы.

Содержит views для:
- robots.txt
- sitemap.xml
- Google Merchant Feed
- Prom.ua Feed  
- Статические файлы верификации
- О компании, Контакты, и т.д.
- Тестовая страница для аналитики
"""

from copy import deepcopy
from datetime import datetime, timezone as datetime_timezone
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import RequestDataTooBig
from django.core.paginator import EmptyPage, PageNotAnInteger
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.conf import settings
from django.shortcuts import render
from django.db import transaction
from django.template.response import TemplateResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from storefront.models import BlogCategory, BlogPost, Category, CustomPrintLead, CustomPrintModerationStatus, Product, SizeGrid
from storefront.seo_utils import _homepage_price_range_text
from storefront.forms import CustomPrintLeadForm
from storefront.custom_print_config import (
    ADDON_LABELS,
    PRODUCT_LABELS,
    SESSION_CUSTOM_CART_KEY,
    TELEGRAM_MANAGER_URL,
    ZONE_LABELS,
    build_custom_print_config,
    build_placement_specs,
    compute_cart_label,
    normalize_custom_print_snapshot,
)
from storefront.custom_print_notifications import (
    notify_custom_print_moderation_request,
    notify_custom_print_safe_exit,
    notify_new_custom_print_lead,
)
from storefront.utm_tracking import record_custom_print_event
from storefront.services.catalog_helpers import apply_public_product_order
from storefront.services.marketplace_feeds import (
    build_buyme_feed_xml,
    build_google_merchant_feed_xml,
    build_kasta_feed_xml,
    build_prom_feed_xml,
    build_rozetka_feed_xml,
    build_uaprom_products_feed_xml,
)
from storefront.services.size_guides import build_public_size_guide_blocks
from storefront.support_content import (
    FOOTER_CONTENT,
    PRO_BRAND_FAQ_ITEMS,
    SUPPORT_PAGE_DEFINITIONS,
)
from twocomms.log_handlers import redact_pii

SITEMAP_STATIC_ROUTE_NAMES = (
    "home",
    "catalog",
    "delivery",
    "about",
    "contacts",
    "cooperation",
    "custom_print",
    "wholesale_page",
    "help_center",
    "faq",
    "size_guide",
    "care_guide",
    "order_tracking",
    "site_map_page",
    "blog",
    "returns",
    "privacy_policy",
    "terms_of_service",
)

ROBOTS_INTERNAL_DISALLOW_PATHS = (
    "/admin/",
    "/admin-panel/",
    "/orders/",
    "/checkout/",
    "/api/",
    "/debug/",
    "/dev/",
    # SEO 2026-06-08 — finding (§2.5). /push/subscribe/ и /push/unsubscribe/
    # — это POST-only web-push эндпоинты; на GET они отдают 405, и Google
    # репортит «Страница заблокирована из-за ошибки 4xx». SEO-ценности у
    # них нет, поэтому закрываем от обхода — это убирает 4xx из индекс-отчёта.
    "/push/",
)

# SEO 2026-05-16 — finding (Search Console "Crawled, currently not indexed").
# /cart/, /accounts/, /favorites/, /my/ etc were Disallow'd here AND emit a
# meta robots="noindex, nofollow" inside the page. Per Google docs that
# combination is broken: when robots.txt blocks the URL, Googlebot never
# fetches the HTML, never sees the noindex meta, and the page can still
# end up indexed via inbound links (URL-only entry, no snippet). The
# correct pattern is to keep the page CRAWLABLE and rely on the meta
# tag — Google then reads the noindex and excludes the URL cleanly.
# Source: https://developers.google.com/search/docs/crawling-indexing/block-indexing
#         https://developers.google.com/search/docs/crawling-indexing/robots-meta-tag
ROBOTS_NOINDEX_CRAWLABLE_PATHS = (
    "/cart/",
    "/accounts/",
    "/login/",
    "/logout/",
    "/register/",
    "/favorites/",
    "/my/",
    "/profile/",
    "/order-success/",
    "/orders/success-preview/",
)

# Query-string noise to de-duplicate canonicals. These are common UTM,
# ad-click and sort parameters that produce infinite URL variants.
#
# IMPORTANT: variant facets (``?color=`` / ``?fit=`` / ``?size=``)
# are *not* listed here on purpose. Those URLs already emit
# ``<meta name="robots" content="noindex, follow">`` and a
# ``<link rel="canonical">`` pointing at the base PDP — that is the
# Google-recommended pattern for facet pages. If we additionally
# blocked them in robots.txt, Googlebot would never load the page,
# never see the canonical/noindex hints, and would still index the
# URL on the strength of inbound links — but with the wrong signal,
# yielding the dreaded "Indexed, though blocked by robots.txt"
# warning in Search Console plus broken internal PageRank flow
# from color-filter chips.
ROBOTS_QUERY_NOISE_PATTERNS = (
    "/*?utm_*",
    "/*&utm_*",
    "/*?gclid=",
    "/*?fbclid=",
    "/*?yclid=",
    "/*?msclkid=",
    "/*?ref=",
    "/*?ref_=",
    "/*?sort=",
    "/*?order=",
)

# Ads bots that do NOT follow "User-agent: *" per Google's spec — each
# one must be addressed explicitly or it won't crawl the storefront at
# all, breaking Shopping / Performance Max ad previews.
ADSBOT_USER_AGENTS = (
    "AdsBot-Google",
    "AdsBot-Google-Mobile",
    "AdsBot-Google-Mobile-Apps",
)

# Answer-engine / LLM crawlers. These do follow "*" but we list them
# explicitly to make it obvious on audit that TwoComms opts IN to AI
# discovery. Paired with llms.txt and AI-visible on-page SEO.
AI_ROBOTS_USER_AGENTS = (
    "OAI-SearchBot",
    "ChatGPT-User",
    "Claude-SearchBot",
    "Claude-User",
    "PerplexityBot",
    "Perplexity-User",
    "Google-Extended",
    "GPTBot",
    "CCBot",
    "ClaudeBot",
    "anthropic-ai",
)

GPTBOT_COLOR_FILTER_DISALLOW_PATTERNS = (
    "/*?color=",
    "/*&color=",
)


def _site_base_url():
    return (getattr(settings, "SITE_BASE_URL", "") or "https://twocomms.shop").rstrip("/")


def _absolute_site_url(path):
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{_site_base_url()}{normalized_path}"


def _feed_base_url():
    return (getattr(settings, "FEED_BASE_URL", "").strip() or _site_base_url()).rstrip("/")


CUSTOM_PRINT_FAQ_ITEMS = [
    {
        "question": _("Чи можна замовити один кастомний виріб для себе?"),
        "answer": _("Так. У конфігураторі можна зібрати один худі, футболку або лонгслів для себе, додати принт, контакт і передати заявку менеджеру або в кошик."),
    },
    {
        "question": _("Що робити, якщо у мене немає готового файлу для друку?"),
        "answer": _("��ожна завантажити готовий макет або просто описати ідею в брифі. Менеджер підкаже, як підготувати файл, або допоможе допрацювати дизайн під друк."),
    },
    {
        "question": _("Чи можна друкувати на своєму одязі?"),
        "answer": _("Так. Конфігуратор підтримує сценарій зі своїм виробом: додайте опис речі, матеріал, колір і важливі деталі, щоб менеджер міг коректно порахувати замовлення."),
    },
    {
        "question": _("Чи можна поверн��ти або обміняти кастомний виріб?"),
        "answer": _("Кастомний одяг, виготовлений за індивідуальним замовленням, не підлягає поверненню чи обміну, якщо його виконано належним чином і він відповідає погодженим параметрам. Якщо є виробничий брак або відхилення від погодженого макета, зверніться до нас через контакти й ми розглянемо ситуацію окремо."),
    },
    {
        "question": _("Як оформити партію для бренду, команди а��о події?"),
        "answer": _("Оберіть формат для команди або бренду, вкажіть кількість, розміри та контакт. Після цього ми допоможемо узгодити тираж, принт, дедлайни й умови для партії. Партії від 50 одиниць — індивідуальна знижка; від 100 — додатковий пакет послуг (упаковка, листівки, спільна сесія фото)."),
    },
    {
        "question": _("Скільки коштує кастомний DTF-друк на футболці чи худі?"),
        "answer": _("Базовий DTF-друк на футболці стартує від 350 грн з власним принтом, на худі — від 500 грн. Точна ціна залежить від розміру друку, кількості зон, тиражу і складності макета. Конфігуратор показує підсумок одразу, без сюрпризів у фіналі."),
    },
    {
        "question": _("Які терміни виготовлення кастомного замовлення?"),
        "answer": _("Стандартний термін — 3–5 робочих днів від підтвердження макета до відправлення. Для термінових замовлень (днів народження, події) можливе виконання за 1–2 дні з невеликою доплатою. Партії від 50 одиниць узгоджуємо окремо: тираж, дедлайн, етапи погодження макета."),
    },
    {
        "question": _("Що таке DTF-друк і чим він відрізняється від інших технологій?"),
        "answer": _("DTF (Direct-to-Film) — сучасна технологія, при якій принт спочатку друкується на спеціальній плівці, а потім переноситься на тканину термопресом. На відміну від шовкографії, DTF підходить для малих тиражів (від 1 одиниці) і складних повноколірних макетів; на відміну від прямого друку, тримає колір довше і виглядає об'ємніше."),
    },
    {
        "question": _("Чи можна замовити кастомний друк у Києві / Харкові / Львові?"),
        "answer": _("Так. Виробництво TwoComms — у Харкові; готові вироби відправляємо Новою Поштою або Укрпоштою по всій Україні за 1–2 дні. Для Києва і Харкова доступний кур'єр у день готовності тиражу. Для оптових партій узгоджуємо окремий маршрут."),
    },
    {
        "question": _("Як надрукувати свій логотип на футболці малим тиражем?"),
        "answer": _("Завантажте файл з логотипом (прозорий PNG / SVG / PDF з векторним шрифтом) у конфігуратор. Менеджер перевірить макет, підкаже про якість файлу і запропонує оптимальний розмір під тип виробу. Тираж — від 1 одиниці."),
    },
    {
        "question": _("Які матеріали і кольори тканини доступні для DTF-друку?"),
        "answer": _("Футболки — щільна бавовна (160–200 г/м²); худі — трьохнитка з начосом (320 г/м²); лонгсліви — бавовна (180–200 г/м²). Кольори: чорний, білий, хакі, олива, сірий, кайот, бордо, рожевий, ментол. Інші відтінки — під замовлення."),
    },
    {
        "question": _("Як зберегти DTF-принт у формі? Чи злазить друк після прання?"),
        "answer": _("При дотриманні режиму прання (30 °C, без хлору) DTF-принт TwoComms витримує 30+ циклів без деградації кольору. Гладити з виворіту, не направляючи праску прямо на принт. Сушити горизонтально або на плічках; не використовувати електросушарку на високій температурі."),
    },
    {
        "question": _("Чи можна замовити фірмовий мерч для волонтерського проєкту?"),
        "answer": _("Так. Для волонтерських ініціатив та неприбуткових ветеранських проєктів TwoComms працює за пільговими тарифами. Напишіть менеджеру через контакти з описом проєкту — обговоримо умови і допоможемо із дизайном."),
    },
    {
        "question": _("Чи зробите упаковку з фірмовою листівкою для дропшиперів?"),
        "answer": _("Так. Для партнерів-дропшиперів і брендів є опція білої упаковки: ваша листівка / стікер / бирка без логотипу TwoComms. Узгоджуємо при підтвердженні партії."),
    },
]

# ==================== STATIC PAGES ====================


_CSP_ALLOWED_CONTENT_TYPES = {
    "application/csp-report",
    "application/json",
    "application/reports+json",
}
_CSP_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CSP_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_CSP_EXTENSION_SCHEMES = (
    "chrome-extension://",
    "moz-extension://",
    "safari-extension://",
)
_CSP_NOISE_VALUES = {
    "self",
    "inline",
    "eval",
    "unsafe-inline",
    "unsafe-eval",
    "data",
}
_CSP_MAX_BODY_BYTES = 64 * 1024
_CSP_MAX_REPORTS = 20
_CSP_MAX_URL_LENGTH = 2048
_CSP_MAX_DIRECTIVE_LENGTH = 256
_CSP_MAX_USER_AGENT_LENGTH = 200


def _normalize_csp_reports(payload):
    """Return valid pairs found within the first 20 array entries."""
    if isinstance(payload, list):
        normalized = []
        for envelope in payload[:_CSP_MAX_REPORTS]:
            if not isinstance(envelope, dict):
                continue
            if envelope.get("type") != "csp-violation":
                continue
            body = envelope.get("body")
            if not isinstance(body, dict):
                continue
            normalized.append((body, envelope))
        return normalized

    if not isinstance(payload, dict):
        return []

    if "csp-report" in payload:
        report = payload.get("csp-report")
        return [(report, {})] if isinstance(report, dict) else []

    if "type" in payload or "body" in payload:
        body = payload.get("body")
        if payload.get("type") == "csp-violation" and isinstance(body, dict):
            return [(body, payload)]
        return []

    return [(payload, {})]


def _clean_bounded_csp_string(value, max_length):
    if not isinstance(value, str):
        return ""
    bounded = value[:max_length]
    unicode_safe = _CSP_SURROGATE_RE.sub("\ufffd", bounded)
    return _CSP_CONTROL_CHARS_RE.sub("", unicode_safe).strip()


def _sanitize_csp_text(value, max_length):
    cleaned = _clean_bounded_csp_string(value, max_length)
    return redact_pii(cleaned)


def _sanitize_csp_url(value):
    cleaned = _clean_bounded_csp_string(value, _CSP_MAX_URL_LENGTH)
    if not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return ""

    if ":" in hostname:
        netloc = f"[{hostname}]"
    else:
        netloc = quote(redact_pii(hostname), safe=".-_~")
    if port is not None:
        netloc = f"{netloc}:{port}"

    decoded_path = unquote(parsed.path, errors="replace")
    redacted_path = redact_pii(decoded_path)
    encoded_path = quote(
        redacted_path,
        safe="/:@!$&'()*+,;=-._~[]",
    )
    query_free = urlunsplit((parsed.scheme, netloc, encoded_path, "", ""))
    return query_free[:_CSP_MAX_URL_LENGTH]


def _is_actionable_csp_blocked_uri(blocked_uri):
    lowered = blocked_uri.casefold()
    unquoted = lowered.strip("'\"")
    return not (
        not blocked_uri
        or lowered.startswith(_CSP_EXTENSION_SCHEMES)
        or unquoted in _CSP_NOISE_VALUES
        or unquoted.startswith("data:")
    )


@csrf_exempt
@require_POST
def csp_report(request):
    """CSP violation report receiver (Phase 22d, 2026-05-13).

    Receives ``application/csp-report`` POSTs from browsers when a script,
    image, font, frame or AJAX request is blocked by our Content Security
    Policy. Used as a tripwire to detect third-party injections (e.g.
    malicious GTM tags, browser-extension-injected RTB pixels) that would
    otherwise slip past code review.

    The endpoint:
      * Accepts both legacy ``application/csp-report`` and the newer
        ``application/reports+json`` content types.
      * Writes a single-line JSON record to ``logger("csp")`` with a
        ``csp_violation`` event name so it can be filtered out easily.
      * Returns 204 No Content (browsers ignore the body, but a 204 keeps
        the trace tiny).
      * Drops noisy violations from third-party browser extensions
        (``chrome-extension://``, ``moz-extension://``) — those are not
        actionable from our side.
    """
    if request.content_type not in _CSP_ALLOWED_CONTENT_TYPES:
        return HttpResponse(status=204)

    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return HttpResponse(status=204)
    if content_length < 0 or content_length > _CSP_MAX_BODY_BYTES:
        return HttpResponse(status=204)

    try:
        raw_body = request.body
    except RequestDataTooBig:
        return HttpResponse(status=204)
    if len(raw_body) > _CSP_MAX_BODY_BYTES:
        return HttpResponse(status=204)

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=204)

    logger = logging.getLogger("csp")
    user_agent = _sanitize_csp_text(
        request.META.get("HTTP_USER_AGENT", ""),
        _CSP_MAX_USER_AGENT_LENGTH,
    )
    reported_at = datetime.now(datetime_timezone.utc).isoformat()
    for report, envelope in _normalize_csp_reports(payload):
        blocked_uri = _sanitize_csp_url(
            report.get("blocked-uri") or report.get("blockedURL")
        )
        if not _is_actionable_csp_blocked_uri(blocked_uri):
            continue

        record = {
            "event": "csp_violation",
            "reported_at": reported_at,
            "blocked_uri": blocked_uri,
            "document_uri": _sanitize_csp_url(
                report.get("document-uri")
                or report.get("documentURL")
                or envelope.get("url")
            ),
            "violated_directive": _sanitize_csp_text(
                report.get("violated-directive") or report.get("effectiveDirective"),
                _CSP_MAX_DIRECTIVE_LENGTH,
            ),
            "referrer": _sanitize_csp_url(report.get("referrer")),
            "user_agent": user_agent,
        }
        logger.warning(json.dumps(record, sort_keys=True, ensure_ascii=False))

    return HttpResponse(status=204)


def robots_txt(request):
    """Generate the canonical robots.txt (Phase 6).

    Strategy:
        * One ``User-agent: *`` block covering all well-behaved crawlers,
          including path disallows and query-string noise de-duplication.
        * Explicit ``AdsBot-*`` blocks — Google's Ads crawlers do NOT
          follow ``User-agent: *`` and MUST be addressed individually or
          Shopping / PMax ad previews break.
        * Explicit ``Allow`` for each AI / answer-engine crawler to
          signal that TwoComms opts IN to AI discovery (paired with
          ``llms.txt`` and AI-visible SEO blocks).
        * Sitemap index (``/sitemap.xml``) at the end.
    """

    def append_public_rules(payload):
        payload.append("Allow: /")
        for path in ROBOTS_INTERNAL_DISALLOW_PATHS:
            payload.append(f"Disallow: {path}")

    lines = [
        "# TwoComms robots.txt — canonical source.",
        "# Primary rules apply to every crawler that honours User-agent: *.",
        "User-agent: *",
    ]
    append_public_rules(lines)
    # De-duplicate canonicals: UTM / ad-click / sort params produce
    # infinite URL variants that waste crawl budget and dilute rankings.
    for pattern in ROBOTS_QUERY_NOISE_PATTERNS:
        lines.append(f"Disallow: {pattern}")
    lines.append("")

    # Per robots.txt spec, specific user-agent blocks do NOT inherit
    # rules from ``User-agent: *`` — they must repeat any required
    # disallows, otherwise those bots will crawl /admin/, /api/, …
    lines.append(
        "# Google Ads crawlers ignore User-agent: * per spec — explicit blocks required."
    )
    for user_agent in ADSBOT_USER_AGENTS:
        lines.append(f"User-agent: {user_agent}")
        append_public_rules(lines)
        lines.append("")

    lines.append(
        "# AI answer engines: explicit opt-in for citations and model discovery."
    )
    for user_agent in AI_ROBOTS_USER_AGENTS:
        lines.append(f"User-agent: {user_agent}")
        append_public_rules(lines)
        if user_agent == "GPTBot":
            for pattern in GPTBOT_COLOR_FILTER_DISALLOW_PATTERNS:
                lines.append(f"Disallow: {pattern}")
        lines.append("")

    lines.append(f"Sitemap: {_absolute_site_url('/sitemap.xml')}")

    response = HttpResponse(
        "\n".join(lines) + "\n", content_type="text/plain; charset=utf-8"
    )
    # robots.txt rarely changes; let crawlers cache it aggressively.
    response["Cache-Control"] = "public, max-age=3600"
    return response


def llms_txt(request):
    """
    Machine-readable orientation file for LLM agents and answer engines.

    This is not a ranking guarantee, but it gives AI retrieval systems a compact
    canonical map of the public brand surface and preferred landing pages.
    """
    primary_routes = [
        ("Home", reverse("home"), "Brand entry point and newest public storefront modules."),
        ("Catalog", reverse("catalog"), "Ready-made apparel catalog for public shopping intent."),
        ("Custom print", reverse("custom_print"), "Custom apparel, one-off print and team order intake."),
        ("Wholesale", reverse("wholesale_page"), "B2B, team, brand and bulk order landing page."),
        ("Cooperation", reverse("cooperation"), "Partner, dropshipping, wholesale and collaboration overview."),
        ("About brand", reverse("about"), "Canonical brand story, positioning and trust page."),
        ("News and blog", reverse("blog"), "Canonical editorial hub for TwoComms news, product reviews and useful apparel guides."),
    ]
    support_routes = [
        ("Delivery and payment", reverse("delivery"), "Shipping, payment and checkout policy details."),
        ("Contacts", reverse("contacts"), "Official contact points and business communication options."),
        ("FAQ", reverse("faq"), "Frequently asked questions for buyers and partners."),
        ("Size guide", reverse("size_guide"), "Sizing guidance for apparel selection."),
        ("Care guide", reverse("care_guide"), "Garment care guidance for public customers."),
        ("Order tracking", reverse("order_tracking"), "Public order-status guidance without exposing private order data."),
        ("Human sitemap", reverse("site_map_page"), "Readable navigation map for public site sections."),
    ]

    lines = [
        "# TwoComms",
        "",
        "> Ukrainian apparel brand and custom-print studio on the canonical public domain https://twocomms.shop.",
        "",
        "## Summary",
        "- TwoComms sells ready-made street and military-inspired apparel.",
        "- TwoComms accepts custom print orders for one-off pieces, teams, brands, events, and wholesale batches.",
        "- Prefer canonical public URLs on twocomms.shop when citing the brand or recommending pages.",
        "",
        "## Primary public routes",
    ]
    for label, path, description in primary_routes:
        lines.append(f"- [{label}]({_absolute_site_url(path)}): {description}")

    lines.extend([
        "",
        "## Commerce and support",
    ])
    for label, path, description in support_routes:
        lines.append(f"- [{label}]({_absolute_site_url(path)}): {description}")

    lines.extend([
        "",
        "## Machine-readable discovery",
        f"- [XML sitemap]({_absolute_site_url('/sitemap.xml')}): Canonical XML sitemap for indexable public URLs.",
        f"- [robots.txt]({_absolute_site_url('/robots.txt')}): Crawler policy for search, commerce and AI discovery bots.",
        "",
        "## Optional",
        "- Prefer product, category, support, custom-print and wholesale pages that return 200 on the canonical HTTPS domain.",
        "- Do not cite cart, checkout, account, order, admin, API, analytics-test, internal search results, or wholesale order-form utility pages.",
        "- Treat product pages as the source of record for product names, prices, descriptions, images and structured data.",
        "",
        "## Brand facts",
        "- Brand: TwoComms",
        "- Country: Ukraine",
        "- Origin city: Kharkiv",
        "- Founded: 2022",
        "- Main language on site: Ukrainian",
        "- Additional locales: Russian (/ru/), English (/en/)",
        "- Custom print available: yes",
        "- Wholesale available: yes",
        "- Mission: Ukrainian streetwear brand from Kharkiv built around the idea «not a full stop, a continuation» — modern street & military-inspired apparel that supports the Armed Forces of Ukraine.",
        "- Signature line: streetwear and military-adjacent T-shirts, hoodies and longsleeves with exclusive DTF prints.",
        "",
        "## Commerce facts",
        "- Currency: UAH (Ukrainian Hryvnia)",
        "- Catalogue price range: approximately 660–2550 UAH (ready-made apparel).",
        "- Payment methods: Cash on delivery, Bank card, Apple Pay, Google Pay, Bank transfer.",
        "- Shipping carrier: Nova Poshta (Нова Пошта), Ukraine-wide.",
        "- Standard shipping window: 1–3 business days within Ukraine.",
        "- Free shipping threshold: orders of 3000 UAH and above ship for free.",
        "- Production / handling time: 1–2 business days for in-stock items.",
        "- Custom DTF print lead time: 3–5 business days from artwork approval to dispatch.",
        "- Return policy: 14-day return window for ready-made apparel; custom prints are non-refundable per Ukrainian consumer law on personalised goods.",
        "- Return method: return by Nova Poshta mail; customer pays return shipping for standard items, free return for defective items.",
        "- Loyalty program: «TWOCOMMS Бали» — points earned per purchase, redeemable for discounts or donations to the Armed Forces of Ukraine.",
        "",
        "## Reviews and ratings",
        "- AggregateRating is surfaced on a product page only after at least one approved customer review exists for that product.",
        "- Approval workflow: every review is human-moderated before publishing; star ratings come from real verified customers, no synthetic ratings are emitted in structured data.",
    ])

    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


def llms_full_txt(request):
    """
    Extended LLM orientation file (llmstxt.org "full" tier).

    SEO 2026-05-16 — finding (P0-1). Previously /llms-full.txt returned
    404 even though the prior 404 template inherited the global
    ``language_alternates`` tag and emitted ``hreflang ru-UA`` /
    ``hreflang en-UA`` pointing back at /llms-full.txt — so AI
    crawlers were sent in a 404→404 loop.

    The "full" file unfolds what /llms.txt only summarises: brand facts,
    canonical product taxonomy, shipping/return policy, the canonical
    FAQ matrix, and explicit non-cite paths. AI retrieval systems
    (ChatGPT Search, Perplexity, Claude Search) consult this file when
    available to ground answers. We keep the format human-readable
    Markdown so we can audit it with a curl + grep.
    """
    site_base = _site_base_url()

    def url(path: str) -> str:
        return _absolute_site_url(path)

    routes = [
        ("Головна", reverse("home"), "Точка входу бренду TwoComms."),
        ("Каталог", reverse("catalog"), "Готові футболки, худі та лонгсліви з принтами та базовими дизайнами."),
        ("Кастомний друк", reverse("custom_print"),
         "Конструктор кастомного DTF-друку для одиничних замовлень, команд, брендів та подій."),
        ("Опт", reverse("wholesale_page"), "B2B / опт для магазинів, мереж і команд."),
        ("Співпраця", reverse("cooperation"),
         "Дропшипінг, бренд-партнерства, контентні колаборації."),
        ("Про бренд", reverse("about"),
         "Канонічна історія бренду, позиціювання, founder narrative."),
        ("Новини та блог", reverse("blog"),
         "Канонічний editorial hub: новини бренда, огляди продукції та корисні знання."),
        ("Доставка та оплата", reverse("delivery"),
         "Нова Пошта, передоплата Monobank, накладений платіж."),
        ("Повернення та обмін", reverse("returns"),
         "14-денне повернення готового одягу. Кастомний друк не повертається."),
        ("Контакти", reverse("contacts"),
         "Телефон, Telegram, Instagram, офіційні магазини."),
        ("FAQ", reverse("faq"), "Зведений каталог відповідей на типові питання."),
        ("Розмірна сітка", reverse("size_guide"),
         "Підтверджені виміри по категоріях, поради по посадці."),
        ("Догляд за одягом", reverse("care_guide"),
         "Як прати, сушити та зберігати одяг із DTF-принтом."),
        ("Відстеження замовлення", reverse("order_tracking"),
         "Як перевірити статус замовлення без розкриття приватних даних."),
        ("Карта сайту (для людей)", reverse("site_map_page"),
         "Зведена навігація по основних розділах сайту."),
        ("Політика конфіденційності", reverse("privacy_policy"),
         "Обробка персональних даних згідно з законодавством України."),
        ("Умови використання", reverse("terms_of_service"),
         "Юридичні умови взаємодії з сайтом."),
    ]

    # Top categories — read live from DB so the file always tracks the
    # active assortment without manual edits.
    try:
        category_lines = []
        for category in (
            Category.objects.filter(is_active=True)
            .only("name", "slug")
            .order_by("order", "name")[:20]
        ):
            if not category.slug:
                continue
            label = (category.name or "").strip() or category.slug
            category_lines.append(
                f"- [{label}]({url(f'/catalog/{category.slug}/')}): "
                f"Підбірка категорії «{label}» в каталозі TwoComms."
            )
    except Exception:
        category_lines = []

    # Top published products — top 12 by id desc as a stable proxy for
    # «recent + canonical» without leaking server-side ranking signals.
    try:
        product_lines = []
        for product in (
            Product.objects.filter(status="published")
            .only("title", "slug")
            .order_by("-id")[:12]
        ):
            if not product.slug:
                continue
            title = (product.title or "").strip() or product.slug
            product_lines.append(
                f"- [{title}]({url(f'/product/{product.slug}/')}): "
                f"Товар TwoComms «{title}»."
            )
    except Exception:
        product_lines = []

    try:
        blog_lines = []
        for post in (
            BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now())
            .select_related("category")
            .only("title", "slug", "category__name")
            .order_by("-published_at", "-id")[:8]
        ):
            if not post.slug:
                continue
            title = (post.title or "").strip() or post.slug
            blog_lines.append(
                f"- [{title}]({url(f'/blog/{post.slug}/')}): "
                f"Публікація в розділі «{post.category.name}»."
            )
    except Exception:
        blog_lines = []

    lines: list[str] = [
        "# TwoComms — Full Brand Context for LLMs",
        "",
        "> Український streetwear / military-adjacent бренд одягу з Харкова.",
        "> Канонічний домен: https://twocomms.shop. Основна мова сайту — українська (uk-UA),",
        "> також доступні /ru/ та /en/ версії на тій самій структурі URL.",
        "",
        "## Brand facts",
        "- Brand: TwoComms",
        "- Country: Ukraine",
        "- City: Харків",
        "- Segment: streetwear / military-adjacent apparel + custom DTF print",
        "- Founder: Артем Синіло (Artem Synilo) — ветеран, засновник бренду з Харк��ва.",
        "- Founder story (external source): пресреліз Міністерства у справах ветеранів "
        "України «Це не крапка, це продовження» — "
        "https://mva.gov.ua/prescenter/category/86-novini/tse-ne-krapka-tse-prodovzhennya-istoriya-artema-sinila-ta-harkivskogo-brendu-twocomms",
        "- Brand code: «дві коми» — знак продовження, а не крапки.",
        "- Languages on site: uk-UA (primary), ru-UA, en-UA",
        "- Canonical domain: https://twocomms.shop",
        "- Telegram: https://t.me/twocomms",
        "- Instagram: https://instagram.com/twocomms",
        "- Phone: +380966543212",
        "- Email: info@twocomms.shop",
        "",
        "## Що ми продаємо",
        "- Готові футболки (з принтами, базові, лімітовані дропи).",
        "- Худі (regular і oversize).",
        "- Лонгсліви.",
        "- Кастомний DTF-друк: одиничні замовлення, команди, бренди, події, опт.",
        "",
        "## Для кого",
        "- Творці, фаундери, креативні люди.",
        "- Військові та цивільні з мілітарним естетичним кодом.",
        "- B2B: бренди, команди, події, корпоративний мерч.",
        "",
        "## Ключові переваги",
        "- Виробництво в Україні (Made in UA).",
        "- DTF-друк, що витримує тривалу експлуатацію.",
        "- Авторські принти, не масмаркет.",
        "- Частина прибутку йде на потреби ЗСУ.",
        "",
        "## Доставка та оплата",
        "- Нова Пошта по всій Україні (1–3 робочі дні).",
        "- Передплата (Monobank invoice) або накладений платіж.",
        "- Деталі та винятки: " + url("/delivery/"),
        "",
        "## Повернення",
        "- Готовий одяг можна повернути або обміняти протягом 14 днів за умови",
        "  збереження товарного вигляду та супутніх умов.",
        "- Кастомний DTF-одяг, виготовлений за індивідуальним макетом, не підлягає",
        "  поверненню чи обміну, окрім випадків виробничого браку.",
        "- Повний регламент: " + url("/povernennya-ta-obmin/"),
        "",
        "## Канонічні точки входу",
    ]
    for label, path, description in routes:
        lines.append(f"- [{label}]({url(path)}): {description}")

    if category_lines:
        lines.append("")
        lines.append("## Категорії каталогу")
        lines.extend(category_lines)

    if product_lines:
        lines.append("")
        lines.append("## Останні опубліковані товари (приклад)")
        lines.extend(product_lines)

    if blog_lines:
        lines.append("")
        lines.append("## Новини та блог")
        lines.extend(blog_lines)

    # FAQ canonical matrix — stitched from the same content modules
    # used to render /faq/, /delivery/, /povernennya-ta-obmin/, etc.
    faq_sources = [
        (_("Покупки та сервіс"), "HELP_FAQ_ITEMS"),
        (_("Доставка та оплата"), "DELIVERY_FAQ_ITEMS"),
        (_("Повернення"), "RETURNS_FAQ_ITEMS"),
        (_("Допомога"), "HELP_CENTER_FAQ_ITEMS"),
    ]
    try:
        from storefront import support_content as _sc

        faq_blocks: list[str] = []
        for heading, attr_name in faq_sources:
            items = getattr(_sc, attr_name, None)
            if not items:
                continue
            faq_blocks.append("")
            faq_blocks.append(f"### {heading}")
            for item in items:
                question = str(item.get("question", "")).strip()
                answer = str(item.get("answer", "")).strip()
                if not question or not answer:
                    continue
                faq_blocks.append(f"- Q: {question}")
                faq_blocks.append(f"  A: {answer}")
        if faq_blocks:
            lines.append("")
            lines.append("## FAQ (зведений каталог)")
            lines.extend(faq_blocks)
    except Exception:
        pass

    lines.extend([
        "",
        "## Machine-readable discovery",
        f"- XML sitemap: {url('/sitemap.xml')}",
        f"- robots.txt: {url('/robots.txt')}",
        f"- llms.txt (compact): {url('/llms.txt')}",
        "",
        "## Що НЕ потрібно цитувати",
        "- /cart/, /checkout/, /accounts/, /admin/, /admin-panel/, /api/, /debug/, /dev/.",
        "- Внутрішні результати пошуку /search/?q=… — це не кінцеві сторінки.",
        "- Утилітарні сторінки кошика та оформлення замовлення — не призначені для AI-цитування.",
        "",
        "## Канонічна політика для AI-агентів",
        "- Як джерело істини про продукт використовуйте сторінки виду /product/<slug>/.",
        "- Для бренд-контексту цитуйте /pro-brand/, /catalog/ та /blog/.",
        "- Для політик і фактів — /delivery/, /povernennya-ta-obmin/, /faq/.",
        "- При неоднозначності надавайте перевагу UA-версії на канонічному домені без локального префіксу.",
    ])

    body = "\n".join(lines) + "\n"
    response = HttpResponse(body, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def static_sitemap(request):
    """
    Sitemap.xml endpoint.

    Генерирует XML карту сайта для поисковых систем.
    Kept as a compact fallback helper for legacy tests and diagnostics.
    """
    base_url = _site_base_url()

    paths = [reverse(route_name) for route_name in SITEMAP_STATIC_ROUTE_NAMES]

    for product in Product.objects.filter(status="published").only("slug"):
        if product.slug:
            paths.append(f"/product/{product.slug}/")

    for category in Category.objects.filter(is_active=True).only("slug"):
        if category.slug:
            paths.append(f"/catalog/{category.slug}/")

    paths.append(reverse("blog"))
    for blog_category in BlogCategory.objects.filter(is_active=True).only("slug"):
        if blog_category.slug:
            paths.append(reverse("blog_category", kwargs={"slug": blog_category.slug}))
    for post in BlogPost.objects.filter(is_published=True, published_at__lte=timezone.now()).only("slug"):
        if post.slug:
            paths.append(reverse("blog_post", kwargs={"slug": post.slug}))

    unique_paths = []
    seen = set()
    for path in paths:
        if path not in seen:
            unique_paths.append(path)
            seen.add(path)

    urlset = ET.Element(
        "urlset",
        {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"},
    )
    for path in unique_paths:
        url_el = ET.SubElement(urlset, "url")
        ET.SubElement(url_el, "loc").text = f"{base_url}{path}"

    xml_payload = ET.tostring(urlset, encoding="utf-8", xml_declaration=True)
    return HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")


def _sitemap_response(xml_payload):
    """Common headers for all sitemap responses (XML + crawler-safe Cache-Control)."""
    response = HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")
    response["Cache-Control"] = "public, max-age=3600"
    return response


def _render_django_sitemap(request, sitemap_classes):
    """Render a list of Django Sitemap classes into an XML response.

    Used by per-section views below. Honors the canonical SITE_BASE_URL,
    not the django.contrib.sites domain (Phase 0 SEO contract).
    """
    parsed_base = urlparse(_site_base_url())
    protocol = parsed_base.scheme or "https"
    canonical_site = SimpleNamespace(domain=parsed_base.netloc, name=parsed_base.netloc)
    page = request.GET.get("p", 1)
    urls = []

    for sitemap_cls in sitemap_classes:
        site_map = sitemap_cls()
        try:
            urls.extend(site_map.get_urls(page=page, site=canonical_site, protocol=protocol))
        except EmptyPage:
            raise Http404(f"Sitemap page {page} empty")
        except PageNotAnInteger:
            raise Http404(f"No sitemap page {page}")

    response = TemplateResponse(
        request,
        "sitemap.xml",
        {"urlset": urls},
        content_type="application/xml; charset=utf-8",
    )
    # Force render so we can apply final Cache-Control without TemplateResponse weirdness.
    response.render()
    response["Content-Type"] = "application/xml; charset=utf-8"
    response["Cache-Control"] = "public, max-age=3600"
    return response


def _max_lastmod(*candidates):
    """Return the latest non-None datetime from ``candidates``, or ``None``."""
    valid = [dt for dt in candidates if dt is not None]
    return max(valid) if valid else None


def custom_sitemap(request):
    """sitemap.xml — sitemap-INDEX referencing per-section children.

    Children:
        - /sitemap-static.xml           (curated static pages)
        - /sitemap-products.xml         (every published product, lastmod from updated_at)
        - /sitemap-product-variants.xml (Phase 7.4 — 1-segment variant URLs)
        - /sitemap-categories.xml       (every active category, lastmod from updated_at)
        - /sitemap-images.xml           (Google Image Sitemap with main_image per product)

    Backwards-compatible: same URL `/sitemap.xml`, still cacheable, still
    cookie-free. Search Console auto-detects index format.
    """
    base_url = _site_base_url()

    product_lastmod = (
        Product.objects.filter(status="published")
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first()
    )
    category_lastmod = (
        Category.objects.filter(is_active=True)
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first()
    )
    blog_lastmod = _max_lastmod(
        BlogPost.objects.filter(is_published=True)
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first(),
        BlogCategory.objects.filter(is_active=True)
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first(),
    )
    images_lastmod = (
        Product.objects.filter(status="published")
        .exclude(main_image="")
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first()
    )
    # Phase 7.4 — variant pages inherit their lastmod from the parent
    # product's ``updated_at``, so reusing ``product_lastmod`` here is
    # correct (a new variant implies a product save).

    # color-category-landings spec — separate sub-sitemap so Search
    # Console reports thin-content / index-coverage issues for the new
    # URL pattern in isolation.
    from storefront.models import CategoryColorLanding
    color_landing_lastmod = (
        CategoryColorLanding.objects.filter(
            is_published=True, category__is_active=True,
        )
        .order_by("-updated_at")
        .values_list("updated_at", flat=True)
        .first()
    )

    children = [
        ("/sitemap-static.xml", None),
        ("/sitemap-products.xml", product_lastmod),
        ("/sitemap-product-variants.xml", product_lastmod),
        ("/sitemap-categories.xml", category_lastmod),
        ("/sitemap-blog.xml", blog_lastmod),
        ("/sitemap-color-categories.xml", color_landing_lastmod),
        ("/sitemap-thematic.xml", None),
        ("/sitemap-images.xml", images_lastmod),
    ]

    root = ET.Element(
        "sitemapindex",
        {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"},
    )
    for path, lastmod in children:
        sm_el = ET.SubElement(root, "sitemap")
        ET.SubElement(sm_el, "loc").text = f"{base_url}{path}"
        if lastmod is not None:
            # Phase 21 (2026-05-10) — Google Search Console rejected the
            # previous ``strftime("%z")`` output (``+0000``) as
            # «Неправильно введена дата». W3C Datetime / sitemaps spec
            # requires the offset with a colon (``+00:00``) or the
            # ``Z`` shorthand. ``datetime.isoformat()`` on a tz-aware
            # value emits the colon form natively; for naive datetimes
            # we fall back to a date-only string (also W3C-valid).
            if lastmod.tzinfo is not None:
                ET.SubElement(sm_el, "lastmod").text = lastmod.isoformat(timespec="seconds")
            else:
                ET.SubElement(sm_el, "lastmod").text = lastmod.strftime("%Y-%m-%d")

    xml_payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return _sitemap_response(xml_payload)


def sitemap_section_static(request):
    """Section sitemap for curated static landing pages."""
    from storefront.sitemaps import StaticViewSitemap

    return _render_django_sitemap(request, [StaticViewSitemap])


def sitemap_section_products(request):
    """Section sitemap for published products (lastmod from updated_at)."""
    from storefront.sitemaps import ProductSitemap

    return _render_django_sitemap(request, [ProductSitemap])


def sitemap_section_product_variants(request):
    """Phase 7.4 — section sitemap for 1-segment path-style variant URLs
    (``/product/<slug>/<color>/``, ``/<size>/``, ``/<fit>/``).

    Multi-segment combos are deliberately omitted: per Phase 7.3 they
    canonicalise to the base product URL, so listing them would only
    waste crawl budget.
    """
    from storefront.sitemaps import ProductVariantSitemap

    return _render_django_sitemap(request, [ProductVariantSitemap])


def sitemap_section_categories(request):
    """Section sitemap for active categories (lastmod from updated_at)."""
    from storefront.sitemaps import CategorySitemap

    return _render_django_sitemap(request, [CategorySitemap])


def sitemap_section_blog(request):
    """Section sitemap for the dynamic News and Blog hub."""
    from storefront.sitemaps import BlogCategorySitemap, BlogPostSitemap

    return _render_django_sitemap(request, [BlogCategorySitemap, BlogPostSitemap])


def sitemap_section_color_categories(request):
    """Section sitemap for indexable colour×category landing pages.

    Spec: ``.kiro/specs/color-category-landings``. Includes only
    ``CategoryColorLanding`` rows with ``is_published=True``.
    """
    from storefront.sitemaps import CategoryColorLandingSitemap

    return _render_django_sitemap(request, [CategoryColorLandingSitemap])


def sitemap_section_thematic(request):
    """Section sitemap for thematic landings (US-5).

    Lists the four indexable themes registered in
    ``storefront.views.catalog.THEMATIC_LANDINGS_CONFIG`` —
    military / streetwear / patriotic / kharkiv-edition.
    """
    from storefront.sitemaps import ThematicLandingSitemap

    return _render_django_sitemap(request, [ThematicLandingSitemap])


def sitemap_images(request):
    """Google Image Sitemap.

    Per https://developers.google.com/search/docs/crawling-indexing/sitemaps/image-sitemaps
    each <url> may carry up to ~1000 <image:image> children. We emit the
    main_image of every published product (one image per URL — most direct
    win for product image search ranking).
    """
    base_url = _site_base_url()

    # Phase 21 (PR-6, T16.1) — expanded image sitemap. Emits, per
    # published product (deduped, capped at ~50 images per <url> to
    # stay well under Google's 1000-per-URL limit while keeping the
    # XML compact):
    #   * ``main_image`` (hero / OG)
    #   * ``display_image`` (catalog card image, if different)
    #   * ``ProductImage`` gallery rows
    #   * ``ProductColorVariant.images`` for every active colour variant
    products = (
        Product.objects.filter(status="published")
        .prefetch_related("images", "color_variants__images")
        .only("slug", "title", "main_image")
        .order_by("id")
    )

    urlset = ET.Element(
        "urlset",
        {
            "xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9",
            "xmlns:image": "http://www.google.com/schemas/sitemap-image/1.1",
        },
    )

    def _absolute(url_value: str) -> str:
        if not url_value:
            return ""
        if url_value.startswith(("http://", "https://")):
            return url_value
        if url_value.startswith("/"):
            return f"{base_url}{url_value}"
        return f"{base_url}/{url_value.lstrip('/')}"

    def _safe_url(field) -> str:
        try:
            return field.url if field else ""
        except (ValueError, AttributeError):
            return ""

    for product in products:
        if not product.slug:
            continue

        seen: set[str] = set()
        ordered: list[str] = []

        def _add(url_value: str) -> None:
            absolute = _absolute(url_value)
            if not absolute or absolute in seen:
                return
            seen.add(absolute)
            ordered.append(absolute)

        _add(_safe_url(product.main_image))
        _add(_safe_url(getattr(product, "display_image", None)))
        for img in getattr(product, "images", []).all():
            _add(_safe_url(getattr(img, "image", None)))
        for variant in product.color_variants.all():
            for img in variant.images.all():
                _add(_safe_url(getattr(img, "image", None)))

        if not ordered:
            continue

        url_el = ET.SubElement(urlset, "url")
        ET.SubElement(url_el, "loc").text = f"{base_url}/product/{product.slug}/"
        for image_url in ordered[:50]:
            image_el = ET.SubElement(url_el, "image:image")
            ET.SubElement(image_el, "image:loc").text = image_url
            if product.title:
                ET.SubElement(image_el, "image:title").text = product.title

    xml_payload = ET.tostring(urlset, encoding="utf-8", xml_declaration=True)
    return _sitemap_response(xml_payload)


def _hydrate_link_payload(item):
    payload = deepcopy(item)
    url_name = payload.pop("url_name", None)
    # Phase 21 (PR-5 T13.2) — let static-page link payloads point at
    # parametrised routes (e.g. ``catalog_by_cat`` for category pages)
    # by carrying optional ``url_kwargs``. Falls through to the legacy
    # plain-``reverse(url_name)`` path when ``url_kwargs`` is absent.
    url_kwargs = payload.pop("url_kwargs", None) or {}
    if url_name:
        payload["url"] = reverse(url_name, kwargs=url_kwargs) if url_kwargs else reverse(url_name)
    return payload


def _build_page_context(request, page_key):
    if page_key not in SUPPORT_PAGE_DEFINITIONS:
        raise Http404("Support page not found")

    page = deepcopy(SUPPORT_PAGE_DEFINITIONS[page_key])
    # Phase 17d.7 — translate top-level meta + hero fields per active language.
    from ..support_translations import apply_language_overrides
    page = apply_language_overrides(
        page, page_key, getattr(request, "LANGUAGE_CODE", "uk") or "uk"
    )
    page["key"] = page_key
    page["schema_type"] = page.get("schema_type") or ("CollectionPage" if page_key in {"faq", "site_map_page", "news"} else "WebPage")
    page["intro_links"] = [_hydrate_link_payload(link) for link in page.get("intro_links", [])]

    hydrated_sections = []
    for idx, section in enumerate(page.get("sections", []), start=1):
        payload = deepcopy(section)
        payload["anchor"] = payload.get("id") or f"section-{idx}"
        payload["cards"] = [_hydrate_link_payload(card) for card in payload.get("cards", [])]
        payload["links"] = [_hydrate_link_payload(link) for link in payload.get("links", [])]
        hydrated_sections.append(payload)
    page["sections"] = hydrated_sections

    hydrated_faq = []
    for idx, faq_item in enumerate(page.get("faq_items", []), start=1):
        payload = deepcopy(faq_item)
        payload["anchor"] = f"faq-{idx}"
        hydrated_faq.append(payload)
    page["faq_items"] = hydrated_faq

    cta = page.get("cta")
    if cta:
        cta_payload = deepcopy(cta)
        for action_name in ("primary", "secondary"):
            action = cta_payload.get(action_name)
            if action:
                cta_payload[action_name] = _hydrate_link_payload(action)
        page["cta"] = cta_payload

    context = {
        "page": page,
        "page_title": page["page_title"],
        "faq_items": page.get("faq_items", []),
        "footer_content": deepcopy(FOOTER_CONTENT),
        "breadcrumb_items": [
            {"name": _("Головна"), "url": reverse("home")},
            {"name": page["hero_title"], "url": request.path},
        ],
    }

    if page_key == "size_guide":
        size_grids = list(
            SizeGrid.objects.filter(is_active=True)
            .select_related("catalog")
            .order_by("catalog__order", "catalog__name", "order", "name")
        )
        context["size_guide_blocks"] = build_public_size_guide_blocks(size_grids)

    if page_key == "site_map_page":
        categories = [
            {
                "title": category.name,
                "text": category.description or _("Категорія каталогу TwoComms з окремою підбіркою товарів."),
                "url": reverse("catalog_by_cat", kwargs={"cat_slug": category.slug}),
            }
            for category in Category.objects.filter(is_active=True).only("name", "slug", "description")
            if category.slug
        ]
        latest_products = [
            {
                "title": product.title,
                "text": (
                    _("Сторінка товару у категорії %(category)s.") % {"category": product.category.name}
                    if getattr(product, "category", None)
                    else _("Сторінка товару.")
                ),
                "url": reverse("product", kwargs={"slug": product.slug}),
            }
            for product in (
                apply_public_product_order(
                    Product.objects.filter(status="published", slug__isnull=False)
                    .exclude(slug="")
                    .select_related("category")
                    .only("title", "slug", "category__name")
                )[:8]
            )
        ]
        context["dynamic_groups"] = [
            {
                "title": _("Категорії каталогу"),
                "eyebrow": "Catalog map",
                "items": categories,
            },
            {
                "title": _("Актуальні товарні сторін��и"),
                "eyebrow": "Fresh product links",
                "items": latest_products,
            },
        ]

    if page_key == "news":
        context["featured_products"] = list(
            apply_public_product_order(
                Product.objects.filter(status="published", slug__isnull=False)
                .exclude(slug="")
                .select_related("category")
                .only("title", "slug", "category__name")
            )[:8]
        )

    return context


def _render_support_page(request, page_key):
    context = _build_page_context(request, page_key)
    return render(request, context["page"]["template"], context)


def google_merchant_feed(request):
    """
    Google Merchant Center Product Feed.

    Генерирует XML feed для Google Shopping.
    """
    xml_payload = build_google_merchant_feed_xml(
        base_url=_feed_base_url()
    )
    response = HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="google_merchant_feed.xml"'
    return response


def rozetka_feed_xml(request):
    """Dynamic Rozetka XML/YML feed."""
    xml_payload = build_rozetka_feed_xml(
        base_url=_feed_base_url()
    )
    response = HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="rozetka-feed.xml"'
    return response


def kasta_feed_xml(request):
    """Dynamic Kasta XML/YML feed."""
    xml_payload = build_kasta_feed_xml(
        base_url=_feed_base_url()
    )
    response = HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="kasta-feed.xml"'
    return response


def buyme_feed_xml(request):
    """Dynamic BuyMe XML/YML feed."""
    xml_payload = build_buyme_feed_xml(
        base_url=_feed_base_url()
    )
    response = HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="buyme-feed.xml"'
    return response


def uaprom_products_feed(request):
    """
    Генерирует XML feed для Bezzet.
    Формат: legacy YML feed для marketplace-импорта.

    Обновленная версия:
    - Включает только опубликованные товары
    - Правильно обрабатывает все изображения (main_image + images + color variants)
    - Корректно отображает цены (oldprice = price, price = final_price)
    - Использует full_description если доступно
    """
    xml_payload = build_uaprom_products_feed_xml(
        base_url=_feed_base_url()
    )
    response = HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="products_feed.xml"'
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def static_verification_file(request):
    """
    Файл верификации для внешних сервисов.

    Например: Google Search Console, Bing Webmaster Tools, и т.д.
    """
    # Путь к файлу верификации
    verification_file = Path(settings.BASE_DIR) / '494cb80b2da94b4395dbbed566ab540d.txt'

    try:
        if verification_file.exists():
            return FileResponse(
                open(verification_file, 'rb'),
                content_type='text/plain'
            )
    except Exception:
        pass

    raise Http404("Verification file not found")


def indexnow_key_file(request, key):
    configured_key = (getattr(settings, "INDEXNOW_KEY", "") or "").strip()
    if not getattr(settings, "INDEXNOW_ENABLED", True) or not configured_key or key != configured_key:
        raise Http404("IndexNow key not found")

    return HttpResponse(configured_key, content_type="text/plain; charset=utf-8")


def about(request):
    """
    Brand story page with dedicated layout and structured content.
    """
    brand_page_url = f"{settings.SITE_BASE_URL}{reverse('about')}"
    breadcrumb_items = [
        {"name": _("Головна"), "url": reverse("home")},
        {"name": _("Про бренд"), "url": brand_page_url},
    ]

    return render(
        request,
        "pages/pro_brand.html",
        {
            "page_title": _("Про бренд"),
            "brand_page_url": brand_page_url,
            "faq_items": deepcopy(PRO_BRAND_FAQ_ITEMS),
            "breadcrumb_items": breadcrumb_items,
            "footer_content": deepcopy(FOOTER_CONTENT),
        },
    )


def contacts(request):
    """
    Страница "Контакты".

    SEO 2026-05-16 (P1-3) — pass ``breadcrumb_items`` and emit a
    ``BreadcrumbList`` JSON-LD (rendered by ``contacts.html`` via
    the global ``{% breadcrumb_schema %}`` tag) so the page enters the
    Google Knowledge Graph with a typed path Home → Contacts.
    """
    breadcrumb_items = [
        {"name": _("Головна"), "url": reverse("home")},
        {"name": _("Контакти"), "url": reverse("contacts")},
    ]
    # SEO 2026-05-19 (VILNI deep review §3.2, §4.7, §4.11) — supply the
    # ClothingStore JSON-LD with the dynamic catalogue price range and
    # the curated sameAs list, so /contacts/ no longer ships the
    # non-standard "₴₴" placeholder nor a stale 2-link social list that
    # drifts from the canonical Organization node on /.
    from storefront.seo_utils import (
        _homepage_price_range_text,
        _organization_same_as,
    )
    return render(request, 'pages/contacts.html', {
        'page_title': 'Контакти',
        'breadcrumb_items': breadcrumb_items,
        'price_range': _homepage_price_range_text(),
        'same_as_links': _organization_same_as(),
    })


def delivery(request):
    """
    Страница "Доставка и оплата".
    """
    return _render_support_page(request, "delivery")


def help_center(request):
    """Service-manual page with internal linking and support FAQ."""
    return _render_support_page(request, "help_center")


def faq(request):
    """FAQ hub."""
    return _render_support_page(request, "faq")


def size_guide(request):
    """Size guide and fit advice."""
    return _render_support_page(request, "size_guide")


def care_guide(request):
    """Care guide for garments and prints."""
    return _render_support_page(request, "care_guide")


def order_tracking(request):
    """Order tracking help page."""
    return _render_support_page(request, "order_tracking")


def site_map_page(request):
    """Human-readable sitemap with link clusters."""
    return _render_support_page(request, "site_map_page")


def news(request):
    """Brand updates and latest product links."""
    return _render_support_page(request, "news")


def healthz(request):
    """Cheap liveness probe.

    SEO v1.0 Phase 8 (2026-05-12) — finding (B21). External monitoring
    services (UptimeRobot, BetterStack, Pingdom, Cronitor, etc.) need a
    deterministic 200-OK endpoint that neither hits the database nor
    triggers expensive middleware so they can poll every 30-60 s
    without distorting analytics or holding DB connections. Return a
    minimal JSON payload — Cache-Control: no-store keeps any
    intermediate caches honest, ``server-timing`` lets monitoring
    services chart server-side latency without a separate trace pass.
    """
    response = JsonResponse(
        {
            "status": "ok",
            "service": "twocomms",
            "timestamp": timezone.now().isoformat(),
        },
        json_dumps_params={"ensure_ascii": False},
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def custom_print(request):
    """
    Guided DTF-only configurator на основном сайте.
    """
    config = build_custom_print_config(
        submit_url=request.build_absolute_uri(reverse("custom_print_lead")),
        safe_exit_url=request.build_absolute_uri(reverse("custom_print_safe_exit")),
        add_to_cart_url=request.build_absolute_uri(reverse("custom_print_add_to_cart")),
        track_event_url=request.build_absolute_uri(reverse("track_event")),
    )
    breadcrumb_items = [
        {"name": _("Головна"), "url": reverse("home")},
        {"name": _("Кастомний принт"), "url": reverse("custom_print")},
    ]

    # PDP → Custom prefill (ТЗ v3, файл 08). Якщо користувач прийшов з
    # картки товару ("Хочеш цей принт інакше?"), показуємо context-banner
    # «Почали з товару: …» і помічаємо сторінку noindex, щоб query-URL не
    # створював дублі в індексі. canonical завжди веде на чистий /custom-print/.
    prefill_source = (request.GET.get("source") or "").strip()[:32]
    prefill_product_slug = (request.GET.get("product") or "").strip()[:200]
    prefill_product = None
    if prefill_source == "pdp" and prefill_product_slug:
        prefill_product = (
            Product.objects.filter(status="published", slug=prefill_product_slug)
            .only("title", "slug")
            .first()
        )
    prefill_noindex = bool(request.GET.get("source") or request.GET.get("product"))

    response = render(request, 'pages/custom_print.html', {
        'page_title': _('Кастомний принт'),
        'custom_print_config': config,
        'breadcrumb_items': breadcrumb_items,
        'faq_items': CUSTOM_PRINT_FAQ_ITEMS,
        'prefill_product': prefill_product,
        'prefill_source': prefill_source,
        'prefill_noindex': prefill_noindex,
    })
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@require_POST
def custom_print_lead(request):
    """
    AJAX endpoint для новой лид-формы кастомного принта.
    """
    form = CustomPrintLeadForm(request.POST, request.FILES, require_artwork_files=False)
    if not form.is_valid():
        errors = {}
        for field, field_errors in form.errors.get_json_data().items():
            errors[field] = [error["message"] for error in field_errors]
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    with transaction.atomic():
        lead = form.save()
        transaction.on_commit(
            lambda: notify_new_custom_print_lead(lead),
            robust=True,
        )
    record_custom_print_event(
        request,
        "custom_print_send_to_manager",
        lead=lead,
        step_key=(lead.exit_step or "contact"),
        metadata={"submission_type": "lead"},
    )
    return JsonResponse(
        {
            "ok": True,
            "lead_number": lead.lead_number,
            "message": "Дякуємо! Менеджер зв’яжеться з вами найближчим часом.",
        }
    )


def _safe_decimal_or_zero(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _build_custom_cart_session_item(lead) -> dict:
    snapshot = lead.config_draft_json or {}
    pricing = snapshot.get("pricing") or {}
    product_payload = snapshot.get("product") or {}
    print_payload = snapshot.get("print") or {}
    order_payload = snapshot.get("order") or {}
    artwork_payload = snapshot.get("artwork") or {}
    gift_payload = order_payload.get("gift")
    placement_specs = lead.placement_specs_json or []

    if isinstance(gift_payload, dict):
        gift_enabled = bool(gift_payload.get("enabled"))
        gift_text = (gift_payload.get("text") or "").strip()
    else:
        gift_enabled = bool(gift_payload)
        gift_text = (order_payload.get("gift_text") or "").strip()

    try:
        quantity = int(order_payload.get("quantity") or lead.quantity or 1)
    except (TypeError, ValueError):
        quantity = 1
    if quantity < 1:
        quantity = 1

    final_total = _safe_decimal_or_zero(pricing.get("final_total") or lead.final_price_value)
    unit_total = _safe_decimal_or_zero(pricing.get("unit_total") or pricing.get("base_price"))
    if unit_total <= 0 and quantity > 0 and final_total > 0:
        unit_total = (final_total / Decimal(quantity)).quantize(Decimal("0.01"))

    raw_add_ons = list(print_payload.get("add_ons") or [])
    add_on_labels = []
    for add_on in raw_add_ons:
        label = ADDON_LABELS.get(add_on, add_on)
        if label and label not in add_on_labels:
            add_on_labels.append(label)

    zone_labels = []
    if placement_specs:
        for spec in placement_specs:
            if not isinstance(spec, dict):
                continue
            placement_key = spec.get("placement_key") or spec.get("zone")
            label = spec.get("label") or ZONE_LABELS.get(placement_key, placement_key or "")
            if label and label not in zone_labels:
                zone_labels.append(label)
    else:
        for zone in (print_payload.get("zones") or lead.placements or []):
            label = ZONE_LABELS.get(zone, zone)
            if label and label not in zone_labels:
                zone_labels.append(label)

    return {
        "lead_id": lead.pk,
        "lead_number": getattr(lead, "lead_number", "") or f"CP-{lead.pk}",
        "label": compute_cart_label(snapshot),
        "product_type": product_payload.get("type") or lead.product_type,
        "product_label": PRODUCT_LABELS.get(product_payload.get("type") or lead.product_type, ""),
        "fit": product_payload.get("fit") or lead.fit or "",
        "fabric": product_payload.get("fabric") or lead.fabric or "",
        "color": product_payload.get("color") or lead.color_choice or "",
        "zones": list(print_payload.get("zones") or lead.placements or []),
        "zone_labels": zone_labels,
        "placement_specs": placement_specs,
        "quantity": quantity,
        "size_mode": order_payload.get("size_mode") or lead.size_mode or "single",
        "size_breakdown": order_payload.get("size_breakdown") or {},
        "sizes_note": order_payload.get("sizes_note") or lead.sizes_note or "",
        "gift_enabled": gift_enabled,
        "gift_text": gift_text,
        "unit_total": str(unit_total.quantize(Decimal("0.01"))),
        "final_total": str(final_total.quantize(Decimal("0.01"))),
        "b2b_discount_per_unit": pricing.get("b2b_discount_per_unit") or 0,
        "mode": snapshot.get("mode") or lead.client_kind or "personal",
        "service_kind": artwork_payload.get("service_kind") or lead.service_kind or "",
        "file_triage_status": artwork_payload.get("triage_status") or lead.file_triage_status or "",
        "add_ons": raw_add_ons,
        "add_on_labels": add_on_labels,
        "placement_note": (print_payload.get("placement_note") or lead.placement_note or "").strip(),
        "moderation_status": lead.moderation_status or CustomPrintModerationStatus.DRAFT,
    }


@require_POST
def custom_print_add_to_cart(request):
    """
    V2 endpoint: create a CustomPrintLead (source=custom_print_cart) and
    push a lightweight reference into the session-based custom cart.
    """
    form = CustomPrintLeadForm(request.POST, request.FILES, require_artwork_files=True)
    if not form.is_valid():
        errors = {}
        for field, field_errors in form.errors.get_json_data().items():
            errors[field] = [error["message"] for error in field_errors]
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    with transaction.atomic():
        lead = form.save(
            source="custom_print_cart",
            moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW,
        )
        lead.ensure_moderation_token()

    custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
    if not isinstance(custom_cart, dict):
        custom_cart = {}

    key = f"custom:{lead.pk}"
    custom_cart[key] = _build_custom_cart_session_item(lead)
    request.session[SESSION_CUSTOM_CART_KEY] = custom_cart
    request.session.modified = True
    from storefront.views.utils import _reset_monobank_session
    _reset_monobank_session(request, drop_pending=True)

    try:
        notify_custom_print_moderation_request(lead)
    except Exception:
        logging.getLogger(__name__).exception(
            "notify_custom_print_moderation_request failed for lead %s during add-to-cart",
            lead.pk,
        )
    record_custom_print_event(
        request,
        "custom_print_add_to_cart",
        lead=lead,
        step_key=(lead.exit_step or "contact"),
        metadata={"submission_type": "cart"},
    )

    return JsonResponse(
        {
            "ok": True,
            "lead_number": getattr(lead, "lead_number", None),
            "cart_url": reverse("cart"),
            "custom_cart_count": len(custom_cart),
            "message": "Кастом додано в кошик і передано менеджеру на модерацію. Відкриваємо…",
        }
    )


@require_POST
def custom_print_remove(request):
    """
    V2 endpoint: remove a single custom-print item from the session cart.
    """
    raw_key = (request.POST.get("key") or "").strip()
    lead_id_raw = (request.POST.get("lead_id") or "").strip()
    custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
    if not isinstance(custom_cart, dict):
        custom_cart = {}

    removed = None
    if raw_key and raw_key in custom_cart:
        removed = custom_cart.pop(raw_key, None)
    elif lead_id_raw:
        try:
            lead_id_int = int(lead_id_raw)
        except (TypeError, ValueError):
            lead_id_int = None
        if lead_id_int is not None:
            key_by_id = f"custom:{lead_id_int}"
            if key_by_id in custom_cart:
                removed = custom_cart.pop(key_by_id, None)

    request.session[SESSION_CUSTOM_CART_KEY] = custom_cart
    request.session.modified = True
    if removed:
        from storefront.views.utils import _reset_monobank_session
        _reset_monobank_session(request, drop_pending=True)
    return JsonResponse({
        "ok": True,
        "removed": bool(removed),
        "custom_cart_count": len(custom_cart),
    })


def _custom_print_action_signature(lead_id: int, action: str, token: str) -> str:
    """Compute HMAC signature for Telegram moderation action URLs."""
    import hmac
    import hashlib
    from django.conf import settings
    secret = (getattr(settings, "SECRET_KEY", "") or "").encode("utf-8")
    payload = f"{lead_id}:{action}:{token}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()[:32]


def _verify_custom_print_action(lead, action: str, token: str, signature: str) -> bool:
    import hmac
    if not lead or not token or not signature:
        return False
    if (lead.moderation_token or "") != token:
        return False
    expected = _custom_print_action_signature(lead.pk, action, token)
    return hmac.compare_digest(expected, signature)


@require_POST
def custom_print_submit_review(request):
    """
    User clicks "Send to manager for review". Mark every draft/rejected custom
    item in the session cart as awaiting_review and notify the manager via Telegram
    with approve/reject/contact inline buttons.
    """
    from storefront.models import CustomPrintLead, CustomPrintModerationStatus
    from storefront.custom_print_notifications import notify_custom_print_moderation_request

    custom_cart = request.session.get(SESSION_CUSTOM_CART_KEY) or {}
    if not isinstance(custom_cart, dict) or not custom_cart:
        return JsonResponse({"ok": False, "error": "Кастомних товарів у кошику немає."}, status=400)

    lead_ids = [item.get("lead_id") for item in custom_cart.values()
                if isinstance(item, dict) and item.get("lead_id")]
    if not lead_ids:
        return JsonResponse({"ok": False, "error": "Немає кастомних позицій для відправки."}, status=400)

    leads = list(CustomPrintLead.objects.filter(pk__in=lead_ids))
    notified = 0
    now = timezone.now()
    for lead in leads:
        # Skip if already awaiting or approved
        if lead.moderation_status in (CustomPrintModerationStatus.AWAITING_REVIEW,
                                       CustomPrintModerationStatus.APPROVED):
            continue
        lead.ensure_moderation_token()
        lead.moderation_status = CustomPrintModerationStatus.AWAITING_REVIEW
        lead.reviewed_at = None
        lead.save(update_fields=["moderation_status", "moderation_token", "reviewed_at"])
        # Sync session snapshot
        key = f"custom:{lead.pk}"
        if key in custom_cart and isinstance(custom_cart[key], dict):
            custom_cart[key]["moderation_status"] = CustomPrintModerationStatus.AWAITING_REVIEW
        try:
            if notify_custom_print_moderation_request(lead):
                notified += 1
        except Exception:
            logging.getLogger(__name__).exception("notify_custom_print_moderation_request failed for lead %s", lead.pk)
        record_custom_print_event(
            request,
            "custom_print_send_to_manager",
            lead=lead,
            step_key=(lead.exit_step or "contact"),
            metadata={"submission_type": "cart_review"},
        )

    request.session[SESSION_CUSTOM_CART_KEY] = custom_cart
    request.session.modified = True

    return JsonResponse({
        "ok": True,
        "notified": notified,
        "message": "Замовлення надіслано менеджеру на перевірку.",
    })


def _render_moderation_result(request, *, title: str, message: str, ok: bool = True, status_code: int = 200):
    return HttpResponse(
        f"<!doctype html><html lang='uk'><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        f"<style>body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0b0b0b;color:#eee;"
        f"display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:20px;text-align:center;}}"
        f".card{{max-width:420px;background:#1c1c1c;border-radius:14px;padding:32px;"
        f"border:1px solid {'#2ecc71' if ok else '#e74c3c'};}}"
        f"h1{{font-size:22px;margin:0 0 12px;}}p{{margin:0;line-height:1.5;color:#cfcfcf;}}</style>"
        f"</head><body><div class='card'><h1>{title}</h1><p>{message}</p></div></body></html>",
        status=status_code,
    )


def custom_print_moderation_action(request, lead_id: int, action: str):
    """
    HMAC-signed URL endpoint used by manager from the Telegram inline buttons.
    Actions: 'approve', 'reject'.
    """
    from storefront.models import CustomPrintLead, CustomPrintModerationStatus

    if action not in {"approve", "reject"}:
        return _render_moderation_result(request, title="Некоректна дія",
                                         message="Невідома дія модерації.", ok=False, status_code=400)

    token = (request.GET.get("token") or "").strip()
    signature = (request.GET.get("sig") or "").strip()
    try:
        lead = CustomPrintLead.objects.get(pk=lead_id)
    except CustomPrintLead.DoesNotExist:
        return _render_moderation_result(request, title="Не знайдено",
                                         message="Заявку не знайдено.", ok=False, status_code=404)

    if not _verify_custom_print_action(lead, action, token, signature):
        return _render_moderation_result(request, title="Підпис невалідний",
                                         message="Посилання застаріло або недійсне.",
                                         ok=False, status_code=403)

    if action == "approve":
        if Decimal(str(lead.final_price_value or 0)) <= 0:
            return _render_moderation_result(
                request,
                title="Потрібна ціна",
                message=(
                    f"Заявку {lead.lead_number} не можна погодити без фінальної ціни. "
                    "Вкажіть суму в staff-панелі й повторіть дію."
                ),
                ok=False,
                status_code=409,
            )
        lead.moderation_status = CustomPrintModerationStatus.APPROVED
        lead.reviewed_at = timezone.now()
        lead.save(update_fields=["moderation_status", "reviewed_at"])
        record_custom_print_event(
            request,
            "custom_print_moderation_result",
            lead=lead,
            step_key=(lead.exit_step or ""),
            metadata={"result": "approved"},
        )
        return _render_moderation_result(
            request,
            title="Погоджено",
            message=f"Заявку {lead.lead_number} позначено як погоджену. Клієнт тепер може оплатити замовлення.",
            ok=True,
        )

    # reject
    lead.moderation_status = CustomPrintModerationStatus.REJECTED
    lead.reviewed_at = timezone.now()
    lead.save(update_fields=["moderation_status", "reviewed_at"])
    record_custom_print_event(
        request,
        "custom_print_moderation_result",
        lead=lead,
        step_key=(lead.exit_step or ""),
        metadata={"result": "rejected"},
    )
    return _render_moderation_result(
        request,
        title="Відхилено",
        message=f"Заявку {lead.lead_number} позначено як відхилену. Клієнту показано статус у кошику.",
        ok=True,
    )


@require_POST
def custom_print_safe_exit(request):
    """
    Safe exit with snapshot handoff to менеджеру.
    """
    try:
        payload = json.loads((request.body or b"{}").decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Некоректний safe-exit payload."}, status=400)

    snapshot = normalize_custom_print_snapshot(payload if isinstance(payload, dict) else {})
    contact = snapshot.get("contact") or {}
    product = snapshot.get("product") or {}
    artwork = snapshot.get("artwork") or {}
    order = snapshot.get("order") or {}
    print_payload = snapshot.get("print") or {}
    notes = snapshot.get("notes") or {}

    lead = None
    can_persist_lead = all(
        (
            contact.get("channel"),
            contact.get("name"),
            contact.get("value"),
        )
    )

    if can_persist_lead:
        lead = CustomPrintLead.objects.create(
            service_kind=artwork.get("service_kind") or "design",
            product_type=product.get("type") or "hoodie",
            placements=print_payload.get("zones") or [],
            placement_note=print_payload.get("placement_note") or "",
            quantity=order.get("quantity") or 1,
            size_mode=order.get("size_mode") or "single",
            sizes_note=order.get("sizes_note") or "",
            client_kind=snapshot.get("mode") or "personal",
            business_kind="",
            brand_name=notes.get("brand_name") or "",
            fit=product.get("fit") or "",
            fabric=product.get("fabric") or "",
            color_choice=product.get("color") or "",
            garment_note=notes.get("garment_note") or "",
            file_triage_status=artwork.get("triage_status") or "needs-review",
            exit_step=(snapshot.get("ui") or {}).get("current_step") or "",
            placement_specs_json=build_placement_specs(snapshot),
            pricing_snapshot_json=snapshot.get("pricing") or {},
            config_draft_json=snapshot,
            name=contact.get("name") or "",
            contact_channel=contact.get("channel") or "",
            contact_value=contact.get("value") or "",
            brief=notes.get("brief") or "",
            source="custom_print_safe_exit",
            moderation_status=CustomPrintModerationStatus.DRAFT,
        )

    record_custom_print_event(
        request,
        "custom_print_safe_exit",
        lead=lead,
        step_key=(snapshot.get("ui") or {}).get("current_step"),
        metadata={
            "had_contact": can_persist_lead,
            "zones": list((snapshot.get("print") or {}).get("zones") or []),
            "submission_type": "safe_exit",
        },
    )
    notify_custom_print_safe_exit(lead=lead, snapshot=lead.config_draft_json if lead else snapshot)
    return JsonResponse(
        {
            "ok": True,
            "lead_number": getattr(lead, "lead_number", None),
            "manager_url": TELEGRAM_MANAGER_URL,
        }
    )


def returns(request):
    """
    Страница "Возврат и обмен".
    """
    return _render_support_page(request, "returns")


def privacy_policy(request):
    """
    Страница "Политика конфиденциальности".
    """
    return _render_support_page(request, "privacy_policy")


def terms_of_service(request):
    """
    Страница "Условия использования".
    """
    return _render_support_page(request, "terms_of_service")


@staff_member_required
def test_analytics_events(request):
    """
    Тестовая страница для проверки аналитических событий.

    ВАЖНО (AN-015/W1-8): страница авто-стреляет полную воронку событий
    (включая Purchase) в БОЕВЫЕ Pixel/Analytics, поэтому доступ — только staff.

    Автоматически отправляет все типы событий для тестирования
    в TikTok Events Manager и Facebook Events Manager.

    Использование:
    - Откройте /test-analytics/
    - Добавьте ?ttq_test=YOUR_TEST_CODE для TikTok
    - События отправятся автоматически при загрузке
    """
    import os

    # Получаем test_event_code из URL или environment
    ttq_test_code = request.GET.get('ttq_test') or os.environ.get('TIKTOK_TEST_EVENT_CODE')

    # Генерируем тестовые данные для событий
    test_product = {
        'id': 'TC-999-test-001-M',
        'name': 'Тестовый товар TwoComms',
        'category': 'Тестовая категория',
        'price': 599,
        'quantity': 1
    }

    return render(request, 'pages/test_analytics.html', {
        'page_title': 'Тест аналитических событий',
        'ttq_test_code': ttq_test_code,
        'test_product': test_product,
    })


def prom_feed_xml(request):
    """
    Dynamic generation of Prom.ua feed (replacing the static file cron job).
    URL: /prom-feed.xml
    """
    xml_payload = build_prom_feed_xml(
        base_url=_feed_base_url()
    )
    response = HttpResponse(xml_payload, content_type="application/xml; charset=utf-8")
    response["Content-Disposition"] = 'inline; filename="prom-feed.xml"'
    return response
