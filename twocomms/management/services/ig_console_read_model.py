"""Bounded, metadata-only projection of the existing InstagramBotLog stream.

This reader neither writes events nor decides retention. The caller supplies
current console permission and the existing writer's retention target. Filtered
rows still consume the scan cursor, so routine noise cannot trap continuation.
"""
from __future__ import annotations

from datetime import datetime, timezone as datetime_timezone
import json
import re
from typing import Any
from types import SimpleNamespace

from django.db.models import Count, Max, Min
from django.db.models.functions import Substr
from django.utils import timezone

SCHEMA_VERSION = 1

# Existing operational event codes remain queryable; arbitrary event text
# is not stored. The public reader uses the stricter finite kind/reason schema.
DB_EVENT_CODES = frozenset({
    'analysis_schedule_deferred', 'authority_claim_gate', 'avatar_store', 'bad_signature',
    'catalog_candidate_digest', 'catalog_candidate_overflow', 'catalog_media_delivery', 'catalog_media_duplicate',
    'catalog_media_fallback', 'catalog_truncated', 'claim_lost', 'claim_lost_after_send',
    'classified_skip', 'client_missing', 'commerce_delivery_finalize_recovery', 'commerce_delivery_unknown',
    'commerce_reply_sent', 'commerce_source_admission', 'commerce_suppressed', 'commerce_turn_parse',
    'commerce_turn_project', 'commerce_turn_reduce', 'conversations', 'customer_turn',
    'debt_alert_revalidation_unavailable', 'deferred_classification', 'duplicate_reply_suppressed', 'early_reply_suppressed',
    'echo', 'echo_media_during_automation', 'escalation', 'follow_boundary_legacy_fallback',
    'follow_decision_authorize', 'follow_decision_cancel', 'follow_decision_finalize', 'follow_decision_io',
    'follow_decision_prepare', 'follow_opportunity', 'follow_refusal', 'followup_schedule',
    'gemini', 'gemini_backoff', 'gemini_empty', 'gemini_fallback',
    'gemini_intelligence_missing', 'gemini_invalid_controls', 'gemini_ok', 'gemini_request_size',
    'gemini_start', 'gemini_try', 'give_up', 'holding_decision',
    'holding_reservation_release', 'ignored', 'image_download', 'inbound_namespace_unproven',
    'inbox_refresh_analysis_schedule', 'inline_media_binding', 'inline_media_budget', 'inline_media_rejected',
    'invalid_message_id', 'invalid_model_controls', 'invalid_sender_id', 'l3_deterministic',
    'lease_busy', 'logical_turn', 'manager_media_capture', 'manager_notes',
    'manual_resume', 'match_guard_local', 'media_fail_safe', 'media_recovery',
    'memory_source_enqueue_deferred', 'message_media_capture', 'message_media_store', 'missing_inbound_identity',
    'no_app_secret', 'notification_operator_action', 'notification_throttled', 'objection_attempt',
    'observed', 'observed_not_allowed', 'observed_skip', 'option_control_gate',
    'outgoing_registry', 'owned_media_hash', 'owned_media_read', 'page_token',
    'page_token_parse', 'paused_skip', 'paylink', 'paylink_awaiting_configuration',
    'paylink_control_gate', 'paylink_gate', 'paylink_inventory_unavailable', 'paylink_item_gate',
    'paylink_multi_price_gate', 'paylink_option_gate', 'paylink_payment_amount_gate', 'paylink_price_gate',
    'paylink_qty_gate', 'paylink_stock_gap_mark', 'payment_link_delivery_review', 'payment_review_operator_action',
    'phone_disclosure_gate', 'pin_product', 'policy_not_ready', 'poll_cache',
    'poll_manager_message', 'poll_messages', 'poll_namespace_unproven', 'poll_revision_echo',
    'postback_handled', 'postback_router', 'presence_summary', 'price_claim_gate',
    'price_quote_gate', 'private_media_purge', 'process', 'profile_refresh',
    'prompt_context', 'queued', 'rate_limited', 'reaction_observed',
    'reclaim', 'recovery_cancel', 'recovery_schedule', 'recovery_terminalize',
    'referral', 'registration_notification_reconcile_failed', 'repeat_guard', 'reply_sent',
    'reply_summarized_before_send', 'reply_truncated_before_send', 'reply_truth_final', 'reply_truth_pre_effect',
    'request_context_not_ready', 'revision_collaboration_recovery', 'revision_execution', 'revision_route_abstained',
    'selection_persist', 'selection_saved', 'send', 'send_blocked',
    'send_link_fallback', 'send_tag', 'send_unknown', 'settings_saved',
    'shown_cards', 'shown_cards_context', 'shown_cards_row', 'shown_products',
    'shown_products_context', 'shown_products_row', 'size_gap', 'size_gap_notify',
    'spam_block', 'stale_already_answered', 'stale_failed', 'stale_requeue',
    'start', 'state_arbiter', 'stock_gap_mark', 'stop',
    'takeover_media_capture', 'takeover_media_refresh', 'takeover_observation', 'takeover_observed',
    'takeover_transition', 'task_alert_revalidation_unavailable', 'template_rejected', 'turn_finalize',
    'turn_note', 'turn_reconcile', 'ugc_deterministic_reply', 'ugc_ingress_assessment',
    'unit_probe', 'unit_probe_unknown', 'unit_probe_warning', 'webhook_inbox_committed',
    'webhook_inbox_unavailable', 'webhook_rejected', 'webhook_signature_source',
})
PAGE_MAX = 120
DETAIL_CHARS_MAX = 4000
MAX_ID = 2**63 - 1
LEVELS = frozenset({"debug", "info", "success", "warning", "error"})
KIND_CATEGORIES = {
    "inbound_received": "client", "source_updated": "client", "payment_updated": "client",
    "decision_completed": "decision", "reply_sent": "decision", "reply_failed": "errors",
    "provider_attempt": "decision", "manager_action": "manager", "manager_notification": "manager",
    "memory_updated": "decision", "error": "errors", "cron": "routine", "health": "routine",
    "worker_state": "routine", "legacy_event": "unknown",
}
REASONS = frozenset({
    "unknown", "accepted", "completed", "pending", "processing", "failed", "cancelled", "no_reply",
    "sent", "delivery_unknown", "partial_delivery", "provider_unavailable", "provider_timeout",
    "provider_error", "quota_denied", "quota_exhausted", "quota_cooldown", "deadline",
    "source_changed", "source_admission_denied", "source_admission_unavailable", "source_erased",
    "source_scope_changed", "permission_changed", "manager_owned", "manager_takeover",
    "bot_paused", "maintenance", "lease_busy", "owner_changed", "retry_deferred",
    "recovered", "healthy", "stalled", "observation_unavailable", "legacy_unstructured",
})
ACTIONABLE_REASONS = frozenset({"failed", "delivery_unknown", "partial_delivery", "provider_unavailable",
    "provider_timeout", "provider_error", "quota_denied", "quota_exhausted", "stalled", "observation_unavailable"})
