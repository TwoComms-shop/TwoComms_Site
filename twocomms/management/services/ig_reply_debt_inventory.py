"""Read-only evidence inventory for explicitly named historical IG reply debt.

The inventory is intentionally a projection, not a reconciler.  It never
creates tasks, changes a source/revision, finalizes a delivery effect, or
contacts a provider.  Output contains structural identifiers and bounded
delivery state only; it deliberately omits all customer and provider content.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from django.db.models import Q

from management.models import (
    IgCustomerTurnRevision,
    IgFollowUpTask,
    IgRevisionDeliveryEffect,
    IgTurnRevisionSource,
    InstagramBotMessage,
)


SCHEMA_VERSION = "ig-reply-debt-inventory-v1"
MAX_REQUESTED_IDS = 200
MAX_RELATED_REVISIONS = 800
MAX_RELATED_SOURCES = 3200
MAX_RELATED_EFFECTS = 3200
_CLASSIFICATIONS = ("coverage", "transfer", "manual", "unsupported")
_DEBT_REASON = "revision_case:execution_debt"
_DEBT_KIND = "revision_execution_debt"
_TRANSFER_IN = "source_transfer_in"
_TRANSFER_OUT = "source_transfer_out"
_UNCERTAIN_EFFECT_STATES = frozenset({"claimed", "provider_started", "unknown"})


class InventoryRequestError(ValueError):
    """The caller did not provide a bounded, explicit inventory target."""


def _normalize_ids(values: Iterable[object], *, label: str) -> tuple[int, ...]:
    normalized = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise InventoryRequestError(f"{label} must contain positive integer IDs")
        normalized.add(value)
    if len(normalized) > MAX_REQUESTED_IDS:
        raise InventoryRequestError(
            f"at most {MAX_REQUESTED_IDS} {label} may be requested at once"
        )
    return tuple(sorted(normalized))


def _safe_id_list(value: object) -> list[int] | None:
    if not isinstance(value, list) or not value or len(value) > MAX_REQUESTED_IDS:
        return None
    result = []
    seen = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            return None
        if item in seen:
            return None
        seen.add(item)
        result.append(item)
    return result


def _safe_optional_id_list(value: object) -> list[int] | None:
    if not isinstance(value, list):
        return None
    if not value:
        return []
    return _safe_id_list(value)


def _load_revisions(revision_ids: set[int]) -> dict[int, dict]:
    if not revision_ids:
        return {}
    fields = (
        "id", "client_id", "parent_id", "origin", "permission_epoch",
        "action_receipts", "quiet_started_at", "quiet_cap_at", "overall_deadline",
    )
    return {
        row["id"]: row
        for row in IgCustomerTurnRevision.objects.filter(pk__in=revision_ids).values(*fields)
    }


def _load_revision_objects(revision_ids: set[int]) -> tuple[dict[int, object], bool]:
    rows = list(
        IgCustomerTurnRevision.objects.select_related("client")
        .filter(pk__in=revision_ids)
        .order_by("id")[: MAX_RELATED_REVISIONS + 1]
    )
    overflow = len(rows) > MAX_RELATED_REVISIONS
    rows = rows[:MAX_RELATED_REVISIONS]
    for row in rows:
        # Canonical transfer receipts call this historical timestamp by its
        # receipt name; current ORM rows store it as quiet_started_at.
        row.first_unanswered_at = row.quiet_started_at
    return {row.pk: row for row in rows}, overflow


def _load_revision_source_objects(revision_ids: set[int]):
    rows = list(
        IgTurnRevisionSource.objects.filter(revision_id__in=revision_ids)
        .order_by("revision_id", "ordinal", "id")[: MAX_RELATED_SOURCES + 1]
    )
    overflow = len(rows) > MAX_RELATED_SOURCES
    rows = rows[:MAX_RELATED_SOURCES]
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.revision_id].append(row)
    return grouped, overflow


def _transfer_target_ids(revisions: dict[int, dict]) -> set[int]:
    targets = set()
    for revision in revisions.values():
        receipts = revision.get("action_receipts") or {}
        if not isinstance(receipts, dict):
            continue
        for key in (_TRANSFER_IN, _TRANSFER_OUT):
            receipt = receipts.get(key)
            if not isinstance(receipt, dict):
                continue
            for field in ("predecessor_revision_id", "successor_revision_id", "root_revision_id"):
                value = receipt.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    targets.add(value)
    return targets


def _complete_direct_delivery(source: dict) -> bool:
    planned = source["delivery_planned_chunk_count"]
    delivered = source["delivery_delivered_chunk_count"]
    provider_ids = source["delivery_provider_message_ids"]
    return (
        source["send_state"] == "sent"
        and isinstance(planned, int)
        and not isinstance(planned, bool)
        and planned > 0
        and delivered == planned
        and not source["delivery_failure_boundary"]
        and isinstance(provider_ids, list)
        and len(provider_ids) == planned
        and all(isinstance(value, str) and bool(value.strip()) for value in provider_ids)
        and len(set(provider_ids)) == len(provider_ids)
    )


def _direct_evidence_malformed(source: dict) -> bool:
    planned = source["delivery_planned_chunk_count"]
    delivered = source["delivery_delivered_chunk_count"]
    provider_ids = source["delivery_provider_message_ids"]
    if not isinstance(planned, int) or isinstance(planned, bool) or planned < 0:
        return True
    if not isinstance(delivered, int) or isinstance(delivered, bool) or delivered < 0:
        return True
    if delivered > planned:
        return True
    if not isinstance(provider_ids, list):
        return True
    if any(not isinstance(value, str) or not value.strip() for value in provider_ids):
        return True
    return len(set(provider_ids)) != len(provider_ids) or len(provider_ids) > planned


def _direct_delivery_issue(source: dict) -> str:
    state = str(source["send_state"] or "")
    planned = source["delivery_planned_chunk_count"]
    delivered = source["delivery_delivered_chunk_count"]
    boundary = str(source["delivery_failure_boundary"] or "")
    has_delivery_evidence = bool(planned or delivered or boundary or state)
    if state in {"sending", "unknown", "ambiguous"}:
        return "delivery_uncertain"
    if has_delivery_evidence and not _complete_direct_delivery(source):
        return "delivery_partial_or_unproven"
    return ""


def _effect_delivery_issue(effects: list[dict]) -> str:
    if not effects:
        return ""
    states = {str(row["state"] or "") for row in effects}
    if states.intersection(_UNCERTAIN_EFFECT_STATES):
        return "delivery_uncertain"
    if "sent" in states and states != {"sent"}:
        return "delivery_partial_or_unproven"
    return ""


def _effects_malformed(effects: list[dict]) -> bool:
    allowed_states = {
        "planned", "claimed", "provider_started", "sent", "definite_failed",
        "unknown", "cancelled", "superseded",
    }
    seen = set()
    group_counts: dict[tuple[int, str], set[int]] = defaultdict(set)
    for effect in effects:
        part_index = effect["part_index"]
        part_count = effect["part_count"]
        key = (effect["revision_id"], effect["group"], part_index)
        if (
            not effect["group"]
            or effect["state"] not in allowed_states
            or isinstance(part_index, bool)
            or not isinstance(part_index, int)
            or part_index < 0
            or isinstance(part_count, bool)
            or not isinstance(part_count, int)
            or part_count < 1
            or part_index >= part_count
            or key in seen
        ):
            return True
        seen.add(key)
        group_counts[(effect["revision_id"], effect["group"])].add(part_count)
    return any(len(counts) != 1 for counts in group_counts.values())


def _canonical_transfer_for_source(
    source_id: int,
    revision_ids: list[int],
    revision_objects: dict[int, object],
    source_objects_by_revision,
) -> tuple[int, int] | None:
    from management.services.ig_revision_source_coverage import validate_source_transfer

    candidate_ids = set()
    for revision_id in revision_ids:
        revision = revision_objects.get(revision_id)
        receipts = getattr(revision, "action_receipts", None)
        if revision is None or not isinstance(receipts, dict):
            continue
        receipt = receipts.get(_TRANSFER_IN)
        if isinstance(receipt, dict):
            candidate_ids.add(revision_id)
        receipt = receipts.get(_TRANSFER_OUT)
        if isinstance(receipt, dict):
            successor_id = receipt.get("successor_revision_id")
            if isinstance(successor_id, int) and not isinstance(successor_id, bool):
                candidate_ids.add(successor_id)
    for successor_id in sorted(candidate_ids):
        successor = revision_objects.get(successor_id)
        receipt = ((getattr(successor, "action_receipts", None) or {}).get(_TRANSFER_IN) or {})
        try:
            valid = validate_source_transfer(
                successor,
                revision_rows=revision_objects,
                sources_by_revision=source_objects_by_revision,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            valid = False
        if valid and source_id in (receipt.get("source_message_ids") or []):
            return receipt["predecessor_revision_id"], successor_id
    return None


def _has_transfer_claim(revision_ids: list[int], revisions: dict[int, dict]) -> bool:
    for revision_id in revision_ids:
        receipts = (revisions.get(revision_id) or {}).get("action_receipts")
        if isinstance(receipts, dict) and (
            _TRANSFER_IN in receipts or _TRANSFER_OUT in receipts
        ):
            return True
    return False


def _revision_delivery_proof(revision, effects) -> tuple[str, str]:
    """Read the canonical aggregate/proof for every effect in a revision."""
    if revision is None:
        return "unsupported", "revision_missing"
    if not effects:
        return "", ""
    from management.services.ig_revision_proposal import _snapshot_sources

    snapshot_sources, _parts = _snapshot_sources(revision)
    if len(snapshot_sources) != revision.source_count:
        return "unsupported", "revision_source_snapshot_invalid"
    from management.services.ig_revision_execution import _completion_decision, _whole_sent_proof

    completed, debt, reason = _completion_decision(effects)
    if debt or reason in {
        "delivery_unknown", "provider_started_unreconciled", "partial_definite_failure",
        "partial_cancelled_delivery", "technical_holding_sent", "definite_failure",
    }:
        return "manual", reason
    if reason in {"effects_pending", "cancelled_before_provider"}:
        return "manual", reason
    if not completed:
        return "unsupported", reason or "effect_state_invalid"
    proof, proof_reason = _whole_sent_proof(revision, effects)
    if not proof:
        return "unsupported", proof_reason or "delivery_proof_invalid"
    if not any(
        effect.group == "substantive_text" and effect.purpose == "normal_reply"
        for effect in effects
    ):
        return "unsupported", "substantive_reply_missing"
    return "coverage", "canonical_delivery_proof"


def _classify_source(
    source_id: int,
    *,
    sources: dict[int, dict],
    revision_sources_by_message: dict[int, list[dict]],
    revisions: dict[int, dict],
    sources_by_revision: dict[int, list[dict]],
    effects_by_source: dict[int, list[dict]],
    revision_delivery: dict[int, tuple[str, str]],
    revision_objects: dict[int, object],
    source_objects_by_revision,
) -> dict:
    source = sources.get(source_id)
    if source is None:
        return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "source_missing"}}
    if source["client_id"] is None or source["role"] != "user" or not source["sender_id"]:
        return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "source_malformed"}}
    source_rows = revision_sources_by_message.get(source_id, [])
    revision_ids = [row["revision_id"] for row in source_rows]
    invalid_rows = [
        row for row in source_rows
        if row["role"] != "user" or not row["source_digest"]
    ]
    related_revisions = [revisions.get(revision_id) for revision_id in revision_ids]
    if invalid_rows or any(revision is None for revision in related_revisions):
        return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "source_revision_malformed"}}
    if any(revision["client_id"] != source["client_id"] for revision in related_revisions):
        return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "source_cross_client"}}
    for effect in effects_by_source.get(source_id, []):
        effect_revision = revisions.get(effect["revision_id"])
        revision_source_ids = {
            row["message_id"] for row in sources_by_revision.get(effect["revision_id"], [])
        }
        if effect_revision is None or source_id not in revision_source_ids:
            return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "delivery_linkage_malformed"}}
        if effect_revision["client_id"] != source["client_id"]:
            return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "delivery_cross_client"}}
    effects = effects_by_source.get(source_id, [])
    if _direct_evidence_malformed(source) or _effects_malformed(effects):
        return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "delivery_evidence_malformed"}}
    transfer = _canonical_transfer_for_source(
        source_id, revision_ids, revision_objects, source_objects_by_revision
    )
    if _has_transfer_claim(revision_ids, revisions) and transfer is None:
        return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": "source_transfer_malformed"}}
    proof_results = [revision_delivery.get(revision_id, ("", "")) for revision_id in revision_ids]
    if any(result[0] == "unsupported" for result in proof_results):
        reason = next(result[1] for result in proof_results if result[0] == "unsupported")
        return {"source_message_id": source_id, "classification": "unsupported", "evidence": {"reason": reason}}
    if any(result[0] == "manual" for result in proof_results):
        reason = next(result[1] for result in proof_results if result[0] == "manual")
        return {"source_message_id": source_id, "classification": "manual", "evidence": {"reason": reason}}
    direct_issue = _direct_delivery_issue(source)
    effect_issue = _effect_delivery_issue(effects)
    if direct_issue or effect_issue:
        return {
            "source_message_id": source_id,
            "classification": "manual",
            "evidence": {
                "reason": direct_issue or effect_issue,
                "revision_ids": sorted(revision_ids),
            },
        }
    if any(result[0] == "coverage" for result in proof_results):
        return {
            "source_message_id": source_id,
            "classification": "coverage",
            "evidence": {"reason": "canonical_revision_delivery_complete", "revision_ids": sorted(revision_ids)},
        }
    if _complete_direct_delivery(source):
        return {
            "source_message_id": source_id,
            "classification": "coverage",
            "evidence": {"reason": "same_source_delivery_complete", "revision_ids": sorted(revision_ids)},
        }
    if transfer is not None:
        return {
            "source_message_id": source_id,
            "classification": "transfer",
            "evidence": {
                "reason": "paired_source_transfer_valid",
                "predecessor_revision_id": transfer[0],
                "successor_revision_id": transfer[1],
            },
        }
    return {
        "source_message_id": source_id,
        "classification": "manual",
        "evidence": {"reason": "exact_source_unresolved", "revision_ids": sorted(revision_ids)},
    }


def _classify_task(
    task_id: int,
    *,
    tasks: dict[int, dict],
    revisions: dict[int, dict],
    sources_by_revision: dict[int, list[dict]],
    source_results: dict[int, dict],
    effects_by_revision: dict[int, list[dict]],
) -> dict:
    task = tasks.get(task_id)
    if task is None:
        return {"task_id": task_id, "classification": "unsupported", "evidence": {"reason": "task_missing"}}
    payload = task["event_payload"]
    context = task["manager_context"]
    if not isinstance(payload, dict) or not isinstance(context, dict):
        return {"task_id": task_id, "classification": "unsupported", "evidence": {"reason": "task_malformed", "task_status": task["status"]}}
    revision_id = payload.get("revision_id")
    source_ids = _safe_id_list(payload.get("source_message_ids"))
    effect_ids = _safe_optional_id_list(payload.get("effect_ids"))
    revision = revisions.get(revision_id) if isinstance(revision_id, int) and not isinstance(revision_id, bool) else None
    review = context.get("operator_review")
    allowed_statuses = {value for value, _label in IgFollowUpTask.Status.choices}
    task_evidence = {
        "task_status": task["status"] if task["status"] in allowed_statuses else "invalid",
        "operator_review_present": isinstance(review, dict),
    }
    if (
        task["kind"] != "manager_task"
        or task["reason"] != _DEBT_REASON
        or not revision
        or task["client_id"] != revision["client_id"]
        or task["event_key"] != f"ig-revision-debt:{revision_id}"
        or context.get("case_kind") != _DEBT_KIND
        or context.get("revision_id") != revision_id
        or context.get("automatic_http_retry") is not False
        or source_ids is None
        or effect_ids is None
        or source_ids != [row["message_id"] for row in sources_by_revision.get(revision_id, [])]
        or effect_ids != [row["id"] for row in effects_by_revision.get(revision_id, [])]
    ):
        return {"task_id": task_id, "classification": "unsupported", "evidence": {"reason": "task_linkage_malformed", **task_evidence}}
    if "owner" not in context:
        return {"task_id": task_id, "classification": "unsupported", "evidence": {"reason": "legacy_owner_unverified", **task_evidence}}
    if context.get("owner") != "manager":
        return {"task_id": task_id, "classification": "unsupported", "evidence": {"reason": "task_linkage_malformed", **task_evidence}}
    classifications = [source_results[source_id]["classification"] for source_id in source_ids]
    if "unsupported" in classifications:
        classification, reason = "unsupported", "task_source_unsupported"
    elif "manual" in classifications:
        classification, reason = "manual", "task_source_manual_review"
    elif set(classifications) == {"coverage"}:
        classification, reason = "coverage", "all_task_sources_covered"
    elif set(classifications) == {"transfer"}:
        classification, reason = "transfer", "all_task_sources_transferred"
    else:
        classification, reason = "manual", "task_sources_mixed_disposition"
    return {
        "task_id": task_id,
        "classification": classification,
        "evidence": {"reason": reason, "revision_id": revision_id, "source_message_ids": source_ids, **task_evidence},
    }


def _unsupported_inventory(source_ids, task_ids, reason: str) -> dict:
    items = [
        {
            "target_type": "source_message",
            "source_message_id": source_id,
            "classification": "unsupported",
            "evidence": {"reason": reason},
        }
        for source_id in source_ids
    ]
    items.extend({
        "target_type": "task",
        "task_id": task_id,
        "classification": "unsupported",
        "evidence": {"reason": reason},
    } for task_id in task_ids)
    return {
        "schema_version": SCHEMA_VERSION,
        "requested": {"source_message_ids": list(source_ids), "task_ids": list(task_ids)},
        "counts": {name: len(items) if name == "unsupported" else 0 for name in _CLASSIFICATIONS},
        "items": items,
    }


def inventory_reply_debt(*, source_message_ids=(), task_ids=()) -> dict:
    """Return deterministic dispositions for only the explicitly named IDs."""
    source_ids = _normalize_ids(source_message_ids, label="source_message_ids")
    requested_task_ids = _normalize_ids(task_ids, label="task_ids")
    if not source_ids and not requested_task_ids:
        raise InventoryRequestError("at least one source_message_id or task_id is required")
    if len(source_ids) + len(requested_task_ids) > MAX_REQUESTED_IDS:
        raise InventoryRequestError(
            f"at most {MAX_REQUESTED_IDS} total source/task IDs may be requested at once"
        )

    task_fields = (
        "id", "client_id", "kind", "reason", "status", "event_key", "event_payload", "manager_context",
    )
    tasks = {
        row["id"]: row
        for row in IgFollowUpTask.objects.filter(pk__in=requested_task_ids).values(*task_fields)
    }
    task_source_ids = set()
    task_revision_ids = set()
    for task in tasks.values():
        payload = task["event_payload"]
        if isinstance(payload, dict):
            ids = _safe_id_list(payload.get("source_message_ids"))
            if ids is not None:
                task_source_ids.update(ids)
            revision_id = payload.get("revision_id")
            if isinstance(revision_id, int) and not isinstance(revision_id, bool) and revision_id > 0:
                task_revision_ids.add(revision_id)

    all_source_ids = set(source_ids) | task_source_ids
    source_fields = (
        "id", "client_id", "role", "sender_id", "send_state",
        "delivery_planned_chunk_count", "delivery_delivered_chunk_count",
        "delivery_provider_message_ids", "delivery_failure_boundary",
    )
    sources = {
        row["id"]: row
        for row in InstagramBotMessage.objects.filter(pk__in=all_source_ids).values(*source_fields)
    }
    revision_source_rows = list(IgTurnRevisionSource.objects.filter(
        message_id__in=all_source_ids
    ).values("id", "revision_id", "message_id", "ordinal", "role", "source_digest", "source_namespace")
        .order_by("message_id", "revision_id", "ordinal", "id")[: MAX_RELATED_SOURCES + 1])
    if len(revision_source_rows) > MAX_RELATED_SOURCES:
        return _unsupported_inventory(source_ids, requested_task_ids, "related_rows_truncated")
    revision_sources_by_message: dict[int, list[dict]] = defaultdict(list)
    revision_ids = set(task_revision_ids)
    for row in revision_source_rows:
        revision_sources_by_message[row["message_id"]].append(row)
        revision_ids.add(row["revision_id"])

    effect_fields = (
        "id", "revision_id", "source_message_id", "group", "order_index", "part_index", "part_count", "state",
    )
    effects_by_source: dict[int, list[dict]] = defaultdict(list)
    effects_by_revision: dict[int, list[dict]] = defaultdict(list)
    effect_objects = list(IgRevisionDeliveryEffect.objects.filter(
        Q(source_message_id__in=all_source_ids) | Q(revision_id__in=revision_ids)
    ).order_by("revision_id", "order_index", "id")[: MAX_RELATED_EFFECTS + 1])
    if len(effect_objects) > MAX_RELATED_EFFECTS:
        return _unsupported_inventory(source_ids, requested_task_ids, "related_rows_truncated")
    effect_objects = effect_objects[:MAX_RELATED_EFFECTS]
    effect_objects_by_revision = defaultdict(list)
    for effect in effect_objects:
        row = {field: getattr(effect, field) for field in effect_fields}
        effects_by_source[row["source_message_id"]].append(row)
        effects_by_revision[row["revision_id"]].append(row)
        revision_ids.add(row["revision_id"])
        effect_objects_by_revision[row["revision_id"]].append(effect)
    if len(revision_ids) > MAX_RELATED_REVISIONS:
        return _unsupported_inventory(source_ids, requested_task_ids, "related_rows_truncated")
    revisions = _load_revisions(revision_ids)
    transfer_ids = _transfer_target_ids(revisions) - set(revisions)
    if len(set(revisions) | transfer_ids) > MAX_RELATED_REVISIONS:
        return _unsupported_inventory(source_ids, requested_task_ids, "related_rows_truncated")
    if transfer_ids:
        revisions.update(_load_revisions(transfer_ids))
    if len(revisions) > MAX_RELATED_REVISIONS:
        return _unsupported_inventory(source_ids, requested_task_ids, "related_rows_truncated")
    revision_objects, revisions_overflow = _load_revision_objects(set(revisions))
    source_objects_by_revision, sources_overflow = _load_revision_source_objects(set(revisions))
    if revisions_overflow or sources_overflow:
        return _unsupported_inventory(source_ids, requested_task_ids, "related_rows_truncated")
    sources_by_revision = defaultdict(list)
    for revision_id, rows in source_objects_by_revision.items():
        sources_by_revision[revision_id] = [{
            "id": row.pk,
            "revision_id": row.revision_id,
            "message_id": row.message_id,
            "ordinal": row.ordinal,
            "role": row.role,
            "source_digest": row.source_digest,
            "source_namespace": row.source_namespace,
        } for row in rows]
    revision_delivery = {
        revision_id: _revision_delivery_proof(
            revision_objects.get(revision_id),
            effect_objects_by_revision.get(revision_id, []),
        )
        for revision_id in revisions
    }

    all_classified_source_ids = sorted(all_source_ids)
    source_results = {
        source_id: _classify_source(
            source_id,
            sources=sources,
            revision_sources_by_message=revision_sources_by_message,
            revisions=revisions,
            sources_by_revision=sources_by_revision,
            effects_by_source=effects_by_source,
            revision_delivery=revision_delivery,
            revision_objects=revision_objects,
            source_objects_by_revision=source_objects_by_revision,
        )
        for source_id in all_classified_source_ids
    }
    items = [
        {"target_type": "source_message", **source_results[source_id]}
        for source_id in source_ids
    ]
    items.extend(
        {
            "target_type": "task",
            **_classify_task(
                task_id,
                tasks=tasks,
                revisions=revisions,
                sources_by_revision=sources_by_revision,
                source_results=source_results,
                effects_by_revision=effects_by_revision,
            ),
        }
        for task_id in requested_task_ids
    )
    counts = Counter(item["classification"] for item in items)
    return {
        "schema_version": SCHEMA_VERSION,
        "requested": {"source_message_ids": list(source_ids), "task_ids": list(requested_task_ids)},
        "counts": {name: counts[name] for name in _CLASSIFICATIONS},
        "items": items,
    }
