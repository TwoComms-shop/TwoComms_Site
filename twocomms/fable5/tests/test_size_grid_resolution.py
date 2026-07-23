from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.template.loader import render_to_string
from django.utils import translation
from io import StringIO
from django.test import TestCase, override_settings
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from productcolors.models import Color, ProductColorVariant
from storefront.models import (
    Catalog,
    CatalogOption,
    CatalogOptionValue,
    Category,
    Product,
    ProductFitOption,
    SizeGrid,
)

from fable5.models import (
    ProductOptionSizeGrid,
    ProductSizeRule,
    SizeGridProfile,
    VariantSizeRule,
)


class SizeGridNormalizationTests(TestCase):
    def test_classic_fs101_guide_matches_supplier_measurements(self):
        from fable5.default_size_guides import CLASSIC_GUIDE_DATA

        self.assertEqual(
            CLASSIC_GUIDE_DATA["columns"],
            [
                {"key": "size", "label": "Міжнародний розмір"},
                {"key": "chest", "label": "Обхват грудей"},
                {"key": "garment_length", "label": "Довжина виробу"},
                {"key": "sleeve_length", "label": "Довжина рукава"},
                {"key": "shoulder_width", "label": "Ширина плечей"},
            ],
        )
        self.assertEqual(
            CLASSIC_GUIDE_DATA["rows"],
            [
                {"size": "S", "chest": "92", "garment_length": "65", "sleeve_length": "16", "shoulder_width": "43"},
                {"size": "M", "chest": "100", "garment_length": "68", "sleeve_length": "17", "shoulder_width": "44"},
                {"size": "L", "chest": "108", "garment_length": "70", "sleeve_length": "19", "shoulder_width": "47"},
                {"size": "XL", "chest": "116", "garment_length": "74", "sleeve_length": "21", "shoulder_width": "49"},
                {"size": "2XL", "chest": "124", "garment_length": "76", "sleeve_length": "22", "shoulder_width": "52"},
                {"size": "3XL", "chest": "132", "garment_length": "79", "sleeve_length": "24", "shoulder_width": "53"},
            ],
        )

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

    def test_canonical_oversize_profile_is_fallback_for_new_products(self):
        from fable5.size_grid_services import resolve_option_size_grid

        ProductOptionSizeGrid.objects.filter(
            product=self.product,
            option_key="fit=oversize",
        ).delete()
        SizeGridProfile.objects.filter(size_grid=self.oversize_grid).delete()
        self.assertEqual(
            resolve_option_size_grid(self.product, "fit=oversize"),
            None,
        )
        SizeGridProfile.objects.create(
            size_grid=self.oversize_grid,
            option_key="fit=oversize",
            garment_code="tshirt",
        )
        self.assertEqual(
            resolve_option_size_grid(self.product, "fit=oversize"),
            self.oversize_grid,
        )

    def test_classic_falls_back_to_legacy_catalog_grid_without_copying(self):
        from fable5.size_grid_services import resolve_option_size_grid

        ProductOptionSizeGrid.objects.filter(
            product=self.product,
            option_key="fit=classic",
        ).delete()
        self.assertEqual(
            resolve_option_size_grid(self.product, "fit=classic"),
            self.classic_grid,
        )

    def test_comparison_adds_fallback_and_product_specific_image_alt(self):
        from fable5.size_grid_services import build_size_grid_comparison

        ProductOptionSizeGrid.objects.filter(
            product=self.product,
            option_key="fit=oversize",
        ).delete()
        comparison = build_size_grid_comparison(self.product, lang="uk")
        oversize = next(item for item in comparison if item["fit_code"] == "oversize")
        self.assertEqual([row["size"] for row in oversize["sizes"]], ["XS", "S", "M", "L"])
        self.assertIn(self.product.title, oversize["guide"]["image_alt"])
        self.assertEqual(oversize["guide"]["image_width"], 2400)
        self.assertEqual(oversize["guide"]["image_height"], 1800)

    def test_classic_guide_uses_uploaded_chart_fallback(self):
        from fable5.size_grid_services import build_size_grid_comparison

        comparison = build_size_grid_comparison(self.product, lang="uk")
        classic = next(item for item in comparison if item["fit_code"] == "classic")

        self.assertTrue(
            classic["guide"]["image_url"].endswith("img/size-guides/classic-tshirt.webp"),
            classic["guide"]["image_url"],
        )
        self.assertEqual(classic["guide"]["image_width"], 993)
        self.assertEqual(classic["guide"]["image_height"], 292)

    def test_guide_rows_do_not_expand_catalog_sellable_sizes(self):
        from fable5.default_size_guides import CLASSIC_GUIDE_DATA
        from fable5.size_grid_services import build_size_grid_comparison

        size_option = CatalogOption.objects.create(
            catalog=self.catalog,
            name="Розмір",
            option_type=CatalogOption.OptionType.SIZE,
        )
        for order, size in enumerate(("S", "M", "L", "XL", "XXL")):
            CatalogOptionValue.objects.create(
                option=size_option,
                value=size,
                display_name="2XL" if size == "XXL" else size,
                order=order,
            )
        self.classic_grid.guide_data = CLASSIC_GUIDE_DATA
        self.classic_grid.save(update_fields=["guide_data"])

        comparison = build_size_grid_comparison(self.product, lang="uk")
        classic = next(item for item in comparison if item["fit_code"] == "classic")

        self.assertEqual(
            [row["display_size"] for row in classic["sizes"]],
            ["S", "M", "L", "XL", "2XL", "3XL"],
        )
        self.assertEqual(classic["available_sizes"], ["S", "M", "L", "XL", "XXL"])

    def test_informational_3xl_does_not_expand_legacy_sellable_sizes(self):
        from fable5.default_size_guides import CLASSIC_GUIDE_DATA
        from fable5.size_grid_services import build_size_grid_comparison
        from storefront.services.size_guides import resolve_product_size_context

        self.classic_grid.guide_data = CLASSIC_GUIDE_DATA
        self.classic_grid.save(update_fields=["guide_data"])

        comparison = build_size_grid_comparison(self.product, lang="uk")
        classic = next(item for item in comparison if item["fit_code"] == "classic")
        context = resolve_product_size_context(self.product)

        self.assertEqual(
            [row["display_size"] for row in classic["sizes"]],
            ["S", "M", "L", "XL", "2XL", "3XL"],
        )
        self.assertEqual(classic["available_sizes"], ["S", "M", "L", "XL", "XXL"])
        self.assertEqual(context["sizes"], ["S", "M", "L", "XL", "XXL"])
        self.assertNotIn("XXXL", context["display_labels"])

    def test_explicit_catalog_3xl_becomes_sellable(self):
        from fable5.default_size_guides import CLASSIC_GUIDE_DATA
        from fable5.size_grid_services import build_size_grid_comparison
        from storefront.services.size_guides import resolve_product_size_context

        size_option = CatalogOption.objects.create(
            catalog=self.catalog,
            name="Розмір",
            option_type=CatalogOption.OptionType.SIZE,
        )
        for order, size in enumerate(("S", "M", "L", "XL", "XXL", "XXXL")):
            CatalogOptionValue.objects.create(
                option=size_option,
                value=size,
                display_name={"XXL": "2XL", "XXXL": "3XL"}.get(size, size),
                order=order,
            )
        self.classic_grid.guide_data = CLASSIC_GUIDE_DATA
        self.classic_grid.save(update_fields=["guide_data"])

        classic = next(
            item for item in build_size_grid_comparison(self.product, lang="uk")
            if item["fit_code"] == "classic"
        )
        context = resolve_product_size_context(self.product)

        self.assertEqual(classic["available_sizes"], ["S", "M", "L", "XL", "XXL", "XXXL"])
        self.assertEqual(context["sizes"], ["S", "M", "L", "XL", "XXL", "XXXL"])
        self.assertEqual(context["display_labels"]["XXXL"], "3XL")

    def test_ensure_tshirt_guides_updates_classic_grid_idempotently(self):
        from fable5.default_size_guides import CLASSIC_GUIDE_DATA

        ProductOptionSizeGrid.objects.filter(
            product=self.product,
            option_key="fit=classic",
        ).delete()
        output = StringIO()
        call_command("ensure_tshirt_size_guides", stdout=output, no_input=True)
        self.classic_grid.refresh_from_db()
        assignment = ProductOptionSizeGrid.objects.get(
            product=self.product,
            option_key="fit=classic",
        )

        self.assertEqual(assignment.size_grid_id, self.classic_grid.id)
        self.assertEqual(self.classic_grid.guide_data, CLASSIC_GUIDE_DATA)
        self.assertTrue(
            self.classic_grid.image.name.startswith("size_grids/classic-tshirt"),
            self.classic_grid.image.name,
        )
        first_image_name = self.classic_grid.image.name

        second_output = StringIO()
        call_command("ensure_tshirt_size_guides", stdout=second_output, no_input=True)
        self.classic_grid.refresh_from_db()
        self.assertEqual(self.classic_grid.image.name, first_image_name)
        self.assertIn("assignments_created=0", second_output.getvalue())

    def test_ensure_tshirt_guides_does_not_overwrite_ambiguous_grid(self):
        ProductOptionSizeGrid.objects.filter(
            product=self.product,
            option_key="fit=classic",
        ).delete()
        self.classic_grid.fable5_profile.delete()
        original_payload = {
            "columns": [{"key": "size", "label": "Size"}],
            "rows": [{"size": "S"}],
            "profile_key": "unrelated_garment",
        }
        self.classic_grid.name = "Unrelated garment grid"
        self.classic_grid.guide_data = original_payload
        self.classic_grid.save(update_fields=["name", "guide_data"])

        call_command("ensure_tshirt_size_guides", stdout=StringIO(), no_input=True)

        self.classic_grid.refresh_from_db()
        assignment = ProductOptionSizeGrid.objects.get(
            product=self.product,
            option_key="fit=classic",
        )
        self.assertEqual(self.classic_grid.guide_data, original_payload)
        self.assertNotEqual(assignment.size_grid_id, self.classic_grid.id)
        self.assertEqual(assignment.size_grid.fable5_profile.option_key, "fit=classic")

    def test_ensure_tshirt_guides_fails_when_canonical_asset_is_missing(self):
        with TemporaryDirectory() as temp_dir, override_settings(BASE_DIR=Path(temp_dir)):
            with self.assertRaises(CommandError):
                call_command("ensure_tshirt_size_guides", stdout=StringIO(), no_input=True)

    def test_ensure_oversize_command_fills_only_missing_assignment(self):
        ProductOptionSizeGrid.objects.filter(
            product=self.product,
            option_key="fit=oversize",
        ).delete()
        output = StringIO()
        call_command("ensure_oversize_size_guides", stdout=output, no_input=True)
        assignment = ProductOptionSizeGrid.objects.get(
            product=self.product,
            option_key="fit=oversize",
        )
        self.assertEqual(assignment.size_grid_id, self.oversize_grid.id)
        self.assertTrue(
            assignment.size_grid.image.name.startswith("size_grids/oversize-tshirt"),
            assignment.size_grid.image.name,
        )
        self.assertIn("assignments_created=1", output.getvalue())

    def test_comparison_template_exposes_independent_accessible_guide_tabs(self):
        from fable5.size_grid_services import build_size_grid_comparison

        comparison = build_size_grid_comparison(self.product, lang="uk")
        for item in comparison:
            item["display_guide"] = item["guide"]
        html = render_to_string(
            "fable5/_size_grid_comparison.html",
            {
                "product": self.product,
                "size_grid_comparison": comparison,
                "size_advisor_enabled": True,
            },
        )
        self.assertIn('data-size-guide-fit="classic"', html)
        self.assertIn('data-size-guide-fit="oversize"', html)
        self.assertIn('data-size-advisor-tab', html)
        self.assertIn('data-size-advisor-form', html)
        self.assertIn('role="tablist"', html)
        self.assertIn("Класична таблиця охоплює S–3XL", html)
        self.assertIn("Таблиця розмірів оверсайз футболки", html)

    def test_product_template_uses_thin_size_actions_without_large_compare_block(self):
        template_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme/templates/pages/product_detail.html"
        )
        source = template_path.read_text(encoding="utf-8")

        self.assertIn('data-pdp-size-tool-trigger="guide"', source)
        self.assertIn('data-pdp-size-tool-trigger="advisor"', source)
        self.assertNotIn("tc-size-compare-link", source)

    def test_size_advisor_template_copy_is_translated_in_ru_and_en(self):
        from fable5.size_grid_services import build_size_grid_comparison

        comparison = build_size_grid_comparison(self.product, lang="uk")
        for item in comparison:
            item["display_guide"] = item["guide"]
        context = {
            "product": self.product,
            "size_grid_comparison": comparison,
            "preselected_fit_code": "classic",
            "size_advisor_enabled": True,
        }

        with translation.override("ru"):
            ru_html = render_to_string("fable5/_size_grid_comparison.html", context)
        with translation.override("en"):
            en_html = render_to_string("fable5/_size_grid_comparison.html", context)

        self.assertIn("Подобрать размер", ru_html)
        self.assertIn("Укажите рост от 145 до 210 см.", ru_html)
        self.assertIn("Find my size", en_html)
        self.assertIn("Enter a height from 145 to 210 cm.", en_html)

    def test_non_tshirt_comparison_has_no_advisor_or_tshirt_image_fallback(self):
        from fable5.size_grid_services import build_size_grid_comparison

        self.product.title = "Heavy hoodie"
        self.product.save(update_fields=["title"])
        self.category.name = "Hoodies"
        self.category.slug = "hoodies"
        self.category.save(update_fields=["name", "slug"])
        self.catalog.name = "Hoodies"
        self.catalog.slug = "hoodies"
        self.catalog.save(update_fields=["name", "slug"])

        comparison = build_size_grid_comparison(self.product, lang="uk")
        for item in comparison:
            item["display_guide"] = item["guide"]
        html = render_to_string(
            "fable5/_size_grid_comparison.html",
            {
                "product": self.product,
                "size_grid_comparison": comparison,
                "preselected_fit_code": "classic",
                "size_advisor_enabled": False,
            },
        )

        self.assertNotIn("data-size-advisor-tab", html)
        self.assertNotIn("data-size-advisor-panel", html)
        self.assertTrue(all(not item["guide"]["image_url"] for item in comparison))

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

    def test_comparison_localizes_measurement_content_for_ru_and_en(self):
        from fable5.default_size_guides import CLASSIC_GUIDE_DATA
        from fable5.size_grid_services import build_size_grid_comparison

        self.classic_grid.guide_data = CLASSIC_GUIDE_DATA
        self.classic_grid.save(update_fields=["guide_data"])

        ru_classic = next(
            item for item in build_size_grid_comparison(self.product, lang="ru")
            if item["fit_code"] == "classic"
        )
        en_classic = next(
            item for item in build_size_grid_comparison(self.product, lang="en")
            if item["fit_code"] == "classic"
        )

        self.assertEqual(ru_classic["guide"]["title"], "Таблица размеров классической футболки")
        self.assertEqual(
            [column["label"] for column in ru_classic["guide"]["columns"]],
            ["Размер", "Обхват груди", "Длина изделия", "Длина рукава", "Ширина плеч"],
        )
        self.assertEqual(en_classic["guide"]["title"], "Classic T-shirt size chart")
        self.assertEqual(
            [column["label"] for column in en_classic["guide"]["columns"]],
            ["Size", "Chest circumference", "Garment length", "Sleeve length", "Shoulder width"],
        )

    def test_size_advisor_schema_describes_free_tool_and_howto(self):
        from storefront.templatetags.seo_tags import size_advisor_schema

        markup = str(
            size_advisor_schema(
                self.product,
                language="en",
                canonical_path="/product/grid-product/",
            )
        )
        payload = json.loads(re.search(r">(.*)</script>", markup, re.S).group(1))
        graph = payload["@graph"]

        self.assertEqual([item["@type"] for item in graph], ["WebApplication", "HowTo"])
        self.assertEqual(graph[0]["offers"]["price"], "0")
        self.assertIn("height", graph[0]["description"].lower())
        self.assertEqual(len(graph[1]["step"]), 3)

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
