"""Own-current business cooperation evidence from an already owned source set.

This pure adapter opens discussion, never pricing, fulfillment or send authority.
The caller supplies sealed USER sources in conversation order; no CRM history,
provider, cache or mutable client state is read here.
"""
import re

from management.services.ig_reply_language import own_reply_text, _QUOTES


_STORE = re.compile(r"\b(?:магазин\w*|бутик\w*|shops?|stores?|boutiques?)\b", re.I)
_WHOLESALE = re.compile(r"\b(?:опт(?:ом|ов\w*)?|гурт\w*|wholesale|b2b)\b", re.I)
_DROPSHIP = re.compile(r"\b(?:дропшип\w*|дроп[- ]?шип\w*|dropship\w*|drop[- ]shipping)\b", re.I)
_PARTNERSHIP = re.compile(r"\b(?:партнерств\w*|партнёрств\w*|співпрац\w*|сотруднич\w*|collab\w*|cooperat\w*|partnership\w*)\b", re.I)
_CREATOR = re.compile(r"\b(?:дизайнер\w*|designer\w*|artist|художник\w*|фотограф\w*|photographer\w*|відеограф\w*|видеограф\w*|videographer\w*|creator|креатор|ugc|блогер\w*|influencer\w*|інфлюенсер\w*|инфлюенсер\w*|модел\w*|models?|стиліст\w*|стилист\w*|stylist\w*)\b", re.I)
_RESELL = re.compile(r"\b(?:перепродаж\w*|перепродав\w*|resell\w*|прода(?:вати|вать)\s+ваш\w*|sell(?:ing)?\s+your)\b", re.I)
_LAUNCH = re.compile(
    r"\b(?:(?:хочу|хочемо|планую|плануємо|планирую|планируем|збираюся|собираюсь|"
    r"(?:i|we)\s+(?:want|plan|intend)(?:\s+to)?|(?:i\s+am|i'm|we\s+are)\s+going\s+to)"
    r"[^.!?;\n]{0,35}(?:відкрити|открыть|запустити|запустить|відкривати|открывать|open|launch|start)|"
    r"відкриваю|відкриваємо|открываю|открываем|запускаю|запускаємо|запускаем|"
    r"(?:i\s+am|i'm|we\s+are)\s+(?:opening|launching|starting))\b", re.I)
_REPORTED = re.compile(r"^(?:це\s+(?:(?:лише|просто)\s+)?цитата|цитата|this\s+is\s+(?:a\s+)?quote|у\s+(?:рекламі|статті)|в\s+(?:рекламе|статье)|"
    r"(?:друг|подруга|він|вона|он|она)\s+(?:сказ\w*|напис\w*|каже|говор\w*)|"
    r"(?:мені|мне)\s+(?:сказали|написали)|quote|(?:my\s+friend|he|she|they)\s+(?:said|wrote))\b", re.I)
_HISTORICAL = re.compile(r"\b(?:раніше|раньше|колись|торік|минулого|previously|earlier|used\s+to|last\s+time)\b", re.I)
_CURRENT = re.compile(r"\b(?:зараз|тепер|сейчас|теперь|now|currently)\b", re.I)
_WITHDRAW = re.compile(
    r"\b(?:не\s+(?:хочу|хочемо|цікав\w*|интерес\w*|потріб\w*|нуж\w*|актуальн\w*|пропону\w*|предлага\w*|буду|будемо|будем)|"
    r"(?:більше|больше)\s+не|відмовля\w*|отказыва\w*|скасуй\w*|отмени\w*|"
    r"not\s+(?:interested|proposing|offering)|no\s+longer|(?:do\s+not|don['’]t)\s+want|withdraw\w*|cancel\w*)\b|"
    r"\b(?:співпрац\w*|сотруднич\w*|партнерств\w*|partnership|wholesale|dropship\w*)"
    r"[^.!?;\n]{0,35}\b(?:не\s+(?:цікав\w*|интерес\w*|потріб\w*|нуж\w*)|скасован\w*|отменен\w*)\b", re.I)
_TERSE_WITHDRAW = re.compile(
    r"^(?:(?:це|это|вже|уже|більше|больше)\s+){0,2}не\s+"
    r"(?:актуальн\w*|цікав\w*|интерес\w*|потріб\w*|нуж\w*)(?:,\s*(?:дякую|спасибо))?$|"
    r"^(?:no\s+longer\s+interested|not\s+interested\s+anymore|never\s+mind)(?:,\s*thanks)?$", re.I)
