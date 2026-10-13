"""Actual source-bound manager recounts use ordinary finite inventory owners."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import connection, close_old_connections, connections
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.models import IgClient, IgCheckoutProposal, IgCheckoutInventoryReservation as Reservation, AdminAuditLog
from management.ig_fulfillment_stock_sync_models import IgFulfillmentStockSync as Sync
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_fulfillment_stock_sync as sync
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_inventory import commit_proposal_inventory, release_proposal_inventory
from management.tests_ig_restock_subscriptions import _RestockFixture
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule, VariantBlankLink
from warehouse.models import StorageCategory, StorageSubcategory, StockItem, StockMovement


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeFulfillmentStockSyncTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="stock-sync-manager",
            email="stock-sync@example.test", password="test")

    def case(self, *, availability="available", basis="accessible_unrecorded", purchase=False,
            text="Ця позиція є в наявності? Встигнете відправити до 20.10.2026?"):
        if purchase:
            self.canonical("Хочу купити")
        source = self.source(text)
        revision, _ = self.revision(source)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(result["ready"], result)
        request = reviews.Request.objects.get(pk=result["request_id"])
        result = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
            decisions_by_line=[{"line_id": row["line_id"], "availability": availability,
                "checked_quantity": row["target"].get("quantity", 1),
                "supply_basis": basis.get(row["target"]["product_id"], "accessible_unrecorded") if isinstance(basis, dict) else basis}
                for row in request.envelope["lines"]], now=self.now)
        self.assertTrue(result["ready"], result)
        return request

    def preview(self, request):
        result = sync.projection(request.client_id, request.pk, now=self.now)
        self.assertTrue(result["eligible"], result)
        return result

    @staticmethod
    def counts(preview, *, extra=0):
        result = {}
        for pool in preview["pools"]:
            row = {"total": pool["minimum_total"] + extra}
            if pool["source"] == "catalog_variant":
                row["size_counts"] = {key: value["minimum_total"] + extra for key, value in pool["size_counts"].items()}
            result[pool["pool_id"]] = row
        return result

    def apply(self, request, preview=None, *, counts=None, operation=None, on_synced=None):
        preview = preview or self.preview(request)
        def actual():
            return sync.apply_stock_sync(request.pk, actor=self.manager,
                expected_decision_digest=preview["decision_digest"], expected_preview_digest=preview["preview_digest"],
                operation_id=operation or uuid.uuid4(), counts_by_pool=counts if counts is not None else self.counts(preview),
                now=self.now)
        if on_synced is not None:
            with patch("management.services.ig_fulfillment_checkout.prepare_fulfillment_checkout", side_effect=on_synced):
                return actual()
        return actual()

    def warehouse(self):
        ProductInventoryPolicy.objects.filter(product=self.product).update(source="warehouse")
        self.rule.stock = None
        self.rule.save(update_fields=["stock"])
        category = StorageCategory.objects.create(name="Actual counted clothing", slug="stock-sync-clothing")
        storage = StorageSubcategory.objects.create(category=category, name="Classic", slug="classic")
        VariantBlankLink.objects.create(variant=self.variant, option_key="fit=classic", storage_subcategory=storage)
        return StockItem.objects.create(subcategory=storage, color_id=self.variant.color_id, size="M", quantity=0)

    def foreign_hold(self, *, warehouse_stock=None, paid=False, size="M"):
        self.variant.stock = 3
        self.variant.save(update_fields=["stock"])
        self.rule.stock = None if warehouse_stock else 3
        self.rule.save(update_fields=["stock"])
        if warehouse_stock:
            warehouse_stock.quantity = 3
            warehouse_stock.save(update_fields=["quantity"])
        other = IgClient.objects.create(igsid="stock-sync-protected-other")
        with override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0):
            proposal = create_or_update_proposal(client=other, pay_type="online_full", item_specs=[{
                "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1,
                "size": size, "fit_option_code": "classic", "option_values": {"fit": "classic"}}])
        hold = proposal.inventory_reservations.get(state=Reservation.State.ACTIVE)
        if paid:
            self.assertIsNotNone(warehouse_stock)
            commit_proposal_inventory(proposal, paid_at=self.now)
            hold.refresh_from_db()
            self.assertEqual(hold.state, Reservation.State.PAID_COMMITTED)
        return proposal, hold

    def test_real_preview_is_readonly_and_no_dialogue_decision_changes_stock(self):
        request = self.case()
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            preview = self.preview(request)
        provider.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in queries))
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertEqual(preview["pools"][0]["minimum_total"], 1)
        self.assertEqual(preview["pools"][0]["before_total"], 0)
        self.assertFalse(Sync.objects.exists())

    def test_absolute_recount_seals_actual_stock_and_ordinary_checkout_without_provider(self):
        request = self.case()
        preview = self.preview(request)
        operation = uuid.uuid4()
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = self.apply(request, preview, operation=operation)
        provider.assert_not_called()
        self.assertTrue(result["ready"], result)
        receipt = Sync.objects.get(operation_id=operation)
        self.assertTrue(reviews._verified(receipt, "stock_sync"))
        self.assertEqual(receipt.envelope["decision_digest"], preview["decision_digest"])
        self.assertNotIn("url", receipt.envelope["checkout"])
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (1, 1))
        request.task.refresh_from_db()
        self.assertNotIn(sync.DEBT, request.task.manager_context["required_decisions"])
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_fulfillment_stock_sync").count(), 1)
        if result.get("proposal_id"):
            proposal = IgCheckoutProposal.objects.get(pk=result["proposal_id"])
            self.assertEqual(proposal.client_id, request.client_id)
            self.assertEqual(proposal.inventory_reservations.filter(state=Reservation.State.ACTIVE).count(), 1)
            self.assertIsNone(proposal.payment_attempt_id)
            self.assertIsNone(proposal.winner_invoice_generation_id)

    def test_real_purchase_recount_and_full_priced_proposal_reserve_in_one_action(self):
        request = self.case(purchase=True)
        result = self.apply(request)
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["proposal_id"])
        proposal = IgCheckoutProposal.objects.get(pk=result["proposal_id"])
        self.assertEqual(proposal.catalog_total, self.product.price)
        self.assertEqual(list(proposal.inventory_reservations.filter(state=Reservation.State.ACTIVE)
            .values_list("quantity", flat=True)), [1])
        self.assertIsNone(proposal.payment_attempt_id)

    def _assert_zero_accounting_recount_then_finite_purchase(self, *, basis, warehouse=False):
        stock = self.warehouse() if warehouse else None
        request = self.case(purchase=True, basis=basis)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        if stock:
            stock.refresh_from_db()
            self.assertEqual(stock.quantity, 0)
        else:
            self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertFalse(Reservation.objects.filter(proposal__client=request.client).exists())
        request.task.refresh_from_db()
        self.assertIn(sync.DEBT, request.task.manager_context["required_decisions"])
        preview = self.preview(request)
        self.assertEqual(preview["pools"][0]["before_total"], 0)
        self.assertFalse(self.apply(request, preview, counts={})["ready"])
        self.assertFalse(Sync.objects.exists())
        result = self.apply(request, preview)
        self.assertTrue(result["ready"], result)
        proposal = IgCheckoutProposal.objects.get(pk=result["proposal_id"])
        self.assertEqual(proposal.catalog_total, self.product.price)
        hold = proposal.inventory_reservations.get(state=Reservation.State.ACTIVE)
        self.assertEqual((hold.quantity, hold.allocation_source),
            (1, "warehouse" if warehouse else "catalog_variant"))
        self.assertIsNone(proposal.payment_attempt_id)
        self.assertIsNone(proposal.winner_invoice_generation_id)
        self.assertTrue(reviews._verified(Sync.objects.get(pk=result["sync_id"]), "stock_sync"))
        request.task.refresh_from_db()
        self.assertNotIn(sync.DEBT, request.task.manager_context["required_decisions"])
        from management.services.ig_checkout import CheckoutConfigurationError
        other = IgClient.objects.create(igsid="confirmed-recount-second-buyer")
        with override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0), self.assertRaises(CheckoutConfigurationError):
            create_or_update_proposal(client=other, pay_type="online_full", item_specs=[{
                "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1,
                "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}}])

    def test_checked_catalog_zero_requires_absolute_recount_before_finite_purchase(self):
        self._assert_zero_accounting_recount_then_finite_purchase(basis="warehouse_checked")

    def test_checked_warehouse_zero_requires_absolute_recount_before_finite_purchase(self):
        self._assert_zero_accounting_recount_then_finite_purchase(basis="warehouse_checked", warehouse=True)

    def test_unrecorded_catalog_zero_requires_absolute_recount_before_finite_purchase(self):
        self._assert_zero_accounting_recount_then_finite_purchase(basis="accessible_unrecorded")

    def test_unrecorded_warehouse_zero_requires_absolute_recount_before_finite_purchase(self):
        self._assert_zero_accounting_recount_then_finite_purchase(basis="accessible_unrecorded", warehouse=True)

    def _decision_from_second_capable_actor(self):
        from django.contrib.auth.models import Permission
        decision_actor = get_user_model().objects.create_user(username="original-stock-decision-actor",
            is_staff=True, password="test")
        permissions = list(Permission.objects.filter(content_type__app_label="management",
            codename__in=["operate_ig_bot", "view_ig_conversation_pii"]).order_by("pk"))
        self.assertEqual(len(permissions), 2)
        decision_actor.user_permissions.add(*permissions)
        executing_actor = self.manager
        self.manager = decision_actor
        try:
            request = self.case(purchase=True, basis="warehouse_checked")
        finally:
            self.manager = executing_actor
        decision = reviews.Decision.objects.get(request=request)
        self.assertEqual(decision.actor_id, decision_actor.pk)
        self.assertNotEqual(decision.actor_id, executing_actor.pk)
        return request, decision, decision_actor, permissions

    def _assert_original_actor_denied_with_no_inventory_effect(self, *, reviewer=False, revoke_codename="operate_ig_bot"):
        request, decision, actor, permissions = self._decision_from_second_capable_actor()
        preview = self.preview(request)
        before = deepcopy(decision.envelope)
        before_hmac = decision.envelope_hmac
        if reviewer:
            from django.contrib.auth.models import Group
            from management.bot_access import META_REVIEWER_GROUP_NAME
            actor.groups.add(Group.objects.get_or_create(name=META_REVIEWER_GROUP_NAME)[0])
        else:
            actor.user_permissions.remove(next(row for row in permissions if row.codename == revoke_codename))
        denied = sync.projection(request.client_id, request.pk, now=self.now)
        self.assertFalse(denied["eligible"])
        self.assertEqual(denied["reason"], "decision_actor_forbidden")
        result = self.apply(request, preview)
        self.assertEqual(result, {"ready": False, "reason": "decision_actor_forbidden"})
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertFalse(Sync.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.filter(client=request.client).exists())
        self.assertFalse(Reservation.objects.filter(proposal__client=request.client).exists())
        self.assertFalse(AdminAuditLog.objects.filter(action="ig_fulfillment_stock_sync").exists())
        decision.refresh_from_db()
        self.assertEqual(decision.envelope, before)
        self.assertEqual(decision.envelope_hmac, before_hmac)
        self.assertTrue(reviews._verified(decision, "decision"))

    def test_authorized_recounter_cannot_use_original_actor_with_revoked_capability(self):
        self._assert_original_actor_denied_with_no_inventory_effect()

    def test_authorized_recounter_cannot_use_original_actor_with_revoked_pii_capability(self):
        self._assert_original_actor_denied_with_no_inventory_effect(revoke_codename="view_ig_conversation_pii")

    def test_authorized_recounter_cannot_use_original_actor_now_meta_reviewer(self):
        self._assert_original_actor_denied_with_no_inventory_effect(reviewer=True)

    def test_second_authorized_actor_recounts_when_original_actor_remains_capable(self):
        request, decision, _actor, _permissions = self._decision_from_second_capable_actor()
        preview = self.preview(request)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        result = self.apply(request, preview)
        self.assertTrue(result["ready"], result)
        receipt = Sync.objects.get(pk=result["sync_id"])
        self.assertEqual(receipt.actor_id, self.manager.pk)
        self.assertEqual(receipt.decision_id, decision.pk)
        proposal = IgCheckoutProposal.objects.get(pk=result["proposal_id"])
        self.assertEqual(proposal.catalog_total, self.product.price)
        self.assertEqual(proposal.inventory_reservations.get(state=Reservation.State.ACTIVE).quantity, 1)

    def test_same_operation_replays_receipt_without_adding_capacity_or_another_checkout(self):
        request, operation = self.case(), uuid.uuid4()
        preview = self.preview(request)
        first = self.apply(request, preview, operation=operation)
        second = self.apply(request, preview, operation=operation)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["sync_id"], second["sync_id"])
        self.assertEqual(Sync.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
        changed = self.counts(preview, extra=1)
        self.assertEqual(self.apply(request, preview, operation=operation, counts=changed)["reason"], "operation_conflict")

    def test_completed_readback_has_real_receipt_and_never_returns_public_bearer_url(self):
        request = self.case()
        result = self.apply(request)
        readback = sync.projection(request.client_id, request.pk, now=self.now)
        self.assertFalse(readback["eligible"])
        self.assertEqual(readback["reason"], "already_synced")
        self.assertEqual(readback["sync_id"], result["sync_id"])
        self.assertEqual(readback["checkout"]["url"], "")

    def test_changed_shared_total_is_stale_cas_not_recount_authority(self):
        request = self.case()
        preview = self.preview(request)
        self.variant.stock = 2
        self.variant.save(update_fields=["stock"])
        self.assertEqual(self.apply(request, preview), {"ready": False, "reason": "preview_changed"})
        self.assertFalse(Sync.objects.exists())
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 0)

    def test_changed_exact_size_count_is_stale_even_if_shared_total_unchanged(self):
        request = self.case()
        preview = self.preview(request)
        VariantSizeRule.objects.filter(pk=self.rule.pk).update(stock=2)
        self.assertEqual(self.apply(request, preview), {"ready": False, "reason": "preview_changed"})
        self.assertFalse(Sync.objects.exists())

    def test_released_reservation_remains_in_allstate_preview_fence(self):
        proposal, _hold = self.foreign_hold()
        request = self.case()
        preview = self.preview(request)
        self.assertEqual(preview["pools"][0]["protected"], 1)
        release_proposal_inventory(proposal)
        self.assertEqual(self.apply(request, preview), {"ready": False, "reason": "preview_changed"})
        fresh = self.preview(request)
        self.assertEqual(fresh["pools"][0]["protected"], 0)
        self.assertEqual(fresh["pools"][0]["reservations"][0]["state"], "released")

    def test_count_cannot_steal_another_clients_live_shared_pool_reservation(self):
        _proposal, hold = self.foreign_hold()
        request = self.case()
        preview = self.preview(request)
        self.assertEqual(preview["pools"][0]["minimum_total"], 2)
        counts = self.counts(preview)
        counts[preview["pools"][0]["pool_id"]]["total"] = 1
        self.assertEqual(self.apply(request, preview, counts=counts),
            {"ready": False, "reason": "physical_total_below_protected_and_requested"})
        hold.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.ACTIVE)
        self.assertFalse(Sync.objects.exists())

    def test_expired_paid_warehouse_commitment_is_still_protected(self):
        stock = self.warehouse()
        _proposal, hold = self.foreign_hold(warehouse_stock=stock, paid=True)
        Reservation.objects.filter(pk=hold.pk).update(expires_at=self.now - timedelta(days=10))
        request = self.case()
        preview = self.preview(request)
        pool = preview["pools"][0]
        self.assertEqual((pool["protected"], pool["minimum_total"]), (1, 2))
        self.assertFalse(self.apply(request, preview, counts={pool["pool_id"]: {"total": 1}})["ready"])
        hold.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.PAID_COMMITTED)

    def test_real_warehouse_recount_records_movement_and_exact_allocation(self):
        stock = self.warehouse()
        request = self.case()
        result = self.apply(request)
        self.assertTrue(result["ready"], result)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 1)
        movement = StockMovement.objects.get(object_id=stock.pk)
        self.assertEqual((movement.delta, movement.quantity_after), (1, 1))
        receipt = Sync.objects.get(pk=result["sync_id"])
        self.assertEqual(receipt.envelope["movement_ids"], [movement.pk])

    def test_checkout_failure_rolls_back_catalog_totals_audit_and_manager_debt(self):
        request = self.case()
        def denied(*args, **kwargs):
            self.variant.refresh_from_db()
            self.assertEqual(self.variant.stock, 1)
            raise sync.StockSyncError("actual_checkout_failed")
        self.assertEqual(self.apply(request, on_synced=denied), {"ready": False, "reason": "actual_checkout_failed"})
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertFalse(Sync.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.filter(client=request.client).exists())
        request.task.refresh_from_db()
        self.assertIn(sync.DEBT, request.task.manager_context["required_decisions"])

    def test_checkout_failure_rolls_back_warehouse_movement(self):
        stock = self.warehouse()
        request = self.case()
        def denied(*args, **kwargs):
            self.assertTrue(StockMovement.objects.filter(object_id=stock.pk).exists())
            raise sync.StockSyncError("actual_checkout_failed")
        self.assertEqual(self.apply(request, on_synced=denied), {"ready": False, "reason": "actual_checkout_failed"})
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 0)
        self.assertFalse(StockMovement.objects.filter(object_id=stock.pk).exists())
        self.assertFalse(Sync.objects.exists())

    def test_failure_after_actual_checkout_rolls_back_proposal_and_finite_hold_together(self):
        from management.services.ig_fulfillment_checkout import prepare_fulfillment_checkout
        request = self.case(purchase=True)
        def denied_after_checkout(request, decision, *, actor, now):
            result = prepare_fulfillment_checkout(request, decision, actor=actor, now=now)
            self.assertTrue(result["proposal_id"], result)
            self.assertTrue(Reservation.objects.filter(proposal_id=result["proposal_id"]).exists())
            raise sync.StockSyncError("post_checkout_failure")
        result = self.apply(request, on_synced=denied_after_checkout)
        self.assertEqual(result, {"ready": False, "reason": "post_checkout_failure"})
        self.assertFalse(IgCheckoutProposal.objects.filter(client_id=request.client_id).exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(Sync.objects.exists())
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        request.task.refresh_from_db()
        self.assertIn(sync.DEBT, request.task.manager_context["required_decisions"])

    def test_null_exact_size_cap_requires_explicit_count_distinct_from_shared_total(self):
        self.rule.stock = None
        self.rule.save(update_fields=["stock"])
        request = self.case()
        preview = self.preview(request)
        pool = preview["pools"][0]
        self.assertIsNone(pool["size_counts"][str(self.rule.pk)]["before_total"])
        result = self.apply(request, preview, counts={pool["pool_id"]: {"total": 5,
            "size_counts": {str(self.rule.pk): 1}}})
        self.assertTrue(result["ready"], result)
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (5, 1))
        other = IgClient.objects.create(igsid="null-cap-second-buyer")
        from management.services.ig_checkout import CheckoutConfigurationError
        with override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0), self.assertRaises(CheckoutConfigurationError):
            create_or_update_proposal(client=other, pay_type="online_full", item_specs=[{
                "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 2,
                "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}}])

    def test_missing_exact_size_rule_requires_editor_not_new_configuration(self):
        self.rule.delete()
        request = self.case()
        preview = sync.projection(request.client_id, request.pk, now=self.now)
        self.assertFalse(preview["eligible"])
        self.assertEqual(preview["reason"], "exact_size_count_requires_editor")
        self.assertFalse(Sync.objects.exists())
        self.assertFalse(VariantSizeRule.objects.filter(variant=self.variant, size="M").exists())

    def test_missing_count_bool_or_incomplete_sizecap_never_changes_stock(self):
        request = self.case()
        preview = self.preview(request)
        key = preview["pools"][0]["pool_id"]
        for counts in ({}, {key: {"total": True, "size_counts": {}}}, {key: {"total": 1, "size_counts": {}}}):
            with self.subTest(counts=counts):
                self.assertFalse(self.apply(request, preview, counts=counts)["ready"])
        self.assertFalse(Sync.objects.exists())
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)

    def test_wait_never_grants_recount(self):
        request = self.case(availability="wait", basis="incoming_confirmed")
        preview = sync.projection(request.client_id, request.pk, now=self.now)
        self.assertFalse(preview["eligible"])
        self.assertEqual(preview["reason"], "all_lines_require_available")

    def test_checked_available_accounting_with_no_shortfall_has_no_recount(self):
        self.variant.stock = self.rule.stock = 3
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        request = self.case(basis="warehouse_checked")
        preview = sync.projection(request.client_id, request.pk, now=self.now)
        self.assertFalse(preview["eligible"])
        self.assertEqual(preview["reason"], "accessible_stock_reconciliation_not_required")
        self.assertFalse(Sync.objects.exists())

    def test_full_mixed_available_cart_preserves_regular_and_unrecorded_items(self):
        other, variant = self.products[1], self.variants[1]
        variant.stock = 2
        variant.save(update_fields=["stock"])
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="M", stock=2, is_enabled=True)
        ProductInventoryPolicy.objects.create(product=other, source="catalog_variant")
        self.canonical("Хочу купити")
        self.canonical(f"добавьте {other.title}, количество 1, размер M, цвет pink, крій classic")
        request = self.case(purchase=True, basis={self.product.pk: "accessible_unrecorded", other.pk: "warehouse_checked"})
        self.assertEqual(len(request.envelope["lines"]), 2)
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_response_cart_plan import source_line_preferences
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        for line in cart["lines"]:
            preferences = source_line_preferences(cart, line)
            self.assertIs(preferences["values"].get("purchase_requested"), True, preferences)
            self.assertTrue(preferences["evidence"].get("purchase_requested"), preferences)
        preview = self.preview(request)
        self.assertEqual(len(preview["pools"]), 2)
        result = self.apply(request, preview)
        self.assertTrue(result["ready"], result)
        proposal = IgCheckoutProposal.objects.get(pk=result["proposal_id"])
        self.assertEqual(set(proposal.items.values_list("product_id", flat=True)), {self.product.pk, other.pk})
        self.assertEqual(sum(proposal.inventory_reservations.filter(state=Reservation.State.ACTIVE)
            .values_list("quantity", flat=True)), 2)

    def test_other_size_paid_ledger_commitment_protects_shared_total_not_requested_size_cap(self):
        VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="L", stock=3, is_enabled=True)
        _proposal, hold = self.foreign_hold(size="L")
        # Preserve an existing paid ledger obligation, including legacy catalog rows.
        # This does not assert new payment recognition or create an order.
        Reservation.objects.filter(pk=hold.pk).update(state=Reservation.State.PAID_COMMITTED,
            expires_at=self.now - timedelta(days=1), paid_committed_at=self.now - timedelta(days=2))
        request = self.case()
        preview = self.preview(request)
        pool = preview["pools"][0]
        self.assertEqual((pool["protected"], pool["minimum_total"]), (1, 2))
        self.assertEqual(pool["size_counts"][str(self.rule.pk)]["minimum_total"], 1)
        counts = {pool["pool_id"]: {"total": 2, "size_counts": {str(self.rule.pk): 1}}}
        result = self.apply(request, preview, counts=counts)
        self.assertTrue(result["ready"], result)
        hold.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.PAID_COMMITTED)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (2, 1))

    def test_disabled_size_is_manager_editor_work_not_silent_enable(self):
        request = self.case()
        self.rule.is_enabled = False
        self.rule.save(update_fields=["is_enabled"])
        preview = sync.projection(request.client_id, request.pk, now=self.now)
        self.assertFalse(preview["eligible"])
        self.assertFalse(Sync.objects.exists())
        self.rule.refresh_from_db()
        self.assertFalse(self.rule.is_enabled)

    def test_permission_epoch_change_and_other_client_request_cannot_reconcile(self):
        request = self.case()
        preview = self.preview(request)
        self.assertFalse(sync.projection(request.client_id + 999, request.pk, now=self.now)["eligible"])
        self.client_row.reply_permission_epoch += 1
        self.client_row.save(update_fields=["reply_permission_epoch"])
        self.assertFalse(self.apply(request, preview)["ready"])
        self.assertFalse(Sync.objects.exists())

    @skipUnless(connection.vendor == "mysql", "Native two-connection stock row contention requires MariaDB")
    def test_two_real_customer_recounts_of_one_unit_have_one_checkout_winner(self):
        first = self.case(purchase=True)
        first_preview = self.preview(first)
        original = self.client_row
        self.client_row = IgClient.objects.create(igsid="second-stock-sync-customer", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        try:
            self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
            second = self.case(purchase=True)
            second_preview = self.preview(second)
        finally:
            self.client_row = original
        barrier = Barrier(2, timeout=20)
        actor_id, now = self.manager.pk, self.now
        def contend(request_id, preview):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=actor_id)
                barrier.wait()
                return sync.apply_stock_sync(request_id, actor=actor,
                    expected_decision_digest=preview["decision_digest"], expected_preview_digest=preview["preview_digest"],
                    operation_id=uuid.uuid4(), counts_by_pool=self.counts(preview), now=now)
            except sync.StockSyncError as exc:
                return {"ready": False, "reason": exc.reason}
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contend, request.pk, preview) for request, preview in ((first, first_preview), (second, second_preview))]
            results = [future.result(timeout=40) for future in futures]
        self.assertEqual(sum(row.get("ready", False) for row in results), 1, results)
        self.assertTrue(all(row.get("ready") or row.get("reason") in {"preview_changed", "inventory_conflict"} for row in results), results)
        self.assertEqual(Sync.objects.count(), 1)
        self.assertEqual(IgCheckoutProposal.objects.filter(client_id__in=[first.client_id, second.client_id]).count(), 1)
        self.assertEqual(sum(Reservation.objects.filter(state=Reservation.State.ACTIVE,
            color_variant=self.variant).values_list("quantity", flat=True)), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
