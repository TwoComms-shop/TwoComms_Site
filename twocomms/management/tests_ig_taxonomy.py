from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from management.models import IgClient, IgConversationAnalysisSnapshot, InstagramBotMessage
from management.services.bot_sales_classifier import ANALYSIS_RULES_VERSION, _interaction_type


class InteractionTaxonomyTests(SimpleTestCase):
    def _classify(self, text, *, intent=IgClient.Intent.UNKNOWN):
        client = SimpleNamespace(
            stage=IgClient.Stage.COLD,
            is_blocked=False,
            intent=intent,
            primary_objection=IgClient.Objection.NONE,
        )
        result = {
            "interaction_type": "unknown",
            "opt_out": False,
            "no_buy": False,
            "objection": IgClient.Objection.NONE,
            "intent": intent,
            "signals": [],
        }
        with patch("management.services.bot_payment_truth.client_has_verified_payment", return_value=False):
            return _interaction_type(client, result, text, InstagramBotMessage.Role.USER)

    def test_collaboration_wholesale_support_and_community_are_separate(self):
        self.assertEqual(self._classify("Я блогер, хочу обсудить коллаб"), "collaboration")
        self.assertEqual(self._classify("Нужен опт для магазина"), "wholesale_b2b")
        self.assertNotEqual(self._classify("В каком магазине вы находитесь?"), "wholesale_b2b")
        self.assertEqual(self._classify("Есть проблема: хочу обмен"), "support_complaint")
        self.assertEqual(self._classify("Ахаха, очень круто"), "community_casual")

    def test_missing_delivery_support_variants_are_recognized(self):
        for phrase in (
            "Замовлення не прийшло",
            "Посылка не пришла",
            "Мне не доставили заказ",
            "Я не отримав товар",
            "Заказ не получен",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(self._classify(phrase), "support_complaint")

    def test_unrelated_negative_phrases_are_not_support_complaints(self):
        for phrase in (
            "Я не пришлю фото сегодня",
            "Вона не прийшла на зустріч",
            "Ви не отримали оплату?",
            "Коли прийшов новий товар?",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotEqual(self._classify(phrase), "support_complaint")

    def test_taxonomy_rules_version_tracks_semantic_change(self):
        self.assertEqual(ANALYSIS_RULES_VERSION, "2026-07-24.v4")
