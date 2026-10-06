"""Pure required-policy contracts: no Django, ORM, provider or fixtures."""
from copy import deepcopy
import json
import unittest

from management.services.ig_required_policy import (
    REQUIRED_KEY, SHOOTING_METADATA, RequiredPolicyError,
    admit_required_policy_modules, derive_required_scenarios,
    normalize_programme_metadata, required_for_scenarios, shooting_programme_metadata,
)


def module(identity, *, required=(), **values):
    result = {"id": identity, "body": identity + " guidance", "priority": 100,
        "active": True, "locale": "all", "trust_scope": "public_policy",
        "programme_metadata": {REQUIRED_KEY: list(required)} if required else {}}
    result.update(values)
    return result


def admit(rows, **overrides):
    values = {"applicable_scenarios": ("size",), "eligible_ids": [row["id"] for row in rows]}
    values.update(overrides)
    return admit_required_policy_modules(rows, **values)


class RequiredPolicyMetadataTests(unittest.TestCase):
    def test_empty_declaration_preserves_exact_legacy_serialization(self):
        for old in ({}, SHOOTING_METADATA):
            original = deepcopy(old)
            extended = {**old, REQUIRED_KEY: []}
            self.assertEqual(json.dumps(normalize_programme_metadata(extended), sort_keys=True),
                             json.dumps(old, sort_keys=True))
            self.assertEqual(old, original)

    def test_finite_declarations_canonicalize_order_without_mutating_input(self):
        raw = {REQUIRED_KEY: ["ugc", "size", "size", "objection"]}
        self.assertEqual(required_for_scenarios(raw), ("objection", "size", "ugc"))
        self.assertEqual(raw[REQUIRED_KEY], ["ugc", "size", "size", "objection"])
        normalized = normalize_programme_metadata(raw)
        normalized[REQUIRED_KEY].append("collaboration")
        self.assertNotIn("collaboration", raw[REQUIRED_KEY])

    def test_invalid_required_values_and_unknown_metadata_fail_with_finite_code(self):
        for raw in ({REQUIRED_KEY: "size"}, {REQUIRED_KEY: [True]}, {REQUIRED_KEY: ["size", "money"]},
                    {REQUIRED_KEY: ["size"] * 6}, {"arbitrary": "instruction"},
                    {"kind": "shooting_prize"}, {**SHOOTING_METADATA, "manager_required": False}):
            with self.subTest(raw=raw), self.assertRaises(RequiredPolicyError) as caught:
                normalize_programme_metadata(raw)
            self.assertIn(caught.exception.code, {"invalid_required_scenarios", "invalid_required_policy_metadata"})

    def test_shooting_adapter_preserves_exact_subset_with_new_declaration(self):
        raw = {**SHOOTING_METADATA, REQUIRED_KEY: ["ugc"]}
        self.assertEqual(shooting_programme_metadata(raw), SHOOTING_METADATA)
        self.assertEqual(required_for_scenarios(raw), ("ugc",))
        self.assertEqual(shooting_programme_metadata({REQUIRED_KEY: ["size"]}), {})


class RequiredScenarioApplicabilityTests(unittest.TestCase):
    def test_captured_current_scenarios_are_finite_and_creator_is_collaboration(self):
        self.assertEqual(derive_required_scenarios(captured_tags={"creator", "custom_print", "ugc", "objection_size_risk"}),
                         ("collaboration", "custom_print", "objection", "size", "ugc"))

    def test_existing_size_choice_and_arbitrary_image_do_not_create_new_requirement(self):
        self.assertEqual(derive_required_scenarios(captured_tags={"size", "fit", "product", "image"}), ())
        self.assertEqual(derive_required_scenarios(captured_tags={"objection_fake"}), ())

    def test_semantic_objection_triggers_do_not_promote_plain_price_question(self):
        self.assertEqual(derive_required_scenarios(semantic_triggers={"price_question"}), ())
        self.assertEqual(derive_required_scenarios(semantic_triggers={"price_objection", "hesitation", "size_question"}),
                         ("objection", "size"))

    def test_invalid_or_unbounded_capture_selector_inputs_fail(self):
        for raw in ("creator", ["creator"] * 65, [None]):
            with self.subTest(raw=raw), self.assertRaises(RequiredPolicyError):
                derive_required_scenarios(captured_tags=raw)


