"""Sitemap & IndexNow integration tests for colour-category landings."""

from __future__ import annotations

from xml.etree import ElementTree as ET
from unittest.mock import patch

from django.test import TestCase, override_settings

from productcolors.models import Color
from storefront.models import Category, CategoryColorLanding, Product
from productcolors.models import ProductColorVariant


_LONG = (
    "Чорна футболка TwoComms — це базовий шар стрітвір-гардеробу. "
    "Ми друкуємо принти DTF-методом на щільній бавовні 200 г/м² і "
    "збираємо комплекти для тих, хто звик носити одяг довго. "
) * 4


@override_settings(SITEMAP_BASE_URL="https://twocomms.shop")
class CategoryColorLandingSitemapTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            name="Футболки", slug="tshirts", order=10, is_active=True,
        )
        cls.black = Color.objects.create(name="Чорний", primary_hex="#000000")
        cls.tee = Product.objects.create(
            title="Black tee",
            slug="black-tee",
            category=cls.category,
            price=600,
            status="published",
        )
        ProductColorVariant.objects.create(
            product=cls.tee, color=cls.black, is_default=True, order=0
        )

    def setUp(self):
        super().setUp()
        merchant_patcher = patch(
            "storefront.signals.generate_google_merchant_feed_task.apply_async"
        )
        indexnow_patcher = patch("storefront.signals.enqueue_indexnow_urls")
        self.addCleanup(merchant_patcher.stop)
        self.addCleanup(indexnow_patcher.stop)
        merchant_patcher.start()
        self.indexnow_mock = indexnow_patcher.start()

    def test_sitemap_index_references_color_categories_section(self):
        # Create at least one published landing so the section has a lastmod.
        CategoryColorLanding.objects.create(
            category=self.category,
            color=self.black,
            seo_title="Чорні футболки TwoComms",
            seo_description="Чорні футболки TwoComms.",
            editorial_html=_LONG,
            is_published=True,
        )
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("/sitemap-color-categories.xml", body)

    def test_sitemap_section_lists_only_published_landings(self):
        # Published.
        CategoryColorLanding.objects.create(
            category=self.category,
            color=self.black,
            seo_title="Чорні футболки",
            seo_description="—",
            editorial_html=_LONG,
            is_published=True,
        )
        # Draft — must NOT appear.
        white = Color.objects.create(name="Білий", primary_hex="#FFFFFF")
        CategoryColorLanding.objects.create(
            category=self.category,
            color=white,
            seo_title="Білі футболки (draft)",
            seo_description="—",
            editorial_html="short draft",
            is_published=False,
        )
        response = self.client.get("/sitemap-color-categories.xml")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("/catalog/tshirts/black/", body)
        self.assertNotIn("/catalog/tshirts/white/", body)

    def test_sitemap_emits_unique_locale_urls_and_reciprocal_alternates(self):
        CategoryColorLanding.objects.create(
            category=self.category,
            color=self.black,
            seo_title="Чорні футболки",
            seo_description="Чорні футболки TwoComms.",
            editorial_html=_LONG,
            is_published=True,
        )

        response = self.client.get("/sitemap-color-categories.xml")

        self.assertEqual(response.status_code, 200)
        root = ET.fromstring(response.content)
        sitemap_ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
        xhtml_ns = "http://www.w3.org/1999/xhtml"
        urls = root.findall(f"{{{sitemap_ns}}}url")
        locations = {
            url.findtext(f"{{{sitemap_ns}}}loc")
            for url in urls
        }
        expected_locations = {
            "https://twocomms.shop/catalog/tshirts/black/",
            "https://twocomms.shop/ru/catalog/tshirts/black/",
            "https://twocomms.shop/en/catalog/tshirts/black/",
        }
        self.assertEqual(len(urls), 3)
        self.assertEqual(locations, expected_locations)

        for url in urls:
            alternates = {
                (link.attrib["hreflang"], link.attrib["href"])
                for link in url.findall(f"{{{xhtml_ns}}}link")
            }
            self.assertEqual(
                alternates,
                {
                    ("uk", "https://twocomms.shop/catalog/tshirts/black/"),
                    ("ru", "https://twocomms.shop/ru/catalog/tshirts/black/"),
                    ("en", "https://twocomms.shop/en/catalog/tshirts/black/"),
                    ("x-default", "https://twocomms.shop/catalog/tshirts/black/"),
                },
            )

    def test_publishing_landing_pings_indexnow(self):
        # Create as draft — should NOT ping IndexNow.
        with self.captureOnCommitCallbacks(execute=True):
            landing = CategoryColorLanding.objects.create(
                category=self.category,
                color=self.black,
                seo_title="Чорні футболки",
                seo_description="—",
                editorial_html=_LONG,
                is_published=False,
            )
        self.indexnow_mock.assert_not_called()

        # Flip to published — IndexNow should fire.
        with self.captureOnCommitCallbacks(execute=True):
            landing.is_published = True
            landing.save()
        self.indexnow_mock.assert_called()
        called_urls = self.indexnow_mock.call_args[0][0]
        self.assertIn("/catalog/tshirts/black/", called_urls)
