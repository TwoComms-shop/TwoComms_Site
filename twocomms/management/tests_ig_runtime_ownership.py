from datetime import timedelta
from io import StringIO
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from management.models import IgClient, IgFollowUpTask, InstagramBotMessage, InstagramBotSettings, InstagramBotTaskHeartbeat
from management.services.ig_runtime_ownership import (
    DAEMON_OWNER,
    PERIODIC_LANES,
    PERIODIC_OWNER,
    PeriodicLane,
    RUNTIME_LANE_OWNERS,
    lane_owner,
    validate_runtime_lane_owners,
)
from management.management.commands.run_instagram_periodic_jobs import due_periodic_lanes


class RuntimeOwnerManifestTests(TestCase):
    def test_every_lane_has_exactly_one_owner(self):
        validate_runtime_lane_owners()
        lanes = [entry.lane for entry in RUNTIME_LANE_OWNERS]
        self.assertEqual(len(lanes), len(set(lanes)))
        self.assertEqual(lane_owner("ig_deal_payments"), PERIODIC_OWNER)
        self.assertEqual(lane_owner("nova_poshta_tracking"), PERIODIC_OWNER)
        self.assertEqual(lane_owner("live_reply"), DAEMON_OWNER)

    def test_periodic_manifest_keeps_all_previous_business_lanes(self):
        self.assertEqual(
            {lane.task_key for lane in PERIODIC_LANES},
            {
                "manager_notification_backstop",
                "nova_poshta_tracking",
                "order_telegram_reconcile",
                "ig_checkout_reconcile",
                "ig_order_fulfillment",
                "ig_deal_payments",
                "binotel_call_ai_analyses",
            },
        )
        self.assertLessEqual(
            sum(lane.deadline_seconds for lane in PERIODIC_LANES),
            660,
        )
        tracking = next(lane for lane in PERIODIC_LANES if lane.task_key == "nova_poshta_tracking")
        self.assertEqual((tracking.command, tracking.interval_seconds, tracking.deadline_seconds),
                         ("update_tracking_statuses", 300, 120))

    def test_notification_drain_has_an_owner_outside_the_daemon(self):
        """ЭА.16: алерт про мертвий демон не може залежати від того ж демона.

        Смуга демона лишається — вона швидка. Але якщо власник drain-у ТІЛЬКИ
        демон, то повідомлення «демон не працює» доставляти нікому: єдиний
        доставник і є той, хто помер. Тест закріплює, що backstop існує і що він
        не належить демону.
        """
        self.assertEqual(lane_owner("manager_notification_outbox"), DAEMON_OWNER)
        self.assertEqual(
            lane_owner("manager_notification_backstop"), PERIODIC_OWNER
        )
        backstop = next(
            lane for lane in PERIODIC_LANES
            if lane.task_key == "manager_notification_backstop"
        )
        self.assertEqual(backstop.command, "drain_ig_notifications")

    def test_notification_backstop_runs_before_the_repair_lanes(self):
        """Порядок не косметичний: backstop останнім спрацював би надто пізно."""
        keys = [lane.task_key for lane in PERIODIC_LANES]
        self.assertEqual(keys[0], "manager_notification_backstop")
        self.assertEqual(keys[1], "nova_poshta_tracking")


