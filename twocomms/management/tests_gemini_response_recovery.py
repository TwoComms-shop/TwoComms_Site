"""HTTP-200 failures must retain accounting without exposing provider content."""
import json
import os
from unittest.mock import Mock, patch

from django.test import TestCase, TransactionTestCase, override_settings

from management import tests_gemini_accounting_shadow as fixtures
from management.models import GeminiModelQuotaUsage, GeminiQuotaState, GeminiRequest, GeminiRequestAttempt
from management.services import call_ai_analysis as ai
from management.services import gemini_accounting_runtime as runtime


PAYLOAD = {"contents": [{"role": "user", "parts": [{"text": "customer-private-sentinel"}]}]}
USAGE = {"promptTokenCount": 31, "thoughtsTokenCount": 1500, "candidatesTokenCount": 0, "totalTokenCount": 1531}
ONE_KEY = {**fixtures.KEY_ENV, "GEMINI_API6": ""}


def response(*, finish="MAX_TOKENS", text=None, usage=None, block=None):
    body = {"candidates": [{"finishReason": finish, "content": {"parts": [
        {"thought": True, "text": "private-internal-thought-sentinel"},
        *([{"text": text}] if text is not None else []),
    ]}}], "usageMetadata": USAGE if usage is None else usage}
    if block is not None:
        body["promptFeedback"] = {"blockReason": block}
    return fixtures._Response(payload=body)


