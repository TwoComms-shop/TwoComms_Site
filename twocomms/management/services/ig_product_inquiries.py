"""Pure disposition for source-owned product wishes and unanswered facts.

Callers own the revision/source/catalog capture and revalidate it before effects.
This module performs no database, cache, provider or transport operations. A
request is evidence of a question or wish, never a catalogue/checkout fact.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re

VERSION = "product-inquiries.v1"
REVIEW_REASONS = frozenset({"product_configuration_review", "product_information_review"})
_PRODUCT = re.compile(r"худі|худи|hoodie|футболк\w*|t-?shirt|товар\w*|product|одяг|одежд", re.I)
_ASK = re.compile(r"\?|\b(?:what|which|have|is|are|can|tell|available)\b|(?:який|яка|какой|какая|есть|чи|підкаж|подскаж|уточн)\w*", re.I)
_WISH = re.compile(r"\b(?:want|need|choose|хочу|хотів|хотел|потріб\w*|нуж\w*|обира\w*|выбир\w*)\b", re.I)
_CORRECTION = re.compile(r"\b(?:actually|instead|rather|better|краще|лучше)\b|\b(?:не|not)\s+[^.!?;]{0,40}[,]\s*(?:а|but)\b", re.I)
_HISTORY = re.compile(r"\b(?:раніше|раньше|ранее|вчера|учора|previously|earlier|formerly|yesterday|used\s+to)\b|(?:минул\w*|прошл\w*|previous|old)\s+(?:замовлен\w*|заказ\w*|order)", re.I)
_REJECT = re.compile(r"\b(?:не|not|no)\s+(?:цікав\w*|интерес\w*|хочу|потріб\w*|нуж\w*|want|need|interested)|(?:не\s+(?:треба|потрібно|нужно)|don't\s+need)", re.I)
_SPEC = re.compile(r"фурн[іи]тур\w*|люверс\w*|резинк\w*|манжет\w*|заст[іе]б\w*|блискавк\w*|молни\w*|ґудзик\w*|пуговиц\w*|заклеп\w*|матер[іи]ал\w*|тканин\w*|состав\w*|склад\s+(?:тканин\w*|матер[іи]ал\w*)|\b(?:eyelets?|grommets?|rib(?:bing)?|cuffs?|zipper\w*|hardware|buttons?|composition|fabric(?:\s+content)?)\b", re.I)
_NEGATED = re.compile(r"\b(?:не|ні|not|no|без|without)(?:\s+(?:хочу|want|need|потріб\w*|нуж\w*|цвет|колір|color)){0,2}\s*$|\binstead\s+of\s*$", re.I)
_SPEC_ASSERTION = re.compile(r"метал\w*|\bmetal\w*\b|пластик\w*|\bplastic\b|хлоп\w*|бавовн\w*|\bcotton\b|поліестер\w*|полиэстер\w*|\bpolyester\b|\b\d{1,3}\s*%|^\s*(?:так|да|yes)\b|^\s*(?:it|they|this|that)\s+(?:does|do|is|are|has|have|can)\b", re.I)
_PRODUCTION = re.compile(r"зробимо|сделаем|виготов\w*|изготов\w*|відправ\w*|отправ\w*|\b(?:make|manufactur\w*|produce|ship|dispatch)\b", re.I)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def source_refs(sources):
    return [{"message_id": row.get("message_id"), "source_digest": row.get("source_digest"),
             "content_digest": _digest({key: row.get(key) for key in ("message_id", "role", "text", "provider_created_at", "source_digest")})}
            for row in sources if row.get("role") == "user"]


def cart_identity(capture):
    return _digest({key: (capture or {}).get(key) for key in
                   ("schema", "scope", "session_id", "generation", "selection_revision", "active_line_id", "lines")})


def _color(value):
    from management.services.ig_commerce_turns import _COLOR_WORDS, _find_prefix_value
    value = str(value or "").casefold().replace("ё", "е")
    return _find_prefix_value(value, _COLOR_WORDS) or value


def _known_color(wanted, state):
    color = state.get("color") or {}
    if (color.get("verified_resolution") is True and type(color.get("selected_variant_id")) is int
        and color["selected_variant_id"] > 0 and _color(color.get("resolved_choice")) == wanted):
        return True
    options = color.get("options") or []
    return any(_color(row.get("name") or row.get("label") or row.get("color") or "") == wanted
               for row in options if isinstance(row, dict))


def capture_product_inquiries(sources, *, cart_capture=None, readiness_by_line=None, profile_language="uk"):
    """Classify a bounded *already owned* current source/catalog observation."""
    from management.services.ig_commerce_turns import (_canonical_source_size, _current_source_text,
                                                     _SIZE_RE, _source_size, _find_prefix_value, _COLOR_WORDS,
                                                     _withdrawn_preference, _word_pattern)
    from management.services.ig_reply_language import own_reply_text, resolve_own_source_reply_language

    rows = list(sources or [])
    empty = {"schema": VERSION, "status": "none", "reason": "", "inquiries": [], "source_refs": [],
             "cart_identity": cart_identity(cart_capture), "locale": ""}
    if (not rows or len(rows) > 16 or any(not isinstance(row, dict) for row in rows)
        or any(type(row.get("message_id")) is not int or not isinstance(row.get("text", ""), str)
               or len(row.get("text", "")) > 64000 for row in rows)
        or len({row.get("message_id") for row in rows}) != len(rows)):
        return {**empty, "gap": "product_inquiry_source_bound"}
    owned = [row for row in rows if row.get("role") == "user"]
    watermark = max((row["message_id"] for row in owned), default=0)
    capture = cart_capture or {}
    if any(type(identity) is not int or identity > watermark for identity in (capture.get("fence") or {}).get("source_ids", [])):
        return {**empty, "gap": "product_inquiry_cart_newer_than_source"}
    states = readiness_by_line or {}
    lines = capture.get("lines") or []
    if len(lines) > 20 or any(not isinstance(row, dict) for row in lines):
        return {**empty, "gap": "product_inquiry_cart_bound"}
    active_id = capture.get("active_line_id")
    active = next((row for row in lines if row.get("line_id") == active_id), lines[0] if len(lines) == 1 else {})
    state = states.get(active.get("line_id"), {})
    product_id = ((state.get("product") or {}).get("id") or active.get("product_id")
                  or ((active.get("fields") or {}).get("product_id") or {}).get("value"))
    inquiries = []
    for source in owned:
        source_line = active
        source_product = product_id
        if len(lines) > 1:
            matches = [line for line in lines if any((field.get("source") or {}).get("source_message_id") == source["message_id"]
                for field in (line.get("fields") or {}).values() if isinstance(field, dict))]
            source_line = matches[0] if len(matches) == 1 else {}
            source_product = (((source_line.get("fields") or {}).get("product_id") or {}).get("value")
                              or source_line.get("product_id"))
            state = states.get(source_line.get("line_id"), {})
        raw = own_reply_text(_current_source_text(source.get("text", "")))
        affirmed_color = _find_prefix_value(raw, _COLOR_WORDS)
        correction = bool(_CORRECTION.search(raw))
        # An explicit correction supersedes the older wish for that axis.
        changed = set()
        if _source_size(raw):
            changed.add("size")
        if _find_prefix_value(raw, _COLOR_WORDS) or _withdrawn_preference(raw, _COLOR_WORDS):
            changed.add("color")
        inquiries = [item for item in inquiries if not (item["axis"] in changed
            and item["line_id"] == source_line.get("line_id", "")
            and item["product_id"] == source_product)]
        for clause in re.split(r"[.!;,\n]+|\b(?:але|но|but|а)\b", raw):
            if not clause.strip() or _HISTORY.search(clause):
                continue
            product_context = bool(source_product or _PRODUCT.search(raw))
            asking = bool(_ASK.search(clause))
            wishing = bool(_WISH.search(clause) or correction or affirmed_color)
            if not product_context or not (asking or wishing):
                continue
            candidates = []
            color_matches = [match for needle in _COLOR_WORDS for match in re.finditer(_word_pattern(needle), clause.replace("ё", "е").replace("Ё", "Е"), re.I)]
            for match in color_matches:
                if not _NEGATED.search(clause[:match.start()]):
                    wanted = _color(match.group())
                    if not _known_color(wanted, state) and (source_product or wanted in {"olive", "purple", "beige", "burgundy", "orange"}):
                        candidates.append(("color", wanted, "requested_color_unverified"))
            size_state = state.get("size") or {}
            available = {_canonical_source_size(str(value)) for value in size_state.get("available") or []}
            for match in _SIZE_RE.finditer(clause):
                if _NEGATED.search(clause[:match.start()]):
                    continue
                wanted = _canonical_source_size(match.group())
                if wanted not in available and (available or wanted in {"XXS", "XXXS", "XXXL", "4XL", "5XL"}
                                                or size_state.get("requested_unavailable") == wanted):
                    candidates.append(("size", wanted, "requested_size_unverified"))
            spec = _SPEC.search(clause)
            # A short composition question needs a product; warehouse/order
            # phrases containing "склад" are deliberately not spec questions.
            ui_button = bool(re.search(r"buttons?", clause, re.I) and re.search(r"click|tap|press|pay|checkout|клік|натис|нажат|оплат", clause, re.I))
            if asking and not _REJECT.search(clause) and not ui_button and (spec or re.search(r"(?:який|яка|what)\s+(?:склад|composition)\b", clause, re.I)):
                candidates.append(("information", spec.group() if spec else "composition", "product_fact_unverified"))
            for axis, wanted, reason in candidates:
                item = {"source_message_id": source["message_id"], "source_digest": source.get("source_digest", ""),
                        "line_id": source_line.get("line_id", ""), "product_id": source_product,
                        "axis": axis, "requested_value": wanted, "question": clause.strip()[:500], "reason": reason,
                        "catalog_authority": False, "checkout_authority": False}
                if not any((row["source_message_id"], row["axis"], row["requested_value"]) ==
                           (item["source_message_id"], axis, wanted) for row in inquiries):
                    inquiries.append(item)
    language = resolve_own_source_reply_language(sources=owned, profile_language=profile_language)
    result = {**empty, "status": "manager_required" if inquiries else "none", "inquiries": inquiries[:32],
              "source_refs": source_refs(owned), "locale": language.template_family,
              "reason": ("product_configuration_review" if any(item["axis"] != "information" for item in inquiries)
                         else "product_information_review" if inquiries else "")}
    result["digest"] = _digest(result)
    return result


@dataclass(frozen=True)
class ProductInquiryCapture:
    """Backend capability, never accepted from JSON/provider controls."""

    snapshot_digest: str
    source_refs_json: str
    cart_json: str
    readiness_json: str
    profile_language: str

    def recompute(self, revision):
        sources = (revision.bundle_snapshot or {}).get("sources") or []
        if any(not isinstance(value, str) or len(value) > 262144 for value in
               (self.source_refs_json, self.cart_json, self.readiness_json)):
            return None
        try:
            refs, cart, readiness = json.loads(self.source_refs_json), json.loads(self.cart_json), json.loads(self.readiness_json)
        except (ValueError, TypeError, RecursionError):
            return None
        if (self.snapshot_digest != revision.snapshot_digest or refs != source_refs(sources)
            or not isinstance(cart, dict) or not isinstance(readiness, dict)):
            return None
        scope = cart.get("scope") or {}
        if scope and scope.get("client_id") != revision.client_id:
            return None
        result = capture_product_inquiries(sources, cart_capture=cart,
            readiness_by_line=readiness, profile_language=self.profile_language)
        if result.get("gap"):
            return None
        result["snapshot_digest"] = revision.snapshot_digest
        return result


def _resolved_assisted_inquiries(revision, result):
    from management.services.ig_assisted_commerce_context import AssistedCommerceContext
    context = getattr(revision, "_assisted_commerce_context", None)
    if (not isinstance(context, AssistedCommerceContext) or context.revision_id != revision.pk
        or context.revision_digest != revision.snapshot_digest):
        return result
    remaining = [row for row in result.get("inquiries", ()) if not context.resolves_inquiry(row)]
    if len(remaining) == len(result.get("inquiries", ())):
        return result
    resolved = {**deepcopy(result), "inquiries": remaining,
        "assisted_resolution": context.reference,
        "status": "manager_required" if remaining else "none",
        "reason": "product_configuration_review" if any(row["axis"] != "information" for row in remaining)
                  else "product_information_review" if remaining else ""}
    resolved["digest"] = _digest({key: value for key, value in resolved.items() if key not in {"digest", "snapshot_digest"}})
    return resolved


def product_inquiries_for_revision(revision, *, response_plan=None):
    """Reuse captured catalog state, never query a newer product/conversation."""
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    if response_plan is not None:
        readiness = {}
        for row in response_plan.line_plans:
            data = row.as_dict()
            readiness[row.line_id] = {"product": {"id": data["configuration"].get("product_id")},
                "size": {"available": data["authority"].get("sizes", []),
                         "requested_unavailable": data["choices"].get("size", "") if data["configuration"].get("requested_size_unavailable") else ""},
                "color": {"options": [{"name": label} for label in data["authority"].get("colors", [])]}}
        capture = response_plan.source_cart_capture
        # Price/checkout authority may be empty because the current size has
        # zero stock. Its separately captured active readiness still supplies
        # the actual catalogue selector contract, without new reads or guesses.
        active_id = capture.get("active_line_id")
        if active_id in readiness and response_plan.readiness_snapshot:
            readiness[active_id] = deepcopy(response_plan.readiness_snapshot)
        if not readiness:
            line_id = capture.get("active_line_id") or next((row.get("line_id") for row in capture.get("lines", [])), "")
            readiness[line_id] = deepcopy(response_plan.readiness_snapshot)
        active_id = active_id or next(iter(readiness), "")
        if active_id in readiness:
            color = readiness[active_id].get("color") or {}
            if (readiness[active_id].get("applicability_known") is True
                and type(color.get("selected_variant_id")) is int and color["selected_variant_id"] > 0):
                color.update(resolved_choice=response_plan.choices.get("color"), verified_resolution=True)
        capability = ProductInquiryCapture(revision.snapshot_digest, json.dumps(source_refs(sources), sort_keys=True),
            json.dumps(capture, sort_keys=True), json.dumps(readiness, sort_keys=True), revision.client.language)
        result = capability.recompute(revision) or {"status": "none", "reason": "", "gap": "product_inquiry_capture_invalid"}
        revision._product_inquiries = result
        revision._product_inquiry_capture = capability
        return _resolved_assisted_inquiries(revision, result)
    cached = getattr(revision, "_product_inquiries", None)
    if cached and cached.get("snapshot_digest") == revision.snapshot_digest and cached.get("source_refs") == source_refs(sources):
        return _resolved_assisted_inquiries(revision, deepcopy(cached))
    coverage = (revision.action_receipts or {}).get("response_coverage") or {}
    stored = coverage.get("product_inquiries") or {}
    material = {key: value for key, value in stored.items() if key not in {"digest", "snapshot_digest"}}
    original = (revision.generation_proposal or {}).get("source_cart_capture")
    if (coverage.get("snapshot_digest") == revision.snapshot_digest
        and coverage.get("digest") == _digest({key: value for key, value in coverage.items() if key != "digest"})
        and stored.get("schema") == VERSION and stored.get("digest") == _digest(material)
        and stored.get("snapshot_digest") == revision.snapshot_digest and stored.get("source_refs") == source_refs(sources)
        and (not original or stored.get("cart_identity") == cart_identity(original))):
        return _resolved_assisted_inquiries(revision, deepcopy(stored))
    holding = (revision.action_receipts or {}).get("technical_holding") or {}
    stored = holding.get("product_inquiries") or {}
    material = {key: value for key, value in stored.items() if key not in {"digest", "snapshot_digest"}}
    if (holding.get("snapshot_digest") == revision.snapshot_digest
        and stored.get("schema") == VERSION and stored.get("digest") == _digest(material)
        and stored.get("snapshot_digest") == revision.snapshot_digest and stored.get("source_refs") == source_refs(sources)):
        return _resolved_assisted_inquiries(revision, deepcopy(stored))
    # Source-only questions remain recognizable without a selection. This
    # cannot invent supported sizes, product identity or catalogue authority.
    result = capture_product_inquiries(sources, profile_language=revision.client.language)
    result["snapshot_digest"] = revision.snapshot_digest
    return _resolved_assisted_inquiries(revision, result)


def inquiry_prompt(disposition):
    if disposition.get("status") != "manager_required":
        return ""
    return ("[SOURCE-BOUND PRODUCT INQUIRIES]\n" + json.dumps(disposition, ensure_ascii=False, separators=(",", ":"))
            + "\nAcknowledge these exact wishes/questions naturally in the conversation language and say the manager will check. "
            "Emit manager=true. Do not substitute another colour/size. These are requests, not catalogue/stock/specification facts. "
            "Do not answer an unverified hardware/material/composition question confidently. No price, stock, timing or checkout authority is granted. "
            "Continue answering independently verified unrelated questions; manager review does not complete an order or payment.")


def inquiry_reply(disposition):
    """A check commitment, never an already-sent notification assertion."""
    locale = disposition.get("locale")
    if locale not in {"uk", "ru", "en"} or disposition.get("status") != "manager_required":
        return ""
    copy = {"uk": ("Уточню у менеджера", "ці параметри та можливість виконати ваш запит", "деталі товару за вашим питанням"),
            "ru": ("Уточню у менеджера", "эти параметры и возможность выполнить ваш запрос", "детали товара по вашему вопросу"),
            "en": ("I will check with our manager", "these options and whether we can fulfil your request", "the product details you asked about")}[locale]
    wishes = list(dict.fromkeys(item["requested_value"] for item in disposition["inquiries"] if item["axis"] != "information"))
    labels = {"uk": {"olive": "оливковий", "purple": "фіолетовий", "beige": "бежевий", "burgundy": "бордо", "orange": "помаранчевий", "red": "червоний", "yellow": "жовтий", "brown": "коричневий", "black": "чорний", "white": "білий", "blue": "синій", "green": "зелений", "pink": "рожевий", "grey": "сірий"},
              "ru": {"olive": "оливковый", "purple": "фиолетовый", "beige": "бежевый", "burgundy": "бордо", "orange": "оранжевый", "red": "красный", "yellow": "жёлтый", "brown": "коричневый", "black": "чёрный", "white": "белый", "blue": "синий", "green": "зелёный", "pink": "розовый", "grey": "серый"}}
    wishes = [labels.get(locale, {}).get(value, value) for value in wishes]
    detail = copy[1] + " (" + ", ".join(wishes) + ")" if wishes else copy[2]
    return f"{copy[0]}: {detail}."


def validate_inquiry_response(disposition, response):
    """A deterministic handoff disposition, not a blanket keyword exception."""
    if disposition.get("status") != "manager_required":
        return ""
    control, text = response.control, response.reply_text
    if control.get("paylink") or control.get("payment") or control.get("items") or control.get("item"):
        return "product_inquiry_checkout_unverified"
    if not control.get("manager"):
        return "product_inquiry_handoff_required"
    if any(row["axis"] != "information" for row in disposition["inquiries"]):
        from management.services.ig_reply_truth import (_claim_sentences, _product_availability_claim,
            _TIMING_RE, _ARRIVAL_RE, _TIMING_GUARANTEE_RE, _timing_nonclaim)
        from management.services.ig_reply_language import own_reply_text
        clauses = list(_claim_sentences(own_reply_text(text), ()))
        if any(_product_availability_claim(clause) for clause in clauses):
            return "product_inquiry_availability_unverified"
        for clause in clauses:
            for part in re.split(r"[,;:]|\b(?:але|но|but)\b", clause):
                if _HISTORY.search(part):
                    continue
                for pattern in (_TIMING_RE, _ARRIVAL_RE, _TIMING_GUARANTEE_RE, _PRODUCTION):
                    if any(not _timing_nonclaim(part, match.start()) for match in pattern.finditer(part)):
                        return "product_inquiry_fulfillment_unverified"
    # Each unanswered spec clause requires uncertainty/check language. An
    # appended "manager" control cannot bless a confident material claim.
    information = any(row["axis"] == "information" for row in disposition["inquiries"])
    from management.services.ig_reply_language import own_reply_text
    for clause in re.split(r"[.!;,:\n]+|\b(?:але|но|but)\b", own_reply_text(text)):
        match = _SPEC.search(clause) or _SPEC_ASSERTION.search(clause)
        if information and match and "?" not in clause:
            prefix = clause[:match.start()]
            if not re.search(r"^\s*(?:(?:я\s+)?(?:уточн\w*|перевір\w*|провер\w*|не\s+(?:зна\w*|мож\w*\s+підтверд\w*))|(?:i(?:['’]ll)?|we(?:['’]ll)?)\s+(?:will\s+)?(?:check|clarif\w*)|(?:i|we)\s+(?:don't|do\s+not)\s+know|let\s+(?:me|us)\s+check|потрібно\s+уточн\w*)", prefix, re.I):
                return "product_information_unverified"
    return ""
