from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from storefront.models import Catalog, CatalogOption, CatalogOptionValue, Category, Product, ProductStatus, SizeGrid


class EnsureDefaultSizeCatalogsCommandTests(TestCase):
    def setUp(self):
        self.feed_task_patcher = patch(
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            return_value=None,
        )
        self.feed_task_patcher.start()
        self.addCleanup(self.feed_task_patcher.stop)

        self.hoodie_category = Category.objects.create(name="Худі", slug="hoodie")
        self.tshirt_category = Category.objects.create(name="Футболки", slug="tshirts")
        self.longsleeve_category = Category.objects.create(name="Лонгсліви", slug="long-sleeve")

        self.hoodie_product = Product.objects.create(
            title="Classic Hoodie",
            slug="classic-hoodie-seed",
            category=self.hoodie_category,
            price=1800,
            status=ProductStatus.PUBLISHED,
        )
        self.tshirt_product = Product.objects.create(
            title="Classic Tee",
            slug="classic-tee-seed",
            category=self.tshirt_category,
            price=1200,
            status=ProductStatus.PUBLISHED,
        )
        self.longsleeve_product = Product.objects.create(
            title="Classic Longsleeve",
            slug="classic-longsleeve-seed",
            category=self.longsleeve_category,
            price=1300,
            status=ProductStatus.PUBLISHED,
        )

    def test_command_creates_catalogs_and_assigns_products_idempotently(self):
        call_command("ensure_default_size_catalogs")
        call_command("ensure_default_size_catalogs")

        hoodie_catalog = Catalog.objects.get(slug="hoodie-default")
        tshirt_catalog = Catalog.objects.get(slug="basic-tshirts")

        self.hoodie_product.refresh_from_db()
        self.tshirt_product.refresh_from_db()
        self.longsleeve_product.refresh_from_db()

        self.assertEqual(self.hoodie_product.catalog_id, hoodie_catalog.id)
        self.assertEqual(self.tshirt_product.catalog_id, tshirt_catalog.id)
        self.assertIsNone(self.longsleeve_product.catalog_id)

        hoodie_option = CatalogOption.objects.get(catalog=hoodie_catalog, option_type=CatalogOption.OptionType.SIZE)
        tshirt_option = CatalogOption.objects.get(catalog=tshirt_catalog, option_type=CatalogOption.OptionType.SIZE)

        self.assertEqual(
            list(hoodie_option.values.order_by("order").values_list("value", flat=True)),
            ["XS", "S", "M", "L", "XL", "XXL"],
        )
        self.assertEqual(
            list(tshirt_option.values.order_by("order").values_list("display_name", flat=True)),
            ["S", "M", "L", "XL", "2XL"],
        )

        hoodie_grid = SizeGrid.objects.get(catalog=hoodie_catalog, name="Hoodie size guide")
        tshirt_grid = SizeGrid.objects.get(catalog=tshirt_catalog, name="Basic tee size guide")

        self.assertEqual(hoodie_grid.guide_data.get("profile_key"), "hoodie")
        self.assertFalse(hoodie_grid.guide_data.get("rows"))
        self.assertIsInstance(hoodie_grid.guide_data.get("title"), str)
        self.assertEqual(tshirt_grid.guide_data.get("profile_key"), "basic_tshirt")
        self.assertTrue(tshirt_grid.guide_data.get("rows"))

        self.assertEqual(Catalog.objects.filter(slug="hoodie-default").count(), 1)
        self.assertEqual(Catalog.objects.filter(slug="basic-tshirts").count(), 1)
        self.assertEqual(CatalogOptionValue.objects.filter(option=hoodie_option).count(), 6)
        self.assertEqual(CatalogOptionValue.objects.filter(option=tshirt_option).count(), 5)
