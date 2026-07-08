"""
Catalog views - Каталог товаров и категорий.

Содержит views для:
- Главной страницы (home)
- Каталога товаров (catalog)
- Поиска
- Фильтрации
- AJAX подгрузки товаров
"""

from collections import defaultdict

from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, Http404, HttpResponsePermanentRedirect
from django.core.paginator import Paginator, EmptyPage
from django.db.models import Count, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from ..models import CategoryColorLanding, Product, Category, SurveySession, UserPromoCode, UserAction
from ..pagination import build_homepage_pagination_items
from ..services.card_preview import (
    attach_preferred_card_image,
    enrich_color_preview_with_slugs,
)
from ..services.catalog_helpers import (
    apply_public_product_order,
    build_color_preview_key,
    build_color_preview_map,
    get_categories_cached,
    get_public_category_version,
    get_public_product_order_version,
)
from ..services.category_seo_blocks import (
    get_category_seo_blocks,
    get_category_seo_layout,
)
from ..services.general_catalog_seo import get_general_catalog_seo_layout
from ..services.color_seo_copy import build_catalog_color_seo
from ..services.color_filter import (
    apply_color_filter,
    build_available_colors,
    build_home_color_chips,
    build_reset_url,
    parse_color_filter,
)
from ..services.survey_engine import load_survey_definition
from ..utm_tracking import record_search
from cache_utils import get_fragment_cache
from .utils import (
    cache_page_for_anon,
    HOME_PRODUCTS_PER_PAGE,
    PRODUCTS_PER_PAGE,
    public_product_listing_cache_prefix,
)


# ==================== CATALOG VIEWS ====================

CATALOG_SHOWCASE_CARD_CONFIG = (
    {
        'key': 'longsleeves',
        'number': '01',
        'title': _('Лонгсліви'),
        'subtitle': _('Функціональність. Стиль. Характер.'),
        'image': 'img/catalog/catalog-longsleeves.webp',
        'slugs': ('longslivy', 'longsleeves', 'longsleeve', 'longslivi'),
        'tokens': ('лонг', 'long'),
        'swatches': ('#050505', '#6a6b60', '#e7e1d3', '#8c8f79'),
    },
    {
        'key': 'tshirts',
        'number': '02',
        'title': _('Футболки'),
        'subtitle': _('Графіка, що говорить гучніше за слова.'),
        'image': 'img/catalog/catalog-tshirts.webp',
        'slugs': ('futbolki', 'futbolky', 'tshirts', 't-shirts', 'tshirt', 'tees'),
        'tokens': ('футбол', 'tshirt', 'shirt', 'tee'),
        'swatches': ('#050505', '#3a3d3f', '#62684a', '#ede8dc'),
    },
    {
        'key': 'hoodies',
        'number': '03',
        'title': _('Худі'),
        'subtitle': _('Тепло. Захист. Нічого зайвого.'),
        'image': 'img/catalog/catalog-hoodies.webp',
        'slugs': ('hudi', 'hoodie', 'hoodies', 'khudi'),
        'tokens': ('худі', 'hood'),
        'swatches': ('#050505', '#303436', '#6a6f48', '#efe9dc'),
    },
)


def _pagination_query_prefix(request):
    params = request.GET.copy()
    params.pop('page', None)
    encoded = params.urlencode()
    return f'{encoded}&' if encoded else ''


def _match_showcase_category(categories, config):
    slugs = {slug.lower() for slug in config['slugs']}
    tokens = tuple(token.lower() for token in config['tokens'])

    for category in categories:
        slug = (getattr(category, 'slug', '') or '').lower()
        name = (getattr(category, 'name', '') or '').lower()
        if slug in slugs or any(token in slug or token in name for token in tokens):
            return category

    return None


def _compute_showcase_swatches(category_ids, fallback_per_category, *, min_usage=1):
    """Phase 19i (2026-05-10): showcase swatches reflect REAL DB colours.

    For each category, return ALL distinct colours stocked across
    *published* products, ordered by usage (most-stocked first), as
    rich dicts ``{primary, secondary}`` so the template can render
    split swatches for two-tone colours like "white-burgundy" (where
    secondary_hex is set on ``Color``).

    Design decisions:
    * No usage threshold by default (``min_usage=1``) — even a single
      product's colour is shown so the card is honest about inventory.
      Phase 19h's threshold caused the card to fall back to fake grey
      ramps; the user wants real DB output even if it's only 1–2
      swatches.
    * No fallback padding when the category has any real colours;
      cards visually adapt to 1–N swatches. Fallback is only used as
      the very last resort for empty categories so the layout doesn't
      break before any product exists.
    * Up to 4 swatches per card to keep the visual rhythm consistent.

    Args:
        category_ids: iterable of category PKs to compute for.
        fallback_per_category: ``{cat_id: tuple(hex, ...)}`` defaults
            used ONLY when a category has zero published variants
            (keeps an empty category card from rendering blank).
        min_usage: minimum number of products required for a colour
            to appear; defaults to 1 (no filtering).

    Returns:
        ``{cat_id: tuple({'primary': hex, 'secondary': hex|None}, ...)}``
        with at most 4 entries per category.
    """
    if not category_ids:
        return {}

    from productcolors.models import ProductColorVariant

    # One query grouped by (category_id, primary_hex, secondary_hex).
    # Distinct-product count drives ordering — when one product carries
    # multiple variants of the same colour we still want product-level
    # usage to win, mirroring the chip counts above the catalog grid.
    rows = (
        ProductColorVariant.objects
        .filter(
            product__category_id__in=list(category_ids),
            product__status='published',
        )
        .values(
            'product__category_id',
            'color__primary_hex',
            'color__secondary_hex',
        )
        .annotate(usage=Count('product_id', distinct=True))
        .order_by('product__category_id', '-usage')
    )

    by_category: dict[int, list[dict]] = defaultdict(list)
    seen_per_category: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        cat_id = row['product__category_id']
        primary = (row['color__primary_hex'] or '').strip()
        secondary = (row['color__secondary_hex'] or '').strip() or None
        if not primary:
            continue
        if (row.get('usage') or 0) < min_usage:
            continue
        key = (primary.lower(), (secondary or '').lower())
        if key in seen_per_category[cat_id]:
            continue
        if len(by_category[cat_id]) >= 4:
            continue
        by_category[cat_id].append({'primary': primary, 'secondary': secondary})
        seen_per_category[cat_id].add(key)

    result: dict[int, tuple[dict, ...]] = {}
    for cat_id in category_ids:
        live = by_category.get(cat_id, [])
        if live:
            # Honest reflection of inventory — no fake fallback padding.
            result[cat_id] = tuple(live)
        else:
            # Empty category: keep the legacy palette so the layout
            # doesn't break for a not-yet-stocked category.
            fallback = list(fallback_per_category.get(cat_id, ('#050505',)))[:4]
            result[cat_id] = tuple(
                {'primary': hex_value, 'secondary': None} for hex_value in fallback
            )
    return result


