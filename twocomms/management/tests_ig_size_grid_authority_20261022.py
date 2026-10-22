"""Catalog display and bot measurements share explicit hoodie chart authority."""
from copy import deepcopy
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.utils import translation

from management.services.ig_catalog_configured_choices import _configuration, size_measurements
from product_catalog.models import ProductOptionSizeGrid, SizeGridProfile, VariantSizeRule
from product_catalog.size_grid_services import build_size_grid_comparison
from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, CatalogOption, CatalogOptionValue, Category, Product, ProductFitOption, SizeGrid


def legacy_seed():
    return {"profile_key": "hoodie", "eyebrow": "Hoodie fit guide",
        "aliases": ["hoodie", "hoodies", "худі", "худи", "fleece", "флис"],
        "columns": [{"key": "size", "label": "Розмір"},
                    {"key": "length", "label": "Довжина"},
                    {"key": "width", "label": "Ширина"}],
        "rows": [{"size": size, "length": length, "width": width} for size, length, width in (
            ("XS", "54", "52"), ("S", "58", "56"), ("M", "67", "60"),
            ("L", "70", "64"), ("XL", "73", "68"), ("XXL", "78", "72"))]}


class BotHoodieMeasurementAuthorityTests(SimpleTestCase):
    def test_persisted_seed_cannot_supply_bot_measurements_or_review_flag_bypass(self):
        raw = legacy_seed()
        raw.update(reviewed=True, measurement_authority="approved")
        grid = SimpleNamespace(pk=2, guide_data=raw)
        self.assertEqual(size_measurements(grid, product_id=9), {})
        self.assertEqual(size_measurements(None, product_id=9), {})

    def test_custom_measurements_preserve_exact_cells_columns_units_and_semantics(self):
        raw = {"profile_key": "hoodie", "intro": "Garment measured laid flat, cm.",
            "columns": [{"key": "size", "label": "Size"},
                        {"key": "length", "label": "Garment length (cm)"},
                        {"key": "width", "label": "Half chest laid flat (cm)"}],
            "rows": [{"size": "M", "length": "99", "width": "88"}]}
        result = size_measurements(SimpleNamespace(pk=2, guide_data=raw), product_id=9)
        self.assertEqual(result["rows"], raw["rows"])
        self.assertEqual(result["columns"], raw["columns"][1:])
        self.assertEqual(result["unit"], "cm")
        self.assertEqual(result["measurement_semantics"], "flat_garment")
        self.assertEqual(raw["rows"][0]["length"], "99")


class CatalogHoodieMeasurementAuthorityTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name="Hoodies authority", slug="hoodies-authority")
        self.category = Category.objects.create(name="Hoodies", slug="hoodie-authority")
        self.product = Product.objects.create(title="Authority hoodie", slug="authority-hoodie",
            catalog=self.catalog, category=self.category, price=1200)
        ProductFitOption.objects.create(product=self.product, code="oversize", label="Оверсайз",
                                       is_default=True, is_active=True)
        self.grid = SizeGrid.objects.create(catalog=self.catalog, name="Historical hoodie grid",
            guide_data=legacy_seed(), is_active=True)
        SizeGridProfile.objects.create(size_grid=self.grid, option_key="fit=oversize")
        self.assignment = ProductOptionSizeGrid.objects.create(product=self.product,
            option_key="fit=oversize", size_grid=self.grid)
        self.size_option = CatalogOption.objects.create(catalog=self.catalog, name="Size",
            option_type=CatalogOption.OptionType.SIZE)
        for order, value in enumerate(("S", "M", "2XL")):
            CatalogOptionValue.objects.create(option=self.size_option, value=value,
                display_name=value, order=order)
        color = Color.objects.create(name="Black authority", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(product=self.product, color=color,
            slug="black-authority")

    def comparison(self, lang="uk"):
        return build_size_grid_comparison(self.product, variants=[self.variant], lang=lang)

    def bot(self):
        self.product._prefetched_objects_cache = {"product_catalog_size_grid_assignments": [self.assignment],
                                                 "product_catalog_size_rules": []}
        self.catalog._prefetched_objects_cache = {"options": [self.size_option], "size_grids": [self.grid]}
        self.size_option._prefetched_objects_cache = {"values": list(self.size_option.values.all())}
        self.variant._prefetched_objects_cache = {"product_catalog_size_grid_assignments": [],
                                                 "product_catalog_size_rules": []}
        return _configuration(self.product, self.variant, {"fit": "oversize"}, metadata_only=True)

    def test_seed_is_hidden_in_matrix_but_catalog_and_variant_sizes_remain_sellable(self):
        VariantSizeRule.objects.create(variant=self.variant, fit_code="oversize", size="M", is_enabled=False)
        item = self.comparison()[0]
        self.assertEqual(item["available_sizes"], ["S", "M", "XXL"])
        self.assertEqual(item["variants"][0]["available_sizes"], ["S", "XXL"])
        for guide in (item["guide"], item["variants"][0]["guide"]):
            self.assertEqual(guide["measurement_authority"], "legacy_seed_unverified")
            self.assertEqual(guide["rows"], [])
            self.assertEqual(guide["columns"], [])
            self.assertFalse(guide["image_url"])
        for row in item["sizes"] + item["variants"][0]["sizes"]:
            self.assertNotIn("length", row)
            self.assertNotIn("width", row)
        self.grid.refresh_from_db()
        self.assertEqual(self.grid.guide_data, legacy_seed())

    def test_bot_keeps_catalog_choices_without_seed_measurement_authority(self):
        result = self.bot()
        self.assertEqual([row["value"] for row in result["sizes"]], ["S", "M", "XXL"])
        self.assertEqual(result["grid"]["measurements"], {})
        self.assertEqual(result["grid"]["grid_id"], self.grid.pk)

    def test_legacy_matrix_html_discloses_missing_chart_in_each_locale(self):
        expected = {"uk": "підтверджених числових замірів", "ru": "подтверждённых числовых замеров",
                    "en": "Confirmed numerical measurements"}
        for lang, disclosure in expected.items():
            with self.subTest(lang=lang), translation.override(lang):
                comparison = self.comparison(lang)
                comparison[0]["display_guide"] = comparison[0]["guide"]
                html = render_to_string("product_catalog/_size_grid_comparison.html", {
                    "product": self.product, "size_grid_comparison": comparison,
                    "preselected_fit_code": "oversize", "size_advisor_enabled": False})
                self.assertIn(disclosure, html)
                self.assertNotIn("Точні виміри", html)
                self.assertNotIn("Класичний крій починається", html)
                self.assertNotIn("tc-size-table", html)
                self.assertNotIn("T-shirt", html)
                self.assertNotIn("футбол", html.lower())

    def test_custom_hoodie_rows_are_preserved_in_bot_matrix_and_localized_copy(self):
        raw = {"profile_key": "hoodie", "title": "Measured hoodie", "intro": "Measured laid flat, cm.",
            "columns": [{"key": "size", "label": "Size"}, {"key": "length", "label": "Length (cm)"},
                        {"key": "width", "label": "Half chest (cm)"}],
            "rows": [{"size": "M", "length": "99", "width": "88"}]}
        self.grid.guide_data = deepcopy(raw)
        self.grid.save(update_fields=["guide_data"])
        for lang in ("uk", "ru", "en"):
            with self.subTest(lang=lang):
                guide = self.comparison(lang)[0]["guide"]
                self.assertEqual(guide["rows"][0]["length"], "99")
                self.assertEqual(guide["rows"][0]["width"], "88")
                self.assertEqual(guide["intro"], raw["intro"])
                self.assertEqual(guide["title"], raw["title"])
                self.assertEqual(guide["columns"], raw["columns"])
                self.assertNotIn("футбол", str(guide).lower())
                self.assertNotIn("T-shirt", str(guide))
                self.assertNotIn("±", str(guide))
        self.assertEqual(self.bot()["grid"]["measurements"]["rows"], raw["rows"])

    def test_no_configured_grid_never_creates_measurements(self):
        self.assignment.delete()
        self.grid.delete()
        self.assertEqual(self.comparison(), [])
        self.product._prefetched_objects_cache = {"product_catalog_size_grid_assignments": [],
                                                 "product_catalog_size_rules": []}
        self.catalog._prefetched_objects_cache = {"options": [self.size_option], "size_grids": []}
        self.size_option._prefetched_objects_cache = {"values": list(self.size_option.values.all())}
        result = _configuration(self.product, None, {"fit": "oversize"}, metadata_only=True)
        self.assertEqual([row["value"] for row in result["sizes"]], ["S", "M", "XXL"])
        self.assertEqual(result["grid"]["measurements"], {})

    def test_known_tshirt_chart_keeps_its_own_measurements_and_locale(self):
        from product_catalog.default_size_guides import OVERSIZE_GUIDE_DATA
        self.product.title = "Current T-shirt"
        self.category.name = "T-shirts"
        self.category.slug = "tshirts"
        self.catalog.name = "T-shirts"
        self.catalog.slug = "tshirts"
        self.grid.guide_data = deepcopy(OVERSIZE_GUIDE_DATA)
        self.grid.save(update_fields=["guide_data"])
        guide = self.comparison("en")[0]["guide"]
        self.assertEqual(guide["title"], "Oversize T-shirt size chart")
        self.assertEqual(guide["rows"][0]["garment_length"], "70")
        self.assertTrue(guide["image_url"].endswith("oversize-tshirt.webp"))
