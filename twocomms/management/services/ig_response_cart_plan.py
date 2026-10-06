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
    return {**expected, "values": values, "evidence": evidence, "cleared": cleared}


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
    ordinal = []
    for row in rows:
        number = row["index"] + 1
        if re.search(r"(?:позиці[яїю]|позици[яию]|item|line)\s*(?:№|#)?\s*" + str(number) + r"(?!\d)", text):
            ordinal.append(row["line_id"])
    if ordinal:
        if len(ordinal) != 1:
            return ()
        selected = next(row for row in rows if row["line_id"] == ordinal[0])
        for other in rows:
            recipient = str(other["recipient_id"]).casefold()
            if recipient != str(selected["recipient_id"]).casefold() and re.search(r"(?<!\w)" + re.escape(recipient) + r"(?!\w)", text):
                return ()
            title = str(other["configuration"].get("product_title") or "").casefold()
            if (other["line_id"] != ordinal[0] and title and title != str(selected["configuration"].get("product_title") or "").casefold()
                and re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text)):
                return ()
        return tuple(ordinal)
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


def line_clauses(text, line_plans):
    """Identity cannot bleed from a sibling sentence or quoted report."""
    from management.services.ig_response_plan import _unquoted_text
    result = {plan.line_id: [] for plan in line_plans}
    for clause in scoped_claim_clauses(_unquoted_text(text)):
        matches = matching_line_ids(clause, line_plans)
        if len(matches) == 1:
            result[matches[0]].append(clause)
    return result


def scoped_claim_clauses(text):
    """A comma can separate different line assertions; identities stay local."""
    from management.services.ig_reply_truth import _claim_sentences
    for sentence in _claim_sentences(text, ()):
        yield from re.split(r"(?<!\d),|,(?!\d)|[;\n]|\b(?:але|но|but)\b", sentence, flags=re.I)
