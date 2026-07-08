"""
W3-3 / W3-4 (SEO-010 / CRO-032): гигиена кэшируемых анонимных страниц.

Инварианты:
1. Анонимный GET главной/каталога НЕ ставит куки (Set-Cookie выключает
   LiteSpeed page cache → холодный TTFB 8-18s).
2. В кэшируемом HTML НЕТ inline csrfmiddlewaretoken (иначе cache-hit
   отдаёт чужой токен → 403 на POST /i18n/setlang).
3. /api/bootstrap/ (ленивая выдача кук) ставит csrftoken и analytics-куки
   и всегда no-store.
4. Персональные AJAX-эндпоинты корзины — no-store.
5. Landing с UTM/click-id ПОЛУЧАЕТ analytics-куки (first-touch атрибуция
   важнее кэша; такие URL уникальны по query-string).
"""

from django.core.cache import cache
from django.test import Client, TestCase


REQ = {
    'HTTP_HOST': 'twocomms.shop',
    'SERVER_PORT': '443',
    'wsgi.url_scheme': 'https',
}
UA = {'HTTP_USER_AGENT': 'Mozilla/5.0 (X11; Linux x86_64)'}


class AnonymousCacheHygieneTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(**UA, **REQ)

    def test_anonymous_home_get_sets_no_cookies(self):
        """W3-3: чистый анонимный GET → ни одной Set-Cookie."""
        response = self.client.get('/', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(response.cookies.keys()), [],
            f"Anonymous GET / must not set cookies, got: {list(response.cookies.keys())}",
        )

    def test_cached_home_has_no_inline_csrf_token(self):
        """W3-4: в HTML нет «запечённого» значения csrfmiddlewaretoken."""
        response = self.client.get('/', secure=True)
        content = response.content.decode('utf-8')
        # Пустой value="" допустим (заполняется JS из cookie);
        # непустой 64-символьный токен — это баг.
        import re
        baked = re.findall(
            r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', content
        )
        self.assertEqual(
            baked, [],
            f"Cached HTML must not contain a non-empty inline CSRF token, found: {baked}",
        )

    def test_bootstrap_endpoint_sets_csrf_and_analytics_cookies(self):
        """W3-3: /api/bootstrap/ выдаёт csrftoken + twc_vid, всегда no-store."""
        response = self.client.get('/api/bootstrap/', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)
        self.assertIn('twc_vid', response.cookies)
        self.assertIn('no-store', response.get('Cache-Control', ''))

    def test_utm_landing_still_receives_analytics_cookies(self):
        """W3-3: paid-landing получает first-touch куки несмотря на гейт."""
        response = self.client.get(
            '/', {'utm_source': 'facebook', 'utm_medium': 'cpc'}, secure=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('twc_vid', response.cookies)

    def test_cart_endpoints_are_no_store(self):
        """W3-4: персональные cart-эндпоинты не должны попадать в page cache."""
        for url in ('/cart/summary/', '/cart/count/'):
            response = self.client.get(url, secure=True)
            self.assertEqual(response.status_code, 200, url)
            cc = response.get('Cache-Control', '')
            self.assertTrue(
                'no-store' in cc or 'no-cache' in cc,
                f"{url} must be no-store/no-cache, got Cache-Control: {cc}",
            )

    def test_second_anonymous_home_get_is_cache_hit_without_cookies(self):
        """W3-3: повторный GET (cache-hit) тоже чистый от Set-Cookie."""
        self.client.get('/', secure=True)
        fresh = Client(**UA, **REQ)
        response = fresh.get('/', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.cookies.keys()), [])
