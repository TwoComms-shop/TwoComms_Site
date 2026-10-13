"""Dormant, provider-free identity helpers for Assisted Checkout V2."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from django.conf import settings


SERIES_KEY_VERSION = "ig-assisted-series-v1"
ORDER_KEY_VERSION = "ig-assisted-order-v1"
VALID_MODES = frozenset({"off", "shadow", "enforced"})


class AssistedCheckoutV2Disabled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckoutSeriesIdentity:
    series_key: str
    generation: int
    order_idempotency_key: str


def assisted_checkout_v2_mode() -> str:
    value = str(
        getattr(settings, "IG_ASSISTED_CHECKOUT_V2", "off") or "off"
    ).strip().casefold()
    return value if value in VALID_MODES else "off"


def assisted_checkout_v2_enabled() -> bool:
    return assisted_checkout_v2_mode() in {"shadow", "enforced"}


def assisted_checkout_v2_new_proposal_enabled(identity) -> bool:
    """Stable canary gate used only when a new proposal is first created."""
    if assisted_checkout_v2_mode() != "enforced":
        return False
    try:
        percent = int(
            getattr(settings, "IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT", 0) or 0
        )
    except (TypeError, ValueError):
        return False
    percent = max(0, min(percent, 100))
    if not percent:
        return False
    bucket = int(_sha256(f"ig-assisted-canary-v1:{identity}")[:8], 16) % 100
    return bucket < percent


def _require_enabled() -> None:
    if not assisted_checkout_v2_enabled():
        raise AssistedCheckoutV2Disabled("Assisted Checkout V2 is disabled")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_checkout_series_key(proposal_public_id) -> str:
    """Return one opaque key for all invoice generations of a proposal."""
    _require_enabled()
    try:
        proposal_id = str(uuid.UUID(str(proposal_public_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("proposal_public_id must be a UUID") from exc
    return _sha256(f"{SERIES_KEY_VERSION}:{proposal_id}")


def stable_order_idempotency_key(series_key: str) -> str:
    """Reuse Order's existing unique key without writing or creating an Order."""
    _require_enabled()
    normalized = str(series_key or "").strip().casefold()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("series_key must be a SHA-256 hex digest")
    return _sha256(f"{ORDER_KEY_VERSION}:{normalized}")


def existing_series_order_idempotency_key(series_key: str) -> str:
    """Derive an already-issued V2 order key even after rollout is disabled."""
    normalized = str(series_key or "").strip().casefold()
    if len(normalized) != 64 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise ValueError("series_key must be a SHA-256 hex digest")
    return _sha256(f"{ORDER_KEY_VERSION}:{normalized}")


def build_checkout_series_identity(
    proposal_public_id,
    *,
    generation: int,
) -> CheckoutSeriesIdentity:
    """Build a deterministic schema identity; this function performs no I/O."""
    _require_enabled()
    return _validated_checkout_series_identity(proposal_public_id, generation=generation)


def _validated_checkout_series_identity(proposal_public_id, *, generation: int) -> CheckoutSeriesIdentity:
    """Pure identity derivation after the caller's native commerce authority gate.

    This private helper grants no checkout permission. Ordinary callers retain
    the public rollout gate; the generation owner also uses it after validating
    a fresh producer-issued assisted Execution.
    """
    if type(generation) is not int or generation < 1:
        raise ValueError("generation must be a positive integer")
    try:
        proposal_id = str(uuid.UUID(str(proposal_public_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("proposal_public_id must be a UUID") from exc
    series_key = _sha256(f"{SERIES_KEY_VERSION}:{proposal_id}")
    return CheckoutSeriesIdentity(
        series_key=series_key,
        generation=generation,
        # Every generation of one proposal must converge on one DB duplicate-
        # order barrier. Runtime application of this key is intentionally out
        # of scope until the winner service is independently reviewed.
        order_idempotency_key=existing_series_order_idempotency_key(series_key),
    )
