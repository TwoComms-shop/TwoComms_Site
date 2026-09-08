import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from management.tests_ig_provider_dispatch_budget import _Observer
from management.services import call_ai_analysis as ai
from management.services.ig_provider_dispatch_budget import (
    ProviderDispatchBudget,
    ValidationDecision,
)


SCARCE = "gemini-3.7-flash"
LITE = "gemini-3.5-flash-lite"


class _Response:
    def __init__(self, text="ok", *, status_code=200):
        self.status_code = status_code
        self.text = text

    def json(self):
        if self.status_code != 200:
            return {
                "error": {
                    "code": self.status_code,
                    "status": "UNAUTHENTICATED" if self.status_code == 401 else "UNAVAILABLE",
                    "message": "API_KEY_INVALID" if self.status_code == 401 else "provider unavailable",
                }
            }
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": self.text}]},
            }],
            "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 8},
        }


def _candidate(number, model, *, key_name=None):
    return {
        "key_name": key_name or f"test-{number}",
        "key_value": f"secret-{number}",
        "model": model,
        "project_identity": f"project-{number}",
        "identity_status": "known",
        "skip_reason": "",
    }


def _payload():
    return {"contents": [{"role": "user", "parts": [{"text": "original"}]}]}


@override_settings(
    GEMINI_ACCOUNTING_V2_MODE="off",
    GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="",
)
class ResilientValidatedDispatchTests(SimpleTestCase):
    def test_explicit_budget_accepts_one_through_eight_only(self):
        self.assertEqual(ProviderDispatchBudget(max_dispatches=1).max_dispatches, 1)
        self.assertEqual(ProviderDispatchBudget(max_dispatches=8).max_dispatches, 8)
        for invalid in (0, 9):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ProviderDispatchBudget(max_dispatches=invalid)

    def test_unknown_or_malformed_model_quota_is_conservatively_scarce(self):
        self.assertFalse(ProviderDispatchBudget._is_scarce_model(""))
        self.assertTrue(ProviderDispatchBudget._is_scarce_model("gemini-3.8-flash"))
        with override_settings(
            GEMINI_MODEL_BUDGETS={"gemini-broken": {"rpd": "invalid"}}
        ):
            self.assertTrue(
                ProviderDispatchBudget._is_scarce_model("gemini-broken")
            )

    def _stack(self, candidates, *, observer=None):
        stack = ExitStack()
        stack.enter_context(patch.object(
            ai.gemini_keys, "live_chat_candidate_plan", return_value=candidates,
        ))
        stack.enter_context(patch.object(
            ai.gemini_scoreboard,
            "order_candidates",
            side_effect=lambda rows, **_kwargs: rows,
        ))
        stack.enter_context(patch.object(
            ai.gemini_keys, "model_circuit_open", return_value=False,
        ))
        stack.enter_context(patch.object(
            ai.gemini_keys, "model_quota_pressure", return_value=False,
        ))
        attempts = stack.enter_context(patch.object(ai.gemini_keys, "record_attempt"))
        stack.enter_context(patch.object(ai.gemini_keys, "record_model_success"))
        if observer is not None:
            stack.enter_context(patch(
                "management.services.gemini_accounting_runtime.begin_request",
                return_value=observer,
            ))
        return stack, attempts

    def test_five_invalid_keys_rotate_to_sixth_success(self):
        candidates = [_candidate(number, LITE) for number in range(1, 7)]
        stack, attempts = self._stack(candidates)
        with stack, patch.object(
            ai.requests,
            "post",
            side_effect=[*[_Response(status_code=401) for _ in range(5)], _Response("valid")],
        ) as post:
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid"
                ),
                max_actual_dispatches=8,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(post.call_count, 6)
        actual = [
            call.kwargs.get("outcome")
            for call in attempts.call_args_list
            if call.kwargs.get("outcome") in {"failed", "succeeded"}
        ]
        self.assertEqual(actual, ["failed"] * 5 + ["succeeded"])

    def test_second_invalid_result_moves_to_next_model_with_original_payload(self):
        candidates = [_candidate(1, SCARCE), _candidate(2, LITE)]
        observer = _Observer()
        posted = []

        def post(_url, **kwargs):
            posted.append(json.loads(kwargs["data"]))
            return [_Response("bad-one"), _Response("bad-two"), _Response("valid")][
                len(posted) - 1
            ]

        def repair(payload, _parsed, _reasons):
            payload["contents"][0]["parts"].append({"text": "REPAIR_ONLY"})
            return payload

        stack, attempts = self._stack(candidates, observer=observer)
        with stack, patch.object(ai.requests, "post", side_effect=post):
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[SCARCE, LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid",
                    reason_codes=() if parsed == "valid" else ("invalid_schema",),
                ),
                repair_payload_factory=repair,
                max_actual_dispatches=4,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(posted), 3)
        self.assertNotIn("REPAIR_ONLY", json.dumps(posted[0]))
        self.assertIn("REPAIR_ONLY", json.dumps(posted[1]))
        self.assertNotIn("REPAIR_ONLY", json.dumps(posted[2]))
        self.assertEqual(len(observer.boundaries), 3)
        self.assertEqual(observer.boundaries[0].failed_calls, 1)
        self.assertEqual(observer.boundaries[1].failed_calls, 1)
        self.assertEqual(observer.boundaries[2].succeeded_calls, 1)
        validation_decisions = [
            call.kwargs.get("decision")
            for call in attempts.call_args_list
            if call.kwargs.get("failure_kind") == "invalid_response"
        ]
        self.assertEqual(validation_decisions, ["repair_result", "rotate_model"])
        self.assertNotIn("result_validation_failed", observer.failure_reasons)

    def test_repair_transport_failure_does_not_contaminate_fallback_payload(self):
        candidates = [_candidate(1, SCARCE), _candidate(2, LITE)]
        posted = []
        responses = iter([
            _Response("bad"),
            _Response(status_code=503),
            _Response("valid"),
        ])

        def post(_url, **kwargs):
            posted.append(json.loads(kwargs["data"]))
            return next(responses)

        def repair(payload, _parsed, _reasons):
            payload["contents"][0]["parts"].append({"text": "REPAIR_ONLY"})
            return payload

        stack, _attempts = self._stack(candidates)
        with stack, patch.object(ai.requests, "post", side_effect=post):
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[SCARCE, LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid",
                    reason_codes=() if parsed == "valid" else ("invalid_schema",),
                ),
                repair_payload_factory=repair,
                max_actual_dispatches=4,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(posted), 3)
        self.assertNotIn("REPAIR_ONLY", json.dumps(posted[0]))
        self.assertIn("REPAIR_ONLY", json.dumps(posted[1]))
        self.assertNotIn("REPAIR_ONLY", json.dumps(posted[2]))

    def test_all_failures_stop_at_explicit_global_dispatch_cap(self):
        candidates = [_candidate(number, LITE) for number in range(1, 10)]
        stack, _attempts = self._stack(candidates)
        with stack, patch.object(
            ai.requests, "post", return_value=_Response(status_code=401),
        ) as post:
            with self.assertRaises(ai.CallAIAnalysisError) as caught:
                ai.gemini_generate_text(
                    _payload(),
                    role="chat",
                    model_chain_override=[LITE],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(valid=True),
                    max_actual_dispatches=8,
                )

        self.assertEqual(post.call_count, 8)
        self.assertEqual(caught.exception.failure_kind, "invalid_key")

    def test_local_quota_skips_do_not_consume_actual_dispatches(self):
        aliases = ai.gemini_keys.ALL_KEYS[:3]
        candidates = [
            _candidate(number, LITE, key_name=alias)
            for number, alias in enumerate(aliases, start=1)
        ]
        stack, attempts = self._stack(candidates)
        stack.enter_context(patch.object(ai.gemini_keys, "is_available", return_value=True))
        stack.enter_context(patch.object(ai.gemini_keys, "acquire_key_lease", return_value="lease"))
        stack.enter_context(patch.object(ai.gemini_keys, "release_key_lease"))
        stack.enter_context(patch.object(ai.gemini_keys, "record_key_success"))
        stack.enter_context(patch.object(
            ai.gemini_quota, "try_reserve", side_effect=[False, False, True],
        ))
        stack.enter_context(patch.object(ai.gemini_quota, "settle"))
        with stack, patch.object(ai.requests, "post", return_value=_Response("valid")) as post:
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid"
                ),
                max_actual_dispatches=3,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(post.call_count, 1)
        skipped = [
            call.kwargs.get("not_attempted_reason")
            for call in attempts.call_args_list
            if call.kwargs.get("outcome") == "not_attempted"
        ]
        self.assertEqual(skipped[:2], ["quota_exhausted", "quota_exhausted"])

    def test_scarce_models_share_two_dispatch_cap_then_lite_can_succeed(self):
        second_scarce = "gemini-3.6-flash"
        candidates = [
            _candidate(1, SCARCE),
            _candidate(2, SCARCE),
            _candidate(3, second_scarce),
            _candidate(4, LITE),
        ]
        stack, _attempts = self._stack(candidates)
        urls = []

        def post(url, **_kwargs):
            urls.append(url)
            return _Response(status_code=401) if len(urls) <= 2 else _Response("valid")

        with stack, patch.object(ai.requests, "post", side_effect=post):
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[SCARCE, second_scarce, LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid"
                ),
                max_actual_dispatches=8,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(urls), 3)
        self.assertNotIn(second_scarce, urls[2])
        self.assertIn(LITE, urls[2])

    def test_deadline_stops_before_an_extra_dispatch(self):
        candidates = [_candidate(1, LITE), _candidate(2, LITE)]
        stack, _attempts = self._stack(candidates)
        with stack, patch.object(
            ai.requests, "post", return_value=_Response(status_code=503),
        ) as post, patch.object(ai, "_chat_timeout", side_effect=[(1.0, 1.0), None]):
            with self.assertRaises(ai.CallAIAnalysisError):
                ai.gemini_generate_text(
                    _payload(),
                    role="chat",
                    model_chain_override=[LITE],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(valid=True),
                    max_actual_dispatches=8,
                )

        self.assertEqual(post.call_count, 1)


