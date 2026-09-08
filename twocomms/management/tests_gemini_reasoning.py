from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from management.services import call_ai_analysis as ai


class GeminiReasoningPolicyTests(SimpleTestCase):
    def test_customer_chat_is_low_with_1536_output_cap(self):
        policy = ai.reasoning_policy("customer_chat")

        self.assertEqual(policy["level"], "low")
        self.assertEqual(policy.get("max_output_tokens"), 1536)

    def test_customer_decision_tasks_are_high_with_4096_output_cap(self):
        for task in (
            "product_decision",
            "size_fit_decision",
            "catalog_match",
            "media_analysis",
            "payment_decision",
            "order_decision",
        ):
            with self.subTest(task=task):
                policy = ai.reasoning_policy(task)
                self.assertEqual(policy["level"], "high")
                self.assertEqual(policy.get("max_output_tokens"), 4096)

    def test_background_analysis_tasks_remain_high(self):
        for task in (
            "customer_intelligence",
            "conversion_analysis",
            "conversation_reanalysis",
        ):
            with self.subTest(task=task):
                self.assertEqual(ai.reasoning_policy(task)["level"], "high")

    def test_health_probe_is_low(self):
        self.assertEqual(ai.reasoning_policy("health_probe")["level"], "low")

    def test_journey_trace_medium_preserves_output_and_model_specific_controls(self):
        policy = ai.reasoning_policy("journey_trace_reconstruction")
        self.assertEqual(policy["level"], "medium")
        self.assertEqual(policy["thinking_budget"], 4096)
        self.assertIsNone(policy["max_output_tokens"])
        self.assertEqual(ai.reasoning_policy("conversation_reanalysis")["level"], "high")
        original = {"generationConfig": {"maxOutputTokens": 12288,
                                         "thinkingConfig": {"thinkingLevel": "high", "thinkingBudget": 8192}}}
        for model, expected in (
            ("gemini-3.6-flash", {"thinkingLevel": "medium"}),
            ("gemini-2.5-flash", {"thinkingBudget": 4096}),
        ):
            with self.subTest(model=model):
                result = ai._payload_for_model(model, original, reasoning_task="journey_trace_reconstruction")
                self.assertEqual(result["generationConfig"]["thinkingConfig"], expected)
                self.assertEqual(result["generationConfig"]["maxOutputTokens"], 12288)
        self.assertEqual(original["generationConfig"]["thinkingConfig"]["thinkingLevel"], "high")

    def test_unknown_task_is_rejected(self):
        with self.assertRaises(ValueError):
            ai.reasoning_policy("invented_task")

    def test_gemini_36_chat_uses_low_caps_output_and_removes_legacy_budget(self):
        payload = {
            "generationConfig": {
                "maxOutputTokens": 8192,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }

        normalized = ai._payload_for_model(
            "gemini-3.6-flash", payload, reasoning_task="customer_chat"
        )

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )
        self.assertEqual(normalized["generationConfig"]["maxOutputTokens"], 1536)
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 8192)
        self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)

    def test_gemini_36_high_stakes_uses_high(self):
        payload = {
            "generationConfig": {
                "maxOutputTokens": 8192,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }

        normalized = ai._payload_for_model(
            "gemini-3.6-flash", payload, reasoning_task="payment_decision"
        )

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high"},
        )
        self.assertEqual(normalized["generationConfig"]["maxOutputTokens"], 4096)

    def test_gemini_3_removes_deprecated_sampling_but_25_preserves_it(self):
        payload = {
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.9,
                "topK": 40,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }

        gen3 = ai._payload_for_model(
            "gemini-3.6-flash", payload, reasoning_task="customer_chat"
        )
        gen25 = ai._payload_for_model(
            "gemini-2.5-flash", payload, reasoning_task="customer_chat"
        )

        self.assertNotIn("temperature", gen3["generationConfig"])
        self.assertNotIn("topP", gen3["generationConfig"])
        self.assertNotIn("topK", gen3["generationConfig"])
        self.assertEqual(gen25["generationConfig"]["temperature"], 0.2)
        self.assertEqual(gen25["generationConfig"]["topP"], 0.9)
        self.assertEqual(gen25["generationConfig"]["topK"], 40)

    def test_gemini_25_uses_versioned_budget_mapping(self):
        payload = {"generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}}}

        normalized = ai._payload_for_model(
            "gemini-2.5-flash", payload, reasoning_task="conversion_analysis"
        )

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 8192},
        )

    def test_probe_payload_explicitly_stays_low(self):
        from management.services.gemini_probe import build_probe_payload

        self.assertEqual(
            build_probe_payload("gemini-3.6-flash")["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )

    @patch("management.services.call_ai_analysis._run_with_pool")
    def test_public_text_api_forwards_the_task(self, run_pool):
        run_pool.return_value = {"parsed": "ok", "meta": {}}

        ai.gemini_generate_text(
            {"contents": []}, role="management", reasoning_task="payment_decision"
        )

        self.assertEqual(run_pool.call_args.kwargs["reasoning_task"], "payment_decision")

    def test_provider_thought_parts_never_become_answer_text(self):
        response = Mock(status_code=200, text="")
        response.json.return_value = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [
                    {"thought": True, "text": "private reasoning"},
                    {"text": "Customer answer"},
                ]},
            }],
            "usageMetadata": {"thoughtsTokenCount": 12, "candidatesTokenCount": 3},
        }

        with patch("management.services.call_ai_analysis.requests.post", return_value=response):
            text, usage = ai._gemini_call_once(
                "gemini-3.6-flash",
                {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]},
                "secret",
                parse=False,
            )

        self.assertEqual(text, "Customer answer")
        self.assertNotIn("private reasoning", text)
        self.assertEqual(usage["_finish_reason"], "STOP")


class InstagramChatReasoningSelectionTests(SimpleTestCase):
    def test_plain_chat_stays_medium_task(self):
        from management.services.instagram_bot import select_chat_reasoning_task

        self.assertEqual(
            select_chat_reasoning_task([{"role": "user", "text": "Привіт"}]),
            "customer_chat",
        )

    def test_size_payment_and_media_escalate(self):
        from management.services.instagram_bot import select_chat_reasoning_task

        cases = (
            ([{"role": "user", "text": "Який розмір мені підійде?"}], None, "size_fit_decision"),
            ([{"role": "user", "text": "Дайте посилання на оплату"}], None, "payment_decision"),
            ([{"role": "user", "text": "Що це за товар?"}], [("image/jpeg", b"x")], "media_analysis"),
        )
        for history, images, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(select_chat_reasoning_task(history, images), expected)
