from unittest.mock import patch

import json
from decimal import Decimal

from django.contrib.sites.models import Site
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from orders.models import Order
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product
from storefront.seo_utils import (
    SEOKeywordGenerator,
    SEOMetaGenerator,
    _pick_product_description_source,
    get_product_schema,
)
from storefront.views.static_pages import static_sitemap


@override_settings(SITE_BASE_URL="https://twocomms.shop")
class CanonicalHttpsRegressionTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="80",
            **{"wsgi.url_scheme": "http"},
        )

    def test_home_uses_configured_https_origin_for_seo_urls_on_http_request(self):
        response = self.client.get(reverse("home"), secure=False, follow=True)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('href="https://twocomms.shop/"', html)
        self.assertIn('content="https://twocomms.shop/"', html)
        self.assertNotIn("http://twocomms.shop", html)

    def test_catalog_uses_configured_https_origin_for_seo_urls_on_http_request(self):
        response = self.client.get(reverse("catalog"), secure=False, follow=True)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn('href="https://twocomms.shop/catalog/"', html)
        self.assertIn('content="https://twocomms.shop/catalog/"', html)
        self.assertNotIn("http://twocomms.shop", html)


class ProductPageSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(
            name="SEO Test Category",
            slug="seo-test-category",
            is_active=True,
        )
        self.product = Product.objects.create(
            title="SEO Test Product",
            slug="seo-test-product",
            category=self.category,
            price=100,
            description="SEO regression coverage product.",
            status="published",
        )

    def test_product_page_does_not_leak_template_syntax(self):
        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}), follow=True)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        self.assertNotIn("{% optimized_image", html)
        self.assertNotIn("{#", html)
        self.assertNotIn('data-offer-id-map="{"', html)

    def test_product_page_prefers_manual_meta_and_social_preview_fallback(self):
        self.product.seo_title = "SEO Title Override для товару"
        self.product.seo_description = "Акуратний SEO опис для перевірки метатегів без обривів."
        self.product.seo_keywords = "seo override, twocomms, test product"
        self.product.save(update_fields=["seo_title", "seo_description", "seo_keywords"])

        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<title>SEO Title Override для товару</title>", html=False)
        self.assertContains(response, 'content="Акуратний SEO опис для перевірки метатегів без обривів."', html=False)
        # Phase 21 (2026-05-10) — public ``<meta name="keywords">`` removed.
        # Internal seo_keywords field is preserved in DB but must not leak
        # into rendered HTML (Google ignores the tag and the boilerplate
        # listing creates noise / off-topic keyword stuffing risk).
        self.assertNotContains(response, '<meta name="keywords"', html=False)
        self.assertContains(response, "social-preview.jpg", html=False)
        self.assertContains(response, 'property="og:image:alt"', html=False)

    def test_footer_renders_canonical_phone(self):
        """Phase 21 (Cross-cutting G1) — footer surfaces +380 96 654-32-12
        as a tel: link, matching the Organization JSON-LD contactPoint
        and the Contacts page (NAP consistency).
        """
        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="tel:+380966543212"', html=False)

    def test_product_page_does_not_emit_faq_page_schema(self):
        """Phase 21 (PR-5, T12.2) — PDP no longer carries FAQPage JSON-LD;
        identical FAQ content lives on dedicated support pages
        (/faq/, /delivery/, /povernennya-ta-obmin/, categories).
        """
        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}), follow=True)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        # The Product schema is still present and may carry a Question
        # via "additionalProperty" — we only assert FAQPage isn't there.
        self.assertNotIn('"@type": "FAQPage"', html)

    def test_product_page_does_not_render_meta_keywords(self):
        """Phase 21 — keep the ``<meta name="keywords">`` tag out of HTML."""
        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<meta name="keywords"', html=False)

    def test_product_page_does_not_render_fake_aggregate_rating(self):
        """Phase 21 — fake ``4.9 (45 відгуків)`` block must be gone, and
        ``aggregateRating`` must not appear until the configured
        threshold of approved reviews is reached. SEO v1.0 Phase 12
        (2026-05-13) — finding (M) lowered that threshold to 1 (see
        ``reviews.services.aggregate.MIN_APPROVED_REVIEWS_FOR_RATING``);
        with no reviews on the fixture product the rich-result block
        must still be absent so we keep this regression strictly
        anchored on the no-reviews case.
        """
        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}), follow=True)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertNotIn("45 відгуків", html)
        self.assertNotIn("aggregateRating", html)

    def test_product_page_does_not_emit_legacy_inline_organization(self):
        """Phase 21 — only the helper-driven Organization (with stable @id)
        should appear; the legacy hardcoded inline copy in ``base.html``
        is removed.
        """
        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}), follow=True)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertEqual(html.count('"@id": "https://twocomms.shop/#organization"'), 1)

    def test_seo_og_image_uses_selected_variant_first_photo(self):
        """Phase 21 (PR-2 T7.3) — when ``selected_variant`` is passed,
        ``seo_og_image`` returns its first image URL (absolute) so OG
        and Twitter previews on a colour-variant PDP show the actual
        colour user is looking at.
        """
        from django.template import Context, Template
        from productcolors.models import Color, ProductColorVariant

        color = Color.objects.create(name="OGColor", primary_hex="#777777")
        variant = ProductColorVariant.objects.create(
            product=self.product, color=color, slug="ogcolor", stock=3,
        )
        # No image attached → falls back to default OG path; assert no crash.
        rendered = Template(
            "{% load seo_tags %}{% seo_og_image product=p selected_variant=v %}"
        ).render(Context({"p": self.product, "v": variant}))
        self.assertIsInstance(rendered, str)

    def test_product_schema_embeds_top5_nested_reviews(self):
        """Phase 21 (PR-4c3) — when ≥3 approved reviews exist, schema
        carries up to 5 nested Review blocks for rich-result eligibility.
        """
        from reviews.models import Review, ReviewStatus
        from reviews.services.aggregate import aggregate_rating_for_product

        for i in range(6):
            Review.objects.create(
                product=self.product, author_name=f"Reviewer {i}",
                anon_key=f"k{i}", rating=5 - (i % 5),
                body="Чудовий товар, рекомендую усім друзям. " * 3,
                status=ReviewStatus.APPROVED, helpful_count=6 - i,
            )
        summary = aggregate_rating_for_product(self.product)
        schema = json.loads(get_product_schema(self.product, review_summary=summary))
        self.assertIn("review", schema)
        self.assertEqual(len(schema["review"]), 5)
        for block in schema["review"]:
            self.assertEqual(block["@type"], "Review")
            self.assertIn("reviewRating", block)
            self.assertIn("author", block)

    def test_product_schema_embeds_aggregate_rating_only_above_threshold(self):
        """Phase 21 (PR-4b) — Product schema's ``aggregateRating`` block
        is gated by ``ProductReviewSummary.show_rating``. SEO v1.0
        Phase 12 (2026-05-13) — finding (M) lowered the threshold
        from 3 to 1 (see
        ``reviews.services.aggregate.MIN_APPROVED_REVIEWS_FOR_RATING``)
        so the rich result is now available the moment a single
        approved review is moderated. The regression preserves the
        contract: with zero approved reviews no rating block must
        leak; with one or more it must appear and carry the exact
        ``reviewCount`` Google reads in the rich-result preview.
        """
        from reviews.models import Review, ReviewStatus
        from reviews.services.aggregate import aggregate_rating_for_product

        # No reviews → no aggregateRating, no review_summary passed.
        schema = json.loads(get_product_schema(self.product))
        self.assertNotIn("aggregateRating", schema)

        # 1st approved review crosses the (new) threshold immediately.
        Review.objects.create(
            product=self.product, author_name="A", anon_key="k",
            rating=5, body="x" * 30, status=ReviewStatus.APPROVED,
        )
        summary = aggregate_rating_for_product(self.product)
        schema = json.loads(get_product_schema(self.product, review_summary=summary))
        self.assertTrue(summary.show_rating)
        self.assertIn("aggregateRating", schema)
        self.assertEqual(schema["aggregateRating"]["reviewCount"], "1")
        self.assertEqual(schema["aggregateRating"]["bestRating"], "5")
        self.assertEqual(schema["aggregateRating"]["worstRating"], "1")

        # Adding more approved reviews keeps the block and updates count.
        for r in (4, 5):
            Review.objects.create(
                product=self.product, author_name="A", anon_key="k",
                rating=r, body="x" * 30, status=ReviewStatus.APPROVED,
            )
        summary = aggregate_rating_for_product(self.product)
        schema = json.loads(get_product_schema(self.product, review_summary=summary))
        self.assertTrue(summary.show_rating)
        self.assertEqual(schema["aggregateRating"]["reviewCount"], "3")

    def test_product_schema_url_uses_canonical_path_when_provided(self):
        """Phase 21 — Product schema ``url`` follows the page canonical.

        Self-canonical colour PDP must emit the colour URL; size-only or
        multi-segment combos must collapse back to the base URL.
        """
        schema_self = json.loads(get_product_schema(
            self.product, canonical_path=f"/product/{self.product.slug}/black/"
        ))
        self.assertEqual(
            schema_self["url"], f"https://twocomms.shop/product/{self.product.slug}/black/"
        )
        self.assertEqual(schema_self["offers"]["url"], schema_self["url"])

        schema_base = json.loads(get_product_schema(
            self.product, canonical_path=f"/product/{self.product.slug}/"
        ))
        self.assertEqual(
            schema_base["url"], f"https://twocomms.shop/product/{self.product.slug}/"
        )

    def test_product_schema_uses_instock_for_made_to_order(self):
        """SEO 2026-06-04 (GSC availability) — TwoComms is a made-to-order
        DTF shop, so a purchasable product must emit the Google-accepted
        ``InStock`` enum even when every ``ProductColorVariant.stock`` is 0.
        Previously the generator emitted ``MadeToOrder``, which Google's
        Product spec rejects ("Недопустимое перечислимое значение в поле
        availability"), discarding the whole Offer and triggering the
        critical offers/review/aggregateRating error. Only an admin
        explicitly disabling the product (``is_dropship_available=False``)
        may surface ``OutOfStock``.
        """
        color = Color.objects.create(name="Black", primary_hex="#000000")
        variant = ProductColorVariant.objects.create(product=self.product, color=color, stock=0)

        # All variants stock=0 but product still sellable → InStock.
        schema = json.loads(get_product_schema(self.product))
        self.assertEqual(schema["offers"]["availability"], "https://schema.org/InStock")

        # A tracked variant with positive stock stays InStock too.
        variant.stock = 3
        variant.save(update_fields=["stock"])
        schema = json.loads(get_product_schema(self.product))
        self.assertEqual(schema["offers"]["availability"], "https://schema.org/InStock")

        # Admin disabled the product → the only path to OutOfStock.
        self.product.is_dropship_available = False
        self.product.save(update_fields=["is_dropship_available"])
        schema = json.loads(get_product_schema(self.product))
        self.assertEqual(schema["offers"]["availability"], "https://schema.org/OutOfStock")

    def test_product_schema_never_emits_made_to_order(self):
        """Guard against a regression to the GSC-rejected ``MadeToOrder``
        availability enum anywhere in the Product schema (top-level Offer).
        """
        Color = __import__(
            "productcolors.models", fromlist=["Color"]
        ).Color
        color = Color.objects.create(name="Olive", primary_hex="#59604A")
        ProductColorVariant.objects.create(product=self.product, color=color, stock=0)

        schema_json = get_product_schema(self.product)
        self.assertNotIn("MadeToOrder", schema_json)


