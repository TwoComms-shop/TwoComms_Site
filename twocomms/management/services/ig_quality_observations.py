"""Small, explicit revision cohorts over the same protected trace read model.

This is execution evidence, not a subjective response-quality score, a census,
conversion attribution, an economic-root counter or a historical as-of replay.
The CSV deliberately contains aggregate observations only.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db.models import BigIntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from management.models import IgCustomerTurnRevision, IgFunnelResetAudit
from management.services.ig_decision_trace_read_model import READ_QUERY_CAP, read_revision_decision_trace

SCHEMA_VERSION = "ig-quality-observations.v1"
MAX_DAYS = 31
MAX_PAGE = 5
QUERY_CAP = MAX_PAGE * READ_QUERY_CAP + 4
STAGES = ("context", "decision", "generation", "proposal", "delivery", "semantic", "manager_case")
_TERMINAL = frozenset({"succeeded", "succeeded_late", "failed", "timeout_ambiguous", "cancelled_pre_dispatch"})
METRICS = {
    "no_reply_decision": ("Зафіксовано рішення не відповідати", "sampled_revisions"),
    "static_reply_decision": ("Зафіксовано статичну відповідь", "sampled_revisions"),
    "generation_winner": ("Прийнята генерація", "generation_applicable_revisions"),
    "normal_reply_sent": ("Усі звичайні частини доставлено", "reply_applicable_revisions"),
    "semantic_reply_complete": ("Повну змістовну відповідь підтверджено", "reply_applicable_revisions"),
    "provider_phase_started": ("Зафіксовано початок звернення до моделі", "recorded_attempts"),
    "local_semantic_rejection": ("Локальне відхилення змісту", "recorded_terminal_attempts"),
    "receipt_confirmed": ("Підтверджена фізична частина", "recorded_physical_parts"),
    "transcript_parity": ("Підтверджену доставку відображено в історії", "confirmed_receipt_parts"),
    "manager_notification_sent": ("Доставлено сповіщення команді", "bound_manager_cases"),
    "total_tokens_observed": ("Відоме використання токенів", "recorded_attempts"),
    "latency_observed": ("Відома тривалість завершеної спроби", "recorded_terminal_attempts"),
}


def _utc(value):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError("window_invalid")
    return value.astimezone(dt_timezone.utc)


def _iso(value):
    return value.isoformat()


def _metric(key):
    label, unit = METRICS[key]
    return {"key": key, "label": label, "denominator_unit": unit,
            "denominator": 0, "numerator": 0, "negative_count": 0,
            "unknown_count": 0, "not_applicable_count": 0, "percent": None}


def _observe(metric, value, *, applicable=True):
    if not applicable:
        metric["not_applicable_count"] += 1
        return
    metric["denominator"] += 1
    metric["numerator" if value is True else "negative_count" if value is False else "unknown_count"] += 1


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        return _utc(parsed) if parsed else None
    except (ValueError, TypeError):
        return None


def _classify(trace, metrics, coverage, counts, usage, latency):
    available = trace.get("status") == "available"
    if not available:
        counts["unavailable_trace_revisions"] += 1
        for stage in STAGES:
            coverage[stage]["unknown"] += 1
        for key in ("no_reply_decision", "static_reply_decision", "generation_winner", "normal_reply_sent", "semantic_reply_complete"):
            _observe(metrics[key], None)
        counts["attempt_population_unknown_revisions"] += 1
        counts["physical_part_population_unknown_revisions"] += 1
        counts["manager_case_population_unknown_revisions"] += 1
        return
    for stage in STAGES:
        value = (trace.get("coverage") or {}).get(stage, "unknown")
        coverage[stage]["known" if value in {"confirmed", "recorded"} else "unknown"] += 1
    decision, generation, delivery, semantic = (trace.get(key) or {} for key in ("decision", "generation", "delivery", "semantic"))
    input_known = decision.get("proof") == "confirmed"
    no_reply = input_known and decision.get("origin") == "no_reply"
    static = input_known and decision.get("origin") == "static_reply"
    _observe(metrics["no_reply_decision"], decision.get("origin") == "no_reply" if input_known else None)
    _observe(metrics["static_reply_decision"], decision.get("origin") == "static_reply" if input_known else None)
    requests = generation.get("requests") or []
    _observe(metrics["generation_winner"], True if generation.get("actual_model") else
             False if requests and all(row.get("terminal_resolution") == "failed" for row in requests) else None,
             applicable=not ((no_reply or static) and not requests))
    effects = delivery.get("effects") or []
    conclusive = bool(delivery.get("proof") == "confirmed" and effects and all(
        effect.get("receipt_proof") == "confirmed" or effect.get("state") in {"definite_failed", "cancelled", "superseded"}
        for effect in effects))
    _observe(metrics["normal_reply_sent"], True if delivery.get("normal_reply_complete") is True else
             False if conclusive else None, applicable=not no_reply)
    semantic_known = semantic.get("proof") == "confirmed"
    _observe(metrics["semantic_reply_complete"], True if semantic_known and semantic.get("customer_reply_complete") is True else
             False if semantic_known and (conclusive or (semantic.get("remaining_count") or 0) > 0) else None, applicable=not no_reply)
    counts["historical_scope_revisions"] += int((trace.get("revision") or {}).get("scope") == "historical")
    attempts = [attempt for request in requests for attempt in request.get("attempts", ())]
    if not requests and not (no_reply or static):
        counts["attempt_population_unknown_revisions"] += 1
    for attempt in attempts:
        counts["recorded_attempts"] += 1
        started = _timestamp(attempt.get("recorded_provider_started_at"))
        _observe(metrics["provider_phase_started"], True if started else False if attempt.get("not_attempted_reason") else None)
        finished = _timestamp(attempt.get("finished_at"))
        terminal = attempt.get("state") in _TERMINAL and finished is not None
        if terminal:
            counts["recorded_terminal_attempts"] += 1
        kind = attempt.get("failure_kind")
        local = True if kind == "local_semantic_rejection" else None if kind == "unknown" or (
            kind is None and attempt.get("state") in {"failed", "timeout_ambiguous"}) else False
        _observe(metrics["local_semantic_rejection"], local, applicable=terminal)
        total = ((attempt.get("usage") or {}).get("tokens") or {}).get("total")
        total_known = type(total) is int and total >= 0
        _observe(metrics["total_tokens_observed"], True if total_known else None)
        if total_known:
            usage.append(total)
        duration = attempt.get("latency_ms")
        duration_known = terminal and type(duration) is int and duration >= 0
        _observe(metrics["latency_observed"], True if duration_known else None, applicable=terminal)
        if duration_known:
            latency.append(duration)
    if not effects and not no_reply:
        counts["physical_part_population_unknown_revisions"] += 1
    for effect in effects:
        counts["recorded_physical_parts"] += 1
        exact = effect.get("receipt_proof") == "confirmed" and _timestamp(effect.get("receipt_finalized_at")) is not None
        _observe(metrics["receipt_confirmed"], True if exact else
                 False if effect.get("state") in {"definite_failed", "cancelled", "superseded"} else None)
        if exact:
            counts["confirmed_receipt_parts"] += 1
        _observe(metrics["transcript_parity"], True if effect.get("transcript_proof") == "confirmed" else None, applicable=exact)
    manager = (trace.get("actions") or {}).get("manager_case") or {}
    bound = manager.get("proof") == "confirmed"
    if bound:
        counts["bound_manager_cases"] += 1
    elif manager.get("reason") != "manager_action_not_recorded":
        counts["manager_case_population_unknown_revisions"] += 1
    _observe(metrics["manager_notification_sent"], True if manager.get("notification_delivered") is True else
             False if manager.get("notification_state") == "pending" else None, applicable=bound)


def build_quality_observation_page(*, since, until, before_revision_id=None, limit=MAX_PAGE):
    """Current evidence of at most five revisions in a half-open creation cohort."""
    started = timezone.now()
    since, until = _utc(since), _utc(until)
    if not since < until <= _utc(started) or until - since > timedelta(days=MAX_DAYS):
        raise ValueError("window_invalid")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("page_limit_invalid")
    if before_revision_id is not None and (type(before_revision_id) is not int or not 0 < before_revision_id <= 2**63 - 1):
        raise ValueError("cursor_invalid")
    cohort = IgCustomerTurnRevision.objects.filter(created_at__gte=since, created_at__lt=until,
        client__privacy_erasure_started_at__isnull=True, client__hidden_at__isnull=True,
        erasure_started_at_snapshot__isnull=True, client__pk__isnull=False)
    population = cohort.count()
    page = cohort.filter(pk__lt=before_revision_id) if before_revision_id is not None else cohort
    rows = list(page.order_by("-pk").values("pk", "client_id", "snapshot_digest")[:limit + 1])
    selected = rows[:limit]
    metrics = {key: _metric(key) for key in METRICS}
    coverage = {stage: {"denominator": len(selected), "known": 0, "unknown": 0} for stage in STAGES}
    counts = {key: 0 for key in (
        "unavailable_trace_revisions", "historical_scope_revisions", "recorded_attempts", "recorded_terminal_attempts",
        "recorded_physical_parts", "confirmed_receipt_parts", "bound_manager_cases",
        "attempt_population_unknown_revisions", "physical_part_population_unknown_revisions", "manager_case_population_unknown_revisions",
    )}
    traces, usage, latency = {}, [], []
    for row in selected:
        trace = read_revision_decision_trace(client_id=row["client_id"], revision_id=row["pk"])
        if trace.get("reason") in {"owner_unavailable", "privacy_erasure", "read_scope_changed"}:
            return {"schema_version": SCHEMA_VERSION, "status": "unavailable", "reason": "read_scope_changed"}
        traces[row["pk"]] = trace
        _classify(trace, metrics, coverage, counts, usage, latency)
    # The final bounded query includes client erasure/hidden checks, original
    # revision identity and each customer's latest explicit reset scope.
    latest_reset = IgFunnelResetAudit.objects.filter(client_id=OuterRef("client_id")).order_by("-pk").values("reset_after_message_id")[:1]
    fresh = list(cohort.filter(pk__in=[row["pk"] for row in selected]).annotate(
        reset_floor=Coalesce(Subquery(latest_reset), Value(0), output_field=BigIntegerField()) + Value(1),
    ).values("pk", "client_id", "snapshot_digest", "reset_floor")) if selected else []
    stable = {row["pk"]: row for row in fresh}
    if any(row["pk"] not in stable or stable[row["pk"]]["client_id"] != row["client_id"]
           or stable[row["pk"]]["snapshot_digest"] != row["snapshot_digest"]
           or (traces[row["pk"]].get("status") == "available"
               and stable[row["pk"]]["reset_floor"] != traces[row["pk"]]["revision"]["reset_floor"]) for row in selected):
        return {"schema_version": SCHEMA_VERSION, "status": "unavailable", "reason": "read_scope_changed"}
    finished = timezone.now()
    return {
        "schema_version": SCHEMA_VERSION, "status": "available",
        "cohort": {"start": _iso(since), "end_exclusive": _iso(until), "timezone": "UTC",
                   "time_field": "IgCustomerTurnRevision.created_at", "unit": "revision_records",
                   "eligible_population": "existing_visible_non_erasing_client_revisions",
                   "population_count_at_read_start": population},
        "observation": {"mode": "current_evidence_during_read_interval", "read_started_at": _iso(started),
                        "read_finished_at": _iso(finished), "historical_as_of_supported": False,
                        "outcomes_may_postdate_cohort_end": True},
        "sample": {"count": len(selected), "limit": limit, "maximum": MAX_PAGE,
                   "selection": "revision_id_descending_page_not_representative_sample",
                   "whole_window_sampled": before_revision_id is None and population == len(selected),
                   "has_more": len(rows) > limit,
                   "next_before_revision_id": selected[-1]["pk"] if selected and len(rows) > limit else None},
        "metrics": list(metrics.values()), "stage_coverage": coverage, "counts": counts,
        "usage": {"total_tokens_sum": sum(usage) if usage else None, "observed_attempts": len(usage),
                  "unknown_attempts": counts["recorded_attempts"] - len(usage), "monetary_cost": None},
        "latency": {"milliseconds_average": round(sum(latency) / len(latency), 2) if latency else None,
                    "observed_terminal_attempts": len(latency), "unknown_terminal_attempts": counts["recorded_terminal_attempts"] - len(latency)},
        "limits": {"provider_calls": 0, "page_query_cap": QUERY_CAP, "trace_query_cap": READ_QUERY_CAP},
        "retention": {"export_storage": "not_persisted_by_report", "report_window_is_not_retention_policy": True,
                      "technical_ledger_retention": "unbounded_currently", "automated_purge": "not_configured"},
        "interpretation": {"quality_score": None, "population_extrapolation": False, "conversion_attribution": False,
                           "logical_customer_turns": None, "economic_roots": None},
    }


def quality_observation_csv(payload):
    """Buffered aggregate CSV: no customer/revision/source/request/MID columns."""
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("status") != "available":
        raise ValueError("export_unavailable")
    cohort, observation, sample = (payload[key] for key in ("cohort", "observation", "sample"))
    def integer(value):
        if type(value) is not int or value < 0:
            raise ValueError("export_count_invalid")
        return value
    times = [_timestamp(value) for value in (cohort["start"], cohort["end_exclusive"], observation["read_started_at"], observation["read_finished_at"])]
    if any(value is None for value in times) or not times[0] < times[1] <= times[2] <= times[3]:
        raise ValueError("export_time_invalid")
    prefix = (_iso(times[0]), _iso(times[1]), "revision_records", "IgCustomerTurnRevision.created_at",
        _iso(times[2]), _iso(times[3]), "current_evidence_during_read_interval", False, True,
        integer(sample["count"]), integer(cohort["population_count_at_read_start"]), sample["whole_window_sampled"] is True)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("cohort_start_utc", "cohort_end_exclusive_utc", "cohort_unit", "cohort_time_field",
        "read_started_at", "read_finished_at", "observation_mode", "historical_as_of_supported", "outcomes_may_postdate_cohort_end",
        "sample_revisions", "population_revisions_at_read_start", "whole_window_sampled",
        "metric", "denominator_unit", "numerator", "denominator", "negative_count", "unknown_count", "not_applicable_count",
        "observed_total_tokens_sum", "token_usage_observed_attempts", "token_usage_unknown_attempts",
        "latency_ms_average", "latency_observed_terminal_attempts", "latency_unknown_terminal_attempts", "monetary_cost"))
    usage, latency = payload["usage"], payload["latency"]
    numeric_tail = (integer(usage["total_tokens_sum"]) if usage["total_tokens_sum"] is not None else "unknown",
        integer(usage["observed_attempts"]), integer(usage["unknown_attempts"]),
        latency["milliseconds_average"] if type(latency["milliseconds_average"]) in (int, float) and latency["milliseconds_average"] >= 0 else "unknown",
        integer(latency["observed_terminal_attempts"]), integer(latency["unknown_terminal_attempts"]), "unknown")
    for metric in payload["metrics"]:
        key = metric["key"]
        if key not in METRICS:
            raise ValueError("export_metric_invalid")
        values = tuple(integer(metric[name]) for name in ("numerator", "denominator", "negative_count", "unknown_count", "not_applicable_count"))
        if values[0] + values[2] + values[3] != values[1]:
            raise ValueError("export_denominator_invalid")
        writer.writerow((*prefix, key, METRICS[key][1], *values, *numeric_tail))
    for stage in STAGES:
        coverage = payload["stage_coverage"][stage]
        values = tuple(integer(coverage[name]) for name in ("known", "denominator", "unknown"))
        if values[0] + values[2] != values[1]:
            raise ValueError("export_denominator_invalid")
        writer.writerow((*prefix, "stage_known_" + stage, "sampled_revisions", values[0], values[1], 0, values[2], 0, *numeric_tail))
    return output.getvalue()
