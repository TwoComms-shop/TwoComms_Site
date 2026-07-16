from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache, caches
from django.test import TestCase
from django.urls import reverse

from storefront.models import Category, Product


class _PdpShellParser(HTMLParser):
    TARGET_CLASSES = {
        "reviews": "tc-reviews",
        "recommendations": "tc-related-panel",
        "recent": "tc-recent-panel",
        "general_seo": "pdp-seo-block",
        "landing_seo": "product-seo-landing",
    }

    def __init__(self):
        super().__init__()
        self._stack = []
        self._shell_depth = 0
        self.inside_shell = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        opens_shell = "tc-pdp-shell" in classes
        self._stack.append((tag, opens_shell))
        if opens_shell:
            self._shell_depth += 1
        for marker, class_name in self.TARGET_CLASSES.items():
            if class_name in classes:
                self.inside_shell[marker] = self._shell_depth > 0

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        while self._stack:
            open_tag, opens_shell = self._stack.pop()
            if opens_shell:
                self._shell_depth -= 1
            if open_tag == tag:
                break


class ProductDetailContentOrderTests(TestCase):
    def setUp(self):
        cache.clear()
        caches["fragments"].clear()
        for target in (
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            "storefront.signals.enqueue_indexnow_urls",
        ):
            patcher = patch(target)
            self.addCleanup(patcher.stop)
            patcher.start()

        category = Category.objects.create(
            name="Футболки",
            slug="pdp-content-order",
            is_active=True,
        )
        self.product = Product.objects.create(
            title="Футболка для перевірки порядку",
            slug="pdp-content-order-product",
            category=category,
            price=1000,
            description="Основний опис товару для перевірки PDP.",
            status="published",
        )
        self.recommendation = Product.objects.create(
            title="Рекомендована футболка",
            slug="pdp-content-order-recommendation",
            category=category,
            price=900,
            status="published",
        )

    def test_reviews_recommendations_and_recently_viewed_precede_both_seo_blocks(self):
        with patch(
            "storefront.views.product.ProductRecommendationEngine.get_recommendations",
            return_value=[self.recommendation],
        ):
            response = self.client.get(
                reverse("product", kwargs={"slug": self.product.slug})
            )
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        markers = (
            'id="product-reviews"',
            'class="tc-related-panel"',
            'class="tc-recent-panel"',
            'class="pdp-seo-block"',
            'class="product-seo-landing"',
        )
        for marker in markers:
            self.assertIn(marker, html)
        positions = [html.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

        parser = _PdpShellParser()
        parser.feed(html)
        self.assertEqual(
            parser.inside_shell,
            {
                "reviews": True,
                "recommendations": True,
                "recent": True,
                "general_seo": True,
                "landing_seo": True,
            },
        )

    def test_general_seo_block_uses_full_pdp_shell_width(self):
        css = Path(
            settings.BASE_DIR,
            "twocomms_django_theme/static/css/product-detail.css",
        ).read_text(encoding="utf-8")
        rule = css.split(".pdp-seo-block {", 1)[1].split("}", 1)[0]

        self.assertIn("max-width: 100%;", rule)
        self.assertNotIn("max-width: 1080px;", rule)
