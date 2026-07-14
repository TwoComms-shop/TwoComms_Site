from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from orders.models import Order
from storefront.models import PageView, SiteSession, UTMSession, UserAction
from storefront.utm_analytics import (
    get_campaigns_stats,
    get_content_stats,
    get_funnel_stats,
    get_general_stats,
    get_sources_stats,
)
from storefront.utm_tracking import record_order_action


class RecordOrderActionTests(TestCase):
    def _build_order(self, *, session_key: str, user=None, utm_session=None):
        return Order.objects.create(
            user=user,
            session_key=session_key,
            utm_session=utm_session,
            full_name='Тестовий клієнт',
            phone='+380991112233',
            city='Київ',
            np_office='Відділення №4',
            pay_type='online_full',
            total_sum=Decimal('1499.00'),
            status='new',
        )

    def test_record_order_action_links_purchase_to_existing_sessions(self):
        user = get_user_model().objects.create_user(username='utm-user', password='pass12345')
        site_session = SiteSession.objects.create(session_key='sess-auth-1', user=user)
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='sess-auth-1',
            utm_source='instagram',
        )
        order = self._build_order(session_key='sess-auth-1', user=user)

        action = record_order_action(
            'purchase',
            order,
            cart_value=float(order.total_sum),
            metadata={'source': 'monobank'},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.utm_session_id, utm_session.id)
        self.assertEqual(action.site_session_id, site_session.id)
        self.assertEqual(action.user_id, user.id)
        self.assertEqual(action.order_id, order.id)
        self.assertEqual(action.order_number, order.order_number)
        self.assertEqual(action.metadata['source'], 'monobank')

        utm_session.refresh_from_db()
        self.assertTrue(utm_session.is_converted)
        self.assertEqual(utm_session.conversion_type, 'purchase')

    def test_record_order_action_supports_guest_orders_via_order_utm_session(self):
        site_session = SiteSession.objects.create(session_key='sess-guest-1')
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='sess-guest-1',
            utm_source='facebook',
        )
        order = self._build_order(
            session_key='sess-guest-1',
            utm_session=utm_session,
        )

        action = record_order_action(
            'lead',
            order,
            cart_value=float(order.total_sum),
            metadata={'source': 'monobank'},
        )

        self.assertIsNotNone(action)
        self.assertEqual(action.utm_session_id, utm_session.id)
        self.assertEqual(action.site_session_id, site_session.id)
        self.assertIsNone(action.user)
        self.assertEqual(action.action_type, 'lead')

        utm_session.refresh_from_db()
        self.assertTrue(utm_session.is_converted)
        self.assertEqual(utm_session.conversion_type, 'lead')

    def test_purchase_reuses_existing_order_action_and_heals_conversion(self):
        site_session = SiteSession.objects.create(session_key='sess-heal-1')
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='sess-heal-1',
            utm_source='instagram',
        )
        order = self._build_order(
            session_key='sess-heal-1',
            utm_session=utm_session,
        )
        existing = UserAction.objects.create(
            utm_session=utm_session,
            site_session=site_session,
            action_type='purchase',
            order_id=order.pk,
            order_number=order.order_number,
            cart_value=order.total_sum,
        )

        action = record_order_action(
            'purchase',
            order,
            cart_value=float(order.total_sum),
            metadata={'source': 'recovery'},
        )

        self.assertEqual(action.pk, existing.pk)
        self.assertEqual(
            UserAction.objects.filter(action_type='purchase', order_id=order.pk).count(),
            1,
        )
        utm_session.refresh_from_db()
        self.assertTrue(utm_session.is_converted)
        self.assertEqual(utm_session.conversion_type, 'purchase')

    def test_stale_order_instance_uses_current_locked_utm_attribution(self):
        site_session = SiteSession.objects.create(session_key='sess-locked-utm-1')
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='sess-locked-utm-1',
            utm_source='instagram',
        )
        stale_order = self._build_order(session_key=None)
        Order.objects.filter(pk=stale_order.pk).update(utm_session=utm_session)

        action = record_order_action('purchase', stale_order)

        self.assertIsNotNone(action)
        self.assertEqual(action.utm_session_id, utm_session.pk)
        self.assertEqual(action.site_session_id, site_session.pk)

    def test_purchase_upgrades_lead_conversion_at_purchase_time(self):
        site_session = SiteSession.objects.create(session_key='sess-upgrade-1')
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='sess-upgrade-1',
            utm_source='instagram',
        )
        utm_session.mark_as_converted(conversion_type='lead')
        order = self._build_order(
            session_key='sess-upgrade-1',
            utm_session=utm_session,
        )
        occurred_at = timezone.now() - timedelta(days=2)

        record_order_action(
            'purchase',
            order,
            occurred_at=occurred_at,
        )

        utm_session.refresh_from_db()
        self.assertEqual(utm_session.conversion_type, 'purchase')
        self.assertAlmostEqual(
            utm_session.converted_at.timestamp(),
            occurred_at.timestamp(),
            delta=1,
        )

    def test_stale_lead_write_cannot_downgrade_purchase_conversion(self):
        site_session = SiteSession.objects.create(session_key='sess-strongest-1')
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='sess-strongest-1',
            utm_source='instagram',
        )
        stale_session = UTMSession.objects.get(pk=utm_session.pk)
        purchase_time = timezone.now() - timedelta(hours=1)

        utm_session.mark_as_converted(
            conversion_type='purchase',
            converted_at=purchase_time,
        )
        stale_session.mark_as_converted(conversion_type='lead')

        utm_session.refresh_from_db()
        self.assertEqual(utm_session.conversion_type, 'purchase')
        self.assertAlmostEqual(
            utm_session.converted_at.timestamp(),
            purchase_time.timestamp(),
            delta=1,
        )

    def test_strict_order_action_propagates_storage_errors(self):
        order = self._build_order(session_key='sess-strict-1')

        with (
            patch.object(
                UserAction.objects,
                'get_or_create',
                side_effect=RuntimeError('storage unavailable'),
            ),
            self.assertRaisesMessage(RuntimeError, 'storage unavailable'),
        ):
            record_order_action('purchase', order, raise_errors=True)


