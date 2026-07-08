"""
SEO утилиты для автоматической генерации мета-тегов и ключевых слов
"""
import re
import json
import os
from datetime import timedelta
from decimal import Decimal
from typing import List, Dict, Optional
from urllib.parse import urljoin
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import get_language
from .models import Product, Category
from .services.size_guides import resolve_product_sizes
from .services.policy import (
    APPLICABLE_COUNTRY,
    CURRENCY,
    RETURN_POLICY,
    SHIPPING_TIERS,
    shipping_tiers_as_dicts,
)

SITE_BASE_URL = getattr(settings, 'SITE_BASE_URL', 'https://twocomms.shop')
if not SITE_BASE_URL.endswith('/'):
    SITE_BASE_URL += '/'

DEFAULT_SOCIAL_IMAGE_PATH = "static/img/social-preview.jpg"


def _build_absolute_url(path: str) -> str:
    """Возвращает абсолютный URL, используя SITE_BASE_URL как базу"""
    if not path:
        return SITE_BASE_URL
    if path.startswith(('http://', 'https://')):
        return path
    return urljoin(SITE_BASE_URL, path.lstrip('/'))


def get_default_social_image_url() -> str:
    return _build_absolute_url(DEFAULT_SOCIAL_IMAGE_PATH)


def _clean_text(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", str(raw))
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text

    cut = text[: max(0, limit - 3)].rstrip(" ,.;:-")
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return f"{cut}..."


# ADS-3: лимит <title>. Google показывает ~600px ≈ 65-70 символов; прежний
# жёсткий limit=60 с «...» давал «недописанные» тайтлы в SERP и соцпревью.
TITLE_LIMIT = 70


def _fit_title(text: str, limit: int = TITLE_LIMIT) -> str:
    """Подгоняет <title> под лимит БЕЗ многоточия.

    ADS-3: «...» в тайтле выглядит как обрыв (SERP сам ставит своё
    многоточие при переполнении по пикселям). Режем по границе слова и
    чистим висящие разделители («|», «—», «-») в конце.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-–—|(«\"'")


def _ai_enabled(obj) -> bool:
    """Returns True only if the object explicitly opted into AI-driven SEO.

    AI content (ai_keywords / ai_description) is used in fallbacks ONLY when
    the corresponding ai_generation_enabled flag is True. Otherwise we treat
    those fields as if they were empty.
    """
    return bool(getattr(obj, "ai_generation_enabled", False))


def _pick_product_description_source(product: Product) -> str:
    """Cascade through description fields in priority order.

    AI description is consulted only when the product opted into AI generation.
    """
    candidates = ["seo_description", "short_description"]
    if _ai_enabled(product):
        candidates.append("ai_description")
    candidates.extend(["full_description", "description"])
    for attr in candidates:
        value = _clean_text(getattr(product, attr, ""))
        if value:
            return value
    return ""


def _guess_product_material(product: Product) -> str:
    lookup_source = " ".join(
        filter(
            None,
            [
                getattr(product, "title", ""),
                getattr(getattr(product, "category", None), "name", ""),
                getattr(product, "slug", ""),
            ],
        )
    ).lower()

    if any(token in lookup_source for token in ("худі", "hoodie", "світшот", "sweatshirt")):
        return "трьохнитка"
    if "лонг" in lookup_source or "long" in lookup_source:
        return "бавовна"
    return "бавовна"


def _safe_product_display_image(product) -> object | None:
    main_image = getattr(product, "main_image", None)
    if main_image:
        return main_image

    try:
        return getattr(product, "display_image", None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Homepage commerce signals (US-16 — eliminate Google price-snippet
# hallucination "200 200 грн" by providing explicit priceRange and
# AggregateOffer derived from real Product data).
# ---------------------------------------------------------------------------


def _homepage_price_aggregate() -> Dict[str, object]:
    """Compute live low/high price + offer count across the catalogue.

    Used both inside ``Organization.priceRange`` (text form) and as the
    standalone ``AggregateOffer`` node on the homepage. Errors during
    aggregation are non-fatal — Google handles a missing AggregateOffer
    gracefully, but we never want a 500 on the home view because of a
    schema helper. Returns an empty dict on any DB failure.
    """
    try:
        active_qs = Product.objects.filter(price__isnull=False).only(
            "price", "discount_percent"
        )
        prices: List[int] = []
        for product in active_qs:
            base_price = int(product.price or 0)
            if base_price <= 0:
                continue
            if product.discount_percent and product.discount_percent > 0:
                final_price = int(base_price * (100 - product.discount_percent) / 100)
            else:
                final_price = base_price
            if final_price > 0:
                prices.append(final_price)
        if not prices:
            return {}
        return {
            "lowPrice": min(prices),
            "highPrice": max(prices),
            "offerCount": len(prices),
        }
    except Exception:
        return {}


def _homepage_price_range_text() -> str:
    """Render ``priceRange`` as a single string ("880-2550 UAH")."""
    aggregate = _homepage_price_aggregate()
    if not aggregate:
        # Fall back to the published catalogue range from the brand brief
        # (BrandDNA / SEO/napolke-veteran-brands-2026.md) so the property
        # is never empty even when the DB is unavailable.
        return "880-2550 UAH"
    return f"{aggregate['lowPrice']}-{aggregate['highPrice']} UAH"


def _organization_same_as() -> List[str]:
    """Curated ``sameAs`` list for Organization / OnlineStore.

    Composes confirmed social profiles in a stable order so the
    Knowledge Graph can deduplicate the entity. Owner-confirmed
    handles only — placeholder/empty values from settings are
    silently dropped, never emitted as fake links.

    Configurable additions go through environment variables (read at
    request time so a server restart is enough — no code change):

    * ``BRAND_INSTAGRAM_URL`` (default https://instagram.com/twocomms)
    * ``BRAND_TELEGRAM_URL`` (default https://t.me/twocomms)
    * ``BRAND_TIKTOK_URL``
    * ``BRAND_THREADS_URL``
    * ``BRAND_FACEBOOK_URL``
    * ``BRAND_YOUTUBE_URL``
    * ``BRAND_PINTEREST_URL``
    * ``BRAND_TWITTER_URL`` (X / Twitter)
    * ``BRAND_LINKEDIN_URL``
    * ``BRAND_WIKIDATA_URL`` (full https://www.wikidata.org/wiki/Q… URL)

    Each value must start with ``http://`` or ``https://`` to land in
    the schema; anything else is silently skipped. The Instagram +
    Telegram defaults match the verified handles already published in
    the site footer so the helper is non-empty out of the box.
    """

    def _read(env_key: str, default: str = "") -> str:
        value = (
            getattr(settings, env_key, "")
            or os.environ.get(env_key, "")
            or default
        ).strip()
        if value and value.startswith(("http://", "https://")):
            return value
        return ""

    candidates = [
        _read("BRAND_INSTAGRAM_URL", default="https://instagram.com/twocomms"),
        _read("BRAND_TELEGRAM_URL", default="https://t.me/twocomms"),
        _read("BRAND_TIKTOK_URL"),
        _read("BRAND_THREADS_URL"),
        _read("BRAND_FACEBOOK_URL"),
        _read("BRAND_YOUTUBE_URL"),
        _read("BRAND_PINTEREST_URL"),
        _read("BRAND_TWITTER_URL"),
        _read("BRAND_LINKEDIN_URL"),
        _read("BRAND_WIKIDATA_URL"),
    ]
    seen = set()
    out: List[str] = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


class SEOKeywordGenerator:
    """Генератор ключевых слов на основе анализа контента"""

    # Базовые ключевые слова для магазина одежды
    BASE_KEYWORDS = [
        'стріт одяг', 'мілітарі одяг', 'футболки', 'худі', 'лонгсліви',
        'ексклюзивний дизайн', 'якісний одяг', 'одяг з характером',
        'стріт стиль', 'мілітарі стиль', 'TwoComms', 'купити одяг онлайн',
        'модний одяг', 'трендовий одяг', 'український бренд'
    ]

    # Ключевые слова по категориям
    CATEGORY_KEYWORDS = {
        'футболки': ['чоловічі футболки', 'жіночі футболки', 'базові футболки', 'принтовані футболки'],
        'худі': ['чоловічі худі', 'жіночі худі', 'базові худі', 'принтовані худі'],
        'лонгсліви': ['чоловічі лонгсліви', 'жіночі лонгсліви', 'базові лонгсліви', 'принтовані лонгсліви']
    }

    # Ключевые слова по цветам
    COLOR_KEYWORDS = {
        'чорний': ['чорна футболка', 'чорне худі', 'чорний лонгслів'],
        'білий': ['біла футболка', 'біле худі', 'білий лонгслів'],
        'сірий': ['сіра футболка', 'сіре худі', 'сірий лонгслів'],
        'зелений': ['зелена футболка', 'зелене худі', 'зелений лонгслів'],
        'синій': ['синя футболка', 'синє худі', 'синій лонгслів']
    }

    @classmethod
    def extract_keywords_from_text(cls, text: str) -> List[str]:
        """Извлекает ключевые слова из текста"""
        if not text:
            return []

        # Очищаем текст от HTML тегов
        clean_text = re.sub(r'<[^>]+>', '', text)

        # Разбиваем на слова и фильтруем
        words = re.findall(r'\b[а-яіїєґ]{3,}\b', clean_text.lower())

        # Убираем стоп-слова
        stop_words = {
            'для', 'від', 'до', 'на', 'у', 'з', 'по', 'про', 'та', 'або', 'але',
            'як', 'що', 'де', 'коли', 'чому', 'який', 'яка', 'яке', 'які',
            'можна', 'потрібно', 'варто', 'краще', 'добре', 'гарний', 'якісний'
        }

        keywords = [word for word in words if word not in stop_words and len(word) > 2]

        # Подсчитываем частоту
        word_freq = {}
        for word in keywords:
            word_freq[word] = word_freq.get(word, 0) + 1

        # Сортируем по частоте и возвращаем топ-10
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [word for word, freq in sorted_words[:10]]

    @classmethod
    def generate_product_keywords(cls, product: Product) -> List[str]:
        """Генерирует ключевые слова для товара"""
        keywords = []

        # Базовые ключевые слова
        keywords.extend(cls.BASE_KEYWORDS)

        # Ключевые слова категории
        if product.category:
            category_name = product.category.name.lower()
            if category_name in cls.CATEGORY_KEYWORDS:
                keywords.extend(cls.CATEGORY_KEYWORDS[category_name])

        # Ключевые слова из названия товара
        title_keywords = cls.extract_keywords_from_text(product.title)
        keywords.extend(title_keywords)

        # Ключевые слова из описания
        if product.description:
            desc_keywords = cls.extract_keywords_from_text(product.description)
            keywords.extend(desc_keywords)

        # Ключевые слова по цветам (если есть цветовые варианты)
        try:
            from storefront.services.catalog_helpers import get_product_color_variant_rows
            for variant in get_product_color_variant_rows(product):
                if variant.color and variant.color.name:
                    color_name = variant.color.name.lower()
                    if color_name in cls.COLOR_KEYWORDS:
                        keywords.extend(cls.COLOR_KEYWORDS[color_name])
        except Exception:
            pass

        # Добавляем сохраненные AI-ключевые слова ТОЛЬКО если AI явно включен
        if _ai_enabled(product) and getattr(product, 'ai_keywords', None):
            ai_keywords = [kw.strip() for kw in product.ai_keywords.split(',') if kw.strip()]
            for kw in ai_keywords:
                if kw and kw not in keywords:
                    keywords.append(kw)

        return list(dict.fromkeys(keywords))[:20]  # Максимум 20 ключевых слов

    @classmethod
    def generate_product_keywords_ai(cls, product: Product) -> List[str]:
        """Генерирует ключевые слова для товара с помощью OpenAI (если доступно)"""
        model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
        api_key = getattr(settings, 'OPENAI_API_KEY', None) or os.environ.get('OPENAI_API_KEY')
        if not api_key:
            return []
        try:
            import openai
            client = openai.OpenAI(api_key=api_key)
        except Exception:
            return []

        color_names = []
        try:
            from storefront.services.catalog_helpers import get_product_color_variant_rows
            for variant in get_product_color_variant_rows(product):
                if variant.color and getattr(variant.color, 'name', None):
                    color_names.append(variant.color.name)
        except Exception:
            color_names = []

        prompt = (
            f"Створіть до 20 SEO ключових слів для товару. Назва: {product.title}. "
            f"Категорія: {product.category.name if product.category else 'N/A'}. "
            f"Опис: {product.description or ''}. "
            f"Кольори: {', '.join(color_names) if color_names else 'N/A'}. "
            "Виведіть ключові слова через кому."
        )
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Ви - генератор SEO ключових слів."},
                    {"role": "user", "content": prompt},
                ],
            )
            text = response.choices[0].message.get('content', '') if hasattr(response, 'choices') else ''
        except Exception:
            return []
        if not text:
            return []
        raw = [t.strip() for t in text.replace('\n', ',').split(',') if t.strip()]
        seen = set()
        keywords = []
        for w in raw:
            lw = w.lower()
            if lw not in seen:
                seen.add(lw)
                keywords.append(w)
            if len(keywords) >= 20:
                break
        return keywords

    @classmethod
    def generate_category_keywords(cls, category: Category) -> List[str]:
        """Генерирует ключевые слова для категории"""
        keywords = []

        # Базовые ключевые слова
        keywords.extend(cls.BASE_KEYWORDS)

        # Ключевые слова категории
        category_name = category.name.lower()
        if category_name in cls.CATEGORY_KEYWORDS:
            keywords.extend(cls.CATEGORY_KEYWORDS[category_name])

        # Ключевые слова из описания категории
        if category.description:
            desc_keywords = cls.extract_keywords_from_text(category.description)
            keywords.extend(desc_keywords)

        # Добавляем сохраненные AI-ключевые слова ТОЛЬКО если AI явно включен
        if _ai_enabled(category) and getattr(category, 'ai_keywords', None):
            ai_keywords = [kw.strip() for kw in category.ai_keywords.split(',') if kw.strip()]
            for kw in ai_keywords:
                if kw and kw not in keywords:
                    keywords.append(kw)

        return list(dict.fromkeys(keywords))[:15]

    @classmethod
    def generate_category_keywords_ai(cls, category: Category) -> List[str]:
        """Генерирует ключевые слова для категории с помощью OpenAI (если доступно)"""
        model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
        api_key = getattr(settings, 'OPENAI_API_KEY', None) or os.environ.get('OPENAI_API_KEY')
        if not api_key:
            return []
        try:
            import openai
            client = openai.OpenAI(api_key=api_key)
        except Exception:
            return []
        prompt = (
            f"Створіть до 20 SEO ключових слів для категорії '{category.name}'. "
            f"Опис: {category.description or ''}."
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Ви - генератор SEO ключових слів."},
                    {"role": "user", "content": prompt},
                ],
            )
            text = resp.choices[0].message.get('content', '') if hasattr(resp, 'choices') else ''
        except Exception:
            return []
        if not text:
            return []
        raw = [t.strip() for t in text.replace('\n', ',').split(',') if t.strip()]
        seen = set()
        keywords = []
        for w in raw:
            lw = w.lower()
            if lw not in seen:
                seen.add(lw)
                keywords.append(w)
            if len(keywords) >= 20:
                break
        return keywords

    @classmethod
    def generate_meta_description(cls, product: Product) -> str:
        """Генерирует мета-описание для товара"""
        stored_description = _clean_text(getattr(product, "seo_description", ""))
        if not stored_description and _ai_enabled(product):
            stored_description = _clean_text(getattr(product, "ai_description", ""))
        if stored_description:
            return _truncate_at_word_boundary(stored_description, 160)

        category_label = product.category.name.lower() if product.category else "одяг"
        source_description = _pick_product_description_source(product)
        teaser = _truncate_at_word_boundary(source_description, 84) if source_description else f"Якісний {category_label} з характером і швидким відвантаженням."
        parts = [
            product.title,
            teaser,
            f"Ціна від {product.final_price} грн.",
            "Доставка по Україні.",
        ]
        return _truncate_at_word_boundary(" ".join(part for part in parts if part), 160)

    @classmethod
    def generate_meta_title(cls, product: Product) -> str:
        """Генерирует мета-заголовок для товара"""
        stored_title = _clean_text(getattr(product, "seo_title", ""))
        if stored_title:
            # ADS-3: было limit=60 c «...» — обрубленные тайтлы в SERP
            return _fit_title(stored_title)

        # SEO 2026-05-19 (VILNI deep review §13.13 — PDP title
        # optimisation for low-CTR products). The «бентежне» print
        # series ships under product slugs prefixed with
        # ``bentejne-``. Their default titles read like internal
        # codes («Бентежне худі 02») and bury the actual commercial
        # intent (apparel category + print theme). Prefer a clearer
        # commercial template so SERP recovery is possible:
        #   «Принт «Бентежне» — {category} | TwoComms»
        # The product.title stays untouched for use in product cards;
        # this only affects the <title> tag and OG titles.
        slug = (getattr(product, "slug", "") or "").lower()
        if slug.startswith("bentejne-") and product.category:
            patterned = (
                f"Принт «Бентежне» — {product.category.name} | TwoComms"
            )
            return _fit_title(patterned)

        # ADS-3: деградируем аккуратно — если полный шаблон с категорией не
        # влезает, пробуем без категории; и только потом режем по слову.
        # Раньше: жёсткий limit=60 + «...» → обрубки в SERP.
        if product.category:
            with_category = f"{product.title} ({product.category.name}) - TwoComms"
            if len(with_category) <= TITLE_LIMIT:
                return with_category

        title = f"{product.title} - TwoComms"
        if len(title) <= TITLE_LIMIT:
            return title

        return _fit_title(str(product.title))


class SEOMetaGenerator:
    """Генератор SEO мета-тегов"""

    @staticmethod
    def generate_product_meta(product: Product) -> Dict[str, str]:
        """Генерирует все мета-теги для товара"""
        keywords = _clean_text(getattr(product, "seo_keywords", "")) or ", ".join(SEOKeywordGenerator.generate_product_keywords(product))
        description = SEOKeywordGenerator.generate_meta_description(product)
        title = SEOKeywordGenerator.generate_meta_title(product)

        image_url = get_default_social_image_url()
        display_image = _safe_product_display_image(product)
        if display_image:
            image_url = _build_absolute_url(display_image.url)

        return {
            'title': title,
            'description': description,
            'keywords': keywords,
            'og_title': title,
            'og_description': description,
            'og_image': image_url,
            'twitter_title': title,
            'twitter_description': description,
            'twitter_image': image_url,
        }

    @staticmethod
    def generate_category_meta(category: Category) -> Dict[str, str]:
        """Генерирует мета-теги для категории"""
        keywords = SEOKeywordGenerator.generate_category_keywords(category)

        title = f"{category.name} - TwoComms"

        # Используем сохраненное AI-описание ТОЛЬКО если AI явно включен для категории
        if _ai_enabled(category) and getattr(category, 'ai_description', None):
            description = category.ai_description[:160]
        else:
            description = f"Купити {category.name.lower()} в TwoComms. Якісний одяг з ексклюзивним дизайном. Швидка доставка по Україні."

            if category.description:
                description = _truncate_at_word_boundary(_clean_text(category.description), 160)

        cover_url = get_default_social_image_url()
        if category.cover:
            cover_url = _build_absolute_url(category.cover.url)

        return {
            'title': title,
            'description': _truncate_at_word_boundary(_clean_text(description), 160),
            'keywords': ', '.join(keywords),
            'og_title': title,
            'og_description': _truncate_at_word_boundary(_clean_text(description), 160),
            'og_image': cover_url,
            'twitter_title': title,
            'twitter_description': _truncate_at_word_boundary(_clean_text(description), 160),
            'twitter_image': cover_url,
        }


class StructuredDataGenerator:
    """Генератор структурированных данных (Schema.org)"""

    # Phase 5: SHIPPING_OPTIONS is now backed by the canonical
    # SHIPPING_TIERS dataclasses in ``services/policy.py``. Leaving
    # this attribute as a list-of-dicts for backwards compatibility
    # with any external code that imported it.
    SHIPPING_OPTIONS = shipping_tiers_as_dicts()

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        formatted = format(value, 'f')
        return formatted.rstrip('0').rstrip('.') if '.' in formatted else formatted

    @staticmethod
    def _get_return_shipping_amount() -> Dict:
        rates: List[Decimal] = []
        for option in StructuredDataGenerator.SHIPPING_OPTIONS:
            rate = option.get("rate")
            if rate is None:
                continue
            try:
                rates.append(Decimal(str(rate)))
            except Exception:
                continue

        if not rates:
            value = "0"
        else:
            min_rate = min(rates)
            value = StructuredDataGenerator._format_decimal(min_rate)

        return {
            "@type": "MonetaryAmount",
            "value": value,
            "currency": CURRENCY,
        }

    @staticmethod
    def _get_dynamic_price_valid_until() -> str:
        """Returns a priceValidUntil date 90 days from today in ISO format."""
        return (timezone.now().date() + timedelta(days=90)).isoformat()

    @staticmethod
    def _get_product_availability(product: Product) -> str:
        """Return the canonical Schema.org availability URI for a product.

        SEO v1.0 Phase 11 (2026-05-12) — finding (QQ). Live prod observation
        showed *every* PDP returning ``OutOfStock`` because the per-colour
        ``ProductColorVariant.stock`` row defaults to 0 in the admin and the
        old logic only flipped to ``InStock`` when at least one variant had
        an explicit positive stock count. TwoComms produces every garment
        on demand via DTF print after the order is placed, so the legacy
        boolean «do we hold a finished SKU on the shelf?» is the wrong
        question — and emitting OutOfStock blocks the catalogue from
        Google Shopping, Facebook Catalog, Pinterest Rich Pins and
        Perplexity / SGE shopping answers.

        SEO 2026-06-04 — finding (GSC availability). Search Console started
        flagging «Недопустимое перечислимое значение в поле availability»
        on every PDP, plus the critical «set one of hasVariant.offers /
        review / aggregateRating» error. Root cause: the previous revision
        emitted ``https://schema.org/MadeToOrder`` for on-demand items.
        Although ``MadeToOrder`` is a real schema.org enum, Google's
        Product / merchant-listing spec does **not** accept it — the
        documented ``availability`` enum is limited to BackOrder,
        Discontinued, InStock, InStoreOnly, LimitedAvailability,
        OnlineOnly, OutOfStock, PreOrder, PreSale and SoldOut
        (developers.google.com/search/docs/appearance/structured-data/
        product-snippet, updated 2026-02-20). An invalid ``availability``
        makes Google discard the whole ``Offer``, which in turn leaves the
        Product / ProductGroup with none of the required
        offers/review/aggregateRating triplet → critical error.

        New rules (in priority order):

        1. ``is_dropship_available=False`` → admin explicitly disabled
           sale of the product → OutOfStock (true negative).
        2. Any colour variant with ``stock > 0`` → InStock.
        3. Variants exist but all stock=0 → InStock. TwoComms is a
           made-to-order DTF shop: the item is always purchasable and
           ships in the 3–5 day window already declared via
           ``deliveryLeadTime`` / ``OfferShippingDetails.deliveryTime``.
           ``InStock`` is the Google-accepted value that keeps the
           product purchasable without the «out of stock» badge.
        4. No variants at all → InStock (legacy fallback for products
           authored before the colour matrix was introduced).
        """
        if not getattr(product, "is_dropship_available", True):
            return "https://schema.org/OutOfStock"

        # Every other state collapses to InStock: a made-to-order DTF
        # garment is always available to buy regardless of the per-variant
        # stock counter (which admins rarely maintain). Kept the variant
        # lookup out of the hot path entirely — the answer no longer
        # depends on it, so we avoid an extra query per schema render.
        return "https://schema.org/InStock"

    @staticmethod
    def _build_shipping_delivery_time() -> Dict:
        return {
            "@type": "ShippingDeliveryTime",
            "businessDays": {
                "@type": "OpeningHoursSpecification",
                "dayOfWeek": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
            },
            "cutoffTime": "14:00",
            "handlingTime": {
                "@type": "QuantitativeValue",
                "minValue": 1,
                "maxValue": 2,
                "unitCode": "DAY"
            },
            "transitTime": {
                "@type": "QuantitativeValue",
                "minValue": 1,
                "maxValue": 5,
                "unitCode": "DAY"
            }
        }

    @staticmethod
    def _get_weight_based_shipping_details() -> List[Dict]:
        shipping_details: List[Dict] = []
        for option in StructuredDataGenerator.SHIPPING_OPTIONS:
            weight_spec: Dict[str, object] = {"@type": "QuantitativeValue", "unitCode": "KGM"}
            if "min_weight" in option:
                weight_spec["minValue"] = option["min_weight"]
            if "max_weight" in option:
                weight_spec["maxValue"] = option["max_weight"]

            shipping_details.append({
                "@type": "OfferShippingDetails",
                "shippingRate": {
                    "@type": "MonetaryAmount",
                    "value": option["rate"],
                    "currency": CURRENCY,
                },
                "shippingDestination": {
                    "@type": "DefinedRegion",
                    "addressCountry": APPLICABLE_COUNTRY,
                },
                "deliveryTime": StructuredDataGenerator._build_shipping_delivery_time(),
                "shippingWeight": weight_spec,
            })

        return shipping_details

    @staticmethod
    def generate_product_schema(
        product: Product,
        canonical_path: Optional[str] = None,
        selected_variant=None,
        review_summary=None,
    ) -> Dict:
        """Генерирует Product schema для товара (совместимо с Google Merchant Center).

        Phase 21 (2026-05-10) — accepts ``canonical_path`` and
        ``selected_variant`` so schema ``url`` and ``image`` track the
        page's canonical strategy:
          * On a self-canonical colour/fit page, ``url`` is the variant
            URL (matches ``<link rel=canonical>``).
          * On a size-only page (canonical → base) or the base page,
            ``url`` is the base product URL.
          * If ``selected_variant`` (a ``ProductColorVariant``) is
            provided, its images are surfaced first so OG/Twitter and
            schema ``image`` reflect the visual on the page.
        """
        # Базовые изображения
        images: list[str] = []

        # Phase 21 — variant images first when a variant is selected
        # so the ``image[0]`` slot (used for OG/Twitter) matches the
        # rendered hero image on a colour-variant PDP.
        if selected_variant is not None:
            try:
                for img in selected_variant.images.all()[:3]:
                    candidate = _build_absolute_url(img.image.url)
                    if candidate not in images:
                        images.append(candidate)
            except Exception:
                pass

        display_image = _safe_product_display_image(product)
        if display_image:
            candidate = _build_absolute_url(display_image.url)
            if candidate not in images:
                images.append(candidate)

        # Добавляем дополнительные изображения
        try:
            for img in product.images.all()[:4]:  # Максимум 4 дополнительных изображения
                candidate = _build_absolute_url(img.image.url)
                if candidate not in images:
                    images.append(candidate)
        except Exception:
            pass

        # Добавляем изображения из цветовых вариантов (для остальных вариантов)
        try:
            from storefront.services.catalog_helpers import get_product_color_variant_rows
            color_variants = get_product_color_variant_rows(product)
            for variant in color_variants[:2]:  # Максимум 2 цветовых варианта
                if selected_variant is not None and getattr(variant, 'pk', None) == getattr(selected_variant, 'pk', None):
                    continue
                for img in variant.images.all()[:2]:
                    candidate = _build_absolute_url(img.image.url)
                    if candidate not in images:
                        images.append(candidate)
        except Exception:
            pass

        if not images:
            images.append(get_default_social_image_url())

        description = _truncate_at_word_boundary(
            _pick_product_description_source(product)
            or f"Якісний {product.category.name.lower() if product.category else 'одяг'} з ексклюзивним дизайном від TwoComms",
            320,
        )
        material = _guess_product_material(product)

        # Phase 21 — schema URL must mirror the page's canonical strategy.
        # ``canonical_path`` (if provided by the view) already reflects
        # whether the variant page is self-canonical or collapses to the
        # base product. Falls back to base for callers that don't yet
        # pass it (e.g. Merchant feed, sitemap-images).
        if canonical_path:
            cp = canonical_path.lstrip("/")
            product_canonical_url = _build_absolute_url(cp)
        else:
            product_canonical_url = _build_absolute_url(f"product/{product.slug}/")

        schema = {
            "@context": "https://schema.org",
            "@type": "Product",
            # SEO molecular-upgrade US-8 (2026-05-16) — stable @id so AI
            # knowledge graphs can deduplicate Product entities across
            # variant URLs and sitemap rebuilds. Anchored to canonical
            # URL so colour/fit variants (whose canonical_path includes
            # the variant slug) get their own ID; size-only facets
            # collapse to the base.
            "@id": f"{product_canonical_url}#product",
            # SEO v1.1 (2026-05-16) — Phase 17v. Derive inLanguage from
            # the active locale so /ru/ and /en/ Product schemas declare
            # the correct language. Previously hardcoded uk-UA, which
            # caused Google Search Console to flag /ru/ and /en/ as
            # "schema language doesn't match page language".
            "inLanguage": StructuredDataGenerator._resolve_inlanguage_code(),
            "name": product.title,
            "description": description,
            "sku": f"TC-{product.id}",
            "mpn": f"TC-{product.id}",  # Manufacturer Part Number
            "url": product_canonical_url,
            "image": images[0] if len(images) == 1 else images,
            "material": StructuredDataGenerator._localized_attr(
                product, "material", fallback=str(material or "")
            ),
            "countryOfOrigin": {
                "@type": "Country",
                "name": _("Україна"),
            },
            "additionalProperty": [
                {
                    "@type": "PropertyValue",
                    "name": _("Матеріал"),
                    "value": StructuredDataGenerator._localized_attr(
                        product, "material", fallback=str(material or "")
                    ),
                },
                {
                    "@type": "PropertyValue",
                    "name": _("Країна виробництва"),
                    "value": _("Україна"),
                },
                {
                    "@type": "PropertyValue",
                    "name": _("Стиль"),
                    "value": _("Стріт & Мілітарі"),
                },
            ],
            "brand": {
                "@id": f"{_build_absolute_url('')}#brand"
            },
            "manufacturer": {
                "@id": f"{_build_absolute_url('')}#organization"
            },
            "offers": {
                "@type": "Offer",
                "price": str(product.final_price),
                "priceCurrency": "UAH",
                "availability": StructuredDataGenerator._get_product_availability(product),
                "itemCondition": "https://schema.org/NewCondition",
                "url": product_canonical_url,
                "priceValidUntil": StructuredDataGenerator._get_dynamic_price_valid_until(),
                # SEO 2026-05-19 (VILNI deep review §4.5, §13.12) — explicit
                # production/handling window for made-to-order DTF print so
                # Google Shopping and AI Search can compare apples-to-apples
                # against in-stock competitors. 3–5 days mirrors the value
                # already exposed in ``OfferShippingDetails.deliveryTime``
                # and the /delivery/ page copy.
                "deliveryLeadTime": {
                    "@type": "QuantitativeValue",
                    "minValue": 3,
                    "maxValue": 5,
                    "unitCode": "DAY",
                },
                "hasMerchantReturnPolicy": {
                    "@type": "MerchantReturnPolicy",
                    "returnPolicyCategory": RETURN_POLICY["category"],
                    "merchantReturnDays": RETURN_POLICY["days"],
                    "returnMethod": RETURN_POLICY["method"],
                    "returnFees": RETURN_POLICY["fees_type"],
                    "returnShippingFeesAmount": StructuredDataGenerator._get_return_shipping_amount(),
                    "applicableCountry": APPLICABLE_COUNTRY,
                },
                "seller": {
                    "@id": f"{_build_absolute_url('')}#organization"
                },
                "shippingDetails": StructuredDataGenerator._get_weight_based_shipping_details()
            },
        }

        # SEO 2026-05-16 (P1-9) — emit ``releaseDate`` and ``dateModified``
        # so AI search engines (Perplexity, ChatGPT Search, Google AI
        # Overviews) can score freshness per product. Mirrors what we
        # already do on Article schema for the DTF blog. Skip silently
        # if the underlying datetimes are missing (e.g. fixtures-only
        # rows during tests) — the rest of the schema stays valid.
        try:
            created_at = getattr(product, "created_at", None)
            updated_at = getattr(product, "updated_at", None)
            if created_at is not None:
                if hasattr(created_at, "isoformat"):
                    schema["releaseDate"] = created_at.isoformat() if getattr(
                        created_at, "tzinfo", None
                    ) else created_at.date().isoformat()
                else:
                    schema["releaseDate"] = str(created_at)
            if updated_at is not None:
                if hasattr(updated_at, "isoformat"):
                    schema["dateModified"] = updated_at.isoformat() if getattr(
                        updated_at, "tzinfo", None
                    ) else updated_at.date().isoformat()
                else:
                    schema["dateModified"] = str(updated_at)
        except Exception:
            # Defensive: never break the whole schema over a
            # date-formatting hiccup.
            pass

        # Phase 21 (2026-05-10) — embed AggregateRating only when the
        # caller passes a ``review_summary`` whose ``show_rating`` is
        # True. SEO v1.0 Phase 12 (2026-05-13) — finding (M) lowered
        # the underlying ``MIN_APPROVED_REVIEWS_FOR_RATING`` from 3
        # to 1, so this branch now activates the moment a single
        # approved review exists on the product. Mirrors the rule in
        # ``reviews.services.aggregate.MIN_APPROVED_REVIEWS_FOR_RATING``
        # so we never emit fake or thin rating signals to Google.
        if review_summary is not None and getattr(review_summary, "show_rating", False):
            avg = getattr(review_summary, "avg", None)
            count = int(getattr(review_summary, "count", 0) or 0)
            if avg is not None and count >= 1:
                schema["aggregateRating"] = {
                    "@type": "AggregateRating",
                    "ratingValue": f"{float(avg):.1f}",
                    "reviewCount": str(count),
                    "bestRating": "5",
                    "worstRating": "1",
                }

                # Nested top-5 Review JSON-LD. Live-queried here so the
                # schema mirrors what's currently on the page; the same
                # threshold (``MIN_APPROVED_REVIEWS_FOR_RATING``, lowered
                # from 3 to 1 in SEO v1.0 Phase 12 — finding (M)) gates
                # surfacing, so once a single approved review exists the
                # nested ``Review`` blocks ride along with
                # ``aggregateRating`` and feed the rich-result snippet.
                try:
                    from reviews.models import Review as _Review, ReviewStatus as _RS
                    top_reviews = list(
                        _Review.objects
                        .filter(product=product, status=_RS.APPROVED)
                        .order_by("-helpful_count", "-created_at")[:5]
                    )
                except Exception:
                    top_reviews = []
                review_blocks = []
                for r in top_reviews:
                    block = {
                        "@type": "Review",
                        "reviewRating": {
                            "@type": "Rating",
                            "ratingValue": str(int(r.rating)),
                            "bestRating": "5",
                            "worstRating": "1",
                        },
                        "author": {"@type": "Person", "name": r.author_name or "TwoComms клієнт"},
                        "datePublished": r.created_at.date().isoformat() if r.created_at else "",
                    }
                    if r.title:
                        block["name"] = r.title
                    if r.body:
                        # Cap to 600 chars — Google ignores giant review
                        # bodies and we don't want to bloat per-page schema.
                        block["reviewBody"] = (r.body[:600] + "…") if len(r.body) > 600 else r.body
                    review_blocks.append(block)
                if review_blocks:
                    schema["review"] = review_blocks

        # Merchant-level properties (age_group, gender, google_product_category)
        age_group = "adult"
        gender = "unisex"
        if product.category:
            category_name = product.category.name.lower()
            if any(word in category_name for word in ['чоловіч', 'мужск', 'men']):
                gender = "male"
            elif any(word in category_name for word in ['жіноч', 'женск', 'women']):
                gender = "female"

        # SEO molecular-upgrade US-8 (2026-05-16) — explicit
        # PeopleAudience so Google Shopping / AI Search can resolve
        # gender targeting without parsing additionalProperty. Maps the
        # already-derived ``gender`` to schema.org audienceType vocab.
        audience_type_map = {
            "male": "Men",
            "female": "Women",
            "unisex": "Adults",
        }
        suggested_gender_map = {
            "male": "https://schema.org/Male",
            "female": "https://schema.org/Female",
            "unisex": "https://schema.org/Unisex",
        }
        schema["audience"] = {
            "@type": "PeopleAudience",
            "audienceType": audience_type_map.get(gender, "Adults"),
            "suggestedGender": suggested_gender_map.get(gender, "https://schema.org/Unisex"),
            "suggestedMinAge": 16,
        }

        schema["additionalProperty"].extend([
            {"@type": "PropertyValue", "name": "age_group", "value": age_group},
            {"@type": "PropertyValue", "name": "gender", "value": gender},
            {"@type": "PropertyValue", "name": "size_type", "value": "regular"},
            {"@type": "PropertyValue", "name": "size_system", "value": "UA"},
        ])

        # Добавляем категорию
        if product.category:
            schema["category"] = product.category.name
            schema["additionalProperty"].append({
                "@type": "PropertyValue",
                "name": "google_product_category",
                "value": "1604"  # Apparel & Accessories > Clothing
            })

        # Добавляем все изображения
        # Добавляем реальные цвета из вариантов
        try:
            from storefront.services.color_filter import _translate_color_label
            from storefront.services.catalog_helpers import get_product_color_variant_rows
            color_names = []
            color_variants = get_product_color_variant_rows(product)
            for variant in color_variants:
                if variant.color and variant.color.name:
                    color_names.append(str(_translate_color_label(variant.color.name)))
            if color_names:
                unique_colors = list(dict.fromkeys(color_names))[:5]
                schema["color"] = ", ".join(unique_colors)
                schema["additionalProperty"].append({
                    "@type": "PropertyValue",
                    "name": _("Колір"),
                    "value": ", ".join(unique_colors)
                })
            else:
                schema["additionalProperty"].append({
                    "@type": "PropertyValue",
                    "name": _("Колір"),
                    "value": _("Різні кольори"),
                })
        except Exception:
            schema["additionalProperty"].append({
                "@type": "PropertyValue",
                "name": _("Колір"),
                "value": _("Різні кольори"),
            })

        # Добавляем размеры
        resolved_sizes = resolve_product_sizes(product)
        if resolved_sizes:
            schema["size"] = ", ".join(resolved_sizes)
            schema["additionalProperty"].append({
                "@type": "PropertyValue",
                "name": _("Розміри"),
                "value": ", ".join(resolved_sizes)
            })

        # Добавляем скидку если есть
        if product.has_discount:
            schema["offers"]["priceSpecification"] = {
                "@type": "CompoundPriceSpecification",
                "price": str(product.final_price),
                "priceCurrency": "UAH",
                "referenceQuantity": {
                    "@type": "QuantitativeValue",
                    "value": 1,
                    "unitCode": "C62"
                }
            }

        # SEO molecular-upgrade US-8 finishing (2026-05-16) — variant
        # URLs declare ``isVariantOf`` ref on the base product node so
        # Google Shopping / AI Search can model the colour/fit hierarchy
        # without a full ProductGroup migration. The base product gets a
        # ``hasVariant`` array if any colour variants exist. Stable @ids
        # let the references resolve across crawls.
        #
        # SEO 2026-06-04 — finding (GSC ProductGroup). The ``@id`` graph
        # must stay locale-consistent: the in-page Product node carries the
        # canonical (locale-prefixed) ``@id``, so the base reference and the
        # variant URLs are built with the SAME locale prefix derived from
        # ``canonical_path``. Otherwise an /en/ or /ru/ page emits
        # references the in-page graph cannot resolve.
        locale_prefix = StructuredDataGenerator._locale_url_prefix(canonical_path)
        base_product_url = _build_absolute_url(f"{locale_prefix}product/{product.slug}/")
        base_product_id = f"{base_product_url}#product"
        is_variant_render = (
            selected_variant is not None
            and product_canonical_url != base_product_url
        )
        if is_variant_render:
            schema["isVariantOf"] = {"@id": base_product_id}
        else:
            try:
                from storefront.services.catalog_helpers import get_product_color_variant_rows
                variant_ids: list[Dict[str, str]] = []
                for variant in get_product_color_variant_rows(product):
                    slug = (getattr(variant, "slug", "") or "").strip()
                    if not slug:
                        continue
                    variant_url = _build_absolute_url(f"{locale_prefix}product/{product.slug}/{slug}/")
                    variant_ids.append({"@id": f"{variant_url}#product"})
                if variant_ids:
                    schema["hasVariant"] = variant_ids
            except Exception:
                pass

        # Видео товара (YouTube) → Schema.org VideoObject.
        # SEO 2026-06-08 — finding (§2.8). ``video`` НЕ является валидным
        # свойством ``Product`` у schema.org, поэтому Google игнорировал
        # вложенный VideoObject (ни Video rich result, ни польза для
        # Merchant). Корректный способ привязать видео к товару — через
        # ``subjectOf`` (Product → CreativeWork/VideoObject). VideoObject
        # остаётся обнаружимым в @graph и теперь eligible для Video rich
        # results, не ломая остальной Product-граф.
        try:
            if getattr(product, "has_video", False):
                video_id = product.youtube_id
                if video_id:
                    upload_date = None
                    created_at = getattr(product, "created_at", None)
                    if created_at is not None and hasattr(created_at, "date"):
                        upload_date = created_at.date().isoformat()
                    video_obj = {
                        "@type": "VideoObject",
                        "name": _("Відео огляд: %(title)s") % {"title": product.title},
                        "description": description,
                        "thumbnailUrl": product.video_thumbnail_url,
                        "contentUrl": product.video_watch_url,
                        "embedUrl": product.video_embed_url,
                    }
                    if upload_date:
                        video_obj["uploadDate"] = upload_date
                    schema["subjectOf"] = video_obj
        except Exception:
            pass

        # Respect product.seo_schema as JSON override (merge with generated)
        if product.seo_schema and isinstance(product.seo_schema, dict):
            for key, value in product.seo_schema.items():
                if key not in ('@context', '@type'):
                    schema[key] = value

        return schema

    @staticmethod
    def _locale_url_prefix(canonical_path: Optional[str] = None) -> str:
        """Return the locale URL segment (``""``, ``"ru/"`` or ``"en/"``).

        SEO 2026-06-04 — finding (GSC ProductGroup). The PDP renders under
        ``/``, ``/ru/`` or ``/en/`` and the base ``Product`` node's ``@id``
        carries that prefix. The ProductGroup (and its ``hasVariant``
        children) must use the SAME prefix so the in-page ``@id`` graph
        references resolve. We derive the prefix from ``canonical_path``
        when available (most precise — it is what the template used for
        ``<link rel=canonical>``) and fall back to the active Django
        locale otherwise.
        """
        if canonical_path:
            stripped = canonical_path.lstrip("/")
            head = stripped.split("/", 1)[0].lower()
            if head in ("ru", "en"):
                return f"{head}/"
            return ""
        lang = (get_language() or "uk").split("-", 1)[0].lower()
        return f"{lang}/" if lang in ("ru", "en") else ""

    @staticmethod
    def generate_product_group_schema(
        product: Product,
        canonical_path: Optional[str] = None,
    ) -> Optional[Dict]:
        """Generate a ``ProductGroup`` schema for the base PDP.

        SEO 2026-05-19 (VILNI deep review §13.1). Google's
        product-variants guidance (Search Central, Feb 2024) recommends
        wrapping multi-variant products in ``ProductGroup`` so the
        search engine can consolidate ranking signals across colour
        and fit pages. We emit this **alongside** the base ``Product``
        node inside the @graph on the canonical PDP — the base Product
        keeps its rich-result eligibility while the ProductGroup gives
        Google an explicit cluster head with ``variesBy`` + ``hasVariant``.

        SEO 2026-06-04 — finding (GSC ProductGroup). Search Console raised
        the critical «Задайте значення для одного з: hasVariant.offers,
        review або aggregateRating» on the ProductGroup object itself.
        Root cause: ``hasVariant`` carried only bare ``@id`` references to
        Product nodes that do NOT exist on the page (colour-variant URLs
        live on their own pages), and the base reference even had a
        locale mismatch (page rendered under ``/en/`` while the reference
        pointed at the non-prefixed ``@id``). Per Google's multi-page
        variant docs, off-page objects do not count, so the ProductGroup
        ended up with zero resolvable ``offers``. Fix: emit FULL inline
        ``Product`` variant nodes (each with a valid ``offers`` block) per
        the documented nested ProductGroup pattern, and keep every ``@id``
        / ``url`` locale-consistent with the page.

        Returns ``None`` when the product has no real variants (no
        colour variants and no fit options), so the caller can skip
        emitting an empty group node.
        """
        try:
            from storefront.services.catalog_helpers import get_product_color_variant_rows
            color_variants = get_product_color_variant_rows(product)
        except Exception:
            color_variants = []

        try:
            from storefront.services.catalog_helpers import get_active_fit_options
            fit_options = get_active_fit_options(product)
        except Exception:
            fit_options = []

        if not color_variants and not fit_options:
            return None

        # SEO 2026-06-08 — finding (§2.1/§2.2/§3.5). Emit ProductGroup ONLY
        # for genuinely multi-variant products (>=2 colour variants with a
        # slug). Rationale:
        #   * For single-colour / colour-less SKUs the standalone Product
        #     node already covers rich-result eligibility; a one-member
        #     ProductGroup added zero clustering value.
        #   * The previous design reused the base Product ``#product`` @id
        #     inside ``hasVariant[0]`` — a duplicate @id on the same page
        #     that made Google raise the critical «Недопустимый тип объекта
        #     в поле parent_node» and the «set one of hasVariant.offers /
        #     review / aggregateRating» errors (GSC 2026-06-07, reality-bends
        #     ×2 and /en/ idea-hd, all single-colour).
        # Gating here removes the group from ~58 single-colour SKUs and from
        # the flagged products, eliminating both errors at the source. The
        # remaining multi-colour groups below carry NO base node (no @id
        # collision) and each variant declares ``size`` for Merchant.
        slugged_color_variants = [
            cv for cv in color_variants
            if (getattr(cv, "slug", "") or "").strip()
        ]
        if len(slugged_color_variants) < 2:
            return None

        prefix = StructuredDataGenerator._locale_url_prefix(canonical_path)
        base_url = _build_absolute_url(f"{prefix}product/{product.slug}/")
        group_id = f"{base_url}#product-group"

        varies_by: List[str] = []
        if color_variants:
            varies_by.append("https://schema.org/color")
        if fit_options:
            # ``sizeGroup`` is schema.org's canonical property for
            # fit / cut (regular / oversize / relaxed).
            varies_by.append("https://schema.org/sizeGroup")
        # Size selection is always present for apparel — declare it so
        # Google can match against Merchant feed size dimensions even
        # though the size URLs collapse to base via canonical.
        varies_by.append("https://schema.org/size")

        description = _truncate_at_word_boundary(
            _pick_product_description_source(product)
            or f"Якісний {product.category.name.lower() if product.category else 'одяг'} з ексклюзивним дизайном від TwoComms",
            320,
        )

        availability = StructuredDataGenerator._get_product_availability(product)
        price = str(product.final_price)
        price_valid_until = StructuredDataGenerator._get_dynamic_price_valid_until()
        inlanguage = StructuredDataGenerator._resolve_inlanguage_code()

        def _variant_offer(offer_url: str) -> Dict:
            return {
                "@type": "Offer",
                "price": price,
                "priceCurrency": "UAH",
                "availability": availability,
                "itemCondition": "https://schema.org/NewCondition",
                "url": offer_url,
                "priceValidUntil": price_valid_until,
                "seller": {"@id": f"{_build_absolute_url('')}#organization"},
            }

        group: Dict = {
            "@context": "https://schema.org",
            "@type": "ProductGroup",
            "@id": group_id,
            "productGroupID": product.slug,
            "name": product.title,
            "description": description,
            "url": base_url,
            "inLanguage": inlanguage,
            "brand": {"@id": f"{_build_absolute_url('')}#brand"},
            "variesBy": varies_by,
        }

        # ``hasVariant`` MUST contain at least one resolvable Product with
        # ``offers``. We emit full inline Product nodes — starting with the
        # base product (the node also emitted standalone on this page,
        # sharing its ``@id``) and one per colour variant. This guarantees
        # the ProductGroup always carries variant offers regardless of how
        # Google resolves cross-page references.
        default_image = _safe_product_display_image(product)
        base_image = _build_absolute_url(default_image.url) if default_image else get_default_social_image_url()

        # Size dimension (Merchant requires ``size`` on apparel variants).
        product_sizes = resolve_product_sizes(product)
        size_value = ", ".join(product_sizes) if product_sizes else ""

        # ``hasVariant`` holds ONLY the real colour variants — each a full,
        # self-contained Product node with its own distinct @id (the variant
        # URL), resolvable ``offers`` and ``size``. No base node is emitted,
        # so nothing reuses the standalone Product's ``#product`` @id → the
        # parent_node collision is gone.
        variants_nodes: List[Dict] = []

        try:
            from storefront.services.color_filter import _translate_color_label
        except Exception:
            _translate_color_label = None  # type: ignore

        for variant in slugged_color_variants:
            slug = (getattr(variant, "slug", "") or "").strip()
            variant_url = _build_absolute_url(f"{prefix}product/{product.slug}/{slug}/")
            node: Dict = {
                "@type": "Product",
                "@id": f"{variant_url}#product",
                "name": product.title,
                "url": variant_url,
                "sku": f"TC-{product.id}-{slug}",
                "inProductGroupWithID": product.slug,
                "offers": _variant_offer(variant_url),
            }
            if size_value:
                node["size"] = size_value
            # Colour label so Google can map the variant to ``variesBy``.
            color_obj = getattr(variant, "color", None)
            color_name = getattr(color_obj, "name", "") if color_obj else ""
            if color_name and _translate_color_label is not None:
                node["color"] = str(_translate_color_label(color_name))
            elif color_name:
                node["color"] = color_name
            # Variant image (falls back to the base product image).
            variant_image = base_image
            try:
                first_img = variant.images.all()[:1]
                for img in first_img:
                    variant_image = _build_absolute_url(img.image.url)
            except Exception:
                pass
            node["image"] = variant_image
            variants_nodes.append(node)

        group["hasVariant"] = variants_nodes

        return group

    @staticmethod
    def generate_google_merchant_schema(product: Product) -> Dict:
        """
        DEPRECATED: Use generate_product_schema() instead.
        Now returns the same unified product schema to avoid duplicate JSON-LD blocks.
        """
        return StructuredDataGenerator.generate_product_schema(product)

    @staticmethod
    def generate_breadcrumb_schema(breadcrumbs: List[Dict]) -> Dict:
        """Generate BreadcrumbList schema.

        SEO v1.0 Phase 4 (2026-05-12) — finding (B24). The previous
        payload only annotated the leaf ListItem nodes with ``@id``; the
        parent ``BreadcrumbList`` had none, so when the same trail was
        emitted on multiple pages (PDP + variant + parent category)
        Google could not deduplicate them via the @graph reference
        pattern. Derive a deterministic ``@id`` from the trail itself —
        a hash over the ordered URL list — so identical breadcrumb
        chains share a single node in the Knowledge Graph.
        """
        schema = {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": []
        }

        item_urls: List[str] = []
        for i, breadcrumb in enumerate(breadcrumbs):
            item_url = _build_absolute_url(breadcrumb.get("url", ""))
            item_urls.append(item_url)
            schema["itemListElement"].append({
                "@type": "ListItem",
                "@id": item_url,
                "position": i + 1,
                # Force-resolve gettext_lazy proxies so json.dumps does
                # not blow up with "Object of type __proxy__ is not
                # JSON serializable" when callers pass localized labels.
                "name": str(breadcrumb.get("name", "")),
                "item": item_url
            })

        # Stable @id — last item's URL with a #breadcrumbs anchor lets
        # Google merge identical trails across PDP / variant pages while
        # still being human-meaningful when inspected in Search Console.
        if item_urls:
            schema["@id"] = f"{item_urls[-1].rstrip('/')}/#breadcrumbs"

        return schema

    @staticmethod
    def _resolve_inlanguage_code() -> str:
        """Map the active Django locale to a Google-friendly inLanguage code.

        SEO v1.1 (2026-05-16) — Phase 17v. Previously hardcoded ``uk-UA``
        on every locale, which made Google flag /ru/ and /en/ pages as
        schema-language mismatch. Now derived from ``get_language()``:
            uk → uk-UA   ru → ru-UA   en → en-US
        Defaults to uk-UA if no locale is active (e.g. background tasks).
        """
        lang = (get_language() or "uk").split("-", 1)[0].lower()
        return {
            "uk": "uk-UA",
            "ru": "ru-UA",
            "en": "en-US",
        }.get(lang, "uk-UA")

    @staticmethod
    def _localized_attr(obj, attr: str, fallback: str = "") -> str:
        """Pick a locale-specific model attribute with a UA fallback.

        Used by Product schema to surface ``material_ru`` / ``material_en``
        when modeltranslation has populated them, otherwise falls back to
        the canonical Ukrainian field. ``fallback`` is the literal value
        to return if both the localized and base attributes are empty.
        """
        lang = (get_language() or "uk").split("-", 1)[0].lower()
        if lang and lang != "uk":
            localized = getattr(obj, f"{attr}_{lang}", None)
            if localized:
                return localized
        base = getattr(obj, attr, None) or getattr(obj, f"{attr}_uk", None)
        return base or fallback

    @staticmethod
    def generate_organization_schema() -> Dict:
        """Generates the canonical Organization schema for TwoComms.

        Phase 5 — single source of truth. This is the same payload that
        ``pages/pro_brand.html`` previously hard-coded inline. The
        ``@id`` is stable across pages so Google can deduplicate
        instances of this Organization in the Knowledge Graph.

        SEO 2026-05-16 (P1-5/P1-7) — added ``founder`` reference,
        ``foundingDate``, ``foundingLocation`` and an ``address`` block
        with the canonical Kharkiv NAP signals so AI knowledge graphs
        and Google's Knowledge Panel can resolve TwoComms as a
        Kharkiv-rooted brand instead of inferring "Ukraine, location
        unknown" from the contact channels alone.

        SEO molecular-upgrade US-13 / homepage commerce signals —
        ``image`` (1200x630 social preview) so Knowledge Panel has a
        non-SVG visual fallback, ``slogan`` for AI-cite snippet,
        explicit ``knowsLanguage`` triple and Brand-typed alias.
        ``sameAs`` left at minimum verified set (Instagram + Telegram);
        adding more requires owner-confirmed handles.
        """
        base_url = _build_absolute_url("")
        logo_url = _build_absolute_url("static/img/logo.svg")
        social_image = _build_absolute_url(DEFAULT_SOCIAL_IMAGE_PATH)
        return {
            "@context": "https://schema.org",
            "@type": ["Organization", "OnlineStore"],
            "@id": f"{base_url}#organization",
            "name": "TwoComms",
            "alternateName": "TwoComms / TWOCOMMS",
            "url": base_url,
            "logo": {
                "@type": "ImageObject",
                "url": logo_url,
                "contentUrl": logo_url,
                "caption": "TwoComms logo",
            },
            "image": social_image,
            "slogan": _(
                "Не крапка, а продовження. Український streetwear із Харкова."
            ),
            "description": _(
                "TwoComms — український streetwear / military-adjacent бренд "
                "одягу з Харкова, створений навколо ідеї продовження після "
                "критичної точки: не крапка, а продовження."
            ),
            "foundingDate": "2022",
            "foundingLocation": {
                "@type": "Place",
                "name": _("Харків, Україна"),
                "address": {
                    "@type": "PostalAddress",
                    "addressCountry": "UA",
                    "addressRegion": "UA-63",
                    "addressLocality": _("Харків"),
                },
            },
            "address": {
                "@type": "PostalAddress",
                "addressCountry": "UA",
                "addressRegion": "UA-63",
                "addressLocality": _("Харків"),
            },
            "areaServed": {
                "@type": "Country",
                "name": "Ukraine",
                "identifier": "UA",
            },
            "knowsLanguage": ["uk", "ru", "en"],
            "currenciesAccepted": "UAH",
            "paymentAccepted": "Cash, Credit Card, Apple Pay, Google Pay",
            "priceRange": _homepage_price_range_text(),
            "founder": {"@id": f"{base_url}#founder"},
            "subjectOf": {
                "@type": "NewsArticle",
                "headline": _(
                    "Це не крапка, це продовження: історія Артема Синіла "
                    "та харківського бренду TwoComms"
                ),
                "url": (
                    "https://mva.gov.ua/prescenter/category/86-novini/"
                    "tse-ne-krapka-tse-prodovzhennya-istoriya-artema-sinila-"
                    "ta-harkivskogo-brendu-twocomms"
                ),
                "publisher": {
                    "@type": "GovernmentOrganization",
                    "name": _("Міністерство у справах ветеранів України"),
                    "url": "https://mva.gov.ua",
                },
            },
            "sameAs": _organization_same_as(),
            "contactPoint": {
                "@type": "ContactPoint",
                "telephone": "+380966543212",
                "contactType": "customer support",
                "availableLanguage": ["uk", "ru", "en"],
                "areaServed": "UA",
            },
            "brand": {
                "@type": "Brand",
                "@id": f"{base_url}#brand",
                "name": "TwoComms",
                "logo": logo_url,
            },
            # SEO 2026-05-19 (VILNI deep review §13.2) — Organization-level
            # MerchantReturnPolicy is required by Google Merchant Listings
            # to surface store-wide trust signals (return window + free
            # return method). Per-Offer override remains in
            # ``generate_product_schema`` for DTF custom-print exceptions.
            "hasMerchantReturnPolicy": {
                "@type": "MerchantReturnPolicy",
                "applicableCountry": APPLICABLE_COUNTRY,
                "returnPolicyCategory": RETURN_POLICY["category"],
                "merchantReturnDays": RETURN_POLICY["days"],
                "returnMethod": RETURN_POLICY["method"],
                "returnFees": RETURN_POLICY["fees_type"],
                "returnShippingFeesAmount": (
                    StructuredDataGenerator._get_return_shipping_amount()
                ),
            },
            # SEO 2026-05-19 (VILNI deep review §13.3) — declare the
            # «TWOCOMMS Бали» loyalty program with schema.org/MemberProgram
            # so Google can surface it in merchant knowledge panels and AI
            # answer engines (Perplexity, ChatGPT Search) when users ask
            # about loyalty / discount benefits.
            "hasMemberProgram": {
                "@type": "MemberProgram",
                "name": "TWOCOMMS Бали",
                "description": _(
                    "Бали за покупки та промокоди для постійних клієнтів TwoComms; "
                    "можна обміняти на знижки або донати на ЗСУ."
                ),
                "url": f"{base_url}faq/",
                "hostingOrganization": {"@id": f"{base_url}#organization"},
            },
        }

    @staticmethod
    def generate_founder_schema() -> Dict:
        """Canonical Person schema for the brand founder (Артем Синіло).

        SEO 2026-06-08 — finding (§2.1, part 1). The Organization schema
        references ``founder: {"@id": ".../#founder"}`` on every page, but
        no ``Person`` node with that ``@id`` was ever emitted — a dangling
        reference Google / AI knowledge graphs could not resolve, losing the
        strongest E-E-A-T anchor (veteran founder from Kharkiv). This node
        resolves that reference. Data is intentionally minimal and verified:
        name, role, link back to the Organization, nationality. The
        government press release about the founder already lives on the
        Organization node (``subjectOf``), so we do not duplicate it here.
        """
        base_url = _build_absolute_url("")
        return {
            "@context": "https://schema.org",
            "@type": "Person",
            "@id": f"{base_url}#founder",
            "name": "Артем Синіло",
            "jobTitle": _("Засновник TwoComms"),
            "description": _(
                "Засновник укр��їнського streetwear-бренду TwoComms із Харкова, "
                "бойовий ветеран."
            ),
            "worksFor": {"@id": f"{base_url}#organization"},
            "nationality": {"@type": "Country", "name": "Ukraine", "identifier": "UA"},
        }

    @staticmethod
    def generate_website_schema() -> Dict:
        """Generates the WebSite schema with a SearchAction.

        Phase 5 — single source of truth. The ``target`` URL points at
        the real ``/search/`` route registered in ``storefront/urls.py``.
        """
        base_url = _build_absolute_url("")
        return {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "@id": f"{base_url}#website",
            "name": "TwoComms",
            "url": base_url,
            "description": _("Магазин стріт & мілітарі одягу з ексклюзивним дизайном"),
            "potentialAction": {
                "@type": "SearchAction",
                "target": {
                    "@type": "EntryPoint",
                    "urlTemplate": f"{base_url}search/?q={{search_term_string}}",
                },
                "query-input": "required name=search_term_string",
            },
        }

    @staticmethod
    def generate_homepage_storefront_schema() -> Dict:
        """Storefront commerce node for the homepage (US-16).

        Why this exists
        ---------------
        The homepage previously emitted only ``WebPage`` + ``ItemList``
        (categories), without any explicit price signal. Google's SERP
        renderer therefore fell back to heuristic price extraction,
        which scraped the survey banner ("ВИГРАЙ 200 ГРН" + "-200 грн")
        and rendered ``200 200,00 грн`` underneath the ru-UA homepage
        snippet — actively misleading prospective customers.

        Mitigation strategy: emit an ``OnlineStore`` node anchored to
        ``#storefront`` with explicit ``priceRange`` derived from live
        catalogue prices, plus a ``makesOffer`` AggregateOffer that
        resolves Google's price-snippet attribution to the real
        catalogue range. Combined with the survey-banner numeric
        decoupling fix in ``index.html``, this removes the hallucinated
        ``200 200 грн`` price entirely.

        The node references the canonical Organization ``@id`` so the
        Knowledge Graph stays deduplicated.
        """
        base_url = _build_absolute_url("")
        social_image = _build_absolute_url(DEFAULT_SOCIAL_IMAGE_PATH)
        aggregate = _homepage_price_aggregate()

        node: Dict[str, object] = {
            "@context": "https://schema.org",
            "@type": "OnlineStore",
            "@id": f"{base_url}#storefront",
            "name": "TwoComms",
            "url": base_url,
            "image": social_image,
            "logo": _build_absolute_url("static/img/logo.svg"),
            "description": _(
                "Український онлайн-магазин стріт- та мілітарі-одягу TwoComms: "
                "футболки, худі, лонгсліви та кастомний DTF-друк."
            ),
            "telephone": "+380966543212",
            "currenciesAccepted": "UAH",
            "paymentAccepted": "Cash, Credit Card, Apple Pay, Google Pay",
            "priceRange": _homepage_price_range_text(),
            "areaServed": {"@type": "Country", "name": "Ukraine", "identifier": "UA"},
            "parentOrganization": {"@id": f"{base_url}#organization"},
            "sameAs": _organization_same_as(),
            "contactPoint": {
                "@type": "ContactPoint",
                "telephone": "+380966543212",
                "contactType": "customer support",
                "availableLanguage": ["uk", "ru", "en"],
                "areaServed": "UA",
            },
        }

        if aggregate:
            node["makesOffer"] = {
                "@type": "AggregateOffer",
                "priceCurrency": "UAH",
                "lowPrice": aggregate["lowPrice"],
                "highPrice": aggregate["highPrice"],
                "offerCount": aggregate["offerCount"],
                "availability": "https://schema.org/InStock",
                "seller": {"@id": f"{base_url}#organization"},
            }

        return node

    @staticmethod
    def generate_faq_schema(faq_items: List[Dict]) -> Dict:
        """Генерирует FAQ schema"""
        schema = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": []
        }

        for faq in faq_items:
            schema["mainEntity"].append({
                "@type": "Question",
                "name": faq["question"],
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": faq["answer"]
                }
            })

        return schema


class SEOContentOptimizer:
    """Оптимизатор контента для SEO"""

    @staticmethod
    def suggest_content_improvements(product: Product) -> List[str]:
        """Предлагает улучшения контента для товара"""
        suggestions = []

        # Проверяем длину описания
        if not product.description or len(product.description) < 100:
            suggestions.append("Додайте детальний опис товару (мінімум 100 символів)")

        # Проверяем наличие изображений
        if not product.display_image:
            suggestions.append("Додайте головне зображення товару")

        # Проверяем наличие цветовых вариантов
        try:
            from storefront.services.catalog_helpers import get_product_color_variant_rows
            color_count = len(get_product_color_variant_rows(product))
            if color_count == 0:
                suggestions.append("Додайте кольорові варіанти товару")
        except ImportError:
            pass

        # Проверяем длину названия
        if len(product.title) < 10:
            suggestions.append("Збільште назву товару для кращого SEO")

        return suggestions

    @staticmethod
    def generate_alt_text_for_image(image_name: str, product_title: str = "") -> str:
        """Генерирует alt-текст для изображения"""
        if product_title:
            return f"Зображення товару {product_title}"

        # Извлекаем ключевые слова из имени файла
        clean_name = re.sub(r'[^a-zA-Zа-яіїєґ0-9\s]', ' ', image_name.lower())
        words = clean_name.split()

        if words:
            return f"Зображення {' '.join(words[:3])}"

        return "Зображення товару TwoComms"

    @staticmethod
    def generate_ai_category_description(category: Category) -> str:
        """Генерирует AI-описание категории для SEO (если доступно)"""
        try:
            from django.conf import settings
            if not getattr(settings, 'USE_AI_DESCRIPTIONS', False):
                return ''
            api_key = getattr(settings, 'OPENAI_API_KEY', None) or __import__('os').environ.get('OPENAI_API_KEY')
            if not api_key:
                return ''
            import openai
            client = openai.OpenAI(api_key=api_key)
            model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
            prompt = (
                f"Напишіть стислий SEO-дружній опис категорії '{category.name}'. "
            )
            if category.description:
                prompt += f" Опис: {category.description}."
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Ви - SEO-копірайтер."},
                    {"role": "user", "content": prompt},
                ],
            )
            text = resp.choices[0].message.get('content', '') if hasattr(resp, 'choices') and resp.choices else ''
            return text.strip() if text else ''
        except Exception:
            return ''

    @staticmethod
    def generate_ai_product_description(product: Product) -> str:
        """Генерирует AI-описание товара для SEO (если доступно)"""
        try:
            from django.conf import settings
            use_descriptions = getattr(settings, 'USE_AI_DESCRIPTIONS', False)
            if isinstance(use_descriptions, str):
                use_descriptions = use_descriptions.lower() in ('1', 'true', 'yes')
            if not use_descriptions:
                return ''
            api_key = getattr(settings, 'OPENAI_API_KEY', None) or os.environ.get('OPENAI_API_KEY')
            if not api_key:
                return ''
            import openai
            client = openai.OpenAI(api_key=api_key)
            model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
            prompt = (
                f"Напишіть стислий SEO-дружній опис товару '{product.title}'. "
                f"Категорія: {product.category.name if product.category else 'N/A'}. "
                f"Ціна: {product.final_price}."
            )
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Ви - SEO-копірайтер."},
                    {"role": "user", "content": prompt},
                ],
            )
            text = ''
            if hasattr(resp, 'choices') and resp.choices:
                text = resp.choices[0].message.get('content', '') if isinstance(resp.choices[0].message, dict) else ''
            return text.strip() if text else ''
        except Exception:
            return ''


# Глобальные функции для использования в шаблонах
def get_product_seo_meta(product: Product) -> Dict[str, str]:
    """Возвращает SEO мета-теги для товара"""
    try:
        return SEOMetaGenerator.generate_product_meta(product)
    except Exception as e:
        # Возвращаем базовые мета-теги в случае ошибки
        return {
            'title': f"{product.title} - TwoComms",
            'description': f"Купити {product.title} в TwoComms. Якісний одяг з ексклюзивним дизайном.",
            'keywords': 'стріт одяг, мілітарі одяг, TwoComms',
            'og_title': f"{product.title} - TwoComms",
            'og_description': f"Купити {product.title} в TwoComms. Якісний одяг з ексклюзивним дизайном.",
            'og_image': product.main_image.url if product.main_image else '',
            'twitter_title': f"{product.title} - TwoComms",
            'twitter_description': f"Купити {product.title} в TwoComms. Якісний одяг з ексклюзивним дизайном.",
            'twitter_image': product.main_image.url if product.main_image else '',
        }


def get_category_seo_meta(category: Category) -> Dict[str, str]:
    """Возвращает SEO мета-теги для категории"""
    try:
        return SEOMetaGenerator.generate_category_meta(category)
    except Exception as e:
        # Возвращаем базовые мета-теги в случае ошибки
        return {
            'title': f"{category.name} - TwoComms",
            'description': f"Купити {category.name.lower()} в TwoComms. Якісний одяг з ексклюзивним дизайном.",
            'keywords': 'стріт одяг, мілітарі одяг, TwoComms',
            'og_title': f"{category.name} - TwoComms",
            'og_description': f"Купити {category.name.lower()} в TwoComms. Якісний одяг з ексклюзивним дизайном.",
            'og_image': category.cover.url if category.cover else '',
            'twitter_title': f"{category.name} - TwoComms",
            'twitter_description': f"Купити {category.name.lower()} в TwoComms. Якісний одяг з ексклюзивним дизайном.",
            'twitter_image': category.cover.url if category.cover else '',
        }


def get_product_schema(
    product: Product,
    canonical_path: Optional[str] = None,
    selected_variant=None,
    review_summary=None,
) -> str:
    """Возвращает JSON-LD schema для товара.

    Phase 21 — propagates ``canonical_path``, ``selected_variant`` and
    ``review_summary`` to the underlying generator so the rendered
    Product schema's ``url``, ``image`` and ``aggregateRating`` always
    match what the page declares as canonical / approved.
    """
    try:
        schema = StructuredDataGenerator.generate_product_schema(
            product,
            canonical_path=canonical_path,
            selected_variant=selected_variant,
            review_summary=review_summary,
        )
        return json.dumps(schema, ensure_ascii=False, indent=2)
    except Exception:
        return ""


def get_breadcrumb_schema(breadcrumbs: List[Dict]) -> str:
    """Возвращает JSON-LD schema для хлебных крошек"""
    try:
        schema = StructuredDataGenerator.generate_breadcrumb_schema(breadcrumbs)
        return json.dumps(schema, ensure_ascii=False, indent=2)
    except Exception as e:
        # Возвращаем пустую строку в случае ошибки
        return ""


def get_google_merchant_schema(product: Product) -> str:
    """Возвращает JSON-LD schema для Google Merchant Center"""
    try:
        schema = StructuredDataGenerator.generate_google_merchant_schema(product)
        return json.dumps(schema, ensure_ascii=False, indent=2)
    except Exception as e:
        # Возвращаем пустую строку в случае ошибки
        return ""