def _build_catalog_showcase_cards(categories):
    matched_categories = {}
    for config in CATALOG_SHOWCASE_CARD_CONFIG:
        category = _match_showcase_category(categories, config)
        if category and getattr(category, 'id', None):
            matched_categories[config['key']] = category

    category_ids = [category.id for category in matched_categories.values()]
    product_counts = {}
    if category_ids:
        product_counts = {
            item['category_id']: item['total']
            for item in Product.objects.filter(
                category_id__in=category_ids,
                status='published',
            ).values('category_id').annotate(total=Count('id'))
        }

    # Phase 19g: build per-category fallback map from the legacy
    # hard-coded palettes so categories with no published variants
    # still render visually identical to the pre-fix design.
    fallback_swatches = {
        category.id: next(
            (cfg['swatches'] for cfg in CATALOG_SHOWCASE_CARD_CONFIG if cfg['key'] == key),
            ('#050505',),
        )
        for key, category in matched_categories.items()
    }
    live_swatches = _compute_showcase_swatches(category_ids, fallback_swatches)

    cards = []
    for config in CATALOG_SHOWCASE_CARD_CONFIG:
        category = matched_categories.get(config['key'])
        # Override the static config swatches with live ones when
        # available; preserve the rest of the config unchanged.
        card = {**config, 'category': category}
        # Phase 19i: legacy ``swatches`` (tuple of hex strings) →
        # ``swatch_specs`` (list of {primary, secondary}). Convert any
        # static config palette so the template only needs one shape.
        legacy_hexes = config.get('swatches') or ()
        card['swatch_specs'] = [
            {'primary': h, 'secondary': None} for h in legacy_hexes[:4]
        ]
        if category:
            # Phase 19h: admin override wins over live; live wins over
            # legacy fallback. Empty override → fall through to live.
            manual = _normalize_swatch_overrides(
                getattr(category, 'showcase_swatch_hexes', None)
            )
            if manual:
                card['swatch_specs'] = list(manual[:4])
            elif category.id in live_swatches:
                card['swatch_specs'] = list(live_swatches[category.id][:4])
        # Keep the legacy ``swatches`` key as a tuple of primaries for
        # any code path that still consumes it (back-compat).
        card['swatches'] = tuple(s['primary'] for s in card['swatch_specs'])
        card['product_count'] = product_counts.get(category.id, 0) if category else None
        cards.append(card)
    return cards


def _normalize_swatch_overrides(value):
    """Phase 19h/i: sanitize admin-entered swatch override.

    Accepts:
    * list of hex strings: ``["#000000", "#fafafa"]``
    * list of objects: ``[{"primary":"#fafafa","secondary":"#c1382f"}]``
    * mixed.

    Returns up to 4 ``{'primary', 'secondary'}`` dicts with normalised
    lowercase hexes; invalid entries are dropped.
    """
    if not value or not isinstance(value, (list, tuple)):
        return ()

    def _norm_hex(raw):
        if not isinstance(raw, str):
            return None
        candidate = raw.strip()
        if not candidate:
            return None
        if not candidate.startswith('#'):
            candidate = '#' + candidate
        candidate = candidate.lower()
        if len(candidate) not in (4, 7):  # #abc or #aabbcc
            return None
        # Hex digits validation.
        if any(ch not in '0123456789abcdef' for ch in candidate[1:]):
            return None
        return candidate

    out = []
    seen = set()
    for raw in value:
        primary = secondary = None
        if isinstance(raw, dict):
            primary = _norm_hex(raw.get('primary') or raw.get('hex') or '')
            secondary = _norm_hex(raw.get('secondary') or '')
        else:
            primary = _norm_hex(raw)
        if not primary:
            continue
        key = (primary, secondary or '')
        if key in seen:
            continue
        seen.add(key)
        out.append({'primary': primary, 'secondary': secondary})
        if len(out) >= 4:
            break
    return tuple(out)


# Legacy alias kept for any external test imports.
_normalize_swatch_hexes = _normalize_swatch_overrides

