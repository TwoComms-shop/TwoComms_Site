"""Native bounded worker admission: queue time must not spend recovery life."""
from datetime import timedelta
import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgCustomerTurnRevision, IgFollowUpTask, InstagramBotMessage
from management.services.ig_revision_execution import due_revision_ids
from management.services.ig_revision_live import process_pending_revisions
from management.services.ig_revision_recovery import prepare_outage_recovery
from management import tests_ig_revision_live as live_fixtures
from management import tests_ig_revision_recovery as recovery_fixtures
from management import tests_ig_revision_execution as execution_fixtures


@override_settings(IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00", GOOGLE_INDEXING_ENABLED=False)
class RevisionWorkerLivenessTests(TransactionTestCase):
    _message = live_fixtures.RevisionLiveTests._message
    setUp = live_fixtures.RevisionLiveTests.setUp
    _source = execution_fixtures.RevisionExecutionTests._source
    _collecting_revision = execution_fixtures.RevisionExecutionTests._collecting_revision

    def test_busy_clients_are_filtered_before_due_limit(self):
        now = timezone.now()
        self.customer.automation_lease_token = "other-worker"
        self.customer.automation_lease_until = now + timedelta(minutes=3)
        self.customer.save(update_fields=["automation_lease_token", "automation_lease_until"])
        source = self._source("17840000000002", "runnable")
        _, runnable = self._collecting_revision([source], now=now)
        self.assertEqual(due_revision_ids(now=now, limit=1), [runnable.pk])

    def test_busy_preparation_and_provider_waits_do_not_fill_bounded_lanes(self):
        from management.services.ig_revision_recovery import due_preparation_recovery_ids, due_recovery_revision_ids

        now = timezone.now()
        IgClient.objects.filter(pk=self.customer.pk).update(automation_lease_token="other-worker",
            automation_lease_until=now + timedelta(minutes=3))
        source = self._source("17840000000004", "runnable-recovery")
        _, runnable = self._collecting_revision([source], now=now)
        for code, selector in (("preparation_recovery_due", due_preparation_recovery_ids),
            ("recovery_due", due_recovery_revision_ids)):
            IgCustomerTurnRevision.objects.filter(pk__in=[self.revision.pk, runnable.pk]).update(
                recovery_state="waiting", recovery_code=code, recovery_due_at=now)
            self.assertEqual(selector(now=now, limit=1, available_only=True), [runnable.pk])

    def test_native_worker_serves_fresh_source_beside_old_manual_debt(self):
        from management.services.ig_revision_execution import prepare_revision
        from management.services.ig_response_debt import park_manual_revision

        prepared = prepare_revision(self.revision.pk, lambda **kwargs: None)
        self.assertTrue(prepared.ready)
        self.revision.refresh_from_db()
        task = park_manual_revision(self.revision, "captured_state_exceeds_budget")
        old_id = self.revision.pk
        fresh = self._message("Вітаю", "fresh-after-manual")
        _, revision = self._collecting_revision([fresh])
        busy = self._source("17840000000003", "busy")
        _, busy_revision = self._collecting_revision([busy])
        IgCustomerTurnRevision.objects.filter(pk=busy_revision.pk).update(
            quiet_deadline=timezone.now() - timedelta(minutes=1))
        IgClient.objects.filter(pk=busy.client_id).update(automation_lease_token="busy",
            automation_lease_until=timezone.now() + timedelta(minutes=3))
        self.settings.ai_enabled = False
        self.settings.trigger_text = "Вітаю"
        self.settings.reply_text = "Вітаю! Чим можу допомогти?"
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        with patch("management.services.instagram_bot.get_page_token", return_value="test-token"), \
            patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "fresh-sent"}))) as http, \
            patch("management.services.instagram_bot._register_outgoing_message"), \
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("static flow must not generate")):
            handled = process_pending_revisions(self.settings, max_items=1)
        self.assertEqual(handled, 1)
        self.assertEqual(http.call_count, 1)
        revision.refresh_from_db()
        fresh.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(revision.state, "processed")
        self.assertEqual(fresh.status, "done")
        self.assertEqual(self.source.status, "pending")
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{old_id}").count(), 1)
        task.refresh_from_db()
        self.assertNotEqual(task.status, "completed")

    def test_historical_manager_backfill_does_not_cancel_unstarted_preparation(self):
        from management.services.ig_revision_recovery import schedule_revision_recovery

        InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="manager", source="poll_history", mid="old-manager-preparation",
            provider_namespace=self.source.provider_namespace, status="done", text="Old manager answer",
            provider_created_at=self.source.created_at - timedelta(days=1))
        result = schedule_revision_recovery(self.revision.pk,
            now=self.revision.overall_deadline + timedelta(seconds=1))
        self.assertEqual(result.state, "waiting", result.reason)
        self.assertEqual(result.reason, "preparation_recovery_due")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionRecoveryAdmissionLivenessTests(TransactionTestCase):
    _message = live_fixtures.RevisionLiveTests._message
    setUp = recovery_fixtures.RevisionRecoveryTests.setUp
    _admit = recovery_fixtures.RevisionRecoveryTests._admit
    _graph = recovery_fixtures.RevisionRecoveryTests._graph
    _queue = recovery_fixtures.RevisionRecoveryTests._queue
    _generate = live_fixtures.RevisionLiveTests._generate
    _successful_proposal = recovery_fixtures.RevisionRecoveryTests._successful_proposal
    _source = execution_fixtures.RevisionExecutionTests._source
    _collecting_revision = execution_fixtures.RevisionExecutionTests._collecting_revision
    _recover = recovery_fixtures.RevisionRecoveryTests._recover

    def _resume_history(self, role, *, source="poll_history"):
        from management.services.ig_revision_recovery import (
            EXECUTION_RESUME_KEY, execution_resume_is_current, schedule_revision_recovery)

        now = self._successful_proposal()
        schedule_revision_recovery(self.revision.pk, now=now)
        self.assertEqual(self._recover(self.revision.pk, now=now).state, "execution")
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[EXECUTION_RESUME_KEY].copy()
        InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role=role, source=source, mid=f"old-{role}",
            provider_namespace=self.source.provider_namespace, status="done", text="Older history",
            provider_created_at=self.source.created_at - timedelta(days=1))
        self.assertTrue(execution_resume_is_current(self.revision, now=now))
        self.assertEqual(self.revision.action_receipts[EXECUTION_RESUME_KEY], receipt)

    def test_historical_user_backfill_does_not_invalidate_issued_resume(self):
        self._resume_history("user")

    def test_historical_manager_backfill_does_not_invalidate_issued_resume(self):
        self._resume_history("manager")

    def test_old_provider_manager_echo_inserted_late_does_not_invalidate_resume(self):
        self._resume_history("manager", source="echo")

    def _current_manager_blocks_resume(self, source, status, send_state=""):
        from management.services.ig_revision_recovery import execution_resume_is_current, schedule_revision_recovery

        now = self._successful_proposal()
        schedule_revision_recovery(self.revision.pk, now=now)
        self.assertEqual(self._recover(self.revision.pk, now=now).state, "execution")
        self.revision.refresh_from_db()
        InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="manager", source=source, mid=f"current-{source}",
            provider_namespace=self.source.provider_namespace if source == "echo" else "",
            status=status, send_state=send_state, text="Current manager reply", provider_created_at=now)
        self.assertFalse(execution_resume_is_current(self.revision, now=now))

    def test_current_manager_echo_still_fences_issued_resume(self):
        self._current_manager_blocks_resume("echo", "done")

    def test_inflight_authenticated_human_reply_still_fences_issued_resume(self):
        self._current_manager_blocks_resume("human_reply", "processing", "sending")

    def test_historical_manager_backfill_does_not_cancel_provider_recovery(self):
        self._admit()
        self._graph()
        now = self._queue()
        InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="manager", source="poll_history", mid="old-manager-provider",
            provider_namespace=self.source.provider_namespace, status="done", text="Old manager answer",
            provider_created_at=self.source.created_at - timedelta(days=1))
        result = self._recover(self.revision.pk, now=now)
        self.assertEqual(result.state, "spawned", result.reason)

    @override_settings(IG_REVISION_EXECUTION_ENABLED=True,
        IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00")
    def test_saved_proposal_outside_worker_limit_receives_lifetime_only_when_served(self):
        from management.models import GeminiRequest, IgRevisionDeliveryEffect
        from management.services.ig_revision_recovery import EXECUTION_RESUME_KEY

        queued_at = self._successful_proposal()
        deadline = self.revision.overall_deadline
        proposal_digest = self.revision.generation_proposal_digest
        earlier = self._source("17840000000005", "earlier-work", text="👍")
        _, first = self._collecting_revision([earlier], now=queued_at)
        IgCustomerTurnRevision.objects.filter(pk=first.pk).update(
            quiet_deadline=self.revision.quiet_deadline - timedelta(seconds=1))
        with patch("django.utils.timezone.now", return_value=queued_at):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 1)
        self.revision.refresh_from_db()
        self.assertNotIn(EXECUTION_RESUME_KEY, self.revision.action_receipts)
        self.assertEqual(self.revision.recovery_state, "waiting")
        self.assertFalse(IgRevisionDeliveryEffect.objects.filter(revision=self.revision).exists())
        served_at = queued_at + timedelta(seconds=120)
        with patch("django.utils.timezone.now", return_value=served_at), \
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("saved winner must not regenerate")) as generate, \
            patch("management.services.instagram_bot.get_page_token", return_value="test-token"), \
            patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "saved-proposal-sent"}))) as http, \
            patch("management.services.instagram_bot._register_outgoing_message"):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 1)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.revision.overall_deadline, deadline)
        self.assertEqual(self.revision.generation_proposal_digest, proposal_digest)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        receipt = self.revision.action_receipts[EXECUTION_RESUME_KEY]
        self.assertEqual(timezone.datetime.fromisoformat(receipt["issued_at"]), served_at)
        self.assertEqual(timezone.datetime.fromisoformat(receipt["expires_at"]), served_at + timedelta(seconds=45))

    def test_provider_recovery_without_worker_slot_does_not_mint_child(self):
        self._admit()
        self._graph()
        due = self._queue()
        deadline = self.revision.overall_deadline
        result = prepare_outage_recovery(self.revision.pk, now=due)
        self.assertEqual(result.state, "waiting")
        self.assertFalse(result.child_id)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(prepare_outage_recovery(self.revision.pk, now=due + timedelta(seconds=90)).state, "waiting")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.overall_deadline, deadline)

    def test_wrong_or_expired_worker_lease_cannot_mint_recovery(self):
        self._admit()
        self._graph()
        due = self._queue()
        IgClient.objects.filter(pk=self.customer.pk).update(
            automation_lease_token="actual-worker", automation_lease_until=due + timedelta(minutes=3))
        self.assertEqual(prepare_outage_recovery(self.revision.pk, now=due,
            automation_lease_token="wrong-worker").state, "waiting")
        self.assertEqual(prepare_outage_recovery(self.revision.pk, now=due + timedelta(minutes=4),
            automation_lease_token="actual-worker").state, "waiting")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    def test_classification_never_spends_lifetime_even_with_matching_client_lease(self):
        self._admit()
        self._graph()
        due = self._queue()
        IgClient.objects.filter(pk=self.customer.pk).update(
            automation_lease_token="worker", automation_lease_until=due + timedelta(minutes=3))
        result = prepare_outage_recovery(self.revision.pk, now=due,
            automation_lease_token="worker", classify_only=True)
        self.assertEqual(result.state, "waiting")
        self.assertFalse(result.child_id)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    @override_settings(IG_REVISION_EXECUTION_ENABLED=True,
        IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00")
    def test_native_worker_cancels_paused_recovery_without_acquiring_client_lease(self):
        self._admit()
        self._graph()
        due = self._queue()
        IgClient.objects.filter(pk=self.customer.pk).update(bot_paused=True)
        with patch("django.utils.timezone.now", return_value=due):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 0)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_state, "cancelled")
        self.assertEqual(self.revision.recovery_code, "recovery_permission_changed")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    @override_settings(IG_REVISION_EXECUTION_ENABLED=True,
        IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00")
    def test_native_worker_mints_provider_child_only_when_it_can_execute(self):
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution

        self._admit()
        self._graph()
        due = self._queue()
        original_deadline = self.revision.overall_deadline
        served_at = due + timedelta(minutes=2)
        IgClient.objects.filter(pk=self.customer.pk).update(automation_lease_token="previous-worker",
            automation_lease_until=served_at)
        with patch("django.utils.timezone.now", return_value=due):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 0)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        budget = inspect_revision_provider_execution(self.revision, now=served_at)
        self.assertTrue(budget.ready, budget.reason)
        with patch("django.utils.timezone.now", return_value=served_at), \
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate, \
            patch("management.services.instagram_bot.get_page_token", return_value="test-token"), \
            patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "recovery-sent"}))) as http, \
            patch("management.services.instagram_bot._register_outgoing_message"):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 1)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        child = IgCustomerTurnRevision.objects.get(parent=self.revision)
        self.assertEqual(child.overall_deadline, served_at + timedelta(seconds=45))
        self.assertEqual(child.state, "processed")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.overall_deadline, original_deadline)
        self.assertEqual(child.action_receipts["recovery_lineage"]["round"], 1)
