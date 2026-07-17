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
        with patch("facebook_business.api.FacebookAdsApi.init"):
            service = FacebookConversionsService()

        self.assertTrue(service.enabled)
        self.assertEqual(service.Event.__module__, "facebook_business.adobjects.serverside.event")
        self.assertEqual(
            service.EventRequest.__module__,
            "facebook_business.adobjects.serverside.event_request",
        )

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

    def test_purchase_event_time_prefers_verified_transition_timestamp(self):
        service = FacebookConversionsService.__new__(FacebookConversionsService)
        transition_time = int(time.time()) - 30
        order = SimpleNamespace(
            order_number="TWC-TEST",
            payment_payload={"facebook_events": {"purchase_event_time": transition_time}},
            created=None,
        )

        self.assertEqual(service._calculate_event_time(order), transition_time)