class ProductGroupSchemaTests(TestCase):
    """SEO 2026-06-04 (GSC ProductGroup) — the ProductGroup node must carry
    at least one resolvable variant with ``offers`` and stay locale-
    consistent with the in-page Product ``@id`` graph. Previously it only
    held bare ``@id`` references to off-page nodes, which made Google raise
    the critical «set one of hasVariant.offers / review / aggregateRating».
    """

    def setUp(self):
        self.category = Category.objects.create(
            name="PG Category", slug="pg-category", is_active=True,
        )
        self.product = Product.objects.create(
            title="PG Hoodie", slug="pg-hoodie", category=self.category,
            price=1200, description="PG coverage.", status="published",
        )
        # SEO 2026-06-08 — ProductGroup is now emitted only for products
        # with >=2 colour variants (single-colour SKUs rely on the
        # standalone Product node). Create two colours so the group exists.
        black = Color.objects.create(name="Black", primary_hex="#000000")
        olive = Color.objects.create(name="Olive", primary_hex="#59604A")
        ProductColorVariant.objects.create(
            product=self.product, color=black, slug="black", stock=0,
        )
        ProductColorVariant.objects.create(
            product=self.product, color=olive, slug="olive", stock=0,
        )

    def _group(self, canonical_path=None):
        from storefront.seo_utils import StructuredDataGenerator
        return StructuredDataGenerator.generate_product_group_schema(
            self.product, canonical_path=canonical_path
        )

    def test_group_has_variant_with_resolvable_offers(self):
        group = self._group()
        self.assertIsNotNone(group)
        self.assertEqual(group["@type"], "ProductGroup")
        variants = group["hasVariant"]
        # Inline Product nodes (one per colour variant), not bare @id refs.
        self.assertGreaterEqual(len(variants), 2)
        for node in variants:
            self.assertEqual(node["@type"], "Product")
            self.assertIn("offers", node)
            self.assertEqual(node["offers"]["@type"], "Offer")
            self.assertEqual(
                node["offers"]["availability"], "https://schema.org/InStock"
            )
            self.assertEqual(node["offers"]["priceCurrency"], "UAH")

    def test_group_variants_declare_size(self):
        """SEO 2026-06-08 (§2.2) — every hasVariant Product node must carry
        ``size`` so Google Merchant does not raise «Отсутствует поле size».
        """
        group = self._group()
        for node in group["hasVariant"]:
            self.assertIn("size", node)
            self.assertTrue(node["size"])

    def test_group_has_no_at_id_collision_with_product_node(self):
        """SEO 2026-06-08 (§2.1) — the standalone Product ``#product`` @id
        must NOT be reused by any hasVariant node on the same page. The old
        design reused it, which made Google raise «Недопустимый тип объекта
        в поле parent_node». Variant nodes use their own variant-URL @ids.
        """
        canonical = "/product/pg-hoodie/"
        product_schema = json.loads(
            get_product_schema(self.product, canonical_path=canonical)
        )
        group = self._group(canonical_path=canonical)
        variant_ids = {node.get("@id") for node in group["hasVariant"]}
        self.assertNotIn(product_schema["@id"], variant_ids)
        # Each variant declares its membership in the group.
        for node in group["hasVariant"]:
            self.assertEqual(node.get("inProductGroupWithID"), self.product.slug)

    def test_group_variants_are_locale_consistent(self):
        group = self._group(canonical_path="/en/product/pg-hoodie/")
        self.assertTrue(
            group["@id"].startswith("https://twocomms.shop/en/product/pg-hoodie/")
        )
        # Every variant URL carries the same /en/ prefix.
        for node in group["hasVariant"]:
            self.assertIn("/en/product/pg-hoodie/", node["@id"])

    def test_single_colour_product_emits_no_group(self):
        """SEO 2026-06-08 (§3.5) — a single-colour product must NOT emit a
        ProductGroup (the standalone Product covers it). This removes the
        GSC parent_node / size criticals at the source for ~58 SKUs.
        """
        cat = Category.objects.create(
            name="Solo Cat", slug="solo-cat", is_active=True,
        )
        solo = Product.objects.create(
            title="Solo Tee", slug="solo-tee", category=cat,
            price=900, description="Solo.", status="published",
        )
        from storefront.seo_utils import StructuredDataGenerator
        color = Color.objects.create(name="Coyote", primary_hex="#81613C")
        ProductColorVariant.objects.create(
            product=solo, color=color, slug="coyote", stock=0,
        )
        group = StructuredDataGenerator.generate_product_group_schema(solo)
        self.assertIsNone(group)

    def test_group_never_emits_made_to_order(self):
        group_json = json.dumps(self._group(), ensure_ascii=False)
        self.assertNotIn("MadeToOrder", group_json)


