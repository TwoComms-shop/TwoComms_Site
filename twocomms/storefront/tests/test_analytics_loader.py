from pathlib import Path

from django.test import SimpleTestCase


class AnalyticsLoaderRegressionTests(SimpleTestCase):
    @staticmethod
    def _loader_source():
        loader_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "analytics-loader.js"
        )
        return loader_path.read_text(encoding="utf-8")

    def test_bfcache_restore_uses_defined_pixel_initializer(self):
        source = self._loader_source()

        self.assertNotIn("initializePixelsImmediately()", source)
        self.assertIn("function handleBFCacherestore(event)", source)
        self.assertIn("initializePixelsDeferred();", source)

    def test_paid_landing_loads_tiktok_on_low_device(self):
        source = self._loader_source()

        self.assertIn(
            "var isPaidLanding = /[?&](gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign)=/i.test(win.location.search);",
            source,
        )
        self.assertIn("if (!isLowDevice || isPaidLanding) {", source)
        self.assertIn(
            "if (isPaidLanding) {\n    initializePixelsDeferred();\n  } else {",
            source,
        )

    def test_base_template_uses_current_analytics_loader_version(self):
        template_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "templates"
            / "base.html"
        )
        source = template_path.read_text(encoding="utf-8")

        self.assertIn("analytics-loader.js' %}?v=12", source)

    def test_non_standard_meta_events_use_track_custom_and_keep_buffer_type(self):
        source = self._loader_source()

        self.assertIn("var metaTrackMethod = standardMetaEvents[eventName] ? 'track' : 'trackCustom';", source)
        self.assertIn("custom: metaTrackMethod === 'trackCustom'", source)
        self.assertIn("var method = buffered.custom ? 'trackCustom' : 'track';", source)

    def test_catalog_does_not_send_duplicate_meta_view_content(self):
        main_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "static" / "js" / "main.js"
        source = main_path.read_text(encoding="utf-8")

        catalog_block = source.split("// GA4 select_item на листингах.", 1)[1].split("// GA4 view_item_list", 1)[0]
        self.assertNotIn("trackEvent('ViewContent'", catalog_block)
        self.assertIn("data-default-offer-id", source)

    def test_analytics_test_page_has_no_automatic_funnel(self):
        page_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "templates" / "pages" / "test_analytics.html"
        source = page_path.read_text(encoding="utf-8")

        self.assertIn("allowPurchaseTest", source)
        self.assertIn("meta_test=1", source)
        self.assertNotIn("АВТОМАТИЧЕСКАЯ ОТПРАВКА ВСЕХ СОБЫТИЙ", source)

    def test_initiate_checkout_has_one_meta_guard_for_form_and_monobank(self):
        main_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "static" / "js" / "main.js"
        mono_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "static" / "js" / "modules" / "checkout-mono.js"
        main_source = main_path.read_text(encoding="utf-8")
        mono_source = mono_path.read_text(encoding="utf-8")

        self.assertIn("window.__twcInitiateCheckoutMetaSent = true;", main_source)
        self.assertGreaterEqual(mono_source.count("!window.__twcInitiateCheckoutMetaSent"), 2)

    def test_city_normalization_keeps_ukrainian_letters(self):
        source = self._loader_source()

        self.assertIn("replace(/[^\\p{L}\\p{N}]/gu, '')", source)

    def test_legacy_order_success_template_is_marked_unrouted(self):
        legacy_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "templates" / "pages" / "order_success_old.html"
        source = legacy_path.read_text(encoding="utf-8")

        self.assertIn("LEGACY/UNROUTED TEMPLATE", source)

    def test_nova_poshta_selection_is_not_mislabelled_as_meta_find_location(self):
        main_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "static" / "js" / "main.js"
        source = main_path.read_text(encoding="utf-8")

        self.assertIn("trackEvent('SelectShippingPoint'", source)
        self.assertNotIn("trackEvent('FindLocation'", source)

    def test_contact_event_does_not_send_raw_form_fields_to_meta(self):
        path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "templates" / "pages" / "contacts.html"
        source = path.read_text(encoding="utf-8")

        self.assertIn("window.trackEvent('Contact', {method:'form_submit'})", source)
        self.assertNotIn("name:name", source)
        self.assertNotIn("subject:subject", source)
