"""Pure native-checkout state consumers; SimpleTestCase forbids ORM reads."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from django.test import SimpleTestCase

from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
from management.services.ig_turn_intelligence import capture_digest


class CheckoutNativeStateCardTests(SimpleTestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
        self.observed = "2026-10-14T09:00:00+00:00"
        self.watermark = {"message_id": 100, "event_at": "2026-10-13T08:00:00+00:00"}
        self.scope = {"client_id": 7, "episode_id": 13, "order_id": None,
            "reset_id": 17, "reset_floor": 10, "source_namespace": "instagram:verified"}
        self.proof = {"amendment_id": "11111111-1111-4111-8111-111111111111",
            "transition_id": 43, "source_digest": "b" * 64,
            "source_kind": "ig-checkout-cart-amendment.v1", "authority": "checkout_link_holder",
            "observed_at": self.observed}
        self.values = {"product_id": 37, "color_variant_id": 18, "title": "Поточна футболка",
            "size": "L", "fit_option_code": "oversize", "option_values": {"fit": "oversize"},
            "color": "black", "quantity": 3, "garment_type": "tshirt"}
        fields = {key: {"status": "confirmed", "authority": "checkout_link_holder",
            "value": deepcopy(value), "source": deepcopy(self.proof)} for key, value in self.values.items()}
        self.selection = {"schema": "source-selection.v1", "scope": {**self.scope,
            "line_id": "remaining-line", "recipient_id": "self", "session_id": 5,
            "generation": 1, "revision": 4, "active_index": 0}, "fields": fields,
            "values": deepcopy(self.values), "evidence": {key: deepcopy(self.proof) for key in fields}}
        self.cart = {"schema": "source-selections.v1", "status": "captured", "reason": "",
            "scope": deepcopy(self.scope), "session_id": 5, "generation": 1, "selection_revision": 4,
            "active_index": 0, "active_line_id": "remaining-line", "source_watermark": deepcopy(self.watermark),
            "lines": [{"line_id": "remaining-line", "recipient_id": "self", "index": 0,
                "source_selection": deepcopy(self.selection), "fields": deepcopy(fields),
                "evidence": deepcopy(self.selection["evidence"]), "cleared": {}, "history": [],
                "defaults": {}, "unsupported_selectors": []}],
            "omissions": [], "coverage_complete": True, "line_limit": 16, "transition_limit": 64,
            "query_limit": 64, "removed": [{"line_id": "removed-old-line", "recipient_id": "self",
                "quantity": 2, "disposition": "cancelled_by_checkout_holder",
                "operation_id": "22222222-2222-4222-8222-222222222222"}],
            "fence": {"namespace": self.scope["source_namespace"], "source_watermark": deepcopy(self.watermark),
                "owner_digest": "c" * 64, "source_digest": "d" * 64, "snapshot_digest": "e" * 64,
                "source_ids": [], "native_amendment_id": self.proof["amendment_id"],
                "native_source_hmac": self.proof["source_digest"]}}
        self.reseal()
        self.boundary = {**self.scope, "line_id": "remaining-line", "recipient_id": "self",
            "selection_session_id": 5, "selection_generation": 1, "selection_revision": 4,
            "source_watermark": deepcopy(self.watermark), "source_cart_capture_digest": self.cart["capture_digest"]}

    def reseal(self):
        self.cart["capture_digest"] = capture_digest({key: value for key, value in self.cart.items() if key != "capture_digest"})
        if hasattr(self, "boundary"):
            self.boundary["source_cart_capture_digest"] = self.cart["capture_digest"]

    def state(self, *, cart=True, selection=None, extra=None):
        components = {"source_selection": deepcopy(selection or self.cart["lines"][0]["source_selection"]),
            "source_selection_binding": {key: self.boundary.get(key) for key in
                ("client_id", "episode_id", "order_id", "line_id", "recipient_id", "reset_floor", "source_namespace", "reset_id")}}
        if cart:
            components["source_cart"] = deepcopy(self.cart)
        components.update(extra or {})
        return assemble_client_state(boundary=self.boundary, components=components, captured_at=self.at)

    def test_exact_parent_native_facts_survive_active_and_all_line_cards_without_message_identity(self):
        before = deepcopy(self.cart)
        state = self.state().as_dict()
        self.assertEqual(state["status"], "captured")
        self.assertEqual(state["source_selection"], self.cart["lines"][0]["source_selection"])
        for slots in (state["slots"], state["lines"][0]["slots"]):
            for name, value in (("size", "L"), ("color", "black"), ("quantity", 3)):
                slot = slots["choice." + name]
                self.assertEqual((slot["value"], slot["status"], slot["authority"]), (value, "confirmed", "checkout_link_holder"))
                self.assertEqual(slot["observed_at"], self.observed)
                self.assertEqual(slot["source_refs"][0]["kind"], "checkout_cart_amendment")
                self.assertNotIn("message_id", slot["source_watermark"])
                self.assertEqual(slot["source_label"], "Змінено в оформленні · власник посилання")
        self.assertEqual(state["source_watermark"], self.watermark)
        self.assertEqual(self.cart, before)

    def test_prompt_keeps_native_title_options_variant_quantity_and_removal_not_old_purchase(self):
        state = self.state()
        result = render_client_state_prompt(state, budget=10000)
        self.assertFalse(result.oversized)
        self.assertIn("current.source_cart", result.included)
        self.assertIn("not Instagram messages", result.text)
        block = next(json.loads(row) for row in result.text.splitlines() if row.startswith('{"authority":"source_bound_customer_wishes"'))
        facts = block["value"]
        current = dict(zip(facts["field_columns"], facts["lines"][0]["values"]))
        self.assertEqual({key: current[key] for key in self.values}, self.values)
        self.assertEqual(facts["removed"], self.cart["removed"])
        self.assertEqual(len(facts["lines"]), 1)
        self.assertEqual(facts["source_refs"][0], self.proof)
        self.assertNotIn("source_message_id", facts["source_refs"][0])
        self.assertEqual(facts["source_watermark"], self.watermark)

    def test_native_authority_without_verified_parent_cart_is_unknown(self):
        state = self.state(cart=False).as_dict()
        self.assertFalse(state["source_selection"])
        self.assertEqual(state["slots"]["choice.size"]["status"], "unknown")
        self.assertEqual(state["slots"]["choice.size"]["authority"], "none")

    def test_tampered_parent_digest_omits_the_whole_current_cart(self):
        self.cart["lines"][0]["fields"]["quantity"]["value"] = 9
        state = self.state().as_dict()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["slots"], {})
        self.assertFalse(state["source_selection"])
        self.assertEqual(state["omissions"][0]["reason"], "source_cart_digest_changed")

    def test_native_fence_identity_mismatch_does_not_enter_mandatory_prompt(self):
        self.cart["fence"]["native_source_hmac"] = "f" * 64
        self.reseal()
        state = self.state()
        self.assertEqual(state.as_dict()["status"], "unavailable")
        self.assertEqual(state.as_dict()["omissions"][0]["reason"], "checkout_source_fence_invalid")
        self.assertNotIn("Поточна футболка", render_client_state_prompt(state, budget=10000).text)

    def test_receipt_after_capture_clock_fails_even_when_chat_watermark_is_unchanged(self):
        for container in (self.cart["lines"][0]["source_selection"]["fields"], self.cart["lines"][0]["fields"]):
            for field in container.values():
                field["source"]["observed_at"] = "2026-10-14T11:00:00+00:00"
        for container in (self.cart["lines"][0]["source_selection"]["evidence"], self.cart["lines"][0]["evidence"]):
            for proof in container.values():
                proof["observed_at"] = "2026-10-14T11:00:00+00:00"
        self.reseal()
        state = self.state().as_dict()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["omissions"][0]["reason"], "source_cart_field_proof_invalid")

    def test_foreign_line_scope_cannot_relabel_native_checkout_choices(self):
        self.cart["lines"][0]["source_selection"]["scope"]["recipient_id"] = "friend"
        self.reseal()
        state = self.state().as_dict()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["omissions"][0]["reason"], "source_cart_line_scope_changed")

    def test_generic_optional_slot_cannot_self_declare_checkout_authority(self):
        state = self.state(extra={"slots": {"context.fake_checkout": {"value": "forged",
            "status": "confirmed", "authority": "checkout_link_holder", "scope": {
                key: self.boundary.get(key) for key in ("client_id", "episode_id", "order_id", "line_id", "recipient_id", "reset_floor")},
            "source_refs": [{"kind": "checkout_cart_amendment", "id": 43}], "validity": "valid"}}}).as_dict()
        self.assertEqual(state["slots"]["context.fake_checkout"]["authority"], "none")
        self.assertEqual(state["slots"]["context.fake_checkout"]["omission_reason"], "authority_unknown")

    def mixed_chat_cart(self):
        """Actual ordinary-capture shape after a newer customer color choice."""
        self.watermark = {"message_id": 100, "event_at": "2026-10-14T09:30:00+00:00"}
        chat = {"value": "blue", "status": "confirmed", "authority": "customer_source", "source": {
            "source_message_id": 100, "source_digest": "a" * 64, "decision_id": 44,
            "transition_id": 45, "observed_at": self.watermark["event_at"]}}
        row = self.cart["lines"][0]
        for container in (row["fields"], row["source_selection"]["fields"]):
            container["color"] = deepcopy(chat)
        for container in (row["evidence"], row["source_selection"]["evidence"]):
            container["color"] = deepcopy(chat["source"])
        row["source_selection"]["values"]["color"] = "blue"
        row["source_selection"]["scope"]["revision"] = 5
        row["source_selection"]["revision"] = 5
        self.cart["selection_revision"] = self.boundary["selection_revision"] = 5
        self.cart["source_watermark"] = deepcopy(self.watermark)
        self.boundary["source_watermark"] = deepcopy(self.watermark)
        self.cart["fence"].update(source_ids=[100], source_watermark=deepcopy(self.watermark))
        self.cart["fence"].pop("native_amendment_id")
        self.cart["fence"].pop("native_source_hmac")
        self.reseal()

    def test_mixed_parent_retains_old_native_quantity_and_fresh_customer_color_with_separate_dates(self):
        self.mixed_chat_cart()
        state = self.state()
        payload = state.as_dict()
        self.assertEqual(payload["status"], "captured")
        for slots in (payload["slots"], payload["lines"][0]["slots"]):
            quantity, color = slots["choice.quantity"], slots["choice.color"]
            self.assertEqual((quantity["value"], quantity["status"], quantity["authority"]), (3, "confirmed", "checkout_link_holder"))
            self.assertEqual(quantity["observed_at"], self.observed)
            self.assertEqual(quantity["source_refs"][0]["kind"], "checkout_cart_amendment")
            self.assertEqual((color["value"], color["status"], color["authority"]), ("blue", "confirmed", "customer_source"))
            self.assertEqual(color["observed_at"], self.watermark["event_at"])
            self.assertEqual(color["source_refs"][0]["id"], 100)
            self.assertNotIn("source_label", color)
        self.assertEqual(payload["source_watermark"], self.watermark)
        prompt = render_client_state_prompt(state, budget=10000).text
        block = next(json.loads(row) for row in prompt.splitlines() if row.startswith('{"authority":"source_bound_customer_wishes"'))
        facts = block["value"]
        quantities = facts["field_columns"].index("quantity")
        colors = facts["field_columns"].index("color")
        native = next(group for group in facts["lines"][0]["field_evidence"] if quantities in group["fields"])
        customer = next(group for group in facts["lines"][0]["field_evidence"] if colors in group["fields"])
        self.assertEqual(native["authority"], "checkout_link_holder")
        self.assertEqual(customer["authority"], "customer_source")
        self.assertNotIn("source_message_id", facts["source_refs"][native["source_ref"]])
        self.assertNotIn("amendment_id", facts["source_refs"][customer["source_ref"]])

    def test_mixed_parent_still_rejects_malformed_native_receipt_proof(self):
        self.mixed_chat_cart()
        row = self.cart["lines"][0]
        for container in (row["fields"], row["source_selection"]["fields"]):
            container["quantity"]["source"]["amendment_id"] = "not-a-receipt-uuid"
        for container in (row["evidence"], row["source_selection"]["evidence"]):
            container["quantity"]["amendment_id"] = "not-a-receipt-uuid"
        self.reseal()
        state = self.state().as_dict()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["omissions"][0]["reason"], "source_cart_field_proof_invalid")


    def test_journey_display_consumes_native_captured_fields_without_manufacturing_message_ids(self):
        from management.services.ig_journey_selection import source_selection_fields, selection_fields
        captured = self.state().source_selection
        result = source_selection_fields(captured)
        self.assertEqual((result["completed"], result["total"]), (0, None))
        self.assertEqual(result["label"], "Поточний вибір · зміни в оформленні")
        quantity = next(row for row in result["items"] if row["key"] == "quantity")
        self.assertEqual(quantity["value"], "3")
        self.assertEqual(quantity["source"]["observed_at"], self.observed)
        self.assertEqual(quantity["evidence_refs"][0]["kind"], "checkout_cart_amendment")
        self.assertNotIn("source_message_id", quantity["source"])
        self.assertNotIn("removed-old-line", {row.get("line_id") for row in result["items"]})
        self.assertTrue(all(row["applicability"] == "unknown" and row["availability"] == "unknown" for row in result["items"]))
        self.assertEqual(selection_fields({"has_product": False}, scope=self.boundary,
            evidence_refs=[], source_selection=captured), result)

    def test_journey_keeps_colliding_native_and_message_source_ids_separate_after_actual_chat(self):
        from management.services.ig_journey_selection import source_selection_fields
        self.mixed_chat_cart()
        row = self.cart["lines"][0]
        for container in (row["fields"], row["source_selection"]["fields"]):
            for fact in container.values():
                if fact["authority"] == "checkout_link_holder":
                    fact["source"]["transition_id"] = 100
        for container in (row["evidence"], row["source_selection"]["evidence"]):
            for proof in container.values():
                if proof.get("authority") == "checkout_link_holder":
                    proof["transition_id"] = 100
        self.reseal()
        result = source_selection_fields(self.state().source_selection)
        self.assertEqual({(ref["kind"], ref["id"]) for ref in result["evidence_refs"]},
            {("message", 100), ("checkout_cart_amendment", 100)})
        color = next(row for row in result["items"] if row["key"] == "color")
        quantity = next(row for row in result["items"] if row["key"] == "quantity")
        self.assertEqual(color["evidence_refs"][0]["kind"], "message")
        self.assertEqual(quantity["evidence_refs"][0]["kind"], "checkout_cart_amendment")
        self.assertEqual(color["source"]["observed_at"], self.watermark["event_at"])
        self.assertEqual(quantity["source"]["observed_at"], self.observed)

    def test_journey_malformed_native_source_and_missing_ig_reference_are_unknown_not_keyerror(self):
        from management.services.ig_journey_selection import source_selection_fields
        captured = deepcopy(self.state().source_selection)
        for field in captured["fields"].values():
            field["source"]["amendment_id"] = "not-a-native-receipt"
        self.assertIsNone(source_selection_fields(captured))
        self.assertIsNone(source_selection_fields({"fields": {"size": {
            "value": "L", "status": "confirmed", "source": {}}}}))


class CheckoutCompletedStateCardTests(SimpleTestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
        self.boundary = {"client_id": 7, "episode_id": 13, "order_id": 81,
            "line_id": "remaining-line", "recipient_id": "self", "reset_floor": 10,
            "source_namespace": "instagram:verified", "reset_id": 17,
            "source_watermark": {"message_id": 100, "event_at": "2026-10-13T08:00:00+00:00"}}
        before = [{"line_id": "remaining-line", "title": "Футболка після правки", "quantity": 3,
            "product_id": 37, "size": "L", "color": "black", "fit_option_code": "classic"},
            {"line_id": "removed-line", "title": "Видалене худі", "quantity": 1,
                "product_id": 38, "size": "M", "color": "blue", "fit_option_code": "classic"}]
        reduced = deepcopy(before)
        reduced[0]["quantity"] = 1
        self.body = {"schema": "current-checkout-order.v1", "scope": {"client_id": 7, "episode_id": 13, "order_id": 81},
            "order_number": "TC-0081", "winner_generation_id": 31, "attempt_id": 21,
            "observed_at": "2026-10-14T09:45:00+00:00",
            "items": [{"line_id": "remaining-line", "title": "Футболка після правки", "quantity": 1,
                "unit_price": "1000.00", "line_total": "1000.00", "size": "L", "fit": "classic", "color": "black"}],
            "money": {"gross": "1000.00", "discount": "100.00", "merchandise_payable": "900.00",
                "payable": "900.00", "confirmed_paid": "900.00", "balance": "0.00", "currency": "UAH"},
            "checkout_changes": [{"amendment_id": "11111111-1111-4111-8111-111111111111",
                "source_hmac": "a" * 64, "changed_at": "2026-10-14T09:00:00+00:00", "operation": "quantity",
                "line_id": "remaining-line", "quantity": 1, "before": before, "after": reduced},
                {"amendment_id": "22222222-2222-4222-8222-222222222222", "source_hmac": "b" * 64,
                    "changed_at": "2026-10-14T09:15:00+00:00", "operation": "remove", "line_id": "removed-line",
                    "quantity": None, "before": reduced, "after": reduced[:1]}],
            "shipping_authorities": [], "source_ref": {"proposal_id": 11, "proposal_revision": 2,
                "generation_id": 31, "attempt_id": 21, "items_digest": "c" * 64,
                "settlement_status": "paid", "amendment_hmacs": ["a" * 64, "b" * 64]}}

    def signed(self, body=None):
        from management.services.ig_reply_authority import _snapshot_keys, _snapshot_mac
        body = deepcopy(body or self.body)
        key_id = next(iter(_snapshot_keys()))
        return {**body, "key_id": key_id, "hmac": _snapshot_mac(body, key_id)}

    def wrapper(self, snapshot=None):
        return {"scope": deepcopy(self.boundary), "capture": snapshot or self.signed()}

    def state(self, **components):
        return assemble_client_state(boundary=self.boundary, components=components, captured_at=self.at)

    def test_current_order_keeps_final_items_real_changes_and_paid_money_mandatory_without_ig_window_refresh(self):
        component = self.wrapper()
        state = self.state(checkout_order=component)
        payload = state.as_dict()
        slot = payload["slots"]["order.completed"]
        self.assertEqual((slot["status"], slot["authority"], slot["mandatory"]), ("confirmed", "payment_ledger", True))
        self.assertTrue(slot["fully_paid"])
        self.assertEqual(slot["value"]["items"], self.body["items"])
        self.assertEqual(slot["value"]["checkout_changes"], self.body["checkout_changes"])
        self.assertEqual(slot["value"]["money"], self.body["money"])
        self.assertEqual(slot["observed_at"], self.body["observed_at"])
        self.assertEqual(slot["source_label"], "Фактичне замовлення після оформлення")
        self.assertEqual(payload["source_watermark"], self.boundary["source_watermark"])
        self.assertNotIn("message", {ref["kind"] for ref in slot["source_refs"]})
        result = render_client_state_prompt(state, budget=10000)
        self.assertIn("order.completed", result.included)
        order = next(json.loads(row) for row in result.text.splitlines()
            if row.startswith("{") and json.loads(row).get("slot") == "order.completed")
        self.assertEqual(len(order["value"]["items"]), 1)
        self.assertEqual(order["value"]["items"][0]["quantity"], 1)
        self.assertEqual(order["value"]["money"]["payable"], "900.00")

    def test_deposit_and_unknown_balance_never_claim_fully_paid_from_confirmed_capture(self):
        for paid, balance in (("200.00", "700.00"), (None, None)):
            body = deepcopy(self.body)
            body["money"].update(confirmed_paid=paid, balance=balance)
            body["source_ref"]["settlement_status"] = "prepaid" if paid else "converted"
            state = self.state(checkout_order=self.wrapper(self.signed(body))).as_dict()
            slot = state["slots"]["order.completed"]
            self.assertEqual(slot["status"], "confirmed")
            self.assertFalse(slot["fully_paid"])
            self.assertEqual(slot["value"]["money"]["balance"], balance)
            self.assertEqual(state["slots"]["payment.current"]["status"], "unknown")

    def test_tampered_signed_items_or_outer_capture_scope_fail_closed(self):
        tampered = self.signed()
        tampered["items"][0]["quantity"] = 3
        foreign_scope = self.wrapper()
        foreign_scope["scope"]["recipient_id"] = "friend"
        for component in (self.wrapper(tampered), foreign_scope):
            state = self.state(checkout_order=component)
            slot = state.as_dict()["slots"]["order.completed"]
            self.assertEqual((slot["status"], slot["authority"], slot["value"]), ("unknown", "none", None))
            self.assertNotIn("order.completed", render_client_state_prompt(state, budget=10000).included)

    def test_new_episode_does_not_borrow_paid_order_and_history_requires_explicit_adapter_component(self):
        snapshot = self.signed()
        self.boundary.update(episode_id=14, order_id=None)
        rejected = self.state(checkout_order=self.wrapper(snapshot)).as_dict()
        self.assertEqual(rejected["slots"]["order.completed"]["status"], "unknown")
        self.assertNotIn("order.completed_history", rejected["slots"])
        history = self.state(checkout_order=self.wrapper(snapshot), completed_history=[self.wrapper(snapshot)])
        slots = history.as_dict()["slots"]
        self.assertEqual(slots["order.completed"]["status"], "unknown")
        self.assertTrue(slots["order.completed_history"]["historical"])
        self.assertFalse(slots["order.completed_history"]["current_purchase_authority"])
        self.assertFalse(slots["order.completed_history"]["mandatory"])
        self.assertEqual(slots["order.completed_history"]["value"][0]["scope"]["episode_id"], 13)

    def test_current_episode_and_foreign_client_are_not_accepted_as_completed_history(self):
        body = deepcopy(self.body)
        body["scope"]["client_id"] = 8
        state = self.state(completed_history=[self.wrapper(), self.wrapper(self.signed(body))]).as_dict()
        self.assertNotIn("order.completed_history", state["slots"])

    def test_newer_native_receipt_cannot_cross_capture_clock(self):
        for field in ("order", "change"):
            body = deepcopy(self.body)
            if field == "order":
                body["observed_at"] = "2026-10-14T11:00:00+00:00"
            else:
                body["checkout_changes"][0]["changed_at"] = "2026-10-14T11:00:00+00:00"
            state = self.state(checkout_order=self.wrapper(self.signed(body))).as_dict()
            self.assertEqual(state["slots"]["order.completed"]["omission_reason"], "source_after_capture")

    def test_generic_slots_cannot_self_declare_current_or_historical_order_authority(self):
        forged = {"value": self.body, "status": "confirmed", "authority": "payment_ledger",
            "scope": deepcopy(self.boundary), "source_refs": [{"kind": "order", "id": 81}], "validity": "valid"}
        slots = self.state(slots={"order.completed": forged, "order.completed_history": forged}).as_dict()["slots"]
        self.assertNotIn("order.completed", slots)
        self.assertNotIn("order.completed_history", slots)
