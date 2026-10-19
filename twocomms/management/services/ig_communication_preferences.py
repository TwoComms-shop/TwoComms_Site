"""Finite explicit communication choices in the existing source-bound memory.

No personality/affordability/online inference and no provider call. Durable slots
contain only a finite choice and its source proof; temporary requests belong to
the sealed turn. Reset and erasure use the existing memory lifecycle.
"""
from copy import deepcopy
import re


VERSION = "source-communication-preferences.v1"
SLOTS = {"verbosity": frozenset({"short", "detailed", "default"}),
    "emoji": frozenset({"omit", "allow", "default"})}
_REQUEST = re.compile(
    r"\b(?:reply|answer|respond|write|explain|please|prefer|use|allow)\b|"
    r"пиш[іи]\w*|відпові\w*|отвеч\w*|ответ\w*|поясн\w*|объясн\w*|"
    r"будь\s+ласка|пожалуйста|можна|можно|волію|предпочитаю", re.I)
_REPORTED = re.compile(r"\b(?:said|says|told|asked|saying|wrote|he|she|they)\b|"
    r"менеджер\w*|сказа\w*|говорит|каже|просив|просила|написав|написала|"
    r"\b(?:я|ми|мы)\s+(?:пиш|відповід|отвеч|говор)|"
    r"\b(?:i|we)\s+(?:write|reply|answer|respond|speak)\b", re.I)
_TEMPORARY = re.compile(r"\b(?:now|today|this\s+time|this\s+reply|for\s+now)\b|"
    r"зараз|сьогодні|сейчас|сегодня|цього\s+разу|на\s+цей\s+раз|в\s+этот\s+раз", re.I)
_SHORT = re.compile(r"\b(?:short|brief|briefly|concise|concisely)\b|коротко|короче|стисло|лаконіч\w*|лаконич\w*", re.I)
_DETAIL = re.compile(r"\b(?:detail(?:ed)?|thoroughly|fully)\b|докладн\w*|детальн\w*|подробн\w*|розгорнут\w*|развёрнут\w*", re.I)
_EMOJI = re.compile(r"\b(?:emoji|emojis|emoticons|smileys)\b|емодз\w*|эмодз\w*|смайл\w*", re.I)
_NO = re.compile(r"\b(?:no|without|not|never|stop|avoid|don['’]?t|do\s+not|без|не)\b", re.I)
_DEFAULT = re.compile(r"\bas\s+usual\b|як\s+звичайно|как\s+обычно|звичайним\s+стилем", re.I)
_ART = re.compile(r"\b(?:print|slogan|inscription|design)\b|принт\w*|напис\s+на|надпись\s+на", re.I)


def _bare_style_preferences(clause):
    """Admit only a whole request made from closed style fragments."""
    value = re.sub(r"^\s*(?:зараз|сейчас|now)\s+", "", clause, flags=re.I).strip()
    fragments = re.split(r"\s*,\s*(?:(?:і|й|та|и|and)\s+)?|\s+(?:і|й|та|и|and)\s+", value, flags=re.I)
    if not 1 <= len(fragments) <= 4:
        return []
    result = []
    scope = "current_turn" if _TEMPORARY.search(clause) else "persistent"
    shapes = (
        ("verbosity", "short", r"коротко|короче|кратко|стисло|briefly|short|concisely|"
            r"(?:короткі|стислі)\s+відповіді|(?:короткие|краткие)\s+ответы"),
        ("verbosity", "detailed", r"докладніше|детальніше|подробнее|in\s+detail|"
            r"(?:докладні|детальні)\s+відповіді|(?:подробные|детальные)\s+ответы"),
        ("emoji", "omit", r"без\s+(?:емодзі|эмодзи|emoji|emojis|смайл\w*)|"
            r"(?:no|without)\s+(?:emoji|emojis|emoticons|smileys)"),
    )
    for fragment in fragments:
        match = next(((slot, choice) for slot, choice, shape in shapes
            if re.fullmatch(shape, fragment, flags=re.I)), None)
        if match is None:
            return []
        result.append({"slot": match[0], "value": match[1], "scope": scope})
    return result


