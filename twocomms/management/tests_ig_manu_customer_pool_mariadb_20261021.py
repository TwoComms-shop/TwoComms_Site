"""Disposable InnoDB gate: actual consumer leases, graph and SENT receipts."""
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
class CustomerPoolMariaDbTests(TransactionTestCase):
    reset_sequences = False
    _message = fixtures.RevisionLiveTests._message

    def setUp(self):
        self.assertTrue(connection.mysql_is_mariadb)
        self.assertTrue(connection.features.has_select_for_update)
        fixtures.RevisionLiveTests.setUp(self)

    def _second_customer(self):
        client = IgClient.objects.create(igsid="17840000000002", language="uk")
        source = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            mid="pool-second", role="user", source="webhook", text="Що ви можете порадити?",
            provider_namespace="instagram_login:owner-1", status="pending")
        turn = IgCustomerTurn.objects.create(client=client, primary_source_message=source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        return client, create_collecting_revision(turn, [source], bypass_quiet=True).revision

    def _exercise(self, *, second):
        from management.services.ig_turn_lineage import current_context

        slow_started, release = threading.Event(), threading.Event()
        capacity = [1 if second else 2]
        generated, sends, physical_connections = [], [], set()
        lock = threading.Lock()
        slow_id = self.customer.pk
        healthy, healthy_revision = self._second_customer() if second else (None, None)
        from management.services import instagram_bot as bot
        acquire_lease = bot.acquire_client_automation_lease
        competing = threading.Barrier(2)
        lease_attempts = []

        def acquire(client_id):
            if not second:
                competing.wait(timeout=5)
            result = acquire_lease(client_id)
            with lock:
                lease_attempts.append((client_id, bool(result[1])))
            return result

        def generate(payload, **kwargs):
            context = current_context()
            client = IgClient.objects.get(pk=context["client_id"])
            with connection.cursor() as cursor:
                cursor.execute("SELECT CONNECTION_ID()")
                physical_id = cursor.fetchone()[0]
            with lock:
                generated.append(client.pk)
                physical_connections.add(physical_id)
            if client.pk == slow_id:
                slow_started.set()
                if not release.wait(10):
                    raise RuntimeError("fake provider release timeout")
            # Each job's validator fixture owns its customer and payload list;
            # no shared mutable fixture state or Django connection is reused.
            proxy = SimpleNamespace(**vars(self))
            proxy.customer = client
            proxy.generation_calls = []
            for name in ("assertFalse", "assertTrue", "assertEqual", "assertLessEqual", "assertNotIn", "assertIsNone"):
                setattr(proxy, name, getattr(self, name))
            return fixtures.RevisionLiveTests._generate(proxy, payload, **kwargs)

        def send(_settings, _url, **kwargs):
            recipient = json.loads(kwargs["data"])["recipient"]["id"]
            with lock:
                sends.append(recipient)
            return 200, json.dumps({"message_id": f"pool-sent-{recipient}"})

        pool = CustomerExecutionPool(threading.Event(), capacity=lambda: capacity[0],
            maintenance=lambda: {"active": False})
        try:
            with (patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=generate),
                patch("management.services.instagram_bot.acquire_client_automation_lease", side_effect=acquire),
                patch("management.services.instagram_bot.get_page_token", return_value="fake"),
                patch("management.services.instagram_bot._provider_http", side_effect=send),
                patch("management.services.instagram_bot._register_outgoing_message")):
                pool.tick()
                self.assertTrue(slow_started.wait(5))
                self.customer.refresh_from_db()
                self.assertTrue(self.customer.automation_lease_token)
                capacity[0] = 2
                if second:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        pool.tick()
                        if IgRevisionDeliveryEffect.objects.filter(revision=healthy_revision, state="sent").exists():
                            break
                        threading.Event().wait(0.01)
                    self.assertTrue(IgRevisionDeliveryEffect.objects.filter(
                        revision=healthy_revision, state="sent", provider_message_id=f"pool-sent-{healthy.igsid}").exists())
                    self.assertFalse(release.is_set())
                    self.assertEqual(len(physical_connections), 2)
                else:
                    deadline = time.monotonic() + 5
                    while len(lease_attempts) < 2 and time.monotonic() < deadline:
                        threading.Event().wait(0.01)
                    self.assertEqual(sorted(success for _, success in lease_attempts), [False, True])
                    self.assertEqual(generated, [slow_id])
                self.assertFalse(pool.drain(timeout=0.01))
                release.set()
                self.assertTrue(pool.drain(timeout=5))
        finally:
            release.set()
            self.assertTrue(pool.drain(timeout=10))
        expected = [self.revision.pk] + ([healthy_revision.pk] if second else [])
        self.assertEqual(IgRevisionDeliveryEffect.objects.filter(revision_id__in=expected, state="sent").count(), len(expected))
        self.assertEqual(GeminiRequest.objects.filter(client_id__in=generated).count(), len(expected))
        self.assertEqual(len(sends), len(set(sends)))
        self.assertEqual(IgCustomerTurnRevision.objects.filter(pk__in=expected, state="processed").count(), len(expected))
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.automation_lease_token)

    def test_slow_customer_does_not_block_other_native_client_receipt(self):
        self._exercise(second=True)

    def test_competing_slots_cannot_duplicate_same_client_generation_or_send(self):
        self._exercise(second=False)