class PeriodicCoordinatorTests(TransactionTestCase):
    # A real cron command rotates connections with close_old_connections().
    # TestCase's synthetic outer atomic would close SQLite inside that atomic;
    # use committed fixtures so rotation exercises the production boundary.
    @patch(
        "management.management.commands.run_instagram_periodic_jobs._call_auto_analysis_enabled",
        return_value=False,
    )
    def test_tracking_is_due_after_five_minutes_and_prioritized_over_older_repairs(self, _enabled):
        now = timezone.now()
        InstagramBotTaskHeartbeat.objects.create(
            task_key="nova_poshta_tracking",
            label="tracking",
            expected_interval_seconds=300,
            stale_after_seconds=900,
            last_started_at=now - timedelta(seconds=301),
        )
        InstagramBotTaskHeartbeat.objects.create(
            task_key="ig_checkout_reconcile",
            label="checkout",
            expected_interval_seconds=120,
            stale_after_seconds=480,
            last_started_at=now - timedelta(hours=1),
        )
        due = due_periodic_lanes(now=now)
        keys = [lane.task_key for lane in due]
        self.assertEqual(keys[:2], ["manager_notification_backstop", "nova_poshta_tracking"])
        self.assertIn("ig_checkout_reconcile", keys)

    @patch(
        "management.management.commands.run_instagram_periodic_jobs._call_auto_analysis_enabled",
        return_value=False,
    )
    @patch("management.management.commands.run_instagram_periodic_jobs.call_command")
    def test_due_lanes_run_sequentially_without_optional_disabled_lane(
        self, child_command, _enabled
    ):
        call_command("run_instagram_periodic_jobs", stdout=StringIO())

        self.assertEqual(
            [call.args[0] for call in child_command.call_args_list],
            [
                "drain_ig_notifications",
                "update_tracking_statuses",
                "reconcile_order_telegram_notifications",
                "reconcile_ig_checkout",
                "reconcile_ig_order_fulfillment",
                "poll_ig_deal_payments",
            ],
        )

    @patch(
        "management.management.commands.run_instagram_periodic_jobs._call_auto_analysis_enabled",
        return_value=False,
    )
    @patch("management.management.commands.run_instagram_periodic_jobs.call_command")
    def test_recent_heartbeat_prevents_duplicate_lane_run(self, child_command, _enabled):
        now = timezone.now()
        InstagramBotTaskHeartbeat.objects.create(
            task_key="ig_checkout_reconcile",
            label="checkout",
            expected_interval_seconds=120,
            stale_after_seconds=480,
            last_started_at=now,
        )
        for lane in PERIODIC_LANES:
            if lane.task_key in {"ig_checkout_reconcile", "binotel_call_ai_analyses"}:
                continue
            InstagramBotTaskHeartbeat.objects.create(
                task_key=lane.task_key,
                label=lane.task_key,
                expected_interval_seconds=lane.interval_seconds,
                stale_after_seconds=lane.interval_seconds * 3,
                last_started_at=now - timedelta(seconds=lane.interval_seconds + 1),
            )

        call_command("run_instagram_periodic_jobs", stdout=StringIO())

        self.assertNotIn(
            "reconcile_ig_checkout",
            [call.args[0] for call in child_command.call_args_list],
        )

    @patch(
        "management.management.commands.run_instagram_periodic_jobs._call_auto_analysis_enabled",
        return_value=False,
    )
    @patch("management.management.commands.run_instagram_periodic_jobs.call_command")
    def test_one_lane_failure_does_not_starve_following_lanes(
        self, child_command, _enabled
    ):
        # Падає ПЕРША смуга, і саме її ім'я мусить бути у помилці; решта смуг
        # усе одно виконується — інакше один збій голодував би всі наступні.
        child_command.side_effect = [RuntimeError("private"), None, None, None, None, None]

        with self.assertRaisesMessage(
            CommandError, "manager_notification_backstop:RuntimeError"
        ):
            call_command("run_instagram_periodic_jobs", stdout=StringIO())

        self.assertEqual(child_command.call_count, 6)

    def test_force_requires_an_explicit_lane(self):
        with self.assertRaisesMessage(CommandError, "--force requires --lane"):
            call_command("run_instagram_periodic_jobs", force=True, stdout=StringIO())

    @patch(
        "management.services.call_auto_analysis.is_call_auto_analysis_enabled",
        side_effect=RuntimeError("toggle unavailable"),
    )
    @patch("management.management.commands.run_instagram_periodic_jobs.call_command")
    def test_optional_gate_failure_does_not_starve_critical_lanes(
        self, child_command, _enabled
    ):
        call_command("run_instagram_periodic_jobs", stdout=StringIO())

        self.assertEqual(child_command.call_count, 6)
        self.assertNotIn(
            "run_call_ai_analyses",
            [call.args[0] for call in child_command.call_args_list],
        )

    @patch(
        "management.management.commands.run_instagram_periodic_jobs._call_auto_analysis_enabled",
        return_value=False,
    )
    @patch("management.management.commands.run_instagram_periodic_jobs.call_command")
    def test_hung_lane_times_out_and_next_due_lane_still_runs(
        self, child_command, _enabled
    ):
        lanes = (
            PeriodicLane(
                "ig_checkout_reconcile",
                "reconcile_ig_checkout",
                120,
                1,
                (("limit", 100),),
            ),
            PeriodicLane(
                "ig_order_fulfillment",
                "reconcile_ig_order_fulfillment",
                120,
                2,
                (("limit", 100),),
            ),
        )

        def child(command, **_options):
            if command == "reconcile_ig_checkout":
                time.sleep(5)

        child_command.side_effect = child
        output = StringIO()
        started = time.monotonic()
        with (
            patch(
                "management.management.commands.run_instagram_periodic_jobs.PERIODIC_LANES",
                lanes,
            ),
            self.assertRaisesMessage(
                CommandError,
                "ig_checkout_reconcile:PeriodicLaneTimeout",
            ),
        ):
            call_command(
                "run_instagram_periodic_jobs",
                budget_seconds=10,
                stdout=output,
            )

        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(
            [call.args[0] for call in child_command.call_args_list],
            ["reconcile_ig_checkout", "reconcile_ig_order_fulfillment"],
        )
        self.assertIn("timed_out=ig_checkout_reconcile", output.getvalue())
        heartbeat = InstagramBotTaskHeartbeat.objects.get(
            task_key="ig_checkout_reconcile"
        )
        self.assertEqual(heartbeat.last_error_kind, "PeriodicLaneTimeout")


class PeriodicDiagnosticPurityTests(TestCase):
    def test_dry_run_never_repairs_debt_or_notifies_managers(self):
        with (patch("management.services.ig_revision_execution.reconcile_incomplete_revision_deliveries") as repair,
              patch("management.services.ig_human_reply_maintenance.maintain_human_reply_delivery") as human,
              patch("management.services.ig_daemon_health.alert_daemon_runtime_health") as alert,
              patch("management.management.commands.run_instagram_periodic_jobs.call_command") as lane):
            call_command("run_instagram_periodic_jobs", dry_run=True, stdout=StringIO())
        repair.assert_not_called()
        human.assert_not_called()
        alert.assert_not_called()
        lane.assert_not_called()


