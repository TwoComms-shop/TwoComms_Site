"""Durable Telegram notification idempotency tests."""

import json
from unittest.mock import patch

from django.test import TestCase

from management.models import IgBotNotification
from management.services import instagram_bot as bot


class InstagramBotNotificationTests(TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_missing_telegram_credentials_are_recorded(self):
        self.assertFalse(bot.notify_manager("Немає credentials", dedupe_key="missing-credentials"))
        row = IgBotNotification.objects.get(dedupe_key="missing-credentials")
        self.assertEqual(row.status, IgBotNotification.Status.FAILED)
        self.assertEqual(row.last_error, "telegram_not_configured")

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

        self.assertTrue(bot.notify_manager("Повторити", dedupe_key="retry-key"))
        row.refresh_from_db()
        self.assertEqual(row.status, IgBotNotification.Status.SENT)
        self.assertEqual(row.attempts, 2)
        self.assertEqual(http.call_count, 2)
