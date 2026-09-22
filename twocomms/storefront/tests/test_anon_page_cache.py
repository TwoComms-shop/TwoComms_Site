from unittest.mock import Mock, patch

from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from storefront.analytics_noise import is_analytics_noise_path
from storefront.views.utils import cache_page_for_anon


class AnonymousPageCacheTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/catalog/")
        self.request.user = AnonymousUser()

    def test_concurrent_cold_render_reuses_response_from_render_lock(self):
        cached_response = HttpResponse("cached")
        cache_backend = Mock()
        cache_backend.get.side_effect = [None, cached_response]
        cache_backend.add.return_value = False
        view = Mock(return_value=HttpResponse("fresh"))
        view.__name__ = "catalog"
        view.__module__ = "tests"

        with patch("storefront.views.utils.cache", cache_backend), patch(
            "storefront.views.utils.time.monotonic", side_effect=[0, 0, 1]
        ), patch("storefront.views.utils.time.sleep"):
            response = cache_page_for_anon(60)(view)(self.request)

        self.assertIs(response, cached_response)
        view.assert_not_called()

    def test_render_lock_is_released_after_successful_render(self):
        cache_backend = Mock()
        cache_backend.get.return_value = None
        cache_backend.add.return_value = True
        response = HttpResponse("fresh")
        view = Mock(return_value=response)
        view.__name__ = "catalog"
        view.__module__ = "tests"

        with patch("storefront.views.utils.cache", cache_backend):
            result = cache_page_for_anon(60)(view)(self.request)

        self.assertIs(result, response)
        cache_backend.set.assert_called_once()
        cache_backend.delete.assert_called_once()

    def test_csp_reports_skip_analytics_tracking_middleware(self):
        self.assertTrue(is_analytics_noise_path("/csp-report/"))
