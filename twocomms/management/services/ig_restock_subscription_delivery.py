"""Dedicated one-off restock dispatch, gated by confirmed fulfilment.

The inventory ledger and explicit subscription cannot authorize this consumer
without a fresh typed manager decision. Unsupported future native capability
never extends the conservative customer message window.
"""
from contextlib import contextmanager
from datetime import timedelta
import uuid

from django.db import transaction
from django.utils import timezone

from management.services import ig_restock_subscriptions as agreements
from management.services.ig_purpose_sources import latest_customer_source
from management.services.ig_payment_reminder_time import quiet_time_allowed

HOLD_REASONS = {"waiting_window", "quiet_hours", "pending_inbound", "manager_decision_unverified",
    "manager_decision_expired", "inventory_unavailable", "inventory_unknown", "service_case_open",
    "manager_review_pending", "manager_review_expired", "manager_review_stale", "manager_review_unavailable", "manager_actor_forbidden",
    "other_availability_send_in_progress", "availability_coordination_unverified", "other_availability_receipt_requires_review"}


def _event_guard(task, settings_obj, *, now):
    from management.models import InstagramBotSettings
    settings_obj = InstagramBotSettings.objects.filter(pk=settings_obj.pk).first() if settings_obj else None
    if not settings_obj:
        return None, "settings_missing"
    event = agreements.AvailabilityEvent.objects.select_related("subscription__accepted_answer__invitation__client",
        "subscription__accepted_answer__invitation__revision", "subscription__accepted_answer__invitation__request_source",
        "subscription__accepted_answer__receipt__effect", "subscription__accepted_answer__source").filter(task_id=task.pk).first()
    if not event or not agreements._verified(event, "event"):
        return None, "availability_event_unverified"
    subscription = event.subscription
    answer = subscription.accepted_answer
    inv = answer.invitation
    reason = agreements._subscription_reason(subscription, settings_obj, now=now)
    if reason:
        return None, reason
    if (task.client_id != subscription.client_id or task.reason != agreements.ORIGIN
        or task.event_key != event.event_key or task.trigger != "event"
        or task.event_payload != {"origin": agreements.ORIGIN, "purpose": agreements.PURPOSE,
            "subscription_id": str(subscription.pk), "event_id": str(event.pk)}
        or event.envelope.get("answer_hmac") != answer.envelope_hmac
        or event.envelope.get("target_digest") != inv.target_digest
        or event.envelope.get("availability", {}).get("status") not in {"available", "unavailable", "unknown"}
        or not event.envelope.get("manager_decision")
        or event.envelope.get("availability", {}).get("target") != inv.target
        or event.envelope.get("availability", {}).get("digest") != event.availability_digest):
        return None, "availability_event_binding_changed"
    from management.services.ig_fulfillment_notification_coordination import restock_update_coordination
    from management.services.ig_restock_waiting_list import is_list
    if is_list(inv.target):
        from management.services.ig_restock_waiting_list_coordination import restock_update_coordination as list_coordination
        coordinated = list_coordination(event, settings_obj, now=now)
    else:
        coordinated = restock_update_coordination(event, settings_obj, now=now)
    if coordinated.get("reason"):
        return (event, ""), coordinated["reason"]
    client = inv.client
    if latest_customer_source(client, settings_obj, now=now, after_id=answer.source_id, pending_only=True):
        return None, "pending_inbound"
    source = latest_customer_source(client, settings_obj, now=now)
    if not source or not source.provider_created_at or now >= source.provider_created_at + agreements.WINDOW:
        return None, "waiting_window"
    if not quiet_time_allowed(now):
        return None, "quiet_hours"
    from management.services.ig_restock_availability import observe_target
    observation = agreements._observe_target(inv.target, now=now)
    if observation.get("target") != inv.target:
        return None, "inventory_target_changed"
    admission = agreements.manager_admission(subscription, observation, now=now)
    if not admission.get("ready"):
        return None, admission.get("reason") or "manager_decision_unverified"
    # The event retains its original decision as history. A renewed, signed
    # decision may authorize the SAME immutable interest; the current manager
    # gate binds its exact configuration, checked quantity and current stock
    # observation. Authenticated accessible goods may override stale records;
    # inventory observations alone can never authorize this message.
    if not isinstance(admission.get("proof"), dict) or not admission["proof"]:
        return None, "manager_decision_unverified"
    text = agreements._copy_for(inv, "notification")
    if len(text.encode()) > 1000:
        return None, "physical_message_too_long"
    return (event, text), ""


