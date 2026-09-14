"""Projection between durable commerce sessions and legacy ``IgClient`` fields."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from management.ig_bot_models import IgCommerceSelectionSession


def source_preferences_for(client) -> dict:
    """Read preferences from the reducer with accepted original-source evidence.

    This is a projection, never a second selection store. A legacy snapshot or
    an unaccepted/stale decision cannot authorize preference acknowledgement.
    Product changes and resets bound the provenance search to the current line.
    """
    from management.models import IgCommerceTurnDecision
    from management.services.ig_commerce_turns import parse_turn
    import hashlib

    session = IgCommerceSelectionSession.objects.filter(
        client_id=client.pk, open_slot=1,
    ).order_by("-generation").first()
    if session is None:
        return {}
    snapshot = session.snapshot()
    lines = snapshot.get("lines") or []
    index = int(snapshot.get("active_index") or 0)
    line = lines[index] if 0 <= index < len(lines) and isinstance(lines[index], dict) else {}
    if line.get("product_id"):
        return {}
    values = {
        key: line[key] for key in ("fit_option_code", "color", "size", "quantity")
        if line.get(key) not in (None, "")
    }
    garment = (snapshot.get("query_constraints") or {}).get("garment_type")
    if garment:
        values["garment_type"] = garment
    evidence = {}
    withdrawn_keys = set()
    decisions = IgCommerceTurnDecision.objects.filter(
        session=session, accepted=True, is_stale=False, transition__session=session,
        source_message__client_id=client.pk, source_message__sender_id=client.igsid,
        source_message__role="user", source_message__source="webhook",
    ).select_related("transition", "source_message").order_by("-transition__to_revision")[:64]
    for decision in decisions:
        transition = decision.transition
        if transition.source_message_id != decision.source_message_id:
            continue
        after = transition.next_snapshot or {}
        if int(after.get("active_index") or 0) != index:
            break
        after_lines = after.get("lines") or []
        after_line = after_lines[index] if index < len(after_lines) and isinstance(after_lines[index], dict) else {}
        if after_line.get("product_id") or (after_line and after_line.get("line_id") != line.get("line_id")):
            break
        request = decision.request_payload or {}
        original = parse_turn(decision.source_message.text)
        original_updates = dict(original.field_updates)
        updates = {**(request.get("field_updates") or {}), **(request.get("hard") or {})}
        stated = {
            "fit_option_code": updates.get("fit_option_code", updates.get("fit")),
            "color": updates.get("color"), "size": updates.get("size"),
            "quantity": updates.get("quantity", updates.get("qty")),
            "garment_type": request.get("garment_type") or (request.get("preferences") or {}).get("garment_type"),
        }
        for key, value in values.items():
            source_value = original_updates.get("fit" if key == "fit_option_code" else key)
            if key == "garment_type":
                source_value = original.garment_type
            if key not in evidence and key not in withdrawn_keys and stated.get(key) is not None and str(stated[key]) == str(value) and str(source_value) == str(value):
                evidence[key] = {"decision_id": decision.pk, "transition_id": transition.pk,
                                 "source_message_id": decision.source_message_id,
                                 "source_digest": hashlib.sha256(decision.source_message.text.encode()).hexdigest()}
        # A later rejection ends the old evidence chain. Even an accidental
        # legacy write of that value must not revive its previous source proof.
        # Corrections above may prove their new value from this same turn.
        withdrawal = (decision.result_payload or {}).get("preference_withdrawal") or {}
        if withdrawal.get("source_message_id") == decision.source_message_id:
            withdrawn_keys.update(
                "fit_option_code" if key == "fit" else key
                for key in (withdrawal.get("values") or {})
                if key in {"fit", "color", "garment_type"}
            )
        if transition.action in {"selection_reset", "product_rejected", "product_selected", "candidate_selected"}:
            break
    confirmed = {key: value for key, value in values.items() if key in evidence}
    if not confirmed:
        return {}
    return {"session_id": session.pk, "generation": session.generation,
            "revision": session.revision, "active_index": index,
            "line_id": str(line.get("line_id") or ""), "values": confirmed,
            "evidence": {key: evidence[key] for key in confirmed}}


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
