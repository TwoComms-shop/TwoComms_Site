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

from management.models import IgBotNotification, IgBotNotificationAudit
from management.services import instagram_bot as bot


User = get_user_model()
MGMT = override_settings(ROOT_URLCONF="twocomms.urls_management")


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
