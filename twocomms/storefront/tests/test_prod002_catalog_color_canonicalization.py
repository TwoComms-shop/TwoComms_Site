"""PROD-002 regressions for canonical catalog colour-filter URLs."""

from unittest.mock import patch

from django.core.cache import cache, caches
from django.test import RequestFactory, TestCase
from django.urls import reverse

from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product
from storefront.services.color_filter import parse_color_filter
from storefront.views.utils import _build_anon_cache_key


class ColorFilterCanonicalServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_parser_keeps_only_allowed_slugs_in_stable_order(self):
        request = self.factory.get(
            "/catalog/?color=coyote,unknown&color=black,coyote"
        )

        self.assertEqual(
            parse_color_filter(request, allowed_slugs=["coyote", "black"]),
            ["black", "coyote"],
        )

    def test_cache_key_treats_permutations_and_duplicates_as_one_identity(self):
        canonical = self.factory.get("/catalog/?color=black,coyote")
        noisy = self.factory.get(
            "/catalog/?color=coyote&color=black,coyote"
        )

        def catalog_view(request):
            return None

        self.assertEqual(
            _build_anon_cache_key(canonical, catalog_view),
            _build_anon_cache_key(noisy, catalog_view),
        )

    def test_cache_key_treats_query_parameter_order_as_one_identity(self):
        first = self.factory.get(
            "/catalog/?utm_source=instagram&color=black,coyote"
        )
        second = self.factory.get(
            "/catalog/?color=coyote,black&utm_source=instagram"
        )

        def catalog_view(request):
            return None

        self.assertEqual(
            _build_anon_cache_key(first, catalog_view),
            _build_anon_cache_key(second, catalog_view),
        )


class CatalogColorCanonicalRedirectTests(TestCase):
    def setUp(self):
        cache.clear()
        caches["fragments"].clear()
        merchant_patcher = patch(
            "storefront.signals.generate_google_merchant_feed_task.apply_async"
        )
        indexnow_patcher = patch("storefront.signals.enqueue_indexnow_urls")
        self.addCleanup(merchant_patcher.stop)
        self.addCleanup(indexnow_patcher.stop)
        merchant_patcher.start()
        indexnow_patcher.start()

        category = Category.objects.create(
            name="Canonical colours",
            slug="canonical-colours",
            is_active=True,
        )
        black = Color.objects.create(name="black", primary_hex="#000000")
        coyote = Color.objects.create(name="coyote", primary_hex="#7A5A3A")
        product = Product.objects.create(
            title="Canonical tee",
            slug="canonical-tee",
            category=category,
            price=500,
            status="published",
        )
        ProductColorVariant.objects.create(
            product=product, color=black, is_default=True
        )
        ProductColorVariant.objects.create(
            product=product, color=coyote, order=1
        )

    def test_reversed_duplicate_and_unknown_slugs_redirect_to_canonical_url(self):
        response = self.client.get(
            reverse("catalog"),
            {
                "color": "coyote,black,coyote,unknown",
                "page": "7",
                "utm_source": "instagram",
            },
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            "/catalog/?utm_source=instagram&color=black%2Ccoyote",
        )

    def test_unknown_only_redirects_to_unfiltered_catalog(self):
        response = self.client.get(reverse("catalog") + "?color=unknown")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/catalog/")

    def test_canonical_color_still_redirects_to_canonical_query_order(self):
        response = self.client.get(
            reverse("catalog")
            + "?color=black%2Ccoyote&utm_source=instagram"
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            "/catalog/?utm_source=instagram&color=black%2Ccoyote",
        )

    def test_pagination_is_preserved_when_color_identity_did_not_change(self):
        response = self.client.get(
            reverse("catalog") + "?color=black&page=2"
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            "/catalog/?page=2&color=black",
        )

    def test_canonical_color_url_renders_without_redirect(self):
        response = self.client.get(
            reverse("catalog") + "?color=black%2Ccoyote"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["selected_color_slugs"], ["black", "coyote"]
        )

    def test_chip_addition_uses_stable_slug_order(self):
        response = self.client.get(reverse("catalog") + "?color=coyote")

        chips = {chip["slug"]: chip for chip in response.context["available_colors"]}
        self.assertIn("color=black%2Ccoyote", chips["black"]["url"])

    def test_noncanonical_request_is_redirected_before_page_cache_write(self):
        with patch("storefront.views.utils.cache.set") as cache_set:
            response = self.client.get(
                reverse("catalog") + "?color=coyote,black,unknown"
            )

        self.assertEqual(response.status_code, 301)
        page_cache_writes = [
            call
            for call in cache_set.call_args_list
            if call.args and str(call.args[0]).startswith("anon-page:")
        ]
        self.assertEqual(page_cache_writes, [])

    def test_category_catalog_uses_the_same_canonical_filter_contract(self):
        response = self.client.get(
            reverse(
                "catalog_by_cat",
                kwargs={"cat_slug": "canonical-colours"},
            )
            + "?color=coyote,black"
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            "/catalog/canonical-colours/?color=black%2Ccoyote",
        )

    def test_search_uses_the_same_canonical_filter_contract(self):
        response = self.client.get(
            reverse("search") + "?q=tee&color=coyote,black&page=3"
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"],
            "/search/?q=tee&color=black%2Ccoyote",
        )

    def test_multi_select_is_nofollow_and_filter_chips_do_not_publish_graph(self):
        response = self.client.get(
            reverse("catalog") + "?color=black%2Ccoyote"
        )

        self.assertContains(response, 'content="noindex, nofollow"')
        self.assertContains(response, 'data-color-slug="black"')
        self.assertContains(response, 'rel="nofollow"')

    def test_gptbot_is_blocked_from_catalog_color_query_variants(self):
        response = self.client.get(reverse("robots_txt"))
        robots = response.content.decode("utf-8")
        gptbot_block = robots.split("User-agent: GPTBot", 1)[1].split(
            "User-agent:", 1
        )[0]

        self.assertIn("Disallow: /*?color=", gptbot_block)
        self.assertIn("Disallow: /*&color=", gptbot_block)
