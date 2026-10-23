"""Source-bound recruitment and response-delay regression cases; no live I/O."""
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone as utc

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from management.services.ig_operator_reply_drafts import (
    _private_case_reply_reason, prepare_operator_reply_draft,
)
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.services.ig_revision_conversation_context import (
    normalize_response_delay_apology, response_delay_apology_context,
)
from management import tests_ig_operator_reply_drafts as operator_fixtures
from management import tests_ig_revision_wait_policy as wait_fixtures
from management import tests_ig_revision_live as live_fixtures


class ReplyTruthSeamTests(SimpleTestCase):
    def test_configuration_prepositions_are_grammar_and_preserve_the_real_fit(self):
        context = ReplyTruthContext(allowed_fits=("regular",), source_chosen_fits=("regular",),
            allowed_sizes=("L",), allowed_colors=("black",))
        for text in (
            "You selected regular fit for the «Правил немає» T-shirt. Which size do you need?",
            "The regular fit with these sleeves is comfortable.",
            "The fit of the T-shirt is described on its page.",
            "I can help find a size for your hoodie.",
            "The color of this print needs no selection change.",
        ):
            with self.subTest(text=text):
                result = validate_reply_truth(text, context=context)
                self.assertTrue(result.valid, result.reasons)
        for text in (
            "You selected oversize fit for the T-shirt.",
            "The oversize fit with these sleeves is comfortable.",
            "Fit: for.", "Fit=with.", "Selected fit of the T-shirt.",
            "Size: for.", "Selected size for the hoodie.",
            "Color: of.", "Selected color with the print.",
            "Fit oversize.", "Size XL.", "Color blue.",
        ):
            with self.subTest(text=text):
                self.assertIn("configuration_mismatch", validate_reply_truth(text, context=context).reasons)
        self.assertIn("configuration_mismatch", validate_reply_truth(
            "The regular fit for this T-shirt.", context=ReplyTruthContext()).reasons)

    def test_regular_description_matches_only_the_evidenced_classic_fit(self):
        for actual in ("classic", "regular"):
            context = ReplyTruthContext(allowed_fits=(actual,), source_chosen_fits=(actual,))
            for text in ("You selected regular fit for the T-shirt.",
                         "Fit regular.", "Fit classic."):
                with self.subTest(actual=actual, text=text):
                    result = validate_reply_truth(text, context=context)
                    self.assertTrue(result.valid, result.reasons)
            for text in ("You selected oversize fit for the T-shirt.", "Fit: unknown."):
                with self.subTest(actual=actual, text=text):
                    self.assertIn("configuration_mismatch", validate_reply_truth(text, context=context).reasons)
        self.assertIn("configuration_mismatch", validate_reply_truth(
            "You selected regular fit for the hoodie.",
            context=ReplyTruthContext(allowed_fits=("oversize",), source_chosen_fits=("oversize",))).reasons)

    def test_second_person_acceptance_and_candidate_approval_need_hiring_authority(self):
        for text in ("Ви прийняті на роботу.", "Вы приняты на работу.",
                     "Ваша кандидатура схвалена.", "Ваша кандидатура одобрена.",
                     "You have been accepted for the role.", "Your candidacy is approved."):
            with self.subTest(text=text):
                self.assertIn("unverified_recruitment", validate_reply_truth(
                    text, context=ReplyTruthContext()).reasons)

    def test_acceptance_qualifiers_and_ordinary_nonemployment_approval_remain_valid(self):
        for text in ("Ви не прийняті на роботу.", "Вы не приняты на работу.",
                     "Ваша кандидатура не схвалена.", "Ваша кандидатура не одобрена.",
                     "Your candidacy has not been approved.",
                     "Якщо ви прийняті на роботу, умови погоджує керівництво.",
                     "Якщо ваша кандидатура схвалена, умови погоджує керівництво.",
                     "If you have been accepted for the role, management will discuss the terms.",
                     "Не можу підтвердити, що ви прийняті на роботу.",
                     "Не могу подтвердить, что ваша кандидатура одобрена.",
                     "Ваш макет схвалено.", "Your design is approved."):
            with self.subTest(text=text):
                self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext()).valid)
        for quote in ("Ви прийняті на роботу", "Ваша кандидатура схвалена"):
            with self.subTest(quote=quote):
                text = f"Ви написали: «{quote}»."
                self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext(quoted_data=(quote,))).valid)
                self.assertFalse(validate_reply_truth(text, context=ReplyTruthContext()).valid)

    def test_hiring_decision_is_not_authorized_even_by_an_open_vacancy(self):
        for text in ("Вас прийнято на роботу.", "Ми беремо вас у команду.",
                     "Ви вже наш новий менеджер Direct.", "You are hired.",
                     "Мы принимаем вас на работу.", "We're hiring you.",
                     "Якщо маєте час, вас прийнято на роботу."):
            for status in ("unknown", "open", "closed", "hired"):
                with self.subTest(text=text, status=status):
                    self.assertIn("unverified_recruitment", validate_reply_truth(
                        text, context=ReplyTruthContext(recruitment_status=status)).reasons)

    def test_negation_hypothetical_and_source_matched_quote_are_not_hiring_decisions(self):
        for text in ("Ми не беремо вас у команду без рішення керівництва.",
                     "You are not hired.", "Якщо вас прийнято на роботу, умови погоджує керівництво.",
                     "If you are hired, management will discuss the terms.",
                     "Не можу підтвердити, що вас прийнято на роботу.",
                     "Ми беремо вас до уваги."):
            with self.subTest(text=text):
                self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext()).valid)
        quote = "Вас прийнято на роботу"
        text = f"Ви написали: «{quote}»."
        self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext(quoted_data=(quote,))).valid)
        self.assertFalse(validate_reply_truth(text, context=ReplyTruthContext()).valid)
        self.assertFalse(validate_reply_truth(text + " You are hired.",
            context=ReplyTruthContext(quoted_data=(quote,))).valid)

    def test_short_delay_apology_is_removed_without_losing_useful_answer(self):
        revision = SimpleNamespace(bundle_snapshot={"sources": []}, snapshot_digest="")
        answer = "Уточню матеріал у менеджера."
        for apology in ("Перепрошую за очікування.", "Вибачте за очікування!",
                        "Извините за ожидание.", "Sorry for the wait."):
            with self.subTest(apology=apology):
                self.assertEqual(normalize_response_delay_apology(apology + " " + answer, revision), answer)
                with self.assertRaisesRegex(ValueError, "without_useful_reply"):
                    normalize_response_delay_apology(apology, revision)
        for text in ("Вибачте за помилку з товаром. " + answer,
                     "Вибачте за очікування доставки. " + answer,
                     'Ви написали: «Перепрошую за очікування.» ' + answer,
                     "Розумію ваше занепокоєння. " + answer):
            self.assertEqual(normalize_response_delay_apology(text, revision), text)
        with patch("management.services.ig_revision_conversation_context.response_delay_apology_allowed", return_value=True):
            text = "Перепрошую за очікування. " + answer
            self.assertEqual(normalize_response_delay_apology(text, revision), text)

    def test_conditional_followup_requires_current_source_review_and_review_anchor(self):
        facts = {"delay_apology_allowed": False, "employment": {"employment_service_offer": True},
                 "manager_tasks": [{"id": 23}]}
        review = "Керівництво зараз розглядає вашу пропозицію допомоги з Direct. "
        allowed = review + "Якщо після розгляду буде потрібна допомога, зв'яжемося з вами тут."
        self.assertEqual(_private_case_reply_reason(allowed, facts), "")
        pending = allowed.replace(review, "Ваша пропозиція зараз потребує розгляду керівництва. ")
        self.assertEqual(_private_case_reply_reason(pending, facts), "")
        self.assertTrue(validate_reply_truth(allowed, context=ReplyTruthContext()).valid)
        for text in ("Якщо після розгляду буде потрібна допомога, зв'яжемося з вами.",
                     review + "Якщо колись буде потрібна допомога, зв'яжемося з вами.",
                     review + "Якщо після розгляду відкриється вакансія, зв'яжемося з вами.",
                     allowed.replace("зараз розглядає", "зараз не розглядає"),
                     pending.replace("потребує", "не потребує"),
                     allowed + " Ми збережемо вашу пропозицію у кадровому резерві.",
                     allowed + " Якщо відкриється вакансія, зв'яжемося з вами."):
            with self.subTest(text=text):
                self.assertEqual(_private_case_reply_reason(text, facts), "operator_hiring_followup_unverified")
        self.assertEqual(_private_case_reply_reason(allowed, {**facts, "manager_tasks": []}),
                         "operator_hiring_followup_unverified")


