"""Pure, versioned customer-intent proposals; this module grants no authority.

Both the optional live ``customer_routes`` object and an analysis
OPEN_SUBFUNNEL adapter should construct this envelope and call
``normalize_customer_routes``. The adapter must not infer withdrawals from
omitted intents. A later producer verifies USER ownership, source digests,
watermarks, permission epochs and the current route before accepting changes.

Input: {schema_version: "customer-route.v1", intents: [...], focus_index?: int|null}.
Each intent requires kind, operation, evidence_message_ids and confidence;
subtype may be omitted ("none" means no subtype has been identified).
Malformed, missing and empty proposals abstain with distinct finite reasons.
No partial proposal is salvaged by dropping invalid members.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math


SCHEMA_VERSION = "customer-route.v1"
MAX_INTENTS = 4
MAX_EVIDENCE_REFS = 8
KINDS = frozenset({
    "catalog", "custom_print", "dtf", "information", "employment",
    "collaboration", "support", "community",
})
COLLABORATION_SUBTYPES = frozenset({
    "none", "designer", "partnership", "dropship", "wholesale_store",
    "creator", "other",
})
OPERATIONS = frozenset({"open", "continue", "withdraw", "correct"})
_ROOT_REQUIRED = frozenset({"schema_version", "intents"})
_ROOT_ALLOWED = _ROOT_REQUIRED | {"focus_index"}
_INTENT_REQUIRED = frozenset({
    "kind", "operation", "evidence_message_ids", "confidence",
})
_INTENT_ALLOWED = _INTENT_REQUIRED | {"subtype"}


@dataclass(frozen=True, slots=True)
class CustomerRouteIntent:
    kind: str
    subtype: str
    operation: str
    evidence_message_ids: tuple[int, ...]
    confidence: float

    def to_dict(self) -> dict:
        """Return a fresh JSON-safe value; mutations cannot alter this intent."""
        return {
            "kind": self.kind, "subtype": self.subtype,
            "operation": self.operation,
            "evidence_message_ids": list(self.evidence_message_ids),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class CustomerRouteProposal:
    intents: tuple[CustomerRouteIntent, ...]
    focus_index: int | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "intents": [intent.to_dict() for intent in self.intents],
            "focus_index": self.focus_index,
        }

    @property
    def digest(self) -> str:
        # Intent order is meaningful because focus_index addresses that order.
        # Evidence references are sets, normalized to sorted tuples below.
        encoded = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RouteNormalization:
    proposal: CustomerRouteProposal | None = None
    reason_code: str = ""

    @property
    def abstained(self) -> bool:
        return self.proposal is None


def normalize_customer_routes(payload: object) -> RouteNormalization:
    """Validate a parsed JSON envelope, without DB access or business actions.

    Evidence IDs are syntactically valid references, NOT verified USER evidence.
    A proposal is model interpretation, NOT an accepted routing decision.
    Null/missing or empty input preserves prior context; only an explicit valid
    ``operation="withdraw"`` can propose a withdrawal to the later producer.
    """
    if payload is None:
        return RouteNormalization(reason_code="route_missing")
    if not isinstance(payload, dict):
        return RouteNormalization(reason_code="route_shape_invalid")
    keys = set(payload)
    if not _ROOT_REQUIRED.issubset(keys) or not keys.issubset(_ROOT_ALLOWED):
        return RouteNormalization(reason_code="route_fields_invalid")
    if payload["schema_version"] != SCHEMA_VERSION:
        return RouteNormalization(reason_code="route_schema_unsupported")
    raw_intents = payload["intents"]
    if not isinstance(raw_intents, list):
        return RouteNormalization(reason_code="route_intents_invalid")
    if len(raw_intents) > MAX_INTENTS:
        return RouteNormalization(reason_code="route_intent_limit")
    focus = payload.get("focus_index")
    if focus is not None and (type(focus) is not int or not 0 <= focus < len(raw_intents)):
        return RouteNormalization(reason_code="route_focus_invalid")
    if not raw_intents:
        return RouteNormalization(reason_code="route_empty")

    intents, identities, all_refs = [], set(), set()
    for raw in raw_intents:
        if not isinstance(raw, dict):
            return RouteNormalization(reason_code="route_intent_shape_invalid")
        keys = set(raw)
        if not _INTENT_REQUIRED.issubset(keys) or not keys.issubset(_INTENT_ALLOWED):
            return RouteNormalization(reason_code="route_intent_fields_invalid")
        kind, subtype, operation = raw["kind"], raw.get("subtype", "none"), raw["operation"]
        if not isinstance(kind, str) or kind not in KINDS:
            return RouteNormalization(reason_code="route_kind_invalid")
        if (not isinstance(subtype, str)
            or subtype not in (COLLABORATION_SUBTYPES if kind == "collaboration" else {"none"})):
            return RouteNormalization(reason_code="route_subtype_invalid")
        if not isinstance(operation, str) or operation not in OPERATIONS:
            return RouteNormalization(reason_code="route_operation_invalid")
        identity = (kind, subtype)
        if identity in identities:
            return RouteNormalization(reason_code="route_intent_duplicate")
        identities.add(identity)
        refs = raw["evidence_message_ids"]
        if (not isinstance(refs, list) or not refs or len(refs) > MAX_EVIDENCE_REFS
            or any(type(ref) is not int or ref <= 0 for ref in refs)):
            return RouteNormalization(reason_code="route_evidence_invalid")
        if len(set(refs)) != len(refs):
            return RouteNormalization(reason_code="route_evidence_duplicate")
        all_refs.update(refs)
        if len(all_refs) > MAX_EVIDENCE_REFS:
            return RouteNormalization(reason_code="route_evidence_limit")
        confidence = raw["confidence"]
        if (type(confidence) not in (int, float) or not 0 <= confidence <= 1
            or not math.isfinite(confidence)):
            return RouteNormalization(reason_code="route_confidence_invalid")
        intents.append(CustomerRouteIntent(kind, subtype, operation, tuple(sorted(refs)),
            0.0 if confidence == 0 else float(confidence)))
    if focus is not None and intents[focus].operation == "withdraw":
        return RouteNormalization(reason_code="route_focus_withdrawn")
    return RouteNormalization(CustomerRouteProposal(tuple(intents), focus))


__all__ = [
    "SCHEMA_VERSION", "MAX_INTENTS", "MAX_EVIDENCE_REFS", "KINDS",
    "COLLABORATION_SUBTYPES", "OPERATIONS", "CustomerRouteIntent",
    "CustomerRouteProposal", "RouteNormalization", "normalize_customer_routes",
]
