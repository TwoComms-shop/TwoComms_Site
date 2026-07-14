from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase

from orders.models import Order
from storefront.models import PageView, SiteSession, UserAction
from storefront.services.admin_analytics import (
    build_integration_status_widget,
    build_product_admin_metrics,
    parse_analytics_filters,
)


class AdminAnalyticsApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )
        self.staff = User.objects.create_user(username="staff", password="pass1234", is_staff=True)
        self.user = User.objects.create_user(username="user", password="pass1234")

    def test_requires_staff_permissions(self):
        self.client.force_login(self.user)
        response = self.client.get("/api/admin/analytics/", secure=True)
        self.assertEqual(response.status_code, 403)

    def test_returns_dashboard_bundle_for_staff(self):
        session = SiteSession.objects.create(session_key="analytics-session", pageviews=2, last_path="/product/test")
        UserAction.objects.create(
            site_session=session,
            action_type="product_view",
            product_id=1,
            product_name="Test",
            metadata={"visitor_id": "vid-1"},
        )

        self.client.force_login(self.staff)
        response = self.client.get("/api/admin/analytics/?period=month", secure=True)
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertIn("overview", payload)
        self.assertIn("timeseries", payload)
        self.assertIn("integration_status", payload)
        self.assertIn("data", payload["overview"])

    def test_bundle_supports_compare_mode_for_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            "/api/admin/analytics/?period=month&compare_to=previous_period",
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("comparison", payload["overview"]["data"])
        self.assertIn("orders", payload["overview"]["data"]["comparison"])

    def test_products_widget_endpoint_is_available_for_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get("/api/admin/analytics/products/?period=month", secure=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("data", payload)
        self.assertEqual(payload.get("source"), "internal")

    def test_product_metrics_count_only_trusted_human_page_views(self):
        trusted = SiteSession.objects.create(
            session_key="trusted-product-view",
            ip_address="188.163.49.61",
            pageviews=1,
            last_path="/product/trusted/",
        )
        bot = SiteSession.objects.create(
            session_key="bot-product-view",
            ip_address="188.163.49.62",
            pageviews=1,
            last_path="/product/trusted/",
            is_bot=True,
        )
        no_pageview = SiteSession.objects.create(
            session_key="zero-page-product-view",
            ip_address="188.163.49.63",
            pageviews=0,
            last_path="/product/trusted/",
        )
        PageView.objects.create(
            session=trusted,
            path="/product/trusted/",
            is_bot=False,
        )
        PageView.objects.create(
            session=bot,
            path="/product/trusted/",
            is_bot=True,
        )
        for session in (trusted, bot, no_pageview, None):
            UserAction.objects.create(
                site_session=session,
                action_type="product_view",
                product_id=991,
                product_name="Trusted product",
            )

        metrics = build_product_admin_metrics([991])

        self.assertEqual(metrics[991]["total_views"], 1)
        self.assertEqual(metrics[991]["unique_ip_views"], 1)

        self.client.force_login(self.staff)
        response = self.client.get("/api/admin/analytics/products/?period=all_time", secure=True)
        self.assertEqual(response.status_code, 200)
        product = next(
            row for row in response.json()["data"]["top_viewed"] if row["product_id"] == 991
        )
        self.assertEqual(product["total_views"], 1)
        self.assertEqual(product["unique_ip_views"], 1)

        response = self.client.get(
            "/api/admin/analytics/products/?period=all_time&include_bots=1",
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        product = next(
            row for row in response.json()["data"]["top_viewed"] if row["product_id"] == 991
        )
        self.assertEqual(product["total_views"], 2)
        self.assertEqual(product["unique_ip_views"], 2)

    def test_unlinked_purchase_remains_in_dashboard_actions(self):
        UserAction.objects.create(action_type="purchase", order_id=12345)

        self.client.force_login(self.staff)
        response = self.client.get("/api/admin/analytics/?period=all_time", secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["overview"]["data"]["headline"]["purchases"], 1)

    def test_sales_widget_endpoint_is_available_for_staff(self):
        Order.objects.create(
            full_name="Buyer",
            phone="+380991112233",
            city="Kyiv",
            np_office="1",
            pay_type="online_full",
            total_sum=Decimal("1200.00"),
            status="new",
            payment_status="paid",
        )

        self.client.force_login(self.staff)
        response = self.client.get("/api/admin/analytics/sales/?period=month", secure=True)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("daily_series", payload["data"])
        self.assertEqual(payload.get("source"), "internal")

    def test_admin_panel_stats_section_renders_new_dashboard_shell(self):
        self.client.force_login(self.staff)
        response = self.client.get("/admin-panel/?section=stats", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "adminAnalyticsConfig")
        self.assertContains(response, "analyticsOverviewChart")

    @patch("storefront.services.admin_analytics.get_clarity_status")
    @patch("storefront.services.admin_analytics.get_ga4_status")
    def test_integration_status_uses_live_checks_and_does_not_raise_false_ip_warning(self, ga4_mock, clarity_mock):
        ga4_mock.return_value = {
            "key": "ga4",
            "label": "GA4 Data API",
            "status": "healthy",
            "message": "GA4 ok",
            "details": {"configured": True},
        }
        clarity_mock.return_value = {
            "key": "clarity",
            "label": "Microsoft Clarity",
            "status": "healthy",
            "message": "Clarity ok",
            "details": {"configured": True},
        }
        SiteSession.objects.create(
            session_key="ip-session",
            ip_address="188.163.49.54",
            visitor_id="vid-1",
            pageviews=1,
            last_path="/",
        )

        widget = build_integration_status_widget(parse_analytics_filters({}))

        ga4_mock.assert_called_once_with(test_connection=True)
        clarity_mock.assert_called_once_with(test_connection=True)
        self.assertEqual(widget["data"]["integrations"][1]["status"], "healthy")
        self.assertNotIn(
            "IP capture нижче 75%: перед використанням unique-IP KPI перевірити production reverse proxy.",
            widget["data"]["warnings"],
        )

    def test_dashboard_metrics_ignore_technical_only_session_noise(self):
        human_session = SiteSession.objects.create(
            session_key="human-session",
            ip_address="188.163.49.54",
            visitor_id="vid-human",
            pageviews=2,
            last_path="/favorites/count/",
        )
        PageView.objects.create(session=human_session, path="/catalog/", referrer="", is_bot=False)
        PageView.objects.create(session=human_session, path="/favorites/count/", referrer="", is_bot=False)

        noise_session = SiteSession.objects.create(
            session_key="noise-session",
            ip_address="188.163.49.55",
            visitor_id="vid-noise",
            pageviews=1,
            last_path="/sw.js",
        )
        PageView.objects.create(session=noise_session, path="/sw.js", referrer="", is_bot=False)

        self.client.force_login(self.staff)
        response = self.client.get("/api/admin/analytics/?period=month", secure=True)
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertEqual(payload["overview"]["data"]["headline"]["sessions"], 1)
        self.assertEqual(payload["overview"]["data"]["headline"]["page_views"], 1)
        self.assertEqual(payload["overview"]["data"]["headline"]["bounce_rate"], 100.0)

    def test_dashboard_keeps_real_favorites_page_traffic(self):
        favorites_session = SiteSession.objects.create(
            session_key="favorites-session",
            ip_address="188.163.49.56",
            visitor_id="vid-favorites",
            pageviews=1,
            last_path="/favorites/",
        )
        PageView.objects.create(session=favorites_session, path="/favorites/", referrer="", is_bot=False)

        self.client.force_login(self.staff)
        response = self.client.get("/api/admin/analytics/?period=month", secure=True)
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertEqual(payload["overview"]["data"]["headline"]["sessions"], 1)
        self.assertEqual(payload["overview"]["data"]["headline"]["page_views"], 1)
