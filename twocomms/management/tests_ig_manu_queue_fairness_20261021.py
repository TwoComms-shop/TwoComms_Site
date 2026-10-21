"""Characterize cross-client serial queue latency through native execution.

Only provider boundaries and unrelated follow-up work are replaced. These are
offline characterization tests, not a claim that SQLite proves MariaDB locking
or that the worker currently guarantees a cross-client response-time SLA.
"""
from contextlib import ExitStack
from datetime import timedelta
import json
import threading
import time
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management.management.commands import run_instagram_bot as runner
from management.models import IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgTurnMessage, InstagramBotMessage
from management.services import instagram_bot as bot
from management.services.ig_revision_live import process_pending_revisions
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2020-01-01T00:00:00+00:00")
class ManuQueueFairnessCharacterizationTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message

    SLOW_SECONDS = 0.06

    def two_clients(self):
        self.slow_customer, self.slow_revision, self.slow_source = self.customer, self.revision, self.source
        self.healthy_customer = IgClient.objects.create(igsid="manu-fairness-healthy", language="uk")
        self.customer = self.healthy_customer
        self.healthy_source = self._message("Що ви можете порадити?", "fairness-healthy")
        turn = IgCustomerTurn.objects.create(client=self.healthy_customer, primary_source_message=self.healthy_source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=self.healthy_source, ordinal=1, role="user")
        self.healthy_revision = create_collecting_revision(turn, [self.healthy_source], bypass_quiet=True).revision
        self.customer = self.slow_customer
        now = timezone.now()
        IgCustomerTurnRevision.objects.filter(pk=self.slow_revision.pk).update(quiet_deadline=now - timedelta(seconds=2))
        IgCustomerTurnRevision.objects.filter(pk=self.healthy_revision.pk).update(quiet_deadline=now - timedelta(seconds=1))
        self.events = []
        self.probes = []
        self.validation_threads = []

    def fake_provider(self, payload, **kwargs):
        from management.services.ig_turn_lineage import current_context

        context = current_context()
        customer_id = context["client_id"]
        self.customer = IgClient.objects.get(pk=customer_id)
        label = "slow" if customer_id == self.slow_customer.pk else "healthy"
        self.events.append((label + ":provider-start", time.monotonic()))
        self.validation_threads.append(threading.get_ident())
        if label == "slow":
            healthy = IgClient.objects.get(pk=self.healthy_customer.pk)
            revision = IgCustomerTurnRevision.objects.get(pk=self.healthy_revision.pk)
            source = InstagramBotMessage.objects.get(pk=self.healthy_source.pk)
            self.probes.append({"healthy_lease": healthy.automation_lease_token,
                "healthy_state": revision.state, "healthy_source": source.status,
                "in_atomic": connection.in_atomic_block,
                "healthy_started": any(name == "healthy:provider-start" for name, _ in self.events)})
            time.sleep(self.SLOW_SECONDS)
        result = live_fixtures.RevisionLiveTests._generate(self, payload, **kwargs)
        self.events.append((label + ":provider-return", time.monotonic()))
        return result

    def fake_send(self, _settings, _url, **kwargs):
        payload = json.loads(kwargs["data"])
        label = "slow" if payload["recipient"]["id"] == self.slow_customer.igsid else "healthy"
        self.events.append((label + ":sent", time.monotonic()))
        return 200, json.dumps({"message_id": "fairness-receipt-" + label})

    def patched_provider(self, stack):
        stack.enter_context(patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.fake_provider))
        stack.enter_context(patch.object(bot, "get_page_token", return_value="offline-token"))
        stack.enter_context(patch.object(bot, "_provider_http", side_effect=self.fake_send))
        stack.enter_context(patch.object(bot, "_register_outgoing_message"))
        stack.enter_context(patch.object(bot, "maintenance_status", return_value={"active": False}))

    def assert_serial_delivery(self):
        names = [name for name, _ in self.events]
        self.assertEqual(names, ["slow:provider-start", "slow:provider-return", "slow:sent",
            "healthy:provider-start", "healthy:provider-return", "healthy:sent"])
        times = dict(self.events)
        self.assertGreaterEqual(times["healthy:provider-start"] - times["slow:provider-start"], self.SLOW_SECONDS)
        self.assertEqual(self.probes, [{"healthy_lease": "", "healthy_state": "collecting",
            "healthy_source": "pending", "in_atomic": False, "healthy_started": False}])
        self.assertEqual(set(self.validation_threads), {threading.get_ident()})
        for revision, customer in ((self.slow_revision, self.slow_customer), (self.healthy_revision, self.healthy_customer)):
            revision.refresh_from_db()
            customer.refresh_from_db()
            self.assertEqual(revision.state, "processed")
            effect = revision.delivery_effects.get()
            self.assertEqual(effect.state, "sent")
            self.assertTrue(effect.provider_message_id.startswith("fairness-receipt-"))
            self.assertEqual(customer.automation_lease_token, "")

    def test_native_queue_healthy_client_waits_for_slow_client_despite_available_lease(self):
        self.two_clients()
        with ExitStack() as stack:
            self.patched_provider(stack)
            handled = process_pending_revisions(self.settings, max_items=2)
        self.assertEqual(handled, 2)
        self.assert_serial_delivery()

    def test_other_owner_client_lease_is_skipped_without_blocking_healthy_client(self):
        self.two_clients()
        leased, token = bot.acquire_client_automation_lease(self.slow_customer.pk)
        self.assertIsNotNone(leased)
        self.assertTrue(token)
        try:
            with ExitStack() as stack:
                self.patched_provider(stack)
                handled = process_pending_revisions(self.settings, max_items=2)
            self.assertEqual(handled, 1)
            self.assertEqual([name for name, _ in self.events],
                ["healthy:provider-start", "healthy:provider-return", "healthy:sent"])
            self.slow_revision.refresh_from_db()
            self.assertEqual(self.slow_revision.state, "collecting")
            self.assertFalse(self.slow_revision.delivery_effects.exists())
            self.healthy_revision.refresh_from_db()
            self.assertEqual(self.healthy_revision.state, "processed")
            self.assertEqual(self.healthy_revision.delivery_effects.get().state, "sent")
        finally:
            bot.release_client_automation_lease(self.slow_customer.pk, token)

    def test_customer_lane_waits_for_native_queue_before_followups_or_poll_ingress(self):
        self.two_clients()
        self.settings.receive_via_poll = True
        self.settings.poll_interval_seconds = 2
        self.settings.save(update_fields=["receive_via_poll", "poll_interval_seconds"])
        lane_events = []

        def poll(*_args):
            lane_events.append(("poll", [name for name, _ in self.events]))
            return {"ok": True}

        def followups(*_args):
            lane_events.append(("followups", [name for name, _ in self.events]))

        with ExitStack() as stack:
            self.patched_provider(stack)
            stack.enter_context(patch.object(runner, "maintenance_status", return_value={"active": False}))
            stack.enter_context(patch.object(runner, "_drain_customer_followups", side_effect=followups))
            stack.enter_context(patch.object(bot, "poll_ingest", side_effect=poll))
            enabled, _last_poll, interrupted = runner._customer_lane(self.settings, 0.0)
        self.assertTrue(enabled)
        self.assertFalse(interrupted)
        self.assert_serial_delivery()
        self.assertEqual([name for name, _ in lane_events], ["followups", "poll", "followups"])
        for _name, observed in lane_events:
            self.assertIn("healthy:sent", observed, "The entire serial live drain precedes these lane operations.")
