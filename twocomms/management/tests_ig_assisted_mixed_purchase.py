"""Actual whole-current-cart customer purchase preserves every owned line."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings

from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation, IgCommerceSelectionSession
from management.services import ig_assisted_commerce as assisted
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_commerce_state import apply_turn, CommerceRevisionConflict
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_response_cart_plan import source_line_preferences
from management.tests_ig_assisted_commerce import AssistedCommerceFixture
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeAssistedMixedPurchaseTests(AssistedCommerceFixture, TestCase):
    def mixed_approval(self):
        self.canonical("цвет purple, размер L")
        other, variant = self.products[1], self.variants[1]
        variant.stock = 3
        variant.save(update_fields=["stock"])
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="M", stock=3, is_enabled=True)
        ProductInventoryPolicy.objects.create(product=other, source="catalog_variant")
        self.canonical(f"добавьте {other.title}, количество 1, размер M, цвет pink, крій classic")
        request, _revision = self.request_case(custom=False)
        items = self.items_for(request)
        self.assertEqual(len(items), 2)
        catalog = request.envelope["snapshot"]["lines"][1]["catalog_item_spec"]
        self.assertTrue(catalog)
        items[1].update(item_kind="catalog_apparel", product_id=catalog["product_id"],
            color_variant_id=catalog["color_variant_id"], title=other.title, color="Pink", size="M",
            fit_code="classic", option_values=catalog["option_values"], unit_price=catalog["unit_price"],
            supply_basis="warehouse_checked")
        draft, _ = self.record(request, items=items)
        decision, _ = self.record(request, status="approved", latest=draft)
        return request, decision

    def test_actual_explicit_uk_ru_en_full_basket_purchase_proves_every_original_line(self):
        request, decision = self.mixed_approval()
        original = deepcopy(request.envelope["snapshot"]["full_capture"])
        for phrase in ("Хочу купити всі товари в кошику", "Хочу купить все товары в корзине",
                "I want to buy all items in the current cart"):
            with self.subTest(phrase=phrase), patch("management.services.instagram_bot._provider_http") as provider:
                source = self.canonical(phrase)
                self.revision(source)
                capture = capture_current_selection_lines(request.client_id, now=self.now)
                preferences = [source_line_preferences(capture, line) for line in capture["lines"]]
                self.assertEqual(len(preferences), 2)
                for preference in preferences:
                    self.assertIs(preference["values"]["purchase_requested"], True)
                    self.assertEqual(preference["evidence"]["purchase_requested"]["source_message_id"], source.pk)
                self.assertEqual(assisted._configuration_capture(original), assisted._configuration_capture(capture))
                proof = self.verified(request, decision)
                self.assertEqual(proof.request_digest, request.envelope_hmac)
                self.assertEqual(proof.current_capture["source_watermark"]["message_id"], source.pk)
                facts = source.commerce_turn_decision.result_payload["source_facts"]
                self.assertIs(facts["full_cart_purchase_requested"], True)
                self.assertEqual(facts["line_ids"], [line["line_id"] for line in original["lines"]])
            provider.assert_not_called()
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

    def test_actual_bare_buy_retains_active_only_and_cannot_adopt_mixed_basket(self):
        request, decision = self.mixed_approval()
        source = self.canonical("Хочу купити")
        self.revision(source)
        capture = capture_current_selection_lines(request.client_id, now=self.now)
        preferences = [source_line_preferences(capture, line) for line in capture["lines"]]
        self.assertIsNone(preferences[0]["values"].get("purchase_requested"))
        self.assertIs(preferences[1]["values"].get("purchase_requested"), True)
        self.assertNotIn("full_cart_purchase_requested", source.commerce_turn_decision.result_payload["source_facts"])
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "source_configuration_or_head_changed"):
            self.verified(request, decision)

    def test_negated_quoted_historical_question_and_active_line_text_cannot_be_full_basket(self):
        for phrase in ("Не хочу купити всі товари в кошику", "Не хочу купить все товары в корзине",
                "I don't want to buy all items in the current cart", '"Хочу купити всі товари в кошику"',
                "Раніше хочу купити всі товари в кошику", "Previously I want to buy all items in the current cart",
                "Хочу купити всі товари в кошику?", "Хочу купити другу позицію", "Хочу купити",
                "Хочу купити всі товари в кошику, але змініть кількість на 2"):
            with self.subTest(phrase=phrase):
                self.assertIs(parse_turn(phrase).full_cart_purchase_requested, False)

    def test_natural_unambiguous_all_items_and_whole_cart_phrases_preserve_typed_scope(self):
        for phrase in ("Беру всі товари", "Оформіть весь кошик", "Беру все товары",
                "Оформляйте всю корзину", "Buy all items in my cart", "Order the whole cart"):
            with self.subTest(phrase=phrase):
                parsed = parse_turn(phrase)
                self.assertIs(parsed.full_cart_purchase_requested, True)
                self.assertIs(parsed.purchase_requested, True)
                self.assertFalse(parsed.field_updates)
                self.assertFalse(parsed.line_operations)
        for phrase in ("беру всё", "беру все", "так", "yes", "Беру всі товари?", "Не беру всі товари",
                "Не оформляйте всю корзину", "Don't order the whole cart", "Not buy all items in my cart",
                "Previously order the whole cart", "Раніше беру всі товари", "Раньше оформляйте всю корзину",
                '"Беру всі товари"', '"Order the whole cart"', "Беру всі товари розмір L",
                "Оформіть весь кошик, змініть кількість на 2", "Order the whole cart, except item 1",
                "Buy all items in my cart for my mother", "Беру всі фіолетові товари"):
            with self.subTest(phrase=phrase):
                self.assertIs(parse_turn(phrase).full_cart_purchase_requested, False)

    def test_native_reducer_rejects_forged_full_cart_flag_on_actual_bare_source(self):
        request, _decision = self.mixed_approval()
        source = self.source("Хочу купити")
        parsed = replace(parse_turn(source.text), full_cart_purchase_requested=True)
        before = capture_current_selection_lines(request.client_id, now=self.now)
        with self.assertRaisesMessage(CommerceRevisionConflict, "full_cart_purchase_source_unverified"):
            with transaction.atomic():
                apply_turn(self.client_row, source, parsed, reply_payload={})
        after = capture_current_selection_lines(request.client_id, now=self.now)
        self.assertEqual(before["selection_revision"], after["selection_revision"])
        self.assertEqual(assisted._configuration_capture(before), assisted._configuration_capture(after))

    def test_real_quantity_change_before_full_purchase_invalidates_original_physical_approval(self):
        request, decision = self.mixed_approval()
        self.canonical("змініть кількість на 2")
        buy = self.canonical("Хочу купити всі товари в кошику")
        self.revision(buy)
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "source_configuration_or_head_changed"):
            self.verified(request, decision)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_forged_which_product_without_current_pending_question_is_denied(self):
        request, _decision = self.mixed_approval()
        before = capture_current_selection_lines(request.client_id, now=self.now)
        self.assertFalse(IgCommerceSelectionSession.objects.get(pk=before["session_id"]).pending_clarification)
        source = self.source("Хочу купити всі товари в кошику")
        parsed = replace(parse_turn(source.text), pending_clarification="which_product", source_binding={
            "producer": "revision-inbound-commerce-v1", "source_message_id": source.pk,
            "source_digest": hashlib.sha256(source.text.encode()).hexdigest(), "product_resolution": "unresolved",
        })
        with self.assertRaisesMessage(CommerceRevisionConflict, "full_cart_purchase_source_unverified"):
            with transaction.atomic():
                apply_turn(self.client_row, source, parsed, reply_payload={})
        after = capture_current_selection_lines(request.client_id, now=self.now)
        self.assertEqual(before["selection_revision"], after["selection_revision"])
        self.assertEqual(assisted._configuration_capture(before), assisted._configuration_capture(after))

    def test_real_added_line_before_full_purchase_cannot_reuse_older_full_intention(self):
        request, decision = self.mixed_approval()
        self.canonical(f"добавьте {self.product.title}, количество 1, размер L, цвет purple, крій classic")
        buy = self.canonical("Хочу купити всі товари в кошику")
        self.revision(buy)
        self.assertEqual(len(capture_current_selection_lines(request.client_id, now=self.now)["lines"]), 3)
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "source_configuration_or_head_changed"):
            self.verified(request, decision)
