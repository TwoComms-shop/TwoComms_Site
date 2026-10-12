"""Exact physical restock truth, source resolution and canonical writer hooks."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import timedelta
import json
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from management.services.ig_restock_availability import observe_target, observe_configuration, observe_configurations, resolve_source_interest, MAX_QUERIES
from product_catalog.models import GarmentFlow, GarmentFlowCategory, ProductInventoryPolicy, VariantBlankLink, VariantSizeRule
from productcolors.models import Color, ProductColorVariant
from warehouse.models import StockItem, StorageCategory, StorageSubcategory


class MalformedRestockTargetTests(SimpleTestCase):
    def test_missing_extra_bool_quantity_or_incomplete_options_perform_no_reads(self):
        raw = {"product_id": 1, "color_variant_id": 2, "size": "M", "fit_option_code": "classic",
            "option_values": {"fit": "classic"}, "quantity": 1}
        for changes in ({"quantity": True}, {"quantity": "1"}, {"quantity": 0}, {"quantity": 100},
                {"product_id": True}, {"option_values": {}}, {"option_values": {"fit": "oversize"}},
                {"size": "PRIVATE_URL_TOKEN"}, {"provider_url": "PRIVATE"}):
            with self.subTest(changes=changes):
                result = observe_target({**raw, **changes}).as_dict()
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["target"], {})
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_source_interest_never_defaults_missing_size_fit_or_quantity(self):
        for values in ({"product_id": 1}, {"product_id": 1, "size": "M", "quantity": 1},
                {"product_id": 1, "size": "M", "fit_option_code": "classic"}):
            self.assertEqual(resolve_source_interest({"values": values}).as_dict()["status"], "unknown")


class _RestockInventoryFixture:
    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        SourceCartCatalogTests.setUp(self)
        self.product, self.variant = self.products[0], self.variants[0]
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule = VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="M", stock=5, is_enabled=True)
        self.policy = ProductInventoryPolicy.objects.create(product=self.product, source="catalog_variant")
        self.now = timezone.now()
        self.target = {"product_id": self.product.pk, "color_variant_id": self.variant.pk, "size": "M",
            "fit_option_code": "classic", "option_values": {"fit": "classic"}, "quantity": 1}

    def warehouse(self, quantity=5):
        self.policy.source = "warehouse"
        self.policy.save(update_fields=["source", "updated_at"])
        category = StorageCategory.objects.create(name="Restock stock", slug="restock-observer-stock")
        subcategory = StorageSubcategory.objects.create(category=category, name="Classic blank", slug="classic-blank")
        VariantBlankLink.objects.create(variant=self.variant, option_key="fit=classic", storage_subcategory=subcategory)
        self.stock = StockItem.objects.create(subcategory=subcategory, color=self.variant.color, size="M", quantity=quantity)
        return self.stock

    def reserve(self, quantity=1):
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        client = IgClient.get_or_create_for_sender("restock-physical-reservation")
        proposal = create_or_update_proposal(client=client, pay_type="online_full", item_specs=[{
            "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": quantity,
            "size": "M", "fit_option_code": "classic"}])
        return proposal.inventory_reservations.get(state="active")


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class ExactRestockAvailabilityTests(_RestockInventoryFixture, TestCase):
    def observed(self, target=None):
        with CaptureQueriesContext(connection) as queries, \
                patch("management.services.instagram_bot._provider_http") as provider, \
                patch("management.services.instagram_bot.send_text") as sender:
            result = observe_target(target or self.target, now=self.now)
        provider.assert_not_called()
        sender.assert_not_called()
        self.assertLessEqual(len(queries), MAX_QUERIES)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in queries))
        return result

    def test_exact_numeric_size_and_shared_pool_are_both_required(self):
        result = self.observed()
        self.assertEqual(result.as_dict()["status"], "available", result.as_dict())
        with self.assertRaises(FrozenInstanceError):
            result._payload_json = "changed"
        copy = result.as_dict()
        copy["evidence"]["free_quantity"] = 100
        self.assertEqual(result.as_dict()["evidence"]["free_quantity"], 5)
        self.variant.stock = 0
        self.variant.save(update_fields=["stock"])
        self.assertEqual(self.observed().as_dict()["status"], "unavailable")
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 0
        self.rule.save(update_fields=["stock", "updated_at"])
        self.assertEqual(self.observed().as_dict()["status"], "unavailable")

    def test_null_disabled_missing_policy_or_untracked_never_prove_restock(self):
        self.rule.stock = None
        self.rule.save(update_fields=["stock", "updated_at"])
        self.assertEqual(self.observed().as_dict()["reason"], "exact_size_stock_untracked")
        self.rule.is_enabled = False
        self.rule.save(update_fields=["is_enabled", "updated_at"])
        self.assertEqual(self.observed().as_dict()["status"], "unknown")
        self.policy.source = "untracked"
        self.policy.save(update_fields=["source", "updated_at"])
        self.assertEqual(self.observed().as_dict()["reason"], "inventory_policy_unknown")
        self.policy.delete()
        self.assertEqual(self.observed().as_dict()["reason"], "inventory_policy_unknown")

    def test_specific_numeric_size_rule_overrides_generic_cap(self):
        VariantSizeRule.objects.create(variant=self.variant, fit_code="", size="M", stock=50, is_enabled=True)
        self.rule.stock = 1
        self.rule.save(update_fields=["stock", "updated_at"])
        result = self.observed({**self.target, "quantity": 2}).as_dict()
        self.assertEqual((result["status"], result["evidence"]["size_cap"]), ("unavailable", 1))

    def test_catalogue_active_and_paid_commitments_reduce_both_caps(self):
        from management.models import IgCheckoutInventoryReservation as Reservation
        reservation = self.reserve(quantity=4)
        self.now = timezone.now()
        result = self.observed({**self.target, "quantity": 2}).as_dict()
        self.assertEqual((result["status"], result["evidence"]["free_quantity"]), ("unavailable", 1))
        Reservation.objects.filter(pk=reservation.pk).update(state="paid_committed", expires_at=self.now - timedelta(days=1))
        result = self.observed({**self.target, "quantity": 2}).as_dict()
        self.assertEqual((result["status"], result["evidence"]["protected_quantity"]), ("unavailable", 4))

    def test_consumption_newer_than_numeric_count_requires_recount(self):
        from management.models import IgCheckoutInventoryReservation as Reservation
        reservation = self.reserve()
        Reservation.objects.filter(pk=reservation.pk).update(state="fulfilled", fulfilled_at=self.rule.updated_at + timedelta(seconds=1),
            consumed_at=self.rule.updated_at + timedelta(seconds=1))
        result = self.observed().as_dict()
        self.assertEqual((result["status"], result["reason"]), ("unknown", "stale_catalog_size_count"))

    def test_warehouse_uses_exact_physical_size_color_mapping_and_protection(self):
        from management.models import IgCheckoutInventoryReservation as Reservation
        stock = self.warehouse(quantity=3)
        self.rule.stock = 0  # Catalogue stock is not the warehouse physical source.
        self.rule.save(update_fields=["stock", "updated_at"])
        result = self.observed().as_dict()
        self.assertEqual((result["status"], result["evidence"]["stock_item_id"]), ("available", stock.pk))
        # Checkout still requires an enabled catalogue size. Prove warehouse
        # truth independently above, then use the supported reservation path.
        self.rule.stock = 3
        self.rule.save(update_fields=["stock", "updated_at"])
        reservation = self.reserve(quantity=2)
        self.now = timezone.now()
        Reservation.objects.filter(pk=reservation.pk).update(state="paid_committed", expires_at=self.now - timedelta(days=1))
        result = self.observed({**self.target, "quantity": 2}).as_dict()
        self.assertEqual((result["status"], result["evidence"]["free_quantity"]), ("unavailable", 1))

    def test_missing_or_case_ambiguous_warehouse_rows_never_pick_first(self):
        stock = self.warehouse()
        stock.size = "L"
        stock.save(update_fields=["size", "updated_at"])
        self.assertEqual(self.observed().as_dict()["reason"], "inventory_stock_identity_unknown")
        stock.size = "M"
        stock.save(update_fields=["size", "updated_at"])
        if connection.vendor == "mysql":
            # Native collation enforces the physical identity case-insensitively.
            with self.assertRaises(IntegrityError), transaction.atomic():
                StockItem.objects.create(subcategory=stock.subcategory, color=stock.color, size="m", quantity=5)
            self.assertEqual(self.observed().as_dict()["status"], "available")
        else:
            StockItem.objects.create(subcategory=stock.subcategory, color=stock.color, size="m", quantity=5)
            self.assertEqual(self.observed().as_dict()["reason"], "inventory_stock_identity_unknown")

    def test_other_variant_size_or_unknown_option_cannot_borrow_physical_stock(self):
        other = {**self.target, "color_variant_id": self.variants[1].pk}
        self.assertEqual(self.observed(other).as_dict()["status"], "unknown")
        target = {**self.target, "option_values": {"fit": "classic", "lining": "warm"}}
        self.assertEqual(self.observed(target).as_dict()["status"], "unknown")
        self.assertEqual(self.observed({**self.target, "size": "XXXL"}).as_dict()["status"], "unknown")

    def test_real_extra_axis_mapping_is_unknown_until_checkout_preserves_it(self):
        stock = self.warehouse()
        flow = GarmentFlow.objects.create(code="restock-extra-axis", name="Restock lining", axes=[{
            "code": "lining", "label": "Утеплення", "options": [{"code": "fleece", "label": "Фліс"}]}])
        GarmentFlowCategory.objects.create(flow=flow, category=self.product.category)
        VariantBlankLink.objects.create(variant=self.variant, option_key="fit=classic;lining=fleece", storage_subcategory=stock.subcategory)
        target = {**self.target, "option_values": {"fit": "classic", "lining": "fleece"}}
        result = self.observed(target).as_dict()
        self.assertEqual((result["status"], result["reason"]), ("unknown", "actual_checkout_options_unsupported"), result)

    def test_zero_stock_variant_source_is_resolved_by_shared_color_alias(self):
        self.variant.stock = 0
        self.variant.save(update_fields=["stock"])
        values = {"product_id": self.product.pk, "color": "чорний", "size": "M", "fit_option_code": "classic", "quantity": 1}
        result = resolve_source_interest({"values": values}, now=self.now).as_dict()
        self.assertEqual((result["status"], result["target"]["color_variant_id"]), ("unavailable", self.variant.pk), result)
        extra = ProductColorVariant.objects.create(product=self.product,
            color=Color.objects.create(name="Black extra", primary_hex="#222222"), stock=8)
        self.assertEqual(resolve_source_interest({"values": values}, now=self.now).as_dict()["reason"], "source_color_ambiguous")
        self.assertNotEqual(extra.pk, self.variant.pk)

    def test_query_and_rule_limits_fail_closed(self):
        with patch("management.services.ig_restock_availability.MAX_QUERIES", 1):
            self.assertEqual(observe_target(self.target, now=self.now).as_dict()["reason"], "inventory_query_bound")
        with patch("management.services.ig_restock_availability.MAX_RULES", 0):
            self.assertEqual(observe_target(self.target, now=self.now).as_dict()["reason"], "inventory_rule_bound")

    def test_positive_warehouse_callback_is_frozen_and_noop_negative_do_not_emit(self):
        from warehouse.services.inventory import adjust_stock_item, set_stock_quantity
        stock = self.warehouse()
        with patch("management.services.ig_restock_subscriptions.observe_inventory_event") as observe, self.captureOnCommitCallbacks(execute=True):
            movement = adjust_stock_item(stock_item=stock, delta=1)
            adjust_stock_item(stock_item=stock, delta=-1)
            set_stock_quantity(stock_item=stock, new_quantity=stock.quantity)
        observe.assert_called_once_with(source={"kind": "warehouse_movement", "stock_item_id": stock.pk,
            "movement_id": movement.pk, "occurred_at": movement.created_at})

    def test_rollback_discards_positive_warehouse_callback(self):
        from warehouse.services.inventory import adjust_stock_item
        stock = self.warehouse()
        with patch("management.services.ig_restock_subscriptions.observe_inventory_event") as observe, self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValueError), transaction.atomic():
                adjust_stock_item(stock_item=stock, delta=1)
                raise ValueError("abort")
        observe.assert_not_called()

    def test_catalogue_hook_captures_each_edit_revision_not_only_zero_crossing(self):
        staff = get_user_model().objects.create_user(username="restock-observer-editor", is_staff=True)
        self.client.force_login(staff)
        from management.services.bot_followups import variant_inventory_revision
        with patch("management.services.ig_restock_subscriptions.observe_inventory_event") as observe:
            with self.captureOnCommitCallbacks(execute=False) as first_callbacks:
                response = self.client.post(reverse("product_catalog_api_variant_save"), content_type="application/json", data=json.dumps({
                    "product_id": self.product.pk, "id": self.variant.pk, "color": {"id": self.variant.color_id},
                    "sizes": [{"fit_code": "classic", "size": "M", "is_enabled": True, "stock": 6}]}))
            first_revision = "product_catalog:" + variant_inventory_revision(self.variant.pk)
            self.assertEqual(response.status_code, 200)
            self.rule = VariantSizeRule.objects.get(variant=self.variant, fit_code="classic", size="M")
            self.rule.stock = 7
            self.rule.save(update_fields=["stock", "updated_at"])
            for callback in first_callbacks:
                callback()
        self.assertEqual(observe.call_count, 1)
        source = observe.call_args.kwargs["source"]
        self.assertEqual((source["kind"], source["product_id"], source["variant_id"], source["source_revision"]),
            ("catalog_edit", self.product.pk, self.variant.pk, first_revision))
        self.assertNotEqual(first_revision, "product_catalog:" + variant_inventory_revision(self.variant.pk))


@skipUnless(connection.vendor == "mysql", "Requires two real MariaDB connections")
@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeRestockSnapshotRaceTests(_RestockInventoryFixture, TransactionTestCase):
    def test_competing_physical_count_between_reads_rejects_snapshot(self):
        from management.services import ig_restock_availability as reader
        actual = reader._read
        competitor = connection.copy(alias="restock_observer_competitor")
        calls = 0
        def competing(target, now):
            nonlocal calls
            result = actual(target, now)
            calls += 1
            if calls == 1:
                with competitor.cursor() as cursor:
                    cursor.execute("UPDATE productcolors_productcolorvariant SET stock=0 WHERE id=%s", [self.variant.pk])
            return result
        try:
            with patch.object(reader, "_read", side_effect=competing):
                result = observe_target(self.target, now=self.now).as_dict()
            self.assertEqual((result["status"], result["reason"]), ("unknown", "inventory_snapshot_changed"))
        finally:
            competitor.close()


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class ExactRestockConfigurationTests(_RestockInventoryFixture, TestCase):
    def configuration(self, target=None):
        with CaptureQueriesContext(connection) as queries, \
                patch("management.services.instagram_bot._provider_http") as provider, \
                patch("management.services.instagram_bot.send_text") as sender:
            result = observe_configuration(target or self.target, now=self.now)
        provider.assert_not_called()
        sender.assert_not_called()
        self.assertLessEqual(len(queries), MAX_QUERIES)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in queries))
        self.assertEqual(set(result), {"version", "status", "target", "configuration_digest", "reason", "labels"})
        return result

    def test_zero_null_disabled_size_and_untracked_policy_keep_same_config_identity(self):
        original = self.configuration()
        self.assertEqual(original["status"], "valid", original)
        self.variant.stock = 0
        self.variant.save(update_fields=["stock"])
        for stock, enabled in ((0, True), (None, True), (None, False)):
            self.rule.stock, self.rule.is_enabled = stock, enabled
            self.rule.save(update_fields=["stock", "is_enabled", "updated_at"])
            result = self.configuration()
            self.assertEqual((result["status"], result["configuration_digest"]), ("valid", original["configuration_digest"]), result)
        self.policy.source = "untracked"
        self.policy.save(update_fields=["source", "updated_at"])
        self.assertEqual(self.configuration()["configuration_digest"], original["configuration_digest"])
        self.policy.delete()
        self.assertEqual(self.configuration()["configuration_digest"], original["configuration_digest"])
        self.assertNotIn("physical_quantity", json.dumps(original))
        self.assertNotIn("protected_quantity", json.dumps(original))

    def test_canonical_reservation_and_consumption_never_invalidate_manager_supply_config(self):
        from management.models import IgCheckoutInventoryReservation as Reservation
        original = self.configuration()
        reservation = self.reserve(quantity=2)
        self.assertEqual(self.configuration()["configuration_digest"], original["configuration_digest"])
        Reservation.objects.filter(pk=reservation.pk).update(state="paid_committed", expires_at=self.now - timedelta(days=1))
        self.assertEqual(self.configuration()["configuration_digest"], original["configuration_digest"])
        Reservation.objects.filter(pk=reservation.pk).update(state="fulfilled", consumed_at=self.now,
            fulfilled_at=self.now + timedelta(seconds=1))
        self.assertEqual(self.configuration()["configuration_digest"], original["configuration_digest"])

    def test_removed_exact_size_or_inactive_fit_is_invalid_not_unknown_stock(self):
        grid = self.product.size_grid
        grid.guide_data = {"columns": [{"key": "size", "label": "Size"}], "rows": [{"size": "L", "display_size": "L"}]}
        grid.save(update_fields=["guide_data"])
        result = self.configuration()
        self.assertEqual((result["status"], result["reason"], result["configuration_digest"]),
            ("invalid", "configuration_size_unavailable", ""))
        self.product.fit_options.filter(code="classic").update(is_active=False)
        self.assertEqual(self.configuration()["reason"], "fit_unavailable")

    def test_removed_variant_or_wrong_product_color_never_becomes_valid_config(self):
        result = self.configuration({**self.target, "color_variant_id": self.variants[1].pk})
        self.assertEqual((result["status"], result["reason"]), ("invalid", "variant_unavailable"))
        self.variant.delete()
        self.assertEqual(self.configuration()["status"], "invalid")

    def test_actual_axis_changes_invalidate_configuration_and_all_options_are_required(self):
        original = self.configuration()
        flow = GarmentFlow.objects.create(code="restock-config-axis", name="Supply lining", axes=[{
            "code": "lining", "label": "Утеплення", "options": [{"code": "fleece", "label": "Фліс"}]}])
        GarmentFlowCategory.objects.create(flow=flow, category=self.product.category)
        self.assertEqual(self.configuration()["status"], "invalid")
        result = self.configuration({**self.target, "option_values": {"fit": "classic", "lining": "fleece"}})
        self.assertEqual(result["status"], "valid", result)
        self.assertNotEqual(result["configuration_digest"], original["configuration_digest"])
        flow.axes[0]["options"][0]["disabled"] = True
        flow.save(update_fields=["axes"])
        self.assertEqual(self.configuration({**self.target, "option_values": {"fit": "classic", "lining": "fleece"}})["status"], "invalid")

    def test_quantity_is_bound_but_stock_editor_clock_and_price_are_not_config(self):
        original = self.configuration()
        result = self.configuration({**self.target, "quantity": 2})
        self.assertNotEqual(result["configuration_digest"], original["configuration_digest"])
        self.variant.price_override = 1111
        self.variant.save(update_fields=["price_override"])
        self.rule.note = "Physical count pending"
        self.rule.save(update_fields=["note", "updated_at"])
        self.assertEqual(self.configuration()["configuration_digest"], original["configuration_digest"])

    def test_two_fences_ignore_count_change_but_reject_configuration_change(self):
        from management.services import ig_restock_availability as observer
        before = observer._read_configuration(self.target)
        ProductColorVariant.objects.filter(pk=self.variant.pk).update(stock=0)
        after = observer._read_configuration(self.target)
        with patch.object(observer, "_read_configuration", side_effect=[before, after]):
            self.assertEqual(self.configuration()["status"], "valid")
        changed = deepcopy(after)
        changed["configuration_digest"] = "different-current-configuration"
        with patch.object(observer, "_read_configuration", side_effect=[before, changed]):
            result = self.configuration()
        self.assertEqual((result["status"], result["reason"], result["configuration_digest"]),
            ("unknown", "inventory_snapshot_changed", ""))

    def test_malformed_and_bounded_config_fails_without_inventory_authority(self):
        for changes in ({"quantity": True}, {"option_values": {}}, {"size": "XXXL"}):
            result = self.configuration({**self.target, **changes})
            self.assertEqual(result["status"], "invalid")
            self.assertEqual(result["configuration_digest"], "")
        with patch("management.services.ig_restock_availability.MAX_GRAPH_ROWS", 1):
            result = self.configuration()
        self.assertEqual((result["status"], result["reason"]), ("unknown", "configuration_bound"))

    def test_twenty_aligned_targets_share_two_preloads_without_per_target_queries(self):
        from management.services.ig_catalog_pricing import prepare_pricing_context
        original = self.configuration()
        other = {**self.target, "product_id": self.products[1].pk, "color_variant_id": self.variants[1].pk}
        targets = [deepcopy(self.target) if index % 2 == 0 else deepcopy(other) for index in range(20)]
        with CaptureQueriesContext(connection) as queries, patch(
                "management.services.ig_catalog_pricing.prepare_pricing_context", wraps=prepare_pricing_context) as prepare:
            results = observe_configurations(targets, now=self.now)
        self.assertEqual(len(results), len(targets))
        self.assertEqual(prepare.call_count, 2)
        self.assertLessEqual(len(queries), MAX_QUERIES)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in queries))
        for target, result in zip(targets, results):
            self.assertEqual((result["status"], result["target"]), ("valid", target), result)
        self.assertEqual(results[0]["configuration_digest"], original["configuration_digest"])
        results[0]["labels"]["product"] = "changed copy"
        self.assertNotEqual(results[2]["labels"]["product"], "changed copy")

    def test_batch_isolates_invalid_target_and_wrong_product_variant(self):
        wrong = {**self.target, "color_variant_id": self.variants[1].pk}
        results = observe_configurations([self.target, {**self.target, "quantity": True}, wrong, self.target], now=self.now)
        self.assertEqual([row["status"] for row in results], ["valid", "invalid", "invalid", "valid"])
        self.assertEqual(results[1]["target"], {})
        self.assertEqual(results[2]["reason"], "variant_unavailable")
        self.assertEqual(results[0]["configuration_digest"], results[3]["configuration_digest"])

    def test_batch_conflict_withholds_all_valid_proofs(self):
        from management.services import ig_restock_availability as observer
        first = [self.configuration(), self.configuration()]
        second = deepcopy(first)
        second[1]["configuration_digest"] = "changed-real-configuration"
        with patch.object(observer, "_read_configurations", side_effect=[first, second]):
            results = observe_configurations([self.target, self.target], now=self.now)
        self.assertEqual([row["status"] for row in results], ["unknown", "unknown"])
        self.assertTrue(all(row["reason"] == "inventory_snapshot_changed" and not row["configuration_digest"] for row in results))

    def test_batch_collection_limit_and_all_invalid_require_zero_reads(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(observe_configurations([], now=self.now), [])
            result = observe_configurations([None, {"quantity": True}], now=self.now)
            with self.assertRaises(ValueError):
                observe_configurations([self.target] * 21, now=self.now)
            with self.assertRaises(ValueError):
                observe_configurations({"target": self.target}, now=self.now)
        self.assertEqual(len(queries), 0)
        self.assertEqual([row["status"] for row in result], ["invalid", "invalid"])


from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeConfigurationReadbackTests(_RestockFixture, TestCase):
    def captured_readback(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        readback = {}
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now, _catalog_readback=readback)
        self.assertEqual(cart["status"], "captured", cart)
        self.assertTrue(cart["coverage_complete"], cart)
        self.assertTrue(readback.get("prepared"), readback.keys())
        target = {"product_id": self.product.pk, "color_variant_id": self.variant.pk, "size": "M",
            "fit_option_code": "classic", "option_values": {"fit": "classic"}, "quantity": 1}
        return cart, readback, target

    def test_actual_source_readback_matches_fresh_and_uses_only_one_catalog_preload(self):
        from management.services.ig_catalog_pricing import prepare_pricing_context
        cart, readback, target = self.captured_readback()
        with CaptureQueriesContext(connection) as fresh_queries:
            fresh = observe_configurations([target] * 16, now=self.now)
        with CaptureQueriesContext(connection) as reused_queries, patch(
                "management.services.ig_catalog_pricing.prepare_pricing_context", wraps=prepare_pricing_context) as prepare:
            reused = observe_configurations([target] * 16, now=self.now, source_cart=cart, _catalog_readback=readback)
        self.assertEqual(reused, fresh)
        self.assertEqual(prepare.call_count, 1)
        self.assertLess(len(reused_queries), len(fresh_queries))
        self.assertLessEqual(len(reused_queries), MAX_QUERIES)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in reused_queries))

    def test_wrong_owner_head_digest_product_set_or_unprepared_never_requeries(self):
        cart, readback, target = self.captured_readback()
        for key, value in (("scope", {**cart["scope"], "client_id": 999}), ("session_id", 999),
                ("generation", 999), ("selection_revision", 999), ("source_capture_digest", "b" * 64),
                ("products", ()), ("prepared", False)):
            with self.subTest(key=key), CaptureQueriesContext(connection) as queries:
                results = observe_configurations([target], now=self.now, source_cart=cart,
                    _catalog_readback={**readback, key: value})
            self.assertEqual(len(queries), 0)
            self.assertEqual((results[0]["status"], results[0]["reason"], results[0]["configuration_digest"]),
                ("unknown", "configuration_readback_invalid", ""))
        product = readback["products"][0]
        cache = product._prefetched_objects_cache.copy()
        try:
            product._prefetched_objects_cache.pop("fit_options", None)
            with CaptureQueriesContext(connection) as queries:
                results = observe_configurations([target], now=self.now, source_cart=cart, _catalog_readback=readback)
            self.assertEqual(len(queries), 0)
            self.assertEqual(results[0]["status"], "unknown")
        finally:
            product._prefetched_objects_cache = cache

    def test_reused_graph_does_not_mask_actual_configuration_change(self):
        cart, readback, target = self.captured_readback()
        self.product.fit_options.filter(code="classic").update(is_active=False)
        results = observe_configurations([target], now=self.now, source_cart=cart, _catalog_readback=readback)
        self.assertEqual((results[0]["status"], results[0]["reason"], results[0]["configuration_digest"]),
            ("unknown", "inventory_snapshot_changed", ""))

    def test_count_and_size_editor_noise_do_not_break_reused_configuration_proof(self):
        cart, readback, target = self.captured_readback()
        original = observe_configuration(target, now=self.now)
        self.variant.stock = 9
        self.variant.save(update_fields=["stock"])
        self.rule.stock, self.rule.is_enabled = None, False
        self.rule.save(update_fields=["stock", "is_enabled", "updated_at"])
        results = observe_configurations([target], now=self.now, source_cart=cart, _catalog_readback=readback)
        self.assertEqual(results, [original])
