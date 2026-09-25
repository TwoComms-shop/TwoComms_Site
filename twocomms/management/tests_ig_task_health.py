"""IMP-041 / IMP-059: supervise the real Instagram cron boundary."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    CallRecord,
    IgBotNotification,
    IgCustomerTurn,
    IgClient,
    InstagramBotMessage,
    IgTurnMessage,
    InstagramBotSettings,
    InstagramBotTaskHeartbeat,
)
from management.ig_bot_models import IgConversationAnalysisJob
from management.services.ig_turn_revisions import create_collecting_revision
from management.services.ig_task_health import (
    TASK_SPECS,
    check_task_health,
    ensure_task_expectations,
    mark_task_succeeded,
    mark_task_degraded,
    mark_task_failed,
    task_health_snapshot,
    task_health_alert_decisions,
    task_health_incident_key,
    task_heartbeat,
    release_queue_snapshot,
)
from management.tests_call_auto_analysis_helpers import enable_call_auto_analysis


class TaskHeartbeatTests(TestCase):
    def _mark_all_successful(self, *, at=None):
        for spec in TASK_SPECS:
            mark_task_succeeded(spec.key, at=at)

    def test_success_records_one_durable_task_row(self):
        with task_heartbeat("ig_deal_payments"):
            pass

        row = InstagramBotTaskHeartbeat.objects.get(task_key="ig_deal_payments")
        self.assertIsNotNone(row.last_started_at)
        self.assertIsNotNone(row.last_succeeded_at)
        self.assertEqual(row.consecutive_failures, 0)
        self.assertEqual(row.last_error_kind, "")

    @patch("management.services.instagram_bot.notify_manager")
    def test_failure_keeps_only_exception_kind_and_waits_for_repeated_failure(self, notify):
        with self.assertRaisesRegex(RuntimeError, "private customer text"):
            with task_heartbeat("ig_deal_payments"):
                raise RuntimeError("private customer text: 0501234567")

        row = InstagramBotTaskHeartbeat.objects.get(task_key="ig_deal_payments")
        self.assertEqual(row.last_error_kind, "RuntimeError")
        self.assertNotIn("0501234567", row.last_error_kind)
        self.assertEqual(row.consecutive_failures, 1)
        notify.assert_not_called()
        mark_task_failed("ig_deal_payments", RuntimeError("again"))
        notify.assert_not_called()
        mark_task_failed("ig_deal_payments", RuntimeError("again"))
        notify.assert_called_once()
        self.assertFalse(notify.call_args.kwargs["deliver_immediately"])
        self.assertEqual(notify.call_args.kwargs["event_type"], "ig_task_health")
        self.assertEqual(notify.call_args.kwargs["metadata"]["task_failure_reason"], "runtime_error")
        self.assertEqual(notify.call_args.kwargs["metadata"]["task_alert_policy_version"], 2)
        self.assertFalse(notify.call_args.kwargs["metadata"]["requires_human_review"])

    @patch("management.services.instagram_bot.notify_manager")
    def test_watchdog_initialization_pending_stays_diagnostic_during_startup(self, notify):
        mark_task_succeeded("ig_daemon_watchdog", at=timezone.now() - timedelta(days=3))
        with self.assertRaises(CommandError):
            with task_heartbeat("ig_daemon_watchdog"):
                raise CommandError(
                    "daemon initialization pending after singleton lock"
                )

        row = InstagramBotTaskHeartbeat.objects.get(task_key="ig_daemon_watchdog")
        self.assertEqual(row.last_error_kind, "daemon_initialization_pending")
        notify.assert_not_called()

    @patch("management.services.instagram_bot.notify_manager")
    def test_watchdog_actual_startup_deadline_failure_remains_immediate(self, notify):
        mark_task_failed("ig_daemon_watchdog", CommandError(
            "daemon startup exceeded pending window while holding singleton lock",
        ))
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["metadata"]["task_failure_reason"], "daemon_startup_stale")

    @patch("management.services.instagram_bot.notify_manager")
    def test_watchdog_lock_stale_is_immediate_and_typed(self, notify):
        with self.assertRaises(CommandError):
            with task_heartbeat("ig_daemon_watchdog"):
                raise CommandError("daemon did not release singleton lock")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event_type"], "ig_task_health")
        self.assertEqual(notify.call_args.kwargs["metadata"]["task_failure_reason"], "daemon_lock_stale")
        self.assertIn("Instagram", notify.call_args.args[0])

    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.ig_task_health._nova_has_outstanding_tracking", return_value=True)
    def test_provider_degraded_alerts_only_after_two_hours_of_due_work(self, _due, notify):
        now = timezone.now()
        mark_task_succeeded("nova_poshta_tracking", at=now - timedelta(hours=1))
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded", at=now)
        notify.assert_not_called()

        row = InstagramBotTaskHeartbeat.objects.get(task_key="nova_poshta_tracking")
        row.last_succeeded_at = now - timedelta(hours=2, seconds=1)
        row.save(update_fields=["last_succeeded_at"])
        check_task_health(now=now)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event_type"], "ig_task_health")
        self.assertEqual(notify.call_args.kwargs["metadata"]["task_alert_reason"], "overdue_tracking_work")

    def test_degraded_run_is_visible_as_degraded(self):
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        task = next(
            task for task in task_health_snapshot()["tasks"]
            if task["key"] == "nova_poshta_tracking"
        )
        self.assertEqual(task["state"], "degraded")
        self.assertTrue(task["degraded"])

    @patch("management.services.instagram_bot.notify_manager")
    def test_success_resets_provider_degraded_threshold(self, notify):
        for _ in range(2):
            mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        mark_task_succeeded("nova_poshta_tracking")

        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        row = InstagramBotTaskHeartbeat.objects.get(task_key="nova_poshta_tracking")
        self.assertEqual(row.consecutive_failures, 1)
        notify.assert_not_called()

    @patch("management.services.instagram_bot.notify_manager")
    def test_summary_cannot_bypass_provider_age_or_duplicate_alert(self, notify):
        self._mark_all_successful()
        for count in range(1, 4):
            with task_heartbeat("nova_poshta_tracking") as outcome:
                outcome.mark_degraded("nova_poshta_provider_degraded")
            snapshot = check_task_health()
            provider = next(t for t in snapshot["tasks"] if t["key"] == "nova_poshta_tracking")
            self.assertEqual(provider["state"], "degraded")
            self.assertEqual(provider["consecutive_failures"], count)
            self.assertEqual(notify.call_count, 0)

    @patch("management.services.instagram_bot.notify_manager")
    def test_degraded_provider_does_not_suppress_other_task_failure(self, notify):
        self._mark_all_successful()
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        for _ in range(3):
            mark_task_failed("ig_deal_payments", RuntimeError("failed"))
        notify.reset_mock()
        check_task_health()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event_type"], "ig_task_health")
        self.assertIn("оплат", notify.call_args.args[0])

    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.ig_task_health._nova_has_outstanding_tracking", return_value=True)
    def test_missing_cron_after_one_provider_failure_waits_for_due_impact(self, _due, notify):
        now = timezone.now()
        self._mark_all_successful(at=now)
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded", at=now - timedelta(seconds=901))
        row = InstagramBotTaskHeartbeat.objects.get(task_key="nova_poshta_tracking")
        row.last_succeeded_at = now - timedelta(seconds=1200)
        row.last_started_at = now - timedelta(seconds=902)
        row.save(update_fields=["last_succeeded_at", "last_started_at"])
        snapshot = check_task_health(now=now)
        provider = next(t for t in snapshot["tasks"] if t["key"] == "nova_poshta_tracking")
        self.assertEqual(provider["state"], "stale")
        notify.assert_not_called()
        row.last_succeeded_at = now - timedelta(hours=2, seconds=1)
        row.last_failed_at = now - timedelta(hours=2, seconds=1)
        row.last_started_at = now - timedelta(hours=2, seconds=1)
        row.save(update_fields=["last_succeeded_at", "last_failed_at", "last_started_at"])
        check_task_health(now=now)
        notify.assert_called_once()

    @patch("management.services.instagram_bot.notify_manager")
    def test_application_failures_do_not_count_toward_provider_streak(self, notify):
        for _ in range(2):
            mark_task_failed("nova_poshta_tracking", RuntimeError("db unavailable"))
        notify.reset_mock()
        row = mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        self.assertEqual(row.consecutive_failures, 1)
        notify.assert_not_called()

    @patch("management.services.instagram_bot.notify_manager")
    def test_mixed_application_failure_classes_count_as_one_critical_streak(self, notify):
        mark_task_failed("ig_deal_payments", RuntimeError("first"))
        mark_task_failed("ig_deal_payments", ValueError("second"))
        notify.assert_not_called()
        mark_task_failed("ig_deal_payments", CommandError("third"))
        row = InstagramBotTaskHeartbeat.objects.get(task_key="ig_deal_payments")
        self.assertEqual(row.consecutive_failures, 3)
        notify.assert_called_once()

    @patch("management.services.instagram_bot.notify_manager")
    def test_provider_streak_does_not_accelerate_first_application_failure(self, notify):
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        mark_task_failed("nova_poshta_tracking", RuntimeError("application"))
        row = InstagramBotTaskHeartbeat.objects.get(task_key="nova_poshta_tracking")
        self.assertEqual(row.consecutive_failures, 1)
        notify.assert_not_called()

    @patch("management.services.instagram_bot.notify_manager")
    def test_skipped_pass_preserves_failure_streak_but_records_liveness(self, notify):
        now = timezone.now()
        self._mark_all_successful(at=now)
        row = InstagramBotTaskHeartbeat.objects.get(task_key="nova_poshta_tracking")
        row.last_succeeded_at = now - timedelta(hours=2)
        row.last_started_at = now - timedelta(hours=1)
        row.save(update_fields=["last_succeeded_at", "last_started_at"])
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded", at=now - timedelta(hours=1))
        with task_heartbeat("nova_poshta_tracking") as outcome:
            outcome.mark_skipped()
        row.refresh_from_db()
        self.assertEqual(row.consecutive_failures, 1)
        self.assertEqual(row.last_succeeded_at, now - timedelta(hours=2))
        snapshot = check_task_health()
        provider = next(t for t in snapshot["tasks"] if t["key"] == "nova_poshta_tracking")
        self.assertEqual(provider["state"], "degraded")
        notify.assert_not_called()
        mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        row.refresh_from_db()
        self.assertEqual(row.consecutive_failures, 2)
        notify.assert_not_called()

    @patch("management.services.instagram_bot.notify_manager")
    def test_provider_streak_sql_reads_old_reason_before_replacing_it(self, notify):
        # MariaDB evaluates single-table assignments left-to-right; SQLite
        # only checks the final values and would miss an ordering regression.
        mark_task_failed("nova_poshta_tracking", RuntimeError("failure"))
        with CaptureQueriesContext(connection) as queries:
            mark_task_degraded("nova_poshta_tracking", "nova_poshta_provider_degraded")
        sql = next(q["sql"] for q in queries if q["sql"].startswith("UPDATE") and "CASE" in q["sql"])
        setters = sql.split(" SET ", 1)[1]
        self.assertTrue(setters.startswith(connection.ops.quote_name("consecutive_failures") + " = CASE"))

    def test_degraded_reason_rejects_unreviewed_values(self):
        with self.assertRaises(ValueError):
            mark_task_degraded("nova_poshta_tracking", "customer private text")
        self.assertFalse(InstagramBotTaskHeartbeat.objects.exists())

    def test_unobserved_task_has_a_deploy_grace_period_then_degrades(self):
        self.assertTrue(ensure_task_expectations())
        now = timezone.now()

        fresh = task_health_snapshot(now=now)
        self.assertTrue(fresh["healthy"])

        later = task_health_snapshot(now=now + timedelta(minutes=13))
        payment = next(task for task in later["tasks"] if task["key"] == "ig_deal_payments")
        self.assertEqual(payment["state"], "not_observed")
        self.assertFalse(later["healthy"])

    @patch("management.services.instagram_bot.notify_manager")
    def test_health_check_registers_missing_expectations_before_alerting(self, notify):
        now = timezone.now()
        first = check_task_health(now=now)
        self.assertFalse(any(task["state"] == "unobserved" for task in first["tasks"]))
        self.assertTrue(InstagramBotTaskHeartbeat.objects.filter(task_key="ig_checkout_reconcile").exists())
        notify.assert_not_called()

        later = check_task_health(now=now + timedelta(minutes=17))
        checkout = next(task for task in later["tasks"] if task["key"] == "ig_checkout_reconcile")
        self.assertEqual(checkout["state"], "not_observed")
        self.assertTrue(any(
            call.kwargs["metadata"]["task_key"] == "ig_checkout_reconcile"
            for call in notify.call_args_list
        ))

    @patch("management.services.instagram_bot.notify_manager")
    def test_prolonged_stale_task_gets_one_own_incident(self, notify):
        now = timezone.now()
        self._mark_all_successful(at=now)
        stale_at = now - timedelta(minutes=25)
        row = InstagramBotTaskHeartbeat.objects.get(task_key="ig_deal_payments")
        row.last_succeeded_at = stale_at
        row.save(update_fields=["last_succeeded_at", "updated_at"])

        snapshot = check_task_health(now=now)

        self.assertFalse(snapshot["healthy"])
        self.assertEqual(snapshot["unhealthy_count"], 1)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event_type"], "ig_task_health")
        self.assertIn("перевірка фонової задачі", notify.call_args.args[0])
        self.assertEqual(notify.call_args.kwargs["dedupe_key"],
                         task_health_incident_key(row))

    @patch("management.services.instagram_bot.notify_manager")
    def test_new_critical_task_has_independent_incident_key(self, notify):
        now = timezone.now()
        self._mark_all_successful(at=now - timedelta(hours=3))
        with patch("management.services.ig_task_health._nova_has_outstanding_tracking", return_value=True):
            decisions = task_health_alert_decisions(now=now)
        keys = {decision["task_key"]: decision["incident_key"] for decision in decisions}
        self.assertIn("nova_poshta_tracking", keys)
        self.assertIn("ig_checkout_reconcile", keys)
        self.assertNotEqual(keys["nova_poshta_tracking"], keys["ig_checkout_reconcile"])
        self.assertFalse(notify.called)

    @patch("management.services.ig_task_health._nova_has_outstanding_tracking", return_value=False)
    def test_nova_stale_without_due_shipments_is_only_diagnostic(self, _due):
        now = timezone.now()
        self._mark_all_successful(at=now)
        row = InstagramBotTaskHeartbeat.objects.get(task_key="nova_poshta_tracking")
        row.last_succeeded_at = now - timedelta(hours=3)
        row.save(update_fields=["last_succeeded_at"])
        snapshot = task_health_snapshot(now=now)
        self.assertEqual(next(t for t in snapshot["tasks"] if t["key"] == row.task_key)["state"], "stale")
        self.assertEqual(task_health_alert_decisions(snapshot=snapshot, now=now, task_key=row.task_key), [])

    @patch("orders.nova_poshta_service.NovaPoshtaService")
    def test_nova_deferred_shipments_still_justify_prolonged_alert(self, service_cls):
        now = timezone.now()
        self._mark_all_successful(at=now)
        row = InstagramBotTaskHeartbeat.objects.get(task_key="nova_poshta_tracking")
        row.last_succeeded_at = now - timedelta(hours=3)
        row.save(update_fields=["last_succeeded_at"])
        # These are outstanding shipments whose provider retry date is later;
        # the ordinary due-only poll would currently return no rows.
        service_cls.return_value.get_orders_with_tracking_queryset.return_value.exists.return_value = True

        decisions = task_health_alert_decisions(now=now, task_key=row.task_key)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["reason"], "overdue_tracking_work")
        service_cls.return_value.get_orders_with_tracking_queryset.assert_called_once_with(
            include_deferred=True,
        )

    def test_success_changes_incident_identity(self):
        before = timezone.now() - timedelta(hours=3)
        mark_task_succeeded("ig_deal_payments", at=before)
        row = InstagramBotTaskHeartbeat.objects.get(task_key="ig_deal_payments")
        first = task_health_incident_key(row)
        mark_task_succeeded("ig_deal_payments", at=before + timedelta(hours=1))
        row.refresh_from_db()
        self.assertNotEqual(first, task_health_incident_key(row))

    def test_all_production_cron_tasks_have_an_explicit_specification(self):
        self.assertEqual(
            {spec.key for spec in TASK_SPECS},
            {
                "ig_checkout_reconcile",
                "ig_order_fulfillment",
                "ig_deal_payments",
                "order_telegram_reconcile",
                "nova_poshta_tracking",
                "ig_typed_memory_reconcile",
                "ig_trace_refresh",
                "binotel_call_ai_analyses",
            },
        )


class CronCommandHeartbeatTests(TestCase):
    def setUp(self):
        enable_call_auto_analysis(self)

    def test_binotel_call_ai_empty_run_records_success(self):
        call_command("run_call_ai_analyses", limit=1)

        self.assertTrue(
            InstagramBotTaskHeartbeat.objects.filter(
                task_key="binotel_call_ai_analyses",
                last_succeeded_at__isnull=False,
            ).exists()
        )

    @patch(
        "management.models.CallAIAnalysis.objects.filter",
        side_effect=RuntimeError("provider queue unavailable"),
    )
    def test_binotel_call_ai_failure_is_observed(self, _filter):
        with self.assertRaisesRegex(RuntimeError, "provider queue unavailable"):
            call_command("run_call_ai_analyses", limit=1)

        self.assertTrue(
            InstagramBotTaskHeartbeat.objects.filter(
                task_key="binotel_call_ai_analyses",
                last_failed_at__isnull=False,
                last_error_kind="RuntimeError",
            ).exists()
        )

    def test_binotel_call_ai_dry_run_does_not_record_heartbeat(self):
        call_command("run_call_ai_analyses", limit=1, dry_run=True)

        self.assertFalse(
            InstagramBotTaskHeartbeat.objects.filter(
                task_key="binotel_call_ai_analyses",
            ).exists()
        )

    @override_settings(NOVA_POSHTA_API_KEY="")
    def test_nova_poshta_tracking_configuration_failure_is_observed(self):
        with self.assertRaisesRegex(CommandError, "NOVA_POSHTA_API_KEY"):
            call_command("update_tracking_statuses")

        self.assertTrue(
            InstagramBotTaskHeartbeat.objects.filter(
                task_key="nova_poshta_tracking",
                last_failed_at__isnull=False,
                last_error_kind="CommandError",
            ).exists()
        )

    @override_settings(NOVA_POSHTA_API_KEY="test-key")
    @patch("orders.management.commands.update_tracking_statuses.NovaPoshtaService")
    def test_nova_poshta_tracking_records_success(self, service_cls):
        service = service_cls.return_value
        queryset = MagicMock()
        queryset.count.return_value = 1
        service.get_orders_with_tracking_queryset.return_value = queryset
        service.update_all_tracking_statuses.return_value = {
            "total_orders": 1,
            "processed": 1,
            "updated": 0,
            "errors": 0,
        }

        call_command("update_tracking_statuses")

        self.assertTrue(
            InstagramBotTaskHeartbeat.objects.filter(
                task_key="nova_poshta_tracking",
                last_succeeded_at__isnull=False,
            ).exists()
        )

    @patch("management.management.commands.reconcile_ig_checkout.reconcile_ig_checkout")
    def test_checkout_reconciler_records_success(self, reconcile):
        reconcile.return_value = {"repaired": 0}

        call_command("reconcile_ig_checkout")

        self.assertTrue(
            InstagramBotTaskHeartbeat.objects.filter(
                task_key="ig_checkout_reconcile", last_succeeded_at__isnull=False
            ).exists()
        )

    @patch("management.management.commands.poll_ig_deal_payments.bot_payments.poll_pending_deals_locked")
    @patch("management.management.commands.poll_ig_deal_payments.bot_payments.reconcile_payment_projections")
    @patch("management.services.bot_orders.notify_shipped_deals", return_value=0)
    @patch("management.services.bot_orders.fulfill_ready_paid_deals", return_value=0)
    def test_payment_backstop_records_success(self, _fulfilled, _shipped, reconcile, poll):
        reconcile.return_value = 0
        poll.return_value = 0

        call_command("poll_ig_deal_payments")

        self.assertTrue(
            InstagramBotTaskHeartbeat.objects.filter(
                task_key="ig_deal_payments", last_succeeded_at__isnull=False
            ).exists()
        )


@override_settings(ALLOWED_HOSTS=["management.twocomms.shop", "testserver"])
class BotHealthEndpointTests(TestCase):
    def _make_all_tasks_healthy(self):
        for spec in TASK_SPECS:
            mark_task_succeeded(spec.key)

    def test_public_endpoint_is_ready_when_disabled_bot_and_cron_are_healthy(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.save(update_fields=["is_enabled", "updated_at"])
        enable_call_auto_analysis(self)
        self._make_all_tasks_healthy()

        response = self.client.get("/bot/health/", HTTP_HOST="management.twocomms.shop", secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertNotIn("tasks", response.json())
        self.assertEqual(response["Cache-Control"], "no-store, no-cache, must-revalidate, max-age=0")

    def test_public_endpoint_degrades_when_no_cron_heartbeat_exists(self):
        response = self.client.get("/bot/health/", HTTP_HOST="management.twocomms.shop", secure=True)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "degraded")

    def test_public_endpoint_reports_dangerous_queues_and_analysis_failures(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.save(update_fields=["is_enabled", "updated_at"])
        self._make_all_tasks_healthy()
        client = IgClient.objects.create(igsid="release-health-queue-client")
        InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            status=InstagramBotMessage.Status.PENDING,
            text="pending inbound",
        )
        IgBotNotification.objects.create(
            client=client,
            event_type="release-health",
            dedupe_key="release-health-queue",
            status=IgBotNotification.Status.UNKNOWN,
        )
        IgConversationAnalysisJob.objects.create(
            client=client,
            status=IgConversationAnalysisJob.Status.FAILED,
        )

        snapshot = release_queue_snapshot()
        self.assertEqual(snapshot["dangerous_backlog"], 2)
        self.assertEqual(snapshot["legacy_inbound_pending"], 1)
        self.assertEqual(snapshot["revision_owned_pending"], 0)
        self.assertEqual(snapshot["analysis_failed"], 1)

        response = self.client.get("/bot/health/", HTTP_HOST="management.twocomms.shop", secure=True)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["queues"]["dangerous_backlog"], 2)
        self.assertEqual(response.json()["queues"]["analysis_failed"], 1)

    def test_release_snapshot_preserves_revision_owned_pending_as_evidence(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.save(update_fields=["is_enabled", "updated_at"])
        self._make_all_tasks_healthy()
        client = IgClient.objects.create(igsid="release-health-owned-client")
        source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            status=InstagramBotMessage.Status.PENDING,
            text="owned revision source",
        )
        turn = IgCustomerTurn.objects.create(
            client=client,
            primary_source_message=source,
            window_started_at=timezone.now(),
            window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [source], bypass_quiet=True).revision
        revision.sealed_at = timezone.now()
        revision.save(update_fields=["sealed_at", "updated_at"])

        snapshot = release_queue_snapshot()

        self.assertEqual(snapshot["inbound_pending"], 1)
        self.assertEqual(snapshot["legacy_inbound_pending"], 0)
        self.assertEqual(snapshot["revision_owned_pending"], 1)
        self.assertEqual(
            snapshot["revision_owned_pending_reason"],
            "preserved_revision_debt_evidence",
        )
        self.assertEqual(snapshot["dangerous_backlog"], 0)

    def test_release_snapshot_reports_sanitized_binotel_queue_categories(self):
        settings = InstagramBotSettings.load()
        settings.is_enabled = False
        settings.save(update_fields=["is_enabled", "updated_at"])
        enable_call_auto_analysis(self)
        self._make_all_tasks_healthy()
        CallRecord.objects.create(
            provider="binotel",
            external_call_id="health-eligible",
            duration_seconds=65,
            payload={"disposition": "ANSWER"},
            ai_status=CallRecord.AiStatus.PENDING,
        )
        CallRecord.objects.create(
            provider="binotel",
            external_call_id="health-metadata",
            payload={"generalCallID": "health-metadata"},
            ai_status=CallRecord.AiStatus.PENDING,
        )
        CallRecord.objects.create(
            provider="binotel",
            external_call_id="health-ineligible",
            payload={"disposition": "NOANSWER"},
            ai_status=CallRecord.AiStatus.PENDING,
        )
        CallRecord.objects.create(
            provider="binotel",
            external_call_id="health-stale",
            ai_status=CallRecord.AiStatus.RUNNING,
            ai_locked_at=timezone.now() - timedelta(minutes=20),
        )
        CallRecord.objects.create(
            provider="binotel",
            external_call_id="health-error",
            ai_status=CallRecord.AiStatus.ERROR,
        )
        CallRecord.objects.create(
            provider="aggregate",
            external_call_id="health-non-binotel-pending",
            duration_seconds=65,
            payload={"disposition": "ANSWER"},
            ai_status=CallRecord.AiStatus.PENDING,
        )
        CallRecord.objects.create(
            provider="aggregate",
            external_call_id="health-non-binotel-stale",
            ai_status=CallRecord.AiStatus.RUNNING,
            ai_locked_at=timezone.now() - timedelta(minutes=20),
        )
        CallRecord.objects.create(
            provider="aggregate",
            external_call_id="health-non-binotel-error",
            ai_status=CallRecord.AiStatus.ERROR,
        )

        snapshot = release_queue_snapshot()

        self.assertEqual(snapshot["binotel_eligible_pending"], 1)
        self.assertEqual(snapshot["binotel_metadata_pending"], 1)
        self.assertEqual(snapshot["binotel_ineligible_pending"], 1)
        self.assertEqual(snapshot["binotel_stale_running"], 1)
        self.assertEqual(snapshot["binotel_error"], 1)
        self.assertEqual(snapshot["dangerous_backlog"], 4)
        response = self.client.get(
            "/bot/health/", HTTP_HOST="management.twocomms.shop", secure=True
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["queues"]["binotel_stale_running"], 1)

    @patch(
        "management.models.InstagramBotMessage.objects.filter",
        side_effect=DatabaseError("queue unavailable"),
    )
    def test_release_snapshot_database_fallback_preserves_complete_shape(self, _filter):
        self.assertEqual(
            release_queue_snapshot(),
            {
                "available": False,
                "dangerous_backlog": 0,
                "inbound_pending": 0,
                "legacy_inbound_pending": 0,
                "revision_owned_pending": 0,
                "revision_owned_pending_reason": "preserved_revision_debt_evidence",
                "reply_pending": 0,
                "notification_unresolved": 0,
                "analysis_pending": 0,
                "recovery_unresolved": 0,
                "analysis_failed": 0,
                "binotel_eligible_pending": 0,
                "binotel_metadata_pending": 0,
                "binotel_ineligible_pending": 0,
                "binotel_stale_running": 0,
                "binotel_error": 0,
                "error": "DatabaseError",
            },
        )
