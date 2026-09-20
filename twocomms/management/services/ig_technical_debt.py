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


def _collect_db(now, limit):
    from management.models import InstagramBotMessage, IgCustomerTurn, IgCustomerTurnRevision
    from management.models import IgDeferredEcho, IgRevisionDeliveryEffect, IgWebhookInboxEvent

    cases = []
    cutoff = now - STALE_CLAIM
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
        IgRevisionDeliveryEffect.objects.filter(state="planned"),
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
        IgDeferredEcho.objects.filter(state__in=("waiting_receipt", "ambiguous")),
        reason="deferred_provider_echo", scope="deferred_echo", now=now,
        limit=limit, time_field="observed_at"))
    cases.append(_query_case(
        InstagramBotMessage.objects.filter(
            private_media_state="deleting", private_media_delete_claimed_at__lte=cutoff,
        ),
        reason="private_media_delete_claim_expired", scope="private_media", now=now,
        limit=limit, time_field="private_media_delete_claimed_at"))
    cases.append(_query_case(
        IgWebhookInboxEvent.objects.filter(decision="accepted", processed_at__isnull=True),
        reason="webhook_ingress_pending", scope="webhook_inbox", now=now,
        limit=limit, time_field="received_at"))
    return cases


def _collect_media(now, limit):
    """Report stale capture metadata and filesystem orphans without deleting."""
    from management.models import InstagramBotMessage

    cutoff = now - STALE_CLAIM
    rows = InstagramBotMessage.objects.filter(attachment_media__isnull=False).only("id", "attachment_media")
    stale_ids = []
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
                        stale_ids.append(row.pk)
                except (TypeError, ValueError):
                    continue
    cases = [_case("media_capture_claim_expired", "private_media", count=len(stale_ids),
                    ids=stale_ids[:limit], sampled=len(stale_ids) > limit, has_more=len(stale_ids) > limit)]
    root = str(getattr(settings, "IG_PRIVATE_MEDIA_ROOT", "") or "").strip()
    if not root or not os.path.isdir(root):
        cases.append(_case("orphan_media_scan_unavailable", "private_media", count=0, sampled=False, has_more=False))
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
    fingerprint_material = sorted({
        (str(case.get("reason") or "")[:64], str(case.get("scope") or "")[:64])
        for case in cases
    })
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
