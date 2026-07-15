from django.core.exceptions import ValidationError
from django.test import TestCase

from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid

from fable5.models import (
    ProductOptionSizeGrid,
    ProductSizeRule,
    SizeGridProfile,
    VariantSizeRule,
)


class SizeGridNormalizationTests(TestCase):
    def test_normalizes_aliases_and_preserves_order_and_text_cells(self):
        from fable5.size_grid_services import normalize_size_grid_payload

        payload = normalize_size_grid_payload(
            {
                "title": "<b>Класика</b>",
                "columns": [
                    {"key": "size", "label": "Розмір"},
                    {"key": "chest", "label": "Груди"},
                ],
                "rows": [
                    {"size": "2XL", "chest": "58,5", "fabric_note": "stretch"},
                    {"size": "L", "chest": "52"},
                ],
            }
        )

        self.assertEqual(payload["title"], "Класика")
        self.assertEqual([column["key"] for column in payload["columns"]], ["size", "chest"])
        self.assertEqual([row["size"] for row in payload["rows"]], ["XXL", "L"])
        self.assertEqual(payload["rows"][0]["display_size"], "2XL")
        self.assertEqual(payload["rows"][0]["fabric_note"], "stretch")

    def test_all_supported_xxl_aliases_share_one_canonical_value(self):
        from fable5.size_grid_services import normalize_size_value

        self.assertEqual(
            [normalize_size_value(value) for value in ("2XL", "XXL", "x2l")],
            ["XXL", "XXL", "XXL"],
        )

    def test_rejects_duplicate_normalized_rows(self):
        from fable5.size_grid_services import normalize_size_grid_payload

        with self.assertRaises(ValidationError):
            normalize_size_grid_payload(
                {
                    "columns": [{"key": "size", "label": "Розмір"}],
                    "rows": [{"size": "2XL"}, {"size": "x2l"}],
                }
            )

    def test_rejects_duplicate_columns_missing_size_and_empty_rows(self):
        from fable5.size_grid_services import normalize_size_grid_payload

        invalid_payloads = (
            {
                "columns": [
                    {"key": "size", "label": "Розмір"},
                    {"key": "SIZE", "label": "Duplicate"},
                ],
                "rows": [{"size": "S"}],
            },
            {
                "columns": [{"key": "width", "label": "Ширина"}],
                "rows": [{"width": "50"}],
            },
            {"columns": [{"key": "size", "label": "Розмір"}], "rows": []},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                normalize_size_grid_payload(payload)


class EffectiveSizeGridTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name="T-shirts grids", slug="tshirts-grids")
        self.category = Category.objects.create(name="T-shirts grid", slug="tshirts-grid")
        self.product = Product.objects.create(
            title="Grid product",
            slug="grid-product",
            category=self.category,
            catalog=self.catalog,
            price=1200,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Класика",
            is_default=True,
            is_active=True,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="oversize",
            label="Оверсайз",
            order=1,
            is_active=True,
        )
        self.classic_grid = self._grid(
            "Classic grid",
            "fit=classic",
            ["S", "M", "L", "XL", "2XL"],
        )
        self.oversize_grid = self._grid(
            "Oversize grid",
            "fit=oversize",
            ["XS", "S", "M", "L"],
        )
        ProductOptionSizeGrid.objects.create(
            product=self.product,
            option_key="fit=classic",
            size_grid=self.classic_grid,
        )
        ProductOptionSizeGrid.objects.create(
            product=self.product,
            option_key="fit=oversize",
            size_grid=self.oversize_grid,
        )
        color = Color.objects.create(name="Black grid", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            slug="black-grid",
        )

    def _grid(self, name, option_key, sizes):
        grid = SizeGrid.objects.create(
            catalog=self.catalog,
            name=name,
            guide_data={
                "columns": [
                    {"key": "size", "label": "Розмір"},
                    {"key": "width", "label": "Ширина"},
                ],
                "rows": [
                    {"size": size, "width": str(50 + index)}
                    for index, size in enumerate(sizes)
                ],
            },
            is_active=True,
        )
        SizeGridProfile.objects.create(size_grid=grid, option_key=option_key)
        return grid

    def test_product_and_color_fit_rules_narrow_effective_sizes(self):
        from fable5.size_grid_services import resolve_effective_sizes

        ProductSizeRule.objects.create(
            product=self.product,
            option_key="fit=classic",
            size="S",
            is_enabled=False,
        )
        VariantSizeRule.objects.create(
            variant=self.variant,
            fit_code="classic",
            size="2XL",
            is_enabled=False,
        )

        base = resolve_effective_sizes(self.product, "fit=classic")
        black = resolve_effective_sizes(self.product, "fit=classic", variant=self.variant)

        self.assertEqual([row["size"] for row in base], ["M", "L", "XL", "XXL"])
        self.assertEqual([row["size"] for row in black], ["M", "L", "XL"])

    def test_fit_assignments_resolve_different_grids(self):
        from fable5.size_grid_services import resolve_option_size_grid

        self.assertEqual(
            resolve_option_size_grid(self.product, "fit=classic"),
            self.classic_grid,
        )
        self.assertEqual(
            resolve_option_size_grid(self.product, "fit=oversize"),
            self.oversize_grid,
        )

    def test_comparison_contains_both_fits_without_selecting_one(self):
        from fable5.size_grid_services import build_size_grid_comparison

        comparison = build_size_grid_comparison(
            self.product,
            variants=[self.variant],
            lang="uk",
        )

        self.assertEqual([item["fit_code"] for item in comparison], ["classic", "oversize"])
        self.assertEqual(comparison[0]["label"], "Класика")
        self.assertEqual(comparison[1]["label"], "Оверсайз")
        self.assertEqual(comparison[0]["variants"][0]["variant_id"], self.variant.id)

    def test_readiness_requires_active_explicit_grid_for_every_active_fit(self):
        from fable5.readiness import build_readiness

        ProductOptionSizeGrid.objects.filter(
            product=self.product,
            option_key="fit=oversize",
        ).delete()

        readiness = build_readiness(self.product)

        self.assertFalse(readiness["is_ready"])
        self.assertIn("fit=oversize", {issue["option_key"] for issue in readiness["errors"]})

    def test_inactive_grid_is_not_resolved_and_blocks_readiness(self):
        from fable5.readiness import build_readiness
        from fable5.size_grid_services import resolve_effective_sizes

        self.classic_grid.is_active = False
        self.classic_grid.save(update_fields=["is_active"])

        self.assertEqual(resolve_effective_sizes(self.product, "fit=classic"), [])
        readiness = build_readiness(self.product)
        self.assertFalse(readiness["is_ready"])
        self.assertTrue(any(issue["code"] == "inactive_size_grid" for issue in readiness["errors"]))
