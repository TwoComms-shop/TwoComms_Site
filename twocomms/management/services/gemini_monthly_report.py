"""Read-only, sanitized monthly Gemini attempt observations.

This module intentionally derives a bounded reporting window from the durable
attempt ledger.  It does not assert a retention policy and does not mutate the
ledger, quota state, cache, or provider state.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from django.db.models import Case, CharField, Count, IntegerField, Max, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from management.models import GeminiRequestAttempt
from management.services import gemini_health


SCHEMA_VERSION = 1
DEFAULT_DAYS = 30
MIN_DAYS = 1
MAX_DAYS = 31
UTC = dt.timezone.utc

MODEL_ALLOWLIST = frozenset(gemini_health.DISPLAY_MODELS)
LANE_ALLOWLIST = frozenset({
    "analysis", "call", "checker", "diagnostic", "followup", "holding",
    "live", "metadata_probe", "recovery",
})
FAILURE_ALLOWLIST = frozenset({
    "blocked", "empty", "forbidden", "http_408", "http_5xx",
    "invalid_key", "invalid_payload", "invalid_response", "lease_busy",
    "malformed_response", "model_not_found", "model_overload",
    "model_unavailable", "overload", "permission_denied",
    "provider_error", "provider_overload", "quarantined", "quota_429",
    "read_timeout", "request_error", "stale_provider_boundary", "transport",
})

_SUCCESS_STATES = ("succeeded", "succeeded_late")
_FAILED_STATES = ("failed", "timeout_ambiguous")
_TERMINAL_STATES = (*_SUCCESS_STATES, *_FAILED_STATES, "cancelled_pre_dispatch")


def _as_utc(value: dt.datetime) -> dt.datetime:
    if timezone.is_naive(value):
        value = timezone.make_aware(value, UTC)
    return value.astimezone(UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value else None


def _validated_days(days: int | str | None) -> int:
    if days is None:
        parsed = DEFAULT_DAYS
    elif isinstance(days, bool) or isinstance(days, float):
        raise ValueError("days must be an integer from 1 through 31")
    elif isinstance(days, int):
        parsed = days
    elif isinstance(days, str) and days.isdecimal():
        parsed = int(days)
    else:
        raise ValueError("days must be an integer from 1 through 31")
    if not MIN_DAYS <= parsed <= MAX_DAYS:
        raise ValueError("days must be an integer from 1 through 31")
    return parsed


def _bucket(field: str, allowed: frozenset[str]):
    return Case(
        When(**{f"{field}__in": tuple(sorted(allowed))}, then=field),
        default=Value("other"),
        output_field=CharField(),
    )


def _http_bucket():
    return Case(
        When(http_code__gte=400, http_code__lte=499, then=Value("4xx")),
        When(http_code__gte=500, http_code__lte=599, then=Value("5xx")),
        default=Value("other"),
        output_field=CharField(),
    )


def _daily_rows(query):
    started = Q(provider_started_at__isnull=False)
    # A started provider call can still be executing, or historical telemetry
    # can lack a terminal timestamp.  Including its default zero duration would
    # falsely improve latency.  The report therefore samples only completed
    # attempts with both a terminal FSM record and explicit finished_at proof.
    latency_measured = started & Q(
        finished_at__isnull=False, fsm_state__in=_TERMINAL_STATES
    )
    return list(
        query.annotate(
            day=TruncDate("created_at", tzinfo=UTC),
            bucket_model=_bucket("model", MODEL_ALLOWLIST),
            bucket_lane=_bucket("lane", LANE_ALLOWLIST),
        ).values("day", "bucket_model", "bucket_lane").annotate(
            attempts=Count("id"),
            started=Count("id", filter=started),
            skipped=Count("id", filter=~Q(not_attempted_reason="")),
            succeeded=Count("id", filter=Q(fsm_state__in=_SUCCESS_STATES)),
            failed=Count("id", filter=Q(fsm_state__in=_FAILED_STATES)),
            latency_samples=Count("id", filter=latency_measured),
            latency_unknown_count=Count("id", filter=started & ~latency_measured),
            latency_ms_total=Coalesce(
                Sum("latency_ms", filter=latency_measured), Value(0), output_field=IntegerField()
            ),
            latency_ms_min=Min("latency_ms", filter=latency_measured),
            latency_ms_max=Max("latency_ms", filter=latency_measured),
            latency_le_1000ms=Count(
                "id", filter=latency_measured & Q(latency_ms__lte=1000)
            ),
            latency_1001_to_5000ms=Count(
                "id", filter=latency_measured & Q(latency_ms__gte=1001, latency_ms__lte=5000)
            ),
            latency_gt_5000ms=Count("id", filter=latency_measured & Q(latency_ms__gte=5001)),
        ).order_by("day", "bucket_model", "bucket_lane")
    )


def _failure_rows(query):
    return list(
        query.filter(fsm_state__in=_FAILED_STATES).annotate(
            day=TruncDate("created_at", tzinfo=UTC),
            bucket_model=_bucket("model", MODEL_ALLOWLIST),
            bucket_lane=_bucket("lane", LANE_ALLOWLIST),
            bucket_failure_kind=_bucket("failure_kind", FAILURE_ALLOWLIST),
            bucket_http=_http_bucket(),
        ).values("day", "bucket_model", "bucket_lane", "bucket_failure_kind", "bucket_http").annotate(
            attempts=Count("id"),
        ).order_by("day", "bucket_model", "bucket_lane", "bucket_failure_kind", "bucket_http")
    )


def build_monthly_payload(*, days: int | str | None = DEFAULT_DAYS, now=None) -> dict[str, Any]:
    """Build a strictly redacted aggregate for UTC calendar days through now.

    The half-open window starts at 00:00 UTC on the first included calendar
    day and ends at ``now``.  No attempt rows, identifiers, messages, keys, or
    provider details are materialized by this read model.
    """
    window_days = _validated_days(days)
    generated_at = _as_utc(now or timezone.now())
    start_day = generated_at.date() - dt.timedelta(days=window_days - 1)
    window_start = dt.datetime.combine(start_day, dt.time.min, tzinfo=UTC)
    query = GeminiRequestAttempt.objects.filter(
        created_at__gte=window_start,
        created_at__lt=generated_at,
    )
    coverage = query.aggregate(
        attempts=Count("id"),
        first_observed_at=Min("created_at"),
        last_observed_at=Max("created_at"),
    )
    daily = _daily_rows(query)
    failures = _failure_rows(query)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(generated_at),
        "window": {
            "timezone": "UTC",
            "days": window_days,
            "start": _iso(window_start),
            "end_exclusive": _iso(generated_at),
            "includes_current_utc_day": True,
        },
        "coverage": {
            "attempts": int(coverage["attempts"] or 0),
            "first_observed_at": _iso(coverage["first_observed_at"]),
            "last_observed_at": _iso(coverage["last_observed_at"]),
        },
        "retention": {
            "report_window_days": window_days,
            "technical_ledger_retention": "unbounded_currently",
            "automated_purge": "not_configured",
        },
        "daily": [
            {
                **{key: value for key, value in row.items() if not key.startswith("bucket_")},
                "model": row["bucket_model"],
                "lane": row["bucket_lane"],
                "day": row["day"].isoformat(),
                "latency_ms_min": row["latency_ms_min"],
                "latency_ms_max": row["latency_ms_max"],
                "latency_ms_average": (
                    round(row["latency_ms_total"] / row["latency_samples"], 2)
                    if row["latency_samples"] else None
                ),
            }
            for row in daily
        ],
        "failures": [
            {
                "day": row["day"].isoformat(),
                "model": row["bucket_model"],
                "lane": row["bucket_lane"],
                "failure_kind": row["bucket_failure_kind"],
                "http_bucket": row["bucket_http"],
                "attempts": row["attempts"],
            }
            for row in failures
        ],
    }
