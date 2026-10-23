"""Pure customer requirement hints; the source envelope owns their authority.

Dates describe requested calendar days, never arrival guarantees or shipment
cutoffs. Relative days use the actual provider clock under the explicit Kyiv
policy. Neither ingestion time nor narrative/model output supplies that clock.
"""
from datetime import date, datetime, timedelta, time
import hashlib
import json
import re
from zoneinfo import ZoneInfo


VERSION = "fulfillment-requirements.v1"
KYIV = ZoneInfo("Europe/Kyiv")
MAX_QUOTE_CHARS = 500
MAX_INPUT_CHARS = 8000
_GIFT = re.compile(r"\b(?:подар\w*|gift|present)\b", re.I)
_URGENT = re.compile(r"\b(?:сроч\w*|терміново|термінов\w*|urgent\w*|asap)\b", re.I)
_NEGATIVE = re.compile(r"(?:\b(?:не|ні|not|no|never|don't|don’t|не\s+потріб\w*|не\s+нуж\w*)\s+)(?:\w+\s+){0,2}$", re.I)
_ARRIVAL = re.compile(r"\b(?:успе\w*|встиг\w*|достав\w*|отрима\w*|получ\w*|arriv\w*|receiv\w*|delivery|make\s+it)\b", re.I)
_DISPATCH = re.compile(r"\b(?:відправ\w*|отправ\w*|виготов\w*|изготов\w*|вироб(?:ити|ите|ля|лення|ництв)\w*|production|dispatch\w*|ship(?:ping|ped)?|prepar\w*)\b", re.I)
_GENERAL_SHIPPING = re.compile(r"\b(?:(?:умов\w*|услов\w*|правил\w*)\s+(?:достав\w*|відправ\w*|отправ\w*)|(?:shipping|delivery|dispatch)\s+(?:terms|conditions|policy)|(?:terms|conditions)\s+(?:of\s+)?(?:shipping|delivery)|how\s+(?:does\s+)?(?:shipping|delivery)\s+work\w*)\b", re.I)
_DATE_CUE = re.compile(r"\b(?:до|к|на|by|before|on|deadline|дедлайн|дат\w*|числа|потріб\w*|нуж\w*)\b", re.I)
_WITHDRAW = re.compile(r"(?:дата|дату|дедлайн|deadline|термін|срок)[^.!?;\n]{0,35}(?:неваж\w*|не\s+важ\w*|скасов\w*|отмен\w*|no\s+longer|doesn['’]t\s+matter|not\s+important|withdrawn|cancelled|removed)|(?:скасу\w*|отмен\w*|withdraw|cancel)[^.!?;\n]{0,25}(?:дедлайн|deadline|дат\w*|термін|срок)|\bне\s+(?:потрібно|нужно)\s+(?:до|к)\s+\d", re.I)
_CHANGED = re.compile(r"\b(?:тепер|теперь|instead|changed|change\s+the\s+date|перен\w*|змін\w*|измен\w*)\b", re.I)
_TOGETHER = re.compile(r"\b(?:все\s+разом|усе\s+разом|всё\s+вместе|все\s+вместе|однією\s+посилкою|одной\s+посылкой|all\s+together|one\s+(?:parcel|shipment))\b", re.I)
_SPLIT = re.compile(r"\b(?:окрем\w*|отдель\w*|розділ\w*|раздел\w*|split|separately|separate\s+(?:parcels?|shipments?))\b", re.I)
_CUSTOM = re.compile(r"\b(?:кастом\w*|custom\s+print|свій\s+принт|свой\s+принт|my\s+own\s+print|індивідуальн\w*\s+принт)\b", re.I)
_RELATIVE = re.compile(r"\b(?:післязавтра|послезавтра|day\s+after\s+tomorrow|завтра|tomorrow)\b", re.I)
_DEPARTURE = re.compile(r"\b(?:I\s+(?:have\s+to\s+)?leave|I['’]m\s+leaving|we\s+(?:have\s+to\s+)?leave|depart\w*|виїжджа\w*|від['’]їжджа\w*|уезжа\w*)\b", re.I)
_CLOCK = re.compile(r"\b(?:at|before|by|о|об|до|в)\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?!\w)", re.I)
_NUMERIC = re.compile(r"(?<![\w\d])(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./-]\d{1,2}(?:[./-]\d{4})?)(?![\w\d])")
_MONTHS = {}
for _number, _names in enumerate((
    "січня январь января january", "лютого февраль февраля february", "березня март марта march",
    "квітня апрель апреля april", "травня май мая may", "червня июнь июня june",
    "липня июль июля july", "серпня август августа august", "вересня сентябрь сентября september",
    "жовтня октябрь октября october", "листопада ноябрь ноября november", "грудня декабрь декабря december",
), 1):
    _MONTHS.update({name: _number for name in _names.split()})
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_LOCAL_DATE = re.compile(rf"\b(?:(\d{{1,2}})\s+({_MONTH_PATTERN})(?:\s*,?\s*(\d{{4}}))?|({_MONTH_PATTERN})\s+(\d{{1,2}})(?:\s*,?\s*(\d{{4}}))?)\b", re.I)
_BARE_DAY = re.compile(r"\b(?:до|к|на|by|before|on)\s+(\d{1,2})(?:\s*(?:числа|число|th|st|nd|rd))?\b|\b\d{1,2}\s*числа\b", re.I)
_RECIPIENTS = {
    "себе": "self", "себя": "self", "myself": "self", "my self": "self",
    "друга": "friend", "другу": "friend", "friend": "friend",
    "подруги": "friend_female", "подругу": "friend_female",
    "мами": "mother", "мамы": "mother", "mother": "mother", "mom": "mother",
    "тата": "father", "папы": "father", "father": "father", "dad": "father",
    "boyfriend": "boyfriend", "хлопця": "boyfriend", "парня": "boyfriend",
}


