"""Transactional reducer and durable outbox for Instagram commerce turns."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Callable

from django.db import IntegrityError, transaction
from django.utils import timezone

from management.ig_bot_models import (
    IgCommerceManagerReview,
    IgCommerceSelectionSession,
    IgCommerceSelectionTransition,
    IgCommerceTurnDecision,
    IgClient,
)
from management.models import InstagramBotMessage
from management.services.ig_commerce_projection import (
    authoritative_session_for,
    project_active_line_to_legacy_client,
)
from management.services.ig_commerce_types import CommerceTurnRequest
from management.services.ig_delivery_receipts import (
    normalize_provider_message_id as _normalize_provider_message_id,
)


class CommerceRevisionConflict(RuntimeError):
    """The caller's optimistic session revision is no longer current."""


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return value.value
    return value


def _request_payload(request: CommerceTurnRequest) -> dict:
    return _jsonable(request)


def _event_key(message: InstagramBotMessage) -> tuple[datetime, str]:
    event_at = message.provider_created_at or message.created_at
    if event_at is None:
        event_at = datetime.min.replace(tzinfo=dt_timezone.utc)
    elif timezone.is_naive(event_at):
        event_at = timezone.make_aware(event_at, dt_timezone.utc)
    event_id = str(message.mid or message.provider_message_id or message.pk or "")
    return event_at, event_id


def _set_active_line(snapshot: dict, product_id: int) -> None:
    lines = list(snapshot.get("lines") or [])
    index = int(snapshot.get("active_index") or 0)
    if not lines:
        lines = [{"line_id": "line:0", "quantity": 1}]
        index = 0
    while index >= len(lines):
        lines.append({"line_id": f"line:{len(lines)}", "quantity": 1})
    line = dict(lines[index] or {})
    line.setdefault("line_id", f"line:{index}")
    replacing_product = int(line.get("product_id") or 0) != int(product_id)
    if replacing_product:
        # A product switch replaces the commercial identity atomically. Build
        # a fresh line so newly added price, allocation, payment, or proposal
        # fields cannot leak from the previous product.
        line = {
            "line_id": line["line_id"],
            "product_id": int(product_id),
            "quantity": 1,
        }
    else:
        line["product_id"] = int(product_id)
        line["quantity"] = max(1, int(line.get("quantity") or line.get("qty") or 1))
    lines[index] = line
    snapshot["lines"] = lines
    snapshot["active_index"] = index


def _active_line(snapshot: dict) -> dict | None:
    lines = list(snapshot.get("lines") or [])
    index = int(snapshot.get("active_index") or 0)
    if not (0 <= index < len(lines)) or not isinstance(lines[index], dict):
        return None
    return lines[index]


def _clear_active_line(snapshot: dict) -> None:
    """Remove every product-scoped value while retaining stable line identity."""
    lines = list(snapshot.get("lines") or [])
    index = int(snapshot.get("active_index") or 0)
    if not lines:
        lines = [{"line_id": "line:0"}]
        index = 0
    while index >= len(lines):
        lines.append({"line_id": f"line:{len(lines)}"})
    line = lines[index] if isinstance(lines[index], dict) else {}
    lines[index] = {"line_id": str(line.get("line_id") or f"line:{index}")}
    snapshot["lines"] = lines
    snapshot["active_index"] = index


def _apply_field_updates(snapshot: dict, request: CommerceTurnRequest) -> bool:
    updates = dict(request.field_updates or {})
    updates.update(
        {
            key: value
            for key, value in dict(request.hard or {}).items()
            if key in {"size", "color", "fit", "fit_option_code", "quantity", "qty"}
        }
    )
    if not updates:
        return False
    line = _active_line(snapshot)
    if line is None:
        line = {"line_id": "line:0", "quantity": 1}
        snapshot["lines"] = [line]
        snapshot["active_index"] = 0
    changed = False
    for key, value in updates.items():
        key = "fit_option_code" if key == "fit" else key
        key = "quantity" if key == "qty" else key
        if key not in {
            "size",
            "color",
            "fit_option_code",
            "quantity",
            "color_variant_id",
            "pay_type",
        }:
            continue
        if key == "quantity":
            try:
                value = max(1, int(value))
            except (TypeError, ValueError):
                continue
        else:
            value = str(value or "")
        if line.get(key) != value:
            line[key] = value
            changed = True
    return changed


