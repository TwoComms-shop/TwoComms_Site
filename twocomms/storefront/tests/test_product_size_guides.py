from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from storefront.models import (
    Catalog,
    CatalogOption,
    CatalogOptionValue,
    Category,
    Product,
    ProductStatus,
    SizeGrid,
)
from storefront.services.size_guides import (
    build_public_size_guide_blocks,
    resolve_product_size_context,
    resolve_product_size_guide,
)


class ProductSizeGuideResolverTests(TestCase):
    def setUp(self):
        self.feed_task_patcher = patch(
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            return_value=None,
        )
        self.feed_task_patcher.start()
        self.addCleanup(self.feed_task_patcher.stop)

        self.hoodie_category = Category.objects.create(name="Hoodies", slug="hoodie")
        self.tshirt_category = Category.objects.create(name="Футболки", slug="futbolki")
        self.hoodie_catalog = Catalog.objects.create(name="Hoodies", slug="hoodie")
        self.tshirt_catalog = Catalog.objects.create(name="Basic T-Shirts", slug="basic-tshirts")

        self.catalog_default_grid = SizeGrid.objects.create(
            catalog=self.hoodie_catalog,
            name="Hoodie default guide",
            description="Catalog default hoodie guide",
            is_active=True,
            order=10,
        )
        self.override_grid = SizeGrid.objects.create(
            catalog=self.tshirt_catalog,
            name="Basic t-shirt fit guide",
            description="Product-specific override guide",
            is_active=True,
            order=0,
        )

    def test_product_override_has_priority_over_catalog_default(self):
        product = Product.objects.create(
            title="Heavyweight hoodie",
            slug="heavyweight-hoodie",
            category=self.hoodie_category,
            catalog=self.hoodie_catalog,
            size_grid=self.override_grid,
            price=1800,
            description="Override should win.",
            status=ProductStatus.PUBLISHED,
        )

        guide = resolve_product_size_guide(product)

        self.assertEqual(guide["source"], "product_override")
        self.assertEqual(guide["guide_key"], "basic_tshirt")
        self.assertEqual(guide["size_grid"].pk, self.override_grid.pk)
        self.assertEqual([row["size"] for row in guide["rows"]], ["S", "M", "L", "XL", "XXL"])
        self.assertEqual(guide["rows"][-1]["display_size"], "2XL")

    def test_catalog_default_uses_first_active_grid_by_order(self):
        SizeGrid.objects.create(
            catalog=self.hoodie_catalog,
            name="Later hoodie guide",
            is_active=True,
            order=50,
        )
        product = Product.objects.create(
            title="Classic hoodie",
            slug="classic-hoodie",
            category=self.hoodie_category,
            catalog=self.hoodie_catalog,
            price=1700,
            description="Catalog fallback should resolve.",
            status=ProductStatus.PUBLISHED,
        )

        guide = resolve_product_size_guide(product)

        self.assertEqual(guide["source"], "catalog_default")
        self.assertEqual(guide["guide_key"], "hoodie")
        self.assertEqual(guide["size_grid"].pk, self.catalog_default_grid.pk)
        self.assertEqual(guide["rows"][0]["size"], "XS")

    def test_catalog_option_values_define_resolved_sizes_and_display_labels(self):
        option = CatalogOption.objects.create(
            catalog=self.tshirt_catalog,
            name="Розмір",
            option_type=CatalogOption.OptionType.SIZE,
            order=0,
        )
        values = [
            ("S", "S"),
            ("M", "M"),
            ("L", "L"),
            ("XL", "XL"),
            ("XXL", "2XL"),
        ]
        for index, (value, display_name) in enumerate(values):
            CatalogOptionValue.objects.create(
                option=option,
                value=value,
                display_name=display_name,
                order=index,
            )

        product = Product.objects.create(
            title="Футболка базова чорна",
            slug="basic-tee-black",
            category=self.tshirt_category,
            catalog=self.tshirt_catalog,
            price=1200,
            description="Catalog option sizing.",
            status=ProductStatus.PUBLISHED,
        )

        size_context = resolve_product_size_context(product, requested_size="2XL")

        self.assertEqual(size_context["sizes"], ["S", "M", "L", "XL", "XXL"])
        self.assertEqual(size_context["selected_size"], "XXL")
        self.assertEqual(size_context["display_labels"]["XXL"], "2XL")

    def test_preset_detection_without_size_grid_falls_back_to_generic_guidance(self):
        product = Product.objects.create(
            title="Heavyweight hoodie",
            slug="heavyweight-hoodie-no-grid",
            category=self.hoodie_category,
            price=1800,
            description="No confirmed size grid yet.",
            status=ProductStatus.PUBLISHED,
        )

        guide = resolve_product_size_guide(product)

        self.assertEqual(guide["source"], "fallback")
        self.assertEqual(guide["rows"], [])
        self.assertEqual(guide["guide_key"], "hoodie")

    def test_public_size_guide_blocks_require_actual_size_grids(self):
        empty_blocks = build_public_size_guide_blocks([])

        self.assertEqual(empty_blocks["blocks"], [])

        populated_blocks = build_public_size_guide_blocks([self.catalog_default_grid, self.override_grid])

        self.assertEqual([block["guide_key"] for block in populated_blocks["blocks"]], ["hoodie", "basic_tshirt"])

    def test_legacy_context_shape_is_unchanged_without_fable_assignment(self):
        product = Product.objects.create(
            title="Legacy guide product",
            slug="legacy-guide-product",
            category=self.hoodie_category,
            catalog=self.hoodie_catalog,
            price=1700,
        )

        context = resolve_product_size_context(product)

        self.assertEqual(
            set(context),
            {
                "sizes",
                "selected_size",
                "display_labels",
                "guide",
                "profile",
                "size_help_cta",
            },
        )

    def test_fable_assignments_add_both_fit_grids_to_context(self):
        from fable5.models import ProductOptionSizeGrid, SizeGridProfile

        product = Product.objects.create(
            title="Fable comparison product",
            slug="fable-comparison-product",
            category=self.tshirt_category,
            catalog=self.tshirt_catalog,
            price=1200,
        )
        for order, code in enumerate(("classic", "oversize")):
            product.fit_options.create(
                code=code,
                label="Класика" if code == "classic" else "Оверсайз",
                order=order,
                is_default=code == "classic",
            )
            grid = SizeGrid.objects.create(
                catalog=self.tshirt_catalog,
                name=f"{code} structured grid",
                order=order,
                guide_data={
                    "columns": [{"key": "size", "label": "Розмір"}],
                    "rows": [{"size": "S"}, {"size": "M"}],
                },
            )
            SizeGridProfile.objects.create(size_grid=grid, option_key=f"fit={code}")
            ProductOptionSizeGrid.objects.create(
                product=product,
                option_key=f"fit={code}",
                size_grid=grid,
            )

        context = resolve_product_size_context(product, requested_size="M")

        self.assertTrue(context["fable5_size_grids"])
        self.assertEqual(context["selected_size"], "M")
        self.assertEqual(
            [item["fit_code"] for item in context["size_grid_comparison"]],
            ["classic", "oversize"],
        )


