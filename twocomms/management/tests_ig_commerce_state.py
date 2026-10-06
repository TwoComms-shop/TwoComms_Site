from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from management.models import (
    IgClient,
    IgCommerceManagerReview,
    IgCommerceSelectionSession,
    IgCommerceSelectionTransition,
    IgCommerceTurnDecision,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import instagram_bot
from management.services.ig_commerce_projection import (
    authoritative_session_for,
    project_active_line_to_legacy_client,
)
from management.services.ig_commerce_state import (
    CommerceRevisionConflict,
    _create_decision,
    apply_turn,
    claim_decision_delivery,
    resume_turn_delivery,
)
from management.services.ig_commerce_types import CommerceTurnRequest
from storefront.models import Category, Product, ProductStatus


class CommerceStateFixture:
    def setUp(self):
        super().setUp()
        self.category = Category.objects.create(
            name="Commerce state",
            slug=f"igco-state-{self._testMethodName}"[:45],
        )
        self.classic = self.product("Classic", "classic")
        self.reality = self.product("Reality Bends", "reality")
        self.third = self.product("Third", "third")
        self.client = IgClient.get_or_create_for_sender(
            f"commerce-{self._testMethodName}"[:64]
        )

    def product(self, title, suffix):
        return Product.objects.create(
            title=title,
            slug=f"{suffix}-{self._testMethodName}"[:45],
            category=self.category,
            price=790,
            status=ProductStatus.PUBLISHED,
        )

    def message(self, suffix, *, at=None, reply_to="", quick_reply=""):
        return InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text=suffix,
            mid=f"{self._testMethodName}-{suffix}"[:255],
            provider_created_at=at or timezone.now(),
            reply_to_provider_message_id=reply_to,
            quick_reply_payload=quick_reply,
        )

    def select(self, product):
        return CommerceTurnRequest(exact_product_id=product.pk)


