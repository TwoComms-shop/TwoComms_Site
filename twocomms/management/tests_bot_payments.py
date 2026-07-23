"""Тести Phase 5 / Task 16 — формування посилання на оплату Monobank для угоди.

Повний автопілот (Q1): бот сам створює invoice через acquiring-токен. Замовлення
НЕ створюється тут (Q2) — лише invoice на IgDeal; статус підхопить вебхук (Task 17).
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from management.models import IgClient, IgDeal, IgDealItem
from management.services import bot_payments


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


class ApplyPaymentStatusTests(TestCase):
    def setUp(self):
        self.c = IgClient.get_or_create_for_sender("apy1")
        self.deal = IgDeal.objects.create(
            client=self.c, pay_type=IgDeal.PayType.ONLINE_FULL, amount=Decimal("950"),
            invoice_id="inv_apy", status=IgDeal.Status.AWAITING_PAYMENT,
        )

    def test_success_marks_paid_and_stage(self):
        bot_payments.apply_payment_status(self.deal, "success", payload={"status": "success"})
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.status, IgDeal.Status.PAID)
        self.assertEqual(self.deal.payment_status, "paid")
        self.assertIsNotNone(self.deal.paid_at)
        self.c.refresh_from_db()
        self.assertEqual(self.c.stage, IgClient.Stage.PAID)

    def test_prepay_marks_prepaid(self):
        self.deal.pay_type = IgDeal.PayType.PREPAY_200
        self.deal.save()
        bot_payments.apply_payment_status(self.deal, "success")
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.payment_status, "prepaid")

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

        bot_payments.apply_payment_status(self.deal, "success")

        self.deal.refresh_from_db()
        self.assertIsNotNone(self.deal.paid_at)

    @patch("storefront.views.monobank._monobank_api_request")
    def test_poll_deal_status_applies(self, mock_api):
        mock_api.return_value = {"status": "success"}
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
