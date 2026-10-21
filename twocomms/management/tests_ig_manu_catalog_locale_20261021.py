"""Native exact-catalogue-title language wiring, with provider I/O replaced."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management.models import IgClient, IgCustomerTurn, IgTurnMessage, InstagramBotSettings
from management.services.ig_reply_language import reply_language_mismatch, response_plan_language_names
from management.services.ig_revision_holding import resolve_revision_reply_language
from management.services.ig_revision_live import RevisionGenerationBoundary, _generate_proposal
from management.services.ig_revision_media import collect_revision_media
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_capture import detached_payload
from management.services.ig_turn_integration import prepare_revision_turn_context
from management.services.ig_turn_revisions import create_collecting_revision


TITLE = "Футболка «Харків Вокзальна»"
BARE_TITLE = "Футболка Харків Вокзальна"


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=2400)
class ManuCatalogLocaleNativeTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare

    def fixture(self):
        from django.db import transaction
        from product_catalog.models import VariantSizeRule
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source

        category = Category.objects.create(name="T-shirts", slug="tshirts")
        catalog = Catalog.objects.create(name="Locale native tee", slug="locale-native-tee")
        grid = SizeGrid.objects.create(catalog=catalog, name="Locale sizes", guide_data={
            "columns": [{"key": "size", "label": "Size"}],
            "rows": [{"size": size, "display_size": size} for size in ("M", "XXL")]})
        self.product = Product.objects.create(title=TITLE, slug="locale-native-tee", category=category,
            catalog=catalog, size_grid=grid, price=900, status="published")
        ProductFitOption.objects.create(product=self.product, code="classic", label="Classic",
            is_active=True, is_default=True)
        color = Color.objects.create(name="Black", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(product=self.product, color=color, stock=20, is_default=True)
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="XXL", is_enabled=True, stock=20)
        self.revision.delete()
        self.turn.delete()
        self.source.delete()
        now = timezone.now()
        history = self._message("My boyfriend is in Kharkiv. Please help me with your shirt.", "locale-history")
        history.provider_created_at, history.status = now - timedelta(minutes=1), "done"
        history.save(update_fields=["provider_created_at", "status"])
        self.source = self._message(BARE_TITLE + " 2XL", "locale-current")
        self.source.provider_created_at = now
        self.source.save(update_fields=["provider_created_at"])
        with transaction.atomic():
            InstagramBotSettings.objects.select_for_update().get(pk=self.settings.pk)
            customer = IgClient.objects.select_for_update().get(pk=self.customer.pk)
            result = reduce_inbound_commerce_source(customer, self.source,
                expected_provider_namespace=self.source.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.current_product_id, self.customer.current_size), (self.product.pk, "XXL"))
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=now, window_deadline=now)
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        boundary = RevisionGenerationBoundary(self.revision, self.token, self.settings, publication, has_images=False)
        collection = collect_revision_media(self.revision.pk, self.token)
        prepared = prepare_revision_turn_context(self.revision, generation_boundary=boundary, collection=collection,
            settings_row=self.settings, publication=publication)
        return boundary, collection, publication, prepared

    def test_native_bare_title_and_size_keep_english_in_policy_primary_generation_and_validator(self):
        from management.services.call_ai_analysis import CallAIAnalysisError

        boundary, collection, publication, prepared = self.fixture()
        original, plan_digest = deepcopy(self.revision.bundle_snapshot), boundary.response_plan.digest
        self.assertEqual(prepared.policy_inputs.knowledge_language, "en")
        self.assertIn(TITLE, response_plan_language_names(boundary.response_plan))
        calls, validations = [], []

        def provider(payload, **kwargs):
            calls.append(deepcopy(payload))
            field = boundary.response_plan.next_selector
            question = {"color": "Which color would you like?", "fit": "Which fit would you like?",
                "size": "Which size would you like?"}.get(field, "How can I help you further?")
            candidate = {"reply_text": f"You chose size 2XL for {TITLE}. {question}", "controls": []}
            validations.append(kwargs["result_validator"](candidate, usage={}))
            raise CallAIAnalysisError("offline catalogue locale fixture")

        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=provider):
            _generate_proposal(self.revision, self.token, self.settings, publication, collection)
        self.assertEqual(len(calls), 1)
        self.assertIn("Reply in English.", calls[0]["system_instruction"]["parts"][0]["text"])
        self.assertEqual(len(validations), 1)
        self.assertTrue(validations[0].valid, validations[0].reason_codes)
        self.assertIn("2XL", str(calls[0]["contents"]))
        self.assertEqual(self.revision.bundle_snapshot, original)
        self.assertEqual(boundary.response_plan.digest, plan_digest)

    def test_frozen_names_reach_guard_and_prepared_resolution_without_queries_or_mutable_lookup(self):
        boundary, _collection, _publication, prepared = self.fixture()
        with self.assertNumQueries(0):
            decision = resolve_revision_reply_language(self.revision, captured_context=prepared.context,
                response_plan=boundary.response_plan)
            self.assertEqual(decision.reply_language, "en")
            # A short English phrase plus a longer bare Cyrillic title needs
            # exact frozen-name masking, rather than the script-majority rule.
            self.assertFalse(reply_language_mismatch("I want " + BARE_TITLE, decision))
            self.assertTrue(reply_language_mismatch("Пожалуйста, подскажите размер " + BARE_TITLE, decision))
        self.assertNotIn("_observation_names", decision.as_dict())
        self.assertNotIn(TITLE, str(decision.as_dict()))
        self.product.title = "Changed live catalogue title"
        self.product.save(update_fields=["title"])
        boundary.response_plan.configuration["product_title"] = "Changed request object"
        with self.assertNumQueries(0):
            frozen = resolve_revision_reply_language(self.revision, captured_context=prepared.context,
                response_plan=boundary.response_plan)
            self.assertFalse(reply_language_mismatch("I want " + BARE_TITLE, frozen))
        self.assertIn(TITLE, detached_payload(prepared.context.components)["language_catalog_names"]["names"])

    def test_early_owned_plan_masks_only_exact_name_and_preserves_real_language_requests(self):
        boundary, _collection, _publication, _prepared = self.fixture()
        decision = resolve_revision_reply_language(self.revision, response_plan=boundary.response_plan)
        self.assertEqual(decision.reply_language, "en")
        sources = deepcopy(self.revision.bundle_snapshot["sources"])
        for text, expected in (("Пожалуйста, подскажите размер " + BARE_TITLE, "ru"),
                ("Підкажіть, будь ласка, розмір " + BARE_TITLE, "uk"),
                ("Reply in Russian. " + BARE_TITLE, "ru"),
                ("Футболка Харків Вокзаль", "uk")):
            sources[0]["text"] = text
            # Pure resolver receives only this explicitly selected test body;
            # source admission/digest fences are exercised above by native sealing.
            from management.services.ig_reply_language import resolve_own_source_reply_language
            result = resolve_own_source_reply_language(sources=sources,
                history=[{"message_id": self.source.pk - 1, "role": "user", "text": "Please help me with your shirt."}],
                non_language_names=response_plan_language_names(boundary.response_plan))
            self.assertEqual(result.reply_language, expected, text)

    def test_native_configuration_presentation_uses_english_prose_with_bare_current_title(self):
        from management.services.ig_catalog_choice_presentations import prepare_catalog_configuration_choices
        from management.services.ig_response_control import ValidatedResponse

        boundary, _collection, _publication, _prepared = self.fixture()
        original, digest = deepcopy(self.revision.bundle_snapshot), boundary.response_plan.digest
        response = ValidatedResponse(reply_text="Which colour would you prefer?", controls=())
        prepared = prepare_catalog_configuration_choices(self.revision, response, self.settings,
            kind="color", response_plan=boundary.response_plan)
        self.assertTrue(prepared.ready, prepared.reasons)
        self.assertEqual(prepared.manifest["scope"]["locale"], "en")
        payloads = json.dumps([effect["payload"] for effect in prepared.effects], ensure_ascii=False)
        self.assertIn("Choose a colour:", payloads)
        self.assertNotIn("Оберіть колір", payloads)
        self.assertNotIn("Выберите цвет", payloads)
        self.assertEqual(self.revision.bundle_snapshot, original)
        self.assertEqual(boundary.response_plan.digest, digest)
