from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from orders.models import Order
from orders.nova_poshta_documents import (
    TELEGRAM_CREATE_NP_WAYBILL_ACTION,
    TELEGRAM_DELETE_NP_WAYBILL_ACTION,
)
from warehouse.models import WriteOffRequest


@override_settings(
    NOVA_POSHTA_FALLBACK_ENABLED=False,
    RATELIMIT_ENABLE=False,
    COMPRESS_ENABLED=False,
    COMPRESS_OFFLINE=False,
)
class AdminOrderOperationRedirectTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="order-operations-staff",
            password="pass1234",
            is_staff=True,
        )
        self.order = Order.objects.create(
            user=self.staff,
            order_number="OPS-0001",
            full_name="Операторський клієнт",
            phone="+380991112233",
            city="Київ",
            np_office="Відділення №1",
            status="new",
            payment_status="paid",
        )

    def test_anonymous_user_cannot_open_order_operation_redirects(self):
        for route_name in (
            "admin_order_nova_poshta_action",
            "admin_order_warehouse_action",
        ):
            with self.subTest(route_name=route_name):
                response = self.client.get(
                    reverse(route_name, args=[self.order.pk]),
                    secure=True,
                )

                self.assertEqual(response.status_code, 302)
                self.assertIn("/admin/login/", response["Location"])

    @patch(
        "storefront.views.admin_order_actions.build_order_action_url",
        return_value="https://twocomms.shop/orders/telegram-waybill/1/create-np-waybill/?token=create",
    )
    def test_staff_ttn_action_redirects_to_canonical_create_url(self, build_url):
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_order_nova_poshta_action", args=[self.order.pk]),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://twocomms.shop/orders/telegram-waybill/1/create-np-waybill/?token=create")
        build_url.assert_called_once_with(
            self.order,
            TELEGRAM_CREATE_NP_WAYBILL_ACTION,
            route_name="telegram_order_np_waybill_action",
        )

    @patch(
        "storefront.views.admin_order_actions.build_order_action_url",
        return_value="https://twocomms.shop/orders/telegram-waybill/1/delete-np-waybill/?token=delete",
    )
    def test_staff_ttn_action_redirects_to_canonical_delete_url(self, build_url):
        self.order.nova_poshta_document_ref = "document-ref-1"
        self.order.tracking_number = "20450012345678"
        self.order.save(update_fields=["nova_poshta_document_ref", "tracking_number"])
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_order_nova_poshta_action", args=[self.order.pk]),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://twocomms.shop/orders/telegram-waybill/1/delete-np-waybill/?token=delete")
        build_url.assert_called_once_with(
            self.order,
            TELEGRAM_DELETE_NP_WAYBILL_ACTION,
            route_name="telegram_order_np_waybill_action",
            token_scope="document-ref-1",
        )

    @patch(
        "storefront.views.admin_order_actions.build_storage_writeoff_url",
        return_value="https://storage.twocomms.shop/order/writeoff-token/write-off/",
    )
    def test_staff_warehouse_action_delegates_to_writeoff_link_builder(self, build_url):
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_order_warehouse_action", args=[self.order.pk]),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://storage.twocomms.shop/order/writeoff-token/write-off/")
        build_url.assert_called_once_with(self.order)

    def test_staff_warehouse_action_reuses_the_same_pending_request(self):
        self.client.force_login(self.staff)

        first = self.client.get(
            reverse("admin_order_warehouse_action", args=[self.order.pk]),
            secure=True,
        )
        pending = WriteOffRequest.objects.get(
            order=self.order,
            status=WriteOffRequest.STATUS_PENDING,
        )
        second = self.client.get(
            reverse("admin_order_warehouse_action", args=[self.order.pk]),
            secure=True,
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            first["Location"],
            f"https://storage.twocomms.shop/order/{pending.token}/write-off/",
        )
        self.assertEqual(second["Location"], first["Location"])
        self.assertEqual(
            WriteOffRequest.objects.filter(
                order=self.order,
                status=WriteOffRequest.STATUS_PENDING,
            ).count(),
            1,
        )

    @patch(
        "storefront.views.admin_order_actions.build_storage_cancel_sale_url",
        return_value="https://storage.twocomms.shop/order/writeoff-token/cancel-sale/",
    )
    def test_staff_warehouse_action_redirects_to_cancel_sale_after_completion(self, build_url):
        WriteOffRequest.objects.create(
            order=self.order,
            status=WriteOffRequest.STATUS_COMPLETED,
        )
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_order_warehouse_action", args=[self.order.pk]),
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://storage.twocomms.shop/order/writeoff-token/cancel-sale/")
        build_url.assert_called_once_with(self.order)

    def test_order_card_shows_create_ttn_and_writeoff_without_side_effects(self):
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_panel"),
            {"section": "orders"},
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([order.pk for order in response.context["orders"]], [self.order.pk])
        self.assertContains(response, 'data-admin-operation="nova-poshta-create"')
        self.assertContains(response, 'data-admin-operation="warehouse-writeoff"')
        self.assertEqual(
            WriteOffRequest.objects.filter(order=self.order).count(),
            0,
        )

    def test_order_card_replaces_create_ttn_with_unlink_for_api_waybill(self):
        self.order.nova_poshta_document_ref = "document-ref-1"
        self.order.tracking_number = "20450012345678"
        self.order.save(update_fields=["nova_poshta_document_ref", "tracking_number"])
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_panel"),
            {"section": "orders"},
            secure=True,
        )

        self.assertContains(response, 'data-admin-operation="nova-poshta-unlink"')
        self.assertNotContains(response, 'data-admin-operation="nova-poshta-create"')
        self.assertContains(response, "20450012345678")

    def test_order_card_does_not_offer_api_unlink_for_manual_ttn(self):
        self.order.tracking_number = "20450012345678"
        self.order.save(update_fields=["tracking_number"])
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_panel"),
            {"section": "orders"},
            secure=True,
        )

        self.assertContains(response, "Ручна ТТН")
        self.assertNotContains(response, 'data-admin-operation="nova-poshta-create"')
        self.assertNotContains(response, 'data-admin-operation="nova-poshta-unlink"')

    def test_order_card_switches_writeoff_control_to_cancel_after_completion(self):
        WriteOffRequest.objects.create(
            order=self.order,
            status=WriteOffRequest.STATUS_COMPLETED,
        )
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_panel"),
            {"section": "orders"},
            secure=True,
        )

        self.assertContains(response, 'data-admin-operation="warehouse-cancel"')
        self.assertNotContains(response, 'data-admin-operation="warehouse-writeoff"')

    def test_cancelled_order_has_no_operation_controls(self):
        self.order.status = "cancelled"
        self.order.save(update_fields=["status"])
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse("admin_panel"),
            {"section": "orders"},
            secure=True,
        )

        self.assertNotContains(response, 'data-admin-operation="nova-poshta-create"')
        self.assertNotContains(response, 'data-admin-operation="nova-poshta-unlink"')
        self.assertNotContains(response, 'data-admin-operation="warehouse-writeoff"')
        self.assertNotContains(response, 'data-admin-operation="warehouse-cancel"')
