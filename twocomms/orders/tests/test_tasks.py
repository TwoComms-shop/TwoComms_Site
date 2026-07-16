from unittest.mock import patch

from django.test import TestCase

from orders.models import Order
from orders.tasks import _send_notification
from orders.telegram_notifications import telegram_notifier


class TelegramNotificationTaskTests(TestCase):
    def setUp(self):
        self.order = Order.objects.create(
            full_name="Task Buyer",
            phone="+380501112233",
            city="Kyiv",
            np_office="Branch 1",
        )

    def notification_cases(self):
        return (
            ("new_order", "send_new_order_notification", {}),
            (
                "status_update",
                "send_order_status_update",
                {"old_status": "new", "new_status": "prep"},
            ),
            ("ttn_added", "send_ttn_added_notification", {}),
        )

    def test_false_notifier_result_logs_failure_without_sent_claim(self):
        for notification_type, method_name, kwargs in self.notification_cases():
            with self.subTest(notification_type=notification_type), patch.object(
                telegram_notifier, method_name, return_value=False
            ), self.assertLogs("orders.tasks", level="INFO") as logs:
                _send_notification(self.order.pk, notification_type, **kwargs)

            output = "\n".join(logs.output)
            self.assertIn("delivery failed", output)
            self.assertNotIn(" sent for order ", output)

    def test_true_notifier_result_keeps_sent_log(self):
        for notification_type, method_name, kwargs in self.notification_cases():
            with self.subTest(notification_type=notification_type), patch.object(
                telegram_notifier, method_name, return_value=True
            ), self.assertLogs("orders.tasks", level="INFO") as logs:
                _send_notification(self.order.pk, notification_type, **kwargs)

            self.assertIn(" sent for order ", "\n".join(logs.output))
