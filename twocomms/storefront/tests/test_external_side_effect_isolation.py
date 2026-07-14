from django.test import SimpleTestCase

from orders.telegram_notifications import TelegramNotifier


class ExternalSideEffectIsolationTests(SimpleTestCase):
    def test_test_settings_disable_real_telegram_delivery(self):
        """Production credentials must never leak into a Django test run."""
        self.assertFalse(TelegramNotifier().is_configured())
