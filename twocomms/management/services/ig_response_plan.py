"""Bounded response contract over existing source choices and catalog authority.

No ledger, provider, or mutation lives here. Customer choice is deliberately
separate from applicable configuration and from any checkout effect.
"""
from __future__ import annotations
from copy import deepcopy

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation


CHOICE_FIELDS = ("product_id", "model_query", "size", "fit_option_code", "color", "quantity", "garment_type", "purchase_requested")
REVISION_PROVIDER_CONTROL_KINDS = frozenset({
    "product", "color_variant_id", "fit", "option", "size", "qty",
    "show_products", "catalog_link", "price_quoted", "paylink", "payment",
    "stage", "spam", "manager", "order",
})
_SELECTOR_WORDS = {
    "product": r"товар|модель|принт|product|model|print|посилання|ссылк|link",
    "size": r"розмір|размер|\bsizes?\b",
    "fit": r"посадк|крій|крой|фасон|fit",
    "color": r"колір|кольор|цвет|color|colour",
    "quantity": r"кільк|колич|quantity|how many",
    "option": r"варіант|вариант|опці|опци|option|variant",
}
CHOICE_ALIASES = {
    "coyote": ("coyote", "koyote", "койот", "коёт", "койота", "койотовий", "койотовый"),
    "oversize": ("oversize", "оверсайз"), "classic": ("classic", "regular", "класичн", "классическ"),
    "black": ("black", "чорний", "чорна", "чорне", "чорну", "чорні", "чорного", "чорної",
              "чёрный", "чёрная", "чёрное", "чёрную", "чёрные", "чёрного", "чёрной",
              "черный", "черная", "черное", "черную", "черные", "черного", "черной"),
    "white": ("white", "білий", "біла", "біле", "білу", "білі", "білого", "білої",
              "белый", "белая", "белое", "белую", "белые", "белого", "белой"),
    "blue": ("blue", "синій", "синя", "синє", "синю", "сині", "синього", "синьої",
             "синий", "синяя", "синее", "синюю", "синие", "синего", "синей"),
    "pink": ("pink", "рожевий", "рожева", "рожеве", "рожеву", "рожеві", "рожевого", "рожевої",
             "розовый", "розовая", "розовое", "розовую", "розовые", "розового", "розовой"),
    "grey": ("grey", "gray", "сірий", "сіра", "сіре", "сіру", "сірі", "сірого", "сірої",
             "серый", "серая", "серое", "серую", "серые", "серого", "серой"),
    "green": ("green", "зелений", "зелена", "зелене", "зелену", "зелені", "зеленого", "зеленої",
              "зелёный", "зелёная", "зелёное", "зелёную", "зелёные", "зелёного", "зелёной",
              "зеленый", "зеленая", "зеленое", "зеленую", "зеленые", "зеленого", "зеленой"),
    "tshirt": ("tshirt", "t-shirt", "футболка", "футболку"),
    "hoodie": ("hoodie", "худі", "худи"),
}

def _additional_color_choice_aliases():
    # Finite adjective forms for the remaining source-parser color families.
    # These are acknowledgements of a wish, never catalogue/physical authority.
    ru = ("ый", "ая", "ое", "ые", "ую", "ого", "ой", "ых", "ыми", "ым", "ом", "ому")
    uk = ("ий", "а", "е", "і", "у", "ого", "ої", "их", "ими", "им", "ому", "ою", "ій")
    families = {
        "black": (("чорн", uk), ("черн", ru), ("чёрн", ru)),
        "white": (("біл", uk), ("бел", ru)),
        "blue": (("син", ("ій", "я", "є", "і", "ю", "ього", "ьої", "іх", "іми", "ім", "ьому", "ьою",
                         "ий", "яя", "ее", "ие", "юю", "его", "ей", "их", "ими", "им", "ем", "ему")),),
        "pink": (("рожев", uk), ("розов", ru)),
        "grey": (("сір", uk), ("сер", ru)),
        "green": (("зелен", ru + uk), ("зелён", ru)),
        "coyote": (("койотов", ru + uk),),
        "olive": (("оливков", ru + uk),),
        "purple": (("фиолетов", ru), ("фіолетов", uk)),
        "beige": (("бежев", ru + uk),),
        "burgundy": (("бордо", ("",)), ("бордов", ru + uk + ("ій",))),
        "orange": (("оранжев", ru + uk), ("помаранчев", uk)),
        "red": (("красн", ru), ("червон", uk)),
        "yellow": (("желт", ru), ("жёлт", ru), ("жовт", uk)),
        "brown": (("коричнев", ru + uk),),
    }
    return {color: tuple(dict.fromkeys((*CHOICE_ALIASES.get(color, ()), color, *(("violet",) if color == "purple" else ()),
        *(root + suffix for root, suffixes in roots for suffix in suffixes))))
        for color, roots in families.items()}


CHOICE_ALIASES.update(_additional_color_choice_aliases())

_NEGATED_CHOICE_RE = re.compile(
    r"\b(?:не|ні|not|no|never|(?:do|does|did|has|have|had|is|are|was|were|ca|wo|could|would|should|must|need)n['’]t)\b", re.I,
)
_PAYMENT_WORDS = re.compile(r"\b(?:оплат\w*|сплат\w*|сплач\w*|передоплат\w*|платіж\w*|плат[её]ж\w*|paid|payment|pay|чек\w*|квитанц\w*|receipt\w*|реквізит\w*|реквизит\w*|iban)\b", re.I)
_RECEIPT_WORDS = re.compile(r"\b(?:чек\w*|квитанц\w*|receipt\w*)\b", re.I)
_PAYMENT_CLAIM = re.compile(r"\b(?:оплатив|оплатила|оплатил|оплатили|сплатив|сплатила|сплачено|оплачено|paid)\b", re.I)
_PAYMENT_PENDING = re.compile(r"перевір\w*|провер\w*|звір\w*|свер\w*|очіку\w*|ожида\w*|pending|under\s+review|(?:not|isn['’]t)\s+(?:yet\s+)?(?:verified|confirmed)|не\s+(?:підтвердж\w*|подтвержд\w*)", re.I)
_RECEIPT_ACK = re.compile(r"(?:отрим\w*|одерж\w*|получ\w*|бач\w*|виж\w*|received|see|got)[^.!?\n]{0,45}(?:чек\w*|квитанц\w*|receipt\w*)|(?:чек\w*|квитанц\w*|receipt\w*)[^.!?\n]{0,45}(?:отрим\w*|получ\w*|received|under\s+review|перевір\w*|провер\w*)", re.I)
_REPORTED_PAYMENT_ACK = re.compile(r"(?:повідом\w*|сообщ\w*|заяв\w*|reported|say|said)[^.!?\n]{0,60}(?:оплат\w*|сплат\w*|paid|payment)|(?:заяв\w*|reported)\s+(?:оплат\w*|payment)", re.I)
_REPORTED_RECEIPT_ACK = re.compile(r"(?:повідом\w*|сообщ\w*|заяв\w*|reported|say|said)[^.!?\n]{0,60}(?:чек\w*|квитанц\w*|receipt\w*)", re.I)
_PAYMENT_FORWARDED = re.compile(r"(?:передал\w*|передав\w*|передан\w*|передано|сповіст\w*|уведомил\w*|уведомлен\w*|forwarded|notified)[^.!?\n]{0,70}(?:менедж\w*|команд\w*|manager|team)|(?:менедж\w*|manager|team)[^.!?\n]{0,45}(?:уведомлен\w*|notified|сповіщ\w*)", re.I)
_REPLY_LANGUAGE_REQUEST = re.compile(
    r"\b(?:do\s+you|can\s+you|please|could\s+you)\s+(?:speak|reply|answer|write|use|continue)[^.!?\n]{0,30}(?:english|language)"
    r"|\b(?:i\s+)?(?:don['’]?t|do\s+not|can['’]?t|cannot)\s+(?:speak|understand|read)[^.!?\n]{0,35}(?:language|ukrainian|russian)"
    r"|(?:не\s+(?:розумію|понимаю)|відповідайте|отвечайте)[^.!?\n]{0,25}(?:мов|язык|англ)", re.I)


def _payment_requests(text):
    """Current source semantics; questions/negations never prove a payment."""
    text = _unquoted_text(text)
    result = set()
    for clause in re.findall(r"[^,.!?;\n]+[?]?", text):
        if not _PAYMENT_WORDS.search(clause):
            continue
        asking = "?" in clause or bool(re.search(r"\b(?:як|как|де|где|чи|ли|how|where|when|what|can|could|підкаж\w*|подскаж\w*)\b", clause, re.I))
        negated = bool(_NEGATED_CHOICE_RE.search(clause))
        instructions = bool(re.search(r"посилан\w*|ссыл\w*|лінк\w*|link|реквізит\w*|реквизит\w*|iban|як\s+оплат|как\s+оплат|how\s+(?:do\s+i\s+)?pay|надішл\w*|пришл\w*|send", clause, re.I))
        asking = asking or bool(instructions and re.search(r"\b(?:дайт\w*|дайте|надішл\w*|пришл\w*|send|provide|please)\b", clause, re.I))
        future = bool(re.search(r"\b(?:буду|збира\w*|собира\w*|will|going\s+to)\b", clause, re.I))
        if not asking and not negated and not future:
            if _RECEIPT_WORDS.search(clause) and not instructions:
                result.add("payment:receipt")
            elif _PAYMENT_CLAIM.search(clause) and not instructions:
                result.add("payment:claim")
        if asking or negated:
            result.add("info:payment_instructions" if instructions else "info:payment")
    return result


def _captured_payment_observation(raw, current_sources):
    """Keep only admitted observations of exact current USER sources, no PII."""
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("reason"):
        return {}
    observation = raw.get("observation", raw)
    if not isinstance(observation, dict) or observation.get("state") not in {"observed", "pending"}:
        return {}
    current_ids = {row["message_id"] for row in current_sources}
    refs = raw.get("source_refs") or []
    proven_ids = {ref.get("message_id", ref.get("id")) for ref in refs if isinstance(ref, dict)
                  and re.fullmatch(r"[a-f0-9]{64}", str(ref.get("source_digest", ref.get("digest", ""))))}
    ids = sorted(identity for identity in observation.get("source_message_ids") or []
                 if type(identity) is int and identity in current_ids and identity in proven_ids)
    if not ids:
        return {}
    receipts = []
    for row in (observation.get("receipts") or [])[:8]:
        if not isinstance(row, dict) or row.get("source_message_id") not in ids or row.get("role") != "receipt" or row.get("state") != "inspected":
            continue
        if not row.get("source_part_id") or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("content_hash") or "")):
            continue
        # Only document type/transfer outcome go into the plan, never banking PII.
        facts = row.get("receipt_facts") or {}
        transfer = facts.get("payment_status")
        receipts.append({"source_message_id": row["source_message_id"], "source_part_id": str(row["source_part_id"])[:64],
            "content_hash": row["content_hash"], "document_type": "receipt",
            "reported_transfer_status": transfer if transfer in {"completed", "pending", "failed", "unknown"} else "unknown"})
    return {"state": "observed" if receipts else "pending", "source_message_ids": ids,
        "receipts": receipts, "payment_verified": False, "verification": "unresolved"}


def _payment_covered(kind, response, plan, source_id):
    # Final truth/action validation still owns authority. Coverage cannot turn
    # a keyword, size ack, manager control or OCR into proof of money/forwarding.
    if kind == "info:payment_instructions":
        return bool(response.control.get("paylink"))
    text = _unquoted_text(response.reply_text)
    from management.services.ig_reply_truth import _claim_sentences, _has_positive_claim, _locally_negated
    if plan.authority["payment_confirmed"]:
        from management.services.ig_reply_truth import _PAYMENT_CLAIM_RE
        if any("?" not in clause and _has_positive_claim(_PAYMENT_CLAIM_RE, clause)
               for clause in _claim_sentences(text, ())):
            return True
    sentences = [clause for clause in re.findall(r"[^.!?\n]+[?]?", text) if "?" not in clause]
    pending = any(_PAYMENT_WORDS.search(clause) and any(not _locally_negated(clause, match.start())
        for match in _PAYMENT_PENDING.finditer(clause)) for clause in sentences)
    receipt_ack = any(_has_positive_claim(_RECEIPT_ACK, clause) for clause in sentences)
    claim_ack = any(_has_positive_claim(_REPORTED_PAYMENT_ACK, clause) for clause in sentences)
    receipt_report_ack = any(_has_positive_claim(_REPORTED_RECEIPT_ACK, clause) for clause in sentences)
    source_receipt = source_id in plan.payment_observation.get("source_message_ids", [])
    if kind == "payment:receipt":
        return (receipt_ack if source_receipt else receipt_report_ack) and pending
    if kind == "payment:claim":
        return (claim_ack or (receipt_ack and source_receipt)) and pending
    return pending and bool(plan.payment_observation or plan.payment_claim_source_ids)


def _unquoted_text(text):
    return re.sub(
        r'«[^»]*»|“[^”]*”|"[^"\n]*"|‘(?:[^’\n]|(?<=\w)’(?=\w))*’|'
        r'(?<!\w)\x27(?:[^\x27\n]|(?<=\w)\x27(?=\w))+\x27(?!\w)',
        "", str(text or ""),
    )


def _acknowledges_choice(kind, value, text, authority, *, audited=False):
    from management.services.ig_reply_truth import (
        _claim_sentences, _CUSTOMER_CHOICE_RE, _locally_negated,
        CORRECTED_REQUIREMENT_RE, _comma_choice_continuation,
    )
    aliases = CHOICE_ALIASES.get(value, (value,))
    unquoted = _unquoted_text(text)
    for sentence in _claim_sentences(unquoted, ()):
        cursor = 0
        for clause in re.split(r"[,;]|\b(?:але|но|but)\b", sentence, flags=re.I):
            clause_start = sentence.find(clause, cursor)
            clause_end = clause_start + len(clause)
            cursor = clause_end
            choice = _CUSTOMER_CHOICE_RE.search(clause)
            for alias in aliases:
                match = re.search(r"(?<!\w)" + re.escape(alias) +
                    (r"\w*" if alias.endswith(("ичн", "ческ")) else r"(?!\w)"), clause, re.I)
                if not match or _locally_negated(clause, match.start()):
                    continue
                if _NEGATED_CHOICE_RE.search(clause[:match.end()]):
                    continue
                if audited and CORRECTED_REQUIREMENT_RE.search(clause):
                    return True
                if not audited and ((choice and not _locally_negated(clause, choice.start()))
                    or _comma_choice_continuation(sentence, clause_start, clause_end)):
                    return True
                canonical = authority.get({"size": "sizes", "fit_option_code": "fits", "color": "colors"}.get(kind, ""), ())
                if value.casefold() in {str(item).casefold() for item in canonical} and re.search(
                    r"наявн|налич|доступн|available|in\s+stock|маємо|we\s+have", clause, re.I):
                    return True
    return False


_PRODUCT_NAME_QUOTES = re.compile(
    r'"([^"\n]{1,240})"|«([^»\n]{1,240})»|“([^”\n]{1,240})”|‘([^’\n]{1,240})’|'
    r"(?<!\w)'((?:[^'\n]|(?<=\w)'(?=\w)){1,240})'(?!\w)")
_PRODUCT_GARMENT_NOUN = r'(?:t[- ]?shirts?|hoodies?|sweatshirts?|футболк\w*|худі|худи|світшот\w*|свитшот\w*)'


def _acknowledges_product(title, text):
    from management.services.ig_reply_truth import _CUSTOMER_CHOICE_RE, _claim_sentences
    # A canonical label may include its garment noun around one literal name.
    # Accept that captured inner identity only as a quoted named garment, not
    # as an unquoted color/style keyword or a manufactured translation.
    embedded = list(_PRODUCT_NAME_QUOTES.finditer(title))
    if len(embedded) == 1:
        name = next(value for value in embedded[0].groups() if value is not None)
        literal = r'["\'«“‘]' + re.escape(name) + r'["\'»”’]'
        garment = _PRODUCT_GARMENT_NOUN
        if (re.search(literal + r'\s*' + garment + r'\b|\b' + garment + r'\s*' + literal, text, re.I)
            and _acknowledges_product(name, text)):
            return True
    # Remove whole reported utterances before splitting sentences, so an
    # opening quote in an earlier sentence cannot be lost. Only the exact
    # selected title can be unwrapped inside an otherwise own acknowledgement.
    identity = re.sub(r'["\'«“‘]' + re.escape(title) + r'["\'»”’]',
        lambda _match: title, text, flags=re.I)
    for sentence in _claim_sentences(_unquoted_text(identity), ()):
        if title.casefold() not in sentence.casefold():
            continue
        if _NEGATED_CHOICE_RE.search(sentence):
            continue
        if re.search(r'\b(?:customer|client|manager|bot|assistant|клієнт\w*|клиент\w*|менеджер\w*|бот\w*|асистент\w*|помічниц\w*|помощниц\w*)\s+(?:wrote|said|asked|напис\w*|сказ\w*|запит\w*|спрос\w*)\b', sentence, re.I):
            continue
        if _CUSTOMER_CHOICE_RE.search(sentence):
            return True
        if re.match(r"(?:для\s+(?:моделі|модели)|for\s+)", sentence, re.I):
            return True
    return False


def _named_selected_product_conflict(plan, text):
    """Reject an own named-garment substitution for one fresh proven SKU.

    Titles are identity only. Generic facts need no repeated name; ambiguous
    carts, comparisons and reported/negated utterances grant no new identity.
    """
    product_id = plan.configuration.get("product_id")
    proof = plan.evidence.get("product_id") or {}
    source_id = proof.get("source_message_id")
    title = str(plan.configuration.get("product_title") or "")
    if (plan.line_plans or type(product_id) is not int or product_id <= 0
        or plan.choices.get("product_id") != product_id or type(source_id) is not int or source_id <= 0
        or not title or len(text) > 64000 or not any(row["kind"] == "product_id"
            and row.get("source_message_id") == source_id for row in plan.obligations)):
        return False
    from management.services.ig_reply_truth import _CUSTOMER_CHOICE_RE, _claim_sentences

    quoted = _PRODUCT_NAME_QUOTES
    garment = _PRODUCT_GARMENT_NOUN
    normalize = lambda value: " ".join(str(value).casefold().split())
    allowed = {normalize(title)}
    allowed.update(normalize(next(value for value in match.groups() if value is not None))
        for match in quoted.finditer(title))
    names = {}

    def mask(match):
        name = next(value for value in match.groups() if value is not None)
        if (len(names) >= 16 or not (re.match(r"\s*" + garment + r"\b", text[match.end():], re.I)
            or re.search(r"\b" + garment + r"\s*$", text[:match.start()], re.I))):
            return match.group(0)
        marker = f"twc_named_garment_{len(names)}"
        names[marker] = name
        return marker

    own = _unquoted_text(quoted.sub(mask, text))
    clauses = (clause for sentence in _claim_sentences(own, ())
        for clause in re.split(r"[,;]|\b(?:але|но|but|while|whereas)\b", sentence, flags=re.I))
    for clause in clauses:
        for marker in re.finditer(r"\btwc_named_garment_\d+\b", clause):
            name = names.get(marker.group(0))
            if name is None or normalize(name) in allowed:
                continue
            choices = list(_CUSTOMER_CHOICE_RE.finditer(clause[:marker.start()]))
            if not choices:
                continue
            choice = choices[-1]
            between = clause[choice.end():marker.start()]
            if (re.search(r"\btwc_named_garment_\d+\b|\b(?:could|may|might|would)\b", between, re.I)
                or (choice.group(0).casefold() in {"noted", "recorded"}
                    and not re.match(r"\s+your\s+(?:selection|choice|preference)\b", between, re.I))):
                continue
            # The named garment must be the selected object. A fit/size or
            # comparison/example bridge does not select the example's print.
            bridge = re.sub(r"^\s+your\s+(?:selection|choice|preference)\b", "", between, flags=re.I)
            if not re.fullmatch(r"[\s:=–—-]*(?:(?:of|for|the|a|an|цю|цей|це|эту|этот|эта|" + garment
                + r")\b[\s:=–—-]*)*", bridge, re.I):
                continue
            rendered = re.sub(r"\btwc_named_garment_\d+\b",
                lambda found: '"' + names[found.group(0)] + '"' if found.group(0) in names else found.group(0), clause)
            if _acknowledges_product(name, rendered):
                return True
    return False


