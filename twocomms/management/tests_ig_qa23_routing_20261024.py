"""Own trade and production requests reach the reviewed published modules."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from management import tests_ig_turn_context_integration as native
from management.knowledge.scenarios import reviewed_scenario_values
from management.models import BotInstruction
from management.services.bot_instruction_routing import turn_triggers
from management.services.bot_playbooks import active_instruction_selection
from management.services.bot_sales_classifier import is_creator_collaboration_offer
from management.services.ig_business_cooperation import extract_business_cooperation
from management.services.ig_policy_publication import load_active_policy_snapshot
from management.services.ig_revision_outbox import PublicationBinding
from management.tests_ig_policy_helpers import publish_current_instructions


SHOP = "I run a shop Please confirm a guaranteed20percent rate payouts everyFriday and dispatch today before I send any orders"
CONTENT = "Я пропоную платний тестовий контент для бренду Це оплата моєї зйомки чи купівля зразка А якщо ви даєте річ безкоштовно для фото її треба повернути чи це подарунок"
PARTICIPATION = "Мені 16 років хочу взяти участь у зйомці та отримати зразок або оплату Можна погодити все тут без батьків"


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=2400)
class NativeOwnCooperationRoutingTests(TestCase):
    publish = native.TurnContextConsumerIntegrationTests.publish
    fixture = native.TurnContextConsumerIntegrationTests.fixture

    def setUp(self):
        native.TurnContextConsumerIntegrationTests.setUp(self)
        for target in ("management.services.instagram_bot._provider_http",
                "management.services.call_ai_analysis.gemini_generate_text"):
            guard = patch(target, side_effect=AssertionError("routing must perform zero provider I/O"))
            observed = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(observed.assert_not_called)
        for identity, key in ((22, "collaboration"), (26, "public_cooperation_uk"),
                (27, "public_cooperation_ru"), (28, "public_cooperation_en")):
            values = reviewed_scenario_values(key)
            BotInstruction.objects.update_or_create(pk=identity, defaults={
                "title": values["title"], "body": values["body"], "is_active": values["active"],
                "intent_tags": ",".join(values["tags"]), "trigger_codes": values.get("triggers", []),
                "priority": values["priority"], "locale": values["locale"],
                "trust_scope": values["trust_scope"], "allowed_actions": values["allowed_actions"],
                "programme_metadata": values["programme_metadata"],
                "reviewed_source": values.get("reviewed_source", {})})
        publish_current_instructions()
        self.settings_row.refresh_from_db()
        self.publication = self.settings_row.active_instruction_publication
        self.publication_snapshot = load_active_policy_snapshot(self.settings_row)
        self.publication_binding = PublicationBinding(self.publication.pk, self.publication.version,
            self.publication.snapshot_hash)

    def selected(self, text):
        fixture = self.fixture(text)
        self.assertEqual(fixture.source.text, text)
        selection = active_instruction_selection(fixture.customer, turn_text=fixture.prepared.current_text,
            publication_snapshot=self.publication_snapshot,
            captured_tags=fixture.prepared.policy_inputs.tags, budget_chars=48000)
        from management.services.instagram_bot import assemble_system_instruction
        metadata = {}
        assembled = assemble_system_instruction(self.settings_row, client=fixture.customer,
            captured_client_state=fixture.prepared.state,
            captured_dynamic_notes=fixture.prepared.dynamic_notes,
            captured_policy_tags=fixture.prepared.policy_inputs.tags,
            captured_knowledge_language=fixture.prepared.policy_inputs.knowledge_language,
            turn_text=fixture.prepared.current_text, turn_note=fixture.prepared.context.turn_note,
            instruction_publication=self.publication_snapshot, compiled_metadata=metadata)
        self.assertEqual(set(selection.selected_ids), set(metadata["instruction_selection"]["selected_ids"]))
        self.assertTrue(assembled)
        self.assertFalse(fixture.customer.checkout_proposals.exists())
        return selection, fixture

    def test_existing_own_shop_with_concrete_trade_terms_selects_business_publication(self):
        for text in (SHOP, "I run a shop. What rate do you offer?"):
            with self.subTest(text=text):
                selection, fixture = self.selected(text)
                self.assertTrue(extract_business_cooperation(text))
                self.assertTrue({"business_cooperation", "collaboration"}.issubset(turn_triggers(text)))
                self.assertTrue({"instruction:22", "instruction:28"}.issubset(selection.required_ids))
                self.assertFalse({"instruction:26", "instruction:27"}.intersection(selection.selected_ids))
                self.assertIn("collaboration", fixture.prepared.policy_inputs.tags)

    def assert_creator_only(self, text):
        selection, _ = self.selected(text)
        self.assertTrue(is_creator_collaboration_offer(text))
        self.assertFalse(extract_business_cooperation(text))
        self.assertIn("collaboration", turn_triggers(text))
        self.assertIn("instruction:22", selection.required_ids)
        self.assertFalse({"instruction:26", "instruction:27", "instruction:28"}.intersection(selection.selected_ids))

    def test_own_paid_test_content_without_profession_selects_creator_publication(self):
        self.assert_creator_only(CONTENT)

    def test_own_shoot_participation_request_selects_creator_without_trade(self):
        self.assert_creator_only(PARTICIPATION)

    def test_quoted_reported_historical_and_withdrawn_requests_do_not_open_routes(self):
        for text in ('"' + SHOP + '"', '«' + CONTENT + '»', '«' + PARTICIPATION + '»',
                "My friend said " + SHOP, "Раніше я пропонувала платний контент для бренду.",
                SHOP + ". I no longer want partnership.",
                CONTENT + ". Я більше не пропоную контент для бренду.",
                PARTICIPATION + ". Не хочу взяти участь у зйомці."):
            with self.subTest(text=text):
                selection, _ = self.selected(text)
                self.assertFalse({"collaboration", "business_cooperation"}.intersection(turn_triggers(text)))
                self.assertFalse({"instruction:22", "instruction:26", "instruction:27", "instruction:28"}.intersection(selection.selected_ids))

    def test_retail_photos_and_shop_mentions_are_not_cooperation_offers(self):
        for text in ("I run a shop. I want one shirt for myself. What is the price?",
                "I run a shop. Please rate my shirt.",
                "I saw your shop. Can you send photos of the black shirt?",
                "Покажіть фото футболки для мого хлопця.",
                "Мені 16 років. Чи можна отримати фото розміру M?",
                "Я можу отримати фото вашого бренду?", "Send me photos of your brand.",
                "I offer no content for your brand; I only want a shirt for myself.",
                "Я пропоную не контент для бренду, а хочу футболку для себе."):
            with self.subTest(text=text):
                selection, _ = self.selected(text)
                self.assertFalse(is_creator_collaboration_offer(text))
                self.assertFalse(extract_business_cooperation(text))
                self.assertFalse({"instruction:22", "instruction:26", "instruction:27", "instruction:28"}.intersection(selection.selected_ids))

    def test_direct_work_keeps_collaboration_without_trade_module(self):
        for text in ("Можу відповідати клієнтам у Direct щодо товарів і доставки.",
                "I run a shop. I can answer customers in Direct. What are your rates and payouts for this work?",
                "I run a shop. I offer content for your brand. What are your rates and payouts for a photo shoot?"):
            with self.subTest(text=text):
                selection, _ = self.selected(text)
                self.assertFalse(extract_business_cooperation(text))
                self.assertIn("instruction:22", selection.required_ids)
                self.assertFalse({"instruction:26", "instruction:27", "instruction:28"}.intersection(selection.selected_ids))

    def test_explicit_mixed_resale_and_service_keeps_both_required_routes(self):
        text = "I run a shop. I want to resell your shirts. I can answer customers in Direct. What are your rates and payouts for this work?"
        selection, _ = self.selected(text)
        self.assertTrue(extract_business_cooperation(text))
        self.assertTrue({"instruction:22", "instruction:28"}.issubset(selection.required_ids))