_TOPICS = (
    ("partner_price", re.compile(r"\b(?:цін\w*|цен\w*|price\w*|pricing|знижк\w*|скидк\w*|discount\w*|комісі\w*|комисси\w*|commission\w*)\b", re.I)),
    ("catalog_options", re.compile(r"\b(?:розмір\w*|размер\w*|кольор\w*|колір\w*|цвет\w*|sizes?|colou?rs?)\b", re.I)),
    ("start_without_stock", re.compile(r"\b(?:без\s+(?:закуп\w*|парті\w*|партии|склад\w*)|without\s+(?:buying|purchasing|stock|inventory)|dropship\w*|дропшип\w*)\b", re.I)),
    ("delivery_terms", re.compile(r"\b(?:достав\w*|відправ\w*|отправ\w*|shipping|delivery|dispatch)\b", re.I)),
    ("resale_channel", re.compile(r"\b(?:instagram|інстаграм|инстаграм|перепродаж\w*|прода(?:вати|вать)|resell\w*|sell\w*)\b", re.I)),
    ("photo_usage", re.compile(r"\b(?:фото\w*|фотографі\w*|photo\w*|images?|pictures?)\b", re.I)),
)


def _own_cooperation_text(text):
    # Keep a genuinely supplied contact URL/handle in the operator brief. Its
    # text never becomes a routing signal (own_reply_text below removes it).
    value = re.sub(r"(?m)^\s*>[^\n]*(?:\n|$)", " ", str(text or ""))
    value = _QUOTES.sub(" ", value).strip()
    return "" if value.startswith(('"', "'", "«", "“", ">")) else value


def current_cooperation_sources(sources):
    """Keep own-current clauses, clearing earlier offers on explicit withdrawal.

    An unrelated later retail question/greeting does not erase the business
    question. Explicit reentry after withdrawal starts a fresh evidence set.
    Returned rows retain source IDs/digests; only their routing text is filtered.
    """
    result = []
    for source in sources or ():
        if not isinstance(source, dict) or source.get("role", "user") != "user":
            continue
        clauses = []
        own = _own_cooperation_text(source.get("text") or "")
        for part in re.split(r"[!?;\n]+|\.(?=\s|$)|\b(?:але|но|but)\b|,\s*а\s+", own, flags=re.I):
            clause = part.strip(" ,:")
            if (not clause or _REPORTED.search(clause) or re.search(
                r"\b(?:не\s+(?:моя|моє|мое|моё)\s+(?:пропозиція|предложение)|not\s+my\s+(?:offer|proposal))\b", clause, re.I)):
                continue
            historical = _HISTORICAL.search(clause)
            if historical:
                current = _CURRENT.search(clause, historical.end())
                if current is None:
                    continue
                clause = clause[current.start():]
            routing = own_reply_text(clause)
            topic = bool(_STORE.search(routing) or _WHOLESALE.search(routing)
                or _DROPSHIP.search(routing) or _PARTNERSHIP.search(routing) or _CREATOR.search(routing))
            if (topic and _WITHDRAW.search(routing)) or _TERSE_WITHDRAW.fullmatch(routing):
                result.clear()
                clauses.clear()
                continue
            clauses.append(clause)
        if clauses:
            result.append({**source, "text": ". ".join(clauses)})
        elif not str(source.get("text") or "").strip() and source.get("media_parts"):
            result.append(dict(source))
    return result


def business_cooperation_for_sources(sources):
    """Return a bounded business brief, or {} without current own evidence."""
    current = current_cooperation_sources(sources)
    text = ". ".join(own_reply_text(str(row.get("text") or "")) for row in current)
    store, launch = bool(_STORE.search(text)), bool(_LAUNCH.search(text))
    wholesale, dropship = bool(_WHOLESALE.search(text)), bool(_DROPSHIP.search(text))
    resell = bool(_RESELL.search(text))
    partnership = bool(_PARTNERSHIP.search(text))
    # A designer/photographer's generic collaboration is its separate creator
    # path. Store launch, resale and explicit trade terms remain business facts.
    business = wholesale or dropship or resell or store and (launch or partnership)
    business |= partnership and not _CREATOR.search(text)
    if not business:
        return {}
    subtypes = []
    if dropship:
        subtypes.append("dropship")
    if wholesale or store or resell:
        subtypes.append("wholesale_store")
    if partnership:
        subtypes.append("partnership")
    if not subtypes:
        subtypes.append("partnership")
    ids = [row["message_id"] for row in current if type(row.get("message_id")) is int and row["message_id"] > 0]
    return {
        "schema_version": 1, "business_cooperation": True,
        "subtypes": subtypes, "primary_subtype": subtypes[0],
        "shop_launch": bool(store and launch),
        "requested_topics": [name for name, pattern in _TOPICS if pattern.search(text)],
        "evidence_message_ids": list(dict.fromkeys(ids))[-64:],
        "decision_owner": "manager", "multiple_intents": len(subtypes) > 1,
    }


def extract_business_cooperation(text):
    return business_cooperation_for_sources([{"role": "user", "text": text}])
