from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from orders.models import Order, OrderItem
from orders.nova_poshta_documents import (
    TELEGRAM_CREATE_NP_WAYBILL_ACTION,
    TELEGRAM_DELETE_NP_WAYBILL_ACTION,
    NovaPoshtaResolvedPoint,
)
from orders.telegram_notifications import TelegramNotifier
from orders.telegram_status_links import (
    build_order_action_token,
    build_order_status_action_url,
    build_order_status_token,
)
from storefront.models import Category, Product

@override_settings(
    NOVA_POSHTA_FALLBACK_ENABLED=False,
    RATELIMIT_ENABLE=False,
    COMPRESS_ENABLED=False,
    COMPRESS_OFFLINE=False,
)
class TelegramOrderStatusActionTests(TestCase):
    def setUp(self):
        self._merchant_feed_patcher = patch("storefront.signals.generate_google_merchant_feed_task.apply_async")
        self._merchant_feed_patcher.start()
        self.addCleanup(self._merchant_feed_patcher.stop)

        self.category = Category.objects.create(name="Telegram Orders", slug="telegram-orders")
        self.product = Product.objects.create(
            title="Telegram Hoodie",
            slug="telegram-hoodie",
            category=self.category,
            price=1299,
        )

    def _create_order(self, **kwargs):
        payload = {
            "full_name": "Тестовий клієнт",
            "phone": "+380991112233",
            "city": "Київ",
            "np_office": "Відділення №4",
            "total_sum": "1299.00",
            "payment_status": "paid",
            "pay_type": "online_full",
        }
        payload.update(kwargs)
        order = Order.objects.create(**payload)
        OrderItem.objects.create(
            order=order,
            product=self.product,
            title=self.product.title,
            qty=1,
            unit_price="1299.00",
            line_total="1299.00",
        )
        return order

    def test_send_new_order_notification_adds_ship_button(self):
        order = self._create_order()
        notifier = TelegramNotifier(bot_token="token", admin_id="1", async_enabled=False)

        with patch.object(
            TelegramNotifier,
            "send_message",
            return_value=[{"chat": {"id": 1}, "message_id": 22}],
        ) as send_message_mock:
            notifier.send_new_order_notification(order)

        _, kwargs = send_message_mock.call_args
        self.assertIn("reply_markup", kwargs)
        markup = kwargs["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "📦 Створити ТТН НП")
        button = markup["inline_keyboard"][1][0]
        self.assertEqual(button["text"], "🚚 Відправлено + ТТН")
        self.assertEqual(button["url"], build_order_status_action_url(order, "ship"))

    @patch("orders.signals._safe_queue_notification")
    def test_signed_ship_action_updates_status_and_tracking_number(self, _queue_mock):
        order = self._create_order()
        token = build_order_status_token(order.pk, "ship")

        response = self.client.post(
            reverse("telegram_order_status_action", args=[order.pk, "ship"]),
            data={"token": token, "tracking_number": "20450012345678"},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "ship")
        self.assertEqual(order.tracking_number, "20450012345678")
        self.assertContains(response, "переведено у статус")

    def test_invalid_token_is_rejected(self):
        order = self._create_order()

        response = self.client.get(
            reverse("telegram_order_status_action", args=[order.pk, "ship"]),
            {"token": "broken-token"},
            secure=True,
        )

        self.assertEqual(response.status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, "new")
        self.assertEqual(order.tracking_number, None)

    def test_done_order_cannot_be_reverted_to_ship_via_old_telegram_link(self):
        order = self._create_order(status="done", tracking_number="20450000000001")
        token = build_order_status_token(order.pk, "ship")

        response = self.client.get(
            reverse("telegram_order_status_action", args=[order.pk, "ship"]),
            {"token": token},
            secure=True,
        )

        self.assertEqual(response.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.status, "done")
        self.assertEqual(order.tracking_number, "20450000000001")

    def test_staff_payment_status_update_canonicalizes_prepaid(self):
        from storefront.models import UserAction

        order = self._create_order(payment_status="unpaid")
        staff = User.objects.create_user(username="staff-pay", password="pass12345", is_staff=True)
        self.client.force_login(staff)

        response = self.client.post(
            reverse("admin_update_payment_status"),
            data={"order_id": order.pk, "payment_status": "prepaid"},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "prepaid")
        self.assertEqual(response.json()["payment_status_label"], "Внесена передплата")
        self.assertEqual(
            UserAction.objects.filter(action_type="purchase", order_id=order.pk).count(),
            1,
        )

    def test_staff_payment_snapshot_endpoint_merges_legacy_partial_into_prepaid(self):
        order = self._create_order(payment_status="partial", pay_type="prepay_200")
        staff = User.objects.create_user(username="staff-live", password="pass12345", is_staff=True)
        self.client.force_login(staff)

        response = self.client.get(
            reverse("admin_order_payment_snapshots"),
            {"ids": str(order.pk)},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["orders"][str(order.pk)]["payment_status"], "prepaid")
        self.assertEqual(payload["orders"][str(order.pk)]["cod_amount"], "1099.00")

    @patch("orders.signals._safe_queue_notification")
    def test_staff_admin_order_update_uses_shared_helper(self, _queue_mock):
        order = self._create_order()
        staff = User.objects.create_user(username="staffer", password="pass12345", is_staff=True)
        self.client.force_login(staff)

        response = self.client.post(
            reverse("admin_update_order_status"),
            data={
                "order_id": order.pk,
                "status": "ship",
                "tracking_number": "20450012349999",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "success": True,
                "message": 'Статус замовлення змінено на "Відправлено" з ТТН: 20450012349999',
                "status": "ship",
                "tracking_number": "20450012349999",
                "cancellation_reason": None,
                "cancellation_comment": None,
            },
        )
        order.refresh_from_db()
        self.assertEqual(order.status, "ship")
        self.assertEqual(order.tracking_number, "20450012349999")

    @patch("storefront.views.order_actions.telegram_notifier.update_order_notification_message", return_value=True)
    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    @patch("orders.signals._safe_queue_notification")
    def test_guest_order_can_create_nova_poshta_waybill(self, _queue_mock, service_cls, update_message_mock):
        order = self._create_order(
            user=None,
            pay_type="prepay_200",
            payment_status="prepaid",
        )
        token = build_order_action_token(order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION)

        service = service_cls.return_value
        service.is_configured.return_value = True
        service.build_initial_payload.return_value = {
            "recipient_full_name": "Тестовий клієнт",
            "recipient_phone": "+380991112233",
            "recipient_city": "Київ",
            "recipient_settlement_ref": "recipient-settlement-ref",
            "recipient_city_ref": "recipient-city-ref",
            "recipient_warehouse": "Відділення №4",
            "recipient_warehouse_ref": "recipient-warehouse-ref",
            "sender_city": "Харків",
            "sender_settlement_ref": "sender-settlement-ref",
            "sender_city_ref": "sender-city-ref",
            "sender_warehouse": "Відділення №138",
            "sender_warehouse_ref": "sender-warehouse-ref",
            "description": "Одяг бренду TwoComms, Telegram Hoodie",
            "declared_cost": "1299.00",
            "weight": "1.0",
            "seats_amount": "1",
            "length_cm": "30",
            "width_cm": "20",
            "height_cm": "8",
            "cod_amount": "1099.00",
            "payer_type": "Recipient",
            "payment_method": "Cash",
        }
        service.create_waybill.return_value = {
            "tracking_number": "20451234123456",
            "document_ref": "document-ref-1",
            "recipient_ref": "recipient-ref-1",
            "recipient_contact_ref": "recipient-contact-ref-1",
            "recipient_point": NovaPoshtaResolvedPoint(
                city_label="м. Київ, Київ",
                warehouse_label="Відділення №4",
                settlement_ref="recipient-settlement-ref",
                city_ref="recipient-city-ref",
                warehouse_ref="recipient-warehouse-ref",
                warehouse_kind="branch",
            ),
            "sender_point": NovaPoshtaResolvedPoint(
                city_label="м. Харків, Харків",
                warehouse_label="Відділення №138",
                settlement_ref="sender-settlement-ref",
                city_ref="sender-city-ref",
                warehouse_ref="sender-warehouse-ref",
                warehouse_kind="branch",
            ),
            "warnings": [],
        }

        response = self.client.post(
            reverse("telegram_order_np_waybill_action", args=[order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION]),
            data={
                "token": token,
                "recipient_full_name": "Тестовий клієнт",
                "recipient_phone": "+380991112233",
                "recipient_city": "Київ",
                "recipient_settlement_ref": "recipient-settlement-ref",
                "recipient_city_ref": "recipient-city-ref",
                "recipient_warehouse": "Відділення №4",
                "recipient_warehouse_ref": "recipient-warehouse-ref",
                "sender_city": "Харків",
                "sender_settlement_ref": "sender-settlement-ref",
                "sender_city_ref": "sender-city-ref",
                "sender_warehouse": "Відділення №138",
                "sender_warehouse_ref": "sender-warehouse-ref",
                "description": "Одяг бренду TwoComms, Telegram Hoodie",
                "declared_cost": "1299.00",
                "weight": "1.0",
                "seats_amount": "1",
                "length_cm": "30",
                "width_cm": "20",
                "height_cm": "8",
                "cod_amount": "1099.00",
                "payer_type": "Recipient",
                "payment_method": "Cash",
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertIsNone(order.user)
        self.assertEqual(order.status, "ship")
        self.assertEqual(order.tracking_number, "20451234123456")
        self.assertEqual(order.nova_poshta_document_ref, "document-ref-1")
        self.assertEqual(order.nova_poshta_recipient_ref, "recipient-ref-1")
        self.assertEqual(order.nova_poshta_recipient_contact_ref, "recipient-contact-ref-1")
        self.assertEqual(order.np_city_ref, "recipient-city-ref")
        self.assertEqual(order.np_warehouse_ref, "recipient-warehouse-ref")
        update_message_mock.assert_called_once()
        self.assertContains(response, "ТТН 20451234123456 створено")

    @patch("storefront.views.order_actions.telegram_notifier.update_order_notification_message", return_value=True)
    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    @patch("orders.signals._safe_queue_notification")
    def test_guest_order_can_delete_nova_poshta_waybill(self, _queue_mock, service_cls, update_message_mock):
        order = self._create_order(
            status="ship",
            tracking_number="20451234123456",
            nova_poshta_document_ref="00000000-0000-0000-0000-000000000001",
            nova_poshta_recipient_ref="recipient-ref-1",
            nova_poshta_recipient_contact_ref="recipient-contact-ref-1",
        )
        token = build_order_action_token(
            order.pk,
            TELEGRAM_DELETE_NP_WAYBILL_ACTION,
            scope=order.nova_poshta_document_ref,
        )

        service = service_cls.return_value
        service.is_configured.return_value = True
        service.delete_waybill.return_value = {
            "document_ref": "00000000-0000-0000-0000-000000000001",
            "warnings": [],
        }

        response = self.client.post(
            reverse("telegram_order_np_waybill_action", args=[order.pk, TELEGRAM_DELETE_NP_WAYBILL_ACTION]),
            data={"token": token},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        service.delete_waybill.assert_called_once_with("00000000-0000-0000-0000-000000000001")
        order.refresh_from_db()
        self.assertEqual(order.status, "prep")
        self.assertIsNone(order.tracking_number)
        self.assertIsNone(order.nova_poshta_document_ref)
        self.assertIsNone(order.nova_poshta_recipient_ref)
        self.assertIsNone(order.nova_poshta_recipient_contact_ref)
        update_message_mock.assert_called_once()
        self.assertContains(response, "ТТН 20451234123456 видалено")

    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    def test_delete_nova_poshta_waybill_rejects_stale_document_token(self, service_cls):
        order = self._create_order(
            status="ship",
            tracking_number="20451234123456",
            nova_poshta_document_ref="00000000-0000-0000-0000-000000000002",
        )
        token = build_order_action_token(
            order.pk,
            TELEGRAM_DELETE_NP_WAYBILL_ACTION,
            scope="00000000-0000-0000-0000-000000000001",
        )

        response = self.client.post(
            reverse("telegram_order_np_waybill_action", args=[order.pk, TELEGRAM_DELETE_NP_WAYBILL_ACTION]),
            data={"token": token},
            secure=True,
        )

        self.assertEqual(response.status_code, 403)
        service_cls.return_value.delete_waybill.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "20451234123456")
        self.assertEqual(order.nova_poshta_document_ref, "00000000-0000-0000-0000-000000000002")

    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    def test_delete_nova_poshta_waybill_rechecks_token_inside_order_lock(self, service_cls):
        order = self._create_order(
            status="ship",
            tracking_number="20451234123456",
            nova_poshta_document_ref="00000000-0000-0000-0000-000000000001",
        )
        service_cls.return_value.is_configured.return_value = True

        with patch(
            "storefront.views.order_actions.verify_order_action_token",
            side_effect=[True, False],
        ):
            response = self.client.post(
                reverse("telegram_order_np_waybill_action", args=[order.pk, TELEGRAM_DELETE_NP_WAYBILL_ACTION]),
                data={"token": "token"},
                secure=True,
            )

        self.assertEqual(response.status_code, 403)
        service_cls.return_value.delete_waybill.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "20451234123456")
        self.assertEqual(order.nova_poshta_document_ref, "00000000-0000-0000-0000-000000000001")

    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    def test_delete_nova_poshta_waybill_bad_token_does_not_reveal_missing_order(self, service_cls):
        response = self.client.get(
            reverse("telegram_order_np_waybill_action", args=[999999, TELEGRAM_DELETE_NP_WAYBILL_ACTION]),
            data={"token": "bad-token"},
            secure=True,
        )

        self.assertEqual(response.status_code, 403)
        service_cls.assert_not_called()

    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    def test_create_waybill_blocked_page_does_not_mint_delete_link(self, service_cls):
        order = self._create_order(
            status="ship",
            tracking_number="20451234123456",
            nova_poshta_document_ref="00000000-0000-0000-0000-000000000001",
        )
        token = build_order_action_token(order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION)
        service_cls.return_value.is_configured.return_value = True

        response = self.client.get(
            reverse("telegram_order_np_waybill_action", args=[order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION]),
            data={"token": token},
            secure=True,
        )

        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, "delete-np-waybill", status_code=409)
        self.assertNotContains(response, "Видалити ТТН", status_code=409)

    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    def test_delete_waybill_page_does_not_poll_create_snapshot(self, service_cls):
        order = self._create_order(
            status="ship",
            tracking_number="20451234123456",
            nova_poshta_document_ref="00000000-0000-0000-0000-000000000001",
        )
        token = build_order_action_token(
            order.pk,
            TELEGRAM_DELETE_NP_WAYBILL_ACTION,
            scope=order.nova_poshta_document_ref,
        )
        service_cls.return_value.is_configured.return_value = True

        response = self.client.get(
            reverse("telegram_order_np_waybill_action", args=[order.pk, TELEGRAM_DELETE_NP_WAYBILL_ACTION]),
            data={"token": token},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "telegram_order_payment_snapshot")

    @patch("storefront.views.order_actions.NovaPoshtaDocumentService")
    def test_waybill_page_restores_saved_postomat_tokens(self, service_cls):
        order = self._create_order(
            np_settlement_ref="recipient-settlement-ref",
            np_city_ref="recipient-city-ref",
            np_warehouse_ref="recipient-warehouse-ref",
            np_office="Поштомат №22, Київ, вул. Тестова, 1",
            payment_status="prepaid",
            pay_type="prepay_200",
        )
        token = build_order_action_token(order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION)

        service = service_cls.return_value
        service.is_configured.return_value = True
        service.build_initial_payload.return_value = {
            "recipient_full_name": "Тестовий клієнт",
            "recipient_phone": "+380991112233",
            "recipient_city": "Київ",
            "recipient_settlement_ref": "recipient-settlement-ref",
            "recipient_city_ref": "recipient-city-ref",
            "recipient_city_token": "city-token-1",
            "recipient_warehouse": "Поштомат №22, Київ, вул. Тестова, 1",
            "recipient_warehouse_ref": "recipient-warehouse-ref",
            "recipient_warehouse_token": "warehouse-token-1",
            "sender_city": "Харків",
            "sender_settlement_ref": "sender-settlement-ref",
            "sender_city_ref": "sender-city-ref",
            "sender_city_token": "sender-city-token-1",
            "sender_warehouse": "Відділення №138",
            "sender_warehouse_ref": "sender-warehouse-ref",
            "sender_warehouse_token": "sender-warehouse-token-1",
            "description": "Одяг бренду TwoComms, Telegram Hoodie",
            "declared_cost": "1299.00",
            "weight": "1.0",
            "seats_amount": "1",
            "length_cm": "30",
            "width_cm": "20",
            "height_cm": "8",
            "cod_amount": "1099.00",
            "payer_type": "Recipient",
            "payment_method": "Cash",
        }

        response = self.client.get(
            reverse("telegram_order_np_waybill_action", args=[order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION]),
            {"token": token},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="recipient_city_token"')
        self.assertContains(response, 'value="city-token-1"', count=1)
        self.assertContains(response, 'name="recipient_warehouse_token"')
        self.assertContains(response, 'value="warehouse-token-1"', count=1)
        self.assertContains(response, "Поштомат №22, Київ, вул. Тестова, 1")

    def test_waybill_payment_snapshot_returns_live_payment_values(self):
        order = self._create_order(pay_type="prepay_200", payment_status="paid")
        token = build_order_action_token(order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION)

        response = self.client.get(
            reverse("telegram_order_payment_snapshot", args=[order.pk, TELEGRAM_CREATE_NP_WAYBILL_ACTION]),
            {"token": token},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["snapshot"]["payment_status"], "paid")
        self.assertEqual(payload["snapshot"]["payment_status_label"], "Оплачено повністю")
        self.assertEqual(payload["snapshot"]["declared_cost"], "1299.00")
        self.assertEqual(payload["snapshot"]["cod_amount"], "0.00")
