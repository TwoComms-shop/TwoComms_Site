"""Native three-customer progress and ownership with a held provider request.

Only the external model/token/HTTP boundaries are replaced. The finite waits
bound this disposable test, and do not specify a production response-time SLA.
SQLite deliberately skips these row-lock/concurrency assertions.
"""
from contextlib import contextmanager
from datetime import timedelta
import json
import threading
import time
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import (
    GeminiRequest, IgClient, IgCustomerTurn, IgCustomerTurnRevision,
    IgRevisionDeliveryEffect, IgTurnMessage, InstagramBotMessage,
)
from management.services.ig_customer_execution_pool import CustomerExecutionPool
from management.services.ig_turn_revisions import create_collecting_revision


@skipUnless(connection.vendor == "mysql", "requires disposable MariaDB row locks")
@override_settings(IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00", GOOGLE_INDEXING_ENABLED=False)
class LiveBurstFairnessMariaDbTests(TransactionTestCase):
    _message = fixtures.RevisionLiveTests._message

    def setUp(self):
        self.assertTrue(connection.mysql_is_mariadb)
        self.assertTrue(connection.features.has_select_for_update)
        fixtures.RevisionLiveTests.setUp(self)
        self.slow_started = threading.Event()
        self.release = threading.Event()
        self.capacity = 1
        self.lock = threading.Lock()
        self.events = []
        self.physical_connections = set()
        self.generated = []
        self.sends = []
        self.pools = []
        self.started_at = time.monotonic()
        self.rows = [(self.customer, self.revision, [self.source])]

    def _customer(self, ordinal, *, burst=False):
        client = IgClient.objects.create(igsid=f"1784000000000{ordinal}", language="uk")
        texts = (["Привіт", "Підкажіть, будь ласка", "Що ви можете порадити?"] if burst
            else ["Що ви можете порадити?"])
        sources = [InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            mid=f"burst-{ordinal}-{index}", role="user", source="webhook", text=text,
            provider_namespace="instagram_login:owner-1", status="pending")
            for index, text in enumerate(texts)]
        turn = IgCustomerTurn.objects.create(client=client, primary_source_message=sources[0],
            window_started_at=timezone.now(), window_deadline=timezone.now())
        for index, source in enumerate(sources):
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=index + 1, role="user")
        revision = create_collecting_revision(turn, sources, bypass_quiet=True).revision
        self.rows.append((client, revision, sources))
        # Stable FIFO eligibility across three real clients.
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(
            quiet_deadline=timezone.now() - timedelta(seconds=10 - ordinal))
        return client, revision, sources

    def _event(self, kind, client_id):
        with self.lock:
            self.events.append((kind, client_id, round(time.monotonic() - self.started_at, 4)))

    def _generate(self, payload, **kwargs):
        from management.services.ig_turn_lineage import current_context

        context = current_context()
        client = IgClient.objects.get(pk=context["client_id"])
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        for row_client, _revision, sources in self.rows:
            if row_client.pk == client.pk:
                for source in sources:
                    self.assertIn(source.text, serialized_payload)
        with connection.cursor() as cursor:
            cursor.execute("SELECT CONNECTION_ID()")
            physical_id = cursor.fetchone()[0]
        with self.lock:
            self.generated.append(client.pk)
            self.physical_connections.add(physical_id)
        self._event("generation_started", client.pk)
        if client.pk == self.customer.pk:
            self.slow_started.set()
            if not self.release.wait(20):
                raise RuntimeError("finite fake-provider release timeout")
        proxy = SimpleNamespace(**vars(self))
        proxy.customer = client
        proxy.generation_calls = []
        for name in ("assertFalse", "assertTrue", "assertEqual", "assertLessEqual", "assertNotIn", "assertIsNone"):
            setattr(proxy, name, getattr(self, name))
        try:
            result = fixtures.RevisionLiveTests._generate(proxy, payload, **kwargs)
        except Exception as exc:
            self._event("generation_error_" + type(exc).__name__, client.pk)
            print("NATIVE_BURST_GENERATOR_ERROR " + repr(exc))
            raise
        self._event("generation_returned", client.pk)
        return result

    def _send(self, _settings, _url, **kwargs):
        recipient = json.loads(kwargs["data"])["recipient"]["id"]
        client = IgClient.objects.get(igsid=recipient)
        with self.lock:
            self.sends.append(recipient)
        self._event("provider_receipt", client.pk)
        return 200, json.dumps({"message_id": f"burst-receipt-{recipient}"})

    def _pool(self):
        pool = CustomerExecutionPool(threading.Event(), capacity=lambda: self.capacity,
            maintenance=lambda: {"active": False})
        self.pools.append(pool)
        return pool

    @contextmanager
    def _providers(self):
        from management.services import ig_revision_live

        execute = ig_revision_live.execute_claimed_revision

        def observed_execute(*args, **kwargs):
            result = execute(*args, **kwargs)
            print("NATIVE_BURST_EXECUTION " + repr(result))
            return result

        with (patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate),
            patch("management.services.instagram_bot.get_page_token", return_value="fake-token"),
            patch("management.services.instagram_bot._provider_http", side_effect=self._send),
            patch.object(ig_revision_live, "execute_claimed_revision", side_effect=observed_execute)):
            try:
                yield
            finally:
                self.release.set()
                for pool in self.pools:
                    self.assertTrue(pool.drain(timeout=10), "test must release and own every physical future")

    def _until(self, pool, predicate, *, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.assertLessEqual(pool.tick()["in_flight"], 2)
            if predicate():
                return
            threading.Event().wait(0.01)
        self.fail(f"finite native progress bound exceeded: {self.events!r}")

    def _sent(self, revision):
        return IgRevisionDeliveryEffect.objects.filter(revision=revision, state="sent").exists()

    def _assert_receipts_and_trace(self):
        print("NATIVE_BURST_DIAGNOSTIC " + json.dumps({"case": self._testMethodName,
            "events": self.events, "revisions": list(IgCustomerTurnRevision.objects.filter(
                pk__in=[row[1].pk for row in self.rows]).values(
                    "pk", "client_id", "state", "recovery_state", "recovery_code",
                    "generation_proposal_digest"))}))
        for client, revision, sources in self.rows:
            revision.refresh_from_db()
            client.refresh_from_db()
            self.assertEqual(revision.state, "processed")
            self.assertEqual(client.automation_lease_token, "")
            self.assertEqual(revision.delivery_effects.filter(state="sent").count(), 1)
            self.assertEqual(revision.delivery_effects.get().provider_message_id,
                f"burst-receipt-{client.igsid}")
            self.assertFalse(InstagramBotMessage.objects.filter(
                pk__in=[source.pk for source in sources]).exclude(status="done").exists())
        self.assertEqual(len(self.sends), len(set(self.sends)))
        self.assertEqual(len(self.generated), len(set(self.generated)))
        self.assertEqual(GeminiRequest.objects.filter(client_id__in=self.generated).count(), len(self.rows))
        self.assertGreaterEqual(len(self.physical_connections), 2)
        print("NATIVE_BURST_TRACE " + json.dumps({"case": self._testMethodName,
            "clients": len(self.rows), "source_counts": [len(row[2]) for row in self.rows],
            "physical_connections": len(self.physical_connections), "events": self.events,
            "unique_receipts": len(self.sends)}))

    def test_two_healthy_customers_and_short_burst_progress_while_slow_slot_remains_owned(self):
        pool = self._pool()
        with self._providers():
            pool.tick()
            self.assertTrue(self.slow_started.wait(5))
            healthy = self._customer(2)
            burst = self._customer(3, burst=True)
            self.capacity = 2
            self._until(pool, lambda: self._sent(healthy[1]) and self._sent(burst[1]))
            self.assertFalse(self.release.is_set())
            self.assertFalse(self._sent(self.revision))
            self.customer.refresh_from_db()
            self.assertTrue(self.customer.automation_lease_token)
            self.release.set()
        self._assert_receipts_and_trace()

    def test_capacity_shrink_preserves_running_owner_then_restoration_admits_third_customer(self):
        pool = self._pool()
        with self._providers():
            pool.tick()
            self.assertTrue(self.slow_started.wait(5))
            healthy = self._customer(2)
            third = self._customer(3, burst=True)
            self.capacity = 2
            self._until(pool, lambda: self._sent(healthy[1]))
            self.capacity = 1
            # Reap the healthy future and prove admission stays bounded while
            # the already running request retains the remaining capacity.
            self._until(pool, lambda: pool.snapshot()["in_flight"] == 1)
            for _ in range(4):
                self.assertEqual(pool.tick()["in_flight"], 1)
            self.assertFalse(self._sent(third[1]))
            self.assertNotIn(third[0].pk, self.generated)
            self.capacity = 2
            self._until(pool, lambda: self._sent(third[1]))
            self.assertFalse(self.release.is_set())
            self.release.set()
        self._assert_receipts_and_trace()

    def test_timed_out_drain_retains_old_owner_while_replacement_pool_serves_other_clients(self):
        original = self._pool()
        with self._providers():
            original.tick()
            self.assertTrue(self.slow_started.wait(5))
            self.assertFalse(original.drain(timeout=0.01))
            self.assertEqual(original.snapshot()["in_flight"], 1)
            self.assertTrue(original.snapshot()["closing"])
            healthy = self._customer(2)
            burst = self._customer(3, burst=True)
            replacement = self._pool()
            self.capacity = 2
            self._until(replacement, lambda: self._sent(healthy[1]) and self._sent(burst[1]))
            self.assertEqual(self.generated.count(self.customer.pk), 1)
            self.assertFalse(self._sent(self.revision))
            self.assertFalse(original.closed)
            self.release.set()
        self._assert_receipts_and_trace()