class CommerceProjectionTests(CommerceStateFixture, TestCase):
    def test_legacy_bootstrap_keeps_only_matching_assisted_selection(self):
        self.client.current_product = self.classic
        self.client.current_size = "M"
        self.client.current_color = "black"
        self.client.current_qty = 2
        self.client.current_product_confidence = Decimal("0.93")
        self.client.sales_context = {
            "keep": True,
            "assisted_checkout_selection": {
                "product_id": self.classic.pk,
                "fit_option_code": "classic",
                "color_variant_id": 77,
                "option_values": {"material": "thermo"},
            },
        }
        self.client.save()

        session = authoritative_session_for(self.client)

        self.assertEqual(session.lines[0]["product_id"], self.classic.pk)
        self.assertEqual(session.lines[0]["size"], "M")
        self.assertEqual(session.lines[0]["fit_option_code"], "classic")
        self.assertEqual(session.lines[0]["option_values"], {"material": "thermo"})

        other = IgClient.get_or_create_for_sender(
            f"mismatch-{self._testMethodName}"[:64]
        )
        other.current_product = self.classic
        other.sales_context = {
            "assisted_checkout_selection": {
                "product_id": self.reality.pk,
                "fit_option_code": "oversize",
            }
        }
        other.save()
        mismatched = authoritative_session_for(other)
        self.assertNotIn("fit_option_code", mismatched.lines[0])

    def test_session_projection_is_authoritative_for_legacy_fields(self):
        session = authoritative_session_for(self.client)
        session.lines = [{
            "line_id": "line-1",
            "product_id": self.classic.pk,
            "size": "L",
            "color": "white",
            "quantity": 3,
            "confidence": "0.88",
            "fit_option_code": "classic",
            "option_values": {"material": "standard"},
        }]
        session.active_index = 0
        session.save()
        self.client.current_product = self.reality
        self.client.current_size = "S"
        self.client.save()

        project_active_line_to_legacy_client(session, self.client)
        self.client.refresh_from_db()

        self.assertEqual(self.client.current_product_id, self.classic.pk)
        self.assertEqual(self.client.current_size, "L")
        self.assertEqual(self.client.current_color, "white")
        self.assertEqual(self.client.current_qty, 3)
        self.assertEqual(
            self.client.sales_context["assisted_checkout_selection"]["product_id"],
            self.classic.pk,
        )

    def test_product_replacement_changes_only_active_line(self):
        session = authoritative_session_for(self.client)
        session.lines = [
            {"line_id": "line-1", "product_id": self.classic.pk, "quantity": 1},
            {
                "line_id": "line-2",
                "product_id": self.reality.pk,
                "quantity": 2,
                "size": "M",
                "confidence": "0.94",
            },
        ]
        session.active_index = 1
        session.candidate_product_ids = [self.classic.pk, self.reality.pk]
        session.candidate_digest = "d" * 64
        session.candidate_generation = 4
        session.candidate_prompt_provider_ids = ["old-candidate-mid"]
        session.save()

        apply_turn(self.client, self.message("replace"), self.select(self.third))
        session.refresh_from_db()

        self.assertEqual(session.lines[0]["product_id"], self.classic.pk)
        self.assertEqual(session.lines[1]["line_id"], "line-2")
        self.assertEqual(session.lines[1]["product_id"], self.third.pk)
        self.assertEqual(session.lines[1]["quantity"], 1)
        self.assertNotIn("size", session.lines[1])
        self.assertNotIn("confidence", session.lines[1])
        self.assertEqual(session.candidate_product_ids, [])
        self.assertEqual(session.candidate_digest, "")
        self.assertEqual(session.candidate_prompt_provider_ids, [])
        self.assertEqual(session.candidate_generation, 4)
        self.client.refresh_from_db()
        self.assertEqual(self.client.current_product_id, self.third.pk)
        self.assertEqual(self.client.current_qty, 1)
        self.assertEqual(self.client.current_product_confidence, Decimal("0"))

    def test_product_replacement_clears_every_old_commercial_field(self):
        session = authoritative_session_for(self.client)
        session.lines = [{
            "line_id": "line-1",
            "product_id": self.classic.pk,
            "quantity": 3,
            "size": "M",
            "color": "white",
            "fit_option_code": "classic",
            "color_variant_id": 77,
            "option_values": {"material": "thermo"},
            "price": "1090.00",
            "unit_price": "1090.00",
            "pay_type": "full",
            "availability_error": "out_of_stock",
            "proposal_intent": "checkout",
            "unknown_future_commercial_field": "must-not-leak",
        }]
        session.active_index = 0
        session.selection_constraints = {"back_decoration": "print"}
        session.query_constraints = {"query": "old product"}
        session.save()

        apply_turn(self.client, self.message("replace-all"), self.select(self.third))
        session.refresh_from_db()

        self.assertEqual(
            session.lines[0],
            {"line_id": "line-1", "product_id": self.third.pk, "quantity": 1},
        )
        self.assertEqual(session.selection_constraints, {})
        self.assertEqual(session.query_constraints, {})

    def test_product_replacement_applies_explicit_new_fields_from_same_turn(self):
        session = authoritative_session_for(self.client)
        session.lines = [{
            "line_id": "line-1",
            "product_id": self.classic.pk,
            "quantity": 2,
            "size": "S",
            "color": "black",
            "fit_option_code": "classic",
            "option_values": {"material": "standard"},
        }]
        session.active_index = 0
        session.selection_constraints = {"back_decoration": "print"}
        session.query_constraints = {"query": "old product"}
        session.save()

        request = CommerceTurnRequest(
            exact_product_id=self.third.pk,
            field_updates={"size": "L", "color": "green", "fit": "oversize", "quantity": "4"},
            hard={"back_decoration": "none"},
        )
        apply_turn(self.client, self.message("replace-configured"), request)
        session.refresh_from_db()

        self.assertEqual(
            session.lines[0],
            {
                "line_id": "line-1",
                "product_id": self.third.pk,
                "quantity": 4,
                "size": "L",
                "color": "green",
                "fit_option_code": "oversize",
            },
        )
        self.assertEqual(session.selection_constraints, {"back_decoration": "none"})
        self.assertEqual(session.query_constraints, {})

    def test_rejected_product_is_recorded_and_clears_its_configuration(self):
        session = authoritative_session_for(self.client)
        session.lines = [{
            "line_id": "line-1",
            "product_id": self.reality.pk,
            "quantity": 2,
            "size": "L",
            "color": "pink",
            "fit_option_code": "oversize",
            "color_variant_id": 91,
            "price": "1450.00",
        }]
        session.active_index = 0
        session.selection_constraints = {"back_decoration": "print"}
        session.query_constraints = {"query": "reality bends"}
        session.save()

        decision = apply_turn(
            self.client,
            self.message("reject-reality"),
            CommerceTurnRequest(
                rejected_product_ids=(self.reality.pk,),
                field_updates={"color": "black", "fit": "classic"},
            ),
        )

        session.refresh_from_db()
        self.assertEqual(decision.transition.action, "product_rejected")
        self.assertEqual(session.rejected_reason, "customer_rejected_product")
        self.assertEqual(session.rejected_selection["product_ids"], [self.reality.pk])
        self.assertEqual(
            session.lines[0],
            {"line_id": "line-1", "color": "black", "fit_option_code": "classic"},
        )
        self.assertEqual(session.selection_constraints, {})
        self.assertEqual(session.query_constraints, {})


