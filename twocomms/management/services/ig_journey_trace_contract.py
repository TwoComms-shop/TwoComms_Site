"""Pure, bounded transcript interpretation; never business or send authority.

The caller supplies an already scoped transcript. Quotes establish attribution,
not the truth of a payment, entitlement, permission, or other reported claim.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter


SEMANTIC_NODE_KEYS = frozenset({
    "inbound", "ad_resolved_product", "catalog_discovery", "photo_reference",
    "availability_question", "stock_wait", "restock_consent", "custom_print",
    "dtf_only", "custom_brief", "mockup_current_acceptance", "prize_candidate",
    "prize_decision", "information_question", "information_resolved",
    "collaboration", "collaboration_designer", "collaboration_partnership",
    "collaboration_dropship", "collaboration_wholesale_store",
    "collaboration_creator", "collaboration_other", "business_decision",
    "employment", "employment_response", "spam_confirmed", "configured_line",
    "quoted_offer", "awaiting_payment", "payment_help", "settlement",
    "fulfillment", "objection_case", "channel_consent", "channel_grant_checked",
    "post_purchase_contact_offer", "post_sale_request", "post_sale_case",
    "ugc_assessment", "reward_entitlement", "reward_delivery", "reward_use",
    "repeat_interest", "new_purchase_interest",
})
KINDS = frozenset({"progress", "return", "retry", "objection", "negative", "waiting", "handoff"})
REASON_CODES = frozenset({
    "entered", "product_selected", "configuration_discussed", "custom_requested",
    "mockup_discussed", "certificate_presented", "awaiting_stock",
    "alternative_considered", "consent_discussed", "payment_discussed",
    "payment_problem", "external_order_reported", "fulfillment_discussed",
    "objection_raised", "objection_addressed", "manager_discussion",
    "employment_request", "collaboration_request", "information_answered",
    "changed_request", "repeat_interest",
})
MAX_STEPS, MAX_REFS_PER_STEP, MAX_UNIQUE_IDS = 12, 8, 64
_STEP_KEYS = {"from_node", "to_node", "kind", "reason_code", "confidence", "evidence", "summary"}
_ROLES = {"user", "manager", "model"}
_STEP_REJECTION_CODES = frozenset({
    "step_schema", "node_invalid", "classification_invalid", "confidence_invalid",
    "evidence_bounds", "evidence_schema", "message_id_invalid", "source_unavailable",
    "source_encoding_invalid", "quote_mismatch", "human_evidence_required",
    "unique_message_limit",
})
_GLOBAL_REJECTION_CODES = frozenset({"watermark_invalid", "transcript_index_invalid", "trace_schema"})
_REJECTION_CODES = _STEP_REJECTION_CODES | _GLOBAL_REJECTION_CODES | {"step_limit", "current_node_unsupported"}
_RESULT_KEYS = {"schema_version", "status", "authority", "provenance", "watermark", "steps", "current_node", "coverage"}
_COUNTERS = {"input_steps", "inspected_steps", "accepted_steps", "omitted_steps", "cited_message_count", "disconnected_steps"}
_COVERAGE_KEYS = _COUNTERS | {"scope", "earlier_history", "trace_start", "current_node_omitted", "reasons"}


def _normalized_text(value):
    return " ".join(value.split())


def _summary(value):
    """Optional short discussion explanation; never a source quote or authority."""
    if not isinstance(value, str):
        return ""
    value = _normalized_text(value)
    if len(value) > 140 or re.search(r"[<>\x00-\x1f\x7f]|https?://|www\.|@|(?:\+?\d[\s().-]*){7,}", value, re.I):
        return ""
    return value


def _step(raw, *, index, by_id, watermark, source_cache):
    if not isinstance(raw, dict) or (set(raw) != _STEP_KEYS and set(raw) != _STEP_KEYS - {"summary"}):
        return None, "step_schema"
    origin, target = raw["from_node"], raw["to_node"]
    if (
        not isinstance(origin, str) or not isinstance(target, str)
        or target not in SEMANTIC_NODE_KEYS
        or (origin not in SEMANTIC_NODE_KEYS and not (origin == "" and index == 0))
    ):
        return None, "node_invalid"
    if (
        not isinstance(raw["kind"], str) or raw["kind"] not in KINDS
        or not isinstance(raw["reason_code"], str) or raw["reason_code"] not in REASON_CODES
    ):
        return None, "classification_invalid"
    confidence = raw["confidence"]
    if type(confidence) not in (int, float) or not .7 <= confidence <= 1 or not math.isfinite(confidence):
        return None, "confidence_invalid"
    refs = raw["evidence"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_REFS_PER_STEP:
        return None, "evidence_bounds"
    evidence, cited = [], set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"message_id", "quote"}:
            return None, "evidence_schema"
        message_id = ref["message_id"]
        if type(message_id) is not int or not 0 < message_id <= watermark:
            return None, "message_id_invalid"
        source = by_id.get(message_id)
        if (
            not isinstance(source, dict) or type(source.get("message_id")) is not int
            or source["message_id"] != message_id
            or not isinstance(source.get("role"), str) or source["role"] not in _ROLES
            or not isinstance(source.get("text"), str)
        ):
            return None, "source_unavailable"
        if message_id not in source_cache:
            text = _normalized_text(source["text"])
            try:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            except UnicodeEncodeError:
                return None, "source_encoding_invalid"
            source_cache[message_id] = (text, digest)
        text, digest = source_cache[message_id]
        quote = ref["quote"]
        if not isinstance(quote, str) or not (quote := _normalized_text(quote)) or quote not in text:
            return None, "quote_mismatch"
        if message_id not in cited:
            evidence.append({"message_id": message_id, "role": source["role"], "source_text_sha256": digest})
            cited.add(message_id)
    if not any(ref["role"] in {"user", "manager"} for ref in evidence):
        return None, "human_evidence_required"
    return {
        "from_node": origin, "to_node": target, "kind": raw["kind"],
        "reason_code": raw["reason_code"], "confidence": float(confidence),
        "evidence": evidence, "summary": _summary(raw.get("summary", "")),
    }, ""


def normalize_journey_trace(raw, *, by_id, watermark):
    """Return allowlisted interpretation and coverage, without storing quotes.

    Invalid steps are omitted whole. No adjacency, current focus or missing
    history is inferred. Global message IDs cannot prove transcript completeness.
    """
    reasons = Counter()
    result = {
        "schema_version": 1, "status": "missing", "authority": "none",
        "provenance": "transcript_reconstruction", "watermark": None,
        "steps": [], "current_node": "",
        "coverage": {
            "scope": "provided_transcript_only", "earlier_history": "unknown",
            "input_steps": 0, "inspected_steps": 0, "accepted_steps": 0,
            "omitted_steps": 0, "cited_message_count": 0,
            "disconnected_steps": 0, "trace_start": "unobserved",
            "current_node_omitted": False, "reasons": {},
        },
    }
    coverage = result["coverage"]

    def rejected(code):
        result["status"] = "rejected"
        coverage["reasons"] = {code: 1}
        return result

    if type(watermark) is not int or watermark < 0:
        return rejected("watermark_invalid")
    result["watermark"] = watermark
    if not isinstance(by_id, dict) or any(type(key) is not int or key <= 0 for key in by_id):
        return rejected("transcript_index_invalid")
    if raw is None or raw == {}:
        return result
    if (
        not isinstance(raw, dict) or set(raw) != {"schema_version", "steps", "current_node"}
        or type(raw["schema_version"]) is not int or raw["schema_version"] != 1
        or not isinstance(raw["steps"], list) or not isinstance(raw["current_node"], str)
    ):
        return rejected("trace_schema")
    coverage["input_steps"] = len(raw["steps"])
    if len(raw["steps"]) > MAX_STEPS:
        reasons["step_limit"] = len(raw["steps"]) - MAX_STEPS
    cited, source_cache, nodes = set(), {}, set()
    for index, item in enumerate(raw["steps"][:MAX_STEPS]):
        coverage["inspected_steps"] += 1
        step, reason = _step(item, index=index, by_id=by_id, watermark=watermark, source_cache=source_cache)
        if reason:
            reasons[reason] += 1
            continue
        step_ids = {ref["message_id"] for ref in step["evidence"]}
        if len(cited | step_ids) > MAX_UNIQUE_IDS:
            reasons["unique_message_limit"] += 1
            continue
        if result["steps"] and step["from_node"] != result["steps"][-1]["to_node"]:
            coverage["disconnected_steps"] += 1
        if index == 0 and step["from_node"] == "":
            coverage["trace_start"] = "cited_start_within_window"
        result["steps"].append(step)
        cited.update(step_ids)
        nodes.update((step["from_node"], step["to_node"]))
    current = raw["current_node"]
    if current:
        if current in SEMANTIC_NODE_KEYS and current in nodes:
            result["current_node"] = current
        else:
            coverage["current_node_omitted"] = True
            reasons["current_node_unsupported"] += 1
    coverage.update({
        "accepted_steps": len(result["steps"]),
        "omitted_steps": len(raw["steps"]) - len(result["steps"]),
        "cited_message_count": len(cited), "reasons": dict(reasons),
    })
    if result["steps"]:
        result["status"] = "partial" if reasons or coverage["disconnected_steps"] else "interpretation_only"
    elif reasons:
        result["status"] = "rejected"
    return result


def validate_normalized_journey_trace(value):
    """Check the exact stored interpretation shape; return False on tampering.

    This does not authenticate source hashes. The reader must recheck IDs, roles
    and SHA256 of each full whitespace-normalized text against its owned source.
    """
    try:
        return _valid_normalized_trace(value)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _valid_normalized_trace(value):
    if not isinstance(value, dict) or set(value) != _RESULT_KEYS:
        return False
    if (
        type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["authority"] != "none" or value["provenance"] != "transcript_reconstruction"
        or value["status"] not in {"interpretation_only", "missing", "rejected", "partial"}
    ):
        return False
    coverage, steps, watermark = value["coverage"], value["steps"], value["watermark"]
    if (
        not isinstance(coverage, dict) or set(coverage) != _COVERAGE_KEYS
        or coverage["scope"] != "provided_transcript_only" or coverage["earlier_history"] != "unknown"
        or coverage["trace_start"] not in {"unobserved", "cited_start_within_window"}
        or type(coverage["current_node_omitted"]) is not bool
        or any(type(coverage[key]) is not int or coverage[key] < 0 for key in _COUNTERS)
        or not isinstance(steps, list) or len(steps) > MAX_STEPS
    ):
        return False
    reasons = coverage["reasons"]
    if (
        not isinstance(reasons, dict) or not set(reasons) <= _REJECTION_CODES
        or any(type(count) is not int or count < 1 for count in reasons.values())
    ):
        return False
    if watermark is None:
        if reasons != {"watermark_invalid": 1}:
            return False
    elif type(watermark) is not int or watermark < 0 or "watermark_invalid" in reasons:
        return False
    sources, nodes, disconnected = {}, set(), 0
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != _STEP_KEYS:
            return False
        origin, target, confidence = step["from_node"], step["to_node"], step["confidence"]
        if (
            not isinstance(step["summary"], str) or _summary(step["summary"]) != step["summary"]
            or
            not isinstance(origin, str) or not isinstance(target, str)
            or target not in SEMANTIC_NODE_KEYS
            or (origin not in SEMANTIC_NODE_KEYS and not (origin == "" and index == 0))
            or step["kind"] not in KINDS or step["reason_code"] not in REASON_CODES
            or type(confidence) not in (int, float) or not .7 <= confidence <= 1 or not math.isfinite(confidence)
            or not isinstance(step["evidence"], list) or not 1 <= len(step["evidence"]) <= MAX_REFS_PER_STEP
        ):
            return False
        step_ids, has_human = set(), False
        for ref in step["evidence"]:
            if not isinstance(ref, dict) or set(ref) != {"message_id", "role", "source_text_sha256"}:
                return False
            message_id, role, digest = ref["message_id"], ref["role"], ref["source_text_sha256"]
            if (
                type(message_id) is not int or not 0 < message_id <= watermark
                or message_id in step_ids or role not in _ROLES
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or (message_id in sources and sources[message_id] != (role, digest))
            ):
                return False
            sources[message_id] = (role, digest)
            step_ids.add(message_id)
            has_human |= role in {"user", "manager"}
        if not has_human:
            return False
        if index and origin != steps[index - 1]["to_node"]:
            disconnected += 1
        nodes.update((origin, target))
    current = value["current_node"]
    if (
        len(sources) > MAX_UNIQUE_IDS or not isinstance(current, str)
        or (current and (current not in SEMANTIC_NODE_KEYS or current not in nodes))
        or (current and coverage["current_node_omitted"])
        or reasons.get("current_node_unsupported", 0) != int(coverage["current_node_omitted"])
        or coverage["accepted_steps"] != len(steps)
        or coverage["inspected_steps"] != min(coverage["input_steps"], MAX_STEPS)
        or coverage["accepted_steps"] > coverage["inspected_steps"]
        or coverage["omitted_steps"] != coverage["input_steps"] - len(steps)
        or coverage["cited_message_count"] != len(sources)
        or coverage["disconnected_steps"] != disconnected
        or coverage["trace_start"] != ("cited_start_within_window" if steps and steps[0]["from_node"] == "" else "unobserved")
    ):
        return False
    global_errors = set(reasons) & _GLOBAL_REJECTION_CODES
    if global_errors:
        if len(reasons) != 1 or reasons[next(iter(global_errors))] != 1 or coverage["input_steps"] != 0:
            return False
    elif (
        reasons.get("step_limit", 0) != max(0, coverage["input_steps"] - MAX_STEPS)
        or sum(reasons.get(code, 0) for code in _STEP_REJECTION_CODES) != coverage["inspected_steps"] - len(steps)
    ):
        return False
    expected_status = (
        ("partial" if reasons or disconnected else "interpretation_only")
        if steps else ("rejected" if reasons else "missing")
    )
    return value["status"] == expected_status
