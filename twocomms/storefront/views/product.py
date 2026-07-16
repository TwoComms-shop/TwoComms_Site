"""
Product views - Детальная информация о товарах.

Содержит views для:
- Детальной страницы товара
- Получения изображений
- Цветовых вариантов
- Отзывов (в будущем)
"""

import json
import re
from itertools import product as option_product

from django.shortcuts import render, get_object_or_404
from django.http import Http404, HttpResponsePermanentRedirect, JsonResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from ..models import Product
from ..services.catalog_helpers import (
    build_product_image_alt,
    get_active_fit_options,
    get_detailed_color_variants,
    get_public_product_order_version,
)
from ..services.image_variants import build_optimized_image_payload
from ..services.size_guides import resolve_product_size_context
from ..services.variant_meta import VariantMetaInputs, build_variant_meta
from ..recommendations import ProductRecommendationEngine
from ..utm_tracking import record_product_view


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
        # Generic Fable5 garment flows explicitly opt a category into the
        # configurator. ProductFitOption rows then extend that flow with the
        # fit axis, including for garments such as hoodies. Legacy rows on
        # unrelated categories remain hidden until an active flow is linked.
        try:
            from fable5.models import GarmentFlow

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


# ВАЖНО: Не кэшируем страницу товара, так как нужен предвыбор размера/цвета из URL параметров
# @cache_page_for_anon(600)  # Отключено для поддержки ?size=M и ?color=X
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
    product = get_object_or_404(
        Product.objects.select_related('category', 'catalog', 'size_grid', 'size_grid__catalog').prefetch_related(
            'images',
            'catalog__size_grids',
            'catalog__options__values',
            'fit_options',
            'faqs',
        ),
        slug=slug,
        status='published',
    )
    # W2-4 (AN-035): record_product_view перенесён НИЖЕ — после решения о
    # legacy-301 редиректе, иначе один просмотр считался дважды
    # (query-string URL + канонический path-URL).
    images = product.images.all()

    # Читаем параметры из URL (?size=M&color=123)
    preselected_size = request.GET.get('size', '').upper()
    preselected_color_id = request.GET.get('color', '')  # ID цветового варианта
    preselected_fit_from_query = str(request.GET.get('fit', '') or '').strip().lower()

    size_context = resolve_product_size_context(product, preselected_size)
    available_sizes = size_context["sizes"]
    preselected_size = size_context["selected_size"]

    # Варианты цветов с изображениями (если есть приложение и данные)
    color_variants = get_detailed_color_variants(product)

    # Fable5 size grids can differ by fit and by colour. Build the complete
    # matrix before parsing variant URLs so a size that exists only in a
    # colour override is still a valid path segment and receives an offer ID.
    has_fable_size_matrix = False
    grid_variants = []
    try:
        from fable5.size_grid_services import build_size_grid_comparison
        from productcolors.models import ProductColorVariant as _GridVariant

        grid_variants = list(
            _GridVariant.objects
            .filter(product=product)
            .select_related(
                'product',
                'color',
                'color__fable5_profile',
                'fable5_details',
            )
            .prefetch_related(
                'fable5_details__i18n',
                'fable5_fit_rules',
                'fable5_size_rules',
                'fable5_faqs',
                'fable5_combinations',
                'fable5_combinations__i18n',
                'product__fit_options',
                'product__fable5_fit_notes',
                'product__fable5_option_profiles',
                'product__fable5_option_profiles__i18n',
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
        fable_size_order = []
        for grid_item in size_grid_comparison:
            fit_code = grid_item.get('fit_code', '')
            for variant_item in grid_item.get('variants', []):
                sizes = [
                    row.get('size')
                    for row in variant_item.get('sizes', [])
                    if row.get('size')
                ]
                variant_size_matrix.setdefault(
                    variant_item.get('variant_id'), {}
                )[fit_code] = sizes
                color_entry = color_entries_by_id.get(variant_item.get('variant_id'))
                if color_entry is not None:
                    color_entry.setdefault('size_guides_by_fit', {})[fit_code] = (
                        variant_item.get('guide') or grid_item.get('guide') or {}
                    )
                for size in sizes:
                    if size not in fable_size_order:
                        fable_size_order.append(size)
        for variant in color_variants:
            variant['available_sizes_by_fit'] = variant_size_matrix.get(
                variant.get('id'), {}
            )
        if fable_size_order:
            has_fable_size_matrix = True
            available_sizes = fable_size_order
            if preselected_size not in available_sizes:
                preselected_size = available_sizes[0]
    except Exception:
        size_grid_comparison = []

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
        available_sizes_upper = {str(s).upper() for s in available_sizes}
        color_slug_to_id = {
            (cv.get('slug') or '').lower(): cv.get('id')
            for cv in color_variants
            if cv.get('slug')
        }
        fit_codes_lower = {
            (opt.code or '').lower()
            for opt in get_active_fit_options(product)
        }

        parsed_size = None
        parsed_color_id = None
        parsed_color_slug = None
        parsed_fit = None
        for segment in path_segments:
            seg_upper = segment.upper()
            seg_lower = segment.lower()
            if parsed_size is None and seg_upper in available_sizes_upper:
                parsed_size = seg_upper
                continue
            if parsed_color_id is None and seg_lower in color_slug_to_id:
                parsed_color_id = color_slug_to_id[seg_lower]
                parsed_color_slug = seg_lower
                continue
            if parsed_fit is None and seg_lower in fit_codes_lower:
                parsed_fit = seg_lower
                continue
            raise Http404(f"Unknown product variant segment: {segment!r}")

        if parsed_size is not None:
            preselected_size = parsed_size
            # Fable grids are authoritative when present. Passing a size that
            # exists only in a color/fit override through the legacy resolver
            # would silently replace it with a different default size.
            if not has_fable_size_matrix:
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
        # path-style URL. Only triggered on the base URL — if the
        # request already has path segments, we honour them as-is.
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

        # Если н��т главного изображения, автоматически выбираем первый цвет
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
        payload["alt"] = build_product_image_alt(product, image.alt_text, index=index)
        extra_image_urls.append(payload)

    primary_image_alt = build_product_image_alt(product, product.main_image_alt, main=True)
    if not product.main_image and color_variants and color_variants[0].get("images"):
        primary_image_alt = color_variants[0]["images"][0].get("alt") or primary_image_alt
    elif not product.main_image and extra_image_urls:
        primary_image_alt = extra_image_urls[0].get("alt") or primary_image_alt

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

    product_faq_items = [
        {"question": faq.question, "answer": faq.answer}
        for faq in product.faqs.filter(is_active=True).order_by("order", "id")
    ]

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
    recommended_products = list(recommendation_engine.get_recommendations(product=product, limit=8))
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

    # Fable5 is colour-first: the first entry is the active colour after the
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
            from fable5.services import variant_public_context

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
                        'price_reason': resolved['price_delta_reason'],
                        'has_price_adjustment': resolved['has_price_adjustment'],
                        'display_name': resolved['display_name'],
                        'marketing_html': resolved['marketing_html'],
                        'seo_title': resolved['seo_title'],
                        'seo_description': resolved['seo_description'],
                        'seo_keywords': resolved['seo_keywords'],
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

    # Generic Fable5 configurator snapshot. Every public surface and the cart
    # resolve the same normalized option dictionary, price, and availability.
    product_option_payload = {"axes": [], "selected_values": {}}
    product_size_options = []
    if color_variants and grid_variants:
        try:
            from fable5.content_resolution import build_combination_key
            from fable5.services import (
                product_option_context,
                variant_allows_options,
                variant_allows_purchase,
                variant_public_context,
            )

            variants_by_id = {variant.id: variant for variant in grid_variants}
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
                axes = option_context.get("axes") or []
                choice_groups = [axis.get("choices") or [] for axis in axes]
                configurations = {}
                if choice_groups and all(choice_groups):
                    for choices in option_product(*choice_groups):
                        values = {
                            axis["code"]: choice["code"]
                            for axis, choice in zip(axes, choices)
                        }
                        if not variant_allows_options(db_variant, values):
                            continue
                        resolved = variant_public_context(
                            db_variant,
                            option_values=values,
                            lang=language,
                        )
                        configurations[build_combination_key(values)] = {
                            "option_values": values,
                            "final_price": resolved["final_price"],
                            "price_difference": resolved["price_difference"],
                            "price_reason": resolved["price_delta_reason"],
                            "has_price_adjustment": resolved["has_price_adjustment"],
                            "display_name": resolved["display_name"],
                            "marketing_html": resolved["marketing_html"],
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
            if selected_configuration:
                selected_variant_merchandising.update(selected_configuration)
                selected_variant_merchandising["price_reason"] = selected_configuration.get("price_reason", "")
                selected_variant_price = selected_configuration["final_price"]
            size_availability = selected_configuration.get("size_availability", {})
            product_size_options = [
                {
                    "value": size,
                    "label": size_context["display_labels"].get(size, size),
                    "is_available": bool(size_availability.get(str(size), True)),
                }
                for size in available_sizes
            ]
        except Exception:
            product_option_payload = {"axes": [], "selected_values": {}}

    if not product_size_options:
        product_size_options = [
            {
                "value": size,
                "label": size_context["display_labels"].get(size, size),
                "is_available": True,
            }
            for size in available_sizes
        ]

    if not product_option_payload.get("axes"):
        try:
            from fable5.services import product_option_context

            product_option_payload = product_option_context(
                product,
                option_values={"fit": preselected_fit_code} if preselected_fit_code else {},
                lang=getattr(request, 'LANGUAGE_CODE', 'uk')[:2],
            )
        except Exception:
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
        product, fit_code=path_fit_code or None
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
        )
    )
    if active_variant_entry is not None:
        if active_variant_entry.get('seo_title'):
            variant_meta['page_title'] = active_variant_entry['seo_title']
        if active_variant_entry.get('seo_description'):
            variant_meta['page_description'] = active_variant_entry['seo_description']
        if active_variant_entry.get('seo_keywords'):
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

    return render(
        request,
        'pages/product_detail.html',
        {
            'product': product,
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
            'product_video': product_video,
            'product_faq_items': product_faq_items,
            'available_sizes': available_sizes,
            'product_size_options': product_size_options,
            'product_option_context': product_option_payload,
            'size_display_labels': size_context["display_labels"],
            'resolved_size_guide': size_context["guide"],
            'resolved_size_profile': size_context["profile"],
            'size_grid_comparison': size_grid_comparison,
            'public_product_order_version': public_product_order_version,
            'fit_options': fit_options,
            'show_fit_selector': bool(fit_options),
            'preselected_fit_code': preselected_fit_code,
            'selected_variant_price': selected_variant_price,
            'selected_variant_original_price': selected_variant_original_price,
            'selected_variant_merchandising': selected_variant_merchandising,
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

        image_urls = []

        # Главное изображение
        if product.main_image:
            image_urls.append({
                'url': product.main_image.url,
                'is_main': True,
                'alt': build_product_image_alt(product, product.main_image_alt, main=True),
            })

        # Дополнительные изображения
        for index, img in enumerate(images, start=1):
            image_urls.append({
                'url': img.image.url,
                'is_main': False,
                'alt': build_product_image_alt(product, img.alt_text, index=index),
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
        color_variants = get_detailed_color_variants(product)

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
        color_variants = get_detailed_color_variants(product)

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