class RequiredPolicyAdmissionTests(unittest.TestCase):
    def test_legacy_head_is_compatible_but_does_not_claim_scenario_coverage(self):
        result = admit([module("legacy")])
        self.assertTrue(result.ready)
        self.assertEqual(result.selected_ids, ("legacy",))
        self.assertEqual(result.covered_scenarios, ())
        self.assertEqual(result.undeclared_scenarios, ("size",))
        self.assertEqual(result.coverage_status, "legacy_unconfigured")
        self.assertEqual(result.diagnostics, ("legacy_required_scenarios_undeclared",))

    def test_required_module_reserves_budget_before_higher_priority_optional(self):
        result = admit([module("optional", priority=1, body="o" * 8),
                        module("required", priority=999, body="r" * 5, required=("size",))], budget_chars=7)
        self.assertTrue(result.ready)
        self.assertEqual(result.selected_ids, ("required",))
        self.assertEqual(result.required_ids, ("required",))
        self.assertEqual(result.omitted, (("optional", "budget_exhausted"),))
        self.assertEqual(result.covered_scenarios, ("size",))

    def test_multiple_required_bodies_fit_with_exact_compiler_separator_cost(self):
        rows = [module("r1", body="x" * 3, required=("size",)), module("r2", body="y" * 4, required=("size",))]
        result = admit(rows, used_chars=10, budget_chars=21)
        self.assertTrue(result.ready)
        self.assertEqual(result.required_chars, 11)
        self.assertEqual(result.used_chars, 21)
        failed = admit(rows, used_chars=10, budget_chars=20)
        self.assertFalse(failed.ready)
        self.assertEqual(failed.reason, "required_policy_exceeds_budget")
        self.assertEqual(failed.selected_ids, ())

    def test_declared_inactive_empty_private_or_irrelevant_module_is_named_missing(self):
        for changes, eligible in (({"active": False}, ["r"]), ({"body": ""}, ["r"]),
                                  ({"trust_scope": "operator_only"}, ["r"]), ({}, [])):
            with self.subTest(changes=changes):
                result = admit([module("r", required=("size",), **changes)], eligible_ids=eligible)
                self.assertFalse(result.ready)
                self.assertEqual(result.reason, "required_scenario_module_missing")
                self.assertEqual(result.selected_ids, ())

    def test_wrong_locale_only_declaration_is_missing_without_foreign_body(self):
        result = admit([module("uk", required=("size",), locale="uk")], locale="en")
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "required_scenario_module_missing")
        self.assertIn(("uk", "locale_mismatch"), result.omitted)

    def test_translated_required_modules_choose_only_applicable_locale(self):
        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale):
                rows = [module(code, required=("size",), locale=code) for code in ("uk", "ru", "en")]
                result = admit(rows, locale=locale)
                self.assertTrue(result.ready)
                self.assertEqual(result.required_ids, (locale,))
                self.assertEqual(result.selected_ids, (locale,))

    def test_partial_contract_reports_undeclared_scenario_without_claiming_full_coverage(self):
        result = admit([module("r", required=("size",))], applicable_scenarios=("size", "ugc"))
        self.assertTrue(result.ready)
        self.assertEqual(result.coverage_status, "partial")
        self.assertEqual(result.covered_scenarios, ("size",))
        self.assertEqual(result.undeclared_scenarios, ("ugc",))

    def test_no_current_scenario_does_not_require_inactive_scenario_module(self):
        result = admit([module("r", required=("size",), active=False)], applicable_scenarios=())
        self.assertTrue(result.ready)
        self.assertEqual(result.required_ids, ())
        self.assertEqual(result.coverage_status, "not_applicable")

    def test_actual_48000_budget_reservation_uses_remaining_mandatory_space(self):
        result = admit([module("optional", body="o" * 4000, priority=1),
                        module("required", body="r" * 9000, required=("size",), priority=999)],
                       used_chars=38_000, budget_chars=48_000)
        self.assertTrue(result.ready)
        self.assertEqual(result.selected_ids, ("required",))
        self.assertEqual(result.used_chars, 47_002)
        self.assertIn(("optional", "budget_exhausted"), result.omitted)
        failed = admit([module("required", body="r" * 10_000, required=("size",))],
                       used_chars=38_000, budget_chars=48_000)
        self.assertFalse(failed.ready)
        self.assertEqual(failed.reason, "required_policy_exceeds_budget")

    def test_safe_metadata_has_no_bodies_and_is_defensively_copied(self):
        rows = [module("r", body="private identifying customer content", required=("size",))]
        before = deepcopy(rows)
        result = admit(rows)
        metadata = result.metadata()
        self.assertNotIn("private identifying", repr(metadata))
        metadata["selected_ids"].clear()
        self.assertEqual(result.selected_ids, ("r",))
        self.assertEqual(rows, before)

    def test_duplicate_ids_invalid_budget_and_unknown_eligibility_are_finite_errors(self):
        for rows, values in (([module("r"), module("r")], {}), ([module("r")], {"budget_chars": True}),
                             ([module("r")], {"eligible_ids": ["foreign"]})):
            with self.subTest(values=values), self.assertRaises(RequiredPolicyError):
                admit(rows, **values)


