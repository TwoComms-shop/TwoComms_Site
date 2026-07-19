import unittest

import django
from django.apps import apps
from django.test import override_settings
from django.utils import translation

if not apps.ready:
    try:
        django.setup()
    except Exception:
        # The standalone source-contract invocation may not configure Django;
        # the project test runner does so before these tests execute.
        pass

from storefront.custom_print_config import (
    ISO_SIZES,
    build_custom_print_config,
    build_placement_specs,
    normalize_custom_print_snapshot,
    resolve_color_label,
    resolve_fabric_badge,
    resolve_fabric_label,
    resolve_fit_label,
    resolve_lead_display_labels,
)


class CustomPrintLabelResolverTests(unittest.TestCase):
    """CP-UX-2026-05-18: резолвери для адмінки і Telegram-уведомлень."""

    def test_resolve_color_label_returns_label_and_hex_for_hoodie(self):
        result = resolve_color_label("hoodie", "black", "")
        self.assertEqual(result, {"label": "Чорний", "hex": "#151515"})

    def test_resolve_color_label_returns_graphite(self):
        result = resolve_color_label("hoodie", "graphite", "")
        self.assertEqual(result["label"], "Графіт")
        self.assertEqual(result["hex"], "#3b3b3f")

    def test_resolve_color_label_for_thermo_tshirt_uses_thermo_palette(self):
        result = resolve_color_label("tshirt", "thermo_green", "thermo")
        self.assertEqual(result["label"], "Зелений (Термо)")

    def test_resolve_color_label_unknown_value_returns_raw(self):
        result = resolve_color_label("hoodie", "unknown_slug", "")
        self.assertEqual(result, {"label": "unknown_slug", "hex": ""})

    def test_resolve_color_label_empty_returns_empty(self):
        self.assertEqual(resolve_color_label("hoodie", "", ""), {"label": "", "hex": ""})

    def test_resolve_fabric_label_premium_hoodie(self):
        self.assertEqual(resolve_fabric_label("hoodie", "regular", "premium"), "Преміум")

    def test_resolve_fabric_label_thermo_tshirt(self):
        self.assertEqual(resolve_fabric_label("tshirt", "oversize", "thermo"), "Термохромна тканина")

    def test_resolve_fabric_label_falls_back_to_static_dict(self):
        # Тип тканини, якого немає в матриці продукту, повинен резолвитися
        # через FABRIC_LABELS (статичний словник).
        result = resolve_fabric_label("longsleeve", "", "premium")
        self.assertEqual(result, "Преміум")

    def test_resolve_fit_label_hoodie_regular(self):
        self.assertEqual(resolve_fit_label("hoodie", "regular"), "Класичний")

    def test_resolve_fit_label_hoodie_oversize(self):
        self.assertEqual(resolve_fit_label("hoodie", "oversize"), "Оверсайз")

    def test_resolve_fabric_badge_premium_has_emoji_and_note(self):
        badge = resolve_fabric_badge("premium")
        self.assertEqual(badge.get("emoji"), "💎")
        self.assertIn("г/м²", badge.get("note", ""))

    def test_resolve_fabric_badge_thermo(self):
        badge = resolve_fabric_badge("thermo")
        self.assertEqual(badge.get("emoji"), "🌡")

    def test_resolve_fabric_badge_unknown_returns_empty_dict(self):
        self.assertEqual(resolve_fabric_badge("alien_fabric"), {})

    def test_resolve_lead_display_labels_returns_resolved_strings(self):
        class _Lead:
            product_type = "hoodie"
            fit = "oversize"
            fabric = "premium"
            color_choice = "graphite"

        labels = resolve_lead_display_labels(_Lead())
        self.assertEqual(labels["fit_label"], "Оверсайз")
        self.assertEqual(labels["fabric_label"], "Преміум")
        self.assertEqual(labels["color_label"], "Графіт")
        self.assertEqual(labels["color_hex"], "#3b3b3f")


