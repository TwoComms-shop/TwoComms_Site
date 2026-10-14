"""Finite native assisted card delivery and authenticated customer sources.

Uses the same task leases, Meta window and native checkpoint lifecycle as split
shipping; signed workflow terms and native receipts are independent authority.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta, datetime
import hmac
import json
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from management.services.ig_payment_reminder_time import quiet_time_allowed
from management.services.ig_purpose_sources import latest_customer_source

ORIGINS = ("assisted_partial_offer", "assisted_continuation_offer")
CHECK_INTERVAL = timedelta(minutes=15)
WINDOW = timedelta(hours=23)
HOLD_REASONS = {"waiting_window", "quiet_hours", "pending_inbound", "permission_changed",
    "offer_body_too_large", "source_not_ready", "other_split_send_in_progress"}
_ISSUER = object()


class AssistedPartialSourceError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class NativeAssistedCardReceipt:
    task_id: int
    card: dict
    receipt: dict
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            raise AssistedPartialSourceError("native_card_receipt_requires_producer")


@dataclass(frozen=True)
class NativeAssistedCardChoice:
    task_id: int
    action: str
    card: dict
    receipt: dict
    source_proof: dict
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            raise AssistedPartialSourceError("native_card_choice_requires_producer")


def _core():
    from management.services import ig_assisted_partial
    return ig_assisted_partial


def _card_payload_hint(raw):
    """Locate a candidate only; the signed delivered card authenticates it.

    Keep this private to the same validation call. A syntactic task/action
    never supplies card authority or authorizes a customer choice.
    """
    match = re.fullmatch(r"AC1:([1-9]\d{0,19}):(p|t|r):([a-f0-9]{16})", str(raw or ""))
    return (int(match[1]), {"p": "partial", "t": "together", "r": "resume"}[match[2]]) if match else None


def _guard(task, settings_obj, *, now):
    if not task or task.reason not in ORIGINS or task.trigger != "event":
        return None, "task_binding_changed"
    card = _core().card_for_task(task, settings_obj, now=now, require_current=True)
    if not card.get("ready"):
        return None, card.get("reason") or "assisted_card_unverified"
    proof, client = card["proof"], task.client
    if (proof.get("task_id") != task.pk or proof.get("client_id") != client.pk
        or task.event_key != "assisted-card:" + proof["operation_id"]
        or task.event_payload != {"origin": task.reason, "operation_id": proof["operation_id"]}):
        return None, "task_binding_changed"
    if latest_customer_source(client, settings_obj, now=now, after_id=int(proof["source_id"]), pending_only=True):
        return None, "pending_inbound"
    source = latest_customer_source(client, settings_obj, now=now)
    if not source or source.provider_namespace != proof["source_namespace"]:
        return None, "source_not_ready"
    if not source.provider_created_at or now >= source.provider_created_at + WINDOW:
        return None, "waiting_window"
    if not quiet_time_allowed(now):
        return None, "quiet_hours"
    text, buttons = card["text"], card["buttons"]
    from management.services.ig_delivery_plan import build_delivery_plan
    plan = build_delivery_plan(text)
    if not text or not plan.complete or not 1 <= len(buttons) <= 2:
        return None, "offer_body_too_large"
    return ({"proof": deepcopy(proof)}, text, buttons), ""


def _progress(task):
    from management.services import ig_assisted_commerce as commerce
    signed = (task.manager_context or {}).get("assisted_partial_progress") or {}
    value = signed.get("manifest") or {}
    signature = commerce._sign("partial_progress", value, signed.get("key_id"))
    if not value:
        return {}
    if not signature or not hmac.compare_digest(str(signed.get("hmac") or ""), signature):
        raise AssistedPartialSourceError("native_card_progress_changed")
    return value


def _complete_delivery_parts(task, card):
    from management.services.ig_delivery_plan import build_delivery_plan
    from management.services import ig_assisted_commerce as commerce
    plan = build_delivery_plan(card["text"])
    progress = _progress(task)
    mids = progress.get("provider_message_ids") or []
    expected = {"task_id": task.pk, "body_digest": card["proof"]["body_digest"],
        "chunk_digests": [commerce._digest(chunk) for chunk in plan.chunks], "provider_message_ids": mids}
    if (not plan.complete or progress != expected or not mids or len(mids) != len(plan.chunks)
        or len(set(mids)) != len(mids) or mids[-1] != task.provider_message_id):
        raise AssistedPartialSourceError("native_card_delivery_incomplete")
    return {"provider_message_ids": mids, "planned_chunk_count": len(plan.chunks),
        "delivered_chunk_count": len(mids), "chunk_digests": expected["chunk_digests"]}


def _store_native_receipt(task, proof):
    from management.services import ig_assisted_commerce as commerce
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    card = _core().card_for_task(task, now=task.sent_at, require_current=False)
    if (not card.get("ready") or card["proof"] != proof or not task.sent_at
        or task.status != "sent" or task.attempt_count != 1
        or task.message_text != card["text"] or not normalize_provider_message_id(task.provider_message_id)):
        raise AssistedPartialSourceError("native_card_checkpoint_changed")
    manifest = {"schema": "assisted-card-delivery.v1", "task_id": task.pk,
        "client_id": task.client_id, "card_digest": proof["card_digest"], "body_digest": proof["body_digest"],
        "native_mid": task.provider_message_id, "source_namespace": proof["source_namespace"],
        "delivered_at": task.sent_at.isoformat(), "attempt_count": 1,
        **_complete_delivery_parts(task, card)}
    key_id = task.manager_context["assisted_partial_card"]["key_id"]
    signed = {"key_id": key_id, "manifest": manifest, "hmac": commerce._sign("partial_delivery", manifest, key_id)}
    task.manager_context = {**task.manager_context, "assisted_partial_delivery": signed}


def capture_delivered_card(task, settings_obj=None, *, now=None, require_current=True, read_unit=None):
    from management.models import IgFollowUpTask
    from management.services import ig_assisted_commerce as commerce
    actual = IgFollowUpTask.objects.select_related("client").filter(pk=getattr(task, "pk", task)).first()
    if not actual or actual.reason not in ORIGINS:
        return None, "assisted_card_task_missing"
    card = _core().card_for_task(actual, settings_obj, now=now, require_current=require_current,
        **({"read_unit": read_unit} if require_current and read_unit is not None else {}))
    if not card.get("ready"):
        return None, card.get("reason") or "assisted_card_unverified"
    if (actual.status != "sent" or actual.attempt_count != 1 or not actual.provider_message_id or not actual.sent_at
        or actual.claim_token or actual.claim_until):
        return None, "assisted_card_native_receipt_missing"
    try:
        parts = _complete_delivery_parts(actual, card)
    except AssistedPartialSourceError as exc:
        return None, exc.reason
    signed = (actual.manager_context or {}).get("assisted_partial_delivery") or {}
    native = signed.get("manifest") or {}
    proof = card["proof"]
    expected = {"schema": "assisted-card-delivery.v1", "task_id": actual.pk,
        "client_id": actual.client_id, "card_digest": proof["card_digest"], "body_digest": proof["body_digest"],
        "native_mid": actual.provider_message_id, "source_namespace": proof["source_namespace"],
        "delivered_at": actual.sent_at.isoformat() if actual.sent_at else "", "attempt_count": 1,
        **parts}
    signature = commerce._sign("partial_delivery", native, signed.get("key_id"))
    if (actual.status != "sent" or actual.attempt_count != 1 or not actual.provider_message_id or not actual.sent_at
        or actual.claim_token or actual.claim_until or actual.message_text != card["text"]
        or native != expected or not signature or not hmac.compare_digest(str(signed.get("hmac") or ""), signature)):
        return None, "assisted_card_native_receipt_missing"
    return NativeAssistedCardReceipt(actual.pk, card, deepcopy(native), _ISSUER), ""


def validate_card_choice_source(source, revision, settings_obj, *, now=None, expected_task_id=None,
        require_current=True, read_unit=None):
    from management.models import IgFollowUpTask
    from management.services.ig_purpose_sources import owned_source
    from management.services.ig_reply_boundary import capture_reply_permission
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.bot_sales_classifier import is_reaction_only
    now = now or timezone.now()
    owned, reason = owned_source(revision, source, settings_obj, require_head=require_current)
    if not owned:
        raise AssistedPartialSourceError(reason)
    revision, source, source_proof = owned
    parsed = _card_payload_hint(source.quick_reply_payload)
    if not parsed or (expected_task_id is not None and parsed[0] != expected_task_id):
        raise AssistedPartialSourceError("assisted_card_payload_unverified")
    task_id, action = parsed
    task = IgFollowUpTask.objects.select_related("client").filter(pk=task_id, client_id=revision.client_id).first()
    native, reason = capture_delivered_card(task, settings_obj, now=now, require_current=require_current,
        read_unit=read_unit if require_current else None)
    if native is None:
        raise AssistedPartialSourceError(reason)
    card, proof = native.card, native.card["proof"]
    payload = card["actions"].get(action)
    title = next((button.title for button in card["buttons"] if button.payload == payload), None)
    permission = capture_reply_permission(settings_obj.pk, task.client_id) if require_current else None
    if (action not in {"partial", "together", "resume"} or not payload
        or not hmac.compare_digest(payload, source.quick_reply_payload) or title is None
        or source.provider_created_at < task.sent_at or source.provider_created_at > now
        or (require_current and source.provider_created_at >= datetime.fromisoformat(proof["expires_at"].replace("Z", "+00:00")))
        or source.provider_namespace != proof["source_namespace"] or source.pk < conversation_route_reset_floor(task.client_id)
        or (source.reply_to_provider_message_id and source.reply_to_provider_message_id != native.receipt["native_mid"])
        or (require_current and (not permission or permission.settings_epoch != proof["settings_epoch"]
            or permission.client_epoch != proof["client_epoch"]))):
        raise AssistedPartialSourceError("assisted_card_choice_source_changed")
    actual = {member.message_id: member.message for member in revision.sources.select_related("message")}
    for frozen in revision.bundle_snapshot.get("sources", ()):
        row = actual.get(frozen.get("message_id"))
        if (not row or frozen.get("role") != "user" or frozen.get("media_parts") or row.attachment_media
            or str(row.attachments or "").strip() not in {"", "[]", "null"}):
            raise AssistedPartialSourceError("assisted_card_mixed_source")
        text = str(row.text or "").strip()
        if row.quick_reply_payload:
            if row.quick_reply_payload != payload or text not in {"", title}:
                raise AssistedPartialSourceError("assisted_card_mixed_source")
        elif text and not is_reaction_only(text):
            raise AssistedPartialSourceError("assisted_card_mixed_source")
    return NativeAssistedCardChoice(task.pk, action, deepcopy(card), deepcopy(native.receipt), source_proof, _ISSUER)


def revalidate_native_card_choice(value, *, now=None, require_current=True, read_unit=None):
    from management.models import InstagramBotMessage, IgCustomerTurnRevision, InstagramBotSettings
    if not isinstance(value, NativeAssistedCardChoice) or value._issuer is not _ISSUER:
        raise AssistedPartialSourceError("native_card_choice_requires_producer")
    source = InstagramBotMessage.objects.get(pk=value.source_proof["message_id"])
    revision = IgCustomerTurnRevision.objects.get(pk=value.source_proof["revision_id"])
    settings_obj = InstagramBotSettings.objects.get(pk=value.card["proof"]["settings_id"])
    current = validate_card_choice_source(source, revision, settings_obj, now=now,
        expected_task_id=value.task_id, require_current=require_current,
        read_unit=read_unit if require_current else None)
    if current != value:
        raise AssistedPartialSourceError("native_card_choice_changed")
    return current


def _defer(task, reason, now):
    task.status = "pending" if reason in HOLD_REASONS else "cancelled"
    task.skip_reason = reason
    task.claim_token, task.claim_until, task.attempt_count = "", None, 0
    task.next_attempt_at = now + CHECK_INTERVAL if reason in HOLD_REASONS else None
    task.save(update_fields=["status", "skip_reason", "claim_token", "claim_until", "attempt_count", "next_attempt_at", "updated_at"])


@contextmanager
def _physical_boundary(task_id, token, settings_obj, text, proof, *, now=None, admission=None, **delivery):
    from management.models import IgFollowUpTask, IgClient
    from management.services.instagram_bot import ProviderRequestBoundaryResult
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    clock = now or timezone.now()
    with commercial_episode_client_lock(proof["client_id"]), transaction.atomic():
        IgClient.objects.select_for_update().get(pk=proof["client_id"])
        task = IgFollowUpTask.objects.select_for_update().filter(pk=task_id, client_id=proof["client_id"]).first()
        guard, reason = _guard(task, settings_obj, now=clock)
        if not reason and (task.status != "processing" or task.claim_token != token or task.provider_message_id
                or not task.claim_until or task.claim_until <= clock):
            reason = "claim_changed"
        from management.services.ig_delivery_plan import build_delivery_plan
        plan = build_delivery_plan(text)
        progress = _progress(task) if task else {}
        mids = progress.get("provider_message_ids") or []
        if not reason and (guard[1] != text or task.message_text != text or guard[0]["proof"] != proof
                or not plan.complete or delivery.get("delivered_chunk_count", 0) != len(mids)
                or tuple(delivery.get("provider_message_ids", ())) != tuple(mids)
                or delivery.get("planned_chunk_count", 0) != len(plan.chunks)):
            reason = "physical_message_changed"
        if reason and task and task.status == "processing" and task.claim_token == token and not task.provider_message_id:
            if mids:
                task.status, task.last_error = "ambiguous", "native_card_partial_delivery_no_retry"
                task.claim_token, task.claim_until = "", None
                task.save(update_fields=["status", "last_error", "claim_token", "claim_until", "updated_at"])
            else:
                _defer(task, reason, clock)
    if admission is not None and not reason:
        admission["admitted"] = True
    yield ProviderRequestBoundaryResult(not reason, reason=reason)


def _checkpoint(task_id, token, mid, text, proof, *, now=None):
    from management.models import IgFollowUpTask
    from management.services.ig_delivery_receipts import normalize_provider_message_id
    mid = normalize_provider_message_id(mid)
    if not mid:
        return False
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            return task.provider_message_id == mid
        if (task.reason not in ORIGINS or task.status != "processing" or task.claim_token != token
                or task.attempt_count != 1 or task.message_text != text):
            return False
        from management.services.ig_delivery_plan import build_delivery_plan
        from management.services import ig_assisted_commerce as commerce
        plan = build_delivery_plan(text)
        progress = _progress(task)
        mids = list(progress.get("provider_message_ids") or [])
        if not plan.complete or mid in mids or len(mids) >= len(plan.chunks):
            raise AssistedPartialSourceError("native_card_checkpoint_changed")
        mids.append(mid)
        body = {"task_id": task.pk, "body_digest": proof["body_digest"],
            "chunk_digests": [commerce._digest(chunk) for chunk in plan.chunks], "provider_message_ids": mids}
        key_id = task.manager_context["assisted_partial_card"]["key_id"]
        task.manager_context = {**task.manager_context, "assisted_partial_progress": {
            "key_id": key_id, "manifest": body, "hmac": commerce._sign("partial_progress", body, key_id)}}
        if len(mids) == len(plan.chunks):
            task.provider_message_id, task.status, task.sent_at = mids[-1], "sent", now or timezone.now()
            task.claim_token, task.claim_until = "", None
            _store_native_receipt(task, proof)
        task.save(update_fields=["provider_message_id", "status", "sent_at", "claim_token", "claim_until", "manager_context", "updated_at"])
    return True


def _history(task_id):
    from management.models import IgFollowUpTask, InstagramBotMessage
    try:
        with transaction.atomic():
            task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
            if task.status != "sent" or not task.provider_message_id:
                return False
            native, reason = capture_delivered_card(task, require_current=False)
            if native is None:
                return False
            if task.sent_message_id:
                return True
            task.sent_message = InstagramBotMessage.objects.create(client_id=task.client_id,
                sender_id=task.client.igsid, role="model", source="followup", status="done",
                text=task.message_text, provider_message_id=task.provider_message_id,
                provider_namespace=native.card["proof"]["source_namespace"], processed_at=task.sent_at)
            task.save(update_fields=["sent_message", "updated_at"])
        return True
    except Exception:
        return False


def _finish(task_id, token, receipt, *, admitted, proof, now=None):
    from management.models import IgFollowUpTask
    from management.services.instagram_bot import ProviderDeliveryReceipt
    from management.services.ig_delivery_receipts import normalize_provider_message_ids
    native = isinstance(receipt, ProviderDeliveryReceipt)
    mids = normalize_provider_message_ids(receipt.provider_message_ids) if native else ()
    from management.services.ig_delivery_plan import build_delivery_plan
    expected_count = len(build_delivery_plan(receipt.request_text).chunks) if native else 0
    ok = bool(admitted and native and receipt.ok and expected_count and len(mids) == expected_count
        and receipt.planned_chunk_count == expected_count and receipt.delivered_chunk_count == expected_count)
    with transaction.atomic():
        task = IgFollowUpTask.objects.select_for_update().get(pk=task_id)
        if task.provider_message_id:
            if (mids and (tuple((_progress(task).get("provider_message_ids") or [])) != tuple(mids) or mids[-1] != task.provider_message_id)
                    or native and receipt.request_text and receipt.request_text != task.message_text):
                task.status, task.last_error = "ambiguous", "native_receipt_identity_changed"
                task.save(update_fields=["status", "last_error", "updated_at"])
                return False
            ok = task.status == "sent"
        elif task.status == "processing" and task.claim_token == token:
            if native and receipt.request_text and receipt.request_text != task.message_text:
                ok = False
            if ok:
                task.provider_message_id, task.status, task.sent_at = mids[-1], "sent", now or timezone.now()
                _store_native_receipt(task, proof)
            elif mids or (admitted and not (native and receipt.kind == "cancelled" and not receipt.delivered_chunk_count)):
                task.status, task.last_error = "ambiguous", "provider_outcome_unknown_no_retry"
                task.provider_message_id = mids[0] if len(mids) == 1 else ""
            else:
                task.status, task.attempt_count, task.skip_reason = "pending", 0, "known_not_dispatched"
                task.next_attempt_at = (now or timezone.now()) + CHECK_INTERVAL
            task.claim_token, task.claim_until = "", None
            task.save(update_fields=["provider_message_id", "status", "sent_at", "last_error", "attempt_count", "skip_reason", "claim_token", "claim_until", "next_attempt_at", "manager_context", "updated_at"])
        else:
            ok = False
    if ok:
        _history(task_id)
    return ok


def process_due_assisted_partial_offers(settings_obj, now=None, limit=20):
    from django.db.models import Q
    from management.models import IgFollowUpTask, InstagramBotSettings, IgClient
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    from management.services import instagram_bot as bot
    from management.services.ig_reply_boundary import capture_reply_permission, customer_send_boundary
    if not getattr(settings, "IG_FULFILLMENT_REVIEWS_ENABLED", False):
        return 0
    settings_obj = settings_obj or InstagramBotSettings.load()
    fixed_clock, clock = now, now or timezone.now()
    bound = max(0, min(int(limit), 20))
    for identity in IgFollowUpTask.objects.filter(reason__in=ORIGINS, status="sent", sent_message__isnull=True).exclude(provider_message_id="").values_list("pk", flat=True)[:bound]:
        _history(identity)
    IgFollowUpTask.objects.filter(pk__in=list(IgFollowUpTask.objects.filter(reason__in=ORIGINS, status="processing", claim_until__lte=clock,
        provider_message_id="").values_list("pk", flat=True)[:bound]), status="processing", provider_message_id="").update(
        status="ambiguous", skip_reason="processing_crash_no_retry", claim_token="", claim_until=None)
    ids = list(IgFollowUpTask.objects.filter(reason__in=ORIGINS, status="pending", due_at__lte=clock)
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
            with commercial_episode_client_lock(candidate.client_id), transaction.atomic():
                IgClient.objects.select_for_update().get(pk=candidate.client_id)
                task = IgFollowUpTask.objects.select_for_update().get(pk=identity, client_id=candidate.client_id)
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
                    # The advisory prefix precedes the existing customer send
                    # boundary's Client lock. No new transaction spans HTTP;
                    # nested fresh readers reuse this same session-held lock.
                    with commercial_episode_client_lock(client.pk):
                        receipt = bot.send_text(settings_obj, client.igsid, text, return_receipt=True, allow_url_fallback=False, quick_replies=buttons,
                            provider_message_callback=lambda mid: admission["admitted"] and _checkpoint(identity, token, mid, text, proof, now=fixed_clock),
                            permission_boundary_factory=lambda: customer_send_boundary(settings_obj.pk, client.pk, permission),
                            provider_request_boundary_factory=lambda **state: _physical_boundary(identity, token, settings_obj, text, proof,
                                now=fixed_clock, admission=admission, **state))
                except Exception:
                    pass
            sent += int(_finish(identity, token, receipt, admitted=admission["admitted"], proof=proof, now=fixed_clock))
        finally:
            bot.release_client_automation_lease(client.pk, lease)
    return sent


def is_authenticated_card_source(request, source, settings_obj=None, now=None):
    """Non-mutating provenance classification, never a purchase capability.

    Used by the current request's source-head verifier to recognize the real
    native card response without recursively invoking current approval guards.
    Sealed-source acceptance still uses validate_card_choice_source above.
    """
    from management.models import IgFollowUpTask, InstagramBotMessage
    from management.services.ig_reply_boundary import capture_reply_permission
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services import ig_assisted_commerce as commerce
    now = now or timezone.now()
    row = InstagramBotMessage.objects.filter(pk=getattr(source, "message_id", None) or getattr(source, "pk", None)).first()
    parsed = _card_payload_hint(row.quick_reply_payload) if row else None
    if not parsed or not settings_obj or not commerce.verify_assisted_evidence(request, "request"):
        return False
    task = IgFollowUpTask.objects.filter(pk=parsed[0], client_id=request.client_id).first()
    native, _reason = capture_delivered_card(task, settings_obj, now=now, require_current=False)
    if native is None:
        return False
    proof, action = native.card["proof"], parsed[1]
    payload = native.card["actions"].get(action)
    title = next((button.title for button in native.card["buttons"] if button.payload == payload), None)
    permission = capture_reply_permission(settings_obj.pk, request.client_id)
    return bool(proof["request_id"] == str(request.pk) and proof["request_digest"] == request.envelope_hmac
        and row.client_id == request.client_id and row.sender_id == task.client.igsid
        and row.role == "user" and row.source == "webhook" and row.status != "failed" and row.mid
        and row.provider_created_at and not timezone.is_naive(row.provider_created_at)
        and task.sent_at <= row.provider_created_at <= now and row.pk >= conversation_route_reset_floor(request.client_id)
        and row.provider_namespace == proof["source_namespace"] and payload
        and hmac.compare_digest(row.quick_reply_payload, payload)
        and title is not None and str(row.text or "").strip() in {"", title}
        and not row.attachment_media and str(row.attachments or "").strip() in {"", "[]", "null"}
        and (not row.reply_to_provider_message_id or row.reply_to_provider_message_id == native.receipt["native_mid"])
        and permission and permission.settings_epoch == proof["settings_epoch"] and permission.client_epoch == proof["client_epoch"])
