from pathlib import Path

from django.test import SimpleTestCase

from management.services.bot_sales_classifier import ANALYSIS_RULES_VERSION
from management.bot_views import _interaction_tone


class InteractionCategoryUiContractTests(SimpleTestCase):
    def test_operator_tones_are_semantic_and_payment_safe(self):
        self.assertEqual(_interaction_tone("support_complaint"), "support")
        self.assertEqual(_interaction_tone("wholesale_b2b"), "business")
        self.assertEqual(_interaction_tone("collaboration"), "business")
        self.assertEqual(_interaction_tone("high_intent"), "intent")
        self.assertEqual(_interaction_tone("paid_order_waiting"), "success")
        self.assertEqual(_interaction_tone("explicit_no_buy"), "negative")
        self.assertEqual(_interaction_tone("reaction_only"), "neutral")
        self.assertEqual(_interaction_tone(""), "neutral")

    def test_ui_contract_uses_current_rules_version(self):
        self.assertEqual(ANALYSIS_RULES_VERSION, "2026-07-24.v4")

    def test_overview_explanation_uses_runtime_model_truth(self):
        template = (
            Path(__file__).with_name("templates") / "management" / "bot.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="bot-explainer-model"', template)
        self.assertIn("const activeModel=st.last_gemini_model||st.gemini_effective_model||'';", template)
        self.assertIn("explainerModel.textContent=activeModel||'поточну перевірену модель';", template)
        self.assertNotIn("<b>{{ settings.gemini_model }}</b>", template)
