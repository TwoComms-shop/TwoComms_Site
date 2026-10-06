"""Тести Phase 4 / Tasks 14-15 — playbook продавця + інжект інструкцій/посилань."""
from importlib import import_module
from unittest.mock import patch

from django.apps import apps
from django.test import SimpleTestCase, TestCase

from management.models import (
    BotInstruction,
    BotQuickLink,
    DEFAULT_BOT_SYSTEM_PROMPT,
    InstagramBotSettings,
)
from management.services import instagram_bot as bot


class SystemPromptMigrationTests(TestCase):
    def test_fixed_prepayment_rules_are_replaced_without_losing_custom_text(self):
        old_prepayment_rule = (
            "• Передоплата 200 грн (решта — накладеним при отриманні) можлива; згадуй коротко, "
            "деталі (навіщо передоплата) пояснюй лише якщо запитають."
        )
        old_paylink_rule = (
            "• [PAYLINK:full] або [PAYLINK:prepay] — коли клієнт підтвердив товар і готовий "
            "платити (повна оплата / передоплата 200). Система сформує і надішле посилання."
        )
        custom_suffix = "ОСОБИСТА ІНСТРУКЦІЯ МЕНЕДЖЕРА: зберегти дослівно."
        settings = InstagramBotSettings.objects.create(
            system_prompt="\n".join((old_prepayment_rule, old_paylink_rule, custom_suffix))
        )

        migration = import_module(
            "management.migrations.0108_alter_instagrambotsettings_system_prompt"
        )
        migration.replace_fixed_prepayment_instructions(apps, None)

        settings.refresh_from_db()
        self.assertNotIn(old_prepayment_rule, settings.system_prompt)
        self.assertNotIn(old_paylink_rule, settings.system_prompt)
        self.assertIn("точну суму", settings.system_prompt)
        self.assertIn("[PAYMENT:сума]", settings.system_prompt)
        self.assertIn(custom_suffix, settings.system_prompt)


class PlaybookPromptTests(SimpleTestCase):
    def test_prompt_has_structured_control_protocol(self):
        p = DEFAULT_BOT_SYSTEM_PROMPT
        self.assertIn("JSON", p)
        self.assertIn("reply_text", p)
        self.assertIn("controls", p)
        for token in ["[STAGE:", "[MANAGER]", "[PAYLINK:", "[ORDER]", "[SPAM]"]:
            self.assertNotIn(token, p)

    def test_prompt_has_sales_and_safety_rules(self):
        p = DEFAULT_BOT_SYSTEM_PROMPT.lower()
        self.assertIn("каталог", p)
        self.assertIn("передоплат", p)  # передоплата 200
        # не вивалювати всі посилання
        self.assertTrue("посилань" in p or "каталог" in p)


class QuickLinkActiveBlockTests(TestCase):
    def test_active_block_lists_links(self):
        BotQuickLink.objects.create(
            kind=BotQuickLink.Kind.SIZE_CHART, label="Розмірна сітка худі",
            url="https://ig/hl/hoodie", garment_type="hoodie",
        )
        BotQuickLink.objects.create(
            kind=BotQuickLink.Kind.CATALOG, label="Каталог футболок",
            url="https://twocomms.shop/c/tshirts", is_active=False,
        )
        block = BotQuickLink.active_block()
        self.assertIn("https://ig/hl/hoodie", block)
        self.assertNotIn("tshirts", block)  # неактивне не потрапляє


class ContextInjectionTests(TestCase):
    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_instructions_and_links_injected(self, mock_gen):
        captured = {}

        def _fake(payload, role="chat", manual_key=None, **kwargs):
            captured["payload"] = payload
            return {"parsed": "ок", "model": "x", "meta": {}}

        mock_gen.side_effect = _fake
        BotInstruction.objects.create(title="Графік", body="Працюємо щодня 10-20")
        BotQuickLink.objects.create(
            kind=BotQuickLink.Kind.SIZE_CHART, label="Сітка худі", url="https://ig/hl/h"
        )
        s = InstagramBotSettings.load()
        from management.tests_ig_policy_helpers import publish_current_instructions
        publish_current_instructions()
        s.refresh_from_db()
        bot.gemini_generate(s, [{"role": "user", "text": "привіт"}])
        sysi = captured["payload"].get("system_instruction", {}).get("parts", [{}])[0].get("text", "")
        self.assertIn("Працюємо щодня 10-20", sysi)
        self.assertIn("https://ig/hl/h", sysi)

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_complete_gemini_context_never_advertises_fixed_instagram_prepayment(self, mock_gen):
        captured = {}

        def _fake(payload, role="chat", manual_key=None, **kwargs):
            captured["payload"] = payload
            return {"parsed": "ок", "model": "x", "meta": {}}

        mock_gen.side_effect = _fake
        s = InstagramBotSettings.load()
        from management.tests_ig_policy_helpers import publish_current_instructions
        publish_current_instructions()
        s.refresh_from_db()
        bot.gemini_generate(s, [{"role": "user", "text": "як оплатити?"}])

        sysi = captured["payload"].get("system_instruction", {}).get("parts", [{}])[0].get("text", "")
        from management.services.ig_core_policy import CANONICAL_IG_CORE_POLICY
        self.assertIn(CANONICAL_IG_CORE_POLICY, sysi)
        # The reviewed core describes a capability-gated option. Dynamic
        # sources must not turn that example into an unconditional offer.
        self.assertNotIn("передоплата 200 грн", sysi.replace(CANONICAL_IG_CORE_POLICY, "").casefold())
        self.assertIn("лише коли поточний серверний capability", CANONICAL_IG_CORE_POLICY)
        self.assertIn("точна", sysi.casefold())
        self.assertIn("погоджен", sysi.casefold())


class BrandKnowledgeTests(TestCase):
    def test_brand_md_filled(self):
        from django.core.cache import cache

        from management.services.bot_knowledge import get_brand_knowledge

        cache.clear()
        kb = get_brand_knowledge()
        self.assertIn("Пошт", kb)  # Нова Пошта / Новою Поштою
        self.assertNotIn("передоплата 200 грн", kb.casefold())
        self.assertIn("погоджен", kb.casefold())
        self.assertIn("передопла", kb.lower())
        self.assertNotIn("TODO", kb)
