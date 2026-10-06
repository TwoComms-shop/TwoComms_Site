"""Captured cart → authority → immutable proposal → restart, without provider I/O."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_response_cart_fence as cart_fixtures
from management import tests_ig_revision_proposal as proposal_fixtures
from management import tests_ig_source_cart_catalog as catalog_fixtures
from management import tests_ig_turn_capture as turn_fixtures
from management import tests_ig_revision_preference_fallback as fallback_fixtures
from management.models import (BotPolicyPublication, IgClient, IgCommercialEpisode,
    IgCommerceSelectionSession, InstagramBotMessage, InstagramBotSettings)
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import ValidatedResponse
from management.services.ig_response_plan import capture_response_plan
from management.services.ig_response_cart_fence import capture_source_cart_authority
from management.services.ig_revision_authority import (CLAIM_SOURCE_PREFERENCES, CLAIM_PUBLIC_POLICY_INPUTS,
    FACT_CLAIMS, MAX_CLAIMS, SUPPORTED_CLAIMS, build_revision_authority_bindings, check_fact_bindings)
from management.services.ig_revision_live import (
    RevisionGenerationBoundary, _captured_reply_truth, _restore_response, _prepare_effects,
)
from management.services.ig_revision_outbox import PublicationBinding, _safe_bindings
from management.services.ig_revision_proposal import store_revision_generation_proposal


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeCartFallbackDeliveryTests(TransactionTestCase):
    """Real sealed reductions, retained failure, outbox and source fences."""
    reset_sequences = True
    _live_setup = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._live_setup
    _message = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._message
    _prepare = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._prepare
    _execute = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._execute
    _fit_fixture = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._fit_fixture
    _record = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._record

    def setUp(self):
        self._live_setup()
        catalog_fixtures.SourceCartCatalogTests.setUp(self)

    def _generate(self, *_args, **_kwargs):
        self.fail("A retained failed request must not cause another generation")

    def admitted_cart(self):
        result = self._fit_fixture(source_texts=[
            f"add https://twocomms.shop/product/{self.products[0].slug}/ black size M fit classic",
            f"add https://twocomms.shop/product/{self.products[1].slug}/ pink fit classic for friend",
        ])
        self.assertTrue(result.ready, result.reason)
        self.revision.refresh_from_db()
        return result

    def test_actual_multiline_fallback_sends_once_and_preserves_each_source_and_recipient(self):
        import hashlib
        fallback = self.admitted_cart()
        reduction = self.revision.action_receipts["commerce_reduction"]
        source = self.revision.bundle_snapshot["sources"][-1]
        self.assertNotEqual(source["source_digest"], hashlib.sha256(source["text"].encode()).hexdigest())
        self.assertEqual(reduction["decisions"][-1]["source_digest"], source["source_digest"])
        replay = self._record()
        self.assertTrue(replay.ready, replay.reason)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt, fallback.receipt)
        result, generate, http = self._execute()
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1, result.reasons)
        self.revision.refresh_from_db()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "sent")
        text = effect.payload["message"]["text"]
        self.assertIn("Позиція 1 для себе · Classic: ви обрали розмір M.", text)
        self.assertIn("Позиція 2 для друга · Storm: ви обрали колір рожевий.", text)
        self.assertEqual(text.count("?"), 1)
        self.assertIn("Позиція 2 для друга · Storm:", text.splitlines()[-1])
        self.assertNotIn("Позиція 2 для друга · Storm: ви обрали розмір M", text)
        self.assertEqual(self.revision.generation_proposal, {})
        self.failed_graph.refresh_from_db()
        self.assertEqual(self.failed_graph.terminal_resolution, "failed")
        self.assertIsNone(self.failed_graph.winner_attempt_id)
        _, next_generate, next_http = self._execute()
        next_generate.assert_not_called()
        next_http.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.count(), 1)

    def test_changed_nonactive_original_source_blocks_recorded_fallback_before_http(self):
        self.admitted_cart()
        InstagramBotMessage.objects.filter(pk=self.fit_sources[0].pk).update(text="Changed nonactive source")
        result, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()
        self.assertNotEqual(result.state, "completed")
        self.assertFalse(self.revision.delivery_effects.filter(state="sent").exists())

    def test_changed_nonactive_configuration_blocks_recorded_fallback_before_http(self):
        self.admitted_cart()
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        lines = deepcopy(session.lines)
        lines[0]["size"] = "L"
        IgCommerceSelectionSession.objects.filter(pk=session.pk).update(lines=lines)
        result, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()
        self.assertNotEqual(result.state, "completed")
        self.assertFalse(self.revision.delivery_effects.filter(state="sent").exists())


class CartLiveFixture(cart_fixtures.CartFenceFixture):
    def sealed_cart(self, *, noop=False):
        first, second, session, capture = self.two_lines()
        sources = [first, second]
        if noop:
            observed, _ = self.reduce("Гаразд")
            sources.append(observed)
            capture = self.capture()
        self.client_row = self.customer
        revision, collection, _ = turn_fixtures.RevisionTurnCaptureTests.seal(self, *sources)
        self.token = revision.claim_token
        admitted = capture_source_cart_authority(self.customer, capture, revision=revision)
        self.assertTrue(admitted.ready, admitted.reason)
        authority = build_revision_authority_bindings(self.customer,
            claims=(CLAIM_SOURCE_PREFERENCES,), control={"source_cart_fence": admitted.binding})
        self.assertTrue(authority.ready, authority.reasons)
        return revision, collection, session, capture, authority, sources

    def check(self, revision, authority):
        return check_fact_bindings(authority.fact_bindings, revision=revision, client=self.customer)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CartLiveClaimIntegrationTests(CartLiveFixture, TestCase):
    def test_persisted_source_claim_checks_nonactive_configuration_without_active_projection_escape(self):
        revision, _, session, _, authority, _ = self.sealed_cart()
        durable = json.loads(json.dumps(list(authority.fact_bindings)))
        self.assertTrue(check_fact_bindings(durable, revision=revision, client=self.customer))
        lines = deepcopy(session.lines)
        lines[0]["size"] = "M"
        IgCommerceSelectionSession.objects.filter(pk=session.pk).update(lines=lines)
        self.assertFalse(check_fact_bindings(durable, revision=revision, client=self.customer))

    def test_persisted_source_claim_checks_supporting_noop_not_just_current_choice_evidence(self):
        revision, _, _, capture, authority, sources = self.sealed_cart(noop=True)
        observed = sources[-1]
        self.assertIn(observed.pk, capture["fence"]["source_ids"])
        self.assertTrue(self.check(revision, authority))
        InstagramBotMessage.objects.filter(pk=observed.pk).update(text="mutated supporting source")
        self.assertFalse(self.check(revision, authority))

    def test_current_permission_epoch_is_not_replaced_by_stored_cart_permission(self):
        revision, _, _, _, authority, _ = self.sealed_cart()
        self.assertTrue(self.check(revision, authority))
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1)
        self.assertFalse(self.check(revision, authority))

    def test_source_claim_keeps_existing_eight_claim_and_4096_byte_gates(self):
        revision, _, _, _, authority, _ = self.sealed_cart()
        original = authority.fact_bindings[0]
        self.assertEqual(MAX_CLAIMS, 8)
        rows = [{**deepcopy(original), "claim": claim} for claim in sorted(FACT_CLAIMS)]
        self.assertEqual(len(rows), 8)
        self.assertEqual(_safe_bindings(rows), rows)
        oversized = deepcopy(original)
        oversized["subjects"]["diagnostic"] = "x" * 4096
        with self.assertRaisesMessage(ValueError, "binding_too_large"):
            _safe_bindings([oversized])
        too_many = build_revision_authority_bindings(self.customer, claims=tuple(SUPPORTED_CLAIMS))
        self.assertFalse(too_many.ready)
        self.assertEqual(too_many.reasons, ("claim_count_invalid",))

    def test_claim_checker_does_not_recapture_choices_or_write_after_persistence(self):
        revision, _, _, _, authority, _ = self.sealed_cart()
        with patch("management.services.ig_commerce_projection.capture_current_selection_lines",
                side_effect=AssertionError("persisted claim must use compact fence")), CaptureQueriesContext(connection) as queries:
            self.assertTrue(self.check(revision, authority))
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))


class RealCartCatalogFixture(cart_fixtures.CartFenceFixture):
    def setUp(self):
        super().setUp()
        catalog_fixtures.SourceCartCatalogTests.setUp(self)

    def real_cart_plan(self):
        first, _ = self.reduce(f"add https://twocomms.shop/product/{self.products[0].slug}/ black size M fit classic")
        second, decision = self.reduce(f"add https://twocomms.shop/product/{self.products[1].slug}/ pink size L fit classic for friend")
        self.client_row = self.customer
        revision, collection, _ = turn_fixtures.RevisionTurnCaptureTests.seal(self, first, second)
        capture = self.capture()
        self.assertEqual(capture["status"], "captured", capture)
        plan = capture_response_plan(self.customer, revision=revision, source_cart_capture=capture)
        self.assertFalse(plan.plan_gap, plan.plan_gap)
        self.assertEqual(len(plan.line_plans), 2)
        self.assertEqual([row.as_dict()["authority"]["prices"] for row in plan.line_plans],
            [[str(Decimal(str(product.price)).quantize(Decimal("0.01")))] for product in self.products])
        return revision, collection, decision.session, capture, plan, [first, second]


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CartLiveTruthIntegrationTests(RealCartCatalogFixture, TestCase):
    def test_two_actual_catalog_prices_and_source_wishes_are_scoped_before_aggregate_guard(self):
        _, _, _, _, plan, _ = self.real_cart_plan()
        for text in ("Classic коштує 790 грн, Storm коштує 1290 грн.",
                "Ви обрали для Classic розмір M, ви обрали для Storm розмір L."):
            with self.subTest(text=text):
                self.assertTrue(_captured_reply_truth(plan, ValidatedResponse(reply_text=text), ReplyTruthContext()).valid)
        swapped = _captured_reply_truth(plan, ValidatedResponse(reply_text="Classic коштує 1290 грн, Storm коштує 790 грн."),
            ReplyTruthContext(authorized_prices=(Decimal("790"), Decimal("1290"))))
        self.assertFalse(swapped.valid)

    def test_real_decimal_dot_and_comma_prices_keep_line_and_cents_authority(self):
        # The actual catalog stores integer hryvnias, so its verified cents are
        # .00. Nonzero cents are not a fixture authority and must be rejected.
        _, _, _, _, plan, _ = self.real_cart_plan()
        for text in ("Classic коштує 790.00 грн, Storm коштує 1290.00 грн.",
                "Classic коштує 790,00 грн, Storm коштує 1290,00 грн."):
            with self.subTest(text=text):
                result = _captured_reply_truth(plan, ValidatedResponse(reply_text=text), ReplyTruthContext())
                self.assertTrue(result.valid, result.reasons)
        for text in ("Classic коштує 790,50 грн, Storm коштує 1290,75 грн.",
                "Classic коштує 1290,00 грн, Storm коштує 790,00 грн."):
            with self.subTest(unverified=text):
                result = _captured_reply_truth(plan, ValidatedResponse(reply_text=text), ReplyTruthContext())
                self.assertFalse(result.valid)

    def test_line_prices_do_not_authorize_false_payment_or_foreign_url(self):
        _, _, _, _, plan, _ = self.real_cart_plan()
        for text in ("Classic коштує 790 грн. Оплата підтверджена.",
                "Classic коштує 790 грн. https://foreign.example/checkout"):
            with self.subTest(text=text):
                result = _captured_reply_truth(plan, ValidatedResponse(reply_text=text), ReplyTruthContext())
                self.assertFalse(result.valid)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CartLiveProposalIntegrationTests(RealCartCatalogFixture, TransactionTestCase):
    reset_sequences = True
    _digest = staticmethod(proposal_fixtures.RevisionGenerationProposalTests._digest)
    _policy_manifest = proposal_fixtures.RevisionGenerationProposalTests._policy_manifest
    _create_generation_graph = proposal_fixtures.RevisionGenerationProposalTests._create_generation_graph

    def setUp(self):
        super().setUp()
        snapshot = {"schema_version": 1, "instructions": []}
        self.publication = BotPolicyPublication.objects.create(version=1, kind="publish", schema_version=1,
            snapshot=snapshot, snapshot_hash=self._digest(snapshot), compiler_version="instruction-set-v1", instruction_count=0)
        self.settings = InstagramBotSettings.objects.get(pk=1)
        self.settings.active_instruction_publication = self.publication
        self.settings.is_enabled = self.settings.ai_enabled = True
        self.settings.save(update_fields=["active_instruction_publication", "is_enabled", "ai_enabled"])
        self.publication_binding = PublicationBinding(self.publication.pk, 1, self.publication.snapshot_hash)
        self.revision, collection, self.session, self.cart_capture, self.plan, self.sources = self.real_cart_plan()
        # Live accounting permits one graph anchored to the final immutable
        # bundle source; the reused single-source fixture follows this too.
        self.source = self.sources[-1]
        self.token = self.revision.claim_token
        self.request_id = "cart-integration-request"
        self.model = "gemini-3.7-flash"
        self.generated_at = timezone.now()
        self.media = deepcopy(collection.binding)
        self.media.update(actual_inline_count=0, actual_content_hashes=[])
        admitted = capture_source_cart_authority(self.customer, self.cart_capture, revision=self.revision)
        self.assertTrue(admitted.ready, admitted.reason)
        self.authority = build_revision_authority_bindings(self.customer,
            claims=(CLAIM_SOURCE_PREFERENCES, CLAIM_PUBLIC_POLICY_INPUTS),
            control={"source_cart_fence": admitted.binding}, settings_obj=self.settings)
        self.assertTrue(self.authority.ready, self.authority.reasons)
        self._create_generation_graph()

    def store(self, **overrides):
        kwargs = dict(source_message_ids=[row.pk for row in self.sources], settings_id=1,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.publication_binding,
            request_id=self.request_id, actual_model=self.model, generated_at=self.generated_at,
            response=ValidatedResponse(reply_text="Classic коштує 790 грн, Storm коштує 1290 грн."),
            turn_intelligence={}, request_media_manifest=self.media, policy_manifest=self._policy_manifest(),
            authority=self.authority, source_cart_capture=self.cart_capture)
        kwargs.update(overrides)
        return store_revision_generation_proposal(self.revision.pk, self.token, **kwargs)

    def test_exact_cart_artifact_is_write_once_and_restart_uses_original(self):
        original = deepcopy(self.cart_capture)
        result = self.store()
        self.assertTrue(result.created, result.reasons)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["source_cart_capture"], original)
        replay = self.store()
        self.assertTrue(replay.stored, replay.reasons)
        self.assertFalse(replay.created)
        self.assertEqual(result.digest, replay.digest)
        restored = _restore_response(self.revision.generation_proposal)
        restored_plan = capture_response_plan(self.customer, revision=self.revision,
            source_cart_capture=self.revision.generation_proposal["source_cart_capture"])
        self.assertEqual(restored_plan.source_cart_capture, original)
        self.assertTrue(_captured_reply_truth(restored_plan, restored, ReplyTruthContext()).valid)
        self.cart_capture["lines"][0]["recipient_id"] = "modified-caller-object"
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["source_cart_capture"], original)

    def test_source_fence_without_original_artifact_cannot_store_reconstructable_draft(self):
        result = self.store(source_cart_capture=None)
        self.assertFalse(result.stored)
        self.assertIn("checkout_generation_artifact_missing", result.reasons)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal, {})

    def test_original_raw_and_semantic_digest_must_match_persisted_fact_reference(self):
        from management.services.ig_turn_intelligence import capture_digest
        changed = deepcopy(self.cart_capture)
        changed["fence"]["owner_digest"] = "f" * 64
        changed["capture_digest"] = capture_digest({key: value for key, value in changed.items() if key != "capture_digest"})
        result = self.store(source_cart_capture=changed)
        self.assertFalse(result.stored)
        self.assertEqual(result.reasons, ("generation_cart_authority_mismatch",))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal, {})

    def test_actual_restart_boundary_rejects_nonactive_change_without_provider_or_latest_replacement(self):
        stored = self.store()
        self.assertTrue(stored.stored, stored.reasons)
        self.revision.refresh_from_db()
        original = deepcopy(self.revision.generation_proposal["source_cart_capture"])
        boundary = RevisionGenerationBoundary(self.revision, self.token, self.settings,
            self.publication_binding, source_cart_capture=original)
        self.assertTrue(boundary.baseline.ready, boundary.baseline.reasons)
        lines = deepcopy(self.session.lines)
        lines[0]["size"] = "L"
        IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(lines=lines)
        resumed = RevisionGenerationBoundary(self.revision, self.token, self.settings,
            self.publication_binding, source_cart_capture=original)
        self.assertFalse(resumed.baseline.ready)
        self.assertEqual(resumed.response_plan.source_cart_capture, original)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["source_cart_capture"], original)

    def test_actual_prepare_effects_restores_saved_cart_and_rejects_swapped_reply(self):
        stored = self.store()
        self.assertTrue(stored.stored, stored.reasons)
        self.revision.refresh_from_db()
        restored = _restore_response(self.revision.generation_proposal)
        effects, reasons = _prepare_effects(self.revision, restored, self.settings)
        self.assertFalse(reasons, reasons)
        self.assertTrue(any(row.get("group") == "substantive_text" for row in effects))
        swapped = replace(restored, reply_text="Classic коштує 1290 грн, Storm коштує 790 грн.")
        effects, reasons = _prepare_effects(self.revision, swapped, self.settings)
        self.assertFalse(effects)
        self.assertIn("response_plan_line_claim_unverified", reasons)
