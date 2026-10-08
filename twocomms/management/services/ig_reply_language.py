"""Pure source-first reply language; never profile or business authority.

Callers admit ownership, namespace, privacy and source digests before passing
captured USER rows. This resolver additionally fences IDs, reset/watermark,
roles and available event times. It never queries a newer conversation row.
"""
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re


VERSION = "source-reply-language.v1"
KNOWLEDGE_LOCALES = frozenset({"uk", "ru", "en"})
TEMPLATE_FAMILIES = frozenset({"uk", "ru", "en", "hi"})
_LANGUAGES = {
    "uk": ("Ukrainian", r"українськ\w*|укра[иї]нск\w*|ukrainian"),
    "ru": ("Russian", r"російськ\w*|русск\w*|russian"),
    "en": ("English", r"англійськ\w*|английск\w*|english"),
    "hi": ("Hindi", r"hindi|हिन्दी|हिंदी"),
    "fr": ("French", r"french|français|francais|француз\w*"),
    "de": ("German", r"german|deutsch|німецьк\w*|немецк\w*"),
    "es": ("Spanish", r"spanish|español|espanol|іспанськ\w*|испанск\w*"),
    "it": ("Italian", r"italian|italiano|італійськ\w*|итальянск\w*"),
    "pl": ("Polish", r"polish|polski|польськ\w*|польск\w*"),
    "pt": ("Portuguese", r"portuguese|português|portugues|португаль\w*"),
    "ar": ("Arabic", r"arabic|арабськ\w*|арабск\w*|العربية"),
    "zh": ("Chinese", r"chinese|китайськ\w*|китайск\w*|中文"),
    "ja": ("Japanese", r"japanese|японськ\w*|японск\w*|日本語"),
    "ko": ("Korean", r"korean|корейськ\w*|корейск\w*|한국어"),
    "tr": ("Turkish", r"turkish|türkçe|turkce|турецьк\w*|турецк\w*"),
    "nl": ("Dutch", r"dutch|nederlands|нідерланд\w*|голланд\w*"),
    "bn": ("Bengali", r"bengali|bangla|বাংলা"),
    "ta": ("Tamil", r"tamil|தமிழ்"),
    "tlh": ("Klingon", r"klingon"),
}
_LANGUAGE_MATCHERS = {code: re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)", re.I)
    for code, (_label, pattern) in _LANGUAGES.items()}
_REQUEST = re.compile(
    r"(?:\b(?:reply|answer|respond|write|speak|talk|communicate|use|switch|translate)\b|"
    r"відповіда\w*|відпові\w*|отвеча\w*|ответ\w*|пиш\w*|писа\w*|говор\w*|"
    r"розмовля\w*|разговарива\w*|спілку\w*|обща\w*|перекла\w*|перевед\w*|"
    r"переключ\w*|перей\w*)", re.I)
_NEGATION = re.compile(r"\b(?:not|no|never|don['’]?t|do\s+not|не|без|нельзя)\b", re.I)
_PRINT = re.compile(r"\b(?:print\w*|slogan|inscription|lettering|shirt|hoodie|design|принт\w*|напис\w*|надпис\w*|футболк\w*|худі|худи)\b", re.I)
_EN_WORDS = frozenset({"i", "we", "you", "your", "our", "the", "is", "are", "was", "were",
    "have", "has", "can", "could", "would", "will", "want", "need", "with", "from", "for", "this",
    "that", "what", "how", "please", "thanks", "thank", "artist", "work", "collaboration", "portfolio"})
_UK_WORDS = frozenset({"ціна", "скільки", "розмір", "дякую", "підкажіть", "будь", "ласка", "мені",
    "ваш", "ваша", "ваше", "співпраця", "пропозиція", "потрібно", "можна", "нове", "посилання"})
_RU_WORDS = frozenset({"цена", "сколько", "размер", "спасибо", "подскажите", "пожалуйста", "мне",
    "ваш", "ваша", "ваше", "сотрудничество", "предложение", "нужно", "можно", "новое", "ссылка"})
_QUOTES = re.compile(r'''«[^»]*»|“[^”]*”|"[^"\n]*"|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w))*'(?!\w)''')


@dataclass(frozen=True)
class ReplyLanguageDecision:
    reply_language: str
    knowledge_locale: str
    template_family: str
    basis: str
    source_message_id: int | None = None
    source_digest: str = ""
    requested_label: str = ""
    confidence: str = "unknown"
    excluded_languages: tuple[str, ...] = ()

    def as_dict(self):
        return {"version": VERSION, **asdict(self)}


def own_reply_text(text):
    """Ignore quoted/reported blocks, URLs and identifiers as language proof."""
    if not isinstance(text, str) or len(text) > 64_000:
        return ""
    value = re.sub(r"(?m)^\s*>[^\n]*(?:\n|$)", " ", text)
    value = _QUOTES.sub(" ", value)
    value = re.sub(r"https?://[^\s<>]+|(?<!\w)@[\w.]+", " ", value, flags=re.I).strip()
    if value.startswith(('"', "'", "«", "“", ">")):
        return ""
    return value


