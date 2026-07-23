"""Phase 10 / Task 25 — сквозний e2e потік IG-бота (без мережі, з моками).

Перевіряє зчеплення фаз: шер поста → черга з медіа; угода+лінк → оплата (вебхук
→ pull-verify) → автостворення замовлення (source=manual, sale_source=Instagram).
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from management.models import IgClient, IgDeal, InstagramBotMessage, InstagramBotSettings
from management.services import bot_orders, bot_payments
from management.services import instagram_bot as bot
from orders.models import Order


class EndToEndFlowTests(TestCase):
    def setUp(self):
        s = InstagramBotSettings.load()
        s.is_enabled = True
        s.allowed_senders = ""
        s.save()
        self.s = s
        from storefront.models import Category, Product, ProductStatus

        cat = Category.objects.create(name="Худі", slug="hudi-e2e")
        self.product = Product.objects.create(
            title="Худі Kharkiv", slug="hk-e2e", category=cat, price=950,
            status=ProductStatus.PUBLISHED,
        )

    def test_shared_post_enqueues_with_media_and_creates_client(self):
        payload = {"entry": [{"messaging": [{
            "sender": {"id": "e2e1"},
            "message": {"mid": "e2em1", "attachments": [
                {"type": "share", "payload": {"url": "https://cdn/post.jpg"}}
            ]},
        }]}]}
        self.assertEqual(bot.handle_webhook_payload(self.s, payload), 1)
        m = InstagramBotMessage.objects.get(mid="e2em1")
        self.assertIn("post.jpg", m.attachments)
        self.assertTrue(IgClient.objects.filter(igsid="e2e1").exists())

    @patch("storefront.views.monobank._monobank_api_request")
    def test_full_payment_to_order(self, mock_api):
        c = IgClient.get_or_create_for_sender("e2e2")
        # 1) Формуємо угоду + посилання на оплату.
        mock_api.return_value = {"invoiceId": "e2einv", "pageUrl": "https://pay/e2e"}
        res = bot_orders.create_deal_and_link(c, pay_type="full", product_id=self.product.id, size="M")
        self.assertTrue(res["ok"])
        deal = IgDeal.objects.get(client=c)
        self.assertEqual(deal.invoice_id, "e2einv")
        self.assertEqual(deal.amount, Decimal("950"))

        # Дані доставки (зібрані в діалозі).
        deal.np_full_name = "Іван Іванов"
        deal.np_phone = "0931112233"
        deal.np_city = "Київ"
        deal.np_office = "Відділення 1"
        deal.save()

        # 2) Оплата підтверджена → вебхук → pull-verify (status success).
        mock_api.return_value = {"status": "success", "amount": 95000, "finalAmount": 95000}
        self.assertTrue(bot_payments.handle_webhook_invoice("e2einv"))

        # 3) Замовлення створено автоматично.
        deal.refresh_from_db()
        self.assertEqual(deal.status, IgDeal.Status.ORDER_CREATED)
        self.assertIsNotNone(deal.order_id)
        order = deal.order
        self.assertEqual(order.sale_source, "Instagram")
        self.assertEqual(order.source, "manual")
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.total_sum, Decimal("950"))
        self.assertEqual(order.items.count(), 1)
        c.refresh_from_db()
        self.assertEqual(c.stage, IgClient.Stage.ORDER_CREATED)
        self.assertEqual(c.purchases_count, 1)
