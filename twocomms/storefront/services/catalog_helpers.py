"""
Utility helpers for catalog-related views: cached categories and colour variants.
"""
from __future__ import annotations

import logging
import hashlib
from collections import defaultdict
from decimal import Decimal
from typing import Iterable, List, Dict, Any

from django.apps import apps
from django.core.cache import BaseCache
from django.db import DatabaseError
from django.db.models import QuerySet
from django.utils.translation import gettext as _

from cache_utils import get_cache
from .image_variants import build_optimized_image_payload

logger = logging.getLogger(__name__)

PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY = "products:public_order_version"
PUBLIC_CATEGORY_VERSION_CACHE_KEY = "categories:public_version"

# Raw UA labels are kept here so dictionary equality comparisons remain
# locale-independent. Display lookups go through ``_color_label`` which
# wraps them in ``gettext`` — this gives the user-visible text in the
# active locale (RU / EN) without breaking the matching code that uses
# label equality (``primary_name != secondary_name``).
_COLOR_LABELS_BY_HEX = {
    "000000": "Чорний",
    "050505": "Чорний",
    "111111": "Чорний",
    "151515": "Чорний",
    "FFFFFF": "Білий",
    "F5F5F5": "Білий",
    "7A5A3A": "Койот",
    "8B6B45": "Койот",
    "A47A4D": "Койот",
    "59604A": "Олива",
    "5C6449": "Олива",
    "6B6F45": "Олива",
    "4F5A3A": "Олива",
}

_COLOR_LABELS_BY_NAME = {
    "black": "Чорний",
    "white": "Білий",
    "olive": "Олива",
    "coyote": "Койот",
    "khaki": "Хакі",
}


def _color_label(raw: str | None) -> str:
    """Translate a raw UA color label to the active locale.

    Falls back to the raw value when no translation catalogue entry
    exists. ``gettext`` (eager) is used because callers immediately
    feed the result into ``f"..."`` formatting.
    """
    if not raw:
        return ''
    return _(raw)


def _display_color_name(color) -> str:
    name = (getattr(color, 'name', '') or '').strip()
    if name:
        raw = _COLOR_LABELS_BY_NAME.get(name.lower(), name)
        return _color_label(raw)

    primary = (getattr(color, 'primary_hex', '') or '').strip().lstrip('#').upper()
    secondary = (getattr(color, 'secondary_hex', '') or '').strip().lstrip('#').upper()
    primary_raw = _COLOR_LABELS_BY_HEX.get(primary)
    secondary_raw = _COLOR_LABELS_BY_HEX.get(secondary)

    primary_name = _color_label(primary_raw) if primary_raw else None
    secondary_name = _color_label(secondary_raw) if secondary_raw else None

    if primary_name and secondary_name and primary_name != secondary_name:
        return f'{primary_name}/{secondary_name}'
    if primary_name:
        return primary_name
    if primary:
        return f'#{primary}'
    return ''


def build_product_image_alt(product, stored_alt: str | None = None, *, color_name: str = '', index: int | None = None, main: bool = False) -> str:
    """
    Return stored ALT text or a localized fallback for product images.
    """
    value = (stored_alt or '').strip()
    if value:
        return value

    title = (getattr(product, 'title', '') or _('Товар TwoComms')).strip()
    color = (color_name or '').strip()
    number = f' {index}' if index else ''

    if main:
        return _('%(title)s — головне фото товару TwoComms') % {'title': title}
    if color:
        return _('%(title)s — %(color)s — фото%(number)s TwoComms') % {
            'title': title, 'color': color, 'number': number,
        }
    return _('%(title)s — фото%(number)s TwoComms') % {'title': title, 'number': number}


def get_categories_cached(cache_backend: BaseCache, timeout: int = 600):
    """
    Retrieve ordered categories with caching.
    """
    if cache_backend is None:
        logger.warning("No cache backend passed to get_categories_cached; querying DB directly.")
        Category = apps.get_model('storefront', 'Category')
        return list(Category.objects.filter(is_active=True).order_by('order', 'name'))

    categories = cache_backend.get('categories:ordered')
    if categories is not None:
        return categories

    Category = apps.get_model('storefront', 'Category')
    categories = list(Category.objects.filter(is_active=True).order_by('order', 'name'))
    cache_backend.set('categories:ordered', categories, timeout)
    return categories


