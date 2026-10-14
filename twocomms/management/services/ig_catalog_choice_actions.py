"""Owned catalog button sources, admitted into the ordinary selection reducer.

The invitation proves what was offered. The admission seals the exact customer
event and reducer operation. Historical replay verifies both immutable proofs;
it does not renew an invitation or grant stock, payment or send authority.
"""
from copy import deepcopy
from dataclasses import fields, replace
import hashlib
import json

from django.db import connection
from django.utils.crypto import constant_time_compare, salted_hmac

from management.services.ig_commerce_types import CommerceLineOperation, CommerceTurnRequest

PREFIX = "CC1:"
PRODUCER = "catalog-choice-source-v1"
SCHEMA = "catalog-choice-admission.v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _mac(value):
    return salted_hmac(SCHEMA, _digest(value), algorithm="sha256").hexdigest()


def _source(source):
    return {"message_id": source.pk, "client_id": source.client_id,
        "text": str(source.text or ""), "quick_reply_payload": str(source.quick_reply_payload or ""),
        "mid": str(source.mid or ""), "provider_message_id": str(source.provider_message_id or ""),
        "namespace": str(source.provider_namespace or ""),
        "reply_to": str(source.reply_to_provider_message_id or ""),
        "event_at": (source.provider_created_at or source.created_at).isoformat()}


def _delivery(client, source, *, fresh):
    from management.services.ig_catalog_choice_presentations import delivered_catalog_choice
    return delivered_catalog_choice(client, source, fresh=fresh)


def _snapshot(client):
    from management.models import IgCommerceSelectionSession
    session = IgCommerceSelectionSession.objects.filter(client_id=client.pk,
        open_slot=1, state="open").order_by("-generation").first()
    return session, session.snapshot() if session else {}


def _operations(action, scope, before):
    """Translate only an enumerated, authenticated choice to one exact operation."""
    kind = action.get("kind")
    product_id = action.get("product_id")
    if type(product_id) is not int or product_id <= 0:
        raise ValueError("catalog_choice_product_invalid")
    lines = before.get("lines") or []
    if not isinstance(lines, list) or len(lines) > 16 or any(not isinstance(row, dict) for row in lines):
        raise ValueError("catalog_choice_line_bound")
    recipient = str(scope.get("recipient_id") or "self")
    if kind == "product":
        # Picking another card in this shortlist adds another position. A
        # repeated pick selects the existing position rather than duplicating it.
        matches = [row for row in lines if row.get("product_id") == product_id
            and row.get("recipient_id", "self") == recipient]
        if len(matches) == 1:
            return (CommerceLineOperation("select", target_line_id=matches[0]["line_id"],
                target_product_id=product_id, target_recipient_id=recipient),)
        if len(matches) > 1:
            raise ValueError("catalog_choice_line_ambiguous")
        partial = next((row for row in lines if row.get("line_id") == scope.get("line_id")
            and not row.get("product_id") and row.get("recipient_id", "self") == recipient), None)
        if partial is not None:
            return (CommerceLineOperation("update", target_line_id=partial["line_id"],
                target_recipient_id=recipient, exact_product_id=product_id),)
        return (CommerceLineOperation("add", exact_product_id=product_id, recipient_id=recipient),)
    if kind not in {"color", "size"}:
        raise ValueError("catalog_choice_action_invalid")
    matches = [row for row in lines if row.get("line_id") == scope.get("line_id")
        and row.get("product_id") == product_id and row.get("recipient_id", "self") == recipient]
    if len(matches) != 1:
        raise ValueError("catalog_choice_line_changed")
    value = action.get("label") if kind == "color" else action.get("size", action.get("value"))
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        raise ValueError("catalog_choice_value_invalid")
    if kind == "color" and (type(action.get("variant_id")) is not int or action["variant_id"] <= 0):
        raise ValueError("catalog_choice_variant_invalid")
    return (CommerceLineOperation("update", target_line_id=matches[0]["line_id"],
        target_product_id=product_id, target_recipient_id=recipient, field_updates={kind: value}),)


def _operation_payload(operations):
    return [{field.name: dict(getattr(row, field.name)) if field.name == "field_updates"
             else getattr(row, field.name) for field in fields(CommerceLineOperation)} for row in operations]