def asks_next_selector(text, selector):
    """A selector must occur in the same interrogative clause as its request."""
    if selector == "recipient":
        from management.services.ig_response_cart_plan import cart_recipient_question
        return any(cart_recipient_question({"clarification": True}, question)
            for question in re.findall(r"[^.!?;\n]*\?", _unquoted_text(text)))
    pattern = _SELECTOR_WORDS.get(str(selector).split(":", 1)[0])
    if not pattern:
        return False
    text = _unquoted_text(text)
    for clause in re.findall(r"[^.!?;\n]*\?", text):
        if re.search(pattern, clause, re.I) and re.search(
            r"який|яку|яка|яке|які|котр|какой|какую|какое|какие|котор|which|what|how\s+many|"
            r"обира|обер|выб|choose|want|бажає|хочете|хотите|підкаж|уточн|скаж|please", clause, re.I):
            return True
    return False


def _catalog_inquiry_context(sources):
    """Retain sealed source identities for the narrow photo inquiry contract."""
    from types import SimpleNamespace
    from management.services.ig_turn_intent import _product_interest_signal, _NEGATED_ORDER, _customer_text
    from management.services.instagram_bot import _has_meaningful_media_caption

    interests = []
    media = []
    for source in sources:
        signal = _product_interest_signal(source.get("text"))
        if signal is False or _NEGATED_ORDER.search(_customer_text(source.get("text"))):
            interests = []
        elif signal is True:
            interests.append(source["message_id"])
        parts = source.get("media_parts") or []
        if parts:
            media.append({"source_message_id": source["message_id"],
                "source_digest": source.get("source_digest") or "",
                "bare": not _has_meaningful_media_caption(SimpleNamespace(text=source.get("text") or "")),
                "parts": [{key: deepcopy(part.get(key)) for key in
                    ("source_part_id", "original_index", "content_hash", "mime", "capture_outcome")} for part in parts]})
    return {"interest_source_ids": interests, "media_sources": media} if interests and media else {}


