"""Durable low-priority refresh bounds, coalescing and source fences."""
from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgClient, IgConversationAnalysisJob, IgFunnelResetAudit, IgJourneyTraceSnapshot,
    IgJourneyTraceRefreshControl as Control, IgJourneyTraceRefreshJob as Job,
    InstagramBotLog, InstagramBotMessage,
)
from management.services import ig_journey_trace_refresh as refresh
from management.services.ig_journey_trace_generation import generate_journey_trace
from management.services.ig_typed_memory import purge_client_analysis_memory
from management.tests_ig_journey_trace_generation import JourneyTraceGenerationTests


class JourneyTraceRefreshTests(TestCase):
    reply = staticmethod(JourneyTraceGenerationTests.reply)

    def setUp(self):
        JourneyTraceGenerationTests.setUp(self)
        self.operator = get_user_model().objects.create(username="trace-operator", is_staff=True, is_active=True)
        Control.objects.get_or_create(pk=1)
        self.settings.analysis_backfill_enabled = False
        self.settings.save(update_fields=["analysis_backfill_enabled"])

    def activate(self):
        return refresh.configure_refresh(enabled=True, actor_id=self.operator.pk)

    def add_message(self, client=None, text="Хочу інший варіант"):
        client = client or self.buyer
        return InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            role="user", status="done", text=text)

    def due(self):
        Job.objects.update(due_at=timezone.now() - timedelta(seconds=1))

    def test_audited_activation_skips_history_and_configuration_is_bounded(self):
        self.assertEqual(refresh.refresh_tick()["status"], "disabled")
        self.assertFalse(Job.objects.exists())
        self.provider.assert_not_called()
        with self.assertRaises(CommandError):
            call_command("configure_ig_journey_trace_refresh", enable=True, stdout=StringIO())
        with self.assertRaises(ValueError):
            refresh.configure_refresh(enabled=True, actor_id=self.operator.pk, max_starts_per_hour=25)
        out = StringIO()
        call_command("configure_ig_journey_trace_refresh", enable=True, actor_id=self.operator.pk, stdout=out)
        self.assertEqual(json.loads(out.getvalue())["activation_watermark"], self.job.watermark_message_id)
        refresh.discover_refresh_requests()
        self.assertFalse(Job.objects.exists())
        self.assertEqual(InstagramBotLog.objects.filter(event="journey_trace_refresh_configuration").count(), 1)
        newest = self.add_message()
        refresh.discover_refresh_requests()
        job = Job.objects.get(client=self.buyer)
        self.assertEqual(job.requested_watermark, newest.pk)
        self.assertGreater(job.due_at, timezone.now() + timedelta(seconds=25))
        # Disable/re-enable reinitializes admission, not the provider lease.
        Control.objects.filter(pk=1).update(lease_token="running", lease_until=timezone.now() + timedelta(seconds=120))
        refresh.configure_refresh(enabled=False, actor_id=self.operator.pk)
        refresh.configure_refresh(enabled=True, actor_id=self.operator.pk)
        self.assertEqual(Control.objects.get(pk=1).lease_token, "running")
        self.due()
        self.assertNotIn("client", refresh.refresh_tick())

    def test_new_clients_discovered_without_snapshot_and_repair_is_grouped(self):
        self.activate()
        clients = [IgClient(igsid=f"refresh-new-{i}") for i in range(105)]
        IgClient.objects.bulk_create(clients)
        InstagramBotMessage.objects.bulk_create([InstagramBotMessage(client=row, sender_id=row.igsid,
            role="user", status="done", text="Підкажіть") for row in clients])
        Control.objects.filter(pk=1).update(repair_at=timezone.now())
        with CaptureQueriesContext(connection) as captured:
            result = refresh.discover_refresh_requests()
        self.assertEqual(result["messages"], 100)
        self.assertEqual(Job.objects.count(), 100)
        self.assertLessEqual(len(captured), 12)
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())
        # Simulate a missed late commit below the main cursor, then repair it.
        target = clients[-1]
        latest = InstagramBotMessage.objects.latest("id").pk
        Control.objects.filter(pk=1).update(scan_cursor=latest, repair_cursor=clients[-3].pk, repair_at=None)
        refresh.discover_refresh_requests()
        self.assertTrue(Job.objects.filter(client=target).exists())
        self.provider.assert_not_called()

    def test_auto_own_gate_busy_and_mapping_skips_do_not_consume_attempt(self):
        self.activate()
        self.add_message()
        refresh.discover_refresh_requests()
        self.due()
        self.waiting.return_value = True
        result = refresh.refresh_tick()["client"]
        self.assertEqual(result["reason"], "customer_reply_busy")
        self.waiting.return_value = False
        self.due()
        self.mapping.return_value = {}
        self.assertEqual(refresh.refresh_tick()["client"]["reason"], "key_project_mapping_missing")
        self.mapping.return_value = {"test-key": "project"}
        self.due()
        IgConversationAnalysisJob.objects.filter(pk=self.job.pk).update(status="pending")
        self.assertEqual(refresh.refresh_tick()["reason"], "normal_work_busy")
        job = Job.objects.get(client=self.buyer)
        self.assertEqual(job.attempted_fingerprint, "")
        self.assertEqual(Control.objects.get(pk=1).budget_starts, 0)
        self.provider.assert_not_called()

    def test_auto_success_no_normal_job_writes_and_same_fingerprint_not_replayed(self):
        self.activate()
        self.add_message()
        refresh.discover_refresh_requests()
        self.due()
        before = IgConversationAnalysisJob.objects.filter(pk=self.job.pk).values().get()
        result = refresh.refresh_tick()["client"]
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["admission"], "automatic_refresh")
        self.assertEqual(IgConversationAnalysisJob.objects.filter(pk=self.job.pk).values().get(), before)
        job = Job.objects.get(client=self.buyer)
        self.assertEqual((job.status, job.last_snapshot_id), ("done", result["snapshot_id"]))
        self.assertEqual(Control.objects.get(pk=1).lease_token, "")
        self.assertNotIn("client", refresh.refresh_tick())
        # Even explicitly re-pending the same input only returns the artifact.
        Job.objects.update(status="pending", due_at=timezone.now())
        self.assertEqual(refresh.refresh_tick()["client"]["status"], "existing")
        self.assertEqual(self.provider.call_count, 1)

    def test_failure_is_terminal_for_input_new_input_observes_cooldown(self):
        self.activate()
        self.add_message()
        refresh.discover_refresh_requests()
        self.due()
        self.provider.side_effect = RuntimeError("PRIVATE provider credentials")
        first = refresh.refresh_tick()["client"]
        self.assertEqual(first["reason"], "provider_failed")
        self.assertNotIn("PRIVATE", json.dumps(first))
        self.assertEqual(Job.objects.get(client=self.buyer).status, "failed")
        Job.objects.update(status="pending", due_at=timezone.now())
        self.assertEqual(refresh.refresh_tick()["client"]["reason"], "fingerprint_already_attempted")
        self.assertEqual(self.provider.call_count, 1)
        self.add_message(text="Нове питання")
        refresh.discover_refresh_requests()
        job = Job.objects.get(client=self.buyer)
        self.assertEqual(job.status, "pending")
        self.assertGreaterEqual(job.due_at, job.last_attempt_at + timedelta(seconds=120))
        self.assertNotIn("client", refresh.refresh_tick())

    def test_shared_manual_lease_budget_and_expiry_are_token_fenced(self):
        self.activate()
        newest = self.add_message()
        refresh.discover_refresh_requests()
        self.due()
        token, reason = refresh.start_trace_attempt(client_id=self.buyer.pk, fingerprint="a" * 64,
            watermark=newest.pk, reset_floor=1, automatic=True)
        self.assertEqual(reason, "")
        self.assertEqual(generate_journey_trace(self.buyer.pk, apply=True, allow_historical=True)["reason"], "trace_lease_busy")
        self.provider.assert_not_called()
        Control.objects.filter(pk=1).update(lease_until=timezone.now() - timedelta(seconds=1))
        second, reason = refresh.start_trace_attempt(client_id=self.buyer.pk, fingerprint="b" * 64,
            watermark=newest.pk, reset_floor=1, automatic=False)
        self.assertEqual(reason, "")
        refresh.finish_trace_attempt(client_id=self.buyer.pk, token=token, report={"reason": "provider_failed"}, automatic=True)
        self.assertEqual(Control.objects.get(pk=1).lease_token, second)
        refresh.finish_trace_attempt(client_id=self.buyer.pk, token=second, report={"reason": "provider_failed"}, automatic=False)
        Control.objects.filter(pk=1).update(budget_starts=6)
        self.assertEqual(generate_journey_trace(self.buyer.pk, apply=True, allow_historical=True)["reason"], "refresh_budget")
        self.provider.assert_not_called()

    def test_new_generation_during_call_is_preserved_and_old_result_discarded(self):
        self.activate()
        self.add_message()
        refresh.discover_refresh_requests()
        self.due()
        def changed(*args, **kwargs):
            response = self.reply(*args, **kwargs)
            self.add_message(text="Зміна рішення")
            refresh.discover_refresh_requests()
            return response
        self.provider.side_effect = changed
        result = refresh.refresh_tick()["client"]
        self.assertEqual(result["reason"], "sources_changed")
        job = Job.objects.get(client=self.buyer)
        self.assertEqual((job.status, job.generation), ("pending", 2))
        self.assertGreater(job.due_at, timezone.now())
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())

    def test_erasure_during_provider_purges_state_and_cannot_recreate(self):
        self.activate()
        self.add_message()
        refresh.discover_refresh_requests()
        self.due()
        def erased(*args, **kwargs):
            response = self.reply(*args, **kwargs)
            IgClient.objects.filter(pk=self.buyer.pk).update(privacy_erasure_started_at=timezone.now())
            purge_client_analysis_memory([self.buyer.pk])
            # Purge cannot release an in-flight global lease prematurely.
            self.assertTrue(Control.objects.get(pk=1).lease_token)
            return response
        self.provider.side_effect = erased
        self.assertEqual(refresh.refresh_tick()["client"]["reason"], "privacy_erasure")
        self.assertFalse(Job.objects.exists())
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())
        self.add_message()
        refresh.discover_refresh_requests()
        self.assertFalse(Job.objects.exists())
        self.assertEqual(Control.objects.get(pk=1).lease_token, "")

    def test_reset_invalidates_old_input_and_one_query_coverage_is_sanitized(self):
        self.activate()
        msg = self.add_message()
        refresh.discover_refresh_requests()
        self.due()
        def reset(*args, **kwargs):
            response = self.reply(*args, **kwargs)
            IgFunnelResetAudit.objects.create(client=self.buyer, reset_after_message_id=msg.pk)
            return response
        self.provider.side_effect = reset
        self.assertEqual(refresh.refresh_tick()["client"]["reason"], "sources_changed")
        self.assertFalse(IgJourneyTraceSnapshot.objects.exists())
        Job.objects.update(last_reason="PRIVATE unsafe error")
        with self.assertNumQueries(1):
            result = refresh.read_refresh_coverage(self.buyer.pk)
        self.assertEqual(result["last_reason"], "internal_failure")
        self.assertNotIn("PRIVATE", json.dumps(result))
