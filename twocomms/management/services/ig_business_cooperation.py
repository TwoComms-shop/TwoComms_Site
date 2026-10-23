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
_SUPPORT_WORK = re.compile(
    r"(?:допомог\w*|помощ\w*|help|support)[^.!?\n]{0,55}\b(?:instagram\s+)?direct\b"
    r"|(?:менеджер\w*|оператор\w*|customer\s+support)[^.!?\n]{0,35}\bdirect\b"
    r"|\bdirect\b[^.!?\n]{0,35}(?:менеджер\w*|оператор\w*|customer\s+support)"
    r"|(?:відповід\w*|отвеч\w*|answer\w*|reply\w*|respond\w*|консульт\w*)"
    r"[^.!?\n]{0,60}(?:повідомлен\w*|сообщени\w*|messages?|customers?|clients?|клієнт\w*|клиент\w*)"
    r"|\bcustomer\s+(?:support|service)\b", re.I)
_WORK_OFFER = re.compile(
    r"\b(?:(?:за)?пропон\w*|предлага\w*|запропон\w*|можу|можемо|могу|можем|готов\w*|"
    r"(?:хочу|хотів|хотіла|хотел)\s+(?:би\s+|бы\s+)?(?:працювати|работать)|"
    r"(?:i|we)\s+(?:can|offer|(?:want|would\s+like)\s+to\s+(?:work|help))|i['’]m\s+interested\s+in\s+working)\b", re.I)
_WORK_WITHDRAWAL = re.compile(
    r"\b(?:не\s+(?:хочу|буду|пропон\w*|предлага\w*|можу|могу)|(?:do\s+not|don['’]t)\s+"
    r"(?:want|offer)|no\s+longer\s+(?:want|offer|interested)|not\s+(?:offering|interested))"
    r"[^.!?\n]{0,65}(?:direct|допомог\w*|помощ\w*|прац\w*|работ\w*|work|help|support|answer)"
    r"|(?:direct|customer\s+support)[^.!?\n]{0,35}(?:не\s+актуальн\w*|no\s+longer)", re.I)
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


def business_source_observations(sources):
    """Exact customer observations, not qualification or agreed trade terms."""
    import hashlib
    originals = {row.get("message_id"): row for row in sources if isinstance(row, dict)}
    current = current_cooperation_sources(sources)
    if not business_cooperation_for_sources(current):
        return []
    observations = []
    future = re.compile(r"план\w*|plan\w*|хочу|хочемо|want|intend|future|майбут\w*|\bwill\b|\bбуду\b|\bбудемо\b|\bбудем\b|expect\w*|очіку\w*|ожида\w*", re.I)
    for source in current:
        if type(source.get("message_id")) is not int or source["message_id"] <= 0:
            continue
        for clause in re.findall(r"[^.!?\n]+[.!?]?", str(source.get("text") or "")):
            text = clause.strip()
            original = str(originals[source["message_id"]].get("text") or "")
            # Questions, future thresholds and negated offers are not a sales
            # record. Keep their original text in the normal source brief.
            quote = text.rstrip(".!?")
            if (quote not in original or "?" in original[original.find(quote):original.find(quote)+len(quote)+1]
                or not re.search(r"\b(?:я|ми|мы|у\s+мене|у\s+нас|у\s+меня|i|we|(?:my|our)\s+(?:shop|store|business))\b", text, re.I)
                or re.search(r"\b(?:якщо|если|if|would|could|мож\w*|не|not|no)\b", text, re.I)):
                continue
            basis = "planned" if future.search(text) else "stated"
            values = []
            bases = {}
            monthly = re.search(r"\b(\d{1,6})\s*(?:продаж\w*|sales)\s*(?:на|в|за|per|a)\s*(?:місяц\w*|месяц\w*|month)\b", text, re.I)
            monthly_owner = re.match(r"^(?:(?:i|we)\s+(?:(?:will|shall)\s+)?(?:have|make|record|expect|plan)|"
                r"(?:my|our)\s+(?:shop|store|business)\s+(?:has|have|records|makes)|"
                r"у\s+(?:мене|нас|меня)\b|(?:я|ми|мы)\s+(?:(?:буду|будемо|будем)\s+)?(?:маю|маємо|имею|имеем|мати|иметь|план\w*|очіку\w*|ожида\w*))", text, re.I)
            monthly_gap = text[monthly_owner.end():monthly.start()].strip() if monthly and monthly_owner else None
            if monthly and monthly_owner and re.fullmatch(r"(?:(?:about|around|approximately|roughly|to\s+have|мати|иметь|є|близько|приблизно|около|примерно)\s*)?", monthly_gap, re.I):
                values.append(("monthly_sales", int(monthly[1]), "sales_per_month"))
                bases["monthly_sales"] = "planned" if future.search(text[:monthly.end()]) else "stated"
            batch = re.search(r"(?:купую|купил\w*|викуп\w*|выкуп\w*|buy|buying|order)\s+(\d{1,6})\s*(?:реч\w*|штук\w*|футбол\w*|худі|худи|items?|garments?|shirts?|hoodies?)\b", text, re.I)
            if batch:
                values.append(("batch_quantity", int(batch[1]), "garments"))
            if re.search(r"instagram|інстаграм|инстаграм", text, re.I):
                values.append(("resale_channel", "instagram", "channel"))
            if re.search(r"(?:я|ми|i|we)[^.!?]{0,20}(?:оформл\w*|оформля\w*|prepare|create)[^.!?]{0,25}(?:ттн|waybill)", text, re.I):
                values.append(("waybill_preference", "partner", "requested_owner"))
            for kind, value, unit in values:
                observations.append({"kind": kind, "value": value, "unit": unit,
                    "basis": bases.get(kind, basis), "quote": quote[:500], "source_message_id": source["message_id"],
                    "source_digest": source.get("source_digest") or hashlib.sha256(original.encode()).hexdigest(), "authority": "customer_statement",
                    "individual_terms_confirmed": False})
    return observations[-24:]


