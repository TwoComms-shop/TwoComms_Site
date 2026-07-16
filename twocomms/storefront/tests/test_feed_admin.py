from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase


class FeedAdminTests(TestCase):
    def test_admin_panel_defaults_to_orders_without_loading_analytics(self):
        staff = User.objects.create_user(
            username="feed-admin",
            password="pass1234",
            is_staff=True,
        )
        self.client.force_login(staff)

        with patch("storefront.views.admin.build_admin_analytics_context") as analytics:
            response = self.client.get("/admin-panel/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["section"], "orders")
        analytics.assert_not_called()
        self.assertContains(response, 'class="orders-admin-section oadm"')
