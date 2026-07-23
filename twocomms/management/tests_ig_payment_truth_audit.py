import json
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import IgClient, IgDeal
from orders.models import Order


class PaymentTruthAuditCommandTests(TestCase):
    def test_json_report_finds_legacy_truth_conflicts_without_writes(self):
        forged_client = IgClient.get_or_create_for_sender("audit-forged-client")
        forged_client.stage = IgClient.Stage.PAID
        forged_client.save(update_fields=["stage", "updated_at"])

        missing_evidence = IgDeal.objects.create(
            client=forged_client,
            status=IgDeal.Status.PAID,
            payment_status="unpaid",
        )
        split_truth = IgDeal.objects.create(
            client=IgClient.get_or_create_for_sender("audit-split-truth"),
            status=IgDeal.Status.DRAFT,
            payment_status="paid",
            paid_at=timezone.now(),
        )
        order = Order.objects.create(full_name="Audit", phone="000")
        order_without_payment = IgDeal.objects.create(
            client=IgClient.get_or_create_for_sender("audit-order-no-payment"),
            status=IgDeal.Status.ORDER_CREATED,
            payment_status="unpaid",
            order=order,
        )
        missing_order = IgDeal.objects.create(
            client=IgClient.get_or_create_for_sender("audit-missing-order"),
            status=IgDeal.Status.ORDER_CREATED,
            payment_status="paid",
            paid_at=timezone.now(),
        )

        stdout = StringIO()
        with CaptureQueriesContext(connection) as captured:
            call_command("audit_ig_payment_truth", "--json", stdout=stdout)
        report = json.loads(stdout.getvalue())

        self.assertTrue(report["read_only"])
        self.assertEqual(report["schema_version"], "2026-07-23.v1")
        self.assertIn(forged_client.id, report["samples"]["client_hard_stage_without_verified_payment"])
        self.assertIn(missing_evidence.id, report["samples"]["deal_hard_status_without_verified_payment"])
        self.assertIn(split_truth.id, report["samples"]["deal_verified_fields_without_hard_status"])
        self.assertIn(order_without_payment.id, report["samples"]["deal_order_without_verified_payment"])
        self.assertIn(missing_order.id, report["samples"]["deal_order_created_without_order"])
        self.assertFalse(
            any(
                query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for query in captured.captured_queries
            )
        )

    def test_sample_limit_does_not_change_full_counts(self):
        for index in range(3):
            client = IgClient.get_or_create_for_sender(f"audit-limit-{index}")
            client.stage = IgClient.Stage.PAID
            client.save(update_fields=["stage", "updated_at"])

        stdout = StringIO()
        call_command("audit_ig_payment_truth", "--json", "--limit", "1", stdout=stdout)
        report = json.loads(stdout.getvalue())

        self.assertEqual(report["counts"]["client_hard_stage_without_verified_payment"], 3)
        self.assertEqual(len(report["samples"]["client_hard_stage_without_verified_payment"]), 1)