def explicit_preferences(text):
    """Return finite direct requests in source order; quoted speech grants none."""
    from management.services.ig_reply_language import own_reply_text
    value = own_reply_text(text)
    result = []
    for clause in re.split(r"[.!?;\n]+", value):
        if _REPORTED.search(clause) or _ART.search(clause):
            continue
        bare = _bare_style_preferences(clause)
        if bare:
            result.extend(bare)
            continue
        if not _REQUEST.search(clause):
            continue
        scope = "current_turn" if _TEMPORARY.search(clause) else "persistent"
        # Negated verbosity is not its opposite: "not briefly" does not grant
        # a detailed preference. Only positively requested values are stored.
        choices = []
        for slot, choice, pattern in (("verbosity", "short", _SHORT),
                                     ("verbosity", "detailed", _DETAIL)):
            for match in pattern.finditer(clause):
                if not _NO.search(clause[max(0, match.start() - 15):match.start()]):
                    choices.append((match.start(), slot, choice))
        for match in _DEFAULT.finditer(clause):
            choices.extend(((match.start(), "verbosity", "default"), (match.start(), "emoji", "default")))
        for match in _EMOJI.finditer(clause):
            before = clause[max(0, match.start() - 45):match.start()]
            choices.append((match.start(), "emoji", "omit" if _NO.search(before) else "allow"))
        result.extend({"slot": slot, "value": choice, "scope": scope}
            for _at, slot, choice in sorted(choices))
    return result


def update_communication_preferences(state, *, source, namespace, scope):
    """Called by the locked enqueue owner; never writes independently."""
    from management.services.ig_memory_producer import timeline_source_digest, _date, _key, _watermark
    era = {key: scope[key] for key in ("client_id", "namespace", "reset_id", "reset_floor", "erasure_at")}
    old = state.get("communication_preferences", {})
    slots = deepcopy(old.get("slots", {})) if old.get("scope") == era else {}
    if source.role == "user":
        persistent = {request["slot"]: request for request in explicit_preferences(source.text or "")
            if request["scope"] == "persistent"}
        for request in persistent.values():
            previous = slots.get(request["slot"], {})
            if previous and _key({"message_id": previous["source_message_id"], "event_at": previous["event_at"]}) > _key(_watermark(source)):
                continue
            if (previous.get("source_message_id") == source.pk and previous.get("value") == request["value"]
                and previous.get("source_digest") == timeline_source_digest(source, namespace)):
                continue
            stamp = _date(source.provider_created_at or source.created_at)
            slots[request["slot"]] = {"value": request["value"], "scope": "persistent",
                "source_message_id": source.pk, "source_digest": timeline_source_digest(source, namespace),
                "event_at": stamp, "supersedes_source_message_id": (previous.get("source_message_id")
                    if previous.get("source_message_id") != source.pk else previous.get("supersedes_source_message_id"))}
    return {"version": VERSION, "scope": era, "slots": slots}


