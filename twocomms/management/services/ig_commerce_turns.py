"""Bounded multilingual turn parsing for the commerce state reducer."""

from __future__ import annotations

import re

from .ig_commerce_types import CommerceLineOperation, CommerceTurnRequest
from .ig_product_references import resolve_product_reference


_COLOR_WORDS = {
    "black": "black", "черн": "black", "чорн": "black",
    "white": "white", "бел": "white", "біл": "white",
    "син": "blue", "blue": "blue",
    "pink": "pink", "розов": "pink", "рожев": "pink",
    "grey": "grey", "gray": "grey", "сер": "grey", "сір": "grey",
    "green": "green", "зелен": "green",
}
_FIT_WORDS = {
    "classic": "classic", "классик": "classic", "классич": "classic", "класик": "classic",
    "класич": "classic", "standard": "classic", "стандарт": "classic",
    "regular": "classic", "oversize": "oversize", "oversized": "oversize",
    "оверсайз": "oversize",
}
_GARMENT_WORDS = {
    "футболк": "tshirt", "t-shirt": "tshirt", "tshirt": "tshirt",
    "худі": "hoodie", "худи": "hoodie", "hoodie": "hoodie",
}
_SIZE_RE = re.compile(r"(?<!\w)(?:xxxs|xxs|xs|[xх]{1,3}[lл]|[2-5][xх][lл]|s|m|l|л|м|с|ес|эм|ем|ель|эль|ікс\s*ель|икс\s*эль)(?!\w)", re.I)
_SIZE_ALIASES = {"л": "L", "м": "M", "с": "S", "ес": "S", "эм": "M", "ем": "M", "ель": "L", "эль": "L", "іксель": "XL", "иксэль": "XL", "2XL": "XXL", "3XL": "XXXL"}
_SOURCE_WORD_SUFFIXES = {
    "черн": r"(?:ый|ая|ое|ые|ую|ого|ой|ых|ыми|ым|ом|ому)?",
    "бел": r"(?:ый|ая|ое|ые|ую|ого|ой|ых|ыми|ым|ом|ому)?",
    "сер": r"(?:ый|ая|ое|ые|ую|ого|ой|ых|ыми|ым|ом|ому)?",
    "розов": r"(?:ый|ая|ое|ые|ую|ого|ой|ых|ыми|ым|ом|ому)?",
    "чорн": r"(?:ий|а|е|і|у|ого|ої|их|ими|им|ому|ою)?",
    "рожев": r"(?:ий|а|е|і|у|ого|ої|их|ими|им|ому|ою)?",
    "сір": r"(?:ий|а|е|і|у|ого|ої|их|ими|им|ому|ою)?",
    "син": r"(?:ий|яя|ее|ие|юю|его|ей|их|ими|им|ем|ему|ій|я|є|і|ю|ього|ьої|іх|ім|ьому|ьою)?",
    "зелен": r"(?:ый|ая|ое|ые|ую|ого|ой|ых|ыми|ым|ом|ому|ий|а|е|і|у|ої|ою)?",
    "біл": r"(?:ий|а|е|і|у|ого|ої|их|ими|им|ому|ою)?",
    "футболк": r"(?:а|и|у|е|ою|ой|ами|ах|ам|і)?",
    "hoodie": r"s?", "tshirt": r"s?", "t-shirt": r"s?",
}


def _word_pattern(needle):
    suffix = _SOURCE_WORD_SUFFIXES.get(needle, "" if needle.isascii() else r"[\w-]*")
    return r"(?<!\w)" + re.escape(needle) + suffix + r"(?!\w)"


def _current_source_text(text):
    """Keep current customer assertions; mask quotes and reported/old copy.

    Positions stay stable for URL and clause evidence. An explicit present
    correction after a historical clause remains available to the parser.
    """
    raw = str(text or "")
    visible = list(raw)
    for match in _quoted_preference_spans(raw):
        visible[match.start():match.end()] = " " * (match.end() - match.start())
    masked = "".join(visible)
    for match in re.finditer(r"[^.!?;\n,]+[.!?;\n,]?", masked):
        clause = match.group().casefold()
        if re.search(r"\b(?:не|ні|not|don['’]t|do\s+not)\s+(?:(?:хочу|want\s+to)\s+)?(?:добав\w*|додай\w*|замен\w*|замін\w*|убер\w*|удал\w*|измен\w*|змін\w*|add|remove|delete|replace|change)\b", clause):
            visible[match.start():match.end()] = " " * (match.end() - match.start())
            continue
        if re.search(
            r"\b(?:раньше|раніше|прежде|previously|earlier|formerly|used\s+to|last\s+time)\b"
            r"|(?:прошл|минул)\w*\s+(?:раз|заказ|замовлен)"
            r"|написан\w*|написав\w*|писа[вл]\w*|\b(?:said|wrote|reported|caption|description|advert\w*)\b"
            r"|опис[аі]\w*|(?:в|у|из|з)\s+реклам\w*|в\s+карточк\w*",
            clause,
        ):
            # A marker explicitly opens a new current assertion, rather than
            # allowing a later product word to inherit historical authority.
            present = re.search(r"\b(?:теперь|зараз|сейчас|тепер|now)\b", clause)
            stop = match.start() + (present.start() if present else len(match.group()))
            visible[match.start():stop] = " " * (stop - match.start())
    return "".join(visible)


