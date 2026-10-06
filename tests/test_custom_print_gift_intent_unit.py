import json
import os
import unittest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twocomms.settings")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import django

django.setup()

from django.test import override_settings
from django.utils import translation
from django.utils.functional import Promise

from storefront.custom_print_config import (
    CLIENT_MODES,
    ZONE_LABELS,
    build_placement_specs,
    compute_cart_label,
    normalize_custom_print_snapshot,
)


class CustomPrintGiftIntentUnitTests(unittest.TestCase):
    def test_configurator_has_personal_gift_and_organization_choices(self):
        self.assertEqual([choice["value"] for choice in CLIENT_MODES], ["personal", "gift", "brand"])

    def test_legacy_mode_derives_purpose_without_enabling_packaging(self):
        for mode, purpose in (("personal", "personal"), ("brand", "organization")):
            with self.subTest(mode=mode):
                snapshot = normalize_custom_print_snapshot({"mode": mode})
                self.assertEqual(snapshot["mode"], mode)
                self.assertEqual(snapshot["order_purpose"], purpose)
                self.assertFalse(snapshot["order"]["gift"])

    def test_invalid_purpose_falls_back_to_legacy_mode(self):
        for raw_purpose in (None, "", "unknown", {"gift": True}):
            for mode, purpose in (("personal", "personal"), ("brand", "organization")):
                with self.subTest(raw_purpose=raw_purpose, mode=mode):
                    snapshot = normalize_custom_print_snapshot({"mode": mode, "order_purpose": raw_purpose})
                    self.assertEqual(snapshot["order_purpose"], purpose)

    def test_gift_intent_preserves_explicit_packaging_choice(self):
        for gift_payload in (True, False, {"enabled": True}, {"enabled": False}):
            with self.subTest(gift_payload=gift_payload):
                snapshot = normalize_custom_print_snapshot({
                    "mode": "personal",
                    "order_purpose": "gift",
                    "order": {"gift": gift_payload},
                })
                self.assertEqual(snapshot["mode"], "personal")
                self.assertEqual(snapshot["order_purpose"], "gift")
                expected = gift_payload.get("enabled") if isinstance(gift_payload, dict) else gift_payload
                self.assertEqual(snapshot["order"]["gift"], expected)

    def test_gift_intent_does_not_force_packaging_when_omitted(self):
        snapshot = normalize_custom_print_snapshot({"mode": "personal", "order_purpose": "gift"})

        self.assertEqual(snapshot["order_purpose"], "gift")
        self.assertFalse(snapshot["order"]["gift"])

    def test_personal_order_can_enable_packaging_without_changing_purpose(self):
        snapshot = normalize_custom_print_snapshot({"mode": "personal", "order": {"gift": True}})

        self.assertEqual(snapshot["order_purpose"], "personal")
        self.assertTrue(snapshot["order"]["gift"])

    def test_placement_modes_do_not_overwrite_legacy_order_mode(self):
        for mode, purpose in (("personal", "gift"), ("brand", "organization")):
            for zone, options, normalized_key, expected in (
                ("hem", {"side": "front", "mode": "text", "text": "Gift"}, "mode", "text"),
                ("sleeve", {"left_enabled": True, "left_mode": "full_text"}, "left_mode", "full_text"),
            ):
                with self.subTest(mode=mode, zone=zone):
                    snapshot = normalize_custom_print_snapshot({
                        "mode": mode,
                        "order_purpose": purpose,
                        "product": {"type": "tshirt" if zone == "hem" else "hoodie"},
                        "print": {"zones": [zone], "zone_options": {zone: options}},
                    })
                    self.assertEqual(snapshot["print"]["zone_options"][zone][normalized_key], expected)
                    self.assertEqual(snapshot["mode"], mode)
                    self.assertEqual(snapshot["order_purpose"], purpose)

    def test_normalized_gift_draft_round_trip_preserves_disabled_packaging_and_text(self):
        first = normalize_custom_print_snapshot({
            "mode": "personal",
            "order_purpose": " gift ",
            "order": {"gift": {"enabled": False, "text": " Зі святом! "}},
        })
        second = normalize_custom_print_snapshot(first)

        self.assertEqual(second["order_purpose"], "gift")
        self.assertEqual(second["mode"], "personal")
        self.assertFalse(second["order"]["gift"])
        self.assertEqual(second["order"]["gift_text"], "Зі святом!")

    @override_settings(USE_I18N=True)
    def test_cart_label_materializes_lazy_labels_before_joining(self):
        self.assertIsInstance(ZONE_LABELS["front"], Promise)
        with translation.override("uk"):
            label = compute_cart_label({
                "mode": "personal",
                "order_purpose": "gift",
                "product": {"type": "hoodie"},
                "print": {"zones": ["front", "sleeve"]},
            })

        self.assertIsInstance(label, str)
        self.assertEqual(label, "Кастом · Худі · Спереду, На рукавах")

    @override_settings(USE_I18N=True)
    def test_gift_placement_specs_are_json_serializable_with_lazy_labels(self):
        self.assertIsInstance(ZONE_LABELS["front"], Promise)
        with translation.override("uk"):
            snapshot = normalize_custom_print_snapshot({
                "mode": "personal",
                "order_purpose": "gift",
                "print": {
                    "zones": ["front", "custom", "sleeve"],
                    "zone_options": {"sleeve": {"left_enabled": True, "left_mode": "full_text"}},
                },
                "order": {"gift": False},
            })
            specs = build_placement_specs(snapshot)
            stored = json.loads(json.dumps({"snapshot": snapshot, "placement_specs": specs}))

        self.assertEqual([spec["label"] for spec in stored["placement_specs"]], [
            "Спереду", "Інша зона", "Лівий рукав",
        ])
        self.assertEqual(stored["snapshot"]["order_purpose"], "gift")
        self.assertFalse(stored["snapshot"]["order"]["gift"])


if __name__ == "__main__":
    unittest.main()
