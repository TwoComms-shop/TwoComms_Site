"""Technical review episodes cannot manufacture repeat purchases."""
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgClient, IgCommercialEpisode, IgCommercialEpisodeEvent, IgDeal, IgPaymentConfirmationReview, IgPaymentReviewDecision
from management.services.ig_analysis_v2 import _prior_purchase_count, normalize_analysis_v2
from management.services.ig_journey_snapshot import build_journey_snapshot
from orders.models import Order


class PurchaseHistoryTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.objects.create(igsid="purchase-history-owner", purchases_count=99)
        self.order = Order.objects.create(order_number="PURCHASE-HISTORY-ONE", full_name="Test", phone="0",
            total_sum=Decimal("850.00"), payment_status="paid", status="done")
        self.canonical = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:canonical", status="confirmed", order=self.order,
            evidence={"claim_anchor": "a" * 64})
        IgPaymentReviewDecision.objects.create(review=self.canonical, client=self.buyer,
            decision="manager_verified", verification_source="manager", verification_scope="full_payment",
            confirmed_amount=Decimal("970.00"), currency="UAH", actor_source="management_user",
            actor_external_id="123", amount_source="manager_input")
        self.first = self.episode(1, state="fulfilled", current=False, intended_order=self.order,
            primary_payment_review=self.canonical)
        self.duplicates = []
        for number in (2, 3):
            review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
                dedupe_key=f"purchase-history:duplicate:{number}", status="superseded", order=self.order,
                superseded_by=self.canonical, evidence={"claim_anchor": "a" * 64})
            self.duplicates.append(self.episode(number, state="lost", current=False,
                primary_payment_review=review, outcome="superseded_duplicate_payment_review"))

    def episode(self, sequence, *, current=True, **kwargs):
        return IgCommercialEpisode.objects.create(client=self.buyer, sequence=sequence,
            open_slot=1 if current else None, materialization_key=f"purchase-history:episode:{sequence}", **kwargs)

    def test_reconciled_duplicates_show_one_purchase_and_preserve_audit(self):
        with CaptureQueriesContext(connection) as queries:
            snapshot = build_journey_snapshot(self.buyer)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        self.assertIsNone(snapshot["current_episode_id"])
        self.assertEqual(snapshot["viewed_episode_id"], self.first.pk)
        self.assertTrue(snapshot["is_history"])
        self.assertEqual(snapshot["episodes"]["visible_total"], 1)
        self.assertEqual(snapshot["episodes"]["hidden_duplicate_episodes"], 2)
        self.assertEqual(snapshot["episodes"]["items"][0]["label"], "Покупка 1")
        payment = next(node for node in snapshot["nodes"] if node["id"] == "payment")
        self.assertNotIn("waiting", payment)
        amount = next(fact for fact in payment["facts"] if fact["id"] == "payment:manager_confirmed_amount")
        self.assertEqual(amount["value"], "970.00 UAH")
        self.assertEqual(amount["source"], "payment_review.current")
        graph_payment = next(node for node in snapshot["graph"]["nodes"] if node["id"] == "guide:payment")
        self.assertEqual(graph_payment["settlement_presentation"]["status"], "paid")
        self.assertEqual(graph_payment["settlement_presentation"]["episode_id"], self.first.pk)
        self.assertEqual(graph_payment["settlement_presentation"]["evidence_refs"], [{"kind": "order", "id": self.order.pk}])
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.buyer).count(), 3)
        self.assertEqual(list(IgCommercialEpisode.objects.filter(client=self.buyer).order_by("sequence").values_list("sequence", flat=True)), [1, 2, 3])
        aliased = build_journey_snapshot(self.buyer, view_episode_id=self.duplicates[-1].pk)
        self.assertEqual(aliased["viewed_episode_id"], self.first.pk)
        self.assertEqual(aliased["resolved_from_episode_id"], self.duplicates[-1].pk)

    def test_first_purchase_own_settlement_is_not_prior_purchase(self):
        self.assertEqual(_prior_purchase_count(self.buyer), 0)
        result = normalize_analysis_v2(parsed={}, legacy_normalized={}, by_id={}, client=self.buyer,
            truth_state={}, analyzed_at=timezone.now())
        self.assertEqual(result.result_values["prior_purchase_count"], 0)
        self.assertEqual(result.result_values["ltv_signal"], "first_purchase")

    def test_later_distinct_pending_purchase_stays_current_and_unpaid(self):
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:later", evidence={"claim_anchor": "b" * 64})
        later = self.episode(4, primary_payment_review=review, repeat_kind="explicit_more")
        self.buyer.current_commercial_episode = later
        self.buyer.save(update_fields=["current_commercial_episode"])
        snapshot = build_journey_snapshot(self.buyer)
        self.assertEqual(snapshot["current_episode_id"], later.pk)
        self.assertEqual(snapshot["viewed_episode_id"], later.pk)
        self.assertFalse(snapshot["is_history"])
        self.assertEqual(snapshot["viewed_episode"]["label"], "Покупка 2")
        self.assertEqual(snapshot["viewed_episode"]["sequence"], 4)
        payment = next(node for node in snapshot["nodes"] if node["id"] == "payment")
        self.assertEqual(payment["waiting"]["evidence_refs"], [{"kind": "payment_review", "id": review.pk}])
        self.assertFalse(any(fact["id"] == "payment:order_payment" for fact in payment["facts"]))
        self.assertEqual(_prior_purchase_count(self.buyer), 1)

    def test_truncated_history_keeps_latest_cycle_without_inventing_global_number(self):
        later = self.episode(4)
        with patch("management.services.ig_journey_snapshot.PURCHASE_HISTORY_LIMIT", 2):
            snapshot = build_journey_snapshot(self.buyer)
        self.assertEqual(snapshot["current_episode_id"], later.pk)
        self.assertEqual(snapshot["viewed_episode"]["label"], "Епізод 4")
        self.assertIsNone(snapshot["viewed_episode"]["purchase_number"])
        self.assertEqual(snapshot["episodes"]["purchase_group_coverage"], "bounded_history")

    def test_paid_exact_order_removes_stale_pending_wait(self):
        self.first.delete()
        pending = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:stale-pending")
        episode = self.episode(4, intended_order=self.order, primary_payment_review=pending)
        snapshot = build_journey_snapshot(self.buyer)
        payment = next(node for node in snapshot["nodes"] if node["id"] == "payment")
        self.assertNotIn("waiting", payment)
        self.assertIn("замовленні", payment["summary"])
        self.assertEqual(snapshot["current_episode_id"], episode.pk)
        self.assertEqual(snapshot["focus"]["node_id"], "guide:fulfillment")

    def test_own_confirmed_unbound_review_is_not_prior_purchase(self):
        buyer = IgClient.objects.create(igsid="purchase-history-own-review")
        review = IgPaymentConfirmationReview.objects.create(client=buyer,
            dedupe_key="purchase-history:own-unbound-review", status="confirmed")
        IgPaymentReviewDecision.objects.create(review=review, client=buyer,
            decision="manager_verified", verification_source="manager", verification_scope="full_payment",
            confirmed_amount="100.00", actor_source="management_user", actor_external_id="123")
        episode = IgCommercialEpisode.objects.create(client=buyer, sequence=1, primary_payment_review=review,
            materialization_key="purchase-history:own-review")
        buyer.current_commercial_episode = episode
        buyer.save(update_fields=["current_commercial_episode"])
        self.assertEqual(_prior_purchase_count(buyer), 0)

    def test_own_confirmed_unbound_deal_is_not_prior_purchase(self):
        buyer = IgClient.objects.create(igsid="purchase-history-own-deal")
        deal = IgDeal.objects.create(client=buyer, amount="100.00", status="paid", payment_status="paid",
            paid_at=timezone.now())
        episode = IgCommercialEpisode.objects.create(client=buyer, sequence=1, deal=deal,
            materialization_key="purchase-history:own-deal")
        buyer.current_commercial_episode = episode
        buyer.save(update_fields=["current_commercial_episode"])
        self.assertEqual(_prior_purchase_count(buyer), 0)

    def test_superseded_review_with_independent_commerce_remains_visible(self):
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:independent", status="superseded", superseded_by=self.canonical)
        independent = self.episode(4, current=False, primary_payment_review=review,
            product_snapshot=[{"product_id": 777}], repeat_evidence_message_ids=[123])
        snapshot = build_journey_snapshot(self.buyer)
        self.assertIn(independent.pk, [item["id"] for item in snapshot["episodes"]["items"]])

    def test_foreign_canonical_review_cannot_hide_owned_episode(self):
        other = IgClient.objects.create(igsid="purchase-history-foreign")
        canonical = IgPaymentConfirmationReview.objects.create(client=other,
            dedupe_key="purchase-history:foreign-canonical", status="confirmed")
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:foreign-supersession", status="superseded", superseded_by=canonical)
        episode = self.episode(4, current=False, primary_payment_review=review)
        snapshot = build_journey_snapshot(self.buyer)
        self.assertIn(episode.pk, [item["id"] for item in snapshot["episodes"]["items"]])

    def test_paid_order_reconciliation_does_not_hide_pending_review(self):
        self.first.delete()
        pending = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:conflicting-payment")
        self.episode(4, intended_order=self.order, primary_payment_review=pending,
            payment_snapshot={"needs_reconciliation": True})
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertIn("waiting", payment)
        self.assertEqual(payment["settlement_presentation"]["status"], "reconciliation")

    def test_confirmed_payment_claim_does_not_assert_full_payment(self):
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:claim-only", status="confirmed")
        IgPaymentReviewDecision.objects.create(review=review, client=self.buyer,
            decision="manager_verified", verification_source="manager", verification_scope="payment_claim",
            actor_source="management_user", actor_external_id="123")
        self.episode(4, primary_payment_review=review)
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertIn("сума не підтверджена", payment["summary"])
        self.assertEqual(payment["settlement_presentation"]["status"], "claim")
        self.assertNotEqual(payment["summary"], "Оплату підтверджено менеджером")
        self.assertFalse(any(fact["id"] == "payment:manager_confirmed_amount" for fact in payment["facts"]))

    def test_confirmed_prepayment_keeps_partial_amount_semantics(self):
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:prepayment-only", status="confirmed")
        IgPaymentReviewDecision.objects.create(review=review, client=self.buyer,
            decision="manager_verified", verification_source="manager", verification_scope="prepayment",
            confirmed_amount="200.00", currency="UAH", actor_source="management_user", actor_external_id="123")
        self.episode(4, primary_payment_review=review)
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertEqual(payment["summary"], "Передоплату підтверджено менеджером")
        self.assertEqual(payment["settlement_presentation"]["status"], "prepaid")
        amount = next(fact for fact in payment["facts"] if fact["id"] == "payment:manager_confirmed_amount")
        self.assertEqual(amount["state"], "partial")
        self.assertEqual(amount["value"], "200.00 UAH")

    def test_full_scope_below_known_total_requires_reconciliation(self):
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:short-full", status="confirmed")
        IgPaymentReviewDecision.objects.create(review=review, client=self.buyer,
            decision="manager_verified", verification_source="manager", verification_scope="full_payment",
            confirmed_amount="200.00", order_total_amount="970.00", currency="UAH",
            actor_source="management_user", actor_external_id="123")
        self.episode(4, primary_payment_review=review)
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertEqual(payment["settlement_presentation"]["status"], "reconciliation")
        self.assertEqual(payment["summary"], "Потрібна звірка оплати")

    def test_full_scope_with_unknown_total_and_positive_remaining_is_not_paid(self):
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:unknown-full", status="confirmed")
        IgPaymentReviewDecision.objects.create(review=review, client=self.buyer,
            decision="manager_verified", verification_source="manager", verification_scope="full_payment",
            confirmed_amount="200.00", currency="UAH", actor_source="management_user", actor_external_id="123")
        self.episode(4, primary_payment_review=review, payment_snapshot={"remaining_amount": "770.00"})
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertEqual(payment["settlement_presentation"]["status"], "reconciliation")

    def test_full_known_total_with_positive_remaining_still_requires_reconciliation(self):
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:known-full-remaining", status="confirmed")
        IgPaymentReviewDecision.objects.create(review=review, client=self.buyer,
            decision="manager_verified", verification_source="manager", verification_scope="full_payment",
            confirmed_amount="970.00", order_total_amount="970.00", currency="UAH",
            actor_source="management_user", actor_external_id="123")
        self.episode(4, primary_payment_review=review, payment_snapshot={"remaining_amount": "30.00"})
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertEqual(payment["settlement_presentation"]["status"], "reconciliation")

    def test_refunded_snapshot_cannot_present_paid_order_as_settled(self):
        self.first.payment_snapshot = {"provider_source": "monobank_projection", "projection_id": 7,
            "provider_truth": "refunded"}
        self.first.save(update_fields=["payment_snapshot"])
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertEqual(payment["settlement_presentation"]["status"], "reconciliation")
        self.assertIn("Повернено", payment["summary"])

    def test_historical_active_lifecycle_is_a_past_event_caption(self):
        IgCommercialEpisodeEvent.objects.create(episode=self.first, dedupe_key="purchase-history:lifecycle",
            event_type="order_bound", from_state="active", to_state="order_created", source="order_resolution",
            evidence={"order_id": self.order.pk})
        snapshot = build_journey_snapshot(self.buyer)
        node = next(node for node in snapshot["graph"]["nodes"] if node["id"] == f"episode_state:{self.first.pk}:active")
        self.assertEqual(node["label"], "Цикл розпочато")
        self.assertTrue(snapshot["is_history"])

    def test_conflicting_order_supersession_is_not_hidden(self):
        other_order = Order.objects.create(order_number="PURCHASE-HISTORY-CONFLICT", full_name="Test", phone="0", total_sum="20")
        review = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:conflict", status="superseded", order=other_order,
            superseded_by=self.canonical, evidence={"claim_anchor": "a" * 64})
        conflicting = self.episode(4, state="lost", current=False, primary_payment_review=review)
        snapshot = build_journey_snapshot(self.buyer)
        self.assertIn(conflicting.pk, [item["id"] for item in snapshot["episodes"]["items"]])

    def test_done_unpaid_order_does_not_remove_pending_wait(self):
        self.order.payment_status = "unpaid"
        self.order.save(update_fields=["payment_status"])
        self.first.delete()
        pending = IgPaymentConfirmationReview.objects.create(client=self.buyer,
            dedupe_key="purchase-history:unpaid-pending")
        self.episode(4, intended_order=self.order, primary_payment_review=pending)
        payment = next(node for node in build_journey_snapshot(self.buyer)["nodes"] if node["id"] == "payment")
        self.assertIn("waiting", payment)
