from unittest.mock import patch

from django.test import TestCase, override_settings

from management.models import IgClient
from management.services.ig_reply_language import resolve_own_source_reply_language


@override_settings(GEMINI_NONLIVE_ADMISSION_MODE="shadow", GOOGLE_INDEXING_ENABLED=False)
class ReplyLanguageIntegrationTests(TestCase):
    def setUp(self):
        from management.tests_bot_memory import _memory_prompt_settings
        self.settings = _memory_prompt_settings()
        self.client_record = IgClient.objects.create(igsid="language-integration", language="ru")

    def test_legacy_render_scope_keeps_language_and_resets_without_profile_write(self):
        from management.models import InstagramBotMessage
        from management.services import instagram_bot as bot
        from management.services.ig_ugc_assessment import safe_ugc_acknowledgement
        self.settings.is_enabled = True
        self.settings.save(update_fields=["is_enabled"])
        row = InstagramBotMessage.objects.create(client=self.client_record, sender_id=self.client_record.igsid,
            role="user", source="webhook", text="Please help me with this voice message.",
            provider_namespace="instagram_login:language-test", mid="language-scope")

        def inside(*_args):
            self.assertEqual(bot._assisted_checkout_locale(self.client_record), "en")
            self.assertIn("could not open", bot._media_unavailable_reply(self.client_record))
            self.assertIn("Thank you", safe_ugc_acknowledgement(bot._reply_template_client(self.client_record), ""))
            answer, _ = bot._apply_turn_intelligence_resolution("", {}, {"audio_status": "unintelligible"}, self.client_record)
            self.assertIn("Please type", answer)
            self.assertTrue(bot._reply_language_mismatch_for_client(self.client_record,
                "Дякую за повідомлення. Будь ласка, уточніть своє питання."))
            other = IgClient(pk=self.client_record.pk + 1, language="uk")
            self.assertEqual(bot._assisted_checkout_locale(other), "uk")
            return True

        with patch.object(bot, "_process_one_inside_reply_boundary", side_effect=inside), patch.object(bot, "_stop_typing_indicator"):
            self.assertTrue(bot._process_one_unlocked(self.settings, row))
        self.assertIsNone(bot._REPLY_LANGUAGE_SCOPE.get())
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.language, "ru")
        with patch.object(bot, "_process_one_inside_reply_boundary", side_effect=RuntimeError("fixture")), patch.object(bot, "_stop_typing_indicator"):
            with self.assertRaises(RuntimeError):
                bot._process_one_unlocked(self.settings, row)
        self.assertIsNone(bot._REPLY_LANGUAGE_SCOPE.get())

    def test_authored_fallback_respects_negative_requests_and_can_omit(self):
        from management.services import instagram_bot as bot
        for source_text in ("Do not reply in Ukrainian.", "Reply in French, not English."):
            decision = resolve_own_source_reply_language(sources=[{"message_id": 1,
                "role": "user", "text": source_text}], profile_language="uk")
            token = bot._REPLY_LANGUAGE_SCOPE.set((self.client_record.pk, decision))
            try:
                reply = bot._response_validation_fallback(self.client_record)
                self.assertTrue(reply)
                from management.services.ig_reply_language import _observed_language
                self.assertNotIn(_observed_language(reply), decision.excluded_languages)
            finally:
                bot._REPLY_LANGUAGE_SCOPE.reset(token)
        decision = resolve_own_source_reply_language(sources=[{"message_id": 1, "role": "user",
            "text": "Do not reply in Ukrainian. Do not reply in Russian. Do not reply in English."}])
        token = bot._REPLY_LANGUAGE_SCOPE.set((self.client_record.pk, decision))
        try:
            self.assertEqual(bot._response_validation_fallback(self.client_record), "")
        finally:
            bot._REPLY_LANGUAGE_SCOPE.reset(token)

    def test_actual_generator_rejects_wrong_candidate_and_repairs_same_payload(self):
        from management.services.instagram_bot import gemini_generate
        source = {"message_id": 3288, "role": "user", "text":
            "Your drip is firee. I think your pieces have a lot of potential. "
            "If you can afford a small fee I'll help you get the brand out there."}
        language = resolve_own_source_reply_language(sources=[source], profile_language="ru")
        observed = {}

        def dispatch(payload, **kwargs):
            observed.update(kwargs)
            system = payload["system_instruction"]["parts"][0]["text"]
            self.assertIn("Reply in English.", system)
            self.assertIn("facts:reply_language", kwargs["request_policy_manifest"]["mandatory_ids"])
            wrong = {"reply_text": "Дякую за пропозицію! Будь ласка, надішліть приклади ваших робіт.", "controls": []}
            rejected = kwargs["result_validator"](wrong, usage={})
            self.assertFalse(rejected.valid)
            self.assertEqual(rejected.reason_codes, ("reply_language_mismatch",))
            repaired = kwargs["repair_payload_factory"](payload, wrong, rejected.reason_codes)
            self.assertIn("Reply in English.", repaired["contents"][-1]["parts"][0]["text"])
            # Locale repair does not authorize an unverified financial claim.
            false_payment = {"reply_text": "Your payment is confirmed.", "controls": []}
            self.assertFalse(kwargs["result_validator"](false_payment, usage={}).valid)
            correct = {"reply_text": "Thank you for your proposal. Please send examples of your work.", "controls": []}
            self.assertTrue(kwargs["result_validator"](correct, usage={}).valid)
            return {"parsed": correct, "model": "gemini-3.5-flash-lite", "meta": {}}

        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=dispatch):
            result = gemini_generate(self.settings, [source], client=self.client_record, captured_reply_language=language)
        self.assertIn("Thank you", result.reply_text)
        self.assertEqual(observed["max_actual_dispatches"], 2)

    def test_wrong_language_cannot_bypass_final_transport_result(self):
        from management.services.instagram_bot import gemini_generate
        source = {"message_id": 1, "role": "user", "text": "Can you show your collaboration policy please?"}
        decision = resolve_own_source_reply_language(sources=[source])
        with patch("management.services.call_ai_analysis.gemini_generate_text", return_value={
            "parsed": {"reply_text": "Дякую за пропозицію. Будь ласка, надішліть приклади ваших робіт.", "controls": []},
            "model": "gemini-3.5-flash-lite", "meta": {},
        }):
            self.assertIsNone(gemini_generate(self.settings, [source], client=self.client_record, captured_reply_language=decision))

    def test_actual_legacy_worker_replaces_wrong_language_before_actions(self):
        from django.utils import timezone
        from management.tests_ig_live_reply_priority import StructuredWorkerAuthorityBoundaryTests
        self.settings.is_enabled = True
        self.settings.ai_enabled = True
        self.settings.allowed_senders = ""
        self.settings.save(update_fields=["is_enabled", "ai_enabled", "allowed_senders"])
        self.client_record.profile_fetched_at = timezone.now()
        self.client_record.save(update_fields=["profile_fetched_at"])
        payload = {"reply_text": "Дякую за повідомлення. Будь ласка, уточніть своє питання.",
            "controls": [{"kind": "paylink", "value": "full"}]}
        with patch("management.services.bot_orders.create_checkout_proposal_link") as checkout:
            _, handled, delivered = StructuredWorkerAuthorityBoundaryTests._run(self, self.client_record,
                payload, suffix="source-language", text="Please help me with your product options.")
        self.assertEqual(handled, 1)
        self.assertTrue(delivered)
        self.assertIn("reliable answer", delivered)
        checkout.assert_not_called()
        self.client_record.refresh_from_db()
        self.assertEqual(self.client_record.language, "ru")