def _negated_action(before):
    return bool(re.search(
        r"\b(?:не|ні|not|no)(?:\s+(?:хочу|want|need|нужен|нужно|надо|треба|потрібно|a|the)){0,2}\s*$"
        r"|\b(?:do|does|did)n['’]t\s+(?:(?:want|need)(?:\s+to)?\s+)?$"
        r"|\bdo\s+not\s+(?:(?:want|need)(?:\s+to)?\s+)?$"
        r"|\b(?:have|has|had)n['’]t\s+(?:requested|chosen|selected|ordered|asked\s+for)\s*$", before.casefold(),
    ))


def _preference_source_text(text):
    # A product slug is identity evidence, never a customer's chosen size,
    # color, or fit. Preserve offsets while omitting URL path vocabulary.
    return re.sub(r"https?://[^\s<>]+", lambda match: " " * len(match.group()), _current_source_text(text))


def _canonical_source_size(token):
    token = re.sub(r"\s+", "", token).casefold()
    if token in _SIZE_ALIASES:
        return _SIZE_ALIASES[token]
    token = token.translate(str.maketrans({"х": "x", "л": "l"})).upper()
    return _SIZE_ALIASES.get(token, token)


def _contracted_size_negation(before):
    """Distinguish a rejected choice from a report that never affirmed one."""
    if re.search(r"\b(?:do|does|did)n['’]t\s+(?:want|need|choose|select|order|buy)(?:\s+(?:a|the|size)){0,2}\s*$", before, re.I):
        return "rejected"
    if re.search(r"\b(?:have|has|had)n['’]t\s+(?:requested|chosen|selected|ordered|said|asked\s+for)(?:\s+(?:a|the|size)){0,2}\s*$", before, re.I):
        return "unaffirmed"
    return ""


def _source_size(text: str) -> str:
    """A chosen size, with explicit corrections winning over rejected mentions."""
    text = _preference_source_text(text)
    candidates = list(_SIZE_RE.finditer(text))
    selected = []
    for match in candidates:
        start, end = _preference_segment(text, match.start())
        segment = text[start:end].casefold()
        if "?" in segment or _is_quoted_preference(text, match.start(), match.end()):
            continue
        if re.search(r"\b(?:який|какой|which|what)\b|\b(?:є|есть|available|have)\b", segment):
            continue
        if re.search(r"розмірн\w*|размерн\w*|size\s+(?:guide|chart)|опис[аі]\w*|description|карточк", segment):
            continue
        before = text[start:match.start()].casefold()
        after = text[match.end():end].casefold()
        if _contracted_size_negation(before):
            continue
        if re.search(r"\b(?:не|ні|not|no)\s+(?:(?:хочу|потрібен|треба|want|size|розмір|размер)\s+){0,2}$", before):
            continue
        if re.match(r"\s*(?:не\s+(?:хочу|треба|потрібен)|not\s+wanted)\b", after):
            continue
        token = re.sub(r"\s+", "", match.group()).casefold()
        if token in {"с", "м"} and text.strip(" .!").casefold() != token and not re.search(r"(?:розмір|размер|size)\s*$", before):
            continue
        selected.append(_canonical_source_size(token))
    if re.search(r"\b(?:или|або|чи|or)\b|/", text) and len(candidates) > 1:
        return ""
    return selected[-1] if len(set(selected)) == 1 else ""


def _source_purchase(text: str) -> bool:
    text = _current_source_text(text)
    for match in re.finditer(r"(?:хочу|хочемо|хотів\s+би|хотел\s+бы|жел[ао]ю|(?:i\s+)?want\s+to|i(?:'d|\s+would)\s+like\s+to)\s+(?:замовити|заказать|купити|купить|order|buy|purchase|оформити\s+замовлення|оформить\s+заказ)|\b(?:замовляю|заказываю|купую|беру)\b", text, re.I):
        start, end = _preference_segment(text, match.start())
        if not _is_quoted_preference(text, match.start(), match.end()) and "?" not in text[start:end] and not _negated_action(text[start:match.start()]):
            return True
    return False


def _withdrawn_size(text: str) -> str:
    text = _preference_source_text(text)
    rejected = set()
    for match in _SIZE_RE.finditer(text):
        start, end = _preference_segment(text, match.start())
        segment = text[start:end].casefold()
        if "?" in segment or _is_quoted_preference(text, match.start(), match.end()) or re.search(r"розмірн\w*|размерн\w*|size\s+(?:guide|chart)|опис[аі]\w*|description|карточк", segment):
            continue
        before = text[start:match.start()].casefold()
        if _contracted_size_negation(before) == "rejected" or re.search(r"\b(?:не|ні|not|no)\s+(?:(?:хочу|потрібен|треба|want|size|розмір|размер)\s+){0,2}$", before):
            token = re.sub(r"\s+", "", match.group()).casefold()
            rejected.add(_canonical_source_size(token))
    return next(iter(rejected)) if len(rejected) == 1 else ""


