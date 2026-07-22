from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from management.services import instagram_bot as bot


class SecretRedactionTests(SimpleTestCase):
    def test_query_credentials_are_redacted(self):
        raw = "error access_token=direct-secret&client_secret=app-secret api_key=gemini-secret"
        safe = bot._redact_secret_text(raw)
        self.assertNotIn("direct-secret", safe)
        self.assertNotIn("app-secret", safe)
        self.assertNotIn("gemini-secret", safe)
        self.assertEqual(safe.count("[REDACTED]"), 3)

    @patch("management.services.instagram_bot.log")
    def test_page_token_error_does_not_log_secret_query_value(self, log):
        settings = MagicMock()
        bot._log_token_error(
            settings,
            400,
            '{"error":{"message":"access_token=direct-secret"}}',
        )
        details = " ".join(str(call.args) for call in log.call_args_list)
        self.assertNotIn("direct-secret", details)
        self.assertIn("[REDACTED]", details)
