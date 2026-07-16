"""
Динамический sitemap для SEO.

Использует стандартный Django Sitemap framework.
Конфигурация подключена в twocomms/urls.py через crawler-safe wrapper.
"""
from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from django.utils import timezone
from .models import BlogCategory, BlogPost, Product, Category


# Static routes that should appear in sitemap.
# IMPORTANT: 'search' intentionally excluded (noindex policy).
PUBLIC_STATIC_ROUTE_NAMES = [
    'home',
    'catalog',
    'delivery',
    'about',
    'contacts',
    'cooperation',
    'custom_print',
    'wholesale_page',        # B2B hub — must be indexed
    'help_center',
    'faq',
    'size_guide',
    'care_guide',
    'order_tracking',
    'site_map_page',
    'blog',
    'returns',
    'privacy_policy',
    'terms_of_service',
]

# Priority tiers for static pages
_HIGH_PRIORITY_ROUTES = {'home', 'catalog', 'custom_print', 'wholesale_page'}
_MID_PRIORITY_ROUTES = {'delivery', 'about', 'contacts', 'cooperation', 'faq', 'size_guide'}


class StaticViewSitemap(Sitemap):
    """
    Sitemap для статических страниц.
    - search исключён (noindex policy)
    - wholesale добавлен (B2B hub)
    - lastmod = None для статических страниц (честнее чем timezone.now())

    SEO v1.0 Phase 1 (2026-05-12) — Path A multilingual fix. The Phase
    17e setup (``i18n=True`` + ``alternates=True`` + ``x_default=True``)
    emitted three ``<url>`` entries (uk/ru/en) per static route, each
    pointing to the same path, and listed reciprocal ``<xhtml:link>``
    pairs even though RU/EN copies share UA content. Google
    consolidates the duplicates and Ahrefs reports them as missing
    reciprocal hreflang (258 URLs in the 2026-05-11 CSV). Under Path A
    only the UA URL is indexable, so we collapse the i18n options and
    emit one entry per route. Restore the flags once the RU/EN copy
    decks are actually translated.

    SEO v1.1 Phase 2 (2026-05-15) — RESTORED. RU/EN are no longer
    noindex (per ownership directive). Re-enable ``i18n=True`` +
    ``alternates=True`` + ``x_default=True`` so Google receives proper
    reciprocal hreflang triples and clusters the variants instead of
    flagging them as duplicates.
    """
    changefreq = 'weekly'
    protocol = 'https'
    i18n = True
    alternates = True
    x_default = True

    def items(self):
        return PUBLIC_STATIC_ROUTE_NAMES

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        if item in _HIGH_PRIORITY_ROUTES:
            return 1.0
        if item in _MID_PRIORITY_ROUTES:
            return 0.7
        return 0.5

    def lastmod(self, item):
        # Static pages don't have meaningful lastmod.
        # Returning None is more honest than timezone.now() — Google
        # penalises sites that always return "now" for lastmod.
        return None


class ProductSitemap(Sitemap):
    """
    Sitemap для товаров.
    - lastmod использует updated_at (если доступно) или published_at

    SEO v1.0 Phase 1 (2026-05-12) — Path A multilingual fix. See
    ``StaticViewSitemap`` docstring for the rationale; collapsing the
    ×3 duplication drops the live ``sitemap-products.xml`` payload from
    195 ``<loc>`` rows back to 65 (one per published product).

    SEO v1.1 Phase 2 (2026-05-15) — RESTORED i18n alternates so RU/EN
    PDPs are discoverable and properly clustered with their UA twin.
    """
    changefreq = 'weekly'
    priority = 0.9
    protocol = 'https'
    i18n = True
    alternates = True
    x_default = True

    def items(self):
        return (
            Product.objects
            .filter(status='published')
            .exclude(slug='')
            .only('slug', 'updated_at', 'published_at')
            .order_by('id')
        )

    def lastmod(self, obj):
        # Prefer updated_at (auto_now), fall back to published_at
        return getattr(obj, 'updated_at', None) or getattr(obj, 'published_at', None)

    def location(self, obj):
        return reverse('product', kwargs={'slug': obj.slug})


