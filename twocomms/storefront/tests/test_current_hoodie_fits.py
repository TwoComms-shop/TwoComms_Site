from io import StringIO
import json
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError, connection
from django.db.models.query import QuerySet
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from orders.models import Order, OrderItem
from storefront.models import Category, Product, ProductFitOption
from storefront.services.current_hoodie_fits import (
    AUTHORIZED_HOODIE_FITS, HoodieFitAlignmentError, align_current_hoodie_fits,
)


class CurrentHoodieFitAlignmentTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Reviewed hoodies", slug="hoodie", is_active=True)
        for product_id, (classic_id, oversize_id) in AUTHORIZED_HOODIE_FITS.items():
            product = Product.objects.create(pk=product_id, title=f"Hoodie {product_id}",
                slug=f"hoodie-{product_id}", category=self.category, price=1800,
                discount_percent=7, status="published")
            ProductFitOption.objects.create(pk=classic_id, product=product, code="classic", label="Legacy classic",
                description="Keep legacy description", is_active=True, is_default=True)
            ProductFitOption.objects.create(pk=oversize_id, product=product, code="oversize", label="Current oversize",
                is_active=True, is_default=False, order=1)
        self.other = Product.objects.create(pk=501, title="Unrelated hoodie", slug="other-hoodie",
            category=self.category, price=2300, status="published")
        ProductFitOption.objects.create(product=self.other, code="classic", label="Keep", is_default=True)
        # Bulk creation keeps notification side effects outside this fixture.
        order = Order(order_number="TEST-LEGACY-FIT", full_name="Local fixture", phone="0000000000",
            city="Test", np_office="Test", total_sum=1600)
        Order.objects.bulk_create([order])
        OrderItem.objects.bulk_create([OrderItem(order=order, product_id=102, title="Legacy item", size="M",
            fit_option_code="classic", fit_option_label="Original classic", unit_price=1600, line_total=1600,
            option_values={"fit": "classic"}, option_labels={"fit": "Original classic"})])

    def _state(self):
        return {"products": list(Product.objects.order_by("pk").values()),
                "fits": list(ProductFitOption.objects.order_by("pk").values()),
                "orders": list(Order.objects.values()), "items": list(OrderItem.objects.values())}

    def test_default_and_command_dry_run_only_read_exact_rows(self):
        before = self._state()
        with patch("storefront.services.current_hoodie_fits.bump_public_product_order_version") as cache:
            with CaptureQueriesContext(connection) as queries:
                plan = align_current_hoodie_fits()
            cache.assert_not_called()
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(len(plan["snapshot_hash"]), 64)
        self.assertEqual({row["product_id"] for row in plan["changes"]}, {102, 115})
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for query in queries))
        self.assertEqual(self._state(), before)
        stdout = StringIO()
        call_command("align_current_hoodie_fits", "--dry-run", stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue())["snapshot_hash"], plan["snapshot_hash"])

    def test_apply_preserves_products_prices_legacy_orders_and_invalidates_after_commit(self):
        before = self._state()
        plan = align_current_hoodie_fits()
        with patch("storefront.services.current_hoodie_fits.bump_public_product_order_version", return_value=88) as cache:
            with self.captureOnCommitCallbacks(execute=True):
                result = align_current_hoodie_fits(apply=True, expected_snapshot_hash=plan["snapshot_hash"])
                cache.assert_not_called()
            cache.assert_called_once_with()
        after = self._state()
        for key in ("products", "orders", "items"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(result["post_commit"], {"public_cache": "invalidated", "public_cache_version": 88})
        for product_id, (classic_id, oversize_id) in AUTHORIZED_HOODIE_FITS.items():
            classic, oversize = ProductFitOption.objects.get(pk=classic_id), ProductFitOption.objects.get(pk=oversize_id)
            self.assertFalse(classic.is_active)
            self.assertFalse(classic.is_default)
            self.assertTrue(oversize.is_active)
            self.assertTrue(oversize.is_default)
            self.assertEqual(classic.label, "Legacy classic")
        self.assertTrue(ProductFitOption.objects.get(product=self.other).is_default)
        replay_plan = align_current_hoodie_fits()
        self.assertEqual(replay_plan["changes"], [])
        with patch("storefront.services.current_hoodie_fits.bump_public_product_order_version") as cache:
            replay = align_current_hoodie_fits(apply=True, expected_snapshot_hash=replay_plan["snapshot_hash"])
            cache.assert_not_called()
        self.assertFalse(replay["changed"])
        self.assertEqual(replay["snapshot_hash"], result["snapshot_hash"])

    def test_missing_cas_or_changed_fit_or_product_version_prevents_apply(self):
        with self.assertRaises(HoodieFitAlignmentError) as missing:
            align_current_hoodie_fits(apply=True)
        self.assertEqual(missing.exception.code, "hoodie_fit_cas_required")
        plan = align_current_hoodie_fits()
        ProductFitOption.objects.filter(pk=67).update(description="Another window's reviewed edit")
        before = self._state()
        with self.assertRaises(HoodieFitAlignmentError) as changed:
            align_current_hoodie_fits(apply=True, expected_snapshot_hash=plan["snapshot_hash"])
        self.assertEqual(changed.exception.code, "hoodie_fit_cas_changed")
        self.assertEqual(self._state(), before)
        fresh_plan = align_current_hoodie_fits()
        Product.objects.filter(pk=102).update(price=1900)
        before = self._state()
        with self.assertRaises(HoodieFitAlignmentError) as changed:
            align_current_hoodie_fits(apply=True, expected_snapshot_hash=fresh_plan["snapshot_hash"])
        self.assertEqual(changed.exception.code, "hoodie_fit_cas_changed")
        self.assertEqual(self._state(), before)

    def test_scope_or_profile_drift_is_rejected_before_any_writes(self):
        plan = align_current_hoodie_fits()
        Product.objects.filter(pk=115).update(status="draft")
        before = self._state()
        with self.assertRaises(HoodieFitAlignmentError) as invalid:
            align_current_hoodie_fits(apply=True, expected_snapshot_hash=plan["snapshot_hash"])
        self.assertEqual(invalid.exception.code, "hoodie_product_scope_changed")
        self.assertEqual(self._state(), before)
        Product.objects.filter(pk=115).update(status="published")
        ProductFitOption.objects.filter(pk=68).update(is_active=False)
        with self.assertRaises(HoodieFitAlignmentError) as invalid:
            align_current_hoodie_fits()
        self.assertEqual(invalid.exception.code, "hoodie_fit_profile_changed")

    def test_mid_write_failure_rolls_back_flags_and_does_not_schedule_cache(self):
        before = self._state()
        plan = align_current_hoodie_fits()
        original_update, calls = QuerySet.update, []
        def fail_second_fit_update(queryset, **values):
            if queryset.model is ProductFitOption:
                calls.append(values)
                if len(calls) == 2:
                    raise IntegrityError("Injected local fit write failure")
            return original_update(queryset, **values)
        with patch.object(QuerySet, "update", fail_second_fit_update):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(IntegrityError):
                    align_current_hoodie_fits(apply=True, expected_snapshot_hash=plan["snapshot_hash"])
            self.assertEqual(callbacks, [])
        self.assertEqual(self._state(), before)

    def test_cache_failure_is_reported_as_committed_data_needing_refresh(self):
        plan = align_current_hoodie_fits()
        with patch("storefront.services.current_hoodie_fits.bump_public_product_order_version", side_effect=RuntimeError("cache unavailable")):
            with self.captureOnCommitCallbacks(execute=True):
                result = align_current_hoodie_fits(apply=True, expected_snapshot_hash=plan["snapshot_hash"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["post_commit"], {"public_cache": "failed", "error": "cache_refresh_failed"})
        self.assertEqual(align_current_hoodie_fits()["changes"], [])