class CommerceWorkerIntegrationTests(CommerceStateFixture, TestCase):
    def test_live_media_commerce_owner_preserves_unknown_delivery(self):
        from management.services.ig_commerce_replies import build_durable_reply_payload

        source = self.message("live-media-unknown")
        decision = apply_turn(
            self.client,
            source,
            self.select(self.classic),
            reply_builder=build_durable_reply_payload,
        )
        decision.delivery_state = IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED
        decision.reconciliation_result = {
            "delivery_owner": instagram_bot.LIVE_MEDIA_COMMERCE_OWNER,
            "source_message_id": source.pk,
        }
        decision.save(update_fields=[
            "delivery_state", "reconciliation_result", "updated_at",
        ])

        instagram_bot._finalize_live_media_commerce_delivery(
            decision,
            state="unknown",
            error="provider_message_id_missing",
        )

        decision.refresh_from_db()
        self.assertEqual(
            decision.delivery_state,
            IgCommerceTurnDecision.DeliveryState.UNKNOWN,
        )
        self.assertEqual(
            decision.reconciliation_status,
            IgCommerceTurnDecision.ReconciliationStatus.REQUIRED,
        )
        self.assertEqual(decision.delivery_error, "provider_message_id_missing")

    def test_image_and_durable_commerce_fact_share_one_live_answer(self):
        self.client.profile_fetched_at = timezone.now()
        self.client.save(update_fields=["profile_fetched_at", "updated_at"])
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "ai_enabled", "allowed_senders"])
        raw = b"commerce-image"
        url = "https://lookaside.example/commerce-image.jpg"
        row = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text=f"https://twocomms.shop/product/{self.classic.slug}/",
            mid="commerce-worker-image-reference",
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
            source="webhook",
            media_capture_eligible=True,
            attachment_media=[{
                "url": url,
                "type": "image",
                "provenance": "live_webhook",
                "status": "owned",
                "storage_name": "private/commerce.jpg",
                "mime": "image/jpeg",
            }],
        )
        part = {
            "source_part_id": "mp1_" + "c" * 32,
            "source_message_scope": "scope",
            "original_index": 0,
            "identity_origin": "ingress",
            "provenance": "live_webhook",
            "status": "owned",
            "capture_state": "owned",
            "mime": "image/jpeg",
            "bytes": len(raw),
            "content_hash": hashlib.sha256(raw).hexdigest(),
            "data": raw,
        }
        from management.services.instagram_bot import ProviderDeliveryReceipt

        with patch(
            "management.services.bot_sales_classifier.ensure_rule_classification",
            return_value=None,
        ), patch(
            "management.services.instagram_bot._capture_message_media"
        ), patch(
            "management.services.instagram_bot._collect_media_parts",
            return_value=[part],
        ), patch(
            "management.services.instagram_bot._rate_exceeded", return_value=False
        ), patch(
            "management.services.instagram_bot._repeated_question", return_value=0
        ), patch(
            "management.services.instagram_bot.send_sender_action"
        ), patch(
            "management.services.instagram_bot.gemini_generate",
            return_value=(
                "Бачу фото. Зафіксувала цей варіант; підкажіть розмір, колір і кількість."
            ),
        ) as generate, patch(
            "management.services.instagram_bot.send_text",
            return_value=ProviderDeliveryReceipt(
                True,
                "",
                "",
                "provider-commerce-image",
            ),
        ) as send_text:
            self.assertTrue(instagram_bot._process_one(settings, row))

        generate.assert_called_once()
        send_text.assert_called_once()
        self.assertEqual(len(generate.call_args.kwargs["images"]), 1)
        self.assertIn(
            "Зафіксувала цей варіант",
            generate.call_args.kwargs["turn_note"],
        )
        decision = IgCommerceTurnDecision.objects.get(source_message=row)
        self.assertEqual(decision.delivery_state, IgCommerceTurnDecision.DeliveryState.SENT)
        self.assertEqual(decision.provider_message_ids, ["provider-commerce-image"])
        self.assertEqual(decision.attempts, 0)
        self.assertEqual(
            decision.reconciliation_result["delivery_owner"],
            instagram_bot.LIVE_MEDIA_COMMERCE_OWNER,
        )

    def test_worker_persists_exact_product_reference_before_gemini(self):
        self.client.current_product = self.reality
        self.client.current_size = "L"
        self.client.current_color = "pink"
        self.client.current_qty = 2
        self.client.profile_fetched_at = timezone.now()
        self.client.save()
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = True
        settings.save(update_fields=["is_enabled", "ai_enabled", "updated_at"])
        row = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text=f"https://twocomms.shop/product/{self.classic.slug}/",
            mid="commerce-worker-exact-reference",
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
        )
        from management.services.instagram_bot import ProviderDeliveryReceipt

        with patch(
            "management.services.bot_sales_classifier.ensure_rule_classification",
            return_value=None,
        ), patch(
            "management.services.instagram_bot._rate_exceeded", return_value=False
        ), patch(
            "management.services.instagram_bot._repeated_question", return_value=0
        ), patch("management.services.instagram_bot.send_sender_action"), patch(
            "management.services.instagram_bot.gemini_generate",
            side_effect=AssertionError("durable commerce reply must skip Gemini"),
        ), patch(
            "management.services.instagram_bot.send_text",
            return_value=ProviderDeliveryReceipt(
                True, "", "", "provider-commerce-exact"
            ),
        ):
            self.assertTrue(instagram_bot._process_one(settings, row))

        decision = IgCommerceTurnDecision.objects.get(source_message=row)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.delivery_state, IgCommerceTurnDecision.DeliveryState.SENT)
        self.assertEqual(decision.session.lines[0]["product_id"], self.classic.pk)
        self.client.refresh_from_db()
        self.assertEqual(self.client.current_product_id, self.classic.pk)


