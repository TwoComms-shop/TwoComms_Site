"""
Сигналы для уведомлений о заказах
"""
import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone
from django.db.models.signals import post_save, pre_save
from django.db import transaction
from django.dispatch import receiver
from .models import Order, WholesaleInvoice

logger = logging.getLogger(__name__)


def _safe_queue_notification(order_id, notification_type, **kwargs):
    """Persist notification intent in the caller's transaction."""
    from .payment_side_effects import enqueue_order_telegram_notification

    return enqueue_order_telegram_notification(
        order_id,
        notification_type,
        **kwargs,
    )


# Отключен автоматический сигнал - уведомления отправляются вручную в views
# @receiver(post_save, sender=Order)
# def send_new_order_notification(sender, instance, created, **kwargs):
#     """Отправляет уведомление при создании нового заказа"""
#     if created:
#         # Отправляем уведомление о новом заказе
#         telegram_notifier.send_new_order_notification(instance)


@receiver(pre_save, sender=Order)
def track_order_changes(sender, instance, **kwargs):
    """Отслеживает изменения в заказе"""
    if instance.pk:
        try:
            old_instance = Order.objects.get(pk=instance.pk)
            transition_version = old_instance.updated.isoformat()
            update_fields = kwargs.get('update_fields')
            status_will_save = update_fields is None or 'status' in update_fields
            tracking_will_save = (
                update_fields is None or 'tracking_number' in update_fields
            )

            # Отслеживаем изменение статуса заказа
            from management.services.ig_assisted_partial_shipping import assisted_shipping_dispatch_ready
            shipping_ready = None
            if (status_will_save and old_instance.status != instance.status
                    and instance.status in {'ship', 'done'}):
                shipping_ready = assisted_shipping_dispatch_ready(instance)
            if (status_will_save and old_instance.status != instance.status
                    and shipping_ready is not False):
                _safe_queue_notification(
                    instance.id,
                    'status_update',
                    transition_version=transition_version,
                    old_status=old_instance.get_status_display(),
                    new_status=instance.get_status_display()
                )

            # Отслеживаем добавление ТТН
            if (
                tracking_will_save
                and not old_instance.tracking_number
                and instance.tracking_number
                and (shipping_ready is True or assisted_shipping_dispatch_ready(instance))
            ):
                _safe_queue_notification(
                    instance.id,
                    'ttn_added',
                    transition_version=transition_version,
                )

        except Order.DoesNotExist:
            pass

    if instance.pk:
        try:
            previous_tracking = (Order.objects.only('tracking_number').get(pk=instance.pk).tracking_number or '').strip()
        except Order.DoesNotExist:
            previous_tracking = ''
        instance._ig_ttn_transition = bool(
            (instance.tracking_number or '').strip() and not previous_tracking
        )
    else:
        instance._ig_ttn_transition = bool((instance.tracking_number or '').strip())


@receiver(post_save, sender=Order)
def project_instagram_tracking_transition(sender, instance, created, **kwargs):
    """Project every first non-empty TTN save, not only the admin helper path."""
    if not getattr(instance, '_ig_ttn_transition', False):
        return
    order_id = instance.pk

    def _emit():
        try:
            from management.ig_bot_models import IgLifecycleEvent
            from management.services.ig_lifecycle import dispatch_lifecycle_event, ensure_lifecycle_event

            order = Order.objects.get(pk=order_id)
            event, _created = ensure_lifecycle_event(
                order,
                IgLifecycleEvent.Kind.TTN_CREATED,
                payload={
                    'tracking_number': (order.tracking_number or '').strip(),
                    'order_number': order.order_number,
                },
            )
            if event is not None:
                dispatch_lifecycle_event(event.pk)
        except Exception:
            logger.exception('Unable to project Instagram TTN transition for order %s', order_id)

    transaction.on_commit(_emit)
