"""Small, evidence-backed attention projection for the Instagram workspace."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.utils import timezone


def _seconds_since(value, *, now):
    if not value:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return max(0, int((now - value).total_seconds()))


def _coerce_datetime(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def attention_snapshot(
    *,
    now=None,
    response_debt=None,
    manager_action_required=False,
    post_sale_needs_action=False,
    next_followup_at=None,
    bot_paused=False,
    manager_takeover=False,
    commercial_visual_state="",
):
    """Return one deterministic owner for the row's attention treatment.

    The projection deliberately contains no provider calls or writes.  A
    current reply debt wins over generic manager/post-sale flags, and an
    overdue follow-up is only attention when no stronger operator obligation
    exists.  Payment/shipment remains a separate fact, never an attention
    owner.
    """
    now = now or timezone.now()
    response_debt = response_debt or {}
    debt_required = bool(response_debt.get("required"))
    if next_followup_at and timezone.is_naive(next_followup_at):
        next_followup_at = timezone.make_aware(next_followup_at, dt_timezone.utc)
    followup_overdue = bool(next_followup_at and next_followup_at <= now)
    if debt_required:
        owner = "reply_debt"
        reason = response_debt.get("reason_label") or response_debt.get("label") or "Потрібна відповідь команди"
        waiting_for = "team"
        observed_at = response_debt.get("since")
    elif manager_action_required:
        owner = "manager_action"
        reason = "Потрібна дія менеджера"
        waiting_for = "team"
        observed_at = None
    elif post_sale_needs_action:
        owner = "post_sale"
        reason = "Потрібна дія у зверненні після замовлення"
        waiting_for = "team"
        observed_at = None
    elif followup_overdue:
        owner = "overdue"
        reason = "Прострочений запланований контакт"
        waiting_for = "team"
        observed_at = next_followup_at
    else:
        owner = "none"
        reason = ""
        waiting_for = "customer" if manager_takeover else "none"
        observed_at = None

    if manager_takeover:
        mode = "manager"
    elif bot_paused and owner == "none":
        mode = "paused"
    else:
        mode = "bot"

    observed_seconds = _seconds_since(_coerce_datetime(observed_at), now=now)

    return {
        "version": 1,
        "owner": owner,
        "required": owner != "none",
        "reason": reason,
        "waiting_for": waiting_for,
        "mode": mode,
        "observed_seconds": observed_seconds,
        "target_at": next_followup_at.isoformat() if next_followup_at else "",
        "overdue": owner == "overdue",
        "commercial_state": str(commercial_visual_state or ""),
    }
