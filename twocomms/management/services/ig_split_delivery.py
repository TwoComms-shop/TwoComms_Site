"""Native, finite split-offer delivery; uncertain provider outcomes never replay."""
from contextlib import contextmanager
from datetime import timedelta
import json
import uuid

from django.db import transaction
from django.utils import timezone

from management.services.ig_payment_reminder_time import quiet_time_allowed
from management.services.ig_purpose_sources import latest_customer_source

ORIGIN = "split_shipping_offer"
CHECK_INTERVAL = timedelta(minutes=15)
WINDOW = timedelta(hours=23)
HOLD_REASONS = {"waiting_window", "quiet_hours", "pending_inbound", "permission_changed",
    "offer_body_too_large", "source_not_ready", "other_split_send_in_progress"}


def _core():
    from management.services import ig_split_shipping
    return ig_split_shipping


def _guard(task, settings_obj, *, now):
    from management.ig_split_shipping_models import IgSplitShippingOffer as Offer, IgSplitShippingChoice as Choice
    if not task or task.reason != ORIGIN or task.trigger != "event":
        return None, "task_binding_changed"
    offer = Offer.objects.select_related("decision__request", "decision__actor", "actor", "client", "task").filter(task_id=task.pk).first()
    reason = _core().current_offer_reason(offer, settings_obj, now)
    if reason:
        return None, reason
    if task.event_key != f"split-shipping:{offer.pk}" or task.event_payload != {"offer_id": str(offer.pk)}:
        return None, "task_binding_changed"
    if Choice.objects.filter(offer=offer).exists():
        return None, "choice_already_recorded"
    request, client = offer.envelope["request"], offer.client
    if client.privacy_erasure_started_at:
        return None, "client_erased"
    if latest_customer_source(client, settings_obj, now=now, after_id=int(request.get("source_id") or 0), pending_only=True):
        return None, "pending_inbound"
    source = latest_customer_source(client, settings_obj, now=now)
    if source and source.provider_namespace != request["source_namespace"]:
        return None, "task_binding_changed"
    if not source or not source.provider_created_at or now >= source.provider_created_at + WINDOW:
        return None, "waiting_window"
    if not quiet_time_allowed(now):
        return None, "quiet_hours"
    text, buttons = _core().render_offer(offer)
    if not text or len(buttons) != 2:
        return None, "offer_body_too_large"
    proof = {"offer_id": str(offer.pk), "offer_digest": offer.envelope_hmac,
        "request_id": offer.envelope["request_id"], "request_digest": offer.envelope["request_digest"],
        "decision_id": offer.envelope["decision_id"], "decision_digest": offer.envelope["decision_digest"],
        "recipient": client.igsid, "source_namespace": request["source_namespace"],
        "body_digest": _core()._body_digest(offer)}
    return ({"proof": proof}, text, buttons), ""


def _defer(task, reason, now):
    task.status = "pending" if reason in HOLD_REASONS else "cancelled"
    task.skip_reason = reason
    task.claim_token, task.claim_until, task.attempt_count = "", None, 0
    task.next_attempt_at = now + CHECK_INTERVAL if reason in HOLD_REASONS else None
    task.save(update_fields=["status", "skip_reason", "claim_token", "claim_until", "attempt_count", "next_attempt_at", "updated_at"])


@contextmanager
def _physical_boundary(task_id, token, settings_obj, text, proof, *, now=None, admission=None, **delivery):
    from management.models import IgFollowUpTask
    from management.services.instagram_bot import ProviderRequestBoundaryResult
    clock = now or timezone.now()
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id).first()
        guard, reason = _guard(task, settings_obj, now=clock)
        if not reason and (task.status != "processing" or task.claim_token != token or task.provider_message_id
                or not task.claim_until or task.claim_until <= clock):
            reason = "claim_changed"
        if not reason and (guard[1] != text or task.message_text != text or guard[0]["proof"] != proof
                or delivery.get("delivered_chunk_count", 0) != 0 or delivery.get("planned_chunk_count", 1) != 1):
            reason = "physical_message_changed"
        if reason and task and task.status == "processing" and task.claim_token == token and not task.provider_message_id:
            _defer(task, reason, clock)
    if admission is not None and not reason:
        admission["admitted"] = True
    yield ProviderRequestBoundaryResult(not reason, reason=reason)


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
        if (task.reason != ORIGIN or task.status != "processing" or task.claim_token != token
                or task.attempt_count != 1 or task.message_text != text):
            return False
        task.provider_message_id, task.status, task.sent_at = mid, "sent", now or timezone.now()
        task.claim_token, task.claim_until = "", None
        task.save(update_fields=["provider_message_id", "status", "sent_at", "claim_token", "claim_until", "updated_at"])
    return True


def _history(task_id):
    from management.models import IgFollowUpTask, InstagramBotMessage
    try:
        with transaction.atomic():
            task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
            if task.status != "sent" or not task.provider_message_id:
                return False
            receipt = _core().record_delivery(task, task.provider_message_id)
            if receipt is None:
                return False
            if task.sent_message_id:
                return True
            offer = receipt.offer
            request = offer.envelope["request"]
            task.sent_message = InstagramBotMessage.objects.create(client_id=task.client_id,
                sender_id=offer.client.igsid, role="model", source="followup", status="done",
                text=task.message_text, provider_message_id=task.provider_message_id,
                provider_namespace=request["source_namespace"], processed_at=task.sent_at)
            task.save(update_fields=["sent_message", "updated_at"])
        return True
    except Exception:
        return False


