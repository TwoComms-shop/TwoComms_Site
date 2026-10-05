"""Projection between durable commerce sessions and legacy ``IgClient`` fields."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
import hashlib

from django.db import IntegrityError, transaction

from management.ig_bot_models import IgCommerceSelectionSession


_pending_action_projection = ContextVar("ig_pending_selection_action_projection", default=None)


def source_preferences_for(client, *, episode_id=None, line_id=None) -> dict:
    """Read preferences from the reducer with accepted original-source evidence.

    This is a projection, never a second selection store. A legacy snapshot or
    an unaccepted/stale decision cannot authorize preference acknowledgement.
    Product changes and resets bound the provenance search to the current line.
    """
    from management.models import IgClient, IgCommerceSelectionTransition
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_commerce_turns import parse_turn
    current = IgClient.objects.filter(pk=client.pk).values(
        "current_commercial_episode_id", "privacy_erasure_started_at", "igsid",
    ).first()
    if not current or current["privacy_erasure_started_at"] is not None:
        return {}
    current_episode = current["current_commercial_episode_id"]
    if episode_id is not None and episode_id != current_episode:
        return {}
    sessions = IgCommerceSelectionSession.objects.filter(
        client_id=client.pk, open_slot=1, state=IgCommerceSelectionSession.State.OPEN,
        commercial_episode_id=current_episode,
    )
    if current_episode is not None:
        sessions = sessions.filter(commercial_episode__client_id=client.pk)
    session = sessions.order_by("-generation").first()
    if session is None:
        return {}
    snapshot = session.snapshot()
    lines = snapshot.get("lines") or []
    index = snapshot.get("active_index")
    if not isinstance(lines, list) or not isinstance(index, int) or not 0 <= index < len(lines):
        return {}
    line = lines[index] if 0 <= index < len(lines) and isinstance(lines[index], dict) else {}
    if not line.get("line_id") or (line_id is not None and line.get("line_id") != line_id):
        return {}
    if sum(isinstance(row, dict) and row.get("line_id") == line["line_id"] for row in lines) != 1:
        return {}
    for key, expected in (("client_id", client.pk), ("commercial_episode_id", current_episode), ("session_id", session.pk)):
        if key in line and line[key] != expected:
            return {}
    reset_floor = conversation_route_reset_floor(client.pk)
    values = {
        key: line[key] for key in ("product_id", "fit_option_code", "color", "size", "quantity")
        if line.get(key) not in (None, "")
    }
    constraints = snapshot.get("query_constraints") or {}
    if not isinstance(constraints, dict):
        return {}
    garment = constraints.get("garment_type")
    if garment:
        values["garment_type"] = garment
    if constraints.get("purchase_requested") is True:
        values["purchase_requested"] = True
    if constraints.get("query") and not line.get("product_id"):
        values["model_query"] = constraints["query"]
    evidence = {}
    withdrawn_keys = set()
    transitions = IgCommerceSelectionTransition.objects.filter(
        session=session, to_revision__lte=session.revision,
        source_message__pk__gte=reset_floor,
    ).select_related("source_message__commerce_turn_decision").order_by("-to_revision")[:64]
    expected_snapshot = _source_snapshot(snapshot)
    revision = session.revision
    for transition in transitions:
        source = transition.source_message
        decision = getattr(source, "commerce_turn_decision", None)
        if (transition.to_revision != revision or transition.from_revision != revision - 1
                or _source_snapshot(transition.next_snapshot) != expected_snapshot):
            return {}
        after = transition.next_snapshot or {}
        if int(after.get("active_index") or 0) != index:
            break
        after_lines = after.get("lines") or []
        after_line = after_lines[index] if index < len(after_lines) and isinstance(after_lines[index], dict) else {}
        if after_line and after_line.get("line_id") != line.get("line_id"):
            break
        owned = (source.client_id == client.pk and source.sender_id == current["igsid"]
                 and source.role == "user" and source.source == "webhook")
        authorized_product = None
        if transition.action == "selection_authorized":
            if not owned:
                return {}
            action_receipt = _selection_action_receipt(transition, client_id=client.pk)
            if action_receipt is None:
                return {}
            authorized_product = after_line.get("product_id")
        if (decision is None or decision.session_id != session.pk or decision.is_stale
                or not decision.accepted or not owned) and authorized_product is None:
            # A rejected/no-op turn may still advance the durable revision.
            # It proves no choice; walk it only if selection was unchanged.
            before_choice = _source_snapshot(transition.previous_snapshot)
            after_choice = _source_snapshot(transition.next_snapshot)
            for candidate in (before_choice, after_choice):
                candidate.pop("revision", None)
                candidate.pop("last_provider_message_id", None)
            if before_choice != after_choice:
                return {}
            expected_snapshot = _source_snapshot(transition.previous_snapshot)
            revision = transition.from_revision
            continue
        request = (decision.request_payload or {}) if decision is not None else {}
        if not isinstance(request, dict):
            return {}
        original = parse_turn(source.text)
        original_updates = dict(original.field_updates)
        updates = {**(request.get("field_updates") or {}), **(request.get("hard") or {})}
        stated = {
            "product_id": request.get("exact_product_id"),
            "fit_option_code": updates.get("fit_option_code", updates.get("fit")),
            "color": updates.get("color"), "size": updates.get("size"),
            "quantity": updates.get("quantity", updates.get("qty")),
            "garment_type": request.get("garment_type") or (request.get("preferences") or {}).get("garment_type"),
            "purchase_requested": request.get("purchase_requested"),
            "model_query": request.get("query"),
        }
        source_digest = hashlib.sha256(source.text.encode()).hexdigest()
        binding = request.get("source_binding") or {}
        facts = ((decision.result_payload or {}).get("source_facts") or {}) if decision is not None else {}
        if not isinstance(binding, dict) or not isinstance(facts, dict):
            return {}
        # Constraints captured before the first line existed may follow that
        # first line through a continuous chain, never another recipient/line.
        source_line = facts.get("line_id")
        preline_constraints = (source_line == "" and not after_lines and index == 0
                               and set(facts.get("values") or {}) <= {"garment_type", "purchase_requested", "model_query"})
        valid_source = (
            (not binding or (binding.get("source_message_id") == source.pk
                             and binding.get("source_digest") == source_digest))
            and (not facts or (facts.get("source_message_id") == source.pk
                              and facts.get("source_digest") == source_digest
                              and facts.get("episode_id") == current_episode
                              and (source_line == line["line_id"] or preline_constraints)))
        )
        bound_facts = (
            facts.get("values") or {}
            if facts.get("source_message_id") == source.pk
            and facts.get("source_digest") == source_digest
            and facts.get("episode_id") == current_episode
            and (source_line == line["line_id"] or preline_constraints) else {}
        )
        for key, value in values.items():
            action_proof = False
            source_value = original_updates.get("fit" if key == "fit_option_code" else key)
            if key == "garment_type":
                source_value = original.garment_type
            elif key == "product_id":
                source_value = original.exact_product_id
                if binding.get("product_resolution") in {"exact_name", "exact_presentation", "exact_reference"}:
                    source_value = request.get("exact_product_id")
                if authorized_product and str(source_value) != str(value):
                    source_value = stated["product_id"] = authorized_product
                    action_proof = True
            elif key == "purchase_requested":
                source_value = getattr(original, "purchase_requested", False)
            elif key == "model_query":
                source_value = bound_facts.get(key) if binding.get("source_digest") == source_digest else None
            matches = stated.get(key) is not None and str(stated[key]) == str(value)
            if not valid_source and matches and key not in evidence:
                withdrawn_keys.add(key)
            if valid_source and key not in evidence and key not in withdrawn_keys and matches and str(source_value) == str(value):
                source_transition_id = transition.pk if action_proof else decision.transition_id if decision else transition.pk
                evidence[key] = {"decision_id": decision.pk if decision else None, "transition_id": source_transition_id,
                                 "source_message_id": source.pk,
                                 "source_digest": source_digest,
                                 "observed_at": (source.provider_created_at or source.created_at).isoformat()}
                if key == "product_id":
                    evidence[key]["product_resolution"] = binding.get("product_resolution") or "exact_reference"
                    evidence[key]["presentation_effect_ids"] = list(binding.get("presentation_effect_ids") or [])
                    if action_proof:
                        evidence[key]["authority"] = "validated_selection_action"
                elif key == "model_query":
                    evidence[key]["product_resolution"] = binding.get("product_resolution") or "unresolved"
        # A later rejection ends the old evidence chain. Even an accidental
        # legacy write of that value must not revive its previous source proof.
        # Corrections above may prove their new value from this same turn.
        withdrawal = ((decision.result_payload or {}).get("preference_withdrawal") or {}) if decision is not None else {}
        if withdrawal.get("source_message_id") == source.pk:
            withdrawn_keys.update(
                "fit_option_code" if key == "fit" else key
                for key in (withdrawal.get("values") or {})
                if key in {"fit", "color", "garment_type", "size"}
            )
        before_lines = (transition.previous_snapshot or {}).get("lines") or []
        before_line = before_lines[index] if index < len(before_lines) and isinstance(before_lines[index], dict) else {}
        identity_changed = (
            before_line.get("product_id") and before_line.get("product_id") != after_line.get("product_id")
        )
        if transition.action in {"selection_reset", "product_rejected"} or identity_changed:
            break
        if "recipient_scope_changed" in (transition.reasons or []) or transition.action == "recipient_scope_changed":
            break
        expected_snapshot = _source_snapshot(transition.previous_snapshot)
        revision = transition.from_revision
    confirmed = {key: value for key, value in values.items() if key in evidence}
    if not confirmed:
        return {}
    # A GET can race erasure/reset or a selection update. Never publish a
    # captured object assembled from two different authority scopes.
    final_client = IgClient.objects.filter(pk=client.pk).values(
        "current_commercial_episode_id", "privacy_erasure_started_at", "igsid",
    ).first()
    final_session = IgCommerceSelectionSession.objects.filter(pk=session.pk, open_slot=1).first()
    if (final_client != current or conversation_route_reset_floor(client.pk) != reset_floor
            or final_session is None or final_session.commercial_episode_id != current_episode
            or final_session.snapshot() != snapshot):
        return {}
    from management.models import InstagramBotMessage
    proof_sources = {evidence[key]["source_message_id"]: evidence[key]["source_digest"] for key in confirmed}
    final_sources = {row["pk"]: hashlib.sha256(row["text"].encode()).hexdigest()
                     for row in InstagramBotMessage.objects.filter(
                         pk__in=proof_sources, client_id=client.pk, sender_id=current["igsid"],
                         role="user", source="webhook", pk__gte=reset_floor,
                     ).values("pk", "text")}
    if final_sources != proof_sources:
        return {}
    return {"session_id": session.pk, "generation": session.generation,
            "revision": session.revision, "active_index": index,
            "episode_id": current_episode, "reset_floor": reset_floor,
            "product_id": line.get("product_id"),
            "recipient_id": str(line.get("recipient_id") or "self"),
            "line_id": str(line.get("line_id") or ""), "values": confirmed,
            "evidence": {key: evidence[key] for key in confirmed}}


def _source_snapshot(snapshot):
    value = deepcopy(snapshot) if isinstance(snapshot, dict) else {}
    # Written by the reducer after its append-only transition snapshot.
    value.pop("last_provider_event_at", None)
    return value


@contextmanager
def _selection_action_projection(revision, receipt):
    """Expose a trusted candidate only during the protected mutation transaction.

    The caller saves this same immutable receipt after rebuilding authority.
    No API/GET accepts or sets this context; its full proof is checked below.
    """
    from django.db import connection
    if not connection.in_atomic_block:
        raise ValueError("selection action projection requires transaction")
    token = _pending_action_projection.set((revision, deepcopy(receipt)))
    try:
        yield
    finally:
        _pending_action_projection.reset(token)


def _selection_action_receipt(transition, *, client_id):
    """A follow-up mutation is read only through its original CAS receipt."""
    from management.models import IgCustomerTurnRevision
    from management.services.ig_revision_actions import (
        ACTION_CLIENT_CONFIGURATION_UPDATE, _authority_from_projection,
        _bound_control, _catalog_binding, _digest,
    )
    from django.db import connection
    pending = _pending_action_projection.get() if connection.in_atomic_block else None
    if (pending and pending[0].client_id == client_id
            and (pending[1].get("selection_session") or {}).get("transition_id") == transition.pk):
        revision, receipt = pending
    else:
        rows = list(IgCustomerTurnRevision.objects.filter(
            client_id=client_id,
            action_receipts__client_configuration_update__selection_session__transition_id=transition.pk,
        ).only("action_receipts", "generation_proposal", "generation_proposal_digest", "bundle_snapshot", "snapshot_digest")[:2])
        if len(rows) != 1:
            return None
        revision = rows[0]
        receipt = (revision.action_receipts or {}).get(ACTION_CLIENT_CONFIGURATION_UPDATE) or {}
    selected = receipt.get("selection_session") or {}
    proposal = revision.generation_proposal or {}
    if not all(isinstance(value, dict) for value in (receipt, selected, proposal, revision.bundle_snapshot)):
        return None
    if (receipt.get("schema_version") != 1 or receipt.get("action") != ACTION_CLIENT_CONFIGURATION_UPDATE
            or receipt.get("source_message_id") != transition.source_message_id
            or selected.get("source_message_id") != transition.source_message_id
            or selected.get("session_id") != transition.session_id
            or selected.get("before") != transition.previous_snapshot
            or selected.get("after") != transition.next_snapshot
            or receipt.get("generation_proposal_digest") != revision.generation_proposal_digest
            or not revision.generation_proposal_digest or _digest(proposal) != revision.generation_proposal_digest
            or proposal.get("authority") != receipt.get("before_authority")
            or _digest(revision.bundle_snapshot) != revision.snapshot_digest):
        return None
    sources = [item for item in (revision.bundle_snapshot or {}).get("sources", ())
               if isinstance(item, dict) and item.get("message_id") == transition.source_message_id]
    if len(sources) != 1 or sources[0].get("text") != transition.source_message.text:
        return None
    authority = _authority_from_projection(receipt.get("before_authority"))
    binding = _catalog_binding(authority) if authority else None
    control, reason = _bound_control(binding) if binding else (None, "missing")
    if reason or not control or ACTION_CLIENT_CONFIGURATION_UPDATE not in authority.allowed_actions:
        return None
    request_id = str((proposal.get("generation") or {}).get("request_id") or "")[:40]
    if (not request_id or receipt.get("generation_request_id") != request_id
            or transition.source_message_id not in {item.get("message_id") for item in proposal.get("sources", ()) if isinstance(item, dict)}
            or receipt.get("input_digest") != _digest({
                "action": ACTION_CLIENT_CONFIGURATION_UPDATE, "source_message_id": transition.source_message_id,
                "generation_request_id": request_id, "generation_proposal_digest": revision.generation_proposal_digest,
                "control": control, "before_authority": receipt["before_authority"],
            })):
        return None
    after_lines = (transition.next_snapshot or {}).get("lines") or []
    index = int((transition.next_snapshot or {}).get("active_index") or 0)
    line = after_lines[index] if 0 <= index < len(after_lines) and isinstance(after_lines[index], dict) else {}
    if str(line.get("product_id")) != str(control.get("product")):
        return None
    return receipt


def captured_selection_for(client, *, episode_id=None, line_id=None) -> dict:
    """Read one source choice object for prompt/card/journey, without effects.

    Values are customer requirements, never availability or checkout authority.
    Historical callers must pass their episode; mismatched scope is absent.
    """
    projection = source_preferences_for(client, episode_id=episode_id, line_id=line_id)
    if not projection:
        return {}
    scope = {key: projection[key] for key in (
        "session_id", "generation", "revision", "active_index", "episode_id", "line_id", "recipient_id", "reset_floor",
    )}
    scope["client_id"] = client.pk
    return {**projection, "schema": "source-selection.v1", "scope": scope,
            "fields": {key: {"value": value, "status": "ambiguous" if key == "model_query" else "confirmed",
                              "authority": projection["evidence"][key].get("authority", "customer_source"), "source": projection["evidence"][key],
                              "applicability": "unknown", "availability": "unknown"}
                       for key, value in projection["values"].items()}}


def _matching_legacy_selection(client) -> dict:
    context = client.sales_context if isinstance(client.sales_context, dict) else {}
    selection = context.get("assisted_checkout_selection")
    if not isinstance(selection, dict):
        return {}
    product_id = selection.get("product_id")
    if not client.current_product_id or str(product_id) != str(client.current_product_id):
        return {}
    return dict(selection)


def _legacy_line(client, generation: int) -> dict:
    product_id = client.current_product_id
    if not product_id:
        return {}
    line = {
        "line_id": f"legacy:{client.pk}:{generation}:0",
        "product_id": int(product_id),
        "size": str(client.current_size or ""),
        "color": str(client.current_color or ""),
        "quantity": max(1, int(client.current_qty or 1)),
        "confidence": str(client.current_product_confidence or "0"),
    }
    selection = _matching_legacy_selection(client)
    for key in ("fit_option_code", "color_variant_id", "option_values", "pay_type"):
        if key in selection and selection[key] not in (None, "", {}, []):
            line[key] = selection[key]
    return line


def bootstrap_session_from_legacy(client) -> IgCommerceSelectionSession:
    """Create the first durable open session from a legacy client snapshot.

    The assisted checkout context is copied only when it points at the same
    current product. Unknown/stale legacy context is deliberately discarded.
    """
    existing = (
        IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1)
        .order_by("-generation")
        .first()
    )
    if existing is not None:
        return existing
    last_generation = (
        IgCommerceSelectionSession.objects.filter(client_id=client.pk)
        .order_by("-generation")
        .values_list("generation", flat=True)
        .first()
        or 0
    )
    generation = int(last_generation) + 1
    line = _legacy_line(client, generation)
    defaults = {
        "commercial_episode_id": client.current_commercial_episode_id,
        "open_slot": 1,
        "state": IgCommerceSelectionSession.State.OPEN,
        "lines": [line] if line else [],
        "active_index": 0,
    }
    try:
        with transaction.atomic():
            return IgCommerceSelectionSession.objects.create(
                client=client,
                generation=generation,
                **defaults,
            )
    except IntegrityError:
        winner = (
            IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1)
            .order_by("-generation")
            .first()
        )
        if winner is None:
            raise
        return winner


def authoritative_session_for(client) -> IgCommerceSelectionSession:
    """Return the one open durable session, bootstrapping legacy state once."""
    session = (
        IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1)
        .order_by("-generation")
        .first()
    )
    return session or bootstrap_session_from_legacy(client)


def start_new_session_for_episode(client, episode) -> IgCommerceSelectionSession:
    """Close the previous selection cycle and open a clean episode session.

    Repeat purchases must not inherit a prior product, configuration, price,
    candidate anchor, or allocation. The caller owns the client's transaction
    lock; the database uniqueness constraint remains the final guard against a
    second open session.
    """
    previous = (
        IgCommerceSelectionSession.objects.select_for_update()
        .filter(client_id=client.pk, open_slot=1)
        .order_by("-generation")
        .first()
    )
    if previous is not None:
        previous.state = IgCommerceSelectionSession.State.CLOSED
        previous.open_slot = None
        previous.save(update_fields=["state", "open_slot", "updated_at"])

    last_generation = (
        IgCommerceSelectionSession.objects.select_for_update()
        .filter(client_id=client.pk)
        .order_by("-generation")
        .values_list("generation", flat=True)
        .first()
        or 0
    )
    session = IgCommerceSelectionSession.objects.create(
        client_id=client.pk,
        commercial_episode_id=episode.pk,
        generation=int(last_generation) + 1,
        open_slot=1,
        state=IgCommerceSelectionSession.State.OPEN,
        lines=[],
        active_index=0,
    )
    project_active_line_to_legacy_client(session, client)
    return session


def _safe_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def project_active_line_to_legacy_client(session, client) -> None:
    """Expose only the durable active line through legacy CRM fields."""
    lines = list(session.lines or [])
    index = int(session.active_index or 0)
    line = (
        lines[index]
        if 0 <= index < len(lines) and isinstance(lines[index], dict)
        else {}
    )
    context = dict(client.sales_context or {}) if isinstance(client.sales_context, dict) else {}
    context.pop("assisted_checkout_selection", None)
    update_fields = [
        "current_product",
        "current_size",
        "current_color",
        "current_qty",
        "current_product_confidence",
        "sales_context",
        "updated_at",
    ]
    if line.get("product_id"):
        client.current_product_id = int(line["product_id"])
        client.current_size = str(line.get("size") or "")[:16]
        client.current_color = str(line.get("color") or "")[:64]
        try:
            client.current_qty = max(1, int(line.get("quantity") or line.get("qty") or 1))
        except (TypeError, ValueError):
            client.current_qty = 1
        client.current_product_confidence = _safe_decimal(line.get("confidence"))
        selection = {
            key: line[key]
            for key in (
                "product_id",
                "fit_option_code",
                "color_variant_id",
                "option_values",
                "pay_type",
            )
            if key in line and line[key] not in (None, "", {}, [])
        }
        if selection:
            context["assisted_checkout_selection"] = selection
    else:
        client.current_product_id = None
        client.current_size = ""
        client.current_color = ""
        client.current_qty = 1
        client.current_product_confidence = Decimal("0")
    client.sales_context = context
    client.save(update_fields=update_fields)
