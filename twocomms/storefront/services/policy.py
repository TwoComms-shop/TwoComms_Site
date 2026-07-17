"""Single source of truth for shipping tariffs and return policy.

Phase 5 (Schema.org) refactor: previously these constants lived inline
inside ``StructuredDataGenerator`` in ``seo_utils.py``. They are now here
so the JSON-LD generator and any future delivery / returns text rendering
can share one definition.

The values intentionally match the public ``/delivery/`` and ``/returns/``
pages. Nova Poshta is the carrier; rates are weight-based brackets in UAH.
The 14-day return window is enshrined in Ukrainian consumer protection
law for goods of proper quality (see ``support_content.py`` →
``returns`` block).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ShippingTier:
    """A single Nova Poshta weight-based shipping tariff bracket."""

    rate: str  # UAH, kept as string to avoid float artifacts in JSON-LD
    min_weight: Optional[float] = None  # KGM (None = no lower bound)
    max_weight: Optional[float] = None  # KGM (None = no upper bound)


# Nova Poshta tariffs (UAH). Order matches the public delivery page.
SHIPPING_TIERS: tuple[ShippingTier, ...] = (
    ShippingTier(rate="85", max_weight=2.0),
    ShippingTier(rate="180", min_weight=2.01, max_weight=5.0),
    ShippingTier(rate="220", min_weight=5.01),
)

# Currency for shipping & returns monetary amounts.
CURRENCY = "UAH"

# Country (ISO-3166 alpha-2) where the policy applies.
APPLICABLE_COUNTRY = "UA"

# Return policy — schema.org / Merchant Center compliant.
RETURN_POLICY = {
    "days": 14,
    # https://schema.org/MerchantReturnFiniteReturnWindow
    "category": "https://schema.org/MerchantReturnFiniteReturnWindow",
    # https://schema.org/ReturnByMail
    "method": "https://schema.org/ReturnByMail",
    # Buyer pays return shipping (Nova Poshta cash-on-delivery is common).
    "fees_type": "https://schema.org/ReturnShippingFees",
    # Returned goods are refunded to the original payment method after
    # inspection; exchanges remain available through the support workflow.
    "refund_type": "https://schema.org/FullRefund",
}


def shipping_tiers_as_dicts() -> list[dict]:
    """Compatibility shim for older code expecting plain dicts.

    Returns a list with the legacy keys (``rate``, ``min_weight``,
    ``max_weight``) so that code paths still using
    ``StructuredDataGenerator.SHIPPING_OPTIONS`` continue to work after
    we migrate to dataclasses.
    """
    payload: list[dict] = []
    for tier in SHIPPING_TIERS:
        entry: dict = {"rate": tier.rate}
        if tier.min_weight is not None:
            entry["min_weight"] = tier.min_weight
        if tier.max_weight is not None:
            entry["max_weight"] = tier.max_weight
        payload.append(entry)
    return payload