def _product_cards_queryset():
    return Product.objects.select_related('category').prefetch_related('images', 'color_variants__images').defer(
        'description', 'full_description', 'short_description', 'ai_description', 'ai_keywords',
        'seo_title', 'seo_description', 'seo_keywords', 'seo_schema', 'recommendation_tags',
        'dropship_note', 'unpublished_reason'
    )


HOME_SURVEY_VISIBILITY_CACHE_VERSION = "survey-visible-20260530"


def homepage_cache_prefix(request, view_func):
    base_prefix = public_product_listing_cache_prefix(request, view_func)
    return f"{base_prefix}:{HOME_SURVEY_VISIBILITY_CACHE_VERSION}"


# W3-3: @ensure_csrf_cookie снят — Set-Cookie на каждом анонимном GET
# выключал LiteSpeed page cache; csrftoken выдаётся лениво (/api/bootstrap/).
@cache_page_for_anon(
    300,
    key_prefix=homepage_cache_prefix,
)  # Phase 4.1: 5-мин кэш для анонимов; cart/favs-бейджи идут AJAX, не в кэше
def home(request):
    """
    Главная страница сайта.

    Features:
    - Показывает featured товар
    - Список последних товаров с пагинацией
    - Категории товаров
    - Предпросмотр цветовых вариантов
    """
    # SEO 2026-06-04 — Search Console flagged ``/?page=1`` as a duplicate of
    # the homepage root (it renders identical content with canonical → "/").
    # Collapse it with a 301 so Google retires the duplicate URL and
    # consolidates signal on "/". Only ``page=1`` is redirected; ``page>=2``
    # are legitimate paginator states that stay 200.
    if request.GET.get('page') == '1':
        return HttpResponsePermanentRedirect(reverse('home'))

    # Оптимизированные запросы с select_related и prefetch_related
    featured = apply_public_product_order(
        _product_cards_queryset().filter(
            featured=True,
            status='published'
        )
    ).first()

    fragment_cache = get_fragment_cache()
    categories = get_categories_cached(fragment_cache)
    public_product_order_version = get_public_product_order_version(fragment_cache)
    public_category_version = get_public_category_version(fragment_cache)

    # Пагинация
    page_number = request.GET.get('page', '1')
    product_qs = apply_public_product_order(
        _product_cards_queryset().filter(status='published')
    )
    paginator = Paginator(product_qs, HOME_PRODUCTS_PER_PAGE)

    try:
        page_obj = paginator.get_page(page_number)
    except EmptyPage:
        page_obj = paginator.get_page(paginator.num_pages)

    products = list(page_obj.object_list)

    # Подготавливаем цветовые превью
    preview_products = list(products)
    if featured:
        preview_products.append(featured)

    color_previews = build_color_preview_map(preview_products)
    featured_variants = color_previews.get(featured.id, []) if featured else []

    for product in products:
        colors_preview = color_previews.get(product.id, [])
        setattr(product, 'colors_preview', colors_preview)
        setattr(product, 'colors_preview_key', build_color_preview_key(colors_preview))

    # Проверяем есть ли еще товары для пагинации
    total_products = paginator.count
    has_more = page_obj.has_next()
    homepage_pagination_items = build_homepage_pagination_items(
        current_page=page_obj.number,
        total_pages=paginator.num_pages,
        base_path=reverse("home"),
    )

    survey_def = load_survey_definition()
    survey_ui_home = survey_def.get('ui_copy', {}).get('homepage_block', {}) if survey_def else {}
    survey_ui_modal = survey_def.get('ui_copy', {}).get('modal', {}) if survey_def else {}
    survey_reward = survey_def.get('reward', {}) if survey_def else {}
    survey_key = survey_def.get('survey_key', 'print_feedback_v1') if survey_def else 'print_feedback_v1'
    survey_has_active = False
    survey_block_hidden = False
    if request.user.is_authenticated and survey_def:
        survey_has_active = SurveySession.objects.filter(
            user=request.user,
            survey_key=survey_key,
            status='in_progress',
        ).exists()
        # Hide the homepage survey block for signed-in users who already
        # finished it (completed session) or already hold the reward
        # (UserPromoCode). For anonymous visitors the page is cached for
        # everyone, so their visibility is handled client-side in survey.js
        # via localStorage instead.
        already_completed = SurveySession.objects.filter(
            user=request.user,
            survey_key=survey_key,
            status='completed',
        ).exists()
        already_granted = UserPromoCode.objects.filter(
            user=request.user,
            survey_key=survey_key,
        ).exists()
        already_dismissed = UserAction.objects.filter(
            user=request.user,
            action_type='survey_dismiss',
        ).exists()
        survey_block_hidden = already_completed or already_granted or already_dismissed
    survey_cta_text = survey_ui_home.get(
        'cta_continue_uk' if survey_has_active else 'cta_start_uk',
        'Пройти опитування',
    )

    # ``expires_in_days`` drives the client-side "show again after the promo
    # expired" rule for anonymous visitors (see survey.js).
    survey_expires_in_days = int(survey_reward.get('expires_in_days', 5)) if survey_reward else 5

    # Phase 9 — colour chips near the categories block. Each chip
    # links to ``/catalog/?color=<slug>``; no filter is applied to
    # the homepage itself.
    home_color_chips = build_home_color_chips(
        apply_public_product_order(
            _product_cards_queryset().filter(status='published')
        ),
        reverse('catalog'),
    )

    return render(
        request,
        'pages/index.html',
        {
            'featured': featured,
            'categories': categories,
            'products': products,
            'featured_variants': featured_variants,
            'has_more_products': has_more,
            'current_page': page_obj.number,
            'paginator': paginator,
            'page_obj': page_obj,
            'homepage_pagination_items': homepage_pagination_items,
            'total_products': total_products,
            'survey_ui_home': survey_ui_home,
            'survey_ui_modal': survey_ui_modal,
            'survey_reward': survey_reward,
            'survey_key': survey_key,
            'survey_cta_text': survey_cta_text,
            'survey_has_active': survey_has_active,
            'survey_block_hidden': survey_block_hidden,
            'survey_expires_in_days': survey_expires_in_days,
            'public_product_order_version': public_product_order_version,
            'public_category_version': public_category_version,
            'home_color_chips': home_color_chips,
        }
    )


