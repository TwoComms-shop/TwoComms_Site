"""Native zero-dispatch fault acknowledgements preserve substantive reply debt."""
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.tests_gemini_accounting_shadow import _raw_plan
from management.models import GeminiRequest, GeminiRequestAttempt, IgClient, IgFollowUpTask, InstagramBotMessage
from management.services.ig_response_debt import response_obligation
from management.services.ig_revision_holding import holding_receipt_valid
from management.services.ig_revision_live import RevisionGenerationBoundary
from management.services.ig_revision_outbox import PublicationBinding, pre_winner_readiness
from management.services.ig_revision_recovery import record_generation_admission, record_local_preprovider_fault
from management.services.ig_terminal_fault_ack import (
    ATTEMPT_KEY, CANDIDATE_KEY, LIFETIME, claim_terminal_fault_ack,
    execute_terminal_fault_ack, record_terminal_fault_ack_candidate,
    terminal_fault_ack_revision_ids,
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class TerminalFaultAckTests(TransactionTestCase):
    reset_sequences = True
    _prepare = fixtures.RevisionLiveTests._prepare

    def _message(self, text, mid):
        if mid == "first":
            text = {
                "test_atypical_product_fault_sends_only_technical_ack_without_forged_product_case": "Які люверси на цьому худі?",
                "test_service_complaint_fault_sends_only_technical_ack_without_forged_service_case":
                    "Доставка коштувала 90 а взяли 120\nТак 30 грн не багато, але на ровному місці\nОсь такі перші враження",
            }.get(self._testMethodName, text)
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            mid=mid, provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            text=text, provider_created_at=timezone.now(), status="pending")

    def setUp(self):
        fixtures.RevisionLiveTests.setUp(self)
        self._prepare()
        self.binding = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        self.authority = RevisionGenerationBoundary(self.revision, self.token, self.settings, self.binding).baseline
        if self._testMethodName != "test_zero_graph_without_generation_admission_is_not_ack_authority":
            admission = record_generation_admission(self.revision.pk, self.token,
                settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=self.binding, authority=self.authority, candidate_plan=_raw_plan())
            self.assertTrue(admission.ready, admission.reason)
        self.now = timezone.now()
        self.customer.automation_lease_token = "terminal-ack-worker"
        self.customer.automation_lease_until = self.now + timedelta(minutes=2)
        self.customer.save(update_fields=["automation_lease_token", "automation_lease_until"])

    def _fault(self):
        result = record_local_preprovider_fault(self.revision.pk, self.token,
            stage="revision_execution", exception_type="ValueError", now=self.now)
        self.assertTrue(result.ready, result.reason)
        self.revision.refresh_from_db()
        self.task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{self.revision.pk}")

    def _candidate(self):
        self._fault()
        result = record_terminal_fault_ack_candidate(self.revision.pk, now=self.now)
        self.assertTrue(result.ready, result.reason)
        self.revision.refresh_from_db()
        return result

    def _claim(self):
        revision, token = claim_terminal_fault_ack(self.revision.pk,
            automation_lease_token=self.customer.automation_lease_token, now=self.now)
        self.assertIsNotNone(revision)
        self.assertTrue(token)
        self.revision, self.ack_token = revision, token
        return revision.action_receipts["technical_holding"]

    def _send(self, *, http_result=(200, '{"message_id":"terminal-ack-receipt"}')):
        with (
            patch("management.services.instagram_bot.get_page_token", return_value="local-test-token"),
            patch("management.services.instagram_bot._provider_http", return_value=http_result) as http,
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("ack cannot generate")) as generate,
            patch("management.services.ig_response_plan.capture_response_plan", side_effect=AssertionError("neutral ack cannot compile a plan")),
        ):
            result = execute_terminal_fault_ack(self.revision.pk, self.ack_token, self.settings)
        generate.assert_not_called()
        return result, http

    def test_real_case_precedes_one_receipted_ack_without_generation_or_plan_compilation(self):
        self._candidate()
        receipt = self._claim()
        task_id = self.task.pk
        self.assertEqual(receipt["task_id"], task_id)
        self.assertEqual(receipt["substantive_obligation"], "open_manager_reply")
        self.assertEqual(receipt["authority"]["allowed_actions"], [])
        self.assertNotIn("policy_readiness_failure", self.revision.action_receipts)
        self.assertNotIn("співпрац", receipt["reply_text"])
        self.assertNotIn("передал", receipt["reply_text"])
        self.assertFalse(connection.in_atomic_block)
        result, http = self._send()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(result.reasons, ("technical_holding_sent",))
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(self.source.status, "pending")
        self.assertNotIn(self.task.status, {"completed", "cancelled"})
        obligation = response_obligation(self.revision)
        self.assertFalse(obligation["customer_ack_owed"])
        self.assertTrue(obligation["substantive_owed"])
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        again, replay_http = self._send()
        replay_http.assert_not_called()
        self.assertNotEqual(again.sent_parts, 1)
        self.assertEqual(terminal_fault_ack_revision_ids(), [])

    def test_missing_manifest_or_an_old_manual_fault_does_not_become_a_candidate(self):
        self._fault()
        self.assertEqual(terminal_fault_ack_revision_ids(), [])
        denied = record_terminal_fault_ack_candidate(self.revision.pk, now=self.now + LIFETIME)
        self.assertEqual(denied.reason, "terminal_ack_fault_not_new")
        self.assertFalse(denied.ready)
        self.revision.refresh_from_db()
        self.assertNotIn(CANDIDATE_KEY, self.revision.action_receipts)

    def test_candidate_deadline_is_fixed_and_does_not_extend_generation_deadline(self):
        original_deadline = self.revision.overall_deadline
        first = self._candidate()
        second = record_terminal_fault_ack_candidate(self.revision.pk, now=self.now + timedelta(seconds=5))
        self.assertEqual(first.receipt, second.receipt)
        revision, token = claim_terminal_fault_ack(self.revision.pk,
            automation_lease_token=self.customer.automation_lease_token, now=self.now + LIFETIME)
        self.assertIsNone(revision)
        self.assertEqual(token, "")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.overall_deadline, original_deadline)

    def test_expired_generation_gets_only_explicit_ack_authority(self):
        self.now = self.revision.overall_deadline + timedelta(seconds=1)
        self.customer.automation_lease_until = self.now + timedelta(minutes=2)
        self.customer.save(update_fields=["automation_lease_until"])
        with patch("management.services.ig_terminal_fault_ack.timezone.now", return_value=self.now):
            self._candidate()
            self._claim()
            regular = pre_winner_readiness(self.revision.pk, self.ack_token,
                settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=self.binding, now=self.now)
            self.assertIn("revision_deadline_exhausted", regular.reasons)
            result, http = self._send()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(result.reasons, ("technical_holding_sent",))

    def test_unknown_delivery_never_replays_or_claims_acknowledgement(self):
        self._candidate()
        self._claim()
        _result, http = self._send(http_result=(500, '{}'))
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "unknown")
        self.assertTrue(response_obligation(self.revision)["customer_ack_owed"])
        _again, second_http = self._send()
        second_http.assert_not_called()
        self.assertEqual(terminal_fault_ack_revision_ids(), [])
        self.assertFalse(GeminiRequest.objects.exists())

    def test_permission_guards_deny_claim_without_consuming_an_attempt(self):
        self._candidate()
        updates = ({"bot_paused": True}, {"manager_takeover": True}, {"is_blocked": True},
            {"stage": IgClient.Stage.SPAM}, {"opted_out_at": self.now},
            {"privacy_erasure_started_at": self.now}, {"reply_permission_epoch": 99})
        for values in updates:
            with self.subTest(values=values):
                previous = {key: getattr(self.customer, key) for key in values}
                IgClient.objects.filter(pk=self.customer.pk).update(**values)
                revision, token = claim_terminal_fault_ack(self.revision.pk,
                    automation_lease_token=self.customer.automation_lease_token, now=self.now)
                self.assertIsNone(revision)
                self.assertFalse(token)
                IgClient.objects.filter(pk=self.customer.pk).update(**previous)
        self.revision.refresh_from_db()
        self.assertNotIn(ATTEMPT_KEY, self.revision.action_receipts)

    def test_new_source_blocks_ack_after_claim(self):
        self._candidate()
        self._claim()
        self._message("А ще яке є худі?", "newer-question")
        _result, http = self._send()
        http.assert_not_called()

    def test_takeover_after_claim_blocks_physical_http(self):
        self._candidate()
        self._claim()
        IgClient.objects.filter(pk=self.customer.pk).update(manager_takeover=True)
        _result, http = self._send()
        http.assert_not_called()

    def test_closed_case_after_claim_blocks_physical_http(self):
        self._candidate()
        self._claim()
        self.task.status = "completed"
        self.task.save(update_fields=["status"])
        _result, http = self._send()
        http.assert_not_called()

    def test_changed_publication_after_claim_blocks_physical_http(self):
        self._candidate()
        self._claim()
        self.settings.active_instruction_publication = None
        self.settings.save(update_fields=["active_instruction_publication"])
        _result, http = self._send()
        http.assert_not_called()

    def test_expired_ack_lifetime_blocks_physical_http(self):
        self._candidate()
        self._claim()
        with patch("management.services.ig_terminal_fault_ack.timezone.now", return_value=self.now + LIFETIME):
            _result, http = self._send()
        http.assert_not_called()

    def test_closed_meta_window_blocks_http_even_while_ack_lifetime_is_current(self):
        self._candidate()
        self._claim()
        with patch("management.services.ig_revision_outbox._normal_reply_window_deadline",
            return_value=self.now - timedelta(seconds=1)):
            _result, http = self._send()
        http.assert_not_called()

    def test_pause_after_provider_started_is_rechecked_before_actual_http(self):
        from management.services.ig_revision_outbox import mark_provider_started
        self._candidate()
        self._claim()
        def started_then_pause(*args, **kwargs):
            result = mark_provider_started(*args, **kwargs)
            self.assertEqual(result.reason, "provider_started")
            IgClient.objects.filter(pk=self.customer.pk).update(bot_paused=True)
            return result
        with patch("management.services.ig_revision_delivery.mark_provider_started", side_effect=started_then_pause):
            _result, http = self._send()
        http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertNotEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertTrue(response_obligation(self.revision)["customer_ack_owed"])

    def test_orphan_attempt_blocks_ack_even_without_a_request_graph(self):
        self._candidate()
        GeminiRequestAttempt.objects.create(request_id="orphan-terminal-fault", role="chat",
            key_name="GEMINI_API", model="gemini-3.7-flash", outcome="unknown",
            logical_turn_id=f"ig-revision:{self.revision.pk}", source_message_id=self.source.pk,
            client_id=self.customer.pk, lane="live")
        self.assertFalse(GeminiRequest.objects.exists())
        revision, token = claim_terminal_fault_ack(self.revision.pk,
            automation_lease_token=self.customer.automation_lease_token, now=self.now)
        self.assertIsNone(revision)
        self.assertFalse(token)
        self.assertEqual(terminal_fault_ack_revision_ids(), [])

    def test_forged_fault_or_admission_does_not_admit_ack(self):
        self._fault()
        self.task.manager_context["local_preprovider_fault"]["provider_graph_present"] = True
        self.task.save(update_fields=["manager_context"])
        self.assertFalse(record_terminal_fault_ack_candidate(self.revision.pk, now=self.now).ready)
        self.assertEqual(terminal_fault_ack_revision_ids(), [])

    def test_zero_graph_without_generation_admission_is_not_ack_authority(self):
        self._fault()
        self.assertNotIn("generation_admission", self.revision.action_receipts)
        self.assertFalse(record_terminal_fault_ack_candidate(self.revision.pk, now=self.now).ready)
        self.assertEqual(terminal_fault_ack_revision_ids(), [])

    def test_candidate_with_existing_proposal_abstains(self):
        self._candidate()
        self.revision.generation_proposal = {"reply_text": "Already generated"}
        self.revision.save(update_fields=["generation_proposal"])
        revision, token = claim_terminal_fault_ack(self.revision.pk,
            automation_lease_token=self.customer.automation_lease_token, now=self.now)
        self.assertIsNone(revision)
        self.assertFalse(token)
        self.assertEqual(terminal_fault_ack_revision_ids(), [])

    def test_acknowledgement_text_cannot_be_replaced_with_a_business_promise(self):
        from management.services.ig_revision_outbox import _digest
        self._candidate()
        self._claim()
        receipt = self.revision.action_receipts["technical_holding"]
        receipt["reply_text"] = "Команда погодила партнерські ціни та відправлення сьогодні."
        receipt["reply_digest"] = _digest(receipt["reply_text"])
        self.assertFalse(holding_receipt_valid(self.revision))

    def _assert_domain_fault_ack(self, expected_reason):
        from management.services.ig_revision_intents import manager_case_reason
        self.assertEqual(manager_case_reason(self.revision), expected_reason)
        self._candidate()
        self._claim()
        result, http = self._send()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(result.reasons, ("technical_holding_sent",))
        self.revision.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(self.task.reason, "revision_case:execution_debt")
        self.assertNotIn("manager_handoff", self.revision.action_receipts)
        self.assertNotIn("product_review", self.revision.action_receipts)
        self.assertNotIn(self.task.status, {"completed", "cancelled"})
        self.assertTrue(response_obligation(self.revision)["substantive_owed"])
        self.assertFalse(response_obligation(self.revision)["customer_ack_owed"])

    def test_atypical_product_fault_sends_only_technical_ack_without_forged_product_case(self):
        self._assert_domain_fault_ack("product_information_review")

    def test_service_complaint_fault_sends_only_technical_ack_without_forged_service_case(self):
        self._assert_domain_fault_ack("service_complaint_review")