def _find_prefix_value(text: str, words: dict[str, str]) -> str:
    """Return one explicitly selected value, never just a mentioned one.

    Preference source evidence must survive a reparse, so negated choices,
    alternatives, questions, and quoted product copy deliberately abstain.
    """
    lowered = _preference_source_text(text).casefold().replace("ё", "е")
    candidates = [
        (match.start(), match.end(), value)
        for needle, value in words.items()
        for match in re.finditer(_word_pattern(needle), lowered)
    ]
    if not candidates:
        return ""

    selected: list[tuple[int, str]] = []
    for start, end, value in candidates:
        segment_start, segment_end = _preference_segment(lowered, start)
        segment = lowered[segment_start:segment_end]
        if "?" in segment or _is_quoted_preference(lowered, start, end):
            continue
        if re.search(r"(?:описани|description|карточк|характеристик)\w*", segment):
            continue
        before = lowered[segment_start:start]
        if re.search(r"\b(?:do|does|did|have|has|had)n['’]t\s+(?:want|need|choose|select|order|buy|requested|chosen|selected)(?:\s+(?:a|the|color|fit)){0,2}\s*$", before):
            continue
        if re.search(
            r"(?:^|[\s,;:])(?:не|ні|без|no|not)(?:\s+[\w-]+){0,2}\s*$",
            before,
        ):
            continue
        selected.append((start, value))

    # "классика или оверсайз" names possibilities, not a choice. Do not let
    # either side become state, even when the question mark was omitted.
    if any(
        re.search(r"\b(?:или|або|чи|or)\b", lowered[first[0]:second[0]])
        for index, first in enumerate(candidates)
        for second in candidates[index + 1:]
    ):
        return ""

    values = {value for _position, value in selected}
    if len(values) != 1:
        return ""
    if _withdrawn_preference(text, words) in values:
        return ""
    return selected[-1][1]


def _preference_segment(text: str, position: int) -> tuple[int, int]:
    """Return the sentence-like span that supplies preference context."""
    starts = [text.rfind(marker, 0, position) for marker in ".!?;\n"]
    start = max(starts) + 1
    ends = [index for marker in ".!?;\n" if (index := text.find(marker, position)) >= 0]
    # Keep the delimiter in the scope: otherwise a trailing "?" would be
    # omitted and a question such as "Оверсайз?" would look affirmative.
    return start, min(ends) + 1 if ends else len(text)


def _quoted_preference_spans(text: str):
    """Bounded quotation spans, retaining apostrophes inside words."""
    return re.finditer(r'''"[^"\n]{0,240}"|«[^»\n]{0,240}»|“[^”\n]{0,240}”|‘(?:[^’\n]|(?<=\w)’(?=\w)){0,240}’|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w)){0,240}'(?!\w)''', text)


def _is_quoted_preference(text: str, start: int, end: int) -> bool:
    """Quotes often repeat card or product-description text rather than select it."""
    for match in _quoted_preference_spans(text):
        if match.start() <= start and end <= match.end():
            return True
    return False


def _has_unaffirmed_preference_mention(text: str, words: dict[str, str]) -> bool:
    """Keep model hints from turning an abstained source mention into a choice."""
    lowered = text.casefold().replace("ё", "е")
    return any(re.search(_word_pattern(needle), lowered) for needle in words) and not _find_prefix_value(text, words)


def _is_negated_url(text: str, url: str, *, position=None) -> bool:
    before = text[: text.find(url) if position is None else position].casefold()
    return bool(re.search(r"(?:не|не хочу|не нужен|not|don't want)\s*$", before[-32:]))


def _withdrawn_preference(text: str, words: dict[str, str]) -> str:
    """Recognize a direct rejection of one value, not a negative description."""
    lowered = _preference_source_text(text).casefold().replace("ё", "е").replace("’", "'")
    values = set()
    for needle, value in words.items():
        for match in re.finditer(_word_pattern(needle), lowered):
            start, end = _preference_segment(lowered, match.start())
            segment = lowered[start:end]
            if "?" in segment or _is_quoted_preference(lowered, match.start(), match.end()):
                continue
            if re.search(r"(?:описани|description|карточк|характеристик)\w*", segment):
                continue
            before = lowered[start:match.start()]
            after = lowered[match.end():end].strip(" .!")
            direct_prefix = re.search(
                r"(?:^|[,;:])\s*(?:(?:я|i)\s+)?"
                r"(?:не(?:\s+(?:хочу|нужен|нужна|нужно|треба|потрібен|потрібна))?"
                r"|not|no|(?:don't|do not|no longer)\s+want)\s+$", before,
            )
            direct_suffix = (
                not before.strip()
                and re.fullmatch(r"(?:не\s+(?:хочу|нужен|нужна|нужно|треба)|not\s+wanted)", after)
            )
            if direct_prefix or direct_suffix:
                values.add(value)
    return next(iter(values)) if len(values) == 1 else ""