def _request(proof):
    raw = proof.get("request") or {}
    if set(raw) != {"line_operations"} or not isinstance(raw["line_operations"], list) or len(raw["line_operations"]) != 1:
        raise ValueError("catalog_choice_request_invalid")
    allowed = {field.name for field in fields(CommerceLineOperation)}
    if any(not isinstance(row, dict) or set(row) != allowed for row in raw["line_operations"]):
        raise ValueError("catalog_choice_request_invalid")
    operations = tuple(CommerceLineOperation(**row) for row in raw["line_operations"])
    return CommerceTurnRequest(line_operations=operations, source_binding={
        "producer": PRODUCER, "source_message_id": proof["source"]["message_id"],
        "source_digest": hashlib.sha256(proof["source"]["text"].encode()).hexdigest(),
        "product_resolution": "native_catalog_choice", "native_catalog_choice": deepcopy(proof)})


def _proof(delivered, source, before, operations=()):
    result = {"schema": SCHEMA, "source": _source(source),
        "invitation_id": delivered["invitation_id"], "manifest_digest": delivered["manifest_digest"],
        "effect_id": delivered["effect_id"], "provider_message_id": delivered["provider_message_id"],
        "scope": deepcopy(delivered["scope"]), "action": deepcopy(delivered["action"]),
        "before_digest": _digest(before) if before else "",
        "request": {"line_operations": _operation_payload(operations)}}
    result["hmac"] = _mac(result)
    return result


def _bootstrap_before(proof, before):
    """Only an own first product pick may bridge no session to default state."""
    scope = proof.get("scope") or {}
    if (scope.get("session_id") is not None or scope.get("generation") != 0
        or scope.get("selection_revision") != 0 or scope.get("candidate_generation") != 0
        or scope.get("line_id") != "" or scope.get("recipient_id") != "self"
        or (proof.get("action") or {}).get("kind") != "product"):
        return False
    return bool(before.get("lines") == [] and before.get("active_index") == 0
        and before.get("revision") == 0 and before.get("state") == "open"
        and type(before.get("generation")) is int and before["generation"] > 0
        and not any(before.get(key) for key in ("selection_constraints", "query_constraints",
            "candidate_product_ids", "candidate_digest", "candidate_generation", "candidate_prompt_provider_ids",
            "rejected_selection", "rejected_reason", "pending_field", "pending_clarification",
            "semantic_block_key", "graph_digest", "last_provider_event_at", "last_provider_message_id")))


def _verified(proof, client, source, *, fresh=False, before=None):
    if (not isinstance(proof, dict) or proof.get("schema") != SCHEMA
        or proof.get("source") != _source(source) or source.client_id != client.pk
        or source.sender_id != client.igsid or source.role != "user" or source.source != "webhook"
        or source.status == "failed" or not (source.mid or source.provider_message_id) or not source.provider_namespace
        or not str(source.quick_reply_payload or "").startswith(PREFIX)):
        return False
    signed = {key: value for key, value in proof.items() if key != "hmac"}
    if not constant_time_compare(str(proof.get("hmac") or ""), _mac(signed)):
        return False
    delivered = _delivery(client, source, fresh=fresh)
    if not delivered.get("ready") or any(proof.get(key) != delivered.get(key)
        for key in ("invitation_id", "manifest_digest", "effect_id", "provider_message_id", "scope", "action")):
        return False
    if before is not None:
        if proof.get("before_digest"):
            if proof["before_digest"] != _digest(before):
                return False
        elif not _bootstrap_before(proof, before):
            return False
        try:
            if proof.get("request") != {"line_operations": _operation_payload(
                _operations(proof["action"], proof["scope"], before))}:
                return False
        except (ValueError, TypeError, KeyError):
            return False
    return True


