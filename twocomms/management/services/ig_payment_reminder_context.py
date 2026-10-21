"""Captured reminder agreement context; never a sending or payment authority.

The service verifies persisted sources and the original purchase. This adapter
only admits its finite snapshot into an enclosing capture; renderers do no I/O.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo

from management.services.ig_client_state_card import CAPTURE_SCOPE_KEYS, SCOPE_KEYS

VERSION = "payment-reminder-context.v1"
SLOT = "payment.reminder"
KYIV = ZoneInfo("Europe/Kyiv")
SOURCE_ROLES = ("request", "card", "answer", "send")
PURCHASE_KEYS = ("selection_session_id", "selection_generation", "selection_revision", "source_cart_capture_digest")
STATE_LABELS = {
    "awaiting_time": "Очікує дати й часу від клієнта",
    "awaiting_delivery": "Підтвердження ще не доставлено",
    "awaiting_confirmation": "Очікує підтвердження клієнта",
    "accepted": "Узгоджено — очікує свого часу",
    "sent": "Нагадування надіслано",
    "revoked": "Домовленість скасовано",
    "expired": "Час нагадування минув",
    "delivery_unknown": "Результат надсилання потребує перевірки",
    "unavailable": "Нагадування недоступне",
    "unknown": "Стан невідомий",
    "none": "Домовленості немає",
}
STATUS_MAP = {**{key: key for key in STATE_LABELS}, "pending": "accepted", "processing": "accepted",
    "cancelled": "revoked", "ambiguous": "delivery_unknown", "failed": "unavailable", "skipped": "unavailable",
    "blocked": "unavailable"}
REASONS = frozenset({"", "payment_reminder_absent", "payment_reminder_capture_unavailable",
    "payment_reminder_projection_invalid", "payment_reminder_scope_changed", "payment_reminder_source_after_capture",
    "payment_reminder_source_before_reset", "payment_reminder_source_invalid", "payment_reminder_state_unverified"})


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("naive timestamp")
    return result


def _unknown(boundary, captured_at, reason):
    return {"value": None, "status": "unknown", "authority": "none", "source_refs": [],
        "scope": {key: boundary.get(key) for key in SCOPE_KEYS},
        "capture_scope": {key: boundary.get(key) for key in (*CAPTURE_SCOPE_KEYS, *PURCHASE_KEYS)},
        "observed_at": captured_at.isoformat(), "validity": "unknown", "mandatory": False,
        "omission_reason": reason}


def payment_reminder_slot(projection, *, boundary, captured_at):
    """Admit only source-backed state within this exact frozen purchase/time."""
    if (not isinstance(projection, dict) or type(projection.get("version")) is not int
            or projection["version"] != 1 or not isinstance(projection.get("status"), str)):
        return _unknown(boundary, captured_at, "payment_reminder_projection_invalid")
    if projection.get("status") == "none":
        return _unknown(boundary, captured_at, "payment_reminder_absent")
    state = STATUS_MAP.get(projection.get("status"))
    if state is None or state in {"none", "unknown"}:
        return _unknown(boundary, captured_at, "payment_reminder_state_unverified")
    try:
        if (type(projection.get("client_id")) is not int or projection["client_id"] != boundary.get("client_id")
                or _time(projection["captured_at"]) != captured_at):
            return _unknown(boundary, captured_at, "payment_reminder_projection_invalid")
    except (TypeError, ValueError, KeyError):
        return _unknown(boundary, captured_at, "payment_reminder_projection_invalid")
    scope = projection.get("source_scope")
    if (not isinstance(scope, dict) or boundary.get("historical") is True
            or boundary.get("erasure_epoch") or not boundary.get("source_cart_capture_digest")
            or any(key not in scope or scope[key] != boundary.get(key) for key in
                ("client_id", "episode_id", "reset_floor", "source_namespace", *PURCHASE_KEYS))):
        return _unknown(boundary, captured_at, "payment_reminder_scope_changed")
    ids, times = projection.get("source_ids"), projection.get("source_times")
    if (not isinstance(ids, dict) or set(ids) - set(SOURCE_ROLES)
            or not isinstance(times, dict) or set(times) - set(SOURCE_ROLES)):
        return _unknown(boundary, captured_at, "payment_reminder_source_invalid")
    needed = ["request"]
    if state in {"awaiting_time", "awaiting_confirmation", "accepted", "sent", "revoked", "delivery_unknown"}:
        needed.append("card")
    if state in {"accepted", "sent", "revoked", "delivery_unknown"}:
        needed.append("answer")
    if state == "sent":
        needed.append("send")
    if any(type(ids.get(role)) is not int or ids[role] <= 0 for role in needed):
        return _unknown(boundary, captured_at, "payment_reminder_source_invalid")
    watermark = boundary.get("source_watermark") or boundary.get("watermark") or {}
    refs = []
    try:
        cutoff = _time(watermark["event_at"])
        if captured_at.tzinfo is None or type(watermark.get("message_id")) is not int:
            raise ValueError("capture boundary unknown")
        if state == "awaiting_time":
            if (projection.get("due_at") is not None or projection.get("due_at_kyiv") is not None
                or projection.get("task_id") is not None or projection.get("invitation_id") is not None
                or projection.get("prompt_receipt_confirmed") is not True
                or any(ids.get(role) is not None or times.get(role) is not None for role in ("answer", "send"))):
                raise ValueError("time request is not a scheduled agreement")
            due = None
        else:
            due = _time(projection["due_at"])
        for role in SOURCE_ROLES:
            identity = ids.get(role)
            if identity is None:
                continue
            if type(identity) is not int or identity <= 0:
                raise ValueError("source identity invalid")
            event_at = _time(times[role])
            if identity > watermark["message_id"] or event_at > cutoff or event_at > captured_at:
                return _unknown(boundary, captured_at, "payment_reminder_source_after_capture")
            if identity < boundary["reset_floor"]:
                return _unknown(boundary, captured_at, "payment_reminder_source_before_reset")
            refs.append({"kind": "message", "id": identity, "event_at": event_at.isoformat()})
    except (TypeError, ValueError, KeyError, OverflowError):
        return _unknown(boundary, captured_at, "payment_reminder_source_invalid")
    # All public strings are produced here, never copied from model/provider text.
    due_label = due.astimezone(KYIV).strftime("%d.%m.%Y о %H:%M") + " (Київ)" if due else None
    value = {"version": VERSION, "state": state, "state_label": STATE_LABELS[state],
        "scheduled_for": due.isoformat() if due else None, "scheduled_for_label": due_label,
        "purchase_label": "Поточна покупка — усі погоджені позиції",
        "business_agreement": state in {"accepted", "sent", "delivery_unknown"},
        "native_future_permission": "unverified", "one_reminder_only": True,
        "automatic_invoice_reissue": False, "payment_or_refund_confirmed": False,
        "stock_or_price_promised": False}
    return {"value": value, "status": "confirmed", "authority": "conversation_agreement",
        "source_refs": refs, "scope": {key: boundary.get(key) for key in SCOPE_KEYS},
        "capture_scope": {key: boundary.get(key) for key in (*CAPTURE_SCOPE_KEYS, *PURCHASE_KEYS)},
        "source_watermark": deepcopy(watermark), "observed_at": captured_at.isoformat(),
        "validity": "valid", "mandatory": True, "omission_reason": ""}


def capture_payment_reminder_context(client, *, boundary, captured_at, current_source_cart=None):
    """One service read; the same slot is then reused by all consumers."""
    try:
        from management.services.ig_payment_reminders import payment_reminder_projection
        projection = payment_reminder_projection(client, now=captured_at,
            current_source_cart=deepcopy(current_source_cart))
    except Exception:
        return _unknown(boundary, captured_at, "payment_reminder_capture_unavailable")
    return payment_reminder_slot(projection, boundary=boundary, captured_at=captured_at)


def payment_reminder_manifest(slot):
    """Content-free closed proof derived from the admitted slot, without I/O."""
    value = slot.get("value") or {}
    valid = slot.get("status") == "confirmed" and value.get("state") in STATE_LABELS
    due_epoch = int(_time(value["scheduled_for"]).timestamp()) if valid and value["state"] != "awaiting_time" else 0
    watermark = slot.get("source_watermark") or {}
    observed = slot.get("observed_at")
    reason = slot.get("omission_reason") or ""
    return {"version": VERSION, "status": value["state"] if valid else "unknown",
        "reason": reason if reason in REASONS else "payment_reminder_projection_invalid",
        "source_message_ids": sorted({ref["id"] for ref in slot.get("source_refs") or []
            if ref.get("kind") == "message" and type(ref.get("id")) is int}),
        "scheduled_for_epoch": due_epoch, "scope_digest": _digest(slot.get("capture_scope") or {}),
        "as_of_epoch": int(_time(observed).timestamp()) if observed else 0,
        "source_watermark_id": watermark.get("message_id") or 0,
        "source_watermark_epoch": int(_time(watermark["event_at"]).timestamp()) if watermark.get("event_at") else 0,
        "capture_digest": _digest(slot)}
