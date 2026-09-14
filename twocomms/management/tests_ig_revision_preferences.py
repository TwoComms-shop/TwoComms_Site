"""Preference-before-product regressions using isolated reducer source receipts."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage
from management.services.ig_commerce_projection import source_preferences_for
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn, understand_turn
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_CONFIGURATION, CLAIM_SOURCE_PREFERENCES,
    build_revision_authority_bindings, check_fact_bindings,
)
from management.services.ig_revision_live import RevisionGenerationBoundary
from management.services.ig_response_control import parse_structured_response
from management.services.ig_response_guard import ProviderResponseGuard, build_source_preference_fallback
from management.services.ig_reply_truth import ReplyTruthContext


class RevisionPreferenceTests(TestCase):
    def setUp(self):
        self.client = IgClient.objects.create(igsid="preference-fixture", language="ru")
        self.sequence = 0

    def reduce(self, text):
        self.sequence += 1
        source = InstagramBotMessage.objects.create(
            client=self.client, sender_id=self.client.igsid, role="user",
            source="webhook", mid=f"preference-{self.sequence}", text=text,
            provider_created_at=timezone.now(), provider_namespace="instagram_login:fixture",
        )
        decision = apply_turn(self.client, source, understand_turn(text), reply_payload={})
        self.client.refresh_from_db()
        return source, decision

    def fit_sequence(self):
        self.reduce("Хочу оформить заказ")
        self.reduce("Рост 190, вес 100, чёрная футболка")
        return self.reduce("Оверсайз")

    def test_full_sequence_preserves_preferences_without_product_or_price(self):
        source, decision = self.fit_sequence()
        self.assertTrue(decision.accepted)
        projection = source_preferences_for(self.client)
        self.assertEqual(projection["values"], {"fit_option_code": "oversize", "color": "black", "garment_type": "tshirt"})
        self.assertEqual(projection["evidence"]["fit_option_code"]["source_message_id"], source.pk)
        self.assertIsNone(self.client.current_product_id)
        binding = build_revision_authority_bindings(self.client, claims=(CLAIM_SOURCE_PREFERENCES,))
        self.assertTrue(binding.ready, binding.reasons)
        self.assertEqual(binding.allowed_actions, ())
        self.assertTrue(check_fact_bindings(binding.fact_bindings, revision=SimpleNamespace(client_id=self.client.pk), client=self.client))
        catalog = build_revision_authority_bindings(self.client, claims=(CLAIM_CATALOG_CONFIGURATION,), control={"fit": "oversize"})
        self.assertFalse(catalog.ready)
        self.assertEqual(catalog.reasons, ("catalog_selector_missing",))
        response, proof = build_source_preference_fallback(self.client)
        self.assertIsNotNone(response)
        self.assertIn("пожелание «оверсайз»", response.reply_text)
        self.assertIn("принт из нашего ассортимента", response.reply_text)
        self.assertIn("свой дизайн", response.reply_text)
        self.assertEqual(response.control, {})
        self.assertEqual(proof["kind"], "source_preference_fallback")

    def test_matching_fit_control_does_not_authorize_configuration_action(self):
        self.fit_sequence()
        boundary = RevisionGenerationBoundary.__new__(RevisionGenerationBoundary)
        boundary.revision = SimpleNamespace(client_id=self.client.pk, bundle_snapshot={"sources": []})
        boundary.settings = None
        boundary.baseline = build_revision_authority_bindings(self.client, claims=(CLAIM_SOURCE_PREFERENCES,))
        boundary.programme = None
        response = parse_structured_response({"reply_text": "Учту пожелание «оверсайз». Какой принт?", "controls": [{"kind": "fit", "value": "oversize"}]})
        authority = boundary.response_authority(response)
        self.assertTrue(authority.ready, authority.reasons)
        self.assertEqual(authority.allowed_actions, ())
        wrong = parse_structured_response({"reply_text": "Какой принт?", "controls": [{"kind": "fit", "value": "classic"}]})
        self.assertEqual(boundary.response_authority(wrong).reasons, ("catalog_selector_missing",))

    def test_price_and_payment_remain_unverified_in_preference_reply(self):
        self.fit_sequence()
        guard = ProviderResponseGuard(context_factory=lambda _control, _reply: ReplyTruthContext())
        result = guard.validate({"reply_text": "Ціна 900 грн.", "controls": []})
        self.assertFalse(result.valid)
        self.assertIn("unverified_price", result.reason_codes)
        result = guard.validate({"reply_text": "Оплата підтверджена.", "controls": []})
        self.assertFalse(result.valid)
        self.assertIn("unverified_payment", result.reason_codes)

    def test_source_mutation_and_new_fit_invalidate_old_binding(self):
        source, _decision = self.fit_sequence()
        old = build_revision_authority_bindings(self.client, claims=(CLAIM_SOURCE_PREFERENCES,))
        source.text = "Привет"
        source.save(update_fields=["text"])
        self.assertNotIn("fit_option_code", source_preferences_for(self.client).get("values", {}))
        self.assertFalse(check_fact_bindings(old.fact_bindings, revision=SimpleNamespace(client_id=self.client.pk), client=self.client))
        self.reduce("Классическая посадка")
        self.assertEqual(source_preferences_for(self.client)["values"]["fit_option_code"], "classic")
        self.assertFalse(check_fact_bindings(old.fact_bindings, revision=SimpleNamespace(client_id=self.client.pk), client=self.client))

    def test_fallback_requires_current_source_receipt_and_no_delivery_effect(self):
        source, decision = self.fit_sequence()
        effects = SimpleNamespace(exists=lambda: False)
        revision = SimpleNamespace(pk=123, client_id=self.client.pk, snapshot_digest="a" * 64,
                                   bundle_snapshot={}, delivery_effects=effects, action_receipts={})
        boundary = RevisionGenerationBoundary.__new__(RevisionGenerationBoundary)
        boundary.revision = revision
        boundary.has_images = False
        boundary.authority = build_revision_authority_bindings(self.client, claims=(CLAIM_SOURCE_PREFERENCES,))
        with patch.object(boundary, "validate", return_value=SimpleNamespace(valid=True)):
            self.assertEqual(boundary.contextual_fallback(policy_manifest={}), (None, {}))
            revision.action_receipts = {"commerce_reduction": {"snapshot_digest": revision.snapshot_digest, "decisions": [
                {"source_message_id": source.pk, "decision_id": decision.pk, "accepted": True, "is_stale": False}]}}
            response, proof = boundary.contextual_fallback(policy_manifest={})
            self.assertIsNotNone(response)
            self.assertEqual(proof["snapshot_digest"], revision.snapshot_digest)
            effects.exists = lambda: True
            self.assertEqual(boundary.contextual_fallback(policy_manifest={}), (None, {}))

    def test_repair_explains_price_and_preference_boundary(self):
        repaired = ProviderResponseGuard.repair({}, {}, ("unverified_price", "catalog_selector_missing"))
        guidance = repaired["contents"][-1]["parts"][0]["text"]
        self.assertIn("Remove the unverified price", guidance)
        self.assertIn("without selection controls", guidance)

    def test_unaccepted_receipt_and_legacy_snapshot_cannot_authorize_fit(self):
        from management.models import IgCommerceSelectionSession
        self.reduce("Чёрная футболка")
        session = IgCommerceSelectionSession.objects.get(client=self.client, open_slot=1)
        session.lines = [{**session.lines[0], "fit_option_code": "oversize"}]
        session.save(update_fields=["lines"])
        source = InstagramBotMessage.objects.create(
            client=self.client, sender_id=self.client.igsid, role="user", source="webhook",
            mid="stale-preference", text="Оверсайз", provider_namespace="instagram_login:fixture",
            provider_created_at=timezone.now() - timedelta(days=1),
        )
        decision = apply_turn(self.client, source, understand_turn(source.text), reply_payload={})
        self.assertFalse(decision.accepted)
        self.assertTrue(decision.is_stale)
        self.assertNotIn("fit_option_code", source_preferences_for(self.client).get("values", {}))
        response, _proof = build_source_preference_fallback(self.client)
        self.assertIsNone(response)
        self.client.sales_context = {"assisted_checkout_selection": {"fit_option_code": "oversize"}}
        self.client.save(update_fields=["sales_context"])
        self.assertNotIn("fit_option_code", source_preferences_for(self.client).get("values", {}))
        self.assertIsNone(build_source_preference_fallback(self.client)[0])

    def test_known_model_query_does_not_get_asked_again_by_narrow_fallback(self):
        self.fit_sequence()
        from management.models import IgCommerceSelectionSession
        session = IgCommerceSelectionSession.objects.get(client=self.client, open_slot=1)
        session.query_constraints = {**session.query_constraints, "query": "fixture model"}
        session.save(update_fields=["query_constraints"])
        self.assertEqual(build_source_preference_fallback(self.client), (None, {}))

    def test_garment_before_fit_without_colour_retains_source_evidence(self):
        source, _decision = self.reduce("Хочу футболку")
        self.reduce("Оверсайз")
        projection = source_preferences_for(self.client)
        self.assertEqual(projection["values"]["garment_type"], "tshirt")
        self.assertEqual(projection["evidence"]["garment_type"]["source_message_id"], source.pk)

    def test_only_affirmed_preference_mentions_update_or_prove_state(self):
        cases = (
            ("не хочу оверсайз", {}, ""),
            ("не оверсайз, а классика", {"fit": "classic"}, ""),
            ("классика или оверсайз?", {}, ""),
            ("Оверсайз?", {}, ""),
            ("класика або оверсайз", {}, ""),
            ("класика чи оверсайз", {}, ""),
            ("не оверсайз і не класика", {}, ""),
            ("не чёрную, белую футболку", {"color": "white"}, "tshirt"),
            ("чёрная или белая?", {}, ""),
            ("не футболку, а худи", {}, "hoodie"),
            ("В описании товара «оверсайз, чёрная футболка»", {}, ""),
            ("оверсайз", {"fit": "oversize"}, ""),
        )
        for text, expected_updates, expected_garment in cases:
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(dict(request.field_updates), expected_updates)
                self.assertEqual(request.garment_type, expected_garment)

        self.reduce("не хочу оверсайз")
        self.assertNotIn("fit_option_code", source_preferences_for(self.client).get("values", {}))
        source, _decision = self.reduce("Оверсайз")
        projection = source_preferences_for(self.client)
        self.assertEqual(projection["values"]["fit_option_code"], "oversize")
        self.assertEqual(projection["evidence"]["fit_option_code"]["source_message_id"], source.pk)

    def test_model_hint_cannot_promote_a_negated_quoted_or_questioned_choice(self):
        payload = {"fit": "oversize", "color": "black", "garment_type": "tshirt"}
        for text in ("не хочу оверсайз", "«чёрная футболка оверсайз» в описании", "классика или оверсайз?", "Оверсайз?"):
            with self.subTest(text=text):
                request = understand_turn(text, model_payload=payload)
                self.assertNotIn("fit", request.field_updates)
        self.assertEqual(understand_turn("оверсайз", model_payload=payload).field_updates["fit"], "oversize")

    def test_fallback_languages_are_independently_guarded(self):
        self.fit_sequence()
        for language in ("uk", "ru", "en"):
            self.client.language = language
            response, proof = build_source_preference_fallback(self.client)
            self.assertIsNotNone(response, language)
            self.assertEqual(proof["language"], language)