TASK_KEYS = frozenset({
    "analysis", "reply_recovery", "conversation_refresh", "journey_trace_refresh", "permission_transition",
    "inbox_refresh", "checkout_lifecycle", "follow_intelligence", "typed_memory", "memory_summary",
    "instagram", "instagram_daemon", "instagram_periodic", "manager_notifications",
})
SCOPE_IDS = frozenset({"client_id", "revision_id", "message_id", "attempt_id", "notification_id"})
SCOPE_KEYS = SCOPE_IDS | {"request_ref", "task_key"}
CATEGORIES = frozenset({"all", "client", "decision", "manager", "errors", "routine", "unknown"})
_REQUEST_REF = re.compile(r"^greq_[a-f0-9]{20}$")


def console_access_allowed(user) -> bool:
    """Use the same two capabilities as the existing full console endpoint."""
    from management.bot_access import (
        OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION, has_all_bot_capabilities,
    )
    return has_all_bot_capabilities(user, OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)


def _id(value: Any, *, zero=False) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.isascii() or not value.isdecimal() or len(value) > 19:
            return None
        value = int(value)
    if not isinstance(value, int) or not (0 if zero else 1) <= value <= MAX_ID:
        return None
    return value


def _scope(value: Any) -> dict | None:
    if not isinstance(value, dict) or set(value) - SCOPE_KEYS:
        return None
    result = {}
    for key, raw in value.items():
        if key in SCOPE_IDS:
            validated = _id(raw)
        elif key == "request_ref":
            validated = raw if isinstance(raw, str) and _REQUEST_REF.fullmatch(raw) else None
        else:
            validated = raw if isinstance(raw, str) and raw in TASK_KEYS else None
        if validated is None:
            return None
        result[key] = validated
    return dict(sorted(result.items()))