def _catalog_inquiry_coverage(plan, response, evidence):
    """A selected catalog match covers only its exact, understood photo source.

    Neither a candidate alone nor source text/current selection supplies image
    understanding. The bound request must prove every sealed part of the source.
    """
    from management.services.ig_media_analysis import MediaAnalysisError, validate_bound_media_analysis

    context = plan.catalog_inquiry
    if not context or not isinstance(evidence, dict) or plan.line_plans:
        return set(), set()
    # Catalog candidates currently have no per-image relation. A global match
    # can answer one reference photo; it cannot identify two distinct garments
    # or discharge an unrelated receipt/document sibling.
    media_sources = context.get("media_sources") or []
    if len(media_sources) != 1 or len(media_sources[0].get("parts") or []) != 1:
        return set(), set()
    artifact, binding = evidence.get("artifact"), evidence.get("binding")
    if not isinstance(artifact, dict) or not isinstance(binding, dict):
        return set(), set()
    request = artifact.get("media_request") or {}
    if (not isinstance(request, dict) or not isinstance(binding.get("items"), list)
        or len(binding["items"]) != 1 or binding.get("actual_inline_count") != 1):
        return set(), set()
    # Revision collector manifests use sealed_capture_outcome; the existing
    # analysis validator consumes the equivalent owned-part projection.
    normalized_binding = {**binding, "items": [{**row, "capture_state": row.get("capture_state") or
        ("owned" if row.get("sealed_capture_outcome") == "owned" else "unavailable")}
        for row in binding["items"] if isinstance(row, dict)]}
    try:
        analysis = validate_bound_media_analysis(artifact.get("media_analysis"), media=normalized_binding,
            request_id=request.get("request_id"), provider_model=request.get("provider_model"),
            legacy_images=artifact.get("image_observations") or ())
    except (MediaAnalysisError, TypeError, ValueError):
        return set(), set()
    selected = str(response.control.get("product") or "")
    candidates = artifact.get("catalog_candidates") or []
    if (not selected or artifact.get("catalog_resolution") != "auto_select"
        or str(artifact.get("auto_product_id") or "") != selected or not any(isinstance(row, dict) and str(row.get("product_id") or "") == selected
            and type(row.get("confidence")) in {int, float} and row["confidence"] >= .9 for row in candidates)):
        return set(), set()
    rows = analysis["parts"]
    covered_media = set()
    for source in context["media_sources"]:
        parts = source["parts"]
        if not parts:
            continue
        matched = []
        for expected in parts:
            matches = [row for row in rows if row.get("source_message_id") == source["source_message_id"]
                and all(row.get(key) == expected.get(key) for key in
                    ("source_part_id", "original_index", "content_hash", "mime"))]
            if (expected.get("capture_outcome") != "owned" or len(matches) != 1
                or not str(expected.get("mime") or "").startswith("image/")):
                break
            row = matches[0]
            if (row.get("outcome") != "understood" or row.get("origin") not in
                    {"provider_observation", "legacy_observation"}):
                break
            if row["origin"] == "legacy_observation":
                product = any(isinstance(observation, dict)
                    and observation.get("source_image_index") == row.get("source_inline_index")
                    and observation.get("source_part_id") == row["source_part_id"]
                    and observation.get("content_hash") == row["content_hash"]
                    and observation.get("outcome") == "understood" and observation.get("type_code") == "product"
                    for observation in artifact.get("image_observations") or ())
            else:
                product = row.get("content_kind") == "product_photo"
            if not product:
                break
            matched.append(row)
        if len(matched) == len(parts):
            covered_media.add(source["source_message_id"])
    if not covered_media:
        return set(), set()
    # A factual garment identification answers this question without claiming
    # the customer already chose it. Financial/catalog truth runs separately.
    from management.services.ig_reply_truth import _locally_negated
    garment = str(plan.choices.get("garment_type") or "")
    mentioned = any(not _locally_negated(clause, match.start())
        for clause in re.split(r"[.!?;\n]+", _unquoted_text(response.reply_text))
        for alias in CHOICE_ALIASES.get(garment, ())
        for match in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", clause, re.I))
    if not mentioned:
        return set(), set()
    bare_ids = {source["source_message_id"] for source in context["media_sources"] if source["bare"]}
    return set(context["interest_source_ids"]), covered_media & bare_ids


def _chart_absence_described(reply_text, fit):
    aliases = r"classic|regular|класич\w*|классич\w*" if fit == "classic" else r"oversize|оверсайз\w*" if fit == "oversize" else r"(?!)"
    return any(re.search(aliases, clause, re.I) and re.search(
        r"немає|відсутн\w*|недоступн\w*|нет\b|unavailable|missing|not\s+(?:available|yet)", clause, re.I)
        and re.search(r"сіт\w*|сет\w*|таблиц\w*|charts?|guides?", clause, re.I)
        for clause in re.split(r"[.!?;\n]+", _unquoted_text(reply_text)))


def _native_chart_absence_coverage(plan, evidence, reply_text):
    from management.services.ig_product_inquiry_presentation import validate_fit_illustration_metadata
    try:
        effect = evidence.get("native_fit_illustration") if isinstance(evidence, dict) else None
        expected = plan.configuration.get("native_chart_scope")
        if not isinstance(effect, dict) or not isinstance(expected, dict):
            return set()
        metadata = effect["projection_metadata"]
        if metadata.get("delivery") != "native" or not validate_fit_illustration_metadata(metadata,
                payload=effect["payload"], group=effect["group"], kind=effect["kind"]):
            return set()
        manifest = metadata["fit_illustration"]
        scope = manifest["scope"]
        product_id = manifest["focus"].get("product_id")
        focus_matches = manifest["focus"] == plan.product_focus or (
            type(product_id) is int and product_id > 0 and plan.configuration.get("product_id") == product_id
            and plan.product_focus.get("status") != "focused")
        if (manifest["revision_id"] != expected["revision_id"] or not focus_matches
            or any(scope.get(key) != expected[key] for key in
                ("client_id", "client_epoch", "namespace", "revision_snapshot_digest"))):
            return set()
        fits = {fit for fit in manifest.get("unavailable_chart_fits", ()) if _chart_absence_described(reply_text, fit)}
        line_ids = {line.line_id for line in plan.line_plans if line.as_dict()["configuration"].get("product_id") == product_id}
        return {row["id"] for row in plan.obligations if fits and row["kind"] == "info:size_chart"
            and row["source_message_id"] in expected["source_message_ids"]
            and (not row.get("line_id") or row["line_id"] in line_ids)
            and (not row.get("fit_code") or row["fit_code"] in fits)}
    except (AttributeError, KeyError, TypeError, ValueError):
        return set()


def _native_chart_coverage(plan, evidence, reply_text=""):
    """Credit authenticated attachments or proved, explicitly explained absence.

    Outbox ownership still proves delivery; this grants no selection authority.
    """
    absence = _native_chart_absence_coverage(plan, evidence, reply_text)
    if isinstance(evidence, dict) and "native_size_charts" in evidence:
        effects = evidence["native_size_charts"]
        if not isinstance(effects, (list, tuple)) or len(effects) > 2:
            return set()
        return absence.union(*(_native_chart_coverage(plan, {"native_size_chart": effect}, reply_text) for effect in effects))
    from management.services.ig_catalog_choice_presentations import validate_catalog_chart_metadata

    try:
        effect = evidence.get("native_size_chart") if isinstance(evidence, dict) else None
        expected = plan.configuration.get("native_chart_scope")
        if not isinstance(effect, dict) or not isinstance(expected, dict):
            return absence
        metadata = effect["projection_metadata"]
        if not validate_catalog_chart_metadata(metadata, payload=effect["payload"], group=effect["group"], kind=effect["kind"]):
            return set()
        chart = metadata["catalog_chart"]
        scope = chart["scope"]
        if (chart["invitation_id"] != expected["revision_id"]
            or any(scope.get(key) != expected[key] for key in
                ("client_id", "client_epoch", "namespace", "revision_snapshot_digest", "source_message_ids"))
            or type(chart.get("grid_id")) is not int or chart["grid_id"] <= 0):
            return set()
        product_id = chart.get("product_id")
        presentation = chart.get("presentation_context") or {}
        focus = plan.product_focus
        canonical = plan.configuration.get("product_id") == product_id and bool(product_id)
        discussed = (focus.get("status") == "focused" and focus.get("product_id") == product_id
            and (presentation.get("product_focus") or {}) == focus
            and presentation.get("schema") in {"discussed-product-configuration.v1", "discussed-fit-comparison.v1"}
            and presentation.get("proof_digest")
            and presentation.get("fit_code") == chart["configuration_selector"].get("fit_code")
            and (presentation.get("scope") or {}).get("revision_id") == expected["revision_id"]
            and presentation["scope"].get("snapshot_digest") == expected["revision_snapshot_digest"])
        if not (canonical or discussed):
            return set()
        satisfied_fits = {chart["configuration_selector"].get("fit_code")}
        for missing in chart.get("unavailable_requested_fits", ()):
            if _chart_absence_described(reply_text, missing):
                satisfied_fits.add(missing)
        return absence | {row["id"] for row in plan.obligations if row["kind"] == "info:size_chart"
            and row["source_message_id"] in expected["source_message_ids"]
            and (not row.get("fit_code") or row["fit_code"] in satisfied_fits)
            and (not row.get("line_id") or row["line_id"] == scope.get("line_id"))}
    except (AttributeError, KeyError, TypeError, ValueError):
        return set()


def _requested_topics(text, parsed_topics=()):
    """Retain information requests independently from a choice in that source."""
    from management.services.ig_reply_truth import _locally_negated
    from management.services.ig_product_inquiries import _garment_weight_requested, _FABRIC_WEIGHT
    topics = {"info:" + ("size_chart" if str(topic).split(":", 1)[0] == "size_guide"
        else str(topic).split(":", 1)[0]) for topic in parsed_topics}
    for clause in re.findall(r"[^.!?\n]+[?]?", _unquoted_text(text)):
        from management.services.ig_pending_question_context import status_request_source_ids
        status_request = bool(status_request_source_ids([{"message_id": 1, "role": "user", "text": clause}]))
        language_request = bool(_REPLY_LANGUAGE_REQUEST.search(clause))
        if status_request:
            topics.add("info:manager_status")
        if language_request:
            topics.add("info:reply_language")
        from management.services.ig_fit_consultation import fit_consultation_request

        fit_request = fit_consultation_request([{"role": "user", "text": clause}])
        chart = re.search(r"таблиц\w*\s+р[ао]зм[іе]р\w*|(?:розмірн\w*|размерн\w*)\s+(?:сіт\w*|сет\w*)|size\s+(?:chart|guide)|(?:обидв\w*|обе|both)\s+(?:сіт\w*|сет\w*|charts?|guides?)", clause, re.I)
        chart_request = bool(chart and not _locally_negated(clause, chart.start()) and re.search(
            r"\?|покаж\w*|надішл\w*|пришл\w*|скин\w*|дайте|send|show|please|можна|можно|can|could|потріб\w*|нуж\w*", clause, re.I))
        if chart_request:
            topics.add("info:size_chart")
        presentation = bool(re.search(
            r"(?:покаж\w*|надішл\w*|пришл\w*|show|send)[^.!?\n]{0,30}(?:фото|photos?|pictures?|images?)"
            r"|^\s*(?:фото|photos?|pictures?|images?)\s*\?\s*$", clause, re.I))
        if presentation and not fit_request and not chart_request:
            topics.add("info:presentation")
        if fit_request:
            if fit_request.get("fit_explanation_requested"):
                topics.add("info:fit")
            if fit_request.get("photos_requested"):
                topics.add("info:fit_presentation")
            if fit_request.get("audience_requested"):
                topics.add("info:audience")
        markers = list(re.finditer(
            r"\b(?:скільки|сколько|яка|який|які|какой|какая|какие|як|как|коли|когда|where|when|how|what|which|чи|ли|tell)\b"
            r"|\b(?:підкаж\w*|подскаж\w*|уточн\w*|check|clarify)", clause, re.I))
        # An entire short topic (possibly after a size choice) is an ordinary
        # elliptical request. A noun inside a title, description or statement
        # is not: keep the full-match boundary instead of accepting any mention.
        nominal_request = bool(re.fullmatch(
            r"\s*(?:(?:(?:розмір|размер|size)\s+)?(?:[2-5]?x{0,3}[sml]|[хx]{1,3}[лl])(?!\w)\s*[,;]?\s*)?"
            r"(?:(?:а|і|и|and|also)\s+)?(?:состав|склад|матеріал|материал|тканина|ткань|"
            r"ціна|цена|вартість|price|cost|composition|fabric|material|догляд|уход|care)"
            r"(?:\s*[,;]?\s+(?:будь\s+ласка|пожалуйста|please))?\s*", clause, re.I))
        asking = nominal_request or ("?" in clause and not markers) or any(
            not _locally_negated(clause, marker.start()) and not any(
                negation.end() == len(clause[:marker.start()].rstrip())
                for negation in _NEGATED_CHOICE_RE.finditer(clause[:marker.start()].rstrip()))
            for marker in markers)
        if not asking:
            continue
        matched = presentation or bool(fit_request) or chart_request or status_request or language_request
        if _FABRIC_WEIGHT.search(clause):
            topics.add("info:density")
            matched = True
        for topic, pattern in (
            ("material", r"матеріал|материал|тканин\w*|ткан[ьи]\b|fabric|material|склад\b|состав|composition|cotton|бавов|хлоп"),
            ("density", r"щільн|плотност|density|gsm|г/м"),
            ("weight", r"ваг(?:а|и|у|ою)\b|важ(?:ить|ать)\b|вес(?:а|у|ом|ит|ят)?\b|\bweigh(?:t|s|ing)?\b"),
            ("care", r"прат|стир|догляд|уход|wash|care"),
            ("quality", r"якіст|качеств|quality|тріска|треска|crack"),
            ("payment", _PAYMENT_WORDS.pattern),
            ("price", r"ціна|ціну|вартість|кошту|цена|цену|стоим|стоит|price|cost|how\s+much"),
            ("dispatch_timing", r"відправ|отправ|dispatch|ship(?:ped|ping)?\b"),
            ("shipping", r"достав|посилк|посылк|отрима|получу|deliver|arrival"),
            ("recruitment", r"ваканс|праців|работ\w*|робот\w*|staff|job|hiring"),
            ("service", r"власн\w*\s+принт|св(?:о[йяю]|ій)\w*\s+(?:принт|дизайн)|custom|own\s+design|сервіс|сервис|service"),
        ):
            topic_matches = list(re.finditer(pattern, clause, re.I))
            def refused(match):
                boundary = r"[,;]|\b(?:а|але|но|but)\b"
                local = (re.split(boundary, clause[:match.start()], flags=re.I)[-1]
                    + match.group() + re.split(boundary, clause[match.end():], flags=re.I)[0])
                return bool(re.search(r"\bне\s+(?:цікав\w*|интерес\w*|важлив\w*|важн\w*|хочу\s+(?:знати|знать))"
                    r"|\b(?:not\s+interested|(?:do\s+not|don't)\s+want\s+to\s+know)\b", local, re.I))
            if topic_matches and all(refused(match) for match in topic_matches):
                continue
            if topic == "price":
                price_matches = list(re.finditer(pattern, clause, re.I))
                if price_matches and all(_locally_negated(clause, match.start()) or re.search(
                    r"\b(?:не|not)\s+(?:цікав\w*|интерес\w*|interested(?:\s+in)?)\s*$",
                    re.split(r"[,;]|\b(?:а|але|но|but)\b", clause[:match.start()], flags=re.I)[-1], re.I)
                    for match in price_matches):
                    continue
            if re.search(pattern, clause, re.I) and (topic != "weight" or _garment_weight_requested(clause)):
                topics.add("info:" + topic)
                matched = True
        if "?" in clause and not matched:
            topics.add("info:question")
    return sorted(topics)


def _manual_delivery_purchase_handled(plan, response):
    """Handle this purchase request by its mandatory delivery intent, not an offer.

    The source-owned fact also requires the existing native manager action
    before transport. This credit grants no payment/shipment facts and leaves
    every independent information or selection obligation with its own owner.
    """
    requirement = plan.configuration.get("manual_delivery_requirement") or {}
    dimensions = requirement.get("dimensions") or []
    if not dimensions or response.control.get("paylink") or response.control.get("payment"):
        return False
    from management.services.ig_reply_truth import _locally_negated
    future = re.compile(r"\b(?:уточню|уточнимо|уточним|узгоджу|узгодимо|погоджу|погодимо|согласую|согласуем|передам|передамо|попрошу|"
        r"(?:i|we)['’]ll\s+(?:check|ask|arrange)|(?:i|we)\s+will\s+(?:check|ask|arrange))\b", re.I)
    carrier_words = {"ukr": r"укрпошт\w*|укрпочт\w*|ukrposhta", "meest": r"meest|міст\s+експрес\w*|мист\s+экспресс\w*",
        "pickup": r"самовив\w*|самовывоз\w*|pick\s*up", "other": r"інш\w*\s+(?:служб\w*|перевізник\w*)|друг\w*\s+(?:служб\w*|перевозчик\w*)|another\s+carrier"}
    handled = set()
    for clause in re.findall(r"[^.!?\n]+[?]?", _unquoted_text(response.reply_text)):
        action = future.search(clause)
        review = bool(action and not _locally_negated(clause, action.start())
            and re.search(r"менеджер\w*|manager|команд\w*|team", clause, re.I))
        single_question = bool("?" in clause and re.search(r"\b(?:чи|мож\w*|чи\s+мож\w*|can|could|would)\b", clause, re.I)
            and re.search(r"\b(?:одн\w*|один|одну|one|single)\s+(?:адрес\w*|посилк\w*|посылк\w*|address|parcel|shipment)\b", clause, re.I)
            and not re.search(r"\b(?:не|not|no)\b", clause, re.I))
        for index, dimension in enumerate(dimensions):
            if dimension.get("kind") == "multiple_destinations":
                distinct = re.search(r"\b(?:дві|двох|двом\w*|двум\w*|два|две|2|різн\w*|разн\w*|окрем\w*|two|both|different|separate|multiple)\s+"
                    r"(?:(?:різн\w*|разн\w*|different|separate)\s+)?(?:адрес\w*|міст\w*|город\w*|addresses|cities|destinations?)\b", clause, re.I)
                if single_question or (review and distinct):
                    handled.add(index)
            elif dimension.get("kind") == "carrier" and review:
                pattern = carrier_words.get(dimension.get("value"))
                if pattern and re.search(pattern, clause, re.I):
                    handled.add(index)
    return len(handled) == len(dimensions)


def _fulfillment_question_purchase_handled(plan, response):
    from management.services.ig_fulfillment_question_debt import question_purchase_handled
    return question_purchase_handled(plan.configuration.get("fulfillment_question_debt"), response)


_PERSONAL_PURCHASE = re.compile(
    r"\b(?:для\s+(?:себе|себя)|(?:і|и)\s+собі|особисто\s+(?:хочу|мені)|"
    r"лично\s+(?:хочу|мне)|for\s+myself|for\s+my\s+own\s+use)\b", re.I)
_BUSINESS_RETAIL_TOPICS = {"info:price", "info:shipping", "info:dispatch_timing", "info:question"}


def _business_source_partition(sources):
    """Separate an evidenced trade proposal from explicit personal questions.

    Only sources which independently contain the current business proposal
    qualify. The native brief's aggregate evidence IDs alone cannot convert an
    unrelated retail source into a business question. Full source digests stay
    in the contract; the original sources and commerce state are untouched.
    """
    from management.services.ig_business_cooperation import (
        _TOPICS, business_cooperation_for_sources, current_cooperation_sources,
    )
    brief = business_cooperation_for_sources(sources)
    if not brief:
        return {}, list(sources)
    current = {row["message_id"]: row for row in current_cooperation_sources(sources)
        if type(row.get("message_id")) is int}
    rows, retail = [], []
    for source in sources:
        own = current.get(source.get("message_id"))
        if own is None:
            retail.append(source)
            continue
        text = str(own.get("text") or "")
        personal = _PERSONAL_PURCHASE.search(text)
        trade, suffix = (text[:personal.start()], text[personal.start():]) if personal else (text, "")
        if personal and not business_cooperation_for_sources([{**own, "text": trade}]):
            # Personal purchase can precede the proposal in the same source.
            # A new independently evidenced trade clause starts its own scope.
            reentry = next((part.start() for part in re.finditer(r"[^.!?\n]+", text)
                if part.start() > personal.end()
                and business_cooperation_for_sources([{**own, "text": part.group()}])), None)
            if reentry is not None:
                trade, suffix = text[reentry:], text[:reentry]
        trade_brief = business_cooperation_for_sources([{**own, "text": trade}])
        topics = trade_brief.get("requested_topics") or []
        if not trade_brief or not topics:
            retail.append(source)
            continue
        # Material, care, language, payment and other independent questions
        # survive even inside the proposal's source. An unmatched question is
        # also debt; a known trade topic cannot cover arbitrary source text.
        residual = []
        for clause in re.findall(r"[^.!?\n]+[.!?]?", trade):
            requested = set(_requested_topics(clause))
            if requested - _BUSINESS_RETAIL_TOPICS or (
                "info:question" in requested and not any(pattern.search(clause) for _, pattern in _TOPICS)):
                residual.append(clause)
        row = {"source_message_id": source["message_id"],
            "source_digest": source.get("source_digest") or hashlib.sha256(str(source.get("text") or "").encode()).hexdigest(),
            "topics": topics}
        if "catalog_options" in topics:
            row["catalog_facets"] = [name for name, pattern in (
                ("sizes", r"розмір\w*|размер\w*|\bsizes?\b"),
                ("colors", r"кольор\w*|колір\w*|цвет\w*|\bcolou?rs?\b")) if re.search(pattern, trade, re.I)]
        rows.append(row)
        retail.append({**source, "text": ". ".join([*residual, suffix]).strip(" .")})
    if not rows:
        return {}, list(sources)
    return {"version": 1, "case_kind": "collaboration_review", "decision_owner": "manager",
        "evidence_message_ids": [row["source_message_id"] for row in rows], "sources": rows}, retail


def _business_obligations(context):
    return [{"id": f"{row['source_message_id']}:business:{topic}", "kind": "business:" + topic,
        "source_message_id": row["source_message_id"]}
        for row in context.get("sources", ()) for topic in row["topics"]]


def _personal_choice_fields(source):
    from management.services.ig_commerce_turns import parse_turn
    parsed = parse_turn(str(source.get("text") or ""))
    fields = {"fit_option_code" if key == "fit" else "quantity" if key == "qty" else key
        for key in parsed.field_updates}
    if parsed.exact_product_id:
        fields.add("product_id")
    if parsed.garment_type:
        fields.add("garment_type")
    if parsed.purchase_requested:
        fields.add("purchase_requested")
    return fields


def _business_topic_covered(kind, response, context, source_id, cooperation_policy=None):
    """A trade acknowledgement keeps the actual management decision pending."""
    from management.services.ig_business_cooperation import _TOPICS
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_reply_truth import _locally_negated
    from management.services.ig_cooperation_policy import public_statement_topic
    from management.services.ig_reply_truth import _claim_sentences
    topic = kind.removeprefix("business:")
    row = next((row for row in context.get("sources", ())
        if row["source_message_id"] == source_id and topic in row["topics"]), None)
    if row is not None and any(public_statement_topic(sentence, cooperation_policy) == topic
            for sentence in _claim_sentences(response.reply_text, ())):
        return True
    if (not response.control.get("manager") or context.get("case_kind") != "collaboration_review"
            or _business_approval_reason(response, context, cooperation_policy)):
        return False
    row = next((row for row in context.get("sources", ())
        if row["source_message_id"] == source_id and topic in row["topics"]), None)
    pattern = dict(_TOPICS).get(topic)
    if row is None or pattern is None:
        return False
    for clause in re.split(r"[.!?\n]+", own_reply_text(response.reply_text)):
        owner = re.search(r"менедж\w*|команд\w*|адміністрац\w*|администрац\w*|manager\w*|team|administration", clause, re.I)
        action = re.search(r"переда\w*|передам|передано|уточн\w*|перевір\w*|провер\w*|розгля\w*|рассмотр\w*|forward\w*|pass\w*|review\w*|check\w*", clause, re.I)
        whole_brief = re.search(
            r"(?:ваш\w*|ус[іи]|вс[еи]|your|all)[^.!?\n]{0,25}(?:питан\w*|вопрос\w*|questions?|proposal|запит\w*)"
            r"\s+(?:щодо|про|по|о|about|regarding|on)\s+(?:співпрац\w*|сотруднич\w*|cooperat\w*|partnership|business)", clause, re.I)
        review = owner or re.search(r"на\s+(?:розгляд|рассмотрен\w*)|for\s+review", clause, re.I)
        if whole_brief and action and review and not _locally_negated(clause, action.start()):
            return True
        if not owner or not action or _locally_negated(clause, action.start()) or not pattern.search(clause):
            continue
        if topic == "catalog_options" and any(not re.search(
            r"розмір\w*|размер\w*|\bsizes?\b" if facet == "sizes" else r"кольор\w*|колір\w*|цвет\w*|\bcolou?rs?\b",
            clause, re.I) for facet in row.get("catalog_facets", ())):
            continue
        return True
    return False


def _business_approval_reason(response, context, cooperation_policy=None):
    """A captured trade request contains no approval of individual terms."""
    if not context:
        return ""
    from management.services.ig_business_cooperation import _TOPICS
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_reply_truth import _claim_sentences, _locally_negated
    approval = re.compile(
        r"\b(?:confirmed|approved|agreed|authori[sz]ed|permitted|allowed)\b|"
        r"\bпідтверджен(?:о|а|ий|і)\b|підтверди\w*|\bподтвержден[оыа]\b|подтвердил\w*|"
        r"погоджено|узгоджено|согласовано|затверджено|утверждено|"
        r"дозвол(?:ено|яємо|яється)|разреш(?:ено|аем|ается)|"
        r"(?:можете|можна|можно|you\s+(?:may|can))\s+(?:використов\w*|использов\w*|reuse|use|resell|sell)|"
        r"\b(?:may|can)\s+be\s+(?:re)?used\b", re.I)
    trade_scope = re.compile(r"співпрац\w*|сотруднич\w*|партнер\w*|партнёр\w*|оптов\w*|гуртов\w*|"
        r"wholesale|business|\bpartners?\b|partnership|cooperat\w*|resell\w*|dropship\w*", re.I)
    personal_partner = re.compile(r"\bfor\s+(?:your|my|his|her|their|our)\s+partner\b", re.I)
    trade_money = re.compile(r"(?<!\w)(?:(?:грн|₴|uah|usd|eur|gbp|pln|\$|€|£)\s*)?"
        r"\d{1,9}(?:[.,]\d{1,2})?\s*(?:%|відсот\w*|процент\w*|percent\w*|грн|₴|uah|usd|eur|gbp|pln)?(?!\w)", re.I)
    for sentence in _claim_sentences(own_reply_text(response.reply_text), ()):
        from management.services.ig_cooperation_policy import public_statement_topic, is_reviewed_public_statement
        if public_statement_topic(sentence, cooperation_policy):
            continue
        if is_reviewed_public_statement(sentence):
            return "response_plan_business_terms_unconfirmed"
        from management.services.ig_cooperation_policy import unreviewed_business_entitlement
        if unreviewed_business_entitlement(sentence):
            return "response_plan_business_terms_unconfirmed"
        if (re.search(r"повернен\w*|возврат\w*|refund\w*", sentence, re.I)
            and re.search(r"\d\s*%", sentence)):
            return "response_plan_business_terms_unconfirmed"
        for clause in re.split(r"[;\n]|\b(?:але|но|but)\b", sentence, flags=re.I):
            # Ordinary selected-product prices/sizes keep their own native
            # truth authority. Only trade terms or photo-use rights belong to
            # this unapproved business decision.
            rights = dict(_TOPICS)["photo_usage"].search(clause) and re.search(
                r"використов\w*|использов\w*|use|usage|rights?|дозвол\w*|разреш\w*", clause, re.I)
            if "?" in clause or not (trade_scope.search(personal_partner.sub(" ", clause)) or rights):
                continue
            # Retail catalog money proves no partner price/discount. Bind
            # both the trade label and pricing term to this local segment;
            # a neighbouring ordinary retail quote or handoff is independent.
            part_cursor = 0
            for part in re.split(r"(?<!\d),|,(?!\d)|\b(?:and|та|і|й|и)\b", clause, flags=re.I):
                part_offset = clause.find(part, part_cursor)
                part_cursor = part_offset + len(part)
                if not trade_scope.search(personal_partner.sub(" ", part)) or not dict(_TOPICS)["partner_price"].search(part):
                    continue
                for amount in trade_money.finditer(part):
                    # Own supplied item counts and monthly sales are not a
                    # monetary quote. A currency/percent still stays guarded.
                    suffix = part[amount.end():]
                    if (not re.search(r"%|грн|₴|uah|usd|eur|\$|€|відсот|процент|percent", amount.group(), re.I)
                        and re.match(r"\s*(?:худі|худи|футбол\w*|реч\w*|штук\w*|hoodies?|shirts?|t-shirts?|garments?|items?|продаж\w*|sales)\b", suffix, re.I)):
                        continue
                    # An explicit check of a proposed price is a question,
                    # whereas a later coordinated price assertion is not.
                    before = clause[:part_offset + amount.start()]
                    checks = list(re.finditer(r"(?:уточн\w*|перевір\w*|провер\w*|check\w*)[^.!?\n]{0,50}(?:чи|ли|whether|if)\b", before, re.I))
                    if checks and not re.search(r",|\b(?:and|а|і|й)\b", before[checks[-1].end():], re.I):
                        continue
                    return "response_plan_business_terms_unconfirmed"
            for match in approval.finditer(clause):
                if _locally_negated(clause, match.start()):
                    continue
                if re.search(r"(?:не\s+(?:маю|отрим\w*|получ\w*)|"
                    r"(?:do\s+not|don['’]t|haven['’]t|have\s+not)\s+(?:have|received|got)?)\s+(?:an?\s+)?$",
                    clause[:match.start()], re.I):
                    continue
                # A whether/чи clause scopes only its own question, not a
                # later coordinated assertion of the same permission.
                checks = list(re.finditer(r"(?:уточн\w*|перевір\w*|провер\w*|check\w*)[^.!?\n]{0,50}(?:чи|ли|whether|if)\b",
                    clause[:match.start()], re.I))
                if checks and not re.search(r",|\b(?:and|а|і|й)\b", clause[checks[-1].end():match.start()], re.I):
                    continue
                return "response_plan_business_terms_unconfirmed"
    return ""


def _expand_chart_obligations(rows, source, *, supported_fits=()):
    """A comparison requests each chart; one attachment cannot cover both."""
    from management.services.ig_fit_consultation import requested_chart_fits
    fits = requested_chart_fits([source], supported_fits=supported_fits)
    if len(fits) < 2:
        return rows
    result = []
    for row in rows:
        if row["kind"] == "info:size_chart" and not row.get("fit_code"):
            result.extend({**row, "id": row["id"] + ":" + fit, "fit_code": fit} for fit in fits)
        else:
            result.append(row)
    return result


def _capture_pending_stock_questions(client, revision, plan):
    """Only a current native review proves that this exact stock ask is queued."""
    from management.services.ig_response_cart_plan import stock_question
    cart = plan.source_cart_capture
    lines = cart.get("lines") or []
    if len(lines) != 1 or cart.get("active_line_id") != lines[0].get("line_id"):
        return {}
    selected = {key: value.get("value") for key, value in lines[0].get("fields", {}).items()}
    ids = {row["source_message_id"] for row in plan.obligations
        if row["kind"] in {"info:question", "info:price"}}
    candidates = [row for row in revision.bundle_snapshot.get("sources", ())
        if row.get("message_id") in ids and stock_question(row.get("text"), size=selected.get("size"),
            color=selected.get("color"), title=plan.configuration.get("product_title", ""))]
    if not candidates:
        return {}
    from management.services.ig_inventory_availability import capture_inventory_availability
    inventory = capture_inventory_availability(client.pk, current_source_cart=cart)
    positive = next((row for row in inventory.get("lines") or []
        if row.get("line_id") == lines[0]["line_id"] and row.get("status") == "available"), None)
    if positive:
        return {"mode": "inventory_available", "inventory_capture": inventory,
            "source_message_ids": [row["message_id"] for row in candidates], "line_id": lines[0]["line_id"],
            "choices": selected, "title": plan.configuration.get("product_title", ""),
            "readiness": {"product": deepcopy(plan.readiness_snapshot.get("product") or {})}}
    from management.services.ig_fulfillment_reviews import fulfillment_review_projection
    from django.utils import timezone
    projection = fulfillment_review_projection(client.pk, now=timezone.now(), current_source_cart=cart)
    request = projection.get("current_request") or {}
    targets = request.get("lines") or []
    current = [row["message_id"] for row in candidates]
    if (projection.get("status") != "pending" or projection.get("decision_recorded")
        or projection.get("reason") or not projection.get("can_decide")
        or request.get("scope") != "cart" or request.get("client_id") != client.pk
        or len(targets) != 1 or targets[0].get("target", {}).get("product_id") != selected.get("product_id")
        or not current):
        return {}
    return {"request_id": request["id"], "request_digest": projection["request_digest"],
        "source_message_ids": current, "line_id": lines[0]["line_id"], "choices": selected,
        "title": plan.configuration.get("product_title", ""),
        "readiness": {"product": deepcopy(plan.readiness_snapshot.get("product") or {})}}


def _pending_stock_answer_covered(plan, text, source_message_id):
    captured = plan.pending_stock_questions
    if source_message_id not in captured.get("source_message_ids", ()):
        return False
    if captured.get("mode") == "inventory_available":
        from management.services.ig_reply_authority import apply_inventory_availability_context
        from management.services.ig_reply_truth import ReplyTruthContext, _claim_sentences, _manager_availability_matches, _product_availability_claim
        from management.services.ig_reply_language import own_reply_text
        context = apply_inventory_availability_context(ReplyTruthContext(), captured.get("inventory_capture"))
        return any(_product_availability_claim(clause) and any(fact.line_id == captured["line_id"] for fact in _manager_availability_matches(clause, context))
            for clause in _claim_sentences(own_reply_text(text), ()))
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_reply_truth import (_claim_sentences, _has_positive_claim,
        _source_owned_availability_check, _pending_manager_stock_check)
    from management.services.ig_response_cart_plan import _scoped_request_tokens, selected_title_text
    from management.services.approved_public_facts import _captured_base, _GARMENT_WORDS
    check = re.compile(r"\b(?:уточню|уточнимо|перевірю|перевіримо|проверю|проверим)\b|"
        r"\b(?:i|we)\s+(?:(?:will|shall)\s+(?:also\s+)?(?:check|verify)|(?:am|are)\s+(?:checking|verifying))\b", re.I)
    manager = re.compile(r"менедж\w*|\bmanager\b", re.I)
    title = str(captured.get("title") or "")
    identity = selected_title_text(text, title)
    # A quoted foreign named object or reported answer cannot inherit this
    # purchase's source choices after its quote delimiters are removed.
    if _PRODUCT_NAME_QUOTES.search(identity):
        return False
    for clause in _claim_sentences(own_reply_text(identity), ()):
        # This protected capture owns exactly this product label. Shared
        # grammar must never guess labels from capitalization or swallow an
        # unknown predicate as though it were a model name.
        semantic = re.sub(r"(?<!\w)" + re.escape(title) + r"(?!\w)", ' ', clause, flags=re.I) if title else clause
        pending_check = _pending_manager_stock_check(semantic)
        if ("?" in clause or (not pending_check and (not _source_owned_availability_check(clause)
            or not manager.search(clause) or not _has_positive_claim(check, clause)))):
            continue
        literal = title and re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", clause, re.I)
        garment = _captured_base(captured.get("readiness"), "")[1]
        if garment and any(key != garment and pattern.search(clause) for key, pattern in _GARMENT_WORDS.items()):
            continue
        if (not literal and any(pattern.search(clause) for pattern in _GARMENT_WORDS.values())
            and not _captured_base(captured.get("readiness"), clause)[1]):
            continue
        if pending_check:
            subject = pending_check["subject"]
            subject = re.sub(r"\b(?:наявн(?:ість|ості)|налич(?:ие|ия|ию)|доступн(?:ість|ості|ость|ости))\b", "availability", subject, flags=re.I)
            subject = re.sub(r"\b(?:розмір(?:у|і|ом)?|размер(?:а|е|ом)?)\b", "size", subject, flags=re.I)
            subject = re.sub(r"\b(?:на\s+склад[іе]|at\s+(?:the\s+)?warehouse)\b", "", subject, flags=re.I)
            subject = re.sub(r"\b(?:саме|именно|exactly)\b", "", subject, flags=re.I)
            if _scoped_request_tokens(subject, choices=captured["choices"], title=title):
                return True
        if _scoped_request_tokens(clause, choices=captured["choices"], title=title, extra_words=(
            "i", "we", "will", "shall", "am", "also", "check", "checking", "verify", "verifying",
            "with", "our", "manager", "уточню", "уточнимо", "перевірю", "перевіримо", "проверю", "проверим",
            "менеджера", "менеджером", "менеджеру", "у", "з", "с", "для", "вас", "наразі", "зараз")):
            return True
    return False


def _superseded_color_wish_covered(plan, response, source_message_id, *, current_garment=False):
    """A sole old wish is covered only by its proven, acknowledged replacement."""
    from management.services.ig_product_inquiries import _digest, validate_inquiry_response, inquiry_reply
    from management.services.ig_reply_language import own_reply_text
    captured = plan.product_information
    witnesses = captured.get("superseded_color_wishes")
    if (not isinstance(witnesses, list) or not 1 <= len(witnesses) <= 16
        or captured.get("digest") != _digest({key: value for key, value in captured.items()
            if key not in {"digest", "snapshot_digest"}})
        or validate_inquiry_response(captured, response)):
        return False
    sources = captured.get("source_refs")
    if (not isinstance(sources, list) or len(sources) > 32 or any(not isinstance(row, dict)
        or type(row.get("message_id")) is not int
        or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("source_digest") or "")) for row in sources)):
        return False
    refs = {row["message_id"]: row["source_digest"] for row in sources}
    text = own_reply_text(response.reply_text)
    server_fallback = response.reply_text.strip() == inquiry_reply(captured).strip()
    if re.search(r"\b(?:customer|client|manager|bot|assistant|клієнт\w*|клиент\w*|менеджер\w*)\s+(?:said|wrote|напис\w*|сказ\w*)\b", text, re.I):
        return False
    for row in witnesses:
        if (not isinstance(row, dict) or row.get("axis") != "color"
            or row.get("previous_source_sole_color_wish") is not True
            or row.get("current_source_message_id" if current_garment else "previous_source_message_id") != source_message_id
            or type(row.get("previous_source_message_id")) is not int
            or row.get("previous_source_digest") != refs.get(row["previous_source_message_id"])
            or type(row.get("current_source_message_id")) is not int
            or row["current_source_message_id"] <= row["previous_source_message_id"]
            or row.get("current_source_digest") != refs.get(row["current_source_message_id"])):
            continue
        if (type(row.get("product_id")) is not int or row["product_id"] <= 0
            or not isinstance(row.get("line_id"), str) or not 0 < len(row["line_id"]) <= 80
            or any(not isinstance(row.get(key), str) or not 0 < len(row[key]) <= 80
                for key in ("previous_value", "current_value"))):
            continue
        matched = any(all(inquiry.get(key) == value for key, value in {
            "axis": "color", "line_id": row.get("line_id"), "product_id": row.get("product_id"),
            "source_message_id": row["current_source_message_id"], "source_digest": row["current_source_digest"],
            "requested_value": row.get("current_value")}.items()) for inquiry in captured.get("inquiries", ()))
        commitment = _current_color_check_commitment(plan, response.reply_text, row["current_value"]) if matched else False
        if matched and (server_fallback or commitment or (not current_garment
                and _acknowledges_choice("color", row["current_value"], text, plan.authority))):
            return True
    return False


