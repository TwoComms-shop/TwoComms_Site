"""Winning paid basket, deposit and shipping policy have separate authority."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.services import ig_reply_authority as authority
from management.services.ig_reply_truth import (CheckoutLineAuthority, CheckoutOrderAuthority,
    ReplyTruthContext, ShippingAuthority, validate_reply_truth)


class PaidCommunicationTruthTests(SimpleTestCase):
    def context(self, *, payable="3000", paid="200", eligible=True):
        net, received = Decimal(payable), Decimal(paid)
        item = CheckoutLineAuthority("1", "Hoodie", 1, net, net, "M", "classic", "white")
        shipping = ShippingAuthority("native-order:1:attempt:2", "merchant_free" if eligible else "carrier_recipient",
            "Sender" if eligible else "Recipient", "automatic_threshold", customer_charge=Decimal("0") if eligible else None,
            policy_version="threshold-after-discounts.v1", threshold=Decimal("3000"), basis_total=net, eligible=eligible)
        order = CheckoutOrderAuthority(1, 2, 3, "TWC-PAID", 4, 5, timezone.now().isoformat(), (item,),
            net, Decimal("0"), net, net, received, max(net-received, Decimal("0")), (shipping,))
        return ReplyTruthContext(authorized_prices=(net,), payment_confirmed=received > 0, order_created=True,
            checkout_authority=order, full_payment_confirmed=order.fully_paid, shipping_authorities=(shipping,))

    def test_200_deposit_of_3000_has_free_shipping_without_full_payment_claim(self):
        context = self.context()
        for text in ("Внесена передоплата 200 грн. Залишилося сплатити 2800 грн. Доставка безкоштовна.",
            "Внесена предоплата 200 грн. Осталось оплатить 2800 грн. Доставка бесплатная.",
            "Deposit: 200 UAH. Balance: 2800 UAH. Free shipping."):
            with self.subTest(text=text):
                self.assertTrue(validate_reply_truth(text, context=context).valid,
                    validate_reply_truth(text, context=context).reasons)
        for text in ("Замовлення повністю оплачено.", "Заказ полностью оплачен.", "Order is fully paid.",
            "Paid: 3000 UAH.", "Hoodie: price 200 UAH."):
            with self.subTest(text=text):
                self.assertFalse(validate_reply_truth(text, context=context).valid)

    def test_threshold_is_a_policy_amount_never_a_product_price_or_actual_tariff(self):
        context = self.context(payable="850", paid="850", eligible=False)
        for text in ("Безкоштовна доставка від 3000 грн.", "Бесплатная доставка от 3000 грн.",
            "Free delivery from 3000 UAH."):
            with self.subTest(text=text):
                self.assertTrue(validate_reply_truth(text, context=context).valid,
                    validate_reply_truth(text, context=context).reasons)
        for text in ("Hoodie: price 3000 UAH.", "Delivery costs 3000 UAH.", "Free delivery from 200 UAH.",
            "Free delivery.", "TwoComms pays delivery."):
            with self.subTest(text=text):
                self.assertFalse(validate_reply_truth(text, context=context).valid)
        self.assertNotIn(Decimal("3000"), context.authorized_prices)

    def test_policy_requires_verified_origin_and_keeps_local_negation_questions_and_urls(self):
        context = self.context(payable="850", paid="850", eligible=False)
        for text in ("Delivery is not free.", "We do not pay delivery.", "Is delivery free?"):
            self.assertTrue(validate_reply_truth(text, context=context).valid,
                validate_reply_truth(text, context=context).reasons)
        self.assertFalse(validate_reply_truth("Is delivery free? See https://unapproved.example/", context=context).valid)
        bare = ReplyTruthContext(authorized_prices=(Decimal("3000"),))
        self.assertFalse(validate_reply_truth("Free shipping from 3000 UAH.", context=bare).valid)
        manual = ReplyTruthContext(shipping_authorities=(ShippingAuthority("manager:1", "merchant_free", "Sender", "manual_manager"),))
        self.assertTrue(validate_reply_truth("TwoComms covers shipping.", context=manual).valid)
        self.assertFalse(validate_reply_truth("Free shipping from 3000 UAH.", context=manual).valid)

    def test_source_signed_wire_cannot_be_rebound_or_change_the_paid_basket(self):
        body = {"schema": authority.CHECKOUT_ORDER_SCHEMA,
            "scope": {"client_id": 1, "episode_id": 2, "order_id": 3}, "order_number": "TWC-PAID",
            "winner_generation_id": 4, "attempt_id": 5, "observed_at": timezone.now().isoformat(),
            "items": [{"line_id": "1", "title": "Hoodie", "quantity": 2, "unit_price": "1500.00",
                "line_total": "3000.00", "size": "M", "fit": "classic", "color": "white"}],
            "money": {"gross": "3000.00", "discount": "0.00", "merchandise_payable": "3000.00",
                "payable": "3000.00", "confirmed_paid": "200.00", "balance": "2800.00", "currency": "UAH"},
            "checkout_changes": [], "shipping_authorities": [], "source_ref": {"proposal_id": 6,
                "proposal_revision": 2, "generation_id": 4, "attempt_id": 5, "items_digest": "a"*64,
                "settlement_status": "prepaid", "amendment_hmacs": []}}
        key_id = next(iter(authority._snapshot_keys()))
        wire = {**body, "key_id": key_id, "hmac": authority._snapshot_mac(body, key_id)}
        typed = authority.checkout_order_authority(wire, client_id=1, episode_id=2)
        self.assertIsNotNone(typed)
        self.assertEqual(typed.items[0].quantity, 2)
        self.assertFalse(typed.fully_paid)
        self.assertEqual(typed.as_dict(), wire)
        self.assertIsNone(authority.checkout_order_authority(wire, client_id=1, episode_id=99))
        changed = deepcopy(wire)
        changed["items"][0]["quantity"] = 1
        self.assertIsNone(authority.checkout_order_authority(changed, client_id=1, episode_id=2))
        self.assertIsNone(authority.checkout_order_authority(body, client_id=1, episode_id=2))
        from datetime import datetime, timedelta
        future_body = deepcopy(body)
        future_body["source_ref"]["paid_at"] = (datetime.fromisoformat(body["observed_at"]) + timedelta(days=1)).isoformat()
        future_wire = {**future_body, "key_id": key_id, "hmac": authority._snapshot_mac(future_body, key_id)}
        self.assertIsNone(authority.checkout_order_authority(future_wire, client_id=1, episode_id=2))


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativePaidCommunicationTests(TestCase):
    def setUp(self):
        from management.tests_ig_checkout_cart_amendments import NativeCheckoutCartAmendmentTests
        NativeCheckoutCartAmendmentTests.setUp(self)
        self.client_row = self.client
        self.http = Client()

    def _valid_item(self, **values):
        from management.tests_ig_checkout_service import InstagramCheckoutConfigurationTests
        return InstagramCheckoutConfigurationTests._valid_item(self, **values)

    def _payload(self, **values):
        from management.tests_ig_checkout_cart_amendments import NativeCheckoutCartAmendmentTests
        return NativeCheckoutCartAmendmentTests._payload(self, **values)

    def _open(self, proposal):
        from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests
        return CheckoutGenerationRuntimeTests._open(self, proposal)

    def _delivery_payload(self, **values):
        from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests
        return CheckoutGenerationRuntimeTests._delivery_payload(self, **values)

    def invoice(self, *, payment_choice="online_full"):
        from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests
        saved, self.client = self.client, self.http
        try:
            with override_settings(ROOT_URLCONF="twocomms.urls"):
                return CheckoutGenerationRuntimeTests._create_invoice(self, self.proposal,
                    invoice_id="native-paid-communication", payment_choice=payment_choice)
        finally:
            self.client = saved

    def pay(self, generation):
        from storefront.views.monobank import _apply_payment_attempt_status
        order, created = _apply_payment_attempt_status(generation.payment_attempt, "success",
            payload={"paidAmount": int(generation.payment_attempt.payment_amount * 100)}, source="provider_pull")
        self.assertTrue(created)
        self.client_row.refresh_from_db()
        self.proposal.refresh_from_db()
        return order

    def test_real_amended_then_paid_order_drives_reply_prompt_plan_and_shared_capture(self):
        from management.services.ig_checkout_cart_amendments import amend_checkout_cart
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_admin_state_capture import capture_payment_context
        from management.services.ig_client_state_card import capture_client_state, render_client_state_prompt
        old_revision = deepcopy(self.proposal.revisions.get(revision=1).snapshot)
        amend_checkout_cart(self.proposal, request=self.request, grant=self.grant, payload=self._payload(quantity=2))
        self.proposal.refresh_from_db()
        order = self.pay(self.invoice())
        with patch("storefront.views.monobank._monobank_api_request") as provider:
            typed = authority.capture_checkout_order_authority(self.client_row)
            self.assertIsNotNone(typed)
            self.assertEqual(typed.order_id, order.pk)
            self.assertEqual(sum(item.quantity for item in typed.items), 2)
            self.assertEqual(typed.confirmed_paid, typed.payable)
            self.assertTrue(typed.as_dict()["checkout_changes"])
            for change in typed.as_dict()["checkout_changes"]:
                self.assertLessEqual(len(change["before"]), 1)
                self.assertLessEqual(len(change["after"]), 1)
                self.assertEqual(change["before"][0]["quantity"], 1)
                self.assertEqual(change["after"][0]["quantity"], 2)
                self.assertEqual(change["before"][0]["title"], self.shirt.title)
                self.assertEqual(change["after"][0]["title"], self.shirt.title)
            context = authority.build_reply_truth_context(self.client_row, checkout_authority=typed)
            self.assertTrue(validate_reply_truth(f"Всього {typed.payable} грн.", context=context).valid)
            self.assertFalse(validate_reply_truth("Всього 950 грн.", context=context).valid)
            self.assertFalse(validate_reply_truth("Футболка Київ: 1 шт.", context=context).valid)
            guidance = authority.checkout_communication_prompt_guidance(context)
            self.assertIn('"quantity":2', guidance)
            self.assertIn('"confirmed_paid":', guidance)
            plan = capture_response_plan(self.client_row)
            self.assertEqual(plan.communication_context.checkout_authority.order_id, order.pk)
            boundary = {"client_id": self.client_row.pk, "episode_id": typed.episode_id, "order_id": order.pk,
                "line_id": None, "recipient_id": None, "reset_floor": 0, "source_namespace": "", "reset_id": 0,
                "erasure_epoch": 0, "source_watermark": {}}
            captured_at = timezone.now()
            payment, reason, fence = capture_payment_context(boundary, now=captured_at)
            self.assertFalse(reason)
            again, reason, again_fence = capture_payment_context(boundary, now=captured_at)
            self.assertFalse(reason)
            self.assertEqual(fence, again_fence)
            self.assertEqual(payment["checkout_order"], again["checkout_order"])
            self.assertEqual(payment["checkout_order"]["capture"]["scope"]["order_id"], order.pk)
            state = capture_client_state(boundary=boundary, components={"checkout_order": payment["checkout_order"]}, captured_at=timezone.now())
            self.assertEqual(state.as_dict()["slots"]["order.completed"]["value"]["scope"]["order_id"], order.pk)
            self.assertIn("confirmed_paid", render_client_state_prompt(state, budget=12000).text)
            order.items.update(qty=1)
            changed, _reason, changed_fence = capture_payment_context(boundary, now=captured_at)
            self.assertNotEqual(fence, changed_fence)
            self.assertNotIn("checkout_order", changed)
        provider.assert_not_called()
        self.assertEqual(self.proposal.revisions.get(revision=1).snapshot, old_revision)

    def test_actual_order_cannot_be_borrowed_by_a_new_purchase_episode(self):
        from management.models import IgDeal
        from management.services.ig_commercial_episodes import ensure_episode_for_deal
        self.pay(self.invoice())
        old = authority.capture_checkout_order_authority(self.client_row)
        self.assertIsNotNone(old)
        new_deal = IgDeal.objects.create(client=self.client_row, amount=Decimal("950"), requested_payment_amount=Decimal("950"))
        ensure_episode_for_deal(new_deal)
        self.client_row.refresh_from_db()
        self.assertNotEqual(self.client_row.current_commercial_episode_id, old.episode_id)
        self.assertIsNone(authority.capture_checkout_order_authority(self.client_row))
        context = authority.build_reply_truth_context(self.client_row, checkout_authority=old)
        self.assertIsNone(context.checkout_authority)
        self.assertFalse(context.payment_confirmed)

    def test_native_200_deposit_then_qualified_manager_full_payment_updates_current_cash(self):
        from django.contrib.auth import get_user_model
        from management.models import IgClient, InstagramBotMessage, IgPaymentConfirmationReview, IgPaymentProjection
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_commercial_episodes import payment_truth_snapshot
        from management.services.ig_payment_review import record_review_decision
        self.shirt.price = Decimal("3000")
        self.shirt.save(update_fields=["price"])
        # Start a fresh customer's checkout. The setup's existing full-payment
        # proposal must not become this direct 200+COD request's source.
        self.client_row = IgClient.get_or_create_for_sender("native-later-payment-200")
        self.client = self.client_row
        message = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="user", text="Чи можна 200 грн передоплати, а решту післяплатою?")
        self.proposal = create_or_update_proposal(client=self.client_row, pay_type="prepayment",
            requested_payment_amount=Decimal("200"), item_specs=[self._valid_item()],
            evidence={"message_ids": [message.pk]})
        self.assertEqual(self.proposal.payment_policy, self.proposal.PaymentPolicy.FULL_OR_200_COD)
        self.assertEqual(self.proposal.payment_policy_evidence_message_id, message.pk)
        economic_revision = self.proposal.revision
        original_quote = deepcopy(self.proposal.revisions.get(revision=economic_revision).snapshot)
        generation = self.invoice(payment_choice="prepay_200_cod")
        self.assertEqual(generation.payment_attempt.payment_amount, Decimal("200"))
        order = self.pay(generation)
        generation.payment_attempt.refresh_from_db()
        self.assertEqual(generation.payment_attempt.paid_amount, Decimal("200"))
        before = authority.capture_checkout_order_authority(self.client_row)
        native_ledger = payment_truth_snapshot(episode=self.client_row.current_commercial_episode,
            allow_deal_review_fallback=False)
        actual_deal = self.client_row.current_commercial_episode.deal
        self.assertEqual(actual_deal.requested_payment_amount, Decimal("200"))
        self.assertEqual(actual_deal.pay_type, actual_deal.PayType.PREPAY_200)
        self.assertFalse(native_ledger["needs_reconciliation"], native_ledger)
        self.assertEqual(native_ledger["confirmed_paid_amount"], "200.00")
        self.assertEqual(self.proposal.quoted_total, Decimal("3000"))
        self.assertEqual(self.proposal.requested_payment_amount, Decimal("3000"))
        self.assertEqual(self.proposal.revisions.get(revision=economic_revision).snapshot, original_quote)
        self.assertEqual(before.confirmed_paid, Decimal("200"),
            (before.as_dict()["source_ref"], native_ledger))
        self.assertEqual(before.balance, Decimal("2800"))
        self.assertEqual(before.as_dict()["source_ref"]["paid_at"], self.proposal.paid_at.isoformat())
        actor = get_user_model().objects.create_user(username="checkout-ledger-manager", is_staff=True, is_superuser=True)
        episode = self.client_row.current_commercial_episode
        review = IgPaymentConfirmationReview.objects.create(client=self.client_row, deal=episode.deal,
            order=order, dedupe_key="checkout-later-full-payment", watermark_message_id=message.pk,
            evidence={"order_draft": {"quoted_total": "3000.00"}})
        episode.primary_payment_review = review
        episode.save(update_fields=["primary_payment_review", "updated_at"])
        record_review_decision(review, actor=actor, decision="manager_verified",
            verification_scope="full_payment", confirmed_amount="3000.00")
        self.client_row.refresh_from_db()
        episode.refresh_from_db()
        # A bank deposit and a conflicting manager total require reconciliation;
        # they are never blindly summed or promoted to full payment.
        conflict = payment_truth_snapshot(episode=episode, allow_deal_review_fallback=False)
        self.assertTrue(conflict["needs_reconciliation"])
        conflicted = authority.capture_checkout_order_authority(self.client_row)
        self.assertIsNone(conflicted.confirmed_paid)
        self.assertFalse(conflicted.fully_paid)
        self.assertFalse(validate_reply_truth("Order is fully paid.",
            context=authority.build_reply_truth_context(self.client_row, checkout_authority=conflicted)).valid)
        # A manager-only settled ledger is a distinct native source. The first
        # Attempt retains its original 200; current cash comes from the ledger.
        projection = IgPaymentProjection.objects.get(deal_id=episode.deal_id)
        projection.truth, projection.gross_amount, projection.refunded_amount = "unverified", Decimal("0"), Decimal("0")
        projection.needs_reconciliation = False
        projection.save(update_fields=["truth", "gross_amount", "refunded_amount", "needs_reconciliation", "updated_at"])
        ledger = payment_truth_snapshot(episode=episode, allow_deal_review_fallback=False)
        self.assertTrue(ledger["authoritative_for_fulfillment"])
        self.assertEqual(ledger["confirmed_paid_amount"], "3000.00")
        after = authority.capture_checkout_order_authority(self.client_row)
        self.assertEqual(after.confirmed_paid, Decimal("3000"))
        self.assertEqual(after.balance, Decimal("0"))
        self.assertTrue(after.fully_paid)
        review.refresh_from_db()
        self.assertEqual(after.as_dict()["source_ref"]["paid_at"], review.confirmed_at.isoformat())
        self.assertEqual(after.as_dict()["source_ref"]["payment_ledger"]["decision_id"], review.decisions.get().pk)
        self.assertTrue(validate_reply_truth("Order is fully paid. Paid: 3000 UAH. Balance: 0 UAH.",
            context=authority.build_reply_truth_context(self.client_row, checkout_authority=after)).valid)
        projection.truth = "refunded"
        projection.save(update_fields=["truth", "updated_at"])
        refunded = authority.capture_checkout_order_authority(self.client_row)
        self.assertIsNone(refunded.confirmed_paid)
        self.assertFalse(refunded.fully_paid)
        from datetime import timedelta
        future = authority.capture_checkout_order_authority(self.client_row, now=timezone.now()-timedelta(days=1))
        self.assertIsNone(future.confirmed_paid)
        self.assertFalse(future.fully_paid)
        self.assertIsNone(future.as_dict()["source_ref"]["paid_at"])


class LegacyManagerOrderCommunicationTests(TestCase):
    def test_manager_order_without_bank_attempt_keeps_actual_prices_and_qualified_payment(self):
        from django.contrib.auth import get_user_model
        from management.models import IgPaymentConfirmationReview
        from management.tests_ig_reply_authority import ReplyAuthorityContextTests
        from management.services.ig_payment_review import record_review_decision
        client, deal, episode = ReplyAuthorityContextTests._current_episode(self, "legacy-manager-actual-order")
        order = ReplyAuthorityContextTests._order(self, "LEGACY-MGR", total_sum=Decimal("900"))
        order.items.create(title="Manager Hoodie", qty=2, unit_price=Decimal("450"), line_total=Decimal("900"), size="M")
        deal.order = order
        deal.save(update_fields=["order", "updated_at"])
        episode.intended_order = order
        review = IgPaymentConfirmationReview.objects.create(client=client, deal=deal, order=order,
            dedupe_key="legacy-manager-actual-review", evidence={"order_draft": {"quoted_total": "900"}})
        episode.primary_payment_review = review
        episode.save(update_fields=["intended_order", "primary_payment_review", "updated_at"])
        actor = get_user_model().objects.create_user(username="legacy-manager-actual", is_staff=True, is_superuser=True)
        record_review_decision(review, actor=actor, decision="manager_verified", verification_scope="full_payment", confirmed_amount="900")
        client.refresh_from_db()
        context = authority.build_reply_truth_context(client)
        self.assertIsNone(context.checkout_authority)
        self.assertTrue(context.full_payment_confirmed)
        self.assertIn(Decimal("450"), context.authorized_prices)
        self.assertIn(Decimal("900"), context.authorized_prices)
        self.assertTrue(validate_reply_truth("Manager Hoodie: price 450 UAH. Order is fully paid.", context=context).valid)
        self.assertFalse(validate_reply_truth("Manager Hoodie: price 3000 UAH.", context=context).valid)
