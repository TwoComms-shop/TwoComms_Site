"""Focused contracts for B02.3's policy compiler producers."""
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from management.models import BotInstruction, IgClient
from management.services.ig_policy_compiler import (
    PolicyModule,
    PolicyReadinessError,
    compile_policy,
)


def _compile(**overrides):
    values = {
        "immutable_authority": [PolicyModule("authority", "NO INVENTED PAYMENTS")],
        "published_core": [PolicyModule("core", "SOLOMIIA RULE")],
        "verified_dynamic_facts": [PolicyModule("facts", "VERIFIED CHECKOUT FACT")],
        "playbooks": (),
        "knowledge": (),
        "customer_data": (),
        "budget_chars": 1_000,
        "version": "draft-1",
    }
    values.update(overrides)
    return compile_policy(**values)


class PolicyCompilerTests(SimpleTestCase):
    def test_long_optional_module_cannot_displace_mandatory_policy(self):
        compiled = _compile(
            playbooks=[PolicyModule("long-playbook", "x" * 500, priority=1)],
            budget_chars=80,
        )

        self.assertIn("NO INVENTED PAYMENTS", compiled.text)
        self.assertIn("SOLOMIIA RULE", compiled.text)
        self.assertIn("VERIFIED CHECKOUT FACT", compiled.text)
        self.assertNotIn("x" * 500, compiled.text)
        self.assertEqual(compiled.selected, ("authority", "core", "facts"))
        self.assertEqual(compiled.omitted[0].metadata(), {
            "id": "long-playbook", "reason": "budget_exhausted",
        })

    def test_order_is_mandatory_then_optional_source_classes(self):
        compiled = _compile(
            playbooks=[PolicyModule("playbook", "PLAYBOOK")],
            knowledge=[PolicyModule("knowledge", "KNOWLEDGE")],
            customer_data=[PolicyModule("customer", "CUSTOMER PRIVATE TEXT")],
        )

        self.assertEqual(compiled.text.split("\n\n"), [
            "NO INVENTED PAYMENTS", "SOLOMIIA RULE", "VERIFIED CHECKOUT FACT",
            "PLAYBOOK", "KNOWLEDGE", "CUSTOMER PRIVATE TEXT",
        ])
        metadata = compiled.metadata()
        self.assertNotIn("CUSTOMER PRIVATE TEXT", repr(metadata))
        self.assertEqual(metadata["selected_ids"][-1], "customer")

    def test_manifest_hash_changes_for_body_priority_and_tags(self):
        base = _compile(playbooks=[PolicyModule("rule", "BODY", priority=10, tags=("sales",))])
        body = _compile(playbooks=[PolicyModule("rule", "CHANGED", priority=10, tags=("sales",))])
        priority = _compile(playbooks=[PolicyModule("rule", "BODY", priority=11, tags=("sales",))])
        tags = _compile(playbooks=[PolicyModule("rule", "BODY", priority=10, tags=("service",))])

        self.assertNotEqual(base.content_hash, body.content_hash)
        self.assertNotEqual(base.content_hash, priority.content_hash)
        self.assertNotEqual(base.content_hash, tags.content_hash)

    def test_customer_context_changes_request_digest_not_reusable_policy_hash(self):
        first = _compile(customer_data=[PolicyModule("customer", "FIRST PRIVATE QUESTION")])
        second = _compile(customer_data=[PolicyModule("customer", "SECOND PRIVATE QUESTION")])

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.context_hash, second.context_hash)
        self.assertNotIn("context_hash", first.metadata())

    def test_mandatory_policy_that_exceeds_budget_is_a_readiness_error(self):
        with self.assertRaises(PolicyReadinessError) as raised:
            _compile(budget_chars=10)

        error = raised.exception
        self.assertEqual(error.code, "mandatory_policy_exceeds_budget")
        self.assertEqual(error.details["budget_chars"], 10)
        self.assertNotIn("NO INVENTED PAYMENTS", repr(error.details))

    def test_overflow_reports_entire_mandatory_cost_without_prompt_bodies(self):
        with self.assertRaises(PolicyReadinessError) as raised:
            _compile(budget_chars=10)
        details = raised.exception.details
        bodies = ["NO INVENTED PAYMENTS", "SOLOMIIA RULE", "VERIFIED CHECKOUT FACT"]
        self.assertEqual(details["mandatory_count"], 3)
        self.assertEqual(details["mandatory_total_chars"], len("\n\n".join(bodies)))
        self.assertGreater(details["mandatory_total_chars"], details["required_chars"])
        self.assertEqual(details["mandatory_module_lengths"],
            dict(zip(("authority", "core", "facts"), map(len, bodies))))
        self.assertTrue(all(body not in repr(details) for body in bodies))


