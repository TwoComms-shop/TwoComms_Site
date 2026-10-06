"""Pure cart/quote lineage contract; SimpleTestCase forbids database access."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

from django.test import SimpleTestCase

from management.services.ig_revision_cart_binding import (
    bind_quote_lines, build_cart_binding, frozen_cart_binding_payload,
    validate_cart_binding,
    validate_quote_source_positions,
)


class RevisionCartBindingTests(SimpleTestCase):
    def setUp(self):
        self.scope = {"client_id": 7, "episode_id": 13, "order_id": None,
                      "reset_id": None, "reset_floor": 10, "source_namespace": "ig:shop:1"}
        self.cart = self.capture()

    def capture(self, count=2):
        lines = []
        for index in range(count):
            line_id, recipient = "line-" + str(index), "self" if index == 0 else "friend-" + str(index)
            own = {**self.scope, "line_id": line_id, "recipient_id": recipient,
                   "session_id": 11, "generation": 2, "revision": 4}
            choices = {"product_id": 37 + index, "size": "L" if index == 0 else "M", "fit_option_code": "regular"}
            fields = {key: {"value": value, "status": "confirmed", "authority": "customer_source",
                "source": {"source_message_id": 20 + index, "source_digest": "a" * 64,
                           "decision_id": 42 + index, "transition_id": 51 + index}}
                      for key, value in choices.items()}
            lines.append({"line_id": line_id, "recipient_id": recipient, "index": index,
                "source_selection": {"schema": "source-selection.v1", "scope": own,
                    "values": deepcopy(choices), "fields": fields},
                "defaults": {"quantity": {"value": 1, "authority": "existing_cart_default", "source_confirmed": False}}})
        return {"schema": "source-selections.v1", "status": "captured", "reason": "",
                "scope": deepcopy(self.scope), "session_id": 11, "generation": 2,
                "selection_revision": 4, "active_line_id": "line-0", "active_index": 0,
                "capture_digest": "b" * 64, "lines": lines}

    def build(self, cart=None, scope=None, **kwargs):
        return build_cart_binding(captured_cart=self.cart if cart is None else cart,
                                  expected_scope=self.scope if scope is None else scope, **kwargs)

    def quote(self, cart=None):
        result = []
        for line in (cart or self.cart)["lines"]:
            fields = line["source_selection"]["fields"]
            result.append({"product_id": fields["product_id"]["value"], "qty": fields.get("quantity", {}).get("value", 1),
                           "size": fields["size"]["value"], "fit_option_code": fields["fit_option_code"]["value"],
                           "color_variant_id": 100 + line["index"], "option_values": {"fabric": "cotton"}})
        return result

    def readiness(self, cart=None, quoted=None):
        cart = cart or self.cart
        quoted = quoted or self.quote(cart)
        return {line["line_id"]: {"scope": {**cart["scope"], "line_id": line["line_id"], "recipient_id": line["recipient_id"]},
                "capture_digest": cart["capture_digest"], "readiness": {
                    "applicability_known": True, "can_issue_link": True, "missing": [],
                    "product": {"id": item["product_id"]}, "quantity": item["qty"],
                    "size": {"selected": item["size"]}, "fit": {"selected": item["fit_option_code"]},
                    "color": {"selected_variant_id": item["color_variant_id"]}, "options": {"selected": item["option_values"]}}}
                for line, item in zip(cart["lines"], quoted)}

    def ready(self):
        return bind_quote_lines(binding=self.build(), quoted_items=self.quote(), readiness_by_line=self.readiness())

    def test_two_lines_preserve_source_scope_recipient_and_quote_position(self):
        bound = self.ready()
        self.assertEqual(bound.status, "ready")
        artifact = frozen_cart_binding_payload(bound)
        self.assertEqual([row["line_id"] for row in artifact["quote_line_map"]], ["line-0", "line-1"])
        self.assertEqual([row["recipient_id"] for row in artifact["quote_line_map"]], ["self", "friend-1"])
        self.assertEqual([row["configuration"]["size"] for row in artifact["quote_line_map"]], ["L", "M"])
        self.assertEqual(artifact["lines"][1]["source_selection"], self.cart["lines"][1]["source_selection"])

    def test_source_capture_does_not_grant_quote_or_sku_authority(self):
        bound = self.build()
        self.assertEqual(bound.status, "captured")
        self.assertNotIn("quote_line_map", bound.binding)
        with self.assertRaisesRegex(ValueError, "not_ready"):
            frozen_cart_binding_payload(bound)
        denied = bind_quote_lines(binding=bound, quoted_items=self.quote(), readiness_by_line={})
        self.assertEqual((denied.status, denied.reason), ("partial", "cart_readiness_missing"))

    def test_default_quantity_remains_unconfirmed(self):
        payload = self.ready().binding
        self.assertNotIn("quantity", payload["lines"][0]["choices"])
        self.assertFalse(payload["quote_line_map"][0]["quantity_source_confirmed"])
        self.assertEqual(payload["lines"][0]["quantity_default"]["value"], 1)

    def test_explicit_quantity_uses_current_source_proof(self):
        field = deepcopy(self.cart["lines"][0]["source_selection"]["fields"]["size"])
        field["value"] = 2
        self.cart["lines"][0]["source_selection"]["fields"]["quantity"] = field
        bound = self.ready()
        self.assertEqual(bound.binding["quote_line_map"][0]["configuration"]["qty"], 2)
        self.assertTrue(bound.binding["quote_line_map"][0]["quantity_source_confirmed"])

    def test_unproved_snapshot_quantity_cannot_become_a_default(self):
        self.cart["lines"][0]["quantity"] = 5
        self.cart["lines"][0]["defaults"] = {}
        bound = self.build()
        self.assertEqual(bound.status, "partial")
        self.assertIn("line-0:quantity", bound.missing)
        with self.assertRaises(ValueError):
            frozen_cart_binding_payload(bound)

    def test_unknown_model_identity_is_partial_not_invented_product(self):
        self.cart["lines"][1]["source_selection"]["fields"]["product_id"]["status"] = "ambiguous"
        bound = self.build()
        self.assertEqual(bound.status, "partial")
        self.assertIn("line-1:product", bound.missing)
        self.assertEqual(len(bound.binding["lines"]), 2)

    def test_changed_episode_order_reset_namespace_do_not_borrow_scope(self):
        for key, value in (("client_id", 8), ("episode_id", 14), ("order_id", 88), ("reset_id", 3),
                           ("reset_floor", 11), ("source_namespace", "ig:other")):
            with self.subTest(key=key):
                scope = {**self.scope, key: value}
                result = self.build(scope=scope)
                self.assertEqual((result.status, result.reason), ("conflict", "cart_scope_changed"))

    def test_parent_scope_can_bind_current_order_without_fabricating_line_order(self):
        for line in self.cart["lines"]:
            line["source_selection"]["scope"].pop("order_id")
        self.scope["order_id"] = 90
        self.cart["scope"]["order_id"] = 90
        self.assertEqual(self.build().status, "captured")
        self.cart["lines"][1]["source_selection"]["scope"]["order_id"] = 91
        self.assertEqual(self.build().reason, "cart_line_scope_changed")

    def test_recipient_change_never_borrows_first_line_size(self):
        self.cart["lines"][1]["recipient_id"] = "different-friend"
        self.assertEqual(self.build().reason, "cart_line_scope_changed")

    def test_foreign_or_duplicate_line_scope_is_rejected(self):
        self.cart["lines"][1]["source_selection"]["scope"]["line_id"] = "line-0"
        self.assertEqual(self.build().reason, "cart_line_scope_changed")
        self.cart = self.capture()
        self.cart["lines"][1]["line_id"] = "line-0"
        self.assertEqual(self.build().reason, "cart_line_identity_invalid")

    def test_missing_digest_or_untrusted_choice_cannot_promote_size(self):
        size = self.cart["lines"][0]["source_selection"]["fields"]["size"]
        size["source"].pop("source_digest")
        self.assertEqual(self.build().reason, "cart_source_proof_missing")
        size["source"]["source_digest"] = "a" * 64
        size["authority"] = "typed_analysis"
        self.assertEqual(self.build().reason, "cart_choice_authority_invalid")

    def test_reset_fence_rejects_old_source(self):
        self.cart["lines"][1]["source_selection"]["fields"]["size"]["source"]["source_message_id"] = 9
        self.assertEqual(self.build().reason, "cart_source_before_reset")

    def test_same_sku_for_two_gift_recipients_is_not_silently_deduplicated(self):
        self.cart["lines"][1]["source_selection"]["fields"] = deepcopy(self.cart["lines"][0]["source_selection"]["fields"])
        quoted = self.quote()
        quoted[1]["color_variant_id"] = quoted[0]["color_variant_id"]
        bound = bind_quote_lines(binding=self.build(), quoted_items=quoted, readiness_by_line=self.readiness(quoted=quoted))
        self.assertEqual(bound.status, "ready")
        self.assertEqual(len(bound.binding["quote_line_map"]), 2)
        self.assertNotEqual(bound.binding["quote_line_map"][0]["recipient_id"], bound.binding["quote_line_map"][1]["recipient_id"])
        admitted = validate_quote_source_positions(binding=bound, client_id=self.scope["client_id"], item_specs=quoted)
        self.assertTrue(admitted.ok, admitted.reason)
        self.assertEqual(len(admitted.binding["lines"]), 2)

    def test_quote_position_proof_rejects_bool_foreign_scope_missing_line_and_changed_quantity(self):
        bound = self.ready()
        for proof in (True, {}, {**bound.binding, "source_binding_digest": "0" * 64}):
            with self.subTest(proof=proof):
                self.assertFalse(validate_quote_source_positions(binding=proof, client_id=7, item_specs=self.quote()).ok)
        self.assertFalse(validate_quote_source_positions(binding=bound, client_id=8, item_specs=self.quote()).ok)
        self.assertEqual(validate_quote_source_positions(binding=bound, client_id=7, item_specs=self.quote()[:1]).reason,
            "cart_quote_line_count_changed")
        altered = self.quote()
        altered[1]["qty"] = 2
        self.assertEqual(validate_quote_source_positions(binding=bound, client_id=7, item_specs=altered).reason,
            "cart_quote_choice_changed")

    def test_original_ready_quote_position_validation_remains_detached(self):
        ready = self.ready()
        original = deepcopy(ready.binding)
        result = validate_quote_source_positions(binding=ready.binding, client_id=7, item_specs=self.quote())
        self.assertTrue(result.ok, result.reason)
        mutable = result.binding
        mutable["lines"][0]["recipient_id"] = "someone_else"
        self.assertEqual(ready.binding, original)

    def test_unready_second_line_does_not_close_whole_cart(self):
        readiness = self.readiness()
        readiness["line-1"]["readiness"].update(can_issue_link=False, missing=["color"])
        bound = bind_quote_lines(binding=self.build(), quoted_items=self.quote(), readiness_by_line=readiness)
        self.assertEqual((bound.status, bound.reason), ("partial", "cart_readiness_incomplete"))
        self.assertEqual(bound.missing, ("line-1:readiness",))
        self.assertNotIn("quote_line_map", bound.binding)

    def test_unknown_applicability_does_not_infer_available_sku(self):
        readiness = self.readiness()
        readiness["line-1"]["readiness"]["applicability_known"] = False
        result = bind_quote_lines(binding=self.build(), quoted_items=self.quote(), readiness_by_line=readiness)
        self.assertEqual(result.missing, ("line-1:applicability",))

    def test_readiness_wrong_recipient_or_capture_is_rejected(self):
        readiness = self.readiness()
        readiness["line-1"]["scope"]["recipient_id"] = "self"
        self.assertEqual(bind_quote_lines(binding=self.build(), quoted_items=self.quote(), readiness_by_line=readiness).reason, "cart_readiness_scope_changed")
        readiness = self.readiness()
        readiness["line-1"]["capture_digest"] = "c" * 64
        self.assertEqual(bind_quote_lines(binding=self.build(), quoted_items=self.quote(), readiness_by_line=readiness).reason, "cart_readiness_capture_changed")

    def test_quote_omission_extra_line_or_reordering_cannot_bind(self):
        for quoted in (self.quote()[:1], self.quote() + self.quote()[:1], list(reversed(self.quote()))):
            with self.subTest(quoted=quoted):
                result = bind_quote_lines(binding=self.build(), quoted_items=quoted, readiness_by_line=self.readiness())
                self.assertEqual(result.status, "conflict")

    def test_quote_size_quantity_variant_option_mutations_are_rejected(self):
        for key, value in (("size", "XL"), ("qty", 2), ("color_variant_id", 999), ("option_values", {"fabric": "wool"})):
            quoted = self.quote()
            quoted[1][key] = value
            with self.subTest(key=key):
                result = bind_quote_lines(binding=self.build(), quoted_items=quoted, readiness_by_line=self.readiness())
                self.assertEqual(result.reason, "cart_quote_configuration_changed")

    def test_foreign_catalog_selection_cannot_override_source_choice(self):
        quoted = self.quote()
        quoted[1]["size"] = "XL"
        result = bind_quote_lines(binding=self.build(), quoted_items=quoted, readiness_by_line=self.readiness(quoted=quoted))
        self.assertEqual(result.reason, "cart_quote_choice_changed")

    def test_price_fields_are_not_a_cart_price_or_tender_authority(self):
        quoted = self.quote()
        quoted[0].update(unit_price="0", payment_amount="0", currency="USD", discount="999")
        result = bind_quote_lines(binding=self.build(), quoted_items=quoted, readiness_by_line=self.readiness())
        self.assertEqual(result.status, "ready")
        payload = json.dumps(result.binding)
        for key in ("unit_price", "payment_amount", "currency", "discount"):
            self.assertNotIn(key, payload)

    def test_old_artifact_cannot_revalidate_after_color_change_remove_or_new_head(self):
        frozen = self.ready()
        for key, value in (("capture_digest", "c" * 64), ("selection_revision", 5), ("generation", 3)):
            current = deepcopy(self.cart)
            current[key] = value
            if key == "selection_revision":
                for line in current["lines"]:
                    line["source_selection"]["scope"]["revision"] = value
            if key == "generation":
                for line in current["lines"]:
                    line["source_selection"]["scope"]["generation"] = value
            with self.subTest(key=key):
                self.assertEqual(validate_cart_binding(binding=frozen, current_capture=current, expected_scope=self.scope).reason, "cart_selection_changed")
        current = self.capture(count=1)
        self.assertEqual(validate_cart_binding(binding=frozen, current_capture=current, expected_scope=self.scope).reason, "cart_selection_changed")

    def test_identical_replay_validates_and_tampered_artifact_does_not(self):
        artifact = frozen_cart_binding_payload(self.ready())
        self.assertEqual(validate_cart_binding(binding=artifact, current_capture=self.cart, expected_scope=self.scope).status, "ready")
        artifact["lines"][1]["recipient_id"] = "self"
        self.assertEqual(validate_cart_binding(binding=artifact, current_capture=self.cart, expected_scope=self.scope).reason, "cart_binding_digest_invalid")

    def test_quote_map_tamper_is_not_source_binding_replay(self):
        artifact = self.ready().binding
        artifact["quote_line_map"][1]["configuration"]["size"] = "XL"
        result = validate_cart_binding(binding=artifact, current_capture=self.cart, expected_scope=self.scope)
        self.assertEqual(result.reason, "cart_quote_binding_digest_invalid")

    def test_boolean_scope_identity_cannot_equal_integer_owner(self):
        cart = deepcopy(self.cart)
        cart["scope"]["client_id"] = True
        self.assertEqual(self.build(cart=cart).reason, "cart_scope_changed")

    def test_captured_pre_episode_choice_does_not_issue_current_order_authority(self):
        scope = {**self.scope, "episode_id": None}
        cart = deepcopy(self.cart)
        cart["scope"]["episode_id"] = None
        for line in cart["lines"]:
            line["source_selection"]["scope"]["episode_id"] = None
        result = self.build(cart=cart, scope=scope)
        self.assertEqual((result.status, result.missing), ("partial", ("scope:episode",)))
        with self.assertRaises(ValueError):
            frozen_cart_binding_payload(result)

    def test_capture_and_result_mutation_are_isolated(self):
        result = self.ready()
        digest = result.binding["quote_binding_digest"]
        self.cart["lines"][0]["source_selection"]["fields"]["size"]["value"] = "XL"
        detached = result.binding
        detached["lines"][0]["choices"]["size"] = "S"
        self.assertEqual(result.binding["lines"][0]["choices"]["size"], "L")
        self.assertEqual(result.binding["quote_binding_digest"], digest)
        with self.assertRaises(FrozenInstanceError):
            result.status = "ready"

    def test_existing_checkout_limits_decline_entire_cart(self):
        for count, limit in ((9, 8), (13, 12), (17, 12)):
            with self.subTest(count=count, limit=limit):
                result = self.build(cart=self.capture(count), checkout_item_limit=limit)
                self.assertEqual((result.status, result.reason, result.binding), ("partial", "cart_item_limit", {}))

    def test_legacy_absence_and_malformed_capture_are_finite_without_bootstrap(self):
        self.assertEqual(self.build(cart={}).reason, "cart_capture_missing")
        self.cart["lines"][0]["source_selection"]["fields"]["size"]["value"] = object()
        self.assertEqual(self.build().reason, "cart_capture_invalid")