class VariantCanonicalPhase21Tests(SimpleTestCase):
    """Phase 21 (2026-05-10) — size-only one-segment URLs collapse to
    the base product canonical; colour and fit one-segment URLs stay
    self-canonical.
    """

    def _build(self, *, segments, **kwargs):
        from storefront.services.variant_meta import VariantMetaInputs, build_variant_meta
        defaults = dict(
            product_title="Tee",
            base_path="/product/tee/",
            current_path="/product/tee/m/",
            segments_count=segments,
            color_name=None,
            color_slug=None,
            size_code=None,
            fit_label=None,
            fit_code=None,
        )
        defaults.update(kwargs)
        return build_variant_meta(VariantMetaInputs(**defaults))

    def test_size_only_single_segment_canonical_collapses_to_base(self):
        result = self._build(
            segments=1,
            current_path="/product/tee/m/",
            size_code="M",
        )
        self.assertEqual(result["canonical_path"], "/product/tee/")
        self.assertFalse(result["is_self_canonical"])

    def test_color_only_single_segment_is_self_canonical(self):
        result = self._build(
            segments=1,
            current_path="/product/tee/black/",
            color_name="Чорний",
            color_slug="black",
        )
        self.assertEqual(result["canonical_path"], "/product/tee/black/")
        self.assertTrue(result["is_self_canonical"])

    def test_fit_only_single_segment_is_self_canonical(self):
        result = self._build(
            segments=1,
            current_path="/product/tee/oversize/",
            fit_label="Оверсайз",
            fit_code="oversize",
        )
        self.assertEqual(result["canonical_path"], "/product/tee/oversize/")
        self.assertTrue(result["is_self_canonical"])

    def test_color_plus_size_collapses_to_base(self):
        result = self._build(
            segments=2,
            current_path="/product/tee/black/m/",
            color_name="Чорний",
            color_slug="black",
            size_code="M",
        )
        self.assertEqual(result["canonical_path"], "/product/tee/")
        self.assertFalse(result["is_self_canonical"])


