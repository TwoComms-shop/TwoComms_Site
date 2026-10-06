"""Stored evidence contracts: no generation, bootstrap, transport or GET writes."""
import json
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_revision_proposal as proposal_fixture
from management.models import (
    GeminiRequest, GeminiRequestAttempt, IgBotNotification, IgClient,
    IgFollowUpTask, IgFunnelResetAudit, IgRevisionDeliveryEffect, InstagramBotMessage,
)
from management.services.gemini_accounting_runtime import revision_request_execution
from management.services.ig_request_manifest import capture_dispatch_context, capture_request_context
from management.services.ig_revision_input import decide_revision_input
from management.services.ig_revision_outbox import _digest
from management.services.ig_revision_reply_projection import project_sent_reply
from management.services.ig_decision_trace_read_model import (
    INDEX_QUERY_CAP, READ_QUERY_CAP, read_revision_decision_trace,
    read_revision_decision_trace_index,
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class DecisionTraceReadModelTests(TransactionTestCase):
    reset_sequences = True
    setUp = proposal_fixture.RevisionGenerationProposalTests.setUp
    _digest = staticmethod(proposal_fixture.RevisionGenerationProposalTests._digest)
    _authority = proposal_fixture.RevisionGenerationProposalTests._authority
    _media_manifest = proposal_fixture.RevisionGenerationProposalTests._media_manifest
    _intelligence = proposal_fixture.RevisionGenerationProposalTests._intelligence
    _store = proposal_fixture.RevisionGenerationProposalTests._store

    def _policy_manifest(self):
        value = proposal_fixture.RevisionGenerationProposalTests._policy_manifest(self)
        value["request_context"] = capture_request_context(payload={"contents": []}, metadata={
            "revision_id": self.revision.pk, "client_id": self.client_row.pk,
            "source_message_ids": [self.source.pk], "bundle_digest": self.revision.snapshot_digest,
            "builder_version": "ig-turn-intelligence.v1", "effective_mode": "unified",
            "selected_block_ids": ["history", "source"],
            "omitted_blocks": [{"block_id": "memory", "reason": "budget_exhausted"}],
            "view_versions": {"publication_hash": self.publication.snapshot_hash},
        })
        return value

    def _create_generation_graph(self):
        if self._testMethodName in {"test_no_reply_and_static_decisions_remain_in_revision_index",
                                   "test_static_reply_has_revision_identity_without_gemini_request"}:
            return
        if self._testMethodName == "test_old_manifest_absent_is_unknown_without_today_policy_reconstruction":
            original = self._policy_manifest
            self._policy_manifest = lambda: proposal_fixture.RevisionGenerationProposalTests._policy_manifest(self)
            try:
                proposal_fixture.RevisionGenerationProposalTests._create_generation_graph(self)
            finally:
                self._policy_manifest = original
            return
        execution = f"ig-revision:{self.revision.pk}"
        with revision_request_execution(self.revision.pk, self.token, settings_id=self.settings.pk,
                                        settings_permission_epoch=self.settings.reply_permission_epoch):
            self.graph = GeminiRequest.objects.create(
                request_id=self.request_id, client_id=self.client_row.pk,
                source_message_id=self.source.pk, logical_turn_id=execution, source_execution_key=execution,
                lane="live", task_class="complex_live", reasoning_task="customer_chat", accounting_mode="shadow",
                policy_manifest=self._policy_manifest(),
            )
        self.failed = self._attempt(index=1, state="failed", model="gemini-rejected", failure_kind="local_semantic_rejection",
                                    error_detail="unverified_price,unnecessary_manager_handoff", http_code=200,
                                    total_tokens=29, prompt_tokens=23, candidates_tokens=6)
        self.winner = self._attempt(index=2, state="succeeded", model=self.model, winner_claimed=True,
                                   error_detail="response.v1;finish=STOP;block=NONE;usage=present;counts=1111",
                                   http_code=200, total_tokens=31, prompt_tokens=23, candidates_tokens=8)
        self.graph.winner_attempt = self.winner
        self.graph.terminal_resolution = "succeeded"
        self.graph.terminal_reason = "provider_success"
        self.graph.resolved_at = timezone.now()
        self.graph.save(update_fields=["winner_attempt", "terminal_resolution", "terminal_reason", "resolved_at", "updated_at"])

    def _attempt(self, *, index, state, model, **kwargs):
        now = timezone.now()
        return GeminiRequestAttempt.objects.create(
            request_graph=self.graph, request_id=self.graph.request_id, client_id=self.client_row.pk,
            source_message_id=self.source.pk, logical_turn_id=self.graph.logical_turn_id,
            lane="live", role="chat", key_name="private-key-alias", model=model, attempt_index=index,
            candidate_index=1, fsm_state=state, outcome=state, provider_started_at=now,
            finished_at=now, dispatch_pacific_day=timezone.localdate(), accounting_mode="shadow",
            dispatch_manifest=capture_dispatch_context(payload=b'{"contents":[]}',
                context=self.graph.policy_manifest["request_context"], attempt_index=index, model=model), **kwargs,
        )

    def read(self):
        return read_revision_decision_trace(client_id=self.client_row.pk, revision_id=self.revision.pk)

    def _effects(self, states=("sent",), *, purpose="normal_reply", namespace=None, source=None):
        parts = []
        for index, state in enumerate(states):
            payload = {"recipient": {"id": self.client_row.igsid}, "message": {"text": "PRIVATE CUSTOMER REPLY"}}
            parts.append(IgRevisionDeliveryEffect.objects.create(
                revision=self.revision, source_message=source or self.source,
                effect_key=f"trace-test:{self.revision.pk}:{index}", actor="bot", purpose=purpose,
                group="substantive_text", kind="text", order_index=index, part_index=index, part_count=len(states),
                plan_digest="a" * 64, payload=payload, payload_digest=_digest(payload),
                recipient_igsid=self.client_row.igsid, provider_namespace=namespace or self.source.provider_namespace,
                generation_request_id=self.graph.request_id, generation_model=self.model,
                settings_id_snapshot=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
                client_permission_epoch=self.revision.permission_epoch, revision_snapshot_digest=self.revision.snapshot_digest,
                publication_id=self.publication.pk, publication_version=self.publication.version,
                publication_hash=self.publication.snapshot_hash, authority_context_digest="b" * 64,
                state=state, provider_started_at=timezone.now() if state in {"sent", "unknown"} else None,
                terminal_at=timezone.now() if state in {"sent", "unknown", "cancelled"} else None,
                provider_message_id=f"trace-mid-{index}" if state == "sent" else "",
            ))
        return parts

    def _coverage(self, *, disposition="complete", remaining=()):
        self.revision.refresh_from_db()
        material = {"version": 1, "snapshot_digest": self.revision.snapshot_digest,
                    "source_message_ids": [self.source.pk], "covered": ["synthetic:answer"],
                    "remaining": list(remaining), "disposition": disposition}
        self.revision.action_receipts = {**self.revision.action_receipts, "response_coverage": {**material, "digest": _digest(material)}}
        self.revision.save(update_fields=["action_receipts"])

    def test_failed_semantic_attempt_and_winner_are_generation_not_sent(self):
        result = self.read()
        self.assertEqual(result["generation"]["actual_model"], self.model)
        attempts = result["generation"]["requests"][0]["attempts"]
        self.assertEqual(attempts[0]["validator_layer"], "local_semantic")
        self.assertEqual(attempts[0]["validator_codes"], ["unverified_price", "unnecessary_manager_handoff"])
        self.assertEqual(attempts[0]["usage"]["tokens"]["total"], 29)
        self.assertIsNone(attempts[0]["usage"]["monetary_cost"])
        self.assertFalse(attempts[0]["winner"])
        self.assertTrue(attempts[1]["winner"])
        self.assertEqual(result["delivery"]["physical_state"], "unknown")
        self.assertEqual(result["semantic"]["proof"], "unknown")
        self.assertIsNone(result["decision"]["routing_reason_codes"])

    def test_validated_proposal_does_not_fabricate_delivery_or_expose_prose(self):
        stored = self._store()
        self.assertTrue(stored.stored, stored.reasons)
        result = self.read()
        self.assertEqual(result["proposal"]["proof"], "confirmed")
        self.assertFalse(result["delivery"]["normal_reply_complete"])
        serialized = json.dumps(result)
        for private in ("Що на фото?", "На фото товар.", "private-key-alias", "signed.invalid", "ig-private/", self.revision.snapshot_digest):
            self.assertNotIn(private, serialized)

    def test_partial_unknown_has_exact_first_receipt_but_semantic_debt_remains(self):
        self._effects(("sent", "unknown", "planned"))
        self._coverage(disposition="recovery", remaining=("synthetic:second-answer",))
        result = self.read()
        self.assertEqual(result["delivery"]["physical_state"], "partial")
        self.assertEqual([row["receipt_proof"] for row in result["delivery"]["effects"]], ["confirmed", "unknown", "unknown"])
        self.assertFalse(result["delivery"]["normal_reply_complete"])
        self.assertFalse(result["semantic"]["customer_reply_complete"])
        self.assertEqual(result["semantic"]["remaining_count"], 1)

    def test_sent_receipt_and_transcript_are_independent_then_exact_parity(self):
        self._effects()
        first = self.read()
        self.assertEqual(first["delivery"]["physical_state"], "sent")
        self.assertEqual(first["delivery"]["effects"][0]["transcript_proof"], "unknown")
        project_sent_reply(self.revision.pk)
        self._coverage()
        second = self.read()
        self.assertEqual(second["delivery"]["effects"][0]["transcript_proof"], "confirmed")
        self.assertTrue(second["semantic"]["customer_reply_complete"])
        message = InstagramBotMessage.objects.get(source="revision_reply")
        message.provider_message_id = "wrong-receipt"
        message.save(update_fields=["provider_message_id"])
        third = self.read()
        self.assertEqual(third["delivery"]["effects"][0]["transcript_proof"], "unknown")

    def test_holding_receipt_never_fulfills_normal_reply(self):
        self._effects(purpose="technical_holding")
        self._coverage()
        result = self.read()
        self.assertEqual(result["delivery"]["physical_state"], "sent")
        self.assertFalse(result["delivery"]["normal_reply_complete"])
        self.assertFalse(result["semantic"]["customer_reply_complete"])

    def test_cross_namespace_and_foreign_source_receipts_fail_closed(self):
        self._effects(namespace="instagram_login:foreign-owner")
        result = self.read()
        self.assertEqual(result["delivery"]["proof"], "unknown")
        self.assertEqual(result["delivery"]["effects"][0]["receipt_proof"], "unknown")
        self.assertIsNone(result["delivery"]["effects"][0]["provider_message_id"])

    def test_foreign_source_effect_cannot_prove_same_owner_delivery(self):
        other = IgClient.objects.create(igsid="trace-other-client")
        source = InstagramBotMessage.objects.create(client=other, sender_id=other.igsid, role="user",
                                                    text="FOREIGN PRIVATE QUESTION", mid="foreign-trace-source")
        self._effects(source=source)
        result = self.read()
        self.assertEqual(result["delivery"]["proof"], "unknown")
        self.assertIsNone(result["delivery"]["effects"][0]["source_message_id"])
        self.assertNotIn("FOREIGN", json.dumps(result))

    def test_cross_owner_revision_and_changed_source_are_unavailable(self):
        other = IgClient.objects.create(igsid="trace-foreign-client")
        self.assertEqual(read_revision_decision_trace(client_id=other.pk, revision_id=self.revision.pk)["status"], "unavailable")
        self.source.text = "CHANGED PRIVATE TEXT"
        self.source.save(update_fields=["text"])
        self.assertEqual(self.read()["reason"], "source_binding_unverified")

    def test_erasure_hidden_deleted_and_revoked_media_never_return_evidence(self):
        self.client_row.privacy_erasure_started_at = timezone.now()
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.read()["status"], "unavailable")
        self.assertEqual(read_revision_decision_trace_index(client_id=self.client_row.pk)["status"], "unavailable")

    def test_hidden_owner_and_revoked_source_media_fail_closed(self):
        self.client_row.hidden_at = timezone.now()
        self.client_row.save(update_fields=["hidden_at"])
        self.assertEqual(self.read()["status"], "unavailable")
        self.client_row.hidden_at = None
        self.client_row.save(update_fields=["hidden_at"])
        self.source.private_media_state = "deleted"
        self.source.save(update_fields=["private_media_state"])
        self.assertEqual(self.read()["reason"], "source_binding_unverified")
        self.assertEqual(read_revision_decision_trace_index(client_id=self.client_row.pk)["items"][0]["scope"], "unknown")

    def test_deleted_source_is_unknown_in_index_and_unavailable_in_detail(self):
        self.source.delete()
        self.assertEqual(self.read()["status"], "unavailable")
        index = read_revision_decision_trace_index(client_id=self.client_row.pk)
        self.assertTrue(not index["items"] or index["items"][0]["scope"] == "unknown")

    def test_late_receipt_stays_original_revision_after_reset(self):
        self._effects()
        IgFunnelResetAudit.objects.create(client=self.client_row, reset_after_message_id=self.source.pk,
                                        reason="synthetic reset", resulting_stage="new")
        self.client_row.reply_permission_epoch += 1
        self.client_row.save(update_fields=["reply_permission_epoch"])
        result = self.read()
        self.assertEqual(result["revision"]["id"], self.revision.pk)
        self.assertEqual(result["revision"]["scope"], "historical")
        self.assertEqual(result["delivery"]["physical_state"], "sent")
        self.assertEqual(read_revision_decision_trace_index(client_id=self.client_row.pk)["items"][0]["scope"], "historical")

    def test_old_manifest_absent_is_unknown_without_today_policy_reconstruction(self):
        # Setup uses the canonical producer's legacy captured policy, without
        # changing an immutable original graph or constructing today's prompt.
        result = self.read()
        self.assertEqual(result["generation"]["proof"], "unknown")
        self.assertEqual(result["generation"]["requests"][0]["reason"], "legacy_context_uncaptured")

    def test_usage_defaults_are_unknown_and_repair_never_leaks_reservation(self):
        self.graph.candidate_outcomes = {
            "_provider_repair_reservation": {"token": "PRIVATE-REPAIR-TOKEN", "key_name": "PRIVATE-ALIAS"},
            "_provider_repair_preparation": {"state": "preparation_refused", "reason": "repair_factory_missing",
                                             "key_name": "PRIVATE-ALIAS", "candidate_index": 1},
        }
        self.graph.save(update_fields=["candidate_outcomes", "updated_at"])
        self._attempt(index=3, state="failed", model="gemini-empty", error_detail="", failure_kind="empty")
        result = self.read()
        request = result["generation"]["requests"][0]
        self.assertEqual(request["attempts"][-1]["usage"]["status"], "unknown")
        self.assertIsNone(request["attempts"][-1]["usage"]["tokens"]["total"])
        self.assertEqual(request["repair"]["reason"], "repair_factory_missing")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(result["generation"]["economic_limits"], {"http": 8, "scarce_http": 2, "repairs": 1})

    def test_manager_case_queued_is_not_delivered_or_customer_sent(self):
        task = IgFollowUpTask.objects.create(client=self.client_row, kind="manager_task", status="skipped",
                                             reason="revision_case:customer_manager_request", due_at=timezone.now())
        notification = IgBotNotification.objects.create(client=self.client_row, dedupe_key=f"ig-revision-case:{task.pk}", status="pending")
        self.revision.action_receipts = {**self.revision.action_receipts, "manager_handoff": {
            "task_id": task.pk, "notification_id": notification.pk, "case_kind": "customer_manager_request",
            "snapshot_digest": self.revision.snapshot_digest, "generation_proposal_digest": self.revision.generation_proposal_digest}}
        self.revision.save(update_fields=["action_receipts"])
        result = self.read()
        self.assertEqual(result["actions"]["manager_case"]["proof"], "confirmed")
        self.assertFalse(result["actions"]["manager_case"]["notification_delivered"])
        self.assertFalse(result["delivery"]["normal_reply_complete"])

    def test_read_query_budget_zero_dml_and_no_provider_or_bootstrap(self):
        self._effects()
        with patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("provider forbidden")), \
             patch("management.services.ig_revision_live.execute_claimed_revision", side_effect=AssertionError("generation forbidden")), \
             CaptureQueriesContext(connection) as queries:
            result = self.read()
        self.assertEqual(result["status"], "available")
        self.assertLessEqual(len(queries), READ_QUERY_CAP)
        self.assertFalse([query["sql"] for query in queries if not query["sql"].lstrip().upper().startswith("SELECT")])

    def test_index_cursor_bound_and_zero_dml(self):
        with CaptureQueriesContext(connection) as queries:
            result = read_revision_decision_trace_index(client_id=self.client_row.pk, limit=1)
        self.assertEqual(result["items"][0]["revision_id"], self.revision.pk)
        self.assertLessEqual(len(queries), INDEX_QUERY_CAP)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        self.assertEqual(read_revision_decision_trace_index(client_id=self.client_row.pk,
                         before_revision_id=self.revision.pk)["items"], [])
        for limit in (True, 0, 26):
            with self.assertRaises(ValueError):
                read_revision_decision_trace_index(client_id=self.client_row.pk, limit=limit)

    def test_no_reply_and_static_decisions_remain_in_revision_index(self):
        self.settings.ai_enabled = False
        self.settings.trigger_text = "absent"
        self.settings.reply_text = "PRIVATE STATIC TEXT"
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        decision = decide_revision_input(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(decision.origin, "no_reply")
        result = self.read()
        self.assertEqual(result["decision"]["origin"], "no_reply")
        self.assertEqual(result["delivery"]["physical_state"], "no_reply")
        self.assertEqual(read_revision_decision_trace_index(client_id=self.client_row.pk)["items"][0]["input_origin"], "no_reply")
        self.assertNotIn("PRIVATE STATIC TEXT", json.dumps(result))

    def test_static_reply_has_revision_identity_without_gemini_request(self):
        self.settings.ai_enabled = False
        self.settings.trigger_text = self.source.text
        self.settings.reply_text = "PRIVATE STATIC TEXT"
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        decision = decide_revision_input(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(decision.origin, "static_reply")
        result = self.read()
        self.assertEqual(result["decision"]["origin"], "static_reply")
        self.assertEqual(result["generation"]["requests"], [])
        self.assertEqual(result["delivery"]["physical_state"], "unknown")
        self.assertEqual(read_revision_decision_trace_index(client_id=self.client_row.pk)["items"][0]["input_origin"], "static_reply")
        self.assertNotIn("PRIVATE STATIC TEXT", json.dumps(result))

    def test_effect_limit_never_fabricates_adjacent_or_complete_delivery(self):
        self._effects(tuple("sent" for _ in range(65)))
        result = self.read()
        self.assertEqual(result["delivery"]["reason"], "effect_read_limit")
        self.assertEqual(result["delivery"]["effects"], [])
        self.assertFalse(result["delivery"]["normal_reply_complete"])

    def test_privacy_race_rechecks_owner_after_join(self):
        from management.services import ig_decision_trace_read_model as reader
        real_owner = reader._owner
        calls = []
        def fenced_owner(client_id):
            value = real_owner(client_id)
            calls.append(client_id)
            return {**value, "privacy_erasure_started_at": timezone.now()} if len(calls) > 1 else value
        with patch.object(reader, "_owner", side_effect=fenced_owner):
            self.assertEqual(self.read()["reason"], "read_scope_changed")
