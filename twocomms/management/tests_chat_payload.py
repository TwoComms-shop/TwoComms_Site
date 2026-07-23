"""Tests for model-aware chat reasoning and persisted Gemini telemetry."""
from unittest.mock import patch

from django.test import TestCase

from management.models import InstagramBotSettings
from management.services import instagram_bot as bot


class ChatPayloadThinkingTests(TestCase):
    def test_chat_payload_caps_thinking_and_has_room_for_reply(self):
        s = InstagramBotSettings.load()
        captured = {}

        def fake_text(payload, *, role="chat", manual_key=None, **kwargs):
            captured["payload"] = payload
            captured["role"] = role
            captured["reasoning_task"] = kwargs.get("reasoning_task")
            return {
                "parsed": "Привіт!",
                "model": "gemini-3.6-flash",
                "usage": {"thoughtsTokenCount": 20, "candidatesTokenCount": 7},
                "meta": {
                    "key": "GEMINI_API",
                    "reasoning_task": kwargs.get("reasoning_task"),
                    "reasoning_level": "medium",
                    "reasoning_policy_version": "2026-07-23.v1",
                    "latency_ms": 25,
                },
            }

        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=fake_text):
            out = bot.gemini_generate(s, [{"role": "user", "text": "Привіт"}])

        self.assertEqual(out, "Привіт!")
        cfg = captured["payload"]["generationConfig"]
        # достатньо токенів на сам текст відповіді (не зʼїдається thinking-ом)
        self.assertGreaterEqual(cfg["maxOutputTokens"], 1536)
        self.assertEqual(captured["role"], "chat")
        self.assertEqual(captured["reasoning_task"], "customer_chat")

        s.refresh_from_db()
        self.assertEqual(s.last_gemini_reasoning_task, "customer_chat")
        self.assertEqual(s.last_gemini_reasoning_level, "medium")
        self.assertEqual(s.last_gemini_policy_version, "2026-07-23.v1")
        self.assertEqual(s.last_gemini_thoughts_tokens, 20)
        self.assertEqual(s.last_gemini_candidates_tokens, 7)

        status = bot.status_snapshot()
        self.assertEqual(status["last_gemini_reasoning_task"], "customer_chat")
        self.assertEqual(status["last_gemini_reasoning_level"], "medium")
        self.assertEqual(status["last_gemini_policy_version"], "2026-07-23.v1")