class ProductVariantSitemapPhase21Tests(TestCase):
    """Phase 21 — size-only one-segment URLs are no longer in the
    variant sitemap. Colour and fit URLs continue to appear.
    """

    def setUp(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product

        self.category = Category.objects.create(
            name="Sitemap Phase21",
            slug="sitemap-phase21",
            is_active=True,
        )
        self.product = Product.objects.create(
            title="Phase21 Tee",
            slug="phase21-tee",
            category=self.category,
            price=300,
            status="published",
        )
        color = Color.objects.create(name="Black", primary_hex="#000000")
        ProductColorVariant.objects.create(
            product=self.product, color=color, slug="black", stock=5
        )

    def test_variant_sitemap_excludes_size_only_urls(self):
        from storefront.sitemaps import ProductVariantSitemap

        urls = {entry["loc"] for entry in ProductVariantSitemap().items()}

        self.assertIn("/product/phase21-tee/black/", urls)
        # Size-only paths must NOT appear; sample the most common sizes.
        for size in ("/m/", "/l/", "/xl/", "/s/"):
            self.assertNotIn(f"/product/phase21-tee{size}", urls)


class CategorySeoOverridePhase21Tests(TestCase):
    """Phase 21 — manual ``seo_title`` / ``seo_h1`` / ``seo_description``
    on Category drive the catalog page when filled, and fall back to
    the boilerplate when blank.
    """

    def setUp(self):
        from django.core.cache import cache
        from storefront.models import Category
        # ``catalog`` view wraps responses in ``cache_page_for_anon`` — clear
        # the cache so tests don't see each other's rendered output.
        cache.clear()
        self.category = Category.objects.create(
            name="Категорія Тест",
            slug="kateg-test",
            is_active=True,
        )
        self.client = Client()

    def _get(self):
        return self.client.get(
            reverse("catalog_by_cat", kwargs={"cat_slug": self.category.slug}),
            follow=True, secure=True, HTTP_HOST="twocomms.shop",
        )

    def test_blank_overrides_keep_boilerplate(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<title>Категорія Тест — TwoComms</title>", html=False)

    def test_filled_overrides_replace_title_and_description(self):
        self.category.seo_title = "Унікальний тайтл — TwoComms"
        self.category.seo_h1 = "Унікальний H1 категорії"
        self.category.seo_description = "Унікальний опис категорії для перевірки фолбеків."
        self.category.save(update_fields=["seo_title", "seo_h1", "seo_description"])

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<title>Унікальний тайтл — TwoComms</title>", html=False)
        self.assertContains(
            response,
            'content="Унікальний опис категорії для перевірки фолбеків."',
            html=False,
        )
        self.assertContains(response, "Унікальний H1 категорії", html=False)


class GeneralCatalogSeoColorlessQueriesTests(SimpleTestCase):
    """Phase 21 — curated top queries on the bottom catalog block must
    not link to ``?color=`` URLs (those pages are ``noindex, follow``).
    """

    def test_curated_top_queries_do_not_link_to_color_filtered_pages(self):
        from storefront.services.general_catalog_seo import _CURATED_TOP_QUERIES

        for entry in _CURATED_TOP_QUERIES:
            with self.subTest(label=entry["label"]):
                self.assertNotIn("?color=", entry["url"])


class OrderSuccessSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.order = Order.objects.create(
            full_name="SEO Test User",
            phone="+380991112233",
            email="seo@example.com",
            city="Kyiv",
            np_office="1",
            pay_type="cod",
            payment_status="unpaid",
            total_sum=Decimal("200.00"),
        )

    def test_order_success_page_is_noindex(self):
        # W1-2: order_success is owner-only now — mark session as the buyer's.
        session = self.client.session
        session["recent_order_ids"] = [self.order.id]
        session.save()
        response = self.client.get(reverse("order_success", kwargs={"order_id": self.order.id}), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'content="noindex, nofollow"', html=False)


@override_settings(
    NOVA_POSHTA_FALLBACK_ENABLED=False,
    COMPRESS_ENABLED=False,
    COMPRESS_OFFLINE=False,
)
class ServicePageSeoMetaRegressionTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_key_service_pages_override_default_social_meta(self):
        cases = [
            (
                reverse("about"),
                '<meta name="twitter:title" content="TwoComms — не крапка. Продовження.">',
            ),
            (
                reverse("contacts"),
                # SEO Audit 2026-05-15 (Part 4) — extended contacts title
                # to add channel inventory + geo signal. The original 20-char
                # title was the shortest meta_title across the storefront.
                '<meta name="twitter:title" content="Контакти TwoComms — телефон, Telegram, Instagram | Харків">',
            ),
            (
                reverse("delivery"),
                # SEO Audit 2026-05-15 (Part 4) — extended delivery title
                # from 28 to 58 chars: front-loads carrier (Нова Пошта),
                # lead-time (1-2 дні) and geo (Україна).
                '<meta property="og:title" content="Доставка і оплата TwoComms — Нова Пошта, 1–3 дні по Україні">',
            ),
            (
                reverse("cooperation"),
                '<meta property="og:title" content="Співпраця з TwoComms — дропшипінг, опт і бренд-партнерства">',
            ),
        ]

        for url, expected_meta in cases:
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_meta, html=False)

    def test_about_page_uses_brand_story_layout(self):
        response = self.client.get(reverse("about"), secure=True, follow=True)

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertContains(
            response,
            'pro-brand-page pb-page',
            html=False,
        )
        self.assertContains(response, 'data-pro-brand-video', html=False)
        self.assertNotContains(response, 'data-brand-scroll', html=False)
        self.assertContains(response, 'aria-label="Breadcrumb"', html=False)
        self.assertContains(response, '"@type": "FAQPage"', html=False)
        self.assertContains(response, 'href="https://twocomms.shop/pro-brand/"', html=False)
        self.assertContains(response, 'content="https://twocomms.shop/pro-brand/"', html=False)
        self.assertContains(response, '"@type": "Organization"', html=False)
        self.assertNotContains(response, '"@type": ["Organization", "OnlineStore"]', html=False)
        self.assertContains(response, '"name": "Про бренд TwoComms"', html=False)
        self.assertContains(response, 'Що означає назва TwoComms')
        self.assertContains(response, 'Знак для своїх')
        self.assertContains(response, 'Походження бренду')
        self.assertContains(response, 'Якість, яка доводить ідею')
        self.assertContains(response, 'Streetwear / Military-adjacent / Made in Ukraine')
        self.assertNotContains(response, 'Brand film / soon')
        self.assertNotContains(response, 'IN DEVELOPMENT')
        self.assertEqual(html.count("<h1"), 1)
        self.assertContains(response, 'data-page-shell="marketing"', html=False)
        self.assertContains(response, 'data-analytics-mode="passive"', html=False)
        self.assertNotContains(response, 'points-modal-template', html=False)
        self.assertNotContains(response, 'js/ui-fallback.js', html=False)

    def test_legacy_about_url_redirects_to_pro_brand(self):
        response = self.client.get("/about/", secure=True, follow=False)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/pro-brand/")

    def test_llms_txt_prefers_pro_brand_canonical_url(self):
        response = self.client.get("/llms.txt", secure=True)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("https://twocomms.shop/pro-brand/", body)
        self.assertNotIn("https://twocomms.shop/about/", body)

    def test_delivery_page_faq_schema_matches_visible_content(self):
        response = self.client.get(reverse("delivery"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '"@type": "FAQPage"', html=False)
        self.assertContains(response, "Чи можна оплатити товар при отриманні?")
        self.assertNotContains(response, "Чи є таблиця розмірів?")

    @patch("storefront.views.static_pages.SizeGrid.objects.filter")
    @patch("storefront.views.static_pages.Product.objects.filter")
    def test_support_pages_render_breadcrumb_schema_consistently(self, product_filter_mock, size_grid_filter_mock):
        product_filter_mock.return_value.exclude.return_value.select_related.return_value.only.return_value.order_by.return_value.__getitem__.return_value = []
        size_grid_filter_mock.return_value.select_related.return_value.order_by.return_value = []

        routes = [
            reverse("help_center"),
            reverse("faq"),
            reverse("delivery"),
            reverse("returns"),
            reverse("privacy_policy"),
            reverse("terms_of_service"),
            reverse("about"),
            reverse("size_guide"),
        ]

        for url in routes:
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '"@type": "BreadcrumbList"', html=False)
                self.assertContains(response, 'aria-label="Breadcrumb"', html=False)


class ContactsSeoSignalRegressionTests(TestCase):
    def test_contacts_use_real_catalog_price_range_without_placeholder_addresses(self):
        category = Category.objects.create(
            name="Contacts SEO products",
            slug="contacts-seo-products",
            is_active=True,
        )
        Product.objects.create(
            title="Entry product",
            slug="entry-product",
            category=category,
            price=880,
            status="published",
        )
        Product.objects.create(
            title="Premium product",
            slug="premium-product",
            category=category,
            price=2550,
            status="published",
        )

        response = self.client.get(reverse("contacts"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '"priceRange": "880-2550 UAH"', html=False)
        self.assertNotContains(response, '"priceRange": "₴₴"', html=False)
        self.assertNotContains(response, "Хрещатик", html=False)
        self.assertNotContains(response, "Соборний", html=False)


class CategoryNavigationSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = Category.objects.create(
            name="Breadcrumb Category",
            slug="breadcrumb-category",
            is_active=True,
        )

    def test_category_page_renders_visible_breadcrumbs(self):
        response = self.client.get(reverse("catalog_by_cat", kwargs={"cat_slug": self.category.slug}), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="breadcrumb"', html=False)
        self.assertContains(response, f'href="{reverse("catalog")}"', html=False)
        self.assertContains(response, self.category.name)

    def test_catalog_root_renders_breadcrumbs_and_breadcrumb_schema(self):
        response = self.client.get(reverse("catalog"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="breadcrumb"', html=False)
        self.assertContains(response, reverse("home"))
        self.assertContains(response, '"@type": "BreadcrumbList"', html=False)


class SitemapSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )
        self.factory = RequestFactory()

    def test_sitemap_prefers_pro_brand_url_and_not_legacy_about(self):
        request = self.factory.get(reverse("django.contrib.sitemaps.views.sitemap"), secure=True)
        response = static_sitemap(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://twocomms.shop/pro-brand/")
        self.assertNotContains(response, "https://twocomms.shop/about/")

    def test_sitemap_does_not_set_analytics_cookie(self):
        response = self.client.get(reverse("django.contrib.sitemaps.views.sitemap"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("twc_vid", response.cookies)

    def test_sitemap_headers_are_crawler_safe(self):
        response = self.client.get(reverse("django.contrib.sitemaps.views.sitemap"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "public, max-age=3600")
        self.assertNotIn("X-Robots-Tag", response)

    @override_settings(SITE_BASE_URL="https://twocomms.shop", SITE_ID=1)
    def test_django_sitemap_uses_configured_origin_not_sites_domain(self):
        # /sitemap.xml is now a sitemap-INDEX (Phase 4). It should reference
        # children at the canonical SITE_BASE_URL, never the django.contrib.sites
        # domain.
        Site.objects.update_or_create(
            id=1,
            defaults={"domain": "example.com", "name": "Example"},
        )

        response = self.client.get(reverse("django.contrib.sitemaps.views.sitemap"), secure=True)

        self.assertEqual(response.status_code, 200)
        # Index format: <sitemapindex> with <sitemap><loc> children.
        self.assertContains(response, "<sitemapindex", html=False)
        self.assertContains(response, "<loc>https://twocomms.shop/sitemap-static.xml</loc>", html=False)
        self.assertContains(response, "<loc>https://twocomms.shop/sitemap-products.xml</loc>", html=False)
        self.assertContains(response, "<loc>https://twocomms.shop/sitemap-categories.xml</loc>", html=False)
        self.assertContains(response, "<loc>https://twocomms.shop/sitemap-images.xml</loc>", html=False)
        self.assertNotContains(response, "https://example.com", html=False)

    def test_sitemap_static_section_has_pro_brand(self):
        response = self.client.get(reverse("sitemap_static"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        self.assertContains(response, "https://twocomms.shop/pro-brand/")

    def test_sitemap_images_section_uses_image_namespace(self):
        response = self.client.get(reverse("sitemap_images"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        self.assertContains(response, "xmlns:image=\"http://www.google.com/schemas/sitemap-image/1.1\"")


class StructuredDataPhase5Tests(TestCase):
    """Phase 5 — Schema.org Organization, WebSite and policy regressions."""

    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )

    def test_home_page_includes_organization_schema_with_stable_id(self):
        response = self.client.get(reverse("home"), secure=True, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '"@type": "Organization"', html=False)
        self.assertContains(response, '"@id": "https://twocomms.shop/#organization"', html=False)
        self.assertContains(response, '"sameAs"', html=False)

    def test_home_page_includes_website_schema_with_searchaction(self):
        response = self.client.get(reverse("home"), secure=True, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '"@type": "WebSite"', html=False)
        self.assertContains(response, '"@id": "https://twocomms.shop/#website"', html=False)
        self.assertContains(response, '"@type": "SearchAction"', html=False)
        # SearchAction must use the real /search/ endpoint registered in storefront/urls.py.
        self.assertContains(response, "search/?q={search_term_string}", html=False)

    def test_organization_schema_has_logo_and_contact_point(self):
        from storefront.seo_utils import StructuredDataGenerator

        schema = StructuredDataGenerator.generate_organization_schema()

        self.assertEqual(schema["@type"], "Organization")
        self.assertTrue(schema["logo"].startswith("https://"))
        self.assertEqual(schema["contactPoint"]["@type"], "ContactPoint")
        self.assertIn("telephone", schema["contactPoint"])

    def test_product_schema_uses_policy_module_constants(self):
        """Phase 5 contract: SHIPPING_OPTIONS / RETURN_POLICY come from
        ``services/policy.py`` so any tariff change updates the schema
        in a single place.
        """
        from storefront.seo_utils import StructuredDataGenerator
        from storefront.services.policy import (
            APPLICABLE_COUNTRY,
            CURRENCY,
            RETURN_POLICY,
            shipping_tiers_as_dicts,
        )

        # Policy-module dataclasses round-trip to legacy dict shape exactly.
        self.assertEqual(
            StructuredDataGenerator.SHIPPING_OPTIONS,
            shipping_tiers_as_dicts(),
        )

        # Spot-check the wired constants land in the generated schema body.
        details = StructuredDataGenerator._get_weight_based_shipping_details()
        self.assertTrue(details, msg="weight-based shipping details must not be empty")
        self.assertEqual(details[0]["shippingRate"]["currency"], CURRENCY)
        self.assertEqual(
            details[0]["shippingDestination"]["addressCountry"],
            APPLICABLE_COUNTRY,
        )
        self.assertEqual(RETURN_POLICY["days"], 14)


class HeaderCtaSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_header_primary_cta_points_to_custom_print(self):
        response = self.client.get(reverse("home"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"class='btn btn-sm btn-gradient-suggest me-3' href='{reverse('custom_print')}'",
            html=False,
        )
        self.assertNotContains(
            response,
            f"class='btn btn-sm btn-gradient-suggest me-3' href='{reverse('add_print')}'",
            html=False,
        )


class AddPrintSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_add_print_page_is_noindex_follow(self):
        response = self.client.get(reverse("add_print"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'content="noindex, follow"', html=False)


class PublicUrlIndexationSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_pricelist_page_redirects_to_wholesale(self):
        response = self.client.get(reverse("pricelist_page"), secure=True, follow=False)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("wholesale_page"))

    def test_utility_pages_are_noindex_follow(self):
        cases = [reverse("wholesale_order_form")]

        for url in cases:
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'content="noindex, follow"', html=False)

    def test_test_analytics_requires_staff(self):
        """AN-015/W1-8 — /test-analytics/ авто-стреляет Purchase в боевой
        Pixel, поэтому анонимный доступ запрещён (redirect на admin login)."""
        response = self.client.get(reverse("test_analytics"), follow=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_private_user_flow_pages_are_noindex_nofollow(self):
        cases = [reverse("cart"), reverse("login"), reverse("register")]

        for url in cases:
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'content="noindex, nofollow"', html=False)

    def test_legacy_page_pagination_url_redirects_to_querystring(self):
        """SEO 2026-05-16 — /page/N/ legacy URL must 301 to /?page=N so
        Search Console can retire the dead pattern and external links
        keep their equity flowing into the canonical paginator URL."""
        response = self.client.get("/page/2/", secure=True, follow=False)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/?page=2")

    def test_legacy_page_pagination_first_page_redirects_to_homepage(self):
        """SEO 2026-05-16 — /page/1/ collapses to / (no ?page=1 needed)."""
        response = self.client.get("/page/1/", secure=True, follow=False)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/")

    def test_legacy_catalog_page_pagination_redirects_to_querystring(self):
        """SEO 2026-05-16 — /catalog/page/N/ legacy URL must 301 to
        /catalog/?page=N for the same reason as the homepage paginator."""
        response = self.client.get("/catalog/page/3/", secure=True, follow=False)

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/catalog/?page=3")


class CustomPrintSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )

    def test_custom_print_page_has_breadcrumbs_and_faq_layer(self):
        response = self.client.get(reverse("custom_print"), secure=True, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Breadcrumb"', html=False)
        self.assertContains(response, "Поширені питання про кастомний друк")
        self.assertContains(response, '"@type": "FAQPage"', html=False)


class WholesaleSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_wholesale_page_exposes_visible_faq_content(self):
        response = self.client.get(reverse("wholesale_page"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Поширені питання про оптові закупівлі")
        self.assertContains(response, "Який мінімальний обсяг оптового замовлення у TwoComms?")


class CooperationSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )

    def test_cooperation_page_has_breadcrumbs_meta_and_faq_layer(self):
        response = self.client.get(reverse("cooperation"), secure=True, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<meta property="og:title" content="Співпраця з TwoComms — дропшипінг, опт і бренд-партнерства">',
            html=False,
        )
        self.assertContains(response, 'aria-label="Breadcrumb"', html=False)
        self.assertContains(response, "Поширені питання про співпрацю")
        self.assertContains(response, '"@type": "FAQPage"', html=False)


class RobotsTxtAiSearchRegressionTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )

    def test_robots_txt_allows_ai_search_and_training_bots_for_maximum_discoverability(self):
        response = self.client.get(reverse("robots_txt"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        body = response.content.decode("utf-8")

        self.assertIn("User-agent: OAI-SearchBot\nAllow: /", body)
        self.assertIn("User-agent: ChatGPT-User\nAllow: /", body)
        self.assertIn("User-agent: Claude-SearchBot\nAllow: /", body)
        self.assertIn("User-agent: Claude-User\nAllow: /", body)
        self.assertIn("User-agent: PerplexityBot\nAllow: /", body)
        self.assertIn("User-agent: Perplexity-User\nAllow: /", body)
        self.assertIn("User-agent: Google-Extended\nAllow: /", body)
        self.assertIn("User-agent: GPTBot\nAllow: /", body)
        self.assertIn("User-agent: CCBot\nAllow: /", body)
        self.assertIn("User-agent: ClaudeBot\nAllow: /", body)
        self.assertIn("User-agent: anthropic-ai\nAllow: /", body)
        # Phase 6: Googlebot, bingbot, Storebot-Google etc follow
        # ``User-agent: *`` per spec, so we no longer emit duplicate
        # explicit blocks for them. Keep AdsBot-* explicit because
        # Google's ads crawlers do NOT obey ``*``.
        self.assertIn("User-agent: AdsBot-Google\nAllow: /", body)
        self.assertIn("User-agent: AdsBot-Google-Mobile\nAllow: /", body)
        self.assertNotIn("Disallow: /search/", body)
        self.assertIn("Sitemap: https://twocomms.shop/sitemap.xml", body)

    def test_ai_specific_robots_groups_keep_internal_paths_disallowed(self):
        response = self.client.get(reverse("robots_txt"), secure=True)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        ai_group = body.split(
            "# AI answer engines: explicit opt-in for citations and model discovery.",
            1,
        )[1]

        # SEO 2026-05-16 — /cart/, /accounts/ removed from Disallow so
        # Googlebot can fetch the page, see the meta noindex, and exclude
        # the URL cleanly. The Disallow’d set now lists only routes that
        # have NO public HTML body to noindex (admin, api, internal),
        # which is the Google-recommended pattern.
        for path in ("/admin/", "/admin-panel/", "/orders/", "/checkout/", "/api/"):
            with self.subTest(path=path):
                self.assertIn(f"Disallow: {path}", ai_group)

    def test_robots_txt_blocks_utm_and_ad_click_query_noise(self):
        """Phase 6: UTM / gclid / fbclid query variants waste crawl
        budget and dilute canonicals. They must be disallowed globally.
        """
        response = self.client.get(reverse("robots_txt"), secure=True)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")

        for pattern in ("/*?utm_*", "/*?gclid=", "/*?fbclid=", "/*?sort="):
            with self.subTest(pattern=pattern):
                self.assertIn(f"Disallow: {pattern}", body)

    def test_search_page_is_crawlable_so_noindex_can_be_seen(self):
        robots_response = self.client.get(reverse("robots_txt"), secure=True)
        search_response = self.client.get(reverse("search"), {"q": "худі"}, secure=True)

        self.assertEqual(search_response.status_code, 200)
        self.assertContains(search_response, 'content="noindex, follow"', html=False)
        self.assertContains(search_response, f'href="https://twocomms.shop{reverse("catalog")}"', html=False)
        self.assertNotContains(search_response, '"@type": "CollectionPage"', html=False)
        self.assertNotIn("Disallow: /search/", robots_response.content.decode("utf-8"))


class LlmsTxtSeoRegressionTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )

    def test_llms_txt_exposes_primary_brand_and_commerce_routes(self):
        response = self.client.get(reverse("llms_txt"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertNotIn("twc_vid", response.cookies)

        body = response.content.decode("utf-8")
        self.assertIn("# TwoComms", body)
        self.assertIn("- [Custom print](https://twocomms.shop/custom-print/):", body)
        self.assertIn("- [Wholesale](https://twocomms.shop/wholesale/):", body)
        self.assertIn("- [Catalog](https://twocomms.shop/catalog/):", body)
        self.assertIn("- [XML sitemap](https://twocomms.shop/sitemap.xml):", body)
        self.assertIn("## Optional", body)

    def test_llms_txt_uses_configured_https_origin_on_http_request(self):
        response = self.client.get(
            reverse("llms_txt"),
            secure=False,
            SERVER_PORT="80",
            **{"wsgi.url_scheme": "http"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("https://twocomms.shop/catalog/", body)
        self.assertNotIn("http://twocomms.shop", body)

    def test_well_known_llms_txt_alias_resolves(self):
        primary = self.client.get(reverse("llms_txt"), secure=True)
        response = self.client.get("/.well-known/llms.txt", secure=True)

        self.assertEqual(primary.status_code, 200)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response.content.decode("utf-8"), primary.content.decode("utf-8"))

    def test_llms_txt_excludes_utility_and_internal_routes(self):
        response = self.client.get(reverse("llms_txt"), secure=True)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")

        for disallowed_path in (
            "/search/",
            "/cart/",
            "/checkout/",
            "/admin/",
            "/api/",
            "/test-analytics/",
            "/wholesale/order-form/",
        ):
            with self.subTest(disallowed_path=disallowed_path):
                self.assertNotIn(disallowed_path, body)


class AiOptInBehaviorTests(TestCase):
    """Phase 1 — AI content is consulted ONLY when ai_generation_enabled=True.

    These regression tests guard the strict opt-in behavior: meta description
    generation, the description-source cascade and keyword merging must ignore
    ai_* fields when the flag is off.
    """

    def setUp(self):
        self.category = Category.objects.create(
            name="AI Opt-In Test",
            slug="ai-opt-in-test",
            is_active=True,
        )
        self.product = Product.objects.create(
            title="AI Opt-In Tee",
            slug="ai-opt-in-tee",
            category=self.category,
            price=500,
            short_description="Manual short description.",
            ai_description="AI-generated description that must not leak.",
            ai_keywords="ai-keyword-1, ai-keyword-2",
            status="published",
        )

    def test_ai_description_ignored_when_flag_disabled(self):
        self.product.ai_generation_enabled = False
        self.product.save(update_fields=["ai_generation_enabled"])

        meta_desc = SEOKeywordGenerator.generate_meta_description(self.product)

        self.assertNotIn("AI-generated description", meta_desc)
        self.assertIn("Manual short description.", meta_desc)

    def test_ai_description_used_as_seo_fallback_when_flag_enabled(self):
        self.product.ai_generation_enabled = True
        self.product.short_description = ""
        self.product.save(update_fields=["ai_generation_enabled", "short_description"])

        meta_desc = SEOKeywordGenerator.generate_meta_description(self.product)

        self.assertIn("AI-generated description", meta_desc)

    def test_seo_description_always_wins_over_ai(self):
        self.product.ai_generation_enabled = True
        self.product.seo_description = "Manual SEO description rules."
        self.product.save(update_fields=["ai_generation_enabled", "seo_description"])

        meta_desc = SEOKeywordGenerator.generate_meta_description(self.product)

        self.assertEqual(meta_desc, "Manual SEO description rules.")
        self.assertNotIn("AI-generated", meta_desc)

    def test_description_source_cascade_skips_ai_when_flag_disabled(self):
        self.product.ai_generation_enabled = False
        self.product.short_description = ""
        self.product.save(update_fields=["ai_generation_enabled", "short_description"])

        # With AI off and no short/seo_description, must NOT pick ai_description.
        source = _pick_product_description_source(self.product)
        self.assertNotIn("AI-generated description", source)

    def test_ai_keywords_excluded_from_keyword_set_when_disabled(self):
        self.product.ai_generation_enabled = False
        self.product.save(update_fields=["ai_generation_enabled"])

        keywords = SEOKeywordGenerator.generate_product_keywords(self.product)
        flat = " ".join(keywords).lower()

        self.assertNotIn("ai-keyword-1", flat)
        self.assertNotIn("ai-keyword-2", flat)

    def test_ai_keywords_included_when_flag_enabled(self):
        self.product.ai_generation_enabled = True
        self.product.save(update_fields=["ai_generation_enabled"])

        keywords = SEOKeywordGenerator.generate_product_keywords(self.product)
        self.assertIn("ai-keyword-1", keywords)
        self.assertIn("ai-keyword-2", keywords)