def _finish(task_id, token, receipt, *, admitted, now=None):
    from management.models import IgFollowUpTask
    from management.services.instagram_bot import ProviderDeliveryReceipt
    from management.services.ig_delivery_receipts import normalize_provider_message_ids
    native = isinstance(receipt, ProviderDeliveryReceipt)
    mids = normalize_provider_message_ids(receipt.provider_message_ids) if native else ()
    ok = bool(admitted and native and receipt.ok and len(mids) == 1 and receipt.planned_chunk_count == 1 and receipt.delivered_chunk_count == 1)
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            if (mids and (len(mids) != 1 or mids[0] != task.provider_message_id)
                    or native and receipt.request_text and receipt.request_text != task.message_text):
                task.status, task.last_error = "ambiguous", "native_receipt_identity_changed"
                task.save(update_fields=["status", "last_error", "updated_at"])
                return False
            ok = task.status == "sent"
        elif task.status == "processing" and task.claim_token == token:
            if native and receipt.request_text and receipt.request_text != task.message_text:
                ok = False
            if ok:
                task.provider_message_id, task.status, task.sent_at = mids[0], "sent", now or timezone.now()
            elif mids or (admitted and not (native and receipt.kind == "cancelled" and not receipt.delivered_chunk_count)):
                task.status, task.last_error = "ambiguous", "provider_outcome_unknown_no_retry"
                task.provider_message_id = mids[0] if len(mids) == 1 else ""
            else:
                task.status, task.attempt_count, task.skip_reason = "pending", 0, "known_not_dispatched"
                task.next_attempt_at = (now or timezone.now()) + CHECK_INTERVAL
            task.claim_token, task.claim_until = "", None
            task.save(update_fields=["provider_message_id", "status", "sent_at", "last_error", "attempt_count", "skip_reason", "claim_token", "claim_until", "next_attempt_at", "updated_at"])
        else:
            ok = False
    if ok:
        _history(task_id)
    return ok


def process_due_split_offers(settings_obj, now=None, limit=20):
    from django.db.models import Q
    from management.models import IgFollowUpTask, InstagramBotSettings
    from management.services import instagram_bot as bot
    from management.services.ig_reply_boundary import capture_reply_permission, customer_send_boundary
    if not _core().reviews._enabled():
        return 0
    settings_obj = settings_obj or InstagramBotSettings.load()
    fixed_clock, clock = now, now or timezone.now()
    bound = max(0, min(int(limit), 20))
    for identity in IgFollowUpTask.objects.filter(reason=ORIGIN, status="sent", sent_message__isnull=True).exclude(provider_message_id="").values_list("pk", flat=True)[:bound]:
        _history(identity)
    IgFollowUpTask.objects.filter(pk__in=list(IgFollowUpTask.objects.filter(reason=ORIGIN, status="processing", claim_until__lte=clock,
        provider_message_id="").values_list("pk", flat=True)[:bound]), status="processing", provider_message_id="").update(
        status="ambiguous", skip_reason="processing_crash_no_retry", claim_token="", claim_until=None)
    ids = list(IgFollowUpTask.objects.filter(reason=ORIGIN, status="pending", due_at__lte=clock)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=clock)).order_by("due_at", "pk").values_list("pk", flat=True)[:bound])
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
                guard, reason = _guard(task, settings_obj, now=fixed_clock or timezone.now())
                if reason:
                    _defer(task, reason, fixed_clock or timezone.now())
                    continue
                projection, text, buttons = guard
                # Copy only the finite signed admission proof into this attempt.
                proof = json.loads(json.dumps(projection["proof"]))
                task.status, task.claim_token, task.claim_until = "processing", token, (fixed_clock or timezone.now()) + timedelta(minutes=3)
                task.attempt_count, task.message_text = 1, text
                task.save(update_fields=["status", "claim_token", "claim_until", "attempt_count", "message_text", "updated_at"])
            permission = capture_reply_permission(settings_obj.pk, client.pk)
            admission, receipt = {"admitted": False}, None
            if bot.renew_client_automation_lease(client.pk, lease):
                try:
                    receipt = bot.send_text(settings_obj, client.igsid, text, return_receipt=True, allow_url_fallback=False, quick_replies=buttons,
                        provider_message_callback=lambda mid: admission["admitted"] and _checkpoint(identity, token, mid, text, now=fixed_clock),
                        permission_boundary_factory=lambda: customer_send_boundary(settings_obj.pk, client.pk, permission),
                        provider_request_boundary_factory=lambda **state: _physical_boundary(identity, token, settings_obj, text, proof,
                            now=fixed_clock, admission=admission, **state))
                except Exception:
                    pass
            sent += int(_finish(identity, token, receipt, admitted=admission["admitted"], now=fixed_clock))
        finally:
            bot.release_client_automation_lease(client.pk, lease)
    return sent
