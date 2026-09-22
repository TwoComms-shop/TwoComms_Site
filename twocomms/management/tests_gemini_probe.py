import json
import os
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command
from django.test import TestCase, override_settings

from management.models import GeminiKeyState, GeminiRequest, GeminiRequestAttempt
from management.services import call_ai_analysis as caa
from management.services import gemini_probe


class GeminiModelPayloadContractTests(TestCase):
    def test_gemini_36_uses_thinking_level_instead_of_legacy_budget(self):
        payload = {
            "generationConfig": {
                "maxOutputTokens": 128,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }

        normalized = caa._payload_for_model(
            "gemini-3.6-flash", payload, reasoning_task="health_probe"
        )

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )
        self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)

    def test_gemini_25_probe_uses_low_fallback_budget(self):
        payload = {
            "generationConfig": {
                "maxOutputTokens": 128,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }

        normalized = caa._payload_for_model(
            "gemini-2.5-flash", payload, reasoning_task="health_probe"
        )

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 1024},
        )

    def test_gemini_36_preserves_explicit_level_and_other_thinking_fields(self):
        payload = {
            "generationConfig": {
                "thinkingConfig": {
                    "thinkingBudget": 64,
                    "thinkingLevel": "high",
                    "includeThoughts": True,
                },
            },
        }

        normalized = caa._payload_for_model(
            "gemini-3.6-flash", payload, reasoning_task="payment_decision"
        )

        self.assertEqual(
            normalized["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "high", "includeThoughts": True},
        )


class GeminiProbeClassificationTests(TestCase):
    def test_success_without_candidates_is_reachable_empty(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({"usageMetadata": {"totalTokenCount": 1}}),
        )

        self.assertEqual(status["status"], "reachable_empty")
        self.assertEqual(status["finish_reason"], "")

    def test_malformed_candidates_shape_is_malformed(self):
        status = gemini_probe.classify_probe_response(200, json.dumps({"candidates": {}}))

        self.assertEqual(status["status"], "malformed_response")

    def test_max_tokens_without_text_is_reachable_degraded(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}],
                "usageMetadata": {"thoughtsTokenCount": 120, "candidatesTokenCount": 0},
            }),
        )

        self.assertEqual(status["status"], "reachable_degraded")
        self.assertEqual(status["finish_reason"], "MAX_TOKENS")
        self.assertEqual(status["thoughts_tokens"], 120)

    def test_partial_max_tokens_is_still_degraded(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "O"}]}}],
            }),
        )

        self.assertEqual(status["status"], "reachable_degraded")

    def test_safety_takes_precedence_even_if_provider_includes_text(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "SAFETY", "content": {"parts": [{"text": "blocked"}]}}],
            }),
        )

        self.assertEqual(status["status"], "blocked")

    def test_thought_only_parts_are_not_an_answer(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"thought": True, "text": "internal"}]}}],
            }),
        )

        self.assertEqual(status["status"], "reachable_empty")

    def test_safety_block_is_reachable_but_not_usable(self):
        status = gemini_probe.classify_probe_response(
            200,
            json.dumps({
                "promptFeedback": {"blockReason": "SAFETY"},
                "candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}],
            }),
        )

        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["finish_reason"], "SAFETY")

    def test_non_200_is_classified_without_exposing_response_body(self):
        status = gemini_probe.classify_probe_response(429, '{"error":{"message":"secret-key-value"}}')

        self.assertEqual(status["status"], "quota")
        self.assertNotIn("secret-key-value", json.dumps(status))


class GeminiMetadataProbeQuotaTests(TestCase):
    @patch("management.services.gemini_probe.requests.post")
    @patch("management.services.gemini_probe.requests.get")
    def test_metadata_probe_never_calls_generation_endpoint(self, get, post):
        response = Mock(status_code=200)
        response.json.return_value = {
            "supportedGenerationMethods": ["generateContent"],
        }
        get.return_value = response

        result = gemini_probe.probe_key_metadata("gemini-3.8-flash", "private-key")

        self.assertEqual(result["status"], "metadata_ok")
        get.assert_called_once()
        post.assert_not_called()
        self.assertIn("/models/gemini-3.8-flash", get.call_args.args[0])


