from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from storefront.models import Category, Product


class TelegramContactManagerTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Contact", slug="contact")
        self.product = Product.objects.create(
            title="Contact Product",
            slug="contact-product",
            category=category,
            price=300,
            status="published",
        )
        session = self.client.session
        session["cart"] = {
            f"{self.product.pk}:M:default": {
                "product_id": self.product.pk,
                "qty": 1,
                "size": "M",
                "color_variant_id": None,
            }
        }
        session.save()
        self.url = reverse("contact_manager")
        self.form = {
            "full_name": "Contact Buyer",
            "phone": "+380501112233",
        }

    @patch(
        "orders.telegram_notifications.TelegramNotifier.send_admin_message",
        return_value=False,
    )
    def test_false_delivery_returns_retryable_error(self, send_message):
        response = self.client.post(self.url, self.form)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])
        self.assertIn("error", response.json())
        send_message.assert_called_once()
    @patch(
        "orders.telegram_notifications.TelegramNotifier.send_admin_message",
        return_value=True,
    )
    def test_successful_delivery_keeps_success_response(self, send_message):
        response = self.client.post(self.url, self.form)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True})
        send_message.assert_called_once()
