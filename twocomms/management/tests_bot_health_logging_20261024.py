"""Readiness debt remains 503 without entering the generic Telegram channel."""
import io
import json
import logging
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import patch

from django.db import DatabaseError
from django.http import HttpResponseServerError
from django.test import RequestFactory, SimpleTestCase
from django.utils.log import log_response

from management.bot_views import bot_health
from twocomms.log_handlers import (
    BOT_READINESS_MARKER, ExpectedBotReadinessFilter,
    PIIRedactionFilter, TelegramAlertHandler,
)


class BotHealthLoggingTests(SimpleTestCase):
    def setUp(self):
        self.tasks = {'available': True, 'healthy': True, 'unhealthy_count': 0}
        self.queues = {'available': True, 'revision_execution_overdue': 0}
        lanes = {name: {'available': True, 'healthy': True} for name in (
            'customer_revisions', 'legacy_inbound', 'legacy_outbound', 'revision_delivery',
            'manager_notifications', 'conversation_analysis', 'analysis_materialization',
            'reply_recovery', 'binotel_analysis', 'typed_memory', 'trace_refresh',
        )}
        lanes['customer_revisions'].update(
            healthy=False, state='attention', risk_coverage_complete=True,
            risk_scan_complete=True, attention_total_exact=True, attention_total=1,
            counts={'failed': 0, 'unknown': 0, 'attention': 1, 'manual': 1},
        )
        self.operations = {
            'available': True, 'healthy': False, 'bot_state': 'running', 'lanes': lanes,
            'consumer': {'process_online': True, 'main_healthy': True, 'stalled_reason': ''},
            'worker_lanes': {'analysis': {'healthy': True, 'state': 'idle'}},
        }
        self.request = RequestFactory().get('/bot/health/', HTTP_HOST='management.twocomms.shop')
        self.request.urlconf = 'twocomms.urls_management'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('requests.sessions.Session.request',
                                      side_effect=AssertionError('health must not call a provider')))
        self.stack.enter_context(patch('management.services.instagram_bot.notify_manager',
                                      side_effect=AssertionError('health must not create notifications')))
        self.task_snapshot = self.stack.enter_context(patch(
            'management.services.ig_task_health.task_health_snapshot', return_value=self.tasks))
        self.stack.enter_context(patch(
            'management.services.ig_task_health.release_queue_snapshot', return_value=self.queues))
        self.stack.enter_context(patch(
            'management.services.ig_lane_health.operational_lane_snapshot', return_value=self.operations))

    def log_response_through_handlers(self, response, *, exception=None):
        """Use Django's native response logger with both configured handler roles."""
        stream = io.StringIO()
        durable = logging.StreamHandler(stream)
        durable.addFilter(PIIRedactionFilter())
        telegram = TelegramAlertHandler()
        telegram.addFilter(PIIRedactionFilter())
        telegram.addFilter(ExpectedBotReadinessFilter())
        logger = logging.Logger('django.request', logging.ERROR)
        logger.addHandler(durable)
        logger.addHandler(telegram)
        with (patch.object(TelegramAlertHandler, '_send_async') as send,
              patch('django.core.cache.cache') as alert_cache):
            alert_cache.get.return_value = 0
            log_response('%s: %s', response.reason_phrase, self.request.path,
                         response=response, request=self.request, logger=logger, exception=exception)
        return stream.getvalue(), send

    def test_customer_attention_keeps_json_503_and_durable_log_without_telegram(self):
        original = deepcopy((self.tasks, self.queues, self.operations))
        response = bot_health(self.request)
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.content)
        self.assertEqual(body['status'], 'degraded')
        self.assertEqual(body['operations'], self.operations)
        self.assertEqual(body['queues'], self.queues)
        self.assertEqual(body['cron_unhealthy'], 0)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.assertEqual(original, (self.tasks, self.queues, self.operations))
        self.assertEqual(getattr(self.request, BOT_READINESS_MARKER)['attention_count'], 1)
        self.assertNotIn(BOT_READINESS_MARKER, response.content.decode())
        durable, send = self.log_response_through_handlers(response)
        self.assertIn('Service Unavailable: /bot/health/', durable)
        send.assert_not_called()

    def test_unknown_or_infrastructure_degradation_keeps_server_error_alert(self):
        changes = [
            ('tasks', ('available',), False), ('tasks', ('healthy',), False),
            ('tasks', ('unhealthy_count',), 1), ('queues', ('available',), False),
            ('tasks', ('unhealthy_count',), False),
            ('queues', ('revision_execution_overdue',), 1),
            ('queues', ('revision_execution_overdue',), False),
            ('operations', ('available',), False),
            ('operations', ('bot_state',), 'worker_stalled'),
            ('operations', ('consumer', 'main_healthy'), False),
            ('operations', ('consumer', 'process_online'), False),
            ('operations', ('consumer', 'stalled_reason'), 'main_progress_error'),
            ('operations', ('worker_lanes',), {}),
            ('operations', ('worker_lanes', 'analysis', 'healthy'), False),
            ('operations', ('lanes', 'legacy_inbound', 'healthy'), False),
            ('operations', ('lanes', 'legacy_inbound', 'available'), False),
        ]
        for field, value in (
            ('state', 'stalled'), ('risk_coverage_complete', False), ('risk_scan_complete', False),
            ('attention_total_exact', False), ('attention_total', 0), ('attention_total', True),
            ('attention_total', '1'), ('available', False),
        ):
            changes.append(('operations', ('lanes', 'customer_revisions', field), value))
        for field in ('failed', 'unknown'):
            changes.append(('operations', ('lanes', 'customer_revisions', 'counts', field), 1))
            changes.append(('operations', ('lanes', 'customer_revisions', 'counts', field), False))
        originals = deepcopy((self.tasks, self.queues, self.operations))
        for owner, path, value in changes:
            with self.subTest(owner=owner, path=path, value=value):
                for target, original in zip((self.tasks, self.queues, self.operations), originals):
                    target.clear()
                    target.update(deepcopy(original))
                target = getattr(self, owner)
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                response = bot_health(self.request)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(hasattr(self.request, BOT_READINESS_MARKER))
                durable, send = self.log_response_through_handlers(response)
                self.assertIn('Service Unavailable', durable)
                send.assert_called_once()
                self.assertIn('SERVER ERROR', send.call_args.args[0])

    def test_missing_or_unknown_lane_preserves_alert(self):
        for replace in (False, True):
            with self.subTest(unknown_lane=replace):
                lanes = self.operations['lanes']
                saved = lanes.pop('trace_refresh')
                if replace:
                    lanes['unknown_lane'] = saved
                response = bot_health(self.request)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(hasattr(self.request, BOT_READINESS_MARKER))
                if replace:
                    del lanes['unknown_lane']
                lanes['trace_refresh'] = saved

    def test_request_headers_cannot_classify_unavailable_observation(self):
        self.request.META['HTTP_X_TWC_BOT_READINESS_NOTIFICATION'] = 'customer_attention'
        self.tasks['available'] = False
        response = bot_health(self.request)
        self.assertFalse(hasattr(self.request, BOT_READINESS_MARKER))
        self.assertEqual(response.status_code, 503)

    def test_snapshot_exception_is_unclassified_and_real_500_still_alerts(self):
        self.task_snapshot.side_effect = DatabaseError('observation unavailable')
        with self.assertRaises(DatabaseError) as failure:
            bot_health(self.request)
        self.assertFalse(hasattr(self.request, BOT_READINESS_MARKER))
        durable, send = self.log_response_through_handlers(
            HttpResponseServerError(), exception=failure.exception)
        self.assertIn('Internal Server Error', durable)
        send.assert_called_once()

    def test_healthy_probe_does_not_classify_or_log_error(self):
        self.operations['healthy'] = True
        response = bot_health(self.request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['status'], 'ok')
        self.assertFalse(hasattr(self.request, BOT_READINESS_MARKER))
