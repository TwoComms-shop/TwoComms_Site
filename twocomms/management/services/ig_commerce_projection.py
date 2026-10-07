"""Projection between durable commerce sessions and legacy ``IgClient`` fields."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json

from django.db import IntegrityError, transaction

from management.ig_bot_models import IgCommerceSelectionSession


_pending_action_projection = ContextVar("ig_pending_selection_action_projection", default=None)


def _choice_digest(value):
    return hashlib.sha256(json.dumps(_source_snapshot(value), sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _original_choice_request(source, capture):
    from management.services.ig_commerce_turns import parse_turn
    if capture is None:
        return parse_turn(source.text)
    key = (source.pk, hashlib.sha256(source.text.encode()).hexdigest())
    parsed = capture.setdefault("parsed_sources", {})
    if key not in parsed:
        from management.services.ig_commerce_types import CatalogGraph
        # Shape detection must not resolve each URL through live ORM. Typed
        # line operations are then reparsed against the one captured graph.
        # Legacy exact identity retains its accepted source binding below.
        original = parse_turn(source.text, parsed_catalog_graph=CatalogGraph((), "", ""))
        if original.line_operations or original.pending_line_clarification:
            from management.services.ig_catalog_graph import build_catalog_graph
            if "catalog_graph" not in capture:
                capture["catalog_graph"] = build_catalog_graph(product_ids=capture.get("catalog_product_ids", ()))
            original = parse_turn(source.text, parsed_catalog_graph=capture["catalog_graph"])
            from types import SimpleNamespace
            from management.services.ig_commerce_source_identity import resolve_source_product_request
            original = resolve_source_product_request(SimpleNamespace(pk=source.client_id,
                igsid=capture["current"]["igsid"], privacy_erasure_started_at=None,
                current_commercial_episode_id=capture["current"]["current_commercial_episode_id"]), source,
                original, catalog_graph=capture["catalog_graph"])
        parsed[key] = original
    return parsed[key]


def _line_replay(transition, original, facts):
    from management.services.ig_commerce_state import _apply_line_operations, _jsonable
    copy_fact = next((row for row in facts.get("lines", []) if row.get("copy_evidence")), {})
    scope = copy_fact.get("copy_scope") or {}
    copied = {"values": {key: value for key, value in (copy_fact.get("values") or {}).items()
                         if key in (copy_fact.get("copy_evidence") or {})},
        "evidence": copy_fact.get("copy_evidence") or {}, **scope,
        **({"reorder_origin": copy_fact["reorder_origin"]} if copy_fact.get("reorder_origin") else {})}
    batch, expected_facts, reason = _apply_line_operations(transition.previous_snapshot,
        original, transition.source_message, copy_projection=copied)
    if reason or batch is None or _jsonable(expected_facts) != facts.get("lines"):
        return False
    batch["revision"] = transition.to_revision
    batch["last_provider_message_id"] = transition.next_snapshot.get("last_provider_message_id")
    return _source_snapshot(batch) == _source_snapshot(transition.next_snapshot)


def _line_operation_proof_matches(recorded, original):
    """The optional old-recipient selector defaults to no selector.

    Earlier v1 producers omitted that newly introduced empty field. Retain
    their immutable bytes and compare this one exact default semantically;
    every other field, unknown key and nonempty target remains exact.
    """
    from management.services.ig_commerce_state import _jsonable, MAX_LINE_OPERATIONS
    if (not isinstance(recorded, list) or len(recorded) > MAX_LINE_OPERATIONS
        or any(not isinstance(row, dict) for row in recorded)):
        return False
    normalized = [{"target_recipient_id": "", **row} for row in recorded]
    return normalized == _jsonable(original)


def _validated_copy_projection(client, scope, capture):
    """Rewalk the sealed prior head; no latest-order or text-similarity fallback."""
    if capture.get("copy_depth", 0) >= 6:
        return {}
    key = (scope.get("head_transition_id"), scope.get("line_id"), scope.get("head_digest"))
    cache = capture.setdefault("copy_cache", {})
    if key in cache:
        return cache[key]
    head = capture.get("row_lookup", {}).get(scope.get("head_transition_id"))
    if (head is None or head.session.client_id != client.pk or head.session_id != scope.get("session_id")
        or head.session.generation != scope.get("generation") or head.to_revision != scope.get("revision")
        or head.session.commercial_episode_id != scope.get("episode_id")
        or (not capture.get("allow_order_line_copy") and (scope.get("line_count") != 1 or len(head.next_snapshot.get("lines") or []) != 1))
        or (capture.get("allow_order_line_copy") and scope.get("line_count") != len(head.next_snapshot.get("lines") or []))
        or scope.get("reset_floor") != capture["reset_floor"]
        or _choice_digest(head.next_snapshot) != scope.get("head_digest")):
        cache[key] = {}
        return {}
    old = {**capture, "current": {**capture["current"], "current_commercial_episode_id": scope["episode_id"]},
        "session": head.session, "snapshot": head.next_snapshot,
        "transitions": sorted((row for row in capture["row_lookup"].values()
            if row.session_id == head.session_id and row.to_revision <= head.to_revision),
            key=lambda row: row.to_revision, reverse=True), "copy_depth": capture.get("copy_depth", 0) + 1,
        "allow_order_line_copy": False}
    if any(row.source_message.client_id != client.pk or row.source_message.sender_id != capture["current"]["igsid"]
        or row.source_message.role != "user" or row.source_message.source != "webhook"
        or row.source_message.status == "failed" or row.source_message.provider_namespace != capture["namespace"]
        for row in old["transitions"]):
        cache[key] = {}
        return {}
    result = source_preferences_for(client, episode_id=scope["episode_id"], line_id=scope.get("line_id"), _capture=old)
    cache[key] = result
    return result


def source_preferences_for(client, *, episode_id=None, line_id=None, _capture=None) -> dict:
    """Read preferences from the reducer with accepted original-source evidence.

    This is a projection, never a second selection store. A legacy snapshot or
    an unaccepted/stale decision cannot authorize preference acknowledgement.
    Product changes and resets bound the provenance search to the current line.
    """
    from management.models import IgClient, IgCommerceSelectionTransition
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_commerce_turns import parse_turn
    current = _capture["current"] if _capture is not None else IgClient.objects.filter(pk=client.pk).values(
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
    session = _capture["session"] if _capture is not None else sessions.order_by("-generation").first()
    if session is None:
        return {}
    snapshot = _capture["snapshot"] if _capture is not None else session.snapshot()
    lines = snapshot.get("lines") or []
    index = snapshot.get("active_index")
    if line_id is not None and isinstance(lines, list):
        matches = [ordinal for ordinal, candidate in enumerate(lines)
                   if isinstance(candidate, dict) and candidate.get("line_id") == line_id]
        if len(matches) != 1:
            return {}
        index = matches[0]
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
    reset_floor = _capture["reset_floor"] if _capture is not None else conversation_route_reset_floor(client.pk)
    values = {
        key: line[key] for key in ("product_id", "fit_option_code", "color", "size", "quantity")
        if line.get(key) not in (None, "")
    }
    constraints = snapshot.get("query_constraints") or {}
    if not isinstance(constraints, dict):
        return {}
    garment = line.get("garment_type") or (constraints.get("garment_type") if len(lines) == 1 else "")
    if garment:
        values["garment_type"] = garment
    if constraints.get("purchase_requested") is True:
        values["purchase_requested"] = True
    if constraints.get("query") and not line.get("product_id"):
        values["model_query"] = constraints["query"]
    evidence = {}
    cleared = {}
    correction_namespace = None
    correction_reset_id = None
    correction_order_id = None
    withdrawn_keys = set()
    transitions = _capture["transitions"] if _capture is not None else list(IgCommerceSelectionTransition.objects.filter(
        session=session, to_revision__lte=session.revision,
        source_message__pk__gte=reset_floor,
    ).select_related("session", "source_message__commerce_turn_decision").order_by("-to_revision")[:64])
    if "garment_type" not in values and index == 0 and not line.get("garment_type"):
        # A legacy category lived in shared constraints while this was the
        # only position. Keep its exact last single-position value when a
        # sibling is added. The walk below must still prove its original
        # source through every intervening snapshot; this grants no fact.
        for candidate in transitions:
            prior = candidate.previous_snapshot or {}
            prior_lines = prior.get("lines") or []
            if (len(prior_lines) == 1 and isinstance(prior_lines[0], dict)
                and prior_lines[0].get("line_id") == line["line_id"]):
                prior_garment = (prior.get("query_constraints") or {}).get("garment_type")
                if prior_garment:
                    values["garment_type"] = prior_garment
                break
    copy_capture = _capture
    if _capture is None and any((getattr(row.source_message, "commerce_turn_decision", None).result_payload or {}).get("line_source_facts")
            for row in transitions if getattr(row.source_message, "commerce_turn_decision", None)):
        from management.services.ig_admin_state_capture import _namespace
        from management.models import IgFunnelResetAudit, IgCommercialEpisode
        extra = list(IgCommerceSelectionTransition.objects.filter(session__client_id=client.pk,
            source_message_id__gte=reset_floor).exclude(session=session)
            .select_related("session", "source_message__commerce_turn_decision").order_by("-id")[:max(0, 64-len(transitions))])
        copy_capture = {"current": current, "session": session, "snapshot": snapshot,
            "transitions": transitions, "row_lookup": {row.pk: row for row in (*transitions, *extra)},
            "reset_floor": reset_floor, "namespace": _namespace(),
            "reset_id": IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values_list("pk", flat=True).first(),
            "order_id": IgCommercialEpisode.objects.filter(pk=current_episode, client_id=client.pk).values_list("intended_order_id", flat=True).first()}
        copy_capture["catalog_product_ids"] = _captured_product_ids(copy_capture["row_lookup"].values())
        if copy_capture["catalog_product_ids"] is None:
            return {}
    expected_snapshot = _source_snapshot(snapshot)
    revision = int(snapshot.get("revision") or 0)
    for transition in transitions:
        source = transition.source_message
        decision = getattr(source, "commerce_turn_decision", None)
        if (transition.to_revision != revision or transition.from_revision != revision - 1
                or _source_snapshot(transition.next_snapshot) != expected_snapshot):
            return {}
        after = transition.next_snapshot or {}
        after_lines = after.get("lines") or []
        after_line = next((row for row in after_lines if isinstance(row, dict) and row.get("line_id") == line["line_id"]), {})
        if not after_line and not (index == 0 and not after_lines
            and not (transition.previous_snapshot or {}).get("lines")):
            break
        before_lines = (transition.previous_snapshot or {}).get("lines") or []
        before_line = next((row for row in before_lines if isinstance(row, dict) and row.get("line_id") == line["line_id"]), {})
        owned = (source.client_id == client.pk and source.sender_id == current["igsid"]
                 and source.role == "user" and source.source == "webhook")
        if transition.action == "manager_size_correction":
            correction_index = transition.previous_snapshot.get("active_index", 0)
            correction_line = before_lines[correction_index] if isinstance(correction_index, int) and 0 <= correction_index < len(before_lines) else {}
            if correction_line.get("line_id") != line["line_id"]:
                if before_line != after_line:
                    return {}
                expected_snapshot = _source_snapshot(transition.previous_snapshot)
                revision = transition.from_revision
                continue
            from management.services.ig_selection_corrections import validated_correction_receipt
            from management.services.ig_admin_state_capture import _namespace
            from management.models import IgFunnelResetAudit, IgCommercialEpisode
            if correction_namespace is None:
                correction_namespace = _capture["namespace"] if _capture is not None else _namespace()
                correction_reset_id = _capture["reset_id"] if _capture is not None else IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values_list("pk", flat=True).first()
                correction_order_id = _capture["order_id"] if _capture is not None else IgCommercialEpisode.objects.filter(pk=current_episode, client_id=client.pk).values_list("intended_order_id", flat=True).first()
            correction_scope = {"client_id": client.pk, "episode_id": current_episode,
                "line_id": str(line["line_id"]), "recipient_id": str(line.get("recipient_id") or "self"),
                "reset_floor": reset_floor, "reset_id": correction_reset_id, "order_id": correction_order_id}
            receipt = validated_correction_receipt(transition, scope=correction_scope, namespace=correction_namespace) if owned else None
            if receipt is None:
                return {}
            proof = {"decision_id": None, "transition_id": transition.pk, "source_message_id": source.pk,
                "source_digest": hashlib.sha256(source.text.encode()).hexdigest(),
                "observed_at": (source.provider_created_at or source.created_at).isoformat(),
                "authority": "audited_correction", "correction": {"transition_id": transition.pk, "receipt": receipt}}
            if "size" not in evidence and "size" not in withdrawn_keys:
                if receipt["operation"] == "clear":
                    cleared["size"] = proof
                    withdrawn_keys.add("size")
                elif values.get("size") == receipt["after"]:
                    evidence["size"] = proof
                else:
                    return {}
            elif "size" in evidence and evidence["size"].get("authority") != "audited_correction":
                evidence["size"]["supersedes_transition_id"] = transition.pk
            expected_snapshot = _source_snapshot(transition.previous_snapshot)
            revision = transition.from_revision
            continue
        authorized_product = None
        line_facts = ((decision.result_payload or {}).get("line_source_facts") or {}) if decision is not None else {}
        if line_facts:
            if (not owned or (not decision.accepted and transition.action != "line_clarification_required")
                or decision.is_stale or source.status == "failed"
                or copy_capture is None or source.provider_namespace != copy_capture["namespace"]):
                return {}
            original = _original_choice_request(source, copy_capture)
            from management.services.ig_commerce_state import _jsonable
            source_digest = hashlib.sha256(source.text.encode()).hexdigest()
            if (line_facts.get("schema") != "line-source-facts.v1"
                or line_facts.get("source_message_id") != source.pk or line_facts.get("source_digest") != source_digest
                or line_facts.get("episode_id") != current_episode
                or not _line_operation_proof_matches(line_facts.get("operations"), original.line_operations)):
                return {}
            if transition.action == "line_clarification_required":
                previous, following = _source_snapshot(transition.previous_snapshot), _source_snapshot(transition.next_snapshot)
                for item in (previous, following):
                    for key in ("revision", "last_provider_message_id", "pending_clarification"):
                        item.pop(key, None)
                if previous != following or line_facts.get("lines"):
                    return {}
                expected_snapshot, revision = _source_snapshot(transition.previous_snapshot), transition.from_revision
                continue
            if not _line_replay(transition, original, line_facts):
                return {}
            target_facts = [row for row in line_facts.get("lines", [])
                if isinstance(row, dict) and row.get("line_id") == line["line_id"]]
            for facts in reversed(target_facts):
                ordinal = facts.get("operation_index")
                if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 0 <= ordinal < len(original.line_operations):
                    return {}
                operation = original.line_operations[ordinal]
                if operation.operation != facts.get("operation") or facts.get("recipient_id") != line.get("recipient_id", "self"):
                    return {}
                if operation.operation in {"select", "remove"}:
                    continue
                updates = {{"fit": "fit_option_code", "qty": "quantity"}.get(key, key): value
                           for key, value in operation.field_updates.items()}
                stated = {**updates, **({"product_id": operation.exact_product_id} if operation.exact_product_id else {}),
                    **({"garment_type": operation.garment_type} if operation.garment_type else {})}
                for key, value in values.items():
                    copied = (facts.get("copy_evidence") or {}).get(key)
                    matches = str((facts.get("values") or {}).get(key)) == str(value)
                    if key not in evidence and key not in withdrawn_keys and matches and str(stated.get(key)) == str(value):
                        evidence[key] = {"decision_id": decision.pk, "transition_id": transition.pk,
                            "source_message_id": source.pk, "source_digest": source_digest,
                            "observed_at": (source.provider_created_at or source.created_at).isoformat(),
                            "operation_index": ordinal}
                    elif copied and matches and key not in evidence:
                        if facts.get("reorder_origin"):
                            from management.services.ig_commerce_reorder import validated_reorder_projection
                            origin = validated_reorder_projection(client, source, original, facts["reorder_origin"], copy_capture)
                        else:
                            origin = _validated_copy_projection(client, facts.get("copy_scope") or {}, copy_capture)
                        if (operation.copy_previous and (origin.get("evidence") or {}).get(key) == copied
                            and str((origin.get("values") or {}).get(key)) == str(value)):
                            evidence[key] = {"decision_id": decision.pk, "transition_id": transition.pk,
                                "source_message_id": source.pk, "source_digest": source_digest,
                                "observed_at": (source.provider_created_at or source.created_at).isoformat(),
                                "operation_index": ordinal, "copied_from": {"scope": facts["copy_scope"], "proof": copied}}
                            if copied.get("authority") == "validated_selection_action":
                                evidence[key]["authority"] = "validated_selection_action"
                                evidence[key]["reorder_origin"] = facts.get("reorder_origin")
                        else:
                            withdrawn_keys.add(key)
            if before_line and (before_line.get("product_id") != after_line.get("product_id")
                or before_line.get("recipient_id", "self") != after_line.get("recipient_id", "self")):
                break
            if not before_line:
                break
            expected_snapshot = _source_snapshot(transition.previous_snapshot)
            revision = transition.from_revision
            continue
        if before_line == after_line and ((decision.result_payload or {}).get("source_facts") or {}).get("line_id") not in (None, "", line["line_id"]):
            expected_snapshot = _source_snapshot(transition.previous_snapshot)
            revision = transition.from_revision
            continue
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
        original = _original_choice_request(source, copy_capture)
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
        identity_changed = (
            before_line.get("product_id") and before_line.get("product_id") != after_line.get("product_id")
        )
        if transition.action in {"selection_reset", "product_rejected"} or identity_changed:
            break
        if before_line and before_line.get("recipient_id", "self") != after_line.get("recipient_id", "self"):
            break
        expected_snapshot = _source_snapshot(transition.previous_snapshot)
        revision = transition.from_revision
    confirmed = {key: value for key, value in values.items() if key in evidence}
    if not confirmed and not cleared:
        return {}
    if _capture is not None:
        return {"session_id": session.pk, "generation": session.generation,
            "revision": int(snapshot.get("revision") or 0), "active_index": index, "episode_id": current_episode,
            "reset_floor": reset_floor, "product_id": line.get("product_id"),
            "recipient_id": str(line.get("recipient_id") or "self"), "line_id": str(line.get("line_id") or ""),
            "line_count": len(lines),
            "values": confirmed, "evidence": {**{key: evidence[key] for key in confirmed}, **cleared},
            **({"cleared": cleared} if cleared else {}),
            "head_transition_id": transitions[0].pk if transitions else None,
            "head_digest": _choice_digest(transitions[0].next_snapshot) if transitions else None}
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
    if correction_namespace is not None:
        from management.services.ig_admin_state_capture import _namespace
        from management.models import IgFunnelResetAudit, IgCommercialEpisode
        if (_namespace() != correction_namespace
            or IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values_list("pk", flat=True).first() != correction_reset_id
            or IgCommercialEpisode.objects.filter(pk=current_episode, client_id=client.pk).values_list("intended_order_id", flat=True).first() != correction_order_id):
            return {}
        clear_sources = {row["pk"]: hashlib.sha256(row["text"].encode()).hexdigest()
            for row in InstagramBotMessage.objects.filter(pk__in={row["source_message_id"] for row in cleared.values()},
                client_id=client.pk, sender_id=current["igsid"], role="user", source="webhook", pk__gte=reset_floor
            ).values("pk", "text")}
        if clear_sources != {row["source_message_id"]: row["source_digest"] for row in cleared.values()}:
            return {}
    return {"session_id": session.pk, "generation": session.generation,
            "revision": session.revision, "active_index": index,
            "episode_id": current_episode, "reset_floor": reset_floor,
            "product_id": line.get("product_id"),
            "recipient_id": str(line.get("recipient_id") or "self"),
            "line_id": str(line.get("line_id") or ""), "values": confirmed,
            "line_count": len(lines),
            "evidence": {**{key: evidence[key] for key in confirmed}, **cleared},
            **({"cleared": cleared} if cleared else {}),
            "head_transition_id": transitions[0].pk if transitions else None,
            "head_digest": _choice_digest(transitions[0].next_snapshot) if transitions else None}


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
    return captured_selection_from_preferences(client.pk, projection)


_MEDIA_INSPECTION_FIELDS = frozenset({
    "version", "state", "source_part_id", "source_image_index", "outcome",
    "evidence_code", "type_code", "content_hash", "request_id", "provider_model",
    "revision_id", "analysis_state", "origin", "content_kind", "sentiment",
    "confidence", "evidence", "complaint_code", "transcript", "audio_status",
})


def _source_fence_media(media, *, message_scope):
    """Fence input media, excluding only the typed backend inspection owner.

    Use the revision source's canonical part identity. Keep all transport,
    capture, storage and privacy fields, including unknown metadata. Malformed
    media remains hash-sensitive instead of being silently dropped by the
    normalizer. Interpretation changes are checked by their own accepted proof.
    """
    from management.services.ig_media_manifest import MediaManifestError, normalize_attachment_media

    if not isinstance(media, list) or any(not isinstance(part, Mapping) for part in media):
        return deepcopy(media)
    try:
        parts = normalize_attachment_media(deepcopy(media), message_scope=message_scope)
    except MediaManifestError:
        return deepcopy(media)
    for original, part in zip(media, parts, strict=True):
        # Missing legacy identity may receive deterministic enrichment. An
        # explicitly supplied identity is source input, even when malformed:
        # normalization must not erase its subsequent mutation from the fence.
        for key in ("source_part_id", "original_index", "identity_origin"):
            if key in original:
                part[key] = deepcopy(original[key])
        inspection = part.get("inspection")
        if (isinstance(inspection, Mapping)
                and inspection.get("version") == "ig-media-inspection-v1"
                and inspection.get("state") in {"inspected", "uninspected"}
                and set(inspection) <= _MEDIA_INSPECTION_FIELDS):
            del part["inspection"]
    return parts


def _source_fence_row(source, *, legacy_raw_media=False):
    return {"id": source.pk, "client_id": source.client_id, "sender_id": source.sender_id,
        "role": source.role, "source": source.source, "status": source.status, "text": source.text,
        "namespace": source.provider_namespace, "provider_created_at": source.provider_created_at.isoformat() if source.provider_created_at else None,
        "created_at": source.created_at.isoformat(), "mid": source.mid,
        "reply_to": source.reply_to_provider_message_id, "quick_reply": source.quick_reply_payload,
        "attachments": source.attachments,
        "attachment_media": (source.attachment_media if legacy_raw_media else
            _source_fence_media(source.attachment_media, message_scope=source.pk))}


def _captured_product_ids(rows):
    """Bound historical identity revalidation to persisted typed selections."""
    result = set()
    for row in rows:
        decision = getattr(row.source_message, "commerce_turn_decision", None)
        facts = ((decision.result_payload or {}).get("line_source_facts") or {}) if decision else {}
        for operation in facts.get("operations", []):
            for product_id in (operation.get("exact_product_id"), operation.get("target_product_id")):
                if isinstance(product_id, int) and not isinstance(product_id, bool) and product_id > 0:
                    result.add(product_id)
        for fact in facts.get("lines", []):
            product_id = (fact.get("reorder_origin") or {}).get("configuration", {}).get("product_id")
            if type(product_id) is int and product_id > 0:
                result.add(product_id)
    return tuple(sorted(result)) if len(result) <= 16 else None


def _selection_history(client, capture, line_id, *, limit=8):
    """Only validated field events; a raw before value is never history proof."""
    records = []
    for transition in capture["transitions"]:
        before_line = next((row for row in transition.previous_snapshot.get("lines", []) if row.get("line_id") == line_id), {})
        after_line = next((row for row in transition.next_snapshot.get("lines", []) if row.get("line_id") == line_id), {})
        if not after_line:
            continue
        source = transition.source_message
        # Reuse the same canonical proof walk at the immutable historical head.
        # This also validates copied origins and audited corrections; stored
        # changes or a raw previous snapshot cannot establish a customer fact.
        historical = {**capture, "snapshot": transition.next_snapshot,
            "transitions": [row for row in capture["transitions"] if row.to_revision <= transition.to_revision]}
        proof = source_preferences_for(client, line_id=line_id, _capture=historical)
        values = {key: value for key, value in (proof.get("values") or {}).items()
            if key in {"size", "color", "fit_option_code", "quantity", "product_id", "garment_type"}
            and (proof.get("evidence", {}).get(key) or {}).get("transition_id") == transition.pk
            and before_line.get(key) != value}
        if values:
            for field, value in values.items():
                evidence = proof["evidence"][field]
                records.append({"field": field, "value": value, "authority": evidence.get("authority", "customer_source"),
                    "transition_id": transition.pk, "source_message_id": source.pk,
                    "source_digest": hashlib.sha256(source.text.encode()).hexdigest(),
                    "event_at": (source.provider_created_at or source.created_at).isoformat(),
                    "recipient_id": after_line.get("recipient_id", "self"),
                    "product_id": after_line.get("product_id"), "copied_from": evidence.get("copied_from"),
                    "previous": None, "previous_source": None, "superseded_by": None})
        if len(records) >= limit:
            break
    records = records[:limit]
    by_field = {}
    for row in reversed(records):
        previous = by_field.get((row["field"], row["recipient_id"], row["product_id"]))
        if previous:
            row["previous"] = previous["value"]
            row["previous_source"] = {key: previous[key] for key in ("source_message_id", "source_digest", "transition_id")}
            previous["superseded_by"] = row["transition_id"]
        by_field[(row["field"], row["recipient_id"], row["product_id"])] = row
    return records


class SelectionCaptureReadError(RuntimeError):
    """Finite observation failure; no partial cart is publication authority."""


def capture_current_selection_lines(client_id, *, now=None, _owner_snapshot=None):
    """Public read facade with a hard SQL budget and no writes/bootstrap."""
    from django.db import connection, DatabaseError
    reads = 0
    def bounded(execute, sql, params, many, context):
        nonlocal reads
        if not sql.lstrip().upper().startswith("SELECT"):
            raise SelectionCaptureReadError("selection_capture_not_read_only")
        if reads >= 64:
            raise SelectionCaptureReadError("selection_capture_query_bound")
        reads += 1
        return execute(sql, params, many, context)
    try:
        with connection.execute_wrapper(bounded):
            return _capture_current_selection_lines(client_id, now=now, _owner_snapshot=_owner_snapshot)
    except SelectionCaptureReadError as exc:
        reason = str(exc)
    except (DatabaseError, AttributeError, TypeError, ValueError, KeyError, IndexError):
        reason = "selection_capture_unavailable"
    return {"schema": "source-selections.v1", "status": "unavailable", "reason": reason,
        "lines": [], "coverage_complete": False}


def _capture_current_selection_lines(client_id, *, now=None, _owner_snapshot=None):
    """Bounded canonical cart proof without bootstrap or active-index mutation.

    All current and copy-origin transitions share a hard 64-row preload. Scope
    and every source are rechecked after projection. Missing history/selector
    proof is unknown; raw snapshots never become customer authority.
    """
    from types import SimpleNamespace
    from django.utils import timezone
    from management.models import IgCommerceSelectionTransition, InstagramBotMessage
    from management.services.ig_admin_state_capture import _owner_fence, _namespace, _source_watermark, valid_identifier
    now = now or timezone.now()
    unavailable = lambda reason, status="unavailable": {"schema": "source-selections.v1", "status": status,
        "reason": reason, "lines": [], "coverage_complete": False}
    if not valid_identifier(client_id):
        return unavailable("client_id_invalid")
    # The enclosing admin read already owns this initial read. Reuse only
    # its request-local detached snapshot; the final fresh owner, session,
    # source and namespace checks below still run in every case.
    first = deepcopy(_owner_snapshot) if _owner_snapshot is not None else _owner_fence(client_id)
    if not first or first["owner"]["privacy_erasure_started_at"]:
        return unavailable("client_erasing" if first else "client_missing")
    owner, selection = first["owner"], first["session"]
    if selection is None:
        return unavailable("selection_unavailable")
    session = IgCommerceSelectionSession.objects.filter(pk=selection["pk"], client_id=client_id,
        open_slot=1, state="open", commercial_episode_id=owner["current_commercial_episode_id"]).first()
    if session is None:
        return unavailable("selection_scope_changed", "conflict")
    snapshot = session.snapshot()
    lines = snapshot.get("lines") or []
    if not isinstance(lines, list) or len(lines) > 16 or any(not isinstance(line, dict) or not line.get("line_id") for line in lines) or len({line["line_id"] for line in lines}) != len(lines):
        return unavailable("selection_line_scope_unknown")
    reset_floor = int((first["reset"] or {}).get("reset_after_message_id") or 0) + 1
    namespace = _namespace()
    if not namespace:
        return unavailable("source_namespace_unknown")
    scope = {"client_id": client_id, "episode_id": owner["current_commercial_episode_id"],
        "order_id": first["episode"]["intended_order_id"] if first["episode"] else None,
        "reset_id": (first["reset"] or {}).get("pk"), "reset_floor": reset_floor, "source_namespace": namespace}
    watermark = _source_watermark(scope, namespace, owner["igsid"], now, customer_only=True)
    if watermark["message_id"] is None:
        # A reset can leave an old session with no source inside the current
        # scope. It supplies no cart authority or valid capture watermark.
        return unavailable("selection_source_unavailable")
    transitions = list(IgCommerceSelectionTransition.objects.filter(session=session,
        to_revision__lte=session.revision, source_message_id__gte=reset_floor)
        .select_related("session", "source_message__commerce_turn_decision").order_by("-to_revision")[:64])
    needs_copy = any(any(fact.get("copy_scope") for fact in ((getattr(row.source_message,
        "commerce_turn_decision", None).result_payload or {}).get("line_source_facts") or {}).get("lines", []))
        for row in transitions if getattr(row.source_message, "commerce_turn_decision", None))
    extra = list(IgCommerceSelectionTransition.objects.filter(session__client_id=client_id,
        source_message_id__gte=reset_floor).exclude(session=session)
        .select_related("session", "source_message__commerce_turn_decision").order_by("-id")[:64-len(transitions)]) if needs_copy else []
    rows = [*transitions, *extra]
    product_ids = _captured_product_ids(rows)
    if product_ids is None:
        return unavailable("selection_identity_bound_exceeded")
    sources = {row.source_message_id: row.source_message for row in rows}
    for source in {row.source_message_id: row.source_message for row in transitions}.values():
        if (source.client_id != client_id or source.sender_id != owner["igsid"] or source.role != "user"
            or source.source != "webhook" or source.status == "failed" or source.provider_namespace != namespace
            or (source.provider_created_at or source.created_at) > now):
            return unavailable("choice_source_scope_unknown")
    capture = {"current": {key: owner[key] for key in ("current_commercial_episode_id", "privacy_erasure_started_at", "igsid")},
        "session": session, "snapshot": snapshot, "transitions": transitions,
        "row_lookup": {row.pk: row for row in rows}, "reset_floor": reset_floor,
        "namespace": namespace, "reset_id": scope["reset_id"], "order_id": scope["order_id"],
        "parsed_sources": {}, "copy_cache": {}, "catalog_product_ids": product_ids}
    client = SimpleNamespace(pk=client_id)
    captured_lines = []
    omissions = []
    for index, line in enumerate(lines):
        proof = source_preferences_for(client, episode_id=scope["episode_id"], line_id=line["line_id"], _capture=capture)
        selection_capture = captured_selection_from_preferences(client_id, proof)
        if selection_capture:
            selection_capture["scope"] = {**scope, **selection_capture["scope"]}
        if not proof:
            omissions.append({"line_id": line["line_id"], "reason": "line_source_unavailable"})
        captured_lines.append({"line_id": line["line_id"], "recipient_id": line.get("recipient_id", "self"),
            "index": index, "source_selection": selection_capture,
            "fields": selection_capture.get("fields", {}), "evidence": proof.get("evidence", {}),
            "cleared": proof.get("cleared", {}), "history": _selection_history(client, capture, line["line_id"]),
            "history_coverage": {"complete": False, "event_limit": 8, "reason": "bounded_validated_history"},
            "defaults": {"quantity": {"value": 1 if line.get("quantity", 1) == 1 else None,
                "authority": "existing_cart_default", "source_confirmed": False}},
            "unsupported_selectors": ["color_variant_id", "option_values"]})
    final_sources = {source.pk: _source_fence_row(source) for source in InstagramBotMessage.objects.filter(pk__in=sources)}
    initial_sources = {key: _source_fence_row(source) for key, source in sources.items()}
    final_session = IgCommerceSelectionSession.objects.filter(pk=session.pk, client_id=client_id, open_slot=1, state="open").first()
    if (first != _owner_fence(client_id) or namespace != _namespace()
        or _source_watermark(scope, namespace, owner["igsid"], now, customer_only=True) != watermark
        or final_sources != initial_sources or final_session is None or final_session.snapshot() != snapshot):
        return unavailable("selection_capture_changed", "conflict")
    fence = {"owner_digest": hashlib.sha256(json.dumps(first, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest(), "namespace": namespace, "source_watermark": watermark,
        "source_ids": sorted(initial_sources),
        "source_digest": hashlib.sha256(json.dumps(initial_sources, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(), "snapshot_digest": _choice_digest(snapshot)}
    payload = {"schema": "source-selections.v1", "status": "captured", "reason": "",
        "scope": scope, "session_id": session.pk, "generation": session.generation,
        "selection_revision": session.revision, "active_index": snapshot["active_index"],
        "active_line_id": lines[snapshot["active_index"]]["line_id"] if lines and 0 <= snapshot["active_index"] < len(lines) else None,
        "source_watermark": watermark, "lines": captured_lines, "omissions": omissions,
        "coverage_complete": not omissions, "transition_limit": 64, "line_limit": 16, "query_limit": 64, "fence": fence}
    payload["capture_digest"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode()).hexdigest()
    return payload


def captured_selection_from_preferences(client_id, projection) -> dict:
    """Render an already validated projection without another mutable read."""
    if not projection:
        return {}
    scope = {key: projection[key] for key in (
        "session_id", "generation", "revision", "active_index", "episode_id", "line_id", "recipient_id", "reset_floor",
    )}
    scope["client_id"] = client_id
    fields = {key: {"value": value, "status": "ambiguous" if key == "model_query" else "confirmed",
                              "authority": projection["evidence"][key].get("authority", "customer_source"), "source": projection["evidence"][key],
                              "applicability": "unknown", "availability": "unknown"}
                       for key, value in projection["values"].items()}
    for key, proof in (projection.get("cleared") or {}).items():
        fields[key] = {"value": None, "status": "unknown", "authority": "audited_correction", "source": proof,
            "applicability": "unknown", "availability": "unknown"}
    return {**projection, "schema": "source-selection.v1", "scope": scope, "fields": fields}


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
