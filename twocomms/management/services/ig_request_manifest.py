"""Content-free captured request evidence on the existing Gemini graph.

The logical-input digest is not an HTTP-body digest: provider model adapters
and repairs may still change that input. Dispatch capture is a separate pure
helper for the existing attempt record. Neither helper retains payload bodies.
Historical inspection never assembles a prompt from today's customer state.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import json
import re

from django.conf import settings

from management.services.gemini_accounting_contract import RequestPolicyManifestError


SCHEMA_VERSION = "ig-request-context.v1"
DISPATCH_SCHEMA_VERSION = "ig-request-dispatch.v1"
MAX_ITEMS = 256
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_BUDGET_KEYS = {"builder_chars", "history_entries", "source_count", "media_parts", "request_bytes"}
_VIEW_KEYS = {
    "canonical_selection", "response_plan_digest", "memory_head_version",
    "memory_capture_digest", "publication_hash", "routing_policy", "facts_version",
    "state_view_version", "core_version",
    "conversation_agreement", "receipt_observation",
}
_DIGEST_VIEW_KEYS = {"response_plan_digest", "memory_capture_digest", "publication_hash"}
_METADATA_KEYS = {
    "revision_id", "client_id", "source_message_ids", "history_message_ids",
    "reset_floor", "bundle_digest", "builder_version", "effective_mode",
    "selected_block_ids", "omitted_blocks", "readiness_codes", "budgets",
    "view_versions", "media",
}
_CONTEXT_KEYS = _METADATA_KEYS | {
    "schema_version", "payload_stage", "digest_scheme", "context_digest", "request_digest",
}
_DISPATCH_KEYS = {
    "schema_version", "payload_stage", "digest_scheme", "revision_id", "client_id",
    "request_digest", "logical_request_digest", "attempt_index", "model",
}


def _invalid():
    raise RequestPolicyManifestError("policy_manifest_context_invalid", "request context shape or value is invalid")


def _object(value, keys, *, exact=False):
    if not isinstance(value, dict) or (set(value) != keys if exact else set(value) - keys):
        _invalid()
    return value


def _count(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or not (1 if positive else 0) <= value <= 2**63 - 1:
        _invalid()
    return value


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        _invalid()
    return value


def _digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _invalid()
    return value


def _ids(values):
    if not isinstance(values, list) or len(values) > MAX_ITEMS:
        _invalid()
    result = [_count(value, positive=True) for value in values]
    if len(result) != len(set(result)):
        _invalid()
    return result


def _tokens(values, *, codes=False):
    if not isinstance(values, list) or len(values) > MAX_ITEMS:
        _invalid()
    result = [_token(value) for value in values]
    if codes and any(not _CODE.fullmatch(value) for value in result):
        _invalid()
    if len(result) != len(set(result)):
        _invalid()
    return result


def _metadata(value):
    raw = _object(value, _METADATA_KEYS)
    omitted = raw.get("omitted_blocks", [])
    if not isinstance(omitted, list) or len(omitted) > MAX_ITEMS:
        _invalid()
    safe_omitted = []
    for item in omitted:
        item = _object(item, {"block_id", "reason"}, exact=True)
        reason = _token(item["reason"])
        if not _CODE.fullmatch(reason):
            _invalid()
        safe_omitted.append({"block_id": _token(item["block_id"]), "reason": reason})
    if len({item["block_id"] for item in safe_omitted}) != len(safe_omitted):
        _invalid()
    budgets = _object(raw.get("budgets", {}), _BUDGET_KEYS)
    versions = _object(raw.get("view_versions", {}), _VIEW_KEYS)
    media = _object(raw.get("media", {}), {"admitted_part_ids", "omitted_part_ids", "unavailable_part_ids"})
    mode = raw.get("effective_mode", "legacy")
    if mode not in {"legacy", "shadow", "unified"}:
        _invalid()
    result = {
        "revision_id": _count(raw.get("revision_id", 0)),
        "client_id": _count(raw.get("client_id", 0)),
        "source_message_ids": _ids(raw.get("source_message_ids", [])),
        "history_message_ids": _ids(raw.get("history_message_ids", [])),
        "reset_floor": _count(raw.get("reset_floor", 0)),
        "bundle_digest": _digest(raw["bundle_digest"]) if raw.get("bundle_digest") else "",
        "builder_version": _token(raw.get("builder_version", "legacy")),
        "effective_mode": mode,
        "selected_block_ids": _tokens(raw.get("selected_block_ids", [])),
        "omitted_blocks": safe_omitted,
        "readiness_codes": _tokens(raw.get("readiness_codes", []), codes=True),
        "budgets": {key: _count(budgets[key]) for key in sorted(budgets)},
        "view_versions": {
            key: _digest(versions[key]) if key in _DIGEST_VIEW_KEYS else _token(versions[key])
            for key in sorted(versions)
        },
        "media": {key: _tokens(media.get(key, [])) for key in ("admitted_part_ids", "omitted_part_ids", "unavailable_part_ids")},
    }
    if set(result["selected_block_ids"]) & {item["block_id"] for item in safe_omitted}:
        _invalid()
    media_ids = [value for values in result["media"].values() for value in values]
    if len(media_ids) != len(set(media_ids)):
        _invalid()
    if result["revision_id"] and (not result["client_id"] or not result["source_message_ids"] or not result["bundle_digest"]):
        _invalid()
    return result


def _canonical(value):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _invalid()
    if len(data) > MAX_PAYLOAD_BYTES:
        _invalid()
    return data


def _mac(domain, value):
    secret = settings.SECRET_KEY
    if not isinstance(secret, str) or not secret:
        _invalid()
    return hmac.new(secret.encode("utf-8"), domain.encode("ascii") + b"\0" + _canonical(value), hashlib.sha256).hexdigest()


def capture_request_context(*, payload, metadata):
    """Capture bounded metadata and secret-bound logical input, without I/O."""
    safe = _metadata(metadata)
    scope = {key: safe[key] for key in ("revision_id", "client_id", "source_message_ids", "reset_floor")}
    context_digest = _mac("ig-request-context.v1", {"scope": scope, "metadata": safe, "payload": payload})
    request_digest = _mac("ig-request-logical-input.v1", {"scope": scope, "payload": payload})
    return sanitize_request_context({
        **safe, "schema_version": SCHEMA_VERSION, "payload_stage": "logical_input",
        "digest_scheme": "hmac-sha256-django-secret-v1",
        "context_digest": context_digest, "request_digest": request_digest,
    })


def sanitize_request_context(value):
    """Strict extension boundary; reject unknown keys and noncanonical IDs."""
    raw = _object(value, _CONTEXT_KEYS, exact=True)
    if raw["schema_version"] != SCHEMA_VERSION or raw["payload_stage"] != "logical_input" or raw["digest_scheme"] != "hmac-sha256-django-secret-v1":
        _invalid()
    return {
        **_metadata({key: raw[key] for key in _METADATA_KEYS}),
        "schema_version": SCHEMA_VERSION, "payload_stage": "logical_input",
        "digest_scheme": "hmac-sha256-django-secret-v1",
        "context_digest": _digest(raw["context_digest"]), "request_digest": _digest(raw["request_digest"]),
    }


def capture_dispatch_context(*, payload, context, attempt_index, model):
    """Hash the final HTTP input before dispatch; caller persists on attempt."""
    context = sanitize_request_context(context)
    safe = {
        "schema_version": DISPATCH_SCHEMA_VERSION, "payload_stage": "http_dispatch",
        "digest_scheme": "hmac-sha256-django-secret-v1",
        "revision_id": context["revision_id"], "client_id": context["client_id"],
        "logical_request_digest": context["request_digest"],
        "attempt_index": _count(attempt_index, positive=True), "model": _token(model),
    }
    body = payload if isinstance(payload, bytes) else _canonical(payload)
    if len(body) > MAX_PAYLOAD_BYTES:
        _invalid()
    secret = settings.SECRET_KEY
    if not isinstance(secret, str) or not secret:
        _invalid()
    safe["request_digest"] = hmac.new(
        secret.encode("utf-8"), b"ig-request-http-dispatch.v1\0" + _canonical(safe) + b"\0" + body,
        hashlib.sha256,
    ).hexdigest()
    return sanitize_dispatch_context(safe)


def sanitize_dispatch_context(value):
    raw = _object(value, _DISPATCH_KEYS, exact=True)
    if raw["schema_version"] != DISPATCH_SCHEMA_VERSION or raw["payload_stage"] != "http_dispatch" or raw["digest_scheme"] != "hmac-sha256-django-secret-v1":
        _invalid()
    return {
        "schema_version": DISPATCH_SCHEMA_VERSION, "payload_stage": "http_dispatch",
        "digest_scheme": "hmac-sha256-django-secret-v1",
        "revision_id": _count(raw["revision_id"]), "client_id": _count(raw["client_id"]),
        "logical_request_digest": _digest(raw["logical_request_digest"]),
        "request_digest": _digest(raw["request_digest"]),
        "attempt_index": _count(raw["attempt_index"], positive=True), "model": _token(raw["model"]),
    }


def _load_preview_records(graph, owner_id, revision_id):
    from management.models import GeminiRequest, IgClient, IgCustomerTurnRevision

    lookup = {"request_id": graph} if isinstance(graph, str) else {"pk": getattr(graph, "pk", None)}
    request = GeminiRequest.objects.filter(**lookup).first()
    if request is None:
        return None, None, None, []
    owner = IgClient.objects.filter(pk=owner_id).only("pk", "privacy_erasure_started_at").first()
    revision = IgCustomerTurnRevision.objects.filter(pk=revision_id, client_id=owner_id).only(
        "pk", "client_id", "permission_epoch", "erasure_started_at_snapshot", "snapshot_digest", "bundle_snapshot",
    ).first()
    attempts = list(request.attempts.order_by("attempt_index", "pk").values(
        "pk", "request_graph_id", "request_id", "client_id", "logical_turn_id", "source_message_id",
        "attempt_index", "model", "fsm_state", "outcome", "provider_started_at", "winner_claimed",
        "dispatch_manifest", "http_code",
    )[:MAX_ITEMS + 1])
    return request, owner, revision, attempts


def _hidden_context(context):
    safe = deepcopy(context)
    for key in ("context_digest", "request_digest", "bundle_digest"):
        safe.pop(key, None)
    for key in _DIGEST_VIEW_KEYS - {"publication_hash"}:
        safe.get("view_versions", {}).pop(key, None)
    return safe


def actual_request_preview(graph, *, owner_id, revision_id, allow_protected_context=False):
    """Read captured evidence only; authorization must precede this reader.

    Exact prompt replay is deliberately unavailable here. Existing sources may
    survive, but dynamic captured prose is not retained by this manifest. The
    report proves composition and provider outcomes without inventing replay.
    """
    _count(owner_id, positive=True)
    _count(revision_id, positive=True)
    result = {"mode": "actual_request", "reconstruction": "not_reconstructable", "reason": "request_missing", "manifest": {}, "attempts": [], "actual_model": ""}
    request, owner, revision, attempts = _load_preview_records(graph, owner_id, revision_id)
    if request is None:
        return result
    if request.client_id != owner_id:
        result["reason"] = "owner_binding_mismatch"
        return result
    if owner is None or owner.privacy_erasure_started_at is not None:
        result["reason"] = "owner_missing" if owner is None else "privacy_erasure"
        return result
    if revision is None or revision.client_id != owner_id or revision.pk != revision_id:
        result["reason"] = "revision_missing"
        return result
    if revision.erasure_started_at_snapshot is not None:
        result["reason"] = "privacy_erasure"
        return result
    from management.services.gemini_accounting_contract import sanitize_request_policy_manifest

    try:
        safe_policy = sanitize_request_policy_manifest(deepcopy(request.policy_manifest))
        context = safe_policy.pop("request_context", None)
    except (RequestPolicyManifestError, TypeError, ValueError):
        result["reason"] = "manifest_invalid"
        return result
    if context is None:
        result.update(reason="legacy_context_uncaptured", manifest=safe_policy)
        return result
    expected_turn = f"ig-revision:{revision_id}"
    source_ids = [source.get("message_id") for source in (revision.bundle_snapshot or {}).get("sources", [])]
    if (
        request.logical_turn_id != expected_turn or request.source_execution_key != expected_turn
        or context["revision_id"] != revision_id or context["client_id"] != owner_id
        or context["source_message_ids"] != source_ids or request.source_message_id not in source_ids
        or not source_ids
        or not context["bundle_digest"] or context["bundle_digest"] != revision.snapshot_digest
        or hashlib.sha256(_canonical(revision.bundle_snapshot)).hexdigest() != revision.snapshot_digest
    ):
        result["reason"] = "revision_binding_mismatch"
        return result
    if len(attempts) > MAX_ITEMS or any(
        row["request_graph_id"] != request.pk or row["request_id"] != request.request_id
        or row["client_id"] != owner_id or row["logical_turn_id"] != expected_turn
        or row["source_message_id"] != request.source_message_id
        for row in attempts
    ):
        result["reason"] = "attempt_binding_mismatch"
        return result
    safe_attempts = []
    for row in attempts:
        dispatch = row.get("dispatch_manifest") or {}
        if dispatch:
            try:
                dispatch = sanitize_dispatch_context(dispatch)
            except RequestPolicyManifestError:
                result["reason"] = "dispatch_manifest_invalid"
                return result
            if (
                dispatch["client_id"] != owner_id or dispatch["revision_id"] != revision_id
                or dispatch["attempt_index"] != row["attempt_index"] or dispatch["model"] != row["model"]
                or dispatch["logical_request_digest"] != context["request_digest"]
            ):
                result["reason"] = "dispatch_binding_mismatch"
                return result
            if not allow_protected_context:
                dispatch.pop("request_digest", None)
                dispatch.pop("logical_request_digest", None)
        safe_attempts.append({
            "attempt_index": row["attempt_index"], "model": row["model"],
            "state": row["fsm_state"], "outcome": row["outcome"],
            # Admission may expire before POST; this is the recorded accounting
            # phase, never an assertion that an HTTP request reached Gemini.
            "provider_started": row["provider_started_at"] is not None,
            "http_code": row.get("http_code"), "dispatch_manifest": dispatch,
            "winner": row["pk"] == request.winner_attempt_id and row["winner_claimed"] is True,
        })
    winner = next((row for row in safe_attempts if row["winner"]), None)
    result.update(
        reason="full_payload_not_retained", capture_status="captured_metadata_available",
        payload_stage="logical_input", request_id=request.request_id,
        terminal_resolution=request.terminal_resolution, terminal_reason=request.terminal_reason,
        manifest={**safe_policy, "request_context": context if allow_protected_context else _hidden_context(context)},
        attempts=safe_attempts, actual_model=winner["model"] if winner else "",
    )
    return result


def compare_request_previews(actual, hypothetical):
    """Keep public policy parity separate from protected context parity."""
    left = actual.get("manifest") or {}
    right = hypothetical.get("manifest") or {}
    left_hash, right_hash = left.get("content_hash"), right.get("content_hash")
    left_context = left.get("request_context") or {}
    right_context = right.get("request_context") or {}
    left_digest, right_digest = left_context.get("context_digest"), right_context.get("context_digest")
    return {
        "policy_equal": hmac.compare_digest(left_hash, right_hash) if isinstance(left_hash, str) and isinstance(right_hash, str) else None,
        "context_equal": hmac.compare_digest(left_digest, right_digest) if isinstance(left_digest, str) and isinstance(right_digest, str) else None,
        "actual_mode": actual.get("mode", "actual_request"),
        "preview_mode": hypothetical.get("mode", "next_turn_preview"),
    }