def requirement_digest(requirement):
    """Stable identity of a detached requirement, including its source when known."""
    payload = {key: value for key, value in requirement.items() if key != "requirement_digest"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def active_hard_requirements(requirements):
    """Keep the latest explicit cutoff; withdrawals do not leave an old hold."""
    active = []
    for hint in requirements:
        if not isinstance(hint, dict):
            continue
        supersession = deadline_supersession(hint)
        if supersession == "withdrawn" or supersession == "changed" and hint.get("deadline_hard") is True:
            active = []
        if hint.get("deadline_hard") is True and hint.get("supersession_hint") != "withdrawn":
            active = [hint]
    return active


def deadline_supersession(hint):
    """Validate old sealed hints without rewriting their immutable contents.

    Earlier producers classified any 'change/now' as a deadline change. A
    size or recipient edit cannot withdraw a previously stated departure.
    """
    if not isinstance(hint, dict):
        return ""
    kind = hint.get("supersession_hint")
    value, _ = _customer_text(hint.get("raw_quote") or "")
    if kind == "withdrawn" and _affirmative_deadline_withdrawal(hint.get("raw_quote") or ""):
        return kind
    if kind == "changed" and _CHANGED.search(value) and (
            hint.get("deadline_hard") is True or hint.get("deadline_raw")
            or re.search(r"\b(?:deadline|дедлайн|дат[ауиы]|термін\w*|срок\w*)\b", value, re.I)):
        return kind
    return ""


def _affirmative_deadline_withdrawal(raw):
    value, hypothetical = _customer_text(raw)
    if hypothetical or re.search(r'[?？"«»“”‘]|(?<!\w)[\x27’]|[\x27’](?!\w)', raw):
        return False
    if re.search(r"\b(?:said|says|told|wrote|asked|according|maybe|might|may|perhaps|possibly|could|"
            r"можливо|возможно|мабуть|напевно|наверно\w*|ймовір\w*|вероят\w*|"
            r"сказ\w*|каже|казав\w*|говор\w*|напис\w*|попрос\w*)\b", raw, re.I):
        return False
    if re.search(r"\b(?:refus\w*|declin\w*|reject\w*|against|відмов\w*|відхил\w*|отказ\w*|отклон\w*|проти|против)\b", raw, re.I):
        return False
    if re.search(r"\b(?:not|no|never|cannot|не|ні|нет|"
            r"(?:can|don|doesn|didn|couldn|wouldn|shouldn|won|wasn|weren|isn|aren|haven|hasn|hadn|mustn|needn)[\W_]*t)\b[\W_]*"
            r"(?:\w+[\W_]+){0,3}(?:cancel\w*|withdraw\w*|remove\w*|скасу\w*|скасов\w*|отмен\w*)\b"
            r"|\b(?:нескасу\w*|нескасов\w*|неотмен\w*)\b", raw, re.I):
        return False
    return bool(_WITHDRAW.search(value))


def global_deadline_withdrawal(hint):
    """An explicit unqualified all-deadlines instruction resolves unknown scope."""
    raw = (hint or {}).get("raw_quote") or ""
    value, hypothetical = _customer_text(raw)
    return bool(deadline_supersession(hint) == "withdrawn" and not hypothetical
        and not re.search(r'["«»“”‘]|(?<!\w)[\x27’]|[\x27’](?!\w)', raw)
        and not re.search(r"[?？]|\b(?:not|never|don[\W_]*t|не|ні)\W+(?:\w+\W+){0,2}"
            r"(?:cancel|withdraw|remove|скасу\w*|отмен\w*)\b", value, re.I)
        and re.search(r"\b(?:all|every|всіх|усіх|всі|усі|всех|все)\b", value, re.I)
        and not re.search(r"\b(?:for|для|щодо|каса\w*|стосовно|item|line|позици\w*|позиці\w*)\b", value, re.I))


def _departure_requirement(result, value):
    departure = any(_affirmed(value, match) for match in _DEPARTURE.finditer(value))
    if not departure:
        return result
    result.update(deadline_kind="departure", deadline_hard=True)
    clocks = list(_CLOCK.finditer(value))
    # An hour without AM/PM or a 24-hour minute is ambiguous. Do not guess it.
    parsed = []
    for match in clocks:
        hour, minute, meridiem = int(match[1]), int(match[2] or 0), (match[3] or "").lower().replace(".", "")
        if minute > 59 or (meridiem and not 1 <= hour <= 12) or (not meridiem and (match[2] is None or hour > 23)):
            continue
        if meridiem:
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
        parsed.append((time(hour, minute), match.group()))
    if len(parsed) == 1 and result.get("deadline_date"):
        clock, raw = parsed[0]
        result.update(deadline_at=datetime.combine(date.fromisoformat(result["deadline_date"]), clock, KYIV).isoformat(),
            deadline_time_raw=raw, deadline_timezone_basis="explicit_kyiv_policy", timezone=KYIV.key)
    else:
        result["reasons"].append("departure_time_needs_clarification")
    return result


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _affirmed(text, match):
    return not _NEGATIVE.search(text[max(0, match.start() - 55):match.start()])


def _customer_text(text):
    # Preserve offsets without accidentally treating word apostrophes as quotes.
    text = re.sub(r'"[^"\n]*"|«[^»]*»|“[^”]*”|‘[^’]*’|(?<!\w)\'[^\'\n]*\'(?!\w)',
        lambda match: " " * len(match.group()), text)
    clauses = re.split(r"(?<=[!?;\n])|(?<=\.)(?!\d)|\b(?:але|но|but)\b", text, flags=re.I)
    accepted = []
    excluded = False
    for clause in clauses:
        polite_request = bool("?" in clause and re.search(r"\bwould\s+(?:you|it|this|that|we)\b", clause, re.I))
        polite_intent = bool(re.search(r"\bI\s+would\s+like\b", clause, re.I))
        if (re.search(r"\b(?:якщо|если|if|hypothetical|might|цитат\w*)\b|(?:він|он|she|he)\s+(?:сказав|сказал|said|wrote)", clause, re.I)
                or re.search(r"\bwould\b", clause, re.I) and not (polite_request or polite_intent)):
            excluded = True
        elif re.search(r"\b(?:не\s+(?:потріб\w*|нуж\w*|достав\w*|відправ\w*|отправ\w*)|(?:don't|don’t|do\s+not)\s+(?:need|want|require))\b", clause, re.I):
            # A withdrawn/negated request does not affirm the date later in it.
            # Keep explicit non-urgency in a separate clause if present.
            accepted.extend(match.group() for match in re.finditer(r"\b(?:не|not)\s+(?:сроч\w*|термінов\w*|urgent\w*)\b", clause, re.I))
            if _WITHDRAW.search(clause):
                accepted.append(clause)
        else:
            accepted.append(clause)
    return " ".join(accepted), excluded


def _date_value(raw):
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
            year, month, day = map(int, raw.split("-"))
        elif re.fullmatch(r"\d{1,2}[.-]\d{1,2}[.-]\d{4}", raw):
            day, month, year = map(int, re.split(r"[.-]", raw))
        else:
            return None
        return date(year, month, day)
    except ValueError:
        return None


def extract_requirements(text, *, provider_timestamp=None, now=None):
    """Return finite detached source hints, with no database/provider I/O."""
    raw = text if isinstance(text, str) else ""
    result = {"version": VERSION, "raw_quote": raw[:MAX_QUOTE_CHARS],
        "request_detected": False, "gift": None, "purpose_hint": "unknown",
        "recipient_hint": "", "urgency": None, "deadline_raw": "", "deadline_date": None,
        "date_status": "none", "time_basis": "unknown", "timezone": None,
        "intent": "unknown", "delivery_preference": "unknown", "custom_print_hint": False,
        "supersession_hint": "none", "reasons": []}
    if len(raw) > MAX_INPUT_CHARS:
        result["reasons"].append("input_bound")
        return result
    value, hypothetical = _customer_text(raw)
    # Trade logistics policies are business questions, not a requested ETA for
    # a purchase. Filter only those clauses; a real deadline/urgency in a mixed
    # message must retain the ordinary source-bound fulfillment path.
    from management.services.ig_business_cooperation import extract_business_cooperation
    if extract_business_cooperation(raw):
        clauses = re.split(r"(?<=[!?;\n])|(?<=\.)(?!\d)", value)
        value = " ".join(clause for clause in clauses if not (
            _GENERAL_SHIPPING.search(clause)
            and not (_NUMERIC.search(clause) or _LOCAL_DATE.search(clause)
                or _RELATIVE.search(clause) or _BARE_DAY.search(clause)
                or _URGENT.search(clause))))
    if hypothetical:
        result["reasons"].append("hypothetical_or_reported_clause_ignored")
    gift = list(_GIFT.finditer(value))
    if gift:
        affirmative = [_affirmed(value, match) for match in gift]
        result["gift"] = True if all(affirmative) else False if not any(affirmative) else None
        if result["gift"]:
            result["purpose_hint"] = "gift"
    from management.services.ig_recipient_planning import recipient_mentions, affirmative_apparel_request
    beneficiary_mentions, beneficiary_alternative = recipient_mentions(value)
    recipients = {item[0] for item in beneficiary_mentions}
    if len(recipients) == 1:
        result["recipient_hint"] = next(iter(recipients))
        if result["recipient_hint"] == "self" and result["gift"] is not True:
            result["purpose_hint"] = "self"
    elif recipients:
        if not beneficiary_alternative and affirmative_apparel_request(value):
            result["recipient_plan_hints"] = [item[0] for item in beneficiary_mentions]
        else:
            result["reasons"].append("recipient_ambiguous")
    urgent = [_affirmed(value, match) for match in _URGENT.finditer(value)]
    if urgent:
        result["urgency"] = True if all(urgent) else False if not any(urgent) else None
        if result["urgency"] is None:
            result["reasons"].append("urgency_ambiguous")
    arrival = any(_affirmed(value, match) for match in _ARRIVAL.finditer(value))
    dispatch = any(_affirmed(value, match) for match in _DISPATCH.finditer(value))
    result["custom_print_hint"] = any(_affirmed(value, match) for match in _CUSTOM.finditer(value))
    result["intent"] = "mixed" if arrival and dispatch else "arrival" if arrival else "dispatch" if dispatch else "unknown"
    together = any(_affirmed(value, match) for match in _TOGETHER.finditer(value))
    split = any(_affirmed(value, match) for match in _SPLIT.finditer(value))
    result["delivery_preference"] = "together" if together and not split else "split" if split and not together else "unknown"
    if together and split:
        result["reasons"].append("delivery_preference_ambiguous")
    if (together or split) and re.search(r"\b(?:або|или|or)\b", value, re.I):
        result["delivery_preference"] = "unknown"
        if "delivery_preference_ambiguous" not in result["reasons"]:
            result["reasons"].append("delivery_preference_ambiguous")
    if _affirmative_deadline_withdrawal(raw):
        result["supersession_hint"] = "withdrawn"
        result["reasons"].append("deadline_withdrawal")
        return result
    changed = bool(_CHANGED.search(value) or re.search(r"\bне\s+\d[^;\n]{0,40}\b(?:а|але)\b", value, re.I))
    candidates = []
    covered = []
    for match in _NUMERIC.finditer(value):
        covered.append(match.span())
        if _affirmed(value, match):
            candidates.append((match.group(), _date_value(match.group()), "explicit_date"))
    for match in _LOCAL_DATE.finditer(value):
        covered.append(match.span())
        if not _affirmed(value, match):
            continue
        day, month, year = match.group(1, 2, 3) if match.group(1) else match.group(5, 4, 6)
        parsed = None
        if year:
            try:
                parsed = date(int(year), _MONTHS[month.casefold()], int(day))
            except ValueError:
                pass
        candidates.append((match.group(), parsed, "explicit_date"))
    for match in _RELATIVE.finditer(value):
        covered.append(match.span())
        if _affirmed(value, match):
            parsed = None
            if _aware(provider_timestamp) and not (_aware(now) and provider_timestamp > now):
                days = 2 if match.group().casefold() in {"післязавтра", "послезавтра", "day after tomorrow"} else 1
                try:
                    parsed = provider_timestamp.astimezone(KYIV).date() + timedelta(days=days)
                except OverflowError:
                    pass
            candidates.append((match.group(), parsed, "provider"))
    for match in _BARE_DAY.finditer(value):
        if not any(start <= match.start() < end or match.start() <= start < match.end() for start, end in covered) and _affirmed(value, match):
            candidates.append((match.group(), None, "unknown"))
    # A date alone can answer an already open manager clarification; the caller
    # still owns the exact purchase and source. Dates embedded in unrelated text
    # never become a delivery requirement merely because they resemble a date.
    departure = any(_affirmed(value, match) for match in _DEPARTURE.finditer(value))
    relevant = bool(arrival or dispatch or departure or result["gift"] is True or _DATE_CUE.search(value)
        or (len(candidates) == 1 and value.strip(" .,!?;:") == candidates[0][0]))
    result["request_detected"] = bool(result["urgency"] is True or ((arrival or dispatch) and "?" in value) or (candidates and relevant))
    if candidates and relevant:
        result["deadline_raw"] = "; ".join(row[0] for row in candidates)[:240]
        if len(candidates) != 1 or candidates[0][1] is None:
            result["date_status"] = "needs_clarification"
            result["reasons"].append("deadline_ambiguous_or_incomplete")
        else:
            _, target, basis = candidates[0]
            result.update(deadline_date=target.isoformat(), date_status="resolved", time_basis=basis)
            if basis == "provider":
                result["timezone"] = KYIV.key
            comparison = now if _aware(now) else provider_timestamp
            if _aware(comparison) and target < comparison.astimezone(KYIV).date():
                result["date_status"] = "past"
    elif relevant and re.search(r"\b(?:deadline|дедлайн|дат[ау]|когда|коли|when|успе\w*|встиг\w*)\b", value, re.I):
        result["request_detected"] = True
        result["date_status"] = "needs_clarification"
        result["reasons"].append("deadline_missing")
    result = _departure_requirement(result, value)
    if changed and (result.get("deadline_hard") or result.get("deadline_raw")
            or re.search(r"\b(?:deadline|дедлайн|дат[ауиы]|термін\w*|срок\w*)\b", value, re.I)):
        result["supersession_hint"] = "changed"
    return result
