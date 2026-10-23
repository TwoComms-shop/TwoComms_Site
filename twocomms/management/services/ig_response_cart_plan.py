"""Pure, bounded response requirements over an already captured source cart.

This adapter does not authenticate input, read sources, or grant checkout effects.
Its caller owns canonical source capture and exact catalog authority per line.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

MAX_LINES = 16
MAX_OBLIGATIONS = 64
FIELDS = ("product_id", "model_query", "size", "fit_option_code", "color", "quantity", "garment_type", "purchase_requested")
SCOPE_KEYS = ("client_id", "episode_id", "order_id", "reset_id", "reset_floor", "source_namespace")


def admitted_checkout_line_ids(binding, original_capture, *, local=False):
    """Use only a backend checkout owner's complete, exact positional proof.

    Structural verification adds no checkout authority. The caller supplies
    this argument only after its normal checkout/source/permission gates pass.
    Model controls and local acknowledgements never discharge purchase debt.
    """
    if local or binding is None or not isinstance(original_capture, dict):
        return frozenset()
    from management.services.ig_revision_cart_binding import validate_cart_binding

    validated = validate_cart_binding(binding=binding, current_capture=original_capture,
        expected_scope=original_capture.get("scope"))
    if validated.status != "ready":
        return frozenset()
    rows = validated.binding["quote_line_map"]
    expected = {(row["line_id"], row["recipient_id"]) for row in original_capture["lines"]}
    actual = {(row["line_id"], row["recipient_id"]) for row in rows}
    return frozenset(actual) if actual == expected and len(rows) == len(expected) else frozenset()


@dataclass(frozen=True)
class ResponseLinePlan:
    """JSON-backed projection: callers receive detached, inspectable values."""
    _payload_json: str

    def as_dict(self):
        return json.loads(self._payload_json)

    @property
    def line_id(self):
        return self.as_dict()["line_id"]

    @property
    def recipient_id(self):
        return self.as_dict()["recipient_id"]


def source_line_preferences(capture, row, *, source_watermark=0):
    """Retain each field's accepted proof, never raw snapshot/default values."""
    scope = (row.get("source_selection") or {}).get("scope") or {}
    expected = {**(capture.get("scope") or {}), "session_id": capture.get("session_id"),
        "generation": capture.get("generation"), "revision": capture.get("selection_revision"),
        "line_id": row.get("line_id"), "recipient_id": row.get("recipient_id", "self"),
        "active_index": row.get("index")}
    if type(expected["active_index"]) is not int or expected["active_index"] < 0:
        return {}
    if "active_index" in scope and (type(scope["active_index"]) is not int or scope["active_index"] != expected["active_index"]):
        return {}
    if any(key not in scope or scope[key] != expected.get(key) or type(scope[key]) is not type(expected.get(key))
        for key in (*SCOPE_KEYS, "session_id", "generation", "revision", "line_id", "recipient_id")):
        return {}
    values, evidence, cleared = {}, {}, {}
    source_ids = set((capture.get("fence") or {}).get("source_ids") or ())
    for key, field in (row.get("fields") or {}).items():
        if key not in FIELDS or not isinstance(field, dict):
            continue
        proof = field.get("source") or {}
        source_id = proof.get("source_message_id")
        if (not isinstance(source_id, int) or isinstance(source_id, bool) or source_id not in source_ids
            or source_id < expected["reset_floor"] or (source_watermark and source_id > source_watermark)
            or not re.fullmatch(r"[a-f0-9]{64}", str(proof.get("source_digest") or ""))
            or not proof.get("transition_id")):
            continue
        if field.get("status") == "unknown" and field.get("value") is None:
            cleared[key] = proof
        elif field.get("status") in {"confirmed", "ambiguous"} and field.get("value") is not None:
            values[key], evidence[key] = field["value"], proof
    planning = (row.get("source_selection") or {}).get("planning")
    return {**expected, "values": values, "evidence": evidence, "cleared": cleared,
        **({"planning": planning} if isinstance(planning, dict) else {})}


