"""All current source positions survive capture/render as one mandatory view."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgClient, IgCommerceSelectionSession, IgFunnelResetAudit, InstagramBotMessage
from management.services.ig_admin_state_capture import current_admin_state
from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_response_plan import build_response_plan
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_turn_capture import capture_revision_context, detached_payload
from management.services.ig_turn_intelligence import (TurnContextError, build_turn_context, capture_digest,
    source_cart_current_facts, validate_source_cart_capture)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified")
class MultilineCapturedContextTests(TestCase):
    def setUp(self):
        from management.tests_ig_commerce_line_operations import CommerceLineOperationsTests
        CommerceLineOperationsTests.setUp(self)
        self.client_row = self.customer
        self.settings_row = SimpleNamespace(reply_permission_epoch=0, turn_intelligence_mode="unified",
            gemini_routing_mode="adaptive", pinned_chat_model="", pinned_until=None)
        self.publication = {"id": 1, "version": 1, "hash": "a" * 64}
        timing = patch("management.services.ig_revision_conversation_context.conversation_timing_guidance", return_value="")
        timing.start()
        self.addCleanup(timing.stop)

    def source(self, text, **kwargs):
        from management.tests_ig_commerce_line_operations import CommerceLineOperationsTests
        return CommerceLineOperationsTests.source(self, text, **kwargs)

    def reduce(self, text, **kwargs):
        from management.tests_ig_commerce_line_operations import CommerceLineOperationsTests
        return CommerceLineOperationsTests.reduce(self, text, **kwargs)

    def seal(self, *sources):
        from management.tests_ig_turn_capture import RevisionTurnCaptureTests
        return RevisionTurnCaptureTests.seal(self, *sources)

    def fixture(self):
        first, _ = self.reduce("добавьте чёрную футболку размер L")
        second, _ = self.reduce("добавьте худи размер M для друга")
        cart = capture_current_selection_lines(self.customer.pk, now=self.now)
        self.assertEqual(cart["status"], "captured", cart)
        self.assertEqual(len(cart["lines"]), 2)
        revision, collection, boundary = self.seal(first, second)
        active = cart["lines"][cart["active_index"]]["source_selection"]
        plan = build_response_plan(preferences=active, readiness={}, context=ReplyTruthContext(),
            sources=revision.bundle_snapshot["sources"])
        boundary.response_plan = replace(plan, source_selection=active, source_cart_capture=cart)
        return SimpleNamespace(first=first, second=second, cart=cart, revision=revision,
            collection=collection, boundary=boundary)

    def captured(self, fixture):
        return capture_revision_context(fixture.revision, generation_boundary=fixture.boundary,
            collection=fixture.collection, settings_row=self.settings_row,
            publication=self.publication, now=self.now)

    def state(self, context):
        components = detached_payload(context.components)
        active = components["source_selection"]
        return assemble_client_state(boundary=detached_payload(context.boundary), components={
            "source_selection": active["capture"], "source_selection_binding": active["scope"],
            "source_cart": components["source_cart"]["capture"]}, captured_at=self.now)

    def denied(self, fixture, reason):
        with self.assertRaises(TurnContextError) as caught:
            self.captured(fixture)
        self.assertEqual(caught.exception.reason, reason)

    def test_two_positions_reuse_exact_plan_capture_and_keep_distinct_sizes(self):
        fixture = self.fixture()
        with patch("management.services.ig_commerce_projection.capture_current_selection_lines",
                   side_effect=AssertionError("context must not recapture choices")), CaptureQueriesContext(connection) as queries:
            context = self.captured(fixture)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertIs(context.response_plan, fixture.boundary.response_plan)
        self.assertEqual(detached_payload(context.components["source_cart"]["capture"]), fixture.cart)
        self.assertEqual(context.boundary["source_cart_capture_digest"], fixture.cart["capture_digest"])
        self.assertIn("context:source_cart", context.context_blocks)
        state = self.state(context).as_dict()
        self.assertEqual([row["slots"]["choice.size"]["value"] for row in state["lines"]], ["L", "M"])
        self.assertEqual([row["recipient_id"] for row in state["lines"]], ["self", "friend"])
        self.assertEqual(state["slots"]["choice.size"]["value"], "M")
        self.assertEqual(state["source_cart"], fixture.cart)
        self.assertTrue(all(row["readiness"]["status"] == "unknown" for row in state["lines"]))
        self.assertTrue(all(row["slots"]["payment.current"]["status"] == "unknown" for row in state["lines"]))

    def test_all_line_prompt_is_mandatory_or_whole_refusal(self):
        context = self.captured(self.fixture())
        state = self.state(context)
        rendered = render_client_state_prompt(state, budget=2400)
        self.assertFalse(rendered.oversized)
        self.assertIn("current.source_cart", rendered.included)
        self.assertIn('"line_id"', rendered.text)
        denied = render_client_state_prompt(state, budget=1)
        self.assertTrue(denied.oversized)
        self.assertEqual(denied.text, "")
        self.assertIn(("current.source_cart", "mandatory_budget_exceeded"), denied.omitted)

    def test_builder_never_drops_cart_to_fit_optional_context(self):
        fixture = self.fixture()
        context = self.captured(fixture)
        plan_size = len(fixture.boundary.response_plan.prompt_guidance())
        with self.assertRaises(TurnContextError) as caught:
            build_turn_context(boundary=detached_payload(context.boundary),
                sources=fixture.revision.bundle_snapshot["sources"], captured_at=self.now,
                response_plan=fixture.boundary.response_plan, components=detached_payload(context.components),
                routing_policy={"mode": "unified", "context_chars": plan_size + 1})
        self.assertEqual(caught.exception.reason, "required_context_budget_exceeded")

    def test_black_to_pink_changes_only_target_and_old_black_stays_history(self):
        first, _ = self.reduce("добавьте чёрную футболку размер L")
        second, _ = self.reduce("добавьте худи размер M для друга")
        pink, _ = self.reduce("не чёрную, а розовую футболку")
        cart = capture_current_selection_lines(self.customer.pk, now=self.now)
        revision, collection, boundary = self.seal(first, second, pink)
        active = cart["lines"][cart["active_index"]]["source_selection"]
        plan = build_response_plan(preferences=active, readiness={}, context=ReplyTruthContext(),
            sources=revision.bundle_snapshot["sources"])
        boundary.response_plan = replace(plan, source_selection=active, source_cart_capture=cart)
        fixture = SimpleNamespace(cart=cart, revision=revision, collection=collection, boundary=boundary)
        context = self.captured(fixture)
        state = self.state(context).as_dict()
        self.assertEqual(state["lines"][0]["slots"]["choice.color"]["value"], "pink")
        self.assertEqual(state["lines"][0]["slots"]["choice.color"]["source_refs"][0]["id"], pink.pk)
        self.assertEqual(state["lines"][1]["slots"]["choice.size"]["value"], "M")
        self.assertEqual(state["lines"][1]["slots"]["choice.color"]["status"], "unknown")
        current_block = context.context_blocks["context:source_cart"]
        self.assertNotIn('"value":"black"', current_block)
        history = cart["lines"][0]["history"]
        self.assertEqual([row["value"] for row in history if row["field"] == "color"], ["pink", "black"])

    def test_nonactive_direct_mutation_after_plan_invalidates_head(self):
        fixture = self.fixture()
        session = IgCommerceSelectionSession.objects.get(pk=fixture.cart["session_id"])
        lines = deepcopy(session.lines)
        lines[0]["size"] = "XL"
        self.assertEqual(session.active_index, 1)
        IgCommerceSelectionSession.objects.filter(pk=session.pk).update(lines=lines)
        self.denied(fixture, "source_cart_head_changed")
        self.assertEqual(fixture.cart["lines"][0]["fields"]["size"]["value"], "L")

    def test_nonactive_source_text_change_after_plan_invalidates_source_fence(self):
        first, _ = self.reduce("добавьте чёрную футболку размер L")
        second, _ = self.reduce("добавьте худи размер M для друга")
        cart = capture_current_selection_lines(self.customer.pk, now=self.now)
        # Only the second position is part of this seal. The first position's
        # older source is still mandatory current choice proof, not history.
        revision, collection, boundary = self.seal(second)
        active = cart["lines"][cart["active_index"]]["source_selection"]
        plan = build_response_plan(preferences=active, readiness={}, context=ReplyTruthContext(),
            sources=revision.bundle_snapshot["sources"])
        boundary.response_plan = replace(plan, source_selection=active, source_cart_capture=cart)
        fixture = SimpleNamespace(cart=cart, revision=revision, collection=collection, boundary=boundary)
        InstagramBotMessage.objects.filter(pk=first.pk).update(text="Changed older nonactive source")
        self.denied(fixture, "source_cart_sources_changed")

    def test_new_nonactive_source_after_seal_cannot_enter_context(self):
        fixture = self.fixture()
        self.reduce("измените размер футболки на XL", at=self.now + timedelta(seconds=1))
        newer = capture_current_selection_lines(self.customer.pk, now=self.now + timedelta(seconds=2))
        fixture.boundary.response_plan = replace(fixture.boundary.response_plan, source_cart_capture=newer)
        self.denied(fixture, "source_cart_after_seal")

    def test_later_own_model_observation_changes_admin_header_without_changing_sealed_customer_cart(self):
        fixture = self.fixture()
        later = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace=self.namespace, role="model", source="echo", status="done", mid="multiline-later-own-model",
            text="Historical seller observation after this customer seal.", provider_created_at=self.now + timedelta(seconds=1))
        cart = capture_current_selection_lines(self.customer.pk, now=self.now + timedelta(seconds=2))
        self.assertEqual(cart["source_watermark"]["message_id"], fixture.second.pk)
        self.assertEqual(cart["capture_digest"], fixture.cart["capture_digest"])
        fixture.boundary.response_plan = replace(fixture.boundary.response_plan, source_cart_capture=cart)
        context = self.captured(fixture)
        self.assertEqual(context.boundary["watermark"]["message_id"], fixture.second.pk)
        self.assertNotIn(later.pk, context.metadata["captured_history_ids"])
        admin = current_admin_state(self.customer.pk, now=self.now + timedelta(seconds=2))
        self.assertEqual(admin.status, "captured", admin.as_dict())
        payload = admin.state.as_dict()
        self.assertEqual(payload["source_watermark"]["message_id"], later.pk)
        self.assertEqual(payload["source_cart"]["source_watermark"]["message_id"], fixture.second.pk)

    def test_reset_and_erasure_do_not_relabel_old_cart_as_current(self):
        fixture = self.fixture()
        reset = IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=fixture.second.pk)
        self.denied(fixture, "source_cart_scope_changed")
        reset.delete()
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=self.now)
        self.denied(fixture, "client_erasing")

    def test_missing_multiline_cart_is_finite_instead_of_active_only(self):
        fixture = self.fixture()
        fixture.boundary.response_plan = replace(fixture.boundary.response_plan, source_cart_capture={})
        self.denied(fixture, "source_cart_unavailable")

    def test_unavailable_multiline_cart_is_finite(self):
        fixture = self.fixture()
        fixture.boundary.response_plan = replace(fixture.boundary.response_plan, source_cart_capture={
            "schema": "source-selections.v1", "status": "unavailable", "coverage_complete": False,
            "lines": [], "reason": "selection_capture_query_bound"})
        self.denied(fixture, "source_cart_unavailable")

    def test_validator_rejects_forged_parent_and_digest_even_with_real_sources(self):
        fixture = self.fixture()
        context = self.captured(fixture)
        boundary = detached_payload(context.boundary)
        for mutate, reason in ((lambda row: row.update(capture_digest="b" * 64), "source_cart_digest_changed"),
                (lambda row: row["scope"].update(episode_id=99999), "source_cart_scope_changed")):
            value = deepcopy(fixture.cart)
            mutate(value)
            if reason != "source_cart_digest_changed":
                value["capture_digest"] = capture_digest({key: item for key, item in value.items() if key != "capture_digest"})
                boundary.pop("source_cart_capture_digest", None)
            with self.assertRaises(TurnContextError) as caught:
                validate_source_cart_capture(value, boundary)
            self.assertEqual(caught.exception.reason, reason)

    def test_state_card_refuses_inconsistent_cart_instead_of_showing_active_complete(self):
        fixture = self.fixture()
        context = self.captured(fixture)
        components = detached_payload(context.components)
        cart = components["source_cart"]["capture"]
        cart["lines"][0]["fields"]["size"]["value"] = "XL"
        state = assemble_client_state(boundary=detached_payload(context.boundary),
            components={"source_cart": cart}, captured_at=self.now).as_dict()
        self.assertEqual(state["status"], "unavailable")
        self.assertEqual(state["lines"], [])
        self.assertEqual(state["source_selection"], {})

    def test_previous_paid_order_never_supplies_current_line_size_or_payment(self):
        fixture = self.fixture()
        from orders.models import Order, OrderItem
        from management.models import IgOrderAttribution
        old = Order.objects.create(full_name="Old owner", phone="+380501234567", total_sum=790, payment_status="paid")
        OrderItem.objects.create(order=old, title="Previous size S", size="S", qty=1, unit_price=790, line_total=790, is_custom=True)
        IgOrderAttribution.objects.create(order=old, client=self.customer, creation_mode="manager_review", payment_source="unknown")
        IgClient.objects.filter(pk=self.customer.pk).update(purchases_count=1)
        context = self.captured(fixture)
        state = self.state(context).as_dict()
        self.assertEqual([row["slots"]["choice.size"]["value"] for row in state["lines"]], ["L", "M"])
        self.assertIsNone(state["scope"]["order_id"])
        self.assertEqual(state["slots"]["payment.current"]["status"], "unknown")
        self.assertNotIn("Previous size S", context.turn_note)

    def test_admin_capture_once_preserves_all_lines_and_64_select_zero_dml(self):
        fixture = self.fixture()
        with patch("management.services.ig_commerce_projection.capture_current_selection_lines",
                wraps=capture_current_selection_lines) as canonical, CaptureQueriesContext(connection) as queries:
            result = current_admin_state(self.customer.pk, now=self.now)
        self.assertEqual(result.status, "captured", result.as_dict())
        self.assertEqual(canonical.call_count, 1)
        self.assertLessEqual(result.read_queries, 64)
        self.assertFalse(any(row["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"} for row in queries))
        state = result.state.as_dict()
        self.assertEqual([row["slots"]["choice.size"]["value"] for row in state["lines"]], ["L", "M"])
        self.assertEqual(state["source_cart"]["capture_digest"], fixture.cart["capture_digest"])
        self.assertEqual(state["boundary"]["size_correction_context"]["context"]["value"], "M")

    def test_admin_same_connection_writer_is_rejected_and_source_is_unchanged(self):
        fixture = self.fixture()
        original_text = fixture.first.text
        from management.services.ig_checkout_readiness import selection_readiness
        def mutate(**kwargs):
            result = selection_readiness(**kwargs)
            InstagramBotMessage.objects.filter(pk=fixture.first.pk).update(text="Changed nonactive source")
            return result
        with patch("management.services.ig_checkout_readiness.selection_readiness", side_effect=mutate):
            result = current_admin_state(self.customer.pk, now=self.now)
        self.assertEqual(result.reason, "state_read_side_effect_rejected")
        fixture.first.refresh_from_db()
        self.assertEqual(fixture.first.text, original_text)
        self.assertTrue(IgClient.objects.filter(pk=self.customer.pk).exists(), "read guard must not poison caller transaction")

    def test_actual_sealed_factory_keeps_receipt_agreement_and_all_lines_without_payment_confirmation(self):
        from management.models import IgCommercialEpisode, IgDeal, IgPaymentConfirmationReview
        from management.services.ig_conversation_agreement import persist_conversation_agreement
        from management.services.ig_turn_integration import prepare_revision_turn_context
        first, _ = self.reduce("добавьте чёрную футболку размер L")
        second, _ = self.reduce("добавьте худи размер M для друга")
        seller = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            provider_namespace=self.namespace, role="manager", source="echo", status="done", mid="multiline-seller",
            text="Футболка Fixture L біла оверсайз. 1820 + доставка 80 = 1900 грн.", provider_created_at=self.now)
        accepted, _ = self.reduce("Так", at=self.now)
        agreement = persist_conversation_agreement(self.customer, [first, second, seller, accepted], accepted.pk)
        self.assertTrue(agreement["persisted"], agreement)
        receipt = self.source("Fixture receipt", at=self.now)
        binding = {"source_message_id": receipt.pk, "source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64}
        receipt.attachment_media = [{**binding, "type": "image", "status": "owned", "private_storage": True,
            "receipt_inspection": {**binding, "schema_version": "ig-receipt-inspection-v1", "state": "inspected",
                "role": "receipt", "confidence": .99, "provider_model": "offline-fixture", "request_id": "fixture-receipt",
                "receipt_facts": {"amount": "1900.00", "currency": "UAH", "payment_status": "completed"}}}]
        receipt.private_media_state = "active"
        receipt.private_media_delete_after = self.now + timedelta(hours=1)
        receipt.save(update_fields=["attachment_media", "private_media_state", "private_media_delete_after"])
        episode = IgCommercialEpisode.objects.get(pk=self.customer.current_commercial_episode_id)
        deal = IgDeal.objects.create(client=self.customer, amount="1900.00")
        review = IgPaymentConfirmationReview.objects.create(client=self.customer, deal=deal,
            dedupe_key="multiline-pending-receipt-review", status="pending",
            evidence={"order_draft": {"quoted_total": "1820.00"}})
        episode.deal, episode.primary_payment_review = deal, review
        episode.save(update_fields=["deal", "primary_payment_review"])
        revision, collection, boundary = self.seal(receipt)
        cart = capture_current_selection_lines(self.customer.pk, now=self.now)
        self.assertEqual(cart["status"], "captured", cart)
        active = cart["lines"][cart["active_index"]]["source_selection"]
        plan = build_response_plan(preferences=active, readiness={}, context=ReplyTruthContext(),
            sources=revision.bundle_snapshot["sources"])
        boundary.response_plan = replace(plan, source_selection=active, source_cart_capture=cart)
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            prepared = prepare_revision_turn_context(revision, generation_boundary=boundary, collection=collection,
                settings_row=self.settings_row, publication=self.publication, now=self.now)
        provider.assert_not_called()
        state = prepared.state.as_dict()
        self.assertEqual([row["slots"]["choice.size"]["value"] for row in state["lines"]], ["L", "M"])
        self.assertEqual(state["slots"]["conversation.agreement"]["value"]["amounts"]["payable_total"], "1900.00")
        self.assertEqual(state["slots"]["receipt.observation"]["value"]["receipts"][0]["receipt_facts"]["amount"], "1900.00")
        self.assertEqual(state["slots"]["receipt.observation"]["authority"], "typed_analysis")
        self.assertEqual(state["slots"]["payment.current"]["authority"], "derived")
        self.assertEqual(state["slots"]["payment.current"]["snapshot_state"], "unverified_payment")
        self.assertEqual(state["slots"]["payment.current"]["value"]["confirmed_paid_amount"], "0.00")
        self.assertEqual(prepared.request_metadata["view_versions"]["receipt_observation"], "payment-observation.v1")
        self.assertNotIn("1900.00", str(prepared.request_metadata))


@skipUnless(connection.vendor == "mysql", "requires disposable MariaDB independent source writer")
@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified")
class MultilineSourceFenceNativeTests(TransactionTestCase):
    """Committed fixtures and a different session mutate during the real GET.

    The reader's read-only wrapper stays in force. The writer never uses that
    connection and commits before the reader performs its final source fence.
    """
    setUp = MultilineCapturedContextTests.setUp
    source = MultilineCapturedContextTests.source
    reduce = MultilineCapturedContextTests.reduce
    seal = MultilineCapturedContextTests.seal
    fixture = MultilineCapturedContextTests.fixture
    WAIT_SECONDS = 5
    JOIN_SECONDS = 10

    def interleaved_source_write(self, source, changes):
        self.assertTrue(connection.mysql_is_mariadb)
        self.assertRegex(str(connection.settings_dict.get("NAME") or ""), r"^test_twocomms_[A-Za-z0-9_]+$")
        self.assertFalse(connection.in_atomic_block, "fixtures must be committed for the independent native writer")
        self.assertTrue(connection.get_autocommit())
        transaction.commit()
        with connection.cursor() as cursor:
            cursor.execute("SELECT CONNECTION_ID()")
            reader_id = int(cursor.fetchone()[0])
        connected, requested, committed, abort = Event(), Event(), Event(), Event()
        ready, outcomes = Queue(), Queue()

        def writer():
            writer_id = None
            try:
                connections.close_all()
                writer_connection = connections["default"]
                writer_connection.ensure_connection()
                with writer_connection.cursor() as cursor:
                    cursor.execute("SELECT CONNECTION_ID()")
                    writer_id = int(cursor.fetchone()[0])
                ready.put(writer_id)
                connected.set()
                if not requested.wait(self.WAIT_SECONDS):
                    raise AssertionError("protected catalog read did not request the interleave")
                if abort.is_set():
                    return
                with transaction.atomic():
                    changed = InstagramBotMessage.objects.filter(pk=source.pk).update(**changes)
                # Notify only after the actual transaction has committed.
                outcomes.put({"writer_id": writer_id, "changed": changed, "error": None})
            except BaseException as exc:
                outcomes.put({"writer_id": writer_id, "error": exc})
            finally:
                connected.set()
                committed.set()
                connections.close_all()

        worker = Thread(target=writer, name="multiline-source-fence-writer", daemon=True)
        worker.start()
        from management.services.ig_checkout_readiness import selection_readiness

        def catalog_read(**kwargs):
            value = selection_readiness(**kwargs)
            if not requested.is_set():
                requested.set()
                self.assertTrue(committed.wait(self.WAIT_SECONDS), "independent writer did not commit during protected GET")
                outcome = outcomes.get_nowait()
                if outcome["error"] is not None:
                    raise outcome["error"]
                self.assertEqual(outcome["changed"], 1)
                self.assertNotEqual(outcome["writer_id"], reader_id)
            return value

        try:
            self.assertTrue(connected.wait(self.WAIT_SECONDS), "independent writer did not connect")
            self.assertFalse(ready.empty(), "independent writer failed before opening a session")
            self.assertNotEqual(ready.get_nowait(), reader_id)
            with patch("management.services.ig_checkout_readiness.selection_readiness", side_effect=catalog_read), \
                    CaptureQueriesContext(connection) as reader_queries:
                result = current_admin_state(self.customer.pk, now=self.now)
            self.assertTrue(requested.is_set(), "the real protected catalog read must reach the interleave")
            self.assertEqual((result.status, result.reason), ("conflict", "current_source_changed"))
            self.assertFalse(result.state.as_dict()["slots"])
            self.assertFalse(any(row["sql"].lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}
                for row in reader_queries), "the protected GET itself must remain read-only")
            self.assertLessEqual(result.read_queries, 64)
            source.refresh_from_db()
            for field, value in changes.items():
                self.assertEqual(getattr(source, field), value, "the independent writer must really change stored source evidence")
        finally:
            abort.set()
            requested.set()
            worker.join(self.JOIN_SECONDS)
            self.assertFalse(worker.is_alive(), "source writer connection must be closed before fixture cleanup")

    def test_admin_nonactive_source_mutation_during_catalog_read_discards_cart(self):
        fixture = self.fixture()
        self.interleaved_source_write(fixture.first, {"text": "Changed nonactive source"})

    def test_admin_supporting_noop_source_is_in_final_complete_fence(self):
        self.fixture()
        noop, _ = self.reduce("Гаразд")
        captured = capture_current_selection_lines(self.customer.pk, now=self.now)
        self.assertIn(noop.pk, captured["fence"]["source_ids"])
        self.assertFalse(any(proof.get("source_message_id") == noop.pk
            for line in captured["lines"] for proof in line["evidence"].values()))
        self.interleaved_source_write(noop, {"quick_reply_payload": "mutated-supporting-source"})


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class MultilineActiveCorrectionCompatibilityTests(TestCase):
    def setUp(self):
        from management.tests_ig_selection_corrections import SizeCorrectionTests
        SizeCorrectionTests.setUp(self)

    def message(self, text):
        from management.tests_ig_selection_corrections import SizeCorrectionTests
        return SizeCorrectionTests.message(self, text)

    def test_same_capture_preserves_size_correction_context_and_clear_receipt(self):
        from management.services.ig_selection_corrections import save_size_correction
        before = current_admin_state(self.row.pk, now=self.now)
        context = before.state.as_dict()["boundary"]["size_correction_context"]
        self.assertTrue(context["available"], before.as_dict())
        corrected = save_size_correction(self.row.pk, actor=self.actor, operation_id=uuid.uuid4(),
            expected_selection_revision=context["context"]["selection_revision"],
            expected_context_digest=context["context_digest"], operation="clear", value=None, now=self.now)
        self.assertIsNotNone(corrected)
        after = current_admin_state(self.row.pk, now=self.now)
        self.assertEqual(after.status, "captured", after.as_dict())
        state = after.state.as_dict()
        self.assertEqual(state["slots"]["choice.size"]["authority"], "audited_correction")
        self.assertEqual(state["slots"]["choice.size"]["omission_reason"], "requirement_explicitly_cleared")
        self.assertEqual(state["lines"][0]["slots"]["choice.size"], state["slots"]["choice.size"])
        self.assertTrue(state["boundary"]["size_correction_context"]["available"])
        self.source.refresh_from_db()
        self.assertEqual(self.source.text, "Хочу футболку розмір M")


class MultilinePromptBudgetTests(SimpleTestCase):
    """Full schema-valid proof stays captured; only presentation is compact."""
    def fixture(self, count=8):
        from management.tests_ig_turn_intelligence import capture
        args = capture(*("Дякую!" for _ in range(count)))
        boundary = args["boundary"]
        scope = {key: boundary[key] for key in
            ("client_id", "episode_id", "order_id", "reset_id", "reset_floor", "source_namespace")}
        lines = []
        for index, source in enumerate(args["sources"]):
            line_id = str(uuid.UUID(int=index + 1))
            recipient = "self" if index == 0 else "friend-" + str(index)
            own = {**scope, "line_id": line_id, "recipient_id": recipient,
                "session_id": 11, "generation": 2, "revision": 4, "active_index": index}
            values = {"product_id": 37 + index, "model_query": "Принт " + str(index),
                "garment_type": "tshirt" if index % 2 == 0 else "hoodie",
                "size": "L" if index % 2 == 0 else "M", "fit_option_code": "regular",
                "color": "pink" if index == 0 else "black", "quantity": 1, "purchase_requested": True}
            proof = {"source_message_id": source["message_id"],
                "source_digest": hashlib.sha256(source["text"].encode()).hexdigest(),
                "observed_at": source["provider_created_at"], "decision_id": 300 + index,
                "transition_id": 400 + index, "authority": "customer_source"}
            fields = {key: {"value": value, "status": "ambiguous" if key == "model_query" else "confirmed",
                "authority": "customer_source", "source": deepcopy(proof)} for key, value in values.items()}
            selection = {"schema": "source-selection.v1", "scope": own, "revision": 4,
                "values": deepcopy(values), "fields": deepcopy(fields),
                "evidence": {key: deepcopy(proof) for key in values}}
            lines.append({"line_id": line_id, "recipient_id": recipient, "index": index,
                "fields": fields, "source_selection": selection,
                "defaults": {"quantity": {"value": 1, "authority": "existing_cart_default", "source_confirmed": False}},
                "history": [{"field": "color", "value": "old-colour-" + str(n), **deepcopy(proof)}
                    for n in range(8)]})
        cart = {"schema": "source-selections.v1", "status": "captured", "reason": "", "scope": scope,
            "session_id": 11, "generation": 2, "selection_revision": 4, "active_index": 0,
            "active_line_id": lines[0]["line_id"], "lines": lines, "omissions": [], "coverage_complete": True,
            "line_limit": 16, "transition_limit": 64, "query_limit": 64,
            "source_watermark": deepcopy(boundary["watermark"]),
            "fence": {"namespace": scope["source_namespace"], "source_watermark": deepcopy(boundary["watermark"]),
                "source_ids": list(boundary["source_ids"]), "source_digest": "a" * 64,
                "owner_digest": "b" * 64, "snapshot_digest": "c" * 64}}
        cart["capture_digest"] = capture_digest(cart)
        boundary.update(line_id=lines[0]["line_id"], recipient_id="self", selection_session_id=11,
            selection_generation=2, selection_revision=4, source_cart_capture_digest=cart["capture_digest"],
            source_watermark=deepcopy(boundary["watermark"]))
        validate_source_cart_capture(cart, boundary)
        return args, cart

    def state(self, args, cart):
        active = cart["lines"][cart["active_index"]]["source_selection"]
        state = assemble_client_state(boundary=args["boundary"], components={"source_selection": active,
            "source_cart": cart}, captured_at=args["captured_at"])
        self.assertEqual(state.as_dict()["status"], "captured", state.as_dict()["omissions"])
        return state

    def test_eight_positions_keep_all_values_and_exact_evidence_within_state_budget(self):
        args, cart = self.fixture()
        original = deepcopy(cart)
        state = self.state(args, cart)
        rendered = render_client_state_prompt(state, budget=2400)
        self.assertFalse(rendered.oversized, rendered.estimated_tokens)
        self.assertLessEqual(rendered.estimated_tokens, 2400)
        block = next(json.loads(line) for line in rendered.text.splitlines()
            if line.startswith("{") and json.loads(line).get("slot") == "current.source_cart")
        facts = block["value"]
        self.assertEqual(len(facts["lines"]), 8)
        self.assertEqual(facts["scope"], cart["scope"])
        self.assertEqual(facts["source_watermark"], cart["source_watermark"])
        self.assertEqual(facts["capture_digest"], cart["capture_digest"])
        for projected, row in zip(facts["lines"], cart["lines"]):
            self.assertEqual((projected["line_id"], projected["recipient_id"]), (row["line_id"], row["recipient_id"]))
            self.assertEqual(len(projected["values"]), len(facts["field_columns"]))
            self.assertEqual({name: projected["values"][facts["field_columns"].index(name)]
                for name in row["source_selection"]["values"]}, row["source_selection"]["values"])
            bound = {}
            for group in projected["field_evidence"]:
                for field_index in group["fields"]:
                    name = facts["field_columns"][field_index]
                    self.assertNotIn(name, bound)
                    bound[name] = group
            self.assertEqual(set(bound), set(row["fields"]))
            for name, field in row["fields"].items():
                proof = field["source"]
                self.assertEqual((bound[name]["status"], bound[name]["authority"]), (field["status"], field["authority"]))
                source_ref = facts["source_refs"][bound[name]["source_ref"]]
                for key in ("source_message_id", "source_digest", "observed_at"):
                    self.assertEqual(source_ref[key], proof[key])
            self.assertEqual(facts["default_groups"][projected["default_ref"]], row["defaults"])
            self.assertFalse(facts["default_groups"][projected["default_ref"]]["quantity"]["source_confirmed"])
            self.assertEqual(projected["readiness"], "unknown")
        self.assertNotIn("old-colour-", rendered.text)
        self.assertEqual(cart, original)
        self.assertEqual(state.as_dict()["source_cart"], original)
        self.assertEqual(rendered.capture_digest, state.digest)

    def test_actual_eight_line_response_plan_and_all_facts_fit_context_cap_without_changing_artifact(self):
        args, cart = self.fixture()
        active = cart["lines"][0]["source_selection"]
        plan = build_response_plan(preferences=active, readiness={}, context=ReplyTruthContext(),
            sources=args["sources"], captured_cart=cart)
        self.assertEqual(len(plan.line_plans), 8)
        self.assertEqual(sum(len(row.as_dict()["obligations"]) for row in plan.line_plans), 64)
        original_plan, original_digest = plan.as_dict(), plan.digest
        def decoded_obligations(projection):
            return [(identity, kind, group["line_id"], group["source_message_id"])
                for group in projection["obligation_groups"] for identity, kind in
                ([(group["id_prefix"] + kind, kind) for kind in group["obligation_kinds"]]
                    if "obligation_kinds" in group else group["obligations"])]
        expected = [(item["id"], item["kind"], item.get("line_id"), item["source_message_id"])
            for item in plan.obligations]
        self.assertEqual(decoded_obligations(plan.prompt_projection()), expected)
        self.assertEqual(len(expected), 64)
        noncanonical = deepcopy(plan.obligations)
        noncanonical[0]["id"] = "opaque-original-obligation-id"
        fallback_plan = replace(plan, obligations=noncanonical)
        self.assertEqual(decoded_obligations(fallback_plan.prompt_projection()),
            [(item["id"], item["kind"], item.get("line_id"), item["source_message_id"])
                for item in noncanonical])
        self.assertEqual(plan.as_dict(), original_plan)
        context = build_turn_context(**args, response_plan=plan,
            components={"source_cart": {"scope": {key: args["boundary"][key] for key in
                ("client_id", "source_namespace", "reset_id", "reset_floor", "erasure_epoch",
                 "episode_id", "line_id", "recipient_id")}, "capture": cart}})
        self.assertIs(context.response_plan, plan)
        self.assertLessEqual(context.metadata["budget"]["selected_chars"], 12000)
        self.assertIn("context:response_plan", context.context_blocks)
        self.assertIn("context:source_cart", context.context_blocks)
        self.assertEqual(detached_payload(context.components["source_cart"]["capture"]), cart)
        self.assertEqual(plan.as_dict(), original_plan)
        self.assertEqual(plan.digest, original_digest)
        self.assertEqual(len(source_cart_current_facts(cart)["lines"]), 8)

    def test_oversized_nonactive_requirement_refuses_whole_prompt_and_keeps_full_original(self):
        args, cart = self.fixture()
        huge = "complete customer model requirement " * 1000
        row = cart["lines"][-1]
        row["fields"]["model_query"]["value"] = huge
        row["source_selection"]["fields"]["model_query"]["value"] = huge
        row["source_selection"]["values"]["model_query"] = huge
        cart["capture_digest"] = capture_digest({key: value for key, value in cart.items() if key != "capture_digest"})
        args["boundary"]["source_cart_capture_digest"] = cart["capture_digest"]
        state = self.state(args, cart)
        rendered = render_client_state_prompt(state, budget=2400)
        self.assertTrue(rendered.oversized)
        self.assertEqual((rendered.text, rendered.included), ("", ()))
        self.assertIn(("current.source_cart", "mandatory_budget_exceeded"), rendered.omitted)
        self.assertEqual(state.as_dict()["source_cart"]["lines"][-1]["fields"]["model_query"]["value"], huge)