@override_settings(GOOGLE_INDEXING_ENABLED=False, **operator_fixtures.SHADOW)
class PrivateCurrentReviewSeamTests(TestCase):
    setUp = operator_fixtures.OperatorReplyDraftTests.setUp
    kwargs = operator_fixtures.OperatorReplyDraftTests.kwargs
    response = operator_fixtures.OperatorReplyDraftTests.response
    pending_review = operator_fixtures.OperatorReplyDraftTests.pending_review

    def test_current_review_conditional_contact_saves_a_private_draft(self):
        task = self.pending_review()
        self.text = ("Перепрошую за очікування. Дякуємо за пропозицію допомоги з Direct. "
            "Ваша пропозиція зараз потребує розгляду керівництва. "
            "Якщо після розгляду буде потрібна допомога, зв'яжемося з вами тут.")
        with patch("management.services.call_ai_analysis.requests.post", return_value=self.response()) as provider:
            saved = prepare_operator_reply_draft(**self.kwargs(), allow_delay_apology=True)
        self.assertEqual(saved["state"], "saved", saved)
        provider.assert_called_once()
        task.refresh_from_db()
        self.assertNotIn(task.status, ("completed", "cancelled"))
        self.assertFalse(operator_fixtures.HumanReplyCommand.objects.exists())


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class ShortDelayReceiptSeamTests(TransactionTestCase):
    setUp = wait_fixtures.RevisionWaitPolicyTests.setUp
    _message = wait_fixtures.RevisionWaitPolicyTests._message
    _prepare = wait_fixtures.RevisionWaitPolicyTests._prepare
    _issue = wait_fixtures.RevisionWaitPolicyTests._issue
    _effect = wait_fixtures.RevisionWaitPolicyTests._effect
    _related = wait_fixtures.RevisionWaitPolicyTests._related

    def test_verified_current_issue_allows_short_apology_and_its_receipt_suppresses_repeat(self):
        self._issue()
        now = datetime(2026, 10, 5, 13, tzinfo=utc.utc)
        text = "Перепрошую за очікування. Перевірмо оплату з менеджером."
        with patch("management.services.ig_revision_conversation_context.timezone.now", return_value=now):
            self.assertEqual(normalize_response_delay_apology(text, self.revision), text)
        effect = self._effect(state="unknown", text=text)
        self._related()
        for state in ("unknown", "sent"):
            effect.state = state
            effect.save(update_fields=["state"])
            facts = response_delay_apology_context(self.revision, now=now)
            self.assertEqual(facts["reason"], "already_apologized_for_wait")
            self.assertFalse(facts["delay_apology_eligible"])

    def test_short_apology_receipt_on_foreign_recipient_does_not_suppress_current_issue(self):
        self._issue()
        self._effect(state="sent", text="Перепрошую за очікування.", recipient="foreign-client")
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertTrue(facts["delay_apology_eligible"])
        self.assertIsNone(facts["prior_apology_receipt"])


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeHiringAcceptanceSeamTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute
    _replace_bundle = live_fixtures.RevisionLiveTests._replace_bundle

    def _generate(self, payload, **kwargs):
        validator = kwargs["result_validator"]

        def capture_reasons(*args, **validation_kwargs):
            decision = validator(*args, **validation_kwargs)
            self.rejection_reason_codes = decision.reason_codes
            return decision

        kwargs["result_validator"] = capture_reasons
        return live_fixtures.RevisionLiveTests._generate(self, payload, **kwargs)

    def test_second_person_acceptance_cannot_reach_fake_provider(self):
        self._replace_bundle(["Можу відповідати на повідомлення клієнтів вашого бренду в Instagram Direct."])
        self.parsed = {"reply_text": "Дякуємо за пропозицію допомоги з Direct! Ви прийняті на роботу.",
                       "controls": [{"kind": "manager", "value": True}]}
        result, _, http = self._execute()
        self.assertEqual(result.state, "blocked", result.reasons)
        self.assertIn("unverified_recruitment", self.rejection_reason_codes)
        http.assert_not_called()


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE='unified',
    IG_CLIENT_STATE_PROMPT_TOKENS=20000, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeCombinedStockPriceSeamTests(TransactionTestCase):
    """An exact current review binds a short check without inventing stock truth."""
    reset_sequences = True
    from management import tests_ig_restock_subscriptions as restock_fixtures
    from management import tests_ig_standard_funnel_qa as stock_fixtures
    source = restock_fixtures._RestockFixture.source
    canonical = restock_fixtures._RestockFixture.canonical
    _execute = live_fixtures.RevisionLiveTests._execute
    _generate = stock_fixtures.NativeStockContinuationTests._generate
    answer = stock_fixtures.NativeStockContinuationTests.answer
    assert_complete = stock_fixtures.NativeStockContinuationTests.assert_complete

    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        from product_catalog.models import VariantSizeRule
        original = SourceCartCatalogTests.setUp

        def catalog_before_sources(case):
            original(case)
            case.category.name, case.category.slug = 'Худі', 'hoodies'
            case.category.save(update_fields=['name', 'slug'])
            case.products[0].price = case.variants[0].price_override = 900
            case.products[0].save(update_fields=['price'])
            case.variants[0].save(update_fields=['price_override'])
            grid = case.products[0].size_grid
            grid.guide_data['rows'].append({'size': 'XXL', 'display_size': 'XXL'})
            grid.save(update_fields=['guide_data'])
            VariantSizeRule.objects.create(variant=case.variants[0], fit_code='classic',
                size='XXL', stock=100, is_enabled=True)

        with patch.object(SourceCartCatalogTests, 'setUp', catalog_before_sources):
            self.restock_fixtures._RestockFixture.setUp(self)
        self.settings.ai_enabled = True
        self.settings.save(update_fields=['ai_enabled'])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count, self.actual_hash_override = 0, 'observed'
        self.provider_failure = False
        self.canonical('I want Restock Aurora hoodie, quantity 1, size XXL, color black, fit classic')
        from management.services.ig_response_plan import capture_response_plan
        self.before_validate = lambda: setattr(self, 'observed_plan',
            capture_response_plan(self.customer, revision=self.revision))

    def test_natural_combined_question_and_anaphoric_pending_check_send_once(self):
        outcome = self.answer(
            'Is this hoodie in size XXL available, and what is the price?',
            'The Restock Aurora hoodie costs 900 UAH. I will check availability in size XXL with our manager.',
            controls=[{'kind': 'manager', 'value': True}, {'kind': 'price_quoted', 'value': 900}])
        self.assertTrue(self.observed_plan.pending_stock_questions, self.observed_plan.obligations)
        self.assert_complete(*outcome)

    def test_same_natural_answer_to_stock_only_source_already_has_an_owner(self):
        self.assert_complete(*self.answer('Is this hoodie in size XXL available?',
            'The Restock Aurora hoodie costs 900 UAH. I will check availability in size XXL with our manager.',
            controls=[{'kind': 'manager', 'value': True}, {'kind': 'price_quoted', 'value': 900}]))

    def test_normalized_decimal_string_price_control_preserves_native_quote_authority(self):
        controls = [{'kind': 'manager', 'value': True}, {'kind': 'price_quoted', 'value': '900.00'}]
        self.assert_complete(*self.answer(
            'Is this hoodie in size XXL available, and what is the price?',
            'The Restock Aurora hoodie costs 900 UAH. I will check availability in size XXL with our manager.',
            controls=controls))
        self.assertEqual(self.parsed['controls'], controls)

    def test_foreign_or_reported_pending_check_cannot_inherit_current_line(self):
        for check in (
            'I will check availability in size L with our manager.',
            'I will check availability of the T-shirt in size XXL with our manager.',
            'I will check availability of Other Print in size XXL with our manager.',
            'I will check availability of the "Other Print" hoodie in size XXL with our manager.',
            '“I will check availability in size XXL with our manager.”',
            'The manager said: I will check availability in size XXL with our manager.',
            'I will not check availability in size XXL with our manager.',
            'It is available in size XXL.',
        ):
            with self.subTest(check=check):
                result, _, http = self.answer(
                    'Is this hoodie in size XXL available, and what is the price?',
                    'The Restock Aurora hoodie costs 900 UAH. ' + check)
                self.assertNotEqual(result.state, 'completed', result.reasons)
                http.assert_not_called()
                self.assertFalse(self.revision.delivery_effects.exists())

    def test_combined_source_keeps_stock_debt_when_only_known_price_is_answered(self):
        result, _, http = self.answer('Is this hoodie in size XXL available, and what is the price?',
            'The Restock Aurora hoodie costs 900 UAH.')
        self.assertNotEqual(result.state, 'completed', result.reasons)
        response = SimpleNamespace(reply_text=self.parsed['reply_text'], control={'manager': True})
        self.assertTrue(self.observed_plan.coverage(response)['remaining'])
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_current_pending_check_cannot_authorize_a_wrong_price(self):
        result, _, http = self.answer('Is this hoodie in size XXL available, and what is the price?',
            'The Restock Aurora hoodie costs 1000 UAH. I will check availability in size XXL with our manager.')
        self.assertEqual(result.state, 'blocked', result.reasons)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_price_only_does_not_capture_stock_review_or_require_a_check(self):
        outcome = self.answer('What is the price?', 'The Restock Aurora hoodie costs 900 UAH.', controls=[])
        self.assertFalse(self.observed_plan.pending_stock_questions)
        self.assert_complete(*outcome, case=False)

    def test_later_separate_price_source_is_answered_while_prior_review_remains_pending(self):
        self.assert_complete(*self.answer('Is this hoodie in size XXL available?',
            'I will check availability in size XXL with our manager.'))
        outcome = self.answer('What is the price?', 'The Restock Aurora hoodie costs 900 UAH.', controls=[])
        self.assertFalse(self.observed_plan.pending_stock_questions)
        self.assert_complete(*outcome)

    def test_two_real_cart_lines_do_not_share_an_anaphoric_stock_owner(self):
        self.canonical('добавьте Storm, количество 1, размер L, цвет pink, крій classic')
        result, _, http = self.answer(
            'Is the Restock Aurora hoodie in size XXL available, and what is the price?',
            'The Restock Aurora hoodie costs 900 UAH. I will check availability in size XXL with our manager.')
        self.assertEqual(len(self.observed_plan.line_plans), 2)
        self.assertFalse(self.observed_plan.pending_stock_questions)
        self.assertEqual(result.state, 'blocked', result.reasons)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())


class CombinedStockQuestionScopeTests(SimpleTestCase):
    def test_price_adjunct_preserves_exact_own_stock_subject(self):
        from management.services.ig_response_cart_plan import stock_question
        for text in (
            'Is this hoodie in size XXL available, and what is the price?',
            'Чи є це худі в розмірі XXL у наявності та яка його ціна?',
            'Есть ли этот худи в размере XXL в наличии и какая цена?',
            'Есть ли это худи в размере XXL в наличии и какая цена?',
        ):
            with self.subTest(text=text):
                self.assertTrue(stock_question(text, size='XXL'))
                self.assertFalse(stock_question(text, size='M'))
        for text in (
            'What is the price?',
            'Is this Other Print hoodie in size XXL available, and what is the price?',
            'Is this hoodie in size XXL available? Can I collect it in person?',
            '“Is this hoodie in size XXL available, and what is the price?”',
            'The customer asked: Is this hoodie in size XXL available, and what is the price?',
            'Is this hoodie in size XXL available, and what is the price of item 2?',
            'Is this hoodie in size XXL not available, and what is the price?',
        ):
            with self.subTest(text=text):
                self.assertFalse(stock_question(text, size='XXL', title='Restock Aurora'))
