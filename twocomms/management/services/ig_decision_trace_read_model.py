"""Bounded, content-free joins of existing revision evidence. Never repairs it.

The caller must authorize both bot operation and conversation PII access. A
winner is generation evidence, a SENT effect is a physical receipt, and source
coverage is a separate semantic contract. No current policy is reconstructed.
"""
from __future__ import annotations

import re
from collections import defaultdict

from management.models import (
    GeminiRequest, GeminiRequestAttempt, IgBotNotification, IgClient,
    IgCustomerTurnRevision, IgFollowUpTask, IgRevisionDeliveryEffect,
    IgTurnRevisionSource, InstagramBotMessage,
)
from management.services.ig_conversation_routes import conversation_route_reset_floor
from management.services.ig_request_manifest import sanitize_dispatch_context
from management.services.gemini_accounting_contract import (
    RequestPolicyManifestError, sanitize_request_policy_manifest,
)
from management.services.ig_reply_truth import REASON_CODES
from management.services.ig_revision_outbox import _digest
from management.services.ig_response_debt import response_coverage

SCHEMA_VERSION = "ig-decision-trace.v1"
INDEX_LIMIT = 25
SOURCE_LIMIT = 64
GRAPH_LIMIT = 8
ATTEMPT_LIMIT = 64
EFFECT_LIMIT = 64
READ_QUERY_CAP = 15
INDEX_QUERY_CAP = 6
_MODEL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/()-]{0,79}$")
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/=-]{0,254}$")
_VALIDATOR_CODES = frozenset(REASON_CODES) | frozenset({
    "invalid_response_schema", "invalid_result", "validator_error", "authority_unavailable",
    "missing_turn_intelligence", "unknown_inline_coverage", "unknown_inline_hashes",
    "actual_media_binding_mismatch", "incomplete_image_coverage", "catalog_selector_missing",
    "unnecessary_manager_handoff", "source_preference_mismatch",
}) | frozenset("schema_" + name for name in (
    "invalid_json", "malformed_payload", "invalid_reply_text", "too_many_controls",
    "control_token_in_reply_text", "malformed_control", "invalid_control",
    "conflicting_control", "invalid_turn_intelligence",
))
_FAILURES = frozenset({
    "local_semantic_rejection", "invalid_response", "empty", "blocked", "safety_blocked",
    "forbidden", "invalid_payload", "invalid_key", "read_timeout", "request_error",
    "transport", "http_408", "http_429", "http_5xx", "quota_429", "quota",
    "model_not_found", "model_overload", "model_unavailable", "overload", "unavailable",
    "permission_denied", "provider_error", "provider_overload", "quarantined",
    "lease_busy", "stale_provider_boundary", "malformed_response", "timeout",
})
_DECISIONS = frozenset({
    "repair_result", "rotate_model", "stop_result", "salvage_fresh_cheap_project",
    "rotate_key", "retry_simplified_payload", "stop_payload", "winner_found",
    "provider_success", "retry", "stop", "fallback", "not_attempted",
})
_REPAIR_REASONS = frozenset({
    "authority_unavailable", "unknown_inline_coverage", "unknown_inline_hashes",
    "actual_media_binding_mismatch", "repair_payload_unavailable", "repair_factory_failed",
    "provider_dispatch_budget", "scarce_model_budget", "repair_unavailable",
    "repair_preparation_failed", "repair_factory_unavailable", "repair_factory_refused",
    "repair_factory_missing", "repair_preparation_error",
})
_INPUT_ORIGINS = frozenset({"generate", "static_reply", "no_reply", "postback", "blocked"})
_INPUT_REASONS = frozenset({
    "opt_out", "spam_abuse", "customer_action", "reaction_only", "no_reply", "explicit_no_buy",
    "static_trigger_absent", "static_reply_invalid", "configured_reply", "model_reply",
    "rate_limited", "source_role_invalid",
})
_NOT_ATTEMPTED = frozenset({
    "circuit_open", "deadline", "duplicate_credential", "duplicate_project", "fatal_payload",
    "lease_busy", "model_overload", "model_terminal", "model_unavailable", "not_available_plan",
    "policy_stop", "quarantine", "quota_cooldown", "quota_exhausted", "sla_model_budget",
    "unconfigured", "winner_found",
})
_ACTION_KEYS = frozenset({"client_configuration_update", "manager_handoff", "rate_alert",
    "postback_decision", "normal_followups", "commerce_reduction"})


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError("positive_integer_required")
    return value


