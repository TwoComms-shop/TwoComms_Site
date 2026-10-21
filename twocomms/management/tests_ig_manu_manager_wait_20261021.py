"""Native multi-turn manager waiting regressions; providers are stubbed."""
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import IgClient, IgCustomerTurn, IgFollowUpTask, IgTurnMessage
from management.services.ig_pending_question_context import (
    capture_pending_question_context, pending_question_context_current,
    pending_question_guidance,
)
from management.services.ig_product_inquiries import capture_product_inquiries, validate_inquiry_response
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import parse_structured_response
from management.services.ig_response_plan import build_response_plan, capture_response_plan
from management.services.ig_turn_revisions import create_collecting_revision


class ScopedManagerCoverageTests(SimpleTestCase):
    def response(self, text, manager=True):
        return parse_structured_response({"reply_text": text,
            "controls": [{"kind": "manager", "value": True}] if manager else []})

    def test_timing_handoff_does_not_cover_independent_known_price(self):
        plan = build_response_plan(preferences={},
            readiness={"has_product": True, "product": {"id": 55}},
            context=ReplyTruthContext(authorized_prices=(Decimal("600"),)),
            sources=[{"message_id": 8, "role": "user", "text": "What is the price? When will you ship?"}])
        coverage = plan.coverage(self.response("I will check shipping with our manager."))
        self.assertIn("8:info:price", coverage["remaining"])
        self.assertIn("8:info:dispatch_timing", coverage["remaining"])
        covered = plan.coverage(self.response("The price is 600 UAH. I will check shipping with our manager."))
        self.assertIn("8:info:price", covered["covered"])
        self.assertIn("8:info:dispatch_timing", covered["remaining"])

    def test_manager_flag_does_not_cover_fit_or_unrelated_unknown_question(self):
        for question in ("L. What is the fit?", "L. Can I collect it in person?"):
            plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
                sources=[{"message_id": 8, "role": "user", "text": question}])
            self.assertTrue(plan.coverage(self.response("I will check with our manager."))["remaining"])

    def test_only_exact_unknown_product_topic_can_be_acknowledged_as_handoff(self):
        sources = [{"message_id": 8, "role": "user", "text": "What material is this hoodie?", "source_digest": "a" * 64}]
        inquiry = capture_product_inquiries(sources,
            cart_capture={"active_line_id": "hoodie", "lines": [{"line_id": "hoodie", "product_id": 12}]},
            readiness_by_line={"hoodie": {"product": {"id": 12}}})
        plan = replace(build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
            sources=sources), product_information=inquiry)
        self.assertIn("8:info:material", plan.coverage(self.response("I will check the material with our manager."))["covered"])
        self.assertIn("8:info:material", plan.coverage(self.response("I will check the price with our manager."))["remaining"])
        changed = replace(plan, product_information={**inquiry, "inquiries": [
            {**row, "source_message_id": 9} for row in inquiry["inquiries"]]})
        self.assertIn("8:info:material", changed.coverage(self.response("I will check the material with our manager."))["remaining"])

    def test_unknown_line_never_gains_checkout_from_manager_flag(self):
        inquiry = {"status": "manager_required", "inquiries": [{"axis": "color"}], "resolved_information": []}
        reply = parse_structured_response({"reply_text": "I will check with our manager.",
            "controls": [{"kind": "manager", "value": True}, {"kind": "payment", "value": 600}]})
        self.assertEqual(validate_inquiry_response(inquiry, reply), "product_inquiry_checkout_unverified")

    def test_native_source_bound_16_17_32_retains_every_source_and_unknown_question(self):
        for count in (16, 17, 32):
            sources = [{"message_id": index + 1, "role": "user", "text":
                "What material is this hoodie?" if index == count - 1 else "Hello",
                "source_digest": "a" * 64} for index in range(count)]
            inquiry = capture_product_inquiries(sources)
            self.assertEqual(inquiry["status"], "manager_required")
            self.assertEqual(len(inquiry["source_refs"]), count)
            self.assertEqual(inquiry["inquiries"][-1]["source_message_id"], count)
            self.assertEqual(inquiry["inquiries"][-1]["requested_topic"], "material")

    def test_capture_gap_is_blocked_and_cannot_validate_as_ordinary_answer(self):
        sources = [{"message_id": index + 1, "role": "user", "text": "Hello"} for index in range(33)]
        inquiry = capture_product_inquiries(sources)
        self.assertEqual(inquiry["status"], "blocked")
        self.assertEqual(validate_inquiry_response(inquiry,
            self.response("The hardware is metal.", False)), "product_inquiry_source_bound")

    def test_many_unknown_topics_are_not_silently_truncated_to_32(self):
        sources = [{"message_id": index + 1, "role": "user",
            "text": "What material is this hoodie; what is its composition?", "source_digest": "a" * 64}
            for index in range(32)]
        inquiry = capture_product_inquiries(sources)
        self.assertEqual(inquiry["status"], "manager_required")
        self.assertEqual(len(inquiry["inquiries"]), 64)
        self.assertEqual({row["source_message_id"] for row in inquiry["inquiries"]}, set(range(1, 33)))

    def test_language_concern_requires_actual_reassurance(self):
        for question in ("Do you speak English?", "sorry, I dont speak the language"):
            plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(),
                sources=[{"message_id": 8, "role": "user", "text": question}])
            self.assertEqual([row["kind"] for row in plan.obligations], ["info:reply_language"])
            self.assertEqual(plan.coverage(self.response("Yes, I can reply in English.", False))["remaining"], [])
            self.assertEqual(plan.coverage(self.response("I will check with our manager."))["remaining"], ["8:info:reply_language"])

    def test_oversize_fit_question_does_not_repeat_confirmed_size(self):
        plan = build_response_plan(preferences={"values": {"size": "XXL"},
            "evidence": {"size": {"source_message_id": 8}}},
            readiness={"has_product": True, "product": {"id": 55}, "missing": ["fit"]},
            context=ReplyTruthContext(), sources=[{"message_id": 8, "role": "user", "text": "XXL"}])
        self.assertEqual(plan.next_selector, "fit")
        self.assertEqual(plan.validate(self.response(
            "You selected size XXL. Which fit would you prefer: classic or oversize?", False)), "")
        self.assertEqual(plan.validate(self.response("Which size would you prefer?", False)), "response_plan_repeated_selector")

    def test_required_measurement_capture_is_projected_once_without_losing_distinct_grids(self):
        capture = {"schema": "consultation-measurements.v1", "guides": [
            {"product_id": 111, "variant_id": 22, "grid_id": index + 1,
             "guide_version": str(index + 1), "fit_code": fit,
             "cells": {"XXL": {"chest_cm": width, "length_cm": 76}},
             "measurement_semantics": "garment_half_width", "unit": "cm",
             "notes": (fit + " measured on the garment; no body-size guarantee. ") * 30}
            for index, (fit, width) in enumerate((("classic", 62), ("oversize", 66)))]}
        plan = replace(build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext()),
            configuration={"consultation_measurements": capture, "source_clarifications": {"question": "which print?"},
                "reorder_observations": [{"classification": "reorder_clarification"}]})
        before, digest = plan.as_dict(), plan.digest
        projected = plan.prompt_projection()
        self.assertNotIn("consultation_measurements", projected["configuration"])
        self.assertNotIn("source_clarifications", projected["configuration"])
        self.assertNotIn("reorder_observations", projected["configuration"])
        self.assertEqual(projected["consultation_measurements"], capture)
        self.assertEqual(projected["pending_source_clarifications"], {"question": "which print?"})
        self.assertEqual(projected["reorder_resolution"], [{"classification": "reorder_clarification"}])
        self.assertEqual(plan.as_dict(), before)
        self.assertEqual(plan.digest, digest)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeManagerWaitTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle

    def _handoff(self):
        self.customer.language = "en"
        self.customer.save(update_fields=["language"])
        self._replace_bundle(["Can your manager check when you will ship?"])
        self.parsed = {"reply_text": "I will check dispatch timing with our manager.",
            "controls": [{"kind": "manager", "value": True}]}
        outcome, _, http = self._execute([(200, '{"message_id":"initial-handoff"}')])
        self.assertEqual(http.call_count, 1, outcome.reasons)
        self.task = IgFollowUpTask.objects.get(reason="revision_case:manager_handoff")
        self.old_revision = self.revision
        self.old_source = self.source
        return outcome

    def _fresh(self, text):
        self.source = self._message(text, f"fresh-{self.revision.pk}")
        self.turn = IgCustomerTurn.objects.create(client=self.customer,
            primary_source_message=self.source, window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()

    def test_fresh_status_ping_is_answered_without_duplicate_case_or_periodic_generation(self):
        self._handoff()
        count = IgFollowUpTask.objects.count()
        self._fresh("Any news?")
        context = capture_pending_question_context(self.revision)
        self.assertEqual(context["cases"][0]["task_id"], self.task.pk)
        self.assertTrue(pending_question_context_current(self.revision, context))
        self.assertLess(len(pending_question_guidance(context)), 1000)
        plan = capture_response_plan(self.customer, revision=self.revision)
        wrong_topic = parse_structured_response({"reply_text": "Your payment is pending.", "controls": []})
        self.assertIn(f"{self.source.pk}:info:manager_status", plan.coverage(wrong_topic)["remaining"])
        self.parsed = {"reply_text": "I don't have a confirmed update from our manager yet.", "controls": []}
        outcome, generate, http = self._execute([(200, '{"message_id":"status-answer"}')])
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(IgFollowUpTask.objects.count(), count)
        from management.services.ig_revision_live import process_pending_revisions
        with override_settings(IG_REVISION_EXECUTION_ENABLED=True,
                IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00"), \
                patch("management.services.instagram_bot.gemini_generate") as model, \
                patch("management.services.instagram_bot._provider_http") as send:
            process_pending_revisions(self.settings)
            process_pending_revisions(self.settings)
        model.assert_not_called()
        send.assert_not_called()

    def test_native_16_17_32_source_bursts_keep_unknown_product_handoff_and_known_question_debt(self):
        for count in (16, 17, 32):
            with self.subTest(source_count=count):
                self._fresh("Hello")
                self._replace_bundle(["Hello"] * (count - 1)
                    + ["What material is this hoodie? How much does it cost?"])
                self.parsed = {"reply_text": "I will check the material with our manager.",
                    "controls": [{"kind": "manager", "value": True}]}
                outcome, _, http = self._execute([(200, '{"message_id":"burst-' + str(count) + '"}')])
                self.assertEqual(http.call_count, 1, outcome.reasons)
                self.revision.refresh_from_db()
                receipt = self.revision.action_receipts["manager_handoff"]
                task = IgFollowUpTask.objects.get(pk=receipt["task_id"])
                self.assertEqual(task.reason, "revision_case:product_information_review")
                self.assertEqual(len(task.manager_context["sources"]), count)
                coverage = self.revision.action_receipts["response_coverage"]
                self.assertIn(f"{max(row['message_id'] for row in self.revision.bundle_snapshot['sources'])}:info:price", coverage["remaining"])

    def test_fresh_language_question_survives_open_manager_case(self):
        self._handoff()
        count = IgFollowUpTask.objects.count()
        self._fresh("sorry, I dont speak the language")
        self.parsed = {"reply_text": "Yes, I can reply in English.", "controls": []}
        outcome, _, http = self._execute([(200, '{"message_id":"english-answer"}')])
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(IgFollowUpTask.objects.count(), count)
        self.task.refresh_from_db()
        self.assertNotIn(self.task.status, {"completed", "cancelled"})

    def test_fresh_size_choice_is_captured_while_manager_question_stays_pending(self):
        self._handoff()
        self._fresh("L")
        self.parsed = {"reply_text": "You selected size L.", "controls": []}
        outcome, _, http = self._execute([(200, '{"message_id":"size-answer"}')])
        self.assertEqual(http.call_count, 1, outcome.reasons)
        from management.services.ig_commerce_projection import source_preferences_for
        preferences = source_preferences_for(self.customer)
        self.assertEqual(preferences["values"]["size"], "L")
        self.assertEqual(preferences["evidence"]["size"]["source_message_id"], self.source.pk)
        self.task.refresh_from_db()
        self.assertNotIn(self.task.status, {"completed", "cancelled"})

    def test_fresh_known_price_and_standard_timing_get_native_reply_beside_pending_case(self):
        self._handoff()
        from storefront.models import Category, Product
        from productcolors.models import Color, ProductColorVariant
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_revision_commerce import resolve_source_product_request
        category = Category.objects.create(name="Wait price", slug="wait-price")
        product = Product.objects.create(title="Reality Bends", slug="wait-priced-product",
            category=category, price=900, status="published")
        color = Color.objects.create(name="Wait price black", primary_hex="#111111")
        ProductColorVariant.objects.create(product=product, color=color, price_override=900, is_default=True)
        prior = self._message("Reality Bends", "wait-price-product")
        request = resolve_source_product_request(self.customer, prior, parse_turn(prior.text))
        apply_turn(self.customer, prior, request, reply_payload={})
        count = IgFollowUpTask.objects.count()
        self._fresh("What is the price? When will you ship?")
        self.parsed = {"reply_text": "The price is 900 UAH. Standard preparation for dispatch usually takes 1–3 days after payment.", "controls": []}
        outcome, _, http = self._execute([(200, '{"message_id":"price-timing-answer"}')])
        self.assertEqual(http.call_count, 1, outcome.reasons)
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        self.assertEqual(IgFollowUpTask.objects.count(), count)
        self.task.refresh_from_db()
        self.assertNotIn(self.task.status, {"completed", "cancelled"})

    def test_source_proven_other_episode_cannot_borrow_manager_status(self):
        self._handoff()
        self._fresh("Any news?")
        def scope(client, ids):
            return {"commercial_episode_id": 211 if self.old_source.pk in ids else 212}
        with patch("management.services.ig_turn_intent._scope_for_sources", side_effect=scope):
            self.assertEqual(capture_pending_question_context(self.revision), {})

    def test_malformed_case_is_not_ordinary_status_authority(self):
        self._handoff()
        self._fresh("Any news?")
        original = self.task.manager_context
        for change in ({"sources": ["not-a-source-reference"]}, {"latest_revision_id": "not-an-id"}):
            self.task.manager_context = {**original, **change}
            self.task.save(update_fields=["manager_context"])
            self.assertEqual(capture_pending_question_context(self.revision), {})

    def test_invalid_inquiry_capability_blocks_before_provider_or_customer_send(self):
        self._replace_bundle(["What material is this hoodie?"])
        self.parsed = {"reply_text": "The hardware is metal.", "controls": []}
        with patch("management.services.ig_product_inquiries.ProductInquiryCapture.recompute", return_value=None):
            outcome, generate, http = self._execute()
        self.assertEqual(outcome.state, "blocked")
        self.assertIn("product_inquiry_capture_invalid", outcome.reasons)
        generate.assert_not_called()
        http.assert_not_called()

    def test_context_abstains_on_changed_source_epoch_closed_case_and_reset(self):
        self._handoff()
        self._fresh("Any news?")
        self.assertTrue(capture_pending_question_context(self.revision))
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=self.source.pk):
            self.assertEqual(capture_pending_question_context(self.revision), {})
        self.task.status = "completed"
        self.task.save(update_fields=["status"])
        self.assertEqual(capture_pending_question_context(self.revision), {})
        self.task.status = "skipped"
        self.task.save(update_fields=["status"])
        self.old_source.text = "Changed source"
        self.old_source.save(update_fields=["text"])
        self.assertEqual(capture_pending_question_context(self.revision), {})
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1)
        self.assertEqual(capture_pending_question_context(self.revision), {})

    def test_case_closed_after_capture_blocks_stale_pending_status_before_send(self):
        self._handoff()
        self._fresh("Any news?")
        self.parsed = {"reply_text": "I don't have a confirmed update from our manager yet.", "controls": []}
        self.before_validate = lambda: IgFollowUpTask.objects.filter(pk=self.task.pk).update(status="completed")
        outcome, _, http = self._execute()
        self.assertEqual(outcome.state, "blocked")
        self.assertIn("pending_question_context_changed", outcome.reasons)
        http.assert_not_called()

    def test_explicit_pause_and_unknown_delivery_do_not_gain_send_permission(self):
        self._handoff()
        self._fresh("Any news?")
        IgClient.objects.filter(pk=self.customer.pk).update(bot_paused=True, manager_takeover=True)
        self.assertEqual(capture_pending_question_context(self.revision), {})
        self.parsed = {"reply_text": "I don't have a confirmed update yet.", "controls": []}
        _, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()

    def test_unknown_current_send_is_not_replayed(self):
        self._handoff()
        self._fresh("Any news?")
        self.parsed = {"reply_text": "I don't have a confirmed update from our manager yet.", "controls": []}
        outcome, _, http = self._execute([TimeoutError()])
        self.assertEqual(outcome.state, "delivery_pending")
        self.assertEqual(http.call_count, 1)
        _, generate, http = self._execute()
        generate.assert_not_called()
        http.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.get().state, "unknown")


class PendingQuestionNativeAdmissionTests(SimpleTestCase):
    def test_detached_or_unsaved_revision_cannot_discover_private_cases(self):
        from types import SimpleNamespace
        from management.models import IgCustomerTurnRevision
        from management.services.ig_revision_outbox import _digest

        bundle = {"sources": [{"message_id": 8, "role": "user", "text": "Any news?"}]}
        values = {"pk": 44, "client_id": 4, "bundle_snapshot": bundle,
                  "snapshot_digest": _digest(bundle), "permission_epoch": 0}
        for revision in (SimpleNamespace(**values), IgCustomerTurnRevision(**values)):
            with self.subTest(kind=type(revision).__name__):
                # SimpleTestCase forbids every database query in this path.
                self.assertEqual(capture_pending_question_context(revision), {})
