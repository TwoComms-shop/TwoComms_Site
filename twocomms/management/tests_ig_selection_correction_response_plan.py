"""Real audited correction → captured plan → reply/control/fallback boundaries."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management import tests_ig_selection_corrections as correction_fixtures
from management.models import IgClient, IgCommerceSelectionSession, IgCommerceSelectionTransition, IgCustomerTurn, IgTurnMessage
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import parse_structured_response
from management.services.ig_response_guard import ProviderResponseGuard, build_source_preference_fallback
from management.services.ig_response_plan import capture_response_plan
from management.services.ig_turn_revisions import create_collecting_revision, claim_revision_preparation, seal_revision


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class AuditedSizeResponsePlanTests(TestCase):
    setUp = correction_fixtures.SizeCorrectionTests.setUp
    message = correction_fixtures.SizeCorrectionTests.message
    capture = correction_fixtures.SizeCorrectionTests.capture
    context = correction_fixtures.SizeCorrectionTests.context
    request = correction_fixtures.SizeCorrectionTests.request
    save = correction_fixtures.SizeCorrectionTests.save

    def seal(self, source=None):
        source = source or self.source
        turn = IgCustomerTurn.objects.create(client=self.row, primary_source_message=source,
            window_started_at=self.now, window_deadline=self.now)
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        created = create_collecting_revision(turn, [source], bypass_quiet=True, now=self.now)
        self.assertIsNotNone(created.revision, created.reason)
        claim = claim_revision_preparation(created.revision.pk, now=self.now)
        self.assertTrue(claim.token, claim.reason)
        sealed = seal_revision(created.revision.pk, claim.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        return sealed.revision

    def parsed(self, text, controls=()):
        payload = {"reply_text": text, "controls": list(controls)}
        response = parse_structured_response(payload)
        self.assertTrue(response.valid, response.error)
        return payload, response

    def validate(self, plan, text, *, controls=(), catalog_sizes=()):
        payload, response = self.parsed(text, controls)
        guard = ProviderResponseGuard(context_factory=lambda _control, _reply: plan.truth_context(
            ReplyTruthContext(allowed_sizes=tuple(catalog_sizes))))
        decision = guard.validate(payload)
        return decision, plan.validate(response), response

    def audited(self, *, sku=False, clear=False):
        if sku:
            self.select_product()
        revision = self.seal()
        self.save(operation="clear" if clear else "set", value=None if clear else "L")
        self.row.refresh_from_db()
        return revision, capture_response_plan(self.row, revision=revision)

    def select_product(self):
        from storefront.models import Category, Product, ProductStatus
        from productcolors.models import Color, ProductColorVariant
        from management.services.ig_commerce_source_identity import resolve_source_product_request
        category = Category.objects.create(name="Футболки", slug="audited-response-tshirts")
        product = Product.objects.create(category=category, title="Audited response tshirt", slug="audited-response-tshirt",
            price=790, status=ProductStatus.PUBLISHED)
        color = Color.objects.create(name="Чорний", primary_hex="#101010")
        ProductColorVariant.objects.create(product=product, color=color, stock=0)
        source = self.message("https://twocomms.shop/product/audited-response-tshirt/ розмір M")
        decision = apply_turn(self.row, source, resolve_source_product_request(self.row, source, parse_turn(source.text)), reply_payload={})
        self.assertTrue(decision.accepted)
        self.row.refresh_from_db()
        self.assertEqual((self.row.current_product_id, self.row.current_size), (product.pk, "M"))
        self.source = source
        return product

    def test_real_capture_retains_audit_and_original_customer_m(self):
        revision, plan = self.audited()
        self.assertEqual(plan.choices["size"], "L")
        proof = plan.evidence["size"]
        self.assertEqual(proof["authority"], "audited_correction")
        self.assertEqual(proof["correction"]["receipt"]["before"], "M")
        self.assertEqual(proof["source_message_id"], self.source.pk)
        self.assertEqual(revision.bundle_snapshot["sources"][0]["text"], "Хочу футболку розмір M")
        self.source.refresh_from_db()
        self.assertEqual(self.source.text, "Хочу футболку розмір M")
        truth = plan.truth_context(ReplyTruthContext())
        self.assertEqual(truth.source_chosen_sizes, ())
        self.assertEqual(truth.audited_chosen_sizes, ("L",))
        self.assertFalse(plan.authority["audited_size_configuration_matches"])
        self.assertIn("audited manager correction", plan.prompt_guidance())

    def test_literal_customer_claims_denied_even_with_independent_catalog_l(self):
        _, plan = self.audited()
        for text in ("Ви обрали розмір L.", "Вы выбрали размер L.", "You selected size L.",
                "You have chosen size L.", "Ви написали розмір L.", "Вы попросили размер L.",
                "Ви уточнили розмір L.", "Ви обрали розмір «L».", 'You selected size "L".'):
            with self.subTest(text=text):
                _, reason, _ = self.validate(plan, text, catalog_sizes=("L",))
                self.assertEqual(reason, "response_plan_audited_choice_misattributed")

    def test_neutral_corrected_l_is_valid_and_covers_size_without_stock_money_or_order(self):
        revision, plan = self.audited()
        for text in ("Уточнений розмір — L.", "Уточнённый размер — L.", "The corrected size requirement is L."):
            with self.subTest(text=text):
                decision, reason, response = self.validate(plan, text)
                self.assertTrue(decision.valid, decision.reason_codes)
                self.assertEqual(reason, "")
                self.assertIn(f"{self.source.pk}:size", plan.coverage(response)["covered"])
        for text in ("Уточнений розмір — M.", "The corrected size requirement is M.",
                "Уточнений розмір L є в наявності.", "Corrected size L is available.",
                "Уточнений розмір L. Оплату підтверджено.", "Уточнений розмір L. Замовлення оформлено.",
                "Уточнений розмір L. Ціна 500 грн."):
            with self.subTest(text=text):
                decision, _, _ = self.validate(plan, text)
                self.assertFalse(decision.valid, text)
        self.assertEqual(revision.bundle_snapshot["sources"][0]["text"], self.source.text)

    def test_real_fallback_is_neutral_in_each_language_and_does_zero_provider_work(self):
        revision, _ = self.audited()
        for language, expected in (("uk", "Уточнений розмір — L."), ("ru", "Уточнённый размер — L."),
                ("en", "The corrected size requirement is L.")):
            with self.subTest(language=language):
                self.row.language = language
                with patch("management.services.call_ai_analysis.gemini_generate_text") as generate, patch(
                        "management.services.instagram_bot._provider_http") as http:
                    response, proof = build_source_preference_fallback(self.row, revision=revision)
                generate.assert_not_called()
                http.assert_not_called()
                self.assertIsNotNone(response, proof)
                self.assertIn(expected, response.reply_text)
                self.assertFalse(any(text in response.reply_text for text in ("Ви обрали розмір L", "Вы выбрали размер L", "You selected size L")))
                self.assertEqual(response.control, {})
                self.assertEqual(proof["response_plan"]["evidence"]["size"]["authority"], "audited_correction")
                self.assertIn(f"{self.source.pk}:size", proof["coverage"]["covered"])
        self.source.refresh_from_db()
        self.assertIn("розмір M", self.source.text)

    def test_real_catalog_legacy_m_controls_and_checkout_denied_after_audited_l(self):
        _, plan = self.audited(sku=True)
        self.assertEqual(self.row.current_size, "M")
        self.assertEqual(plan.choices["size"], "L")
        self.assertFalse(plan.authority["audited_size_configuration_matches"])
        _, wrong_control, _ = self.validate(plan, "Уточнений розмір — L.", controls=({"kind": "size", "value": "M"},))
        self.assertEqual(wrong_control, "response_plan_audited_size_conflict")
        for controls in (({"kind": "paylink", "value": "full"},),
                ({"kind": "size", "value": "L"}, {"kind": "paylink", "value": "full"})):
            _, checkout, _ = self.validate(plan, "Уточнений розмір — L.", controls=controls)
            self.assertEqual(checkout, "response_plan_audited_configuration_unready")
        # Matching ordinary configuration input may be separately validated;
        # it does not claim that a payment/order effect already occurred.
        _, allowed_control, _ = self.validate(plan, "Уточнений розмір — L.", controls=({"kind": "size", "value": "L"},))
        self.assertEqual(allowed_control, "")

    def test_matching_legacy_text_without_sku_never_proves_checkout_configuration(self):
        revision, _ = self.audited()
        IgClient.objects.filter(pk=self.row.pk).update(current_size="L")
        self.row.refresh_from_db()
        self.assertIsNone(self.row.current_product_id)
        plan = capture_response_plan(self.row, revision=revision)
        self.assertFalse(plan.authority["audited_size_configuration_matches"])
        _, reason, _ = self.validate(plan, "Уточнений розмір — L.", controls=({"kind": "paylink", "value": "full"},))
        self.assertEqual(reason, "response_plan_audited_configuration_unready")

    def test_real_clear_cannot_revive_legacy_m_or_grant_checkout(self):
        revision, plan = self.audited(sku=True, clear=True)
        self.assertEqual(self.row.current_size, "M")
        self.assertNotIn("size", plan.choices)
        self.assertEqual(plan.evidence["size"]["correction"]["receipt"]["operation"], "clear")
        self.assertEqual(plan.truth_context(ReplyTruthContext()).source_chosen_sizes, ())
        self.assertEqual(plan.truth_context(ReplyTruthContext()).audited_chosen_sizes, ())
        self.assertEqual(plan.next_selector, "size")
        _, control, _ = self.validate(plan, "Який розмір ви обираєте?", controls=({"kind": "size", "value": "M"},))
        self.assertEqual(control, "response_plan_audited_size_conflict")
        _, checkout, _ = self.validate(plan, "Який розмір ви обираєте?", controls=({"kind": "paylink", "value": "full"},))
        self.assertEqual(checkout, "response_plan_audited_configuration_unready")
        response, proof = build_source_preference_fallback(self.row, revision=revision)
        if response is not None:
            self.assertNotIn("M", response.reply_text)
            self.assertNotIn("size", response.control)
        self.assertNotIn("M", str((proof.get("response_plan") or {}).get("choices", {})))

    def test_customer_xl_supersedes_manager_l_with_customer_authority(self):
        _, old_plan = self.audited()
        source = self.message("Насправді розмір XL")
        apply_turn(self.row, source, parse_turn(source.text), reply_payload={})
        self.row.refresh_from_db()
        plan = capture_response_plan(self.row)
        self.assertEqual(plan.choices["size"], "XL")
        self.assertFalse(plan._audited_size())
        self.assertNotEqual(plan.evidence["size"].get("authority"), "audited_correction")
        decision, reason, _ = self.validate(plan, "Ви обрали розмір XL.")
        self.assertTrue(decision.valid, decision.reason_codes)
        self.assertEqual(reason, "")
        self.assertEqual(plan.truth_context(ReplyTruthContext()).source_chosen_sizes, ("XL",))
        self.assertEqual(plan.truth_context(ReplyTruthContext()).audited_chosen_sizes, ())
        self.assertEqual(old_plan.choices["size"], "L")

    def test_original_sealed_plan_and_binding_cannot_authorize_after_correction(self):
        from management.services.ig_revision_authority import (CLAIM_SOURCE_PREFERENCES,
            build_revision_authority_bindings, check_fact_bindings)
        revision = self.seal()
        snapshot, digest = deepcopy(revision.bundle_snapshot), revision.snapshot_digest
        original = capture_response_plan(self.row, revision=revision)
        bindings = build_revision_authority_bindings(self.row, claims=(CLAIM_SOURCE_PREFERENCES,))
        self.assertTrue(bindings.ready, bindings.reasons)
        self.assertTrue(check_fact_bindings(bindings.fact_bindings, revision=revision, client=self.row))
        self.save()
        current = capture_response_plan(self.row, revision=revision)
        self.assertNotEqual(original.digest, current.digest)
        self.assertFalse(check_fact_bindings(bindings.fact_bindings, revision=revision, client=self.row))
        revision.refresh_from_db()
        self.assertEqual((revision.bundle_snapshot, revision.snapshot_digest), (snapshot, digest))
        self.assertEqual(original.choices["size"], "M")
        self.assertEqual(current.choices["size"], "L")

    def test_quoted_negative_or_question_correction_does_not_cover_size_obligation(self):
        _, plan = self.audited()
        for text in ('Менеджер написав «Уточнений розмір L».', "Уточнений розмір — не L.",
                "Уточнений розмір L?", "The corrected size requirement is not L."):
            with self.subTest(text=text):
                _, response = self.parsed(text)
                self.assertIn(f"{self.source.pk}:size", plan.coverage(response)["remaining"])

    def test_independently_valid_catalog_m_cannot_override_audited_requirement_l(self):
        _, plan = self.audited()
        catalog = ("M", "L")
        self.assertEqual(plan.truth_context(ReplyTruthContext(allowed_sizes=catalog)).allowed_sizes, catalog)
        for text in ("The corrected size requirement is M.", "Уточнений розмір — M.", "Уточнённый размер — M."):
            with self.subTest(text=text):
                _, reason, response = self.validate(plan, text, catalog_sizes=catalog)
                self.assertEqual(reason, "response_plan_audited_size_conflict")
                self.assertIn(f"{self.source.pk}:size", plan.coverage(response)["remaining"])
        _, reason, _ = self.validate(plan, "The corrected size requirement is L.", catalog_sizes=catalog)
        self.assertEqual(reason, "")

    def assert_possessive_claims_denied(self, plan, size):
        for text in (f"Your choice is size {size}.", f"Ваш вибір — розмір {size}.",
                     f"Ваш выбор — размер {size}.", f'Your preference is size "{size}".',
                     f"Your choice is corrected size {size}.",
                     f"You selected size {size} and no changes are needed.", f"Your choice is {size}."):
            with self.subTest(text=text):
                _, reason, _ = self.validate(plan, text, catalog_sizes=("M", "L"))
                expected = "response_plan_audited_size_conflict" if not plan.choices.get("size") and "corrected" in text else "response_plan_audited_choice_misattributed"
                self.assertEqual(reason, expected)

    def test_possessive_customer_choice_cannot_attribute_manager_correction(self):
        _, plan = self.audited()
        self.assert_possessive_claims_denied(plan, "L")

    def test_possessive_customer_choice_cannot_revive_cleared_requirement(self):
        _, plan = self.audited(clear=True)
        self.assert_possessive_claims_denied(plan, "M")

    def test_clear_denies_current_customer_m_but_preserves_explicit_original_history(self):
        revision, plan = self.audited(clear=True)
        for text in ("You selected size M.", "Ви обрали розмір M.", "Вы выбрали размер M.",
                     'You selected size "M".'):
            with self.subTest(text=text):
                _, reason, _ = self.validate(plan, text, catalog_sizes=("M", "L"))
                self.assertIn(reason, ("response_plan_audited_size_conflict", "response_plan_audited_choice_misattributed"))
        for text in (
                "Earlier you selected size M. That earlier requirement is now cleared. Which size do you choose now?",
                "Раніше ви обрали розмір M. Це попереднє побажання вже скасовано. Який розмір ви обираєте тепер?"):
            with self.subTest(text=text):
                decision, reason, response = self.validate(plan, text, catalog_sizes=("M", "L"))
                self.assertTrue(decision.valid, decision.reason_codes)
                self.assertEqual(reason, "")
                self.assertNotIn(f"{self.source.pk}:size", plan.coverage(response)["covered"])
                self.assertIn(f"{self.source.pk}:size", plan.coverage(response)["remaining"])
        self.assertNotIn("size", plan.choices)
        self.assertEqual(plan.evidence["size"]["correction"]["receipt"]["before"], "M")
        self.assertEqual(revision.bundle_snapshot["sources"][0]["text"], "Хочу футболку розмір M")
        self.source.refresh_from_db()
        self.assertEqual(self.source.text, "Хочу футболку розмір M")

    @override_settings(ROOT_URLCONF=correction_fixtures.__name__, SECURE_SSL_REDIRECT=False)
    def test_duplicate_typed_json_fields_return_finite_400_without_writes(self):
        self.client.force_login(self.actor)
        request = self.request()
        body = {key: str(value) if key == "operation_id" else value for key, value in request.items()
                if key not in {"actor", "now"}}
        body["field"] = "size"
        original_client = IgClient.objects.values().get(pk=self.row.pk)
        original_source = type(self.source).objects.values().get(pk=self.source.pk)
        count = IgCommerceSelectionTransition.objects.count()
        revision = IgCommerceSelectionSession.objects.get(pk=self.session.pk).revision
        for duplicate, value in (("value", "XL"), ("operation_id", body["operation_id"]),
                                 ("expected_context_digest", body["expected_context_digest"])):
            wire = json.dumps(body)[:-1] + "," + json.dumps(duplicate) + ":" + json.dumps(value) + "}"
            with self.subTest(duplicate=duplicate), CaptureQueriesContext(connection) as queries:
                response = self.client.post(f"/bot/api/clients/{self.row.pk}/state/size/", wire,
                    content_type="application/json")
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.json(), {"success": False, "code": "correction_request_invalid", "retryable": False})
            self.assertFalse(any(query["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}
                                 for query in queries), queries.captured_queries)
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), count)
        self.assertEqual(IgCommerceSelectionSession.objects.get(pk=self.session.pk).revision, revision)
        self.assertEqual(IgClient.objects.values().get(pk=self.row.pk), original_client)
        self.assertEqual(type(self.source).objects.values().get(pk=self.source.pk), original_source)
