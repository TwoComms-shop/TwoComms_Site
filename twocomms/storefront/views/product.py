"""
Product views - Детальная информация о товарах.

Содержит views для:
- Детальной страницы товара
- Получения изображений
- Цветовых вариантов
- Отзывов (в будущем)
"""

import json
import logging
import re
from itertools import product as option_product

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.db.models import Prefetch
from django.shortcuts import render, get_object_or_404
from django.http import Http404, HttpResponsePermanentRedirect, JsonResponse
from django.urls import reverse
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from ..models import CatalogOption, CatalogOptionValue, Product
from ..services.catalog_helpers import (
    build_product_image_alt,
    get_active_fit_options,
    get_detailed_color_variants,
    get_public_product_order_version,
)
from ..services.image_variants import build_optimized_image_payload
from ..services.size_guides import resolve_product_size_context
from ..services.variant_meta import VariantMetaInputs, build_variant_meta
from ..services.locale_publication import publication_context
from ..recommendations import ProductRecommendationEngine
from ..utm_tracking import record_product_view
from twocomms.db_resilience import retry_mysql_read
from .utils import cache_page_for_anon


logger = logging.getLogger(__name__)
MAX_PRODUCT_OPTION_COMBINATIONS = 128
CONFIGURATOR_EXPECTED_EXCEPTIONS = (
    ValidationError,
    DatabaseError,
    TypeError,
    ValueError,
)


def _load_product_recommendations(recommendation_engine, product, *, limit):
    """Retry one dropped DB connection without making PDP recommendations fatal."""
    return retry_mysql_read(
        lambda: list(
            recommendation_engine.get_recommendations(
                product=product,
                limit=limit,
            )
        ),
        fallback=[],
    )


def _is_locale_owned_variant_meta(entry, field: str, language: str) -> bool:
    """Return whether a variant SEO override is reviewed for ``language``.

    ``content_resolution`` records the language of the winning row.  RU/EN
    must not publish a Ukrainian legacy field merely because it was available
    as a fallback; Ukrainian may use the canonical legacy row itself.
    """
    language = str(language or "uk").split("-", 1)[0].lower()
    source = str(entry.get(f"{field}_source") or "").strip().lower()
    if not source:
        return False
    if language == "uk":
        return source.endswith(":uk") or source in {"color:legacy", "product:canonical"}
    return source.endswith(f":{language}")


def _resolve_og_availability_flag(product) -> bool:
    """Return True when the product can be sold (Open Graph `instock`).

    SEO v1.0 Phase 11 (2026-05-12) — finding (UU)+(QQ). Open Graph's
    ``product:availability`` only knows two binary states (`instock` /
    `out of stock`), so we collapse the Schema.org availability URI from
    ``StructuredDataGenerator._get_product_availability`` into a single
    boolean. The generator emits ``InStock`` for every purchasable item
    (including made-to-order DTF prints, see finding GSC availability
    2026-06-04) so Facebook Catalog / Pinterest Rich Pins / Telegram
    previews treat on-demand prints as purchasable. Only ``OutOfStock``
    (admin disabled the product) maps to False.
    """
    from ..seo_utils import StructuredDataGenerator

    try:
        availability = StructuredDataGenerator._get_product_availability(product)
    except Exception:
        return True
    return not availability.endswith("/OutOfStock")


# ==================== PRODUCT VIEWS ====================

def _is_tshirt_product(product):
    candidates = [
        getattr(product, 'title', ''),
        getattr(getattr(product, 'category', None), 'name', ''),
        getattr(getattr(product, 'category', None), 'slug', ''),
        getattr(getattr(product, 'catalog', None), 'name', ''),
        getattr(getattr(product, 'catalog', None), 'slug', ''),
    ]
    normalized = ' '.join(str(item or '').lower() for item in candidates)
    tshirt_pattern = re.compile(r'(^|[^a-z0-9а-яіїєґ])(?:футбол\w*|t-?shirts?|tees?)(?=$|[^a-z0-9а-яіїєґ])')
    return bool(tshirt_pattern.search(normalized))


def _resolve_fit_options(product):
    # Per-product opt-out: admins can hide the fit selector entirely.
    if not getattr(product, 'fit_selector_enabled', True):
        return []

    is_tshirt = _is_tshirt_product(product)
    if not is_tshirt:
        # Generic ProductCatalog garment flows explicitly opt a category into the
        # configurator. ProductFitOption rows then extend that flow with the
        # fit axis, including for garments such as hoodies. Legacy rows on
        # unrelated categories remain hidden until an active flow is linked.
        try:
            from product_catalog.models import GarmentFlow

            has_configurable_flow = GarmentFlow.objects.filter(
                categories__id=getattr(product, 'category_id', None),
                is_active=True,
            ).exists()
        except Exception:
            has_configurable_flow = False
        if not has_configurable_flow:
            return []

    # Phase 17 — heal legacy tshirts created before the fit-toggle UI:
    # if no fit_options rows exist yet, lazily create classic+oversize
    # so the storefront selector stops being silently empty.
    if is_tshirt:
        try:
            from ..forms import ensure_default_fit_options_for_tshirt
            ensure_default_fit_options_for_tshirt(product)
            # Healing may have just created rows — drop the per-request memo
            # so get_active_fit_options re-reads them.
            if getattr(product, '_active_fit_options_cache', None) == []:
                del product._active_fit_options_cache
        except Exception:
            pass

    options = get_active_fit_options(product)
    if not options:
        return []

    has_default = any(option.is_default for option in options)
    if not has_default:
        options[0].is_default = True
    return options


# Phase 7.5 — query-string variant segments we know how to promote to
# path URLs. All other query params (``utm_*`` / ``gclid`` / …) are
# preserved verbatim on the redirect so analytics tracking survives.
_REDIRECTABLE_VARIANT_QUERY_KEYS = ("size", "color", "fit")


def _parse_path_variant_segments(
    *,
    path_segments,
    available_sizes,
    color_variants,
    fit_options,
):
    """Resolve variant segments without mutating the selected product state."""
    size_values = {
        str(size).lower(): str(size).upper()
        for size in available_sizes
    }
    color_values = {
        str(variant.get("slug") or "").lower(): variant.get("id")
        for variant in color_variants
        if variant.get("slug")
    }
    fit_values = {
        str(option.code or "").lower(): str(option.code or "")
        for option in fit_options
        if option.code
    }

    axis_values = {
        "color": color_values,
        "size": size_values,
        "fit": fit_values,
    }
    resolved = {}
    owner_segments = {}

    for segment in path_segments:
        normalized_segment = str(segment).lower()
        matching_axes = [
            axis
            for axis, values in axis_values.items()
            if normalized_segment in values
        ]
        if len(matching_axes) != 1:
            raise Http404(f"Unknown or ambiguous product variant segment: {segment!r}")

        axis = matching_axes[0]
        if axis in resolved:
            raise Http404(f"Repeated product variant axis: {axis!r}")

        resolved[axis] = axis_values[axis][normalized_segment]
        if axis == "color":
            resolved["color_slug"] = normalized_segment
        owner_segments[axis] = normalized_segment

    canonical_segments = tuple(
        owner_segments[axis]
        for axis in ("color", "size", "fit")
        if axis in owner_segments
    )
    return resolved, canonical_segments


