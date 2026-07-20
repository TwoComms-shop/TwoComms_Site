import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "twocomms/storefront/custom_print_config.py"
CONFIGURATOR_PATH = REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js"


def load_config_module():
    storefront = types.ModuleType("storefront")
    storefront.__path__ = [str(REPO_ROOT / "twocomms/storefront")]
    django = types.ModuleType("django")
    django_utils = types.ModuleType("django.utils")
    django_translation = types.ModuleType("django.utils.translation")
    django_translation.gettext_lazy = lambda value: value
    modules = {
        "storefront": storefront,
        "django": django,
        "django.utils": django_utils,
        "django.utils.translation": django_translation,
    }
    managed_names = [*modules, "storefront.custom_print_stage_art", "storefront.custom_print_config"]
    previous = {name: sys.modules.get(name) for name in managed_names}
    sys.modules.update(modules)
    try:
        stage_path = REPO_ROOT / "twocomms/storefront/custom_print_stage_art.py"
        stage_spec = importlib.util.spec_from_file_location("storefront.custom_print_stage_art", stage_path)
        stage_module = importlib.util.module_from_spec(stage_spec)
        sys.modules["storefront.custom_print_stage_art"] = stage_module
        stage_spec.loader.exec_module(stage_module)
        spec = importlib.util.spec_from_file_location("storefront.custom_print_config", CONFIG_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["storefront.custom_print_config"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class CustomPrintPricingSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config_module()

    def test_print_formats_have_customer_visible_price_deltas(self):
        prices = {
            item["value"]: item["price_delta"]
            for item in self.config.FRONT_SIZE_PRESETS + self.config.BACK_SIZE_PRESETS
        }
        self.assertEqual(prices["A6"], 40)
        self.assertEqual(prices["A4"], 60)
        self.assertEqual(prices["A3"], 80)
        self.assertEqual(prices["A2"], 110)
        self.assertTrue(all(item.get("range_label") for item in self.config.FRONT_SIZE_PRESETS))
        self.assertTrue(all(item.get("range_label") for item in self.config.BACK_SIZE_PRESETS))

    def test_tshirt_oversize_uses_premium_base_and_plus_200_fit(self):
        tshirt = self.config.PRODUCT_MATRIX["tshirt"]
        self.assertEqual(tshirt["pricing"]["oversize_delta"], 200)
        self.assertEqual([item["value"] for item in tshirt["fabrics"]["oversize"]], ["standard", "premium", "thermo"])
        self.assertFalse(tshirt["fabrics"]["oversize"][0]["available"])
        self.assertTrue(tshirt["fabrics"]["oversize"][1]["included_in_base"])
        self.assertEqual(tshirt["fabrics"]["oversize"][1]["price_delta"], 0)
        self.assertEqual(tshirt["fabrics"]["oversize"][2]["label"], "Термохромна тканина")
        self.assertEqual([item["value"] for item in tshirt["fabrics"]["regular"]], ["standard", "premium"])
        self.assertEqual(tshirt["fabrics"]["regular"][0]["label"], "Звичайна тканина")
        self.assertEqual(tshirt["fabrics"]["regular"][1]["price_delta"], 150)
        self.assertEqual(tshirt["default_fabric"], "standard")

    def test_print_format_price_is_included_in_unit_total(self):
        source = CONFIGURATOR_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "let unitTotal = base + printPrice + zonesPrice + designPrice + addonsPrice;",
            source,
        )


if __name__ == "__main__":
    unittest.main()
