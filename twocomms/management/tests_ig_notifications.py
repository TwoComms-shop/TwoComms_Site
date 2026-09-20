"""Durable Telegram notification idempotency tests."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgBotNotification,
    IgBotNotificationAudit,
    InstagramBotTaskHeartbeat,
)
from management.services import instagram_bot as bot


User = get_user_model()
MGMT = override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    SECURE_SSL_REDIRECT=False,
)


class InstagramBotNotificationTests(TestCase):
    @patch("management.services.ig_maintenance.maintenance_status", return_value={"active": True})
    @patch("management.services.instagram_bot._http")
    def test_maintenance_does_not_claim_or_send_notification(self, http, _maintenance):
        row = IgBotNotification.objects.create(
            dedupe_key="maintenance-pending",
            payload={"text": "Не надсилати", "chat_id": "123"},
        )
        self.assertFalse(bot._deliver_manager_notification(row.dedupe_key))
        row.refresh_from_db()
        self.assertEqual(row.status, IgBotNotification.Status.PENDING)
        self.assertEqual(row.attempts, 0)
        http.assert_not_called()

    @patch(
        "management.management.commands.drain_ig_notifications.maintenance_status",
        return_value={"active": True},
    )
    def test_manual_drain_command_refuses_during_maintenance(self, _maintenance):
        with self.assertRaisesMessage(CommandError, "notification drain refused"):
            call_command("drain_ig_notifications")

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_telegram_credentials_are_recorded(self):
        self.assertFalse(bot.notify_manager("Немає credentials", dedupe_key="missing-credentials"))
        row = IgBotNotification.objects.get(dedupe_key="missing-credentials")
        self.assertEqual(row.status, IgBotNotification.Status.FAILED)
        self.assertEqual(row.last_error, "telegram_not_configured")
        self.assertEqual(row.failure_kind, "configuration")
        self.assertIsNotNone(row.next_attempt_at)

    @patch.dict(
        "os.environ",
        {
            "MANAGEMENT_TG_BOT_TOKEN": "test-token",
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "123",
        },
        clear=False,
    )
    @patch("management.services.instagram_bot._http", return_value=(200, json.dumps({"ok": True, "result": {"message_id": 77}})))
    def test_same_dedupe_key_sends_once_and_records_success(self, http):
        bot.notify_manager("Менеджер підключився", dedupe_key="takeover:client:epoch-1", event_type="takeover")
        bot.notify_manager("Менеджер підключився", dedupe_key="takeover:client:epoch-1", event_type="takeover")

        self.assertEqual(http.call_count, 1)
        row = IgBotNotification.objects.get(dedupe_key="takeover:client:epoch-1")
        self.assertEqual(row.status, IgBotNotification.Status.SENT)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.telegram_message_id, "77")

    @patch.dict(
        "os.environ",
        {
            "MANAGEMENT_TG_BOT_TOKEN": "test-token",
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "123",
        },
        clear=False,
    )
    @patch("management.services.instagram_bot._http", return_value=(200, json.dumps({"ok": True, "result": {"message_id": 88}})))
    def test_inline_keyboard_is_persisted_and_sent(self, http):
        keyboard = {"inline_keyboard": [[{"text": "Перейти до підтвердження", "url": "https://management.twocomms.shop/bot/?payment_review=42"}]]}
        self.assertTrue(bot.notify_manager("Перевірка", dedupe_key="payment-review-button", reply_markup=keyboard))
        payload = json.loads(http.call_args.kwargs["data"])
        self.assertEqual(payload["reply_markup"], keyboard)
        self.assertEqual(IgBotNotification.objects.get(dedupe_key="payment-review-button").payload["reply_markup"], keyboard)

    def test_payment_review_is_explicitly_actionable_for_terminal_monitoring(self):
        with patch(
            "management.services.instagram_bot._deliver_manager_notification",
            return_value=True,
        ):
            bot.notify_manager(
                "Перевірка оплати",
                dedupe_key="payment-review-actionable",
                event_type="payment_review",
                metadata={"requires_human_review": True},
            )

        row = IgBotNotification.objects.get(dedupe_key="payment-review-actionable")
        self.assertTrue(row.payload["requires_human_review"])

    @patch.dict(
        "os.environ",
        {
            "MANAGEMENT_TG_BOT_TOKEN": "test-token",
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "123",
        },
        clear=False,
    )
    @patch("management.services.instagram_bot._http", return_value=(200, json.dumps({"ok": True, "result": {"message_id": 89}})))
    def test_notification_persists_media_evidence_for_separate_delivery(self, http):
        media = [
            {"role": "product", "url": "https://cdn.example/product.jpg"},
            {"role": "receipt", "local_url": "https://management.example/media/receipt.jpg"},
        ]
        self.assertTrue(bot.notify_manager("Перевірка", dedupe_key="payment-review-media", media=media))
        payload = IgBotNotification.objects.get(dedupe_key="payment-review-media").payload
        self.assertEqual(
            [(item["role"], item.get("url"), item.get("local_url")) for item in payload["media"]],
            [
                ("product", "https://cdn.example/product.jpg", None),
                ("receipt", None, "https://management.example/media/receipt.jpg"),
            ],
        )
        self.assertTrue(all(item["delivery_status"] == "sent" for item in payload["media"]))
        self.assertTrue(all(item["delivery_message_id"] == "89" for item in payload["media"]))
        self.assertGreaterEqual(http.call_count, 3)

    @patch.dict(
        "os.environ",
        {"MANAGEMENT_TG_BOT_TOKEN": "test-token", "MANAGEMENT_TG_ADMIN_CHAT_ID": "123"},
        clear=False,
    )
    @patch(
        "management.services.instagram_bot._http",
        side_effect=[
            (200, json.dumps({"ok": True, "result": {"message_id": 90}})),
            (503, json.dumps({"ok": False, "description": "temporary photo failure"})),
            (200, json.dumps({"ok": True, "result": {"message_id": 91}})),
        ],
    )
    def test_failed_photo_is_retried_without_duplicate_main_alert(self, http):
        media = [{"role": "receipt", "url": "https://cdn.example/receipt.jpg", "message_id": "12"}]
        self.assertFalse(bot.notify_manager("Перевірка", dedupe_key="payment-photo-retry", media=media))
        row = IgBotNotification.objects.get(dedupe_key="payment-photo-retry")
        self.assertEqual(row.status, IgBotNotification.Status.FAILED)
        self.assertEqual(row.payload["main_delivery_message_id"], "90")

        row.next_attempt_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["next_attempt_at"])
        self.assertEqual(bot.drain_manager_notifications(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, IgBotNotification.Status.SENT)
        urls = [call.args[0] for call in http.call_args_list]
        self.assertEqual(sum(url.endswith("/sendMessage") for url in urls), 1)
        self.assertEqual(sum(url.endswith("/sendPhoto") for url in urls), 2)
        self.assertEqual(row.payload["media"][0]["delivery_status"], "sent")

    @patch.dict(
        "os.environ",
        {"MANAGEMENT_TG_BOT_TOKEN": "test-token", "MANAGEMENT_TG_ADMIN_CHAT_ID": "123"},
        clear=False,
    )
    def test_atomic_claim_rejects_a_second_delivery_while_provider_call_is_in_flight(self):
        IgBotNotification.objects.create(
            dedupe_key="claim-race",
            payload={"text": "Один виклик", "chat_id": "123"},
        )
        calls = []

        def provider_call(*args, **kwargs):
            calls.append("provider")
            self.assertFalse(bot._deliver_manager_notification("claim-race"))
            return 200, json.dumps({"ok": True, "result": {"message_id": 79}})

        with patch("management.services.instagram_bot._http", side_effect=provider_call):
            self.assertTrue(bot._deliver_manager_notification("claim-race"))

        self.assertEqual(calls, ["provider"])
        row = IgBotNotification.objects.get(dedupe_key="claim-race")
        self.assertEqual(row.status, IgBotNotification.Status.SENT)
        self.assertEqual(row.attempts, 1)

    @patch.dict(
        "os.environ",
        {
            "MANAGEMENT_TG_BOT_TOKEN": "test-token",
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "123",
        },
        clear=False,
    )
    @patch(
        "management.services.instagram_bot._http",
        side_effect=[
            (500, json.dumps({"ok": False, "description": "temporary"})),
            (200, json.dumps({"ok": True, "result": {"message_id": 78}})),
        ],
    )
    def test_failed_delivery_is_retryable_and_becomes_sent(self, http):
        self.assertFalse(bot.notify_manager("Повторити", dedupe_key="retry-key"))
        row = IgBotNotification.objects.get(dedupe_key="retry-key")
        self.assertEqual(row.status, IgBotNotification.Status.FAILED)
        self.assertEqual(row.attempts, 1)

        row.next_attempt_at = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["next_attempt_at"])
        self.assertEqual(bot.drain_manager_notifications(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, IgBotNotification.Status.SENT)
        self.assertEqual(row.attempts, 2)
        self.assertEqual(http.call_count, 2)

    @patch.dict(
        "os.environ",
        {"MANAGEMENT_TG_BOT_TOKEN": "test-token", "MANAGEMENT_TG_ADMIN_CHAT_ID": "123"},
        clear=False,
    )
    @patch("management.services.instagram_bot._http", return_value=(-1, "TimeoutError('socket timed out')"))
    def test_ambiguous_transport_failure_is_not_automatically_retried(self, http):
        self.assertFalse(bot.notify_manager("Можливо доставлено", dedupe_key="unknown-key"))
        row = IgBotNotification.objects.get(dedupe_key="unknown-key")
        self.assertEqual(row.status, IgBotNotification.Status.UNKNOWN)
        self.assertEqual(row.failure_kind, "ambiguous_transport")

        self.assertEqual(bot.drain_manager_notifications(), 0)
        self.assertEqual(http.call_count, 1)

    @patch.dict(
        "os.environ",
        {"MANAGEMENT_TG_BOT_TOKEN": "test-token", "MANAGEMENT_TG_ADMIN_CHAT_ID": "123"},
        clear=False,
    )
    @patch("management.services.instagram_bot._http")
    def test_stale_sending_is_quarantined_instead_of_resent(self, http):
        row = IgBotNotification.objects.create(
            dedupe_key="stale-key",
            payload={"text": "Невідомий результат", "chat_id": "123"},
            status=IgBotNotification.Status.SENDING,
            attempts=1,
            last_attempt_at=timezone.now() - timedelta(minutes=10),
        )

        self.assertEqual(bot.drain_manager_notifications(), 0)
        row.refresh_from_db()
        self.assertEqual(row.status, IgBotNotification.Status.UNKNOWN)
        self.assertEqual(row.failure_kind, "ambiguous_stale_sending")
        http.assert_not_called()

    def test_terminal_notifications_are_summarized_once_per_unresolved_set(self):
        pii_marker = "customer@example.com +380501112233 private-provider-body"
        IgBotNotification.objects.create(
            dedupe_key="terminal-unknown",
            event_type="delivery_unknown",
            payload={"text": "Невідомий результат", "requires_human_review": True},
            status=IgBotNotification.Status.UNKNOWN,
            last_error=f"timeout access_token=secret-value {pii_marker}",
        )
        IgBotNotification.objects.create(
            dedupe_key="terminal-dead",
            event_type="send_gave_up",
            payload={"text": "Вичерпано спроби", "requires_human_review": True},
            status=IgBotNotification.Status.DEAD_LETTER,
            last_error="retry_exhausted",
        )

        self.assertEqual(bot._monitor_terminal_notifications(force=True), 1)
        monitor = IgBotNotification.objects.get(event_type="notification_terminal_monitor")
        self.assertEqual(monitor.status, IgBotNotification.Status.PENDING)
        self.assertIn("UNKNOWN: 1", monitor.payload["text"])
        self.assertIn("DEAD_LETTER: 1", monitor.payload["text"])
        self.assertIn("/bot/", monitor.payload["text"])
        self.assertNotIn("secret-value", monitor.payload["text"])
        self.assertNotIn(pii_marker, monitor.payload["text"])
        self.assertNotIn("customer@example.com", monitor.payload["text"])
        self.assertNotIn("+380501112233", monitor.payload["text"])

        # The same unresolved rows never produce a periodic duplicate.
        self.assertEqual(bot._monitor_terminal_notifications(force=True), 1)
        self.assertEqual(
            IgBotNotification.objects.filter(event_type="notification_terminal_monitor").count(),
            1,
        )

        IgBotNotification.objects.create(
            dedupe_key="terminal-new-decision",
            event_type="discount_approval",
            payload={"text": "Підтвердити", "requires_human_review": True},
            status=IgBotNotification.Status.UNKNOWN,
        )
        self.assertEqual(bot._monitor_terminal_notifications(force=True), 1)
        self.assertEqual(
            IgBotNotification.objects.filter(event_type="notification_terminal_monitor").count(),
            2,
        )

    def test_terminal_monitor_excludes_self_healing_system_alerts(self):
        IgBotNotification.objects.create(
            dedupe_key="system-terminal",
            event_type="ig_task_failure",
            payload={"text": "Watchdog", "requires_human_review": False},
            status=IgBotNotification.Status.UNKNOWN,
        )

        self.assertEqual(bot._monitor_terminal_notifications(force=True), 0)
        self.assertFalse(
            IgBotNotification.objects.filter(
                event_type="notification_terminal_monitor"
            ).exists()
        )

    def test_terminal_monitor_keeps_legacy_actionable_events_visible(self):
        IgBotNotification.objects.create(
            dedupe_key="legacy-delivery-unknown",
            event_type="delivery_unknown",
            payload={"text": "Перевірити Meta Inbox"},
            status=IgBotNotification.Status.UNKNOWN,
        )

        self.assertEqual(bot._monitor_terminal_notifications(force=True), 1)
        monitor = IgBotNotification.objects.get(
            event_type="notification_terminal_monitor"
        )
        self.assertIn("delivery_unknown", monitor.payload["text"])

    def test_actionable_event_policy_is_applied_by_notifier(self):
        with patch(
            "management.services.instagram_bot._deliver_manager_notification",
            return_value=True,
        ):
            bot.notify_manager(
                "Потрібна ручна відповідь",
                dedupe_key="shipment-policy",
                event_type="shipment_human_review",
            )

        row = IgBotNotification.objects.get(dedupe_key="shipment-policy")
        self.assertTrue(row.payload["requires_human_review"])

    def test_terminal_monitor_reports_full_backlog_beyond_sample_limit(self):
        IgBotNotification.objects.bulk_create([
            IgBotNotification(
                dedupe_key=f"terminal-backlog-{index}",
                event_type="delivery_unknown",
                payload={"text": "Невідомий результат", "requires_human_review": True},
                status=IgBotNotification.Status.UNKNOWN,
            )
            for index in range(101)
        ])

        self.assertEqual(bot._monitor_terminal_notifications(force=True), 1)
        monitor = IgBotNotification.objects.get(event_type="notification_terminal_monitor")
        self.assertIn("UNKNOWN: 101", monitor.payload["text"])

    def test_terminal_monitor_sanitizes_persisted_event_metadata(self):
        marker = "terminal_PRIVATE_MONITOR_MARKER"
        IgBotNotification.objects.create(
            dedupe_key="terminal-private-marker",
            event_type=marker,
            status=IgBotNotification.Status.UNKNOWN,
            last_error=marker,
            payload={
                "text": marker,
                "chat_id": "123",
                "requires_human_review": True,
            },
        )

        self.assertEqual(bot._monitor_terminal_notifications(force=True), 1)
        monitor = IgBotNotification.objects.get(event_type="notification_terminal_monitor")
        self.assertNotIn(marker, monitor.payload["text"])
        self.assertIn("UNKNOWN", monitor.payload["text"])

    def test_recovered_task_failure_auto_resolves_new_and_legacy_alerts(self):
        now = timezone.now()
        heartbeat = InstagramBotTaskHeartbeat.objects.create(
            task_key="ig_daemon_watchdog",
            label="watchdog Instagram-демона",
            expected_interval_seconds=60,
            stale_after_seconds=180,
            last_failed_at=now,
            last_error_kind="CommandError",
        )
        new_style = IgBotNotification.objects.create(
            dedupe_key=f"ig_task_failure:e{heartbeat.pk}:w-new",
            event_type="ig_task_failure",
            payload={
                "text": "Watchdog failed",
                "task_key": heartbeat.task_key,
                "task_heartbeat_id": heartbeat.pk,
                "requires_human_review": False,
            },
            status=IgBotNotification.Status.UNKNOWN,
        )
        legacy = IgBotNotification.objects.create(
            dedupe_key=f"ig_task_failure:e{heartbeat.pk}:w-legacy",
            event_type="ig_task_failure",
            payload={"text": "Legacy watchdog failure"},
            status=IgBotNotification.Status.UNKNOWN,
        )
        degraded = IgBotNotification.objects.create(
            dedupe_key=f"ig_task_degraded:e{heartbeat.pk}:w-provider",
            event_type="ig_task_degraded",
            payload={
                "text": "Nova Poshta degraded",
                "task_key": heartbeat.task_key,
                "task_heartbeat_id": heartbeat.pk,
                "requires_human_review": False,
            },
            status=IgBotNotification.Status.UNKNOWN,
        )
        heartbeat.last_succeeded_at = now + timedelta(minutes=1)
        heartbeat.consecutive_failures = 0
        heartbeat.last_error_kind = ""
        heartbeat.save(update_fields=[
            "last_succeeded_at", "consecutive_failures", "last_error_kind", "updated_at"
        ])

        self.assertEqual(bot.reconcile_recovered_system_notifications(), 3)

        new_style.refresh_from_db()
        legacy.refresh_from_db()
        degraded.refresh_from_db()
        self.assertEqual(new_style.status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(legacy.status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(degraded.status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(
            IgBotNotificationAudit.objects.filter(action="auto_recovered").count(),
            3,
        )

    def test_obsolete_queued_terminal_monitor_is_resolved_before_delivery(self):
        monitor = IgBotNotification.objects.create(
            dedupe_key="ig-notification-terminal:legacy-hour-bucket",
            event_type="notification_terminal_monitor",
            payload={"text": "Старий monitor", "requires_human_review": False},
            status=IgBotNotification.Status.PENDING,
        )

        self.assertEqual(bot.reconcile_obsolete_terminal_monitors(), 1)

        monitor.refresh_from_db()
        self.assertEqual(monitor.status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(monitor.failure_kind, "terminal_monitor_obsolete")
        self.assertTrue(
            IgBotNotificationAudit.objects.filter(
                notification=monitor,
                action="terminal_monitor_obsolete",
            ).exists()
        )

    @patch.dict(
        "os.environ",
        {"MANAGEMENT_TG_BOT_TOKEN": "test-token", "MANAGEMENT_TG_ADMIN_CHAT_ID": "123"},
        clear=False,
    )
    @patch(
        "management.services.instagram_bot._http",
        return_value=(503, json.dumps({"ok": False, "description": "temporary"})),
    )
    def test_retry_budget_moves_notification_to_dead_letter(self, http):
        self.assertFalse(bot.notify_manager("П'ять спроб", dedupe_key="exhausted-key"))
        row = IgBotNotification.objects.get(dedupe_key="exhausted-key")
        for _ in range(bot.NOTIFICATION_MAX_ATTEMPTS - 1):
            row.next_attempt_at = timezone.now() - timedelta(seconds=1)
            row.save(update_fields=["next_attempt_at"])
            bot.drain_manager_notifications()
            row.refresh_from_db()

        self.assertEqual(row.status, IgBotNotification.Status.DEAD_LETTER)
        self.assertEqual(row.failure_kind, "retry_exhausted")
        self.assertIsNone(row.next_attempt_at)
        self.assertEqual(row.attempts, bot.NOTIFICATION_MAX_ATTEMPTS)
        self.assertEqual(http.call_count, bot.NOTIFICATION_MAX_ATTEMPTS)

    @patch.dict(
        "os.environ",
        {"MANAGEMENT_TG_BOT_TOKEN": "test-token", "MANAGEMENT_TG_ADMIN_CHAT_ID": "123"},
        clear=False,
    )
    @patch(
        "management.services.instagram_bot._http",
        return_value=(429, json.dumps({
            "ok": False,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 7200},
        })),
    )
    def test_rate_limit_honours_provider_retry_after(self, http):
        before = timezone.now()
        self.assertFalse(bot.notify_manager("Почекати", dedupe_key="rate-limit-key"))

        row = IgBotNotification.objects.get(dedupe_key="rate-limit-key")
        self.assertEqual(row.status, IgBotNotification.Status.FAILED)
        self.assertEqual(row.failure_kind, "rate_limited")
        self.assertGreaterEqual(row.next_attempt_at, before + timedelta(seconds=7200))
        self.assertLess(row.next_attempt_at, before + timedelta(seconds=7220))
        self.assertEqual(row.attempts, 1)
        http.assert_called_once()

    def test_retry_schedule_bounds_malformed_and_extreme_provider_delays(self):
        row = IgBotNotification(dedupe_key="retry-bounds", attempts=1)
        now = timezone.now()

        malformed = bot._notification_retry_at(row, now, minimum_delay_seconds="bad")
        missing = bot._notification_retry_at(row, now, minimum_delay_seconds=None)
        numeric_string = bot._notification_retry_at(row, now, minimum_delay_seconds="120")
        negative = bot._notification_retry_at(row, now, minimum_delay_seconds=-500)
        extreme = bot._notification_retry_at(row, now, minimum_delay_seconds=999999999)

        self.assertGreaterEqual(malformed, now + timedelta(seconds=30))
        self.assertGreaterEqual(missing, now + timedelta(seconds=30))
        self.assertGreaterEqual(numeric_string, now + timedelta(seconds=120))
        self.assertLess(numeric_string, now + timedelta(seconds=140))
        self.assertGreaterEqual(negative, now + timedelta(seconds=30))
        self.assertGreaterEqual(extreme, now + timedelta(seconds=86400))
        self.assertLess(extreme, now + timedelta(seconds=86420))


@MGMT
class InstagramBotNotificationReviewApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("notif-admin", password="x", is_staff=True)
        self.client.force_login(self.admin)
        self.row = IgBotNotification.objects.create(
            dedupe_key="manual-review",
            event_type="takeover",
            payload={"text": "Перевірте access_token=secret-value\nдругий рядок", "chat_id": "123"},
            status=IgBotNotification.Status.UNKNOWN,
            attempts=1,
            failure_kind="ambiguous_transport",
            last_error="request token=secret-value failed",
        )

    def test_review_list_is_sanitized_and_ordered(self):
        newer = IgBotNotification.objects.create(
            dedupe_key="manual-review-newer",
            event_type="shipment_human_review",
            payload={"text": "Друге"},
            status=IgBotNotification.Status.DEAD_LETTER,
        )
        response = self.client.get(reverse("management_bot_notification_review_api"))

        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([item["id"] for item in items], [self.row.id, newer.id])
        item = items[0]
        self.assertEqual(item["id"], self.row.id)
        self.assertNotIn("secret-value", item["text_preview"])
        self.assertNotIn("secret-value", item["error"])
        self.assertNotIn("chat_id", item)

    def test_resolve_is_audited_and_cannot_be_repeated(self):
        url = reverse("management_bot_notification_review_action_api", args=[self.row.id])
        response = self.client.post(url, {"action": "resolve", "note": "Перевірено вручну"})

        self.assertEqual(response.status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, IgBotNotification.Status.RESOLVED)
        audit = IgBotNotificationAudit.objects.get(notification=self.row)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.from_status, IgBotNotification.Status.UNKNOWN)
        self.assertEqual(audit.to_status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(self.client.post(url, {"action": "resolve"}).status_code, 409)

    def test_requeue_is_explicit_audited_and_resets_retry_budget(self):
        self.row.status = IgBotNotification.Status.DEAD_LETTER
        self.row.attempts = 5
        self.row.save(update_fields=["status", "attempts"])
        url = reverse("management_bot_notification_review_action_api", args=[self.row.id])

        response = self.client.post(url, {"action": "requeue"})

        self.assertEqual(response.status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, IgBotNotification.Status.PENDING)
        self.assertEqual(self.row.attempts, 0)
        self.assertIsNotNone(self.row.next_attempt_at)
        self.assertTrue(IgBotNotificationAudit.objects.filter(
            notification=self.row,
            action="requeue",
            to_status=IgBotNotification.Status.PENDING,
        ).exists())
        self.assertEqual(self.client.post(url, {"action": "requeue"}).status_code, 409)

    def test_review_endpoints_require_staff(self):
        self.client.logout()
        self.client.force_login(User.objects.create_user("notif-user", password="x"))

        self.assertEqual(self.client.get(reverse("management_bot_notification_review_api")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("management_bot_notification_review_action_api", args=[self.row.id]),
                {"action": "resolve"},
            ).status_code,
            403,
        )

    def test_review_action_requires_csrf(self):
        csrf_client = self.client_class(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)

        response = csrf_client.post(
            reverse("management_bot_notification_review_action_api", args=[self.row.id]),
            {"action": "resolve"},
        )

        self.assertEqual(response.status_code, 403)
