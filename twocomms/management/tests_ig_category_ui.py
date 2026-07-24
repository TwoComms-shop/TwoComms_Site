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

    def test_notification_telemetry_keeps_unavailable_distinct_from_zero(self):
        template = (
            Path(__file__).with_name("templates") / "management" / "bot.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "const outboxAvailable=[st.notification_failed,st.notification_pending,st.notification_unknown,st.notification_dead_letter]",
            template,
        )
        self.assertIn(": 'Дані недоступні';", template)
        self.assertIn("Стан сповіщень недоступний.", template)
        self.assertNotIn("st.notification_pending||0", template)

    def test_model_selector_uses_normalized_effective_model(self):
        template = (
            Path(__file__).with_name("templates") / "management" / "bot.html"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "{% if status.gemini_effective_model == 'gemini-3.6-flash' %}",
            template,
        )
        self.assertNotIn(
            "{% if settings.gemini_model == 'gemini-3.6-flash' %}",
            template,
        )

    def test_client_cards_use_localized_operational_labels_and_keyboard_open(self):
        template = (
            Path(__file__).with_name("templates") / "management" / "bot.html"
        ).read_text(encoding="utf-8")

        self.assertIn("row.setAttribute('role','button');", template)
        self.assertIn("row.setAttribute('tabindex','0');", template)
        self.assertIn("Заперечення: "+"'+(c.primary_objection_label||c.primary_objection)", template)
        self.assertIn("Наступний контакт: "+"'+fmt(c.next_followup_at)", template)
        self.assertIn("Попередня оцінка · "+"'+(c.buying_readiness||0)+'%", template)
        for raw_label in ("'obj: '", "'FU '", "'discount '", "'ad: '", "'legacy '"):
            self.assertNotIn(raw_label, template)

    def test_client_dom_uses_safe_external_urls_and_no_inline_avatar_handler(self):
        template = (
            Path(__file__).with_name("templates") / "management" / "bot.html"
        ).read_text(encoding="utf-8")

        self.assertIn("function safeHttpUrl(value)", template)
        self.assertIn("url.protocol==='http:'||url.protocol==='https:'", template)
        self.assertIn("data-avatar", template)
        self.assertIn("rel=\"noopener noreferrer\"", template)
        self.assertNotIn("onerror=", template)
        self.assertIn("'\"':'&quot;'", template)

    def test_visible_operator_copy_is_ukrainian_and_internal_codes_are_mapped(self):
        template = (
            Path(__file__).with_name("templates") / "management" / "bot.html"
        ).read_text(encoding="utf-8")

        for visible_label in (
            "Стан зв’язку",
            "Рівень міркування",
            "Подієва схема",
            "Резервне опитування Instagram",
            "Розмірна таблиця",
            "Ідентифікатор реклами",
            "Інтерес до товарів",
        ):
            self.assertIn(visible_label, template)
        for raw_visible in (
            ">Heartbeat<",
            ">Reasoning<",
            "Ad ID",
            "Інтерес до товарів / SKU",
            "analysis_band||c.analysis_band||'Legacy'",
        ):
            self.assertNotIn(raw_visible, template)
        self.assertIn("const reasoningLabels=", template)
        self.assertIn("const followupStatusLabels=", template)
        self.assertIn("const paymentTruthLabels=", template)

    def test_exact_technical_terms_keep_their_canonical_english_names(self):
        template = (
            Path(__file__).with_name("templates") / "management" / "bot.html"
        ).read_text(encoding="utf-8")

        for technical_label in (
            "live",
            "Server ENV",
            "Instagram Direct API · Facebook Token",
            "Gemini API · Google API Key",
            "Meta Conversions API",
            "Meta Test Event Code",
            "Checkout started",
        ):
            self.assertIn(technical_label, template)
        self.assertIn("Сигнали показують події діалогу, але самі по собі не підтверджують оплату.", template)
