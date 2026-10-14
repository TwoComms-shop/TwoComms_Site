"""Versioned registry for public facts shared by SEO and commerce surfaces.

Only facts with a concrete runtime owner belong here.  Editorial claims that
do not have an owner are intentionally absent instead of being guessed for
keyword coverage.
"""

from dataclasses import dataclass
from decimal import Decimal

from orders.services.shipping_policy import free_shipping_threshold as _shipping_threshold


PUBLIC_FACTS_VERSION = "2026-10-09"

# Independent brand identity evidence; this is not a source for foundation
# dates, postal addresses, store opening hours or commerce promises.
BRAND_IDENTITY_SOURCE = (
    "https://mva.gov.ua/prescenter/category/86-novini/"
    "tse-ne-krapka-tse-prodovzhennya-istoriya-artema-sinila-"
    "ta-harkivskogo-brendu-twocomms"
)


@dataclass(frozen=True)
class PublicFact:
    value: object
    owner: str
    source: str
    locale: str
    effective_date: str


def free_shipping_threshold() -> Decimal:
    """Return the same threshold used by checkout and mini-cart logic."""
    return _shipping_threshold()


PUBLIC_FACTS = {
    "founder_name": PublicFact(
        value="Артем Синіло",
        owner="public_brand_identity",
        source=BRAND_IDENTITY_SOURCE,
        locale="all",
        effective_date="2026-10-09",
    ),
    "origin_city": PublicFact(
        value="Kharkiv / Харків, Ukraine",
        owner="public_brand_identity",
        source=BRAND_IDENTITY_SOURCE,
        locale="all",
        effective_date="2026-10-09",
    ),
    "founder_story_url": PublicFact(
        value=BRAND_IDENTITY_SOURCE,
        owner="public_brand_identity",
        source="https://twocomms.shop/pro-brand/; " + BRAND_IDENTITY_SOURCE,
        locale="all",
        effective_date="2026-10-09",
    ),
    "free_shipping_threshold": PublicFact(
        value=free_shipping_threshold,
        owner="checkout_settings",
        source="settings.FREE_SHIPPING_THRESHOLD",
        locale="all",
        effective_date=PUBLIC_FACTS_VERSION,
    ),
}