def _apply_candidate_prompt(snapshot: dict, candidate_prompt: dict | None) -> bool:
    if not candidate_prompt:
        return False
    ids = [
        int(value)
        for value in candidate_prompt.get("product_ids") or []
        if str(value).isdigit()
    ]
    digest = str(candidate_prompt.get("digest") or "")[:64]
    provider_ids = [
        str(value)[:255]
        for value in candidate_prompt.get("provider_message_ids") or []
        if str(value).strip()
    ]
    if (
        ids == list(snapshot.get("candidate_product_ids") or [])
        and digest == str(snapshot.get("candidate_digest") or "")
        and provider_ids == list(snapshot.get("candidate_prompt_provider_ids") or [])
    ):
        return False
    snapshot["candidate_product_ids"] = ids
    snapshot["candidate_digest"] = digest
    snapshot["candidate_generation"] = int(snapshot.get("candidate_generation") or 0) + 1
    snapshot["candidate_prompt_provider_ids"] = provider_ids
    return True


def _apply_preference_withdrawals(snapshot, request, source, client) -> dict:
    """Clear only the named current choice proved by this customer's source."""
    from management.services.ig_commerce_turns import parse_turn

    if not request.preference_withdrawals or not (
        source.client_id == client.pk and source.sender_id == client.igsid
        and source.role == "user" and source.source == "webhook"
    ):
        return {}
    original = parse_turn(source.text).preference_withdrawals
    withdrawn = {}
    line = _active_line(snapshot) or {}
    for key, value in request.preference_withdrawals.items():
        if key not in {"fit", "color", "garment_type"} or original.get(key) != value:
            continue
        line_key = "fit_option_code" if key == "fit" else key
        changed = False
        if line.get(line_key) == value:
            line.pop(line_key)
            if key == "color":
                line.pop("color_variant_id", None)
            changed = True
        for constraint_key in ("selection_constraints", "query_constraints"):
            constraints = dict(snapshot.get(constraint_key) or {})
            for alias in {key, line_key}:
                if constraints.get(alias) == value:
                    constraints.pop(alias)
                    changed = True
            snapshot[constraint_key] = constraints
        if changed:
            withdrawn[key] = value
    if withdrawn:
        _clear_candidate_anchor(snapshot)
    return withdrawn


def _clear_candidate_anchor(snapshot: dict) -> None:
    snapshot["candidate_product_ids"] = []
    snapshot["candidate_digest"] = ""
    snapshot["candidate_prompt_provider_ids"] = []


def _candidate_identity_matches(
    provider_ids,
    candidate_generation: int,
    source_message: InstagramBotMessage,
    selection_number: str,
) -> bool:
    current = {str(value) for value in provider_ids or []}
    if not current:
        return False
    reply_to = str(source_message.reply_to_provider_message_id or "")
    quick = str(source_message.quick_reply_payload or "")
    if reply_to:
        return reply_to in current
    quick_parts = quick.split(":")
    return bool(
        len(quick_parts) == 4
        and quick_parts[0] == "commerce"
        and quick_parts[1] == str(int(candidate_generation or 0))
        and quick_parts[2] == "select"
        and quick_parts[3] == selection_number
    )


def _apply_numeric_candidate(
    snapshot: dict,
    source_message: InstagramBotMessage,
    request: CommerceTurnRequest,
) -> tuple[bool, str]:
    query = str(request.query or "").strip()
    if not re.fullmatch(r"\d+", query):
        return False, ""
    if not _candidate_identity_matches(
        snapshot.get("candidate_prompt_provider_ids"),
        int(snapshot.get("candidate_generation") or 0),
        source_message,
        query,
    ):
        return False, "candidate_prompt_mismatch"
    index = int(query) - 1
    ids = list(snapshot.get("candidate_product_ids") or [])
    if index < 0 or index >= len(ids):
        return False, "candidate_out_of_range"
    _set_active_line(snapshot, int(ids[index]))
    _clear_candidate_anchor(snapshot)
    return True, "candidate_selected"


def _apply_snapshot(
    session: IgCommerceSelectionSession,
    snapshot: dict,
    *,
    event_at,
    event_id: str,
) -> None:
    fields = (
        "state",
        "lines",
        "active_index",
        "selection_constraints",
        "query_constraints",
        "candidate_product_ids",
        "candidate_digest",
        "candidate_generation",
        "candidate_prompt_provider_ids",
        "rejected_selection",
        "rejected_reason",
        "pending_field",
        "pending_clarification",
        "semantic_block_key",
        "graph_digest",
    )
    for field in fields:
        if field in snapshot:
            setattr(session, field, snapshot[field])
    session.revision = int(snapshot.get("revision") or 0)
    session.last_provider_event_at = event_at
    session.last_provider_message_id = event_id
    session.save(
        update_fields=[
            *fields,
            "revision",
            "last_provider_event_at",
            "last_provider_message_id",
            "updated_at",
        ]
    )


