"""Receipt images use final enforced admission; all HTTP is mocked."""
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import json
from unittest.mock import patch

from PIL import Image
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.models import GeminiQuotaState, GeminiRequest, GeminiRequestAttempt, IgClient, InstagramBotMessage
from management.services import call_ai_analysis as ai, gemini_accounting_runtime as runtime
from management import tests_ig_nonlive_admission as nonlive

ENFORCE = nonlive.ENFORCE
MODEL = nonlive.MODEL


def image_bytes(format="JPEG", size=(32, 40)):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format=format)
    return output.getvalue()


def receipt_payload(raw, *, mime="image/jpeg", count=1):
    return {"contents": [{"role": "user", "parts": [{"text": "Inspect untrusted receipt evidence"},
        *[{"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode("ascii")}} for _ in range(count)]]}],
        "generationConfig": {"maxOutputTokens": 4096, "responseMimeType": "application/json",
                             "mediaResolution": "MEDIA_RESOLUTION_HIGH"}}


def admission(raw, *, count=1, message_id=1):
    return runtime.ReceiptInlineAdmission(sources=tuple(runtime.ReceiptInlineSource(
        source_message_id=message_id, source_part_id=f"part-{index}",
        content_hash=hashlib.sha256(raw).hexdigest(), storage_name=f"private/{index}", use_token="lease")
        for index in range(count)))


def estimate(payload, capability, model=MODEL):
    return runtime._estimate_receipt_inline(json.dumps(payload).encode(), model=model, admission=capability)


class ReceiptEstimatorTests(SimpleTestCase):
    def test_every_configured_flash_image_family_has_the_same_bounded_profile(self):
        raw = image_bytes()
        for model in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"):
            with self.subTest(model=model):
                self.assertIsNotNone(estimate(receipt_payload(raw), admission(raw), model))

    def test_supported_decoded_images_preserve_transport_and_bound_tokens(self):
        for mime, format in (("image/jpeg", "JPEG"), ("image/png", "PNG"), ("image/webp", "WEBP")):
            with self.subTest(mime=mime):
                raw = image_bytes(format)
                payload = receipt_payload(raw, mime=mime, count=8)
                before = deepcopy(payload)
                result = estimate(payload, admission(raw, count=8))
                self.assertIsNotNone(result)
                self.assertEqual(result.inline_count, 8)
                self.assertGreater(result.prompt_tokens, 8 * runtime.RECEIPT_IMAGE_TOKEN_RESERVE)
                self.assertEqual(payload, before)

    def test_unsupported_inputs_and_missing_or_forged_capabilities_stay_unknown(self):
        raw = image_bytes()
        payload, capability = receipt_payload(raw), admission(raw)
        self.assertIsNone(estimate(payload, None))
        self.assertIsNone(estimate(payload, {"version": runtime.RECEIPT_INLINE_ESTIMATOR_VERSION}))
        self.assertIsNone(estimate(payload, replace(capability, version="unaccepted")))
        self.assertIsNone(estimate(payload, capability, "gemini-2.5-flash"))
        self.assertIsNone(estimate(payload, capability, "gemini-3-not-registered"))
        for mime in ("application/pdf", "audio/wav", "video/mp4", "image/heic"):
            self.assertIsNone(estimate(receipt_payload(raw, mime=mime), capability))

    def test_malformed_base64_bytes_mime_dimensions_animation_and_hash_are_unknown(self):
        raw = image_bytes()
        payload, capability = receipt_payload(raw), admission(raw)
        for encoded in ("%%%", "AAAA=", base64.b64encode(b"not-an-image").decode()):
            candidate = deepcopy(payload)
            candidate["contents"][0]["parts"][1]["inline_data"]["data"] = encoded
            self.assertIsNone(estimate(candidate, capability))
        self.assertIsNone(estimate(receipt_payload(raw, mime="image/png"), capability))
        truncated = raw[:100]
        self.assertIsNone(estimate(receipt_payload(truncated), admission(truncated)))
        self.assertIsNone(estimate(payload, replace(capability, sources=(replace(capability.sources[0], content_hash="f" * 64),))))
        with patch("management.services.ig_media_url_policy.MAX_IMAGE_PIXELS", 1):
            self.assertIsNone(estimate(payload, capability))
        animated = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(animated, format="WEBP", save_all=True,
            append_images=[Image.new("RGB", (8, 8), "black")], duration=100, loop=0)
        animation = animated.getvalue()
        self.assertIsNone(estimate(receipt_payload(animation, mime="image/webp"), admission(animation)))

    def test_final_envelope_and_caps_are_independently_verified(self):
        raw = image_bytes()
        payload, capability = receipt_payload(raw), admission(raw)
        changes = (
            lambda p: p.update(tools=[{"googleSearch": {}}]),
            lambda p: p.update(cachedContent="cache"),
            lambda p: p["generationConfig"].update(mediaResolution="MEDIA_RESOLUTION_LOW"),
            lambda p: p["generationConfig"].update(maxOutputTokens=True),
            lambda p: p["generationConfig"].update(maxOutputTokens=8192),
            lambda p: p["contents"][0]["parts"][1].update(mediaResolution={"level": "MEDIA_RESOLUTION_ULTRA_HIGH"}),
            lambda p: p["contents"][0]["parts"].append({"fileData": {"fileUri": "https://example.test/receipt"}}),
            lambda p: p["contents"][0]["parts"][0].update(text="x" * runtime.RECEIPT_INLINE_MAX_TEXT_BYTES),
        )
        for change in changes:
            candidate = deepcopy(payload)
            change(candidate)
            self.assertIsNone(estimate(candidate, capability))
        self.assertIsNone(estimate(receipt_payload(raw, count=9), admission(raw, count=9)))
        self.assertIsNone(estimate(payload, replace(capability, sources=capability.sources * 2)))
        with patch.object(runtime, "RECEIPT_INLINE_MAX_RAW_BYTES", len(raw) - 1):
            self.assertIsNone(estimate(payload, capability))

    def test_transport_size_does_not_stand_in_for_image_token_allowance(self):
        # Padding a valid JPEG affects transport size, not its HIGH allocation.
        raw = image_bytes() + b"\0" * (1024 * 1024)
        payload = receipt_payload(raw)
        result = estimate(payload, admission(raw))
        self.assertIsNotNone(result)
        self.assertLess(result.prompt_tokens, 10000)
        self.assertGreater(len(json.dumps(payload)) // 4, 250000)


@override_settings(**ENFORCE)
class ReceiptFinalAdmissionTests(TransactionTestCase):
    # Reuse fixture helpers without inheriting unrelated test methods.
    _profile = nonlive.NonliveFinalAdmissionTests._profile
    _state = nonlive.NonliveFinalAdmissionTests._state
    _facade = nonlive.NonliveFinalAdmissionTests._facade

    def setUp(self):
        # Synthetic 503 fixtures must not make another test's real planner
        # skip a model through the gateway's process-local overload cache.
        ai.gemini_keys.clear_model_overload()
        self.addCleanup(ai.gemini_keys.clear_model_overload)
        self.profile_sequence = 0
        self.profile = self._profile()
        self.raw = image_bytes() + b"\0" * (1024 * 1024)
        self.payload = receipt_payload(self.raw)
        self.owner = IgClient.objects.create(igsid="receipt-inline-fixture")
        self.message = InstagramBotMessage.objects.create(client=self.owner, sender_id=self.owner.igsid,
            role="user", source="webhook", media_capture_eligible=True, private_media_state="active",
            private_media_use_token="lease", private_media_use_until=timezone.now() + timedelta(minutes=3))
        self.capability = admission(self.raw, message_id=self.message.pk)
        source = self.capability.sources[0]
        self.message.attachment_media = [{"source_part_id": source.source_part_id, "content_hash": source.content_hash,
            "storage_name": source.storage_name, "status": "owned", "private_storage": True}]
        self.message.save(update_fields=["attachment_media"])

    def generate(self, *, payload=None, capability=True, guard=lambda: True):
        kwargs = {"role": "management", "reasoning_task": "media_analysis", "pre_dispatch_guard": guard}
        if capability:
            kwargs["inline_admission_profile"] = self.capability
        return ai.gemini_generate_text(payload or self.payload, **kwargs)

    def assert_candidate_matrix(self, graph, models):
        # The canonical graph also records unavailable candidates from the
        # other configured projects. The mocked execution prefix stays exact;
        # the full four-project matrix remains auditable and reserve-safe.
        plan = graph.candidate_plan
        self.assertEqual([row["model"] for row in plan[:len(models)]], models)
        self.assertEqual(len(plan), 4 * len(models))
        self.assertEqual({(row["project_identity"], row["model"]) for row in plan},
            {(f"gemini-project-{index}", model) for index in (3, 4, 5, 6) for model in models})

    def test_receipt_facade_uses_final_body_and_settles_actual_usage_once(self):
        with self._facade() as (post, cancel):
            self.generate()
        post.assert_called_once()
        cancel.assert_not_called()
        graph = GeminiRequest.objects.get()
        row = graph.attempts.get(provider_started_at__isnull=False)
        self.assertEqual(row.shadow_decision, "allow")
        self.assertGreater(row.estimated_prompt_tokens, runtime.RECEIPT_IMAGE_TOKEN_RESERVE)
        self.assertLess(row.estimated_prompt_tokens, 10000)
        self.assertEqual(row.prompt_tokens, 11)
        self.assertIsNotNone(row.settled_at)
        self.assertIsNotNone(row.permit_released_at)
        # A dispatched reservation is spent and settled, never refunded.
        self.assertIsNone(row.reservation_released_at)
        state = GeminiQuotaState.objects.get()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count), (1, 0))
        transport = post.call_args.kwargs["data"]
        self.assertEqual(json.loads(transport)["contents"], self.payload["contents"])
        self.assertNotIn(b"use_token", transport)
        self.assertNotIn("lease", json.dumps(graph.candidate_plan))
        self.assertNotIn("private/", json.dumps(graph.policy_manifest))

    def test_actual_prompt_usage_replaces_image_reservation_in_the_tpm_window(self):
        self.profile = self._profile(input_tpm_limit=10000)
        with self._facade() as (post, _cancel):
            self.generate()
            self.generate()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False, prompt_tokens=11).count(), 2)
        state = GeminiQuotaState.objects.get()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count), (2, 0))

    def test_generic_inline_consumer_remains_unknown_and_zero_http(self):
        with self._facade() as (post, cancel):
            with self.assertRaises(ai.CallAIAnalysisError):
                self.generate(capability=False)
        post.assert_not_called()
        cancel.assert_called_once()
        self.assertEqual(GeminiRequestAttempt.objects.get(candidate_index=1).shadow_deny_reason, "estimator_uncalibrated")
        self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).exists())

    def test_supported_images_respect_tpm_and_do_not_add_spend(self):
        self.profile = self._profile(input_tpm_limit=runtime.RECEIPT_IMAGE_TOKEN_RESERVE)
        state = self._state()
        with self._facade() as (post, cancel):
            with self.assertRaises(ai.CallAIAnalysisError):
                self.generate()
        post.assert_not_called()
        cancel.assert_called_once()
        self.assertEqual(GeminiRequestAttempt.objects.get(candidate_index=1).shadow_deny_reason, "tpm_exhausted")
        state.refresh_from_db()
        self.assertEqual((state.rpd_dispatched, state.in_flight_count), (0, 0))

    def test_project_exhaustion_rotates_receipt_to_an_independent_project(self):
        self._state(rpd_dispatched=self.profile.rpd_limit)
        with self._facade(aliases=("GEMINI_API3", "GEMINI_API4")) as (post, _cancel):
            result = self.generate()
        post.assert_called_once()
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], nonlive.KEYS["GEMINI_API4"])
        self.assertEqual(GeminiQuotaState.objects.get(project_identity="gemini-project-3").rpd_dispatched, self.profile.rpd_limit)
        self.assertEqual(GeminiQuotaState.objects.get(project_identity="gemini-project-4").rpd_dispatched, 1)

    def test_model_project_exhaustion_reaches_each_configured_fallback_family(self):
        primary = "gemini-3.8-flash"
        for fallback in ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"):
            with self.subTest(fallback=fallback):
                GeminiRequest._base_manager.all().update(winner_attempt_id=None)
                GeminiRequestAttempt.objects.all().delete()
                GeminiRequest.objects.all().delete()
                GeminiQuotaState.objects.all().delete()
                primary_profile = self._profile(model=primary)
                self._profile(model=fallback)
                GeminiQuotaState.objects.create(project_identity="gemini-project-3", model=primary,
                    quota_profile=primary_profile, pacific_day=timezone.now().astimezone(runtime.PT).date(),
                    rpd_dispatched=primary_profile.rpd_limit)
                candidates = [("GEMINI_API3", nonlive.KEYS["GEMINI_API3"], model) for model in (primary, fallback)]
                with self._facade() as (post, _cancel), \
                     patch.object(ai.gemini_keys, "task_model_chain", return_value=[primary, fallback]), \
                     patch.object(ai.gemini_keys, "iter_attempts", side_effect=lambda *_args, **_kwargs: iter(candidates)):
                    result = self.generate()
                post.assert_called_once()
                self.assertEqual(result["model"], fallback)
                self.assertIn(f"/models/{fallback}:generateContent", post.call_args.args[0])
                self.assertFalse(GeminiRequestAttempt.objects.filter(model=primary, provider_started_at__isnull=False).exists())
                self.assertEqual(GeminiQuotaState.objects.get(model=primary).rpd_dispatched, primary_profile.rpd_limit)
                self.assertEqual(GeminiQuotaState.objects.get(model=fallback).rpd_dispatched, 1)

    def test_receipt_prefers_configured_ordinary_flash_before_freezing_plan(self):
        preferred, strong = "gemini-3.5-flash", "gemini-3.8-flash"
        self._profile(model=preferred)
        self._profile(model=strong)
        seen_chains = []
        def candidates(_role, **kwargs):
            chain = kwargs["model_chain_override"]
            seen_chains.append(list(chain))
            return iter(("GEMINI_API3", nonlive.KEYS["GEMINI_API3"], model) for model in chain)
        with self._facade() as (post, _cancel), \
             patch.object(ai.gemini_keys, "task_model_chain", return_value=[strong, preferred, MODEL]), \
             patch.object(ai.gemini_keys, "iter_attempts", side_effect=candidates):
            result = self.generate()
        post.assert_called_once()
        self.assertEqual(result["model"], preferred)
        self.assertEqual(seen_chains, [[preferred, strong, MODEL]])
        graph = GeminiRequest.objects.get()
        self.assert_candidate_matrix(graph, [preferred, strong, MODEL])
        self.assertEqual(graph.attempts.get(provider_started_at__isnull=False).model, preferred)
        self.assertIn(f"/models/{preferred}:generateContent", post.call_args.args[0])

    def test_exhausted_ordinary_receipt_flash_falls_back_to_configured_strong_flash(self):
        preferred, strong = "gemini-3.5-flash", "gemini-3.8-flash"
        primary_profile = self._profile(model=preferred)
        self._profile(model=strong)
        GeminiQuotaState.objects.create(project_identity="gemini-project-3", model=preferred,
            quota_profile=primary_profile, pacific_day=timezone.now().astimezone(runtime.PT).date(),
            rpd_dispatched=primary_profile.rpd_limit)
        def candidates(_role, **kwargs):
            return iter(("GEMINI_API3", nonlive.KEYS["GEMINI_API3"], model) for model in kwargs["model_chain_override"])
        with self._facade() as (post, _cancel), \
             patch.object(ai.gemini_keys, "task_model_chain", return_value=[strong, preferred]), \
             patch.object(ai.gemini_keys, "iter_attempts", side_effect=candidates):
            result = self.generate()
        post.assert_called_once()
        self.assertEqual(result["model"], strong)
        graph = GeminiRequest.objects.get()
        self.assert_candidate_matrix(graph, [preferred, strong])
        denied = graph.attempts.get(model=preferred, candidate_index=1)
        self.assertEqual(denied.shadow_deny_reason, "rpd_exhausted")
        self.assertIsNone(denied.provider_started_at)
        self.assertEqual(graph.attempts.get(provider_started_at__isnull=False).model, strong)

    def test_missing_ordinary_receipt_model_preserves_configured_chain(self):
        strong = "gemini-3.8-flash"
        self._profile(model=strong)
        seen_chains = []
        def candidates(_role, **kwargs):
            chain = kwargs["model_chain_override"]
            seen_chains.append(list(chain))
            return iter(("GEMINI_API3", nonlive.KEYS["GEMINI_API3"], model) for model in chain)
        with self._facade() as (post, _cancel), \
             patch.object(ai.gemini_keys, "task_model_chain", return_value=[strong, MODEL]), \
             patch.object(ai.gemini_keys, "iter_attempts", side_effect=candidates):
            result = self.generate()
        self.assertEqual(seen_chains, [[strong, MODEL]])
        self.assertEqual(result["model"], strong)
        post.assert_called_once()

    def test_explicit_operator_model_choice_is_not_reordered_for_receipts(self):
        preferred, strong = "gemini-3.5-flash", "gemini-3.8-flash"
        self._profile(model=strong)
        seen_chains = []
        def candidates(_role, **kwargs):
            chain = kwargs["model_chain_override"]
            seen_chains.append(list(chain))
            return iter(("GEMINI_API3", nonlive.KEYS["GEMINI_API3"], model) for model in chain)
        with self._facade() as (post, _cancel), \
             patch.object(ai.gemini_keys, "task_model_chain", return_value=[strong, preferred]), \
             patch.object(ai.gemini_keys, "iter_attempts", side_effect=candidates):
            result = ai.gemini_generate_text(self.payload, role="management", reasoning_task="media_analysis",
                inline_admission_profile=self.capability, pre_dispatch_guard=lambda: True, model_override=strong)
        self.assertEqual(seen_chains, [[strong, preferred]])
        self.assertEqual(result["model"], strong)
        post.assert_called_once()

    def test_receipt_fast_failures_stop_after_two_http_boundaries_without_third_spend(self):
        failure = nonlive.fixtures._Response(code=503, payload={"error": {"status": "UNAVAILABLE", "message": "synthetic"}})
        with self._facade(aliases=("GEMINI_API3", "GEMINI_API4", "GEMINI_API5", "GEMINI_API6"), response=failure) as (post, _cancel):
            with self.assertRaises(ai.CallAIAnalysisError) as caught:
                self.generate()
        self.assertEqual(caught.exception.failure_kind, "provider_dispatch_budget")
        self.assertEqual(post.call_count, 2)
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.attempts.filter(provider_started_at__isnull=False).count(), 2)
        self.assertEqual(graph.terminal_reason, "provider_dispatch_budget")
        self.assertEqual(sum(GeminiQuotaState.objects.values_list("rpd_dispatched", flat=True)), 2)
        self.assertFalse(GeminiQuotaState.objects.filter(project_identity__in=("gemini-project-5", "gemini-project-6")).exists())

    def test_quota_denials_leave_both_receipt_http_slots_for_eligible_model_fallback(self):
        preferred, strong = "gemini-3.5-flash", "gemini-3.8-flash"
        primary_profile = self._profile(model=preferred)
        self._profile(model=strong)
        for index in (3, 4):
            GeminiQuotaState.objects.create(project_identity=f"gemini-project-{index}", model=preferred,
                quota_profile=primary_profile, pacific_day=timezone.now().astimezone(runtime.PT).date(),
                rpd_dispatched=primary_profile.rpd_limit)
        def candidates(_role, **kwargs):
            return iter((alias, nonlive.KEYS[alias], model) for model in kwargs["model_chain_override"]
                        for alias in ("GEMINI_API3", "GEMINI_API4"))
        failure = nonlive.fixtures._Response(code=503, payload={"error": {"status": "UNAVAILABLE", "message": "synthetic"}})
        with self._facade() as (post, _cancel), \
             patch.object(ai.gemini_keys, "task_model_chain", return_value=[strong, preferred]), \
             patch.object(ai.gemini_keys, "iter_attempts", side_effect=candidates):
            post.side_effect = [failure, nonlive.fixtures._Response()]
            result = self.generate()
        self.assertEqual(result["model"], strong)
        self.assertEqual(post.call_count, 2)
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.attempts.filter(model=preferred, shadow_deny_reason="rpd_exhausted").count(), 2)
        self.assertFalse(graph.attempts.filter(model=preferred, provider_started_at__isnull=False).exists())
        self.assertEqual(graph.attempts.filter(model=strong, provider_started_at__isnull=False).count(), 2)
        self.assertEqual(GeminiQuotaState.objects.get(project_identity="gemini-project-4", model=strong).rpd_dispatched, 1)

    def test_same_project_aliases_do_not_multiply_receipt_quota(self):
        self._state(rpd_dispatched=self.profile.rpd_limit)
        groups = {**nonlive.fixtures.SHADOW["GEMINI_KEY_PROJECT_GROUPS"], "GEMINI_API4": "gemini-project-3"}
        with override_settings(GEMINI_KEY_PROJECT_GROUPS=groups), \
             self._facade(aliases=("GEMINI_API3", "GEMINI_API4")) as (post, _cancel):
            with self.assertRaises(ai.CallAIAnalysisError):
                self.generate()
        post.assert_not_called()
        self.assertEqual(GeminiQuotaState.objects.count(), 1)
        self.assertEqual(GeminiQuotaState.objects.get().rpd_dispatched, self.profile.rpd_limit)

    def test_source_reset_or_lost_blob_lease_is_zero_http(self):
        self.message.private_media_use_token = "new-owner-lease"
        self.message.save(update_fields=["private_media_use_token"])
        with self._facade() as (post, _cancel):
            with self.assertRaises(ai.CallAIAnalysisError):
                self.generate()
        post.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).exists())

    def test_fresh_caller_guard_rejection_is_zero_http(self):
        with self._facade() as (post, _cancel):
            with self.assertRaises(ai.CallAIAnalysisError):
                self.generate(guard=lambda: "receipt_reset_changed")
        post.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.filter(provider_started_at__isnull=False).exists())

    def test_receipt_capability_cannot_authorize_another_role_or_task(self):
        for role, task in (("chat", "customer_chat"), ("management", "memory_summary"), ("checker", "media_analysis")):
            with self.assertRaises(ValueError), self._facade() as (post, _cancel):
                ai.gemini_generate_text(self.payload, role=role, reasoning_task=task,
                    inline_admission_profile=self.capability, pre_dispatch_guard=lambda: True)
            post.assert_not_called()
