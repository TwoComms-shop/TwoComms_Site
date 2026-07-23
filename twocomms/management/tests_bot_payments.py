"""Тести Phase 5 / Task 16 — формування посилання на оплату Monobank для угоди.

Повний автопілот (Q1): бот сам створює invoice через acquiring-токен. Замовлення
НЕ створюється тут (Q2) — лише invoice на IgDeal; статус підхопить вебхук (Task 17).
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from management.models import (
    IgClient,
    IgDeal,
    IgDealItem,
    IgMetaEventLog,
    IgPaymentEvent,
    IgPaymentProjection,
)
from management.services import bot_payments
from management.services.bot_payment_truth import client_has_verified_payment


class CreatePaymentLinkTests(TestCase):
    def setUp(self):
        self.c = IgClient.get_or_create_for_sender("pay1")
        self.deal = IgDeal.objects.create(client=self.c, pay_type=IgDeal.PayType.ONLINE_FULL)
        IgDealItem.objects.create(deal=self.deal, title="Худі Kharkiv", qty=1, unit_price=Decimal("950"))
        self.deal.recalc_total()

    def _payload(self, mock_api):
        ca = mock_api.call_args
        return ca.kwargs.get("json_payload") if ca.kwargs.get("json_payload") else ca.args[2]

    @patch("storefront.views.monobank._monobank_api_request")
    def test_creates_link_full(self, mock_api):
        mock_api.return_value = {"invoiceId": "inv_1", "pageUrl": "https://pay/inv_1"}
        res = bot_payments.create_payment_link(self.deal)
        self.assertTrue(res["ok"])
        self.assertEqual(res["invoice_url"], "https://pay/inv_1")
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.invoice_id, "inv_1")
        self.assertEqual(self.deal.status, IgDeal.Status.AWAITING_PAYMENT)
        self.assertEqual(self.deal.payment_status, "checking")
        self.assertEqual(self._payload(mock_api)["amount"], 95000)

    @patch("storefront.views.monobank._monobank_api_request")
    def test_prepay_amount_is_200(self, mock_api):
        mock_api.return_value = {"result": {"invoiceId": "inv_2", "pageUrl": "https://pay/2"}}
        self.deal.pay_type = IgDeal.PayType.PREPAY_200
        self.deal.save()
        bot_payments.create_payment_link(self.deal)
        self.assertEqual(self._payload(mock_api)["amount"], 20000)

    @patch("storefront.views.monobank._monobank_api_request")
    def test_idempotent_reuse(self, mock_api):
        self.deal.invoice_id = "X"
        self.deal.invoice_url = "https://u"
        self.deal.save()
        res = bot_payments.create_payment_link(self.deal)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("reused"))
        self.assertEqual(mock_api.call_count, 0)

    @patch("storefront.views.monobank._monobank_api_request")
    def test_api_error_leaves_deal_clean(self, mock_api):
        from storefront.views.monobank import MonobankAPIError

        mock_api.side_effect = MonobankAPIError("fail")
        res = bot_payments.create_payment_link(self.deal)
        self.assertFalse(res["ok"])
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.invoice_id, "")

    @patch("storefront.views.monobank._monobank_api_request")
    def test_invoice_creation_cannot_downgrade_concurrently_confirmed_projection(self, mock_api):
        mock_api.return_value = {"invoiceId": "inv_race", "pageUrl": "https://pay/race"}
        IgPaymentProjection.objects.create(
            deal=self.deal,
            client=self.c,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("950"),
        )
        self.deal.payment_truth = IgDeal.PaymentTruth.CONFIRMED
        self.deal.payment_status = "paid"
        self.deal.status = IgDeal.Status.PAID
        self.deal.save()

        self.assertTrue(bot_payments.create_payment_link(self.deal, force=True)["ok"])

        self.deal.refresh_from_db()
        projection = IgPaymentProjection.objects.get(deal=self.deal)
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertEqual(self.deal.status, IgDeal.Status.PAID)


class ApplyPaymentStatusTests(TestCase):
    def setUp(self):
        self.c = IgClient.get_or_create_for_sender("apy1")
        self.deal = IgDeal.objects.create(
            client=self.c, pay_type=IgDeal.PayType.ONLINE_FULL, amount=Decimal("950"),
            invoice_id="inv_apy", status=IgDeal.Status.AWAITING_PAYMENT,
        )

    def test_success_marks_paid_and_stage(self):
        bot_payments.apply_payment_status(
            self.deal, "success",
            payload={"status": "success", "amount": 95000, "finalAmount": 95000},
        )
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.status, IgDeal.Status.PAID)
        self.assertEqual(self.deal.payment_status, "paid")
        self.assertIsNotNone(self.deal.paid_at)
        self.c.refresh_from_db()
        self.assertEqual(self.c.stage, IgClient.Stage.PAID)

    def test_prepay_marks_prepaid(self):
        self.deal.pay_type = IgDeal.PayType.PREPAY_200
        self.deal.save()
        bot_payments.apply_payment_status(
            self.deal, "success",
            payload={"status": "success", "amount": 20000, "finalAmount": 20000},
        )
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.payment_status, "prepaid")

    def test_hold_is_authorized_but_not_confirmed_paid(self):
        bot_payments.apply_payment_status(
            self.deal,
            "hold",
            payload={"status": "hold", "amount": 95000, "finalAmount": 95000},
        )

        self.deal.refresh_from_db()
        self.c.refresh_from_db()
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.PENDING)
        self.assertEqual(self.deal.payment_status, "checking")
        self.assertIsNone(self.deal.paid_at)
        self.assertFalse(client_has_verified_payment(self.c))
        self.assertNotEqual(self.c.stage, IgClient.Stage.PAID)

    def test_duplicate_hold_then_success_promotes_once(self):
        hold = {
            "status": "hold",
            "amount": 95000,
            "finalAmount": 95000,
            "modifiedDate": "2026-07-23T10:00:00Z",
        }
        success = {
            **hold,
            "status": "success",
            "modifiedDate": "2026-07-23T10:01:00Z",
        }

        bot_payments.apply_payment_status(self.deal, "hold", payload=hold)
        bot_payments.apply_payment_status(self.deal, "hold", payload=hold)
        bot_payments.apply_payment_status(self.deal, "success", payload=success)

        self.deal.refresh_from_db()
        self.c.refresh_from_db()
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertTrue(client_has_verified_payment(self.c))
        self.assertEqual(self.c.stage, IgClient.Stage.PAID)
        self.assertEqual(IgPaymentEvent.objects.filter(deal=self.deal).count(), 2)

    def test_failure_marks_unpaid(self):
        bot_payments.apply_payment_status(self.deal, "failure")
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.payment_status, "unpaid")
        self.assertEqual(self.deal.status, IgDeal.Status.AWAITING_PAYMENT)

    def test_provider_success_heals_missing_paid_timestamp_on_legacy_paid_row(self):
        self.deal.status = IgDeal.Status.PAID
        self.deal.payment_status = "paid"
        self.deal.paid_at = None
        self.deal.save(update_fields=["status", "payment_status", "paid_at", "updated_at"])

        bot_payments.apply_payment_status(
            self.deal, "success",
            payload={"status": "success", "amount": 95000, "finalAmount": 95000},
        )

        self.deal.refresh_from_db()
        self.assertIsNotNone(self.deal.paid_at)

    def test_duplicate_provider_payload_creates_one_append_only_event(self):
        payload = {
            "status": "success",
            "amount": 95000,
            "finalAmount": 95000,
            "modifiedDate": "2026-07-23T10:00:00Z",
        }

        bot_payments.apply_payment_status(self.deal, "success", payload=payload)
        bot_payments.apply_payment_status(self.deal, "success", payload=payload)

        self.assertEqual(IgPaymentEvent.objects.filter(deal=self.deal).count(), 1)
        event = IgPaymentEvent.objects.get(deal=self.deal)
        self.assertEqual(event.provider_status, "success")
        self.assertEqual(event.final_amount, Decimal("950.00"))
        self.assertNotIn("paymentInfo", event.evidence)
        event.source = "tampered"
        with self.assertRaisesMessage(ValueError, "append-only"):
            event.save()
        with self.assertRaisesMessage(ValueError, "append-only"):
            event.delete()
        with self.assertRaisesMessage(ValueError, "append-only"):
            IgPaymentEvent.objects.filter(pk=event.pk).update(source="tampered")
        with self.assertRaisesMessage(ValueError, "append-only"):
            IgPaymentEvent.objects.filter(pk=event.pk).delete()

    def test_success_with_wrong_or_missing_amount_fails_closed(self):
        for payload in (
            {"status": "success"},
            {"status": "success", "amount": 94999, "finalAmount": 94999},
        ):
            bot_payments.apply_payment_status(self.deal, "success", payload=payload)

        self.deal.refresh_from_db()
        self.c.refresh_from_db()
        projection = IgPaymentProjection.objects.get(deal=self.deal)
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.UNVERIFIED)
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.UNVERIFIED)
        self.assertIsNone(self.deal.paid_at)
        self.assertFalse(client_has_verified_payment(self.c))
        self.assertEqual(
            list(IgPaymentEvent.objects.filter(deal=self.deal).values_list("amount_valid", flat=True)),
            [False, False],
        )

    def test_older_processing_event_cannot_downgrade_confirmed_payment(self):
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={
                "status": "success",
                "amount": 95000,
                "finalAmount": 95000,
                "modifiedDate": "2026-07-23T10:00:00Z",
            },
        )
        bot_payments.apply_payment_status(
            self.deal,
            "processing",
            payload={
                "status": "processing",
                "amount": 95000,
                "modifiedDate": "2026-07-23T09:59:00Z",
            },
        )

        self.deal.refresh_from_db()
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertEqual(self.deal.payment_status, "paid")
        self.assertTrue(client_has_verified_payment(self.c))
        self.assertEqual(IgPaymentEvent.objects.filter(deal=self.deal).count(), 2)

    def test_partial_refund_preserves_paid_truth_and_records_net_amounts(self):
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={"status": "success", "amount": 95000, "finalAmount": 95000},
        )
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={
                "status": "success",
                "amount": 95000,
                "finalAmount": 70000,
                "cancelList": [{"status": "success", "amount": 25000}],
            },
        )

        self.deal.refresh_from_db()
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.PARTIALLY_REFUNDED)
        self.assertEqual(self.deal.paid_amount, Decimal("950.00"))
        self.assertEqual(self.deal.refunded_amount, Decimal("250.00"))
        self.assertTrue(client_has_verified_payment(self.c))
        self.c.refresh_from_db()
        self.assertEqual(self.c.purchases_count, 1)
        self.assertEqual(self.c.total_spent, Decimal("700.00"))

    def test_reversed_payment_is_not_paid_truth_and_history_remains(self):
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={"status": "success", "amount": 95000, "finalAmount": 95000},
        )
        self.deal.refresh_from_db()
        paid_at = self.deal.paid_at

        bot_payments.apply_payment_status(
            self.deal,
            "reversed",
            payload={"status": "reversed", "amount": 95000, "finalAmount": 0},
        )

        self.deal.refresh_from_db()
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.REVERSED)
        self.assertEqual(self.deal.payment_status, "reversed")
        self.assertEqual(self.deal.paid_at, paid_at)
        self.assertEqual(self.deal.refunded_amount, Decimal("950.00"))
        self.assertFalse(client_has_verified_payment(self.c))
        self.assertEqual(IgPaymentEvent.objects.filter(deal=self.deal).count(), 2)
        log = IgMetaEventLog.objects.get(deal=self.deal, event_name="Refund")
        self.assertEqual(log.reason, "refund_feedback_requires_explicit_policy")
        self.assertIn(log.status, {IgMetaEventLog.Status.DISABLED, IgMetaEventLog.Status.SKIPPED})

    def test_full_refund_from_final_amount_is_not_paid_conversion(self):
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={"status": "success", "amount": 95000, "finalAmount": 95000},
        )
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={
                "status": "success",
                "amount": 95000,
                "finalAmount": 0,
                "cancelList": [{"status": "success", "amount": 95000}],
            },
        )

        self.deal.refresh_from_db()
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.REFUNDED)
        self.assertEqual(self.deal.payment_status, "refunded")
        self.assertEqual(self.deal.refunded_amount, Decimal("950.00"))
        self.assertFalse(client_has_verified_payment(self.c))

    def test_terminal_truth_cannot_be_resurrected_by_stale_success(self):
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={
                "status": "success", "amount": 95000, "finalAmount": 95000,
                "modifiedDate": "2026-07-23T10:00:00Z",
            },
        )
        bot_payments.apply_payment_status(
            self.deal,
            "reversed",
            payload={
                "status": "reversed", "amount": 95000, "finalAmount": 0,
                "modifiedDate": "2026-07-23T10:01:00Z",
            },
        )
        for payload in (
            {
                "status": "success", "amount": 95000, "finalAmount": 95000,
                "modifiedDate": "2026-07-23T10:01:00Z",
            },
            {"status": "success", "amount": 95000, "finalAmount": 95000},
        ):
            bot_payments.apply_payment_status(self.deal, "success", payload=payload)

        projection = IgPaymentProjection.objects.get(deal=self.deal)
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.REVERSED)
        self.assertFalse(client_has_verified_payment(self.c))

    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.bot_orders.send_text")
    def test_reversal_after_order_blocks_shipment_and_corrects_aggregates(
        self, mock_send, mock_notify
    ):
        from management.services import bot_orders

        self.deal.np_full_name = "Іван Іванов"
        self.deal.np_phone = "0931112233"
        self.deal.np_city = "Київ"
        self.deal.np_office = "Відділення 1"
        self.deal.save()
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={"status": "success", "amount": 95000, "finalAmount": 95000},
        )
        self.deal.refresh_from_db()
        self.assertIsNotNone(self.deal.order_id)
        self.c.refresh_from_db()
        self.assertEqual(self.c.purchases_count, 1)
        self.assertEqual(self.c.total_spent, Decimal("950.00"))

        order = self.deal.order
        order.status = "ship"
        order.tracking_number = "20450000000000"
        order.save(update_fields=["status", "tracking_number"])
        bot_payments.apply_payment_status(
            self.deal,
            "reversed",
            payload={"status": "reversed", "amount": 95000, "finalAmount": 0},
        )

        order.refresh_from_db()
        self.c.refresh_from_db()
        self.assertEqual(order.payment_status, "unpaid")
        self.assertTrue(order.payment_payload["ig_payment_reconciliation"]["automatic_fulfillment_blocked"])
        self.assertEqual(self.c.purchases_count, 0)
        self.assertEqual(self.c.total_spent, Decimal("0.00"))
        self.assertFalse(self.c.conversion_flags["is_buyer"])
        self.assertEqual(bot_orders.notify_shipped_deals(), 0)
        mock_send.assert_not_called()
        self.assertTrue(mock_notify.called)

    def test_failed_order_reconciliation_rolls_back_event_and_is_retryable(self):
        self.deal.np_full_name = "Іван Іванов"
        self.deal.np_phone = "0931112233"
        self.deal.np_city = "Київ"
        self.deal.np_office = "Відділення 1"
        self.deal.save()
        bot_payments.apply_payment_status(
            self.deal,
            "success",
            payload={"status": "success", "amount": 95000, "finalAmount": 95000},
        )
        reversal = {"status": "reversed", "amount": 95000, "finalAmount": 0}

        with patch("orders.models.Order.save", side_effect=RuntimeError("db unavailable")):
            with self.assertRaisesMessage(RuntimeError, "db unavailable"):
                bot_payments.apply_payment_status(self.deal, "reversed", payload=reversal)

        projection = IgPaymentProjection.objects.get(deal=self.deal)
        self.deal.refresh_from_db()
        self.c.refresh_from_db()
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertEqual(self.c.purchases_count, 1)
        self.assertEqual(self.c.total_spent, Decimal("950.00"))
        self.assertEqual(IgPaymentEvent.objects.filter(deal=self.deal).count(), 1)

        bot_payments.apply_payment_status(self.deal, "reversed", payload=reversal)

        projection.refresh_from_db()
        self.deal.order.refresh_from_db()
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.REVERSED)
        self.assertEqual(self.deal.order.payment_status, "unpaid")

    def test_projection_reconciliation_repairs_failed_myisam_mirror(self):
        payload = {"status": "success", "amount": 95000, "finalAmount": 95000}
        with patch(
            "management.services.bot_payments._sync_legacy_payment_mirror",
            side_effect=RuntimeError("legacy table unavailable"),
        ):
            bot_payments.apply_payment_status(self.deal, "success", payload=payload)

        projection = IgPaymentProjection.objects.get(deal=self.deal)
        self.deal.refresh_from_db()
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertTrue(projection.needs_reconciliation)
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.UNVERIFIED)

        self.assertEqual(bot_payments.reconcile_payment_projections(limit=1), 1)

        projection.refresh_from_db()
        self.deal.refresh_from_db()
        self.c.refresh_from_db()
        self.assertFalse(projection.needs_reconciliation)
        self.assertIsNotNone(projection.reconciled_at)
        self.assertEqual(self.deal.payment_truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertEqual(self.c.purchases_count, 1)
        self.assertEqual(self.c.total_spent, Decimal("950.00"))

    @patch("storefront.views.monobank._monobank_api_request")
    def test_poll_deal_status_applies(self, mock_api):
        mock_api.return_value = {"status": "success", "amount": 95000, "finalAmount": 95000}
        st = bot_payments.poll_deal_status(self.deal)
        self.assertEqual(st, "success")
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.status, IgDeal.Status.PAID)

    @patch("management.services.bot_payments.poll_deal_status")
    def test_handle_webhook_invoice_found(self, mock_poll):
        ok = bot_payments.handle_webhook_invoice("inv_apy")
        self.assertTrue(ok)
        self.assertEqual(mock_poll.call_count, 1)

    def test_handle_webhook_invoice_not_found(self):
        self.assertFalse(bot_payments.handle_webhook_invoice("nope"))


class PollCommandTests(TestCase):
    def test_command_runs(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("poll_ig_deal_payments", stdout=out)
        self.assertIn("Оплачено угод", out.getvalue())