def _current_color_check_commitment(plan, text, value):
    """A current exact color wish may be repeated in an own manager check."""
    from management.services.ig_response_cart_plan import selected_title_text, _scoped_request_tokens
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_reply_truth import _claim_sentences, _has_positive_claim
    identity = selected_title_text(text, value)
    check = re.compile(r"\b(?:уточню|уточнимо|перевірю|перевіримо|проверю|проверим)\b|"
        r"\b(?:i|we)\s+will\s+(?:check|verify)\b", re.I)
    for clause in _claim_sentences(own_reply_text(identity), ()):
        if ("?" in clause or not re.search(r"менедж\w*|\bmanager\b", clause, re.I)
            or not re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", clause, re.I)
            or not _has_positive_claim(check, clause)):
            continue
        if _scoped_request_tokens(clause, choices={"color": value},
                title=plan.configuration.get("product_title", ""), extra_words=(
                    "уточню", "уточнимо", "перевірю", "перевіримо", "проверю", "проверим",
                    "i", "we", "will", "check", "verify", "with", "our", "manager", "менеджера", "менеджеру", "менеджером",
                    "можливість", "виконати", "ваше", "ваш", "побажання", "запит", "ці", "параметри", "для", "цього",
                    "колір", "кольору", "кольорі", "цвет", "цвета", "цвете", "color", "colour", "your", "request", "possibility", "fulfil", "fulfill")):
            return True
    return False


def _approved_base_wish_anchors(plan, response):
    """Bind an own published base answer to its exact captured pending wish.

    This grants only clause ownership and garment acknowledgement. The native
    inquiry receipt, catalogue and fulfillment gates retain their authority.
    """
    from management.services.approved_public_facts import approved_assertion_valid, _GARMENT_WORDS
    from management.services.ig_product_inquiries import _digest, cart_identity, validate_inquiry_response
    from management.services.ig_reply_language import own_reply_text
    from management.services.ig_reply_truth import approved_product_assertion_covered, _has_positive_claim
    from management.services.ig_response_cart_plan import scoped_claim_clauses
    captured = plan.product_information
    if (plan.plan_gap or len(plan.line_plans) != 1 or captured.get("status") != "manager_required"
        or plan.source_cart_capture.get("status") != "captured"
        or plan.source_cart_capture.get("coverage_complete") is not True
        or captured.get("reason") != "product_configuration_review"
        or captured.get("digest") != _digest({key: value for key, value in captured.items()
            if key not in {"digest", "snapshot_digest"}})
        or captured.get("cart_identity") != cart_identity(plan.source_cart_capture)
        or validate_inquiry_response(captured, response)):
        return ()
    inquiries = captured.get("inquiries") or ()
    refs = captured.get("source_refs") or ()
    if (len(inquiries) != 1 or len(refs) > 32
        or any(not isinstance(row, dict) for row in refs)):
        return ()
    inquiry = inquiries[0]
    product_id, source_id = inquiry.get("product_id"), inquiry.get("source_message_id")
    digest, value = inquiry.get("source_digest"), inquiry.get("requested_value")
    if (inquiry.get("axis") != "color" or type(product_id) is not int or product_id <= 0
        or type(source_id) is not int or not re.fullmatch(r"[a-f0-9]{64}", str(digest or ""))
        or not isinstance(value, str) or not 0 < len(value) <= 80
        or not any(row.get("message_id") == source_id and row.get("source_digest") == digest for row in refs)):
        return ()
    rows = [line.as_dict() for line in plan.line_plans if line.line_id == inquiry.get("line_id")]
    if len(rows) != 1:
        return ()
    row = rows[0]
    if (plan.source_cart_capture.get("active_line_id") != row["line_id"]
        or row["choices"].get("product_id") != product_id or row["configuration"].get("product_id") != product_id):
        return ()
    garment = row["choices"].get("garment_type")
    if row["evidence"].get("garment_type", {}).get("source_message_id") != source_id:
        return ()
    obligations = [item for item in plan.obligations if item.get("kind") == "garment_type"
        and item.get("line_id") == row["line_id"] and item.get("source_message_id") == source_id
        and item.get("value") == garment
        and item.get("evidence", {}).get("source_message_id") == source_id
        and item.get("evidence", {}).get("source_digest") == row["evidence"].get("garment_type", {}).get("source_digest")
        and re.fullmatch(r"[a-f0-9]{64}", str(item.get("evidence", {}).get("source_digest") or ""))]
    if not garment or not obligations:
        return ()
    text = own_reply_text(response.reply_text)
    if any(key != garment and pattern.search(text) for key, pattern in _GARMENT_WORDS.items()):
        return ()
    if re.search(r"\b(?:customer|client|manager|bot|assistant|клієнт\w*|клиент\w*|менеджер\w*)\s+(?:said|wrote|напис\w*|сказ\w*)\b", text, re.I):
        return ()
    # A typed manager control alone is insufficient: the same own sentence
    # must positively forward this exact color wish for a possibility check.
    forwarding = re.compile(r"\bя\s+передам\b|\b(?:i|we)\s+will\s+(?:forward|pass)\b", re.I)
    committed = _current_color_check_commitment(plan, text, value)
    for sentence in re.findall(r"[^.!?;\n]+[?]?", text):
        if ("?" not in sentence and re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", sentence, re.I)
            and re.search(r"менедж\w*|\bmanager\b", sentence, re.I)
            and _has_positive_claim(forwarding, sentence)
            and re.search(r"можлив\w*|возможн\w*|possibilit\w*", sentence, re.I)
            and not re.search(r"\b(?:не|not|never|no)\b", sentence, re.I)):
            committed = True
    if not committed:
        return ()
    normalize = lambda value: re.sub(r"\s+", " ", str(value)).strip(" .!\n").casefold()
    clauses = scoped_claim_clauses(_unquoted_text(response.reply_text))
    result = []
    for assertion in captured.get("base_context_assertions", ()):
        if (assertion.get("schema") != "approved-base-assertion.v1"
            or assertion.get("topic") != "composition" or assertion.get("product_id") != product_id
            or assertion.get("garment") != garment or not approved_assertion_valid(assertion)):
            continue
        # Use the reviewed published answer itself, never naked percentages or
        # a guessed single-cart anaphora. Quotes, negation and foreign subjects
        # cannot equal this positive own clause.
        for clause in clauses:
            if (normalize(clause) == normalize(assertion.get("text"))
                and approved_product_assertion_covered(clause, assertion, topic="composition")):
                result.append({"line_id": row["line_id"], "source_message_id": source_id,
                    "garment": garment, "clause": clause})
    return tuple(result)


def _topic_covered(kind, response, plan, *, source_message_id=None, source_price_subjects=()):
    """Require a protected, canonical answer assertion or an admitted handoff.

    The full candidate has already passed truth/scope/action validation. This
    additional test never treats an arbitrary number or keyword as an answer.
    """
    from management.services.ig_reply_truth import (
        ReplyTruthContext, _claim_sentences, _MONEY_RE, _TIMING_RE,
        _is_qualified_standard_dispatch_timing, validate_reply_truth,
        _RECRUITMENT_STATUS_RE, _RECRUITMENT_UNCERTAINTY_RE,
    )
    authority = plan.authority
    if kind == "info:question" and _pending_stock_answer_covered(plan, response.reply_text, source_message_id):
        return True
    from management.services.ig_product_inquiries import approved_product_topic_covered
    if approved_product_topic_covered(kind, response.reply_text, plan.product_information):
        return True
    if kind == "info:size_chart":
        # Only the separately authenticated native attachment covers a chart.
        return False
    if kind in {"info:fit", "info:fit_presentation", "info:audience"}:
        from management.services.ig_fit_consultation import fit_topic_covered

        return fit_topic_covered(kind, response, plan.fit_consultation)
    if kind == "info:manager_status":
        from management.services.ig_pending_question_context import pending_status_answer_covered
        return pending_status_answer_covered(plan.pending_questions, response.reply_text,
            source_message_id=source_message_id)
    if kind == "info:reply_language":
        from management.services.ig_reply_language import _observed_language
        product_id = plan.configuration.get("product_id")
        proof = plan.evidence.get("product_id") or {}
        names = (str(plan.configuration["product_title"]),) if (
            product_id and str(plan.choices.get("product_id")) == str(product_id)
            and type(proof.get("source_message_id")) is int
            and plan.configuration.get("product_title")) else ()
        if _observed_language(response.reply_text, non_language_names=names) == "en":
            # Answering another current request in English demonstrates that
            # we can continue in it. English filler or a generic deferral alone
            # does not answer a standalone communication concern.
            checked = set()
            for obligation in plan.obligations:
                other = obligation["kind"]
                key = (other, obligation.get("source_message_id"))
                if other == kind or key in checked:
                    continue
                checked.add(key)
                if other == "product_id" and names and _acknowledges_product(names[0], response.reply_text):
                    return True
                value = str(plan.choices.get(other) or "")
                if (other in {"size", "fit_option_code", "color", "quantity", "garment_type"}
                    and value and _acknowledges_choice(other, value, response.reply_text, plan.authority,
                        audited=other == "size" and plan._audited_size())):
                    return True
                if (other.startswith("info:") and not other.startswith("info:payment")
                    and _topic_covered(other, response, plan,
                        source_message_id=obligation.get("source_message_id"))):
                    return True
        return bool(re.search(r"\b(?:i|we)\s+(?:can|will|shall|(?:am|are)\s+happy\s+to)\s+(?:reply|answer|speak|write|continue)[^.!?\n]{0,25}\benglish\b"
            r"|\b(?:let['’]s|let\s+us)\s+(?:continue|speak|write)[^.!?\n]{0,15}\benglish\b", _unquoted_text(response.reply_text), re.I))
    if kind == "info:presentation":
        if plan.configuration.get("catalog_browse"):
            from management.services.ig_catalog_browsing import browse_presentation_covered, browse_clarification_covered
            capture = plan.configuration["catalog_browse"]
            return (browse_presentation_covered(capture, response, source_message_id)
                or browse_clarification_covered(capture, response, source_message_id))
        if response.control.get("show_products"):
            return True
    prices = tuple(Decimal(value) for value in authority["prices"])
    days = authority.get("standard_dispatch_days")
    context = ReplyTruthContext(authorized_prices=prices,
        approved_timing_claims=tuple(authority.get("timing_claims") or ()),
        explicitly_qualified_standard_dispatch_days=tuple(days) if days else None,
        payment_confirmed=authority["payment_confirmed"],
        recruitment_status=authority.get("recruitment_status", "unknown"))
    for clause in _claim_sentences(response.reply_text, ()):
        if "?" in clause or (kind in {"info:price", "info:dispatch_timing"} and re.search(
            r"не\s+(?:знаю|знаємо|знаем)|(?:do\s+not|don't)\s+know", clause, re.I)):
            continue
        if kind == "info:price" and plan.configuration.get("product_id"):
            from management.services.ig_response_cart_plan import single_price_subject
            scoped_subject = any(subject.line_id == plan.scope.get("line_id")
                and subject.product_id == plan.configuration["product_id"]
                and subject.title == plan.configuration.get("product_title")
                and re.search(r"(?<!\w)" + re.escape(subject.title) + r"(?!\w)", clause, re.I)
                for subject in source_price_subjects)
            if (not re.search(r"ціна|вартість|кошту|цена|стоим|стоит|price|cost", clause, re.I)
                and not scoped_subject and not single_price_subject(clause, choices=plan.choices, title=plan.configuration.get("product_title", ""))):
                continue
            amounts = _MONEY_RE.findall(clause)
            price_context = plan.truth_context(replace(context,
                allowed_sizes=tuple(authority.get("sizes", ())),
                allowed_fits=tuple(authority.get("fits", ())),
                allowed_colors=tuple(authority.get("colors", ()))))
            price_context = replace(price_context, source_price_subjects=tuple(source_price_subjects))
            if len(amounts) == 1 and validate_reply_truth(clause, context=price_context).valid:
                try:
                    if Decimal(amounts[0][0].replace(",", ".")) in prices:
                        return True
                except InvalidOperation:
                    pass
        elif kind == "info:dispatch_timing" and re.search(r"підготов|подготов|відправ|отправ|prepar|dispatch|ship", clause, re.I):
            if validate_reply_truth(clause, context=context).valid and any(
                _is_qualified_standard_dispatch_timing(clause, match.group(0), context)
                or match.group(0) in context.approved_timing_claims for match in _TIMING_RE.finditer(clause)):
                return True
        elif kind == "info:recruitment" and re.search(r"ваканс|staff|job|hiring", clause, re.I):
            if context.recruitment_status != "unknown" and _RECRUITMENT_STATUS_RE.search(clause) and validate_reply_truth(clause, context=context).valid:
                return True
            if context.recruitment_status == "unknown" and _RECRUITMENT_UNCERTAINTY_RE.search(clause):
                return True
    from management.services.ig_product_inquiries import inquiry_handoff_topic_covered
    return inquiry_handoff_topic_covered(kind, response, plan.product_information,
        source_message_id=source_message_id)


