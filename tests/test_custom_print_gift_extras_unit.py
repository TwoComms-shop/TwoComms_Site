import io
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from html import escape

from tests.test_custom_print_creation_unit import envelope
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.datastructures import MultiValueDict
from PIL import Image

from storefront.custom_print_config import GIFT_SERVICE, normalize_custom_print_snapshot
from storefront.custom_print_creation import CreationValidationError, custom_print_checkout_payload, prepare_creation
from storefront.custom_print_notifications import _build_creation_message
from orders.services.delivery_payment import build_custom_print_delivery_contract, delivery_payment_snapshot
from management.services.ig_order_amounts import order_amounts
from orders.nova_poshta_documents import build_order_payment_snapshot


class GiftExtrasUnitTests(unittest.TestCase):
    def _raw(self, *, box=False, delivery=False, certificate=False, method="branch"):
        raw = envelope(False)
        raw["gift"] = {"enabled": box, "box": {"enabled": box, "content_type": "text", "text": "Зі святом"},
                       "delivery": {"enabled": delivery, "method": method}, "certificate": {"enabled": certificate}}
        return raw

    def _leads(self, *, method="branch"):
        creation = prepare_creation(self._raw(delivery=True, certificate=True, method=method))
        leads = []
        for index, item in enumerate(creation.items):
            snap = item["snapshot"]
            snap["creation"] = {"id": creation.id, "gift_owner_lead_id": 1, "gift": creation.gift,
                                 "lead_ids": [1, 2], "item_index": index, "item_count": 2}
            leads.append(SimpleNamespace(pk=index + 1, lead_number=f"CP-EXTRA-{index + 1}", quantity=snap["order"]["quantity"],
                                         moderation_status="approved", source="custom_print_cart", config_draft_json=snap,
                                         pricing_snapshot_json=snap["pricing"], final_price_value=Decimal(str(snap["pricing"]["final_total"]))))
        return leads

    def _order(self, *, status="unpaid", method="branch", discount=0):
        leads = self._leads(method=method)
        data = custom_print_checkout_payload(leads)
        gross = sum(lead.final_price_value for lead in leads)
        contract = build_custom_print_delivery_contract(gross_total=gross, discount_amount=discount, creation_data=data, order_id=42)
        payload = {"custom_print_creation": data, "custom_print_lead_ids": [1, 2], "delivery_payment": contract}
        return SimpleNamespace(pk=42, total_sum=gross, discount_amount=discount, payment_payload=payload,
                               payment_status=status, pay_type="online_full", items=[], custom_print_leads=[])

    def test_old_packaging_price_and_automatic_discount_claim_are_removed(self):
        self.assertEqual(GIFT_SERVICE["price"], 350)
        self.assertEqual(GIFT_SERVICE["base_packaging"]["price"], 0)
        self.assertNotIn("promo_code", GIFT_SERVICE)
        self.assertNotIn("promo_discount_percent", GIFT_SERVICE)
        raw = self._raw()
        raw["gift"] = {"enabled": True, "text": "Old draft"}
        creation = prepare_creation(raw)
        self.assertEqual(creation.items[0]["snapshot"]["pricing"]["final_total"], 1000)
        self.assertFalse(creation.gift["box"]["enabled"])

    def test_authoritative_shipping_and_certificate_prices_apply_once(self):
        raw = self._raw(delivery=True, certificate=True)
        raw["gift"]["delivery"]["price"] = 1
        raw["gift"]["certificate"].update(price=1, discount_percent=99, issued=True)
        creation = prepare_creation(raw, submission_type="cart")
        self.assertEqual([item["snapshot"]["pricing"]["final_total"] for item in creation.items], [1300, 1500])
        self.assertEqual(creation.gift["certificate"]["discount_percent"], 15)
        self.assertFalse(creation.gift["certificate"]["issued"])
        self.assertFalse(creation.gift["delivery"]["funded"])

    def test_box_costs_350_and_can_be_added_to_cart_with_text(self):
        raw = self._raw(box=True)
        creation = prepare_creation(raw)
        self.assertEqual(creation.items[0]["snapshot"]["pricing"]["final_total"], 1350)
        self.assertFalse(creation.items[0]["snapshot"]["pricing"]["estimate_required"])
        self.assertEqual(prepare_creation(raw, submission_type="cart").items[0]["snapshot"]["pricing"]["gift_box_price"], 350)
        raw["gift"]["box"]["text"] = " "
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw)
        self.assertIn("gift_box_text", caught.exception.errors)

    def test_unknown_box_price_fallback_requires_quote_before_cart(self):
        with patch.dict(GIFT_SERVICE, {"box": {"price": None, "estimate_required": True}}):
            creation = prepare_creation(self._raw(box=True))
            self.assertIsNone(creation.items[0]["snapshot"]["pricing"]["final_total"])
            self.assertTrue(creation.items[0]["snapshot"]["pricing"]["estimate_required"])
            with self.assertRaises(CreationValidationError) as caught:
                prepare_creation(self._raw(box=True), submission_type="cart")
        self.assertIn("gift_box_price", caught.exception.errors)

    def test_box_delivery_certificate_wrapping_and_message_are_priced_once(self):
        raw = self._raw(box=True, delivery=True, certificate=True)
        raw["gift"]["wrapping"] = {"enabled": True, "paper": "black", "price": 999}
        raw["gift"]["certificate"].update(message_mode="write", message="Нехай усе вдається!", placement="wrong", message_price=999)
        creation = prepare_creation(raw, submission_type="cart")
        self.assertEqual([item["snapshot"]["pricing"]["final_total"] for item in creation.items], [1650, 1500])
        self.assertEqual(creation.items[0]["snapshot"]["pricing"]["gift_price"], 650)
        self.assertEqual(creation.gift["wrapping"]["price"], 0)
        certificate = creation.gift["certificate"]
        self.assertEqual(certificate["message_price"], 0)
        self.assertEqual(certificate["placement"], "box_top")
        self.assertEqual(certificate["scope"], "any_order_including_custom")
        raw["gift"]["box"]["enabled"] = False
        self.assertEqual(prepare_creation(raw).gift["certificate"]["placement"], "zip_inside")

    def test_certificate_message_limit_rejects_instead_of_truncating(self):
        raw = self._raw(certificate=True)
        raw["gift"]["certificate"].update(message_mode="write", message="а" * 240)
        self.assertEqual(prepare_creation(raw).gift["certificate"]["message"], "а" * 240)
        raw["gift"]["certificate"]["message"] += "а"
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw)
        self.assertIn("gift_certificate_message", caught.exception.errors)
        raw["gift"]["certificate"]["message"] = " "
        with self.assertRaises(CreationValidationError):
            prepare_creation(raw)

    def test_blank_card_does_not_submit_an_abandoned_message(self):
        raw = self._raw(certificate=True)
        raw["gift"]["certificate"].update(message_mode="blank", message="💛" * 241)
        certificate = prepare_creation(raw).gift["certificate"]
        self.assertEqual(certificate["message_mode"], "blank")
        self.assertEqual(certificate["message"], "")
        self.assertEqual(certificate["message_price"], 0)

    def test_invalid_paper_type_returns_a_validation_error(self):
        raw = self._raw()
        raw["gift"]["wrapping"] = {"enabled": True, "paper": ["ivory"]}
        with self.assertRaises(CreationValidationError) as caught:
            prepare_creation(raw)
        self.assertIn("gift_wrapping", caught.exception.errors)

    def test_image_box_requires_real_uploaded_image_but_safe_exit_keeps_filename(self):
        raw = self._raw(box=True)
        raw["gift"]["box"].update(content_type="image", image_name="gift.png")
        with self.assertRaises(CreationValidationError):
            prepare_creation(raw)
        partial = prepare_creation(raw, submission_type="safe_exit", partial=True)
        self.assertEqual(partial.gift["box"]["image_name"], "gift.png")
        self.assertTrue(partial.gift["box"]["needs_reupload"])
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(stream, format="PNG")
        upload = SimpleUploadedFile("gift.png", stream.getvalue(), content_type="image/png")
        with patch("storefront.custom_print_creation.validate_uploaded_file"):
            creation = prepare_creation(raw, MultiValueDict({"gift_image": [upload]}), submission_type="cart")
        self.assertIs(creation.gift_image, upload)
        self.assertEqual(upload.tell(), 0)
        self.assertEqual(creation.items[0]["snapshot"]["pricing"]["gift_box_price"], 350)
        with patch("storefront.custom_print_creation.validate_uploaded_file"), self.assertRaises(CreationValidationError):
            prepare_creation(raw, MultiValueDict({"gift_image": [SimpleUploadedFile("fake.png", b"not image")]}))

    def test_extras_survive_snapshot_normalization_and_do_not_claim_paid_or_issued(self):
        raw = self._raw(delivery=True, certificate=True)
        creation = prepare_creation(raw)
        normalized = normalize_custom_print_snapshot(creation.items[0]["snapshot"])
        gift = normalized["order"]["gift"]
        self.assertEqual(gift["delivery"]["price"], 150)
        self.assertFalse(gift["delivery"]["funded"])
        self.assertEqual(gift["certificate"]["discount_percent"], 15)

    def test_partial_approval_without_owner_does_not_apply_or_bill_shipping(self):
        leads = self._leads()
        data = custom_print_checkout_payload([leads[1]])
        self.assertFalse(data["groups"][0]["applied"])
        self.assertIsNone(build_custom_print_delivery_contract(gross_total=1500, discount_amount=0, creation_data=data))

    def test_conflicting_shipping_methods_require_manager_resolution(self):
        branch = self._leads()
        courier = self._leads(method="courier")
        for index, lead in enumerate(courier, start=3):
            lead.pk = index
            lead.config_draft_json["creation"]["gift_owner_lead_id"] = 3
        with self.assertRaises(CreationValidationError):
            custom_print_checkout_payload(branch + courier)

    def test_included_shipping_does_not_double_count_order_or_np_amounts(self):
        order = self._order(status="paid", discount=100)
        order.payment_payload["paid_amount"] = "2700.00"
        snapshot = delivery_payment_snapshot(order)
        self.assertEqual(snapshot["payable_total"], Decimal("2700.00"))
        self.assertEqual(snapshot["merchandise_total"], Decimal("2550.00"))
        self.assertTrue(snapshot["funded"])
        amounts = order_amounts(order)
        self.assertEqual(amounts["payable"], Decimal("2700.00"))
        np = build_order_payment_snapshot(order)
        self.assertEqual(np["payable_total_value"], Decimal("2700.00"))
        self.assertEqual(np["declared_cost_value"], Decimal("2550.00"))
        self.assertEqual(np["delivery_payer_type"], "Sender")

    def test_unfunded_delivery_is_sender_agreement_but_not_prepaid(self):
        for status in ("unpaid", "checking", "prepaid"):
            with self.subTest(status=status):
                order = self._order(status=status)
                snapshot = delivery_payment_snapshot(order)
                self.assertTrue(snapshot["valid"])
                self.assertEqual(snapshot["payer_type"], "Sender")
                self.assertFalse(snapshot["delivery_prepaid"])
                self.assertFalse(snapshot["funded"])
                self.assertTrue(snapshot["requires_manual"])

    def test_courier_stays_manual_even_after_verified_payment(self):
        snapshot = delivery_payment_snapshot(self._order(status="paid", method="courier"))
        self.assertTrue(snapshot["funded"])
        self.assertTrue(snapshot["requires_manual"])
        self.assertEqual(snapshot["reason"], "courier_manual_required")

    def test_contract_rejects_changed_order_total_owner_binding_or_gift_amount(self):
        for change in ("total", "owner", "gift"):
            order = self._order(status="paid")
            if change == "total":
                order.total_sum += 1
            elif change == "owner":
                order.payment_payload["custom_print_lead_ids"] = [2]
            else:
                order.payment_payload["custom_print_creation"]["groups"][0]["gift"]["delivery"]["price"] = 1
            with self.subTest(change=change):
                self.assertFalse(delivery_payment_snapshot(order)["valid"])

    def test_group_notification_reports_requests_and_certificate_fulfillment_honestly(self):
        message = _build_creation_message(self._leads())
        self.assertIn("ще не оплачено", message)
        self.assertIn("−15%", message)
        self.assertIn("не виданий автоматично", message)

    def test_ten_item_summary_preserves_full_certificate_message_within_limit(self):
        raw = self._raw(box=True, delivery=True, certificate=True)
        raw["items"] = [{"id": f"item-{index}", "snapshot": deepcopy(raw["items"][0]["snapshot"])} for index in range(10)]
        raw["contact"]["name"] = "&" * 200
        raw["gift"]["box"]["text"] = "&" * 1000
        for item in raw["items"]:
            item["snapshot"]["notes"]["brief"] = "&" * 100
            item["snapshot"]["order"]["sizes_note"] = "&" * 50
        raw["gift"]["wrapping"] = {"enabled": True, "paper": "black"}
        text = "&" * 240
        raw["gift"]["certificate"].update(message_mode="write", message=text)
        creation = prepare_creation(raw)
        leads = []
        for index, item in enumerate(creation.items):
            snap = item["snapshot"]
            snap["creation"] = {"id": creation.id, "gift": creation.gift}
            leads.append(SimpleNamespace(pk=index + 1, lead_number=f"CP07102026L{index + 1:013d}", config_draft_json=snap, pricing_snapshot_json=snap["pricing"]))
        message = _build_creation_message(leads)
        self.assertIn(escape(text), message)
        self.assertLessEqual(len(message), 4096)
        for lead in leads:
            self.assertIn(lead.lead_number, message)


if __name__ == "__main__":
    unittest.main()
