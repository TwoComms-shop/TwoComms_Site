"""W3 / IMP-017 — вісім підтверджених конфліктів паттернів (F-PAT-001).

Спільна причина всіх восьми: класифікація побудована як каскад `if/elif` із
першим `return`, плюс кілька незалежних `if`-блоків заперечень. Пріоритет
зашитий у порядок рядків, а не виражений явно, тому додавання паттерна
непередбачувано змінює результат для сусіднього.

Кожен тест нижче — дословний приклад із реєстру знахідок, який зараз дає
неправильну класифікацію.
"""
from django.test import TestCase

from management.ig_bot_models import IgClient, IgConversationSignal
from management.models import InstagramBotMessage


class PatternConflictMixin:
    def _classify(self, text, *, key=None, role="user"):
        from management.services.bot_sales_classifier import classify_message

        client = IgClient.get_or_create_for_sender(
            key or f"pattern-{abs(hash(text)) % 10**9}"
        )
        message = InstagramBotMessage.objects.create(
            client=client, role=role, text=text
        )
        result = classify_message(client, message=message)
        client.refresh_from_db()
        return client, result

    def _signal_types(self, client):
        return set(
            client.conversation_signals.values_list("signal_type", flat=True)
        )


class DeliveryPriceConflictTests(PatternConflictMixin, TestCase):
    """#1 «Скільки коштує доставка?» → бот пропонує знижку.

    `DELIVERY_RE` перемагає в `elif`-цепочці, але блок заперечень — незалежний
    `if`, і `PRICE_RE` матчить «скільки». Playbook отримує теги price/discount,
    follow-up ставиться THINKING на 12 годин.
    """

    def test_delivery_cost_question_is_not_a_price_objection(self):
        client, _ = self._classify(
            "Скільки коштує доставка?", key="pattern-delivery-cost"
        )

        self.assertEqual(client.intent, IgClient.Intent.DELIVERY)
        self.assertNotEqual(client.primary_objection, IgClient.Objection.PRICE)

    def test_delivery_cost_question_emits_no_price_objection_signal(self):
        client, _ = self._classify(
            "скільки коштує доставка новою поштою?", key="pattern-delivery-signal"
        )

        self.assertNotIn(
            IgConversationSignal.Type.PRICE_OBJECTION, self._signal_types(client)
        )

    def test_product_price_question_is_intent_not_an_objection(self):
        """IMP-057: питання про ціну не означає, що клієнту дорого."""
        client, _ = self._classify(
            "скільки коштує ця футболка?", key="pattern-product-price"
        )

        self.assertEqual(client.intent, IgClient.Intent.PRICE)
        self.assertNotIn(
            IgConversationSignal.Type.PRICE_OBJECTION, self._signal_types(client)
        )

    def test_expensive_complaint_still_registers_a_price_objection(self):
        client, _ = self._classify("це дорого як для футболки", key="pattern-expensive")

        self.assertEqual(client.primary_objection, IgClient.Objection.PRICE)


