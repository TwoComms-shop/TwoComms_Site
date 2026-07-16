"""
Fable 5 — сервісні хелпери для ПУБЛІЧНОЇ частини сайту та генераторів фідів.

Ці функції безпечні: якщо для варіанта/кольору немає Fable5-даних —
повертаються нейтральні значення, і сайт працює як раніше.
Див. INTEGRATION.md — як підключити до картки товару, головної та фідів.
"""
from decimal import Decimal, InvalidOperation

from .models import (
    ColorProfile,
    FeedImageRule,
    FeedOnlyImage,
    FeedProductRule,
    FeedProfile,
    GarmentFlow,
    ProductFitNote,
    ProductOptionProfile,
    VariantCombinationProfile,
    VariantDetails,
    VariantFAQ,
    VariantFitRule,
    VariantSizeRule,
)

# Public compatibility exports for code that already imports fable5.services.
from .content_resolution import (  # noqa: E402,F401
    build_combination_key,
    normalize_option_values,
    resolve_merchandising_context,
    resolve_variant_text,
)


def color_is_thermo(color) -> bool:
    profile = ColorProfile.objects.filter(color=color).only("is_thermo").first()
    return bool(profile and profile.is_thermo)


DEFAULT_THERMO_NOTE = "Реагує на тепло — змінює відтінок"
DEFAULT_THERMO_DESCRIPTION = (
    "Термохромна тканина змінює відтінок під дією тепла, тому кожна річ "
    "виглядає по-різному залежно від температури."
)
DEFAULT_THERMO_PRICE_REASON = "Термохромна тканина"


