"""Bounded, read-only inventory of Instagram technical debt.

This module deliberately reports uncertainty instead of repairing it.  It is
safe to call from health/diagnostic paths: collectors only issue SELECTs and
filesystem reads, and every result is capped by ``limit``.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta

from django.conf import settings
from django.db import DatabaseError
from django.db import transaction
from django.utils import timezone


DEFAULT_LIMIT = 100
MAX_LIMIT = 500
STALE_CLAIM = timedelta(minutes=5)
TRANSIENT_DEBT_GRACE = timedelta(minutes=5)
RECONCILER_SCHEMA_VERSION = "ig-technical-debt-reconcile-v1"
RECONCILER_DISPOSITION = "manual_review_required"
_PRESERVED_STATUSES = {"resolved", "dismissed"}
_LIFECYCLE_AUDIT_ACTION = "ig_technical_debt_case_transition"
_LIFECYCLE_ENTITY = "IgTechnicalDebtCase"
_LIFECYCLE_TRANSITIONS = {
    "open": {"acknowledged", "claimed", "resolved", "dismissed"},
    "unknown": {"acknowledged", "claimed", "resolved", "dismissed"},
    "acknowledged": {"claimed", "resolved", "dismissed"},
    "claimed": {"resolved", "dismissed"},
    "resolved": set(),
    "dismissed": set(),
}
_LIFECYCLE_STATUSES = frozenset(_LIFECYCLE_TRANSITIONS)


def _bounded_limit(limit: int) -> int:
    try:
        return max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _bounded_text(value, limit=2048):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _bounded_evidence(value):
    """Keep operator evidence JSON-compatible and bounded for durable storage."""
    if value is None:
        return {}
    if isinstance(value, str):
        return _bounded_text(value, 4096)
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            _bounded_text(key, 80): _bounded_evidence(item)
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_evidence(item) for item in list(value)[:40]]
    return _bounded_text(value, 512)


def _case_payload(case):
    """Serialize a case without exposing model internals or related objects."""
    return {
        "id": case.pk,
        "case_key": case.case_key,
        "reason": case.reason,
        "scope": case.scope,
        "status": case.status,
        "first_observed_at": case.first_observed_at.isoformat() if case.first_observed_at else None,
        "last_observed_at": case.last_observed_at.isoformat() if case.last_observed_at else None,
        "oldest_observed_at": case.oldest_observed_at.isoformat() if case.oldest_observed_at else None,
        "last_count": case.last_count,
        "observation_fingerprint": case.observation_fingerprint,
        "fingerprint": case.observation_fingerprint,
        "sample_ids": list(case.sample_ids or []),
        "has_more": bool(case.has_more),
        "coverage_complete": bool(case.coverage_complete),
        "disposition": case.disposition,
        "operator_note": case.operator_note,
        "owner_id": case.owner_id,
        "acknowledged_at": case.acknowledged_at.isoformat() if case.acknowledged_at else None,
        "resolved_at": case.resolved_at.isoformat() if case.resolved_at else None,
        "evidence": case.evidence if isinstance(case.evidence, dict) else {},
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
    }


def list_ig_technical_debt_cases(*, status=None, limit=DEFAULT_LIMIT, offset=0):
    """Return a bounded operator inventory; this function never writes."""
    from management.ig_bot_models import IgTechnicalDebtCase

    limit = _bounded_limit(limit)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    queryset = IgTechnicalDebtCase.objects.all().order_by("status", "first_observed_at", "id")
    if status:
        status = _bounded_text(status, 16)
        if status not in _LIFECYCLE_STATUSES:
            return {"ok": False, "status": 400, "error": "invalid_status", "cases": []}
        queryset = queryset.filter(status=status)
    total = queryset.count()
    rows = list(queryset[offset:offset + limit])
    return {
        "ok": True,
        "status": 200,
        "cases": [_case_payload(case) for case in rows],
        "count": len(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
    }


def transition_ig_technical_debt_case(
    case_id=None,
    *,
    case_key=None,
    target_status,
    actor=None,
    action="",
    evidence=None,
    note="",
    now=None,
):
    """Apply one explicit operator transition and record an audit event.

    The service only mutates the durable case and its audit record. It never
    calls Instagram/provider code or edits source/client records.
    """
    from management.ig_bot_models import IgTechnicalDebtCase
    from management.models import AdminAuditLog

    target_status = _bounded_text(target_status, 16).lower()
    action = _bounded_text(action, 512)
    note = _bounded_text(note, 4096)
    evidence = _bounded_evidence(evidence)
    if target_status not in _LIFECYCLE_STATUSES:
        return {"ok": False, "status": 400, "error": "invalid_target_status"}
    if actor is None:
        return {"ok": False, "status": 403, "error": "actor_required"}
    if not action:
        return {"ok": False, "status": 400, "error": "action_required"}
    if target_status in {"resolved", "dismissed"} and not evidence:
        return {"ok": False, "status": 400, "error": "evidence_required"}
    if case_id is None and not case_key:
        return {"ok": False, "status": 400, "error": "case_identifier_required"}
    now = now or timezone.now()
    with transaction.atomic():
        lookup = {"pk": case_id} if case_id is not None else {"case_key": _bounded_text(case_key, 160)}
        case = IgTechnicalDebtCase.objects.select_for_update().filter(**lookup).first()
        if case is None:
            return {"ok": False, "status": 404, "error": "case_not_found"}
        current_status = case.status
        if current_status == target_status:
            if target_status == "claimed" and actor is not None and case.owner_id not in (None, actor.pk):
                return {"ok": False, "status": 409, "error": "case_claimed_by_other"}
            return {"ok": True, "status": 200, "idempotent": True, "case": _case_payload(case)}
        if target_status not in _LIFECYCLE_TRANSITIONS.get(current_status, set()):
            return {"ok": False, "status": 409, "error": "invalid_transition", "current_status": current_status}
        if target_status == "claimed" and case.owner_id not in (None, getattr(actor, "pk", None)):
            return {"ok": False, "status": 409, "error": "case_claimed_by_other"}

        before = _case_payload(case)
        event = {
            "at": now.isoformat(),
            "from_status": current_status,
            "to_status": target_status,
            "action": action,
            "evidence": evidence,
            "actor_id": getattr(actor, "pk", None),
        }
        stored_evidence = dict(case.evidence) if isinstance(case.evidence, dict) else {}
        events = list(stored_evidence.get("operator_events") or [])
        events.append(event)
        stored_evidence["operator_events"] = events[-100:]
        stored_evidence["latest_operator_action"] = action
        stored_evidence["latest_operator_evidence"] = evidence
        case.status = target_status
        case.evidence = stored_evidence
        if note:
            case.operator_note = note
        if target_status == "claimed":
            case.owner = actor
        if target_status in {"acknowledged", "claimed"} and case.acknowledged_at is None:
            case.acknowledged_at = now
        if target_status in {"resolved", "dismissed"}:
            case.resolved_at = now
        fields = ["status", "evidence", "operator_note", "owner", "acknowledged_at", "resolved_at", "updated_at"]
        case.save(update_fields=fields)
        after = _case_payload(case)
        AdminAuditLog.objects.create(
            actor=actor,
            actor_role="staff" if actor is not None else "",
            action=_LIFECYCLE_AUDIT_ACTION,
            entity_type=_LIFECYCLE_ENTITY,
            entity_id=str(case.pk),
            before={"status": before["status"], "owner_id": before["owner_id"]},
            after={"status": after["status"], "owner_id": after["owner_id"], "action": action, "evidence": evidence},
            reason=action,
        )
        return {"ok": True, "status": 200, "idempotent": False, "case": after}


def acknowledge_ig_technical_debt_case(case_id=None, **kwargs):
    return transition_ig_technical_debt_case(case_id, target_status="acknowledged", **kwargs)


def claim_ig_technical_debt_case(case_id=None, **kwargs):
    return transition_ig_technical_debt_case(case_id, target_status="claimed", **kwargs)


def resolve_ig_technical_debt_case(case_id=None, **kwargs):
    return transition_ig_technical_debt_case(case_id, target_status="resolved", **kwargs)


def dismiss_ig_technical_debt_case(case_id=None, **kwargs):
    return transition_ig_technical_debt_case(case_id, target_status="dismissed", **kwargs)


def _age(now, value):
    if not value:
        return None
    return max(0, int((now - value).total_seconds()))


def _case(reason, scope, *, count, oldest=None, ids=(), sampled=False, has_more=False):
    return {
        "reason": str(reason)[:64],
        "scope": str(scope)[:64],
        "count": int(count),
        "oldest_age_seconds": oldest,
        "sample_ids": [int(value) for value in ids[:MAX_LIMIT]],
        "sampled": bool(sampled),
        "has_more": bool(has_more),
    }


def _query_case(query, *, reason, scope, now, limit, time_field, id_field="id"):
    total = query.count()
    rows = list(query.order_by(time_field, id_field).values(time_field, id_field)[:limit])
    oldest = _age(now, rows[0].get(time_field)) if rows else None
    return _case(reason, scope, count=total, oldest=oldest,
                 ids=[row[id_field] for row in rows], sampled=total > len(rows),
                 has_more=total > len(rows))


def _fingerprint_material(cases):
    """Return stable case identity without making age-only polls noisy."""
    material = []
    for case in cases:
        sample_ids = tuple(
            sorted({
                int(value)
                for value in (case.get("sample_ids") or ())
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
            })
        )
        material.append((
            str(case.get("reason") or "")[:64],
            str(case.get("scope") or "")[:64],
            sample_ids,
            bool(case.get("has_more")),
        ))
    return sorted(set(material))


def _collect_db(now, limit):
    from management.models import InstagramBotMessage, IgCustomerTurn, IgCustomerTurnRevision
    from management.models import IgDeferredEcho, IgRevisionDeliveryEffect, IgWebhookInboxEvent

    cases = []
    cutoff = now - STALE_CLAIM
    transient_cutoff = now - TRANSIENT_DEBT_GRACE
    cases.append(_query_case(
        InstagramBotMessage.objects.filter(status="processing", processing_started_at__lt=cutoff),
        reason="legacy_processing_claim_expired", scope="legacy_message", now=now,
        limit=limit, time_field="processing_started_at"))
    cases.append(_query_case(
        IgCustomerTurn.objects.filter(claim_state="claimed", claimed_at__lt=cutoff),
        reason="turn_claim_expired", scope="customer_turn", now=now,
        limit=limit, time_field="claimed_at"))
    cases.append(_query_case(
        IgCustomerTurnRevision.objects.filter(state__in=("preparing", "claimed"), lease_until__lte=now),
        reason="revision_claim_expired", scope="turn_revision", now=now,
        limit=limit, time_field="lease_until"))
    cases.append(_query_case(
        IgRevisionDeliveryEffect.objects.filter(
            state="planned", created_at__lt=transient_cutoff,
        ),
        reason="unsent_delivery_intent", scope="delivery_effect", now=now,
        limit=limit, time_field="created_at"))
    cases.append(_query_case(
        IgRevisionDeliveryEffect.objects.filter(state__in=("claimed", "provider_started"), lease_until__lte=now),
        reason="delivery_claim_expired", scope="delivery_effect", now=now,
        limit=limit, time_field="lease_until"))
    cases.append(_query_case(
        IgRevisionDeliveryEffect.objects.filter(state="unknown"),
        reason="canonical_delivery_unknown", scope="delivery_effect", now=now,
        limit=limit, time_field="updated_at"))
    cases.append(_query_case(
        InstagramBotMessage.objects.filter(send_state="unknown"),
        reason="legacy_send_unknown", scope="legacy_message", now=now,
        limit=limit, time_field="send_started_at"))
    cases.append(_query_case(
        IgDeferredEcho.objects.filter(
            state__in=("waiting_receipt", "ambiguous"),
            observed_at__lt=transient_cutoff,
        ),
        reason="deferred_provider_echo", scope="deferred_echo", now=now,
        limit=limit, time_field="observed_at"))
    cases.append(_query_case(
        InstagramBotMessage.objects.filter(
            private_media_state="deleting", private_media_delete_claimed_at__lte=cutoff,
        ),
        reason="private_media_delete_claim_expired", scope="private_media", now=now,
        limit=limit, time_field="private_media_delete_claimed_at"))
    cases.append(_query_case(
        IgWebhookInboxEvent.objects.filter(
            decision="accepted",
            processed_at__isnull=True,
            received_at__lt=transient_cutoff,
        ),
        reason="webhook_ingress_pending", scope="webhook_inbox", now=now,
        limit=limit, time_field="received_at"))
    return cases


def _collect_media(now, limit):
    """Report stale capture metadata and filesystem orphans without deleting."""
    from management.models import InstagramBotMessage

    cutoff = now - STALE_CLAIM
    rows = InstagramBotMessage.objects.filter(attachment_media__isnull=False).only("id", "attachment_media")
    stale_ids = set()
    referenced = set()
    reference_scan_complete = True
    for row_index, row in enumerate(rows.iterator(chunk_size=min(limit, 100))):
        if row_index >= limit * 20:
            reference_scan_complete = False
            break
        for item in row.attachment_media or ():
            if not isinstance(item, dict):
                continue
            name = str(item.get("storage_name") or "").strip()
            if name:
                referenced.add(name)
            started = item.get("capture_started_at")
            if item.get("status") == "acquiring" and started:
                try:
                    parsed = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                    if parsed <= cutoff:
                        stale_ids.add(row.pk)
                except (TypeError, ValueError):
                    continue
    ordered_stale_ids = sorted(stale_ids)
    cases = [_case("media_capture_claim_expired", "private_media", count=len(ordered_stale_ids),
                    ids=ordered_stale_ids[:limit], sampled=len(ordered_stale_ids) > limit,
                    has_more=len(ordered_stale_ids) > limit)]
    root = str(getattr(settings, "IG_PRIVATE_MEDIA_ROOT", "") or "").strip()
    if not root:
        # Private media is an optional capability. An intentionally unset root
        # must not manufacture a recurring technical-debt incident.
        return cases, True
    if not os.path.isdir(root):
        # A configured but unavailable root is a coverage problem, not proof of
        # orphaned customer media. Keep it in the degraded metadata path.
        return cases, False
    orphan_count = 0
    walked = 0
    for base, _dirs, files in os.walk(root):
        for filename in files:
            walked += 1
            relative = os.path.relpath(os.path.join(base, filename), root).replace(os.sep, "/")
            if reference_scan_complete and relative not in referenced:
                orphan_count += 1
            if walked >= limit * 20:
                break
        if walked >= limit * 20:
            break
    if orphan_count:
        cases.append(_case("orphan_private_media", "private_media", count=orphan_count,
                           ids=(), sampled=walked >= limit * 20, has_more=walked >= limit * 20,
                           oldest=None))
        # File names are intentionally omitted from the report; they can contain
        # provider/customer-derived material and are not needed for triage.
        cases[-1]["sample_ids"] = []
    # An incomplete reference scan cannot distinguish an orphan from a
    # referenced file outside the row cap. Report degraded coverage and wait
    # for a complete bounded pass instead of raising a false orphan alert.
    return cases, bool(reference_scan_complete and walked < limit * 20)


def _media_coverage_reason():
    """Return a bounded, non-PII reason for an incomplete media scan."""
    root = str(getattr(settings, "IG_PRIVATE_MEDIA_ROOT", "") or "").strip()
    if root and not os.path.isdir(root):
        return "private_media_root_unavailable"
    return "private_media_scan_capped"


def technical_debt_snapshot(*, now=None, limit=DEFAULT_LIMIT):
    """Return a bounded, privacy-safe technical-debt snapshot.

    ``coverage_complete`` is false whenever a collector fails or a filesystem
    walk is capped.  The exception class is retained only as a machine code.
    """
    now = now or timezone.now()
    limit = _bounded_limit(limit)
    cases = []
    errors = []
    coverage_reasons = []
    complete = True
    try:
        cases.extend(_collect_db(now, limit))
    except Exception as exc:
        complete = False
        errors.append(type(exc).__name__[:64])
        coverage_reasons.append("db_collector_error")
    try:
        media_cases, media_complete = _collect_media(now, limit)
        cases.extend(media_cases)
        complete = complete and media_complete
        if not media_complete:
            coverage_reasons.append(_media_coverage_reason())
    except Exception as exc:
        complete = False
        errors.append(type(exc).__name__[:64])
        coverage_reasons.append("media_collector_error")
    cases = [case for case in cases if case["count"] or case["reason"].endswith("unavailable")]
    # Counts and ages are observations, not identity.  Including either value
    # made the fingerprint change on every health poll while the same debt was
    # still open, defeating the hourly alert dedupe and spamming Telegram.
    # Identity is the stable set of debt classes and scopes; the alert metadata
    # still carries the current counts and ages for triage.
    fingerprint_material = _fingerprint_material(cases)
    fingerprint = hashlib.sha256(repr(fingerprint_material).encode("utf-8")).hexdigest()[:24]
    return {
        "observed_at": now.isoformat(),
        "fingerprint": fingerprint,
        "cases": cases,
        "case_count": len(cases),
        "coverage_complete": complete,
        "coverage_reasons": coverage_reasons[:8],
        "errors": errors,
        "sample_limit": limit,
    }


def _reconciler_case(case, *, now):
    """Project one bounded observation into a deterministic operator proposal."""
    reason = str(case.get("reason") or "unknown")[:64]
    scope = str(case.get("scope") or "unknown")[:64]
    sample_ids = []
    for value in case.get("sample_ids") or ():
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            sample_ids.append(value)
    sample_ids = sorted(set(sample_ids))
    oldest_age = case.get("oldest_age_seconds")
    if isinstance(oldest_age, bool) or not isinstance(oldest_age, (int, float)):
        oldest_age = None
    elif oldest_age < 0:
        oldest_age = 0
    else:
        oldest_age = int(oldest_age)
    first_observed = now - timedelta(seconds=oldest_age) if oldest_age is not None else None
    identity = f"{reason}:{scope}"
    fingerprint_material = (identity, tuple(sample_ids), bool(case.get("has_more")))
    case_fingerprint = hashlib.sha256(
        repr(fingerprint_material).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "identity": identity,
        "case_fingerprint": case_fingerprint,
        "reason": reason,
        "scope": scope,
        "count": int(case.get("count") or 0),
        "first_observed_at": first_observed.isoformat() if first_observed else None,
        "last_observed_at": now.isoformat(),
        "oldest_age_seconds": oldest_age,
        "source_ids": sample_ids,
        "sample_ids": sample_ids,
        "sampled": bool(case.get("sampled")),
        "has_more": bool(case.get("has_more")),
        "disposition": RECONCILER_DISPOSITION,
        "action": "no_automatic_mutation",
    }


def reconcile_ig_technical_debt_once(*, now=None, limit=DEFAULT_LIMIT, dry_run=True):
    """Return bounded proposals, optionally persisting operator observations."""
    now = now or timezone.now()
    snapshot = technical_debt_snapshot(now=now, limit=limit)
    proposals = sorted(
        (_reconciler_case(case, now=now) for case in snapshot.get("cases") or ()),
        key=lambda case: (case["identity"], case["case_fingerprint"]),
    )
    writes = 0
    if not dry_run and snapshot.get("coverage_complete") and proposals:
        from management.ig_bot_models import IgTechnicalDebtCase

        with transaction.atomic():
            for proposal in proposals:
                case, created = IgTechnicalDebtCase.objects.select_for_update().get_or_create(
                    case_key=proposal["identity"],
                    defaults={
                        "reason": proposal["reason"],
                        "scope": proposal["scope"],
                        "first_observed_at": (
                            now - timedelta(seconds=proposal["oldest_age_seconds"])
                            if proposal["oldest_age_seconds"] is not None else now
                        ),
                        "last_observed_at": now,
                        "oldest_observed_at": (
                            now - timedelta(seconds=proposal["oldest_age_seconds"])
                            if proposal["oldest_age_seconds"] is not None else None
                        ),
                        "last_count": proposal["count"],
                        "observation_fingerprint": proposal["case_fingerprint"],
                        "sample_ids": proposal["sample_ids"],
                        "has_more": proposal["has_more"],
                        "coverage_complete": True,
                        "status": IgTechnicalDebtCase.Status.OPEN,
                    },
                )
                updates = {
                    "reason": proposal["reason"], "scope": proposal["scope"],
                    "last_observed_at": now, "oldest_observed_at": (
                        now - timedelta(seconds=proposal["oldest_age_seconds"])
                        if proposal["oldest_age_seconds"] is not None else None
                    ),
                    "last_count": proposal["count"],
                    "observation_fingerprint": proposal["case_fingerprint"],
                    "sample_ids": proposal["sample_ids"],
                    "has_more": proposal["has_more"],
                    "coverage_complete": True,
                }
                if case.status not in _PRESERVED_STATUSES:
                    updates["status"] = case.status or IgTechnicalDebtCase.Status.OPEN
                changed = {
                    field: value for field, value in updates.items()
                    if getattr(case, field) != value
                }
                if changed:
                    for field, value in changed.items():
                        setattr(case, field, value)
                    case.save(update_fields=[*changed, "updated_at"])
                    writes += 1
                elif created:
                    writes += 1
    return {
        "schema_version": RECONCILER_SCHEMA_VERSION,
        "observed_at": now.isoformat(),
        "dry_run": bool(dry_run),
        "mode": "proposal_only" if dry_run else "apply",
        "idempotent": True,
        "provider_calls": 0,
        "writes": writes,
        "persistence": {
            "supported": True,
            "reason": "ig_technical_debt_case",
        },
        "proposed_cases": proposals,
        "case_count": len(proposals),
        "coverage_complete": bool(snapshot.get("coverage_complete")),
        "errors": list(snapshot.get("errors") or ()),
        "sample_limit": snapshot.get("sample_limit", _bounded_limit(limit)),
    }
