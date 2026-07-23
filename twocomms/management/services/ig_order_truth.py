"""Durable clock for order fields that affect Instagram CRM analysis."""

from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from orders.models import Order


ORDER_TRUTH_FIELDS = frozenset({
    "status",
    "payment_status",
    "tracking_number",
    "shipment_status",
})


def order_truth_changed(previous, current, *, update_fields=None) -> bool:
    fields = ORDER_TRUTH_FIELDS
    if update_fields is not None:
        fields = fields.intersection(update_fields)
    return bool(fields) and any(
        previous.get(field) != getattr(current, field, None)
        for field in fields
    )


@receiver(pre_save, sender=Order, dispatch_uid="ig_capture_order_truth_change")
def capture_order_truth_change(sender, instance, update_fields=None, **kwargs):
    instance._ig_order_truth_changed = False
    if not instance.pk:
        return
    fields = ORDER_TRUTH_FIELDS
    if update_fields is not None:
        fields = fields.intersection(update_fields)
    if not fields:
        return
    previous = sender.objects.filter(pk=instance.pk).values(*fields).first()
    if previous is not None:
        instance._ig_order_truth_changed = order_truth_changed(
            previous,
            instance,
            update_fields=fields,
        )


@receiver(post_save, sender=Order, dispatch_uid="ig_publish_order_truth_change")
def publish_order_truth_change(sender, instance, **kwargs):
    if not getattr(instance, "_ig_order_truth_changed", False):
        return
    from management.models import IgDeal

    IgDeal.objects.filter(order_id=instance.pk).update(
        order_truth_updated_at=timezone.now()
    )


@receiver(pre_delete, sender=Order, dispatch_uid="ig_publish_order_truth_unlink")
def publish_order_truth_unlink(sender, instance, **kwargs):
    from management.models import IgDeal

    IgDeal.objects.filter(order_id=instance.pk).update(
        order_truth_updated_at=timezone.now()
    )
