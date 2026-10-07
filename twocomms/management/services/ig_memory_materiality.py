"""Pure re-observation classification for already authenticated v1 captures.

The locked publisher owns result/evidence/chain validation and effects. Returning
``append`` means use its existing strict publication path; it is not permission
to append. Checkpoints and the existing 512-depth guard remain separate work.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re


SCOPE_FIELDS = (
    "client_id", "scope", "commercial_episode_id", "line_id", "order_id",
    "post_sale_case_id", "fact_key", "schema_version",
)
OBJECTION_TYPES = frozenset({
    "price", "thinking", "size_risk", "prepayment_trust", "defect_risk",
    "delivery_time", "cheaper_elsewhere", "print_quality", "out_of_stock",
    "payday", "compare_brand", "ask_partner",
})


def _positive(value):
    return type(value) is int and value > 0


def _utc(value):
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError, TypeError):
        return None


def _semantic_value(row):
    if not isinstance(row, Mapping) or any(field not in row for field in SCOPE_FIELDS):
        return None
    if (
        not _positive(row["client_id"])
        or row["schema_version"] != "typed-memory.v1"
        or row.get("producer_policy_version") != "typed-memory-projector.v1"
        or row.get("operation") != "assert"
        or row.get("producer") != "analysis_v2"
        or row.get("source_role") != "user"
        or row.get("closure_method") != "analysis_assertion"
        or row["order_id"] is not None
        or row["post_sale_case_id"] is not None
        or not isinstance(row["line_id"], str)
    ):
        return None
    value = row.get("typed_value")
    if not isinstance(value, dict):
        return None
    key, episode, line = row["fact_key"], row["commercial_episode_id"], row["line_id"]
    expiry = row.get("valid_until")
    expiry_at = _utc(expiry) if expiry is not None else None
    observed_at = _utc(row.get("observed_at"))
    if observed_at is None or (expiry is not None and (expiry_at is None or expiry_at <= observed_at)):
        return None
    confidence = row.get("confidence")
    if confidence is not None and (
        not isinstance(confidence, Decimal) or not confidence.is_finite()
        or not Decimal("0") <= confidence <= Decimal("1")
    ):
        return None
    if key == "observed_language":
        if (
            row["scope"] != "client" or episode is not None or line
            or set(value) != {"code"} or not isinstance(value["code"], str)
            or value["code"] not in {"uk", "ru", "en", "mixed", "unknown"}
            or confidence is not None or expiry is not None
            or row.get("sensitivity") != "low" or row.get("retention_class") != "client"
        ):
            return None
    elif key == "objection_observed":
        if (
            not _positive(episode)
            or not ((row["scope"] == "episode" and not line) or (
                row["scope"] == "line" and re.fullmatch(r"line:(?!.*[0-9]{7})[a-z0-9][a-z0-9_.-]{0,79}", line)))
            or set(value) != {"type"} or not isinstance(value["type"], str)
            or value["type"] not in OBJECTION_TYPES
            or expiry is not None or row.get("sensitivity") != "personal_preference"
            or row.get("retention_class") != "episode"
        ):
            return None
    elif key == "deferred_intent":
        kind = value.get("kind")
        kinds = {"date": "customer_date", "event": "after_event", "payday": "payday", "indefinite": "indefinite"}
        if (
            row["scope"] != "episode" or not _positive(episode) or line
            or set(value) != {"kind", "condition_code"} or not isinstance(kind, str)
            or kind not in kinds or value["condition_code"] != kinds[kind]
            or confidence is not None or (kind == "date") != (expiry is not None)
            or row.get("sensitivity") != "personal_preference"
            or row.get("retention_class") != ("until_date" if kind == "date" else "episode")
        ):
            return None
    else:
        return None
    return tuple(row[field] for field in SCOPE_FIELDS), value, expiry_at


@dataclass(frozen=True, slots=True)
class MaterialityDecision:
    action: str
    reason: str


def classify_memory_candidate(candidate, *, head=None, current_fact=None, boundary, now):
    """Only a valid unchanged assertion can avoid strict append validation."""
    append = MaterialityDecision("append", "memory_requires_strict_publication")
    clock = _utc(now)
    value = _semantic_value(candidate)
    old_value = _semantic_value(current_fact)
    if clock is None or value is None or old_value is None or value != old_value:
        return append
    if not isinstance(head, Mapping) or not isinstance(boundary, Mapping):
        return append
    if (
        head.get("state") != "active"
        or not _positive(head.get("current_fact_id"))
        or head["current_fact_id"] != current_fact.get("id")
        or not _positive(head.get("revision"))
        or any(head.get(field) != candidate[field] or type(head.get(field)) is not type(candidate[field]) for field in SCOPE_FIELDS)
        or boundary.get("client_id") != candidate["client_id"]
        or type(boundary.get("client_id")) is not int
        or boundary.get("erasure_started") is not False
        or boundary.get("hidden") is not False
        or boundary.get("source_evidence_valid") is not True
    ):
        return append
    floor, watermark = boundary.get("reset_floor"), boundary.get("watermark_message_id")
    if not _positive(floor) or not _positive(watermark):
        return append
    for row in (candidate, current_fact):
        source = row.get("source_watermark_message_id")
        if not _positive(source) or not floor <= source <= watermark:
            return append
    if candidate["scope"] != "client" and (
        candidate["commercial_episode_id"] != boundary.get("episode_id")
        or candidate["scope"] == "line" and candidate["line_id"] != boundary.get("line_id")
    ):
        return append
    if value[2] is not None and value[2] <= clock:
        return append
    return MaterialityDecision("reobserve", "memory_same_value_new_observation")
