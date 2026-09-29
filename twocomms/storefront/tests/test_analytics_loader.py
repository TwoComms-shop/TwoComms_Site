import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

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

    def _run_loader_runtime(self, search="", cookies=None, legacy=False):
        # Execute the actual loader in an offline browser-shaped VM. All timers
        # and external pixel IDs are disabled; no conversion/network is sent.
        harness = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const cookies = new Map(Object.entries(input.cookies));
const writes = [];
const doc = {
  documentElement: {
    getAttribute: name => name === 'data-tiktok-test-event-code' ? 'offline-harness' : '',
    dataset: {},
  },
  readyState: 'complete',
  getElementById: () => null,
  addEventListener: () => {},
};
Object.defineProperty(doc, 'cookie', {
  get: () => Array.from(cookies, ([key, value]) => key + '=' + encodeURIComponent(value)).join('; '),
  set: value => {
    const pair = value.split(';', 1)[0];
    const split = pair.indexOf('=');
    const key = pair.slice(0, split);
    cookies.set(key, decodeURIComponent(pair.slice(split + 1)));
    writes.push(key);
  },
});
const win = {
  location: { search: input.search, protocol: 'https:' },
  navigator: {},
  addEventListener: () => {},
  removeEventListener: () => {},
};
class Clock extends Date { static now() { return 1800000000000; } }
vm.runInNewContext(input.source, {
  window: win, document: doc, Date: Clock,
  URLSearchParams: input.legacy ? undefined : URLSearchParams,
  setTimeout: () => 0, clearTimeout: () => {},
  console: { log: () => {}, debug: () => {}, warn: () => {} },
});
const context = win.getTrackingContext();
process.stdout.write(JSON.stringify({ fbc: context.fbc, fbp: context.fbp, writes }));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            input=json.dumps({
                "source": self._loader_source(), "search": search,
                "cookies": cookies or {}, "legacy": legacy,
            }),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @skipUnless(shutil.which("node"), "Node.js is required for analytics browser runtime checks")
    def test_fbp_new_fallback_is_numeric_and_existing_identifier_is_preserved(self):
        fresh = self._run_loader_runtime()
        self.assertRegex(fresh["fbp"], r"^fb\.1\.1800000000000\.\d+$")
        self.assertEqual(fresh["writes"].count("_fbp"), 1)
        existing_fbp = "fb.1.1700000000000.retained-browser-id"
        existing = self._run_loader_runtime(cookies={"_fbp": existing_fbp})
        self.assertEqual(existing["fbp"], existing_fbp)
        self.assertNotIn("_fbp", existing["writes"])

    @skipUnless(shutil.which("node"), "Node.js is required for analytics browser runtime checks")
    def test_fbc_tracks_latest_click_without_refreshing_same_click_timestamp(self):
        old_fbc = "fb.1.1700000000000.old-click"
        cases = (
            ("new click replaces old cookie", "?fbclid=new-click", old_fbc, False, "fb.1.1800000000000.new-click", 1),
            ("same click preserves timestamp", "?fbclid=old-click", old_fbc, False, old_fbc, 0),
            ("same click preserves Meta appendix", "?fbclid=old-click", old_fbc + ".AQQ", False, old_fbc + ".AQQ", 0),
            ("ordinary navigation preserves cookie", "", old_fbc, False, old_fbc, 0),
            ("no click does not invent cookie", "", "", False, None, 0),
            ("first click creates cookie", "?fbclid=first-click", "", False, "fb.1.1800000000000.first-click", 1),
            ("legacy parser decodes click", "?fbclid=new%2Dclick", old_fbc, True, "fb.1.1800000000000.new-click", 1),
        )
        for label, search, fbc, legacy, expected, expected_writes in cases:
            with self.subTest(label=label):
                output = self._run_loader_runtime(
                    search=search, cookies={"_fbc": fbc} if fbc else {}, legacy=legacy,
                )
                self.assertEqual(output["fbc"], expected)
                self.assertEqual(output["writes"].count("_fbc"), expected_writes)

    def test_bfcache_restore_uses_defined_pixel_initializer(self):
        source = self._loader_source()

        self.assertNotIn("initializePixelsImmediately()", source)
        self.assertIn("function handleBFCacherestore(event)", source)
        self.assertIn("initializePixelsDeferred();", source)

    def test_paid_landing_loads_tiktok_on_low_device(self):
        source = self._loader_source()

        self.assertIn(
            "var isPaidLanding = /[?&](gclid|fbclid|ttclid|wbraid|gbraid|msclkid|utm_source|utm_medium|utm_campaign)=/i.test(win.location.search)",
            source,
        )
        self.assertIn("if (!isLowDevice || isPaidLanding) {", source)
        self.assertIn(
            "if (isPaidLanding) {\n    initializePixelsDeferred();\n  } else {",
            source,
        )

    def test_paid_instagram_checkout_is_eager_pixel_landing(self):
        source = self._loader_source()

        self.assertIn("data-checkout-state') === 'paid'", source)
        self.assertIn("data-purchase-event-id", source)

    def test_instagram_checkout_pixels_require_explicit_marketing_consent(self):
        source = self._loader_source()
        checkout_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "instagram-checkout.js"
        )
        checkout_source = checkout_path.read_text(encoding="utf-8")

        self.assertIn("twc_analytics_consent", source)
        self.assertIn("globalPrivacyControl", source)
        self.assertIn("window.__twcAnalyticsConsent !== true", checkout_source)
        self.assertLess(
            source.index("if (!analyticsConsentGranted)"),
            source.index("ensureFbpCookie();"),
        )

    def test_base_template_uses_current_analytics_loader_version(self):
        template_path = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "templates"
            / "base.html"
        )
        source = template_path.read_text(encoding="utf-8")

        self.assertIn("analytics-loader.js' %}?v=13", source)

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

    def test_legacy_order_success_template_is_absent(self):
        legacy_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "templates" / "pages" / "order_success_old.html"

        self.assertFalse(legacy_path.exists())

    def test_order_success_does_not_suppress_pixel_when_capi_already_sent(self):
        path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "templates" / "pages" / "order_success.html"
        source = path.read_text(encoding="utf-8")

        self.assertIn("var purchaseAlreadySent = sessionStorage.getItem(purchaseStorageKey);", source)
        self.assertNotIn("var purchaseAlreadySent = serverPurchaseSent || sessionStorage.getItem(purchaseStorageKey);", source)

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