class GeminiProbeAdmissionTests(TestCase):
    @staticmethod
    def _success_response():
        payload = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "OK"}]},
            }],
            "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 5},
        }
        response = Mock(status_code=200, text=json.dumps(payload))
        response.json.return_value = payload
        return response

    @patch("management.services.gemini_probe.requests.post")
    @patch("management.services.gemini_accounting_runtime.begin_request")
    def test_false_admission_is_cancelled_without_provider_post(
        self,
        begin_request,
        post,
    ):
        boundary = Mock()
        boundary.before_provider.return_value = False
        observer = Mock()
        observer.attempt.return_value = boundary
        begin_request.return_value = observer

        result = gemini_probe.probe_key(
            "gemini-3.6-flash",
            "private-probe-key",
        )

        post.assert_not_called()
        boundary.cancelled_pre_dispatch.assert_called_once()
        observer.resolve_failure.assert_called_once_with("admission_rejected")
        self.assertEqual(result["status"], "cancelled_pre_dispatch")
        self.assertEqual(result["http_code"], 0)
        self.assertEqual(result["evidence_kind"], "local_admission")
        self.assertNotIn("private-probe-key", json.dumps(result))

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T00:00:00-07:00",
        GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY="probe-boundary-test-hmac",
        GEMINI_KEY_PROJECT_GROUPS={"GEMINI_API": "gemini-project-1"},
    )
    @patch.dict(os.environ, {
        "GEMINI_API": "private-probe-key",
        "GEMINI_API2": "",
        "GEMINI_API3": "",
        "GEMINI_API4": "",
        "GEMINI_API5": "",
        "GEMINI_API6": "",
    }, clear=False)
    @patch("management.services.gemini_probe.requests.post")
    @patch(
        "management.services.gemini_accounting_runtime."
        "AttemptBoundary.before_provider",
        return_value=False,
    )
    def test_false_admission_persists_only_sanitized_local_outcome(
        self,
        _before_provider,
        post,
    ):
        result = gemini_probe.probe_key(
            "gemini-3.6-flash",
            "private-probe-key",
        )

        post.assert_not_called()
        graph = GeminiRequest.objects.get()
        attempt = GeminiRequestAttempt.objects.get(request_graph=graph)
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertEqual(graph.terminal_reason, "admission_rejected")
        self.assertEqual(attempt.outcome, "cancelled_pre_dispatch")
        self.assertEqual(
            attempt.fsm_state,
            GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
        )
        self.assertEqual(attempt.failure_kind, "stale_provider_boundary")
        self.assertIsNone(attempt.provider_started_at)
        persisted = json.dumps({
            "graph": graph.candidate_plan,
            "outcomes": graph.candidate_outcomes,
            "attempt": {
                "failure_kind": attempt.failure_kind,
                "provider_reason": attempt.provider_reason,
                "error_detail": attempt.error_detail,
            },
            "result": result,
        })
        self.assertNotIn("private-probe-key", persisted)

    @patch("management.services.gemini_probe.requests.post")
    @patch("management.services.gemini_accounting_runtime.begin_request")
    def test_true_admission_preserves_success_and_settles_boundary(
        self,
        begin_request,
        post,
    ):
        boundary = Mock()
        boundary.before_provider.return_value = True
        observer = Mock()
        observer.attempt.return_value = boundary
        begin_request.return_value = observer
        post.return_value = self._success_response()

        result = gemini_probe.probe_key(
            "gemini-3.6-flash",
            "private-probe-key",
        )

        post.assert_called_once()
        boundary.manual_result.assert_called_once_with(
            succeeded=True,
            http_code=200,
            failure_kind="",
            usage={"promptTokenCount": 4, "totalTokenCount": 5},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_code"], 200)

    @patch("management.services.gemini_probe.requests.post")
    @patch("management.services.gemini_accounting_runtime.begin_request")
    def test_observer_failure_before_decision_preserves_explicit_probe(
        self,
        begin_request,
        post,
    ):
        begin_request.side_effect = RuntimeError("observer unavailable")
        post.return_value = self._success_response()

        result = gemini_probe.probe_key(
            "gemini-3.6-flash",
            "private-probe-key",
        )

        post.assert_called_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["http_code"], 200)
        self.assertNotIn("observer unavailable", json.dumps(result))

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T00:00:00-07:00",
    )
    @patch("management.services.gemini_probe.requests.post")
    @patch(
        "management.services.gemini_accounting_runtime.begin_request",
        side_effect=RuntimeError("observer unavailable"),
    )
    def test_required_observer_failure_stops_probe_before_provider(self, _begin, post):
        result = gemini_probe.probe_key("gemini-3.6-flash", "private-probe-key")

        post.assert_not_called()
        self.assertEqual(result["status"], "cancelled_pre_dispatch")
        self.assertEqual(result["evidence_kind"], "local_admission")
        self.assertEqual(result["admission_reason"], "provider_accounting_unavailable")
        self.assertNotIn("private-probe-key", json.dumps(result))

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T00:00:00-07:00",
    )
    @patch("management.services.gemini_probe.requests.post")
    @patch("management.services.gemini_accounting_runtime.begin_request")
    def test_required_null_observer_stops_probe_before_provider(self, begin_request, post):
        begin_request.return_value = type("DisabledObserver", (), {"enabled": False})()

        result = gemini_probe.probe_key("gemini-3.6-flash", "private-probe-key")

        post.assert_not_called()
        self.assertEqual(result["status"], "cancelled_pre_dispatch")
        self.assertEqual(result["admission_reason"], "provider_accounting_unavailable")

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T00:00:00-07:00",
    )
    @patch("management.services.gemini_probe.requests.post")
    @patch("management.services.gemini_accounting_runtime.begin_request")
    def test_required_denied_boundary_stops_probe_before_provider(self, begin_request, post):
        boundary = Mock()
        boundary.before_provider.return_value = False
        observer = Mock(enabled=True, provider_blocked=False)
        observer.attempt.return_value = boundary
        begin_request.return_value = observer

        result = gemini_probe.probe_key("gemini-3.6-flash", "private-probe-key")

        post.assert_not_called()
        boundary.cancelled_pre_dispatch.assert_called_once()
        self.assertEqual(result["status"], "cancelled_pre_dispatch")
        self.assertEqual(result["admission_reason"], "admission_rejected")

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T00:00:00-07:00",
    )
    @patch("management.services.gemini_probe.requests.post")
    @patch("management.services.gemini_accounting_runtime.begin_request")
    def test_required_admitted_boundary_posts_once(self, begin_request, post):
        boundary = Mock()
        boundary.before_provider.return_value = True
        observer = Mock(enabled=True, provider_blocked=False)
        observer.attempt.return_value = boundary
        begin_request.return_value = observer
        post.return_value = self._success_response()

        result = gemini_probe.probe_key("gemini-3.6-flash", "private-probe-key")

        post.assert_called_once()
        boundary.manual_result.assert_called_once()
        self.assertEqual(result["status"], "ok")


