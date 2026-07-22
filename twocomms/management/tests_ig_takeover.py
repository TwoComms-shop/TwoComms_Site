"""Regression coverage for manager takeover alert epochs."""

from unittest.mock import patch

from django.test import TestCase

from management.models import IgClient, InstagramBotMessage
from management.services import instagram_bot as bot


class ManagerTakeoverAlertTests(TestCase):
    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.bot_followups.cancel_pending")
    @patch("management.services.bot_sales_classifier.classify_message")
    def test_repeated_manager_messages_emit_one_alert_per_takeover_epoch(
        self, classify_message, cancel_pending, notify
    ):
        client = IgClient.get_or_create_for_sender("takeover-epoch")

        for text in ["Первое сообщение", "Уточню размер", "Есть в наличии", "Надішлю реквізити", "Дякую"]:
            bot._handle_echo(client.igsid, text)

        self.assertEqual(notify.call_count, 1)
        self.assertEqual(
            InstagramBotMessage.objects.filter(
                client=client, role=InstagramBotMessage.Role.MANAGER
            ).count(),
            5,
        )

        client.refresh_from_db()
        self.assertTrue(client.manager_takeover)
        self.assertTrue(client.bot_paused)

        # Explicit operator resume closes the epoch. A later manager message
        # must create exactly one new transition notification.
        client.manager_takeover = False
        client.bot_paused = False
        client.paused_reason = ""
        client.save(update_fields=["manager_takeover", "bot_paused", "paused_reason", "updated_at"])

        bot._handle_echo(client.igsid, "Новий менеджерський меседж")

        self.assertEqual(notify.call_count, 2)
        self.assertEqual(
            InstagramBotMessage.objects.filter(
                client=client, role=InstagramBotMessage.Role.MANAGER
            ).count(),
            6,
        )
