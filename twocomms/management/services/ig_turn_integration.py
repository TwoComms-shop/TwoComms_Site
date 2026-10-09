"""Request-local adapters over the captured context and state read models."""
from __future__ import annotations

from dataclasses import dataclass
import json

from django.conf import settings
from django.utils import timezone

from management.services.ig_client_state_card import assemble_client_state
from management.services.ig_turn_capture import capture_revision_context, detached_payload


@dataclass(frozen=True)
class PreparedTurnContext:
    context: object
    state: object
    history: tuple
    dynamic_notes: dict
    request_metadata: dict
    current_text: str
    policy_inputs: object


def legacy_revision_context_metadata(revision, *, mode="legacy", reason="context_legacy_mode"):
    """Describe actual compatibility assembly without claiming a state capture."""
    sources = revision.bundle_snapshot.get("sources") or []
    return {"revision_id": revision.pk, "client_id": revision.client_id,
        "source_message_ids": [row["message_id"] for row in sources],
        "history_message_ids": [], "reset_floor": 0,
        "bundle_digest": revision.snapshot_digest, "builder_version": "legacy_revision.v1",
        "effective_mode": mode, "selected_block_ids": [], "omitted_blocks": [],
        "readiness_codes": [reason], "budgets": {"source_count": len(sources)},
        "view_versions": {}, "media": {"admitted_part_ids": [], "omitted_part_ids": [],
                                        "unavailable_part_ids": []}}


def prepare_revision_turn_context(revision, *, generation_boundary, collection,
                                  settings_row, publication, now=None):
    """Capture once; all consumers receive defensive views of that capture."""
    captured_at = now or timezone.now()
    settings_row.turn_intelligence_mode = getattr(settings, "IG_TURN_CONTEXT_MODE", "legacy")
    context = capture_revision_context(revision, generation_boundary=generation_boundary,
        collection=collection, settings_row=settings_row, publication=publication, now=captured_at)
    boundary = detached_payload(context.boundary)
    components = detached_payload(context.components)
    from management.services.ig_captured_policy_inputs import capture_policy_inputs
    sources = revision.bundle_snapshot["sources"]
    policy_inputs = capture_policy_inputs(revision.client_id, boundary=boundary,
        sources=sources, history=detached_payload(context.captured_history),
        response_plan=generation_boundary.response_plan, captured_at=captured_at)
    state_inputs = {}
    for key, value in components.items():
        if not isinstance(value, dict):
            continue
        if key == "source_selection":
            state_inputs[key] = value.get("capture") or {}
            state_inputs["source_selection_binding"] = value.get("scope") or {}
        elif key == "readiness":
            state_inputs[key] = {**(value.get("capture") or {}), "scope": value.get("scope") or {}}
        elif key == "source_cart":
            state_inputs[key] = value.get("capture") or {}
        elif key in {"payment_truth", "slots"}:
            state_inputs[key] = value["capture"] if "capture" in value else value
        elif key in {"consent_state", "narrative", "manager_notes"}:
            state_inputs[key] = value
    observation_omissions = components.get("observation_omissions") or []
    state_inputs["observation_omissions"] = (observation_omissions.get("capture") or []
        if isinstance(observation_omissions, dict) else observation_omissions)
    signals = components.get("signals") or {}
    signal_items = signals.get("items") or []
    if signal_items:
        state_inputs.setdefault("slots", {})["conversation_signals"] = {
            "value": [{"type": item["kind"], "source_message_id": item["source_message_id"]}
                      for item in signal_items if item.get("source_message_id")],
            "status": "confirmed", "authority": "derived",
            "source_refs": [{"kind": "message", "id": item["source_message_id"]}
                            for item in signal_items if item.get("source_message_id")],
            "scope": signals.get("scope"), "observed_at": boundary["watermark"]["event_at"],
        }
    state_inputs.setdefault("slots", {}).update(policy_inputs.state_slots)
    from management.services.ig_review_reward_context import SLOT, VERSION as REVIEW_REWARD_CONTEXT_VERSION, capture_review_reward_context
    state_inputs["slots"][SLOT] = capture_review_reward_context(revision.client_id,
        boundary=boundary, captured_at=captured_at)
    state = assemble_client_state(boundary=boundary, components=state_inputs,
        captured_at=captured_at)
    if state.as_dict()["status"] != "captured" and (state_inputs.get("source_cart") or {}).get("status") == "captured":
        from management.services.ig_turn_intelligence import TurnContextError
        raise TurnContextError("source_cart_state_unavailable")
    metadata = detached_payload(context.metadata)
    budget = metadata.get("budget") or {}
    versions = metadata.get("view_versions") or {}
    safe_versions = {key: str(value) for key, value in versions.items() if value not in (None, "")}
    safe_versions["state_view_version"] = state.as_dict()["schema"]
    safe_versions["canonical_selection"] = "source-selection.v1"
    safe_versions["conversation_agreement"] = "conversation-agreement.v1"
    safe_versions["receipt_observation"] = "payment-observation.v1"
    from management.services.approved_public_facts import APPROVED_PUBLIC_FACTS_VERSION
    safe_versions["facts_version"] = APPROVED_PUBLIC_FACTS_VERSION
    safe_versions["ugc_review_benefit"] = REVIEW_REWARD_CONTEXT_VERSION
    sources = revision.bundle_snapshot["sources"]
    admitted = [str(part.source_part_id) for part in collection.parts]
    unavailable = [str(part["source_part_id"]) for source in sources
                   for part in source.get("media_parts") or []
                   if part.get("source_part_id") and part["source_part_id"] not in admitted]
    request_metadata = {
        "revision_id": revision.pk, "client_id": revision.client_id,
        "source_message_ids": list(boundary["source_ids"]),
        "history_message_ids": list(metadata.get("captured_history_ids") or []),
        "reset_floor": boundary["reset_floor"], "bundle_digest": revision.snapshot_digest,
        "builder_version": metadata["builder_version"], "effective_mode": metadata["effective_mode"],
        "selected_block_ids": list(metadata.get("selected_block_ids") or []),
        "omitted_blocks": list(metadata.get("omitted_blocks") or []),
        "readiness_codes": list(metadata.get("readiness_codes") or []),
        "budgets": {"builder_chars": int(budget.get("selected_chars") or 0),
                    "history_entries": int(budget.get("history_entries") or 0),
                    "source_count": int(budget.get("source_count") or 0),
                    "media_parts": int(budget.get("media_parts") or 0)},
        "view_versions": safe_versions,
        "memory_snapshot": metadata.get("memory_snapshot") or {},
        "media": {"admitted_part_ids": admitted, "omitted_part_ids": [],
                  "unavailable_part_ids": unavailable},
    }
    readiness = generation_boundary.response_plan.readiness_snapshot
    dynamic_notes = {
        "automation": policy_inputs.automation_note,
        "checkout_readiness": "[CAPTURED CHECKOUT READINESS]\n" + json.dumps(
            readiness, ensure_ascii=False, separators=(",", ":")),
        "shown_products": "", "funnel_journal": "",
        "objection_lifecycle": "[CURRENT SOURCE OBJECTION]\n" + json.dumps(
            {"present": context.facts.objection_present,
             "source_message_ids": list(boundary["source_ids"])}, separators=(",", ":")),
    }
    return PreparedTurnContext(context, state, tuple(detached_payload(context.provider_history)),
        dynamic_notes, request_metadata, "\n".join(str(source.get("text") or "") for source in sources),
        policy_inputs)
