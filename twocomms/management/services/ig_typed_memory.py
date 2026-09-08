"""Provider-free, evidence-bound Typed Memory V2 shadow projection."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F, OuterRef, Subquery
from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationAnalysisJob,
    IgConversationAnalysisResult,
    IG_MEMORY_MAX_CHAIN_DEPTH,
    IgMemoryFact,
    IgMemoryFactEvidence,
    IgMemoryHead,
    InstagramBotMessage,
    InstagramBotSettings,
)


MODE_OFF = "off"
MODE_SHADOW = "shadow_compare"
SCHEMA_VERSION = "typed-memory.v1"
PROJECTOR_VERSION = "typed-memory-projector.v1"
RESULT_SCHEMA_VERSION = "analysis-v2.2"
MAX_EVIDENCE = 40
MAX_RECONCILE = 500
MAX_CHAIN_DEPTH = IG_MEMORY_MAX_CHAIN_DEPTH
_KEY_ID_RE = re.compile(r"^tmk_(?!.*[0-9]{7})[a-z0-9][a-z0-9_.-]{0,27}$")


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    scope: str
    episode_id: int | None
    line_id: str
    fact_key: str
    typed_value: dict
    confidence: Decimal | None
    evidence_ids: tuple[int, ...]
    valid_until: object | None
    sensitivity: str
    retention_class: str


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    status: str
    result_id: int | None = None
    created_facts: int = 0
    advanced_heads: int = 0
    unchanged_heads: int = 0


def configured_mode() -> str:
    value = str(getattr(settings, "IG_TYPED_MEMORY_MODE", MODE_OFF) or MODE_OFF)
    return value.strip().casefold() if value.strip().casefold() in {
        MODE_OFF, MODE_SHADOW,
    } else MODE_OFF


def _keyring_configuration() -> tuple[str, dict[str, bytes], tuple[str, ...]]:
    """Return an all-or-nothing signing configuration without exposing secrets.

    A malformed retained key is as dangerous as a malformed active key: historical
    rows may have been signed by any retained id.  Shadow therefore remains fully
    disabled until every entry is valid.
    """
    raw = getattr(settings, "IG_TYPED_MEMORY_HMAC_KEYRING", {})
    active_raw = getattr(settings, "IG_TYPED_MEMORY_HMAC_ACTIVE_KEY_ID", "")
    active = active_raw.strip() if isinstance(active_raw, str) else ""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return "", {}, ("keyring_not_object",)
    if (
        not isinstance(active_raw, str)
        or active_raw != active
        or not active
        or not _KEY_ID_RE.fullmatch(active)
    ):
        errors.append("active_key_id_invalid")
    ring: dict[str, bytes] = {}
    for key_id, secret in raw.items():
        if (
            not isinstance(key_id, str)
            or key_id != key_id.strip()
            or not _KEY_ID_RE.fullmatch(key_id)
        ):
            errors.append("retained_key_id_invalid")
            continue
        if not isinstance(secret, str):
            errors.append("retained_secret_invalid")
            continue
        encoded = secret.encode("utf-8")
        if not 32 <= len(encoded) <= 512:
            errors.append("retained_secret_invalid")
            continue
        ring[key_id] = encoded
    if active not in ring:
        errors.append("active_key_not_retained")
    if errors:
        return "", {}, tuple(sorted(set(errors)))
    return active, ring, ()


def _keyring() -> tuple[str, dict[str, bytes]]:
    active, ring, errors = _keyring_configuration()
    return (active, ring) if not errors else ("", {})


def shadow_enabled() -> bool:
    """Pure settings gate; off performs no database reads or writes."""
    if configured_mode() != MODE_SHADOW:
        return False
    if str(getattr(settings, "IG_ANALYSIS_V2_MODE", "off") or "off") != "shadow":
        return False
    if str(getattr(settings, "IG_ANALYSIS_MATERIALITY_MODE", "off") or "off") != "shadow":
        return False
    if not bool(getattr(settings, "IG_ANALYSIS_V2_EXTENDED_PROMPT", False)):
        return False
    active, ring = _keyring()
    return bool(active and ring)


def _canonical(payload) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
    ).encode("utf-8")


def _sha(payload) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _mac(domain: str, payload, *, key_id: str | None = None) -> tuple[str, str]:
    active, ring = _keyring()
    selected = str(key_id or active)
    secret = ring.get(selected)
    if not selected or secret is None:
        raise ValueError("typed-memory HMAC key is unavailable")
    digest = hmac.new(
        secret,
        domain.encode("ascii") + b"\0" + _canonical(payload),
        hashlib.sha256,
    ).hexdigest()
    return selected, digest


def _evidence_ids(result, claim_code: str) -> tuple[int, ...]:
    ids = sorted({
        int(row["message_id"])
        for row in result.evidence_manifest or ()
        if isinstance(row, dict)
        and row.get("source_role") == "user"
        and claim_code in (row.get("claim_codes") or ())
        and isinstance(row.get("message_id"), int)
        and row["message_id"] > 0
    })
    return tuple(ids[:MAX_EVIDENCE])


def candidates_from_result(result: IgConversationAnalysisResult) -> tuple[MemoryCandidate, ...]:
    """Map a validated Analysis V2 row to a closed, PII-free fact allowlist."""
    if result.result_schema_version != RESULT_SCHEMA_VERSION:
        return ()
    candidates: list[MemoryCandidate] = []
    language_ids = tuple(result.language_evidence_message_ids or ())
    if result.detected_language and language_ids:
        candidates.append(MemoryCandidate(
            scope=IgMemoryFact.Scope.CLIENT,
            episode_id=None,
            line_id="",
            fact_key=IgMemoryFact.FactKey.OBSERVED_LANGUAGE,
            typed_value={"code": result.detected_language},
            confidence=None,
            evidence_ids=language_ids,
            valid_until=None,
            sensitivity="low",
            retention_class="client",
        ))
    objection_ids = _evidence_ids(result, "objection")
    if result.active_objection_type and objection_ids and result.commercial_episode_id:
        line_id = str(result.line_id or "")[:96]
        candidates.append(MemoryCandidate(
            scope=(IgMemoryFact.Scope.LINE if line_id else IgMemoryFact.Scope.EPISODE),
            episode_id=result.commercial_episode_id,
            line_id=line_id,
            fact_key=IgMemoryFact.FactKey.OBJECTION_OBSERVED,
            typed_value={"type": result.active_objection_type},
            confidence=result.active_objection_confidence,
            evidence_ids=objection_ids,
            valid_until=None,
            sensitivity="personal_preference",
            retention_class="episode",
        ))
    deferred_ids = _evidence_ids(result, "deferred_intent")
    if (
        result.deferred_kind != result.DeferredKind.NONE
        and deferred_ids
        and result.commercial_episode_id
    ):
        candidates.append(MemoryCandidate(
            scope=IgMemoryFact.Scope.EPISODE,
            episode_id=result.commercial_episode_id,
            line_id="",
            fact_key=IgMemoryFact.FactKey.DEFERRED_INTENT,
            typed_value={
                "kind": result.deferred_kind,
                "condition_code": result.deferred_condition_code,
            },
            confidence=None,
            evidence_ids=deferred_ids,
            valid_until=result.deferred_until,
            sensitivity="personal_preference",
            retention_class=("until_date" if result.deferred_until else "episode"),
        ))
    return tuple(candidates)


def _slot_payload(result, candidate: MemoryCandidate) -> dict:
    return {
        "client_id": result.client_id,
        "scope": candidate.scope,
        "episode_id": candidate.episode_id or 0,
        "line_id": candidate.line_id,
        "order_id": 0,
        "case_id": 0,
        "fact_key": candidate.fact_key,
        "schema_version": SCHEMA_VERSION,
    }


def _fact_payload(result, candidate, *, slot_key, supersedes_id, key_id) -> dict:
    values = {
        "record_key": "",
        "slot_key": slot_key,
        "client_id": result.client_id,
        "scope": candidate.scope,
        "commercial_episode_id": candidate.episode_id,
        "line_id": candidate.line_id,
        "order_id": None,
        "post_sale_case_id": None,
        "fact_key": candidate.fact_key,
        "schema_version": SCHEMA_VERSION,
        "operation": IgMemoryFact.Operation.ASSERT,
        "typed_value": candidate.typed_value,
        "confidence": candidate.confidence,
        "source_role": "user",
        "producer": "analysis_v2",
        "producer_policy_version": PROJECTOR_VERSION,
        "closure_method": "analysis_assertion",
        "source_result_id": result.pk,
        "source_result_digest": result.result_digest,
        "source_materiality_digest": result.materiality_digest,
        "source_state_correlation": result.state_correlation,
        "source_watermark_message_id": result.watermark_message_id,
        "source_event_digest": "",
        "expected_evidence_count": len(candidate.evidence_ids),
        "supersedes_id": supersedes_id,
        "reason_code": "",
        "integrity_key_id": key_id,
        "observed_at": result.analyzed_at,
        "valid_until": candidate.valid_until,
        "sensitivity": candidate.sensitivity,
        "retention_class": candidate.retention_class,
    }
    identity = _fact_hmac_payload(values)
    identity.pop("record_key", None)
    identity.pop("supersedes_id", None)
    identity.pop("integrity_key_id", None)
    identity["source_result_key"] = result.result_key
    identity["evidence_ids"] = candidate.evidence_ids
    values["record_key"] = "memory-fact:" + _sha(identity)
    return values


def _fact_hmac_payload(values: dict) -> dict:
    return {
        key: values.get(key)
        for key in (
            "record_key", "slot_key", "client_id", "scope",
            "commercial_episode_id", "line_id", "order_id", "post_sale_case_id",
            "fact_key", "schema_version", "operation", "typed_value", "confidence",
            "source_role", "producer", "producer_policy_version", "closure_method",
            "source_result_id", "source_result_digest",
            "source_materiality_digest", "source_state_correlation",
            "source_watermark_message_id", "source_event_digest",
            "expected_evidence_count", "supersedes_id", "reason_code",
            "observed_at", "valid_until", "sensitivity", "retention_class",
            "integrity_key_id",
        )
    }


def fact_integrity_valid(fact: IgMemoryFact) -> bool:
    values = {
        field: getattr(fact, field)
        for field in _fact_hmac_payload({}).keys()
    }
    try:
        _key_id, expected = _mac(
            "management.typed-memory.fact.v1",
            _fact_hmac_payload(values),
            key_id=fact.integrity_key_id,
        )
    except ValueError:
        return False
    return hmac.compare_digest(str(fact.integrity_hmac or ""), expected)


def evidence_integrity_valid(evidence: IgMemoryFactEvidence) -> bool:
    payload = {
        "fact_id": evidence.fact_id,
        "ordinal": evidence.ordinal,
        "message_id": evidence.message_id,
        "source_role": evidence.source_role,
        "claim_code": evidence.claim_code,
        "integrity_key_id": evidence.integrity_key_id,
    }
    try:
        _key_id, expected = _mac(
            "management.typed-memory.evidence.v1",
            payload,
            key_id=evidence.integrity_key_id,
        )
    except ValueError:
        return False
    return hmac.compare_digest(str(evidence.evidence_hmac or ""), expected)


def _semantic_record_key(fact: IgMemoryFact, evidence_rows) -> str:
    """Key one semantic append operation independently from the signing key id."""
    values = {
        field: getattr(fact, field)
        for field in _fact_hmac_payload({}).keys()
    }
    identity = _fact_hmac_payload(values)
    identity.pop("record_key", None)
    identity.pop("supersedes_id", None)
    identity.pop("integrity_key_id", None)
    if fact.operation == IgMemoryFact.Operation.ASSERT:
        try:
            source = getattr(fact, "source_result", None)
        except ObjectDoesNotExist:
            source = None
        identity["source_result_key"] = str(getattr(source, "result_key", "") or "")
        identity["evidence_ids"] = tuple(row.message_id for row in evidence_rows)
    return "memory-fact:" + _sha(identity)


def _evidence_matches_source(fact: IgMemoryFact, evidence: IgMemoryFactEvidence) -> bool:
    if (
        fact.operation != IgMemoryFact.Operation.ASSERT
        or not fact.source_result_id
        or evidence.fact_id != fact.pk
        or evidence.source_role != "user"
        or evidence.claim_code != {
            IgMemoryFact.FactKey.OBSERVED_LANGUAGE: "language",
            IgMemoryFact.FactKey.OBJECTION_OBSERVED: "objection",
            IgMemoryFact.FactKey.DEFERRED_INTENT: "deferred_intent",
        }.get(fact.fact_key)
        or evidence.integrity_key_id != fact.integrity_key_id
    ):
        return False
    try:
        result = fact.source_result
    except ObjectDoesNotExist:
        return False
    if evidence.claim_code == "language":
        return evidence.message_id in (result.language_evidence_message_ids or ())
    return any(
        isinstance(row, dict)
        and row.get("message_id") == evidence.message_id
        and row.get("source_role") == "user"
        and evidence.claim_code in (row.get("claim_codes") or ())
        for row in result.evidence_manifest or ()
    )


def _head_hmac_payload(head_values: dict) -> dict:
    return {
        key: head_values.get(key)
        for key in (
            "slot_key", "client_id", "scope", "commercial_episode_id", "line_id",
            "order_id", "post_sale_case_id", "fact_key", "schema_version",
            "current_fact_id", "state", "revision", "projection_policy_version",
            "projected_at",
            "integrity_key_id",
        )
    }


def head_integrity_valid(head: IgMemoryHead) -> bool:
    values = {
        field: getattr(head, field)
        for field in _head_hmac_payload({}).keys()
    }
    try:
        _key_id, expected = _mac(
            "management.typed-memory.head.v1",
            _head_hmac_payload(values),
            key_id=head.integrity_key_id,
        )
    except ValueError:
        return False
    return hmac.compare_digest(str(head.projection_hmac or ""), expected)


def memory_chain_valid(head: IgMemoryHead) -> bool:
    """Validate the exact current slot and its complete bounded append chain."""
    if not head_integrity_valid(head):
        return False
    try:
        revision = int(head.revision or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not 1 <= revision <= MAX_CHAIN_DEPTH:
        return False
    expected_slot = (
        head.slot_key,
        head.client_id,
        head.scope,
        head.commercial_episode_id,
        head.line_id,
        head.order_id,
        head.post_sale_case_id,
        head.fact_key,
        head.schema_version,
    )
    facts = list(
        IgMemoryFact.objects.filter(slot_key=head.slot_key)
        .select_related("source_result")
        .prefetch_related("evidence_rows")
        .order_by("-id")[:MAX_CHAIN_DEPTH + 1]
    )
    if len(facts) > MAX_CHAIN_DEPTH:
        return False
    by_id = {fact.pk: fact for fact in facts}
    fact = by_id.get(head.current_fact_id)
    if fact is None:
        return False
    current_operation = fact.operation
    seen: set[int] = set()
    depth = 0
    while fact is not None:
        if not fact.pk or fact.pk in seen:
            return False
        seen.add(fact.pk)
        depth += 1
        if depth > MAX_CHAIN_DEPTH:
            return False
        fact_slot = (
            fact.slot_key,
            fact.client_id,
            fact.scope,
            fact.commercial_episode_id,
            fact.line_id,
            fact.order_id,
            fact.post_sale_case_id,
            fact.fact_key,
            fact.schema_version,
        )
        if fact_slot != expected_slot or not fact_integrity_valid(fact):
            return False
        rows = sorted(
            fact.evidence_rows.all(),
            key=lambda row: (row.ordinal, row.pk or 0),
        )
        if (
            len(rows) != int(fact.expected_evidence_count or 0)
            or [row.ordinal for row in rows] != list(range(1, len(rows) + 1))
            or _semantic_record_key(fact, rows) != fact.record_key
        ):
            return False
        if fact.operation == IgMemoryFact.Operation.ASSERT:
            if not rows or not all(
                evidence_integrity_valid(row) and _evidence_matches_source(fact, row)
                for row in rows
            ):
                return False
        elif rows:
            return False
        predecessor_id = fact.supersedes_id
        if predecessor_id is None:
            fact = None
        else:
            fact = by_id.get(predecessor_id)
            if fact is None:
                return False
    expected_state = {
        IgMemoryFact.Operation.ASSERT: IgMemoryHead.State.ACTIVE,
        IgMemoryFact.Operation.INVALIDATE: IgMemoryHead.State.INVALIDATED,
        IgMemoryFact.Operation.EXPIRE: IgMemoryHead.State.EXPIRED,
    }.get(current_operation)
    return depth == revision and head.state == expected_state


def _result_is_exact_current(result, client, job) -> bool:
    if (
        client.privacy_erasure_started_at
        or client.hidden_at
        or job.status != IgConversationAnalysisJob.Status.DONE
        or str(job.materiality_digest or "")
        != str(job.analyzed_materiality_digest or "")
        or int(job.analyzed_materiality_event_highwater or 0)
        < int(job.materiality_event_highwater or 0)
        or int(job.analyzed_watermark_message_id or 0)
        < int(job.watermark_message_id or 0)
        or int(job.analyzed_revision or 0) < int(job.revision or 0)
        or result.client_id != client.pk
        or result.commercial_episode_id != client.current_commercial_episode_id
        or result.line_id != str(job.materiality_line_id or "")
        or result.watermark_message_id != int(job.analyzed_watermark_message_id or 0)
        or result.job_revision != int(job.analyzed_revision or 0)
        or result.materiality_event_highwater
        != int(job.analyzed_materiality_event_highwater or 0)
        or result.materiality_digest != str(job.analyzed_materiality_digest or "")
        or result.authority_digest != str(job.authority_digest or "")
        or result.artifact_digest != str(job.artifact_digest or "")
    ):
        return False
    from management.services.ig_analysis_v2 import (
        result_digest_for_instance,
        state_correlation,
    )
    from management.services.ig_funnel_reset import current_message_floor

    return bool(
        result.result_digest == result_digest_for_instance(result)
        and result.state_correlation == state_correlation(job.required_state_fingerprint)
        and result.watermark_message_id >= current_message_floor(client)
    )


def _publish_analysis_memory_once(result_or_id) -> PublishOutcome:
    """Project one current result; never calls Gemini or reads raw transcript."""
    if not shadow_enabled():
        return PublishOutcome(status="off")
    result_id = getattr(result_or_id, "pk", result_or_id)
    if not result_id:
        return PublishOutcome(status="missing")
    try:
        identity = (
            IgConversationAnalysisResult.objects.filter(pk=result_id)
            .values("client_id")
            .first()
        )
        if not identity:
            return PublishOutcome(status="missing", result_id=int(result_id))
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
            job = (
                IgConversationAnalysisJob.objects.select_for_update()
                .filter(client_id=client.pk)
                .first()
            )
            result = (
                IgConversationAnalysisResult.objects.select_for_update()
                .select_related("commercial_episode")
                .filter(pk=result_id, result_schema_version=RESULT_SCHEMA_VERSION)
                .first()
            )
            if result is None:
                return PublishOutcome(status="incompatible", result_id=int(result_id))
            if job is None or not _result_is_exact_current(result, client, job):
                return PublishOutcome(status="stale", result_id=result.pk)
            candidates = candidates_from_result(result)
            if not candidates:
                return PublishOutcome(status="no_claims", result_id=result.pk)
            key_id, _ring = _keyring()
            all_evidence_ids = sorted({
                message_id
                for candidate in candidates
                for message_id in candidate.evidence_ids
            })
            from management.services.ig_funnel_reset import current_message_floor

            floor = current_message_floor(client)
            owned_evidence = set(
                InstagramBotMessage.objects.filter(
                    pk__in=all_evidence_ids,
                    client_id=client.pk,
                    role=InstagramBotMessage.Role.USER,
                    pk__gte=floor,
                    pk__lte=result.watermark_message_id,
                ).values_list("pk", flat=True)
            )
            if owned_evidence != set(all_evidence_ids):
                return PublishOutcome(status="invalid_evidence", result_id=result.pk)
            plans = []
            for candidate in sorted(candidates, key=lambda row: (
                row.fact_key, row.scope, row.line_id,
            )):
                slot_key = "memory-slot:" + _sha(_slot_payload(result, candidate))
                head = (
                    IgMemoryHead.objects.select_for_update()
                    .select_related("current_fact")
                    .filter(slot_key=slot_key)
                    .first()
                )
                if head is not None and not memory_chain_valid(head):
                    return PublishOutcome(status="integrity_error", result_id=result.pk)
                supersedes_id = head.current_fact_id if head else None
                values = _fact_payload(
                    result,
                    candidate,
                    slot_key=slot_key,
                    supersedes_id=supersedes_id,
                    key_id=key_id,
                )
                existing = IgMemoryFact.objects.filter(
                    record_key=values["record_key"]
                ).first()
                if existing is not None:
                    if not fact_integrity_valid(existing):
                        raise ValueError("typed-memory fact identity conflict")
                exact_noop = bool(
                    head is not None
                    and existing is not None
                    and head.current_fact_id == existing.pk
                )
                if (
                    head is not None
                    and not exact_noop
                    and int(head.revision or 0) >= MAX_CHAIN_DEPTH
                ):
                    return PublishOutcome(
                        status="chain_depth_exhausted",
                        result_id=result.pk,
                    )
                plans.append((candidate, slot_key, head, values, existing, exact_noop))

            # Every affected head is now locked and every possible advancement
            # has passed the depth gate. Only this second phase may append rows.
            created_facts = advanced_heads = unchanged_heads = 0
            for candidate, slot_key, head, values, existing, exact_noop in plans:
                if exact_noop:
                    unchanged_heads += 1
                    continue
                if existing is None:
                    _kid, signature = _mac(
                        "management.typed-memory.fact.v1",
                        _fact_hmac_payload(values),
                        key_id=key_id,
                    )
                    fact = IgMemoryFact(**values, integrity_hmac=signature)
                    fact.save(force_insert=True)
                    for ordinal, message_id in enumerate(candidate.evidence_ids, start=1):
                        evidence_payload = {
                            "fact_id": fact.pk,
                            "ordinal": ordinal,
                            "message_id": message_id,
                            "source_role": "user",
                            "claim_code": {
                                "observed_language": "language",
                                "objection_observed": "objection",
                                "deferred_intent": "deferred_intent",
                            }[candidate.fact_key],
                            "integrity_key_id": key_id,
                        }
                        _ekid, evidence_signature = _mac(
                            "management.typed-memory.evidence.v1",
                            evidence_payload,
                            key_id=key_id,
                        )
                        IgMemoryFactEvidence.objects.create(
                            **evidence_payload,
                            evidence_hmac=evidence_signature,
                        )
                    created_facts += 1
                else:
                    fact = existing
                head_values = {
                    "slot_key": slot_key,
                    "client_id": result.client_id,
                    "scope": candidate.scope,
                    "commercial_episode_id": candidate.episode_id,
                    "line_id": candidate.line_id,
                    "order_id": None,
                    "post_sale_case_id": None,
                    "fact_key": candidate.fact_key,
                    "schema_version": SCHEMA_VERSION,
                    "current_fact_id": fact.pk,
                    "state": IgMemoryHead.State.ACTIVE,
                    "revision": (int(head.revision) + 1 if head else 1),
                    "projection_policy_version": PROJECTOR_VERSION,
                    "projected_at": timezone.now(),
                    "integrity_key_id": key_id,
                }
                _hkid, head_signature = _mac(
                    "management.typed-memory.head.v1",
                    _head_hmac_payload(head_values),
                    key_id=key_id,
                )
                if head is None:
                    head = IgMemoryHead(
                        **head_values,
                        projection_hmac=head_signature,
                    )
                else:
                    head.current_fact = fact
                    head.state = IgMemoryHead.State.ACTIVE
                    head.revision = head_values["revision"]
                    head.projected_at = head_values["projected_at"]
                    head.projection_hmac = head_signature
                    head.integrity_key_id = key_id
                head.save()
                advanced_heads += 1
            return PublishOutcome(
                status="published",
                result_id=result.pk,
                created_facts=created_facts,
                advanced_heads=advanced_heads,
                unchanged_heads=unchanged_heads,
            )
    except ValueError:
        return PublishOutcome(status="conflict", result_id=int(result_id))


def publish_analysis_memory(result_or_id) -> PublishOutcome:
    if not shadow_enabled():
        return PublishOutcome(status="off")
    for attempt in range(3):
        try:
            return _publish_analysis_memory_once(result_or_id)
        except (OperationalError, IntegrityError) as exc:
            if not _retryable_database_error(exc) or attempt == 2:
                return PublishOutcome(
                    status=(
                        "retryable_error"
                        if _retryable_database_error(exc)
                        else "database_error"
                    ),
                    result_id=getattr(result_or_id, "pk", result_or_id),
                )
        except (ValidationError, IgClient.DoesNotExist):
            return PublishOutcome(
                status="invalid",
                result_id=getattr(result_or_id, "pk", result_or_id),
            )
    return PublishOutcome(status="retryable_error")


def _retryable_database_error(error) -> bool:
    code = error.args[0] if getattr(error, "args", ()) else None
    text = str(error).casefold()
    return code in {1205, 1213} or "deadlock" in text or "database is locked" in text


def _append_memory_tombstone_once(
    head_or_id,
    *,
    operation: str,
    source_event_digest: str,
    reason_code: str,
    source_role: str = "system",
    now=None,
) -> PublishOutcome:
    """Append one deterministic invalidation/expiry and advance its exact head."""
    if not shadow_enabled():
        return PublishOutcome(status="off")
    head_id = getattr(head_or_id, "pk", head_or_id)
    if operation == IgMemoryFact.Operation.INVALIDATE:
        closure_method = "deterministic_invalidation"
        if reason_code == "valid_until_elapsed":
            return PublishOutcome(status="invalid_tombstone")
        next_state = IgMemoryHead.State.INVALIDATED
    elif operation == IgMemoryFact.Operation.EXPIRE:
        closure_method = "ttl_expiry"
        if reason_code != "valid_until_elapsed":
            return PublishOutcome(status="invalid_tombstone")
        next_state = IgMemoryHead.State.EXPIRED
    else:
        return PublishOutcome(status="invalid_tombstone")
    if not isinstance(source_event_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_event_digest
    ):
        return PublishOutcome(status="invalid_tombstone")
    now = now or timezone.now()
    try:
        identity = IgMemoryHead.objects.filter(pk=head_id).values("client_id").first()
        if not identity:
            return PublishOutcome(status="missing")
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=identity["client_id"])
            if client.privacy_erasure_started_at:
                return PublishOutcome(status="privacy_fenced")
            head = (
                IgMemoryHead.objects.select_for_update()
                .select_related("current_fact")
                .get(pk=head_id)
            )
            if not memory_chain_valid(head):
                return PublishOutcome(status="not_active")
            if int(head.revision or 0) >= MAX_CHAIN_DEPTH:
                return PublishOutcome(status="chain_depth_exhausted")
            if head.state != IgMemoryHead.State.ACTIVE:
                return PublishOutcome(status="not_active")
            old = head.current_fact
            if operation == IgMemoryFact.Operation.EXPIRE and (
                old.valid_until is None or old.valid_until > now
            ):
                return PublishOutcome(status="not_due")
            key_id, _ring = _keyring()
            values = {
                "record_key": "",
                "slot_key": old.slot_key,
                "client_id": old.client_id,
                "scope": old.scope,
                "commercial_episode_id": old.commercial_episode_id,
                "line_id": old.line_id,
                "order_id": old.order_id,
                "post_sale_case_id": old.post_sale_case_id,
                "fact_key": old.fact_key,
                "schema_version": SCHEMA_VERSION,
                "operation": operation,
                "typed_value": {},
                "confidence": None,
                "source_role": source_role,
                "producer": "deterministic_projector",
                "producer_policy_version": PROJECTOR_VERSION,
                "closure_method": closure_method,
                "source_result_id": None,
                "source_result_digest": "",
                "source_materiality_digest": "",
                "source_state_correlation": "",
                "source_watermark_message_id": 0,
                "source_event_digest": source_event_digest,
                "expected_evidence_count": 0,
                "supersedes_id": old.pk,
                "reason_code": reason_code,
                "integrity_key_id": key_id,
                "observed_at": now,
                "valid_until": None,
                "sensitivity": old.sensitivity,
                "retention_class": old.retention_class,
            }
            record_identity = _fact_hmac_payload(values)
            record_identity.pop("record_key", None)
            record_identity.pop("supersedes_id", None)
            record_identity.pop("integrity_key_id", None)
            values["record_key"] = "memory-fact:" + _sha(record_identity)
            _kid, signature = _mac(
                "management.typed-memory.fact.v1",
                _fact_hmac_payload(values),
                key_id=key_id,
            )
            fact = IgMemoryFact(**values, integrity_hmac=signature)
            fact.save(force_insert=True)
            head_values = {
                "slot_key": head.slot_key,
                "client_id": head.client_id,
                "scope": head.scope,
                "commercial_episode_id": head.commercial_episode_id,
                "line_id": head.line_id,
                "order_id": head.order_id,
                "post_sale_case_id": head.post_sale_case_id,
                "fact_key": head.fact_key,
                "schema_version": head.schema_version,
                "current_fact_id": fact.pk,
                "state": next_state,
                "revision": int(head.revision) + 1,
                "projection_policy_version": PROJECTOR_VERSION,
                "projected_at": now,
                "integrity_key_id": key_id,
            }
            _hkid, head_signature = _mac(
                "management.typed-memory.head.v1",
                _head_hmac_payload(head_values),
                key_id=key_id,
            )
            head.current_fact = fact
            head.state = next_state
            head.revision = head_values["revision"]
            head.projected_at = now
            head.projection_hmac = head_signature
            head.integrity_key_id = key_id
            head.save()
            return PublishOutcome(
                status="published",
                created_facts=1,
                advanced_heads=1,
            )
    except (ValueError, IgMemoryHead.DoesNotExist):
        return PublishOutcome(status="conflict")


def append_memory_tombstone(*args, **kwargs) -> PublishOutcome:
    for attempt in range(3):
        try:
            return _append_memory_tombstone_once(*args, **kwargs)
        except (OperationalError, IntegrityError) as exc:
            if not _retryable_database_error(exc) or attempt == 2:
                return PublishOutcome(
                    status=(
                        "retryable_error"
                        if _retryable_database_error(exc)
                        else "database_error"
                    )
                )
        except ValidationError:
            return PublishOutcome(status="invalid")
    return PublishOutcome(status="retryable_error")


def expire_due_memory(*, limit=100, now=None) -> dict:
    if not shadow_enabled():
        return {"mode": MODE_OFF, "considered": 0, "expired": 0}
    now = now or timezone.now()
    ids = list(
        IgMemoryHead.objects.filter(
            state=IgMemoryHead.State.ACTIVE,
            current_fact__valid_until__isnull=False,
            current_fact__valid_until__lte=now,
            client__privacy_erasure_started_at__isnull=True,
        ).order_by("current_fact__valid_until", "pk").values_list("pk", flat=True)[:
            max(1, min(int(limit or 1), MAX_RECONCILE))
        ]
    )
    expired = 0
    for head_id in ids:
        event_digest = _sha({"head_id": head_id, "valid_until": str(now.date())})
        outcome = append_memory_tombstone(
            head_id,
            operation=IgMemoryFact.Operation.EXPIRE,
            source_event_digest=event_digest,
            reason_code="valid_until_elapsed",
            now=now,
        )
        expired += int(outcome.status == "published")
    return {"mode": MODE_SHADOW, "considered": len(ids), "expired": expired}


def invalidate_memory_for_reset(*, client_id: int, reset_after_message_id: int) -> dict:
    if not shadow_enabled():
        return {"mode": MODE_OFF, "considered": 0, "invalidated": 0}
    ids = list(
        IgMemoryHead.objects.filter(
            client_id=client_id,
            state=IgMemoryHead.State.ACTIVE,
            scope__in=(IgMemoryFact.Scope.EPISODE, IgMemoryFact.Scope.LINE),
            current_fact__source_watermark_message_id__lte=max(
                0, int(reset_after_message_id or 0)
            ),
        ).order_by("slot_key").values_list("pk", flat=True)
    )
    invalidated = 0
    for head_id in ids:
        digest = _sha({
            "client_id": int(client_id),
            "reset_after_message_id": int(reset_after_message_id or 0),
            "head_id": head_id,
        })
        outcome = append_memory_tombstone(
            head_id,
            operation=IgMemoryFact.Operation.INVALIDATE,
            source_event_digest=digest,
            reason_code="reset_boundary",
        )
        invalidated += int(outcome.status == "published")
    return {
        "mode": MODE_SHADOW,
        "considered": len(ids),
        "invalidated": invalidated,
    }


def reconcile_reset_tombstones(*, limit=100) -> dict:
    if not shadow_enabled():
        return {"mode": MODE_OFF, "considered": 0, "invalidated": 0}
    from management.models import IgFunnelResetAudit

    latest = IgFunnelResetAudit.objects.filter(
        client_id=OuterRef("client_id")
    ).order_by("-id")
    rows = list(
        IgMemoryHead.objects.filter(
            state=IgMemoryHead.State.ACTIVE,
            scope__in=(IgMemoryFact.Scope.EPISODE, IgMemoryFact.Scope.LINE),
            client__privacy_erasure_started_at__isnull=True,
        )
        .annotate(
            reset_boundary=Subquery(latest.values("reset_after_message_id")[:1]),
            reset_id=Subquery(latest.values("id")[:1]),
        )
        .filter(
            reset_boundary__isnull=False,
            current_fact__source_watermark_message_id__lte=F("reset_boundary"),
        )
        .order_by("slot_key")
        .values("id", "client_id", "reset_boundary", "reset_id")[:
            max(1, min(int(limit or 1), MAX_RECONCILE))
        ]
    )
    invalidated = 0
    for row in rows:
        digest = _sha({
            "client_id": row["client_id"],
            "reset_id": row["reset_id"],
            "reset_boundary": row["reset_boundary"],
            "head_id": row["id"],
        })
        outcome = append_memory_tombstone(
            row["id"],
            operation=IgMemoryFact.Operation.INVALIDATE,
            source_event_digest=digest,
            reason_code="reset_boundary",
        )
        invalidated += int(outcome.status == "published")
    return {
        "mode": MODE_SHADOW,
        "considered": len(rows),
        "invalidated": invalidated,
    }


def reconcile_typed_memory(*, limit=100) -> dict:
    if not shadow_enabled():
        return {"mode": MODE_OFF, "considered": 0, "published": 0}
    bounded = max(1, min(int(limit or 1), MAX_RECONCILE))
    settings_row = InstagramBotSettings.load()
    cursor = int(settings_row.typed_memory_reconcile_cursor or 0)
    base = IgConversationAnalysisResult.objects.filter(
        result_schema_version=RESULT_SCHEMA_VERSION,
        client__privacy_erasure_started_at__isnull=True,
    ).order_by("id")
    result_ids = list(base.filter(id__gt=cursor).values_list("id", flat=True)[:bounded])
    if not result_ids and cursor:
        InstagramBotSettings.objects.filter(
            pk=settings_row.pk,
            typed_memory_reconcile_cursor=cursor,
        ).update(
            typed_memory_reconcile_cursor=0,
            updated_at=timezone.now(),
        )
        cursor = 0
        result_ids = list(base.values_list("id", flat=True)[:bounded])
    counts: dict[str, int] = {}
    for result_id in result_ids:
        outcome = publish_analysis_memory(result_id)
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    if result_ids:
        from django.db.models.functions import Greatest
        from django.db.models import F, Value

        InstagramBotSettings.objects.filter(pk=settings_row.pk).update(
            typed_memory_reconcile_cursor=Greatest(
                F("typed_memory_reconcile_cursor"),
                Value(max(result_ids)),
            ),
            updated_at=timezone.now(),
        )
    expiry = expire_due_memory(limit=bounded)
    reset_tombstones = reconcile_reset_tombstones(limit=bounded)
    return {
        "mode": MODE_SHADOW,
        "considered": len(result_ids),
        "published": counts.get("published", 0),
        "outcomes": dict(sorted(counts.items())),
        "expiry": expiry,
        "reset_tombstones": reset_tombstones,
    }


def parity_report(*, limit=500) -> dict:
    """Sanitized aggregates only; never reads message text or summary content."""
    if not shadow_enabled():
        return {"mode": MODE_OFF, "eligible_results": 0, "active_heads": 0}
    bounded = max(1, min(int(limit or 1), 5000))
    results = IgConversationAnalysisResult.objects.filter(
        result_schema_version=RESULT_SCHEMA_VERSION,
    )
    heads = list(
        IgMemoryHead.objects.select_related("current_fact")
        .order_by("-updated_at")[:bounded]
    )
    bad_hmac = sum(1 for head in heads if not memory_chain_valid(head))
    by_fact: dict[str, int] = {}
    for head in heads:
        by_fact[head.fact_key] = by_fact.get(head.fact_key, 0) + 1
    return {
        "mode": MODE_SHADOW,
        "eligible_results": results.count(),
        "projected_results": results.filter(memory_facts__isnull=False).distinct().count(),
        "active_heads": sum(head.state == IgMemoryHead.State.ACTIVE for head in heads),
        "bad_hmac": bad_hmac,
        "facts": dict(sorted(by_fact.items())),
    }


def purge_client_analysis_memory(client_ids) -> dict:
    """Delete client-scoped analytical data only behind a committed privacy fence."""
    parsed_ids = set()
    for value in client_ids:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed > 0:
            parsed_ids.add(parsed)
    ids = sorted(parsed_ids)
    if not ids:
        return {"clients": 0, "rows": 0}
    with transaction.atomic():
        fenced = list(
            IgClient.objects.select_for_update()
            .filter(pk__in=ids, privacy_erasure_started_at__isnull=False)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if fenced != ids:
            raise ValueError("typed-memory privacy purge requires every client fence")
        placeholders = ", ".join(["%s"] * len(ids))
        quote = connection.ops.quote_name
        statements = (
            (
                "management_igmemoryfactevidence",
                "fact_id IN (SELECT id FROM management_igmemoryfact "
                f"WHERE client_id IN ({placeholders}))",
            ),
            ("management_igmemoryhead", f"client_id IN ({placeholders})"),
            ("management_igmemoryfact", f"client_id IN ({placeholders})"),
            (
                "management_igconversationroutedecision",
                f"client_id IN ({placeholders})",
            ),
            ("management_iganalysisproposal", f"client_id IN ({placeholders})"),
            ("management_igconversationanalysisresult", f"client_id IN ({placeholders})"),
            ("management_iganalysismaterialityevent", f"client_id IN ({placeholders})"),
            ("management_igconversationanalysisevent", f"client_id IN ({placeholders})"),
        )
        deleted: dict[str, int] = {}
        with connection.cursor() as cursor:
            for table_name, where in statements:
                cursor.execute(
                    f"DELETE FROM {quote(table_name)} WHERE {where}",
                    ids,
                )
                deleted[table_name] = max(0, int(cursor.rowcount or 0))
        return {
            "clients": len(ids),
            "rows": sum(deleted.values()),
            "tables": deleted,
        }
