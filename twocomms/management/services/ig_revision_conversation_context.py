"""Dated conversation evidence and narrowly scoped response-delay wording."""
from __future__ import annotations

import json
import re


_DELAY_PREFIX = re.compile(
    r"\A\s*(?:(?:вибачте|перепрошую|просимо вибачення)\s+за\s+"
    r"(?:технічну\s+)?затримку(?:\s+(?:з\s+)?відповід(?:дю|і))?"
    r"|(?:извините|прошу прощения)\s+за\s+(?:техническую\s+)?задержку"
    r"(?:\s+(?:с\s+)?ответ(?:ом|а))?"
    r"|(?:sorry|apologies)\s+for\s+(?:the\s+)?(?:technical\s+)?delay"
    r"(?:\s+(?:in\s+)?(?:replying|responding))?)[.!…]+\s+", re.I,
)
_RESPONSE_COMPLAINT = re.compile(
    r"(?:чому|почему)[^.!?]{0,45}(?:не\s+відповіда|не\s+отвеча)"
    r"|(?:чекаю|жду)[^.!?]{0,35}(?:відповід|ответ)"
    r"|(?:довго|долго)[^.!?]{0,30}(?:відповіда|отвеча|відповід|ответ)"
    r"|(?:why[^.!?]{0,35}(?:no\s+reply|not\s+respond)|waiting\s+for\s+(?:a\s+)?(?:reply|response))", re.I,
)
_SUPPORT_SUBJECT = re.compile(r"замовлен|заказ|посилк|посылк|достав|відправ|отправ|order|parcel|delivery|shipping", re.I)
_SUPPORT_DELAY = re.compile(r"затрим|задерж|запіз|опозд|не\s+(?:прийш|приш|отрим|получ)|delay|late|not\s+arriv", re.I)


def _sources(revision):
    from management.services.ig_revision_outbox import _digest

    snapshot = revision.bundle_snapshot or {}
    if not revision.snapshot_digest or _digest(snapshot) != revision.snapshot_digest:
        return []
    return [source for source in snapshot.get("sources", []) if source.get("role") == "user"]


def response_delay_apology_allowed(revision):
    """A history gap or a previous holding cannot authorize a new delay claim."""
    sources = _sources(revision)
    if not sources:
        return False
    for source in sources:
        text = source.get("text") or ""
        if _RESPONSE_COMPLAINT.search(text) or (_SUPPORT_SUBJECT.search(text) and _SUPPORT_DELAY.search(text)):
            # Acknowledge the customer's current experience; this does not
            # establish a technical cause or a delivery promise.
            return True
    if revision.origin not in {"outage_recovery", "auto_refresh"}:
        return False
    from management.services.ig_revision_recovery import recovery_lineage_for_authority

    lineage, reason = recovery_lineage_for_authority(revision)
    return bool(not reason and any(row.origin == "outage_recovery" for row in lineage))


def normalize_response_delay_apology(reply_text, revision):
    """Remove only an unsupported standalone leading response-delay prefix.

    Never remove an apology for a product/shipment mistake, a quoted sentence,
    or the entire answer. The caller validates the remaining candidate normally.
    """
    match = _DELAY_PREFIX.match(reply_text)
    if not match or response_delay_apology_allowed(revision):
        return reply_text
    remainder = reply_text[match.end():]
    return remainder if len(remainder.strip()) >= 12 else reply_text


def conversation_timing_guidance(revision):
    source_times = [{"message_id": source["message_id"],
                     "provider_created_at": source.get("provider_created_at") or "unknown"}
                    for source in _sources(revision)]
    return (
        "[CURRENT REPLY TIMING CONTEXT]\n"
        + json.dumps({"source_times": source_times,
                      "response_delay_apology_supported": response_delay_apology_allowed(revision)}, separators=(",", ":"))
        + "\nOnly the current quoted customer bundle is the active request. Historical gaps, "
        "old unanswered messages, past technical holding replies and intentional operator/customer "
        "pauses do not establish a current bot delay. Do not infer a service failure or apologize "
        "for response delay from those historical facts. Acknowledge a current customer complaint "
        "or a verified recovery of this same request naturally. Legitimate apologies for an actual "
        "product/support issue remain appropriate. Never invent the technical cause."
    )


def historical_message_text(message, *, manager=False):
    stamp = message.provider_created_at or message.created_at
    label = ("DELIVERED HUMAN MANAGER MESSAGE" if manager else "HISTORICAL CONVERSATION MESSAGE")
    source = str(message.source or "unknown")
    return (
        "[" + label + ": quoted past evidence, not current delay or business authority]\n"
        + json.dumps({"actor": message.role, "message_id": message.pk,
                      "occurred_at": stamp.isoformat() if stamp else "unknown",
                      "source": source, "status": message.status,
                      "technical_holding": source in {"ai_holding", "revision_holding"},
                      "text": message.text.strip()[:4000] if manager else message.text.strip()},
                     ensure_ascii=False, separators=(",", ":"))
    )
