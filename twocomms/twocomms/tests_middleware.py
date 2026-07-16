from unittest.mock import Mock, patch

from django.core.exceptions import DisallowedHost
from django.db import DatabaseError
from django.http import HttpResponseNotFound
from django.test import RequestFactory, SimpleTestCase

from .middleware import SubdomainRedirectFallbackMiddleware


class RedirectFallbackResilienceTests(SimpleTestCase):
    def test_non_storefront_hosts_skip_redirect_lookup(self):
        response = HttpResponseNotFound()

        for host in (
            'management.twocomms.shop',
            'fin.twocomms.shop',
            'dtf.twocomms.shop',
            'storage.twocomms.shop',
        ):
            request = RequestFactory().get('/missing', HTTP_HOST=host)
            middleware = SubdomainRedirectFallbackMiddleware(lambda req: response)
            with patch(
                'django.contrib.redirects.middleware.RedirectFallbackMiddleware.process_response'
            ) as fallback:
                result = middleware.process_response(request, response)

            self.assertIs(result, response)
            fallback.assert_not_called()

    def test_db_failure_preserves_404(self):
        request = RequestFactory().get('/missing', HTTP_HOST='twocomms.shop')
        middleware = SubdomainRedirectFallbackMiddleware(lambda req: HttpResponseNotFound())
        response = HttpResponseNotFound()

        with patch(
            'django.contrib.redirects.middleware.RedirectFallbackMiddleware.process_response',
            side_effect=DatabaseError('db down'),
        ):
            result = middleware.process_response(request, response)

        self.assertIs(result, response)

    def test_disallowed_subdomain_host_still_skips_redirect_lookup(self):
        request = RequestFactory().get(
            '/missing',
            HTTP_HOST='management.twocomms.shop',
        )
        request.get_host = Mock(side_effect=DisallowedHost('unlisted host'))
        middleware = SubdomainRedirectFallbackMiddleware(lambda req: HttpResponseNotFound())
        response = HttpResponseNotFound()

        with patch(
            'django.contrib.redirects.middleware.RedirectFallbackMiddleware.process_response'
        ) as fallback:
            result = middleware.process_response(request, response)

        self.assertIs(result, response)
        fallback.assert_not_called()
