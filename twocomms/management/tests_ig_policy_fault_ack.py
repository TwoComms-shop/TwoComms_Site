"""Native local-policy acknowledgement admission, case and delivery boundaries."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import BotPolicyPublication, GeminiRequest, IgBotNotification, IgFollowUpTask
from management.services.ig_revision_holding import record_policy_fault_ack
from management.services.ig_revision_live import RevisionGenerationBoundary, _execute_deterministic_input, execute_claimed_revision
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_revision_recovery import record_generation_admission, record_policy_readiness_failure
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class PolicyFaultAckTests(TransactionTestCase):
    reset_sequences = True
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare

    def setUp(self):
        fixtures.RevisionLiveTests.setUp(self)
        self._reset_fault_text("Хочу відкрити інстаграм магазин і співпрацювати з вами. "
            "Які партнерські ціни, розміри і кольори? Чи можна почати без закупівлі партії? "
            "Як відправляти замовлення з Instagram? Чи можна використовувати ваші фото?")

    def _reset_fault_text(self, text, *, record_fault=True):
        self.revision.delete()
        self.source.text = text
        self.source.provider_created_at = timezone.now()
        self.source.save(update_fields=["text", "provider_created_at"])
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        self.binding = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        if not record_fault:
            return
        self._record_fault()

    def _record_fault(self):
        from management.tests_gemini_accounting_shadow import _raw_plan

        authority = RevisionGenerationBoundary(self.revision, self.token, self.settings, self.binding).baseline
        admitted = record_generation_admission(self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.binding, authority=authority,
            candidate_plan=_raw_plan())
        self.assertTrue(admitted.ready, admitted.reason)
        recorded = record_policy_readiness_failure(self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.binding,
            code="captured_state_exceeds_budget", details={"budget_chars": 48000}, park=False)
        self.assertTrue(recorded.ready, recorded.reason)

    def _ack(self):
        return record_policy_fault_ack(self.revision.pk, self.token, settings_id=self.settings.pk)

    def _send(self, receipt, result=None):
        self.revision.refresh_from_db()
        with (patch("management.services.instagram_bot.get_page_token", return_value="local-test-token"),
            patch("management.services.instagram_bot._provider_http", side_effect=result or self._http) as http,
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate):
            outcome = _execute_deterministic_input(self.revision, self.token, self.settings, receipt)
        generate.assert_not_called()
        self.assertFalse(GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{self.revision.pk}").exists())
        return outcome, http

    def _http(self, *args, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["technical_holding"]
        task = IgFollowUpTask.objects.get(pk=receipt["task_id"])
        self.assertEqual(task.manager_context["cooperation_kind"], "business")
        self.assertEqual(task.manager_context["source_message_ids"], [self.source.pk])
        self.assertEqual(task.reason, "revision_case:collaboration_review")
        self._assert_pending_notification_copy(receipt)
        return 200, json.dumps({"message_id": "policy-ack-once"})

    def _assert_pending_notification_copy(self, receipt):
        notification = IgBotNotification.objects.get(pk=receipt["notification_id"])
        self.assertEqual(notification.status, "pending")
        self.assertIsNone(notification.sent_at)
        self.assertRegex(receipt["reply_text"], r"(?i)зафіксувала|зафиксировала|recorded|दर्ज")
        self.assertNotRegex(receipt["reply_text"], r"(?i)передал|передан|надіслал|відправил|passed|forwarded|sent.{0,30}(?:team|management)|(?:команд\w*|керівництв\w*|руководств\w*|team|management).{0,20}(?:отримал|получил|прочитал|received|read|approved)")

    def test_business_case_precedes_one_sent_ack_and_substantive_question_stays_owed(self):
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        self.assertNotIn("портфол", ack.receipt["reply_text"].lower())
        self.assertNotIn("фото-", ack.receipt["reply_text"].lower())
        self.assertIn("партнер", ack.receipt["reply_text"].lower())
        first, http = self._send(ack.receipt)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(first.reasons, ("technical_holding_sent",))
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        self.assertEqual(self.revision.recovery_state, "manual")
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(ack.receipt["substantive_obligation"], "open_manager_reply")
        second, again = self._send(ack.receipt)
        again.assert_not_called()
        self.assertFalse(self._ack().ready)
        self.assertEqual(self.revision.delivery_effects.count(), 1)

    @override_settings(IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=1)
    def test_actual_captured_state_compiler_fault_enters_live_ack_outbox_without_model_dispatch(self):
        self._reset_fault_text(self.source.text, record_fault=False)
        with (patch("management.services.instagram_bot.get_page_token", return_value="local-test-token"),
            patch("management.services.instagram_bot._provider_http", side_effect=self._http) as http,
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate):
            outcome = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1, outcome)
        self.assertEqual(outcome.reasons, ("technical_holding_sent",))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["policy_readiness_failure"]["code"], "captured_state_exceeds_budget")
        self.assertFalse(GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{self.revision.pk}").exists())

    def test_creator_ack_keeps_creator_inputs_separate_from_business_questions(self):
        self._reset_fault_text("Я фотограф, пропоную співпрацю вашому бренду, можу знімати фото для вас.")
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        self._assert_pending_notification_copy(ack.receipt)
        self.assertIn("результати попередніх проєктів", ack.receipt["reply_text"].lower())
        task = IgFollowUpTask.objects.get(pk=ack.receipt["task_id"])
        self.assertEqual(task.manager_context["cooperation_kind"], "creator")
        self.assertIn("portfolio_review", task.manager_context["required_decisions"])

    def test_unspecified_cooperation_records_real_case_then_asks_only_format(self):
        self._reset_fault_text("Добрий день! Можна щодо співпраці?")
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        self._assert_pending_notification_copy(ack.receipt)
        reply = ack.receipt["reply_text"]
        self.assertIn("Який формат співпраці вас цікавить?", reply)
        self.assertEqual(reply.count("?"), 1)
        self.assertNotRegex(reply.lower(), r"портфол|результат|фото|магазин|ваші запитання|додаткові")
        task = IgFollowUpTask.objects.get(pk=ack.receipt["task_id"])
        self.assertEqual(task.manager_context["source_message_ids"], [self.source.pk])
        self.assertNotIn("portfolio_review", task.manager_context["required_decisions"])
        result, http = self._send(ack.receipt)
        self.assertEqual(result.reasons, ("technical_holding_sent",))
        self.assertEqual(http.call_count, 1)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_shop_question_invitation_does_not_claim_unasked_questions_were_recorded(self):
        self._reset_fault_text("Хочу запустити Instagram-магазин, можна поставити кілька запитань?")
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        self._assert_pending_notification_copy(ack.receipt)
        self.assertIn("Напишіть, будь ласка, свої запитання тут", ack.receipt["reply_text"])
        self.assertNotIn("Зафіксувала ваші запитання", ack.receipt["reply_text"])
        self.assertNotIn("Який формат", ack.receipt["reply_text"])
        self.assertNotIn("портфоліо", ack.receipt["reply_text"])

    def test_question_invitation_does_not_require_a_business_or_creator_offer(self):
        self._reset_fault_text("Можна поставити кілька запитань?")
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        self._assert_pending_notification_copy(ack.receipt)
        self.assertIn("запитання", ack.receipt["reply_text"])
        self.assertNotIn("портфоліо", ack.receipt["reply_text"])
        task = IgFollowUpTask.objects.get(pk=ack.receipt["task_id"])
        self.assertEqual(task.reason, "revision_case:manager_handoff")

    def test_unknown_ack_never_replays_or_claims_source_completion(self):
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        first, http = self._send(ack.receipt, result=lambda *args, **kwargs: (200, "{}"))
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "unknown")
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        _, again = self._send(ack.receipt)
        again.assert_not_called()
        self.assertFalse(self._ack().ready)

    def test_audited_resume_cannot_send_another_ack_for_the_same_unanswered_question(self):
        from django.contrib.auth import get_user_model
        from management.models import IgCustomerTurnRevision
        from management.services.ig_manual_resume import resume_client_automation

        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        _, http = self._send(ack.receipt)
        self.assertEqual(http.call_count, 1)
        self.customer.bot_paused = True
        self.customer.save(update_fields=["bot_paused"])
        actor = get_user_model().objects.create_user("policy-ack-resume-reviewer", is_staff=True, is_superuser=True)
        with patch("management.services.ig_revision_live.revision_execution_enabled", return_value=True):
            resumed = resume_client_automation(self.customer.pk, actor=actor)
        self.assertTrue(resumed.successor_created, resumed.successor_reason)
        self.customer.refresh_from_db()
        self.revision = IgCustomerTurnRevision.objects.get(pk=resumed.successor_revision_id)
        self._prepare()
        self._record_fault()
        again = self._ack()
        self.assertFalse(again.ready)
        self.assertEqual(again.reason, "holding_continuous_wait_already_notified")
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_new_source_blocks_ack_before_creating_case(self):
        self._message("Нове питання", "new-after-fault")
        result = self._ack()
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "pending_inbound")
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-policy-fault-case:{self.customer.pk}:{self.revision.pk}").exists())

    def test_changed_publication_blocks_ack(self):
        publication = BotPolicyPublication.objects.create(version=self.publication.version + 1, kind="publish", schema_version=1,
            snapshot=self.publication.snapshot, snapshot_hash=self.publication.snapshot_hash,
            compiler_version="instruction-set-v1", instruction_count=0)
        self.assertGreater(publication.version, self.binding.version)
        self.settings.active_instruction_publication = publication
        self.settings.save(update_fields=["active_instruction_publication"])
        self.assertFalse(self._ack().ready)

    def test_takeover_blocks_ack(self):
        self.customer.manager_takeover = True
        self.customer.save(update_fields=["manager_takeover"])
        result = self._ack()
        self.assertFalse(result.ready)
        self.assertIn(result.reason, {"manager_takeover", "client_permission_changed"})

    def test_closed_window_blocks_ack(self):
        future = timezone.now() + timedelta(days=2)
        with patch("management.services.ig_revision_outbox.timezone.now", return_value=future):
            result = self._ack()
        self.assertFalse(result.ready)
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_closed_business_case_after_admission_blocks_physical_send(self):
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        IgFollowUpTask.objects.filter(pk=ack.receipt["task_id"]).update(status="completed")
        _, http = self._send(ack.receipt)
        http.assert_not_called()

    def test_takeover_after_admission_blocks_physical_send(self):
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        self.customer.manager_takeover = True
        self.customer.save(update_fields=["manager_takeover"])
        _, http = self._send(ack.receipt)
        http.assert_not_called()

    def test_new_inbound_after_admission_blocks_physical_send(self):
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        self._message("Додам нові умови", "new-after-ack")
        _, http = self._send(ack.receipt)
        http.assert_not_called()

    def test_publication_change_after_admission_blocks_physical_send(self):
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        publication = BotPolicyPublication.objects.create(version=self.publication.version + 1, kind="publish", schema_version=1,
            snapshot=self.publication.snapshot, snapshot_hash=self.publication.snapshot_hash,
            compiler_version="instruction-set-v1", instruction_count=0)
        self.assertGreater(publication.version, self.binding.version)
        self.settings.active_instruction_publication = publication
        self.settings.save(update_fields=["active_instruction_publication"])
        _, http = self._send(ack.receipt)
        http.assert_not_called()

    def test_closed_window_after_admission_blocks_physical_send(self):
        ack = self._ack()
        self.assertTrue(ack.ready, ack.reason)
        with patch("management.services.ig_revision_outbox.timezone.now", return_value=timezone.now() + timedelta(days=2)):
            _, http = self._send(ack.receipt)
        http.assert_not_called()

    def test_no_stored_policy_proof_cannot_admit_an_ack(self):
        from management.services.ig_policy_fault_ack import _proof_valid
        self.revision.refresh_from_db()
        self.assertFalse(_proof_valid(self.revision, {}))