def load_more_products(request):
    """
    AJAX view для загрузки дополнительных товаров.

    Используется для бесконечной прокрутки на главной странице.

    Returns:
        JsonResponse: HTML фрагмент с товарами + метаданные пагинации
    """
    if request.method == 'GET':
        page = request.GET.get('page', 1)
        per_page = HOME_PRODUCTS_PER_PAGE

        product_qs = apply_public_product_order(
            _product_cards_queryset().filter(status='published')
        )
        paginator = Paginator(product_qs, per_page)

        page_obj = paginator.get_page(page)

        products = list(page_obj.object_list)

        # Подготавливаем цвета для товаров
        color_previews = build_color_preview_map(products)
        for product in products:
            product.colors_preview = color_previews.get(product.id, [])
            product.colors_preview_key = build_color_preview_key(product.colors_preview)

        # Проверяем есть ли еще товары
        total_products = paginator.count
        has_more = page_obj.has_next()
        homepage_pagination_items = build_homepage_pagination_items(
            current_page=page_obj.number,
            total_pages=paginator.num_pages,
            base_path=reverse("home"),
        )

        # Рендерим HTML для товаров
        products_html = render_to_string('partials/products_list.html', {
            'products': products,
            'page': page_obj.number
        })
        pagination_html = render_to_string(
            'partials/home_pagination.html',
            {
                'homepage_pagination_items': homepage_pagination_items,
                'page_obj': page_obj,
                'paginator': paginator,
                'homepage_pagination_base_path': reverse("home"),
            },
            request=request,
        )

        return JsonResponse({
            'html': products_html,
            'has_more': has_more,
            'next_page': page_obj.next_page_number() if has_more else None,
            'total_pages': paginator.num_pages,
            'current_page': page_obj.number,
            'pagination_html': pagination_html,
            'total_products': total_products,
        })

    return JsonResponse({'error': 'Invalid request'}, status=400)


# W3-3: @ensure_csrf_cookie снят (см. комментарий у home)
@cache_page_for_anon(600, key_prefix=public_product_listing_cache_prefix)  # Кэшируем каталог на 10 минут только для анонимов
def catalog(request, cat_slug=None):
    """
    Страница каталога товаров.

    Args:
        cat_slug (str, optional): Slug категории для фильтрации

    Features:
    - Показывает все товары или фильтрует по категории
    - Цветовые варианты товаров
    - Карточки категорий (если не выбрана конкретная категория)
    """
    fragment_cache = get_fragment_cache()
    categories = get_categories_cached(fragment_cache)
    public_product_order_version = get_public_product_order_version(fragment_cache)
    public_category_version = get_public_category_version(fragment_cache)

    if cat_slug:
        category = get_object_or_404(Category, slug=cat_slug, is_active=True)
        base_product_qs = apply_public_product_order(
            _product_cards_queryset().filter(
                category=category,
                status='published',
            )
        )
        show_category_cards = False
    else:
        category = None
        base_product_qs = apply_public_product_order(
            _product_cards_queryset().filter(status='published')
        )
        show_category_cards = True

    # Phase 9 — colour filter (?color=black,red). Build chips from the
    # *unfiltered* queryset so users can always OR-in another colour.
    selected_color_slugs = parse_color_filter(request)
    available_colors = build_available_colors(
        base_product_qs, request, selected_color_slugs, category=category,
    )
    has_active_color_filter = bool(selected_color_slugs)
    color_filter_reset_url = build_reset_url(request) if has_active_color_filter else ''
    product_qs = apply_color_filter(base_product_qs, selected_color_slugs)

    # When a colour filter is active on the catalog root we hide the
    # category showcase cards and surface the matching products list
    # instead — otherwise the user would see no products at all.
    if has_active_color_filter:
        show_category_cards = False

    # Pagination
    paginator = Paginator(product_qs, PRODUCTS_PER_PAGE)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.get_page(page_number)
    except EmptyPage:
        page_obj = paginator.get_page(paginator.num_pages)

    products = list(page_obj.object_list)
    color_previews = build_color_preview_map(products)

    for product in products:
        colors_preview = color_previews.get(product.id, [])
        product.colors_preview = colors_preview
        product.colors_preview_key = build_color_preview_key(colors_preview)

    # Phase 14 — color-filter-aware preview: when ``?color=...`` is set,
    # show the matching variant's image on each card (instead of the
    # default ``homepage_image``).
    enrich_color_preview_with_slugs(products)
    attach_preferred_card_image(products, selected_color_slugs)

    return render(
        request,
        'pages/catalog.html',
        {
            'categories': categories,
            'category': category,
            'products': products,
            'show_category_cards': show_category_cards,
            'catalog_showcase_cards': _build_catalog_showcase_cards(categories) if show_category_cards else [],
            'cat_slug': cat_slug or '',
            'page_obj': page_obj,
            'paginator': paginator,
            'public_product_order_version': public_product_order_version,
            'public_category_version': public_category_version,
            'available_colors': available_colors,
            'selected_color_slugs': selected_color_slugs,
            'has_active_color_filter': has_active_color_filter,
            'color_filter_reset_url': color_filter_reset_url,
            'pagination_query_prefix': _pagination_query_prefix(request),
            # Phase 10 — structured SEO blocks shown after the products grid.
            'category_seo_blocks': get_category_seo_blocks(category) if category else [],
            # Phase 10b — split layout: tabs (top_menu/top_filters/top_queries/
            # top_cards) vs. best_prices pricing table. Per-category catalogs
            # pull from CategorySeoBlock rows; the general /catalog/ root
            # synthesises an in-memory layout (top_menu = all categories,
            # top_filters = available colours, top_queries = curated set)
            # so the same partial renders on every catalog screen.
            'category_seo_layout': (
                get_category_seo_layout(category) if category
                else get_general_catalog_seo_layout(
                    categories=categories,
                    available_colors=available_colors,
                )
            ),
            # Phase 19g — colour-aware SEO copy. Renders on /catalog/ root
            # (brand-level landing) and on any catalog screen with an
            # active colour filter (cross-category or per-category x
            # colour). Returns None for /catalog/<cat>/ without a colour
            # filter so we don't double-up on the existing
            # ``category.description`` SEO text.
            'color_seo_copy': build_catalog_color_seo(
                category=category,
                selected_color_slugs=selected_color_slugs,
                available_colors=available_colors,
            ),
        }
    )


