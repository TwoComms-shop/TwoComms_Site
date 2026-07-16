from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from orders.models import Order


class FeedAdminTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="feed-admin",
            password="pass1234",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _create_orders(self, count):
        for index in range(count):
            Order.objects.create(
                user=self.staff,
                order_number=f"PAGE-{index:04d}",
                full_name=f"Customer {index}",
                phone="+380991112233",
                city="Kyiv",
                np_office="1",
                status="new",
                payment_status="unpaid",
            )

    def test_admin_panel_defaults_to_orders_without_loading_stats_or_analytics(self):
        with (
            patch("storefront.views.admin._build_stats") as stats,
            patch("storefront.views.admin.build_admin_analytics_context") as analytics,
        ):
            response = self.client.get("/admin-panel/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["section"], "orders")
        stats.assert_not_called()
        analytics.assert_not_called()
        self.assertContains(response, 'class="orders-admin-section oadm"')

    def test_explicit_stats_section_loads_stats_and_analytics(self):
        with (
            patch("storefront.views.admin._build_stats", return_value={"orders_today": 7}) as stats,
            patch(
                "storefront.views.admin.build_admin_analytics_context",
                return_value={"analytics_dashboard": {"config": {}}},
            ) as analytics,
        ):
            response = self.client.get("/admin-panel/?section=stats")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["section"], "stats")
        self.assertEqual(response.context["stats"], {"orders_today": 7})
        stats.assert_called_once_with("today")
        analytics.assert_called_once_with(response.wsgi_request)

    def test_orders_are_paginated_server_side_with_filter_preserving_controls(self):
        self._create_orders(55)
        filters = (
            f"section=orders&status=new&payment=unpaid&user_id={self.staff.pk}"
        )

        first_response = self.client.get(f"/admin-panel/?{filters}")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(len(first_response.context["orders"]), 50)
        first_page = first_response.context["orders_page"]
        self.assertEqual(first_page.number, 1)
        self.assertEqual(first_page.paginator.count, 55)
        self.assertEqual(first_page.paginator.num_pages, 2)
        self.assertContains(first_response, "Сторінка 1 з 2")
        self.assertContains(
            first_response,
            (
                f'href="?section=orders&amp;status=new&amp;payment=unpaid'
                f'&amp;user_id={self.staff.pk}&amp;page=2"'
            ),
        )

        second_response = self.client.get(f"/admin-panel/?{filters}&page=2")

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(len(second_response.context["orders"]), 5)
        second_page = second_response.context["orders_page"]
        self.assertEqual(second_page.number, 2)
        self.assertTrue(second_page.has_previous())
        self.assertFalse(second_page.has_next())
        self.assertContains(second_response, "Сторінка 2 з 2")
        self.assertContains(
            second_response,
            (
                f'href="?section=orders&amp;status=new&amp;payment=unpaid'
                f'&amp;user_id={self.staff.pk}&amp;page=1"'
            ),
        )

    def test_orders_navigation_precedes_statistics(self):
        response = self.client.get("/admin-panel/")

        content = response.content.decode()
        self.assertLess(
            content.index('href="?section=orders"'),
            content.index('href="?section=stats"'),
        )