def build_cart_response_lines(*, capture, sources, readiness_by_line, authority_by_line, single_builder):
    """Return all source-backed line plans and a named finite gap; no truncation."""
    rows = capture.get("lines") if isinstance(capture, dict) else None
    if (capture.get("schema") != "source-selections.v1" or capture.get("status") != "captured"
        or not capture.get("coverage_complete") or not isinstance(rows, list)
        or not rows or len(rows) > MAX_LINES or any(not isinstance(row, dict) or not row.get("line_id") or not isinstance(row.get("index"), int) for row in rows)
        or len({row.get("line_id") for row in rows}) != len(rows)):
        return (), "response_plan_cart_capture_unavailable"
    current_ids = {row["message_id"] for row in sources if row.get("role") == "user"}
    watermark = max(current_ids, default=0)
    plans, count = [], 0
    for row in rows:
        preferences = source_line_preferences(capture, row, source_watermark=watermark)
        if not preferences or len(preferences["values"]) != sum(field.get("value") is not None for key, field in (row.get("fields") or {}).items() if key in FIELDS):
            return (), "response_plan_line_source_unavailable"
        line_id = row["line_id"]
        base = single_builder(preferences, readiness_by_line.get(line_id) or {}, authority_by_line.get(line_id))
        payload = base.as_dict()
        # Read-only catalogue identity belongs to this exact captured line.
        # Keep its full signed proof server-side; prompt_projection uses an
        # explicit presentation allowlist and does not expose configuration.
        from management.services.ig_source_cart_catalog import verified_catalog_color_identity
        identity = ((readiness_by_line.get(line_id) or {}).get("color") or {}).get("catalog_color_identity")
        if verified_catalog_color_identity(identity, product_id=payload["configuration"].get("product_id"),
                capture=capture, line=row):
            payload["configuration"]["catalog_color_identity"] = identity
        if preferences.get("planning"):
            payload["configuration"]["recipient_planning"] = preferences["planning"]
        obligations = []
        for key, value in preferences["values"].items():
            proof = preferences["evidence"][key]
            source_id = proof["source_message_id"]
            if source_id not in current_ids:
                continue
            obligations.append({"id": f"{source_id}:{line_id}:{key}", "kind": key,
                "source_message_id": source_id, "line_id": line_id,
                "recipient_id": row.get("recipient_id", "self"), "value": value, "evidence": proof})
        for key, proof in preferences["cleared"].items():
            if proof["source_message_id"] in current_ids:
                kind = "withdrawal:" + {"fit_option_code": "fit"}.get(key, key)
                obligations.append({"id": f"{proof['source_message_id']}:{line_id}:{kind}", "kind": kind,
                    "source_message_id": proof["source_message_id"], "line_id": line_id,
                    "recipient_id": row.get("recipient_id", "self"), "value": None, "evidence": proof})
        count += len(obligations)
        if count > MAX_OBLIGATIONS or any(len(item["id"]) > 160 for item in obligations):
            return (), "response_plan_obligation_bound"
        payload.update(line_id=line_id, recipient_id=row.get("recipient_id", "self"),
            index=row["index"], scope={**preferences, "head_digest": (capture.get("fence") or {}).get("snapshot_digest"),
                "capture_digest": capture.get("capture_digest")}, obligations=obligations)
        for key in ("values", "evidence", "cleared"):
            payload["scope"].pop(key, None)
        plans.append(ResponseLinePlan(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
    return tuple(plans), ""


def matching_line_ids(clause, line_plans):
    """A clause needs a unique explicit identity; duplicated SKUs need scope."""
    rows = [plan.as_dict() for plan in line_plans]
    text = clause.casefold()
    from management.services.ig_recipient_planning import RECIPIENT_WORDS, recipient_mentions
    aliases = {**RECIPIENT_WORDS, "хлопцю": "boyfriend", "парню": "boyfriend",
        "дівчині": "girlfriend", "девушке": "girlfriend", "мамі": "mother", "маме": "mother",
        "татові": "father", "папе": "father", "подрузі": "friend_female", "подруге": "friend_female"}
    dative = {"собі", "мені", "мне", "хлопцю", "парню", "дівчині", "девушке", "сестрі", "сестре", "брату", "братові"}
    roles = {role for word, role in aliases.items()
        if re.search(r"\b(?:для|for)\s+(?:(?:my|your|ваш\w*|мо\w*)\s+)?" + re.escape(word) + r"(?!\w)", text)
        or (word in dative and re.search(r"^\s*" + re.escape(word) + r"(?!\w)", text))
        or re.search(r"(?<!\w)" + re.escape(role) + r"\s*:", text)}
    roles.update(row[0] for row in recipient_mentions(clause)[0])
    # Dynamic recipient IDs retain literal matching; only the canonical
    # source-backed row can own an alias. Generic 'you' is never a self scope.
    roles.update(str(row["recipient_id"]).casefold() for row in rows if row["recipient_id"] not in set(aliases.values())
        if re.search(r"(?<!\w)" + re.escape(str(row["recipient_id"]).casefold()) + r"(?!\w)", text))
    ordinal = []
    for row in rows:
        number = row["index"] + 1
        if re.search(r"(?:позиці[яїю]|позици[яию]|item|line)\s*(?:№|#)?\s*" + str(number) + r"(?!\d)", text):
            ordinal.append(row["line_id"])
    if ordinal:
        if len(ordinal) != 1:
            return ()
        selected = next(row for row in rows if row["line_id"] == ordinal[0])
        if roles and roles != {str(selected["recipient_id"]).casefold()}:
            return ()
        for other in rows:
            recipient = str(other["recipient_id"]).casefold()
            if recipient != str(selected["recipient_id"]).casefold() and re.search(r"(?<!\w)" + re.escape(recipient) + r"(?!\w)", text):
                return ()
            title = str(other["configuration"].get("product_title") or "").casefold()
            if (other["line_id"] != ordinal[0] and title and title != str(selected["configuration"].get("product_title") or "").casefold()
                and re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text)):
                return ()
        return tuple(ordinal)
    if roles:
        candidates = [row for row in rows if str(row["recipient_id"]).casefold() in roles]
        if len(roles) != 1 or len(candidates) != 1:
            return ()
        selected = candidates[0]
        # A different captured exact title is an identity conflict even when
        # the beneficiary is explicit.
        for other in rows:
            title = str(other["configuration"].get("product_title") or "").casefold()
            if (other["line_id"] != selected["line_id"] and title
                and title != str(selected["configuration"].get("product_title") or "").casefold()
                and re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text)):
                return ()
        return (selected["line_id"],)
    candidates = []
    garments = {"hoodie": r"\bhoodie\b|\bхуді\b|\bхуди\b", "tshirt": r"\bt-?shirt\b|\bфутболк\w*"}
    for row in rows:
        title = str(row["configuration"].get("product_title") or "").casefold()
        product = row["choices"].get("product_id")
        garment = garments.get(str(row["choices"].get("garment_type") or ""))
        title_match = bool(title and re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text))
        product_match = bool(product and re.search(r"(?:id|товар|product)\s*[:=#]?\s*" + re.escape(str(product)) + r"(?!\d)", text))
        garment_match = bool(garment and re.search(garment, text))
        if not (title_match or product_match or garment_match):
            continue
        recipient = str(row["recipient_id"])
        same = [other for other in rows if other["choices"].get("product_id") == product and product]
        if len(same) > 1 and not re.search(r"(?<!\w)" + re.escape(recipient.casefold()) + r"(?!\w)", text):
            continue
        candidates.append(row["line_id"])
    return tuple(candidates) if len(candidates) == 1 else ()


