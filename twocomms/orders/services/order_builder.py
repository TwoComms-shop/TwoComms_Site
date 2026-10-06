"""
Створення замовлення (orders.Order) з угоди IG-бота (management.IgDeal).

Викликається ПІСЛЯ підтвердженої оплати (рішення Q2). Ідемпотентно: одна угода →
одне замовлення. Дані Нової Пошти зберігаються текстом (Q3=a), ТТН оформлює
менеджер. Спільного сервісу створення замовлень у проєкті не було (логіка
дублювалась у checkout/monobank-в'ю) — це перша переюзабельна точка для бота.
"""
from __future__ import annotations

import json
from decimal import Decimal
from functools import wraps

from django.db import transaction
from django.utils import timezone


def _ensure_purchase_action(order, deal_id):
    from storefront.utm_tracking import ensure_order_purchase_action

    return ensure_order_purchase_action(
        order,
        metadata={
            'source': 'instagram_deal',
            'ig_deal_id': deal_id,
        },
    )


def _commercial_item_fingerprint(item):
    return (
        int(getattr(item, "product_id", None) or 0),
        int(getattr(item, "color_variant_id", None) or 0),
        str(getattr(item, "title", "") or "").strip().casefold(),
        str(getattr(item, "size", "") or "").strip().casefold(),
        str(getattr(item, "color_name_custom", "") or "").strip().casefold(),
        bool(getattr(item, "product_id", None) is None or getattr(item, "is_custom", False)),
        str(getattr(item, "fit_option_code", "") or "").strip().casefold(),
        json.dumps(
            getattr(item, "option_values", {}) or {},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        int(getattr(item, "qty", 0) or 0),
        str(Decimal(getattr(item, "unit_price", 0) or 0).quantize(Decimal("0.01"))),
        str(Decimal(getattr(item, "line_total", 0) or 0).quantize(Decimal("0.01"))),
    )


def assert_order_matches_commercial_contract(
    order,
    *,
    expected_fields,
    expected_items,
    declared_total,
    expected_delivery_contract=None,
):
    """Reject an idempotency-key collision with a different commercial order."""
    if Decimal(order.total_sum or 0).quantize(Decimal("0.01")) != declared_total:
        raise ValueError("Episode idempotency key points to an incompatible order total")
    for field, expected in expected_fields.items():
        if str(getattr(order, field, "") or "") != expected:
            raise ValueError(
                f"Episode idempotency key points to an incompatible order {field}"
            )
    if expected_delivery_contract:
        payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
        actual_delivery = payload.get("instagram_delivery_contract") or {}
        for key in ("merchandise_total", "delivery_amount", "payable_total", "prepaid", "payer_type"):
            if actual_delivery.get(key) != expected_delivery_contract.get(key):
                raise ValueError("Episode idempotency key points to an incompatible delivery contract")
    actual_items = list(order.items.all())
    if not actual_items:
        raise ValueError("Episode idempotency key points to an incompatible order without items")
    if sorted(map(_commercial_item_fingerprint, actual_items)) != sorted(
        map(_commercial_item_fingerprint, expected_items)
    ):
        raise ValueError("Episode idempotency key points to an incompatible order items")


def assert_episode_order_compatible(order, *, deal, deal_items, declared_total):
    assert_order_matches_commercial_contract(
        order,
        expected_fields={
            "full_name": str(deal.np_full_name or "")[:200],
            "phone": str(deal.np_phone or "")[:32],
            "city": str(deal.np_city or "")[:100],
            "np_office": str(deal.np_office or "")[:200],
        },
        expected_items=deal_items,
        declared_total=declared_total,
    )


def _deal_order_transaction(function):
    @wraps(function)
    def wrapped(deal, *, created_by=None):
        from management.models import IgClient
        from management.services.ig_commercial_episodes import commercial_episode_client_lock
        client_id = deal.__class__.objects.filter(pk=deal.pk).values_list('client_id', flat=True).first()
        if client_id is None or client_id != deal.client_id:
            raise ValueError('IG deal client identity changed')
        # Nested order-resolution callers must acquire this same barrier
        # before their outer transaction takes any client/review row locks.
        with commercial_episode_client_lock(client_id), transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=client_id)
            current = deal.__class__.objects.filter(pk=deal.pk, client_id=client_id).first()
            if current is None or client.hidden_at is not None:
                raise ValueError('IG deal client unavailable')
            current.client = client
            result = function(current, created_by=created_by)
            for field in ('order_id', 'status', 'order_truth_updated_at'):
                setattr(deal, field, getattr(current, field))
            return result
    return wrapped