class CommerceStateTests(CommerceStateFixture, TransactionTestCase):
    reset_sequences = True

    def test_same_source_replay_returns_decision_after_revision_changed(self):
        source = self.message("same-source")
        first = apply_turn(self.client, source, self.select(self.classic))
        apply_turn(self.client, self.message("advance"), self.select(self.reality))

        replay = apply_turn(self.client, source, self.select(self.third))

        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(
            IgCommerceSelectionTransition.objects.filter(source_message=source).count(),
            1,
        )
        self.assertEqual(replay.request_payload["exact_product_id"], self.classic.pk)

    def test_delayed_older_message_is_stale_and_cannot_project_or_send(self):
        now = timezone.now()
        fresh = self.message("fresh", at=now)
        delayed = self.message("delayed", at=now - timedelta(hours=1))
        apply_turn(self.client, fresh, self.select(self.classic))

        stale = apply_turn(self.client, delayed, self.select(self.reality))

        self.assertTrue(stale.is_stale)
        self.assertFalse(stale.accepted)
        self.assertFalse(stale.delivery_required)
        self.assertEqual(stale.delivery_state, IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED)
        self.assertIsNone(stale.transition_id)
        session = authoritative_session_for(self.client)
        self.assertEqual(session.lines[0]["product_id"], self.classic.pk)
        self.client.refresh_from_db()
        self.assertEqual(self.client.current_product_id, self.classic.pk)

    def test_effective_order_uses_mid_as_stable_tie_breaker(self):
        at = timezone.now()
        later_mid = self.message("z-mid", at=at)
        earlier_mid = self.message("a-mid", at=at)
        later_mid.mid = "z-provider-mid"
        later_mid.save(update_fields=["mid"])
        earlier_mid.mid = "a-provider-mid"
        earlier_mid.save(update_fields=["mid"])
        apply_turn(self.client, later_mid, self.select(self.classic))

        stale = apply_turn(self.client, earlier_mid, self.select(self.reality))

        self.assertTrue(stale.is_stale)
        self.assertEqual(authoritative_session_for(self.client).lines[0]["product_id"], self.classic.pk)

    def test_expected_revision_conflict_has_no_mutation(self):
        session = authoritative_session_for(self.client)
        before = session.revision

        with self.assertRaises(CommerceRevisionConflict):
            apply_turn(
                self.client,
                self.message("conflict"),
                self.select(self.classic),
                expected_revision=before + 1,
            )

        session.refresh_from_db()
        self.assertEqual(session.revision, before)
        self.assertFalse(IgCommerceTurnDecision.objects.exists())
        self.assertFalse(IgCommerceSelectionTransition.objects.exists())

    def test_delivery_outbox_does_not_repeat_selection_effect(self):
        decision = apply_turn(
            self.client,
            self.message("outbox-effect"),
            self.select(self.classic),
            reply_payload={"text": ["Готово"]},
        )
        transition_id = decision.transition_id

        claimed = claim_decision_delivery(decision)

        self.assertEqual(claimed.pk, decision.pk)
        self.assertEqual(claimed.delivery_state, IgCommerceTurnDecision.DeliveryState.SENDING)
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), 1)
        self.assertEqual(claimed.transition_id, transition_id)

    def test_pending_decision_resumes_once_without_reducing_again(self):
        decision = apply_turn(
            self.client,
            self.message("resume"),
            self.select(self.classic),
            reply_payload={"text": ["Готово"], "media": ["https://cdn/item.jpg"]},
        )
        calls = []

        def transport(stored):
            calls.append(stored.pk)
            return {
                "state": "sent",
                "text_receipts": [{"index": 0, "provider_message_id": "text-mid"}],
                "media_receipts": [{"index": 0, "provider_message_id": "media-mid"}],
            }

        first = resume_turn_delivery(decision.source_message, transport=transport)
        second = resume_turn_delivery(decision.source_message, transport=transport)

        self.assertEqual(calls, [decision.pk])
        self.assertEqual(first.delivery_state, IgCommerceTurnDecision.DeliveryState.SENT)
        self.assertEqual(second.delivery_state, IgCommerceTurnDecision.DeliveryState.SENT)
        self.assertEqual(first.provider_message_ids, ["text-mid", "media-mid"])
        self.assertEqual(IgCommerceSelectionTransition.objects.count(), 1)

    def test_sent_requires_receipts_for_every_text_and_media_part(self):
        decision = apply_turn(
            self.client,
            self.message("incomplete-receipts"),
            self.select(self.classic),
            reply_payload={
                "text": ["Part one", "Part two"],
                "media": ["https://cdn/item.jpg"],
            },
        )

        delivered = resume_turn_delivery(
            decision.source_message,
            transport=lambda stored: {
                "state": "sent",
                "text_receipts": [{"index": 0, "provider_message_id": "text-0"}],
                "media_receipts": [{"index": 0, "provider_message_id": "media-0"}],
            },
        )

        self.assertEqual(delivered.delivery_state, IgCommerceTurnDecision.DeliveryState.PARTIAL)
        self.assertEqual(
            delivered.reconciliation_status,
            IgCommerceTurnDecision.ReconciliationStatus.REQUIRED,
        )
        self.assertTrue(
            IgCommerceManagerReview.objects.filter(
                decision=decision,
                reason="delivery_partial",
            ).exists()
        )

    def test_missing_transport_does_not_cross_claim_boundary(self):
        decision = apply_turn(
            self.client,
            self.message("missing-transport"),
            self.select(self.classic),
            reply_payload={"text": ["Ready"]},
        )

        with self.assertRaisesMessage(ValueError, "injected transport"):
            resume_turn_delivery(decision.source_message)

        decision.refresh_from_db()
        self.assertEqual(decision.delivery_state, IgCommerceTurnDecision.DeliveryState.PENDING)
        self.assertEqual(decision.attempts, 0)

    def test_non_pending_outbox_states_never_blind_resend(self):
        blocked_states = (
            IgCommerceTurnDecision.DeliveryState.SENDING,
            IgCommerceTurnDecision.DeliveryState.UNKNOWN,
            IgCommerceTurnDecision.DeliveryState.PARTIAL,
            IgCommerceTurnDecision.DeliveryState.SENT,
            IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED,
        )
        calls = []
        for index, state in enumerate(blocked_states):
            decision = apply_turn(
                self.client,
                self.message(f"blocked-{index}"),
                CommerceTurnRequest(info_topics=("size_guide",)),
                reply_payload={"text": ["Info"]},
            )
            IgCommerceTurnDecision.objects.filter(pk=decision.pk).update(delivery_state=state)
            resume_turn_delivery(
                decision.source_message,
                transport=lambda stored: calls.append(stored.pk),
            )

        self.assertEqual(calls, [])

    def test_transport_exception_is_unknown_and_not_retried(self):
        decision = apply_turn(
            self.client,
            self.message("ambiguous"),
            self.select(self.classic),
            reply_payload={"text": ["Готово"]},
        )
        calls = []

        def transport(stored):
            calls.append(stored.pk)
            raise TimeoutError("provider result unknown")

        first = resume_turn_delivery(decision.source_message, transport=transport)
        second = resume_turn_delivery(decision.source_message, transport=transport)

        self.assertEqual(calls, [decision.pk])
        self.assertEqual(first.delivery_state, IgCommerceTurnDecision.DeliveryState.UNKNOWN)
        self.assertEqual(second.delivery_state, IgCommerceTurnDecision.DeliveryState.UNKNOWN)
        review = IgCommerceManagerReview.objects.get(decision=decision)
        self.assertGreater(review.due_at, timezone.now())
        self.assertEqual(len(review.selection_digest), 64)
        self.assertNotIn("igsid", review.selection_snapshot)

    def test_review_uniqueness_race_rolls_back_inner_savepoint(self):
        decision = apply_turn(
            self.client,
            self.message("review-race"),
            self.select(self.classic),
            reply_payload={"text": ["Ready"]},
        )

        IgCommerceManagerReview.objects.create(
            idempotency_key=f"commerce-decision:{decision.pk}:delivery_unknown",
            client=self.client,
            session=decision.session,
            decision=decision,
            reason="delivery_unknown",
            due_at=timezone.now(),
        )

        delivered = resume_turn_delivery(
            decision.source_message,
            transport=lambda stored: {"state": "unknown"},
        )

        self.assertEqual(delivered.delivery_state, IgCommerceTurnDecision.DeliveryState.UNKNOWN)
        self.assertTrue(IgCommerceTurnDecision.objects.filter(pk=decision.pk).exists())

    def test_candidate_generation_survives_information_only_revision(self):
        prompt = apply_turn(
            self.client,
            self.message("prompt-one"),
            CommerceTurnRequest(query="options"),
            candidate_prompt={
                "product_ids": [self.classic.pk, self.reality.pk],
                "digest": "a" * 64,
                "provider_message_ids": ["candidate-mid-1"],
            },
        )
        generation = prompt.session.candidate_generation

        info = apply_turn(
            self.client,
            self.message("info"),
            CommerceTurnRequest(info_topics=("size_guide",)),
        )

        self.assertGreater(info.session.revision, prompt.transition.to_revision)
        self.assertEqual(info.session.candidate_generation, generation)
        self.assertEqual(info.session.candidate_prompt_provider_ids, ["candidate-mid-1"])

    def test_replaced_candidate_prompt_invalidates_old_numeric_reply(self):
        apply_turn(
            self.client,
            self.message("prompt-old"),
            CommerceTurnRequest(query="options"),
            candidate_prompt={
                "product_ids": [self.classic.pk, self.reality.pk],
                "digest": "a" * 64,
                "provider_message_ids": ["candidate-mid-old"],
            },
        )
        apply_turn(
            self.client,
            self.message("prompt-new"),
            CommerceTurnRequest(query="other options"),
            candidate_prompt={
                "product_ids": [self.reality.pk, self.third.pk],
                "digest": "b" * 64,
                "provider_message_ids": ["candidate-mid-new"],
            },
        )

        rejected = apply_turn(
            self.client,
            self.message("1", reply_to="candidate-mid-old"),
            CommerceTurnRequest(query="1"),
        )

        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.result_payload["reason"], "candidate_prompt_mismatch")
        rejected.session.refresh_from_db()
        self.assertEqual(
            rejected.session.last_provider_message_id,
            rejected.source_message.mid,
        )
        self.assertEqual(rejected.transition.action, "candidate_rejected")
        self.assertEqual(rejected.session.rejected_reason, "candidate_prompt_mismatch")
        self.assertEqual(
            authoritative_session_for(self.client).candidate_prompt_provider_ids,
            ["candidate-mid-new"],
        )

    def test_current_reply_or_quick_reply_can_select_candidate_number(self):
        apply_turn(
            self.client,
            self.message("prompt-current"),
            CommerceTurnRequest(query="options"),
            candidate_prompt={
                "product_ids": [self.classic.pk, self.reality.pk],
                "digest": "a" * 64,
                "provider_message_ids": ["candidate-mid-current"],
            },
        )

        selected = apply_turn(
            self.client,
            self.message("choose", quick_reply="commerce:1:select:1"),
            CommerceTurnRequest(query="1"),
        )

        self.assertTrue(selected.accepted)
        self.assertEqual(authoritative_session_for(self.client).lines[0]["product_id"], self.classic.pk)

    def test_quick_reply_requires_exact_current_candidate_generation(self):
        prompt = apply_turn(
            self.client,
            self.message("prompt-exact"),
            CommerceTurnRequest(query="options"),
            candidate_prompt={
                "product_ids": [self.classic.pk, self.reality.pk],
                "digest": "e" * 64,
                "provider_message_ids": ["candidate-exact-mid"],
            },
        )
        generation = prompt.session.candidate_generation

        wrong_generation = apply_turn(
            self.client,
            self.message("wrong-generation", quick_reply=f"commerce:{generation + 1}:select:1"),
            CommerceTurnRequest(query="1"),
        )

        self.assertFalse(wrong_generation.accepted)
        self.assertEqual(
            wrong_generation.result_payload["reason"],
            "candidate_prompt_mismatch",
        )

    def test_one_open_session_and_review_idempotency_are_database_enforced(self):
        session = authoritative_session_for(self.client)
        with self.assertRaises(IntegrityError):
            with connection.cursor():
                IgCommerceSelectionSession.objects.create(
                    client=self.client,
                    generation=session.generation + 1,
                    open_slot=1,
                )

        review = IgCommerceManagerReview.objects.create(
            idempotency_key=f"review:{self.client.pk}",
            client=self.client,
            session=session,
            reason="ambiguous_selection",
            selection_snapshot={"product_id": self.classic.pk},
            selection_digest="c" * 64,
            selection_generation=session.generation,
            due_at=timezone.now() + timedelta(minutes=15),
        )
        with self.assertRaises(IntegrityError):
            IgCommerceManagerReview.objects.create(
                idempotency_key=review.idempotency_key,
                client=self.client,
                session=session,
                reason="duplicate",
                due_at=timezone.now(),
            )

    def test_duplicate_decision_integrity_race_fetches_database_winner(self):
        source = self.message("decision-winner")
        session = authoritative_session_for(self.client)
        winner = IgCommerceTurnDecision.objects.create(
            source_message=source,
            session=session,
            request_payload={"winner": True},
            result_payload={},
            reply_payload={},
            effects_payload={},
            delivery_required=False,
            delivery_state=IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED,
        )

        with transaction.atomic(), patch.object(
            IgCommerceTurnDecision.objects,
            "create",
            side_effect=IntegrityError("unique source race"),
        ):
            replay = _create_decision(
                source_message=source,
                session=session,
                request_payload={"loser": True},
                result_payload={},
                reply_payload={},
                effects_payload={},
                delivery_required=False,
                delivery_state=IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED,
            )

        self.assertEqual(replay.pk, winner.pk)


