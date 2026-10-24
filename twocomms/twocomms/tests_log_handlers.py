import logging
import threading
import time
from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from twocomms.log_handlers import (
    BOT_READINESS_MARKER, ExpectedBotReadinessFilter,
    PIIRedactionFilter, TelegramAlertHandler,
)


class ExpectedBotReadinessFilterTests(SimpleTestCase):
    def record(self):
        request = RequestFactory().get('/bot/health/', HTTP_HOST='management.twocomms.shop')
        request.urlconf = 'twocomms.urls_management'
        setattr(request, BOT_READINESS_MARKER, {
            'schema': 'ig-readiness-notification.v1', 'reason': 'customer_attention',
            'attention_count': 1,
        })
        record = logging.LogRecord('django.request', logging.ERROR, __file__, 1,
                                   'Service Unavailable: /bot/health/', (), None)
        record.request, record.status_code = request, 503
        return record

    def test_only_explicit_customer_readiness_is_filtered(self):
        self.assertFalse(ExpectedBotReadinessFilter().filter(self.record()))

    def test_unclassified_and_malformed_markers_still_alert(self):
        markers = [None, {}, {'reason': 'customer_attention'},
                   {'schema': 'other', 'reason': 'customer_attention', 'attention_count': 1},
                   {'schema': 'ig-readiness-notification.v1', 'reason': 'unknown', 'attention_count': 1}]
        valid = getattr(self.record().request, BOT_READINESS_MARKER)
        for count in (0, -1, True, '1', None):
            markers.append({**valid, 'attention_count': count})
        markers.append({**valid, 'unexpected': True})
        for marker in markers:
            with self.subTest(marker=marker):
                record = self.record()
                setattr(record.request, BOT_READINESS_MARKER, marker)
                self.assertTrue(ExpectedBotReadinessFilter().filter(record))
        record = self.record()
        delattr(record.request, BOT_READINESS_MARKER)
        self.assertTrue(ExpectedBotReadinessFilter().filter(record))

    def test_other_errors_routes_hosts_methods_and_exceptions_still_alert(self):
        changes = [
            ('record', 'status_code', 500), ('record', 'status_code', 504),
            ('record', 'name', 'other'), ('record', 'levelno', logging.CRITICAL),
            ('record', 'exc_info', (RuntimeError, RuntimeError('failure'), None)),
            ('request', 'method', 'POST'), ('request', 'path', '/bot/api/status/'),
            ('request', 'urlconf', 'twocomms.urls'),
        ]
        for target, attribute, value in changes:
            with self.subTest(attribute=attribute, value=value):
                record = self.record()
                setattr(record if target == 'record' else record.request, attribute, value)
                self.assertTrue(ExpectedBotReadinessFilter().filter(record))
        record = self.record()
        record.request.META['HTTP_HOST'] = 'twocomms.shop'
        self.assertTrue(ExpectedBotReadinessFilter().filter(record))
        record.request.get_host = mock.Mock(side_effect=RuntimeError('metadata unavailable'))
        self.assertTrue(ExpectedBotReadinessFilter().filter(record))
        record.request = None
        self.assertTrue(ExpectedBotReadinessFilter().filter(record))

    def test_filter_is_configured_only_on_telegram_and_preserves_durable_handler(self):
        from twocomms import settings as project_settings

        config = project_settings.LOGGING
        self.assertEqual(config['filters']['expected_bot_readiness']['()'],
                         'twocomms.log_handlers.ExpectedBotReadinessFilter')
        owners = [name for name, handler in config['handlers'].items()
                  if 'expected_bot_readiness' in handler.get('filters', [])]
        self.assertEqual(owners, ['telegram_alert'])
        self.assertEqual(config['loggers']['django.request']['handlers'],
                         ['app_error_file', 'telegram_alert'])


class PIIRedactionFilterTests(SimpleTestCase):
    def test_masks_email_phone_and_long_numbers(self):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="client john@example.com phone +380991112233 card 4111111111111111",
            args=(),
            exc_info=None,
        )

        PIIRedactionFilter().filter(record)

        self.assertEqual(
            record.getMessage(),
            "client [email] phone [phone] card [number]",
        )

    def test_masks_instagram_webhook_verify_token_in_access_path(self):
        record = logging.LogRecord(
            name="django.server",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='"GET /bot/webhook/?hub.mode=subscribe&hub.verify_token=secret-value&hub.challenge=123 HTTP/1.1" 403 9',
            args=(),
            exc_info=None,
        )

        PIIRedactionFilter().filter(record)

        self.assertNotIn("secret-value", record.getMessage())
        self.assertIn("hub.verify_token=[redacted]", record.getMessage())
