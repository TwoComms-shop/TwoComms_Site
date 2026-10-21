"""Direct support work offers keep a reviewed, source-bound staff/service path."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.services.bot_sales_classifier import extract_collaboration_brief, is_creator_collaboration_offer
from management.services.ig_business_cooperation import (
    business_cooperation_for_sources, recruitment_service_for_sources, recruitment_response_reason,
)
from management.services.ig_revision_intents import collaboration_brief_for_revision, manager_case_reason
from management.services.ig_response_control import parse_structured_response


DIRECT_WORK = (
    "Добрий день! Хотів би запропонувати вам свою допомогу з Instagram Direct. "
    "Можу відповідати на повідомлення клієнтів, консультувати щодо розмірів, наявності товарів, цін і доставки. "
    "Мені цікавий ваш бренд, тому хотів би спробувати себе в цій справі. "
    "Підкажіть, будь ласка, чи потрібна вам зараз така допомога та які умови співпраці? Дякую!"
)
DIRECT_ONLY = "Можу відповідати на повідомлення клієнтів вашого бренду в Instagram Direct."


def sources(*texts):
    return [{"message_id": index + 1, "role": "user", "text": text, "source_digest": "a" * 64}
        for index, text in enumerate(texts)]


def revision(*texts):
    return SimpleNamespace(pk=901, client=SimpleNamespace(intent=""), snapshot_digest="recruitment-fixture",
        bundle_snapshot={"sources": sources(*texts)}, generation_proposal={})


class RecruitmentSourceTests(SimpleTestCase):
    def test_own_offers_in_ukrainian_russian_and_english_are_not_creator_or_resale(self):
        for text in (DIRECT_WORK, DIRECT_ONLY,
                "Хочу работать оператором Instagram Direct. Могу отвечать на сообщения ваших клиентов.",
                "I can answer your customers' messages in Instagram Direct.",
                "I would like to work in customer support for your brand."):
            with self.subTest(text=text):
                work = recruitment_service_for_sources(sources(text))
                self.assertTrue(work["employment_service_offer"])
                self.assertEqual(work["hiring_status"], "unknown")
                self.assertEqual(business_cooperation_for_sources(sources(text)), {})
                self.assertFalse(is_creator_collaboration_offer(text))
                brief = extract_collaboration_brief(text)
                self.assertEqual(brief["primary_subtype"], "customer_support_service")
                self.assertNotIn("creator", brief["subtypes"])
                self.assertEqual(manager_case_reason(revision(text)), "collaboration_review")
                native = collaboration_brief_for_revision(revision(text))
                self.assertEqual(native["cooperation_kind"], "recruitment_service")
                self.assertEqual(native["recruitment_service"]["evidence_message_ids"], [1])

    def test_quotes_reports_negation_history_and_non_user_sources_do_not_offer_work(self):
        for text in ('«Можу відповідати на повідомлення ваших клієнтів у Direct» — цитата.',
                '> I can answer your customers in Direct',
                'My friend said: I can answer your customers in Direct.',
                'Раніше міг запропонувати допомогу з Instagram Direct.',
                'I am not offering customer support for your brand.',
                'Я не пропоную допомогу з Instagram Direct.',
                'Я не можу відповідати на повідомлення ваших клієнтів.',
                'Не могу отвечать на сообщения ваших клиентов.',
                "I can't answer your customers' messages in Direct.",
                "I can not answer your customers' messages in Direct.",
                'Хочу футболку, чи може ваш оператор допомогти в Direct?',
                'Please connect me to a customer support manager.',
                'Я фотограф, можу зняти фото для вашого бренду. Напишіть у Direct.'):
            with self.subTest(text=text):
                self.assertEqual(recruitment_service_for_sources(sources(text)), {})
        for role in ("model", "manager"):
            self.assertEqual(recruitment_service_for_sources([{**sources(DIRECT_WORK)[0], "role": role}]), {})

    def test_withdrawal_clears_work_offer_but_preserves_new_retail_question(self):
        for withdrawal in ("Не хочу пропонувати допомогу з Direct", "I no longer want to help with Direct", "Never mind"):
            with self.subTest(withdrawal=withdrawal):
                self.assertEqual(recruitment_service_for_sources(sources(DIRECT_ONLY, withdrawal, "Дякую")), {})
        value = revision(DIRECT_WORK, "Для себе також хочу купити худі L — яка ціна?")
        brief = collaboration_brief_for_revision(value)
        self.assertEqual(brief["cooperation_kind"], "recruitment_service")
        self.assertEqual(value.bundle_snapshot["sources"][1]["text"], "Для себе також хочу купити худі L — яка ціна?")

    def test_independent_human_request_and_real_creator_path_remain_available(self):
        for text in ("Покличте менеджера, будь ласка.", "Позовите менеджера.", "Please connect me to a manager."):
            with self.subTest(text=text):
                self.assertEqual(manager_case_reason(revision(text)), "customer_manager_request")
        creator = "Я фотограф, можу зняти фото та відео для вашого бренду."
        response = parse_structured_response({"reply_text": "Please share your photo portfolio.", "controls": []})
        self.assertTrue(is_creator_collaboration_offer(creator))
        self.assertEqual(recruitment_response_reason(sources(creator), response), "")
        mixed_creator = "I am a photographer, I can shoot photos for your brand and answer your customers' messages in Direct."
        self.assertTrue(is_creator_collaboration_offer(mixed_creator))
        self.assertEqual(recruitment_response_reason(sources(mixed_creator), response), "")

    def test_support_offer_blocks_creator_requirements_but_allows_neutral_experience_question(self):
        for text in ("Надішліть портфоліо та результати creator-проєктів.",
                "Пришлите примеры фото-работ.", "Please share your portfolio and Reels."):
            response = parse_structured_response({"reply_text": text, "controls": []})
            self.assertEqual(recruitment_response_reason(sources(DIRECT_WORK), response), "recruitment_creator_requirements")
        for text in ("Дякуємо за пропозицію підтримки Direct. Передам її команді для розгляду.",
                "Please share your experience in customer support.", "You do not need to send a portfolio."):
            response = parse_structured_response({"reply_text": text, "controls": []})
            self.assertEqual(recruitment_response_reason(sources(DIRECT_WORK), response), "")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeRecruitmentTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle

    def _generate(self, payload, **kwargs):
        expected = getattr(self, "expected_rejection", "")
        if expected:
            from management.services.call_ai_analysis import CallAIAnalysisError
            decision = kwargs["result_validator"](self.parsed,
                usage={"_request_inline_count": 0, "_request_inline_content_hashes": []})
            self.assertFalse(decision.valid)
            self.assertIn(expected, decision.reason_codes)
            self.observed_rejection = decision.reason_codes
            raise CallAIAnalysisError("expected source-scoped rejection")
        return fixtures.RevisionLiveTests._generate(self, payload, **kwargs)

    def test_native_reply_and_manager_task_retain_support_scope_without_portfolio_or_hiring_claim(self):
        from management.models import IgBotNotification, IgFollowUpTask
        self._replace_bundle([DIRECT_WORK])
        self.parsed = {"reply_text": "Дякуємо за пропозицію допомоги з Instagram Direct! Передам її команді для розгляду потреби та умов.",
            "controls": [{"kind": "manager", "value": True}]}
        outcome, model, http = self._execute()
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(http.call_count, 1)
        task = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        self.assertEqual(task.manager_context["cooperation_kind"], "recruitment_service")
        self.assertEqual(task.manager_context["required_decisions"], ["employment_service_review", "customer_support_scope", "work_conditions"])
        self.assertNotIn("portfolio_review", task.manager_context["required_decisions"])
        self.assertEqual(task.manager_context["sources"][0]["message_id"], self.source.pk)
        self.assertFalse(task.manager_context["recruitment_service"]["work_conditions_confirmed"])
        self.assertEqual(IgBotNotification.objects.get(dedupe_key=f"ig-revision-case:{task.pk}").status, "pending")
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")

    def test_native_creator_portfolio_request_is_rejected_for_support_work(self):
        self._replace_bundle([DIRECT_WORK])
        self.parsed = {"reply_text": "Надішліть, будь ласка, портфоліо та приклади фото-робіт.",
            "controls": [{"kind": "manager", "value": True}]}
        self.expected_rejection = "recruitment_creator_requirements"
        outcome, _, http = self._execute()
        self.assertEqual(outcome.state, "blocked")
        self.assertIn("recruitment_creator_requirements", outcome.reasons)
        http.assert_not_called()

    def test_native_hiring_promise_is_rejected_as_unverified(self):
        self._replace_bundle([DIRECT_WORK])
        self.parsed = {"reply_text": "We are hiring. Please join our team!", "controls": [{"kind": "manager", "value": True}]}
        self.expected_rejection = "unverified_recruitment"
        outcome, _, http = self._execute()
        self.assertEqual(outcome.state, "blocked")
        self.assertIn("unverified_recruitment", self.observed_rejection)
        http.assert_not_called()

    @override_settings(IG_REVISION_OPTIONAL_HOLDING_ENABLED=True)
    def test_provider_outage_keeps_support_scope_in_actual_sent_holding_and_replay(self):
        from management.models import IgFollowUpTask
        self._replace_bundle([DIRECT_WORK])
        self.provider_failure = True
        outcome, _, http = self._execute()
        self.assertEqual(http.call_count, 1, outcome.reasons)
        task = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        self.assertEqual(task.manager_context["cooperation_kind"], "recruitment_service")
        self.assertEqual(task.manager_context["required_decisions"], ["employment_service_review", "customer_support_scope", "work_conditions"])
        self.assertIn("Direct", task.message_text)
        self.assertNotRegex(task.message_text.casefold(), r"портфол|creator|фото")
        self.assertEqual(task.manager_context["sources"][0]["message_id"], self.source.pk)
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "sent")
        text = effect.payload["message"]["text"]
        self.assertIn("підтримкою клієнтів", text)
        self.assertNotRegex(text.casefold(), r"портфол|фото|відео|зв.яж|creator")
        again, model, replay_http = self._execute()
        model.assert_not_called()
        replay_http.assert_not_called()
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:collaboration_review").count(), 1)
        self.assertEqual(self.revision.delivery_effects.count(), 1)

    @override_settings(IG_TURN_CONTEXT_MODE="unified")
    def test_offer_without_collaboration_keyword_reaches_actual_compiled_required_guidance(self):
        from management.knowledge.scenarios import reviewed_scenario_values
        from management.services.ig_policy_publication import draft_state, save_instruction_draft
        from management.tests_ig_policy_helpers import publish_current_instructions
        self.source.provider_created_at = timezone.now()
        self.source.save(update_fields=["provider_created_at"])
        state = draft_state()
        values = reviewed_scenario_values("collaboration")
        instruction, _ = save_instruction_draft(expected_revision=state.revision,
            expected_snapshot_hash=state.snapshot_hash, values=values, note="Disposable support-work policy test")
        self.publication = publish_current_instructions()
        self.settings.refresh_from_db()
        self._replace_bundle([DIRECT_ONLY])
        self.parsed = {"reply_text": "Дякуємо за пропозицію допомоги з Direct. Передам її команді для розгляду.",
            "controls": [{"kind": "manager", "value": True}]}
        outcome, _, http = self._execute()
        self.assertEqual(http.call_count, 1, outcome.reasons)
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        import json
        payload = json.dumps(self.generation_calls[0], ensure_ascii=False)
        self.assertIn(values["body"], payload)
        self.assertIn("customer_support_service", payload)
        from management.models import GeminiRequest
        manifest = GeminiRequest.objects.get().policy_manifest
        self.assertIn(f"instruction:{instruction.pk}", manifest["instruction_selection"]["required_policy"]["required_ids"])