@dataclass(frozen=True)
class ResponsePlan:
    choices: dict
    evidence: dict
    scope: dict
    configuration: dict
    next_selector: str
    authority: dict
    obligations: tuple[dict, ...]
    pending_questions: dict = field(default_factory=dict)
    source_selection: dict = field(default_factory=dict)
    readiness_snapshot: dict = field(default_factory=dict)
    payment_observation: dict = field(default_factory=dict)
    payment_claim_source_ids: tuple[int, ...] = ()
    payment_context_snapshot: dict = field(default_factory=dict)
    payment_context_boundary: dict = field(default_factory=dict)
    payment_context_fence: str = ""
    payment_context_reason: str = ""
    payment_context_captured_at: str = ""
    source_cart_capture: dict = field(default_factory=dict)
    line_plans: tuple = ()
    plan_gap: str = ""
    # In-process frozen communication input. Business authority and dispatch
    # remain with the separately captured native payment/source fences.
    communication_context: object | None = field(default=None, repr=False, compare=False)
    catalog_inquiry: dict = field(default_factory=dict)
    fit_consultation: dict = field(default_factory=dict)
    product_focus: dict = field(default_factory=dict)
    product_information: dict = field(default_factory=dict)
    business_context: dict = field(default_factory=dict)
    cooperation_policy: object | None = field(default=None, repr=False, compare=False)
    pending_stock_questions: dict = field(default_factory=dict)
    recipient_planning: dict = field(default_factory=dict)

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()

    def as_dict(self):
        payload = {"version": 1, "choices": self.choices, "evidence": self.evidence,
                "scope": self.scope, "configuration": self.configuration,
                "next_selector": self.next_selector, "authority": self.authority,
                "obligations": list(self.obligations)}
        if self.pending_questions:
            payload["pending_questions"] = deepcopy(self.pending_questions)
        if self.recipient_planning:
            payload["recipient_planning"] = deepcopy(self.recipient_planning)
        if self.business_context:
            payload["business_context"] = deepcopy(self.business_context)
        if self.cooperation_policy is not None:
            payload["cooperation_public_policy"] = self.cooperation_policy.metadata()
        if self.pending_stock_questions:
            payload["pending_stock_questions"] = deepcopy(self.pending_stock_questions)
        if self.payment_observation or self.payment_claim_source_ids:
            payload.update(payment_observation=self.payment_observation,
                payment_claim_source_ids=list(self.payment_claim_source_ids))
        if self.line_plans:
            payload["line_plans"] = [line.as_dict() for line in self.line_plans]
            payload["next_selector_line"] = self.next_selector_line
        if self.plan_gap:
            payload["plan_gap"] = self.plan_gap
        if self.catalog_inquiry:
            payload["catalog_inquiry"] = deepcopy(self.catalog_inquiry)
        if self.fit_consultation:
            payload["fit_consultation"] = deepcopy(self.fit_consultation)
        if self.product_focus:
            payload["product_focus"] = deepcopy(self.product_focus)
        if self.product_information:
            payload["product_information"] = deepcopy(self.product_information)
        return payload

    @property
    def focused_candidate_line_id(self):
        candidate = self.configuration.get("focus_selection_candidate") or {}
        if (len(self.line_plans) == 1 and self.product_focus
            and candidate.get("line_id") == self.line_plans[0].line_id
            and candidate.get("product_id") == self.product_focus.get("product_id")
            and candidate.get("focus_proof_digest") == self.product_focus.get("proof_digest")):
            return self.line_plans[0].line_id
        return ""

    @property
    def next_selector_line(self):
        if self.obligations and all(row["kind"].startswith(("payment:", "info:payment", "business:")) for row in self.obligations):
            return {}
        for line in self.line_plans:
            row = line.as_dict()
            selector = (self.configuration["focus_selection_candidate"]["next_selector_hint"]
                if row["line_id"] == self.focused_candidate_line_id else row["next_selector"])
            if selector:
                return {"line_id": row["line_id"], "recipient_id": row["recipient_id"],
                    "field": selector, "label": row["configuration"].get("next_selector_label") or selector,
                    "product_title": row["configuration"].get("product_title") or "",
                    "ordinal": row["index"] + 1}
        return {}

    def line_truth_contexts(self, context):
        """Exact line contexts; callers must validate claims in that line's clause."""
        contexts = {}
        for line in self.line_plans:
            row = line.as_dict()
            authority = row["authority"]
            scoped = replace(context, cooperation_policy=self.cooperation_policy, authorized_prices=tuple(Decimal(value) for value in authority["prices"]),
                authorized_price_ranges=tuple((Decimal(low), Decimal(high)) for low, high in authority["price_ranges"]),
                allowed_sizes=tuple(authority["sizes"]), allowed_fits=tuple(authority["fits"]), allowed_colors=tuple(authority["colors"]))
            derived = ResponsePlan(row["choices"], row["evidence"], row["scope"],
                row["configuration"], row["next_selector"], authority, tuple(row["obligations"]),
                product_information={"resolved_information": [item for item in self.product_information.get("resolved_information", ())
                    if item.get("line_id") == row["line_id"]],
                    "base_context_assertions": [item for item in self.product_information.get("base_context_assertions", ())
                        if item.get("product_id") == row["configuration"].get("product_id")]}).truth_context(scoped)
            from management.services.ig_reply_truth import SourcePriceSubject
            capture = self.source_cart_capture
            fields = ("product_id", *(key for key in ("size", "fit_option_code", "color") if row["choices"].get(key)))
            source_ids = tuple((row["evidence"].get(key) or {}).get("source_message_id") for key in fields)
            captured_rows = [item for item in capture.get("lines") or [] if item.get("line_id") == row["line_id"]]
            captured_fields = captured_rows[0].get("fields") or {} if len(captured_rows) == 1 else {}
            if (capture.get("status") == "captured" and capture.get("coverage_complete")
                and re.fullmatch(r"[a-f0-9]{64}", str(capture.get("capture_digest") or ""))
                and row["scope"].get("capture_digest") == capture["capture_digest"]
                and len(captured_rows) == 1
                and all((captured_fields.get(key) or {}).get("value") == row["choices"].get(key) for key in fields)
                and type(row["choices"].get("product_id")) is int
                and row["choices"]["product_id"] == row["configuration"].get("product_id")
                and row["configuration"].get("product_title") and authority["prices"]
                and all(type(identity) is int and identity > 0 for identity in source_ids)):
                subject = SourcePriceSubject(row["line_id"], row["choices"]["product_id"],
                    row["configuration"]["product_title"], capture["capture_digest"], tuple(sorted(set(source_ids))),
                    tuple((key, str(row["choices"][key])) for key in fields if key != "product_id"),
                    tuple(Decimal(value) for value in authority["prices"]),
                    product_kind=str(row["configuration"].get("product_kind") or ""))
                derived = replace(derived, source_price_subjects=(subject,))
            contexts[row["line_id"]] = derived
        return contexts

    def validate_multiline_claims(self, response, context):
        """Validate each assertion against its own line; no union authority."""
        if not self.line_plans:
            return ""
        combined_sources = {row["source_message_id"] for row in self.obligations
            if row["kind"] == "info:price"} & set(self.pending_stock_questions.get("source_message_ids", ()))
        if any(not _pending_stock_answer_covered(self, response.reply_text, source_id)
                for source_id in combined_sources):
            return "response_plan_pending_stock_answer_required"
        from management.services.ig_response_cart_plan import matching_line_ids, scoped_claim_clauses, anaphoric_price_reply_line_id, selected_title_text
        from management.services.ig_reply_truth import validate_reply_truth
        scoped = self.line_truth_contexts(context)
        unbound = replace(context, cooperation_policy=self.cooperation_policy, authorized_prices=(), authorized_price_ranges=(), allowed_sizes=(),
            allowed_fits=(), allowed_colors=(), source_chosen_sizes=(), source_chosen_fits=(),
            source_chosen_colors=(), audited_chosen_sizes=(), source_price_subjects=())
        identity = (selected_title_text(response.reply_text, self.configuration.get("product_title", ""))
            if len(self.line_plans) == 1 else response.reply_text)
        plain_identity = _unquoted_text(identity)
        base_anchors = _approved_base_wish_anchors(self, response)
        for sentence in scoped_claim_clauses(plain_identity):
            from management.services.ig_cooperation_policy import public_statement_topic
            if public_statement_topic(sentence, self.cooperation_policy):
                continue
            matches = matching_line_ids(sentence, self.line_plans)
            if not matches:
                matches = tuple({row["line_id"] for row in base_anchors if row["clause"] == sentence})
            if not matches and plain_identity == identity:
                active = self.source_cart_capture.get("active_line_id")
                owner = anaphoric_price_reply_line_id(sentence, self.line_plans, active_line_id=active,
                    readiness_by_line={active: self.readiness_snapshot}, obligations=self.obligations)
                if owner:
                    matches = (owner,)
            if not matches and any(_pending_stock_answer_covered(self, sentence, source_id)
                    for source_id in self.pending_stock_questions.get("source_message_ids", ())):
                matches = (self.pending_stock_questions["line_id"],)
            candidate_context = scoped[matches[0]] if len(matches) == 1 else unbound
            if len(matches) == 1:
                row = next(line.as_dict() for line in self.line_plans if line.line_id == matches[0])
                local = ResponsePlan(row["choices"], row["evidence"], row["scope"], row["configuration"],
                    row["next_selector"], row["authority"], tuple(row["obligations"]))
                from types import SimpleNamespace
                reason = local.validate(SimpleNamespace(reply_text=sentence, control={}))
                if reason:
                    return reason
            if not validate_reply_truth(sentence, context=candidate_context).valid:
                return "response_plan_line_claim_unverified"
        return ""

    def answered_product_assertions(self, response, context):
        """Reviewed facts actually answered in an own, uniquely bound clause.

        The final aggregate guard calls this only after every clause passed
        validate_multiline_claims; unused facts from sibling lines stay out.
        """
        from management.services.approved_public_facts import approved_assertion_valid
        from management.services.ig_reply_language import own_reply_text
        from management.services.ig_reply_truth import approved_product_assertion_covered
        from management.services.ig_response_cart_plan import matching_line_ids, scoped_claim_clauses
        contexts = self.line_truth_contexts(context)
        anchors = _approved_base_wish_anchors(self, response)
        answered = {}
        for clause in scoped_claim_clauses(own_reply_text(_unquoted_text(response.reply_text))):
            if _NEGATED_CHOICE_RE.search(clause):
                continue
            owners = matching_line_ids(clause, self.line_plans)
            if not owners:
                owners = tuple({row["line_id"] for row in anchors if row["clause"] == clause})
            if len(owners) != 1:
                continue
            for assertion in contexts[owners[0]].approved_product_assertions:
                if (approved_assertion_valid(assertion)
                    and approved_product_assertion_covered(clause, assertion)):
                    answered[assertion["digest"]] = assertion
        return tuple(answered.values())

    def truth_context(self, context):
        # Source preferences authorize only acknowledgement of a wish. They
        # never widen the old catalog/proposal size/fit/color allowlists.
        from management.services.ig_product_inquiries import approved_inquiry_assertion
        assertions = tuple(row["assertion"] for row in self.product_information.get("resolved_information", ())
                           if approved_inquiry_assertion(row))
        assertions += tuple(self.product_information.get("base_context_assertions") or ())
        return replace(context, cooperation_policy=self.cooperation_policy or context.cooperation_policy, approved_product_assertions=assertions,
                       source_chosen_sizes=(str(self.choices["size"]),) if self.choices.get("size") and not self._audited_size() else (),
                       audited_chosen_sizes=(str(self.choices["size"]),) if self.choices.get("size") and self._audited_size() else (),
                       source_chosen_fits=(str(self.choices["fit_option_code"]),) if self.choices.get("fit_option_code") else (),
                       source_chosen_colors=CHOICE_ALIASES.get(str(self.choices["color"]), (str(self.choices["color"]),)) if self.choices.get("color") else (),
                       source_price_subjects=())

    def prompt_projection(self):
        """Compact presentation only; full plan/digest/source proof stay intact.

        No mandatory obligation or allowed claim is truncated. The existing
        required-context budget owner rejects the complete projection if it
        cannot fit, rather than admitting a partial list of customer needs.
        """
        from management.services.ig_fit_consultation import fit_consultation_projection
        groups = {}
        for obligation in self.obligations:
            key = (obligation.get("line_id"), obligation["source_message_id"])
            group = groups.setdefault(key, {"line_id": key[0], "source_message_id": key[1], "obligations": []})
            group["obligations"].append([obligation["id"], obligation["kind"]])
        for group in groups.values():
            prefix = (f"{group['source_message_id']}:{group['line_id']}:" if group["line_id"] is not None
                else f"{group['source_message_id']}:")
            pairs = group["obligations"]
            # Exact presentation compression only: canonical IDs are the
            # shared prefix plus the complete kind. Preserve arbitrary IDs
            # verbatim rather than guessing or normalizing their structure.
            if all(identity == prefix + kind for identity, kind in pairs):
                group["id_prefix"] = prefix
                group["obligation_kinds"] = [kind for identity, kind in pairs]
                del group["obligations"]
        payload = {"version": 1, "plan_digest": self.digest, "obligation_columns": ["id", "kind"],
            "obligation_groups": list(groups.values())}
        if self.configuration.get("manual_delivery_requirement"):
            payload["manual_delivery_requirement"] = deepcopy(self.configuration["manual_delivery_requirement"])
        if self.configuration.get("fulfillment_question_debt"):
            fact = self.configuration["fulfillment_question_debt"]
            payload["fulfillment_question_debt"] = {key: deepcopy(fact[key]) for key in (
                "request_id", "source_message_ids", "line_ids", "deadline_dates", "date_status", "state", "checkout", "next_step")}
        if self.recipient_planning:
            payload["recipient_planning"] = deepcopy(self.recipient_planning)
        if self.configuration.get("cart_selector_clarification"):
            fact = self.configuration["cart_selector_clarification"]
            payload["cart_selector_clarification"] = {key: deepcopy(fact[key]) for key in ("source_message_ids", "requests")}
        if self.business_context:
            payload["business_context"] = deepcopy(self.business_context)
        chart_fits = [{key: row[key] for key in ("id", "source_message_id", "fit_code", "line_id") if key in row}
            for row in self.obligations if row.get("fit_code") and row["kind"] == "info:size_chart"]
        if chart_fits:
            payload["requested_chart_alternatives"] = chart_fits
        def choice_sources(evidence):
            grouped = {}
            for name, proof in evidence.items():
                key = (proof.get("authority", "customer_source"), proof.get("source_message_id"))
                grouped.setdefault(key, []).append(name)
            return [{"authority": authority, "source_message_id": source_id, "fields": names}
                for (authority, source_id), names in grouped.items()]
        if self.line_plans:
            payload["lines"] = []
            payload["choice_source_groups"] = []
            for line in self.line_plans:
                row = line.as_dict()
                configuration, authority = row["configuration"], row["authority"]
                item = {"line_id": row["line_id"], "recipient_id": row["recipient_id"], "ordinal": row["index"] + 1,
                    "product_id": configuration.get("product_id"), "title": configuration.get("product_title") or "",
                    "choice_source_refs": [],
                    "catalog_authority": {key: authority[key] for key in ("prices", "price_ranges", "sizes", "fits", "colors") if authority.get(key)},
                    "readiness": {"applicability_known": bool(configuration.get("applicability_known"))}}
                if configuration.get("missing"):
                    item["readiness"]["missing"] = configuration["missing"]
                for group in choice_sources(row["evidence"]):
                    if group not in payload["choice_source_groups"]:
                        payload["choice_source_groups"].append(group)
                    item["choice_source_refs"].append(payload["choice_source_groups"].index(group))
                if configuration.get("requested_size_unavailable"):
                    item["readiness"]["requested_size_unavailable"] = True
                if row["next_selector"]:
                    item["next_selector"] = row["next_selector"]
                    if configuration.get("next_selector_label"):
                        item["next_selector_label"] = configuration["next_selector_label"]
                payload["lines"].append(item)
            payload["next_selector_line"] = self.next_selector_line
            payload["authority"] = {key: value for key, value in self.authority.items()
                if key not in {"prices", "price_ranges", "sizes", "fits", "colors"}}
        else:
            payload.update(choices=deepcopy(self.choices), choice_sources=choice_sources(self.evidence),
                scope=deepcopy(self.scope), configuration=deepcopy(self.configuration),
                next_selector=self.next_selector, authority=deepcopy(self.authority))
        if self.payment_observation:
            payload["payment_observation"] = {key: deepcopy(self.payment_observation[key]) for key in
                ("state", "source_message_ids", "payment_verified", "verification") if key in self.payment_observation}
            payload["payment_observation"]["receipts"] = [{key: receipt[key] for key in
                ("source_message_id", "document_type", "reported_transfer_status") if key in receipt}
                for receipt in self.payment_observation.get("receipts", ())]
        if self.payment_claim_source_ids:
            payload["payment_claim_source_ids"] = list(self.payment_claim_source_ids)
        if self.plan_gap:
            payload["plan_gap"] = self.plan_gap
        if self.fit_consultation:
            payload["fit_consultation"] = fit_consultation_projection(self.fit_consultation)
        if self.configuration.get("catalog_browse"):
            from management.services.ig_catalog_browsing import browse_prompt_guidance
            payload["catalog_browse"] = browse_prompt_guidance(self.configuration["catalog_browse"])
        if self.product_focus:
            payload["product_focus"] = deepcopy(self.product_focus)
        if self.configuration.get("focus_selection_candidate"):
            payload["focused_selection_candidate"] = deepcopy(self.configuration["focus_selection_candidate"])
        if self.configuration.get("consultation_measurements"):
            from management.services.ig_turn_intelligence import consultation_measurements_prompt_projection
            payload["consultation_measurements"] = consultation_measurements_prompt_projection(
                self.configuration["consultation_measurements"])
        if self.configuration.get("source_clarifications"):
            payload["pending_source_clarifications"] = deepcopy(self.configuration["source_clarifications"])
        if self.configuration.get("reorder_observations"):
            payload["reorder_resolution"] = self.configuration["reorder_observations"]
        # These named projections already retain their full semantics above.
        # The no-line configuration must not duplicate the same large grids.
        if "configuration" in payload:
            for key in ("consultation_measurements", "source_clarifications", "reorder_observations", "catalog_browse", "product_kind", "manual_delivery_requirement", "fulfillment_question_debt"):
                payload["configuration"].pop(key, None)
        return payload

    def prompt_guidance(self):
        audited = self._audited_size() or any(proof.get("authority") == "audited_correction"
            for line in self.line_plans for proof in line.as_dict()["evidence"].values())
        guidance = (
            "[SERVER RESPONSE PLAN]\n" + json.dumps(self.prompt_projection(), ensure_ascii=False, separators=(",", ":"))
            + "\nCurrent wishes and full field proof are in captured source-cart facts. Source choices authorize wishes only; "
            "Each obligation_kinds entry retains full kind and full ID=id_prefix+kind; other groups keep full [id,kind] pairs. "
            "unknown applicability authorizes no availability claim. Empty catalog_authority permits no catalog claim. "
            "Cover every obligation independently; size acknowledgement alone completes neither purchase nor payment support. "
            "A manager flag covers only its exact unknown product question, never other factual questions. "
            "Do not repeat confirmed choices/questions/controls. Ask only the named next selector. "
            "Selection controls product/size/fit/option/qty require no payment; item/objhandle and unsupported effects are forbidden. "
        )
        if self.pending_questions:
            from management.services.ig_pending_question_context import pending_question_guidance
            guidance += "\n" + pending_question_guidance(self.pending_questions)
        if self.recipient_planning:
            guidance += ("\nRecipient plans are provisional beneficiaries, not ordered units. Answer current independent questions first; "
                "do not force a selector after a complete conversational answer. Where prints are still unknown, ask whether "
                "the customer has designs for each person or wants options. When one print is known, name only the remaining person. "
                "A pending recipient clarification asks whose preference this is. Never copy another person's SKU, size, fit, color or quantity. "
                "Recipient count proves neither units, same designs, common address, delivery, discount nor payment. "
                "Consultation questions grant no selection or checkout controls; existing quote gates own the eventual all-line checkout.")
        if self.configuration.get("cart_selector_clarification"):
            guidance += ("\nThe listed current selector correction is still UNASSIGNED to a recipient/item. Preserve each line's existing "
                "size, fit, color and quantity. Answer independently supported current information questions first; do not repeat the recipient question after a complete answer. Otherwise ask one focused question whose preference should change. The active/last line is not "
                "the answer. Do not say the correction was recorded and emit no selection or payment controls until a new own source resolves its recipient.")
        if len({row.get("line_id") for row in self.obligations if row["kind"] == "info:price" and row.get("line_id")}) > 1:
            guidance += ("\nThe price request has separate source-owned line subjects. Answer each requested line's own verified unit price, "
                "naming its recipient or position number. A price for one line never answers its sibling; do not pool catalog prices or infer a total/discount.")
        if self.configuration.get("manual_delivery_requirement"):
            guidance += ("\nThe active manual delivery requirement blocks hosted checkout. Preserve the cart and arrange delivery with the manager; "
                "a carrier choice alone cannot resolve distinct destinations. Only an own explicit single-destination/parcel choice permits this form.")
        if self.configuration.get("fulfillment_question_debt"):
            guidance += ("\nThe customer's explicit date-feasibility question remains unanswered for the listed garments. "
                "Current inventory answers availability only. Preserve the existing manager review; state the date is still unconfirmed "
                "Ask exactly one clear question about whether that specific date is critical. Ask about accepting uncertain timing separately if needed; never combine the alternatives into one yes/no question. Name the date and affected person or garment when its referent could be ambiguous. Do not offer checkout while this question remains open.")
        if self.business_context or any(case.get("kind") == "collaboration_review" for case in self.pending_questions.get("cases", ())):
            guidance += "\nKnown catalog retail prices never authorize partner prices or discounts; this pending business case contains no agreed trade amount."
        if self.business_context:
            guidance += ("\nBusiness obligations acknowledge each exact trade topic with manager control and explicit team review; "
                "they grant no partner terms or retail price authority. The native collaboration case retains the decision. "
                "Trade catalog colors/sizes are no personal SKU selection. Answer independent personal purchase questions separately.")
        if self.cooperation_policy is not None:
            guidance += ("\nThe selected reviewed public cooperation module permits its exact relevant Public answers as information. "
                "Use their full exact sentences for protected amounts and permissions. They answer the public-policy topic only; "
                "individual partnership approval, exact percentage, payment receipt, order, shipment and payout remain unconfirmed. "
                "Do not replace available public answers with a blanket unknown-terms handoff.")
        if self.configuration.get("source_clarifications"):
            guidance += "\nThe current ad reference is ambiguous. Ask one short product clarification without selection/payment controls; retained cart choices do not answer that reference."
        if self.configuration.get("catalog_browse"):
            guidance += ("\nCatalog browse is a source-bound presentation capability, never selection or stock. "
                "Use up to three relevant shortlist IDs with SHOW_PRODUCTS. Respect each garment/theme/exclusion; "
                "extra membership does not disqualify a matching theme. eligible_count is catalogue membership, not stock. "
                "Themed cards have choose/theme/all-garment buttons; all-garment intentionally broadens the theme. "
                "Repeat only captured navigation URLs accompanying those same themed cards. No URL-only answer grants this capability. "
                "Unavailable/incomplete capture permits no guessed products or dropped constraints: ask one necessary clarification. "
                "An unresolved theme needs clarification, not automatic custom-print rescue.")
        if (not self.line_plans or self.payment_observation or self.payment_claim_source_ids
            or any(item["kind"].startswith(("payment:", "info:payment")) for item in self.obligations)):
            guidance += (
            "Payment claims and receipt/OCR are reported evidence, never verified money. Acknowledge an observed receipt with "
            "verification pending; a text-only claim permits reported-payment acknowledgement, never claiming receipt was read. "
            "Only authority.payment_confirmed permits verified payment. No forwarding/manager notification claim without SENT notification proof. "
            "Conversation amounts retain their source authority and component: merchandise, delivery and payable total. "
            "Attribute seller_instruction amounts explicitly to the manager's quote (За розрахунком менеджера); "
            "attribute customer-accepted amounts to the agreement. Never call the payable total a garment price or claim delivery/payment was paid from a quote. "
            "Links/requisites are instructions; receipt response completion leaves manager verification unresolved."
            )
        if audited:
            guidance += (
                "\nAn audited manager correction is a neutral corrected requirement; never attribute its value to what the customer "
                "said/chose/requested. Original source remains unchanged; correction proves neither stock nor an updated checkout."
            )
        if self.line_plans:
            guidance += ("\nchoice_source_refs indexes choice_source_groups for that line's fields and source authority. "
                "Identify each line by title/product or ordinal; duplicate SKUs require recipient or ordinal. "
                "Sibling acknowledgement/price/availability covers nothing for another line. Historical superseded choices are not current. "
                "Ask at most next_selector_line.field for that line; ambiguous line targets require finite clarification.")
        if self.configuration.get("reorder_observations"):
            guidance += ("\nThe exact historical reorder remains unresolved. Current context describes the prior purchase; "
                "no new cart or purchase has been admitted. Clarify the exact owned order/position or the stated unavailable "
                "configuration. Never invent the latest order, promote old payment/shipping to a new purchase, or submit selection/payment controls.")
        if self.fit_consultation:
            from management.services.ig_fit_consultation import fit_consultation_guidance

            guidance += "\n" + fit_consultation_guidance(self.fit_consultation)
        if self.product_focus:
            from management.services.ig_product_focus import product_focus_guidance

            guidance += "\n" + product_focus_guidance(self.product_focus)
        if self.configuration.get("focus_selection_candidate"):
            guidance += ("\nThe focused_selection_candidate is a bounded hint for this fresh customer selector/purchase assent. "
                "Use its candidate_controls only as a new current-generation proposal to the existing selection action owner. "
                "It has not selected a product or authorized availability, price or checkout. Keep the discussed print; "
                "ask its next_selector_hint only when needed, never ask for the print again. A payment link still requires "
                "canonical selection persistence and fresh checkout authority.")
        return guidance

    def _audited_size(self):
        proof = self.evidence.get("size") or {}
        receipt = (proof.get("correction") or {}).get("receipt") or {}
        return bool(proof.get("authority") == "audited_correction"
                    and receipt.get("schema") == "manager-correction.v1"
                    and receipt.get("field") == "size"
                    and receipt.get("after") == self.choices.get("size"))

    def validate(self, response):
        if self.plan_gap:
            return self.plan_gap
        from management.services.ig_catalog_browsing import validate_catalog_browse_response
        browse_reason = validate_catalog_browse_response(self.configuration.get("catalog_browse"), response)
        if browse_reason:
            return browse_reason
        if _named_selected_product_conflict(self, response.reply_text):
            return "response_plan_selected_product_mismatch"
        business_context = self.business_context or ({"case_kind": "collaboration_review"}
            if any(case.get("kind") == "collaboration_review" for case in self.pending_questions.get("cases", ())) else {})
        business_reason = _business_approval_reason(response, business_context, self.cooperation_policy)
        if business_reason:
            return business_reason
        if self.fit_consultation:
            from management.services.ig_fit_consultation import validate_fit_consultation

            reason = validate_fit_consultation(self.fit_consultation, response)
            if reason:
                return reason
        elif self.product_focus:
            from management.services.ig_fit_consultation import validate_fit_consultation

            # A new explicit browse predicate authorizes presentation beyond
            # the retained print, while granting no product/cart mutation.
            browse_presentation = bool(self.configuration.get("catalog_browse")
                and response.control.get("show_products")
                and not set(response.control).intersection({"product", "item", "option", "qty", "size", "fit", "color_variant_id"}))
            reason = validate_fit_consultation({"focus": self.product_focus,
                "assortment_requested": browse_presentation}, response)
            if reason:
                return reason
        if any(row["kind"].startswith(("payment:", "info:payment")) for row in self.obligations):
            from management.services.ig_reply_truth import _claim_sentences, _has_positive_claim
            if any("?" not in clause and _has_positive_claim(_PAYMENT_FORWARDED, clause)
                   for clause in _claim_sentences(_unquoted_text(response.reply_text), ())):
                return "response_plan_payment_forwarding_unverified"
        if self.line_plans:
            from management.services.ig_response_cart_plan import (matching_line_ids, cart_clarification_reason,
                cart_recipient_question, shared_discovery_question)
            from management.services.ig_recipient_planning import planning_question_kind
            clarification = self.configuration.get("cart_selector_clarification") or {}
            independent_answer = bool(clarification and not response.control and "?" not in response.reply_text
                and self.obligations and all(item["kind"].startswith("info:") for item in self.obligations)
                and self.coverage(response)["disposition"] == "complete")
            clarification_reason = cart_clarification_reason(clarification, response, self.line_plans,
                independent_answer=independent_answer)
            if clarification_reason:
                return clarification_reason
            for question in re.findall(r"[^.!?\n]*[?]", _unquoted_text(response.reply_text)):
                if cart_recipient_question(clarification, question):
                    continue
                if (planning_question_kind(self.recipient_planning, question, self.line_plans)
                    or shared_discovery_question(self.recipient_planning, question, self.line_plans)):
                    if set(response.control).intersection({"product", "size", "fit", "option", "qty", "color_variant_id", "items", "paylink", "payment"}):
                        return "response_plan_provisional_selection_unsupported"
                    continue
                if (self.recipient_planning and re.search(r"для кожного|для каждого|for each|для обох|для обоих|for both", question, re.I)
                    and re.search(r"\b(?:принт\w*|designs?)\b", question, re.I)
                    and any(row["has_product"] for row in self.recipient_planning.get("recipients", ()))):
                    return "response_plan_line_selector_mismatch"
                selectors = [field for field, pattern in _SELECTOR_WORDS.items() if re.search(pattern, question, re.I)
                    and asks_next_selector(question, field)]
                if not selectors:
                    continue
                matches = matching_line_ids(question, self.line_plans)
                expected = self.next_selector_line
                if not matches and self.focused_candidate_line_id:
                    matches = (self.focused_candidate_line_id,)
                if len(matches) != 1 or not expected or matches[0] != expected["line_id"] or selectors != [expected["field"].split(":", 1)[0]]:
                    return "response_plan_line_selector_mismatch"
            if "items" in response.control and not (response.control.get("paylink") or response.control.get("payment")):
                return "revision_cart_selection_unsupported"
            return ""
        if self._audited_size():
            from management.services.ig_reply_truth import _claim_sentences, _SIZE_RE, _locally_negated, CORRECTED_REQUIREMENT_RE
            chosen = self.choices.get("size")
            control = response.control.get("size")
            if control and (not chosen or str(control).casefold() != str(chosen).casefold()):
                return "response_plan_audited_size_conflict"
            if (response.control.get("paylink") or response.control.get("payment")) and not self.authority.get("audited_size_configuration_matches"):
                return "response_plan_audited_configuration_unready"
            for sentence in _claim_sentences(response.reply_text, ()):
                for clause in re.split(r"[,;]|\b(?:але|но|but)\b", sentence, flags=re.I):
                    unquoted = _unquoted_text(clause)
                    # Catalog eligibility cannot authorize a different current
                    # requirement. Quoted whole statements remain data, while
                    # a quoted size token in an assertion remains a claim.
                    values = [match.group("value") for match in _SIZE_RE.finditer(clause)
                        if not _locally_negated(clause, match.start())
                        and not _NEGATED_CHOICE_RE.fullmatch(match.group("value"))]
                    corrected = CORRECTED_REQUIREMENT_RE.search(unquoted)
                    if corrected and _locally_negated(unquoted, corrected.start()):
                        corrected = None
                    if corrected and any(not chosen or value.casefold() != str(chosen).casefold() for value in values):
                        return "response_plan_audited_size_conflict"
                    choice = re.search(r"\b(?:ви|вы|you)\s+(?:(?:have|had)\s+)?(?:обрали|вибрали|просили|сказали|написали|уточнили|попросили|выбрали|хотели|chose|chosen|selected|said|wrote|clarified|requested)\b|\b(?:ваш\w*|your)\s+(?:вибір|выбор|choice|побажан\w*|пожелан\w*|запит\w*|запрос\w*|request|preference)\b", unquoted, re.I)
                    if not choice:
                        continue
                    if _locally_negated(unquoted, choice.start()):
                        continue
                    historical = re.search(r"\b(?:earlier|previously|originally|formerly|раніше|раньше|попередньо|прежде)\b", unquoted[:choice.start()], re.I)
                    if historical:
                        continue
                    bare_size = re.search(r"\b(?:XS|S|M|L|XL|XXL|XXXL|XXXXL|[5-8]XL)\b", clause, re.I)
                    if values or bare_size or (chosen and re.search(r"\b" + re.escape(str(chosen)) + r"\b", clause, re.I)):
                        return "response_plan_audited_choice_misattributed"
        for field, selector in (("size", "size"), ("fit_option_code", "fit"), ("color", "color")):
            if not self.choices.get(field) or self.next_selector == selector:
                continue
            for question in re.findall(r"[^.!?\n]*[?]", response.reply_text):
                if re.search(_SELECTOR_WORDS[selector], question, re.I) and re.search(
                    r"який|яку|какой|какую|which|what", question, re.I):
                    return "response_plan_repeated_selector"
        if "items" in response.control and not (response.control.get("paylink") or response.control.get("payment")):
            return "revision_cart_selection_unsupported"
        return ""

    def coverage(self, response, *, local=False, checkout_cart_binding=None, catalog_media_evidence=None):
        if self.plan_gap:
            return {"version": 1, "plan_digest": self.digest,
                "source_message_ids": sorted({row["source_message_id"] for row in self.obligations}),
                "covered": [], "remaining": [row["id"] for row in self.obligations], "disposition": "recovery",
                "next_selector": "", "local": bool(local), "gap": self.plan_gap}
        if self.line_plans:
            return self._line_coverage(response, local=local, checkout_cart_binding=checkout_cart_binding,
                catalog_media_evidence=catalog_media_evidence)
        chart_ids = _native_chart_coverage(self, catalog_media_evidence, response.reply_text)
        inquiry_ids, media_ids = _catalog_inquiry_coverage(self, response, catalog_media_evidence) if not local else (set(), set())
        text = response.reply_text
        covered, remaining = [], []
        question = asks_next_selector(text, self.next_selector)
        checkout = bool(response.control.get("paylink") or response.control.get("payment"))
        for obligation in self.obligations:
            kind = obligation["kind"]
            value = str(self.choices.get(kind) or "")
            acknowledged = kind in {"size", "fit_option_code", "color", "quantity", "garment_type"} and value and _acknowledges_choice(kind, value, text, self.authority, audited=kind == "size" and self._audited_size())
            product_acknowledged = kind == "product_id" and self.choices.get("product_id") and (
                str(response.control.get("product") or "") == str(self.choices["product_id"])
                or (self.configuration.get("product_title") and _acknowledges_product(self.configuration["product_title"], text)))
            done = bool((kind in {"unclassified", "garment_type"} and _superseded_color_wish_covered(self, response,
                            obligation["source_message_id"], current_garment=kind == "garment_type"))
                        or obligation["id"] in chart_ids or acknowledged or product_acknowledged or (kind == "purchase_requested" and (checkout or _manual_delivery_purchase_handled(self, response) or _fulfillment_question_purchase_handled(self, response)))
                        or (kind == "garment_type" and obligation["source_message_id"] in inquiry_ids)
                        or (kind == "media:catalog_reference" and obligation["source_message_id"] in media_ids)
                        or (kind.startswith(("payment:", "info:payment")) and _payment_covered(kind, response, self, obligation["source_message_id"]))
                        or (kind.startswith("info:") and not kind.startswith("info:payment") and _topic_covered(kind, response, self, source_message_id=obligation["source_message_id"]))
                        or (kind.startswith("business:") and _business_topic_covered(kind, response, self.business_context, obligation["source_message_id"], self.cooperation_policy))
                        or (kind.startswith("withdrawal:") and asks_next_selector(text, kind.split(":", 1)[1])))
            if not done and kind in {"unresolved_line_operation", "info:question"}:
                from management.services.ig_source_clarification import source_clarifier_covered
                done = source_clarifier_covered(self.configuration, obligation["source_message_id"], response)
            (covered if done else remaining).append(obligation["id"])
        dependent_kinds = {"purchase_requested", "model_query"}
        if self.next_selector and not self.authority["prices"]:
            dependent_kinds.add("info:price")
        dependent_remaining = any(row["id"] in remaining and row["kind"] in dependent_kinds for row in self.obligations)
        other_remaining = any(row["id"] in remaining and row["kind"] not in dependent_kinds for row in self.obligations)
        disposition = "complete" if not remaining else (
            "waiting_on_customer" if question and dependent_remaining and not other_remaining else "recovery")
        result = {"version": 1, "plan_digest": self.digest,
                "source_message_ids": sorted({row["source_message_id"] for row in self.obligations}),
                "covered": covered, "remaining": remaining, "disposition": disposition,
                "next_selector": self.next_selector if question else "", "local": bool(local)}
        if (self.payment_observation or self.payment_claim_source_ids) and not self.authority["payment_confirmed"]:
            result["payment_verification"] = "unresolved"
        return result

    def _line_coverage(self, response, *, local=False, checkout_cart_binding=None, catalog_media_evidence=None):
        from types import SimpleNamespace
        from management.services.ig_response_cart_plan import (
            admitted_checkout_line_ids, line_clauses, matching_line_ids,
        )
        clauses = line_clauses(response.reply_text, self.line_plans)
        from management.services.ig_response_cart_plan import scoped_claim_clauses, anaphoric_price_reply_line_id, selected_title_text
        active = self.source_cart_capture.get("active_line_id")
        identity = (selected_title_text(response.reply_text, self.configuration.get("product_title", ""))
            if len(self.line_plans) == 1 else response.reply_text)
        for clause in scoped_claim_clauses(identity if _unquoted_text(identity) == identity else ""):
            owner = anaphoric_price_reply_line_id(clause, self.line_plans, active_line_id=active,
                readiness_by_line={active: self.readiness_snapshot}, obligations=self.obligations)
            if not owner and any(_pending_stock_answer_covered(self, clause, source_id)
                    for source_id in self.pending_stock_questions.get("source_message_ids", ())):
                owner = self.pending_stock_questions["line_id"]
            if owner and clause not in clauses[owner]:
                clauses[owner].append(clause)
        if self.focused_candidate_line_id:
            from management.services.ig_response_cart_plan import scoped_claim_clauses

            clauses[self.focused_candidate_line_id] = list(scoped_claim_clauses(_unquoted_text(response.reply_text)))
        admitted = admitted_checkout_line_ids(checkout_cart_binding, self.source_cart_capture, local=local)
        chart_ids = _native_chart_coverage(self, catalog_media_evidence, response.reply_text)
        base_anchors = _approved_base_wish_anchors(self, response)
        covered, remaining = [], []
        rows = {line.line_id: line.as_dict() for line in self.line_plans}
        from management.services.ig_reply_truth import ReplyTruthContext
        price_contexts = (self.line_truth_contexts(ReplyTruthContext())
            if any(item["kind"] == "info:price" for item in self.obligations) else {})
        selector = self.next_selector_line
        from management.services.ig_recipient_planning import planning_question_kind
        from management.services.ig_response_cart_plan import shared_discovery_question, cart_recipient_question, cart_clarification_reason
        planning_questions = {planning_question_kind(self.recipient_planning, text, self.line_plans)
            or ("product" if shared_discovery_question(self.recipient_planning, text, self.line_plans) else "")
            for text in re.findall(r"[^.!?\n]*[?]", _unquoted_text(response.reply_text))} - {""}
        clarification = self.configuration.get("cart_selector_clarification") or {}
        recipient_question = any(cart_recipient_question(clarification, text)
            for text in re.findall(r"[^.!?\n]*[?]", _unquoted_text(response.reply_text)))
        question = any(asks_next_selector(text, selector.get("field", ""))
            and (matching_line_ids(text, self.line_plans) == (selector.get("line_id"),)
                or selector.get("line_id") == self.focused_candidate_line_id)
            for text in re.findall(r"[^.!?\n]*[?]", _unquoted_text(response.reply_text))) if selector else False
        for obligation in self.obligations:
            kind, line_id = obligation["kind"], obligation.get("line_id")
            done = False
            if kind in {"unclassified", "garment_type"} and _superseded_color_wish_covered(self, response,
                    obligation["source_message_id"], current_garment=kind == "garment_type"):
                done = True
            elif kind == "garment_type" and any(row["source_message_id"] == obligation["source_message_id"]
                    and row["line_id"] == line_id and row["garment"] == obligation.get("value") for row in base_anchors):
                done = True
            elif obligation["id"] in chart_ids:
                done = True
            elif kind.startswith("business:"):
                done = _business_topic_covered(kind, response, self.business_context, obligation["source_message_id"], self.cooperation_policy)
            elif kind in {"unresolved_line_operation", "info:question"} and self.configuration.get("source_clarifications"):
                from management.services.ig_source_clarification import source_clarifier_covered
                done = source_clarifier_covered(self.configuration, obligation["source_message_id"], response)
                if not done and kind == "info:question":
                    done = _topic_covered(kind, response, self, source_message_id=obligation["source_message_id"])
            elif kind.startswith(("payment:", "info:payment")):
                done = _payment_covered(kind, response, self, obligation["source_message_id"])
            elif line_id in rows:
                row = rows[line_id]
                text = ". ".join(clauses[line_id])
                plan = ResponsePlan(row["choices"], row["evidence"], row["scope"], row["configuration"],
                    row["next_selector"], row["authority"], tuple(row["obligations"]),
                    pending_questions=self.pending_questions,
                    pending_stock_questions=self.pending_stock_questions,
                    product_information={"resolved_information": [item for item in self.product_information.get("resolved_information", ())
                        if item.get("line_id") == line_id],
                        "base_context_assertions": [item for item in self.product_information.get("base_context_assertions", ())
                            if item.get("product_id") == row["configuration"].get("product_id")]})
                value = str(obligation.get("value") or "")
                if kind in {"size", "fit_option_code", "color", "quantity", "garment_type"} and value:
                    done = _acknowledges_choice(kind, value, text, row["authority"], audited=kind == "size" and plan._audited_size())
                elif kind == "product_id":
                    done = bool(row["configuration"].get("product_title") and _acknowledges_product(row["configuration"]["product_title"], text))
                elif kind.startswith("withdrawal:"):
                    done = question and selector.get("line_id") == line_id and selector.get("field") == kind.split(":", 1)[1]
                elif kind.startswith("info:"):
                    done = _topic_covered(kind, SimpleNamespace(reply_text=text, control=response.control), plan,
                        source_message_id=obligation["source_message_id"],
                        source_price_subjects=price_contexts[line_id].source_price_subjects if kind == "info:price" else ())
                elif kind == "purchase_requested":
                    done = (line_id, obligation.get("recipient_id")) in admitted or _manual_delivery_purchase_handled(self, response) or _fulfillment_question_purchase_handled(self, response)
            elif kind == "info:price" and len(rows) == 1:
                # A sole captured position is an exact price subject even
                # when the source's terse price question had no line label.
                row = next(iter(rows.values()))
                price_plan = ResponsePlan(row["choices"], row["evidence"], row["scope"], row["configuration"],
                    row["next_selector"], row["authority"], tuple(row["obligations"]),
                    pending_questions=self.pending_questions,
                    pending_stock_questions=self.pending_stock_questions,
                    product_information=self.product_information)
                done = _topic_covered(kind, response, price_plan, source_message_id=obligation["source_message_id"],
                    source_price_subjects=price_contexts[row["line_id"]].source_price_subjects)
            elif kind.startswith("info:") and kind != "info:price":
                done = _topic_covered(kind, response, self, source_message_id=obligation["source_message_id"])
            if (kind == "info:price" and obligation["source_message_id"]
                    in self.pending_stock_questions.get("source_message_ids", ())):
                # One combined source asked both questions. A valid price
                # alone cannot discharge its still-pending availability ask.
                done = done and _pending_stock_answer_covered(self, response.reply_text,
                    obligation["source_message_id"])
            (covered if done else remaining).append(obligation["id"])
        dependent = {"purchase_requested", "model_query", "info:price"}
        if selector:
            dependent.add("withdrawal:" + selector["field"])
        planning_sources = {row["source"]["source_message_id"] for row in self.recipient_planning.get("recipients", ())}
        clarification_source = (self.recipient_planning.get("clarification_source") or {}).get("source_message_id")
        def planning_dependent(item):
            return bool(("product" in planning_questions and item["source_message_id"] in planning_sources
                and item["kind"] in {"garment_type", "unresolved_line_operation", "purchase_requested", "model_query"})
                or ("recipient" in planning_questions and item["source_message_id"] == clarification_source
                    and item["kind"] == "unresolved_line_operation"))
        waiting = (question or planning_questions) and not self.plan_gap and all(
            item["kind"] in dependent or planning_dependent(item) for item in self.obligations if item["id"] in remaining)
        if recipient_question and not cart_clarification_reason(clarification, response, self.line_plans):
            pending_ids = set(clarification["source_message_ids"])
            cutoff = max(pending_ids)
            waiting = not self.plan_gap and all((item["kind"] == "unresolved_line_operation"
                and item["source_message_id"] in pending_ids) or (item.get("line_id") in rows
                and item["source_message_id"] < cutoff and item["kind"] in {
                    "product_id", "size", "fit_option_code", "color", "quantity", "garment_type"})
                for item in self.obligations if item["id"] in remaining)
        result = {"version": 1, "plan_digest": self.digest,
            "source_message_ids": sorted({item["source_message_id"] for item in self.obligations}),
            "covered": covered, "remaining": remaining,
            "disposition": "complete" if not remaining and not self.plan_gap else "waiting_on_customer" if waiting else "recovery",
            "next_selector": "recipient" if recipient_question or "recipient" in planning_questions else "product" if "product" in planning_questions else selector.get("field", "") if question else "", "next_selector_line": selector if question else {},
            "local": bool(local), **({"gap": self.plan_gap} if self.plan_gap else {})}
        if (self.payment_observation or self.payment_claim_source_ids) and not self.authority["payment_confirmed"]:
            result["payment_verification"] = "unresolved"
        return result

