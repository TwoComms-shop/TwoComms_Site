"""Exact manager dispositions stop work without inventing customer delivery."""
from datetime import timedelta

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_human_reply as human_fixtures
from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_preference_fallback as fallback_fixtures
from management import tests_ig_revision_reply_resilience as resilience_fixtures
from management.models import (
    AdminAuditLog, IgCustomerTurn, IgCustomerTurnRevision, IgFollowUpTask,
    IgTurnMessage, IgTurnRevisionSource, InstagramBotMessage,
)
from management.services.ig_response_debt import (
    paused_response_obligation_q, record_reply_debt,
    review_reply_debt, settle_human_reply_obligation,
)
from management.services.ig_revision_execution import expired_revision_debt_ids
from management.services.ig_revision_outbox import _digest
from management.services.ig_turn_revisions import _source_payload


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class ManagerWaitLifecycleTests(TransactionTestCase):
    setUp = human_fixtures.HumanReplyCommandTests.setUp
    _physical_http = human_fixtures.HumanReplyCommandTests._physical_http

    def _revision(self, source=None):
        source = source or self.inbound
        now = timezone.now()
        IgCustomerTurnRevision.objects.filter(client=self.customer, active_slot=1).update(active_slot=None)
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source,
            window_started_at=now, window_deadline=now)
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        payload = _source_payload(source, ordinal=1)
        snapshot = {"sources": [payload]}
        revision = IgCustomerTurnRevision.objects.create(client=self.customer, turn=turn,
            revision=IgCustomerTurnRevision.objects.filter(client=self.customer).count() + 1,
            active_slot=1, state="claimed", snapshot_digest=_digest(snapshot), bundle_snapshot=snapshot,
            quiet_started_at=now, quiet_deadline=now, quiet_cap_at=now,
            overall_deadline=now-timedelta(minutes=1), source_count=1,
            permission_epoch=self.customer.reply_permission_epoch,
            claim_token="expired-worker", claimed_at=now-timedelta(minutes=2), lease_until=now-timedelta(minutes=1))
        IgTurnRevisionSource.objects.create(revision=revision, message=source, ordinal=1,
            role="user", source_namespace=source.provider_namespace, text=source.text,
            source_digest=payload["source_digest"])
        return revision

    def _new_source(self):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", provider_namespace=self.inbound.provider_namespace,
            mid="new-independent-question", text="А яка вартість доставки?", status="pending",
            provider_created_at=timezone.now())

    def _send(self):
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command
        accepted = create_human_reply_command(self.customer.pk, actor=self.actor,
            text="Ціна цієї футболки 900 грн.", operation_id="98111111-1111-1111-1111-111111111111")
        return dispatch_human_reply_command(accepted.command.pk)

    def test_handled_is_durable_and_new_independent_source_still_runs(self):
        from management.services.ig_manual_resume import _latest_unanswered_reference
        from management.services.ig_revision_recovery import _set_state, schedule_revision_recovery, due_recovery_revision_ids

        revision = self._revision()
        task = record_reply_debt(revision, "generation_failed")
        receipts = revision.action_receipts.copy()
        first = review_reply_debt(self.customer.pk, task.pk, actor=self.actor, expected_revision_id=revision.pk)
        revision.refresh_from_db()
        settled_at = revision.updated_at
        self.assertTrue(first["ok"])
        self.assertTrue(IgCustomerTurnRevision.objects.filter(pk=revision.pk).filter(paused_response_obligation_q()).exists())
        self.assertEqual(_latest_unanswered_reference(self.customer)[2], "operator_handled")
        self.assertEqual(revision.action_receipts, receipts)
        self.assertIsNone(revision.processed_at)
        for _ in range(3):
            self.assertNotIn(revision.pk, expired_revision_debt_ids())
            self.assertNotIn(revision.pk, due_recovery_revision_ids())
            self.assertEqual(schedule_revision_recovery(revision.pk).reason, "operator_handled")
            self.assertEqual(_set_state(revision, "waiting", "recovery_due", due_at=timezone.now()).reason,
                             "operator_handled")
            self.assertTrue(review_reply_debt(self.customer.pk, task.pk, actor=self.actor,
                expected_revision_id=revision.pk)["idempotent"])
        revision.refresh_from_db()
        self.assertEqual(revision.updated_at, settled_at)
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_reply_debt_reviewed").count(), 1)
        newer = self._revision(self._new_source())
        self.assertIn(newer.pk, expired_revision_debt_ids())
        self.assertNotIn(revision.pk, expired_revision_debt_ids())
        self.assertNotEqual(_latest_unanswered_reference(self.customer)[2], "operator_handled")
        self.http.assert_not_called()

    def test_confirmed_human_command_settles_exact_context_and_replay_is_idle(self):
        from management.services.ig_human_reply import dispatch_human_reply_command

        revision = self._revision()
        task = record_reply_debt(revision, "generation_failed")
        other = IgFollowUpTask.objects.create(client=self.customer, due_at=timezone.now(),
            kind="manager_task", reason="revision_case:execution_debt", status="skipped",
            event_key="ig-revision-debt:unrelated", event_payload={"revision_id": 999, "source_message_ids": [999]})
        business = IgFollowUpTask.objects.create(client=self.customer, due_at=timezone.now(),
            kind="manager_task", reason="revision_case:product_information_review", status="skipped")
        command = self._send()
        self.assertEqual(command.state, "sent")
        task.refresh_from_db(); other.refresh_from_db(); business.refresh_from_db(); revision.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.manager_context["reply_resolution"]["command_id"], command.pk)
        self.assertEqual(task.manager_context["reply_resolution"]["source_message_ids"], [self.inbound.pk])
        self.assertEqual(revision.recovery_code, "manager_reply_confirmed")
        self.assertIsNone(revision.processed_at)
        self.assertEqual(other.status, "skipped")
        self.assertEqual(business.status, "skipped")
        self.assertEqual(settle_human_reply_obligation(command.pk), 0)
        dispatch_human_reply_command(command.pk)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_reply_debt_manager_replied").count(), 1)

    def test_unknown_human_delivery_never_settles_reply_debt(self):
        revision = self._revision()
        task = record_reply_debt(revision, "generation_failed")
        self.provider_exception = TimeoutError("fixture timeout")
        command = self._send()
        self.assertEqual(command.state, "unknown")
        self.assertEqual(settle_human_reply_obligation(command.pk), 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertNotIn("reply_resolution", task.manager_context)

    def test_definite_human_failure_never_settles_reply_debt(self):
        revision = self._revision()
        task = record_reply_debt(revision, "generation_failed")
        self.http.side_effect = None
        self.http.return_value = (400, '{"error":{"message":"fixture rejection","code":100}}')
        command = self._send()
        self.assertEqual(command.state, "definite_failed")
        self.assertEqual(settle_human_reply_obligation(command.pk), 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_manager_reply_does_not_close_unknown_bot_transport(self):
        revision = self._revision()
        task = record_reply_debt(revision, "delivery_unknown")
        command = self._send()
        self.assertEqual(command.state, "sent")
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertEqual(settle_human_reply_obligation(command.pk), 0)

    def test_sent_label_without_whole_confirmed_human_plan_cannot_close_debt(self):
        from management.ig_bot_models import HumanReplyCommand
        revision = self._revision()
        task = record_reply_debt(revision, "generation_failed")
        self.provider_exception = TimeoutError("fixture timeout")
        command = self._send()
        HumanReplyCommand.objects.filter(pk=command.pk).update(state="sent", provider_message_ids=["fabricated"])
        self.assertEqual(settle_human_reply_obligation(command.pk), 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_handled_old_request_does_not_close_new_context_command_debt(self):
        old = self._revision()
        old_task = record_reply_debt(old, "generation_failed")
        review_reply_debt(self.customer.pk, old_task.pk, actor=self.actor, expected_revision_id=old.pk)
        newer = self._revision(self._new_source())
        new_task = record_reply_debt(newer, "generation_failed")
        command = self._send()
        self.assertEqual(command.context_message_id, newer.sources.get().message_id)
        new_task.refresh_from_db(); old_task.refresh_from_db(); old.refresh_from_db()
        self.assertEqual(new_task.status, "completed")
        self.assertEqual(old_task.status, "cancelled")
        self.assertEqual(old.recovery_code, "operator_handled")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class DeliveredCustomerWaitTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute
    _generate = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._generate
    _fit_fixture = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._fit_fixture
    _record = fallback_fixtures.RevisionPreferenceFallbackIntegrationTests._record
    _selected_product = resilience_fixtures.RevisionReplyResilienceTests._selected_product

    def test_real_delivered_selector_wait_is_not_expired_executable_debt(self):
        self.assertTrue(self._fit_fixture().ready)
        result, generate, http = self._execute()
        self.assertEqual(result.reasons, ("waiting_on_customer",))
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        before = self.revision.updated_at
        self.assertEqual(self.revision.recovery_code, "waiting_on_customer")
        future = self.revision.overall_deadline + timedelta(minutes=1)
        for _ in range(3):
            self.assertNotIn(self.revision.pk, expired_revision_debt_ids(now=future))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.updated_at, before)
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:execution_debt").exists())
        generate.assert_not_called()

    def test_handled_unknown_delivery_stops_execution_but_keeps_reconciliation(self):
        from django.contrib.auth import get_user_model
        from management.services.ig_response_debt import park_manual_revision
        from management.services.ig_technical_debt import technical_debt_snapshot

        self.assertTrue(self._fit_fixture().ready)
        _, _, http = self._execute([TimeoutError()])
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "unknown")
        task = park_manual_revision(self.revision, "delivery_unknown")
        actor = get_user_model().objects.create_superuser(username="handled-unknown", password="fixture")
        self.assertTrue(review_reply_debt(self.customer.pk, task.pk, actor=actor,
            expected_revision_id=self.revision.pk)["ok"])
        effect.refresh_from_db(); self.source.refresh_from_db(); self.revision.refresh_from_db()
        self.assertEqual(effect.state, "unknown")
        self.assertEqual(self.source.status, "pending")
        self.assertIsNone(self.revision.processed_at)
        self.assertNotIn(self.revision.pk, expired_revision_debt_ids(
            now=self.revision.overall_deadline + timedelta(minutes=1)))
        debt = technical_debt_snapshot()
        self.assertTrue(any(case["reason"] == "canonical_delivery_unknown"
            and effect.pk in case["sample_ids"] for case in debt["cases"]))
        _, generation, replay_http = self._execute()
        generation.assert_not_called()
        replay_http.assert_not_called()

    @override_settings(IG_REVISION_OPTIONAL_HOLDING_ENABLED=True)
    def test_public_resume_after_confirmed_holding_preserves_substantive_request(self):
        from django.contrib.auth import get_user_model
        from management.services.ig_manual_resume import resume_client_automation

        self._selected_product(size="M")
        self._fit_fixture()
        result, _, http = self._execute()
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.customer.refresh_from_db()
        self.customer.bot_paused = True
        self.customer.save(update_fields=["bot_paused"])
        actor = get_user_model().objects.create_superuser(username="resume-after-holding", password="fixture")
        with override_settings(IG_REVISION_EXECUTION_ENABLED=True,
                               IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00"):
            resumed = resume_client_automation(self.customer.pk, actor=actor)
        self.assertTrue(resumed.successor_created, resumed.successor_reason)
        self.assertEqual(resumed.successor_source_message_id, self.source.pk)
