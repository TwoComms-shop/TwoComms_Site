from decimal import Decimal
from datetime import timedelta
from contextlib import contextmanager
from unittest.mock import patch

from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.ig_bot_models import IgClient, IgOrderCustomerEvent
from management.models import InstagramBotSettings
from management.services.ig_order_assignments import link_order_to_client, unlink_order_from_client


@override_settings(ROOT_URLCONF="twocomms.urls")
class IgOrderFulfillmentTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from orders.models import Order

        self.manager = get_user_model().objects.create_user("fulfillment-manager", password="x", is_staff=True)
        self.client.force_login(self.manager)
        InstagramBotSettings.objects.update_or_create(
            pk=1,
            defaults={"is_enabled": True},
        )
        self.ig_client = IgClient.get_or_create_for_sender("fulfillment-locale-client")
        self.ig_client.language = "en"
        self.ig_client.last_message_at = timezone.now()
        self.ig_client.save(update_fields=["language", "last_message_at", "updated_at"])
        self.order = Order.objects.create(
            order_number="TWC-FULFILLMENT-01",
            full_name="Website buyer",
            phone="380501112233",
            city="Kyiv",
            np_office="Branch 1",
            total_sum=Decimal("790.00"),
            payment_status="paid",
            status="ship",
            tracking_number="20400000000000",
        )

    def test_materializes_english_ttn_once_and_delivers(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        assignment = link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        def send_with_evidence(_settings, _igsid, _message, **kwargs):
            kwargs["provider_message_callback"]("mid.fulfillment.english")
            return True, "", ""

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=send_with_evidence,
        ) as send:
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)
        event = IgOrderCustomerEvent.objects.get(kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.SENT)
        self.assertIn("tracking number for order", event.message_snapshot)
        self.assertIn("has been created", event.message_snapshot)
        self.assertNotIn("on its way", event.message_snapshot)
        self.assertIn("20400000000000", event.message_snapshot)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["sent"], 1)
        reconcile_order_customer_events(order_id=self.order.pk, send=True)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(assignment.version, 1)

    def test_automated_checkout_assignment_uses_canonical_lifecycle_only(self):
        import hashlib

        from management.models import (
            IgCheckoutProposal,
            IgDeal,
            IgOrderAssignment,
            IgOrderAttribution,
        )
        from management.services.ig_commercial_episodes import ensure_episode_for_deal
        from management.services.ig_order_fulfillment import reconcile_order_customer_events
        from orders.models import PaymentAttempt

        deal = IgDeal.objects.create(
            client=self.ig_client,
            status=IgDeal.Status.QUOTED,
            amount=self.order.total_sum,
            requested_payment_amount=self.order.total_sum,
        )
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal,
            commercial_episode=ensure_episode_for_deal(deal),
            catalog_total=self.order.total_sum,
            quoted_total=self.order.total_sum,
            requested_payment_amount=self.order.total_sum,
            items_digest=hashlib.sha256(b"fulfillment-checkout-context").hexdigest(),
        )
        attempt = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"fulfillment-checkout-attempt").hexdigest(),
            full_name=self.order.full_name,
            phone=self.order.phone,
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal"},
            gross_amount=self.order.total_sum,
            payable_amount=self.order.total_sum,
            payment_amount=self.order.total_sum,
            order=self.order,
        )
        proposal.payment_attempt = attempt
        proposal.save(update_fields=["payment_attempt", "updated_at"])
        IgOrderAttribution.objects.create(
            order=self.order,
            client=self.ig_client,
            deal=deal,
            creation_mode="provider_auto",
            payment_source="provider_attempt",
        )

        link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
            source=IgOrderAssignment.Source.PROVIDER_AUTO,
        )

        result = reconcile_order_customer_events(order_id=self.order.pk, send=False)

        self.assertEqual(result["created"], 0)
        self.assertFalse(IgOrderCustomerEvent.objects.filter(order=self.order).exists())

    def test_legacy_provider_auto_assignment_keeps_fulfillment_without_proposal(self):
        from management.models import IgOrderAssignment
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
            source=IgOrderAssignment.Source.PROVIDER_AUTO,
        )

        result = reconcile_order_customer_events(order_id=self.order.pk, send=False)

        self.assertEqual(result["created"], 1)
        self.assertTrue(
            IgOrderCustomerEvent.objects.filter(
                order=self.order,
                kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
            ).exists()
        )

    def test_legacy_manager_review_assignment_keeps_fulfillment_without_proposal(self):
        from management.models import IgOrderAssignment
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
            source=IgOrderAssignment.Source.MANAGER_PAYMENT_REVIEW,
        )

        result = reconcile_order_customer_events(order_id=self.order.pk, send=False)

        self.assertEqual(result["created"], 1)
        self.assertTrue(
            IgOrderCustomerEvent.objects.filter(
                order=self.order,
                kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
            ).exists()
        )

    def test_unlink_cancels_pending_event_and_relink_uses_new_version_key(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        assignment = link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        event = IgOrderCustomerEvent.objects.get(kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED)
        unlink_order_from_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
            expected_version=assignment.version,
            reason_code="manager_correction",
            reason="Wrong customer selected",
        )
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.CANCELLED)
        second_client = IgClient.get_or_create_for_sender("fulfillment-relink-client")
        relinked = link_order_to_client(self.order, client=second_client, actor=self.manager)
        self.assertEqual(relinked.version, 3)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        self.assertEqual(IgOrderCustomerEvent.objects.filter(order=self.order, kind="ttn_assigned").count(), 2)

    def test_legacy_backfill_does_not_materialize_historical_notifications(self):
        from management.ig_bot_models import IgOrderAssignment
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        self.order.status = "done"
        self.order.save(update_fields=["status"])
        link_order_to_client(
            self.order,
            client=self.ig_client,
            source=IgOrderAssignment.Source.LEGACY_ATTRIBUTION,
            reason_code="legacy_attribution",
            reason="Imported by migration 0119",
        )

        result = reconcile_order_customer_events(order_id=self.order.pk, send=False)

        self.assertEqual(result["created"], 0)
        self.assertFalse(IgOrderCustomerEvent.objects.filter(order=self.order).exists())

    def test_opted_in_after_opt_out_remains_eligible(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        now = timezone.now()
        self.ig_client.opted_out_at = now - timedelta(hours=2)
        self.ig_client.opted_in_at = now - timedelta(hours=1)
        self.ig_client.save(update_fields=["opted_out_at", "opted_in_at", "updated_at"])
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=lambda _settings, _igsid, _message, **kwargs: (
                kwargs["provider_message_callback"]("mid.fulfillment.optin")
                or (True, "", "")
            ),
        ) as send:
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        self.assertEqual(result["sent"], 1)
        send.assert_called_once()

    def test_blocked_client_event_is_cancelled_without_send(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        self.ig_client.is_blocked = True
        self.ig_client.save(update_fields=["is_blocked", "updated_at"])
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        with patch("management.services.instagram_bot.send_text") as send:
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(order=self.order)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.CANCELLED)
        self.assertEqual(result["cancelled"], 1)
        send.assert_not_called()

    def test_closed_response_window_routes_event_to_manager_review(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        self.ig_client.last_message_at = timezone.now() - timedelta(hours=24)
        self.ig_client.save(update_fields=["last_message_at", "updated_at"])
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        with patch("management.services.instagram_bot.send_text") as send:
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(order=self.order)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.MANAGER_REVIEW)
        self.assertEqual(result["manager_review"], 1)
        self.assertIn("response window", event.last_error)
        send.assert_not_called()

    def test_final_send_boundary_rechecks_response_window_with_fresh_time(self):
        from management.services.ig_order_fulfillment import (
            RESPONSE_WINDOW,
            deliver_event,
            reconcile_order_customer_events,
        )

        initial_now = timezone.now()
        self.ig_client.last_message_at = initial_now - RESPONSE_WINDOW + timedelta(
            minutes=1
        )
        self.ig_client.save(update_fields=["last_message_at", "updated_at"])
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(
            order_id=self.order.pk,
            send=False,
            now=initial_now,
        )
        event = IgOrderCustomerEvent.objects.get(order=self.order)
        callback_calls = []

        def send_at_final_boundary(_settings, _igsid, _message, **kwargs):
            with kwargs["permission_boundary_factory"]() as allowed:
                if allowed:
                    callback_calls.append("provider")
                    kwargs["provider_message_callback"]("must-not-send")
                    return True, "", ""
            return False, "cancelled", "permission_epoch_changed"

        with (
            patch(
                "management.services.instagram_bot.send_text",
                side_effect=send_at_final_boundary,
            ),
            patch(
                "management.services.ig_order_fulfillment.timezone.now",
                return_value=initial_now + timedelta(minutes=2),
            ),
        ):
            result = deliver_event(event.pk, now=initial_now)

        event.refresh_from_db()
        self.assertEqual(result, "manager_review")
        self.assertEqual(event.state, IgOrderCustomerEvent.State.MANAGER_REVIEW)
        self.assertIn("response window", event.last_error)
        self.assertEqual(callback_calls, [])

    def test_delivery_uses_final_assignment_boundary_and_persists_provider_id(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        def send_with_evidence(_settings, _igsid, _message, **kwargs):
            self.assertIn("permission_boundary_factory", kwargs)
            callback = kwargs.get("provider_message_callback")
            self.assertIsNotNone(callback)
            callback("mid.fulfillment.1")
            with kwargs["permission_boundary_factory"]() as allowed:
                self.assertTrue(allowed)
            return True, "", ""

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=send_with_evidence,
        ):
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(order=self.order)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(event.provider_message_id, "mid.fulfillment.1")

    def test_multi_chunk_delivery_preserves_first_exact_provider_id(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        first_provider_id = "a" * 160
        second_provider_id = "b" * 160

        def send_with_two_receipts(_settings, _igsid, _message, **kwargs):
            callback = kwargs["provider_message_callback"]
            callback(first_provider_id)
            callback(second_provider_id)
            return True, "", ""

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=send_with_two_receipts,
        ):
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(order=self.order)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(event.provider_message_id, first_provider_id)
        self.assertEqual(
            event.delivery_provider_message_ids,
            [first_provider_id, second_provider_id],
        )

    def test_http_success_without_provider_message_id_is_ambiguous(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        with patch(
            "management.services.instagram_bot.send_text",
            return_value=(True, "", ""),
        ):
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(order=self.order)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.AMBIGUOUS)
        self.assertEqual(event.provider_message_id, "")
        self.assertIn("message_id", event.last_error)

    def test_partial_delivery_keeps_provider_ids_and_is_ambiguous(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        def send_partial(_settings, _igsid, _message, **kwargs):
            kwargs["provider_message_callback"]("mid.fulfillment.partial")
            return False, "unknown", "partial delivery"

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=send_partial,
        ):
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(order=self.order)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.AMBIGUOUS)
        self.assertEqual(event.provider_message_id, "mid.fulfillment.partial")

    def test_confirmed_provider_receipt_survives_crash_before_finish(self):
        from management.services.ig_order_fulfillment import (
            deliver_event,
            reconcile_order_customer_events,
        )

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        event = IgOrderCustomerEvent.objects.get(order=self.order)

        def crash_after_receipt(_settings, _igsid, _message, **kwargs):
            kwargs["provider_message_callback"]("mid.fulfillment.crash")
            persisted = IgOrderCustomerEvent.objects.get(pk=event.pk)
            self.assertEqual(
                persisted.delivery_provider_message_ids,
                ["mid.fulfillment.crash"],
            )
            raise RuntimeError("worker terminated after Meta accepted the chunk")

        with (
            patch(
                "management.services.instagram_bot.send_text",
                side_effect=crash_after_receipt,
            ),
            self.assertRaisesMessage(
                RuntimeError,
                "worker terminated after Meta accepted the chunk",
            ),
        ):
            deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.PROCESSING)
        self.assertEqual(event.provider_message_id, "mid.fulfillment.crash")
        self.assertEqual(
            event.delivery_provider_message_ids,
            ["mid.fulfillment.crash"],
        )

        IgOrderCustomerEvent.objects.filter(pk=event.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )
        with patch("management.services.instagram_bot.send_text") as resend:
            self.assertEqual(deliver_event(event.pk), "ambiguous")
        resend.assert_not_called()

        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.AMBIGUOUS)
        self.assertEqual(
            event.delivery_provider_message_ids,
            ["mid.fulfillment.crash"],
        )

    def test_materialization_limit_cannot_starve_older_assignments(self):
        from orders.models import Order
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        second_client = IgClient.get_or_create_for_sender("fulfillment-older-client")
        second_client.last_message_at = timezone.now()
        second_client.save(update_fields=["last_message_at", "updated_at"])
        older_order = Order.objects.create(
            order_number="TWC-FULFILL-OLDER",
            full_name="Older website buyer",
            phone="380509998877",
            total_sum=Decimal("790.00"),
            payment_status="paid",
            status="ship",
            tracking_number="20400000000001",
        )
        link_order_to_client(older_order, client=second_client, actor=self.manager)
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        reconcile_order_customer_events(limit=1, send=False)
        reconcile_order_customer_events(limit=1, send=False)

        self.assertTrue(IgOrderCustomerEvent.objects.filter(order=older_order).exists())
        self.assertTrue(IgOrderCustomerEvent.objects.filter(order=self.order).exists())

    def test_manual_done_without_carrier_delivery_does_not_create_review_request(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        self.order.status = "done"
        self.order.tracking_status_code = 7
        self.order.save(update_fields=["status", "tracking_status_code"])
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        result = reconcile_order_customer_events(order_id=self.order.pk, send=False)

        self.assertEqual(result["created"], 0)
        self.assertFalse(
            IgOrderCustomerEvent.objects.filter(
                order=self.order,
                kind=IgOrderCustomerEvent.Kind.DELIVERED_REVIEW,
            ).exists()
        )

    def test_carrier_confirmed_done_order_creates_localized_review_request(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        self.order.status = "done"
        self.order.tracking_status_code = 9
        self.order.tracking_terminal_at = timezone.now()
        self.order.save(
            update_fields=["status", "tracking_status_code", "tracking_terminal_at"]
        )
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        event = IgOrderCustomerEvent.objects.get(kind=IgOrderCustomerEvent.Kind.DELIVERED_REVIEW)
        self.assertIn("Thank you", event.message_snapshot)
        self.assertIn("honest review", event.message_snapshot)
        self.assertEqual(event.payload["post_purchase_invitation"]["mode"], "review_only")
        for incentive in ("10%", "@twocomms", "90", "story"):
            self.assertNotIn(incentive, event.message_snapshot)
        self.assertEqual(event.locale, "en")
        self.assertFalse(
            IgOrderCustomerEvent.objects.filter(
                order=self.order,
                kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
            ).exists()
        )

    def test_done_transition_cancels_stale_pending_ttn_before_review_send(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        stale_ttn = IgOrderCustomerEvent.objects.get(
            order=self.order,
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
        )
        self.assertEqual(stale_ttn.state, IgOrderCustomerEvent.State.PENDING)

        self.order.status = "done"
        self.order.tracking_status_code = 9
        self.order.tracking_terminal_at = timezone.now()
        self.order.save(
            update_fields=["status", "tracking_status_code", "tracking_terminal_at"]
        )

        def send_with_evidence(_settings, _igsid, message, **kwargs):
            self.assertIn("Thank you", message)
            kwargs["provider_message_callback"]("mid.fulfillment.review")
            return True, "", ""

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=send_with_evidence,
        ) as send:
            result = reconcile_order_customer_events(
                order_id=self.order.pk,
                send=True,
            )

        stale_ttn.refresh_from_db()
        review = IgOrderCustomerEvent.objects.get(
            order=self.order,
            kind=IgOrderCustomerEvent.Kind.DELIVERED_REVIEW,
        )
        self.assertEqual(stale_ttn.state, IgOrderCustomerEvent.State.CANCELLED)
        self.assertIn("superseded", stale_ttn.last_error)
        self.assertEqual(review.state, IgOrderCustomerEvent.State.SENT)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["cancelled"], 1)
        send.assert_called_once()

    def test_unconfirmed_manager_review_event_remains_audit_only_without_send(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        assignment = link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
        )
        self.order.status = "done"
        self.order.tracking_status_code = 7
        self.order.save(update_fields=["status", "tracking_status_code"])
        event = IgOrderCustomerEvent.objects.create(
            event_key=f"ig-assignment:{assignment.pk}:v{assignment.version}:delivered-review",
            assignment=assignment,
            assignment_version=assignment.version,
            order=self.order,
            client=self.ig_client,
            kind=IgOrderCustomerEvent.Kind.DELIVERED_REVIEW,
            locale="en",
            message_snapshot="Thank you for your order",
            payload={"order_number": self.order.order_number},
            state=IgOrderCustomerEvent.State.MANAGER_REVIEW,
        )

        with patch("management.services.instagram_bot.send_text") as send:
            result = reconcile_order_customer_events(
                order_id=self.order.pk,
                send=True,
            )

        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.MANAGER_REVIEW)
        self.assertEqual(result["cancelled"], 0)
        send.assert_not_called()

    def test_tracking_replacement_cancels_old_pending_ttn_and_materializes_current_one(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        stale_ttn = IgOrderCustomerEvent.objects.get(
            order=self.order,
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
            payload__tracking_number="20400000000000",
        )

        self.order.tracking_number = "20400000000001"
        self.order.save(update_fields=["tracking_number"])

        result = reconcile_order_customer_events(order_id=self.order.pk, send=False)

        stale_ttn.refresh_from_db()
        current_ttn = IgOrderCustomerEvent.objects.get(
            order=self.order,
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
            payload__tracking_number="20400000000001",
        )
        self.assertEqual(stale_ttn.state, IgOrderCustomerEvent.State.CANCELLED)
        self.assertIn("superseded", stale_ttn.last_error)
        self.assertEqual(current_ttn.state, IgOrderCustomerEvent.State.PENDING)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["cancelled"], 1)

    def test_cancelled_transition_cancels_pending_ttn_without_send(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        stale_ttn = IgOrderCustomerEvent.objects.get(
            order=self.order,
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
        )

        self.order.status = "cancelled"
        self.order.save(update_fields=["status"])

        with patch("management.services.instagram_bot.send_text") as send:
            result = reconcile_order_customer_events(
                order_id=self.order.pk,
                send=True,
            )

        stale_ttn.refresh_from_db()
        self.assertEqual(stale_ttn.state, IgOrderCustomerEvent.State.CANCELLED)
        self.assertIn("superseded", stale_ttn.last_error)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["cancelled"], 1)
        send.assert_not_called()

    def test_send_boundary_cancels_ttn_when_order_becomes_done_after_claim(self):
        from orders.models import Order
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)

        def transition_before_meta_io(_settings, _igsid, _message, **kwargs):
            Order.objects.filter(pk=self.order.pk).update(status="done")
            with kwargs["permission_boundary_factory"]() as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "final eligibility check refused delivery"

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=transition_before_meta_io,
        ) as send:
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(
            order=self.order,
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED,
        )
        self.assertEqual(event.state, IgOrderCustomerEvent.State.CANCELLED)
        self.assertIn("superseded", event.last_error)
        self.assertEqual(result["cancelled"], 1)
        send.assert_called_once()

    def test_no_send_materializes_without_consuming_an_attempt(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        result = reconcile_order_customer_events(order_id=self.order.pk, send=False)
        event = IgOrderCustomerEvent.objects.get(kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED)

        self.assertEqual(result["created"], 1)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.PENDING)
        self.assertEqual(event.attempts, 0)

    def test_expired_processing_lease_becomes_ambiguous_without_resend(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events
        from django.utils import timezone

        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False)
        event = IgOrderCustomerEvent.objects.get(kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED)
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.lease_token = "abandoned"
        event.lease_expires_at = timezone.now() - timedelta(minutes=1)
        event.save(update_fields=["state", "lease_token", "lease_expires_at"])

        with patch("management.services.instagram_bot.send_text") as send:
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.AMBIGUOUS)
        self.assertEqual(result["ambiguous"], 1)
        send.assert_not_called()

    def test_direct_delivery_does_not_reclaim_expired_processing_lease(self):
        from management.services.ig_order_fulfillment import (
            deliver_event,
            reconcile_order_customer_events,
        )

        now = timezone.now()
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False, now=now)
        event = IgOrderCustomerEvent.objects.get(
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED
        )
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.lease_token = "expired-direct-worker"
        event.lease_expires_at = now - timedelta(minutes=1)
        event.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )

        def send_with_receipt(_settings, _igsid, _message, **kwargs):
            kwargs["provider_message_callback"]("mid.expired-replay")
            return True, "", ""

        with patch(
            "management.services.instagram_bot.send_text",
            side_effect=send_with_receipt,
        ) as send:
            result = deliver_event(event.pk, send=True, now=now)

        event.refresh_from_db()
        self.assertEqual(result, "ambiguous")
        self.assertEqual(event.state, IgOrderCustomerEvent.State.AMBIGUOUS)
        self.assertEqual(event.lease_token, "")
        self.assertIsNone(event.lease_expires_at)
        send.assert_not_called()

    def test_unlink_preserves_live_processing_delivery_audit(self):
        from management.services.ig_order_fulfillment import (
            reconcile_order_customer_events,
        )

        now = timezone.now()
        assignment = link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
        )
        reconcile_order_customer_events(order_id=self.order.pk, send=False, now=now)
        event = IgOrderCustomerEvent.objects.get(
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED
        )
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.lease_token = "live-unlink-worker"
        event.lease_expires_at = now + timedelta(minutes=1)
        event.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )

        unlink_order_from_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
            expected_version=assignment.version,
            reason_code="manager_correction",
            reason="Order belongs to another Instagram customer.",
        )

        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.PROCESSING)
        self.assertEqual(event.lease_token, "live-unlink-worker")
        self.assertEqual(event.lease_expires_at, now + timedelta(minutes=1))

    def test_reconcile_preserves_live_processing_event_when_truth_changes(self):
        from management.services.ig_order_fulfillment import (
            reconcile_order_customer_events,
        )

        now = timezone.now()
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False, now=now)
        event = IgOrderCustomerEvent.objects.get(
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED
        )
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.lease_token = "live-reconcile-worker"
        event.lease_expires_at = now + timedelta(minutes=1)
        event.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )
        self.order.status = "done"
        self.order.save(update_fields=["status"])

        result = reconcile_order_customer_events(
            order_id=self.order.pk,
            send=False,
            now=now,
        )

        event.refresh_from_db()
        self.assertEqual(result["cancelled"], 0)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.PROCESSING)
        self.assertEqual(event.lease_token, "live-reconcile-worker")

    def test_canonical_handoff_preserves_live_processing_event(self):
        from management.services.ig_order_fulfillment import (
            _cancel_redundant_events,
            reconcile_order_customer_events,
        )

        now = timezone.now()
        assignment = link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
        )
        reconcile_order_customer_events(order_id=self.order.pk, send=False, now=now)
        event = IgOrderCustomerEvent.objects.get(
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED
        )
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.lease_token = "live-canonical-worker"
        event.lease_expires_at = now + timedelta(minutes=1)
        event.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )

        cancelled = _cancel_redundant_events(assignment, now=now)

        event.refresh_from_db()
        self.assertEqual(cancelled, 0)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.PROCESSING)
        self.assertEqual(event.lease_token, "live-canonical-worker")

    def test_send_boundary_blocks_legacy_event_after_canonical_handoff(self):
        import hashlib

        from management.models import (
            IgCheckoutProposal,
            IgDeal,
            IgOrderAssignment,
            IgOrderAttribution,
        )
        from management.services.ig_commercial_episodes import ensure_episode_for_deal
        from management.services.ig_order_fulfillment import (
            _event_send_boundary,
            reconcile_order_customer_events,
        )
        from orders.models import PaymentAttempt

        now = timezone.now()
        assignment = link_order_to_client(
            self.order,
            client=self.ig_client,
            actor=self.manager,
            source=IgOrderAssignment.Source.PROVIDER_AUTO,
        )
        reconcile_order_customer_events(order_id=self.order.pk, send=False, now=now)
        event = IgOrderCustomerEvent.objects.get(
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED
        )
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.lease_token = "canonical-handoff-worker"
        event.lease_expires_at = now + timedelta(minutes=1)
        event.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )

        deal = IgDeal.objects.create(
            client=self.ig_client,
            status=IgDeal.Status.QUOTED,
            amount=self.order.total_sum,
            requested_payment_amount=self.order.total_sum,
        )
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal,
            commercial_episode=ensure_episode_for_deal(deal),
            catalog_total=self.order.total_sum,
            quoted_total=self.order.total_sum,
            requested_payment_amount=self.order.total_sum,
            items_digest=hashlib.sha256(b"late-canonical-handoff").hexdigest(),
        )
        attempt = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"late-canonical-attempt").hexdigest(),
            full_name=self.order.full_name,
            phone=self.order.phone,
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal"},
            gross_amount=self.order.total_sum,
            payable_amount=self.order.total_sum,
            payment_amount=self.order.total_sum,
            order=self.order,
        )
        proposal.payment_attempt = attempt
        proposal.save(update_fields=["payment_attempt", "updated_at"])
        IgOrderAttribution.objects.create(
            order=self.order,
            client=self.ig_client,
            deal=deal,
            creation_mode="provider_auto",
            payment_source="provider_attempt",
        )

        @contextmanager
        def permit_customer_send(*_args, **_kwargs):
            yield True

        boundary_state = {}
        with patch(
            "management.services.ig_reply_boundary.customer_send_boundary",
            side_effect=permit_customer_send,
        ):
            with _event_send_boundary(
                event,
                token=event.lease_token,
                settings_id=1,
                permission=object(),
                now=now,
                boundary_state=boundary_state,
            ) as current:
                self.assertFalse(current)

        self.assertTrue(boundary_state["canonical_handoff"])
        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.PROCESSING)
        self.assertEqual(event.lease_token, "canonical-handoff-worker")

    def test_send_boundary_locks_order_before_assignment_and_event(self):
        from management.ig_bot_models import IgOrderAssignment
        from management.services.ig_order_fulfillment import (
            _event_send_boundary,
            reconcile_order_customer_events,
        )
        from orders.models import Order

        now = timezone.now()
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        reconcile_order_customer_events(order_id=self.order.pk, send=False, now=now)
        event = IgOrderCustomerEvent.objects.get(
            kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED
        )
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.lease_token = "lock-order-worker"
        event.lease_expires_at = now + timedelta(minutes=1)
        event.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )

        lock_order = []
        order_lock = Order.objects.select_for_update
        assignment_lock = IgOrderAssignment.objects.select_for_update
        event_lock = IgOrderCustomerEvent.objects.select_for_update

        @contextmanager
        def permit_customer_send(*_args, **_kwargs):
            yield True

        with patch(
            "management.services.ig_reply_boundary.customer_send_boundary",
            side_effect=permit_customer_send,
        ), patch.object(
            Order.objects,
            "select_for_update",
            side_effect=lambda: lock_order.append("order") or order_lock(),
        ), patch.object(
            IgOrderAssignment.objects,
            "select_for_update",
            side_effect=lambda: lock_order.append("assignment") or assignment_lock(),
        ), patch.object(
            IgOrderCustomerEvent.objects,
            "select_for_update",
            side_effect=lambda: lock_order.append("event") or event_lock(),
        ):
            with _event_send_boundary(
                event,
                token=event.lease_token,
                settings_id=1,
                permission=object(),
                now=now,
                boundary_state={},
            ) as current:
                self.assertTrue(current)

        self.assertEqual(lock_order, ["order", "assignment", "event"])

    def test_global_stop_prevents_customer_delivery(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        InstagramBotSettings.objects.filter(pk=1).update(is_enabled=False)
        link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        with patch("management.services.instagram_bot.send_text") as send:
            result = reconcile_order_customer_events(order_id=self.order.pk, send=True)

        event = IgOrderCustomerEvent.objects.get(kind=IgOrderCustomerEvent.Kind.TTN_ASSIGNED)
        self.assertEqual(event.state, IgOrderCustomerEvent.State.PENDING)
        self.assertEqual(result["paused"], 1)
        send.assert_not_called()

    def test_order_transition_wakes_after_commit_but_creation_does_not(self):
        from orders.models import Order

        with patch(
            "management.services.ig_order_fulfillment.kick_order_fulfillment"
        ) as kick:
            Order.objects.create(
                full_name="Unassigned website buyer",
                phone="380509998877",
                total_sum=Decimal("790.00"),
            )
            self.assertEqual(kick.call_count, 0)

            self.order.shipment_status = "in_transit"
            with self.captureOnCommitCallbacks(execute=True):
                self.order.save(update_fields=["shipment_status"])

        kick.assert_called_once_with(self.order.pk)

    @override_settings(IG_FULFILLMENT_BACKGROUND_WAKE_ENABLED=True)
    def test_legacy_background_wake_flag_never_starts_a_request_owned_thread(self):
        from management.services.ig_order_fulfillment import kick_order_fulfillment

        with patch("threading.Thread") as thread:
            kick_order_fulfillment(self.order.pk)

        thread.assert_not_called()

    def test_manual_order_context_links_without_creating_attribution(self):
        token = signing.dumps({"client_id": self.ig_client.pk}, salt="storefront.manual-order.ig-client")
        response = self.client.post(
            reverse("manual_order_create"),
            data={
                "full_name": "Manual Instagram buyer",
                "phone": "0501112233",
                "delivery_method": "manual",
                "city": "Київ",
                "np_office": "Відділення 1",
                "payment_preset": "cod",
                "sale_source": "Instagram",
                "items": [{"kind": "custom", "title": "Футболка", "unit_price": "790", "qty": 1, "size": "M", "color_name": "Чорний"}],
                "ig_client_id": str(self.ig_client.pk),
                "ig_client_token": token,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        from management.ig_bot_models import IgOrderAssignment, IgOrderAttribution
        from orders.models import Order

        created = Order.objects.get(order_number=response.json()["order_number"])
        assignment = IgOrderAssignment.objects.get(order=created)
        self.assertEqual(assignment.client_id, self.ig_client.pk)
        self.assertEqual(assignment.source, IgOrderAssignment.Source.MANAGER_CREATED)
        self.assertEqual(created.items.count(), 1)
        self.assertFalse(IgOrderAttribution.objects.filter(order=created).exists())
        self.assertEqual(created.payment_status, "unpaid")
