"""
Fable 5 — сервісні хелпери для ПУБЛІЧНОЇ частини сайту та генераторів фідів.

Ці функції безпечні: якщо для варіанта/кольору немає Fable5-даних —
повертаються нейтральні значення, і сайт працює як раніше.
Див. INTEGRATION.md — як підключити до картки товару, головної та фідів.
"""
from .models import (
    ColorProfile,
    FeedImageRule,
    FeedOnlyImage,
    FeedProductRule,
    FeedProfile,
    ProductFitNote,
    VariantDetails,
    VariantFAQ,
    VariantFitRule,
    VariantSizeRule,
)


def color_is_thermo(color) -> bool:
    profile = ColorProfile.objects.filter(color=color).only("is_thermo").first()
    return bool(profile and profile.is_thermo)


def variant_public_context(variant) -> dict:
    """Все потрібне для картки товару про конкретний колір:

    - is_thermo + опис термохромної тканини
    - надбавка до ціни (+300 грн) та її причина для покупця
    - фінальна ціна цього кольору (базова зі знижкою + надбавка)
    - per-color SEO (title/description/keywords), відео, FAQ
    - доступність посадок і розмірів з причинами
    """
    details = VariantDetails.objects.filter(variant=variant).first()
    profile = ColorProfile.objects.filter(color=variant.color).first()
    product = variant.product

    base_price = variant.price_override if variant.price_override is not None else None
    if base_price is None:
        try:
            base_price = product.final_price
        except Exception:
            base_price = product.price
    price_delta = details.price_delta if details else 0

    return {
        "variant_id": variant.id,
        "is_thermo": bool(profile and profile.is_thermo),
        "thermo_note": (profile.thermo_note if profile else "") or "",
        "thermo_description": (profile.description if profile else "") or "",
        "display_name": (details.display_name if details else "") or product.title,
        "price_delta": price_delta,
        "price_delta_reason": (details.price_delta_reason if details else "") or "",
        "final_price": (base_price or 0) + price_delta,
        "marketing_html": (details.marketing_html if details else "") or "",
        "youtube_url": (details.youtube_url if details else "") or getattr(product, "video_url", "") or "",
        "seo_title": (details.seo_title if details else "") or "",
        "seo_description": (details.seo_description if details else "") or "",
        "seo_keywords": (details.seo_keywords if details else "") or "",
        "faqs": [
            {
                "question_uk": f.question_uk, "question_ru": f.question_ru, "question_en": f.question_en,
                "answer_uk": f.answer_uk, "answer_ru": f.answer_ru, "answer_en": f.answer_en,
            }
            for f in VariantFAQ.objects.filter(variant=variant, is_active=True)
        ],
        "fit_rules": {
            r.fit_code: {"is_enabled": r.is_enabled, "reason": r.reason}
            for r in VariantFitRule.objects.filter(variant=variant)
        },
        "size_rules": [
            {"fit_code": r.fit_code, "size": r.size, "is_enabled": r.is_enabled, "stock": r.stock}
            for r in VariantSizeRule.objects.filter(variant=variant)
        ],
    }


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
