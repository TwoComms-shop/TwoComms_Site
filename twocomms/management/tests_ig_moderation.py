"""Тести Phase 7 (Tasks 20-22) — стоп/старт, перехоплення менеджером, антиспам."""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from management.models import IgClient
from management.services import instagram_bot as bot


class BlockedGateTests(TestCase):
    def test_paused_blocked(self):
        c = IgClient.get_or_create_for_sender("b1")
        c.bot_paused = True
        c.save()
        self.assertTrue(bot._client_blocked(c))

    def test_hidden_client_blocked(self):
        from django.utils import timezone

        c = IgClient.get_or_create_for_sender("b_hidden")
        c.hidden_at = timezone.now()
        c.save(update_fields=["hidden_at", "updated_at"])
        self.assertTrue(bot._client_blocked(c))

    def test_normal_not_blocked(self):
        c = IgClient.get_or_create_for_sender("b2")
        self.assertFalse(bot._client_blocked(c))


class SpamStrikeTests(TestCase):
    @patch("management.services.instagram_bot.notify_manager")
    def test_three_strikes_pause_and_stage(self, mock_notify):
        c = IgClient.get_or_create_for_sender("sp1")
        self.assertFalse(bot._register_spam(c))
        self.assertFalse(bot._register_spam(c))
        self.assertTrue(bot._register_spam(c))  # 3-й — блок
        c.refresh_from_db()
        self.assertTrue(c.bot_paused)
        self.assertEqual(c.stage, IgClient.Stage.SPAM)
        self.assertTrue(mock_notify.called)


class PhoneCaptureTests(TestCase):
    def test_captures_phone(self):
        c = IgClient.get_or_create_for_sender("ph1")
        self.assertTrue(bot._maybe_capture_phone(c, "мій номер 0931112233, дякую"))
        c.refresh_from_db()
        self.assertTrue(c.phone_normalized.startswith("+380"))

    def test_does_not_overwrite(self):
        c = IgClient.get_or_create_for_sender("ph2")
        c.phone = "+380501112233"
        c.save()
        self.assertFalse(bot._maybe_capture_phone(c, "0931112233"))
        c.refresh_from_db()
        self.assertEqual(c.phone, "+380501112233")

    def test_no_phone_no_capture(self):
        c = IgClient.get_or_create_for_sender("ph3")
        self.assertFalse(bot._maybe_capture_phone(c, "просто привіт"))


class EchoTakeoverTests(TestCase):
    @patch("management.services.instagram_bot.notify_manager")
    def test_manager_echo_pauses(self, mock_notify):
        cache.clear()
        c = IgClient.get_or_create_for_sender("eo1")
        bot._handle_echo("eo1", "Вітаю, це Іван, менеджер TwoComms")
        c.refresh_from_db()
        self.assertTrue(c.bot_paused)
        self.assertTrue(c.manager_takeover)
        self.assertTrue(mock_notify.called)

    @patch("management.services.instagram_bot.notify_manager")
    def test_bot_own_echo_ignored(self, mock_notify):
        cache.clear()
        c = IgClient.get_or_create_for_sender("eo2")
        bot._mark_bot_sent("eo2", "Ваше замовлення прийнято, дякуємо!")
        bot._handle_echo("eo2", "Ваше замовлення прийнято, дякуємо!")
        c.refresh_from_db()
        self.assertFalse(c.bot_paused)
        self.assertFalse(c.manager_takeover)
