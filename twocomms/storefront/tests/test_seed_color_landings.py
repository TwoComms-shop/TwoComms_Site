from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, CategoryColorLanding, Product


class SeedColorLandingsGrammarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.black = Color.objects.create(name="Чорний", primary_hex="#000000")
        cls.coyote = Color.objects.create(name="Кайот", primary_hex="#A98463")
        for index, (slug, name) in enumerate(
            (
                ("tshirts", "Футболки"),
                ("hoodie", "Худі"),
                ("long-sleeve", "Лонгсліви"),
            ),
            start=1,
        ):
            category = Category.objects.create(
                name=name,
                slug=slug,
                order=index,
                is_active=True,
            )
            product = Product.objects.create(
                title=f"Product {index}",
                slug=f"product-{index}",
                category=category,
                price=600,
                status="published",
            )
            ProductColorVariant.objects.create(
                product=product,
                color=cls.black,
                is_default=True,
            )
        tshirts = Category.objects.get(slug="tshirts")
        coyote_product = Product.objects.create(
            title="Coyote tee",
            slug="coyote-tee",
            category=tshirts,
            price=600,
            status="published",
        )
        ProductColorVariant.objects.create(
            product=coyote_product,
            color=cls.coyote,
            is_default=True,
        )

    def setUp(self):
        for target in (
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            "storefront.signals.enqueue_indexnow_urls",
        ):
            patcher = patch(target)
            self.addCleanup(patcher.stop)
            patcher.start()

    def test_generated_titles_and_h1_use_natural_ukrainian_inflection(self):
        call_command(
            "seed_color_landings",
            "--apply",
            "--min-products=1",
            stdout=StringIO(),
        )

        expected = {
            ("tshirts", "black"): (
                "Купити футболку чорного кольору з принтом — TwoComms",
                "Чорні футболки TwoComms — стрітвеар з Харкова",
            ),
            ("hoodie", "black"): (
                "Купити худі чорного кольору з принтом — TwoComms",
                "Чорні худі TwoComms — стрітвеар з Харкова",
            ),
            ("long-sleeve", "black"): (
                "Купити лонгслів чорного кольору з принтом — TwoComms",
                "Чорні лонгсліви TwoComms — стрітвеар з Харкова",
            ),
            ("tshirts", "coyote"): (
                "Купити футболку кольору «Кайот» — TwoComms",
                "Футболки кольору «Кайот» — TwoComms",
            ),
        }
        for key, (title, h1) in expected.items():
            with self.subTest(key=key):
                landing = CategoryColorLanding.objects.get(
                    category__slug=key[0],
                    color_slug=key[1],
                )
                self.assertEqual(landing.seo_title, title)
                self.assertEqual(landing.seo_h1, h1)
                self.assertLessEqual(len(landing.seo_title), 60)

        rendered_copy = " ".join(
            f"{landing.seo_title} {landing.seo_h1} "
            f"{landing.seo_description} {landing.editorial_html} "
            f"{' '.join(item['question'] for item in landing.faq_items)}"
            for landing in CategoryColorLanding.objects.all()
        )
        for broken_phrase in (
            "стрітвір",
            "Купити футболка",
            "Чорна худі",
            "Чорна лонгслів",
            "у чорний футболка",
        ):
            with self.subTest(broken_phrase=broken_phrase):
                self.assertNotIn(broken_phrase, rendered_copy)