class TelegramAlertHandlerTests(SimpleTestCase):
    def wait_for_fallback(self, fallback):
        self.assertTrue(fallback.wait(1), "expected the emergency fallback")

    @mock.patch("twocomms.log_handlers.TELEGRAM_ALERT_TIMEOUT_SECONDS", 0.01)
    @mock.patch("twocomms.log_handlers.sys.stderr")
    @mock.patch("twocomms.log_handlers.requests.post")
    @mock.patch("orders.telegram_notifications.TelegramNotifier")
    def test_failed_delivery_writes_direct_stderr_fallback(
        self, notifier_class, post, stderr
    ):
        fallback = threading.Event()
        stderr.write.side_effect = lambda message: fallback.set()
        notifier = notifier_class.return_value
        notifier.is_configured.return_value = True
        notifier._resolve_targets.return_value = ["admin-chat"]
        post.return_value.ok = False

        TelegramAlertHandler._send_async("server exploded")

        self.wait_for_fallback(fallback)
        self.assertIn("server exploded", stderr.write.call_args.args[0])
        self.assertIn("failed", stderr.write.call_args.args[0])
        notifier.send_message.assert_not_called()
        post.assert_called_once()

    @mock.patch("twocomms.log_handlers.TELEGRAM_ALERT_TIMEOUT_SECONDS", 0.01)
    @mock.patch("twocomms.log_handlers.sys.stderr")
    @mock.patch("twocomms.log_handlers.requests.post")
    @mock.patch("orders.telegram_notifications.TelegramNotifier")
    def test_hung_delivery_is_bounded_and_falls_back(
        self, notifier_class, post, stderr
    ):
        fallback = threading.Event()
        post_started = threading.Event()
        request_finished = threading.Event()
        release = threading.Event()
        stderr.write.side_effect = lambda message: fallback.set()
        notifier = notifier_class.return_value
        notifier.is_configured.return_value = True
        notifier._resolve_targets.return_value = ["admin-chat"]

        def stalled_request(*args, **kwargs):
            post_started.set()
            release.wait(1)
            request_finished.set()
            return mock.Mock(ok=False)

        post.side_effect = stalled_request

        started = time.monotonic()
        TelegramAlertHandler._send_async("transport stalled")
        self.assertTrue(post_started.wait(1), "emergency transport did not start")
        self.wait_for_fallback(fallback)
        elapsed = time.monotonic() - started
        release.set()
        self.assertTrue(request_finished.wait(1), "stalled request did not finish")

        self.assertLess(elapsed, 0.5)
        self.assertIn("transport stalled", stderr.write.call_args.args[0])
        self.assertIn("timeout", stderr.write.call_args.args[0])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["timeout"], 0.01)
        notifier.send_message.assert_not_called()

    @mock.patch("twocomms.log_handlers.requests.post")
    @mock.patch("orders.telegram_notifications.TelegramNotifier")
    def test_notifier_logging_does_not_reenter_alert_handler(self, notifier_class, post):
        notifier = notifier_class.return_value
        notifier.is_configured.return_value = True
        notifier._resolve_targets.return_value = ["admin-chat"]
        nested_logger = logging.getLogger("telegram-alert-recursion-test")
        nested_logger.propagate = False
        handler = TelegramAlertHandler()
        nested_logger.addHandler(handler)
        try:
            done = threading.Event()

            def send_request(*_args, **_kwargs):
                nested_logger.error("notifier transport failed")
                done.set()
                response = mock.Mock()
                response.ok = True
                return response

            post.side_effect = send_request
            TelegramAlertHandler._send_async("outer alert")

            self.assertTrue(done.wait(1), "emergency transport worker did not run")
            self.assertEqual(post.call_count, 1)
            notifier.send_message.assert_not_called()
        finally:
            nested_logger.removeHandler(handler)

    @mock.patch("twocomms.log_handlers.requests.post")
    @mock.patch("orders.telegram_notifications.TelegramNotifier")
    def test_emergency_transport_uses_one_target_and_bounded_timeout(
        self, notifier_class, post
    ):
        notifier = notifier_class.return_value
        notifier.is_configured.return_value = True
        notifier._resolve_targets.return_value = ["first-admin", "second-admin"]
        post.return_value.ok = True

        TelegramAlertHandler._send_async("one bounded request")

        deadline = time.monotonic() + 1
        while post.call_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["timeout"], 2.0)
        self.assertEqual(post.call_args.kwargs["data"]["chat_id"], "first-admin")
        notifier.send_message.assert_not_called()
