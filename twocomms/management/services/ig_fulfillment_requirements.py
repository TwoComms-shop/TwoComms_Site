"""Pure customer requirement hints; the source envelope owns their authority.

Dates describe requested calendar days, never arrival guarantees or shipment
cutoffs. Relative days use the actual provider clock under the explicit Kyiv
policy. Neither ingestion time nor narrative/model output supplies that clock.
"""
from datetime import date, datetime, timedelta
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
}


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
        if (re.search(r"\b(?:якщо|если|if|hypothetical|might|цитат\w*)\b|(?:він|он|she|he)\s+(?:сказав|сказал|said|wrote)", clause, re.I)
                or re.search(r"\bwould\b", clause, re.I) and not polite_request):
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
    recipients = { _RECIPIENTS[match.group(1).casefold()] for match in re.finditer(
        r"\b(?:для|for)\s+(?:(?:my|мого|моего)\s+)?(" + "|".join(_RECIPIENTS) + r")\b", value, re.I)
        if _affirmed(value, match)}
    if len(recipients) == 1:
        result["recipient_hint"] = next(iter(recipients))
        if result["recipient_hint"] == "self" and result["gift"] is not True:
            result["purpose_hint"] = "self"
    elif recipients:
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
    if _WITHDRAW.search(value):
        result["supersession_hint"] = "withdrawn"
        result["reasons"].append("deadline_withdrawal")
        return result
    if _CHANGED.search(value) or re.search(r"\bне\s+\d[^;\n]{0,40}\b(?:а|але)\b", value, re.I):
        result["supersession_hint"] = "changed"
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
    relevant = bool(arrival or dispatch or result["gift"] is True or _DATE_CUE.search(value)
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
    return result
