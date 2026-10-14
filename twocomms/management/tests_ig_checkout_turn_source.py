"""A site edit is a dated link-holder fact, never fabricated Instagram ingress."""
from copy import deepcopy
from datetime import timedelta
import uuid

from django.test import SimpleTestCase

from management.services.ig_turn_intelligence import (
    TurnContextError, capture_digest, validate_source_cart_capture,
)
from management import tests_ig_multiline_context as multiline_fixtures
from management.tests_ig_turn_intelligence import NOW


class CheckoutTurnSourceTests(SimpleTestCase):
    def fixture(self):
        args, cart = multiline_fixtures.MultilinePromptBudgetTests().fixture(count=1)
        proof = {"amendment_id": str(uuid.uuid4()), "transition_id": 91,
            "source_digest": "d" * 64, "source_kind": "ig-checkout-cart-amendment.v1",
            "authority": "checkout_link_holder", "observed_at": NOW.isoformat()}
        row = cart["lines"][0]
        selection = row["source_selection"]
        selection["values"].update(color_variant_id=53, option_values={"fit": "regular"})
        selection["fields"] = {field: {"value": deepcopy(value), "status": "confirmed",
            "authority": "checkout_link_holder", "source": deepcopy(proof)}
            for field, value in selection["values"].items()}
        selection["evidence"] = {field: deepcopy(proof) for field in selection["values"]}
        row["fields"] = deepcopy(selection["fields"])
        row["history"] = []
        row["evidence"] = deepcopy(selection["evidence"])
        return args["boundary"], cart

    def resign(self, boundary, cart):
        cart["capture_digest"] = capture_digest({key: value for key, value in cart.items() if key != "capture_digest"})
        boundary["source_cart_capture_digest"] = cart["capture_digest"]

    def mutate_proof(self, cart, update):
        row = cart["lines"][0]
        for fact in row["source_selection"]["fields"].values():
            fact["source"].update(update)
        for proof in row["source_selection"]["evidence"].values():
            proof.update(update)
        row["fields"] = deepcopy(row["source_selection"]["fields"])
        row["evidence"] = deepcopy(row["source_selection"]["evidence"])

    def test_native_site_facts_preserve_actor_timestamp_and_exact_options(self):
        boundary, cart = self.fixture()
        self.resign(boundary, cart)
        accepted = validate_source_cart_capture(cart, boundary)
        self.assertEqual(accepted, cart)
        fact = accepted["lines"][0]["fields"]["option_values"]
        self.assertEqual(fact["value"], {"fit": "regular"})
        self.assertEqual(fact["authority"], "checkout_link_holder")
        self.assertNotIn("source_message_id", fact["source"])

    def test_native_receipt_newer_than_sealed_turn_is_rejected(self):
        boundary, cart = self.fixture()
        self.mutate_proof(cart, {"observed_at": (NOW + timedelta(seconds=1)).isoformat()})
        self.resign(boundary, cart)
        with self.assertRaises(TurnContextError):
            validate_source_cart_capture(cart, boundary)

    def test_separate_native_capture_clock_does_not_advance_instagram_watermark(self):
        boundary, cart = self.fixture()
        self.mutate_proof(cart, {"observed_at": (NOW + timedelta(seconds=1)).isoformat()})
        self.resign(boundary, cart)
        original = deepcopy(boundary["watermark"])
        accepted = validate_source_cart_capture(cart, boundary, native_as_of=NOW + timedelta(seconds=2))
        self.assertEqual(accepted["source_watermark"], original)
        self.assertEqual(boundary["watermark"], original)
        with self.assertRaises(TurnContextError):
            validate_source_cart_capture(cart, boundary, native_as_of=NOW)

    def test_prompt_preserves_site_actor_exact_selectors_and_removal_tombstones(self):
        from management.services.ig_turn_intelligence import source_cart_current_facts
        boundary, cart = self.fixture()
        cart["removed"] = [{"line_id": "cancelled-line", "disposition": "cancelled_by_checkout_holder"}]
        self.resign(boundary, cart)
        value = source_cart_current_facts(validate_source_cart_capture(cart, boundary))
        ref, = value["source_refs"]
        self.assertEqual(ref["authority"], "checkout_link_holder")
        self.assertNotIn("source_message_id", ref)
        self.assertEqual(value["removed"], cart["removed"])
        self.assertIn("option_values", value["field_columns"])

    def test_native_proof_cannot_add_a_fake_webhook_identity(self):
        boundary, cart = self.fixture()
        self.mutate_proof(cart, {"source_message_id": 100})
        self.resign(boundary, cart)
        with self.assertRaises(TurnContextError):
            validate_source_cart_capture(cart, boundary)

    def test_native_source_requires_exact_schema_and_canonical_receipt_id(self):
        for update in ({"source_kind": "model"}, {"amendment_id": "not-a-receipt"},
                       {"transition_id": True}, {"authority": "customer_source"}):
            boundary, cart = self.fixture()
            self.mutate_proof(cart, update)
            self.resign(boundary, cart)
            with self.subTest(update=update), self.assertRaises(TurnContextError):
                validate_source_cart_capture(cart, boundary)

    def test_new_variant_options_cannot_be_added_as_ordinary_text_authority(self):
        boundary, cart = self.fixture()
        row = cart["lines"][0]
        for field in ("color_variant_id", "option_values"):
            fact = row["source_selection"]["fields"][field]
            fact.update(authority="customer_source", source={"source_message_id": 100,
                "source_digest": "a" * 64, "observed_at": NOW.isoformat(), "transition_id": 91})
            row["source_selection"]["evidence"][field] = deepcopy(fact["source"])
        row["fields"] = deepcopy(row["source_selection"]["fields"])
        self.resign(boundary, cart)
        with self.assertRaises(TurnContextError):
            validate_source_cart_capture(cart, boundary)