@override_settings(**fixtures.SHADOW)
class GeminiResponseRecoveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.seed_shadow_profiles()

    def gateway_failure(self, result, error_type, *, parse=False):
        boundary = Mock()
        boundary.before_provider.return_value = True
        with patch.object(ai.requests, "post", return_value=result):
            with self.assertRaises(error_type) as caught:
                ai._gemini_call_once("gemini-3.5-flash-lite", PAYLOAD, "local-test-key", parse=parse, attempt_boundary=boundary)
        boundary.failed.assert_called_once()
        boundary.succeeded.assert_not_called()
        self.assertEqual(boundary.failed.call_args.kwargs["usage"], caught.exception.usage)
        return caught.exception

    def test_thought_only_max_tokens_keeps_first_failed_usage(self):
        error = self.gateway_failure(response(), ai._GeminiEmpty)
        self.assertEqual(error.provider_reason, "MAX_TOKENS")
        self.assertEqual(error.usage["thoughtsTokenCount"], 1500)
        self.assertEqual(error.usage["totalTokenCount"], 1531)
        self.assertEqual(runtime.classify_failure(error)["http_code"], 200)
        self.assertIn("usage=present;counts=1111", error.response_diagnostic)
        self.assertNotIn("sentinel", str(error) + json.dumps(error.usage))

    def test_stop_empty_and_missing_usage_are_not_reported_as_known_zero(self):
        error = self.gateway_failure(response(finish="STOP", usage={}), ai._GeminiEmpty)
        self.assertEqual(error.provider_reason, "STOP")
        self.assertIn("usage=missing;counts=0000", error.response_diagnostic)
        self.assertNotIn("totalTokenCount", error.usage)

    def test_malformed_candidate_json_is_not_empty_and_never_logs_candidate(self):
        error = self.gateway_failure(response(finish="STOP", text="private-invalid-json-sentinel"), ai._GeminiMalformedResponse, parse=True)
        self.assertEqual(runtime.classify_failure(error)["failure_kind"], "invalid_response")
        self.assertEqual(error.provider_reason, "MALFORMED_JSON")
        self.assertNotIn("sentinel", str(error) + error.response_diagnostic)
        self.assertEqual(error.usage["promptTokenCount"], 31)

    def test_untrusted_provider_enum_and_invalid_counts_cannot_enter_diagnostics(self):
        error = self.gateway_failure(response(finish="private-finish-sentinel", usage={
            "promptTokenCount": "private-token-sentinel", "thoughtsTokenCount": -1,
            "candidatesTokenCount": True, "totalTokenCount": 10**100,
        }), ai._GeminiEmpty)
        self.assertEqual(error.provider_reason, "EMPTY_TEXT")
        self.assertIn("usage=invalid;counts=0000", error.response_diagnostic)
        self.assertNotIn("sentinel", json.dumps(error.usage) + str(error))

    def test_safety_refusal_precedes_even_nonempty_candidate(self):
        for finish, block in (("SAFETY", None), ("STOP", "SAFETY"), ("STOP", "private-block-sentinel")):
            with self.subTest(finish=finish, block=block):
                error = self.gateway_failure(response(finish=finish, text="candidate-private-sentinel", block=block), ai._GeminiSafetyBlocked)
                self.assertEqual(runtime.classify_failure(error)["failure_kind"], "safety_blocked")
                self.assertNotIn("sentinel", str(error) + error.response_diagnostic)

    def test_failed_boundary_forwards_exception_usage_when_caller_omits_it(self):
        observer = Mock()
        boundary = runtime.AttemptBoundary(observer, "GEMINI_API", "gemini-3.5-flash-lite", 1, 1)
        error = ai._GeminiEmpty(usage=USAGE)
        boundary.failed(error)
        self.assertEqual(observer._finish.call_args.kwargs["usage"], USAGE)

    @patch.dict(os.environ, ONE_KEY, clear=False)
    def test_real_failed_http_200_settles_legacy_and_canonical_tokens_once(self):
        with patch.object(ai.requests, "post", return_value=response()) as post:
            with self.assertRaises(ai.CallAIAnalysisError):
                ai.gemini_generate_text(PAYLOAD, role="chat", model_chain_override=["gemini-3.5-flash-lite"],
                    result_validator=lambda *_args, **_kwargs: True, max_actual_dispatches=1)
        self.assertEqual(post.call_count, 1)
        attempt = GeminiRequestAttempt.objects.get(provider_started_at__isnull=False)
        self.assertEqual((attempt.prompt_tokens, attempt.thoughts_tokens, attempt.candidates_tokens, attempt.total_tokens), (31, 1500, 0, 1531))
        self.assertEqual(attempt.provider_reason, "MAX_TOKENS")
        self.assertIn("counts=1111", attempt.error_detail)
        self.assertEqual(attempt.fsm_state, "failed")
        self.assertFalse(attempt.winner_claimed)
        self.assertIsNone(GeminiRequest.objects.get().winner_attempt_id)
        self.assertEqual(GeminiModelQuotaUsage.objects.get(key_name="GEMINI_API").tokens, 1531)
        state = GeminiQuotaState.objects.get()
        self.assertEqual(state.in_flight_count, 0)
        self.assertFalse(state.provider_blocks)

    @patch.dict(os.environ, fixtures.KEY_ENV, clear=False)
    def test_real_safety_block_is_one_call_without_other_key_or_model_retry(self):
        with patch.object(ai.requests, "post", return_value=response(finish="SAFETY")) as post:
            with self.assertRaises(ai.CallAIAnalysisError) as caught:
                ai.gemini_generate_text(PAYLOAD, role="chat",
                    model_chain_override=["gemini-3.5-flash-lite", "gemini-3.6-flash"],
                    result_validator=lambda *_args, **_kwargs: True, max_actual_dispatches=8)
        self.assertEqual(caught.exception.failure_kind, "safety_blocked")
        self.assertEqual(post.call_count, 1)
        attempt = GeminiRequestAttempt.objects.get(provider_started_at__isnull=False)
        self.assertEqual(attempt.failure_kind, "safety_blocked")
        self.assertEqual(attempt.total_tokens, 1531)
        self.assertIsNone(GeminiRequest.objects.get().winner_attempt_id)
        self.assertFalse(GeminiQuotaState.objects.get().provider_blocks)