def selected_title_text(text, title):
    """Unwrap only literal names present in the captured canonical label."""
    from management.services.ig_response_plan import _PRODUCT_NAME_QUOTES
    names = [title] if title else []
    embedded = list(_PRODUCT_NAME_QUOTES.finditer(title)) if title else []
    if len(embedded) == 1:
        names.append(next(value for value in embedded[0].groups() if value is not None))
    for name in names:
        text = re.sub(r'["\'«“‘]' + re.escape(name) + r'["\'»”’]', lambda _match: name, text, flags=re.I)
    return text


def _scoped_request_tokens(text, *, choices, title="", extra_words=()):
    """Closed question vocabulary plus exact source choices; no named fallback."""
    from management.services.ig_response_plan import _unquoted_text, CHOICE_ALIASES
    from management.services.ig_reply_language import own_reply_text
    value = str(text or "").strip()
    value = selected_title_text(value, title)
    if not value or len(value) > 1000 or _unquoted_text(value) != value or own_reply_text(value) != value:
        return ()
    from management.services.ig_commerce_turns import _SIZE_RE, _canonical_source_size
    from product_catalog.size_grid_services import normalize_size_value
    selected_size = _canonical_source_size(normalize_size_value(choices.get("size") or ""))
    size_spans = re.compile(r"(?<!\w)[2-5]\s+[xх]\s*[lл](?!\w)|" + _SIZE_RE.pattern, re.I)
    if selected_size:
        value = size_spans.sub(lambda match: " " if _canonical_source_size(
            normalize_size_value(match.group())) == selected_size else match.group(), value)
    for selected in (title, *CHOICE_ALIASES.get(
            str(choices.get("color") or ""), (str(choices.get("color") or ""),))):
        if selected:
            value = re.sub(r"(?<!\w)" + re.escape(selected) + r"(?!\w)", " ", value, flags=re.I)
    words = re.findall(r"[\w’']+", value.casefold())
    allowed = set("а чи є в у цей це ця цієї цього його її ще і та який яка наявності наявність розмір розмірі "
        "есть ли это эта этот этой этого его её размер размере наличии наличие и какая "
        "is are do you have this that my the selected it its in of for size stock available availability "
        "худі худи hoodie футболка футболки t shirt tshirt товар товара товару позиція позиції позиция item one "
        "колір кольорі кольору кольором цвет цвете цвета цветом color colour".split())
    allowed.update(extra_words)
    return tuple(words) if words and all(word in allowed for word in words) else ()


