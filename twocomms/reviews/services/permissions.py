"""Phase 21 (PR-4c) — review submission gating.

Single rule: a registered user can leave a review for a product only
if they have at least one ``orders.Order`` with ``payment_status='paid'``
that contains an ``OrderItem`` for the product. Guests can submit
without this check (their review still goes through moderation, and
``is_verified_purchase`` simply stays False).

Kept in a service module rather than directly on the model so the
business rule has one home and tests can patch it cleanly.
"""

from __future__ import annotations

from typing import Optional


def has_paid_order_with_product(user, product) -> bool:
    """Return True if ``user`` has a paid order containing ``product``.

    Defensive against ``None`` / anonymous user — anonymous always
    returns False so callers can use this as a single boolean gate.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False

    # Local import to avoid circulars between reviews → orders → reviews.
    from orders.models import Order

    return (
        Order.objects
        .filter(
            user=user,
            payment_status="paid",
            items__product=product,
        )
        .exclude(status="cancelled")
        .exists()
    )


def can_user_review_product(user, product) -> bool:
    """Public API for the form / view layer.

    Currently identical to ``has_paid_order_with_product`` for
    authenticated users; guests skip this gate at the view layer
    (their reviews land as ``is_verified_purchase=False``).
    """
    return has_paid_order_with_product(user, product)


def already_reviewed(user, product, *, anon_key: str = "") -> bool:
    """Detect whether the same user/anon already has a review for this
    product (any status) — used to keep the form from accepting
    duplicates. Pending reviews still count, otherwise spammers could
    queue dozens of pending submissions and starve the moderation
    queue.
    """
    from .._helpers import _build_review_owner_filter  # type: ignore[attr-defined]
    return _build_review_owner_filter(user=user, anon_key=anon_key, product=product).exists()


__all__ = [
    "can_user_review_product",
    "has_paid_order_with_product",
    "already_reviewed",
]
