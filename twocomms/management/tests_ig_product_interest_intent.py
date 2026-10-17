"""Explicit interest in apparel permits a conversation, never a paid order."""
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from management.models import IgClient, InstagramBotMessage
from management.services.ig_turn_intent import _purpose, build_turn_intent, validate_turn_response


class ProductInterestLanguageTests(SimpleTestCase):
    def test_incident_greeting_and_specific_shirt_is_an_inquiry(self):
        self.assertEqual(_purpose("Вітаю\nЦікавить ця футболка"), "product_inquiry")

    def test_addressed_apparel_interest_uk_ru_en(self):
        for text in (
            "Вітаю, цікавить ця футболка", "Мене цікавить чорна футболка на фото", "Мені цікава ця футболка",
            "Добрий день! Цікавить це худі", "Привіт, цікавлять ваші світшоти",
            "Здравствуйте, интересует эта футболка", "Меня интересует худи на фотографии", "Мне интересна эта футболка",
            "Привет! Интересуют ваши свитшоты", "Hi, I'm interested in this T-shirt",
            "I am interested in the hoodie in this photo", "Interested in your sweatshirts",
        ):
            with self.subTest(text=text):
                self.assertEqual(_purpose(text), "product_inquiry")

    def test_negation_quotes_reported_text_and_generic_interest_grant_nothing(self):
        for text in (
            "Мене не цікавить ця футболка", "Не цікавить футболка", "Не интересует эта футболка",
            "I'm not interested in this T-shirt", "I am no longer interested in this hoodie",
            '«Цікавить ця футболка»', '"I am interested in this T-shirt"',
            "> Интересует эта футболка", "Вона написала: цікавить ця футболка",
            "Вітаю\nПодруга написала:\nЦікавить ця футболка",
            "Реклама:\nЦікавить ця футболка", "She said:\nInterested in this T-shirt",
            "Цікавить історія футболки", "Цікавить стаття про футболки",
            "Меня интересует доставка", "I'm interested in your story about clothing",
            "This T-shirt", "Фото футболки", "Дякую за фото футболки", "Цікавить цей футбольний матч",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(_purpose(text), "product_inquiry")

    def test_reported_request_can_be_followed_by_separate_own_interest(self):
        self.assertEqual(_purpose("Подруга сказала: подивись футболку. А мене цікавить худі."), "product_inquiry")

    def test_interest_does_not_replace_independent_explicit_order_purpose(self):
        self.assertEqual(_purpose("Цікавить ця футболка. Хочу замовити."), "retail")

    def test_last_own_interest_retraction_and_renewal_in_one_message(self):
        for interest, withdrawal in (
            ("Цікавить ця футболка", "Мене більше не цікавить ця футболка"),
            ("Интересует эта футболка", "Меня больше не интересует эта футболка"),
            ("I'm interested in this T-shirt", "I'm no longer interested in this T-shirt"),
        ):
            with self.subTest(withdrawal=withdrawal):
                self.assertEqual(_purpose(interest + ". " + withdrawal), "product_interest_withdrawal")
                self.assertEqual(_purpose(withdrawal + ". " + interest), "product_inquiry")
        self.assertEqual(_purpose('Цікавить ця футболка. Подруга написала: мене більше не цікавить ця футболка.'), "product_inquiry")


class ProductInterestIntentTests(TestCase):
    def setUp(self):
        self.customer = IgClient.objects.create(igsid="product-interest-customer")

    def message(self, text, **kwargs):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", provider_namespace="instagram_login:interest-owner",
            text=text, **kwargs)

    def test_incident_text_and_photo_allow_product_question_without_route(self):
        text = self.message("Вітаю\nЦікавить ця футболка")
        photo = self.message("", attachment_media=[{"type": "image", "status": "owned"}])
        decision = build_turn_intent(self.customer, source_messages=[text, photo])
        self.assertEqual(decision["purpose"], "product_inquiry")
        self.assertEqual(decision["commerce_evidence_refs"], [text.pk])
        self.assertIn("retail_consultation", decision["allowed_response_acts"])
        self.assertEqual(validate_turn_response(decision,
            "Вітаю! Це футболка «Правил немає». Оберіть, який фасон і розмір вам потрібні?",
            ["client_configuration_update"]), "")
        self.assertNotIn("checkout_proposal_create", decision["allowed_response_acts"])

    def test_photo_and_greeting_alone_cannot_invent_interest(self):
        text, photo = self.message("Вітаю"), self.message("", attachment_media=[{"type": "image"}])
        decision = build_turn_intent(self.customer, source_messages=[text, photo])
        self.assertFalse(decision["commerce_evidence_refs"])
        self.assertEqual(validate_turn_response(decision, "Оберіть розмір футболки."), "current_purpose_disallows_sales")

    def test_later_interest_retraction_clears_burst_evidence_until_renewal(self):
        for interest, withdrawal in (
            ("Цікавить ця футболка", "Мене більше не цікавить ця футболка"),
            ("Интересует эта футболка", "Меня больше не интересует эта футболка"),
            ("I'm interested in this T-shirt", "I'm not interested in this T-shirt"),
        ):
            with self.subTest(withdrawal=withdrawal):
                original, retracted = self.message(interest), self.message(withdrawal)
                decision = build_turn_intent(self.customer, source_messages=[original, retracted])
                self.assertEqual(decision["purpose"], "product_interest_withdrawal")
                self.assertFalse(decision["commerce_evidence_refs"])
                self.assertEqual(validate_turn_response(decision, "Оберіть розмір футболки."), "current_purpose_disallows_sales")
                renewed = self.message(interest)
                decision = build_turn_intent(self.customer, source_messages=[original, retracted, renewed])
                self.assertEqual(decision["purpose"], "product_inquiry")
                self.assertEqual(decision["commerce_evidence_refs"], [renewed.pk])

    def test_later_order_refusal_and_service_hold_keep_priority(self):
        interest = self.message("Цікавить ця футболка")
        refusal = self.message("Не хочу нічого замовляти")
        decision = build_turn_intent(self.customer, source_messages=[interest, refusal])
        self.assertEqual(decision["purpose"], "purchase_refusal")
        self.assertFalse(decision["commerce_evidence_refs"])
        with patch("management.services.ig_service_complaints.promotion_service_hold_reason", return_value="service_review_open"):
            held = build_turn_intent(self.customer, source_messages=[interest])
        self.assertEqual(held["purpose"], "support")
        self.assertFalse(held["commerce_evidence_refs"])
        self.assertEqual(validate_turn_response(held, "Оберіть розмір.", ["client_configuration_update"]), "service_complaint_disallows_promotion")

    def test_inquiry_text_still_fails_independent_purchase_gate(self):
        from management.services.instagram_bot import payment_link_allowed

        for text in ("Вітаю\nЦікавить ця футболка", "Интересует эта футболка", "I'm interested in this T-shirt"):
            with self.subTest(text=text):
                self.assertFalse(payment_link_allowed(self.customer, {"product": 112, "size": "M"}, text))
