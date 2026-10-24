"""Read-only, bounded adapter from a sealed revision to pure turn intelligence.

No history reconstruction, session bootstrap, provider I/O, or business effects.
The generation boundary's response plan remains the sole selection authority.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
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
    _validate_checkout_holder_sources(capture)
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


def _validate_checkout_holder_sources(capture):
    """Native site edits must point to their exact persisted selection witness."""
    from management.models import IgCommerceSelectionTransition
    from management.services.ig_checkout_cart_amendments import (
        AUTHORITY, SCHEMA, CheckoutAmendmentError, verify_native_transition, validate_native_capture,
    )
    if (capture.get("fence") or {}).get("native_amendment_id"):
        if not validate_native_capture(capture):
            raise TurnContextError("source_cart_hosted_source_changed")
    facts = []
    for line in capture.get("lines") or []:
        for field, fact in (line.get("fields") or {}).items():
            if isinstance(fact, dict) and fact.get("authority") == AUTHORITY:
                facts.append((line, field, fact))
    if not facts:
        return
    identities = {fact.get("source", {}).get("transition_id") for _, _, fact in facts}
    if len(identities) > 64 or any(type(value) is not int or value <= 0 for value in identities):
        raise TurnContextError("source_cart_hosted_source_invalid")
    transitions = {row.pk: row for row in IgCommerceSelectionTransition.objects.filter(
        pk__in=identities, session_id=capture.get("session_id"), checkout_amendment__isnull=False,
    ).select_related("session", "checkout_amendment")}
    if set(transitions) != identities:
        raise TurnContextError("source_cart_hosted_source_changed")
    verified = {}
    for line, field, fact in facts:
        proof = fact.get("source") or {}
        row = transitions[proof["transition_id"]]
        try:
            if row.pk not in verified:
                verified[row.pk] = verify_native_transition(row, historical=True)
            body = verified[row.pk]
        except CheckoutAmendmentError:
            raise TurnContextError("source_cart_hosted_source_changed") from None
        receipt = row.checkout_amendment
        expected = {"amendment_id": str(receipt.pk), "transition_id": row.pk,
            "source_digest": receipt.source_hmac, "source_kind": SCHEMA,
            "authority": AUTHORITY, "observed_at": receipt.created_at.isoformat()}
        scoped = [item for item in body["next_snapshot"]["lines"]
            if item.get("line_id") == line.get("line_id")
            and item.get("recipient_id") == line.get("recipient_id")]
        if (proof != expected or body["client_id"] != capture["scope"]["client_id"]
            or body["episode_id"] != capture["scope"]["episode_id"]
            or body["generation"] != capture["generation"]
            or row.to_revision > capture["selection_revision"] or len(scoped) != 1
            or field not in scoped[0] or fact.get("value") != scoped[0][field]):
            raise TurnContextError("source_cart_hosted_source_changed")


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


def _referral(sources, scope, *, client=None, boundary=None):
    from management.services.ig_ad_context import capture_ad_context

    return capture_ad_context(sources, scope, client=client, boundary=boundary)


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


def _human_history_receipt_times(rows, *, reset_floor):
    """Read exact immutable part receipts for timestamp-less human transcripts."""
    human = [row for row in rows if row.role == "manager" and row.source == "human_reply"
        and row.provider_created_at is None and row.status == "done" and row.send_state == "sent"
        and row.provider_message_id and row.send_completed_at]
    if not human:
        return {}
    from collections import defaultdict
    from django.db.models import Subquery
    from management.ig_human_reply_models import HumanReplyPart
    from management.services.ig_human_reply_delivery import DEFAULT_MAX_CHUNKS, _valid_plan

    owners = HumanReplyPart.objects.filter(
        provider_namespace__in={row.provider_namespace for row in human},
        provider_message_id__in={row.provider_message_id for row in human},
    ).values("command_id")
    cap = MAX_HISTORY * DEFAULT_MAX_CHUNKS
    parts = list(HumanReplyPart.objects.filter(command_id__in=Subquery(owners))
        .select_related("command").order_by("command_id", "ordinal")[:cap + 1])
    if len(parts) > cap:
        return {}
    grouped = defaultdict(list)
    for part in parts:
        grouped[part.command_id].append(part)
    proven = {}
    for command_parts in grouped.values():
        command = command_parts[0].command
        if command.purpose != "human_reply" or not _valid_plan(command, command_parts, current_owner=False):
            continue
        for part in command_parts:
            # Late transcript projection gets a new ID; the immutable context
            # source still belongs to its original conversation reset scope.
            if (part.state != "sent" or not part.provider_message_id or not part.terminal_at
                or not part.provider_started_at or part.provider_started_at > part.terminal_at
                or part.context_message_id_snapshot < reset_floor):
                continue
            for row in human:
                if (row.client_id == part.client_id and row.sender_id == part.recipient_igsid
                    and row.provider_namespace == part.provider_namespace
                    and row.provider_message_id == part.provider_message_id
                    and row.send_idempotency_key == f"human:{part.operation_id_snapshot}:part:{part.ordinal}"
                    and row.text == part.text and row.send_completed_at == part.terminal_at
                    and row.delivery_provider_message_ids == [part.provider_message_id]
                    and not row.delivery_failure_boundary):
                    proven[row.pk] = part.terminal_at
    return proven


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
        human_times = _human_history_receipt_times(proofs.values(), reset_floor=boundary["reset_floor"])
        for item in rows:
            row = proofs.get(item["message_id"])
            manager = item.get("role") == "manager"
            stamp = (row.provider_created_at or human_times.get(row.pk)) if row is not None else None
            if (row is None or row.role != item.get("role") or stamp is None
                or item.get("event_at") != stamp.isoformat()
                or stamp > _stamp(boundary["watermark"]["event_at"])
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
            role__in=("user", "model", "manager")).filter(
                Q(provider_created_at__lte=_stamp(boundary["watermark"]["event_at"]))
                | Q(role="manager", source="human_reply", provider_created_at__isnull=True,
                    send_completed_at__lte=_stamp(boundary["watermark"]["event_at"]),
                    status="done", send_state="sent"),
            ).exclude(status="failed").order_by("-pk")[:MAX_HISTORY])
        human_times = _human_history_receipt_times(previous, reset_floor=boundary["reset_floor"])
        for row in reversed(previous):
            if not row.text.strip():
                continue
            stamp = row.provider_created_at or human_times.get(row.pk)
            if stamp is None:
                continue
            manager = row.role == "manager"
            if manager and not (row.status == "done" and row.send_state not in {"unknown", "sending", "failed"}
                    and bool(row.provider_message_id or (row.source in {"webhook", "poll", "echo"} and row.mid))):
                continue
            if row.role == "model" and not (row.status == "done" and row.send_state == "sent" and row.provider_message_id):
                continue
            if row.role == "user" and row.source not in {"webhook", "poll"}:
                continue
            rows.append(dict(message_id=row.pk, event_at=stamp.isoformat(),
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


@dataclass(frozen=True)
class RevisionRoutingCapture:
    """Request-local routing inputs, shared with later context preparation."""
    revision_id: int
    snapshot_digest: str
    plan_digest: str
    settings_id: int | None
    settings_epoch: int
    captured_at: datetime
    client: object
    inputs: Mapping
    facts: object
    decision: object


def _capture_revision_routing(revision, *, generation_boundary, collection, settings_row,
                              publication, now=None):
    """Capture the canonical route before history/catalog/prompt preparation."""
    from management.models import IgClient, IgCommercialEpisode, IgCommerceSelectionSession, IgFunnelResetAudit

    captured_at = now or timezone.now()
    plan = getattr(generation_boundary, "response_plan", None)
    if plan is None:
        raise TurnContextError("response_plan_capture_missing")
    from management.services.ig_restock_waiting_list_context import waiting_list_owner_query
    client = waiting_list_owner_query(IgClient.objects.filter(pk=revision.client_id)).first()
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
        source_cart = validate_source_cart_capture(source_cart, boundary, native_as_of=captured_at)
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
    referral = optional("referral", lambda: _referral(sources, scope, client=client, boundary=boundary))
    media = _media(collection, revision, sources)
    pinned_until = getattr(settings_row, "pinned_until", None)
    policy = dict(mode=getattr(settings_row, "turn_intelligence_mode", "legacy"),
        gemini_routing_mode=getattr(settings_row, "gemini_routing_mode", "adaptive"),
        pinned_chat_model=getattr(settings_row, "pinned_chat_model", ""),
        pinned_until=pinned_until.isoformat() if pinned_until else None)
    from types import SimpleNamespace
    from management.services.gemini_routing import classify_live_turn
    from management.services.ig_turn_intelligence import build_routing_facts
    facts = build_routing_facts(sources=sources, media=media, commerce=commerce, referral=referral)
    decision = classify_live_turn(facts, settings_obj=SimpleNamespace(
        gemini_routing_mode=policy["gemini_routing_mode"],
        pinned_chat_model=policy["pinned_chat_model"], pinned_until=pinned_until), now=captured_at)
    return RevisionRoutingCapture(revision.pk, revision.snapshot_digest, plan.digest,
        getattr(settings_row, "pk", None), settings_row.reply_permission_epoch, captured_at, client,
        _frozen(dict(sources=sources, boundary=boundary, source_cart=source_cart,
            canonical_cart=canonical_cart, line=line, scope=scope, commerce=commerce,
            referral=referral, omissions=omissions, media=media, routing_policy=policy)), facts, decision)


def capture_revision_routing(revision, *, generation_boundary, collection, settings_row,
                             publication, now=None):
    """Public finite-error contract for the shared, provider-free route capture."""
    try:
        return _capture_revision_routing(revision, generation_boundary=generation_boundary,
            collection=collection, settings_row=settings_row, publication=publication, now=now)
    except TurnContextError:
        raise
    except Exception:
        raise TurnContextError("revision_routing_capture_unavailable") from None


def _capture_revision_context(revision, *, generation_boundary, collection, settings_row,
                             publication, history=None, now=None, routing_capture=None):
    """Reuse the admitted route, then capture optional context outside locks."""
    from management.models import IgCommerceSelectionSession

    routing = routing_capture or capture_revision_routing(revision,
        generation_boundary=generation_boundary, collection=collection,
        settings_row=settings_row, publication=publication, now=now)
    plan = generation_boundary.response_plan
    if (not isinstance(routing, RevisionRoutingCapture)
        or routing.revision_id != revision.pk or routing.snapshot_digest != revision.snapshot_digest
        or routing.plan_digest != plan.digest or routing.settings_id != getattr(settings_row, "pk", None)
        or routing.settings_epoch != settings_row.reply_permission_epoch
        or routing.inputs["boundary"]["publication"] != _publication(publication)
        or (now is not None and now != routing.captured_at)):
        raise TurnContextError("routing_capture_binding_changed")
    captured_at, client = routing.captured_at, routing.client
    values = detached_payload(routing.inputs)
    sources, boundary, source_cart = (values[key] for key in ("sources", "boundary", "source_cart"))
    canonical_cart, line, scope = (values[key] for key in ("canonical_cart", "line", "scope"))
    commerce, referral, omissions = (values[key] for key in ("commerce", "referral", "omissions"))
    def optional(identity, capture):
        try:
            return capture()
        except Exception:
            reason = identity + "_capture_unavailable"
            omissions.append(dict(block_id="state:" + identity, reason=reason))
            return dict(scope=scope, omission_reason=reason)
    def timing_capture():
        from management.services.ig_revision_conversation_context import conversation_timing_guidance
        return dict(scope=scope, guidance=conversation_timing_guidance(revision))
    timing = optional("timing", timing_capture)
    components = dict(source_selection=(dict(scope=scope, capture=deepcopy(line.get("source_selection") or {}))
        if canonical_cart else _selection(plan, scope)),
        signals=optional("signals", lambda: _signals(client, boundary, captured_at)))
    components["source_cart"] = dict(scope=scope, capture=source_cart)
    from management.services.ig_reply_language import response_plan_language_names
    components["language_catalog_names"] = dict(scope=scope,
        names=list(response_plan_language_names(plan)), response_plan_digest=plan.digest)
    components["ad_context"] = deepcopy(referral)
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
    from management.services.ig_payment_reminder_context import SLOT, capture_payment_reminder_context
    reminder_slot = capture_payment_reminder_context(client, boundary=boundary, captured_at=captured_at,
        current_source_cart=source_cart)
    components.setdefault("slots", dict(scope=scope, capture={}))["capture"][SLOT] = reminder_slot
    from management.services.ig_fulfillment_context import SLOT as FULFILLMENT_SLOT, capture_fulfillment_context
    components["slots"]["capture"][FULFILLMENT_SLOT] = capture_fulfillment_context(
        client, boundary=boundary, captured_at=captured_at, current_source_cart=source_cart)
    from management.services.ig_inventory_availability import SLOT as INVENTORY_SLOT, capture_inventory_context
    components["slots"]["capture"][INVENTORY_SLOT] = capture_inventory_context(
        client, boundary=boundary, captured_at=captured_at, current_source_cart=source_cart)
    if client._has_waiting_list:
        from management.services.ig_restock_waiting_list_context import capture_waiting_list_context
        components["slots"]["capture"].update(capture_waiting_list_context(
            client.pk, boundary=boundary, captured_at=captured_at))
    if payment_context.get("checkout_order"):
        components["checkout_order"] = deepcopy(payment_context["checkout_order"])
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
    result = build_turn_context(boundary=boundary, sources=sources, captured_at=captured_at,
        history=captured_history, media=values["media"], commerce=commerce,
        referral=referral, memory=memory, timing=timing, response_plan=plan,
        routing_policy=values["routing_policy"], components=components)
    if result.facts != routing.facts or result.decision != routing.decision:
        raise TurnContextError("routing_capture_decision_changed")
    result = replace(result, facts=routing.facts, decision=routing.decision)
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
                             publication, history=None, now=None, routing_capture=None):
    """Public finite-error contract: unknown required failures halt unified."""
    try:
        return _capture_revision_context(revision, generation_boundary=generation_boundary,
            collection=collection, settings_row=settings_row, publication=publication, history=history,
            now=now, routing_capture=routing_capture)
    except TurnContextError:
        raise
    except Exception:
        raise TurnContextError("revision_context_capture_unavailable") from None
