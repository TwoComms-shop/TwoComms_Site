from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from management.models import IgClient, IgConversationAnalysisSnapshot, InstagramBotMessage
from management.services.bot_sales_classifier import (
    ANALYSIS_RULES_VERSION,
    _interaction_type,
    observed_stage_target,
)


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

    def test_observed_payment_intent_advances_paused_conversation_to_checkout(self):
        self.assertEqual(
            observed_stage_target(
                IgClient.Stage.NEW,
                intent=IgClient.Intent.PAYMENT,
                has_size=True,
            ),
            IgClient.Stage.CHECKOUT,
        )

    def test_observed_stage_never_claims_paid_without_verified_payment(self):
        self.assertEqual(
            observed_stage_target(
                IgClient.Stage.CHECKOUT,
                intent=IgClient.Intent.PAYMENT,
            ),
            IgClient.Stage.CHECKOUT,
        )

    def test_manager_takeover_signal_does_not_advance_funnel(self):
        self.assertEqual(
            observed_stage_target(
                IgClient.Stage.NEW,
                signal_types=["manager_takeover"],
            ),
            IgClient.Stage.NEW,
        )

    def test_manager_led_checkout_can_advance_without_reply_automation(self):
        self.assertEqual(
            observed_stage_target(
                IgClient.Stage.LEAD_TO_MANAGER,
                intent=IgClient.Intent.PAYMENT,
            ),
            IgClient.Stage.CHECKOUT,
        )

    def test_cold_stage_stays_terminal_until_verified_payment(self):
        self.assertEqual(
            observed_stage_target(IgClient.Stage.COLD, intent=IgClient.Intent.PAYMENT),
            IgClient.Stage.COLD,
        )
        self.assertEqual(
            observed_stage_target(
                IgClient.Stage.COLD,
                intent=IgClient.Intent.PAYMENT,
                verified_payment=True,
            ),
            IgClient.Stage.PAID,
        )
