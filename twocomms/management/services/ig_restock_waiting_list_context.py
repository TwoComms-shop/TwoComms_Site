"""Bounded captured restock agreements; no purchase or dispatch authority.

The native reader alone verifies consent. Pure adapters retain its exact list
and historical dates under the enclosing capture, independently of today's cart.
"""
from copy import deepcopy
from datetime import datetime
import re

from management.services.ig_client_state_card import CAPTURE_SCOPE_KEYS, SCOPE_KEYS

SUMMARY = "waiting_list.interest"
DETAILS = "waiting_list.items"
MAX_CONTEXT_READS = 24
MAX_LISTS = 1
MAX_ITEMS = 16


def waiting_list_owner_query(queryset, *, alias="_has_waiting_list"):
    """Fold a routing hint into an existing owner SELECT; it grants no facts."""
    from django.db.models import Exists, OuterRef
    from management.ig_restock_subscription_models import IgRestockInvitation
    from management.services.ig_restock_waiting_list import SCHEMA
    return queryset.annotate(**{alias: Exists(IgRestockInvitation.objects.filter(
        client_id=OuterRef("pk"), target__schema=SCHEMA))})


def _time(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("waiting_list_time_invalid")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("waiting_list_time_invalid")
    return result


def _scope(boundary):
    raw = boundary.get("scope") if isinstance(boundary.get("scope"), dict) else boundary
    return {key: raw.get(key) for key in CAPTURE_SCOPE_KEYS}


def _slot(value, boundary, captured_at, *, refs=(), authority="conversation_agreement", mandatory=False):
    scope = _scope(boundary)
    return {"value": value, "status": "confirmed", "authority": authority,
        "source_refs": deepcopy(list(refs)), "scope": {key: scope[key] for key in SCOPE_KEYS},
        "capture_scope": scope, "observed_at": captured_at.isoformat(),
        "source_watermark": deepcopy(boundary.get("source_watermark") or {}),
        "validity": "valid", "mandatory": mandatory, "omission_reason": ""}


def unavailable_waiting_list_slots(boundary, captured_at, reason):
    """A known capture omission must never be interpreted as no agreement."""
    reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,64}", reason) else "waiting_list_unavailable"
    return {SUMMARY: _slot({"state": "not_captured", "reason": reason, "coverage_complete": False},
        boundary, captured_at, refs=[{"kind": "capture_boundary", "id": _scope(boundary)["client_id"]}],
        authority="derived", mandatory=True)}


