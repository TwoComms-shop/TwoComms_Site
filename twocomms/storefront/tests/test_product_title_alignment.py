import importlib
import re

from django.test import SimpleTestCase
from django.utils.translation import override

from storefront.models import Category, Product
from storefront.seo_utils import SEOKeywordGenerator


class ProductTitleAlignmentTests(SimpleTestCase):
    def test_stale_quoted_seo_name_falls_back_to_canonical_product_title(self):
        category = Category(name_uk="Футболки", slug="tshirts")
        product = Product(
            title_uk="Футболка «Серце Та Грощі»",
            seo_title_uk=(
                "Футболка «death grabs ass» — купити футболку TwoComms"
            ),
            category=category,
        )

        with override("uk"):
            title = SEOKeywordGenerator.generate_meta_title(product)

        self.assertEqual(
            title,
            "Футболка «Серце Та Грощі» (Футболки) - TwoComms",
        )

    def test_matching_quoted_seo_name_keeps_editor_copy(self):
        stored = "Футболка «Серце Та Грощі» — купити в TwoComms"
        product = Product(
            title_uk="Футболка «Серце Та Грощі»",
            seo_title_uk=stored,
        )

        with override("uk"):
            title = SEOKeywordGenerator.generate_meta_title(product)

        self.assertEqual(title, stored)

    def test_data_repair_covers_all_audited_products_with_matching_names(self):
        migration = importlib.import_module(
            "storefront.migrations.0082_align_product_seo_titles_with_h1"
        )
        self.assertEqual(len(migration.REPAIRS), 13)

        for slug, payload in migration.REPAIRS.items():
            with self.subTest(slug=slug):
                repaired_seo = migration._seo_new(payload)
                title_name = re.search(r"«([^»]+)»", payload["title"]).group(1)
                seo_name = re.search(r"«([^»]+)»", repaired_seo).group(1)
                self.assertEqual(title_name, seo_name)
                self.assertLessEqual(len(repaired_seo), 70)
                self.assertNotIn(" с ", payload["title"].casefold())
