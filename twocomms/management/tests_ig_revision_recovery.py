from datetime import timedelta
import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import BotPolicyPublication, GeminiRequest, GeminiRequestAttempt, IgCustomerTurnRevision, IgProviderIncident, IgRevisionDeliveryEffect, InstagramBotMessage
from management.services.ig_revision_execution import prepare_revision
from management.services.ig_revision_live import RevisionGenerationBoundary
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_revision_provider_execution import revision_provider_continuation
from management.tests_gemini_accounting_shadow import _raw_plan
from management.services.ig_revision_recovery import (
    due_recovery_revision_ids, prepare_outage_recovery,
    record_generation_admission, schedule_revision_recovery,
    EXECUTION_RESUME_KEY, execution_resume_is_current,
)
from management import tests_ig_revision_live as live_fixtures


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionRecoveryTests(TransactionTestCase):
    reset_sequences = True
    _message = live_fixtures.RevisionLiveTests._message

    def setUp(self):
        live_fixtures.RevisionLiveTests.setUp(self)
        prepared = prepare_revision(self.revision.pk, lambda **kwargs: None)
        self.assertTrue(prepared.ready, prepared.reason)
        self.token = prepared.execution_token
        self.revision.refresh_from_db()
        self.publication_binding = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        with patch("management.services.ig_provider_dispatch_budget.ProviderDispatchBudget._is_scarce_model", return_value=False):
            frozen = revision_provider_continuation(self.revision.pk, self.token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, candidate_plan=_raw_plan())
        self.assertTrue(frozen.ready, frozen.reason)
        self.revision.refresh_from_db()
        self.no_http = patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("recovery producer must not send"))
        self.no_http.start()
        self.addCleanup(self.no_http.stop)

    def _admit(self, revision=None, token=None, now=None):
        revision = revision or self.revision
        authority = RevisionGenerationBoundary(revision, token or self.token, self.settings, self.publication_binding).baseline
        result = record_generation_admission(
            revision.pk, token or self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.publication_binding, authority=authority, now=now,
        )
        self.assertTrue(result.ready, result.reason)
        revision.refresh_from_db()
        return result

    def _graph(self, revision=None, outcome="failed"):
        from management.services.gemini_accounting_runtime import revision_request_execution

        revision = revision or self.revision
        kwargs = {}
        if any(field.name == "source_execution_key" for field in GeminiRequest._meta.fields):
            kwargs["source_execution_key"] = f"ig-revision:{revision.pk}"
        with revision_request_execution(revision.pk, revision.claim_token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch), patch("management.services.gemini_accounting_runtime.timezone.now", return_value=revision.claimed_at):
            return GeminiRequest.objects.create(
                request_id=f"recovery-graph-{revision.pk}", lane="live", task_class="simple_live",
                logical_turn_id=f"ig-revision:{revision.pk}", client_id=self.customer.pk,
                source_message_id=self.source.pk, terminal_resolution=outcome,
                accounting_mode="shadow", **kwargs,
            )

    def _queue(self, revision=None):
        revision = revision or self.revision
        queued_at = revision.overall_deadline + timedelta(seconds=1)
        result = schedule_revision_recovery(revision.pk, now=queued_at)
        self.assertEqual(result.state, "waiting")
        revision.refresh_from_db()
        self.assertIn(revision.pk, due_recovery_revision_ids(now=revision.recovery_due_at))
        return revision.recovery_due_at

    def test_admission_is_idempotent_and_source_bound(self):
        self._admit()
        stored = self.revision.action_receipts["generation_admission"]
        self.assertEqual(stored["logical_turn_id"], f"ig-revision:{self.revision.pk}")
        self.assertEqual(stored["snapshot_digest"], self.revision.snapshot_digest)
        self.assertEqual(self._admit().reason, "existing_admission")
        self.assertNotIn(self.source.text, str(stored))

    def test_failed_request_creates_one_fresh_original_source_successor(self):
        self._admit()
        self._graph()
        deadline = self.revision.overall_deadline
        snapshot = self.revision.bundle_snapshot
        original_ids = list(self.revision.sources.values_list("message_id", flat=True))
        now = self._queue()
        result = prepare_outage_recovery(self.revision.pk, now=now)
        self.assertEqual(result.state, "spawned", result.reason)
        child = IgCustomerTurnRevision.objects.get(pk=result.child_id)
        self.assertEqual(child.origin, "outage_recovery")
        self.assertEqual(child.overall_deadline, now + timedelta(seconds=45))
        self.assertEqual(child.bundle_snapshot, snapshot)
        self.assertEqual(list(child.sources.values_list("message_id", flat=True)), original_ids)
        self.assertEqual(child.action_receipts["recovery_lineage"]["round"], 1)
        self.assertFalse(child.generation_proposal_digest)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.revision.overall_deadline, deadline)
        self.assertEqual(self.source.status, InstagramBotMessage.Status.PENDING)
        self.assertEqual(InstagramBotMessage.objects.filter(role="user").count(), 1)
        duplicate = prepare_outage_recovery(self.revision.pk, now=now)
        self.assertEqual(duplicate.child_id, child.pk)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 2)

    def test_open_quota_incident_defers_without_spending_generation(self):
        self._admit()
        self._graph()
        now = self._queue()
        incident = IgProviderIncident.objects.create(
            role="chat", failure_class="quota", fingerprint="chat:quota",
            active_fingerprint="chat:quota", state="open",
            opened_at=now - timedelta(hours=2), last_failure_at=now - timedelta(hours=2),
        )
        for _ in range(3):
            result = prepare_outage_recovery(self.revision.pk, now=now)
            self.assertEqual(result.reason, "recovery_provider_incident_open")
            self.revision.refresh_from_db()
            self.assertEqual(self.revision.recovery_due_at, now + timedelta(seconds=45))
            now = self.revision.recovery_due_at
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        incident.state = "closed"
        incident.save(update_fields=["state"])
        self.assertEqual(prepare_outage_recovery(self.revision.pk, now=now).state, "spawned")

    @patch("management.services.ig_revision_recovery.RETRY_DELAYS", (45,))
    def test_secondary_generation_bound_does_not_replace_global_http_budget(self):
        revision, token, clock = self.revision, self.token, timezone.now()
        for number in range(1, 9):
            self._admit(revision, token, now=clock)
            self._graph(revision)
            now = self._queue(revision)
            result = prepare_outage_recovery(revision.pk, now=now)
            self.assertEqual(result.state, "spawned", result.reason)
            revision = IgCustomerTurnRevision.objects.get(pk=result.child_id)
            self.assertEqual(revision.action_receipts["recovery_lineage"]["round"], number)
            prepared = prepare_revision(revision.pk, lambda **kwargs: None, now=now)
            self.assertTrue(prepared.ready, prepared.reason)
            token, clock = prepared.execution_token, now
            revision.refresh_from_db()
        self._admit(revision, token, now=clock)
        self._graph(revision)
        now = self._queue(revision)
        result = prepare_outage_recovery(revision.pk, now=now)
        self.assertEqual(result.state, "manual")
        self.assertEqual(result.reason, "recovery_generations_exhausted")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 9)

    def test_success_graph_without_proposal_requires_reconciliation(self):
        self._admit()
        self._graph(outcome="succeeded")
        result = prepare_outage_recovery(self.revision.pk, now=self._queue())
        self.assertEqual(result.state, "manual")
        self.assertEqual(result.reason, "recovery_success_result_missing")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    def _successful_proposal(self):
        from management.services.ig_revision_live import execute_claimed_revision

        with (
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=lambda *args, **kwargs: live_fixtures.RevisionLiveTests._generate(self, *args, **kwargs)),
            patch("management.services.instagram_bot.get_page_token", return_value=""),
        ):
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(result.reasons, ("provider_not_configured",))
        self.revision.refresh_from_db()
        self.assertTrue(self.revision.generation_proposal_digest)
        return self.revision.overall_deadline + timedelta(seconds=1)

    def _resume_readiness(self, token, now):
        from management.services.ig_revision_outbox import pre_winner_readiness
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings

        authority = self.revision.generation_proposal["authority"]
        return pre_winner_readiness(self.revision.pk, token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=self.publication_binding,
            fact_bindings=authority["fact_bindings"], offer_bindings=authority["offer_bindings"],
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now)

    @override_settings(
        IG_REVISION_EXECUTION_ENABLED=True,
        IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
    )
    def test_last_budget_winner_resumes_same_proposal_without_generation(self):
        from management.services.ig_revision_live import process_pending_revisions
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution

        now = self._successful_proposal()
        graph = GeminiRequest.objects.get()
        GeminiRequestAttempt.objects.filter(pk=graph.winner_attempt_id).update(provider_started_at=graph.created_at, dispatch_pacific_day=graph.created_at.date())
        for number in range(2, 9):
            GeminiRequestAttempt.objects.create(request_graph=graph, request_id=graph.request_id,
                role="chat", key_name="GEMINI_API", model="gemini-3.7-flash", attempt_index=number,
                candidate_index=number, provider_started_at=graph.created_at, dispatch_pacific_day=graph.created_at.date(), fsm_state="failed", outcome="transient",
                logical_turn_id=graph.logical_turn_id, client_id=graph.client_id,
                source_message_id=graph.source_message_id, lane="live", accounting_mode="shadow")
        self.assertEqual(inspect_revision_provider_execution(self.revision, now=now).reason, "provider_dispatch_budget")
        original_deadline, original_proposal = self.revision.overall_deadline, self.revision.generation_proposal_digest
        with (
            patch("management.services.ig_revision_live.timezone.now", return_value=now),
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
            patch("management.services.instagram_bot.get_page_token", return_value="memory-token"),
            patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "resumed-same-proposal"}))) as send,
            patch("management.services.instagram_bot._register_outgoing_message"),
        ):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 1)
        generate.assert_not_called()
        self.assertEqual(send.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.revision.overall_deadline, original_deadline)
        self.assertEqual(self.revision.generation_proposal_digest, original_proposal)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 8)

    def test_resume_receipt_is_once_claim_is_cas_and_provider_deadline_stays_closed(self):
        from management.services.ig_revision_live import _reclaim_execution
        from management.services.gemini_accounting_runtime import _RevisionExecution, _revision_execution_valid

        now = self._successful_proposal()
        self.assertEqual(schedule_revision_recovery(self.revision.pk, now=now).state, "execution")
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[EXECUTION_RESUME_KEY]
        self.assertEqual(self.revision.claim_token, "")
        self.assertIn("revision_not_current", self._resume_readiness(self.token, now).reasons)
        self.assertEqual(schedule_revision_recovery(self.revision.pk, now=now + timedelta(seconds=1)).state, "execution")
        with patch("management.services.ig_revision_live.timezone.now", return_value=now):
            claimed, token = _reclaim_execution(self.revision.pk)
            self.assertIsNotNone(claimed)
            self.assertEqual(_reclaim_execution(self.revision.pk), (None, ""))
            self.assertFalse(_revision_execution_valid(_RevisionExecution(self.revision.pk, token, self.settings.pk, self.settings.reply_permission_epoch), source_message_id=self.source.pk, client_id=self.customer.pk, lane="live", logical_turn_id=f"ig-revision:{self.revision.pk}"))
        self.assertTrue(self._resume_readiness(token, now).ready)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[EXECUTION_RESUME_KEY], receipt)
        with self.assertRaises(ValueError):
            self.revision.action_receipts[EXECUTION_RESUME_KEY] = {**receipt, "expires_at": (now + timedelta(minutes=5)).isoformat()}
            self.revision.save(update_fields=["action_receipts"])

    def test_resume_expiry_is_not_renewed_and_calls_no_provider(self):
        from management.services.ig_revision_live import _reclaim_execution, execute_claimed_revision

        now = self._successful_proposal()
        schedule_revision_recovery(self.revision.pk, now=now)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[EXECUTION_RESUME_KEY]
        expires = timezone.datetime.fromisoformat(receipt["expires_at"])
        with (
            patch("management.services.ig_revision_live.timezone.now", return_value=expires),
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
            patch("management.services.instagram_bot._provider_http") as send,
        ):
            self.assertEqual(_reclaim_execution(self.revision.pk), (None, ""))
            self.assertEqual(execute_claimed_revision(self.revision.pk, self.token, self.settings).state, "blocked")
            self.assertEqual(schedule_revision_recovery(self.revision.pk).reason, "proposal_execution_resume_expired")
        generate.assert_not_called()
        send.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[EXECUTION_RESUME_KEY], receipt)

    def test_active_client_lease_defers_receipt_without_burning_execution_window(self):
        now = self._successful_proposal()
        self.customer.automation_lease_token = "original-worker"
        self.customer.automation_lease_until = now + timedelta(seconds=100)
        self.customer.save(update_fields=["automation_lease_token", "automation_lease_until"])
        result = schedule_revision_recovery(self.revision.pk, now=now)
        self.assertEqual(result.reason, "proposal_execution_lease_wait")
        self.revision.refresh_from_db()
        self.assertNotIn(EXECUTION_RESUME_KEY, self.revision.action_receipts)
        due = self.revision.recovery_due_at
        self.assertEqual(prepare_outage_recovery(self.revision.pk, now=due).state, "execution")
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[EXECUTION_RESUME_KEY]
        self.assertEqual(timezone.datetime.fromisoformat(receipt["expires_at"]), due + timedelta(seconds=45))

    def test_invalid_winner_proof_does_not_issue_resume_receipt(self):
        now = self._successful_proposal()
        GeminiRequestAttempt.objects.update(winner_claimed=False)
        result = schedule_revision_recovery(self.revision.pk, now=now)
        self.assertEqual(result.reason, "recovery_success_result_missing")
        self.revision.refresh_from_db()
        self.assertNotIn(EXECUTION_RESUME_KEY, self.revision.action_receipts)

    def test_automatic_child_resume_keeps_and_checks_original_root_reference(self):
        from management.services.ig_turn_revisions import create_refresh_successor
        from management.services.ig_revision_provider_execution import REFERENCE_KEY

        self._successful_proposal()
        root_id = self.revision.pk
        child = create_refresh_successor(self.revision.pk, self.token, reason="publication_changed")
        self.assertTrue(child.created, child.reason)
        self.revision = child.revision
        prepared = prepare_revision(self.revision.pk, lambda **kwargs: None)
        self.assertTrue(prepared.ready, prepared.reason)
        self.token = prepared.execution_token
        self.revision.refresh_from_db()
        now = self._successful_proposal()
        self.assertEqual(schedule_revision_recovery(self.revision.pk, now=now).state, "execution")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[EXECUTION_RESUME_KEY]["root_revision_id"], root_id)
        self.assertTrue(execution_resume_is_current(self.revision, now=now))
        receipts = {**self.revision.action_receipts, REFERENCE_KEY: {"root_revision_id": root_id, "manifest_digest": "0" * 64}}
        with self.assertRaises(ValueError):
            IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(action_receipts=receipts)
        # Even a corrupted caller snapshot fails the execution proof.
        self.revision.action_receipts = receipts
        self.assertFalse(execution_resume_is_current(self.revision, now=now))

    def test_resume_cas_rejects_epoch_supersession_new_source_and_unknown_send(self):
        from management.services.ig_revision_live import _reclaim_execution

        now = self._successful_proposal()
        schedule_revision_recovery(self.revision.pk, now=now)
        with patch("management.services.ig_revision_live.timezone.now", return_value=now):
            _revision, token = _reclaim_execution(self.revision.pk)
        self.assertTrue(self._resume_readiness(token, now).ready)
        type(self.customer).objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1)
        self.assertIn("client_permission_changed", self._resume_readiness(token, now).reasons)
        type(self.customer).objects.filter(pk=self.customer.pk).update(reply_permission_epoch=0)
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(active_slot=None)
        self.assertIn("revision_not_current", self._resume_readiness(token, now).reasons)
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(active_slot=1)
        newer = self._message("Нове питання", "resume-new-inbound")
        self.assertIn("revision_deadline_exhausted", self._resume_readiness(token, now).reasons)
        newer.delete()
        effect = IgRevisionDeliveryEffect.objects.create(revision=self.revision, source_message=self.source,
            effect_key="resume-unknown", group="reply", kind="text", order_index=0, part_index=0,
            part_count=1, plan_digest="a" * 64, payload_digest="b" * 64,
            recipient_igsid=self.customer.igsid, provider_namespace="instagram_login:owner-1",
            settings_id_snapshot=self.settings.pk, settings_permission_epoch=0, client_permission_epoch=0,
            revision_snapshot_digest=self.revision.snapshot_digest, publication_id=self.publication.pk,
            publication_version=1, publication_hash=self.publication.snapshot_hash, authority_context_digest="c" * 64,
            state="unknown")
        self.assertIn("revision_deadline_exhausted", self._resume_readiness(token, now).reasons)
        self.revision.refresh_from_db()
        self.assertFalse(execution_resume_is_current(self.revision, now=now))
        effect.refresh_from_db()
        self.assertEqual(effect.state, "unknown")

    def test_unresolved_graph_does_not_infer_failure(self):
        self._admit()
        self._graph(outcome="")
        result = prepare_outage_recovery(self.revision.pk, now=self._queue())
        self.assertEqual(result.reason, "recovery_generation_outcome_unknown")

    def test_missing_original_admission_is_manual(self):
        self._graph()
        result = prepare_outage_recovery(self.revision.pk, now=self._queue())
        self.assertEqual(result.reason, "recovery_generation_admission_missing")

    def test_purchase_selection_drift_is_not_reused_as_consent(self):
        self._admit()
        self._graph()
        self.customer.current_size = "XL"
        self.customer.save(update_fields=["current_size"])
        result = prepare_outage_recovery(self.revision.pk, now=self._queue())
        self.assertEqual(result.state, "manual")
        self.assertEqual(result.reason, "recovery_financial_authority_stale")

    def test_pause_cancels_pending_recovery(self):
        self._admit()
        now = self._queue()
        self.customer.bot_paused = True
        self.customer.save(update_fields=["bot_paused"])
        self.assertEqual(prepare_outage_recovery(self.revision.pk, now=now).state, "cancelled")
        self.assertNotIn(self.revision.pk, due_recovery_revision_ids(now=now))

    def test_newer_inbound_invalidates_recovery_without_head_hook(self):
        self._admit()
        now = self._queue()
        self._message("Змінив думку", "newer")
        result = prepare_outage_recovery(self.revision.pk, now=now)
        self.assertEqual(result.reason, "recovery_newer_inbound")

    def test_manager_message_invalidates_old_recovery(self):
        self._admit()
        now = self._queue()
        InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid, role="manager", source="echo", text="Відповів менеджер", status="done")
        result = prepare_outage_recovery(self.revision.pk, now=now)
        self.assertEqual(result.reason, "recovery_manager_answered")

    def test_source_window_is_not_extended_by_new_generation(self):
        self._admit()
        self._queue()
        result = prepare_outage_recovery(self.revision.pk, now=timezone.now() + timedelta(hours=23))
        self.assertEqual(result.reason, "recovery_source_window_closed")

    def test_never_schedule_before_original_deadline(self):
        result = schedule_revision_recovery(self.revision.pk)
        self.assertEqual(result.reason, "recovery_not_expired")
        self.assertEqual(due_recovery_revision_ids(), [])

    def test_publication_change_can_have_a_fresh_manifest(self):
        self._admit()
        self._graph()
        now = self._queue()
        publication = BotPolicyPublication.objects.create(
            version=2, kind="publish", schema_version=1,
            snapshot=self.publication.snapshot, snapshot_hash=self.publication.snapshot_hash,
            compiler_version="instruction-set-v1", instruction_count=0,
        )
        self.settings.active_instruction_publication = publication
        self.settings.save(update_fields=["active_instruction_publication"])
        result = prepare_outage_recovery(self.revision.pk, now=now)
        self.assertEqual(result.state, "spawned", result.reason)
        child = IgCustomerTurnRevision.objects.get(pk=result.child_id)
        self.assertNotIn("generation_admission", child.action_receipts)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["generation_admission"]["publication"]["id"], self.publication.pk)

    def test_any_irreversible_effect_blocks_regeneration(self):
        self._admit()
        now = self._queue()
        effect = IgRevisionDeliveryEffect.objects.create(
            revision=self.revision, source_message=self.source, effect_key="recovery-unsafe",
            group="reply", kind="text", order_index=0, part_index=0, part_count=1,
            plan_digest="a" * 64, payload_digest="b" * 64,
            recipient_igsid=self.customer.igsid, provider_namespace="instagram_login:owner-1",
            settings_id_snapshot=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            client_permission_epoch=self.customer.reply_permission_epoch,
            revision_snapshot_digest=self.revision.snapshot_digest,
            publication_id=self.publication.pk, publication_version=1,
            publication_hash=self.publication.snapshot_hash, authority_context_digest="c" * 64,
        )
        for state in ("sent", "provider_started", "unknown", "definite_failed"):
            with self.subTest(state=state):
                IgRevisionDeliveryEffect.objects.filter(pk=effect.pk).update(state=state)
                IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(recovery_state="waiting", recovery_due_at=now)
                result = prepare_outage_recovery(self.revision.pk, now=now)
                self.assertEqual(result.state, "manual")
                self.assertEqual(result.reason, "recovery_delivery_reconciliation")
                effect.refresh_from_db()
                self.assertEqual(effect.state, state)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
