"""Required declarations through immutable publication, selection and generation."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from management.services.bot_playbooks import active_instruction_selection
from management.services.gemini_accounting_contract import (
    RequestPolicyManifestError, sanitize_request_policy_manifest,
)
from management.services.ig_policy_compiler import PolicyModule, compile_policy
from management.services.ig_policy_publication import (
    ActivePolicySnapshot, PUBLICATION_COMPILER_VERSION, PolicyPublicationError,
    select_policy_snapshot, snapshot_from_rows, snapshot_hash,
)
from management.services.ig_required_policy import REQUIRED_KEY, SHOOTING_METADATA
from management import tests_ig_request_policy_manifest as manifest_fixtures


def row(identity, **overrides):
    values = dict(pk=identity, title="", body="Reviewed guidance", is_active=True,
                  priority=100, locale="all", trust_scope="public_policy",
                  intent_tags="", trigger_codes=[], allowed_actions=[],
                  programme_metadata={}, reviewed_source={})
    values.update(overrides)
    return SimpleNamespace(**values)


def publication(*rows):
    snapshot = snapshot_from_rows(rows)
    return ActivePolicySnapshot(3, 7, snapshot_hash(snapshot), PUBLICATION_COMPILER_VERSION, snapshot)


class RequiredPublicationSelectionTests(SimpleTestCase):
    def test_empty_declaration_preserves_legacy_snapshot_hash_and_selection_order(self):
        original = publication(row(2, body="abc"), row(10, body="def"))
        empty = publication(row(2, body="abc", programme_metadata={REQUIRED_KEY: []}), row(10, body="def"))
        self.assertEqual(original.snapshot, empty.snapshot)
        self.assertEqual(original.snapshot_hash, empty.snapshot_hash)
        selected = select_policy_snapshot(empty.snapshot, budget_chars=5,
                                          active_triggers={"size_question"})
        self.assertEqual(selected["selected_ids"], ["instruction:2"])
        self.assertEqual(selected["required_policy"]["selected_ids"], selected["selected_ids"])
        self.assertEqual(selected["required_policy"]["omitted"], selected["omitted"])
        self.assertEqual(selected["required_policy"]["coverage_status"], "legacy_unconfigured")

    def test_declared_required_reserves_selector_and_final_compiler_budget(self):
        bound = publication(row(1, body="optional optional", priority=1),
                            row(2, body="size rule", priority=999,
                                intent_tags="on:size_question", programme_metadata={REQUIRED_KEY: ["size"]}))
        selection = active_instruction_selection(turn_text="Який розмір мені підійде?",
                         publication_snapshot=bound, captured_tags={"uk"}, budget_chars=16)
        self.assertEqual(selection.required_ids, ("instruction:2",))
        self.assertEqual(selection.selected_ids, ("instruction:2",))
        compiled = compile_policy(immutable_authority=[PolicyModule("authority:server", "core")],
                                  published_core=[], verified_dynamic_facts=[],
                                  playbooks=selection.policy_inputs(), budget_chars=17)
        self.assertEqual(compiled.required_ids, ("instruction:2",))
        self.assertIn("size rule", compiled.text)
        self.assertNotIn("optional", compiled.text)
        self.assertEqual(selection.metadata()["required_policy"]["coverage_status"], "declared_covered")

    def test_declared_missing_relevance_private_inactive_and_wrong_locale_fail_named(self):
        variants = ({"intent_tags": "on:price_question"}, {"trust_scope": "operator_only"},
                    {"is_active": False}, {"locale": "ru"})
        for override in variants:
            with self.subTest(override=override):
                bound = publication(row(2, programme_metadata={REQUIRED_KEY: ["size"]}, **override))
                with self.assertRaises(PolicyPublicationError) as caught:
                    active_instruction_selection(turn_text="Який розмір мені підійде?",
                        publication_snapshot=bound, captured_tags={"uk"})
                self.assertEqual(caught.exception.code, "required_scenario_module_missing")

    def test_locale_translation_is_not_a_second_required_body(self):
        bound = publication(row(1, body="English size", locale="en", programme_metadata={REQUIRED_KEY: ["size"]}),
                            row(2, body="Russian size", locale="ru", programme_metadata={REQUIRED_KEY: ["size"]}))
        selection = active_instruction_selection(turn_text="What size fits me?", publication_snapshot=bound,
                                                 captured_tags={"en"})
        self.assertEqual(selection.required_ids, ("instruction:1",))
        self.assertEqual(selection.metadata()["omitted"], [{"id": "instruction:2", "reason": "locale_mismatch"}])

    def test_greeting_ordinary_price_negation_history_quote_do_not_activate_declaration(self):
        bound = publication(row(2, intent_tags="on:price_objection",
                                programme_metadata={REQUIRED_KEY: ["objection"]}))
        for text in ("Привіт", "Скільки коштує футболка?", "Це не дорого.",
                     "Раніше було дорого.", '«Дорого» — цитата.'):
            with self.subTest(text=text):
                selection = active_instruction_selection(turn_text=text, publication_snapshot=bound,
                                                         captured_tags={"uk", "size", "fit"})
                self.assertEqual(selection.required_ids, ())
                self.assertNotIn("required_policy", selection.metadata())

    def test_current_captured_tags_activate_custom_print_without_stale_crm_reread(self):
        bound = publication(row(2, intent_tags="custom_print", programme_metadata={REQUIRED_KEY: ["custom_print"]}))
        client = SimpleNamespace(language="ru", intent="creator", primary_objection="price")
        with patch("management.services.bot_playbooks.tags_for_client", side_effect=AssertionError("uncaptured CRM read")):
            selection = active_instruction_selection(client, turn_text="Привіт", publication_snapshot=bound,
                                                     captured_tags={"uk", "custom_print"})
        self.assertEqual(selection.required_ids, ("instruction:2",))
        metadata = selection.required_policy
        metadata["covered_scenarios"].append("ugc")
        self.assertEqual(selection.required_policy["covered_scenarios"], ["custom_print"])

    def test_required_selector_overflow_is_named_instead_of_optional_omission(self):
        bound = publication(row(2, body="size rule", programme_metadata={REQUIRED_KEY: ["size"]}))
        with self.assertRaises(PolicyPublicationError) as caught:
            active_instruction_selection(turn_text="Який розмір?", publication_snapshot=bound,
                                         captured_tags={"uk"}, budget_chars=4)
        self.assertEqual(caught.exception.code, "required_policy_exceeds_budget")

    def test_shooting_adapter_uses_subset_and_keeps_manager_entitlement_boundary(self):
        from management.services.ig_prize_programme import active_shooting_prize_programme
        legacy = publication(row(2, intent_tags="programme:shooting_prize", programme_metadata=SHOOTING_METADATA))
        declared = publication(row(2, intent_tags="programme:shooting_prize",
                                   programme_metadata={**SHOOTING_METADATA, REQUIRED_KEY: ["ugc"]}))
        before = active_shooting_prize_programme(publication_snapshot=legacy)
        after = active_shooting_prize_programme(publication_snapshot=declared)
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertEqual(after.instruction, before.instruction)
        self.assertTrue(after.manager_required)
        self.assertFalse(after.confirmed_visual_sample)
        self.assertNotEqual(after.version, before.version)


class RequiredManifestTests(SimpleTestCase):
    def manifest(self, *, declared=True):
        result = manifest_fixtures.policy_manifest()
        result["required_ids"] = ["instruction:17"] if declared else []
        result["instruction_selection"]["required_policy"] = {
            "ready": True, "reason": "required_policy_admitted" if declared else "required_policy_legacy",
            "selected_ids": ["instruction:17"], "required_ids": result["required_ids"],
            "omitted": deepcopy(result["instruction_selection"]["omitted"]),
            "declared_scenarios": ["size"] if declared else [],
            "covered_scenarios": ["size"] if declared else [],
            "undeclared_scenarios": [] if declared else ["size"],
            "coverage_status": "declared_covered" if declared else "legacy_unconfigured",
            "diagnostics": [] if declared else ["legacy_required_scenarios_undeclared"],
            "required_chars": 12 if declared else 0, "used_chars": 12,
        }
        return result

    def test_legacy_manifest_absence_is_exact_and_declared_or_legacy_diagnostics_roundtrip(self):
        legacy = manifest_fixtures.policy_manifest()
        self.assertEqual(sanitize_request_policy_manifest(legacy), legacy)
        self.assertNotIn("required_ids", sanitize_request_policy_manifest(legacy))
        for declared in (True, False):
            raw = self.manifest(declared=declared)
            self.assertEqual(sanitize_request_policy_manifest(raw), raw)

    def test_required_metadata_rejects_text_unknown_scenarios_and_inconsistent_coverage(self):
        changes = ({"policy_body": "secret"}, {"declared_scenarios": ["money"]},
                   {"reason": "free text"}, {"ready": 1}, {"coverage_status": []},
                   {"covered_scenarios": []}, {"required_ids": ["instruction:18"]},
                   {"required_chars": 13}, {"required_chars": 0},
                   {"diagnostics": ["private customer text"]}, {"selected_ids": []})
        for change in changes:
            with self.subTest(change=change):
                raw = self.manifest()
                raw["instruction_selection"]["required_policy"].update(change)
                with self.assertRaises(RequestPolicyManifestError):
                    sanitize_request_policy_manifest(raw)

    def test_required_reservation_and_existing_publication_binding_cannot_be_lost(self):
        for mutation in ("required_ids", "publication", "omission"):
            with self.subTest(mutation=mutation):
                raw = self.manifest()
                if mutation == "required_ids":
                    raw.pop("required_ids")
                elif mutation == "publication":
                    raw["instruction_selection"]["publication_version"] += 1
                else:
                    raw["instruction_selection"]["required_policy"]["omitted"] = []
                with self.assertRaises(RequestPolicyManifestError):
                    sanitize_request_policy_manifest(raw)


class RequiredPolicyRuntimeTests(TestCase):
    def setUp(self):
        from management.models import BotInstruction, InstagramBotSettings
        BotInstruction.objects.all().delete()
        self.settings = InstagramBotSettings.load()

    def create_instruction(self, **values):
        from management.models import BotInstruction
        from management.tests_ig_policy_helpers import publish_current_instructions
        instruction = BotInstruction.objects.create(title="Reviewed size", body="Reviewed size guidance",
            is_active=True, programme_metadata={REQUIRED_KEY: ["size"]}, **values)
        publish_current_instructions()
        self.settings.refresh_from_db()
        return instruction

    def generate(self, **kwargs):
        from management.services import instagram_bot
        # A sealed customer invocation has a captured routing audience. Omitting
        # both client and captured tags intentionally invokes the old admin
        # preview path, which includes every instruction regardless of routing.
        kwargs.setdefault("captured_policy_tags", {"uk", "global", "core", "sales"})
        return instagram_bot.gemini_generate(self.settings,
            [{"role": "user", "text": "Який розмір мені підійде?"}], **kwargs)

    def test_missing_required_instruction_stops_real_generation_before_provider(self):
        self.create_instruction(intent_tags="on:price_question")
        failure = {}
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, patch("requests.post") as http:
            result = self.generate(failure_context=failure)
        self.assertIsNone(result)
        self.assertEqual(failure["policy_readiness"], "required_scenario_module_missing")
        provider.assert_not_called()
        http.assert_not_called()

    @override_settings(IG_BOT_POLICY_BUDGET_CHARS=1)
    def test_required_overflow_stops_real_generation_before_provider(self):
        self.create_instruction(intent_tags="on:size_question")
        failure = {}
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, patch("requests.post") as http:
            result = self.generate(failure_context=failure)
        self.assertIsNone(result)
        self.assertEqual(failure["policy_readiness"], "required_policy_exceeds_budget")
        provider.assert_not_called()
        http.assert_not_called()

    def test_actual_assembly_provider_payload_and_manifest_use_same_required_module(self):
        instruction = self.create_instruction(intent_tags="on:size_question")
        provider_result = {"parsed": {"reply_text": "Допоможу підібрати розмір.", "controls": []},
                           "model": "gemini-test", "usage": {}, "meta": {"key": "test", "reasoning_task": "customer_chat"}}
        with patch("management.services.call_ai_analysis.gemini_generate_text", return_value=provider_result) as provider:
            result = self.generate()
        self.assertTrue(result.valid)
        manifest = provider.call_args.kwargs["request_policy_manifest"]
        identity = f"instruction:{instruction.pk}"
        self.assertEqual(manifest["required_ids"], [identity])
        self.assertEqual(manifest["instruction_selection"]["required_policy"]["required_ids"], [identity])
        self.assertIn("Reviewed size guidance", provider.call_args.args[0]["system_instruction"]["parts"][0]["text"])
        self.assertEqual(sanitize_request_policy_manifest(manifest), manifest)

    def test_draft_changes_do_not_change_capture_and_rollback_restores_declared_snapshot(self):
        from management.tests_ig_policy_helpers import publish_current_instructions
        from management.services.ig_policy_publication import load_active_policy_snapshot, rollback_instruction_policy
        instruction = self.create_instruction(intent_tags="on:size_question")
        original = load_active_policy_snapshot()
        instruction.programme_metadata = {}
        instruction.save(update_fields=["programme_metadata", "updated_at"])
        captured = active_instruction_selection(turn_text="Який розмір?", publication_snapshot=original)
        self.assertEqual(captured.required_ids, (f"instruction:{instruction.pk}",))
        changed = publish_current_instructions()
        self.assertNotEqual(changed.snapshot_hash, original.snapshot_hash)
        restored = rollback_instruction_policy(target_publication_id=original.publication_id,
            expected_head_id=changed.publication_id, expected_head_hash=changed.snapshot_hash)
        self.assertEqual(restored.publication.snapshot_hash, original.snapshot_hash)
        selected = active_instruction_selection(turn_text="Який розмір?")
        self.assertEqual(selected.required_ids, captured.required_ids)