class SizeTokenConflictTests(PatternConflictMixin, TestCase):
    """#2 «it's ok» / «m ok» → intent=SIZE, objection=SIZE, сигнал SIZE_CONCERN.

    `SIZE_RE` містить односимвольні альтернативи `s|m|l` із `\\b`, а апостроф —
    не-словний символ, тому «it's» розпадається на «it» і «s».
    """

    def test_agreement_is_not_a_size_question(self):
        client, _ = self._classify("it's ok", key="pattern-its-ok")

        self.assertNotEqual(client.intent, IgClient.Intent.SIZE)
        self.assertNotEqual(client.primary_objection, IgClient.Objection.SIZE)

    def test_agreement_emits_no_size_signal(self):
        client, _ = self._classify("it's ok, thanks", key="pattern-its-ok-thanks")

        self.assertNotIn(
            IgConversationSignal.Type.SIZE_CONCERN, self._signal_types(client)
        )

    def test_bare_letter_without_size_context_is_not_a_size(self):
        client, _ = self._classify("s", key="pattern-bare-letter")

        self.assertNotEqual(client.intent, IgClient.Intent.SIZE)

    def test_size_with_explicit_word_is_still_detected(self):
        """Регрес: явне «розмір L» мусить лишатися питанням про розмір."""
        client, _ = self._classify("який розмір L підійде?", key="pattern-size-word")

        self.assertEqual(client.intent, IgClient.Intent.SIZE)

    def test_short_bare_size_answer_is_still_detected(self):
        """Коротка відповідь «XL» на питання про розмір — валідний розмір."""
        client, _ = self._classify("XL", key="pattern-bare-xl")

        self.assertEqual(client.intent, IgClient.Intent.SIZE)

    def test_size_grid_question_is_still_detected(self):
        client, _ = self._classify("є сітка розмірів?", key="pattern-size-grid")

        self.assertEqual(client.intent, IgClient.Intent.SIZE)


class CustomPrintExchangeConflictTests(PatternConflictMixin, TestCase):
    """#4 «хочу замінити принт на свій» → кейс обміну замість кастому.

    `EXCHANGE_RE` матчить `замін\\w*`, а `CUSTOM_REQUEST_RE` знає «змінити»,
    але не «замінити».
    """

    def test_replacing_a_print_is_a_custom_request(self):
        client, _ = self._classify(
            "хочу замінити принт на свій", key="pattern-custom-replace"
        )

        self.assertEqual(client.intent, IgClient.Intent.CUSTOM_PRINT)

    def test_replacing_a_print_is_not_a_post_sale_exchange(self):
        from management.services.ig_post_sale import detect_post_sale_type

        self.assertEqual(detect_post_sale_type("хочу замінити принт на свій"), "")

    def test_replacing_a_size_is_still_a_post_sale_exchange(self):
        from management.ig_bot_models import IgPostSaleCase
        from management.services.ig_post_sale import detect_post_sale_type

        self.assertEqual(
            detect_post_sale_type("хочу замінити розмір на XL"),
            IgPostSaleCase.CaseType.EXCHANGE,
        )


class ThinkingConflictTests(PatternConflictMixin, TestCase):
    """#5 «думаю візьму L» → objection=THINKING і 12-годинна затримка.

    `THINKING_RE` перетирає заперечення, хоча «візьму» — це рішення купити.
    """

    def test_deciding_to_take_a_size_is_not_hesitation(self):
        client, _ = self._classify("думаю візьму L", key="pattern-thinking-take")

        self.assertNotEqual(client.primary_objection, IgClient.Objection.THINKING)

    def test_real_hesitation_is_still_hesitation(self):
        client, _ = self._classify("подумаю ще, напишу пізніше", key="pattern-thinking-real")

        self.assertEqual(client.primary_objection, IgClient.Objection.THINKING)


class WholesaleCollabConflictTests(PatternConflictMixin, TestCase):
    """#7 «є оптом для магазину? і коллаб цікавить» → оптовий лід губиться.

    `COLLAB_RE` перевіряється раніше `WHOLESALE_RE`, тому клієнт не потрапляє
    у фільтр `wholesale_b2b`.
    """

    def test_wholesale_wins_over_collaboration_when_both_appear(self):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.bot_sales_classifier import _interaction_type

        client = IgClient.get_or_create_for_sender("pattern-wholesale-collab")
        result = _interaction_type(
            client,
            {},
            "є оптом для магазину? і коллаб цікавить",
            InstagramBotMessage.Role.USER,
        )

        self.assertEqual(
            result, IgConversationAnalysisSnapshot.InteractionType.WHOLESALE_B2B
        )

    def test_pure_collaboration_is_still_collaboration(self):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.bot_sales_classifier import _interaction_type

        client = IgClient.get_or_create_for_sender("pattern-pure-collab")
        result = _interaction_type(
            client,
            {},
            "я блогер, цікавить колаборація",
            InstagramBotMessage.Role.USER,
        )

        self.assertEqual(
            result, IgConversationAnalysisSnapshot.InteractionType.COLLABORATION
        )


