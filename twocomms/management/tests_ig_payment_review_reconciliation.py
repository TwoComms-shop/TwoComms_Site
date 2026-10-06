import io
import json
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase

from management.ig_bot_models import IgBotNotification, IgClient, IgPaymentConfirmationReview
from management.services.ig_payment_review import LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF


class PaymentReviewReconciliationCommandTests(TestCase):
    def setUp(self):
        self.client = IgClient.get_or_create_for_sender("payment-review-reconciliation")
        evidence = {
            "amount_evidence": [{"amount": "1760", "message_id": 1133}],
            "media": [{
                "role": "receipt",
                "source_message_id": 1136,
                "url": "https://cdn.test/legacy-receipt.jpg",
            }],
            "order_draft": {"quoted_total": "1760", "currency": "UAH", "items": []},
        }
        self.canonical = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="legacy-command-canonical",
            evidence=evidence,
            watermark_message_id=1136,
        )
        self.duplicate = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="legacy-command-duplicate",
            evidence={
                **evidence,
                "order_draft": {**evidence["order_draft"], "delivery": {"city": "Київ"}},
            },
            watermark_message_id=1141,
        )
        self.unrelated = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="legacy-command-unrelated",
            evidence={
                **evidence,
                "media": [{
                    "role": "receipt",
                    "source_message_id": 1142,
                    "url": "https://cdn.test/other-receipt.jpg",
                }],
            },
            watermark_message_id=1142,
        )
        self.notification = IgBotNotification.objects.create(
            client=self.client,
            event_type="payment_review",
            dedupe_key=self.duplicate.dedupe_key,
        )
        # The repair command is exclusively for the historical-refresh window,
        # never current payment claims.
        IgPaymentConfirmationReview.objects.filter(
            pk__in=[self.canonical.pk, self.duplicate.pk, self.unrelated.pk],
        ).update(created_at=LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF - timedelta(days=10))

    def test_command_is_dry_run_by_default_then_idempotently_applies_exact_receipt_merges(self):
        preview_stdout = io.StringIO()
        call_command(
            "reconcile_ig_payment_reviews",
            "--client-id", str(self.client.pk),
            stdout=preview_stdout,
        )

        preview = json.loads(preview_stdout.getvalue())
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["would_supersede"], 1)
        self.duplicate.refresh_from_db()
        self.assertEqual(self.duplicate.status, IgPaymentConfirmationReview.Status.PENDING)
        self.notification.refresh_from_db()
        self.assertEqual(self.notification.status, IgBotNotification.Status.PENDING)

        apply_stdout = io.StringIO()
        call_command(
            "reconcile_ig_payment_reviews",
            "--apply", "--client-id", str(self.client.pk),
            stdout=apply_stdout,
        )

        applied = json.loads(apply_stdout.getvalue())
        self.assertFalse(applied["dry_run"])
        self.assertEqual(applied["superseded"], 1)
        self.duplicate.refresh_from_db()
        self.unrelated.refresh_from_db()
        self.notification.refresh_from_db()
        self.assertEqual(self.duplicate.status, IgPaymentConfirmationReview.Status.SUPERSEDED)
        self.assertEqual(self.duplicate.superseded_by_id, self.canonical.pk)
        self.assertEqual(self.unrelated.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertEqual(self.notification.status, IgBotNotification.Status.RESOLVED)

        rerun_stdout = io.StringIO()
        call_command(
            "reconcile_ig_payment_reviews",
            "--apply", "--client-id", str(self.client.pk),
            stdout=rerun_stdout,
        )
        self.assertEqual(json.loads(rerun_stdout.getvalue())["superseded"], 0)

    def test_command_does_not_merge_a_transcript_with_two_receipt_sources(self):
        other_client = IgClient.get_or_create_for_sender("payment-review-two-receipts")
        two_receipts = {
            "media": [
                {"role": "receipt", "source_message_id": 2101},
                {"role": "payment_candidate", "source_message_id": 2102},
            ],
            "order_draft": {"quoted_total": "1760", "currency": "UAH", "items": []},
        }
        first = IgPaymentConfirmationReview.objects.create(
            client=other_client,
            dedupe_key="two-receipts-first",
            evidence=two_receipts,
            watermark_message_id=2102,
        )
        second = IgPaymentConfirmationReview.objects.create(
            client=other_client,
            dedupe_key="two-receipts-second",
            evidence={
                **two_receipts,
                "order_draft": {"quoted_total": "1760", "items": []},
            },
            watermark_message_id=2103,
        )
        IgPaymentConfirmationReview.objects.filter(
            pk__in=[first.pk, second.pk],
        ).update(created_at=LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF - timedelta(days=10))

        stdout = io.StringIO()
        call_command(
            "reconcile_ig_payment_reviews",
            "--apply", "--client-id", str(other_client.pk),
            stdout=stdout,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(json.loads(stdout.getvalue())["superseded"], 0)
        self.assertEqual(first.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertEqual(second.status, IgPaymentConfirmationReview.Status.PENDING)

    def test_command_does_not_change_notification_while_it_is_sending(self):
        self.notification.status = IgBotNotification.Status.SENDING
        self.notification.save(update_fields=["status", "updated_at"])

        stdout = io.StringIO()
        call_command(
            "reconcile_ig_payment_reviews",
            "--apply", "--client-id", str(self.client.pk),
            stdout=stdout,
        )

        self.duplicate.refresh_from_db()
        self.notification.refresh_from_db()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["superseded"], 0)
        self.assertEqual(result["skipped_sending_notification"], 1)
        self.assertEqual(self.duplicate.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertEqual(self.notification.status, IgBotNotification.Status.SENDING)

    def test_command_uses_confirmed_order_link_as_legacy_duplicate_canonical(self):
        from orders.models import Order

        order = Order.objects.create(
            full_name="Canonical payment",
            phone="380501234567",
            city="Київ",
            np_office="Відділення №1",
            total_sum="1760.00",
        )
        self.canonical.status = IgPaymentConfirmationReview.Status.CONFIRMED
        self.canonical.order = order
        self.canonical.save(update_fields=["status", "order", "updated_at"])

        stdout = io.StringIO()
        call_command(
            "reconcile_ig_payment_reviews",
            "--apply", "--client-id", str(self.client.pk),
            stdout=stdout,
        )

        self.duplicate.refresh_from_db()
        self.assertEqual(json.loads(stdout.getvalue())["superseded"], 1)
        self.assertEqual(self.duplicate.status, IgPaymentConfirmationReview.Status.SUPERSEDED)
        self.assertEqual(self.duplicate.superseded_by_id, self.canonical.pk)

    def test_command_never_uses_receipt_fallback_for_v2_claims(self):
        source_media = [{"role": "receipt", "source_message_id": 3101}]
        first = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="v2-first",
            evidence={"claim_anchor": "a" * 64, "media": source_media},
            watermark_message_id=3101,
        )
        second = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            dedupe_key="v2-second",
            evidence={"claim_anchor": "b" * 64, "media": source_media},
            watermark_message_id=3102,
        )
        IgPaymentConfirmationReview.objects.filter(
            pk__in=[first.pk, second.pk],
        ).update(created_at=LEGACY_PAYMENT_REVIEW_REPAIR_CUTOFF - timedelta(days=10))

        stdout = io.StringIO()
        call_command(
            "reconcile_ig_payment_reviews",
            "--apply", "--client-id", str(self.client.pk),
            stdout=stdout,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, IgPaymentConfirmationReview.Status.PENDING)
        self.assertEqual(second.status, IgPaymentConfirmationReview.Status.PENDING)