class RequiredFinalCompilerTests(unittest.TestCase):
    def compile(self, **overrides):
        from management.services.ig_policy_compiler import PolicyModule, compile_policy

        values = {"immutable_authority": [PolicyModule("authority", "A")],
            "published_core": [PolicyModule("core", "C")], "verified_dynamic_facts": [],
            "budget_chars": 48_000}
        values.update(overrides)
        return compile_policy(**values)

    def test_final_compiler_reserves_required_module_before_high_priority_optional(self):
        from management.services.ig_policy_compiler import PolicyModule

        result = self.compile(playbooks=[PolicyModule("optional", "o" * 8, priority=1),
            PolicyModule("required", "r" * 5, priority=999, required=True)], budget_chars=12)
        self.assertEqual(result.selected, ("authority", "core", "required"))
        self.assertEqual(result.required_ids, ("required",))
        self.assertEqual(result.mandatory_ids, ("authority", "core"))
        self.assertEqual(result.omitted[0].metadata(), {"id": "optional", "reason": "budget_exhausted"})

    def test_final_required_overflow_is_named_instead_of_optional_omission(self):
        from management.services.ig_policy_compiler import PolicyModule, PolicyReadinessError

        with self.assertRaises(PolicyReadinessError) as raised:
            self.compile(playbooks=[PolicyModule("required", "r" * 7, required=True)], budget_chars=12)
        self.assertEqual(raised.exception.code, "required_policy_exceeds_budget")
        self.assertNotIn("rrrrrrr", repr(raised.exception.details))

    def test_legacy_defaults_keep_compiler_hash_and_metadata_shape(self):
        from management.services.ig_policy_compiler import PolicyModule

        old = self.compile(playbooks=[PolicyModule("optional", "BODY")])
        explicit_empty = self.compile(playbooks=[{"id": "optional", "body": "BODY", "required": False}])
        self.assertEqual(old.content_hash, explicit_empty.content_hash)
        self.assertEqual(old.metadata(), explicit_empty.metadata())
        self.assertNotIn("required_ids", old.metadata())

    def test_required_flag_is_strict_and_invalid_or_duplicate_required_cannot_compile(self):
        from management.services.ig_policy_compiler import PolicyModule, PolicyReadinessError

        for modules in ([{"id": "r", "body": "R", "required": "false"}],
                        [PolicyModule("r", "", required=True)],
                        [PolicyModule("r", "R", active=False, required=True)],
                        [PolicyModule("r", "R", required=True), PolicyModule("r", "duplicate")]):
            with self.subTest(modules=modules), self.assertRaises(PolicyReadinessError):
                self.compile(playbooks=modules)

    def test_required_customer_body_stays_out_of_reusable_public_hash(self):
        from management.services.ig_policy_compiler import PolicyModule

        first = self.compile(customer_data=[PolicyModule("context:turn", "PRIVATE ONE", customer_bound=True, required=True)])
        second = self.compile(customer_data=[PolicyModule("context:turn", "PRIVATE TWO", customer_bound=True, required=True)])
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.context_hash, second.context_hash)
        self.assertNotIn("PRIVATE", repr(first.metadata()))

    def test_real_48000_budget_accounts_mandatory_blocks_before_required(self):
        from management.services.ig_policy_compiler import PolicyModule

        result = self.compile(published_core=[PolicyModule("core", "c" * 38_000)],
            playbooks=[PolicyModule("optional", "o" * 4_000, priority=1),
                       PolicyModule("required", "r" * 9_000, priority=999, required=True)])
        self.assertIn("required", result.selected)
        self.assertNotIn("optional", result.selected)
        self.assertLessEqual(len(result.text), 48_000)
