"""Authoritative, provider-free integrity boundary for Gemini accounting V2.

This module performs no routing, quota admission, provider I/O or dual-write.
It exists in S3a so every future S3b writer has one tested boundary for
cross-table invariants that MariaDB CHECK constraints cannot express.
"""
from __future__ import annotations

import hashlib
import json
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone


REQUEST_IMMUTABLE_FIELDS = (
    "request_id",
    "parent_request_id",
    "lane",
    "task_class",
    "reasoning_task",
    "logical_turn_id",
    "source_message_id",
    "source_execution_key",
    "client_id",
    "recovery_job_id",
    "routing_policy_version",
    "accounting_policy_version",
    "quota_profile_version",
    "authority_snapshot_version",
    "routing_mode",
    "commercial_risk",
    "requires_media_reasoning",
    "candidate_plan",
    "candidate_plan_digest",
    "policy_manifest",
    "deadline_ms",
    "deadline_at",
    "accounting_mode",
    "created_at",
)
REQUEST_MUTABLE_FIELDS = frozenset({
    "candidate_outcomes",
    "reply_message_id",
    "terminal_resolution",
    "terminal_reason",
    "provider_phase_started_at",
    "resolved_at",
    "updated_at",
})
ATTEMPT_IMMUTABLE_FIELDS = (
    "request_graph_id",
    "request_id",
    "role",
    "key_name",
    "project_group",
    "project_identity",
    "model",
    "quota_profile_id",
    "accounting_mode",
    "logical_turn_id",
    "source_message_id",
    "client_id",
    "lane",
    "attempt_index",
    "candidate_index",
    "incident_id",
    "recovery_job_id",
    "created_at",
)
ATTEMPT_MUTABLE_FIELDS = frozenset({
    "fsm_state",
    "outcome",
    "shadow_decision",
    "shadow_deny_reason",
    "failure_kind",
    "http_code",
    "provider_reason",
    "decision",
    "latency_ms",
    "remaining_deadline_ms",
    "prompt_tokens",
    "thoughts_tokens",
    "candidates_tokens",
    "total_tokens",
    "estimated_prompt_tokens",
    "reserved_prompt_tokens",
    "error_detail",
    "not_attempted_reason",
    "reply_message_id",
    "reserved_at",
    "reservation_expires_at",
    "provider_started_at",
    "dispatch_pacific_day",
    "permit_expires_at",
    "finished_at",
    "settled_at",
    "reservation_released_at",
    "permit_released_at",
    "provider_quota_metric",
    "provider_quota_id",
    "provider_quota_dimensions",
    "provider_retry_after_seconds",
    "provider_block_until",
    "winner_claimed",
})


class RequestPolicyManifestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_MANIFEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MANIFEST_REASON_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_MANIFEST_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_MANIFEST_TOP_KEYS = frozenset({
    "version", "content_hash", "selected_ids", "omitted", "mandatory_ids",
    "budget_chars", "visual_trigger_codes", "core", "knowledge_hash",
    "instruction_publication", "instruction_selection",
})


