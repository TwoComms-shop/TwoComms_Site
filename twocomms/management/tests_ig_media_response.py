"""Pure reply policy; no database, provider requests, actions or rewards."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from management.services.ig_media_analysis import bind_media_analysis
from management.services.ig_media_response import media_limitation_reply, normalize_media_reply
from management.services.ig_ugc_assessment import safe_ugc_acknowledgement


def analysis(*, kind="wearing", sentiment="positive", complaint=False,
    mime="image/jpeg", omitted=False, understood=True):
    parts = [{"source_message_id": 41, "source_part_id": "mp1_" + "1" * 32,
        "content_hash": "1" * 64, "original_index": 0, "capture_state": "owned", "mime": mime}]
    observation = {"source_inline_index": 0, "outcome": "understood" if understood else "uncertain",
        "content_kind": kind if understood else "unknown", "sentiment": sentiment if understood else "unknown",
        "confidence": .9, "evidence_code": "audio_speech" if mime.startswith("audio/") else "visual_content",
        "evidence": "Customer reports ripped seam" if complaint else "Source content observation",
        "complaint_code": "current_customer_complaint" if complaint else "none"}
    if mime.startswith("audio/"):
        observation.update(audio_status="transcribed" if understood else "unintelligible",
            transcript="Customer voice" if understood else "")
    if omitted:
        parts.append({**parts[0], "source_part_id": "mp1_" + "2" * 32,
            "content_hash": "2" * 64, "original_index": 1})
    return bind_media_analysis(parts=parts, observations=[observation], actual_inline_count=1,
        actual_content_hashes=["1" * 64], request_id="source-bound-request", provider_model="actual-model")


class MediaResponsePolicyTests(SimpleTestCase):
    client = SimpleNamespace(language="uk")

    def test_neutral_question_is_preserved_without_known_positive_sentiment(self):
        source = analysis(sentiment="neutral")
        generated = "Вас цікавить розмір чи посадка футболки?"
        self.assertEqual(normalize_media_reply(self.client, generated, media_analysis=source), generated)

    def test_partial_useful_answer_survives_omitted_tail(self):
        source = analysis(omitted=True)
        generated = "На першому фото ви показали образ. Друге вкладення недоступне. Який розмір вас цікавить?"
        result = normalize_media_reply(self.client, generated, media_analysis=source)
        self.assertTrue(result.startswith(generated))
        self.assertIn("частину вкладень із зображеннями", result)

    def test_validated_commerce_caption_survives_unknown_attachment(self):
        generated = "Розмір L обрано. Ціна — 1090 грн. Ось каталог: https://example.com/catalog?size=L"
        result = normalize_media_reply(self.client, generated, media_analysis=analysis(understood=False))
        self.assertTrue(result.startswith(generated))
        self.assertIn("Не вдалося", result)

    def test_validated_decimal_price_and_url_are_not_split_by_sentence_filter(self):
        generated = "Ціна — 1090.50 грн. Деталі: https://example.com/item/1.5"
        self.assertEqual(normalize_media_reply(self.client, generated, media_analysis=analysis(sentiment="neutral")), generated)

    def test_social_only_has_no_sales_invitation_even_with_positive_source(self):
        generated = "Дякуємо! Якщо захочете, покажемо інші варіанти за 1090 грн."
        result = normalize_media_reply(self.client, generated, media_analysis=analysis(), social_only=True)
        self.assertIn("образ", result)
        self.assertNotIn("1090", result)
        self.assertNotIn("покаж", result)

    def test_complaint_service_question_beats_qualified_auto_gratitude(self):
        source = analysis(sentiment="mixed", complaint=True)
        assessment = SimpleNamespace(decision="qualified_auto", pk=1)
        generated = "Підкажіть, будь ласка, що сталося зі швом?"
        self.assertEqual(safe_ugc_acknowledgement(self.client, generated,
            assessment=assessment, media_analysis=source), generated)
        self.assertEqual(assessment.decision, "qualified_auto")

    def test_complaint_removes_praise_but_preserves_independent_service_question(self):
        generated = "Круто виглядаєте! Підкажіть, що сталося зі швом?"
        result = normalize_media_reply(self.client, generated, media_analysis=analysis(sentiment="negative", complaint=True))
        self.assertEqual(result, "Підкажіть, що сталося зі швом?")

    def test_complaint_removes_manager_claim_and_retains_helpful_question(self):
        generated = "Передала менеджеру. Підкажіть, що сталося зі швом?"
        self.assertEqual(normalize_media_reply(self.client, generated,
            media_analysis=analysis(sentiment="negative", complaint=True)), "Підкажіть, що сталося зі швом?")

    def test_complaint_praise_and_discount_have_helpful_service_fallback(self):
        result = normalize_media_reply(self.client, "Ви чудово виглядаєте! Ось знижка 10%.",
            media_analysis=analysis(sentiment="negative", complaint=True))
        self.assertIn("проблема", result)
        self.assertIn("?", result)
        self.assertNotIn("зниж", result)
        self.assertNotIn("вигляда", result)

    def test_negative_without_complaint_never_invents_customer_problem(self):
        result = normalize_media_reply(self.client, "Чудово виглядаєте!",
            media_analysis=analysis(sentiment="negative"))
        self.assertNotIn("проблем", result)
        self.assertNotIn("Чудово", result)

    def test_negative_preserves_service_gratitude_for_reporting_problem(self):
        generated = "Дякуємо, що повідомили про проблему. Що саме сталося зі швом?"
        self.assertEqual(normalize_media_reply(self.client, generated,
            media_analysis=analysis(sentiment="negative", complaint=True)), generated)

    def test_positive_observed_taxonomy_has_distinct_safe_fallbacks(self):
        outputs = []
        for kind, word, mime in (("unboxing", "розпакування", "image/jpeg"),
            ("wearing", "образ", "image/jpeg"), ("review_video", "відеовідгук", "video/webm"),
            ("custom_design", "дизайн", "image/jpeg")):
            with self.subTest(kind=kind):
                reply = normalize_media_reply(self.client, "", media_analysis=analysis(kind=kind, mime=mime))
                self.assertIn(word, reply)
                self.assertNotIn("нашому", reply)
                outputs.append(reply)
        self.assertEqual(len(set(outputs)), 4)

    def test_audio_review_does_not_invent_a_video(self):
        result = normalize_media_reply(self.client, "", media_analysis=analysis(kind="review_video", mime="audio/ogg"))
        self.assertIn("відгук", result)
        self.assertNotIn("відео", result)

    def test_audio_observation_does_not_support_visual_outfit_praise(self):
        result = normalize_media_reply(self.client, "Ви круто виглядаєте!",
            media_analysis=analysis(kind="wearing", mime="audio/ogg"))
        self.assertNotIn("вигляда", result)

    def test_qualified_assessment_alone_cannot_invent_our_clothing_or_inspection(self):
        for decision in ("qualified_auto", "pending", "rejected"):
            with self.subTest(decision=decision):
                result = safe_ugc_acknowledgement(self.client, "Ви круто виглядаєте в нашому одязі!",
                    assessment=SimpleNamespace(decision=decision, pk=4))
                self.assertIn("Дяку", result)
                self.assertNotIn("вигляда", result)
                self.assertNotIn("нашому", result)
                self.assertNotIn("Перевіряємо", result)

    def test_unknown_attachment_never_claims_inspection(self):
        for generated in ("Я бачу ваш образ.", "Переглянула відео.", "На фото ви в нашій футболці."):
            with self.subTest(generated=generated):
                result = normalize_media_reply(self.client, generated, media_analysis=analysis(understood=False))
                self.assertIn("Не вдалося", result)
                self.assertNotEqual(result, generated)

    def test_partial_batch_cannot_claim_all_attachments_were_inspected(self):
        result = normalize_media_reply(self.client, "Я переглянула всі вкладення.",
            media_analysis=analysis(omitted=True))
        self.assertIn("Не вдалося", result)
        self.assertNotIn("переглянула", result)

    def test_unavailable_audio_sibling_keeps_image_question_and_names_only_audio_limitation(self):
        source = analysis(sentiment="neutral")
        source["unavailable_kinds"] = ["audio/ogg"]
        generated = "Вас цікавить посадка футболки на першому фото?"
        result = normalize_media_reply(self.client, generated, media_analysis=source)
        self.assertTrue(result.startswith(generated))
        self.assertIn("аудіочастину", result)
        self.assertIn("текстом", result)
        self.assertNotIn("розібрати зображення", result)

    def test_understood_image_does_not_prove_missing_video_or_audio_was_inspected(self):
        for kind, generated in (("video", "Я переглянула відео."),
            ("audio", "Я прослухала голосове повідомлення.")):
            source = analysis(sentiment="neutral")
            source["unavailable_kinds"] = [kind]
            with self.subTest(kind=kind):
                result = normalize_media_reply(self.client, generated, media_analysis=source)
                self.assertNotEqual(result, generated)
                self.assertNotIn("переглянула", result)
                self.assertNotIn("прослухала", result)
                self.assertIn("Не вдалося", result)

    def test_neutral_style_question_is_not_confused_with_unsupported_praise(self):
        generated = "Вас цікавить стильне худі чи футболка?"
        self.assertEqual(normalize_media_reply(self.client, generated,
            media_analysis=analysis(sentiment="neutral")), generated)

    def test_limitation_is_idempotent_for_partial_and_wholly_unavailable_media(self):
        for source in (analysis(omitted=True), analysis(mime="audio/ogg", understood=False),
            analysis(sentiment="negative", complaint=True, omitted=True)):
            for social_only in (False, True):
                with self.subTest(source=source, social_only=social_only):
                    first = normalize_media_reply(self.client, "", media_analysis=source, social_only=social_only)
                    second = normalize_media_reply(self.client, first, media_analysis=source, social_only=social_only)
                    self.assertEqual(first, second)

    def test_ephemeral_audio_limitation_is_idempotent_without_losing_validated_answer(self):
        source = analysis(sentiment="neutral")
        source["unavailable_kinds"] = ["audio"]
        generated = "Розмір L обрано. Уточніть, будь ласка, колір."
        first = normalize_media_reply(self.client, generated, media_analysis=source)
        second = normalize_media_reply(self.client, first, media_analysis=source)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(generated))

    def test_service_question_with_failed_audio_is_preserved_across_both_policy_calls(self):
        source = analysis(sentiment="negative", complaint=True)
        source["unavailable_kinds"] = ["audio"]
        generated = "Напишіть, будь ласка, що сталося зі швом?"
        first = normalize_media_reply(self.client, generated, media_analysis=source)
        second = normalize_media_reply(self.client, first, media_analysis=source)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(generated))

    def test_native_positive_custom_gratitude_with_failed_audio_is_idempotent(self):
        source = analysis()
        source["unavailable_kinds"] = ["audio"]
        generated = "Дякуємо за відмітку TwoComms!"
        first = normalize_media_reply(self.client, generated, media_analysis=source, social_only=True)
        second = normalize_media_reply(self.client, first, media_analysis=source, social_only=True)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(generated))

    def test_unknown_audio_asks_text_summary_without_picture_or_retry(self):
        result = normalize_media_reply(self.client, "", media_analysis=analysis(mime="audio/ogg", understood=False))
        self.assertIn("голосове", result)
        self.assertIn("текстом", result)
        for word in ("картин", "фото", "перевір", "пізніше", "менедж"):
            self.assertNotIn(word, result)

    def test_missing_retry_owner_cannot_promise_rechecking_or_later_contact(self):
        for generated in ("Перевіримо фото пізніше.", "Перевіряємо публікацію.",
            "Менеджер зв'яжеться з вами.", "Повідомимо, коли переглянемо відео."):
            with self.subTest(generated=generated):
                result = normalize_media_reply(self.client, generated, media_analysis=analysis(understood=False))
                self.assertNotEqual(result, generated)
                self.assertIn("Не вдалося", result)

    def test_media_observation_does_not_authorize_reward_offer(self):
        result = normalize_media_reply(self.client, "За фото дамо знижку!", media_analysis=analysis())
        self.assertNotIn("зниж", result)

    def test_adapter_opt_out_keeps_independent_caption_answer(self):
        generated = "Вас цікавить розмір L?"
        self.assertEqual(safe_ugc_acknowledgement(self.client, generated,
            media_analysis=analysis(sentiment="neutral"), social_only=False), generated)

    def test_localized_limitation_matches_audio_video_image_and_mixed_source(self):
        for language, audio_word, video_word, image_word in (
            ("uk", "голосове", "відео", "зображення"),
            ("ru", "голосовое", "видео", "изображение"),
            ("en", "voice", "video", "image")):
            client = SimpleNamespace(language=language)
            with self.subTest(language=language):
                self.assertIn(audio_word, media_limitation_reply(client, media_kinds=("audio/ogg",)))
                self.assertIn(video_word, media_limitation_reply(client, media_kinds=("video/webm",)))
                self.assertIn(image_word, media_limitation_reply(client, media_kinds=("image/jpeg",)))
                self.assertTrue(media_limitation_reply(client, media_kinds=("audio", "video")))

    def test_unavailable_only_capture_gets_honest_generic_clarification(self):
        source = bind_media_analysis(parts=[], actual_inline_count=0, actual_content_hashes=[],
            request_id="source-bound-request", provider_model="actual-model", capture_outcomes=[{
                "source_message_id": 41, "source_part_id": "mp1_" + "1" * 32, "original_index": 0,
                "collection_outcome": "unavailable", "reason": "owned_bytes_unavailable"}])
        result = normalize_media_reply(self.client, "", media_analysis=source)
        self.assertIn("вкладення", result)
        self.assertIn("текстом", result)
        self.assertNotIn("фото", result)

    def test_input_analysis_and_assessment_are_not_mutated(self):
        from copy import deepcopy
        source = analysis(sentiment="mixed", complaint=True, omitted=True)
        before = deepcopy(source)
        normalize_media_reply(self.client, "", media_analysis=source)
        self.assertEqual(source, before)

    def test_current_service_caption_overrides_positive_photo_without_media_complaint_authority(self):
        from copy import deepcopy
        from management.services.ig_media_analysis import media_reaction

        source = analysis()
        before = deepcopy(source)
        presented = {**source, "source_service": True}
        generated = "Круто виглядаєте! Підкажіть, що сталося зі швом?"
        self.assertEqual(normalize_media_reply(self.client, generated, media_analysis=presented),
            "Підкажіть, що сталося зі швом?")
        self.assertEqual(source, before)
        self.assertEqual(presented["parts"][0]["sentiment"], "positive")
        self.assertEqual(media_reaction(presented)["complaint_parts"], [])

    def test_positive_photo_with_service_caption_and_only_praise_gets_service_fallback(self):
        source = {**analysis(), "source_service": True}
        result = normalize_media_reply(self.client, "Чудово виглядаєте!", media_analysis=source)
        self.assertIn("проблема", result)
        self.assertNotIn("Чудово", result)
