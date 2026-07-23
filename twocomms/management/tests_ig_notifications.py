"""Durable Telegram notification idempotency tests."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import IgBotNotification
from management.services import instagram_bot as bot


class InstagramBotNotificationTests(TestCase):
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
