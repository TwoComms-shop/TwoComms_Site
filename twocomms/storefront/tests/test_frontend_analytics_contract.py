from pathlib import Path
from types import SimpleNamespace

from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from storefront.context_processors import analytics_settings


class FrontendAnalyticsContractTests(SimpleTestCase):
    def _shell(self, name):
        path = Path(__file__).resolve().parents[2] / 'twocomms_django_theme/templates' / name
        source = path.read_text()
        opening = source[source.index('<html'):source.index('\n<head>')]
        context = analytics_settings(SimpleNamespace())
        context.update(html_lang_attr='uk-UA', request=SimpleNamespace(
            resolver_match=SimpleNamespace(url_name='home'),
        ))
        # Intentionally omit `debug`: no debug context processor is installed.
        return Template(opening).render(Context(context))

    @override_settings(DEBUG=True, META_PIXEL_ID='offline-meta', TIKTOK_PIXEL_ID='offline-tiktok')
    def test_debug_shells_do_not_enable_live_analytics_without_debug_context_processor(self):
        for path in ('base.html', 'pages/ig_checkout.html'):
            with self.subTest(path=path):
                shell = self._shell(path)
                self.assertNotIn('offline-meta', shell)
                self.assertNotIn('offline-tiktok', shell)
                self.assertNotIn('GTM-PRLLBF9H', shell)
                self.assertNotIn('G-109EFTWM05', shell)

    @override_settings(DEBUG=False, META_PIXEL_ID='offline-meta', TIKTOK_PIXEL_ID='offline-tiktok')
    def test_production_shell_retains_exact_gtm_owners_and_vendor_destinations(self):
        shell = self._shell('base.html')
        for marker in ('data-gtm-ga-id="G-109EFTWM05"',
                       'data-gtm-clarity-id="t7u94cvpqc"',
                       'data-meta-pixel-id="offline-meta"',
                       'data-tiktok-pixel-id="offline-tiktok"'):
            self.assertIn(marker, shell)
