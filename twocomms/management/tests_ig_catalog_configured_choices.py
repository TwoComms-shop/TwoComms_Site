"""Configured choice capture must not borrow stock or unrelated grid truth."""
from types import SimpleNamespace as NS
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase

from management.services import ig_catalog_configured_choices as capture


def owner(**values):
    return NS(_prefetched_objects_cache={}, **values)


def grid(pk, sizes, key=""):
    return NS(pk=pk, id=pk, order=0, name=f"Grid {pk}", is_active=True,
              product_catalog_profile=NS(is_active=True, option_key=key), image=None,
              guide_data={"columns": [{"key": "size", "label": "Розмір"},
                                       {"key": "width", "label": "Ширина"}],
                          "rows": [{"size": size, "width": "50"}
                                   for size in sizes]})


class ConfiguredChoiceCaptureTests(SimpleTestCase):
    def setUp(self):
        self.product = owner(pk=1, size_grid=None, size_grid_id=None, catalog=None,
                             main_image=None, home_card_image=None)
        self.variant = owner(pk=10, color=NS(name="Чорна"), stock=0)
        self.key = "fit=classic;lining=fleece"

    def test_full_option_variant_grid_and_enabled_zero_stock_size_are_exact(self):
        shared = grid(3, ["S", "M", "L"])
        exact = grid(4, ["M", "L"])
        self.product._prefetched_objects_cache = {
            "product_catalog_size_grid_assignments": [NS(option_key=self.key, size_grid=shared)],
            "product_catalog_size_rules": []}
        self.variant._prefetched_objects_cache = {
            "product_catalog_size_grid_assignments": [NS(option_key=self.key, size_grid=exact)],
            "product_catalog_size_rules": [NS(pk=9, fit_code="classic", size="M",
                                              is_enabled=True, stock=0),
                                           NS(pk=8, fit_code="classic", size="L",
                                              is_enabled=False, stock=10)]}
        row = capture._configuration(self.product, self.variant,
                                     {"fit": "classic", "lining": "fleece"})
        self.assertEqual(row["option_key"], self.key)
        self.assertEqual(row["grid"]["grid_id"], 4)
        self.assertEqual(row["grid"]["source"], "variant_assignment")
        self.assertEqual(row["sizes"], [{"value": "M", "grid_id": 4,
                                        "product_rule_id": None, "variant_rule_id": 9}])
        digest = row["configuration_digest"]
        self.variant._prefetched_objects_cache["product_catalog_size_rules"][0].stock = 50
        self.assertEqual(capture._configuration(self.product, self.variant,
            {"fit": "classic", "lining": "fleece"})["configuration_digest"], digest)

    def test_direct_product_grid_is_used_before_catalog_grid(self):
        self.product.size_grid = grid(2, ["M"])
        self.product.size_grid_id = 2
        self.product.catalog = owner()
        self.product.catalog._prefetched_objects_cache["size_grids"] = [grid(3, ["S"])]
        row = capture._configuration(self.product, self.variant, {})
        self.assertEqual(row["grid"]["grid_id"], 2)
        self.assertEqual([size["value"] for size in row["sizes"]], ["M"])

    def test_unrelated_fit_grid_is_never_substituted(self):
        self.product.catalog = owner()
        self.product.catalog._prefetched_objects_cache["size_grids"] = [
            grid(3, ["S"], "fit=oversize")]
        row = capture._configuration(self.product, self.variant, {"fit": "classic"})
        self.assertIsNone(row["grid"]["grid_id"])
        self.assertIsNone(row["grid"]["photo_ref"])
        self.assertEqual(row["sizes"], [])

    @patch("product_catalog.services.product_option_context")
    def test_color_choices_include_zero_stock_and_ignore_context_defaults(self, context):
        context.return_value = {"axes": [{"code": "fit", "choices": [
            {"code": "classic", "is_enabled": True, "is_default": True},
            {"code": "oversize", "is_enabled": True}]}]}
        rows, colors = capture._capture_product(self.product, [self.variant], {}, None)
        self.assertEqual([row["fit_code"] for row in rows], ["classic", "oversize"])
        self.assertEqual(colors[0]["variant_id"], 10)
        self.assertEqual(colors[0]["canonical_name"], "Чорна")
        self.assertNotIn("stock", colors[0])

    @patch("product_catalog.services.product_option_context")
    def test_unknown_option_or_foreign_variant_produces_no_guessed_choice(self, context):
        context.return_value = {"axes": []}
        self.assertEqual(capture._capture_product(self.product, [self.variant],
                          {"lining": "fleece"}, 10), ([], []))
        self.assertEqual(capture._capture_product(self.product, [self.variant], {}, 99), ([], []))

    @patch("product_catalog.services.product_option_context")
    def test_inactive_exact_multi_axis_combination_is_excluded(self, context):
        context.return_value = {"axes": [
            {"code": "fit", "choices": [{"code": "classic", "is_enabled": True}]},
            {"code": "lining", "choices": [{"code": "fleece", "is_enabled": True}]}]}
        self.variant._prefetched_objects_cache["product_catalog_combinations"] = [
            NS(combination_key=self.key, is_active=False)]
        self.assertEqual(capture._capture_product(self.product, [self.variant], {}, 10), ([], []))

    def test_photo_metadata_never_uses_remote_storage(self):
        class Remote:
            @property
            def path(self):
                raise AssertionError("remote storage must never be invoked")
        self.assertIsNone(capture._photo_ref(NS(storage=Remote()), product_id=1))

    def test_public_photo_requires_existing_file_with_real_metadata(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "photo.jpg"
            path.write_bytes(b"real-file")
            field = NS(storage=FileSystemStorage(location=directory), path=str(path),
                       name="photo.jpg", url="https://twocomms.shop/media/photo.jpg")
            photo = capture._photo_ref(field, product_id=1, variant_id=10, source="variant")
            self.assertEqual(photo["size_bytes"], 9)
            self.assertEqual(photo["variant_id"], 10)
            field.url = "https://foreign.example/photo.jpg"
            self.assertIsNone(capture._photo_ref(field, product_id=1))
            field.url = "https://twocomms.shop/media/photo.jpg"
            path.unlink()
            self.assertIsNone(capture._photo_ref(field, product_id=1))

    def test_conflicting_fit_selector_fails_before_catalog_lookup(self):
        result = capture.capture_configured_choices(1, option_values={"fit": "classic"},
                                                     fit_code="oversize")
        self.assertEqual(result["error"], "conflicting_fit_selector")
        self.assertEqual(result["sizes"], [])

    def test_invalid_ids_are_not_coerced_to_other_products(self):
        for value in (True, 1.9, -1, "1.9", " 1", {}):
            with self.subTest(value=value):
                self.assertEqual(capture.capture_configured_choices(value)["error"],
                                 "invalid_selector")

    @patch("management.services.ig_catalog_configured_choices._photo_ref")
    def test_metadata_only_configuration_has_same_digest_without_stat(self, photo):
        self.product.size_grid = grid(2, ["M"])
        self.product.size_grid_id = 2
        photo.return_value = {"url": "https://twocomms.shop/media/grid.png", "size_bytes": 50}
        physical = capture._configuration(self.product, self.variant, {})
        photo.reset_mock()
        metadata = capture._configuration(self.product, self.variant, {}, metadata_only=True)
        photo.assert_not_called()
        self.assertEqual(physical["configuration_digest"], metadata["configuration_digest"])
        self.assertIsNone(metadata["grid"]["photo_ref"])
