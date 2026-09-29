from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection, models
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from orders.models import Order, OrderItem
from storefront.models import Category, PageView, Product, SiteSession, UserAction, UTMSession
from storefront.services.admin_analytics import (
    _build_overview_cards,
    _products_data,
    _sales_data,
    _survey_data,
    _timeseries_data,
    build_integration_status_widget,
    build_product_admin_metrics,
    parse_analytics_filters,
)


# The live analytics contract includes legacy NULL user IDs even though the
# current SurveySession model cannot create them in the SQLite test schema.
class NullableAnalyticsSurveySession(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        db_constraint=False,
        on_delete=models.DO_NOTHING,
    )
    status = models.CharField(max_length=20)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True)
    current_question_id = models.CharField(max_length=100, blank=True, null=True)
    back_used = models.BooleanField(default=False)
    awarded_promocode = models.IntegerField(null=True)

    class Meta:
        app_label = "storefront"
        managed = False
        db_table = "test_admin_analytics_nullable_survey_session"


def _queries_for_table(captured_queries, table_name):
    return [query["sql"] for query in captured_queries if table_name in query["sql"]]


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

    def _revenue_order(self, total, discount, payment_status="paid"):
        return Order.objects.create(
            full_name="Revenue Buyer", phone="+380501112233", city="Kyiv", np_office="1",
            pay_type="online_full", payment_status=payment_status,
            total_sum=Decimal(total), discount_amount=Decimal(discount),
            utm_source="instagram", utm_medium="paid_social",
        )

    def test_order_revenue_widgets_use_discounted_values_and_paid_population(self):
        self._revenue_order("1200.00", "200.00")
        self._revenue_order("800.00", "100.00")
        self._revenue_order("900.00", "100.00", "prepaid")
        self._revenue_order("900.00", "100.00", "unpaid")
        filters = parse_analytics_filters({"period": "today"})

        headline = _build_overview_cards(filters)["headline"]
        self.assertEqual(headline["revenue"], 1700.0)
        self.assertEqual(headline["aov"], 850.0)
        self.assertEqual(headline["paid_orders"], 2)
        self.assertEqual(_timeseries_data(filters)["series"]["revenue"], [1700.0])
        sales = _sales_data(filters)
        self.assertEqual(sales["summary"]["revenue"], 1700.0)
        self.assertEqual(sales["summary"]["aov"], 850.0)
        self.assertEqual(sales["summary"]["paid_or_prepay_orders"], 3)
        self.assertEqual(sales["daily_series"]["revenue"], [1700.0])
        self.assertEqual(sales["source_ltv"][0]["revenue"], 1700.0)

    def test_order_revenue_clamps_invalid_overdiscount_to_zero(self):
        self._revenue_order("100.00", "120.00")
        self._revenue_order("800.00", "0.00")
        headline = _build_overview_cards(parse_analytics_filters({"period": "today"}))["headline"]
        self.assertEqual(headline["revenue"], 800.0)
        self.assertEqual(headline["aov"], 400.0)

    def test_revenue_comparison_uses_discounted_values_in_both_periods(self):
        self._revenue_order("1200.00", "200.00")
        previous = self._revenue_order("800.00", "100.00")
        Order.objects.filter(pk=previous.pk).update(created=timezone.now() - timedelta(days=1))
        filters = parse_analytics_filters({"period": "today", "compare_to": "previous_period"})
        revenue = _build_overview_cards(filters)["comparison"]["revenue"]
        self.assertEqual(revenue["previous"], 700.0)
        self.assertEqual(_timeseries_data(filters)["comparison"]["series"]["revenue"], [700.0])

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

    def test_acquisition_widget_breaks_out_google_free_listing_products(self):
        merchant_session = SiteSession.objects.create(
            session_key="merchant-session",
            pageviews=2,
            last_path="/product/merchant-shirt/",
            first_touch_data={
                "srsltid": "merchant-click",
                "landing_path": "/catalog/tshirts/",
                "utm_source": "google",
                "utm_medium": "organic",
            },
        )
        UTMSession.objects.create(
            session_key=merchant_session.session_key,
            utm_source="google",
            utm_medium="organic",
            srsltid="merchant-click",
            landing_page="/catalog/tshirts/",
            is_converted=True,
        )
        PageView.objects.create(
            session=merchant_session,
            path="/product/merchant-shirt/",
            is_bot=False,
        )
        UserAction.objects.create(
            site_session=merchant_session,
            action_type="product_view",
            product_id=321,
            product_name="Merchant shirt",
        )
        SiteSession.objects.create(
            session_key="plain-google-session",
            pageviews=1,
            last_path="/catalog/",
            first_touch_data={
                "landing_path": "/catalog/",
                "utm_source": "google",
                "utm_medium": "organic",
            },
        )

        self.client.force_login(self.staff)
        response = self.client.get(
            "/api/admin/analytics/acquisition/?period=all_time",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        merchant = response.json()["data"]["google_free_listings"]
        self.assertEqual(merchant["sessions"], 1)
        self.assertEqual(merchant["converted_sessions"], 1)
        self.assertEqual(merchant["conversion_rate"], 100.0)
        self.assertEqual(
            merchant["landing_pages"],
            [{"label": "/catalog/tshirts/", "sessions": 1}],
        )
        self.assertEqual(
            merchant["products"],
            [{"product_id": 321, "label": "Merchant shirt", "views": 1, "sessions": 1}],
        )

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

    def test_product_metrics_count_only_paid_items_as_sold(self):
        category = Category.objects.create(name="Metrics tees", slug="metrics-tees")
        product = Product.objects.create(
            title="Metrics product",
            slug="metrics-product",
            category=category,
            price=1090,
        )
        paid_order = Order.objects.create(
            full_name="Paid buyer",
            phone="+380991110001",
            city="Kyiv",
            np_office="1",
            payment_status="paid",
        )
        unpaid_order = Order.objects.create(
            full_name="Unpaid buyer",
            phone="+380991110002",
            city="Kyiv",
            np_office="2",
            payment_status="unpaid",
        )
        OrderItem.objects.create(
            order=paid_order,
            product=product,
            title=product.title,
            qty=2,
            unit_price=Decimal(1090),
            line_total=Decimal(2180),
        )
        OrderItem.objects.create(
            order=unpaid_order,
            product=product,
            title=product.title,
            qty=5,
            unit_price=Decimal(1090),
            line_total=Decimal(5450),
        )

        metrics = build_product_admin_metrics([product.pk])

        self.assertEqual(metrics[product.pk]["purchase_orders"], 1)
        self.assertEqual(metrics[product.pk]["items_sold"], 2)

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


class AdminAnalyticsOrmQueryTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_products_data_bulk_loads_products_without_changing_rows_or_fallbacks(self):
        tees = Category.objects.create(name="Tees", slug="tees-query-test")
        hoodies = Category.objects.create(name="Hoodies", slug="hoodies-query-test")
        first_product = Product.objects.create(
            title="Fallback tee",
            slug="fallback-tee-query-test",
            category=tees,
            price=100,
        )
        second_product = Product.objects.create(
            title="Fallback hoodie",
            slug="fallback-hoodie-query-test",
            category=hoodies,
            price=200,
        )
        site_session = SiteSession.objects.create(
            session_key="product-analytics-query-test",
            ip_address="198.51.100.10",
            pageviews=20,
            last_path="/catalog/",
        )

        def create_views(product_id, product_name, count):
            UserAction.objects.bulk_create(
                [
                    UserAction(
                        site_session=site_session,
                        action_type="product_view",
                        product_id=product_id,
                        product_name=product_name,
                    )
                    for _ in range(count)
                ]
            )

        create_views(None, "Ignored null product", 6)
        create_views(first_product.pk, "Popular alias", 5)
        create_views(first_product.pk, "", 4)
        create_views(second_product.pk, None, 3)
        create_views(987654321, None, 2)
        UserAction.objects.bulk_create(
            [
                UserAction(
                    site_session=site_session,
                    action_type="add_to_cart",
                    product_id=first_product.pk,
                )
                for _ in range(2)
            ]
        )
        paid_order = Order.objects.create(
            full_name="Analytics buyer",
            phone="+380991110099",
            city="Kyiv",
            np_office="1",
            payment_status="paid",
        )
        OrderItem.objects.create(
            order=paid_order,
            product=first_product,
            title=first_product.title,
            qty=3,
            unit_price=Decimal("100.00"),
            line_total=Decimal("300.00"),
        )

        with CaptureQueriesContext(connection) as captured:
            payload = _products_data(parse_analytics_filters({"period": "all_time"}))

        product_queries = _queries_for_table(captured.captured_queries, "storefront_product")
        self.assertEqual(len(product_queries), 2, product_queries)
        self.assertEqual(
            payload["top_viewed"],
            [
                {
                    "product_id": first_product.pk,
                    "title": "Popular alias",
                    "category": "Tees",
                    "total_views": 5,
                    "unique_ip_views": 1,
                    "adds_to_cart": 2,
                    "purchases": 1,
                    "items_sold": 3,
                    "revenue": 300.0,
                    "view_to_cart_rate": 40.0,
                    "view_to_purchase_rate": 20.0,
                },
                {
                    "product_id": first_product.pk,
                    "title": "Fallback tee",
                    "category": "Tees",
                    "total_views": 4,
                    "unique_ip_views": 1,
                    "adds_to_cart": 2,
                    "purchases": 1,
                    "items_sold": 3,
                    "revenue": 300.0,
                    "view_to_cart_rate": 50.0,
                    "view_to_purchase_rate": 25.0,
                },
                {
                    "product_id": second_product.pk,
                    "title": "Fallback hoodie",
                    "category": "Hoodies",
                    "total_views": 3,
                    "unique_ip_views": 1,
                    "adds_to_cart": 0,
                    "purchases": 0,
                    "items_sold": 0,
                    "revenue": 0,
                    "view_to_cart_rate": 0.0,
                    "view_to_purchase_rate": 0.0,
                },
                {
                    "product_id": 987654321,
                    "title": "—",
                    "category": "—",
                    "total_views": 2,
                    "unique_ip_views": 1,
                    "adds_to_cart": 0,
                    "purchases": 0,
                    "items_sold": 0,
                    "revenue": 0,
                    "view_to_cart_rate": 0.0,
                    "view_to_purchase_rate": 0.0,
                },
            ],
        )
        self.assertEqual(
            payload["categories"],
            [
                {"category": "Tees", "views": 9},
                {"category": "Hoodies", "views": 3},
            ],
        )


class AdminAnalyticsNullableSurveyQueryTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(NullableAnalyticsSurveySession)

    @classmethod
    def tearDownClass(cls):
        try:
            with connection.schema_editor() as schema_editor:
                schema_editor.delete_model(NullableAnalyticsSurveySession)
        finally:
            super().tearDownClass()

    def setUp(self):
        cache.clear()

    def _create_order_at(self, *, user, created_at, phone_suffix):
        order = Order.objects.create(
            user=user,
            full_name="Survey buyer",
            phone=f"+38099112{phone_suffix}",
            city="Kyiv",
            np_office="1",
        )
        Order.objects.filter(pk=order.pk).update(created=created_at)
        return order

    def test_survey_data_uses_one_null_safe_purchase_lookup_for_completed_sessions(self):
        now = timezone.now()
        buyer_after = User.objects.create_user(username="survey-after")
        buyer_before = User.objects.create_user(username="survey-before")
        buyer_without_order = User.objects.create_user(username="survey-no-order")

        sessions = [
            NullableAnalyticsSurveySession(
                user=buyer_after,
                status="completed",
                started_at=now - timedelta(hours=3),
                completed_at=now - timedelta(hours=2),
            ),
            NullableAnalyticsSurveySession(
                user=buyer_before,
                status="completed",
                started_at=now - timedelta(hours=3),
                completed_at=now - timedelta(hours=1),
            ),
            NullableAnalyticsSurveySession(
                user=buyer_without_order,
                status="completed",
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=1),
            ),
            NullableAnalyticsSurveySession(
                user=None,
                status="completed",
                started_at=now - timedelta(hours=3),
                completed_at=now - timedelta(hours=2),
            ),
            NullableAnalyticsSurveySession(
                user=None,
                status="completed",
                started_at=now,
                completed_at=now + timedelta(hours=1),
            ),
        ]
        NullableAnalyticsSurveySession.objects.bulk_create(sessions)
        self._create_order_at(
            user=buyer_after,
            created_at=now - timedelta(hours=1),
            phone_suffix="01",
        )
        self._create_order_at(
            user=buyer_before,
            created_at=now - timedelta(hours=2),
            phone_suffix="02",
        )
        self._create_order_at(
            user=None,
            created_at=now - timedelta(hours=1),
            phone_suffix="03",
        )

        nullable_survey_qs = NullableAnalyticsSurveySession.objects.all()
        with patch(
            "storefront.services.admin_analytics._survey_queryset",
            return_value=nullable_survey_qs,
        ):
            with CaptureQueriesContext(connection) as captured:
                payload = _survey_data(parse_analytics_filters({"period": "all_time"}))

        order_queries = _queries_for_table(captured.captured_queries, "orders_order")
        self.assertEqual(len(order_queries), 1, order_queries)
        self.assertEqual(payload["summary"]["completed"], 5)
        self.assertEqual(payload["summary"]["downstream_purchase_rate"], 40.0)