class CommerceIngressIdentityTests(TestCase):
    def setUp(self):
        super().setUp()
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.allowed_senders = ""
        self.settings.save()

    def test_enqueue_persists_reply_and_quick_reply_identity(self):
        payload = "commerce:" + ("x" * 980)
        queued = instagram_bot.enqueue_inbound(
            self.settings,
            sender_id="commerce-ingress",
            text="1",
            mid="commerce-ingress-mid",
            reply_to_provider_message_id="candidate-provider-mid",
            quick_reply_payload=payload,
        )

        self.assertTrue(queued)
        message = InstagramBotMessage.objects.get(mid="commerce-ingress-mid")
        self.assertEqual(message.reply_to_provider_message_id, "candidate-provider-mid")
        self.assertEqual(message.quick_reply_payload, payload)
        self.assertEqual(
            InstagramBotMessage._meta.get_field("quick_reply_payload").max_length,
            1000,
        )

    def test_webhook_and_poll_persistence_thread_identity(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "commerce-webhook"},
            "message": {
                "mid": "commerce-webhook-mid",
                "text": "1",
                "reply_to": {"mid": "candidate-webhook-mid"},
                "quick_reply": {"payload": "commerce:1:candidate-webhook-mid"},
            },
        }]}]}
        self.assertEqual(instagram_bot.handle_webhook_payload(self.settings, payload), 1)
        webhook = InstagramBotMessage.objects.get(mid="commerce-webhook-mid")
        self.assertEqual(webhook.reply_to_provider_message_id, "candidate-webhook-mid")
        self.assertEqual(webhook.quick_reply_payload, "commerce:1:candidate-webhook-mid")

        poll = {
            "id": "commerce-poll-mid",
            "from": {"id": "commerce-poll"},
            "to": {"data": [{"id": self.settings.ig_user_id or "page"}]},
            "message": "1",
            "created_time": timezone.now().isoformat(),
        }
        self.assertTrue(instagram_bot._persist_polled_message(self.settings, poll))
        poll["reply_to"] = {"mid": "candidate-poll-mid"}
        poll["quick_reply"] = {"payload": "commerce:1:candidate-poll-mid"}
        self.assertTrue(instagram_bot._persist_polled_message(self.settings, poll))
        stored = InstagramBotMessage.objects.get(mid="commerce-poll-mid")
        self.assertEqual(stored.reply_to_provider_message_id, "candidate-poll-mid")
        self.assertEqual(stored.quick_reply_payload, "commerce:1:candidate-poll-mid")

    def test_manual_refresh_promotion_keeps_reply_identity(self):
        event_at = timezone.now()
        client = IgClient.get_or_create_for_sender("commerce-promotion")
        existing = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="1",
            mid="commerce-promotion-mid",
            source="manual_refresh",
            status=InstagramBotMessage.Status.DONE,
            provider_created_at=event_at,
            processed_at=timezone.now(),
        )
        self.settings.reply_after = event_at - timedelta(minutes=1)
        self.settings.save(update_fields=["reply_after"])

        promoted = instagram_bot.enqueue_inbound(
            self.settings,
            sender_id=client.igsid,
            text="1",
            mid=existing.mid,
            source="webhook",
            received_at=event_at,
            reply_to_provider_message_id="candidate-promotion-mid",
            quick_reply_payload="commerce:1:select:1",
        )

        self.assertTrue(promoted)
        existing.refresh_from_db()
        self.assertEqual(existing.status, InstagramBotMessage.Status.PENDING)
        self.assertEqual(existing.source, "webhook")
        self.assertEqual(existing.reply_to_provider_message_id, "candidate-promotion-mid")
        self.assertEqual(existing.quick_reply_payload, "commerce:1:select:1")