def _code(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def _model(value):
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else "unknown"


def _iso(value):
    return value.isoformat() if value is not None else None


def _unknown(reason):
    return {"proof": "unknown", "reason": reason}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _unavailable(reason):
    return {"schema_version": SCHEMA_VERSION, "status": "unavailable", "reason": reason}


def _owner(client_id):
    return IgClient.objects.filter(pk=client_id).values(
        "pk", "igsid", "privacy_erasure_started_at", "hidden_at",
    ).first()


def _sources(revision, owner):
    rows = list(IgTurnRevisionSource.objects.filter(revision=revision).select_related("message")
                .order_by("ordinal", "pk")[:SOURCE_LIMIT + 1])
    return _check_sources(revision, owner, rows)


def _check_sources(revision, owner, rows):
    from management.services.ig_turn_revisions import _source_payload

    snapshots = _mapping(revision.bundle_snapshot).get("sources")
    if (not isinstance(snapshots, list) or not 0 < len(snapshots) <= SOURCE_LIMIT
        or revision.source_count != len(snapshots)
        or _digest(revision.bundle_snapshot) != revision.snapshot_digest):
        return [], "revision_snapshot_unverified"
    if len(rows) != len(snapshots):
        return [], "source_binding_unverified"
    for row, snapshot in zip(rows, snapshots, strict=True):
        if (not isinstance(snapshot, dict) or type(snapshot.get("message_id")) is not int
            or row.message_id != snapshot.get("message_id")
            or row.source_digest != snapshot.get("source_digest")
            or row.message.client_id != revision.client_id or row.message.role != "user"
            or row.message.private_media_state not in {"", "active"}
            or row.message.sender_id != owner["igsid"]
            or _source_payload(row.message, previous=row, ordinal=row.ordinal)["source_digest"] != row.source_digest):
            return [], "source_binding_unverified"
    return rows, ""


def _attempt(row, graph, context):
    reasons = row.error_detail.split(",") if row.failure_kind in {"local_semantic_rejection", "invalid_response"} else []
    safe_reasons = list(dict.fromkeys(code for code in reasons if code in _VALIDATOR_CODES))[:12]
    diagnostic = re.fullmatch(r"response\.v1;finish=[A-Z_]+;block=[A-Z_]+;usage=(present|missing|invalid);counts=([01]{4})", row.error_detail)
    counts = [row.prompt_tokens, row.thoughts_tokens, row.candidates_tokens, row.total_tokens]
    present = diagnostic is not None and diagnostic.group(1) == "present"
    # Zero is the legacy DB default as well as a possible observation. Positive
    # counters are recorded; absent presence evidence leaves zero unknown.
    usage = {name: value if value > 0 or (present and diagnostic.group(2)[index] == "1") else None
             for index, (name, value) in enumerate(zip(("prompt", "thoughts", "candidates", "total"), counts, strict=True))}
    dispatch = row.dispatch_manifest or {}
    dispatch_proof = "unknown"
    if dispatch:
        try:
            dispatch = sanitize_dispatch_context(dispatch)
            if (dispatch["revision_id"] == int(graph.logical_turn_id.removeprefix("ig-revision:"))
                and dispatch["client_id"] == graph.client_id
                and dispatch["attempt_index"] == row.attempt_index and dispatch["model"] == row.model
                and dispatch["logical_request_digest"] == context.get("request_digest")):
                dispatch_proof = "confirmed"
        except (RequestPolicyManifestError, ValueError, TypeError, KeyError):
            pass
    return {
        "id": row.pk, "attempt_index": row.attempt_index, "candidate_index": row.candidate_index,
        "created_at": _iso(row.created_at),
        "model": _model(row.model), "state": _code(row.fsm_state, {value for value, _ in row.FsmState.choices}),
        "failure_kind": _code(row.failure_kind, _FAILURES) if row.failure_kind else None,
        "validator_layer": ("local_semantic" if row.failure_kind == "local_semantic_rejection" else
                            "schema" if any(code.startswith("schema_") for code in safe_reasons) else "unknown"),
        "validator_codes": safe_reasons, "validator_codes_complete": len(safe_reasons) == len(reasons),
        "decision": _code(row.decision, _DECISIONS) if row.decision else None,
        "not_attempted_reason": _code(row.not_attempted_reason, _NOT_ATTEMPTED) if row.not_attempted_reason else None,
        "http_status": row.http_code, "recorded_provider_started_at": _iso(row.provider_started_at),
        "finished_at": _iso(row.finished_at), "latency_ms": row.latency_ms or None,
        "dispatch_proof": dispatch_proof, "winner": bool(row.pk == graph.winner_attempt_id and row.winner_claimed
                                                         and row.fsm_state == "succeeded" and graph.terminal_resolution == "succeeded"),
        "usage": {"status": "recorded" if present or any(counts) else "unknown", "tokens": usage,
                  "estimated_prompt_tokens": row.estimated_prompt_tokens or None,
                  "reserved_prompt_tokens": row.reserved_prompt_tokens or None, "monetary_cost": None},
    }


def _generation(revision, source_ids):
    execution = f"ig-revision:{revision.pk}"
    graphs = list(GeminiRequest.objects.filter(client_id=revision.client_id,
        logical_turn_id=execution, source_execution_key=execution).order_by("pk")[:GRAPH_LIMIT + 1])
    result = {**_unknown("request_not_recorded"), "requests": [], "actual_model": None,
              "economic_limits": {"http": 8, "scarce_http": 2, "repairs": 1},
              "economic_scope": "limits_only_not_remaining_admission"}
    if len(graphs) > GRAPH_LIMIT:
        return {**result, "reason": "request_read_limit"}
    if not graphs:
        return result
    attempts = list(GeminiRequestAttempt.objects.filter(request_graph_id__in=[row.pk for row in graphs])
                    .order_by("request_graph_id", "attempt_index", "pk")[:GRAPH_LIMIT * ATTEMPT_LIMIT + 1])
    grouped = defaultdict(list)
    for attempt in attempts:
        grouped[attempt.request_graph_id].append(attempt)
    winners = []
    for graph in graphs:
        rows = grouped[graph.pk]
        if (graph.source_message_id not in source_ids or len(rows) > ATTEMPT_LIMIT
            or any(row.request_id != graph.request_id or row.client_id != revision.client_id
                   or row.logical_turn_id != execution or row.source_message_id != graph.source_message_id for row in rows)):
            return {**result, "reason": "attempt_binding_unverified"}
        try:
            policy = sanitize_request_policy_manifest(graph.policy_manifest or {})
        except (RequestPolicyManifestError, TypeError, ValueError):
            policy = {}
        context = policy.get("request_context") or {}
        captured = bool(context and context.get("revision_id") == revision.pk
                        and context.get("client_id") == revision.client_id
                        and context.get("source_message_ids") == source_ids
                        and context.get("bundle_digest") == revision.snapshot_digest)
        if context and not captured:
            return {**result, "reason": "manifest_binding_unverified"}
        safe_attempts = [_attempt(row, graph, context) for row in rows]
        winner = next((row for row in rows if row.pk == graph.winner_attempt_id and row.winner_claimed
                       and row.fsm_state == "succeeded" and graph.terminal_resolution == "succeeded"), None)
        if winner:
            winners.append((graph.request_id, winner.model))
        marker = _mapping(graph.candidate_outcomes).get("_provider_repair_preparation") or {}
        repair = _unknown("repair_not_captured")
        if isinstance(marker, dict) and marker.get("state") in {"eligible", "preparation_refused", "preparation_failed", "reserved", "http_started"}:
            repair = {"proof": "recorded", "state": marker["state"],
                      "reason": _code(marker.get("reason"), _REPAIR_REASONS) if marker.get("reason") else None,
                      "candidate_index": marker.get("candidate_index") if type(marker.get("candidate_index")) is int else None}
        result["requests"].append({
            "id": graph.request_id, "proof": "confirmed" if captured else "unknown",
            "created_at": _iso(graph.created_at), "resolved_at": _iso(graph.resolved_at),
            "reason": "captured_metadata_available" if captured else "legacy_context_uncaptured",
            "terminal_resolution": _code(graph.terminal_resolution, {"succeeded", "failed"}) if graph.terminal_resolution else None,
            "attempts": safe_attempts, "repair": repair,
            "context": {"selected_block_ids": context.get("selected_block_ids", []),
                        "omitted_blocks": context.get("omitted_blocks", []),
                        "readiness_codes": context.get("readiness_codes", [])} if captured else {},
        })
    result.update(proof="confirmed" if all(row["proof"] == "confirmed" for row in result["requests"]) else "unknown",
                  reason="joined_recorded_evidence", actual_model=_model(winners[0][1]) if len(winners) == 1 else None)
    result["winning_request_id"] = winners[0][0] if len(winners) == 1 else None
    result["winner_bindings"] = winners  # Internal only, removed before returning.
    return result


def _proposal(revision, sources, generation):
    proposal = _mapping(revision.generation_proposal)
    if not proposal:
        return _unknown("proposal_not_recorded")
    refs = [{"message_id": row.message_id, "source_digest": row.source_digest, "ordinal": row.ordinal} for row in sources]
    binding = _mapping(proposal.get("generation"))
    if (not isinstance(proposal, dict) or proposal.get("schema_version") != 1
        or _digest(proposal) != revision.generation_proposal_digest or proposal.get("sources") != refs
        or (binding.get("request_id"), binding.get("actual_model")) not in generation.get("winner_bindings", ())):
        return _unknown("proposal_binding_unverified")
    authority = _mapping(proposal.get("authority"))
    allowed = authority.get("allowed_actions") or []
    if (not isinstance(authority, dict) or not isinstance(allowed, list)
        or not isinstance(authority.get("fact_bindings"), list)
        or not isinstance(authority.get("offer_bindings"), list)):
        return _unknown("proposal_authority_unverified")
    from management.services.ig_revision_authority import ACTION_REQUIRED_CLAIMS
    return {"proof": "confirmed", "generated_at": binding.get("generated_at"),
            "allowed_actions": [item for item in allowed[:32] if item in ACTION_REQUIRED_CLAIMS],
            "fact_binding_count": len(authority.get("fact_bindings") or []),
            "offer_binding_count": len(authority.get("offer_bindings") or []),
            "facts": "captured_authority_bindings", "customer_text": "not_exposed"}


def _manager_action(revision):
    receipt = _mapping(_mapping(revision.action_receipts).get("manager_handoff"))
    if not receipt:
        return _unknown("manager_action_not_recorded")
    if (receipt.get("snapshot_digest") != revision.snapshot_digest
        or receipt.get("generation_proposal_digest") != revision.generation_proposal_digest
        or type(receipt.get("task_id")) is not int or type(receipt.get("notification_id")) is not int):
        return _unknown("manager_action_binding_unverified")
    task = IgFollowUpTask.objects.filter(pk=receipt["task_id"], client_id=revision.client_id, kind="manager_task").first()
    notification = IgBotNotification.objects.filter(pk=receipt["notification_id"], client_id=revision.client_id,
                                                   dedupe_key=f"ig-revision-case:{receipt['task_id']}").first()
    if task is None or notification is None or task.reason != "revision_case:" + str(receipt.get("case_kind")):
        return _unknown("manager_action_binding_unverified")
    return {"proof": "confirmed", "task_id": task.pk,
            "task_state": _code(task.status, {value for value, _ in task.Status.choices}),
            "approval_state": _code(task.manager_approval_status, {value for value, _ in task.ManagerApprovalStatus.choices}),
            "notification_id": notification.pk, "notification_state": _code(notification.status, {value for value, _ in notification.Status.choices}),
            "notification_delivered": bool(notification.status == "sent" and notification.telegram_message_id and notification.sent_at),
            "notification_sent_at": _iso(notification.sent_at), "customer_delivery": "separate_contract"}


def _action_receipts(revision):
    """A captured local action is not proof that its domain object completed."""
    receipts = _mapping(revision.action_receipts)
    return [{"kind": key, "proof": "recorded" if _mapping(receipts[key]).get("snapshot_digest") == revision.snapshot_digest else "unknown",
             "outcome": "recorded_intent_not_delivery"} for key in sorted(_ACTION_KEYS & receipts.keys())]


def _delivery(revision, sources, owner):
    source_namespaces = {row.message_id: row.source_namespace for row in sources}
    effects = list(IgRevisionDeliveryEffect.objects.filter(revision=revision).order_by("order_index", "pk")[:EFFECT_LIMIT + 1])
    result = {**_unknown("delivery_not_recorded"), "effects": [], "physical_state": "unknown", "normal_reply_complete": False}
    if not effects:
        return result
    if len(effects) > EFFECT_LIMIT:
        return {**result, "reason": "effect_read_limit"}
    keys = [f"ig-revision-effect:{row.pk}" for row in effects]
    transcript = {row.synthetic_event_key: row for row in InstagramBotMessage.objects.filter(
        client_id=revision.client_id, synthetic_event_key__in=keys)[:EFFECT_LIMIT + 1]}
    plans, namespaces, settings = set(), set(), set()
    mid_counts = defaultdict(int)
    all_bound = True
    for effect in effects:
        if effect.provider_message_id:
            mid_counts[effect.provider_message_id] += 1
    groups = defaultdict(list)
    for effect in effects:
        groups[effect.group].append(effect)
    groups_valid = all([row.part_index for row in parts] == list(range(len(parts)))
                       and all(row.part_count == len(parts) for row in parts) for parts in groups.values())
    for effect in effects:
        plans.add(effect.plan_digest); namespaces.add(effect.provider_namespace); settings.add(effect.settings_id_snapshot)
        bound = bool(effect.source_message_id in source_namespaces and effect.revision_snapshot_digest == revision.snapshot_digest
                     and effect.recipient_igsid == owner["igsid"] and effect.client_permission_epoch == revision.permission_epoch
                     and _digest(effect.payload) == effect.payload_digest and effect.provider_namespace
                     and effect.provider_namespace == source_namespaces[effect.source_message_id])
        all_bound = all_bound and bound
        exact = bool(bound and effect.state == "sent" and effect.provider_started_at and effect.terminal_at
                     and _ID.fullmatch(effect.provider_message_id or "") and mid_counts[effect.provider_message_id] == 1
                     and _mapping(_mapping(effect.payload).get("recipient")).get("id") == effect.recipient_igsid)
        message = transcript.get(f"ig-revision-effect:{effect.pk}")
        source = "revision_holding" if effect.purpose == "technical_holding" else "revision_reply"
        parity = bool(exact and message and message.role == "model" and message.source == source
                      and message.send_state == "sent" and message.provider_namespace == effect.provider_namespace
                      and message.provider_message_id == effect.provider_message_id
                      and message.provider_created_at == effect.terminal_at and message.send_completed_at == effect.terminal_at
                      and message.gemini_request_id == effect.generation_request_id and message.gemini_model == effect.generation_model)
        result["effects"].append({"id": effect.pk, "state": _code(effect.state, {value for value, _ in effect.State.choices}),
            "purpose": _code(effect.purpose, {"normal_reply", "technical_holding"}),
            "part_index": effect.part_index, "part_count": effect.part_count,
            "source_message_id": effect.source_message_id if bound else None,
            "receipt_proof": "confirmed" if exact else "unknown",
            "provider_message_id": effect.provider_message_id if exact else None,
            "provider_started_at": _iso(effect.provider_started_at), "terminal_at": _iso(effect.terminal_at),
            "receipt_finalized_at": _iso(effect.terminal_at) if exact else None,
            "transcript_proof": "confirmed" if parity else "unknown", "transcript_message_id": message.pk if parity else None})
    scoped = all_bound and groups_valid and len(plans) == len(namespaces) == len(settings) == 1 and "" not in plans
    sent = [row for row in result["effects"] if row["receipt_proof"] == "confirmed"]
    all_sent = scoped and len(sent) == len(effects)
    result.update(proof="confirmed" if scoped else "unknown", reason="joined_physical_receipts" if scoped else "delivery_scope_unverified",
        physical_state="sent" if all_sent else "partial" if sent else "unknown" if any(row.state == "unknown" for row in effects) else "not_confirmed",
        normal_reply_complete=bool(all_sent and all(row.actor == "bot" and row.purpose == "normal_reply" for row in effects)))
    return result


def read_revision_decision_trace(*, client_id, revision_id):
    """Exact revision identity; no latest fallback, bootstrap, provider or writes."""
    _positive(client_id); _positive(revision_id)
    owner = _owner(client_id)
    if owner is None or owner["privacy_erasure_started_at"] is not None or owner["hidden_at"] is not None:
        return _unavailable("owner_unavailable")
    revision = IgCustomerTurnRevision.objects.select_related("turn").filter(pk=revision_id, client_id=client_id).first()
    if revision is None or revision.turn.client_id != client_id:
        return _unavailable("revision_missing")
    if revision.erasure_started_at_snapshot is not None:
        return _unavailable("privacy_erasure")
    if not isinstance(revision.action_receipts, dict):
        return _unavailable("revision_receipts_unverified")
    floor = conversation_route_reset_floor(client_id)
    sources, reason = _sources(revision, owner)
    if reason:
        return _unavailable(reason)
    ids = [row.message_id for row in sources]
    receipts = revision.action_receipts or {}
    input_receipt = _mapping(receipts.get("input_decision"))
    input_valid = (input_receipt.get("version") == "revision-input-v1"
                   and input_receipt.get("snapshot_digest") == revision.snapshot_digest
                   and input_receipt.get("source_message_ids") == ids
                   and input_receipt.get("origin") in _INPUT_ORIGINS)
    decision = {"proof": "confirmed", "origin": input_receipt["origin"],
                "reason": _code(input_receipt.get("reason"), _INPUT_REASONS), "why_bot": "captured_input_decision"} if input_valid else _unknown("input_decision_not_captured")
    generation = _generation(revision, ids)
    proposal = _proposal(revision, sources, generation)
    generation.pop("winner_bindings", None)
    delivery = _delivery(revision, sources, owner)
    if (input_valid and input_receipt["origin"] == "no_reply" and delivery["reason"] == "delivery_not_recorded"
        and not revision.generation_proposal_digest and generation["reason"] == "request_not_recorded"):
        delivery.update(proof="confirmed", reason="captured_no_reply_decision", physical_state="no_reply")
    semantic_receipt = response_coverage(revision) if isinstance(receipts.get("response_coverage", {}), dict) else {}
    semantic = _unknown("semantic_coverage_not_recorded")
    if semantic_receipt:
        verified = semantic_receipt.get("disposition") in {"complete", "waiting_on_customer", "recovery", "manual"} and semantic_receipt.get("remaining") != ["coverage_unverified"]
        semantic = {"proof": "confirmed" if verified else "unknown", "disposition": semantic_receipt.get("disposition") if verified else "unknown",
                    "remaining_count": len(semantic_receipt.get("remaining") or []) if verified else None,
                    "covered_count": len(semantic_receipt.get("covered") or []) if verified else None,
                    "next_selector": semantic_receipt.get("next_selector") if verified and semantic_receipt.get("next_selector") in {"model", "size", "color", "fit", "variant", "options", "quantity", "product"} else None,
                    "customer_reply_complete": bool(verified and semantic_receipt.get("disposition") == "complete" and delivery["normal_reply_complete"])}
    result = {"schema_version": SCHEMA_VERSION, "status": "available",
              "revision": {"id": revision.pk, "turn_id": revision.turn_id, "parent_id": revision.parent_id,
                           "created_at": _iso(revision.created_at),
                           "state": revision.state, "origin": revision.origin,
                           "scope": "historical" if any(pk < floor for pk in ids) else "current", "reset_floor": floor},
              "context": {"proof": "confirmed", "source_message_ids": ids, "captured_at": _iso(revision.sealed_at)},
              "decision": decision, "generation": generation, "proposal": proposal,
              "actions": {"manager_case": _manager_action(revision), "recorded_receipts": _action_receipts(revision)},
              "delivery": delivery, "semantic": semantic}
    result["decision"]["routing_reason_codes"] = None  # These were not captured by this revision manifest.
    result["coverage"] = {stage: result[stage].get("proof", "unknown")
                          for stage in ("context", "decision", "generation", "proposal", "delivery", "semantic")}
    result["coverage"]["manager_case"] = result["actions"]["manager_case"]["proof"]
    # Privacy/reset/source scope is re-read after all joined evidence, rather
    # than relying on a cached customer object across an erasure/reset race.
    fresh_owner = _owner(client_id)
    fresh_revision = IgCustomerTurnRevision.objects.filter(pk=revision_id, client_id=client_id).first()
    if (fresh_owner != owner or fresh_revision is None or fresh_revision.erasure_started_at_snapshot is not None
        or fresh_revision.snapshot_digest != revision.snapshot_digest or fresh_revision.bundle_snapshot != revision.bundle_snapshot
        or conversation_route_reset_floor(client_id) != floor):
        return _unavailable("read_scope_changed")
    _, changed = _sources(fresh_revision, fresh_owner)
    return _unavailable(changed) if changed else result


def read_revision_decision_trace_index(*, client_id, before_revision_id=None, limit=10):
    """Revision-owned cursor includes static/no-reply even without Gemini rows."""
    _positive(client_id)
    if type(limit) is not int or not 1 <= limit <= INDEX_LIMIT:
        raise ValueError("index_limit_invalid")
    if before_revision_id is not None:
        _positive(before_revision_id)
    owner = _owner(client_id)
    if owner is None or owner["privacy_erasure_started_at"] is not None or owner["hidden_at"] is not None:
        return _unavailable("owner_unavailable")
    floor = conversation_route_reset_floor(client_id)
    queryset = IgCustomerTurnRevision.objects.filter(client_id=client_id, turn__client_id=client_id,
                                                      erasure_started_at_snapshot__isnull=True).order_by("-pk")
    if before_revision_id is not None:
        queryset = queryset.filter(pk__lt=before_revision_id)
    rows = list(queryset[:limit + 1])
    sources_by_revision = defaultdict(list)
    for source in IgTurnRevisionSource.objects.filter(revision_id__in=[row.pk for row in rows[:limit]])\
            .select_related("message").order_by("revision_id", "ordinal", "pk")[:INDEX_LIMIT * SOURCE_LIMIT + 1]:
        sources_by_revision[source.revision_id].append(source)
    items = []
    for row in rows[:limit]:
        sources, source_reason = _check_sources(row, owner, sources_by_revision[row.pk])
        ids = [source.message_id for source in sources]
        valid = not source_reason
        receipt = _mapping(_mapping(row.action_receipts).get("input_decision"))
        origin = receipt.get("origin") if valid and receipt.get("snapshot_digest") == row.snapshot_digest and receipt.get("source_message_ids") == ids and receipt.get("version") == "revision-input-v1" else None
        items.append({"revision_id": row.pk, "state": row.state, "origin": row.origin,
                      "input_origin": origin if origin in _INPUT_ORIGINS else "unknown",
                      "sealed_at": _iso(row.sealed_at), "scope": ("historical" if any(pk < floor for pk in ids) else "current") if valid else "unknown"})
    if _owner(client_id) != owner or conversation_route_reset_floor(client_id) != floor:
        return _unavailable("read_scope_changed")
    return {"schema_version": SCHEMA_VERSION, "status": "available", "items": items,
            "has_more": len(rows) > limit, "next_before_revision_id": items[-1]["revision_id"] if len(rows) > limit and items else None}
