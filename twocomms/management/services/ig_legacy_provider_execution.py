"""Durable provider budget shared by a legacy live turn and its recovery jobs."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import json
import re
import secrets

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    GeminiRequest,
    GeminiRequestAttempt,
    IgAiReplyRecoveryJob,
    IgClient,
    InstagramBotMessage,
)
from management.services.ig_revision_provider_execution import ProviderContinuation


MANIFEST_KEY = "_legacy_provider_manifest"
REPAIR_KEY = "_provider_repair_reservation"
MAX_HTTP = 8
MAX_SCARCE_HTTP = 2
HORIZON = timedelta(minutes=30)
MAX_CHILD_GRAPHS = 8
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/() -]{1,80}$")


@dataclass(frozen=True)
class LegacyProviderExecution:
    job_id: int
    recovery_token: str
    automation_token: str
    client_id: int
    source_message_id: int
    logical_turn_id: str
    source_execution_key: str
    root_graph_id: int


@dataclass(frozen=True)
class LegacyRootExecution:
    automation_token: str
    client_id: int
    source_message_id: int
    logical_turn_id: str
    root_graph_id: int


@dataclass(frozen=True)
class LegacyProviderFailureDisposition:
    state: str
    reason: str
    consume_attempt: bool = True
    retry_at: object = None
    horizon_at: object = None


_TERMINAL_REASONS = frozenset({
    "provider_dispatch_budget",
    "scarce_model_budget",
    "provider_horizon_exhausted",
    "provider_candidates_exhausted",
    "legacy_manifest_missing",
    "legacy_manifest_invalid",
    "legacy_graph_invalid",
    "legacy_root_missing",
    "legacy_root_ambiguous",
    "legacy_holding_root_mismatch",
    "legacy_execution_invalid",
    "legacy_route_invalid",
})


def classify_legacy_provider_failure(
    reason, *, next_due_at=None, horizon_at=None, now=None,
):
    """Return one finite recovery disposition shared by live and recovery."""
    now = now or timezone.now()
    reason = str(reason or "legacy_execution_invalid")[:80]
    if reason in _TERMINAL_REASONS:
        return LegacyProviderFailureDisposition(
            "terminal", reason, horizon_at=horizon_at,
        )
    if reason == "provider_wait":
        if (
            next_due_at is not None
            and horizon_at is not None
            and now < next_due_at < horizon_at
        ):
            return LegacyProviderFailureDisposition(
                "wait", reason, consume_attempt=False,
                retry_at=next_due_at, horizon_at=horizon_at,
            )
        return LegacyProviderFailureDisposition(
            "terminal", reason, horizon_at=horizon_at,
        )
    # Claim/source changes and incomplete legacy discovery get the existing
    # bounded job retry accounting; they never receive a fresh provider budget.
    return LegacyProviderFailureDisposition(
        "retry", reason, consume_attempt=True, horizon_at=horizon_at,
    )


def continuation_horizon(continuation):
    try:
        value = (continuation.manifest or {}).get("horizon_at")
        horizon = timezone.datetime.fromisoformat(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return horizon if not timezone.is_naive(horizon) else None


def classify_legacy_provider_continuation(continuation, *, now=None):
    return classify_legacy_provider_failure(
        getattr(continuation, "reason", ""),
        next_due_at=getattr(continuation, "next_due_at", None),
        horizon_at=continuation_horizon(continuation),
        now=now,
    )


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def _execution_key(job, recovery_token) -> str:
    # A non-consuming provider wait can reclaim the same job attempt number.
    # Bind the graph to this lease without persisting the lease capability.
    claim_digest = hashlib.sha256(str(recovery_token).encode()).hexdigest()[:20]
    return f"ig-recovery:{job.pk}:{claim_digest}"[:64]


def _target_is_current(job, source_message_id: int) -> bool:
    from management.services.ig_ai_reply_recovery import effective_target_id

    return int(effective_target_id(job) or 0) == int(source_message_id or 0)


def _root_graphs(*, client_id, source_message_id, logical_turn_id):
    return GeminiRequest.objects.filter(
        client_id=client_id,
        source_message_id=source_message_id,
        logical_turn_id=logical_turn_id,
        lane="live",
        source_execution_key="",
        parent_request__isnull=True,
    ).order_by("pk")


def validate_legacy_root_execution(execution, *, now=None) -> bool:
    if not isinstance(execution, LegacyRootExecution) or not execution.automation_token:
        return False
    now = now or timezone.now()
    if not IgClient.objects.filter(
        pk=execution.client_id,
        automation_lease_token=execution.automation_token,
        automation_lease_until__gt=now,
    ).exists():
        return False
    source = InstagramBotMessage.objects.filter(
        pk=execution.source_message_id,
        client_id=execution.client_id,
        role=InstagramBotMessage.Role.USER,
        status=InstagramBotMessage.Status.PROCESSING,
        processing_started_at__isnull=False,
    ).first()
    if source is None or InstagramBotMessage.objects.filter(
        client_id=execution.client_id,
        role=InstagramBotMessage.Role.USER,
        pk__gt=execution.source_message_id,
    ).exists():
        return False
    from management.services.ig_turn_lineage import resolve_logical_turn_key

    if resolve_logical_turn_key(source) != execution.logical_turn_id:
        return False
    return GeminiRequest.objects.filter(
        pk=execution.root_graph_id,
        client_id=execution.client_id,
        source_message_id=execution.source_message_id,
        logical_turn_id=execution.logical_turn_id,
        lane="live",
        source_execution_key="",
        parent_request__isnull=True,
    ).exists()


def resolve_legacy_execution(
    *, job_id, recovery_token, automation_token, client_id,
    source_message_id, logical_turn_id, now=None,
):
    """Bind one claimed recovery attempt to exactly one prior live graph."""
    now = now or timezone.now()
    job = IgAiReplyRecoveryJob.objects.select_related("holding_message").filter(
        pk=job_id, client_id=client_id,
        status=IgAiReplyRecoveryJob.Status.PROCESSING,
        lease_token=recovery_token, lease_until__gt=now,
        response_window_deadline__gt=now,
    ).first()
    if job is None:
        return None, "legacy_recovery_claim_changed"
    client = IgClient.objects.filter(
        pk=client_id,
        automation_lease_token=automation_token,
        automation_lease_until__gt=now,
    ).first()
    if client is None:
        return None, "legacy_automation_claim_changed"
    source = InstagramBotMessage.objects.filter(
        pk=source_message_id, client_id=client_id, role="user",
    ).first()
    if source is None:
        return None, "legacy_source_missing"
    if not _target_is_current(job, source_message_id):
        return None, "legacy_source_changed"
    from management.services.ig_turn_lineage import resolve_logical_turn_key

    if resolve_logical_turn_key(source) != logical_turn_id:
        return None, "legacy_turn_changed"
    roots = list(_root_graphs(
        client_id=client_id, source_message_id=source_message_id,
        logical_turn_id=logical_turn_id,
    )[:2])
    if not roots:
        return None, "legacy_root_missing"
    if len(roots) != 1:
        return None, "legacy_root_ambiguous"
    root = roots[0]
    holding_request_id = str(
        getattr(job.holding_message, "gemini_request_id", "") or ""
    )
    if holding_request_id and holding_request_id != root.request_id:
        return None, "legacy_holding_root_mismatch"
    return LegacyProviderExecution(
        job_id=int(job.pk), recovery_token=str(recovery_token),
        automation_token=str(automation_token), client_id=int(client_id),
        source_message_id=int(source_message_id),
        logical_turn_id=str(logical_turn_id),
        source_execution_key=_execution_key(job, recovery_token), root_graph_id=root.pk,
    ), ""


def validate_legacy_execution(execution, *, source_message_id, client_id, lane,
                              logical_turn_id, source_execution_key) -> bool:
    if not isinstance(execution, LegacyProviderExecution):
        return False
    if (
        lane != "recovery"
        or int(source_message_id or 0) != execution.source_message_id
        or int(client_id or 0) != execution.client_id
        or str(logical_turn_id or "") != execution.logical_turn_id
        or str(source_execution_key or "") != execution.source_execution_key
    ):
        return False
    resolved, _reason = resolve_legacy_execution(
        job_id=execution.job_id,
        recovery_token=execution.recovery_token,
        automation_token=execution.automation_token,
        client_id=execution.client_id,
        source_message_id=execution.source_message_id,
        logical_turn_id=execution.logical_turn_id,
    )
    return bool(resolved and resolved.root_graph_id == execution.root_graph_id)


def _freeze_candidates(raw_plan, root):
    persisted = {
        int(row.get("candidate_index") or 0): row
        for row in root.candidate_plan or () if isinstance(row, dict)
    }
    result, seen = [], set()
    from management.services.ig_provider_dispatch_budget import ProviderDispatchBudget

    for position, raw in enumerate(raw_plan or (), 1):
        if not isinstance(raw, dict):
            return ()
        alias = str(raw.get("key_name") or "")
        model = str(raw.get("model") or "")
        identity = str(raw.get("project_identity") or "")
        if not _TOKEN.fullmatch(alias) or not _TOKEN.fullmatch(model) or (
            identity and not _TOKEN.fullmatch(identity)
        ):
            return ()
        index = raw.get("candidate_index") or position
        if (
            isinstance(index, bool) or not isinstance(index, int)
            or not 1 <= index <= 1000 or index in seen
        ):
            return ()
        stored = persisted.get(index)
        if (
            not stored
            or stored.get("model") != model
            or str(stored.get("project_identity") or "") != identity
        ):
            return ()
        seen.add(index)
        skip = str(raw.get("skip_reason") or "")
        if skip and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", skip):
            return ()
        identity_status = str(raw.get("identity_status") or "unknown").casefold()
        if identity_status == "known" and not identity:
            identity_status = "unknown"
        if not identity and not skip:
            skip = "provider_identity_unknown"
        result.append({
            "candidate_index": index, "key_name": alias, "model": model,
            "project_identity": identity,
            "identity_status": identity_status,
            "skip_reason": skip,
            "scarce": bool(ProviderDispatchBudget._is_scarce_model(model)),
        })
    return tuple(result) if result and len(result) <= 128 else ()


def _manifest(root, *, client_id, source_message_id, logical_turn_id):
    value = (root.candidate_outcomes or {}).get(MANIFEST_KEY) or {}
    if not value:
        return {}
    unsigned = {key: item for key, item in value.items() if key != "digest"}
    if value.get("digest") != _digest(unsigned):
        return {}
    if (
        value.get("root_graph_id") != root.pk
        or value.get("root_request_id") != root.request_id
        or value.get("client_id") != client_id
        or value.get("source_message_id") != source_message_id
        or value.get("logical_turn_id") != logical_turn_id
        or value.get("max_http") != MAX_HTTP
        or value.get("max_scarce_http") != MAX_SCARCE_HTTP
        or value.get("max_repairs") != 1
    ):
        return {}
    return value


def initialize_legacy_provider_root(
    *, graph_id, automation_token, candidate_plan, now=None,
):
    """Freeze an eligible legacy live route before its first provider call."""
    now = now or timezone.now()
    with transaction.atomic():
        identity = GeminiRequest.objects.filter(pk=graph_id).values(
            "source_message_id",
        ).first()
        if identity is None:
            return None, ProviderContinuation(reason="legacy_root_missing")
        locked_source = InstagramBotMessage.objects.select_for_update().filter(
            pk=identity["source_message_id"],
        ).first()
        graph = GeminiRequest.objects.select_for_update().get(pk=graph_id)
        execution = LegacyRootExecution(
            automation_token=str(automation_token or ""),
            client_id=int(graph.client_id or 0),
            source_message_id=int(graph.source_message_id or 0),
            logical_turn_id=str(graph.logical_turn_id or ""),
            root_graph_id=graph.pk,
        )
        if not validate_legacy_root_execution(execution, now=now):
            return None, ProviderContinuation(reason="legacy_live_claim_invalid")
        if graph.terminal_resolution or graph.winner_attempt_id:
            return None, ProviderContinuation(reason="legacy_root_resolved")
        existing = _manifest(
            graph, client_id=execution.client_id,
            source_message_id=execution.source_message_id,
            logical_turn_id=execution.logical_turn_id,
        )
        if existing:
            return execution, _root_continuation(execution, now=now)
        if (graph.candidate_outcomes or {}).get(MANIFEST_KEY):
            return None, ProviderContinuation(reason="legacy_manifest_invalid")
        if GeminiRequestAttempt.objects.filter(
            request_graph=graph, provider_started_at__isnull=False,
        ).exists():
            return None, ProviderContinuation(reason="legacy_manifest_missing")
        frozen = _freeze_candidates(candidate_plan, graph)
        if not frozen or not any(not row["skip_reason"] for row in frozen):
            return None, ProviderContinuation(reason="legacy_route_invalid")
        from management.services.ig_ai_reply_recovery import _window_deadline

        client = IgClient.objects.filter(pk=execution.client_id).first()
        channel_deadline = (
            _window_deadline(locked_source, client)
            if locked_source is not None and client is not None else None
        )
        horizon = min(now + HORIZON, channel_deadline or now)
        if horizon <= now:
            return None, ProviderContinuation(
                reason="provider_horizon_exhausted",
                root_revision_id=graph.pk,
            )
        manifest = {
            "version": 1,
            "origin": "live_pre_dispatch",
            "root_graph_id": graph.pk,
            "root_request_id": graph.request_id,
            "client_id": execution.client_id,
            "source_message_id": execution.source_message_id,
            "logical_turn_id": execution.logical_turn_id,
            "started_at": now.isoformat(),
            "horizon_at": horizon.isoformat(),
            "max_http": MAX_HTTP,
            "max_scarce_http": MAX_SCARCE_HTTP,
            "max_repairs": 1,
            "candidates": list(frozen),
        }
        manifest["digest"] = _digest(manifest)
        outcomes = {**(graph.candidate_outcomes or {}), MANIFEST_KEY: manifest}
        GeminiRequest.objects.filter(pk=graph.pk).update(
            candidate_outcomes=outcomes, updated_at=now,
        )
        graph.candidate_outcomes = outcomes
        continuation = _continuation(
            graph, client_id=execution.client_id,
            source_message_id=execution.source_message_id,
            logical_turn_id=execution.logical_turn_id,
            now=now,
        )
        return execution, replace(continuation, root_revision_id=graph.pk)


def _family_graphs(
    root, *, client_id, source_message_id, logical_turn_id, lock_ledger=False,
):
    query = GeminiRequest.objects.filter(
        Q(pk=root.pk) | Q(parent_request_id=root.pk),
        client_id=client_id,
        source_message_id=source_message_id,
        logical_turn_id=logical_turn_id,
    ).order_by("pk")
    rows = list(
        (query.select_for_update() if lock_ledger else query)[:MAX_CHILD_GRAPHS + 2]
    )
    if len(rows) > MAX_CHILD_GRAPHS + 1:
        return []
    if any(
        row.pk != root.pk and (
            row.parent_request_id != root.pk
            or row.lane != "recovery"
            or not str(row.source_execution_key or "").startswith("ig-recovery:")
        )
        for row in rows
    ):
        return []
    return rows


def _candidate_key(row):
    return row["key_name"], row["model"], row["project_identity"]


def _disposition(candidate, attempts):
    due = None
    for attempt in attempts:
        same_model = attempt.model == candidate["model"]
        same_identity = attempt.project_identity == candidate["project_identity"]
        same_alias = attempt.key_name == candidate["key_name"]
        if attempt.failure_kind == "invalid_response" and same_model:
            return "provider_model_schema_rejected", None
        if attempt.http_code == 401 and same_alias:
            return "provider_credential_rejected", None
        if attempt.http_code in {400, 403, 404} and same_model and same_identity:
            return "provider_candidate_rejected", None
        if same_model and same_identity and attempt.provider_started_at and attempt.fsm_state in {
            "provider_started", "reserved",
        }:
            return "provider_generation_unresolved", attempt.permit_expires_at
        if same_model and same_identity and attempt.failure_kind in {
            "quota_429", "quota", "http_429", "read_timeout", "transport",
            "http_5xx", "timeout", "empty", "unavailable",
        }:
            candidate_due = (attempt.finished_at or attempt.provider_started_at) + timedelta(seconds=45)
            due = max(due, candidate_due) if due else candidate_due
    return "", due


def _continuation(
    root, *, client_id, source_message_id, logical_turn_id,
    candidate_plan=None, now=None, lock_ledger=False,
):
    now = now or timezone.now()
    manifest = _manifest(
        root, client_id=client_id, source_message_id=source_message_id,
        logical_turn_id=logical_turn_id,
    )
    if not manifest:
        reason = (
            "legacy_manifest_invalid"
            if (root.candidate_outcomes or {}).get(MANIFEST_KEY)
            else "legacy_manifest_missing"
        )
        return ProviderContinuation(reason=reason, root_revision_id=root.pk)
    if manifest.get("origin") != "live_pre_dispatch":
        return ProviderContinuation(
            reason="legacy_manifest_invalid", root_revision_id=root.pk,
        )
    try:
        horizon = timezone.datetime.fromisoformat(manifest["horizon_at"])
    except (KeyError, TypeError, ValueError):
        return ProviderContinuation(
            reason="legacy_manifest_invalid", root_revision_id=root.pk,
        )
    if timezone.is_naive(horizon) or now >= horizon:
        return ProviderContinuation(
            reason="provider_horizon_exhausted", root_revision_id=root.pk,
            manifest=manifest,
        )
    graphs = _family_graphs(
        root, client_id=client_id, source_message_id=source_message_id,
        logical_turn_id=logical_turn_id, lock_ledger=lock_ledger,
    )
    if not graphs:
        return ProviderContinuation(
            reason="legacy_graph_invalid", root_revision_id=root.pk,
            manifest=manifest,
        )
    attempt_query = GeminiRequestAttempt.objects.filter(
        request_graph_id__in=[row.pk for row in graphs],
        provider_started_at__isnull=False,
    ).order_by("pk")
    attempts = list(
        attempt_query.select_for_update() if lock_ledger else attempt_query
    )
    by_key = {_candidate_key(row): row for row in manifest["candidates"]}
    scarce_used = sum(bool(by_key.get(
        (row.key_name, row.model, row.project_identity), {"scarce": True},
    )["scarce"]) for row in attempts)
    counts = {}
    for attempt in attempts:
        key = (attempt.key_name, attempt.model, attempt.candidate_index)
        counts[key] = counts.get(key, 0) + 1
    repair = (root.candidate_outcomes or {}).get(REPAIR_KEY) or any(
        count > 1 for count in counts.values()
    )
    http_remaining = max(0, MAX_HTTP - len(attempts))
    scarce_remaining = max(0, MAX_SCARCE_HTTP - scarce_used)
    base = {
        "root_revision_id": root.pk, "manifest": manifest,
        "http_remaining": http_remaining,
        "scarce_remaining": scarce_remaining,
        "repair_remaining": not bool(repair),
    }
    if not http_remaining:
        return ProviderContinuation(reason="provider_dispatch_budget", **base)
    current = {
        _candidate_key(row): row for row in _freeze_candidates(candidate_plan, root)
    } if candidate_plan is not None else None
    candidates, waits = [], []
    for frozen in manifest["candidates"]:
        candidate = dict(frozen)
        code, due = _disposition(candidate, attempts)
        if current is not None:
            live = current.get(_candidate_key(candidate))
            if live is None or live["identity_status"] != candidate["identity_status"]:
                code = "provider_candidate_identity_changed"
            elif live["skip_reason"]:
                code = live["skip_reason"]
        candidate["skip_reason"] = candidate["skip_reason"] or code
        if candidate["scarce"] and not scarce_remaining and not candidate["skip_reason"]:
            candidate["skip_reason"] = "scarce_model_budget"
        if due and due > now and not candidate["skip_reason"]:
            candidate["skip_reason"] = "provider_candidate_wait"
            waits.append(due)
        candidates.append(candidate)
    ready = any(not row["skip_reason"] for row in candidates)
    next_due = min(waits) if waits else None
    reason = (
        "" if ready else "provider_wait"
        if next_due and next_due < horizon else "provider_candidates_exhausted"
    )
    return ProviderContinuation(
        ready=ready, reason=reason, candidate_plan=tuple(candidates),
        next_due_at=next_due, **base,
    )


def _root_continuation(execution, *, now=None, lock_ledger=False):
    now = now or timezone.now()
    if not validate_legacy_root_execution(execution, now=now):
        return ProviderContinuation(reason="legacy_live_claim_invalid")
    with transaction.atomic():
        InstagramBotMessage.objects.select_for_update().filter(
            pk=execution.source_message_id,
        ).first()
        root = GeminiRequest.objects.select_for_update().filter(
            pk=execution.root_graph_id,
        ).first()
        if root is None:
            return ProviderContinuation(reason="legacy_root_missing")
        return _continuation(
            root, client_id=execution.client_id,
            source_message_id=execution.source_message_id,
            logical_turn_id=execution.logical_turn_id, now=now,
            lock_ledger=lock_ledger,
        )


def legacy_provider_continuation(
    execution, *, candidate_plan=None, now=None, lock_ledger=False,
):
    """Freeze or inspect the common live+recovery provider graph."""
    now = now or timezone.now()
    if not validate_legacy_execution(
        execution, source_message_id=execution.source_message_id,
        client_id=execution.client_id, lane="recovery",
        logical_turn_id=execution.logical_turn_id,
        source_execution_key=execution.source_execution_key,
    ):
        return ProviderContinuation(reason="legacy_execution_invalid")
    with transaction.atomic():
        source = InstagramBotMessage.objects.select_for_update().filter(
            pk=execution.source_message_id, client_id=execution.client_id,
        ).first()
        root = GeminiRequest.objects.select_for_update().filter(
            pk=execution.root_graph_id,
        ).first()
        job = IgAiReplyRecoveryJob.objects.filter(pk=execution.job_id).first()
        if source is None or root is None or job is None:
            return ProviderContinuation(reason="legacy_root_missing")
        if not job.response_window_deadline or job.response_window_deadline <= now:
            return ProviderContinuation(reason="provider_horizon_exhausted")
        return _continuation(
            root, client_id=execution.client_id,
            source_message_id=execution.source_message_id,
            logical_turn_id=execution.logical_turn_id,
            candidate_plan=candidate_plan, now=now,
            lock_ledger=lock_ledger,
        )


def inspect_legacy_job_provider_state(job, *, now=None):
    """Inspect the frozen root before a recovery worker waits on an incident."""
    now = now or timezone.now()
    if not job or not job.client_id:
        return ProviderContinuation(reason="legacy_root_missing")
    from management.services.ig_ai_reply_recovery import recovery_target_message
    from management.services.ig_turn_lineage import resolve_logical_turn_key

    try:
        source = recovery_target_message(job)
    except Exception:
        return ProviderContinuation(reason="legacy_source_missing")
    logical_turn_id = resolve_logical_turn_key(source)
    roots = list(_root_graphs(
        client_id=job.client_id,
        source_message_id=source.pk,
        logical_turn_id=logical_turn_id,
    )[:2])
    if not roots:
        return ProviderContinuation(reason="legacy_root_missing")
    if len(roots) != 1:
        return ProviderContinuation(reason="legacy_root_ambiguous")
    root = roots[0]
    holding_request_id = str(
        getattr(job.holding_message, "gemini_request_id", "") or ""
    )
    if holding_request_id and holding_request_id != root.request_id:
        return ProviderContinuation(reason="legacy_holding_root_mismatch")
    if not job.response_window_deadline or job.response_window_deadline <= now:
        return ProviderContinuation(
            reason="provider_horizon_exhausted", root_revision_id=root.pk,
        )
    return _continuation(
        root, client_id=job.client_id, source_message_id=source.pk,
        logical_turn_id=logical_turn_id, now=now,
    )


def admit_legacy_provider_dispatch_locked(graph, boundary, *, now):
    execution = getattr(boundary.observer, "_legacy_execution", None)
    root_execution = getattr(boundary.observer, "_legacy_root_execution", None)
    if root_execution is not None:
        if graph.pk != root_execution.root_graph_id or not validate_legacy_root_execution(
            root_execution, now=now,
        ):
            return "legacy_live_claim_invalid"
        continuation = _root_continuation(
            root_execution, now=now, lock_ledger=True,
        )
        root_graph_id = root_execution.root_graph_id
    else:
        if not validate_legacy_execution(
            execution, source_message_id=graph.source_message_id,
            client_id=graph.client_id, lane=graph.lane,
            logical_turn_id=graph.logical_turn_id,
            source_execution_key=graph.source_execution_key,
        ):
            return "legacy_execution_invalid"
        continuation = legacy_provider_continuation(
            execution, now=now, lock_ledger=True,
        )
        root_graph_id = execution.root_graph_id
    identity = boundary.observer._identity_for(boundary)
    candidate = next((row for row in continuation.candidate_plan if (
        row["candidate_index"] == boundary.candidate_index
        and row["key_name"] == boundary.key_name
        and row["model"] == boundary.model
        and row["project_identity"] == identity
    )), None)
    if candidate is None:
        return continuation.reason or "provider_candidate_identity_changed"
    if not continuation.http_remaining:
        return "provider_dispatch_budget"
    if candidate["scarce"] and not continuation.scarce_remaining:
        return "scarce_model_budget"
    repair_token = getattr(boundary, "provider_repair_token", "")
    if repair_token:
        root = GeminiRequest.objects.filter(pk=root_graph_id).first()
        marker = (root.candidate_outcomes or {}).get(REPAIR_KEY) if root else None
        expected = {
            "token": repair_token, "request_id": graph.request_id,
            "candidate_index": boundary.candidate_index,
            "key_name": boundary.key_name, "model": boundary.model,
        }
        if marker != expected:
            return "provider_repair_identity_invalid"
        return "" if candidate["skip_reason"] in {"", "provider_model_schema_rejected"} else candidate["skip_reason"]
    return candidate["skip_reason"] or ("" if continuation.ready else continuation.reason)


def reserve_legacy_provider_repair(observer, *, key_name, model, candidate_index=0):
    execution = observer._legacy_execution
    root_execution = getattr(observer, "_legacy_root_execution", None)
    with transaction.atomic():
        graph, _message = observer._lock_canonical_graph()
        if graph is None or graph.terminal_resolution or graph.winner_attempt_id:
            return False
        continuation = (
            _root_continuation(root_execution, lock_ledger=True)
            if root_execution is not None
            else legacy_provider_continuation(execution, lock_ledger=True)
        )
        if not continuation.http_remaining or not continuation.repair_remaining:
            return False
        candidate_index = candidate_index or observer.candidate_index(key_name, model)
        candidate = next((row for row in continuation.candidate_plan if (
            row["candidate_index"] == candidate_index
            and row["key_name"] == key_name and row["model"] == model
        )), None)
        if not candidate or candidate["skip_reason"] != "provider_model_schema_rejected" or (
            candidate["scarce"] and not continuation.scarce_remaining
        ):
            return False
        root_graph_id = (
            root_execution.root_graph_id
            if root_execution is not None else execution.root_graph_id
        )
        root = GeminiRequest.objects.filter(pk=root_graph_id).first()
        if root is None:
            return False
        marker = {
            "token": secrets.token_hex(16), "request_id": graph.request_id,
            "candidate_index": candidate_index, "key_name": key_name,
            "model": model,
        }
        GeminiRequest.objects.filter(pk=root.pk).update(
            candidate_outcomes={**(root.candidate_outcomes or {}), REPAIR_KEY: marker},
            updated_at=timezone.now(),
        )
        observer._pending_provider_repair = marker
        return True
