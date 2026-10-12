"""Native V2 reservation transfer preserves every other physical obligation."""
from datetime import timedelta
import os
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from management.models import (
    IgCheckoutInventoryReservation, IgCheckoutInvoiceGeneration, IgClient,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_checkout_generation import _prepare_generation
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_inventory import (
    InventoryReservationError, _fingerprint, commit_proposal_inventory,
    reserve_proposal_inventory,
)
from management.services.ig_revision_checkout import checkout_authority_control
from management.services.ig_revision_commerce import reduce_inbound_commerce_source
from orders.models import PaymentAttempt
from product_catalog.models import ProductInventoryPolicy, VariantBlankLink, VariantSizeRule
from warehouse.models import StockItem, StorageCategory, StorageSubcategory
from warehouse.services.inventory import protected_stock_quantity


@skipUnless(connection.vendor == "mysql", "Requires native MariaDB transaction and stock locks")
@override_settings(
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    SITE_BASE_URL="https://twocomms.test",
)
class FulfillmentInventoryTransferTests(TestCase):
    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests

        SourceCartCatalogTests.setUp(self)
        self.product, self.variant = self.products[0], self.variants[0]
        self.now = timezone.now()
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        transport = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login",
            "IG_INSTAGRAM_BOT": "synthetic-inventory-transfer-test-token"})
        transport.start()
        self.addCleanup(transport.stop)
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True,
            ig_user_id="owner-1")
        provider = patch("storefront.views.monobank._monobank_api_request",
            side_effect=AssertionError("Reservation preparation must perform zero provider I/O"))
        self.provider = provider.start()
        self.addCleanup(provider.stop)
        self.serial = 0
        self.client_row = IgClient.get_or_create_for_sender("inventory-transfer-owner")
        self.policy = ProductInventoryPolicy.objects.create(product=self.product, source="warehouse")
        category = StorageCategory.objects.create(name="Transfer stock", slug="transfer-stock",
            linked_storefront_category=self.category)
        subcategory = StorageSubcategory.objects.create(category=category,
            name="Transfer blank", slug="transfer-blank")
        self.stock = StockItem.objects.create(subcategory=subcategory,
            color=self.variant.color, size="M", quantity=1)
        VariantBlankLink.objects.create(variant=self.variant, option_key="fit=classic",
            storage_subcategory=subcategory)

    def proposal(self, *, client=None, source_lines=None):
        client = client or self.client_row
        source_lines = source_lines or (
            f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic",
        )
        for text in source_lines:
            self.serial += 1
            source = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
                role="user", source="webhook", status="pending", text=text,
                provider_namespace="instagram_login:owner-1", mid=f"transfer-source-{self.serial}",
                provider_created_at=self.now)
            client.refresh_from_db()
            with transaction.atomic():
                admitted = reduce_inbound_commerce_source(client, source,
                    expected_provider_namespace=source.provider_namespace)
            self.assertTrue(admitted.ready, admitted.reason)
            source.status = "done"
            source.save(update_fields=["status"])
        client.refresh_from_db()
        captured = capture_current_selection_lines(client.pk)
        self.assertEqual(captured.get("status"), "captured", captured)
        self.assertTrue(captured.get("coverage_complete"), captured)
        control, reasons = checkout_authority_control(client, {}, source_cart_capture=captured)
        self.assertFalse(reasons, reasons)
        proposal = create_or_update_proposal(client=client, pay_type="online_full",
            item_specs=control["items"], source_cart_binding=control["source_cart_binding"],
            source_cart_capture=control["source_cart_capture"],
            checkout_owner_scope=control["checkout_owner_scope"])
        self.assertTrue(proposal.assisted_checkout_v2)
        self.assertFalse(proposal.inventory_reservations.exists())
        return proposal

    def hold(self, proposal):
        created = reserve_proposal_inventory(proposal, require_policy=True)
        self.assertEqual(len(created), 1)
        row = proposal.inventory_reservations.get()
        self.assertIsNone(row.invoice_generation_id)
        self.assertEqual(row.state, "active")
        self.assertEqual(row.line_ids, list(proposal.items.values_list("pk", flat=True)))
        self.assertEqual(row.quantity, 1)
        return row

    def prepare(self, proposal):
        from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests

        request = RequestFactory().post("/internal-transfer-prepare/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session.save()
        request.user = AnonymousUser()
        return _prepare_generation(proposal, request=request,
            payload=CheckoutGenerationRuntimeTests._delivery_payload(self))

    def assert_no_generation(self, proposal):
        self.assertFalse(IgCheckoutInvoiceGeneration.objects.filter(proposal=proposal).exists())
        proposal.refresh_from_db()
        self.assertIsNone(proposal.current_invoice_generation_id)
        self.assertIsNone(proposal.payment_attempt_id)
        self.provider.assert_not_called()

    def test_exact_source_bound_last_warehouse_unit_transfers_into_first_generation(self):
        proposal = self.proposal()
        hold = self.hold(proposal)
        _, generation, attempt, _, reused = self.prepare(proposal)
        self.assertFalse(reused)
        self.assertEqual(generation.generation, 1)
        self.assertEqual(generation.state, "provider_inflight")
        self.assertEqual(attempt.status, PaymentAttempt.Status.PROCESSING)
        self.assertFalse(attempt.monobank_invoice_id)
        hold.refresh_from_db()
        reservation = generation.inventory_reservations.get()
        self.assertEqual((hold.state, hold.release_reason),
            ("released", "transferred_to_invoice_generation"))
        self.assertEqual((reservation.state, reservation.quantity), ("active", 1))
        self.assertEqual(reservation.line_ids, hold.line_ids)
        self.assertEqual(reservation.stock_item_id, self.stock.pk)
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock.pk), 1)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 1)
        self.provider.assert_not_called()

    def test_last_catalog_variant_unit_uses_the_same_exact_transfer_rule(self):
        self.policy.source = "catalog_variant"
        self.policy.save(update_fields=["source"])
        self.variant.stock = 1
        self.variant.save(update_fields=["stock"])
        proposal = self.proposal()
        hold = self.hold(proposal)
        _, generation, _, _, _ = self.prepare(proposal)
        hold.refresh_from_db()
        reservation = generation.inventory_reservations.get()
        self.assertEqual(hold.state, "released")
        self.assertEqual(reservation.allocation_source, "catalog_variant")
        self.assertEqual(reservation.color_variant_id, self.variant.pk)
        self.assertEqual(reservation.quantity, 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
        with connection.cursor() as cursor:
            cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [self.variant._meta.db_table])
            self.assertEqual(cursor.fetchone(), ("InnoDB",))
        self.provider.assert_not_called()

    def test_another_proposals_hold_remains_protected_in_the_same_global_pool(self):
        self.stock.quantity = 2
        self.stock.save(update_fields=["quantity", "updated_at"])
        own = self.proposal()
        own_hold = self.hold(own)
        other = self.proposal(client=IgClient.get_or_create_for_sender("inventory-transfer-other"))
        other_hold = self.hold(other)
        _, generation, _, _, _ = self.prepare(own)
        own_hold.refresh_from_db()
        other_hold.refresh_from_db()
        self.assertEqual((own_hold.state, other_hold.state), ("released", "active"))
        self.assertIsNone(other_hold.invoice_generation_id)
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock.pk), 2)
        third = self.proposal(client=IgClient.get_or_create_for_sender("inventory-transfer-third"))
        with self.assertRaises(InventoryReservationError) as failure:
            self.prepare(third)
        self.assertEqual(failure.exception.reason, "insufficient_reserved_stock")
        self.assert_no_generation(third)
        self.assertEqual(generation.inventory_reservations.get().state, "active")
        other_hold.refresh_from_db()
        self.assertEqual(other_hold.state, "active")

    def test_two_distinct_catalog_variant_size_caps_transfer_only_their_own_hold(self):
        self.policy.source = "catalog_variant"
        self.policy.save(update_fields=["source"])
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        rules = []
        for variant, size in zip(self.variants, ("M", "L")):
            variant.stock = 1
            variant.save(update_fields=["stock"])
            rules.append(VariantSizeRule.objects.create(variant=variant, fit_code="classic",
                size=size, stock=1, is_enabled=True))
        proposal = self.proposal(source_lines=(
            f"добавьте {self.products[0].title}, количество 1, размер M, цвет black, крій classic",
            f"добавьте {self.products[1].title}, количество 1, размер L, цвет pink, крій classic",
        ))
        items = list(proposal.items.order_by("pk"))
        self.assertEqual(len(items), 2)
        expected_lines = {item.color_variant_id: [item.pk] for item in items}
        self.assertEqual(set(expected_lines), {variant.pk for variant in self.variants})
        holds = reserve_proposal_inventory(proposal, require_policy=True)
        self.assertEqual(len(holds), 2)
        for hold in holds:
            self.assertIsNone(hold.invoice_generation_id)
            self.assertEqual((hold.state, hold.quantity), ("active", 1))
            self.assertEqual(hold.line_ids, expected_lines[hold.color_variant_id])
        _, generation, _, _, reused = self.prepare(proposal)
        self.assertFalse(reused)
        rows = list(generation.inventory_reservations.order_by("color_variant_id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.color_variant_id for row in rows}, set(expected_lines))
        for row in rows:
            self.assertEqual((row.state, row.quantity), ("active", 1))
            self.assertEqual(row.line_ids, expected_lines[row.color_variant_id])
        for hold in holds:
            hold.refresh_from_db()
            self.assertEqual((hold.state, hold.release_reason),
                ("released", "transferred_to_invoice_generation"))
        self.assertEqual(reserve_proposal_inventory(proposal, generation=generation,
            require_policy=True), [])
        self.assertEqual(proposal.inventory_reservations.count(), 4)
        self.assertEqual(generation.inventory_reservations.count(), 2)
        for variant, rule in zip(self.variants, rules):
            variant.refresh_from_db()
            rule.refresh_from_db()
            self.assertEqual((variant.stock, rule.stock), (1, 1))
            self.assertEqual(proposal.inventory_reservations.filter(color_variant=variant,
                state="active").count(), 1)
        self.provider.assert_not_called()

    def test_transfer_cannot_hide_other_hold_after_physical_count_correction(self):
        self.stock.quantity = 2
        self.stock.save(update_fields=["quantity", "updated_at"])
        own = self.proposal()
        own_hold = self.hold(own)
        other = self.proposal(client=IgClient.get_or_create_for_sender("inventory-transfer-count-correction"))
        other_hold = self.hold(other)
        # Persist a corrected physical count while both historical holds
        # exist. No reservation/admission result is fabricated or patched.
        self.stock.quantity = 1
        self.stock.save(update_fields=["quantity", "updated_at"])
        with self.assertRaises(InventoryReservationError) as failure:
            self.prepare(own)
        self.assertEqual(failure.exception.reason, "insufficient_reserved_stock")
        self.assert_no_generation(own)
        own_hold.refresh_from_db()
        other_hold.refresh_from_db()
        self.assertEqual((own_hold.state, other_hold.state), ("active", "active"))

    def test_paid_warehouse_commitment_blocks_even_after_its_original_ttl(self):
        paid = self.proposal(client=IgClient.get_or_create_for_sender("inventory-transfer-paid"))
        paid_hold = self.hold(paid)
        self.assertEqual(commit_proposal_inventory(paid), 1)
        paid_hold.refresh_from_db()
        self.assertEqual(paid_hold.state, "paid_committed")
        IgCheckoutInventoryReservation.objects.filter(pk=paid_hold.pk).update(
            expires_at=self.now - timedelta(minutes=1))
        own = self.proposal()
        with self.assertRaises(InventoryReservationError) as failure:
            self.prepare(own)
        self.assertEqual(failure.exception.reason, "insufficient_reserved_stock")
        self.assert_no_generation(own)
        paid_hold.refresh_from_db()
        self.assertEqual(paid_hold.state, "paid_committed")
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock.pk), 1)

    def test_wrong_line_ids_quantity_or_allocation_cannot_authorize_transfer(self):
        proposal = self.proposal()
        hold = self.hold(proposal)
        original = {"line_ids": hold.line_ids, "quantity": hold.quantity,
            "allocation_key": hold.allocation_key}
        # This mutable reservation is deliberately corrupted, leaving all
        # immutable source, quote, and generation authority untouched.
        for wrong in ({"line_ids": []}, {"quantity": 2}, {"allocation_key": "warehouse:stock_item:foreign"}):
            with self.subTest(wrong=wrong):
                IgCheckoutInventoryReservation.objects.filter(pk=hold.pk).update(**{**original, **wrong})
                with self.assertRaises(InventoryReservationError) as failure:
                    self.prepare(proposal)
                self.assertEqual(failure.exception.reason, "inventory_proposal_hold_changed")
                self.assert_no_generation(proposal)
                hold.refresh_from_db()
                self.assertEqual(hold.state, "active")

    def test_prior_revision_fingerprint_is_not_an_excludable_current_hold(self):
        proposal = self.proposal()
        hold = self.hold(proposal)
        stale = _fingerprint(SimpleNamespace(pk=proposal.pk, revision=proposal.revision - 1), hold.allocation_key)
        self.assertNotEqual(stale, hold.reservation_fingerprint)
        IgCheckoutInventoryReservation.objects.filter(pk=hold.pk).update(reservation_fingerprint=stale)
        with self.assertRaises(InventoryReservationError) as failure:
            self.prepare(proposal)
        self.assertEqual(failure.exception.reason, "insufficient_reserved_stock")
        self.assert_no_generation(proposal)
        hold.refresh_from_db()
        self.assertEqual(hold.state, "active")

    def test_expired_proposal_hold_is_not_transferred_or_counted_twice(self):
        proposal = self.proposal()
        hold = self.hold(proposal)
        IgCheckoutInventoryReservation.objects.filter(pk=hold.pk).update(
            expires_at=self.now - timedelta(seconds=1))
        _, generation, _, _, _ = self.prepare(proposal)
        hold.refresh_from_db()
        self.assertEqual((hold.state, hold.release_reason), ("active", ""))
        self.assertLess(hold.expires_at, self.now)
        reservation = generation.inventory_reservations.get()
        self.assertEqual((reservation.state, reservation.quantity), ("active", 1))
        self.assertGreater(reservation.expires_at, self.now)
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock.pk), 1)
        self.provider.assert_not_called()

    def test_outer_transaction_failure_rolls_back_transfer_and_every_generation_pointer(self):
        proposal = self.proposal()
        hold = self.hold(proposal)
        attempts = PaymentAttempt.objects.count()
        class AbortAfterTransfer(Exception):
            pass
        with self.assertRaises(AbortAfterTransfer):
            with transaction.atomic():
                _, generation, _, _, _ = self.prepare(proposal)
                hold.refresh_from_db()
                self.assertEqual(hold.state, "released")
                self.assertEqual(generation.inventory_reservations.get().state, "active")
                raise AbortAfterTransfer()
        hold.refresh_from_db()
        self.assertEqual((hold.state, hold.release_reason), ("active", ""))
        self.assertEqual(PaymentAttempt.objects.count(), attempts)
        self.assert_no_generation(proposal)
        _, generation, _, _, _ = self.prepare(proposal)
        self.assertEqual(generation.inventory_reservations.count(), 1)

    def test_repeated_generation_reservation_does_not_double_hold_or_release_other_rows(self):
        proposal = self.proposal()
        hold = self.hold(proposal)
        _, generation, _, _, _ = self.prepare(proposal)
        reservation = generation.inventory_reservations.get()
        for _ in range(2):
            self.assertEqual(reserve_proposal_inventory(proposal, generation=generation,
                require_policy=True), [])
        self.assertEqual(generation.inventory_reservations.get().pk, reservation.pk)
        self.assertEqual(proposal.inventory_reservations.count(), 2)
        self.assertEqual(protected_stock_quantity(stock_item_id=self.stock.pk), 1)
        hold.refresh_from_db()
        self.assertEqual(hold.state, "released")
        self.provider.assert_not_called()
