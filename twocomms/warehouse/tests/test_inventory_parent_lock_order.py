"""Exercise the real write-off transaction against shared stock parent locks."""
from decimal import Decimal
from queue import Queue
from threading import Barrier, Event, Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import close_old_connections, connection, connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings

from orders.models import Order, OrderItem
from productcolors.models import Color
from warehouse.models import StockItem, StorageCategory, StorageSubcategory, WriteOffRequest
from warehouse.services.inventory import adjust_stock_item
from warehouse.views.write_off import write_off_submit


class _WriteOffFixture:
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(
            username="parent_lock_admin", is_staff=True, is_superuser=True,
        )
        color = Color.objects.create(name="Parent lock black", primary_hex="#000000")
        category = StorageCategory.objects.create(name="Parent locks", slug="parent-locks")
        subcategory = StorageSubcategory.objects.create(category=category, name="Classic")
        self.low = StockItem.objects.create(subcategory=subcategory, color=color, size="M", quantity=5)
        self.high = StockItem.objects.create(subcategory=subcategory, color=color, size="L", quantity=5)
        self.order = Order.objects.create(full_name="Parent lock customer", phone="+380500000000")
        # Deliberately oppose the global numeric parent order.
        self.high_item = OrderItem.objects.create(
            order=self.order, title="High parent", size="L", qty=1, is_custom=True,
            unit_price=Decimal("100"), line_total=Decimal("100"),
        )
        self.low_item = OrderItem.objects.create(
            order=self.order, title="Low parent", size="M", qty=1, is_custom=True,
            unit_price=Decimal("100"), line_total=Decimal("100"),
        )
        self.writeoff = WriteOffRequest.objects.create(order=self.order)

    def _request(self, *, low_qty=1):
        request = RequestFactory().post("/write-off/submit/", {
            f"item_{self.high_item.pk}_stock_id": str(self.high.pk),
            f"item_{self.high_item.pk}_qty": "1",
            f"item_{self.low_item.pk}_stock_id": str(self.low.pk),
            f"item_{self.low_item.pk}_qty": str(low_qty),
        })
        request.user = self.user
        request._messages = CookieStorage(request)
        return request


@override_settings(ROOT_URLCONF="twocomms.urls_storage")
class WriteOffParentOrderTests(_WriteOffFixture, TestCase):
    def test_both_parents_are_selected_in_numeric_order_before_first_deduction(self):
        selected = []
        before_adjustment = []

        def trace(execute, sql, params, many, context):
            if sql.lstrip().upper().startswith("SELECT") and "warehouse_stockitem" in sql.lower():
                if params and params[0] in {self.low.pk, self.high.pk}:
                    selected.append(params[0])
            return execute(sql, params, many, context)

        def adjust(**kwargs):
            before_adjustment.append(tuple(selected))
            return adjust_stock_item(**kwargs)

        with connection.execute_wrapper(trace), patch(
            "warehouse.views.write_off.adjust_stock_item", side_effect=adjust,
        ), patch("warehouse.views.write_off._notify_order_writeoff"):
            response = write_off_submit(self._request(), self.writeoff.token)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(before_adjustment[0], (self.low.pk, self.high.pk))
        self.assertEqual(len(before_adjustment), 2)
        self.low.refresh_from_db()
        self.high.refresh_from_db()
        self.writeoff.refresh_from_db()
        self.assertEqual((self.low.quantity, self.high.quantity), (4, 4))
        self.assertEqual(self.writeoff.status, WriteOffRequest.STATUS_COMPLETED)
        self.assertEqual(self.writeoff.movements.count(), 2)

    def test_shortfall_after_prelocks_rolls_back_all_deductions(self):
        with patch("warehouse.views.write_off._notify_order_writeoff") as notify:
            response = write_off_submit(self._request(low_qty=6), self.writeoff.token)
        self.assertEqual(response.status_code, 302)
        self.low.refresh_from_db()
        self.high.refresh_from_db()
        self.writeoff.refresh_from_db()
        self.assertEqual((self.low.quantity, self.high.quantity), (5, 5))
        self.assertEqual(self.writeoff.status, WriteOffRequest.STATUS_PENDING)
        self.assertFalse(self.writeoff.movements.exists())
        notify.assert_not_called()