def _ensure_review(decision: IgCommerceTurnDecision, reason: str) -> None:
    snapshot = decision.session.snapshot()
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    try:
        with transaction.atomic():
            IgCommerceManagerReview.objects.create(
                idempotency_key=f"commerce-decision:{decision.pk}:{reason}",
                client_id=decision.session.client_id,
                session=decision.session,
                decision=decision,
                reason=reason,
                selection_snapshot=snapshot,
                selection_digest=digest,
                selection_generation=decision.session.generation,
                due_at=timezone.now() + timedelta(minutes=15),
            )
    except IntegrityError:
        pass


def _create_decision(*, source_message, **kwargs) -> IgCommerceTurnDecision:
    """Let the database choose the winner when a missing-row race occurs."""
    try:
        with transaction.atomic():
            return IgCommerceTurnDecision.objects.create(
                source_message=source_message,
                **kwargs,
            )
    except IntegrityError:
        winner = (
            IgCommerceTurnDecision.objects.select_for_update()
            .filter(source_message_id=source_message.pk)
            .first()
        )
        if winner is None:
            raise
        return winner


@transaction.atomic
def apply_turn(
    client: IgClient,
    source_message: InstagramBotMessage,
    request: CommerceTurnRequest,
    *,
    expected_revision: int | None = None,
    reply_payload: dict | None = None,
    reply_builder: Callable[..., dict] | None = None,
    effects_payload: dict | None = None,
    candidate_prompt: dict | None = None,
) -> IgCommerceTurnDecision:
    """Reduce one inbound turn exactly once under the documented lock order."""
    locked_client = IgClient.objects.select_for_update().get(pk=client.pk)
    source = InstagramBotMessage.objects.select_for_update().get(pk=source_message.pk)
    session = (
        IgCommerceSelectionSession.objects.select_for_update()
        .filter(client_id=locked_client.pk, open_slot=1)
        .order_by("-generation")
        .first()
    )
    if session is None:
        session = authoritative_session_for(locked_client)
        session = IgCommerceSelectionSession.objects.select_for_update().get(pk=session.pk)
    existing = (
        IgCommerceTurnDecision.objects.select_for_update()
        .filter(source_message_id=source.pk)
        .first()
    )
    if existing is not None:
        return existing
    current_revision = int(session.revision or 0)
    if expected_revision is not None and int(expected_revision) != current_revision:
        raise CommerceRevisionConflict(
            f"commerce session {session.pk} revision {current_revision} "
            f"!= expected {expected_revision}"
        )

    request_payload = _request_payload(request)
    before = session.snapshot()
    after = dict(before)
    after["lines"] = [
        dict(line) if isinstance(line, dict) else line
        for line in before.get("lines") or []
    ]
    reasons: list[str] = []
    accepted = True
    action = "turn_observed"
    event_at, event_id = _event_key(source)
    last_at = session.last_provider_event_at
    if last_at is not None and timezone.is_naive(last_at):
        last_at = timezone.make_aware(last_at, dt_timezone.utc)
    if last_at is not None and (event_at, event_id) <= (
        last_at,
        str(session.last_provider_message_id or ""),
    ):
        return _create_decision(
            source_message=source,
            session=session,
            request_payload=request_payload,
            result_payload={"reason": "stale_provider_event"},
            reply_payload=reply_payload or {},
            effects_payload=effects_payload or {},
            accepted=False,
            is_stale=True,
            delivery_required=False,
            delivery_state=IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED,
        )

    if _apply_candidate_prompt(after, candidate_prompt):
        action = "candidate_prompt_replaced"
        reasons.append("candidate_prompt_replaced")

    selection_constraints = dict(after.get("selection_constraints") or {})
    incoming_selection_constraints = {
        **dict(request.semantic_constraints or {}),
        **dict(request.hard or {}),
    }
    if incoming_selection_constraints:
        selection_constraints.update(incoming_selection_constraints)
        after["selection_constraints"] = selection_constraints
        if not reasons:
            action = "selection_constraints_updated"
            reasons.append("selection_constraints_updated")
    query_constraints = dict(after.get("query_constraints") or {})
    incoming_query_constraints = dict(request.preferences or {})
    if request.garment_type:
        incoming_query_constraints["garment_type"] = request.garment_type
    if request.query:
        incoming_query_constraints["query"] = request.query
    if incoming_query_constraints:
        query_constraints.update(incoming_query_constraints)
        after["query_constraints"] = query_constraints
        if not reasons:
            action = "query_constraints_updated"
            reasons.append("query_constraints_updated")

    rejected_product_ids = sorted(
        {
            int(product_id)
            for product_id in (request.rejected_product_ids or ())
            if str(product_id).isdigit() and int(product_id) > 0
        }
    )
    active_line = _active_line(after) or {}
    active_product_id = int(active_line.get("product_id") or 0)
    rejects_active_product = bool(
        rejected_product_ids and active_product_id in rejected_product_ids
    )

    numeric_accepted, numeric_reason = _apply_numeric_candidate(after, source, request)
    if numeric_accepted:
        # A numbered candidate is also a product switch. Keep only constraints
        # stated by the current turn and do not inherit the rejected product's
        # configuration or search context.
        after["selection_constraints"] = {}
        after["query_constraints"] = {}
        after["pending_field"] = ""
        after["pending_clarification"] = ""
    candidate_rejected = False
    if numeric_reason:
        if not numeric_accepted:
            candidate_rejected = True
            accepted = False
            action = "candidate_rejected"
            reasons.append(numeric_reason)
            after["rejected_selection"] = {
                "query": str(request.query or "")[:40],
                "reply_to_provider_message_id": str(
                    source.reply_to_provider_message_id or ""
                )[:255],
                "quick_reply_payload": str(source.quick_reply_payload or "")[:1000],
            }
            after["rejected_reason"] = numeric_reason
        else:
            action = "candidate_selected"
            reasons.append(numeric_reason)

    withdrawn = {} if candidate_rejected else _apply_preference_withdrawals(
        after, request, source, locked_client,
    )
    if withdrawn:
        action = "preference_withdrawn"
        reasons.append("explicit_preference_withdrawal")

    if candidate_rejected:
        pass
    elif rejects_active_product and not request.exact_product_id:
        # A customer rejection is stronger than the legacy product projection.
        # Keep only explicitly supplied replacement constraints; every old
        # product/configuration/price/allocation value becomes inapplicable.
        _clear_active_line(after)
        _clear_candidate_anchor(after)
        after["selection_constraints"] = dict(incoming_selection_constraints)
        after["query_constraints"] = dict(incoming_query_constraints)
        after["rejected_selection"] = {"product_ids": rejected_product_ids}
        after["rejected_reason"] = "customer_rejected_product"
        after["pending_field"] = ""
        after["pending_clarification"] = ""
        action = "product_rejected"
        reasons.append("customer_rejected_product")
        if _apply_field_updates(after, request):
            reasons.append("explicit_field_update")
    elif request.reset_requested:
        after["lines"] = []
        after["active_index"] = 0
        after["selection_constraints"] = {}
        after["query_constraints"] = {}
        action = "selection_reset"
        reasons.append("explicit_reset")
    elif request.exact_product_id:
        _set_active_line(after, int(request.exact_product_id))
        _clear_candidate_anchor(after)
        after["selection_constraints"] = dict(incoming_selection_constraints)
        after["query_constraints"] = dict(incoming_query_constraints)
        after["pending_field"] = ""
        after["pending_clarification"] = ""
        action = "product_selected"
        reasons.append("exact_product_reference")
        if _apply_field_updates(after, request):
            reasons.append("explicit_field_update")
    elif _apply_field_updates(after, request):
        action = "selection_updated"
        reasons.append("explicit_field_update")
    elif request.pending_clarification:
        after["pending_clarification"] = str(request.pending_clarification)[:120]
        action = "clarification_requested"
        reasons.append("pending_clarification")
    elif request.info_topics:
        action = "information_only"
        reasons.append("information_only")
    elif request.checkout_requested:
        action = "checkout_requested"
        reasons.append("checkout_requested")
    elif not reasons:
        accepted = False
        action = "turn_unresolved"
        reasons.append("no_state_change")

    after["revision"] = current_revision + 1
    after["last_provider_message_id"] = event_id
    if reply_payload is None and reply_builder is not None:
        reply_payload = reply_builder(
            request,
            action=action,
            reasons=tuple(reasons),
            before=before,
            after=after,
        )
    transition = IgCommerceSelectionTransition.objects.create(
        session=session,
        source_message=source,
        action=action,
        from_revision=current_revision,
        to_revision=after["revision"],
        previous_snapshot=before,
        next_snapshot=after,
        effects=effects_payload or {},
        reasons=reasons,
        graph_digest=str(after.get("graph_digest") or ""),
        source_order_key=f"{event_at.isoformat()}|{event_id}",
    )
    _apply_snapshot(session, after, event_at=event_at, event_id=event_id)
    project_active_line_to_legacy_client(session, locked_client)
    delivery_required = bool(reply_payload)
    return _create_decision(
        source_message=source,
        session=session,
        transition=transition,
        request_payload=request_payload,
        result_payload={
            "reason": reasons[-1] if reasons else action,
            "candidate_generation": session.candidate_generation,
            **({"preference_withdrawal": {
                "values": withdrawn,
                "source_message_id": source.pk,
                "source_digest": hashlib.sha256(str(source.text or "").encode()).hexdigest(),
            }} if withdrawn else {}),
        },
        reply_payload=reply_payload or {},
        effects_payload=effects_payload or {},
        accepted=accepted,
        is_stale=False,
        delivery_required=delivery_required,
        delivery_state=(
            IgCommerceTurnDecision.DeliveryState.PENDING
            if delivery_required
            else IgCommerceTurnDecision.DeliveryState.NOT_REQUIRED
        ),
    )


