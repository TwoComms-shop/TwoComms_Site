"""Provider-free, strictly redacted read models for Gemini V2 admin APIs.

The module deliberately has no provider or probe dependency.  It projects
existing immutable request/attempt evidence and local quota state into the known display
models by six opaque slots.  Missing or dormant accounting is reported as
unknown rather than being converted into reassuring zero usage.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import OuterRef, Prefetch, Q, Subquery
from django.utils import timezone
from django.utils.crypto import salted_hmac

from management.models import (
    GeminiQuotaProfile,
    GeminiQuotaState,
    GeminiRequest,
    GeminiRequestAttempt,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import (
    gemini_accounting_runtime,
    gemini_health,
    gemini_keys,
    gemini_routing,
)


SCHEMA_VERSION = 1
MODELS = tuple(gemini_health.DISPLAY_MODELS)
SLOT_IDS = tuple(gemini_health.SLOT_IDS)
SLOT_BY_ALIAS = dict(gemini_health.SLOT_BY_ALIAS)
CURSOR_TTL_SECONDS = 30 * 24 * 3600
PUBLIC_HISTORY_CAP = 100
ATTEMPT_PAGE_DEFAULT = 25
ATTEMPT_PAGE_MAX = 50
ATTEMPTS_PER_REQUEST_CAP = 64
QUOTA_TRAFFIC_WINDOW = dt.timedelta(hours=24)
QUOTA_ATTEMPT_CAP = 5000
RECENT_SUCCESS_SECONDS = gemini_health.FRESH_EVIDENCE_SECONDS
UNRESOLVED_FAILURE_SECONDS = int(QUOTA_TRAFFIC_WINDOW.total_seconds())
PT = ZoneInfo("America/Los_Angeles")

_CURSOR_KEY_DOMAIN = b"twocomms/gemini-v2/attempt-cursor/v1\0"
_SAFE_QUOTA_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_PUBLIC_LANES = frozenset({
    "analysis", "call", "checker", "diagnostic", "followup", "holding",
    "live", "metadata_probe", "recovery", "unknown",
})
_PUBLIC_TASK_CLASSES = frozenset({
    item.value for item in gemini_routing.TaskClass
}) | {"diagnostic", "unknown"}
_PUBLIC_FAILURE_KINDS = frozenset({
    "blocked", "empty", "forbidden", "http_408", "http_5xx",
    "invalid_key", "invalid_payload", "invalid_response", "lease_busy",
    "malformed_response", "model_not_found", "model_overload",
    "model_unavailable", "overload", "permission_denied",
    "provider_error", "provider_overload", "quarantined", "quota_429",
    "read_timeout", "request_error", "stale_provider_boundary", "transport",
})
_PUBLIC_NOT_ATTEMPTED = frozenset({
    "circuit_open", "deadline", "duplicate_credential", "duplicate_project",
    "fatal_payload", "lease_busy", "model_overload", "model_terminal",
    "model_unavailable", "not_available_plan", "policy_stop", "quarantine",
    "quota_cooldown", "quota_exhausted", "sla_model_budget", "unconfigured",
    "winner_found",
})
_PUBLIC_ADMISSION_REASONS = frozenset({
    "unknown_project", "missing_profile", "estimator_uncalibrated", "provider_block",
    "rpd_exhausted", "rpm_exhausted", "permit_exhausted", "tpm_exhausted", "quota_profile_conflict",
    "provider_deadline_expired", "policy_manifest_dispatch_missing", "source_admission_denied",
    "source_admission_unavailable", "claim_replaced", "claim_deadline_expired", "capture_digest_invalid",
    "scope_changed", "source_watermark_advanced", "head_changed", "source_changed", "privacy_erasure",
    "owner_changed", "owner_unavailable", "generation_disabled", "admission_not_enforced",
    "maintenance_active", "db_circuit_open", "database_circuit_open", "client_erasing", "customer_reply_priority", "analysis_priority",
    "lane_owner_changed", "lane_owner_unavailable", "not_dispatched", "deadline",
})
_PUBLIC_FAILURE_KINDS |= {"provider_admission_denied", "provider_admission_unknown", "provider_deadline_expired",
    "source_admission_denied", "source_admission_unavailable", "local_semantic_rejection"}
_PUBLIC_NOT_ATTEMPTED |= _PUBLIC_ADMISSION_REASONS
_PUBLIC_MANIFEST_CODES = frozenset({
    "budget_exceeded", "budget_exhausted", "ready", "budget", "scope_mismatch", "scope_unknown", "source_unknown", "source_changed",
    "source_invalid", "privacy_erasure", "unavailable", "not_applicable", "missing_source", "missing_selector",
    "legacy_context_uncaptured", "captured_artifact_unavailable", "readiness_unknown", "selection_unknown",
    "ambiguous_product", "availability_unknown", "payment_unknown", "consent_unknown", "media_unavailable",
    "history_budget", "optional_budget", "component_scope_mismatch", "component_unavailable",
})


_PUBLIC_FSM = frozenset(value for value, _label in GeminiRequestAttempt.FsmState.choices)
_PUBLIC_RESOLUTIONS = frozenset({"failed", "succeeded"})
_PUBLIC_TERMINAL_REASONS = frozenset({
    "deadline", "exhausted", "fatal_payload", "model_terminal",
    "model_unavailable", "no_candidates", "no_model", "provider_success",
    "quota_cooldown", "quota_exhausted", "sla_model_budget", "winner_found",
})
_PUBLIC_SEND_STATES = frozenset({
    "", "cancelled", "duplicate", "failed", "sending", "sent", "unknown",
})
_PUBLIC_MESSAGE_STATUSES = frozenset({"done", "failed", "pending", "processing"})
_PUBLIC_QUOTA_METRICS = frozenset({"rpm", "tpm", "rpd", "unknown"})
_PUBLIC_QUOTA_DIMENSIONS = frozenset({"location", "model", "region", "tier"})


class InvalidCursor(ValueError):
    pass


class PublicProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class _SlotIdentity:
    alias: str
    slot_id: str
    configured: bool
    identity: str
    mapping_state: str


def _as_utc(value: dt.datetime) -> dt.datetime:
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value else None


def _aware_now(now=None) -> dt.datetime:
    return _as_utc(now or timezone.now())


def _public_code(value: Any, allowed: frozenset[str], *, default="other") -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in allowed else default


def _bounded_int(value: Any, *, maximum: int = 10**12) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(0, parsed), maximum)


def _opaque_reference(domain: str, value: Any, prefix: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    digest = salted_hmac(
        f"management.gemini-v2.{domain}.v1",
        normalized,
    ).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _public_model(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in MODELS else "unknown"


def _public_version(value: Any, *, limit: int = 40) -> str:
    normalized = str(value or "").strip()[:limit]
    return normalized if _SAFE_VERSION.fullmatch(normalized) else ""


def _slot_identities() -> list[_SlotIdentity]:
    explicit = gemini_keys.explicit_project_groups()
    identity_counts = Counter(explicit.values())
    fingerprints: dict[str, bytes] = {}
    fingerprint_counts: Counter[bytes] = Counter()
    configured: dict[str, bool] = {}
    for alias in gemini_keys.ALL_KEYS:
        value = str(gemini_keys._key_value(alias) or "").strip()
        configured[alias] = bool(value)
        fingerprint = gemini_keys.credential_fingerprint(value)
        if fingerprint:
            fingerprints[alias] = fingerprint
            fingerprint_counts[fingerprint] += 1

    rows: list[_SlotIdentity] = []
    for alias in gemini_keys.ALL_KEYS:
        identity = str(explicit.get(alias) or "")
        duplicate = bool(
            identity and identity_counts[identity] > 1
        ) or bool(
            fingerprints.get(alias)
            and fingerprint_counts[fingerprints[alias]] > 1
        )
        mapping_state = (
            "duplicate"
            if duplicate
            else "explicit"
            if identity
            else "missing"
        )
        rows.append(_SlotIdentity(
            alias=alias,
            slot_id=SLOT_BY_ALIAS[alias],
            configured=configured[alias],
            identity=identity if not duplicate else "",
            mapping_state=mapping_state,
        ))
    return rows


def _identity_to_slot(slots: list[_SlotIdentity]) -> dict[str, str]:
    return {
        row.identity: row.slot_id
        for row in slots
        if row.mapping_state == "explicit" and row.identity
    }


def _active_profiles(now: dt.datetime) -> dict[str, GeminiQuotaProfile]:
    effective = GeminiQuotaProfile.objects.filter(model__in=MODELS, effective_from__lte=now).filter(
        Q(effective_until__isnull=True) | Q(effective_until__gt=now))
    latest = effective.filter(model=OuterRef("model")).order_by("-effective_from", "-id").values("pk")[:1]
    # Bound materialization, not just the returned dictionary: one actual
    # latest effective row per display model, using the runtime tie-break.
    rows = effective.filter(pk=Subquery(latest)).order_by("model")
    return {row.model: row for row in rows}


def _pacific_window(now: dt.datetime) -> tuple[dt.date, dt.datetime, dt.datetime]:
    local = now.astimezone(PT)
    start = dt.datetime.combine(local.date(), dt.time.min, tzinfo=PT)
    reset = dt.datetime.combine(
        local.date() + dt.timedelta(days=1),
        dt.time.min,
        tzinfo=PT,
    )
    return local.date(), start.astimezone(dt.timezone.utc), reset.astimezone(dt.timezone.utc)


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(max(0, int(value)) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _safe_counter(rows, key: str, allowed: frozenset[str]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = str(row.get(key) or "unknown").strip().casefold()
        counts[value if value in allowed else "unknown"] += 1
    return dict(sorted(counts.items()))


def _parse_block_until(value: Any) -> dt.datetime | None:
    return gemini_accounting_runtime.parse_effective_from(value)


def _public_quota_id(value: Any) -> str:
    normalized = str(value or "").strip()[:120]
    return normalized if _SAFE_QUOTA_TOKEN.fullmatch(normalized) else ""


def _public_quota_dimensions(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_value in list(value.items())[:8]:
        key = str(raw_key or "").strip().casefold()
        item = str(raw_value or "").strip()[:80]
        if (
            key in _PUBLIC_QUOTA_DIMENSIONS
            and _SAFE_QUOTA_TOKEN.fullmatch(item)
        ):
            result[key] = item
    return dict(sorted(result.items()))


def _public_blocks(blocks: Any, now: dt.datetime) -> list[dict[str, Any]]:
    if not isinstance(blocks, dict):
        return []
    result: list[dict[str, Any]] = []
    for raw_metric, value in list(blocks.items())[:8]:
        if not isinstance(value, dict):
            continue
        metric = _public_code(raw_metric, _PUBLIC_QUOTA_METRICS, default="unknown")
        until = _parse_block_until(value.get("until"))
        result.append({
            "metric": metric,
            "quota_id": _public_quota_id(value.get("quota_id")),
            "dimensions": _public_quota_dimensions(value.get("dimensions")),
            "active": bool(until and until > now),
            "until": _iso(until),
            "retry_after_seconds": _bounded_int(
                value.get("retry_after_seconds"), maximum=7 * 24 * 3600
            ),
        })
    result.sort(key=lambda item: (not item["active"], item["metric"]))
    return result[:4]


def _pair_status(
    *,
    accounting_active: bool,
    slot: _SlotIdentity,
    profile: GeminiQuotaProfile | None,
    state: GeminiQuotaState | None,
    blocks: list[dict[str, Any]],
    now: dt.datetime,
    traffic_rows: list[dict[str, Any]] | None = None,
) -> str:
    if not slot.configured:
        return "not_configured"
    if not accounting_active or slot.mapping_state != "explicit" or profile is None:
        return "accounting_unknown"
    block_metrics = {item["metric"] for item in blocks}
    active_metrics = {item["metric"] for item in blocks if item["active"]}
    # A provider-confirmed block without a recognized quota dimension is
    # stronger evidence than local remaining counters.  Never turn it into an
    # optimistic availability claim merely because the 429 omitted details or
    # its best-effort cooldown timestamp elapsed.
    if "unknown" in block_metrics:
        return "accounting_unknown"
    if "rpd" in active_metrics:
        return "rpd_exhausted_until_reset"
    if "rpm" in active_metrics:
        return "rpm_limited"
    if "tpm" in active_metrics:
        return "tpm_limited"
    if state is None:
        return "available_assumed"
    if state.in_flight_count:
        return "in_flight"
    failure = str(state.last_failure_kind or "").casefold()
    if failure == "invalid_key" or state.last_http_code == 401:
        return "auth_failed"
    if failure in {"model_not_found", "permission_denied", "model_unavailable"}:
        return "model_unavailable_for_project"
    # A classified RPM/TPM/RPD cooldown is time-bounded provider truth.  Once
    # every such block is expired, the UI may return to the deliberately weak
    # ``available_assumed`` state.  Missing/unparseable/unknown blocks above,
    # and any non-quota failure below, remain fail-closed.
    if state.accounting_status == GeminiQuotaState.AccountingStatus.BLOCKED:
        expired_classified_only = bool(block_metrics) and block_metrics.issubset(
            {"rpm", "tpm", "rpd"}
        ) and not active_metrics and all(
            item.get("until") is not None for item in blocks
        )
        if not expired_classified_only or failure not in {"", "quota_429"}:
            return "provider_degraded"
    success_at = _as_utc(state.last_success_at) if state.last_success_at else None
    failure_at = _as_utc(state.last_failure_at) if state.last_failure_at else None
    degraded = state.accounting_status == GeminiQuotaState.AccountingStatus.DEGRADED
    failure_current = False
    if traffic_rows is not None:
        # Pair state mixes all admitted roles for economic coordination.
        # Explicit diagnostics still consume quota, but their success cannot
        # recover a failed customer/useful-work route or create fresh traffic.
        evidence_rows = [row for row in traffic_rows if gemini_health.is_generation_traffic_evidence(row)]
        successes = [row for row in evidence_rows if row.get("fsm_state") in {"succeeded", "succeeded_late"}]
        failures = [row for row in evidence_rows if row.get("fsm_state") in {"failed", "timeout_ambiguous"}]
        latest_success = max(successes, key=lambda row: (_as_utc(row["provider_started_at"]), int(row.get("id") or 0)), default=None)
        latest_failure = max(failures, key=lambda row: (_as_utc(row["provider_started_at"]), int(row.get("id") or 0)), default=None)
        state_failure_at = failure_at
        state_failure_kind = failure
        state_degraded = degraded
        success_at = _as_utc(latest_success["provider_started_at"]) if latest_success else None
        failure_at = _as_utc(latest_failure["provider_started_at"]) if latest_failure else None
        failure = str(latest_failure.get("failure_kind") or "").casefold() if latest_failure else ""
        failure_current = bool(latest_failure and (latest_success is None or
            (_as_utc(latest_failure["provider_started_at"]), int(latest_failure.get("id") or 0)) >
            (_as_utc(latest_success["provider_started_at"]), int(latest_success.get("id") or 0))))
        degraded = failure_current
        if failure_current and (failure == "invalid_key" or latest_failure.get("http_code") == 401):
            return "auth_failed"
        if failure_current and failure in {"model_not_found", "permission_denied", "model_unavailable"}:
            return "model_unavailable_for_project"
        # Negative provider evidence is still an incident even if the purpose
        # ledger is missing/truncated or the failure was a diagnostic. Only a
        # useful-work success can recover it. Classified expired quota blocks
        # have already been handled above and do not become transient failures.
        if state_failure_kind not in {"", "quota_429"} and (
            state_failure_at is not None and (failure_at is None or state_failure_at > failure_at)
        ):
            failure_at = state_failure_at
            failure = state_failure_kind
            failure_current = success_at is None or failure_at >= success_at
            degraded = failure_current
        elif not evidence_rows and state_degraded and state_failure_kind not in {"", "quota_429"}:
            failure = state_failure_kind
            failure_at = state_failure_at
            degraded = True
    if success_at and (failure_at is None or success_at >= failure_at) and not failure_current:
        age = (now - success_at).total_seconds()
        if 0 <= age <= RECENT_SUCCESS_SECONDS:
            return "confirmed_recent_success"
        # A successful request after the last failure is still evidence that
        # the pair recovered.  It may be too old for the stronger "recent"
        # badge, but historical 503/timeout state must not keep the pair red
        # forever when no quota block is active.
        if age > RECENT_SUCCESS_SECONDS:
            return "available_assumed"
    if failure == "local_semantic_rejection" and state.accounting_status != GeminiQuotaState.AccountingStatus.BLOCKED:
        # A rejected draft after HTTP success is local response validation,
        # not evidence that the provider/model is unavailable. Real blocks and
        # later successful recovery above retain their existing precedence.
        if failure_at is None or (now - failure_at).total_seconds() <= UNRESOLVED_FAILURE_SECONDS:
            return "local_validation_failed"
        return "available_assumed"
    if degraded:
        # DEGRADED is an observation state written for every non-success,
        # including transient 503s and read timeouts.  It is not a durable
        # quarantine.  Once the unresolved failure is outside the traffic
        # window, report weak availability until fresh evidence arrives; keep
        # recent failures visible so an active incident remains actionable.
        if failure_at is None:
            return "provider_degraded"
        failure_age = (now - failure_at).total_seconds()
        if failure_age < 0 or failure_age <= UNRESOLVED_FAILURE_SECONDS:
            return "provider_degraded"
        return "available_assumed"
    return "available_assumed"


def _metric(*, used, limit, reserved=0, uncertain=0, complete: bool) -> dict[str, Any]:
    if not complete:
        return {
            "used": None,
            "limit": _bounded_int(limit) if limit is not None else None,
            "remaining": None,
            "reserved": None,
            "uncertain": None,
            "complete": False,
        }
    used_value = _bounded_int(used)
    reserved_value = _bounded_int(reserved)
    limit_value = _bounded_int(limit) if limit is not None else None
    return {
        "used": used_value,
        "limit": limit_value,
        "remaining": (
            max(0, limit_value - used_value - reserved_value)
            if limit_value is not None
            else None
        ),
        "reserved": reserved_value,
        "uncertain": _bounded_int(uncertain),
        "complete": True,
    }


def _last_evidence(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [row for row in rows if gemini_health.is_generation_traffic_evidence(row)]
    if not rows:
        return None
    latest = max(
        rows,
        key=lambda row: (
            _as_utc(row["provider_started_at"]),
            int(row.get("id") or 0),
        ),
    )
    fsm = _public_code(latest.get("fsm_state"), _PUBLIC_FSM, default="unknown")
    failure = _public_code(
        latest.get("failure_kind"), _PUBLIC_FAILURE_KINDS, default="other"
    ) if latest.get("failure_kind") else ""
    return {
        "request_ref": gemini_health.public_request_reference(latest.get("request_id")),
        "at": _iso(latest.get("provider_started_at")),
        "fsm_state": fsm,
        "success": fsm in {"succeeded", "succeeded_late"},
        "failure_kind": failure,
        "http_code": _bounded_int(latest.get("http_code"), maximum=599) or None,
        "latency_ms": _bounded_int(latest.get("latency_ms"), maximum=600_000),
    }


def _profile_limits(profile: GeminiQuotaProfile | None) -> dict[str, int] | None:
    if profile is None:
        return None
    return {
        "rpm": _bounded_int(profile.rpm_limit),
        "input_tpm": _bounded_int(profile.input_tpm_limit),
        "rpd": _bounded_int(profile.rpd_limit),
        "permits": _bounded_int(profile.permit_limit),
    }


def _first_plan_model(plan: Any) -> str:
    if not isinstance(plan, list):
        return ""
    ordered = sorted(
        (item for item in plan if isinstance(item, dict)),
        key=lambda item: _bounded_int(item.get("candidate_index")) or 65535,
    )
    return next(
        (str(item.get("model")) for item in ordered if item.get("model") in MODELS),
        "",
    )


def _nonlive_accounting_fields(*, accounting_active):
    mode = gemini_accounting_runtime.nonlive_admission_mode() if getattr(settings, "GEMINI_NONLIVE_ADMISSION_MODE", None) is not None else "enforce" if accounting_active else "shadow"
    return {"nonlive_admission_mode": mode if mode in {"enforce", "shadow", "invalid"} else "invalid",
        "nonlive_enforcement_active": bool(accounting_active and mode == "enforce"),
        "capacity_authority": "local_advisory_not_dispatch_permission"}


def _tpm_metric(*, used, limit, complete, calibrated, usage_source, profile_bound=True):
    result = _metric(used=used, limit=limit, complete=bool(complete and calibrated and profile_bound))
    # Real ledger usage remains observable even when estimator calibration
    # prevents computing a trustworthy remaining input-token allowance.
    result.update(used=_bounded_int(used) if complete else None,
        usage_source=usage_source if complete else "unknown", observed_usage_known=bool(complete),
        headroom_known=bool(complete and calibrated and profile_bound),
        calibration="unknown" if calibrated is None else "calibrated" if calibrated else "uncalibrated")
    return result


def build_quotas_payload(*, now=None) -> dict[str, Any]:
    """Return a bounded model-by-slot local quota projection without writes/provider I/O."""
    generated_at = _aware_now(now)
    pacific_day, pacific_start, pacific_reset = _pacific_window(generated_at)
    slots = _slot_identities()
    profiles = _active_profiles(generated_at)
    accounting_mode = gemini_accounting_runtime.configured_mode()
    accounting_active = gemini_accounting_runtime.shadow_runtime_active(now=generated_at)

    known_identities = {
        slot.identity
        for slot in slots
        if slot.configured and slot.mapping_state == "explicit" and slot.identity
    }
    states: dict[tuple[str, str], GeminiQuotaState] = {}
    traffic_rows: list[dict[str, Any]] = []
    truncated = False
    graph_plans: dict[int, list] = {}
    if accounting_active and known_identities:
        states = {
            (row.project_identity, row.model): row
            for row in GeminiQuotaState.objects.filter(
                project_identity__in=known_identities,
                model__in=MODELS,
            ).select_related("quota_profile")
        }
        raw_rows = list(
            GeminiRequestAttempt.objects.filter(
                project_identity__in=known_identities,
                model__in=MODELS,
                provider_started_at__gte=generated_at - QUOTA_TRAFFIC_WINDOW,
                provider_started_at__lte=generated_at,
            )
            .order_by("-provider_started_at", "-id")
            .values(
                "id", "request_id", "request_graph_id", "model",
                "project_identity", "role", "lane", "request_graph__task_class",
                "fsm_state", "failure_kind", "http_code", "latency_ms",
                "prompt_tokens", "reserved_prompt_tokens",
                "provider_started_at", "request_graph__winner_attempt_id",
            )[:QUOTA_ATTEMPT_CAP + 1]
        )
        truncated = len(raw_rows) > QUOTA_ATTEMPT_CAP
        traffic_rows = raw_rows[:QUOTA_ATTEMPT_CAP]
        winner_graph_ids = {
            int(row["request_graph_id"])
            for row in traffic_rows
            if row.get("request_graph_id")
            and int(row.get("request_graph__winner_attempt_id") or 0)
            == int(row.get("id") or 0)
        }
        if winner_graph_ids:
            graph_plans = {
                int(row["id"]): row["candidate_plan"]
                for row in GeminiRequest.objects.filter(id__in=winner_graph_ids)
                .values("id", "candidate_plan")
            }

    rows_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    rows_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in traffic_rows:
        pair = (str(row.get("project_identity") or ""), str(row.get("model") or ""))
        rows_by_pair[pair].append(row)
        rows_by_model[pair[1]].append(row)

    fallback_from: Counter[str] = Counter()
    fallback_to: Counter[str] = Counter()
    fallback_wins_by_pair: Counter[tuple[str, str]] = Counter()
    for row in traffic_rows:
        if (
            not row.get("request_graph_id")
            or int(row.get("request_graph__winner_attempt_id") or 0)
            != int(row.get("id") or 0)
        ):
            continue
        primary = _first_plan_model(graph_plans.get(int(row["request_graph_id"]), []))
        winner_model = str(row.get("model") or "")
        if primary in MODELS and winner_model in MODELS and primary != winner_model:
            fallback_from[primary] += 1
            fallback_to[winner_model] += 1
            fallback_wins_by_pair[(str(row.get("project_identity") or ""), winner_model)] += 1

    matrix: list[dict[str, Any]] = []
    for model in MODELS:
        active_profile = profiles.get(model)
        for slot in slots:
            state = states.get((slot.identity, model)) if slot.identity else None
            profile = state.quota_profile if state is not None else active_profile
            profile_bound = bool(active_profile and profile and profile.pk == active_profile.pk)
            pair_rows = rows_by_pair.get((slot.identity, model), []) if slot.identity else []
            blocks = _public_blocks(state.provider_blocks if state else {}, generated_at)
            complete = bool(
                accounting_active
                and slot.configured
                and slot.mapping_state == "explicit"
                and profile is not None
            )
            minute_rows = [
                row for row in pair_rows
                if generated_at - _as_utc(row["provider_started_at"])
                <= dt.timedelta(seconds=60)
            ]
            minute_tokens = sum(
                _bounded_int(row.get("prompt_tokens"))
                or _bounded_int(row.get("reserved_prompt_tokens"))
                for row in minute_rows
            )
            calibrated = profile.estimator_version == gemini_accounting_runtime.ACTIVE_ESTIMATOR_VERSION if profile else None
            actual = any(_bounded_int(row.get("prompt_tokens")) for row in minute_rows)
            estimated = any(not _bounded_int(row.get("prompt_tokens")) and _bounded_int(row.get("reserved_prompt_tokens")) for row in minute_rows)
            usage_source = "mixed" if actual and estimated else "provider_reported" if actual else "estimated" if estimated else "no_observed_usage"
            same_day_state = bool(state and state.pacific_day == pacific_day)
            rpd_dispatched = state.rpd_dispatched if same_day_state else 0
            rpd_reserved = state.rpd_reserved if same_day_state else 0
            rpd_uncertain = state.rpd_uncertain if same_day_state else 0
            latencies = [
                _bounded_int(row.get("latency_ms"), maximum=600_000)
                for row in pair_rows if _bounded_int(row.get("latency_ms")) > 0
            ]
            traffic_evidence = _last_evidence(pair_rows)
            credential_incident = bool(state and (state.last_failure_kind in {
                "invalid_key", "model_not_found", "permission_denied", "model_unavailable",
            } or state.last_http_code == 401))
            latest_traffic_at = max((
                _as_utc(row["provider_started_at"]) for row in pair_rows
                if gemini_health.is_generation_traffic_evidence(row)
            ), default=None)
            state_negative_evidence = bool(state and state.last_failure_kind and (
                latest_traffic_at is None or (
                    state.last_failure_at is not None
                    and _as_utc(state.last_failure_at) >= latest_traffic_at
                )
            ))
            # Missing purpose-qualified rows weaken positive availability;
            # they never erase a finite, persisted negative incident.
            expose_state_failure = credential_incident or state_negative_evidence
            matrix.append({
                "model": model,
                "slot_id": slot.slot_id,
                "configured": slot.configured,
                "identity_mapping": slot.mapping_state,
                "status": _pair_status(
                    accounting_active=accounting_active,
                    slot=slot,
                    profile=active_profile,
                    state=state,
                    blocks=blocks,
                    now=generated_at,
                    traffic_rows=pair_rows,
                ),
                "profile": ({
                    "version": _public_version(profile.profile_version),
                    "limits": _profile_limits(profile),
                } if profile else None),
                "rpm": _metric(
                    used=len(minute_rows),
                    limit=profile.rpm_limit if profile else None,
                    complete=complete,
                ),
                "input_tpm": _tpm_metric(
                    used=minute_tokens, limit=profile.input_tpm_limit if profile else None,
                    complete=complete, calibrated=calibrated, usage_source=usage_source, profile_bound=profile_bound,
                ),
                "nonlive_profile": {
                    "calibration": "calibrated" if calibrated else "uncalibrated" if profile else "unknown",
                    "runtime_profile_binding": "matched" if profile_bound else "different" if active_profile and profile else "unknown",
                    "eligible_prerequisites": bool(complete and calibrated and profile_bound),
                    "authority": "prerequisites_only_not_dispatch_permission",
                },
                "rpd": _metric(
                    used=rpd_dispatched,
                    limit=profile.rpd_limit if profile else None,
                    reserved=rpd_reserved,
                    uncertain=rpd_uncertain,
                    complete=complete,
                ),
                "in_flight": (
                    _bounded_int(state.in_flight_count, maximum=100)
                    if complete and state else 0 if complete else None
                ),
                "provider_blocks": blocks if complete else [],
                "external_usage_suspected": (
                    bool(state.external_usage_suspected) if complete and state else False
                ),
                "usage_by_lane": _safe_counter(pair_rows, "lane", _PUBLIC_LANES) if complete else {},
                "usage_by_task_class": _safe_counter(
                    pair_rows, "request_graph__task_class", _PUBLIC_TASK_CLASSES
                ) if complete else {},
                "fallback_wins": fallback_wins_by_pair[(slot.identity, model)] if complete else None,
                "latency_ms": {
                    "p50": _percentile(latencies, 0.50) if complete else None,
                    "p95": _percentile(latencies, 0.95) if complete else None,
                },
                "last_success_at": max((_iso(row["provider_started_at"]) for row in pair_rows
                    if gemini_health.is_generation_traffic_evidence(row) and row.get("fsm_state") in {"succeeded", "succeeded_late"}), default=None),
                "last_failure_at": max((_iso(row["provider_started_at"]) for row in pair_rows
                    if gemini_health.is_generation_traffic_evidence(row) and row.get("fsm_state") in {"failed", "timeout_ambiguous"}), default=None),
                "last_failure_kind": (
                    _public_code(state.last_failure_kind, _PUBLIC_FAILURE_KINDS) if expose_state_failure else
                    traffic_evidence["failure_kind"] if traffic_evidence and not traffic_evidence["success"] else ""
                ),
                "last_http_code": (
                    _bounded_int(state.last_http_code, maximum=599) or None if expose_state_failure else
                    traffic_evidence["http_code"] if traffic_evidence else None
                ),
                "last_real_evidence": traffic_evidence,
            })

    models: list[dict[str, Any]] = []
    for model in MODELS:
        model_rows = [row for row in matrix if row["model"] == model]
        traffic = rows_by_model.get(model, [])
        complete_rows = [row for row in model_rows if row["rpm"]["complete"]]
        latencies = [
            _bounded_int(row.get("latency_ms"), maximum=600_000)
            for row in traffic if _bounded_int(row.get("latency_ms")) > 0
        ]

        def aggregate_metric(name: str) -> dict[str, Any]:
            if name == "input_tpm":
                result = _metric(used=0, limit=None, complete=False)
                result.update(used=sum(row[name]["used"] or 0 for row in complete_rows) if complete_rows else None,
                    observed_usage_known=bool(complete_rows), headroom_known=False,
                    usage_source="local_aggregate" if complete_rows else "unknown",
                    calibration="calibrated" if complete_rows and all(row[name]["headroom_known"] for row in complete_rows) else "uncalibrated" if complete_rows else "unknown")
                if complete_rows and all(row[name]["headroom_known"] for row in complete_rows):
                    result.update(_metric(used=result["used"], limit=sum(row[name]["limit"] for row in complete_rows),
                        complete=len(complete_rows) == len([row for row in model_rows if row["configured"]])))
                    result["used"] = sum(row[name]["used"] or 0 for row in complete_rows)
                    result["headroom_known"] = result["complete"]
                return result
            if not complete_rows:
                return _metric(used=0, limit=None, complete=False)
            metrics = [row[name] for row in complete_rows]
            return {
                "used": sum(item["used"] for item in metrics),
                "limit": sum(item["limit"] for item in metrics if item["limit"] is not None),
                "remaining": sum(item["remaining"] for item in metrics if item["remaining"] is not None),
                "reserved": sum(item["reserved"] for item in metrics),
                "uncertain": sum(item["uncertain"] for item in metrics),
                "complete": len(complete_rows) == len([row for row in model_rows if row["configured"]]),
            }

        models.append({
            "model": model,
            "projects": model_rows,
            "coverage": {
                "slots": len(model_rows),
                "configured": sum(1 for row in model_rows if row["configured"]),
                "accounted": len(complete_rows),
            },
            "rpm": aggregate_metric("rpm"),
            "input_tpm": aggregate_metric("input_tpm"),
            "rpd": aggregate_metric("rpd"),
            "in_flight": (
                sum(row["in_flight"] or 0 for row in complete_rows)
                if complete_rows else None
            ),
            "usage_by_lane": _safe_counter(traffic, "lane", _PUBLIC_LANES) if complete_rows else {},
            "usage_by_task_class": _safe_counter(
                traffic, "request_graph__task_class", _PUBLIC_TASK_CLASSES
            ) if complete_rows else {},
            "fallbacks_from": fallback_from[model] if complete_rows else None,
            "fallbacks_to": fallback_to[model] if complete_rows else None,
            "latency_ms": {
                "p50": _percentile(latencies, 0.50) if complete_rows else None,
                "p95": _percentile(latencies, 0.95) if complete_rows else None,
            },
            "external_usage_suspected": any(
                row["external_usage_suspected"] for row in complete_rows
            ),
            "last_success_at": max(
                (row["last_success_at"] for row in model_rows if row["last_success_at"]),
                default=None,
            ),
            "last_failure_at": max(
                (row["last_failure_at"] for row in model_rows if row["last_failure_at"]),
                default=None,
            ),
        })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(generated_at),
        "accounting": {
            "mode": accounting_mode if accounting_mode in {"off", "shadow"} else "invalid",
            "runtime_active": accounting_active,
            **_nonlive_accounting_fields(accounting_active=accounting_active),
            "traffic_window_seconds": int(QUOTA_TRAFFIC_WINDOW.total_seconds()),
            "traffic_truncated": truncated,
        },
        "pacific_day": pacific_day.isoformat(),
        "pacific_day_started_at": _iso(pacific_start),
        "pacific_reset_at": _iso(pacific_reset),
        "models": models,
    }
    _assert_no_sensitive_values(payload, _quota_sensitive_values(slots))
    return payload


def _read_only_bot_settings() -> InstagramBotSettings:
    row = (
        InstagramBotSettings.objects.filter(pk=1)
        .only("gemini_routing_mode", "pinned_chat_model", "pinned_until")
        .first()
    )
    return row if row is not None else InstagramBotSettings(pk=1)


def build_routes_payload(*, now=None) -> dict[str, Any]:
    generated_at = _aware_now(now)
    settings_obj = _read_only_bot_settings()
    base_no_model = gemini_routing.classify_live_turn(
        gemini_routing.TurnFacts(deterministic_action="authoritative_reply"),
        now=generated_at,
    )
    base_ordinary = gemini_routing.classify_live_turn(
        gemini_routing.TurnFacts(), now=generated_at
    )
    effective_ordinary = gemini_routing.classify_live_turn(
        gemini_routing.TurnFacts(), settings_obj=settings_obj, now=generated_at
    )
    base_complex = gemini_routing.classify_live_turn(
        gemini_routing.TurnFacts(has_image=True), now=generated_at
    )
    effective_complex = gemini_routing.classify_live_turn(
        gemini_routing.TurnFacts(has_image=True), settings_obj=settings_obj,
        now=generated_at,
    )
    analysis = gemini_routing.durable_analysis_decision()
    decisions = {
        gemini_routing.TaskClass.NO_MODEL: (base_no_model, base_no_model),
        gemini_routing.TaskClass.ORDINARY_LIVE: (base_ordinary, effective_ordinary),
        gemini_routing.TaskClass.COMPLEX_LIVE: (base_complex, effective_complex),
        gemini_routing.TaskClass.DURABLE_ANALYSIS: (analysis, analysis),
    }
    routes = []
    for task_class in gemini_routing.TaskClass:
        base, effective = decisions[task_class]
        definition = gemini_routing.PUBLIC_TASK_CLASS_DEFINITIONS[task_class]
        routes.append({
            "task_class": task_class.value,
            "title": definition["title"],
            "definition": definition["definition"],
            "lane": effective.lane,
            "base_chain": list(base.model_chain),
            "effective_chain": list(effective.model_chain),
            "deadline_ms": effective.deadline_ms,
            "routing_mode": effective.routing_mode.value,
            "escalation_chain": (
                list(gemini_routing.ANALYSIS_ESCALATION_CHAIN)
                if task_class == gemini_routing.TaskClass.DURABLE_ANALYSIS
                else []
            ),
        })
    pinned = gemini_routing.active_pin(settings_obj, now=generated_at)
    effective_from = gemini_accounting_runtime.parse_effective_from(
        getattr(settings, "GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM", "")
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(generated_at),
        "policy_version": gemini_routing.POLICY_VERSION,
        "authority_snapshot_version": gemini_routing.AUTHORITY_SNAPSHOT_VERSION,
        "accounting": {
            "mode": (
                gemini_accounting_runtime.configured_mode()
                if gemini_accounting_runtime.configured_mode() in {"off", "shadow"}
                else "invalid"
            ),
            "runtime_active": gemini_accounting_runtime.shadow_runtime_active(now=generated_at),
            **_nonlive_accounting_fields(accounting_active=gemini_accounting_runtime.shadow_runtime_active(now=generated_at)),
            "effective_from": _iso(effective_from),
        },
        "emergency_pin": {
            "active": bool(pinned),
            "model": pinned or None,
            "expires_at": _iso(settings_obj.pinned_until) if pinned else None,
        },
        "routes": routes,
    }
    route_slots = _slot_identities()
    _assert_no_sensitive_values(payload, _quota_sensitive_values(route_slots))
    return payload


def _encode_cursor(row: GeminiRequest) -> str:
    from cryptography.fernet import Fernet

    plaintext = json.dumps(
        {"created_at": _iso(row.created_at), "id": int(row.pk)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return Fernet(_cursor_key()).encrypt(plaintext).decode("ascii")


def _decode_cursor(value: str) -> tuple[dt.datetime, int] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > 512:
        raise InvalidCursor("invalid_cursor")
    try:
        from cryptography.fernet import Fernet, InvalidToken

        plaintext = Fernet(_cursor_key()).decrypt(
            raw.encode("ascii"), ttl=CURSOR_TTL_SECONDS
        )
        payload = json.loads(plaintext.decode("utf-8"))
        created_at = dt.datetime.fromisoformat(str(payload["created_at"]))
        row_id = int(payload["id"])
    except (InvalidToken, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise InvalidCursor("invalid_cursor") from exc
    if timezone.is_naive(created_at) or row_id <= 0:
        raise InvalidCursor("invalid_cursor")
    return _as_utc(created_at), row_id


def _cursor_key() -> bytes:
    secret = str(settings.SECRET_KEY or "").encode("utf-8")
    if not secret:
        raise PublicProjectionError("cursor_key_unavailable")
    digest = hmac.new(secret, _CURSOR_KEY_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)


def _parse_limit(value: Any) -> int:
    try:
        parsed = int(value or ATTEMPT_PAGE_DEFAULT)
    except (TypeError, ValueError, OverflowError):
        parsed = ATTEMPT_PAGE_DEFAULT
    return min(ATTEMPT_PAGE_MAX, max(1, parsed))


def _attempt_slot(row: GeminiRequestAttempt, identity_to_slot: dict[str, str]) -> str | None:
    direct = SLOT_BY_ALIAS.get(str(row.key_name or ""))
    if direct:
        return direct
    return identity_to_slot.get(str(row.project_identity or "")) or None


def _manifest_code_counts(values):
    counts = Counter(value if value in _PUBLIC_MANIFEST_CODES else "other" for value in values)
    return dict(sorted(counts.items()))


def project_request_context(graph):
    """Project captured metadata, never reconstruct from current customer state.

    Protected source IDs, digests and arbitrary identifier/version strings are
    omitted. Binding means the immutable graph agrees; it is not a new source
    freshness or dispatch authority decision.
    """
    result = {"status": "uncaptured", "reason": "legacy_context_uncaptured",
        "reconstruction": "full_payload_not_retained", "counts": {}, "budgets": {},
        "readiness": {}, "omissions": {}, "digest_presence": {}, "publication": {}, "versions": {}}
    policy = graph.policy_manifest
    if not isinstance(policy, dict):
        return {**result, "status": "invalid", "reason": "policy_manifest_invalid"}, None
    if "request_context" not in policy:
        return result, None
    from management.services.gemini_accounting_contract import RequestPolicyManifestError, sanitize_request_policy_manifest
    try:
        safe_policy = sanitize_request_policy_manifest(policy)
        context = safe_policy["request_context"]
    except (RequestPolicyManifestError, KeyError, TypeError, ValueError, RecursionError):
        return {**result, "status": "invalid", "reason": "request_context_invalid"}, None
    expected = f"ig-revision:{context['revision_id']}"
    if (not context["revision_id"] or not context["client_id"] or context["client_id"] != graph.client_id
            or graph.logical_turn_id != expected or graph.source_execution_key != expected
            or graph.source_message_id not in context["source_message_ids"]):
        return {**result, "status": "invalid", "reason": "request_context_binding_mismatch"}, None
    counts = {"source_messages": len(context["source_message_ids"]), "history_messages": len(context["history_message_ids"]),
        "selected_blocks": len(context["selected_block_ids"]), "omitted_blocks": len(context["omitted_blocks"]),
        **{key: len(value) for key, value in context["media"].items()}}
    publication = safe_policy["instruction_publication"]
    result.update(status="captured", reason="immutable_graph_binding_checked", payload_stage="logical_input",
        effective_mode=context["effective_mode"], counts=counts,
        budgets={key: value if value <= 2**53 - 1 else None for key, value in context["budgets"].items()},
        readiness=_manifest_code_counts(context["readiness_codes"]),
        omissions=_manifest_code_counts([value["reason"] for value in context["omitted_blocks"]]),
        digest_presence={"context": bool(context["context_digest"]), "logical_input": bool(context["request_digest"]),
            "source_bundle": bool(context["bundle_digest"]), "response_plan": bool(context["view_versions"].get("response_plan_digest")),
            "memory_capture": bool(context["view_versions"].get("memory_capture_digest"))},
        publication={"version": publication["version"], "hash": publication["hash"],
            "hash_purpose": "published_instruction_collection"},
        versions={"builder": "ig-turn-intelligence.v1" if context["builder_version"] == "ig-turn-intelligence.v1" else "unrecognized",
            "memory_head_present": bool(context["view_versions"].get("memory_head_version")),
            "canonical_selection": "source-selection.v1" if context["view_versions"].get("canonical_selection") == "source-selection.v1" else "unknown",
            "state_view": "ig-client-state.v1" if context["view_versions"].get("state_view_version") == "ig-client-state.v1" else "unknown"})
    return result, context


def project_dispatch_manifest(row, *, graph=None, context=None):
    result = {"status": "uncaptured", "reason": "dispatch_not_captured", "payload_stage": "http_dispatch",
        "provider_phase": "recorded_started" if row.provider_started_at else "not_recorded_started",
        "http_receipt_recorded": isinstance(row.http_code, int) and not isinstance(row.http_code, bool) and 100 <= row.http_code <= 599, "digest_present": False}
    if not row.dispatch_manifest:
        if row.provider_started_at:
            result["reason"] = "provider_phase_started_without_capture"
        return result
    from management.services.gemini_accounting_contract import RequestPolicyManifestError
    from management.services.ig_request_manifest import sanitize_dispatch_context
    try:
        dispatch = sanitize_dispatch_context(row.dispatch_manifest)
    except (RequestPolicyManifestError, TypeError, ValueError, RecursionError):
        return {**result, "status": "invalid", "reason": "dispatch_manifest_invalid"}
    if graph is None or context is None:
        return {**result, "status": "invalid", "reason": "request_context_unavailable"}
    if (row.request_graph_id != graph.pk or row.request_id != graph.request_id or row.client_id != graph.client_id
            or row.logical_turn_id != graph.logical_turn_id or row.source_message_id != graph.source_message_id
            or dispatch["revision_id"] != context["revision_id"] or dispatch["client_id"] != context["client_id"]
            or dispatch["attempt_index"] != row.attempt_index or dispatch["model"] != row.model
            or dispatch["logical_request_digest"] != context["request_digest"]):
        return {**result, "status": "invalid", "reason": "dispatch_binding_mismatch"}
    return {**result, "status": "captured", "reason": "immutable_attempt_binding_checked", "digest_present": True}


def project_validation_reasons(row):
    from management.services.ig_reply_truth import REASON_CODES
    known = frozenset(REASON_CODES) | {
        "invalid_response_schema", "invalid_result", "validator_error", "authority_unavailable",
        "missing_turn_intelligence", "unknown_inline_coverage", "unknown_inline_hashes",
        "actual_media_binding_mismatch", "incomplete_image_coverage", "catalog_selector_missing",
        "unnecessary_manager_handoff", "source_preference_mismatch",
    } | {"schema_" + name for name in (
        "invalid_json", "malformed_payload", "invalid_reply_text", "too_many_controls",
        "control_token_in_reply_text", "malformed_control", "invalid_control", "conflicting_control", "invalid_turn_intelligence",
    )}
    kind = row.failure_kind
    if kind not in {"local_semantic_rejection", "invalid_response"}:
        return {"layer": "none", "reason_codes": [], "unknown_reason_count": 0}
    raw = row.error_detail if isinstance(row.error_detail, str) else ""
    codes = [value for value in raw[:120].split(",") if value]
    safe = list(dict.fromkeys(value for value in codes if value in known))[:12]
    layer = "local_semantic" if kind == "local_semantic_rejection" else "schema" if any(value.startswith("schema_") or value == "invalid_response_schema" for value in safe) else "unknown"
    return {"layer": layer, "reason_codes": safe, "unknown_reason_count": sum(value not in known for value in codes)}


def _public_attempt(
    row: GeminiRequestAttempt,
    *,
    identity_to_slot: dict[str, str],
    winner_id: int | None,
    graph=None,
    context=None,
) -> dict[str, Any]:
    fsm = _public_code(row.fsm_state, _PUBLIC_FSM, default="unknown")
    if row.not_attempted_reason:
        public_outcome = "not_attempted"
    elif fsm in {"succeeded", "succeeded_late"}:
        public_outcome = fsm
    elif fsm in {"failed", "timeout_ambiguous", "cancelled_pre_dispatch"}:
        public_outcome = fsm
    elif fsm in {"planned", "reserved", "provider_started"}:
        public_outcome = fsm
    else:
        public_outcome = "unknown"
    return {
        "dispatch_capture": project_dispatch_manifest(row, graph=graph, context=context),
        "validation": project_validation_reasons(row),
        "admission": {
            "mode": row.accounting_mode if row.accounting_mode in {"off", "shadow", "enforced", "emergency"} else "unknown",
            "role": row.role if row.role in {"chat", "management", "checker", "call", "diagnostic", "health_metadata", "health_probe"} else "unknown",
            "decision": row.shadow_decision if row.shadow_decision in {"allow", "deny", "unknown"} else "unknown",
            "reason": _public_code(row.shadow_deny_reason, _PUBLIC_ADMISSION_REASONS, default="other") if row.shadow_deny_reason else "",
            "authority": "recorded_admission_not_delivery_receipt",
        },
        "attempt_index": _bounded_int(row.attempt_index, maximum=65535),
        "candidate_index": _bounded_int(row.candidate_index, maximum=65535),
        "slot_id": _attempt_slot(row, identity_to_slot),
        "model": _public_model(row.model),
        "fsm_state": fsm,
        "outcome": public_outcome,
        "not_attempted_reason": (
            _public_code(row.not_attempted_reason, _PUBLIC_NOT_ATTEMPTED)
            if row.not_attempted_reason else ""
        ),
        "failure_kind": (
            _public_code(row.failure_kind, _PUBLIC_FAILURE_KINDS)
            if row.failure_kind else ""
        ),
        "http_code": _bounded_int(row.http_code, maximum=599) or None,
        "latency_ms": _bounded_int(row.latency_ms, maximum=600_000),
        "winner": bool(row.pk == winner_id),
        "provider_started_at": _iso(row.provider_started_at),
        "finished_at": _iso(row.finished_at),
        "quota_block": ({
            "metric": _public_code(
                row.provider_quota_metric, _PUBLIC_QUOTA_METRICS, default="unknown"
            ),
            "quota_id": _public_quota_id(row.provider_quota_id),
            "dimensions": _public_quota_dimensions(
                row.provider_quota_dimensions
            ),
            "retry_after_seconds": _bounded_int(
                row.provider_retry_after_seconds, maximum=7 * 24 * 3600
            ),
            "until": _iso(row.provider_block_until),
        } if row.http_code == 429 else None),
        "reply_linked": bool(row.reply_message_id),
    }


def _public_candidate_plan(
    plan: Any,
    *,
    identity_to_slot: dict[str, str],
    attempts_by_candidate: dict[int, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(plan, list):
        return [], False
    truncated = len(plan) > ATTEMPTS_PER_REQUEST_CAP
    result = []
    for position, raw in enumerate(plan[:ATTEMPTS_PER_REQUEST_CAP], start=1):
        if not isinstance(raw, dict):
            continue
        candidate_index = _bounded_int(raw.get("candidate_index"), maximum=65535) or position
        candidate_attempts = attempts_by_candidate.get(candidate_index, [])
        if any(item["provider_started_at"] for item in candidate_attempts):
            execution_state = "attempted"
        elif candidate_attempts and all(
            item["outcome"] in {"not_attempted", "cancelled_pre_dispatch"}
            for item in candidate_attempts
        ):
            execution_state = "not_attempted"
        elif candidate_attempts:
            execution_state = "pending"
        else:
            execution_state = "not_recorded"
        identity = str(raw.get("project_identity") or "")
        slot_id = identity_to_slot.get(identity)
        identity_status = str(raw.get("identity_status") or "unknown").casefold()
        result.append({
            "candidate_index": candidate_index,
            "slot_id": slot_id,
            "project_state": (
                "mapped" if slot_id and identity_status == "known" else "unknown"
            ),
            "model": _public_model(raw.get("model")),
            "initial_skip_reason": (
                _public_code(raw.get("initial_skip_reason"), _PUBLIC_NOT_ATTEMPTED)
                if raw.get("initial_skip_reason") else ""
            ),
            "execution_state": execution_state,
            "outcomes": candidate_attempts,
        })
    return result, truncated


def _effective_reply_link(graph: GeminiRequest) -> tuple[int | None, str]:
    graph_reply = int(graph.reply_message_id or 0)
    winner_reply = int(
        getattr(graph.winner_attempt, "reply_message_id", 0) or 0
    )
    if graph_reply and winner_reply and graph_reply != winner_reply:
        return None, "conflict"
    if graph_reply and winner_reply:
        return graph_reply, "graph_and_winner"
    if graph_reply:
        return graph_reply, "graph"
    if winner_reply:
        return winner_reply, "winner"
    return None, "none"


def _public_reply(
    reply: dict[str, Any] | None,
    *,
    effective_id: int | None,
    link_source: str,
) -> dict[str, Any]:
    if link_source == "conflict":
        return {
            "state": "link_conflict", "link_source": "conflict",
            "message_status": None, "send_state": None,
            "provider_receipt_present": False, "planned_chunks": 0,
            "delivered_chunks": 0,
        }
    if effective_id is None:
        return {
            "state": "not_linked", "link_source": "none",
            "message_status": None, "send_state": None,
            "provider_receipt_present": False, "planned_chunks": 0,
            "delivered_chunks": 0,
        }
    if reply is None:
        return {
            "state": "missing", "link_source": link_source,
            "message_status": None, "send_state": None,
            "provider_receipt_present": False, "planned_chunks": 0,
            "delivered_chunks": 0,
        }
    message_status = _public_code(
        reply.get("status"), _PUBLIC_MESSAGE_STATUSES, default="unknown"
    )
    send_state = _public_code(
        reply.get("send_state"), _PUBLIC_SEND_STATES, default="unknown"
    )
    receipt_ids = reply.get("delivery_provider_message_ids")
    receipt_ids = receipt_ids if isinstance(receipt_ids, list) else []
    return {
        "state": "persisted",
        "link_source": link_source,
        "message_status": message_status,
        "send_state": send_state,
        "provider_receipt_present": bool(reply.get("provider_message_id") or receipt_ids),
        "planned_chunks": _bounded_int(
            reply.get("delivery_planned_chunk_count"), maximum=100
        ),
        "delivered_chunks": _bounded_int(
            reply.get("delivery_delivered_chunk_count"), maximum=100
        ),
    }


def build_attempts_payload(*, cursor="", limit=None, now=None) -> dict[str, Any]:
    generated_at = _aware_now(now)
    page_size = _parse_limit(limit)
    decoded_cursor = _decode_cursor(cursor)
    slots = _slot_identities()
    identity_to_slot = _identity_to_slot(slots)
    attempt_queryset = GeminiRequestAttempt.objects.order_by(
        "candidate_index", "attempt_index", "id"
    )[:ATTEMPTS_PER_REQUEST_CAP + 1]
    query = GeminiRequest.objects.select_related("winner_attempt").prefetch_related(
        Prefetch("attempts", queryset=attempt_queryset, to_attr="public_attempt_rows")
    ).order_by("-created_at", "-id")
    if decoded_cursor:
        cursor_at, cursor_id = decoded_cursor
        query = query.filter(
            Q(created_at__lt=cursor_at)
            | Q(created_at=cursor_at, id__lt=cursor_id)
        )
    graphs = list(query[:page_size + 1])
    has_more = len(graphs) > page_size
    graphs = graphs[:page_size]

    reply_ids = {
        int(graph.reply_message_id)
        for graph in graphs if graph.reply_message_id
    }
    for graph in graphs:
        if graph.winner_attempt and graph.winner_attempt.reply_message_id:
            reply_ids.add(int(graph.winner_attempt.reply_message_id))
        reply_ids.update(
            int(row.reply_message_id)
            for row in graph.public_attempt_rows if row.reply_message_id
        )
    replies = {
        int(row["id"]): row
        for row in InstagramBotMessage.objects.filter(id__in=reply_ids).values(
            "id", "status", "send_state", "provider_message_id",
            "delivery_provider_message_ids", "delivery_planned_chunk_count",
            "delivery_delivered_chunk_count",
        )
    } if reply_ids else {}

    items = []
    sensitive_values: list[str] = []
    for graph in graphs:
        sensitive_values.extend([
            str(graph.request_id or ""),
            str(graph.logical_turn_id or ""),
        ])
        capture_public, captured_context = project_request_context(graph)
        raw_attempts = list(graph.public_attempt_rows)
        projected_attempts = [
            _public_attempt(
                row,
                identity_to_slot=identity_to_slot,
                winner_id=graph.winner_attempt_id, graph=graph, context=captured_context,
            )
            for row in raw_attempts[:ATTEMPTS_PER_REQUEST_CAP]
        ]
        attempts_by_candidate: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in projected_attempts:
            attempts_by_candidate[item["candidate_index"]].append(item)
        plan, plan_truncated = _public_candidate_plan(
            graph.candidate_plan,
            identity_to_slot=identity_to_slot,
            attempts_by_candidate=attempts_by_candidate,
        )
        winner = None
        if graph.winner_attempt is not None:
            winner = _public_attempt(
                graph.winner_attempt,
                identity_to_slot=identity_to_slot,
                winner_id=graph.winner_attempt_id, graph=graph, context=captured_context,
            )
        effective_reply_id, reply_link_source = _effective_reply_link(graph)
        resolution = (
            _public_code(graph.terminal_resolution, _PUBLIC_RESOLUTIONS, default="other")
            if graph.terminal_resolution else "pending"
        )
        terminal_reason = (
            _public_code(graph.terminal_reason, _PUBLIC_TERMINAL_REASONS)
            if graph.terminal_reason else ""
        )
        items.append({
            "context_capture": capture_public,
            "request_ref": gemini_health.public_request_reference(graph.request_id),
            "turn_ref": _opaque_reference("turn-ref", graph.logical_turn_id, "gturn"),
            "client_ref": _opaque_reference("client-ref", graph.client_id, "gclient"),
            "created_at": _iso(graph.created_at),
            "lane": _public_code(graph.lane, _PUBLIC_LANES, default="unknown"),
            "task_class": _public_code(
                graph.task_class, _PUBLIC_TASK_CLASSES, default="unknown"
            ),
            "policy_version": _public_version(
                graph.routing_policy_version, limit=32
            ),
            "accounting_mode": (
                graph.accounting_mode
                if graph.accounting_mode in {"off", "shadow", "enforced", "emergency"}
                else "unknown"
            ),
            "deadline_ms": _bounded_int(graph.deadline_ms, maximum=600_000),
            "candidate_plan": plan,
            "candidate_plan_truncated": plan_truncated,
            "attempts": projected_attempts,
            "attempts_truncated": len(raw_attempts) > ATTEMPTS_PER_REQUEST_CAP,
            "winner": winner,
            "resolution": {
                "state": resolution,
                "reason": terminal_reason,
                "resolved_at": _iso(graph.resolved_at),
            },
            "reply": _public_reply(
                replies.get(effective_reply_id) if effective_reply_id else None,
                effective_id=effective_reply_id,
                link_source=reply_link_source,
            ),
        })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(generated_at),
        "limit": page_size,
        "items": items,
        "next_cursor": _encode_cursor(graphs[-1]) if has_more and graphs else None,
        "retention": {"technical_ledger_retention": "unbounded_currently", "automated_purge": "not_configured",
            "cursor_ttl_seconds": CURSOR_TTL_SECONDS, "render_cap": PUBLIC_HISTORY_CAP},
    }
    _assert_no_sensitive_values(
        payload,
        [*_quota_sensitive_values(slots), *sensitive_values],
    )
    return payload


def _quota_sensitive_values(slots: list[_SlotIdentity]) -> list[str]:
    values = [*gemini_keys.ALL_KEYS]
    values.extend(slot.identity for slot in slots if slot.identity)
    values.extend(
        str(gemini_keys._key_value(alias) or "")
        for alias in gemini_keys.ALL_KEYS
    )
    return values


def _assert_no_sensitive_values(payload: dict[str, Any], values) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for value in values:
        normalized = str(value or "").strip()
        if len(normalized) >= 6 and normalized in serialized:
            raise PublicProjectionError("sensitive_value_in_public_projection")
    forbidden_keys = {
        "alias", "client_id", "error_detail", "key_name", "logical_turn_id",
        "project_group", "project_identity", "provider_reason", "request_id",
        "source_message_id", "source_message_ids", "history_message_ids", "provider_body", "prompt",
        "customer_text", "request_digest", "logical_request_digest", "context_digest", "bundle_digest",
    }

    def walk(value):
        if isinstance(value, dict):
            if forbidden_keys.intersection(value):
                raise PublicProjectionError("forbidden_public_field")
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
