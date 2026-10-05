import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from management.services import call_ai_analysis as ai
from management.services.ig_provider_dispatch_budget import (
    ProviderDispatchBudget,
    ValidationDecision,
    normalize_validation_decision,
    sanitized_validation_usage,
)


PRIMARY = "gemini-3.7-flash"
FALLBACK = "gemini-3.5-flash-lite"


class _Response:
    def __init__(self, text="ok", *, status_code=200):
        self.status_code = status_code
        self.text = text

    def json(self):
        if self.status_code != 200:
            return {
                "error": {
                    "code": self.status_code,
                    "status": "UNAVAILABLE",
                    "message": "provider unavailable",
                }
            }
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": self.text}]},
            }],
            "usageMetadata": {
                "promptTokenCount": 4,
                "totalTokenCount": 8,
            },
        }


class _Boundary:
    attempt_id = None

    def __init__(self):
        self.succeeded_calls = 0
        self.failed_calls = 0
        self.cancelled_calls = 0
        self.failed_usage = None
        self.failure_kind = ""

    def validate_ownership(self):
        return True

    def before_provider(self, **_kwargs):
        return True

    def succeeded(self, _usage=None):
        self.succeeded_calls += 1

    def failed(self, _error, *, usage=None, failure_kind=""):
        self.failed_calls += 1
        self.failed_usage = usage
        self.failure_kind = failure_kind

    def cancelled_pre_dispatch(self, _error=None):
        self.cancelled_calls += 1


class _Observer:
    enabled = True
    provider_blocked = False
    request_id = "validated-request"

    def __init__(self):
        self.boundaries = []
        self.remaining_reasons = []
        self.failure_reasons = []

    def candidate_index(self, key_name, _model):
        return int(str(key_name).rsplit("-", 1)[-1])

    def attempt(self, **_kwargs):
        boundary = _Boundary()
        self.boundaries.append(boundary)
        return boundary

    def record_not_attempted(self, **_kwargs):
        return None

    def record_remaining(self, reason, **_kwargs):
        self.remaining_reasons.append(reason)

    def resolve_failure(self, reason):
        self.failure_reasons.append(reason)


def _candidate(key_number, model):
    return {
        "key_name": f"test-{key_number}",
        "key_value": f"secret-{key_number}",
        "model": model,
        "project_identity": "",
        "identity_status": "unknown",
        "skip_reason": "",
    }


def _payload():
    return {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}


class DispatchBudgetUnitTests(SimpleTestCase):
    def test_budget_is_two_actual_dispatches_and_one_repair(self):
        budget = ProviderDispatchBudget()

        self.assertTrue(budget.consume_dispatch())
        self.assertTrue(budget.consume_repair())
        self.assertFalse(budget.consume_repair())
        self.assertTrue(budget.consume_dispatch())
        self.assertFalse(budget.consume_dispatch())

    def test_validation_decision_bounds_reason_codes(self):
        raw = type("Result", (), {
            "valid": False,
            "reasons": ("UNVERIFIED_PRICE", "bad value", "unverified_price"),
        })()

        decision = normalize_validation_decision(raw)

        self.assertEqual(decision.reason_codes, ("unverified_price",))

    def test_validation_usage_exposes_only_bounded_runtime_fields(self):
        usage = sanitized_validation_usage({
            "totalTokenCount": "8",
            "_request_inline_count": 3,
            "providerPrivateField": "do-not-forward",
        })

        self.assertEqual(usage["totalTokenCount"], 8)
        self.assertEqual(usage["_request_inline_count"], 3)
        self.assertNotIn("providerPrivateField", usage)


