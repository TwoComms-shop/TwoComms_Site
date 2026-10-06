import json
import io
from decimal import Decimal
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
from storefront.models import CustomPrintLead, CustomPrintModerationStatus


def creation_payload(gift=True):
    def snapshot(product, quantity, total):
        return {"product": {"type": product, "fit": "oversize", "fabric": "standard", "color": "black"},
                "print": {"zones": ["front"], "zone_options": {"front": {"size_preset": "A4"}}},
                "artwork": {"service_kind": "design", "files": []},
                "notes": {"brief": "Створити принт за моєю ідеєю"},
                "order": {"quantity": quantity, "size_mode": "mixed", "size_breakdown": {"M": quantity}, "gift": False},
                "pricing": {"unit_total": total // quantity, "final_total": total, "gift_price": 0}}
    return {"version": 1, "mode": "personal", "order_purpose": "gift",
            "contact": {"name": "Микита", "channel": "telegram", "value": "@void_unit"},
            "gift": {"enabled": False, "box": {"enabled": False}, "certificate": {"enabled": gift}},
            "items": [{"id": "tshirts", "snapshot": snapshot("tshirt", 2, 1000)},
                      {"id": "hoodie", "snapshot": snapshot("hoodie", 1, 1500)}]}


@override_settings(SECURE_SSL_REDIRECT=False)
class CustomPrintCreationTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        media_settings = override_settings(MEDIA_ROOT=self.media.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        event_patch = patch("storefront.views.static_pages.record_custom_print_event")
        event_patch.start()
        self.addCleanup(event_patch.stop)

    def _submit(self, payload=None, *, cart=False, extra=None):
        data = {"creation_json": json.dumps(payload or creation_payload())}
        data.update(extra or {})
        return self.client.post(reverse("custom_print_add_to_cart" if cart else "custom_print_lead"), data, secure=True)

    def test_group_lead_creates_two_item_records_and_one_shared_notification(self):
        with patch("storefront.views.static_pages.notify_custom_print_creation") as notify, \
             patch("storefront.views.static_pages.notify_new_custom_print_lead") as legacy_notify, \
             self.captureOnCommitCallbacks(execute=True):
            response = self._submit()

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        leads = list(CustomPrintLead.objects.order_by("pk"))
        self.assertEqual([(lead.product_type, lead.quantity) for lead in leads], [("tshirt", 2), ("hoodie", 1)])
        self.assertEqual(payload["total_quantity"], 3)
        self.assertEqual(payload["item_count"], 2)
        self.assertEqual(payload["lead_ids"], [lead.pk for lead in leads])
        for index, lead in enumerate(leads):
            meta = lead.config_draft_json["creation"]
            self.assertEqual(meta["id"], payload["creation_id"])
            self.assertEqual(meta["lead_ids"], payload["lead_ids"])
            self.assertEqual(meta["item_index"], index)
            self.assertEqual(meta["gift_owner_lead_id"], leads[0].pk)
            self.assertEqual(lead.config_draft_json["order_purpose"], "gift")
        self.assertEqual([int(lead.final_price_value) for lead in leads], [1150, 1500])
        notify.assert_called_once()
        legacy_notify.assert_not_called()

    def test_cart_creation_inserts_every_item_without_replacing_existing_cart(self):
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {"custom:old": {"lead_id": 987, "quantity": 1}}
        session.save()
        with patch("storefront.views.static_pages.notify_custom_print_creation"), self.captureOnCommitCallbacks(execute=True):
            response = self._submit(cart=True)

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["custom_cart_count"], 3)
        cart = self.client.session[SESSION_CUSTOM_CART_KEY]
        self.assertIn("custom:old", cart)
        for lead in CustomPrintLead.objects.all():
            item = cart[f"custom:{lead.pk}"]
            self.assertEqual(item["quantity"], lead.quantity)
            self.assertEqual(item["creation"]["id"], payload["creation_id"])
            self.assertEqual(lead.moderation_status, CustomPrintModerationStatus.AWAITING_REVIEW)
            self.assertTrue(lead.moderation_token)

    def test_invalid_second_item_creates_no_leads_and_does_not_change_cart(self):
        payload = creation_payload()
        payload["items"][1]["snapshot"]["notes"]["brief"] = ""
        response = self._submit(payload, cart=True)

        self.assertEqual(response.status_code, 400)
        self.assertIn("brief", response.json()["item_errors"]["hoodie"])
        self.assertFalse(CustomPrintLead.objects.exists())
        self.assertNotIn(SESSION_CUSTOM_CART_KEY, self.client.session)

    def test_cart_projection_failure_rolls_back_all_item_records(self):
        from storefront.views.static_pages import _build_custom_cart_session_item
        calls = 0
        def failing_builder(lead):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("cart projection failed")
            return _build_custom_cart_session_item(lead)
        with patch("storefront.views.static_pages._build_custom_cart_session_item", side_effect=failing_builder), self.assertRaises(RuntimeError):
            self._submit(cart=True)
        self.assertFalse(CustomPrintLead.objects.exists())
        self.assertNotIn(SESSION_CUSTOM_CART_KEY, self.client.session)

    def test_failure_after_upload_removes_saved_files_and_rows(self):
        payload = creation_payload(False)
        payload["items"][0]["snapshot"]["artwork"]["files"] = [{"name": "print.png", "zone": "front", "placement_key": "front", "file_index": 0}]
        upload = SimpleUploadedFile("print.png", b"test-image", content_type="image/png")
        with patch("storefront.forms.validate_uploaded_file"), \
             patch("storefront.views.static_pages._build_custom_cart_session_item", side_effect=RuntimeError("projection failed")), \
             self.assertRaises(RuntimeError):
            self._submit(payload, cart=True, extra={"files:tshirts": upload})
        self.assertFalse(CustomPrintLead.objects.exists())
        self.assertEqual([path for path in Path(self.media.name).rglob("*") if path.is_file()], [])

    def test_per_item_uploads_preserve_sides_and_reference_photo(self):
        payload = creation_payload(False)
        snap = payload["items"][1]["snapshot"]
        snap["print"] = {"zones": ["sleeve"], "zone_options": {"sleeve": {"left_enabled": True, "right_enabled": True}}}
        snap["artwork"] = {"service_kind": "ready", "files": [
            {"name": "left.png", "zone": "sleeve", "placement_key": "sleeve_left", "file_index": 0},
            {"name": "left-detail.png", "zone": "sleeve", "placement_key": "sleeve_left", "file_index": 1},
            {"name": "right.png", "zone": "sleeve", "placement_key": "sleeve_right", "file_index": 2},
        ]}
        files = [SimpleUploadedFile(name, b"test-image", content_type="image/png") for name in ("left.png", "left-detail.png", "right.png")]
        with patch("storefront.forms.validate_uploaded_file"), patch("storefront.views.static_pages.notify_custom_print_creation"):
            response = self._submit(payload, extra={"files:hoodie": files, "garment_photo:hoodie": SimpleUploadedFile("reference.png", b"image")})
        self.assertEqual(response.status_code, 200, response.content)
        tshirt, hoodie = list(CustomPrintLead.objects.order_by("pk"))
        self.assertEqual(tshirt.attachments.count(), 0)
        self.assertEqual(list(hoodie.attachments.order_by("sort_order").values_list("placement_zone", flat=True)),
                         ["sleeve_left", "sleeve_left", "sleeve_right", "garment_reference"])

    def test_client_cannot_supply_server_group_or_existing_lead_binding(self):
        payload = creation_payload()
        for item in payload["items"]:
            item["snapshot"]["creation"] = {"id": "forged", "lead_ids": [999], "gift_owner_lead_id": 999}
        with patch("storefront.views.static_pages.notify_custom_print_creation"):
            response = self._submit(payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["creation_id"], "forged")
        for lead in CustomPrintLead.objects.all():
            self.assertNotIn(999, lead.config_draft_json["creation"]["lead_ids"])

    def test_safe_exit_preserves_completed_and_incomplete_items(self):
        payload = creation_payload()
        payload["items"][1]["snapshot"] = {"product": {"type": "hoodie"}, "ui": {"current_step": "config"}}
        with patch("storefront.views.static_pages.notify_custom_print_creation") as notify, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("custom_print_safe_exit"), json.dumps(payload), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(CustomPrintLead.objects.count(), 2)
        self.assertEqual(CustomPrintLead.objects.order_by("pk").last().exit_step, "config")
        notify.assert_called_once()

    def test_safe_exit_without_contact_notifies_group_without_persisting_leads(self):
        payload = creation_payload()
        payload["contact"] = {}
        with patch("storefront.views.static_pages.notify_custom_print_creation") as notify, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("custom_print_safe_exit"), json.dumps(payload), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(CustomPrintLead.objects.exists())
        self.assertEqual(response.json()["item_count"], 2)
        notify.assert_called_once()

    def test_one_aggregate_analytics_event_preserves_each_stored_item_total(self):
        with patch("storefront.views.static_pages.notify_custom_print_creation"), \
             patch("storefront.views.static_pages._send_custom_print_lead_capi") as send_event, \
             self.captureOnCommitCallbacks(execute=True):
            response = self._submit(extra={"analytics_event_id": "creation-event"})
        self.assertEqual(response.status_code, 200)
        send_event.assert_called_once()
        projected = send_event.call_args.args[0]
        self.assertEqual(projected.quantity, 3)
        self.assertEqual(projected.config_draft_json["pricing"]["final_total"], 2650)
        self.assertEqual(list(CustomPrintLead.objects.order_by("pk").values_list("pricing_snapshot_json", flat=True))[0]["final_total"], 1150)

    def test_review_resubmission_notifies_once_for_group(self):
        with patch("storefront.views.static_pages.notify_custom_print_creation"):
            response = self._submit(cart=True)
        self.assertEqual(response.status_code, 200)
        CustomPrintLead.objects.update(moderation_status=CustomPrintModerationStatus.DRAFT)
        with patch("storefront.views.static_pages.notify_custom_print_creation", return_value=True) as notify, \
             patch("storefront.views.static_pages.notify_custom_print_moderation_request") as single_notify:
            response = self.client.post(reverse("custom_print_submit_review"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["notified"], 1)
        notify.assert_called_once()
        single_notify.assert_not_called()

    def test_cart_read_promotes_group_drafts_with_one_summary(self):
        from storefront.views.cart import _collect_custom_cart_state
        from django.test import RequestFactory
        with patch("storefront.views.static_pages.notify_custom_print_creation"):
            response = self._submit(cart=True)
        self.assertEqual(response.status_code, 200)
        CustomPrintLead.objects.update(moderation_status=CustomPrintModerationStatus.DRAFT)
        request = RequestFactory().get("/cart/")
        request.session = self.client.session
        with patch("storefront.views.cart.notify_custom_print_creation", return_value=True) as notify, \
             patch("storefront.views.cart.notify_custom_print_moderation_request") as single_notify:
            state = _collect_custom_cart_state(request)
        self.assertEqual(len(state["custom_items"]), 2)
        self.assertEqual(CustomPrintLead.objects.filter(moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW).count(), 2)
        notify.assert_called_once()
        single_notify.assert_not_called()

    def _gift_extras_cart(self, *, wrapping=False):
        payload = creation_payload(False)
        payload["gift"] = {"box": {"enabled": False}, "delivery": {"enabled": True, "method": "branch"}, "certificate": {"enabled": True}}
        payload["gift"]["wrapping"] = {"enabled": wrapping, "paper": "red", "style": "hearts", "preference": "Без блискіток"}
        with patch("storefront.views.static_pages.notify_custom_print_creation"):
            response = self._submit(payload, cart=True)
        self.assertEqual(response.status_code, 200, response.content)
        CustomPrintLead.objects.update(moderation_status=CustomPrintModerationStatus.APPROVED)
        return list(CustomPrintLead.objects.order_by("pk"))

    def test_cart_payload_exposes_common_extras_and_exactly_one_fee_owner(self):
        from storefront.views.cart import _collect_custom_cart_state
        from django.test import RequestFactory
        self._gift_extras_cart(wrapping=True)
        request = RequestFactory().get("/cart/")
        request.session = self.client.session
        rows = _collect_custom_cart_state(request)["custom_items"]
        self.assertEqual([row["gift_charge_owner"] for row in rows], [True, False])
        self.assertTrue(all(row["gift_extras"]["delivery"]["enabled"] for row in rows))
        self.assertTrue(all(row["order_purpose"] == "gift" for row in rows))
        response = self.client.get(reverse("cart"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Упаковка + промокод 10%")
        self.assertContains(response, "200 грн один раз для добірки", count=1)
        self.assertContains(response, "Без блискіток", count=1)
        self.assertContains(response, "наявність підтвердить менеджер", count=1)

    def test_one_box_image_is_stored_on_carrier_and_shared_by_metadata(self):
        payload = creation_payload(False)
        payload["gift"] = {"box": {"enabled": True, "content_type": "image", "image_name": "inside.png"}}
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(stream, format="PNG")
        image = SimpleUploadedFile("inside.png", stream.getvalue(), content_type="image/png")
        with patch("storefront.custom_print_creation.validate_uploaded_file"), patch("storefront.views.static_pages.notify_custom_print_creation"):
            response = self._submit(payload, cart=True, extra={"gift_image": image})
        self.assertEqual(response.status_code, 200, response.content)
        carrier, sibling = list(CustomPrintLead.objects.order_by("pk"))
        attachment = carrier.attachments.get(attachment_role="gift_reference")
        self.assertEqual(attachment.placement_zone, "gift_box")
        self.assertEqual(sibling.attachments.count(), 0)
        for lead in (carrier, sibling):
            self.assertEqual(lead.config_draft_json["creation"]["gift"]["box"]["attachment"]["id"], attachment.pk)
        self.assertFalse(carrier.estimate_required)
        self.assertEqual(carrier.pricing_snapshot_json["final_total"], 1350)
        self.assertEqual(carrier.pricing_snapshot_json["gift_box_price"], 350)

    def test_invoice_freezes_extras_and_materialized_order_has_no_double_shipping_charge(self):
        from orders.models import Order, PaymentAttempt
        from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
        from orders.payment_attempts import materialize_payment_attempt
        from orders.services.delivery_payment import delivery_payment_snapshot
        from management.services.ig_order_amounts import order_amounts
        from orders.nova_poshta_documents import build_order_payment_snapshot
        self._gift_extras_cart(wrapping=True)
        payload = {"full_name": "Gift Buyer", "phone": "+380501112236", "city": "Kyiv", "np_office": "Branch 1", "pay_type": "online_full",
                   "np_city_token": build_city_choice_token({"label": "Kyiv", "settlement_ref": "settlement-1", "city_ref": "city-1"}),
                   "np_warehouse_token": build_warehouse_choice_token({"label": "Branch 1", "ref": "warehouse-1", "kind": "branch", "city_ref": "city-1"})}
        with patch("storefront.views.monobank._monobank_api_request", return_value={"invoiceId": "gift-invoice", "pageUrl": "https://pay.example/gift"}) as provider, \
             patch("orders.telegram_notifications.TelegramNotifier.send_payment_attempt_notification", return_value=True), \
             patch("orders.facebook_conversions_service.get_facebook_conversions_service") as fb:
            fb.return_value.send_add_payment_info_event.return_value = True
            response = self.client.post(reverse("monobank_create_invoice"), json.dumps(payload), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Order.objects.exists())
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.gross_amount, Decimal("3000.00"))
        self.assertEqual(provider.call_args.kwargs["json_payload"]["amount"], 300000)
        self.assertTrue(attempt.cart_snapshot["custom_print_creation"]["groups"][0]["applied"])
        self.assertEqual(attempt.cart_snapshot["custom_print_creation"]["groups"][0]["gift"]["wrapping"]["target"], "zip")
        order, created = materialize_payment_attempt(attempt.pk, status="success", payload={"paidAmount": 300000})
        self.assertTrue(created)
        self.assertEqual(order.custom_print_leads.count(), 2)
        self.assertEqual(order.total_sum, Decimal("3000.00"))
        gift = order.payment_payload["custom_print_creation"]["groups"][0]["gift"]
        self.assertEqual(gift["wrapping"]["price"], 200)
        self.assertEqual(gift["wrapping"]["style"], "hearts")
        self.assertEqual(gift["wrapping"]["preference"], "Без блискіток")
        self.assertTrue(delivery_payment_snapshot(order)["funded"])
        self.assertEqual(order_amounts(order)["payable"], Decimal("3000.00"))
        np = build_order_payment_snapshot(order)
        self.assertEqual(np["declared_cost_value"], Decimal("2850.00"))
        self.assertEqual(np["delivery_payer_type"], "Sender")

    def test_unpaid_manual_order_projection_keeps_requested_delivery_unfunded(self):
        from orders.models import Order
        from storefront.custom_print_creation import attach_custom_print_checkout
        from orders.services.delivery_payment import delivery_payment_snapshot
        from management.services.ig_order_amounts import order_amounts
        leads = self._gift_extras_cart(wrapping=True)
        order = Order.objects.create(full_name="Buyer", phone="+380501112236", city="Kyiv", np_office="Branch 1",
                                     pay_type="cod", payment_status="unpaid", total_sum=Decimal("3000.00"))
        attach_custom_print_checkout(order, leads=leads)
        self.assertEqual(order_amounts(order)["payable"], Decimal("3000.00"))
        self.assertFalse(delivery_payment_snapshot(order)["delivery_prepaid"])
        self.assertEqual(delivery_payment_snapshot(order)["payer_type"], "Sender")

    def test_box_card_message_and_wrapping_are_preserved_with_one_850_charge(self):
        payload = creation_payload(False)
        message = "а" * 240
        payload["gift"] = {"box": {"enabled": True, "content_type": "text", "text": "Зі святом!"},
                           "wrapping": {"enabled": True, "paper": "kraft", "style": "custom", "preference": "Без блискіток", "price": 999, "target": "zip"},
                           "delivery": {"enabled": True, "method": "branch"},
                           "certificate": {"enabled": True, "message_mode": "write", "message": message, "placement": "wrong"}}
        with patch("storefront.views.static_pages.notify_custom_print_creation"):
            response = self._submit(payload, cart=True)
        self.assertEqual(response.status_code, 200, response.content)
        carrier, sibling = list(CustomPrintLead.objects.order_by("pk"))
        self.assertEqual(carrier.pricing_snapshot_json["gift_price"], 850)
        self.assertEqual([int(lead.final_price_value) for lead in (carrier, sibling)], [1850, 1500])
        for lead in (carrier, sibling):
            gift = lead.config_draft_json["creation"]["gift"]
            self.assertEqual(gift["certificate"]["message"], message)
            self.assertEqual(gift["certificate"]["placement"], "box_top")
            self.assertEqual(gift["certificate"]["message_price"], 0)
            self.assertEqual(gift["wrapping"]["price"], 200)
            self.assertEqual(gift["wrapping"]["target"], "box")
            self.assertEqual(gift["wrapping"]["preference"], "Без блискіток")
            self.assertTrue(gift["base_packaging"]["included"])
