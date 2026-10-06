"""Source-owned commerce/payment observation, independent of customer replies.

Ingress only queues accepted source identities. The worker owns media capture
and payment-review recognition; neither admission nor this lane grants a send,
invoice, fulfillment, or provider-paid capability.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
import re
import secrets

from django.conf import settings
from django.db import connection, transaction
from django.db.models import BigIntegerField, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.models import IgClient, InstagramBotMessage

VERSION = "payment-observation.v1"
LEASE_SECONDS = 180
MAX_ATTEMPTS = 5
CONTEXT_LIMIT = 80
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObservationResult:
    queued: bool = False
    observed: bool = False
    reason: str = ""
    source_id: int = 0


def _model():
    from management.models import IgPaymentObservationSource
    return IgPaymentObservationSource


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _namespace(source):
    # Native human replies require their SENT command proof, not a role label.
    from management.services.ig_memory_producer import _namespace as proved_namespace
    return proved_namespace(source)


def _source_digest(source, namespace):
    return _digest({"version": VERSION, "id": source.pk, "client": source.client_id,
        "sender": source.sender_id, "role": source.role, "source": source.source,
        "namespace": namespace, "mid": source.mid or source.provider_message_id or "",
        "text": source.text, "attachments": source.attachments,
        "event_at": (source.provider_created_at or source.created_at).isoformat(),
        "reply_to": source.reply_to_provider_message_id,
        "quick_reply": source.quick_reply_payload})


def _media_digest(source):
    # Capture progression is independent of the immutable source text/identity.
    # Inspection output is deliberately excluded: it is an observation result.
    return _digest({"private_state": source.private_media_state,
        "delete_after": source.private_media_delete_after,
        "eligible": source.media_capture_eligible,
        "parts": [{key: part.get(key) for key in (
            "source_part_id", "original_index", "type", "status", "content_hash",
            "storage_path", "storage_name", "private_storage", "mime", "delete_after", "capture_attempts", "capture_terminal",
            "capture_next_attempt_at", "capture_deadline_at")}
            for part in source.attachment_media or [] if isinstance(part, dict)]})


def _source_reason(client, source, namespace, *, reset_floor=None):
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_memory_producer import _source_allowed
    if not client or not source or source.client_id != client.pk or source.sender_id != client.igsid:
        return "source_owner_changed"
    if source.role not in {"user", "manager"} or not _source_allowed(source):
        return "source_not_accepted"
    if client.privacy_erasure_started_at:
        return "client_erasing"
    if client.hidden_at or client.is_blocked:
        return "client_observation_blocked"
    if not namespace:
        return "source_namespace_unproven"
    floor = conversation_route_reset_floor(client.pk)
    if source.pk < floor or (reset_floor is not None and floor != reset_floor):
        return "source_reset_changed"
    episode_floor = int(getattr(client.current_commercial_episode, "opened_watermark_message_id", 0) or 0)
    if source.pk < episode_floor:
        return "source_episode_changed"
    return ""


def enqueue_payment_observation(message_id, *, now=None):
    """Provider-free source admission; safe inside the ingress transaction."""
    now = now or timezone.now()
    identity = InstagramBotMessage.objects.filter(pk=message_id).values("client_id").first()
    if not identity or not identity["client_id"]:
        return ObservationResult(reason="source_missing", source_id=message_id)
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    Model = _model()
    with transaction.atomic():
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        source = InstagramBotMessage.objects.select_for_update().filter(pk=message_id).first()
        namespace = _namespace(source) if source else ""
        reason = _source_reason(client, source, namespace)
        if reason:
            return ObservationResult(reason=reason, source_id=message_id)
        digest, media_digest = _source_digest(source, namespace), _media_digest(source)
        row, created = Model.objects.select_for_update().get_or_create(message=source,
            defaults={"client": client, "source_digest": digest,
                "provider_namespace": namespace,
                "reset_floor": conversation_route_reset_floor(client.pk), "next_attempt_at": now})
        if row.source_digest != digest or row.provider_namespace != namespace:
            return ObservationResult(reason="source_digest_changed", source_id=message_id)
        if (not created and row.state in {Model.State.APPLIED, Model.State.FAILED}
            and row.observed_media_digest != media_digest):
            row.state, row.attempts, row.next_attempt_at = Model.State.PENDING, 0, now
            row.claim_token, row.lease_until = "", None
            row.save(update_fields=["state", "attempts", "next_attempt_at", "claim_token", "lease_until", "updated_at"])
        return ObservationResult(queued=True, reason="queued" if created else "already_queued", source_id=message_id)


def _cutover():
    value = getattr(settings, "IG_PAYMENT_OBSERVATION_CUTOVER_AT", None)
    if isinstance(value, str):
        try:
            value = parse_datetime(value)
        except ValueError:
            return None
    return value if value and hasattr(value, "utcoffset") and timezone.is_aware(value) else None


def reconcile_payment_observation_sources(*, limit=30, now=None):
    """Repair missed ingress queues after an explicit deployment cutover only."""
    cutover = _cutover()
    if cutover is None:
        return 0
    from management.models import IgFunnelResetAudit, InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    namespace = ingress_provider_namespace(InstagramBotSettings.load())
    if not namespace:
        return 0
    # Old/imported rows and whole-history scans are never provider work.
    rows = InstagramBotMessage.objects.filter(created_at__gte=cutover,
        client__isnull=False, client__hidden_at__isnull=True,
        client__is_blocked=False, client__privacy_erasure_started_at__isnull=True).filter(
            Q(role="user", source__in=("webhook", "poll"))
            | Q(role="manager", source__in=("echo", "manager", "manual", "human_reply")))
    rows = rows.exclude(status="failed").filter(
        Q(provider_namespace=namespace)
        | Q(source="human_reply", send_state="sent", provider_message_id__gt=""))
    reset_after = IgFunnelResetAudit.objects.filter(client_id=OuterRef("client_id"))
    rows = rows.annotate(observation_reset_after=Coalesce(Subquery(
        reset_after.order_by("-pk").values("reset_after_message_id")[:1]), Value(0), output_field=BigIntegerField()))
    rows = rows.filter(pk__gt=F("observation_reset_after")).filter(
        Q(client__current_commercial_episode__isnull=True)
        | Q(pk__gte=F("client__current_commercial_episode__opened_watermark_message_id")))
    due = rows.filter(payment_observation__isnull=True).order_by("pk")
    ids = list(due.values_list("pk", flat=True)[:max(0, limit)])
    return sum(enqueue_payment_observation(pk, now=now).queued for pk in dict.fromkeys(ids))


def _claim(source_id=None, *, now=None):
    now = now or timezone.now()
    Model = _model()
    with transaction.atomic():
        query = Model.objects.filter(
            Q(state=Model.State.PENDING, next_attempt_at__lte=now)
            | Q(state=Model.State.PROCESSING, lease_until__lte=now))
        if source_id is not None:
            query = query.filter(message_id=source_id)
        row = query.select_for_update().order_by("next_attempt_at", "message_id").first()
        if row is None:
            return None
        if row.attempts >= MAX_ATTEMPTS:
            row.state, row.last_error = Model.State.FAILED, "observation_claim_attempts_exhausted"
            row.claim_token, row.lease_until = "", None
            row.outcome = {**dict(row.outcome or {}), "version": VERSION, "receipt_disposition": "needs_manual"}
            row.save(update_fields=["state", "last_error", "claim_token", "lease_until", "outcome", "updated_at"])
            return None
        row.state, row.claim_token = Model.State.PROCESSING, secrets.token_hex(24)
        row.lease_until, row.attempts = now + timedelta(seconds=LEASE_SECONDS), row.attempts + 1
        row.save(update_fields=["state", "claim_token", "lease_until", "attempts", "updated_at"])
        return row.pk, row.claim_token


def _context(client, source, namespace):
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_memory_producer import _namespaces, _source_allowed
    floor = max(conversation_route_reset_floor(client.pk),
        int(getattr(client.current_commercial_episode, "opened_watermark_message_id", 0) or 0))
    rows = list(InstagramBotMessage.objects.filter(client=client, sender_id=client.igsid,
        pk__gte=floor, pk__lte=source.pk).exclude(status="failed").order_by("-pk")[:CONTEXT_LIMIT])
    namespaces = _namespaces(rows)
    event_at = source.provider_created_at or source.created_at
    rows = [row for row in rows if _source_allowed(row) and namespaces[row.pk] == namespace
        and (row.provider_created_at or row.created_at) <= event_at]
    rows.sort(key=lambda row: row.pk)
    return [{"id": row.pk, "mid": row.mid, "role": row.role, "text": row.text,
        "attachments": row.attachments, "attachment_media": row.attachment_media,
        "source": row.source, "provider_namespace": namespace,
        "sender_id": row.sender_id, "client_id": row.client_id,
        "status": row.status, "send_state": row.send_state,
        "provider_message_id": row.provider_message_id,
        "provider_created_at": row.provider_created_at.isoformat() if row.provider_created_at else None,
        "media_capture_eligible": row.media_capture_eligible,
        "created_at": row.created_at.isoformat()}
        for row in rows]


def _private_media_reason(source, *, now=None):
    """Existing private blobs share one expiry; metadata grants no new lease."""
    parts = [part for part in source.attachment_media or []
             if isinstance(part, dict) and part.get("private_storage") is True]
    if not parts:
        return ""
    if source.private_media_state not in {"", "active"}:
        return "receipt_media_unavailable"
    from management.services.ig_private_media import earliest_private_media_deadline
    deadline = earliest_private_media_deadline(source)
    if deadline is None:
        return "receipt_media_expiry_unknown"
    return "receipt_media_expired" if deadline <= (now or timezone.now()) else ""


def _capture_current_media(source, *, allow_provider):
    if not allow_provider or not source.media_capture_eligible or source.role != "user":
        return
    if source.private_media_state not in {"", "active"}:
        return
    if source.private_media_delete_after and source.private_media_delete_after <= timezone.now():
        return
    if _private_media_reason(source):
        return
    from management.services.ig_media_recovery import retry_due
    now = timezone.now()
    parts = [part for part in source.attachment_media or [] if isinstance(part, dict)]
    capture_due = any(part.get("status") in {"pending", "acquiring"}
        or retry_due(part, now=now) for part in parts)
    if capture_due or (source.attachments and not parts):
        from management.services.instagram_bot import _capture_message_media
        _capture_message_media(source)
        source.refresh_from_db()


def _pending_media(source, review, *, allow_provider):
    from management.services.ig_media_recovery import pending_retry_at
    now = timezone.now()
    private_reason = _private_media_reason(source, now=now)
    if private_reason:
        return private_reason, None
    if source.private_media_state not in {"", "active"} and source.attachment_media:
        return "receipt_media_unavailable", None
    parts = [part for part in source.attachment_media or [] if isinstance(part, dict)] if source.role == "user" else []
    retry_at = pending_retry_at(parts, now=now)
    if retry_at or any(part.get("status") in {"pending", "acquiring"}
        and not part.get("capture_terminal") for part in parts):
        return "media_capture_pending", retry_at
    evidence = review.evidence if review and isinstance(review.evidence, dict) else {}
    for item in [*parts, *(evidence.get("media") or [])]:
        inspection = item.get("receipt_inspection") or {} if isinstance(item, dict) else {}
        if allow_provider and inspection.get("state") == "deferred" and inspection.get("reason") == "receipt_capability_unavailable":
            return "receipt_capability_unavailable", None
        if allow_provider and (inspection.get("state") in {"pending", "failed", "retry", "retryable", "provider_failed"}
            or (inspection.get("state") == "deferred" and inspection.get("reason") in {
                "receipt_inspection_failed", "receipt_observation_missing", "receipt_image_busy", "receipt_image_budget",
                "receipt_provider_failed", "receipt_quota_unavailable", "receipt_source_changed",
                "receipt_inspection_not_persisted", "receipt_observation_unbound"})):
            retry_at = parse_datetime(str(inspection.get("retry_at") or ""))
            if retry_at is None or timezone.is_naive(retry_at) or retry_at <= now:
                retry_at = now + timedelta(seconds=900) if inspection.get("reason") == "receipt_quota_unavailable" else None
            return "receipt_recognition_pending", retry_at
    if (allow_provider and source.role == "user" and source.media_capture_eligible
        and source.private_media_state in {"", "active"}
        and (not source.private_media_delete_after or source.private_media_delete_after > now)
        and any(part.get("status") == "owned" and part.get("private_storage") is True
            and str(part.get("mime") or "").startswith("image/")
            and part.get("content_hash") and part.get("source_part_id")
            and not part.get("receipt_inspection") for part in parts)):
        # A busy image lease may prevent even a deferred marker from being
        # persisted. Keep the source due until an actual observation exists.
        return "receipt_recognition_pending", None
    return "", None


def _finish(row_id, token, *, reason="", outcome=None, media_digest="", retry_at=None, blocked=False):
    Model = _model()
    with transaction.atomic():
        row = Model.objects.select_for_update().filter(pk=row_id,
            state=Model.State.PROCESSING, claim_token=token, lease_until__gt=timezone.now()).first()
        if row is None:
            return ObservationResult(reason="observation_claim_lost")
        retry = bool(reason and not blocked and row.attempts < MAX_ATTEMPTS)
        row.state = (Model.State.BLOCKED if blocked else Model.State.PENDING if retry
            else Model.State.FAILED if reason else Model.State.APPLIED)
        row.next_attempt_at = retry_at or timezone.now() + timedelta(seconds=min(900, 30 * 2 ** min(row.attempts, 5)))
        row.last_error, row.outcome = reason[:120], {"version": VERSION, **(outcome or {})}
        row.observed_media_digest = media_digest
        row.observed_at = timezone.now() if not reason else None
        row.claim_token, row.lease_until = "", None
        row.save(update_fields=["state", "next_attempt_at", "last_error", "outcome",
            "observed_media_digest", "observed_at", "claim_token", "lease_until", "updated_at"])
        return ObservationResult(observed=not reason, reason=reason or "observed", source_id=row.message_id)


def _observe_claim(row_id, token, *, allow_provider=True):
    Model = _model()
    row = Model.objects.select_related("message", "client").filter(pk=row_id).first()
    if row is None:
        return ObservationResult(reason="observation_source_erased")
    if row.state != Model.State.PROCESSING or row.claim_token != token or not row.lease_until or row.lease_until <= timezone.now():
        return ObservationResult(reason="observation_claim_lost", source_id=row.message_id)
    from management.services.ig_commercial_episodes import commercial_episode_client_lock
    try:
        with commercial_episode_client_lock(row.client_id):
            admission = _claim_current(row_id, token)
            if admission is not True:
                return _finish(row_id, token, reason=admission, blocked=True)
            source = InstagramBotMessage.objects.get(pk=row.message_id)
            client = IgClient.objects.get(pk=row.client_id)
            namespace = _namespace(source)
            reason = _source_reason(client, source, namespace, reset_floor=row.reset_floor)
            if not reason and (namespace != row.provider_namespace or _source_digest(source, namespace) != row.source_digest):
                reason = "source_digest_changed"
            if reason:
                return _finish(row_id, token, reason=reason, blocked=True)
            from management.models import InstagramBotSettings
            from management.services.instagram_bot import ingress_provider_namespace
            if namespace != ingress_provider_namespace(InstagramBotSettings.load()):
                return _finish(row_id, token, reason="provider_namespace_changed", blocked=True)
            _capture_current_media(source, allow_provider=allow_provider)
            admission = _claim_current(row_id, token)
            if admission is not True:
                return _finish(row_id, token, reason=admission, blocked=True)
            messages = _context(client, source, namespace)
            from management.services.ig_conversation_agreement import persist_conversation_agreement
            agreement = persist_conversation_agreement(client, messages, watermark=source.pk)
            client.refresh_from_db()
            admission = _claim_current(row_id, token)
            if admission is not True:
                return _finish(row_id, token, reason=admission, blocked=True)
            from management.services.ig_payment_review import create_payment_review
            recognition_allowed = bool(allow_provider and source.role == "user"
                and not _private_media_reason(source))
            review = create_payment_review(client, watermark=source.pk,
                messages=messages, allow_provider=recognition_allowed,
                pre_dispatch_guard=lambda: _claim_current(row_id, token))
            manager_confirmation = None
            if source.role == "manager":
                admission = _claim_current(row_id, token)
                if admission is not True:
                    return _finish(row_id, token, reason=admission, blocked=True)
                from management.services.ig_payment_review import apply_authenticated_manager_payment_confirmation
                manager_confirmation = apply_authenticated_manager_payment_confirmation(
                    source.pk, observation_guard=lambda: _claim_current(row_id, token))
                if manager_confirmation is not None:
                    review = manager_confirmation
            source.refresh_from_db()
            client.refresh_from_db()
            reason = _source_reason(client, source, _namespace(source), reset_floor=row.reset_floor)
            if not reason and _source_digest(source, namespace) != row.source_digest:
                reason = "source_digest_changed"
            if reason:
                return _finish(row_id, token, reason=reason, blocked=True)
            pending, retry_at = _pending_media(source, review, allow_provider=recognition_allowed)
            manual_blocked = pending in {"receipt_capability_unavailable", "receipt_media_expiry_unknown",
                "receipt_media_expired", "receipt_media_unavailable"}
            return _finish(row_id, token, reason=pending, retry_at=retry_at,
                blocked=manual_blocked,
                media_digest=_media_digest(source), outcome={"source_message_id": source.pk,
                    "watermark_message_id": source.pk, "review_id": review.pk if review else None,
                    "manager_payment_review_id": manager_confirmation.pk if manager_confirmation else None,
                    "receipt_disposition": "needs_manual" if manual_blocked else "pending" if pending else "observed",
                    "agreement_observed": bool(agreement.get("persisted")),
                    "agreement_reason": str(agreement.get("reason") or "")[:120],
                    "recognition_allowed": recognition_allowed,
                    "allow_provider": bool(allow_provider)})
    except Exception as exc:
        logger.warning("payment_observation_failed source=%s error=%s", row.message_id, type(exc).__name__)
        return _finish(row_id, token, reason="observation_" + type(exc).__name__,
            media_digest=_media_digest(row.message))


def _claim_current(row_id, token):
    row = _model().objects.select_related("message", "client").filter(pk=row_id,
        state="processing", claim_token=token, lease_until__gt=timezone.now()).first()
    if row is None:
        return "observation_claim_lost"
    source, client = row.message, row.client
    namespace = _namespace(source)
    reason = _source_reason(client, source, namespace, reset_floor=row.reset_floor)
    if reason:
        return reason
    if namespace != row.provider_namespace or _source_digest(source, namespace) != row.source_digest:
        return "observation_source_changed"
    from management.models import InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    return True if namespace == ingress_provider_namespace(InstagramBotSettings.load()) else "provider_namespace_changed"


def observe_payment_source(message_id, *, allow_provider=True, now=None):
    if connection.in_atomic_block:
        return ObservationResult(reason="caller_transaction_active", source_id=message_id)
    queued = enqueue_payment_observation(message_id, now=now)
    if not queued.queued:
        return queued
    if allow_provider:
        # Explicit provider-free replay can later admit recognition without
        # confusing a local observation receipt with successful inspection.
        Model = _model()
        Model.objects.filter(message_id=message_id, state=Model.State.APPLIED,
            outcome__allow_provider=False).update(state=Model.State.PENDING,
                attempts=0, next_attempt_at=now or timezone.now())
    claim = _claim(message_id, now=now)
    return _observe_claim(*claim, allow_provider=allow_provider) if claim else ObservationResult(reason="not_due", source_id=message_id)


def drain_payment_observations(*, max_items=5, allow_provider=True, adopt=True):
    """Worker-only bounded drain. Never checks reply permissions or sends DM."""
    if connection.in_atomic_block:
        return 0
    bounded = max(0, min(int(max_items), 30))
    if adopt:
        reconcile_payment_observation_sources(limit=bounded)
    observed = 0
    for _ in range(bounded):
        claim = _claim()
        if claim is None:
            break
        observed += _observe_claim(*claim, allow_provider=allow_provider).observed
    return observed


def _manual_review_receipts(source, evidence, review_id, *, namespace, now):
    """Project bound document/link evidence as unreadable; never fetch it."""
    normalized = " ".join(str(source.text or "").split())[:300]
    proofs = [row for row in evidence.get("messages") or [] if isinstance(row, dict)
        and row.get("message_id") == source.pk and row.get("role") in {"user", "customer", "client"}
        and " ".join(str(row.get("quote") or "").split()) == normalized]
    if not proofs:
        return []
    parts = [row for row in source.attachment_media or [] if isinstance(row, dict)]
    try:
        attached_urls = json.loads(source.attachments or "[]")
    except (TypeError, ValueError):
        attached_urls = []
    attached_urls = attached_urls if isinstance(attached_urls, list) else []
    results = []
    for item in (evidence.get("media") or [])[:128]:
        if not isinstance(item, dict) or (item.get("source_message_id") or item.get("message_id")) != source.pk:
            continue
        kind = str(item.get("media_type") or item.get("type") or "").casefold()
        if item.get("mime") == "application/pdf":
            kind = "document"
        if kind not in {"file", "document", "link", "receipt_link"} or item.get("role") not in {"receipt", "payment_candidate"}:
            continue
        if item.get("source_digest") and item["source_digest"] != _source_digest(source, namespace):
            continue
        url, part = str(item.get("url") or ""), None
        if kind in {"link", "receipt_link"}:
            if not url or url not in re.findall(r"https?://[^\s<>]+", source.text or ""):
                continue
            reason = "receipt_link_not_fetched"
        else:
            matching = [row for row in parts if (
                item.get("source_part_id") and row.get("source_part_id") == item["source_part_id"]
            ) or (not item.get("source_part_id") and url and row.get("url") == url)]
            if len(matching) == 1:
                part = matching[0]
                if part.get("status") == "expired" or source.private_media_state not in {"", "active"}:
                    continue
                if (item.get("private_storage") is True or part.get("private_storage") is True) and _private_media_reason(source, now=now):
                    continue
                deadline = parse_datetime(str(part.get("delete_after") or ""))
                if part.get("delete_after") and (deadline is None or timezone.is_naive(deadline) or deadline <= now):
                    continue
                if item.get("content_hash") and item["content_hash"] != part.get("content_hash"):
                    continue
                if item.get("private_storage") is True and (
                    part.get("private_storage") is not True or part.get("status") != "owned"
                    or item.get("storage_name") != part.get("storage_name")):
                    continue
            elif item.get("private_storage") is True or not url or url not in attached_urls:
                continue
            reason = "receipt_document_not_readable"
        results.append({"schema_version": "ig-receipt-manual-review-v1", "state": "unreadable",
            "role": "payment_candidate", "confidence": 0.0, "source_message_id": source.pk,
            "source_part_id": str((part or {}).get("source_part_id") or ""),
            "content_hash": str((part or {}).get("content_hash") or ""),
            "receipt_facts": {}, "uncertainties": [reason], "reason": reason,
            "review_id": review_id, "media_type": kind, "payment_verified": False})
    return results[:8]


def read_receipt_observation(client, *, episode_id, source_namespace, reset_floor, watermark, now=None):
    """Read OCR evidence for a caller's freshly captured client/scope.

    The captured-state adapter supplies fresh clients on both validation reads;
    this function performs no queue writes, capture, or provider I/O.
    """
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_receipt_inspection import bound_receipt_inspection
    now = now or timezone.now()
    empty = {"observation": {"state": "unavailable", "source_message_ids": [], "receipts": []},
        "source_refs": [], "source_rows": [], "reason": "receipt_observation_scope_changed"}
    if (client is None or client.hidden_at or client.is_blocked or client.privacy_erasure_started_at
        or client.current_commercial_episode_id != episode_id or not source_namespace
        or conversation_route_reset_floor(client.pk) != int(reset_floor or 0)):
        return empty
    watermark_id = (watermark or {}).get("message_id")
    event_at = parse_datetime(str((watermark or {}).get("event_at") or ""))
    if type(watermark_id) is not int or watermark_id < int(reset_floor or 0) or not event_at or timezone.is_naive(event_at):
        return empty
    candidates = InstagramBotMessage.objects.filter(client=client, sender_id=client.igsid,
        role="user", source__in=("webhook", "poll"), pk__gte=int(reset_floor or 0),
        pk__lte=watermark_id, provider_namespace=source_namespace).exclude(status="failed")
    priority_ids = []
    evidence, review_id = {}, None
    episode = client.current_commercial_episode
    if episode is not None and episode.primary_payment_review_id:
        from management.models import IgPaymentConfirmationReview
        review = IgPaymentConfirmationReview.objects.filter(pk=episode.primary_payment_review_id,
            client_id=client.pk, status="pending").values("pk", "evidence").first()
        review_id = review["pk"] if review else None
        evidence = review.get("evidence") or {} if review else {}
        evidence = evidence if isinstance(evidence, dict) else {}
        for item in evidence.get("media") or []:
            if not isinstance(item, dict) or item.get("role") not in {"receipt", "payment_candidate"}:
                continue
            pk = item.get("source_message_id") or item.get("message_id")
            if type(pk) is int and pk > 0 and pk not in priority_ids:
                priority_ids.append(pk)
            if len(priority_ids) >= 8:
                break
    priority = list(candidates.filter(pk__in=priority_ids).order_by("-pk")) if priority_ids else []
    recent = list(candidates.exclude(attachment_media=[]).exclude(attachment_media__isnull=True)
        .order_by("-pk")[:CONTEXT_LIMIT + 1])
    candidate_window_limited = len(recent) > CONTEXT_LIMIT
    # An actual episode review anchors its receipt while ordinary conversation
    # and unrelated media advance. Every anchored row still passes the same
    # source/time/namespace/reset/private-part proof below.
    selected = {}
    for source in [*priority, *recent]:
        if source.pk not in selected and len(selected) < CONTEXT_LIMIT:
            selected[source.pk] = source
    rows = sorted(selected.values(), key=lambda row: row.pk, reverse=True)
    rows = [row for row in rows if row.attachment_media or row.pk in priority_ids]
    if not rows:
        return {"observation": {"state": "absent", "source_message_ids": [], "receipts": [],
            "pending_sources": False, "needs_manual": False, "payment_verified": False},
            "source_refs": [], "source_rows": [], "reason": "receipt_observation_absent"}
    receipts, infos = [], {}
    Model = _model()
    jobs = {row.message_id: row for row in Model.objects.filter(message_id__in=[row.pk for row in rows])}
    episode_floor = int(getattr(client.current_commercial_episode, "opened_watermark_message_id", 0) or 0)
    for source in reversed(rows):
        if source.pk < episode_floor or (source.provider_created_at or source.created_at) > event_at:
            continue
        private_reason = _private_media_reason(source, now=now)
        digest, job = _source_digest(source, source_namespace), jobs.get(source.pk)
        if job is not None and (job.source_digest != digest or job.provider_namespace != source_namespace
            or job.reset_floor != reset_floor or (job.state == Model.State.BLOCKED
                and job.last_error not in {"receipt_capability_unavailable", "receipt_media_expiry_unknown",
                    "receipt_media_expired", "receipt_media_unavailable"})):
            continue
        relevant, source_pending, source_manual = False, False, False
        for part in source.attachment_media or []:
            if not isinstance(part, dict):
                continue
            if source.private_media_state not in {"", "active"} or part.get("status") == "expired":
                continue
            if part.get("private_storage") is True and private_reason:
                continue
            part_deadline = parse_datetime(str(part.get("delete_after") or ""))
            if part.get("delete_after") and (part_deadline is None or timezone.is_naive(part_deadline) or part_deadline <= now):
                continue
            bound = bound_receipt_inspection({**part, "message_id": source.pk})
            if bound is not None and bound["role"] in {"receipt", "payment_candidate"}:
                if part.get("status") != "owned" or part.get("private_storage") is not True:
                    continue
                receipts.append(bound)
                relevant = True
                if bound.get("state") != "inspected" or bound.get("confidence", 0) < 0.75:
                    source_manual = True
            elif part.get("role") in {"receipt", "payment_candidate"} or part.get("payment_evidence"):
                relevant, source_pending = True, True
            else:
                deferred = part.get("receipt_inspection") or {}
                if (deferred.get("schema_version") == "ig-receipt-inspection-v1"
                    and deferred.get("state") == "deferred"
                    and deferred.get("source_message_id") == source.pk
                    and deferred.get("source_part_id") == part.get("source_part_id")
                    and deferred.get("content_hash") == part.get("content_hash")):
                    relevant, source_pending = True, True
            if relevant and job and (job.state == Model.State.FAILED or job.last_error == "receipt_capability_unavailable"):
                source_manual = True
        manual = _manual_review_receipts(source, evidence, review_id, namespace=source_namespace, now=now) if review_id else []
        if manual:
            receipts.extend(manual)
            relevant, source_manual = True, True
        if relevant:
            infos[source.pk] = {"ref": {"message_id": source.pk, "source_digest": digest},
                "row": {"message_id": source.pk, "source_digest": digest,
                "media_digest": _media_digest(source), "event_at": (source.provider_created_at or source.created_at).isoformat(),
                "observation_state": job.state if job else "pending"},
                "pending": source_pending, "needs_manual": source_manual}
    receipts.sort(key=lambda row: (row["source_message_id"] not in priority_ids, -row["source_message_id"]))
    selected_receipts = receipts[:8]
    visible_ids = list(dict.fromkeys([
        *[pk for pk in priority_ids if pk in infos],
        *[row["source_message_id"] for row in selected_receipts],
        *sorted((pk for pk, info in infos.items() if info["pending"] or info["needs_manual"]), reverse=True),
    ]))[:32]
    refs = [infos[pk]["ref"] for pk in sorted(visible_ids)]
    source_rows = [infos[pk]["row"] for pk in sorted(visible_ids)]
    omitted_ids = sorted(set(infos) - set(visible_ids))
    pending = any(infos[pk]["pending"] for pk in visible_ids)
    needs_manual = any(infos[pk]["needs_manual"] for pk in visible_ids)
    confidently_observed = any(row.get("state") == "inspected" and row.get("confidence", 0) >= 0.75 for row in selected_receipts)
    observation = {"state": "observed" if confidently_observed else "unreadable" if needs_manual else "pending" if pending else "absent",
        "source_message_ids": [row["message_id"] for row in refs], "receipts": selected_receipts,
        "pending_sources": pending, "needs_manual": needs_manual, "payment_verified": False,
        "omitted_source_message_ids": omitted_ids, "omitted_source_count": len(omitted_ids),
        "omitted_receipt_count": len(receipts) - len(selected_receipts),
        "media_source_window_limited": candidate_window_limited}
    return {"observation": observation, "source_refs": refs, "source_rows": source_rows,
        "reason": "" if receipts or pending else "receipt_observation_absent"}
