"""Owner-reviewed preparation facts reach the native request without provider I/O."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from management.services.approved_public_facts import (
    APPROVED_PUBLIC_FACTS_VERSION, approved_public_fact_manifest, approved_public_facts,
    knowledge_topics,
)
from management.services.bot_knowledge import read_knowledge_manifest


class MadeToOrderPublicationTests(SimpleTestCase):
    def test_reviewed_preparation_fact_has_equal_scope_and_lineage_in_every_language(self):
        for language in ("uk", "ru", "en"):
            with self.subTest(language=language):
                manifest = approved_public_fact_manifest(language)
                fact = next(row for row in manifest["facts"] if row.key == "dispatch")
                self.assertEqual(manifest["version"], APPROVED_PUBLIC_FACTS_VERSION)
                self.assertEqual(fact.source_ref, "owner:2026-10-21:made-to-order")
                self.assertEqual(fact.review_status, "reviewed:owner-brief:2026-10-21")
                self.assertEqual(fact.scope, "current_tshirts_and_hoodies_preparation_after_confirmed_payment")
                self.assertEqual(fact.version, "2026-10-21.owner-made-to-order.1")
                self.assertIn("1–3", fact.public_text)
                self.assertIn(fact.body, read_knowledge_manifest(language, topics=("dispatch",)).text)
                self.assertNotIn("363", fact.body)
                self.assertNotIn("2XL", fact.body)

    def test_fragmented_departure_selects_reviewed_preparation_without_stock_or_arrival_right(self):
        sources = [{"role": "user", "text": text} for text in (
            "It is a gift for my boyfriend", "2XL", "I leave tomorrow at 10")]
        selected = read_knowledge_manifest("en", topics=knowledge_topics(sources))
        module = next(row for row in selected.modules if row.id == "approved_public:en:dispatch")
        self.assertIn("print current TwoComms T-shirts and hoodies to order after payment is confirmed", module.body)
        self.assertIn("do not keep finished printed garments ready in stock", module.body)
        self.assertIn("Preparation usually takes 1–3 days after payment confirmation", module.body)
        self.assertIn("varies with the season", module.body)
        self.assertIn("not carrier transit time", module.body)
        self.assertIn("deadline feasibility need a manager decision for this case", module.body)
        self.assertNotIn("ordinary_dispatch_after_confirmed_payment", module.body)

    def test_scenario_update_preserves_five_existing_admission_declarations(self):
        from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, reviewed_scenario_values

        keys = ("custom_print", "collaboration", "ugc", "size", "objection")
        self.assertEqual(tuple(row["key"] for row in SCENARIO_INSTRUCTIONS), keys)
        for key in keys:
            values = reviewed_scenario_values(key)
            self.assertEqual(values["programme_metadata"]["required_for_scenarios"], [key])
            self.assertEqual(values["allowed_actions"], [])
        detached = reviewed_scenario_values("objection")
        detached["programme_metadata"]["required_for_scenarios"].append("forged")
        self.assertEqual(reviewed_scenario_values("objection")["programme_metadata"]["required_for_scenarios"], ["objection"])


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=2400, GEMINI_NONLIVE_ADMISSION_MODE="shadow")
class MadeToOrderNativeRequestTests(TestCase):
    def setUp(self):
        from management import tests_ig_turn_context_integration as fixtures
        fixtures.TurnContextConsumerIntegrationTests.setUp(self)

    def publish(self, **kwargs):
        from management import tests_ig_turn_context_integration as fixtures
        return fixtures.TurnContextConsumerIntegrationTests.publish(self, **kwargs)

    def test_native_sealed_capture_compilation_payload_and_response_validator_share_reviewed_fact(self):
        from management import tests_ig_turn_context_integration as fixtures
        from management.services import instagram_bot as bot
        from management.services.call_ai_analysis import CallAIAnalysisError
        from management.services.ig_core_policy import CORE_POLICY_VERSION, CORE_POLICY_SHA256
        from management.services.ig_reply_language import resolve_own_source_reply_language
        from management.services.ig_turn_capture import detached_payload

        fixture = fixtures.TurnContextConsumerIntegrationTests.fixture(self,
            "Please reply in English. Are your current T-shirts made to order? I leave tomorrow at 10.",
            reduction=False)
        calls, validations = [], []
        fact = next(row for row in approved_public_facts("en") if row.key == "dispatch")

        def transport(payload, **kwargs):
            calls.append((deepcopy(payload), deepcopy(kwargs["request_policy_manifest"])))
            validations.append(kwargs["result_validator"]({"reply_text": fact.public_text,
                "controls": []}, usage={}))
            raise CallAIAnalysisError("offline proof ends before provider or customer delivery")

        prepared = fixture.prepared
        language = resolve_own_source_reply_language(sources=fixture.revision.bundle_snapshot["sources"],
            history=detached_payload(prepared.context.captured_history), profile_language=fixture.customer.language)
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=transport):
            bot.gemini_generate(self.settings_row, list(prepared.history), client=fixture.customer,
                routing_decision=prepared.context.decision, generation_boundary=fixture.boundary,
                turn_note=prepared.context.turn_note, memory_note=prepared.context.memory_note,
                context_note=prepared.context.context_note, captured_client_state=prepared.state,
                captured_dynamic_notes=prepared.dynamic_notes, captured_turn_text=prepared.current_text,
                captured_policy_tags=prepared.policy_inputs.tags,
                captured_knowledge_language=prepared.policy_inputs.knowledge_language,
                captured_knowledge_selectors=prepared.policy_inputs.knowledge_selectors,
                captured_reply_language=language,
                request_context_metadata=prepared.request_metadata, failure_context={},
                turn_media_binding=fixture.collection.binding, turn_media_context=[],
                deadline_at=fixture.revision.overall_deadline - timedelta(seconds=5))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(validations), 1)
        self.assertTrue(validations[0].valid, validations[0].reason_codes)
        payload, manifest = calls[0]
        system = payload["system_instruction"]["parts"][0]["text"]
        self.assertIn(fact.public_text, system)
        self.assertIn("Reply in English.", system)
        self.assertEqual(manifest["core"]["version"], CORE_POLICY_VERSION)
        self.assertEqual(manifest["core"]["prompt_hash"], CORE_POLICY_SHA256)
        selectors = prepared.policy_inputs.knowledge_selectors
        self.assertEqual(manifest["knowledge_hash"], read_knowledge_manifest("en",
            topics=selectors["topics"], base_keys=selectors["base_keys"]).content_hash)
        self.assertIn("approved_public:en:dispatch", manifest["selected_ids"])
