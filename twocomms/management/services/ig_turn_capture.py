"""Read-only, bounded adapter from a sealed revision to pure turn intelligence.

No history reconstruction, session bootstrap, provider I/O, or business effects.
The generation boundary's response plan remains the sole selection authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import re
from collections.abc import Mapping
from types import MappingProxyType

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from management.services.ig_turn_intelligence import (
    MAX_HISTORY, MAX_MEDIA_PARTS, MAX_SOURCES, SCOPE_KEYS, TurnContextError,
    build_turn_context, capture_digest, validate_source_cart_capture,
)

MAX_SIGNALS = 24
MAX_MEMORY_SOURCES = 64
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def detached_payload(value):
    """Thaw pure-builder captures for JSON/state-card assembly without reads."""
    if isinstance(value, Mapping):
        return {key: detached_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [detached_payload(item) for item in value]
    return deepcopy(value)


def _frozen(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_frozen(item) for item in value)
    return value


def validate_current_source_cart_sources(capture, *, source_rows=None):
    """One bounded source fence read, without choice reconstruction/reparse.

    The producer hashes an INT-key row map; preserve its numeric key ordering.
    This digest is distinct from both canonical pre-seal source identity and
    the cart DTO digest. Admin may reuse its existing complete values rows.
    """
    from types import SimpleNamespace
    from management.models import InstagramBotMessage
    from management.services.ig_commerce_projection import _source_fence_row
    capture = detached_payload(capture)
    fence = capture.get("fence") or {}
    identities = fence.get("source_ids")
    if (not isinstance(identities, list) or len(identities) > 64
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in identities)
            or identities != sorted(set(identities))):
        raise TurnContextError("source_cart_fence_invalid")
    if source_rows is None:
        source_rows = list(InstagramBotMessage.objects.filter(pk__in=identities).only(
            "pk", "client_id", "sender_id", "role", "source", "status", "text", "provider_namespace",
            "provider_created_at", "created_at", "mid", "reply_to_provider_message_id", "quick_reply_payload",
            "attachments", "attachment_media"))
    rows = [SimpleNamespace(**row) if isinstance(row, Mapping) else row for row in source_rows]
    if {row.pk for row in rows} != set(identities) or len(rows) != len(identities):
        raise TurnContextError("source_cart_sources_changed")
    actual = {row.pk: _source_fence_row(row) for row in rows}
    digest = hashlib.sha256(json.dumps(actual, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    if digest != fence.get("source_digest"):
        # Historical captures hashed the complete raw media JSON. Accept only
        # their exact original row image; never rebase an old immutable fence
        # onto a changed source or infer which inspection was present then.
        legacy = {row.pk: _source_fence_row(row, legacy_raw_media=True) for row in rows}
        legacy_digest = hashlib.sha256(json.dumps(legacy, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if legacy_digest != fence.get("source_digest"):
            raise TurnContextError("source_cart_sources_changed")
    return True


def _stamp(value):
    value = value if isinstance(value, datetime) else parse_datetime(str(value or ""))
    if value is None or timezone.is_naive(value):
        raise TurnContextError("capture_watermark_invalid")
    return value


def _source_time(source):
    if source.get("provider_created_at"):
        return _stamp(source["provider_created_at"]), "provider"
    if source.get("observed_created_at"):
        return _stamp(source["observed_created_at"]), "local_ingest"
    if "observed_created_at" not in source:
        raise TurnContextError("legacy_sealed_time_unavailable")
    raise TurnContextError("capture_watermark_invalid")


def _scope(boundary):
    return {key: boundary[key] for key in (*SCOPE_KEYS, "order_id")}


def _publication(publication):
    if isinstance(publication, dict):
        result = {key: publication.get(key) for key in ("id", "version", "hash")}
    else:
        result = dict(id=getattr(publication, "publication_id", None),
                      version=getattr(publication, "version", None),
                      hash=getattr(publication, "snapshot_hash", None))
    if not result["id"] or not result["version"] or not _HASH.fullmatch(str(result["hash"] or "")):
        raise TurnContextError("publication_binding_missing")
    return result


def _capture_sources(revision, client):
    from management.models import IgTurnRevisionSource
    from management.services.ig_turn_revisions import _copied_source_payloads

    if revision.active_slot != 1 or revision.state not in {"sealed", "claimed"}:
        raise TurnContextError("capture_revision_not_owned")
    snapshot = revision.bundle_snapshot
    if not isinstance(snapshot, dict) or capture_digest(snapshot) != revision.snapshot_digest:
        raise TurnContextError("sealed_revision_digest_changed")
    sources = deepcopy(snapshot.get("sources"))
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise TurnContextError("sealed_source_count_invalid")
    ids = [row.get("message_id") for row in sources if isinstance(row, dict)]
    if len(ids) != len(sources) or len(set(ids)) != len(ids) or revision.source_count != len(ids):
        raise TurnContextError("sealed_source_window_changed")
    rows = list(IgTurnRevisionSource.objects.filter(revision_id=revision.pk,
        message_id__in=ids).select_related("message").order_by("ordinal", "id")[:MAX_SOURCES + 1])
    if [row.message_id for row in rows] != ids:
        raise TurnContextError("sealed_source_window_changed")
    namespace = rows[0].source_namespace
    if not namespace:
        raise TurnContextError("source_namespace_unknown")
    canonical = _copied_source_payloads(rows)
    for source, row, original in zip(sources, rows, canonical, strict=True):
        message = row.message
        # source_digest is pre-seal identity: media fields deliberately do not
        # enter its digest again. The exact seal has its own whole-list digest.
        if (any(source.get(key) != value for key, value in original.items()
                if key not in {"discovered_media", "text_chars", "media_part_count"})
            or source.get("source_digest") != row.source_digest
            or not _HASH.fullmatch(str(row.source_digest))
            or source.get("source_namespace") != namespace or row.source_namespace != namespace
            or source.get("role") != "user" or row.role != "user"
            or message.client_id != client.pk or message.role != "user"
            or message.provider_namespace != namespace or message.sender_id != client.igsid
            or message.source not in {"webhook", "poll"}
            # Manual/recovery successors legitimately retain DONE sources.
            # Ownership, authorization and dispatch remain revision/CAS duties;
            # source status alone neither grants nor withdraws that ownership.
            or message.status == "failed" or message.send_state
            or source.get("text") != row.text or row.text != message.text
            or source.get("provider_message_id") != row.provider_message_id or row.provider_message_id != message.mid
            or source.get("ordinal") != row.ordinal
            or source.get("provider_created_at") != (row.provider_created_at.isoformat() if row.provider_created_at else "")
            or row.provider_created_at != message.provider_created_at
            or ("observed_created_at" in source and source["observed_created_at"] != message.created_at.isoformat())
            or source.get("referral", {}) != row.referral
            or source.get("quick_reply_payload", "") != row.quick_reply_payload
            or row.quick_reply_payload != message.quick_reply_payload
            or source.get("reply_to_provider_message_id", "") != row.reply_to_provider_message_id
            or row.reply_to_provider_message_id != message.reply_to_provider_message_id):
            raise TurnContextError("sealed_source_scope_changed")
    watermark = max((_source_time(source)[0], source["message_id"]) for source in sources)
    return sources, namespace, dict(event_at=watermark[0].isoformat(), message_id=watermark[1])


def _media(collection, revision, sources):
    if collection is None or collection.readiness not in {"ready", "no_media", "partial", "unavailable"}:
        raise TurnContextError("media_collection_unavailable")
    parts = collection.parts
    binding = collection.binding or {}
    if (len(parts) > MAX_MEDIA_PARTS or binding.get("revision_id") != revision.pk
        or binding.get("revision_snapshot_digest") != revision.snapshot_digest
        or len(binding.get("items") or []) != len(parts)):
        raise TurnContextError("media_binding_invalid")
    sealed = {(source["message_id"], part.get("source_part_id")): part
              for source in sources for part in source.get("media_parts") or []}
    captured = []
    for index, (part, item) in enumerate(zip(parts, binding["items"], strict=True)):
        own = sealed.get((part.source_message_id, part.source_part_id)) or {}
        if (part.inline_index != index or item.get("inline_index") != index
            or item.get("source_message_id") != part.source_message_id
            or item.get("source_part_id") != part.source_part_id
            or item.get("mime") != part.mime or item.get("content_hash") != part.content_hash
            or item.get("bytes") != part.byte_length or part.byte_length != len(part.data)
            or hashlib.sha256(part.data).hexdigest() != part.content_hash
            or own.get("capture_outcome") != "owned" or own.get("content_hash") != part.content_hash):
            raise TurnContextError("media_binding_invalid")
        captured.append(dict(source_message_id=part.source_message_id, mime=part.mime,
                             sha256=part.content_hash, validated=True))
    return {"parts": captured}


def _commerce(sources, plan, scope, *, purchases_count=0):
    from management.services.ig_commerce_turns import parse_turn

    result = dict(scope=scope, purchases_count=max(0, int(purchases_count)), candidate_product_ids=[])
    requests = [parse_turn(source.get("text") or "") for source in sources]
    for key in ("checkout_requested", "reset_requested", "support_requested", "new_purchase_requested",
                "exchange_requested", "personalized_fit_requested", "custom_print_requested", "comparison_requested"):
        result[key] = any(getattr(request, key) for request in requests)
    recipients = [request.recipient_id for request in requests if request.recipient_id]
    result["recipient_switch"] = bool(recipients and recipients[-1] != scope["recipient_id"])
    for request in requests:
        if request.exact_product_id:
            result["candidate_product_ids"].append(request.exact_product_id)
        if request.pending_clarification:
            result["pending_clarification"] = request.pending_clarification
        if request.semantic_constraints:
            result["semantic_constraints"] = dict(request.semantic_constraints)
        if request.garment_type:
            result["garment_type"] = request.garment_type
    result["exact_product_id"] = plan.choices.get("product_id")
    return result


def _referral(sources, scope):
    from management.models import BotAdCampaign

    refs = [(source, source.get("referral")) for source in sources
        if isinstance(source.get("referral"), dict)
        and any(str(source["referral"].get(key) or "").strip() for key in ("ref", "ad_id", "source", "type"))]
    result = dict(scope=scope, status="unknown", present=False)
    if not refs:
        return result
    source, ref = refs[-1]
    result.update(present=True, source_message_id=source["message_id"], status="unresolved")
    if not isinstance(ref, dict):
        return result
    query = Q()
    if ref.get("ad_id"):
        query |= Q(ad_id=ref["ad_id"])
    if ref.get("ref"):
        query |= Q(ref=ref["ref"])
    if not query:
        return result
    matches = list(BotAdCampaign.objects.filter(query, is_active=True).values("pk", "product_id", "theme")[:2])
    result.update(mapping_active=bool(matches), mapping_unique=len(matches) == 1)
    if len(matches) == 1:
        row = matches[0]
        result.update(status="resolved", mapping_id=row["pk"], product_id=row["product_id"], theme=row["theme"])
    return result


def _signals(client, boundary, captured_at):
    from management.models import IgConversationSignal

    scope, watermark = _scope(boundary), boundary["watermark"]
    rows = list(IgConversationSignal.objects.filter(client_id=client.pk,
        message__client_id=client.pk, message__provider_namespace=boundary["source_namespace"],
        message__role="user", message_id__gte=boundary["reset_floor"],
        message_id__lte=max(boundary["source_ids"]), created_at__lte=captured_at,
        message__provider_created_at__lte=_stamp(watermark["event_at"])).values(
            "pk", "message_id", "signal_type", "value", "confidence", "created_at",
            "message__provider_created_at").order_by("-message_id", "-pk")[:MAX_SIGNALS + 1])
    if len(rows) > MAX_SIGNALS:
        return dict(scope=scope, omission_reason="signal_count_budget", items=[])
    return dict(scope=scope, items=[dict(id=row["pk"], source_message_id=row["message_id"],
        kind=row["signal_type"], value=row["value"], confidence=str(row["confidence"]),
        event_at=row["message__provider_created_at"].isoformat()) for row in rows
        if (_stamp(row["message__provider_created_at"]), row["message_id"]) <=
           (_stamp(watermark["event_at"]), watermark["message_id"])])


def _memory(client, boundary, *, sealed_sources=(), history=()):
    """Capture one current head at this watermark, preserving its reader contract."""
    from management.services.ig_memory_producer import read_memory_summary

    scope = _scope(boundary)
    result = dict(scope=scope, reason="narrative_empty")
    text, proof = client.memory_summary or "", client.memory_provenance or {}
    if not isinstance(proof, dict):
        return dict(scope=scope, reason="narrative_integrity_invalid")
    if isinstance(proof, dict) and proof.get("version") == "captured-memory.timeline.v2":
        from management.services.ig_memory_producer import read_memory_timeline
        read = read_memory_timeline(client, boundary=boundary, sealed_sources=sealed_sources, history=history)
        return dict(scope=scope, text=read.text, reason=read.reason,
                    provenance=deepcopy(read.provenance or {}))
    if not text.strip():
        return result
    result["reason"] = "narrative_integrity_invalid"
    capture = proof.get("capture") or {}
    try:
        captured_scope = capture["scope"]
        expected = {key: scope[key] for key in SCOPE_KEYS}
        expected["namespace"] = expected.pop("source_namespace")
        expected["erasure_at"] = expected.pop("erasure_epoch")
        if captured_scope != expected:
            result["reason"] = "narrative_scope_changed"
            return result
        rows = capture["sources"]
        target = capture["target"]
        bound = (_stamp(boundary["watermark"]["event_at"]), boundary["watermark"]["message_id"])
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MEMORY_SOURCES:
            result["reason"] = "narrative_source_budget"
            return result
        if ((_stamp(target["event_at"]), target["message_id"]) > bound
            or any(not boundary["reset_floor"] <= row["message_id"] <= max(boundary["source_ids"])
                   or (_stamp(row["event_at"]), row["message_id"]) > bound for row in rows)):
            result["reason"] = "narrative_after_sealed_watermark"
            return result
        if not _HASH.fullmatch(str(capture.get("generation_input_digest") or "")):
            return result
        # The canonical reader owns current-head, source and namespace checks,
        # including exact SENT human-reply receipts resolved in one batch.
        read = read_memory_summary(client)
        if read.reason != "current":
            return dict(scope=scope, reason=read.reason)
        current_proof = read.provenance or {}
        # A racing head is not retrofitted to the earlier client/scope capture.
        if current_proof != proof or read.text != text:
            return dict(scope=scope, reason="narrative_head_changed")
        return dict(scope=scope, text=read.text, reason="current", provenance=deepcopy(current_proof))
    except (KeyError, TypeError, ValueError):
        return result


def _selection(plan, scope):
    supplied = getattr(plan, "source_selection", None)
    if isinstance(supplied, dict) and supplied.get("schema") == "source-selection.v1":
        return dict(scope=scope, capture=deepcopy(supplied))
    return dict(scope=scope, capture={}, omission_reason="selection_capture_missing")


def _history(client, boundary, sources, supplied):
    from management.models import InstagramBotMessage
    from management.services.ig_revision_conversation_context import historical_message_text

    rows = []
    if supplied is not None:
        if not isinstance(supplied, (list, tuple)) or len(supplied) > MAX_HISTORY:
            raise TurnContextError("history_capture_overflow")
        if any(not isinstance(row, dict) or "message_id" not in row for row in supplied):
            return [], "history_identity_unavailable"
        rows = deepcopy(list(supplied))
        proofs = {row.pk: row for row in InstagramBotMessage.objects.filter(client_id=client.pk,
            sender_id=client.igsid, provider_namespace=boundary["source_namespace"],
            pk__gte=boundary["reset_floor"], pk__lt=min(boundary["source_ids"]),
            pk__in=[row["message_id"] for row in rows])[:MAX_HISTORY]}
        for item in rows:
            row = proofs.get(item["message_id"])
            manager = item.get("role") == "manager"
            if (row is None or row.role != item.get("role") or row.provider_created_at is None
                or item.get("event_at") != row.provider_created_at.isoformat()
                or item.get("scope") != _scope(boundary)
                or item.get("text") != historical_message_text(row, manager=manager)
                or (manager and not (item.get("receipt_confirmed") is True and row.status == "done"
                    and row.send_state not in {"unknown", "sending", "failed"}
                    and (row.provider_message_id or (row.source in {"webhook", "poll", "echo"} and row.mid))))
                or (row.role == "model" and not (row.status == "done" and row.send_state == "sent" and row.provider_message_id))):
                return [], "history_source_unproven"
    else:
        previous = list(InstagramBotMessage.objects.filter(client_id=client.pk,
            sender_id=client.igsid, provider_namespace=boundary["source_namespace"],
            pk__gte=boundary["reset_floor"], pk__lt=min(boundary["source_ids"]),
            provider_created_at__lte=_stamp(boundary["watermark"]["event_at"]),
            role__in=("user", "model", "manager")).exclude(status="failed").order_by("-pk")[:MAX_HISTORY])
        for row in reversed(previous):
            if not row.text.strip():
                continue
            manager = row.role == "manager"
            if manager and not (row.status == "done" and row.send_state not in {"unknown", "sending", "failed"}
                    and bool(row.provider_message_id or (row.source in {"webhook", "poll", "echo"} and row.mid))):
                continue
            if row.role == "model" and not (row.status == "done" and row.send_state == "sent" and row.provider_message_id):
                continue
            if row.role == "user" and row.source not in {"webhook", "poll"}:
                continue
            rows.append(dict(message_id=row.pk, event_at=row.provider_created_at.isoformat(),
                role=row.role, text=historical_message_text(row, manager=manager), scope=_scope(boundary),
                **({"receipt_confirmed": True} if manager else {})))
    return rows, ""


def _provider_history(history, sources):
    result = [dict(role="user" if row["role"] == "manager" else row["role"], text=row["text"],
                   message_id=row["message_id"]) for row in history]
    current = [{key: source.get(key) or "" for key in ("role", "text", "provider_created_at",
        "reply_to_provider_message_id", "quick_reply_payload")} | dict(source_message_id=source["message_id"],
        referral=source.get("referral") or {}, time_origin=_source_time(source)[1]) |
        ({"observed_created_at": source["observed_created_at"]} if "observed_created_at" in source else {}) for source in sources]
    result.append(dict(role="user", text="[CURRENT CUSTOMER BUNDLE: quoted customer data]\n" +
        json.dumps(current, ensure_ascii=False, separators=(",", ":"))))
    return tuple(result)


def _capture_revision_context(revision, *, generation_boundary, collection, settings_row,
                             publication, history=None, now=None):
    """Capture once; mandatory failures raise finite TurnContextError codes.

    Optional failures become named omissions. Caller owns the post-capture CAS
    and must halt the unified request when a mandatory capture fails.
    """
    from management.models import IgClient, IgCommercialEpisode, IgCommerceSelectionSession, IgFunnelResetAudit

    captured_at = now or timezone.now()
    plan = getattr(generation_boundary, "response_plan", None)
    if plan is None:
        raise TurnContextError("response_plan_capture_missing")
    client = IgClient.objects.filter(pk=revision.client_id).first()
    if client is None:
        raise TurnContextError("capture_client_missing")
    if client.privacy_erasure_started_at or revision.erasure_started_at_snapshot:
        raise TurnContextError("client_erasing")
    if (client.reply_permission_epoch != revision.permission_epoch
        or getattr(generation_boundary, "settings_epoch", None) != settings_row.reply_permission_epoch):
        raise TurnContextError("capture_permission_changed")
    sources, namespace, watermark = _capture_sources(revision, client)
    reset = IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values("pk", "reset_after_message_id").first() or {}
    source_cart = detached_payload(getattr(plan, "source_cart_capture", None) or {})
    canonical_cart = source_cart.get("schema") == "source-selections.v1" and source_cart.get("status") == "captured"
    # Current production uses the already captured cart. The single-line read
    # below is compatibility coverage only for older plans without that DTO.
    session = None if canonical_cart else IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1,
        commercial_episode_id=client.current_commercial_episode_id).order_by("-generation").first()
    line = {}
    episode = (IgCommercialEpisode.objects.filter(pk=client.current_commercial_episode_id,
        client_id=client.pk).values("pk", "intended_order_id").first() if client.current_commercial_episode_id else None)
    if canonical_cart:
        rows = source_cart.get("lines") or []
        index = source_cart.get("active_index")
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(rows):
            line = rows[index]
    elif session is not None:
        if plan.scope.get("session_id") and (plan.scope["session_id"] != session.pk
            or plan.scope.get("revision") != session.revision or plan.scope.get("generation") != session.generation):
            raise TurnContextError("capture_selection_changed")
        lines, index = session.lines or [], int(session.active_index or 0)
        if 0 <= index < len(lines) and isinstance(lines[index], dict):
            line = lines[index]
    boundary = dict(client_id=client.pk, revision_id=revision.pk, source_namespace=namespace,
        source_ids=[source["message_id"] for source in sources],
        source_digests={str(source["message_id"]): source["source_digest"] for source in sources},
        sealed_sources_digest=capture_digest(sources), watermark=watermark, source_watermark=watermark,
        reset_id=reset.get("pk"), reset_floor=int(reset.get("reset_after_message_id") or 0) + 1,
        erasure_epoch="", permission_epoch=revision.permission_epoch,
        settings_permission_epoch=settings_row.reply_permission_epoch, publication=_publication(publication),
        episode_id=client.current_commercial_episode_id, order_id=episode.get("intended_order_id") if episode else None,
        line_id=str(line.get("line_id") or ""), recipient_id=str(line.get("recipient_id") or "self"))
    if canonical_cart:
        boundary.update(selection_session_id=source_cart.get("session_id"),
            selection_generation=source_cart.get("generation"), selection_revision=source_cart.get("selection_revision"),
            selection_line_count=len(source_cart.get("lines") or []), source_cart_capture_digest=source_cart.get("capture_digest"))
        source_cart = validate_source_cart_capture(source_cart, boundary)
        if plan.scope.get("session_id") is not None and any(plan.scope.get(key) != source_cart.get(value)
                for key, value in (("session_id", "session_id"), ("generation", "generation"), ("revision", "selection_revision"))):
            raise TurnContextError("capture_selection_changed")
    else:
        count = len(session.lines or []) if session is not None else 0
        if count > 1:
            raise TurnContextError("source_cart_unavailable")
        boundary["selection_line_count"] = count
        source_cart = {"schema": "source-selections.v1", "status": "unavailable", "coverage_complete": False,
            "reason": source_cart.get("reason") or "legacy_single_line_capture", "lines": []}
    boundary["source_time_origins"] = {str(source["message_id"]): _source_time(source)[1] for source in sources}
    boundary["watermark_time_origin"] = boundary["source_time_origins"][str(watermark["message_id"])]
    if any(source["message_id"] < boundary["reset_floor"] for source in sources):
        raise TurnContextError("sealed_source_before_reset")
    scope = _scope(boundary)
    omissions = []
    def optional(identity, capture):
        try:
            return capture()
        except Exception:
            reason = identity + "_capture_unavailable"
            omissions.append(dict(block_id="state:" + identity, reason=reason))
            return dict(scope=scope, omission_reason=reason)
    commerce = _commerce(sources, plan, scope, purchases_count=client.purchases_count)
    referral = optional("referral", lambda: _referral(sources, scope))
    def timing_capture():
        from management.services.ig_revision_conversation_context import conversation_timing_guidance
        return dict(scope=scope, guidance=conversation_timing_guidance(revision))
    timing = optional("timing", timing_capture)
    components = dict(source_selection=(dict(scope=scope, capture=deepcopy(line.get("source_selection") or {}))
        if canonical_cart else _selection(plan, scope)),
        signals=optional("signals", lambda: _signals(client, boundary, captured_at)))
    components["source_cart"] = dict(scope=scope, capture=source_cart)
    readiness = getattr(plan, "readiness_snapshot", None)
    if isinstance(readiness, dict) and readiness:
        components["readiness"] = dict(scope=scope, capture=deepcopy(readiness))
    # Capture agreement/receipt and exact episode payment once using the same
    # source-verifying read model as admin. The sealed watermark excludes later
    # messages; optional context failures never fabricate payment completion.
    def payment_context_capture():
        from management.services.ig_admin_state_capture import capture_payment_context
        from management.services.ig_client_state_card import CAPTURE_SCOPE_KEYS
        prior = getattr(plan, "payment_context_snapshot", None)
        prior_boundary = getattr(plan, "payment_context_boundary", None)
        prior_fence = getattr(plan, "payment_context_fence", "")
        if prior_fence and isinstance(prior, dict) and isinstance(prior_boundary, dict):
            if any(prior_boundary.get(key) != boundary.get(key) for key in (*CAPTURE_SCOPE_KEYS, "source_watermark")):
                raise TurnContextError("payment_context_scope_changed")
            first, reason, fence = deepcopy(prior), getattr(plan, "payment_context_reason", ""), prior_fence
        else:
            first, reason, fence = capture_payment_context(boundary, now=captured_at)
        _, second_reason, second_fence = capture_payment_context(boundary, now=captured_at)
        if reason != second_reason or fence != second_fence:
            raise TurnContextError("payment_context_changed")
        return first
    payment_context = optional("payment_context", payment_context_capture)
    # Pure builder components require one full outer capture scope. Retain the
    # original observation/payment scopes inside; never relabel their proof.
    components.update({key: dict(scope=scope, capture=deepcopy(value))
        for key, value in payment_context.items() if key in {"slots", "payment_truth"}})
    components["observation_omissions"] = dict(scope=scope, capture=deepcopy(payment_context.get("omissions") or []))
    omissions.extend({"block_id": "state:" + item["component"], "reason": item["reason"]}
        for item in payment_context.get("omissions") or [])
    try:
        captured_history, history_reason = _history(client, boundary, sources, history)
    except TurnContextError:
        raise
    except Exception:
        captured_history, history_reason = [], "history_capture_unavailable"
    if history_reason:
        omissions.append(dict(block_id="context:history", reason=history_reason))
    memory = optional("memory", lambda: _memory(client, boundary,
        sealed_sources=sources, history=captured_history))
    if omissions:
        components["capture_omissions"] = dict(scope=scope, items=omissions)
    boundary["history_digests"] = {str(row["message_id"]): capture_digest(row) for row in captured_history}
    pinned_until = getattr(settings_row, "pinned_until", None)
    policy = dict(mode=getattr(settings_row, "turn_intelligence_mode", "legacy"),
        gemini_routing_mode=getattr(settings_row, "gemini_routing_mode", "adaptive"),
        pinned_chat_model=getattr(settings_row, "pinned_chat_model", ""),
        pinned_until=pinned_until.isoformat() if pinned_until else None)
    result = build_turn_context(boundary=boundary, sources=sources, captured_at=captured_at,
        history=captured_history, media=_media(collection, revision, sources), commerce=commerce,
        referral=referral, memory=memory, timing=timing, response_plan=plan,
        routing_policy=policy, components=components)
    metadata = detached_payload(result.metadata)
    metadata["source_time_origins"] = boundary["source_time_origins"]
    metadata["watermark_time_origin"] = boundary["watermark_time_origin"]
    if omissions:
        metadata["omitted_blocks"].extend(omissions)
        metadata["readiness_codes"] = sorted(set(metadata["readiness_codes"]) | {item["reason"] for item in omissions})
        result = replace(result, omissions=(*result.omissions, *(_frozen(item) for item in omissions)))
    if canonical_cart:
        # This is a head fence, never another choice projection. In particular
        # a nonactive line edit must not survive just because active CRM fields
        # and the already sealed customer source happen to be unchanged.
        from management.services.ig_commerce_projection import _choice_digest
        current_session = IgCommerceSelectionSession.objects.filter(pk=source_cart["session_id"],
            client_id=client.pk, commercial_episode_id=boundary["episode_id"], open_slot=1, state="open").first()
        if (current_session is None or current_session.generation != source_cart["generation"]
                or current_session.revision != source_cart["selection_revision"]
                or _choice_digest(current_session.snapshot()) != source_cart["fence"]["snapshot_digest"]):
            raise TurnContextError("source_cart_head_changed")
        validate_current_source_cart_sources(source_cart)
    return replace(result, metadata=_frozen(metadata), provider_history=_frozen(_provider_history(captured_history, sources)))


def capture_revision_context(revision, *, generation_boundary, collection, settings_row,
                             publication, history=None, now=None):
    """Public finite-error contract: unknown required failures halt unified."""
    try:
        return _capture_revision_context(revision, generation_boundary=generation_boundary,
            collection=collection, settings_row=settings_row, publication=publication, history=history, now=now)
    except TurnContextError:
        raise
    except Exception:
        raise TurnContextError("revision_context_capture_unavailable") from None