def build_response_plan(*, preferences, readiness, context, sources=(), payment_observation=None, captured_cart=None,
                        readiness_by_line=None, contexts_by_line=None):
    """Pure constructor. Inputs must come from the existing scoped readers."""
    if captured_cart is not None:
        plan = build_response_plan(preferences=preferences, readiness=readiness, context=context)
        admitted = _captured_payment_observation(payment_observation, [row for row in sources if row.get("role") == "user"])
        claim_ids = tuple(sorted(row["message_id"] for row in sources if row.get("role") == "user" and "payment:claim" in _payment_requests(row.get("text"))))
        plan = replace(plan, payment_observation=admitted, payment_claim_source_ids=claim_ids)
        return attach_cart_response_plan(plan, capture=captured_cart, sources=sources,
            readiness_by_line=readiness_by_line or {}, contexts_by_line=contexts_by_line or {}, context=context)
    current_sources = [row for row in sources[:64] if row.get("role") == "user"]
    business_context, retail_sources = _business_source_partition(current_sources)
    business_ids = set(business_context.get("evidence_message_ids", ()))
    personal_fields = {row["message_id"]: _personal_choice_fields(row)
        for row in retail_sources if row["message_id"] in business_ids}
    values = preferences.get("values") or {}
    watermark = max((int(row.get("message_id") or 0) for row in sources), default=0)
    original_evidence = preferences.get("evidence") or {}
    choices = {key: values[key] for key in CHOICE_FIELDS
               if key in values and isinstance(original_evidence.get(key), dict)
               and original_evidence[key].get("source_message_id")
               and (original_evidence[key]["source_message_id"] not in business_ids
                    or key in personal_fields[original_evidence[key]["source_message_id"]])
               and (not watermark or int(original_evidence[key]["source_message_id"]) <= watermark)}
    evidence = {key: {name: original_evidence[key][name] for name in
                     ("source_message_id", "source_digest", "decision_id", "transition_id", "product_resolution", "presentation_effect_ids", "authority", "correction")
                     if name in original_evidence[key]} for key in choices}
    if "size" in (preferences.get("cleared") or {}):
        evidence["size"] = deepcopy(preferences["cleared"]["size"])
    missing = [str(item) for item in readiness.get("missing") or []][:16]
    # A chosen but unavailable size requires availability resolution, not the
    # same size question. It is never silently recast as absent customer choice.
    unavailable = bool((readiness.get("size") or {}).get("requested_unavailable"))
    known = {"size": "size", "fit": "fit_option_code", "color": "color", "quantity": "quantity"}
    next_selector = next((item for item in missing if item != "options_unavailable"
                          and not choices.get(known.get(item, ""))), "")
    if not readiness.get("has_product"):
        next_selector = "" if choices.get("product_id") else "product"
    axis = next((row for row in (readiness.get("options") or {}).get("axes", [])
                 if "option:" + str(row.get("code") or "") == next_selector), {})
    configuration = {"product_id": (readiness.get("product") or {}).get("id"),
                     "product_kind": str((readiness.get("product") or {}).get("kind") or "")[:120],
                     "next_selector_label": str(axis.get("label") or "")[:80],
                     "product_title": str((readiness.get("product") or {}).get("title") or "")[:240],
                     "applicability_known": bool(readiness.get("applicability_known")),
                     "missing": missing, "requested_size_unavailable": unavailable}
    authority = {"prices": [str(value) for value in context.authorized_prices][:8],
                 "price_ranges": [[str(low), str(high)] for low, high in context.authorized_price_ranges][:8],
                 "sizes": list(context.allowed_sizes)[:32], "fits": list(context.allowed_fits)[:16],
                 "colors": list(context.allowed_colors)[:16],
                 "order_created": bool(context.order_created), "payment_confirmed": bool(context.payment_confirmed),
                 "standard_dispatch_days": list(context.explicitly_qualified_standard_dispatch_days or ()),
                 "timing_claims": list(context.approved_timing_claims)[:16],
                 "recruitment_status": context.recruitment_status}
    inquiry_context = _catalog_inquiry_context(current_sources)
    bare_media = {row["source_message_id"] for row in inquiry_context.get("media_sources", ()) if row["bare"]}
    payment_observation = _captured_payment_observation(payment_observation, current_sources)
    receipt_ids = set(payment_observation.get("source_message_ids") or [])
    payment_claim_ids = set()
    obligations_list = _business_obligations(business_context)
    commerce_present = bool(obligations_list)
    for source in retail_sources:
        from management.services.ig_commerce_turns import parse_turn
        parsed = parse_turn(str(source.get("text") or ""))
        kinds = ["fit_option_code" if key == "fit" else "quantity" if key == "qty" else key
                 for key in parsed.field_updates if key in {"size", "fit", "color", "qty", "quantity"}]
        # A contextual answer such as "Звична" keeps its raw source text. The
        # canonical reducer's exact current-source preference proof, rather
        # than a global word alias, retains that fit as a reply obligation.
        text_digest = hashlib.sha256(str(source.get("text") or "").encode()).hexdigest()
        for field in ("fit_option_code", "size", "color", "quantity"):
            proof = original_evidence.get(field) or {}
            if (source["message_id"] not in business_ids and field in choices and proof.get("source_message_id") == source["message_id"]
                and proof.get("source_digest") == text_digest and field not in kinds):
                kinds.append(field)
        if source["message_id"] not in business_ids and (evidence.get("model_query") or {}).get("source_message_id") == source["message_id"]:
            # A catalog title such as Classic may resemble a parser preference.
            # Canonical ambiguity evidence cannot manufacture that choice.
            kinds = [kind for kind in kinds if (evidence.get(kind) or {}).get("source_message_id") == source["message_id"]]
        if parsed.exact_product_id or (source["message_id"] not in business_ids and (evidence.get("product_id") or {}).get("source_message_id") == source["message_id"]):
            kinds.append("product_id")
        if source["message_id"] not in business_ids and (evidence.get("model_query") or {}).get("source_message_id") == source["message_id"]:
            kinds.append("model_query")
        if parsed.purchase_requested:
            kinds.append("purchase_requested")
        if (source["message_id"] not in business_ids and (evidence.get("garment_type") or {}).get("source_message_id") == source["message_id"]) or (source["message_id"] in business_ids and parsed.garment_type):
            kinds.append("garment_type")
        for field in parsed.preference_withdrawals:
            selector = {"fit": "fit", "size": "size", "color": "color"}.get(field)
            if selector:
                kinds.append("withdrawal:" + selector)
                if not choices.get({"fit": "fit_option_code"}.get(field, field)):
                    next_selector = selector
        if kinds:
            commerce_present = True
        topics = _requested_topics(source.get("text"), parse_turn(_unquoted_text(source.get("text"))).info_topics)
        payment_kinds = _payment_requests(source.get("text"))
        if source["message_id"] in receipt_ids:
            payment_kinds.add("payment:receipt")
        if payment_kinds:
            commerce_present = True
            # Replace its generic payment topic, keeping unrelated questions.
            topics = [topic for topic in topics if not topic.startswith("info:payment")]
            kinds.extend(sorted(payment_kinds))
            if "payment:claim" in payment_kinds:
                payment_claim_ids.add(source["message_id"])
        if any(topic in topics for topic in ("info:presentation", "info:price", "info:fit", "info:fit_presentation", "info:audience", "info:size_chart",
                "info:material", "info:density", "info:weight", "info:care", "info:quality", "info:reply_language", "info:manager_status")):
            commerce_present = True
        if topics:
            kinds.extend(topics)
        if not kinds and (source["message_id"] not in business_ids or str(source.get("text") or "").strip()):
            kinds = (["media:catalog_reference"] if source["message_id"] in bare_media else
                [] if re.fullmatch(r"\s*(?:привіт|вітаю|добрий\s+день|привет|здравствуйте|hello|hi|дякую|спасиб[оа]|thanks?|thank you|ок|okay|👍|❤|❤️)[.!\s]*", str(source.get("text") or ""), re.I) else ["unclassified"])
        for kind in kinds:
            obligations_list.append({"id": f"{source['message_id']}:{kind}", "kind": kind,
                                     "source_message_id": source["message_id"]})
        source_rows = [row for row in obligations_list if row["source_message_id"] == source["message_id"]]
        obligations_list = [row for row in obligations_list if row["source_message_id"] != source["message_id"]]
        obligations_list.extend(_expand_chart_obligations(source_rows, source, supported_fits=context.allowed_fits))
    # Outside this commerce slice existing intent and delivery contracts retain
    # responsibility. Within it, an independent source never vanishes.
    obligations = tuple(obligations_list) if commerce_present else ()
    if obligations and all(row["kind"].startswith(("payment:", "info:payment", "business:")) for row in obligations):
        next_selector = ""
    scope = {key: preferences[key] for key in ("session_id", "generation", "revision", "line_id", "active_index") if key in preferences}
    from management.services.ig_fit_consultation import build_fit_consultation

    return ResponsePlan(choices, evidence, scope, configuration, next_selector, authority, obligations,
        payment_observation=payment_observation, payment_claim_source_ids=tuple(sorted(payment_claim_ids)),
        communication_context=context, catalog_inquiry=inquiry_context,
        fit_consultation=build_fit_consultation(retail_sources), business_context=business_context)