class PhoneReadinessConflictTests(PatternConflictMixin, TestCase):
    """#8 «мій друг 0501234567 казав…» → +40 readiness і Band.HIGH_INTENT.

    `PHONE_RE` стоїть в одному `elif` із `PAYMENT_RE`, тому будь-який номер
    у тексті робить клієнта гарячим.
    """

    def test_mentioning_someone_elses_phone_is_not_a_payment_intent(self):
        client, _ = self._classify(
            "мій друг 0501234567 казав що у вас класні футболки",
            key="pattern-friend-phone",
        )

        self.assertNotEqual(client.intent, IgClient.Intent.PAYMENT)
        self.assertLess(client.buying_readiness, 40)

    def test_phone_offered_as_contact_data_is_still_a_payment_intent(self):
        """Регрес: телефон як контакт для замовлення — реальний сигнал."""
        client, _ = self._classify(
            "мій номер 0501234567, оформлюйте", key="pattern-contact-phone"
        )

        self.assertEqual(client.intent, IgClient.Intent.PAYMENT)


class PhoneDisclosurePolicyTests(PatternConflictMixin, TestCase):
    def test_post_sale_wording_alone_does_not_authorize_support_phone(self):
        from management.services.bot_sales_classifier import phone_contact_policy

        client = IgClient.get_or_create_for_sender("policy-pre-sale-phone")

        result = phone_contact_policy(
            client,
            "Хочу повернути товар, дайте ваш номер телефону",
            confirmed_purchase=False,
            post_sale_request="return",
        )

        self.assertEqual(result["decision"], "clarify_purpose")

    def test_common_english_business_phone_requests_are_recognized(self):
        from management.services.bot_sales_classifier import phone_contact_policy

        client = IgClient.get_or_create_for_sender("policy-english-phone")
        requests = (
            "Could you give me your phone number?",
            "Can you share a contact number?",
            "Do you have a phone number?",
            "How can I reach you by phone?",
        )

        for text in requests:
            with self.subTest(text=text):
                result = phone_contact_policy(
                    client,
                    text,
                    confirmed_purchase=False,
                )
                self.assertEqual(result["decision"], "clarify_purpose")

    def test_undelivered_order_wording_needs_durable_service_evidence(self):
        from management.services.bot_sales_classifier import phone_contact_policy

        client = IgClient.get_or_create_for_sender("policy-undelivered-order-phone")
        text = "My order has not arrived; could you give me your phone number?"

        unverified = phone_contact_policy(
            client,
            text,
            confirmed_purchase=False,
        )
        verified = phone_contact_policy(
            client,
            text,
            confirmed_purchase=True,
        )

        self.assertEqual(unverified["decision"], "clarify_purpose")
        self.assertEqual(verified["decision"], "support_escalation")

    def test_phone_policy_is_refreshed_on_a_noncommercial_user_turn(self):
        client, result = self._classify(
            "Не хочу купувати, але дайте ваш номер телефону",
            key="policy-noncommercial-phone",
        )

        self.assertEqual(result["phone_contact_policy"]["decision"], "clarify_purpose")
        policy = client.sales_context["_phone_contact_policy"]
        self.assertEqual(set(policy), {
            "schema_version", "decision", "source", "observed_at", "source_message_id",
        })

    def test_manager_approved_service_case_is_an_explicit_phone_authorization(self):
        from management.ig_bot_models import IgPostSaleCase
        from management.services.bot_sales_classifier import phone_contact_policy

        client = IgClient.get_or_create_for_sender("policy-approved-service-phone")
        source = InstagramBotMessage.objects.create(
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="потрібен обмін через дефект",
        )
        IgPostSaleCase.objects.create(
            client=client,
            source_message=source,
            case_type=IgPostSaleCase.CaseType.RETURN,
            status=IgPostSaleCase.Status.APPROVED,
        )

        result = phone_contact_policy(
            client,
            "У товарі брак, дайте ваш номер телефону",
            confirmed_purchase=False,
            post_sale_request=IgPostSaleCase.CaseType.RETURN,
        )

        self.assertEqual(result["decision"], "support_escalation")

    def test_bare_business_phone_request_becomes_a_current_turn_prompt_policy(self):
        from management.services import bot_memory

        client, result = self._classify(
            "Підкажіть, будь ласка, ваш номер телефону",
            key="policy-business-phone-purpose",
        )

        self.assertEqual(
            result["phone_contact_policy"]["decision"], "clarify_purpose"
        )
        self.assertEqual(
            client.sales_context["_phone_contact_policy"]["decision"],
            "clarify_purpose",
        )
        note = bot_memory.client_context_note(client)
        self.assertIn("[ПОЛІТИКА КОНТАКТУ ДЛЯ ЦЬОГО ХОДУ", note)
        self.assertIn("Не повідомляй номер", note)
        self.assertIn("з'ясуй", note)

    def test_collaboration_request_keeps_the_number_private_and_invites_callback_details(self):
        from management.services import bot_memory

        client, result = self._classify(
            "Дайте ваш номер, хочу запропонувати колаборацію",
            key="policy-collaboration-phone",
        )

        self.assertEqual(
            result["phone_contact_policy"]["decision"],
            "collaboration_callback",
        )
        note = bot_memory.client_context_note(client)
        self.assertIn("Не повідомляй номер бренду", note)
        self.assertIn("описати пропозицію", note)
        self.assertIn("зворотного зв'язку", note)

    def test_confirmed_support_issue_allows_only_an_authorized_escalation(self):
        from management.services import bot_memory
        from management.services.bot_sales_classifier import classify_message

        client = IgClient.get_or_create_for_sender("policy-confirmed-support-phone")
        client.has_confirmed_purchase = True
        message = InstagramBotMessage.objects.create(
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="У замовленні брак, дайте ваш номер телефону",
        )

        result = classify_message(client, message=message)
        client.refresh_from_db()

        self.assertEqual(
            result["phone_contact_policy"]["decision"],
            "support_escalation",
        )
        note = bot_memory.client_context_note(client)
        self.assertIn("передай питання менеджеру", note)
        self.assertIn("Не вигадуй", note)

    def test_policy_is_not_reused_after_a_newer_user_turn(self):
        from management.services import bot_memory

        client, _ = self._classify(
            "Підкажіть, будь ласка, ваш номер телефону",
            key="policy-stale-after-newer-turn",
        )
        InstagramBotMessage.objects.create(
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Дякую, зрозуміло",
        )

        self.assertIsNone(bot_memory.client_context_note(client))


class IntentPriorityTableTests(TestCase):
    """Пріоритет мусить бути виражений явно, а не порядком рядків."""

    def test_intent_priority_table_covers_every_candidate_intent(self):
        from management.services.bot_sales_classifier import INTENT_PRIORITY

        self.assertEqual(
            set(INTENT_PRIORITY),
            {
                IgClient.Intent.CUSTOM_PRINT,
                IgClient.Intent.PAYMENT,
                IgClient.Intent.ORDER_STATUS,
                IgClient.Intent.SUPPORT,
                IgClient.Intent.DELIVERY,
                IgClient.Intent.SIZE,
                IgClient.Intent.PRICE,
                IgClient.Intent.PRODUCT,
            },
        )

    def test_priorities_are_unique(self):
        from management.services.bot_sales_classifier import INTENT_PRIORITY

        values = list(INTENT_PRIORITY.values())
        self.assertEqual(len(values), len(set(values)))
