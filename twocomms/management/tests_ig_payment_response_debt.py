"""Persist customer payment coverage separately from canonical managerial review.

Integration fixtures exercise real sealed-source/lease and receipt integrity;
root runs these on the disposable databases. No provider/transport is used.
"""
from copy import deepcopy

from django.test import TransactionTestCase

from management import tests_ig_revision_execution as fixtures
from management.models import IgPaymentConfirmationReview, IgRevisionDeliveryEffect
from management.services.bot_payment_truth import client_has_verified_payment
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import parse_structured_response
from management.services.ig_response_debt import record_response_coverage, response_coverage
from management.services.ig_response_plan import build_response_plan


class PaymentResponseDebtTests(TransactionTestCase):
    reset_sequences = True
    _collecting_revision = fixtures.RevisionExecutionTests._collecting_revision
    _ready_revision = fixtures.RevisionExecutionTests._ready_revision

    def _source(self, sender, mid, text="Я оплатив"):
        return fixtures.RevisionExecutionTests._source(self, sender, mid, text)

    def setUp(self):
        self.source, self.revision, self.token = self._ready_revision(sender="payment-coverage-owner")
        self.review = IgPaymentConfirmationReview.objects.create(client=self.source.client,
            dedupe_key=f"source-bound-review:{self.source.pk}", watermark_message_id=self.source.pk,
            evidence={"messages": [{"message_id": self.source.pk, "role": "user", "text": self.source.text}]})
        plan = build_response_plan(preferences={}, readiness={"missing": ["product"]},
            context=ReplyTruthContext(), sources=self.revision.bundle_snapshot["sources"])
        response = parse_structured_response({"reply_text": "Ви повідомили про оплату. Оплата очікує перевірки.", "controls": []})
        self.coverage = plan.coverage(response)

    def test_ack_coverage_is_durable_but_manager_review_and_money_remain_unresolved(self):
        self.assertTrue(record_response_coverage(self.revision.pk, self.token, self.coverage))
        self.revision.refresh_from_db()
        self.review.refresh_from_db()
        self.source.client.refresh_from_db()
        receipt = response_coverage(self.revision)
        self.assertEqual(receipt["covered"], [f"{self.source.pk}:payment:claim"])
        self.assertEqual(receipt["remaining"], [])
        self.assertEqual(receipt["disposition"], "complete")
        self.assertEqual(receipt["payment_verification"], "unresolved")
        self.assertEqual(self.review.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertIsNone(self.review.confirmed_at)
        self.assertIsNone(self.review.order_id)
        self.assertFalse(client_has_verified_payment(self.source.client))
        self.assertFalse(IgRevisionDeliveryEffect.objects.filter(revision=self.revision).exists())
        self.assertTrue(record_response_coverage(self.revision.pk, self.token, self.coverage))
        self.assertEqual(IgPaymentConfirmationReview.objects.filter(client=self.source.client).count(), 1)

    def test_nonpayment_or_foreign_source_marker_is_rejected_without_receipt_write(self):
        for obligation in (f"{self.source.pk}:size", f"{self.source.pk + 1000}:payment:claim"):
            invalid = {**self.coverage, "covered": [obligation]}
            self.assertFalse(record_response_coverage(self.revision.pk, self.token, invalid))
            self.revision.refresh_from_db()
            self.assertNotIn("response_coverage", self.revision.action_receipts)

    def test_marker_cannot_set_verified_paid_or_new_waiting_state(self):
        for marker in ("verified", "paid", "pending", True, {"paid": True}):
            self.assertFalse(record_response_coverage(self.revision.pk, self.token,
                {**self.coverage, "payment_verification": marker}))
        self.revision.refresh_from_db()
        self.assertNotIn("response_coverage", self.revision.action_receipts)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, IgPaymentConfirmationReview.Status.PENDING)

    def test_receipt_digest_covers_unresolved_marker_and_detects_tampering(self):
        self.assertTrue(record_response_coverage(self.revision.pk, self.token, self.coverage))
        self.revision.refresh_from_db()
        tampered = deepcopy(self.revision.action_receipts)
        tampered["response_coverage"]["payment_verification"] = "verified"
        self.revision.action_receipts = tampered
        self.assertEqual(response_coverage(self.revision), {"remaining": ["coverage_unverified"], "disposition": "manual"})

    def test_changed_owned_source_and_lost_lease_reject_payment_receipt(self):
        original = self.source.text
        self.source.text = "Я ще не оплатив"
        self.source.save(update_fields=["text"])
        self.assertFalse(record_response_coverage(self.revision.pk, self.token, self.coverage))
        self.source.text = original
        self.source.save(update_fields=["text"])
        self.assertFalse(record_response_coverage(self.revision.pk, "foreign-claim", self.coverage))
        self.revision.refresh_from_db()
        self.assertNotIn("response_coverage", self.revision.action_receipts)
