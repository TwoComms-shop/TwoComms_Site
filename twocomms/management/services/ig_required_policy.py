"""Pure required-scenario metadata and whole-module budget admission.

This helper never loads a publication or invents scenario instructions. Its
caller supplies captured applicability, normal selector eligibility and the
exact remaining compiler budget. Legacy publications remain compatible, with
explicit undeclared-coverage diagnostics instead of fabricated requirements.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

SCENARIOS = frozenset({"collaboration", "custom_print", "ugc", "size", "objection"})
REQUIRED_KEY = "required_for_scenarios"
MAX_MODULES = 300
MAX_BODY_CHARS = 64_000
SHOOTING_METADATA = {
    "kind": "shooting_prize", "programme_id": "shooting_prize",
    "manager_required": True, "confirmed_visual_sample": False,
}
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_OBJECTION_TAGS = frozenset("objection_" + code for code in (
    "price", "thinking", "size_risk", "prepayment_trust", "defect_risk", "delivery_time",
    "cheaper_elsewhere", "print_quality", "out_of_stock", "payday", "compare_brand", "ask_partner"))


class RequiredPolicyError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _scenarios(value, *, allow_tuple=False):
    if not isinstance(value, (list, tuple, set, frozenset) if allow_tuple else list) or len(value) > len(SCENARIOS):
        raise RequiredPolicyError("invalid_required_scenarios")
    if any(not isinstance(code, str) or code not in SCENARIOS for code in value):
        raise RequiredPolicyError("invalid_required_scenarios")
    return tuple(sorted(set(value)))


def normalize_programme_metadata(value):
    """Extend the existing JSON metadata without changing legacy empty hashes.

    Only the existing exact shooting schema and the finite required key are
    accepted. Empty required declarations disappear from canonical output.
    Existing publication code still checks the reserved shooting audience tag.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - (set(SHOOTING_METADATA) | {REQUIRED_KEY}):
        raise RequiredPolicyError("invalid_required_policy_metadata")
    base = {key: item for key, item in value.items() if key != REQUIRED_KEY}
    if base and (set(base) != set(SHOOTING_METADATA)
        or base.get("kind") != "shooting_prize" or base.get("programme_id") != "shooting_prize"
        or base.get("manager_required") is not True or base.get("confirmed_visual_sample") is not False):
        raise RequiredPolicyError("invalid_required_policy_metadata")
    required = _scenarios(value[REQUIRED_KEY]) if REQUIRED_KEY in value else ()
    result = dict(base)
    if required:
        result[REQUIRED_KEY] = list(required)
    return result


def required_for_scenarios(metadata):
    return tuple(normalize_programme_metadata(metadata).get(REQUIRED_KEY, ()))


def shooting_programme_metadata(metadata):
    """Exact canonical shooting subset for the existing prize matcher.

    A required-only module returns {}; unknown/unsafe programme fields fail.
    This adapter does not confirm entitlement or create a programme.
    """
    normalized = normalize_programme_metadata(metadata)
    return {key: value for key, value in normalized.items() if key != REQUIRED_KEY}


def derive_required_scenarios(*, captured_tags=(), semantic_triggers=()):
    """Derive relevance from explicit captured signals, not profile fields.

    Plain ``size``/``fit`` tags can describe an existing selection on a thank-you
    turn, so they alone do not create a new sizing requirement. UGC and creator
    tags must be supplied by the caller's captured source/capability adapter;
    an arbitrary image is not such a tag.
    """
    if any(not isinstance(values, (list, tuple, set, frozenset)) for values in (captured_tags, semantic_triggers)):
        raise RequiredPolicyError("invalid_required_scenario_inputs")
    tags, triggers = tuple(captured_tags), tuple(semantic_triggers)
    if len(tags) > 64 or len(triggers) > 32 or any(not isinstance(item, str) for item in (*tags, *triggers)):
        raise RequiredPolicyError("invalid_required_scenario_inputs")
    tags, triggers = set(tags), set(triggers)
    result = set()
    if tags & {"collaboration", "creator"} or triggers & {"collaboration", "creator"}:
        result.add("collaboration")
    if "custom_print" in tags or "custom_print" in triggers:
        result.add("custom_print")
    if tags & {"ugc", "ugc_review"} or "ugc" in triggers:
        result.add("ugc")
    if "size_question" in triggers or "objection_size_risk" in tags:
        result.add("size")
    if tags & _OBJECTION_TAGS or triggers & {"price_objection", "hesitation"}:
        result.add("objection")
    return tuple(sorted(result))


@dataclass(frozen=True)
class RequiredPolicyAdmission:
    ready: bool
    reason: str
    selected_ids: tuple[str, ...]
    required_ids: tuple[str, ...]
    omitted: tuple[tuple[str, str], ...]
    declared_scenarios: tuple[str, ...]
    covered_scenarios: tuple[str, ...]
    undeclared_scenarios: tuple[str, ...]
    coverage_status: str
    diagnostics: tuple[str, ...]
    required_chars: int
    used_chars: int

    def metadata(self):
        return {"ready": self.ready, "reason": self.reason,
            "selected_ids": list(self.selected_ids), "required_ids": list(self.required_ids),
            "omitted": [{"id": identity, "reason": reason} for identity, reason in self.omitted],
            "declared_scenarios": list(self.declared_scenarios), "covered_scenarios": list(self.covered_scenarios),
            "undeclared_scenarios": list(self.undeclared_scenarios), "coverage_status": self.coverage_status,
            "diagnostics": list(self.diagnostics), "required_chars": self.required_chars, "used_chars": self.used_chars}


