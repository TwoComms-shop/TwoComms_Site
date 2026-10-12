"""Exact size capacity survives ordinary reservation and payment lifecycles."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
import time
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection, connections, close_old_connections, transaction
from django.test import TransactionTestCase, override_settings

from management import tests_ig_fulfillment_stock_sync as stock_fixture
from management.models import IgClient, IgCheckoutProposal, IgCheckoutInventoryReservation as Reservation
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_inventory import reserve_proposal_inventory, commit_proposal_inventory, InventoryReservationError
from management.services.ig_inventory_size_capacity import check_catalog_size_capacity, SizeCapacityError
from management.tests_ig_restock_subscriptions import _RestockFixture
from product_catalog.models import VariantSizeRule


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class CatalogSizeCapacityTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    case = stock_fixture.NativeFulfillmentStockSyncTests.case
    preview = stock_fixture.NativeFulfillmentStockSyncTests.preview
    counts = staticmethod(stock_fixture.NativeFulfillmentStockSyncTests.counts)
    apply = stock_fixture.NativeFulfillmentStockSyncTests.apply

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="size-cap-manager",
            email="size-cap@example.test", password="test")
        self.large = VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="L",
            is_enabled=True, stock=4)
        self.buyer_serial = 0

    def synced_offer(self):
        request = self.case(purchase=True)
        preview = self.preview(request)
        pool = preview["pools"][0]
        result = self.apply(request, preview, counts={pool["pool_id"]: {"total": 5,
            "size_counts": {str(self.rule.pk): 1}}})
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["proposal_id"], result)
        return IgCheckoutProposal.objects.get(pk=result["proposal_id"])

    def source_bound_quote(self, *, size="M", quantity=1, reserve=True, expected_reasons=None):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_checkout import checkout_authority_control
        self.buyer_serial += 1
        client = IgClient.objects.create(igsid=f"size-cap-source-buyer-{self.buyer_serial}", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        original = self.client_row
        self.client_row = client
        try:
            self.canonical(f"добавьте {self.product.title}, количество {quantity}, размер {size}, цвет black, крій classic")
            self.canonical("Хочу купити")
            client.refresh_from_db()
            cart = capture_current_selection_lines(client.pk, now=self.now)
            control, reasons = checkout_authority_control(client, {}, source_cart_capture=cart)
            if expected_reasons is not None:
                self.assertEqual(reasons, expected_reasons)
                self.assertFalse(control)
                self.assertFalse(IgCheckoutProposal.objects.filter(client=client).exists())
                return {"client_id": client.pk, "reasons": reasons, "cart": cart}
            self.assertFalse(reasons, reasons)
            with transaction.atomic():
                proposal = create_or_update_proposal(client=client, pay_type="online_full",
                    item_specs=control["items"], source_cart_binding=control["source_cart_binding"],
                    source_cart_capture=control["source_cart_capture"], checkout_owner_scope=control["checkout_owner_scope"])
                if reserve:
                    reserve_proposal_inventory(proposal, require_policy=True)
            return proposal
        finally:
            self.client_row = original

    def legacy_offer(self, *, size="M", quantity=1, fit="classic"):
        self.buyer_serial += 1
        client = IgClient.objects.create(igsid=f"size-cap-legacy-buyer-{self.buyer_serial}")
        with override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0):
            return create_or_update_proposal(client=client, pay_type="online_full", item_specs=[{
                "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": quantity,
                "size": size, "fit_option_code": fit, "option_values": {"fit": fit}}])

    def checked(self, items, **kwargs):
        from productcolors.models import ProductColorVariant
        with transaction.atomic():
            ProductColorVariant.objects.select_for_update().get(pk=self.variant.pk)
            return check_catalog_size_capacity(self.variant.pk, items, self.now, **kwargs)

    def test_actual_sync_one_m_of_shared_five_cannot_reserve_a_second_m(self):
        first = self.synced_offer()
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 5)
        with self.assertRaises(InventoryReservationError) as denial:
            self.source_bound_quote()
        self.assertEqual(denial.exception.reason, "insufficient_catalog_size_stock")
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)
        self.assertEqual(first.inventory_reservations.filter(state=Reservation.State.ACTIVE).get().quantity, 1)
        other = self.source_bound_quote(size="L", quantity=4)
        self.assertEqual(other.inventory_reservations.get(state=Reservation.State.ACTIVE).quantity, 4)
        self.assertEqual(sum(Reservation.objects.filter(state=Reservation.State.ACTIVE)
            .values_list("quantity", flat=True)), 5)

    def test_actual_catalog_payment_consumes_exact_cap_and_preserves_other_size(self):
        first = self.synced_offer()
        commit_proposal_inventory(first, paid_at=self.now)
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.large.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock, self.large.stock), (4, 0, 4))
        self.assertEqual(first.inventory_reservations.get().state, Reservation.State.FULFILLED)
        self.source_bound_quote(expected_reasons=("checkout_cart_readiness_incomplete",))
        large = self.source_bound_quote(size="L", quantity=4)
        self.assertEqual(large.inventory_reservations.get().quantity, 4)

    def test_payment_rollback_restores_exact_cap_variant_and_reservation(self):
        first = self.synced_offer()
        with self.assertRaisesMessage(RuntimeError, "rollback paid capacity"):
            with transaction.atomic():
                commit_proposal_inventory(first, paid_at=self.now)
                self.rule.refresh_from_db()
                self.variant.refresh_from_db()
                self.assertEqual((self.variant.stock, self.rule.stock), (4, 0))
                raise RuntimeError("rollback paid capacity")
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (5, 1))
        self.assertEqual(first.inventory_reservations.get().state, Reservation.State.ACTIVE)

    def test_same_numeric_cap_aggregates_multiple_requested_rows(self):
        first = self.synced_offer()
        item = first.items.get()
        with self.assertRaises(SizeCapacityError) as denial:
            self.checked([item, item], exclude_proposal_id=first.pk)
        self.assertEqual(denial.exception.reason, "insufficient_catalog_size_stock")
        self.assertEqual(self.checked([item], exclude_proposal_id=first.pk), {self.rule.pk: 1})

    def test_expired_paid_commitment_remains_size_protected(self):
        first = self.synced_offer()
        hold = first.inventory_reservations.get()
        Reservation.objects.filter(pk=hold.pk).update(state=Reservation.State.PAID_COMMITTED,
            expires_at=self.now - timedelta(days=10), paid_committed_at=self.now - timedelta(days=11))
        with self.assertRaises(SizeCapacityError) as denial:
            self.checked(list(first.items.all()))
        self.assertEqual(denial.exception.reason, "insufficient_catalog_size_stock")

    def test_other_size_protection_does_not_charge_requested_numeric_cap(self):
        first = self.synced_offer()
        large = self.source_bound_quote(size="L", quantity=1)
        Reservation.objects.filter(proposal=large).update(state=Reservation.State.PAID_COMMITTED,
            expires_at=self.now - timedelta(days=3))
        self.assertEqual(self.checked(list(first.items.all()), exclude_proposal_id=first.pk), {self.rule.pk: 1})

    def test_legacy_empty_line_ids_requires_actual_primary_item_exact_quantity(self):
        first = self.synced_offer()
        hold = first.inventory_reservations.get()
        Reservation.objects.filter(pk=hold.pk).update(line_ids=[])
        with self.assertRaises(SizeCapacityError) as denial:
            self.checked(list(first.items.all()))
        self.assertEqual(denial.exception.reason, "insufficient_catalog_size_stock")
        Reservation.objects.filter(pk=hold.pk).update(quantity=2)
        with self.assertRaises(SizeCapacityError) as denial:
            self.checked(list(first.items.all()))
        self.assertEqual(denial.exception.reason, "catalog_size_reservation_scope_unverified")

    def test_grouped_reservation_primary_item_does_not_replace_missing_line(self):
        first = self.synced_offer()
        hold = first.inventory_reservations.get()
        Reservation.objects.filter(pk=hold.pk).update(line_ids=[hold.item_id, hold.item_id + 99999], quantity=2)
        with self.assertRaises(SizeCapacityError) as denial:
            self.checked(list(first.items.all()))
        self.assertEqual(denial.exception.reason, "catalog_size_item_busy")

    def test_null_cap_is_untracked_for_ordinary_legacy_purchase(self):
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.stock = None
        self.rule.save(update_fields=["stock"])
        offer = self.legacy_offer(quantity=2)
        self.assertEqual(self.checked(list(offer.items.all()), exclude_proposal_id=offer.pk), {})
        self.assertEqual(offer.inventory_reservations.get().quantity, 2)

    def test_specific_fit_cap_does_not_share_general_rule_capacity(self):
        first = self.synced_offer()
        general = VariantSizeRule.objects.create(variant=self.variant, fit_code="", size="M", stock=4, is_enabled=True)
        from storefront.models import ProductFitOption
        ProductFitOption.objects.create(product=self.product, code="oversize", label="Oversize", is_active=True)
        other = self.legacy_offer(fit="oversize", quantity=2)
        self.assertEqual(self.checked(list(other.items.all()), exclude_proposal_id=other.pk), {general.pk: 2})
        self.assertEqual(self.checked(list(first.items.all()), exclude_proposal_id=first.pk), {self.rule.pk: 1})

    @skipUnless(connection.vendor == "mysql", "Two current-read allocation connections require native MariaDB")
    def test_payment_foreign_item_contention_rolls_back_then_consumes_exactly_once(self):
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 3
        self.rule.save(update_fields=["stock"])
        own = self.source_bound_quote()
        foreign = self.source_bound_quote()
        own_hold = own.inventory_reservations.get(state=Reservation.State.ACTIVE)
        foreign_hold = foreign.inventory_reservations.get(state=Reservation.State.ACTIVE)
        foreign_item_id = foreign.items.get().pk
        locked, release = Event(), Event()
        def editor():
            from management.models import IgCheckoutProposalItem
            close_old_connections()
            try:
                with transaction.atomic():
                    IgCheckoutProposalItem.objects.select_for_update().get(pk=foreign_item_id)
                    locked.set()
                    if not release.wait(15):
                        raise RuntimeError("Payment contention item lock was not released")
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(editor)
            try:
                self.assertTrue(locked.wait(10))
                started = time.monotonic()
                with self.assertRaises(OperationalError) as denial:
                    commit_proposal_inventory(own, paid_at=self.now)
                self.assertEqual(denial.exception.args[0], 1205)
                self.assertLess(time.monotonic() - started, 5)
                own_hold.refresh_from_db()
                foreign_hold.refresh_from_db()
                self.rule.refresh_from_db()
                self.variant.refresh_from_db()
                self.assertEqual((self.variant.stock, self.rule.stock), (5, 3))
                self.assertEqual(own_hold.state, Reservation.State.ACTIVE)
                self.assertEqual(foreign_hold.state, Reservation.State.ACTIVE)
                self.assertFalse(Reservation.objects.filter(state=Reservation.State.OVERBOOKED_REVIEW).exists())
            finally:
                release.set()
                future.result(timeout=20)
        self.assertEqual(commit_proposal_inventory(own, paid_at=self.now), 1)
        own_hold.refresh_from_db()
        foreign_hold.refresh_from_db()
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (4, 2))
        self.assertEqual(own_hold.state, Reservation.State.FULFILLED)
        self.assertEqual(foreign_hold.state, Reservation.State.ACTIVE)
        self.assertEqual(commit_proposal_inventory(own, paid_at=self.now), 0)
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (4, 2))

    @skipUnless(connection.vendor == "mysql", "Two current-read allocation connections require native MariaDB")
    def test_foreign_locked_reservation_denies_promptly_and_retry_keeps_all_counts(self):
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 3
        self.rule.save(update_fields=["stock"])
        foreign = self.legacy_offer()
        reservation_id = foreign.inventory_reservations.get().pk
        candidate = self.source_bound_quote(reserve=False)
        self.assertFalse(candidate.inventory_reservations.exists())
        locked, release = Event(), Event()
        def editor():
            close_old_connections()
            try:
                with transaction.atomic():
                    Reservation.objects.select_for_update().get(pk=reservation_id)
                    locked.set()
                    if not release.wait(15):
                        raise RuntimeError("Reservation lock test was not released")
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(editor)
            try:
                self.assertTrue(locked.wait(10))
                started = time.monotonic()
                with self.assertRaises(SizeCapacityError) as denial:
                    self.checked(list(foreign.items.all()))
                self.assertEqual(denial.exception.reason, "catalog_size_reservation_busy")
                with self.assertRaises(InventoryReservationError) as denial:
                    reserve_proposal_inventory(candidate, require_policy=True)
                self.assertEqual(denial.exception.reason, "inventory_allocation_busy")
                self.assertLess(time.monotonic() - started, 5)
                self.assertFalse(candidate.inventory_reservations.exists())
            finally:
                release.set()
                future.result(timeout=20)
        reserve_proposal_inventory(candidate, require_policy=True)
        self.assertEqual(candidate.inventory_reservations.get().quantity, 1)
        self.assertEqual(sum(Reservation.objects.filter(state=Reservation.State.ACTIVE)
            .values_list("quantity", flat=True)), 2)

    @skipUnless(connection.vendor == "mysql", "Two current-read allocation connections require native MariaDB")
    def test_foreign_editor_item_lock_temporarily_denies_without_parent_lock_cycle(self):
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 3
        self.rule.save(update_fields=["stock"])
        foreign = self.legacy_offer()
        foreign_item_id = foreign.items.get().pk
        candidate = self.source_bound_quote(reserve=False)
        self.assertFalse(candidate.inventory_reservations.exists())
        locked, release = Event(), Event()
        def editor():
            from management.models import IgCheckoutProposalItem
            close_old_connections()
            try:
                with transaction.atomic():
                    IgCheckoutProposalItem.objects.select_for_update().get(pk=foreign_item_id)
                    locked.set()
                    if not release.wait(15):
                        raise RuntimeError("Foreign item lock test was not released")
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(editor)
            try:
                self.assertTrue(locked.wait(10))
                started = time.monotonic()
                with self.assertRaises(InventoryReservationError) as denial:
                    reserve_proposal_inventory(candidate, require_policy=True)
                self.assertIn(denial.exception.reason, {"inventory_allocation_busy", "catalog_size_item_busy"})
                self.assertLess(time.monotonic() - started, 5)
                self.assertFalse(candidate.inventory_reservations.exists())
                self.rule.refresh_from_db()
                self.variant.refresh_from_db()
                self.assertEqual((self.variant.stock, self.rule.stock), (5, 3))
            finally:
                release.set()
                future.result(timeout=20)
        reserve_proposal_inventory(candidate, require_policy=True)
        self.assertEqual(candidate.inventory_reservations.get().quantity, 1)

    @skipUnless(connection.vendor == "mysql", "Two current-read allocation connections require native MariaDB")
    def test_two_ordinary_reserve_connections_have_one_winner_for_numeric_cap_one(self):
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 1
        self.rule.save(update_fields=["stock"])
        # V2 proposal creation is source-bound but does not reserve until the
        # ordinary inventory owner admits the invoice or initial offer.
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_checkout import checkout_authority_control
        proposals = []
        original = self.client_row
        try:
            for index in range(2):
                self.client_row = IgClient.objects.create(igsid=f"concurrent-size-cap-{index}", language="uk",
                    reply_permission_epoch=3, last_user_message_at=self.now)
                self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
                self.canonical("Хочу купити")
                cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
                control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=cart)
                self.assertFalse(reasons, reasons)
                proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full",
                    item_specs=control["items"], source_cart_binding=control["source_cart_binding"],
                    source_cart_capture=control["source_cart_capture"], checkout_owner_scope=control["checkout_owner_scope"])
                self.assertFalse(proposal.inventory_reservations.exists())
                proposals.append(proposal.pk)
        finally:
            self.client_row = original
        barrier = Barrier(2, timeout=20)
        def reserve(identity):
            close_old_connections()
            try:
                proposal = IgCheckoutProposal.objects.get(pk=identity)
                barrier.wait()
                reserve_proposal_inventory(proposal, require_policy=True)
                return "reserved"
            except InventoryReservationError as exc:
                return exc.reason
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, identity) for identity in proposals]
            outcomes = [future.result(timeout=40) for future in futures]
        self.assertEqual(outcomes.count("reserved"), 1, outcomes)
        losing_index = next(index for index, outcome in enumerate(outcomes) if outcome != "reserved")
        self.assertIn(outcomes[losing_index], {"insufficient_catalog_size_stock", "inventory_allocation_busy"})
        # A native plan may visit the other editor's item before its WHERE
        # predicate, producing the temporary NOWAIT denial first. Once that
        # transaction ends, the same losing offer must see the occupied cap.
        losing = IgCheckoutProposal.objects.get(pk=proposals[losing_index])
        with self.assertRaises(InventoryReservationError) as denial:
            reserve_proposal_inventory(losing, require_policy=True)
        self.assertEqual(denial.exception.reason, "insufficient_catalog_size_stock")
        self.assertFalse(losing.inventory_reservations.exists())
        self.assertEqual(sum(Reservation.objects.filter(state=Reservation.State.ACTIVE)
            .values_list("quantity", flat=True)), 1)
