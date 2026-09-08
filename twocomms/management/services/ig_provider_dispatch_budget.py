"""Request-local limits for validated Instagram provider replies."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_SAFE_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_REASON_CODES = 12
MAX_VALIDATED_DISPATCHES = 8
MAX_SCARCE_MODEL_DISPATCHES = 2
_VALIDATION_USAGE_COUNTS = (
    "promptTokenCount",
    "thoughtsTokenCount",
    "candidatesTokenCount",
    "totalTokenCount",
    "_request_inline_count",
    "_request_trimmed_inline",
    "_request_serialized_bytes",
)


@dataclass(frozen=True)
class ValidationDecision:
    valid: bool
    reason_codes: tuple[str, ...] = ()


def normalize_validation_decision(value) -> ValidationDecision:
    """Return one bounded decision without retaining validator error details."""
    valid = bool(getattr(value, "valid", False))
    raw_reasons: Iterable = getattr(
        value,
        "reason_codes",
        getattr(value, "reasons", ()),
    ) or ()
    reasons: list[str] = []
    for raw in raw_reasons:
        reason = str(raw or "").strip().casefold()
        if _SAFE_REASON_CODE.fullmatch(reason) and reason not in reasons:
            reasons.append(reason)
        if len(reasons) >= MAX_REASON_CODES:
            break
    if valid:
        return ValidationDecision(valid=True)
    return ValidationDecision(
        valid=False,
        reason_codes=tuple(reasons) or ("invalid_result",),
    )


def sanitized_validation_usage(usage) -> dict:
    """Expose only bounded runtime counters needed by deterministic validation."""
    source = usage if isinstance(usage, dict) else {}
    sanitized = {}
    for name in _VALIDATION_USAGE_COUNTS:
        try:
            value = int(source.get(name) or 0)
        except (TypeError, ValueError, OverflowError):
            value = 0
        sanitized[name] = max(0, value)
    sanitized["_finish_reason"] = str(
        source.get("_finish_reason") or ""
    )[:32]
    if "_request_inline_content_hashes" in source:
        hashes = source["_request_inline_content_hashes"]
        sanitized["_request_inline_content_hashes"] = (
            list(hashes)
            if isinstance(hashes, list) and len(hashes) <= 8
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
            else None
        )
    return sanitized


@dataclass
class ProviderDispatchBudget:
    """Count actual HTTP dispatches and permit at most one repair payload."""

    max_dispatches: int = 2
    max_scarce_dispatches: int = MAX_SCARCE_MODEL_DISPATCHES
    consumed_dispatches: int = 0
    consumed_scarce_dispatches: int = 0
    repair_consumed: bool = False

    def __post_init__(self) -> None:
        value = int(self.max_dispatches)
        if value < 1 or value > MAX_VALIDATED_DISPATCHES:
            raise ValueError(
                "validated provider dispatch limit must be between 1 and "
                f"{MAX_VALIDATED_DISPATCHES}"
            )
        self.max_dispatches = value
        scarce_value = int(self.max_scarce_dispatches)
        if scarce_value < 0 or scarce_value > MAX_SCARCE_MODEL_DISPATCHES:
            raise ValueError(
                "scarce provider dispatch limit must be between 0 and "
                f"{MAX_SCARCE_MODEL_DISPATCHES}"
            )
        self.max_scarce_dispatches = scarce_value

    @property
    def remaining_dispatches(self) -> int:
        return max(0, self.max_dispatches - self.consumed_dispatches)

    @staticmethod
    def _is_scarce_model(model: str) -> bool:
        """Use the configured quota profile instead of a model-name list."""
        from management.services import gemini_quota

        model_name = str(model or "").strip()
        if not model_name:
            return False
        budget = gemini_quota.budget_for(model_name)
        try:
            rpd = int(budget.get("rpd") or 0)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return True
        return rpd <= 0 or rpd < int(gemini_quota.HEDGE_MIN_RPD)

    def dispatch_block_reason(
        self, model: str = "", *, scarce: bool | None = None
    ) -> str:
        if self.remaining_dispatches <= 0:
            return "provider_dispatch_budget"
        is_scarce = self._is_scarce_model(model) if scarce is None else bool(scarce)
        if (
            is_scarce
            and self.consumed_scarce_dispatches
            >= self.max_scarce_dispatches
        ):
            return "scarce_model_budget"
        return ""

    def consume_dispatch(
        self, model: str = "", *, scarce: bool | None = None
    ) -> bool:
        """Consume immediately before HTTP I/O; false means no dispatch."""
        if self.dispatch_block_reason(model, scarce=scarce):
            return False
        self.consumed_dispatches += 1
        is_scarce = self._is_scarce_model(model) if scarce is None else bool(scarce)
        if is_scarce:
            self.consumed_scarce_dispatches += 1
        return True

    def consume_repair(self) -> bool:
        """Reserve the sole repair only while an HTTP attempt remains."""
        if self.repair_consumed or self.remaining_dispatches <= 0:
            return False
        self.repair_consumed = True
        return True