def _preserve_non_variant_query(request, target_path):
    preserved_query = request.GET.copy()
    for key in _REDIRECTABLE_VARIANT_QUERY_KEYS:
        preserved_query.pop(key, None)
    encoded_query = preserved_query.urlencode()
    if encoded_query:
        return f"{target_path}?{encoded_query}"
    return target_path


def _build_path_variant_redirect(
    *,
    request,
    product,
    available_sizes,
    color_variants,
):
    """Build the ``/product/<slug>/<color>/<size>/<fit>/`` URL a legacy
    ``?size=…&color=…&fit=…`` request should 301-redirect to, or return
    ``None`` when no variant query is present / resolvable.

    Contract:
        * At least one of ``size`` / ``color`` / ``fit`` must be a
          valid value for this product. If nothing resolves, we return
          ``None`` so the view renders the base page normally (that's
          what the old URLs did too).
        * All OTHER query params are preserved verbatim on the
          redirect target — utm, gclid, fbclid et al. must not get
          stripped by a 301 or campaign tracking breaks.
        * Segment order matches the canonical written elsewhere in
          Phase 7: colour first, then size, then fit.
    """
    query = request.GET

    raw_size = str(query.get("size") or "").strip().upper()
    raw_color = str(query.get("color") or "").strip()
    raw_fit = str(query.get("fit") or "").strip().lower()

    if not (raw_size or raw_color or raw_fit):
        return None

    # Resolve size.
    size_segment = ""
    if raw_size:
        available_upper = {str(s).upper() for s in available_sizes}
        if raw_size in available_upper:
            size_segment = raw_size.lower()

    # Resolve colour: legacy URLs used numeric variant IDs.
    color_segment = ""
    if raw_color and color_variants:
        try:
            color_id = int(raw_color)
        except (TypeError, ValueError):
            color_id = None
        if color_id is not None:
            match = next(
                (cv for cv in color_variants if cv.get("id") == color_id),
                None,
            )
            if match and match.get("slug"):
                color_segment = match["slug"]

    # Resolve fit.
    fit_segment = ""
    if raw_fit:
        valid_fit_codes = {
            (opt.code or "").lower()
            for opt in get_active_fit_options(product)
        }
        if raw_fit in valid_fit_codes:
            fit_segment = raw_fit

    # Nothing resolved → let the view handle the stale params quietly.
    # This keeps ``?color=junk`` from breaking the page.
    if not (size_segment or color_segment or fit_segment):
        return None

    path_segments = [seg for seg in (color_segment, size_segment, fit_segment) if seg]
    kwargs = {"slug": product.slug}
    for index, seg in enumerate(path_segments, start=1):
        kwargs[f"v{index}"] = seg
    target_path = reverse("product", kwargs=kwargs)

    # Preserve non-variant query params (utm_*, gclid, fbclid, ref, …).
    preserved = [
        (key, value)
        for key, value in query.lists()
        for value in (value if isinstance(value, list) else [value])
        if key not in _REDIRECTABLE_VARIANT_QUERY_KEYS
    ]
    # ``lists()`` already decomposes multi-values — flatten defensively:
    flat_preserved = []
    for key in query:
        if key in _REDIRECTABLE_VARIANT_QUERY_KEYS:
            continue
        for value in query.getlist(key):
            flat_preserved.append((key, value))
    if flat_preserved:
        from urllib.parse import urlencode
        target_path = f"{target_path}?{urlencode(flat_preserved, doseq=True)}"

    return target_path


def _dedupe_product_faq_items(product):
    """Return active FAQ pairs with exact normalized duplicates removed.

    The first row in editorial order wins. The pair key normalizes case and
    whitespace only, so conflicting answers to the same question remain
    visible for later fact review instead of being silently discarded.
    """
    items = []
    seen_pairs = set()
    queryset = product.faqs.filter(is_active=True).order_by("order", "id")

    for faq in queryset:
        question = str(faq.question or "").strip()
        answer = str(faq.answer or "").strip()
        if not question or not answer:
            continue

        key = (
            " ".join(question.split()).casefold(),
            " ".join(answer.split()).casefold(),
        )
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        items.append({"question": question, "answer": answer})

    return items


def _pdp_cache_condition(request):
    """Cache only canonical, query-free guest PDP requests.

    Attribution and selector query strings must still execute the view so
    analytics and canonical redirects remain accurate. Path-style variants
    are already part of the cache key and are safe to cache independently.
    """
    return not request.GET


def _pdp_cache_prefix(request, view_func):
    """Version PDP entries by the current product and public data version.

    The lookup is a single indexed projection and prevents a cached response
    for a previous test/product row (or an edited product) from being reused
    under the same slug.
    """
    segments = [segment for segment in request.path.strip("/").split("/") if segment]
    try:
        slug = segments[segments.index("product") + 1]
    except (ValueError, IndexError):
        slug = ""
    marker = (
        Product.objects.filter(slug=slug, status="published")
        .values_list("pk", "updated_at")
        .first()
    )
    if marker is None:
        return "product-detail-v2:missing"
    product_id, updated_at = marker
    updated_token = (
        str(int(updated_at.timestamp() * 1_000_000))
        if updated_at is not None
        else "0"
    )
    public_version = get_public_product_order_version()
    language = (get_language() or "uk").split("-", 1)[0].lower()
    return f"product-detail-v2:p{product_id}:u{updated_token}:l{language}:v{public_version}"