def claim_decision_delivery(decision: IgCommerceTurnDecision) -> IgCommerceTurnDecision:
    """Atomically claim a pending outbox row before any provider I/O."""
    with transaction.atomic():
        locked = IgCommerceTurnDecision.objects.select_for_update().get(pk=decision.pk)
        if (
            locked.delivery_state != IgCommerceTurnDecision.DeliveryState.PENDING
            or not locked.delivery_required
        ):
            locked._delivery_claimed = False
            return locked
        now = timezone.now()
        locked.delivery_state = IgCommerceTurnDecision.DeliveryState.SENDING
        locked.attempts = int(locked.attempts or 0) + 1
        locked.delivery_started_at = now
        locked.last_attempt_at = now
        locked.save(
            update_fields=[
                "delivery_state",
                "attempts",
                "delivery_started_at",
                "last_attempt_at",
                "updated_at",
            ]
        )
        locked._delivery_claimed = True
        return locked


def _provider_ids(payload: dict) -> list[str]:
    ids: list[str] = []
    for key in ("text_receipts", "media_receipts"):
        for receipt in payload.get(key) or []:
            if isinstance(receipt, dict):
                value = receipt.get("provider_message_id") or receipt.get("id")
                normalized = _normalize_provider_message_id(value)
                if normalized:
                    ids.append(normalized)
    return ids


