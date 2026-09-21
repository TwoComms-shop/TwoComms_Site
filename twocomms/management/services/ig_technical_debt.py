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
from django.utils import timezone


DEFAULT_LIMIT = 100
MAX_LIMIT = 500
STALE_CLAIM = timedelta(minutes=5)
TRANSIENT_DEBT_GRACE = timedelta(minutes=5)
RECONCILER_SCHEMA_VERSION = "ig-technical-debt-reconcile-v1"
RECONCILER_DISPOSITION = "manual_review_required"


def _bounded_limit(limit: int) -> int:
    try:
        return max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


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


def technical_debt_snapshot(*, now=None, limit=DEFAULT_LIMIT):
    """Return a bounded, privacy-safe technical-debt snapshot.

    ``coverage_complete`` is false whenever a collector fails or a filesystem
    walk is capped.  The exception class is retained only as a machine code.
    """
    now = now or timezone.now()
    limit = _bounded_limit(limit)
    cases = []
    errors = []
    complete = True
    try:
        cases.extend(_collect_db(now, limit))
    except Exception as exc:
        complete = False
        errors.append(type(exc).__name__[:64])
    try:
        media_cases, media_complete = _collect_media(now, limit)
        cases.extend(media_cases)
        complete = complete and media_complete
    except Exception as exc:
        complete = False
        errors.append(type(exc).__name__[:64])
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
    """Return bounded, idempotent technical-debt proposals.

    There is no dedicated technical-debt case table in the current schema.
    Consequently this pass never writes an operator task, changes source
    records, or contacts a provider, even when ``dry_run`` is false.  The
    explicit persistence metadata lets callers distinguish a proposal from a
    durable reconciliation without inventing lifecycle state.
    """
    now = now or timezone.now()
    snapshot = technical_debt_snapshot(now=now, limit=limit)
    proposals = sorted(
        (_reconciler_case(case, now=now) for case in snapshot.get("cases") or ()),
        key=lambda case: (case["identity"], case["case_fingerprint"]),
    )
    return {
        "schema_version": RECONCILER_SCHEMA_VERSION,
        "observed_at": now.isoformat(),
        "dry_run": bool(dry_run),
        "mode": "proposal_only",
        "idempotent": True,
        "provider_calls": 0,
        "writes": 0,
        "persistence": {
            "supported": False,
            "reason": "no_dedicated_technical_debt_case_schema",
        },
        "proposed_cases": proposals,
        "case_count": len(proposals),
        "coverage_complete": bool(snapshot.get("coverage_complete")),
        "errors": list(snapshot.get("errors") or ()),
        "sample_limit": snapshot.get("sample_limit", _bounded_limit(limit)),
    }