def _manifest_object(value, *, keys, code: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise RequestPolicyManifestError(code, "policy manifest object shape is invalid")
    return value


def _manifest_token(value, *, code: str, maximum=128) -> str:
    token = str(value or "")
    if len(token) > maximum or not _MANIFEST_ID_RE.fullmatch(token):
        raise RequestPolicyManifestError(code, "policy manifest identifier is invalid")
    return token


def _manifest_hash(value, *, code: str) -> str:
    digest = str(value or "").casefold()
    if not _MANIFEST_HASH_RE.fullmatch(digest):
        raise RequestPolicyManifestError(code, "policy manifest hash is invalid")
    return digest


def _manifest_positive_int(value, *, code: str, allow_zero=False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestPolicyManifestError(code, "policy manifest count is invalid")
    minimum = 0 if allow_zero else 1
    if value < minimum or value > 1_000_000:
        raise RequestPolicyManifestError(code, "policy manifest count is outside bounds")
    return value


def _manifest_ids(values, *, code: str, maximum=100) -> list[str]:
    if not isinstance(values, list) or len(values) > maximum:
        raise RequestPolicyManifestError(code, "policy manifest identifier list is invalid")
    result = [_manifest_token(value, code=code) for value in values]
    if len(result) != len(set(result)):
        raise RequestPolicyManifestError(code, "policy manifest identifiers must be unique")
    return result


def _manifest_omissions(values, *, code: str) -> list[dict]:
    if not isinstance(values, list) or len(values) > 100:
        raise RequestPolicyManifestError(code, "policy manifest omissions are invalid")
    result = []
    for raw in values:
        item = _manifest_object(raw, keys={"id", "reason"}, code=code)
        module_id = _manifest_token(item["id"], code=code)
        reason = str(item["reason"] or "").casefold()
        if not _MANIFEST_REASON_RE.fullmatch(reason):
            raise RequestPolicyManifestError(code, "policy manifest omission is invalid")
        result.append({"id": module_id, "reason": reason})
    return result


def sanitize_request_policy_manifest(value) -> dict:
    """Accept only content-free compiler/publication metadata."""
    if value in (None, {}):
        return {}
    manifest = _manifest_object(
        value,
        keys=_MANIFEST_TOP_KEYS,
        code="policy_manifest_invalid",
    )
    core = _manifest_object(
        manifest["core"],
        keys={"version", "prompt_hash", "directives_hash"},
        code="policy_manifest_core_invalid",
    )
    publication = _manifest_object(
        manifest["instruction_publication"],
        keys={"id", "version", "hash", "compiler_version"},
        code="policy_manifest_publication_invalid",
    )
    selection = _manifest_object(
        manifest["instruction_selection"],
        keys={
            "selected_ids", "omitted", "visual_trigger_codes",
            "publication_id", "publication_version", "publication_hash",
            "publication_compiler_version",
        },
        code="policy_manifest_selection_invalid",
    )
    safe_publication = {
        "id": _manifest_positive_int(
            publication["id"], code="policy_manifest_publication_invalid"
        ),
        "version": _manifest_positive_int(
            publication["version"], code="policy_manifest_publication_invalid"
        ),
        "hash": _manifest_hash(
            publication["hash"], code="policy_manifest_publication_invalid"
        ),
        "compiler_version": _manifest_token(
            publication["compiler_version"],
            code="policy_manifest_publication_invalid",
            maximum=64,
        ),
    }
    safe_selection = {
        "selected_ids": _manifest_ids(
            selection["selected_ids"], code="policy_manifest_selection_invalid"
        ),
        "omitted": _manifest_omissions(
            selection["omitted"], code="policy_manifest_selection_invalid"
        ),
        "visual_trigger_codes": _manifest_ids(
            selection["visual_trigger_codes"],
            code="policy_manifest_selection_invalid",
            maximum=32,
        ),
        "publication_id": _manifest_positive_int(
            selection["publication_id"], code="policy_manifest_selection_invalid"
        ),
        "publication_version": _manifest_positive_int(
            selection["publication_version"], code="policy_manifest_selection_invalid"
        ),
        "publication_hash": _manifest_hash(
            selection["publication_hash"], code="policy_manifest_selection_invalid"
        ),
        "publication_compiler_version": _manifest_token(
            selection["publication_compiler_version"],
            code="policy_manifest_selection_invalid",
            maximum=64,
        ),
    }
    if (
        safe_selection["publication_id"] != safe_publication["id"]
        or safe_selection["publication_version"] != safe_publication["version"]
        or safe_selection["publication_hash"] != safe_publication["hash"]
        or safe_selection["publication_compiler_version"]
        != safe_publication["compiler_version"]
    ):
        raise RequestPolicyManifestError(
            "policy_manifest_mismatch",
            "instruction publication metadata does not match selection metadata",
        )
    return {
        "version": _manifest_token(
            manifest["version"], code="policy_manifest_invalid"
        ),
        "content_hash": _manifest_hash(
            manifest["content_hash"], code="policy_manifest_invalid"
        ),
        "selected_ids": _manifest_ids(
            manifest["selected_ids"], code="policy_manifest_invalid"
        ),
        "omitted": _manifest_omissions(
            manifest["omitted"], code="policy_manifest_invalid"
        ),
        "mandatory_ids": _manifest_ids(
            manifest["mandatory_ids"], code="policy_manifest_invalid"
        ),
        "budget_chars": _manifest_positive_int(
            manifest["budget_chars"], code="policy_manifest_invalid", allow_zero=True
        ),
        "visual_trigger_codes": _manifest_ids(
            manifest["visual_trigger_codes"],
            code="policy_manifest_invalid",
            maximum=32,
        ),
        "core": {
            "version": _manifest_token(
                core["version"], code="policy_manifest_core_invalid"
            ),
            "prompt_hash": _manifest_hash(
                core["prompt_hash"], code="policy_manifest_core_invalid"
            ),
            "directives_hash": _manifest_hash(
                core["directives_hash"], code="policy_manifest_core_invalid"
            ),
        },
        "knowledge_hash": _manifest_hash(
            manifest["knowledge_hash"], code="policy_manifest_invalid"
        ),
        "instruction_publication": safe_publication,
        "instruction_selection": safe_selection,
    }


def canonical_candidate_plan_digest(candidate_plan) -> str:
    """Return a deterministic digest without retaining plan payload elsewhere."""
    try:
        serialized = json.dumps(
            candidate_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError({"candidate_plan": "Candidate plan is not canonical JSON."}) from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_request_contract(request) -> None:
    """Validate immutable plan identity and a winner belonging to this graph."""
    errors: dict[str, str] = {}
    plan = request.candidate_plan
    if not isinstance(plan, list):
        errors["candidate_plan"] = "Candidate plan must be a list."
    elif plan:
        expected_digest = canonical_candidate_plan_digest(plan)
        if not request.candidate_plan_digest:
            errors["candidate_plan_digest"] = "Non-empty candidate plan requires a digest."
        elif request.candidate_plan_digest != expected_digest:
            errors["candidate_plan_digest"] = "Candidate plan digest does not match the plan."
    try:
        safe_manifest = sanitize_request_policy_manifest(request.policy_manifest)
        if request.policy_manifest != safe_manifest:
            errors["policy_manifest"] = "Policy manifest is not canonical."
    except RequestPolicyManifestError as exc:
        errors["policy_manifest"] = str(exc)

    if request.pk is not None:
        persisted = (
            type(request)._base_manager.filter(pk=request.pk)
            .values(*REQUEST_IMMUTABLE_FIELDS, "winner_attempt_id")
            .first()
        )
        if persisted is not None:
            for field in REQUEST_IMMUTABLE_FIELDS:
                if getattr(request, field) != persisted[field]:
                    errors[field] = "Gemini request routing/lineage identity is immutable."
            if (
                persisted["winner_attempt_id"] is not None
                and request.winner_attempt_id != persisted["winner_attempt_id"]
            ):
                errors["winner_attempt"] = "Gemini request winner is immutable once claimed."

    if request.winner_attempt_id is not None:
        if request.pk is None:
            errors["winner_attempt"] = "Winner cannot be attached before request creation."
        else:
            attempt = request.winner_attempt
            if attempt.request_graph_id != request.pk:
                errors["winner_attempt"] = "Winner attempt belongs to another request graph."
            elif attempt.request_id != request.request_id:
                errors["winner_attempt"] = "Winner attempt request id does not match its graph."

    if errors:
        raise ValidationError(errors)


def validate_attempt_contract(attempt) -> None:
    """Validate parent request identity and model/profile compatibility."""
    errors: dict[str, str] = {}
    if attempt.request_graph_id is not None:
        request = attempt.request_graph
        if attempt.request_id != request.request_id:
            errors["request_id"] = "Attempt request id does not match its request graph."
    if attempt.quota_profile_id is not None:
        profile = attempt.quota_profile
        if attempt.model != profile.model:
            errors["quota_profile"] = "Attempt model does not match quota profile model."
    if attempt.pk is not None:
        persisted = (
            type(attempt)._base_manager.filter(pk=attempt.pk)
            .values(*ATTEMPT_IMMUTABLE_FIELDS)
            .first()
        )
        if persisted is not None:
            for field in ATTEMPT_IMMUTABLE_FIELDS:
                if getattr(attempt, field) != persisted[field]:
                    errors[field] = "Gemini attempt project/candidate/lineage identity is immutable."
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def rotate_quota_state_profile(
    *,
    state_id: int,
    new_profile_id: int,
    expected_revision: int,
    actor=None,
    reason: str = "quota_profile_rotation",
    now=None,
):
    """Atomically rotate one idle pair to a current same-model profile.

    Generic model/queryset mutation remains blocked. This is the only S3a
    primitive allowed to change ``GeminiQuotaState.quota_profile``.
    """
    from management.models import (
        AdminAuditLog,
        GeminiQuotaProfile,
        GeminiQuotaState,
    )

    now = now or timezone.now()
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"expected_revision": "Expected revision is invalid."}) from exc
    state = (
        GeminiQuotaState.objects.select_for_update()
        .select_related("quota_profile")
        .get(pk=state_id)
    )
    new_profile = GeminiQuotaProfile.objects.get(pk=new_profile_id)
    if state.revision != expected_revision:
        raise ValidationError({"revision": "Gemini quota-state revision changed."})
    if state.in_flight_count:
        raise ValidationError({"in_flight_count": "Cannot rotate an in-flight quota state."})
    if new_profile.model != state.model:
        raise ValidationError({"quota_profile": "Quota profile model does not match state model."})
    if new_profile.effective_from > now or (
        new_profile.effective_until is not None
        and new_profile.effective_until <= now
    ):
        raise ValidationError({"quota_profile": "Quota profile is not currently effective."})
    if state.quota_profile_id == new_profile.pk:
        raise ValidationError({"quota_profile": "Quota state already uses this profile."})

    updated = GeminiQuotaState.objects.filter(
        pk=state.pk,
        revision=expected_revision,
        in_flight_count=0,
    )._rotate_profile(
        quota_profile_id=new_profile.pk,
        revision=F("revision") + 1,
        updated_at=now,
    )
    if updated != 1:
        raise ValidationError({"revision": "Gemini quota-state rotation lost its revision race."})
    audit = AdminAuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        actor_role="staff" if getattr(actor, "pk", None) else "system",
        action="ig_gemini.quota_profile_rotated",
        entity_type="GeminiQuotaState",
        entity_id=str(state.pk),
        before={
            "profile_version": state.quota_profile.profile_version,
            "model": state.model,
            "revision": expected_revision,
        },
        after={
            "profile_version": new_profile.profile_version,
            "model": state.model,
            "revision": expected_revision + 1,
        },
        reason=str(reason or "quota_profile_rotation")[:1000],
    )
    state.refresh_from_db()
    return state, audit