def apply_public_product_order(queryset: QuerySet) -> QuerySet:
    """
    Central source of truth for public product ordering.

    Admin drag-and-drop writes to Product.priority, so every public product list
    that should reflect admin order must use this helper.
    """
    return queryset.order_by("-priority", "-id")


def get_public_product_order_version(cache_backend: BaseCache | None = None) -> int:
    """
    Version marker for public product list cache keys.

    When admin reorder changes product priority, bumping this version invalidates
    cached anonymous listing pages without clearing unrelated cache entries.
    """
    cache_backend = cache_backend or get_cache()
    version = cache_backend.get(PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY)
    if version is None:
        cache_backend.add(PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY, 1, timeout=None)
        version = cache_backend.get(PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY)

    try:
        return max(int(version), 1)
    except (TypeError, ValueError):
        cache_backend.set(PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY, 1, timeout=None)
        return 1


def bump_public_product_order_version(cache_backend: BaseCache | None = None) -> int:
    """
    Bump the public product order cache version after admin reorder commits.
    """
    cache_backend = cache_backend or get_cache()
    current_version = get_public_product_order_version(cache_backend)
    try:
        next_version = cache_backend.incr(PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY)
    except Exception:
        next_version = current_version + 1
        cache_backend.set(PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY, next_version, timeout=None)

    try:
        return max(int(next_version), current_version + 1)
    except (TypeError, ValueError):
        fallback_version = current_version + 1
        cache_backend.set(PUBLIC_PRODUCT_ORDER_VERSION_CACHE_KEY, fallback_version, timeout=None)
        return fallback_version


def get_public_category_version(cache_backend: BaseCache | None = None) -> int:
    """
    Version marker for public category fragments and listing pages.
    """
    cache_backend = cache_backend or get_cache()
    version = cache_backend.get(PUBLIC_CATEGORY_VERSION_CACHE_KEY)
    if version is None:
        cache_backend.add(PUBLIC_CATEGORY_VERSION_CACHE_KEY, 1, timeout=None)
        version = cache_backend.get(PUBLIC_CATEGORY_VERSION_CACHE_KEY)

    try:
        return max(int(version), 1)
    except (TypeError, ValueError):
        cache_backend.set(PUBLIC_CATEGORY_VERSION_CACHE_KEY, 1, timeout=None)
        return 1


def bump_public_category_version(cache_backend: BaseCache | None = None) -> int:
    """
    Bump the public category cache version after category mutations commit.
    """
    cache_backend = cache_backend or get_cache()
    current_version = get_public_category_version(cache_backend)
    try:
        next_version = cache_backend.incr(PUBLIC_CATEGORY_VERSION_CACHE_KEY)
    except Exception:
        next_version = current_version + 1
        cache_backend.set(PUBLIC_CATEGORY_VERSION_CACHE_KEY, next_version, timeout=None)

    try:
        return max(int(next_version), current_version + 1)
    except (TypeError, ValueError):
        fallback_version = current_version + 1
        cache_backend.set(PUBLIC_CATEGORY_VERSION_CACHE_KEY, fallback_version, timeout=None)
        return fallback_version


def _load_product_color_variant_queryset(product_ids: Iterable[int]):
    """
    Internal helper that fetches ProductColorVariant queryset safely.
    """
    try:
        ProductColorVariant = apps.get_model('productcolors', 'ProductColorVariant')
    except LookupError:
        logger.debug("productcolors.ProductColorVariant is not available; skipping colour enrichment.")
        return None

    try:
        return (
            ProductColorVariant.objects.select_related(
                'product',
                'color',
                'color__fable5_profile',
                'fable5_details',
            )
            .prefetch_related(
                'images',
                'fable5_fit_rules',
                'fable5_size_rules',
                'fable5_faqs',
                'fable5_details__i18n',
                'fable5_combinations',
                'fable5_combinations__i18n',
                'product__fit_options',
                'product__fable5_fit_notes',
                'product__fable5_option_profiles',
                'product__fable5_option_profiles__i18n',
            )
            .filter(product_id__in=product_ids)
            .order_by('product_id', 'order', 'id')
        )
    except DatabaseError as exc:
        logger.warning("Failed to load ProductColorVariant rows: %s", exc, exc_info=exc)
        return None


