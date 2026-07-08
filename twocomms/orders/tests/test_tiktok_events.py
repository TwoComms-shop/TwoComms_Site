"""
W2-6 (AN-020): TikTok Events API — стандартные имена событий + Events API 2.0.
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from orders.models import Order


@override_settings(
    TIKTOK_EVENTS_ACCESS_TOKEN='test-token',
    TIKTOK_EVENTS_PIXEL_CODE='test-pixel',
)
class TikTokEventNameMappingTests(TestCase):
    def setUp(self):
        # Сбрасываем singleton, чтобы подхватились override_settings
        import orders.tiktok_events_service as svc_module
        svc_module._tiktok_service = None
        self.svc_module = svc_module

        self.order = Order.objects.create(
            full_name='TT Buyer',
            phone='+380501112255',
            city='Київ',
            np_office='Відділення №1',
            pay_type='online_full',
            status='new',
            payment_status='paid',
            total_sum=Decimal('300'),
        )

    def tearDown(self):
        self.svc_module._tiktok_service = None

    def _get_service(self):
        return self.svc_module.get_tiktok_events_service()

    def test_endpoint_is_events_api_v2(self):
        svc = self._get_service()
        self.assertIn('/event/track/', svc.api_endpoint)
        self.assertNotIn('/pixel/track/', svc.api_endpoint)

    def test_purchase_mapped_to_complete_payment(self):
        svc = self._get_service()
        payload = svc._build_payload(self.order, 'Purchase', 'evt-1', None, None)
        self.assertEqual(payload['event_source'], 'web')
        self.assertEqual(payload['event_source_id'], 'test-pixel')
        event = payload['data'][0]
        self.assertEqual(event['event'], 'CompletePayment')
        self.assertEqual(event['event_id'], 'evt-1')  # event_id не меняется (дедуп)
        self.assertIsInstance(event['event_time'], int)

    def test_lead_mapped_to_place_an_order(self):
        svc = self._get_service()
        payload = svc._build_payload(self.order, 'Lead', 'evt-2', None, None)
        event = payload['data'][0]
        self.assertEqual(event['event'], 'PlaceAnOrder')
        self.assertEqual(event['event_id'], 'evt-2')

    def test_standard_names_pass_through(self):
        svc = self._get_service()
        payload = svc._build_payload(self.order, 'ViewContent', 'evt-3', None, None)
        self.assertEqual(payload['data'][0]['event'], 'ViewContent')

    def test_send_event_posts_mapped_payload(self):
        svc = self._get_service()
        with patch.object(svc.session, 'post') as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            mock_post.return_value.json.return_value = {'code': 0}
            ok = svc.send_event(self.order, 'Purchase', 'evt-4')
        self.assertTrue(ok)
        sent = mock_post.call_args.kwargs['json']
        self.assertEqual(sent['data'][0]['event'], 'CompletePayment')
