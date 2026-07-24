from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from management.services import instagram_bot as bot


class _MemoryCache:
    def __init__(self):
        self.values = {}

    def add(self, key, value, timeout=None):
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def incr(self, key):
        self.values[key] += 1
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value, timeout=None):
        self.values[key] = value


class InstagramMetaContractTests(SimpleTestCase):
    def test_graph_url_builder_is_versioned_and_rejects_external_paths(self):
        self.assertEqual(
            bot._graph_url("/me/accounts", {"fields": "name"}),
            "https://graph.facebook.com/v25.0/me/accounts?fields=name",
        )
        for path in (
            "https://evil.example/v25.0/me",
            "/v24.0/me",
            "/me#fragment",
            "/me?client_secret=leak",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    bot._graph_url(path)

    @patch("management.services.instagram_bot._http", return_value=(200, "{}"))
    def test_graph_transport_moves_access_token_out_of_url(self, http):
        code, body = bot._graph_http(
            "https://graph.facebook.com/v25.0/me?fields=id&access_token=secret-token",
        )

        self.assertEqual((code, body), (200, "{}"))
        called_url = http.call_args.args[0]
        self.assertNotIn("access_token", called_url)
        self.assertEqual(http.call_args.kwargs["headers"]["Authorization"], "Bearer secret-token")

    @patch("management.services.instagram_bot._graph_http", return_value=(200, '{"access_token":"ll"}'))
    @patch("management.services.instagram_bot.app_secret", return_value="app-secret")
    def test_long_lived_exchange_keeps_credentials_out_of_graph_query(self, _secret, graph_http):
        self.assertEqual(bot._exchange_long_lived("short-token"), "ll")
        called_url = graph_http.call_args.args[0]
        self.assertEqual(called_url, "https://graph.facebook.com/v25.0/oauth/access_token")
        self.assertNotIn("client_secret", called_url)
        self.assertNotIn("fb_exchange_token", called_url)
        body = graph_http.call_args.kwargs["data"].decode("utf-8")
        self.assertIn("client_secret=app-secret", body)
        self.assertIn("fb_exchange_token=short-token", body)

    def test_capability_status_keeps_allowlist_permission_and_delivery_separate(self):
        settings = SimpleNamespace(allowed_senders="123,456", direct_source="env")
        with patch.object(bot, "resolve_direct_token", return_value="token"):
            status = bot.meta_capability_status(settings)

        self.assertEqual(status["local_allowlist"], "restricted")
        self.assertTrue(status["token_configured"])
        self.assertEqual(status["token_permission"], "unknown")
        self.assertEqual(status["account_access"], "unknown")
        self.assertEqual(status["recipient_delivery"], "per_recipient")

    def test_rate_observability_counts_endpoint_classes_without_quota_claims(self):
        self.assertEqual(bot._meta_endpoint_class(f"{bot.GRAPH}/page/conversations"), "conversations")
        self.assertEqual(bot._meta_endpoint_class(f"{bot.GRAPH}/page/messages"), "send")
        self.assertEqual(bot._meta_endpoint_class(f"{bot.GRAPH}/oauth/access_token"), "oauth")
        fake_cache = _MemoryCache()
        with patch.object(bot, "cache", fake_cache):
            bot._record_meta_http_observation("send", 429)
            bot._record_meta_http_observation("conversations", 200, '{"error":{"code":4}}')
            status = bot.meta_rate_limit_status()

        self.assertEqual(status["endpoints"]["send"]["requests"], 1)
        self.assertEqual(status["endpoints"]["send"]["rate_limited"], 1)
        self.assertEqual(status["endpoints"]["conversations"]["requests"], 1)
        self.assertEqual(status["endpoints"]["conversations"]["rate_limited"], 1)
        self.assertTrue(status["degraded"])
        self.assertNotIn("remaining", status)