def build_color_preview_map(products: Iterable[Any]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Returns mapping {product_id: [colour preview dicts]} suitable for product cards / featured.
    """
    products = list(products)
    product_ids = [p.id for p in products if getattr(p, 'id', None)]
    if not product_ids:
        return {}

    queryset = _load_product_color_variant_queryset(product_ids)
    if queryset is None:
        return {}

    preview_map: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for variant in queryset:
        from fable5.services import variant_public_context

        color = getattr(variant, 'color', None)
        merchandising = variant_public_context(variant)
        fit_merchandising = []
        for fit_code in merchandising.get('available_fit_codes') or []:
            fit_merchandising.append(
                variant_public_context(variant, fit_code=fit_code)
            )
        if fit_merchandising:
            # Cards communicate the lowest purchasable combination price. This
            # avoids presenting a thermo/default colour as more expensive when
            # another allowed fit or colour is cheaper.
            merchandising = min(
                fit_merchandising,
                key=lambda item: Decimal(str(item['final_price'])),
            )
        # Use the prefetched images directly without calling .all() again
        # This prevents N+1 queries
        images = getattr(variant, '_prefetched_objects_cache', {}).get('images', [])
        if not images:
            # Fallback if prefetch didn't work
            images = list(variant.images.all()) if hasattr(variant, 'images') else []

        first_image = ''
        original_image = ''
        if images:
            first_payload = build_optimized_image_payload(images[0].image, display_width=320)
            # ``thumbnail_url`` is a small (~320 px wide) WebP suitable
            # for JS hover-swap on chip clicks. ``original_url`` is the
            # un-optimised source path; pass that through the
            # ``optimized_image`` template tag so it can locate sibling
            # responsive AVIF/WebP variants and emit a proper srcset
            # (the tag derives variant filenames from the source stem
            # — handing it a thumbnail URL like ``foo_320w.webp`` makes
            # the tag look for ``foo_320w_*.{avif,webp}``, which never
            # exist, so it falls back to rendering the 320 px asset
            # at the card's full ~480 px width — visibly soft).
            first_image = first_payload.get('thumbnail_url') or first_payload.get('url') or ''
            original_image = first_payload.get('original_url') or ''
        preview_map[variant.product_id].append(
            {
                'id': variant.id,
                'slug': getattr(variant, 'slug', '') or '',
                'name': _display_color_name(color),
                'primary_hex': getattr(color, 'primary_hex', '') or '',
                'secondary_hex': getattr(color, 'secondary_hex', '') or '',
                'first_image_url': first_image,
                'original_image_url': original_image,
                'is_default': bool(getattr(variant, 'is_default', False)),
                'is_thermo': merchandising['is_thermo'],
                'thermo_note': merchandising['thermo_note'],
                'final_price': merchandising['final_price'],
                'price_difference': merchandising['price_difference'],
                'price_reason': merchandising['price_delta_reason'],
                'has_price_adjustment': merchandising['has_price_adjustment'],
                'fit_rules': merchandising['fit_rules'],
            }
        )

    for product in products:
        variants = preview_map.get(product.id, [])
        prices = [
            Decimal(str(item['final_price']))
            for item in variants
            if item.get('final_price') is not None
        ]
        fallback = Decimal(str(getattr(product, 'final_price', 0) or 0))
        product.card_price_min = min(prices) if prices else fallback
        product.card_price_max = max(prices) if prices else fallback
        product.card_has_variant_prices = product.card_price_min != product.card_price_max
        default_variant = next(
            (item for item in variants if item.get('is_default')),
            variants[0] if variants else None,
        )
        product.card_selected_price = (
            Decimal(str(default_variant['final_price']))
            if default_variant is not None
            else fallback
        )
        product.card_selected_price_reason = (
            default_variant.get('price_reason', '') if default_variant else ''
        )
        product.card_selected_is_thermo = bool(
            default_variant and default_variant.get('is_thermo')
        )

    return preview_map


def build_color_preview_key(variants: Iterable[Dict[str, Any]]) -> str:
    """
    Compact fragment-cache key for rendered colour controls.
    """
    variants = list(variants or [])
    if not variants:
        return "colors:0"

    digest = hashlib.blake2s(digest_size=8)
    for variant in variants:
        for field in (
            "id", "name", "primary_hex", "secondary_hex", "first_image_url",
            "is_default", "is_thermo", "final_price", "price_reason",
            "has_price_adjustment", "fit_rules",
        ):
            digest.update(str(variant.get(field, "")).encode("utf-8"))
            digest.update(b"\0")

    return f"colors:{len(variants)}:{digest.hexdigest()}"


def get_product_color_variant_rows(product) -> List[Any]:
    """Raw ProductColorVariant rows (select_related color, prefetched images),
    memoized on the product instance to avoid duplicate queries when the
    detail view, SEO structured data and variant meta all need them within
    one request."""
    cached = getattr(product, '_color_variant_rows_cache', None)
    if cached is not None:
        return cached
    if not getattr(product, 'id', None):
        return []
    queryset = _load_product_color_variant_queryset([product.id])
    rows = list(queryset) if queryset is not None else []
    try:
        product._color_variant_rows_cache = rows
    except Exception:
        pass
    return rows


def get_active_fit_options(product) -> List[Any]:
    """Active fit options ordered by (order, id), memoized per instance."""
    cached = getattr(product, '_active_fit_options_cache', None)
    if cached is not None:
        return cached
    try:
        rows = list(product.fit_options.filter(is_active=True).order_by('order', 'id'))
    except Exception:
        rows = []
    try:
        product._active_fit_options_cache = rows
    except Exception:
        pass
    return rows


def get_detailed_color_variants(product, lang='uk') -> List[Dict[str, Any]]:
    """
    Returns list of colour variants with full image sets for product detail page.
    Memoized per product instance and language (the detail view needs it several times).
    """
    language = str(lang or 'uk').split('-', 1)[0].lower()
    if language not in {'uk', 'ru', 'en'}:
        language = 'uk'
    cached_by_language = getattr(product, '_detailed_color_variants_cache', None)
    if isinstance(cached_by_language, dict) and language in cached_by_language:
        return cached_by_language[language]
    if not getattr(product, 'id', None):
        return []

    queryset = get_product_color_variant_rows(product)
    if not queryset:
        return []

    variants: List[Dict[str, Any]] = []
    for variant in queryset:
        from fable5.services import variant_public_context

        color = getattr(variant, 'color', None)
        merchandising = variant_public_context(variant, lang=language)
        # Use the prefetched images directly without calling .all() again
        # This prevents N+1 queries
        images = getattr(variant, '_prefetched_objects_cache', {}).get('images', [])
        if not images:
            # Fallback if prefetch didn't work
            images = list(variant.images.all()) if hasattr(variant, 'images') else []
        # Явно сортируем по order/id, чтобы порядок соответствовал админскому драг-н-дропу
        images = sorted(
            images,
            key=lambda image: (getattr(image, "order", 0), getattr(image, "id", 0)),
        )

        image_urls = []
        color_name = _display_color_name(color)
        for index, image in enumerate(images, start=1):
            if not getattr(image, "image", None):
                continue
            payload = build_optimized_image_payload(image.image)
            payload["alt"] = build_product_image_alt(
                product,
                getattr(image, "alt_text", ""),
                color_name=color_name,
                index=index,
            )
            image_urls.append(payload)

        variants.append(
            {
                'id': variant.id,
                'slug': getattr(variant, 'slug', '') or '',
                'name': color_name,
                'primary_hex': getattr(color, 'primary_hex', '') or '',
                'secondary_hex': getattr(color, 'secondary_hex', '') or '',
                'is_default': bool(getattr(variant, 'is_default', False)),
                'is_thermo': merchandising['is_thermo'],
                'thermo_note': merchandising['thermo_note'],
                'thermo_description': merchandising['thermo_description'],
                'final_price': merchandising['final_price'],
                'price_difference': merchandising['price_difference'],
                'price_reason': merchandising['price_delta_reason'],
                'has_price_adjustment': merchandising['has_price_adjustment'],
                'fit_rules': merchandising['fit_rules'],
                'available_fit_codes': merchandising['available_fit_codes'],
                'size_rules': merchandising['size_rules'],
                'display_name': merchandising['display_name'],
                'marketing_html': merchandising['marketing_html'],
                'seo_title': merchandising['seo_title'],
                'seo_description': merchandising['seo_description'],
                'seo_keywords': merchandising['seo_keywords'],
                'images': image_urls,
            }
        )
    try:
        if not isinstance(cached_by_language, dict):
            cached_by_language = {}
        cached_by_language[language] = variants
        product._detailed_color_variants_cache = cached_by_language
    except Exception:
        pass
    return variants
