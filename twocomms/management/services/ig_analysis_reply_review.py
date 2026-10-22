"""Bounded, read-only response evidence for the existing conversation analyzer."""
from __future__ import annotations

import hashlib
import json

from django.db.models import Q

from management.models import IgCustomerTurnRevision, InstagramBotMessage, InstagramBotSettings
from management.services.ig_funnel_reset import current_message_floor

MAX_SOURCE_MESSAGES = 32
MAX_REVISIONS = 4
MAX_SENT_EFFECTS = 8


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def _items(value, limit):
    return value[:limit] if isinstance(value, list) else []


def build_reply_review_context(client, watermark, *, boundary=None):
    """Only exact current source -> immutable revision -> accepted send receipts.

    No clock, inferred delivery/read status, provider I/O, queue writes, or
    nearest-outgoing-message guesses enter this capture or its stable digest.
    """
    target = InstagramBotMessage.objects.filter(
        pk=(boundary or {}).get("message_id", watermark), client_id=client.pk,
        sender_id=client.igsid, role__in=("user", "manager"),
    ).exclude(status="failed").first()
    episode = client.commercial_episodes.filter(pk=client.current_commercial_episode_id).first()
    floor = max(int(current_message_floor(client) or 0),
                int(getattr(episode, "opened_watermark_message_id", 0) or 0))
    namespace = str((boundary or {}).get("namespace") or getattr(target, "provider_namespace", ""))
    context = {"schema_version": 1, "client_id": client.pk,
               "namespace": namespace, "episode_id": episode.pk if episode else None,
               "reset_floor": floor, "client_permission_epoch": int(client.reply_permission_epoch or 0),
               "watermark_message_id": int(watermark), "coverage": "source_scope_unavailable",
               "diagnostic_refs": [], "diagnostics": []}
    from management.services.instagram_bot import ingress_provider_namespace

    settings_row = InstagramBotSettings.objects.filter(pk=1).first()
    current_namespace = ingress_provider_namespace(settings_row) if settings_row else ""
    if (target is None or not namespace or target.provider_namespace != namespace
        or (current_namespace and current_namespace != namespace)
        or target.pk < floor or client.hidden_at or client.is_blocked
        or client.privacy_erasure_started_at):
        return {**context, "digest": _digest(context)}
    event_at = target.provider_created_at or target.created_at
    source_rows = list(InstagramBotMessage.objects.filter(
        client_id=client.pk, sender_id=client.igsid, provider_namespace=namespace,
        role="user", source__in=("webhook", "poll"), pk__gte=floor, pk__lte=watermark,
    ).exclude(status="failed").filter(
        Q(provider_created_at__lte=event_at)
        | Q(provider_created_at__isnull=True, created_at__lte=event_at),
    ).order_by("-id")[:MAX_SOURCE_MESSAGES])
    by_id = {row.pk: row for row in source_rows}
    context["coverage"] = "reply_not_yet_observable"
    if by_id:
        from management.services.ig_revision_execution import _whole_sent_proof
        from management.services.ig_revision_outbox import revision_sources_current

        revisions = IgCustomerTurnRevision.objects.filter(
            client_id=client.pk, sources__message_id__in=by_id,
        ).select_related("client", "turn").distinct().order_by("-id")[:MAX_REVISIONS]
        for revision in revisions:
            if revision.turn.episode_id != context["episode_id"]:
                continue
            sources = list(revision.sources.order_by("ordinal", "id"))
            source_ids = [row.message_id for row in sources]
            if (not source_ids or any(message_id not in by_id for message_id in source_ids)
                or any(row.source_namespace != namespace or row.role != "user" for row in sources)
                or not revision_sources_current(revision)):
                continue
            effects = list(revision.delivery_effects.order_by("order_index", "id")[:33])
            if len(effects) > 32 or not _whole_sent_proof(revision, effects)[0]:
                continue
            latest_source = max((by_id[message_id] for message_id in source_ids),
                                key=lambda row: (row.provider_created_at or row.created_at, row.pk))
            source_time = latest_source.provider_created_at or latest_source.created_at
            for effect in effects:
                if len(context["diagnostics"]) >= MAX_SENT_EFFECTS:
                    break
                if (effect.state != "sent" or effect.provider_namespace != namespace
                    or effect.recipient_igsid != client.igsid or not effect.provider_message_id
                    or effect.terminal_at is None or not effect.provider_http_status
                    or not 200 <= effect.provider_http_status < 300
                    or str((effect.payload.get("recipient") or {}).get("id") or "") != client.igsid):
                    continue
                content = effect.payload.get("message") or {}
                elapsed = (effect.terminal_at - source_time).total_seconds()
                ref = f"revision_effect:{effect.pk}"
                context["diagnostic_refs"].append(ref)
                context["diagnostics"].append({
                    "ref": ref, "kind": "sent_reply", "state": "sent",
                    "delivery_state": "provider_accepted", "source_message_ids": source_ids,
                    "revision_id": revision.pk, "effect_id": effect.pk,
                    "provider_message_id": effect.provider_message_id,
                    "sent_at": effect.terminal_at.isoformat(), "source_event_at": source_time.isoformat(),
                    "time_basis": "provider_event_to_provider_accept" if latest_source.provider_created_at else "local_ingest_to_provider_accept",
                    "response_seconds": round(elapsed, 3) if elapsed >= 0 else None,
                    "text": str(content.get("text") or "")[:1500],
                    "media_observed": isinstance(content.get("attachment"), dict),
                    "media_kind": str(effect.kind)[:32],
                })
    if context["diagnostics"]:
        context["coverage"] = "sent_receipts_observed"
    return {**context, "digest": _digest(context)}