# The PDP builds a large product/configurator graph. A short anonymous cache
# prevents navigation bursts from making every worker render the same HTML.
@cache_page_for_anon(
    120,
    key_prefix=_pdp_cache_prefix,
    cache_condition=_pdp_cache_condition,
)
def product_detail(request, slug, v1=None, v2=None, v3=None):
    """
    Детальная страница товара.

    Args:
        slug (str): Уникальный slug товара
        v1/v2/v3 (str|None): Phase 7.2 — optional path-style variant
            segments. Each can carry a size code (e.g. ``m``), a colour
            slug (e.g. ``black``) or a fit code (e.g. ``oversize``).
            The view parses them content-addressably — order does not
            matter — and 404s on any segment that matches none of the
            product's known sizes / colour slugs / fit codes.

    Features:
    - Основная информация о товаре
    - Галерея изображений
    - Цветовые варианты с изображениями
    - Автоматический выбор первого цвета (если нет main_image)
    - SEO breadcrumbs
    - Рекомендованные товары (опционально)
    - Поддержка URL параметра ?size=X для предвыбора размера
    - Поддержка path-URL ``/product/<slug>/<color>/<size>/<fit>/``
    - Генерация offer_ids для синхронизации с пикселями

    Context:
        product: Объект товара
        images: Дополнительные изображения товара
        color_variants: Список цветовых вариантов с изображениями
        auto_select_first_color: Флаг автовыбора первого цвета
        breadcrumbs: Хлебные крошки для SEO
        preselected_size: Предвыбранный размер из URL параметра
        offer_id_map: JSON mapping (color_variant_id, size) -> offer_id для JS
        default_offer_id: Offer ID для текущего выбора (default цвет + размер)
    """
    product_queryset = (
        Product.objects.select_related('category', 'catalog', 'size_grid', 'size_grid__catalog').prefetch_related(
            'images',
            'catalog__size_grids',
            Prefetch(
                'catalog__options',
                queryset=(
                    CatalogOption.objects
                    # ``additional_cost`` is a legacy DecimalField that is
                    # not part of the public size-selector contract. Avoid
                    # fetching it on every PDP request: under the affected
                    # MariaDB connector it is the first DECIMAL converter
                    # touched by this otherwise integer/string path.
                    .only('id', 'catalog_id', 'option_type', 'order')
                    .prefetch_related(
                        Prefetch(
                            'values',
                            queryset=CatalogOptionValue.objects.only(
                                'id', 'option_id', 'value', 'display_name', 'order'
                            ).order_by('order', 'id'),
                        )
                    )
                    .order_by('order', 'id')
                ),
            ),
            'fit_options',
            'faqs',
            'audience_assignments__tag',
            'merch_collection_assignments__collection',
        )
    )
    product = retry_mysql_read(
        lambda: get_object_or_404(
            product_queryset,
            slug=slug,
            status='published',
        )
    )
    # W2-4 (AN-035): record_product_view перенесён НИЖЕ — после решения о
    # legacy-301 редиректе, иначе один просмотр считался дважды
    # (query-string URL + канонический path-URL).
    images = product.images.all()

    # Читаем параметры из URL (?size=M&color=123)
    preselected_size = request.GET.get('size', '').upper()
    preselected_color_id = request.GET.get('color', '')  # ID цветового варианта
    preselected_fit_from_query = str(request.GET.get('fit', '') or '').strip().lower()
    # Product selectors are owned by the PDP UX. Every other query key is
    # inbound attribution or an opaque external parameter and must not create
    # an indexable duplicate of the clean product URL.
    product_query_noindex = bool(
        set(request.GET) - {'size', 'color', 'fit'}
    )

    size_context = resolve_product_size_context(product, preselected_size)
    available_sizes = size_context["sizes"]
    preselected_size = size_context["selected_size"]

    # Варианты цветов с изображениями (если есть приложение и данные)
    language = (getattr(request, 'LANGUAGE_CODE', None) or 'uk').split('-', 1)[0].lower()
    locale_publication = publication_context(product, language)
    # Query-string variant selectors are UX state, not indexable Product
    # owners. Keep the page crawlable for its links, but suppress the
    # alternate cluster whenever the same response is rendered with a facet
    # query; path-owned colour/fit URLs are handled by the separate variant
    # ownership policy below.
    if any(request.GET.get(key) for key in ('color', 'fit', 'size')):
        locale_publication = {**locale_publication, 'indexable': False}
    color_variants = get_detailed_color_variants(product, lang=language)

    # ProductCatalog size grids can differ by fit and by colour. Build the complete
    # matrix before parsing variant URLs so a size that exists only in a
    # colour override is still a valid path segment and receives an offer ID.
    has_catalog_size_matrix = False
    grid_variants = []
    try:
        from product_catalog.size_grid_services import build_size_grid_comparison
        from productcolors.models import ProductColorVariant as _GridVariant

        grid_variants = list(
            _GridVariant.objects
            .filter(product=product)
            .select_related(
                'product',
                'color',
                'color__product_catalog_profile',
                'product_catalog_details',
            )
            .prefetch_related(
                'product_catalog_details__i18n',
                'product_catalog_fit_rules',
                'product_catalog_size_rules',
                'product_catalog_faqs',
                'product_catalog_combinations',
                'product_catalog_combinations__i18n',
                'product__fit_options',
                'product__product_catalog_fit_notes',
                'product__product_catalog_option_profiles',
                'product__product_catalog_option_profiles__i18n',
                'product__product_catalog_axis_presentations',
            )
            .order_by('order', 'id')
        )
        size_grid_comparison = build_size_grid_comparison(
            product,
            variants=grid_variants,
            lang=getattr(request, 'LANGUAGE_CODE', 'uk')[:2],
        )
        variant_size_matrix = {}
        color_entries_by_id = {
            entry.get('id'): entry for entry in color_variants
        }
        catalog_size_order = []
        for grid_item in size_grid_comparison:
            fit_code = grid_item.get('fit_code', '')
            for variant_item in grid_item.get('variants', []):
                sizes = list(variant_item.get('available_sizes') or [])
                variant_size_matrix.setdefault(
                    variant_item.get('variant_id'), {}
                )[fit_code] = sizes
                color_entry = color_entries_by_id.get(variant_item.get('variant_id'))
                if color_entry is not None:
                    color_entry.setdefault('size_guides_by_fit', {})[fit_code] = (
                        variant_item.get('guide') or grid_item.get('guide') or {}
                    )
                for size in sizes:
                    if size not in catalog_size_order:
                        catalog_size_order.append(size)
        for variant in color_variants:
            variant['available_sizes_by_fit'] = variant_size_matrix.get(
                variant.get('id'), {}
            )
        if catalog_size_order:
            has_catalog_size_matrix = True
            available_sizes = catalog_size_order
            if preselected_size not in available_sizes:
                preselected_size = available_sizes[0]
    except Exception:
        size_grid_comparison = []
    size_advisor_enabled = _is_tshirt_product(product) and bool(size_grid_comparison)

    # Phase 7.2 — path-style variant URLs. Segments may arrive in any
    # order; we dispatch content-addressably (a segment is a size if it
    # matches ``available_sizes``, a colour if it matches a variant
    # slug, a fit if it matches a fit code). Unknown segments 404.
    # Path wins over query string.
    path_segments = [s for s in (v1, v2, v3) if s]
    path_fit_code = ""
    path_parsed_size = None
    path_parsed_color_id = None
    path_parsed_color_slug = None
    if path_segments:
        resolved_path, owner_segments = _parse_path_variant_segments(
            path_segments=path_segments,
            available_sizes=available_sizes,
            color_variants=color_variants,
            fit_options=get_active_fit_options(product),
        )
        if tuple(path_segments) != owner_segments:
            owner_kwargs = {"slug": product.slug}
            owner_kwargs.update(
                {
                    f"v{index}": segment
                    for index, segment in enumerate(owner_segments, start=1)
                }
            )
            owner_path = reverse("product", kwargs=owner_kwargs)
            return HttpResponsePermanentRedirect(
                _preserve_non_variant_query(request, owner_path)
            )

        parsed_size = resolved_path.get("size")
        parsed_color_id = resolved_path.get("color")
        parsed_color_slug = resolved_path.get("color_slug")
        parsed_fit = resolved_path.get("fit")

        if parsed_size is not None:
            preselected_size = parsed_size
            # Catalog grids are authoritative when present. Passing a size that
            # exists only in a color/fit override through the legacy resolver
            # would silently replace it with a different default size.
            if not has_catalog_size_matrix:
                size_context = resolve_product_size_context(product, parsed_size)
                preselected_size = size_context["selected_size"]
        if parsed_color_id is not None:
            preselected_color_id = str(parsed_color_id)
        if parsed_fit is not None:
            path_fit_code = parsed_fit

        # Keep parsed values for Phase 7.3 canonical + meta building.
        path_parsed_size = parsed_size
        path_parsed_color_id = parsed_color_id
        path_parsed_color_slug = parsed_color_slug
    else:
        # Phase 7.5 — 301 redirect from legacy query-string variant
        # form (``?size=M&color=123&fit=oversize``) to the canonical
        # path-style URL. Only triggered on the base URL; normalized
        # owner paths above set only the variant axes they represent.
        redirect_url = _build_path_variant_redirect(
            request=request,
            product=product,
            available_sizes=available_sizes,
            color_variants=color_variants,
        )
        if redirect_url is not None:
            return HttpResponsePermanentRedirect(redirect_url)

    # W2-4: просмотр записывается только когда страница реально рендерится
    # (все 301-редиректы уже отработали). Дедуп 30 мин session+product —
    # внутри record_user_action.
    record_product_view(request, product.id, product.title)

    auto_select_first_color = False
    preselected_color = None  # Будем хранить выбранный цвет для шаблона

    if color_variants:
        # Валидируем и ищем предвыбранный цвет
        if preselected_color_id:
            try:
                preselected_color_id_int = int(preselected_color_id)
                # Ищем вариант с таким ID
                preselected_index = next(
                    (idx for idx, variant in enumerate(color_variants)
                     if variant.get('id') == preselected_color_id_int),
                    None
                )
                if preselected_index is not None:
                    preselected_color = preselected_color_id_int
                    # Перемещаем предвыбранный вариант на первое место
                    if preselected_index != 0:
                        preselected_variant = color_variants.pop(preselected_index)
                        color_variants.insert(0, preselected_variant)
            except (ValueError, TypeError):
                pass  # Невалидный ID цвета

        # Если цвет не был предвыбран, находим default вариант
        if preselected_color is None:
            default_index = next(
                (idx for idx, variant in enumerate(color_variants)
                 if variant.get('is_default')),
                0
                )

            if default_index != 0:
                default_variant = color_variants.pop(default_index)
                color_variants.insert(0, default_variant)

        # Устанавливаем первый вариант как default (теперь это либо предвыбранный, либо default)
        for idx, variant in enumerate(color_variants):
            variant['is_default'] = (idx == 0)

        # Если нет главного изображения, автоматически выбираем первый цвет
        if not product.main_image:
            auto_select_first_color = True

        # A colour/fit/size URL is a purchasable variant contract, not merely
        # decorative SEO text. Reject impossible combinations instead of
        # silently rendering another fit under the requested canonical URL.
        selected_entry = color_variants[0]
        if path_fit_code:
            fit_rule = selected_entry.get('fit_rules', {}).get(path_fit_code)
            if fit_rule is not None and not fit_rule.get('is_enabled', True):
                raise Http404("Fit is unavailable for the selected colour")
        if path_parsed_size:
            size_matrix = selected_entry.get('available_sizes_by_fit', {})
            if path_fit_code and path_fit_code in size_matrix:
                allowed_path_sizes = size_matrix[path_fit_code]
            else:
                allowed_path_sizes = [
                    size
                    for sizes in size_matrix.values()
                    for size in sizes
                ]
            if size_matrix and path_parsed_size not in allowed_path_sizes:
                raise Http404("Size is unavailable for the selected colour")

    # Генерируем offer_id mapping для всех комбинаций (цвет × размер)
    # Формат: { "variant_id:size": "offer_id" } или { "default:size": "offer_id" }
    offer_id_map = {}
    default_offer_id = None

    if color_variants:
        # Есть цветовые варианты
        for variant in color_variants:
            variant_id = variant.get('id')
            for size in available_sizes:
                offer_id = product.get_offer_id(variant_id, size)
                key = f"{variant_id}:{size}"
                offer_id_map[key] = offer_id

                # Определяем default offer_id (первый вариант + первый/предвыбранный размер)
                if variant.get('is_default') and default_offer_id is None:
                    if preselected_size and size == preselected_size:
                        default_offer_id = offer_id
                    elif not preselected_size and size == available_sizes[0]:
                        default_offer_id = offer_id
    else:
        # Нет цветовых вариантов - используем default
        for size in available_sizes:
            offer_id = product.get_offer_id(None, size)
            key = f"default:{size}"
            offer_id_map[key] = offer_id

            # Определяем default offer_id
            if default_offer_id is None:
                if preselected_size and size == preselected_size:
                    default_offer_id = offer_id
                elif not preselected_size and size == available_sizes[0]:
                    default_offer_id = offer_id

    # Если default_offer_id не установлен, берем самый первый
    if not default_offer_id and offer_id_map:
        default_offer_id = list(offer_id_map.values())[0]

    offer_id_map_json = json.dumps(offer_id_map)
    extra_image_urls = []
    for index, image in enumerate(images, start=1):
        if not getattr(image, 'image', None):
            continue
        payload = build_optimized_image_payload(image.image)
        payload["alt"] = build_product_image_alt(
            product, image.alt_text, index=index, language=language
        )
        extra_image_urls.append(payload)

    primary_image_alt = build_product_image_alt(
        product, product.main_image_alt, main=True, language=language
    )
    if not product.main_image and color_variants and color_variants[0].get("images"):
        primary_image_alt = color_variants[0]["images"][0].get("alt") or primary_image_alt
    elif not product.main_image and extra_image_urls:
        primary_image_alt = extra_image_urls[0].get("alt") or primary_image_alt

    display_image = product.display_image
    initial_hero_image_url = display_image.url if display_image else ""
    initial_hero_image_alt = primary_image_alt
    if color_variants:
        selected_images = color_variants[0].get("images") or []
        selected_image = next(
            (
                image
                for image in selected_images
                if image.get("original_url") or image.get("url")
            ),
            None,
        )
        if selected_image is not None:
            # The responsive-image tags must start from the source asset, not
            # a generated width variant, so they can discover sibling srcsets.
            initial_hero_image_url = (
                selected_image.get("original_url") or selected_image.get("url") or ""
            )
            initial_hero_image_alt = selected_image.get("alt") or primary_image_alt

    # Видео товара (YouTube) — отдельный слайд в галерее + структурированные данные.
    product_video = None
    if product.has_video:
        product_video = {
            "id": product.youtube_id,
            "embed_url": product.video_embed_url,
            "watch_url": product.video_watch_url,
            "thumbnail_url": product.video_thumbnail_url,
            "title": _("Відео огляд: %(title)s") % {"title": product.title},
        }

    product_faq_items = _dedupe_product_faq_items(product)

    # Генерируем breadcrumbs для SEO
    breadcrumbs = [
        {'name': _('Головна'), 'url': reverse('home')},
        {'name': _('Каталог'), 'url': reverse('catalog')},
    ]

    if product.category:
        breadcrumbs.append({
            'name': product.category.name,
            'url': reverse('catalog_by_cat', kwargs={'cat_slug': product.category.slug})
        })

    breadcrumbs.append({
        'name': product.title,
        'url': reverse('product', kwargs={'slug': product.slug})
    })

    # Получаем рекомендации товаров
    recommendation_engine = ProductRecommendationEngine(user=request.user if hasattr(request, 'user') else None)
    recommended_products = _load_product_recommendations(
        recommendation_engine,
        product,
        limit=8,
    )
    recommended_product_ids = ':'.join(str(rec_product.id) for rec_product in recommended_products)

    # Обрабатываем цветовые превью для рекомендаций
    if recommended_products:
        from django.db.models import prefetch_related_objects
        from ..services.catalog_helpers import build_color_preview_map
        # Bulk-prefetch variant images so card rendering (display_image /
        # homepage_image fallbacks) doesn't issue 2 queries per card.
        try:
            prefetch_related_objects(recommended_products, 'color_variants__images')
        except Exception:
            pass
        preview_map = build_color_preview_map(list(recommended_products))
        for rec_product in recommended_products:
            rec_product.colors_preview = preview_map.get(rec_product.id, [])

    public_product_order_version = get_public_product_order_version()
    fit_options = _resolve_fit_options(product)

    # ProductCatalog is colour-first: the first entry is the active colour after the
    # preselection/default reordering above.  Price, thermo messaging and fit
    # availability must therefore all be derived from that same entry.
    selected_variant_merchandising = color_variants[0] if color_variants else {}
    selected_variant_price = selected_variant_merchandising.get('final_price') or product.final_price

    selected_variant_original_price = product.price

    # Keep every active product-level fit visible. Colour-specific rules mark
    # choices as unavailable instead of removing them and shifting the layout.
    # Phase 7.2 — path fit wins over query fit; fallback chain is
    # path → ``?fit=`` query → default option.
    requested_fit_code = path_fit_code or preselected_fit_from_query
    if fit_options:
        selected_fit = next((option for option in fit_options if option.code == requested_fit_code), None)
        if selected_fit is None:
            selected_fit = next((option for option in fit_options if option.is_default), fit_options[0])
        preselected_fit_code = selected_fit.code
        # Query-selected defaults may be unavailable for the active colour.
        # Choose that colour's first purchasable fit so server-rendered price,
        # SEO and the checked control start from one exact combination.
        if color_variants:
            active_rules = color_variants[0].get('fit_rules', {})
            if not active_rules.get(preselected_fit_code, {}).get('is_enabled', True):
                allowed_fit = next(
                    (
                        option for option in fit_options
                        if active_rules.get(option.code, {}).get('is_enabled', True)
                    ),
                    None,
                )
                if allowed_fit is not None:
                    preselected_fit_code = allowed_fit.code
        for option in fit_options:
            option.is_default = option.code == preselected_fit_code
    else:
        preselected_fit_code = ''

    # Resolve sparse color × fit content and pricing for the active fit. This
    # keeps PDP display, cart snapshots and SEO on the same inheritance layer.
    if preselected_fit_code and color_variants:
        try:
            from product_catalog.services import variant_public_context

            variants_by_id = {variant.id: variant for variant in grid_variants}
            for entry in color_variants:
                db_variant = variants_by_id.get(entry.get('id'))
                if db_variant is None:
                    continue
                by_fit = {}
                for option in fit_options:
                    rule = entry.get('fit_rules', {}).get(option.code, {})
                    if not rule.get('is_enabled', True):
                        continue
                    resolved = variant_public_context(
                        db_variant,
                        fit_code=option.code,
                        lang=getattr(request, 'LANGUAGE_CODE', 'uk')[:2],
                    )
                    by_fit[option.code] = {
                        'final_price': resolved['final_price'],
                        'price_difference': resolved['price_difference'],
                        'price_breakdown': resolved['price_breakdown'],
                        'price_reason': resolved['price_delta_reason'],
                        'has_price_adjustment': resolved['has_price_adjustment'],
                        'display_name': resolved['display_name'],
                        'marketing_html': resolved['marketing_html'],
                        'material_story': resolved['material_story'],
                        'seo_title': resolved['seo_title'],
                        'seo_description': resolved['seo_description'],
                        'seo_keywords': resolved['seo_keywords'],
                        'seo_title_source': resolved['seo_title_source'],
                        'seo_description_source': resolved['seo_description_source'],
                        'seo_keywords_source': resolved['seo_keywords_source'],
                    }
                entry['merchandising_by_fit'] = by_fit
                fit_merchandising = (
                    by_fit.get(preselected_fit_code)
                    or next(iter(by_fit.values()), {})
                )
                entry.update({
                    key: value for key, value in fit_merchandising.items()
                })
        except Exception:
            pass

    selected_variant_merchandising = color_variants[0] if color_variants else {}
    selected_variant_price = selected_variant_merchandising.get('final_price') or product.final_price

    # Generic ProductCatalog configurator snapshot. Every public surface and the cart
    # resolve the same normalized option dictionary, price, and availability.
    product_option_payload = {"axes": [], "selected_values": {}}
    product_size_options = []
    configurator_failed = False
    failed_option_context = None
    variants_by_id = {variant.id: variant for variant in grid_variants}
    if color_variants and grid_variants:
        try:
            from product_catalog.content_resolution import build_combination_key
            from product_catalog.services import (
                product_option_context,
                variant_allows_options,
                variant_allows_purchase,
                variant_allows_size,
                variant_public_context,
            )

            language = getattr(request, 'LANGUAGE_CODE', 'uk')[:2]
            for entry in color_variants:
                db_variant = variants_by_id.get(entry.get('id'))
                if db_variant is None:
                    continue
                seed_values = {"fit": preselected_fit_code} if preselected_fit_code else {}
                option_context = product_option_context(
                    product,
                    variant=db_variant,
                    option_values=seed_values,
                    lang=language,
                )
                failed_option_context = option_context
                axes = option_context.get("axes") or []
                choice_groups = [axis.get("choices") or [] for axis in axes]
                configurations = {}
                if choice_groups and all(choice_groups):
                    combination_count = 1
                    for choices in choice_groups:
                        combination_count *= len(choices)
                        if combination_count > MAX_PRODUCT_OPTION_COMBINATIONS:
                            raise ValidationError(
                                "Product option matrix exceeds the public PDP limit",
                                code="combination_limit",
                            )
                    for choices in option_product(*choice_groups):
                        values = {
                            axis["code"]: choice["code"]
                            for axis, choice in zip(axes, choices)
                        }
                        key = build_combination_key(values)
                        is_available = variant_allows_options(db_variant, values)
                        if not is_available:
                            configurations[key] = {
                                "option_values": values,
                                "is_available": False,
                                "size_availability": {
                                    str(size): False for size in available_sizes
                                },
                            }
                            continue
                        resolved = variant_public_context(
                            db_variant,
                            option_values=values,
                            lang=language,
                        )
                        configurations[key] = {
                            "option_values": values,
                            "is_available": True,
                            "final_price": resolved["final_price"],
                            "price_difference": resolved["price_difference"],
                            "price_breakdown": resolved["price_breakdown"],
                            "price_reason": resolved["price_delta_reason"],
                            "has_price_adjustment": resolved["has_price_adjustment"],
                            "display_name": resolved["display_name"],
                            "marketing_html": resolved["marketing_html"],
                            "material_story": resolved["material_story"],
                            "is_thermo": resolved["is_thermo"],
                            "thermo_description": resolved["thermo_description"],
                            "size_availability": {
                                str(size): variant_allows_purchase(
                                    product,
                                    db_variant,
                                    fit_code=values.get("fit", ""),
                                    size=str(size),
                                    option_values=values,
                                )
                                for size in available_sizes
                            },
                        }
                entry["option_context"] = option_context
                entry["configurations"] = configurations

            product_option_payload = color_variants[0].get("option_context") or product_option_payload
            selected_values = product_option_payload.get("selected_values") or {}
            selected_key = build_combination_key(selected_values)
            selected_configuration = (
                color_variants[0].get("configurations", {}).get(selected_key) or {}
            )
            if selected_configuration.get("is_available") is False:
                available_configurations = [
                    configuration
                    for configuration in color_variants[0].get("configurations", {}).values()
                    if configuration.get("is_available") is not False
                ]
                if available_configurations:
                    selected_configuration = max(
                        available_configurations,
                        key=lambda configuration: sum(
                            configuration.get("option_values", {}).get(axis.get("code"))
                            == selected_values.get(axis.get("code"))
                            for axis in product_option_payload.get("axes", [])
                        ),
                    )
                    selected_values = dict(selected_configuration["option_values"])
                    product_option_payload["selected_values"] = selected_values
                    for axis in product_option_payload.get("axes", []):
                        axis["selected_value"] = selected_values.get(axis.get("code"), "")
            if selected_configuration.get("is_available") is not False and selected_configuration:
                selected_variant_merchandising.update(selected_configuration)
                selected_variant_merchandising["price_reason"] = selected_configuration.get("price_reason", "")
                selected_variant_price = selected_configuration["final_price"]
            size_availability = selected_configuration.get("size_availability", {})
            default_size_availability = not bool(selected_configuration)
            product_size_options = [
                {
                    "value": size,
                    "label": size_context["display_labels"].get(size, size),
                    "is_available": bool(
                        size_availability.get(str(size), default_size_availability)
                    ),
                }
                for size in available_sizes
            ]

        except CONFIGURATOR_EXPECTED_EXCEPTIONS as exc:
            configurator_failed = True
            logger.exception(
                "PDP configurator generation failed for product_id=%s",
                product.pk,
            )
            error_code = (
                "combination_limit"
                if getattr(exc, "code", "") == "combination_limit"
                else "resolver_error"
            )
            product_option_payload = failed_option_context or product_option_payload
            for entry in color_variants:
                entry.setdefault("option_context", product_option_payload)
                entry["configurations"] = {}
                entry["configurator_error"] = error_code
            active_variant_id = color_variants[0].get("id") if color_variants else None
            active_variant = variants_by_id.get(active_variant_id)
            product_size_options = []
            for size in available_sizes:
                try:
                    is_available = (
                        variant_allows_size(active_variant, str(size), preselected_fit_code)
                        if active_variant is not None
                        else True
                    )
                except CONFIGURATOR_EXPECTED_EXCEPTIONS:
                    logger.exception(
                        "Legacy PDP size fallback failed for product_id=%s size=%s",
                        product.pk,
                        size,
                    )
                    is_available = False
                product_size_options.append({
                    "value": size,
                    "label": size_context["display_labels"].get(size, size),
                    "is_available": bool(is_available),
                })

    if not product_size_options:
        product_size_options = [
            {
                "value": size,
                "label": size_context["display_labels"].get(size, size),
                "is_available": True,
            }
            for size in available_sizes
        ]

    all_product_sizes_unavailable = bool(product_size_options) and not any(
        option["is_available"] for option in product_size_options
    )

    # Build the PDP merchandising rail only after fit/configuration resolution.
    # This keeps its thermo, material, and price facts aligned with the same
    # combination that drives the visible title and purchase controls.
    from ..services.product_merchandising import build_product_merchandising_context
    product_merchandising_context = build_product_merchandising_context(
        product,
        language=language,
        selected_variant_context=selected_variant_merchandising,
    )

    if not product_option_payload.get("axes") and not configurator_failed:
        try:
            from product_catalog.services import product_option_context

            product_option_payload = product_option_context(
                product,
                option_values={"fit": preselected_fit_code} if preselected_fit_code else {},
                lang=getattr(request, 'LANGUAGE_CODE', 'uk')[:2],
            )
        except CONFIGURATOR_EXPECTED_EXCEPTIONS:
            logger.exception(
                "PDP option context fallback failed for product_id=%s",
                product.pk,
            )
            product_option_payload = {"axes": [], "selected_values": {}}

    if not fit_options and product_option_payload.get("axes"):
        product_option_payload["axes"] = [
            axis for axis in product_option_payload["axes"]
            if axis.get("code") != "fit"
        ]
        product_option_payload["selected_values"].pop("fit", None)

    # Select the currently active colour's override for the visible comparison
    # tables; both fit cards remain visible side-by-side.
    try:
        selected_grid_variant_id = color_variants[0].get('id') if color_variants else None
        for grid_item in size_grid_comparison:
            selected_grid_variant = next(
                (
                    item for item in grid_item.get('variants', [])
                    if item.get('variant_id') == selected_grid_variant_id
                ),
                None,
            )
            grid_item['display_guide'] = (
                selected_grid_variant.get('guide')
                if selected_grid_variant
                else grid_item.get('guide')
            )
            grid_item['selected_color_name'] = (
                selected_grid_variant.get('color_name', '')
                if selected_grid_variant
                else ''
            )
    except Exception:
        pass

    # Phase 7.3 — dynamic canonical + title/description for path-style
    # variant URLs. Only the path (``/product/x/black/m/``) drives
    # variant meta; ``?size=`` / ``?color=`` query params do NOT (those
    # are a private UX affordance and must not fork canonicals).
    base_path = reverse('product', kwargs={'slug': product.slug})
    active_color_name = ""
    active_color_slug = ""
    active_variant_entry = None
    if path_parsed_color_id is not None and color_variants:
        active_variant_entry = next(
            (v for v in color_variants if v.get('id') == path_parsed_color_id),
            None,
        )
        if active_variant_entry is not None:
            active_color_name = active_variant_entry.get('name') or ""
            active_color_slug = active_variant_entry.get('slug') or path_parsed_color_slug or ""

    active_fit_label = ""
    if path_fit_code and fit_options:
        active_fit_option = next(
            (opt for opt in fit_options if opt.code == path_fit_code),
            None,
        )
        if active_fit_option is not None:
            active_fit_label = active_fit_option.label or ""

    # Phase 15 — per-product SEO landing block (theme-aware copy + per-product
    # top queries + category top_filters/top_menu reuse).
    #
    # SEO v1.0 Phase 2 (2026-05-12) — finding (E) in the master audit.
    # The original call fell back to ``preselected_fit_code`` (the
    # category default) whenever no fit was present in the URL path.
    # That meant the *base* PDP still rendered «Футболка класична
    # (Класична) — деталі моделі» and the fit-specific paragraph —
    # identical copy as ``/product/<slug>/classic/``, i.e. a duplicate.
    # Restrict fit-aware landing generation to requests that actually
    # carry the fit segment in the URL so the base page gets clean,
    # non-duplicate copy and ``/classic/`` retains its unique content.
    from ..services.product_seo_landing import build_landing as _build_product_landing
    product_seo_landing = _build_product_landing(
        product, fit_code=path_fit_code or None, language=language
    )

    variant_meta = build_variant_meta(
        VariantMetaInputs(
            product_title=product.title,
            base_path=base_path,
            current_path=request.path,
            segments_count=len(path_segments),
            color_name=active_color_name or None,
            color_slug=active_color_slug or None,
            size_code=path_parsed_size or None,
            fit_label=active_fit_label or None,
            fit_code=path_fit_code or None,
            language=language,
        )
    )
    if active_variant_entry is not None:
        if (
            active_variant_entry.get('seo_title')
            and _is_locale_owned_variant_meta(active_variant_entry, 'seo_title', language)
        ):
            variant_meta['page_title'] = active_variant_entry['seo_title']
        if (
            active_variant_entry.get('seo_description')
            and _is_locale_owned_variant_meta(
                active_variant_entry, 'seo_description', language
            )
        ):
            variant_meta['page_description'] = active_variant_entry['seo_description']
        if (
            active_variant_entry.get('seo_keywords')
            and _is_locale_owned_variant_meta(
                active_variant_entry, 'seo_keywords', language
            )
        ):
            variant_meta['page_keywords'] = active_variant_entry['seo_keywords']

    # Phase 21 (2026-05-10) — review summary + approved review list for
    # the PDP. Summary feeds the ``aggregateRating`` block (rendered
    # only at >=3 approved reviews via ``product_review_summary``) and
    # the rating chip near the H1. Approved reviews list is paginated
    # at the template layer; we surface the most-recent 10 here as
    # initial render, with helpful_count first as a tie-breaker.
    from reviews.models import Review as _Review, ReviewStatus as _RS
    from reviews.services.aggregate import aggregate_rating_for_product as _aggregate
    from reviews.services.permissions import has_paid_order_with_product as _has_paid_order
    product_review_summary = _aggregate(product)
    approved_reviews = list(
        _Review.objects
        .filter(product=product, status=_RS.APPROVED)
        .select_related("user")
        .prefetch_related("images")
        .order_by("-helpful_count", "-created_at")[:10]
    )
    product_customer_has_paid_order = _has_paid_order(request.user, product)

    # Phase 21 (2026-05-10) — resolve the actual ProductColorVariant
    # instance for the active colour so Product schema / OG / Twitter
    # use variant images on a self-canonical colour PDP. Only fetches
    # when the URL path explicitly selected a colour; ``?color=`` query
    # params still don't fork canonical (Phase 7.3 contract).
    selected_color_variant = None
    if path_parsed_color_id is not None and variant_meta['is_self_canonical']:
        try:
            from productcolors.models import ProductColorVariant as _PCV
            selected_color_variant = (
                _PCV.objects
                .prefetch_related('images')
                .filter(product=product, pk=path_parsed_color_id)
                .first()
            )
        except Exception:
            selected_color_variant = None

    social_image_is_fallback = not bool(display_image)

    social_image_alt = primary_image_alt
    if not product.main_image:
        # ``seo_og_image`` falls back to ``product.display_image`` on a base
        # URL. That property follows the first color by DB order, while the
        # visible SSR hero follows the explicit default color. Keep the
        # social alt tied to the actual fallback asset rather than the hero.
        social_image_alt = product.title or primary_image_alt
        if display_image:
            try:
                display_image_url = display_image.url
            except (AttributeError, ValueError):
                display_image_url = ""
            for variant in color_variants:
                matching_image = next(
                    (
                        image
                        for image in (variant.get("images") or [])
                        if (
                            image.get("original_url") or image.get("url")
                        ) == display_image_url
                    ),
                    None,
                )
                if matching_image is not None:
                    social_image_alt = matching_image.get("alt") or social_image_alt
                    break

    if selected_color_variant is not None:
        try:
            selected_social_image = selected_color_variant.images.all().first()
        except Exception:
            selected_social_image = None
        # Match the ``seo_og_image`` override exactly: only a real first
        # variant image changes the social card away from ``display_image``.
        if selected_social_image and getattr(selected_social_image, "image", None):
            social_image_alt = initial_hero_image_alt
            social_image_is_fallback = False

    return render(
        request,
        'pages/product_detail.html',
        {
            'product': product,
            'locale_publication': locale_publication,
            'product_query_noindex': product_query_noindex,
            'images': images,
            'color_variants': color_variants,
            'auto_select_first_color': auto_select_first_color,
            'breadcrumbs': breadcrumbs,
            'recommended_products': recommended_products,
            'recommended_product_ids': recommended_product_ids,
            'preselected_size': preselected_size,
            'preselected_color': preselected_color,  # ID предвыбранного цвета из ?color=123
            'offer_id_map': offer_id_map_json,
            'default_offer_id': default_offer_id,
            'offer_id_map_data': offer_id_map,
            'extra_image_urls': extra_image_urls,
            'primary_image_alt': primary_image_alt,
            'initial_hero_image_url': initial_hero_image_url,
            'initial_hero_image_alt': initial_hero_image_alt,
            'social_image_alt': social_image_alt,
            'social_image_is_fallback': social_image_is_fallback,
            'product_video': product_video,
            'product_faq_items': product_faq_items,
            'available_sizes': available_sizes,
            'product_size_options': product_size_options,
            'all_product_sizes_unavailable': all_product_sizes_unavailable,
            'product_option_context': product_option_payload,
            'size_display_labels': size_context["display_labels"],
            'resolved_size_guide': size_context["guide"],
            'resolved_size_profile': size_context["profile"],
            'size_grid_comparison': size_grid_comparison,
            'size_advisor_enabled': size_advisor_enabled,
            'public_product_order_version': public_product_order_version,
            'fit_options': fit_options,
            'show_fit_selector': bool(fit_options),
            'preselected_fit_code': preselected_fit_code,
            'selected_variant_price': selected_variant_price,
            'selected_variant_original_price': selected_variant_original_price,
            'selected_variant_merchandising': selected_variant_merchandising,
            'product_merchandising_context': product_merchandising_context,
            # Phase 7.3 — variant-aware SEO meta.
            'variant_canonical_path': variant_meta['canonical_path'],
            'variant_page_title': variant_meta['page_title'],
            'variant_page_description': variant_meta['page_description'],
            'variant_page_keywords': variant_meta.get('page_keywords', ''),
            'variant_is_self_canonical': variant_meta['is_self_canonical'],
            # Phase 21 — selected colour variant instance for Product
            # schema, OG image and Twitter image on self-canonical
            # colour PDPs. ``None`` everywhere else.
            'selected_color_variant': selected_color_variant,
            # Phase 21 — review aggregate + approved review list.
            # ``product_review_summary`` exposes ``count``/``average``/
            # ``histogram``/``show_rating``. Templates read
            # ``show_rating`` (not a hard-coded count) so the rating
            # chip threshold stays anchored to the single source of
            # truth in ``reviews.services.aggregate``. SEO v1.0
            # Phase 12 (2026-05-13) — finding (M) lowered the
            # threshold from 3 to 1.
            'product_review_summary': product_review_summary,
            'approved_reviews': approved_reviews,
            'product_customer_has_paid_order': product_customer_has_paid_order,
            # Phase 15 — per-product SEO landing block.
            'product_seo_landing': product_seo_landing,
            # SEO v1.0 Phase 11 (2026-05-12) — finding (UU). Pre-resolve
            # the in-stock flag at the view layer so the OG product
            # meta in the template doesn't have to import / call the
            # availability helper. Mirrors the same logic Schema.org
            # Product.offers.availability uses (StructuredDataGenerator
            # ._get_product_availability) but bound to the simpler
            # OG vocabulary (`instock` / `out of stock`). Stays True
            # for DTF on-demand catalogue per finding (QQ) so we don't
            # accidentally suppress products that print on demand.
            'product_in_stock_for_og': _resolve_og_availability_flag(product),
        }
    )


