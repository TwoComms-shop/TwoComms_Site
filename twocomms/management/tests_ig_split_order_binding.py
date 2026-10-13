"""Actual paid-winner shipping binding and independently delivered parcel TTNs."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings

from management.models import (
    IgCheckoutInventoryReservation, IgCheckoutInvoiceGeneration, IgCheckoutProposal,
    IgOrderAssignment, IgOrderCustomerEvent, IgOrderShipment, IgSplitOrderPlan,
    IgSplitParcelDispatch, IgSplitShippingChoice,
)
from management.services import ig_split_execution as execution
from management.services import ig_split_order_binding as binding
from management.tests_ig_split_execution import _SplitExecutionFixture
from orders.fulfillment_truth import nova_poshta_order_fulfillment_confirmed
from orders.models import Order, PaymentAttempt
from warehouse.models import StockItem, StockMovement


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeSplitOrderBindingTests(_SplitExecutionFixture, TransactionTestCase):
    reset_sequences = True

    def _economic_state(self):
        return {
            "orders": Order.objects.count(), "attempts": PaymentAttempt.objects.count(),
            "proposals": IgCheckoutProposal.objects.count(),
            "generations": IgCheckoutInvoiceGeneration.objects.count(),
            "reservations": list(IgCheckoutInventoryReservation.objects.order_by("pk").values(
                "pk", "order_id", "state", "quantity", "fulfilled_quantity", "stock_item_id")),
            "stock": list(StockItem.objects.order_by("pk").values_list("pk", "quantity")),
            "movements": StockMovement.objects.count(), "shipments": IgOrderShipment.objects.count(),
            "dispatches": IgSplitParcelDispatch.objects.count(),
        }

    def _assert_exact_plan(self, data):
        plan = IgSplitOrderPlan.objects.get(order=data["order"])
        self.assertEqual(plan.choice_id, data["choice"].pk)
        self.assertEqual(plan.client_id, self.client_row.pk)
        checked = execution.validated_split_plan(plan.pk, now=self.now)
        self.assertTrue(checked["ready"], checked)
        allocation = [item for parcel in checked["parcels"] for item in parcel.envelope["items"]]
        self.assertEqual({item["order_item_id"] for item in allocation},
            set(data["order"].items.values_list("pk", flat=True)))
        self.assertEqual(sum(item["quantity"] for item in allocation), 4)
        self.assertEqual(plan.envelope["invoice_generation_id"], data["generation"].pk)
        self.assertEqual(plan.envelope["payment_attempt_id"], data["generation"].payment_attempt_id)
        return plan, checked["parcels"]

    def test_paid_on_commit_binds_exact_full_order_only_after_outer_transaction_commits(self):
        observed = []

        def before_commit(order, choice):
            self.assertTrue(transaction.get_connection().in_atomic_block)
            self.assertFalse(IgSplitOrderPlan.objects.filter(order=order).exists())
            generation = IgCheckoutInvoiceGeneration.objects.get(payment_attempt__order=order)
            proposal = IgCheckoutProposal.objects.get(pk=generation.proposal_id)
            self.assertEqual(generation.state, generation.State.PAID_WINNER)
            self.assertEqual(proposal.status, proposal.Status.PAID)
            self.assertEqual(choice.client_id, proposal.client_id)
            observed.append(self._economic_state())

        data = self._paid_split_plan(warehouse=True, before_payment_commit=before_commit)
        self.assertEqual(len(observed), 1)
        self._assert_exact_plan(data)
        self.assertEqual(self._economic_state(), observed[0])
        self.assertEqual(IgSplitOrderPlan.objects.count(), 1)

    def test_recovery_after_lost_binding_wakeup_creates_one_plan_without_provider_or_stock_effects(self):
        data = self._paid_split_plan(warehouse=True, bind=False)
        self.assertFalse(IgSplitOrderPlan.objects.exists())
        before = self._economic_state()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("binding must stay local")) as provider:
            self.assertEqual(binding.reconcile_paid_shipping_plans(), 1)
            plan, _parcels = self._assert_exact_plan(data)
            self.assertEqual(binding.reconcile_paid_shipping_plans(), 0)
            replay = binding.bind_paid_shipping_order(data["order"].pk, now=self.now)
        provider.assert_not_called()
        self.assertTrue(replay["ready"], replay)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["plan_id"], str(plan.pk))
        self.assertEqual(IgSplitOrderPlan.objects.count(), 1)
        self.assertEqual(self._economic_state(), before)

    def test_stale_unbound_agreement_does_not_attach_to_actual_paid_order(self):
        data = self._paid_split_plan(warehouse=True, bind=False)
        before = self._economic_state()
        result = binding.bind_paid_shipping_order(data["order"].pk, now=self.now + timedelta(days=3))
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "shipping_choice_missing_or_ambiguous")
        self.assertFalse(IgSplitOrderPlan.objects.exists())
        self.assertEqual(self._economic_state(), before)

    def test_eleven_authentic_expired_unbound_choices_do_not_starve_new_paid_agreement(self):
        from management.services import ig_split_shipping as shipping
        initial_clock = self.now
        historical = []
        for position in range(11):
            # Each old agreement goes through a new actual review, physical
            # native cost card, receipt, and customer SS1 source/revision.
            self.now = initial_clock + timedelta(minutes=position + 1)
            with patch("django.utils.timezone.now", return_value=self.now):
                offer, receipt = self.sent_offer()
                answer, source, _revision = self.answer(offer)
            self.assertTrue(answer.ready, answer)
            choice = IgSplitShippingChoice.objects.get(pk=answer.choice_id)
            self.assertEqual(choice.source_id, source.pk)
            self.assertEqual(choice.receipt_id, receipt.pk)
            self.assertTrue(shipping.validated_choice_history(choice)["ready"])
            historical.append(choice.pk)
            self.complete_input()
        self.complete_input()
        self.assertEqual(IgSplitShippingChoice.objects.filter(client=self.client_row, plan__isnull=True).count(), 11)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

        self.now = initial_clock + timedelta(days=2)
        # Use the actual later clock for every fresh producer, rather than
        # rewriting old signed rows or mutating their creation timestamps.
        fresh_clock = patch("django.utils.timezone.now", return_value=self.now)
        fresh_clock.start()
        self.addCleanup(fresh_clock.stop)
        self.canonical("Хочу купити")
        data = self._paid_split_plan()
        plan, _parcels = self._assert_exact_plan(data)
        self.assertNotIn(plan.choice_id, historical)
        self.assertEqual(IgSplitShippingChoice.objects.count(), 12)
        self.assertEqual(IgSplitShippingChoice.objects.filter(pk__in=historical, plan__isnull=True).count(), 11)
        self.assertTrue(all(choice.offer.decision.created_at < self.now - timedelta(hours=24)
            for choice in IgSplitShippingChoice.objects.filter(pk__in=historical).select_related("offer__decision")))
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count(), IgCheckoutProposal.objects.count(),
            IgCheckoutInvoiceGeneration.objects.count(), IgSplitOrderPlan.objects.count()), (1, 1, 1, 1, 1))

    def test_ambiguous_native_offer_checkpoint_cannot_bind_retained_choice(self):
        data = self._paid_split_plan(warehouse=True, bind=False)
        task = data["choice"].receipt.task
        task.status = "ambiguous"
        task.save(update_fields=["status", "updated_at"])
        before = self._economic_state()
        result = binding.bind_paid_shipping_order(data["order"].pk, now=self.now)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "shipping_choice_missing_or_ambiguous")
        self.assertFalse(IgSplitOrderPlan.objects.exists())
        self.assertEqual(self._economic_state(), before)

    def test_changed_actual_full_order_item_map_cannot_bind_customer_agreement(self):
        data = self._paid_split_plan(warehouse=True, bind=False)
        item = data["order"].items.order_by("pk").first()
        item.qty += 1
        item.save(update_fields=["qty"])
        before = self._economic_state()
        result = binding.bind_paid_shipping_order(data["order"].pk, now=self.now)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "shipping_choice_missing_or_ambiguous")
        self.assertFalse(IgSplitOrderPlan.objects.exists())
        self.assertEqual(self._economic_state(), before)

    def test_foreign_provenance_owner_cannot_select_an_actual_other_clients_agreement(self):
        data = self._paid_split_plan(warehouse=True, bind=False)
        from management.models import IgClient
        foreign = IgClient.get_or_create_for_sender("split-binding-foreign-owner")
        payload = deepcopy(data["order"].payment_payload)
        payload["source_cart_provenance"]["owner"]["client_id"] = foreign.pk
        data["order"].payment_payload = payload
        data["order"].save(update_fields=["payment_payload"])
        before = self._economic_state()
        result = binding.bind_paid_shipping_order(data["order"].pk, now=self.now)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "shipping_choice_missing_or_ambiguous")
        self.assertFalse(IgSplitOrderPlan.objects.exists())
        self.assertEqual(self._economic_state(), before)

    def test_real_first_parcel_ttn_sends_once_on_canonical_assignment_without_delivery_claim(self):
        from management.services import ig_order_fulfillment as fulfillment
        data = self._paid_split_plan(warehouse=True)
        assignment = IgOrderAssignment.objects.get(order=data["order"], unassigned_at__isnull=True)
        self.assertTrue(fulfillment._uses_canonical_lifecycle(assignment))
        dispatched = self._dispatch(data["parcels"][0])
        self.assertTrue(dispatched["ready"], dispatched)
        dispatch = IgSplitParcelDispatch.objects.get(pk=dispatched["dispatch_id"])
        created = fulfillment.ensure_assignment_events(assignment, now=self.now + timedelta(seconds=6), queue_consent=False)
        self.assertEqual(len(created), 1)
        event = created[0]
        self.assertEqual(event.kind, "ttn_assigned")
        self.assertEqual(event.payload["split_dispatch_id"], str(dispatch.pk))
        self.assertEqual(event.payload["split_dispatch_digest"], dispatch.envelope_hmac)
        self.assertEqual((event.payload["parcel_position"], event.payload["parcel_count"]), (1, 2))
        self.assertIn("Створено ТТН для посилки 1 з 2", event.message_snapshot)
        self.assertIn(dispatch.tracking_number, event.message_snapshot)
        self.assertIn("Номери решти посилок надішлемо окремо", event.message_snapshot)
        self.assertNotIn("отримано", event.message_snapshot.lower())
        self.assertNotIn("відгук", event.message_snapshot.lower())

        def transport(_settings, recipient, message, **kwargs):
            self.assertEqual(recipient, self.client_row.igsid)
            self.assertEqual(message, event.message_snapshot)
            with kwargs["permission_boundary_factory"]() as allowed:
                self.assertTrue(allowed)
                kwargs["provider_message_callback"]("mid.native.split.parcel-one")
            return True, "", ""

        with patch("management.services.instagram_bot.send_text", side_effect=transport) as send:
            self.assertEqual(fulfillment.deliver_event(event.pk, now=self.now + timedelta(seconds=6)), "sent")
            self.assertEqual(fulfillment.deliver_event(event.pk, now=self.now + timedelta(seconds=7)), "skipped")
        send.assert_called_once()
        event.refresh_from_db()
        self.assertEqual(event.state, IgOrderCustomerEvent.State.SENT)
        self.assertEqual(event.provider_message_id, "mid.native.split.parcel-one")
        self.assertEqual(fulfillment.ensure_assignment_events(assignment, now=self.now + timedelta(seconds=8), queue_consent=False), [])
        self.assertEqual(IgOrderCustomerEvent.objects.filter(order=data["order"], kind="ttn_assigned").count(), 1)
        self.assertFalse(IgOrderCustomerEvent.objects.filter(order=data["order"], kind="delivered_review").exists())
        data["order"].refresh_from_db()
        self.assertFalse(nova_poshta_order_fulfillment_confirmed(data["order"]))
        self.assertNotEqual(data["order"].status, "done")
        self.assertEqual(IgSplitParcelDispatch.objects.filter(parcel__plan=data["plan"]).count(), 1)
