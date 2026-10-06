"""Pure captured-turn regressions; no database or provider fixtures required."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

from management.services.gemini_routing import TaskClass, classify_live_turn
from management.services.ig_response_plan import build_response_plan
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_turn_intelligence import (
    SCOPE_KEYS, TurnContextError, build_routing_facts, build_turn_context, capture_digest,
)


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def capture(*texts):
    sources = [{"message_id": 100 + index, "role": "user", "text": text,
        "source_namespace": "instagram:fixture", "source_digest": str(index + 1) * 64,
        "provider_created_at": (NOW + timedelta(seconds=index)).isoformat()}
        for index, text in enumerate(texts or ("Дякую!",))]
    boundary = dict(client_id=7, revision_id=9, source_namespace="instagram:fixture",
        source_ids=[row["message_id"] for row in sources],
        source_digests={str(row["message_id"]): row["source_digest"] for row in sources},
        sealed_sources_digest=capture_digest(sources), watermark=dict(event_at=sources[-1]["provider_created_at"],
            message_id=sources[-1]["message_id"]), reset_id=2, reset_floor=40, erasure_epoch="",
        permission_epoch=3, publication=dict(id=4, version=5, hash="a" * 64),
        episode_id=6, order_id=None, line_id="line-a", recipient_id="self")
    return dict(boundary=boundary, sources=sources, captured_at=NOW + timedelta(seconds=20),
        routing_policy={"mode": "unified"})


def scoped(args, **values):
    return dict(scope={key: deepcopy(args["boundary"][key]) for key in SCOPE_KEYS}, **values)


def narrative(args, text="Раніше клієнт обирав чорний принт."):
    scope = {key: args["boundary"][key] for key in SCOPE_KEYS}
    scope["namespace"] = scope.pop("source_namespace")
    scope["erasure_at"] = scope.pop("erasure_epoch")
    source = dict(message_id=95, role="user", event_at=(NOW-timedelta(minutes=2)).isoformat(),
                  source_digest="c" * 64)
    captured = dict(scope=scope, target={key: source[key] for key in ("message_id", "event_at")},
                    sources=[source], previous_head_version=0)
    # publish_memory_result removes generation-only fields and the capture
    # digest, retaining that original commitment under this published key.
    captured["generation_input_digest"] = capture_digest(captured)
    import hashlib
    proof = dict(version="captured-memory.v1", head_version=1, capture=captured,
                 summary_digest=hashlib.sha256(text.encode()).hexdigest(), generated_at=NOW.isoformat())
    proof["digest"] = capture_digest(proof)
    return scoped(args, text=text, reason="current", provenance=proof)


def resign_memory(memory):
    proof = memory["provenance"]
    captured = proof["capture"]
    captured["generation_input_digest"] = capture_digest({key: value for key, value in captured.items()
        if key != "generation_input_digest"})
    proof["digest"] = capture_digest({key: value for key, value in proof.items() if key != "digest"})


class TurnIntelligenceTests(TestCase):
    def assertReason(self, reason, args):
        with self.assertRaises(TurnContextError) as caught:
            build_turn_context(**args)
        self.assertEqual(caught.exception.reason, reason)

    def test_bundle_question_and_reaction_keeps_question_semantics(self):
        args = capture("Мені дорого, є інший варіант?", "❤️")
        result = build_turn_context(**args)
        self.assertEqual(result.decision.task_class, TaskClass.COMPLEX_LIVE)
        self.assertTrue(result.facts.objection_present)
        self.assertEqual(result.metadata["captured_source_ids"], (100, 101))

    def test_plain_price_question_is_ordinary(self):
        result = build_turn_context(**capture("Яка ціна футболки?"))
        self.assertFalse(result.facts.objection_present)
        self.assertEqual(result.decision.task_class, TaskClass.ORDINARY_LIVE)

    def test_quoted_negated_and_historical_objections_are_ordinary(self):
        for text in ('Друг сказав, що дорого.', '«Дорого» — це цитата.',
                     'Це не дорого.', 'Раніше було дорого.', 'Я не боюсь передоплати.'):
            with self.subTest(text=text):
                result = build_turn_context(**capture(text))
                self.assertFalse(result.facts.objection_present)
                self.assertEqual(result.decision.task_class, TaskClass.ORDINARY_LIVE)

    def test_own_contrast_after_quote_is_active_and_negative_trust_is_active(self):
        for text in ('Друг сказав дорого, але мені дорого теж.', 'Не довіряю передоплаті.'):
            with self.subTest(text=text):
                self.assertTrue(build_turn_context(**capture(text)).facts.objection_present)

    def test_later_own_negation_withdraws_previous_same_bundle_objection(self):
        result = build_turn_context(**capture("Дорого.", "Ні, це не дорого."))
        self.assertFalse(result.facts.objection_present)

    def test_third_purchase_thanks_does_not_inherit_historical_primary_objection(self):
        args = capture("Дякую!")
        args["commerce"] = scoped(args, purchases_count=3, primary_objection="price")
        result = build_turn_context(**args)
        self.assertEqual(result.decision.task_class, TaskClass.ORDINARY_LIVE)
        self.assertIn("Warm continuity", result.context_note)
        self.assertFalse(result.facts.product_or_recipient_switch)

    def test_custom_fit_and_product_conflict_reuse_existing_classifier(self):
        for fact, reason in ((dict(custom_print_requested=True), "custom_print"),
                             (dict(personalized_fit_requested=True), "personalized_fit"),
                             (dict(pending_clarification="multiple_product_links"), "ambiguous_catalog"),
                             (dict(recipient_switch=True), "branch_switch")):
            with self.subTest(reason=reason):
                args = capture("Підкажіть варіант.")
                args["commerce"] = scoped(args, **fact)
                result = build_turn_context(**args)
                self.assertIn(reason, result.decision.reason_codes)
                self.assertEqual(result.decision.task_class, TaskClass.COMPLEX_LIVE)

    def test_image_audio_and_video_alone_preserve_media_reasoning(self):
        for mime, reason in (("image/jpeg", "image_reasoning"), ("audio/ogg", "audio_reasoning"),
                             ("video/webm", "video_reasoning")):
            with self.subTest(mime=mime):
                args = capture("")
                args["media"] = {"parts": [dict(source_message_id=100, mime=mime, sha256="b"*64, validated=True)]}
                result = build_turn_context(**args)
                self.assertTrue(result.decision.requires_media_reasoning)
                self.assertEqual(result.decision.reasoning_task, "media_analysis")
                self.assertIn(reason, result.decision.reason_codes)

    def test_media_binding_or_validation_failure_is_finite(self):
        for change in (dict(source_message_id=999), dict(validated=False), dict(sha256="bad")):
            args = capture("")
            args["media"] = {"parts": [{**dict(source_message_id=100, mime="image/jpeg", sha256="b"*64, validated=True), **change}]}
            self.assertReason("media_binding_invalid", args)

    def test_source_order_and_exact_digest_are_required(self):
        args = capture("Мені L", "Але чорну")
        args["sources"].reverse()
        self.assertReason("sealed_source_window_changed", args)
        args = capture("Мені L")
        args["sources"][0]["text"] = "Мені M"
        self.assertReason("sealed_source_digest_changed", args)

    def test_original_source_digest_is_preserved_not_recomputed_from_seal(self):
        args = capture("L")
        self.assertNotEqual(args["sources"][0]["source_digest"], capture_digest(args["sources"][0]))
        result = build_turn_context(**args)
        self.assertEqual(result.source_bindings[0]["source_digest"], "1" * 64)

    def test_reset_namespace_and_sealed_watermark_fail_closed(self):
        args = capture("L")
        args["boundary"]["reset_floor"] = 101
        self.assertReason("sealed_source_before_reset", args)
        args = capture("L")
        args["sources"][0]["source_namespace"] = "other"
        args["boundary"]["sealed_sources_digest"] = capture_digest(args["sources"])
        self.assertReason("sealed_source_scope_changed", args)
        args = capture("L")
        args["boundary"]["watermark"]["event_at"] = (NOW-timedelta(seconds=1)).isoformat()
        self.assertReason("sealed_source_after_watermark", args)

    def test_erasure_and_explicit_source_episode_mismatch_are_rejected(self):
        args = capture("L")
        args["boundary"]["erasure_epoch"] = NOW.isoformat()
        self.assertReason("client_erasing", args)
        args = capture("L")
        args["sources"][0]["scope"] = scoped(args)["scope"]
        args["sources"][0]["scope"]["episode_id"] = 999
        args["boundary"]["sealed_sources_digest"] = capture_digest(args["sources"])
        self.assertReason("sealed_source_scope_changed", args)

    def test_ambiguous_referral_is_source_bound_and_does_not_grant_ad_product(self):
        args = capture("Що це за модель?")
        args["referral"] = scoped(args, present=True, source_message_id=100, status="ambiguous", product_id=999)
        result = build_turn_context(**args)
        self.assertTrue(result.facts.ambiguous_ad_referral)
        self.assertIsNone(result.context_note)
        self.assertIn("ambiguous_referral", result.metadata["readiness_codes"])

    def test_absent_referral_is_unknown_and_wrong_episode_mapping_is_omitted(self):
        args = capture("Дякую!")
        args["referral"] = scoped(args, present=True, source_message_id=100, status="resolved",
                                   mapping_active=True, mapping_unique=True, product_id=8)
        args["referral"]["scope"]["episode_id"] = 999
        result = build_turn_context(**args)
        self.assertFalse(result.facts.ambiguous_ad_referral)
        self.assertIsNone(result.context_note)
        self.assertIn("referral_not_source_bound", result.metadata["readiness_codes"])

    def test_valid_exact_referral_context_contains_no_ad_price_authority(self):
        args = capture("Ця футболка")
        args["referral"] = scoped(args, present=True, source_message_id=100, status="resolved",
            mapping_active=True, mapping_unique=True, product_id=8, price="1 грн", mapping_id=4)
        result = build_turn_context(**args)
        self.assertIn('"product_id":8', result.context_note)
        self.assertNotIn("грн", result.context_note)

    def test_valid_current_narrative_is_untrusted_and_newer_memory_is_omitted(self):
        args = capture("Дякую")
        args["memory"] = narrative(args)
        result = build_turn_context(**args)
        self.assertIn("UNTRUSTED CAPTURED NARRATIVE", result.memory_note)
        args["memory"]["provenance"]["capture"]["target"] = dict(event_at=(NOW+timedelta(hours=1)).isoformat(), message_id=101)
        resign_memory(args["memory"])
        result = build_turn_context(**args)
        self.assertIsNone(result.memory_note)
        self.assertIn("narrative_after_sealed_watermark", result.metadata["readiness_codes"])

    def test_late_import_memory_source_cannot_cross_sealed_id_boundary(self):
        args = capture("Дякую")
        args["memory"] = narrative(args)
        args["memory"]["provenance"]["capture"]["sources"][0]["message_id"] = 101
        resign_memory(args["memory"])
        self.assertIsNone(build_turn_context(**args).memory_note)

    def test_unproven_stale_or_reset_narrative_does_not_fallback(self):
        for reason in ("narrative_provenance_missing", "narrative_source_stale"):
            args = capture("Дякую")
            args["memory"] = narrative(args)
            args["memory"]["reason"] = reason
            result = build_turn_context(**args)
            self.assertIsNone(result.memory_note)
            self.assertIn(reason, result.metadata["readiness_codes"])
        args = capture("Дякую")
        args["memory"] = narrative(args)
        args["memory"]["provenance"]["capture"]["scope"]["reset_id"] = 1
        resign_memory(args["memory"])
        result = build_turn_context(**args)
        self.assertIsNone(result.memory_note)
        self.assertIn("narrative_scope_changed", result.metadata["readiness_codes"])

    def test_history_is_captured_and_manager_needs_delivered_receipt(self):
        args = capture("Дякую")
        row = scoped(args, message_id=99, event_at=(NOW-timedelta(minutes=1)).isoformat(),
                     role="manager", text="Підтвердили", receipt_confirmed=True)
        args["history"] = [row]
        args["boundary"]["history_digests"] = {"99": capture_digest(row)}
        self.assertEqual(build_turn_context(**args).metadata["captured_history_ids"], (99,))
        row["receipt_confirmed"] = False
        self.assertReason("history_manager_unconfirmed", args)

    def test_legacy_shadow_and_unified_same_capture_have_same_proposed_context(self):
        args = capture("Мені дорого")
        outputs = []
        for mode in ("legacy", "shadow", "unified"):
            args["routing_policy"]["mode"] = mode
            outputs.append(build_turn_context(**args))
        self.assertTrue(all(item.facts == outputs[0].facts and item.decision == outputs[0].decision
                            and item.context_blocks == outputs[0].context_blocks for item in outputs))
        self.assertEqual([item.metadata["effective_mode"] for item in outputs], ["legacy", "shadow", "unified"])

    def test_legacy_feature_helper_and_validated_revision_builder_share_facts_and_decision(self):
        args = capture("Хочу свій принт", "Дякую!")
        args["commerce"] = scoped(args, custom_print_requested=True, comparison_requested=True)
        args["media"] = {"parts": [dict(source_message_id=100, mime="image/png", sha256="b"*64, validated=True)]}
        args["referral"] = scoped(args, present=True, source_message_id=100, status="ambiguous")
        legacy_facts = build_routing_facts(sources=args["sources"], media=args["media"],
            commerce=args["commerce"], referral=args["referral"])
        revision = build_turn_context(**args)
        self.assertEqual(legacy_facts, revision.facts)
        self.assertEqual(classify_live_turn(legacy_facts, now=args["captured_at"]), revision.decision)

    def test_open_objection_needs_current_bound_source_not_global_or_historical_state(self):
        args = capture("Дякую!")
        for source_id, active, expected in ((99, True, False), (100, False, False), (100, True, True)):
            with self.subTest(source_id=source_id, active=active):
                args["commerce"] = scoped(args, primary_objection="price", objections=[dict(
                    state="open", active_for_turn=active, source_message_id=source_id)])
                self.assertEqual(build_turn_context(**args).facts.objection_present, expected)

    def test_response_plan_timing_and_canonical_selection_pass_through_once(self):
        args = capture("Хочу футболку L")
        plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(), sources=args["sources"])
        args["response_plan"] = plan
        args["timing"] = scoped(args, guidance="[TIMING] waiting_on=customer; apology not required")
        selection = scoped(args, version="source-selection.v1", lines=[{"size": "L", "source_message_id": 100}])
        args["components"] = {"selection": selection}
        result = build_turn_context(**args)
        self.assertIs(result.response_plan, plan)
        self.assertEqual(result.turn_note.count("[SERVER RESPONSE PLAN]"), 1)
        self.assertIn("waiting_on=customer", result.turn_note)
        self.assertEqual(result.components["selection"]["lines"][0]["size"], "L")

    def test_capture_is_not_mutated_and_returned_components_are_immutable(self):
        args = capture("Дякую")
        args["components"] = {"selection": scoped(args, choices={"size": "L"})}
        before = deepcopy(args)
        result = build_turn_context(**args)
        self.assertEqual(args, before)
        args["components"]["selection"]["choices"]["size"] = "M"
        self.assertEqual(result.components["selection"]["choices"]["size"], "L")
        with self.assertRaises(TypeError):
            result.boundary["episode_id"] = 999

    def test_response_plan_cannot_be_silently_removed_by_optional_context_budget(self):
        args = capture("Хочу футболку L")
        args["response_plan"] = build_response_plan(preferences={}, readiness={},
            context=ReplyTruthContext(), sources=args["sources"])
        args["routing_policy"]["context_chars"] = 5
        self.assertReason("required_context_budget_exceeded", args)

    def test_budget_omission_and_metadata_do_not_copy_customer_text_or_pii(self):
        args = capture("телефон 0991234567; мені дорого")
        args["memory"] = narrative(args, "адреса вулиця 8, телефон 0991234567")
        args["routing_policy"]["context_chars"] = 5
        result = build_turn_context(**args)
        self.assertIsNone(result.memory_note)
        self.assertIn("context_budget_exceeded", result.metadata["readiness_codes"])
        self.assertNotIn("0991234567", repr(result.metadata))

    def test_classifier_receives_explicit_captured_clock_and_no_override_chain(self):
        args = capture("Дякую")
        from management.services import ig_turn_intelligence as module
        original = module.classify_live_turn
        with patch.object(module, "classify_live_turn", wraps=original) as classifier:
            result = build_turn_context(**args)
        self.assertEqual(classifier.call_args.kwargs["now"], args["captured_at"])
        self.assertEqual(result.decision.model_chain, original(result.facts, now=args["captured_at"]).model_chain)
