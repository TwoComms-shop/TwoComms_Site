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
