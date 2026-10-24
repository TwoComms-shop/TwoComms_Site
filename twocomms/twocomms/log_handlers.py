"""
W3-2 (TD-022/TECH-041): Telegram-алерты для серверных ошибок.

Раньше django.request ERROR писался только в stderr.log «в никуда» —
о 500-ках узнавали от покупателей. Этот handler шлёт краткий алерт
админу в Telegram с жёстким rate-limit (антифлуд при каскадных сбоях).

Дизайн-решения:
- Rate-limit через Django cache: не больше MAX_ALERTS_PER_WINDOW за
  WINDOW_SECONDS; при превышении шлётся один «suppressed N» алерт.
- Отправка в daemon-потоке — logging не должен блокировать request.
- Emergency transport is one direct Bot API request to one target with a
  two-second cap; it intentionally bypasses the normal retrying notifier flow.
- Любая ошибка внутри handler'а глотается (иначе рекурсия
  logging → error → logging).
- Текст обрезается: Telegram лимит 4096, нам хватает 1000.
"""

import logging
import re
import sys
import threading

import requests

WINDOW_SECONDS = 600
MAX_ALERTS_PER_WINDOW = 5
# The alert is deliberately best-effort: requests must not wait for Telegram,
# and a direct stderr line remains available when the transport is unavailable.
TELEGRAM_ALERT_TIMEOUT_SECONDS = 2.0
EMAIL_RE = re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b')
PHONE_RE = re.compile(
    r'(?<![\w])(?:\+?380|0)[\s-]?\(?\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)'
)
LONG_NUMBER_RE = re.compile(r'(?<!\d)\d{12,19}(?!\d)')
WEBHOOK_VERIFY_TOKEN_RE = re.compile(
    r'(?i)([?&]hub\.verify_token=)[^&\s"\']+'
)
BOT_READINESS_MARKER = '_twc_bot_readiness_notification'
BOT_READINESS_LANES = frozenset({
    'customer_revisions', 'legacy_inbound', 'legacy_outbound', 'revision_delivery',
    'manager_notifications', 'conversation_analysis', 'analysis_materialization',
    'reply_recovery', 'binotel_analysis', 'typed_memory', 'trace_refresh',
})


def _is_management_bot_health_request(request):
    try:
        return bool(
            request.method == 'GET' and request.path == '/bot/health/'
            and request.urlconf == 'twocomms.urls_management'
            and request.get_host().split(':', 1)[0].lower() == 'management.twocomms.shop'
        )
    except Exception:
        # A logging filter must fail open if request metadata is unavailable.
        return False


def _zero_readiness_count(value):
    return type(value) is int and value == 0


def mark_expected_bot_readiness(request, *, tasks, queues, operations):
    """Classify only fully observed customer debt; never suppress unknown failures.

    Customer debt already has a source-bound management notification. Probing
    readiness must neither send that notification again nor call a provider.
    """
    if not _is_management_bot_health_request(request):
        return
    lanes = operations.get('lanes')
    consumer = operations.get('consumer')
    workers = operations.get('worker_lanes')
    if not (
        tasks.get('available') is True and tasks.get('healthy') is True
        and _zero_readiness_count(tasks.get('unhealthy_count'))
        and queues.get('available') is True and _zero_readiness_count(queues.get('revision_execution_overdue'))
        and operations.get('available') is True and operations.get('healthy') is False
        and operations.get('bot_state') == 'running'
        and isinstance(consumer, dict) and consumer.get('process_online') is True
        and consumer.get('main_healthy') is True and consumer.get('stalled_reason') == ''
        and isinstance(workers, dict) and workers
        and all(isinstance(row, dict) and row.get('healthy') is True for row in workers.values())
        and isinstance(lanes, dict) and set(lanes) == BOT_READINESS_LANES
        and all(isinstance(row, dict) and row.get('available') is True
                and (name == 'customer_revisions' or row.get('healthy') is True)
                for name, row in lanes.items())
    ):
        return
    debt = lanes['customer_revisions']
    count = debt.get('attention_total')
    counts = debt.get('counts')
    if not (
        debt.get('healthy') is False and debt.get('state') == 'attention'
        and debt.get('risk_coverage_complete') is True and debt.get('risk_scan_complete') is True
        and debt.get('attention_total_exact') is True and type(count) is int and count > 0
        and isinstance(counts, dict) and _zero_readiness_count(counts.get('failed'))
        and _zero_readiness_count(counts.get('unknown'))
    ):
        return
    setattr(request, BOT_READINESS_MARKER, {
        'schema': 'ig-readiness-notification.v1', 'reason': 'customer_attention',
        'attention_count': count,
    })