def render_business_observations(observations):
    labels = {"monthly_sales": "Продажі за місяць", "batch_quantity": "Кількість для закупівлі",
        "resale_channel": "Канал перепродажу", "waybill_preference": "Хто оформляє ТТН"}
    units = {"sales_per_month": "продажів на місяць", "garments": "речей", "channel": "", "requested_owner": ""}
    values = {"instagram": "Instagram", "partner": "партнер (побажання клієнта)"}
    return "\n".join(f"{labels[row['kind']]} ({'план клієнта' if row['basis'] == 'planned' else 'зі слів клієнта'}, не погоджено): {values.get(row['value'], row['value'])} {units[row['unit']]} · повідомлення №{row['source_message_id']}: {row['quote']}"
        for row in observations)


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
    business |= partnership and not _CREATOR.search(text) and not recruitment_service_for_sources(current)
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
    brief = {
        "schema_version": 1, "business_cooperation": True,
        "subtypes": subtypes, "primary_subtype": subtypes[0],
        "shop_launch": bool(store and launch),
        "requested_topics": [name for name, pattern in _TOPICS if pattern.search(text)],
        "evidence_message_ids": list(dict.fromkeys(ids))[-64:],
        "decision_owner": "manager", "multiple_intents": len(subtypes) > 1,
    }
    return brief


def extract_business_cooperation(text):
    return business_cooperation_for_sources([{"role": "user", "text": text}])


def recruitment_service_for_sources(sources):
    """Own offered Direct/customer-support work, never a hiring decision.

    Merely asking for a manager, discussing a purchase or giving a photographer
    a Direct contact channel does not offer customer-support work.
    """
    from management.services.ig_reply_truth import _locally_negated
    evidence = []
    for source in current_cooperation_sources(sources):
        text = own_reply_text(str(source.get("text") or ""))
        if _WORK_WITHDRAWAL.search(text) or _TERSE_WITHDRAW.fullmatch(text.strip(" .!")):
            evidence.clear()
            continue
        offers = [match for match in _WORK_OFFER.finditer(text)
            if not _locally_negated(text, match.start())
            and not re.match(r"['’]t\b|\s+(?:not|не)\b", text[match.end():], re.I)]
        if _SUPPORT_WORK.search(text) and offers:
            evidence.append(source)
    if not evidence:
        return {}
    text = " ".join(str(row.get("text") or "") for row in evidence)
    return {"schema_version": 1, "employment_service_offer": True,
        "subtype": "customer_support_service", "decision_owner": "manager",
        "service_scope": "instagram_direct" if re.search(r"\bdirect\b", text, re.I) else "customer_support",
        "evidence_message_ids": [row["message_id"] for row in evidence if type(row.get("message_id")) is int][-64:],
        "hiring_status": "unknown", "work_conditions_confirmed": False,
        "creator_work_also_offered": bool(_CREATOR.search(text) and re.search(
            r"фото\w*|відео\w*|видео\w*|\b(?:photos?|videos?|reels?|portfolio)\b", text, re.I))}


def recruitment_response_reason(sources, response):
    """Reject creator requirements for an actual customer-support offer."""
    recruitment = recruitment_service_for_sources(sources)
    if not recruitment or recruitment.get("creator_work_also_offered"):
        return ""
    from management.services.ig_reply_truth import _locally_negated
    text = own_reply_text(response.reply_text)
    for clause in re.split(r"[.!?;\n]+", text):
        request = re.search(r"надішл\w*|покаж\w*|пришл\w*|отправ\w*|поділ\w*|дайте|send|share|show|provide", clause, re.I)
        creator = re.search(r"портфол\w*|портфоліо|portfolio|reels?|(?:фото|відео|видео|photo|video)[-\s]*(?:робіт|работ|work|examples?)|creator\s+results", clause, re.I)
        negated_need = request and re.search(
            r"(?:do\s+not|don['’]t)\s+(?:need|have)\s+to\s*$|"
            r"не\s+(?:потрібно|треба|нужно)\s*$", clause[:request.start()], re.I)
        if request and creator and not negated_need and not _locally_negated(clause, request.start()):
            return "recruitment_creator_requirements"
    return ""
