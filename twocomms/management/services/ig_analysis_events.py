"""Owned operational consumer for Instagram conversation-analysis events.

The Gemini worker may publish a typed proposal after a successful snapshot, but
only this module may materialize that proposal into commercial state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone


REPEAT_EVENT_VERSION = "v1"
MAX_EVENT_ATTEMPTS = 5
EVENT_RETRY_DELAYS = (60, 180, 600, 1800, 3600)
MIN_REPEAT_CONFIDENCE = Decimal("0.7000")


def _decimal(value, default="0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _repeat_payload(*, repeat_intent, analysis_model, analysis_prompt_version):
    from management.ig_bot_models import IgCommercialEpisode

    kind = str((repeat_intent or {}).get("kind") or "")
    allowed = {
        value
        for value, _label in IgCommercialEpisode.RepeatKind.choices
    } - {IgCommercialEpisode.RepeatKind.FIRST_PURCHASE}
    if kind not in allowed:
        raise ValueError("Unsupported repeat event kind")
    evidence_ids = sorted({
        int(value)
        for value in ((repeat_intent or {}).get("evidence_message_ids") or [])
        if str(value).isdigit()
    })
    if not evidence_ids:
        raise ValueError("Repeat event requires customer evidence")
    confidence = _decimal((repeat_intent or {}).get("confidence"), "0").quantize(
        Decimal("0.0001")
    )
    if not MIN_REPEAT_CONFIDENCE <= confidence <= Decimal("1.0000"):
        raise ValueError("Repeat event confidence must be between 0.7000 and 1.0000")
    return {
        "repeat_kind": kind,
        "evidence_message_ids": evidence_ids,
        "confidence": str(confidence),
        "analysis_model": str(analysis_model or "")[:80],
        "analysis_prompt_version": str(analysis_prompt_version or "")[:80],
    }


def _event_key(snapshot_dedupe_key: str, payload: dict) -> str:
    canonical = json.dumps(
        {
            "version": REPEAT_EVENT_VERSION,
            "snapshot": str(snapshot_dedupe_key),
            "payload": payload,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"analysis-event:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _snapshot_source_digest(snapshot) -> str:
    """Bind an operational event to the source snapshot as published."""
    canonical = {
        "client_id": snapshot.client_id,
        "last_analyzed_message_id": snapshot.last_analyzed_message_id,
        "dedupe_key": str(snapshot.dedupe_key),
        "score_band": str(snapshot.score_band),
        "interaction_type": str(snapshot.interaction_type),
        "purchase_probability": str(snapshot.purchase_probability),
        "confidence": str(snapshot.confidence),
        "evidence": snapshot.evidence or [],
        "uncertainties": snapshot.uncertainties or [],
        "repeat_intent": snapshot.repeat_intent or {},
        "commercial_episode_id": snapshot.commercial_episode_id,
        "analysis_model": str(snapshot.analysis_model or ""),
        "analysis_prompt_version": str(snapshot.analysis_prompt_version or ""),
        "required_state_fingerprint": str(snapshot.required_state_fingerprint or ""),
    }
    encoded = json.dumps(
        canonical,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def publish_repeat_episode_event(
    *,
    client,
    snapshot,
    repeat_intent: dict,
    analysis_model: str,
    analysis_prompt_version: str,
    required_state_fingerprint: str,
):
    """Publish one immutable repeat proposal after an analysis snapshot."""
    from management.ig_bot_models import IgConversationAnalysisEvent

    if snapshot.client_id != client.pk:
        raise ValueError("Analysis snapshot belongs to another client")
    payload = _repeat_payload(
        repeat_intent=repeat_intent,
        analysis_model=analysis_model,
        analysis_prompt_version=analysis_prompt_version,
    )
    source_digest = _snapshot_source_digest(snapshot)
    event_key = _event_key(snapshot.dedupe_key, payload)
    event, _created = IgConversationAnalysisEvent.objects.get_or_create(
        event_key=event_key,
        defaults={
            "client": client,
            "snapshot": snapshot,
            "event_type": IgConversationAnalysisEvent.EventType.REPEAT_EPISODE,
            "payload": payload,
            "required_state_fingerprint": str(required_state_fingerprint or "")[:64],
            "source_digest": source_digest,
        },
    )
    if event.client_id != client.pk or event.snapshot_id != snapshot.pk:
        raise ValueError("Analysis event key belongs to another source")
    if event.payload != payload:
        raise ValueError("Analysis event payload is immutable")
    if event.source_digest != source_digest:
        raise ValueError("Analysis event source digest is immutable")
    return event


def _reject(event, reason: str, now):
    from management.ig_bot_models import IgConversationAnalysisEvent

    event.status = IgConversationAnalysisEvent.Status.REJECTED
    event.rejected_reason = str(reason)[:120]
    event.rejected_at = now
    event.last_error = ""
    event.next_attempt_at = None
    event.save(update_fields=[
        "status", "rejected_reason", "rejected_at", "last_error",
        "next_attempt_at", "updated_at",
    ])
    return "rejected"


def _validate_event(event, client, now):
    from management.ig_bot_models import IgConversationAnalysisEvent
    from management.models import InstagramBotMessage
    from management.services.bot_conversation_analysis import (
        _fingerprint_for_truth,
        _has_explicit_repeat_evidence,
        _required_truth_state,
    )
    from management.services.bot_payment_truth import client_has_confirmed_purchase

    snapshot = event.snapshot
    if event.event_type != IgConversationAnalysisEvent.EventType.REPEAT_EPISODE:
        return "unknown_event_type"
    if snapshot.client_id != client.pk or event.client_id != client.pk:
        return "client_mismatch"
    if client.hidden_at:
        return "client_hidden"
    if client.is_blocked:
        return "client_blocked"
    if client.opted_out_at and (
        not client.opted_in_at or client.opted_in_at < client.opted_out_at
    ):
        return "client_opted_out"
    if event.source_digest != _snapshot_source_digest(snapshot):
        return "snapshot_digest_mismatch"
    if snapshot.required_state_fingerprint != event.required_state_fingerprint:
        return "snapshot_fingerprint_mismatch"
    try:
        expected_payload = _repeat_payload(
            repeat_intent=snapshot.repeat_intent,
            analysis_model=snapshot.analysis_model,
            analysis_prompt_version=snapshot.analysis_prompt_version,
        )
    except ValueError:
        return "invalid_repeat_intent"
    if event.payload != expected_payload:
        return "payload_mismatch"
    if _event_key(snapshot.dedupe_key, event.payload) != event.event_key:
        return "event_key_mismatch"
    if not client_has_confirmed_purchase(client):
        return "payment_not_confirmed"
    watermark = int(snapshot.last_analyzed_message_id or 0)
    if InstagramBotMessage.objects.filter(
        client_id=client.pk,
        pk__gt=watermark,
        role__in=[
            InstagramBotMessage.Role.USER,
            InstagramBotMessage.Role.MANAGER,
        ],
    ).exists():
        return "conversation_advanced"
    evidence_ids = expected_payload["evidence_message_ids"]
    rows = list(
        InstagramBotMessage.objects.filter(
            client_id=client.pk,
            pk__in=evidence_ids,
            role=InstagramBotMessage.Role.USER,
        ).values_list("pk", "text")
    )
    if sorted(int(message_id) for message_id, _text in rows) != evidence_ids:
        return "evidence_not_customer_owned"
    if any(
        not _has_explicit_repeat_evidence(str(text or ""))
        for _message_id, text in rows
    ):
        return "repeat_evidence_missing"
    if any(message_id > watermark for message_id in evidence_ids):
        return "evidence_after_watermark"
    current_fingerprint = _fingerprint_for_truth(
        client.pk,
        watermark,
        _required_truth_state(client),
    )
    if current_fingerprint != event.required_state_fingerprint:
        return "truth_fingerprint_stale"
    return ""


def _consume_event(event_id: int, now):
    from management.ig_bot_models import (
        IgClient,
        IgCommercialEpisode,
        IgConversationAnalysisEvent,
    )
    from management.services.ig_commercial_episodes import (
        commercial_episode_client_lock,
        repeat_materialization_key,
        start_repeat_episode,
    )

    reference = (
        IgConversationAnalysisEvent.objects.filter(pk=event_id)
        .values("client_id")
        .first()
    )
    if not reference:
        return "missing"
    with commercial_episode_client_lock(int(reference["client_id"])):
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=reference["client_id"])
            event = (
                IgConversationAnalysisEvent.objects.select_for_update()
                .select_related("snapshot")
                .get(pk=event_id)
            )
            if event.status == IgConversationAnalysisEvent.Status.APPLIED:
                return "already_applied"
            if event.status == IgConversationAnalysisEvent.Status.REJECTED:
                return "already_rejected"
            raw_payload = event.payload if isinstance(event.payload, dict) else {}
            try:
                materialization_key = repeat_materialization_key(
                    client.pk,
                    raw_payload.get("evidence_message_ids") or [],
                )
            except (TypeError, ValueError):
                materialization_key = ""
            if materialization_key and IgCommercialEpisode.objects.filter(
                client=client,
                materialization_key=materialization_key,
            ).exists():
                return _reject(event, "already_materialized", now)
            reason = _validate_event(event, client, now)
            if reason:
                return _reject(event, reason, now)
            payload = event.payload
            episode = start_repeat_episode(
                client,
                repeat_kind=payload["repeat_kind"],
                evidence_message_ids=payload["evidence_message_ids"],
                confidence=payload["confidence"],
                analysis_model=payload["analysis_model"],
                analysis_prompt_version=payload["analysis_prompt_version"],
                _lock_held=True,
                preserve_client_stage=True,
            )
            event.status = IgConversationAnalysisEvent.Status.APPLIED
            event.applied_episode = episode
            event.applied_at = now
            event.last_error = ""
            event.next_attempt_at = None
            event.save(update_fields=[
                "status", "applied_episode", "applied_at", "last_error",
                "next_attempt_at", "updated_at",
            ])
            return "applied"


def _record_failure(event_id: int, exc, now):
    from management.ig_bot_models import IgConversationAnalysisEvent

    with transaction.atomic():
        event = (
            IgConversationAnalysisEvent.objects.select_for_update()
            .filter(
                pk=event_id,
                status=IgConversationAnalysisEvent.Status.PENDING,
            )
            .first()
        )
        if not event:
            return "missing"
        event.attempts = int(event.attempts or 0) + 1
        event.last_error = str(exc)[:1000]
        if event.attempts >= MAX_EVENT_ATTEMPTS:
            event.status = IgConversationAnalysisEvent.Status.FAILED
            event.next_attempt_at = None
        else:
            delay = EVENT_RETRY_DELAYS[
                min(event.attempts - 1, len(EVENT_RETRY_DELAYS) - 1)
            ]
            event.next_attempt_at = now + timedelta(seconds=delay)
        event.save(update_fields=[
            "attempts", "last_error", "status", "next_attempt_at",
            "updated_at",
        ])
        return (
            "failed"
            if event.status == IgConversationAnalysisEvent.Status.FAILED
            else "retry_scheduled"
        )


def process_due_analysis_events(*, limit: int = 2, now=None) -> dict:
    """Materialize pending analysis proposals through one owned consumer."""
    from management.ig_bot_models import IgConversationAnalysisEvent
    from management.services.ig_analysis_lane import owner_claim_admission

    now = now or timezone.now()
    if not owner_claim_admission(now=now):
        return {"deferred": 1}
    event_ids = list(
        IgConversationAnalysisEvent.objects.filter(
            status=IgConversationAnalysisEvent.Status.PENDING,
            next_attempt_at__isnull=False,
            next_attempt_at__lte=now,
        )
        .order_by("created_at", "id")
        .values_list("pk", flat=True)[: max(0, min(int(limit), 10))]
    )
    counts = {
        "applied": 0,
        "already_applied": 0,
        "already_rejected": 0,
        "rejected": 0,
        "retry_scheduled": 0,
        "failed": 0,
        "missing": 0,
    }
    for event_id in event_ids:
        try:
            outcome = _consume_event(event_id, now)
        except Exception as exc:
            failure_outcome = _record_failure(event_id, exc, now)
            counts[failure_outcome] = counts.get(failure_outcome, 0) + 1
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