@_deal_order_transaction
def _return_existing_order_after_payment_recheck(deal, *, created_by=None):
    """Serialize idempotent return with provider refund/reversal updates."""
    from management.ig_bot_models import IgPaymentConfirmationReview, IgPaymentProjection
    from management.services.bot_payment_truth import verified_payment_deals
    from management.services.ig_commercial_episodes import payment_truth_snapshot
    from management.services.ig_order_links import (
        authoritative_manager_decision,
        create_order_attribution,
    )

    review = (
        IgPaymentConfirmationReview.objects.select_for_update()
        .filter(
            deal_id=deal.pk,
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        )
        .order_by("-id")
        .first()
    )
    projection = IgPaymentProjection.objects.select_for_update().filter(
        deal_id=deal.pk
    ).first()
    decision = authoritative_manager_decision(review) if review else None
    payment_truth = payment_truth_snapshot(
        deal=deal,
        review=review,
        order=deal.order,
        projection=projection,
        decision=decision,
    )
    if payment_truth["needs_reconciliation"]:
        raise ValueError("IG payment reconciliation required before order materialization")
    provider_verified = bool(
        projection and projection.truth == deal.PaymentTruth.CONFIRMED
    )
    legacy_verified = bool(
        projection is None
        and verified_payment_deals(deal.__class__.objects.filter(pk=deal.pk)).exists()
    )
    if not provider_verified and not legacy_verified and not decision:
        raise ValueError(
            "IG order requires provider-confirmed payment or source-qualified manager decision"
        )
    create_order_attribution(
        deal.order,
        client=deal.client,
        deal=deal,
        review=review if decision else None,
        manager_decision=decision,
        creation_mode=(
            "provider_auto" if provider_verified
            else "manager_review" if decision
            else "provider_auto" if legacy_verified
            else "linked_existing"
        ),
        payment_source=(
            "provider_projection" if provider_verified
            else "manager_verified" if decision
            else "provider_attempt" if legacy_verified
            else "unknown"
        ),
        created_by=created_by,
    )
    if provider_verified or legacy_verified:
        _ensure_purchase_action(deal.order, deal.pk)
    return deal.order