class KnowledgeReadinessTests(SimpleTestCase):
    def test_unknown_approved_knowledge_language_is_an_explicit_readiness_gap(self):
        from management.services import bot_knowledge

        with self.assertRaises(bot_knowledge.KnowledgeReadinessError) as raised:
            bot_knowledge.read_knowledge_manifest("de")

        self.assertEqual(raised.exception.code, "approved_public_facts_unavailable")
        self.assertEqual(raised.exception.details["language"], "de")


class PlaybookSelectionCompatibilityTests(TestCase):
    def setUp(self):
        BotInstruction.objects.all().delete()
        self.client = IgClient.get_or_create_for_sender("policy-selection-client")

    def test_structured_selection_preserves_legacy_block_and_reason_codes(self):
        kept = BotInstruction.objects.create(
            title="Kept", body="WHOLE BODY", intent_tags="global", priority=1,
        )
        narrow = BotInstruction.objects.create(
            title="Narrow", body="SIZE ONLY", intent_tags="on:size_question", priority=2,
        )
        disabled = BotInstruction.objects.create(
            title="Disabled", body="NEVER", is_active=False, priority=3,
        )
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        from management.services.bot_playbooks import active_instruction_block, active_instruction_selection

        selection = active_instruction_selection(
            self.client, turn_text="привіт", visual_trigger_codes=["gift_candidate", "gift_candidate"],
        )

        self.assertEqual(selection.selected_ids, (f"instruction:{kept.pk}",))
        self.assertEqual(selection.visual_trigger_codes, ("gift_candidate",))
        reasons = {item.id: item.reason for item in selection.omitted}
        self.assertEqual(reasons[f"instruction:{narrow.pk}"], "not_relevant")
        self.assertEqual(reasons[f"instruction:{disabled.pk}"], "inactive")
        self.assertEqual(active_instruction_block(self.client, turn_text="привіт"), "• Kept: WHOLE BODY")

    def test_budget_omission_keeps_each_playbook_whole(self):
        first = BotInstruction.objects.create(title="First", body="a" * 30, priority=1)
        second = BotInstruction.objects.create(title="Second", body="b" * 30, priority=2)
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        from management.services.bot_playbooks import active_instruction_selection

        selection = active_instruction_selection(self.client, budget_chars=45)

        self.assertEqual(selection.selected_ids, (f"instruction:{first.pk}",))
        self.assertEqual(selection.omitted[0].id, f"instruction:{second.pk}")
        self.assertEqual(selection.omitted[0].reason, "budget_exhausted")


class LivePolicyCompilationTests(TestCase):
    def test_live_compiler_preserves_core_and_reports_oversized_optional_instruction(self):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot as bot
        optional = BotInstruction.objects.create(title="Oversized", body="x" * 60000, is_active=True)
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        metadata = {}
        prompt = bot.assemble_system_instruction(
            InstagramBotSettings(system_prompt="MANDATORY-CORE", knowledge_base="CURRENT-DIRECTIVE"),
            compiled_metadata=metadata,
        )
        self.assertTrue(prompt.startswith(bot.CANONICAL_PROMPT_AUTHORITY_POLICY))
        self.assertIn("MANDATORY-CORE", prompt)
        self.assertIn("CURRENT-DIRECTIVE", prompt)
        self.assertIn("core:live_directives", metadata["mandatory_ids"])
        self.assertIn({"id": f"instruction:{optional.pk}", "reason": "budget_exhausted"}, metadata["omitted"])
        self.assertNotIn("MANDATORY-CORE", str(metadata))
        from management.services.gemini_accounting_contract import (
            sanitize_request_policy_manifest,
        )

        self.assertEqual(sanitize_request_policy_manifest(metadata), metadata)

    def test_missing_required_knowledge_stops_before_provider_call(self):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot as bot
        from management.services.bot_knowledge import KnowledgeReadinessError
        from management.tests_ig_policy_helpers import (
            ensure_test_instruction_publication,
        )

        ensure_test_instruction_publication()
        failure = {}
        with patch("management.services.bot_knowledge.read_knowledge_manifest", side_effect=KnowledgeReadinessError("knowledge_directory_missing", "missing")), patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            response = bot.gemini_generate(
                InstagramBotSettings.load(), [{"role": "user", "text": "Вітаю"}],
                failure_context=failure,
            )
        self.assertIsNone(response)
        self.assertEqual(failure["policy_readiness"], "knowledge_directory_missing")
        provider.assert_not_called()

    def test_oversized_mandatory_publication_is_rejected(self):
        from management.services.instagram_bot import validate_core_policy_for_publication
        with self.assertRaisesRegex(PolicyReadinessError, "does not fit"):
            validate_core_policy_for_publication("x" * 48000)