def audit_scope(context):
    return {"client_id": context.get("client_id"), "source_namespace": context.get("namespace", ""),
            "episode_id": context.get("episode_id"), "watermark_message_id": context.get("watermark_message_id"),
            "reset_floor": context.get("reset_floor", 0),
            "client_permission_epoch": context.get("client_permission_epoch", 0),
            "response_review_digest": context.get("digest", "")}


def review_observations(context, *, coverage=None):
    """Persist facts and identities without duplicating transcript text or MIDs."""
    fields = ("ref", "kind", "state", "delivery_state", "source_message_ids", "revision_id",
              "effect_id", "sent_at", "source_event_at", "time_basis", "response_seconds", "media_observed", "media_kind")
    coverage = coverage or context["coverage"]
    rows = context["diagnostics"] if coverage == "sent_receipts_observed" else []
    return {"coverage": coverage, "diagnostics": [
        {key: row[key] for key in fields} for row in rows]}


def public_conversion_audit(snapshot, client, *, fresh):
    """Existing permission-protected readers retain responsibility for PII access."""
    if client.privacy_erasure_started_at or client.hidden_at:
        return {}
    value = getattr(snapshot, "conversion_audit", None)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return {}
    scope = value.get("_source_scope") or {}
    if not isinstance(scope, dict):
        return {}
    episode = client.commercial_episodes.filter(pk=client.current_commercial_episode_id).first()
    floor = max(int(current_message_floor(client) or 0),
                int(getattr(episode, "opened_watermark_message_id", 0) or 0))
    if (scope.get("client_id") != client.pk
        or scope.get("episode_id") != client.current_commercial_episode_id
        or snapshot.commercial_episode_id != scope.get("episode_id")
        or scope.get("watermark_message_id") != snapshot.last_analyzed_message_id
        or scope.get("reset_floor") != floor
        or scope.get("client_permission_epoch") != int(client.reply_permission_epoch or 0)):
        return {}
    source = InstagramBotMessage.objects.filter(pk=snapshot.last_analyzed_message_id,
        client_id=client.pk, sender_id=client.igsid).first()
    if source is None or not scope.get("source_namespace") or scope["source_namespace"] != source.provider_namespace:
        return {}
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_conversion_audit import normalize_conversion_audit
    from management.services.ig_ingress_source_proof import is_current_customer_source

    settings_row = InstagramBotSettings.objects.filter(pk=1).first()
    namespace = ingress_provider_namespace(settings_row) if settings_row else ""
    if namespace and namespace != scope["source_namespace"]:
        return {}
    context = build_reply_review_context(client, snapshot.last_analyzed_message_id)
    if context["digest"] != scope.get("response_review_digest"):
        return {}
    message_ids = set()
    for key in ("motivation", "nonconversion_reasons"):
        for item in _items(value.get(key), 3):
            if not isinstance(item, dict):
                continue
            for proof in (*_items(item.get("evidence"), 4), *_items(item.get("counterevidence"), 4)):
                if isinstance(proof, dict) and type(proof.get("message_id")) is int:
                    message_ids.add(proof["message_id"])
    for proof in _items(value.get("readiness_evidence"), 4):
        if isinstance(proof, dict) and type(proof.get("message_id")) is int:
            message_ids.add(proof["message_id"])
    for diagnostic in context["diagnostics"]:
        message_ids.update(diagnostic["source_message_ids"])
    rows = InstagramBotMessage.objects.filter(pk__in=message_ids, client_id=client.pk,
        sender_id=client.igsid, provider_namespace=scope["source_namespace"],
        role="user", pk__gte=floor, pk__lte=snapshot.last_analyzed_message_id).exclude(status="failed")
    by_id = {row.pk: {"role": "user", "text": row.text, "evidence_eligible": True,
                      "analysis_scope": "current_episode"}
             for row in rows if is_current_customer_source(row, namespace=scope["source_namespace"])}
    normalized = normalize_conversion_audit(value, by_id, context)
    return {"schema_version": 1, "motivation": [], "readiness_evidence": [],
            "nonconversion_reasons": [], **normalized,
            "_response_review": review_observations(context)}
