"""Real checkout writes stay within one current, unconverted purchase owner."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import (
    IgCheckoutInvoiceGeneration, IgCheckoutProposal, IgCommercialEpisode, IgCommerceSelectionSession,
    IgClient, IgDeal, IgPaymentProjection, InstagramBotMessage, provider_evidence_signature,
)
from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal
from management.services.ig_commercial_episodes import (
    ensure_open_episode_for_locked_client, start_repeat_episode,
)
from orders.models import Order, PaymentAttempt
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption
from storefront.views.monobank import _apply_payment_attempt_status
from management.tests_ig_hosted_settlement import HostedSettlementFixture


@override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class CheckoutCurrentEpisodeBindingTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Футболки", slug="checkout-episode-shirts")
        self.product = Product.objects.create(title="Episode shirt", slug="checkout-episode-shirt",
            category=category, price=Decimal("900.00"), status="published")
        self.fit = ProductFitOption.objects.create(product=self.product, code="classic", label="Classic", is_active=True)
        color = Color.objects.create(name="Чорний", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(product=self.product, color=color, stock=0)
        self.client_row = IgClient.get_or_create_for_sender("checkout-episode-owner")
        self.item = {"product_id": self.product.pk, "color_variant_id": self.variant.pk,
                     "qty": 1, "size": "M", "fit_option_code": self.fit.code}
        no_http = patch("storefront.views.monobank._monobank_api_request", side_effect=AssertionError("checkout proposal must not dispatch HTTP"))
        self.http = no_http.start()
        self.addCleanup(no_http.stop)

    def quote(self, **kwargs):
        return create_or_update_proposal(client=self.client_row, pay_type="online_full", item_specs=[self.item], **kwargs)

    def paid_graph(self):
        """Produce a genuine verified hosted winner through the accepted API."""
        # Hosted checkout reserves this actual catalog variant. Keep enough
        # physical stock for the original order and subsequent repeat fixtures.
        self.variant.stock = max(5, int(self.item["qty"]))
        self.variant.save(update_fields=["stock"])
        proposal = self.quote()
        # The accepted factory exercises token/session, signed delivery fields,
        # immutable generation and invoice identity. Its physical HTTP is mocked;
        # our outer no-HTTP sentinel remains untouched by all subsequent reads.
        with patch("management.services.ig_lifecycle.dispatch_lifecycle_event", return_value="pending"):
            attempt = HostedSettlementFixture.invoice(self, proposal, "old-paid-invoice")
            payload = {"invoiceId": attempt.monobank_invoice_id, "reference": attempt.reference,
                "ccy": 980, "status": "success", "amount": int(attempt.payment_amount * 100),
                "finalAmount": int(attempt.payment_amount * 100), "modifiedDate": timezone.now().isoformat()}
            order, created = _apply_payment_attempt_status(attempt, "success", payload=payload, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        proposal.refresh_from_db()
        attempt.refresh_from_db()
        self.client_row.refresh_from_db()
        generation = IgCheckoutInvoiceGeneration.objects.get(payment_attempt=attempt)
        projection = IgPaymentProjection.objects.select_related("last_event").get(deal_id=proposal.deal_id)
        self.assertEqual((proposal.status, proposal.winner_invoice_generation_id), (IgCheckoutProposal.Status.PAID, generation.pk))
        self.assertEqual((generation.state, generation.winner_slot), (IgCheckoutInvoiceGeneration.State.PAID_WINNER, 1))
        self.assertEqual((attempt.status, attempt.order_id, attempt.checkout_winner_claimed), (PaymentAttempt.Status.CONVERTED, order.pk, True))
        self.assertEqual((projection.truth, projection.net_paid_amount), (IgDeal.PaymentTruth.CONFIRMED, attempt.payment_amount))
        event = projection.last_event
        self.assertEqual((event.provider, event.source, event.invoice_id, event.currency, event.amount_valid),
                         ("monobank", "provider_attempt", attempt.monobank_invoice_id, "UAH", True))
        self.assertIsNotNone(event.provider_modified_at)
        self.assertEqual(event.evidence["attempt_reference"], attempt.reference)
        self.assertEqual(event.evidence["signature"], provider_evidence_signature(
            deal_id=proposal.deal_id, client_id=self.client_row.pk, provider=event.provider,
            source=event.source, invoice_id=event.invoice_id, provider_status="success", payload_digest=event.payload_digest))
        self.assertTrue(any(observation.get("status") == "success" and observation.get("source") == "provider_pull"
                            for observation in attempt.payment_history))
        self.http.assert_not_called()
        return proposal, generation, attempt, order

    def repeat(self):
        source = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="user", source="webhook", text="Хочу ще одну таку футболку", provider_message_id="repeat-episode-source")
        return start_repeat_episode(self.client_row, repeat_kind=IgCommercialEpisode.RepeatKind.EXPLICIT_MORE,
            evidence_message_ids=[source.pk], confidence=Decimal("1.00"), analysis_model="source_parser",
            analysis_prompt_version="episode_binding_tests")

    def assert_denied(self, code, **kwargs):
        before = (IgDeal.objects.count(), IgCheckoutProposal.objects.count(), IgCommercialEpisode.objects.count(),
                  IgCheckoutInvoiceGeneration.objects.count(), Order.objects.count())
        with self.assertRaises(CheckoutConfigurationError) as caught:
            self.quote(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(before, (IgDeal.objects.count(), IgCheckoutProposal.objects.count(), IgCommercialEpisode.objects.count(),
                                 IgCheckoutInvoiceGeneration.objects.count(), Order.objects.count()))
        self.http.assert_not_called()

    def test_new_repeat_same_money_digest_never_reuses_old_paid_graph(self):
        old, generation, attempt, order = self.paid_graph()
        old_snapshot = deepcopy(old.revisions.get(revision=old.revision).snapshot)
        new_episode = self.repeat()
        session_count = IgCommerceSelectionSession.objects.filter(client=self.client_row).count()
        new = self.quote()
        retry = self.quote()
        self.assertEqual(new.items_digest, old.items_digest)
        self.assertNotEqual(new.pk, old.pk)
        self.assertNotEqual(new.deal_id, old.deal_id)
        self.assertEqual(new.commercial_episode_id, new_episode.pk)
        self.assertIsNone(new.payment_attempt_id)
        self.assertIsNone(new.current_invoice_generation_id)
        self.assertIsNone(new.winner_invoice_generation_id)
        self.assertIsNone(new.deal.order_id)
        self.assertEqual(retry.pk, new.pk)
        self.assertEqual(IgCommerceSelectionSession.objects.filter(client=self.client_row).count(), session_count)
        old.refresh_from_db()
        generation.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((old.status, old.payment_attempt_id, old.winner_invoice_generation_id),
                         (IgCheckoutProposal.Status.PAID, attempt.pk, generation.pk))
        self.assertEqual((generation.state, generation.proposal_id, attempt.order_id),
                         (IgCheckoutInvoiceGeneration.State.PAID_WINNER, old.pk, order.pk))
        self.assertEqual(old.revisions.get(revision=old.revision).snapshot, old_snapshot)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), 1)
        self.http.assert_not_called()

    def test_same_current_unconverted_retry_reuses_proposal_revision_and_deal(self):
        first = self.quote()
        second = self.quote()
        self.client_row.refresh_from_db()
        self.assertEqual((first.pk, first.deal_id, first.commercial_episode_id),
                         (second.pk, second.deal_id, self.client_row.current_commercial_episode_id))
        self.assertEqual(second.revision, 1)
        self.assertEqual(second.revisions.count(), 1)
        self.assertEqual(IgDeal.objects.count(), 1)
        self.assertEqual(IgCommercialEpisode.objects.count(), 1)
        self.http.assert_not_called()

    def test_null_legacy_pointer_does_not_select_historical_paid_deal(self):
        old, _generation, _attempt, _order = self.paid_graph()
        episode = old.commercial_episode
        episode.open_slot = None
        episode.save(update_fields=["open_slot", "updated_at"])
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=None)
        new = self.quote()
        retry = self.quote()
        self.client_row.refresh_from_db()
        self.assertNotEqual(new.deal_id, old.deal_id)
        self.assertNotEqual(new.commercial_episode_id, old.commercial_episode_id)
        self.assertEqual(new.items_digest, old.items_digest)
        self.assertEqual(retry.pk, new.pk)
        self.assertEqual(self.client_row.current_commercial_episode_id, new.commercial_episode_id)
        self.assertEqual(IgDeal.objects.count(), 2)
        self.assertEqual(Order.objects.count(), 1)
        self.http.assert_not_called()

    def test_null_first_pointer_attaches_only_exact_unbound_open_owner(self):
        from django.db import transaction
        with transaction.atomic():
            row = IgClient.objects.select_for_update().get(pk=self.client_row.pk)
            open_episode = ensure_open_episode_for_locked_client(row, materialization_prefix="checkout-test")
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=None)
        proposal = self.quote()
        retry = self.quote()
        self.client_row.refresh_from_db()
        open_episode.refresh_from_db()
        self.assertEqual(open_episode.deal_id, proposal.deal_id)
        self.assertEqual(proposal.commercial_episode_id, open_episode.pk)
        self.assertEqual(self.client_row.current_commercial_episode_id, open_episode.pk)
        self.assertEqual(retry.pk, proposal.pk)
        self.assertEqual(IgDeal.objects.count(), 1)

    def test_null_pointer_with_bound_open_cycle_is_finite_conflict(self):
        first = self.quote()
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=None)
        self.assert_denied("checkout_episode_scope_unknown")
        first.refresh_from_db()
        self.assertEqual(first.revision, 1)
        self.assertEqual(first.deal.active_checkout_proposal_id, first.pk)

    def test_current_converted_episode_requires_new_source_episode_before_quote(self):
        old, _generation, _attempt, _order = self.paid_graph()
        self.assert_denied("checkout_episode_converted")
        old.refresh_from_db()
        self.assertEqual(old.status, IgCheckoutProposal.Status.PAID)

    def test_explicit_old_deal_cannot_attach_to_new_repeat_episode(self):
        old, _generation, _attempt, _order = self.paid_graph()
        self.repeat()
        self.assert_denied("checkout_episode_scope_changed", deal=old.deal)

    def test_foreign_current_pointer_is_not_repaired_by_latest_deal_fallback(self):
        first = self.quote()
        other_client = IgClient.get_or_create_for_sender("checkout-foreign-owner")
        other = create_or_update_proposal(client=other_client, pay_type="online_full", item_specs=[self.item])
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode_id=other.commercial_episode_id)
        self.assert_denied("checkout_episode_scope_changed")
        first.refresh_from_db()
        self.assertEqual(first.revision, 1)

    def test_current_closed_unconverted_episode_is_stale(self):
        first = self.quote()
        episode = first.commercial_episode
        episode.open_slot = None
        episode.save(update_fields=["open_slot", "updated_at"])
        self.assert_denied("checkout_episode_stale")

    def test_verified_paid_deal_with_lagging_episode_is_still_converted(self):
        first = self.quote()
        deal = first.deal
        deal.payment_truth = IgDeal.PaymentTruth.CONFIRMED
        deal.paid_amount = first.quoted_total
        deal.save(update_fields=["payment_truth", "paid_amount", "updated_at"])
        self.assert_denied("checkout_episode_converted")

    def test_paid_proposal_cannot_replay_through_lagging_episode_and_deal(self):
        old, _generation, _attempt, _order = self.paid_graph()
        episode = old.commercial_episode
        episode.state = IgCommercialEpisode.State.ACTIVE
        episode.intended_order = None
        episode.save(update_fields=["state", "intended_order", "updated_at"])
        deal = old.deal
        deal.status = IgDeal.Status.AWAITING_PAYMENT
        deal.order = None
        deal.paid_at = None
        deal.paid_amount = Decimal("0")
        deal.payment_truth = IgDeal.PaymentTruth.PENDING
        deal.save(update_fields=["status", "order", "paid_at", "paid_amount", "payment_truth", "updated_at"])
        self.assert_denied("checkout_episode_converted")

    def test_paid_attempt_with_lagging_deal_and_episode_cannot_same_digest_replay(self):
        first = self.quote()
        attempt = PaymentAttempt.objects.create(fingerprint="lagging-paid-attempt", full_name="Paid buyer",
            phone="+380501112233", city="Kyiv", np_office="Branch 1", pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PAID, gross_amount=first.quoted_total, payable_amount=first.quoted_total,
            payment_amount=first.quoted_total, paid_amount=first.quoted_total)
        first.payment_attempt = attempt
        first.status = IgCheckoutProposal.Status.INVOICE_CREATED
        first.save(update_fields=["payment_attempt", "status", "updated_at"])
        self.assert_denied("checkout_episode_converted")

    def test_foreign_explicit_deal_is_rejected_without_new_owner(self):
        other = IgClient.get_or_create_for_sender("checkout-other-deal-owner")
        deal = IgDeal.objects.create(client=other, amount=Decimal("900.00"))
        self.assert_denied("invalid_deal", deal=deal)

    def test_explicit_fresh_legacy_deal_can_bind_current_unbound_episode(self):
        deal = IgDeal.objects.create(client=self.client_row, amount=Decimal("900.00"))
        first = self.quote(deal=deal)
        retry = self.quote(deal=deal)
        self.assertEqual((first.pk, first.deal_id), (retry.pk, deal.pk))
        self.assertEqual(IgDeal.objects.count(), 1)
        self.client_row.refresh_from_db()
        self.assertEqual(first.commercial_episode_id, self.client_row.current_commercial_episode_id)
