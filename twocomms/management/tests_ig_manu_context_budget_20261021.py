"""Offline native regressions for required context and local reply renderers."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings


class ManuMeasurementProjectionTests(SimpleTestCase):
    def test_review_prompt_omits_only_audit_bookkeeping_and_preserves_full_frozen_state(self):
        import json
        from management.services.ig_client_state_card import CapturedClientState, _slot, render_client_state_prompt
        from management.services.ig_fulfillment_context import fulfillment_prompt_value

        value = {"status": "reviewed", "request_digest": "a" * 64, "captured_at": "2026-10-21T12:05:00+00:00",
            "current_request": {"id": "review", "created_at": "2026-10-21T12:00:00+00:00",
                "customer_requirements": [{"raw_quote": "Can it arrive before tomorrow at 10?", "source_id": 8}],
                "lines": [{"line_id": "tee", "recipient_id": "self", "target": None,
                    "observation": {"status": "unknown", "reason": "stock_unconfirmed"},
                    "preferences": {"values": {"size": "XXL"}, "evidence": {"size": {"source_message_id": 8}}}}]},
            "decision": {"id": "decision", "created_at": "2026-10-21T12:04:00+00:00",
                "decided_at": "2026-10-21T12:03:00+00:00", "arrival_guaranteed": False,
                "lines": [{"line_id": "tee", "availability": "wait",
                    "preparation": {"min_days": 7, "max_days": 10, "unit": "calendar"}}]}}
        raw = {"status": "captured", "digest": "f" * 64, "slots": {"fulfillment.review": _slot(value,
            status="confirmed", authority="verified_manager_review", refs=[{"kind": "message", "id": 8}],
            observed_at=value["captured_at"], mandatory=True, validity="valid")}}
        state = CapturedClientState(json.dumps(raw, ensure_ascii=False).encode())
        before, digest = state.as_dict(), state.digest
        rendered = render_client_state_prompt(state, budget=2400)
        self.assertFalse(rendered.oversized)
        actual = next(json.loads(line) for line in rendered.text.splitlines()
            if line.startswith("{") and json.loads(line).get("slot") == "fulfillment.review")
        expected = fulfillment_prompt_value(value)
        expected.pop("request_digest")
        expected.pop("captured_at")
        expected["current_request"].pop("created_at")
        expected["decision"].pop("created_at")
        self.assertEqual(actual["value"], expected)
        self.assertEqual(actual["observed_at"], value["captured_at"])
        self.assertEqual(actual["source_refs"], raw["slots"]["fulfillment.review"]["source_refs"])
        self.assertEqual(state.as_dict(), before)
        self.assertEqual(state.digest, digest)
        self.assertEqual(rendered.capture_digest, digest)

    def test_substantive_current_checkout_language_wins_over_other_language_history(self):
        from management.tests_ig_manu_language_20261021 import row
        from management.services.ig_reply_language import resolve_own_source_reply_language

        for text, history, expected in (
            ("Беру. Оформлюйте замовлення.", "Добавьте футболку, цвет чёрный, размер M.", "uk"),
            ("Беру. Оформляйте заказ.", "Додайте футболку, колір чорний, розмір M.", "ru"),
        ):
            decision = resolve_own_source_reply_language(sources=[row(text, 20)], history=[row(history, 10)])
            self.assertEqual((decision.reply_language, decision.basis), (expected, "current_source_observed"))

    def measurements(self):
        guide = {"schema": "size-measurements.v1", "product_id": 111, "grid_id": 7,
            "variant_id": 1, "option_key": "fit=classic", "guide_version": "a" * 64,
            "columns": [{"key": "chest", "label": "Garment width"}, {"key": "length", "label": "Length"}],
            "rows": [{"size": size, "chest": str(50 + index), "length": str(70 + index)}
                for index, size in enumerate(("S", "M", "L", "XL", "2XL"))],
            "unit": "cm", "measurement_semantics": "flat_garment",
            "intro": "Measure the garment laid flat. " * 12,
            "notes": ["Production tolerance is ±2 cm. " * 8], "fit_notes": ["Compare a comfortable garment. " * 8],
            "tolerance_source_text": ["Production tolerance is ±2 cm. " * 8],
            "legend": [{"label": "Width", "description": "Half-chest of the laid-flat garment."}],
            "comparison_guidance": "Do not equate garment width with body circumference. Never guarantee fit.",
            "selection_authority": False, "stock_authority": False}
        return {"schema": "consultation-measurements.v1", "product_id": 111,
            "guides": [{**deepcopy(guide), "variant_id": index + 1} for index in range(8)],
            "complete": True, "configuration_digest": "b" * 64,
            "selection_authority": False, "stock_authority": False}

    def test_shared_guides_round_trip_every_cell_binding_and_caveat_without_mutation(self):
        from management.services.ig_turn_intelligence import consultation_measurements_prompt_projection
        import json

        capture = self.measurements()
        original = deepcopy(capture)
        projected = consultation_measurements_prompt_projection(capture)
        self.assertEqual(len(projected["guide_contents"]), 1)
        restored = [{**projected["guide_contents"][row["content_ref"]],
            **{key: value for key, value in row.items() if key != "content_ref"}}
            for row in projected["guide_bindings"]]
        self.assertEqual(restored, capture["guides"])
        self.assertLess(len(json.dumps(projected)), len(json.dumps(capture)) // 2)
        self.assertEqual(capture, original)

    def test_distinct_guide_values_and_oversized_required_facts_are_never_discarded(self):
        from management.tests_ig_turn_intelligence import capture
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_plan import build_response_plan
        from management.services.ig_turn_intelligence import (
            TurnContextError, build_turn_context, consultation_measurements_prompt_projection,
        )

        measurements = self.measurements()
        measurements["guides"][1]["rows"][-1]["chest"] = "77"
        projected = consultation_measurements_prompt_projection(measurements)
        self.assertEqual(len(projected["guide_contents"]), 2)
        self.assertEqual(projected["guide_contents"][1]["rows"][-1]["chest"], "77")
        measurements["guides"][1]["intro"] = "A necessary distinct garment measurement caveat. " * 1000
        args = capture("Please help me choose the size.")
        plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(), sources=args["sources"])
        plan = replace(plan, configuration={**plan.configuration, "consultation_measurements": measurements})
        original, digest = deepcopy(plan.as_dict()), plan.digest
        with self.assertRaises(TurnContextError) as caught:
            build_turn_context(**args, response_plan=plan)
        self.assertEqual(caught.exception.reason, "required_context_budget_exceeded")
        self.assertEqual(plan.as_dict(), original)
        self.assertEqual(plan.digest, digest)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=2400, GEMINI_NONLIVE_ADMISSION_MODE="shadow")
class ManuOwnedRendererLanguageTests(TestCase):
    def setUp(self):
        from management.tests_ig_manu_language_20261021 import ManuNativeLanguagePipelineTests
        ManuNativeLanguagePipelineTests.setUp(self)

    def publish(self, **kwargs):
        from management.tests_ig_manu_language_20261021 import ManuNativeLanguagePipelineTests
        return ManuNativeLanguagePipelineTests.publish(self, **kwargs)

    def fixture(self):
        from management.tests_ig_manu_language_20261021 import ManuNativeLanguagePipelineTests
        return ManuNativeLanguagePipelineTests.fixture(self)

    def test_short_followup_local_media_and_catalog_use_owned_history(self):
        from management.services.ig_revision_holding import resolve_revision_reply_language
        from management.services.ig_catalog_choice_presentations import _presentation_locale
        from management.services.ig_media_response import media_limitation_reply

        fixture = self.fixture()
        original = deepcopy(fixture.revision.bundle_snapshot)
        for _ in range(2):
            decision = resolve_revision_reply_language(fixture.revision)
            self.assertEqual((decision.reply_language, decision.basis), ("en", "captured_history_observed"))
            self.assertEqual(_presentation_locale(fixture.revision, SimpleNamespace()), "en")
            text = media_limitation_reply(SimpleNamespace(language=decision.template_family), media_kinds=("audio",))
            self.assertTrue(text.startswith("I could not"), text)
        self.assertEqual(fixture.revision.bundle_snapshot, original)
        fixture.customer.refresh_from_db()
        self.assertEqual(fixture.customer.language, "uk")

    def test_future_and_foreign_rows_do_not_supply_local_language(self):
        from management.models import IgClient, InstagramBotMessage
        from management.services.ig_revision_holding import resolve_revision_reply_language

        fixture = self.fixture()
        foreign = IgClient.objects.create(igsid="other-renderer-client", language="ru")
        for customer, namespace, at in (
            (fixture.customer, "instagram_login:owner-1", self.now + timedelta(seconds=1)),
            (foreign, "instagram_login:owner-1", self.now - timedelta(seconds=1)),
            (fixture.customer, "instagram_login:another-owner", self.now - timedelta(seconds=1)),
        ):
            InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid,
                provider_namespace=namespace, role="user", source="webhook", status="done",
                text="Пожалуйста, отвечайте на русском языке.", provider_created_at=at)
        self.assertEqual(resolve_revision_reply_language(fixture.revision).reply_language, "en")

    def test_captured_context_reuses_frozen_history_without_reads(self):
        from management.services.ig_revision_holding import resolve_revision_reply_language

        fixture = self.fixture()
        with self.assertNumQueries(0):
            language = resolve_revision_reply_language(fixture.revision,
                captured_context=fixture.prepared.context)
        self.assertEqual(language.reply_language, "en")

    def test_before_reset_and_tampered_seal_history_do_not_supply_language(self):
        from management.services.ig_revision_holding import resolve_revision_reply_language

        fixture = self.fixture()
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor",
                return_value=fixture.messages[-1].pk):
            self.assertEqual(resolve_revision_reply_language(fixture.revision).reply_language, "")
        fixture.revision.snapshot_digest = "0" * 64
        self.assertEqual(resolve_revision_reply_language(fixture.revision).reply_language, "")


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified",
    IG_REVISION_EXECUTION_ENABLED=False, IG_CLIENT_STATE_PROMPT_TOKENS=2400)
class ManuNativeRequiredContextDeliveryTests(TransactionTestCase):
    """Real catalogue/choices/capture/validator/outbox; provider HTTP is fake."""
    reset_sequences = True

    def setUp(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        RevisionLiveTests.setUp(self)

    def _message(self, *args, **kwargs):
        from management.tests_ig_revision_live import RevisionLiveTests
        return RevisionLiveTests._message(self, *args, **kwargs)

    def _prepare(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        return RevisionLiveTests._prepare(self)

    def _execute(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        return RevisionLiveTests._execute(self)

    def _generate(self, payload, **kwargs):
        from management.tests_ig_revision_live import RevisionLiveTests
        return RevisionLiveTests._generate(self, payload, **kwargs)

    def test_selected_tee_two_distinct_grids_reaches_payload_validator_and_one_sent_effect(self):
        import json
        from django.db import transaction
        from django.utils import timezone
        from management.models import IgClient, IgCustomerTurn, IgTurnMessage, IgCommerceSelectionSession, InstagramBotSettings
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from management.services.ig_revision_live import RevisionGenerationBoundary
        from management.services.ig_revision_media import collect_revision_media
        from management.services.ig_revision_outbox import PublicationBinding
        from management.services.ig_turn_integration import prepare_revision_turn_context
        from management.services.ig_turn_revisions import create_collecting_revision
        from product_catalog.models import ProductOptionSizeGrid
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid

        # A normal single selected tee has two distinct fit grids. Each grid
        # carries real cells, units, tolerance and fitting caveats. No guide
        # is discarded just because the current customer message is short.
        category = Category.objects.create(name="T-shirts", slug="tshirts")
        catalog = Catalog.objects.create(name="Native measured tee", slug="native-measured-tee")
        product = Product.objects.create(title="Atlas Wave", slug="native-measured-tee", category=category,
            catalog=catalog, price=900, status="published")
        color = Color.objects.create(name="Black", primary_hex="#111111")
        ProductColorVariant.objects.create(product=product, color=color, stock=20, is_default=True)
        for ordinal, fit in enumerate(("classic", "oversize")):
            ProductFitOption.objects.create(product=product, code=fit, label=fit.title(), is_active=True)
            grid = SizeGrid.objects.create(catalog=catalog, name=fit, guide_data={
                "columns": [{"key": "size", "label": "Size"}, {"key": "chest", "label": "Garment width"},
                    {"key": "length", "label": "Length"}],
                "rows": [{"size": size, "display_size": size, "chest": str(50 + index + ordinal * 6),
                    "length": str(70 + index)} for index, size in enumerate(("S", "M", "L", "XL", "XXL"))],
                "intro": "Measure the garment laid flat in cm. " * 12,
                "notes": ["Production tolerance is ±2 cm. " * 8],
                "fit_notes": ["Compare the same measurement on a comfortable garment. " * 5],
                "legend": [{"label": "Garment width", "description": "Half-chest measured on the garment laid flat."}],
            })
            ProductOptionSizeGrid.objects.create(product=product, option_key="fit=" + fit, size_grid=grid)
        self.revision.delete()
        self.turn.delete()
        self.source.delete()
        now = timezone.now()
        deadline = self._message("My boyfriend is in Kharkiv and will leave tomorrow at 10. Please help me.", "native-deadline")
        deadline.provider_created_at = now - timedelta(minutes=2)
        deadline.status = "done"
        deadline.save(update_fields=["provider_created_at", "status"])
        choice = self._message("I want Atlas Wave t-shirt, size XXL", "native-size-choice")
        choice.provider_created_at = now - timedelta(minutes=1)
        choice.save(update_fields=["provider_created_at"])
        with transaction.atomic():
            InstagramBotSettings.objects.select_for_update().get(pk=self.settings.pk)
            customer = IgClient.objects.select_for_update().get(pk=self.customer.pk)
            reduced = reduce_inbound_commerce_source(customer, choice,
                expected_provider_namespace=choice.provider_namespace)
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual(session.lines[session.active_index]["product_id"], product.pk)
        self.assertEqual(session.lines[session.active_index]["size"], "XXL")
        self.source = self._message("no?", "native-budget-followup")
        self.source.provider_created_at = now
        self.source.save(update_fields=["provider_created_at"])
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=now, window_deadline=now)
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        boundary = RevisionGenerationBoundary(self.revision, self.token, self.settings, publication, has_images=False)
        collection = collect_revision_media(self.revision.pk, self.token)
        prepared = prepare_revision_turn_context(self.revision, generation_boundary=boundary,
            collection=collection, settings_row=self.settings, publication=publication)
        plan = boundary.response_plan
        self.assertEqual(len(plan.configuration["consultation_measurements"]["guides"]), 2)
        self.assertNotIn("consultation_measurements", plan.prompt_projection()["configuration"])
        self.assertLessEqual(prepared.context.metadata["budget"]["selected_chars"], 12000)
        required_chars = sum(len(prepared.context.context_blocks[key]) for key in
            ("context:response_plan", "context:source_cart"))
        duplicate_chars = len(',"consultation_measurements":' + json.dumps(
            plan.configuration["consultation_measurements"], ensure_ascii=False, separators=(",", ":")))
        self.assertGreater(required_chars + duplicate_chars, 12000,
            "This native fixture must reproduce the previous duplicated-guide overflow.")
        original_sources, original_plan, original_digest = deepcopy(self.revision.bundle_snapshot), deepcopy(plan.as_dict()), plan.digest
        original_cart, original_state = deepcopy(plan.source_cart_capture), prepared.state.as_dict()
        original_lines = deepcopy(session.lines)
        self.parsed = {"reply_text": "You chose size 2XL for Atlas Wave. Which fit would you prefer: classic or oversize?", "controls": []}
        result, generate, http = self._execute()
        self.assertEqual(generate.call_count, 1, result.reasons)
        self.assertEqual(http.call_count, 1, result.reasons)
        self.revision.refresh_from_db()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "sent", result.reasons)
        self.assertTrue(effect.provider_message_id)
        payload = self.generation_calls[0]
        contents = json.dumps(payload, ensure_ascii=False)
        self.assertIn("Reply in English.", contents)
        self.assertIn("garment", contents.casefold())
        self.assertIn("XXL", contents)
        self.assertIn("fit=classic", contents)
        self.assertIn("fit=oversize", contents)
        for guide in plan.configuration["consultation_measurements"]["guides"]:
            self.assertIn(guide["rows"][-1]["chest"], contents)
        self.assertEqual(self.revision.bundle_snapshot, original_sources)
        self.assertEqual(plan.as_dict(), original_plan)
        self.assertEqual(plan.digest, original_digest)
        self.assertEqual(plan.source_cart_capture, original_cart)
        self.assertEqual(prepared.state.as_dict(), original_state)
        session.refresh_from_db()
        self.assertEqual(session.lines, original_lines)
        _, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.count(), 1)
