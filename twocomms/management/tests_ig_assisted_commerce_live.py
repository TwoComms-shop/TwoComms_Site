"""Native own sources -> signed manager offer -> existing Gemini/outbox gates."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import json

from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection

from management.tests_ig_assisted_commerce import AssistedCommerceFixture
from management.tests_ig_revision_live import RevisionLiveTests
from management.models import IgCheckoutAccessToken, IgCheckoutProposal, IgFollowUpTask
from management.services.ig_assisted_commerce_context import capture_assisted_context, CLAIM, ACTION
from management.services.ig_response_control import ResponseControl, ValidatedResponse
from management.services.ig_response_plan import capture_response_plan
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_revision_live import RevisionGenerationBoundary, _revision_response_purpose_reason
from management.services.ig_revision_authority import check_fact_bindings
from management.services.ig_revision_intents import manager_case_reason
from management.services.ig_product_inquiries import product_inquiries_for_revision


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.shop", IG_REVISION_OPTIONAL_HOLDING_ENABLED=False)
class AssistedCommerceLiveTests(AssistedCommerceFixture, TransactionTestCase):
    reset_sequences = True
    _generate = RevisionLiveTests._generate
    _execute = RevisionLiveTests._execute

    def setUp(self):
        super().setUp()
        presence = patch("management.services.ig_presence.start_presence", return_value=None)
        presence.start()
        self.addCleanup(presence.stop)
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = "observed"
        self.provider_failure = False

    def approved_purchase(self):
        request, draft, decision, _ = self.approved_case()
        self.source = self.canonical("Беру. Оформіть замовлення.")
        self.revision, self.token = self.revision(self.source)
        self.parsed = {"reply_text": "Фіолетове худі в наявності, розмір L. Ціна 1500 грн. Перевірте деталі на сторінці оформлення.",
                       "controls": [{"kind": "paylink", "value": "full"}]}
        return request, draft, decision

    def boundary(self, revision, token):
        return RevisionGenerationBoundary(revision, token, self.settings, self.binding)

    def test_native_draft_is_not_truth_but_exact_approved_questions_resolve(self):
        request, revision = self.request_case()
        plan = capture_response_plan(self.client_row, revision=revision)
        original = product_inquiries_for_revision(revision, response_plan=plan)
        self.assertEqual(original["status"], "manager_required")
        draft, _ = self.record(request, items=self.items_for(request))
        frame, reason = capture_assisted_context(self.client_row, revision, plan, now=self.now)
        self.assertIsNone(frame)
        self.assertEqual(reason, "approved_decision_missing")
        decision, _ = self.record(request, status="approved", latest=draft)
        frame, reason = capture_assisted_context(self.client_row, revision, plan, now=self.now)
        self.assertIsNotNone(frame, reason)
        revision._assisted_commerce_context = frame
        self.assertEqual(product_inquiries_for_revision(revision, response_plan=plan)["status"], "none")
        self.assertEqual(manager_case_reason(revision), "")
        self.assertFalse(frame.can_prepare_checkout)
        self.assertEqual(frame.verified.decision_id, decision.pk)
        self.assertIn("1500.00", frame.prompt_guidance())
        self.assertIn(str(request.pk), frame.prompt_guidance())

    def test_native_offer_validates_exact_price_stock_and_no_unsupplied_timing_specs(self):
        self.approved_purchase()
        boundary = self.boundary(self.revision, self.token)
        self.assertIsNotNone(boundary.assisted_context, boundary.assisted_reason)
        good = ValidatedResponse(reply_text=self.parsed["reply_text"], controls=(ResponseControl("paylink", "full"),))
        self.assertTrue(boundary.validate_response_truth(good, ReplyTruthContext()).valid)
        for text in ("Фіолетове худі в наявності. Ціна 900 грн.",
            "Фіолетове худі відправимо за 1–3 дні.", "Фіолетове худі прибуде завтра.",
            "Фіолетове худі з бавовни, манжети металеві.", "Чорне худі в наявності, розмір M."):
            with self.subTest(text=text):
                self.assertFalse(boundary.validate_response_truth(ValidatedResponse(reply_text=text), ReplyTruthContext()).valid)
        self.assertIn(ACTION, boundary.response_authority(good).allowed_actions)
        bad = ValidatedResponse(reply_text=good.reply_text, controls=(ResponseControl("paylink", "full"), ResponseControl("size", "M")))
        self.assertFalse(boundary.response_authority(bad).ready)

    def test_raw_receipt_or_stale_source_cannot_supply_current_manager_truth(self):
        self.approved_purchase()
        boundary = self.boundary(self.revision, self.token)
        ref = boundary.assisted_context.reference
        changed = dict(ref, decision_digest="0" * 64)
        self.assertIsNone(capture_assisted_context(self.client_row, self.revision, reference=changed, now=self.now)[0])
        self.revision._assisted_commerce_context = {"verified_decision": {"approved_total": "1"}}
        self.assertNotEqual(product_inquiries_for_revision(self.revision, response_plan=boundary.response_plan).get("assisted_resolution"), ref)
        self.source.text = "Беру чорне худі замість фіолетового."
        self.source.save(update_fields=["text"])
        self.assertFalse(boundary.check().ready)
        self.assertIsNone(capture_assisted_context(self.client_row, self.revision, reference=ref, now=self.now)[0])

    def test_new_hardware_question_is_not_resolved_by_old_color_price_approval(self):
        self.approved_case()
        source = self.canonical("Який склад тканини худі та чи металеві люверси?")
        revision, token = self.revision(source)
        boundary = self.boundary(revision, token)
        self.assertEqual(boundary.product_inquiries["status"], "manager_required")
        self.assertEqual(manager_case_reason(revision), "product_information_review")
        reply = ValidatedResponse(reply_text="Манжети з бавовни, люверси металеві.")
        self.assertFalse(boundary.validate(reply, policy_manifest={"instruction_publication": {
            "id": self.publication.pk, "version": 1, "hash": self.publication.snapshot_hash}}).valid)

    def test_expiry_and_changed_manager_head_remove_current_capability(self):
        request, draft, decision, revision = self.approved_case()
        plan = capture_response_plan(self.client_row, revision=revision)
        frame, _ = capture_assisted_context(self.client_row, revision, plan, now=self.now)
        self.assertIsNotNone(frame)
        self.assertIsNone(capture_assisted_context(self.client_row, revision, reference=frame.reference,
            now=decision.expires_at + timedelta(seconds=1))[0])
        self.record(request, status="rejected", latest=decision, rejection_reason="Потрібна нова перевірка")
        self.assertIsNone(capture_assisted_context(self.client_row, revision, reference=frame.reference, now=self.now)[0])

    def test_genuine_catalogue_path_performs_no_assisted_lookup(self):
        self.variant.stock = 2
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 2
        self.rule.save(update_fields=["stock"])
        source = self.canonical(f"Хочу {self.product.title}, колір black, розмір M, крій classic")
        revision, _ = self.revision(source)
        plan = capture_response_plan(self.client_row, revision=revision)
        # Cache the ordinary classifier's existing revision.client relation;
        # this test measures the new branch, not initial plan admission reads.
        product_inquiries_for_revision(revision, response_plan=plan)
        with CaptureQueriesContext(connection) as queries:
            frame, reason = capture_assisted_context(self.client_row, revision, plan, now=self.now)
        self.assertIsNone(frame)
        self.assertEqual(reason, "assisted_context_not_applicable")
        self.assertEqual(len(queries), 0)

    def test_native_gemini_prompt_offer_checkout_and_physical_replay_share_one_proof(self):
        request, _draft, decision = self.approved_purchase()
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertGreaterEqual(http.call_count, 1)
        prompt = json.dumps(self.generation_calls[0], ensure_ascii=False)
        self.assertIn("CURRENT VERIFIED MANAGER OFFER", prompt)
        self.assertIn("1500.00", prompt)
        self.assertIn("Фіолетове худі", prompt)
        self.revision.refresh_from_db()
        saved = self.revision.generation_proposal
        self.assertEqual(saved["assisted_offer_reference"]["decision_id"], str(decision.pk))
        self.assertIn(ACTION, saved["authority"]["allowed_actions"])
        checkout = IgCheckoutProposal.objects.get(client=self.client_row)
        self.assertEqual(checkout.quoted_total, Decimal("1500.00"))
        self.assertTrue(checkout.assisted_checkout_v2)
        self.assertEqual(IgCheckoutAccessToken.objects.filter(proposal=checkout).count(), 1)
        delivered = tuple(self.revision.delivery_effects.order_by("order_index"))
        self.assertTrue(delivered)
        self.assertTrue(all(effect.state == "sent" and effect.provider_message_id for effect in delivered))
        first = delivered[0]
        self.assertEqual(first.provider_message_id, "sent-1")
        self.assertTrue(check_fact_bindings(first.fact_bindings, revision=self.revision,
            client=self.client_row, settings_obj=self.settings))
        self.assertEqual(_revision_response_purpose_reason(self.revision), "")
        self.assertFalse(IgFollowUpTask.objects.filter(client=self.client_row,
            reason="revision_case:product_configuration_review").exists())
        # Revoked/expired approval blocks a retained physical reply, even when
        # its immutable payload and hosted quote were already prepared.
        with patch("django.utils.timezone.now", return_value=decision.expires_at + timedelta(seconds=1)):
            self.assertFalse(check_fact_bindings(first.fact_bindings, revision=self.revision,
                client=self.client_row, settings_obj=self.settings))
            self.assertTrue(_revision_response_purpose_reason(self.revision))


    def test_native_spec_answer_does_not_authorize_appended_material_claims(self):
        import uuid
        from management.services import ig_assisted_commerce as assisted
        from management.services.ig_commerce_projection import capture_current_selection_lines
        source = self.canonical("Колір purple, розмір L. Чи металеві люверси?")
        revision, token = self.revision(source)
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        request, created = assisted.create_assisted_request(revision.pk, settings_id=self.settings.pk,
            operation_id=uuid.uuid4(), expected_revision_digest=revision.snapshot_digest,
            expected_full_capture_digest=cart["capture_digest"], now=self.now)
        self.assertTrue(created)
        info = [question for question in request.envelope["snapshot"]["questions"] if question["axis"] == "information"]
        self.assertEqual(len(info), 1)
        items = self.items_for(request)
        for answer in items[0]["question_answers"]:
            if answer["question_id"] == info[0]["question_id"]:
                answer["answer"] = "Люверси металеві."
        draft, _ = self.record(request, items=items)
        self.record(request, status="approved", latest=draft)
        boundary = self.boundary(revision, token)
        self.assertIsNotNone(boundary.assisted_context, boundary.assisted_reason)
        self.assertEqual(boundary.product_inquiries["status"], "none")
        authorized = ValidatedResponse(reply_text="Фіолетове худі: Люверси металеві.")
        self.assertTrue(boundary.validate_response_truth(authorized, ReplyTruthContext()).valid)
        for text in ("Люверси металеві та тканина зі 100% бавовни.",
                     "Люверси металеві, тканина зі 100% бавовни.",
                     "Люверси металеві. Тканина зі 100% бавовни.",
                     "Люверси металеві; манжети з бавовни."):
            with self.subTest(text=text):
                result = boundary.validate_response_truth(ValidatedResponse(reply_text=text), ReplyTruthContext())
                self.assertFalse(result.valid)
                self.assertIn("product_information_unverified", result.reasons)

    def test_native_spec_answer_is_bound_to_its_original_question_and_line(self):
        import uuid
        from management.models import IgCustomerTurn, IgTurnMessage
        from management.services import ig_assisted_commerce as assisted
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_turn_revisions import (create_collecting_revision,
            claim_revision_preparation, seal_revision, claim_sealed_revision)
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        catalog_product, catalog_variant = self.products[1], self.variants[1]
        catalog_variant.stock = 3
        catalog_variant.save(update_fields=["stock"])
        VariantSizeRule.objects.create(variant=catalog_variant, fit_code="classic", size="M", stock=3, is_enabled=True)
        ProductInventoryPolicy.objects.create(product=catalog_product, source="catalog_variant")
        first = self.canonical("Колір purple, розмір L. Чи металеві люверси?")
        second = self.canonical(f"добавьте {catalog_product.title}, количество 1, размер M, цвет pink, крій classic. Чи металеві люверси?")
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=first,
            window_started_at=self.now, window_deadline=self.now)
        for ordinal, source in enumerate((first, second), 1):
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=ordinal, role="user")
        revision = create_collecting_revision(turn, [first, second], now=self.now, bypass_quiet=True).revision
        self.assertIsNotNone(revision)
        preparation = claim_revision_preparation(revision.pk, now=self.now)
        sealed = seal_revision(revision.pk, preparation.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(revision.pk, now=self.now)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        revision, token = claimed.revision, claimed.token
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        request, _ = assisted.create_assisted_request(revision.pk, settings_id=self.settings.pk,
            operation_id=uuid.uuid4(), expected_revision_digest=revision.snapshot_digest,
            expected_full_capture_digest=cart["capture_digest"], now=self.now)
        info = [question for question in request.envelope["snapshot"]["questions"] if question["axis"] == "information"]
        self.assertEqual(len(info), 2)
        self.assertEqual(len({question["line_id"] for question in info}), 2)
        data = request.envelope["snapshot"]
        items = assisted._editable_items(data)
        self.assertEqual([item["item_kind"] for item in items], ["custom_apparel", "catalog_apparel"])
        questions = {question["question_id"]: question for question in data["questions"]}
        for index, item in enumerate(items):
            if item["item_kind"] == "custom_apparel":
                item.update(title="Фіолетове худі", color="Фіолетовий", size="L", unit_price="1500.00")
            item.update(availability="ready", checked_quantity=item["quantity"],
                supply_basis="warehouse_checked", source_location="Менеджер перевірив точну річ")
            for answer in item["question_answers"]:
                answer["state"] = "confirmed"
                question = questions[answer["question_id"]]
                answer["answer"] = (("Люверси металеві.", "Люверси пластикові.")[index]
                    if question["axis"] == "information" else "Підтверджено: " + str(question["requested_value"]))
        catalog_title = items[1]["title"]
        draft, _ = self.record(request, items=items)
        self.record(request, status="approved", latest=draft)
        boundary = self.boundary(revision, token)
        self.assertIsNotNone(boundary.assisted_context, boundary.assisted_reason)
        good = f"Фіолетове худі: Люверси металеві. {catalog_title}: Люверси пластикові."
        self.assertTrue(boundary.validate_response_truth(ValidatedResponse(reply_text=good), ReplyTruthContext()).valid)
        for text in (f"{catalog_title}: Люверси металеві.",
                     "Фіолетове худі: Люверси пластикові.", "Люверси металеві.",
                     "Фіолетове худі: Люверси металеві та тканина зі 100% бавовни."):
            with self.subTest(text=text):
                result = boundary.validate_response_truth(ValidatedResponse(reply_text=text), ReplyTruthContext())
                self.assertFalse(result.valid)
                self.assertIn("product_information_unverified", result.reasons)
