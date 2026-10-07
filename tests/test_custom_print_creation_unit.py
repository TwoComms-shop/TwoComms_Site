import json
import os
import unittest
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twocomms.settings")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
import django
django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.datastructures import MultiValueDict

from storefront.custom_print_config import PRODUCT_MATRIX, normalize_custom_print_snapshot
from storefront.custom_print_creation import CreationValidationError, creation_analytics_lead, prepare_creation, save_creation
from storefront.custom_print_notifications import _build_creation_message, _telegram_message_parts, notify_custom_print_creation


def item_snapshot(product="tshirt", quantity=2, total=1000):
    return {"product": {"type": product, "fit": "oversize", "fabric": "standard", "color": "black"},
            "print": {"zones": ["front"], "zone_options": {"front": {"size_preset": "A4"}}},
            "artwork": {"service_kind": "design", "files": []},
            "order": {"quantity": quantity, "size_mode": "mixed", "size_breakdown": {"M": quantity}, "gift": False},
            "notes": {"brief": "Мій принт"}, "pricing": {"unit_total": total // quantity, "final_total": total, "gift_price": 0}}


def envelope(gift=True):
    return {"version": 1, "mode": "personal", "order_purpose": "gift",
            "contact": {"name": "Микита", "channel": "telegram", "value": "@void_unit"},
            "gift": {"enabled": False, "box": {"enabled": False}, "certificate": {"enabled": gift}},
            "items": [{"id": "tshirts", "snapshot": item_snapshot()}, {"id": "hoodie", "snapshot": item_snapshot("hoodie", 1, 1500)}]}


class CreationUnitTests(unittest.TestCase):
    def test_customer_garment_starting_price_never_becomes_a_payable_creation_total(self):
        raw = envelope()
        snapshot = raw["items"][0]["snapshot"]
        snapshot["product"]["type"] = "customer_garment"
        snapshot["notes"]["garment_note"] = "Моя щільна бавовняна сорочка"
        snapshot["pricing"].update(base_price=150, unit_total=150, final_total=300, estimate_required=False)

        creation = prepare_creation(raw, submission_type="cart")
        pricing = creation.items[0]["snapshot"]["pricing"]
        self.assertEqual(pricing["base_price"], 150)
        self.assertEqual(pricing["gift_price"], 150)
        self.assertIsNone(pricing["unit_total"])
        self.assertIsNone(pricing["final_total"])
        self.assertTrue(pricing["estimate_required"])
        self.assertIsNone(creation.forms[0].cleaned_data["pricing_snapshot_json"]["final_total"])
        self.assertEqual(creation.items[1]["snapshot"]["pricing"]["final_total"], 1500)
        self.assertEqual(normalize_custom_print_snapshot(creation.items[0]["snapshot"])["pricing"], pricing)

    def test_all_hoodie_modes_fits_and_fabrics_include_fleece_and_lacing_without_hardware_charge(self):
        for mode, purpose in (("personal", "personal"), ("personal", "gift"), ("brand", "organization")):
            for fit, fabrics in PRODUCT_MATRIX["hoodie"]["fabrics"].items():
                for fabric in fabrics:
                    snapshot = item_snapshot("hoodie", 2, 3900)
                    snapshot.update(mode=mode, order_purpose=purpose)
                    snapshot["product"].update(fit=fit, fabric=fabric["value"])
                    snapshot["print"]["add_ons"] = ["no_fleece", "grommets"]
                    snapshot["pricing"].update(addons_price=150, unit_total=1950, base_price=1800, gift_price=0)
                    normalized = normalize_custom_print_snapshot(snapshot)
                    with self.subTest(mode=mode, purpose=purpose, fit=fit, fabric=fabric):
                        self.assertEqual(normalized["print"]["add_ons"], ["fleece", "lacing"])
                        self.assertEqual(normalized["product"]["fabric"], fabric["value"])
                        self.assertEqual(normalized["pricing"]["base_price"], 1800)
                        self.assertEqual(normalized["pricing"]["addons_price"], 0)
                        self.assertEqual(normalized["pricing"]["unit_total"], 1800)
                        self.assertEqual(normalized["pricing"]["final_total"], 3600)
                        self.assertEqual(normalize_custom_print_snapshot(normalized), normalized)

    def test_mixed_creation_removes_cached_hoodie_fee_only_and_forms_keep_correct_prices(self):
        raw = envelope(False)
        hoodie = raw["items"][1]["snapshot"]
        hoodie["pricing"].update(addons_price=150, unit_total=1500, final_total=1500)
        hoodie["print"]["add_ons"] = ["no_fleece", "lacing"]
        creation = prepare_creation(raw, submission_type="cart")
        self.assertEqual([item["snapshot"]["pricing"]["final_total"] for item in creation.items], [1000, 1350])
        self.assertEqual(creation.items[0]["snapshot"]["print"]["add_ons"], [])
        self.assertEqual(creation.forms[1].cleaned_data["pricing_snapshot_json"]["final_total"], 1350)
        self.assertEqual(creation.forms[1].cleaned_data["config_draft_json"]["print"]["add_ons"], ["fleece", "lacing"])

    def test_two_items_keep_independent_config_quantity_and_single_gift_price(self):
        raw = envelope()
        original = deepcopy(raw)
        creation = prepare_creation(json.dumps(raw))

        self.assertEqual([form.cleaned_data["quantity"] for form in creation.forms], [2, 1])
        self.assertEqual([form.cleaned_data["product_type"] for form in creation.forms], ["tshirt", "hoodie"])
        self.assertEqual([form.cleaned_data["pricing_snapshot_json"]["final_total"] for form in creation.forms], [1150, 1500])
        self.assertEqual([form.cleaned_data["pricing_snapshot_json"]["gift_price"] for form in creation.forms], [150, 0])
        self.assertTrue(creation.forms[0].cleaned_data["config_draft_json"]["order"]["gift"]["certificate"]["enabled"])
        self.assertFalse(creation.forms[1].cleaned_data["config_draft_json"]["order"]["gift"])
        self.assertEqual(raw, original)

    def test_disabled_common_gift_does_not_change_gift_intent_or_charge_items(self):
        creation = prepare_creation(envelope(gift=False))

        self.assertEqual([item["snapshot"]["order_purpose"] for item in creation.items], ["gift", "gift"])
        self.assertEqual([item["snapshot"]["pricing"]["final_total"] for item in creation.items], [1000, 1500])

    def test_errors_identify_invalid_item_and_validate_whole_creation(self):
        raw = envelope()
        raw["items"][1]["snapshot"]["notes"]["brief"] = ""
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw)

        self.assertIn("brief", caught.exception.item_errors["hoodie"])
        self.assertNotIn("tshirts", caught.exception.item_errors)

    def test_common_contact_error_is_not_duplicated_for_each_item(self):
        raw = envelope()
        raw["contact"]["value"] = "not a telegram"
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw)

        self.assertIn("contact_value", caught.exception.errors)
        self.assertEqual(caught.exception.item_errors, {})

    def test_duplicate_ids_and_more_than_ten_items_are_rejected(self):
        for count, duplicate in ((2, True), (11, False)):
            raw = envelope()
            raw["items"] = [{"id": "same" if duplicate else f"item-{index}", "snapshot": item_snapshot()} for index in range(count)]
            with self.subTest(count=count), self.assertRaises(CreationValidationError):
                prepare_creation(raw)

    def test_unexpected_file_field_cannot_attach_to_another_item(self):
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(envelope(), MultiValueDict({"files:foreign": [object()]}))
        self.assertIn("files", caught.exception.errors)

    def test_size_quantities_must_match_item_quantity(self):
        raw = envelope()
        raw["items"][0]["snapshot"]["order"]["size_breakdown"] = {"M": 1}
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw)
        self.assertIn("sizes_note", caught.exception.item_errors["tshirts"])

    def test_sleeve_files_retain_sides_and_multiple_files_for_same_zone(self):
        raw = envelope(gift=False)
        snap = raw["items"][1]["snapshot"]
        snap["print"] = {"zones": ["sleeve"], "zone_options": {"sleeve": {"left_enabled": True, "right_enabled": True}}}
        snap["artwork"] = {"service_kind": "ready", "files": [
            {"name": "a.png", "zone": "sleeve", "placement_key": "sleeve_left", "file_index": 0},
            {"name": "b.png", "zone": "sleeve", "placement_key": "sleeve_left", "file_index": 1},
            {"name": "c.png", "zone": "sleeve", "placement_key": "sleeve_right", "file_index": 2},
        ]}
        uploads = MultiValueDict({"files:hoodie": [SimpleUploadedFile(name, b"image") for name in ("a.png", "b.png", "c.png")]})
        with patch("storefront.forms.validate_uploaded_file"):
            creation = prepare_creation(raw, uploads)

        metadata = creation.forms[1].cleaned_data["config_draft_json"]["artwork"]["files"]
        self.assertEqual([item["placement_key"] for item in metadata], ["sleeve_left", "sleeve_left", "sleeve_right"])
        self.assertEqual([item["file_index"] for item in metadata], [0, 1, 2])

    def test_garment_photo_does_not_count_as_required_print_artwork(self):
        raw = envelope(gift=False)
        raw["items"][1]["snapshot"]["artwork"]["service_kind"] = "ready"
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw, MultiValueDict({"garment_photo:hoodie": [SimpleUploadedFile("garment.png", b"image")]}))
        self.assertIn("files", caught.exception.item_errors["hoodie"])

    def test_current_tshirt_hem_is_accepted_by_form(self):
        raw = envelope(gift=False)
        raw["items"][0]["snapshot"]["print"] = {"zones": ["hem"], "zone_options": {"hem": {"side": "front", "mode": "text", "text": "Hello"}}}
        creation = prepare_creation(raw)
        self.assertEqual(creation.forms[0].cleaned_data["placements"], ["hem"])

    def test_safe_exit_keeps_completed_item_and_partial_next_item(self):
        raw = envelope()
        raw["items"][1]["snapshot"] = {"ui": {"current_step": "product"}}
        creation = prepare_creation(raw, submission_type="safe_exit", partial=True)
        self.assertEqual(len(creation.items), 2)
        self.assertEqual(creation.items[0]["snapshot"]["order"]["quantity"], 2)
        self.assertEqual(creation.items[1]["snapshot"]["ui"]["current_step"], "product")

    def test_save_failure_cleans_written_files(self):
        creation = prepare_creation(envelope())
        storage = Mock()
        def fail_save(**kwargs):
            kwargs["saved_uploads"].append((storage, "custom_print/leads/new.png"))
            raise RuntimeError("persistence failed")
        creation.forms[0].save = fail_save
        with patch("storefront.custom_print_creation.transaction.atomic", return_value=nullcontext()), self.assertRaises(RuntimeError):
            save_creation(creation)
        storage.delete.assert_called_once_with("custom_print/leads/new.png")

    def _leads(self, raw=None):
        creation = prepare_creation(raw or envelope())
        leads = []
        for index, item in enumerate(creation.items):
            snap = item["snapshot"]
            snap["creation"] = {"id": creation.id, "item_index": index, "item_count": len(creation.items), "gift": creation.gift}
            leads.append(SimpleNamespace(pk=index + 1, lead_number=f"CP-UNIT-{index + 1}", name=creation.contact["name"], contact_channel="telegram",
                                         contact_value=creation.contact["value"], quantity=snap["order"]["quantity"], config_draft_json=snap,
                                         pricing_snapshot_json=snap["pricing"], product_type=snap["product"]["type"], moderation_status="awaiting_review"))
        return leads

    def test_analytics_aggregates_prices_and_quantity_without_mutating_item_prices(self):
        leads = self._leads()
        projected = creation_analytics_lead(leads)
        self.assertEqual(projected.quantity, 3)
        self.assertEqual(projected.config_draft_json["pricing"]["final_total"], 2650)
        self.assertEqual(leads[0].pricing_snapshot_json["final_total"], 1150)

    def test_ten_item_group_summary_is_bounded_and_includes_each_quantity_and_service(self):
        raw = envelope()
        raw["items"] = [{"id": f"item-{index}", "snapshot": item_snapshot()} for index in range(10)]
        raw["contact"]["name"] = "<&>" * 100
        raw["gift"]["text"] = "<&>" * 400
        # The contact form length limit remains in force; use a valid bounded name.
        raw["contact"]["name"] = "<&>" * 60
        for item in raw["items"]:
            item["snapshot"]["notes"]["brief"] = "<&>" * 100
        leads = self._leads(raw)
        message = _build_creation_message(leads)
        self.assertTrue(_telegram_message_parts(message))
        for lead in leads:
            self.assertIn(lead.lead_number, message)
        self.assertEqual(message.count("×2</b>"), 10)
        self.assertEqual(message.count("Потрібен дизайн"), 10)

    def test_notifier_sends_one_summary_then_files_for_every_item(self):
        leads = self._leads()
        notifier = Mock()
        notifier.is_configured.return_value = True
        notifier.send_admin_message.return_value = True
        for lead in leads:
            lead.attachments = SimpleNamespace(all=lambda: [])
        with patch("storefront.custom_print_notifications._claim_notification_slot", return_value=True), \
             patch("storefront.custom_print_notifications._build_notifier", return_value=notifier), \
             patch("storefront.custom_print_notifications._collect_attachment_payloads", return_value=[]) as send_files:
            self.assertTrue(notify_custom_print_creation(leads))
        notifier.send_admin_message.assert_called_once()
        self.assertEqual(send_files.call_count, 2)


if __name__ == "__main__":
    unittest.main()
