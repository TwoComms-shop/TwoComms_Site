from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from .error_views import subdomain_not_found, subdomain_server_error


class SubdomainErrorViewTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get('/missing', HTTP_HOST='management.twocomms.shop')

    def test_management_not_found_does_not_render_shared_template(self):
        with patch('twocomms.error_views.loader.get_template') as get_template:
            response = subdomain_not_found(self.request)

        self.assertEqual(response.status_code, 404)
        self.assertIn(b'Page not found', response.content)
        get_template.assert_not_called()

    def test_subdomain_server_error_is_db_free(self):
        with patch('twocomms.error_views.loader.get_template') as get_template:
            response = subdomain_server_error(self.request)

        self.assertEqual(response.status_code, 500)
        self.assertIn(b'Server error', response.content)
        get_template.assert_not_called()