def encode_console_event(*, kind: str, scope: dict | None = None, reason: str = "unknown") -> str:
    """Pure writer contract: caller passes proven IDs/codes, never free text."""
    safe_scope = _scope({} if scope is None else scope)
    if (not isinstance(kind, str) or kind not in KIND_CATEGORIES or not isinstance(reason, str)
            or reason not in REASONS or safe_scope is None):
        raise ValueError("console_event_metadata_invalid")
    return json.dumps({"schema_version": SCHEMA_VERSION, "kind": kind, "scope": safe_scope,
        "reason": reason}, sort_keys=True, separators=(",", ":"))


def _metadata(raw: Any) -> dict | None:
    if not isinstance(raw, str) or len(raw) > DETAIL_CHARS_MAX:
        return None
    try:
        value = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != {"schema_version", "kind", "scope", "reason"}:
        return None
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        return None
    if not isinstance(value["kind"], str) or value["kind"] not in KIND_CATEGORIES:
        return None
    if not isinstance(value["reason"], str) or value["reason"] not in REASONS:
        return None
    scope = _scope(value["scope"])
    return None if scope is None else {"kind": value["kind"], "scope": scope, "reason": value["reason"]}


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(datetime_timezone.utc).isoformat() if isinstance(value, datetime) and timezone.is_aware(value) else None


def project_console_row(row) -> dict:
    """Drop arbitrary historical event/detail text, including already stored PII."""
    metadata = _metadata(getattr(row, "detail", None))
    level = getattr(row, "level", None)
    level = level if isinstance(level, str) and level in LEVELS else "info"
    kind = metadata["kind"] if metadata else "legacy_event"
    return {
        "id": _id(getattr(row, "pk", None)), "level": level, "kind": kind,
        "event": kind, "detail": "", "category": KIND_CATEGORIES[kind],
        "scope": metadata["scope"] if metadata else {},
        "reason": metadata["reason"] if metadata else "legacy_unstructured",
        "structured": metadata is not None,
        "actionable": level in {"warning", "error"} or bool(metadata and metadata["reason"] in ACTIONABLE_REASONS),
        "created_at": _iso(getattr(row, "created_at", None)),
    }


def _filters(value: Any) -> dict:
    value = value if isinstance(value, dict) else {}
    category = value.get("category", "all")
    category = category if isinstance(category, str) and category in CATEGORIES else "all"
    reason = value.get("reason", "")
    reason = reason if isinstance(reason, str) and reason in REASONS else ""
    # Invalid explicit identity filters fail closed instead of broadening to all.
    client = _id(value.get("client_id")) if value.get("client_id") not in (None, "") else None
    invalid = value.get("client_id") not in (None, "") and client is None
    return {"category": category, "reason": reason, "client_id": client,
        "include_routine": value.get("include_routine") is True, "invalid": invalid}


def _matches(item: dict, filters: dict) -> bool:
    if filters["invalid"]:
        return False
    if filters["client_id"] is not None and item["scope"].get("client_id") != filters["client_id"]:
        return False
    if filters["reason"] and item["reason"] != filters["reason"]:
        return False
    category = filters["category"]
    if category == "errors":
        return item["actionable"] or item["category"] == "errors"
    if category != "all" and item["category"] != category:
        return False
    if category == "all" and item["kind"] == "legacy_event" and not item["actionable"]:
        # Important events should not be drowned by harmless old rows whose
        # free-text detail supplies no trustworthy metadata. Explicit unknown
        # history still exposes their safe projection; scan cursors are unchanged.
        return False
    return filters["include_routine"] or category == "routine" or item["category"] != "routine" or item["actionable"]


def _group_scope_known(item: dict) -> bool:
    if not item["structured"]:
        return False
    scope, kind = item["scope"], item["kind"]
    if kind in {"provider_attempt", "reply_failed", "error"}:
        return "attempt_id" in scope
    if kind in {"health", "cron", "worker_state"}:
        return "task_key" in scope
    if kind == "manager_notification":
        return "notification_id" in scope
    if kind == "decision_completed":
        return "revision_id" in scope
    return "message_id" in scope


