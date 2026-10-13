"""Physical remaining-unit protection and legacy compatibility for parcel dispatch."""
from decimal import Decimal
from datetime import timedelta
import uuid

from django.test import TransactionTestCase, override_settings

from management.models import IgCheckoutInventoryReservation as Reservation, IgClient
from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal
from management.services.ig_inventory import (commit_proposal_inventory,
    fulfill_order_inventory_reservations, fulfill_warehouse_reservation,
    fulfill_split_parcel_inventory, InventoryReservationError, reserve_proposal_inventory)
from management import tests_ig_inventory_allocations as allocation_tests
from orders.models import Order
from product_catalog.models import ProductInventoryPolicy
from warehouse.models import StockMovement, WriteOffRequest
from warehouse.services.inventory import (adjust_stock_item, protected_stock_quantity,
    reverse_write_off, set_stock_quantity)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class SplitInventoryRemainingTests(TransactionTestCase):
    """Use native allocation models and transactions, including MariaDB locks."""

    def setUp(self):
        allocation_tests.IgInventoryAllocationTests.setUp(self)

    def _item(self, **kwargs):
        return allocation_tests.IgInventoryAllocationTests._item(self, **kwargs)

    def paid(self, quantity=3):
        self.stock_item.quantity = quantity
        self.stock_item.save(update_fields=["quantity", "updated_at"])
        proposal = create_or_update_proposal(client=self.client, pay_type="online_full",
            item_specs=[self._item(quantity=quantity)])
        order = Order.objects.create(full_name="Parcel buyer", phone="+380501112233",
            city="Київ", np_office="Відділення 1", pay_type="online_full", payment_status="paid",
            total_sum=Decimal("1090.00") * quantity)
        commit_proposal_inventory(proposal, order=order)
        return proposal.inventory_reservations.get(), order

    def partially_fulfilled_legacy(self):
        reservation, order = self.paid()
        # Existing partial ledgers must remain protected after migration and
        # during compatibility paths; current split sends use native dispatch.
        Reservation.objects.filter(pk=reservation.pk).update(fulfilled_quantity=1)
        type(self.stock_item).objects.filter(pk=self.stock_item.pk).update(quantity=2)
        reservation.refresh_from_db()
        return reservation, order

    def test_protected_quantity_counts_only_unfulfilled_units_in_current_and_aggregate_reads(self):
        self.partially_fulfilled_legacy()
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock_item.pk), 2)
        from django.db import transaction
        with transaction.atomic():
            type(self.stock_item).objects.select_for_update().get(pk=self.stock_item.pk)
            self.assertEqual(protected_stock_quantity(stock_item_id=self.stock_item.pk, lock=True), 2)

    def test_parcel_inventory_requires_an_existing_dispatch_transaction(self):
        with self.assertRaisesMessage(InventoryReservationError, "split_inventory_requires_dispatch_transaction"):
            fulfill_split_parcel_inventory(None, None, None)
        self.assertFalse(StockMovement.objects.exists())

    def test_manual_writeoff_cannot_steal_remaining_paid_units(self):
        self.partially_fulfilled_legacy()
        with self.assertRaises(ValueError):
            adjust_stock_item(stock_item=self.stock_item, delta=-1)
        self.stock_item.refresh_from_db()
        self.assertEqual(self.stock_item.quantity, 2)
        self.assertFalse(StockMovement.objects.exists())

    def test_manager_recount_cannot_remove_remaining_paid_units(self):
        self.partially_fulfilled_legacy()
        from django.db import transaction
        with self.assertRaises(ValueError):
            with transaction.atomic():
                set_stock_quantity(stock_item=self.stock_item, new_quantity=1)
        self.stock_item.refresh_from_db()
        self.assertEqual(self.stock_item.quantity, 2)

    def test_remaining_units_are_unavailable_to_new_checkout(self):
        self.partially_fulfilled_legacy()
        other = IgClient.get_or_create_for_sender("split-remaining-other")
        with self.assertRaises(CheckoutConfigurationError):
            create_or_update_proposal(client=other, pay_type="online_full", item_specs=[self._item()])
        self.assertFalse(Reservation.objects.filter(proposal__client=other).exists())

    def test_legacy_fulfillment_consumes_only_remaining_quantity_then_replays_without_writeoff(self):
        reservation, order = self.partially_fulfilled_legacy()
        movement = fulfill_warehouse_reservation(reservation, order=order)
        self.assertEqual(movement.delta, -2)
        reservation.refresh_from_db()
        self.stock_item.refresh_from_db()
        self.assertEqual(reservation.fulfilled_quantity, 3)
        self.assertEqual(reservation.state, Reservation.State.FULFILLED)
        self.assertEqual(self.stock_item.quantity, 0)
        self.assertIsNone(fulfill_warehouse_reservation(reservation, order=order))
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_legacy_adapter_binds_existing_remaining_movement_without_second_deduction(self):
        reservation, order = self.partially_fulfilled_legacy()
        writeoff = WriteOffRequest.objects.create(order=order)
        movement = adjust_stock_item(stock_item=self.stock_item, delta=-2, order=order,
            write_off_request=writeoff, reason="order_write_off")
        self.assertEqual(fulfill_order_inventory_reservations(order, write_off_request=writeoff), 1)
        reservation.refresh_from_db()
        self.assertEqual(reservation.stock_movement_id, movement.pk)
        self.assertEqual(reservation.fulfilled_quantity, 3)
        self.assertEqual(fulfill_order_inventory_reservations(order, write_off_request=writeoff), 0)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_catalogue_is_consumed_at_payment_with_exact_order_ownership(self):
        ProductInventoryPolicy.objects.filter(product=self.product).update(source="catalog_variant")
        type(self.variant).objects.filter(pk=self.variant.pk).update(stock=1)
        proposal = create_or_update_proposal(client=self.client, pay_type="online_full", item_specs=[self._item()])
        order = Order.objects.create(full_name="Catalogue buyer", phone="+380501112233",
            city="Київ", np_office="Відділення 1", pay_type="online_full", payment_status="paid",
            total_sum=Decimal("1090.00"))
        commit_proposal_inventory(proposal, order=order)
        reservation = proposal.inventory_reservations.get()
        self.variant.refresh_from_db()
        self.assertEqual(reservation.order_id, order.pk)
        self.assertEqual(reservation.fulfilled_quantity, 1)
        self.assertEqual(self.variant.stock, 0)
        self.assertEqual(commit_proposal_inventory(proposal, order=order), 0)
        self.assertEqual(fulfill_order_inventory_reservations(order), 0)
        self.assertFalse(StockMovement.objects.exists())

    def test_distinct_variants_share_one_full_warehouse_hold_and_generation_reservation(self):
        from django.utils import timezone
        from management.models import IgCheckoutInvoiceGeneration
        from product_catalog.models import VariantBlankLink
        from productcolors.models import ProductColorVariant
        from storefront.models import Product, ProductFitOption, ProductStatus
        from warehouse.services.inventory import protected_stock_quantity

        other = Product.objects.create(title="Second shared-pool shirt", slug="second-shared-pool-shirt",
            category=self.product.category, price=Decimal("1090.00"), status=ProductStatus.PUBLISHED)
        ProductFitOption.objects.create(product=other, code="classic", label="Класичний", is_active=True)
        variant = ProductColorVariant.objects.create(product=other, color=self.color, stock=0, sku="SHARED-SECOND")
        ProductInventoryPolicy.objects.create(product=other, source="warehouse")
        VariantBlankLink.objects.create(variant=variant, option_key="fit=classic", storage_subcategory=self.subcategory)
        specs = [self._item(quantity=2), {"product_id": other.pk, "color_variant_id": variant.pk,
            "qty": 2, "size": "M", "fit_option_code": "classic"}]
        type(self.stock_item).objects.filter(pk=self.stock_item.pk).update(quantity=3)
        short_client = IgClient.get_or_create_for_sender("shared-pool-shortfall")
        with self.assertRaises(CheckoutConfigurationError):
            create_or_update_proposal(client=short_client, pay_type="online_full", item_specs=specs)
        self.assertFalse(Reservation.objects.filter(proposal__client=short_client).exists())
        type(self.stock_item).objects.filter(pk=self.stock_item.pk).update(quantity=4)
        proposal = create_or_update_proposal(client=self.client, pay_type="online_full", item_specs=specs)
        hold = proposal.inventory_reservations.get()
        item_ids = list(proposal.items.order_by("pk").values_list("pk", flat=True))
        self.assertEqual((hold.quantity, hold.line_ids, hold.stock_item_id), (4, item_ids, self.stock_item.pk))
        generation = IgCheckoutInvoiceGeneration.objects.create(proposal=proposal, generation=1,
            series_key=uuid.uuid4().hex, proposal_revision=proposal.revision,
            payment_amount=proposal.quoted_total, provider_call_token=uuid.uuid4().hex,
            expires_at=timezone.now() + timedelta(minutes=25))
        reserve_proposal_inventory(proposal, generation=generation)
        hold.refresh_from_db()
        self.assertEqual((hold.state, hold.release_reason), (Reservation.State.RELEASED, "transferred_to_invoice_generation"))
        owned = proposal.inventory_reservations.get(invoice_generation=generation)
        self.assertEqual((owned.quantity, owned.line_ids, owned.stock_item_id), (4, item_ids, self.stock_item.pk))
        self.assertNotEqual(hold.reservation_fingerprint, owned.reservation_fingerprint)
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock_item.pk), 4)
        self.assertEqual(reserve_proposal_inventory(proposal, generation=generation), [])
        self.assertEqual(proposal.inventory_reservations.filter(invoice_generation=generation).count(), 1)
        owned.refresh_from_db()
        self.assertEqual((owned.quantity, owned.line_ids), (4, item_ids))

    def test_legacy_reversal_restores_protection_for_all_previously_fulfilled_units(self):
        reservation, order = self.paid(1)
        writeoff = WriteOffRequest.objects.create(order=order, status=WriteOffRequest.STATUS_COMPLETED)
        fulfill_warehouse_reservation(reservation, order=order, write_off_request=writeoff)
        reverse_write_off(write_off_request=writeoff)
        reservation.refresh_from_db()
        self.stock_item.refresh_from_db()
        self.assertEqual(reservation.fulfilled_quantity, 0)
        self.assertEqual(reservation.state, Reservation.State.PAID_COMMITTED)
        self.assertEqual(self.stock_item.quantity, 1)
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock_item.pk), 1)
