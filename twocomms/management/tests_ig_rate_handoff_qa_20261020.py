"""The real 26th input retains its question and receives one bounded handoff."""
from datetime import timedelta
import json
import traceback
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.db import connection
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management import tests_ig_revision_execution as execution_fixtures
from management.models import GeminiRequest, IgBotNotification, IgClient, IgCustomerTurnRevision, IgFollowUpTask, IgRevisionDeliveryEffect
from management.services.ig_revision_input import decide_revision_input
from management.services.ig_revision_live import process_pending_revisions


@override_settings(IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00", GOOGLE_INDEXING_ENABLED=False)
class RateHandoffNativeQATests(TransactionTestCase):
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _collecting_revision = execution_fixtures.RevisionExecutionTests._collecting_revision
    _generate = fixtures.RevisionLiveTests._generate

    def setUp(self):
        fixtures.RevisionLiveTests.setUp(self)
        for number in range(25):
            if number:
                self._new(f"Попереднє питання {number}")
            self._prepare()
            decision = decide_revision_input(self.revision.pk, self.token, settings_id=self.settings.pk)
            self.assertTrue(decision.ready, decision.reason)
            self.assertEqual(decision.origin, "generate")
            self.revision.refresh_from_db()
            # Historical successfully handled admissions seed the existing cap;
            # only the new 26th turn is sent through the worker under test.
            self.revision.state = "processed"
            self.revision.active_slot = None
            self.revision.claim_token = ""
            self.revision.lease_until = None
            self.revision.save(update_fields=["state", "active_slot", "claim_token", "lease_until"])
            self.source.status = "done"
            self.source.save(update_fields=["status"])
        self._new("Можна поставити кілька запитань про ваш одяг?")

    def _new(self, text):
        self.source = self._message(text, f"rate-source-{timezone.now().timestamp()}")
        self.turn, self.revision = self._collecting_revision([self.source])
        self.token = ""

    def _run(self, result=None):
        with patch("management.services.instagram_bot.get_page_token", return_value="test-token"), \
            patch("management.services.instagram_bot._provider_http", side_effect=result or self._http) as send, \
            patch("management.services.instagram_bot._register_outgoing_message"), \
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("cap must not generate")) as generate, \
            patch("management.services.instagram_bot.log", side_effect=self._worker_log):
            outcome = process_pending_revisions(self.settings, max_items=1)
        generate.assert_not_called()
        return outcome, send

    def _worker_log(self, level, event, detail, **metadata):
        if event == "revision_execution" and level == "error":
            self.fail(traceback.format_exc())

    def _http(self, *args, **kwargs):
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:rate_limit_handoff").count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="sender_rate_limited").count(), 1)
        return 200, json.dumps({"message_id": "cap-ack"})

    def test_actual_26th_question_is_acknowledged_once_and_substantive_reply_stays_owed(self):
        _, send = self._run()
        self.assertEqual(send.call_count, 1)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.turn.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertEqual(self.source.status, "pending")
        self.assertNotEqual(self.turn.terminal_reason, "no_reply_needed")
        self.assertNotEqual(self.revision.state, "processed")
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertEqual(IgCustomerTurnRevision.objects.filter(
            action_receipts__input_decision__rate_counted=True).count(), 25)
        _, repeated = self._run()
        repeated.assert_not_called()

    def test_new_capped_questions_join_case_without_repeating_ack_or_notification(self):
        from management.services.ig_response_debt import response_obligation

        self._run()
        first_revision = self.revision
        first_revision.refresh_from_db()
        original_ack = first_revision.action_receipts["technical_holding"].copy()
        task = IgFollowUpTask.objects.get(reason="revision_case:rate_limit_handoff")
        initial_payload = task.event_payload.copy()
        notice = IgBotNotification.objects.get(event_type="sender_rate_limited")
        original_notice = notice.payload.copy()
        notice.status, notice.sent_at, notice.telegram_message_id = "sent", timezone.now(), "manager-saw-first"
        notice.save(update_fields=["status", "sent_at", "telegram_message_id"])
        self._new("А які способи доставки?")
        _, send = self._run()
        send.assert_not_called()
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        self.assertEqual(self.revision.action_receipts["rate_handoff"]["task_id"], task.pk)
        task.refresh_from_db()
        self.assertEqual(task.event_payload, initial_payload)
        self.assertEqual(task.manager_context["scope_count"], 2)
        self.assertEqual(task.manager_context["latest_source_message_ids"], [self.source.pk])
        self.assertIn(self.source.text, task.manager_context["latest_questions"])
        first_revision.refresh_from_db()
        self.assertEqual(first_revision.action_receipts["technical_holding"], original_ack)
        notice.refresh_from_db()
        self.assertEqual(notice.payload, original_notice)
        state = response_obligation(self.revision)
        self.assertEqual(state["task_id"], task.pk)
        self.assertTrue(state["substantive_owed"])
        self.assertTrue(state["customer_ack_owed"])
        self.assertFalse(state["manager_informed"])
        self.assertFalse(state["manager_notification_recorded"])
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=2)):
            _, repeat = self._run()
        repeat.assert_not_called()
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)

    def test_unknown_first_ack_is_never_replayed_or_replaced_for_later_questions(self):
        self._run(result=[TimeoutError("ambiguous acknowledgement")])
        self.assertEqual(IgRevisionDeliveryEffect.objects.get().state, "unknown")
        _, retry = self._run()
        retry.assert_not_called()
        self._new("Ще одне питання про доставку")
        _, later = self._run()
        later.assert_not_called()
        self.assertEqual(IgRevisionDeliveryEffect.objects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)

    def test_fresh_turn_after_window_uses_normal_generation_and_old_case_stays_open(self):
        self._run()
        old = self.source
        self.revision.refresh_from_db()
        until = timezone.datetime.fromisoformat(self.revision.action_receipts["input_decision"]["rate_window"]["until"])
        with patch("django.utils.timezone.now", return_value=until + timedelta(seconds=5)):
            self._new("Вітаю, розкажіть більше")
            with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate, \
                patch("management.services.instagram_bot.get_page_token", return_value="test-token"), \
                patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "normal-after-window"}))) as send, \
                patch("management.services.instagram_bot._register_outgoing_message"):
                self.assertEqual(process_pending_revisions(self.settings, max_items=1), 1)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.source.status, "done")
        self.assertEqual(old.status, "pending")
        self.assertEqual(IgFollowUpTask.objects.get().status, "skipped")

    def test_manager_takeover_after_plan_keeps_ack_unsent_and_question_owed(self):
        from management.services.ig_revision_outbox import plan_revision_effects

        def plan_then_take_over(*args, **kwargs):
            planned = plan_revision_effects(*args, **kwargs)
            IgClient.objects.filter(pk=self.customer.pk).update(manager_takeover=True)
            return planned

        with patch("management.services.ig_revision_live.plan_revision_effects", side_effect=plan_then_take_over):
            _, send = self._run()
        send.assert_not_called()
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_new_source_between_plan_and_send_cancels_ack_without_consuming_question(self):
        from management.services.ig_revision_outbox import plan_revision_effects

        def plan_then_new_source(*args, **kwargs):
            planned = plan_revision_effects(*args, **kwargs)
            self._message("Нове уточнення", "rate-before-send-new-source")
            return planned

        with patch("management.services.ig_revision_live.plan_revision_effects", side_effect=plan_then_new_source):
            _, send = self._run()
        send.assert_not_called()
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_no_reply_completion_rejects_rate_handoff(self):
        from management.services.ig_revision_input import complete_no_reply_input

        self._prepare()
        decision = decide_revision_input(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertEqual(decision.origin, "rate_handoff")
        self.assertFalse(complete_no_reply_input(self.revision.pk, self.token))
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def _human(self, source, *, unknown=False):
        from django.contrib.auth import get_user_model
        from management.ig_human_reply_models import HumanReplyPart
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command

        actor, _ = get_user_model().objects.get_or_create(username="rate-handoff-manager",
            defaults={"is_staff": True, "is_superuser": True})
        accepted = create_human_reply_command(self.customer.pk, actor=actor,
            text="Дякую за запитання, ось відповідь команди.", context_message_id=source.pk)
        def physical(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(HumanReplyPart.objects.filter(command=accepted.command, state="provider_started").exists())
            if unknown:
                raise TimeoutError("ambiguous human reply")
            return 200, json.dumps({"message_id": "rate-human-confirmed"})
        with patch("management.services.instagram_bot.get_page_token", return_value="test-token"), \
            patch("management.services.instagram_bot._provider_http", side_effect=physical), \
            patch("management.services.instagram_bot._enqueue_memory_source_event"):
            return dispatch_human_reply_command(accepted.command.pk)

    def test_real_sent_human_reply_settles_only_exact_appended_scope(self):
        from management.services.ig_response_debt import response_obligation

        self._run()
        first = self.revision
        self._new("Друге незалежне питання")
        self._run()
        second = self.revision
        command = self._human(self.source)
        self.assertEqual(command.state, "sent")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(response_obligation(first)["substantive_owed"])
        self.assertFalse(response_obligation(second)["substantive_owed"])
        self.assertEqual(IgFollowUpTask.objects.get().status, "skipped")
        # Mutable case state cannot preserve resolution after physical proof
        # becomes UNKNOWN or the command is rebound to an unrelated source.
        type(command).objects.filter(pk=command.pk).update(state="unknown")
        self.assertTrue(response_obligation(second)["substantive_owed"])

    def test_only_bound_scope_answer_completes_case_with_native_delivery_proof(self):
        from management.services.ig_response_debt import response_obligation

        self._run()
        command = self._human(self.source)
        self.assertEqual(command.state, "sent")
        self.revision.refresh_from_db()
        self.assertFalse(response_obligation(self.revision)["substantive_owed"])
        self.assertEqual(IgFollowUpTask.objects.get().status, "completed")

    def test_unrelated_or_unknown_human_delivery_does_not_settle_capped_source(self):
        from management.services.ig_response_debt import response_obligation

        self._run()
        capped = self.revision
        self._new("Нове питання поза переданим зверненням")
        self.assertEqual(self._human(self.source, unknown=True).state, "unknown")
        capped.refresh_from_db()
        self.assertTrue(response_obligation(capped)["substantive_owed"])
        self.assertEqual(IgFollowUpTask.objects.get(reason="revision_case:rate_limit_handoff").status, "skipped")

    def test_another_window_task_cannot_pause_or_own_this_source(self):
        from management.services.ig_response_debt import paused_response_obligation_q, response_obligation_paused
        from management.services.ig_revision_outbox import _digest

        self._run()
        self.revision.refresh_from_db()
        original = IgFollowUpTask.objects.get()
        other_window = {**original.event_payload["rate_window"], "anchor_revision_id": 999999}
        other = IgFollowUpTask.objects.create(client=self.customer, kind="manager_task", due_at=timezone.now(),
            reason="revision_case:rate_limit_handoff", event_key=f"ig-rate-handoff:{self.customer.pk}:999999",
            event_payload={"rate_window": other_window, "rate_window_digest": _digest(other_window)},
            manager_context={"owner": "manager"})
        receipts = dict(self.revision.action_receipts)
        scope = {**receipts["rate_handoff"], "task_id": other.pk}
        scope["digest"] = _digest({key: value for key, value in scope.items() if key != "digest"})
        receipts["rate_handoff"] = scope
        # Corrupt-state regression deliberately bypasses application guards in
        # this disposable SQLite fixture; the runtime must fail closed.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE management_igcustomerturnrevision SET action_receipts = %s WHERE id = %s",
                [json.dumps(receipts), self.revision.pk])
        self.revision.refresh_from_db()
        self.assertFalse(response_obligation_paused(self.revision))
        self.assertFalse(IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).filter(paused_response_obligation_q()).exists())

    def _undispatched_first_ack_with_fresh_successor(self):
        from management.services.ig_revision_outbox import plan_revision_effects

        first = self.revision
        def plan_then_successor(*args, **kwargs):
            plan = plan_revision_effects(*args, **kwargs)
            self._new("Уточнення: можна поставити запитання?")
            return plan
        with patch("management.services.ig_revision_live.plan_revision_effects", side_effect=plan_then_successor):
            _, send = self._run()
        send.assert_not_called()
        first.refresh_from_db()
        self.assertFalse(first.delivery_effects.filter(provider_started_at__isnull=False).exists())
        return first

    def test_zero_dispatch_first_ack_moves_to_fresh_scope_and_is_sent_once(self):
        from management.services.ig_response_debt import response_obligation

        first = self._undispatched_first_ack_with_fresh_successor()
        task = IgFollowUpTask.objects.get(reason="revision_case:rate_limit_handoff")
        payload = task.event_payload.copy()
        notice = IgBotNotification.objects.get(event_type="sender_rate_limited")
        notice_payload = notice.payload.copy()
        original = first.action_receipts.copy()
        _, send = self._run()
        self.assertEqual(send.call_count, 1)
        first.refresh_from_db()
        self.revision.refresh_from_db()
        task.refresh_from_db()
        notice.refresh_from_db()
        self.assertEqual(first.action_receipts, original)
        self.assertEqual(task.event_payload, payload)
        self.assertEqual(notice.payload, notice_payload)
        self.assertEqual(first.delivery_effects.get().state, "cancelled")
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertFalse(response_obligation(self.revision)["manager_informed"])
        self.assertTrue(response_obligation(self.revision)["substantive_owed"])
        _, repeat = self._run()
        repeat.assert_not_called()
        self.assertEqual(IgBotNotification.objects.count(), 1)
        self.assertEqual(IgRevisionDeliveryEffect.objects.filter(state="sent").count(), 1)

    def _prior_effect_blocks_transfer(self, state, *, started=False):
        first = self._undispatched_first_ack_with_fresh_successor()
        fields = {"state": state}
        if started:
            fields["provider_started_at"] = timezone.now()
        if state == "claimed":
            fields.update(claim_token="inflight-old-owner", lease_until=timezone.now() + timedelta(seconds=60))
        first.delivery_effects.update(**fields)
        _, send = self._run()
        send.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertEqual(first.delivery_effects.get().state, state)

    def test_claimed_predecessor_is_never_transferred(self):
        self._prior_effect_blocks_transfer("claimed")

    def test_provider_started_predecessor_is_never_transferred(self):
        self._prior_effect_blocks_transfer("provider_started", started=True)

    def test_cancelled_state_cannot_hide_a_previous_provider_intent(self):
        self._prior_effect_blocks_transfer("cancelled", started=True)

    def test_corrupt_predecessor_scope_does_not_authorize_new_ack(self):
        first = self._undispatched_first_ack_with_fresh_successor()
        receipts = dict(first.action_receipts)
        receipts["rate_handoff"] = {**receipts["rate_handoff"], "digest": "0" * 64}
        with connection.cursor() as cursor:
            cursor.execute("UPDATE management_igcustomerturnrevision SET action_receipts = %s WHERE id = %s",
                [json.dumps(receipts), first.pk])
        _, send = self._run()
        send.assert_not_called()
        self.assertEqual(first.delivery_effects.get().state, "planned")

    def test_transfer_rolls_back_old_cancellation_if_new_receipt_commit_fails(self):
        from management.services.ig_revision_rate_handoff import record_rate_handoff
        from management.services.instagram_bot import acquire_client_automation_lease, release_client_automation_lease

        first = self._undispatched_first_ack_with_fresh_successor()
        self._prepare()
        decide_revision_input(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.revision.refresh_from_db()
        client, lease = acquire_client_automation_lease(self.customer.pk)
        self.assertIsNotNone(client)
        native_save = IgCustomerTurnRevision.save
        def fail_new_receipt(row, *args, **kwargs):
            if row.pk == self.revision.pk and (row.action_receipts or {}).get("technical_holding"):
                raise RuntimeError("simulate crash before successor receipt commit")
            return native_save(row, *args, **kwargs)
        try:
            with patch.object(IgCustomerTurnRevision, "save", new=fail_new_receipt):
                with self.assertRaises(RuntimeError):
                    record_rate_handoff(self.revision.pk, self.token, settings_id=self.settings.pk,
                        automation_lease_token=lease)
            self.assertEqual(first.delivery_effects.get().state, "planned")
            self.revision.refresh_from_db()
            self.assertNotIn("rate_handoff", self.revision.action_receipts)
            task = IgFollowUpTask.objects.get(reason="revision_case:rate_limit_handoff")
            self.assertEqual(task.manager_context["ack_owner_revision_id"], first.pk)
            recovered = record_rate_handoff(self.revision.pk, self.token, settings_id=self.settings.pk,
                automation_lease_token=lease)
            self.assertTrue(recovered.ready, recovered.reason)
            self.assertEqual(recovered.origin, "technical_holding")
            first.refresh_from_db()
            old_effect = first.delivery_effects.get()
            from management.services.ig_revision_outbox import claim_next_effect
            self.assertFalse(claim_next_effect(first.pk, first.claim_token, old_effect.group).token)
            self.assertEqual(old_effect.state, "cancelled")
        finally:
            release_client_automation_lease(self.customer.pk, lease)

    def test_later_capped_scope_preserves_confirmed_successor_ack_proof(self):
        from management.services.ig_revision_holding import holding_receipt_valid
        from management.services.ig_response_debt import response_obligation

        self._undispatched_first_ack_with_fresh_successor()
        _, sent = self._run()
        self.assertEqual(sent.call_count, 1)
        acknowledged = self.revision
        acknowledged.refresh_from_db()
        receipt = acknowledged.action_receipts["technical_holding"].copy()
        self.assertTrue(holding_receipt_valid(acknowledged))
        self._new("А ще питання про кольори")
        _, later = self._run()
        later.assert_not_called()
        acknowledged.refresh_from_db()
        self.assertEqual(acknowledged.action_receipts["technical_holding"], receipt)
        self.assertTrue(holding_receipt_valid(acknowledged))
        self.assertFalse(response_obligation(acknowledged)["customer_ack_owed"])
        self.assertTrue(response_obligation(self.revision)["customer_ack_owed"])
        self.assertEqual(IgRevisionDeliveryEffect.objects.filter(state="sent").count(), 1)

    def test_after_committed_transfer_without_plan_fresh_source_can_ack_once(self):
        self._undispatched_first_ack_with_fresh_successor()
        unplanned = self.revision
        with patch("management.services.ig_revision_live._execute_deterministic_input",
            side_effect=RuntimeError("crash after successor receipt commit")):
            with self.assertRaisesRegex(AssertionError, "crash after successor receipt commit"):
                self._run()
        unplanned.refresh_from_db()
        self.assertIn("zero_dispatch_predecessors", unplanned.action_receipts["technical_holding"])
        self.assertFalse(unplanned.delivery_effects.exists())
        self._new("Підкажіть, будь ласка, відповідь на запитання")
        _, sent = self._run()
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(IgRevisionDeliveryEffect.objects.filter(state="sent").count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)

    def _transferred_plan_fence(self, change):
        from management.services.ig_revision_outbox import plan_revision_effects

        first = self._undispatched_first_ack_with_fresh_successor()
        def plan_then_change(*args, **kwargs):
            planned = plan_revision_effects(*args, **kwargs)
            change()
            return planned
        with patch("management.services.ig_revision_live.plan_revision_effects", side_effect=plan_then_change):
            _, send = self._run()
        send.assert_not_called()
        first.refresh_from_db()
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(first.delivery_effects.get().state, "cancelled")
        self.assertIn("zero_dispatch_predecessors", self.revision.action_receipts["technical_holding"])
        self.assertFalse(self.revision.delivery_effects.filter(provider_started_at__isnull=False).exists())
        self.assertEqual(self.source.status, "pending")

    def test_new_source_after_transferred_plan_still_blocks_physical_send(self):
        self._transferred_plan_fence(lambda: self._message("Ще новіше уточнення", "rate-transfer-newer-source"))

    def test_publication_switch_after_transferred_plan_still_blocks_physical_send(self):
        from management.models import BotPolicyPublication

        def switch():
            publication = BotPolicyPublication.objects.create(version=self.publication.version + 1, kind="publish", schema_version=1,
                snapshot=self.publication.snapshot, snapshot_hash=self.publication.snapshot_hash,
                compiler_version="instruction-set-v1", instruction_count=0)
            self.settings.active_instruction_publication = publication
            self.settings.save(update_fields=["active_instruction_publication"])
        self._transferred_plan_fence(switch)

    def test_closed_window_after_transferred_plan_still_blocks_physical_send(self):
        from contextlib import ExitStack

        with ExitStack() as changed_clock:
            def close_window():
                changed_clock.enter_context(patch("django.utils.timezone.now",
                    return_value=timezone.now() + timedelta(days=2)))
            self._transferred_plan_fence(close_window)