def rotate_locked_quota_state_profile(
    *,
    state,
    new_profile,
    reason: str,
    now=None,
):
    """Rotate an already select-for-update locked idle state without new reads."""
    from management.models import AdminAuditLog, GeminiQuotaState

    now = now or timezone.now()
    if state.in_flight_count:
        raise ValidationError({"in_flight_count": "Cannot rotate an in-flight quota state."})
    if new_profile.model != state.model:
        raise ValidationError({"quota_profile": "Quota profile model does not match state model."})
    if new_profile.effective_from > now or (
        new_profile.effective_until is not None
        and new_profile.effective_until <= now
    ):
        raise ValidationError({"quota_profile": "Quota profile is not currently effective."})
    if state.quota_profile_id == new_profile.pk:
        return state, None
    previous_version = state.quota_profile.profile_version
    expected_revision = int(state.revision)
    updated = GeminiQuotaState.objects.filter(
        pk=state.pk,
        revision=expected_revision,
        in_flight_count=0,
    )._rotate_profile(
        quota_profile_id=new_profile.pk,
        revision=F("revision") + 1,
        updated_at=now,
    )
    if updated != 1:
        raise ValidationError({"revision": "Gemini quota-state rotation lost its revision race."})
    audit = AdminAuditLog.objects.create(
        actor=None,
        actor_role="system",
        action="ig_gemini.quota_profile_rotated",
        entity_type="GeminiQuotaState",
        entity_id=str(state.pk),
        before={
            "profile_version": previous_version,
            "model": state.model,
            "revision": expected_revision,
        },
        after={
            "profile_version": new_profile.profile_version,
            "model": state.model,
            "revision": expected_revision + 1,
        },
        reason=str(reason or "shadow_effective_profile_transition")[:1000],
    )
    state.quota_profile = new_profile
    state.revision = expected_revision + 1
    return state, audit


def validate_quota_state_contract(state) -> None:
    """Validate the cross-table pair/profile model relation."""
    errors: dict[str, str] = {}
    if (
        state.quota_profile_id is not None
        and state.model != state.quota_profile.model
    ):
        errors["quota_profile"] = "Quota-state model does not match quota profile model."
    if state.pk is not None:
        persisted = (
            type(state)._base_manager.filter(pk=state.pk)
            .values("project_identity", "model", "quota_profile_id")
            .first()
        )
        if persisted is not None:
            for field in ("project_identity", "model", "quota_profile_id"):
                if getattr(state, field) != persisted[field]:
                    errors[field] = "Gemini quota-state identity/profile is immutable."
    if errors:
        raise ValidationError(errors)
