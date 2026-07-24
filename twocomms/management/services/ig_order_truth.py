"""Durable clock for order fields that affect Instagram CRM analysis."""

from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone

from management.ig_bot_models import IgDeal
from orders.models import Order


ORDER_TRUTH_FIELDS = frozenset({
    "status",
    "payment_status",
    "tracking_number",
    "shipment_status",
})
DEAL_ORDER_TRUTH_FIELDS = frozenset({"order", "order_id", "shipped_notified_at"})


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


@receiver(pre_save, sender=IgDeal, dispatch_uid="ig_capture_deal_order_truth_change")
def capture_deal_order_truth_change(sender, instance, update_fields=None, **kwargs):
    """Remember deal-side link/shipment edits, including narrow update_fields saves."""
    instance._ig_deal_order_truth_changed = False
    fields = DEAL_ORDER_TRUTH_FIELDS
    if update_fields is not None:
        fields = {
            "order_id" if field == "order" else field
            for field in update_fields
        }.intersection(fields)
    if not fields or not instance.pk:
        instance._ig_deal_order_truth_changed = bool(
            fields and (instance.order_id or instance.shipped_notified_at)
        )
        return
    previous = sender.objects.filter(pk=instance.pk).values(
        "order_id", "shipped_notified_at"
    ).first()
    if previous is None:
        return
    instance._ig_deal_order_truth_changed = any(
        previous.get(field) != getattr(instance, field, None)
        for field in fields
    )


@receiver(post_save, sender=IgDeal, dispatch_uid="ig_publish_deal_order_truth_change")
def publish_deal_order_truth_change(sender, instance, **kwargs):
    if not getattr(instance, "_ig_deal_order_truth_changed", False):
        return
    changed_at = timezone.now()
    sender.objects.filter(pk=instance.pk).update(order_truth_updated_at=changed_at)
    instance.order_truth_updated_at = changed_at