class ExpectedBotReadinessFilter(logging.Filter):
    """Telegram-only exclusion for an explicitly classified readiness response."""

    def filter(self, record):
        request = getattr(record, 'request', None)
        marker = getattr(request, BOT_READINESS_MARKER, None)
        expected = (
            record.name == 'django.request' and record.levelno == logging.ERROR
            and getattr(record, 'status_code', None) == 503 and not record.exc_info
            and _is_management_bot_health_request(request)
            and isinstance(marker, dict)
            and set(marker) == {'schema', 'reason', 'attention_count'}
            and marker.get('schema') == 'ig-readiness-notification.v1'
            and marker.get('reason') == 'customer_attention'
            and type(marker.get('attention_count')) is int and marker['attention_count'] > 0
        )
        return not expected


def redact_pii(text):
    """Mask common PII shapes before they reach persistent logs."""
    if not text:
        return text
    text = EMAIL_RE.sub('[email]', str(text))
    text = PHONE_RE.sub('[phone]', text)
    text = LONG_NUMBER_RE.sub('[number]', text)
    text = WEBHOOK_VERIFY_TOKEN_RE.sub(r'\1[redacted]', text)
    return text


class PIIRedactionFilter(logging.Filter):
    """Redact email/phone/long numeric identifiers from log records."""

    def filter(self, record):
        try:
            record.msg = redact_pii(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


class TelegramAlertHandler(logging.Handler):
    """Send ERROR+ records through an independent, bounded emergency channel."""

    _recursion_state = threading.local()

    def emit(self, record):
        if getattr(self._recursion_state, 'active', False):
            return
        self._recursion_state.active = True
        try:
            self._emit_inner(record)
        except Exception:
            # Никогда не даём handler'у уронить логирование.
            pass
        finally:
            self._recursion_state.active = False

    def _emit_inner(self, record):
        from django.core.cache import cache

        # --- rate limit ---
        key = 'tg_error_alert:window'
        try:
            count = cache.get(key, 0)
            if count >= MAX_ALERTS_PER_WINDOW:
                # Один раз за окно сообщаем о подавлении.
                if count == MAX_ALERTS_PER_WINDOW:
                    cache.set(key, count + 1, WINDOW_SECONDS)
                    self._send_async(
                        '\u26a0\ufe0f Error alerts rate-limited: '
                        'больше {}/10 мин. Смотри stderr.log.'.format(MAX_ALERTS_PER_WINDOW)
                    )
                return
            cache.set(key, count + 1, WINDOW_SECONDS)
        except Exception:
            # Кэш недоступен → шлём без лимита (лучше алерт, чем тишина).
            pass

        message = self.format(record)
        if len(message) > 1000:
            message = message[:1000] + '\u2026'
        incident = getattr(record, 'incident_code', None)
        prefix = '\U0001f6a8 SERVER ERROR'
        if incident:
            prefix += f' [{incident}]'
        self._send_async(f'{prefix}\n{message}')

    @staticmethod
    def _write_fallback(text, reason):
        """Write a bounded emergency trace without going through logging."""
        try:
            sys.stderr.write(
                'Telegram alert fallback ({0}): {1}\n'.format(reason, text[:1000])
            )
            sys.stderr.flush()
        except Exception:
            pass

    @classmethod
    def _send_async(cls, text):
        state = {'success': False, 'fallback': False}
        state_lock = threading.Lock()
        completed = threading.Event()

        def fallback(reason):
            with state_lock:
                if state['success'] or state['fallback']:
                    return
                state['fallback'] = True
            cls._write_fallback(text, reason)

        def _worker():
            cls._recursion_state.active = True
            try:
                if cls._send_emergency_alert(text):
                    with state_lock:
                        state['success'] = True
                else:
                    fallback('failed')
            except Exception:
                fallback('exception')
            finally:
                completed.set()
                cls._recursion_state.active = False

        def _deadline_watcher():
            if not completed.wait(TELEGRAM_ALERT_TIMEOUT_SECONDS):
                fallback('timeout')

        try:
            threading.Thread(target=_worker, daemon=True).start()
            threading.Thread(target=_deadline_watcher, daemon=True).start()
        except Exception:
            fallback('thread_start')

    @staticmethod
    def _send_emergency_alert(text):
        """Send one bounded Bot API request without entering the normal notifier flow."""
        from orders.telegram_notifications import TelegramNotifier

        notifier = TelegramNotifier()
        if not notifier.is_configured():
            return False
        targets = notifier._resolve_targets(admin=True)
        if not targets:
            return False

        response = requests.post(
            f'https://api.telegram.org/bot{notifier.bot_token}/sendMessage',
            data={'chat_id': targets[0], 'text': text},
            timeout=TELEGRAM_ALERT_TIMEOUT_SECONDS,
        )
        return bool(response.ok)
