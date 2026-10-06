"""Repeat purchases use exact history; neutral replies create no draft cycle."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import os
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone

from management.models import (
    IgClient, IgCommercialEpisode, IgCommerceSelectionSession,
    IgCommerceSelectionTransition, IgCommerceTurnDecision, IgOrderAttribution,
    InstagramBotMessage, InstagramBotSettings, IgTurnMessage,
)
from management.services.ig_commerce_state import apply_turn, CommerceNonMutation, CommerceRevisionConflict
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_revision_commerce import reduce_inbound_commerce_source, reduce_revision_commerce
from management.services.ig_revision_outbox import PublicationBinding
from management.services.instagram_bot import ingress_provider_namespace, _persist_commerce_turn
from orders.models import Order, OrderItem


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class HistoricalReorderTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        from management.tests_ig_checkout_episode_binding import CheckoutCurrentEpisodeBindingTests
        CheckoutCurrentEpisodeBindingTests.setUp(self)
        self.settings = InstagramBotSettings.objects.create(pk=1, ig_user_id="reorder-owner")
        self.namespace = ingress_provider_namespace(self.settings)
        self.variant.stock = 10
        self.variant.save(update_fields=["stock"])
        self.customer = self.client_row
        self.ordinal = 0

    def source(self, text):
        self.ordinal += 1
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", text=text, mid="reorder-source-" + str(self.ordinal),
            provider_namespace=self.namespace, provider_created_at=timezone.now() - timedelta(minutes=2) + timedelta(seconds=self.ordinal))

    def order(self, number="OLD-1", *, paid=True, count=1):
        order = Order.objects.create(order_number=number, full_name="Old owner", phone="380501234567",
            payment_status="paid" if paid else "unpaid", status="done", total_sum=900 * count)
        for index in range(count):
            OrderItem.objects.create(order=order, product=self.product, color_variant=self.variant,
                size="M" if index == 0 else "L", fit_option_code=self.fit.code, option_values={},
                qty=1, unit_price=900, line_total=900)
        IgOrderAttribution.objects.create(client=self.customer, order=order,
            creation_mode="manager_review", payment_source="unknown")
        return order

    def counts(self):
        return tuple(model.objects.filter(client=self.customer).count() if model in (
            IgCommercialEpisode, IgCommerceSelectionSession) else model.objects.count()
            for model in (IgCommercialEpisode, IgCommerceSelectionSession, IgCommerceSelectionTransition, IgCommerceTurnDecision))

    def test_neutral_acknowledgement_has_no_commercial_rows_and_legacy_returns_no_decision(self):
        self.order()
        before = self.counts()
        source = self.source("Дякую! 👍")
        with self.assertRaises(CommerceNonMutation) as caught:
            apply_turn(self.customer, source, parse_turn(source.text))
        self.assertEqual(caught.exception.observation["classification"], "neutral")
        request, decision = _persist_commerce_turn(source)
        self.assertIsNotNone(request)
        self.assertIsNone(decision)
        self.assertEqual(self.counts(), before)
        self.http.assert_not_called()

    def test_neutral_rejects_forged_generated_commerce_hint(self):
        source = self.source("Спасибo!".replace("o", "о"))
        with self.assertRaises(CommerceRevisionConflict):
            apply_turn(self.customer, source, replace(parse_turn(source.text), field_updates={"size": "XL"}))
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_unique_owned_history_starts_fresh_source_cycle_and_replay_creates_nothing(self):
        order = self.order()
        source = self.source("Хочу ещё такую же")
        request = parse_turn(source.text)
        self.assertTrue(request.line_operations[0].copy_previous)
        decision = apply_turn(self.customer, source, request)
        self.assertTrue(decision.accepted, decision.result_payload)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_commercial_episode.intended_order_id)
        capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(capture["status"], "captured", capture)
        line = capture["lines"][0]
        self.assertEqual(line["fields"]["size"]["value"], "M")
        self.assertEqual(line["fields"]["size"]["authority"], "validated_selection_action")
        self.assertEqual(line["fields"]["size"]["source"]["source_message_id"], source.pk)
        self.assertIsNone(capture["scope"]["order_id"])
        before = self.counts()
        self.assertEqual(apply_turn(self.customer, source, request).pk, decision.pk)
        self.assertEqual(self.counts(), before)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.http.assert_not_called()

    def test_multiple_orders_or_items_require_explicit_reference_before_new_cycle(self):
        self.order("OLD-1")
        self.order("OLD-2", count=2)
        for text, reason in (("Ещё такую же", "reorder_order_ambiguous"),
            ("Ещё такую же из заказа OLD-2", "reorder_item_ambiguous")):
            source = self.source(text)
            before = self.counts()
            with self.assertRaises(CommerceNonMutation) as caught:
                apply_turn(self.customer, source, parse_turn(text))
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(caught.exception.observation["classification"], "reorder_clarification")
            self.assertEqual(self.counts(), before)
        source = self.source("Ещё такую же из заказа OLD-2, позиция 2")
        decision = apply_turn(self.customer, source, parse_turn(source.text))
        self.assertTrue(decision.accepted, decision.result_payload)
        self.assertEqual(decision.session.lines[0]["size"], "L")

    def test_unpaid_missing_and_foreign_order_never_materialize_a_repeat(self):
        self.order(paid=False)
        for text in ("Ещё такую же", "Ещё такую же из заказа FOREIGN-99"):
            source = self.source(text)
            with self.assertRaises(CommerceNonMutation):
                apply_turn(self.customer, source, parse_turn(text))
            self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_neutral_keeps_existing_draft_and_all_proven_choices(self):
        source = self.source("добавьте чёрную футболку размер L")
        decision = apply_turn(self.customer, source, parse_turn(source.text))
        before = deepcopy(decision.session.snapshot())
        counts = self.counts()
        neutral = self.source("👍")
        with self.assertRaises(CommerceNonMutation):
            apply_turn(self.customer, neutral, parse_turn(neutral.text))
        decision.session.refresh_from_db()
        self.assertEqual(decision.session.snapshot(), before)
        self.assertEqual(self.counts(), counts)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class NonMutationRevisionReceiptTests(TransactionTestCase):
    def setUp(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        self.fixture = RevisionLiveTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture._replace_bundle(["Дякую!", "👍"])

    def counts(self):
        return tuple(model.objects.count() for model in (IgCommercialEpisode, IgCommerceSelectionSession,
            IgCommerceSelectionTransition, IgCommerceTurnDecision))

    def test_ingress_and_sealed_replay_keep_exact_observations_without_commercial_entities(self):
        fixture = self.fixture
        before = self.counts()
        for source in IgTurnMessage.objects.filter(turn=fixture.turn).select_related("message").order_by("ordinal"):
            with transaction.atomic():
                result = reduce_inbound_commerce_source(fixture.customer, source.message,
                    expected_provider_namespace=source.message.provider_namespace)
            self.assertTrue(result.ready, result.reason)
            self.assertEqual(len(result.observations), 1)
        self.assertEqual(self.counts(), before)
        kwargs = {"settings_id": fixture.settings.pk, "settings_permission_epoch": fixture.settings.reply_permission_epoch,
            "publication": PublicationBinding(fixture.publication.pk, fixture.publication.version, fixture.publication.snapshot_hash)}
        first = reduce_revision_commerce(fixture.revision.pk, fixture.token, **kwargs)
        self.assertTrue(first.ready, first.reason)
        self.assertFalse(first.decisions)
        self.assertEqual(len(first.observations), 2)
        again = reduce_revision_commerce(fixture.revision.pk, fixture.token, **kwargs)
        self.assertTrue(again.replayed, again.reason)
        self.assertEqual(again.observations, first.observations)
        self.assertEqual(self.counts(), before)
        fixture.revision.refresh_from_db()
        receipt = fixture.revision.action_receipts["commerce_reduction"]
        self.assertIsNone(receipt["scope_binding"]["terminal_session_id"])
        self.assertIsNone(receipt["scope_binding"]["terminal_episode_id"])


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class BoundHistoricalReorderTests(TestCase):
    def setUp(self):
        from management.tests_ig_revision_multiline_checkout import RevisionMultilineCheckoutTests
        self.fixture = RevisionMultilineCheckoutTests(methodName="runTest")
        self.fixture.client = self.client
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.customer = self.fixture.customer

    def paid_order(self):
        from management.tests_ig_hosted_settlement import HostedSettlementFixture
        from storefront.views.monobank import _apply_payment_attempt_status
        fixture = self.fixture
        control, reasons = fixture.control()
        self.assertFalse(reasons, reasons)
        proposal = fixture.quote(control)
        with patch("management.services.ig_lifecycle.dispatch_lifecycle_event", return_value="pending"):
            attempt = HostedSettlementFixture.invoice(fixture, proposal, "reorder-bound-invoice")
            payload = {"invoiceId": attempt.monobank_invoice_id, "reference": attempt.reference, "ccy": 980,
                "status": "success", "amount": int(attempt.payment_amount * 100),
                "finalAmount": int(attempt.payment_amount * 100), "modifiedDate": timezone.now().isoformat()}
            order, created = _apply_payment_attempt_status(attempt, "success", payload=payload, source="provider_pull")
        self.assertTrue(created)
        order.order_number = "BOUND-1"
        order.save(update_fields=["order_number"])
        self.customer.refresh_from_db()
        return order, proposal

    def source(self, text):
        fixture = self.fixture
        fixture.ordinal += 1
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", text=text, mid="bound-reorder-" + str(fixture.ordinal),
            provider_namespace=fixture.namespace, provider_created_at=timezone.now())

    def test_exact_owned_paid_item_carries_proven_source_without_old_payment_or_order(self):
        order, proposal = self.paid_order()
        original = deepcopy(order.payment_payload["source_cart_provenance"])
        old_episode = self.customer.current_commercial_episode_id
        source = self.source("Ещё такую же из заказа BOUND-1, позиция 1")
        decision = apply_turn(self.customer, source, parse_turn(source.text))
        self.assertTrue(decision.accepted, decision.result_payload)
        self.customer.refresh_from_db()
        self.assertNotEqual(self.customer.current_commercial_episode_id, old_episode)
        self.assertIsNone(self.customer.current_commercial_episode.intended_order_id)
        self.assertIsNone(self.customer.current_commercial_episode.deal_id)
        capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(capture["status"], "captured", capture)
        line = capture["lines"][0]
        self.assertEqual(line["fields"]["size"]["value"], "L")
        self.assertEqual(line["fields"]["product_id"]["value"], self.fixture.products[0].pk)
        self.assertEqual(line["fields"]["size"]["source"]["source_message_id"], source.pk)
        self.assertEqual(line["fields"]["size"]["source"]["copied_from"]["proof"]["source_message_id"],
            original["binding"]["lines"][0]["evidence"]["size"]["source_message_id"])
        order.refresh_from_db()
        self.assertEqual(order.payment_payload["source_cart_provenance"], original)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "paid")
        self.fixture.http.assert_not_called()


    def test_changed_old_configuration_does_not_create_new_cycle_or_fake_old_source(self):
        order, proposal = self.paid_order()
        OrderItem.objects.filter(order=order).order_by("pk").first().delete()
        before = (IgCommercialEpisode.objects.count(), IgCommerceSelectionSession.objects.count(),
            IgCommerceSelectionTransition.objects.count(), IgCommerceTurnDecision.objects.count())
        source = self.source("Ещё такую же из заказа BOUND-1, позиция 1")
        with self.assertRaises(CommerceNonMutation) as caught:
            apply_turn(self.customer, source, parse_turn(source.text))
        self.assertEqual(caught.exception.reason, "reorder_binding_invalid")
        self.assertEqual(before, (IgCommercialEpisode.objects.count(), IgCommerceSelectionSession.objects.count(),
            IgCommerceSelectionTransition.objects.count(), IgCommerceTurnDecision.objects.count()))
        self.fixture.http.assert_not_called()


class ReorderConsumerGuardTests(SimpleTestCase):
    def fixture(self):
        import hashlib
        from types import SimpleNamespace
        from management.tests_ig_response_cart_plan import ResponseCartPlanTests
        plan = ResponseCartPlanTests().plan()
        text = "Ещё такую же"
        row = {"schema": "commerce-source-observation.v1", "classification": "reorder_clarification",
            "reason": "reorder_order_ambiguous", "client_id": 4, "source_message_id": 8,
            "source_digest": hashlib.sha256(text.encode()).hexdigest(), "source_namespace": "test:ig",
            "reset_floor": 1, "event_at": "2026-10-06T12:00:00+00:00"}
        revision = SimpleNamespace(action_receipts={"commerce_reduction": {"observations": [row]}},
            bundle_snapshot={"sources": [{"message_id": 8, "text": text, "source_namespace": "test:ig"}]})
        return plan, SimpleNamespace(pk=4), revision

    def test_source_bound_pending_reorder_reaches_prompt_and_digest(self):
        from management.services.ig_response_plan import capture_response_plan
        plan, client, revision = self.fixture()
        with patch("management.services.ig_response_plan._capture_response_plan_inputs", return_value=plan):
            guarded = capture_response_plan(client, revision=revision)
        self.assertNotEqual(guarded.digest, plan.digest)
        self.assertIn("no new cart or purchase has been admitted", guarded.prompt_guidance())
        self.assertEqual(guarded.prompt_projection()["reorder_resolution"], revision.action_receipts["commerce_reduction"]["observations"])

    def test_changed_original_source_cannot_admit_observation(self):
        from management.services.ig_response_plan import capture_response_plan
        plan, client, revision = self.fixture()
        revision.bundle_snapshot["sources"][0]["text"] = "different original source"
        with patch("management.services.ig_response_plan._capture_response_plan_inputs", return_value=plan):
            guarded = capture_response_plan(client, revision=revision)
        self.assertEqual(guarded.plan_gap, "reorder_observation_changed")

    def test_pending_reorder_cannot_promote_old_paid_order_or_modify_it(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from management.services.ig_revision_live import _captured_reply_truth, RevisionGenerationBoundary
        from management.services.ig_reply_truth import ReplyTruthContext
        plan, client, revision = self.fixture()
        plan = replace(plan, configuration={**plan.configuration,
            "reorder_observations": revision.action_receipts["commerce_reduction"]["observations"]})
        truth = _captured_reply_truth(plan, SimpleNamespace(reply_text="Ваш новий заказ оплачено.", control={}),
            ReplyTruthContext(payment_confirmed=True, order_created=True))
        self.assertFalse(truth.valid)
        boundary = RevisionGenerationBoundary.__new__(RevisionGenerationBoundary)
        boundary.response_plan = plan
        for control in ({"paylink": "full"}, {"size": "L"}, {"items": ["model chosen product"]}):
            authority = boundary.response_authority(SimpleNamespace(control=control))
            self.assertFalse(authority.ready)
            self.assertEqual(authority.reasons, ("reorder_resolution_required",))
