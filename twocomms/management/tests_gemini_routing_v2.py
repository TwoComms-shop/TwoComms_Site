import json
import inspect
import hashlib
import os
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import (
    AdminAuditLog,
    GeminiRequestAttempt,
    GeminiKeyState,
    GeminiModelQuotaUsage,
    GeminiModelState,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import call_ai_analysis, gemini_keys
from management.services.gemini_routing import (
    ANALYSIS_CHAIN,
    COMPLEX_CHAIN,
    ORDINARY_CHAIN,
    RoutingMode,
    TaskClass,
    TurnFacts,
    analysis_escalation_chain,
    classify_live_turn,
    persist_decision,
)


def _candidate_snapshot(candidates):
    return {
        "version": "test-candidates-v2",
        "digest": hashlib.sha256(json.dumps(
            candidates,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "complete": True,
        "overflow": False,
        "candidates": candidates,
    }


class RoutingDecisionContractTests(SimpleTestCase):
    def test_ordinary_complex_and_analysis_chains_are_explicit(self):
        self.assertEqual(
            ORDINARY_CHAIN,
            (
                "gemini-3.5-flash-lite",
                "gemini-3.5-flash",
                "gemini-3.6-flash",
                "gemini-3.7-flash",
            ),
        )
        self.assertEqual(
            COMPLEX_CHAIN,
            (
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3.5-flash-lite",
            ),
        )
        self.assertEqual(
            ANALYSIS_CHAIN,
            (
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3.5-flash-lite",
            ),
        )

    def test_plain_text_does_not_promote_itself_without_structured_evidence(self):
        decision = classify_live_turn(TurnFacts())
        self.assertEqual(decision.task_class, TaskClass.ORDINARY_LIVE)
        self.assertEqual(decision.model_chain, ORDINARY_CHAIN)

    def test_media_and_ambiguous_catalog_are_complex(self):
        media = classify_live_turn(TurnFacts(has_image=True))
        ambiguous = classify_live_turn(TurnFacts(unresolved_catalog_candidates=3))
        self.assertEqual(media.task_class, TaskClass.COMPLEX_LIVE)
        self.assertEqual(media.reasoning_task, "media_analysis")
        self.assertEqual(ambiguous.task_class, TaskClass.COMPLEX_LIVE)
        self.assertIn("ambiguous_catalog", ambiguous.reason_codes)

    def test_objection_resolution_uses_complex_chain(self):
        decision = classify_live_turn(TurnFacts(objection_present=True))
        self.assertEqual(decision.task_class, TaskClass.COMPLEX_LIVE)
        self.assertIn("objection_resolution", decision.reason_codes)
        self.assertEqual(decision.reasoning_task, "product_decision")

    def test_deterministic_action_never_has_a_model_chain(self):
        decision = classify_live_turn(
            TurnFacts(deterministic_action="provider_native_ugc")
        )
        self.assertEqual(decision.task_class, TaskClass.NO_MODEL)
        self.assertEqual(decision.model_chain, ())

    def test_analysis_escalation_is_one_separate_guarded_strong_pass(self):
        eligible = dict(
            schema_valid=True,
            low_confidence=True,
            high_value=True,
            conflict_or_missing_fact=True,
            already_escalated=False,
            capacity_available=True,
        )
        self.assertEqual(
            analysis_escalation_chain(**eligible),
            ("gemini-3.8-flash", "gemini-3.7-flash"),
        )
        for field in eligible:
            changed = dict(eligible)
            changed[field] = not changed[field]
            self.assertEqual(analysis_escalation_chain(**changed), (), field)


@override_settings(GEMINI_NONLIVE_ADMISSION_MODE="shadow")
class ActualInstagramGeminiRoutingTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()
        self.settings.gemini_routing_mode = self.settings.GeminiRoutingMode.ADAPTIVE
        self.settings.gemini_model = "gemini-3.7-flash"
        self.settings.save(
            update_fields=["gemini_routing_mode", "gemini_model"]
        )

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_actual_entrypoint_ignores_legacy_model_in_adaptive_mode(self, generate):
        from management.services.instagram_bot import gemini_generate

        generate.return_value = {
            "parsed": "Вітаю!",
            "model": "gemini-3.5-flash-lite",
            "meta": {},
        }
        reply = gemini_generate(
            self.settings,
            [{"role": "user", "text": "Яка ціна?"}],
        )

        self.assertEqual(reply, "Вітаю!")
        self.assertEqual(
            tuple(generate.call_args.kwargs["model_chain_override"]),
            ORDINARY_CHAIN,
        )
        self.assertIsNone(generate.call_args.kwargs.get("model_override"))
        self.assertEqual(generate.call_args.kwargs["reasoning_task"], "customer_chat")

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_actual_entrypoint_routes_an_image_to_complex_chain(self, generate):
        from management.services.instagram_bot import gemini_generate
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Routing images", slug="routing-images")
        product = Product.objects.create(
            title="Validated image product",
            slug="validated-image-product",
            category=category,
            price=900,
            status=ProductStatus.PUBLISHED,
        )
        generate.return_value = {
            "parsed": {
                "reply_text": "Бачу принт.",
                "controls": [],
                "turn_intelligence": {
                    "catalog_candidates": [{
                        "product_id": product.pk,
                        "confidence": 0.96,
                        "evidence": "visible matching print",
                    }],
                    "transcript": "",
                    "intent": "product_match",
                    "confidence": 0.96,
                },
            },
            "model": "gemini-3.7-flash",
            "meta": {},
        }
        context = {}
        gemini_generate(
            self.settings,
            [{"role": "user", "text": "Що це?"}],
            images=[("image/jpeg", b"image")],
            failure_context=context,
        )

        generate.assert_called_once()
        self.assertEqual(
            tuple(generate.call_args.kwargs["model_chain_override"]),
            COMPLEX_CHAIN,
        )
        self.assertEqual(generate.call_args.kwargs["reasoning_task"], "media_analysis")
        self.assertEqual(
            context["turn_intelligence"]["auto_product_id"],
            product.pk,
        )

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_owned_audio_uses_one_complex_pass_and_returns_transcript_artifact(self, generate):
        from management.services.instagram_bot import gemini_generate

        generate.return_value = {
            "parsed": {
                "reply_text": "Так, допоможу з вибором.",
                "controls": [],
                "turn_intelligence": {
                    "catalog_candidates": [],
                    "transcript": "Хочу чорне худі oversize",
                    "intent": "product_selection",
                    "confidence": 0.94,
                },
            },
            "model": "gemini-3.7-flash",
            "meta": {},
        }
        context = {}

        gemini_generate(
            self.settings,
            [{"role": "user", "text": "(голосове)"}],
            images=[("audio/mp4", b"owned-audio")],
            failure_context=context,
        )

        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["reasoning_task"], "media_analysis")
        self.assertEqual(
            context["turn_intelligence"]["transcript"],
            "Хочу чорне худі oversize",
        )
        self.assertEqual(
            context["turn_intelligence"]["audio_status"],
            "transcribed",
        )
        self.assertEqual(context["turn_intelligence"]["media_count"], 1)
        self.assertEqual(
            context["turn_intelligence"]["media_content_hashes"],
            [hashlib.sha256(b"owned-audio").hexdigest()],
        )
        payload = generate.call_args.args[0]
        inline = payload["contents"][-1]["parts"][-1]["inline_data"]
        self.assertEqual(inline["mime_type"], "audio/m4a")

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_audio_without_transcript_or_typed_unintelligible_fails_safe(self, generate):
        from management.services.instagram_bot import gemini_generate

        generate.return_value = {
            "parsed": {
                "reply_text": "Зрозуміла.",
                "controls": [],
                "turn_intelligence": {
                    "catalog_candidates": [],
                    "transcript": "",
                    "intent": "product_selection",
                    "confidence": 0.8,
                },
            },
            "model": "gemini-3.7-flash",
            "meta": {},
        }
        context = {}

        reply = gemini_generate(
            self.settings,
            [{"role": "user", "text": "(голосове)"}],
            images=[("audio/ogg", b"unclear")],
            failure_context=context,
        )

        self.assertIsNone(reply)
        self.assertEqual(context["kind"], "invalid_response")

    @patch(
        "management.services.instagram_bot.INLINE_MEDIA_RAW_BUDGET",
        5,
    )
    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_actual_entrypoint_applies_combined_media_budget(self, generate):
        from management.services.instagram_bot import gemini_generate

        generate.return_value = {
            "parsed": {
                "reply_text": "Бачу вкладення.",
                "controls": [],
                "turn_intelligence": {
                    "catalog_candidates": [],
                    "transcript": "",
                    "intent": "media_review",
                    "confidence": 0.8,
                },
            },
            "model": "gemini-3.7-flash",
            "meta": {},
        }
        context = {}

        reply = gemini_generate(
            self.settings,
            [{"role": "user", "text": "Два вкладення"}],
            images=[("image/jpeg", b"1234"), ("image/png", b"5678")],
            failure_context=context,
            turn_candidate_set=_candidate_snapshot([]),
        )

        self.assertEqual(reply.reply_text, "Бачу вкладення.")
        generate.assert_called_once()
        parts = generate.call_args.args[0]["contents"][-1]["parts"]
        self.assertEqual(len([part for part in parts if "inline_data" in part]), 1)
        self.assertEqual(context["inline_media_omitted"], 1)

    @patch("management.services.instagram_bot.INLINE_REQUEST_MAX_BYTES", 3500)
    @patch(
        "management.services.ig_response_control.structured_response_schema",
        return_value={"type": "object"},
    )
    @patch(
        "management.services.instagram_bot.assemble_system_instruction",
        return_value="rules",
    )
    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_final_serialized_request_trims_last_media_below_twenty_mb_boundary(
        self,
        generate,
        _assemble,
        _schema,
    ):
        from management.services.instagram_bot import gemini_generate

        generate.return_value = {
            "parsed": {
                "reply_text": "Бачу перше вкладення.",
                "controls": [],
                "turn_intelligence": {
                    "catalog_candidates": [],
                    "transcript": "",
                    "intent": "media_review",
                    "confidence": 0.8,
                },
            },
            "model": "gemini-3.7-flash",
            "meta": {},
        }
        context = {}

        reply = gemini_generate(
            self.settings,
            [{"role": "user", "text": "Два фото"}],
            images=[
                ("image/jpeg", b"a" * 1500),
                ("image/png", b"b" * 1500),
            ],
            failure_context=context,
            turn_candidate_set=_candidate_snapshot([]),
        )

        self.assertTrue(reply)
        payload = generate.call_args.args[0]
        inline = [
            part
            for part in payload["contents"][-1]["parts"]
            if "inline_data" in part
        ]
        self.assertEqual(len(inline), 1)
        self.assertLessEqual(context["serialized_request_bytes"], 3500)
        self.assertEqual(context["inline_media_omitted"], 1)

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_actual_entrypoint_fails_safe_when_all_media_is_rejected(self, generate):
        from management.services.instagram_bot import gemini_generate

        context = {}
        reply = gemini_generate(
            self.settings,
            [{"role": "user", "text": "Файл"}],
            images=[("video/mp4", b"not-inline-audio")],
            failure_context=context,
        )

        self.assertIsNone(reply)
        self.assertEqual(context["kind"], "invalid_media")
        self.assertEqual(context["inline_media_omitted"], 1)
        generate.assert_not_called()

    def test_turn_intelligence_controls_pin_or_clarification_deterministically(self):
        from management.services.instagram_bot import _apply_turn_intelligence_resolution

        auto_reply, auto_control = _apply_turn_intelligence_resolution(
            "Ось ця модель.",
            {},
            {
                "catalog_candidates": [{"product_id": 11, "title": "One"}],
                "catalog_resolution": "auto_select",
                "auto_product_id": 11,
            },
            None,
        )
        self.assertEqual(auto_reply, "Ось ця модель.")
        self.assertEqual(auto_control["product"], "11")

        clarification, control = _apply_turn_intelligence_resolution(
            "Бачу кілька схожих варіантів.",
            {"product": "11"},
            {
                "catalog_candidates": [
                    {"product_id": 11, "title": "One"},
                    {"product_id": 12, "title": "Two"},
                ],
                "catalog_resolution": "clarify",
                "auto_product_id": None,
            },
            None,
        )
        self.assertNotIn("product", control)
        self.assertIn("One, Two", clarification)
        self.assertIn("?", clarification)

        no_match_reply, no_match_control = _apply_turn_intelligence_resolution(
            "Не впевнений.",
            {"product": "99"},
            {
                "catalog_candidates": [],
                "catalog_resolution": "no_match",
                "auto_product_id": None,
            },
            None,
        )
        self.assertEqual(no_match_reply, "Не впевнений.")
        self.assertNotIn("product", no_match_control)

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_real_published_product_outside_candidate_set_is_rejected(
        self,
        generate,
    ):
        from management.services.instagram_bot import gemini_generate
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Candidate gate", slug="candidate-gate")
        allowed = Product.objects.create(
            title="Allowed",
            slug="candidate-allowed",
            category=category,
            price=800,
            status=ProductStatus.PUBLISHED,
        )
        outsider = Product.objects.create(
            title="Hallucinated outsider",
            slug="candidate-outsider",
            category=category,
            price=900,
            status=ProductStatus.PUBLISHED,
        )
        generate.return_value = {
            "parsed": {
                "reply_text": "Схоже на товар.",
                "controls": [],
                "turn_intelligence": {
                    "catalog_candidates": [{
                        "product_id": outsider.pk,
                        "confidence": 0.99,
                        "evidence": "hallucinated existing id",
                    }],
                    "transcript": "",
                    "intent": "product_match",
                    "confidence": 0.99,
                },
            },
            "model": "gemini-3.7-flash",
            "meta": {},
        }
        context = {}

        reply = gemini_generate(
            self.settings,
            [{"role": "user", "text": "Що це?"}],
            images=[("image/jpeg", b"image")],
            failure_context=context,
            turn_candidate_set=_candidate_snapshot([{
                    "product_id": allowed.pk,
                    "title": allowed.title,
                    "fingerprint": "allowed visual",
                }]),
        )

        self.assertTrue(reply)
        artifact = context["turn_intelligence"]
        self.assertEqual(artifact["catalog_candidates"], [])
        self.assertIsNone(artifact["auto_product_id"])
        self.assertEqual(artifact["catalog_resolution"], "no_match")
        self.assertEqual(artifact["candidate_set_size"], 1)
        self.assertTrue(artifact["candidate_set_digest"])

    def test_ingress_has_no_second_bot_vision_provider_pass(self):
        from management.services.instagram_bot import (
            _process_one_inside_reply_boundary,
        )

        source = inspect.getsource(_process_one_inside_reply_boundary)
        self.assertNotIn("bot_vision.match", source)
        self.assertEqual(source.count("reply = gemini_generate("), 1)

    @patch("management.services.instagram_bot._pin_control_product", return_value=True)
    @patch("management.services.bot_vision.match")
    @patch("management.services.instagram_bot._wait_for_typing_window", return_value="allowed")
    @patch("management.services.instagram_bot.send_sender_action")
    @patch("management.services.instagram_bot.send_text")
    @patch("management.services.instagram_bot._collect_media_images")
    @patch("management.services.instagram_bot._recover_current_message_media")
    @patch("management.services.instagram_bot._capture_message_media")
    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_image_ingress_calls_provider_once_and_persists_validated_artifact(
        self,
        generate,
        _capture,
        recover,
        collect,
        send_text,
        _sender_action,
        _typing_wait,
        legacy_match,
        pin_product,
    ):
        from management.models import IgClient
        from management.services import instagram_bot
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Ingress images", slug="ingress-images")
        product = Product.objects.create(
            title="One pass product",
            slug="one-pass-product",
            category=category,
            price=990,
            status=ProductStatus.PUBLISHED,
        )
        client = IgClient.get_or_create_for_sender("one-pass-image-client")
        client.profile_fetched_at = timezone.now()
        client.save(update_fields=["profile_fetched_at", "updated_at"])
        source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Що це за футболка?",
            mid="one-pass-image-mid",
            source="webhook",
            status=InstagramBotMessage.Status.PENDING,
            media_capture_eligible=True,
            attachment_media=[{
                "url": "https://lookaside.invalid/one-pass.jpg",
                "media_type": "image",
                "mime": "image/jpeg",
                "provenance": "live_webhook",
                "status": "owned",
                "storage_name": "ig/one-pass.jpg",
            }],
        )
        media = [{
            **source.attachment_media[0],
            "role": "product",
            "catalog_match_allowed": True,
        }]
        recover.return_value = media
        collect.return_value = [("image/jpeg", b"owned-image")]
        generate.return_value = {
            "parsed": {
                "reply_text": "Це модель з нашого каталогу.",
                "controls": [],
                "turn_intelligence": {
                    "catalog_candidates": [{
                        "product_id": product.pk,
                        "confidence": 0.97,
                        "evidence": "print and cut match",
                    }],
                    "transcript": "",
                    "intent": "product_match",
                    "confidence": 0.97,
                },
            },
            "model": "gemini-3.7-flash",
            "meta": {},
        }
        send_text.return_value = instagram_bot.ProviderDeliveryReceipt(
            True, "", "", "one-pass-image-reply"
        )
        self.settings.is_enabled = True
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["is_enabled", "ai_enabled"])

        instagram_bot.process_pending(self.settings, max_items=1)

        generate.assert_called_once()
        legacy_match.assert_not_called()
        pin_product.assert_called_once()
        self.assertEqual(pin_product.call_args.args[1], product.pk)
        source.refresh_from_db()
        self.assertEqual(
            source.turn_intelligence_artifact["auto_product_id"],
            product.pk,
        )
        self.assertEqual(source.turn_intelligence_artifact["source_message_id"], source.pk)
        self.assertEqual(source.turn_intelligence_artifact["media_count"], 1)
        self.assertEqual(len(source.turn_intelligence_artifact["source_message_revision"]), 64)
        self.assertEqual(len(source.turn_intelligence_artifact["media_digest"]), 64)
        self.assertEqual(source.send_state, "sent")

    def test_routing_decision_is_durable_on_the_source_message(self):
        row = InstagramBotMessage.objects.create(
            sender_id="route-durable",
            role=InstagramBotMessage.Role.USER,
            text="hello",
        )
        decision = classify_live_turn(TurnFacts(has_image=True))
        persist_decision(row, decision)
        row.refresh_from_db()
        self.assertEqual(row.gemini_task_class, TaskClass.COMPLEX_LIVE)
        self.assertEqual(tuple(row.gemini_routing_model_chain), COMPLEX_CHAIN)
        self.assertIn("image_reasoning", row.gemini_routing_reason_codes)
        self.assertEqual(row.gemini_routing_deadline_ms, 45_000)
        self.assertEqual(row.gemini_routing_lane, "live")
        self.assertTrue(row.gemini_routing_requires_media)
        self.assertEqual(row.gemini_routing_commercial_risk, "low")
        self.assertEqual(row.gemini_routing_mode, "adaptive")

        persist_decision(row, classify_live_turn(TurnFacts()))
        row.refresh_from_db()
        self.assertEqual(row.gemini_task_class, TaskClass.COMPLEX_LIVE)
        self.assertEqual(tuple(row.gemini_routing_model_chain), COMPLEX_CHAIN)

    def test_typed_commerce_parser_wires_fit_custom_comparison_and_safe_reset(self):
        from management.services.ig_commerce_turns import parse_turn
        from management.services.instagram_bot import live_routing_decision

        generic = parse_turn("У меня другой вопрос по доставке")
        self.assertFalse(generic.reset_requested)
        self.assertEqual(
            live_routing_decision(
                self.settings, commerce_request=generic
            ).task_class,
            TaskClass.ORDINARY_LIVE,
        )

        cases = (
            ("Какой размер мне подойдет?", "personalized_fit"),
            ("Хочу свой принт на футболке", "custom_print"),
            ("Сравни эти две модели, что лучше?", "comparison"),
            ("Хочу другую футболку", "branch_switch"),
            ("Хочу іншу модель худі", "branch_switch"),
            ("Please switch to a different hoodie", "branch_switch"),
            ("I want another one", "branch_switch"),
        )
        for text, reason in cases:
            with self.subTest(text=text):
                request = parse_turn(text)
                decision = live_routing_decision(
                    self.settings,
                    commerce_request=request,
                )
                self.assertEqual(decision.task_class, TaskClass.COMPLEX_LIVE)
                self.assertIn(reason, decision.reason_codes)

    def test_unresolved_real_referral_is_complex(self):
        from management.models import BotAdCampaign, IgClient
        from management.services.ig_commerce_turns import parse_turn
        from management.services.instagram_bot import live_routing_decision
        from storefront.models import Category, Product, ProductStatus

        client = IgClient.get_or_create_for_sender("routing-referral-client")
        client.ad_id = "ad-without-product-map"
        client.save(update_fields=["ad_id", "updated_at"])
        decision = live_routing_decision(
            self.settings,
            commerce_request=parse_turn("Привіт"),
            client=client,
        )

        self.assertEqual(decision.task_class, TaskClass.COMPLEX_LIVE)
        self.assertIn("ambiguous_referral", decision.reason_codes)

        category = Category.objects.create(name="Mapped ads", slug="mapped-ads")
        product = Product.objects.create(
            title="Mapped ad product",
            slug="mapped-ad-product",
            category=category,
            price=800,
            status=ProductStatus.PUBLISHED,
        )
        BotAdCampaign.objects.create(
            ad_id="mapped-ad",
            title="Mapped",
            product=product,
        )
        mapped = IgClient.get_or_create_for_sender("routing-mapped-referral")
        mapped.ad_id = "mapped-ad"
        mapped.save(update_fields=["ad_id", "updated_at"])

        mapped_decision = live_routing_decision(
            self.settings,
            commerce_request=parse_turn("Привіт"),
            client=mapped,
        )

        self.assertEqual(mapped_decision.task_class, TaskClass.ORDINARY_LIVE)
        self.assertNotIn("ambiguous_referral", mapped_decision.reason_codes)

    def test_duplicate_ad_mapping_is_typed_ambiguous_in_one_read(self):
        from management.models import BotAdCampaign, IgClient
        from management.services.ig_ad_referral import resolve_ad_referral
        from management.services.ig_commerce_turns import parse_turn
        from management.services.instagram_bot import live_routing_decision

        client = IgClient.get_or_create_for_sender("routing-duplicate-referral")
        client.ad_id = "duplicate-ad"
        client.save(update_fields=["ad_id", "updated_at"])
        BotAdCampaign.objects.create(ad_id="duplicate-ad", theme="one")
        BotAdCampaign.objects.create(ad_id="duplicate-ad", theme="two")

        with CaptureQueriesContext(connection) as queries:
            resolution = resolve_ad_referral(client)

        self.assertEqual(resolution.status, "ambiguous")
        self.assertIn("duplicate_active_mapping", resolution.reason_codes)
        self.assertLessEqual(len(queries), 1)
        decision = live_routing_decision(
            self.settings,
            commerce_request=parse_turn("Привіт"),
            client=client,
            ad_resolution=resolution,
        )
        self.assertEqual(decision.task_class, TaskClass.COMPLEX_LIVE)

    def test_resolved_ad_object_is_reused_by_routing_and_context(self):
        from management.models import BotAdCampaign, IgClient
        from management.services import bot_memory
        from management.services.ig_ad_referral import AdReferralResolution
        from management.services.ig_commerce_turns import parse_turn
        from management.services.instagram_bot import live_routing_decision

        client = IgClient.get_or_create_for_sender("routing-reused-referral")
        client.ad_id = "reused-ad"
        client.save(update_fields=["ad_id", "updated_at"])
        campaign = BotAdCampaign.objects.create(
            ad_id="reused-ad",
            title="Reused",
            theme="hoodies",
        )
        resolution = AdReferralResolution(
            "resolved",
            ("mapping_resolved",),
            campaign,
        )

        with patch(
            "management.services.ig_ad_referral.resolve_ad_referral",
            side_effect=AssertionError("resolver called twice"),
        ):
            decision = live_routing_decision(
                self.settings,
                commerce_request=parse_turn("Привіт"),
                client=client,
                ad_resolution=resolution,
            )
            note = bot_memory.client_context_note(
                client,
                ad_resolution=resolution,
            )

        self.assertEqual(decision.task_class, TaskClass.ORDINARY_LIVE)
        self.assertIn("hoodies", note)


