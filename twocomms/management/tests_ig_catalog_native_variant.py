"""Duplicate color labels retain only an authenticated exact native variant."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase

from management.tests_ig_catalog_choice_actions import ChoiceReducerTests
from management.services import ig_catalog_choice_actions as choices
from management.services.ig_source_cart_catalog import resolve_source_cart_catalog


class NativeVariantIdentityTests(TestCase):
    consume = ChoiceReducerTests.consume

    def setUp(self):
        ChoiceReducerTests.setUp(self)
        # Sequential real webhook events precede the actual capture time.
        # The capture's ordinary future-source fence remains enabled.
        self.clock -= timedelta(minutes=1)
        from productcolors.models import Color, ProductColorVariant
        duplicate_color = Color.objects.create(name=self.variants[0].color.name, primary_hex="#101010")
        self.duplicate = ProductColorVariant.objects.create(product=self.products[0], color=duplicate_color,
            price_override=990, slug="same-color-second")
        product = self.source({"kind": "product", "product_id": self.products[0].pk})
        self.assertTrue(self.consume(product)["ready"])
        self.color_source = self.source({"kind": "color", "product_id": self.products[0].pk,
            "variant_id": self.duplicate.pk, "label": self.duplicate.color.name})
        self.assertTrue(self.consume(self.color_source)["ready"])
        self.typed("крій classic, розмір L")

    def source(self, action):
        source = ChoiceReducerTests.source(self, action)
        from management.services.ig_conversation_routes import conversation_route_reset_floor
        self.deliveries[source.pk]["scope"].update(generation=self.session.generation,
            candidate_generation=self.session.candidate_generation,
            reset_floor=conversation_route_reset_floor(self.customer.pk), namespace=source.provider_namespace)
        return source

    def typed(self, text):
        from management.models import InstagramBotMessage
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        self.serial += 1
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", text=text, mid=f"native-variant-text-{self.serial}",
            provider_namespace="instagram_login:choice-owner",
            provider_created_at=self.clock + timedelta(seconds=self.serial))
        with transaction.atomic():
            apply_turn(self.customer, source, parse_turn(source.text), reply_payload={})
        self.customer.refresh_from_db()
        return source

    def capture(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        with patch("management.services.ig_admin_state_capture._namespace", return_value=self.color_source.provider_namespace):
            capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(capture["status"], "captured", capture)
        self.assertTrue(capture["coverage_complete"], capture)
        return capture

    @staticmethod
    def reseal(capture):
        capture["capture_digest"] = choices._digest({key: value for key, value in capture.items() if key != "capture_digest"})
        return capture

    def test_same_named_native_color_selects_exact_offered_variant(self):
        capture = self.capture()
        result = resolve_source_cart_catalog(capture)
        self.assertTrue(result.complete, result.gaps)
        row = capture["lines"][0]
        self.assertEqual(result.item_specs_by_line[row["line_id"]]["color_variant_id"], self.duplicate.pk)
        self.assertEqual(str(result.item_specs_by_line[row["line_id"]]["unit_price"]), "990.00")
        self.assertEqual(choices.admitted_native_color_variants(capture, capture["lines"])[row["line_id"]],
            {"status": "verified", "variant_id": self.duplicate.pk})

    def test_raw_variant_hint_cannot_replace_native_selection(self):
        capture = self.capture()
        capture["lines"][0]["color_variant_id"] = self.variants[0].pk
        self.reseal(capture)
        result = resolve_source_cart_catalog(capture)
        self.assertTrue(result.complete, result.gaps)
        self.assertEqual(next(iter(result.item_specs_by_line.values()))["color_variant_id"], self.duplicate.pk)

    def test_native_source_evidence_foreign_line_or_recipient_is_not_variant_authority(self):
        original = self.capture()
        row = original["lines"][0]
        for axis, value in (("recipient_id", "friend"), ("line_id", "foreign-line")):
            capture = deepcopy(original)
            capture["lines"][0][axis] = value
            capture["lines"][0]["source_selection"]["scope"][axis] = value
            self.reseal(capture)
            result = resolve_source_cart_catalog(capture)
            self.assertFalse(result.complete)
            self.assertFalse(result.item_specs_by_line)
        self.assertEqual(row["fields"]["color"]["source"]["source_message_id"], self.color_source.pk)

    def test_changed_native_source_payload_rejects_exact_variant_without_named_sibling_fallback(self):
        capture = self.capture()
        self.color_source.quick_reply_payload = "CC1:7:999:foreign"
        self.color_source.save(update_fields=["quick_reply_payload"])
        result = resolve_source_cart_catalog(capture)
        self.assertFalse(result.complete)
        self.assertFalse(result.item_specs_by_line)

    def test_disabled_config_keeps_past_wish_but_cannot_select_available_same_named_sibling(self):
        from product_catalog.models import VariantFitRule
        capture = self.capture()
        VariantFitRule.objects.create(variant=self.duplicate, fit_code="classic", is_enabled=False)
        result = resolve_source_cart_catalog(capture)
        self.assertFalse(result.complete)
        self.assertFalse(result.item_specs_by_line)
        self.assertIsNotNone(choices.native_choice_request(self.customer, self.color_source, historical=True))
        self.assertEqual(capture["lines"][0]["fields"]["color"]["value"], self.duplicate.color.name)

    def test_later_plain_same_name_wish_cannot_inherit_old_native_variant_id(self):
        self.typed(f"колір {self.duplicate.color.name}")
        capture = self.capture()
        self.assertNotEqual(capture["lines"][0]["fields"]["color"]["source"]["source_message_id"], self.color_source.pk)
        capture["lines"][0]["color_variant_id"] = self.duplicate.pk
        self.reseal(capture)
        result = resolve_source_cart_catalog(capture)
        self.assertFalse(result.complete)
        self.assertFalse(result.item_specs_by_line)
