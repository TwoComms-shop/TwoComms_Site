import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from management.models import GeminiKeyState
from management.services import call_ai_analysis as caa
from management.services import gemini_probe


class GeminiModelPayloadContractTests(TestCase):
    def test_gemini_36_uses_thinking_level_instead_of_legacy_budget(self):
        payload = {
            "generationConfig": {
                "maxOutputTokens": 128,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }

        normalized = caa._payload_for_model("gemini-3.6-flash", payload)

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )
        self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)

    def test_legacy_models_keep_legacy_thinking_budget(self):
        payload = {
            "generationConfig": {
                "maxOutputTokens": 128,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }

        normalized = caa._payload_for_model("gemini-2.5-flash", payload)

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 0},
        )

    def test_gemini_36_preserves_explicit_level_and_other_thinking_fields(self):
        payload = {
            "generationConfig": {
                "thinkingConfig": {
                    "thinkingBudget": 64,
                    "thinkingLevel": "high",
                    "includeThoughts": True,
                },
            },
        }

        normalized = caa._payload_for_model("gemini-3.6-flash", payload)

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high", "includeThoughts": True},
        )


class GeminiProbeClassificationTests(TestCase):
    def test_success_without_candidates_is_reachable_empty(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({"usageMetadata": {"totalTokenCount": 1}}),
        )

        self.assertEqual(status["status"], "reachable_empty")
        self.assertEqual(status["finish_reason"], "")

    def test_malformed_candidates_shape_is_malformed(self):
        status = gemini_probe.classify_probe_response(200, json.dumps({"candidates": {}}))

        self.assertEqual(status["status"], "malformed_response")

    def test_max_tokens_without_text_is_reachable_degraded(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}],
                "usageMetadata": {"thoughtsTokenCount": 120, "candidatesTokenCount": 0},
            }),
        )

        self.assertEqual(status["status"], "reachable_degraded")
        self.assertEqual(status["finish_reason"], "MAX_TOKENS")
        self.assertEqual(status["thoughts_tokens"], 120)

    def test_partial_max_tokens_is_still_degraded(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "O"}]}}],
            }),
        )

        self.assertEqual(status["status"], "reachable_degraded")

    def test_safety_takes_precedence_even_if_provider_includes_text(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "SAFETY", "content": {"parts": [{"text": "blocked"}]}}],
            }),
        )

        self.assertEqual(status["status"], "blocked")

    def test_thought_only_parts_are_not_an_answer(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"thought": True, "text": "internal"}]}}],
            }),
        )

        self.assertEqual(status["status"], "reachable_empty")

    def test_safety_block_is_reachable_but_not_usable(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "promptFeedback": {"blockReason": "SAFETY"},
                "candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}],
            }),
        )

        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["finish_reason"], "SAFETY")

    def test_non_200_is_classified_without_exposing_response_body(self):
        status = gemini_probe.classify_probe_response(429, '{"error":{"message":"secret-key-value"}}')

        self.assertEqual(status["status"], "quota")
        self.assertNotIn("secret-key-value", json.dumps(status))


class GeminiProbeCommandTests(TestCase):
    @patch.dict("os.environ", {
        "GEMINI_API": "secret-one",
        "GEMINI_API2": "secret-two",
        "GEMINI_API3": "secret-three",
        "GEMINI_API4": "secret-four",
        "GEMINI_API5": "secret-five",
        "GEMINI_API6": "secret-six",
    }, clear=False)
    @patch("management.services.gemini_probe.probe_key")
    def test_probe_command_checks_all_keys_and_redacts_values(self, probe_key, captured=None):
        probe_key.side_effect = lambda model, key, timeout: {
            "status": "ok",
            "http_code": 200,
            "finish_reason": "STOP",
            "latency_ms": 5,
            "model": model,
        }

        from io import StringIO

        output = StringIO()
        call_command("probe_ig_gemini_pool", role="chat", model="gemini-3.6-flash", parallel=2, stdout=output)

        self.assertEqual(probe_key.call_count, 6)
        self.assertNotIn("secret-one", output.getvalue())
        self.assertIn("GEMINI_API", output.getvalue())
        self.assertEqual(
            GeminiKeyState.objects.filter(last_probe_status="ok", last_probe_model="gemini-3.6-flash").count(),
            6,
        )
