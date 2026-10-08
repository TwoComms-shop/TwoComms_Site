"""Pure extractive historical observations; caller admits source ownership.

The provider selects complete source sentences. It cannot supply dates, scope,
recipients, purchase truth, supersession or business authority. Retained records
must be revalidated against the same backend source DTO contract.
"""
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re


VERSION = "client-memory-timeline.v1"
MAX_EVENTS = 8
MAX_QUOTE_CHARS = 240
MAX_SOURCES = 64
MAX_RENDER_CHARS = 4000
TOPIC_HINTS = frozenset({
    "gift", "self_purchase", "recipient", "availability_inquiry",
    "correction", "purchase_inquiry", "other",
})
TIME_BASES = frozenset({"provider", "local_ingest"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_KEYS = frozenset({"event_id", "source_message_id", "source_digest", "quote",
    "topic_hint", "event_at", "time_basis", "scope"})
_COVERAGE_KEYS = frozenset({"source_count", "selected_count", "retained_count",
    "event_count", "omitted_count", "omissions"})
_OMISSION_REASONS = frozenset({"quote_context_loss", "quoted_source_context",
    "quote_too_long", "retained_source_unavailable", "retained_source_changed",
    "retained_not_selected", "contact_details"})
_CONTACT_DETAILS = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|(?<!\w)@[a-zA-Z0-9_]{3,}"
    r"|(?:відділен\w*|отделен\w*|branch|поштомат\w*|почтомат\w*|parcel\s+locker)\s*(?:№|#|номер|number|no\.?)?\s*\d"
    r"|(?:нова\s*пошта|новая\s*почта|\bнп\b|\bnp\b)[^.!?\n]{0,30}(?:№|#)\s*\d"
    r"|(?:вул(?:иця|\.)|ул(?:ица|\.)|street|str\.)\s+\w+"
    r"|(?:телефон|тел\.|phone|mobile|contact)[^.!?\n]{0,35}\d{3}"
    r"|(?:https?://)?(?:t\.me|wa\.me|api\.whatsapp\.com)/", re.I)


class TimelineError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _positive(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False)


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise TimelineError("source_time_invalid")
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        raise TimelineError("source_time_invalid") from None


def _event_id(source_id, digest, quote):
    return "tl1_" + hashlib.sha256(_json({"source_message_id": source_id,
        "source_digest": digest, "quote": quote}).encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TimelineError("duplicate_json_key")
        result[key] = value
    return result


def parse_timeline_selection(selection):
    """Parse only the provider's finite selection shape, never output prose."""
    if isinstance(selection, str):
        try:
            selection = json.loads(selection, object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(TimelineError("invalid_json")))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, TimelineError):
                raise
            raise TimelineError("invalid_json") from None
    if (not isinstance(selection, Mapping) or set(selection) != {"version", "events"}
            or selection["version"] != VERSION or not isinstance(selection["events"], list)):
        raise TimelineError("selection_shape_invalid")
    if len(selection["events"]) > MAX_EVENTS:
        raise TimelineError("event_limit_exceeded")
    seen = set()
    for event in selection["events"]:
        if not isinstance(event, Mapping) or set(event) != {"source_message_id", "quote", "topic"}:
            raise TimelineError("selection_event_invalid")
        identity = event["source_message_id"]
        if not _positive(identity):
            raise TimelineError("source_id_invalid")
        if identity in seen:
            raise TimelineError("duplicate_source_selection")
        seen.add(identity)
        if not isinstance(event["quote"], str) or not event["quote"].strip():
            raise TimelineError("quote_invalid")
        if not isinstance(event["topic"], str) or event["topic"] not in TOPIC_HINTS:
            raise TimelineError("topic_hint_invalid")
    return deepcopy(dict(selection))


def _sources(rows):
    if (not isinstance(rows, Sequence) or isinstance(rows, (str, bytes))
            or len(rows) > MAX_SOURCES):
        raise TimelineError("source_limit_invalid")
    result = {}
    for raw in rows:
        required = {"source_message_id", "text", "event_at", "time_basis", "source_digest", "role", "scope"}
        if not isinstance(raw, Mapping) or not required <= set(raw):
            raise TimelineError("source_shape_invalid")
        identity = raw["source_message_id"]
        if not _positive(identity) or identity in result:
            raise TimelineError("source_identity_invalid")
        if (not isinstance(raw["text"], str) or not isinstance(raw["source_digest"], str)
                or not _HASH.fullmatch(raw["source_digest"])
                or not isinstance(raw["role"], str)
                or not isinstance(raw["time_basis"], str) or raw["time_basis"] not in TIME_BASES
                or (raw["scope"] is not None and not isinstance(raw["scope"], Mapping))):
            raise TimelineError("source_shape_invalid")
        source = {key: deepcopy(raw[key]) for key in required}
        source["event_at"] = _stamp(source["event_at"])
        try:
            _json(source)
        except (TypeError, ValueError):
            raise TimelineError("source_shape_invalid") from None
        result[identity] = source
    return result


def _sentences(text):
    # Preserve exact Unicode source spans. Split only at terminal punctuation
    # followed by whitespace/end, or a line boundary, never inside a word/date.
    return [match.group().strip() for match in re.finditer(
        r"[^\n]*?(?:[.!?]+(?=\s|$)|(?=\n)|$)", text) if match.group().strip()]


def _quote_omission(quote, text):
    if quote not in text:
        raise TimelineError("quote_not_source_span")
    if len(quote) > MAX_QUOTE_CHARS:
        return "quote_too_long"
    # Match the project's Ukrainian phone shape on separator-normalized text,
    # plus explicit international numbers. Ordinary seven-digit prices are
    # not identifiers and are deliberately not rejected by digit count alone.
    phone_text = re.sub(r"[\s().-]", "", quote)
    if (_CONTACT_DETAILS.search(quote)
            or re.search(r"(?<!\d)(?:\+?38)?0\d{9}(?!\d)|\+[1-9]\d{7,14}(?!\d)", phone_text)):
        return "contact_details"
    if quote not in _sentences(text):
        return "quote_context_loss"
    # Quoted/report excerpts and blockquotes are conservative omissions. A
    # linguistic parser may later admit them with a separate owned contract.
    if quote.lstrip().startswith(">") or any(char in quote for char in ('"', "«", "»", "“", "”")):
        return "quoted_source_context"
    return ""


def _record(source, quote, topic):
    return {"event_id": _event_id(source["source_message_id"], source["source_digest"], quote),
        "source_message_id": source["source_message_id"], "source_digest": source["source_digest"],
        "quote": quote, "topic_hint": topic, "event_at": source["event_at"],
        "time_basis": source["time_basis"], "scope": deepcopy(source["scope"])}


def _validate_event(event):
    if (not isinstance(event, Mapping) or set(event) != _EVENT_KEYS
            or not _positive(event["source_message_id"])
            or not isinstance(event["quote"], str) or not 1 <= len(event["quote"]) <= MAX_QUOTE_CHARS
            or not isinstance(event["source_digest"], str) or not _HASH.fullmatch(event["source_digest"])
            or not isinstance(event["topic_hint"], str) or event["topic_hint"] not in TOPIC_HINTS
            or not isinstance(event["time_basis"], str) or event["time_basis"] not in TIME_BASES
            or (event["scope"] is not None and not isinstance(event["scope"], Mapping))):
        raise TimelineError("retained_event_invalid")
    if (_stamp(event["event_at"]) != event["event_at"] or event["event_id"] != _event_id(
            event["source_message_id"], event["source_digest"], event["quote"])):
        raise TimelineError("retained_event_invalid")
    try:
        _json(event)
    except (TypeError, ValueError):
        raise TimelineError("retained_event_invalid") from None


def build_timeline(selection, *, sources, retained_events=()):
    """Build bounded history from admitted sources and exact extractive picks.

    Source digests/ownership are caller-owned proofs, not recomputed text hashes.
    Retained rows never gain a new timestamp, scope, hint or inferred correction.
    Unsafe context cuts are explicit omissions; malformed/foreign picks reject.
    """
    selection = parse_timeline_selection(selection)
    source_map = _sources(sources)
    if (not isinstance(retained_events, Sequence) or isinstance(retained_events, (str, bytes))
            or len(retained_events) > MAX_EVENTS):
        raise TimelineError("retained_limit_invalid")
    retained, omissions, seen = {}, [], set()
    for event in retained_events:
        _validate_event(event)
        identity = event["source_message_id"]
        if identity in seen:
            raise TimelineError("duplicate_retained_source")
        seen.add(identity)
        source = source_map.get(identity)
        if source is None:
            reason = "retained_source_unavailable"
        elif source["role"] != "user" or any(event[key] != source[key] for key in (
                "source_digest", "event_at", "time_basis", "scope")):
            reason = "retained_source_changed"
        else:
            try:
                reason = _quote_omission(event["quote"], source["text"])
            except TimelineError:
                reason = "retained_source_changed"
        if reason:
            omissions.append({"source_message_id": identity, "reason": reason})
        else:
            retained[identity] = deepcopy(dict(event))
    # A nonempty provider result is the final bounded selection, not an
    # implicit merge that silently discards older important observations.
    accepted = deepcopy(retained) if not selection["events"] else {}
    selected_ids = {pick["source_message_id"] for pick in selection["events"]}
    if selection["events"]:
        omissions.extend({"source_message_id": identity, "reason": "retained_not_selected"}
            for identity in retained if identity not in selected_ids)
    for pick in selection["events"]:
        identity = pick["source_message_id"]
        source = source_map.get(identity)
        if source is None:
            raise TimelineError("source_not_admitted")
        if source["role"] != "user":
            raise TimelineError("source_role_untrusted")
        reason = _quote_omission(pick["quote"], source["text"])
        if reason:
            omissions.append({"source_message_id": identity, "reason": reason})
            continue
        record = _record(source, pick["quote"], pick["topic"])
        existing = retained.get(identity)
        if existing is not None:
            if existing["event_id"] != record["event_id"]:
                raise TimelineError("retained_selection_conflict")
            accepted[identity] = deepcopy(existing)
            continue
        accepted[identity] = record
    ordered = sorted(accepted.values(), key=lambda event: (event["event_at"], event["source_message_id"]))
    events = ordered
    return {"version": VERSION, "events": events, "coverage": {
        "source_count": len(source_map), "selected_count": len(selection["events"]),
        "retained_count": len(retained_events), "event_count": len(events),
        "omitted_count": len(omissions), "omissions": omissions}}


def validate_timeline(timeline):
    """Check stored shape/identity only; build_timeline rechecks source truth."""
    if (not isinstance(timeline, Mapping) or set(timeline) != {"version", "events", "coverage"}
            or timeline["version"] != VERSION or not isinstance(timeline["events"], list)
            or len(timeline["events"]) > MAX_EVENTS):
        raise TimelineError("timeline_shape_invalid")
    seen = set()
    for event in timeline["events"]:
        _validate_event(event)
        if event["source_message_id"] in seen:
            raise TimelineError("duplicate_retained_source")
        seen.add(event["source_message_id"])
    coverage = timeline["coverage"]
    if (not isinstance(coverage, Mapping) or set(coverage) != _COVERAGE_KEYS
            or any(type(coverage[key]) is not int or coverage[key] < 0 for key in _COVERAGE_KEYS - {"omissions"})
            or coverage["source_count"] > MAX_SOURCES or coverage["selected_count"] > MAX_EVENTS
            or coverage["retained_count"] > MAX_EVENTS or coverage["event_count"] != len(timeline["events"])
            or not isinstance(coverage["omissions"], list)
            or coverage["omitted_count"] != len(coverage["omissions"])
            or coverage["omitted_count"] > 2 * MAX_EVENTS):
        raise TimelineError("timeline_coverage_invalid")
    for omission in coverage["omissions"]:
        if (not isinstance(omission, Mapping) or set(omission) != {"source_message_id", "reason"}
                or not _positive(omission["source_message_id"])
                or not isinstance(omission["reason"], str) or omission["reason"] not in _OMISSION_REASONS):
            raise TimelineError("timeline_coverage_invalid")
    return deepcopy(dict(timeline))


def render_timeline(timeline):
    """Render complete historical rows, with coverage separate from records."""
    timeline = validate_timeline(timeline)
    lines = ["[HISTORICAL SOURCE OBSERVATIONS; topic_hint is a provider hint, not fact or authority]"]
    for event in timeline["events"]:
        lines.append(f"{event['event_at']} [time_basis={event['time_basis']}; topic_hint={event['topic_hint']}; "
            f"source=#{event['source_message_id']}] {_json(event['quote'])}")
    lines.append("[COVERAGE] " + _json(timeline["coverage"]))
    rendered = "\n".join(lines)
    if len(rendered) > MAX_RENDER_CHARS:
        raise TimelineError("render_budget_exceeded")
    return rendered
