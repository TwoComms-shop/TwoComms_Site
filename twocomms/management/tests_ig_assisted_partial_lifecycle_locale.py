"""Native selected card language owns new paid/shipping lifecycle copy."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from management.models import IgLifecycleEvent
from management.services import ig_assisted_partial as partial
from management.services import ig_assisted_partial_shipping as shipping
from management.services import ig_lifecycle as lifecycle
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.nova_poshta_service import NovaPoshtaService


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class NativePartialLifecycleLocaleTests(PartialCommerceFixture, TransactionTestCase):
    def english_paid_case(self):
        actual_source = self.canonical

        def own_english_source(text, **kwargs):
            source = actual_source(f"{text}. Please reply in English.", **kwargs)
            # Simulate stale profile state after the genuine inbound reducer;
            # the sealed own source, delivered card and choice remain real.
            self.client_row.language = "ru"
            self.client_row.save(update_fields=["language"])
            return source

        with patch.object(self, "canonical", side_effect=own_english_source):
            case = self.selected_case()
        self.assertEqual(partial.selection_reply_language(case["selection"]), "en")
        self.client_row.language = "ru"
        self.client_row.save(update_fields=["language"])
        prepared = self.prepare_selection(case["selection"])
        self.assertEqual(prepared["proposal"].locale, "en")
        order = self.pay_prepared(prepared)
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.language, "ru")
        return case, order

    def verified_tracking(self, order):
        order.tracking_number = "20450000000001"
        order.nova_poshta_document_ref = "native-partial-locale-document"
        order.save(update_fields=["tracking_number", "nova_poshta_document_ref"])
        contract = shipping.assisted_shipping_contract(order)
        with patch.object(NovaPoshtaService, "get_tracking_info", return_value={
                "Number": order.tracking_number, "Ref": order.nova_poshta_document_ref,
                "PayerType": "Recipient", "Cost": "100.00"}) as carrier:
            shipping.verify_manual_assisted_tracking(order, tracking_number=order.tracking_number)
        carrier.assert_called_once()
        order.save(update_fields=["payment_payload"])
        order.refresh_from_db()
        self.assertIsNotNone(contract)
        self.assertTrue(shipping.assisted_shipping_dispatch_ready(order))
        return order

    def assert_english_event(self, order, kind, prefix):
        event = IgLifecycleEvent.objects.get(order=order, kind=kind)
        self.assertEqual(event.locale, "en")
        self.assertTrue(event.payload[lifecycle.MESSAGE_SNAPSHOT_KEY].startswith(prefix), event.payload)
        self.assertEqual(lifecycle._base_message(event), event.payload[lifecycle.MESSAGE_SNAPSHOT_KEY])
        return event

    def test_actual_english_paid_card_owns_thanks_ttn_and_aftercare_with_stale_russian_profile(self):
        _case, order = self.english_paid_case()
        # The payment producer must create this event in the paid transaction;
        # reconciliation must not be needed to repair a missing first receipt.
        self.assert_english_event(order, IgLifecycleEvent.Kind.PAYMENT_VERIFIED,
            "Thank you, payment received.")
        order = self.verified_tracking(order)
        event, created = lifecycle.ensure_lifecycle_event(order, IgLifecycleEvent.Kind.TTN_CREATED,
            payload={"tracking_number": order.tracking_number})
        self.assertTrue(created)
        self.assertIsNotNone(event)
        self.assert_english_event(order, IgLifecycleEvent.Kind.TTN_CREATED, "Your order #")
        service = NovaPoshtaService()
        with patch("management.services.instagram_bot._provider_http", return_value=(200,
                json.dumps({"message_id": "native-partial-locale-delivered"}))), \
                patch.object(service, "_send_admin_delivery_notification"), \
                patch.object(service, "_send_delivery_notification"):
            self.assertTrue(service._apply_tracking_info_to_order(order, {
                "Number": order.tracking_number, "Ref": order.nova_poshta_document_ref,
                "Status": "Отримано", "StatusCode": "9", "PayerType": "Recipient", "Cost": "100.00",
                "DateLastMovementStatus": self.now.isoformat()}))
        self.assert_english_event(order, IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED,
            "Thank you for your order #")

    def test_paid_card_language_survives_expiry_and_existing_event_copy_is_immutable(self):
        _case, order = self.english_paid_case()
        payment = self.assert_english_event(order, IgLifecycleEvent.Kind.PAYMENT_VERIFIED,
            "Thank you, payment received.")
        original = (payment.locale, dict(payment.payload), payment.final_text)
        self.now += timedelta(days=2)
        # Profile changes cannot rewrite a stored receipt or become language
        # authority for a newly created shipment of this paid selection.
        self.client_row.language = "uk"
        self.client_row.save(update_fields=["language"])
        replay, created = lifecycle.ensure_lifecycle_event(order, IgLifecycleEvent.Kind.PAYMENT_VERIFIED,
            payload={"attempt_id": payment.payload["attempt_id"]})
        self.assertFalse(created)
        self.assertEqual(replay.pk, payment.pk)
        payment.refresh_from_db()
        self.assertEqual((payment.locale, payment.payload, payment.final_text), original)
        order = self.verified_tracking(order)
        event, created = lifecycle.ensure_lifecycle_event(order, IgLifecycleEvent.Kind.TTN_CREATED,
            payload={"tracking_number": order.tracking_number})
        self.assertTrue(created)
        self.assertEqual(event.locale, "en")
        self.assertTrue(event.payload[lifecycle.MESSAGE_SNAPSHOT_KEY].startswith("Your order #"))
