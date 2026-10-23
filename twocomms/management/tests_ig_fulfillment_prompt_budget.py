"""Production-sized captures and trade questions keep their separate contracts."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest as Request
from management.models import IgBotNotification, IgFollowUpTask
from management.services.ig_client_state_card import render_client_state_prompt
from management.services.ig_fulfillment_context import fulfillment_prompt_value
from management.services.ig_fulfillment_requirements import extract_requirements
from management.services import ig_fulfillment_reviews as review
from management.tests_ig_restock_subscriptions import _RestockFixture


BUSINESS_QUESTIONS = (
    "Добрий день! Хочу запустити Instagram-магазин TEST SHOP в Україні. "
    "Цікавить співпраця з продажу чорних oversize-футболок. "
    "1. Яка ціна для партнера? 2. Які є розміри та кольори? "
    "3. Чи можна почати без закупівлі партії товару? "
    "4. Чи можна продавати через Instagram і які умови доставки? "
    "5. Чи дозволяєте використовувати ваші фото товарів у магазині? Дякую!")


class TradeRequirementExtractionTests(SimpleTestCase):
    def test_general_business_shipping_and_catalogue_nouns_are_not_a_deadline(self):
        for text in (BUSINESS_QUESTIONS,
                "Цікавить співпраця. Які вироби є в каталозі?",
                "I want to launch a store. What are your shipping terms?"):
            with self.subTest(text=text):
                hint = extract_requirements(text)
                self.assertFalse(hint["request_detected"], hint)
                self.assertEqual(hint["intent"], "unknown")

    def test_actual_mixed_deadline_survives_business_clause_filter(self):
        hint = extract_requirements(BUSINESS_QUESTIONS + " Моє замовлення відправите до 20.10.2026?")
        self.assertTrue(hint["request_detected"], hint)
        self.assertEqual(hint["deadline_date"], "2026-10-20")
        self.assertEqual(hint["intent"], "dispatch")

    def test_projection_keeps_all_decisions_and_shipping_terms_without_mutating_capture(self):
        lines = [{"line_id": "a", "target": None, "preferences": {"color": "чорний"},
            "observation": {"status": "unknown", "reason": "size_unknown"}}]
        requirements = [{"raw_quote": BUSINESS_QUESTIONS, "source_id": 7}]
        decision = {"id": "decision", "lines": [{"line_id": "a", "availability": "wait",
            "checked_quantity": 2, "preparation": {"min_days": 7, "max_days": 10, "unit": "calendar"}}]}
        value = {"status": "reviewed", "current_request": {"id": "request", "lines": lines,
            "customer_requirements": requirements, "columns": {"signed": "audit"}},
            "lines": deepcopy(lines), "customer_requirements": deepcopy(requirements),
            "decision": decision, "split_shipping": {"proof": {"customer_fee_cap": "70.00"}},
            "restock_interest": {"status": "accepted"}, "customer_update": {"state": "sent"}}
        before = deepcopy(value)
        rendered = fulfillment_prompt_value(value)
        self.assertEqual(value, before)
        expected_lines = deepcopy(lines)
        observation = expected_lines[0].pop("observation")
        expected_lines[0]["accounting_observation"] = {
            "accounting_status": observation["status"], "accounting_reason": observation["reason"]}
        self.assertEqual(rendered["current_request"]["lines"], expected_lines)
        self.assertEqual(rendered["current_request"]["customer_requirements"], requirements)
        self.assertNotIn("lines", rendered)
        self.assertNotIn("customer_requirements", rendered)
        for key in ("decision", "split_shipping", "restock_interest", "customer_update"):
            self.assertEqual(rendered[key], value[key])


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=2400,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeFulfillmentPromptBudgetTests(_RestockFixture, TestCase):
    def prepare(self, text):
        source = self.source(text)
        revision, _ = self.revision(source)
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = review.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        provider.assert_not_called()
        return result

    def test_trade_questions_do_not_create_stock_case_for_existing_cart(self):
        result = self.prepare(BUSINESS_QUESTIONS)
        self.assertEqual(result["reason"], "no_review_requirement", result)
        self.assertFalse(Request.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason="fulfillment_review").exists())
        self.assertFalse(IgBotNotification.objects.filter(event_type="fulfillment_review").exists())

    def test_mixed_trade_and_specific_retail_availability_still_needs_review(self):
        result = self.prepare(BUSINESS_QUESTIONS + " А ця футболка M є в наявності?")
        self.assertTrue(result["ready"], result)
        self.assertTrue(Request.objects.filter(pk=result["request_id"]).exists())

    def test_real_pending_and_reviewed_capture_fit_production_budget_without_fact_loss(self):
        from management.services.ig_admin_state_capture import current_admin_state
        text = BUSINESS_QUESTIONS + " Моє замовлення відправите до 20.10.2026?"
        result = self.prepare(text)
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        manager = get_user_model().objects.create_superuser(username="budget-manager", password="test")
        for status in ("pending", "reviewed"):
            if status == "reviewed":
                import uuid
                decision = review.apply_fulfillment_decision(request.pk, actor=manager,
                    expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
                    decisions_by_line=[{"line_id": line["line_id"], "availability": "wait",
                        "preparation": {"min_days": 7, "max_days": 10, "unit": "calendar"}}
                        for line in request.envelope["lines"]], now=self.now)
                self.assertTrue(decision["ok"], decision)
            captured = current_admin_state(self.client_row.pk, now=self.now)
            self.assertEqual(captured.status, "captured", captured)
            state = captured.state
            before = state.as_dict()
            rendered = render_client_state_prompt(state, budget=2400,
                excluded_slots=("communication.preferences", "context.useful_memory"))
            self.assertFalse(rendered.oversized, (status, rendered.estimated_tokens))
            self.assertLessEqual(rendered.estimated_tokens, 2400)
            block = next(json.loads(line) for line in rendered.text.splitlines()
                if line.startswith('{') and json.loads(line).get("slot") == "fulfillment.review")
            value = block["value"]
            self.assertEqual(value["status"], status)
            self.assertEqual(value["current_request"]["customer_requirements"],
                request.envelope["customer_requirements"])
            for actual, original in zip(value["current_request"]["lines"], request.envelope["lines"], strict=True):
                for key in ("line_id", "recipient_id", "target"):
                    self.assertEqual(actual[key], original[key])
                accounting = deepcopy(original["observation"])
                for key in ("status", "reason"):
                    if key in accounting:
                        accounting["accounting_" + key] = accounting.pop(key)
                self.assertEqual(actual["accounting_observation"], accounting)
                for key in ("values", "evidence", "cleared"):
                    self.assertEqual(actual["preferences"].get(key), original["preferences"].get(key))
            if status == "reviewed":
                self.assertEqual(value["decision"]["lines"][0]["preparation"]["max_days"], 10)
            self.assertIn("current.source_cart", rendered.included)
            self.assertEqual(state.as_dict(), before)

    def test_genuinely_oversized_requirement_still_fails_closed(self):
        from management.services.ig_admin_state_capture import current_admin_state
        from management.services.ig_client_state_card import CapturedClientState
        result = self.prepare("Відправите до 20.10.2026?")
        self.assertTrue(result["ready"], result)
        payload = current_admin_state(self.client_row.pk, now=self.now).state.as_dict()
        # Exercise the presentation boundary with an oversized detached fact;
        # no persisted envelope or source evidence is changed.
        payload["slots"]["fulfillment.review"]["value"]["current_request"]["customer_requirements"] = [
            {"raw_quote": "повна вимога " * 4000}]
        state = CapturedClientState(json.dumps(payload, ensure_ascii=False).encode())
        rendered = render_client_state_prompt(state, budget=2400)
        self.assertTrue(rendered.oversized)
        self.assertEqual(rendered.text, "")
