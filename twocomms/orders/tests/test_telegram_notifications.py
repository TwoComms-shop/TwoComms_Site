from http.client import RemoteDisconnected
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from orders.telegram_notifications import TelegramNotifier


class TelegramResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class TelegramSendMessageRetryTests(SimpleTestCase):
    def make_notifier(self, admin_id="admin-one"):
        return TelegramNotifier(
            bot_token="bot-token-secret",
            admin_id=admin_id,
            chat_id="",
        )

    def test_remote_disconnect_then_success_retries_and_reports_recovery(self):
        post = Mock(
            side_effect=[
                requests.ConnectionError(RemoteDisconnected("remote closed")),
                TelegramResponse(200, {"ok": True, "result": {"message_id": 17}}),
            ]
        )

        with patch("orders.telegram_notifications.requests.post", post), patch(
            "time.sleep"
        ) as sleep, self.assertLogs("orders.telegram_notifications", level="INFO") as logs:
            delivered = self.make_notifier().send_message("private-message")

        self.assertTrue(delivered)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        self.assertIn("retry_recovered", "\n".join(logs.output))

    def test_tls_disconnect_then_success_retries(self):
        post = Mock(
            side_effect=[
                requests.exceptions.SSLError("tls details must stay private"),
                TelegramResponse(200, {"ok": True, "result": {"message_id": 18}}),
            ]
        )

        with patch("orders.telegram_notifications.requests.post", post), patch(
            "time.sleep"
        ) as sleep:
            delivered = self.make_notifier().send_message("private-message")

        self.assertTrue(delivered)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()

    def test_timeout_exhaustion_is_bounded_to_three_attempts(self):
        post = Mock(side_effect=requests.Timeout("timeout details must stay private"))

        with patch("orders.telegram_notifications.requests.post", post), patch(
            "time.sleep"
        ) as sleep, self.assertLogs("orders.telegram_notifications", level="WARNING") as logs:
            delivered = self.make_notifier().send_message("private-message")

        self.assertFalse(delivered)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("retry_exhausted", "\n".join(logs.output))

    def test_http_500_and_429_are_retried(self):
        for retry_status in (500, 429):
            with self.subTest(status=retry_status):
                post = Mock(
                    side_effect=[
                        TelegramResponse(retry_status, {"ok": False}),
                        TelegramResponse(200, {"ok": True, "result": {"message_id": 19}}),
                    ]
                )
                with patch("orders.telegram_notifications.requests.post", post), patch(
                    "time.sleep"
                ) as sleep:
                    delivered = self.make_notifier().send_message("private-message")

                self.assertTrue(delivered)
                self.assertEqual(post.call_count, 2)
                sleep.assert_called_once()

    def test_http_200_ok_false_and_non_429_4xx_are_not_retried(self):
        cases = (
            TelegramResponse(200, {"ok": False, "description": "rejected"}),
            TelegramResponse(400, {"ok": False, "description": "bad request"}),
        )
        for response in cases:
            with self.subTest(status=response.status_code):
                post = Mock(return_value=response)
                with patch("orders.telegram_notifications.requests.post", post), patch(
                    "time.sleep"
                ) as sleep:
                    delivered = self.make_notifier().send_message("private-message")

                self.assertFalse(delivered)
                post.assert_called_once()
                sleep.assert_not_called()

    def test_one_exhausted_target_does_not_block_later_target_success(self):
        post = Mock(
            side_effect=[
                requests.ConnectionError("first target failure"),
                requests.ConnectionError("first target failure"),
                requests.ConnectionError("first target failure"),
                TelegramResponse(200, {"ok": True, "result": {"message_id": 21}}),
            ]
        )

        with patch("orders.telegram_notifications.requests.post", post), patch(
            "time.sleep"
        ), self.assertLogs("orders.telegram_notifications", level="WARNING") as logs:
            delivered = self.make_notifier(
                "admin-one-secret,admin-two-secret"
            ).send_message(
                "private-message-secret",
                reply_markup={"private": "markup-secret"},
            )

        output = "\n".join(logs.output)
        self.assertTrue(delivered)
        self.assertEqual(post.call_count, 4)
        self.assertIn("partial_delivery", output)
        for secret in (
            "bot-token-secret",
            "admin-one-secret",
            "admin-two-secret",
            "private-message-secret",
            "markup-secret",
            "first target failure",
        ):
            self.assertNotIn(secret, output)

    def test_return_results_contains_only_successful_targets(self):
        post = Mock(
            side_effect=[
                TelegramResponse(400, {"ok": False}),
                TelegramResponse(200, {"ok": True, "result": {"message_id": 22}}),
            ]
        )

        with patch("orders.telegram_notifications.requests.post", post), self.assertLogs(
            "orders.telegram_notifications", level="WARNING"
        ):
            results = self.make_notifier("admin-one,admin-two").send_message(
                "private-message", return_results=True
            )

        self.assertEqual(results, [{"message_id": 22}])
        self.assertEqual(post.call_count, 2)

    def test_generic_post_json_remains_single_attempt(self):
        post = Mock(side_effect=requests.ConnectionError("document failure"))

        with patch("orders.telegram_notifications.requests.post", post):
            with self.assertRaises(requests.ConnectionError):
                self.make_notifier()._post_json("sendDocument", data={})

        post.assert_called_once()

    def test_document_tls_disconnect_then_success_retries(self):
        post = Mock(
            side_effect=[
                requests.exceptions.SSLError("document tls details stay private"),
                TelegramResponse(200, {"ok": True, "result": {"message_id": 24}}),
            ]
        )
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.xlsx"
            path.write_bytes(b"xlsx")
            with patch("orders.telegram_notifications.requests.post", post), patch(
                "time.sleep"
            ) as sleep, self.assertLogs("orders.telegram_notifications", level="INFO") as logs:
                delivered = self.make_notifier().send_admin_document(str(path), "report")

        self.assertTrue(delivered)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        self.assertIn("telegram_send_document retry_recovered", "\n".join(logs.output))

    def test_document_http_rejection_is_not_retried(self):
        post = Mock(return_value=TelegramResponse(400, {"ok": False, "description": "private"}))
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.xlsx"
            path.write_bytes(b"xlsx")
            with patch("orders.telegram_notifications.requests.post", post), patch(
                "time.sleep"
            ) as sleep, self.assertLogs("orders.telegram_notifications", level="WARNING"):
                delivered = self.make_notifier().send_admin_document(str(path), "report")

        self.assertFalse(delivered)
        post.assert_called_once()
        sleep.assert_not_called()

    def test_document_timeout_exhaustion_is_bounded_and_sanitized(self):
        post = Mock(side_effect=requests.Timeout("document timeout secret"))
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "private-report-name.xlsx"
            path.write_bytes(b"xlsx")
            with patch("orders.telegram_notifications.requests.post", post), patch(
                "time.sleep"
            ) as sleep, patch("builtins.print") as print_mock, self.assertLogs(
                "orders.telegram_notifications", level="WARNING"
            ) as logs:
                delivered = self.make_notifier().send_admin_document(
                    str(path), "private report caption"
                )

        output = "\n".join(logs.output)
        self.assertFalse(delivered)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        print_mock.assert_not_called()
        self.assertIn("telegram_send_document retry_exhausted", output)
        for secret in (
            "document timeout secret",
            "private-report-name.xlsx",
            "private report caption",
            "bot-token-secret",
        ):
            self.assertNotIn(secret, output)

    def test_personal_remote_disconnect_then_success_uses_shared_retry(self):
        post = Mock(
            side_effect=[
                requests.ConnectionError(
                    RemoteDisconnected("personal raw exception secret")
                ),
                TelegramResponse(200, {"ok": True, "result": {"message_id": 23}}),
            ]
        )

        with patch("orders.telegram_notifications.requests.post", post), patch(
            "time.sleep"
        ) as sleep, patch("builtins.print") as print_mock, self.assertLogs(
            "orders.telegram_notifications", level="INFO"
        ) as logs:
            delivered = self.make_notifier().send_personal_message(
                "personal-id-secret", "personal-message-secret"
            )

        output = "\n".join(logs.output)
        self.assertTrue(delivered)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        print_mock.assert_not_called()
        self.assertIn("retry_recovered", output)
        for secret in (
            "personal-id-secret",
            "bot-token-secret",
            "personal-message-secret",
            "personal raw exception secret",
            "api.telegram.org/bot",
        ):
            self.assertNotIn(secret, output)

    def test_personal_http_200_ok_false_is_not_success_or_retried(self):
        post = Mock(
            return_value=TelegramResponse(
                200, {"ok": False, "description": "personal rejection secret"}
            )
        )

        with patch("orders.telegram_notifications.requests.post", post), patch(
            "time.sleep"
        ) as sleep, patch("builtins.print") as print_mock, self.assertLogs(
            "orders.telegram_notifications", level="WARNING"
        ) as logs:
            delivered = self.make_notifier().send_personal_message(
                "personal-id-secret", "personal-message-secret"
            )

        self.assertFalse(delivered)
        post.assert_called_once()
        sleep.assert_not_called()
        print_mock.assert_not_called()
        self.assertNotIn("personal rejection secret", "\n".join(logs.output))

    def test_personal_transient_exhaustion_returns_false_after_three_attempts(self):
        post = Mock(side_effect=requests.Timeout("personal timeout secret"))

        with patch("orders.telegram_notifications.requests.post", post), patch(
            "time.sleep"
        ) as sleep, patch("builtins.print") as print_mock, self.assertLogs(
            "orders.telegram_notifications", level="WARNING"
        ) as logs:
            delivered = self.make_notifier().send_personal_message(
                "personal-id-secret", "personal-message-secret"
            )

        output = "\n".join(logs.output)
        self.assertFalse(delivered)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        print_mock.assert_not_called()
        self.assertIn("retry_exhausted", output)
        self.assertNotIn("personal timeout secret", output)

    def test_personal_missing_configuration_or_target_uses_sanitized_logs(self):
        with patch("builtins.print") as print_mock, self.assertLogs(
            "orders.telegram_notifications", level="WARNING"
        ) as logs:
            without_token = TelegramNotifier(bot_token="", admin_id="admin")
            self.assertFalse(without_token.send_personal_message("personal-id", "message"))
            self.assertFalse(self.make_notifier().send_personal_message("", "message"))

        output = "\n".join(logs.output)
        print_mock.assert_not_called()
        self.assertIn("not_configured", output)
        self.assertIn("invalid_target", output)
        self.assertNotIn("personal-id", output)
