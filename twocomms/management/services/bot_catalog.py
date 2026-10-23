"""
Каталог для бота: компактний контекст про товари (ціни, наявність, кольори,
посилання), який підставляється в system_instruction Gemini. Кешується, щоб
не смикати БД на кожне повідомлення.

Джерела:
- storefront.Product (status=published): назва, категорія, slug.
- productcolors.ProductColorVariant + Каталог товарів: ціна конфігурації, кольори,
  фасони/опції та залишок (stock) на вітрині.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)
from django.db.models import Sum

CACHE_KEY = "ig_bot_catalog_ctx_physical_boundary_v2"
CACHE_COMPACT_KEY = "ig_bot_catalog_ctx_compact_physical_boundary_v2"
CACHE_TTL = 600          # 10 хв
# Збій build-у кешується на дуже короткий час: знімок мусить швидко оновитись
# після відновлення, а не жити ті самі 10 хвилин, що й валідний каталог.
CATALOG_ERROR_TTL = 45
# Останній добрий знімок живе довше за звичайний кеш: він потрібен саме тоді,
# коли build падає підряд кілька разів.
CACHE_LAST_KNOWN_GOOD_TTL = 6 * 60 * 60
MAX_PRODUCTS = 250
# Бюджет підвищено після прод-перевірки 02.08.2026. При 16 000 символів каталог
# важив 15 977 і в промпт потрапляли **48 товарів із 71**: сортування
# `-featured, -id` ставить нові товари першими, тому відрізало найстаріші —
# а це базові моделі id=1 «Футболка класична», id=2 «Худі класичне»,
# id=3 «Класичний лонгслів».
#
# Ціна цієї обрізки видна в переписці клієнта #5: він просив «стандартную
# черную классику», надіслав посилання саме на id=1, а бот відповів, що
# «полностью однотонной черной без рисунка сейчас нет в наличии». Модель не
# вигадувала — вона казала правду про той каталог, який бачила.
#
# 48 000 символів вміщують усі 71 товар із запасом. Для gemini-3.7-flash з
# контекстом на мільйон токенів це дрібниця, а обрізка тут — прямі втрачені
# продажі.
MAX_CHARS = 48000
# The complete catalog is valuable to media/vision workflows, but its verbose
# merchandising copy should not consume the sales reply budget on every turn.
# This form retains every product and purchase-critical configuration fact.
COMPACT_MAX_CHARS = 20000
COMPACT_PRINT_FINGERPRINT_CHARS = 96
SITE = "https://twocomms.shop"


def resolve_catalog_sizes(product) -> dict[str, list[str]]:
    """Resolve the published size contract for each active fit option."""
    try:
        from product_catalog.size_grid_services import (
            normalize_size_value,
            resolve_effective_sizes,
        )
        from storefront.services.size_guides import resolve_product_sizes

        fits = list(product.fit_options.filter(is_active=True).order_by("order", "id"))
        if not fits:
            values = [normalize_size_value(value) for value in resolve_product_sizes(product)]
            return {"default": [value for value in values if value]}
        result: dict[str, list[str]] = {}
        for fit in fits:
            rows = resolve_effective_sizes(product, f"fit={fit.code}")
            values = [
                normalize_size_value(row.get("size"))
                for row in rows
                if isinstance(row, dict) and row.get("is_enabled", True)
            ]
            values = [value for value in values if value]
            if not values:
                values = [normalize_size_value(value) for value in resolve_product_sizes(product)]
                values = [value for value in values if value]
            result[str(fit.code).lower()] = list(dict.fromkeys(values))
        return result
    except Exception:
        return {}


def _build(*, compact: bool = False) -> str:
    try:
        from storefront.models import Product, ProductStatus
        from productcolors.models import ProductColorVariant
    except Exception:
        return ""

    qs = (
        Product.objects.filter(status=ProductStatus.PUBLISHED)
        .select_related("category")
        .order_by("-featured", "-id")[:MAX_PRODUCTS]
    )
    products = list(qs)
    if not products:
        return ""
    ids = [p.id for p in products]

    # Кольори + залишки + службові IDs по варіантах одним запитом.
    variants_by_product: dict[int, list[object]] = {}
    stock_by_product: dict[int, int] = {}
    fp_by_product: dict[int, list[str]] = {}
    variants = list(
        ProductColorVariant.objects.filter(product_id__in=ids)
        .select_related("color", "product")
        .order_by("product_id", "order", "id")
    )
    for v in variants:
        variants_by_product.setdefault(v.product_id, []).append(v)
        stock_by_product[v.product_id] = stock_by_product.get(v.product_id, 0) + int(v.stock or 0)
        bv = (v.metadata or {}).get("bot_vision") or {}
        seg = (bv.get("summary") or bv.get("print_subject") or "").strip()
        if seg:
            fp_by_product.setdefault(v.product_id, [])
            if seg not in fp_by_product[v.product_id]:
                fp_by_product[v.product_id].append(seg)

    from management.services.ig_catalog_pricing import (
        format_variant_pricing,
        prepare_pricing_context,
        resolve_product_pricing,
    )
    prepare_pricing_context(products, variants)

    lines = [
        "Каталог TwoComms (актуальні товари, ціни в грн):"
        if not compact
        else "Каталог TwoComms (усі актуальні товари; скорочений формат):"
    ]
    for p in products:
        pricing = resolve_product_pricing(
            p,
            variants=variants_by_product.get(p.id, []),
        )
        price_label = (
            f"{pricing['display']} грн"
            if pricing["display"]
            else "ціна залежить від конфігурації"
        )
        disc = ""
        try:
            if p.has_discount and p.discount_percent:
                if variants_by_product.get(p.id):
                    disc = f" (знижка {p.discount_percent}% врахована)"
                else:
                    disc = f" (знижка {p.discount_percent}%, було {p.price})"
        except Exception:
            pass
        fps = fp_by_product.get(p.id, [])
        variants_s = format_variant_pricing(pricing["configurations"])
        colors_s = ("; кольори: " + variants_s) if variants_s else ""
        has_variant_size_contract = any(
            row.get("has_compatible_size_contract")
            for row in pricing["configurations"]
        )
        sizes_by_fit = {} if has_variant_size_contract else resolve_catalog_sizes(p)
        fits_s = "; фасони/розміри: " + "; ".join(
            f"{code}: {'/'.join(values)}" for code, values in sizes_by_fit.items() if values
        ) if sizes_by_fit else ""

        if compact:
            # Keep the exact selection contract but remove context-free prose
            # repeated on every row (category, generic stock note and URL).
            # A short visual fingerprint remains available for product matching.
            compact_line = f"• id={p.id} | {p.title} — {price_label}{disc}"
            if variants_s:
                compact_line += f"; кольори: {variants_s}"
            if fits_s:
                compact_line += fits_s
            if fps:
                fingerprint = "; ".join(fps[:3])[:COMPACT_PRINT_FINGERPRINT_CHARS].rstrip()
                if fingerprint:
                    compact_line += f"; принт: {fingerprint}"
            lines.append(compact_line)
            continue

        cat = getattr(p.category, "name", "") or ""
        stock = stock_by_product.get(p.id, 0)
        # Э3.7: формулировку наличия даёт ЕДИНЫЙ resolver, а не самостоятельное
        # суммирование `stock` здесь. Иначе каталог и checkout снова начнут
        # расходиться, и клиент прочитает «під замовлення» там, где checkout
        # считает вариант доступным (или наоборот). Агрегированный `stock`
        # остаётся диагностикой для оператора.
        avail = _availability_label(p.id, aggregate_stock=stock)
        fp_s = (" | принт: " + "; ".join(fps[:3])) if fps else ""
        # `stock` у рядку варіанта показуємо лише коли він додатний: нуль у цьому
        # проєкті означає «облік не ведеться», і модель читала його як «немає».
        url = f"{SITE}/product/{p.slug}/"
        lines.append(
            f"• id={p.id} | {p.title} — {price_label}{disc} [{cat}]"
            f"{colors_s}{avail}{fits_s}{fp_s} | {url}"
        )

    price_rule = (
        "\nМежа каталогу: це ціни та описані конфігурації, не фізична наявність. "
        "Обліковий stock будь-якого значення, доступні/недоступні для checkout "
        "розміри й неповний перелік кольорів не доводять наявність або відсутність. "
        "Для непідтвердженої можливості збережи точне побажання й уточни у менеджера; "
        "не підмінюй його показаним кольором чи іншим розміром."
        "\nПравило ціни: точну ціну називай лише для обраної конфігурації "
        "variant_id + фасон/опції. Якщо в рядку є діапазон або різні ціни, "
        "спершу уточни параметри; не підмінюй ціну товару базовою. Не вигадуй "
        "variant_id, фасон або розмір; використовуй тільки значення з каталогу."
    )
    if compact:
        from management.services.ig_catalog_prompt_compaction import fit_compact_catalog
        text, dropped = fit_compact_catalog(lines, limit=COMPACT_MAX_CHARS, footer=price_rule)
        if dropped:
            _log_catalog_truncation(dropped, len(products), COMPACT_MAX_CHARS)
        return text
    text, _dropped = truncate_catalog_lines(lines, limit=MAX_CHARS)
    return text + price_rule


def _availability_label(product_id: int, *, aggregate_stock: int) -> str:
    """Expose resolver eligibility and accounting without a physical-stock claim."""
    try:
        from management.services.ig_offer_resolver import (
            OfferStatus,
            resolve_product_presentation,
        )

        resolution = resolve_product_presentation(product_id)
    except Exception:
        logger.debug("offer resolver unavailable for product %s", product_id, exc_info=True)
        return ", наявність уточнюємо"
    status = {OfferStatus.IN_STOCK: "in_stock", OfferStatus.MADE_TO_ORDER: "made_to_order",
        OfferStatus.UNAVAILABLE: "unavailable"}.get(resolution.status, "unknown")
    return (f", обліковий stock={aggregate_stock}, checkout_status={status} "
        "(фізичну наявність не підтверджує)")


def _log_catalog_truncation(dropped: int, total: int, limit: int) -> None:
    """Make the truncation visible: it used to happen without a trace."""
    try:
        from management.services.instagram_bot import log

        log(
            "warning",
            "catalog_truncated",
            f"{dropped} of {total} products dropped to fit {limit} chars",
        )
    except Exception:
        logger.warning(
            "Instagram catalog truncated: %s of %s products dropped to fit %s chars",
            dropped,
            total,
            limit,
        )


def truncate_catalog_lines(lines: list[str], *, limit: int = MAX_CHARS) -> tuple[str, int]:
    """Fit the catalog into the budget by whole products, not by characters.

    F-CAT-001: the previous ``text[:MAX_CHARS]`` cut mid-line, so the last
    product arrived truncated and the model could read a partial price or a
    partial variant_id. On production the catalog is 16 118 characters against a
    16 000 limit, which means 22 of 71 published products never reach the prompt
    — and nothing said so.
    """
    total = len(lines)
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > limit:
            break
        kept.append(line)
        used += cost
    dropped = total - len(kept)
    if not dropped:
        return "\n".join(kept), 0
    _log_catalog_truncation(dropped, total, limit)
    kept.append(
        f"…(показано {len(kept)} товарів із {total}; {dropped} не вміщено — "
        "якщо клієнт питає про товар, якого тут немає, скажи що уточниш у менеджера)"
    )
    return "\n".join(kept), dropped


def get_catalog_context(force: bool = False, *, compact: bool = False) -> str:
    """Э3.8: технічний збій НЕ кешується як валідний порожній каталог.

    Раніше будь-яке виключення `_build()` перетворювалось у порожній рядок і
    кешувалось на 600 секунд. Зовнішній `_prompt_section()` бачив звичайний
    порожній результат, тому в логах не було навіть `error/prompt_context`. Один
    транзиентний збій позбавляв усі наступні відповіді каталогу, цін і розмірів
    на десять хвилин — і робив це тихо.

    Тепер: успішний build кешується як завжди і додатково зберігається як
    last-known-good; збій НЕ кешується, а віддає останній добрий знімок зі
    штампом віку. Якщо доброго знімка немає — повертається порожній рядок, але з
    видимим alert-ом, а не як нормальний стан.
    """
    cache_key = CACHE_COMPACT_KEY if compact else CACHE_KEY
    good_key = f"{cache_key}:last_known_good"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    try:
        text = _build(compact=compact)
    except Exception as exc:
        logger.exception("Catalog context build failed")
        fallback = None
        try:
            fallback = cache.get(good_key)
        except Exception:
            fallback = None
        _alert_catalog_build_failure(exc, has_fallback=bool(fallback))
        if fallback:
            stale = (
                "[КАТАЛОГ — ОСТАННІЙ ВІДОМИЙ ЗНІМОК] Дані могли змінитись; якщо "
                "клієнт питає про наявність конкретного товару — уточни в менеджера, "
                "не стверджуй.\n" + str(fallback)
            )
            # Короткий кеш, щоб не бити по БД на кожному ході, але значно менший
            # за CACHE_TTL: знімок мусить швидко оновитись після відновлення.
            try:
                cache.set(cache_key, stale, CATALOG_ERROR_TTL)
            except Exception:
                pass
            return stale
        return ""
    try:
        cache.set(cache_key, text, CACHE_TTL)
        if text:
            cache.set(good_key, text, CACHE_LAST_KNOWN_GOOD_TTL)
    except Exception:
        logger.debug("catalog cache unavailable", exc_info=True)
    return text


def _alert_catalog_build_failure(exc: Exception, *, has_fallback: bool) -> None:
    """Один дедуплікований алерт: агент не має лишатись без каталогу молча."""
    try:
        from management.services.ig_alerts import alert_dedupe_key, format_technical_alert
        from management.services.instagram_bot import notify_manager

        notify_manager(
            format_technical_alert(
                "⚠️ IG: не вдалося зібрати контекст каталогу",
                event_type="catalog_build_failed",
                failure_kind=type(exc).__name__,
                instruction_code=(
                    "catalog_stale_snapshot" if has_fallback else "catalog_unavailable"
                ),
            ),
            dedupe_key=alert_dedupe_key("catalog_build_failed", window_minutes=30),
            event_type="catalog_build_failed",
            deliver_immediately=False,
        )
    except Exception:
        logger.debug("catalog build alert unavailable", exc_info=True)
