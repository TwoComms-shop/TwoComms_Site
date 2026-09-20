"""Operational readiness is not the release drain's raw pending count."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgBotNotification, IgClient, IgConversationAnalysisJob, IgCustomerTurn,
    IgCustomerTurnRevision, IgTurnMessage, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_lane_health import operational_lane_snapshot, SAMPLE_LIMIT
from management.services.ig_response_debt import record_reply_debt
from management.services.ig_task_health import (
    TASK_SPECS, mark_task_failed, mark_task_succeeded, release_queue_snapshot,
)
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00")
class OperationalLaneHealthTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True)
        self.person = IgClient.objects.create(igsid="private-customer-health")
        self.daemon = patch("management.services.ig_daemon_health.daemon_runtime_health_snapshot", return_value={
            "process_online": True, "main_healthy": True, "process_age_seconds": 1,
            "main_age_seconds": 1, "stalled_reason": "",
        })
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        for target, result in (
            ("management.services.instagram_bot.ingress_status", {"healthy": True}),
            ("management.services.instagram_bot.allowed_sender_ids", set()),
            ("management.services.ig_maintenance.maintenance_status", {"active": False}),
        ):
            mock = patch(target, return_value=result)
            mock.start()
            self.addCleanup(mock.stop)

    def snapshot(self):
        return operational_lane_snapshot(now=self.now)

    def source(self, person=None):
        person = person or self.person
        return InstagramBotMessage.objects.create(
            client=person, sender_id=person.igsid, role="user", status="pending",
            text="private source 0501234567", mid=f"health-{person.pk}",
            provider_namespace="instagram_login:private-owner",
        )

    def revision(self, *, age=0, future=False):
        source = self.source()
        moment = self.now - timedelta(seconds=age)
        turn = IgCustomerTurn.objects.create(client=self.person, primary_source_message=source,
            window_started_at=moment, window_deadline=moment)
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [source], now=moment, bypass_quiet=not future).revision
        return source, revision

    def test_due_customer_work_is_healthy_without_draining_raw_queue(self):
        self.revision()
        result = self.snapshot()
        self.assertTrue(result["healthy"], result)
        self.assertEqual(result["lanes"]["customer_revisions"]["counts"]["runnable"], 1)
        self.assertGreater(release_queue_snapshot()["dangerous_backlog"], 0)

    def test_future_analysis_and_retry_backoff_are_healthy(self):
        job = IgConversationAnalysisJob.objects.create(client=self.person,
            due_at=self.now + timedelta(hours=1), next_attempt_at=self.now)
        result = self.snapshot()
        self.assertTrue(result["healthy"], result)
        self.assertEqual(result["lanes"]["conversation_analysis"]["counts"]["deferred"], 1)
        job.due_at = self.now - timedelta(hours=1)
        job.next_attempt_at = self.now + timedelta(minutes=1)
        job.save()
        self.assertTrue(self.snapshot()["healthy"])

    def test_analysis_starvation_is_visible_despite_fresh_daemon_pulse(self):
        IgConversationAnalysisJob.objects.create(client=self.person,
            due_at=self.now - timedelta(hours=1), next_attempt_at=self.now,
            last_error="private provider error")
        result = self.snapshot()
        self.assertFalse(result["healthy"])
        self.assertEqual(result["lanes"]["conversation_analysis"]["state"], "stalled")
        self.assertGreater(result["lanes"]["conversation_analysis"]["oldest_runnable_age_seconds"], 900)

    def test_expired_revision_requires_real_manual_owner(self):
        _source, revision = self.revision(age=3600)
        self.assertFalse(self.snapshot()["healthy"])
        record_reply_debt(revision, "provider_candidates_exhausted", now=self.now)
        result = self.snapshot()
        self.assertTrue(result["healthy"], result)
        self.assertEqual(result["lanes"]["customer_revisions"]["counts"]["manual"], 1)

    def test_unowned_old_source_is_attention_not_runnable(self):
        source = self.source()
        InstagramBotMessage.objects.filter(pk=source.pk).update(created_at=self.now - timedelta(days=2))
        result = self.snapshot()
        self.assertFalse(result["healthy"])
        self.assertEqual(result["lanes"]["legacy_inbound"]["counts"]["attention"], 1)

    def test_paused_customer_and_disabled_bot_are_visible_deferred(self):
        self.source()
        self.person.bot_paused = True
        self.person.save(update_fields=["bot_paused"])
        self.assertTrue(self.snapshot()["healthy"])
        self.assertEqual(self.snapshot()["lanes"]["legacy_inbound"]["counts"]["deferred"], 1)
        self.settings_row.is_enabled = False
        self.settings_row.save()
        with patch("management.services.ig_daemon_health.daemon_runtime_health_snapshot", return_value={
            "process_online": False, "main_healthy": False, "process_age_seconds": None,
            "main_age_seconds": None, "stalled_reason": "",
        }):
            result = self.snapshot()
        self.assertTrue(result["healthy"])
        self.assertEqual(result["bot_state"], "disabled")

    def test_expired_analysis_claim_and_unknown_notification_degrade(self):
        IgConversationAnalysisJob.objects.create(client=self.person, status="processing",
            lease_until=self.now - timedelta(seconds=1))
        self.assertEqual(self.snapshot()["lanes"]["conversation_analysis"]["counts"]["attention"], 1)
        IgBotNotification.objects.create(client=self.person, dedupe_key="health-unknown", status="unknown")
        self.assertFalse(self.snapshot()["healthy"])
        self.assertEqual(self.snapshot()["lanes"]["manager_notifications"]["counts"]["unknown"], 1)

    def test_late_failed_row_cannot_hide_outside_bounded_sample(self):
        for index in range(SAMPLE_LIMIT):
            IgBotNotification.objects.create(dedupe_key=f"health-{index}", next_attempt_at=self.now + timedelta(hours=1))
        IgBotNotification.objects.create(dedupe_key="late-failed", status="failed")
        result = self.snapshot()["lanes"]["manager_notifications"]
        self.assertTrue(result["sampled"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["attention_total"], 1)
        self.assertFalse(result["healthy"])

    def test_missing_per_lane_progress_is_not_invented_from_process_pulse(self):
        result = self.snapshot()
        self.assertIsNone(result["lanes"]["conversation_analysis"]["progress_age_seconds"])
        self.assertEqual(result["lanes"]["conversation_analysis"]["progress_evidence"], "unavailable")
        self.assertEqual(result["lanes"]["trace_refresh"]["state"], "unobserved")
        self.assertEqual(result["lanes"]["typed_memory"]["state"], "disabled")

    @patch("management.services.ig_lane_health.shadow_enabled", return_value=True)
    def test_isolated_consumer_failure_is_visible_without_raw_queue_rewrite(self, _shadow_enabled):
        mark_task_succeeded("ig_typed_memory_reconcile", at=self.now - timedelta(hours=1))
        mark_task_failed("ig_trace_refresh", RuntimeError("refresh failed"), at=self.now)
        result = self.snapshot()
        self.assertFalse(result["healthy"])
        self.assertEqual(result["lanes"]["typed_memory"]["state"], "stalled")
        self.assertEqual(result["lanes"]["trace_refresh"]["state"], "failed")
        self.assertEqual(result["unobserved_lanes"], [])

    def test_missing_settings_and_database_outage_are_unavailable_without_bootstrap(self):
        InstagramBotSettings.objects.all().delete()
        self.assertFalse(self.snapshot()["available"])
        self.assertFalse(InstagramBotSettings.objects.exists())
        with patch("management.services.ig_lane_health.InstagramBotSettings.objects.filter", side_effect=DatabaseError("private db error")):
            self.assertFalse(self.snapshot()["available"])

    def test_public_get_is_read_only_no_http_and_contains_no_customer_evidence(self):
        self.revision()
        for spec in TASK_SPECS:
            mark_task_succeeded(spec.key, at=self.now)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("health performed HTTP")), CaptureQueriesContext(connection) as captured:
            response = self.client.get("/bot/health/", HTTP_HOST="management.twocomms.shop", secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        writes = [row["sql"] for row in captured if row["sql"].lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}]
        self.assertEqual(writes, [])
        data = response.json()
        self.assertGreater(data["queues"]["dangerous_backlog"], 0)
        for secret in (self.person.igsid, "0501234567", "private-owner", "private source"):
            self.assertNotIn(secret, json.dumps(data))
