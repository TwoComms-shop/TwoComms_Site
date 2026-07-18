"""SEO v1.1 Phase 2 (2026-05-15) — multilingual indexing reopened.

Verifies that:
    * /, /catalog/, /product/<slug>/ render ``index, follow`` on UK, RU
      and EN locales.
    * Each page emits reciprocal ``hreflang`` entries for ``uk-UA``,
      ``ru-UA``, ``en-UA`` and ``x-default``.
    * Facet pages (search, color filter) stay ``noindex, follow`` on
      every locale — those are duplicates by definition.
    * Sitemap classes carry ``i18n = True`` + ``alternates = True``
      flags so the rendered XML lists ``<xhtml:link>`` siblings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from django.core.cache import cache, caches
from django.test import TestCase
from django.urls import reverse

from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product
from storefront import sitemaps


class MultilingualRobotsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            name="Футболки", slug="tshirts", order=10, is_active=True,
        )
        cls.tee = Product.objects.create(
            title="Black tee",
            slug="black-tee",
            category=cls.category,
            price=600,
            status="published",
        )
        cls.black = Color.objects.create(name="Чорний", primary_hex="#000000")
        ProductColorVariant.objects.create(
            product=cls.tee, color=cls.black, is_default=True, order=0
        )

    def setUp(self):
        super().setUp()
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

    # ---- Robots ----

    def _assert_indexable(self, response):
        self.assertContains(response, 'name="robots"')
        body = response.content.decode("utf-8")
        # Must NOT carry a noindex token.
        self.assertNotIn("noindex", body.split('name="description"')[0],
                         msg="page should be indexable but emits noindex")

    def test_homepage_uk_is_indexable(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self._assert_indexable(response)

    def test_homepage_ru_is_indexable(self):
        response = self.client.get("/ru/")
        self.assertEqual(response.status_code, 200)
        self._assert_indexable(response)

    def test_homepage_en_is_indexable(self):
        response = self.client.get("/en/")
        self.assertEqual(response.status_code, 200)
        self._assert_indexable(response)

    def test_catalog_ru_is_indexable(self):
        response = self.client.get("/ru/catalog/")
        self.assertEqual(response.status_code, 200)
        self._assert_indexable(response)

    def test_catalog_en_is_indexable(self):
        response = self.client.get("/en/catalog/")
        self.assertEqual(response.status_code, 200)
        self._assert_indexable(response)

    def test_product_detail_ru_is_indexable(self):
        response = self.client.get("/ru/product/black-tee/")
        self.assertEqual(response.status_code, 200)
        self._assert_indexable(response)

    def test_product_detail_en_is_indexable(self):
        response = self.client.get("/en/product/black-tee/")
        self.assertEqual(response.status_code, 200)
        self._assert_indexable(response)

    # ---- Facet pages stay noindex on every locale ----

    def test_color_filter_stays_noindex_on_ru(self):
        response = self.client.get("/ru/catalog/?color=black")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "noindex, follow")

    def test_search_stays_noindex_on_ru(self):
        response = self.client.get("/ru/search/?q=black")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "noindex, follow")

    def test_pdp_color_query_stays_noindex_on_ru(self):
        response = self.client.get("/ru/product/black-tee/?color=black")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "noindex, follow")

    # ---- hreflang reciprocity ----

    def test_homepage_emits_all_four_hreflang_entries(self):
        response = self.client.get("/")
        body = response.content.decode("utf-8")
        self.assertIn('hreflang="uk-UA"', body)
        self.assertIn('hreflang="ru-UA"', body)
        self.assertIn('hreflang="en-UA"', body)
        self.assertIn('hreflang="x-default"', body)

    def test_homepage_ru_emits_all_four_hreflang_entries(self):
        response = self.client.get("/ru/")
        body = response.content.decode("utf-8")
        self.assertIn('hreflang="uk-UA"', body)
        self.assertIn('hreflang="ru-UA"', body)
        self.assertIn('hreflang="en-UA"', body)
        self.assertIn('hreflang="x-default"', body)

    def test_hreflang_links_point_to_correct_paths(self):
        response = self.client.get("/")
        body = response.content.decode("utf-8")
        # UK → "/", RU → "/ru/", EN → "/en/".
        self.assertIn('hreflang="uk-UA" href="https://twocomms.shop/"', body)
        self.assertIn('hreflang="ru-UA" href="https://twocomms.shop/ru/"', body)
        self.assertIn('hreflang="en-UA" href="https://twocomms.shop/en/"', body)

    def test_homepage_ru_meta_is_translated(self):
        response = self.client.get("/ru/")

        self.assertContains(
            response,
            "<title>TwoComms — стрит и милитари одежда из Харькова: футболки, худи</title>",
            html=False,
        )
        self.assertContains(
            response,
            'content="TwoComms — харьковский бренд стритвир и милитари одежды: футболки, худи, лонгсливы, кастомная DTF-печать, доставка Новой Почтой по всей Украине."',
            html=False,
        )

    def test_homepage_en_meta_is_translated(self):
        response = self.client.get("/en/")

        self.assertContains(
            response,
            "<title>TwoComms — street and military apparel from Kharkiv: t-shirts, hoodies</title>",
            html=False,
        )
        self.assertContains(
            response,
            'content="TwoComms is a Kharkiv streetwear and military apparel brand: t-shirts, hoodies, longsleeves, custom DTF print and Nova Poshta delivery across Ukraine."',
            html=False,
        )


class SitemapI18nFlagsTests(TestCase):
    """Sanity checks on sitemap class flags — no XML parsing here."""

    def test_static_view_sitemap_carries_i18n_flags(self):
        self.assertTrue(getattr(sitemaps.StaticViewSitemap, "i18n", False))
        self.assertTrue(getattr(sitemaps.StaticViewSitemap, "alternates", False))
        self.assertTrue(getattr(sitemaps.StaticViewSitemap, "x_default", False))

    def test_product_sitemap_carries_i18n_flags(self):
        self.assertTrue(getattr(sitemaps.ProductSitemap, "i18n", False))
        self.assertTrue(getattr(sitemaps.ProductSitemap, "alternates", False))
        self.assertTrue(getattr(sitemaps.ProductSitemap, "x_default", False))

    def test_category_sitemap_carries_i18n_flags(self):
        self.assertTrue(getattr(sitemaps.CategorySitemap, "i18n", False))
        self.assertTrue(getattr(sitemaps.CategorySitemap, "alternates", False))
        self.assertTrue(getattr(sitemaps.CategorySitemap, "x_default", False))

    def test_color_landing_sitemap_carries_i18n_flags(self):
        self.assertTrue(getattr(sitemaps.CategoryColorLandingSitemap, "i18n", False))
        self.assertTrue(getattr(sitemaps.CategoryColorLandingSitemap, "alternates", False))
        self.assertTrue(getattr(sitemaps.CategoryColorLandingSitemap, "x_default", False))


class SitemapXmlAlternatesTests(TestCase):
    """End-to-end: render the actual sitemap XML and verify it lists
    reciprocal ``<xhtml:link>`` entries for each locale.
    """

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            name="Футболки", slug="tshirts", order=10, is_active=True,
        )
        cls.product = Product.objects.create(
            title="Black tee",
            slug="black-tee",
            category=cls.category,
            price=600,
            status="published",
        )

    def setUp(self):
        super().setUp()
        cache.clear()
        merchant_patcher = patch(
            "storefront.signals.generate_google_merchant_feed_task.apply_async"
        )
        indexnow_patcher = patch("storefront.signals.enqueue_indexnow_urls")
        self.addCleanup(merchant_patcher.stop)
        self.addCleanup(indexnow_patcher.stop)
        merchant_patcher.start()
        indexnow_patcher.start()

    def test_categories_sitemap_lists_locale_urls_and_alternates(self):
        response = self.client.get("/sitemap-categories.xml")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        # All three locale URLs present.
        self.assertIn("/catalog/tshirts/", body)
        self.assertIn("/ru/catalog/tshirts/", body)
        self.assertIn("/en/catalog/tshirts/", body)
        # xhtml:link alternates with hreflang attributes.
        self.assertIn('xhtml:link', body)
        self.assertIn('hreflang="uk"', body)
        self.assertIn('hreflang="ru"', body)
        self.assertIn('hreflang="en"', body)
        self.assertIn('hreflang="x-default"', body)

    def test_products_sitemap_lists_locale_urls_and_alternates(self):
        response = self.client.get("/sitemap-products.xml")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("/product/black-tee/", body)
        self.assertIn("/ru/product/black-tee/", body)
        self.assertIn("/en/product/black-tee/", body)
        self.assertIn('xhtml:link', body)

    def test_products_sitemap_uses_each_products_own_updated_at(self):
        second = Product.objects.create(
            title="White tee",
            slug="white-tee",
            category=self.category,
            price=700,
            status="published",
        )
        Product.objects.filter(pk=self.product.pk).update(
            updated_at=datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        )
        Product.objects.filter(pk=second.pk).update(
            updated_at=datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)
        )

        response = self.client.get("/sitemap-products.xml")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        # Django emits one URL row per locale. Each product's date must be
        # repeated only for its UK/RU/EN rows, not copied from another item.
        self.assertEqual(body.count("<lastmod>2026-01-02</lastmod>"), 3)
        self.assertEqual(body.count("<lastmod>2026-03-04</lastmod>"), 3)

    def test_static_sitemap_lists_locale_urls(self):
        response = self.client.get("/sitemap-static.xml")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("/ru/catalog/", body)
        self.assertIn("/en/catalog/", body)
        self.assertIn('xhtml:link', body)