class FunnelStatsProductViewQualityTests(TestCase):
    def _utm_session(self, suffix: str, *, pageviews: int, is_bot: bool = False):
        site_session = SiteSession.objects.create(
            session_key=f"funnel-{suffix}",
            pageviews=pageviews,
            is_bot=is_bot,
            last_path="/product/funnel/",
        )
        if pageviews:
            PageView.objects.create(
                session=site_session,
                path="/product/funnel/",
                is_bot=is_bot,
            )
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key=site_session.session_key,
            utm_source="instagram",
        )
        return site_session, utm_session

    def test_funnel_counts_only_trusted_product_view_sessions(self):
        trusted_site, trusted_utm = self._utm_session("trusted", pageviews=1)
        bot_site, bot_utm = self._utm_session("bot", pageviews=1, is_bot=True)
        zero_site, zero_utm = self._utm_session("zero", pageviews=0)
        _unlinked_site, unlinked_utm = self._utm_session("unlinked", pageviews=1)

        for site_session, utm_session in (
            (trusted_site, trusted_utm),
            (bot_site, bot_utm),
            (zero_site, zero_utm),
            (None, unlinked_utm),
        ):
            UserAction.objects.create(
                site_session=site_session,
                utm_session=utm_session,
                action_type="product_view",
                product_id=991,
                points_earned=3,
            )
        UserAction.objects.create(
            utm_session=trusted_utm,
            action_type="add_to_cart",
            product_id=991,
            points_earned=7,
        )

        stats = get_funnel_stats("all_time")

        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["product_views"], 1)
        self.assertEqual(stats["product_views_rate"], 25.0)
        self.assertEqual(get_general_stats("all_time")["total_score"], 10)
        self.assertEqual(get_sources_stats("all_time")[0]["total_score"], 10)
        self.assertEqual(get_campaigns_stats("all_time")[0]["total_score"], 10)
        self.assertEqual(get_content_stats("all_time")[0]["total_score"], 10)