class CustomPrintConfigContractTests(unittest.TestCase):
    @override_settings(LANGUAGE_CODE="ru")
    def test_product_ui_copy_is_localized_for_russian_runtime(self):
        with translation.override("ru"):
            config = build_custom_print_config(submit_url="x", safe_exit_url="x")
        tshirt = config["products"]["tshirt"]
        self.assertEqual(tshirt["fits"][0]["label"], "Классическая")
        self.assertEqual(tshirt["colors"][2]["label"], "Койот")
        self.assertEqual(tshirt["fabrics"]["oversize"][1]["label"], "Термохромная ткань")

    @override_settings(LANGUAGE_CODE="en")
    def test_product_ui_copy_is_localized_for_english_runtime(self):
        with translation.override("en"):
            config = build_custom_print_config(submit_url="x", safe_exit_url="x")
        hoodie = config["products"]["hoodie"]
        self.assertEqual(hoodie["fits"][0]["label"], "Classic")
        self.assertEqual(hoodie["fit_colors"]["oversize"][1]["label"], "Pink")

    def test_config_exposes_progress_steps_tshirt_rules_and_zone_presets(self):
        config = build_custom_print_config(
            submit_url="https://twocomms.shop/custom-print/lead/",
            safe_exit_url="https://twocomms.shop/custom-print/safe-exit/",
            add_to_cart_url="https://twocomms.shop/custom-print/add-to-cart/",
        )

        self.assertEqual(
            [item["value"] for item in config["progress_steps"]],
            ["format", "garment", "config", "placement", "artwork", "quantity", "gift", "contact"],
        )
        self.assertEqual(
            [item["value"] for item in config["front_size_presets"]],
            ["A6", "A5", "A4"],
        )
        self.assertEqual(
            [item["value"] for item in config["back_size_presets"]],
            ["A4", "A3", "A2"],
        )
        self.assertEqual(
            [item["value"] for item in config["sleeve_mode_options"]],
            ["a6", "full_text"],
        )
        self.assertEqual(config["front_size_default"], "A4")
        self.assertEqual(config["back_size_default"], "A4")
        self.assertEqual(config["artwork_services"][1]["price_delta"], 100)
        self.assertIn("почистити чи адаптувати", config["artwork_services"][1]["hint"])
        self.assertEqual(config["artwork_services"][2]["price_delta"], 300)
        self.assertEqual(config["products"]["hoodie"]["add_ons"][0]["price_delta"], 150)
        self.assertEqual(
            [item["value"] for item in config["products"]["tshirt"]["fits"]],
            ["regular", "oversize"],
        )
        self.assertEqual(
            [item["value"] for item in config["products"]["tshirt"]["fabrics"]["regular"]],
            ["standard", "premium"],
        )
        self.assertEqual(
            [item["value"] for item in config["products"]["tshirt"]["fabrics"]["oversize"]],
            ["premium", "thermo"],
        )
        self.assertEqual(config["products"]["tshirt"]["fabrics"]["oversize"][1]["price_delta"], 500)
        self.assertIn("stage_profiles", config)
        self.assertIn("hoodie", config["stage_profiles"])
        self.assertIn("regular", config["stage_profiles"]["hoodie"])
        self.assertIn("oversize", config["stage_profiles"]["hoodie"])

    def test_config_exposes_guided_studio_preview_contract(self):
        config = build_custom_print_config(
            submit_url="https://twocomms.shop/custom-print/lead/",
            safe_exit_url="https://twocomms.shop/custom-print/safe-exit/",
            add_to_cart_url="https://twocomms.shop/custom-print/add-to-cart/",
        )

        self.assertEqual(
            config["format_dimensions"],
            {
                "A6": {"width_mm": 105, "height_mm": 148},
                "A5": {"width_mm": 148, "height_mm": 210},
                "A4": {"width_mm": 210, "height_mm": 297},
                "A3": {"width_mm": 297, "height_mm": 420},
                "A2": {"width_mm": 420, "height_mm": 594},
            },
        )
        self.assertEqual(ISO_SIZES["A4"], (210, 297))
        self.assertIn("preview_assets", config)
        self.assertIn("preview_calibration", config)
        self.assertIn("ui_strings", config)
        self.assertIn("artwork_file_required", config["ui_strings"])

        expected_profiles = {
            "hoodie:regular",
            "hoodie:oversize",
            "tshirt:regular",
            "tshirt:oversize",
            "longsleeve:regular",
        }
        self.assertEqual(set(config["preview_assets"]), expected_profiles)
        for key, profile in config["preview_assets"].items():
            self.assertTrue(profile["front"].endswith(".png"), key)
            self.assertTrue(profile["back"].endswith(".png"), key)
            self.assertGreater(config["preview_calibration"][key]["garment_width_mm"], 0)
            self.assertIn("body", config["preview_calibration"][key]["zones"])
            self.assertTrue(config["preview_calibration"][key]["allowed_zones"])

    def test_config_exposes_clean_hoodie_fabric_labels_and_premium_info(self):
        config = build_custom_print_config(
            submit_url="https://twocomms.shop/custom-print/lead/",
            safe_exit_url="https://twocomms.shop/custom-print/safe-exit/",
            add_to_cart_url="https://twocomms.shop/custom-print/add-to-cart/",
        )

        regular_fabrics = config["products"]["hoodie"]["fabrics"]["regular"]
        oversize_fabrics = config["products"]["hoodie"]["fabrics"]["oversize"]
        self.assertEqual(
            [item["value"] for item in regular_fabrics],
            ["standard", "premium"],
        )
        self.assertEqual(
            [item["value"] for item in oversize_fabrics],
            ["premium"],
        )
        self.assertEqual(oversize_fabrics[0]["label"], "Преміум")
        self.assertEqual(oversize_fabrics[0]["price_delta"], 0)
        self.assertTrue(oversize_fabrics[0]["included_in_base"])
        self.assertEqual(oversize_fabrics[0]["info_theme"], "premium")
        self.assertIn("вищу щільність", oversize_fabrics[0]["info_desc"])

    def test_config_exposes_product_specific_color_matrices(self):
        config = build_custom_print_config(
            submit_url="/lead/",
            safe_exit_url="/safe-exit/",
            add_to_cart_url="/cart/",
        )
        tshirt_colors = config["products"]["tshirt"]["colors"]
        self.assertEqual([color["value"] for color in tshirt_colors], ["black", "white", "coyote"])
        self.assertEqual(next(color["hex"] for color in tshirt_colors if color["value"] == "coyote"), "#8B6B45")
        self.assertNotIn("graphite", {color["value"] for color in tshirt_colors})
        hoodie_fit_colors = config["products"]["hoodie"]["fit_colors"]
        self.assertEqual([color["value"] for color in hoodie_fit_colors["oversize"]], ["black", "pink"])
        self.assertEqual([color["value"] for color in hoodie_fit_colors["regular"]], ["black", "graphite", "sand", "bone"])
        thermo = config["products"]["tshirt"]["fabrics"]["oversize"][-1]
        self.assertEqual([color["value"] for color in thermo["colors"]], ["thermo_green", "thermo_pink"])

    def test_config_exposes_clear_classic_premium_and_thermo_descriptions(self):
        config = build_custom_print_config(submit_url="/lead/", safe_exit_url="/safe-exit/", add_to_cart_url="/cart/")
        tshirt = config["products"]["tshirt"]["fabrics"]
        self.assertIn("без пеньє-обробки", tshirt["regular"][0]["short_desc"])
        self.assertIn("турецький кулір", tshirt["regular"][1]["short_desc"].lower())
        self.assertIn("ребана", tshirt["oversize"][0]["short_desc"])
        self.assertIn("від тепла", tshirt["oversize"][1]["short_desc"])
        self.assertEqual(
            tshirt["oversize"][1]["preview_image"],
            "/static/img/configurator/ui/thermo-preview.png",
        )

    def test_artwork_services_separate_ready_adjustment_and_new_design(self):
        config = build_custom_print_config(submit_url="/lead/", safe_exit_url="/safe-exit/", add_to_cart_url="/cart/")
        ready, adjust, design = config["artwork_services"]
        self.assertIn("PNG", ready["hint"])
        self.assertIn("прозор", ready["hint"].lower())
        self.assertIn("менеджер", ready["hint"].lower())
        self.assertIn("чорне або біле тло", adjust["hint"].lower())
        self.assertIn("напівпрозорі пікселі", adjust["hint"].lower())
        self.assertNotIn("референс", adjust["hint"].lower())
        self.assertIn("референс", design["hint"].lower())
        self.assertIn("з нуля", design["hint"].lower())

    def test_stage_profiles_expose_distinct_back_presets_for_a4_a3_a2(self):
        config = build_custom_print_config(
            submit_url="https://twocomms.shop/custom-print/lead/",
            safe_exit_url="https://twocomms.shop/custom-print/safe-exit/",
            add_to_cart_url="https://twocomms.shop/custom-print/add-to-cart/",
        )

        back_presets = (
            config["stage_profiles"]["hoodie"]["regular"]["back"]["anchors"]["back"]["presets"]
        )
        a4 = back_presets["A4"]
        a3 = back_presets["A3"]
        a2 = back_presets["A2"]

        self.assertLess(a4["width"], a3["width"])
        self.assertLess(a3["width"], a2["width"])
        self.assertLess(a4["height"], a3["height"])
        self.assertLess(a3["height"], a2["height"])
        self.assertGreater(a2["y"], a3["y"])

    def test_normalize_snapshot_preserves_zone_sizes_sleeves_and_legacy_lacing(self):
        normalized = normalize_custom_print_snapshot(
            {
                "product": {
                    "type": "hoodie",
                    "fit": "oversize",
                    "fabric": "premium",
                    "color": "graphite",
                },
                "print": {
                    "zones": ["front", "back", "sleeve"],
                    "add_ons": ["grommets"],
                    "zone_options": {
                        "front": {
                            "size_preset": "A4",
                        },
                        "back": {
                            "size_preset": "A2",
                        },
                        "sleeve": {
                            "left_enabled": True,
                            "right_enabled": True,
                            "left_mode": "full_text",
                            "left_text": "TWOCOMMS",
                            "right_mode": "a6",
                        },
                    },
                },
            }
        )

        self.assertEqual(normalized["product"]["fit"], "oversize")
        self.assertEqual(normalized["product"]["fabric"], "premium")
        self.assertEqual(normalized["print"]["add_ons"], ["lacing"])
        self.assertEqual(normalized["print"]["zone_options"]["front"]["size_preset"], "A4")
        self.assertEqual(normalized["print"]["zone_options"]["back"]["size_preset"], "A2")
        self.assertTrue(normalized["print"]["zone_options"]["sleeve"]["left_enabled"])
        self.assertTrue(normalized["print"]["zone_options"]["sleeve"]["right_enabled"])
        self.assertEqual(normalized["print"]["zone_options"]["sleeve"]["left_mode"], "full_text")
        self.assertEqual(normalized["print"]["zone_options"]["sleeve"]["left_text"], "TWOCOMMS")

    def test_normalize_snapshot_defaults_front_size_when_front_is_selected(self):
        normalized = normalize_custom_print_snapshot(
            {
                "product": {
                    "type": "tshirt",
                    "color": "black",
                },
                "print": {
                    "zones": ["front"],
                },
            }
        )

        self.assertEqual(normalized["print"]["zone_options"]["front"]["size_preset"], "A4")

    def test_normalize_snapshot_preserves_fit_specific_hoodie_color(self):
        normalized = normalize_custom_print_snapshot(
            {"product": {"type": "hoodie", "fit": "oversize", "fabric": "premium", "color": "pink"}}
        )

        self.assertEqual(normalized["product"]["color"], "pink")

    def test_normalize_snapshot_preserves_fabric_specific_thermo_color(self):
        normalized = normalize_custom_print_snapshot(
            {
                "product": {
                    "type": "tshirt",
                    "fit": "oversize",
                    "fabric": "thermo",
                    "color": "thermo_pink",
                }
            }
        )

        self.assertEqual(normalized["product"]["color"], "thermo_pink")

    def test_build_placement_specs_expand_back_and_both_sleeves(self):
        specs = build_placement_specs(
            {
                "print": {
                    "zones": ["front", "back", "sleeve"],
                    "zone_options": {
                        "front": {
                            "size_preset": "A5",
                        },
                        "back": {
                            "size_preset": "A2",
                        },
                        "sleeve": {
                            "left_enabled": True,
                            "right_enabled": True,
                            "left_mode": "full_text",
                            "left_text": "LEFT TEXT",
                            "right_mode": "a6",
                        },
                    },
                }
            }
        )

        self.assertEqual(specs[0]["zone"], "front")
        self.assertEqual(specs[0]["size_preset"], "A5")
        self.assertTrue(specs[0]["requires_artwork_file"])
        self.assertEqual(specs[0]["file_index"], 0)
        self.assertEqual(specs[1]["zone"], "back")
        self.assertEqual(specs[1]["size_preset"], "A2")
        self.assertTrue(specs[1]["requires_artwork_file"])
        self.assertEqual(specs[1]["file_index"], 1)
        self.assertEqual(specs[2]["placement_key"], "sleeve_left")
        self.assertEqual(specs[2]["mode"], "full_text")
        self.assertEqual(specs[2]["text"], "LEFT TEXT")
        self.assertFalse(specs[2]["requires_artwork_file"])
        self.assertNotIn("file_index", specs[2])
        self.assertEqual(specs[3]["placement_key"], "sleeve_right")
        self.assertEqual(specs[3]["mode"], "a6")
        self.assertTrue(specs[3]["requires_artwork_file"])
        self.assertEqual(specs[3]["file_index"], 2)

    def test_build_placement_specs_text_only_sleeve_does_not_require_file(self):
        specs = build_placement_specs(
            {
                "print": {
                    "zones": ["sleeve"],
                    "zone_options": {
                        "sleeve": {
                            "left_enabled": True,
                            "right_enabled": False,
                            "left_mode": "full_text",
                            "left_text": "TWOCOMMS",
                        },
                    },
                }
            }
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["placement_key"], "sleeve_left")
        self.assertFalse(specs[0]["requires_artwork_file"])
        self.assertNotIn("file_index", specs[0])


if __name__ == "__main__":
    unittest.main()