def get_product_images(request, product_id):
    """
    AJAX endpoint для получения изображений товара.

    Args:
        product_id (int): ID товара

    Returns:
        JsonResponse: Список URL изображений
    """
    try:
        product = Product.objects.prefetch_related('images').get(id=product_id, status='published')
        images = product.images.all()
        language = (getattr(request, 'LANGUAGE_CODE', None) or 'uk').split('-', 1)[0].lower()

        image_urls = []

        # Главное изображение
        if product.main_image:
            image_urls.append({
                'url': product.main_image.url,
                'is_main': True,
                'alt': build_product_image_alt(
                    product, product.main_image_alt, main=True, language=language
                ),
            })

        # Дополнительные изображения
        for index, img in enumerate(images, start=1):
            image_urls.append({
                'url': img.image.url,
                'is_main': False,
                'alt': build_product_image_alt(
                    product, img.alt_text, index=index, language=language
                ),
            })

        return JsonResponse({
            'success': True,
            'images': image_urls,
            'count': len(image_urls)
        })

    except Product.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Product not found'
        }, status=404)


def get_product_variants(request, product_id):
    """
    AJAX endpoint для получения цветовых вариантов товара.

    Args:
        product_id (int): ID товара

    Returns:
        JsonResponse: Список вариантов с изображениями
    """
    try:
        product = Product.objects.get(id=product_id, status='published')
        language = (getattr(request, 'LANGUAGE_CODE', None) or 'uk').split('-', 1)[0].lower()
        color_variants = get_detailed_color_variants(product, lang=language)

        return JsonResponse({
            'success': True,
            'variants': color_variants,
            'count': len(color_variants)
        })

    except Product.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Product not found'
        }, status=404)


def quick_view(request, product_id):
    """
    Quick view модал для товара (AJAX).

    Args:
        product_id (int): ID товара

    Returns:
        JsonResponse или rendered HTML fragment
    """
    try:
        product = Product.objects.select_related('category').get(id=product_id, status='published')
        language = (getattr(request, 'LANGUAGE_CODE', None) or 'uk').split('-', 1)[0].lower()
        color_variants = get_detailed_color_variants(product, lang=language)

        from django.template.loader import render_to_string

        html = render_to_string('partials/product_quick_view.html', {
            'product': product,
            'color_variants': color_variants
        })

        return JsonResponse({
            'success': True,
            'html': html
        })

    except Product.DoesNotExist:
        return JsonResponse({
            'success': False,
            'error': 'Product not found'
        }, status=404)