def _capture_payment_context(client, revision, sources, session, line):
    """Read the exact sealed source boundary; optional failures are finite."""
    from copy import deepcopy
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    from management.models import IgFunnelResetAudit
    from management.services.ig_admin_state_capture import capture_payment_context

    if revision is None or not sources:
        return {}, {}, "", "payment_context_sealed_source_unavailable", ""
    namespaces = {row.get("source_namespace") for row in sources}
    if len(namespaces) != 1 or not next(iter(namespaces)):
        return {}, {}, "", "payment_context_namespace_unavailable", ""
    clocks = []
    for row in sources:
        event = parse_datetime(str(row.get("provider_created_at") or row.get("observed_created_at") or ""))
        if event is None or timezone.is_naive(event):
            return {}, {}, "", "payment_context_event_time_unavailable", ""
        clocks.append((event, int(row["message_id"])))
    event, identity = max(clocks)
    watermark = {"message_id": identity, "event_at": event.isoformat()}
    reset = IgFunnelResetAudit.objects.filter(client_id=client.pk).order_by("-pk").values("pk", "reset_after_message_id").first() or {}
    episode = getattr(client, "current_commercial_episode", None)
    boundary = {"client_id": client.pk, "episode_id": client.current_commercial_episode_id,
        "order_id": getattr(episode, "intended_order_id", None), "line_id": str(line.get("line_id") or ""),
        "recipient_id": str(line.get("recipient_id") or "self"), "reset_floor": int(reset.get("reset_after_message_id") or 0) + 1,
        "source_namespace": next(iter(namespaces)), "reset_id": reset.get("pk"), "erasure_epoch": "",
        "source_watermark": watermark, "watermark": deepcopy(watermark)}
    if client.privacy_erasure_started_at or any(int(row["message_id"]) < boundary["reset_floor"] for row in sources):
        return {}, boundary, "", "payment_context_scope_changed", ""
    now = timezone.now()
    first, reason, fence = capture_payment_context(boundary, now=now)
    second, second_reason, second_fence = capture_payment_context(boundary, now=now)
    if not fence or reason != second_reason or fence != second_fence:
        return {}, boundary, "", "payment_context_changed", now.isoformat()
    return deepcopy(first), boundary, fence, reason, now.isoformat()
