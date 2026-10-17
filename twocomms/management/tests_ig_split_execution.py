"""Native consent, ordinary paid winner and exact parcel execution integration."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
import uuid
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.ig_split_shipping_models import (IgSplitShippingChoice as Choice,
    IgSplitOrderPlan as Plan, IgSplitParcel as Parcel, IgSplitParcelDispatch as Dispatch)
from management.models import (IgCheckoutInventoryReservation as Reservation,
    IgCheckoutProposal, IgOrderShipment, InstagramBotMessage)
from management.services import ig_split_execution as execution
from management.services import ig_split_tracking as tracking
from management.tests_ig_restock_subscriptions import _RestockFixture
from management import tests_ig_split_shipping as _shipping_tests
from management import tests_ig_checkout_generation_runtime as _runtime_tests
from orders.models import Order, PaymentAttempt
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule, VariantBlankLink


class _SplitExecutionFixture(_RestockFixture):
    """Reusable real producer fixture; no authority, inventory or ledger mocks."""
    manager_offer = _shipping_tests.NativeSplitShippingTests.manager_offer
    options = _shipping_tests.NativeSplitShippingTests.options
    offer = _shipping_tests.NativeSplitShippingTests.offer
    complete_input = _shipping_tests.NativeSplitShippingTests.complete_input
    send = _shipping_tests.NativeSplitShippingTests.send
    answer = _shipping_tests.NativeSplitShippingTests.answer
    sent_offer = _shipping_tests.NativeSplitShippingTests.sent_offer
    _delivery_payload = _runtime_tests.CheckoutGenerationRuntimeTests._delivery_payload
    _open = _runtime_tests.CheckoutGenerationRuntimeTests._open
    _create_invoice = _runtime_tests.CheckoutGenerationRuntimeTests._create_invoice

    def setUp(self):
        # Exact native card/invoice helpers override these guards with their
        # own receipt stubs. Ancillary payment callbacks must stay local too.
        for target in ("management.services.instagram_bot._provider_http", "requests.sessions.Session.request"):
            guard = patch(target, side_effect=AssertionError("split execution fixture attempted external transport"))
            guard.start()
            self.addCleanup(guard.stop)
        super().setUp()
        from django.contrib.auth import get_user_model
        self.manager = get_user_model().objects.create_superuser(username="split-execution-manager",
            email="split-execution@example.test", password="test")
        self.canonical("количество 2")
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        VariantSizeRule.objects.create(variant=self.variants[1], fit_code="classic", size="L",
            stock=0, is_enabled=True)
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        self.canonical("Хочу купити")
        self.stocks = []

    def _warehouse(self, *, shared_pool=False):
        from warehouse.models import StorageCategory, StorageSubcategory, StockItem
        category = StorageCategory.objects.create(name="Split native stock", slug="split-native-stock",
            linked_storefront_category=self.category)
        if shared_pool:
            self.variants[1].color = self.variants[0].color
            self.variants[1].save(update_fields=["color"])
            VariantSizeRule.objects.create(variant=self.variants[1], fit_code="classic", size="M",
                stock=0, is_enabled=True)
            self.canonical("измените вторую позицию: размер M, цвет black")
            sub = StorageSubcategory.objects.create(category=category, name="Shared split blank",
                slug="shared-split-blank")
            self.stocks.append(StockItem.objects.create(subcategory=sub, color=self.variants[0].color,
                size="M", quantity=4))
            for variant in self.variants:
                VariantBlankLink.objects.create(variant=variant, option_key="fit=classic", storage_subcategory=sub)
                ProductInventoryPolicy.objects.filter(product=variant.product).update(source="warehouse")
            return
        for index, (variant, size) in enumerate(zip(self.variants, ("M", "L"))):
            sub = StorageSubcategory.objects.create(category=category, name=f"Split blank {index}",
                slug=f"split-blank-{index}")
            self.stocks.append(StockItem.objects.create(subcategory=sub, color=variant.color,
                size=size, quantity=2))
            VariantBlankLink.objects.create(variant=variant, option_key="fit=classic", storage_subcategory=sub)
            ProductInventoryPolicy.objects.filter(product=variant.product).update(source="warehouse")

    def _paid_split_plan(self, *, warehouse=False, shared_pool=False, option="separate", payer="customer", prepay=False, discount=False, bind=True, before_payment_commit=None, existing_offer=None):
        """Return exact real plan/parcels/choice/order/proposal/generation models.

        Only external Instagram and Monobank transports are replaced by native
        response stubs. All source, receipt, quote, stock and payment validators
        execute through the actual production producer paths.
        """
        if warehouse:
            self._warehouse(shared_pool=shared_pool)
        evidence = []
        if discount:
            human = InstagramBotMessage.objects.create(client=self.client_row, sender_id="staff-manager",
                role="manager", source="manual", status="done", text="Домовлена сума замовлення 4000 грн")
            agreed = self.source("Домовились, 4000 грн")
            self.revision(agreed)
            evidence.extend((human.pk, agreed.pk))
        if existing_offer is None:
            offer, receipt = self.sent_offer(payer=payer)
        else:
            from management.ig_split_shipping_models import IgSplitShippingOffer, IgSplitShippingReceipt
            from management.services import ig_split_shipping as shipping
            offer = IgSplitShippingOffer.objects.select_related("decision__request", "decision__actor",
                "actor", "client", "task").get(pk=getattr(existing_offer, "pk", existing_offer))
            self.assertEqual(offer.client_id, self.client_row.pk)
            self.assertTrue(shipping.verified_offer(offer))
            self.assertIsNotNone(shipping._original(offer))
            self.assertFalse(shipping.current_offer_reason(offer, self.settings, self.now))
            receipt = IgSplitShippingReceipt.objects.filter(offer=offer).order_by("-created_at", "-pk").first()
            if receipt is None:
                count, physical = self.send(offer)
                self.assertEqual((count, len(physical)), (1, 1))
                receipt = IgSplitShippingReceipt.objects.get(offer=offer)
            self.assertTrue(shipping._native_receipt(offer, receipt))
        answer, _source, _revision = self.answer(offer, option=option)
        self.assertTrue(answer.ready, answer)
        choice = Choice.objects.get(pk=answer.choice_id)
        self.complete_input()
        # The ordinary quote independently recaptures without an explicit now.
        # Use one real clock after the provider card/reply for every quote,
        # invoice and paid materialization read, rather than a future capture.
        self.now += timedelta(seconds=3)
        quote_clock = patch("django.utils.timezone.now", return_value=self.now)
        quote_clock.start()
        self.addCleanup(quote_clock.stop)
        if prepay:
            # The ordinary policy requires the customer's actual latest
            # direct question; an earlier question cannot survive arbitrary
            # later turns as a fresh financial grant.
            source = self.source("Чи можна 200 грн передоплати, а решту післяплатою?", at=self.now)
            self.revision(source)
            self.complete_input()
            evidence.append(source.pk)
        if not warehouse:
            type(self.variant).objects.filter(pk__in=[row.pk for row in self.variants]).update(stock=2)
            VariantSizeRule.objects.filter(variant__in=self.variants).update(stock=2)
        else:
            # Configured size eligibility remains required by the ordinary
            # quote gate. The actual warehouse pool (2 each or 4 shared) and
            # its finite reservation still own inventory allocation.
            for variant, size in zip(self.variants, ("M", "M" if shared_pool else "L")):
                VariantSizeRule.objects.filter(variant=variant, fit_code="classic", size=size).update(stock=2)
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_checkout import checkout_authority_control
        from management.services.ig_checkout import create_or_update_proposal
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.client_row.refresh_from_db()
        control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=cart)
        self.assertFalse(reasons, reasons)
        self.assertEqual(len(control["items"]), 2)
        evidence = sorted(set(evidence) | set(cart["fence"]["source_ids"]) | {offer.decision.request.source_id})
        with transaction.atomic():
            proposal = create_or_update_proposal(client=self.client_row,
                pay_type="prepayment" if prepay else "online_full",
                requested_payment_amount=Decimal("200.00") if prepay else None,
                item_specs=control["items"], negotiated_total=Decimal("4000.00") if discount else None,
                evidence={"message_ids": evidence}, allow_promo=False,
                source_cart_binding=control["source_cart_binding"], source_cart_capture=control["source_cart_capture"],
                checkout_owner_scope=control["checkout_owner_scope"])
        if prepay:
            self.assertEqual(proposal.payment_policy, proposal.PaymentPolicy.FULL_OR_200_COD)
        generation = self._create_invoice(proposal, invoice_id=f"native-split-paid-invoice-{self.client_row.pk}",
            payment_choice="prepay_200_cod" if prepay else "online_full")
        self.assertIsNotNone(generation)
        from storefront.views.monobank import _apply_payment_attempt_status
        attempt = generation.payment_attempt
        binding_callback = patch("management.services.ig_split_order_binding.try_bind_paid_shipping_order") if not bind else nullcontext()
        with binding_callback as suppressed, transaction.atomic():
            order, created = _apply_payment_attempt_status(attempt, "success",
                payload={"paidAmount": int(attempt.payment_amount * 100)}, source="provider_pull")
            if before_payment_commit is not None:
                before_payment_commit(order, choice)
        if not bind:
            suppressed.assert_called_once_with(order.pk)
        self.assertTrue(created)
        self.assertIsNotNone(order)
        proposal.refresh_from_db()
        generation.refresh_from_db()
        order.refresh_from_db()
        if not bind:
            self.assertFalse(Plan.objects.filter(order=order).exists())
            return {"plan": None, "parcels": [], "choice": choice, "order": order,
                "proposal": proposal, "generation": generation, "receipt": receipt}
        auto_plan = Plan.objects.get(order=order)
        self.assertEqual(auto_plan.choice_id, choice.pk)
        result = execution.bind_split_plan(choice.pk, order.pk, actor=self.manager,
            operation_id=uuid.uuid4(), now=self.now)
        self.assertTrue(result["ready"], result)
        self.assertFalse(result["created"])
        self.assertEqual(result["plan_id"], str(auto_plan.pk))
        plan = Plan.objects.get(pk=result["plan_id"])
        checked = execution.validated_split_plan(plan.pk, now=self.now)
        self.assertTrue(checked["ready"], checked)
        return {"plan": plan, "parcels": checked["parcels"], "choice": choice,
            "order": order, "proposal": proposal, "generation": generation, "receipt": receipt}

    def _dispatch(self, parcel, *, operation_id=None, number=None, fee="70.00", cod=None, at=None):
        actual_now = at or self.now + timedelta(seconds=5)
        with patch("management.services.ig_order_fulfillment.kick_order_fulfillment") as kick, \
                patch("django.utils.timezone.now", return_value=actual_now):
            result = execution.record_split_dispatch(parcel.pk, actor=self.manager,
                operation_id=operation_id or uuid.uuid4(), tracking_number=number or f"2045000000000{1 if parcel.key == 'p1' else 2}",
                customer_delivery_amount=fee, cod_amount=cod if cod is not None else parcel.envelope["cod_amount"],
                now=actual_now)
        if result.get("created"):
            kick.assert_called_once_with(parcel.plan.order_id)
        else:
            kick.assert_not_called()
        return result


class SplitMoneyAllocationTests(SimpleTestCase):
    def test_discount_and_residual_money_are_allocated_once_in_exact_cents(self):
        self.assertEqual(execution.allocate_cents(100, [1, 1, 1]), [34, 33, 33])
        self.assertEqual(sum(execution.allocate_cents(399999, [158000, 258000])), 399999)
        self.assertEqual(execution.allocate_cents(0, [0, 0]), [0, 0])
        for total, weights in ((True, [1]), (100, [0]), (10, [-1, 2])):
            with self.subTest(total=total, weights=weights), self.assertRaises(execution.SplitExecutionError):
                execution.allocate_cents(total, weights)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeSplitExecutionTests(_SplitExecutionFixture, TransactionTestCase):
    reset_sequences = True

    def test_paid_order_readback_preserves_original_offer_names_and_exact_bound_choice(self):
        data = self._paid_split_plan()
        from management.services import ig_split_shipping as shipping
        before = (Order.objects.count(), PaymentAttempt.objects.count(), Reservation.objects.count())
        with patch("management.services.instagram_bot._provider_http") as provider:
            history = shipping.shipping_projection(self.client_row, self.settings, now=self.now)
            plans = execution.execution_projection(self.client_row.pk, now=self.now)
        provider.assert_not_called()
        self.assertEqual(history["status"], "chosen")
        self.assertEqual(history["offer"]["locale"], "uk")
        original = data["choice"].offer.envelope["request"]["lines"]
        self.assertEqual([(row["line_id"], row["label"], row["quantity"]) for row in history["offer"]["lines"]],
            [(str(row["line_id"]), row["label"], row["target"]["quantity"]) for row in original])
        self.assertEqual(history["offer"]["body"], shipping.render_offer(data["choice"].offer)[0])
        expected_labels = {str(row["line_id"]): shipping.reviews._display_label(row["observation"], index)
            for index, row in enumerate(original)}
        for row in history["offer"]["lines"]:
            self.assertEqual(row["display_label"], expected_labels[row["line_id"]])
        for parcel in plans["plans"][0]["parcels"]:
            for row in parcel["lines"]:
                self.assertEqual(row["display_label"], expected_labels[row["line_id"]])
        self.assertEqual(plans["plans"][0]["choice_id"], str(data["choice"].pk))
        self.assertTrue(plans["plans"][0]["ready"])
        self.assertFalse(plans["can_bind_order"])
        self.assertEqual(before, (Order.objects.count(), PaymentAttempt.objects.count(), Reservation.objects.count()))

    def test_native_full_order_manifest_covers_all_expanded_order_rows_without_new_commerce(self):
        data = self._paid_split_plan(discount=True, prepay=True)
        plan, order, parcels = data["plan"], data["order"], data["parcels"]
        allocations = [row for parcel in parcels for row in parcel.envelope["items"]]
        self.assertEqual({row["order_item_id"] for row in allocations}, set(order.items.values_list("pk", flat=True)))
        self.assertEqual(sum(row["quantity"] for row in allocations), 4)
        self.assertEqual(sum(Decimal(parcel.envelope["merchandise_amount"]) for parcel in parcels), Decimal("4000.00"))
        self.assertEqual(sum(Decimal(parcel.envelope["cod_amount"]) for parcel in parcels), Decimal("3800.00"))
        before = (Order.objects.count(), PaymentAttempt.objects.count(), IgCheckoutProposal.objects.count(), Reservation.objects.count())
        replay = execution.bind_split_plan(data["choice"].pk, order.pk, actor=self.manager,
            operation_id=uuid.uuid4(), now=self.now + timedelta(seconds=5))
        self.assertTrue(replay["ready"], replay)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["plan_id"], str(plan.pk))
        self.assertEqual(before, (Order.objects.count(), PaymentAttempt.objects.count(), IgCheckoutProposal.objects.count(), Reservation.objects.count()))

    def test_together_is_one_exact_parcel_and_native_separate_is_two(self):
        data = self._paid_split_plan(option="together", payer="merchant")
        self.assertEqual(len(data["parcels"]), 1)
        result = self._dispatch(data["parcels"][0], fee="0.00")
        self.assertTrue(result["ready"], result)
        self.assertEqual(Dispatch.objects.count(), 1)

    def test_native_dispatch_notifications_keep_signed_manifest_order_for_nonlexical_keys(self):
        from management.models import IgOrderAssignment
        from management.services.ig_order_fulfillment import _event_specs

        original_options = self.options

        def nonlexical_options(*args, **kwargs):
            options = original_options(*args, **kwargs)
            options["separate"]["parcels"][0]["key"] = "p2"
            options["separate"]["parcels"][1]["key"] = "p1"
            return options

        # Change the manager's input before the real offer/card/native answer;
        # the receipt, paid winner, manifest and inventory proofs remain real.
        with patch.object(self, "options", side_effect=nonlexical_options):
            data = self._paid_split_plan()
        parcels = data["parcels"]
        self.assertEqual([row.key for row in parcels], ["p2", "p1"])
        terms = data["choice"].offer.envelope["options"]["separate"]["parcels"]
        self.assertEqual([row["key"] for row in terms], ["p2", "p1"])
        for index, parcel in enumerate(parcels):
            self.assertEqual({item["line_id"] for item in parcel.envelope["items"]}, set(terms[index]["line_ids"]))
            self.assertTrue(self._dispatch(parcel, at=self.now + timedelta(seconds=5 + index))["ready"])

        assignment = IgOrderAssignment.objects.select_related("order", "client").get(order=data["order"])
        with patch("management.services.instagram_bot._provider_http") as provider:
            verified = execution.verified_tracking_plan(assignment.order, now=self.now + timedelta(seconds=10))
            self.assertTrue(verified["ready"], verified)
            self.assertEqual([row.key for row in verified["parcels"]], ["p2", "p1"])
            specs = list(_event_specs(assignment, now=self.now + timedelta(seconds=10)))
        provider.assert_not_called()
        self.assertEqual([row["payload"]["parcel_position"] for row in specs], [1, 2])
        dispatches = {row.parcel_id: row for row in verified["dispatches"]}
        for ordinal, (spec, parcel) in enumerate(zip(specs, parcels), 1):
            self.assertEqual(spec["payload"]["split_dispatch_id"], str(dispatches[parcel.pk].pk))
            self.assertEqual(spec["payload"]["tracking_number"], dispatches[parcel.pk].tracking_number)
            self.assertEqual(spec["payload"]["parcel_count"], 2)
            self.assertIn(f"посилки {ordinal} з 2", spec["message"])

    def test_only_full_native_dispatch_coverage_promotes_order_once_without_changing_money_or_scalar_ttn(self):
        data = self._paid_split_plan()
        order = data["order"]
        from orders.models import PaymentSideEffectJob
        status_jobs = PaymentSideEffectJob.objects.filter(order=order,
            payload__notification_type="status_update").count()
        financial = (order.payment_status, order.pay_type, order.total_sum, order.discount_amount,
            deepcopy(order.payment_payload), order.payment_invoice_id)
        operations = [uuid.uuid4(), uuid.uuid4()]
        with patch("orders.status_management._apply_order_status_update_to_order") as financial_status_helper, \
                patch("orders.status_management._queue_tracking_sync") as legacy_lookup, \
                patch("management.services.instagram_bot._provider_http") as provider:
            self.assertTrue(self._dispatch(data["parcels"][0], operation_id=operations[0])["ready"])
            order.refresh_from_db()
            self.assertEqual(order.status, "new")
            from management.services.ig_marketing_consent import _pre_shipment_reason
            self.assertEqual(_pre_shipment_reason(order), "purchase_already_shipped")
            self.assertFalse(order.tracking_number)
            self.assertEqual(financial, (order.payment_status, order.pay_type, order.total_sum, order.discount_amount,
                order.payment_payload, order.payment_invoice_id))
            financial_status_helper.assert_not_called()
            self.assertTrue(self._dispatch(data["parcels"][1], operation_id=operations[1])["ready"])
            order.refresh_from_db()
            self.assertEqual(order.status, "ship")
            self.assertFalse(order.tracking_number)
            self.assertEqual(financial, (order.payment_status, order.pay_type, order.total_sum, order.discount_amount,
                order.payment_payload, order.payment_invoice_id))
            financial_status_helper.assert_not_called()
            for parcel, operation in zip(data["parcels"], operations):
                result = self._dispatch(parcel, operation_id=operation)
                self.assertTrue(result["ready"], result)
                self.assertFalse(result["created"])
            financial_status_helper.assert_not_called()
            order.refresh_from_db()
            self.assertEqual(order.status, "ship")
        legacy_lookup.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(PaymentSideEffectJob.objects.filter(order=order,
            payload__notification_type="status_update").count(), status_jobs)

    def test_cancellation_after_first_parcel_blocks_full_status_promotion_and_second_dispatch(self):
        data = self._paid_split_plan()
        with patch("orders.status_management._apply_order_status_update_to_order") as financial_status_helper:
            self.assertTrue(self._dispatch(data["parcels"][0])["ready"])
            Order.objects.filter(pk=data["order"].pk).update(status="cancelled")
            result = self._dispatch(data["parcels"][1])
            self.assertFalse(result["ready"], result)
            data["order"].refresh_from_db()
            self.assertEqual(data["order"].status, "cancelled")
            self.assertEqual(Dispatch.objects.count(), 1)
            financial_status_helper.assert_not_called()

    def test_changed_actual_order_item_proof_blocks_second_dispatch_and_full_status_promotion(self):
        data = self._paid_split_plan()
        self.assertTrue(self._dispatch(data["parcels"][0])["ready"])
        payload = deepcopy(data["order"].payment_payload)
        payload["source_cart_provenance"]["item_map"][0]["order_item_id"] = data["order"].items.order_by("pk").last().pk
        Order.objects.filter(pk=data["order"].pk).update(payment_payload=payload)
        result = self._dispatch(data["parcels"][1])
        self.assertFalse(result["ready"], result)
        data["order"].refresh_from_db()
        self.assertEqual(data["order"].status, "new")
        self.assertEqual(Dispatch.objects.count(), 1)

    def test_catalogue_paid_stock_is_not_decremented_again_and_two_parcels_never_overwrite_scalar_ttn(self):
        data = self._paid_split_plan()
        before = list(type(self.variant).objects.filter(pk__in=[row.pk for row in self.variants]).order_by("pk").values_list("stock", flat=True))
        for parcel in data["parcels"]:
            result = self._dispatch(parcel)
            self.assertTrue(result["ready"], result)
        data["order"].refresh_from_db()
        self.assertFalse(data["order"].tracking_number)
        self.assertEqual(IgOrderShipment.objects.filter(order=data["order"], purpose="split_initial").count(), 2)
        self.assertFalse(IgOrderShipment.objects.filter(order=data["order"], supersedes__isnull=False).exists())
        self.assertEqual(before, list(type(self.variant).objects.filter(pk__in=[row.pk for row in self.variants]).order_by("pk").values_list("stock", flat=True)))
        self.assertTrue(execution.verified_tracking_plan(data["order"])["ready"])

    def test_customer_cost_cap_wrong_cod_and_merchant_surcharge_reject_before_shipment(self):
        data = self._paid_split_plan(payer="merchant")
        parcel = data["parcels"][0]
        for kwargs in ({"fee": "0.01"}, {"fee": "0.00", "cod": "1.00"}):
            result = self._dispatch(parcel, **kwargs)
            self.assertFalse(result["ready"], result)
        self.assertFalse(Dispatch.objects.exists())
        self.assertFalse(IgOrderShipment.objects.filter(order=data["order"]).exists())

    def test_actual_dispatch_replays_after_money_change_but_pending_parcel_blocks(self):
        data = self._paid_split_plan(prepay=True)
        parcel = data["parcels"][0]
        operation = uuid.uuid4()
        first = self._dispatch(parcel, operation_id=operation)
        self.assertTrue(first["ready"], first)
        payload = deepcopy(data["order"].payment_payload)
        payload["paid_amount"] = payload["paid_value"] = "400.00"
        Order.objects.filter(pk=data["order"].pk).update(payment_payload=payload)
        data["order"].refresh_from_db()
        self.assertTrue(execution.verified_tracking_plan(data["order"])["ready"])
        self.assertFalse(execution.validated_split_plan(data["plan"].pk)["ready"])
        replay = self._dispatch(parcel, operation_id=operation)
        self.assertTrue(replay["ready"], replay)
        self.assertFalse(replay["created"])
        self.assertFalse(self._dispatch(data["parcels"][1])["ready"])
        self.assertEqual(Dispatch.objects.count(), 1)

    def test_warehouse_dispatch_consumes_only_its_parcel_and_replay_has_one_movement(self):
        from warehouse.models import StockMovement
        data = self._paid_split_plan(warehouse=True)
        parcel = data["parcels"][0]
        operation = uuid.uuid4()
        self.assertEqual(list(Reservation.objects.filter(proposal=data["proposal"]).order_by("pk").values_list("fulfilled_quantity", flat=True)), [0, 0])
        first = self._dispatch(parcel, operation_id=operation)
        self.assertTrue(first["ready"], first)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(sum(Reservation.objects.filter(proposal=data["proposal"]).values_list("fulfilled_quantity", flat=True)), 2)
        self.assertEqual(sum(type(self.stocks[0]).objects.filter(pk__in=[row.pk for row in self.stocks]).values_list("quantity", flat=True)), 2)
        self.assertTrue(self._dispatch(parcel, operation_id=operation)["ready"])
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertTrue(self._dispatch(data["parcels"][1])["ready"])
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_warehouse_shortfall_rolls_back_new_shipment_dispatch_and_remaining_commitment(self):
        from warehouse.models import StockMovement
        data = self._paid_split_plan(warehouse=True)
        type(self.stocks[0]).objects.filter(pk=self.stocks[0].pk).update(quantity=1)
        result = self._dispatch(data["parcels"][0])
        self.assertFalse(result["ready"], result)
        self.assertFalse(Dispatch.objects.exists())
        self.assertFalse(IgOrderShipment.objects.filter(order=data["order"]).exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(sum(Reservation.objects.filter(proposal=data["proposal"]).values_list("fulfilled_quantity", flat=True)), 0)

    def test_shared_paid_pool_partition_preserves_remaining_two_units_until_second_parcel(self):
        from warehouse.models import StockMovement
        from warehouse.services.inventory import protected_stock_quantity
        data = self._paid_split_plan(warehouse=True, shared_pool=True)
        reservation = Reservation.objects.get(proposal=data["proposal"])
        self.assertEqual((reservation.quantity, len(reservation.line_ids), reservation.fulfilled_quantity), (4, 2, 0))
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stocks[0].pk, at=self.now), 4)
        self.assertTrue(self._dispatch(data["parcels"][0])["ready"])
        reservation.refresh_from_db()
        self.stocks[0].refresh_from_db()
        self.assertEqual((reservation.fulfilled_quantity, reservation.state, self.stocks[0].quantity),
            (2, Reservation.State.PAID_COMMITTED, 2))
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stocks[0].pk, at=self.now), 2)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertTrue(self._dispatch(data["parcels"][1])["ready"])
        reservation.refresh_from_db()
        self.stocks[0].refresh_from_db()
        self.assertEqual((reservation.fulfilled_quantity, reservation.state, self.stocks[0].quantity),
            (4, Reservation.State.FULFILLED, 0))
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stocks[0].pk, at=self.now), 0)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_paid_second_parcel_dispatch_after_review_age_keeps_exact_original_terms_and_paid_pool(self):
        data = self._paid_split_plan(warehouse=True, shared_pool=True)
        self.assertTrue(self._dispatch(data["parcels"][0])["ready"])
        later = self.now + timedelta(days=3)
        result = self._dispatch(data["parcels"][1], at=later)
        self.assertTrue(result["ready"], result)
        reservation = Reservation.objects.get(proposal=data["proposal"])
        self.assertEqual((reservation.fulfilled_quantity, reservation.state), (4, Reservation.State.FULFILLED))
        self.assertEqual(Dispatch.objects.count(), 2)

    def test_initial_bind_after_legacy_scalar_ttn_or_done_is_rejected_without_plan(self):
        data = self._paid_split_plan(bind=False)
        for change in ({"tracking_number": "20450000009999"}, {"tracking_number": "", "status": "done"}):
            Order.objects.filter(pk=data["order"].pk).update(**change)
            result = execution.bind_split_plan(data["choice"].pk, data["order"].pk,
                actor=self.manager, operation_id=uuid.uuid4(), now=self.now)
            self.assertEqual(result["reason"], "split_order_already_shipped", result)
        self.assertFalse(Plan.objects.exists())

    def test_initial_bind_after_real_legacy_outbound_journal_is_rejected(self):
        data = self._paid_split_plan(bind=False)
        IgOrderShipment.objects.create(order=data["order"], direction=IgOrderShipment.Direction.OUTBOUND,
            purpose=IgOrderShipment.Purpose.INITIAL, tracking_number="20450000009999",
            source=IgOrderShipment.Source.MANAGER_MANUAL, created_by=self.manager)
        result = execution.bind_split_plan(data["choice"].pk, data["order"].pk,
            actor=self.manager, operation_id=uuid.uuid4(), now=self.now)
        self.assertEqual(result["reason"], "split_order_already_shipped", result)
        self.assertFalse(Plan.objects.exists())

    def test_initial_bind_after_native_warehouse_writeoff_cannot_dispatch_same_paid_units_again(self):
        from management.services.ig_inventory import fulfill_warehouse_reservation
        data = self._paid_split_plan(warehouse=True, bind=False)
        reservation = Reservation.objects.filter(proposal=data["proposal"]).order_by("pk").first()
        fulfill_warehouse_reservation(reservation, order=data["order"], user=self.manager)
        result = execution.bind_split_plan(data["choice"].pk, data["order"].pk,
            actor=self.manager, operation_id=uuid.uuid4(), now=self.now)
        self.assertEqual(result["reason"], "split_order_already_shipped", result)
        self.assertFalse(Plan.objects.exists())

    def test_old_review_cannot_create_a_new_paid_plan_and_existing_paid_plan_still_requires_current_configuration(self):
        from storefront.models import ProductFitOption
        data = self._paid_split_plan(bind=False)
        expired = execution.bind_split_plan(data["choice"].pk, data["order"].pk,
            actor=self.manager, operation_id=uuid.uuid4(), now=self.now + timedelta(days=3))
        self.assertEqual(expired["reason"], "manager_review_expired", expired)
        self.assertFalse(Plan.objects.exists())
        bound = execution.bind_split_plan(data["choice"].pk, data["order"].pk,
            actor=self.manager, operation_id=uuid.uuid4(), now=self.now)
        self.assertTrue(bound["ready"], bound)
        parcel = Parcel.objects.filter(plan_id=bound["plan_id"]).order_by("key").first()
        ProductFitOption.objects.filter(product=self.products[0], code="classic").update(is_active=False)
        rejected = self._dispatch(parcel, at=self.now + timedelta(days=3))
        self.assertEqual(rejected["reason"], "configuration_changed", rejected)
        self.assertFalse(Dispatch.objects.exists())

    def test_two_real_paid_orders_racing_same_tracking_number_have_one_receipt_and_one_rollback(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connection, close_old_connections, connections
        from management.models import IgClient
        if connection.vendor != "mysql" or not connection.features.has_select_for_update:
            self.skipTest("Concurrent unique-key rollback requires native MariaDB transactions")
        first = self._paid_split_plan()
        self.client_row = IgClient.objects.create(igsid="split-second-economic-order", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        self.canonical(f"добавьте {self.product.title}, количество 2, размер M, цвет black, крій classic")
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        self.canonical("Хочу купити")
        second = self._paid_split_plan()
        barrier = Barrier(2)
        actual_now = self.now + timedelta(seconds=5)
        def dispatch(parcel):
            close_old_connections()
            try:
                # Synchronize before SQL locks, never wait while a transaction
                # owns a shared source/actor/stock read fence.
                barrier.wait(timeout=15)
                return execution.record_split_dispatch(parcel.pk, actor=self.manager, operation_id=uuid.uuid4(),
                    tracking_number="20450000009998", customer_delivery_amount="70.00",
                    cod_amount=parcel.envelope["cod_amount"], now=actual_now)
            finally:
                connections.close_all()
        with patch("django.utils.timezone.now", return_value=actual_now), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(dispatch, data["parcels"][0]) for data in (first, second)]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sum(result["ready"] for result in results), 1, results)
        self.assertEqual([result["reason"] for result in results if not result["ready"]], ["split_tracking_already_used"])
        self.assertEqual(Dispatch.objects.filter(tracking_number="20450000009998").count(), 1)
        self.assertEqual(IgOrderShipment.objects.filter(tracking_number="20450000009998").count(), 1)

    def test_legacy_native_waybill_creation_is_blocked_before_any_provider_call(self):
        from orders.nova_poshta_documents import NovaPoshtaDocumentService, NovaPoshtaDocumentError
        data = self._paid_split_plan()
        service = NovaPoshtaDocumentService()
        with patch.object(service, "_request") as provider:
            with self.assertRaisesMessage(NovaPoshtaDocumentError, "split_manifest_required"):
                service.create_waybill(data["order"], {})
        provider.assert_not_called()

    def test_actual_all_parcel_tracking_requires_both_native_tt_ns_and_keeps_stable_manifest_proof(self):
        data = self._paid_split_plan()
        for parcel in data["parcels"]:
            self.assertTrue(self._dispatch(parcel)["ready"])
        before = execution.verified_tracking_plan(data["order"])["proof"]
        service = Mock()
        service._tracking_key.side_effect = lambda number: str(number)
        numbers = list(Dispatch.objects.order_by("parcel__key").values_list("tracking_number", flat=True))
        poll = self.now + timedelta(minutes=10)
        event = self.now + timedelta(minutes=9)
        service.get_tracking_info_batch.return_value = {number: {"Number": number,
            "StatusCode": "9" if index == 0 else "7", "Status": "Carrier native result",
            "DateLastMovementStatus": event.isoformat()} for index, number in enumerate(numbers)}
        with patch("django.utils.timezone.now", return_value=poll):
            first = tracking.track_split_orders(service, onlyorder=data["order"], now=poll)
            self.assertEqual(first["errors"], 0, first)
            data["order"].refresh_from_db()
            self.assertEqual(data["order"].status, "ship")
            self.assertFalse(tracking.split_delivery_confirmed(data["order"]))
            self.assertEqual(execution.verified_tracking_plan(data["order"])["proof"], before)
            service._record_purchase_action.assert_not_called()
        later = poll + timedelta(minutes=6)
        service.get_tracking_info_batch.return_value = {number: {"Number": number,
            "StatusCode": "9", "Status": "Received", "DateLastMovementStatus": later.isoformat()} for number in numbers}
        with patch("django.utils.timezone.now", return_value=later):
            second = tracking.track_split_orders(service, onlyorder=data["order"], now=later)
            self.assertEqual(second["errors"], 0, second)
            data["order"].refresh_from_db()
            self.assertEqual(data["order"].status, "done")
            self.assertTrue(tracking.split_delivery_confirmed(data["order"]))
            self.assertEqual(execution.verified_tracking_plan(data["order"])["proof"], before)
        service._record_purchase_action.assert_called_once()
        service._dispatch_ig_delivery_lifecycle.assert_called_once()
