"""Interrupted split DDL must resume with schema evidence, without fake-apply.

The native retry test uses Django's disposable test database and the ordinary
MigrationExecutor. It deliberately leaves complete DDL unrecorded before retry.
"""
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.tests_ig_restock_subscriptions import _RestockFixture
from management import tests_ig_split_shipping as shipping_tests
from management.services import ig_split_shipping as shipping
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule


MIGRATION = "management.migrations.0232_split_shipping"
BEFORE = ("management", "0231_fulfillment_stock_sync")
TARGET = ("management", "0232_split_shipping")


class SplitSchemaEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.schema = import_module(MIGRATION)

    def test_mysql_type_aliases_preserve_precision_and_unsigned_evidence(self):
        normalize = self.schema._normalize_type
        self.assertEqual(normalize("numeric(10, 2)"), normalize("decimal(10,2)"))
        self.assertEqual(normalize("integer AUTO_INCREMENT"), normalize("int(11)"))
        self.assertEqual(normalize("integer UNSIGNED"), normalize("int(10) unsigned"))
        self.assertNotEqual(normalize("decimal(10,2)"), normalize("decimal(10,3)"))
        self.assertNotEqual(normalize("integer unsigned"), normalize("int(11)"))

    def test_existing_wrong_fulfilled_quantity_type_is_rejected(self):
        editor = Mock()
        editor.connection.vendor = "sqlite"
        editor.connection.introspection.get_field_type.return_value = "TextField"
        field = models.PositiveIntegerField(default=0)
        field.set_attributes_from_name("fulfilled_quantity")
        column = SimpleNamespace(type_code="TEXT", null_ok=False, internal_size=None)
        with self.assertRaisesMessage(RuntimeError, "expected PositiveIntegerField"):
            self.schema._validate_existing_field(editor, "management_igcheckoutinventoryreservation", field, column)
        editor.add_field.assert_not_called()

    def test_existing_uuid_with_wrong_physical_width_is_rejected(self):
        editor = Mock()
        editor.connection.vendor = "sqlite"
        field = models.UUIDField(primary_key=True)
        field.set_attributes_from_name("id")
        column = SimpleNamespace(type_code="char(36)", null_ok=False)
        with self.assertRaisesMessage(RuntimeError, "incompatible UUID storage"):
            self.schema._validate_existing_field(editor, "management_igsplitparcel", field, column)

    def test_native_uuid_uses_storage_type_not_driver_buffer_length(self):
        editor = MagicMock()
        editor.connection.vendor = "mysql"
        editor.connection.data_types = {"UUIDField": "uuid"}
        field = models.UUIDField(primary_key=True)
        field.set_attributes_from_name("id")
        column = SimpleNamespace(type_code=254, null_ok=False, internal_size=144, default=None)
        cursor = editor.connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("uuid",)
        self.schema._validate_existing_field(editor, "management_igsplitparcel", field, column)
        cursor.fetchone.return_value = ("char(36)",)
        with self.assertRaisesMessage(RuntimeError, "incompatible physical type"):
            self.schema._validate_existing_field(editor, "management_igsplitparcel", field, column)

    def test_existing_wrong_default_is_rejected_but_interrupted_add_default_is_valid(self):
        editor = Mock()
        editor.connection.vendor = "sqlite"
        editor.connection.introspection.get_field_type.return_value = "PositiveIntegerField"
        field = models.PositiveIntegerField(default=0)
        field.set_attributes_from_name("fulfilled_quantity")
        column = SimpleNamespace(type_code="integer unsigned", null_ok=False, internal_size=None, default="7")
        with self.assertRaisesMessage(RuntimeError, "incompatible database default"):
            self.schema._validate_existing_field(editor, "management_igcheckoutinventoryreservation", field, column)
        column.default = "0"
        self.schema._validate_existing_field(editor, "management_igcheckoutinventoryreservation", field, column)

    def test_same_name_wrong_check_expression_is_rejected(self):
        model = SimpleNamespace(_meta=SimpleNamespace(db_table="management_igcheckoutinventoryreservation"))
        constraint = models.CheckConstraint(condition=models.Q(fulfilled_quantity__lte=models.F("quantity")),
            name="ig_res_fulfilled_lte_qty")
        actual = {"check": True, "columns": ["fulfilled_quantity", "quantity"], "index": False,
            "unique": False, "primary_key": False, "foreign_key": None}
        with patch.object(self.schema, "_check_clause", return_value="fulfilled_quantity >= quantity"):
            with self.assertRaisesMessage(RuntimeError, "incompatible check expression"):
                self.schema._validate_check(Mock(), model, constraint, actual)
        with patch.object(self.schema, "_check_clause", return_value='("fulfilled_quantity" <= ("quantity"))'):
            self.schema._validate_check(Mock(), model, constraint, actual)

    def test_same_name_non_check_and_wrong_unique_shapes_are_rejected(self):
        constraint = SimpleNamespace(name="ig_res_fulfilled_lte_qty")
        with self.assertRaisesMessage(RuntimeError, "incompatible constraint type"):
            self.schema._validate_check(Mock(), Mock(), constraint, {"unique": True, "check": False})
        for shape in ({"unique": False, "columns": ["plan_id", "key"]},
            {"unique": True, "columns": ["key", "plan_id"]},
            {"unique": True, "primary_key": True, "columns": ["plan_id", "key"]}):
            with self.assertRaisesMessage(RuntimeError, "incompatible unique constraint"):
                self.schema._validate_unique(shape, ["plan_id", "key"], "ig_split_parcel_key")

    def test_engine_preflight_rejects_unexpected_table_before_any_alter(self):
        editor = MagicMock()
        editor.connection.vendor = "mysql"
        cursor = editor.connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("MyISAM", "BASE TABLE"), ("MEMORY", "BASE TABLE")]
        with self.assertRaisesMessage(RuntimeError, "Unexpected split-shipping table/engine"):
            self.schema.ensure_transactions(Mock(), editor)
        self.assertTrue(cursor.execute.call_args_list)
        self.assertTrue(all(call.args[0].startswith("SELECT ") for call in cursor.execute.call_args_list))

    def test_repeated_engine_conversion_only_changes_remaining_myisam(self):
        table = self.schema.TRANSACTION_TABLES[0]
        native = MagicMock()
        native.ops.quote_name.side_effect = lambda value: "`" + value + "`"
        cursor = native.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("InnoDB", "BASE TABLE"), ("InnoDB",)]
        self.schema._require_innodb(native, table)
        self.assertFalse(any(call.args[0].startswith("ALTER ") for call in cursor.execute.call_args_list))
        cursor.reset_mock()
        cursor.fetchone.side_effect = [("MyISAM", "BASE TABLE"), ("InnoDB",)]
        self.schema._require_innodb(native, table)
        alters = [call.args[0] for call in cursor.execute.call_args_list if call.args[0].startswith("ALTER ")]
        self.assertEqual(alters, [f"ALTER TABLE `{table}` ENGINE=InnoDB"])
        with self.assertRaisesMessage(RuntimeError, "outside its owned tables"):
            self.schema._require_innodb(native, "unrelated_table")


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True)
class SplitSchemaInterruptedMigrationTests(_RestockFixture, TransactionTestCase):
    manager_offer = shipping_tests.NativeSplitShippingTests.manager_offer
    options = shipping_tests.NativeSplitShippingTests.options
    offer = shipping_tests.NativeSplitShippingTests.offer

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="split-schema-manager",
            email="split-schema@example.test", password="test")
        self.canonical("количество 2")
        second, variant = self.products[1], self.variants[1]
        variant.stock = 0
        variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=second, source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        self.canonical("Хочу купити")

    def _triggers(self):
        with connection.cursor() as cursor:
            if connection.vendor == "mysql":
                cursor.execute("SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME LIKE 'ig_split_%'")
            else:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ig_split_%'")
            return {row[0] for row in cursor.fetchall()}

    def test_late_ddl_failure_replays_ordinary_migrate_without_fake_or_schema_loss(self):
        schema = import_module(MIGRATION)
        if connection.vendor not in {"mysql", "sqlite"}:
            self.skipTest("Split shipping supports native MariaDB and SQLite")
        recorder = MigrationRecorder(connection)
        expected_triggers = {f"ig_split_{short}_{event}" for short in schema.TABLES for event in ("insert", "update", "delete")}
        try:
            before_executor = MigrationExecutor(connection)
            before_executor.migrate([BEFORE])
            old_client = before_executor.loader.project_state([BEFORE]).apps.get_model("management", "IgClient")
            retained_client = old_client.objects.create(igsid="split-schema-retained-client")
            self.assertFalse(recorder.migration_qs.filter(app=TARGET[0], name=TARGET[1]).exists())
            executor = MigrationExecutor(connection)
            migration = executor.loader.graph.nodes[TARGET]
            last_operation = migration.operations[-1]
            self.assertIsInstance(last_operation, migrations.RunPython)
            original = last_operation.code

            retained = {}
            guard_count = {"created": 0}

            def interrupted_during_native_guards(apps, editor):
                # This is real manager-authored, signed evidence created once
                # all tables exist. The retry must retain its byte-for-byte
                # proof while repairing the unfinished trigger/index stage.
                offer = self.offer()
                retained.update(id=offer.pk, envelope=offer.envelope, hmac=offer.envelope_hmac)

                def interrupt_guard(execute, sql, params, many, context):
                    result = execute(sql, params, many, context)
                    if sql.startswith("CREATE TRIGGER ig_split_"):
                        guard_count["created"] += 1
                        if guard_count["created"] == 5:
                            raise RuntimeError("split-late-ddl-interruption")
                    return result

                with editor.connection.execute_wrapper(interrupt_guard):
                    original(apps, editor)

            with patch.object(last_operation, "code", interrupted_during_native_guards):
                with self.assertRaisesMessage(RuntimeError, "split-late-ddl-interruption"):
                    executor.migrate([TARGET])
            self.assertFalse(recorder.migration_qs.filter(app=TARGET[0], name=TARGET[1]).exists())
            self.assertTrue(set(schema.TABLES.values()).issubset(set(connection.introspection.table_names())))
            partial_triggers = self._triggers()
            self.assertEqual(len(partial_triggers), 5)
            self.assertTrue(partial_triggers < expected_triggers)
            with connection.cursor() as cursor:
                names = {column.name for column in connection.introspection.get_table_description(cursor,
                    "management_igcheckoutinventoryreservation")}
                constraints = connection.introspection.get_constraints(cursor, "management_igcheckoutinventoryreservation")
            self.assertIn("fulfilled_quantity", names)
            self.assertTrue(constraints["ig_res_fulfilled_lte_qty"]["check"])
            self.assertTrue(old_client.objects.filter(pk=retained_client.pk, igsid="split-schema-retained-client").exists())

            # A fresh executor sees only the recorded predecessor. Physical DDL
            # is reconciled by 0232 itself, with no fake migration or manual fix.
            MigrationExecutor(connection).migrate([TARGET])
            self.assertTrue(recorder.migration_qs.filter(app=TARGET[0], name=TARGET[1]).exists())
            self.assertEqual(self._triggers(), expected_triggers)
            self.assertTrue(old_client.objects.filter(pk=retained_client.pk, igsid="split-schema-retained-client").exists())
            retained_offer = shipping.Offer.objects.get(pk=retained["id"])
            self.assertEqual(retained_offer.envelope, retained["envelope"])
            self.assertEqual(retained_offer.envelope_hmac, retained["hmac"])
            self.assertTrue(shipping.verified_offer(retained_offer))
            final_apps = MigrationExecutor(connection).loader.project_state([TARGET]).apps
            with connection.cursor() as cursor:
                for model_name in ("IgSplitShippingOffer", "IgSplitShippingReceipt", "IgSplitShippingChoice",
                    "IgSplitOrderPlan", "IgSplitParcel", "IgSplitParcelDispatch", "IgSplitParcelTracking"):
                    model = final_apps.get_model("management", model_name)
                    physical = connection.introspection.get_constraints(cursor, model._meta.db_table)
                    for field in model._meta.local_fields:
                        shapes = [row for row in physical.values() if row.get("columns") == [field.column]]
                        if field.unique and not field.primary_key:
                            self.assertTrue(any(row.get("unique") for row in shapes), (model_name, field.name))
                        elif field.db_index and not field.unique:
                            self.assertTrue(any(row.get("index") or row.get("unique") for row in shapes), (model_name, field.name))
            with connection.schema_editor(atomic=False) as editor:
                clause = schema._check_clause(editor, "management_igcheckoutinventoryreservation", "ig_res_fulfilled_lte_qty")
            self.assertEqual(schema._normalized_check(clause), "fulfilled_quantity<=quantity")
            if connection.vendor == "mysql":
                with connection.cursor() as cursor:
                    for table in schema.TRANSACTION_TABLES:
                        cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
                        self.assertEqual(cursor.fetchone(), ("InnoDB",), table)
        finally:
            # Keep the disposable test DB at its normal current schema even
            # when the assertion pinpoints an interrupted-replay regression.
            MigrationExecutor(connection).migrate([TARGET])
