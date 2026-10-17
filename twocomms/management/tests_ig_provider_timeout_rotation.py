"""One scarce timeout preserves another model opportunity and strict repairs."""
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_gemini_accounting_shadow as fixtures, tests_gemini_response_recovery as response_fixtures
from management.models import GeminiRequest, GeminiRequestAttempt
from management.services import call_ai_analysis as ai
from management.services.ig_revision_provider_execution import inspect_revision_provider_execution
from management.services.ig_response_control import parse_structured_response
from management.services.ig_response_guard import ProviderResponseGuard
from management.tests_gemini_response_recovery import response


@override_settings(**fixtures.SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class ScarcePrimaryTimeoutRotationTests(TransactionTestCase):
    setUp = response_fixtures.DurableResponseRecoveryTests.setUp
    run_reply = response_fixtures.DurableResponseRecoveryTests.run_reply

    def test_first_scarce_timeout_tries_distinct_frozen_model_without_empty_or_repair_proof(self):
        def timeout_first(count):
            if count == 1:
                raise ai.requests.ReadTimeout("fixture timeout")

        result, calls = self.run_reply([response(finish="STOP", text="valid")], before_response=timeout_first,
            model_chain=["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"])
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(calls), 2)
        attempts = list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).order_by("pk"))
        self.assertEqual([row.model for row in attempts], ["gemini-3.8-flash", "gemini-3.7-flash"])
        self.assertEqual(attempts[0].failure_kind, "read_timeout")
        self.assertIsNone(attempts[0].http_code)
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.winner_attempt_id, attempts[1].pk)
        self.assertNotIn("_provider_repair_reservation", graph.candidate_outcomes)
        self.case.revision.refresh_from_db()
        continuation = inspect_revision_provider_execution(self.case.revision)
        self.assertEqual(continuation.empty_response_count, 0)
        self.assertEqual(continuation.last_scarce_empty_attempt_id, 0)
        self.assertEqual((continuation.http_remaining, continuation.scarce_remaining), (6, 0))

    def test_first_timeout_retains_same_primary_when_no_verified_alternative_is_eligible(self):
        original_plan = ai.gemini_keys.live_chat_candidate_plan
        plan_reads = 0

        def plan(*args, **kwargs):
            nonlocal plan_reads
            plan_reads += 1
            rows = original_plan(*args, **kwargs)
            return [{**row, "skip_reason": "quota_exhausted"}
                if plan_reads > 1 and row["model"] != "gemini-3.8-flash" else row for row in rows]

        def timeout_first(count):
            if count == 1:
                raise ai.requests.ReadTimeout("fixture timeout")

        with patch.object(ai.gemini_keys, "live_chat_candidate_plan", side_effect=plan):
            result, calls = self.run_reply([response(finish="STOP", text="valid")], before_response=timeout_first,
                model_chain=["gemini-3.8-flash", "gemini-3.7-flash"])
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False)
            .order_by("pk").values_list("model", flat=True)), ["gemini-3.8-flash"] * 2)

    def test_timeout_rotation_keeps_single_repair_within_root_dispatch_budget(self):
        def timeout_first(count):
            if count == 1:
                raise ai.requests.ReadTimeout("fixture timeout")

        result, calls = self.run_reply([response(finish="STOP", text="invalid"), response(finish="STOP", text="valid")],
            before_response=timeout_first, reason="invalid_response_schema",
            model_chain=["gemini-3.8-flash", "gemini-3.5-flash-lite"])
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(calls), 3)
        attempts = list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).order_by("pk"))
        self.assertEqual([row.model for row in attempts], ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"])
        graph = GeminiRequest.objects.get()
        self.assertIn("_provider_repair_reservation", graph.candidate_outcomes)
        self.assertEqual(graph.winner_attempt_id, attempts[2].pk)
        self.case.revision.refresh_from_db()
        continuation = inspect_revision_provider_execution(self.case.revision)
        self.assertEqual((continuation.http_remaining, continuation.scarce_remaining, continuation.repair_remaining), (5, 1, False))

    def test_one_timeout_leaves_realistic_schema_repair_runway_inside_original_35_seconds(self):
        elapsed = 0.0

        def observed_duration(count):
            nonlocal elapsed
            elapsed += 14.1 if count == 1 else 7.5
            if count == 1:
                raise ai.requests.ReadTimeout("fixture timeout")

        with patch.object(ai.time, "monotonic", side_effect=lambda: 1000.0 + elapsed):
            result, calls = self.run_reply([response(finish="STOP", text="invalid"), response(finish="STOP", text="valid")],
                before_response=observed_duration, reason="invalid_response_schema",
                model_chain=["gemini-3.8-flash", "gemini-3.5-flash-lite"])
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(calls), 3)
        self.assertAlmostEqual(elapsed, 29.1)
        self.assertGreaterEqual(calls[2][1]["timeout"][1], 7.5)
        self.assertLessEqual(21.6 + sum(calls[2][1]["timeout"]), 35.0)
        self.assertEqual(GeminiRequest.objects.get().deadline_ms, 35000)

    def test_cheap_primary_timeout_does_not_change_existing_project_retry_policy(self):
        def timeout_first(count):
            if count == 1:
                raise ai.requests.ReadTimeout("fixture timeout")

        result, calls = self.run_reply([response(finish="STOP", text="valid")], before_response=timeout_first,
            model_chain=["gemini-3.5-flash-lite", "gemini-3.8-flash"])
        self.assertEqual(result["parsed"], "valid")
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False)
            .order_by("pk").values_list("model", flat=True)), ["gemini-3.5-flash-lite"] * 2)


class IntelligenceRepairShapeTests(SimpleTestCase):
    def test_media_repair_example_has_valid_strict_shape_and_preserves_outcome_caveat(self):
        payload = {"contents": [{"role": "user", "parts": [{"text": "current image source"}]}]}
        repaired = ProviderResponseGuard.repair(payload, {}, ("invalid_response_schema", "schema_invalid_turn_intelligence"))
        guidance = repaired["contents"][-1]["parts"][0]["text"]
        example = guidance.split("A valid unreadable image format example is ", 1)[1].split(". This illustrates syntax only:", 1)[0]
        artifact = json.loads(example)
        parsed = parse_structured_response({"reply_text": "Уточніть, будь ласка, що зображено.", "controls": [], "turn_intelligence": artifact})
        self.assertTrue(parsed.valid, parsed.error)
        self.assertEqual(parsed.turn_intelligence.image_observations[0].outcome, "unreadable")
        self.assertEqual(parsed.turn_intelligence.media_observations[0].outcome, "unreadable")
        from management.services.ig_reply_truth import ReplyTruthContext
        guard = ProviderResponseGuard(context_factory=lambda *_args: ReplyTruthContext(),
            image_mimes=("image/jpeg",), expected_content_hashes=("a" * 64,), require_intelligence=True)
        self.assertTrue(guard.validate({"reply_text": "Уточніть, будь ласка, що зображено.", "controls": [],
            "turn_intelligence": artifact}, usage={"_request_inline_count": 1,
                "_request_inline_content_hashes": ["a" * 64]}).valid)
        self.assertIn("do not claim an understood image is unreadable", guidance)
        self.assertIn("actual global indexes", guidance)
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "current image source")
        self.assertEqual(len(payload["contents"]), 1)
