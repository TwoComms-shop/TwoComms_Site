"""Bounded multilingual turn parsing for the commerce state reducer."""

from __future__ import annotations

import re

from .ig_commerce_types import CommerceTurnRequest
from .ig_product_references import resolve_product_reference


_COLOR_WORDS = {
    "black": "black", "черн": "black", "чорн": "black",
    "white": "white", "бел": "white", "білий": "white",
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
_SIZE_RE = re.compile(r"\b(?:xxxs|xxl|xxxl|2xl|3xl|4xl|5xl|xs|s|m|l|xl)\b", re.I)


def _find_prefix_value(text: str, words: dict[str, str]) -> str:
    """Return one explicitly selected value, never just a mentioned one.

    Preference source evidence must survive a reparse, so negated choices,
    alternatives, questions, and quoted product copy deliberately abstain.
    """
    lowered = text.casefold().replace("ё", "е")
    candidates = [
        (match.start(), match.end(), value)
        for needle, value in words.items()
        for match in re.finditer(re.escape(needle), lowered)
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
    starts = [text.rfind(marker, 0, position) for marker in ".!?"]
    start = max(starts) + 1
    ends = [index for marker in ".!?" if (index := text.find(marker, position)) >= 0]
    # Keep the delimiter in the scope: otherwise a trailing "?" would be
    # omitted and a question such as "Оверсайз?" would look affirmative.
    return start, min(ends) + 1 if ends else len(text)


def _is_quoted_preference(text: str, start: int, end: int) -> bool:
    """Quotes often repeat card or product-description text rather than select it."""
    for match in re.finditer(r'"[^"\n]{0,240}"|«[^»\n]{0,240}»|“[^”\n]{0,240}”', text):
        if match.start() <= start and end <= match.end():
            return True
    return False


def _has_unaffirmed_preference_mention(text: str, words: dict[str, str]) -> bool:
    """Keep model hints from turning an abstained source mention into a choice."""
    lowered = text.casefold().replace("ё", "е")
    return any(needle in lowered for needle in words) and not _find_prefix_value(text, words)


def _is_negated_url(text: str, url: str) -> bool:
    before = text[: text.find(url)].casefold()
    return bool(re.search(r"(?:не|не хочу|не нужен|not|don't want)\s*$", before[-32:]))


def _withdrawn_preference(text: str, words: dict[str, str]) -> str:
    """Recognize a direct rejection of one value, not a negative description."""
    lowered = text.casefold().replace("ё", "е").replace("’", "'")
    values = set()
    for needle, value in words.items():
        for match in re.finditer(r"(?<!\w)" + re.escape(needle) + r"[\w-]*", lowered):
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


def parse_turn(text: str | None, *, media_evidence=None) -> CommerceTurnRequest:
    """Parse only bounded facts; never infer a payable product from free text."""
    raw = str(text or "").strip()
    lowered = raw.casefold()
    urls = re.findall(r"https?://[^\s<>]+", raw)
    rejected_ids: list[int] = []
    exact_urls = []
    for url in urls:
        if _is_negated_url(raw, url):
            reference = resolve_product_reference(url, media_evidence=media_evidence)
            if reference.product_id:
                rejected_ids.append(reference.product_id)
        else:
            exact_urls.append(url)

    reference = (
        resolve_product_reference(" ".join(exact_urls), media_evidence=media_evidence)
        if exact_urls
        else None
    )
    pending = ""
    exact_product_id = None
    if len(exact_urls) > 1:
        exact_product_ids = {
            item.product_id
            for item in (
                resolve_product_reference(url, media_evidence=media_evidence)
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
    size_match = _SIZE_RE.search(raw)
    if color:
        field_updates["color"] = color
    if fit:
        field_updates["fit"] = fit
    if size_match and not re.search(r"(?:розмірн\w*|размерн\w*|size\s+guide)", lowered):
        field_updates["size"] = size_match.group(0).upper()

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
        re.search(
            r"(?:друг(?:ой|ую)|інш(?:ий|у)|another|different|сменить|заміни|replace|switch|change)"
            r"[^.!?]{0,32}(?:товар|футболк|худі|худи|принт|модел|колір|цвет|size|розмір|размер|product|shirt|t-shirt|hoodie|print|model|color)",
            lowered,
        )
        or re.search(
            r"(?:товар|футболк|худі|худи|принт|модел|колір|цвет|size|розмір|размер|product|shirt|t-shirt|hoodie|print|model|color)"
            r"[^.!?]{0,24}(?:сменить|змінити|замінити|replace|different|another|switch|change)",
            lowered,
        )
        or re.fullmatch(
            r"(?:хочу|(?:i\s+)?want)\s+(?:другую|іншу|another(?:\s+one)?)",
            lowered.strip(" .!?"),
        )
    )
    new_purchase_requested = bool(
        re.search(r"(?:еще\s+одну|ще\s+одну|another\s+one|new\s+order)", lowered)
    )
    exchange_requested = bool(
        re.search(r"(?:поменять|обмен|обмін|exchange|change\s+size)", lowered)
    )
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
    withdrawals = {
        key: value
        for key, words in (("fit", _FIT_WORDS), ("color", _COLOR_WORDS), ("garment_type", _GARMENT_WORDS))
        if (value := _withdrawn_preference(raw, words))
    }

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
        reset_requested=reset_requested,
        support_requested=bool(re.search(r"(?:помог|вопрос|support|help)", lowered)),
        new_purchase_requested=new_purchase_requested,
        exchange_requested=exchange_requested,
        personalized_fit_requested=personalized_fit_requested,
        custom_print_requested=custom_print_requested,
        comparison_requested=comparison_requested,
    )


def understand_turn(text: str | None, *, model_payload=None, media_evidence=None) -> CommerceTurnRequest:
    """Use model fields only as bounded hints; never accept model product IDs."""
    deterministic = parse_turn(text, media_evidence=media_evidence)
    model = _parse_model_payload(model_payload)
    if model_payload is not None and not model:
        return CommerceTurnRequest(pending_clarification="which_product")
    updates = dict(deterministic.field_updates)
    words_by_key = {"color": _COLOR_WORDS, "fit": _FIT_WORDS, "garment_type": _GARMENT_WORDS}
    for key in ("color", "fit", "size", "garment_type"):
        if key not in updates and key in model:
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