def _parse_model_payload(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    allowed = {"color", "fit", "size", "garment_type", "checkout_requested"}
    result = {}
    for key in allowed:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
        elif key == "checkout_requested" and isinstance(value, bool):
            result[key] = value
    return result


_RECIPIENT_WORDS = {"себя": "self", "себе": "self", "myself": "self", "друга": "friend", "другу": "friend", "friend": "friend", "подруги": "friend_female", "подругу": "friend_female", "мами": "mother", "мамы": "mother", "mother": "mother", "тата": "father", "папы": "father", "father": "father"}
_ADD_ACTION = r"\b(?:добав(?:ьте|ь|ить)|додай(?:те)?|додати|add)\b"
_REMOVE_ACTION = r"\b(?:убер(?:ите|и)|удал(?:ите|и|ить)|прибери|видали|remove|delete)\b"
_REPLACE_ACTION = r"\b(?:замен(?:ите|и|ить)|замін(?:іть|и|ити)|replace|вместо|замість|instead\s+of)\b"
_UPDATE_ACTION = r"\b(?:измен(?:ите|и|ить)|змін(?:іть|и|ити)|change|сделай(?:те)?|зроби(?:ть)?)\b"
_SELECT_ACTION = r"\b(?:выбер(?:ите|и)|обер(?:іть|и)|select|choose)\b"
_MORE_ACTION = r"\b(?:ещ[её]\s+(?:одну|такую\s+же)|ще\s+(?:одну|таку\s+саму)|another\s+one|same\s+again)\b"
_NEW_ORDER_ACTION = r"\b(?:нов(?:ый|ое)\s+(?:заказ|замовлення)|нове\s+замовлення|new\s+order)\b"


def _affirmed_action(text, pattern):
    for match in re.finditer(pattern, _current_source_text(text), re.I):
        start, end = _preference_segment(text, match.start())
        if "?" not in text[start:end] and not _negated_action(text[start:match.start()]):
            return True
    return False


def is_neutral_commerce_source(text):
    """A finite literal acknowledgement, never an absence-of-parser heuristic."""
    return bool(re.fullmatch(r"\s*(?:(?:спасибо|дякую|thanks|thank\s+you)(?:\s+(?:большое|дуже|very\s+much))?|[👍🙏❤❤️])+[\s!.❤❤️👍🙏]*",
        str(text or ""), re.I))


def _historical_selectors(text, operations):
    if not any(operation.copy_previous for operation in operations):
        return {}
    current = _current_source_text(text)
    references = set(re.findall(r"\b(?:заказ(?:а|у)?|замовленн(?:я|і)|order)\s*(?:номер|number|№|#)?\s*((?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{0,39})(?!\w)", current, re.I))
    selectors, pending = _line_selector(current)
    if len(references) > 1 or pending:
        return {"pending_line_clarification": "ambiguous_line_target"}
    # A recipient after an explicit historical clause is an OLD selector.
    # Bare "same again for friend" instead changes the new recipient.
    old_clause = re.search(r"\b(?:из\s+заказа|з\s+замовлення|from\s+order)\b.*", current, re.I)
    recipient, recipient_pending = _recipient(old_clause.group() if old_clause else "")
    if recipient_pending:
        return {"pending_line_clarification": recipient_pending}
    return {"historical_order_reference": next(iter(references)) if references else "",
        "historical_line_id": selectors.get("target_line_id", ""),
        "historical_item_index": selectors.get("target_line_index"), "historical_recipient_id": recipient}


def _recipient(text):
    values = set()
    for match in re.finditer(r"\b(?:для|for)\s+(?:мого\s+|моего\s+|my\s+)?(\w+)\b", text.casefold()):
        if not _negated_action(text[:match.start()]):
            value = _RECIPIENT_WORDS.get(match.group(1))
            if value:
                values.add(value)
    return (next(iter(values)), "") if len(values) == 1 else ("", "ambiguous_recipient" if values else "")


def _garment_mentions(text):
    return sorted({(match.start(), match.end(), value)
        for needle, value in _GARMENT_WORDS.items()
        for match in re.finditer(_word_pattern(needle), text.casefold())})


def _line_selector(text, *, include_ordinals=True):
    """Describe source selectors; only the reducer may prove their uniqueness."""
    ids = set(re.findall(r"(?<!\w)line:\d{1,18}(?::[0-7])?(?![\w:])", text, re.I))
    indices = {int(match.group(1)) - 1 for match in re.finditer(
        r"\b(?:позици[яюи]|рядок|рядка|line|item)\s*#?\s*([1-9]\d?)\b", text, re.I)}
    ordinal_words = {"первую": 0, "первой": 0, "первую позицию": 0, "першу": 0, "first": 0,
        "вторую": 1, "второй": 1, "другу": 1, "second": 1,
        "третью": 2, "третю": 2, "third": 2}
    for word, index in ordinal_words.items():
        if not include_ordinals:
            break
        # "для друга/другу" describes a recipient, not an ordinal cart row.
        for match in re.finditer(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I):
            if not re.search(r"\b(?:для|for)\s*$", text[:match.start()], re.I):
                indices.add(index)
    if len(ids) > 1 or len(indices) > 1:
        return {}, "ambiguous_line_target"
    return {"target_line_id": next(iter(ids)).lower() if ids else "",
        "target_line_index": next(iter(indices)) if indices else None}, ""


def _quantity_requirement_text(text, title_texts):
    """Mask exact reviewed title occurrences only for quantity interpretation.

    The source identity owner supplies these texts from a reviewed catalog
    snapshot. Positions, source bytes and name resolution remain unchanged.
    """
    masked = list(text)
    for title in title_texts:
        if not isinstance(title, str) or not title:
            continue
        for spelling in {title, title.casefold().replace("ё", "е")}:
            for match in re.finditer(r"(?<!\w)" + re.escape(spelling) + r"(?!\w)", text, re.I):
                masked[match.start():match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def _line_quantity(text, *, action):
    patterns = [r"\b(?:количество|кількість|quantity)\s*(?:(?:of\s+)?(?:футболк\w*|худі|худи|hoodies?|t-shirts?)\s*)?(?:[:=]|на|to)?\s*(\d{1,3})(?!\w)",
        r"\b(?:количество|кількість|quantity)[^.!?;\n]{0,48}?(?:\bна\b|\bto\b|[:=])\s*(\d{1,3})(?!\w)",
        r"(?<!\w)(\d{1,3})\s*(?:шт\.?|штук\w*|pieces?|items?)\b"]
    if action in {"add", "update", "replace"}:
        patterns.append(r"(?<!\w)(\d{1,3})\s*(?:футболк\w*|худі|худи|hoodies?|t-shirts?)\b")
    values = {int(match.group(1)) for pattern in patterns for match in re.finditer(pattern, text, re.I)}
    if len(values) > 1 or any(value > 50 for value in values):
        return None, "ambiguous_line_quantity"
    return (next(iter(values)), "") if values else (None, "")


def _source_product_reference(value, *, media_evidence=None, parsed_catalog_graph=None):
    if parsed_catalog_graph is None:
        return resolve_product_reference(value, media_evidence=media_evidence)
    return resolve_product_reference(value, media_evidence=media_evidence, catalog_graph=parsed_catalog_graph)


def _line_operations(text, *, media_evidence=None, _source_clauses=None, _quantity_title_texts=(), parsed_catalog_graph=None):
    """Produce bounded source intents without guessing a canonical cart line."""
    current = _current_source_text(text)
    operations = []
    clauses = re.split(r"\.(?=\s|$)|[;!\n]+|\s+(?:и|і|and)\s+(?=" + _ADD_ACTION + "|" + _REMOVE_ACTION + "|" + _REPLACE_ACTION + ")", current, flags=re.I)
    for clause in clauses:
        if not clause.strip() or "?" in clause:
            continue
        lowered = clause.casefold().replace("ё", "е")
        more = _affirmed_action(clause, _MORE_ACTION)
        action_patterns = (("remove", _REMOVE_ACTION), ("replace", _REPLACE_ACTION),
            ("add", _ADD_ACTION), ("update", _UPDATE_ACTION))
        action = next((kind for kind, pattern in action_patterns if _affirmed_action(clause, pattern)), "")
        if not action and any(re.search(pattern, clause, re.I) for _kind, pattern in action_patterns):
            continue
        if (more or _affirmed_action(clause, _NEW_ORDER_ACTION)) and not action:
            action = "add"
        selector, selector_pending = _line_selector(clause)
        if not action and _affirmed_action(clause, _SELECT_ACTION) and (selector.get("target_line_id") or selector.get("target_line_index") is not None or selector_pending):
            action = "select"
        quantity_clause = _quantity_requirement_text(clause, _quantity_title_texts)
        explicit_quantity = bool(re.search(r"\b(?:количество|кількість|quantity)\b", quantity_clause, re.I))
        if explicit_quantity and not action and not _negated_action(lowered[:re.search(r"\b(?:количество|кількість|quantity)\b", lowered).start()]):
            action = "update"
        correction = re.search(r"\b(?:не|not)\s+(.+?),\s*(?:а|but)\s+(.+)", lowered)
        if correction and not action:
            old_garment = _find_prefix_value(correction.group(1), _GARMENT_WORDS)
            new_garment = _find_prefix_value(correction.group(2), _GARMENT_WORDS)
            action = "replace" if old_garment and new_garment and old_garment != new_garment else ("update" if new_garment else "")
            if action == "update":
                clause = correction.group(2)
                lowered = clause.casefold()
        if not action:
            if re.search(r"\b(?:другой|другую|інший|іншу|another|different)\s+(?:подарок|подарунок|gift)\b", lowered) and not _negated_action(lowered[:re.search(r"\b(?:другой|другую|інший|іншу|another|different)\b", lowered).start()]):
                return (), "ambiguous_line_target"
            continue
        mentions = _garment_mentions(clause)
        unique_garments = {value for _start, _end, value in mentions}
        target_garment = ""
        target_text = ""
        target_product_id = None
        config_text = clause
        garment = _find_prefix_value(clause, _GARMENT_WORDS)
        if action == "replace":
            separator = re.search(r"\b(?:на|with|for)\b", lowered)
            instead = re.search(r"\b(?:вместо|замість|instead\s+of)\b", lowered)
            if correction:
                old_text, config_text = correction.group(1), correction.group(2)
                target_text = old_text
                target_garment = _find_prefix_value(old_text, _GARMENT_WORDS)
                garment = _find_prefix_value(config_text, _GARMENT_WORDS)
            elif separator:
                target_text = clause[:separator.start()]
                target_garment = _find_prefix_value(target_text, _GARMENT_WORDS)
                config_text = clause[separator.end():]
                garment = _find_prefix_value(config_text, _GARMENT_WORDS)
            elif instead and len(mentions) == 2 and len(unique_garments) == 2:
                target_garment = mentions[0][2]
                target_text = clause[:mentions[0][1]]
                config_text = clause[mentions[0][1]:]
                garment = mentions[1][2]
            elif instead:
                references = tuple(re.finditer(r"https?://[^\s<>]+", clause))
                if references:
                    first = references[0]
                    before_reference = clause[instead.end():first.start()].strip(" ,:")
                    boundary = first.start() if before_reference else first.end()
                elif len(mentions) == 1:
                    first = mentions[0]
                    before_garment = clause[instead.end():first[0]].strip(" ,:")
                    boundary = first[0] if before_garment else first[1]
                else:
                    return (), "ambiguous_line_target"
                target_text, config_text = clause[:boundary], clause[boundary:]
                target_garment = _find_prefix_value(target_text, _GARMENT_WORDS)
                garment = _find_prefix_value(config_text, _GARMENT_WORDS)
            elif len(unique_garments) > 1:
                return (), "ambiguous_line_target"
            else:
                return (), "ambiguous_line_operation"
        elif len(unique_garments) > 1:
            return (), "ambiguous_line_target"
        elif action != "add":
            target_garment = garment
            target_text = clause
            if action == "update":
                separator = re.search(r"\b(?:на|to)\b", clause, re.I)
                if separator and _recipient(clause[:separator.start()])[0]:
                    target_text, config_text = clause[:separator.start()], clause[separator.end():]
        selector, selector_pending = _line_selector(target_text if action == "replace" else clause,
            include_ordinals=action != "add")
        if selector_pending:
            return (), selector_pending
        target_recipient, target_pending = _recipient(target_text) if action != "add" else ("", "")
        if target_pending:
            return (), target_pending
        recipient, pending = _recipient(config_text) if action in {"add", "replace"} or config_text != clause else ("", "")
        if pending:
            return (), pending
        fields = {key: value for key, value in (("color", _find_prefix_value(config_text, _COLOR_WORDS)),
            ("fit", _find_prefix_value(config_text, _FIT_WORDS)), ("size", _source_size(config_text))) if value}
        quantity, pending = _line_quantity(_quantity_requirement_text(config_text, _quantity_title_texts), action=action)
        if pending:
            return (), pending
        if explicit_quantity and quantity is None:
            return (), "ambiguous_line_quantity"
        if quantity is not None:
            if quantity == 0:
                if action not in {"update", "remove"}:
                    # A zero in an ADD/replacement clause cannot authorize
                    # deleting an unrelated existing position. Known title
                    # spans are masked by canonical source resolution first.
                    return (), "ambiguous_line_quantity"
                action = "remove"
            else:
                fields["quantity"] = quantity
        # "Change the first shirt for my friend" assigns a new recipient;
        # "change my friend's shirt to pink" selects an existing recipient.
        # Keep those coordinates separate, without choosing the active sibling.
        if action == "update" and config_text == clause and target_recipient and not fields:
            recipient, target_recipient = target_recipient, ""
        urls = re.finditer(r"https?://[^\s<>]+", config_text)
        product_ids = set()
        for match in urls:
            url = match.group()
            if not _is_negated_url(config_text, url, position=match.start()):
                reference = _source_product_reference(url, media_evidence=media_evidence, parsed_catalog_graph=parsed_catalog_graph)
                if reference.is_exact and reference.product_id:
                    product_ids.add(reference.product_id)
        if len(product_ids) > 1:
            return (), "multiple_product_links"
        product_id = next(iter(product_ids)) if product_ids else None
        target_ids = set()
        for match in re.finditer(r"https?://[^\s<>]+", target_text):
            reference = _source_product_reference(match.group(), media_evidence=media_evidence, parsed_catalog_graph=parsed_catalog_graph)
            if reference.is_exact and reference.product_id:
                target_ids.add(reference.product_id)
        if len(target_ids) > 1:
            return (), "ambiguous_line_target"
        target_product_id = next(iter(target_ids)) if target_ids else None
        if action in {"update", "remove", "select"}:
            target_product_id, product_id = target_product_id or product_id, None
        if action == "replace" and not (garment or product_id or fields) and not ((separator or instead) and target_text.strip() and config_text.strip()):
            return (), "ambiguous_line_operation"
        copy_previous = more and not (garment or product_id)
        if action == "add" and copy_previous:
            selector = {"target_line_id": "", "target_line_index": None}
        operations.append(CommerceLineOperation(operation=action, **selector,
            target_garment_type=target_garment, exact_product_id=product_id,
            field_updates=fields, garment_type=garment if action in {"add", "replace"} else "",
            recipient_id=recipient, copy_previous=copy_previous,
            target_product_id=target_product_id, target_recipient_id=target_recipient))
        if _source_clauses is not None:
            _source_clauses.append((target_text, config_text))
        if len(operations) > 8:
            return (), "ambiguous_line_operation"
    return tuple(operations), ""


def line_operation_source_clauses(text, *, _quantity_title_texts=(), parsed_catalog_graph=None):
    """The (old target, positive configuration) clauses in operation order.

    This uses the parser's own segmentation and rejection rules. A resolver
    must never zip global product mentions onto these operation positions.
    """
    clauses = []
    _operations, pending = _line_operations(text, _source_clauses=clauses,
        _quantity_title_texts=_quantity_title_texts, parsed_catalog_graph=parsed_catalog_graph)
    return () if pending else tuple(clauses)


def parse_turn(text: str | None, *, media_evidence=None, _quantity_title_texts=(), parsed_catalog_graph=None) -> CommerceTurnRequest:
    """Parse only bounded facts; never infer a payable product from free text."""
    raw = str(text or "").strip()
    current = _current_source_text(raw)
    lowered = current.casefold()
    urls = tuple(re.finditer(r"https?://[^\s<>]+", current))
    rejected_ids: list[int] = []
    exact_urls = []
    for match in urls:
        url = match.group()
        if _is_negated_url(current, url, position=match.start()):
            reference = _source_product_reference(url, media_evidence=media_evidence, parsed_catalog_graph=parsed_catalog_graph)
            if reference.product_id:
                rejected_ids.append(reference.product_id)
        else:
            exact_urls.append(url)

    reference = (
        _source_product_reference(" ".join(exact_urls), media_evidence=media_evidence, parsed_catalog_graph=parsed_catalog_graph)
        if exact_urls
        else None
    )
    pending = ""
    exact_product_id = None
    if len(exact_urls) > 1:
        exact_product_ids = {
            item.product_id
            for item in (
                _source_product_reference(url, media_evidence=media_evidence, parsed_catalog_graph=parsed_catalog_graph)
                for url in exact_urls
            )
            if item.is_exact and item.product_id
        }
        if len(exact_product_ids) > 1:
            pending = "multiple_product_links"
    elif reference and reference.is_exact:
        exact_product_id = reference.product_id

    field_updates: dict[str, str] = {}
    color = _find_prefix_value(raw, _COLOR_WORDS)
    fit = _find_prefix_value(raw, _FIT_WORDS)
    size = _source_size(raw)
    if color:
        field_updates["color"] = color
    if fit:
        field_updates["fit"] = fit
    if size:
        field_updates["size"] = size

    hard: dict[str, str] = {}
    if re.search(r"логотип\s+(?:спереди|спереду|на\s+груд|front)|logo\s+front", lowered):
        hard["front_decoration"] = "logo"
    if re.search(r"(?:без|without|no)\s+(?:принта|print)\s+(?:сзади|сзаду|на\s+спин|back)", lowered):
        hard["back_decoration"] = "none"
    if re.search(r"(?:без|without|no)\s+(?:принта|print)\b", lowered) and not hard:
        pending = pending or "print_placement"

    info_topics: list[str] = []
    if re.search(r"(?:размерн\w*\s+сетк|сітк\w*\s+розмір|size\s+guide|size\s+chart)", lowered):
        guide_fit = fit or ("oversize" if "оверсайз" in lowered or "oversize" in lowered else "")
        info_topics.append(f"size_guide:{guide_fit}" if guide_fit else "size_guide")
        field_updates.pop("fit", None)

    checkout_requested = bool(
        re.search(r"(?:оплатить|оформить|оформлюємо|оформити|pay|checkout|payment)", lowered)
    )
    # A generic phrase such as "у меня другой вопрос" is not product-change
    # evidence and must never clear the current selection. Require both a
    # change expression and an explicit commerce object.
    reset_requested = bool(
        _affirmed_action(current,
            r"(?:друг(?:ой|ую)|інш(?:ий|у)|another|different|сменить|заміни|replace|switch|change)"
            r"[^.!?]{0,32}(?:товар|футболк|худі|худи|принт|модел|колір|цвет|size|розмір|размер|product|shirt|t-shirt|hoodie|print|model|color)",
        )
        or _affirmed_action(current,
            r"(?:товар|футболк|худі|худи|принт|модел|колір|цвет|size|розмір|размер|product|shirt|t-shirt|hoodie|print|model|color)"
            r"[^.!?]{0,24}(?:сменить|змінити|замінити|replace|different|another|switch|change)",
        )
        or re.fullmatch(
            r"(?:хочу|(?:i\s+)?want)\s+(?:другую|іншу|another(?:\s+one)?)",
            lowered.strip(" .!?"),
        )
    )
    new_order_requested = _affirmed_action(current, _NEW_ORDER_ACTION)
    new_purchase_requested = _affirmed_action(current, _MORE_ACTION) or new_order_requested
    exchange_requested = _affirmed_action(current, r"(?:поменять|обмен|обмін|exchange|change\s+size)")
    personalized_fit_requested = bool(
        re.search(
            r"(?:який|какой|what)\s+(?:мені|мне)?\s*(?:розмір|размер|size)"
            r"[^.!?]{0,32}(?:підійде|подойдет|fit)"
            r"|(?:порадьте|посоветуйте|recommend)[^.!?]{0,24}(?:розмір|размер|size|посадк|fit)"
            r"|(?:розмір|размер|size)[^.!?]{0,32}(?:під|под|for)\s*(?:зріст|рост|height|ваг|вес|weight)",
            lowered,
        )
    )
    custom_print_requested = bool(
        re.search(
            r"(?:кастомн\w*\s+(?:друк|печать|print)|свій\s+принт|свой\s+принт|"
            r"my\s+(?:own\s+)?print|нанест\w*\s+(?:логотип|принт)|"
            r"надрукувати\s+(?:логотип|принт))",
            lowered,
        )
    )
    comparison_requested = bool(
        re.search(
            r"(?:порівня\w*|сравн\w*|compare|чим\s+відрізня|чем\s+отлича|"
            r"що\s+краще|что\s+лучше|which\s+is\s+better)",
            lowered,
        )
    )
    if reset_requested and not new_purchase_requested and not exchange_requested and not exact_product_id:
        pending = pending or "new_purchase_or_exchange"

    garment_type = _find_prefix_value(raw, _GARMENT_WORDS)
    recipient, recipient_pending = _recipient(current)
    withdrawals = {
        key: value
        for key, words in (("fit", _FIT_WORDS), ("color", _COLOR_WORDS), ("garment_type", _GARMENT_WORDS))
        if (value := _withdrawn_preference(raw, words))
    }
    if (withdrawn_size := _withdrawn_size(raw)):
        withdrawals["size"] = withdrawn_size

    line_operations, pending_line = _line_operations(raw, media_evidence=media_evidence,
        _quantity_title_texts=_quantity_title_texts, parsed_catalog_graph=parsed_catalog_graph)
    pending_line = pending_line or recipient_pending
    if pending == "multiple_product_links" and not line_operations:
        pending_line = pending_line or "multiple_product_links"
    if reset_requested and not line_operations and not exchange_requested:
        # An ambiguous "different one" cannot clear an entire cart. Preserve
        # the legacy question, while refusing an unbound destructive change.
        pending_line = pending_line or "ambiguous_line_operation"
    if line_operations or pending_line:
        # A typed operation owns its fields. Scalar copies would otherwise
        # silently update the active line before target resolution.
        field_updates, withdrawals, recipient, garment_type = {}, {}, "", ""
        reset_requested = False
        if line_operations and pending == "multiple_product_links":
            if exact_product_ids == {value for operation in line_operations for value in (operation.exact_product_id, operation.target_product_id) if value}:
                pending = ""
            else:
                line_operations = ()
                pending_line = "multiple_product_links"

    historical = _historical_selectors(raw, line_operations)
    pending_line = pending_line or historical.pop("pending_line_clarification", "")

    return CommerceTurnRequest(
        exact_product_id=exact_product_id,
        garment_type=garment_type,
        exact_unique_alias=False,
        field_updates=field_updates,
        preference_withdrawals=withdrawals,
        hard=hard,
        semantic_constraints=hard,
        exact_reference=reference,
        rejected_product_ids=tuple(sorted(set(rejected_ids))),
        pending_clarification=pending,
        info_topics=tuple(info_topics),
        checkout_requested=checkout_requested,
        purchase_requested=_source_purchase(raw) or new_purchase_requested,
        recipient_id=recipient,
        reset_requested=reset_requested,
        support_requested=bool(re.search(r"(?:помог|вопрос|support|help)", lowered)),
        new_purchase_requested=new_purchase_requested,
        exchange_requested=exchange_requested,
        new_order_requested=new_order_requested,
        **historical,
        line_operations=line_operations,
        pending_line_clarification=pending_line,
        personalized_fit_requested=personalized_fit_requested,
        custom_print_requested=custom_print_requested,
        comparison_requested=comparison_requested,
    )


def understand_turn(text: str | None, *, model_payload=None, media_evidence=None) -> CommerceTurnRequest:
    """Use model fields only as bounded hints; never accept model product IDs."""
    deterministic = parse_turn(text, media_evidence=media_evidence)
    if deterministic.line_operations or deterministic.pending_line_clarification:
        # Model hints cannot supply missing line mapping or reinterpret a
        # captured source operation as a scalar edit of the active row.
        return deterministic
    model = _parse_model_payload(model_payload)
    if model_payload is not None and not model:
        return CommerceTurnRequest(**{**deterministic.__dict__, "pending_clarification": deterministic.pending_clarification or "which_product"})
    updates = dict(deterministic.field_updates)
    words_by_key = {"color": _COLOR_WORDS, "fit": _FIT_WORDS, "garment_type": _GARMENT_WORDS}
    for key in ("color", "fit", "size", "garment_type"):
        if key not in updates and key in model:
            # Source abstention is authoritative, including text with no size.
            # A model may interpret a question but cannot manufacture a choice.
            if key == "size":
                continue
            if key in words_by_key and _has_unaffirmed_preference_mention(text or "", words_by_key[key]):
                continue
            updates[key] = model[key]
    return CommerceTurnRequest(
        **{
            **deterministic.__dict__,
            "field_updates": updates,
            "checkout_requested": deterministic.checkout_requested
            or bool(model.get("checkout_requested")),
        }
    )
