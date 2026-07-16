import json

from django.test import TestCase


class PlatformAliasTests(TestCase):
    def test_legacy_content_aliases_redirect_permanently(self):
        expected_locations = {
            "/help-center/": "/dopomoga/",
            "/kontakty/": "/contacts/",
            "/ru/help-center/": "/ru/dopomoga/",
            "/ru/kontakty/": "/ru/contacts/",
            "/en/help-center/": "/en/dopomoga/",
            "/en/kontakty/": "/en/contacts/",
        }

        for path, location in expected_locations.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response["Location"], location)

    def test_manifest_aliases_serve_the_same_valid_manifest(self):
        canonical = self.client.get("/site.webmanifest")
        legacy = self.client.get("/manifest.webmanifest")

        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(canonical["Content-Type"], "application/manifest+json; charset=utf-8")
        self.assertEqual(json.loads(canonical.content), json.loads(legacy.content))

    def test_service_worker_and_favicon_are_available_at_root(self):
        service_worker = self.client.get("/sw.js")
        favicon = self.client.get("/favicon.ico")

        self.assertEqual(service_worker.status_code, 200)
        self.assertEqual(service_worker["Content-Type"], "application/javascript; charset=utf-8")
        self.assertEqual(service_worker["Service-Worker-Allowed"], "/")
        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(favicon["Content-Type"], "image/x-icon")

    def test_dropshipper_legacy_root_redirects_to_orders_namespace(self):
        response = self.client.get("/dropshipper/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/orders/dropshipper/")