@override_settings(**fixtures.SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class DurableResponseRecoveryTests(TransactionTestCase):
    def setUp(self):
        from management import tests_ig_revision_live as live_fixture
        fixtures.seed_shadow_profiles()
        self.case = live_fixture.RevisionLiveTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._prepare()

    def run_reply(self, replies, *, reason="unauthorized_url", before_response=None, max_calls=8):
        from management.services.ig_provider_dispatch_budget import ValidationDecision
        from management.services.ig_turn_lineage import turn_lineage

        calls = []
        responses = iter(replies)
        def provider(*args, **kwargs):
            calls.append((args, kwargs))
            if before_response:
                before_response(len(calls))
            return next(responses)
        def validate(parsed, *, usage):
            return ValidationDecision(valid=parsed == "valid", reason_codes=() if parsed == "valid" else (reason,))
        with patch.dict(os.environ, {**ONE_KEY, "GEMINI_API2": "test-key-2"}, clear=False), \
             patch.object(ai.requests, "post", side_effect=provider), \
             runtime.revision_request_execution(self.case.revision.pk, self.case.token,
                 settings_id=self.case.settings.pk, settings_permission_epoch=self.case.settings.reply_permission_epoch), \
             turn_lineage(lane="live", client_id=self.case.customer.pk, source_message_id=self.case.source.pk,
                 logical_turn_id=f"ig-revision:{self.case.revision.pk}"):
            result = ai.gemini_generate_text(PAYLOAD, role="chat",
                model_chain_override=["gemini-3.5-flash-lite", "gemini-3.6-flash"],
                result_validator=validate, repair_payload_factory=lambda payload, *_args: payload,
                max_actual_dispatches=max_calls)
        return result, calls

    def test_max_tokens_corrects_same_candidate_once_under_durable_root(self):
        result, calls = self.run_reply([response(), response(finish="STOP", text="valid")])
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(calls), 2)
        bodies = [json.loads(kwargs["data"]) for _args, kwargs in calls]
        self.assertEqual([body["generationConfig"]["maxOutputTokens"] for body in bodies], [1536, 4096])
        self.assertEqual([body["generationConfig"]["thinkingConfig"]["thinkingLevel"] for body in bodies], ["low", "low"])
        attempts = list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).order_by("pk"))
        self.assertEqual(attempts[0].candidate_index, attempts[1].candidate_index)
        self.assertEqual({row.model for row in attempts}, {"gemini-3.5-flash-lite"})
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.candidate_outcomes["_provider_repair_reservation"]["recovery_kind"], "output_cap")
        outcomes = graph.candidate_outcomes[str(attempts[0].candidate_index)]
        self.assertEqual([row["response_policy"]["max_output_tokens"] for row in outcomes], [1536, 4096])
        self.assertEqual(graph.winner_attempt_id, attempts[1].pk)

    def test_stochastic_semantics_get_one_fresh_cheap_project_before_scarce(self):
        result, calls = self.run_reply([response(finish="STOP", text="invalid"), response(finish="STOP", text="invalid"), response(finish="STOP", text="valid")])
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(calls), 3)
        attempts = list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).order_by("pk"))
        self.assertEqual({row.model for row in attempts}, {"gemini-3.5-flash-lite"})
        self.assertEqual(attempts[0].project_identity, attempts[1].project_identity)
        self.assertNotEqual(attempts[1].project_identity, attempts[2].project_identity)
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.candidate_outcomes["_provider_semantic_salvage"]["failure_attempt_id"], attempts[1].pk)
        self.assertEqual(graph.winner_attempt_id, attempts[2].pk)

    def test_price_authority_violation_cannot_use_fresh_cheap_salvage(self):
        _result, calls = self.run_reply([response(finish="STOP", text="invalid"), response(finish="STOP", text="invalid"), response(finish="STOP", text="valid")], reason="unverified_price")
        self.assertEqual(len(calls), 3)
        attempts = list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).order_by("pk"))
        self.assertEqual(attempts[-1].model, "gemini-3.6-flash")
        self.assertNotIn("_provider_semantic_salvage", GeminiRequest.objects.get().candidate_outcomes)

    def test_unknown_empty_does_not_speculatively_raise_output_cap(self):
        _result, calls = self.run_reply([response(finish="UNKNOWN", usage={}), response(finish="STOP", text="valid")])
        self.assertEqual(len(calls), 2)
        self.assertEqual([json.loads(kwargs["data"])["generationConfig"]["maxOutputTokens"] for _args, kwargs in calls], [1536, 1536])
        self.assertNotIn("_provider_repair_reservation", GeminiRequest.objects.get().candidate_outcomes)

    def test_pause_after_first_response_prevents_corrected_provider_dispatch(self):
        from management.models import IgClient
        def pause(_count):
            IgClient.objects.filter(pk=self.case.customer.pk).update(bot_paused=True)
        with self.assertRaises(ai.CallAIAnalysisError):
            self.run_reply([response()], before_response=pause)
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 1)
        self.assertIsNone(GeminiRequest.objects.get().winner_attempt_id)

    def test_corrected_empty_cannot_obtain_second_repair_or_exceed_call_cap(self):
        with self.assertRaises(ai.CallAIAnalysisError):
            self.run_reply([response(), response()], max_calls=2)
        attempts = list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).order_by("pk"))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].candidate_index, attempts[1].candidate_index)
        self.assertIsNone(GeminiRequest.objects.get().winner_attempt_id)

    def test_invalid_salvage_ends_cheap_tier_without_more_cheap_attempts(self):
        _result, calls = self.run_reply([*[response(finish="STOP", text="invalid") for _ in range(3)], response(finish="STOP", text="valid")])
        self.assertEqual(len(calls), 4)
        self.assertEqual(list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).order_by("pk").values_list("model", flat=True)),
                         ["gemini-3.5-flash-lite"] * 3 + ["gemini-3.6-flash"])

    @override_settings(GEMINI_KEY_PROJECT_GROUPS={**fixtures.SHADOW["GEMINI_KEY_PROJECT_GROUPS"], "GEMINI_API2": "gemini-project-1"})
    def test_alias_in_same_project_is_never_fresh_salvage(self):
        _result, calls = self.run_reply([response(finish="STOP", text="invalid"), response(finish="STOP", text="invalid"), response(finish="STOP", text="valid")])
        self.assertEqual(len(calls), 3)
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False, model="gemini-3.5-flash-lite").count(), 2)
        self.assertNotIn("_provider_semantic_salvage", GeminiRequest.objects.get().candidate_outcomes)

    def test_fresh_project_quota_denial_does_not_cross_provider_boundary(self):
        original_reserve = ai.gemini_quota.try_reserve
        def quota(key, model, **kwargs):
            if key == "GEMINI_API2" and model == "gemini-3.5-flash-lite":
                return False
            return original_reserve(key, model, **kwargs)
        with patch.object(ai.gemini_quota, "try_reserve", side_effect=quota):
            _result, calls = self.run_reply([response(finish="STOP", text="invalid"), response(finish="STOP", text="invalid"), response(finish="STOP", text="valid")])
        self.assertEqual(len(calls), 3)
        self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False, key_name="GEMINI_API2", model="gemini-3.5-flash-lite").exists())

    def test_legacy_root_can_correct_max_tokens_without_new_budget(self):
        from datetime import timedelta
        from django.utils import timezone
        from management.models import IgClient, InstagramBotMessage
        from management.services.ig_provider_dispatch_budget import ValidationDecision
        from management.services.ig_turn_lineage import turn_lineage, resolve_logical_turn_key

        client = IgClient.objects.create(igsid="legacy-output-recovery", automation_lease_token="root-lease",
            automation_lease_until=timezone.now() + timedelta(minutes=5))
        source = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid, role="user", text="Hello",
            source="webhook", status="processing", processing_started_at=timezone.now())
        with patch.dict(os.environ, ONE_KEY, clear=False), \
             patch.object(ai.requests, "post", side_effect=[response(), response(finish="STOP", text="valid")]) as post, \
             turn_lineage(lane="live", client_id=client.pk, source_message_id=source.pk,
                 logical_turn_id=resolve_logical_turn_key(source)) as lineage:
            lineage["automation_token"] = "root-lease"
            result = ai.gemini_generate_text(PAYLOAD, role="chat", model_chain_override=["gemini-3.5-flash-lite"],
                result_validator=lambda parsed, **_kwargs: ValidationDecision(valid=parsed == "valid"), max_actual_dispatches=8,
                legacy_provider_root=True)
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(post.call_count, 2)
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.candidate_outcomes["_provider_repair_reservation"]["recovery_kind"], "output_cap")
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).count(), 2)
