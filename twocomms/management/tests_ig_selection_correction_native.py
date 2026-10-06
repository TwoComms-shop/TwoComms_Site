"""Separate-connection MariaDB contention; SQLite cannot prove these locks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import time
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_selection_corrections as fixtures
from management.models import IgClient, IgCommerceSelectionSession, IgCommerceSelectionTransition
from management.services.ig_reply_boundary import pause_reply_boundary
from management.services.ig_selection_corrections import SizeCorrectionRejected, save_size_correction


@skipUnless(connection.vendor == "mysql" and connection.features.has_select_for_update,
            "requires isolated native MariaDB row locks")
@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_GENERATION_ENABLED=False,
                   IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=False)
class NativeSizeCorrectionContentionTests(TransactionTestCase):
    setUp = fixtures.SizeCorrectionTests.setUp
    message = fixtures.SizeCorrectionTests.message
    capture = fixtures.SizeCorrectionTests.capture
    context = fixtures.SizeCorrectionTests.context
    request = fixtures.SizeCorrectionTests.request

    @contextmanager
    def isolated_boundary(self):
        # Keep the real file-lock implementation, with a private path so this
        # test cannot contend with unrelated daemon/browser fixtures.
        with TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / "correction-edge.lock")
            def boundary(**kwargs):
                return pause_reply_boundary(lock_path=lock_path, **kwargs)
            with patch("management.services.ig_reply_boundary.pause_reply_boundary", side_effect=boundary):
                yield

    def worker(self, request, *, start=None, row_lock_attempt=None):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                connection_id = cursor.fetchone()[0]
            def observe(execute, sql, params, many, context):
                if row_lock_attempt is not None and "FOR UPDATE" in sql.upper() and IgClient._meta.db_table in sql:
                    row_lock_attempt.set()
                return execute(sql, params, many, context)
            if start is not None:
                start.wait(timeout=5)
            deadline = time.monotonic() + 3
            with connection.execute_wrapper(observe):
                while True:
                    try:
                        result = save_size_correction(self.row.pk, **request)
                        return connection_id, result
                    except SizeCorrectionRejected as exc:
                        # A real short file barrier may time out before the
                        # winner releases it. Retry the same operation only;
                        # all other denials are returned without reinterpretation.
                        if exc.code != "correction_boundary_busy" or time.monotonic() >= deadline:
                            return connection_id, exc
                        time.sleep(.01)
        finally:
            connections["default"].close()

    def race_after_real_client_lock(self, requests):
        attempted = Event()
        start = Barrier(2)
        with self.isolated_boundary(), ThreadPoolExecutor(max_workers=2) as workers:
            with transaction.atomic():
                IgClient.objects.select_for_update().get(pk=self.row.pk)
                futures = [workers.submit(self.worker, request, start=start, row_lock_attempt=attempted)
                           for request in requests]
                self.assertTrue(attempted.wait(5), "correction did not attempt the real client row lock")
                self.assertFalse(any(future.done() for future in futures), "client lock did not block the writers")
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(len({identity for identity, _ in results}), 2)
        return [result for _, result in results]

    def test_distinct_operations_same_context_append_once_and_stale_loser_is_409(self):
        context = self.context()
        before_client = IgClient.objects.values().get(pk=self.row.pk)
        before_count = IgCommerceSelectionTransition.objects.count()
        requests = [self.request(value=size, context=context) for size in ("L", "XL")]
        results = self.race_after_real_client_lock(requests)
        accepted = [result for result in results if not isinstance(result, SizeCorrectionRejected)]
        rejected = [result for result in results if isinstance(result, SizeCorrectionRejected)]
        self.assertEqual(len(accepted), 1, results)
        self.assertEqual(accepted[0].status, "applied")
        self.assertEqual(len(rejected), 1, results)
        self.assertEqual(rejected[0].status, 409)
        self.assertIn(rejected[0].code, ("correction_state_unavailable", "correction_context_conflict", "correction_selection_conflict"))
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), before_count + 1)
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(action="manager_size_correction").count(), 1)
        session = IgCommerceSelectionSession.objects.get(pk=self.session.pk)
        self.assertEqual(session.revision, context["context"]["selection_revision"] + 1)
        self.assertIn(session.lines[session.active_index]["size"], ("L", "XL"))
        self.assertEqual(IgClient.objects.values().get(pk=self.row.pk), before_client)
        self.source.refresh_from_db()
        self.assertEqual(self.source.text, "Хочу футболку розмір M")

    def test_identical_operation_concurrent_replay_has_one_receipt_and_transition(self):
        request = self.request()
        before_count = IgCommerceSelectionTransition.objects.count()
        results = self.race_after_real_client_lock([request, dict(request)])
        self.assertFalse(any(isinstance(result, SizeCorrectionRejected) for result in results), results)
        self.assertEqual(sorted(result.status for result in results), ["applied", "replayed"])
        self.assertEqual(len({result.transition_id for result in results}), 1)
        self.assertEqual(len({result.operation_id for result in results}), 1)
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), before_count + 1)
        event = IgCommerceSelectionTransition.objects.get(correction_operation_id=request["operation_id"])
        self.assertEqual(event.effects["manager_correction"]["operation_id"], str(request["operation_id"]))
        self.assertEqual(event.effects["manager_correction"]["before"], "M")
        self.assertEqual(event.effects["manager_correction"]["after"], "L")

    def test_erasure_committed_while_correction_waits_for_client_lock_blocks_append(self):
        request = self.request()
        attempted = Event()
        before_count = IgCommerceSelectionTransition.objects.count()
        before_revision = self.session.revision
        before_legacy = IgClient.objects.filter(pk=self.row.pk).values(
            "current_product_id", "current_size", "current_qty", "current_color").get()
        before_source = type(self.source).objects.values().get(pk=self.source.pk)
        with self.isolated_boundary(), ThreadPoolExecutor(max_workers=1) as workers:
            with transaction.atomic():
                IgClient.objects.select_for_update().get(pk=self.row.pk)
                future = workers.submit(self.worker, request, row_lock_attempt=attempted)
                self.assertTrue(attempted.wait(5), "correction did not reach the real row-lock wait")
                self.assertFalse(future.done())
                IgClient.objects.filter(pk=self.row.pk).update(privacy_erasure_started_at=timezone.now())
            _, result = future.result(timeout=10)
        self.assertIsInstance(result, SizeCorrectionRejected)
        self.assertEqual((result.code, result.status), ("client_unavailable", 410))
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), before_count)
        self.assertEqual(IgCommerceSelectionSession.objects.get(pk=self.session.pk).revision, before_revision)
        after_legacy = IgClient.objects.filter(pk=self.row.pk).values(*before_legacy).get()
        self.assertEqual(after_legacy, before_legacy)
        self.assertEqual(type(self.source).objects.values().get(pk=self.source.pk), before_source)
