"""Invoice/readiness gates use the current owner, without erasing old debt."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgClient, IgCommercialEpisode, IgDeal
from management.services.bot_payments import invoice_link_state
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_checkout_readiness import _active_deal_state
from management.services.ig_commercial_episodes import ensure_open_episode_for_locked_client
from management.services.instagram_bot import _has_open_paid_deal, payment_link_allowed


@override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class RepeatCheckoutCurrentOwnerTests(TestCase):
    def setUp(self):
        # Reuse the real catalog/checkout fixture, without inheriting its tests.
        from management.tests_ig_checkout_episode_binding import CheckoutCurrentEpisodeBindingTests
        CheckoutCurrentEpisodeBindingTests.setUp(self)
        self.control = {"paylink": "full", "product": self.product.pk,
                        "size": "M", "fit": self.fit.code}

    def quote(self, **kwargs):
        return create_or_update_proposal(client=self.client_row, pay_type="online_full",
                                         item_specs=[self.item], **kwargs)

    def repeat(self):
        from management.tests_ig_checkout_episode_binding import CheckoutCurrentEpisodeBindingTests
        result = CheckoutCurrentEpisodeBindingTests.repeat(self)
        self.client_row.refresh_from_db()
        return result

    def paid_graph(self):
        from management.tests_ig_checkout_episode_binding import CheckoutCurrentEpisodeBindingTests
        return CheckoutCurrentEpisodeBindingTests.paid_graph(self)

    def allowed(self, text="Так, хочу, оформлюйте"):
        return payment_link_allowed(self.client_row, self.control, text)

    def current_quote(self):
        proposal = self.quote()
        self.client_row.refresh_from_db()
        return proposal

    def debt(self):
        proposal = self.current_quote()
        deal = proposal.deal
        deal.status = IgDeal.Status.PAID
        deal.payment_status = "paid"
        deal.paid_at = timezone.now()
        deal.save(update_fields=["status", "payment_status", "paid_at", "updated_at"])
        self.assertIsNone(deal.order_id)
        return proposal, deal

    def assert_closed(self):
        self.assertTrue(_has_open_paid_deal(self.client_row))
        self.assertFalse(self.allowed())
        self.assertEqual(_active_deal_state(self.client_row),
                         {"status": "unknown", "expires_at": None, "deal_id": None})
        self.http.assert_not_called()

    def test_paid_without_order_stays_original_debt_while_explicit_repeat_can_buy(self):
        old_proposal, old_deal = self.debt()
        old_episode_id = old_proposal.commercial_episode_id
        old_snapshot = deepcopy(old_proposal.revisions.get(revision=old_proposal.revision).snapshot)
        self.assertTrue(_has_open_paid_deal(self.client_row))
        self.assertFalse(self.allowed())
        new_episode = self.repeat()
        self.assertNotEqual(new_episode.pk, old_episode_id)
        self.assertIsNone(new_episode.deal_id)
        self.assertFalse(_has_open_paid_deal(self.client_row))
        self.assertTrue(self.allowed())
        self.assertEqual(_active_deal_state(self.client_row),
                         {"status": "none", "expires_at": None, "deal_id": None})
        new_proposal = self.current_quote()
        self.assertNotEqual(new_proposal.deal_id, old_deal.pk)
        self.assertEqual(new_proposal.commercial_episode_id, new_episode.pk)
        self.assertTrue(self.allowed())
        old_deal.refresh_from_db()
        old_proposal.refresh_from_db()
        self.assertEqual((old_deal.status, old_deal.payment_status, old_deal.order_id),
                         (IgDeal.Status.PAID, "paid", None))
        self.assertIsNotNone(old_deal.paid_at)
        self.assertEqual(old_proposal.commercial_episode_id, old_episode_id)
        self.assertEqual(old_proposal.revisions.get(revision=old_proposal.revision).snapshot, old_snapshot)
        self.http.assert_not_called()

    def test_old_paid_order_and_winner_are_unchanged_by_repeat_gate(self):
        old, generation, attempt, order = self.paid_graph()
        self.client_row.refresh_from_db()
        self.assertFalse(self.allowed())
        self.repeat()
        self.assertTrue(self.allowed())
        old.refresh_from_db()
        generation.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(old.winner_invoice_generation_id, generation.pk)
        self.assertEqual(attempt.order_id, order.pk)
        self.assertEqual(generation.proposal_id, old.pk)
        self.http.assert_not_called()

    def test_current_invoice_expiry_does_not_borrow_newer_unowned_deal(self):
        proposal = self.current_quote()
        expires = timezone.now() - timedelta(minutes=1)
        IgDeal.objects.filter(pk=proposal.deal_id).update(invoice_id="current-expired",
            invoice_url="https://pay.example/current-expired", invoice_expires_at=expires)
        other = IgDeal.objects.create(client=self.client_row, invoice_id="newer-unowned",
            invoice_url="https://pay.example/newer-unowned", invoice_expires_at=timezone.now() + timedelta(minutes=20))
        self.assertGreater(other.pk, proposal.deal_id)
        self.assertEqual(_active_deal_state(self.client_row),
                         {"status": "expired", "expires_at": expires, "deal_id": proposal.deal_id})
        self.assertTrue(self.allowed())

    def test_current_invoice_without_expiry_remains_unknown(self):
        proposal = self.current_quote()
        IgDeal.objects.filter(pk=proposal.deal_id).update(invoice_id="current-no-ttl",
            invoice_url="https://pay.example/current-no-ttl", invoice_expires_at=None)
        self.assertEqual(_active_deal_state(self.client_row),
                         {"status": "unknown", "expires_at": None, "deal_id": proposal.deal_id})

    def test_null_pointer_with_no_episode_is_unknown_and_read_only(self):
        with CaptureQueriesContext(connection) as queries:
            self.assert_closed()
        self.assertTrue(queries.captured_queries)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client_row).exists())
        self.assertFalse(IgDeal.objects.filter(client=self.client_row).exists())

    def test_null_pointer_does_not_find_bound_open_owner(self):
        self.current_quote()
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=None)
        self.client_row.refresh_from_db()
        self.assert_closed()

    def test_stale_loaded_pointer_does_not_authorize_changed_current_owner(self):
        self.current_quote()
        stale = IgClient.objects.get(pk=self.client_row.pk)
        self.repeat()
        self.client_row = stale
        self.assert_closed()

    def test_foreign_pointer_fails_without_falling_back_to_own_deal(self):
        self.current_quote()
        other = IgClient.get_or_create_for_sender("repeat-scope-foreign")
        with transaction.atomic():
            foreign = ensure_open_episode_for_locked_client(IgClient.objects.select_for_update().get(pk=other.pk),
                                                           materialization_prefix="repeat-scope")
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=foreign)
        self.client_row.refresh_from_db()
        self.assert_closed()

    def test_foreign_deal_under_current_owned_episode_is_unknown(self):
        proposal = self.current_quote()
        other = IgClient.get_or_create_for_sender("repeat-scope-foreign-deal")
        foreign_deal = IgDeal.objects.create(client=other)
        IgCommercialEpisode.objects.filter(pk=proposal.commercial_episode_id).update(deal=foreign_deal)
        self.assert_closed()

    def test_dangling_episode_pointer_is_unknown(self):
        self.current_quote()
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode_id=999999999)
        self.client_row.refresh_from_db()
        self.assert_closed()

    def test_current_closed_or_nonactive_owner_fails_closed(self):
        proposal = self.current_quote()
        for state, slot in ((IgCommercialEpisode.State.ACTIVE, None),
                            (IgCommercialEpisode.State.CANCELLED, 1),
                            (IgCommercialEpisode.State.LOST, 1)):
            with self.subTest(state=state, slot=slot):
                IgCommercialEpisode.objects.filter(pk=proposal.commercial_episode_id).update(state=state, open_slot=slot)
                self.assert_closed()

    def test_privacy_erasure_started_after_client_load_is_denied(self):
        self.current_quote()
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=timezone.now())
        self.assert_closed()

    def test_clean_current_scope_still_requires_customer_purchase_evidence(self):
        self.current_quote()
        self.assertFalse(self.allowed("Який розмір?"))
        self.assertTrue(self.allowed())
        self.client_row.intent = IgClient.Intent.CUSTOM_PRINT
        self.assertFalse(self.allowed())
        self.http.assert_not_called()

    def test_lagging_current_money_projection_blocks_invoice(self):
        proposal = self.current_quote()
        for values in ({"paid_amount": "1.00"}, {"paid_at": timezone.now()},
                       {"payment_truth": IgDeal.PaymentTruth.CONFIRMED},
                       {"payment_truth": IgDeal.PaymentTruth.PARTIALLY_REFUNDED},
                       {"payment_truth": IgDeal.PaymentTruth.REFUNDED},
                       {"payment_truth": IgDeal.PaymentTruth.REVERSED}):
            with self.subTest(values=values):
                IgDeal.objects.filter(pk=proposal.deal_id).update(paid_amount=0, paid_at=None,
                    payment_truth=IgDeal.PaymentTruth.UNVERIFIED)
                IgDeal.objects.filter(pk=proposal.deal_id).update(**values)
                self.assertTrue(_has_open_paid_deal(self.client_row))
                self.assertFalse(self.allowed())

    def test_readiness_scope_change_during_invoice_read_omits_old_link(self):
        proposal = self.current_quote()
        IgDeal.objects.filter(pk=proposal.deal_id).update(invoice_id="old-link",
            invoice_url="https://pay.example/old-link", invoice_expires_at=timezone.now() + timedelta(minutes=20))
        def rotate(deal):
            state = invoice_link_state(deal)
            self.repeat()
            return state
        with patch("management.services.bot_payments.invoice_link_state", side_effect=rotate):
            self.assertEqual(_active_deal_state(self.client_row),
                             {"status": "unknown", "expires_at": None, "deal_id": None})

    def test_payment_truth_read_cannot_authorize_after_permission_change(self):
        self.current_quote()
        from management.services import bot_payment_truth
        original = bot_payment_truth.verified_payment_q
        def revoke(*args, **kwargs):
            IgClient.objects.filter(pk=self.client_row.pk).update(
                reply_permission_epoch=self.client_row.reply_permission_epoch + 1)
            return original(*args, **kwargs)
        with patch("management.services.bot_payment_truth.verified_payment_q", side_effect=revoke):
            self.assertFalse(self.allowed())

    def test_unknown_scope_has_no_ambiguous_latest_deal_fallback(self):
        self.current_quote()
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=None)
        self.client_row.refresh_from_db()
        for index in range(2):
            IgDeal.objects.create(client=self.client_row, invoice_id=f"unowned-{index}",
                invoice_url=f"https://pay.example/unowned-{index}", invoice_expires_at=timezone.now() + timedelta(minutes=20))
        self.assert_closed()

    def test_current_cancelled_link_is_not_live_and_does_not_expand_money_truth(self):
        proposal = self.current_quote()
        IgDeal.objects.filter(pk=proposal.deal_id).update(status=IgDeal.Status.CANCELLED,
            invoice_id="cancelled-link", invoice_url="https://pay.example/cancelled-link",
            invoice_expires_at=timezone.now() + timedelta(minutes=20))
        self.assertEqual(_active_deal_state(self.client_row),
                         {"status": "none", "expires_at": None, "deal_id": None})
        self.assertFalse(_has_open_paid_deal(self.client_row))
        self.assertTrue(self.allowed())

    def test_permission_changed_before_read_rejects_stale_caller(self):
        self.current_quote()
        IgClient.objects.filter(pk=self.client_row.pk).update(
            reply_permission_epoch=self.client_row.reply_permission_epoch + 1)
        self.assert_closed()