def native_choice_request(client, source, *, historical=False):
    """Canonical native semantics; never a caller/model-supplied proof bypass."""
    if not str(source.quick_reply_payload or "").startswith(PREFIX):
        return None
    from management.models import IgCommerceTurnDecision
    # Bounded source capture already loads this reverse relation. Reusing its
    # request-local ORM row does not replace the independent immutable witness.
    existing = getattr(getattr(source, "_state", None), "fields_cache", {}).get("commerce_turn_decision")
    if existing is None:
        existing = IgCommerceTurnDecision.objects.filter(source_message_id=source.pk).select_related("transition").first()
    if existing is not None and (existing.source_message_id != source.pk or existing.session.client_id != client.pk):
        return None
    if existing is not None:
        proof = (existing.request_payload or {}).get("source_binding", {}).get("native_catalog_choice")
        if (not existing.accepted or existing.is_stale or existing.transition is None
            or not _verified(proof, client, source, before=existing.transition.previous_snapshot)):
            return None
        request = _request(proof)
        from management.services.ig_commerce_state import _request_payload
        return request if _request_payload(request) == existing.request_payload else None
    if historical:
        return None
    delivered = _delivery(client, source, fresh=True)
    if not delivered.get("ready") or delivered.get("action", {}).get("kind") not in {"product", "color", "size"}:
        return None
    session, before = _snapshot(client)
    scope = delivered.get("scope") or {}
    if scope.get("session_id") is None:
        if session is None:
            # The ordinary reducer's legacy bootstrap must be empty too. An
            # old unproved product is never inherited by a new native pick.
            if getattr(client, "current_product_id", None):
                return None
        elif (delivered["action"].get("kind") != "product"
            or session.commercial_episode_id != client.current_commercial_episode_id
            or (scope.get("episode_id") is not None and scope["episode_id"] != session.commercial_episode_id)
            or session.revision <= 0):
            return None
        # A session that exists here was independently admitted by the
        # producer's exact own-product continuation chain above. Later picks
        # seal its real snapshot instead of the original no-session marker.
    elif scope.get("session_id") != (session.pk if session else None):
        return None
    try:
        operations = _operations(delivered["action"], scope, before)
        return _request(_proof(delivered, source, before, operations))
    except (ValueError, KeyError, TypeError):
        return None


def validate_native_choice_request(client, source, request):
    canonical = native_choice_request(client, source)
    if canonical is None:
        return False
    from management.services.ig_commerce_state import _request_payload
    return _request_payload(request) == _request_payload(canonical)


def validated_choice_transition(transition, *, invitation_id, manifest_digest):
    """Narrow historical proof used for this shortlist's own continuation."""
    from management.models import IgClient, IgCommerceTurnDecision
    client = IgClient.objects.filter(pk=transition.session.client_id).first()
    if client is None:
        return False
    source = transition.source_message
    decision = IgCommerceTurnDecision.objects.filter(source_message_id=source.pk).first()
    if (decision is None or decision.transition_id != transition.pk or decision.session_id != transition.session_id
        or not decision.accepted or decision.is_stale or transition.action != "line_operations_applied"
        or transition.to_revision != transition.from_revision + 1):
        return False
    request = native_choice_request(client, source, historical=True)
    if request is None:
        return False
    proof = request.source_binding["native_catalog_choice"]
    if (proof["invitation_id"] != invitation_id or proof["manifest_digest"] != manifest_digest
        or proof["action"].get("kind") != "product"):
        return False
    if proof.get("before_digest"):
        if proof["before_digest"] != _digest(transition.previous_snapshot):
            return False
    elif transition.from_revision != 0 or not _bootstrap_before(proof, transition.previous_snapshot):
        return False
    original_episode = proof["scope"].get("episode_id")
    if original_episode is not None and original_episode != transition.session.commercial_episode_id:
        return False
    from management.services.ig_commerce_state import _apply_line_operations
    after, _facts, reason = _apply_line_operations(transition.previous_snapshot, request, source)
    if reason or after is None:
        return False
    after.update(revision=transition.to_revision,
        last_provider_message_id=transition.next_snapshot.get("last_provider_message_id"))
    return after == transition.next_snapshot


