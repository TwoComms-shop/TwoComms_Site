"""Tests that Instagram chat settings control the actual provider model."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase

from management.services import call_ai_analysis as ai
from management.models import InstagramBotSettings
from management.bot_views import bot_settings_save_api


class GeminiChatAuthorityTests(SimpleTestCase):
    @patch("management.services.call_ai_analysis._run_with_pool")
    def test_text_generation_forwards_authoritative_model(self, run_pool):
        run_pool.return_value = {"parsed": "ok", "model": "gemini-3.6-flash", "meta": {}}

        result = ai.gemini_generate_text(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            role="chat",
            model_override="gemini-3.6-flash",
        )

        self.assertEqual(result["model"], "gemini-3.6-flash")
        self.assertEqual(run_pool.call_args.kwargs["model_override"], "gemini-3.6-flash")


class GeminiSettingsAllowlistTests(TestCase):
    def test_settings_api_rejects_arbitrary_model(self):
        user = get_user_model().objects.create_user(username="gemini-admin", is_staff=True)
        request = RequestFactory().post(
            "/bot/api/settings/",
            {"gemini_model": "https://attacker.invalid/model", "ai_enabled": "1"},
        )
        request.user = user

        response = bot_settings_save_api(request)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(json.loads(response.content)["success"])
        self.assertNotEqual(InstagramBotSettings.load().gemini_model, "https://attacker.invalid/model")