class GeminiProviderErrorTests(TestCase):
    def _response(self, code, payload):
        response = type("Response", (), {})()
        response.status_code = code
        response.text = json.dumps(payload)
        response.json = lambda: payload
        return response

    @patch("management.services.call_ai_analysis.requests.post")
    def test_invalid_api_key_400_is_safe_key_error(self, post):
        post.return_value = self._response(400, {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "do not persist this secret-key-value",
                "details": [{"reason": "API_KEY_INVALID"}],
            }
        })

        with self.assertRaises(caa._GeminiFatal) as ctx:
            caa._gemini_call_once(
                "gemini-3.6-flash", {"contents": []}, "secret-key-value", parse=False
            )

        self.assertIn("API_KEY_INVALID", str(ctx.exception))
        self.assertNotIn("secret-key-value", str(ctx.exception))

    @patch("management.services.call_ai_analysis.requests.post")
    def test_timeout_is_not_labeled_as_http_503(self, post):
        post.side_effect = requests.ReadTimeout("read timeout")

        with self.assertRaises(caa._GeminiTransient) as ctx:
            caa._gemini_call_once(
                "gemini-3.6-flash", {"contents": []}, "key", parse=False
            )

        self.assertTrue(str(ctx.exception).startswith("timeout:"))
        self.assertNotIn("503", str(ctx.exception))

    @patch("management.services.call_ai_analysis.requests.post")
    def test_permission_error_keeps_provider_status_without_body(self, post):
        post.return_value = self._response(403, {
            "error": {
                "status": "PERMISSION_DENIED",
                "message": "private provider body",
            }
        })

        with self.assertRaises(caa._GeminiModelUnavailable) as ctx:
            caa._gemini_call_once(
                "gemini-3.6-flash", {"contents": []}, "key", parse=False
            )

        self.assertIn("PERMISSION_DENIED", str(ctx.exception))
        self.assertNotIn("private provider body", str(ctx.exception))