def admit_required_policy_modules(instructions, *, applicable_scenarios, eligible_ids,
                                  locale="all", budget_chars=48_000, used_chars=0, separator_chars=2):
    """Reserve declared applicable modules before any optional admission.

    ``eligible_ids`` comes from the ordinary relevance/exclusion selector
    *before* budget filtering. Locale, public scope, active and body checks are
    repeated here. All declared modules for the applicable locale are required;
    translated modules of another locale need not also be included.
    Costs use supplied rendered bodies and the compiler's exact separators.
    Failed admission returns no selected IDs, never a usable partial prompt.
    """
    active = set(_scenarios(applicable_scenarios, allow_tuple=True))
    if locale not in {"all", "uk", "ru", "en"}:
        raise RequiredPolicyError("invalid_required_policy_locale")
    if any(type(value) is not int or value < 0 for value in (budget_chars, used_chars, separator_chars)):
        raise RequiredPolicyError("invalid_required_policy_budget")
    if not isinstance(instructions, (list, tuple)) or len(instructions) > MAX_MODULES:
        raise RequiredPolicyError("invalid_required_policy_modules")
    if not isinstance(eligible_ids, (list, tuple, set, frozenset)):
        raise RequiredPolicyError("invalid_required_policy_eligibility")
    eligible = tuple(eligible_ids)
    if len(eligible) > MAX_MODULES or any(not isinstance(identity, str) or not _ID.fullmatch(identity) for identity in eligible):
        raise RequiredPolicyError("invalid_required_policy_eligibility")
    eligible = set(eligible)
    rows, identities = [], set()
    for item in instructions:
        if not isinstance(item, dict):
            raise RequiredPolicyError("invalid_required_policy_modules")
        identity, body = item.get("id"), item.get("rendered_body", item.get("body", ""))
        priority = item.get("priority", 100)
        if (not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in identities
            or not isinstance(body, str) or len(body) > MAX_BODY_CHARS + 204
            or type(priority) is not int or type(item.get("active", True)) is not bool
            or item.get("locale", "all") not in {"all", "uk", "ru", "en"}
            or item.get("trust_scope", "public_policy") not in {"public_policy", "operator_only"}):
            raise RequiredPolicyError("invalid_required_policy_modules")
        identities.add(identity)
        declaration = set(required_for_scenarios(item.get("programme_metadata", {})))
        locale_ok = locale == "all" or item.get("locale", "all") in {"all", locale}
        reason = ("operator_only" if item.get("trust_scope", "public_policy") != "public_policy"
            else "inactive" if not item.get("active", True) else "empty_body" if not body.strip()
            else "locale_mismatch" if not locale_ok else "not_relevant" if identity not in eligible else "")
        rows.append(dict(id=identity, body=body.strip(), priority=priority, declaration=declaration,
            locale_ok=locale_ok, reason=reason))
    if not eligible <= identities:
        raise RequiredPolicyError("invalid_required_policy_eligibility")
    rows.sort(key=lambda row: (row["priority"], row["id"]))
    declared = active & set().union(*(row["declaration"] for row in rows))
    undeclared = active - declared
    diagnostics = ("legacy_required_scenarios_undeclared",) if undeclared else ()
    required = [row for row in rows if row["locale_ok"] and row["declaration"] & active]
    covered = set().union(*(row["declaration"] & active for row in required if not row["reason"]))
    omitted = [(row["id"], row["reason"]) for row in rows if row["reason"]]
    required_ids = tuple(row["id"] for row in required)
    status = "not_applicable" if not active else "legacy_unconfigured" if not declared else "partial" if undeclared else "declared_covered"

    def result(ready, reason, selected=(), required_chars=0, used=used_chars):
        return RequiredPolicyAdmission(ready, reason, tuple(selected), required_ids, tuple(omitted),
            tuple(sorted(declared)), tuple(sorted(covered)) if ready else (), tuple(sorted(undeclared)),
            status if ready else "unavailable", diagnostics, required_chars, used)

    if declared - covered or any(row["reason"] for row in required):
        return result(False, "required_scenario_module_missing")
    used = used_chars
    for row in required:
        used += len(row["body"]) + (separator_chars if used else 0)
    required_chars = used - used_chars
    if used > budget_chars:
        return result(False, "required_policy_exceeds_budget", required_chars=required_chars)
    selected = list(required_ids)
    for row in rows:
        if row["reason"] or row["id"] in required_ids:
            continue
        cost = len(row["body"]) + (separator_chars if used else 0)
        if used + cost > budget_chars:
            omitted.append((row["id"], "budget_exhausted"))
        else:
            used += cost
            selected.append(row["id"])
    return result(True, "required_policy_admitted" if required_ids else "required_policy_legacy" if undeclared else "required_policy_not_applicable",
        selected, required_chars, used)