def admitted_native_color_variants(capture, rows):
    """Exact variant identity from an admitted current color field's source.

    This read-only branch never accepts a raw variant hint. A native source
    with a broken witness is explicitly invalid, so a same-named sibling cannot
    replace the customer's original variant. Catalog availability is separate.
    """
    from management.models import IgClient, InstagramBotMessage, IgCommerceSelectionSession, IgCommerceSelectionTransition
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    fields_by_line = {}
    for row in rows:
        color = (row.get("fields") or {}).get("color") or {}
        evidence = color.get("source") or {}
        source_id = evidence.get("source_message_id")
        if color.get("status") == "confirmed" and type(source_id) is int and source_id > 0:
            fields_by_line[row["line_id"]] = (row, color, evidence)
    if not fields_by_line:
        return {}
    ids = {entry[2]["source_message_id"] for entry in fields_by_line.values()}
    sources = {source.pk: source for source in InstagramBotMessage.objects.filter(pk__in=ids)
        .select_related("commerce_turn_decision__session", "commerce_turn_decision__transition")}
    native = {line_id: entry for line_id, entry in fields_by_line.items()
        if entry[2]["source_message_id"] in sources and str(sources[entry[2]["source_message_id"]].quick_reply_payload or "").startswith(PREFIX)}
    if not native:
        return {}
    invalid = {line_id: {"status": "invalid", "variant_id": None} for line_id in native}
    scope = capture.get("scope") or {}
    client = IgClient.objects.filter(pk=scope.get("client_id")).first()
    if (client is None or client.privacy_erasure_started_at
        or client.current_commercial_episode_id != scope.get("episode_id")
        or conversation_route_reset_floor(client.pk) != scope.get("reset_floor")
        or capture.get("capture_digest") != _digest({key: value for key, value in capture.items() if key != "capture_digest"})):
        return invalid
    session = IgCommerceSelectionSession.objects.filter(pk=capture.get("session_id"), client=client,
        commercial_episode_id=scope.get("episode_id"), generation=capture.get("generation"),
        revision=capture.get("selection_revision"), state="open", open_slot=1).first()
    if session is None:
        return invalid
    current_lines = {row.get("line_id"): row for row in session.lines or [] if isinstance(row, dict)}
    transitions = list(IgCommerceSelectionTransition.objects.filter(session=session,
        to_revision__lte=session.revision).select_related("source_message__commerce_turn_decision").order_by("from_revision", "pk")[:65])
    if len(transitions) > 64:
        return invalid
    result = dict(invalid)
    from management.services.ig_commerce_state import _apply_line_operations, _event_key
    def committed_snapshot(transition):
        committed = deepcopy(transition.next_snapshot)
        if transition.source_message_id:
            committed["last_provider_event_at"] = _event_key(transition.source_message)[0].isoformat()
        return committed
    for line_id, (row, color, evidence) in native.items():
        source = sources[evidence["source_message_id"]]
        current = current_lines.get(line_id)
        product_id = ((row.get("fields") or {}).get("product_id") or {}).get("value")
        recipient = row.get("recipient_id", "self")
        if (current is None or current.get("product_id") != product_id
            or current.get("color") != color.get("value") or current.get("recipient_id", "self") != recipient
            or source.provider_namespace != scope.get("source_namespace")
            or source.pk not in (capture.get("fence") or {}).get("source_ids", [])
            or evidence.get("source_digest") != hashlib.sha256(source.text.encode()).hexdigest()):
            continue
        request = native_choice_request(client, source, historical=True)
        if request is None:
            continue
        proof = request.source_binding["native_catalog_choice"]
        action, offered = proof["action"], proof["scope"]
        decision = source.commerce_turn_decision
        transition = decision.transition
        if (action.get("kind") != "color" or type(action.get("variant_id")) is not int or action["variant_id"] <= 0
            or action.get("product_id") != product_id or action.get("label") != color.get("value")
            or offered.get("client_id") != client.pk or offered.get("episode_id") != scope.get("episode_id")
            or offered.get("session_id") != session.pk or offered.get("generation") != session.generation
            or offered.get("line_id") != line_id or offered.get("recipient_id", "self") != recipient
            or offered.get("reset_floor") != scope.get("reset_floor")
            or evidence.get("decision_id") != decision.pk or evidence.get("transition_id") != transition.pk
            or evidence.get("operation_index") != 0 or transition.session_id != session.pk
            or transition.to_revision > session.revision or transition.to_revision != transition.from_revision + 1):
            continue
        after, facts, reason = _apply_line_operations(transition.previous_snapshot, request, source)
        if reason or after is None:
            continue
        after.update(revision=transition.to_revision, last_provider_message_id=transition.next_snapshot.get("last_provider_message_id"))
        if after != transition.next_snapshot or not any(fact.get("line_id") == line_id
            and fact.get("recipient_id") == recipient and fact.get("values") == {"color": action["label"]} for fact in facts):
            continue
        expected = committed_snapshot(transition)
        revision = transition.to_revision
        valid = True
        for later in transitions:
            if later.from_revision < revision:
                continue
            if later.from_revision != revision or later.previous_snapshot != expected:
                valid = False
                break
            before_line = next((line for line in later.previous_snapshot.get("lines", []) if line.get("line_id") == line_id), {})
            after_line = next((line for line in later.next_snapshot.get("lines", []) if line.get("line_id") == line_id), {})
            if any(before_line.get(key) != after_line.get(key) for key in ("product_id", "color", "recipient_id")):
                valid = False
                break
            later_decision = getattr(later.source_message, "commerce_turn_decision", None) if later.source_message_id else None
            later_facts = (later_decision.result_payload or {}) if later_decision else {}
            reiterated = any(fact.get("line_id") == line_id and "color" in (fact.get("values") or {})
                for fact in (later_facts.get("line_source_facts") or {}).get("lines", []))
            source_fact = later_facts.get("source_facts") or {}
            if reiterated or (source_fact.get("line_id") == line_id and "color" in (source_fact.get("values") or {})):
                valid = False
                break
            revision, expected = later.to_revision, committed_snapshot(later)
        # Reconstruct each committed event watermark from its actual source.
        final = session.snapshot()
        if valid and revision == session.revision and expected == final:
            result[line_id] = {"status": "verified", "variant_id": action["variant_id"]}
    return result