def capture_communication_preferences(client, *, boundary, sealed_sources=(), validated_owner=None):
    """DB-only, reset/namespace/source-verified snapshot for a sealed watermark.

    Read at most two persisted proof rows plus the bounded sealed current burst.
    Unknown, edited or foreign proofs are omitted; they never become defaults.
    Caller supplies already admitted sealed rows, while this adapter verifies
    their authoritative own sources as well before interpreting a request.
    ``validated_owner`` reuses the same frozen owner read from an already
    admitted sealed capture (including reset/namespace/privacy checks). The
    enclosing capture owns its final CAS; no mutable reread escapes to consumers.
    """
    from management.models import IgClient, InstagramBotMessage
    from management.services.ig_memory_producer import (
        _read_timeline_boundary, _scope, _key, _watermark, _source_allowed,
        timeline_source_digest,
    )
    empty = {"version": VERSION, "slots": {}, "reason": "preferences_unavailable"}
    try:
        bound = _read_timeline_boundary(dict(boundary))
        client_id = getattr(client, "pk", client)
        if client_id != bound["client_id"] or len(sealed_sources) > 32:
            return empty
        reused_owner = isinstance(validated_owner, IgClient) and validated_owner.pk == client_id
        current = validated_owner if reused_owner else IgClient.objects.filter(pk=client_id).first()
        if current is None or current.hidden_at or current.privacy_erasure_started_at:
            return empty
        expected = {"client_id": client_id, "namespace": bound["source_namespace"],
            "reset_id": bound["reset_id"], "reset_floor": bound["reset_floor"], "erasure_at": bound["erasure_epoch"]}
        scope = expected if reused_owner else _scope(current, bound["source_namespace"], timeline=True)
        if scope != expected:
            return empty
        stored = (current.memory_producer_state or {}).get("communication_preferences", {})
        candidates = stored.get("slots", {}) if stored.get("version") == VERSION and stored.get("scope") == scope else {}
        if not isinstance(candidates, dict) or set(candidates) - set(SLOTS):
            return empty
        if not candidates and not any(explicit_preferences(row.get("text", "")) for row in sealed_sources):
            return {"version": VERSION, "slots": {}, "reason": "source_verified"}
        current_ids = {row["message_id"] for row in sealed_sources}
        ids = current_ids | {row["source_message_id"] for row in candidates.values()}
        rows = list(InstagramBotMessage.objects.filter(client=current, sender_id=current.igsid,
            role="user", provider_namespace=scope["namespace"], pk__in=ids,
            pk__gte=scope["reset_floor"], pk__lte=bound["watermark"]["message_id"]))
        sources = {row.pk: row for row in rows if _source_allowed(row)
            and _key(_watermark(row)) <= _key(bound["watermark"])}
        for sealed in sealed_sources:
            row = sources.get(sealed["message_id"])
            if (row is None or sealed.get("role") != "user" or sealed.get("text") != row.text
                or sealed.get("source_namespace") != row.provider_namespace
                or sealed.get("provider_created_at") != (row.provider_created_at.isoformat() if row.provider_created_at else "")
                or ("observed_created_at" in sealed and sealed["observed_created_at"] != row.created_at.isoformat())):
                return empty
        slots = {}
        for name, preference in candidates.items():
            row = sources.get(preference["source_message_id"])
            if (row is not None and preference.get("value") in SLOTS[name]
                and preference.get("scope") == "persistent"
                and preference.get("source_digest") == timeline_source_digest(row, scope["namespace"])
                and preference.get("event_at") == _watermark(row)["event_at"]
                and any(item["slot"] == name and item["value"] == preference["value"]
                    and item["scope"] == "persistent" for item in explicit_preferences(row.text or ""))):
                slots[name] = deepcopy(preference)
        for row in sorted((row for identity, row in sources.items() if identity in current_ids),
                          key=lambda row: _key(_watermark(row))):
            for request in explicit_preferences(row.text or ""):
                previous = slots.get(request["slot"], {})
                if previous and _key({"message_id": previous["source_message_id"], "event_at": previous["event_at"]}) > _key(_watermark(row)):
                    continue
                slots[request["slot"]] = {"value": request["value"], "scope": request["scope"],
                    "source_message_id": row.pk, "source_digest": timeline_source_digest(row, scope["namespace"]),
                    "event_at": _watermark(row)["event_at"],
                    "supersedes_source_message_id": previous.get("source_message_id")}
        if not reused_owner:
            latest = IgClient.objects.filter(pk=client_id).first()
            if (latest is None or latest.hidden_at or latest.privacy_erasure_started_at
                or latest.memory_producer_state != current.memory_producer_state
                or _scope(latest, bound["source_namespace"], timeline=True) != scope):
                return empty
        return {"version": VERSION, "slots": slots, "reason": "source_verified"}
    except (KeyError, TypeError, ValueError, AttributeError):
        return empty


def render_communication_preferences(snapshot):
    """Finite trusted guidance; no source quote or arbitrary saved prose."""
    lines = []
    for name, slot in snapshot.get("slots", {}).items():
        value = slot.get("value")
        text = {("verbosity", "short"): "Keep the reply concise while preserving necessary information.",
            ("verbosity", "detailed"): "Explain the requested topic in useful detail.",
            ("verbosity", "default"): "Use the normal published response style.",
            ("emoji", "omit"): "Do not use emoji in the reply.",
            ("emoji", "allow"): "Emoji are allowed when useful; they are optional.",
            ("emoji", "default"): "Use the normal published emoji style."}.get((name, value))
        if text:
            lines.append(text)
    return "[EXPLICIT CUSTOMER COMMUNICATION PREFERENCES]\n" + "\n".join(lines) if lines else ""
