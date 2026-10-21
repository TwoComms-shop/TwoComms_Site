"""Source-bound departure decisions preserve selected goods and receiving choice."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from management.services.ig_fulfillment_requirements import extract_requirements, requirement_digest
from management.services.ig_fulfillment_checkout import (deadline_checkout_reason, deadline_later_acceptance,
    explicit_later_receipt_option, fulfillment_deadline_checkout_reason)
from management.services.ig_fulfillment_delivery import render_manager_update
from management.services import ig_fulfillment_reviews as reviews
from management.tests_ig_restock_subscriptions import _RestockFixture


class DepartureRequirementTests(SimpleTestCase):
    now = datetime(2026, 10, 21, 15, 5, tzinfo=timezone.utc)

    def requirement(self, text=None):
        return extract_requirements(text or "I want this shirt for my boyfriend. I have to leave tomorrow at 10 AM",
            provider_timestamp=self.now, now=self.now + timedelta(hours=23))

    def test_actual_english_departure_keeps_hour_and_provider_day(self):
        hint = self.requirement("Hello! I am in Kharkiv and I would like to buy a specific shirt for my boyfriend. Do you have a store? I have to leave tomorrow at 10 AM😭")
        self.assertEqual(hint["deadline_at"], "2026-10-22T10:00:00+03:00")
        self.assertEqual(hint["deadline_kind"], "departure")
        self.assertIs(hint["deadline_hard"], True)
        self.assertEqual(hint["recipient_hint"], "boyfriend")
        self.assertTrue(hint["request_detected"])

    def test_clock_ambiguity_missing_provider_and_hypothetical_do_not_guess(self):
        self.assertNotIn("deadline_at", self.requirement("I leave tomorrow at 10"))
        hint = extract_requirements("I leave tomorrow at 10 AM", now=self.now)
        self.assertNotIn("deadline_at", hint)
        self.assertNotIn("deadline_hard", self.requirement("If I leave tomorrow at 10 AM"))
        self.assertNotIn("deadline_hard", self.requirement('He said "I leave tomorrow at 10 AM"'))

    def test_digest_binds_source_and_full_requirement(self):
        hint = {**self.requirement(), "source_id": 14}
        digest = requirement_digest(hint)
        hint["requirement_digest"] = digest
        self.assertEqual(requirement_digest(hint), digest)
        self.assertNotEqual(requirement_digest({**hint, "source_id": 15}), digest)
        self.assertNotEqual(requirement_digest({**hint, "deadline_at": "2026-10-22T11:00:00+03:00"}), digest)

    def test_explicit_later_receipt_requires_customer_instruction_and_domestic_option(self):
        for text in ("okay", "Later is okay", "Can my boyfriend get it later?", "If my boyfriend can receive later in Kharkiv",
            'He said "Send it later to my boyfriend in Kharkiv"', "Send it later to my boyfriend abroad"):
            self.assertIsNone(explicit_later_receipt_option(text), text)
        self.assertEqual(explicit_later_receipt_option("Please send it later to my boyfriend in Kharkiv"), "later_domestic_contact")
        self.assertEqual(explicit_later_receipt_option("I agree to receive it later via Nova Poshta"), "later_domestic_destination")

    def test_decision_schema_binds_requirement_and_never_accepts_feasible(self):
        hint = {**self.requirement(), "source_id": 14}
        request = SimpleNamespace(envelope={"lines": [{"line_id": "selected"}], "customer_requirements": [hint]})
        outcome = {"requirement_digest": requirement_digest(hint), "outcome": "impossible", "line_ids": ["selected"],
            "options": ["later_domestic_contact", "later_domestic_destination", "international_review_required"]}
        self.assertEqual(reviews.normalize_deadline_review(outcome, request), outcome)
        for changes in ({"outcome": "feasible"}, {"line_ids": ["other"]}, {"requirement_digest": "0" * 64}, {"options": ["guaranteed_international"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reviews.normalize_deadline_review({**outcome, **changes}, request)
        request.envelope["customer_requirements"].append({"supersession_hint": "withdrawn", "raw_quote": "Cancel all deadlines"})
        with self.assertRaises(ValueError):
            reviews.normalize_deadline_review(outcome, request)

    def test_payment_anchor_does_not_fabricate_production_start(self):
        timing = {"min_days": 1, "max_days": 3, "unit": "calendar", "anchor": "after_payment_confirmation"}
        self.assertEqual(reviews._timing(timing, self.now), timing)
        self.assertNotIn("anchor_at", reviews._timing(timing, self.now))
        legacy = {"min_days": 1, "max_days": 3, "unit": "calendar"}
        self.assertEqual(reviews._timing(legacy, self.now)["anchor_at"], self.now.isoformat())

    def test_render_impossible_instead_of_open_delivery_check(self):
        projection = {"current_request": {"locale": "en", "lines": [{"line_id": "selected", "label": "Selected tee XXL"}],
            "customer_requirements": [self.requirement()]}, "decision": {"lines": [{"line_id": "selected", "availability": "wait",
                "preparation": {"min_days": 1, "max_days": 3, "unit": "calendar", "anchor": "after_payment_confirmation"}}],
            "deadline_review": {"outcome": "impossible", "line_ids": ["selected"], "options": ["later_domestic_contact", "international_review_required"]}}}
        body = render_manager_update(projection, copy_version=2)
        self.assertIn("cannot meet your departure deadline", body)
        self.assertIn("after payment is confirmed", body)
        self.assertIn("boyfriend", body)
        self.assertIn("check feasibility", body)
        self.assertNotIn("still needs to be checked", body)

    def test_hard_cutoff_does_not_hold_unrelated_cart_line(self):
        request = SimpleNamespace(envelope={"lines": [{"line_id": "selected"}], "customer_requirements": [self.requirement()]})
        decision = SimpleNamespace(envelope={"deadline_review": {"outcome": "impossible", "line_ids": ["selected"]}})
        self.assertEqual(deadline_checkout_reason(request, decision, {"lines": [{"line_id": "other"}]}, now=self.now), "")
        with patch("management.services.ig_fulfillment_checkout.deadline_later_acceptance", return_value=None):
            self.assertEqual(deadline_checkout_reason(request, decision, {"lines": [{"line_id": "selected"}]}, now=self.now),
                "checkout_hard_deadline_later_receipt_consent_required")

    def test_adding_unrelated_line_cannot_erase_hold_on_original_exact_target(self):
        target = {"product_id": 7, "size": "XXL", "fit_option_code": "classic", "quantity": 1}
        request = SimpleNamespace(envelope={"lines": [{"line_id": "selected", "target": target}],
            "customer_requirements": [self.requirement()]})
        cart = {"lines": [{"line_id": "new-id"}, {"line_id": "unrelated"}], "source_watermark": {"message_id": 100}}
        with patch.object(reviews, "fulfillment_review_projection", return_value={"status": "stale", "reason": "selection_changed"}), \
            patch.object(reviews, "Request") as model, patch.object(reviews, "Decision") as decisions, \
            patch.object(reviews, "_current_reason", return_value="selection_changed"), \
            patch.object(reviews, "_settings", return_value=object()), \
            patch.object(reviews, "_lines", return_value=[{"target": target}]) as lines:
            model.objects.select_related.return_value.filter.return_value.order_by.return_value.first.return_value = request
            decisions.objects.filter.return_value.order_by.return_value.first.return_value = None
            self.assertEqual(fulfillment_deadline_checkout_reason(SimpleNamespace(pk=1), cart, now=self.now),
                "checkout_hard_deadline_review_required")
            lines.return_value = [{"target": {**target, "quantity": 2}}]
            self.assertEqual(fulfillment_deadline_checkout_reason(SimpleNamespace(pk=1), cart, now=self.now),
                "checkout_hard_deadline_review_required")
            lines.return_value = [{"target": {**target, "product_id": 8}}]
            self.assertEqual(fulfillment_deadline_checkout_reason(SimpleNamespace(pk=1), cart, now=self.now), "")


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeDepartureReviewTests(_RestockFixture, TestCase):
    def native_case(self, *, outcome_name="impossible"):
        from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest, IgFulfillmentReviewDecision
        first = self.source("I want this shirt for my boyfriend. I leave tomorrow at 10 AM")
        self.revision(first)
        self.canonical("Хочу купити")
        source = self.source("Чи доступна ця позиція?")
        revision, _ = self.revision(source)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        request = IgFulfillmentReviewRequest.objects.get(pk=prepared["request_id"])
        hint = next(item for item in request.envelope["customer_requirements"] if item.get("deadline_hard"))
        self.assertEqual(hint["source_id"], first.pk)
        self.assertIn(first.pk, [proof["message_id"] for proof in request.envelope["sources"]])
        manager = get_user_model().objects.create_superuser(username="departure-manager", email="manager@example.test", password="test")
        outcome = {"requirement_digest": requirement_digest(hint), "outcome": outcome_name,
            "line_ids": [row["line_id"] for row in request.envelope["lines"]], "options": ["later_domestic_contact"]}
        payload = {"lines": [{"line_id": row["line_id"], "availability": "wait",
            "preparation": {"min_days": 1, "max_days": 3, "unit": "calendar", "anchor": "after_payment_confirmation"}}
            for row in request.envelope["lines"]], "deadline_review": outcome}
        operation = uuid.uuid4()
        result = reviews.apply_fulfillment_decision(request.pk, actor=manager, expected_request_digest=request.envelope_hmac,
            operation_id=operation, decisions_by_line=payload, now=self.now)
        self.assertTrue(result["ready"], result)
        decision = IgFulfillmentReviewDecision.objects.get(pk=result["decision_id"])
        self.assertEqual(decision.envelope["deadline_review"], outcome)
        self.assertTrue(reviews._verified(decision, "decision"))
        repeated = reviews.apply_fulfillment_decision(request.pk, actor=manager, expected_request_digest=request.envelope_hmac,
            operation_id=operation, decisions_by_line=deepcopy(payload), now=self.now)
        self.assertTrue(repeated["ready"], repeated)
        self.assertFalse(repeated["created"])
        return request, decision, manager, payload

    def test_preselection_departure_uses_original_sealed_source_then_native_decision(self):
        self.native_case()

    def test_pending_can_resolve_impossible_as_append_only_version(self):
        request, first, manager, payload = self.native_case(outcome_name="review_pending")
        payload["deadline_review"]["outcome"] = "impossible"
        result = reviews.apply_fulfillment_decision(request.pk, actor=manager, expected_request_digest=request.envelope_hmac,
            operation_id=uuid.uuid4(), decisions_by_line=payload, now=self.now)
        self.assertTrue(result["ready"], result)
        self.assertEqual(result["version"], 2)
        first.refresh_from_db()
        self.assertEqual(first.envelope["deadline_review"]["outcome"], "review_pending")
        self.assertTrue(reviews._verified(first, "decision"))

    def test_explicit_later_receipt_owns_current_decision_and_latest_source(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        request, decision, _, _ = self.native_case()
        later = self.now + timedelta(minutes=1)
        source = self.source("Please send it later to my boyfriend in Kharkiv", at=later)
        later_revision, _ = self.revision(source, now=later)
        cart = capture_current_selection_lines(self.client_row.pk, now=later)
        proof = deadline_later_acceptance(request, decision, cart, now=later)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["source"]["message_id"], source.pk)
        self.assertEqual(proof["decision_digest"], decision.envelope_hmac)
        self.assertEqual(deadline_checkout_reason(request, decision, cart, now=later), "")
        again = reviews.prepare_fulfillment_review(later_revision, settings_obj=self.settings, now=later)
        self.assertEqual(again.get("request_id"), str(request.pk), again)
        self.assertFalse(again["created"])
        question_at = later + timedelta(minutes=1)
        question = self.source("Can it really arrive before I leave?", at=question_at)
        self.revision(question, now=question_at)
        cart = capture_current_selection_lines(self.client_row.pk, now=question_at)
        self.assertIsNone(deadline_later_acceptance(request, decision, cart, now=question_at))
