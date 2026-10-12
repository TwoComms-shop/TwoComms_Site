"""Native source purchase and finite checkout after physical stock reconciliation."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings

from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest as Request, IgFulfillmentReviewDecision as Decision
from management.models import IgCheckoutProposal, IgCheckoutAccessToken, IgCheckoutInventoryReservation as Reservation, InstagramBotMessage
from management.services import ig_fulfillment_reviews as reviews
from management.services.ig_fulfillment_checkout import (prepare_fulfillment_checkout,
    FulfillmentCheckoutError, owned_stock_sync_reservation_ids)
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeFulfillmentCheckoutTests(_RestockFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="fulfillment-checkout-manager",
            email="manager@example.test", password="test")

    def case(self, *, purchase=False, availability="available", revision=None):
        if purchase:
            self.canonical("Хочу купити")
        if revision is None:
            source = self.source("Встигнете відправити до 20.10.2026? Це подарунок")
            revision, _ = self.revision(source)
        made = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(made["ready"], made)
        request = Request.objects.get(pk=made["request_id"])
        rows = []
        for index, row in enumerate(request.envelope["lines"]):
            state = availability.get(index, "available") if isinstance(availability, dict) else availability
            rows.append({"line_id": row["line_id"], "availability": state,
                "checked_quantity": 1 if state == "available" else None,
                "supply_basis": "accessible_unrecorded" if state == "available" else "incoming_confirmed"})
        result = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), decisions_by_line=rows, now=self.now)
        self.assertTrue(result["ready"], result)
        return request, Decision.objects.get(pk=result["decision_id"])

    def recount(self, count=1):
        type(self.variant).objects.filter(pk=self.variant.pk).update(stock=count)
        type(self.rule).objects.filter(pk=self.rule.pk).update(stock=count, updated_at=self.now)

    def checkout(self, request, decision):
        with transaction.atomic(), patch("management.services.instagram_bot._provider_http") as http:
            result = prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now)
        http.assert_not_called()
        return result

    def discounted_case(self):
        """Use an actual human price offer, later customer acceptance and quote."""
        self.canonical("Хочу купити")
        offer = InstagramBotMessage.objects.create(client=self.client_row, sender_id="staff-manager",
            role="manager", source="manual", status="done", text="Домовлена сума замовлення 700 грн")
        acceptance = self.source("Домовились, 700 грн")
        self.revision(acceptance)
        request, decision = self.case()
        self.recount(2)
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_checkout import checkout_authority_control
        from management.services.ig_checkout import create_or_update_proposal
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.client_row.refresh_from_db()
        control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=cart)
        self.assertFalse(reasons, reasons)
        evidence_ids = sorted(set(cart["fence"]["source_ids"]) | {offer.pk, acceptance.pk, request.source_id})
        with transaction.atomic():
            proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full",
                item_specs=control["items"], negotiated_total=Decimal("700.00"),
                evidence={"message_ids": evidence_ids}, allow_promo=False,
                source_cart_binding=control["source_cart_binding"], source_cart_capture=control["source_cart_capture"],
                checkout_owner_scope=control["checkout_owner_scope"])
        self.assertGreater(proposal.negotiated_discount, 0)
        return request, decision, proposal, offer

    def test_availability_without_source_purchase_finishes_only_stock_recount(self):
        request, decision = self.case()
        with transaction.atomic():
            self.recount(3)
            result = prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now)
        self.assertEqual(result["status"], "stock_ready")
        self.assertEqual(result["reason"], "checkout_purchase_authority_missing")
        self.assertIsNone(result["proposal_id"])
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 3)

    def test_source_purchase_creates_ordinary_priced_full_cart_and_finite_hold_without_token(self):
        request, decision = self.case(purchase=True)
        with transaction.atomic(), patch("management.services.instagram_bot._provider_http") as http:
            self.recount()
            result = prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now)
        http.assert_not_called()
        self.assertEqual(result["status"], "proposal_ready")
        proposal = IgCheckoutProposal.objects.get(pk=result["proposal_id"])
        self.assertTrue(proposal.assisted_checkout_v2)
        self.assertEqual(proposal.catalog_total, self.product.price)
        self.assertFalse(proposal.payment_attempt_id)
        self.assertFalse(proposal.winner_invoice_generation_id)
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.assertNotIn("url", result)
        self.assertIn(f"client_id={self.client_row.pk}", result["manager_workspace_url"])
        hold = Reservation.objects.get(pk=result["reservation_ids"][0])
        self.assertEqual(hold.state, Reservation.State.ACTIVE)
        self.assertEqual(hold.quantity, 1)
        self.assertIsNone(hold.invoice_generation_id)
        self.assertEqual(result["proof"]["owner_after"]["deal"]["active_checkout_proposal_id"], proposal.pk)
        self.assertFalse(result["proof"]["payment_confirmed"])
        self.assertFalse(result["proof"]["order_created"])

    def test_retry_reuses_same_proposal_and_hold_only_current_source_cart_is_excludable(self):
        request, decision = self.case(purchase=True)
        self.recount(2)
        first = self.checkout(request, decision)
        second = self.checkout(request, decision)
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertEqual(first["reservation_ids"], second["reservation_ids"])
        # Re-produce the manager case from its actual sealed source after the
        # ordinary proposal attaches its owner. Adding a different inbound
        # source would change the frozen checkout watermark and test a new cart.
        current_request, current_decision = self.case(revision=request.revision)
        self.assertNotEqual(current_request.pk, request.pk)
        from management.services.ig_commerce_projection import capture_current_selection_lines
        current_request.client.refresh_from_db()
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(owned_stock_sync_reservation_ids(current_request, current_decision, cart, self.now), first["reservation_ids"])
        Reservation.objects.filter(pk__in=first["reservation_ids"]).update(state=Reservation.State.PAID_COMMITTED)
        self.assertEqual(owned_stock_sync_reservation_ids(current_request, current_decision, cart, self.now), [])

    def test_incoming_wait_cannot_silently_create_available_subset(self):
        request, decision = self.case(purchase=True, availability="wait")
        with self.assertRaisesMessage(FulfillmentCheckoutError, "checkout_full_cart_not_ready"):
            self.checkout(request, decision)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_mixed_two_position_cart_never_creates_only_available_position(self):
        from product_catalog.models import VariantSizeRule, ProductInventoryPolicy
        other, variant = self.products[1], self.variants[1]
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="M", stock=0, is_enabled=True)
        ProductInventoryPolicy.objects.create(product=other, source="catalog_variant")
        self.canonical(f"добавьте {other.title}, количество 1, размер M, цвет pink, крій classic")
        request, decision = self.case(purchase=True, availability={1: "wait"})
        self.assertEqual(len(request.envelope["lines"]), 2)
        with self.assertRaisesMessage(FulfillmentCheckoutError, "checkout_full_cart_not_ready"):
            self.checkout(request, decision)
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(Reservation.objects.exists())

    def test_mutated_original_sealed_requirement_source_cannot_allocate(self):
        request, decision = self.case(purchase=True)
        self.recount()
        request.source.text = "Змінився строк і адресат"
        request.source.save(update_fields=["text"])
        with self.assertRaises(FulfillmentCheckoutError):
            self.checkout(request, decision)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_quote_failure_rolls_back_real_count_writes_with_the_outer_transaction(self):
        request, decision = self.case(purchase=True)
        self.product.price = 0
        self.product.save(update_fields=["price"])
        self.variant.price_override = 0
        self.variant.save(update_fields=["price_override"])
        with self.assertRaises(FulfillmentCheckoutError):
            with transaction.atomic():
                self.recount()
                prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual(self.variant.stock, 0)
        self.assertEqual(self.rule.stock, 0)
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(Reservation.objects.exists())

    def test_changed_source_or_quantity_and_expired_decision_cannot_allocate(self):
        request, decision = self.case(purchase=True)
        self.recount(4)
        self.canonical("кількість 2")
        with self.assertRaises(FulfillmentCheckoutError):
            self.checkout(request, decision)
        self.assertFalse(IgCheckoutProposal.objects.exists())
        with transaction.atomic(), self.assertRaises(FulfillmentCheckoutError):
            prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now + timedelta(hours=25))

    def test_no_capability_or_physical_stock_blocks_actual_purchase(self):
        request, decision = self.case(purchase=True)
        other = get_user_model().objects.create_user(username="no-fulfillment-checkout-permission")
        with transaction.atomic(), self.assertRaisesMessage(FulfillmentCheckoutError, "actor_forbidden"):
            prepare_fulfillment_checkout(request, decision, actor=other, now=self.now)
        with self.assertRaises(FulfillmentCheckoutError):
            self.checkout(request, decision)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_existing_source_proved_negotiated_price_and_payment_policy_are_retained(self):
        request, decision, original, offer = self.discounted_case()
        before_revision = original.revisions.get(revision=original.revision)
        with transaction.atomic(), patch("management.services.instagram_bot._provider_http") as http:
            self.recount(3)
            result = prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now)
        http.assert_not_called()
        proposal = IgCheckoutProposal.objects.get(pk=result["proposal_id"])
        self.assertEqual(proposal.pk, original.pk)
        self.assertEqual(proposal.quoted_total, Decimal("700.00"))
        self.assertEqual(proposal.negotiated_discount, original.negotiated_discount)
        self.assertEqual(proposal.pay_type, original.pay_type)
        self.assertEqual(proposal.requested_payment_amount, original.requested_payment_amount)
        self.assertFalse(proposal.allow_promo)
        evidence = proposal.revisions.get(revision=proposal.revision).evidence_message_ids
        self.assertTrue(set(before_revision.evidence_message_ids).issubset(evidence))
        self.assertIn(offer.pk, evidence)
        self.assertTrue(result["reservation_ids"])
        self.assertFalse(IgCheckoutAccessToken.objects.exists())

    def test_changed_human_price_evidence_rolls_back_recount_without_repricing(self):
        request, decision, original, offer = self.discounted_case()
        offer.text = "Менеджер більше не пропонує погоджену суму"
        offer.save(update_fields=["text"])
        with self.assertRaisesMessage(FulfillmentCheckoutError, "catalog_changed"):
            with transaction.atomic():
                self.recount(3)
                prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now)
        original.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 2)
        self.assertEqual(original.quoted_total, Decimal("700.00"))
        self.assertFalse(original.allow_promo)
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)
        self.assertEqual(original.revisions.count(), 1)
        self.assertFalse(Reservation.objects.exists())

    def test_changed_catalog_price_does_not_silently_reprice_existing_agreement(self):
        request, decision, original, _ = self.discounted_case()
        self.variant.price_override = Decimal("810.00")
        self.variant.save(update_fields=["price_override"])
        with self.assertRaisesMessage(FulfillmentCheckoutError, "catalog_changed"):
            with transaction.atomic():
                self.recount(3)
                prepare_fulfillment_checkout(request, decision, actor=self.manager, now=self.now)
        original.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(original.quoted_total, Decimal("700.00"))
        self.assertEqual(self.variant.stock, 2)
        self.assertEqual(original.revisions.count(), 1)
