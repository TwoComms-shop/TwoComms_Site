from datetime import timedelta
from io import StringIO
import time
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from management.models import InstagramBotTaskHeartbeat
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


class PeriodicCoordinatorTests(TestCase):
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
