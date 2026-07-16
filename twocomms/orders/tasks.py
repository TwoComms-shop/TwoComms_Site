"""Задачи уведомлений о заказах.

Раньше модуль объявлял Celery-таски, но на хостинге нет воркера и брокера,
поэтому каждый ``.delay()`` бился в недоступный Redis и замедлял оформление
заказа. Теперь отправка Telegram-уведомлений выполняется в фоновом daemon-потоке:
HTTP-ответ пользователю не ждёт Telegram API. Для обратной совместимости
функция сохраняет интерфейс ``.delay()`` / ``.apply_async()``.
"""

import logging
from threading import Thread

from django.db import close_old_connections

logger = logging.getLogger(__name__)


def _send_notification(order_id, notification_type, **kwargs):
    """Синхронная отправка уведомления. Выполняется в фоновом потоке."""
    # Поток может получить устаревшее соединение с MySQL ("server has gone
    # away"), поэтому закрываем старые соединения до и после работы.
    close_old_connections()
    try:
        # Импорты внутри функции, чтобы модуль был безопасен при миграциях.
        from .models import Order
        from .telegram_notifications import telegram_notifier

        order = (
            Order.objects.filter(id=order_id)
            .select_related('user__userprofile')
            .first()
        )
        if order is None:
            logger.warning("Order %s not found for Telegram notification", order_id)
            return

        delivered = False
        if notification_type == 'new_order':
            delivered = telegram_notifier.send_new_order_notification(order)
        elif notification_type == 'status_update':
            old_status = kwargs.get('old_status')
            new_status = kwargs.get('new_status')
            if old_status and new_status:
                delivered = telegram_notifier.send_order_status_update(order, old_status, new_status)
        elif notification_type == 'ttn_added':
            delivered = telegram_notifier.send_ttn_added_notification(order)

        if delivered:
            logger.info(
                "Telegram notification '%s' sent for order %s", notification_type, order_id
            )
        else:
            logger.warning(
                "Telegram notification '%s' delivery failed for order %s",
                notification_type,
                order_id,
            )
    except Exception:
        logger.exception(
            "Error sending Telegram notification '%s' for order %s",
            notification_type,
            order_id,
        )
    finally:
        close_old_connections()


def send_telegram_notification_task(order_id, notification_type, **kwargs):
    """Запускает отправку уведомления в фоне, не блокируя запрос.

    notification_type: 'new_order', 'status_update', 'ttn_added'
    """
    try:
        Thread(
            target=_send_notification,
            args=(order_id, notification_type),
            kwargs=kwargs,
            daemon=True,
            name=f"tg-notify-{notification_type}-{order_id}",
        ).start()
    except Exception:
        # Если поток создать не удалось (исчерпаны ресурсы) — шлём синхронно,
        # уведомление важнее задержки.
        logger.exception("Failed to spawn notification thread; sending inline")
        _send_notification(order_id, notification_type, **kwargs)


# Совместимость со старыми вызовами Celery-стиля.
send_telegram_notification_task.delay = send_telegram_notification_task
send_telegram_notification_task.apply_async = (
    lambda args=None, kwargs=None, **_kw: send_telegram_notification_task(
        *(args or ()), **(kwargs or {})
    )
)
