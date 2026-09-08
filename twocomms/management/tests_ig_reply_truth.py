from decimal import Decimal

from django.test import SimpleTestCase

from management.services.ig_reply_truth import (
    AuthorizedAction,
    ProposedAction,
    REASON_CODES,
    ReplyTruthContext,
    validate_reply_truth,
)


class ReplyTruthValidatorTests(SimpleTestCase):
    def test_recruitment_availability_requires_explicit_matching_authority(self):
        statements = {
            "closed": (
                "Наразі ми не шукаємо менеджерів у команду, але якщо щось зміниться, обов’язково повідомимо в соцмережах.",
                "Немає відкритих вакансій.", "Вакансій немає.",
                "Сейчас у нас нет вакансий.", "Мы не ищем менеджеров.",
                "Вакансии закрыты.", "We are not hiring.", "We're not hiring.",
                "We aren't currently hiring.", "There are no open positions.",
            ),
            "open": (
                "Ми шукаємо менеджерів.", "У нас є відкриті вакансії.",
                "Вакансії відкриті.", "Мы ищем менеджеров.",
                "Есть открытые вакансии.", "We are hiring.", "We're currently hiring.",
                "We have open positions.", "Job vacancies are available.",
            ),
        }
        for status, replies in statements.items():
            for reply in replies:
                with self.subTest(status=status, reply=reply):
                    self.assertReason("unverified_recruitment", reply)
                    self.assertValid(reply, context=ReplyTruthContext(recruitment_status=status))
                    opposite = "open" if status == "closed" else "closed"
                    self.assertReason("unverified_recruitment", reply, context=ReplyTruthContext(recruitment_status=opposite))

    def test_recruitment_uncertainty_questions_and_acknowledgements_remain_allowed(self):
        for reply in (
            "Не маю підтвердженої інформації про вакансії.",
            "Не можу підтвердити, що у нас є вакансії.",
            "Не знаю, чи ми шукаємо менеджерів.",
            "У меня нет подтверждённой информации о вакансиях.",
            "Не могу подтвердить, что вакансий нет.",
            "I do not have confirmed recruitment information.",
            "I cannot confirm whether we are hiring.",
            "I don't know if there are open positions.",
            "Яка робота вас цікавить?", "Вас цікавить, чи ми шукаємо менеджерів?",
            "Вы спрашиваете, есть ли вакансии?", "Are you asking whether we are hiring?",
            "Дякую за інтерес до роботи в TwoComms.",
            "Робота з людьми може бути цікавою.", "Working with people can be rewarding.",
            "Будь ласка, гарного дня!",
        ):
            with self.subTest(reply=reply):
                self.assertValid(reply)

    def test_recruitment_uncertainty_or_question_does_not_hide_another_assertion(self):
        for reply in (
            "Не маю підтвердженої інформації, але вакансій немає.",
            "Не можу підтвердити оплату, у нас немає вакансій.",
            "Не знаю, чи є вакансії, але ми не шукаємо менеджерів.",
            "Не могу подтвердить, но вакансии закрыты.",
            "I cannot confirm whether we are hiring, but we have open positions.",
            "Вакансій немає, яка робота вас цікавить?",
            "We are not hiring. What role interests you?",
        ):
            with self.subTest(reply=reply):
                self.assertReason("unverified_recruitment", reply)

    def test_only_source_matched_recruitment_quotes_are_exempt(self):
        quote = "ми не шукаємо менеджерів"
        reply = "Ви написали: «" + quote + "»."
        self.assertValid(reply, context=ReplyTruthContext(quoted_data=(quote,)))
        self.assertReason("unverified_recruitment", reply)
        self.assertReason("unverified_recruitment", reply + " Але вакансії відкриті.", context=ReplyTruthContext(quoted_data=(quote,)))

    def test_invalid_recruitment_context_does_not_authorize_any_status(self):
        self.assertReason("unverified_recruitment", "We are hiring.", context=ReplyTruthContext(recruitment_status="model_said_open"))

    def assertValid(self, text, *, context=None, actions=()):
        result = validate_reply_truth(
            text,
            context=context or ReplyTruthContext(),
            actions=actions,
        )
        self.assertTrue(result.valid, result.reasons)
        self.assertEqual(result.reasons, ())

    def assertReason(self, reason, text, *, context=None, actions=()):
        result = validate_reply_truth(
            text,
            context=context or ReplyTruthContext(),
            actions=actions,
        )
        self.assertFalse(result.valid)
        self.assertIn(reason, result.reasons)
        self.assertTrue(set(result.reasons) <= set(REASON_CODES))

    def test_authorized_multiline_prices_range_configuration_url_and_action(self):
        context = ReplyTruthContext(
            authorized_prices=(Decimal("900"), Decimal("1100")),
            authorized_price_ranges=((Decimal("800"), Decimal("1200")),),
            authorized_urls=(
                "https://twocomms.shop/catalog/",
                "https://tracking.example/known",
            ),
            authorized_discount_percents=(Decimal("10"),),
            authorized_discount_amounts=(Decimal("100"),),
            payment_confirmed=True,
            order_created=True,
            shipment_state="shipped",
            known_tracking_refs=("12345678901234",),
            approved_timing_claims=("1-3 дні",),
            allowed_sizes=("M",),
            allowed_fits=("oversize",),
            allowed_colors=("чорний",),
            authorized_actions=(AuthorizedAction("paylink", "full"),),
        )
        text = (
            "Доступний діапазон від 800 до 1200 грн.\n"
            "Перший варіант 900 грн, другий 1100 грн. "
            "Знижка 10% або 100 грн уже підтверджена. "
            "Розмір M, крій oversize, колір чорний. "
            "Оплату підтверджено, замовлення створено й відправлено. "
            "ТТН 12345678901234. Доставка 1-3 дні. "
            "Каталог: https://twocomms.shop/catalog/. "
            "Трекінг: https://tracking.example/known"
        )
        self.assertValid(
            text,
            context=context,
            actions=(ProposedAction("paylink", "full"),),
        )
        self.assertValid(
            "Точна ціна 900,50 грн.",
            context=ReplyTruthContext(
                authorized_prices=(Decimal("900.50"),),
            ),
        )

    def test_questions_negations_quotes_measurements_and_receipt_are_not_claims(self):
        quote = "оплата підтверджена, сума 900 USD"
        harmless = (
            "Який бюджет: 900 грн? Ціна не 900 грн. "
            "Оплату ще не підтверджено. Замовлення не створене. "
            "Оплата не була підтверджена. Payment has not been received. "
            "Посилку ще не відправлено. Знижки 10% не буде. "
            "Прати при 30°C, склад 100% cotton, довжина 72 см. "
            "Чек отримала, дякую. Клієнт написав: «" + quote + "»"
        )
        self.assertValid(
            harmless,
            context=ReplyTruthContext(quoted_data=(quote,)),
        )

    def test_ordinary_selection_without_protected_claims_is_valid(self):
        self.assertValid(
            "Підкажи, будь ласка, який колір подобається більше: чорний чи білий?"
        )
        self.assertValid("Ваш номер звернення 12345. Можемо продовжити вибір принта.")
        self.assertValid("Щільність тканини 190 gsm. Фото отримано. Повідомлення отримано.")
        self.assertValid("Колір залежить від освітлення. Size depends on fit.")
        self.assertValid("Колір футболки залежить від освітлення.")
        self.assertValid("The size chart is on the product page.")
        self.assertValid("Колір на фото може трохи відрізнятися.")

    def test_explicit_selected_configuration_still_requires_exact_authority(self):
        context = ReplyTruthContext(
            allowed_sizes=("M",),
            allowed_colors=("чорний",),
        )
        self.assertReason(
            "configuration_mismatch",
            "Обраний розмір XL.",
            context=context,
        )
        self.assertReason(
            "configuration_mismatch",
            "Selected size XL.",
            context=context,
        )
        self.assertReason(
            "configuration_mismatch",
            "Обраний колір червоний.",
            context=context,
        )
        self.assertReason(
            "configuration_mismatch",
            "Колір червоний може вам підійти.",
            context=context,
        )
        self.assertValid("Обраний розмір M.", context=context)
        self.assertValid("Selected color чорний.", context=context)

    def test_unprotected_assertions_return_finite_reason_codes(self):
        cases = (
            ("unauthorized_url", "Ось сторінка https://evil.example/pay"),
            ("unsupported_currency", "Ціна 40 USD."),
            ("unsupported_currency", "Ціна 40 GBP."),
            ("unsupported_currency", "Price $40."),
            ("unsupported_currency", "Price USD40."),
            ("unsupported_currency", "Price €40."),
            ("unverified_price", "Ціна ₴999."),
            ("unverified_price", "Ціна UAH999."),
            ("unverified_price", "Ціна 999 грн."),
            ("unverified_discount", "Для вас знижка 15%."),
            ("unverified_discount", "Знижка становить 200 грн."),
            ("unverified_payment", "Оплату підтверджено."),
            ("unverified_payment", "Paid."),
            ("unverified_payment", "Чек підтверджує оплату."),
            ("unverified_order", "Замовлення створено."),
            ("unverified_shipment", "Посилку відправлено."),
            ("unverified_shipment", "Замовлення доставлено."),
            ("unverified_shipment", "Замовлення готове до відправки."),
            ("unverified_tracking", "ТТН 12345678901234."),
            ("unverified_timing", "Доставка 1-3 дні."),
            ("unverified_timing", "Відправимо завтра."),
            ("unverified_timing", "Відправимо до 12 вересня."),
            ("configuration_mismatch", "Розмір XL."),
        )
        for reason, text in cases:
            with self.subTest(reason=reason, text=text):
                self.assertReason(reason, text)

    def test_positive_claim_is_not_hidden_by_unrelated_negation(self):
        self.assertReason(
            "unverified_payment",
            "Не хвилюйтеся, оплату підтверджено.",
        )
        self.assertReason(
            "unverified_payment",
            "Оплату підтверджено, додати ще щось?",
        )

    def test_quote_allowance_never_removes_matching_unquoted_claim(self):
        self.assertReason(
            "unverified_payment",
            "Оплату підтверджено.",
            context=ReplyTruthContext(quoted_data=("Оплату підтверджено",)),
        )

    def test_received_requires_domain_context_for_shipment(self):
        self.assertValid("Фото отримано.")
        payment = validate_reply_truth(
            "Payment received.",
            context=ReplyTruthContext(),
        )
        self.assertIn("unverified_payment", payment.reasons)
        self.assertNotIn("unverified_shipment", payment.reasons)

    def test_preparing_is_not_ready_to_ship(self):
        preparing = ReplyTruthContext(shipment_state="preparing")
        self.assertValid(
            "Замовлення готується до відправлення.",
            context=preparing,
        )
        self.assertValid(
            "Заказ готовится к отправке.",
            context=preparing,
        )
        self.assertReason(
            "unverified_shipment",
            "Замовлення готове до відправлення.",
            context=preparing,
        )
        self.assertReason(
            "unverified_shipment",
            "Заказ готов к отправке.",
            context=preparing,
        )

    def test_ranges_and_multiple_line_amounts_require_structured_allowance(self):
        self.assertReason(
            "unverified_price",
            "Варіанти від 800 до 1200 грн.",
            context=ReplyTruthContext(
                authorized_price_ranges=((Decimal("900"), Decimal("1200")),)
            ),
        )
        self.assertReason(
            "unverified_price",
            "Перший 900 грн. Другий 1100 грн.",
            context=ReplyTruthContext(authorized_prices=(Decimal("900"),)),
        )

    def test_actions_require_exact_authorized_kind_and_value(self):
        context = ReplyTruthContext(
            authorized_actions=(AuthorizedAction("paylink", "full"),)
        )
        self.assertValid(
            "Можемо перейти до оформлення.",
            context=context,
            actions=(ProposedAction("paylink", "full"),),
        )
        self.assertReason(
            "unauthorized_action",
            "Можемо перейти до оформлення.",
            context=context,
            actions=(ProposedAction("paylink", "prepay"),),
        )

    def test_unknown_shipment_and_timing_become_valid_only_with_exact_evidence(self):
        self.assertValid(
            "Посилку відправлено. Доставка 2 дні. Відправимо завтра.",
            context=ReplyTruthContext(
                shipment_state="shipped",
                approved_timing_claims=("2 дні", "завтра"),
            ),
        )

    def test_qualified_standard_dispatch_window_requires_narrow_authority(self):
        context = ReplyTruthContext(
            explicitly_qualified_standard_dispatch_days=(1, 3),
        )
        self.assertValid(
            "Зазвичай підготовка до відправлення займає 1–3 дні після оплати.",
            context=context,
        )
        self.assertReason(
            "unverified_timing",
            "Гарантуємо: зазвичай відправимо за 1–3 дні після оплати.",
            context=context,
        )
        self.assertReason(
            "unverified_timing",
            "Зазвичай доставка Новою поштою займає 1–3 дні після оплати.",
            context=context,
        )

    def test_quoted_unauthorized_url_is_still_blocked(self):
        quote = "Клієнт надіслав https://evil.example/pay"
        self.assertReason(
            "unauthorized_url",
            quote,
            context=ReplyTruthContext(quoted_data=(quote,)),
        )