def _decimal(value, default="0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(str(default))


def _effective_fit_rules(variant) -> dict:
    """Resolve product and color fit rules with buyer-facing explanations."""

    product = variant.product
    product_notes = {
        row.fit_code: row
        for row in product.fable5_fit_notes.all()
    }
    color_rules = {
        row.fit_code: row
        for row in variant.fable5_fit_rules.all()
    }
    options = sorted(
        product.fit_options.all(),
        key=lambda option: (option.order, option.id),
    )
    result = {}
    for option in options:
        note = product_notes.get(option.code)
        color_rule = color_rules.get(option.code)
        enabled = bool(option.is_active)
        if note is not None:
            enabled = enabled and bool(note.is_enabled)
        if color_rule is not None:
            enabled = enabled and bool(color_rule.is_enabled)
        reason = ""
        if color_rule is not None:
            reason = (color_rule.reason or "").strip()
        if not reason and note is not None:
            reason = (note.reason or "").strip()
        result[option.code] = {
            "is_enabled": enabled,
            "reason": reason,
            "label": option.label or option.code,
            "is_default": bool(option.is_default),
        }

    enabled = [item for item in result.values() if item["is_enabled"]]
    if len(enabled) == 1:
        only_label = (enabled[0]["label"] or "").strip().lower()
        for item in result.values():
            if not item["is_enabled"] and not item["reason"]:
                item["reason"] = f"Для цього кольору доступний лише {only_label}"
    return result


def _garment_flow_for_product(product):
    category_id = getattr(product, "category_id", None)
    if not category_id:
        return None
    prefetched = getattr(getattr(product, "category", None), "_prefetched_objects_cache", {})
    cached_flows = prefetched.get("fable5_flows")
    if cached_flows is not None:
        return next((flow for flow in cached_flows if flow.is_active), None)
    return (
        GarmentFlow.objects.filter(categories__id=category_id, is_active=True)
        .order_by("id")
        .first()
    )


def _product_option_profiles(product) -> dict:
    prefetched = getattr(product, "_prefetched_objects_cache", {}).get(
        "fable5_option_profiles"
    )
    rows = prefetched if prefetched is not None else product.fable5_option_profiles.all()
    return {row.option_key: row for row in rows}


def _variant_combination_profiles(variant) -> dict:
    prefetched = getattr(variant, "_prefetched_objects_cache", {}).get(
        "fable5_combinations"
    )
    rows = prefetched if prefetched is not None else variant.fable5_combinations.all()
    return {row.combination_key: row for row in rows}


def product_option_context(
    product,
    *,
    variant=None,
    option_values=None,
    lang="uk",
) -> dict:
    """Build the public, category-driven option axes for one product selection."""

    flow = _garment_flow_for_product(product)
    selected = normalize_option_values(option_values or {})
    profiles = _product_option_profiles(product)
    combinations = _variant_combination_profiles(variant) if variant is not None else {}
    fit_rules = _effective_fit_rules(variant) if variant is not None else {}
    axes = []

    for raw_axis in (getattr(flow, "axes", None) or []):
        if not isinstance(raw_axis, dict):
            continue
        axis_code = str(raw_axis.get("code") or "").strip().lower()
        if not axis_code:
            continue
        choices = []
        for raw_choice in raw_axis.get("options") or []:
            if not isinstance(raw_choice, dict):
                continue
            choice_code = str(raw_choice.get("code") or "").strip().lower()
            if not choice_code:
                continue
            values = {axis_code: choice_code}
            option_key = build_combination_key(values)
            profile = profiles.get(option_key)
            combination = combinations.get(option_key)
            enabled = not bool(raw_choice.get("disabled"))
            reason = str(raw_choice.get("disabled_reason") or "").strip()
            is_default = bool(raw_choice.get("default"))

            if profile is not None:
                enabled = enabled and bool(profile.is_active)
                if not enabled and profile.price_delta_reason:
                    reason = profile.price_delta_reason.strip()
            if combination is not None:
                enabled = enabled and bool(combination.is_active)
                if not enabled and combination.price_delta_reason:
                    reason = combination.price_delta_reason.strip()

            if axis_code == "fit" and choice_code in fit_rules:
                fit_rule = fit_rules[choice_code]
                enabled = enabled and bool(fit_rule["is_enabled"])
                is_default = is_default or bool(fit_rule["is_default"])
                if not enabled and fit_rule["reason"]:
                    reason = fit_rule["reason"]

            merchandising = resolve_merchandising_context(
                product,
                variant=variant,
                option_values=values,
                lang=lang,
            )
            choices.append({
                "code": choice_code,
                "label": str(raw_choice.get("label") or choice_code),
                "description": str(raw_choice.get("description") or ""),
                "icon": str(raw_choice.get("icon") or axis_code),
                "is_enabled": enabled,
                "is_default": is_default,
                "reason": reason,
                "price_delta": int(merchandising.get("price_delta") or 0),
                "price_delta_reason": str(
                    merchandising.get("price_delta_reason") or ""
                ),
                "option_values": values,
                "option_key": option_key,
            })

        requested = selected.get(axis_code, "")
        enabled_codes = {item["code"] for item in choices if item["is_enabled"]}
        if requested not in enabled_codes:
            requested = next(
                (
                    item["code"]
                    for item in choices
                    if item["is_enabled"] and item["is_default"]
                ),
                "",
            )
        if not requested:
            requested = next(
                (item["code"] for item in choices if item["is_enabled"]),
                "",
            )
        axes.append({
            "code": axis_code,
            "label": str(raw_axis.get("label") or axis_code),
            "choices": choices,
            "selected_value": requested,
        })

    resolved_values = {
        axis["code"]: axis["selected_value"]
        for axis in axes
        if axis["selected_value"]
    }
    return {
        "flow_code": getattr(flow, "code", "") if flow is not None else "",
        "flow_name": getattr(flow, "name", "") if flow is not None else "",
        "axes": axes,
        "selected_values": resolved_values,
    }


def variant_allows_options(variant, option_values) -> bool:
    try:
        normalized = normalize_option_values(option_values or {})
    except (TypeError, ValueError):
        return False
    context = product_option_context(
        variant.product,
        variant=variant,
        option_values=normalized,
    )
    known_axes = {axis["code"]: axis for axis in context["axes"]}
    for axis_code, choice_code in normalized.items():
        axis = known_axes.get(axis_code)
        if axis is None:
            return False
        choice = next(
            (item for item in axis["choices"] if item["code"] == choice_code),
            None,
        )
        if choice is None or not choice["is_enabled"]:
            return False

    combination_key = build_combination_key(normalized)
    if combination_key:
        combination = VariantCombinationProfile.objects.filter(
            variant=variant,
            combination_key=combination_key,
        ).only("is_active").first()
        if combination is not None and not combination.is_active:
            return False
    return True


def variant_public_context(
    variant,
    *,
    fit_code="",
    option_values=None,
    lang="uk",
) -> dict:
    """Все потрібне для картки товару про конкретний колір:

    - is_thermo + опис термохромної тканини
    - надбавка до ціни (+300 грн) та її причина для покупця
    - фінальна ціна цього кольору (базова зі знижкою + надбавка)
    - per-color SEO (title/description/keywords), відео, FAQ
    - доступність посадок і розмірів з причинами
    """
    fields_cache = getattr(getattr(variant, "_state", None), "fields_cache", {})
    if "fable5_details" in fields_cache:
        details = fields_cache.get("fable5_details")
    else:
        details = VariantDetails.objects.filter(variant=variant).first()
    color_cache = getattr(getattr(variant.color, "_state", None), "fields_cache", {})
    if "fable5_profile" in color_cache:
        profile = color_cache.get("fable5_profile")
    else:
        profile = ColorProfile.objects.filter(color=variant.color).first()
    product = variant.product
    options = normalize_option_values(option_values or {})
    if fit_code and "fit" not in options:
        options["fit"] = str(fit_code).strip().lower()
    merchandising = resolve_merchandising_context(
        product,
        variant=variant,
        option_values=options or None,
        lang=lang,
    )
    product_price = _decimal(getattr(product, "final_price", product.price))
    base_price = _decimal(
        variant.price_override
        if variant.price_override is not None
        else product_price
    )
    price_delta = _decimal(merchandising.get("price_delta", 0))
    final_price = base_price + price_delta
    is_thermo = bool(profile and profile.is_thermo)
    price_reason = (merchandising.get("price_delta_reason") or "").strip()
    if not price_reason and is_thermo and final_price != product_price:
        price_reason = DEFAULT_THERMO_PRICE_REASON
    fit_rules = _effective_fit_rules(variant)

    return {
        "variant_id": variant.id,
        "option_values": options,
        "option_key": build_combination_key(options),
        "is_thermo": is_thermo,
        "thermo_note": (
            ((profile.thermo_note if profile else "") or "").strip()
            or (DEFAULT_THERMO_NOTE if is_thermo else "")
        ),
        "thermo_description": (
            ((profile.description if profile else "") or "").strip()
            or (DEFAULT_THERMO_DESCRIPTION if is_thermo else "")
        ),
        "display_name": merchandising["display_name"],
        "base_product_price": product_price,
        "base_variant_price": base_price,
        "price_delta": price_delta,
        "price_difference": final_price - product_price,
        "price_delta_reason": price_reason,
        "final_price": final_price,
        "has_price_adjustment": final_price != product_price,
        "marketing_html": merchandising["marketing_text"],
        "youtube_url": merchandising["youtube_url"],
        "seo_title": merchandising["seo_title"],
        "seo_description": merchandising["seo_description"],
        "seo_keywords": merchandising["seo_keywords"],
        "faqs": [
            {
                "question_uk": f.question_uk, "question_ru": f.question_ru, "question_en": f.question_en,
                "answer_uk": f.answer_uk, "answer_ru": f.answer_ru, "answer_en": f.answer_en,
            }
            for f in variant.fable5_faqs.all() if f.is_active
        ],
        "fit_rules": fit_rules,
        "available_fit_codes": [
            code for code, rule in fit_rules.items() if rule["is_enabled"]
        ],
        "size_rules": [
            {"fit_code": r.fit_code, "size": r.size, "is_enabled": r.is_enabled, "stock": r.stock}
            for r in variant.fable5_size_rules.all()
        ],
    }


def variant_allows_fit(variant, fit_code: str) -> bool:
    """Return whether the selected color can be purchased in this fit."""

    code = str(fit_code or "").strip().lower()
    if not code:
        return True
    rule = variant_public_context(variant)["fit_rules"].get(code)
    return bool(rule and rule["is_enabled"])


def effective_cart_unit_price(product, color_variant=None, fit_code: str = "") -> Decimal:
    """Authoritative server-side unit price for a cart/order line.

    The colour variant is accepted only when it belongs to ``product``.  This
    prevents a client from posting a cheaper variant ID from another item.
    """

    if color_variant is None:
        return _decimal(getattr(product, "final_price", product.price))
    if getattr(color_variant, "product_id", None) != getattr(product, "id", None):
        return _decimal(getattr(product, "final_price", product.price))
    return _decimal(
        variant_public_context(color_variant, fit_code=fit_code)["final_price"]
    )


def variant_allows_purchase(product, color_variant, *, fit_code: str = "", size: str = "") -> bool:
    """Validate a persisted cart selection against current variant rules."""

    if color_variant is None:
        return True
    if getattr(color_variant, "product_id", None) != getattr(product, "id", None):
        return False
    fit = str(fit_code or "").strip().lower()
    if fit and not variant_allows_fit(color_variant, fit):
        return False

    wanted_size = str(size or "").strip()
    if wanted_size and fit:
        from .size_grid_services import (
            normalize_size_value,
            resolve_effective_sizes,
            resolve_option_size_grid,
        )

        option_key = f"fit={fit}"
        if resolve_option_size_grid(product, option_key, variant=color_variant):
            allowed = {
                normalize_size_value(row.get("size"))
                for row in resolve_effective_sizes(
                    product,
                    option_key,
                    variant=color_variant,
                )
            }
            if normalize_size_value(wanted_size) not in allowed:
                return False
    return variant_allows_size(color_variant, wanted_size, fit)


def product_fit_notes(product) -> dict:
    """Причини недоступності посадок на рівні товару: {code: {is_enabled, reason}}."""
    return {
        n.fit_code: {"is_enabled": n.is_enabled, "reason": n.reason}
        for n in ProductFitNote.objects.filter(product=product)
    }


def disabled_sizes_for_variant(variant, fit_code: str = "") -> list:
    """Список вимкнених розмірів для кольору (і опційно посадки)."""
    rules = VariantSizeRule.objects.filter(variant=variant, is_enabled=False)
    if fit_code:
        rules = rules.filter(fit_code__in=["", fit_code])
    return sorted({r.size for r in rules})


def variant_allows_size(variant, size: str, fit_code: str = "") -> bool:
    """Resolve general and fit-specific colour size availability."""

    wanted_size = str(size or "").strip().upper()
    wanted_fit = str(fit_code or "").strip().lower()
    if not wanted_size:
        return True
    rules = list(
        VariantSizeRule.objects
        .filter(variant=variant, size__iexact=wanted_size, fit_code__in=["", wanted_fit])
        .order_by("id")
    )
    if not rules:
        return True
    general = next((rule for rule in reversed(rules) if not rule.fit_code), None)
    specific = next((rule for rule in reversed(rules) if rule.fit_code == wanted_fit), None)
    rule = specific or general
    return bool(rule and rule.is_enabled and (rule.stock is None or rule.stock > 0))


# ---------------------------------------------------------------------------
# Фіди
# ---------------------------------------------------------------------------

def feed_includes_product(feed_slug: str, product) -> bool:
    """Чи входить товар у фід. Використовуйте в генераторах фідів."""
    feed = FeedProfile.objects.filter(slug=feed_slug, is_active=True).first()
    if feed is None:
        return True  # фід не керується Fable5 — поводимося як раніше
    rule = FeedProductRule.objects.filter(feed=feed, product=product).first()
    if rule is not None:
        return rule.is_included
    return feed.default_include


def feed_image_urls(feed_slug: str, product, default_urls=None) -> list:
    """Список URL картинок для товару в конкретному фіді.

    Якщо є явно дозволені правила — повертаються ТІЛЬКИ вони (у вказаному
    порядку) + фід-тільки картинки. Інакше — default_urls (поточна поведінка
    генератора) + фід-тільки картинки.
    """
    def _url(field):
        try:
            return field.url if field else ""
        except Exception:
            return ""

    defaults = [u for u in (default_urls or []) if u]
    feed = FeedProfile.objects.filter(slug=feed_slug, is_active=True).first()
    if feed is None:
        return defaults

    urls: list = []
    allowed = list(
        FeedImageRule.objects.filter(feed=feed, product=product, is_allowed=True)
        .select_related("product_image", "color_image")
        .order_by("order", "id")
    )
    for rule in allowed:
        if rule.use_main_image:
            url = _url(product.main_image)
        elif rule.product_image_id:
            url = _url(rule.product_image.image)
        elif rule.color_image_id:
            url = _url(rule.color_image.image)
        else:
            url = ""
        if url and url not in urls:
            urls.append(url)
    if not urls:
        urls = defaults
    from django.db.models import Q
    extra = FeedOnlyImage.objects.filter(product=product).filter(
        Q(feed__isnull=True) | Q(feed=feed)
    )
    for im in extra:
        url = _url(im.image)
        if url and url not in urls:
            urls.append(url)
    return urls
