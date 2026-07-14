"""
W2-1/W2-2 (TECH-060/061): UTM-привязка заказа и is_converted.

Приёмка CRO-050: визит с ?utm_source=audit → COD-заказ → в БД
utm_source='audit', utm_session FK, session_key, UserAction с order_id,
UTMSession.is_converted=True.
"""

from orders.models import Order
from storefront.models import UserAction, UTMSession

from .test_checkout import CheckoutTestSupport


class UTMOrderAttributionTests(CheckoutTestSupport):
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
