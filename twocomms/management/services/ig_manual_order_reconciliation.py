"""Audited repair for a manager-created paid Instagram order."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction


def _review_message_ids(review) -> set[int]:
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    values = set()
    for container in (
        evidence.get("messages"),
        evidence.get("evidence"),
        evidence.get("media"),
    ):
        for item in container or []:
            if not isinstance(item, dict):
                continue
            value = (
                item.get("message_id")
                or item.get("source_message_id")
                or item.get("id")
            )
            if str(value).isdigit():
                values.add(int(value))
    for item in evidence.get("amount_evidence") or []:
        if isinstance(item, dict) and str(item.get("message_id")).isdigit():
            values.add(int(item["message_id"]))
    return values


@transaction.atomic
def reconcile_manager_paid_order(
    *,
    client,
    order,
    actor,
    evidence_message_id: int,
):
    """Create/confirm one receipt review, then bind the exact paid order.

    The explicit customer evidence id and staff actor are mandatory.  The
    order's ``payment_status`` alone never creates payment truth.
    """
    from management.ig_bot_models import IgPaymentConfirmationReview
    from management.services.ig_order_links import link_existing_order_to_review
    from management.services.ig_payment_review import (
        create_payment_review,
        record_review_decision,
    )
    from management.services.ig_order_amounts import order_amounts

    if str(getattr(order, "payment_status", "") or "").casefold() not in {
        "paid", "prepaid", "partial",
    }:
        raise ValueError("Замовлення не має підтвердженого стану оплати.")
    if (
        str(getattr(order, "source", "") or "").casefold() != "manual"
        or str(getattr(order, "sale_source", "") or "").casefold() != "instagram"
    ):
        raise ValueError("Repair дозволений лише для ручного Instagram-замовлення.")
    if not actor or not getattr(actor, "is_staff", False) and not getattr(actor, "is_superuser", False):
        raise ValueError("Потрібен staff-користувач як автор рішення.")
    try:
        evidence_message_id = int(evidence_message_id)
    except (TypeError, ValueError):
        raise ValueError("Вкажіть коректний ID повідомлення-доказу.")
    if evidence_message_id <= 0:
        raise ValueError("Вкажіть коректний ID повідомлення-доказу.")

    review = None
    candidates = (
        IgPaymentConfirmationReview.objects.select_for_update()
        .filter(
            client=client,
            order__isnull=True,
            status__in=[
                IgPaymentConfirmationReview.Status.PENDING,
                IgPaymentConfirmationReview.Status.CONFIRMED,
            ],
        )
        .order_by("-created_at", "-pk")
    )
    for candidate in candidates:
        if evidence_message_id in _review_message_ids(candidate):
            review = candidate
            break
    if review is None:
        review = create_payment_review(client, watermark=evidence_message_id)
        if review is not None and evidence_message_id not in _review_message_ids(review):
            review = None
    if review is None:
        raise ValueError("У переписці не знайдено підтверджену заяву про оплату.")
    if evidence_message_id not in _review_message_ids(review):
        raise ValueError("Повідомлення-доказ не належить цій перевірці оплати.")

    if review.status == IgPaymentConfirmationReview.Status.PENDING:
        total = Decimal(str(order_amounts(order)["payable"])).quantize(Decimal("0.01"))
        record_review_decision(
            review,
            actor=actor,
            decision="manager_verified",
            verification_scope="full_payment",
            confirmed_amount=total,
            order_total_amount=total,
            reason_code="manual_review",
            reason_text=(
                f"Менеджер звірив paid Instagram-замовлення із доказом "
                f"повної оплати, повідомлення #{evidence_message_id}."
            ),
        )
        review.refresh_from_db()

    if review.status != IgPaymentConfirmationReview.Status.CONFIRMED:
        raise ValueError("Перевірка оплати не підтверджена менеджером.")
    linked = link_existing_order_to_review(
        review,
        order_identifier=order.order_number,
        actor=actor,
    )
    return review, linked
