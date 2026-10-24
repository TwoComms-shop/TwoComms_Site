"""Saved QA23 candidates cross the real reducer and native candidate guard."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixture
from management.models import IgCustomerTurn, IgTurnMessage, IgCommerceTurnDecision
from management.services import ig_response_plan, ig_revision_live
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_revision_commerce import reduce_inbound_commerce_source
from management.services.ig_turn_revisions import create_collecting_revision
from management import tests_ig_source_cart_catalog as cart_fixture


SOURCES = (
    "Собі хочу одну чорну футболку QA Trade Футболка, розмір M, крій classic.",
    "Хлопцю дві такі самі чорні футболки QA Trade Футболка, розмір L, крій classic.",
    "Розмір краще M.",
)
# Verbatim parsed candidate from actual run4000bb2d21f84a1dabc4552bbab2f12d.
BAD_CANDIDATE = {
    "reply_text": "Зрозуміла, для хлопця обираємо футболку QA Trade в розмірі M. Записала!",
    "controls": [{"kind": "product", "value": 1}, {"kind": "color_variant_id", "value": 1},
        {"kind": "fit", "value": "classic"}, {"kind": "size", "value": "M"}, {"kind": "qty", "value": 2}],
}
CLARIFICATION = {"reply_text": "Для кого саме змінити розмір на M — для вас чи для хлопця?", "controls": []}
# Verbatim parsed candidates from actual V5 run ddabeb1f985b4f4e954e715d12eaee46.
V5_LINE_CLARIFICATION = "Уточніть, будь ласка, для якої саме позиції змінити розмір на M?"
V5_SHARED_DISCOVERY = "Привіт! З радістю допоможу підібрати футболки для вас і хлопця. Який саме дизайн, колір чи принт вам до вподоби?"


class _CandidateObserved(BaseException):
    pass


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=20000)
class CandidateAuthorityTests(TransactionTestCase):
    reset_sequences = True
    _message = live_fixture.RevisionLiveTests._message
    _prepare = live_fixture.RevisionLiveTests._prepare
    _generate = live_fixture.RevisionLiveTests._generate

    def setUp(self):
        live_fixture.RevisionLiveTests.setUp(self)
        cart_fixture.SourceCartCatalogTests.setUp(self)
        self.category.name, self.category.slug = "Футболки", "tshirts"
        self.category.save()
        self.products[0].title = "QA Trade Футболка"
        self.products[0].save(update_fields=["title"])
        self.assertEqual((self.products[0].pk, self.variants[0].pk), (1, 1))
        self.revision.delete(); self.turn.delete(); self.source.delete()
        self.sequence = 0

    def seal(self, texts):
        rows = []
        for index, text in enumerate(texts):
            self.sequence += 1
            row = self._message(text, f"candidate-source-{self.sequence}")
            row.provider_created_at = timezone.now() - timedelta(seconds=len(texts) - index - 1)
            row.save(update_fields=["provider_created_at"])
            rows.append(row)
        self.source = rows[0]
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=rows[0],
            window_started_at=timezone.now(), window_deadline=timezone.now())
        for index, row in enumerate(rows, 1):
            IgTurnMessage.objects.create(turn=self.turn, message=row, ordinal=index, role="user")
        self.revision = create_collecting_revision(self.turn, rows, bypass_quiet=True).revision
        self._prepare()
        return rows

    def probe(self, candidate):
        observed = {}
        capture = ig_response_plan.capture_response_plan
        def plan(*args, **kwargs):
            result = capture(*args, **kwargs)
            observed["plan"] = result
            return result
        def gateway(payload, **kwargs):
            observed["decision"] = kwargs["result_validator"](deepcopy(candidate), usage={"_request_inline_count": 0})
            raise _CandidateObserved()
        with (patch.object(ig_response_plan, "capture_response_plan", side_effect=plan),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=gateway),
            patch("management.services.instagram_bot.get_page_token", return_value="unused-fictional-token"),
            patch("management.services.instagram_bot._provider_http") as transport):
            with self.assertRaises(_CandidateObserved):
                ig_revision_live.execute_claimed_revision(self.revision.pk, self.token, self.settings)
        transport.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.count(), 0)
        self.assertFalse(self.customer.checkout_proposals.exists())
        return observed["decision"], observed["plan"]

    def assert_original_choices(self):
        cart = capture_current_selection_lines(self.customer.pk)
        self.assertEqual([(row["recipient_id"], row["fields"]["size"]["value"], row["fields"]["quantity"]["value"])
            for row in cart["lines"]], [("self", "M", 1), ("boyfriend", "L", 2)])
        return cart

    def test_exact_saved_candidate_is_rejected_after_three_genuine_sources(self):
        rows = self.seal(SOURCES)
        decision, plan = self.probe(BAD_CANDIDATE)
        self.assertFalse(decision.valid, decision.reason_codes)
        self.assertIn("response_plan_cart_recipient_clarification_required", decision.reason_codes)
        self.assertEqual(list(IgCommerceTurnDecision.objects.filter(source_message__in=rows)
            .order_by("source_message_id").values_list("accepted", flat=True)), [True, True, False])
        cart = self.assert_original_choices()
        self.assertEqual(cart["recipient_planning_state"]["source_message_id"], rows[-1].pk)
        self.assertEqual(cart["recipient_planning_state"]["unassigned_fields"], {"size": "M"})
        self.assertTrue(all(row["fields"]["size"]["source"]["source_message_id"] != rows[-1].pk for row in cart["lines"]))

    def test_focused_clarification_is_valid_without_applying_the_correction(self):
        rows = self.seal(SOURCES)
        from management.services.ig_response_control import ValidatedResponse
        for text in (CLARIFICATION["reply_text"], V5_LINE_CLARIFICATION,
                "Для якої саме позиції потрібен розмір M?"):
            with self.subTest(text=text):
                decision, plan = self.probe({"reply_text": text, "controls": []})
                self.assertTrue(decision.valid, decision.reason_codes)
                coverage = plan.coverage(ValidatedResponse(text))
                self.assertEqual(coverage["disposition"], "waiting_on_customer", coverage)
                self.assertIn(f"{rows[-1].pk}:unresolved_line_operation", coverage["remaining"])
                self.assertEqual(coverage["next_selector"], "recipient")
                self.assertTrue(plan.prompt_projection()["cart_selector_clarification"])
                self.assert_original_choices()

    def test_wrong_recorded_choice_without_controls_is_also_rejected(self):
        self.seal(SOURCES)
        decision, _ = self.probe({**BAD_CANDIDATE, "controls": []})
        self.assertFalse(decision.valid)
        self.assert_original_choices()

    def test_fresh_shared_color_or_print_intake_question_is_valid(self):
        self.seal(["Привіт! Хочу замовити футболку для себе та хлопця"])
        for text in ("Привіт! Чудово, допоможу підібрати футболки для вас обох. Який колір або принт вам подобається?",
                V5_SHARED_DISCOVERY, "Який дизайн вам до вподоби для футболок?"):
            with self.subTest(text=text):
                decision, plan = self.probe({"reply_text": text, "controls": []})
                self.assertTrue(decision.valid, decision.reason_codes)
                self.assertTrue(all(not line.as_dict()["choices"].get("product_id") for line in plan.line_plans))

    def prior_pair(self):
        for text in SOURCES[:2]:
            self.sequence += 1
            row = self._message(text, f"prior-candidate-{self.sequence}")
            row.provider_created_at = timezone.now() - timedelta(minutes=1)
            row.save(update_fields=["provider_created_at"])
            with transaction.atomic():
                result = reduce_inbound_commerce_source(self.customer, row, expected_provider_namespace=row.provider_namespace)
            self.assertTrue(result.ready, result.reason)
            row.status = "done"; row.save(update_fields=["status"])
        self.customer.refresh_from_db()

    def test_cart_wide_price_requires_each_line_own_price(self):
        self.prior_pair()
        self.seal(["Підкажіть ціну на парні футболки."])
        text = "Позиція 1: QA Trade Футболка — 790 грн. Позиція 2 для хлопця: QA Trade Футболка — 790 грн."
        decision, plan = self.probe({"reply_text": text, "controls": []})
        self.assertTrue(decision.valid, decision.reason_codes)
        prices = [row for row in plan.obligations if row["kind"] == "info:price"]
        self.assertEqual({row["line_id"] for row in prices}, {line.line_id for line in plan.line_plans})
        from management.services.ig_response_control import ValidatedResponse
        coverage = plan.coverage(ValidatedResponse(text))
        self.assertTrue(all(row["id"] in coverage["covered"] for row in prices), coverage)

    def test_cart_wide_price_does_not_pool_generic_or_wrong_line_price(self):
        self.prior_pair()
        self.seal(["Підкажіть ціну на парні футболки."])
        decision, _ = self.probe({"reply_text": "Ціна футболки QA Trade Футболка — 790 грн.", "controls": []})
        self.assertFalse(decision.valid)

    def deliver_clarification(self):
        self.seal(SOURCES)
        self.parsed = deepcopy(CLARIFICATION)
        result, _, http = live_fixture.RevisionLiveTests._execute(self)
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.assertEqual(result.reasons, ("waiting_on_customer",))
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        from management.services.ig_response_debt import response_coverage, delivered_selector_wait
        receipt = response_coverage(self.revision)
        self.assertEqual((receipt["disposition"], receipt["next_selector"]), ("waiting_on_customer", "recipient"))
        self.assertTrue(delivered_selector_wait(self.revision))
        return self.revision.sources.order_by("ordinal").last().message_id

    def test_persistent_negative_survives_price_topic_jump_and_purchase(self):
        pending_id = self.deliver_clarification()
        self.seal(["А ціна яка?"])
        _, plan = self.probe(CLARIFICATION)
        self.assertEqual(plan.configuration.get("cart_selector_clarification", {}).get("source_message_ids"), [pending_id], plan.as_dict())
        self.seal(["Беру. Оформіть замовлення."])
        decision, plan = self.probe({"reply_text": "Перейдіть на сторінку оформлення.",
            "controls": [{"kind": "paylink", "value": "full"}]})
        self.assertFalse(decision.valid)
        self.assertTrue(set(decision.reason_codes).intersection({"response_plan_cart_recipient_clarification_required",
            "checkout_cart_clarification_required"}), decision.reason_codes)
        self.assertEqual(plan.configuration.get("cart_selector_clarification", {}).get("source_message_ids"), [pending_id],
            {"plan": plan.as_dict(), "cart": plan.source_cart_capture, "live": capture_current_selection_lines(self.customer.pk)})
        self.assert_original_choices()

    def test_independent_line_prices_send_without_repeating_recipient_question(self):
        pending_id = self.deliver_clarification()
        self.seal(["Підкажіть ціну на парні футболки."])
        self.parsed = {"reply_text": "Позиція 1: QA Trade Футболка — 790 грн. Позиція 2 для хлопця: QA Trade Футболка — 790 грн.",
            "controls": []}
        decision, plan = self.probe(self.parsed)
        self.assertTrue(decision.valid, decision.reason_codes)
        self.assertEqual(plan.configuration["cart_selector_clarification"]["source_message_ids"], [pending_id])
        result, _, http = live_fixture.RevisionLiveTests._execute(self)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(self.assert_original_choices()["recipient_planning_state"]["source_message_id"], pending_id)
        self.seal(["Оформіть замовлення."])
        decision, _ = self.probe({"reply_text": "Перейдіть на сторінку оформлення.",
            "controls": [{"kind": "paylink", "value": "full"}]})
        self.assertFalse(decision.valid)
        self.assert_original_choices()

    def test_independent_answer_cannot_record_pending_selector(self):
        self.deliver_clarification()
        self.seal(["Підкажіть ціну на парні футболки."])
        decision, _ = self.probe({"reply_text": "Позиція 1: QA Trade Футболка — 790 грн. Позиція 2 для хлопця: QA Trade Футболка — 790 грн. Для хлопця записала розмір M.",
            "controls": []})
        self.assertFalse(decision.valid)
        self.assert_original_choices()

    def test_new_own_explicit_boyfriend_size_after_delivered_question_is_authority(self):
        self.deliver_clarification()
        rows = self.seal(["Для хлопця розмір M."])
        decision, plan = self.probe(BAD_CANDIDATE)
        self.assertTrue(decision.valid, decision.reason_codes)
        self.parsed = {"reply_text": "Для хлопця записала розмір M.", "controls": []}
        result, generated, http = live_fixture.RevisionLiveTests._execute(self)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertNotIn("cart_selector_clarification", plan.configuration)
        cart = capture_current_selection_lines(self.customer.pk)
        self.assertEqual([(row["recipient_id"], row["fields"]["size"]["value"], row["fields"]["quantity"]["value"])
            for row in cart["lines"]], [("self", "M", 1), ("boyfriend", "M", 2)])
        self.assertEqual(cart["lines"][1]["fields"]["size"]["source"]["source_message_id"], rows[0].pk)

    def test_unchanged_recipient_confirmation_clears_without_a_value_delta(self):
        self.deliver_clarification()
        self.seal(["Хлопцю залишаємо L, мені M"])
        decision, plan = self.probe({"reply_text": "Для хлопця залишаємо розмір L.", "controls": []})
        self.assertTrue(decision.valid, decision.reason_codes)
        self.assertNotIn("cart_selector_clarification", plan.configuration)
        self.assert_original_choices()

    def test_russian_period_confirmation_clears_without_changing_choices(self):
        self.deliver_clarification()
        self.seal(["Парню оставляем L. Мне M."])
        decision, plan = self.probe({"reply_text": "Для парня остаётся размер L.", "controls": []})
        self.assertTrue(decision.valid, decision.reason_codes)
        self.assertNotIn("cart_selector_clarification", plan.configuration)
        self.assert_original_choices()

    def test_alternative_or_mixed_unscoped_answer_does_not_clear(self):
        self.deliver_clarification()
        self.seal(["Хлопцю або мені розмір M."])
        decision, _ = self.probe(BAD_CANDIDATE)
        self.assertFalse(decision.valid)
        self.assert_original_choices()

    def test_fake_recording_cannot_be_laundered_by_a_following_clarifier(self):
        self.seal(SOURCES)
        for question in ("Для кого саме змінити розмір?", V5_LINE_CLARIFICATION):
            with self.subTest(question=question):
                decision, _ = self.probe({"reply_text": "Для хлопця записала розмір M. " + question, "controls": []})
                self.assertFalse(decision.valid)
                self.assert_original_choices()

    def test_shared_discovery_has_no_selection_controls(self):
        self.seal(["Привіт! Хочу замовити футболку для себе та хлопця"])
        for text in ("Який колір або принт вам подобається?", V5_SHARED_DISCOVERY):
            with self.subTest(text=text):
                decision, _ = self.probe({"reply_text": text,
                    "controls": [{"kind": "qty", "value": 2}]})
                self.assertFalse(decision.valid)

    def test_line_clarification_cannot_carry_selection_or_checkout_controls(self):
        self.seal(SOURCES)
        for control in ({"kind": "size", "value": "M"}, {"kind": "paylink", "value": "full"}):
            with self.subTest(control=control):
                decision, _ = self.probe({"reply_text": V5_LINE_CLARIFICATION, "controls": [control]})
                self.assertFalse(decision.valid)
                self.assert_original_choices()

    def test_shared_discovery_does_not_replace_configured_line_questions(self):
        self.prior_pair()
        self.seal(["Підкажіть ціну на парні футболки."])
        for text in ("Який колір або принт вам подобається?", V5_SHARED_DISCOVERY):
            with self.subTest(text=text):
                decision, _ = self.probe({"reply_text": text, "controls": []})
                self.assertFalse(decision.valid)

    def test_one_line_price_does_not_discharge_sibling_price(self):
        self.prior_pair()
        self.seal(["Підкажіть ціну на парні футболки."])
        text = "Позиція 1: QA Trade Футболка — 790 грн."
        decision, plan = self.probe({"reply_text": text, "controls": []})
        self.assertTrue(decision.valid, decision.reason_codes)
        from management.services.ig_response_control import ValidatedResponse
        coverage = plan.coverage(ValidatedResponse(text))
        prices = [row for row in plan.obligations if row["kind"] == "info:price"]
        self.assertEqual(sum(row["id"] in coverage["covered"] for row in prices), 1)
        self.assertEqual(sum(row["id"] in coverage["remaining"] for row in prices), 1)

    def test_foreign_catalog_price_cannot_answer_a_line(self):
        self.prior_pair()
        self.seal(["Підкажіть ціну на парні футболки."])
        decision, _ = self.probe({"reply_text": "Позиція 1: QA Trade Футболка — 1290 грн. Позиція 2 для хлопця: QA Trade Футболка — 790 грн.", "controls": []})
        self.assertFalse(decision.valid)

    def test_real_fresh_conversation_reset_drops_previous_negative(self):
        from django.contrib.auth import get_user_model
        from management.services.ig_funnel_reset import reset_funnel
        self.deliver_clarification()
        actor = get_user_model().objects.create_superuser(username="candidate-reset-owner", password="test")
        reset = reset_funnel(client_id=self.customer.pk, actor=actor, mode="fresh_conversation")
        self.assertTrue(reset["ok"], reset)
        self.customer.refresh_from_db()
        self.seal([SOURCES[0]])
        decision, plan = self.probe({"reply_text": "Для себе футболка QA Trade Футболка розмір M.", "controls": []})
        self.assertTrue(decision.valid, decision.reason_codes)
        self.assertNotIn("cart_selector_clarification", plan.configuration)

    def test_mixed_explicit_and_unscoped_confirmation_does_not_clear(self):
        self.deliver_clarification()
        self.seal(["Хлопцю залишаємо L. Розмір M."])
        decision, _ = self.probe(BAD_CANDIDATE)
        self.assertFalse(decision.valid)
        self.assert_original_choices()

    def positive_stock(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        variant = self.variants[0]
        variant.stock = 10; variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=self.products[0], source="catalog_variant")
        for size in ("M", "L"):
            VariantSizeRule.objects.create(variant=variant, fit_code="classic", size=size, stock=5, is_enabled=True)

    def test_literal_pair_imperative_adopts_two_lines_and_three_units_through_native_offer(self):
        from management.services import ig_revision_checkout as checkout
        from management.models import IgCheckoutProposal, IgCheckoutAccessToken
        self.positive_stock()
        self.prior_pair()
        rows = self.seal(["Так, оформлюй обидві"])
        self.parsed = {"reply_text": "Чудово! Для оформлення замовлення перейдіть, будь ласка, на нашу персональну сторінку оформлення TwoComms.",
            "controls": [{"kind": "paylink", "value": "full"}]}
        with (patch.object(checkout, "create_or_update_proposal", wraps=checkout.create_or_update_proposal) as factory,
            patch.object(checkout, "prepare_revision_checkout", wraps=checkout.prepare_revision_checkout) as prepare):
            result, _, http = live_fixture.RevisionLiveTests._execute(self)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factory.call_count, prepare.call_count, http.call_count), (1, 1, 1))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 1))
        decision = IgCommerceTurnDecision.objects.get(source_message=rows[0])
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.request_payload["purchase_reference_count"], 2)
        self.assertTrue(decision.result_payload["source_facts"]["full_cart_purchase_requested"])
        self.assertEqual(len(decision.result_payload["source_facts"]["line_ids"]), 2)
        self.assertEqual(sorted(IgCheckoutProposal.objects.get().items.values_list("quantity", flat=True)), [1, 2])
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")

    def test_pair_reference_cannot_adopt_three_positions(self):
        self.positive_stock()
        self.prior_pair()
        self.sequence += 1
        third = self._message("Сестрі одну чорну футболку QA Trade Футболка, розмір L, крій classic.", "third-person")
        third.provider_created_at = timezone.now() - timedelta(seconds=1)
        third.save(update_fields=["provider_created_at"])
        with transaction.atomic():
            reduced = reduce_inbound_commerce_source(self.customer, third, expected_provider_namespace=third.provider_namespace)
        self.assertTrue(reduced.ready, reduced.reason)
        self.assertEqual(len(capture_current_selection_lines(self.customer.pk)["lines"]), 3)
        rows = self.seal(["Так, оформлюй обидві"])
        from management.services import ig_revision_checkout as checkout
        from management.models import IgCheckoutAccessToken
        self.parsed = {"reply_text": "Перейдіть на сторінку оформлення.",
            "controls": [{"kind": "paylink", "value": "full"}]}
        with patch.object(checkout, "create_or_update_proposal", wraps=checkout.create_or_update_proposal) as factory:
            result, _, _ = live_fixture.RevisionLiveTests._execute(self)
        self.assertNotEqual(result.state, "completed", result.reasons)
        factory.assert_not_called()
        self.assertFalse(self.customer.checkout_proposals.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        source = IgCommerceTurnDecision.objects.get(source_message=rows[0])
        self.assertFalse(source.accepted)
        self.assertEqual(source.transition.action, "line_clarification_required")
        self.assertFalse(source.result_payload["source_facts"].get("full_cart_purchase_requested", False))

    def test_pair_reference_does_not_clear_unresolved_recipient_or_rewrite_units(self):
        self.deliver_clarification()
        self.positive_stock()
        self.seal(["Так, оформлюй обидві"])
        from management.services import ig_revision_checkout as checkout
        from management.models import IgCheckoutAccessToken
        self.parsed = {"reply_text": "Перейдіть на сторінку оформлення.",
            "controls": [{"kind": "paylink", "value": "full"}]}
        with patch.object(checkout, "create_or_update_proposal", wraps=checkout.create_or_update_proposal) as factory:
            result, _, _ = live_fixture.RevisionLiveTests._execute(self)
        self.assertNotEqual(result.state, "completed", result.reasons)
        factory.assert_not_called()
        self.assertFalse(self.customer.checkout_proposals.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.assert_original_choices()

    def test_recipient_purpose_projection_does_no_catalog_queries(self):
        from management.services.ig_turn_intent import _selection_signal, _purpose
        with self.assertNumQueries(0):
            self.assertTrue(_selection_signal("Для хлопця розмір M"))
            self.assertIsNone(_selection_signal("Дякую"))
            self.assertEqual(_purpose("Так, оформлюй обидві"), "retail")


class CandidateSourceGrammarTests(SimpleTestCase):
    def test_line_question_modifier_is_bounded_and_requires_pending_context(self):
        from management.services.ig_response_cart_plan import cart_recipient_question
        for question in (V5_LINE_CLARIFICATION, "Для какой именно позиции изменить размер на M?"):
            with self.subTest(question=question):
                self.assertTrue(cart_recipient_question({"clarification": True}, question))
                self.assertFalse(cart_recipient_question({}, question))
        for question in ("Для якої довільно обраної позиції змінити розмір?",
                "Для якої саме позиції змінити розмір? Який колір?", "Для якої саме позиції змінити розмір."):
            self.assertFalse(cart_recipient_question({"clarification": True}, question))

    def test_recipient_configure_is_consultation_not_purchase(self):
        from management.services.ig_turn_intent import _selection_signal
        from management.services.ig_commerce_turns import parse_turn
        for text in ("Для хлопця розмір M", "Хлопцю залишаємо L, мені M", "Парню оставляем L. Мне M."):
            with self.subTest(text=text):
                self.assertTrue(_selection_signal(text))
                self.assertFalse(parse_turn(text).purchase_requested)

    def test_quotes_reported_questions_negation_trade_and_service_do_not_grant_recipient_selection(self):
        from management.services.ig_turn_intent import _selection_signal
        for text in ('«Для хлопця розмір M»', "Він сказав для хлопця розмір M", "Для хлопця розмір M?",
                "Не хочу для хлопця розмір M", "Для хлопця оптом розмір M", "Пошкоджена футболка. Для хлопця розмір M",
                "Якщо для хлопця розмір M", "Для хлопця розмір M. Не підбирайте більше нічого.",
                "Для хлопця розмір M. Не хочу оформлювати."):
            with self.subTest(text=text):
                self.assertIsNot(_selection_signal(text), True)

    def test_pair_imperative_is_bounded_own_source_and_unused_payload_is_stable(self):
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_commerce_state import _request_payload
        for text in ("Так, оформлюй обидві", "Да, оформляй обе", "Yes, order both items"):
            request = parse_turn(text)
            self.assertTrue(request.full_cart_purchase_requested, text)
            self.assertEqual(request.purchase_reference_count, 2)
            self.assertFalse(request.field_updates)
        for text in ('«Так, оформлюй обидві»', "Так, оформлюй обидві?", "Не оформлюй обидві", "Якщо оформлюй обидві",
                "Він сказав оформлюй обидві", "Оформлюй обидві футболки", "Buy both t-shirts"):
            self.assertFalse(parse_turn(text).full_cart_purchase_requested, text)
        self.assertNotIn("purchase_reference_count", _request_payload(parse_turn("Для хлопця розмір M")))
