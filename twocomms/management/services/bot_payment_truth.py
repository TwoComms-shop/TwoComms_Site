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


def payment_truth_inconsistency_report(*, sample_limit: int = 50) -> dict:
    """Build a bounded, PII-free and strictly read-only reconciliation report."""
    from django.utils import timezone

    from management.models import IgClient

    limit = max(0, min(int(sample_limit), 500))
    hard_stages = (IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE)
    hard_deal_statuses = (IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED)

    clients = annotate_verified_payment(
        IgClient.objects.filter(stage__in=hard_stages)
    ).filter(has_verified_payment=False)
    hard_deals_without_truth = IgDeal.objects.filter(status__in=hard_deal_statuses).exclude(
        payment_status__in=VERIFIED_PAYMENT_STATUSES,
        paid_at__isnull=False,
    )
    verified_fields_without_hard_status = IgDeal.objects.filter(
        payment_status__in=VERIFIED_PAYMENT_STATUSES,
        paid_at__isnull=False,
    ).exclude(status__in=hard_deal_statuses)
    orders_without_truth = IgDeal.objects.filter(order__isnull=False).exclude(
        status__in=hard_deal_statuses,
        payment_status__in=VERIFIED_PAYMENT_STATUSES,
        paid_at__isnull=False,
    )
    order_status_without_order = IgDeal.objects.filter(
        status=IgDeal.Status.ORDER_CREATED,
        order__isnull=True,
    )

    categories = {
        "client_hard_stage_without_verified_payment": clients,
        "deal_hard_status_without_verified_payment": hard_deals_without_truth,
        "deal_verified_fields_without_hard_status": verified_fields_without_hard_status,
        "deal_order_without_verified_payment": orders_without_truth,
        "deal_order_created_without_order": order_status_without_order,
    }
    counts = {name: queryset.count() for name, queryset in categories.items()}
    samples = {
        name: list(queryset.order_by("id").values_list("id", flat=True)[:limit])
        for name, queryset in categories.items()
    }
    return {
        "schema_version": "2026-07-23.v1",
        "generated_at": timezone.now().isoformat(),
        "read_only": True,
        "sample_limit": limit,
        "finding_count": sum(counts.values()),
        "counts": counts,
        "samples": samples,
    }
