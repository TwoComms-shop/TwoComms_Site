"""Bind an actual paid winner to its one exact native shipping agreement."""
import logging
from datetime import timedelta
import uuid

from management.services import ig_fulfillment_reviews as reviews

logger = logging.getLogger(__name__)


def bind_paid_shipping_order(order_id, *, now=None):
    from django.utils import timezone
    from management.models import IgSplitShippingChoice, IgSplitOrderPlan
    from orders.models import Order
    from management.services.ig_split_execution import (_choice_frame, _order_facts,
        _allocations, bind_split_plan, SplitExecutionError)
    if not reviews._enabled():
        return {"ready": False, "reason": "disabled"}
    now = now or timezone.now()
    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return {"ready": False, "reason": "order_missing"}
    existing = IgSplitOrderPlan.objects.filter(order_id=order.pk).first()
    if existing:
        from management.services.ig_split_execution import verified_tracking_plan
        checked = verified_tracking_plan(order, now=now)
        return {"ready": checked.get("ready") is True, "reason": checked.get("reason", ""),
            "created": False, "plan_id": str(existing.pk)}
    data = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    client_id = ((data.get("source_cart_provenance") or {}).get("owner") or {}).get("client_id")
    if type(client_id) is not int:
        return {"ready": False, "reason": "shipping_order_source_unknown"}
    # Old abandoned agreements are historical evidence, not candidates for a
    # new initial attachment. Apply the same finite freshness boundary before
    # the capture bound, so history cannot starve a new paid purchase.
    choices = IgSplitShippingChoice.objects.filter(client_id=client_id, plan__isnull=True,
        created_at__lte=now, offer__decision__created_at__gte=now - reviews.FRESHNESS,
        offer__decision__created_at__lte=now).select_related(
        "offer__decision__request", "offer__decision__actor", "offer__actor", "client",
        "receipt__task", "source", "revision").order_by("-created_at", "pk")
    bounded = list(choices[:11])
    if len(bounded) > 10:
        return {"ready": False, "reason": "shipping_choice_capture_bound"}
    matched = []
    for choice in bounded:
        try:
            _, terms = _choice_frame(choice, now)
            facts = _order_facts(order, choice)
            _allocations(choice, terms, facts)
        except SplitExecutionError:
            continue
        matched.append(choice)
    if len(matched) != 1:
        return {"ready": False, "reason": "shipping_choice_missing_or_ambiguous"}
    choice = matched[0]
    return bind_split_plan(choice.pk, order.pk, actor=choice.offer.actor,
        operation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"ig-split-order:{order.pk}:{choice.pk}"), now=now)


def try_bind_paid_shipping_order(order_id):
    """A shipping read failure must never roll back a verified customer payment."""
    try:
        return bind_paid_shipping_order(order_id)
    except Exception:
        logger.exception("Unable to bind exact shipping agreement for paid order %s", order_id)
        return {"ready": False, "reason": "shipping_binding_unavailable"}


def reconcile_paid_shipping_plans(*, limit=10):
    """Recover a lost post-commit wakeup only for actual, still-unbound agreements."""
    from management.models import IgSplitShippingChoice
    from orders.models import Order
    from django.utils import timezone
    if not reviews._enabled():
        return 0
    client_ids = list(IgSplitShippingChoice.objects.filter(plan__isnull=True,
        created_at__gte=timezone.now() - timedelta(hours=24), client__privacy_erasure_started_at__isnull=True)
        .order_by("created_at", "pk").values_list("client_id", flat=True)[:max(0, min(int(limit), 20))])
    if not client_ids:
        return 0
    ids = Order.objects.filter(payment_payload__source_cart_provenance__owner__client_id__in=client_ids,
        instagram_split_plan__isnull=True).exclude(status__in=("done", "cancelled", "refunded"))
    return sum(bool(try_bind_paid_shipping_order(identity).get("created"))
        for identity in ids.order_by("created", "pk").values_list("pk", flat=True)[:max(0, min(int(limit), 20))])
