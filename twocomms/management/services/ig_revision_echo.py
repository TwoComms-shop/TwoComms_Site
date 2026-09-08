"""Exact deferred echo attribution using durable revision provider receipts.

No text resemblance, automation-busy heuristic, download or provider call grants
attribution. Manager projection is staged by the integration owner; this module
only validates its exact persisted message/job proof before acknowledging it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    IgBotNotification, IgClient, IgDeferredEcho, IgPermissionTransitionJob,
    IgRevisionDeliveryEffect, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_revision_outbox import _canonical, _digest


MAX_UNRESOLVED = 32
MAX_RETAINED = 128
MAX_COMPETING = 32
WAIT_REVIEW_AFTER = timedelta(seconds=90)
BLOCKING_STATES = ("waiting_receipt", "ambiguous", "manager_pending")
_NAMESPACE = re.compile(r"(?:instagram_login|legacy_page):[A-Za-z0-9_.-]{1,96}")
_RECIPIENT = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,31}")


@dataclass(frozen=True)
class EchoAttribution:
    accepted: bool = False
    classification: str = "blocked"
    event_id: int = 0
    effect_id: int = 0
    manager_message_id: int = 0
    permission_transition_id: int = 0
    reason: str = ""
    replayed: bool = False
    retryable: bool = False


class _Blocked(Exception):
    pass


def _payload(text, attachments):
    if not isinstance(text, str) or len(text) > 4000:
        raise _Blocked("echo_text_invalid")
    if attachments is None:
        attachments = []
    if not isinstance(attachments, (list, tuple)) or len(attachments) > 8:
        raise _Blocked("echo_media_invalid")
    media = []
    for item in attachments:
        if not isinstance(item, dict):
            raise _Blocked("echo_media_invalid")
        url = item.get("url")
        kind = str(item.get("type") or "image").casefold()
        title = item.get("title") or ""
        if not isinstance(url, str) or len(url) > 1200 or not isinstance(title, str) or len(title) > 700 or not _CODE.fullmatch(kind):
            raise _Blocked("echo_media_invalid")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise _Blocked("echo_media_invalid")
        media.append({"url": url, "type": kind, "title": title, "role": "manager_reference"})
    if not text and not media:
        raise _Blocked("echo_empty")
    return {"text": text, "attachments": media}


def _lock_scope(settings_id, namespace, *, recipient=None, client_id=None):
    from management.services.instagram_bot import ingress_provider_namespace

    settings_row = InstagramBotSettings.objects.select_for_update().filter(pk=settings_id).first()
    if settings_row is None or ingress_provider_namespace(settings_row) != namespace:
        raise _Blocked("echo_namespace_mismatch")
    clients = IgClient.objects.select_for_update()
    client = clients.filter(pk=client_id).first() if client_id is not None else clients.filter(igsid=recipient).first()
    if client is None:
        raise _Blocked("echo_client_missing")
    if client.privacy_erasure_started_at is not None:
        raise _Blocked("echo_erasure_active")
    if recipient is not None and client.igsid != recipient:
        raise _Blocked("echo_recipient_mismatch")
    return settings_row, client


def _effects(client, namespace, recipient):
    return IgRevisionDeliveryEffect.objects.filter(
        revision__client_id=client.pk, provider_namespace=namespace,
        recipient_igsid=recipient,
    )


def _own_effect(client, namespace, recipient, mid):
    return _effects(client, namespace, recipient).filter(
        state=IgRevisionDeliveryEffect.State.SENT,
        provider_message_id=mid,
    ).order_by("pk").first()


def _foreign_receipt(client, namespace, recipient, mid):
    return IgRevisionDeliveryEffect.objects.filter(
        provider_namespace=namespace, provider_message_id=mid, state="sent",
    ).exclude(revision__client_id=client.pk, recipient_igsid=recipient).exists()


def _manager_proof(client, namespace, recipient, mid, *, message_id=None, job_id=None, settings_id=None, historical=False):
    messages = InstagramBotMessage.objects.filter(
        mid=mid, provider_namespace=namespace, sender_id=recipient,
        role=InstagramBotMessage.Role.MANAGER, source="poll_history" if historical else "echo",
    ).filter(Q(client=client) | Q(client__isnull=True))
    if historical:
        messages = messages.filter(client=client)
    if message_id is not None:
        messages = messages.filter(pk=message_id)
    message = messages.first()
    if message is None:
        return None, None
    if historical:
        return message, None
    jobs = IgPermissionTransitionJob.objects.filter(
        kind=IgPermissionTransitionJob.Kind.MANAGER_TAKEOVER,
        client=client, source_message=message,
    )
    if settings_id is not None:
        jobs = jobs.filter(settings_id=settings_id)
    if job_id is not None:
        jobs = jobs.filter(pk=job_id)
    job = jobs.order_by("pk").first()
    return (message, job) if job is not None else (None, None)


def _result(event, *, replayed=False):
    return EchoAttribution(
        True, event.state, event.pk, event.matched_effect_id or 0,
        event.manager_message_id or 0, event.permission_transition_id or 0,
        event.reason, replayed,
    )


def _technical_case(event, client, now):
    from management.services.instagram_bot import notify_manager
    from management.services.ig_alerts import format_technical_alert

    if event.notification_id:
        return
    scope = IgDeferredEcho.objects.filter(client=client, provider_namespace=event.provider_namespace, state__in=BLOCKING_STATES)
    existing_notice = scope.exclude(notification_id__isnull=True).order_by("observed_at", "id").values_list("notification_id", flat=True).first()
    if existing_notice:
        event.notification_id = existing_notice
    else:
        oldest = scope.order_by("observed_at", "id").values_list("pk", flat=True).first() or event.pk
        key = f"revision-echo-unresolved:{client.pk}:{_digest(event.provider_namespace)[:16]}:{oldest}"
        if not notify_manager(
            format_technical_alert(
                "Не вдалося визначити автора повідомлення",
                event_type="revision_echo_unresolved", client_id=client.pk,
                failure_kind=event.reason or "echo_queue_limit",
                counts={"echo_event_id": oldest, "unresolved_echoes": scope.count()},
                instruction_code="revision_echo_unresolved",
            ),
            dedupe_key=key, event_type="revision_echo_unresolved", client=client,
            metadata={"client_id": client.pk, "echo_event_id": oldest, "namespace_digest": _digest(event.provider_namespace), "requires_human_review": True},
            deliver_immediately=False, raise_on_error=True,
        ):
            raise _Blocked("echo_notification_unavailable")
        event.notification_id = IgBotNotification.objects.get(dedupe_key=key, client=client).pk
    event.save(update_fields=["notification", "updated_at"])


def _set_state(event, state, reason, now, *, matched_effect=None):
    changed = event.state != state or event.reason != reason or (matched_effect is not None and event.matched_effect_id != matched_effect.pk)
    if not changed:
        return
    event.state, event.reason = state, reason
    if matched_effect is not None:
        event.matched_effect = matched_effect
    if state == IgDeferredEcho.State.OWN:
        event.resolved_at = now
    event.save(update_fields=["state", "reason", "matched_effect", "resolved_at", "updated_at"])


def _reconcile_event(event, client, now):
    if event.state in {event.State.OWN, event.State.MANAGER_PENDING, event.State.MANAGER_APPLIED}:
        return event
    if _foreign_receipt(client, event.provider_namespace, event.recipient_igsid, event.provider_message_id):
        _set_state(event, event.State.AMBIGUOUS, "echo_receipt_identity_mismatch", now)
        _technical_case(event, client, now)
        return event
    exact = _own_effect(client, event.provider_namespace, event.recipient_igsid, event.provider_message_id)
    if exact is not None:
        _set_state(event, event.State.OWN, "exact_provider_receipt", now, matched_effect=exact)
        return event
    rows = list(_effects(client, event.provider_namespace, event.recipient_igsid).filter(pk__in=event.competing_effect_ids))
    # Read MID together with state: finish_effect need not take the client lock.
    # A receipt committed after the first lookup must not become a manager echo.
    exact = next((row for row in rows if row.state == row.State.SENT and row.provider_message_id == event.provider_message_id), None)
    if exact is not None:
        _set_state(event, event.State.OWN, "exact_provider_receipt", now, matched_effect=exact)
        return event
    missing = len(rows) != len(event.competing_effect_ids)
    unknown = any(row.state == row.State.UNKNOWN for row in rows)
    inflight = any(row.state in {row.State.PROVIDER_STARTED, row.State.CLAIMED, row.State.PLANNED} for row in rows)
    if event.candidate_overflow or missing or unknown:
        reason = "echo_competing_limit" if event.candidate_overflow else "echo_effect_evidence_missing" if missing else "echo_provider_result_unknown"
        _set_state(event, event.State.AMBIGUOUS, reason, now)
        _technical_case(event, client, now)
    elif inflight:
        if event.state != event.State.AMBIGUOUS:
            _set_state(event, event.State.WAITING_RECEIPT, "echo_waiting_receipt", now)
        if event.observed_at + WAIT_REVIEW_AFTER <= now:
            _technical_case(event, client, now)
    else:
        _set_state(event, event.State.MANAGER_PENDING, "competing_calls_settled", now)
    return event


def _prune_confirmed(scope, client):
    """Prune only when another durable exact proof retains replay identity."""
    candidates = list(scope.filter(state__in=("own", "manager_applied")).order_by("observed_at", "pk")[:MAX_UNRESOLVED])
    for event in candidates:
        if event.state == event.State.OWN:
            proof = _effects(client, event.provider_namespace, event.recipient_igsid).filter(pk=event.matched_effect_id, state="sent", provider_message_id=event.provider_message_id).exists()
        else:
            historical = event.payload.get("historical") is True
            message, job = _manager_proof(client, event.provider_namespace, event.recipient_igsid, event.provider_message_id, message_id=event.manager_message_id, job_id=event.permission_transition_id, settings_id=event.settings_id_snapshot, historical=historical)
            proof = message is not None and (historical or job is not None) and _projection_matches(event, message, historical=historical)
        if proof:
            event.delete()
            return True
    return False


def observe_revision_echo(
    *, settings_id, namespace, recipient, mid, text="", attachments=None,
    received_at=None, historical=False, now=None,
):
    """Durably accept one authenticated echo or explicitly request ingress retry."""
    now = now or timezone.now()
    try:
        if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace) or not isinstance(recipient, str) or not _RECIPIENT.fullmatch(recipient):
            raise _Blocked("echo_scope_invalid")
        if not isinstance(mid, str) or not 1 <= len(mid) <= 255 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in mid):
            raise _Blocked("echo_mid_invalid")
        if received_at is not None and (not hasattr(received_at, "tzinfo") or timezone.is_naive(received_at)):
            raise _Blocked("echo_timestamp_invalid")
        if type(historical) is not bool:
            raise _Blocked("echo_historical_mode_invalid")
        payload = {**_payload(text, attachments), "historical": historical}
        material = {"provider_namespace": namespace, "recipient_igsid": recipient, "provider_message_id": mid, "payload": payload, "provider_created_at": received_at.isoformat() if received_at else ""}
        if len(_canonical(material)) > 32 * 1024:
            raise _Blocked("echo_payload_too_large")
        digest = _digest(material)
        with transaction.atomic():
            _settings, client = _lock_scope(settings_id, namespace, recipient=recipient)
            event = IgDeferredEcho.objects.select_for_update().filter(provider_namespace=namespace, provider_message_id=mid).first()
            if event is not None:
                if event.client_id != client.pk or event.recipient_igsid != recipient:
                    raise _Blocked("echo_identity_conflict")
                # First captured metadata is immutable. Poll/CDN URL refreshes
                # for the same exact MID do not overwrite or reapply that event.
                return _result(_reconcile_event(event, client, now), replayed=True)
            if _foreign_receipt(client, namespace, recipient, mid):
                raise _Blocked("echo_receipt_identity_mismatch")
            exact = _own_effect(client, namespace, recipient, mid)
            if exact is not None:
                return EchoAttribution(True, "own", effect_id=exact.pk, reason="exact_provider_receipt")
            message, job = _manager_proof(client, namespace, recipient, mid, settings_id=settings_id)
            if message is None:
                message, job = _manager_proof(client, namespace, recipient, mid, settings_id=settings_id, historical=True)
            if message is not None:
                return EchoAttribution(True, "manager_applied", manager_message_id=message.pk, permission_transition_id=job.pk if job else 0, reason="exact_manager_projection" if job else "exact_historical_projection", replayed=True)
            scope = IgDeferredEcho.objects.filter(client=client, provider_namespace=namespace)
            if scope.filter(state__in=BLOCKING_STATES).count() >= MAX_UNRESOLVED:
                oldest = scope.filter(state__in=BLOCKING_STATES).order_by("observed_at", "pk").first()
                if oldest is not None:
                    _technical_case(oldest, client, now)
                return EchoAttribution(reason="echo_queue_limit", retryable=True)
            if scope.count() >= MAX_RETAINED and not _prune_confirmed(scope, client):
                return EchoAttribution(reason="echo_queue_limit", retryable=True)
            competing = list(_effects(client, namespace, recipient).filter(state__in=("provider_started", "unknown")).order_by("pk").values_list("pk", flat=True)[:MAX_COMPETING + 1])
            # Receipt completion can race the two preceding reads. Recheck after
            # an empty competing set before declaring this an unmatched manager.
            if not competing:
                exact = _own_effect(client, namespace, recipient, mid)
                if exact is not None:
                    return EchoAttribution(True, "own", effect_id=exact.pk, reason="exact_provider_receipt")
            event = IgDeferredEcho.objects.create(
                client=client, settings_id_snapshot=settings_id,
                provider_namespace=namespace, recipient_igsid=recipient, provider_message_id=mid,
                payload=payload, event_digest=digest, provider_created_at=received_at,
                observed_at=now, competing_effect_ids=competing[:MAX_COMPETING],
                candidate_overflow=len(competing) > MAX_COMPETING,
            )
            return _result(_reconcile_event(event, client, now))
    except _Blocked as exc:
        return EchoAttribution(reason=str(exc), retryable=str(exc) == "echo_notification_unavailable")
    except Exception:
        return EchoAttribution(reason="echo_observation_failed", retryable=True)


def revision_echo_blocks(client_id, namespace):
    """Read-only final-CAS hook; missing/invalid scope never grants a send."""
    if not client_id or not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        return True
    rows = list(IgDeferredEcho.objects.filter(
        client_id=client_id, provider_namespace=namespace, state__in=BLOCKING_STATES,
    ).values_list("state", "payload__historical")[:MAX_RETAINED + 1])
    return len(rows) > MAX_RETAINED or any(state != "manager_pending" or historical is not True for state, historical in rows)


def reconcile_revision_echoes(*, settings_id, client_id, namespace, limit=MAX_UNRESOLVED, now=None):
    now = now or timezone.now()
    try:
        with transaction.atomic():
            _settings, client = _lock_scope(settings_id, namespace, client_id=client_id)
            events = list(IgDeferredEcho.objects.select_for_update().filter(client=client, provider_namespace=namespace, recipient_igsid=client.igsid, state__in=BLOCKING_STATES).order_by("observed_at", "pk")[:max(1, min(int(limit), MAX_UNRESOLVED))])
            return tuple(_result(_reconcile_event(event, client, now)) for event in events)
    except _Blocked as exc:
        return (EchoAttribution(reason=str(exc)),)
    except Exception:
        return (EchoAttribution(reason="echo_reconciliation_failed", retryable=True),)


def pending_manager_echoes(*, client_id, namespace, limit=MAX_UNRESOLVED):
    """Exact payloads for the trusted DB-only manager projection integration."""
    return tuple({
        "event_id": event.pk, "settings_id": event.settings_id_snapshot,
        "namespace": event.provider_namespace, "recipient": event.recipient_igsid,
        "mid": event.provider_message_id, "text": event.payload.get("text") or "",
        "attachments": event.payload.get("attachments") or [],
        "received_at": event.provider_created_at, "historical": event.payload.get("historical") is True,
        "provenance": {"kind": "reconciled_revision_echo", "event_id": event.pk, "event_digest": event.event_digest},
    } for event in IgDeferredEcho.objects.filter(
        client_id=client_id, provider_namespace=namespace, state="manager_pending",
        client__privacy_erasure_started_at__isnull=True,
    ).order_by("observed_at", "pk")[:max(1, min(int(limit), MAX_UNRESOLVED))])


def _projection_matches(event, message, *, historical=False):
    import json

    expected_text = event.payload.get("text") or ""
    expected_urls = [item["url"] for item in event.payload.get("attachments") or ()]
    try:
        stored_urls = json.loads(message.attachments) if message.attachments else []
    except (TypeError, ValueError):
        return False
    texts = {expected_text} if historical or expected_text else {"", "(зображення менеджера)"}
    return message.text in texts and stored_urls == expected_urls


def acknowledge_manager_echo(*, event_id, settings_id, manager_message_id, permission_transition_id, now=None):
    """Acknowledge exact staged message+job, never invoke a callback or transition."""
    now = now or timezone.now()
    identity = IgDeferredEcho.objects.filter(pk=event_id).values("client_id", "provider_namespace", "recipient_igsid").first()
    if identity is None:
        return EchoAttribution(reason="echo_event_missing")
    try:
        with transaction.atomic():
            _settings, client = _lock_scope(settings_id, identity["provider_namespace"], client_id=identity["client_id"], recipient=identity["recipient_igsid"])
            event = IgDeferredEcho.objects.select_for_update().get(pk=event_id)
            if event.settings_id_snapshot != settings_id:
                raise _Blocked("echo_settings_changed")
            if event.payload.get("historical") is True:
                raise _Blocked("echo_historical_projection_required")
            if event.state == event.State.MANAGER_APPLIED:
                if event.manager_message_id != manager_message_id or event.permission_transition_id != permission_transition_id:
                    raise _Blocked("echo_manager_projection_mismatch")
                return _result(event, replayed=True)
            if event.state != event.State.MANAGER_PENDING:
                raise _Blocked("echo_manager_not_ready")
            message, job = _manager_proof(client, event.provider_namespace, event.recipient_igsid, event.provider_message_id, message_id=manager_message_id, job_id=permission_transition_id, settings_id=settings_id)
            if message is None or job is None:
                raise _Blocked("echo_manager_projection_mismatch")
            if not _projection_matches(event, message):
                raise _Blocked("echo_manager_projection_mismatch")
            event.manager_message, event.permission_transition = message, job
            event.state, event.reason, event.resolved_at = event.State.MANAGER_APPLIED, "exact_manager_projection", now
            event.save(update_fields=["manager_message", "permission_transition", "state", "reason", "resolved_at", "updated_at"])
            return _result(event)
    except _Blocked as exc:
        return EchoAttribution(reason=str(exc))
    except Exception:
        return EchoAttribution(reason="echo_manager_projection_failed", retryable=True)


def acknowledge_historical_manager_echo(*, event_id, settings_id, manager_message_id, now=None):
    """Record native historical context, with no current permission transition."""
    now = now or timezone.now()
    identity = IgDeferredEcho.objects.filter(pk=event_id).values("client_id", "provider_namespace", "recipient_igsid").first()
    if identity is None:
        return EchoAttribution(reason="echo_event_missing")
    try:
        with transaction.atomic():
            _settings, client = _lock_scope(settings_id, identity["provider_namespace"], client_id=identity["client_id"], recipient=identity["recipient_igsid"])
            event = IgDeferredEcho.objects.select_for_update().get(pk=event_id)
            if event.settings_id_snapshot != settings_id or event.payload.get("historical") is not True:
                raise _Blocked("echo_historical_mode_required")
            if event.state == event.State.MANAGER_APPLIED:
                if event.manager_message_id != manager_message_id or event.permission_transition_id:
                    raise _Blocked("echo_manager_projection_mismatch")
                return _result(event, replayed=True)
            if event.state != event.State.MANAGER_PENDING:
                raise _Blocked("echo_manager_not_ready")
            message, _job = _manager_proof(client, event.provider_namespace, event.recipient_igsid, event.provider_message_id, message_id=manager_message_id, historical=True)
            if message is None or not _projection_matches(event, message, historical=True):
                raise _Blocked("echo_manager_projection_mismatch")
            event.manager_message = message
            event.state, event.reason, event.resolved_at = event.State.MANAGER_APPLIED, "exact_historical_projection", now
            event.save(update_fields=["manager_message", "state", "reason", "resolved_at", "updated_at"])
            return _result(event)
    except _Blocked as exc:
        return EchoAttribution(reason=str(exc))
    except Exception:
        return EchoAttribution(reason="echo_historical_projection_failed", retryable=True)
