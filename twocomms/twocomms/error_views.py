"""
W3-2 (TECH-041): handler500 с инцидент-кодом.

Раньше handler500 не был определён — Django отдавал голый стандартный
500 без какой-либо связи с логами. Теперь каждый 500 получает короткий
инцидент-код, который: (а) показывается пользователю («сообщите код
поддержке»), (б) пишется в stderr.log и уходит в Telegram-алерт
(через logger django.request → TelegramAlertHandler) — админ может
сматчить жалобу покупателя с конкретным трейсбеком.

Шаблон 500.html — STANDALONE (не extends base.html): если упал сам
base.html/контекст-процессор, наследование зациклит ошибку.
"""

import logging
import uuid

from django.http import HttpResponseServerError
from django.template import loader

logger = logging.getLogger('django.request')


def server_error(request):
    incident_code = uuid.uuid4().hex[:8].upper()
    # ERROR уже залогирован Django'м с трейсбеком; добавляем маппинг
    # инцидент-кода на path, чтобы код из жалобы вёл к трейсбеку рядом.
    logger.error(
        'Incident %s: 500 at %s', incident_code, request.path,
        extra={'incident_code': incident_code},
    )
    try:
        template = loader.get_template('500.html')
        body = template.render({'incident_code': incident_code}, request)
    except Exception:
        body = (
            '<h1>Помилка сервера</h1>'
            f'<p>Код інциденту: {incident_code}</p>'
        )
    response = HttpResponseServerError(body)
    response['Cache-Control'] = 'no-store'
    return response
