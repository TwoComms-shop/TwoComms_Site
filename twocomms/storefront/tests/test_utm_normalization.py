"""
W2-8 (AN-032/AN-033): нормализация utm_source + детект AI-трафика.
"""
from django.test import Client, SimpleTestCase, TestCase

from storefront.models import SiteSession, UTMSession
from storefront import utm_utils
from storefront.utm_utils import (
    normalize_utm_source,
    normalize_utm_attribution,
    infer_click_id_attribution,
    detect_ai_source,
    AI_SOURCES,
)


class ParseFbcTests(SimpleTestCase):
    def test_valid_fbc_returns_click_timestamp_and_id(self):
        parse_fbc = getattr(utm_utils, 'parse_fbc', lambda value: None)

        parsed = parse_fbc('fb.1.1700000000000.click-id')

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.created_at_ms, 1700000000000)
        self.assertEqual(parsed.click_id, 'click-id')

    def test_invalid_fbc_values_are_rejected(self):
        parse_fbc = getattr(utm_utils, 'parse_fbc', lambda value: None)
        invalid_values = (
            None,
            '',
            'xx.1.1700000000000.click-id',
            'fb.-1.1700000000000.click-id',
            'fb.domain.1700000000000.click-id',
            'fb.1.170000000000.click-id',
            'fb.1.not-a-timestamp.click-id',
            'fb.1.1700000000000.',
            'fb.1.1700000000000.click id',
            'fb.1.1700000000000.click\n-id',
            'fb.1.1700000000000.cl\N{LATIN SMALL LETTER I WITH ACUTE}ck-id',
            f"fb.1.1700000000000.{'x' * 240}",
        )

        for value in invalid_values:
            with self.subTest(value=repr(value)):
                self.assertIsNone(parse_fbc(value))


class NormalizeUtmSourceTests(TestCase):
    def test_instagram_aliases_collapse(self):
        for alias in ('IG', 'Instagram', 'IGShopping', 'Inst_Vid', 'insta', 'l.instagram.com'):
            self.assertEqual(normalize_utm_source(alias), 'instagram', alias)

    def test_facebook_aliases_collapse(self):
        for alias in ('fb', 'FB', 'Meta', 'facebook_ads', 'm.facebook.com'):
            self.assertEqual(normalize_utm_source(alias), 'facebook', alias)

    def test_unknown_source_lowercased_not_renamed(self):
        self.assertEqual(normalize_utm_source('Newsletter'), 'newsletter')

    def test_empty_and_none(self):
        self.assertIsNone(normalize_utm_source(None))
        self.assertIsNone(normalize_utm_source('  '))

    def test_ai_sources_normalized(self):
        self.assertEqual(normalize_utm_source('chat.openai.com'), 'chatgpt')
        self.assertIn(normalize_utm_source('perplexity.ai'), AI_SOURCES)
        self.assertEqual(normalize_utm_source('you.com'), 'you')
        self.assertIn(normalize_utm_source('you.com'), AI_SOURCES)
        self.assertEqual(normalize_utm_source('poe.com'), 'poe')
        self.assertIn(normalize_utm_source('poe.com'), AI_SOURCES)
        self.assertEqual(normalize_utm_attribution('you.com', None), ('you', 'ai'))
        self.assertEqual(normalize_utm_attribution('poe.com', None), ('poe', 'ai'))

    def test_click_ids_map_to_canonical_source_and_medium(self):
        cases = {
            'fbclid': ('facebook', 'paid_social'),
            'gclid': ('google', 'cpc'),
            'ttclid': ('tiktok', 'paid_social'),
        }
        for click_field, expected in cases.items():
            with self.subTest(click_field=click_field):
                self.assertEqual(
                    infer_click_id_attribution({click_field: 'click-id'}),
                    expected,
                )


class DetectAiSourceTests(TestCase):
    def test_known_ai_referrers(self):
        cases = {
            'https://chatgpt.com/': 'chatgpt',
            'https://chat.openai.com/c/abc': 'chatgpt',
            'https://www.perplexity.ai/search?q=x': 'perplexity',
            'https://gemini.google.com/app': 'gemini',
            'https://claude.ai/chat/1': 'claude',
            'https://you.com/search?q=x': 'you',
            'https://poe.com/chat/1': 'poe',
        }
        for ref, expected in cases.items():
            self.assertEqual(detect_ai_source(ref), expected, ref)

    def test_non_ai_referrer(self):
        self.assertIsNone(detect_ai_source('https://google.com/search?q=x'))
        self.assertIsNone(detect_ai_source(''))
        self.assertIsNone(detect_ai_source(None))


class MiddlewareIntegrationTests(TestCase):
    """Интеграция: middleware нормализует source и детектит AI-referrer."""

    def _utm_session(self):
        return UTMSession.objects.order_by('-id').first()

    def test_alias_normalized_in_session(self):
        client = Client(HTTP_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64)')
        client.get('/?utm_source=IG&utm_medium=cpc')
        session = self._utm_session()
        self.assertIsNotNone(session)
        self.assertEqual(session.utm_source, 'instagram')
        self.assertEqual(session.utm_medium, 'cpc')

    def test_ai_referrer_creates_ai_channel(self):
        client = Client(HTTP_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64)')
        client.get('/', HTTP_REFERER='https://chatgpt.com/')
        session = self._utm_session()
        self.assertIsNotNone(session)
        self.assertEqual(session.utm_source, 'chatgpt')
        self.assertEqual(session.utm_medium, 'ai')

    def test_ai_utm_source_gets_ai_medium(self):
        client = Client(HTTP_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64)')
        client.get('/?utm_source=perplexity')
        session = self._utm_session()
        self.assertIsNotNone(session)
        self.assertEqual(session.utm_source, 'perplexity')
        self.assertEqual(session.utm_medium, 'ai')

    def test_ai_query_normalizes_first_touch_snapshot(self):
        """F-084: the fallback cookie/session path must not retain an alias."""
        client = Client(HTTP_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64)')

        client.get('/?utm_source=chatgpt.com')

        site_session = SiteSession.objects.get()
        self.assertEqual(site_session.first_touch_data['utm_source'], 'chatgpt')
        self.assertEqual(site_session.first_touch_data['utm_medium'], 'ai')

    def test_click_id_only_visit_creates_durable_attribution(self):
        cases = {
            'fbclid': ('facebook', 'paid_social'),
            'gclid': ('google', 'cpc'),
            'ttclid': ('tiktok', 'paid_social'),
        }

        for index, (click_field, expected) in enumerate(cases.items(), start=1):
            with self.subTest(click_field=click_field):
                client = Client(
                    HTTP_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64)'
                )
                client.get(f'/?{click_field}=click-{index}')

                utm_session = UTMSession.objects.get(
                    **{click_field: f'click-{index}'}
                )
                self.assertEqual(
                    (utm_session.utm_source, utm_session.utm_medium),
                    expected,
                )
                site_session = SiteSession.objects.get(
                    session_key=utm_session.session_key
                )
                self.assertEqual(
                    (
                        site_session.first_touch_data['utm_source'],
                        site_session.first_touch_data['utm_medium'],
                    ),
                    expected,
                )

    def test_explicit_utm_wins_over_click_id_inference(self):
        client = Client(HTTP_USER_AGENT='Mozilla/5.0 (X11; Linux x86_64)')

        client.get('/?utm_source=newsletter&utm_medium=email&fbclid=click-explicit')

        utm_session = UTMSession.objects.get(fbclid='click-explicit')
        self.assertEqual(utm_session.utm_source, 'newsletter')
        self.assertEqual(utm_session.utm_medium, 'email')