def _group(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        key = (item["kind"], item["scope"], item["reason"], item["level"])
        previous = result[-1] if result else None
        if previous is not None and _group_scope_known(item) and key == (
                previous["kind"], previous["scope"], previous["reason"], previous["level"]):
            previous["id"] = item["id"]
            previous["ids"].append(item["id"])
            previous["count"] += 1
            previous["last_at"] = item["created_at"]
        else:
            result.append({**item, "ids": [item["id"]], "count": 1,
                "first_at": item["created_at"], "last_at": item["created_at"]})
    return result


def build_console_payload(*, can_view: bool, after_id=0, limit=PAGE_MAX, filters=None,
        now=None, retention_target_rows=None) -> dict:
    """Read at most two queries and PAGE_MAX+1 rows; perform no writes/provider I/O.

    The high-water mark bounds each page even while producers append. Permission
    denial reads no ledger at all. IDs skipped by filters are still scanned.
    """
    captured_at = _iso(now if now is not None else timezone.now())
    after = _id(after_id, zero=True)
    if after is None:
        raise ValueError("console_cursor_invalid")
    parsed_limit = _id(limit)
    page_size = min(PAGE_MAX, parsed_limit) if parsed_limit is not None else PAGE_MAX
    safe_filters = _filters(filters)
    payload = {"schema_version": SCHEMA_VERSION, "captured_at": captured_at,
        "access": "allowed" if can_view is True else "denied", "items": [], "next_after_id": after,
        "has_more": False, "limit": page_size, "scanned_rows": 0, "filters": safe_filters,
        "range": {"oldest_available_id": None, "newest_available_id": None, "retained_rows": None},
        "retention": {"target_rows": _id(retention_target_rows), "policy_source": "existing_writer" if _id(retention_target_rows) else "unknown"},
        "retention_gap": False, "gap_reason": "", "lost_rows": None}
    if can_view is not True:
        return payload
    from management.models import InstagramBotLog
    bounds = InstagramBotLog.objects.aggregate(oldest=Min("pk"), newest=Max("pk"), retained=Count("pk"))
    payload["range"] = {"oldest_available_id": bounds["oldest"], "newest_available_id": bounds["newest"],
        "retained_rows": bounds["retained"]}
    if bounds["newest"] is None:
        return payload
    if after > bounds["newest"]:
        payload["gap_reason"] = "cursor_ahead_of_available_stream"
        return payload
    payload["retention_gap"] = bool(after and after < bounds["oldest"] - 1)
    if payload["retention_gap"]:
        payload["gap_reason"] = "before_oldest_available"
    # Truncate at the SQL boundary too: an oversized historical provider dump
    # must not be materialized merely so the Python allowlist can reject it.
    rows = list(InstagramBotLog.objects.filter(pk__gt=after, pk__lte=bounds["newest"])
        .annotate(console_detail=Substr("detail", 1, DETAIL_CHARS_MAX + 1))
        .values("pk", "level", "console_detail", "created_at").order_by("pk")[:page_size + 1])
    payload["has_more"] = len(rows) > page_size
    rows = rows[:page_size]
    payload["scanned_rows"] = len(rows)
    if rows:
        payload["next_after_id"] = rows[-1]["pk"]
    projected = [project_console_row(SimpleNamespace(pk=row["pk"], level=row["level"],
        detail=row["console_detail"], created_at=row["created_at"])) for row in rows]
    payload["items"] = _group([item for item in projected if _matches(item, safe_filters)])
    return payload


def console_event_for_log(*, level, event, kind=None, scope=None, reason=None):
    """Normalize the existing writer without parsing facts from free text.

    Proven scopes are supplied by the producer. Severity remains independent
    so an unclassified critical incident stays visible rather than becoming
    routine noise.
    """
    known = {
        "queued": ("inbound_received", "accepted"), "observed": ("inbound_received", "accepted"),
        "observed_not_allowed": ("source_updated", "accepted"), "takeover_transition": ("manager_action", "manager_takeover"),
        "reply_sent": ("reply_sent", "sent"), "send_unknown": ("reply_failed", "delivery_unknown"),
        "send_blocked": ("reply_failed", "permission_changed"), "give_up": ("reply_failed", "failed"),
        "gemini_try": ("provider_attempt", "processing"), "gemini_ok": ("provider_attempt", "completed"),
        "start": ("worker_state", "completed"), "stop": ("worker_state", "bot_paused"),
    }
    default_kind, default_reason = known.get(event, ("error", "failed") if level == "error" else ("legacy_event", "unknown"))
    try:
        detail = encode_console_event(kind=default_kind if kind is None else kind,
            scope=scope, reason=default_reason if reason is None else reason)
        return json.loads(detail)["kind"], detail
    except (ValueError, TypeError):
        # A bad diagnostic must not drop the incident or leak arbitrary input.
        return "legacy_event", encode_console_event(kind="legacy_event", reason="unknown")


def structured_log_client_id(detail):
    metadata = _metadata(detail)
    return metadata["scope"].get("client_id") if metadata else None
