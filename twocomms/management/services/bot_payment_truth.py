"""Authoritative payment truth for the Instagram CRM.

Conversation stages are model- and manager-facing workflow state.  They are
never sufficient evidence of money received.  This module is the single query
contract for code that needs confirmed payment truth.

Two different questions live here and must not be collapsed into one predicate
(DR-001, refined by DR-007):

``client_has_verified_payment``
    "Did the payment provider confirm money for a deal?"  Authoritative for
    money and fulfillment: invoice creation, order materialization, shipment
    notification.  A manager looking at a receipt is not a provider ledger.

``client_has_confirmed_purchase``
    "Has this person bought from us?"  Authoritative for CRM: tone of voice,
    scoring, funnel state, follow-up suppression.  Wrong answers here cost a
    relationship, not money, so manager-confirmed evidence counts.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db.models import Count, DecimalField, Exists, ExpressionWrapper, F, OuterRef, Q, QuerySet, Sum

from management.models import IgDeal


VERIFIED_DEAL_STATUSES = (IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED)
VERIFIED_PAYMENT_STATUSES = ("paid", "prepaid")
VERIFIED_PAYMENT_TRUTHS = (
    IgDeal.PaymentTruth.CONFIRMED,
    IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
)
TERMINAL_NEGATIVE_PAYMENT_TRUTHS = (
    IgDeal.PaymentTruth.REFUNDED,
    IgDeal.PaymentTruth.REVERSED,
    IgDeal.PaymentTruth.FAILED,
    IgDeal.PaymentTruth.CANCELLED,
)
# ``Order.payment_status`` values that mean money actually arrived for that
# order. ``partial`` is the legacy spelling of ``prepaid``.  Deliberately
# excludes ``checking``: a receipt under review is not yet a purchase.
CONFIRMED_ORDER_PAYMENT_STATUSES = ("paid", "prepaid", "partial")
FULLY_PAID_ORDER_STATUSES = ("paid",)


def verified_payment_q(prefix: str = "") -> Q:
    """Return a composable predicate for a provider-confirmed payment row."""
    materialized_truth = Q(
        **{f"{prefix}payment_projection__truth__in": VERIFIED_PAYMENT_TRUTHS}
    )
    transitional_legacy_truth = Q(
        **{
            f"{prefix}status__in": VERIFIED_DEAL_STATUSES,
            f"{prefix}payment_status__in": VERIFIED_PAYMENT_STATUSES,
            f"{prefix}paid_at__isnull": False,
            f"{prefix}payment_truth": IgDeal.PaymentTruth.UNVERIFIED,
            f"{prefix}payment_projection__isnull": True,
        }
    )
    return materialized_truth | transitional_legacy_truth


def manual_confirmation_q(prefix: str = "") -> Q:
    """Match only a source-qualified manager decision, never status alone.

    Anchored on ``IgDeal``.  Production reality limits its reach: every
    ``IgPaymentConfirmationReview`` row observed on production carries
    ``deal_id IS NULL`` (the FK is nullable and reviews are opened from a
    conversation, not from a deal).  Use ``manager_confirmed_review_q`` when the
    question is about a client rather than about a deal.
    """
    relation = f"{prefix}payment_confirmation_reviews__"
    return Q(**{
        relation + "status": "confirmed",
        relation + "decisions__decision": "manager_verified",
        relation + "decisions__verification_source": "manager",
        relation + "decisions__actor_source__in": ("management_user", "telegram_user"),
    }) & ~Q(**{relation + "decisions__actor_external_id": ""})


def manager_confirmed_review_q(prefix: str = "") -> Q:
    """Source-qualified manager confirmation, anchored on the review row itself.

    ``status=confirmed`` alone is not enough: a superseded or pending review can
    also carry decisions, and a rejection is a decision too.  The actor must be
    identifiable, otherwise "a manager confirmed it" is unfalsifiable.
    """
    return Q(**{
        f"{prefix}status": "confirmed",
        f"{prefix}decisions__decision": "manager_verified",
        f"{prefix}decisions__verification_source": "manager",
        f"{prefix}decisions__actor_source__in": ("management_user", "telegram_user"),
    }) & ~Q(**{f"{prefix}decisions__actor_external_id": ""})


def current_manager_confirmation_review_q(prefix: str = "") -> Q:
    """Source-qualified manager truth for the current commercial cycle.

    Historical fulfilled reviews remain valid lifetime CRM evidence, but they
    must never make the current row look paid. Only an explicit full payment or
    prepayment decision can drive the current commercial presentation.
    """
    from management.ig_bot_models import (
        IgPaymentConfirmationReview,
        IgPaymentReviewDecision,
    )

    return manager_confirmed_review_q(prefix) & Q(
        **{
            f"{prefix}decisions__verification_scope__in": (
                IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
                IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
            ),
        }
    ) & ~Q(
        **{
            f"{prefix}resolution_kind": (
                IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
            ),
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
    # Э8.5: внутри одной сборки промпта этот вопрос задаётся трижды — стадией,
    # блоком возражений и через `client_has_confirmed_purchase`. Кэш живёт
    # только на время сборки: вне неё (в том числе в `payment_link_allowed`
    # после генерации) запрос выполняется заново, потому что оплату может
    # подтвердить вебхук в другом процессе.
    from management.services.ig_turn_snapshot import prompt_cached

    return prompt_cached(
        f"verified_payment:{client.pk}",
        lambda: verified_payment_deals(client.deals.all()).exists(),
    )


def client_has_terminal_negative_payment(client) -> bool:
    if not client or not getattr(client, "pk", None):
        return False
    latest = client.payment_projections.order_by("-updated_at", "-id").first()
    if latest:
        return latest.truth in TERMINAL_NEGATIVE_PAYMENT_TRUTHS
    # Transitional compatibility until 0090 has backfilled legacy truth rows.
    latest_deal = client.deals.exclude(
        payment_truth=IgDeal.PaymentTruth.UNVERIFIED
    ).order_by("-payment_truth_updated_at", "-id").first()
    return bool(latest_deal and latest_deal.payment_truth in TERMINAL_NEGATIVE_PAYMENT_TRUTHS)


def latest_verified_payment_deal(client):
    if not client or not getattr(client, "pk", None):
        return None
    prefetched = getattr(client, "_verified_payment_deals", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return verified_payment_deals(client.deals.all()).order_by("-paid_at", "-id").first()


def latest_payment_projection(client):
    if not client or not getattr(client, "pk", None):
        return None
    prefetched = getattr(client, "_payment_projections", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return client.payment_projections.select_related("deal").order_by("-updated_at", "-id").first()


def latest_legacy_payment_truth_deal(client):
    """Projectionless fallback used only during the 0089→0090 transition."""
    if not client or not getattr(client, "pk", None):
        return None
    if client.payment_projections.exists():
        return None
    return client.deals.exclude(
        payment_truth=IgDeal.PaymentTruth.UNVERIFIED
    ).order_by("-payment_truth_updated_at", "-id").first()


def _money(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _manager_confirmed_amount(review) -> Decimal | None:
    """Latest manager-verified amount for one review, or None when unstated.

    A ``payment_claim`` decision intentionally carries no amount: the manager
    acknowledged the customer's claim without measuring it.  Returning zero
    there would invent a fact, so absence stays absence.
    """
    decision = (
        review.decisions.filter(
            decision="manager_verified",
            confirmed_amount__isnull=False,
        )
        .exclude(confirmed_amount=0)
        .order_by("-id")
        .first()
    )
    return _money(decision.confirmed_amount) if decision else None


def _projection_net(projection) -> Decimal:
    return _money(projection.gross_amount) - _money(projection.refunded_amount)


def _linked_order_ids(client) -> set[int]:
    """Every real order this client is linked to, by any durable relation."""
    from management.ig_bot_models import IgOrderAssignment, IgOrderAttribution

    ids: set[int] = set()
    ids.update(
        IgOrderAssignment.objects.filter(
            client_id=client.pk,
            unassigned_at__isnull=True,
            order_id__isnull=False,
        ).values_list("order_id", flat=True)
    )
    ids.update(
        IgOrderAttribution.objects.filter(
            client_id=client.pk,
            order_id__isnull=False,
        ).values_list("order_id", flat=True)
    )
    ids.update(
        client.deals.filter(order_id__isnull=False).values_list("order_id", flat=True)
    )
    ids.update(
        client.payment_confirmation_reviews.filter(
            order_id__isnull=False
        ).values_list("order_id", flat=True)
    )
    return {int(value) for value in ids if value}


def confirmed_purchase_units(client) -> list[dict]:
    """One row per distinct confirmed purchase, with the money evidence behind it.

    The unit of counting is a **real order**, not a review and not a payment
    projection.  Production shows why: client #59 has a superseded review and a
    confirmed review pointing at the same ``order=296``, both carrying a
    ``manager_verified`` decision for 2100.00.  Counting reviews would report
    two purchases where one happened.

    Amount precedence, most authoritative first:

    1. provider projection net (``gross - refunded``);
    2. deal amount for a legacy projectionless verified deal;
    3. manager-confirmed amount;
    4. order payable total, but only when the order itself is fully paid.

    When none applies the unit still counts as a purchase and its amount stays
    ``None``.  A partially paid order without a measured amount is a real
    purchase of an unknown size; writing zero would be a quieter lie.
    """
    if not client or not getattr(client, "pk", None):
        return []
    from management.services.ig_order_amounts import order_amounts
    from orders.models import Order

    units: dict[object, dict] = {}

    def unit(key) -> dict:
        return units.setdefault(
            key, {"order_id": None, "amount": None, "sources": []}
        )

    # (1) Provider-confirmed deals — the strictest evidence there is.
    verified_deals = verified_payment_deals(client.deals.all()).select_related(
        "payment_projection"
    )
    for deal in verified_deals:
        key = ("order", deal.order_id) if deal.order_id else ("deal", deal.pk)
        row = unit(key)
        row["order_id"] = deal.order_id
        projection = getattr(deal, "payment_projection", None)
        amount = (
            _projection_net(projection)
            if projection is not None
            else _money(deal.amount)
        )
        if amount > 0:
            row["amount"] = amount
        row["sources"].append("provider_deal")

    # (2) Manager-confirmed reviews — client-anchored, because reviews on
    # production are opened from a conversation and carry no deal.
    reviews = (
        client.payment_confirmation_reviews.filter(manager_confirmed_review_q())
        .distinct()
        .order_by("id")
    )
    for review in reviews:
        key = ("order", review.order_id) if review.order_id else ("review", review.pk)
        row = unit(key)
        row["order_id"] = review.order_id
        row["sources"].append("manager_review")
        if row["amount"] is None:
            amount = _manager_confirmed_amount(review)
            if amount is not None and amount > 0:
                row["amount"] = amount

    # (3) Linked orders that are themselves marked paid.  These cannot create an
    # order, so trusting the manager's payment_status here opens no fraud path.
    linked_ids = _linked_order_ids(client)
    if linked_ids:
        orders = Order.objects.filter(pk__in=linked_ids).only(
            "id", "payment_status", "total_sum", "discount_amount"
        )
        for order in orders:
            status = str(order.payment_status or "")
            key = ("order", order.pk)
            if status not in CONFIRMED_ORDER_PAYMENT_STATUSES and key not in units:
                continue
            row = unit(key)
            row["order_id"] = order.pk
            if status in CONFIRMED_ORDER_PAYMENT_STATUSES:
                row["sources"].append(f"order_{status}")
            if row["amount"] is None and status in FULLY_PAID_ORDER_STATUSES:
                payable = order_amounts(order)["payable"]
                if payable > 0:
                    row["amount"] = payable

    return [units[key] for key in sorted(units, key=lambda item: (item[0], item[1]))]


def client_has_confirmed_purchase(client) -> bool:
    """CRM truth: has this person bought from us at all?

    Deliberately broader than ``client_has_verified_payment`` and deliberately
    kept out of every money path.  Extending the provider predicate instead
    would have blocked repeat sales: ``payment_link_allowed`` refuses a new
    invoice for a client with verified payment, and the contact-info safety net
    would start materializing orders for anyone who ever bought.
    """
    if not client or not getattr(client, "pk", None):
        return False
    annotated = getattr(client, "has_confirmed_purchase", None)
    if annotated is not None:
        return bool(annotated)
    # Э8.5: четыре запроса (deal, review, assignment, attribution) на один
    # ответ, и во время сборки промпта он нужен дважды. Область кэша — сборка,
    # не ход: см. `assemble_system_instruction`.
    from management.services.ig_turn_snapshot import prompt_cached

    return prompt_cached(
        f"confirmed_purchase:{client.pk}",
        lambda: _query_client_has_confirmed_purchase(client),
    )


def _query_client_has_confirmed_purchase(client) -> bool:
    if client_has_verified_payment(client):
        return True
    if client.payment_confirmation_reviews.filter(manager_confirmed_review_q()).exists():
        return True
    from management.ig_bot_models import IgOrderAssignment, IgOrderAttribution

    if IgOrderAssignment.objects.filter(
        client_id=client.pk,
        unassigned_at__isnull=True,
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    ).exists():
        return True
    return IgOrderAttribution.objects.filter(
        client_id=client.pk,
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    ).exists()


def active_client_order_payment_context(client) -> dict | None:
    """Return one active paid order owned by the client, without episode meaning.

    This is intentionally separate from :func:`current_payment_confirmation`.
    An active ``IgOrderAssignment`` establishes operational ownership of an
    order by a client; it does not establish that the order belongs to the
    client's current commercial episode.  Consumers may show this as context,
    but must not use it for payment automation, episode completion, or a
    current-payment presentation.
    """
    if not client or not getattr(client, "pk", None):
        return None

    def payload(order_id, payment_status, source):
        return {
            "order_id": int(order_id),
            # ``partial`` is the historical spelling of an order prepayment.
            "payment_status": (
                "paid" if payment_status == "paid" else "prepaid"
            ),
            "source": str(source or ""),
            "scope": "client",
        }

    # Client-list rows carry these bounded correlated annotations.  Their
    # presence, including a NULL id, must suppress a per-card fallback query.
    if hasattr(client, "active_assigned_paid_order_id"):
        order_id = getattr(client, "active_assigned_paid_order_id", None)
        if not order_id:
            return None
        return payload(
            order_id,
            getattr(client, "active_assigned_paid_order_payment_status", ""),
            getattr(client, "active_assigned_paid_order_source", ""),
        )

    from management.ig_bot_models import IgOrderAssignment

    row = (
        IgOrderAssignment.objects.filter(
            client_id=client.pk,
            unassigned_at__isnull=True,
            order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
        )
        .order_by("-assigned_at", "-id")
        .values("order_id", "order__payment_status", "order__source")
        .first()
    )
    if row is None:
        return None
    return payload(
        row["order_id"],
        row["order__payment_status"],
        row["order__source"],
    )


def current_payment_confirmation(client) -> dict:
    """Current payment fact for list presentation, with canonical provenance.

    This is intentionally narrower than ``client_has_confirmed_purchase``.
    The latter answers a lifetime CRM question; this function decides whether
    the current conversation may receive the high-salience green treatment.
    """
    empty = {"confirmed": False, "source": "", "note": ""}
    if not client or not getattr(client, "pk", None):
        return empty

    current_episode = getattr(client, "current_commercial_episode", None)
    current_episode_id = getattr(client, "current_commercial_episode_id", None)
    if current_episode_id and current_episode is None:
        from management.ig_bot_models import IgCommercialEpisode

        current_episode = IgCommercialEpisode.objects.filter(
            pk=current_episode_id,
            client_id=client.pk,
        ).first()

    if current_episode is not None:
        provider_confirmed = getattr(
            client, "has_current_episode_provider_payment", None
        )
        if provider_confirmed is None:
            prefetched = getattr(client, "_verified_payment_deals", None)
            if prefetched is not None:
                provider_confirmed = any(
                    deal.pk == current_episode.deal_id for deal in prefetched
                )
            else:
                provider_confirmed = bool(
                    current_episode.deal_id
                    and verified_payment_deals(
                        client.deals.filter(pk=current_episode.deal_id)
                    ).exists()
                )
    else:
        provider_confirmed = client_has_verified_payment(client)
    if provider_confirmed:
        return {
            "confirmed": True,
            "source": "provider",
            "note": "Оплату підтверджено платіжним провайдером.",
        }

    if current_episode is not None:
        manager_confirmed = getattr(
            client, "has_current_episode_manager_confirmation", None
        )
        if manager_confirmed is None:
            manager_confirmed = bool(
                current_episode.primary_payment_review_id
                and client.payment_confirmation_reviews.filter(
                    current_manager_confirmation_review_q(),
                    pk=current_episode.primary_payment_review_id,
                ).exists()
            )
    else:
        manager_confirmed = getattr(client, "has_current_manager_confirmation", None)
    if manager_confirmed is None:
        manager_confirmed = client.payment_confirmation_reviews.filter(
            current_manager_confirmation_review_q()
        ).exists()
    if manager_confirmed:
        return {
            "confirmed": True,
            "source": "manager",
            "note": "Поточну оплату підтверджено менеджером.",
        }

    if current_episode is not None:
        paid_order = getattr(
            client, "has_current_episode_paid_linked_order", None
        )
        if paid_order is None:
            from orders.models import Order

            paid_order = bool(
                current_episode.intended_order_id
                and Order.objects.filter(
                    pk=current_episode.intended_order_id,
                    payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
                ).exists()
            )
    else:
        paid_order = getattr(client, "has_current_paid_linked_order", None)
    if paid_order is None:
        from management.ig_bot_models import IgOrderAssignment, IgOrderAttribution

        paid_order = IgOrderAssignment.objects.filter(
            client_id=client.pk,
            unassigned_at__isnull=True,
            order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
        ).exists() or IgOrderAttribution.objects.filter(
            client_id=client.pk,
            order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
        ).exists()
    if paid_order:
        return {
            "confirmed": True,
            "source": "paid_order",
            "note": "Оплату підтверджено у прив'язаному замовленні.",
        }
    return empty


def historical_purchase_confirmation(client) -> dict:
    """Neutral history fact for an archived completed purchase."""
    empty = {"confirmed": False, "source": "", "label": "", "note": ""}
    if not client or not getattr(client, "pk", None):
        return empty

    archived = getattr(client, "has_historical_paid_archive", None)
    if archived is None:
        from management.ig_bot_models import IgPaymentConfirmationReview

        archived = client.payment_confirmation_reviews.filter(
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            resolution_kind=(
                IgPaymentConfirmationReview.ResolutionKind.HISTORICAL_PAID_ARCHIVED
            ),
        ).exists()
    if not archived:
        return empty
    return {
        "confirmed": True,
        "source": "historical_archive",
        "label": "Купував раніше",
        "note": "Архів: раніше завершене оплачене замовлення; не поточна оплата.",
    }


def annotate_confirmed_purchase(
    queryset: QuerySet,
    *,
    alias: str = "has_confirmed_purchase",
) -> QuerySet:
    """Annotate client rows with CRM purchase truth.

    ``Exists`` is not a style choice.  A plain
    ``filter(order_assignments__unassigned_at__isnull=True)`` matches clients
    with **no** assignments at all, because the LEFT JOIN yields NULL for them.
    Measured on production that naive form reported 289 buyers out of 289; the
    correct answer is 2.
    """
    from management.ig_bot_models import (
        IgOrderAssignment,
        IgOrderAttribution,
        IgPaymentConfirmationReview,
    )

    verified_deals = verified_payment_deals(
        IgDeal.objects.filter(client_id=OuterRef("pk"))
    )
    manager_reviews = IgPaymentConfirmationReview.objects.filter(
        manager_confirmed_review_q(),
        client_id=OuterRef("pk"),
    )
    paid_assignments = IgOrderAssignment.objects.filter(
        client_id=OuterRef("pk"),
        unassigned_at__isnull=True,
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    )
    paid_attributions = IgOrderAttribution.objects.filter(
        client_id=OuterRef("pk"),
        order__payment_status__in=CONFIRMED_ORDER_PAYMENT_STATUSES,
    )
    return queryset.annotate(**{
        alias: (
            Exists(verified_deals)
            | Exists(manager_reviews)
            | Exists(paid_assignments)
            | Exists(paid_attributions)
        )
    })


def recalculate_client_payment_aggregates(client) -> None:
    """Project confirmed purchases into the client summary fields.

    Previously computed from ``payment_projections`` alone, which is a provider
    ledger.  On production that table holds one row against 289 clients, so
    ``purchases_count`` was 0 for everyone including a customer with a paid
    order and a size exchange in transit (F-DATA-005).
    """
    if not client or not getattr(client, "pk", None):
        return
    units = confirmed_purchase_units(client)
    purchases = len(units)
    total = sum(
        (row["amount"] for row in units if row["amount"] is not None),
        Decimal("0.00"),
    )
    amount_unknown = any(row["amount"] is None for row in units)
    # ``total_spent`` is a money-shaped field fed by non-provider evidence here.
    # The flag lets the card say "confirmed by a manager" instead of implying a
    # provider ledger entry that does not exist.
    provider_unverified = any(
        "provider_deal" not in row["sources"] for row in units
    )
    flags = dict(client.conversion_flags or {})
    flags["is_buyer"] = purchases > 0
    if amount_unknown:
        flags["purchase_amount_unknown"] = True
    else:
        flags.pop("purchase_amount_unknown", None)
    if provider_unverified:
        flags["purchase_provider_unverified"] = True
    else:
        flags.pop("purchase_provider_unverified", None)
    client.__class__.objects.filter(pk=client.pk).update(
        purchases_count=purchases,
        total_spent=total,
        conversion_flags=flags,
    )
    client.purchases_count = purchases
    client.total_spent = total
    client.conversion_flags = flags


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
    historical_payment_evidence = Q(paid_at__isnull=False) & (
        Q(paid_amount__gt=0)
        | Q(
            payment_truth=IgDeal.PaymentTruth.UNVERIFIED,
            payment_status__in=VERIFIED_PAYMENT_STATUSES,
        )
    )
    hard_deals_without_truth = IgDeal.objects.filter(status__in=hard_deal_statuses).exclude(
        historical_payment_evidence
    ).exclude(manual_confirmation_q())
    verified_fields_without_hard_status = IgDeal.objects.filter(
        Q(payment_truth__in=VERIFIED_PAYMENT_TRUTHS)
        | Q(
            payment_truth=IgDeal.PaymentTruth.UNVERIFIED,
            payment_status__in=VERIFIED_PAYMENT_STATUSES,
            paid_at__isnull=False,
        )
    ).exclude(status__in=hard_deal_statuses)
    orders_without_truth = IgDeal.objects.filter(order__isnull=False).exclude(
        historical_payment_evidence
    ).exclude(manual_confirmation_q())
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
