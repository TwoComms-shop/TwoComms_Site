"""Native partial-card sources enter dialogue truth without charging the wishlist."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
from management.models import IgCheckoutInventoryReservation, IgCheckoutProposal, IgFollowUpTask
from management.services.ig_assisted_commerce_context import capture_assisted_context
from management.services.ig_assisted_scopes import selection_evidence, verified_selection_from_evidence
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import ResponseControl, ValidatedResponse
from management.services.ig_response_plan import capture_response_plan
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.models import Order, PaymentAttempt


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeAssistedPartialContextTests(PartialCommerceFixture, TransactionTestCase):
    reset_sequences = True

    def frame(self, result):
        plan = capture_response_plan(self.client_row, revision=result["revision"])
        frame, reason = capture_assisted_context(self.client_row, result["revision"], plan, now=self.now)
        self.assertIsNotNone(frame, reason)
        return frame, plan

    def economic_counts(self):
        return (IgCheckoutProposal.objects.count(), IgCheckoutInventoryReservation.objects.count(),
            IgAssistedCommerceExecution.objects.count(), PaymentAttempt.objects.count(), Order.objects.count(),
            IgFollowUpTask.objects.count())

    def test_actual_native_choice_captures_exact_selected_price_and_unpaid_deferred_facts_read_only(self):
        result = self.selected_case()
        before = self.economic_counts()
        with CaptureQueriesContext(connection) as queries, \
                patch("management.services.instagram_bot._provider_http") as provider:
            with CaptureQueriesContext(connection) as plan_queries:
                plan = capture_response_plan(self.client_row, revision=result["revision"])
            with CaptureQueriesContext(connection) as context_queries:
                frame, reason = capture_assisted_context(self.client_row, result["revision"], plan, now=self.now)
                self.assertIsNotNone(frame, reason)
            with CaptureQueriesContext(connection) as descriptor_queries:
                projection = frame.projection
                guidance = frame.prompt_guidance()
        provider.assert_not_called()
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")) for row in queries))
        select_count = lambda captured: sum(row["sql"].lstrip().upper().startswith("SELECT ") for row in captured)
        # Comparable two-custom-line released FULL39: 382 fresh dialogue
        # SELECTs. Generic client_context's 8/96 budgets do not include the
        # fresh commercial approval/stock/source gate measured here.
        self.assertEqual(len(descriptor_queries), 0)
        self.assertLessEqual(select_count(queries), 382,
            f"fresh plan={select_count(plan_queries)}, native context={select_count(context_queries)}, "
            f"projection={select_count(queries) - select_count(plan_queries) - select_count(context_queries)}")
        self.assertEqual(self.economic_counts(), before)
        self.assertEqual(projection["schema"], "current-assisted-partial-dialogue.v1")
        self.assertTrue(projection["native_purchase_accepted"])
        self.assertEqual(frame.approved_total, Decimal("1590.00"))
        self.assertEqual(projection["approved_total"], "1590.00")
        self.assertEqual(frame.reference["selection"], selection_evidence(result["selection"]))
        self.assertEqual([line["line_id"] for line in projection["selected_lines"]], result["selection"].selected_line_ids)
        self.assertEqual([line["line_id"] for line in projection["deferred_unpaid_lines"]], result["selection"].deferred_line_ids)
        deferred = projection["deferred_unpaid_lines"][0]
        self.assertEqual((deferred["title"], deferred["availability"], deferred["disposition"], deferred["unit_price"]),
            ("Оливкове худі", "wait", "unpaid_wishlist", None))
        self.assertIsNone(deferred["dispatch"])
        self.assertEqual(projection["shipping_terms"]["now"]["customer_maximum"], "120.00")
        self.assertIn("deferred_unpaid_lines remain an unpaid wishlist", guidance)
        self.assertNotIn('"unit_price":"1490.00"', guidance)
        self.assertTrue(frame.purchase_ready(result["revision"], plan.source_cart_capture))

    def test_scoped_manager_draft_approval_without_native_choice_has_no_price_or_purchase_authority(self):
        result = self.partial_case()
        result["revision"] = result["original_revision"]
        frame, plan = self.frame(result)
        self.assertTrue(frame.partial_pending)
        self.assertIsNone(frame.selection)
        self.assertIsNone(frame.approved_total)
        self.assertIsNone(frame.projection["approved_total"])
        self.assertFalse(frame.projection["native_purchase_accepted"])
        self.assertTrue(all(line["unit_price"] is None for line in frame.projection["lines"]))
        self.assertFalse(frame.purchase_ready(result["revision"], plan.source_cart_capture))
        self.assertFalse(frame.validate_reply(ValidatedResponse(reply_text="Фіолетове худі: ціна 1590 грн."), ReplyTruthContext()).valid)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_native_wait_for_all_is_readable_without_partial_price_or_checkout_capability(self):
        result = self.selected_case(action="together")
        self.assertIsNone(result["selection"])
        frame, plan = self.frame(result)
        self.assertTrue(frame.partial_pending)
        self.assertFalse(frame.can_prepare_checkout)
        self.assertFalse(frame.purchase_ready(result["revision"], plan.source_cart_capture))
        self.assertIsNone(frame.projection["approved_total"])
        self.assertEqual({row["title"] for row in frame.projection["deferred_unpaid_lines"]},
            {"Фіолетове худі", "Оливкове худі"})
        self.assertFalse(frame.validate_reply(ValidatedResponse(reply_text="Готова частина: разом 1590 грн."), ReplyTruthContext()).valid)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_selected_goods_price_cannot_price_deferred_goods_or_the_whole_original_wishlist(self):
        result = self.selected_case()
        frame, _plan = self.frame(result)
        good = ValidatedResponse(reply_text="Фіолетове худі: ціна 1590 грн. Оливкове худі залишається в очікуванні, ще не оплачено.")
        self.assertTrue(frame.validate_reply(good, ReplyTruthContext()).valid)
        for text in ("Оливкове худі: ціна 1490 грн.", "Оливкове худі: ціна 1590 грн.",
            "Ціна 1590 грн.", "Весь кошик: разом 1590 грн.", "Оливкове худі в наявності.",
            "Оливкове худі прибуде завтра."):
            with self.subTest(text=text):
                self.assertFalse(frame.validate_reply(ValidatedResponse(reply_text=text), ReplyTruthContext()).valid)
        self.assertTrue(frame.validate_reply(ValidatedResponse(reply_text="Разом за готову частину 1590 грн."), ReplyTruthContext()).valid)

    def test_raw_selection_and_changed_selected_price_reference_cannot_create_dialogue_authority(self):
        result = self.selected_case()
        frame, _plan = self.frame(result)
        raw = selection_evidence(result["selection"])
        with self.assertRaises(ValueError):
            replace(frame, selection=raw)
        changed = deepcopy(frame.reference)
        changed["selection"]["quoted_total"] = "1.00"
        captured, reason = capture_assisted_context(self.client_row, result["revision"], reference=changed, now=self.now)
        self.assertIsNone(captured)
        self.assertEqual(reason, "assisted_offer_changed")
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_actual_native_execution_history_is_not_a_current_partial_price_capability(self):
        result = self.selected_case()
        frame, _plan = self.frame(result)
        self.prepare_selection(result["selection"])
        execution = IgAssistedCommerceExecution.objects.get(request=result["request"], decision=result["decision"])
        historical = verified_selection_from_evidence(result["request"], result["decision"], execution, historical=True)
        self.assertTrue(historical.historical)
        with self.assertRaises(ValueError):
            replace(frame, selection=historical)

    def test_native_purchase_source_only_authorizes_its_own_sealed_revision(self):
        result = self.selected_case()
        frame, plan = self.frame(result)
        self.assertTrue(frame.purchase_ready(result["revision"], plan.source_cart_capture))
        self.now += timedelta(seconds=1)
        followup = self.source("Чи зможете гарантувати прибуття завтра?", at=self.now)
        unrelated_revision, _token = self.revision(followup, now=self.now)
        self.assertFalse(frame.purchase_ready(unrelated_revision, plan.source_cart_capture))
        self.assertFalse(frame.validate_reply(ValidatedResponse(reply_text="Фіолетове худі точно прибуде завтра."), ReplyTruthContext()).valid)

    def test_native_selected_checkout_credits_only_its_real_source_operation_and_valid_full_action(self):
        result = self.selected_case()
        frame, plan = self.frame(result)
        response = ValidatedResponse(reply_text="Фіолетове худі: ціна 1590 грн. Оливкове худі ще не оплачено.",
            controls=(ResponseControl("paylink", "full"),))
        original = plan.coverage(response)
        owned = [row["id"] for row in plan.obligations if row["kind"] == "unresolved_line_operation"
            and row["source_message_id"] == result["source"].pk and not row.get("line_id")]
        self.assertEqual(len(owned), 1)
        self.assertIn(owned[0], original["remaining"])
        covered = frame.coverage(plan, response, result["revision"], original)
        self.assertIn(owned[0], covered["covered"])
        self.assertNotIn(owned[0], covered["remaining"])
        self.assertEqual(set(original["remaining"]) - set(covered["remaining"]), set(owned))
        for rejected in (replace(response, controls=()),
            replace(response, controls=(ResponseControl("paylink", "prepay"),)),
            replace(response, controls=(ResponseControl("paylink", "full"), ResponseControl("product", str(self.products[1].pk)))),
            replace(response, reply_text="Оливкове худі: ціна 1490 грн.")):
            with self.subTest(response=rejected):
                self.assertEqual(frame.coverage(plan, rejected, result["revision"], original), original)
        self.now += timedelta(seconds=1)
        unrelated = self.source("Чи зможете гарантувати прибуття завтра?", at=self.now)
        other_revision, _token = self.revision(unrelated, now=self.now)
        self.assertEqual(frame.coverage(plan, response, other_revision, original), original)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_edited_native_choice_source_cannot_refresh_partial_dialogue_or_operation_credit(self):
        result = self.selected_case()
        frame, _plan = self.frame(result)
        result["source"].text = "Натомість хочу інший товар."
        result["source"].save(update_fields=["text"])
        refreshed, reason = capture_assisted_context(self.client_row, result["revision"],
            reference=frame.reference, now=self.now)
        self.assertIsNone(refreshed)
        self.assertTrue(reason)
        self.assertFalse(IgCheckoutProposal.objects.exists())
