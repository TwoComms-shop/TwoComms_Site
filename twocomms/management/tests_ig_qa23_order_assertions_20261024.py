"""Order-purpose prose cannot mint or require a completed-order fact."""
from django.test import SimpleTestCase

from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.services.ig_response_cart_plan import scoped_claim_clauses


class OrderAssertionTests(SimpleTestCase):
    # Verbatim reply_text from V4 run4000bb2d21f84a1dabc4552bbab2f12d,
    # ready_two_person_cart_confirmed_offer. This test grants no paylink right.
    saved_raw_cta = (
        "Чудово! Для оформлення замовлення перейдіть, будь ласка, "
        "на нашу персональну сторінку оформлення TwoComms."
    )

    def assert_order_authority_required(self, reply):
        denied = validate_reply_truth(reply, context=ReplyTruthContext(order_created=False))
        self.assertIn("unverified_order", denied.reasons, reply)
        allowed = validate_reply_truth(reply, context=ReplyTruthContext(order_created=True))
        self.assertTrue(allowed.valid, (reply, allowed.reasons))

    def test_actual_saved_raw_cta_and_each_native_clause_are_not_completed_order(self):
        clauses = tuple(scoped_claim_clauses(self.saved_raw_cta))
        self.assertIn("Для оформлення замовлення перейдіть", clauses)
        for order_created in (False, True):
            for reply in (self.saved_raw_cta, *clauses):
                with self.subTest(order_created=order_created, reply=reply):
                    result = validate_reply_truth(reply, context=ReplyTruthContext(order_created=order_created))
                    self.assertTrue(result.valid, result.reasons)

    def test_purpose_nouns_and_case_endings_do_not_claim_order_creation(self):
        for reply in (
            "Для оформлення замовлення перейдіть на сторінку.",
            "Можемо перейти до оформлення замовлення.",
            "Під час оформлення замовлення перевірте деталі.",
            "Для оформления заказа перейдите на страницу.",
            "Перейдём к оформлению заказа.",
            "При оформлении заказа проверьте детали.",
            "To place your order, continue to checkout.",
            "For order placement, please continue to checkout.",
        ):
            for order_created in (False, True):
                with self.subTest(reply=reply, order_created=order_created):
                    result = validate_reply_truth(reply, context=ReplyTruthContext(order_created=order_created))
                    self.assertTrue(result.valid, result.reasons)

    def test_completed_created_and_confirmed_states_still_require_authority(self):
        for reply in (
            "Замовлення оформлено.", "Ваше замовлення оформлене.",
            "Оформлений заказ.", "Оформлене замовлення.",
            "Замовлення створено.", "Створене замовлення.",
            "Заказ оформлен.", "Ваш заказ оформленный.",
            "Оформленная заявка на заказ.", "Заказ создан.",
            "Your order is placed.", "Your order has been created.",
            "We confirmed your order.",
        ):
            with self.subTest(reply=reply):
                self.assert_order_authority_required(reply)

    def test_nominal_completion_remains_an_order_assertion(self):
        for reply in (
            "Оформлення замовлення завершено.",
            "Оформлення цього замовлення завершено.",
            "Завершено оформлення замовлення.",
            "Оформлення вашого замовлення закінчене.",
            "Оформлення замовлення підтверджено.",
            "Оформление заказа завершено.",
            "Оформление этого заказа завершено.",
            "Оформление заказа подтверждено.",
            "Завершено оформление вашего заказа.",
            "Order placement is complete.",
            "Your order registration has been completed.",
        ):
            with self.subTest(reply=reply):
                self.assert_order_authority_required(reply)

    def test_cta_does_not_hide_an_independent_completed_order_claim(self):
        for assertion in (
            "Замовлення оформлено.", "Заказ уже оформлен.",
            "Your order has been created.", "Оформлення замовлення завершено.",
        ):
            reply = self.saved_raw_cta + " " + assertion
            with self.subTest(assertion=assertion):
                self.assert_order_authority_required(reply)

    def test_negated_nominal_completion_is_not_an_order_assertion(self):
        for reply in (
            "Оформлення замовлення не завершено.",
            "Оформлення цього замовлення не завершено.",
            "Оформление заказа ещё не завершено.",
            "Оформление этого заказа ещё не завершено.",
            "Оформлення цього замовлення завершено?",
            "Оформление этого заказа завершено?",
            "Your order registration has not been completed.",
        ):
            with self.subTest(reply=reply):
                result = validate_reply_truth(reply, context=ReplyTruthContext(order_created=False))
                self.assertTrue(result.valid, result.reasons)
                self.assert_order_authority_required(reply + " Замовлення оформлено.")

    def test_first_negative_nominal_completion_cannot_hide_next_positive(self):
        for negative, positive, conjunction in (
            ("Оформлення першого замовлення не завершено", "оформлення другого замовлення завершено", "а"),
            ("Оформление первого заказа не завершено", "оформление второго заказа завершено", "но"),
        ):
            for separator in (", ", "; ", " "):
                with self.subTest(negative=negative, separator=separator):
                    self.assert_order_authority_required(negative + separator + conjunction + " " + positive + ".")
            for suffix in (".", "?"):
                with self.subTest(negative_only=negative, suffix=suffix):
                    result = validate_reply_truth(negative + suffix, context=ReplyTruthContext(order_created=False))
                    self.assertTrue(result.valid, result.reasons)

    def test_nearest_order_subject_preserves_a_later_unmodified_positive(self):
        for reply in (
            "Оформлення замовлення не завершено а оформлення замовлення завершено.",
            "Оформление заказа не завершено но оформление заказа завершено.",
            "Не завершено оформлення першого замовлення, а завершено оформлення другого замовлення.",
            "Не завершено оформление первого заказа но завершено оформление второго заказа.",
        ):
            with self.subTest(reply=reply):
                self.assert_order_authority_required(reply)