class ProductVariantSitemap(Sitemap):
    """
    Phase 7.4 — sitemap для ONE-segment path-style variant URLs.

    Phase 21 (2026-05-10) — size-only one-segment variants removed
    from this sitemap. ``/product/<slug>/m/`` is the same page with a
    selected size: the visible content barely changes, and 349 of 418
    pre-Phase-21 variant URLs were size-only — pure crawl waste. They
    remain reachable for users (the URL still resolves) but canonical
    on those pages now points to the base product (see
    ``services.variant_meta``). The sitemap therefore lists only the
    crawl-worthy 1-segment subset:

        * Base ``/product/<slug>/``                   — self-canonical (ProductSitemap).
        * 1-segment ``/product/<slug>/<color>/``      — self-canonical.
        * 1-segment ``/product/<slug>/<fit>/``        — self-canonical.
        * 1-segment ``/product/<slug>/<size>/``       — canonical→base, NOT in sitemap.
        * 2+ segments                                 — canonical→base, NOT in sitemap.

    SEO v1.0 Phase 1 (2026-05-12) — Path A multilingual fix. Collapsed
    the i18n duplication for the same reason as ``StaticViewSitemap``;
    keeping the variant URLs in the sitemap (without alternates) is
    still useful so Google discovers them quickly.
    """
    changefreq = 'weekly'
    priority = 0.7
    protocol = 'https'

    def items(self):
        products = (
            Product.objects
            .filter(status='published')
            .exclude(slug='')
            .prefetch_related('color_variants', 'fit_options')
            .only('id', 'slug', 'title', 'updated_at', 'published_at',
                  'size_grid', 'catalog', 'category')
            .order_by('id')
        )

        entries = []
        for product in products:
            lastmod = getattr(product, 'updated_at', None) or getattr(product, 'published_at', None)
            base_path = f'/product/{product.slug}'

            # Colour variants. Each variant.slug was backfilled in
            # Phase 7.1 migrations — empty-slug rows should never exist
            # in production but we guard anyway.
            for cv in product.color_variants.all():
                if cv.slug:
                    entries.append({
                        'loc': f'{base_path}/{cv.slug}/',
                        'lastmod': lastmod,
                    })

            # Fit options — only active ones are user-facing.
            active_fits = [
                fit for fit in product.fit_options.all()
                if fit.is_active and fit.code
            ]
            for fit in active_fits:
                entries.append({
                    'loc': f'{base_path}/{fit.code}/',
                    'lastmod': lastmod,
                })

            # SEO 2026-05-19 (VILNI deep review §12.3 / §12.4 TASK I).
            # 2-segment ``/product/<slug>/<color>/<fit>/`` URLs are
            # self-canonical (see ``services.variant_meta``) so Google
            # can index them for combined long-tail queries like
            # "чорна футболка оверсайз з принтом". Listing them in the
            # sitemap accelerates discovery without bloating crawl —
            # ~300-400 extra URLs at current catalogue size.
            colour_slugs = [
                cv.slug for cv in product.color_variants.all() if cv.slug
            ]
            for colour_slug in colour_slugs:
                for fit in active_fits:
                    entries.append({
                        'loc': f'{base_path}/{colour_slug}/{fit.code}/',
                        'lastmod': lastmod,
                    })

        return entries

    def lastmod(self, item):
        return item.get('lastmod')

    def location(self, item):
        return item['loc']


class CategorySitemap(Sitemap):
    """
    Sitemap для категорий.
    - lastmod использует updated_at (если доступно)

    SEO v1.0 Phase 1 (2026-05-12) — Path A multilingual fix; see
    ``StaticViewSitemap``.

    SEO v1.1 Phase 2 (2026-05-15) — RESTORED i18n alternates.
    """
    changefreq = 'monthly'
    priority = 0.8
    protocol = 'https'
    i18n = True
    alternates = True
    x_default = True

    def items(self):
        return (
            Category.objects
            .filter(is_active=True)
            .only('slug', 'updated_at')
        )

    def lastmod(self, obj):
        return getattr(obj, 'updated_at', None)

    def location(self, obj):
        return reverse('catalog_by_cat', kwargs={'cat_slug': obj.slug})


class BlogCategorySitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.7
    protocol = 'https'
    i18n = True
    alternates = True
    x_default = True

    def items(self):
        # SEO 2026-06-08 (§3.4) — only list blog categories that actually
        # have published posts (directly, or via active children for parent
        # categories). Empty categories are noindex (see blog_category view)
        # and must not sit in the sitemap as thin/soft-404 URLs.
        published = BlogPost.objects.filter(
            is_published=True, published_at__lte=timezone.now()
        )
        cats = list(
            BlogCategory.objects
            .filter(is_active=True)
            .exclude(slug='')
            .select_related('parent')
            .only('slug', 'parent', 'parent__slug', 'updated_at')
            .order_by('order', 'name')
        )
        result = []
        for category in cats:
            has_posts = published.filter(category=category).exists()
            if not has_posts and category.parent_id is None:
                child_ids = list(
                    BlogCategory.objects
                    .filter(parent=category, is_active=True)
                    .values_list('id', flat=True)
                )
                if child_ids:
                    has_posts = published.filter(category_id__in=child_ids).exists()
            if has_posts:
                result.append(category)
        return result

    def lastmod(self, obj):
        return getattr(obj, 'updated_at', None)

    def location(self, obj):
        return obj.get_absolute_url()


class BlogPostSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.8
    protocol = 'https'
    i18n = True
    alternates = True
    x_default = True

    def items(self):
        return (
            BlogPost.objects
            .filter(is_published=True, published_at__lte=timezone.now())
            .exclude(slug='')
            .only('slug', 'updated_at', 'published_at')
            .order_by('-published_at', '-id')
        )

    def lastmod(self, obj):
        return getattr(obj, 'updated_at', None) or getattr(obj, 'published_at', None)

    def location(self, obj):
        return reverse('blog_post', kwargs={'slug': obj.slug})



class CategoryColorLandingSitemap(Sitemap):
    """Sitemap section for indexable colour×category landing pages.

    Spec: ``.kiro/specs/color-category-landings``. Includes only
    landings with ``is_published=True`` and an active parent category;
    Search Console gets a clean signal that draft landings should not
    be crawled.

    SEO v1.1 Phase 2 (2026-05-15) — i18n alternates so the new
    landing pages are discoverable in every locale.
    """
    changefreq = 'weekly'
    priority = 0.7
    protocol = 'https'
    i18n = True
    alternates = True
    x_default = True

    def items(self):
        from .models import CategoryColorLanding
        return (
            CategoryColorLanding.objects
            .filter(is_published=True, category__is_active=True)
            .select_related('category', 'color')
        )

    def lastmod(self, obj):
        return getattr(obj, 'updated_at', None)

    def location(self, obj):
        return reverse(
            'catalog_by_cat_color',
            kwargs={
                'cat_slug': obj.category.slug,
                'color_slug': obj.color_slug,
            },
        )


class ThematicLandingSitemap(Sitemap):
    """SEO molecular-upgrade US-5 — sitemap section for thematic landings.

    Lists the four indexable themes (military / streetwear / patriotic /
    kharkiv-edition) registered in
    ``storefront.views.catalog.THEMATIC_LANDINGS_CONFIG``.

    Note: i18n flags intentionally disabled — thematic landings are
    served on a single canonical URL per theme; locale switching is
    driven by the visitor's session, not by the URL path. Enabling
    ``i18n=True`` would emit duplicate ``<loc>`` triples that Google
    consolidates as canonical-only-and-points-at-itself, polluting
    the sitemap with 3x noise.
    """

    changefreq = 'weekly'
    priority = 0.7
    protocol = 'https'

    def items(self):
        return [
            'military',
            'streetwear',
            'patriotic',
            'kharkiv-edition',
        ]

    def location(self, item):
        return f"/catalog/theme/{item}/"

    def lastmod(self, item):
        return None
