"""Тести Phase 7 (Tasks 20-22) — стоп/старт, перехоплення менеджером, антиспам."""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
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

    @patch("management.services.instagram_bot.send_text", return_value=(True, "", ""))
    @patch("management.services.instagram_bot.gemini_generate", return_value="Тестова відповідь")
    @patch("management.services.instagram_bot.send_sender_action")
    def test_worker_refetches_hidden_state_after_claim_before_send(
        self, _sender_action, generate_reply, send_text
    ):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.save(update_fields=["is_enabled"])
        client = IgClient.get_or_create_for_sender("claimed_then_hidden")
        client.profile_fetched_at = timezone.now()
        client.save(update_fields=["profile_fetched_at", "updated_at"])
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="потрібна відповідь",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        self.assertIsNone(row.client.hidden_at)  # кешуємо стан до конкурентного hide.
        IgClient.objects.filter(pk=client.pk).update(hidden_at=timezone.now())

        handled = bot._process_one(settings, row)

        self.assertFalse(handled)
        generate_reply.assert_not_called()
        send_text.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.DONE)

    @patch("management.services.instagram_bot.send_text", return_value=(True, "", ""))
    @patch("management.services.instagram_bot.send_sender_action")
    def test_worker_rechecks_hidden_state_after_generation_before_send(
        self, _sender_action, send_text
    ):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.save(update_fields=["is_enabled"])
        client = IgClient.get_or_create_for_sender("hidden_during_generation")
        client.profile_fetched_at = timezone.now()
        client.save(update_fields=["profile_fetched_at", "updated_at"])
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="потрібна відповідь",
            status=InstagramBotMessage.Status.PROCESSING,
        )

        def hide_then_reply(*_args, **_kwargs):
            IgClient.objects.filter(pk=client.pk).update(hidden_at=timezone.now())
            return "Тестова відповідь"

        with patch("management.services.instagram_bot.gemini_generate", side_effect=hide_then_reply):
            handled = bot._process_one(settings, row)

        self.assertFalse(handled)
        send_text.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.DONE)

    @patch("management.services.instagram_bot.send_text", return_value=(True, "", ""))
    @patch("management.services.instagram_bot.gemini_generate", return_value="Тестова відповідь")
    @patch("management.services.instagram_bot.send_sender_action")
    def test_post_send_error_never_requeues_a_delivered_message(
        self, _sender_action, _generate_reply, send_text
    ):
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.save(update_fields=["is_enabled"])
        client = IgClient.get_or_create_for_sender("delivered_message")
        client.profile_fetched_at = timezone.now()
        client.save(update_fields=["profile_fetched_at", "updated_at"])
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="відправлене повідомлення",
            status=InstagramBotMessage.Status.PENDING,
        )

        with patch.object(InstagramBotSettings, "save", side_effect=RuntimeError("settings write failed")):
            bot.process_pending(settings, max_items=1)

        send_text.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.status, InstagramBotMessage.Status.DONE)


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
