"""Cached, sanitized observations of existing owners; never send admission.

The overview has no overall green flag. Consumer liveness, sampled queue risk,
materialized reply obligations and local capacity evidence are separate facts.
No client text, identifiers, private notes or exception strings enter the cache.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone as dt_timezone
import math

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.db import DatabaseCache
from django.db import DatabaseError, connections
from django.db.models import Count, Min, Q
from django.utils import timezone


SCHEMA_VERSION = "ig-overview.v1"
CACHE_KEY = "ig_overview_read_model:v1"
CACHE_SECONDS = 30
FRESH_SECONDS = 60
OBSERVATION_LIMIT = 3
QUERY_LIMIT = 64
QUERY_BUDGETS = {"daemon": 4, "lanes": 38, "tasks": 2, "attention": 2,
                 "memory_generation": 2, "quotas": 12, "routes": 4}
LANES = ("customer_revisions", "legacy_inbound", "legacy_outbound", "revision_delivery",
         "manager_notifications", "conversation_analysis", "analysis_materialization",
         "reply_recovery", "binotel_analysis", "typed_memory", "trace_refresh")
BUCKETS = ("runnable", "processing", "manual", "manager_owned", "deferred",
           "failed", "historical", "unknown", "attention")
STATES = frozenset({"healthy", "observed", "attention", "stalled", "disabled",
                    "unavailable", "coverage_incomplete", "unobserved", "stale",
                    "failed", "degraded", "not_observed", "running", "maintenance",
                    "pause_pending", "worker_stalled", "starting", "idle"})
FAILURES = frozenset({"observation_unavailable", "query_budget_exceeded", "read_only_violation",
                     "settings_unavailable", "main_progress_missing", "main_progress_stale",
                     "main_progress_error", "worker_lane_stalled"})


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _count(value):
    value = _number(value)
    return int(value) if value is not None else None


def _choice(value, choices, default="unknown"):
    return value if isinstance(value, str) and value in choices else default


def _date(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.astimezone(dt_timezone.utc) if isinstance(value, datetime) and timezone.is_aware(value) else None


def _iso(value):
    value = _date(value)
    return value.isoformat() if value else None


def _age(value, now):
    value = _date(value)
    return max(0, int((now - value).total_seconds())) if value else None


def _unavailable(reason="observation_unavailable"):
    return {"available": False, "reason": _choice(reason, FAILURES, "observation_unavailable")}


class _ReadBudget:
    """Reject SQL effects and excessive owner reads before execution."""
    def __init__(self):
        self.component = ""
        self.total = 0
        self.counts = dict.fromkeys(QUERY_BUDGETS, 0)
        self.rejected = {}

    def __call__(self, execute, sql, params, many, context):
        normalized = " ".join(sql.strip().upper().split())
        reason = ""
        if not normalized.startswith("SELECT ") or any(token in normalized for token in (" FOR UPDATE", " LOCK IN SHARE MODE", " INTO OUTFILE", " INTO DUMPFILE")):
            reason = "read_only_violation"
        elif self.total >= QUERY_LIMIT or self.counts[self.component] >= QUERY_BUDGETS[self.component]:
            reason = "query_budget_exceeded"
        if reason:
            self.rejected[self.component] = reason
            raise DatabaseError(reason)
        self.total += 1
        self.counts[self.component] += 1
        return execute(sql, params, many, context)


def _reply_attention(now):
    from management.models import IgFollowUpTask
    from management.services.ig_response_debt import unresolved_reply_debts
    from management.services.ig_attention import attention_snapshot
    row = unresolved_reply_debts().filter(
        client__hidden_at__isnull=True, client__privacy_erasure_started_at__isnull=True,
    ).aggregate(count=Count("pk"), since=Min("event_occurred_at"))
    required = bool(row["count"])
    projected = attention_snapshot(now=now, response_debt={"required": required, "since": row["since"]})
    human = IgFollowUpTask.objects.filter(kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason="human_reply:delivery_unknown", client__hidden_at__isnull=True,
        client__privacy_erasure_started_at__isnull=True).exclude(
        status__in=(IgFollowUpTask.Status.COMPLETED, IgFollowUpTask.Status.CANCELLED),
    ).aggregate(count=Count("pk"), oldest_created_at=Min("created_at"))
    return {"available": True, "required": required, "count": row["count"],
            "owner": "manager" if required else "none", "since": _iso(row["since"]),
            "waiting_age_seconds": projected["observed_seconds"],
            "coverage": "materialized_reply_debt_visible_clients", "exact": True,
            "human_unknown_cases": {"count": human["count"], "oldest_created_at": _iso(human["oldest_created_at"]),
                "exact": True, "coverage": "materialized_human_unknown_cases_visible_clients"}}


def _memory_queue(now):
    from management.models import IgClient
    active = Q(memory_claim_token__gt="", memory_claim_until__gt=now)
    malformed = (Q(memory_claim_token__gt="", memory_claim_until__isnull=True)
                 | Q(memory_claim_token="", memory_claim_until__gt=now))
    due = Q(memory_due_at__lte=now)
    row = IgClient.objects.filter(memory_dirty_at__isnull=False,
        hidden_at__isnull=True, privacy_erasure_started_at__isnull=True).aggregate(
        total=Count("pk"), processing=Count("pk", filter=active),
        due_unclaimed=Count("pk", filter=due & ~active & ~malformed),
        deferred=Count("pk", filter=Q(memory_due_at__gt=now) & ~active & ~malformed),
        unknown=Count("pk", filter=malformed | (Q(memory_due_at__isnull=True) & ~active)),
        oldest_dirty=Min("memory_dirty_at"))
    enabled = bool(getattr(settings, "IG_MEMORY_GENERATION_ENABLED", False))
    accepted = bool(getattr(settings, "IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED", False))
    return {"available": True, "state": "observed" if enabled and accepted else "disabled",
            "generation_enabled": enabled, "provider_admission_accepted": accepted,
            "counts": {key: row[key] for key in ("total", "processing", "due_unclaimed", "deferred", "unknown")},
            "oldest_dirty_at": _iso(row["oldest_dirty"]), "exact": True,
            "coverage": "dirty_visible_clients", "authority": "queue_observation_not_claim_admission"}


def _collect_components(now):
    from management.services.ig_daemon_health import daemon_runtime_health_snapshot
    from management.services.ig_lane_health import operational_lane_snapshot
    from management.services.ig_task_health import task_health_snapshot
    from management.services.gemini_v2_read_model import build_quotas_payload, build_routes_payload
    loaders = {
        "attention": lambda: _reply_attention(now),
        "memory_generation": lambda: _memory_queue(now),
        "tasks": lambda: task_health_snapshot(now=now),
        "quotas": lambda: build_quotas_payload(now=now),
        "routes": lambda: build_routes_payload(now=now),
        "daemon": lambda: daemon_runtime_health_snapshot(now_epoch=now.timestamp(), include_technical_debt=False),
        "lanes": lambda: operational_lane_snapshot(now=now, observation_limit=OBSERVATION_LIMIT,
            include_progress=False, daemon_snapshot=components.get("daemon", {})),
    }
    budget = _ReadBudget()
    components = {}
    with ExitStack() as stack:
        for connection in connections.all():
            stack.enter_context(connection.execute_wrapper(budget))
        for name, loader in loaders.items():
            budget.component = name
            try:
                components[name] = loader()
            except Exception:
                components[name] = _unavailable()
            if name in budget.rejected:
                components[name] = _unavailable(budget.rejected[name])
    return components, {"executed": budget.total, "limit": QUERY_LIMIT,
        "by_component": budget.counts, "bounded": True,
        "rejected_components": sorted(budget.rejected)}


def _envelope(raw, captured_at, *, available=None, coverage="owner_observation"):
    raw = _mapping(raw)
    if available is None:
        available = raw.get("available") is True
    return {"available": available, "observed_at": _iso(captured_at),
        "observation_age_seconds": 0, "freshness": "fresh", "coverage": coverage,
        "reason": "" if available else _choice(raw.get("reason"), FAILURES, "observation_unavailable"),
        "data": {}}


def _metric(raw, *, tpm=False):
    raw = _mapping(raw)
    known = raw.get("complete") is True
    calibrated = raw.get("calibration") == "calibrated"
    headroom = known and (not tpm or (raw.get("headroom_known") is True and calibrated))
    result = {key: _count(raw.get(key)) for key in ("used", "limit", "reserved", "uncertain")}
    result.update(remaining=_count(raw.get("remaining")) if headroom else None,
                  complete=known, headroom_known=headroom)
    if tpm:
        result.update(observed_usage_known=raw.get("observed_usage_known") is True,
            calibration=_choice(raw.get("calibration"), {"calibrated", "uncalibrated"}),
            usage_source=_choice(raw.get("usage_source"), {"provider_reported", "estimated", "mixed", "no_observed_usage"}))
    return result


def compose_overview_payload(*, components, captured_at, now=None):
    """Pure projection of captured owner mappings; no ORM/cache/clock lookup."""
    captured_at = _date(captured_at)
    now = _date(now) if now is not None else captured_at
    if captured_at is None or now is None:
        raise ValueError("aware_capture_required")
    components = _mapping(components)
    from management.services.ig_task_health import TASK_SPECS
    from management.services.ig_worker_progress import WORKER_LIMITS
    from management.services import gemini_routing, gemini_health
    models = set(gemini_health.DISPLAY_MODELS) | set(gemini_routing.ORDINARY_CHAIN) | set(gemini_routing.COMPLEX_CHAIN) | set(gemini_routing.ANALYSIS_CHAIN)
    output = {}
    for name in ("daemon", "lanes", "tasks", "attention", "memory_generation", "quotas", "routes"):
        raw = _mapping(components.get(name))
        available = raw.get("available") is not False and bool(raw) if name in {"daemon", "quotas", "routes"} else raw.get("available") is True
        component = _envelope(raw, captured_at, available=available)
        data = component["data"]
        if available and name == "daemon":
            data.update(process_online=raw.get("process_online") is True,
                healthy=raw.get("process_online") is True and raw.get("main_healthy") is True,
                main_healthy=raw.get("main_healthy") is True,
                process_age_seconds=_number(raw.get("process_age_seconds")), main_age_seconds=_number(raw.get("main_age_seconds")),
                alive_window_seconds=_number(raw.get("alive_window_seconds")),
                state=_choice(raw.get("main_state"), STATES),
                stalled_reason=_choice(raw.get("stalled_reason"), FAILURES, ""),
                workers={key: {"state": _choice(_mapping(row).get("state"), STATES),
                    "healthy": _mapping(row).get("healthy") is True,
                    "progress_age_seconds": _number(_mapping(row).get("progress_age_seconds")),
                    "completed_age_seconds": _number(_mapping(row).get("completed_age_seconds")),
                    "observation_limit_seconds": WORKER_LIMITS[key]}
                    for key in WORKER_LIMITS
                    if (row := _mapping(raw.get("worker_lanes")).get(key)) is not None})
            data["technical_debt"] = {"coverage_complete": False, "state": "not_requested"}
        elif available and name == "lanes":
            component["coverage"] = "canonical_bounded_samples"
            data.update(bot_state=_choice(raw.get("bot_state"), STATES), lanes={})
            for key in LANES:
                if key not in _mapping(raw.get("lanes")):
                    continue
                row = _mapping(_mapping(raw.get("lanes")).get(key))
                data["lanes"][key] = {"available": row.get("available") is True,
                    "healthy": row.get("healthy") is True, "state": _choice(row.get("state"), STATES),
                    "counts": {bucket: _count(_mapping(row.get("counts")).get(bucket)) for bucket in BUCKETS},
                    "count_authority": "sample", "sample_size": _count(row.get("sample_size")),
                    "sample_limit": _count(row.get("sample_limit")), "has_more": row.get("has_more") is True,
                    "risk_coverage_complete": row.get("risk_coverage_complete") is True,
                    "attention_total": _count(row.get("attention_total")),
                    "attention_total_exact": row.get("attention_total_exact", "attention_total" in row) is True and _count(row.get("attention_total")) is not None,
                    "risk_scan_complete": row.get("risk_scan_complete", True) is True,
                    "risk_sample_size": _count(row.get("risk_sample_size")),
                    "risk_sample_limit": _count(row.get("risk_sample_limit")),
                    "risk_has_more": row.get("risk_has_more") is True,
                    "oldest_runnable_age_seconds": _number(row.get("oldest_runnable_age_seconds")),
                    "stall_after_seconds": _number(row.get("stall_after_seconds")),
                    "progress_age_seconds": _number(row.get("progress_age_seconds")),
                    "progress_evidence": _choice(row.get("progress_evidence"), {"disabled", "task_heartbeat", "terminal_progress", "durable_completion"}, "unavailable")}
        elif available and name == "tasks":
            data["tasks"] = [{"key": row["key"], "state": _choice(row.get("state"), STATES),
                "healthy": row.get("healthy") is True, "age_seconds": _number(row.get("age_seconds")),
                "stale_after_seconds": _number(row.get("stale_after_seconds")),
                "last_succeeded_at": _iso(row.get("last_succeeded_at")),
                "consecutive_failures": _count(row.get("consecutive_failures"))}
                for row in raw.get("tasks", [])[:len(TASK_SPECS)]
                if isinstance(row, dict) and row.get("key") in {spec.key for spec in TASK_SPECS}]
        elif available and name == "attention":
            component["coverage"] = "materialized_reply_debt_and_human_unknown_cases_visible_clients"
            human = _mapping(raw.get("human_unknown_cases"))
            required = raw.get("required") is True or bool(_count(human.get("count")))
            data.update(required=required, reply_debt_required=raw.get("required") is True, count=_count(raw.get("count")),
                owner="manager" if required else "none", since=_iso(raw.get("since")),
                waiting_age_seconds=_age(raw.get("since"), captured_at), exact=raw.get("exact") is True,
                next_action="review_reply_or_receipt" if required else "none",
                human_unknown_cases={"count": _count(human.get("count")),
                    "oldest_created_at": _iso(human.get("oldest_created_at")),
                    "case_age_seconds": _age(human.get("oldest_created_at"), captured_at),
                    "exact": human.get("exact") is True,
                    "coverage": "materialized_human_unknown_cases_visible_clients"},
                coverage_exclusions=["unmaterialized_cases", "payment_reviews", "post_sale_cases", "human_unknown_commands_without_unresolved_case"])
        elif available and name == "memory_generation":
            component["coverage"] = "dirty_visible_clients"
            enabled = raw.get("generation_enabled") is True and raw.get("provider_admission_accepted") is True
            data.update(state="observed" if enabled else "disabled", healthy=None,
                generation_enabled=raw.get("generation_enabled") is True,
                provider_admission_accepted=raw.get("provider_admission_accepted") is True,
                counts={key: _count(_mapping(raw.get("counts")).get(key)) for key in ("total", "processing", "due_unclaimed", "deferred", "unknown")},
                runnable=None, due_unclaimed_is_admitted=False,
                oldest_dirty_at=_iso(raw.get("oldest_dirty_at")), oldest_dirty_age_seconds=_age(raw.get("oldest_dirty_at"), captured_at),
                exact=raw.get("exact") is True, authority="queue_observation_not_claim_admission")
        elif available and name == "quotas":
            component["coverage"] = "local_per_project_model_windows"
            data.update(authority="local_advisory_not_dispatch_permission", guaranteed_headroom=False,
                capacity_is_additive=False, slot_rows_are_not_independent_pools=True,
                traffic_truncated=_mapping(raw.get("accounting")).get("traffic_truncated") is True,
                nonlive_admission_mode=_choice(_mapping(raw.get("accounting")).get("nonlive_admission_mode"), {"enforce", "shadow", "invalid"}),
                nonlive_enforcement_active=_mapping(raw.get("accounting")).get("nonlive_enforcement_active") is True,
                projects=[])
            for model_row in raw.get("models", [])[:len(gemini_health.DISPLAY_MODELS)]:
                model_row = _mapping(model_row)
                if model_row.get("model") not in models:
                    continue
                for pair in model_row.get("projects", [])[:len(gemini_health.SLOT_IDS)]:
                    pair = _mapping(pair)
                    if pair.get("slot_id") not in gemini_health.SLOT_IDS:
                        continue
                    binding = _mapping(pair.get("nonlive_profile"))
                    evidence = _mapping(pair.get("last_real_evidence"))
                    data["projects"].append({"model": model_row["model"], "slot_id": pair["slot_id"],
                        "configured": pair.get("configured") is True,
                        "metadata_checked": None,
                        "metadata_coverage": "not_requested",
                        "status": _choice(pair.get("status"), {"not_configured", "accounting_unknown", "rpd_exhausted_until_reset", "rpm_limited", "tpm_limited", "available_assumed", "in_flight", "auth_failed", "model_unavailable_for_project", "provider_degraded", "confirmed_recent_success"}),
                        "identity_mapping": _choice(pair.get("identity_mapping"), {"explicit", "missing", "duplicate", "unconfigured", "invalid"}),
                        "rpm": _metric(pair.get("rpm")), "rpd": _metric(pair.get("rpd")),
                        "input_tpm": _metric(pair.get("input_tpm"), tpm=True),
                        "nonlive_profile": {"calibration": _choice(binding.get("calibration"), {"calibrated", "uncalibrated"}),
                            "runtime_profile_binding": _choice(binding.get("runtime_profile_binding"), {"matched", "different"}),
                            "eligible_prerequisites": binding.get("eligible_prerequisites") is True,
                            "authority": "prerequisites_only_not_dispatch_permission"},
                        "last_success_at": _iso(pair.get("last_success_at")),
                        "last_success_age_seconds": _age(pair.get("last_success_at"), captured_at),
                        "last_failure_at": _iso(pair.get("last_failure_at")),
                        "last_failure_age_seconds": _age(pair.get("last_failure_at"), captured_at),
                        "last_generation_at": _iso(evidence.get("at")),
                        "last_generation_age_seconds": _age(evidence.get("at"), captured_at),
                        "last_generation_success": evidence.get("success") is True if _iso(evidence.get("at")) else None,
                        "generation_evidence_present": bool(_iso(evidence.get("at")) or _iso(pair.get("last_success_at")))})
        elif available and name == "routes":
            data["routes"] = [{"task_class": row["task_class"],
                "lane": _choice(row.get("lane"), {"live", "analysis", "none"}),
                "effective_chain": [model for model in row.get("effective_chain", [])[:8] if model in models],
                "base_chain": [model for model in row.get("base_chain", [])[:8] if model in models],
                "deadline_ms": _count(row.get("deadline_ms")), "authority": "current_policy_not_generation_evidence"}
                for row in raw.get("routes", [])[:4] if isinstance(row, dict)
                and row.get("task_class") in {item.value for item in gemini_routing.TaskClass}]
        output[name] = component
    if output["quotas"]["available"]:
        routes = output["routes"]["data"].get("routes", [])
        for pair in output["quotas"]["data"]["projects"]:
            pair["candidate_task_classes"] = [route["task_class"] for route in routes if pair["model"] in route["effective_chain"]] if output["routes"]["available"] else None
        output["quotas"]["data"]["demand"] = {
            "authority": "observed_queue_not_project_assignment",
            "lane_samples": {key: {"counts": row["counts"], "has_more": row["has_more"],
                "count_authority": "sample"} for key, row in output["lanes"]["data"].get("lanes", {}).items()
                if key in {"customer_revisions", "legacy_inbound", "conversation_analysis", "binotel_analysis", "reply_recovery"}},
            "memory_due_unclaimed": output["memory_generation"]["data"].get("counts", {}).get("due_unclaimed"),
            "memory_generation_state": output["memory_generation"]["data"].get("state", "unavailable"),
            "lane_coverage_available": output["lanes"]["available"],
            "memory_coverage_available": output["memory_generation"]["available"],
            "route_coverage_available": output["routes"]["available"],
        }
    payload = {"schema_version": SCHEMA_VERSION, "captured_at": _iso(captured_at),
               "generated_at": _iso(captured_at), "components": output,
               "cache": {"hit": False, "ttl_seconds": CACHE_SECONDS}}
    return _refresh_payload(payload, now)


def _refresh_payload(payload, now):
    result = deepcopy(payload)
    previous = _date(result.get("generated_at"))
    elapsed = max(0, int((now - previous).total_seconds())) if previous else 0

    def advance(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (key.endswith("_age_seconds") or key == "age_seconds") and _number(item) is not None:
                    value[key] = item + elapsed
                else:
                    advance(item)
        elif isinstance(value, list):
            for item in value:
                advance(item)
    advance(result)
    result["generated_at"] = _iso(now)
    for name, component in result["components"].items():
        age = _age(component.get("observed_at"), now)
        component.update(observation_age_seconds=age, freshness="fresh" if age is not None and age <= FRESH_SECONDS else "stale")
        data = component["data"]
        if name == "daemon" and component["available"]:
            limit = data.get("alive_window_seconds")
            fresh = lambda age: age is not None and limit is not None and age < limit
            data["process_online"] = data["process_online"] and fresh(data.get("process_age_seconds"))
            data["main_healthy"] = data["main_healthy"] and fresh(data.get("main_age_seconds"))
            data["healthy"] = data["process_online"] and data["main_healthy"] and component["freshness"] == "fresh"
            for worker in data["workers"].values():
                age = worker["progress_age_seconds"]
                if age is not None and age >= worker["observation_limit_seconds"]:
                    worker.update(healthy=False, state="stalled")
        elif name == "tasks":
            for task in data.get("tasks", []):
                if task["healthy"] and task["age_seconds"] is not None and task["stale_after_seconds"] is not None and task["age_seconds"] > task["stale_after_seconds"]:
                    task.update(healthy=False, state="stale")
        elif name == "lanes":
            for lane in data.get("lanes", {}).values():
                age, threshold = lane["oldest_runnable_age_seconds"], lane["stall_after_seconds"]
                if age is not None and threshold is not None and age > threshold:
                    lane.update(healthy=False, state="stalled")
    return result


def build_overview_payload(*, now=None, use_cache=True):
    now = _date(now or timezone.now())
    if now is None:
        raise ValueError("aware_capture_required")
    try:
        backend = caches["default"]
    except Exception:
        backend = None
    # A DB cache would make this GET write SQL. Other cache backends carry only
    # this sanitized, actor-independent DTO; cache failure never proves health.
    cache_available = backend is not None and not isinstance(backend, DatabaseCache)
    if use_cache and cache_available:
        try:
            cached = backend.get(CACHE_KEY)
            if isinstance(cached, dict) and cached.get("schema_version") == SCHEMA_VERSION:
                age = _age(cached.get("captured_at"), now)
                if age is not None and age < CACHE_SECONDS:
                    payload = _refresh_payload(cached, now)
                    payload["cache"].update(hit=True, available=True)
                    if "queries" in payload:
                        payload["queries"].update(executed=0, by_component=dict.fromkeys(QUERY_BUDGETS, 0))
                    return payload
        except Exception:
            cache_available = False
    components, queries = _collect_components(now)
    payload = compose_overview_payload(components=components, captured_at=now)
    payload["queries"] = queries
    payload["queries"]["collection_executed"] = queries["executed"]
    payload["cache"]["available"] = cache_available
    if use_cache and cache_available:
        try:
            backend.set(CACHE_KEY, payload, CACHE_SECONDS)
        except Exception:
            payload["cache"]["available"] = False
    return payload