@override_settings(GEMINI_NONLIVE_ADMISSION_MODE="shadow")
class TurnCandidateSnapshotTests(TestCase):
    def _products(self, count):
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(
            name=f"Candidate snapshot {count}",
            slug=f"candidate-snapshot-{count}",
        )
        Product.objects.bulk_create([
            Product(
                title=f"Candidate {index}",
                slug=f"candidate-snapshot-{count}-{index}",
                category=category,
                price=800,
                status=ProductStatus.PUBLISHED,
            )
            for index in range(count)
        ])

    def test_complete_73_product_snapshot_uses_at_most_two_reads(self):
        from management.services.instagram_bot import _build_turn_candidate_set

        self._products(73)
        with CaptureQueriesContext(connection) as queries:
            snapshot = _build_turn_candidate_set()

        self.assertTrue(snapshot["complete"])
        self.assertFalse(snapshot["overflow"])
        self.assertEqual(len(snapshot["candidates"]), 73)
        self.assertEqual(snapshot["observed_count"], 73)
        self.assertLessEqual(len(queries), 2)

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_catalog_over_cap_fails_safe_before_provider(self, generate):
        from management.services.instagram_bot import gemini_generate

        self._products(201)
        settings_obj = InstagramBotSettings.load()
        context = {}

        reply = gemini_generate(
            settings_obj,
            [{"role": "user", "text": "Що на фото?"}],
            images=[("image/jpeg", b"image")],
            failure_context=context,
        )

        self.assertIsNone(reply)
        self.assertEqual(context["kind"], "catalog_candidate_overflow")
        generate.assert_not_called()

    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_candidate_digest_mismatch_fails_before_provider(self, generate):
        from management.services.instagram_bot import gemini_generate

        settings_obj = InstagramBotSettings.load()
        context = {}
        candidate_set = _candidate_snapshot([])
        candidate_set["digest"] = "0" * 64

        reply = gemini_generate(
            settings_obj,
            [{"role": "user", "text": "Що на фото?"}],
            images=[("image/jpeg", b"image")],
            failure_context=context,
            turn_candidate_set=candidate_set,
        )

        self.assertIsNone(reply)
        self.assertEqual(context["kind"], "invalid_candidate_set")
        generate.assert_not_called()

    @patch(
        "management.services.instagram_bot.assemble_system_instruction",
        return_value="rules",
    )
    @patch("management.services.call_ai_analysis.gemini_generate_text")
    def test_overall_pre_provider_path_is_bounded_to_six_reads(
        self,
        generate,
        _assemble,
    ):
        from management.services.instagram_bot import gemini_generate

        self._products(73)
        settings_obj = InstagramBotSettings.load()
        observed = {}

        def provider(*_args, **_kwargs):
            observed["queries"] = len(query_context)
            return {
                "parsed": {
                    "reply_text": "Бачу товар.",
                    "controls": [],
                    "turn_intelligence": {
                        "catalog_candidates": [],
                        "transcript": "",
                        "intent": "product_match",
                        "confidence": 0.8,
                    },
                },
                "model": "gemini-3.7-flash",
                "meta": {},
            }

        generate.side_effect = provider
        with CaptureQueriesContext(connection) as query_context:
            reply = gemini_generate(
                settings_obj,
                [{"role": "user", "text": "Що на фото?"}],
                images=[("image/jpeg", b"image")],
            )

        self.assertTrue(reply)
        self.assertLessEqual(observed["queries"], 6)


