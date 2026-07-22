"""Тести Phase 9 / Task 24 — розподіл ролей моделей + cost/rate-гард.

Живий діалог — роль 'chat' (швидка 3.5→3.1). Важке (матчинг по фото, пам'ять,
витяг даних) — роль 'management'. Матчинг має ліміт на клієнта (захист квоти).
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from management.models import InstagramBotSettings
from management.services import bot_vision, instagram_bot as bot


class RoleSeparationTests(TestCase):
    @patch("management.services.bot_vision.gemini_generate_text")
    def test_match_uses_management_role(self, mock_gen):
        mock_gen.return_value = {"parsed": '{"product_id":1,"confidence":0.9}'}
        bot_vision.match([("image/jpeg", b"x")], candidates=[{"id": 1, "title": "t", "price": 1, "category": "", "fingerprint": ""}])
        self.assertEqual(mock_gen.call_args.kwargs.get("role"), "management")

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_chat_uses_chat_role(self, mock_gen):
        mock_gen.return_value = {"parsed": "ок", "model": "x", "meta": {}}
        settings = InstagramBotSettings.load()
        settings.gemini_model = "gemini-3.6-flash"
        bot.gemini_generate(settings, [{"role": "user", "text": "привіт"}])
        self.assertEqual(mock_gen.call_args.kwargs.get("role"), "chat")
        self.assertEqual(mock_gen.call_args.kwargs.get("model_override"), "gemini-3.6-flash")


class MatchRateGuardTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_allows_until_limit(self):
        for _ in range(15):
            self.assertTrue(bot._match_allowed("rg1", limit=15, window=3600))
        self.assertFalse(bot._match_allowed("rg1", limit=15, window=3600))

    def test_per_client_independent(self):
        self.assertTrue(bot._match_allowed("rg2", limit=1))
        self.assertFalse(bot._match_allowed("rg2", limit=1))
        self.assertTrue(bot._match_allowed("rg3", limit=1))
