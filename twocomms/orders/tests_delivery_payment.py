"""Delivery payer/merchant-money contract, with no provider or bank admission."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from orders.services.delivery_payment import (
    DeliveryPaymentError, build_delivery_payment_contract, delivery_payment_snapshot,
)
from management.services.ig_order_amounts import order_amounts


class DeliveryPaymentContractTests(SimpleTestCase):
    def order(self, mode="carrier_recipient", fee="0", *, confirmed=None, allocated=None,
              authority="manual_manager", items=None):
        contract = build_delivery_payment_contract(mode=mode, merchandise_total="850.00", delivery_amount=fee,
            actor_id=41, authority=authority, confirmed_amount=confirmed, allocated_delivery_amount=allocated,
            review_id=42 if authority == "payment_review" else None,
            decision_id=43 if authority == "payment_review" else None,
            evidence_message_ids=[44] if authority == "payment_review" else [], item_rows=items)
        payload = {"delivery_payment": contract}
        if authority == "payment_review":
            payload.update(instagram_payment_review_id=42, manager_payment_decision_id=43, manager_confirmed_amount=confirmed)
        return SimpleNamespace(total_sum=Decimal("850.00"), discount_amount=Decimal("0.00"),
                               payment_payload=payload, items=items)

    def test_legacy_default_and_recipient_charge_exclude_carrier_fee_from_merchant_money(self):
        legacy = SimpleNamespace(total_sum="850.00", discount_amount="0", payment_payload=None)
        result = delivery_payment_snapshot(legacy)
        self.assertEqual((result["valid"], result["payer_type"], result["payable_total"]), (None, "Recipient", Decimal("850.00")))
        order = self.order()
        order.payment_payload["carrier_estimated_delivery_amount"] = "120.00"
        result = delivery_payment_snapshot(order)
        self.assertEqual((result["mode"], result["payer_type"], result["delivery_amount"]), ("carrier_recipient", "Recipient", Decimal("0.00")))
        self.assertEqual(order_amounts(order)["payable"], Decimal("850.00"))
        self.assertFalse(result["delivery_prepaid"])

    def test_customer_full_payment_shipping_sender_no_second_carrier_collection(self):
        order = self.order("customer_prepaid", "120", confirmed="970", authority="payment_review")
        result = delivery_payment_snapshot(order)
        self.assertEqual((result["valid"], result["payer_type"], result["delivery_prepaid"]), (True, "Sender", True))
        self.assertEqual((result["merchandise_total"], result["delivery_amount"], result["payable_total"]),
                         (Decimal("850.00"), Decimal("120.00"), Decimal("970.00")))
        self.assertEqual(result["payable_total"] - Decimal("970.00"), Decimal("0.00"))
        self.assertTrue(result["source_locked"])

    def test_explicit_delivery_allocation_keeps_unpaid_goods_in_cod_once(self):
        order = self.order("customer_prepaid", "120", confirmed="120", allocated="120")
        result = delivery_payment_snapshot(order)
        self.assertEqual(result["payer_type"], "Sender")
        self.assertEqual(result["payable_total"] - Decimal("120"), Decimal("850.00"))
        self.assertEqual(result["contract"]["payment_confirmation"]["basis"], "explicit_allocation")
        self.assertFalse(result["source_locked"])
        partial = self.order("customer_prepaid", "120", confirmed="200", allocated="120")
        self.assertEqual(order_amounts(partial)["payable"] - Decimal("200"), Decimal("770.00"))

    def test_merchant_free_shipping_sender_zero_fee_does_not_claim_customer_paid(self):
        order = self.order("merchant_free")
        result = delivery_payment_snapshot(order)
        self.assertEqual((result["valid"], result["payer_type"], result["delivery_amount"]), (True, "Sender", Decimal("0.00")))
        self.assertFalse(result["delivery_prepaid"])
        self.assertFalse(result["source_locked"])
        self.assertEqual(order_amounts(order)["payable"], Decimal("850.00"))

    def test_prepaid_fee_without_sufficient_audited_money_or_exact_allocation_rejected(self):
        for confirmed, allocated in ((None, None), ("80", "120"), ("200", None), ("200", "80"), ("1000", "120")):
            with self.subTest(confirmed=confirmed, allocated=allocated), self.assertRaises(DeliveryPaymentError):
                self.order("customer_prepaid", "120", confirmed=confirmed, allocated=allocated)

    def test_modes_finite_fee_and_actor_constraints(self):
        for mode, fee in (("carrier_recipient", "120"), ("merchant_free", "1"), ("customer_prepaid", "0"),
                          ("unknown", "0"), ("merchant_free", "-1"), ("merchant_free", "NaN"), ("merchant_free", "Infinity")):
            with self.subTest(mode=mode, fee=fee), self.assertRaises(DeliveryPaymentError):
                self.order(mode, fee)
        for actor in (0, -1, True, "41"):
            with self.subTest(actor=actor), self.assertRaises(DeliveryPaymentError):
                build_delivery_payment_contract(mode="merchant_free", merchandise_total="850", actor_id=actor)

    def test_review_requires_source_bindings_and_current_payment_decision(self):
        order = self.order("customer_prepaid", "120", confirmed="970", authority="payment_review")
        order.payment_payload["manager_payment_decision_id"] = 999
        result = delivery_payment_snapshot(order)
        self.assertFalse(result["valid"])
        self.assertTrue(result["source_locked"])
        self.assertEqual(result["reason"], "delivery_review_binding_changed")
        order.payment_payload["manager_payment_decision_id"] = 43
        order.payment_payload["manager_confirmed_amount"] = Decimal("0")
        self.assertFalse(delivery_payment_snapshot(order)["valid"])
        with self.assertRaises(DeliveryPaymentError):
            build_delivery_payment_contract(mode="merchant_free", merchandise_total="850", actor_id=41,
                authority="payment_review", review_id=42, decision_id=43)

    def test_current_price_and_quantity_binding_invalidated_without_querying(self):
        items = [{"title": "Synthetic garment", "qty": 1, "unit_price": "850", "line_total": "850"}]
        order = self.order("merchant_free", items=items)
        self.assertTrue(delivery_payment_snapshot(order, item_rows=items)["valid"])
        self.assertTrue(order_amounts(order)["delivery_contract_valid"])
        self.assertEqual(delivery_payment_snapshot(order)["reason"], "delivery_items_unavailable")
        changed = [{**items[0], "qty": 2, "unit_price": "425"}]
        result = delivery_payment_snapshot(order, item_rows=changed)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "delivery_items_changed")
        order.total_sum = "900"
        self.assertFalse(delivery_payment_snapshot(order, item_rows=items)["valid"])
        for qty in (0, True, -1, "1", 1001):
            with self.subTest(qty=qty), self.assertRaises(DeliveryPaymentError):
                self.order("merchant_free", items=[{**items[0], "qty": qty}])

    def test_discount_keeps_declared_merchandise_separate_from_delivery(self):
        items = [{"qty": 1, "unit_price": "1000", "line_total": "1000"}]
        contract = build_delivery_payment_contract(mode="customer_prepaid", merchandise_total="850", delivery_amount="120",
            actor_id=41, confirmed_amount="970", item_rows=items, discount_amount="150")
        order = SimpleNamespace(total_sum="1000", discount_amount="150", payment_payload={"delivery_payment": contract}, items=items)
        amounts = order_amounts(order)
        self.assertEqual((amounts["merchandise_payable"], amounts["delivery"], amounts["payable"]),
                         (Decimal("850.00"), Decimal("120.00"), Decimal("970.00")))

    def legacy(self, *, prepaid=True):
        return SimpleNamespace(total_sum="850", discount_amount="0", payment_payload={"instagram_delivery_contract": {
            "merchandise_total": "850.00", "delivery_amount": "120.00", "payable_total": "970.00",
            "prepaid": prepaid, "payer_type": "Sender" if prepaid else "Recipient",
            "actor_id": 41, "review_id": 42, "decision_id": 43, "evidence_message_ids": [44]}})

    def test_valid_legacy_review_bridge_deterministic_and_partial_ambiguous_requires_manual(self):
        full = self.legacy()
        first = delivery_payment_snapshot(full)
        self.assertEqual(first, delivery_payment_snapshot(full))
        self.assertEqual((first["mode"], first["payer_type"], first["source_locked"]), ("customer_prepaid", "Sender", True))
        self.assertEqual(order_amounts(full)["payable"], Decimal("970.00"))
        partial = delivery_payment_snapshot(self.legacy(prepaid=False))
        self.assertEqual((partial["valid"], partial["requires_manual"], partial["source_locked"]), (False, True, True))
        self.assertEqual(partial["reason"], "legacy_delivery_allocation_unknown")
        self.assertEqual(partial["delivery_amount"], Decimal("0.00"))

    def test_new_contract_wins_over_obsolete_alias_but_malformed_new_never_falls_back(self):
        order = self.order("merchant_free")
        order.payment_payload.update(self.legacy(prepaid=False).payment_payload)
        self.assertTrue(delivery_payment_snapshot(order)["valid"])
        order.payment_payload["delivery_payment"] = []
        self.assertFalse(delivery_payment_snapshot(order)["valid"])

    def test_poisoned_contract_shapes_payer_and_money_proof_fail_closed(self):
        original = self.order("customer_prepaid", "120", confirmed="970").payment_payload["delivery_payment"]
        for key, invalid in (("schema", "other"), ("payer_type", "Recipient"), ("actor_id", True),
            ("delivery_amount", "NaN"), ("payable_total", "850"), ("payment_confirmation", []),
            ("payment_confirmation", {"basis": "full_payment", "confirmed_amount": "970", "allocated_delivery_amount": "0"})):
            order = self.order("customer_prepaid", "120", confirmed="970")
            order.payment_payload["delivery_payment"] = {**deepcopy(original), key: invalid}
            with self.subTest(key=key):
                self.assertFalse(delivery_payment_snapshot(order)["valid"])
                self.assertTrue(delivery_payment_snapshot(order)["requires_manual"])


class DeliveryPaymentOrderQuantityTests(TestCase):
    def test_current_order_items_read_owned_by_amount_getter_invalidates_same_gross_quantity_change(self):
        from orders.models import Order, OrderItem
        order = Order.objects.create(full_name="Synthetic recipient", phone="0500000000", city="Synthetic city", np_office="1", total_sum="850")
        item = OrderItem.objects.create(order=order, title="Synthetic garment", qty=1, unit_price="850", line_total="850", is_custom=True)
        order.payment_payload = {"delivery_payment": build_delivery_payment_contract(mode="merchant_free",
            merchandise_total="850", actor_id=41, item_rows=[item])}
        order.save(update_fields=["payment_payload"])
        self.assertTrue(order_amounts(order)["delivery_contract_valid"])
        item.qty, item.unit_price = 2, Decimal("425.00")
        item.save(update_fields=["qty", "unit_price"])
        snapshot = order_amounts(order)["delivery_payment_snapshot"]
        self.assertEqual((snapshot["valid"], snapshot["reason"]), (False, "delivery_items_changed"))
