"""Hoodie charts require their own explicit measurement source."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from management.services.ig_catalog_configured_choices import size_measurements
from storefront.services.size_guides import (
    SIZE_GUIDE_PRESETS, build_public_size_guide_blocks,
    resolve_product_size_context, resolve_product_size_guide,
)
from storefront.services.size_guide_authority import is_unverified_legacy_hoodie_seed


def _legacy_hoodie_seed_fixture():
    """Historical generator signature, never a newly approved size chart."""
    return {
        "profile_key": "hoodie", "eyebrow": "Hoodie fit guide",
        "aliases": ["hoodie", "hoodies", "худі", "худи", "fleece", "флис"],
        "columns": [{"key": "size", "label": "Розмір"},
                    {"key": "length", "label": "Довжина"},
                    {"key": "width", "label": "Ширина"}],
        "rows": [{"size": size, "length": length, "width": width} for size, length, width in [
            ("XS", "54", "52"), ("S", "58", "56"), ("M", "67", "60"),
            ("L", "70", "64"), ("XL", "73", "68"), ("XXL", "78", "72"),
        ]],
    }


class HoodieSizeGuideAuthorityTests(SimpleTestCase):
    def setUp(self):
        self.data = {
            "profile_key": "hoodie", "intro": "Garment measured laid flat, cm.",
            "columns": [{"key": "size", "label": "Size"},
                        {"key": "length", "label": "Garment length (cm)"},
                        {"key": "width", "label": "Half chest laid flat (cm)"}],
            "rows": [{"size": "M", "length": "99", "width": "88"}],
        }
        self.grid = SimpleNamespace(pk=701, name="Hoodie measured guide", description="",
                                    guide_data=deepcopy(self.data), image=None, catalog=None)
        self.product = SimpleNamespace(
            pk=702, title="Hoodie", category=SimpleNamespace(name="Hoodies", slug="hoodie"),
            catalog=None, catalog_id=None, size_grid=self.grid, size_grid_id=self.grid.pk,
        )

    def public(self):
        return build_public_size_guide_blocks([self.grid])["blocks"][0]

    def test_same_explicit_cells_reach_public_pdp_and_bot_without_replacement(self):
        pdp = resolve_product_size_guide(self.product)
        public = self.public()
        bot = size_measurements(self.grid, product_id=self.product.pk)
        for guide in (pdp, public, bot):
            self.assertEqual([(row["size"], row["length"], row["width"]) for row in guide["rows"]],
                             [("M", "99", "88")])
            self.assertEqual(guide["columns"][1 if guide is not bot else 0]["label"], "Garment length (cm)")
        self.assertEqual(bot["measurement_semantics"], "flat_garment")
        self.assertEqual(bot["unit"], "cm")
        self.assertNotIn("±", " ".join(str(note) for note in public["notes"]))

    def test_named_empty_hoodie_grid_never_manufactures_numeric_rows(self):
        self.grid.guide_data = {}
        for guide in (resolve_product_size_guide(self.product), self.public()):
            self.assertEqual(guide["rows"], [])
            self.assertEqual(guide["columns"], [])
            self.assertIn("підтверджених числових замірів", " ".join(str(note) for note in guide["notes"]))
        self.assertEqual(size_measurements(self.grid, product_id=self.product.pk), {})
        self.assertNotIn("rows", SIZE_GUIDE_PRESETS["hoodie"])

    def test_persisted_historical_seed_is_unverified_in_public_and_pdp(self):
        self.grid.guide_data = _legacy_hoodie_seed_fixture()
        self.assertTrue(is_unverified_legacy_hoodie_seed(self.grid.guide_data))
        for guide in (resolve_product_size_guide(self.product), self.public()):
            self.assertEqual(guide["measurement_authority"], "legacy_seed_unverified")
            self.assertEqual(guide["rows"], [])
            self.assertEqual(guide["sizes"], ["XS", "S", "M", "L", "XL", "XXL"])

    def test_legacy_signature_is_semantic_and_cannot_be_bypassed_by_review_flag(self):
        raw = _legacy_hoodie_seed_fixture()
        raw["rows"][-1].update(size="2XL", length="78.0", width="72,00")
        raw.update(title="An edited title", reviewed=True)
        self.assertTrue(is_unverified_legacy_hoodie_seed(raw))
        raw["rows"][2]["length"] = "99"
        self.assertFalse(is_unverified_legacy_hoodie_seed(raw))

    def test_legacy_recognizer_handles_malformed_metadata_without_exceptions(self):
        for value in [None, [], {"profile_key": "hoodie", "aliases": [{}]},
                      {**_legacy_hoodie_seed_fixture(), "aliases": [{}]}]:
            self.assertFalse(is_unverified_legacy_hoodie_seed(value))

    def test_no_grid_keeps_honest_generic_guidance(self):
        self.product.size_grid = None
        self.product.size_grid_id = None
        guide = resolve_product_size_guide(self.product)
        self.assertEqual(guide["source"], "fallback")
        self.assertEqual(guide["rows"], [])
        self.assertEqual(guide["guide_key"], "hoodie")

    def test_catalog_size_availability_is_independent_of_missing_measurements(self):
        self.grid.guide_data = {}
        option = SimpleNamespace(option_type="size", order=0, id=10, _prefetched_objects_cache={
            "values": [SimpleNamespace(value="S", display_name="S", order=0, id=11),
                       SimpleNamespace(value="2XL", display_name="2XL", order=1, id=12)],
        })
        self.product.catalog_id = 9
        self.product.catalog = SimpleNamespace(name="Hoodies", slug="hoodie",
                                              _prefetched_objects_cache={"options": [option]})
        context = resolve_product_size_context(self.product, "2XL")
        self.assertEqual(context["sizes"], ["S", "XXL"])
        self.assertEqual(context["selected_size"], "XXL")
        self.assertEqual(context["guide"]["rows"], [])

    def test_image_only_hoodie_grid_keeps_its_image_without_synthetic_table(self):
        self.grid.guide_data = {}
        self.grid.image = SimpleNamespace(url="/media/hoodie-chart.png", width=800, height=600)
        for guide in (resolve_product_size_guide(self.product), self.public()):
            self.assertEqual(guide["rows"], [])
            self.assertEqual(guide["image_url"], "/media/hoodie-chart.png")

    def test_malformed_or_incomplete_structure_does_not_fall_back_to_preset_cells(self):
        invalid = [[], {"rows": self.data["rows"]}, {"columns": self.data["columns"]},
                   {"columns": "size,length,width", "rows": self.data["rows"]},
                   {"columns": [{"key": "length", "label": "Length"}], "rows": self.data["rows"]},
                   {**self.data, "rows": self.data["rows"] * 2}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.grid.guide_data = payload
                for guide in (resolve_product_size_guide(self.product), self.public()):
                    self.assertEqual(guide["rows"], [])
                    self.assertEqual(guide["columns"], [])

    def test_missing_individual_measurement_stays_blank(self):
        self.grid.guide_data["rows"][0].pop("width")
        for guide in (resolve_product_size_guide(self.product), self.public()):
            self.assertEqual(guide["rows"][0]["length"], "99")
            self.assertEqual(guide["rows"][0]["width"], "")

    def test_tshirt_chart_cannot_be_used_as_hoodie_measurement_or_preview(self):
        self.grid.image = SimpleNamespace(url="/media/tshirt-chart.png", width=800, height=600)
        for raw_profile, name in [("classic_tshirt_fs101", "Hoodie measured guide"),
                                  ("", "Basic t-shirt guide")]:
            with self.subTest(profile=raw_profile, name=name):
                self.grid.name = name
                self.grid.catalog = SimpleNamespace(name="Hoodies", slug="hoodie")
                self.grid.guide_data["profile_key"] = raw_profile
                for guide in (resolve_product_size_guide(self.product), self.public()):
                    self.assertEqual(guide["rows"], [])
                    self.assertEqual(guide["image_url"], "")
                    self.assertIn("підтверджених числових замірів", " ".join(str(note) for note in guide["notes"]))

    def test_existing_tshirt_preset_is_preserved(self):
        self.grid.name = "Basic t-shirt guide"
        self.grid.guide_data = {}
        self.product.title = "T-shirt"
        self.product.category = SimpleNamespace(name="T-shirts", slug="tshirts")
        guide = resolve_product_size_guide(self.product)
        self.assertEqual(guide["guide_key"], "basic_tshirt")
        self.assertEqual(guide["rows"][0]["length"], "68,5")

    def test_public_template_renders_missing_chart_disclosure_without_empty_table(self):
        self.grid.guide_data = {}
        guide = self.public()
        html = render_to_string("pages/support_page.html", {"page": {},
                                "size_guide_blocks": {"blocks": [guide], "extra_visuals": []}})
        self.assertNotIn('class="support-table"', html)
        self.assertNotIn("Confirmed guides", html)
        self.assertIn("підтверджених числових замірів", html)


class TshirtAdvisorApplicabilityTests(SimpleTestCase):
    def test_explicit_garment_category_outranks_catalog_and_title(self):
        from storefront.views.product import _is_tshirt_product

        for slug in ["hoodie", "long-sleeve", "pants", "accessories", "custom-print"]:
            with self.subTest(category=slug):
                product = SimpleNamespace(title="T-shirt print", category=SimpleNamespace(slug=slug, name=slug),
                                          catalog=SimpleNamespace(name="Basic T-Shirts", slug="basic-tshirts"))
                self.assertFalse(_is_tshirt_product(product))

    def test_unknown_or_absent_category_preserves_legacy_tshirt_fallback(self):
        from storefront.views.product import _is_tshirt_product

        for category in [None, SimpleNamespace(slug="legacy", name="Archive")]:
            product = SimpleNamespace(title="Футболка", category=category, catalog=None)
            self.assertTrue(_is_tshirt_product(product))
        product.category = SimpleNamespace(slug="tshirts", name="New category label")
        product.title = "Print without garment words"
        self.assertTrue(_is_tshirt_product(product))


class TshirtAdvisorPdpApplicabilityTests(TestCase):
    def setUp(self):
        for target in ["storefront.signals.generate_google_merchant_feed_task.apply_async",
                       "storefront.signals.enqueue_indexnow_urls"]:
            self.enterContext(patch(target, return_value=None))
        self.enterContext(patch("storefront.views.product.ProductRecommendationEngine.get_recommendations", return_value=[]))
        self.enterContext(patch("storefront.views.product.get_detailed_color_variants", return_value=[]))

    def pdp(self, category_slug, fit):
        from product_catalog.models import ProductOptionSizeGrid
        from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid

        category = Category.objects.create(name=category_slug, slug=category_slug)
        catalog = Catalog.objects.create(name="Basic T-Shirts", slug="shared-tshirts")
        product = Product.objects.create(title="T-shirt print on garment", slug="advisor-applicability",
                                         category=category, catalog=catalog, price=1000, status="published")
        ProductFitOption.objects.create(product=product, code=fit, label=fit, is_active=True, is_default=True)
        grid = SizeGrid.objects.create(name="Explicit measured grid", catalog=catalog, guide_data={
            "profile_key": "hoodie" if category_slug == "hoodie" else "basic_tshirt",
            "columns": [{"key": "size", "label": "Size"}, {"key": "length", "label": "Length (cm)"}],
            "rows": [{"size": "M", "length": "99"}],
        })
        ProductOptionSizeGrid.objects.create(product=product, option_key=f"fit={fit}", size_grid=grid)
        return self.client.get(reverse("product", kwargs={"slug": product.slug}), secure=True)

    def test_hoodie_shared_tshirt_catalog_never_renders_tshirt_calculator(self):
        response = self.pdp("hoodie", "oversize")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["size_grid_comparison"])
        self.assertFalse(response.context["size_advisor_enabled"])
        self.assertNotContains(response, "data-size-advisor-form")

    def test_normal_tshirt_keeps_its_calculator(self):
        response = self.pdp("tshirts", "classic")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["size_grid_comparison"])
        self.assertTrue(response.context["size_advisor_enabled"])
        self.assertContains(response, "data-size-advisor-form")
