"""Pure reminder intent must not consume genuine checkout requests."""
from datetime import datetime, timezone

from django.test import SimpleTestCase

from management.services.ig_commerce_turns import parse_turn
from management.services.ig_payment_reminder_time import parse_requested_reminder


class PaymentReminderCommerceIntentTests(SimpleTestCase):
    def test_pure_reminder_keeps_checkout_intent_unchanged_in_all_languages(self):
        for text in ("Remind me to pay", "Remind me to pay in 1 hour",
                "Напомни об оплате", "Нагадай про оплату сьогодні о 15:30"):
            with self.subTest(text=text):
                self.assertTrue(parse_requested_reminder(text, None).handled)
                self.assertFalse(parse_turn(text).checkout_requested)

    def test_mixed_reminder_never_suppresses_real_checkout(self):
        for text in ("Remind me to pay and checkout two hoodies",
                "Remind me to pay after checkout now", "Remind me to pay and change the size to L"):
            with self.subTest(text=text):
                self.assertFalse(parse_requested_reminder(text, None).handled)
                self.assertFalse(parse_requested_reminder(text, datetime(2026, 10, 12, 9, tzinfo=timezone.utc)).handled)
                self.assertTrue(parse_turn(text).checkout_requested)

    def test_ordinary_checkout_still_requests_checkout(self):
        for text in ("I want to pay", "checkout", "хочу оплатить"):
            with self.subTest(text=text):
                self.assertFalse(parse_requested_reminder(text, None).handled)
                self.assertTrue(parse_turn(text).checkout_requested)
