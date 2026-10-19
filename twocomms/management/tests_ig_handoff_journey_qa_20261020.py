"""Native multi-turn handoff journeys, with local model and transport receipts."""
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live
from management import tests_ig_captured_policy_inputs as policy_fixtures
from management.models import (
    IgBotNotification, IgCheckoutProposal, IgCustomerTurn, IgCustomerTurnRevision,
    IgFollowUpTask, IgTurnMessage, InstagramBotMessage,
)
from management.services.ig_response_debt import response_coverage
from management.services.ig_turn_revisions import create_collecting_revision
from management.tests_ig_business_cooperation import SHOP_REQUEST


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class HandoffJourneyQATests(TransactionTestCase):
    reset_sequences = True
    _message = live.RevisionLiveTests._message
    _prepare = live.RevisionLiveTests._prepare
    _generate = live.RevisionLiveTests._generate
    _execute = live.RevisionLiveTests._execute

    def setUp(self):
        guard = patch("management.services.instagram_bot._provider_http",
                      side_effect=AssertionError("journey escaped exact local HTTP receipt"))
        guard.start()
        self.addCleanup(guard.stop)
        live.RevisionLiveTests.setUp(self)
        self.serial = 0

    def bundle(self, *texts):
        self.serial += 1
        now = timezone.now()
        rows = [self._message(text, f"journey-{self.serial}-{index}") for index, text in enumerate(texts)]
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=rows[-1],
            window_started_at=now, window_deadline=now)
        for ordinal, source in enumerate(rows, 1):
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=ordinal, role="user")
        self.source, self.turn = rows[-1], turn
        self.revision = create_collecting_revision(turn, rows, bypass_quiet=True).revision
        self._prepare()
        return rows

    def reply(self, text, *, manager=False, case_reason=None):
        self.parsed = {"reply_text": text, "controls": [{"kind": "manager", "value": True}] if manager else [],
            "turn_intelligence": {"catalog_candidates": [], "transcript": "", "intent": "other", "confidence": 0.9}}
        def provider(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            if case_reason:
                task = IgFollowUpTask.objects.get(reason=case_reason)
                self.assertIn(self.source.pk, [row["message_id"] for row in task.manager_context["sources"]])
                notice = IgBotNotification.objects.get(dedupe_key=f"ig-revision-case:{task.pk}")
                self.assertEqual(notice.status, "pending")
                self.assertEqual(notice.payload["manager_task_id"], task.pk)
            return 200, json.dumps({"message_id": f"journey-sent-{self.revision.pk}"})
        return self._execute(provider)

    def test_shop_five_questions_are_not_completed_by_one_generic_handoff(self):
        sources = self.bundle(SHOP_REQUEST)
        outcome, generation, http = self.reply("Дякую! Зафіксувала ваші запитання про партнерство для команди.", manager=True)
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(http.call_count, 1, outcome)
        task = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        self.assertEqual(task.manager_context["cooperation_kind"], "business")
        self.assertNotIn("portfolio_review", task.manager_context["required_decisions"])
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.revision.refresh_from_db()
        coverage = response_coverage(self.revision)
        self.assertTrue(coverage.get("remaining"), (outcome, coverage, task.manager_context))
        sources[0].refresh_from_db()
        self.assertEqual(sources[0].status, "pending")

    def test_creator_case_uses_native_source_brief_and_replay_does_not_alert_twice(self):
        self.bundle("Я фотограф, хочу знімати фото і відео для вашого бренду. Telegram @creator_local")
        first, generation, http = self.reply("Дякую за пропозицію співпраці. Зафіксувала її для розгляду командою.", manager=True)
        self.assertEqual(http.call_count, 1, first)
        self.assertEqual(generation.call_count, 1)
        task = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        self.assertNotEqual(task.manager_context.get("cooperation_kind"), "business")
        self.assertIn("portfolio_review", task.manager_context["required_decisions"])
        before = IgBotNotification.objects.count()
        _, replay_generation, replay_http = self.reply("Дякую за пропозицію співпраці. Зафіксувала її для розгляду командою.", manager=True)
        replay_generation.assert_not_called()
        replay_http.assert_not_called()
        self.assertEqual(IgBotNotification.objects.count(), before)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:collaboration_review").count(), 1)

    def test_new_retail_question_is_answered_while_business_case_waits(self):
        self.bundle("Хочу відкрити магазин, цікавлять умови співпраці.")
        first, _, first_http = self.reply("Дякую! Зафіксувала ваше питання про партнерство для команди.", manager=True)
        self.assertEqual(first_http.call_count, 1, first)
        case = IgFollowUpTask.objects.get(reason="revision_case:collaboration_review")
        self.bundle("А собі хочу подивитись футболки. Які є фасони?")
        next_result, next_generation, next_http = self.reply("У нас є класичний крій та оверсайз. Який фасон вам подобається?")
        self.assertEqual(next_generation.call_count, 1, next_result)
        self.assertEqual(next_http.call_count, 1, next_result)
        case.refresh_from_db()
        self.assertNotIn(case.status, ("completed", "cancelled"))
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:collaboration_review").count(), 1)

    def test_withdrawn_business_in_same_burst_does_not_escalate_current_retail(self):
        self.bundle("Хочу відкрити магазин, цікавить співпраця", "Співпраця вже не цікавить.",
                    "Краще куплю собі футболку. Які фасони є?")
        result, generation, http = self.reply("Є класичний крій та оверсайз. Який фасон вам подобається?")
        self.assertEqual(generation.call_count, 1, result)
        self.assertEqual(http.call_count, 1, result)
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:collaboration_review").exists())

    def test_custom_print_handoff_owns_feasibility_without_payment_authority(self):
        self.bundle("Хочу футболку зі своїм малюнком. Яка ціна і коли можна зробити?")
        result, generation, http = self.reply("Зафіксувала запит на власний принт для перевірки можливості та вартості командою.",
            manager=True, case_reason="revision_case:custom_print")
        self.assertEqual(generation.call_count, 1, result)
        self.assertEqual(http.call_count, 1, result)
        case = IgFollowUpTask.objects.get(reason="revision_case:custom_print")
        self.assertEqual(case.manager_context["required_decisions"], ["design_feasibility", "quote_approval"])
        self.assertFalse(case.manager_context["authority"]["price_confirmed"])
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.manager_takeover)
        self.assertFalse(self.customer.bot_paused)

    def test_custom_print_withdrawal_in_burst_does_not_create_obsolete_case(self):
        self.bundle("Хочу власний принт з моїм малюнком.",
                    "Передумав, власний принт не потрібен. Краще куплю звичайну футболку. Які є фасони?")
        result, generation, http = self.reply("Є класичний крій та оверсайз. Який фасон вам подобається?")
        self.assertEqual(generation.call_count, 1, result)
        self.assertEqual(http.call_count, 1, result)
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:custom_print").exists(),
                         list(IgFollowUpTask.objects.values("reason", "manager_context")))

    def test_existing_custom_case_does_not_absorb_current_retail_withdrawal(self):
        self.bundle("Хочу власний принт з моїм малюнком.")
        first, _, http = self.reply("Зафіксувала запит на власний принт для перевірки можливості та вартості командою.", manager=True)
        self.assertEqual(http.call_count, 1, first)
        case = IgFollowUpTask.objects.get(reason="revision_case:custom_print")
        old_source_ids = [row["message_id"] for row in case.manager_context["sources"]]
        current = self.bundle("Більше не хочу свій принт. Куплю готову футболку. Які фасони є?")[0]
        result, generation, next_http = self.reply("Є класичний крій та оверсайз. Який фасон вам подобається?")
        self.assertEqual(generation.call_count, 1, result)
        self.assertEqual(next_http.call_count, 1, result)
        case.refresh_from_db()
        self.assertNotIn(current.pk, [row["message_id"] for row in case.manager_context["sources"]])
        self.assertEqual([row["message_id"] for row in case.manager_context["sources"]], old_source_ids)
        from management.services.ig_revision_intents import manager_case_reason
        self.bundle("Так")
        self.assertNotEqual(manager_case_reason(self.revision), "custom_print")

    def test_profile_intent_and_payment_control_cannot_invent_current_custom_request(self):
        from types import SimpleNamespace
        from management.services.ig_revision_intents import manager_case_reason

        self.customer.intent = "custom_print"
        self.customer.save(update_fields=["intent"])
        self.bundle("Хочу готову футболку. Які фасони є?")
        for control in ({"paylink": True}, {"payment": True}):
            with self.subTest(control=control):
                response = SimpleNamespace(control=control, reply_text="")
                self.assertNotEqual(manager_case_reason(self.revision, response), "custom_print")
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:custom_print").exists())

    def test_retail_question_after_confirmed_manager_reply_and_handback_is_not_silent(self):
        from django.contrib.auth import get_user_model
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command
        from management.services.ig_manual_resume import resume_client_automation

        self.bundle("Хочу відкрити магазин, цікавить співпраця.")
        result, _, http = self.reply("Зафіксувала ваше питання про партнерство для команди.", manager=True)
        self.assertEqual(http.call_count, 1, result)
        actor = get_user_model().objects.create_superuser(username="handoff-manager-local", password="fixture")
        command = create_human_reply_command(self.customer.pk, actor=actor,
            text="Дякуємо за пропозицію. Обговоримо умови з вами особисто.",
            context_message_id=self.source.pk).command
        with (patch("management.services.instagram_bot.get_page_token", return_value="local-test-token"),
              patch("management.services.instagram_bot._provider_http",
                    return_value=(200, json.dumps({"message_id": "manager-handoff-local"}))) as physical):
            sent = dispatch_human_reply_command(command.pk)
        self.assertEqual(sent.state, "sent")
        self.assertEqual(physical.call_count, 1)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.manager_takeover)
        resumed = resume_client_automation(self.customer.pk, actor=actor)
        self.assertTrue(resumed.changed)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.manager_takeover)
        self.bundle("А які є фасони футболок для мене?")
        result, generation, next_http = self.reply("Є класичний крій та оверсайз. Який фасон вам подобається?")
        self.assertEqual(generation.call_count, 1, result)
        self.assertEqual(next_http.call_count, 1, result)

    def test_exact_open_custom_case_keeps_short_quantity_reply_in_same_case(self):
        self.bundle("Хочу власний принт з моїм малюнком.")
        first, _, http = self.reply("Зафіксувала запит на власний принт для перевірки можливості командою.", manager=True)
        self.assertEqual(http.call_count, 1, first)
        case = IgFollowUpTask.objects.get(reason="revision_case:custom_print")
        for text in ("Так, одну", "Так"):
            current = self.bundle(text)[0]
            result, generation, physical = self.reply("Зафіксувала уточнення до вашого запиту на власний принт для команди.", manager=True)
            self.assertEqual(generation.call_count, 1, result)
            self.assertEqual(physical.call_count, 1, result)
            case.refresh_from_db()
            self.assertIn(current.pk, [row["message_id"] for row in case.manager_context["sources"]])
            self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:custom_print").count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(dedupe_key=f"ig-revision-case:{case.pk}").count(), 1)

    def test_completed_custom_case_does_not_reopen_from_short_affirmation(self):
        from management.services.ig_revision_intents import manager_case_reason

        self.bundle("Хочу футболку зі своїм малюнком.")
        first, _, http = self.reply("Зафіксувала запит на власний принт для перевірки можливості командою.", manager=True)
        self.assertEqual(http.call_count, 1, first)
        case = IgFollowUpTask.objects.get(reason="revision_case:custom_print")
        case.status = "completed"
        case.save(update_fields=["status"])
        self.bundle("Так, одну")
        self.assertNotEqual(manager_case_reason(self.revision), "custom_print")
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")

    def test_own_artwork_aliases_have_case_and_queued_notice_before_customer_send(self):
        for text, acknowledgement in (
                ("Хочу футболку зі своїм малюнком.", "Зафіксувала запит на власний принт для перевірки можливості командою."),
                ("Хочу нанести власне зображення на футболку.", "Зафіксувала запит на власний принт для перевірки можливості командою."),
                ("Хочу футболку со своим рисунком.", "Зафиксировала запрос на свой принт для проверки возможности командой."),
                ("I want a shirt with my own artwork.", "I recorded your custom print request for our team to check feasibility.")):
            with self.subTest(source=text):
                self.bundle(text)
                result, generation, http = self.reply(
                    acknowledgement,
                    manager=True, case_reason="revision_case:custom_print")
                self.assertEqual(generation.call_count, 1, result)
                self.assertEqual(http.call_count, 1, result)
                self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_cited_or_historical_artwork_does_not_open_manufacturing_case(self):
        for text, reply in (
                ('Цитата: «Хочу футболку зі своїм малюнком». Які у вас фасони?',
                 "Є класичний крій та оверсайз. Який фасон вам подобається?"),
                ('My friend said: I want a shirt with my own artwork.',
                 "Thanks for sharing that quotation."),
                ('Раніше хотів власний принт. Тепер хочу готову футболку. Які є фасони?',
                 "Є класичний крій та оверсайз. Який фасон вам подобається?")):
            with self.subTest(source=text):
                self.bundle(text)
                result, generation, http = self.reply(reply)
                self.assertEqual(generation.call_count, 1, result)
                self.assertEqual(http.call_count, 1, result)
                self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:custom_print").exists())


