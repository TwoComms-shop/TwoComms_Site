"""Disposable MariaDB proof for conversion rewards and batch capacity.

These tests deliberately skip SQLite: successful sequential transactions there
cannot prove that independent database connections serialize money writes.
"""

from importlib import import_module
from threading import Event, Thread
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.test import RequestFactory

from accounts.models import PointsHistory, UserPoints
from management.models import IgAdMetadataRecord
from management.tests_ig_mariadb_follow_ugc import _MariaDbConcurrencyCase
from orders.promo_reservations import PromoReservationError, reserve_promos_for_checkout
from storefront.models import ProductFitOption, PromoCode, UserPromoCode
from storefront.services.loyalty_rewards import LoyaltyRewardError, redeem_points_reward
from storefront.views.profile import purchase_with_points


@skipUnless(connection.vendor == "mysql", "requires disposable MariaDB row locks")
class ConversionMoneyMariaDbTests(_MariaDbConcurrencyCase):
    cleanup_tables = (
        "storefront_userpromocode", "accounts_pointshistory", "accounts_userpoints",
        "accounts_userprofile", "management_igadmetadatarecord",
    ) + _MariaDbConcurrencyCase.cleanup_tables

    def setUp(self):
        self.assertTrue(connection.mysql_is_mariadb)
        self.assertTrue(connection.features.has_select_for_update)
        self.user = get_user_model().objects.create_user(username="mariadb-conversion-money")
        self.balance, _ = UserPoints.objects.update_or_create(
            user=self.user, defaults={"points": 100, "total_earned": 100},
        )

    def _redeem(self, operation):
        try:
            return ("issued", redeem_points_reward(user=self.user, operation_id=operation).pk)
        except LoyaltyRewardError as exc:
            return (str(exc), None)

    def _assert_single_debit(self):
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.points, self.balance.total_spent), (0, 100))
        self.assertEqual(list(PointsHistory.objects.filter(user=self.user).values_list(
            "points_change", "balance_after", "change_type",
        )), [(-100, 0, "spent")])

    def test_native_migrations_engines_and_identity_uniqueness(self):
        applied = MigrationRecorder(connection).applied_migrations()
        self.assertIn(("management", "0237_ad_context_observations"), applied)
        self.assertIn(("management", "0238_transactional_points_grants"), applied)
        self.assertIn(("management", "0240_current_hoodie_fit_storage"), applied)
        for model in (UserPoints, PointsHistory, UserPromoCode, PromoCode, IgAdMetadataRecord, ProductFitOption):
            with connection.cursor() as cursor:
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [model._meta.db_table])
                self.assertEqual(cursor.fetchone(), ("InnoDB",))
        IgAdMetadataRecord.objects.create(namespace="conversion-gate", ad_id="same-ad")
        with self.assertRaises(IntegrityError), transaction.atomic():
            IgAdMetadataRecord.objects.create(namespace="conversion-gate", ad_id="same-ad")
        grant = redeem_points_reward(user=self.user, operation_id="identity")
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserPromoCode.objects.create(user=self.user, promo_code=grant.promo_code,
                survey_key=grant.survey_key, source="points_reward")

    def test_0240_preserves_legacy_generated_fit_identity_rows_and_atomic_rollback(self):
        # A private BASE TABLE is visible to information_schema, unlike SQL
        # TEMPORARY tables. Never rehearse this DDL on an application table.
        self.assertRegex(str(connection.settings_dict["NAME"]), r"^test_twocomms_ig_[a-f0-9]{12}$")
        table = f"conversion_fit_storage_{uuid4().hex[:12]}"
        quoted = connection.ops.quote_name(table)
        created = False

        def rows():
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT id,product_id,code,is_default,label,default_product_identity "
                               f"FROM {quoted} ORDER BY id")
                return cursor.fetchall()

        def physical_identity():
            with connection.cursor() as cursor:
                cursor.execute("SELECT DATA_TYPE,EXTRA,GENERATION_EXPRESSION "
                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME=%s AND COLUMN_NAME='default_product_identity'", [table])
                generated = cursor.fetchone()
                cursor.execute(f"SHOW INDEX FROM {quoted}")
                indexes = sorted((row[1], row[2], row[3], row[4], row[10]) for row in cursor.fetchall())
                return generated, indexes

        def engine():
            with connection.cursor() as cursor:
                cursor.execute("SELECT ENGINE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", [table])
                return cursor.fetchone()[0]

        historical_model = SimpleNamespace(_meta=SimpleNamespace(db_table=table))

        def get_historical_model(app, name):
            self.assertEqual((app, name), ("storefront", "ProductFitOption"))
            return historical_model

        migration = import_module("management.migrations.0240_current_hoodie_fit_storage")
        historical_apps = SimpleNamespace(get_model=get_historical_model)
        schema_editor = SimpleNamespace(connection=connection)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE TABLE {quoted} (id bigint NOT NULL PRIMARY KEY, "
                    "product_id bigint NOT NULL, code varchar(32) NOT NULL, "
                    "is_default tinyint(1) NOT NULL, label varchar(140) NOT NULL, "
                    "default_product_identity bigint GENERATED ALWAYS AS "
                    "(CASE WHEN is_default=1 THEN product_id ELSE NULL END) STORED, "
                    "UNIQUE KEY fit_product_code (product_id,code), "
                    "UNIQUE KEY fit_default_identity (default_product_identity)) "
                    "ENGINE=MyISAM DEFAULT CHARSET=utf8mb4")
                created = True
                cursor.executemany(f"INSERT INTO {quoted} (id,product_id,code,is_default,label) "
                    "VALUES (%s,%s,%s,%s,%s)", [
                        (2 * product - int(default), product, code, int(default), f"Original {product} {code}")
                        for product in range(1, 36)
                        for code, default in (("regular", True), ("oversize", False))
                    ])
            self.assertEqual(engine(), "MyISAM")
            original_rows, original_identity = rows(), physical_identity()
            self.assertEqual(len(original_rows), 70)
            self.assertIn("STORED GENERATED", original_identity[0][1].upper())

            migration.ensure_transactional_fit_flags(historical_apps, schema_editor)
            self.assertEqual(engine(), "InnoDB")
            self.assertEqual(rows(), original_rows)
            self.assertEqual(physical_identity(), original_identity)
            migration.ensure_transactional_fit_flags(historical_apps, schema_editor)
            self.assertEqual(rows(), original_rows)
            self.assertEqual(physical_identity(), original_identity)

            with self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"INSERT INTO {quoted} "
                        "(id,product_id,code,is_default,label) VALUES (999,1,'duplicate',1,'Duplicate')")
            with self.assertRaisesRegex(RuntimeError, "rollback changed fit flags"):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(f"UPDATE {quoted} SET is_default=0 WHERE product_id=1")
                        cursor.execute(f"UPDATE {quoted} SET is_default=1 "
                                       "WHERE product_id=1 AND code='oversize'")
                    self.assertNotEqual(rows(), original_rows)
                    raise RuntimeError("rollback changed fit flags")
            self.assertEqual(rows(), original_rows)
            self.assertEqual(engine(), "InnoDB")
        finally:
            if created:
                with connection.cursor() as cursor:
                    cursor.execute(f"DROP TABLE {quoted}")

    def test_simultaneous_replay_issues_one_grant_and_one_debit(self):
        results = self._run_workers(lambda: self._redeem("same-operation"),
                                    lambda: self._redeem("same-operation"))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], "issued")
        self.assertEqual(UserPromoCode.objects.filter(user=self.user).count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        self._assert_single_debit()

    def test_redemption_waits_for_balance_row_lock_from_independent_connection(self):
        entered, finished = Event(), Event()
        results, errors = [], []

        def redeem():
            close_old_connections()
            try:
                entered.set()
                results.append(self._redeem("held-balance"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
                close_old_connections()

        worker = Thread(target=redeem, daemon=True)
        with transaction.atomic():
            UserPoints.objects.select_for_update().get(pk=self.balance.pk)
            worker.start()
            self.assertTrue(entered.wait(timeout=10))
            self.assertFalse(finished.wait(timeout=0.25))
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0][0], "issued")
        self._assert_single_debit()

    def test_distinct_simultaneous_operations_cannot_spend_the_same_points(self):
        results = self._run_workers(lambda: self._redeem("operation-a"),
                                    lambda: self._redeem("operation-b"))
        self.assertCountEqual([row[0] for row in results], ["issued", "insufficient_points"])
        self.assertEqual(UserPromoCode.objects.filter(user=self.user).count(), 1)
        self._assert_single_debit()

    def test_donation_and_redemption_share_one_serialized_balance(self):
        def donate():
            request = RequestFactory().post("/points/purchase/", {"item_id": "donate_zsu"})
            request.user, request.session = self.user, {}
            request._messages = FallbackStorage(request)
            return purchase_with_points(request).status_code

        results = self._run_workers(lambda: self._redeem("against-donation"), donate)
        self.assertEqual(results[1], 302)
        self.assertIn(results[0][0], {"issued", "insufficient_points"})
        self.assertEqual(UserPromoCode.objects.filter(user=self.user).count(),
                         int(results[0][0] == "issued"))
        self._assert_single_debit()

    def test_history_failure_rolls_back_real_innodb_grant_and_debit(self):
        with patch.object(PointsHistory.objects, "create", side_effect=RuntimeError("history failure")):
            with self.assertRaisesRegex(RuntimeError, "history failure"):
                redeem_points_reward(user=self.user, operation_id="failed-history")
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.points, self.balance.total_spent), (100, 0))
        self.assertFalse(UserPromoCode.objects.exists())
        self.assertFalse(PromoCode.objects.exists())
        self.assertFalse(PointsHistory.objects.exists())

    def test_overlapping_batches_reserve_one_capacity_and_roll_back_losing_batch(self):
        # Create private components before the shared code so a losing batch
        # has already reserved its private component when shared capacity fails.
        private = [PromoCode.objects.create(code=f"MARIADBPRIVATE{i}", discount_value=10,
            discount_type="percentage", max_uses=1) for i in range(2)]
        shared = PromoCode.objects.create(code="MARIADBSHARED", discount_value=10,
            discount_type="percentage", max_uses=1)

        def reserve(index):
            try:
                batch = reserve_promos_for_checkout(codes=[shared.code, private[index].code],
                    user=self.user, total_amount=1000)
                return ("reserved", str(batch.discount))
            except PromoReservationError:
                return ("unavailable", None)

        results = self._run_workers(lambda: reserve(0), lambda: reserve(1))
        self.assertCountEqual([row[0] for row in results], ["reserved", "unavailable"])
        self.assertEqual(next(row[1] for row in results if row[0] == "reserved"), "190.00")
        shared.refresh_from_db()
        self.assertEqual(shared.current_uses, 1)
        for index, promo in enumerate(private):
            promo.refresh_from_db()
            self.assertEqual(promo.current_uses, int(results[index][0] == "reserved"))
