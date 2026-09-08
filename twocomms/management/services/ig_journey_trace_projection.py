"""Read a source-bound interpretation without promoting it to business truth."""
from copy import deepcopy
import hashlib
import json
import re

from django.db.models import Q
from django.db import DatabaseError

from management.models import (
    IgClient, IgCommercialEpisode, IgFunnelResetAudit, IgJourneyTraceSnapshot, InstagramBotMessage,
)
from management.services.ig_journey_catalogue import journey_catalogue
from management.services.ig_journey_trace_contract import validate_normalized_journey_trace


REASON_LABELS = {
    "entered": "Початок звернення", "product_selected": "Обговорили товар",
    "configuration_discussed": "Обговорили склад", "custom_requested": "Власний принт",
    "mockup_discussed": "Обговорили макет", "certificate_presented": "Згадано сертифікат",
    "awaiting_stock": "Очікування наявності", "alternative_considered": "Розглянули інший варіант",
    "consent_discussed": "Обговорили дозвіл", "payment_discussed": "Обговорили оплату",
    "payment_problem": "Складність з оплатою", "external_order_reported": "Згадано зовнішнє замовлення",
    "fulfillment_discussed": "Обговорили виконання", "objection_raised": "Є заперечення",
    "objection_addressed": "Обговорили заперечення", "manager_discussion": "Розмова з менеджером",
    "employment_request": "Запит щодо роботи", "collaboration_request": "Запит щодо співпраці",
    "information_answered": "Обговорили відповідь", "changed_request": "Зміна запиту",
    "repeat_interest": "Повторний інтерес",
}
_DISCUSSION_LABELS = {
    "settlement": "Обговорення розрахунку", "fulfillment": "Обговорення виконання",
    "mockup_current_acceptance": "Обговорення макета", "channel_grant_checked": "Обговорення дозволу",
    "prize_decision": "Обговорення призу", "reward_entitlement": "Обговорення права на нагороду",
    "reward_delivery": "Обговорення нагороди", "reward_use": "Обговорення використання нагороди",
    "information_resolved": "Відповідь на запитання", "spam_confirmed": "Негативне звернення",
    "business_decision": "Обговорення з менеджером", "employment_response": "Обговорення роботи",
}
_ROLES = {"user", "manager", "model"}
_HEX = re.compile(r"[0-9a-f]{64}")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def _read(client_id, episode_id, is_history):
    # Do not trust a caller's possibly stale in-memory privacy state.
    if not IgClient.objects.filter(pk=client_id, hidden_at__isnull=True,
                                   privacy_erasure_started_at__isnull=True).exists():
        return None, "client_unavailable"
    if episode_id is not None and not IgCommercialEpisode.objects.filter(pk=episode_id, client_id=client_id).exists():
        return None, "scope_rejected"
    if is_history and episode_id is None:
        return None, "scope_rejected"
    scope = Q(commercial_episode_id=episode_id) if is_history else Q(commercial_episode_id__isnull=True)
    if not is_history and episode_id is not None:
        scope |= Q(commercial_episode_id=episode_id)
    row = IgJourneyTraceSnapshot.objects.filter(scope, client_id=client_id).order_by("-id").first()
    if row is None:
        return None, "missing"
    trace = row.trace
    if (row.schema_version != "journey-trace.v1" or row.producer_version != "journey-trace-store.v1"
            or not validate_normalized_journey_trace(trace)
            or trace["status"] not in {"partial", "interpretation_only"} or not trace["steps"]
            or trace["watermark"] != row.watermark_message_id
            or any(not isinstance(value, str) or not _HEX.fullmatch(value)
                   for value in (row.source_digest, row.trace_digest, row.snapshot_key))
            or _digest(trace) != row.trace_digest
            or _digest({"client_id": client_id, "episode_id": row.commercial_episode_id,
                        "watermark": row.watermark_message_id, "source_digest": row.source_digest,
                        "prompt_version": row.prompt_version, "schema_version": row.schema_version}) != row.snapshot_key):
        return None, "schema_rejected"
    latest = InstagramBotMessage.objects.filter(client_id=client_id, role__in=_ROLES).order_by("-id").values_list("id", flat=True).first()
    if latest is None or latest < row.watermark_message_id:
        return None, "stale"
    row.trace_freshness = "current" if latest == row.watermark_message_id else "new_messages"
    evidence = {ref["message_id"]: ref for step in trace["steps"] for ref in step["evidence"]}
    reset = IgFunnelResetAudit.objects.filter(client_id=client_id).order_by("-id").values("reset_after_message_id", "created_at").first()
    if reset and (min(evidence) <= reset["reset_after_message_id"]
                  or row.created_at <= reset["created_at"] or row.analyzed_at <= reset["created_at"]):
        return None, "reset_boundary"
    sources = list(InstagramBotMessage.objects.filter(client_id=client_id, pk__in=evidence).values("id", "role", "text"))
    if len(sources) != len(evidence) or sum(len(source["text"]) for source in sources) > 1_000_000:
        return None, "source_rejected"
    for source in sources:
        ref = evidence[source["id"]]
        try:
            digest = hashlib.sha256(" ".join(source["text"].split()).encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            return None, "source_rejected"
        if source["role"] != ref["role"] or digest != ref["source_text_sha256"]:
            return None, "source_rejected"
    return row, trace["status"]


def append_journey_trace(graph, *, client_id, episode_id=None, is_history=False):
    """Append cited discussion nodes/edges; never write journal visits or facts.

    A client-scoped trace may annotate the current view, but does not acquire
    episode ownership by being shown beside a purchase. No older row fallback.
    """
    try:
        row, status = _read(client_id, episode_id, is_history)
    except DatabaseError:
        row, status = None, "unavailable"
    result = deepcopy(graph)
    result.setdefault("coverage", {})["transcript_reconstruction"] = status
    if row is None:
        return result
    trace = row.trace
    metadata = {"snapshot_id": row.pk, "authority": "none", "provenance": "transcript_reconstruction",
                "scope": "client" if row.commercial_episode_id is None else "episode",
                "episode_id": row.commercial_episode_id, "status": status, "freshness": row.trace_freshness}
    result["transcript_reconstruction"] = {**metadata, "coverage": deepcopy(trace["coverage"]),
                                          "label": "За перепискою"}
    definitions = {item["key"]: item for item in journey_catalogue()["definitions"]}
    anchors, trail = {}, []
    for step in trace["steps"]:
        refs = [{"kind": "message", "id": ref["message_id"], "role": ref["role"]} for ref in step["evidence"]]
        for key in (step["from_node"], step["to_node"]):
            if not key:
                continue
            if key not in anchors:
                matches = [node for node in result["nodes"] if node.get("semantic_key") == key]
                # Ambiguous actual identities are not merged or silently rebound.
                if len(matches) > 1:
                    result["coverage"]["transcript_reconstruction"] = "ambiguous_anchor"
                    return {**deepcopy(graph), "coverage": result["coverage"]}
                if matches:
                    node = matches[0]
                else:
                    definition = definitions[key]
                    label = _DISCUSSION_LABELS.get(key, definition["label"])
                    node = {"id": "trace:" + key, "semantic_key": key, "label": label, "short_label": label,
                            "state": "partial", "current": False, "presentation_kind": "interpretation",
                            "summary": "За перепискою. Обговорення не підтверджує виконання етапу.",
                            "facts": [], "evidence_refs": [], "timers": []}
                    if definition.get("implementation_status") == "planned":
                        node.update({field: definition[field] for field in ("implementation_status", "implementation_note")})
                    result["nodes"].append(node)
                node["transcript_interpretation"] = {**metadata, "evidence_refs": []}
                anchors[key] = node
                trail.append(node["id"])
            node = anchors[key]
            existing = node["transcript_interpretation"]["evidence_refs"]
            existing.extend(ref for ref in refs if ref not in existing)
            if node.get("presentation_kind") == "interpretation":
                node["evidence_refs"] = list(existing)
        target = anchors[step["to_node"]]
        summary = step.get("summary", "")
        if summary and target.get("presentation_kind") == "interpretation":
            target["summary"] = summary
        if step["to_node"] == "stock_wait" and target.get("presentation_kind") == "interpretation":
            target["waiting"] = {"kind": "indefinite", "label": "За перепискою: очікуємо наявність; строк невідомий. Автосповіщення ще не налаштовано.",
                                 "evidence_refs": refs}
        if not step["from_node"]:
            continue
        signature = (step["from_node"], step["to_node"], step["kind"], step["reason_code"])
        identifier = "trace-edge:" + str(row.pk) + ":" + ":".join(signature) + ":" + _digest(summary)[:12]
        edge = next((item for item in result["edges"] if item["id"] == identifier), None)
        if edge:
            edge["repeated_count"] += 1
            edge["evidence_refs"].extend(ref for ref in refs if ref not in edge["evidence_refs"])
        else:
            result["edges"].append({"id": identifier, "from_node_id": anchors[step["from_node"]]["id"],
                "to_node_id": target["id"], "relation": "transcript_interpretation", "interpretation_kind": step["kind"],
                "reason_code": step["reason_code"], "reason_label": REASON_LABELS[step["reason_code"]],
                "summary": summary,
                "tone": "danger" if step["kind"] in {"return", "negative"} else "warning" if step["kind"] == "retry" else "recorded",
                "authority": "none", "provenance": "transcript_reconstruction", "evidence_refs": refs, "repeated_count": 1})
    result["trace_node_ids"] = trail
    # A recorded accepted route or current business evidence is stronger than an
    # interpreted topic. Merely receiving the first message is not such evidence.
    stronger = any(node.get("route_focus") or (node.get("current") and any(
        isinstance(fact.get("source"), str) and fact["source"].endswith(".current")
        for fact in node.get("facts", []))) for node in result["nodes"])
    focus = anchors.get(trace["current_node"])
    if focus and not stronger and row.trace_freshness == "current":
        for node in result["nodes"]:
            node["current"] = False
        focus["current"] = True
        focus["interpreted_focus"] = True
    return result
