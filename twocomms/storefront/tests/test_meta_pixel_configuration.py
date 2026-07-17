from types import SimpleNamespace
from unittest.mock import patch

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
