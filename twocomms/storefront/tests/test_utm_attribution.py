"""
W2-1/W2-2 (TECH-060/061): UTM-привязка заказа и is_converted.

Приёмка CRO-050: визит с ?utm_source=audit → COD-заказ → в БД
utm_source='audit', utm_session FK, session_key, UserAction с order_id,
UTMSession.is_converted=True.
"""

from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, override_settings
from django.utils import timezone

from orders.models import Order
from storefront.models import UserAction, UTMSession
from storefront.utm_tracking import link_order_to_utm

from .test_checkout import CheckoutTestSupport


@override_settings(META_FBC_ATTRIBUTION_WINDOW_DAYS=7)
class UTMOrderAttributionTests(CheckoutTestSupport):
    def _unattributed_order(self, *, created=None):
        order = Order.objects.create(
            full_name='FBC Attribution Buyer',
            phone='+380501112233',
            city='Kyiv',
            np_office='Branch 1',
            pay_type='cod',
            payment_status='unpaid',
            total_sum=130,
            source='web',
        )
        if created is not None:
            Order.objects.filter(pk=order.pk).update(created=created)
            order.refresh_from_db()
        return order

    def _order_link_request(self, *, fbc=None, fbp=None):
        request = RequestFactory().get('/', secure=True)
        SessionMiddleware(lambda req: None).process_request(request)
        request.session.create()
        request.user = AnonymousUser()
        request.analytics_first_touch_data = {}
        if fbc is not None:
            request.COOKIES['_fbc'] = fbc
        if fbp is not None:
            request.COOKIES['_fbp'] = fbp
        return request

    @staticmethod
    def _fbc(created_at, click_id='fresh-click'):
        return f'fb.1.{int(created_at.timestamp() * 1000)}.{click_id}'

    def test_fresh_fbc_rebuilds_meta_attribution_at_order_link_time(self):
        order_created = timezone.now()
        order = self._unattributed_order(created=order_created)
        fbc = self._fbc(order_created - timedelta(days=6))
        request = self._order_link_request(fbc=fbc)

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertEqual(order.utm_source, 'facebook')
        self.assertEqual(order.utm_medium, 'paid_social')
        self.assertIsNotNone(order.utm_session_id)
        utm_session = order.utm_session
        self.assertEqual(utm_session.fbc, fbc)
        self.assertEqual(utm_session.fbclid, 'fresh-click')
        self.assertIsNone(utm_session.fbp)
        self.assertIsNone(utm_session.gclid)
        self.assertIsNone(utm_session.ttclid)
        self.assertIsNone(utm_session.utm_campaign)
        self.assertIsNone(utm_session.utm_content)
        self.assertIsNone(utm_session.utm_term)

    def test_fbp_alone_does_not_synthesize_attribution(self):
        order = self._unattributed_order()
        request = self._order_link_request(fbp='fb.1.1700000000000.browser-id')

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertIsNone(order.utm_source)
        self.assertFalse(UTMSession.objects.exists())

    def test_malformed_fbc_does_not_synthesize_attribution(self):
        order = self._unattributed_order()
        request = self._order_link_request(fbc='fb.1.bad-timestamp.click-id')

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertFalse(UTMSession.objects.exists())

    def test_future_fbc_does_not_synthesize_attribution(self):
        order_created = timezone.now()
        order = self._unattributed_order(created=order_created)
        request = self._order_link_request(
            fbc=self._fbc(order_created + timedelta(minutes=10)),
        )

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertFalse(UTMSession.objects.exists())

    def test_stale_fbc_does_not_synthesize_attribution(self):
        order_created = timezone.now()
        order = self._unattributed_order(created=order_created)
        request = self._order_link_request(
            fbc=self._fbc(order_created - timedelta(days=8)),
        )

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertFalse(UTMSession.objects.exists())

    def test_existing_attribution_wins_over_fbc_fallback(self):
        order_created = timezone.now()
        order = self._unattributed_order(created=order_created)
        request = self._order_link_request(
            fbc=self._fbc(order_created - timedelta(days=1), 'fallback-click'),
        )
        existing = UTMSession.objects.create(
            session_key=request.session.session_key,
            utm_source='newsletter',
            utm_medium='email',
            fbclid='current-click',
        )

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertEqual(order.utm_session_id, existing.pk)
        self.assertEqual(order.utm_source, 'newsletter')
        self.assertEqual(order.utm_medium, 'email')
        existing.refresh_from_db()
        self.assertEqual(existing.fbclid, 'current-click')
        self.assertIsNone(existing.fbc)

    def test_existing_raw_order_attribution_is_not_replaced_by_fbc(self):
        order_created = timezone.now()
        order = self._unattributed_order(created=order_created)
        order.utm_source = 'partner'
        order.utm_medium = 'referral'
        order.utm_campaign = 'existing-campaign'
        order.save(update_fields=['utm_source', 'utm_medium', 'utm_campaign'])
        request = self._order_link_request(
            fbc=self._fbc(order_created - timedelta(days=1), 'fallback-click'),
        )

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertIsNone(order.utm_session_id)
        self.assertEqual(order.utm_source, 'partner')
        self.assertEqual(order.utm_medium, 'referral')
        self.assertEqual(order.utm_campaign, 'existing-campaign')
        self.assertFalse(UTMSession.objects.exists())

    def _cod_post_payload(self, delivery, full_name='UTM Buyer', phone='+380631112233'):
        return {
            'full_name': full_name,
            'phone': phone,
            'city': delivery['city'],
            'np_office': delivery['np_office'],
            'np_settlement_ref': delivery['np_settlement_ref'],
            'np_city_ref': delivery['np_city_ref'],
            'np_city_token': delivery['np_city_token'],
            'np_warehouse_ref': delivery['np_warehouse_ref'],
            'np_warehouse_token': delivery['np_warehouse_token'],
            'pay_type': 'cod',
        }

    def _visit_with_utm(self, query='utm_source=audit&utm_medium=cpc&utm_campaign=w2test'):
        # Любая не-noise страница активирует UTMTrackingMiddleware
        response = self.client.get(f'/?{query}', secure=True)
        self.assertLess(response.status_code, 500)

    def test_cod_order_gets_full_utm_attribution(self):
        """CRO-050: COD-заказ наследует UTM визита + is_converted=True."""
        self._visit_with_utm()
        self.set_cart()

        delivery = self.delivery_payload()
        response = self.client.post(
            self.order_create_url, self._cod_post_payload(delivery), secure=True
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()

        # UTM-поля скопированы в заказ
        self.assertEqual(order.utm_source, 'audit')
        self.assertEqual(order.utm_medium, 'cpc')
        self.assertEqual(order.utm_campaign, 'w2test')

        # FK на UTM-сессию и session_key на заказе
        self.assertIsNotNone(order.utm_session)
        self.assertTrue(order.session_key)

        # UserAction 'lead' с order_id записан
        lead_actions = UserAction.objects.filter(action_type='lead', order_id=order.id)
        self.assertEqual(lead_actions.count(), 1)
        self.assertEqual(lead_actions.first().utm_session_id, order.utm_session_id)

        # W2-2: UTM-сессия помечена конверсионной
        utm_session = UTMSession.objects.get(pk=order.utm_session_id)
        self.assertTrue(utm_session.is_converted)
        self.assertEqual(utm_session.conversion_type, 'lead')
        self.assertIsNotNone(utm_session.converted_at)

    def test_cod_order_utm_fallback_from_session_data(self):
        """W2-1 fallback: UTMSession-строки нет, но session['utm_data'] есть —
        UTM-поля всё равно копируются в заказ."""
        self._visit_with_utm('utm_source=fallback_src&utm_medium=email')
        # Ломаем прямой lookup: удаляем UTMSession-строку
        UTMSession.objects.all().delete()

        self.set_cart()
        delivery = self.delivery_payload()
        response = self.client.post(
            self.order_create_url, self._cod_post_payload(delivery), secure=True
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.utm_source, 'fallback_src')
        self.assertEqual(order.utm_medium, 'email')
        self.assertIsNotNone(order.utm_session)
        self.assertEqual(order.utm_session.utm_source, 'fallback_src')
        self.assertTrue(order.utm_session.is_converted)

    def test_cod_order_rebuilds_utm_session_from_first_touch_cookie(self):
        """F-071: first-touch cookie must keep attribution alive when the
        original UTMSession row and Django session UTM payload are missing.
        """
        self._visit_with_utm('utm_source=IG&utm_medium=paid_social&utm_campaign=summer')
        UTMSession.objects.all().delete()
        session = self.client.session
        session.pop('utm_data', None)
        session.pop('platform_data', None)
        session.save()

        self.set_cart()
        delivery = self.delivery_payload()
        response = self.client.post(
            self.order_create_url, self._cod_post_payload(delivery), secure=True
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.utm_source, 'instagram')
        self.assertEqual(order.utm_medium, 'paid_social')
        self.assertEqual(order.utm_campaign, 'summer')
        self.assertIsNotNone(order.utm_session_id)

        rebuilt_session = UTMSession.objects.get(pk=order.utm_session_id)
        self.assertEqual(rebuilt_session.session_key, order.session_key)
        self.assertTrue(rebuilt_session.is_converted)
        self.assertEqual(rebuilt_session.conversion_type, 'lead')

    def test_link_order_to_utm_heals_missing_order_session_key(self):
        """F-044: even a future caller that forgot the writer-side ensure must
        not leave Order.session_key empty after a durable UTM link exists.
        """
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from storefront.utm_tracking import link_order_to_utm

        request = RequestFactory().get('/?utm_source=session_heal', secure=True)
        SessionMiddleware(lambda req: None).process_request(request)
        request.user = AnonymousUser()
        request.analytics_first_touch_data = {
            'utm_source': 'session_heal',
            'utm_medium': 'test',
        }
        self.assertIsNone(request.session.session_key)

        order = Order.objects.create(
            full_name='Session Heal Buyer',
            phone='+380501112233',
            city='Київ',
            np_office='Відділення №1',
            pay_type='cod',
            payment_status='unpaid',
            total_sum=130,
            source='web',
        )

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertIsNotNone(request.session.session_key)
        self.assertEqual(order.session_key, request.session.session_key)
        self.assertEqual(order.utm_session.session_key, order.session_key)

    def test_link_order_to_utm_normalizes_legacy_ai_first_touch(self):
        """F-084: a raw legacy cookie must rebuild canonical AI attribution."""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from storefront.utm_tracking import link_order_to_utm

        request = RequestFactory().get('/', secure=True)
        SessionMiddleware(lambda req: None).process_request(request)
        request.user = AnonymousUser()
        request.analytics_first_touch_data = {'utm_source': 'chatgpt.com'}

        order = Order.objects.create(
            full_name='AI Attribution Buyer',
            phone='+380501112233',
            city='Київ',
            np_office='Відділення №1',
            pay_type='cod',
            payment_status='unpaid',
            total_sum=130,
            source='web',
        )

        link_order_to_utm(request, order)

        order.refresh_from_db()
        self.assertEqual(order.utm_source, 'chatgpt')
        self.assertEqual(order.utm_medium, 'ai')
        self.assertEqual(order.utm_session.utm_source, 'chatgpt')
        self.assertEqual(order.utm_session.utm_medium, 'ai')

    def test_cod_order_tracking_context_with_fbclid_synthesis(self):
        """W2-1: click-ID контекст пишется в payment_payload.tracking для COD;
        fbc синтезируется из fbclid при отсутствии куки _fbc."""
        self.client.cookies['_fbp'] = 'fb.1.1700000000000.111111'
        self._visit_with_utm('utm_source=facebook&fbclid=TEST_FBCLID_123')

        self.set_cart()
        delivery = self.delivery_payload()
        response = self.client.post(
            self.order_create_url, self._cod_post_payload(delivery), secure=True
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()

        tracking = (order.payment_payload or {}).get('tracking') or {}
        self.assertEqual(tracking.get('fbp'), 'fb.1.1700000000000.111111')
        # fbc синтезирован из fbclid (first-touch кука сохранила его)
        self.assertTrue(str(tracking.get('fbc', '')).startswith('fb.1.'))
        self.assertTrue(str(tracking.get('fbc', '')).endswith('TEST_FBCLID_123'))
        self.assertTrue(tracking.get('external_id'))
        self.assertTrue(tracking.get('client_ip_address') or tracking.get('client_user_agent'))