def _record_delegated_delivery(task, guard, settings_obj, *, now):
    """Consume the agreement from its real original-purpose native receipt.

    The restock task is cancelled. Its own MID/history remain empty because
    no restock HTTP was made; signed provenance points to the actual sender.
    Caller owns this task's transaction and existing automation lease.
    """
    from management.services.ig_fulfillment_notification_coordination import restock_update_coordination, FULFILLED
    if not guard or task.provider_message_id or task.status not in {"pending", "processing"}:
        return False
    event = guard[0]
    coordinated = restock_update_coordination(event, settings_obj, now=now)
    proof = coordinated.get("delegated_receipt")
    if coordinated.get("reason") != FULFILLED or not isinstance(proof, dict):
        return False
    key_id = next(iter(agreements._keys()))
    task.manager_context = {**(task.manager_context or {}), "delegated_delivery": {
        "version": 1, "proof": proof, "key_id": key_id,
        "hmac": agreements._sign("delegated_delivery", proof, key_id)}}
    changed = agreements.Subscription.objects.filter(pk=event.subscription_id, state="active").update(
        state="sent", last_reason=FULFILLED)
    if not changed:
        return False
    task.status, task.skip_reason = "cancelled", FULFILLED
    task.claim_token, task.claim_until, task.attempt_count, task.next_attempt_at = "", None, 0, None
    task.save(update_fields=["manager_context", "status", "skip_reason", "claim_token", "claim_until",
        "attempt_count", "next_attempt_at", "updated_at"])
    return True


@contextmanager
def _physical_boundary(task_id, token, settings_obj, text, *, now=None, admission=None, **delivery):
    from management.models import IgFollowUpTask
    clock = now or timezone.now()
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id).first()
        guard, reason = _event_guard(task, settings_obj, now=clock) if task else (None, "task_missing")
        if reason == "fulfilled_by_manager_update":
            if not _record_delegated_delivery(task, guard, settings_obj, now=clock):
                reason = "availability_coordination_unverified"
        if not reason and (task.status != "processing" or task.claim_token != token or task.provider_message_id
            or not task.claim_until or task.claim_until <= clock):
            reason = "claim_changed"
        if not reason and (guard[1] != text or task.message_text != text
            or delivery.get("delivered_chunk_count", 0) or delivery.get("planned_chunk_count", 1) != 1):
            reason = "physical_message_changed"
        if reason and task and task.status == "processing" and task.claim_token == token and not task.provider_message_id:
            # A denied request never becomes UNKNOWN; no time/policy shift.
            task.status = "pending" if reason in HOLD_REASONS else "cancelled"
            task.skip_reason = reason
            task.claim_token = ""
            task.claim_until = None
            task.attempt_count = 0
            task.next_attempt_at = clock + agreements.CHECK_INTERVAL if reason in HOLD_REASONS else None
            task.save(update_fields=["status", "skip_reason", "claim_token", "claim_until", "attempt_count", "next_attempt_at", "updated_at"])
    if admission is not None and not reason:
        admission["admitted"] = True
    yield agreements.RestockBoundary(not reason, reason)


def _checkpoint(task_id, token, mid, text, *, now=None):
    from management.models import IgFollowUpTask
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    mid = normalize_provider_message_id(mid)
    if not mid:
        return False
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            return task.provider_message_id == mid
        if task.reason != agreements.ORIGIN or task.claim_token != token or task.attempt_count != 1 or task.message_text != text:
            return False
        task.provider_message_id = mid
        task.status = "sent"
        task.sent_at = now or timezone.now()
        task.claim_token = ""
        task.claim_until = None
        task.save(update_fields=["provider_message_id", "status", "sent_at", "claim_token", "claim_until", "updated_at"])
        agreements.Subscription.objects.filter(availability_event__task_id=task_id).update(state="sent", last_reason="sent_once")
    return True


def _history(task_id):
    from management.models import IgFollowUpTask, InstagramBotMessage
    try:
        with transaction.atomic():
            task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
            if task.status != "sent" or not task.provider_message_id or task.sent_message_id:
                return bool(task.sent_message_id)
            event = agreements.AvailabilityEvent.objects.select_related("subscription__accepted_answer__invitation__client").get(task_id=task_id)
            inv = event.subscription.accepted_answer.invitation
            if not agreements._verified(event, "event") or not agreements._verified(inv, "invitation") or inv.client.privacy_erasure_started_at:
                return False
            task.sent_message = InstagramBotMessage.objects.create(client_id=task.client_id, sender_id=inv.envelope["recipient"],
                role="model", source="followup", status="done", text=task.message_text,
                provider_message_id=task.provider_message_id, provider_namespace=inv.envelope["source"]["namespace"], processed_at=task.sent_at)
            task.save(update_fields=["sent_message", "updated_at"])
        return True
    except Exception:
        return False


