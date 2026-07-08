import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from orders.models import DropshipperOrder


class AdminDropshipStatusTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin",
            password="StrongPass123!",
            is_staff=True,
        )
        self.dropshipper = User.objects.create_user(
            username="drop",
            password="StrongPass123!",
        )
        self.order = DropshipperOrder.objects.create(
            dropshipper=self.dropshipper,
            client_name="Test Client",
            client_phone="+380991112233",
            client_np_address="Kyiv, branch 1",
            status="pending",
            payment_status="unpaid",
            total_drop_price=Decimal("100.00"),
            total_selling_price=Decimal("200.00"),
            profit=Decimal("100.00"),
        )
        self.client.force_login(self.admin)

    def test_admin_status_update_locks_order_row(self):
        captured = {}
        original = DropshipperOrder.objects.select_for_update

        def _spy(*args, **kwargs):
            captured["called"] = True
            return original(*args, **kwargs)

        with patch.object(DropshipperOrder.objects, "select_for_update", side_effect=_spy):
            response = self.client.post(
                reverse("orders:admin_update_dropship_status", args=[self.order.id]),
                data=json.dumps({"status": "confirmed"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured.get("called"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