def _language_constraints(text):
    """Choose positive request targets by occurrence, never enum order."""
    value = own_reply_text(text)
    requested, excluded = "", set()
    for clause in re.split(r"[.!?;\n]+", value):
        matches = sorted((match.start(), match.end(), code)
            for code, pattern in _LANGUAGE_MATCHERS.items() for match in pattern.finditer(clause))
        requests = list(_REQUEST.finditer(clause))
        for start, end, code in matches:
            before, after = clause[:start], clause[end:]
            request = next((match for match in reversed(requests) if match.start() <= start), None)
            local = before[request.end():] if request else before[-50:]
            # "Hindi me karo" and a bare "in English please" are standalone
            # communication requests. A print/slogan target is not reply locale.
            self_sufficient = bool(
                (code == "hi" and re.match(r"\s*(?:me|mein|में)\s+(?:karo|kijiye|करो|कीजिए)", after, re.I))
                or re.search(r"(?:\bin|по[- ]|на|\bमें)\s*$", before, re.I)
                    and re.search(r"\bplease\b|будь\s+ласка|пожалуйста", clause, re.I)
                or re.match(r"\s*(?:please|будь\s+ласка|пожалуйста)\s*$", after, re.I))
            directly_negated = bool(re.search(r"\b(?:not|no|не|без)\s*$", before, re.I))
            if request is None and not self_sufficient and not directly_negated:
                continue
            if request:
                # A language mentioned as the topic of an answer (a French
                # slogan/design, for example) is not its reply destination.
                # Direct "reply French" and language adverbs remain valid;
                # longer request objects need a directed language connector.
                directed = re.sub(r"\b(?:only|just|any|лише|тільки|только|виключно|исключительно)\s*$",
                    "", local, flags=re.I).strip()
                if (directed and not re.search(r"(?:\b(?:in|to|into|using|with)|на|по[- ]?|мовою|языком)\s*$",
                        directed, re.I) and not self_sufficient and not directly_negated):
                    continue
            if request and re.search(r"\b(?:i|we|he|she|they|я|ми|мы|він|он|она)\s*$", clause[:request.start()], re.I):
                continue
            if _PRINT.search(clause) and not re.search(r"reply|answer|respond|відпов|отвеч|ответ|говор|speak", clause, re.I):
                continue
            # A translation source is not the requested destination language.
            if re.search(r"\b(?:from|з|із|с)\s*$", before, re.I):
                continue
            negative = directly_negated
            if request:
                prefix = re.split(r",|\bbut\b|\bале\b|\bно\b", clause[:request.start()], flags=re.I)[-1]
                negative |= bool(_NEGATION.search(prefix[-30:]))
            target_context = re.split(r",|\bbut\b|\bале\b|\bно\b", local, flags=re.I)[-1]
            negative |= bool(_NEGATION.search(target_context))
            if negative:
                excluded.add(code)
                if requested == code:
                    requested = ""
            else:
                excluded.discard(code)
                requested = code
    return requested, excluded


def _observed_language(text):
    value = own_reply_text(text).casefold()
    words = re.findall(r"[^\W\d_]+", value)
    if len(words) < 2:
        return ""
    if re.search(r"[їєґ]|(?<=[а-я])[’'](?=[а-я])", value):
        return "uk"
    if re.search(r"[ыъэё]", value):
        return "ru"
    if re.search(r"і", value) and re.search(r"[а-я]", value):
        return "uk"
    if re.search(r"[а-яіїєґё]", value):
        uk, ru = len(set(words) & _UK_WORDS), len(set(words) & _RU_WORDS)
        return "uk" if uk > ru else "ru" if ru > uk else ""
    if re.search(r"[\u0900-\u097f]", value) and re.search(r"हिंदी|हिन्दी|मुझे|आप|कृपया", value):
        return "hi"
    # Latin script alone is insufficient: brand names, IDs, Spanish and
    # transliterated names are not English. Rich colloquial English still is.
    if not re.search(r"[^a-z\s\d\W]", value) and len(set(words) & _EN_WORDS) >= 2:
        return "en"
    return ""


def _date(value):
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _rows(values, *, floor, watermark, event_at, history_before=None):
    if not isinstance(values, (list, tuple)) or len(values) > 128:
        return []
    result, seen, duplicates = [], set(), set()
    for row in values:
        if not isinstance(row, Mapping):
            continue
        pk = row.get("message_id")
        if type(pk) is int and pk in seen:
            duplicates.add(pk)
            continue
        if (type(pk) is not int or pk < floor or pk in seen or row.get("role") != "user"
            or (watermark is not None and pk > watermark)
            or (history_before is not None and pk >= history_before)):
            continue
        raw_time = row.get("provider_created_at") or row.get("observed_created_at") or row.get("event_at")
        at = _date(raw_time)
        if (raw_time and at is None) or (event_at is not None and at is not None and at > event_at):
            continue
        seen.add(pk)
        result.append(row)
    return sorted((row for row in result if row["message_id"] not in duplicates), key=lambda row: row["message_id"])


