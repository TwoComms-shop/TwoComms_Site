"""Native partial guard upgrades seeded only by actual commerce producers."""
from copy import deepcopy
from importlib import import_module

from django.apps import apps
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
from management.services import ig_assisted_commerce as commerce
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture
from management.tests_ig_assisted_commerce_models import NativeAssistedCommerceModelsTests


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeAssistedPartialModelsTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    raw_clone = NativeAssistedCommerceModelsTests.raw_clone
    rejected_clone = NativeAssistedCommerceModelsTests.rejected_clone
    raw_update = NativeAssistedCommerceModelsTests.raw_update

    def _guard_upgrade(self):
        migration = import_module("management.migrations.0234_assisted_partial_commerce")
        with connection.schema_editor() as editor:
            migration.enable_partial_guards(apps, editor)

    def _constraint_states(self):
        migration = import_module("management.migrations.0234_assisted_partial_commerce")
        loader = MigrationExecutor(connection).loader
        old = loader.project_state([("management", "0233_assisted_commerce")])
        new = loader.project_state([("management", "0234_assisted_partial_commerce")])
        operation = next(value for value in migration.Migration.operations
            if isinstance(value, migration.ResumableAssistedAddConstraint))
        return migration, operation, old, new

    def test_guard_reinstall_keeps_released_full_quote_columns_hashes_and_owner_shape(self):
        result = self._custom_proposal()
        rows = [result["request"], result["decision"], result["execution"]]
        frozen = [(deepcopy(row.envelope), row.envelope_hmac) for row in rows]
        self._guard_upgrade()
        self._guard_upgrade()
        for row, expected, kind in zip(rows, frozen, ("request", "decision", "execution")):
            row.refresh_from_db()
            self.assertEqual((row.envelope, row.envelope_hmac), expected)
            self.assertTrue(commerce.verify_assisted_evidence(row, kind))
        execution = rows[-1]
        self.assertEqual(execution.scope, "full")
        self.assertEqual(execution.quoted_total, result["decision"].approved_total)
        self.assertEqual(execution.quoted_custom_quantity, result["decision"].approved_custom_quantity)
        self.assertEqual(execution.active_slot, 1)
        self.assertIsNone(execution.order_id)
        expected_fields = {
            "envelope", "key_id", "envelope_hmac", "created_at", "id", "intention_id", "operation_id",
            "request", "decision", "proposal", "client", "episode", "choice_source", "purchase_source",
            "revision", "scope", "scope_digest", "request_digest", "decision_digest", "full_capture_digest",
            "snapshot_digest", "currency", "quoted_total", "quoted_custom_quantity", "expires_at",
            "active_slot", "order", "settled_at", "released_at", "release_reason",
        }
        self.assertEqual({field.name for field in Execution._meta.concrete_fields}, expected_fields)

    def test_full_approval_cannot_be_relabelled_partial_without_approved_scope_quote(self):
        result = self._custom_proposal()
        execution = result["execution"]
        self.assertNotIn("scoped_quotes", result["decision"].envelope["snapshot"])
        self.rejected_clone(execution, {"scope": "partial", "choice_source_id": result["buy"].pk})
        self.rejected_clone(execution, {"scope": "partial", "choice_source_id": None})
        self.assertEqual(Execution.objects.count(), 1)

    def test_partial_check_rerun_and_reverse_restore_full_guards_without_rewriting_evidence(self):
        result = self._custom_proposal()
        frozen = deepcopy(result["execution"].envelope)
        migration, operation, old, new = self._constraint_states()
        # Reapplying the already-present CHECK is a shape verification, never
        # a duplicate DDL failure. Reversal leaves the actual full39 row intact.
        try:
            with connection.schema_editor() as editor:
                operation.database_forwards("management", editor, old, new)
                operation.database_forwards("management", editor, old, new)
                operation.database_backwards("management", editor, new, old)
                operation.database_backwards("management", editor, new, old)
                migration.restore_full_guards(old.apps, editor)
            self.rejected_clone(result["execution"], {"scope": "partial", "choice_source_id": result["buy"].pk})
        finally:
            with connection.schema_editor() as editor:
                operation.database_forwards("management", editor, old, new)
                migration.enable_partial_guards(new.apps, editor)
        result["execution"].refresh_from_db()
        self.assertEqual(result["execution"].envelope, frozen)
        self.assertTrue(commerce.verify_assisted_evidence(result["execution"], "execution"))

    def test_full_native_terminal_and_immutable_guards_survive_scope_upgrade(self):
        result = self._custom_proposal()
        self._guard_upgrade()
        execution = result["execution"]
        for changes in ({"active_slot": None}, {"quoted_total": execution.quoted_total + 1},
                {"scope": "partial"}, {"choice_source_id": result["buy"].pk}):
            with self.subTest(changes=changes), self.assertRaises(DatabaseError), transaction.atomic():
                self.raw_update(execution, **changes)
        execution.refresh_from_db()
        self.assertEqual((execution.scope, execution.active_slot, execution.order_id), ("full", 1, None))
