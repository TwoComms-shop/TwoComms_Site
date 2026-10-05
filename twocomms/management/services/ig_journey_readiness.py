"""Read-only, current active-line requirements for the journey selection guide.

This is an optional UI projection, never payment permission. No materialized
funnel state, legacy client selection, last deal, provider or cache is read.
There are at most eight scope SELECTs and 24 actual catalog SELECTs. Complex
catalogs exceeding that budget have no badge. No readiness is persisted.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from django.db import connection

from management.models import (
    IgClient, IgCommerceSelectionSession, IgCommerceSelectionTransition,
    InstagramBotMessage,
)
from management.services.ig_checkout_readiness import selection_readiness
from management.services.ig_conversation_routes import conversation_route_reset_floor
from management.services.ig_funnel_nodes import project_nodes

CATALOG_READ_LIMIT = 24
TRANSITION_LIMIT = 64
LINE_LIMIT = 32
REQUIREMENT_KEYS = ("product", "fit", "color", "size_named", "size_available", "option_axes")
_SELECTION_KEYS = ("product_id", "fit_option_code", "size", "color_variant_id", "option_values")
_CLIENT_FIELDS = ("id", "current_commercial_episode_id", "reply_permission_epoch", "privacy_erasure_started_at")
_SOURCE_TRANSITION_FIELDS = (
    "id", "from_revision", "to_revision", "previous_snapshot", "next_snapshot",
    "source_message_id", "source_message__client_id", "source_message__role",
    "source_message__text", "source_message__sender_id", "source_message__source",
    "session__client__igsid", "source_message__commerce_turn_decision__request_payload",
    "source_message__commerce_turn_decision__result_payload",
)


class _CatalogBudgetExceeded(Exception):
    pass


class _ReadBudget:
    def __init__(self):
        self.reads = 0
        self.exceeded = False

    def __call__(self, execute, sql, params, many, context):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise _CatalogBudgetExceeded("catalog_not_read_only")
        if self.reads >= CATALOG_READ_LIMIT:
            self.exceeded = True
            raise _CatalogBudgetExceeded("catalog_read_budget")
        self.reads += 1
        return execute(sql, params, many, context)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _absent(reason, reads=0):
    return {"requirements": None, "reason": reason, "catalog_reads": reads}


def _client_fence(client_id):
    return IgClient.objects.filter(pk=client_id).values(*_CLIENT_FIELDS).first()


def _session(client_id, episode_id):
    return IgCommerceSelectionSession.objects.filter(
        client_id=client_id, commercial_episode_id=episode_id,
        commercial_episode__client_id=client_id, open_slot=1,
        state=IgCommerceSelectionSession.State.OPEN,
    ).first()


def _session_fence(session):
    return (session.pk, session.client_id, session.commercial_episode_id,
            session.generation, session.revision, session.open_slot,
            session.state, _digest(session.snapshot()))


def _line(snapshot, line_id):
    if not isinstance(snapshot, dict):
        return None
    rows = snapshot.get("lines")
    if not isinstance(rows, list):
        return None
    matches = [row for row in rows if isinstance(row, dict) and row.get("line_id") == line_id]
    return matches[0] if len(matches) == 1 else None


def _transition_snapshot(snapshot):
    # The reducer writes event_at after recording next_snapshot; that one field
    # is intentionally excluded only from transition comparison, never CAS.
    if not isinstance(snapshot, dict):
        return None
    value = deepcopy(snapshot)
    value.pop("last_provider_event_at", None)
    return value


def _selected_values(line, *, include_quantity=False):
    values = {(key,): line[key] for key in (*_SELECTION_KEYS, *(("quantity",) if include_quantity else ())) if key != "option_values"
              and line.get(key) not in (None, "", {}, [])}
    options = line.get("option_values")
    if isinstance(options, dict):
        values.update({("option_values", key): value for key, value in options.items()
                       if value not in (None, "", {}, [])})
    return values


def _valid_transition_source(row, session, active_line, proven, selected):
    """A changed/erased source cannot continue to authorize its old snapshot."""
    text = str(row.get("source_message__text") or "")
    source_digest = hashlib.sha256(text.encode()).hexdigest()
    request = row.get("source_message__commerce_turn_decision__request_payload") or {}
    result = row.get("source_message__commerce_turn_decision__result_payload") or {}
    if not isinstance(request, dict) or not isinstance(result, dict):
        return None
    binding = request.get("source_binding") or {}
    facts = result.get("source_facts") or {}
    if not isinstance(binding, dict) or not isinstance(facts, dict):
        return None
    if binding and (binding.get("source_message_id") != row["source_message_id"]
                    or binding.get("source_digest") != source_digest):
        return None
    if facts and (facts.get("source_message_id") != row["source_message_id"]
                  or facts.get("source_digest") != source_digest
                  or facts.get("episode_id") != session.commercial_episode_id
                  or (proven and facts.get("line_id") != active_line["line_id"])):
        return None
    if row.get("source_message__sender_id") != row.get("session__client__igsid") or row.get("source_message__source") != "webhook":
        return None
    if not facts and not binding:
        # Pre-canonical transitions have no immutable text digest. Still
        # require the source to affirm the human choices it established.
        from management.services.ig_commerce_turns import parse_turn
        parsed = parse_turn(text)
        for key in proven:
            if key in {("size",), ("fit_option_code",)}:
                source_key = "fit" if key == ("fit_option_code",) else key[0]
                if str(parsed.field_updates.get(source_key)) != str(selected[key]):
                    return None
    return source_digest


def _evidence_sources_current(evidence, *, client_id, reset_floor):
    expected = {ref["id"]: ref["source_digest"] for ref in evidence}
    rows = InstagramBotMessage.objects.filter(
        pk__in=expected, pk__gte=reset_floor, client_id=client_id,
        role=InstagramBotMessage.Role.USER, source="webhook", sender_id__exact=IgClient.objects.filter(pk=client_id).values("igsid")[:1],
    ).values("pk", "text")
    actual = {row["pk"]: hashlib.sha256(str(row["text"] or "").encode()).hexdigest() for row in rows}
    return actual == expected


def _owned_evidence(session, snapshot, active_line, reset_floor, *, rows=None, include_quantity=False):
    """Trace every selected value to an owned post-reset change, not a carryover.

    A new unrelated turn cannot rehabilitate a legacy/bootstrap selection or a
    pre-reset value. Each current selected field must occur in a continuous
    bounded transition chain at the change that established its current value.
    """
    rows = rows if rows is not None else list(IgCommerceSelectionTransition.objects.filter(
        session_id=session.pk, to_revision__lte=session.revision,
    ).order_by("-to_revision").values(*_SOURCE_TRANSITION_FIELDS)[:TRANSITION_LIMIT])
    selected = _selected_values(active_line, include_quantity=include_quantity)
    needed = set(selected)
    if not needed or ("product_id",) not in needed:
        return None
    expected = _transition_snapshot(snapshot)
    revision = session.revision
    refs = []
    for row in rows:
        if (row["to_revision"] != revision or row["from_revision"] != revision - 1
                or row["source_message_id"] < reset_floor
                or row["source_message__client_id"] != session.client_id
                or row["source_message__role"] != InstagramBotMessage.Role.USER
                or _transition_snapshot(row["next_snapshot"]) != expected):
            return None
        after = _line(row["next_snapshot"], active_line["line_id"])
        before = _line(row["previous_snapshot"], active_line["line_id"]) or {}
        if after is None:
            return None
        after_values, before_values = _selected_values(after, include_quantity=include_quantity), _selected_values(before, include_quantity=include_quantity)
        proven = {key for key in needed
                  if after_values.get(key) == selected[key] and before_values.get(key) != after_values.get(key)}
        source_digest = _valid_transition_source(row, session, active_line, proven, selected)
        if source_digest is None:
            return None
        if proven:
            refs.append({"kind": "message", "id": row["source_message_id"], "source_digest": source_digest})
            needed -= proven
        if not needed:
            return refs
        expected = _transition_snapshot(row["previous_snapshot"])
        revision = row["from_revision"]
    return None


def requirements_from_readiness(readiness, *, scope, evidence_refs):
    """Pure canonical counting; unknown totals have no N/M representation."""
    if not readiness.get("has_product") or readiness.get("applicability_known") is not True:
        return _absent("applicability_unknown")
    projection = project_nodes(readiness=readiness, deal=None)
    if not projection.authority_agrees:
        return _absent("authority_disagrees")
    by_key = {node.key: node for node in projection.nodes}
    items = []
    for key in REQUIREMENT_KEYS:
        node = by_key[key]
        if node.status not in {"open", "partial", "complete", "invalidated", "not_applicable"}:
            return _absent("requirement_status_unknown")
        items.append({"key": key, "label": node.definition.ui_label,
                      "status": node.status, "reason": node.reason_code,
                      "required": node.status != "not_applicable"})
    required = [item for item in items if item["required"]]
    if not required:
        return _absent("applicability_unknown")
    label = "Готовність комплектації"
    if scope["line_count"] > 1:
        label += f" · позиція {scope['active_position']} із {scope['line_count']}"
    return {"requirements": {
        "completed": sum(item["status"] == "complete" for item in required),
        "total": len(required), "label": label, "items": items,
        "scope": scope, "evidence_refs": evidence_refs,
    }, "reason": "", "catalog_reads": 0}


def selection_requirements(*, client_id, episode_id, line_id=None, source_selection=None):
    """Fresh projection for exactly the displayed current episode and active line.

    Explicit episode_id is mandatory: a historical journey must never receive
    current checkout requirements. Optional line_id is an equality assertion.
    Endpoint permission checks remain the caller's responsibility.
    """
    first_client = _client_fence(client_id)
    if not first_client or first_client["privacy_erasure_started_at"] is not None:
        return _absent("client_unavailable")
    if not episode_id or first_client["current_commercial_episode_id"] != episode_id:
        return _absent("not_current_episode")
    reset_floor = conversation_route_reset_floor(client_id)
    session = _session(client_id, episode_id)
    if session is None:
        return _absent("no_current_session")
    snapshot = deepcopy(session.snapshot())
    fence = _session_fence(session)
    lines = snapshot.get("lines")
    index = snapshot.get("active_index")
    if (not isinstance(lines, list) or not 0 < len(lines) <= LINE_LIMIT
            or not isinstance(index, int) or not 0 <= index < len(lines)):
        return _absent("active_line_unknown")
    active = lines[index]
    if not isinstance(active, dict) or not active.get("line_id"):
        return _absent("active_line_unknown")
    if line_id is not None and active["line_id"] != line_id:
        return _absent("not_active_line")
    if _line(snapshot, active["line_id"]) is None:
        return _absent("ambiguous_line")
    for key, expected in (("client_id", client_id), ("commercial_episode_id", episode_id), ("session_id", session.pk)):
        if key in active and active[key] != expected:
            return _absent("foreign_line")
    # A captured choice is optional display evidence. It cannot authorize the
    # strict product/configuration chain or change a readiness denominator.
    if source_selection:
        source_scope = source_selection.get("scope") or {}
        if any(source_scope.get(key) != value for key, value in (
            ("client_id", client_id), ("episode_id", episode_id), ("session_id", session.pk),
            ("revision", session.revision), ("line_id", active["line_id"]), ("reset_floor", reset_floor),
        )):
            source_selection = None
    evidence = _owned_evidence(session, snapshot, active, reset_floor)
    if not evidence:
        result = _absent("no_owned_current_source")
        if source_selection:
            from management.services.ig_journey_selection import source_selection_fields
            result["selection_fields"] = source_selection_fields(source_selection)
            result["reason"] = "source_choice_only"
        return result
    budget = _ReadBudget()
    try:
        with connection.execute_wrapper(budget):
            readiness = selection_readiness(
                product_id=active.get("product_id"), selection=active,
                size=active.get("size") or "", quantity=active.get("quantity", 1), strict=True,
            )
    except Exception:
        return _absent("catalog_read_budget" if budget.exceeded else "catalog_unavailable", budget.reads)
    if budget.exceeded:
        return _absent("catalog_read_budget", budget.reads)
    # Fresh rereads outside the catalog budget reject concurrent reset, pause,
    # erasure, episode switches and any session edit (even without revision).
    last_session = _session(client_id, episode_id)
    last_client = _client_fence(client_id)
    last_floor = conversation_route_reset_floor(client_id)
    if (last_client != first_client or last_floor != reset_floor
            or last_session is None or _session_fence(last_session) != fence):
        return _absent("scope_changed", budget.reads)
    # Recheck owned messages: source erasure can occur while catalog is read.
    if not _evidence_sources_current(evidence, client_id=client_id, reset_floor=reset_floor):
        return _absent("source_changed", budget.reads)
    scope = {"kind": "active_selection_line", "action": "pay_link_issue_configuration",
             "client_id": client_id, "episode_id": episode_id, "session_id": session.pk,
             "generation": session.generation, "revision": session.revision,
             "snapshot_digest": fence[-1], "line_id": active["line_id"],
             "active_position": index + 1, "line_count": len(lines), "reset_floor": reset_floor}
    result = requirements_from_readiness(readiness, scope=scope, evidence_refs=evidence)
    from management.services.ig_journey_selection import selection_fields
    result["selection_fields"] = selection_fields(readiness, scope=scope, evidence_refs=evidence,
                                                   source_selection=source_selection)
    result["catalog_reads"] = budget.reads
    return result