class GeminiProbeCommandTests(TestCase):
    @patch.dict("os.environ", {
        "GEMINI_API": "secret-one",
        "GEMINI_API2": "secret-two",
        "GEMINI_API3": "secret-three",
        "GEMINI_API4": "secret-four",
        "GEMINI_API5": "secret-five",
        "GEMINI_API6": "secret-six",
    }, clear=False)
    @patch("management.services.gemini_probe.probe_key")
    def test_probe_command_checks_all_keys_and_redacts_values(self, probe_key, captured=None):
        probe_key.side_effect = lambda model, key, timeout: {
            "status": "ok",
            "http_code": 200,
            "finish_reason": "STOP",
            "latency_ms": 5,
            "model": model,
        }

        from io import StringIO

        output = StringIO()
        call_command(
            "probe_ig_gemini_pool",
            role="chat",
            model="gemini-3.6-flash",
            parallel=2,
            confirm_quota_spend=True,
            stdout=output,
        )

        self.assertEqual(probe_key.call_count, 6)
        self.assertNotIn("secret-one", output.getvalue())
        self.assertIn("GEMINI_API", output.getvalue())
        self.assertEqual(
            GeminiKeyState.objects.filter(last_probe_status="ok", last_probe_model="gemini-3.6-flash").count(),
            6,
        )

    @patch.dict("os.environ", {
        "GEMINI_API": "secret-one",
        "GEMINI_API2": "secret-two",
        "GEMINI_API3": "secret-three",
        "GEMINI_API4": "secret-four",
        "GEMINI_API5": "secret-five",
        "GEMINI_API6": "secret-six",
    }, clear=False)
    @patch("management.services.gemini_probe.probe_key_metadata")
    @patch("management.services.gemini_probe.probe_key")
    def test_probe_command_is_metadata_only_without_explicit_quota_flag(
        self, probe_key, probe_key_metadata
    ):
        probe_key_metadata.side_effect = lambda model, key, timeout: {
            "status": "metadata_ok",
            "http_code": 200,
            "finish_reason": "",
            "latency_ms": 5,
            "model": model,
        }

        from io import StringIO

        call_command(
            "probe_ig_gemini_pool",
            role="chat",
            model="gemini-3.8-flash",
            parallel=2,
            stdout=StringIO(),
        )

        self.assertEqual(probe_key_metadata.call_count, 6)
        probe_key.assert_not_called()
