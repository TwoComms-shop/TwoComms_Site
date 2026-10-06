"""Real accounting/HTTP facade evidence with synthetic, mocked transport."""
from copy import deepcopy
import json
import os
from unittest.mock import patch

import requests
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TransactionTestCase, override_settings

from management import tests_gemini_accounting_shadow as fixtures
from management import tests_ig_revision_live as live_fixture
from management.models import GeminiRequest, GeminiRequestAttempt, IgClient
from management.services import call_ai_analysis as ai
from management.services import gemini_accounting_runtime as runtime
from management.services.gemini_accounting_contract import attach_attempt_dispatch_manifest_locked
from management.services.ig_provider_dispatch_budget import ValidationDecision
from management.services.ig_request_manifest import actual_request_preview, capture_dispatch_context, capture_request_context
from management.services.ig_turn_lineage import turn_lineage
from management.tests_ig_request_context_manifest import policy


PAYLOAD = {"contents": [{"role": "user", "parts": [{"text": "synthetic-private-customer"}]}], "generationConfig": {"temperature": 0.2}}
ONE_KEY = {**fixtures.KEY_ENV, "GEMINI_API6": ""}


def response(text="valid", *, code=200):
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}], "usageMetadata": {"promptTokenCount": 11, "totalTokenCount": 14}}
    if code != 200:
        payload = {"error": {"code": code, "status": "UNAVAILABLE", "message": "synthetic temporary failure"}}
    return fixtures._Response(code=code, payload=payload)


