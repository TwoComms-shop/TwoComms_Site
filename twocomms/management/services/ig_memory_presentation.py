"""Finite, read-only manager projection of an already frozen memory read.

The presentation never discovers a head, parses machine prose into facts,
returns arbitrary provenance, or grants commercial authority to a topic hint.
"""
from collections.abc import Mapping
from datetime import datetime
import re

from management.services.ig_memory_producer import validate_memory_timeline_read


SCHEMA = "ig-memory-presentation.v1"
LEGACY_CHARS = 2000
TOPIC_LABELS = {
    "gift": "Подарунок", "self_purchase": "Для себе", "recipient": "Одержувач",
    "availability_inquiry": "Питання про товар", "correction": "Уточнення",
    "purchase_inquiry": "Запит про покупку", "other": "Інша тема",
}
OMISSION_LABELS = {
    "quote_context_loss": "Уривок без повного контексту",
    "quoted_source_context": "Цитований або переказаний текст",
    "quote_too_long": "Задовга цитата",
    "retained_source_unavailable": "Джерело недоступне",
    "retained_source_changed": "Джерело змінилося",
    "retained_not_selected": "Не включено до поточного огляду",
    "contact_details": "Контактні дані приховано",
}
_MACHINE = re.compile(
    r"\[HISTORICAL SOURCE OBSERVATIONS|\[COVERAGE\]|\[UNTRUSTED|"
    r"captured-memory\.|client-memory-timeline\.|topic_hint\s*[=:]|"
    r"time_basis\s*[=:]|source_digest\s*[\":=]|read_delta\s*[\":=]", re.I)


def _iso(value):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        return stamp.isoformat() if isinstance(stamp, datetime) and stamp.utcoffset() is not None else ""
    except (TypeError, ValueError, OverflowError):
        return ""


def _empty(status="empty", reason_label="Збережених спостережень ще немає."):
    return {"schema": SCHEMA, "status": status, "reason_label": reason_label,
        "events": [], "coverage": {"source_count": 0, "selected_count": 0,
            "retained_count": 0, "event_count": 0, "omitted_count": 0,
            "pending_count": 0, "omissions": []},
        "updated_at": "", "as_of": "", "legacy_text": ""}


def _reason(reason):
    # Raw validator codes never become manager copy or a source of UI markup.
    if reason == "client_hidden":
        return "Пам’ять приховано для прихованого клієнта."
    if reason == "client_erasing":
        return "Пам’ять недоступна під час видалення даних."
    if reason in {"narrative_empty", "timeline_provenance_missing"}:
        return "Збережених спостережень ще немає."
    if reason == "narrative_provenance_missing":
        return "Попередній текстовий огляд не має підтверджених джерел."
    if reason == "timeline_disabled":
        return "Датовані спостереження ще не увімкнено."
    if reason and any(part in reason for part in ("changed", "stale", "after", "scope")):
        return "Збережену пам’ять не підтверджено для поточного стану розмови."
    if reason and any(part in reason for part in ("budget", "limit")):
        return "Повний огляд пам’яті зараз недоступний через обсяг історії."
    return "Пам’ять зараз недоступна: її джерела не підтверджено."


def memory_presentation(memory=None, *, boundary=None, updated_at=None,
                        hidden=False, erasing=False,
                        visible_source_ids=()):
    """Consume the exact caller-owned read once; this function performs no I/O.

    A dated event comes only from the canonical validated frozen v2 read. The
    optional legacy narrative is separately labelled and never given dates,
    topics, source links or structured-fact authority. Source links are limited
    to already owned visible chat nodes admitted by the caller.
    """
    if erasing or hidden:
        return _empty("unavailable", _reason("client_erasing" if erasing else "client_hidden"))
    memory = memory if isinstance(memory, Mapping) else {}
    proof = memory.get("provenance")
    proof = proof if isinstance(proof, Mapping) else {}
    if proof.get("version") == "captured-memory.timeline.v2":
        reason = validate_memory_timeline_read(dict(memory), boundary)
        if reason:
            return _empty("unavailable", _reason(reason))
        timeline = proof["timeline"]
        source_ids = {identity for identity in visible_source_ids if type(identity) is int and identity > 0}
        events = [{"event_at": row["event_at"], "time_basis": row["time_basis"],
            "topic_label": TOPIC_LABELS[row["topic_hint"]], "quote": row["quote"],
            "source_id": row["source_message_id"] if row["source_message_id"] in source_ids else None,
            "scope_label": "Розмова клієнта"} for row in timeline["events"]]
        source_coverage = timeline["coverage"]
        counts = {}
        for omission in source_coverage["omissions"]:
            label = OMISSION_LABELS[omission["reason"]]
            counts[label] = counts.get(label, 0) + 1
        result = _empty("timeline" if events else "empty")
        result.update(events=events, updated_at=_iso(proof.get("generated_at")),
            as_of=_iso(proof["read_boundary"]["watermark"]["event_at"]))
        result["coverage"] = {key: source_coverage[key] for key in (
            "source_count", "selected_count", "retained_count", "event_count", "omitted_count")}
        result["coverage"].update(pending_count=len(proof["read_delta"]),
            omissions=[{"label": label, "count": count} for label, count in counts.items()])
        if events:
            result["reason_label"] = "Датовані цитати клієнта; теми є підказками, а не підтвердженими фактами."
        else:
            result["reason_label"] = "У перевіреному огляді немає збережених цитат."
            if source_coverage["omitted_count"]:
                result["reason_label"] += " Причини пропусків наведено нижче."
            if proof["read_delta"]:
                result["reason_label"] += " Після цього огляду є нові повідомлення."
        return result
    # A failed v2 read has no provenance; never fall back to its raw machine head.
    reason = memory.get("reason") if isinstance(memory.get("reason"), str) else ""
    if reason.startswith("timeline_") or reason in {"client_erasing", "client_hidden"}:
        return _empty("unavailable", _reason(reason))
    text = memory.get("text") if reason == "current" and proof.get("version") == "captured-memory.v1" else ""
    if isinstance(text, str) and text.strip() and not _MACHINE.search(text):
        # Strip non-printing control characters but retain useful line breaks.
        text = "".join(char for char in text if char.isprintable() or char in "\n\t").strip()
        if text:
            result = _empty("legacy", "Попередній текстовий огляд. Дати й джерела окремих тверджень не підтверджено.")
            result.update(legacy_text=text[:LEGACY_CHARS], updated_at=_iso(updated_at))
            return result
    return _empty("unavailable" if reason and reason != "narrative_empty" else "empty", _reason(reason))