def attach_cart_response_plan(plan, *, capture, sources, readiness_by_line, contexts_by_line, context):
    """Pure attachment, preserving the legacy constructor and single-line API."""
    from management.services.ig_response_cart_plan import (build_cart_response_lines, matching_line_ids,
        anaphoric_price_line_id, MAX_OBLIGATIONS, cart_wide_price_line_ids, cart_selector_clarification)
    business_context, retail_sources = _business_source_partition(sources)
    business_ids = set(business_context.get("evidence_message_ids", ()))
    personal_fields = {row["message_id"]: _personal_choice_fields(row)
        for row in retail_sources if row["message_id"] in business_ids}
    def single(preferences, readiness, line_context):
        if line_context is None:
            applicable = bool(readiness.get("applicability_known"))
            line_context = replace(context, authorized_prices=(), authorized_price_ranges=(),
                allowed_sizes=tuple((readiness.get("size") or {}).get("available") or ()) if applicable else (),
                allowed_fits=tuple(item["code"] for item in (readiness.get("fit") or {}).get("options") or ()) if applicable else (),
                allowed_colors=tuple(item["name"] for item in (readiness.get("color") or {}).get("options") or ()) if applicable else (),
                source_chosen_sizes=(), source_chosen_fits=(),
                source_chosen_colors=(), audited_chosen_sizes=(), source_price_subjects=())
        else:
            line_context = replace(context, authorized_prices=line_context.authorized_prices,
                authorized_price_ranges=line_context.authorized_price_ranges, allowed_sizes=line_context.allowed_sizes,
                allowed_fits=line_context.allowed_fits, allowed_colors=line_context.allowed_colors,
                source_chosen_sizes=(), source_chosen_fits=(), source_chosen_colors=(), audited_chosen_sizes=(), source_price_subjects=())
        return build_response_plan(preferences=preferences, readiness=readiness, context=line_context)
    lines, gap = build_cart_response_lines(capture=capture, sources=sources,
        readiness_by_line=readiness_by_line, authority_by_line=contexts_by_line, single_builder=single)
    if gap:
        return replace(plan, source_cart_capture=deepcopy(capture), plan_gap=gap)
    # The source cart is immutable evidence of what the reducer captured.
    # Its reply projection must not recast trade assortment words as personal
    # choices. Retain all unrelated historical/line choices and source debt.
    projected_lines = []
    for line in lines:
        row = line.as_dict()
        row["obligations"] = [item for item in row["obligations"]
            if item["source_message_id"] not in business_ids
            or item["kind"] in personal_fields[item["source_message_id"]]]
        for key, proof in list(row["evidence"].items()):
            source_id = proof.get("source_message_id")
            if source_id in business_ids and key not in personal_fields[source_id]:
                row["evidence"].pop(key, None)
                row["choices"].pop(key, None)
        projected_lines.append(replace(line, _payload_json=json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
    lines = tuple(projected_lines)
    obligations = [*(_business_obligations(business_context)),
        *(item for line in lines for item in line.as_dict()["obligations"])]
    for source in retail_sources:
        if source.get("role") != "user":
            continue
        source_id = source["message_id"]
        owners = {item["line_id"] for item in obligations if item["source_message_id"] == source_id and item.get("line_id")}
        accepted_owners = set(owners)
        semantic = set(matching_line_ids(_unquoted_text(source.get("text")), lines))
        if semantic:
            owners = owners & semantic if owners else semantic
        topics = _requested_topics(source.get("text"))
        payment_kinds = _payment_requests(source.get("text"))
        if source_id in plan.payment_observation.get("source_message_ids", ()):
            payment_kinds.add("payment:receipt")
        if payment_kinds:
            topics = [topic for topic in topics if not topic.startswith("info:payment")]
            obligations.extend({"id": f"{source_id}:{kind}", "kind": kind, "source_message_id": source_id} for kind in sorted(payment_kinds))
        for topic in topics:
            owner = next(iter(owners)) if len(owners) == 1 else None
            if owner is None and topic == "info:price":
                owner = anaphoric_price_line_id(source.get("text"), lines,
                    active_line_id=capture.get("active_line_id"), readiness_by_line=readiness_by_line) or None
            rows = [{"id": f"{source_id}:{owner}:{topic}" if owner else f"{source_id}:{topic}",
                "kind": topic, "source_message_id": source_id, **({"line_id": owner} if owner else {})}]
            if owner is None and topic == "info:price":
                requested_lines = cart_wide_price_line_ids(source, lines)
                if requested_lines:
                    rows = [{"id": f"{source_id}:{line_id}:{topic}", "kind": topic,
                        "source_message_id": source_id, "line_id": line_id} for line_id in requested_lines]
            supported = next((line.as_dict()["authority"].get("fits", ()) for line in lines if line.line_id == owner), context.allowed_fits)
            obligations.extend(_expand_chart_obligations(rows, source, supported_fits=supported))
        # A typed operation with no current surviving source field remains debt.
        # Removed/replaced lines need a separate accepted-operation receipt.
        if not accepted_owners and not topics and not payment_kinds and source.get("text") and not re.fullmatch(
            r"\s*(?:привіт|вітаю|добрий\s+день|привет|здравствуйте|hello|hi|дякую|спасиб[оа]|thanks?|thank you|ок|okay|👍|❤|❤️)[.!\s]*", str(source["text"]), re.I):
            obligations.append({"id": f"{source_id}:unresolved_line_operation", "kind": "unresolved_line_operation", "source_message_id": source_id})
    gap = "response_plan_obligation_bound" if len(obligations) > MAX_OBLIGATIONS or any(len(item["id"]) > 160 for item in obligations) else ""
    from management.services.ig_recipient_planning import planning_context
    selector_clarification = cart_selector_clarification(capture, retail_sources, obligations, lines)
    return replace(plan, source_cart_capture=deepcopy(capture), line_plans=lines,
        configuration={**plan.configuration, **({"cart_selector_clarification": selector_clarification} if selector_clarification else {})},
        recipient_planning=planning_context(capture),
        obligations=tuple(obligations), plan_gap=gap, business_context=business_context,
        next_selector="" if obligations and all(item["kind"].startswith(("payment:", "info:payment", "business:")) for item in obligations) else next((line.as_dict()["next_selector"] for line in lines if line.as_dict()["next_selector"]), ""))


def _provided_source_cart(client, revision, capture):
    """Pure admission of the original artifact under a sealed source boundary.

    The caller separately owns the live owner/head/reset/order SourceCartFence.
    This adapter never substitutes a newer capture when the artifact is invalid.
    """
    from management.services.ig_turn_intelligence import (
        TurnContextError, _event, capture_digest, validate_source_cart_capture,
    )
    try:
        if not isinstance(capture, dict) or revision is None:
            raise TurnContextError("response_plan_provided_cart_invalid")
        detached = deepcopy(capture)
        bundle = revision.bundle_snapshot
        sources = bundle.get("sources") if isinstance(bundle, dict) else None
        if (revision.client_id != client.pk or getattr(revision, "erasure_started_at_snapshot", None)
            or getattr(client, "privacy_erasure_started_at", None)
            or not isinstance(sources, list) or not 1 <= len(sources) <= 32
            or capture_digest(bundle) != revision.snapshot_digest):
            raise TurnContextError("response_plan_provided_cart_revision_changed")
        if any(not isinstance(row, dict) or type(row.get("message_id")) is not int or row["message_id"] <= 0 for row in sources):
            raise TurnContextError("response_plan_provided_cart_sources_invalid")
        namespaces = {row.get("source_namespace") for row in sources}
        if len(namespaces) != 1 or not next(iter(namespaces)):
            raise TurnContextError("response_plan_provided_cart_scope_changed")
        event, source_id = max((_event(row.get("provider_created_at") or row.get("observed_created_at")), row["message_id"]) for row in sources)
        scope = detached.get("scope")
        if not isinstance(scope, dict) or not detached.get("lines"):
            raise TurnContextError("response_plan_provided_cart_invalid")
        boundary = {**scope, "client_id": client.pk, "episode_id": client.current_commercial_episode_id,
            "source_namespace": next(iter(namespaces)), "watermark": {"message_id": source_id, "event_at": event.isoformat()}}
        # Some sealed callers carry additional owner scope; retain those exact
        # values when present rather than drawing them from mutable live rows.
        sealed_scope = bundle.get("scope")
        if isinstance(sealed_scope, dict):
            boundary.update({key: sealed_scope[key] for key in ("order_id", "reset_id", "reset_floor") if key in sealed_scope})
        index = detached.get("active_index")
        rows = detached.get("lines")
        if not isinstance(rows, list) or type(index) is not int or not 0 <= index < len(rows) or not isinstance(rows[index], dict):
            raise TurnContextError("response_plan_provided_cart_invalid")
        boundary.update(line_id=rows[index].get("line_id"), recipient_id=rows[index].get("recipient_id"))
        from django.utils import timezone
        return validate_source_cart_capture(detached, boundary, native_as_of=timezone.now()), ""
    except TurnContextError as exc:
        return {}, exc.reason
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return {}, "response_plan_provided_cart_invalid"


def capture_response_plan(client, *, revision=None, source_cart_capture=None, publication=None):
    plan = _capture_response_plan_inputs(client, revision=revision, source_cart_capture=source_cart_capture)
    if revision is not None and publication is not None:
        from management.services.ig_cooperation_policy import capture_cooperation_policy
        plan = replace(plan, cooperation_policy=capture_cooperation_policy(publication,
            revision.bundle_snapshot.get("sources") or []))
    if revision is not None and plan.business_context:
        from management.services.ig_business_cooperation import business_source_observations
        observations = business_source_observations(revision.bundle_snapshot.get("sources") or [])
        if observations:
            plan = replace(plan, business_context={**plan.business_context, "source_observations": observations})
    if revision is None:
        return plan
    from management.services.ig_revision_checkout import requested_manual_delivery
    # This fact is prompt guidance for the real generation boundary. Ancillary
    # inquiry/read-model plans must not reread the purchase history for it;
    # checkout and mandatory intents independently enforce the actual hold.
    manual_delivery = (requested_manual_delivery(client, revision.bundle_snapshot.get("sources") or (),
        source_cart_capture=plan.source_cart_capture) if publication is not None else {})
    if manual_delivery:
        dimensions = []
        if manual_delivery.get("carrier"):
            dimensions.append({"kind": "carrier", "value": manual_delivery["carrier"], "source_message_id": manual_delivery["source_message_id"]})
        if manual_delivery.get("multiple_destinations"):
            dimensions.append({"kind": "multiple_destinations", "source_message_id": manual_delivery["multiple_destinations"]["source_message_id"]})
        plan = replace(plan, configuration={**plan.configuration, "manual_delivery_requirement": {
            "dimensions": dimensions, "hosted_checkout": "blocked", "next_step": "manual_arrangement_or_own_single_destination"}})
    from management.services.ig_fulfillment_question_debt import capture_fulfillment_question_debt, _own_release, _FEASIBILITY
    current_sources = revision.bundle_snapshot.get("sources") or ()
    current_resolution = (any(_own_release(str(row.get("text") or "")) for row in current_sources)
        and any(_FEASIBILITY.search(str(row.get("text") or "")) for row in current_sources))
    contextual_candidate = any(re.sub(r"[,.!\s]+", " ", str(row.get("text") or "").strip().casefold()).strip()
        in {"не критична", "не критично", "не критична дата", "not critical", "так підходить", "да подходит", "yes that works"}
        for row in current_sources if row.get("role") == "user")
    if publication is not None or current_resolution or contextual_candidate:
        question_debt = capture_fulfillment_question_debt(client, plan.source_cart_capture, include_resolved=True)
        if question_debt.get("state") == "unanswered_feasibility" and publication is not None:
            plan = replace(plan, configuration={**plan.configuration, "fulfillment_question_debt": question_debt})
        elif question_debt.get("state") == "resolved_by_customer":
            # A real own release consumes only a pure date question. Mixed
            # shipping-cost/product/selector questions keep their full owner.
            resolved = set(question_debt["source_message_ids"])
            releases = set(question_debt.get("release_source_message_ids", ()))
            def keep(obligation):
                return not ((obligation["source_message_id"] in resolved
                    and obligation["kind"] in {"info:shipping", "info:dispatch_timing", "info:question"})
                    or (obligation["source_message_id"] in releases and obligation["kind"] == "unresolved_line_operation"))
            from management.services.ig_response_cart_plan import ResponseLinePlan
            lines = []
            for line in plan.line_plans:
                payload = line.as_dict()
                payload["obligations"] = [row for row in payload["obligations"] if keep(row)]
                lines.append(ResponseLinePlan(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            plan = replace(plan, obligations=tuple(row for row in plan.obligations if keep(row)), line_plans=tuple(lines))
    from management.services.ig_pending_question_context import capture_pending_question_context
    plan = replace(plan, pending_questions=capture_pending_question_context(revision))
    if any(case.get("kind") == "collaboration_review" for case in plan.pending_questions.get("cases", ())):
        from management.services.ig_reply_language import own_reply_text
        from management.services.ig_pending_question_context import status_request_source_ids
        from management.services.ig_commerce_turns import parse_turn
        # An explicit question about management's answer on partner pricing
        # asks for the existing decision's status, not the current retail SKU
        # price. Generic price requests and personal/mixed clauses stay debt.
        status_ids = set(plan.pending_questions.get("status_source_ids", ()))
        trade_status_ids = set()
        for source in revision.bundle_snapshot.get("sources") or []:
            text = own_reply_text(str(source.get("text") or ""))
            clauses = re.split(r"[.!?;\n]+|,|\b(?:та|і|й|и|and|also)\s+(?=(?:яка|який|скільки|какая|какой|сколько|what|how)\b)", text, flags=re.I)
            price_clauses = [clause for clause in clauses
                if "info:price" in _requested_topics(clause, parse_turn(clause).info_topics)]
            if (source.get("message_id") in status_ids and not _PERSONAL_PURCHASE.search(text)
                and price_clauses and all(
                    status_request_source_ids([{**source, "text": clause}])
                    and re.search(r"керівництв\w*|руководств\w*|менедж\w*|команд\w*|адміністрац\w*|администрац\w*|management|manager|team", clause, re.I)
                    and re.search(r"партнер\w*|партнёр\w*|wholesale|partner\w*", clause, re.I)
                    for clause in price_clauses)):
                trade_status_ids.add(source["message_id"])
        plan = replace(plan, obligations=tuple(row for row in plan.obligations
            if not (row["source_message_id"] in trade_status_ids and row["kind"] == "info:price")))
    if revision is not None:
        from management.services.ig_fit_consultation import capture_fit_consultation
        from management.services.ig_product_focus import capture_product_focus, focus_selection_candidate

        cart = source_cart_capture or plan.source_cart_capture
        focus = capture_product_focus(revision, source_cart_capture=cart)
        fit = capture_fit_consultation(revision, source_cart_capture=cart, product_focus=focus)
        if plan.business_context:
            from management.services.ig_fit_consultation import build_fit_consultation
            _, retail_sources = _business_source_partition(revision.bundle_snapshot.get("sources") or [])
            retail_fit = build_fit_consultation(retail_sources, focus=focus,
                photo_capability=fit.get("fit_photo_capability"), approved_fact_keys=fit.get("approved_fact_keys", ()))
            # Trade assortment/permission to reuse photos does not request a
            # retail fit-photo effect. Keep actual personal fit capabilities
            # and numeric guide evidence under their existing native scope.
            fit = {**retail_fit, **({key: fit[key] for key in
                ("size_measurements", "size_measurements_complete", "next_consultation_selector") if key in fit}
                if retail_fit.get("focus") else {})} if retail_fit else {}
        candidate = focus_selection_candidate(revision, focus, source_cart_capture=cart)
        # Request-local reuse only. The immutable revision digest fences these
        # hints; a newly loaded dispatch owner recomputes its own source proof.
        revision._focused_selection_candidate = candidate
        revision._focused_selection_snapshot = revision.snapshot_digest
        configuration = {**plan.configuration}
        if any(row["kind"] == "info:size_chart" for row in plan.obligations):
            sources = revision.bundle_snapshot.get("sources") or []
            configuration["native_chart_scope"] = {"revision_id": revision.pk, "client_id": client.pk,
                "client_epoch": revision.permission_epoch, "revision_snapshot_digest": revision.snapshot_digest,
                "namespace": sources[0].get("source_namespace") if sources else "",
                "source_message_ids": [row["message_id"] for row in sources]}
        if candidate:
            configuration["focus_selection_candidate"] = candidate
        # Discussion focus can replace a request to choose the already known
        # print. Existing canonical readiness (including audited clears) owns
        # every other selector; absent fit evidence is not a new fit request.
        next_selector = (candidate["next_selector_hint"] if candidate else
            fit.get("next_consultation_selector", "")
            if fit and focus.get("status") == "focused" and plan.next_selector == "product"
            else plan.next_selector)
        if fit and focus.get("status") == "focused":
            fit = {**fit, "next_consultation_selector": next_selector}
        expanded = []
        sources_by_id = {row["message_id"]: row for row in revision.bundle_snapshot.get("sources", ())}
        focused_lines = {line.line_id for line in plan.line_plans
            if line.as_dict()["configuration"].get("product_id") == focus.get("product_id")}
        for obligation in plan.obligations:
            source = sources_by_id.get(obligation["source_message_id"])
            if (source and fit and focus.get("status") == "focused"
                and (not obligation.get("line_id") or obligation["line_id"] in focused_lines)):
                expanded.extend(_expand_chart_obligations([obligation], source, supported_fits=fit.get("supported_fits", ())))
            else:
                expanded.append(obligation)
        from management.services.ig_response_cart_plan import MAX_OBLIGATIONS
        plan = replace(plan, fit_consultation=fit, product_focus=focus if focus.get("status") == "focused" else {},
            configuration=configuration, next_selector=next_selector, obligations=tuple(expanded),
            plan_gap="response_plan_obligation_bound" if len(expanded) > MAX_OBLIGATIONS else plan.plan_gap)
    from management.services.ig_product_inquiries import product_inquiries_for_revision
    from management.services.ig_fit_consultation import capture_consultation_measurements
    from management.services.ig_product_focus import information_subject
    from management.services.ig_turn_intelligence import capture_digest
    subject = information_subject(revision.bundle_snapshot.get("sources") or ())
    if subject.get("status") == "identified" and all(subject.get(key) is False for key in (
        "future_base_requested", "unsupported_detail_requested", "specific_product_requested")):
        from storefront.models import Product
        from management.services.ig_catalog_graph import _GARMENT_TYPES_BY_CATEGORY
        category_aliases = [slug for slug, garment in _GARMENT_TYPES_BY_CATEGORY.items()
            if garment == subject["garment_type"]]
        witnesses = list(Product.objects.filter(status="published",
            category__slug__in=category_aliases).order_by("pk").values_list("pk", flat=True)[:1])
        categories = [subject["category_slug"]] if witnesses else []
        plan = replace(plan, configuration={**plan.configuration, "current_category_catalogue": {
            "schema": "current-category-catalogue.v1", "categories": categories,
            "catalogue_digest": capture_digest({"categories": categories, "witness_ids": witnesses}),
            "snapshot_digest": revision.snapshot_digest}, "information_subject": subject})
    information = product_inquiries_for_revision(revision, response_plan=plan)
    if information.get("gap") or information.get("status") == "blocked":
        return replace(plan, product_information=information,
            plan_gap=information.get("gap") or "product_inquiry_capture_invalid")
    measurements = capture_consultation_measurements(revision, product_focus=plan.product_focus,
        source_cart_capture=plan.source_cart_capture)
    configuration = dict(plan.configuration)
    if measurements.get("guides"):
        configuration["consultation_measurements"] = measurements
    plan = replace(plan, product_information=information, configuration=configuration)
    from management.services.ig_catalog_browsing import capture_catalog_browse
    browse = capture_catalog_browse(revision)
    # Fit illustrations may deliberately show another print. The existing fit
    # capability, labels and asset guard own that distinct request. A retained
    # design's unqualified "send a photo" likewise stays under its focus guard;
    # explicit garment/theme/design predicates still receive browse validation.
    fit_only = (plan.fit_consultation and not plan.fit_consultation.get("assortment_requested")
        and not any(row.get("include") or row.get("exclude") for row in browse.get("requests", ())))
    if browse and not fit_only:
        # The old parser creates photo debt, but a new named theme need not
        # contain the word "photo". Record its actual source obligation too.
        required_sources = (browse.get("scope", {}).get("source_ids", ()) if browse.get("status") == "unavailable" else
            [row["source_id"] for row in browse.get("requests", ()) if row.get("include") or row.get("exclude")
                or row.get("named_product_ids") or row.get("status") == "unresolved"])
        obligations = list(plan.obligations)
        for source_id in required_sources:
            if not any(row["kind"] == "info:presentation" and row["source_message_id"] == source_id for row in obligations):
                obligations.append({"id": f"{source_id}:info:presentation", "kind": "info:presentation",
                    "source_message_id": source_id})
        from management.services.ig_response_cart_plan import MAX_OBLIGATIONS
        plan = replace(plan, configuration={**plan.configuration, "catalog_browse": browse}, obligations=tuple(obligations),
            plan_gap="response_plan_obligation_bound" if len(obligations) > MAX_OBLIGATIONS else plan.plan_gap)
    plan = replace(plan, pending_stock_questions=_capture_pending_stock_questions(client, revision, plan))
    from management.services.ig_source_clarification import capture_source_clarifications
    clarifications = capture_source_clarifications(revision, plan.source_cart_capture, plan.obligations)
    if clarifications:
        configuration = {**plan.configuration, "source_clarifications": clarifications}
        configuration.pop("focus_selection_candidate", None)
        plan = replace(plan, configuration=configuration)
    observations = ((getattr(revision, "action_receipts", None) or {}).get("commerce_reduction") or {}).get("observations") or []
    if not isinstance(observations, (list, tuple)) or len(observations) > 32 or any(not isinstance(row, dict) for row in observations):
        return replace(plan, plan_gap="reorder_observation_changed")
    pending = [row for row in observations if row.get("classification") == "reorder_clarification"]
    if not pending:
        return plan
    sources = {row["message_id"]: row for row in revision.bundle_snapshot.get("sources") or []}
    if len(pending) > 32 or any(row.get("schema") != "commerce-source-observation.v1"
        or row.get("client_id") != client.pk or row.get("source_message_id") not in sources
        or row.get("source_digest") != hashlib.sha256(str(sources[row["source_message_id"]].get("text") or "").encode()).hexdigest()
        or row.get("source_namespace") != sources[row["source_message_id"]].get("source_namespace") for row in pending):
        return replace(plan, plan_gap="reorder_observation_changed")
    return replace(plan, configuration={**plan.configuration, "reorder_observations": deepcopy(pending)})


def _capture_response_plan_inputs(client, *, revision=None, source_cart_capture=None):
    if source_cart_capture is not None:
        from management.services.ig_reply_truth import ReplyTruthContext
        cart, reason = _provided_source_cart(client, revision, source_cart_capture)
        if reason:
            plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext())
            return replace(plan, plan_gap=reason)
        sources = deepcopy(revision.bundle_snapshot["sources"])
        watermark = max(row["message_id"] for row in sources)
        return _capture_cart_response_plan(client, sources=sources, cart_capture=cart, watermark=watermark, revision=revision)
    from management.models import IgCommerceSelectionSession
    from management.services.ig_commerce_projection import source_preferences_for, capture_current_selection_lines
    from management.services.ig_checkout_readiness import selection_readiness
    from management.services.ig_reply_authority import build_reply_truth_context

    sources = (revision.bundle_snapshot or {}).get("sources", []) if revision else []
    watermark = max((int(row.get("message_id") or 0) for row in sources), default=0)
    cart_capture = deepcopy(capture_current_selection_lines(client.pk))
    cart_rows = cart_capture.get("lines") or []
    all_line_contract = len(cart_rows) > 1 or any(
        "operation_index" in proof for row in cart_rows for proof in (row.get("evidence") or {}).values())
    if all_line_contract:
        return _capture_cart_response_plan(client, sources=sources, cart_capture=cart_capture, watermark=watermark, revision=revision)
    preferences = source_preferences_for(client)
    session = IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1).order_by("-generation").first()
    line = {}
    if session is not None:
        index = int(session.active_index or 0)
        lines = session.lines or []
        if 0 <= index < len(lines) and isinstance(lines[index], dict):
            line = lines[index]
    selection_newer = False
    if watermark and session is not None:
        from management.models import IgCommerceTurnDecision
        if IgCommerceTurnDecision.objects.filter(session=session, accepted=True, is_stale=False,
                source_message_id__gt=watermark, transition__to_revision__lte=session.revision).exists():
            # No historical source reader is being invented here. Omit the
            # newer selection; the existing inbound CAS will reject dispatch.
            preferences, line, selection_newer = {}, {}, True
    try:
        readiness = selection_readiness(product_id=line.get("product_id"), selection=line,
                                        size=str(line.get("size") or ""), fit=str(line.get("fit_option_code") or ""),
                                        quantity=line.get("quantity", 1), color=str(line.get("color") or ""), strict=True)
    except Exception:
        # Optional catalog/read-model failure cannot erase a source choice or
        # grant availability. This named gap has no invented next selector.
        readiness = {"has_product": bool(line.get("product_id")),
                     "product": {"id": line.get("product_id")},
                     "missing": ["options_unavailable"], "applicability_known": False}
    if selection_newer:
        from management.services.ig_reply_truth import ReplyTruthContext
        context = ReplyTruthContext()
    else:
        context = build_reply_truth_context(client)
    try:
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = _capture_payment_context(
            client, revision, sources, session, line)
    except Exception:
        # Optional observation capture neither fabricates receipt truth nor
        # relaxes the existing final money/source/permission guards.
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = {}, {}, "", "payment_context_unavailable", ""
    slot = (payment_snapshot.get("slots") or {}).get("receipt.observation") or {}
    payment_observation = {"observation": slot.get("value") or {}, "source_refs": slot.get("source_refs") or []}
    from management.services.ig_commerce_projection import captured_selection_from_preferences
    plan = build_response_plan(preferences=preferences, readiness=readiness, context=context,
        sources=sources, payment_observation=payment_observation)
    if plan._audited_size():
        from management.services.ig_commerce_projection import _matching_legacy_selection
        legacy = _matching_legacy_selection(client)
        chosen = plan.choices.get("size")
        matches = bool(chosen and line.get("product_id") and readiness.get("has_product")
                       and readiness.get("applicability_known") and not readiness.get("missing")
                       and client.current_product_id == line.get("product_id")
                       and str(client.current_size or "").casefold() == str(chosen).casefold()
                       and (not legacy.get("size") or str(legacy["size"]).casefold() == str(chosen).casefold()))
        plan = replace(plan, authority={**plan.authority, "audited_size_configuration_matches": matches})
    return replace(plan,
                   source_selection=captured_selection_from_preferences(client.pk, preferences),
                   readiness_snapshot=readiness, source_cart_capture=cart_capture, payment_context_snapshot=payment_snapshot,
                   payment_context_boundary=payment_boundary, payment_context_fence=payment_fence,
                   payment_context_reason=payment_reason, payment_context_captured_at=payment_at)


def _capture_cart_response_plan(client, *, sources, cart_capture, watermark, revision=None):
    """One canonical source observation; subsequent reads are catalog-only."""
    from management.services.ig_response_cart_plan import source_line_preferences
    from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
    from management.services.ig_reply_authority import build_reply_truth_context
    from management.services.ig_commerce_projection import captured_selection_from_preferences
    from management.services.ig_reply_truth import ReplyTruthContext
    active = cart_capture.get("active_line_id")
    catalog = resolve_source_cart_catalog(cart_capture)
    readiness_by_line, preferences_by_line = catalog.readiness_by_line, {}
    for row in cart_capture.get("lines") or []:
        preferences = source_line_preferences(cart_capture, row, source_watermark=watermark)
        preferences_by_line[row["line_id"]] = preferences
    preferences = preferences_by_line.get(active) or {}
    # A source beyond the sealed revision invalidates the complete observation.
    newer = bool(watermark and any(source_id > watermark for source_id in (cart_capture.get("fence") or {}).get("source_ids", ())))
    context = ReplyTruthContext() if newer else build_reply_truth_context(client)
    readiness = readiness_by_line.get(active) or {}
    active_row = next((row for row in cart_capture.get("lines") or [] if row["line_id"] == active), {})
    try:
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = _capture_payment_context(client, revision, sources, None, active_row)
    except Exception:
        payment_snapshot, payment_boundary, payment_fence, payment_reason, payment_at = {}, {}, "", "payment_context_unavailable", ""
    slot = (payment_snapshot.get("slots") or {}).get("receipt.observation") or {}
    payment_observation = {"observation": slot.get("value") or {}, "source_refs": slot.get("source_refs") or []}
    plan = build_response_plan(preferences=preferences, readiness=readiness, context=context, sources=sources,
        payment_observation=payment_observation, captured_cart=cart_capture,
        readiness_by_line=readiness_by_line, contexts_by_line=catalog.contexts_by_line)
    if catalog.gaps and not catalog.readiness_by_line:
        plan = replace(plan, plan_gap=catalog.gaps[0]["reason"])
    if newer:
        plan = replace(plan, choices={}, evidence={}, line_plans=(), plan_gap="response_plan_selection_newer_than_revision")
    return replace(plan, source_selection=captured_selection_from_preferences(client.pk, preferences) if preferences else {},
        readiness_snapshot=readiness, payment_context_snapshot=payment_snapshot,
        payment_context_boundary=payment_boundary, payment_context_fence=payment_fence,
        payment_context_reason=payment_reason, payment_context_captured_at=payment_at)
