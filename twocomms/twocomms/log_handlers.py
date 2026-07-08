"""
W3-2 (TD-022/TECH-041): Telegram-алерты для серверных ошибок.

Раньше django.request ERROR писался только в stderr.log «в никуда» —
о 500-ках узнавали от покупателей. Этот handler шлёт краткий алерт
админу в Telegram с жёстким rate-limit (антифлуд при каскадных сбоях).

Дизайн-решения:
- Rate-limit через Django cache: не больше MAX_ALERTS_PER_WINDOW за
  WINDOW_SECONDS; при превышении шлётся один «suppressed N» алерт.
- Отправка в daemon-потоке — logging не должен блокировать request.
- Любая ошибка внутри handler'а глотается (иначе рекурсия
  logging → error → logging).
- Текст обрезается: Telegram лимит 4096, нам хватает 1000.
"""

import logging
import re
import threading

WINDOW_SECONDS = 600
MAX_ALERTS_PER_WINDOW = 5
EMAIL_RE = re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b')
PHONE_RE = re.compile(
    r'(?<![\w])(?:\+?380|0)[\s-]?\(?\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)'
)
LONG_NUMBER_RE = re.compile(r'(?<!\d)\d{12,19}(?!\d)')


def redact_pii(text):
    """Mask common PII shapes before they reach persistent logs."""
    if not text:
        return text
    text = EMAIL_RE.sub('[email]', str(text))
    text = PHONE_RE.sub('[phone]', text)
    text = LONG_NUMBER_RE.sub('[number]', text)
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
    """Шлёт ERROR+ записи админу в Telegram (rate-limited, non-blocking)."""

    def emit(self, record):
        try:
            self._emit_inner(record)
        except Exception:
            # Никогда не даём handler'у уронить логирование.
            pass

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
    def _send_async(text):
        def _worker():
            try:
                from orders.telegram_notifications import TelegramNotifier
                notifier = TelegramNotifier()
                if notifier.is_configured():
                    notifier.send_message(text)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()
