import json
from datetime import timedelta
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils import timezone

from management.bot_views import bot_health
from management.models import (
    IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgTurnMessage, InstagramBotLog,
    InstagramBotMessage,
)
from management.services.ig_revision_live import RevisionLiveResult, _log_revision_blocked
from management.services.ig_task_health import release_queue_snapshot
from management.services.ig_turn_revisions import create_collecting_revision


class SilenceDiagnosticsTests(TestCase):
    def test_blocked_reply_keeps_exact_reason_and_source_scope_without_customer_text(self):
        _log_revision_blocked(225, 360, RevisionLiveResult(
            225, "blocked", ("current_purpose_disallows_sales",),
        ))
        row = InstagramBotLog.objects.get()
        metadata = json.loads(row.detail)
        self.assertEqual(metadata["kind"], "reply_failed")
        self.assertEqual(metadata["reason"], "current_purpose_disallows_sales")
        self.assertEqual(metadata["scope"], {"revision_id": 225, "client_id": 360})

    def test_unknown_failure_text_does_not_enter_structured_console(self):
        with patch("management.services.instagram_bot._INCIDENT_LOGGER"):
            _log_revision_blocked(225, 360, RevisionLiveResult(
                225, "blocked", ("private customer payload",),
            ))
        self.assertNotIn("private customer", InstagramBotLog.objects.get().detail)
        self.assertEqual(json.loads(InstagramBotLog.objects.get().detail)["reason"], "failed")

    def test_live_daemon_does_not_hide_unreconciled_expired_execution(self):
        past = timezone.now() - timedelta(minutes=10)
        client = IgClient.objects.create(igsid="health-overdue")
        source = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role="user", status="pending",
            text="Цікавить ця футболка", source="webhook", mid="health-overdue-mid",
            provider_namespace="instagram_login:health-owner", provider_created_at=past,
        )
        turn = IgCustomerTurn.objects.create(
            client=client, primary_source_message=source,
            window_started_at=past, window_deadline=past,
        )
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [source], now=past, bypass_quiet=True).revision
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(
            state="claimed", claim_token="expired-worker", lease_until=past,
            overall_deadline=past, sealed_at=past,
        )
        snapshot = release_queue_snapshot()
        self.assertEqual(snapshot["revision_execution_overdue"], 1)
        with (
            patch("management.services.ig_task_health.task_health_snapshot", return_value={"available": True, "healthy": True}),
            patch("management.services.ig_lane_health.operational_lane_snapshot", return_value={"available": True, "healthy": True}),
        ):
            response = bot_health(RequestFactory().get("/bot/health/"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["queues"]["revision_execution_overdue"], 1)
        # A durable operator/recovery disposition is visible work with its own
        # owner; it is no longer an abandoned live execution claim.
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(recovery_state="manual")
        self.assertEqual(release_queue_snapshot()["revision_execution_overdue"], 0)

    def test_healthy_inflight_revision_is_not_reported_as_abandoned(self):
        client = IgClient.objects.create(igsid="health-inflight")
        now = timezone.now()
        source = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            role="user", text="Цікавить футболка", status="pending")
        turn = IgCustomerTurn.objects.create(client=client, primary_source_message=source,
            window_started_at=now, window_deadline=now)
        IgCustomerTurnRevision.objects.create(
            client=client, turn=turn, revision=1, state="claimed", claim_token="live-worker",
            quiet_started_at=now, quiet_deadline=now, quiet_cap_at=now,
            overall_deadline=now + timedelta(seconds=30), lease_until=now + timedelta(minutes=3),
        )
        self.assertEqual(release_queue_snapshot()["revision_execution_overdue"], 0)
