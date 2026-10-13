"""Native paid execution graph and carrier CAS; external transport is mocked."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.models import IgSplitParcelDispatch, IgSplitParcelTracking
from management.services import ig_split_tracking as tracking
from management.services import ig_split_execution as execution
from management.tests_ig_split_execution import _SplitExecutionFixture
from orders.models import Order, OrderItem
from orders.nova_poshta_service import NovaPoshtaService, NovaPoshtaTransientAPIError
from orders.fulfillment_truth import nova_poshta_order_fulfillment_confirmed, nova_poshta_delivery_confirmed_at


class SplitTrackingTimestampTests(SimpleTestCase):
    def test_native_local_time_and_explicit_offset_share_same_instant(self):
        expected = datetime(2026, 10, 13, 9, tzinfo=datetime_timezone.utc)
        for value in ("13.10.2026 12:00:00", "2026-10-13 12:00:00", "2026-10-13T09:00:00Z"):
            self.assertEqual(tracking._provider_time({"DateLastMovementStatus": value}), expected)

    def test_date_only_unknown_invalid_and_boolean_code_never_prove_delivery(self):
        for value in (None, "2026-10-13", "yesterday", "2026-99-99T12:00:00", "x" * 81):
            self.assertIsNone(tracking._provider_time({"DateLastMovementStatus": value}))
        self.assertIsNone(tracking._code(True))
        self.assertIsNone(tracking._code("delivered"))
        self.assertIsNone(tracking._provider_time({"DateLastMovementStatus": "2026-10-25 03:30:00"}))

    def test_every_required_parcel_and_latest_actual_event_are_required(self):
        now = datetime(2026, 10, 13, 10, tzinfo=datetime_timezone.utc)
        proof = {"parcels": [SimpleNamespace(pk=1), SimpleNamespace(pk=2)],
            "dispatches": [SimpleNamespace(parcel_id=1), SimpleNamespace(parcel_id=2)],
            "order": SimpleNamespace(created=now-timedelta(hours=1))}
        rows = [SimpleNamespace(parcel_id=index, code=str(code), provider_event_at=now-timedelta(minutes=10-index),
            terminal_at=now-timedelta(minutes=10-index), checked_at=now) for index, code in ((1, 9), (2, 11))]
        self.assertEqual(tracking._completion_time(proof, rows, now=now), rows[1].terminal_at)
        for code in ("7", "102", "103", "999", ""):
            rows[1].code = code
            self.assertIsNone(tracking._completion_time(proof, rows, now=now))
        rows[1].code = "10"
        self.assertIsNone(tracking._completion_time(proof, rows[:1], now=now))
        proof["dispatches"].pop()
        self.assertIsNone(tracking._completion_time(proof, rows, now=now))


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class SplitTrackingNativeTests(_SplitExecutionFixture, TransactionTestCase):

    def setUp(self):
        super().setUp()
        self.actor = self.manager
        option = "together" if self._testMethodName == "test_together_one_required_parcel_completes_only_its_verified_full_manifest" else "separate"
        data = self._paid_split_plan(option=option)
        self.client = self.client_row
        self.order, self.plan, self.parcels = data["order"], data["plan"], data["parcels"]
        self.items = list(self.order.items.order_by("pk"))
        self.initial_payment_status = self.order.payment_status
        self.initial_tracking_number = self.order.tracking_number
        Order.objects.filter(pk=self.order.pk).update(status="ship")
        self.order.refresh_from_db()
        self.assertEqual(self.order.created, self.now)
        self.now += timedelta(hours=1)
        self.dispatches = []
        for index, parcel in enumerate(self.parcels):
            if index and self._testMethodName == "test_missing_real_dispatch_cannot_complete_remaining_items":
                continue
            result = self._dispatch(parcel, at=self.now)
            self.assertTrue(result["ready"], result)
            self.dispatches.append(IgSplitParcelDispatch.objects.get(pk=result["dispatch_id"]))
        self.verified = execution.verified_tracking_plan(self.order, now=self.now)
        self.assertTrue(self.verified["ready"], self.verified)
        clock = patch("management.services.ig_split_tracking.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def expected(self, index):
        dispatch = self.dispatches[index]
        row = IgSplitParcelTracking.objects.filter(parcel=dispatch.parcel).first()
        return {"parcel_id": dispatch.parcel_id, "dispatch": tracking._dispatch_token(dispatch),
            "tracking": tracking._tracking_token(row), "proof": dict(self.verified["proof"]), "client_id": self.client.pk}

    def info(self, index, code=9, *, event=None):
        return {"Number": self.dispatches[index].tracking_number, "StatusCode": str(code), "Status": "Carrier status",
            "DateLastMovementStatus": (event or self.now-timedelta(minutes=10-index)).isoformat()}

    def apply(self, index, code=9, *, event=None, expected=None):
        return tracking._apply_result(self.order.pk, expected or self.expected(index),
            self.info(index, code, event=event), now=self.now)

    def test_first_parcel_never_completes_order_all_successes_complete_once_without_payment(self):
        # A later administrative money-state change must not be undone by carrier truth.
        Order.objects.filter(pk=self.order.pk).update(payment_status="unpaid")
        self.order.refresh_from_db()
        self.assertEqual(self.apply(0, 9), (True, False))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "ship")
        self.assertFalse(nova_poshta_order_fulfillment_confirmed(self.order))
        self.assertIsNone(self.order.tracking_terminal_at)
        self.assertEqual(self.apply(1, 10), (True, True))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "done")
        self.assertEqual(self.order.payment_status, "unpaid")
        self.assertEqual(self.order.tracking_number, self.initial_tracking_number)
        self.assertTrue(nova_poshta_order_fulfillment_confirmed(self.order))
        self.assertEqual(nova_poshta_delivery_confirmed_at(self.order), self.now-timedelta(minutes=9))
        self.assertEqual(self.apply(1, 10), (False, False))

    def test_return_refusal_and_unknown_cannot_complete(self):
        self.apply(0)
        for code in (102, 103, 999, 7):
            self.apply(1, code, event=self.now-timedelta(minutes=code % 4))
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, "ship")
            self.assertFalse(tracking.split_delivery_confirmed(self.order))

    def test_missing_real_dispatch_cannot_complete_remaining_items(self):
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.apply(0), (True, False))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "ship")
        self.assertFalse(tracking.split_delivery_confirmed(self.order))

    def test_together_one_required_parcel_completes_only_its_verified_full_manifest(self):
        self.assertEqual(len(self.parcels), 1)
        self.assertEqual(sum(row["quantity"] for row in self.parcels[0].envelope["items"]), 4)
        self.assertEqual(self.apply(0, 11), (True, True))
        self.order.refresh_from_db()
        self.assertTrue(tracking.split_delivery_confirmed(self.order))
        self.assertEqual(self.order.payment_status, self.initial_payment_status)

    def test_unavailable_historical_verifier_and_wrong_order_fail_closed(self):
        with patch.object(execution, "verified_tracking_plan", side_effect=ValueError("source signature invalid")):
            self.assertFalse(tracking.split_delivery_confirmed(self.order))
        rejected = {**self.verified, "order": SimpleNamespace(pk=self.order.pk + 1)}
        with patch.object(execution, "verified_tracking_plan", return_value=rejected):
            self.assertFalse(tracking.split_delivery_confirmed(self.order))

    def test_future_old_equal_conflicting_and_missing_timestamp_do_not_overwrite(self):
        before = self.expected(0)
        self.assertEqual(self.apply(0, event=self.now+timedelta(seconds=1)), (False, False))
        self.assertFalse(IgSplitParcelTracking.objects.exists())
        self.apply(0, 7)
        original = IgSplitParcelTracking.objects.get(parcel=self.parcels[0])
        for event, code in ((self.now-timedelta(hours=1), 9), (original.provider_event_at, 9)):
            self.assertEqual(self.apply(0, code, event=event), (False, False))
        self.assertEqual(tracking._apply_result(self.order.pk, self.expected(0),
            {"StatusCode": "9", "Status": "received"}, now=self.now), (False, False))
        original.refresh_from_db()
        self.assertEqual(original.code, "7")
        self.assertEqual(self.apply(0, 9, expected=before), (False, False))

    def test_dispatch_cas_signed_proof_change_and_rejected_manifest_block_rows(self):
        expected = self.expected(0)
        expected["dispatch"] = (*expected["dispatch"][:2], "different-ttn", expected["dispatch"][3])
        self.assertEqual(self.apply(0, expected=expected), (False, False))
        expected = self.expected(0)
        expected["proof"] = {"manifest": "older-choice"}
        self.assertEqual(self.apply(0, expected=expected), (False, False))
        OrderItem.objects.filter(pk=self.items[0].pk).update(qty=self.items[0].qty + 1)
        self.assertEqual(self.apply(0), (False, False))
        self.assertFalse(IgSplitParcelTracking.objects.exists())

    def test_scalar_received_cannot_bypass_split_and_scalar_query_excludes_it(self):
        Order.objects.filter(pk=self.order.pk).update(status="done", tracking_number="20450000999999",
            tracking_status_code=9, tracking_terminal_at=self.now)
        self.order.refresh_from_db()
        self.assertFalse(nova_poshta_order_fulfillment_confirmed(self.order))
        service = NovaPoshtaService()
        self.assertNotIn(self.order.pk, service.get_orders_with_tracking_queryset().values_list("pk", flat=True))
        self.assertFalse(service._apply_tracking_info_to_order(self.order, self.info(0)))

    def test_batch_polls_child_ttns_without_scalar_and_lifecycle_fires_only_on_final(self):
        service = NovaPoshtaService()
        received = {service._tracking_key(row.tracking_number): self.info(index)
            for index, row in enumerate(self.dispatches)}
        with patch.object(service, "get_tracking_info_batch", return_value=received) as transport, \
                patch.object(service, "_dispatch_ig_delivery_lifecycle") as lifecycle, \
                patch.object(service, "_record_purchase_action") as purchase:
            result = tracking.track_split_orders(service, now=self.now)
        self.assertEqual(result["total_orders"], 1)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(transport.call_args.args[0]), 2)
        lifecycle.assert_called_once()
        purchase.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, self.initial_payment_status)
        self.assertEqual(self.order.tracking_number, self.initial_tracking_number)

    def test_wrong_provider_ttn_and_transient_failure_never_write_delivery(self):
        service = NovaPoshtaService()
        response = {service._tracking_key(self.dispatches[0].tracking_number): {**self.info(0), "Number": "20459999999999"}}
        with patch.object(service, "get_tracking_info_batch", return_value=response):
            result = tracking.track_split_orders(service, now=self.now)
        self.assertEqual(result["row_errors"], 2)
        self.assertFalse(IgSplitParcelTracking.objects.exists())
        with patch.object(service, "get_tracking_info_batch", side_effect=NovaPoshtaTransientAPIError("temporary")):
            result = tracking.track_split_orders(service, now=self.now)
        self.assertEqual(result["transient_provider_errors"], 2)
        self.assertFalse(IgSplitParcelTracking.objects.exists())

    def test_cancelled_order_no_provider_and_legacy_no_plan_remains_confirmed(self):
        Order.objects.filter(pk=self.order.pk).update(status="cancelled")
        service = NovaPoshtaService()
        with patch.object(service, "get_tracking_info_batch") as provider:
            self.assertEqual(tracking.track_split_orders(service, now=self.now)["processed"], 0)
        provider.assert_not_called()
        legacy = SimpleNamespace(status="done", tracking_number="20450000012345", tracking_status_code=9,
            tracking_terminal_at=self.now, tracking_provider_event_at=self.now)
        self.assertTrue(nova_poshta_order_fulfillment_confirmed(legacy))

    def test_split_only_command_does_not_skip_when_scalar_work_is_empty(self):
        from orders.management.commands.update_tracking_statuses import Command
        service = NovaPoshtaService()
        result = {key: 0 for key in ("total_orders", "processed", "updated", "errors")}
        command = Command(stdout=StringIO())
        heartbeat = Mock()
        with patch.object(service, "update_all_tracking_statuses", return_value=result) as update:
            command._update_all_orders(service, False, heartbeat_state=heartbeat)
        update.assert_called_once()
        heartbeat.mark_skipped.assert_not_called()

    def test_order_number_with_blank_scalar_routes_to_child_worker(self):
        from orders.management.commands.update_tracking_statuses import Command
        service = NovaPoshtaService()
        with patch.object(service, "update_order_tracking_status", return_value=True) as update:
            Command(stdout=StringIO())._update_single_order(service, self.order.order_number, False)
        self.assertEqual(update.call_args.args[0].pk, self.order.pk)

    def test_manually_done_order_keeps_polling_until_actual_children_complete(self):
        Order.objects.filter(pk=self.order.pk).update(status="done")
        self.assertIn(self.order.pk, tracking.pending_split_orders(now=self.now).values_list("pk", flat=True))
        self.apply(0)
        self.assertEqual(self.apply(1), (True, True))
        self.order.refresh_from_db()
        self.assertTrue(nova_poshta_order_fulfillment_confirmed(self.order))
        self.assertEqual(self.order.tracking_terminal_at, self.now-timedelta(minutes=9))

    def test_preorder_success_head_cannot_grant_completion_or_aftercare(self):
        event = self.order.created-timedelta(seconds=1)
        self.assertEqual(self.apply(0, 9, event=event), (False, False))
        self.assertFalse(IgSplitParcelTracking.objects.exists())
        # A previously persisted tainted head is independently rejected by truth.
        IgSplitParcelTracking.objects.create(parcel=self.parcels[0], code="9", status="Received",
            provider_event_at=event, terminal_at=event, checked_at=self.now)
        self.assertEqual(self.apply(1), (True, False))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "ship")
        self.assertFalse(tracking.split_delivery_confirmed(self.order))
        self.assertFalse(nova_poshta_order_fulfillment_confirmed(self.order))

    def test_exact_order_creation_boundary_is_valid_carrier_evidence(self):
        event = self.order.created
        self.assertEqual(self.apply(0, 9, event=event), (True, False))
        self.assertEqual(self.apply(1, 11, event=event), (True, True))
        self.order.refresh_from_db()
        self.assertEqual(nova_poshta_delivery_confirmed_at(self.order), event)

    def test_postorder_event_before_late_waybill_registration_remains_valid(self):
        event = self.order.created+timedelta(minutes=30)
        self.assertLess(event, self.dispatches[0].created_at)
        self.assertEqual(self.apply(0, 10, event=event), (True, False))
        self.assertEqual(self.apply(1, 9, event=event), (True, True))
        self.order.refresh_from_db()
        self.assertTrue(nova_poshta_order_fulfillment_confirmed(self.order))