class CommerceDurabilityGuardTests(CommerceStateFixture, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                for name in (
                    "ig_commerce_transition_no_update",
                    "ig_commerce_transition_no_delete",
                    "ig_commerce_decision_identity_no_update",
                    "ig_commerce_decision_no_delete",
                ):
                    cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
                cursor.execute(
                    "CREATE TRIGGER ig_commerce_transition_no_update BEFORE UPDATE "
                    "ON management_igcommerceselectiontransition BEGIN SELECT RAISE(ABORT, "
                    "'IgCommerceSelectionTransition is append-only'); END"
                )
                cursor.execute(
                    "CREATE TRIGGER ig_commerce_transition_no_delete BEFORE DELETE "
                    "ON management_igcommerceselectiontransition BEGIN SELECT RAISE(ABORT, "
                    "'IgCommerceSelectionTransition is append-only'); END"
                )
                cursor.execute(
                    "CREATE TRIGGER ig_commerce_decision_identity_no_update BEFORE UPDATE "
                    "ON management_igcommerceturndecision WHEN "
                    "OLD.source_message_id IS NOT NEW.source_message_id OR "
                    "OLD.session_id IS NOT NEW.session_id OR OLD.transition_id IS NOT NEW.transition_id OR "
                    "OLD.request_payload IS NOT NEW.request_payload OR OLD.result_payload IS NOT NEW.result_payload OR "
                    "OLD.reply_payload IS NOT NEW.reply_payload OR OLD.effects_payload IS NOT NEW.effects_payload OR "
                    "OLD.accepted IS NOT NEW.accepted OR OLD.is_stale IS NOT NEW.is_stale OR "
                    "OLD.delivery_required IS NOT NEW.delivery_required BEGIN SELECT RAISE(ABORT, "
                    "'IgCommerceTurnDecision identity is immutable'); END"
                )
                cursor.execute(
                    "CREATE TRIGGER ig_commerce_decision_no_delete BEFORE DELETE "
                    "ON management_igcommerceturndecision BEGIN SELECT RAISE(ABORT, "
                    "'IgCommerceTurnDecision is durable'); END"
                )

    @classmethod
    def tearDownClass(cls):
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                for name in (
                    "ig_commerce_transition_no_update",
                    "ig_commerce_transition_no_delete",
                    "ig_commerce_decision_identity_no_update",
                    "ig_commerce_decision_no_delete",
                ):
                    cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.decision = apply_turn(
            self.client,
            self.message("guard"),
            self.select(self.classic),
            reply_payload={"text": ["Готово"]},
        )

    def test_transition_rejects_app_and_raw_sql_mutation_or_delete(self):
        transition = self.decision.transition
        transition.action = "changed"
        with self.assertRaisesMessage(ValueError, "append-only"):
            transition.save()
        with self.assertRaisesMessage(ValueError, "append-only"):
            IgCommerceSelectionTransition.objects.filter(pk=transition.pk).update(action="changed")
        with self.assertRaisesMessage(ValueError, "append-only"):
            transition.delete()
        with self.assertRaisesMessage(ValueError, "append-only"):
            IgCommerceSelectionTransition.objects.filter(pk=transition.pk).delete()
        if connection.vendor == "sqlite":
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE management_igcommerceselectiontransition SET action='raw' WHERE id=%s",
                            [transition.pk],
                        )
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM management_igcommerceselectiontransition WHERE id=%s",
                            [transition.pk],
                        )

    def test_decision_payload_is_immutable_but_delivery_fields_are_mutable(self):
        decision = self.decision
        decision.request_payload = {"changed": True}
        with self.assertRaisesMessage(ValueError, "identity is immutable"):
            decision.save()
        with self.assertRaisesMessage(ValueError, "identity is immutable"):
            IgCommerceTurnDecision.objects.filter(pk=decision.pk).update(
                result_payload={"changed": True}
            )

        IgCommerceTurnDecision.objects.filter(pk=decision.pk).update(
            delivery_state=IgCommerceTurnDecision.DeliveryState.UNKNOWN,
            reconciliation_status=IgCommerceTurnDecision.ReconciliationStatus.REQUIRED,
        )
        decision.refresh_from_db()
        self.assertEqual(decision.delivery_state, IgCommerceTurnDecision.DeliveryState.UNKNOWN)
        with self.assertRaisesMessage(ValueError, "durable"):
            decision.delete()
        with self.assertRaisesMessage(ValueError, "durable"):
            IgCommerceTurnDecision.objects.filter(pk=decision.pk).delete()

        if connection.vendor == "sqlite":
            with self.assertRaises(DatabaseError):
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE management_igcommerceturndecision SET accepted=0 WHERE id=%s",
                        [decision.pk],
                    )