def _finish(task_id, token, receipt, *, admitted):
    from management.models import IgFollowUpTask
    from management.services.instagram_bot import ProviderDeliveryReceipt
    from management.services.ig_delivery_receipts import normalize_provider_message_ids
    native = isinstance(receipt, ProviderDeliveryReceipt)
    mids = normalize_provider_message_ids(getattr(receipt, "provider_message_ids", ())) if native else ()
    ok = bool(native and receipt.ok and len(mids) == 1 and receipt.planned_chunk_count == 1 and receipt.delivered_chunk_count == 1)
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            if mids and mids[0] != task.provider_message_id:
                task.status = "ambiguous"
                task.last_error = "native_receipt_identity_changed"
                task.save(update_fields=["status", "last_error", "updated_at"])
                agreements.Subscription.objects.filter(availability_event__task_id=task_id).update(state="unknown", last_reason="native_receipt_identity_changed")
                return False
            ok = task.status == "sent"
        elif task.claim_token == token:
            if ok:
                task.provider_message_id = mids[0]
                task.status = "sent"
                task.sent_at = timezone.now()
                agreements.Subscription.objects.filter(availability_event__task_id=task_id).update(state="sent", last_reason="sent_once")
            elif admitted and not (native and receipt.kind == "cancelled" and not mids):
                task.provider_message_id = mids[0] if mids else ""
                task.status = "ambiguous"
                task.last_error = "provider_outcome_unknown_no_retry"
                agreements.Subscription.objects.filter(availability_event__task_id=task_id).update(state="unknown", last_reason="provider_outcome_unknown_no_retry")
            else:
                task.status = "pending"
                task.attempt_count = 0
                task.skip_reason = "known_not_dispatched"
            task.claim_token = ""
            task.claim_until = None
            task.save(update_fields=["provider_message_id", "status", "sent_at", "last_error", "attempt_count", "skip_reason", "claim_token", "claim_until", "updated_at"])
    if ok:
        _history(task_id)
    return ok


def process_due_restock_notifications(settings_obj, now=None, limit=50):
    from management.models import IgFollowUpTask, InstagramBotSettings
    from django.db.models import Q
    from management.services import instagram_bot as bot
    from management.services.ig_reply_boundary import capture_reply_permission, customer_send_boundary
    if not agreements._enabled():
        return 0
    settings_obj = settings_obj or InstagramBotSettings.load()
    fixed_clock = now
    now = now or timezone.now()
    bound = max(0, min(int(limit), 50))
    for identity in IgFollowUpTask.objects.filter(reason=agreements.ORIGIN, status="sent", sent_message__isnull=True).exclude(provider_message_id="").values_list("pk", flat=True)[:bound]:
        _history(identity)
    stranded = list(IgFollowUpTask.objects.filter(reason=agreements.ORIGIN, status="processing", claim_until__lte=now,
        provider_message_id="").values_list("pk", flat=True)[:bound])
    for identity in stranded:
        with transaction.atomic():
            changed = IgFollowUpTask.objects.filter(pk=identity, status="processing", provider_message_id="").update(
                status="ambiguous", skip_reason="processing_crash_no_retry", claim_token="", claim_until=None)
            if changed:
                agreements.Subscription.objects.filter(availability_event__task_id=identity).update(state="unknown", last_reason="processing_crash_no_retry")
    ids = list(IgFollowUpTask.objects.filter(reason=agreements.ORIGIN, status="pending", due_at__lte=now)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("due_at", "pk").values_list("pk", flat=True)[:bound])
    sent = 0
    for identity in ids:
        candidate = IgFollowUpTask.objects.filter(pk=identity).first()
        if not candidate:
            continue
        client, lease = bot.acquire_client_automation_lease(candidate.client_id)
        if not client:
            continue
        token = uuid.uuid4().hex
        try:
            with transaction.atomic():
                task = IgFollowUpTask.objects.select_for_update().get(pk=identity)
                if task.status != "pending" or task.provider_message_id or task.attempt_count:
                    continue
                guard, reason = _event_guard(task, settings_obj, now=fixed_clock or timezone.now())
                if reason == "fulfilled_by_manager_update":
                    if _record_delegated_delivery(task, guard, settings_obj, now=fixed_clock or timezone.now()):
                        continue
                    reason = "availability_coordination_unverified"
                if reason:
                    task.skip_reason = reason
                    if reason not in HOLD_REASONS:
                        task.status = "cancelled"
                    else:
                        task.next_attempt_at = now + agreements.CHECK_INTERVAL
                    task.save(update_fields=["skip_reason", "status", "next_attempt_at", "updated_at"])
                    continue
                text = guard[1]
                task.status = "processing"
                task.claim_token = token
                task.claim_until = now + timedelta(minutes=3)
                task.attempt_count = 1
                task.message_text = text
                task.save(update_fields=["status", "claim_token", "claim_until", "attempt_count", "message_text", "updated_at"])
            permission = capture_reply_permission(settings_obj.pk, client.pk)
            admission = {"admitted": False}
            receipt = None
            if bot.renew_client_automation_lease(client.pk, lease):
                try:
                    receipt = bot.send_text(settings_obj, client.igsid, text, return_receipt=True,
                        provider_message_callback=lambda mid: _checkpoint(identity, token, mid, text, now=fixed_clock),
                        permission_boundary_factory=lambda: customer_send_boundary(settings_obj.pk, client.pk, permission),
                        provider_request_boundary_factory=lambda **state: _physical_boundary(identity, token, settings_obj, text,
                            now=fixed_clock, admission=admission, **state))
                except Exception:
                    pass
            sent += int(_finish(identity, token, receipt, admitted=admission["admitted"]))
        finally:
            bot.release_client_automation_lease(client.pk, lease)
    return sent
