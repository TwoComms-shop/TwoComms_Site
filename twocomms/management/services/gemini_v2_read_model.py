"""Provider-free, strictly redacted read models for Gemini V2 admin APIs.

The module deliberately has no provider or probe dependency.  It projects
existing immutable request/attempt evidence and local quota state into four
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
from django.db.models import Prefetch, Q
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
    rows = list(
        GeminiQuotaProfile.objects.filter(
            model__in=MODELS,
            effective_from__lte=now,
        )
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=now))
        .order_by("model", "-effective_from", "-id")
    )
    result: dict[str, GeminiQuotaProfile] = {}
    for row in rows:
        result.setdefault(row.model, row)
    return result


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
    if success_at and (failure_at is None or success_at >= failure_at):
        age = (now - success_at).total_seconds()
        if 0 <= age <= RECENT_SUCCESS_SECONDS:
            return "confirmed_recent_success"
        # A successful request after the last failure is still evidence that
        # the pair recovered.  It may be too old for the stronger "recent"
        # badge, but historical 503/timeout state must not keep the pair red
        # forever when no quota block is active.
        if age > RECENT_SUCCESS_SECONDS:
            return "available_assumed"
    if state.accounting_status == GeminiQuotaState.AccountingStatus.DEGRADED:
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


def build_quotas_payload(*, now=None) -> dict[str, Any]:
    """Return a bounded 4x6 local quota projection without writes/provider I/O."""
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
                "project_identity", "lane", "request_graph__task_class",
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
        profile = profiles.get(model)
        for slot in slots:
            state = states.get((slot.identity, model)) if slot.identity else None
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
            same_day_state = bool(state and state.pacific_day == pacific_day)
            rpd_dispatched = state.rpd_dispatched if same_day_state else 0
            rpd_reserved = state.rpd_reserved if same_day_state else 0
            rpd_uncertain = state.rpd_uncertain if same_day_state else 0
            latencies = [
                _bounded_int(row.get("latency_ms"), maximum=600_000)
                for row in pair_rows if _bounded_int(row.get("latency_ms")) > 0
            ]
            matrix.append({
                "model": model,
                "slot_id": slot.slot_id,
                "configured": slot.configured,
                "identity_mapping": slot.mapping_state,
                "status": _pair_status(
                    accounting_active=accounting_active,
                    slot=slot,
                    profile=profile,
                    state=state,
                    blocks=blocks,
                    now=generated_at,
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
                "input_tpm": _metric(
                    used=minute_tokens,
                    limit=profile.input_tpm_limit if profile else None,
                    complete=complete,
                ),
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
                "last_success_at": _iso(state.last_success_at) if state else None,
                "last_failure_at": _iso(state.last_failure_at) if state else None,
                "last_failure_kind": (
                    _public_code(state.last_failure_kind, _PUBLIC_FAILURE_KINDS)
                    if state and state.last_failure_kind else ""
                ),
                "last_http_code": (
                    _bounded_int(state.last_http_code, maximum=599) or None
                    if state else None
                ),
                "last_real_evidence": _last_evidence(pair_rows),
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
            if not complete_rows:
                return _metric(
                    used=0,
                    # A per-project profile does not prove how many configured
                    # identities belong to the aggregate pool.  Keep aggregate
                    # capacity unknown until at least one explicit slot is
                    # actively accounted.
                    limit=None,
                    complete=False,
                )
            metrics = [row[name] for row in complete_rows]
            return {
                "used": sum(item["used"] for item in metrics),
                "limit": sum(item["limit"] for item in metrics if item["limit"] is not None),
                "remaining": sum(item["remaining"] for item in metrics if item["remaining"] is not None),
                "reserved": sum(item["reserved"] for item in metrics),
                "uncertain": sum(item["uncertain"] for item in metrics),
                "complete": len(complete_rows) == len([
                    row for row in model_rows if row["configured"]
                ]),
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
            raw.encode("ascii"), ttl=30 * 24 * 3600
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


def _public_attempt(
    row: GeminiRequestAttempt,
    *,
    identity_to_slot: dict[str, str],
    winner_id: int | None,
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
        raw_attempts = list(graph.public_attempt_rows)
        projected_attempts = [
            _public_attempt(
                row,
                identity_to_slot=identity_to_slot,
                winner_id=graph.winner_attempt_id,
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
                winner_id=graph.winner_attempt_id,
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
        "source_message_id",
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