def stock_question(text, *, size="", color="", title=""):
    # A price adjunct still asks about the same source-selected SKU. Keep the
    # closed vocabulary and exact-choice checks: this only identifies the ask;
    # current-review capture and reply validation still supply all authority.
    words = _scoped_request_tokens(text, choices={"size": size, "color": color}, title=title,
        extra_words=("ціна", "вартість", "скільки", "коштує", "цена", "стоимость", "сколько", "стоит",
            "price", "cost", "what", "what's", "what’s", "how", "much", "does", "and"))
    return bool(words and set(words) & {"є", "есть", "наявності", "наявність", "наличии", "наличие", "have", "stock", "available", "availability"})


def anaphoric_price_line_id(text, line_plans, *, active_line_id, readiness_by_line):
    """One active source SKU and a clear price continuation, never a cart default."""
    if len(line_plans) != 1 or line_plans[0].line_id != active_line_id:
        return ""
    row = line_plans[0].as_dict()
    product_id = row["choices"].get("product_id")
    if (not product_id or product_id != row["configuration"].get("product_id")
        or type((row["evidence"].get("product_id") or {}).get("source_message_id")) is not int):
        return ""
    words = _scoped_request_tokens(text, choices=row["choices"], title=row["configuration"].get("product_title", ""), extra_words=(
        "ціна", "вартість", "скільки", "коштує", "цена", "стоимость", "сколько", "стоит",
        "price", "cost", "what", "what's", "what’s", "how", "much", "does", "and"))
    if not words or not set(words) & {"ціна", "вартість", "коштує", "цена", "стоимость", "стоит", "price", "cost", "much"}:
        return ""
    from management.services.approved_public_facts import _captured_base, _GARMENT_WORDS
    if any(pattern.search(text) for pattern in _GARMENT_WORDS.values()) and not _captured_base(
            readiness_by_line.get(active_line_id), text)[1]:
        return ""
    return active_line_id


def anaphoric_price_reply_line_id(text, line_plans, *, active_line_id, readiness_by_line, obligations):
    """A plain price answer inherits only its one current, source-owned price ask."""
    if not any(row.get("kind") == "info:price" and row.get("line_id") == active_line_id for row in obligations):
        return ""
    if len(line_plans) != 1 or line_plans[0].line_id != active_line_id:
        return ""
    row = line_plans[0].as_dict()
    return active_line_id if single_price_subject(text, choices=row["choices"],
        title=row["configuration"].get("product_title", ""),
        readiness=readiness_by_line.get(active_line_id)) else ""


def single_price_subject(text, *, choices, title="", readiness=None):
    """Subject binding only; the caller still validates every monetary value."""
    from management.services.ig_reply_truth import _MONEY_RE
    if "?" in text or not _MONEY_RE.search(text):
        return False
    value = _MONEY_RE.sub(" ", text).strip(" .!;:—–-")
    if not value or re.fullmatch(r"(?:it\s+is|it['’]s|це|это)", value, re.I):
        return True
    words = _scoped_request_tokens(value, choices=choices, title=title, extra_words=(
        "ціна", "вартість", "коштує", "цена", "стоимость", "стоит", "price", "cost", "costs", "it's", "it’s"))
    if not words or not set(words) & {"ціна", "вартість", "коштує", "цена", "стоимость", "стоит", "price", "cost", "costs"}:
        return False
    if readiness is not None:
        from management.services.approved_public_facts import _captured_base, _GARMENT_WORDS
        if any(pattern.search(value) for pattern in _GARMENT_WORDS.values()) and not _captured_base(readiness, value)[1]:
            return False
    return True


def line_clauses(text, line_plans):
    """Identity cannot bleed from a sibling sentence or quoted report."""
    from management.services.ig_response_plan import _unquoted_text
    result = {plan.line_id: [] for plan in line_plans}
    for clause in scoped_claim_clauses(_unquoted_text(text)):
        matches = matching_line_ids(clause, line_plans)
        if len(matches) == 1:
            result[matches[0]].append(clause)
    return result