def _expected_parts(reply_payload: dict, key: str) -> int:
    value = reply_payload.get(key)
    if value in (None, "", []):
        return 0
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1


def _receipts_complete(decision: IgCommerceTurnDecision, result: dict) -> bool:
    expected = {
        "text_receipts": _expected_parts(decision.reply_payload or {}, "text"),
        "media_receipts": _expected_parts(decision.reply_payload or {}, "media"),
    }
    for key, count in expected.items():
        if count == 0:
            continue
        receipts = result.get(key) or []
        covered = {
            int(receipt.get("index"))
            for receipt in receipts
            if isinstance(receipt, dict)
            and str(receipt.get("index", "")).isdigit()
            and _normalize_provider_message_id(
                receipt.get("provider_message_id") or receipt.get("id")
            )
        }
        if not set(range(count)).issubset(covered):
            return False
    return True


def _receipts_have_invalid_ids(decision: IgCommerceTurnDecision, result: dict) -> bool:
    expected = {
        "text_receipts": _expected_parts(decision.reply_payload or {}, "text"),
        "media_receipts": _expected_parts(decision.reply_payload or {}, "media"),
    }
    for key, count in expected.items():
        if count == 0:
            continue
        for receipt in result.get(key) or []:
            if not isinstance(receipt, dict):
                continue
            index = receipt.get("index")
            if not str(index if index is not None else "").isdigit() or int(index) not in range(count):
                continue
            raw_id = receipt.get("provider_message_id") or receipt.get("id")
            if not _normalize_provider_message_id(raw_id):
                return True
    return False