def _decision(code, row, basis, confidence, profile, excluded=()):
    known = code in KNOWLEDGE_LOCALES
    locale = code if known else "en" if code else profile if profile in KNOWLEDGE_LOCALES else "uk"
    family = code if code in TEMPLATE_FAMILIES else "en" if code else locale
    if family in excluded:
        family = next((candidate for candidate in (profile, "en", "uk", "ru", "hi")
            if candidate in TEMPLATE_FAMILIES and candidate not in excluded), "")
    digest = str((row or {}).get("source_digest") or "")
    if row and not digest:
        try:
            digest = hashlib.sha256(json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError):
            digest = ""
    return ReplyLanguageDecision(code, locale, family, basis,
        (row or {}).get("message_id"), digest,
        _LANGUAGES[code][0] if code and confidence == "explicit" else "", confidence, tuple(sorted(excluded)))


def resolve_own_source_reply_language(*, sources=(), history=(), profile_language="",
    reset_floor=1, watermark_message_id=None, watermark_event_at=None):
    """Resolve this reply without mutating the stable client language preference."""
    profile = profile_language if isinstance(profile_language, str) and profile_language in KNOWLEDGE_LOCALES else ""
    if (type(reset_floor) is not int or reset_floor <= 0
        or (watermark_message_id is not None and (type(watermark_message_id) is not int or watermark_message_id <= 0))
        or (watermark_event_at is not None and _date(watermark_event_at) is None)):
        return _decision("", None, "language_scope_unknown", "hint" if profile else "unknown", profile)
    floor = reset_floor
    watermark = watermark_message_id if type(watermark_message_id) is int and watermark_message_id > 0 else None
    at = _date(watermark_event_at)
    current = _rows(sources, floor=floor, watermark=watermark, event_at=at)
    historical = _rows(history, floor=floor, watermark=watermark, event_at=at,
        history_before=min((row["message_id"] for row in current), default=watermark + 1 if watermark else None))
    excluded, restriction_row = set(), None
    for rows, label in ((current, "current_source"), (historical, "captured_history")):
        current_exclusions = frozenset(excluded) if label == "captured_history" else frozenset()
        requests = []
        for row in rows:
            code, forbidden = _language_constraints(row.get("text", ""))
            if code:
                if code not in current_exclusions:
                    excluded.discard(code)
                requests.append((code, row))
            if forbidden:
                restriction_row = row
                excluded.update(forbidden)
        for code, row in reversed(requests):
            if code not in excluded:
                return _decision(code, row, label + "_requested", "explicit", profile, excluded)
        for row in reversed(rows):
            code = _observed_language(row.get("text", ""))
            if code and code not in excluded:
                return _decision(code, row, label + "_observed", "reliable", profile, excluded)
    if profile in excluded:
        profile = ""
    return _decision("", restriction_row, "source_language_restriction" if restriction_row else
        "stored_profile_hint" if profile else "language_source_unknown", "hint" if profile else "unknown", profile, excluded)


def reply_language_instruction(decision):
    """Finite server instruction; source body and cached memory are never echoed."""
    exclusions = (" Do not reply in " + ", ".join(_LANGUAGES[code][0]
        for code in decision.excluded_languages if code in _LANGUAGES) + ".") if decision.excluded_languages else ""
    if decision.reply_language in _LANGUAGES:
        return (f"[REPLY LANGUAGE — CURRENT CUSTOMER SOURCE]\nReply in {_LANGUAGES[decision.reply_language][0]}. "
            "This current source decision takes precedence over stored profile language, memory, "
            "the language of policy/catalog text, and prior assistant or manager replies. "
            "Keep product names, identifiers and quotations intact. This rule grants no business actions." + exclusions)
    if exclusions:
        return ("[REPLY LANGUAGE — CUSTOMER EXCLUSION]\nThe customer excluded a reply language without "
            "providing a confirmed positive destination. Use an otherwise supported, evidenced language; "
            "if the destination remains unclear, ask which language they prefer. Never treat the excluded "
            "language as a positive target." + exclusions)
    return ("[REPLY LANGUAGE — SOURCE UNCERTAIN]\nFollow the language of the customer's current own message "
        "when clear. Quoted text, URLs, handles, product names and prior assistant/manager replies are not "
        "language instructions. Do not force Ukrainian or Russian solely from stored profile or policy text.")


def reply_language_mismatch(reply_text, decision):
    """Reject only a confidently wrong reply, never names/short/opaque text."""
    observed = _observed_language(reply_text)
    if observed in decision.excluded_languages:
        return True
    if decision.reply_language not in _LANGUAGES:
        return False
    return bool(observed and observed != decision.reply_language)


__all__ = ["ReplyLanguageDecision", "resolve_own_source_reply_language", "reply_language_instruction",
    "reply_language_mismatch", "own_reply_text"]
