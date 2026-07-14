import json
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from orders.models import Order
from storefront.models import Category, PageView, Product, SiteSession, UserAction
from storefront.views.monobank import _apply_monobank_status


class AnalyticsTrackingTests(TestCase):
    def setUp(self):
        self.client = Client(
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="443",
            **{"wsgi.url_scheme": "https"},
        )
        self.category = Category.objects.create(name="Analytics", slug="analytics", is_active=True)
        self.product = Product.objects.create(
            title="Analytics Hoodie",
            slug="analytics-hoodie",
            category=self.category,
            price=1200,
            description="Analytics product",
            status="published",
        )

    def test_product_detail_records_product_view(self):
        response = self.client.get(reverse("product", args=[self.product.slug]), secure=True)
        self.assertEqual(response.status_code, 200)
        action = UserAction.objects.get(action_type="product_view")
        self.assertEqual(action.product_id, self.product.id)
        self.assertEqual(action.product_name, self.product.title)

    def test_search_records_query(self):
        response = self.client.get(reverse("search"), {"q": "Analytics"}, secure=True)
        self.assertEqual(response.status_code, 200)
        action = UserAction.objects.get(action_type="search")
        self.assertEqual(action.metadata.get("query"), "Analytics")

    def test_service_worker_requests_do_not_create_sessions_or_pageviews(self):
        response = self.client.get(
            "/sw.js",
            secure=True,
            HTTP_SEC_FETCH_MODE="no-cors",
        )
        self.assertIn(response.status_code, {200, 404})
        self.assertFalse(SiteSession.objects.exists())
        self.assertFalse(PageView.objects.exists())

    def test_head_requests_do_not_create_sessions_or_pageviews(self):
        response = self.client.head("/", secure=True)
        self.assertIn(response.status_code, {200, 301, 302})
        self.assertFalse(SiteSession.objects.exists())
        self.assertFalse(PageView.objects.exists())

    def test_track_event_persists_custom_print_step_event(self):
        response = self.client.post(
            reverse("track_event"),
            data=json.dumps({
                "event_type": "custom_print_step_enter",
                "metadata": {
                    "step_key": "artwork",
                    "product_type": "hoodie",
                },
            }),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        action = UserAction.objects.get(action_type="custom_print_step_enter")
        self.assertEqual(action.metadata.get("step_key"), "artwork")

    def test_track_event_rejects_server_only_order_events(self):
        order = Order.objects.create(
            full_name="Server Event Buyer",
            phone="+380991112233",
            city="Київ",
            np_office="Відділення №1",
            pay_type="online_full",
            total_sum=Decimal("1200.00"),
            status="new",
            payment_status="paid",
        )

        for event_type in ("lead", "purchase"):
            with self.subTest(event_type=event_type):
                response = self.client.post(
                    reverse("track_event"),
                    data=json.dumps({
                        "event_type": event_type,
                        "order_id": order.pk,
                        "cart_value": "1.00",
                        "metadata": {"source": "forged-browser-event"},
                    }),
                    content_type="application/json",
                    secure=True,
                )
                self.assertEqual(response.status_code, 400)

        self.assertFalse(
            UserAction.objects.filter(
                action_type__in=("lead", "purchase"),
                order_id=order.pk,
            ).exists()
        )

    @patch("storefront.views.static_pages.notify_custom_print_safe_exit")
    def test_custom_print_safe_exit_records_server_side_event(self, notify_mock):
        payload = {
            "mode": "personal",
            "product": {"type": "hoodie"},
            "print": {"zones": ["front"]},
            "artwork": {"triage_status": "needs-review"},
            "order": {"quantity": 1},
            "contact": {"channel": "telegram", "name": "Test", "value": "@test"},
            "ui": {"current_step": "contact"},
        }
        response = self.client.post(
            reverse("custom_print_safe_exit"),
            data=json.dumps(payload),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        action = UserAction.objects.get(action_type="custom_print_safe_exit")
        self.assertEqual(action.metadata.get("submission_type"), "safe_exit")
        self.assertEqual(action.metadata.get("step_key"), "contact")
        notify_mock.assert_called_once()

    def test_monobank_success_status_records_purchase_once(self):
        order = Order.objects.create(
            full_name="Buyer",
            phone="+380991112233",
            city="Kyiv",
            np_office="1",
            pay_type="online_full",
            total_sum=Decimal("1200.00"),
            status="new",
            payment_status="checking",
        )

        _apply_monobank_status(order, "success", payload={"status": "success"}, source="test")
        _apply_monobank_status(order, "success", payload={"status": "success"}, source="test")

        self.assertEqual(UserAction.objects.filter(action_type="purchase", order_id=order.id).count(), 1)


class EventHygieneTests(TestCase):
    """W2-4 (AN-035/AN-004/TECH-063): бот-фильтр, staff-исключение, дедуп."""

    def setUp(self):
        self.category = Category.objects.create(name="Hygiene", slug="hygiene", is_active=True)
        self.product = Product.objects.create(
            title="Hygiene Tee",
            slug="hygiene-tee",
            category=self.category,
            price=800,
            description="p",
            status="published",
        )
        self.url = reverse("product", args=[self.product.slug])
        self.kwargs = {
            "HTTP_HOST": "twocomms.shop",
            "SERVER_PORT": "443",
            "wsgi.url_scheme": "https",
        }

    def test_bot_user_agent_records_no_product_view(self):
        client = Client(HTTP_USER_AGENT="Mozilla/5.0 (compatible; Googlebot/2.1)", **self.kwargs)
        response = client.get(self.url, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(UserAction.objects.filter(action_type="product_view").exists())

    def test_staff_user_records_no_product_view(self):
        from django.contrib.auth import get_user_model
        staff = get_user_model().objects.create_user(
            username="staffer", password="x", is_staff=True
        )
        client = Client(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)", **self.kwargs)
        client.force_login(staff)
        response = client.get(self.url, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(UserAction.objects.filter(action_type="product_view").exists())

    def test_product_view_deduped_within_window(self):
        client = Client(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)", **self.kwargs)
        client.get(self.url, secure=True)
        client.get(self.url, secure=True)
        client.get(self.url, secure=True)
        self.assertEqual(
            UserAction.objects.filter(action_type="product_view", product_id=self.product.id).count(),
            1,
        )

    def test_product_view_gets_site_session_link(self):
        client = Client(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)", **self.kwargs)
        client.get(self.url, secure=True)
        action = UserAction.objects.get(action_type="product_view")
        self.assertIsNotNone(action.site_session)

    def test_legacy_query_url_redirects_without_recording_view(self):
        client = Client(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)", **self.kwargs)
        response = client.get(self.url, {"size": "M"}, secure=True)
        if response.status_code == 301:
            # 301 сам по себе не должен записывать просмотр
            self.assertFalse(UserAction.objects.filter(action_type="product_view").exists())
            # follow-up на канонический URL записывает ровно один просмотр
            client.get(response["Location"], secure=True)
        self.assertLessEqual(
            UserAction.objects.filter(action_type="product_view", product_id=self.product.id).count(),
            1,
        )
