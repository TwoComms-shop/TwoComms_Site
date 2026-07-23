"""Authoritative payment truth for the Instagram CRM.

Conversation stages are model- and manager-facing workflow state.  They are
never sufficient evidence of money received.  This module is the single query
contract for code that needs confirmed payment truth.
"""
from __future__ import annotations

from django.db.models import Exists, OuterRef, Q, QuerySet

from management.models import IgDeal


VERIFIED_DEAL_STATUSES = (IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED)
VERIFIED_PAYMENT_STATUSES = ("paid", "prepaid")


def verified_payment_q(prefix: str = "") -> Q:
    """Return a composable predicate for a provider-confirmed payment row."""
    return Q(
        **{
            f"{prefix}status__in": VERIFIED_DEAL_STATUSES,
            f"{prefix}payment_status__in": VERIFIED_PAYMENT_STATUSES,
            f"{prefix}paid_at__isnull": False,
        }
    )


def verified_payment_deals(queryset: QuerySet | None = None) -> QuerySet:
    queryset = queryset if queryset is not None else IgDeal.objects.all()
    return queryset.filter(verified_payment_q())


def annotate_verified_payment(
    queryset: QuerySet,
    *,
    alias: str = "has_verified_payment",
    deal_queryset: QuerySet | None = None,
) -> QuerySet:
    """Annotate client rows with one-row-correlated payment truth.

    ``Exists`` is intentional: negated joins across a multi-valued ``deals``
    relation can otherwise combine predicates from different payment attempts.
    """
    deals = deal_queryset if deal_queryset is not None else IgDeal.objects.all()
    confirmed = verified_payment_deals(deals.filter(client_id=OuterRef("pk")))
    return queryset.annotate(**{alias: Exists(confirmed)})


def client_has_verified_payment(client) -> bool:
    if not client or not getattr(client, "pk", None):
        return False
    prefetched = getattr(client, "_verified_payment_deals", None)
    if prefetched is not None:
        return bool(prefetched)
    return verified_payment_deals(client.deals.all()).exists()


def latest_verified_payment_deal(client):
    if not client or not getattr(client, "pk", None):
        return None
    prefetched = getattr(client, "_verified_payment_deals", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return verified_payment_deals(client.deals.all()).order_by("-paid_at", "-id").first()