def resume_turn_delivery(
    source_message: InstagramBotMessage,
    *,
    transport: Callable[[IgCommerceTurnDecision], dict] | None = None,
) -> IgCommerceTurnDecision | None:
    """Deliver one pending decision; ambiguous boundaries are never retried."""
    decision = (
        IgCommerceTurnDecision.objects.select_related("session")
        .filter(source_message_id=source_message.pk)
        .first()
    )
    if decision is None:
        return None
    if transport is None:
        raise ValueError("resume_turn_delivery requires an injected transport")
    claimed = claim_decision_delivery(decision)
    if not getattr(claimed, "_delivery_claimed", False):
        if claimed.delivery_required and claimed.delivery_state in {
            IgCommerceTurnDecision.DeliveryState.SENDING,
            IgCommerceTurnDecision.DeliveryState.UNKNOWN,
            IgCommerceTurnDecision.DeliveryState.PARTIAL,
        }:
            with transaction.atomic():
                locked = (
                    IgCommerceTurnDecision.objects.select_for_update()
                    .select_related("session")
                    .get(pk=claimed.pk)
                )
                locked.reconciliation_status = (
                    IgCommerceTurnDecision.ReconciliationStatus.REQUIRED
                )
                locked.save(update_fields=["reconciliation_status", "updated_at"])
                _ensure_review(locked, f"delivery_{locked.delivery_state}")
                return locked
        return claimed
    try:
        result = transport(claimed) or {}
    except Exception as exc:
        with transaction.atomic():
            locked = IgCommerceTurnDecision.objects.select_for_update().get(pk=claimed.pk)
            locked.delivery_state = IgCommerceTurnDecision.DeliveryState.UNKNOWN
            locked.delivery_error = str(exc)[:1000]
            locked.reconciliation_status = IgCommerceTurnDecision.ReconciliationStatus.REQUIRED
            locked.save(
                update_fields=[
                    "delivery_state",
                    "delivery_error",
                    "reconciliation_status",
                    "updated_at",
                ]
            )
            _ensure_review(locked, "delivery_unknown")
            return locked
    if not isinstance(result, Mapping):
        result = {
            "state": IgCommerceTurnDecision.DeliveryState.UNKNOWN,
            "error": "invalid_transport_result",
        }
    state = str(result.get("state") or "").strip().lower()
    if state not in {
        IgCommerceTurnDecision.DeliveryState.SENT,
        IgCommerceTurnDecision.DeliveryState.PARTIAL,
        IgCommerceTurnDecision.DeliveryState.UNKNOWN,
    }:
        state = IgCommerceTurnDecision.DeliveryState.UNKNOWN
        result = dict(result)
        result.setdefault("error", "invalid_delivery_state")
    if (
        state == IgCommerceTurnDecision.DeliveryState.SENT
        and not _receipts_complete(claimed, result)
    ):
        state = (
            IgCommerceTurnDecision.DeliveryState.UNKNOWN
            if (
                not _provider_ids(result)
                or _receipts_have_invalid_ids(claimed, result)
            )
            else IgCommerceTurnDecision.DeliveryState.PARTIAL
        )
    if state == IgCommerceTurnDecision.DeliveryState.SENT:
        expected_receipts = _expected_parts(claimed.reply_payload or {}, "text") + _expected_parts(
            claimed.reply_payload or {}, "media"
        )
        provider_ids = _provider_ids(result)
        if (
            len(provider_ids) != expected_receipts
            or len(set(provider_ids)) != expected_receipts
        ):
            state = IgCommerceTurnDecision.DeliveryState.UNKNOWN
            result = dict(result)
            result.setdefault("error", "invalid_provider_message_id")
    with transaction.atomic():
        locked = IgCommerceTurnDecision.objects.select_for_update().get(pk=claimed.pk)
        locked.delivery_state = state
        locked.text_receipts = result.get("text_receipts") or []
        locked.media_receipts = result.get("media_receipts") or []
        locked.provider_message_ids = _provider_ids(result)
        locked.delivery_error = str(result.get("error") or "")[:1000]
        locked.delivered_at = (
            timezone.now()
            if state == IgCommerceTurnDecision.DeliveryState.SENT
            else None
        )
        locked.reconciliation_status = (
            IgCommerceTurnDecision.ReconciliationStatus.NOT_REQUIRED
            if state == IgCommerceTurnDecision.DeliveryState.SENT
            else IgCommerceTurnDecision.ReconciliationStatus.REQUIRED
        )
        locked.save(
            update_fields=[
                "delivery_state",
                "text_receipts",
                "media_receipts",
                "provider_message_ids",
                "delivery_error",
                "delivered_at",
                "reconciliation_status",
                "updated_at",
            ]
        )
        if state != IgCommerceTurnDecision.DeliveryState.SENT:
            _ensure_review(locked, f"delivery_{state}")
        return locked