class PeriodicHumanMaintenanceIntegrationTests(TransactionTestCase):
    # The coordinator owns actual connection rotation, not a TestCase savepoint.
    def test_bot_off_cron_reaps_real_started_human_part_before_health_without_transport(self):
        from management.services.ig_human_reply import create_human_reply_command
        from management.services.ig_human_reply_delivery import claim_next_human_part, record_human_part_started

        now = timezone.now() - timedelta(minutes=3)
        actor = get_user_model().objects.create_superuser(username="periodic-human-owner", password="x")
        InstagramBotSettings.objects.create(pk=1, is_enabled=False, ig_user_id="periodic-human", page_id="periodic-human")
        customer = IgClient.objects.create(igsid="periodic-human-customer")
        source = InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid,
            provider_namespace="instagram_login:periodic-human", role="user", source="webhook", status="done",
            mid="periodic-human-source", provider_created_at=now - timedelta(minutes=2), text="Підкажіть розмір")
        events = []
        with patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"}):
            command = create_human_reply_command(customer.pk, actor=actor, text="Вітаю!",
                context_message_id=source.pk, now=now).command
            claim = claim_next_human_part(command.pk, now=now)
            self.assertTrue(claim.ready, claim.reason)
            self.assertTrue(record_human_part_started(claim.part.pk, claim.token, now=now).ready)

            def revision(**options):
                self.assertEqual(options, {"limit": 25, "dry_run": False})
                claim.part.refresh_from_db()
                self.assertEqual(claim.part.state, "provider_started")
                events.append("revision")

            def health():
                claim.part.refresh_from_db()
                self.assertEqual(claim.part.state, "unknown")
                self.assertEqual(IgFollowUpTask.objects.get(event_key=f"human-reply-unknown:{command.pk}").status, "skipped")
                events.append("health")

            with (patch("management.services.ig_revision_execution.reconcile_incomplete_revision_deliveries", side_effect=revision),
                  patch("management.services.ig_daemon_health.alert_daemon_runtime_health", side_effect=health),
                  patch("management.management.commands.run_instagram_periodic_jobs._call_auto_analysis_enabled", return_value=False),
                  patch("management.management.commands.run_instagram_periodic_jobs.call_command") as child,
                  patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("cron provider I/O forbidden")) as http,
                  patch("management.services.instagram_bot.send_text", side_effect=AssertionError("cron customer send forbidden")) as send):
                call_command("run_instagram_periodic_jobs", stdout=StringIO())
            http.assert_not_called()
            send.assert_not_called()
        self.assertEqual(events, ["revision", "health"])
        self.assertEqual(child.call_count, 6)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_human_database_failure_uses_own_circuit_lane_and_stops_before_health(self):
        output = StringIO()
        error = OperationalError(2006, "private connection details")
        with (patch("management.services.ig_revision_execution.reconcile_incomplete_revision_deliveries"),
              patch("management.services.ig_human_reply_maintenance.maintain_human_reply_delivery", side_effect=error) as human,
              patch("management.services.ig_daemon_health.alert_daemon_runtime_health") as health,
              patch("management.services.ig_db_circuit.record_db_failure", return_value=True) as failure,
              patch("management.management.commands.run_instagram_periodic_jobs.call_command") as child):
            call_command("run_instagram_periodic_jobs", stdout=output)
        human.assert_called_once_with(limit=25)
        failure.assert_called_once_with(error, lane="periodic_human_delivery_debt")
        health.assert_not_called()
        child.assert_not_called()
        self.assertIn("deferred=db_circuit", output.getvalue())

    def test_finite_human_receipt_failure_does_not_starve_health_or_business_lanes(self):
        from management.services.ig_human_reply_transport import HumanReceiptCheckpointError

        stderr = StringIO()
        with (patch("management.services.ig_revision_execution.reconcile_incomplete_revision_deliveries"),
              patch("management.services.ig_human_reply_maintenance.maintain_human_reply_delivery",
                    side_effect=HumanReceiptCheckpointError("private payload details")),
              patch("management.services.ig_daemon_health.alert_daemon_runtime_health") as health,
              patch("management.services.ig_db_circuit.record_db_failure", return_value=False),
              patch("management.management.commands.run_instagram_periodic_jobs._call_auto_analysis_enabled", return_value=False),
              patch("management.management.commands.run_instagram_periodic_jobs.call_command") as child):
            call_command("run_instagram_periodic_jobs", stdout=StringIO(), stderr=stderr)
        health.assert_called_once()
        self.assertEqual(child.call_count, 6)
        self.assertIn("human_delivery_debt_reconcile_failed=HumanReceiptCheckpointError", stderr.getvalue())
        self.assertNotIn("private payload details", stderr.getvalue())