@override_settings(ROOT_URLCONF="twocomms.urls_storage")
class MariaDBWriteOffParentConcurrencyTests(_WriteOffFixture, TransactionTestCase):
    def setUp(self):
        if connection.vendor != "mysql" or not connection.mysql_is_mariadb:
            self.skipTest("Two physical MariaDB connections required")
        if not str(connection.settings_dict.get("NAME") or "").startswith("test_twocomms_"):
            self.fail("Concurrency acceptance requires a disposable test_twocomms_* database")
        super().setUp()

    def test_opposite_posted_order_completes_with_ascending_shared_pool_holder(self):
        start = Barrier(2, timeout=10)
        first_parent_attempt = Event()
        outcomes = Queue()
        database_ids = Queue()
        first_parent_ids = Queue()
        low_id, high_id = self.low.pk, self.high.pk
        request = self._request()
        token = self.writeoff.token

        def initialize_connection():
            close_old_connections()
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout=5")
                cursor.execute("SELECT CONNECTION_ID()")
                database_ids.put(cursor.fetchone()[0])

        def ascending_pool_holder():
            try:
                initialize_connection()
                with transaction.atomic():
                    # These are the exact shared StockItem parents locked by
                    # reservation/recount writers, without any fake stock grant.
                    StockItem.objects.select_for_update().get(pk=low_id)
                    start.wait()
                    if not first_parent_attempt.wait(10):
                        raise AssertionError("Write-off never reached its first stock parent")
                    StockItem.objects.select_for_update().get(pk=high_id)
                outcomes.put(("holder", None))
            except BaseException as exc:
                first_parent_attempt.set()
                start.abort()
                outcomes.put(("holder", exc))
            finally:
                connections.close_all()

        def submit():
            observed = False

            def synchronize(execute, sql, params, many, context):
                nonlocal observed
                stock_lock = "warehouse_stockitem" in sql.lower() and "FOR UPDATE" in sql.upper()
                if not observed and stock_lock:
                    observed = True
                    parent_id = params[0]
                    first_parent_ids.put(parent_id)
                    if parent_id == low_id:
                        # Correct code waits on the holder's low parent. Let
                        # that holder acquire high and commit before unblocking.
                        first_parent_attempt.set()
                        return execute(sql, params, many, context)
                    # Regression code acquires high first. Signal only AFTER
                    # acquiring it, forcing high->low against low->high.
                    result = execute(sql, params, many, context)
                    first_parent_attempt.set()
                    return result
                return execute(sql, params, many, context)

            try:
                initialize_connection()
                start.wait()
                with connection.execute_wrapper(synchronize):
                    response = write_off_submit(request, token)
                outcomes.put(("submit", response.status_code))
            except BaseException as exc:
                first_parent_attempt.set()
                start.abort()
                outcomes.put(("submit", exc))
            finally:
                connections.close_all()

        workers = [Thread(target=ascending_pool_holder, daemon=True), Thread(target=submit, daemon=True)]
        with patch("warehouse.views.write_off._notify_order_writeoff"):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=20)
        self.assertFalse(any(worker.is_alive() for worker in workers), "Physical lock acceptance timed out")
        self.assertEqual(database_ids.qsize(), 2)
        self.assertEqual(len({database_ids.get_nowait(), database_ids.get_nowait()}), 2)
        self.assertEqual(first_parent_ids.get_nowait(), low_id)
        results = dict(outcomes.get_nowait() for _ in range(2))
        self.assertIsNone(results["holder"], repr(results))
        self.assertEqual(results["submit"], 302, repr(results))
        self.low.refresh_from_db()
        self.high.refresh_from_db()
        self.writeoff.refresh_from_db()
        self.assertEqual((self.low.quantity, self.high.quantity), (4, 4))
        self.assertEqual(self.writeoff.status, WriteOffRequest.STATUS_COMPLETED)
        self.assertEqual(self.writeoff.movements.count(), 2)
