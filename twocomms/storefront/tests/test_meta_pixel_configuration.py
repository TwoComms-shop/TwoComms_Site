from types import SimpleNamespace
from unittest.mock import patch
from decimal import Decimal
import time

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from management.services.ig_meta_events import _has_capi_env
from orders.facebook_conversions_service import FacebookConversionsService
from storefront.context_processors import analytics_settings


class MetaPixelConfigurationTests(SimpleTestCase):
    def test_legacy_settings_name_is_an_alias_of_canonical_pixel_id(self):
        self.assertEqual(settings.FACEBOOK_PIXEL_ID, settings.META_PIXEL_ID)

    @override_settings(META_PIXEL_ID="", FACEBOOK_PIXEL_ID="legacy-value")
    def test_context_processor_does_not_add_a_second_fallback(self):
        self.assertEqual(analytics_settings(SimpleNamespace())["META_PIXEL_ID"], "")

    @override_settings(
        META_PIXEL_ID="canonical-value",
        FACEBOOK_PIXEL_ID="",
        FACEBOOK_CONVERSIONS_API_TOKEN="",
    )
    def test_storefront_capi_reads_canonical_pixel_id(self):
        with patch("orders.facebook_conversions_service.logger.error"):
            service = FacebookConversionsService()

        self.assertEqual(service.pixel_id, "canonical-value")

    @override_settings(
        META_PIXEL_ID="canonical-value",
        FACEBOOK_PIXEL_ID="",
        FACEBOOK_CONVERSIONS_API_TOKEN="token",
    )
    def test_ig_capi_gate_reads_canonical_pixel_id(self):
        self.assertTrue(_has_capi_env())

    @override_settings(
        META_PIXEL_ID="1234567890",
        FACEBOOK_PIXEL_ID="",
        FACEBOOK_CONVERSIONS_API_TOKEN="token",
    )
    def test_capi_service_loads_serverside_classes_from_sdk_submodules(self):
        # facebook-business 22+ no longer re-exports these classes from the
        # serverside package root. The service must still initialize instead
        # of silently disabling all server-side events.
        with patch("facebook_business.api.FacebookAdsApi.init") as init_mock:
            service = FacebookConversionsService()

        self.assertTrue(service.enabled)
        init_mock.assert_called_once_with(access_token="token", api_version="v25.0")
        self.assertEqual(service.Event.__module__, "facebook_business.adobjects.serverside.event")
        self.assertEqual(
            service.EventRequest.__module__,
            "facebook_business.adobjects.serverside.event_request",
        )

    @override_settings(
        META_PIXEL_ID="1234567890",
        FACEBOOK_PIXEL_ID="",
        FACEBOOK_CONVERSIONS_API_TOKEN="token",
        FACEBOOK_CAPI_API_VERSION="25.0",
    )
    def test_invalid_capi_api_version_falls_back_to_v25(self):
        with patch("facebook_business.api.FacebookAdsApi.init") as init_mock:
            service = FacebookConversionsService()

        self.assertTrue(service.enabled)
        self.assertEqual(service.api_version, "v25.0")
        init_mock.assert_called_once_with(access_token="token", api_version="v25.0")

    def test_meta_cookie_validation_rejects_untrusted_shapes(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)

        self.assertEqual(service._clean_meta_cookie("fb.1.1710000000000.click_123"), "fb.1.1710000000000.click_123")
        self.assertIsNone(service._clean_meta_cookie("not-a-meta-cookie"))
        self.assertIsNone(service._clean_meta_cookie("fb.1.1710000000000.bad value"))

    def test_local_ukrainian_phone_is_normalized_to_international_digits(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)

        self.assertEqual(service._clean_phone_digits("050 123 45 67"), "380501234567")

    @override_settings(SITE_BASE_URL="https://example.test")
    def test_default_event_source_url_is_success_route(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)

        self.assertEqual(
            service._default_event_source_url(SimpleNamespace(pk=42)),
            "https://example.test/orders/success/42/",
        )

    def test_cod_paid_value_uses_discounted_final_total(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)
        order = SimpleNamespace(
            payment_payload={},
            payment_status="paid",
            final_total=Decimal("900.00"),
            total_sum=Decimal("1000.00"),
        )

        self.assertEqual(service._extract_paid_amount(order), 900.0)

    def test_legacy_order_history_paid_amount_is_exposed_as_paid_value(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)
        order = SimpleNamespace(
            payment_payload={
                'history': [
                    {'status': 'success', 'payload': {'paidAmount': 37500}},
                ],
            },
            payment_status='prepaid',
            final_total=Decimal('2600.00'),
            total_sum=Decimal('2600.00'),
        )

        self.assertEqual(service._extract_paid_amount(order), 375.0)

    def test_purchase_event_time_prefers_verified_transition_timestamp(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)
        transition_time = int(time.time()) - 30
        order = SimpleNamespace(
            order_number="TWC-TEST",
            payment_payload={"facebook_events": {"purchase_event_time": transition_time}},
            created=None,
        )

        self.assertEqual(service._calculate_event_time(order), transition_time)

    def test_city_normalization_matches_browser_punctuation_rules(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)

        self.assertEqual(service._normalize_city_value("Київ_центр"), "київцентр")

    def test_multi_item_sku_custom_data_uses_product(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)

        class FakeManager:
            def __init__(self, items):
                self.items = items

            def select_related(self, *args):
                return self

            def all(self):
                return self.items

        def item(product_id, title):
            product = SimpleNamespace(
                id=product_id,
                get_offer_id=lambda variant_id=None, size='S': f'TC-{product_id:04d}-BLACK-{size}',
            )
            return SimpleNamespace(
                pk=product_id,
                product=product,
                product_id=product_id,
                color_variant=None,
                size='S',
                title=title,
                qty=1,
                unit_price=Decimal('100.00'),
            )

        order = SimpleNamespace(
            order_number='TWC-TEST',
            final_total=Decimal('200.00'),
            total_sum=Decimal('200.00'),
            discount_amount=Decimal('0.00'),
            items=FakeManager([item(1, 'One'), item(2, 'Two')]),
        )

        custom_data = service._prepare_custom_data(order)

        self.assertEqual(custom_data.content_type, 'product')
        self.assertEqual(custom_data.content_ids, ['TC-0001-BLACK-S', 'TC-0002-BLACK-S'])

    def test_invalid_contact_logs_do_not_include_raw_values(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)
        order = SimpleNamespace(
            pk=1,
            order_number='TWC-TEST',
            user=None,
            email='not-an-email',
            phone='not-a-phone',
            full_name='',
            city='',
            payment_payload={},
        )

        with patch("orders.facebook_conversions_service.logger.warning") as warning:
            service._prepare_user_data(order)

        messages = [call.args[0] for call in warning.call_args_list]
        self.assertTrue(any('Invalid email' in message for message in messages))
        self.assertTrue(any('Invalid phone' in message for message in messages))
        self.assertTrue(all('not-an-email' not in message and 'not-a-phone' not in message for message in messages))

    def test_purchase_event_serializes_required_meta_fields(self):
        from facebook_business.adobjects.serverside.action_source import ActionSource
        from facebook_business.adobjects.serverside.custom_data import CustomData
        from facebook_business.adobjects.serverside.event import Event
        from facebook_business.adobjects.serverside.event_request import EventRequest
        from facebook_business.adobjects.serverside.user_data import UserData

        class FakeManager:
            def __init__(self, items):
                self.items = items

            def select_related(self, *args):
                return self

            def all(self):
                return self.items

        def make_item(product_id):
            product = SimpleNamespace(
                id=product_id,
                get_offer_id=lambda variant_id=None, size='S': f'TC-{product_id:04d}-BLACK-{size}',
            )
            return SimpleNamespace(
                pk=product_id,
                product=product,
                product_id=product_id,
                color_variant=None,
                size='S',
                title=f'Product {product_id}',
                qty=1,
                unit_price=Decimal('100.00'),
            )

        order = SimpleNamespace(
            pk=42,
            order_number='TWC-TEST-42',
            payment_status='paid',
            payment_payload={'facebook_events': {'purchase_event_time': int(time.time())}},
            final_total=Decimal('200.00'),
            total_sum=Decimal('200.00'),
            discount_amount=Decimal('0.00'),
            items=FakeManager([make_item(1), make_item(2)]),
            get_purchase_event_id=lambda: 'TWC-TEST-42_purchase',
            save=lambda **kwargs: None,
        )

        service = FacebookConversionsService.__new__(FacebookConversionsService)
        service.enabled = True
        service.pixel_id = 'pixel-id'
        service.test_event_code = None
        service.Event = Event
        service.UserData = UserData
        service.CustomData = CustomData
        service.EventRequest = EventRequest
        service.ActionSource = ActionSource
        service._prepare_user_data = lambda current_order: UserData()
        service._send_request_with_retry = lambda request, current_order, event_name: SimpleNamespace(
            events_received=1,
            messages=['accepted'],
            fbtrace_id='trace-42',
        )
        service._validate_response = lambda response, current_order, event_name, event_id: True

        captured = {}
        original_request = service.EventRequest

        def capture_request(**kwargs):
            request = original_request(**kwargs)
            captured['request'] = request
            return request

        service.EventRequest = capture_request

        self.assertTrue(service.send_purchase_event(order, source_url='https://example.test/orders/success/42/'))

        event_obj = captured['request']._events[0]
        if isinstance(event_obj, dict):
            event_name = event_obj['event_name']
            event_id = event_obj['event_id']
            action_source = event_obj['action_source']
            source_url = event_obj['event_source_url']
            custom_data = event_obj['custom_data']
        else:
            event_name = event_obj._event_name
            event_id = event_obj._event_id
            action_source = event_obj._action_source
            source_url = event_obj._event_source_url
            custom_data = event_obj._custom_data
        self.assertEqual(event_name, 'Purchase')
        self.assertEqual(event_id, 'TWC-TEST-42_purchase')
        self.assertEqual(action_source, ActionSource.WEBSITE)
        self.assertEqual(source_url, 'https://example.test/orders/success/42/')
        def custom_value(name):
            return custom_data.get(name) if isinstance(custom_data, dict) else getattr(custom_data, f'_{name}')

        self.assertEqual(custom_value('currency'), 'UAH')
        self.assertEqual(custom_value('value'), 200.0)
        self.assertEqual(custom_value('content_type'), 'product')
        self.assertEqual(custom_value('content_ids'), ['TC-0001-BLACK-S', 'TC-0002-BLACK-S'])
