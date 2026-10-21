"""Conservative coordination of exact list and native CART availability.

An overlapping original-purpose receipt is not an ALL-list receipt. Until a
complete native composite receipt protocol is available, preserve its original
MID and hold the other purpose rather than deliver the same availability twice.
"""
from management.models import IgFollowUpTask
from management.services import ig_fulfillment_notification_coordination as coordination
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_subscriptions as restock
from management.services import ig_restock_waiting_list as waiting

RECEIPT_HOLD = "other_availability_receipt_requires_review"
MAX_TASKS = 32


def _overlap(request, decision, invitation):
    wanted = {(item["line_id"], item["recipient_id"], restock._digest(item["catalog_target"]))
        for item in invitation.target["items"] if item["catalog_target"] is not None}
    available = {row["line_id"] for row in decision.envelope.get("lines", [])
        if row.get("availability") == "available"}
    return any((line.get("line_id"), line.get("recipient_id"), restock._digest(line.get("target"))) in wanted
        for line in request.envelope.get("lines", []) if line.get("line_id") in available)


def restock_update_coordination(event, settings_obj, *, now):
    subscription = event.subscription
    inv = subscription.accepted_answer.invitation
    if (not waiting.is_list(inv.target) or not restock._verified(event, "event")
        or not restock._valid_answer(subscription.accepted_answer, settings_obj)
        or event.envelope.get("target_digest") != inv.target_digest):
        return {"reason": coordination.CORRUPT}
    tasks = list(IgFollowUpTask.objects.filter(client_id=subscription.client_id,
        reason=reviews.UPDATE_REASON, kind="fulfillment", status__in=["pending", "processing", "ambiguous", "unknown", "sent"])
        .select_related("sent_message").order_by("-created_at", "-pk")[:MAX_TASKS + 1])
    if len(tasks) > MAX_TASKS:
        return {"reason": coordination.CORRUPT}
    for task in tasks:
        # A durable earlier receipt cannot consume or block a later agreement.
        if task.status == "sent" and task.sent_at and task.sent_at < subscription.accepted_answer.answered_at:
            continue
        if "source_kind" in (task.event_payload or {}):
            from management.services import ig_assisted_manager_update as assisted
            # This original-purpose update describes an authenticated ALL-WAIT
            # decision, never physical stock availability or a delegated MID.
            if not assisted.validated_update_history(task).get("ready"):
                return {"reason": coordination.CORRUPT}
            continue
        binding = coordination._cart_binding(task)
        if not binding:
            return {"reason": coordination.CORRUPT}
        if not _overlap(*binding, inv):
            continue
        state = coordination._task_state(task)
        if state == "corrupt":
            return {"reason": coordination.CORRUPT}
        if state == "sent":
            # This remains a CART receipt. No delegated MID, no subscription
            # consumption, and no claim that all accepted items were notified.
            return {"reason": RECEIPT_HOLD}
        if state in {"pending", "hold"}:
            return {"reason": coordination.HOLD}
    return {"reason": ""}


def cart_update_coordination(task, *, now):
    """CART has priority until a list send starts; a later CART cannot replay it."""
    binding = coordination._cart_binding(task)
    if not binding:
        return {"reason": coordination.CORRUPT}
    events = list(restock.AvailabilityEvent.objects.filter(subscription__client_id=task.client_id,
        subscription__accepted_answer__invitation__target__schema=waiting.SCHEMA).select_related(
            "task", "subscription__accepted_answer__invitation", "subscription__accepted_answer__source")
        .order_by("-observed_at", "pk")[:MAX_TASKS + 1])
    if len(events) > MAX_TASKS:
        return {"reason": coordination.CORRUPT}
    for event in events:
        inv = event.subscription.accepted_answer.invitation
        if (not restock._verified(event, "event") or not restock._verified(inv, "invitation")
            or inv.target_digest != restock._digest(inv.target)
            or event.envelope.get("target_digest") != inv.target_digest
            or event.envelope.get("answer_hmac") != event.subscription.accepted_answer.envelope_hmac):
            return {"reason": coordination.CORRUPT}
        if not _overlap(*binding, inv):
            continue
        state = coordination._task_state(event.task)
        if state == "corrupt":
            return {"reason": coordination.CORRUPT}
        if state == "sent" and event.task.sent_at >= event.subscription.accepted_answer.answered_at:
            return {"reason": coordination.HOLD}
        if state == "hold":
            return {"reason": coordination.HOLD}
    return {"reason": ""}
