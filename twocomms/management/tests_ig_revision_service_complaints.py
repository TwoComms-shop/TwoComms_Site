"""Source-first service replies through the real revision/action/send pipeline."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import IgBotNotification, IgFollowUpTask, InstagramBotMessage
from management.services.ig_revision_intents import manager_case_reason
from management.services.ig_revision_live import _revision_response_purpose_reason
from management.services.ig_turn_intent import (
    build_turn_intent, service_complaint_reply_reason, validate_turn_response,
)

ACTUAL_DELIVERY_COMPLAINT = (
    "Доставка коштувала 90 а взяли 120\n"
    "Так 30 грн не багато, але на ровному місці\n"
    "Ось такі перші враження"
)


class ServiceComplaintReplyPolicyTests(SimpleTestCase):
    def test_support_rejects_positive_promotions_in_all_three_languages(self):
        for text in (
            "Пропонуємо бонус 10% на наступне замовлення.",
            "Можем дать скидку 10% на следующий заказ.",
            "We can offer a 10% coupon for your next order.",
            "Share a story and tag @twocomms.",
            "Залиште відгук про товар на сайті.",
        ):
            with self.subTest(text=text):
                self.assertEqual(validate_turn_response({"purpose": "support"}, text),
                                 "service_complaint_disallows_promotion")

    def test_normal_service_thanks_and_negative_promotion_mentions_are_allowed(self):
        for text in (
            "Дякуємо за чесний відгук. Команда перевіряє звернення.",
            "Не будемо замінювати вирішення питання бонусом.",
            "We will not replace a solution with a discount.",
            "Delivery charges depend on the carrier's route.",
        ):
            with self.subTest(text=text):
                self.assertEqual(validate_turn_response({"purpose": "support"}, text), "")

    def test_negating_one_pitch_does_not_permit_a_separate_positive_pitch(self):
        self.assertEqual(validate_turn_response({"purpose": "support"},
            "Не будемо замінювати вирішення питання бонусом, але пропонуємо знижку на наступне замовлення."),
            "service_complaint_disallows_promotion")

    def test_own_complaint_needs_apology_context_and_review(self):
        self.assertEqual(service_complaint_reply_reason("Дякую за повідомлення."),
                         "service_complaint_apology_missing")
        self.assertEqual(service_complaint_reply_reason("Перепрошую за ситуацію з доставкою."),
                         "service_complaint_review_missing")
        self.assertEqual(service_complaint_reply_reason(
            "Перепрошую за ситуацію з доставкою. Передамо звернення команді для перевірки."), "")

    def test_source_report_does_not_authorize_completed_leadership_decision(self):
        self.assertEqual(service_complaint_reply_reason(
            "Перепрошую за ситуацію з доставкою. Керівник вже погодив компенсацію, команда перевіряє звернення."),
            "service_complaint_unverified_resolution")

    def test_refund_promises_and_money_before_completed_refund_are_rejected(self):
        for text in (
            "Перепрошую за ситуацію з доставкою. Повернемо 30 грн завтра, команда перевірить звернення.",
            "Извините за ситуацию с доставкой. Вернём разницу, команда проверит обращение.",
            "I'm sorry about the delivery situation. We will refund 30 UAH tomorrow; the team will review it.",
            "Перепрошую за ситуацію з доставкою. Гроші вже повернули, команда перевірить звернення.",
        ):
            with self.subTest(text=text):
                self.assertEqual(service_complaint_reply_reason(text), "service_complaint_unverified_resolution")
        self.assertEqual(service_complaint_reply_reason(
            "I'm sorry about the delivery situation. I cannot promise a refund until manager review."), "")

    def test_unknown_source_cannot_bypass_an_unresolved_response_plan(self):
        from management.services.ig_revision_live import _captured_reply_truth
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_control import ResponseControl, ValidatedResponse

        plan = SimpleNamespace(plan_gap="response_plan_provided_cart_invalid")
        reply = ValidatedResponse(reply_text="Перепрошую за ситуацію з доставкою. Передамо звернення команді для перевірки.",
                                  controls=(ResponseControl("manager", True),))
        outcome = _captured_reply_truth(plan, reply, ReplyTruthContext())
        self.assertFalse(outcome.valid)
        self.assertEqual(outcome.reasons, ("response_plan_provided_cart_invalid",))

    def test_modal_compensation_and_arranged_refund_are_not_review_requests(self):
        for text in (
            "Перепрошую за ситуацію з доставкою. Можемо повернути різницю. Передамо звернення команді для перевірки.",
            "Извините за ситуацию с доставкой. Мы готовы вернуть разницу. Передадим обращение команде для проверки.",
            "I'm sorry about the delivery situation. A refund is arranged for tomorrow. The team will review it.",
            "I'm sorry about the delivery situation. We can issue compensation. The team will review it.",
        ):
            with self.subTest(text=text):
                self.assertEqual(service_complaint_reply_reason(text), "service_complaint_unverified_resolution")
        for text in (
            "Перепрошую за ситуацію з доставкою. Передам ваш запит щодо повернення керівнику.",
            "I'm sorry about the delivery situation. We'll ask the team to check whether a refund is possible.",
            "I'm sorry about the delivery situation. We'll ask the team to check whether we can refund you.",
            "I'm sorry about the delivery situation. I cannot refund you without manager review.",
        ):
            with self.subTest(text=text):
                self.assertEqual(service_complaint_reply_reason(text), "")

    def test_first_impression_copy_requires_explicit_source_evidence(self):
        from management.services.ig_revision_live import _service_complaint_reply

        for locale, phrase in (("uk", "перше враження"), ("ru", "первое впечатление"), ("en", "first impression")):
            with self.subTest(locale=locale):
                ordinary = _service_complaint_reply(locale, {"kind": "delivery_fee_dispute"})
                sourced = _service_complaint_reply(locale, {"kind": "delivery_fee_dispute"}, first_impression=True)
                self.assertNotIn(phrase, ordinary)
                self.assertIn(phrase, sourced)
                self.assertEqual(service_complaint_reply_reason(ordinary), "")
                self.assertEqual(service_complaint_reply_reason(sourced), "")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionServiceComplaintFlowTests(TransactionTestCase):
    reset_sequences = True
    # Reuse only fixture methods, not its test class (avoids duplicate suites).
    setUp = fixtures.RevisionLiveTests.setUp
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle

    def _message(self, text, mid):
        return InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, mid=mid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            text=text, provider_created_at=timezone.now(), status="pending",
        )

    def _assert_service_case_before_http(self, *_args, **_kwargs):
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["manager_handoff"]
        self.assertEqual(receipt["case_kind"], "service_complaint_review")
        task = IgFollowUpTask.objects.get(pk=receipt["task_id"])
        self.assertEqual(task.reason, "revision_case:service_complaint")
        proof = task.manager_context["service_complaint"]
        self.assertTrue(proof["customer_reported"])
        self.assertFalse(proof["monetary_authority"])
        self.assertEqual(proof["source_refs"][0]["message_id"], self.source.pk)
        self.assertEqual(IgBotNotification.objects.get(pk=receipt["notification_id"]).status, "pending")
        self.assertEqual(_revision_response_purpose_reason(self.revision), "")
        return 200, json.dumps({"message_id": f"service-answer-{self.revision.pk}"})

    def test_actual_source_discrepancy_creates_case_without_model_manager_control(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        self.assertEqual(manager_case_reason(self.revision), "service_complaint_review")
        self.parsed = {"reply_text": "Пропонуємо бонус 10% на наступне замовлення.", "controls": []}
        result, generate, http = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        reply = self.revision.generation_proposal["response"]["reply_text"]
        self.assertIn("Перепрошую", reply)
        self.assertNotIn("10%", reply)
        self.assertIn("передамо", reply.casefold())
        self.assertEqual(self.revision.generation_proposal["authority"]["allowed_actions"], ["manager_escalation_intent"])

    def test_russian_complaint_uses_current_source_locale(self):
        self._replace_bundle(["Вы обещали доставку за 90 грн, а взяли 120. Это испортило первое впечатление."])
        self.parsed = {"reply_text": "Можем дать скидку 10% на следующий заказ.", "controls": []}
        result, _, _ = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertIn("Извините", self.revision.generation_proposal["response"]["reply_text"])

    def test_english_complaint_uses_current_source_locale_over_uk_profile(self):
        self._replace_bundle(["You quoted delivery at 90 UAH but charged me 120. The difference is small, but the first impression was negative."])
        self.parsed = {"reply_text": "We can give you a bonus for your next order.", "controls": []}
        result, _, _ = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertIn("I'm sorry", self.revision.generation_proposal["response"]["reply_text"])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.language, "uk")

    def test_mixed_shopping_request_cannot_override_service_first(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT, "І ще хочу замовити худі, скільки коштує?"])
        from management.services.ig_response_plan import capture_response_plan

        original_plan = capture_response_plan(self.customer, revision=self.revision)
        price_obligations = [row["id"] for row in original_plan.obligations if row["kind"] == "info:price"]
        self.assertTrue(price_obligations, original_plan.as_dict())
        intent = build_turn_intent(self.customer, self.revision)
        self.assertEqual(intent["purpose"], "service_complaint")
        self.assertNotIn("retail_consultation", intent["allowed_response_acts"])
        self.parsed = {"reply_text": "Пропонуємо бонус на наступне замовлення.",
                       "controls": [{"kind": "paylink", "value": "full"}]}
        result, generate, http = self._execute(self._assert_service_case_before_http)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["response"]["controls"], [{"kind": "manager", "value": True}])
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertTrue(coverage["remaining"])
        self.assertEqual(coverage["plan_digest"], original_plan.digest)
        self.assertTrue(set(price_obligations).issubset(coverage["remaining"]))
        self.assertNotEqual(coverage["disposition"], "complete")
        self.assertNotEqual(result.state, "completed")
        task = IgFollowUpTask.objects.get(reason="revision_case:service_complaint")
        self.assertEqual(task.manager_context["pending_response_obligations"], coverage["remaining"])
        self.assertIn("remaining_customer_questions", task.manager_context["required_decisions"])

    def test_correct_contextual_apology_is_preserved(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        reply = "Перепрошую за ситуацію з доставкою. Розумію ваше невдоволення. Передамо звернення команді для перевірки."
        self.parsed = {"reply_text": reply, "controls": []}
        result, _, _ = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["response"]["reply_text"], reply)

    def _check_future_refund_candidate(self, source, candidate, apology):
        self._replace_bundle([source])
        self.parsed = {"reply_text": candidate, "controls": []}
        result, generate, http = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        reply = self.revision.generation_proposal["response"]["reply_text"]
        self.assertIn(apology, reply)
        self.assertNotIn("30", reply)
        self.assertEqual(service_complaint_reply_reason(reply), "")
        task = IgFollowUpTask.objects.get(reason="revision_case:service_complaint")
        self.assertFalse(task.manager_context["authority"]["refund_confirmed"])

    def test_uk_future_refund_candidate_is_normalized_without_extra_dispatch(self):
        self._check_future_refund_candidate(ACTUAL_DELIVERY_COMPLAINT,
            "Перепрошую за ситуацію з доставкою. Повернемо 30 грн завтра, команда перевірить звернення.", "Перепрошую")

    def test_ru_future_refund_candidate_is_normalized_without_extra_dispatch(self):
        self._check_future_refund_candidate("Вы обещали доставку за 90 грн, а взяли 120. Это испортило первое впечатление.",
            "Извините за ситуацию с доставкой. Вернём разницу 30 грн, команда проверит обращение.", "Извините")

    def test_en_future_refund_candidate_is_normalized_without_extra_dispatch(self):
        self._check_future_refund_candidate("You quoted delivery at 90 UAH but charged me 120. The difference was frustrating.",
            "I'm sorry about the delivery situation. We will refund 30 UAH tomorrow; the team will review it.", "I'm sorry")

    def test_uk_modal_refund_offer_is_normalized_without_extra_dispatch(self):
        self._check_future_refund_candidate(ACTUAL_DELIVERY_COMPLAINT,
            "Перепрошую за ситуацію з доставкою. Можемо повернути різницю. Передамо звернення команді для перевірки.", "Перепрошую")

    def test_ru_modal_refund_offer_is_normalized_without_extra_dispatch(self):
        self._check_future_refund_candidate("Вы обещали доставку за 90 грн, а взяли 120. Это испортило первое впечатление.",
            "Извините за ситуацию с доставкой. Мы готовы вернуть разницу. Передадим обращение команде для проверки.", "Извините")

    def test_en_arranged_refund_is_normalized_without_extra_dispatch(self):
        self._check_future_refund_candidate("You quoted delivery at 90 UAH but charged me 120. The difference was frustrating.",
            "I'm sorry about the delivery situation. A refund is arranged for tomorrow. The team will review it.", "I'm sorry")

    def test_handoff_recovery_reuses_case_receipt_without_another_generation(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        self.parsed = {"reply_text": "Пропонуємо бонус на наступне замовлення.", "controls": []}
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate), patch("management.services.instagram_bot.get_page_token", return_value=""):
            first = fixtures.execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(first.reasons, ("provider_not_configured",))
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:service_complaint").count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)
        result, generate, http = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:service_complaint").count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)

    def test_unresolved_complaint_continues_to_hold_promotions_after_thanks(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        self.parsed = {"reply_text": "Пропонуємо бонус на наступне замовлення.", "controls": []}
        result, _, _ = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        thanks = self._message("Дякую.", "service-neutral-thanks")
        intent = build_turn_intent(self.customer, source_messages=[thanks])
        self.assertEqual(intent["purpose"], "support")
        self.assertFalse(intent["service_complaint"])
        self.assertEqual(validate_turn_response(intent, "Пропонуємо бонус на наступне замовлення."), "service_complaint_disallows_promotion")
        self.assertEqual(validate_turn_response(intent, "Дякуємо. Звернення перевіряє команда."), "")

    def test_a_distinct_complaint_source_does_not_reuse_an_unrelated_open_case(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        self.parsed = {"reply_text": "Пропонуємо бонус на наступне замовлення.", "controls": []}
        result, _, _ = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        original_task = IgFollowUpTask.objects.get(reason="revision_case:service_complaint")
        original_source = self.source.pk
        self.source = self._message("Доставку обіцяли за 70 грн, а стягнули 100 грн. Це викликало невдоволення.", "distinct-complaint")
        self.turn = fixtures.IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        fixtures.IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = fixtures.create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        result, _, _ = self._execute(self._assert_service_case_before_http)
        self.assertEqual(result.state, "completed", result.reasons)
        original_task.refresh_from_db()
        self.assertEqual(original_task.manager_context["service_complaint"]["source_refs"][0]["message_id"], original_source)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:service_complaint").count(), 2)
        self.assertEqual(IgBotNotification.objects.count(), 2)

    def test_terminal_exact_case_before_http_prevents_automated_ack(self):
        from management.services import ig_revision_intents

        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        self.parsed = {"reply_text": "Пропонуємо бонус на наступне замовлення.", "controls": []}
        original = ig_revision_intents.ensure_revision_manager_case

        def close_exact_case(*args, **kwargs):
            outcome = original(*args, **kwargs)
            self.revision.refresh_from_db()
            receipt = self.revision.action_receipts["manager_handoff"]
            task = IgFollowUpTask.objects.get(pk=receipt["task_id"])
            task.status = "completed"
            task.save(update_fields=["status", "updated_at"])
            return outcome

        with patch.object(ig_revision_intents, "ensure_revision_manager_case", side_effect=close_exact_case):
            result, _, http = self._execute()
        http.assert_not_called()
        self.assertNotEqual(result.state, "completed")
        self.revision.refresh_from_db()
        self.assertIn("manager_handoff", self.revision.action_receipts)
        self.assertEqual(_revision_response_purpose_reason(self.revision), "service_complaint_human_handled")

    def _check_manual_manager_reply_ownership(self, *, terminal_status=None):
        from management.services.ig_response_debt import park_manual_revision

        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        with transaction.atomic():
            task = park_manual_revision(self.revision, "generation_failed")
        InstagramBotMessage.objects.create(client=self.customer, sender_id="owner-1", role="manager",
            source="echo", provider_namespace="instagram_login:owner-1", mid="human-service-answer",
            provider_created_at=timezone.now(), text="Перепрошую. Перевіримо різницю у вартості доставки.")
        if terminal_status:
            task.status = terminal_status
            task.save(update_fields=["status", "updated_at"])
        result, generate, http = self._execute()
        self.assertEqual(result.reasons, ("service_complaint_manager_reply_owned",))
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:service_complaint").exists())

    def test_existing_manual_manager_reply_ownership_prevents_duplicate_ack(self):
        self._check_manual_manager_reply_ownership()

    def test_completed_exact_manual_debt_keeps_old_source_owned_by_human(self):
        self._check_manual_manager_reply_ownership(terminal_status="completed")

    def test_cancelled_exact_manual_debt_preserves_explicit_manual_disposition(self):
        self._check_manual_manager_reply_ownership(terminal_status="cancelled")

    def test_positive_feedback_does_not_gain_complaint_or_handoff_authority(self):
        self._replace_bundle(["Дякую, все сподобалось."])
        self.assertEqual(manager_case_reason(self.revision), "")
        self.parsed = {"reply_text": "Дякуємо за відгук!", "controls": []}
        result, _, _ = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:service_complaint").exists())