class _DurableObserver(_Observer):
    def __init__(self, continuation):
        super().__init__()
        self.provider_continuation = continuation
        self.repair_reservations = []
        self.not_attempted = []

    def reserve_provider_repair(self, **kwargs):
        self.repair_reservations.append(kwargs)
        return True

    def record_not_attempted(self, **kwargs):
        self.not_attempted.append(kwargs)


class _ProviderBlockedBoundary:
    attempt_id = None
    provider_block_reason = "provider_wait"

    def validate_ownership(self):
        return True

    def before_provider(self, **_kwargs):
        return False

    def cancelled_pre_dispatch(self, _error=None):
        return None


class _ProviderBlockedObserver(_DurableObserver):
    def attempt(self, **_kwargs):
        boundary = _ProviderBlockedBoundary()
        self.boundaries.append(boundary)
        return boundary


@override_settings(
    GEMINI_ACCOUNTING_V2_MODE="shadow",
    GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="",
)
class DurableContinuationDispatchTests(SimpleTestCase):
    _stack = ResilientValidatedDispatchTests._stack

    def _continuation(
        self,
        candidates,
        *,
        http_remaining=8,
        scarce_remaining=2,
        ready=True,
        reason="",
    ):
        frozen = []
        for index, candidate in enumerate(candidates, start=1):
            frozen.append({
                "candidate_index": index,
                "key_name": candidate["key_name"],
                "model": candidate["model"],
                "project_identity": candidate["project_identity"],
                "identity_status": candidate["identity_status"],
                "skip_reason": candidate.get("skip_reason", ""),
                "scarce": candidate.get("scarce", False),
            })
        return SimpleNamespace(
            ready=ready,
            reason=reason,
            candidate_plan=tuple(frozen),
            http_remaining=http_remaining,
            scarce_remaining=scarce_remaining,
        )

    def test_continuation_clamps_remaining_http_without_resetting_to_eight(self):
        candidates = [_candidate(1, LITE), _candidate(2, LITE)]
        observer = _DurableObserver(
            self._continuation(candidates, http_remaining=1)
        )
        stack, _attempts = self._stack(candidates, observer=observer)
        with stack, patch.object(
            ai.requests, "post", return_value=_Response(status_code=401),
        ) as post:
            with self.assertRaises(ai.CallAIAnalysisError):
                ai.gemini_generate_text(
                    _payload(),
                    role="chat",
                    model_chain_override=[LITE],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(
                        valid=True
                    ),
                    max_actual_dispatches=8,
                )

        self.assertEqual(post.call_count, 1)

    def test_continuation_uses_frozen_scarcity_and_reserves_exact_repair(self):
        candidate = _candidate(1, SCARCE)
        candidate["scarce"] = False
        observer = _DurableObserver(
            self._continuation(
                [candidate], http_remaining=2, scarce_remaining=0
            )
        )
        responses = iter([_Response("bad"), _Response("valid")])
        stack, _attempts = self._stack([candidate], observer=observer)
        with stack, patch.object(ai.requests, "post", side_effect=responses) as post:
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[SCARCE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid",
                    reason_codes=() if parsed == "valid" else ("invalid_schema",),
                ),
                repair_payload_factory=lambda payload, _parsed, _reasons: payload,
                max_actual_dispatches=8,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(observer.repair_reservations, [{
            "key_name": candidate["key_name"],
            "model": SCARCE,
            "candidate_index": 1,
        }])

    def test_continuation_restores_frozen_model_order(self):
        scarce = _candidate(1, SCARCE)
        lite = _candidate(2, LITE)
        observer = _DurableObserver(self._continuation([scarce, lite]))
        stack, _attempts = self._stack([scarce, lite], observer=observer)
        urls = []

        def post(url, **_kwargs):
            urls.append(url)
            return _Response(status_code=401) if len(urls) == 1 else _Response("valid")

        with stack, patch.object(ai.requests, "post", side_effect=post):
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[LITE, SCARCE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid"
                ),
                max_actual_dispatches=8,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertIn(SCARCE, urls[0])
        self.assertIn(LITE, urls[1])

    def test_missing_local_alias_remains_an_audited_skip(self):
        missing = _candidate(1, LITE)
        present = _candidate(2, LITE)
        observer = _DurableObserver(self._continuation([missing, present]))
        stack, _attempts = self._stack([present], observer=observer)
        with stack, patch.object(ai.requests, "post", return_value=_Response("valid")):
            result = ai.gemini_generate_text(
                _payload(),
                role="chat",
                model_chain_override=[LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid"
                ),
                max_actual_dispatches=8,
            )

        self.assertEqual(result["parsed"], "valid")
        self.assertTrue(any(
            row["key_name"] == missing["key_name"]
            and row["reason"] == "provider_candidate_identity_changed"
            for row in observer.not_attempted
        ))

    def test_zero_continuation_remaining_stops_before_http(self):
        candidate = _candidate(1, LITE)
        observer = _DurableObserver(
            self._continuation(
                [candidate], http_remaining=0, ready=False,
                reason="provider_dispatch_budget",
            )
        )
        stack, _attempts = self._stack([candidate], observer=observer)
        with stack, patch.object(ai.requests, "post") as post:
            with self.assertRaises(ai.CallAIAnalysisError) as caught:
                ai.gemini_generate_text(
                    _payload(), role="chat", model_chain_override=[LITE],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(
                        valid=True
                    ),
                    max_actual_dispatches=8,
                )

        self.assertEqual(post.call_count, 0)
        self.assertEqual(caught.exception.failure_kind, "provider_dispatch_budget")

    def test_atomic_provider_rejection_keeps_sanitized_reason(self):
        candidate = _candidate(1, LITE)
        observer = _ProviderBlockedObserver(self._continuation([candidate]))
        stack, _attempts = self._stack([candidate], observer=observer)
        with stack, patch.object(ai.requests, "post") as post:
            with self.assertRaises(ai.CallAIAnalysisError) as caught:
                ai.gemini_generate_text(
                    _payload(), role="chat", model_chain_override=[LITE],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(
                        valid=True
                    ),
                    max_actual_dispatches=8,
                )

        self.assertEqual(post.call_count, 0)
        self.assertEqual(caught.exception.failure_kind, "provider_wait")


    def test_two_remaining_calls_can_still_rotate_after_prior_repair(self):
        candidates = [_candidate(1, SCARCE), _candidate(2, LITE)]
        observer = _DurableObserver(self._continuation(candidates, http_remaining=2))
        observer.reserve_provider_repair = lambda **_kwargs: False
        stack, _attempts = self._stack(candidates, observer=observer)
        with stack, patch.object(ai.requests, "post", side_effect=[
            _Response("bad"), _Response("valid"),
        ]) as post:
            result = ai.gemini_generate_text(
                _payload(), role="chat", model_chain_override=[SCARCE, LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(
                    valid=parsed == "valid", reason_codes=("invalid_schema",),
                ),
                repair_payload_factory=lambda payload, _parsed, _reasons: payload,
                max_actual_dispatches=8,
            )
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(post.call_count, 2)

    def test_not_ready_zero_allowance_preserves_actual_reason(self):
        candidate = _candidate(1, LITE)
        observer = _DurableObserver(self._continuation(
            [candidate], http_remaining=0, ready=False, reason="provider_lineage_invalid",
        ))
        stack, _attempts = self._stack([candidate], observer=observer)
        with stack, patch.object(ai.requests, "post") as post:
            with self.assertRaises(ai.CallAIAnalysisError) as caught:
                ai.gemini_generate_text(
                    _payload(), role="chat", model_chain_override=[LITE],
                    result_validator=lambda _parsed, *, usage: ValidationDecision(valid=True),
                    max_actual_dispatches=8,
                )
        post.assert_not_called()
        self.assertEqual(caught.exception.failure_kind, "provider_lineage_invalid")

    def test_current_candidate_block_narrows_frozen_route(self):
        frozen = [_candidate(1, LITE), _candidate(2, LITE)]
        current = [dict(frozen[0], skip_reason="circuit_open"), frozen[1]]
        observer = _DurableObserver(self._continuation(frozen))
        stack, _attempts = self._stack(current, observer=observer)
        with stack, patch.object(ai.requests, "post", return_value=_Response("valid")) as post:
            result = ai.gemini_generate_text(
                _payload(), role="chat", model_chain_override=[LITE],
                result_validator=lambda parsed, *, usage: ValidationDecision(valid=parsed == "valid"),
                max_actual_dispatches=8,
            )
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(post.call_count, 1)
        self.assertTrue(any(row["key_name"] == frozen[0]["key_name"] and
                            row["reason"] == "circuit_open" for row in observer.not_attempted))


class LiveProviderFailureClassificationTests(SimpleTestCase):
    def test_wait_and_exhaustion_remain_distinct_safe_codes(self):
        from management.services.instagram_bot import _gemini_failure_kind

        for reason in (
            "provider_wait", "provider_dispatch_budget", "scarce_model_budget",
            "provider_horizon_exhausted", "provider_candidates_exhausted",
            "legacy_manifest_missing", "legacy_source_changed",
        ):
            with self.subTest(reason=reason):
                error = RuntimeError("opaque provider failure")
                error.failure_kind = reason
                self.assertEqual(_gemini_failure_kind(error), reason)

    def test_unknown_provider_text_is_not_promoted_to_routing_code(self):
        from management.services.instagram_bot import _gemini_failure_kind

        error = RuntimeError("opaque provider failure")
        error.failure_kind = "provider_untrusted_arbitrary_value"
        self.assertEqual(_gemini_failure_kind(error), "generation_error")

    def test_only_provider_wait_with_due_before_original_horizon_can_recover(self):
        from datetime import timedelta
        from django.utils import timezone
        from management.services.bot_reply_fallback import is_generic_provider_outage

        row = SimpleNamespace(text="Привіт, які є худі?")
        now = timezone.now()
        self.assertTrue(is_generic_provider_outage(
            row, failure_kind="provider_wait", next_due_at=now + timedelta(seconds=20),
            horizon_at=now + timedelta(minutes=4),
        ))
        for reason in ("provider_dispatch_budget", "provider_horizon_exhausted", "legacy_manifest_missing"):
            self.assertFalse(is_generic_provider_outage(row, failure_kind=reason))
        self.assertFalse(is_generic_provider_outage(row, failure_kind="provider_wait"))
        self.assertFalse(is_generic_provider_outage(
            row, failure_kind="provider_wait", next_due_at=now + timedelta(minutes=5),
            horizon_at=now + timedelta(minutes=4),
        ))
