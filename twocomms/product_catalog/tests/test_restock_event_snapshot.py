"""Staff inventory edits keep their own revision through delayed callbacks.

These exercise the real API, catalogue rows and restock materializer. Callback
capture models a second committed edit overtaking notification preparation; it
does not patch availability, source validation or consent authority.
"""
import json
import hashlib
from contextlib import contextmanager
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from management.models import IgBotNotification, IgClient, IgFollowUpTask, InstagramBotMessage
from management.services import bot_followups
from product_catalog.models import VariantFitRule, VariantSizeRule
from product_catalog.services import variant_allows_purchase
from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, Category, Product, ProductFitOption, ProductStatus, RestockSubscription


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class RestockEventSnapshotApiTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="restock-snapshot-editor", is_staff=True)
        self.client.force_login(self.staff)
        self.catalog = Catalog.objects.create(name="Snapshot shirts", slug="snapshot-shirts")
        self.category = Category.objects.create(name="Snapshot shirts", slug="snapshot-shirts-category")
        self.product, self.variant = self._product_variant("primary", "#112233")
        self.url = reverse("product_catalog_api_variant_save")

    def _product_variant(self, name, color_hex):
        product = Product.objects.create(title=f"Snapshot {name}", slug=f"snapshot-{name}",
            category=self.category, catalog=self.catalog, price=1000, status=ProductStatus.PUBLISHED)
        ProductFitOption.objects.create(product=product, code="oversize", label="Оверсайз",
            is_active=True, is_default=True)
        variant = self._variant(product, name, color_hex)
        return product, variant

    def _variant(self, product, name, color_hex):
        variant = ProductColorVariant.objects.create(product=product,
            color=Color.objects.create(name=f"Snapshot {name}", primary_hex=color_hex))
        VariantFitRule.objects.create(variant=variant, fit_code="oversize", is_enabled=True)
        VariantSizeRule.objects.create(variant=variant, fit_code="oversize", size="M",
            is_enabled=True, stock=0)
        return variant

    def _save(self, *, product=None, variant=None, stock=5):
        product, variant = product or self.product, variant or self.variant
        return self.client.post(self.url, data=json.dumps({
            "product_id": product.pk, "id": variant.pk, "color": {"id": variant.color_id},
            "sizes": [{"fit_code": "oversize", "size": "M", "is_enabled": True, "stock": stock}],
        }), content_type="application/json")

    def _rules(self, variant):
        return list(VariantSizeRule.objects.filter(variant=variant).order_by("fit_code", "size")
            .values("pk", "fit_code", "size", "is_enabled", "stock", "updated_at"))

    def _revision(self, variant=None):
        return f"product_catalog:{bot_followups.variant_inventory_revision((variant or self.variant).pk)}"

    def _waiting_customer(self):
        # This legacy gap is an advisory match, never a subscription or consent.
        return IgClient.objects.create(igsid="snapshot-waiting-customer", current_product=self.product,
            current_size="M", last_user_message_at=timezone.now(), sales_context={
                "assisted_checkout_selection": {"product_id": self.product.pk,
                    "color_variant_id": self.variant.pk, "fit_option_code": "oversize"},
                "_stock_gap": {"product_id": self.product.pk, "variant_id": self.variant.pk,
                    "size": "M", "fit_code": "oversize", "option_values": {"fit": "oversize"},
                    "published": True, "at": timezone.now().isoformat()},
            })

    def _run_observed(self, callbacks):
        actual = bot_followups.materialize_restock_inventory_event
        results = []
        def record_actual(**kwargs):
            result = actual(**kwargs)
            results.append(result)
            return result
        with patch.object(bot_followups, "materialize_restock_inventory_event", side_effect=record_actual) as materialize:
            for callback in callbacks:
                callback()
        return materialize, results

    def test_delayed_callback_retains_edit_revision_and_real_materializer_rejects_later_inventory(self):
        customer = self._waiting_customer()
        original_gap = dict(customer.sales_context["_stock_gap"])
        counts = (IgFollowUpTask.objects.count(), IgBotNotification.objects.count(),
            InstagramBotMessage.objects.count(), RestockSubscription.objects.count())
        actual_revision = bot_followups.variant_inventory_revision
        read_depths = []
        baseline_depth = len(connection.savepoint_ids)
        def observe_revision(variant_id):
            read_depths.append(len(connection.savepoint_ids))
            return actual_revision(variant_id)
        with self.captureOnCommitCallbacks(execute=False) as callbacks, patch.object(
                bot_followups, "variant_inventory_revision", side_effect=observe_revision) as revision_read:
            response = self._save()
        self.assertEqual(response.status_code, 200, response.content)
        revision_read.assert_called_once_with(self.variant.pk)
        self.assertEqual(len(read_depths), 1)
        self.assertGreater(read_depths[0], baseline_depth)
        frozen_revision = self._revision()
        self.assertTrue(callbacks)
        # Availability remains true: only the event's captured revision becomes
        # stale, so rejection cannot be explained by the size being unavailable.
        rule = VariantSizeRule.objects.get(variant=self.variant, fit_code="oversize", size="M")
        rule.stock = 6
        rule.save(update_fields=["stock", "updated_at"])
        self.assertNotEqual(self._revision(), frozen_revision)
        self.assertTrue(variant_allows_purchase(self.product, self.variant, fit_code="oversize",
            size="M", option_values={"fit": "oversize"}))
        with patch("management.services.instagram_bot._provider_http",
                side_effect=AssertionError("Inventory callbacks cannot notify customers")) as provider:
            materialize, results = self._run_observed(callbacks)
        provider.assert_not_called()
        materialize.assert_called_once()
        self.assertEqual(materialize.call_args.kwargs["source_revision"], frozen_revision)
        self.assertEqual(materialize.call_args.kwargs["product_id"], self.product.pk)
        self.assertEqual(materialize.call_args.kwargs["variant_id"], self.variant.pk)
        self.assertEqual(materialize.call_args.kwargs["size"], "M")
        self.assertEqual(materialize.call_args.kwargs["fit_code"], "oversize")
        self.assertEqual(materialize.call_args.kwargs["option_values"], {"fit": "oversize"})
        self.assertTrue(timezone.is_aware(materialize.call_args.kwargs["occurred_at"]))
        self.assertEqual(results, [0])
        self.assertEqual((IgFollowUpTask.objects.count(), IgBotNotification.objects.count(),
            InstagramBotMessage.objects.count(), RestockSubscription.objects.count()), counts)
        customer.refresh_from_db()
        self.assertEqual(customer.sales_context["_stock_gap"], original_gap)

    def test_same_available_inventory_save_does_not_emit_a_second_restock_transition(self):
        with self.captureOnCommitCallbacks(execute=False) as first_callbacks:
            first = self._save()
        self.assertEqual(first.status_code, 200, first.content)
        first_event, _results = self._run_observed(first_callbacks)
        first_event.assert_called_once()
        self.assertEqual(first_event.call_args.kwargs["source_revision"], self._revision())
        counts = (IgFollowUpTask.objects.count(), IgBotNotification.objects.count())
        with self.captureOnCommitCallbacks(execute=False) as duplicate_callbacks:
            duplicate = self._save()
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        duplicate_event, duplicate_results = self._run_observed(duplicate_callbacks)
        duplicate_event.assert_not_called()
        self.assertEqual(duplicate_results, [])
        self.assertEqual((IgFollowUpTask.objects.count(), IgBotNotification.objects.count()), counts)

    def test_edit_and_callback_are_isolated_to_exact_product_and_variant(self):
        same_product_variant = self._variant(self.product, "other-color", "#445566")
        other_product, other_variant = self._product_variant("other-product", "#778899")
        other_rows = {variant.pk: self._rules(variant) for variant in (same_product_variant, other_variant)}
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self._save()
        self.assertEqual(response.status_code, 200, response.content)
        frozen_revision = self._revision()
        materialize, _results = self._run_observed(callbacks)
        materialize.assert_called_once()
        self.assertEqual(materialize.call_args.kwargs["product_id"], self.product.pk)
        self.assertEqual(materialize.call_args.kwargs["variant_id"], self.variant.pk)
        self.assertEqual(materialize.call_args.kwargs["source_revision"], frozen_revision)
        for variant in (same_product_variant, other_variant):
            with self.subTest(variant=variant.pk):
                self.assertEqual(self._rules(variant), other_rows[variant.pk])
        self.assertNotEqual(other_product.pk, self.product.pk)

    def test_variant_from_another_product_is_rejected_without_inventory_event(self):
        other_product, other_variant = self._product_variant("foreign-product", "#99aabb")
        own_rules, foreign_rules = self._rules(self.variant), self._rules(other_variant)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self._save(product=self.product, variant=other_variant)
        self.assertEqual(response.status_code, 404)
        materialize, results = self._run_observed(callbacks)
        materialize.assert_not_called()
        self.assertEqual(results, [])
        self.assertEqual(self._rules(self.variant), own_rules)
        self.assertEqual(self._rules(other_variant), foreign_rules)
        self.assertEqual(other_variant.product_id, other_product.pk)

    def test_resolved_editor_view_is_explicitly_non_atomic_for_default_database(self):
        self.assertIn("default", getattr(resolve(self.url).func, "_non_atomic_requests", set()))

    @skipUnless(connection.vendor == "mysql", "Real SELECT FOR UPDATE requires native MariaDB")
    def test_native_editor_locks_product_before_variant_within_edit_transaction(self):
        locked_tables = []
        baseline_depth = len(connection.savepoint_ids)
        def observe_sql(execute, sql, params, many, context):
            if "FOR UPDATE" in sql.upper():
                for model in (Product, ProductColorVariant):
                    if f"FROM `{model._meta.db_table}`" in sql:
                        locked_tables.append((model._meta.db_table, len(connection.savepoint_ids)))
            return execute(sql, params, many, context)
        with self.captureOnCommitCallbacks(execute=False), connection.execute_wrapper(observe_sql):
            response = self._save()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreaterEqual(len(locked_tables), 2)
        self.assertEqual([table for table, _depth in locked_tables[:2]],
            [Product._meta.db_table, ProductColorVariant._meta.db_table])
        self.assertGreater(locked_tables[0][1], baseline_depth)
        self.assertEqual(locked_tables[0][1], locked_tables[1][1])