@override_settings(
    ROOT_URLCONF="twocomms.urls_management",
    GEMINI_NONLIVE_ADMISSION_MODE="shadow",
)
class PinnedRoutingPolicyTests(TestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.load()

    def test_active_pin_prepends_one_model_and_expired_pin_is_adaptive(self):
        self.settings.gemini_routing_mode = self.settings.GeminiRoutingMode.PINNED
        self.settings.pinned_chat_model = "gemini-3.6-flash"
        self.settings.pinned_until = timezone.now() + timedelta(minutes=10)
        decision = classify_live_turn(TurnFacts(), settings_obj=self.settings)
        self.assertEqual(decision.routing_mode, RoutingMode.PINNED)
        self.assertEqual(decision.model_chain[0], "gemini-3.6-flash")

        self.settings.pinned_until = timezone.now() - timedelta(seconds=1)
        expired = classify_live_turn(TurnFacts(), settings_obj=self.settings)
        self.assertEqual(expired.routing_mode, RoutingMode.ADAPTIVE)
        self.assertEqual(expired.model_chain, ORDINARY_CHAIN)

    def test_settings_api_audits_a_bounded_pin(self):
        user = get_user_model().objects.create_user(
            username="routing-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)
        response = self.client.post(
            "/bot/api/settings/",
            {
                "ai_enabled": "on",
                "gemini_model": "gemini-3.6-flash",
                "gemini_routing_mode": "pinned",
                "pinned_minutes": "15",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.gemini_routing_mode, "pinned")
        self.assertEqual(self.settings.pinned_chat_model, "gemini-3.6-flash")
        self.assertLessEqual(
            self.settings.pinned_until,
            timezone.now() + timedelta(minutes=15, seconds=2),
        )
        audit = AdminAuditLog.objects.get(
            action="ig_gemini.routing_policy_changed"
        )
        self.assertEqual(audit.after["mode"], "pinned")
        self.assertNotIn("key", json.dumps(audit.after).casefold())

    def test_audit_failure_rolls_back_the_pin(self):
        user = get_user_model().objects.create_user(
            username="routing-audit-failure-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)
        with patch.object(
            AdminAuditLog.objects,
            "create",
            side_effect=RuntimeError("audit unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.client.post(
                    "/bot/api/settings/",
                    {
                        "ai_enabled": "on",
                        "gemini_model": "gemini-3.6-flash",
                        "gemini_routing_mode": "pinned",
                        "pinned_minutes": "15",
                    },
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )

        self.settings.refresh_from_db()
        self.assertEqual(self.settings.gemini_routing_mode, "adaptive")
        self.assertEqual(self.settings.pinned_chat_model, "")
        self.assertIsNone(self.settings.pinned_until)

    def test_unrelated_stale_settings_save_cannot_erase_a_new_pin(self):
        user = get_user_model().objects.create_user(
            username="routing-stale-save-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)
        stale = InstagramBotSettings.objects.get(pk=self.settings.pk)
        pinned_until = timezone.now() + timedelta(minutes=20)
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(
            gemini_routing_mode="pinned",
            pinned_chat_model="gemini-3.6-flash",
            pinned_until=pinned_until,
        )

        with patch(
            "management.bot_views.InstagramBotSettings.load",
            return_value=stale,
        ):
            response = self.client.post(
                "/bot/api/settings/",
                {"ai_enabled": "on", "poll_interval_seconds": "7"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.gemini_routing_mode, "pinned")
        self.assertEqual(self.settings.pinned_chat_model, "gemini-3.6-flash")
        self.assertEqual(self.settings.pinned_until, pinned_until)

    def test_explicit_routing_update_detects_a_concurrent_pin(self):
        user = get_user_model().objects.create_user(
            username="routing-race-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)
        stale = InstagramBotSettings.objects.get(pk=self.settings.pk)
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(
            gemini_routing_mode="pinned",
            pinned_chat_model="gemini-3.6-flash",
            pinned_until=timezone.now() + timedelta(minutes=20),
        )

        with patch(
            "management.bot_views.InstagramBotSettings.load",
            return_value=stale,
        ):
            response = self.client.post(
                "/bot/api/settings/",
                {
                    "ai_enabled": "on",
                    "gemini_routing_mode": "adaptive",
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 409)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.gemini_routing_mode, "pinned")

    def test_stale_partial_save_preserves_unposted_general_fields(self):
        user = get_user_model().objects.create_user(
            username="settings-partial-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)
        stale = InstagramBotSettings.objects.get(pk=self.settings.pk)
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(
            knowledge_base="newer operator directive",
            trigger_text="newer trigger",
        )

        with patch(
            "management.bot_views.InstagramBotSettings.load",
            return_value=stale,
        ):
            response = self.client.post(
                "/bot/api/settings/",
                {"poll_interval_seconds": "9"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.poll_interval_seconds, 9)
        self.assertEqual(self.settings.knowledge_base, "newer operator directive")
        self.assertEqual(self.settings.trigger_text, "newer trigger")
        self.assertEqual(self.settings.settings_revision, 1)

    def test_general_revision_rejects_a_stale_admin_form(self):
        user = get_user_model().objects.create_user(
            username="settings-revision-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(user)
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(
            settings_revision=3,
            knowledge_base="newer revision",
        )

        response = self.client.post(
            "/bot/api/settings/",
            {
                "settings_revision": "0",
                "poll_interval_seconds": "11",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 409)
        self.settings.refresh_from_db()
        self.assertNotEqual(self.settings.poll_interval_seconds, 11)
        self.assertEqual(self.settings.knowledge_base, "newer revision")
        self.assertEqual(self.settings.settings_revision, 3)


@override_settings(GEMINI_NONLIVE_ADMISSION_MODE="shadow")
class SixProjectCandidatePlanTests(TestCase):
    def test_default_identities_are_six_distinct_non_secret_labels(self):
        groups = gemini_keys.key_project_groups()
        self.assertEqual(set(groups), set(gemini_keys.ALL_KEYS))
        self.assertEqual(len(set(groups.values())), 6)
        self.assertTrue(all("secret" not in value for value in groups.values()))

    def test_plan_contains_all_six_projects_for_every_model(self):
        env = {alias: f"test-secret-{index}" for index, alias in enumerate(gemini_keys.ALL_KEYS)}
        with patch.dict(os.environ, env, clear=False):
            plan = gemini_keys.live_chat_candidate_plan(
                model_chain_override=list(ORDINARY_CHAIN)
            )
        self.assertEqual(len(plan), 24)
        for model in ORDINARY_CHAIN:
            rows = [item for item in plan if item["model"] == model]
            self.assertEqual(len(rows), 6)
            self.assertEqual(len({item["project_identity"] for item in rows}), 6)

    def test_candidate_plan_is_three_bulk_reads_and_creates_no_zero_rows(self):
        env = {
            alias: f"bulk-read-key-{index}"
            for index, alias in enumerate(gemini_keys.ALL_KEYS)
        }
        with patch.dict(os.environ, env, clear=False), CaptureQueriesContext(
            connection
        ) as queries:
            plan = gemini_keys.live_chat_candidate_plan(
                model_chain_override=list(ORDINARY_CHAIN)
            )

        self.assertEqual(len(plan), 24)
        self.assertLessEqual(len(queries), 3, [query["sql"] for query in queries])
        self.assertEqual(GeminiModelQuotaUsage.objects.count(), 0)
        self.assertEqual(GeminiKeyState.objects.count(), 0)
        self.assertEqual(GeminiModelState.objects.count(), 0)

    def test_legacy_chat_hedge_is_disabled(self):
        self.assertFalse(call_ai_analysis.ENABLE_LEGACY_CHAT_HEDGE)

    def test_accounting_unknown_blocks_only_the_project_model_until_pt_reset(self):
        now = timezone.now().replace(microsecond=0)
        state = gemini_keys.mark_429(
            "GEMINI_API",
            "unknown",
            0,
            now=now,
            model="gemini-3.7-flash",
        )

        self.assertEqual(state.last_status, "429:accounting_unknown")
        until = state.model_cooldowns["gemini-3.7-flash"]
        self.assertEqual(
            datetime.fromisoformat(until),
            gemini_keys.next_midnight_pt(now),
        )
        self.assertIsNone(state.cooldown_until)

    def test_duplicate_env_credentials_are_one_candidate_per_model(self):
        env = {
            "GEMINI_API": "same-provider-secret",
            "GEMINI_API2": "same-provider-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            plan = gemini_keys.live_chat_candidate_plan(
                model_chain_override=["gemini-3.5-flash-lite"]
            )

        by_alias = {item["key_name"]: item for item in plan}
        self.assertEqual(by_alias["GEMINI_API"]["skip_reason"], "")
        self.assertEqual(
            by_alias["GEMINI_API2"]["skip_reason"],
            "duplicate_credential",
        )

    @patch("management.services.call_ai_analysis._gemini_call_once")
    def test_custom_key_equal_to_env_does_not_bypass_or_retry_alias(self, call_once):
        call_once.return_value = "ok", {}
        with patch.dict(
            os.environ,
            {"GEMINI_API": "same-custom-and-env"},
            clear=True,
        ):
            result = call_ai_analysis.gemini_generate_text(
                {"contents": []},
                role="chat",
                manual_key="same-custom-and-env",
            )

        self.assertEqual(result["parsed"], "ok")
        self.assertEqual(result["meta"]["key"], "GEMINI_API")
        call_once.assert_called_once()


class TypedQuotaErrorTests(SimpleTestCase):
    def _response(self, details):
        payload = {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "quota exceeded",
                "details": details,
            }
        }
        response = type("Response", (), {})()
        response.status_code = 429
        response.json = lambda: payload
        response.text = json.dumps(payload)
        return response

    @patch("management.services.call_ai_analysis.requests.post")
    def test_quota_failure_and_retry_info_become_typed_day_error(self, post):
        post.return_value = self._response([
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                }],
            },
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": "48.5s",
            },
        ])
        with self.assertRaises(call_ai_analysis._Gemini429) as raised:
            call_ai_analysis._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "redacted", parse=False
            )
        self.assertEqual(raised.exception.scope, "day")
        self.assertEqual(raised.exception.retry_after_seconds, 50)
        self.assertEqual(raised.exception.provider_reason, "RESOURCE_EXHAUSTED")

    @patch("management.services.call_ai_analysis.requests.post")
    def test_per_minute_token_quota_remains_minute_scoped(self, post):
        post.return_value = self._response([{
            "violations": [{"quotaId": "GenerateContentInputTokensPerMinutePerProjectPerModel"}],
            "retryDelay": "1.2s",
        }])
        with self.assertRaises(call_ai_analysis._Gemini429) as raised:
            call_ai_analysis._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "redacted", parse=False
            )
        self.assertEqual(raised.exception.scope, "minute")
        self.assertEqual(raised.exception.retry_after_seconds, 3)

    @patch("management.services.call_ai_analysis.requests.post")
    def test_billing_depletion_outranks_attached_day_quota_details(self, post):
        response = self._response([{
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{
                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            }],
        }])
        original_json = response.json

        def payload_with_billing_message():
            payload = original_json()
            payload["error"]["message"] = "Billing account prepayment credits are depleted"
            return payload

        response.json = payload_with_billing_message
        post.return_value = response

        with self.assertRaises(call_ai_analysis._Gemini429) as raised:
            call_ai_analysis._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "redacted", parse=False
            )

        self.assertEqual(raised.exception.scope, "topup")

    @patch("management.services.call_ai_analysis.requests.post")
    def test_detail_less_day_and_minute_messages_keep_safe_scope(self, post):
        cases = (
            ("GenerateRequestsPerDayPerProjectPerModel quota exceeded", "day"),
            ("GenerateRequestsPerMinutePerProjectPerModel quota exceeded", "minute"),
            ("Resource exhausted", "unknown"),
        )
        for message, expected in cases:
            payload = {
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": message,
                }
            }
            response = type("Response", (), {})()
            response.status_code = 429
            response.json = lambda payload=payload: payload
            response.text = json.dumps(payload)
            post.return_value = response
            with self.subTest(message=message):
                with self.assertRaises(call_ai_analysis._Gemini429) as raised:
                    call_ai_analysis._gemini_call_once(
                        "gemini-3.7-flash",
                        {"contents": []},
                        "redacted",
                        parse=False,
                    )
                self.assertEqual(raised.exception.scope, expected)

    @patch("management.services.call_ai_analysis.requests.post")
    def test_detail_less_retry_info_remains_accounting_unknown(self, post):
        post.return_value = self._response([{
            "@type": "type.googleapis.com/google.rpc.RetryInfo",
            "retryDelay": "48s",
        }])

        with self.assertRaises(call_ai_analysis._Gemini429) as raised:
            call_ai_analysis._gemini_call_once(
                "gemini-3.7-flash",
                {"contents": []},
                "redacted",
                parse=False,
            )

        self.assertEqual(raised.exception.scope, "unknown")
        self.assertEqual(raised.exception.retry_after_seconds, 50)


class FinalProviderPayloadBoundaryTests(SimpleTestCase):
    def _response(self):
        response = type("Response", (), {})()
        response.status_code = 200
        response.text = ""
        response.json = lambda: {
            "candidates": [{
                "content": {"parts": [{"text": "ok"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {},
        }
        return response

    def _normalized_payload(self):
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "hello"},
                    {"inline_data": {"mime_type": "image/jpeg", "data": "aaaa"}},
                    {"inline_data": {"mime_type": "image/png", "data": "bbbb"}},
                ],
            }],
            "generationConfig": {"temperature": 0.2},
        }
        return call_ai_analysis._payload_for_model(
            "gemini-3.7-flash",
            payload,
            reasoning_task="media_analysis",
        )

    @patch("management.services.call_ai_analysis.requests.post")
    def test_exact_final_serialized_boundary_dispatches_normalized_payload(self, post):
        normalized = self._normalized_payload()
        exact_size = len(json.dumps(normalized).encode("utf-8"))
        post.return_value = self._response()

        with patch(
            "management.services.call_ai_analysis.PROVIDER_REQUEST_MAX_BYTES",
            exact_size,
        ):
            parsed, usage = call_ai_analysis._gemini_call_once(
                "gemini-3.7-flash",
                normalized,
                "redacted",
                parse=False,
            )

        self.assertEqual(parsed, "ok")
        body = post.call_args.kwargs["data"]
        self.assertEqual(len(body), exact_size)
        sent = json.loads(body)
        self.assertEqual(
            sent["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "high",
        )
        self.assertEqual(usage["_request_inline_count"], 2)
        self.assertEqual(usage["_request_trimmed_inline"], 0)

    @patch("management.services.call_ai_analysis.requests.post")
    def test_one_byte_over_final_boundary_fails_without_silent_media_trim(self, post):
        normalized = self._normalized_payload()
        exact_size = len(json.dumps(normalized).encode("utf-8"))

        with patch(
            "management.services.call_ai_analysis.PROVIDER_REQUEST_MAX_BYTES",
            exact_size - 1,
        ):
            with self.assertRaises(call_ai_analysis._GeminiFatal):
                call_ai_analysis._gemini_call_once(
                    "gemini-3.7-flash",
                    normalized,
                    "redacted",
                    parse=False,
                )

        post.assert_not_called()
        self.assertEqual(
            sum(
                "inline_data" in part
                for part in normalized["contents"][0]["parts"]
            ),
            2,
        )


class OwnedAudioCaptureTests(SimpleTestCase):
    @patch("management.services.instagram_bot.urllib.request.urlopen")
    def test_audio_is_captured_with_a_bounded_read(self, urlopen):
        from management.services.instagram_bot import download_image

        class Response:
            headers = {"Content-Type": "audio/ogg"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit):
                self.limit = limit
                return b"voice-bytes"

        response = Response()
        urlopen.return_value = response

        captured = download_image("https://lookaside.invalid/voice")

        self.assertEqual(captured, ("audio/ogg", b"voice-bytes"))
        self.assertEqual(response.limit, 10 * 1024 * 1024 + 1)

    def test_single_ten_mib_audio_is_admitted_and_m4a_is_normalized(self):
        from management.services.instagram_bot import _bounded_inline_media

        raw = b"a" * (10 * 1024 * 1024)
        admitted, omitted = _bounded_inline_media([("audio/x-m4a", raw)])

        self.assertEqual(omitted, 0)
        self.assertEqual(admitted, [("audio/m4a", raw)])

    def test_combined_budget_omits_later_oversized_media(self):
        from management.services.instagram_bot import (
            INLINE_MEDIA_RAW_BUDGET,
            _bounded_inline_media,
        )

        audio = b"a" * (10 * 1024 * 1024)
        later_image = b"i" * (3 * 1024 * 1024)
        admitted, omitted = _bounded_inline_media([
            ("audio/ogg", audio),
            ("image/jpeg", later_image),
        ])

        self.assertEqual(admitted, [("audio/ogg", audio)])
        self.assertEqual(omitted, 1)
        self.assertLessEqual(sum(len(raw) for _mime, raw in admitted), INLINE_MEDIA_RAW_BUDGET)

    @patch("management.services.instagram_bot._owned_media_bytes")
    def test_owned_collection_skips_budget_overflow_but_keeps_later_small_item(
        self,
        owned_bytes,
    ):
        from management.services.instagram_bot import _collect_media_images

        audio = b"a" * (10 * 1024 * 1024)
        too_large_for_remaining = b"i" * (3 * 1024 * 1024)
        small = b"s" * 1024
        owned_bytes.side_effect = [
            ("audio/ogg", audio),
            ("image/jpeg", too_large_for_remaining),
            ("image/png", small),
        ]
        media = [
            {
                "url": f"https://lookaside.invalid/{index}",
                "provenance": "live_webhook",
                "status": "owned",
            }
            for index in range(3)
        ]

        admitted = _collect_media_images(media)

        self.assertEqual(admitted, [("audio/ogg", audio), ("image/png", small)])

    def test_unsupported_audio_mime_is_rejected(self):
        from management.services.instagram_bot import _bounded_inline_media

        admitted, omitted = _bounded_inline_media([
            ("audio/vnd.unsupported", b"voice"),
        ])

        self.assertEqual(admitted, [])
        self.assertEqual(omitted, 1)


@override_settings(GEMINI_NONLIVE_ADMISSION_MODE="shadow")
class ManualDiagnosticsOnlyTests(TestCase):
    def test_metadata_command_requires_explicit_manual_flag(self):
        with self.assertRaises(CommandError):
            call_command("check_ig_gemini_metadata_health", stdout=StringIO())

    @patch("management.management.commands.check_ig_gemini_metadata_health.gemini_metadata_health.run_hour")
    def test_manual_metadata_command_is_token_free_and_explicit(self, run_hour):
        run_hour.return_value = {
            "checked_aliases": 6,
            "configured_aliases": 6,
            "provider_requests": 6,
            "deadline_skipped_models": 0,
        }
        call_command(
            "check_ig_gemini_metadata_health",
            manual=True,
            stdout=StringIO(),
        )
        run_hour.assert_called_once()

    @patch("management.services.gemini_probe.probe_key")
    @patch("management.services.gemini_probe.probe_key_metadata")
    def test_pool_probe_is_metadata_only_without_quota_spend_confirmation(
        self, metadata_probe, generation_probe
    ):
        call_command("probe_ig_gemini_pool", stdout=StringIO())
        metadata_probe.assert_not_called()
        generation_probe.assert_not_called()


class DeadlinePlanEvidenceTests(TestCase):
    @patch.dict(
        os.environ,
        {alias: f"deadline-key-{index}" for index, alias in enumerate(gemini_keys.ALL_KEYS)},
        clear=False,
    )
    @patch("management.services.call_ai_analysis._chat_timeout", return_value=None)
    def test_deadline_records_every_unstarted_project_candidate(self, _timeout):
        with self.assertRaises(call_ai_analysis.CallAIAnalysisError):
            call_ai_analysis.gemini_generate_text(
                {"contents": []},
                role="chat",
                model_chain_override=["gemini-3.7-flash"],
                reasoning_task="media_analysis",
            )
        rows = GeminiRequestAttempt.objects.filter(
            model="gemini-3.7-flash",
            outcome="not_attempted",
        )
        self.assertEqual(rows.count(), 6)
        self.assertEqual(
            set(rows.values_list("not_attempted_reason", flat=True)),
            {"deadline"},
        )

    @patch.dict(
        os.environ,
        {alias: f"slow-key-{index}" for index, alias in enumerate(gemini_keys.ALL_KEYS)},
        clear=False,
    )
    @patch(
        "management.services.call_ai_analysis._gemini_call_once",
        side_effect=call_ai_analysis._GeminiTransient("timeout: slow model"),
    )
    def test_two_slow_calls_skip_the_rest_of_the_same_model_durably(self, _call):
        with self.assertRaises(call_ai_analysis.CallAIAnalysisError):
            call_ai_analysis.gemini_generate_text(
                {"contents": []},
                role="chat",
                model_chain_override=["gemini-3.7-flash"],
                reasoning_task="media_analysis",
            )
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                model="gemini-3.7-flash",
                outcome="failed",
                failure_kind="read_timeout",
            ).count(),
            2,
        )
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                model="gemini-3.7-flash",
                outcome="not_attempted",
                not_attempted_reason="sla_model_budget",
            ).count(),
            4,
        )
