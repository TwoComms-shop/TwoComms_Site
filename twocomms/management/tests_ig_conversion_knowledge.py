"""Pure scoped owner facts and source-safe objection regressions; no network."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from management.services.approved_public_facts import (
    APPROVED_PUBLIC_FACTS_VERSION, approved_base_assertion, approved_assertion_valid,
    approved_public_facts, approved_public_fact_manifest, approved_base_context, approved_category_context,
)
from management.services.bot_knowledge import read_knowledge_manifest
from management.services.ig_product_inquiries import (
    capture_product_inquiries, inquiry_prompt, inquiry_reply, validate_inquiry_response,
    approved_product_topic_covered,
    ProductInquiryCapture, source_refs,
    apply_approved_category_context,
)
from management.services.ig_reply_truth import (
    ReplyTruthContext, approved_product_assertion_covered, validate_reply_truth,
)
from management.services.ig_response_control import ValidatedResponse, ResponseControl
from management.services.ig_policy_compiler import compile_policy, PolicyReadinessError


class ScopedOwnerKnowledgeTests(SimpleTestCase):
    def state(self, kind="tshirts", fit="regular", **product):
        return {"product": {"id": 12, "kind": kind, **product},
                "fit": {"selected": fit, "options": [{"code": fit}] if fit else []},
                "size": {"available": ["S", "M", "L"]}, "color": {"options": [{"name": "Black"}]}}

    def assertion(self, question="Який склад?", kind="tshirts", fit="regular", language="uk", **product):
        return approved_base_assertion(self.state(kind, fit, **product), question=question, language=language)

    def capture(self, text, state=None, language="uk"):
        return capture_product_inquiries([{"message_id": 10, "role": "user", "text": text, "source_digest": "a" * 64}],
            cart_capture={"active_line_id": "line", "lines": [{"line_id": "line", "product_id": 12}]},
            readiness_by_line={"line": state or self.state()}, profile_language=language)

    def test_reviewed_modules_replace_stale_hoodie_unknown_and_density_in_all_locales(self):
        for locale in ("uk", "ru", "en"):
            facts = {row.key: row for row in approved_public_facts(locale)}
            self.assertIn("90%", facts["current_hoodie_bases"].public_text)
            self.assertNotIn("320", facts["current_hoodie_bases"].public_text)
            self.assertTrue(facts["current_hoodie_bases"].review_status.startswith("reviewed"))
            self.assertIn("220", facts["current_tshirt_bases"].public_text)
            self.assertNotIn("210", facts["current_tshirt_bases"].public_text)
            self.assertIn("owner:2026-10-18", facts["current_care"].source_ref)
            self.assertIn("current_hoodie", facts["current_print_quality"].base_keys)
            self.assertEqual(len(facts), len(approved_public_facts(locale)))
            self.assertEqual(approved_public_fact_manifest(locale)["version"], APPROVED_PUBLIC_FACTS_VERSION)

    def test_known_composition_has_one_shared_assertion_without_manager(self):
        for locale, question in (("uk", "L. Який склад футболки?"), ("ru", "L. Какой состав футболки?"),
                                 ("en", "L. What is the T-shirt composition?")):
            with self.subTest(locale=locale):
                disposition = self.capture(question, language=locale)
                self.assertEqual(disposition["status"], "none")
                self.assertFalse(disposition["inquiries"])
                self.assertEqual(len(disposition["resolved_information"]), 1)
                assertion = disposition["resolved_information"][0]["assertion"]
                self.assertTrue(approved_assertion_valid(assertion))
                reply = inquiry_reply(disposition)
                self.assertIn("95%", reply)
                self.assertIn("5%", reply)
                self.assertEqual(validate_inquiry_response(disposition, ValidatedResponse(reply, ())), "")
                self.assertTrue(approved_product_topic_covered("info:material", reply, disposition))
                self.assertFalse(approved_product_topic_covered("info:material", "L", disposition))
                self.assertIn("APPROVED PRODUCT ANSWERS", inquiry_prompt(disposition))
                self.assertFalse(assertion["catalog_authority"])
                self.assertFalse(assertion["checkout_authority"])

    def test_hoodie_composition_density_material_care_and_quality_answer_directly(self):
        for question, number in (("Який склад худі?", "90%"), ("Яка щільність худі?", "залежить"),
                                 ("Яка тканина худі?", "пенье"), ("Як прати худі?", "30"),
                                 ("Яка якість худі?", "100")):
            with self.subTest(question=question):
                disposition = self.capture(question, self.state("hoodies", "oversize"))
                reply = inquiry_reply(disposition)
                self.assertEqual(disposition["status"], "none")
                self.assertIn(number, reply)
                self.assertTrue(approved_product_topic_covered("info:question", reply, disposition))
                self.assertEqual(validate_inquiry_response(disposition, ValidatedResponse(reply, ())), "")

    def test_unknown_hardware_foreign_future_and_unpublished_facts_keep_review(self):
        cases = (("Який склад лонгсліва?", self.state("longsleeves")),
                 ("Який склад худі?", self.state()),
                 ("Який склад наступної партії футболок?", self.state()),
                 ("Які люверси худі?", self.state("hoodies")),
                 ("Який склад футболки?", self.state(base_version="future-v2")),
                 ("Який склад футболки?", self.state(id=None)))
        for question, state in cases:
            with self.subTest(question=question, state=state):
                self.assertEqual(self.capture(question, state)["status"], "manager_required")
        with patch("management.services.approved_public_facts.approved_public_facts", return_value=()):
            self.assertEqual(self.capture("Який склад футболки?")["status"], "manager_required")

    def test_question_topic_without_current_base_does_not_grant_anything(self):
        self.assertIsNone(approved_base_assertion({}, question="Який склад футболки?"))
        self.assertIsNone(self.assertion("Яка щільність футболки?", fit=""))
        self.assertEqual(self.assertion(fit="")["gsm"], None)

    def test_corrected_source_in_bundle_keeps_each_inquiry_product_scope(self):
        sources = [{"message_id": 10, "role": "user", "text": "Який склад футболки?", "source_digest": "a" * 64},
                   {"message_id": 11, "role": "user", "text": "Який склад худі?", "source_digest": "b" * 64}]
        result = capture_product_inquiries(sources, cart_capture={"active_line_id": "line", "lines": [{"line_id": "line", "product_id": 12}]},
            readiness_by_line={"line": self.state()})
        self.assertEqual(result["status"], "manager_required")
        self.assertEqual([row["source_message_id"] for row in result["resolved_information"]], [10])
        self.assertEqual([row["source_message_id"] for row in result["inquiries"]], [11])

    def test_changed_assertion_and_version_fail_closed(self):
        assertion = self.assertion()
        self.assertTrue(approved_assertion_valid(assertion))
        for key, value in (("text", "100% cotton"), ("fact_version", "old"), ("product_id", 44)):
            changed = {**assertion, key: value}
            self.assertFalse(approved_assertion_valid(changed))
            self.assertFalse(approved_product_assertion_covered(assertion["text"], changed))

    def test_material_guard_uses_selected_base_not_any_public_fact(self):
        tee = self.assertion()
        hoodie = self.assertion("Який склад худі?", "hoodies")
        for assertion in (tee, hoodie):
            self.assertTrue(validate_reply_truth(assertion["text"], context=ReplyTruthContext(approved_product_assertions=(assertion,))).valid)
        for text in ("Склад: 100% бавовни.", "Худі: 95% бавовни / 5% еластану.", "Тринитка з флісом.",
                     "Футболка пошита в Туреччині.", "Наступна партія футболок: 95% бавовни / 5% еластану."):
            self.assertIn("unverified_product_material", validate_reply_truth(text,
                context=ReplyTruthContext(approved_product_assertions=(tee,))).reasons, text)
        self.assertFalse(validate_reply_truth(tee["text"], context=ReplyTruthContext()).valid)

    def test_unknown_claims_cannot_be_laundered_through_manager_control(self):
        disposition = self.capture("Які люверси худі?", self.state("hoodies"))
        for text in ("Худі з металевими люверсами.", "100% cotton. I will check with the manager."):
            self.assertEqual(validate_inquiry_response(disposition,
                ValidatedResponse(text, (ResponseControl("manager", True),))), "product_information_unverified")

    def test_quality_owner_attribution_and_care_are_required(self):
        assertion = self.assertion("Яка якість худі?", "hoodies")
        context = ReplyTruthContext(approved_product_assertions=(assertion,))
        self.assertTrue(validate_reply_truth(assertion["text"], context=context).valid)
        for text in ("Принт витримує понад 100 прань.", "За оцінкою власника, понад 100 прань.",
                     "За оцінкою власника, 30 прань при рекомендованому догляді.",
                     "Лабораторний тест підтвердив 100 прань.", "Найкращий в Україні.", "Вічний принт.",
                     "Принт ніколи не тріскається.", "Прати при 60°C."):
            self.assertIn("unverified_product_quality", validate_reply_truth(text, context=context).reasons, text)

    def test_spelled_temperatures_share_scoped_care_validation_and_coverage(self):
        assertion = self.assertion("Як прати цю футболку?")
        context = ReplyTruthContext(approved_product_assertions=(assertion,))
        for text in ("Радимо прати при 30 градусах за рекомендаціями догляду.",
                     "Рекомендуем стирать при 30 градусах, соблюдая рекомендации ухода.",
                     "Wash at 30 degrees Celsius following the care guidance.",
                     "Радимо прати при 30°C за рекомендаціями догляду."):
            with self.subTest(text=text):
                self.assertTrue(validate_reply_truth(text, context=context).valid)
                self.assertTrue(approved_product_assertion_covered(text, assertion))
                self.assertIn("unverified_product_quality", validate_reply_truth(text, context=ReplyTruthContext()).reasons)
                hot = text.replace("30", "90")
                self.assertIn("unverified_product_quality", validate_reply_truth(hot, context=context).reasons)
                self.assertFalse(approved_product_assertion_covered(hot, assertion))

    def test_missing_quality_publication_does_not_inherit_authority_from_composition(self):
        facts = tuple(row for row in approved_public_facts("uk") if row.key != "current_print_quality")
        with patch("management.services.approved_public_facts.approved_public_facts", return_value=facts):
            proof = self.assertion()
            self.assertTrue(approved_assertion_valid(proof))
            self.assertIn("unverified_product_quality", validate_reply_truth(
                "За оцінкою засновника, понад 100 прань при рекомендованому догляді.",
                context=ReplyTruthContext(approved_product_assertions=(proof,))).reasons)

    def test_questions_negations_and_source_matched_quotes_remain_nonclaims(self):
        for text in ("Це 100% бавовна?", "Не обіцяємо вічний принт.", "Уточню тканину у менеджера.",
                     "Це не 100% cotton.", "Це не wool.", "Футболка не пошита в Туреччині."):
            self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext()).valid, text)
        quoted = "100% cotton"
        self.assertTrue(validate_reply_truth('Ви написали "100% cotton".',
            context=ReplyTruthContext(quoted_data=(quoted,))).valid)
        self.assertFalse(validate_reply_truth('Ви написали "100% cotton". Це 100% cotton.',
            context=ReplyTruthContext(quoted_data=(quoted,))).valid)

    def test_prompt_relevance_and_required_unknown_boundary(self):
        empty = read_knowledge_manifest("uk", topics=(), base_keys=())
        self.assertEqual([row.id for row in empty.modules], ["approved_public:uk:unconfirmed_bases"])
        self.assertTrue(empty.modules[0].required)
        selected = read_knowledge_manifest("uk", topics=("composition",), base_keys=("current_hoodie",))
        ids = [row.id for row in selected.modules]
        self.assertIn("approved_public:uk:current_hoodie_bases", ids)
        self.assertNotIn("approved_public:uk:current_tshirt_bases", ids)
        self.assertNotIn("approved_public:uk:current_print_quality", ids)
        with self.assertRaises(PolicyReadinessError) as raised:
            compile_policy(immutable_authority=(), published_core=(), verified_dynamic_facts=(),
                           knowledge=empty.policy_inputs(), budget_chars=1)
        self.assertEqual(raised.exception.code, "required_policy_exceeds_budget")

    def test_story_leads_have_no_unbound_runtime_story(self):
        from management.knowledge.stories import STORY_LEADS
        self.assertTrue(all(row["design_id"] is None and row["artwork_version"] is None for row in STORY_LEADS))
        keys = {row.key for row in approved_public_facts("uk")}
        self.assertFalse(keys & {row["key"] for row in STORY_LEADS})

    def test_base_truth_context_is_topic_independent_but_rejects_foreign_subject(self):
        proof = approved_base_context(self.state(), sources=[{"text": "Яка посадка футболки?"}])
        self.assertTrue(proof)
        self.assertTrue(validate_reply_truth("95% бавовни / 5% еластану.",
            context=ReplyTruthContext(approved_product_assertions=proof)).valid)
        self.assertFalse(approved_base_context(self.state(), sources=[{"text": "Яка посадка худі?"}]))
        self.assertFalse(approved_base_context(self.state(), sources=[{"text": "Який склад майбутньої партії?"}]))

    def test_material_precision_and_other_fibres_require_their_own_proof(self):
        proof = (self.assertion(),)
        for text in ("100% wool.", "Матеріал: viscose.", "Щільність точно 190 gsm."):
            self.assertIn("unverified_product_material", validate_reply_truth(text,
                context=ReplyTruthContext(approved_product_assertions=proof)).reasons)

    def category_subject(self, garment="hoodie", category="hoodies"):
        return {"schema": "information-subject.v1", "status": "identified", "garment_type": garment,
            "category_slug": category, "source_message_ids": [10], "future_base_requested": False,
            "unsupported_detail_requested": False, "specific_product_requested": False,
            "selection_authority": False, "stock_authority": False, "checkout_authority": False}

    def test_current_category_capability_answers_generic_hoodie_without_promoting_old_tee(self):
        for locale in ("uk", "ru", "en"):
            proof = approved_category_context(self.category_subject(), current_catalogue_categories=["hoodies", "tshirts"], language=locale)
            self.assertTrue(proof)
            self.assertTrue(approved_assertion_valid(proof[0]))
            self.assertIsNone(proof[0]["product_id"])
            self.assertFalse(proof[0]["stock_authority"])
            self.assertFalse(proof[0]["selection_authority"])
            context = ReplyTruthContext(approved_product_assertions=proof)
            self.assertTrue(validate_reply_truth(proof[0]["text"], context=context).valid, proof[0]["text"])
            for text in ("Це худі: 90% бавовни / 10% поліестеру.", "Худі: 90% бавовни / 10% поліестеру.",
                         "Ця футболка: 95% бавовни / 5% еластану.", "This hoodie is 90% cotton / 10% polyester."):
                self.assertIn("unverified_product_material", validate_reply_truth(text, context=context).reasons)

    def test_current_category_missing_proof_and_unknown_scope_fail_closed(self):
        self.assertFalse(approved_category_context(self.category_subject()))
        self.assertFalse(approved_category_context(self.category_subject(), current_catalogue_categories=["tshirts"]))
        for key, value in (("status", "ambiguous"), ("future_base_requested", True),
                           ("unsupported_detail_requested", True), ("specific_product_requested", True),
                           ("stock_authority", True)):
            self.assertFalse(approved_category_context({**self.category_subject(), key: value},
                current_catalogue_categories=["hoodies"]))
        self.assertFalse(approved_category_context(self.category_subject("longsleeve", "longsleeves"),
            current_catalogue_categories=["longsleeves"]))

    def test_current_category_tee_density_needs_matching_named_fit(self):
        proof = approved_category_context(self.category_subject("tshirt", "tshirts"), current_catalogue_categories=["tshirts"])
        context = ReplyTruthContext(approved_product_assertions=proof)
        self.assertTrue(validate_reply_truth(proof[0]["text"], context=context).valid)
        for text in ("Наші поточні футболки — 220 gsm.", "Наші поточні футболки regular — 220 gsm."):
            self.assertIn("unverified_product_material", validate_reply_truth(text, context=context).reasons)

    def test_backend_capture_recomputes_category_answer_from_current_subject_and_catalogue_proof(self):
        from management.services.ig_product_focus import information_subject

        for question in ("А жіночі худі у вас є?", "Який склад худі?", "Яка тканина худі?", "Як прати худі?", "Яка якість худі?"):
            sources = [{"message_id": 10, "role": "user", "text": question, "source_digest": "a" * 64}]
            subject = information_subject(sources)
            self.assertTrue(approved_category_context(subject, current_catalogue_categories=["hoodies"]))
            revision = SimpleNamespace(bundle_snapshot={"sources": sources}, snapshot_digest="b" * 64, client_id=5)
            cart = {"scope": {"client_id": 5}, "active_line_id": "line", "lines": [{"line_id": "line", "product_id": 12}]}
            category = {"schema": "current-category-catalogue.v1", "categories": ["hoodies"],
                        "catalogue_digest": "c" * 64, "snapshot_digest": revision.snapshot_digest}
            capability = ProductInquiryCapture(revision.snapshot_digest, json.dumps(source_refs(sources)), json.dumps(cart),
                json.dumps({"line": self.state()}), "uk", json.dumps(category))
            disposition = capability.recompute(revision)
            self.assertIsNotNone(disposition)
            self.assertEqual(disposition["status"], "none")
            self.assertTrue(disposition["base_context_assertions"])
            if disposition["resolved_information"]:
                reply = inquiry_reply(disposition)
                self.assertTrue(approved_product_topic_covered("info:question", reply, disposition), reply)
                self.assertEqual(validate_inquiry_response(disposition, ValidatedResponse(reply, ())), "")
            changed = {**category, "snapshot_digest": "d" * 64}
            self.assertIsNone(replace(capability, category_json=json.dumps(changed)).recompute(revision))

    def test_category_application_rebinds_an_already_resolved_wrong_native_answer(self):
        from management.services.ig_product_focus import information_subject

        sources = [{"message_id": 10, "role": "user", "text": "А худі?", "source_digest": "a" * 64},
                   {"message_id": 11, "role": "user", "text": "Який склад?", "source_digest": "b" * 64}]
        original = capture_product_inquiries(sources,
            cart_capture={"active_line_id": "line", "lines": [{"line_id": "line", "product_id": 12}]},
            readiness_by_line={"line": self.state()})
        self.assertEqual(original["status"], "manager_required")
        self.assertFalse(original["resolved_information"])
        old_wrong = {**original, "inquiries": [], "resolved_information": [{**original["inquiries"][0],
            "reason": "approved_product_information", "assertion": self.assertion()}]}
        proof = approved_category_context(information_subject(sources), current_catalogue_categories=["hoodies"])
        fixed = apply_approved_category_context(old_wrong, proof)
        self.assertEqual(fixed["status"], "none")
        self.assertEqual({row["assertion"]["garment"] for row in fixed["resolved_information"]}, {"hoodie"})
        self.assertIn("90%", inquiry_reply(fixed))
        self.assertNotIn("95%", inquiry_reply(fixed))


class SourceSafeObjectionTests(SimpleTestCase):
    def test_custom_and_collaboration_require_own_current_source(self):
        from management.services.bot_instruction_routing import turn_triggers

        for text in ("Дякую", "Який склад?", "Ціна?", 'Друг каже «кастомний принт»',
                     "Раніше хотів кастомний принт", "Не хочу кастомний принт", "Не хочу співпрацю"):
            self.assertFalse(turn_triggers(text) & {"custom_print", "collaboration"}, text)
        self.assertIn("custom_print", turn_triggers("Хочу свій принт"))
        self.assertIn("custom_print", turn_triggers("I want my own design"))
        self.assertIn("collaboration", turn_triggers("Пропоную співпрацю, ось портфоліо"))

    def test_routing_and_persistence_share_own_current_predicate(self):
        from management.services.ig_objections import detect_objection_types
        from management.services.bot_instruction_routing import turn_triggers
        from management.services.ig_turn_intelligence import current_objections

        for text, expected in (("Дорого", True), ("Не дорого, хочу інший принт", False),
                               ('Друг каже «дорого», але мені ціна підходить', False),
                               ("Раніше було дорого, зараз беру L", False),
                               ('Він сказав: "дорого"', False), ("Ціна?", False), ("Дякую", False),
                               ("Мені дорого. Це не дорого.", False)):
            with self.subTest(text=text):
                kinds = {row["kind"] for row in current_objections([{"message_id": 1, "text": text}])}
                self.assertEqual("price" in kinds, expected)
                self.assertEqual("price" in detect_objection_types(text), expected)
                self.assertEqual("price_objection" in turn_triggers(text), expected)

    def test_scenario_proposals_use_existing_metadata_and_relevance(self):
        from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, reviewed_scenario_values
        from management.services.bot_instruction_routing import validate_instruction_tags
        from management.services.ig_required_policy import normalize_programme_metadata

        for row in SCENARIO_INSTRUCTIONS:
            values = reviewed_scenario_values(row["key"])
            self.assertEqual(normalize_programme_metadata(values["programme_metadata"]), values["programme_metadata"])
            self.assertFalse(validate_instruction_tags(",".join(values["tags"]))["unknown_tags"])
            self.assertNotIn("global", values["tags"])


class ReviewedScenarioPublicationTests(TestCase):
    def test_proposals_are_inert_until_existing_reviewed_publisher_and_then_required(self):
        from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, reviewed_scenario_values
        from management.services.ig_policy_publication import (
            draft_state, load_active_policy_snapshot, save_instruction_draft,
            publish_instruction_policy, select_policy_snapshot, PolicyPublicationError,
        )
        state = draft_state()
        for row in SCENARIO_INSTRUCTIONS:
            _instruction, state = save_instruction_draft(expected_revision=state.revision,
                expected_snapshot_hash=state.snapshot_hash, values=reviewed_scenario_values(row["key"]),
                note="reviewed owner scenarios; disposable test")
        with self.assertRaises(PolicyPublicationError) as absent:
            load_active_policy_snapshot()
        self.assertEqual(absent.exception.code, "active_publication_missing")
        result = publish_instruction_policy(expected_draft_revision=state.revision,
            expected_draft_hash=state.snapshot_hash, expected_head_id=None, expected_head_hash="",
            note="reviewed owner scenarios; disposable test")
        active = load_active_policy_snapshot()
        self.assertEqual(active.publication_id, result.publication.pk)
        for scenario, tags, triggers in (("custom_print", {"custom_print"}, {"custom_print"}),
                                         ("collaboration", {"collaboration"}, {"collaboration"}),
                                         ("ugc", {"ugc"}, {"ugc"}),
                                         ("size", {"size"}, {"size_question"}),
                                         ("objection", {"objection_price"}, {"price_objection"})):
            with self.subTest(scenario=scenario):
                selected = select_policy_snapshot(active.snapshot, client_tags=tags, active_triggers=triggers,
                    required_scenarios=(scenario,))
                self.assertEqual(selected["required_policy"]["coverage_status"], "declared_covered")
                self.assertEqual(selected["required_policy"]["covered_scenarios"], [scenario])
                self.assertTrue(selected["required_ids"])
                with self.assertRaises(PolicyPublicationError) as overflow:
                    select_policy_snapshot(active.snapshot, client_tags=tags, active_triggers=triggers,
                        required_scenarios=(scenario,), budget_chars=1)
                self.assertEqual(overflow.exception.code, "required_policy_exceeds_budget")
        social = select_policy_snapshot(active.snapshot, client_tags={"global", "core"}, active_triggers=())
        self.assertEqual(social["selected_ids"], [])


class KnowledgeLineageAccountingTests(TestCase):
    def manifest(self, selectors=None):
        from management.models import InstagramBotSettings
        from management.services import instagram_bot
        from management.tests_ig_policy_helpers import publish_current_instructions

        publish_current_instructions()
        metadata = {}
        instagram_bot.assemble_system_instruction(
            InstagramBotSettings(system_prompt="MANDATORY-CORE", knowledge_base="CURRENT-DIRECTIVE"),
            compiled_metadata=metadata, captured_knowledge_selectors=selectors)
        return metadata

    def test_actual_compiler_lineage_roundtrip_and_legacy_absence_remain_readable(self):
        from management.services.gemini_accounting_contract import sanitize_request_policy_manifest

        metadata = self.manifest()
        self.assertIn("knowledge_lineage", metadata)
        lineage = metadata["knowledge_lineage"]
        self.assertEqual(lineage["content_hash"], metadata["knowledge_hash"])
        self.assertTrue(lineage["modules"])
        for row in lineage["modules"]:
            self.assertEqual(row["consumed"], row["id"] in metadata["selected_ids"])
            self.assertNotIn("body", row)
        self.assertEqual(sanitize_request_policy_manifest(metadata), metadata)
        legacy = deepcopy(metadata)
        del legacy["knowledge_lineage"]
        self.assertEqual(sanitize_request_policy_manifest(legacy), legacy)
        self.assertNotIn("knowledge_lineage", sanitize_request_policy_manifest(legacy))

    def test_actual_relevance_omissions_roundtrip_with_selected_consumption(self):
        from management.services.gemini_accounting_contract import sanitize_request_policy_manifest

        metadata = self.manifest({"topics": [], "base_keys": []})
        lineage = metadata["knowledge_lineage"]
        self.assertTrue(lineage["omissions"])
        self.assertEqual(len(lineage["modules"]), 1)
        self.assertTrue(lineage["modules"][0]["required"])
        self.assertTrue(lineage["modules"][0]["consumed"])
        self.assertFalse(set(row["id"] for row in lineage["omissions"]) & set(metadata["selected_ids"]))
        self.assertEqual(sanitize_request_policy_manifest(metadata), metadata)

    def test_lineage_rejects_bodies_inconsistent_consumption_and_hash_or_source_injection(self):
        from management.services.gemini_accounting_contract import sanitize_request_policy_manifest, RequestPolicyManifestError

        metadata = self.manifest()
        mutations = []
        for key, value in (("body", "private customer text"), ("source_ref", "https://private.example/customer"),
                           ("version", "unbounded prose private data"), ("consumed", False), ("required", "true")):
            changed = deepcopy(metadata)
            changed["knowledge_lineage"]["modules"][0][key] = value
            mutations.append(changed)
        changed = deepcopy(metadata)
        changed["knowledge_lineage"]["content_hash"] = "f" * 64
        mutations.append(changed)
        changed = deepcopy(metadata)
        changed["knowledge_lineage"]["modules"] *= 101
        mutations.append(changed)
        changed = deepcopy(metadata)
        changed["knowledge_lineage"]["modules"] = []
        mutations.append(changed)
        for changed in mutations:
            with self.subTest(changed=changed["knowledge_lineage"]), self.assertRaises(RequestPolicyManifestError):
                sanitize_request_policy_manifest(changed)


from management.tests_ig_restock_subscriptions import _RestockFixture


class NativeCategoryInformationCoverageTests(_RestockFixture, TestCase):
    def current_categories(self):
        from storefront.models import Category, Product
        self.category.slug, self.category.name = "tshirts", "Футболки"
        self.category.save(update_fields=["slug", "name"])
        # Establish an exact native product choice; the fixture's initial
        # product-name wish alone intentionally has no selected-SKU authority.
        self.canonical(f"Хочу https://twocomms.shop/product/{self.product.slug}/, розмір M, колір black, крій classic")
        hoodies = Category.objects.create(name="Худі", slug="hoodies")
        Product.objects.create(title="Published current hoodie", slug="published-current-hoodie-knowledge",
            category=hoodies, price=1200, status="published")
        from management.models import IgCommerceSelectionSession
        retained_session = IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1)
        before_lines = deepcopy(retained_session.lines)
        self.assertTrue(any(row.get("product_id") == self.product.pk for row in before_lines), before_lines)
        return retained_session, before_lines

    def test_retained_tee_generic_hoodie_audience_is_answered_without_manager_or_selection(self):
        from management.services.ig_response_plan import capture_response_plan

        retained_session, before_lines = self.current_categories()
        source = self.source("А жіночі худі у вас є?")
        revision, _token = self.revision(source)
        revision.client = self.client_row
        plan = capture_response_plan(self.client_row, revision=revision)
        proofs = plan.product_information.get("base_context_assertions") or []
        proof = next(row for row in proofs if row.get("scope_kind") == "current_category")
        self.assertEqual(proof["garment"], "hoodie")
        retained_session.refresh_from_db()
        self.assertEqual(retained_session.lines, before_lines)
        response = ValidatedResponse("Наші поточні худі — унісекс, одна злегка oversize основа.", ())
        truth = plan.truth_context(ReplyTruthContext())
        self.assertTrue(validate_reply_truth(response.reply_text, context=truth).valid)
        self.assertEqual(plan.validate_multiline_claims(response, truth), "")
        coverage = plan.coverage(response)
        audience = [row["id"] for row in plan.obligations if row["kind"] == "info:audience"]
        self.assertTrue(audience, plan.obligations)
        self.assertTrue(set(audience) <= set(coverage["covered"]), coverage)
        self.assertEqual(validate_inquiry_response(plan.product_information, response), "")
        for bad in ("Ця футболка — унісекс.", "Худі — унісекс.", "Наші поточні худі regular — унісекс."):
            self.assertFalse(approved_product_topic_covered("info:audience", bad, plan.product_information))
        self.assertFalse(approved_product_topic_covered("info:fit", "Наші поточні худі regular — унісекс.", plan.product_information))

    def burst_revision(self, sources):
        from management.models import IgCustomerTurn, IgTurnMessage
        from management.services.ig_turn_revisions import (
            create_collecting_revision, claim_revision_preparation, seal_revision, claim_sealed_revision,
        )

        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=sources[0],
            window_started_at=self.now, window_deadline=self.now)
        for index, source in enumerate(sources):
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=index + 1, role="user")
        created = create_collecting_revision(turn, sources, now=self.now, bypass_quiet=True)
        prepared = claim_revision_preparation(created.revision.pk, now=self.now)
        sealed = seal_revision(created.revision.pk, prepared.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(sealed.revision.pk, now=self.now)
        claimed.revision.client = self.client_row
        return claimed.revision

    def test_native_nounless_composition_inherits_current_hoodie_subject_not_retained_tee(self):
        from management.services.ig_response_plan import capture_response_plan

        retained_session, before_lines = self.current_categories()
        sources = [self.source("А худі?"), self.source("Який склад?")]
        revision = self.burst_revision(sources)
        plan = capture_response_plan(self.client_row, revision=revision)
        information = plan.product_information
        known = [row for row in information["resolved_information"] if row["source_message_id"] == sources[1].pk]
        self.assertTrue(known, information)
        self.assertTrue(all(row["assertion"]["garment"] == "hoodie" for row in known))
        self.assertEqual(information["status"], "none")
        reply = inquiry_reply(information)
        self.assertIn("90%", reply)
        self.assertNotIn("95%", reply)
        self.assertTrue(approved_product_topic_covered("info:material", reply, information))
        self.assertFalse(approved_product_topic_covered("info:material", "Склад: 95% бавовни / 5% еластану.", information))
        self.assertEqual(validate_inquiry_response(information, ValidatedResponse(reply, ())), "")
        retained_session.refresh_from_db()
        self.assertEqual(retained_session.lines, before_lines)

    def test_native_combined_composition_and_density_require_both_independent_answers(self):
        from management.services.ig_response_plan import capture_response_plan

        self.current_categories()
        source = self.source("Який склад і щільність цієї футболки?")
        revision, _token = self.revision(source)
        revision.client = self.client_row
        plan = capture_response_plan(self.client_row, revision=revision)
        information = plan.product_information
        self.assertEqual({row["assertion"]["topic"] for row in information["resolved_information"]}, {"composition", "density"})
        density = "Щільність цієї основи — близько 190 г/м²."
        composition = "Склад цієї основи: 95% бавовни / 5% еластану."
        self.assertTrue(approved_product_topic_covered("info:density", density, information))
        self.assertFalse(approved_product_topic_covered("info:material", density, information))
        self.assertTrue(approved_product_topic_covered("info:material", composition, information))
        self.assertFalse(approved_product_topic_covered("info:density", composition, information))
        partial = plan.coverage(ValidatedResponse(density, ()))
        material_ids = [row["id"] for row in plan.obligations if row["kind"] == "info:material"]
        self.assertTrue(material_ids, plan.obligations)
        self.assertTrue(set(material_ids) <= set(partial["remaining"]), partial)
        complete = plan.coverage(ValidatedResponse(composition + " " + density, ()))
        relevant = [row["id"] for row in plan.obligations if row["kind"] in {"info:material", "info:density"}]
        self.assertTrue(set(relevant) <= set(complete["covered"]), complete)


class CapturedServicePolicyReadTests(TestCase):
    def setUp(self):
        from management.tests_ig_captured_policy_inputs import CapturedPolicyInputsTests
        CapturedPolicyInputsTests.setUp(self)

    def capture(self):
        from management.tests_ig_captured_policy_inputs import CapturedPolicyInputsTests
        return CapturedPolicyInputsTests.capture(self)

    def test_captured_real_service_case_needs_no_complaint_reread_or_sales_guard(self):
        from management.models import IgPostSaleCase
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from management.services.instagram_bot import POST_SALE_SERVICE_GUARDRAILS_TEMPLATE

        IgPostSaleCase.objects.create(client=self.client_row, source_message=self.source,
            case_type="exchange", status="open")
        with patch("management.services.ig_service_complaints.promotion_service_hold_reason",
                   side_effect=AssertionError("redundant complaint read")), CaptureQueriesContext(connection) as queries:
            captured = self.capture()
        self.assertLessEqual(len(queries), 4)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertTrue({"post_sale", "service", "exchange"} <= captured.tags)
        self.assertNotIn("sales", captured.tags)
        self.assertIn(POST_SALE_SERVICE_GUARDRAILS_TEMPLATE.split("{case}")[0], captured.automation_note)

    def test_no_case_retains_complaint_priority_before_sales_guidance(self):
        from management.services.instagram_bot import POST_SALE_SERVICE_GUARDRAILS_TEMPLATE

        with patch("management.services.ig_service_complaints.promotion_service_hold_reason", return_value="service_hold") as hold:
            captured = self.capture()
        hold.assert_called_once()
        self.assertIn(POST_SALE_SERVICE_GUARDRAILS_TEMPLATE.split("{case}")[0], captured.automation_note)