def waiting_list_slots(projection, *, boundary, captured_at):
    """Pure admission of a frozen native DTO, never a fresh/latest read."""
    failure = lambda reason: unavailable_waiting_list_slots(boundary, captured_at, reason)
    scope = _scope(boundary)
    if boundary.get("historical") or boundary.get("erasure_epoch") or boundary.get("erasure_started"):
        return failure("waiting_list_scope_unavailable")
    try:
        if (not isinstance(projection, dict) or projection.get("version") != 2
            or type(projection.get("client_id")) is not int or projection["client_id"] != scope["client_id"]
            or _time(projection["captured_at"]) != captured_at
            or type(projection.get("coverage_complete")) is not bool
            or projection.get("native_future_capability") != "unverified"
            or not isinstance(projection.get("lists"), list) or len(projection["lists"]) > MAX_LISTS):
            return failure("waiting_list_projection_invalid")
        watermark = boundary.get("source_watermark") or {}
        cutoff = min(captured_at, _time(watermark["event_at"]))
        summaries, details, refs = [], [], []
        for row in projection["lists"]:
            agreement, manager, notification = row["agreement"], row["manager_review"], row["notification"]
            state = agreement["state"]
            if (row.get("version") != 2 or row.get("completion") != "all"
                or not isinstance(row.get("id"), str) or len(row["id"]) > 64
                or row.get("locale") not in {"uk", "ru", "en"}
                or state not in {"invited", "accepted", "revoked", "expired", "unverified"}
                or not isinstance(row.get("items"), list) or not 1 <= len(row["items"]) <= MAX_ITEMS
                or not isinstance(row.get("source_refs"), list) or not row["source_refs"]):
                return failure("waiting_list_projection_invalid")
            for ref in row["source_refs"]:
                if (ref.get("kind") != "message" or type(ref.get("id")) is not int
                    or not scope["reset_floor"] <= ref["id"] <= watermark["message_id"]
                    or _time(ref["event_at"]) > cutoff):
                    return failure("waiting_list_source_after_capture")
                safe_ref = {key: ref[key] for key in ("kind", "id", "event_at")}
                if safe_ref not in refs:
                    refs.append(safe_ref)
            accepted_at = agreement.get("accepted_at")
            if accepted_at is not None and _time(accepted_at) > cutoff:
                return failure("waiting_list_source_after_capture")
            if state in {"accepted", "revoked"} and (accepted_at is None
                or agreement.get("invitation_receipt_confirmed") is not True
                or len({ref["id"] for ref in row["source_refs"]}) < (3 if state == "revoked" else 2)
                or row["source_refs"][1]["event_at"] != accepted_at):
                return failure("waiting_list_agreement_unverified")
            expires_at = agreement["expires_at"]
            if _time(expires_at) <= captured_at and state == "accepted":
                return failure("waiting_list_agreement_expired")
            if manager.get("checked_at") is not None and _time(manager["checked_at"]) > captured_at:
                return failure("waiting_list_manager_after_capture")
            if notification.get("sent_at") is not None and _time(notification["sent_at"]) > captured_at:
                return failure("waiting_list_send_after_capture")
            sent = notification.get("state") == "sent"
            if notification.get("state") not in {"sent", "unknown", "pending", "not_sent"}:
                return failure("waiting_list_projection_invalid")
            if sent and (notification.get("native_receipt_confirmed") is not True or not notification.get("sent_at")):
                return failure("waiting_list_send_unverified")
            safe_items = []
            for item in row["items"]:
                keys = ("line_id", "recipient_id", "title", "color", "size", "fit", "options", "quantity", "item_kind")
                if (set(item) != set(keys) or type(item["quantity"]) is not int or not 1 <= item["quantity"] <= 50
                    or any(not isinstance(item[key], str) or len(item[key]) > 240 for key in keys if key not in {"quantity", "options"})
                    or not item["line_id"] or not item["title"] or not isinstance(item["options"], dict)
                    or len(item["options"]) > 8 or any(not isinstance(key, str) or not isinstance(value, str)
                        or len(key) > 100 or len(value) > 200 for key, value in item["options"].items())):
                    return failure("waiting_list_items_invalid")
                safe_items.append(deepcopy(item))
            if sum(item["quantity"] for item in safe_items) > 50:
                return failure("waiting_list_items_invalid")
            summaries.append({"id": row["id"], "state": state, "count": len(safe_items),
                "accepted_at": accepted_at, "expires_at": expires_at,
                "physical": "not_captured", "all_ready": "unverified", "notification": notification.get("state", "unknown"),
                "sent_at": notification.get("sent_at"), "channel": "not_evaluated"})
            details.append({"id": row["id"], "completion": "all", "items": safe_items})
        complete = projection["coverage_complete"]
        value = {"lists": summaries, "coverage_complete": complete,
            "details": "captured" if details else "none" if complete else "unavailable"}
        if projection.get("reason"):
            if not re.fullmatch(r"[a-z0-9_]{1,64}", projection["reason"]):
                return failure("waiting_list_projection_invalid")
            value["reason"] = projection["reason"]
        if not refs:
            refs = [{"kind": "capture_boundary", "id": scope["client_id"]}]
        result = {SUMMARY: _slot(value, boundary, captured_at, refs=refs,
            authority="conversation_agreement" if summaries else "derived",
            mandatory=bool(any(row["state"] == "accepted" for row in summaries) or not complete))}
        if details:
            result[DETAILS] = _slot({"lists": details}, boundary, captured_at, refs=refs)
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        return failure("waiting_list_projection_invalid")


def capture_waiting_list_context(client_id, *, boundary, captured_at, max_read_queries=MAX_CONTEXT_READS):
    """One lower-budget native projection, followed by pure scoped admission."""
    if type(max_read_queries) is not int or max_read_queries <= 0:
        return unavailable_waiting_list_slots(boundary, captured_at, "waiting_list_read_budget_exceeded")
    from management.services.ig_restock_waiting_list import waiting_list_projection
    projection = waiting_list_projection(client_id, now=captured_at, limit=MAX_LISTS,
        include_manager_review=False, max_read_queries=min(MAX_CONTEXT_READS, max_read_queries))
    return waiting_list_slots(projection, boundary=boundary, captured_at=captured_at)
