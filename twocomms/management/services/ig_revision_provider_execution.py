"""Source-owned provider budget across immutable revision successors.

The root receipt is immutable. Actual spend comes only from canonical started
attempts. The sole repair reservation lives on the earliest graph, under the
existing source-message -> graph mutex; provider admission never writes a root
revision while holding that mutex.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import json
import re
import secrets

from django.db import connection, transaction
from django.utils import timezone

from management.models import GeminiRequest, GeminiRequestAttempt, IgClient, IgCustomerTurnRevision, InstagramBotMessage, InstagramBotSettings


MANIFEST_KEY = "provider_execution_manifest"
REFERENCE_KEY = "provider_execution_reference"
REPAIR_KEY = "_provider_repair_reservation"
MAX_HTTP = 8
MAX_SCARCE_HTTP = 2
HORIZON = timedelta(minutes=30)
MAX_LINEAGE_ROWS = 32
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/() -]{1,80}$")


@dataclass(frozen=True)
class ProviderContinuation:
    ready: bool = False
    reason: str = ""
    root_revision_id: int = 0
    manifest: dict = field(default_factory=dict)
    candidate_plan: tuple[dict, ...] = ()
    http_remaining: int = 0
    scarce_remaining: int = 0
    repair_remaining: bool = False
    next_due_at: object = None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _root(head):
    seen = set()
    row = IgCustomerTurnRevision.objects.filter(pk=head.pk).first()
    if row is None:
        return None
    while row.origin in {"auto_refresh", "outage_recovery"}:
        if row.pk in seen or len(seen) >= MAX_LINEAGE_ROWS:
            return None
        seen.add(row.pk)
        row = IgCustomerTurnRevision.objects.filter(pk=row.parent_id).first()
        if row is None or row.client_id != head.client_id or row.snapshot_digest != head.snapshot_digest or row.permission_epoch != head.permission_epoch:
            return None
    return row if row.origin in {"inbound", "manual_resume"} else None


def provider_execution_reference(revision):
    """Copy this reference into every automatic child, without copying state."""
    root = _root(revision)
    if root is None:
        return {}
    manifest = (root.action_receipts or {}).get(MANIFEST_KEY)
    return {"root_revision_id": root.pk, "manifest_digest": manifest["digest"]} if manifest else {}


def _family(root):
    rows, pending = [root], [root.pk]
    while pending:
        children = list(IgCustomerTurnRevision.objects.filter(parent_id__in=pending, origin__in=("auto_refresh", "outage_recovery")).order_by("pk"))
        if len(rows) + len(children) > MAX_LINEAGE_ROWS:
            return []
        if any(row.client_id != root.client_id or row.snapshot_digest != root.snapshot_digest or row.permission_epoch != root.permission_epoch for row in children):
            return []
        rows.extend(children)
        pending = [row.pk for row in children]
    return rows


def _manifest(root):
    value = (root.action_receipts or {}).get(MANIFEST_KEY) or {}
    if not value or value.get("digest") != _digest({key: item for key, item in value.items() if key != "digest"}):
        return {}
    if (value.get("root_revision_id") != root.pk or value.get("snapshot_digest") != root.snapshot_digest
        or value.get("client_id") != root.client_id or value.get("permission_epoch") != root.permission_epoch
        or value.get("max_http") != MAX_HTTP or value.get("max_scarce_http") != MAX_SCARCE_HTTP or value.get("max_repairs") != 1):
        return {}
    return value


def _graphs(root, family):
    source_id = root.bundle_snapshot["sources"][-1]["message_id"]
    keys = [f"ig-revision:{row.pk}" for row in family]
    return GeminiRequest.objects.filter(source_message_id=source_id, client_id=root.client_id, lane="live", source_execution_key__in=keys).order_by("pk")


def _frozen_candidates(raw_plan):
    from management.services.ig_provider_dispatch_budget import ProviderDispatchBudget

    result, seen = [], set()
    for position, raw in enumerate(raw_plan or (), 1):
        if not isinstance(raw, dict):
            return ()
        alias, model, identity = (str(raw.get(name) or "") for name in ("key_name", "model", "project_identity"))
        if not _TOKEN.fullmatch(alias) or not _TOKEN.fullmatch(model) or (identity and not _TOKEN.fullmatch(identity)):
            return ()
        index = raw.get("candidate_index") or position
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 1000 or index in seen:
            return ()
        seen.add(index)
        reason = str(raw.get("skip_reason") or "")
        if reason and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            return ()
        result.append({"candidate_index": index, "key_name": alias, "model": model, "project_identity": identity,
                       "identity_status": str(raw.get("identity_status") or "unknown"), "skip_reason": reason,
                       "scarce": bool(ProviderDispatchBudget._is_scarce_model(model))})
    return tuple(result) if len(result) <= 128 else ()


def _candidate_key(row):
    return row["key_name"], row["model"], row["project_identity"]


def _disposition(candidate, attempts, repair):
    """Reject known bad scopes; transient failures defer this exact pair."""
    defer_until = None
    for attempt in attempts:
        same_model = attempt.model == candidate["model"]
        same_identity = attempt.project_identity == candidate["project_identity"]
        same_alias = attempt.key_name == candidate["key_name"]
        if same_model and (attempt.failure_kind == "invalid_response" or (attempt.http_code == 400 and attempt.failure_kind == "invalid_payload")):
            return "provider_model_schema_rejected", None
        if attempt.http_code == 401 and same_alias:
            return "provider_credential_rejected", None
        if attempt.http_code in {400, 403, 404} and same_model and same_identity:
            return "provider_candidate_rejected", None
        if same_model and same_identity and attempt.provider_started_at and attempt.fsm_state in {"provider_started", "reserved"}:
            return "provider_generation_unresolved", attempt.permit_expires_at
        if same_model and same_identity and (attempt.http_code == 429 or attempt.failure_kind in {"quota_429", "quota", "http_429", "read_timeout", "transport", "http_5xx", "timeout", "empty", "unavailable"}):
            at = (attempt.finished_at or attempt.provider_started_at) + timedelta(seconds=max(45, int(attempt.provider_retry_after_seconds or 0)))
            if attempt.provider_block_until:
                at = max(at, attempt.provider_block_until)
            defer_until = max(defer_until, at) if defer_until else at
    return "", defer_until


def inspect_revision_provider_execution(revision, *, now=None, lock_ledger=False):
    """Read-only continuation classification, also valid after claim expiry."""
    now = now or timezone.now()
    root = _root(revision)
    if root is None:
        return ProviderContinuation(reason="provider_lineage_invalid")
    manifest = _manifest(root)
    if not manifest:
        return ProviderContinuation(reason="provider_manifest_missing", root_revision_id=root.pk)
    try:
        horizon = timezone.datetime.fromisoformat(manifest["horizon_at"])
    except (KeyError, ValueError, TypeError):
        return ProviderContinuation(reason="provider_manifest_invalid")
    if timezone.is_naive(horizon) or now >= horizon:
        return ProviderContinuation(reason="provider_horizon_exhausted", root_revision_id=root.pk, manifest=manifest)
    family = _family(root)
    if not family or revision.pk not in {row.pk for row in family}:
        return ProviderContinuation(reason="provider_lineage_invalid")
    reference = {"root_revision_id": root.pk, "manifest_digest": manifest["digest"]}
    if revision.pk != root.pk and (revision.action_receipts or {}).get(REFERENCE_KEY) != reference:
        return ProviderContinuation(reason="provider_reference_invalid")
    graph_query = _graphs(root, family)
    graphs = list(graph_query.select_for_update() if lock_ledger else graph_query)
    if any(graph.logical_turn_id != graph.source_execution_key for graph in graphs):
        return ProviderContinuation(reason="provider_graph_identity_invalid")
    attempt_query = GeminiRequestAttempt.objects.filter(request_graph_id__in=[graph.pk for graph in graphs], provider_started_at__isnull=False).order_by("pk")
    attempts = list(attempt_query.select_for_update() if lock_ledger else attempt_query)
    by_key = {_candidate_key(item): item for item in manifest["candidates"]}
    scarce = sum(by_key.get((attempt.key_name, attempt.model, attempt.project_identity), {}).get("scarce", True) for attempt in attempts)
    repair = next(((graph.candidate_outcomes or {}).get(REPAIR_KEY) for graph in graphs if (graph.candidate_outcomes or {}).get(REPAIR_KEY)), None)
    remaining = max(0, MAX_HTTP - len(attempts))
    scarce_remaining = max(0, MAX_SCARCE_HTTP - scarce)
    base = {"root_revision_id": root.pk, "manifest": manifest, "http_remaining": remaining,
            "scarce_remaining": scarce_remaining, "repair_remaining": not bool(repair)}
    if not remaining:
        return ProviderContinuation(reason="provider_dispatch_budget", **base)
    candidates, waits = [], []
    for frozen in manifest["candidates"]:
        candidate = dict(frozen)
        code, due = _disposition(candidate, attempts, repair)
        if not candidate["skip_reason"]:
            candidate["skip_reason"] = code or ("scarce_model_budget" if candidate["scarce"] and not scarce_remaining else "")
        if due and due > now and not candidate["skip_reason"]:
            candidate["skip_reason"] = "provider_candidate_wait"
            waits.append(due)
        elif due and due > now and code == "provider_generation_unresolved":
            waits.append(due)
        candidates.append(candidate)
    eligible = any(not item["skip_reason"] for item in candidates)
    due = min(waits) if waits else None
    reason = "" if eligible else "provider_wait" if due and due < horizon else "provider_candidates_exhausted"
    return ProviderContinuation(ready=eligible, reason=reason, candidate_plan=tuple(candidates), next_due_at=due, **base)


def revision_provider_continuation(revision_id, token, *, settings_id, settings_permission_epoch, candidate_plan=None, now=None):
    """Freeze before graph creation; callers may only narrow the original route."""
    from management.services.gemini_accounting_runtime import _RevisionExecution, _revision_execution_valid
    from management.services.ig_revision_outbox import _normal_reply_window_deadline

    now = now or timezone.now()
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values("client_id").first()
    if not identity:
        return ProviderContinuation(reason="revision_missing")
    with transaction.atomic():
        settings_row = InstagramBotSettings.objects.select_for_update().select_related("active_instruction_publication").filter(pk=settings_id).first()
        client = IgClient.objects.select_for_update().filter(pk=identity["client_id"]).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(pk=revision_id).first()
        if not settings_row or not client or not revision:
            return ProviderContinuation(reason="provider_owner_missing")
        root = _root(revision)
        if not root:
            return ProviderContinuation(reason="provider_lineage_invalid")
        source_id = revision.bundle_snapshot.get("sources", [{}])[-1].get("message_id")
        if not _revision_execution_valid(_RevisionExecution(revision.pk, token, settings_id, settings_permission_epoch), source_message_id=source_id, client_id=client.pk, lane="live", logical_turn_id=f"ig-revision:{revision.pk}"):
            return ProviderContinuation(reason="revision_execution_invalid")
        manifest = _manifest(root)
        if not manifest:
            if (root.action_receipts or {}).get(MANIFEST_KEY) or GeminiRequest.objects.filter(source_message_id=source_id, source_execution_key__in=[f"ig-revision:{row.pk}" for row in _family(root)]).exists():
                return ProviderContinuation(reason="provider_manifest_missing")
            candidates = _frozen_candidates(candidate_plan)
            window = _normal_reply_window_deadline(root)
            if not candidates or window is None or window <= now:
                return ProviderContinuation(reason="provider_route_invalid")
            manifest = {"version": 1, "root_revision_id": root.pk, "client_id": client.pk,
                        "snapshot_digest": root.snapshot_digest, "source_message_ids": [row["message_id"] for row in root.bundle_snapshot["sources"]],
                        "permission_epoch": root.permission_epoch, "settings_id": settings_id,
                        "settings_permission_epoch": settings_permission_epoch, "started_at": now.isoformat(),
                        "horizon_at": min(now + HORIZON, window).isoformat(), "max_http": MAX_HTTP,
                        "max_scarce_http": MAX_SCARCE_HTTP, "max_repairs": 1, "candidates": list(candidates),
                        "provider_policy_version": "revision-provider-8-v1",
                        "instruction_publication": {
                            "id": settings_row.active_instruction_publication_id,
                            "version": getattr(settings_row.active_instruction_publication, "version", None),
                            "hash": getattr(settings_row.active_instruction_publication, "snapshot_hash", ""),
                        }}
            manifest["digest"] = _digest(manifest)
            root.action_receipts = {**(root.action_receipts or {}), MANIFEST_KEY: manifest}
            root.save(update_fields=["action_receipts", "updated_at"])
            if revision.pk != root.pk:
                revision.action_receipts = {**(revision.action_receipts or {}), REFERENCE_KEY: {"root_revision_id": root.pk, "manifest_digest": manifest["digest"]}}
                revision.save(update_fields=["action_receipts", "updated_at"])
        if manifest["settings_id"] != settings_id or manifest["settings_permission_epoch"] != settings_permission_epoch:
            return ProviderContinuation(reason="provider_permission_changed")
        continuation = inspect_revision_provider_execution(revision, now=now)
        if candidate_plan is not None and continuation.ready:
            current = {_candidate_key(row): row for row in _frozen_candidates(candidate_plan)}
            narrowed = []
            for row in continuation.candidate_plan:
                candidate = dict(row)
                current_row = current.get(_candidate_key(row))
                if current_row is None or current_row["identity_status"] != row["identity_status"]:
                    candidate["skip_reason"] = "provider_candidate_identity_changed"
                elif current_row["skip_reason"]:
                    candidate["skip_reason"] = current_row["skip_reason"]
                narrowed.append(candidate)
            from dataclasses import replace
            ready = any(not row["skip_reason"] for row in narrowed)
            continuation = replace(continuation, ready=ready, reason="" if ready else "provider_route_unavailable", candidate_plan=tuple(narrowed))
        return continuation


def admit_provider_dispatch_locked(graph, boundary, *, now):
    """Called under source -> graph lock in the provider_started transaction."""
    if not graph.source_execution_key:
        return ""
    if not connection.in_atomic_block:
        return "provider_admission_not_atomic"
    revision = IgCustomerTurnRevision.objects.filter(pk=boundary.observer._revision_execution.revision_id).first()
    if revision is None:
        return "revision_missing"
    from management.services.ig_revision_recovery import _eligibility

    client = IgClient.objects.filter(pk=revision.client_id).first()
    root = _root(revision)
    family = _family(root) if root else []
    if client is None or not family:
        return "provider_lineage_invalid"
    _state, ownership_reason = _eligibility(revision, client, family, now)
    if ownership_reason:
        return ownership_reason
    continuation = inspect_revision_provider_execution(revision, now=now, lock_ledger=True)
    candidate = next((row for row in continuation.candidate_plan if row["candidate_index"] == boundary.candidate_index and row["key_name"] == boundary.key_name and row["model"] == boundary.model and row["project_identity"] == boundary.observer._identity_for(boundary)), None)
    if not candidate:
        return continuation.reason or "provider_candidate_identity_changed"
    if not continuation.http_remaining:
        return "provider_dispatch_budget"
    if candidate["scarce"] and not continuation.scarce_remaining:
        return "scarce_model_budget"
    repair_token = getattr(boundary, "provider_repair_token", "")
    if repair_token:
        root = _root(revision)
        anchor = _graphs(root, _family(root)).first()
        reserved = (anchor.candidate_outcomes or {}).get(REPAIR_KEY) or {}
        if reserved != {"token": repair_token, "request_id": graph.request_id, "candidate_index": boundary.candidate_index, "key_name": boundary.key_name, "model": boundary.model}:
            return "provider_repair_identity_invalid"
        return "" if candidate["skip_reason"] in {"", "provider_model_schema_rejected"} else candidate["skip_reason"]
    return candidate["skip_reason"] or ("" if continuation.ready else continuation.reason)


def reserve_provider_repair(observer, *, key_name, model, candidate_index=0):
    """Reserve once even if payload repair or pre-HTTP preparation later fails."""
    if not observer.source_execution_key:
        return True
    with transaction.atomic():
        graph, _message = observer._lock_canonical_graph()
        if graph is None or graph.terminal_resolution or graph.winner_attempt_id:
            return False
        revision = IgCustomerTurnRevision.objects.filter(pk=observer._revision_execution.revision_id).first()
        if revision is None:
            return False
        continuation = inspect_revision_provider_execution(revision)
        if not continuation.http_remaining or not continuation.repair_remaining:
            return False
        candidate_index = candidate_index or observer.candidate_index(key_name, model)
        candidate = next((row for row in continuation.candidate_plan if row["candidate_index"] == candidate_index and row["key_name"] == key_name and row["model"] == model), None)
        if not candidate or candidate["skip_reason"] != "provider_model_schema_rejected" or (candidate["scarce"] and not continuation.scarce_remaining):
            return False
        root = _root(revision)
        anchor = _graphs(root, _family(root)).select_for_update().first()
        marker = {"token": secrets.token_hex(16), "request_id": graph.request_id, "candidate_index": candidate_index, "key_name": key_name, "model": model}
        anchor.candidate_outcomes = {**(anchor.candidate_outcomes or {}), REPAIR_KEY: marker}
        anchor.save(update_fields=["candidate_outcomes", "updated_at"])
        observer._pending_provider_repair = marker
        return True
