"""Deterministic assembly of the executable Instagram policy.

This is deliberately a compiler, not a prompt-writing helper.  Callers name
the mandatory authority, published core and trusted dynamic facts explicitly;
this module never guesses that a title or a piece of prose is authoritative.
Optional material is admitted only as complete blocks after the mandatory
policy has fitted.  The returned metadata is safe to log: it contains module
identities and reason codes, never module or customer text.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping


DEFAULT_POLICY_VERSION = "unpublished"
DEFAULT_POLICY_BUDGET_CHARS = 48_000


class PolicyReadinessError(RuntimeError):
    """A policy cannot safely be used yet.

    ``details`` is intentionally content-safe so callers may include it in
    readiness telemetry.  It has ids, counts and limits only.
    """

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class PolicyModule:
    """One explicitly classified policy block.

    ``priority`` orders modules only inside the same optional source class.
    It has no authority meaning: mandatory authority comes solely from the
    compiler argument that the caller uses.
    """

    id: str
    body: str
    priority: int = 100
    tags: tuple[str, ...] = ()
    active: bool = True
    customer_bound: bool = False
    required: bool = False

    @classmethod
    def coerce(cls, value: "PolicyModule | Mapping[str, Any]", *, fallback_id: str) -> "PolicyModule":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("policy module must be PolicyModule or a mapping")
        required = value.get("required", False)
        if type(required) is not bool:
            raise PolicyReadinessError("invalid_required_policy_module", "required must be boolean")
        raw_tags = value.get("tags") or ()
        if isinstance(raw_tags, str):
            raw_tags = tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
        return cls(
            id=str(value.get("id") or fallback_id),
            body=str(value.get("body") or "").strip(),
            priority=int(value.get("priority", 100)),
            tags=tuple(sorted(str(tag) for tag in raw_tags)),
            active=bool(value.get("active", True)),
            customer_bound=bool(value.get("customer_bound", False)),
            required=required,
        )


@dataclass(frozen=True)
class PolicyOmission:
    id: str
    reason: str

    def metadata(self) -> dict[str, str]:
        return {"id": self.id, "reason": self.reason}


@dataclass(frozen=True)
class PolicyCompilation:
    """Compiled policy plus content-safe observability metadata."""

    text: str
    version: str
    content_hash: str
    context_hash: str
    selected: tuple[str, ...]
    omitted: tuple[PolicyOmission, ...]
    mandatory_ids: tuple[str, ...]
    budget_chars: int
    visual_trigger_codes: tuple[str, ...]
    required_ids: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        """Return telemetry-safe metadata without raw prompt/customer text.

        ``context_hash`` is intentionally absent: it changes with customer
        context and is only for a request-local cache/freshness key.
        """
        result = {
            "version": self.version,
            "content_hash": self.content_hash,
            "selected_ids": list(self.selected),
            "omitted": [item.metadata() for item in self.omitted],
            "mandatory_ids": list(self.mandatory_ids),
            "budget_chars": self.budget_chars,
            "visual_trigger_codes": list(self.visual_trigger_codes),
        }
        if self.required_ids:
            result["required_ids"] = list(self.required_ids)
        return result


def _coerce_many(
    values: Iterable[PolicyModule | Mapping[str, Any]] | None,
    *,
    prefix: str,
    require_explicit_id: bool = False,
) -> list[PolicyModule]:
    modules = []
    for index, value in enumerate(values or ()):
        if require_explicit_id and isinstance(value, Mapping) and not value.get("id"):
            raise PolicyReadinessError(
                "unnamed_mandatory_policy",
                "mandatory policy blocks need stable explicit ids",
                details={"source": prefix, "index": index},
            )
        modules.append(PolicyModule.coerce(value, fallback_id=f"{prefix}:{index}"))
    return modules


def _coerce_omissions(values: Iterable[Any] | None) -> list[PolicyOmission]:
    result = []
    for value in values or ():
        if isinstance(value, PolicyOmission):
            result.append(value)
        elif isinstance(value, Mapping):
            result.append(PolicyOmission(str(value.get("id") or ""), str(value.get("reason") or "omitted")))
        else:
            result.append(PolicyOmission(str(getattr(value, "id", "")), str(getattr(value, "reason", "omitted"))))
    return [item for item in result if item.id]


def _manifest_payload(
    mandatory: list[PolicyModule],
    optional: list[PolicyModule],
    *,
    version: str,
    budget_chars: int,
    visual_trigger_codes: tuple[str, ...],
) -> dict[str, Any]:
    """Hash the reusable policy manifest, excluding customer payload text.

    Customer data can be short and identifying, so it must neither appear in
    telemetry nor become a brute-forceable input to a public policy hash.
    """
    def item(module: PolicyModule) -> dict[str, Any]:
        result = {
            "id": module.id,
            "body": module.body,
            "priority": module.priority,
            "tags": list(module.tags),
            "active": module.active,
        }
        if module.customer_bound:
            result.pop("body")
            result["customer_bound"] = True
        if module.required:
            result["required"] = True
        return result

    return {
        "version": version,
        "budget_chars": budget_chars,
        "visual_trigger_codes": list(visual_trigger_codes),
        "mandatory": [item(module) for module in mandatory],
        "optional": [item(module) for module in optional],
    }


def _join_cost(parts: list[str], body: str) -> int:
    return len(body) + (2 if parts else 0)


def compile_policy(
    *,
    immutable_authority: Iterable[PolicyModule | Mapping[str, Any]],
    published_core: Iterable[PolicyModule | Mapping[str, Any]],
    verified_dynamic_facts: Iterable[PolicyModule | Mapping[str, Any]],
    playbooks: Iterable[PolicyModule | Mapping[str, Any]] = (),
    knowledge: Iterable[PolicyModule | Mapping[str, Any]] = (),
    customer_data: Iterable[PolicyModule | Mapping[str, Any]] = (),
    preselected_omissions: Iterable[Any] = (),
    budget_chars: int = DEFAULT_POLICY_BUDGET_CHARS,
    version: str = DEFAULT_POLICY_VERSION,
    visual_trigger_codes: Iterable[str] | None = None,
) -> PolicyCompilation:
    """Compile the policy in its only valid source order.

    All three first arguments are mandatory classes, even when an individual
    class happens to contain no blocks.  The caller is responsible for passing
    only verified dynamic facts.  Visual codes are carried as explicit future
    inputs; this compiler does not infer a certificate or any other visual fact.
    """
    try:
        budget = int(budget_chars)
    except (TypeError, ValueError) as exc:
        raise PolicyReadinessError("invalid_policy_budget", "policy budget is invalid") from exc
    if budget < 0:
        raise PolicyReadinessError("invalid_policy_budget", "policy budget cannot be negative")

    visual_codes = tuple(sorted({str(code) for code in (visual_trigger_codes or ()) if str(code)}))
    authority = _coerce_many(immutable_authority, prefix="authority", require_explicit_id=True)
    core = _coerce_many(published_core, prefix="core", require_explicit_id=True)
    dynamic = _coerce_many(verified_dynamic_facts, prefix="dynamic", require_explicit_id=True)
    mandatory = [*authority, *core, *dynamic]
    mandatory_ids = tuple(module.id for module in mandatory)
    duplicate_ids = sorted({module.id for module in mandatory if mandatory_ids.count(module.id) > 1})
    if duplicate_ids:
        raise PolicyReadinessError(
            "duplicate_mandatory_policy_id",
            "mandatory policy ids must be unique",
            details={"ids": duplicate_ids},
        )
    invalid = [module.id for module in mandatory if not module.id or not module.body]
    if invalid:
        raise PolicyReadinessError(
            "invalid_mandatory_policy",
            "mandatory policy blocks need an id and a body",
            details={"ids": invalid},
        )

    # Ordering within each optional class is stable and explicit.  A lower
    # numeric priority wins, matching BotInstruction's existing contract.
    optional_groups = (
        _coerce_many(playbooks, prefix="playbook"),
        _coerce_many(knowledge, prefix="knowledge"),
        _coerce_many(customer_data, prefix="customer"),
    )
    required_groups = [sorted((module for module in group if module.required), key=lambda item: (item.priority, item.id))
                       for group in optional_groups]
    required = [module for group in required_groups for module in group]
    if any(type(module.required) is not bool for group in optional_groups for module in group):
        raise PolicyReadinessError("invalid_required_policy_module", "required must be boolean")
    all_ids = [module.id for module in (*mandatory, *(module for group in optional_groups for module in group))]
    if any(all_ids.count(module.id) != 1 or not module.id or not module.active or not module.body for module in required):
        raise PolicyReadinessError("required_scenario_module_missing", "required instruction is unavailable",
            details={"required_ids": [module.id for module in required]})
    reusable_optional = [module for group in optional_groups[:2] for module in group]
    manifest = _manifest_payload(
        mandatory, reusable_optional, version=str(version or DEFAULT_POLICY_VERSION),
        budget_chars=budget, visual_trigger_codes=visual_codes,
    )
    content_hash = sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    parts: list[str] = []
    used = 0
    for module in mandatory:
        cost = _join_cost(parts, module.body)
        if used + cost > budget:
            raise PolicyReadinessError(
                "mandatory_policy_exceeds_budget",
                "mandatory policy does not fit the configured budget",
                details={"mandatory_ids": list(mandatory_ids), "budget_chars": budget, "required_chars": used + cost},
            )
        parts.append(module.body)
        used += cost

    selected = list(mandatory_ids)
    omitted = _coerce_omissions(preselected_omissions)
    for module in required:
        cost = _join_cost(parts, module.body)
        if used + cost > budget:
            raise PolicyReadinessError("required_policy_exceeds_budget", "required instruction does not fit",
                details={"required_ids": [item.id for item in required], "budget_chars": budget,
                         "required_chars": used + cost})
        parts.append(module.body)
        used += cost
        selected.append(module.id)
    for group in optional_groups:
        for module in sorted(group, key=lambda item: (item.priority, item.id)):
            if module.required:
                continue
            if not module.active:
                omitted.append(PolicyOmission(module.id, "inactive"))
            elif not module.body:
                omitted.append(PolicyOmission(module.id, "empty_body"))
            else:
                cost = _join_cost(parts, module.body)
                if used + cost > budget:
                    omitted.append(PolicyOmission(module.id, "budget_exhausted"))
                    continue
                parts.append(module.body)
                used += cost
                selected.append(module.id)

    return PolicyCompilation(
        text="\n\n".join(parts),
        version=str(version or DEFAULT_POLICY_VERSION),
        content_hash=content_hash,
        context_hash=sha256("\n\n".join(parts).encode("utf-8")).hexdigest(),
        selected=tuple(selected),
        omitted=tuple(omitted),
        mandatory_ids=mandatory_ids,
        budget_chars=budget,
        visual_trigger_codes=visual_codes,
        required_ids=tuple(module.id for module in required),
    )
