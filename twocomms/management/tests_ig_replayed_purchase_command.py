import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from management.ig_bot_models import IgCommercialEpisode, IgPaymentConfirmationReview
from management.tests_ig_buyer_truth import BuyerTruthTestMixin


class ReplayedPurchaseRepairTests(BuyerTruthTestMixin, TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user("replay-manager", is_staff=True)
        self.buyer = self._client("replay-command-buyer")
        self.order = self._order("REPLAY-1", total="850", status="done")
        self.canonical = self._review(self.buyer, dedupe="replay:canonical",
                                     status="confirmed", order=self.order)
        self.canonical.evidence = {"claim_anchor": "a" * 64}
        self.canonical.save(update_fields=["evidence"])
        self._manager_decision(self.canonical, amount="970")
        self.first = IgCommercialEpisode.objects.create(
            client=self.buyer, sequence=1, materialization_key="replay:episode:1",
            state="fulfilled", open_slot=None, primary_payment_review=self.canonical, intended_order=self.order)
        self.replay = self._review(self.buyer, dedupe="replay:pending", status="pending")
        self.replay.evidence = {"claim_anchor": "a" * 64}
        self.replay.save(update_fields=["evidence"])
        self.ghost = IgCommercialEpisode.objects.create(
            client=self.buyer, sequence=3, materialization_key="replay:episode:3",
            state="active", open_slot=1, primary_payment_review=self.replay)
        self.buyer.current_commercial_episode = self.ghost
        self.buyer.save(update_fields=["current_commercial_episode"])

    def run_repair(self, *args):
        output = StringIO()
        call_command("reconcile_ig_replayed_purchases", *args, stdout=output)
        return json.loads(output.getvalue())

    def test_preview_is_read_only_apply_is_idempotent_and_keeps_payment_decision(self):
        with patch("management.services.ig_payment_review._queue_payment_review_notification") as outbound:
            preview = self.run_repair("--dry-run")
            self.assertEqual(preview["repairs"][0]["canonical_review_id"], self.canonical.pk)
            self.replay.refresh_from_db()
            self.assertEqual(self.replay.status, "pending")
            self.ghost.refresh_from_db()
            self.assertEqual(self.ghost.open_slot, 1)
            result = self.run_repair("--apply")
            self.assertEqual(result["repairs"][0]["purchases_count"], 1)
            self.assertEqual(result["repairs"][0]["total_spent"], "970.00")
            self.assertEqual(self.run_repair("--apply")["repairs"], [])
            outbound.assert_not_called()
        self.replay.refresh_from_db()
        self.buyer.refresh_from_db()
        self.assertEqual(self.replay.status, "superseded")
        self.assertEqual(self.replay.superseded_by_id, self.canonical.pk)
        self.assertIsNone(self.buyer.current_commercial_episode_id)
        self.assertEqual(self.canonical.decisions.count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "paid")
        self.assertEqual(str(self.order.total_sum), "850.00")

    def test_distinct_claim_and_conflicting_order_remain_pending(self):
        self.replay.evidence = {"claim_anchor": "b" * 64}
        self.replay.save(update_fields=["evidence"])
        self.assertEqual(self.run_repair("--apply")["repairs"], [])
        self.replay.evidence = {"claim_anchor": "a" * 64}
        self.replay.order = self._order("REPLAY-2", total="850", payment_status="unpaid")
        self.replay.save(update_fields=["evidence", "order"])
        self.assertEqual(self.run_repair("--apply")["repairs"], [])
        self.replay.refresh_from_db()
        self.assertEqual(self.replay.status, "pending")
