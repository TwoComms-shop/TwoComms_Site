"""Owner-scoped production policy and native hard-cutoff checkout authority."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from management.services.approved_public_facts import approved_public_facts
from management.services.ig_reply_authority import (
    apply_fulfillment_timing_context, fulfillment_deadline_authorities,
    fulfillment_timing_authorities, build_reply_truth_context,
)
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.tests_ig_fulfillment_timing import projection
from management.tests_ig_restock_subscriptions import _RestockFixture


class MadeToOrderPolicyAuthorityTests(SimpleTestCase):
    def policy(self, language="en"):
        fact = next(row for row in approved_public_facts(language) if row.key == "dispatch")
        proof = (language, fact.version, hashlib.sha256(fact.public_text.encode()).hexdigest())
        return fact, ReplyTruthContext(approved_fulfillment_policy=(proof,))

    def test_only_exact_current_reviewed_copy_grants_general_policy(self):
        for language in ("uk", "ru", "en"):
            fact, context = self.policy(language)
            self.assertTrue(validate_reply_truth(fact.public_text, context=context).valid)
            proof = context.approved_fulfillment_policy[0]
            for forged in ((), ((language, proof[1], "f" * 64),), ((language, "old", proof[2]),),
                    (("xx", proof[1], proof[2]),), ({"text": fact.public_text},)):
                with self.subTest(language=language, forged=forged):
                    result = validate_reply_truth(fact.public_text,
                        context=replace(context, approved_fulfillment_policy=forged))
                    self.assertFalse(result.valid, result)

    def test_policy_absence_and_review_metadata_change_revoke_captured_proof(self):
        fact, context = self.policy()
        original = approved_public_facts
        for changed in (None, replace(fact, review_status="draft"),
                replace(fact, source_ref="customer:text"), replace(fact, scope="whole_catalog")):
            def facts(language):
                return tuple(changed if row.key == "dispatch" else row for row in original(language)
                    if row.key != "dispatch" or changed is not None)
            with self.subTest(changed=changed), patch(
                    "management.services.approved_public_facts.approved_public_facts", side_effect=facts):
                self.assertFalse(validate_reply_truth(fact.public_text, context=context).valid)

    def test_builder_does_not_restore_generic_window_when_reviewed_policy_is_absent(self):
        original = approved_public_facts
        def without_dispatch(language):
            return tuple(row for row in original(language) if row.key != "dispatch")
        with (
            patch("management.services.ig_reply_authority._current_episode", return_value=None),
            patch("management.services.ig_reply_authority._current_configuration_pricing", return_value=(None, False)),
            patch("management.services.ig_reply_authority._server_urls", return_value=((), (), ())),
            patch("management.services.ig_reply_authority._conversation_facts", return_value=((), ())),
            patch("management.services.approved_public_facts.approved_public_facts", side_effect=without_dispatch),
        ):
            context = build_reply_truth_context(SimpleNamespace(pk=1))
        self.assertFalse(context.approved_fulfillment_policy)
        self.assertIsNone(context.explicitly_qualified_standard_dispatch_days)
        self.assertIn("unverified_timing", validate_reply_truth(
            "Usually preparation takes 1–3 days after payment.", context=context).reasons)

    def test_policy_cannot_supply_customer_payment_stock_order_or_speed(self):
        fact, context = self.policy()
        for invention in (
            "Your payment is confirmed.", "Your order has been created.",
            "Your printed hoodie is in stock.", "Your order will arrive in 1–3 days.",
            "We guarantee delivery before 10 tomorrow.",
            "We dispatch in 1–3 days after payment.",
        ):
            with self.subTest(invention=invention):
                self.assertFalse(validate_reply_truth(fact.public_text + " " + invention, context=context).valid)

    def test_conditional_preparation_has_no_fabricated_calendar_anchor(self):
        review = projection(windows=((1, 3),))
        review["decision"]["lines"][0]["preparation"] = {
            "min_days": 1, "max_days": 3, "unit": "calendar", "anchor": "after_payment_confirmation",
        }
        status, facts = fulfillment_timing_authorities(review)
        self.assertEqual(status, "reviewed")
        self.assertTrue(facts)
        self.assertTrue(all(row.anchor == "after_payment_confirmation" and not row.anchor_at for row in facts))
        context = ReplyTruthContext(fulfillment_review_status=status, timing_authorities=facts)
        self.assertTrue(validate_reply_truth(
            "Preparation takes 1–3 days after payment confirmation.", context=context).valid)
        for text in (
            "Preparation takes 1–3 days.", "Preparation starts on 21 October.",
            "We dispatch in 1–3 days after payment confirmation.",
            "Your order will arrive in 1–3 days after payment confirmation.",
            "We can meet your deadline before 10 tomorrow.",
        ):
            with self.subTest(text=text):
                self.assertIn("unverified_timing", validate_reply_truth(text, context=context).reasons)
        self.assertTrue(validate_reply_truth("Preparation takes 1–3 days.",
            context=replace(context, payment_confirmed=True)).valid)

    def test_legacy_generic_window_can_explain_preparation_but_never_dispatch(self):
        context = ReplyTruthContext(explicitly_qualified_standard_dispatch_days=(1, 3))
        self.assertTrue(validate_reply_truth(
            "Usually preparation takes 1–3 days after payment confirmation.", context=context).valid)
        for text in ("Usually we dispatch in 1–3 days after payment.",
                "Usually your order arrives in 1–3 days after payment."):
            self.assertIn("unverified_timing", validate_reply_truth(text, context=context).reasons)

    def test_forged_anchor_does_not_grant_timing_and_mixed_anchors_do_not_merge(self):
        review = projection(windows=((1, 3),))
        timing = review["decision"]["lines"][0]["preparation"]
        for anchor in ("tomorrow", "after_payment_confirmation"):
            timing["anchor"] = anchor  # retaining anchor_at is an invalid mixed clock
            self.assertFalse(fulfillment_timing_authorities(review)[1])
        review = projection(windows=((1, 3), (1, 3)))
        review["decision"]["lines"][1]["preparation"] = {
            "min_days": 1, "max_days": 3, "unit": "calendar", "anchor": "after_payment_confirmation",
        }
        self.assertTrue(all(row.line_id for row in fulfillment_timing_authorities(review)[1]))


class CanonicalSizeTruthTests(SimpleTestCase):
    def test_catalog_source_and_audited_size_aliases_share_one_canonical_identity(self):
        for selected, claimed in (("XXL", "2XL"), ("2XL", "XXL"), ("XXXL", "3XL"), ("3XL", "XXXL")):
            for text, values in (
                (f"Selected size: {claimed}.", {"allowed_sizes": (selected,)}),
                (f"You chose size {claimed}.", {"source_chosen_sizes": (selected,)}),
                (f"Your corrected size requirement is {claimed}.", {"audited_chosen_sizes": (selected,)}),
            ):
                with self.subTest(selected=selected, text=text):
                    result = validate_reply_truth(text, context=ReplyTruthContext(**values))
                    self.assertTrue(result.valid, result.reasons)

    def test_aliases_do_not_grant_wrong_size_unknown_numeric_or_stock_truth(self):
        contexts = (
            ReplyTruthContext(allowed_sizes=("XXL",), fulfillment_review_status="pending"),
            ReplyTruthContext(source_chosen_sizes=("2XL",)),
            ReplyTruthContext(audited_chosen_sizes=("XXL",)),
        )
        for context in contexts:
            for text in ("Selected size: XL.", "You chose size 3XL.",
                    "Your corrected size requirement is 17.", "Size 2XL is available."):
                with self.subTest(context=context, text=text):
                    self.assertFalse(validate_reply_truth(text, context=context).valid)
        for allowed in ("17", "unknown", "", None):
            self.assertIn("configuration_mismatch", validate_reply_truth("Selected size: 2XL.",
                context=ReplyTruthContext(allowed_sizes=(allowed,))).reasons)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeManuDeadlineAuthorityTests(_RestockFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(
            username="native-deadline-manager", email="manager@example.test", password="test")
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])

    def case(self):
        from management.models import IgFulfillmentReviewRequest
        from management.services import ig_fulfillment_reviews as review
        source = self.source("I have to leave tomorrow at 10 am. Can I receive this before I leave?")
        revision, _ = self.revision(source)
        result = review.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(result["ready"], result)
        request = IgFulfillmentReviewRequest.objects.get(pk=result["request_id"])
        return request, revision

    def decide(self, request):
        from management.services import ig_fulfillment_reviews as review
        requirement = next(row for row in request.envelope["customer_requirements"] if row.get("deadline_hard"))
        result = review.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), now=self.now,
            decisions_by_line={"lines": [{"line_id": row["line_id"], "availability": "available",
                "checked_quantity": 1, "supply_basis": "warehouse_checked",
                "preparation": {"min_days": 1, "max_days": 3, "unit": "calendar",
                    "anchor": "after_payment_confirmation"}} for row in request.envelope["lines"]],
                "deadline_review": {"requirement_digest": requirement["requirement_digest"],
                    "outcome": "impossible", "line_ids": [row["line_id"] for row in request.envelope["lines"]],
                    "options": ["later_domestic_contact", "later_domestic_destination", "international_review_required"]}})
        self.assertTrue(result["ready"], result)
        return result

    def capture(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        return capture_current_selection_lines(self.client_row.pk, now=self.now)

    def test_native_signed_decision_and_frozen_context_expose_only_exact_negative_cutoff(self):
        from management.services import ig_fulfillment_reviews as review
        request, _revision = self.case()
        self.decide(request)
        projected = review.fulfillment_review_projection(self.client_row.pk, now=self.now,
            current_source_cart=self.capture())
        self.assertEqual(projected["status"], "reviewed", projected)
        facts = fulfillment_deadline_authorities(projected)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].outcome, "impossible")
        self.assertEqual(facts[0].source_message_id, request.source_id)
        self.assertTrue(facts[0].deadline_at.endswith("10:00:00+03:00"))
        frame = {"status": "captured", "slots": {"fulfillment.review": {
            "status": "confirmed", "value": projected}}}
        context = apply_fulfillment_timing_context(ReplyTruthContext(), frame)
        self.assertEqual(context.fulfillment_deadline_authorities, facts)
        self.assertFalse(validate_reply_truth("We can meet your deadline before 10 tomorrow.", context=context).valid)
        self.assertTrue(validate_reply_truth("We cannot meet your deadline before 10 tomorrow.", context=context).valid)
        for alteration in ("digest", "clock", "line", "outcome", "slot"):
            frozen = deepcopy(frame)
            payload = frozen["slots"]["fulfillment.review"]["value"]
            if alteration == "digest":
                payload["decision"]["deadline_review"]["requirement_digest"] = "f" * 64
            elif alteration == "clock":
                payload["current_request"]["customer_requirements"][0]["deadline_at"] = "2026-10-13T10:00:00"
            elif alteration == "line":
                payload["decision"]["deadline_review"]["line_ids"] = ["other-line"]
            elif alteration == "outcome":
                payload["decision"]["deadline_review"]["outcome"] = "feasible"
            else:
                frozen["slots"]["fulfillment.review"]["status"] = "proposed"
            with self.subTest(alteration=alteration):
                self.assertFalse(apply_fulfillment_timing_context(
                    ReplyTruthContext(), frozen).fulfillment_deadline_authorities)

    def test_ordinary_source_cart_cannot_issue_checkout_before_later_receipt_consent(self):
        from management.services.ig_revision_checkout import checkout_authority_control
        request, _revision = self.case()
        self.decide(request)
        with patch("management.services.instagram_bot._provider_http") as provider:
            result, reasons = checkout_authority_control(self.client_row, {"paylink": "full"},
                source_cart_capture=self.capture())
        self.assertEqual(result, {})
        self.assertEqual(reasons, ("checkout_hard_deadline_later_receipt_consent_required",))
        provider.assert_not_called()

    def test_model_consent_flags_cannot_replace_owned_customer_receiving_source(self):
        from management.services.ig_revision_checkout import checkout_authority_control
        request, _revision = self.case()
        self.decide(request)
        _result, reasons = checkout_authority_control(self.client_row, {
            "paylink": "full", "later_receipt_consent": True,
            "deadline_review": {"outcome": "feasible"}, "payment_confirmed": True,
        }, source_cart_capture=self.capture())
        self.assertEqual(reasons, ("checkout_hard_deadline_later_receipt_consent_required",))

    def test_owned_later_acceptance_clears_only_cutoff_not_payment_or_configuration_guards(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_checkout import authorize_revision_checkout, checkout_authority_control
        request, _revision = self.case()
        self.decide(request)
        later = self.now + timedelta(minutes=1)
        source = self.source("Please send it later to my boyfriend in Kharkiv", at=later)
        self.revision(source, now=later)
        with patch("django.utils.timezone.now", return_value=later):
            captured = capture_current_selection_lines(self.client_row.pk, now=later)
            normalized, reasons = checkout_authority_control(self.client_row, {"paylink": "full"},
                source_cart_capture=captured)
            self.assertFalse(reasons, reasons)
            self.assertEqual(normalized["items"][0]["product_id"], self.product.pk)
            _result, reasons = authorize_revision_checkout(self.client_row,
                {"paylink": "prepay", "payment": 200}, source.text, source_cart_capture=captured)
            self.assertEqual(reasons, ("checkout_payment_source_missing",))
            tampered = deepcopy(captured)
            tampered["lines"][0]["fields"]["size"]["value"] = "L"
            _result, reasons = checkout_authority_control(self.client_row, {"paylink": "full"},
                source_cart_capture=tampered)
            self.assertTrue(set(reasons).intersection({"checkout_cart_source_changed", "checkout_cart_capture_invalid"}), reasons)

    def test_legacy_no_reference_binding_rechecks_cutoff_before_http(self):
        from management.services.ig_revision_authority import (
            CLAIM_CATALOG_CONFIGURATION, build_revision_authority_bindings, check_fact_bindings,
        )
        self.client_row.current_product = self.product
        self.client_row.current_size = "M"
        self.client_row.current_qty = 1
        self.client_row.save(update_fields=["current_product", "current_size", "current_qty"])
        from management.services.instagram_bot import _persist_checkout_selection
        _persist_checkout_selection(self.client_row, self.product.pk, {"color_variant_id": self.variant.pk,
            "fit_option_code": "classic", "option_values": {"fit": "classic"}})
        control = {"items": [{"product_id": self.product.pk, "color_variant_id": self.variant.pk,
            "qty": 1, "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}}]}
        before = build_revision_authority_bindings(self.client_row,
            claims=(CLAIM_CATALOG_CONFIGURATION,), control=control, settings_obj=self.settings)
        self.assertTrue(before.ready, before.reasons)
        request, revision = self.case()
        self.decide(request)
        with patch("management.services.instagram_bot._provider_http") as provider:
            self.assertFalse(check_fact_bindings(before.fact_bindings,
                revision=revision, client=self.client_row, settings_obj=self.settings))
        provider.assert_not_called()