@_deal_order_transaction
def create_order_from_deal(deal, *, created_by=None):
    """Створює Order + OrderItem з оплаченої угоди. Повертає Order.
    Якщо замовлення для угоди вже є — повертає його (ідемпотентність)."""
    if deal.order_id:
        return _return_existing_order_after_payment_recheck(
            deal,
            created_by=created_by,
        )

    from orders.models import Order, OrderItem

    is_prepay = deal.pay_type in {
        deal.PayType.PREPAYMENT,
        deal.PayType.PREPAY_200,
    }
    payment_status = "prepaid" if is_prepay else "paid"

    full_name = (
        deal.np_full_name
        or deal.client.display_name
        or deal.client.username
        or "IG клієнт"
    )
    phone = deal.np_phone or deal.client.phone or ""

    with transaction.atomic():
        # All manager/provider order-resolution paths lock review before
        # projection. MariaDB can otherwise deadlock when a manager links an
        # order while the provider worker materializes the same deal.
        from management.models import IgPaymentProjection
        from management.services.bot_payment_truth import (
            verified_payment_deals,
        )
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_order_links import (
            authoritative_manager_decision,
            create_order_attribution,
        )

        manual_review = IgPaymentConfirmationReview.objects.select_for_update().filter(
            deal_id=deal.pk,
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        ).first()
        projection = IgPaymentProjection.objects.select_for_update().filter(
            deal_id=deal.pk
        ).first()
        legacy_verified = (
            projection is None
            and verified_payment_deals(deal.__class__.objects.filter(pk=deal.pk)).exists()
        )
        manual_decision = authoritative_manager_decision(manual_review) if manual_review else None
        from management.services.ig_commercial_episodes import payment_truth_snapshot

        payment_truth = payment_truth_snapshot(
            deal=deal,
            review=manual_review,
            projection=projection,
            decision=manual_decision,
        )
        if payment_truth["needs_reconciliation"]:
            raise ValueError(
                "IG payment reconciliation required before order materialization"
            )
        projection_verified = bool(
            projection and projection.truth == deal.PaymentTruth.CONFIRMED
        )
        if not projection_verified and not legacy_verified and not manual_decision:
            raise ValueError(
                "IG order requires provider-confirmed payment or source-qualified manager decision"
            )
        manager_only = bool(manual_decision and not projection_verified and not legacy_verified)
        if manager_only:
            # A receipt/manager decision authorizes order preparation only; it
            # must never materialize as paid provider revenue.
            payment_status = "unpaid"
        manager_confirmed_amount = (
            Decimal(manual_decision.confirmed_amount).quantize(Decimal("0.01"))
            if manual_decision and manual_decision.confirmed_amount is not None
            else Decimal("0.00")
        )

        locked = deal.__class__.objects.select_for_update().get(pk=deal.pk)
        if locked.order_id:
            if projection_verified or legacy_verified:
                _ensure_purchase_action(locked.order, locked.pk)
            deal.order_id = locked.order_id
            deal.status = locked.status
            return locked.order
        locked_items = list(locked.items.all())
        if not locked_items:
            raise ValueError("IG deal requires at least one item")
        calculated_total = sum(
            (Decimal(item.line_total or 0) for item in locked_items),
            Decimal("0"),
        ).quantize(Decimal("0.01"))
        declared_total = Decimal(locked.amount or 0).quantize(Decimal("0.01"))
        if calculated_total != declared_total:
            raise ValueError("IG deal total does not match item line totals")

        # Do not persist/activate a commercial episode until payment authority
        # and the complete immutable item/total contract have both passed.
        from management.services.ig_commercial_episodes import ensure_episode_for_deal

        episode = ensure_episode_for_deal(locked)

        order, order_created = Order.objects.get_or_create(
            checkout_idempotency_key=f"ig-episode:{episode.pk}",
            defaults={
                "full_name": full_name[:200],
                "phone": phone[:32],
                "city": (deal.np_city or "")[:100],
                "np_office": (deal.np_office or "")[:200],
                "np_settlement_ref": (deal.np_settlement_ref or "")[:36],
                "np_city_ref": (deal.np_city_ref or "")[:36],
                "np_warehouse_ref": (deal.np_warehouse_ref or "")[:36],
                "pay_type": (
                    deal.PayType.PREPAYMENT
                    if manager_only
                    and manual_decision.verification_scope
                    == manual_decision.VerificationScope.PREPAYMENT
                    else deal.pay_type
                ),
                "payment_status": payment_status,
                "status": "new",
                "source": "manual",
                "sale_source": "Instagram",
                "created_by": created_by,
                "payment_provider": (
                    "monobank" if projection_verified
                    else "legacy_provider_transition" if legacy_verified
                    else ""
                ),
                "payment_invoice_id": deal.invoice_id or "",
                "payment_payload": {
                    "provider_payment_confirmed": bool(projection_verified),
                    "manager_payment_verified": bool(manual_decision),
                    "manual_payment_evidence_confirmed": bool(manual_decision),
                    "manager_payment_decision_id": getattr(manual_decision, "pk", None),
                    "manager_confirmed_amount": f"{manager_confirmed_amount:.2f}",
                    "manager_verification_scope": (
                        getattr(manual_decision, "verification_scope", "") or ""
                    ),
                    "manager_verification_source": (
                        getattr(manual_decision, "verification_source", "") or ""
                    ),
                    "manager_amount_source": (
                        getattr(manual_decision, "amount_source", "") or ""
                    ),
                    "manager_amount_evidence_message_ids": (
                        getattr(manual_decision, "amount_evidence_message_ids", None) or []
                    ),
                    "manager_payment_currency": (
                        getattr(manual_decision, "currency", "") or locked.currency or "UAH"
                    ),
                    "effective_confirmed_amount": (
                        str(projection.net_paid_amount)
                        if projection_verified
                        else f"{manager_confirmed_amount:.2f}"
                        if manual_decision
                        else "0.00"
                    ),
                    "legacy_payment_transition": bool(legacy_verified),
                    "instagram_deal_id": locked.pk,
                    "instagram_commercial_episode_id": episode.pk,
                    "requested_payment_amount": str(locked.payable_amount()),
                    "paid_value": str(
                        projection.net_paid_amount if projection_verified else Decimal("0")
                    ),
                    "negotiated_order_total": str(locked.amount or Decimal("0")),
                    "payment_amount_evidence_message_ids": (
                        locked.requested_payment_evidence_ids or []
                    ),
                },
                "total_sum": deal.amount or Decimal("0"),
            },
        )
        if not order_created:
            assert_episode_order_compatible(
                order,
                deal=locked,
                deal_items=locked_items,
                declared_total=declared_total,
            )

        items = []
        for it in locked_items:
            items.append(
                OrderItem(
                    order=order,
                    product=it.product,
                    color_variant=it.color_variant,
                    title=it.title,
                    size=it.size or "",
                    fit_option_code=it.fit_option_code or "",
                    fit_option_label=it.fit_option_label or "",
                    option_values=it.option_values or {},
                    option_labels=it.option_labels or {},
                    qty=it.qty,
                    unit_price=it.unit_price,
                    line_total=it.line_total,
                    is_custom=(it.product_id is None),
                )
            )
        if items and (order_created or not order.items.exists()):
            OrderItem.objects.bulk_create(items)

        create_order_attribution(
            order,
            client=locked.client,
            deal=locked,
            review=manual_review if manual_decision else None,
            manager_decision=manual_decision,
            creation_mode=(
                "provider_auto" if projection_verified
                else "manager_review" if manual_decision
                else "provider_auto" if legacy_verified
                else "linked_existing"
            ),
            payment_source=(
                "provider_projection" if projection_verified
                else "manager_verified" if manual_decision
                else "provider_attempt" if legacy_verified
                else "unknown"
            ),
            created_by=created_by,
        )

        if projection_verified or legacy_verified:
            _ensure_purchase_action(order, locked.pk)

        locked.order = order
        locked.status = locked.Status.ORDER_CREATED
        locked.order_truth_updated_at = timezone.now()
        locked.save(update_fields=[
            "order",
            "status",
            "order_truth_updated_at",
            "updated_at",
        ])
        deal.order_id = order.id
        deal.status = locked.Status.ORDER_CREATED
        deal.order_truth_updated_at = locked.order_truth_updated_at

    # Client summary is projected from payment truth. Legacy projectionless
    # rows retain the old one-time behavior only during migration transition.
    try:
        from management.models import IgClient

        c = deal.client
        c.current_product = None
        c.save(update_fields=["current_product", "updated_at"])
        # IMP-013: previously this incremented ``purchases_count`` in place while
        # ``recalculate_client_payment_aggregates`` recomputed the same fields
        # from payment projections. Two writers with different units meant the
        # recompute silently erased the increment. One writer now owns them.
        from management.services.bot_payment_truth import (
            recalculate_client_payment_aggregates,
        )

        recalculate_client_payment_aggregates(c)
        c.set_stage(IgClient.Stage.ORDER_CREATED, reason="order")
    except Exception:
        pass

    try:
        from management.services.bot_conversation_analysis import (
            schedule_client_truth_analysis,
        )

        schedule_client_truth_analysis(deal.client, trigger="order_truth")
    except Exception:
        # Periodic reconciliation repairs a missed best-effort order trigger.
        pass

    return order