def consume_catalog_choice(client, source, *, revision_id=None):
    """Admit one original source inside the caller's client transaction."""
    if not str(source.quick_reply_payload or "").startswith(PREFIX):
        return {"handled": False, "ready": False, "reason": "not_catalog_choice"}
    if not connection.in_atomic_block:
        return {"handled": True, "ready": False, "reason": "catalog_choice_transaction_required"}
    from management.models import IgClient, InstagramBotMessage, IgCommerceTurnDecision, IgSourceActionReceipt
    current = IgClient.objects.select_for_update().filter(pk=client.pk).first()
    owned = InstagramBotMessage.objects.select_for_update().filter(pk=source.pk,
        client_id=client.pk, sender_id=client.igsid, role="user", source="webhook").first()
    if current is None or owned is None or current.privacy_erasure_started_at:
        return {"handled": True, "ready": False, "reason": "catalog_choice_owner_changed"}
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    if owned.pk < conversation_route_reset_floor(current.pk) or len(str(owned.text or "")) > 8000:
        return {"handled": True, "ready": False, "reason": "catalog_choice_source_scope_changed"}
    previous = IgSourceActionReceipt.objects.select_for_update().filter(source_message_id=owned.pk, kind="postback").first()
    if previous is not None:
        proof = (previous.outcome or {}).get("proof")
        if (previous.client_id != current.pk or previous.outcome_digest != _digest(previous.outcome)
            or previous.source_digest != _digest(_source(owned)) or previous.payload_digest != _digest(owned.quick_reply_payload)
            or not _verified(proof, current, owned)):
            return {"handled": True, "ready": False, "reason": "catalog_choice_receipt_changed"}
        return {**deepcopy(previous.outcome), "handled": True, "ready": True, "replayed": True}
    existing = IgCommerceTurnDecision.objects.filter(source_message_id=owned.pk).first()
    historical = native_choice_request(current, owned, historical=True) if existing else None
    if existing and historical is None:
        return {"handled": True, "ready": False, "reason": "catalog_choice_admission_changed"}
    delivered = _delivery(current, owned, fresh=historical is None)
    if not delivered.get("ready"):
        return {"handled": True, "ready": False, "reason": delivered.get("reason") or "catalog_choice_stale"}
    kind = (delivered.get("action") or {}).get("kind")
    decision = None
    if kind in {"product", "color", "size"}:
        request = historical or native_choice_request(current, owned)
        if request is None:
            return {"handled": True, "ready": False, "reason": "catalog_choice_request_unverified"}
        from management.services.ig_commerce_state import apply_turn, CommerceRevisionConflict
        try:
            decision = apply_turn(current, owned, request, reply_payload={})
        except CommerceRevisionConflict:
            return {"handled": True, "ready": False, "reason": "catalog_choice_scope_changed"}
        if not decision.accepted or decision.is_stale:
            return {"handled": True, "ready": False, "reason": (decision.result_payload or {}).get("reason") or "catalog_choice_not_applied"}
        proof = deepcopy(request.source_binding["native_catalog_choice"])
    elif kind in {"chart", "page"}:
        _session, before = _snapshot(current)
        proof = _proof(delivered, owned, before)
    else:
        return {"handled": True, "ready": False, "reason": "catalog_choice_action_invalid"}
    result = {"action": deepcopy(delivered["action"]), "proof": proof,
        "decision_id": decision.pk if decision else None, "reason": "catalog_choice_admitted"}
    # At ingress the selection decision already owns the immutable source.
    # Non-mutating chart/page requests get their global action receipt only
    # once the real customer revision exists, never an invented revision ID.
    if revision_id is not None:
        from management.models import IgTurnRevisionSource
        if not IgTurnRevisionSource.objects.filter(revision_id=revision_id, message_id=owned.pk,
            revision__client_id=current.pk).exists():
            return {"handled": True, "ready": False, "reason": "catalog_choice_revision_source_missing"}
        IgSourceActionReceipt.objects.create(client=current, source_message=owned, kind="postback",
            source_digest=_digest(_source(owned)), payload_digest=_digest(owned.quick_reply_payload),
            outcome=result, outcome_digest=_digest(result), first_revision_id=revision_id)
    client.refresh_from_db()
    return {**result, "handled": True, "ready": True, "replayed": existing is not None}