class CapturedCustomPolicyQATests(TestCase):
    setUp = policy_fixtures.CapturedPolicyInputsTests.setUp
    capture = policy_fixtures.CapturedPolicyInputsTests.capture
    text = policy_fixtures.CapturedPolicyInputsTests.text

    def test_shared_custom_predicate_controls_current_trigger_and_captured_policy(self):
        from management.services.bot_instruction_routing import turn_triggers
        for text, expected in (("Хочу футболку зі своїм малюнком", True),
                               ("I want a shirt with my own artwork", True),
                               ("Більше не хочу свій принт. Куплю готову футболку", False),
                               ('Цитата: «Хочу футболку зі своїм малюнком»', False),
                               ("Раніше хотів власний принт", False)):
            with self.subTest(text=text):
                self.text(text)
                self.assertEqual("custom_print" in turn_triggers(text), expected)
                self.assertEqual("custom_print" in self.capture().tags, expected)

    def test_later_custom_withdrawal_in_sealed_burst_clears_captured_policy_tag(self):
        from datetime import timedelta
        from management.services.ig_turn_intelligence import capture_digest
        self.text("Хочу власний принт")
        later = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="user", source="webhook", status="pending", provider_namespace=self.source.provider_namespace,
            text="Передумав, власний принт не потрібен. Краще готову футболку.",
            provider_created_at=self.at + timedelta(milliseconds=100))
        self.sources.append(dict(message_id=later.pk, role="user", text=later.text,
            source_namespace=later.provider_namespace, provider_created_at=later.provider_created_at.isoformat(), source_digest="b" * 64))
        self.boundary.update(source_ids=[row["message_id"] for row in self.sources],
            source_digests={str(row["message_id"]): row["source_digest"] for row in self.sources},
            sealed_sources_digest=capture_digest(self.sources),
            watermark=dict(message_id=later.pk, event_at=later.provider_created_at.isoformat()))
        self.assertNotIn("custom_print", self.capture().tags)