@skipUnless(connection.vendor == "mysql", "Requires root-owned disposable MariaDB")
@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class RestockEditorAdvisoryLockNativeTests(TransactionTestCase):
    """Two physical sessions prove the engine-independent advisory contract.

    Disposable parent tables may be InnoDB while production parents are MyISAM;
    these tests neither change engines nor infer exclusion from row-lock SQL.
    """
    setUp = RestockEventSnapshotApiTests.setUp
    _product_variant = RestockEventSnapshotApiTests._product_variant
    _variant = RestockEventSnapshotApiTests._variant
    _save = RestockEventSnapshotApiTests._save
    _rules = RestockEventSnapshotApiTests._rules

    def _lock_key(self, product=None):
        database_key = hashlib.sha256(str(connection.settings_dict["NAME"]).encode()).hexdigest()[:12]
        return f"twc:catalog-edit:{database_key}:{(product or self.product).pk}"

    @contextmanager
    def _competing_owner(self, product=None):
        competitor = connection.copy(alias="restock_native_editor_competitor")
        key = self._lock_key(product)
        try:
            competitor.ensure_connection()
            with competitor.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0)", [key])
                self.assertEqual(cursor.fetchone()[0], 1)
                cursor.execute("SELECT CONNECTION_ID()")
                owner_id = cursor.fetchone()[0]
            yield competitor, owner_id
        finally:
            if competitor.connection is not None:
                with competitor.cursor() as cursor:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", [key])
            competitor.close()

    def _assert_lock_free(self, product=None):
        with connection.cursor() as cursor:
            cursor.execute("SELECT IS_FREE_LOCK(%s)", [self._lock_key(product)])
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_competing_same_product_save_times_out_without_mutation_then_succeeds_after_release(self):
        before = self._rules(self.variant)
        actual = bot_followups.materialize_restock_inventory_event
        blocked_dml = []
        def observe_sql(execute, sql, params, many, context):
            if sql.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
                blocked_dml.append(sql)
            return execute(sql, params, many, context)
        with patch.object(bot_followups, "materialize_restock_inventory_event", wraps=actual) as materialize, patch(
                "management.services.instagram_bot._provider_http",
                side_effect=AssertionError("Inventory saves cannot send customer messages")) as provider:
            with self._competing_owner() as (_competitor, owner_id):
                with connection.execute_wrapper(observe_sql):
                    blocked = self._save()
                self.assertEqual(blocked.status_code, 409, blocked.content)
                self.assertEqual(blocked_dml, [])
                self.assertEqual(self._rules(self.variant), before)
                materialize.assert_not_called()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT IS_USED_LOCK(%s)", [self._lock_key()])
                    self.assertEqual(cursor.fetchone()[0], owner_id)
            self._assert_lock_free()
            saved = self._save()
            self.assertEqual(saved.status_code, 200, saved.content)
            materialize.assert_called_once()
        provider.assert_not_called()
        self.assertEqual(VariantSizeRule.objects.get(variant=self.variant, fit_code="oversize", size="M").stock, 5)
        self._assert_lock_free()

    def test_lock_on_one_product_does_not_block_another_product_editor(self):
        other_product, other_variant = self._product_variant("independent", "#aabbcc")
        own_rules = self._rules(self.variant)
        actual = bot_followups.materialize_restock_inventory_event
        with self._competing_owner() as (_competitor, owner_id), patch.object(
                bot_followups, "materialize_restock_inventory_event", wraps=actual) as materialize:
            saved = self._save(product=other_product, variant=other_variant)
            self.assertEqual(saved.status_code, 200, saved.content)
            materialize.assert_called_once()
            self.assertEqual(materialize.call_args.kwargs["product_id"], other_product.pk)
            self.assertEqual(materialize.call_args.kwargs["variant_id"], other_variant.pk)
            self.assertEqual(self._rules(self.variant), own_rules)
            with connection.cursor() as cursor:
                cursor.execute("SELECT IS_USED_LOCK(%s)", [self._lock_key()])
                self.assertEqual(cursor.fetchone()[0], owner_id)
            self._assert_lock_free(other_product)
        self._assert_lock_free()

    def test_real_lock_spans_commit_callbacks_even_with_atomic_requests_enabled(self):
        actual = bot_followups.materialize_restock_inventory_event
        observed = []
        competitor = connection.copy(alias="restock_native_callback_competitor")
        try:
            competitor.ensure_connection()
            def observe_callback(**kwargs):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT CONNECTION_ID()")
                    primary_id = cursor.fetchone()[0]
                with competitor.cursor() as cursor:
                    cursor.execute("SELECT IS_USED_LOCK(%s)", [self._lock_key()])
                    observed.append((cursor.fetchone()[0], primary_id, connection.in_atomic_block))
                    cursor.execute("SELECT GET_LOCK(%s, 0)", [self._lock_key()])
                    self.assertEqual(cursor.fetchone()[0], 0)
                return actual(**kwargs)
            # Django's real request handler reads the effective connection
            # setting. The resolved non-atomic marker must prevent an outer
            # request transaction from delaying callbacks past lock release.
            with patch.dict(connection.settings_dict, {"ATOMIC_REQUESTS": True}), patch.object(
                    bot_followups, "materialize_restock_inventory_event", side_effect=observe_callback):
                saved = self._save()
            self.assertEqual(saved.status_code, 200, saved.content)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], observed[0][1])
            self.assertFalse(observed[0][2])
            self._assert_lock_free()
        finally:
            competitor.close()

    def test_invalid_or_unauthorized_requests_never_acquire_edit_lock_or_write(self):
        statements = []
        def observe_sql(execute, sql, params, many, context):
            operation = sql.lstrip().split(None, 1)[0].upper()
            if operation in {"INSERT", "UPDATE", "DELETE", "REPLACE"} or "GET_LOCK(" in sql.upper():
                statements.append(sql)
            return execute(sql, params, many, context)
        before = self._rules(self.variant)
        for body in ("{invalid-json", "[]", '{"product_id":0}', '{"product_id":"invalid"}',
                '{"product_id":true}', '{"product_id":1.5}'):
            with self.subTest(body=body), connection.execute_wrapper(observe_sql):
                response = self.client.post(self.url, data=body, content_type="application/json")
                self.assertEqual(response.status_code, 400, response.content)
        self.client.logout()
        with connection.execute_wrapper(observe_sql):
            unauthorized = self._save()
        self.assertEqual(unauthorized.status_code, 403)
        self.assertEqual(statements, [])
        self.assertEqual(self._rules(self.variant), before)
        self._assert_lock_free()

    def test_missing_color_exception_releases_real_lock_without_inventory_change(self):
        # An actual missing Color raises DoesNotExist after the lock is acquired;
        # staff_api translates it into the existing generic HTTP400 response.
        # No mocked exception or unlocked alternative writer is involved.
        before = self._rules(self.variant)
        missing_color_id = (Color.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1000
        response = self.client.post(self.url, data=json.dumps({"product_id": self.product.pk,
            "id": self.variant.pk, "color": {"id": missing_color_id},
            "sizes": [{"fit_code": "oversize", "size": "M", "is_enabled": True, "stock": 5}],
        }), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._rules(self.variant), before)
        self._assert_lock_free()
        with self._competing_owner():
            pass  # A distinct physical session can immediately own the same key.
        self._assert_lock_free()
