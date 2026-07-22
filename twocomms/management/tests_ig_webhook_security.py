import hashlib
import hmac
import json
import os
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase

from management.models import InstagramBotSettings
from management.services import instagram_bot as bot


class WebhookSignatureTests(SimpleTestCase):
    def test_missing_secret_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bot.verify_signature(b"{}", ""))
            self.assertEqual(bot.webhook_signature_status()["state"], "missing_secret")

    def test_explicit_unsigned_override_is_visible(self):
        with patch.dict(os.environ, {"IG_BOT_ALLOW_UNSIGNED_WEBHOOKS": "true"}, clear=True):
            self.assertTrue(bot.verify_signature(b"{}", ""))
            status = bot.webhook_signature_status()
        self.assertTrue(status["unsigned_override"])
        self.assertEqual(status["state"], "development_override")

    def test_valid_and_invalid_hmac(self):
        body = b'{"object":"instagram"}'
        secret = "test-secret"
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"IG_APP_SECRET": secret}, clear=True):
            self.assertTrue(bot.verify_signature(body, f"sha256={digest}"))
            self.assertFalse(bot.verify_signature(body, "sha256=wrong"))
            self.assertFalse(bot.verify_signature(body, ""))


class WebhookEndpointSecurityTests(TestCase):
    def setUp(self):
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled"])
        self.client = Client()

    def test_unsigned_post_is_rejected_without_secret(self):
        payload = json.dumps({"entry": []})
        with patch.dict(os.environ, {}, clear=True), patch("management.bot_webhook.bot.log"):
            response = self.client.post(
                "/bot/webhook/", data=payload, content_type="application/json",
                HTTP_HOST="management.twocomms.shop",
            )
        self.assertEqual(response.status_code, 403)

    def test_unsigned_post_requires_explicit_override(self):
        payload = json.dumps({"entry": []})
        with patch.dict(os.environ, {"IG_BOT_ALLOW_UNSIGNED_WEBHOOKS": "1"}, clear=True), \
             patch("management.bot_webhook.bot.record_raw_event"), \
             patch("management.bot_webhook.bot.handle_webhook_payload", return_value=0) as handle:
            response = self.client.post(
                "/bot/webhook/", data=payload, content_type="application/json",
                HTTP_HOST="management.twocomms.shop",
            )
        self.assertEqual(response.status_code, 200)
        handle.assert_called_once()