@override_settings(
    GEMINI_ACCOUNTING_V2_MODE="off",
    GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="",
)
class ValidatedChatDispatchTests(SimpleTestCase):
    def _patch_pool(self, candidates, *, observer=None):
        patches = [
            patch.object(
                ai.gemini_keys,
                "live_chat_candidate_plan",
                return_value=candidates,
            ),
            patch.object(
                ai.gemini_scoreboard,
                "order_candidates",
                side_effect=lambda rows, **_kwargs: rows,
            ),
            patch.object(ai.gemini_keys, "model_circuit_open", return_value=False),
            patch.object(ai.gemini_keys, "model_quota_pressure", return_value=False),
            patch.object(ai.gemini_keys, "record_attempt"),
            patch.object(ai.gemini_keys, "record_model_success"),
        ]
        if observer is not None:
            patches.append(
                patch(
                    "management.services.gemini_accounting_runtime.begin_request",
                    return_value=observer,
                )
            )
        return patches

    def test_invalid_result_repairs_once_and_only_valid_attempt_wins(self):
        observer = _Observer()
        candidates = [_candidate(1, PRIMARY)]
        posted_bodies = []
        responses = iter([_Response("bad"), _Response("good")])

        def post(_url, **kwargs):
            posted_bodies.append(json.loads(kwargs["data"]))
            return next(responses)

        def repair(payload, parsed, reasons):
            self.assertEqual(parsed, "bad")
            self.assertEqual(reasons, ("unverified_price",))
            payload["contents"][0]["parts"].append({"text": "REPAIR_ONCE"})
            return payload

        def validate(parsed, *, usage):
            self.assertEqual(usage["_request_inline_count"], 0)
            return ValidationDecision(
                valid=parsed == "good",
                reason_codes=() if parsed == "good" else ("unverified_price",),
            )

        patches = self._patch_pool(candidates, observer=observer)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], \
             patch.object(ai.requests, "post", side_effect=post):
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[PRIMARY],
                result_validator=validate,
                repair_payload_factory=repair,
                max_actual_dispatches=2,
            )

        self.assertEqual(result["parsed"], "good")
        self.assertEqual(result["meta"]["request_id"], observer.request_id)
        self.assertEqual(len(posted_bodies), 2)
        self.assertNotIn("REPAIR_ONCE", json.dumps(posted_bodies[0]))
        self.assertIn("REPAIR_ONCE", json.dumps(posted_bodies[1]))
        self.assertEqual(observer.boundaries[0].succeeded_calls, 0)
        self.assertEqual(observer.boundaries[0].failed_calls, 1)
        self.assertEqual(observer.boundaries[0].failed_usage["totalTokenCount"], 8)
        self.assertEqual(observer.boundaries[0].failure_kind, "local_semantic_rejection")
        self.assertEqual(observer.boundaries[1].succeeded_calls, 1)

    def test_fallback_consumes_second_http_and_leaves_no_repair_dispatch(self):
        candidates = [_candidate(1, PRIMARY), _candidate(2, FALLBACK)]
        responses = iter([_Response(status_code=503), _Response("bad")])
        repair = Mock(return_value=_payload())

        patches = self._patch_pool(candidates)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patch.object(
                 ai.requests, "post", side_effect=lambda *_args, **_kwargs: next(responses)
             ) as post:
            with self.assertRaises(ai.CallAIAnalysisError):
                ai.gemini_generate_text(
                    _payload(),
                    role="chat",
                    model_chain_override=[PRIMARY, FALLBACK],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(
                        valid=False,
                        reason_codes=("unverified_price",),
                    ),
                    repair_payload_factory=repair,
                    max_actual_dispatches=2,
                )

        self.assertEqual(post.call_count, 2)
        repair.assert_not_called()

    def test_two_503_dispatches_retain_typed_outage_failure(self):
        candidates = [_candidate(1, PRIMARY), _candidate(2, FALLBACK)]
        patches = self._patch_pool(candidates)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patch.object(
                 ai.requests,
                 "post",
                 side_effect=[_Response(status_code=503), _Response(status_code=503)],
             ) as post:
            with self.assertRaises(ai.CallAIAnalysisError) as caught:
                ai.gemini_generate_text(
                    _payload(),
                    role="chat",
                    model_chain_override=[PRIMARY, FALLBACK],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(
                        valid=True,
                    ),
                    repair_payload_factory=lambda payload, _parsed, _reasons: payload,
                    max_actual_dispatches=2,
                )

        self.assertEqual(post.call_count, 2)
        self.assertEqual(caught.exception.failure_kind, "http_5xx")

    def test_default_caller_keeps_single_success_without_validation(self):
        candidates = [_candidate(1, PRIMARY)]
        patches = self._patch_pool(candidates)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patch.object(
                 ai.requests, "post", return_value=_Response("legacy-ok")
             ) as post:
            result = ai.gemini_generate_text(
                _payload(), role="chat", model_chain_override=[PRIMARY]
            )

        self.assertEqual(result["parsed"], "legacy-ok")
        self.assertEqual(post.call_count, 1)

    def test_deadline_is_rechecked_before_repair_dispatch(self):
        candidates = [_candidate(1, PRIMARY), _candidate(2, PRIMARY)]
        patches = self._patch_pool(candidates)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patch.object(
                 ai.requests, "post", return_value=_Response("bad")
             ) as post, patch.object(
                 ai, "_chat_timeout", side_effect=[(1.0, 1.0), None]
             ):
            with self.assertRaises(ai.CallAIAnalysisError):
                ai.gemini_generate_text(
                    _payload(),
                    role="chat",
                    model_chain_override=[PRIMARY],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(
                        valid=False,
                        reason_codes=("unverified_price",),
                    ),
                    repair_payload_factory=lambda payload, _parsed, _reasons: payload,
                )

        self.assertEqual(post.call_count, 1)
