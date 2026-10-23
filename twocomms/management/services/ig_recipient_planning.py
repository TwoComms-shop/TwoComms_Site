"""Source-owned beneficiary planning; no product, quantity or delivery authority."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

VERSION = "recipient-planning.v1"
RECIPIENT_WORDS = {
    "себя": "self", "себе": "self", "собі": "self", "мені": "self", "мне": "self", "мене": "self", "меня": "self", "myself": "self", "me": "self",
    "друга": "friend", "другу": "friend", "friend": "friend",
    "подруги": "friend_female", "подругу": "friend_female",
    "мами": "mother", "мамы": "mother", "mother": "mother", "mom": "mother",
    "тата": "father", "папы": "father", "father": "father", "dad": "father",
    "boyfriend": "boyfriend", "хлопця": "boyfriend", "парня": "boyfriend", "хлопцю": "boyfriend", "парню": "boyfriend",
    "girlfriend": "girlfriend", "дівчини": "girlfriend", "девушки": "girlfriend", "дівчині": "girlfriend", "девушке": "girlfriend",
    "сестри": "sister", "сестры": "sister", "сестре": "sister", "сестрі": "sister", "sister": "sister",
    "брата": "brother", "брату": "brother", "братові": "brother", "brother": "brother",
}
_POSSESSIVE = r"(?:(?:мого|моего|моєї|моей|моїй|моєму|my)\s+)?"
_DIRECT = re.compile(r"\b(?:для|for)\s+" + _POSSESSIVE + r"(?P<role>\w+)\b", re.I)
_COORDINATED = re.compile(r"\s*(?:,\s*)?(?P<join>та|і|й|и|and|або|чи|или|or)\s+"
    r"(?:(?:для|for)\s+)?" + _POSSESSIVE + r"(?P<role>\w+)\b", re.I)
_ALTERNATIVE = frozenset({"або", "чи", "или", "or"})
_PRONOUNS = {"нього": "reference:him", "него": "reference:him", "him": "reference:him",
    "неї": "reference:her", "неё": "reference:her", "нее": "reference:her", "her": "reference:her"}
_ROLE_GENDER = {"reference:him": {"boyfriend", "brother", "father", "friend"},
    "reference:her": {"girlfriend", "sister", "mother", "friend_female"}}


def _current(text):
    from management.services.ig_commerce_turns import _current_source_text
    raw = str(text or "")
    if raw.lstrip().startswith(('"', "'", "«", "“", ">")):
        return ""
    return _current_source_text(raw)


def _affirmed_prefix(text):
    from management.services.ig_commerce_turns import _negated_action
    return not _negated_action(text) and not re.search(
        r"\b(?:не|ні|not|no)\s+(?:хочу|want|need)\b|\b(?:do|does|did)n['’]t\s+(?:want|need)\b", text, re.I)


def recipient_mentions(text):
    """Ordered literal beneficiaries; conjunction is not cart quantity."""
    current = _current(text)
    mentions, alternative = [], False
    leading = leading_recipient(current)
    if leading:
        mentions.append((RECIPIENT_WORDS[leading.group("role").casefold()], leading.group(), leading.start(), leading.end()))
    for match in _DIRECT.finditer(current):
        start = max(current.rfind(mark, 0, match.start()) for mark in ".!?;\n") + 1
        if not _affirmed_prefix(current[start:match.start()]):
            continue
        role = RECIPIENT_WORDS.get(match.group("role").casefold())
        if role:
            mentions.append((role, match.group(), match.start(), match.end()))
        end = match.end()
        for _ in range(8):
            following = _COORDINATED.match(current, end)
            if not following:
                break
            role = RECIPIENT_WORDS.get(following.group("role").casefold())
            if not role:
                break
            alternative |= following.group("join").casefold() in _ALTERNATIVE
            mentions.append((role, following.group(), following.start(), following.end()))
            end = following.end()
    unique = []
    for row in mentions:
        if row[0] not in {item[0] for item in unique}:
            unique.append(row)
    return tuple(unique), alternative


def leading_recipient(text):
    current = _current(text)
    match = re.match(r"\s*(?P<role>собі|мені|мне|хлопцю|парню|дівчині|девушке|сестрі|сестре|брату|братові)\b", current, re.I)
    return match if (match and _affirmed_prefix(current) and not re.search(
        r"\b(?:раніше|раньше|колись|якщо|если|if|казав\w*|сказ\w*|опт\w*|wholesale|b2b|або|или|or)\b", current, re.I)) else None


def operation_source_clause(text, ordinal=0):
    """Return the parser-owned operation clause, without catalog or provider IO."""
    from management.services.ig_commerce_turns import line_operation_source_clauses
    from management.services.ig_commerce_types import CatalogGraph
    clauses = line_operation_source_clauses(str(text or ""), parsed_catalog_graph=CatalogGraph((), "", ""))
    return clauses[ordinal][1] if 0 <= ordinal < len(clauses) else ""


def affirmative_apparel_request(text):
    """A present own shopping wish permits consultation, never checkout."""
    current = _current(text)
    if (not current.strip() or re.search(r"\b(?:якщо|если|if|would\s+you|maybe|perhaps|мабуть|можливо|опт\w*|wholesale|b2b|перепрод\w*|resell\w*|парті[юя]\w*|парт[июя]\w*|тираж\w*|дропшип\w*|dropship\w*|співпрац\w*|сотруднич\w*)\b", current, re.I)
        or re.search(r"\b(?:для|for)\s+(?:магазин\w*|shop|store)\b", current, re.I)):
        return False
    desire = re.search(r"\b(?:хочу|хочемо|хотим|(?:i|we)\s+(?:want|need)|i['’]d\s+like|i\s+would\s+like)\b", current, re.I)
    apparel = re.search(r"\b(?:одяг\w*|одежд\w*|футболк\w*|худі|худи|clothes|clothing|outfits?|t[ -]?shirts?|hoodies?)\b", current, re.I)
    return bool(desire and apparel and desire.start() < apparel.start()
        and len(current[desire.end():apparel.start()].split()) <= 10
        and not re.search(r"\b(?:сказ\w*|каж\w*|said|says|wrote|reported|цитат\w*|caption|description)\b", current[:desire.start()], re.I)
        and not re.search(r"\b(?:read|reading|article|почита\w*|прочита\w*|статт\w*|стать[юяи])\b", current[desire.end():apparel.start()], re.I)
        and _affirmed_prefix(current[:desire.start()]))


def planning_operations(text):
    from management.services.ig_commerce_turns import _garment_mentions
    from management.services.ig_commerce_types import CommerceLineOperation
    current = _current(text)
    mentions, alternative = recipient_mentions(current)
    if not affirmative_apparel_request(current) or len(mentions) < 2:
        return (), ""
    if alternative or len(mentions) > 8:
        return (), "ambiguous_recipient"
    # Initial planning and a later explicit add/remove are different acts.
    if re.search(r"\b(?:добав\w*|додай\w*|убер\w*|удал\w*|замен\w*|замін\w*|add|remove|replace|change)\b", current, re.I):
        return (), ""
    garments = {value for _start, _end, value in _garment_mentions(current)}
    garment = next(iter(garments)) if len(garments) == 1 else ""
    return tuple(CommerceLineOperation(operation="plan", recipient_id=role, garment_type=garment)
        for role, _quote, _start, _end in mentions), ""


def recipient_selector(text):
    """One source selector, including an unresolved pronoun; never choose a row."""
    current = _current(text)
    mentions, alternative = recipient_mentions(current)
    if alternative or len(mentions) > 1:
        return "", "", "ambiguous_recipient"
    if mentions:
        role, quote, _start, _end = mentions[0]
        return role, quote, ""
    for match in _DIRECT.finditer(current):
        role = _PRONOUNS.get(match.group("role").casefold())
        if role and _affirmed_prefix(current[:match.start()]):
            return role, match.group(), ""
    return "", "", ""


def source_event_time(source):
    value = getattr(source, "provider_created_at", None) or getattr(source, "created_at", None)
    return value.isoformat() if isinstance(value, datetime) and value.tzinfo is not None else ""


def resolve_recipient_selector(lines, selector, *, source_time=""):
    if selector not in _ROLE_GENDER:
        return selector
    candidates = [row for row in lines if isinstance(row, dict)
        and (row.get("recipient_plan") or row.get("recipient_reference")) and row.get("recipient_id") in _ROLE_GENDER[selector]]
    if len(candidates) != 1:
        return ""
    row = candidates[0]
    try:
        now = datetime.fromisoformat(source_time)
        reference = datetime.fromisoformat((row.get("recipient_reference") or row.get("recipient_plan") or {}).get("observed_at", ""))
        if now.tzinfo is None or reference.tzinfo is None or not timedelta(0) <= now - reference <= timedelta(hours=24):
            return ""
    except (TypeError, ValueError):
        return ""
    return row["recipient_id"]


def unscoped_group_choice(snapshot, request):
    rows = snapshot.get("lines") or []
    return bool(len(rows) > 1 and any(row.get("recipient_plan") or row.get("recipient_reference") for row in rows)
        and not request.line_operations and not request.pending_line_clarification
        and not request.full_cart_purchase_requested
        and (request.field_updates or request.exact_product_id or request.garment_type or request.reset_requested or request.recipient_id))


def planning_context(capture):
    """Hook for the sealed response owner; no recapture and no manager action."""
    if not isinstance(capture, dict) or capture.get("status") != "captured" or not capture.get("coverage_complete"):
        return {}
    rows = []
    for row in capture.get("lines") or []:
        proof = (row.get("source_selection") or {}).get("planning")
        if not isinstance(proof, dict) or proof.get("schema") != VERSION:
            continue
        rows.append({"line_id": row["line_id"], "recipient_id": row["recipient_id"],
            "garment_type": (row.get("fields") or {}).get("garment_type", {}).get("value") or proof.get("garment_type", ""), "source": proof,
            "has_product": bool((row.get("fields") or {}).get("product_id", {}).get("value"))})
    state = capture.get("recipient_planning_state") or {}
    return {"schema": VERSION, "recipients": rows, "quantity_confirmed": False,
        "delivery_confirmed": False,
        **({"clarification": state["clarification"], "clarification_source": state}
            if state.get("schema") == VERSION and state.get("clarification") else {})} if rows else {}


def planning_question(capture, language="uk"):
    context = planning_context(capture)
    if context.get("clarification"):
        return {"uk": "Для кого саме врахувати це побажання?", "ru": "Для кого именно учесть это пожелание?",
            "en": "Which person should this preference apply to?"}.get(language, "")
    if not context or all(row["has_product"] for row in context["recipients"]):
        return ""
    return {"uk": "Уже маєте обрані принти для кожного чи показати варіанти?",
        "ru": "Уже выбрали принты для каждого или показать варианты?",
        "en": "Have you chosen designs for each person, or would you like to see options?"}.get(language, "")


def planning_question_kind(context, question, line_plans):
    """Recognize a consultation question, without granting any selection effect."""
    if not context or "?" not in question:
        return ""
    from management.services.ig_response_plan import _unquoted_text
    from management.services.ig_response_cart_plan import matching_line_ids
    if _unquoted_text(question) != question:
        return ""
    text = question.casefold()
    if context.get("clarification") and re.search(
            r"для кого|для кого именно|which person|who(?:m)?\b.*(?:apply|preference)", text):
        return "recipient"
    unknown = {row["line_id"] for row in context.get("recipients", ()) if not row["has_product"]}
    if not unknown or re.search(r"\d|\b(?:розмір\w*|размер\w*|sizes?|колір\w*|цвет\w*|colou?rs?|кільк\w*|колич\w*|quantit\w*|оплат\w*|плат\w*|pay\w*|адрес\w*|address\w*|достав\w*|delivery|зниж\w*|скид\w*|discount\w*)\b", text):
        return ""
    if not re.search(r"\b(?:принт\w*|designs?|варіант\w*|вариант\w*|options?)\b", text):
        return ""
    matches = matching_line_ids(question, line_plans)
    if matches:
        return "product" if set(matches) <= unknown else ""
    # A shared question is only appropriate while every provisional print is
    # unknown. Once one is known the remaining person must be named.
    group = re.search(r"для кожного|для каждого|for each(?: person)?|для обох|для обоих|for both", text)
    return "product" if group and len(unknown) == len(line_plans) else ""
