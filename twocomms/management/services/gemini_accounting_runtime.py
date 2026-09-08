"""Fail-soft Gemini accounting V2 shadow runtime.

This module is deliberately observational.  ``off`` is the default and returns
null objects before any accounting database access.  ``shadow`` records what
the V2 admission policy would have decided.  The legacy gateway remains the
authority for provider availability and model/key selection, while an owned
immutable V2 graph forbids dispatching the same frozen candidate twice.

No prompt, customer text, credential, environment alias or provider body is
stored in ``GeminiRequest.candidate_plan``.  Provider attempts keep the legacy
alias field for the existing operational scoreboard, while quota coordination
is keyed only by the stable, non-secret project identity.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import DatabaseError, IntegrityError, OperationalError, transaction
from django.db.models import (
    Case,
    Count,
    F,
    IntegerField,
    Min,
    Q,
    Sum,
    When,
)
from django.utils import timezone


PT = ZoneInfo("America/Los_Angeles")
ACCOUNTING_POLICY_VERSION = "gemini-accounting-shadow-v1"
# Keep the original name as a compatibility fixture for the S3b shadow tests
# and historical request graphs.  Runtime ranking deliberately names the
# calibrated version separately so a deploy cannot reinterpret old profiles.
OWNER_PROFILE_VERSION = "owner-observed-2026-08-29.v1"
ACTIVE_QUOTA_PROFILE_VERSION = "production-observed-2026-08-31.v2"
ACTIVE_ESTIMATOR_VERSION = "json_bytes_div4_v1"
RANKING_EVIDENCE_FRESH_SECONDS = 24 * 60 * 60
ATTEMPT_PERMIT_SECONDS = 180
DB_RETRY_DELAYS = (0.0, 0.01, 0.03)
OWNERSHIP_RETRY_DELAYS = (0.0, 0.005, 0.02, 0.05, 0.1)
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SAFE_REASON = re.compile(r"^[a-z0-9_]{0,32}$")
_PLAN_FIELDS = frozenset({
    "candidate_index",
    "project_identity",
    "model",
    "identity_status",
    "initial_skip_reason",
})


@dataclass(frozen=True)
class _RevisionExecution:
    revision_id: int
    token: str
    settings_id: int
    settings_permission_epoch: int


_revision_execution = ContextVar("gemini_revision_execution", default=None)
_legacy_execution = ContextVar("gemini_legacy_execution", default=None)


class _LegacyProviderRootBlocked(Exception):
    def __init__(self, reason):
        self.reason = str(reason or "legacy_route_invalid")
        super().__init__(self.reason)


@contextmanager
def revision_request_execution(
    revision_id, revision_token, *, settings_id, settings_permission_epoch,
):
    """Supply an owned revision capability, never a caller-selected dedupe key.

    Admission rechecks this capability against durable truth. Observers retain
    it for provider workers whose ContextVar context is not inherited.
    """
    capability = _RevisionExecution(
        int(revision_id), str(revision_token), int(settings_id),
        int(settings_permission_epoch),
    )
    context_token = _revision_execution.set(capability)
    try:
        yield
    finally:
        _revision_execution.reset(context_token)


def _revision_execution_valid(capability, *, source_message_id, client_id, lane, logical_turn_id):
    from management.models import IgCustomerTurnRevision, InstagramBotSettings
    from management.services.ig_reply_boundary import capture_reply_permission
    from management.services.ig_revision_outbox import _normal_reply_window_deadline

    if (
        not isinstance(capability, _RevisionExecution)
        or not capability.token
        or lane != "live"
        or logical_turn_id != f"ig-revision:{capability.revision_id}"
        or not source_message_id or not client_id
    ):
        return False
    now = timezone.now()
    revision = IgCustomerTurnRevision.objects.filter(
        pk=capability.revision_id, client_id=client_id, active_slot=1,
        state=IgCustomerTurnRevision.State.CLAIMED,
        claim_token=capability.token, lease_until__gt=now,
        overall_deadline__gt=now,
    ).first()
    if revision is None or revision.erasure_started_at_snapshot is not None:
        return False
    snapshot = revision.bundle_snapshot
    if not isinstance(snapshot, dict) or not revision.snapshot_digest:
        return False
    digest = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if digest != revision.snapshot_digest:
        return False
    sources = snapshot.get("sources") or []
    # The canonical generation anchor is the final immutable bundle source.
    # Accepting any other source would permit two graphs for one revision.
    if not sources or sources[-1].get("message_id") != source_message_id:
        return False
    source = sources[-1]
    if not revision.sources.filter(
        # Refresh/recovery children preserve the parent's sealed snapshot,
        # including its historical revision_source_id. Their own source rows
        # have new PKs and the same immutable envelope identity.
        ordinal=source.get("ordinal"), message_id=source_message_id,
        message__client_id=client_id, message__role="user", role="user",
        source_digest=source.get("source_digest"),
        source_namespace=source.get("source_namespace"),
    ).exists():
        return False
    window_deadline = _normal_reply_window_deadline(revision)
    if window_deadline is None or window_deadline <= now:
        return False
    permission = capture_reply_permission(capability.settings_id, client_id)
    return bool(
        permission.allowed
        and permission.client_epoch == revision.permission_epoch
        and permission.settings_epoch == capability.settings_permission_epoch
        and InstagramBotSettings.objects.filter(
            pk=capability.settings_id, ai_enabled=True,
        ).exists()
    )


def validate_revision_request_creation(request):
    """Prevent arbitrary ORM writers from minting a fresh execution namespace."""
    if not request.source_execution_key:
        return
    from django.core.exceptions import ValidationError

    if str(request.source_execution_key).startswith("ig-recovery:"):
        from management.services.ig_legacy_provider_execution import (
            validate_legacy_execution,
        )

        if validate_legacy_execution(
            _legacy_execution.get(), source_message_id=request.source_message_id,
            client_id=request.client_id, lane=request.lane,
            logical_turn_id=request.logical_turn_id,
            source_execution_key=request.source_execution_key,
        ):
            return
        raise ValidationError({
            "source_execution_key": "Legacy recovery execution is not owned."
        })

    if (
        request.source_execution_key != request.logical_turn_id
        or not _revision_execution_valid(
            _revision_execution.get(), source_message_id=request.source_message_id,
            client_id=request.client_id, lane=request.lane,
            logical_turn_id=request.logical_turn_id,
        )
    ):
        raise ValidationError({"source_execution_key": "Revision execution is not owned."})


def parse_effective_from(value) -> dt.datetime | None:
    """Parse one aware ISO timestamp; invalid/naive values fail closed."""
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def is_pacific_midnight(value: dt.datetime | None) -> bool:
    if value is None:
        return False
    local = value.astimezone(PT)
    return local.time().replace(tzinfo=None) == dt.time.min


def configured_mode() -> str:
    return str(getattr(settings, "GEMINI_ACCOUNTING_V2_MODE", "off") or "off").strip().casefold()


def shadow_runtime_active(*, now=None) -> bool:
    """Return true only after an explicitly configured Pacific-midnight gate."""
    if configured_mode() != "shadow":
        return False
    effective = parse_effective_from(
        getattr(settings, "GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM", "")
    )
    if not is_pacific_midnight(effective):
        return False
    current = now or timezone.now()
    return current >= effective


def project_ranking_enabled() -> bool:
    """Return the explicit reversible ranking policy state."""
    return str(
        getattr(settings, "GEMINI_V2_PROJECT_RANKING_MODE", "enforce")
        or "enforce"
    ).strip().casefold() == "enforce"


def project_ranking_snapshot(*, project_identities, models, now=None) -> dict:
    """Read one provider-free V2 capacity snapshot for project ordering.

    The snapshot is advisory: it never creates a state row and never rejects a
    candidate.  Only explicit identities with the active immutable profile are
    returned. A missing state row is virtual zero local usage, matching the 4x6
    read model. RPD/RPM/in-flight/latency can rank every profiled model; input
    TPM participates only for models whose exact estimator version has
    production samples. An UNKNOWN state falls back to legacy order. Degraded
    real-traffic evidence remains visible with a ranking penalty; a timeout is
    not promoted to a hard quota block.

    At most three SELECTs are issued: calibrated profiles, pair states and one
    grouped rolling-60-second attempt aggregate.
    """
    if not project_ranking_enabled() or not shadow_runtime_active(now=now):
        return {}
    now = now or timezone.now()
    identities = tuple(dict.fromkeys(
        _safe_token(value, limit=80)
        for value in project_identities or ()
        if _safe_token(value, limit=80)
    ))
    model_names = tuple(dict.fromkeys(
        _safe_token(value, limit=80)
        for value in models or ()
        if _safe_token(value, limit=80)
    ))
    if not identities or not model_names:
        return {}

    try:
        from management.models import (
            GeminiQuotaProfile,
            GeminiQuotaState,
            GeminiRequestAttempt,
        )
        from management.services.gemini_quota import pacific_day

        profiles = {
            row.model: row
            for row in GeminiQuotaProfile.objects.filter(
                profile_version=ACTIVE_QUOTA_PROFILE_VERSION,
                model__in=model_names,
                effective_from__lte=now,
            ).filter(models_Q_effective(now))
        }
        if not profiles:
            return {}

        states = {
            (row.project_identity, row.model): row
            for row in GeminiQuotaState.objects.filter(
                project_identity__in=identities,
                model__in=tuple(profiles),
            )
        }
        rolling_cutoff = now - dt.timedelta(seconds=60)
        rolling_rows = GeminiRequestAttempt.objects.filter(
            project_identity__in=identities,
            model__in=tuple(profiles),
        ).filter(
            Q(
                provider_started_at__gte=rolling_cutoff,
                provider_started_at__lte=now,
            )
            | Q(
                fsm_state__in=(
                    GeminiRequestAttempt.FsmState.RESERVED,
                    GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                ),
                permit_expires_at__gt=now,
            )
        ).values("project_identity", "model").annotate(
            requests_60=Count(
                "id",
                filter=Q(
                    provider_started_at__gte=rolling_cutoff,
                    provider_started_at__lte=now,
                ),
            ),
            prompt_tokens_60=Sum(
                Case(
                    When(
                        provider_started_at__gte=rolling_cutoff,
                        provider_started_at__lte=now,
                        prompt_tokens__gt=0,
                        then=F("prompt_tokens"),
                    ),
                    When(
                        provider_started_at__gte=rolling_cutoff,
                        provider_started_at__lte=now,
                        then=F("reserved_prompt_tokens"),
                    ),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            active_permits=Count(
                "id",
                filter=Q(
                    fsm_state__in=(
                        GeminiRequestAttempt.FsmState.RESERVED,
                        GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                    ),
                    permit_expires_at__gt=now,
                ),
            ),
        )
        rolling = {
            (row["project_identity"], row["model"]): row
            for row in rolling_rows
        }
        today = pacific_day(now)
        snapshot = {}
        for identity in identities:
            for model, profile in profiles.items():
                pair = (identity, model)
                state = states.get(pair)
                # An explicit project/profile pair with no state row means no
                # V2 traffic has been observed locally. This is the same
                # virtual-zero left join used by the 4x6 read API and prevents
                # an untouched project from ranking behind a used project.
                if state is None:
                    state_status = "virtual_zero"
                else:
                    state_status = state.accounting_status
                # UNKNOWN has no real traffic evidence. DEGRADED still
                # contains a useful recent latency/outcome signal, but the
                # pure ordering helper ranks it behind healthy projects.
                if state_status not in {
                    "virtual_zero", "available", "blocked", "degraded",
                }:
                    continue
                minute = rolling.get(pair, {})
                requests_60 = max(0, int(minute.get("requests_60") or 0))
                prompt_tokens_60 = max(
                    0, int(minute.get("prompt_tokens_60") or 0)
                )
                tpm_rankable = (
                    profile.estimator_version == ACTIVE_ESTIMATOR_VERSION
                )
                dispatched = (
                    max(0, int(state.rpd_dispatched or 0))
                    if state is not None and state.pacific_day == today
                    else 0
                )
                provider_blocked = bool(
                    state is not None
                    and RequestObserver._active_provider_block(
                        state.provider_blocks,
                        now,
                    )
                )
                remaining_rpd = max(
                    0, int(profile.rpd_limit) - dispatched
                )
                remaining_rpm = max(
                    0, int(profile.rpm_limit) - requests_60
                )
                remaining_input_tpm = (
                    max(0, int(profile.input_tpm_limit) - prompt_tokens_60)
                    if tpm_rankable
                    else None
                )
                # Persisted in_flight_count is conservative crash evidence and
                # may outlive its permit. Ranking uses only unexpired attempt
                # permits so a crashed provider_started row cannot create a
                # permanent skip; the next admitted boundary atomically
                # reconciles the stale state counter.
                in_flight = max(0, int(minute.get("active_permits") or 0))
                evidence_candidates = (
                    [
                        value
                        for value in (
                            state.last_success_at,
                            state.last_failure_at,
                        )
                        if value is not None
                    ]
                    if state is not None
                    else []
                )
                last_evidence_at = max(evidence_candidates, default=None)
                evidence_age_seconds = (
                    max(0, int((now - last_evidence_at).total_seconds()))
                    if last_evidence_at is not None
                    else None
                )
                block_reason = ""
                if provider_blocked:
                    block_reason = "provider_block"
                elif remaining_rpd <= 0:
                    block_reason = "rpd_exhausted"
                elif remaining_rpm <= 0:
                    block_reason = "rpm_exhausted"
                elif (
                    isinstance(remaining_input_tpm, int)
                    and remaining_input_tpm <= 0
                ):
                    block_reason = "tpm_exhausted"
                elif in_flight >= int(profile.permit_limit):
                    block_reason = "permit_exhausted"
                snapshot[pair] = {
                    "profile_version": profile.profile_version,
                    "estimator_version": profile.estimator_version,
                    "remaining_rpd": remaining_rpd,
                    "remaining_rpm": remaining_rpm,
                    "remaining_input_tpm": remaining_input_tpm,
                    "input_tpm_rankable": tpm_rankable,
                    "provider_blocked": provider_blocked,
                    "state_degraded": bool(
                        state_status == "degraded"
                        and evidence_age_seconds is not None
                        and evidence_age_seconds
                        <= RANKING_EVIDENCE_FRESH_SECONDS
                    ),
                    "state_present": state is not None,
                    "evidence_age_seconds": evidence_age_seconds,
                    "evidence_fresh": bool(
                        evidence_age_seconds is not None
                        and evidence_age_seconds <= RANKING_EVIDENCE_FRESH_SECONDS
                    ),
                    "external_usage_suspected": bool(
                        state is not None and state.external_usage_suspected
                    ),
                    "permit_limit": int(profile.permit_limit),
                    "in_flight": in_flight,
                    "eligible": not bool(block_reason),
                    "block_reason": block_reason,
                    "latency_ms": max(
                        0,
                        int(
                            state.latency_ewma_ms
                            or state.last_latency_ms
                            or 0
                        ),
                    ) if state is not None else 0,
                }
        return snapshot
    except DatabaseError:
        return {}


def order_project_aliases(
    key_names,
    *,
    model: str,
    project_identities,
    snapshot,
    legacy_snapshot=None,
) -> list[str]:
    """Apply advisory V2 project ranking with stable legacy fallbacks."""
    aliases = list(key_names or ())
    if not aliases or not isinstance(snapshot, dict) or not snapshot:
        return aliases
    identities = project_identities if isinstance(project_identities, dict) else {}

    def score(item):
        original_index, alias = item
        identity = str(identities.get(alias) or "")
        decision = project_candidate_decision(
            alias,
            model=model,
            project_identity=identity,
            snapshot=snapshot,
            legacy_snapshot=legacy_snapshot,
        )
        if not decision["ranked"]:
            # Unknown evidence stays in original order between known usable
            # projects and projects with an authoritative local block.
            return (1, 0, 0, 0, 0, 0, original_index)
        remaining = (
            int(decision["remaining_rpd"] or 0),
            int(decision["remaining_rpm"] or 0),
        )
        remaining_tpm = decision["remaining_input_tpm"]
        category = (
            3
            if not decision["eligible"]
            else 2
            if decision["state_degraded"]
            else 0
        )
        latency = int(decision["latency_ms"] or 0)
        return (
            category,
            max(0, int(decision["in_flight"] or 0)),
            -remaining[0],
            -remaining[1],
            -int(remaining_tpm) if isinstance(remaining_tpm, int) else 0,
            latency if latency > 0 else 2**31 - 1,
            original_index,
        )

    return [alias for _index, alias in sorted(enumerate(aliases), key=score)]


def project_candidate_decision(
    key_name: str,
    *,
    model: str,
    project_identity: str,
    snapshot,
    legacy_snapshot=None,
) -> dict:
    """Combine V2 and legacy headroom without inventing extra capacity."""
    row = (
        snapshot.get((str(project_identity or ""), str(model or "")))
        if isinstance(snapshot, dict) and project_identity
        else None
    )
    if not isinstance(row, dict):
        return {
            "ranked": False,
            "eligible": True,
            "block_reason": "",
            "remaining_rpd": None,
            "remaining_rpm": None,
            "remaining_input_tpm": None,
            "state_degraded": False,
            "in_flight": 0,
            "latency_ms": 0,
            "evidence_age_seconds": None,
            "evidence_fresh": False,
            "external_usage_suspected": False,
        }

    legacy = (
        legacy_snapshot.get((str(key_name), str(model or "")), {})
        if isinstance(legacy_snapshot, dict)
        else {}
    )

    def conservative(v2_value, legacy_name):
        legacy_value = legacy.get(legacy_name)
        values = [
            int(value)
            for value in (v2_value, legacy_value)
            if value is not None
        ]
        return min(values) if values else None

    remaining_rpd = conservative(row.get("remaining_rpd"), "rpd")
    remaining_rpm = conservative(row.get("remaining_rpm"), "rpm")
    remaining_input_tpm = (
        conservative(row.get("remaining_input_tpm"), "tpm")
        if row.get("input_tpm_rankable")
        else None
    )
    block_reason = _safe_reason(row.get("block_reason"))
    if not block_reason and remaining_rpd is not None and remaining_rpd <= 0:
        block_reason = "rpd_exhausted"
    elif not block_reason and remaining_rpm is not None and remaining_rpm <= 0:
        block_reason = "rpm_exhausted"
    elif (
        not block_reason
        and remaining_input_tpm is not None
        and remaining_input_tpm <= 0
    ):
        block_reason = "tpm_exhausted"
    return {
        "ranked": True,
        "eligible": not bool(block_reason),
        "block_reason": block_reason,
        "remaining_rpd": remaining_rpd,
        "remaining_rpm": remaining_rpm,
        "remaining_input_tpm": remaining_input_tpm,
        "state_degraded": bool(row.get("state_degraded")),
        "in_flight": max(0, int(row.get("in_flight") or 0)),
        "latency_ms": max(0, int(row.get("latency_ms") or 0)),
        "evidence_age_seconds": row.get("evidence_age_seconds"),
        "evidence_fresh": bool(row.get("evidence_fresh")),
        "external_usage_suspected": bool(
            row.get("external_usage_suspected")
        ),
    }


def _safe_token(value, *, limit: int) -> str:
    text = str(value or "").strip()[:limit]
    return text if not text or _SAFE_TOKEN.fullmatch(text) else ""


def _safe_reason(value) -> str:
    text = str(value or "").strip().casefold()[:32]
    return text if _SAFE_REASON.fullmatch(text) else ""


def _ownership_contention(error: OperationalError) -> bool:
    """Identify only retryable row/table lock contention.

    Accounting remains fail-soft for a genuine database outage.  A duplicate
    source/lane race is different: SQLite reports it as ``database ... locked``
    because ``select_for_update`` is a no-op there, while MariaDB reports a
    deadlock or lock-wait timeout.  Those cases must retry and ultimately fail
    closed instead of silently returning a provider-permitting null observer.
    """
    raw_args = tuple(getattr(getattr(error, "__cause__", None), "args", ()) or ())
    raw_args += tuple(getattr(error, "args", ()) or ())
    codes = {
        int(value)
        for value in raw_args
        if isinstance(value, int) and not isinstance(value, bool)
    }
    if codes.intersection({1205, 1213}):
        return True
    text = " ".join(str(value or "") for value in raw_args).casefold()
    return any(token in text for token in (
        "database is locked",
        "database table is locked",
        "deadlock found",
        "lock wait timeout",
    ))


def sanitize_candidate_plan(candidate_plan) -> list[dict]:
    """Return the strict, secret-free persisted candidate representation."""
    result: list[dict] = []
    for position, raw in enumerate(candidate_plan or (), start=1):
        if not isinstance(raw, dict):
            continue
        try:
            candidate_index = max(1, int(raw.get("candidate_index") or position))
        except (TypeError, ValueError):
            candidate_index = position
        identity = _safe_token(raw.get("project_identity"), limit=80)
        model = _safe_token(raw.get("model"), limit=80)
        if not model:
            continue
        identity_status = str(raw.get("identity_status") or "").strip().casefold()
        if identity_status not in {
            "known", "assumed", "unknown", "unconfigured", "duplicate",
        }:
            identity_status = "known" if identity else "unknown"
        item = {
            "candidate_index": candidate_index,
            "project_identity": identity,
            "model": model,
            "identity_status": identity_status,
            "initial_skip_reason": _safe_reason(raw.get("skip_reason")),
        }
        # This assertion makes adding a field an explicit privacy decision.
        if set(item) != _PLAN_FIELDS:
            raise AssertionError("Gemini candidate plan allowlist drift")
        result.append(item)
    return result


def generic_candidate_plan(
    *,
    role: str,
    models,
    manual_key: str | None = None,
    execution_candidates=None,
) -> list[dict]:
    """Build the exact first-round execution order, manual candidates first."""
    from management.services import gemini_keys

    rows: list[dict] = []
    candidate_index = 0
    manual_value = str(manual_key or "").strip()
    explicit_identities = gemini_keys.explicit_project_groups()
    manual_fingerprint = gemini_keys.credential_fingerprint(manual_value)
    pool = gemini_keys.role_key_pools().get(role, {"own": [], "borrow": []})
    aliases: list[str] = []
    for alias in (*pool.get("own", ()), *pool.get("borrow", ())):
        if alias not in aliases:
            aliases.append(alias)
    seen_fingerprints_by_model: dict[str, list[bytes]] = {}
    seen_projects_by_model: dict[str, set[str]] = {}

    def append_candidate(key_name: str, model: str, *, skip_reason: str = ""):
        nonlocal candidate_index
        model = str(model)
        value = manual_value if key_name == "(manual)" else gemini_keys._key_value(key_name)
        alias = (
            gemini_keys.configured_alias_for_secret(manual_value)
            if key_name == "(manual)" and manual_value
            else key_name
        )
        identity = gemini_keys.project_group(alias) if alias else ""
        fingerprint = (
            manual_fingerprint
            if key_name == "(manual)"
            else gemini_keys.credential_fingerprint(value)
        )
        seen_fingerprints = seen_fingerprints_by_model.setdefault(model, [])
        seen_projects = seen_projects_by_model.setdefault(model, set())
        duplicate = bool(
            fingerprint
            and any(
                secrets.compare_digest(fingerprint, existing)
                for existing in seen_fingerprints
            )
        ) or bool(identity and identity in seen_projects)
        effective_skip_reason = skip_reason or ("duplicate" if duplicate else "")
        candidate_index += 1
        rows.append({
            "candidate_index": candidate_index,
            "key_name": key_name,
            "model": model,
            "project_identity": identity,
            "identity_status": (
                "unconfigured"
                if not value
                else "duplicate"
                if duplicate
                else "known"
                if alias in explicit_identities
                else "assumed"
                if identity
                else "unknown"
            ),
            "skip_reason": effective_skip_reason,
        })
        if fingerprint and not any(
            secrets.compare_digest(fingerprint, existing)
            for existing in seen_fingerprints
        ):
            seen_fingerprints.append(fingerprint)
        if identity and value:
            seen_projects.add(identity)

    if manual_value:
        for model in models:
            append_candidate("(manual)", str(model))

    executable_pairs: list[tuple[str, str]] = []
    for raw in execution_candidates or ():
        try:
            key_name, _key_value, model, *metadata = raw
        except (TypeError, ValueError):
            continue
        pair = (str(key_name), str(model))
        if pair not in executable_pairs:
            executable_pairs.append(pair)
            append_candidate(
                *pair,
                skip_reason=(
                    _safe_reason(metadata[0])[:24] if metadata else ""
                ),
            )

    remaining_pairs = [
        (str(alias), str(model))
        for model in models
        for alias in aliases
        if (str(alias), str(model)) not in executable_pairs
    ]
    for alias, model in remaining_pairs:
        append_candidate(
            alias,
            model,
            skip_reason=(
                "unconfigured"
                if not gemini_keys._key_value(alias)
                else "not_available_plan"
            ),
        )
    return rows


class NullAttemptBoundary:
    attempt_id = None

    def validate_ownership(self) -> bool:
        return True

    def before_provider(self, **_kwargs) -> bool:
        return True

    def succeeded(self, _usage=None) -> None:
        return None

    def failed(self, _error, *, usage=None, failure_kind: str = "") -> None:
        return None

    def manual_result(self, **_kwargs) -> None:
        return None

    def cancelled_pre_dispatch(self, _error=None) -> None:
        return None


class NullRequestObserver:
    enabled = False
    provider_blocked = False
    block_reason = ""
    request_id = ""
    graph_id = None

    def attempt(self, **_kwargs):
        return NullAttemptBoundary()

    def resolve_failure(self, _reason="exhausted") -> None:
        return None

    def resolve_without_provider(self, _reason="no_model") -> None:
        return None

    def candidate_index(self, _key_name: str, _model: str) -> int:
        return 0

    def record_not_attempted(self, **_kwargs):
        return None

    def record_remaining(self, _reason="policy_stop", *, model_filter: str = "") -> None:
        return None


NULL_OBSERVER = NullRequestObserver()


class RejectedAttemptBoundary(NullAttemptBoundary):
    admitted = False

    def validate_ownership(self) -> bool:
        return False

    def before_provider(self, **_kwargs) -> bool:
        return False


class BlockedRequestObserver(NullRequestObserver):
    provider_blocked = True

    def __init__(self, reason: str):
        self.block_reason = _safe_reason(reason) or "ownership_conflict"

    def attempt(self, **_kwargs):
        return RejectedAttemptBoundary()


def blocked_observer(reason: str = "ownership_conflict") -> BlockedRequestObserver:
    return BlockedRequestObserver(reason)


def _routing_value(routing_decision, name: str, default=""):
    value = getattr(routing_decision, name, default) if routing_decision is not None else default
    return getattr(value, "value", value)


def _quota_profile_version_for_plan(safe_plan, *, now) -> str:
    """Return the active model-profile policy version with one bounded read."""
    from management.models import GeminiQuotaProfile

    if not safe_plan:
        return ""
    models = {str(item.get("model") or "") for item in safe_plan if item.get("model")}
    if not models:
        return ""
    active_rows = list(
        GeminiQuotaProfile.objects.filter(
            model__in=models,
            effective_from__lte=now,
        )
        .filter(models_Q_effective(now))
        .order_by("model", "-effective_from", "-id")
    )
    active_by_model = {}
    for profile in active_rows:
        active_by_model.setdefault(profile.model, profile)
    if any(model not in active_by_model for model in models):
        return ""
    versions = {
        str(active_by_model[model].profile_version) for model in models
    }
    return next(iter(versions)) if len(versions) == 1 else "mixed"


def begin_request(
    *,
    request_id: str | None,
    role: str,
    reasoning_task: str,
    candidate_plan,
    deadline_seconds: float | None = None,
    routing_decision=None,
    lane: str = "",
    request_policy_manifest=None,
    legacy_provider_root: bool = False,
) -> "RequestObserver | NullRequestObserver":
    """Create one immutable graph only when the shadow gate is active."""
    from management.services.gemini_accounting_contract import (
        RequestPolicyManifestError,
        sanitize_request_policy_manifest,
    )

    try:
        safe_policy_manifest = sanitize_request_policy_manifest(
            request_policy_manifest
        )
    except RequestPolicyManifestError as exc:
        return blocked_observer(
            exc.code if exc.code == "policy_manifest_mismatch"
            else "policy_manifest_invalid"
        )
    if not shadow_runtime_active():
        if _revision_execution.get() is not None:
            return blocked_observer("provider_accounting_unavailable")
        return NULL_OBSERVER
    try:
        from management.models import (
            GeminiRequest,
            InstagramBotMessage,
        )
        from management.services.gemini_accounting_contract import (
            canonical_candidate_plan_digest,
        )
        from management.services.ig_turn_lineage import current_context

        now = timezone.now()
        lineage = current_context()
        safe_plan = sanitize_candidate_plan(candidate_plan)
        resolved_lane = str(
            lane
            or lineage.get("lane")
            or ("followup" if reasoning_task == "follow_cta_copy" else "live" if role == "chat" else "analysis")
        )[:16]
        task_class = str(
            _routing_value(
                routing_decision,
                "task_class",
                "diagnostic" if resolved_lane == "diagnostic" else "durable_analysis" if role != "chat" else "ordinary_live",
            )
        )[:24]
        deadline_ms = max(0, int(float(deadline_seconds or 0) * 1000))
        profile_version = _quota_profile_version_for_plan(safe_plan, now=now)
        source_message_id = lineage.get("source_message_id") or None
        logical_turn_id = str(lineage.get("logical_turn_id") or "")
        revision_execution = _revision_execution.get()
        legacy_execution = None
        legacy_root_execution = None
        parent_request_id = None
        source_execution_key = ""
        explicit_revision = revision_execution is not None or logical_turn_id.startswith("ig-revision:")
        if lineage.get("source_execution_key"):
            return blocked_observer("revision_execution_invalid")
        if explicit_revision:
            if not _revision_execution_valid(
                revision_execution, source_message_id=source_message_id,
                client_id=lineage.get("client_id"), lane=resolved_lane,
                logical_turn_id=logical_turn_id,
            ):
                return blocked_observer("revision_execution_invalid")
            source_execution_key = logical_turn_id
        if legacy_provider_root and (
            explicit_revision
            or resolved_lane != "live"
            or not source_message_id
            or not lineage.get("client_id")
            or not logical_turn_id
            or not lineage.get("automation_token")
        ):
            return blocked_observer("legacy_live_claim_invalid")
        provider_continuation = None
        if source_execution_key:
            from management.services.ig_revision_provider_execution import revision_provider_continuation

            provider_continuation = revision_provider_continuation(
                revision_execution.revision_id, revision_execution.token,
                settings_id=revision_execution.settings_id,
                settings_permission_epoch=revision_execution.settings_permission_epoch,
                candidate_plan=candidate_plan, now=now,
            )
            if not provider_continuation.ready:
                return blocked_observer(provider_continuation.reason)
            candidate_plan = list(provider_continuation.candidate_plan)
            safe_plan = sanitize_candidate_plan(candidate_plan)
        elif (
            resolved_lane == "recovery"
            and lineage.get("recovery_job_id")
            and lineage.get("recovery_token")
            and lineage.get("automation_token")
        ):
            from management.services.ig_legacy_provider_execution import (
                legacy_provider_continuation,
                resolve_legacy_execution,
            )

            legacy_execution, legacy_reason = resolve_legacy_execution(
                job_id=lineage.get("recovery_job_id"),
                recovery_token=lineage.get("recovery_token"),
                automation_token=lineage.get("automation_token"),
                client_id=lineage.get("client_id"),
                source_message_id=source_message_id,
                logical_turn_id=logical_turn_id,
                now=now,
            )
            if legacy_execution is None:
                return blocked_observer(legacy_reason)
            source_execution_key = legacy_execution.source_execution_key
            parent_request_id = legacy_execution.root_graph_id
            provider_continuation = legacy_provider_continuation(
                legacy_execution, candidate_plan=candidate_plan, now=now,
            )
            if not provider_continuation.ready:
                return blocked_observer(provider_continuation.reason)
            candidate_plan = list(provider_continuation.candidate_plan)
            safe_plan = sanitize_candidate_plan(candidate_plan)
        resolved_request_id = str(request_id or uuid.uuid4().hex)[:40]
        last_contention = None
        for delay in OWNERSHIP_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                with transaction.atomic():
                    locked_message = None
                    if source_message_id:
                        locked_message = (
                            InstagramBotMessage.objects.select_for_update()
                            .filter(pk=source_message_id)
                            .only("pk", "gemini_task_class")
                            .first()
                        )
                        if locked_message is None:
                            return blocked_observer("source_message_missing")
                        message_task_class = str(
                            locked_message.gemini_task_class or ""
                        )
                        if not source_execution_key and (
                            (
                                task_class == "no_model"
                                and message_task_class not in {"", "no_model"}
                            )
                            or (
                                task_class != "no_model"
                                and message_task_class == "no_model"
                            )
                        ):
                            return blocked_observer("message_route_conflict")
                        existing = list(
                            GeminiRequest.objects.select_for_update()
                            .filter(
                                source_message_id=source_message_id,
                                lane=resolved_lane,
                                source_execution_key=source_execution_key,
                            )
                            .order_by("id")[:2]
                        )
                        if existing:
                            # The database uniqueness constraint is the final
                            # cross-backend invariant.  The inbound row remains
                            # the MariaDB mutex and this explicit read provides
                            # a deterministic blocked reason on both engines.
                            if (
                                len(existing) == 1
                                and existing[0].policy_manifest
                                != safe_policy_manifest
                            ):
                                return blocked_observer("policy_manifest_mismatch")
                            return blocked_observer(
                                "duplicate_source_lane"
                                if len(existing) == 1
                                else "multiple_source_lane"
                            )
                    existing_request = (
                        GeminiRequest.objects.select_for_update()
                        .filter(request_id=resolved_request_id)
                        .only("pk", "policy_manifest")
                        .first()
                    )
                    if existing_request is not None:
                        return blocked_observer(
                            "policy_manifest_mismatch"
                            if existing_request.policy_manifest != safe_policy_manifest
                            else "request_conflict"
                        )
                    legacy_context_token = (
                        _legacy_execution.set(legacy_execution)
                        if legacy_execution is not None else None
                    )
                    try:
                        graph = GeminiRequest.objects.create(
                            request_id=resolved_request_id,
                            parent_request_id=parent_request_id,
                            lane=resolved_lane,
                            task_class=task_class,
                            reasoning_task=str(reasoning_task or "")[:40],
                            logical_turn_id=str(lineage.get("logical_turn_id") or "")[:64],
                            source_message_id=source_message_id,
                            source_execution_key=source_execution_key,
                            client_id=lineage.get("client_id") or None,
                            recovery_job_id=lineage.get("recovery_job_id") or None,
                            routing_policy_version=str(
                                _routing_value(routing_decision, "policy_version", "")
                            )[:32],
                            accounting_policy_version=ACCOUNTING_POLICY_VERSION,
                            quota_profile_version=profile_version,
                            authority_snapshot_version=str(
                                _routing_value(
                                    routing_decision,
                                    "authority_snapshot_version",
                                    "",
                                )
                            )[:32],
                            routing_mode=str(
                                _routing_value(routing_decision, "routing_mode", "")
                            )[:12],
                            commercial_risk=str(
                                _routing_value(routing_decision, "commercial_risk", "")
                            )[:16],
                            requires_media_reasoning=bool(
                                _routing_value(
                                    routing_decision,
                                    "requires_media_reasoning",
                                    False,
                                )
                            ),
                            candidate_plan=safe_plan,
                            candidate_plan_digest=canonical_candidate_plan_digest(
                                safe_plan
                            ),
                            policy_manifest=safe_policy_manifest,
                            deadline_ms=deadline_ms,
                            deadline_at=(
                                now + dt.timedelta(milliseconds=deadline_ms)
                                if deadline_ms
                                else None
                            ),
                            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
                        )
                    finally:
                        if legacy_context_token is not None:
                            _legacy_execution.reset(legacy_context_token)
                    if legacy_provider_root:
                        from management.services.ig_legacy_provider_execution import (
                            initialize_legacy_provider_root,
                        )

                        legacy_root_execution, provider_continuation = (
                            initialize_legacy_provider_root(
                                graph_id=graph.pk,
                                automation_token=lineage.get("automation_token"),
                                candidate_plan=candidate_plan,
                                now=now,
                            )
                        )
                        if legacy_root_execution is None:
                            raise _LegacyProviderRootBlocked(
                                provider_continuation.reason
                            )
                observer = RequestObserver(
                    graph_id=graph.pk,
                    request_id=graph.request_id,
                    raw_plan=candidate_plan,
                    source_message_id=source_message_id,
                    lane=resolved_lane,
                    source_execution_key=source_execution_key,
                    revision_execution=revision_execution,
                    legacy_execution=legacy_execution,
                    legacy_root_execution=legacy_root_execution,
                )
                observer.provider_continuation = provider_continuation
                return observer
            except _LegacyProviderRootBlocked as exc:
                return blocked_observer(exc.reason)
            except IntegrityError:
                # ``request_id`` and source/lane/execution identity are
                # both durable ownership conflicts.  Neither may degrade into
                # a provider-permitting null observer.
                return blocked_observer("request_conflict")
            except OperationalError as exc:
                if source_message_id and _ownership_contention(exc):
                    last_contention = exc
                    continue
                if explicit_revision:
                    return blocked_observer("revision_execution_invalid")
                if legacy_execution is not None or legacy_provider_root:
                    return blocked_observer("legacy_execution_invalid")
                return NULL_OBSERVER
        if last_contention is not None:
            return blocked_observer("ownership_contention")
        return NULL_OBSERVER
    except Exception:
        # Explicit revisions cannot turn an invalid claim or a failed identity
        # lookup into a provider-permitting legacy null observer.
        if (
            _revision_execution.get() is not None
            or locals().get("explicit_revision", False)
            or locals().get("legacy_execution") is not None
            or legacy_provider_root
        ):
            return blocked_observer(
                "revision_execution_invalid"
                if locals().get("explicit_revision", False)
                else "legacy_execution_invalid"
            )
        return NULL_OBSERVER


def record_no_model_decision(message, routing_decision):
    """Persist one idempotent NO_MODEL graph without crossing provider I/O.

    The message already owns the immutable ``RoutingDecision``.  This helper
    adds the same request-level observability used by provider-backed turns,
    with an empty candidate plan, no attempt rows and no provider phase.  It is
    intentionally shadow-gated and fail-soft like the rest of accounting V2.
    """
    if str(_routing_value(routing_decision, "task_class", "")) != "no_model":
        return NULL_OBSERVER
    message_id = getattr(message, "pk", None)
    if not message_id or not shadow_runtime_active():
        return NULL_OBSERVER
    try:
        from django.utils.crypto import salted_hmac
        from management.services.ig_turn_lineage import (
            current_context,
            resolve_logical_turn_key,
            turn_lineage,
        )

        current = current_context()
        lane = str(_routing_value(routing_decision, "lane", "live") or "live")[:16]
        logical_turn_id = str(
            current.get("logical_turn_id") or resolve_logical_turn_key(message) or ""
        )[:64]
        request_material = ":".join((
            str(message_id),
            lane,
            str(_routing_value(routing_decision, "policy_version", "")),
        ))
        digest = salted_hmac(
            "management.gemini-accounting.no-model.v1",
            request_material,
        ).hexdigest()[:34]
        with turn_lineage(
            lane=lane,
            client_id=getattr(message, "client_id", None),
            source_message_id=message_id,
            logical_turn_id=logical_turn_id,
            incident_id=current.get("incident_id"),
            recovery_job_id=current.get("recovery_job_id"),
        ):
            observer = begin_request(
                request_id=f"nm_{digest}",
                role="chat",
                reasoning_task="no_model",
                candidate_plan=[],
                deadline_seconds=0,
                routing_decision=routing_decision,
                lane=lane,
            )
        if getattr(observer, "enabled", False):
            observer.resolve_without_provider("no_model")
        return observer
    except Exception:
        return NULL_OBSERVER


@dataclass
class AttemptBoundary:
    observer: "RequestObserver"
    key_name: str
    model: str
    candidate_index: int
    attempt_index: int
    attempt_id: int | None = None
    state_id: int | None = None
    started_monotonic: float | None = None
    admitted: bool = False
    provider_repair_token: str = ""

    def validate_ownership(self) -> bool:
        try:
            return bool(self.observer._validate_boundary(self))
        except Exception:
            return False

    def before_provider(self, *, serialized_bytes: int, inline_count: int = 0) -> bool:
        try:
            self.admitted = bool(self.observer._before_provider(
                self,
                serialized_bytes=max(0, int(serialized_bytes or 0)),
                inline_count=max(0, int(inline_count or 0)),
            ))
            return self.admitted
        except Exception:
            # Once accounting shadow owns a source/lane, inability to prove
            # atomic ownership is a pre-dispatch rejection, never permission
            # to call the provider without canonical evidence.
            self.attempt_id = None
            self.state_id = None
            self.admitted = False
            return False

    def succeeded(self, usage=None) -> None:
        try:
            self.observer._finish(self, succeeded=True, usage=usage or {})
        except Exception:
            return None

    def failed(self, error, *, usage=None, failure_kind: str = "") -> None:
        try:
            self.observer._finish(
                self,
                succeeded=False,
                error=error,
                usage=usage or {},
                failure_kind=failure_kind,
            )
        except Exception:
            return None

    def manual_result(
        self,
        *,
        succeeded: bool,
        http_code: int | None = None,
        failure_kind: str = "",
        usage=None,
    ) -> None:
        error = None if succeeded else ManualProviderFailure(
            failure_kind=failure_kind,
            http_code=http_code,
        )
        try:
            self.observer._finish(
                self,
                succeeded=succeeded,
                error=error,
                usage=usage or {},
                http_code=http_code,
                failure_kind=failure_kind,
            )
        except Exception:
            return None

    def cancelled_pre_dispatch(self, error=None) -> None:
        try:
            self.observer._cancel_pre_dispatch(self, error=error)
        except Exception:
            return None


@dataclass(frozen=True)
class ManualProviderFailure(Exception):
    failure_kind: str
    http_code: int | None = None

    def __str__(self):
        return self.failure_kind or "provider_error"


class RequestObserver:
    enabled = True
    provider_blocked = False
    block_reason = ""

    def __init__(
        self,
        *,
        graph_id: int,
        request_id: str,
        raw_plan,
        source_message_id=None,
        lane: str = "",
        source_execution_key: str | None = None,
        revision_execution=None,
        legacy_execution=None,
        legacy_root_execution=None,
    ):
        self.graph_id = int(graph_id)
        self.request_id = str(request_id)
        self.source_message_id = int(source_message_id) if source_message_id else None
        self.lane = str(lane or "")[:16]
        self.source_execution_key = source_execution_key
        self._revision_execution = revision_execution
        self._legacy_execution = legacy_execution
        self._legacy_root_execution = legacy_root_execution
        self.provider_continuation = None
        self._counter = 0
        self._lock = threading.Lock()
        self._candidate_indexes: dict[tuple[str, str], int] = {}
        self._candidate_identities: dict[tuple[str, str], str] = {}
        self._candidate_identity_status: dict[tuple[str, str], str] = {}
        self._plan_candidates: list[tuple[str, str, int]] = []
        self._plan_membership: set[tuple[str, str, int]] = set()
        self._plan_skip_reasons: dict[int, str] = {}
        for position, raw in enumerate(raw_plan or (), start=1):
            if not isinstance(raw, dict):
                continue
            key = (str(raw.get("key_name") or ""), str(raw.get("model") or ""))
            try:
                index = max(1, int(raw.get("candidate_index") or position))
            except (TypeError, ValueError):
                index = position
            self._candidate_indexes.setdefault(key, index)
            self._candidate_identities.setdefault(
                key, _safe_token(raw.get("project_identity"), limit=80)
            )
            self._candidate_identity_status.setdefault(
                key, str(raw.get("identity_status") or "unknown").casefold()
            )
            self._plan_candidates.append((key[0], key[1], index))
            self._plan_membership.add((key[0], key[1], index))
            initial_skip = _safe_reason(raw.get("skip_reason"))[:24]
            if initial_skip:
                self._plan_skip_reasons[index] = initial_skip

    def candidate_index(self, key_name: str, model: str) -> int:
        return int(self._candidate_indexes.get((str(key_name), str(model)), 0) or 0)

    def _allocate_attempt_index(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def attempt(self, *, key_name: str, model: str, candidate_index: int = 0):
        boundary = AttemptBoundary(
            observer=self,
            key_name=str(key_name or "")[:40],
            model=str(model or "")[:80],
            candidate_index=max(
                0,
                int(candidate_index or self.candidate_index(key_name, model) or 0),
            ),
            attempt_index=self._allocate_attempt_index(),
        )
        pending = getattr(self, "_pending_provider_repair", None)
        if pending and (pending["key_name"], pending["model"], pending["candidate_index"]) == (boundary.key_name, boundary.model, boundary.candidate_index):
            boundary.provider_repair_token = pending["token"]
            self._pending_provider_repair = None
        return boundary

    def reserve_provider_repair(self, *, key_name, model, candidate_index=0):
        if (
            self._legacy_execution is not None
            or self._legacy_root_execution is not None
        ):
            from management.services.ig_legacy_provider_execution import (
                reserve_legacy_provider_repair,
            )

            return reserve_legacy_provider_repair(
                self, key_name=key_name, model=model,
                candidate_index=candidate_index,
            )
        from management.services.ig_revision_provider_execution import reserve_provider_repair

        return reserve_provider_repair(self, key_name=key_name, model=model, candidate_index=candidate_index)

    def record_not_attempted(
        self,
        *,
        key_name: str,
        model: str,
        candidate_index: int = 0,
        reason: str,
    ):
        """Persist one considered candidate that never crossed provider I/O."""
        from management.models import GeminiRequest, GeminiRequestAttempt

        now = timezone.now()
        boundary = AttemptBoundary(
            observer=self,
            key_name=str(key_name or "")[:40],
            model=str(model or "")[:80],
            candidate_index=max(
                0,
                int(candidate_index or self.candidate_index(key_name, model) or 0),
            ),
            attempt_index=self._allocate_attempt_index(),
        )
        identity = self._identity_for(boundary)
        bounded_reason = _safe_reason(reason)[:24] or "policy_stop"
        try:
            with transaction.atomic():
                graph, _message = self._lock_canonical_graph()
                if graph is None:
                    return None
                outcomes = dict(graph.candidate_outcomes or {})
                outcome_key = str(
                    boundary.candidate_index or boundary.attempt_index
                )
                prior = outcomes.get(outcome_key)
                prior_items = prior if isinstance(prior, list) else [prior]
                if any(
                    isinstance(item, dict)
                    and item.get("outcome") == "not_attempted"
                    and item.get("reason") == bounded_reason
                    for item in prior_items
                ):
                    return None
                if GeminiRequestAttempt.objects.filter(
                    request_graph=graph,
                    candidate_index=boundary.candidate_index,
                ).exists():
                    return None
                attempt = GeminiRequestAttempt.objects.create(
                    request_id=self.request_id,
                    request_graph=graph,
                    role=self._role_for_graph(graph),
                    key_name=boundary.key_name,
                    project_group=identity,
                    project_identity=identity,
                    model=boundary.model,
                    outcome="not_attempted",
                    fsm_state=GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
                    accounting_mode="shadow",
                    shadow_decision=GeminiRequestAttempt.ShadowDecision.UNKNOWN,
                    shadow_deny_reason="not_dispatched",
                    decision="skip_candidate",
                    logical_turn_id=graph.logical_turn_id,
                    source_message_id=graph.source_message_id,
                    client_id=graph.client_id,
                    lane=graph.lane,
                    attempt_index=boundary.attempt_index,
                    candidate_index=boundary.candidate_index,
                    not_attempted_reason=bounded_reason,
                    recovery_job_id=graph.recovery_job_id,
                    finished_at=now,
                    settled_at=now,
                    reservation_released_at=now,
                    permit_released_at=now,
                )
                existing = outcomes.get(outcome_key)
                payload = {
                    "attempt_index": boundary.attempt_index,
                    "outcome": "not_attempted",
                    "failure_kind": "",
                    "reason": bounded_reason,
                }
                if existing is None:
                    outcomes[outcome_key] = payload
                elif isinstance(existing, list):
                    outcomes[outcome_key] = [*existing, payload]
                else:
                    outcomes[outcome_key] = [existing, payload]
                GeminiRequest.objects.filter(pk=graph.pk).update(
                    candidate_outcomes=outcomes,
                    updated_at=now,
                )
                return attempt
        except Exception:
            return None

    def record_remaining(self, reason: str, *, model_filter: str = "") -> None:
        try:
            from management.models import GeminiRequest, GeminiRequestAttempt

            now = timezone.now()
            with transaction.atomic():
                graph, _message = self._lock_canonical_graph()
                if graph is None:
                    return
                outcomes = dict(graph.candidate_outcomes or {})
                observed = {
                    int(key)
                    for key in outcomes
                    if str(key).isdigit()
                }
                observed.update(
                    GeminiRequestAttempt.objects.filter(request_graph=graph)
                    .exclude(candidate_index=0)
                    .values_list("candidate_index", flat=True)
                )
                rows = []
                for key_name, model, candidate_index in self._plan_candidates:
                    if model_filter and str(model) != str(model_filter):
                        continue
                    if candidate_index in observed:
                        continue
                    boundary = AttemptBoundary(
                        observer=self,
                        key_name=key_name,
                        model=model,
                        candidate_index=candidate_index,
                        attempt_index=self._allocate_attempt_index(),
                    )
                    bounded_reason = _safe_reason(
                        self._plan_skip_reasons.get(candidate_index, reason)
                    )[:24] or "policy_stop"
                    identity = self._identity_for(boundary)
                    rows.append(GeminiRequestAttempt(
                        request_id=self.request_id,
                        request_graph=graph,
                        role=self._role_for_graph(graph),
                        key_name=key_name,
                        project_group=identity,
                        project_identity=identity,
                        model=model,
                        outcome="not_attempted",
                        fsm_state=GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
                        accounting_mode="shadow",
                        shadow_decision=GeminiRequestAttempt.ShadowDecision.UNKNOWN,
                        shadow_deny_reason="not_dispatched",
                        decision="skip_candidate",
                        logical_turn_id=graph.logical_turn_id,
                        source_message_id=graph.source_message_id,
                        client_id=graph.client_id,
                        lane=graph.lane,
                        attempt_index=boundary.attempt_index,
                        candidate_index=candidate_index,
                        not_attempted_reason=bounded_reason,
                        recovery_job_id=graph.recovery_job_id,
                        finished_at=now,
                        settled_at=now,
                        reservation_released_at=now,
                        permit_released_at=now,
                    ))
                    outcomes[str(candidate_index)] = {
                        "attempt_index": boundary.attempt_index,
                        "outcome": "not_attempted",
                        "failure_kind": "",
                        "reason": bounded_reason,
                    }
                    observed.add(candidate_index)
                if not rows:
                    return
                GeminiRequestAttempt.objects.bulk_create(rows)
                GeminiRequest.objects.filter(pk=graph.pk).update(
                    candidate_outcomes=outcomes,
                    updated_at=now,
                )
        except Exception:
            return None

    def resolve_without_provider(self, reason: str = "no_model") -> None:
        """Terminalize an empty graph while proving no provider boundary ran."""
        try:
            from management.models import GeminiRequest, GeminiRequestAttempt

            now = timezone.now()
            bounded_reason = _safe_reason(reason)[:48] or "no_model"
            with transaction.atomic():
                graph, _message = self._lock_canonical_graph()
                if graph is None:
                    return
                if graph.terminal_resolution:
                    return
                if (
                    graph.candidate_plan
                    or graph.provider_phase_started_at is not None
                    or GeminiRequestAttempt.objects.filter(request_graph=graph).exists()
                ):
                    return
                GeminiRequest.objects.filter(pk=graph.pk).update(
                    terminal_resolution="succeeded",
                    terminal_reason=bounded_reason,
                    resolved_at=now,
                    updated_at=now,
                )
        except Exception:
            return None

    def _identity_for(self, boundary: AttemptBoundary) -> str:
        identity = self._candidate_identities.get(
            (boundary.key_name, boundary.model), ""
        )
        identity_status = self._candidate_identity_status.get(
            (boundary.key_name, boundary.model), "unknown"
        )
        if identity and identity_status == "known":
            return identity
        if boundary.key_name.startswith("GEMINI_API"):
            from management.services import gemini_keys

            explicit = gemini_keys.explicit_project_groups()
            if boundary.key_name in explicit:
                return _safe_token(explicit[boundary.key_name], limit=80)
        return ""

    def _active_profile(self, model: str, now):
        from management.models import GeminiQuotaProfile

        return (
            GeminiQuotaProfile.objects.filter(
                model=model,
                effective_from__lte=now,
            )
            .filter(
                models_Q_effective(now)
            )
            .order_by("-effective_from", "-id")
            .first()
        )

    def _lock_canonical_graph(self):
        """Lock and return exactly this observer's canonical source/lane graph.

        Every provider admission and settlement follows the same prefix:
        source message (when present), then canonical graph.  Callers may add a
        quota-state and attempt lock only after this helper returns.
        """
        from management.models import (
            GeminiRequest,
            InstagramBotMessage,
        )

        source_message_id = self.source_message_id
        lane = self.lane
        source_execution_key = self.source_execution_key
        if not lane or source_execution_key is None:
            identity_row = (
                GeminiRequest.objects.filter(pk=self.graph_id)
                .values("source_message_id", "lane", "source_execution_key")
                .first()
            )
            if identity_row is None:
                return None, None
            source_message_id = identity_row["source_message_id"]
            lane = str(identity_row["lane"] or "")
            source_execution_key = identity_row["source_execution_key"]
        locked_message = None
        if source_message_id:
            locked_message = (
                InstagramBotMessage.objects.select_for_update()
                .filter(pk=source_message_id)
                .only("pk", "gemini_task_class")
                .first()
            )
            if locked_message is None:
                return None, None
            graphs = list(
                GeminiRequest.objects.select_for_update()
                .filter(
                    source_message_id=source_message_id, lane=lane,
                    source_execution_key=source_execution_key,
                )
                .order_by("id")[:2]
            )
            if len(graphs) != 1 or graphs[0].pk != self.graph_id:
                return None, locked_message
            graph = graphs[0]
        else:
            graph = (
                GeminiRequest.objects.select_for_update()
                .filter(pk=self.graph_id)
                .first()
            )
        if graph is None or graph.request_id != self.request_id:
            return None, locked_message
        return graph, locked_message

    def _lock_valid_boundary_graph(self, boundary: AttemptBoundary):
        """Lock message -> canonical graph and validate one planned candidate."""
        from management.models import GeminiRequestAttempt

        if boundary.attempt_id is not None:
            return None
        exact_member = (
            boundary.key_name,
            boundary.model,
            int(boundary.candidate_index),
        )
        if exact_member not in self._plan_membership:
            return None
        graph, locked_message = self._lock_canonical_graph()
        if graph is None:
            return None
        if self._legacy_root_execution is not None:
            from management.services.ig_legacy_provider_execution import (
                validate_legacy_root_execution,
            )

            if (
                graph.pk != self._legacy_root_execution.root_graph_id
                or not validate_legacy_root_execution(self._legacy_root_execution)
            ):
                return None
        if graph.source_execution_key:
            if str(graph.source_execution_key).startswith("ig-recovery:"):
                from management.services.ig_legacy_provider_execution import (
                    validate_legacy_execution,
                )

                valid_execution = validate_legacy_execution(
                    self._legacy_execution,
                    source_message_id=graph.source_message_id,
                    client_id=graph.client_id, lane=graph.lane,
                    logical_turn_id=graph.logical_turn_id,
                    source_execution_key=graph.source_execution_key,
                )
            else:
                valid_execution = _revision_execution_valid(
                    self._revision_execution,
                    source_message_id=graph.source_message_id,
                    client_id=graph.client_id, lane=graph.lane,
                    logical_turn_id=graph.logical_turn_id,
                )
            if not valid_execution:
                return None
        if (
            graph.task_class == "no_model"
            or not graph.candidate_plan
            or graph.terminal_resolution
            or graph.winner_attempt_id is not None
        ):
            return None
        if locked_message is not None and not graph.source_execution_key:
            message_task = str(locked_message.gemini_task_class or "")
            if message_task == "no_model":
                return None

        matching = [
            item
            for item in graph.candidate_plan
            if isinstance(item, dict)
            and int(item.get("candidate_index") or 0) == int(boundary.candidate_index)
        ]
        if len(matching) != 1:
            return None
        planned = matching[0]
        if (
            str(planned.get("model") or "") != boundary.model
            or str(planned.get("initial_skip_reason") or "")
        ):
            return None
        planned_identity = str(planned.get("project_identity") or "")
        identity_status = str(planned.get("identity_status") or "")
        actual_identity = self._identity_for(boundary)
        if (
            identity_status == "known"
            and planned_identity
            and planned_identity != actual_identity
        ):
            return None
        if GeminiRequestAttempt.objects.filter(
            request_graph=graph,
            candidate_index=boundary.candidate_index,
            fsm_state__in=(
                GeminiRequestAttempt.FsmState.PLANNED,
                GeminiRequestAttempt.FsmState.RESERVED,
                GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                GeminiRequestAttempt.FsmState.SUCCEEDED,
                GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
                GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
                GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
            ),
        ).exists():
            return None
        return graph

    def _validate_boundary(self, boundary: AttemptBoundary) -> bool:
        with transaction.atomic():
            return self._lock_valid_boundary_graph(boundary) is not None

    def _before_provider(
        self,
        boundary: AttemptBoundary,
        *,
        serialized_bytes: int,
        inline_count: int,
    ) -> bool:
        last_error = None
        for delay in DB_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                return bool(self._before_provider_once(
                    boundary,
                    serialized_bytes=serialized_bytes,
                    inline_count=inline_count,
                ))
            except (OperationalError, IntegrityError) as exc:
                last_error = exc
                boundary.attempt_id = None
                boundary.state_id = None
                boundary.started_monotonic = None
        if last_error is not None:
            raise last_error
        return False

    def _before_provider_once(
        self,
        boundary: AttemptBoundary,
        *,
        serialized_bytes: int,
        inline_count: int,
    ) -> bool:
        from management.models import (
            GeminiQuotaState,
            GeminiRequest,
            GeminiRequestAttempt,
        )
        from management.services.gemini_quota import pacific_day

        now = timezone.now()
        identity = self._identity_for(boundary)
        estimated = max(1, int(math.ceil(serialized_bytes / 4)))
        profile = self._active_profile(boundary.model, now) if identity else None
        expiry = now + dt.timedelta(seconds=ATTEMPT_PERMIT_SECONDS)

        with transaction.atomic():
            # Canonical admission is revalidated in the same transaction that
            # records provider_started.  Lock order is always
            # message -> graph -> quota state.
            graph = self._lock_valid_boundary_graph(boundary)
            if graph is None:
                return False
            state = None
            shadow_decision = GeminiRequestAttempt.ShadowDecision.UNKNOWN
            deny_reason = "unknown_project" if not identity else "missing_profile" if profile is None else ""
            if identity and profile is not None:
                state, _created = (
                    GeminiQuotaState.objects.select_for_update()
                    .select_related("quota_profile")
                    .get_or_create(
                    project_identity=identity,
                    model=boundary.model,
                    defaults={
                        "quota_profile": profile,
                        "pacific_day": pacific_day(now),
                    },
                    )
                )
                if (
                    state.quota_profile_id != profile.pk
                    and not state.in_flight_count
                ):
                    try:
                        from management.services.gemini_accounting_contract import (
                            rotate_locked_quota_state_profile,
                        )

                        state, _audit = rotate_locked_quota_state_profile(
                            state=state,
                            new_profile=profile,
                            reason="shadow_effective_profile_transition",
                            now=now,
                        )
                    except Exception:
                        pass
                profile = state.quota_profile
                self._reconcile_expired_locked(state, now=now)
                if state.pacific_day != pacific_day(now):
                    state.pacific_day = pacific_day(now)
                    state.rpd_reserved = 0
                    state.rpd_dispatched = 0
                    state.rpd_uncertain = 0

                rolling = GeminiRequestAttempt.objects.filter(
                    project_identity=identity,
                    model=boundary.model,
                    provider_started_at__gte=now - dt.timedelta(seconds=60),
                    provider_started_at__lte=now,
                ).aggregate(
                    requests=Count("id"),
                    prompt_tokens=Sum(
                        Case(
                            When(prompt_tokens__gt=0, then=F("prompt_tokens")),
                            default=F("reserved_prompt_tokens"),
                            output_field=IntegerField(),
                        )
                    ),
                )
                requests_60 = int(rolling.get("requests") or 0)
                prompt_60 = int(rolling.get("prompt_tokens") or 0)
                active_block = self._active_provider_block(state.provider_blocks, now)
                if active_block:
                    shadow_decision = GeminiRequestAttempt.ShadowDecision.DENY
                    deny_reason = "provider_block"
                elif int(state.rpd_dispatched or 0) >= int(profile.rpd_limit):
                    shadow_decision = GeminiRequestAttempt.ShadowDecision.DENY
                    deny_reason = "rpd_exhausted"
                elif requests_60 >= int(profile.rpm_limit):
                    shadow_decision = GeminiRequestAttempt.ShadowDecision.DENY
                    deny_reason = "rpm_exhausted"
                elif int(state.in_flight_count or 0) >= int(profile.permit_limit):
                    shadow_decision = GeminiRequestAttempt.ShadowDecision.DENY
                    deny_reason = "permit_exhausted"
                elif not inline_count and prompt_60 + estimated > int(profile.input_tpm_limit):
                    shadow_decision = GeminiRequestAttempt.ShadowDecision.DENY
                    deny_reason = "tpm_exhausted"
                elif inline_count or profile.estimator_version == "shadow-calibration-required":
                    shadow_decision = GeminiRequestAttempt.ShadowDecision.UNKNOWN
                    deny_reason = "estimator_uncalibrated"
                else:
                    shadow_decision = GeminiRequestAttempt.ShadowDecision.ALLOW

            if self._legacy_root_execution is not None:
                from management.services.ig_legacy_provider_execution import (
                    admit_legacy_provider_dispatch_locked,
                )

                reason = admit_legacy_provider_dispatch_locked(
                    graph, boundary, now=now,
                )
                if reason:
                    boundary.provider_block_reason = reason
                    return False
            elif graph.source_execution_key:
                if str(graph.source_execution_key).startswith("ig-recovery:"):
                    from management.services.ig_legacy_provider_execution import (
                        admit_legacy_provider_dispatch_locked,
                    )

                    admission = admit_legacy_provider_dispatch_locked
                else:
                    from management.services.ig_revision_provider_execution import (
                        admit_provider_dispatch_locked,
                    )

                    admission = admit_provider_dispatch_locked

                # Force a current ledger read after acquiring the source mutex.
                # Keep quota -> attempt ordering shared with settlement; no
                # source -> root-revision write is introduced here.
                reason = admission(graph, boundary, now=now)
                if reason:
                    boundary.provider_block_reason = reason
                    return False
            attempt = GeminiRequestAttempt.objects.create(
                request_id=self.request_id,
                request_graph=graph,
                role=self._role_for_graph(graph),
                key_name=boundary.key_name,
                project_group=identity,
                project_identity=identity,
                model=boundary.model,
                outcome="provider_started",
                fsm_state=GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                quota_profile=profile,
                accounting_mode="shadow",
                shadow_decision=shadow_decision,
                shadow_deny_reason=deny_reason[:32],
                logical_turn_id=graph.logical_turn_id,
                source_message_id=graph.source_message_id,
                client_id=graph.client_id,
                lane=graph.lane,
                attempt_index=boundary.attempt_index,
                candidate_index=boundary.candidate_index,
                recovery_job_id=graph.recovery_job_id,
                estimated_prompt_tokens=estimated,
                reserved_prompt_tokens=estimated,
                reserved_at=now,
                reservation_expires_at=expiry,
                provider_started_at=now,
                dispatch_pacific_day=pacific_day(now),
                permit_expires_at=expiry,
            )
            boundary.attempt_id = attempt.pk
            boundary.state_id = state.pk if state is not None else None
            boundary.started_monotonic = time.monotonic()

            if state is not None:
                next_expiry = (
                    min(state.next_permit_expiry_at, expiry)
                    if state.next_permit_expiry_at
                    else expiry
                )
                next_status = (
                    GeminiQuotaState.AccountingStatus.BLOCKED
                    if active_block
                    else GeminiQuotaState.AccountingStatus.AVAILABLE
                )
                GeminiQuotaState.objects.filter(pk=state.pk).update(
                    pacific_day=state.pacific_day,
                    rpd_reserved=state.rpd_reserved,
                    rpd_dispatched=int(state.rpd_dispatched or 0) + 1,
                    rpd_uncertain=state.rpd_uncertain,
                    in_flight_count=int(state.in_flight_count or 0) + 1,
                    next_permit_expiry_at=next_expiry,
                    accounting_status=next_status,
                    revision=F("revision") + 1,
                    updated_at=now,
                )

            if graph.provider_phase_started_at is None:
                GeminiRequest.objects.filter(
                    pk=graph.pk,
                    provider_phase_started_at__isnull=True,
                ).update(provider_phase_started_at=now, updated_at=now)
            return True

    def _cancel_pre_dispatch(self, boundary: AttemptBoundary, *, error=None) -> None:
        """Persist a local final-payload failure without any quota state spend."""
        if boundary.attempt_id:
            return
        from management.models import GeminiRequest, GeminiRequestAttempt

        now = timezone.now()
        identity = self._identity_for(boundary)
        classified = classify_failure(error, failure_kind="invalid_payload")
        with transaction.atomic():
            graph = self._lock_valid_boundary_graph(boundary)
            if graph is None:
                return
            admission_rejected = (
                type(error).__name__ == "_GeminiAdmissionRejected"
            )
            if admission_rejected:
                classified = classify_failure(
                    error,
                    failure_kind="stale_provider_boundary",
                )
            attempt = GeminiRequestAttempt.objects.create(
                request_id=self.request_id,
                request_graph=graph,
                role=self._role_for_graph(graph),
                key_name=boundary.key_name,
                project_group=identity,
                project_identity=identity,
                model=boundary.model,
                outcome="cancelled_pre_dispatch",
                fsm_state=GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
                accounting_mode="shadow",
                shadow_decision=GeminiRequestAttempt.ShadowDecision.UNKNOWN,
                shadow_deny_reason=(
                    "stale_boundary" if admission_rejected else "local_payload"
                ),
                failure_kind=classified["failure_kind"],
                http_code=classified["http_code"],
                provider_reason=classified["provider_reason"],
                decision=("policy_stop" if admission_rejected else "stop_payload"),
                logical_turn_id=graph.logical_turn_id,
                source_message_id=graph.source_message_id,
                client_id=graph.client_id,
                lane=graph.lane,
                attempt_index=boundary.attempt_index,
                candidate_index=boundary.candidate_index,
                recovery_job_id=graph.recovery_job_id,
                finished_at=now,
                settled_at=now,
                reservation_released_at=now,
                permit_released_at=now,
            )
            boundary.attempt_id = attempt.pk
            outcomes = dict(graph.candidate_outcomes or {})
            outcome_key = str(boundary.candidate_index or boundary.attempt_index)
            payload = {
                "attempt_index": boundary.attempt_index,
                "outcome": "cancelled_pre_dispatch",
                "failure_kind": classified["failure_kind"],
            }
            existing = outcomes.get(outcome_key)
            if existing is None:
                outcomes[outcome_key] = payload
            elif isinstance(existing, list):
                outcomes[outcome_key] = [*existing, payload]
            else:
                outcomes[outcome_key] = [existing, payload]
            GeminiRequest.objects.filter(pk=graph.pk).update(
                candidate_outcomes=outcomes,
                updated_at=now,
            )

    @staticmethod
    def _role_for_graph(graph) -> str:
        if graph.lane in {"live", "recovery", "holding"}:
            return "chat"
        if graph.lane == "diagnostic":
            return "diagnostic"
        return "management" if graph.reasoning_task != "conversion_analysis" else "checker"

    @staticmethod
    def _active_provider_block(blocks, now) -> bool:
        for value in (blocks or {}).values():
            if not isinstance(value, dict):
                continue
            until = parse_effective_from(value.get("until"))
            if until and until > now:
                return True
        return False

    def _reconcile_expired_locked(self, state, *, now) -> None:
        from management.models import GeminiRequestAttempt

        stale = list(
            GeminiRequestAttempt.objects.select_for_update().filter(
                project_identity=state.project_identity,
                model=state.model,
                fsm_state__in=(
                    GeminiRequestAttempt.FsmState.RESERVED,
                    GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                ),
                permit_expires_at__lte=now,
            )[:50]
        )
        if not stale:
            return
        uncertain = 0
        for row in stale:
            if row.fsm_state == GeminiRequestAttempt.FsmState.PROVIDER_STARTED:
                row.fsm_state = GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS
                row.outcome = "timeout_ambiguous"
                row.failure_kind = "stale_provider_boundary"
                if row.dispatch_pacific_day == state.pacific_day:
                    uncertain += 1
            else:
                row.fsm_state = GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH
                row.outcome = "cancelled_pre_dispatch"
                row.reservation_released_at = now
            row.finished_at = now
            row.settled_at = now
            row.permit_released_at = now
            row.save(update_fields=[
                "fsm_state", "outcome", "failure_kind", "finished_at",
                "settled_at", "reservation_released_at", "permit_released_at",
            ])
        if uncertain:
            state.rpd_uncertain = int(state.rpd_uncertain or 0) + uncertain
        active = GeminiRequestAttempt.objects.filter(
            project_identity=state.project_identity,
            model=state.model,
            fsm_state__in=(
                GeminiRequestAttempt.FsmState.RESERVED,
                GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
            ),
        ).aggregate(count=Count("id"), next_expiry=Min("permit_expires_at"))
        state.in_flight_count = int(active.get("count") or 0)
        state.next_permit_expiry_at = active.get("next_expiry")

    def _finish(
        self,
        boundary: AttemptBoundary,
        *,
        succeeded: bool,
        error=None,
        usage=None,
        http_code: int | None = None,
        failure_kind: str = "",
    ) -> None:
        last_error = None
        for delay in DB_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                result = self._finish_once(
                    boundary,
                    succeeded=succeeded,
                    error=error,
                    usage=usage,
                    http_code=http_code,
                    failure_kind=failure_kind,
                )
                if succeeded:
                    self.record_remaining("winner_found")
                return result
            except OperationalError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error

    def _finish_once(
        self,
        boundary: AttemptBoundary,
        *,
        succeeded: bool,
        error=None,
        usage=None,
        http_code: int | None = None,
        failure_kind: str = "",
    ) -> None:
        if not boundary.attempt_id:
            return
        from management.models import GeminiQuotaState, GeminiRequest, GeminiRequestAttempt

        now = timezone.now()
        usage = usage if isinstance(usage, dict) else {}
        latency_ms = max(
            0,
            int((time.monotonic() - boundary.started_monotonic) * 1000)
            if boundary.started_monotonic is not None
            else 0,
        )
        classified = classify_failure(error, http_code=http_code, failure_kind=failure_kind)
        with transaction.atomic():
            # Use the same lock order as final provider admission:
            # message -> canonical graph -> quota state -> attempt.
            graph, _locked_message = self._lock_canonical_graph()
            if graph is None:
                return
            state = None
            if boundary.state_id:
                state = GeminiQuotaState.objects.select_for_update().get(
                    pk=boundary.state_id
                )
            attempt = GeminiRequestAttempt.objects.select_for_update().get(
                pk=boundary.attempt_id
            )
            permit_was_active = attempt.permit_released_at is None
            late_success = bool(
                succeeded
                and attempt.fsm_state == GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS
            )
            if attempt.fsm_state in {
                GeminiRequestAttempt.FsmState.SUCCEEDED,
                GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
                GeminiRequestAttempt.FsmState.FAILED,
                GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
                GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
            } and not late_success:
                return

            if succeeded:
                attempt.fsm_state = (
                    GeminiRequestAttempt.FsmState.SUCCEEDED_LATE
                    if late_success
                    else GeminiRequestAttempt.FsmState.SUCCEEDED
                )
                attempt.outcome = "succeeded"
                attempt.failure_kind = ""
                attempt.http_code = 200
                attempt.provider_reason = ""
            else:
                attempt.fsm_state = (
                    GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS
                    if classified["timeout_ambiguous"]
                    else GeminiRequestAttempt.FsmState.FAILED
                )
                attempt.outcome = (
                    "timeout_ambiguous" if classified["timeout_ambiguous"] else "failed"
                )
                attempt.failure_kind = classified["failure_kind"]
                attempt.http_code = classified["http_code"]
                attempt.provider_reason = classified["provider_reason"]
                attempt.provider_quota_metric = classified["quota_metric"]
                attempt.provider_quota_id = classified["quota_id"]
                attempt.provider_quota_dimensions = classified["quota_dimensions"]
                attempt.provider_retry_after_seconds = classified["retry_after_seconds"]
                attempt.provider_block_until = classified["block_until"]
            attempt.prompt_tokens = _usage_int(usage, "promptTokenCount", "prompt_token_count")
            attempt.thoughts_tokens = _usage_int(usage, "thoughtsTokenCount", "thoughts_token_count")
            attempt.candidates_tokens = _usage_int(usage, "candidatesTokenCount", "candidates_token_count")
            attempt.total_tokens = _usage_int(usage, "totalTokenCount", "total_token_count")
            attempt.latency_ms = latency_ms
            attempt.finished_at = now
            attempt.settled_at = now
            if permit_was_active:
                attempt.permit_released_at = now
            attempt.save(update_fields=[
                "fsm_state", "outcome", "failure_kind", "http_code",
                "provider_reason", "provider_quota_metric", "provider_quota_id",
                "provider_quota_dimensions", "provider_retry_after_seconds",
                "provider_block_until", "prompt_tokens", "thoughts_tokens",
                "candidates_tokens", "total_tokens", "latency_ms", "finished_at",
                "settled_at", "permit_released_at",
            ])

            if state is not None:
                if permit_was_active:
                    state.in_flight_count = max(
                        0, int(state.in_flight_count or 0) - 1
                    )
                if succeeded:
                    state.last_success_at = now
                    state.last_latency_ms = latency_ms
                    state.latency_ewma_ms = (
                        latency_ms
                        if not state.latency_ewma_ms
                        else int(state.latency_ewma_ms * 0.7 + latency_ms * 0.3)
                    )
                    same_dispatch_day = (
                        attempt.dispatch_pacific_day == state.pacific_day
                    )
                    if late_success and same_dispatch_day and state.rpd_uncertain:
                        state.rpd_uncertain = max(0, int(state.rpd_uncertain) - 1)
                else:
                    state.last_failure_at = now
                    state.last_failure_kind = classified["failure_kind"]
                    state.last_http_code = classified["http_code"]
                    state.last_latency_ms = latency_ms
                    if (
                        classified["timeout_ambiguous"]
                        and attempt.dispatch_pacific_day == state.pacific_day
                    ):
                        state.rpd_uncertain = int(state.rpd_uncertain or 0) + 1
                    if classified["http_code"] == 429:
                        metric_key = classified["quota_metric"] or "unknown"
                        blocks = dict(state.provider_blocks or {})
                        blocks[metric_key] = {
                            "quota_id": classified["quota_id"],
                            "dimensions": classified["quota_dimensions"],
                            "retry_after_seconds": classified["retry_after_seconds"],
                            "until": (
                                classified["block_until"].isoformat()
                                if classified["block_until"]
                                else ""
                            ),
                        }
                        state.provider_blocks = blocks
                        state.external_usage_suspected = True
                        state.accounting_status = GeminiQuotaState.AccountingStatus.BLOCKED
                    elif not succeeded:
                        state.accounting_status = GeminiQuotaState.AccountingStatus.DEGRADED
                active = GeminiRequestAttempt.objects.filter(
                    project_identity=state.project_identity,
                    model=state.model,
                    fsm_state__in=(
                        GeminiRequestAttempt.FsmState.RESERVED,
                        GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                    ),
                ).aggregate(next_expiry=Min("permit_expires_at"))
                state.next_permit_expiry_at = active.get("next_expiry")
                state.revision = F("revision") + 1
                state.save()

            outcomes = dict(graph.candidate_outcomes or {})
            outcome_key = str(boundary.candidate_index or boundary.attempt_index)
            outcome_payload = {
                "attempt_index": boundary.attempt_index,
                "outcome": attempt.outcome,
                "failure_kind": attempt.failure_kind,
            }
            existing_outcome = outcomes.get(outcome_key)
            if existing_outcome is None:
                outcomes[outcome_key] = outcome_payload
            elif isinstance(existing_outcome, list):
                outcomes[outcome_key] = [*existing_outcome, outcome_payload]
            else:
                outcomes[outcome_key] = [existing_outcome, outcome_payload]
            graph.candidate_outcomes = outcomes
            if succeeded and graph.winner_attempt_id is None:
                graph.winner_attempt = attempt
                graph.terminal_resolution = "succeeded"
                graph.terminal_reason = "provider_success"
                graph.resolved_at = now
                attempt.winner_claimed = True
                winner_fields = ["winner_claimed"]
                if graph.reply_message_id:
                    attempt.reply_message_id = graph.reply_message_id
                    winner_fields.append("reply_message_id")
                attempt.save(update_fields=winner_fields)
            graph.save(update_fields=[
                "candidate_outcomes", "winner_attempt", "terminal_resolution",
                "terminal_reason", "resolved_at", "updated_at",
            ])

    def resolve_failure(self, reason="exhausted") -> None:
        try:
            from management.models import GeminiRequest

            now = timezone.now()
            self.record_remaining(reason)
            GeminiRequest.objects.filter(
                pk=self.graph_id,
                terminal_resolution="",
            ).update(
                terminal_resolution="failed",
                terminal_reason=_safe_reason(reason)[:48] or "exhausted",
                resolved_at=now,
                updated_at=now,
            )
        except Exception:
            return None


def _record_persisted_plan_remainder_locked(graph, *, reason: str, now) -> int:
    """Close missing candidates when only the sanitized graph survived a crash.

    Environment aliases are intentionally absent from ``candidate_plan``.  A
    reconciled not-attempted row therefore keeps ``key_name`` blank and uses the
    durable project identity/model pair; it never guesses an alias or secret.
    The caller must hold the request-graph row lock.
    """
    from management.models import GeminiRequestAttempt

    bounded_default = _safe_reason(reason)[:24] or "expired_reconcile"
    existing_rows = list(
        GeminiRequestAttempt.objects.filter(request_graph=graph).values_list(
            "candidate_index", "attempt_index"
        )
    )
    observed = {
        int(candidate_index)
        for candidate_index, _attempt_index in existing_rows
        if int(candidate_index or 0) > 0
    }
    next_attempt_index = max(
        (int(attempt_index or 0) for _candidate_index, attempt_index in existing_rows),
        default=0,
    )
    outcomes = dict(graph.candidate_outcomes or {})
    rows = []
    for position, planned in enumerate(graph.candidate_plan or (), start=1):
        if not isinstance(planned, dict):
            continue
        try:
            candidate_index = max(
                1, int(planned.get("candidate_index") or position)
            )
        except (TypeError, ValueError):
            candidate_index = position
        if candidate_index in observed:
            continue
        model = _safe_token(planned.get("model"), limit=80)
        if not model:
            continue
        identity = _safe_token(planned.get("project_identity"), limit=80)
        candidate_reason = (
            _safe_reason(planned.get("initial_skip_reason"))[:24]
            or bounded_default
        )
        next_attempt_index += 1
        rows.append(GeminiRequestAttempt(
            request_id=graph.request_id,
            request_graph=graph,
            role=RequestObserver._role_for_graph(graph),
            key_name="",
            project_group=identity,
            project_identity=identity,
            model=model,
            outcome="not_attempted",
            fsm_state=GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
            accounting_mode=str(graph.accounting_mode or "shadow"),
            shadow_decision=GeminiRequestAttempt.ShadowDecision.UNKNOWN,
            shadow_deny_reason="not_dispatched",
            decision="reconcile_expired",
            logical_turn_id=graph.logical_turn_id,
            source_message_id=graph.source_message_id,
            client_id=graph.client_id,
            lane=graph.lane,
            attempt_index=next_attempt_index,
            candidate_index=candidate_index,
            not_attempted_reason=candidate_reason,
            recovery_job_id=graph.recovery_job_id,
            finished_at=now,
            settled_at=now,
            reservation_released_at=now,
            permit_released_at=now,
        ))
        outcomes[str(candidate_index)] = {
            "attempt_index": next_attempt_index,
            "outcome": "not_attempted",
            "failure_kind": "",
            "reason": candidate_reason,
        }
        observed.add(candidate_index)
    if rows:
        GeminiRequestAttempt.objects.bulk_create(rows)
        graph.candidate_outcomes = outcomes
    return len(rows)


def reconcile_expired_request_graphs(*, now=None, limit: int = 100) -> int:
    """Idempotently terminalize expired graphs that have no active boundary.

    A ``provider_started``/``reserved`` row is deliberately left untouched:
    its quota/permit state must first be reconciled by the pair-level owner.
    Already terminal timeout/failure rows remain conservative spend, while all
    never-dispatched candidates receive bounded ``not_attempted`` evidence.
    """
    from management.models import GeminiRequest, GeminiRequestAttempt

    now = now or timezone.now()
    if not shadow_runtime_active(now=now):
        return 0
    try:
        bounded_limit = min(500, max(1, int(limit or 100)))
    except (TypeError, ValueError, OverflowError):
        bounded_limit = 100
    graph_ids = list(
        GeminiRequest.objects.filter(
            accounting_mode="shadow",
            terminal_resolution="",
            deadline_at__isnull=False,
            deadline_at__lte=now,
        )
        .order_by("deadline_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )
    reconciled = 0
    for graph_id in graph_ids:
        try:
            with transaction.atomic():
                graph = (
                    GeminiRequest.objects.select_for_update()
                    .filter(pk=graph_id)
                    .first()
                )
                if (
                    graph is None
                    or graph.terminal_resolution
                    or graph.winner_attempt_id is not None
                    or graph.deadline_at is None
                    or graph.deadline_at > now
                ):
                    continue
                if GeminiRequestAttempt.objects.select_for_update().filter(
                    request_graph=graph,
                    fsm_state__in=(
                        GeminiRequestAttempt.FsmState.RESERVED,
                        GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                    ),
                ).exists():
                    continue
                success_like = list(
                    GeminiRequestAttempt.objects.select_for_update()
                    .filter(
                        request_graph=graph,
                        fsm_state__in=(
                            GeminiRequestAttempt.FsmState.SUCCEEDED,
                            GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
                        ),
                    )
                    .order_by("attempt_index", "id")[:3]
                )
                trustworthy_successes = [
                    attempt
                    for attempt in success_like
                    if attempt.outcome == "succeeded"
                    and attempt.provider_started_at is not None
                    and attempt.settled_at is not None
                    and attempt.permit_released_at is not None
                ]
                claimed = [
                    attempt
                    for attempt in trustworthy_successes
                    if attempt.winner_claimed
                ]
                reply_matched = [
                    attempt
                    for attempt in trustworthy_successes
                    if graph.reply_message_id
                    and attempt.reply_message_id == graph.reply_message_id
                ]
                claimed_ids = {attempt.pk for attempt in claimed}
                reply_ids = {attempt.pk for attempt in reply_matched}
                evidence_conflicts = bool(
                    len(claimed_ids) > 1
                    or len(reply_ids) > 1
                    or (
                        claimed_ids
                        and reply_ids
                        and claimed_ids != reply_ids
                    )
                )
                if evidence_conflicts:
                    continue
                if len(claimed_ids) == 1:
                    succeeded_attempt = claimed[0]
                elif len(reply_ids) == 1:
                    succeeded_attempt = reply_matched[0]
                elif len(trustworthy_successes) == 1 and len(success_like) == 1:
                    succeeded_attempt = trustworthy_successes[0]
                elif success_like:
                    # More than one success, or incomplete success evidence,
                    # cannot be repaired by guessing a winner.  Preserve the
                    # graph for the dedicated success/receipt reconciler.
                    continue
                else:
                    succeeded_attempt = None

                planned_rows = list(
                    GeminiRequestAttempt.objects.select_for_update().filter(
                        request_graph=graph,
                        fsm_state=GeminiRequestAttempt.FsmState.PLANNED,
                    )
                )
                if planned_rows:
                    outcomes = dict(graph.candidate_outcomes or {})
                    for attempt in planned_rows:
                        attempt.fsm_state = (
                            GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH
                        )
                        attempt.outcome = "cancelled_pre_dispatch"
                        attempt.shadow_deny_reason = "not_dispatched"
                        attempt.decision = "reconcile_expired"
                        attempt.not_attempted_reason = "expired_reconcile"
                        attempt.finished_at = now
                        attempt.settled_at = now
                        attempt.reservation_released_at = now
                        attempt.permit_released_at = now
                        attempt.save(update_fields=[
                            "fsm_state",
                            "outcome",
                            "shadow_deny_reason",
                            "decision",
                            "not_attempted_reason",
                            "finished_at",
                            "settled_at",
                            "reservation_released_at",
                            "permit_released_at",
                        ])
                        outcomes[str(
                            attempt.candidate_index or attempt.attempt_index
                        )] = {
                            "attempt_index": attempt.attempt_index,
                            "outcome": "cancelled_pre_dispatch",
                            "failure_kind": "",
                            "reason": "expired_reconcile",
                        }
                    graph.candidate_outcomes = outcomes
                if succeeded_attempt is not None:
                    outcomes = dict(graph.candidate_outcomes or {})
                    outcome_key = str(
                        succeeded_attempt.candidate_index
                        or succeeded_attempt.attempt_index
                    )
                    success_payload = {
                        "attempt_index": succeeded_attempt.attempt_index,
                        "outcome": "succeeded",
                        "failure_kind": "",
                    }
                    existing_outcome = outcomes.get(outcome_key)
                    existing_items = (
                        existing_outcome
                        if isinstance(existing_outcome, list)
                        else [existing_outcome]
                    )
                    if not any(
                        isinstance(item, dict)
                        and item.get("attempt_index")
                        == succeeded_attempt.attempt_index
                        and item.get("outcome") == "succeeded"
                        for item in existing_items
                    ):
                        if existing_outcome is None:
                            outcomes[outcome_key] = success_payload
                        elif isinstance(existing_outcome, list):
                            outcomes[outcome_key] = [
                                *existing_outcome,
                                success_payload,
                            ]
                        else:
                            outcomes[outcome_key] = [
                                existing_outcome,
                                success_payload,
                            ]
                    graph.candidate_outcomes = outcomes
                    _record_persisted_plan_remainder_locked(
                        graph,
                        reason="winner_found",
                        now=now,
                    )
                    winner_updates = ["winner_claimed"]
                    succeeded_attempt.winner_claimed = True
                    if graph.reply_message_id:
                        succeeded_attempt.reply_message_id = graph.reply_message_id
                        winner_updates.append("reply_message_id")
                    succeeded_attempt.save(update_fields=winner_updates)
                    graph.winner_attempt = succeeded_attempt
                    graph.terminal_resolution = "succeeded"
                    graph.terminal_reason = "reconciled_success_evidence"
                else:
                    _record_persisted_plan_remainder_locked(
                        graph,
                        reason="expired_reconcile",
                        now=now,
                    )
                    graph.terminal_resolution = "failed"
                    graph.terminal_reason = "expired_reconcile"
                graph.resolved_at = now
                graph.save(update_fields=[
                    "candidate_outcomes",
                    "winner_attempt",
                    "terminal_resolution",
                    "terminal_reason",
                    "resolved_at",
                    "updated_at",
                ])
                reconciled += 1
        except (OperationalError, IntegrityError):
            # Another worker may have settled the graph between the bounded id
            # scan and row lock.  The next pass is safe and idempotent.
            continue
    return reconciled


def models_Q_effective(now):
    """Kept as a helper so profile selection remains easy to unit-test."""
    from django.db.models import Q

    return Q(effective_until__isnull=True) | Q(effective_until__gt=now)


def _usage_int(usage: dict, *names: str) -> int:
    for name in names:
        try:
            value = int(usage.get(name) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if value >= 0:
            return value
    return 0


def classify_failure(error, *, http_code=None, failure_kind="") -> dict:
    """Translate typed gateway failures to bounded V2 fields."""
    detail = str(error or "")
    name = type(error).__name__ if error is not None else ""
    code = int(http_code) if http_code else int(getattr(error, "http_code", 0) or 0) or None
    kind = _safe_reason(failure_kind)
    if not kind:
        lowered = detail.casefold()
        if name == "_Gemini429" or code == 429:
            kind, code = "quota_429", 429
        elif name == "_GeminiModelUnavailable":
            kind = "model_not_found" if "404" in detail else "permission_denied"
            code = 404 if kind == "model_not_found" else 403
        elif name == "_GeminiEmpty":
            kind = "empty"
        elif name == "_GeminiResultInvalid":
            kind = "invalid_response"
        elif name == "_GeminiAdmissionRejected":
            kind = "stale_provider_boundary"
        elif name == "_GeminiFatal":
            kind = "invalid_key" if "API_KEY_INVALID" in detail.upper() or "HTTP 401" in detail.upper() else "invalid_payload"
        elif name == "_GeminiTransient":
            if lowered.startswith("timeout:"):
                kind = "read_timeout"
            elif lowered.startswith("transport:"):
                kind = "transport"
            elif "невалідний json" in lowered or "invalid json" in lowered:
                kind = "invalid_response"
            elif "http 408" in lowered:
                kind, code = "http_408", 408
            else:
                match = re.search(r"HTTP\s+(5\d\d)", detail, re.I)
                kind = "http_5xx" if match else "transport"
                code = int(match.group(1)) if match else code
        else:
            kind = "provider_error"
    timeout_ambiguous = kind in {"read_timeout", "transport", "http_408"}
    provider_reason = _safe_token(getattr(error, "provider_reason", ""), limit=80)
    quota_metric = _safe_token(getattr(error, "provider_quota_metric", ""), limit=16)
    quota_id = _safe_token(getattr(error, "provider_quota_id", ""), limit=120)
    dimensions = getattr(error, "provider_quota_dimensions", {})
    if not isinstance(dimensions, dict):
        dimensions = {}
    allowed_dimension_names = {"model", "location", "region", "tier"}
    safe_dimensions = {
        _safe_token(key, limit=40): _safe_token(value, limit=80)
        for key, value in list(dimensions.items())[:8]
        if str(key).casefold() in allowed_dimension_names
        and _safe_token(key, limit=40)
        and _safe_token(value, limit=80)
    }
    retry = max(0, int(getattr(error, "retry_after_seconds", 0) or 0))
    block_until = None
    if code == 429:
        now = timezone.now()
        scope = str(getattr(error, "scope", "") or "")
        if not retry and scope == "minute":
            from management.services import gemini_keys

            retry = gemini_keys.DEFAULT_MINUTE_COOLDOWN
        elif not retry and scope == "topup":
            from management.services import gemini_keys

            retry = gemini_keys.TOPUP_COOLDOWN_SECONDS
        if scope in {"day", "unknown"}:
            local = now.astimezone(PT)
            tomorrow = local.date() + dt.timedelta(days=1)
            block_until = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=PT).astimezone(dt.timezone.utc)
        elif retry:
            block_until = now + dt.timedelta(seconds=retry)
    return {
        "failure_kind": kind[:32],
        "http_code": code,
        "provider_reason": provider_reason,
        "quota_metric": quota_metric,
        "quota_id": quota_id,
        "quota_dimensions": safe_dimensions,
        "retry_after_seconds": retry,
        "block_until": block_until,
        "timeout_ambiguous": timeout_ambiguous,
    }


def link_reply_if_present(*, request_id: str, reply_message_id: int) -> bool:
    """Atomically attach null->id or the same id; audit every conflict."""
    if not shadow_runtime_active() or not request_id or not reply_message_id:
        return False
    try:
        from management.models import AdminAuditLog, GeminiRequest, GeminiRequestAttempt

        bounded_request_id = str(request_id)[:40]
        requested_reply_id = int(reply_message_id)
        with transaction.atomic():
            # Graph identity and winner are locked before the attempt, matching
            # provider admission/settlement and avoiding attempt -> graph
            # inversion with a concurrent winner settlement.
            graph = (
                GeminiRequest.objects.select_for_update()
                .filter(request_id=bounded_request_id)
                .first()
            )
            if graph is None:
                return False
            winner = (
                GeminiRequestAttempt.objects.select_for_update().filter(
                    pk=graph.winner_attempt_id
                ).first()
                if graph.winner_attempt_id
                else None
            )
            existing_graph_id = int(graph.reply_message_id or 0)
            existing_attempt_id = int(
                getattr(winner, "reply_message_id", 0) or 0
            )
            conflict = next(
                (
                    value
                    for value in (existing_graph_id, existing_attempt_id)
                    if value and value != requested_reply_id
                ),
                0,
            )
            if conflict:
                AdminAuditLog.objects.create(
                    actor=None,
                    actor_role="system",
                    action="ig_gemini.reply_link_conflict",
                    entity_type="GeminiRequest",
                    entity_id=str(graph.pk),
                    before={"reply_message_id": conflict},
                    after={"requested_reply_message_id": requested_reply_id},
                    reason="immutable_reply_link_conflict",
                )
                return False
            now = timezone.now()
            if not existing_graph_id:
                GeminiRequest.objects.filter(pk=graph.pk).update(
                    reply_message_id=requested_reply_id,
                    updated_at=now,
                )
            if winner is not None and not existing_attempt_id:
                GeminiRequestAttempt.objects.filter(pk=winner.pk).update(
                    reply_message_id=requested_reply_id
                )
            return True
        return False
    except Exception:
        return False
