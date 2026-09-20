"""Selection requests belong to the sender, with bounded refusal continuity."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgFollowUpTask, InstagramBotMessage
from management.services.ig_turn_intent import (
    _purpose, build_turn_intent, intent_generation_guidance, purpose_blockers, validate_turn_response,
)


class SelectionLanguageTests(TestCase):
    def setUp(self):
        self.customer = IgClient.objects.create(igsid="selection-language")

    def message(self, text):
        return InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, role="user", source="webhook",
            text=text, provider_namespace="selection-channel",
        )

    def decision(self, *rows):
        return build_turn_intent(self.customer, source_messages=list(rows))

    def old_catalog(self, watermark):
        journal = SimpleNamespace(pk=9, revision_id=None, watermark_message_id=watermark,
            occurred_at=timezone.now(), source_binding={},
            interpretation={"intents": [{"kind": "catalog", "operation": "open"}]},
            active_intents=[{"kind": "catalog"}])
        mocked = patch("management.services.ig_turn_intent.IgConversationRouteDecision.objects")
        manager = mocked.start()
        manager.filter.return_value.order_by.return_value.first.return_value = journal
        self.addCleanup(mocked.stop)

    def test_explicit_sender_selection_requests_uk_ru_en(self):
        for text in (
            "Підберіть футболку, будь ласка", "Привіт, підберіть мені чорну футболку",
            "Будь ласка, порадьте худі", "Можете підібрати футболку?",
            "Допоможіть підібрати розмір", "Який розмір мені підійде?",
            "Подберите футболку, пожалуйста", "Помогите выбрать худи",
            "Можете подобрать мне футболку?", "Какой размер мне подойдет?",
            "Please pick a T-shirt for me", "Can you help me choose a hoodie?",
            "I'd like help choosing a shirt", "Recommend a sweatshirt", "Which size would fit me?",
            "Підберіть не чорну, а білу футболку", "Pick a white shirt, not a black one",
        ):
            with self.subTest(text=text):
                self.assertEqual(_purpose(text), "requested_selection")

    def test_quoted_reported_ad_or_generic_clothing_has_no_selection_permission(self):
        for text in (
            '«Підберіть футболку, будь ласка»', '"Please help me choose a shirt"',
            "> Подберите футболку, пожалуйста", "Мені написали: підберіть футболку",
            "Вона сказала: Допоможіть обрати футболку", "My friend asked: help me choose a hoodie",
            "Реклама:\nПідберіть футболку", "This ad says:\nChoose a hoodie",
            "Підберіть для себе стильну футболку", "Pick your outfit for yourself",
            "Футболка", "Оверсайз?", "Стаття про засновника бренду", "https://example.test/choose-shirt",
            "Help me choose a database", "I helped my friend choose a shirt",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(_purpose(text), "requested_selection")

    def test_local_negation_is_selection_withdrawal_not_price_or_purchase_refusal(self):
        for text in (
            "Не підбирайте мені футболку", "Не хочу, щоб ви підбирали футболку",
            "Не треба допомагати вибирати футболку", "Підбір мені більше не потрібен",
            "Не подбирайте футболку", "Не нужно помогать мне выбрать худи",
            "Please don't choose a shirt for me", "I don't want you to help me choose a hoodie",
            "Can you please not choose a shirt?", "Stop recommending shirts",
        ):
            with self.subTest(text=text):
                row = self.message(text)
                decision = self.decision(row)
                self.assertEqual(decision["purpose"], "selection_withdrawal", text)
                self.assertTrue(decision["selection_withdrawn"])
                self.assertEqual(decision["selection_withdrawal_refs"], [row.pk])
                self.assertFalse(decision["commerce_evidence_refs"])
                self.assertTrue(validate_turn_response(decision, "Можу підібрати іншу футболку."))
                self.assertEqual(validate_turn_response(decision, "Добре, врахую ваше прохання."), "")

    def test_last_directive_in_same_source_or_bundle_wins(self):
        original = self.message("Підберіть футболку")
        withdrawal = self.message("Не підбирайте мені футболку")
        self.assertFalse(self.decision(original, withdrawal)["commerce_evidence_refs"])
        self.assertEqual(_purpose("Підберіть футболку. Не треба підбирати."), "selection_withdrawal")
        self.assertEqual(_purpose("Не підбирайте футболку. А мені підберіть худі."), "requested_selection")
        renewal = self.message("Подберите худи, пожалуйста")
        renewed = self.decision(original, withdrawal, renewal)
        self.assertFalse(renewed["selection_withdrawn"])
        self.assertEqual(renewed["commerce_evidence_refs"], [renewal.pk])

    def test_quoted_withdrawal_does_not_override_own_request(self):
        source = self.message('У рекламі написано «не підбирайте футболку». А мені підберіть худі.')
        self.assertEqual(self.decision(source)["purpose"], "requested_selection")

    def test_mixed_withdrawal_allows_price_answer_but_no_sales_action_or_followup(self):
        source = self.message("Не підбирайте інше, тільки скажіть ціну")
        decision = self.decision(source)
        self.assertEqual(decision["purpose"], "price_inquiry")
        self.assertTrue(decision["selection_withdrawn"])
        self.assertIn("retail_consultation", decision["allowed_response_acts"])
        self.assertNotIn("optional_retail_next_step", decision["allowed_response_acts"])
        self.assertEqual(validate_turn_response(decision, "Ціна — 1090 грн."), "")
        self.assertTrue(validate_turn_response(decision, "Хочете замовити футболку?"))
        self.assertTrue(validate_turn_response(decision, "Добре", ["client_configuration_update"]))
        self.assertEqual(purpose_blockers(self.customer, decision), "selection_followup_withdrawn")
        self.assertNotIn("price_objection", str(decision))

    def test_own_explicit_checkout_remains_subject_to_independent_checkout_authority(self):
        self.message("Не підбирайте мені іншу футболку")
        source = self.message("Оформіть замовлення")
        decision = self.decision(source)
        self.assertEqual(decision["purpose"], "retail")
        self.assertTrue(decision["selection_withdrawn"])
        self.assertEqual(validate_turn_response(decision, "Оформлення доступне після уточнення розміру.", ["checkout_proposal_create"]), "")
        self.assertTrue(validate_turn_response(decision, "Можу підібрати іншу футболку."))

    def test_withdrawal_cannot_be_revived_by_terse_configuration_or_media(self):
        earlier = self.message("Підберіть футболку")
        self.old_catalog(earlier.pk)
        withdrawn = self.message("Не підбирайте мені футболку")
        for text in ("XL", "Оверсайз", ""):
            source = self.message(text)
            decision = self.decision(source)
            self.assertFalse(decision["commerce_evidence_refs"])
            self.assertTrue(decision["selection_withdrawn"])
            self.assertEqual(decision["selection_withdrawal_refs"], [withdrawn.pk])

    def test_more_than_four_neutral_sources_cannot_silently_forget_withdrawal(self):
        earlier = self.message("Підберіть футболку")
        self.old_catalog(earlier.pk)
        self.message("Не підбирайте мені футболку")
        for _ in range(5):
            self.message("Дякую")
        source = self.message("XL")
        decision = self.decision(source)
        self.assertTrue(decision["selection_continuity_uncertain"])
        self.assertFalse(decision["commerce_evidence_refs"])
        self.assertTrue(validate_turn_response(decision, "Можу підібрати іншу футболку."))
        renewed = self.message("Підберіть худі, будь ласка")
        decision = self.decision(renewed)
        self.assertFalse(decision["selection_continuity_uncertain"])
        self.assertEqual(decision["purpose"], "requested_selection")
        self.assertEqual(decision["commerce_evidence_refs"], [renewed.pk])

    def test_old_permission_epoch_does_not_supply_current_continuity(self):
        earlier = self.message("Підберіть футболку")
        self.old_catalog(earlier.pk)
        self.message("Не підбирайте мені футболку")
        self.customer.reply_permission_epoch = 1
        self.customer.save(update_fields=["reply_permission_epoch"])
        source = self.message("XL")
        decision = self.decision(source)
        self.assertFalse(decision["selection_withdrawn"])
        self.assertFalse(decision["commerce_evidence_refs"])


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class SelectionLivePipelineTests(TransactionTestCase):
    def prepare_case(self, text):
        from management import tests_ig_revision_live as fixtures
        from management.services.ig_turn_revisions import create_collecting_revision

        case = fixtures.RevisionLiveTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.source.text = text
        case.source.save(update_fields=["text"])
        case.revision = create_collecting_revision(case.turn, [case.source], bypass_quiet=True).revision
        case._prepare()
        return case

    def test_actual_positive_selection_sends_one_helpful_question_without_manager(self):
        case = self.prepare_case("Підберіть футболку, будь ласка")
        case.parsed = {"reply_text": "Який стиль вам подобається — мінімалізм чи яскравий принт?", "controls": []}
        result, generation, send = case._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generation.assert_called_once()
        send.assert_called_once()
        self.assertFalse(IgFollowUpTask.objects.filter(kind="manager_task").exists())
        self.assertEqual(case.revision.delivery_effects.get(group="substantive_text").payload["message"]["text"], case.parsed["reply_text"])

    def test_actual_withdrawal_sends_neutral_ack_without_sales_followup(self):
        case = self.prepare_case("Не підбирайте мені іншу футболку")
        case.parsed = {"reply_text": "Добре, врахую ваше прохання.", "controls": []}
        result, generation, send = case._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generation.assert_called_once()
        send.assert_called_once()
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_actual_quoted_request_does_not_gain_permission_from_model_catalog_topic(self):
        case = self.prepare_case('«Підберіть футболку, будь ласка»')
        case.parsed = {"reply_text": "Дякуємо, що поділилися.", "controls": [],
            "customer_routes": {"schema_version": "customer-route.v1", "focus_index": 0,
                "intents": [{"kind": "catalog", "subtype": "none", "operation": "open",
                    "evidence_message_ids": [case.source.pk], "confidence": 0.99}]}}
        result, generation, send = case._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generation.assert_called_once()
        send.assert_called_once()
        self.assertFalse(IgFollowUpTask.objects.exists())
