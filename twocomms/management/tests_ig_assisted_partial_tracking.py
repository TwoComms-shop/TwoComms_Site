"""Native selected paid Orders need actual shipping authority before tracking."""
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch
import json

from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from management.models import IgFunnelStepEvent, IgLifecycleEvent, IgOrderAssignment
from management.services import ig_assisted_partial_shipping as shipping
from management.services import ig_lifecycle as lifecycle
from management.services import ig_order_fulfillment as fulfillment
from management.services.ig_commercial_episodes import sync_episode_fulfillment
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.nova_poshta_service import NovaPoshtaService
from orders.models import PaymentSideEffectJob


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class NativePartialTrackingTests(PartialCommerceFixture, TransactionTestCase):
    def paid_selected_order(self):
        selected = self.selected_case()
        prepared = self.prepare_selection(selected["selection"])
        order = self.pay_prepared(prepared)
        self.assertIsNotNone(shipping.assisted_shipping_contract(order))
        return order

    def retained_tracking(self, order, *, review=True):
        order.tracking_number = "20450000000001"
        order.nova_poshta_document_ref = "actual-retained-partial-document"
        if review:
            order.payment_payload = {**order.payment_payload, "ig_assisted_shipping_review": {
                "state": "requires_review", "reason": "assisted_customer_shipping_maximum_exceeded",
                "tracking_number": order.tracking_number, "document_ref": order.nova_poshta_document_ref,
                "payer_type": "Recipient", "carrier_cost": "121.00"}}
        # Preserve the provider's audit reference without asserting dispatch.
        order.save(update_fields=["tracking_number", "nova_poshta_document_ref", "payment_payload"])
        order.refresh_from_db()
        return order

    def assert_no_shipping_events(self, order):
        kinds = [IgLifecycleEvent.Kind.TTN_CREATED, IgLifecycleEvent.Kind.PARCEL_ARRIVED,
            IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED]
        self.assertFalse(IgLifecycleEvent.objects.filter(order=order, kind__in=kinds).exists())
        self.assertFalse(IgFunnelStepEvent.objects.filter(episode__intended_order=order,
            event_type__in=[IgFunnelStepEvent.Type.TTN_CREATED, IgFunnelStepEvent.Type.DELIVERED]).exists())

    def resolved_order(self):
        order = self.retained_tracking(self.paid_selected_order())
        contract = shipping.assisted_shipping_contract(order)
        with patch.object(NovaPoshtaService, "get_tracking_info", return_value={
                "Number": order.tracking_number, "Ref": order.nova_poshta_document_ref,
                "PayerType": "Recipient", "Cost": "100.00"}) as carrier:
            shipping.resolve_assisted_shipping_review(order.pk, actor=self.manager,
                expected_document_ref=order.nova_poshta_document_ref,
                expected_terms_digest=contract["terms_digest"])
        carrier.assert_called_once()
        order.refresh_from_db()
        self.assertTrue(shipping.assisted_shipping_dispatch_ready(order))
        return order

    def test_retained_over_cap_ttn_never_projects_shipping_or_aftercare(self):
        order = self.retained_tracking(self.paid_selected_order())
        self.assertFalse(shipping.assisted_shipping_dispatch_ready(order))
        assignment = IgOrderAssignment.objects.get(order=order, unassigned_at__isnull=True)
        self.assertEqual(list(fulfillment._event_specs(assignment, now=self.now)), [])
        for kind in (IgLifecycleEvent.Kind.TTN_CREATED, IgLifecycleEvent.Kind.PARCEL_ARRIVED,
                IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED):
            event, created = lifecycle.ensure_lifecycle_event(order, kind,
                payload={"tracking_number": order.tracking_number})
            self.assertIsNone(event)
            self.assertFalse(created)
        pending = SimpleNamespace(kind="ttn_assigned", payload={"tracking_number": order.tracking_number})
        self.assertFalse(fulfillment._matches_current_fulfillment(pending, order))
        sync_episode_fulfillment(order, source="native_partial_shipping_review")
        service = NovaPoshtaService()
        with patch.object(service, "get_tracking_info") as carrier, \
                patch.object(service, "_send_status_notification") as status, \
                patch.object(service, "_send_delivery_notification") as delivered:
            self.assertFalse(service.update_order_tracking_status(order))
        carrier.assert_not_called()
        status.assert_not_called()
        delivered.assert_not_called()
        self.assertFalse(PaymentSideEffectJob.objects.filter(order=order,
            kind=PaymentSideEffectJob.Kind.ORDER_TELEGRAM_NOTIFICATION,
            payload__notification_type="ttn_added").exists())
        self.assert_no_shipping_events(order)
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "20450000000001")
        self.assertNotEqual(order.status, "ship")
        self.assertIsNone(order.tracking_status_code)

    def test_unknown_actual_fee_ttn_is_not_eligible_for_provider_batch(self):
        order = self.retained_tracking(self.paid_selected_order(), review=False)
        self.assertFalse(shipping.assisted_shipping_dispatch_ready(order))
        service = NovaPoshtaService()
        with patch.object(service, "get_tracking_info_batch") as carrier:
            result = service.update_all_tracking_statuses()
        carrier.assert_not_called()
        self.assertEqual((result["total_orders"], result["processed"], result["updated"]), (0, 0, 0))
        self.assert_no_shipping_events(order)

    def test_locked_tracking_application_rechecks_native_shipping_review(self):
        order = self.retained_tracking(self.paid_selected_order())
        service = NovaPoshtaService()
        result = service._apply_tracking_update(order.pk, "Отримано", "", 9, "Отримано",
            provider_event_at=self.now)
        self.assertIsNone(result)
        order.refresh_from_db()
        self.assertIsNone(order.tracking_status_code)
        self.assertNotEqual(order.status, "done")
        self.assert_no_shipping_events(order)

    def test_even_stale_delivered_fields_do_not_close_review_shipping_episode(self):
        order = self.retained_tracking(self.paid_selected_order())
        order.status, order.tracking_status_code = "done", 9
        order.tracking_terminal_at = self.now
        order.save(update_fields=["status", "tracking_status_code", "tracking_terminal_at"])
        episode = sync_episode_fulfillment(order, source="native_partial_stale_carrier_fields")
        self.assertEqual(episode.state, "order_created")
        self.assert_no_shipping_events(order)

    def test_native_under_cap_fee_proof_allows_normal_delivery_tracking(self):
        order = self.resolved_order()
        event, created = lifecycle.ensure_lifecycle_event(order, IgLifecycleEvent.Kind.TTN_CREATED,
            payload={"tracking_number": order.tracking_number})
        self.assertIsNotNone(event)
        self.assertTrue(created)
        sync_episode_fulfillment(order, source="native_partial_shipping_verified")
        self.assertTrue(IgFunnelStepEvent.objects.filter(episode__intended_order=order,
            event_type=IgFunnelStepEvent.Type.TTN_CREATED).exists())
        service = NovaPoshtaService()
        with patch("management.services.instagram_bot._provider_http", return_value=(200,
                json.dumps({"message_id": "native-partial-tracking-delivered"}))), \
                patch.object(service, "_send_admin_delivery_notification") as admin, \
                patch.object(service, "_send_delivery_notification") as delivered:
            self.assertTrue(service._apply_tracking_info_to_order(order, {
                "Number": order.tracking_number, "Status": "Отримано", "StatusCode": "9",
                "Ref": order.nova_poshta_document_ref, "PayerType": "Recipient", "Cost": "100.00",
                "DateLastMovementStatus": self.now.isoformat()}))
        admin.assert_called_once()
        delivered.assert_called_once()
        order.refresh_from_db()
        self.assertEqual((order.status, order.tracking_status_code), ("done", 9))
        self.assertTrue(IgLifecycleEvent.objects.filter(order=order,
            kind=IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED).exists())

    def test_actual_carrier_reweigh_over_cap_retains_ttn_without_tracking_effects(self):
        order = self.resolved_order()
        service = NovaPoshtaService()
        with patch.object(service, "_send_admin_delivery_notification") as admin, \
                patch.object(service, "_send_delivery_notification") as delivered:
            self.assertFalse(service._apply_tracking_info_to_order(order, {
                "Number": order.tracking_number, "Ref": order.nova_poshta_document_ref,
                "Status": "Отримано", "StatusCode": "9", "PayerType": "Recipient",
                "Cost": "121.00", "DateLastMovementStatus": self.now.isoformat()}))
        admin.assert_not_called()
        delivered.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "20450000000001")
        self.assertEqual(order.payment_payload["ig_assisted_shipping_review"]["state"], "requires_review")
        self.assertFalse(shipping.assisted_shipping_dispatch_ready(order))
        self.assertIsNone(order.tracking_status_code)
        self.assertNotEqual(order.status, "done")
        self.assert_no_shipping_events(order)

    def test_queued_ttn_before_fee_review_sends_zero_then_resolved_ship_notifies_once(self):
        # This explicitly exercises the existing compatibility signal channel,
        # as orders.tests.test_order_telegram_side_effects does. AppConfig keeps
        # it dormant in production; a low-level status save does not opt it in.
        from orders.signals import track_order_changes  # noqa: F401
        from orders.payment_side_effects import enqueue_order_telegram_notification, process_payment_side_effect_job
        from orders.status_management import apply_order_status_update
        order = self.resolved_order()
        profile = order.user.userprofile
        profile.telegram_id = 100500
        profile.save(update_fields=["telegram_id"])
        queued, created = enqueue_order_telegram_notification(order.pk, "ttn_added",
            transition_version=order.updated.isoformat())
        self.assertTrue(created)
        # Real later carrier observation invalidates the queued readiness.
        NovaPoshtaService()._apply_tracking_info_to_order(order, {
            "Number": order.tracking_number, "Ref": order.nova_poshta_document_ref,
            "PayerType": "Recipient", "Cost": "121.00", "Status": "У дорозі", "StatusCode": "5"})
        with patch("orders.telegram_notifications.TelegramNotifier.send_ttn_added_notification") as ttn:
            self.assertEqual(process_payment_side_effect_job(queued.pk, now=self.now), "done")
            self.assertEqual(process_payment_side_effect_job(queued.pk, now=self.now), "done")
        ttn.assert_not_called()
        queued.refresh_from_db()
        self.assertIsNone(queued.provider_io_started_at)
        order.refresh_from_db()
        self.assertEqual(order.payment_payload["ig_assisted_shipping_review"]["state"], "requires_review")
        contract = shipping.assisted_shipping_contract(order)
        self.now += timedelta(seconds=1)
        with patch.object(NovaPoshtaService, "get_tracking_info", return_value={
                "Number": order.tracking_number, "Ref": order.nova_poshta_document_ref,
                "PayerType": "Recipient", "Cost": "100.00"}):
            shipping.resolve_assisted_shipping_review(order.pk, actor=self.manager,
                expected_document_ref=order.nova_poshta_document_ref,
                expected_terms_digest=contract["terms_digest"])
        previous_jobs = set(PaymentSideEffectJob.objects.filter(order=order,
            kind=PaymentSideEffectJob.Kind.ORDER_TELEGRAM_NOTIFICATION).values_list("pk", flat=True))
        transition = apply_order_status_update(order.pk, status="ship")
        self.assertTrue(transition["status_changed"])
        fresh_jobs = list(PaymentSideEffectJob.objects.filter(order=order,
            kind=PaymentSideEffectJob.Kind.ORDER_TELEGRAM_NOTIFICATION,
            payload__notification_type="status_update").exclude(pk__in=previous_jobs))
        self.assertEqual(len(fresh_jobs), 1, [job.payload for job in fresh_jobs])
        fresh = fresh_jobs[0]
        # The signal persists the active Django locale's native status display,
        # so do not look it up using a hard-coded Ukrainian UI label.
        self.assertEqual(fresh.payload["new_status"], str(transition["order"].get_status_display()))
        with patch("orders.telegram_notifications.TelegramNotifier.send_order_status_update", return_value="sent") as status:
            self.assertEqual(process_payment_side_effect_job(fresh.pk, now=self.now), "done")
            self.assertEqual(process_payment_side_effect_job(fresh.pk, now=self.now), "done")
        status.assert_called_once()
        order.refresh_from_db()
        self.assertEqual((order.status, order.tracking_number), ("ship", "20450000000001"))

    def test_actual_staff_ship_view_and_existing_reconciler_notify_after_native_resolution(self):
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        order = self.retained_tracking(self.paid_selected_order())
        with override_settings(ROOT_URLCONF="twocomms.urls"):
            endpoint = reverse("admin_update_order_status")
            denied = self.client.post(endpoint, {"order_id": order.pk, "status": "ship"},
                HTTP_HOST="twocomms.shop")
            self.assertEqual(denied.status_code, 200, denied.content)
            self.assertFalse(denied.json()["success"])
            order.refresh_from_db()
            self.assertNotEqual(order.status, "ship")
            self.assert_no_shipping_events(order)
            contract = shipping.assisted_shipping_contract(order)
            with patch.object(NovaPoshtaService, "get_tracking_info", return_value={
                    "Number": order.tracking_number, "Ref": order.nova_poshta_document_ref,
                    "PayerType": "Recipient", "Cost": "100.00"}):
                shipping.resolve_assisted_shipping_review(order.pk, actor=self.manager,
                    expected_document_ref=order.nova_poshta_document_ref,
                    expected_terms_digest=contract["terms_digest"])
            accepted = self.client.post(endpoint, {"order_id": order.pk, "status": "ship"},
                HTTP_HOST="twocomms.shop")
            self.assertEqual(accepted.status_code, 200, accepted.content)
            self.assertTrue(accepted.json()["success"], accepted.content)
            repaired = reconcile_ig_checkout(limit=20, pull_ambiguous=False)
        self.assertEqual(repaired["ttn_events"], 1, repaired)
        event = IgLifecycleEvent.objects.get(order=order, kind=IgLifecycleEvent.Kind.TTN_CREATED)
        with patch("management.services.instagram_bot.get_page_token", return_value="native-tracking-token"), \
                patch("management.services.instagram_bot._provider_http", return_value=(200,
                    json.dumps({"message_id": "native-resolved-shipping-ttn"}))) as provider:
            self.assertEqual(lifecycle.dispatch_lifecycle_event(event.pk), "sent")
            self.assertEqual(lifecycle.dispatch_lifecycle_event(event.pk), "sent")
        provider.assert_called_once()
        order.refresh_from_db()
        self.assertEqual((order.status, order.tracking_number), ("ship", "20450000000001"))