class ProductDetailSizeGuideViewTests(TestCase):
    def setUp(self):
        self.client = self.client_class()
        self.feed_task_patcher = patch(
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            return_value=None,
        )
        self.feed_task_patcher.start()
        self.addCleanup(self.feed_task_patcher.stop)

        self.hoodie_category = Category.objects.create(name="Hoodies", slug="hoodie")
        self.tshirt_category = Category.objects.create(name="Футболки", slug="futbolki")
        self.misc_category = Category.objects.create(name="Accessories", slug="accessories")
        self.hoodie_catalog = Catalog.objects.create(name="Hoodies", slug="hoodie")
        self.tshirt_catalog = Catalog.objects.create(name="Basic T-Shirts", slug="basic-tshirts")
        self.hoodie_grid = SizeGrid.objects.create(
            catalog=self.hoodie_catalog,
            name="Hoodie size guide",
            description="Structured hoodie guide",
            is_active=True,
            order=0,
        )
        self.tshirt_grid = SizeGrid.objects.create(
            catalog=self.tshirt_catalog,
            name="Basic tee size guide",
            description="Structured t-shirt guide",
            is_active=True,
            order=0,
        )

    @patch("storefront.views.product.ProductRecommendationEngine.get_recommendations", return_value=[])
    @patch("storefront.views.product.get_detailed_color_variants", return_value=[])
    def test_product_detail_renders_hoodie_garment_measurements(
        self,
        _color_variants_mock,
        _recommendations_mock,
    ):
        product = Product.objects.create(
            title="Classic hoodie",
            slug="classic-hoodie-pdp",
            category=self.hoodie_category,
            catalog=self.hoodie_catalog,
            price=1750,
            description="PDP size guide coverage.",
            status=ProductStatus.PUBLISHED,
        )

        response = self.client.get(reverse("product", kwargs={"slug": product.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["resolved_size_guide"]["source"], "catalog_default")
        self.assertEqual(response.context["resolved_size_guide"]["guide_key"], "hoodie")
        self.assertContains(response, "Length")
        self.assertContains(response, "Width")
        self.assertContains(response, "Порівняйте заміри зі своїм худі")
        self.assertNotContains(response, "Груди (см)")
        self.assertNotContains(response, "Талія (см)")

    @patch("storefront.views.product.ProductRecommendationEngine.get_recommendations", return_value=[])
    @patch("storefront.views.product.get_detailed_color_variants", return_value=[])
    def test_product_detail_renders_tshirt_measurements_and_2xl_label(
        self,
        _color_variants_mock,
        _recommendations_mock,
    ):
        option = CatalogOption.objects.create(
            catalog=self.tshirt_catalog,
            name="Розмір",
            option_type=CatalogOption.OptionType.SIZE,
            order=0,
        )
        for index, (value, display_name) in enumerate(
            [("S", "S"), ("M", "M"), ("L", "L"), ("XL", "XL"), ("XXL", "2XL")]
        ):
            CatalogOptionValue.objects.create(
                option=option,
                value=value,
                display_name=display_name,
                order=index,
            )

        product = Product.objects.create(
            title="Футболка базова чорна",
            slug="basic-tee-pdp",
            category=self.tshirt_category,
            catalog=self.tshirt_catalog,
            price=1100,
            description="T-shirt guide coverage.",
            status=ProductStatus.PUBLISHED,
        )

        response = self.client.get(reverse("product", kwargs={"slug": product.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["resolved_size_guide"]["guide_key"], "basic_tshirt")
        self.assertContains(response, "Плечі (C)")
        self.assertContains(response, "Рукав (D)")
        self.assertContains(response, 'for="size-xxl"', html=False)
        self.assertContains(response, "2XL")

    @patch("storefront.views.product.ProductRecommendationEngine.get_recommendations", return_value=[])
    @patch("storefront.views.product.get_detailed_color_variants", return_value=[])
    def test_product_detail_falls_back_for_unknown_category_without_old_table(
        self,
        _color_variants_mock,
        _recommendations_mock,
    ):
        product = Product.objects.create(
            title="Mystery accessory",
            slug="mystery-accessory",
            category=self.misc_category,
            price=900,
            description="No catalog and no size grid.",
            status=ProductStatus.PUBLISHED,
        )

        response = self.client.get(reverse("product", kwargs={"slug": product.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["resolved_size_guide"]["source"], "fallback")
        self.assertContains(response, reverse("size_guide"))
        self.assertContains(response, "Потрібна допомога з розміром")
        self.assertNotContains(response, "Груди (см)")
        self.assertNotContains(response, "Талія (см)")