class CommerceStateMigrationTests(SimpleTestCase):
    def test_0146_forward_guards_and_reverse_are_executable(self):
        script = textwrap.dedent(
            """
            import json
            import os
            import sys

            os.environ["DJANGO_SETTINGS_MODULE"] = "twocomms.settings"
            from django.conf import settings
            settings.DATABASES["default"] = {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": sys.argv[1],
            }

            import django
            django.setup()

            from django.db import DatabaseError, connection
            from django.db.migrations.executor import MigrationExecutor

            migrate_from = ("management", "0145_ig_inventory_revision_safety")
            migrate_to = ("management", "0146_ig_commerce_selection_state")
            executor = MigrationExecutor(connection)
            executor.migrate([migrate_from])
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([migrate_from]).apps
            Client = old_apps.get_model("management", "IgClient")
            Message = old_apps.get_model("management", "InstagramBotMessage")
            client = Client.objects.create(igsid="commerce-migration-client", stage="new")
            source = Message.objects.create(
                sender_id=client.igsid,
                client_id=client.pk,
                role="user",
                text="select",
                mid="commerce-migration-source",
                status="done",
            )

            executor = MigrationExecutor(connection)
            executor.migrate([migrate_to])
            executor = MigrationExecutor(connection)
            new_apps = executor.loader.project_state([migrate_to]).apps
            Session = new_apps.get_model("management", "IgCommerceSelectionSession")
            Transition = new_apps.get_model("management", "IgCommerceSelectionTransition")
            Decision = new_apps.get_model("management", "IgCommerceTurnDecision")
            session = Session.objects.create(client_id=client.pk, generation=1, open_slot=1)
            transition = Transition.objects.create(
                session_id=session.pk,
                source_message_id=source.pk,
                action="selected",
                from_revision=0,
                to_revision=1,
                previous_snapshot={},
                next_snapshot={"revision": 1},
                source_order_key="2026-08-05T00:00:00+00:00|mid",
            )
            decision = Decision.objects.create(
                source_message_id=source.pk,
                session_id=session.pk,
                transition_id=transition.pk,
                request_payload={},
                result_payload={},
                reply_payload={},
                effects_payload={},
                delivery_required=True,
                delivery_state="pending",
            )

            transition_update_guarded = False
            transition_delete_guarded = False
            decision_identity_guarded = False
            decision_delivery_allowed = False
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE management_igcommerceselectiontransition SET action='raw' WHERE id=%s",
                        [transition.pk],
                    )
            except DatabaseError:
                transition_update_guarded = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM management_igcommerceselectiontransition WHERE id=%s",
                        [transition.pk],
                    )
            except DatabaseError:
                transition_delete_guarded = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE management_igcommerceturndecision SET accepted=0 WHERE id=%s",
                        [decision.pk],
                    )
            except DatabaseError:
                decision_identity_guarded = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE management_igcommerceturndecision SET delivery_state='sending' WHERE id=%s",
                        [decision.pk],
                    )
                decision_delivery_allowed = True
            except DatabaseError:
                decision_delivery_allowed = False

            executor = MigrationExecutor(connection)
            executor.migrate([migrate_from])
            executor = MigrationExecutor(connection)
            reverted_apps = executor.loader.project_state([migrate_from]).apps
            table_names = set(connection.introspection.table_names())
            message_fields = {
                field.name
                for field in reverted_apps.get_model(
                    "management", "InstagramBotMessage"
                )._meta.get_fields()
            }
            print("MIGRATION_RESULT=" + json.dumps({
                "guards": {
                    "transition_update": transition_update_guarded,
                    "transition_delete": transition_delete_guarded,
                    "decision_identity": decision_identity_guarded,
                    "decision_delivery_allowed": decision_delivery_allowed,
                },
                "reversed": {
                    "session_table_absent": "management_igcommerceselectionsession" not in table_names,
                    "reply_field_absent": "reply_to_provider_message_id" not in message_fields,
                    "quick_field_absent": "quick_reply_payload" not in message_fields,
                },
            }, sort_keys=True))
            """
        )
        project_root = os.path.dirname(os.path.dirname(__file__))
        env = os.environ.copy()
        for key in ("DB_ENGINE", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT"):
            env.pop(key, None)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (project_root, env.get("PYTHONPATH", "")))
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-c", script, os.path.join(temp_dir, "migration.sqlite3")],
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        marker = next(
            line for line in result.stdout.splitlines() if line.startswith("MIGRATION_RESULT=")
        )
        self.assertEqual(
            json.loads(marker.removeprefix("MIGRATION_RESULT=")),
            {
                "guards": {
                    "transition_update": True,
                    "transition_delete": True,
                    "decision_identity": True,
                    "decision_delivery_allowed": True,
                },
                "reversed": {
                    "session_table_absent": True,
                    "reply_field_absent": True,
                    "quick_field_absent": True,
                },
            },
        )