def _configuration_apposition(text, *, stock_tail=False):
    """Only typed, nonverbal configuration descriptors may share commas."""
    from management.services.ig_response_plan import CHOICE_ALIASES
    size = r"(?:(?:розмір(?:у|і)?|размер(?:а|е)?|size)\s*)?(?:XS|S|M|L|XL|XXL|XXXL|[2-5]\s*[xх]\s*[lл])"
    fits = r"classic|regular|oversize|оверсайз|класичн(?:ий|а|е|і|ого|ому|ою)|классическ(?:ий|ая|ое|ие|ого|ом|им|ую)"
    fit = r"(?:(?:крій|крой|fit)\s+(?:" + fits + r")|(?:" + fits + r")(?:\s+(?:крій|крой|fit))?)"
    colors = {alias.casefold() for key, aliases in CHOICE_ALIASES.items()
        if key not in {'hoodie', 'tshirt', 'classic', 'oversize'} for alias in aliases}
    if stock_tail:
        # Keep the identity on "Aurora, black, size M is in stock". The tail
        # grammar contains no new subject or other assertion; truth authority
        # still validates the complete original claim and its configuration.
        match = re.fullmatch(r"(.*?)\s+(?:(?:є|есть)(?:\s+не)?\s+(?:в|у)\s+(?:наявності|наличии)|"
            r"(?:is|are)(?:\s+not)?\s+(?:in\s+stock|available))\s*[.!]*", text.strip(), re.I)
        return bool(match and _configuration_apposition(match[1]))
    color = r"(?:(?:колір|кольору|цвет|цвета|colou?r)\s+)?(?:" + "|".join(
        re.escape(value) for value in sorted(colors, key=lambda value: (-len(value), value))) + r")"
    descriptor = r"(?:" + size + r"|" + fit + r"|" + color + r")"
    parts = text.replace('(', '').replace(')', '').split(',')
    if not 1 <= len(parts) <= 8 or len(text) > 300:
        return False
    for part in parts:
        value = part.strip(" .!")
        if not re.fullmatch(descriptor + r"(?:\s+" + descriptor + r"){0,7}", value, re.I):
            return False
    return True


def scoped_claim_clauses(text):
    """A comma can separate different line assertions; identities stay local."""
    from management.services.ig_reply_truth import _claim_sentences, _pending_manager_stock_check
    for sentence in _claim_sentences(text, ()):
        if _pending_manager_stock_check(sentence):
            yield sentence
            continue
        for section in re.split(r"[;\n]|\b(?:але|но|but)\b", sentence, flags=re.I):
            # Parenthetical size/fit descriptions belong to their named SKU;
            # their internal comma cannot detach a following price from it.
            depth, start, malformed, unbalanced, group_start = 0, 0, False, False, 0
            separators = []
            for index, char in enumerate(section):
                if char == '(':
                    if depth == 0:
                        group_start = index + 1
                    depth += 1
                elif char == ')':
                    unbalanced = unbalanced or depth == 0
                    depth = max(0, depth - 1)
                    if depth == 0:
                        malformed = malformed or not _configuration_apposition(section[group_start:index])
                elif char == ',' and depth == 0 and not (
                        index and index + 1 < len(section) and section[index - 1].isdigit() and section[index + 1].isdigit()):
                    separators.append(index)
            if malformed and not depth and not unbalanced:
                # A foreign recipient/product inside balanced parentheses is
                # part of this claim, not a fresh line after its inner comma.
                # Keep the conflicting identities visible to the line guard.
                yield section
                continue
            if depth or unbalanced:
                separators = [match.start() for match in re.finditer(r"(?<!\d),|,(?!\d)", section)]
            parts = []
            for end in (*separators, len(section)):
                parts.append(section[start:end])
                start = end + 1
            if depth or unbalanced:
                # Invalid punctuation cannot opt into descriptor regrouping.
                yield from parts
                continue
            position = 0
            while position < len(parts):
                group, following = [parts[position]], position + 1
                while following < len(parts) and len(group) < 8 and _configuration_apposition(parts[following]):
                    group.append(parts[following])
                    following += 1
                if following < len(parts) and len(group) < 8 and _configuration_apposition(parts[following], stock_tail=True):
                    group.append(parts[following])
                    following += 1
                # New product/recipient/ordinal or an independent assertion
                # does not match a descriptor and begins its own local group.
                # These commas are syntactic apposition, not claim boundaries.
                # Normalize only this proven descriptor chain for downstream
                # clause-local truth checks; the delivered reply is untouched.
                yield group[0] + ''.join(' ' + part.strip() for part in group[1:])
                position = following