_SEARCH_SYNONYMS = {
    # Latin-keyboard / English / transliterated → UA canonical tokens
    "tshirt":     ["футболк", "тішк", "t-shirt", "tee", "ts"],
    "t-shirt":    ["футболк", "тішк", "tee", "ts"],
    "tee":        ["футболк", "тішк"],
    "hoodie":     ["худі", "hoody", "hd"],
    "hoody":      ["худі", "hoodie", "hd"],
    "longsleeve": ["лонгслів", "long-sleeve", "ls"],
    "long-sleeve": ["лонгслів", "longsleeve", "ls"],
    "sweatshirt": ["світшот", "светшот", "пуловер"],
    "twocomms":   ["twocomms", "ту комс", "ту-комс", "тукомс", "twcomms"],
    "streetwear": ["стрітвеар", "стрітвір", "стрит", "streetwear"],
    "military":   ["мілітарі", "військов"],
    # Generic transliteration shortcuts users type after Cyrillic auto-
    # complete fails (e.g. iOS QWERTY → typed «futbolka»).
    "futbolka":   ["футболк"],
    "khudi":      ["худі"],
    "longsliv":   ["лонгслів"],
}


def _build_search_tokens(query: str) -> list[str]:
    """Expand a free-text query into a list of search tokens.

    SEO v1.0 Phase 11 (2026-05-12) — finding (B5). The original search
    only matched the literal query string against UA fields; English
    tokens (tshirt/hoodie/longsleeve/twocomms) returned 0 results.
    Expand each query word against ``_SEARCH_SYNONYMS`` to reach the UA
    catalogue with the same query. Always include the raw query as
    fallback so existing matches still work.
    """
    raw = (query or "").strip()
    if not raw:
        return []
    tokens: list[str] = [raw]
    for word in raw.lower().split():
        synonyms = _SEARCH_SYNONYMS.get(word)
        if synonyms:
            tokens.extend(synonyms)
        # Also try a hyphen-stripped variant (long-sleeve → longsleeve).
        if "-" in word:
            normalized = word.replace("-", "")
            if normalized in _SEARCH_SYNONYMS:
                tokens.extend(_SEARCH_SYNONYMS[normalized])
    # Deduplicate, preserve order.
    seen: set[str] = set()
    deduped: list[str] = []
    for tok in tokens:
        key = tok.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(tok.strip())
    return deduped