@override_settings(**fixtures.SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class DispatchManifestAccountingTests(TransactionTestCase):
    def setUp(self):
        fixtures.seed_shadow_profiles()
        self.case = live_fixture.RevisionLiveTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._prepare()
        self.context = capture_request_context(payload=PAYLOAD, metadata={
            "revision_id": self.case.revision.pk, "client_id": self.case.customer.pk,
            "source_message_ids": [self.case.source.pk], "bundle_digest": self.case.revision.snapshot_digest,
            "builder_version": "ig-turn-intelligence.v1", "effective_mode": "unified",
            "selected_block_ids": ["history"], "view_versions": {"memory_head_version": "head:8"},
        })
        self.manifest = {**policy(), "request_context": self.context}

    def capability(self):
        return runtime.revision_request_execution(
            self.case.revision.pk, self.case.token, settings_id=self.case.settings.pk,
            settings_permission_epoch=self.case.settings.reply_permission_epoch,
        )

    def lineage(self):
        return turn_lineage(lane="live", client_id=self.case.customer.pk,
            source_message_id=self.case.source.pk, logical_turn_id=f"ig-revision:{self.case.revision.pk}")

    def begin(self):
        with self.capability(), self.lineage():
            observer = runtime.begin_request(request_id=None, role="chat", reasoning_task="customer_chat",
                candidate_plan=fixtures._raw_plan("gemini-3.5-flash-lite"), deadline_seconds=30,
                request_policy_manifest=self.manifest)
        self.assertTrue(observer.enabled, observer.block_reason)
        return observer

    def run_reply(self, responses, *, max_calls=1, repair=None):
        def validate(parsed, *, usage):
            return ValidationDecision(valid=parsed == "valid", reason_codes=() if parsed == "valid" else ("unverified_price",))
        with patch.dict(os.environ, ONE_KEY, clear=False), patch.object(ai.requests, "post", side_effect=responses) as post, self.capability(), self.lineage():
            result = ai.gemini_generate_text(PAYLOAD, role="chat", parse=False,
                model_chain_override=["gemini-3.5-flash-lite"], result_validator=validate,
                repair_payload_factory=repair, max_actual_dispatches=max_calls,
                request_policy_manifest=self.manifest)
        return result, post

    def preview(self, graph):
        return actual_request_preview(graph, owner_id=self.case.customer.pk,
            revision_id=self.case.revision.pk, allow_protected_context=True)

    def test_actual_model_adaptation_is_captured_after_final_serialization(self):
        result, post = self.run_reply([response()])
        graph = GeminiRequest.objects.get(request_id=result["meta"]["request_id"])
        attempt = graph.attempts.get(provider_started_at__isnull=False)
        body = post.call_args.kwargs["data"]
        sent = json.loads(body)
        self.assertNotIn("temperature", sent["generationConfig"])
        self.assertEqual(sent["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")
        expected = capture_dispatch_context(payload=body, context=self.context,
            attempt_index=attempt.attempt_index, model=attempt.model)
        self.assertEqual(attempt.dispatch_manifest, expected)
        self.assertEqual(graph.policy_manifest, self.manifest)
        self.assertNotIn("synthetic-private-customer", json.dumps(attempt.dispatch_manifest))
        preview = self.preview(graph)
        self.assertEqual(preview["actual_model"], "gemini-3.5-flash-lite")
        submitted = [row for row in preview["attempts"] if row["provider_started"]]
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["attempt_index"], attempt.attempt_index)
        self.assertEqual(submitted[0]["dispatch_manifest"], expected)
        exported = actual_request_preview(graph, owner_id=self.case.customer.pk,
            revision_id=self.case.revision.pk)
        exported_attempt = next(row for row in exported["attempts"] if row["attempt_index"] == attempt.attempt_index)
        self.assertEqual(exported_attempt["dispatch_manifest"]["payload_stage"], "http_dispatch")
        self.assertNotIn("request_digest", exported_attempt["dispatch_manifest"])
        self.assertNotIn("logical_request_digest", exported_attempt["dispatch_manifest"])
        self.assertNotIn("context_digest", exported["manifest"]["request_context"])

    def test_same_candidate_repair_keeps_distinct_final_body_digests(self):
        def repair(payload, *_args):
            payload = deepcopy(payload)
            payload["contents"].append({"role": "user", "parts": [{"text": "synthetic repair instruction"}]})
            return payload
        result, post = self.run_reply([response("invalid"), response()], max_calls=2, repair=repair)
        graph = GeminiRequest.objects.get(request_id=result["meta"]["request_id"])
        attempts = list(graph.attempts.filter(provider_started_at__isnull=False).order_by("attempt_index"))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].candidate_index, attempts[1].candidate_index)
        self.assertNotEqual(attempts[0].dispatch_manifest["request_digest"], attempts[1].dispatch_manifest["request_digest"])
        for attempt, call in zip(attempts, post.call_args_list):
            self.assertEqual(attempt.dispatch_manifest, capture_dispatch_context(
                payload=call.kwargs["data"], context=self.context,
                attempt_index=attempt.attempt_index, model=attempt.model))

    def test_pre_http_guard_denial_records_prepared_body_and_zero_http(self):
        observer = self.begin()
        boundary = observer.attempt(key_name="GEMINI_API", model="gemini-3.5-flash-lite")
        normalized = ai._payload_for_model("gemini-3.5-flash-lite", PAYLOAD, reasoning_task="customer_chat")
        with patch.object(ai.requests, "post") as post, self.assertRaises(ai._GeminiAdmissionRejected):
            ai._gemini_call_once("gemini-3.5-flash-lite", normalized, "synthetic-key", parse=False,
                attempt_boundary=boundary, pre_dispatch_guard=lambda: "source_admission_denied")
        post.assert_not_called()
        attempt = GeminiRequestAttempt.objects.get(request_graph_id=observer.graph_id)
        self.assertIsNone(attempt.provider_started_at)
        self.assertEqual(attempt.dispatch_manifest["payload_stage"], "http_dispatch")
        self.assertEqual(attempt.fsm_state, "cancelled_pre_dispatch")
        preview = self.preview(GeminiRequest.objects.get(pk=observer.graph_id))
        self.assertFalse(preview["attempts"][0]["provider_started"])

    def test_same_value_cas_is_idempotent_and_all_late_overwrites_fail(self):
        result, _post = self.run_reply([response()])
        graph = GeminiRequest.objects.get(request_id=result["meta"]["request_id"])
        attempt = graph.attempts.get(provider_started_at__isnull=False)
        original = deepcopy(attempt.dispatch_manifest)
        with transaction.atomic():
            self.assertTrue(attach_attempt_dispatch_manifest_locked(attempt.pk, original))
        changed = {**original, "request_digest": "f" * 64}
        with self.assertRaises(ValidationError), transaction.atomic():
            attach_attempt_dispatch_manifest_locked(attempt.pk, changed)
        with self.assertRaises(ValidationError):
            GeminiRequestAttempt.objects.filter(pk=attempt.pk).update(dispatch_manifest=changed)
        attempt.dispatch_manifest = changed
        with self.assertRaises(ValidationError):
            attempt.save(update_fields=["dispatch_manifest"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.dispatch_manifest, original)
        with self.assertRaises(ValidationError):
            attach_attempt_dispatch_manifest_locked(attempt.pk, original)

    def test_failure_before_response_keeps_manifest_and_historical_owner_fence(self):
        with self.assertRaises(ai.CallAIAnalysisError):
            self.run_reply([requests.Timeout("synthetic transport timeout")])
        graph = GeminiRequest.objects.get()
        attempt = graph.attempts.get(provider_started_at__isnull=False)
        self.assertTrue(attempt.dispatch_manifest)
        self.assertEqual(self.preview(graph)["manifest"], self.manifest)
        self.assertEqual(self.preview(graph)["actual_model"], "")
        IgClient.objects.filter(pk=self.case.customer.pk).update(language="en")
        self.assertEqual(self.preview(graph)["manifest"]["request_context"]["view_versions"]["memory_head_version"], "head:8")
        from django.utils import timezone
        IgClient.objects.filter(pk=self.case.customer.pk).update(privacy_erasure_started_at=timezone.now())
        preview = self.preview(graph)
        self.assertEqual(preview["reason"], "privacy_erasure")
        self.assertEqual(preview["manifest"], {})
        self.assertEqual(preview["attempts"], [])

    def test_duplicate_source_does_not_replace_existing_captured_graph(self):
        observer = self.begin()
        with self.capability(), self.lineage():
            duplicate = runtime.begin_request(request_id=None, role="chat", reasoning_task="customer_chat",
                candidate_plan=fixtures._raw_plan("gemini-3.5-flash-lite"), deadline_seconds=30,
                request_policy_manifest=self.manifest)
        self.assertTrue(duplicate.provider_blocked)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.get(pk=observer.graph_id).policy_manifest, self.manifest)
