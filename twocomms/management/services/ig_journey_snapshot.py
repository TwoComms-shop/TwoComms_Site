"""Bounded, read-only purchase map over producers available at migration 0201.

Milestones describe observations, not current checkout readiness. In particular,
neither a client stage nor a later payment completes earlier guide nodes. This
adapter intentionally does not invoke the (not yet live) node projector or any
episode materializer. Its returned JSON contains no raw evidence or action URLs.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from management.models import (
    IgCommercialEpisode, IgCommercialEpisodeEvent, IgConversationRouteDecision,
    IgFunnelStepEvent, IgObjectionAttempt,
)
from management.services.ig_conversation_routes import conversation_route_reset_floor


EPISODE_LIMIT = 20
EVENT_LIMIT = 50
LINE_LIMIT = 10
ROUTE_DECISION_LIMIT = 24
ROUTE_SOURCE_MESSAGE_LIMIT = 64
GUIDE = (
    ("inquiry", "Звернення"), ("selection", "Підбір/Бриф"),
    ("terms", "Умови"), ("offer", "Пропозиція/Макет"),
    ("payment", "Розрахунок"), ("fulfillment", "Виконання"),
)
STEP_NODES = {
    "conversation_started": "inquiry", "bot_replied_first": "inquiry",
    "product_pinned": "selection", "variant_selected": "selection",
    "price_quoted": "terms", "discount_offered": "terms",
    "paylink_issued": "offer", "paylink_viewed": "offer",
    "payment_confirmed": "payment", "order_created": "fulfillment",
    "ttn_created": "fulfillment", "delivered": "fulfillment",
}
STEP_LABELS = dict(IgFunnelStepEvent.Type.choices)
EPISODE_LABELS = {
    "opened": "Цикл відкрито", "review_detached": "Перевірку відв’язано",
    "review_attached": "Перевірку пов’язано", "payment_review": "Перевірку оплати оновлено",
    "review_superseded": "Перевірку замінено",
    "payment_updated": "Дані оплати оновлено", "order_bound": "Замовлення пов’язано",
    "fulfillment_updated": "Дані виконання оновлено",
    "repeat_evidence_extended": "Доповнено доказ повтору",
    "historical_paid_archived": "Історичну оплату заархівовано",
    "historical_purchase_corrected": "Історичну покупку виправлено",
    "existing_order_linked": "Існуюче замовлення пов’язано",
    "stage_transition": "Змінено фокус циклу",
}
LIFECYCLE_EDGE_TYPES = frozenset({
    "order_bound", "fulfillment_updated", "historical_paid_archived",
    "historical_purchase_corrected",
})
EPISODE_STATES = frozenset({"active", "order_created", "fulfilled", "cancelled", "lost"})
EPISODE_STATE_LABELS = dict(IgCommercialEpisode.State.choices)
REPEAT_KIND_LABELS = dict(IgCommercialEpisode.RepeatKind.choices)
LIFECYCLE_EDGE_SOURCES = {
    "order_bound": frozenset({"order_resolution", "provider_auto", "manager_review", "linked_existing"}),
    "fulfillment_updated": frozenset({"order_truth", "order_signal"}),
    "historical_paid_archived": frozenset({"manager_resolution"}),
    "historical_purchase_corrected": frozenset({"manager_correction"}),
}
GRAPH_EPISODE_STATE = {
    "active": "partial", "order_created": "partial", "fulfilled": "complete",
    "cancelled": "invalidated", "lost": "invalidated",
}
GRAPH_OBJECTION_STATE = {
    "open": "partial", "handled": "partial", "resolved": "complete", "abandoned": "invalidated",
}
GRAPH_ATTEMPT_STATE = {
    "pending": "partial", "accepted": "complete", "purchased": "complete",
    "ignored": "invalidated", "re_objected": "partial", "silent": "partial", "escalated": "partial",
}
ATTEMPT_RESULT_LABELS = dict(IgObjectionAttempt.Result.choices)
EPISODE_FIELDS = (
    "id", "client_id", "sequence", "open_slot", "state", "repeat_kind",
    "opened_at", "closed_at", "updated_at", "intended_order_id",
    "primary_payment_review_id", "stage_snapshot", "product_snapshot",
    "price_snapshot", "payment_snapshot", "fulfillment_snapshot",
)
SAFE_ACTORS = frozenset({
    "customer", "bot", "manager", "provider", "historical_backfill",
    "order_truth", "order_resolution", "linked_existing", "auto_created",
    "payment_review", "provider_projection", "manager_decision",
    "payment_reconciliation", "commercial_flow", "conversation_analysis",
})
EVIDENCE_IDS = (
    "message_id", "product_id", "color_variant_id", "order_id", "review_id",
    "decision_id", "projection_id", "provider_event_id", "proposal_id",
)


class InvalidJourneyEpisode(ValueError):
    """Requested episode is invalid, missing, or not owned by this client."""


def _iso(value):
    return value.isoformat() if value is not None else ""


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _positive_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value) and int(value) > 0:
        return int(value)
    return None


def _money(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and Decimal("0") <= number <= Decimal("1000000000"):
            return f"{number:.2f}"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def _short(value, length=100):
    # Known product labels only; never event source, raw text, token or URL fields.
    if not isinstance(value, str) or "http" in value.casefold() or "://" in value:
        return ""
    return " ".join(value.split())[:length]


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return ""
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _amount_label(value, snapshot):
    currency = snapshot.get("currency")
    return f"{value} {currency}" if currency in {"UAH", "USD", "EUR"} else f"{value} · валюта не вказана"


def _ref(kind, identifier):
    return {"kind": kind, "id": identifier}


def _fact(node, key, label, value, *, source, ref=None, state="partial", tone="neutral", captured_at=""):
    fact = {
        "id": f"{node['id']}:{key}", "label": label, "value": value,
        "state": state, "tone": tone, "classification": "context", "source": source, "captured_at": captured_at,
        "evidence_refs": [ref] if ref else [],
    }
    node["facts"].append(fact)
    if ref and ref not in node["evidence_refs"]:
        node["evidence_refs"].append(ref)
    if node["state"] == "open":
        node["state"] = "partial"
    node["coverage"] = "partial"
    return fact


def _selector(episode):
    return {
        "id": episode["id"], "sequence": episode["sequence"],
        "label": f"Покупка {episode['sequence']}",
        "state": episode["state"], "repeat_kind": episode["repeat_kind"],
        "current": episode["open_slot"] == 1,
        "opened_at": _iso(episode["opened_at"]), "closed_at": _iso(episode["closed_at"]),
    }


def _event(row, kind):
    evidence = _mapping(row["evidence"])
    event_type = row["event_type"]
    known = event_type in (STEP_LABELS if kind == "funnel_event" else EPISODE_LABELS)
    actor = row.get("actor", row.get("source", ""))
    verified_payment = bool(
        kind == "funnel_event" and event_type == "payment_confirmed"
        and actor == "provider" and _positive_id(evidence.get("provider_event_id"))
        and _positive_id(evidence.get("projection_id"))
    )
    result = {
        "id": f"{kind}:{row['id']}", "type": event_type if known else "other",
        "label": str((STEP_LABELS if kind == "funnel_event" else EPISODE_LABELS).get(
            event_type, "Подія циклу")),
        "node_id": STEP_NODES.get(event_type) if kind == "funnel_event" else None,
        "occurred_at": _iso(row.get("occurred_at", row["created_at"])),
        "recorded_at": _iso(row["created_at"]),
        "actor": actor if actor in SAFE_ACTORS else "unknown",
        "provenance": "provider_event" if verified_payment else "recorded_observation",
        "is_backfilled": bool(row.get("is_backfilled", False)),
        "evidence_refs": [_ref(kind, row["id"])],
        "values": {
            key: identifier for key in EVIDENCE_IDS
            if (identifier := _positive_id(evidence.get(key))) is not None
        },
    }
    if kind == "episode_event" and event_type in EPISODE_LABELS:
        result["episode_event_type"] = event_type
        from_state, to_state = str(row.get("from_state") or ""), str(row.get("to_state") or "")
        if (event_type in LIFECYCLE_EDGE_TYPES and actor in LIFECYCLE_EDGE_SOURCES[event_type]
                and from_state in EPISODE_STATES and to_state in EPISODE_STATES):
            result["episode_states"] = {"from": from_state, "to": to_state}
    return result


def _history(episode_id, client_id):
    steps = IgFunnelStepEvent.objects.filter(episode_id=episode_id, episode__client_id=client_id)
    commercial = IgCommercialEpisodeEvent.objects.filter(episode_id=episode_id, episode__client_id=client_id)
    total = steps.count() + commercial.count()
    rows = [
        _event(row, "funnel_event") for row in steps.order_by("-occurred_at", "-id").values(
            "id", "event_type", "actor", "occurred_at", "created_at", "evidence", "is_backfilled",
        )[:EVENT_LIMIT]
    ]
    rows.extend(_event(row, "episode_event") for row in commercial.order_by(
        "-created_at", "-id",
    ).values("id", "event_type", "from_state", "to_state", "source", "created_at", "evidence")[:EVENT_LIMIT])
    rows.sort(key=lambda row: (row["occurred_at"], row["id"].split(":")[0], int(row["id"].split(":")[1])), reverse=True)
    rows = list(reversed(rows[:EVENT_LIMIT]))
    # Commercial events often mirror funnel milestones. Only the typed funnel
    # producer supplies visits; neither stream supplies semantic transition edges.
    visits = [{
        "id": f"visit:{row['id']}", "node_id": row["node_id"],
        "event_id": row["id"], "occurred_at": row["occurred_at"],
        "kind": "observed_milestone",
    } for row in rows if row["node_id"]]
    return {
        "events": rows, "total": total, "has_more": total > len(rows),
        "coverage": "partial", "visits": visits, "edges": [],
        "edge_coverage": "missing_source",
    }


def _graph_node(identifier, label, *, semantic_key=None, state="partial", current=False,
                summary="", facts=None, evidence_refs=None, rank=0, lane=0):
    return {
        "id": identifier, "semantic_key": semantic_key, "label": label,
        "state": state, "current": bool(current), "summary": summary,
        "facts": facts or [], "evidence_refs": evidence_refs or [],
        "layout": {"rank": rank, "lane": lane},
    }


def _route_key(value):
    """Return a registered conversational identity, never an arbitrary JSON key."""
    if not isinstance(value, str):
        return ""
    kind, separator, subtype = value.partition(":")
    if not separator:
        return ""
    from management.services.ig_customer_route_contract import (
        COLLABORATION_SUBTYPES, KINDS,
    )
    if kind not in KINDS:
        return ""
    if subtype not in (COLLABORATION_SUBTYPES if kind == "collaboration" else {"none"}):
        return ""
    return value


def _route_ref_ids(transitions):
    """Extract bounded syntactic message references from accepted delta records."""
    result = []
    for transition in transitions if isinstance(transitions, list) else []:
        if not isinstance(transition, dict):
            continue
        for identifier in transition.get("evidence_message_ids", []):
            identifier = _positive_id(identifier)
            if identifier and identifier not in result:
                result.append(identifier)
                if len(result) == ROUTE_SOURCE_MESSAGE_LIMIT:
                    return result
    return result


def _safe_route_transition(value, source_refs):
    """Expose only an accepted, typed delta and owned USER-message references."""
    if not isinstance(value, dict):
        return None
    operation = value.get("operation")
    key = _route_key(value.get("key"))
    if operation not in {"open", "withdraw", "correct", "focus"} or not key:
        return None
    from_key = _route_key(value.get("from_key")) if operation == "focus" else ""
    reason_code = value.get("reason_code")
    if reason_code not in {"customer_intent", "customer_correction"}:
        reason_code = "customer_intent"
    refs = []
    for identifier in value.get("evidence_message_ids", []):
        identifier = _positive_id(identifier)
        ref = source_refs.get(identifier)
        if ref and ref not in refs:
            refs.append(ref)
    result = {
        "operation": operation, "key": key, "reason_code": reason_code,
        "evidence_refs": refs,
    }
    if operation == "focus":
        # An empty source is a valid first focus, but never a graph endpoint.
        result["from_key"] = from_key
    return result


def _safe_active_intents(value):
    intents = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        key = _route_key(item.get("key"))
        if not key:
            continue
        kind, subtype = key.split(":", 1)
        intent = {"key": key, "kind": kind, "subtype": subtype}
        if intent not in intents:
            intents.append(intent)
    return sorted(intents, key=lambda item: item["key"])


def _conversation_route(client_id):
    """Read the newest explicit-reset journal scope without interpreting it.

    The journal is an append-only accepted source.  Its historical commercial
    episode association, analysis payloads and revision payloads are purposely
    not read here: a current customer dialogue is independent of purchase
    materialization and model output has no route authority in this adapter.
    """
    from management.models import InstagramBotMessage

    reset_floor = conversation_route_reset_floor(client_id)
    rows = list(IgConversationRouteDecision.objects.filter(
        client_id=client_id, reset_floor=reset_floor,
    ).order_by("-sequence", "-id").values(
        "id", "sequence", "previous_id", "watermark_message_id", "active_intents",
        "focus_key", "transitions", "reason_code", "occurred_at", "recorded_at",
    )[:ROUTE_DECISION_LIMIT + 1])
    has_more = len(rows) > ROUTE_DECISION_LIMIT
    rows = list(reversed(rows[:ROUTE_DECISION_LIMIT]))

    # Prefer evidence from the newest decisions when the bounded source-ref
    # budget is exhausted.  This does not alter the accepted route itself.
    source_ids = []
    for row in reversed(rows):
        for identifier in _route_ref_ids(row["transitions"]):
            if identifier not in source_ids:
                source_ids.append(identifier)
                if len(source_ids) == ROUTE_SOURCE_MESSAGE_LIMIT:
                    break
        if len(source_ids) == ROUTE_SOURCE_MESSAGE_LIMIT:
            break
    source_refs = {
        row["id"]: _ref("message", row["id"])
        for row in InstagramBotMessage.objects.filter(
            client_id=client_id, role=InstagramBotMessage.Role.USER,
            pk__gte=reset_floor, pk__in=source_ids,
        ).values("id")
    } if source_ids else {}

    decisions, transitions = [], []
    for row in rows:
        record = {
            "id": row["id"], "sequence": row["sequence"],
            "watermark_message_id": row["watermark_message_id"],
            "reason_code": row["reason_code"] if row["reason_code"] in {
                "customer_intent", "customer_correction",
            } else "customer_intent",
            "occurred_at": _iso(row["occurred_at"]),
            "recorded_at": _iso(row["recorded_at"]),
            "transitions": [],
        }
        for index, raw in enumerate(row["transitions"] if isinstance(row["transitions"], list) else []):
            transition = _safe_route_transition(raw, source_refs)
            if transition is None:
                continue
            event = {**transition, "decision_id": row["id"], "sequence": row["sequence"],
                     "index": index, "occurred_at": record["occurred_at"]}
            record["transitions"].append(transition)
            transitions.append(event)
        decisions.append(record)

    latest = rows[-1] if rows else None
    active_intents = _safe_active_intents(latest["active_intents"]) if latest else []
    active_keys = {item["key"] for item in active_intents}
    focus_key = _route_key(latest["focus_key"]) if latest else ""
    if focus_key not in active_keys:
        focus_key = ""
    return {
        "status": "accepted_journal" if latest else "absent",
        "reset_floor": reset_floor,
        "latest_decision_id": latest["id"] if latest else None,
        "active_intents": active_intents,
        "focus_key": focus_key,
        "history": {"decisions": decisions, "transitions": transitions},
        "coverage": {
            "scope": "current_explicit_reset", "decision_limit": ROUTE_DECISION_LIMIT,
            "returned_decisions": len(rows), "has_more": has_more,
            "source_message_limit": ROUTE_SOURCE_MESSAGE_LIMIT,
            "returned_source_messages": len(source_refs),
            "source_refs": "owned_user_messages_only",
        },
    }


ROUTE_KIND_LABELS = {"catalog": "Підбір одягу", "custom_print": "Свій принт",
    "dtf": "DTF-плівка", "information": "Запитання", "employment": "Робота в команді",
    "collaboration": "Співпраця", "support": "Допомога", "community": "Спільнота"}
ROUTE_SUBTYPE_LABELS = {"designer": "Дизайнер", "partnership": "Партнерство",
    "dropship": "Дропшипінг", "wholesale_store": "Магазин", "creator": "Автор контенту",
    "other": "Інша співпраця"}
ROUTE_OPERATION_LABELS = {"open": "Тему відкрито", "continue": "Продовження теми",
    "withdraw": "Клієнт відмовився від теми", "correct": "Тему уточнено", "focus": "Зміна теми"}
ROUTE_REASON_LABELS = {"customer_intent": "Намір клієнта", "customer_correction": "Уточнення клієнта"}


def _append_conversation_route_graph(graph, route):
    """Append conversational nodes and only recorded focus edges to graph v1."""
    if route["status"] != "accepted_journal":
        graph["coverage"]["conversation_routes"] = route["coverage"]
        return graph

    keys, state_by_key, refs_by_key = [], {}, {}
    for intent in route["active_intents"]:
        key = intent["key"]
        keys.append(key)
        state_by_key[key] = "partial"
    for transition in route["history"]["transitions"]:
        key = transition["key"]
        if key not in keys:
            keys.append(key)
        if transition["operation"] == "withdraw":
            state_by_key[key] = "invalidated"
        elif key not in state_by_key:
            state_by_key[key] = "partial"
        refs = refs_by_key.setdefault(key, [])
        for ref in transition["evidence_refs"]:
            if ref not in refs:
                refs.append(ref)
        if transition["operation"] == "focus" and transition["from_key"]:
            from_key = transition["from_key"]
            if from_key not in keys:
                keys.append(from_key)
            state_by_key.setdefault(from_key, "partial")

    # Latest accepted active set wins over an older withdrawal in the bounded history.
    for intent in route["active_intents"]:
        state_by_key[intent["key"]] = "partial"
    node_ids = {}
    for key in keys:
        kind, subtype = key.split(":", 1)
        node_id = f"conversation_intent:{key}"
        node_ids[key] = node_id
        facts = []
        for transition in route["history"]["transitions"]:
            if transition["key"] != key or transition["operation"] == "focus":
                continue
            facts.append({
                "id": f"conversation_transition:{transition['decision_id']}:{transition['index']}",
                "label": ROUTE_OPERATION_LABELS.get(transition["operation"], "Зміна теми"),
                "value": ROUTE_REASON_LABELS.get(transition["reason_code"], "Прийняте рішення"), "evidence_refs": transition["evidence_refs"],
            })
        node = _graph_node(
            node_id, ROUTE_SUBTYPE_LABELS.get(subtype) or ROUTE_KIND_LABELS.get(kind, "Звернення"), semantic_key="conversation_intent",
            state=state_by_key.get(key, "partial"), current=route["focus_key"] == key,
            summary="Прийнятий маршрут діалогу", facts=facts,
            evidence_refs=refs_by_key.get(key, []),
        )
        # Conversational graph geometry and iconography belong to the UI layer.
        node.pop("layout")
        node["route_kind"] = kind
        node["route_subtype"] = subtype
        node["route_focus"] = route["focus_key"] == key
        graph["nodes"].append(node)

    for transition in route["history"]["transitions"]:
        if transition["operation"] != "focus" or not transition["from_key"]:
            continue
        from_id, to_id = node_ids.get(transition["from_key"]), node_ids.get(transition["key"])
        if not from_id or not to_id or from_id == to_id:
            continue
        graph["edges"].append({
            "id": f"conversation_focus:{transition['decision_id']}:{transition['index']}",
            "from_node_id": from_id, "to_node_id": to_id,
            "relation": "conversation_focus", "tone": "neutral",
            "decision_id": transition["decision_id"],
            "evidence_refs": transition["evidence_refs"],
        })
    active_ids = [node_ids[item["key"]] for item in route["active_intents"] if item["key"] in node_ids]
    graph["overview_node_ids"] = list(dict.fromkeys([*graph["overview_node_ids"], *active_ids]))
    graph["coverage"]["conversation_routes"] = route["coverage"]
    return graph


def _graph_interpretation(client_id, episode_id=None):
    from management.models import IgConversationAnalysisSnapshot, InstagramBotMessage

    query = IgConversationAnalysisSnapshot.objects.filter(client_id=client_id)
    if episode_id is not None:
        query = query.filter(commercial_episode_id=episode_id)
    row = query.order_by("-analyzed_at", "-id").values(
        "id", "interaction_type", "score_band", "confidence", "last_analyzed_message_id", "analyzed_at",
        "analysis_model", "analysis_prompt_version", "rules_version", "required_state_fingerprint",
    ).first()
    if not row:
        return []
    refs = [_ref("analysis_snapshot", row["id"])]
    message_id = _positive_id(row["last_analyzed_message_id"])
    if message_id and InstagramBotMessage.objects.filter(pk=message_id, client_id=client_id).exists():
        refs.append(_ref("message", message_id))
    # Interpretation values are intentionally omitted: raw analysis evidence and
    # uncertainty strings can include transcript-derived sensitive material.
    return [{
        "id": f"interpretation:{row['id']}", "status": "interpretation_not_route_authority",
        "interaction_type": row["interaction_type"], "score_band": row["score_band"],
        "confidence": str(row["confidence"]), "analyzed_at": _iso(row["analyzed_at"]),
        "versions": {"model": row["analysis_model"], "prompt": row["analysis_prompt_version"],
                     "rules": row["rules_version"], "state": row["required_state_fingerprint"]},
        "evidence_refs": refs,
    }]


def _attempt_state(row):
    if not row["verified"]:
        return "partial"
    return GRAPH_ATTEMPT_STATE.get(row["result"], "partial")


def _guide_graph_nodes(nodes, milestones, focus):
    """Merge current snapshots and recorded milestones into one guide node each."""
    semantics = {"inquiry": "inbound", "terms": "quoted_offer",
                 "payment": "settlement", "fulfillment": "fulfillment"}
    result = []
    for rank, (key, label) in enumerate(GUIDE, start=1):
        node = nodes[key]
        milestone = milestones.get(key, {"refs": [], "facts": []})
        facts = [*node["facts"], *milestone["facts"]]
        refs = [*node["evidence_refs"], *milestone["refs"]]
        if not facts:
            continue
        result.append(_graph_node(
            f"guide:{key}", label, semantic_key=semantics.get(key), state=node["state"],
            current=focus == key, summary=node["summary"], facts=facts, evidence_refs=refs,
            rank=rank, lane=1,
        ))
    return result


def _graph(episode, nodes, history, focus):
    """Build sourced graph details without reinterpreting chronology as a route."""
    graph_nodes, edges = [], []
    interpretations = _graph_interpretation(episode["client_id"], episode["id"])
    episode_ref = _ref("episode", episode["id"])
    graph_nodes.append(_graph_node(
        f"episode:{episode['id']}", f"Покупка {episode['sequence']}",
        state=GRAPH_EPISODE_STATE.get(episode["state"], "partial"),
        summary="Цикл покупки", facts=[{"id": "episode:repeat_kind", "label": "Тип циклу",
        "value": str(REPEAT_KIND_LABELS.get(episode["repeat_kind"], "Не вказано")),
        "evidence_refs": [episode_ref]}], evidence_refs=[episode_ref], rank=0,
    ))
    milestones = {}
    for rank, event in enumerate(history["events"], start=10):
        ref = event["evidence_refs"]
        if event["id"].startswith("funnel_event:") and event["node_id"]:
            key = event["node_id"]
            milestone = milestones.setdefault(key, {"refs": [], "facts": []})
            milestone["refs"].extend(ref)
            milestone["facts"].append({
                "id": f"milestone:{event['id']}", "label": event["label"],
                "value": event["occurred_at"], "evidence_refs": ref,
            })
        states = event.get("episode_states")
        if states:
            source_id = f"episode_state:{episode['id']}:{states['from']}"
            target_id = f"episode_state:{episode['id']}:{states['to']}"
            for identifier, value in ((source_id, states["from"]), (target_id, states["to"])):
                if not any(item["id"] == identifier for item in graph_nodes):
                    graph_nodes.append(_graph_node(
                        identifier, str(EPISODE_STATE_LABELS[value]), state=GRAPH_EPISODE_STATE[value],
                        summary="Зафіксований стан циклу", evidence_refs=ref, rank=rank, lane=3,
                    ))
            edges.append({"id": f"lifecycle:{event['id']}", "from_node_id": source_id, "to_node_id": target_id,
                          "relation": "episode_lifecycle", "outcome": str(EPISODE_STATE_LABELS[states["to"]]), "tone": "neutral",
                          "evidence_refs": ref, "event_ids": [event["id"]], "repeated_count": 1})

    graph_nodes.extend(_guide_graph_nodes(nodes, milestones, focus))

    from django.db.models import Count, Q
    from management.models import IgObjection
    objection_query = IgObjection.objects.filter(episode_id=episode["id"], client_id=episode["client_id"])
    unresolved_states = (IgObjection.State.OPEN, IgObjection.State.HANDLED)
    counts = objection_query.aggregate(
        total=Count("id"), unresolved_total=Count("id", filter=Q(state__in=unresolved_states)),
    )
    # Select unresolved cases first, rather than allowing a historical tail to
    # consume the bounded view. These records retain the model's default order.
    objections = list(objection_query.filter(state__in=unresolved_states)[:10])
    remaining = 20 - len(objections)
    if remaining:
        objections.extend(objection_query.exclude(state__in=unresolved_states)[:remaining])
    attempts = list(IgObjectionAttempt.objects.filter(
        objection_id__in=[objection.pk for objection in objections],
        objection__episode_id=episode["id"], objection__client_id=episode["client_id"],
    ).order_by("-id").values("id", "objection_id", "verified", "result")[:100]) if objections else []
    attempts_by_objection = {}
    for attempt in attempts:
        attempts_by_objection.setdefault(attempt["objection_id"], []).append(attempt)
    for index, objection in enumerate(objections, start=100):
        obj_ref = _ref("objection", objection.pk)
        facts = [
            {"id": f"objection:{objection.pk}:repeat_count", "label": "Повторних згадок",
             "value": objection.repeat_count, "evidence_refs": [obj_ref]},
            {"id": f"objection:{objection.pk}:attempt_count", "label": "Спроб відповіді",
             "value": objection.attempts_count, "evidence_refs": [obj_ref]},
        ]
        for attempt in attempts_by_objection.get(objection.pk, []):
            attempt_ref = _ref("objection_attempt", attempt["id"])
            facts.append({
                "id": f"objection_attempt:{attempt['id']}", "label": "Результат спроби відповіді",
                "value": str(ATTEMPT_RESULT_LABELS.get(attempt["result"], "Результат не визначено")),
                "state": _attempt_state(attempt), "evidence_refs": [attempt_ref],
            })
        node_id = f"objection:{objection.pk}"
        graph_nodes.append(_graph_node(node_id, "Заперечення", semantic_key="objection_case",
                           state=GRAPH_OBJECTION_STATE.get(objection.state, "partial"),
                           summary=objection.get_objection_type_display(), facts=facts, evidence_refs=[obj_ref], rank=index, lane=4))
    returned_attempts = len(attempts)
    expected_attempts = sum(objection.attempts_count for objection in objections)
    graph_events = [
        {**event, "node_id": f"guide:{event['node_id']}" if event["node_id"] else None}
        for event in history["events"]
    ]
    return {"schema_version": 1, "version": 1, "nodes": graph_nodes, "edges": edges,
            "history": {"events": graph_events, "total": history["total"], "has_more": history["has_more"]}, "interpretations": interpretations,
            "coverage": {
                "episode": "bounded", "milestones": "bounded", "lifecycle_edges": "allowlisted",
                "objections": {"total": counts["total"], "returned": len(objections), "limit": 20,
                               "unresolved_total": counts["unresolved_total"],
                               "unresolved_returned": sum(item.state in unresolved_states for item in objections),
                               "has_more": counts["total"] > len(objections)},
                "attempt_details": {"returned": returned_attempts, "limit": 100,
                                    "expected_from_case_counters": expected_attempts,
                                    "has_more": returned_attempts == 100 or expected_attempts > returned_attempts},
                "analysis": "interpretation_only",
            },
            "overview_node_ids": [node["id"] for node in graph_nodes if node["current"] or node["id"].startswith("guide:") or node["id"].startswith("objection:") and node["state"] == "partial"]}


def _graph_without_episode(client_id, nodes, focus):
    graph_nodes = _guide_graph_nodes(nodes, {}, focus)
    return {
        "schema_version": 1, "version": 1, "nodes": graph_nodes, "edges": [],
        "history": {"events": [], "total": 0, "has_more": False},
        "interpretations": _graph_interpretation(client_id),
        "coverage": {"episode": "absent", "milestones": "absent", "lifecycle_edges": "absent",
                     "objections": "absent", "attempt_details": "absent", "analysis": "interpretation_only"},
        "overview_node_ids": [node["id"] for node in graph_nodes if node["current"]],
    }


def _snapshots(episode, nodes):
    ref = _ref("episode", episode["id"])
    # Episode.updated_at may reflect an unrelated review or fulfillment update;
    # it is not the time these individual historical facts were captured.
    captured = ""
    products = episode["product_snapshot"]
    if isinstance(products, list):
        for index, item in enumerate(products[:LINE_LIMIT]):
            item = _mapping(item)
            safe = {key: value for key in ("product_id", "color_variant_id", "qty")
                    if (value := _positive_id(item.get(key))) is not None}
            safe.update({key: value for key in ("title", "size", "fit_option_label")
                         if (value := _short(item.get(key)))})
            if safe:
                _fact(nodes["selection"], f"line:{index}", "Вибрана позиція · знімок", safe,
                      source="episode.product_snapshot", ref=ref, captured_at=captured)
        if products:
            nodes["selection"]["summary"] = "Є знімок вибору; актуальність варіантів не перевірено"
            nodes["selection"]["lines"] = {"total": len(products), "has_more": len(products) > LINE_LIMIT}
    price = _mapping(episode["price_snapshot"])
    amount = _money(price.get("negotiated_total"))
    if amount is not None and Decimal(amount) > 0:
        _fact(nodes["terms"], "quoted_total", "Узгоджена сума · знімок", _amount_label(amount, price),
              source="episode.price_snapshot", ref=ref, captured_at=captured)
        nodes["terms"]["summary"] = "Збережено суму; актуальність ціни та умов не перевірено"
    payment = _mapping(episode["payment_snapshot"])
    if payment.get("episode_id") is not None and _positive_id(payment["episode_id"]) != episode["id"]:
        payment = {}
        nodes["payment"]["missing_sources"].append("owned_payment_snapshot")
    stamp = _timestamp(payment.get("captured_at"))
    provider = (payment.get("provider_source") == "monobank_projection"
                and _positive_id(payment.get("projection_id")))
    truth = payment.get("provider_truth")
    if provider and truth in {"confirmed", "partially_refunded", "refunded", "reversed", "failed", "cancelled", "pending", "unknown", "unverified"}:
        labels = {"confirmed": "Підтверджено провайдером", "partially_refunded": "Частково повернено",
                  "refunded": "Повернено", "reversed": "Скасовано провайдером", "failed": "Неуспішно",
                  "cancelled": "Скасовано", "pending": "Очікується", "unknown": "Стан невідомий", "unverified": "Не підтверджено"}
        state, tone = {
            "confirmed": ("complete", "success"),
            "partially_refunded": ("partial", "warning"),
            "refunded": ("partial", "neutral"),
            "reversed": ("invalidated", "warning"),
            "failed": ("invalidated", "warning"),
            "cancelled": ("invalidated", "warning"),
        }.get(truth, ("open", "neutral"))
        if payment.get("needs_reconciliation") is True:
            state, tone = "partial", "warning"
        _fact(nodes["payment"], "provider_truth", "Провайдер · знімок", labels[truth],
              source="episode.payment_snapshot", ref=ref, captured_at=stamp, state=state, tone=tone)
        nodes["payment"]["summary"] = labels[truth] + " · знімок оплати"
        for key, label in (("provider_confirmed_amount", "Підтверджена провайдером сума"),
                           ("remaining_amount", "Залишок · знімок")):
            if (value := _money(payment.get(key))) is not None:
                positive_confirmation = key == "provider_confirmed_amount" and Decimal(value) > 0 and tone == "success"
                _fact(nodes["payment"], key, label, _amount_label(value, payment), source="episode.payment_snapshot", ref=ref,
                      captured_at=stamp, state="complete" if positive_confirmation else "partial",
                      tone="success" if positive_confirmation else "neutral")
        # A payment milestone alone may be a deposit, later refunded, or stale.
        # Full readiness remains unavailable until a live settlement adapter exists.
    manager = payment.get("manager_truth")
    if manager in {"manager_verified", "manager_rejected"} and _positive_id(payment.get("manager_decision_id", payment.get("decision_id"))):
        _fact(nodes["payment"], "manager_decision", "Рішення менеджера · знімок",
              "Підтверджено менеджером" if manager == "manager_verified" else "Відхилено менеджером",
              source="episode.payment_snapshot", ref=ref, captured_at=stamp,
              state="complete" if manager == "manager_verified" else "invalidated",
              tone="success" if manager == "manager_verified" else "warning")
        if not provider:
            nodes["payment"]["summary"] = "Є рішення менеджера; підтвердження провайдера відсутнє"
    if payment.get("needs_reconciliation") is True:
        _fact(nodes["payment"], "reconciliation", "Звірка оплати", "Потрібна звірка",
              source="episode.payment_snapshot", ref=ref, captured_at=stamp, tone="warning")
        nodes["payment"]["summary"] = "Потрібна звірка оплати"
    fulfillment = _mapping(episode["fulfillment_snapshot"])
    from orders.models import Order
    order_status = fulfillment.get("order_status")
    order_status = {"processing": "new", "shipped": "ship"}.get(order_status, order_status) if isinstance(order_status, str) else None
    if order_status in dict(Order.STATUS_CHOICES):
        _fact(nodes["fulfillment"], "order_snapshot", "Стан замовлення · знімок",
              str(dict(Order.STATUS_CHOICES)[order_status]), source="episode.fulfillment_snapshot", ref=ref, captured_at=captured,
              state="invalidated" if order_status == "cancelled" else "partial",
              tone="warning" if order_status == "cancelled" else "neutral")


def _bound_records(episode, nodes):
    """Latest state of the selected purchase, including when viewing its history."""
    from management.models import IgPaymentConfirmationReview
    from orders.models import Order
    from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at

    if episode["primary_payment_review_id"]:
        review = IgPaymentConfirmationReview.objects.filter(
            pk=episode["primary_payment_review_id"], client_id=episode["client_id"],
        ).values("id", "status", "updated_at").first()
        if review:
            labels = dict(IgPaymentConfirmationReview.Status.choices)
            state, tone = {
                "confirmed": ("complete", "success"), "cancelled": ("invalidated", "warning"),
                "superseded": ("superseded", "neutral"),
            }.get(review["status"], ("open", "neutral"))
            _fact(nodes["payment"], "review", "Перевірка менеджером", str(labels.get(review["status"], "Невідомо")),
                  source="payment_review.current", ref=_ref("payment_review", review["id"]), captured_at=_iso(review["updated_at"]),
                  state=state, tone=tone)
            if review["status"] == "pending":
                nodes["payment"]["summary"] = "Очікує перевірки менеджером"
                return_focus = "payment"
            else:
                return_focus = None
        else:
            nodes["payment"]["missing_sources"].append("owned_payment_review")
            return_focus = None
    else:
        return_focus = None
    if episode["intended_order_id"]:
        order = Order.objects.filter(pk=episode["intended_order_id"]).only(
            "id", "status", "payment_status", "updated", "tracking_number",
            "tracking_status_code", "tracking_terminal_at", "tracking_provider_event_at",
        ).first()
        if order:
            ref = _ref("order", order.pk)
            delivered = bool(nova_poshta_delivery_confirmed_at(order))
            _fact(nodes["fulfillment"], "order", "Замовлення", str(dict(Order.STATUS_CHOICES).get(order.status, "Невідомо")),
                  source="intended_order.current", ref=ref, captured_at=_iso(order.updated),
                  state="complete" if delivered else "invalidated" if order.status == "cancelled" else "partial",
                  tone="success" if delivered else "warning" if order.status == "cancelled" else "neutral")
            nodes["fulfillment"]["summary"] = str(dict(Order.STATUS_CHOICES).get(order.status, "Невідомо"))
            _fact(nodes["payment"], "order_payment", "Статус у замовленні", str(dict(Order.PAYMENT_STATUS_CHOICES).get(order.payment_status, "Невідомо")),
                  source="intended_order.current", ref=ref, captured_at=_iso(order.updated),
                  state="complete" if order.payment_status == "paid" else "partial",
                  tone="success" if order.payment_status == "paid" else "neutral")
            if delivered:
                nodes["fulfillment"]["state"] = "complete"
                nodes["fulfillment"]["summary"] = "Отримання підтверджено перевізником"
        else:
            nodes["fulfillment"]["missing_sources"].append("bound_order")
    return return_focus


def build_journey_snapshot(client, *, view_episode_id=None):
    """Return JSON-safe v1; ownership is checked before any selected data read."""
    now = timezone.now()
    client_id = _positive_id(getattr(client, "pk", None))
    if client_id is None:
        raise ValueError("A persisted client is required")
    queryset = IgCommercialEpisode.objects.filter(client_id=client_id)
    total = queryset.count()
    recent = list(queryset.order_by("-sequence", "-id").values(*EPISODE_FIELDS)[:EPISODE_LIMIT])
    current = next((row for row in recent if row["open_slot"] == 1), None)
    if current is None:
        current = queryset.filter(open_slot=1).values(*EPISODE_FIELDS).first()
    if view_episode_id is not None:
        requested = _positive_id(view_episode_id)
        if requested is None:
            raise InvalidJourneyEpisode("Недоступний цикл покупки")
        episode = next((row for row in recent if row["id"] == requested), None)
        if episode is None:
            episode = queryset.filter(pk=requested).values(*EPISODE_FIELDS).first()
        if episode is None:
            raise InvalidJourneyEpisode("Недоступний цикл покупки")
    else:
        episode = current
    is_history = bool(episode and (not current or episode["id"] != current["id"]))
    nodes = {key: {
        "id": key, "label": label, "state": "open", "current": False,
        "coverage": "unknown", "missing_sources": ["live_node_projection"],
        "summary": "Даних для цього етапу поки немає", "facts": [], "evidence_refs": [],
    } for key, label in GUIDE}
    history = {"events": [], "total": 0, "has_more": False, "coverage": "unknown", "visits": [], "edges": [], "edge_coverage": "missing_source"}
    covered = ["commercial_episodes"]
    focus = None
    # The conversational journal has no commercial-episode ownership.  Do not
    # place the latest client dialogue onto an older selected purchase view.
    conversation_route = None if is_history else _conversation_route(client_id)
    if episode:
        _snapshots(episode, nodes)
        history = _history(episode["id"], client_id)
        covered.extend(["episode_snapshots", "funnel_step_events", "commercial_episode_events"])
        for event in history["events"]:
            if event["node_id"]:
                node = nodes[event["node_id"]]
                if not node["facts"]:
                    node["summary"] = "Є події; поточні умови не перевірено"
                node["coverage"] = "partial"
                # Observed visits remain separate from the node's current state.
                node["evidence_refs"].extend(event["evidence_refs"])
        if history["visits"]:
            focus = history["visits"][-1]["node_id"]
        review_focus = _bound_records(episode, nodes)
        focus = review_focus or focus
        for node in nodes.values():
            covered.extend(fact["source"] for fact in node["facts"] if fact["source"].endswith(".current"))
    else:
        from management.models import InstagramBotMessage
        first = InstagramBotMessage.objects.filter(client_id=client_id, role="user").order_by("id").values("id", "created_at").first()
        if first:
            _fact(nodes["inquiry"], "dialogue", "Діалог", "Є вхідне повідомлення",
                  source="client_message", ref=_ref("message", first["id"]), captured_at=_iso(first["created_at"]))
            nodes["inquiry"]["summary"] = "Діалог розпочато; цикл покупки ще не створено"
            focus = "inquiry"
            covered.append("client_message")
    if focus:
        nodes[focus]["current"] = True
    if episode:
        graph = _graph(episode, nodes, history, focus)
    else:
        graph = _graph_without_episode(client_id, nodes, focus)
    if conversation_route is not None:
        graph = _append_conversation_route_graph(graph, conversation_route)
    if episode and not is_history:
        from management.services.ig_journey_timers import invoice_timers
        payment_node = next((node for node in graph["nodes"] if node["id"] == "guide:payment"), None)
        if payment_node is not None:
            payment_node["timers"] = invoice_timers(client_id, episode["id"], now=now)
    result = {
        "schema_version": 1, "client_id": client_id,
        "current_episode_id": current["id"] if current else None,
        "viewed_episode_id": episode["id"] if episode else None,
        "is_history": is_history,
        "episodes": {"items": [_selector(row) for row in recent], "total": total, "has_more": total > len(recent)},
        "viewed_episode": _selector(episode) if episode else None,
        "route_kind": "unknown", "focus": {"node_id": focus, "display_only": True},
        "nodes": list(nodes.values()), "history": history,
        "graph": graph,
        "covered_sources": sorted(set(covered)),
        "deferred_domains": ["live_node_projection", "semantic_transitions", "route_classification", "media_understanding", "checkout_readiness", "offer_validity", "settlement_projection", "attention", "next_action"],
        "capabilities": {"attention": False, "actions": False, "full_projection": False},
    }
    if conversation_route is not None:
        result["conversation_route"] = conversation_route
        if conversation_route["status"] == "accepted_journal":
            covered.append("accepted_conversation_route_journal")
            result["covered_sources"] = sorted(set(covered))
    result["revision"] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    # Clock sync does not force a semantic rerender on every poll.
    result["server_now"] = now.isoformat()
    return result