def search(request):
    """
    Поиск товаров.

    Query params:
        q (str): Поисковый запрос
        category (str, optional): Фильтр по категории

    Features:
    - Поиск по названию и описанию
    - Фильтрация по категории
    - Выделение результатов
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Search function called from catalog.py")

    try:
        query = (request.GET.get('q') or '').strip()
        category_slug = request.GET.get('category', '').strip()

        # Используем тот же подход, что и в рабочей версии из views.py
        product_qs = _product_cards_queryset().filter(status='published')

        if query:
            # SEO v1.0 Phase 11 (2026-05-12) — finding (B5). The previous
            # search only ran ICONTAINS against UA-only text fields, so
            # users with the English keyboard layout (Windows default)
            # who typed «tshirt» / «hoodie» / «longsleeve» / «twocomms»
            # got zero results — even though the catalogue has matching
            # products under «футболка» / «худі» / «лонгслів». That's a
            # textbook 0-results spike in Google Search Console «Site
            # search» queries. Build the lookup over a synonym-expanded
            # token set so the search box behaves like the public-facing
            # brand search the audit assumes.
            tokens = _build_search_tokens(query)
            search_q = Q()
            for token in tokens:
                search_q |= (
                    Q(title__icontains=token)
                    | Q(slug__icontains=token)
                    | Q(description__icontains=token)
                    | Q(full_description__icontains=token)
                    | Q(short_description__icontains=token)
                )
            product_qs = product_qs.filter(search_q)
            record_search(request, query)

        # Фильтрация по категории
        selected_category = None
        if category_slug:
            try:
                selected_category = Category.objects.get(slug=category_slug, is_active=True)
                product_qs = product_qs.filter(category=selected_category)
            except Category.DoesNotExist:
                selected_category = None

        fragment_cache = get_fragment_cache()
        categories = get_categories_cached(fragment_cache)
        public_product_order_version = get_public_product_order_version(fragment_cache)
        public_category_version = get_public_category_version(fragment_cache)

        # Phase 9 — colour filter on search results.
        base_search_qs = apply_public_product_order(product_qs)
        selected_color_slugs = parse_color_filter(request)
        available_colors = build_available_colors(
            base_search_qs, request, selected_color_slugs,
            category=selected_category,
        )
        has_active_color_filter = bool(selected_color_slugs)
        color_filter_reset_url = build_reset_url(request) if has_active_color_filter else ''
        filtered_search_qs = apply_color_filter(base_search_qs, selected_color_slugs)

        paginator = Paginator(filtered_search_qs, PRODUCTS_PER_PAGE)
        page_obj = paginator.get_page(request.GET.get('page'))
        product_list = list(page_obj.object_list)
        color_previews = build_color_preview_map(product_list)

        for product in product_list:
            colors_preview = color_previews.get(product.id, [])
            product.colors_preview = colors_preview
            product.colors_preview_key = build_color_preview_key(colors_preview)

        # Phase 14 — color-filter-aware preview on search results too.
        enrich_color_preview_with_slugs(product_list)
        attach_preferred_card_image(product_list, selected_color_slugs)

        return render(
            request,
            'pages/catalog.html',
            {
                'categories': categories,
                'products': product_list,
                'show_category_cards': False,
                'selected_category': selected_category,
                'query': query,
                'results_count': paginator.count,
                'is_search_page': True,
                'page_obj': page_obj,
                'paginator': paginator,
                'public_product_order_version': public_product_order_version,
                'public_category_version': public_category_version,
                'available_colors': available_colors,
                'selected_color_slugs': selected_color_slugs,
                'has_active_color_filter': has_active_color_filter,
                'color_filter_reset_url': color_filter_reset_url,
                'pagination_query_prefix': _pagination_query_prefix(request),
            }
        )
    except Exception as e:
        # Логируем ошибку для отладки
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error in search view: {e}", exc_info=True)

        # Возвращаем пустую страницу с ошибкой
        try:
            fragment_cache = get_fragment_cache()
            categories = get_categories_cached(fragment_cache) if fragment_cache else []
            public_product_order_version = get_public_product_order_version(fragment_cache)
            public_category_version = get_public_category_version(fragment_cache)
        except Exception:
            categories = []
            public_product_order_version = 1
            public_category_version = 1

        return render(
            request,
            'pages/catalog.html',
            {
                'query': request.GET.get('q', ''),
                'products': [],
                'categories': categories,
                'selected_category': None,
                'show_category_cards': False,
                'results_count': 0,
                'is_search_page': True,
                'error': 'Произошла ошибка при поиске. Попробуйте еще раз.',
                'public_product_order_version': public_product_order_version,
                'public_category_version': public_category_version,
            }
        )



# ==================== COLOR × CATEGORY LANDING ====================

def category_color_landing(request, cat_slug, color_slug):
    """Render an indexable colour×category SEO landing page.

    URL: ``/catalog/<cat_slug>/<color_slug>/`` (e.g. ``/catalog/tshirts/black/``).

    Behaviour:
        * 404 if the landing is not published, the category is inactive,
          the colour slug doesn't match a stored landing, or there are
          zero matching live products (anti-thin-content guard).
        * Renders ``pages/category_color_landing.html`` with the
          editorial copy, a paginated product grid filtered to the
          ``(category, colour)`` slice, FAQ, and structured data.
    """
    landing = (
        CategoryColorLanding.objects
        .select_related("category", "color")
        .filter(
            category__slug=cat_slug,
            color_slug=color_slug,
            is_published=True,
            category__is_active=True,
        )
        .first()
    )
    if landing is None:
        raise Http404("Color-category landing not found.")

    base_qs = (
        _product_cards_queryset()
        .filter(
            category=landing.category,
            status="published",
            color_variants__color=landing.color,
        )
        .distinct()
    )
    product_qs = apply_public_product_order(base_qs)

    if not product_qs.exists():
        # Empty grids would create a thin-content / soft-404 risk — refuse
        # to render the page until inventory is replenished.
        raise Http404("No products available for this colour-category combination.")

    paginator = Paginator(product_qs, PRODUCTS_PER_PAGE)
    try:
        page_obj = paginator.get_page(request.GET.get("page"))
    except EmptyPage:
        page_obj = paginator.get_page(paginator.num_pages)

    products = list(page_obj.object_list)
    color_previews = build_color_preview_map(products)
    for product in products:
        colors_preview = color_previews.get(product.id, [])
        product.colors_preview = colors_preview
        product.colors_preview_key = build_color_preview_key(colors_preview)
    enrich_color_preview_with_slugs(products)
    # Pin every card preview to the landing's colour, so the grid reads
    # as a coherent collection at a glance.
    attach_preferred_card_image(products, [landing.color_slug])

    breadcrumb_items = [
        {"name": _("Головна"), "url": "/"},
        {"name": _("Каталог"), "url": reverse("catalog")},
        {
            "name": landing.category.name,
            "url": reverse("catalog_by_cat", kwargs={"cat_slug": landing.category.slug}),
        },
        {"name": landing.display_h1, "url": request.path},
    ]

    # Surface up to 5 sibling colours of the same category, plus up to 3
    # cross-category landings for the same colour. Cheap queries — both
    # use the (category, is_published) and (color, is_published) indexes.
    sibling_landings = list(
        CategoryColorLanding.objects
        .filter(
            category=landing.category,
            is_published=True,
            category__is_active=True,
        )
        .exclude(pk=landing.pk)
        .select_related("color")
        .order_by("order", "color_slug")[:5]
    )
    cross_category_landings = list(
        CategoryColorLanding.objects
        .filter(
            color=landing.color,
            is_published=True,
            category__is_active=True,
        )
        .exclude(pk=landing.pk)
        .select_related("category")
        .order_by("category__order", "order")[:3]
    )

    canonical_path = request.path
    site_base = request.build_absolute_uri("/").rstrip("/")
    canonical_url = f"{site_base}{canonical_path}"

    return render(
        request,
        "pages/category_color_landing.html",
        {
            "landing": landing,
            "category": landing.category,
            "color": landing.color,
            "products": products,
            "page_obj": page_obj,
            "paginator": paginator,
            "breadcrumb_items": breadcrumb_items,
            "canonical_url": canonical_url,
            "canonical_path": canonical_path,
            "faq_items": landing.faq_items or [],
            "sibling_landings": sibling_landings,
            "cross_category_landings": cross_category_landings,
        },
    )


# ==================== THEMATIC LANDINGS (US-5) ====================
#
# Lightweight thematic SEO landings — `/catalog/theme/<theme_slug>/`.
# No new model, no migration: products are filtered by slug / title /
# topic_keywords keywords using the same topic-detection logic that
# powers ``storefront.services.product_seo_block`` (US-3).
#
# Four indexable themes ship out of the box:
#   * military       → military / тактичний / 225 / штурмовий
#   * streetwear     → streetwear / стріт / urban
#   * patriotic      → glory of ukraine / patriotic / ЗСУ / 225
#   * kharkiv-edition → kharkiv / kha-style / харків / pokrovsk
#
# Each landing reuses the existing /catalog/ template stack but
# injects a custom ``thematic_landing`` context with editorial copy and
# a curated breadcrumb so the page is unique enough to index without a
# new template/landing model.

THEMATIC_LANDINGS_CONFIG = {
    "military": {
        "h1": "Військовий стрітвір TwoComms — мілітарі-одяг із харківським кодом",
        "title": "Купити мілітарі футболки, худі та лонгсліви — TwoComms",
        "description": (
            "Військовий streetwear від ветеранського бренду TwoComms: футболки, "
            "худі і лонгсліви з мілітарі-кодом, авторськими принтами 225-го ОШП "
            "і Pokrovsk Girl. Виробництво в Харкові."
        ),
        "intro": (
            "Військова естетика TwoComms — це не косплей і не агітка. Це "
            "щоденний одяг для людей, які тримають форму зсередини: бойових "
            "ветеранів, активних резервістів, цивільних, які знають контекст. "
            "Принти серії 225-го ОШП, харківські відсилки, патріотичні мотиви "
            "без галасу. Виробництво у Харкові, контроль якості у одному цеху, "
            "тираж не масштабується заради знижок."
        ),
        "keywords": (
            "мілітарі, military, тактичний, 225, штурмовий, ошп, kharkiv, "
            "pokrovsk, glory-of-ukraine, war, soldier, "
            "патріотичний, патриотический"
        ),
        "match_keywords": [
            "military", "мілітар", "милитар", "tactical", "тактич", "225",
            "ошп", "штурм", "war", "soldier", "armed", "warrior", "warfare",
        ],
    },
    "streetwear": {
        "h1": "Стрітвір TwoComms — авторський streetwear із Харкова",
        "title": "Купити streetwear футболки, худі, лонгсліви — TwoComms",
        "description": (
            "Streetwear від TwoComms: футболки, худі та лонгсліви з авторським "
            "DTF-друком, oversize-кроєм, харківським ДНК. Streetwear як код, не "
            "декорація. Доставка по Україні."
        ),
        "intro": (
            "Streetwear-крило TwoComms — це чистий міський код без військових "
            "цитат і політичних маркерів. Авторські принти, акуратні графіки, "
            "сильні шрифти, продумана посадка. Streetwear як шифр, який "
            "зчитують ті, хто розуміє контекст. Тут немає випадкових "
            "колаборацій з невідомими виробниками: усе шиється у Харкові, "
            "перевіряється у одному цеху, пакується вручну."
        ),
        "keywords": (
            "стрітвір, streetwear, urban, street, культовий одяг, авторський "
            "принт, oversize, харківський streetwear"
        ),
        "match_keywords": [
            "street", "стрит", "стріт", "urban", "skate", "punk", "graff",
            "reality-bends", "future", "business",
        ],
    },
    "patriotic": {
        "h1": "Патріотичний одяг TwoComms — український streetwear із характером",
        "title": "Патріотичні футболки і худі з принтом ЗСУ — TwoComms",
        "description": (
            "Патріотичний одяг TwoComms: футболки, худі та лонгсліви з "
            "принтами Glory of Ukraine, ЗСУ, харківської серії. Без галасу, "
            "без фейку — реальна підтримка ветеранських проєктів."
        ),
        "intro": (
            "Патріотична лінія TwoComms — це спокійна українська ідентичність "
            "без агітки. Ми не пишемо «слава Україні» гігантськими літерами — "
            "ми вкладаємо знак у графіку, яку зчитують свої. Принти серії "
            "Glory of Ukraine, харківські відсилки, мотиви, повʼязані з "
            "225-м ОШП. Частина прибутку — на підтримку ветеранських "
            "ініціатив через Український ветеранський фонд."
        ),
        "keywords": (
            "патріотичний одяг, glory of ukraine, патриотический, ЗСУ, "
            "слава україні, kharkiv edition"
        ),
        "match_keywords": [
            "glory", "ukraine", "україн", "украин", "патріот", "патриот",
            "kha", "harkiv", "харків", "харьков", "pokrovsk", "покров",
        ],
    },
    "kharkiv-edition": {
        "h1": "Харківський одяг TwoComms — Kharkiv Edition streetwear",
        "title": "Купити одяг із принтом «Харків» — TwoComms Kharkiv Edition",
        "description": (
            "Колекція Kharkiv Edition від TwoComms: футболки, худі та "
            "лонгсліви з харківськими принтами, Pokrovsk Girl, Kha Style. "
            "Виробництво у Харкові."
        ),
        "intro": (
            "Kharkiv Edition — це харківський бренд про харківську оптику. "
            "TwoComms виріс у місті, де зараз і щодня тестується. У серії — "
            "принти, які працюють як знаки для тих, хто розуміє контекст: "
            "Pokrovsk Girl, Kha Style, харківська обласна графіка. Це не "
            "туристичний мерч і не патріотична агітка, а щоденний код міста."
        ),
        "keywords": (
            "харківський одяг, kharkiv edition, харків стрітвір, "
            "kha-style, pokrovsk girl"
        ),
        "match_keywords": [
            "kharkiv", "kha-", "харків", "харьков", "pokrovsk", "покров",
            "district",
        ],
    },
}


@cache_page_for_anon(600, key_prefix=public_product_listing_cache_prefix)
def thematic_landing(request, theme_slug):
    """Render an indexable thematic SEO landing.

    URL: ``/catalog/theme/<theme_slug>/``.

    Filters published products by slug / title keyword match against
    ``THEMATIC_LANDINGS_CONFIG[theme]["match_keywords"]``. The landing
    reuses the existing catalog template but injects a thematic intro
    + curated breadcrumb so the URL stays unique-enough to index.
    """
    config = THEMATIC_LANDINGS_CONFIG.get(theme_slug)
    if not config:
        raise Http404("Unknown theme")

    fragment_cache = get_fragment_cache()
    categories = get_categories_cached(fragment_cache)
    public_product_order_version = get_public_product_order_version(fragment_cache)
    public_category_version = get_public_category_version(fragment_cache)

    # Build keyword-OR filter from match_keywords.
    keyword_q = Q()
    for keyword in config["match_keywords"]:
        keyword_q |= Q(slug__icontains=keyword) | Q(title__icontains=keyword)
    base_qs = apply_public_product_order(
        _product_cards_queryset().filter(status="published").filter(keyword_q)
    )

    # Fall back to all products if the theme matches nothing — surface
    # at least the catalogue rather than a 404, and never show an empty
    # SEO landing (would be flagged as soft-404 by Google).
    if not base_qs.exists():
        base_qs = apply_public_product_order(
            _product_cards_queryset().filter(status="published")
        )

    # Phase 9 colour filter still works on top of the theme.
    selected_color_slugs = parse_color_filter(request)
    available_colors = build_available_colors(
        base_qs, request, selected_color_slugs, category=None
    )
    has_active_color_filter = bool(selected_color_slugs)
    color_filter_reset_url = build_reset_url(request) if has_active_color_filter else ''
    product_qs = apply_color_filter(base_qs, selected_color_slugs)

    paginator = Paginator(product_qs, PRODUCTS_PER_PAGE)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.get_page(page_number)
    except EmptyPage:
        page_obj = paginator.get_page(paginator.num_pages)

    products = list(page_obj.object_list)
    color_previews = build_color_preview_map(products)
    for product in products:
        colors_preview = color_previews.get(product.id, [])
        product.colors_preview = colors_preview
        product.colors_preview_key = build_color_preview_key(colors_preview)
    enrich_color_preview_with_slugs(products)
    attach_preferred_card_image(products, selected_color_slugs)

    canonical_path = f"/catalog/theme/{theme_slug}/"
    canonical_url = (
        request.build_absolute_uri(canonical_path)
        .split('?', 1)[0]
        .rstrip('/') + '/'
    )

    return render(
        request,
        'pages/catalog.html',
        {
            'categories': categories,
            'category': None,
            'thematic_landing': {
                'slug': theme_slug,
                'h1': config["h1"],
                'title': config["title"],
                'description': config["description"],
                'intro': config["intro"],
                'keywords': config["keywords"],
            },
            'products': products,
            'show_category_cards': False,
            'catalog_showcase_cards': [],
            'cat_slug': '',
            'page_obj': page_obj,
            'paginator': paginator,
            'public_product_order_version': public_product_order_version,
            'public_category_version': public_category_version,
            'available_colors': available_colors,
            'selected_color_slugs': selected_color_slugs,
            'has_active_color_filter': has_active_color_filter,
            'color_filter_reset_url': color_filter_reset_url,
            'category_seo_blocks': [],
            'category_seo_layout': None,
            'color_seo_copy': None,
            'thematic_canonical_url': canonical_url,
            'thematic_canonical_path': canonical_path,
            'breadcrumb_items': [
                {'name': 'Головна', 'url': '/'},
                {'name': 'Каталог', 'url': '/catalog/'},
                {'name': config["h1"], 'url': canonical_path},
            ],
        }
    )
