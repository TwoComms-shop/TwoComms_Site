from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import wraps
import hashlib
import hmac
import json
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone


OVERRIDE_REASON_CODES = frozenset({
    "total_mismatch",
    "phone_mismatch",
    "items_mismatch",
    "manual_review",
    "historical_fulfilled_order",
    "payment_state_mismatch",
    "historical_import",
})


def identity_digest(igsid: str) -> str:
    """Return a non-reversible, secret-bound Instagram identity digest."""
    value = str(igsid or "").strip()
    if not value:
        return ""
    key = str(getattr(settings, "SECRET_KEY", "")).encode("utf-8")
    return hmac.new(key, ("instagram:" + value).encode("utf-8"), hashlib.sha256).hexdigest()


def _reason_code(value, mismatches):
    candidate = str(value or "").strip().lower()
    if candidate in OVERRIDE_REASON_CODES:
        return candidate
    if "items" in mismatches:
        return "items_mismatch"
    if "phone" in mismatches:
        return "phone_mismatch"
    if "total" in mismatches:
        return "total_mismatch"
    return "manual_review"


def authoritative_manager_decision(review):
    from management.ig_bot_models import IgPaymentReviewDecision

    if not review or review.status != review.Status.CONFIRMED:
        return None
    decision = review.decisions.order_by("-id").first()
    if not decision:
        return None
    if decision.decision != IgPaymentReviewDecision.Decision.MANAGER_VERIFIED:
        return None
    if decision.verification_source != "manager":
        return None
    if decision.actor_source not in {
        IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
        IgPaymentReviewDecision.ActorSource.TELEGRAM_USER,
    }:
        return None
    if not str(decision.actor_external_id or "").strip():
        return None
    if not decision.confirmed_amount or decision.confirmed_amount <= 0:
        return None
    if not str(decision.currency or "").strip():
        return None
    if decision.verification_scope not in {
        IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
        IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
    }:
        return None
    return decision


def order_fulfillment_payment_verified(order):
    """Authorize fulfillment from provider truth or an audited manager action."""
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    reconciliation = payload.get("ig_payment_reconciliation") or {}
    if isinstance(reconciliation, dict) and (
        reconciliation.get("needs_reconciliation")
        or reconciliation.get("automatic_fulfillment_blocked")
    ):
        return False
    raw_paid = str(order.payment_status or "") in {"paid", "prepaid", "partial"}
    if str(order.source or "") != "manual":
        return raw_paid
    action = payload.get("manual_payment_action") or {}
    if raw_paid and isinstance(action, dict) and (
        action.get("actor_id") and action.get("source") == "management_user"
        and action.get("payment_status") == order.payment_status
    ):
        return True
    if raw_paid and (payload.get("provider_payment_confirmed") or payload.get("legacy_payment_transition")):
        return True
    if not payload.get("manual_payment_evidence_confirmed"):
        return False
    try:
        attribution = order.instagram_attribution
    except Exception:
        return False
    review = attribution.payment_review
    decision = authoritative_manager_decision(review)
    if not decision or decision.pk != payload.get("manager_payment_decision_id"):
        return False
    from management.services.ig_commercial_episodes import payment_truth_snapshot
    from management.services.ig_order_amounts import order_amounts

    payable = order_amounts(order)["payable"]
    amount = Decimal(decision.confirmed_amount or 0)
    if (
        decision.verification_scope == "full_payment" and amount != payable
        or decision.verification_scope == "prepayment" and not 0 < amount < payable
    ):
        return False
    truth = payment_truth_snapshot(review=review, order=order, decision=decision)
    return bool(truth["authoritative_for_fulfillment"])


def order_link_override_requirements(review, order):
    """Return the structured override contract shared by selector and mutation."""
    decision = authoritative_manager_decision(review)
    if not decision:
        raise ValueError("Потрібне source-qualified рішення менеджера")
    from management.services.ig_order_amounts import order_amounts

    terminal_order = order.status in {"ship", "done"}
    decision_scope = str(getattr(decision, "verification_scope", "") or "")
    payment_state_incompatible = bool(
        (decision_scope == "full_payment" and order.payment_status != "paid")
        or (
            decision_scope == "prepayment"
            and order.payment_status not in {"prepaid", "partial", "paid"}
        )
    )
    decision_amount = Decimal(decision.confirmed_amount or 0).quantize(Decimal("0.01"))
    order_total = order_amounts(order)["payable"]
    payment_amount_incompatible = bool(
        (decision_scope == "full_payment" and decision_amount != order_total)
        or (decision_scope == "prepayment" and decision_amount >= order_total)
    )
    payment_incompatible = payment_state_incompatible or payment_amount_incompatible
    conflicts = []
    if terminal_order:
        conflicts.append("terminal_order")
    if payment_state_incompatible:
        conflicts.append("payment_state_mismatch")
    if payment_amount_incompatible:
        conflicts.append("payment_amount_mismatch")
    if terminal_order and payment_incompatible:
        allowed_codes = ["historical_import"]
    elif terminal_order:
        allowed_codes = ["historical_fulfilled_order", "historical_import"]
    elif payment_incompatible:
        allowed_codes = ["payment_state_mismatch", "historical_import"]
    else:
        allowed_codes = []
    return {
        "decision": decision,
        "terminal_order": terminal_order,
        "payment_incompatible": payment_incompatible,
        "payment_state_incompatible": payment_state_incompatible,
        "payment_amount_incompatible": payment_amount_incompatible,
        "decision_amount": decision_amount,
        "order_total": order_total,
        "conflicts": conflicts,
        "allowed_codes": allowed_codes,
    }


def _item_snapshot(items):
    result = []
    for item in items:
        result.append({
            "product_id": item.product_id,
            "color_variant_id": item.color_variant_id,
            "title": item.title,
            "is_custom": bool(item.product_id is None or getattr(item, "is_custom", False)),
            "color_name_custom": getattr(item, "color_name_custom", "") or "",
            "size": item.size or "",
            "fit_option_code": getattr(item, "fit_option_code", "") or "",
            "fit_option_label": getattr(item, "fit_option_label", "") or "",
            "option_values": getattr(item, "option_values", {}) or {},
            "option_labels": getattr(item, "option_labels", {}) or {},
            "qty": item.qty,
            "unit_price": str(item.unit_price),
            "line_total": str(item.line_total),
            "price_source": getattr(item, "price_source", "") or "",
            "price_evidence_message_ids": getattr(item, "price_evidence_message_ids", []) or [],
        })
    return result


@transaction.atomic
def create_order_attribution(
    order,
    *,
    client,
    creation_mode,
    payment_source,
    deal=None,
    review=None,
    manager_decision=None,
    created_by=None,
):
    from management.ig_bot_models import IgOrderAttribution

    deal_items = list(deal.items.all()) if deal is not None else []
    source_items = deal_items or list(order.items.all())
    evidence_ids = []
    price_sources = []
    for item in source_items:
        price_sources.append(getattr(item, "price_source", "") or "")
        evidence_ids.extend(getattr(item, "price_evidence_message_ids", []) or [])
    review_negotiated_total = None
    if review is not None:
        evidence = review.evidence if isinstance(review.evidence, dict) else {}
        draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
        try:
            review_negotiated_total = Decimal(str(draft.get("quoted_total"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            review_negotiated_total = None
    from management.services.ig_order_amounts import order_amounts

    actual_order_total = order_amounts(order)["payable"]
    attribution, _created = IgOrderAttribution.objects.get_or_create(
        order=order,
        defaults={
            "client": client,
            "deal": deal,
            "payment_review": review,
            "manager_decision": manager_decision,
            "creation_mode": creation_mode,
            "payment_source": payment_source,
            "identity_digest": identity_digest(client.igsid),
            "evidence_watermark_message_id": getattr(review, "watermark_message_id", 0) or 0,
            "item_provenance": _item_snapshot(source_items),
            "negotiated_total": (
                getattr(deal, "amount", None)
                or review_negotiated_total
                or actual_order_total
            ),
            "price_source": (
                "mixed"
                if len({value for value in price_sources if value}) > 1
                else next((value for value in price_sources if value), "")
            ),
            "price_evidence_message_ids": sorted({int(value) for value in evidence_ids if str(value).isdigit()}),
            "created_by": created_by,
        },
    )
    if attribution.client_id != client.pk:
        raise ValueError("Замовлення вже прив'язано до іншого Instagram-клієнта")
    if attribution.deal_id and deal and attribution.deal_id != deal.pk:
        raise ValueError("Замовлення вже належить іншій угоді цього Instagram-клієнта")
    from management.services.ig_order_assignments import (
        assignment_source_for_attribution,
        link_order_to_client,
    )

    link_order_to_client(
        order,
        client=client,
        actor=created_by,
        source=assignment_source_for_attribution(
            creation_mode=creation_mode,
            payment_source=payment_source,
        ),
        actor_source=(
            "management_user" if created_by is not None else "automation"
        ),
        reason_code="attribution_created",
        reason="Created from Instagram attribution",
    )
    from management.services.ig_commercial_episodes import (
        bind_episode_order,
        ensure_episode_for_attribution,
        ensure_episode_for_deal,
        ensure_episode_for_review,
    )

    try:
        episode = attribution.commercial_episode
    except Exception:
        episode = None
    if episode is None:
        if review is not None:
            episode = ensure_episode_for_review(review)
        elif deal is not None:
            episode = ensure_episode_for_deal(deal)
        else:
            episode = ensure_episode_for_attribution(attribution)
    if episode is not None:
        bind_episode_order(
            episode,
            order,
            attribution=attribution,
            creation_mode=creation_mode,
            payment_source=payment_source,
        )
    return attribution


def _find_order_exact(identifier):
    from orders.models import Order

    value = str(identifier or "").strip()
    if not value:
        raise ValueError("Вкажіть точний номер замовлення")
    return Order.objects.select_for_update().filter(order_number=value).first()


def _commercial_fingerprint(item):
    return (
        int(item.product_id or 0),
        int(item.color_variant_id or 0),
        str(item.title or "").strip().casefold(),
        str(item.size or "").strip().casefold(),
        str(getattr(item, "color_name_custom", "") or "").strip().casefold(),
        bool(item.product_id is None or getattr(item, "is_custom", False)),
        str(getattr(item, "fit_option_code", "") or "").strip().casefold(),
        json.dumps(
            getattr(item, "option_values", {}) or {},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        int(item.qty or 0),
        str(Decimal(item.unit_price or 0).quantize(Decimal("0.01"))),
    )


def _review_link_transaction(function):
    @wraps(function)
    def guarded(review, *args, **kwargs):
        from management.services.ig_payment_review import _payment_review_mutation
        with _payment_review_mutation(review):
            return function(review, *args, **kwargs)
    return guarded


@_review_link_transaction
def link_existing_order_to_review(
    review,
    *,
    order_identifier,
    actor,
    override_code="",
    override_reason="",
):
    from management.ig_bot_models import (
        IgDeal,
        IgOrderAttribution,
        IgOrderLinkEvent,
        IgPaymentConfirmationReview,
        IgPaymentProjection,
    )

    from management.services.ig_payment_review import _lock_payment_review
    locked_review = _lock_payment_review(review)
    client = locked_review.client
    if client.hidden_at:
        raise ValueError("Прихований клієнт виключений з операцій")
    if locked_review.status != locked_review.Status.CONFIRMED:
        raise ValueError("Перевірка оплати не підтверджена")
    decision = authoritative_manager_decision(locked_review)
    if not decision:
        raise ValueError("Потрібне source-qualified рішення менеджера")
    from management.services.ig_commercial_episodes import payment_truth_snapshot

    projection = None
    if locked_review.deal_id:
        projection = IgPaymentProjection.objects.select_for_update().filter(
            deal_id=locked_review.deal_id
        ).first()
    payment_truth = payment_truth_snapshot(
        deal=locked_review.deal,
        review=locked_review,
        order=locked_review.order,
        projection=projection,
        decision=decision,
    )
    if payment_truth["needs_reconciliation"]:
        raise ValueError("Потрібна звірка сум оплати перед прив'язкою замовлення")

    order = _find_order_exact(order_identifier)
    if not order:
        raise ValueError("Замовлення з таким точним номером не знайдено")
    if order.status == "cancelled":
        raise ValueError("скасоване замовлення не можна прив'язати")
    if locked_review.order_id and locked_review.order_id != order.pk:
        raise ValueError("Перевірку вже прив'язано до іншого замовлення")

    attribution = IgOrderAttribution.objects.select_for_update().filter(order=order).first()
    if attribution and attribution.client_id != client.pk:
        raise ValueError("Замовлення вже прив'язано до іншого Instagram-клієнта")
    existing_deals = IgDeal.objects.filter(order=order)
    if locked_review.deal_id:
        existing_deals = existing_deals.exclude(pk=locked_review.deal_id)
    if existing_deals.exclude(client_id=client.pk).exists():
        raise ValueError("Замовлення вже прив'язано до іншого Instagram-клієнта")
    if existing_deals.filter(client_id=client.pk).exists():
        raise ValueError("Замовлення вже пов'язано з іншою угодою цього клієнта")
    if IgPaymentConfirmationReview.objects.filter(order=order).exclude(client_id=client.pk).exists():
        raise ValueError("Замовлення вже прив'язано до іншого Instagram-клієнта")
    if (
        locked_review.deal_id
        and locked_review.deal.order_id
        and locked_review.deal.order_id != order.pk
    ):
        raise ValueError("Угоду вже прив'язано до іншого замовлення")

    # A fully materialized repeated exact link is idempotent even when the
    # first attempt used an override reason that is not repeated by the UI.
    # Historical rows may already point review/deal at the order while still
    # missing attribution/episode origin; those must continue through repair.
    if locked_review.order_id == order.pk:
        from management.ig_bot_models import IgCommercialEpisode

        complete_episode = None
        if attribution:
            complete_episode = IgCommercialEpisode.objects.select_for_update().filter(
                intended_order=order,
                order_attribution=attribution,
                primary_payment_review=locked_review,
            ).first()
        if complete_episode:
            return order

    mismatches = {}
    decision_amount = Decimal(decision.confirmed_amount or 0).quantize(Decimal("0.01"))
    from management.services.ig_order_amounts import order_amounts

    order_total = order_amounts(order)["payable"]
    if (
        decision.verification_scope == decision.VerificationScope.FULL_PAYMENT
        and decision_amount != order_total
    ):
        mismatches["payment_amount"] = {
            "confirmed": str(decision_amount),
            "order": str(order_total),
            "scope": decision.verification_scope,
        }
    elif (
        decision.verification_scope == decision.VerificationScope.PREPAYMENT
        and decision_amount >= order_total
    ):
        mismatches["payment_amount"] = {
            "confirmed": str(decision_amount),
            "order": str(order_total),
            "scope": decision.verification_scope,
        }
    if locked_review.deal_id:
        expected = Decimal(locked_review.deal.amount or 0)
        actual = order_total
        if expected != actual:
            mismatches["total"] = {"review": str(expected), "order": str(actual)}
        deal_phone = re.sub(r"\D", "", locked_review.deal.np_phone or "")
        order_phone = re.sub(r"\D", "", order.phone or "")
        if deal_phone and order_phone and deal_phone != order_phone:
            mismatches["phone"] = {"mismatch": True}
        deal_items = sorted(_commercial_fingerprint(item) for item in locked_review.deal.items.all())
        order_items = sorted(_commercial_fingerprint(item) for item in order.items.all())
        if deal_items != order_items:
            mismatches["items"] = {
                "review": [list(value) for value in deal_items],
                "order": [list(value) for value in order_items],
            }
    override_requirements = order_link_override_requirements(locked_review, order)
    terminal_requires_override = override_requirements["terminal_order"]
    payment_incompatible = override_requirements["payment_incompatible"]
    code = str(override_code or "").strip().lower()
    note = str(override_reason or "").strip()
    if code and code not in OVERRIDE_REASON_CODES:
        raise ValueError("Невідома структурована причина override")
    if override_requirements["allowed_codes"] and code not in override_requirements["allowed_codes"]:
        if terminal_requires_override and payment_incompatible:
            raise ValueError("Для історичного замовлення з іншим станом оплати потрібен override historical_import")
        if terminal_requires_override:
            raise ValueError("Для відправленого/виконаного замовлення потрібна структурована причина override")
        raise ValueError(
            "Стан оплати або сума замовлення не збігаються; "
            "потрібна структурована причина override"
        )
    if (terminal_requires_override or payment_incompatible) and not note:
        raise ValueError("Додайте пояснення до структурованої причини override")
    if mismatches and not str(override_reason or "").strip():
        raise ValueError("Дані замовлення не збігаються; потрібна причина override")
    reason_code = code or _reason_code(override_reason, mismatches)

    from management.ig_bot_models import IgCommercialEpisode

    review_episode = IgCommercialEpisode.objects.select_for_update().filter(
        primary_payment_review=locked_review
    ).first()
    order_episode = IgCommercialEpisode.objects.select_for_update().filter(
        intended_order=order
    ).first()
    if order_episode and review_episode and order_episode.pk != review_episode.pk:
        raise ValueError("Замовлення вже належить іншому комерційному епізоду")
    if (
        order_episode
        and order_episode.primary_payment_review_id not in {None, locked_review.pk}
    ):
        raise ValueError("Замовлення вже належить іншому комерційному епізоду")

    if not attribution:
        attribution = create_order_attribution(
            order,
            client=client,
            deal=locked_review.deal,
            review=locked_review,
            manager_decision=decision,
            creation_mode="linked_existing",
            payment_source="manager_verified",
            created_by=actor,
        )
    from management.services.ig_commercial_episodes import (
        append_episode_event,
        bind_episode_order,
        ensure_episode_for_review,
    )

    try:
        episode = attribution.commercial_episode
    except Exception:
        episode = order_episode or ensure_episode_for_review(locked_review)
    if review_episode and review_episode.pk != episode.pk:
        raise ValueError("Замовлення вже належить іншому комерційному епізоду")
    if episode.primary_payment_review_id not in {None, locked_review.pk}:
        raise ValueError("Замовлення вже належить іншому комерційному епізоду")
    if not episode.primary_payment_review_id:
        episode.primary_payment_review = locked_review
        episode.save(update_fields=["primary_payment_review", "updated_at"])
    bind_episode_order(
        episode,
        order,
        attribution=attribution,
        creation_mode="linked_existing",
        payment_source="manager_verified",
        override_snapshot={
            "code": reason_code,
            "note": note,
            "mismatches": mismatches,
            "terminal_order": terminal_requires_override,
            "payment_incompatible": payment_incompatible,
        },
    )
    provider_verified = bool(
        projection is not None
        and projection.truth
        in {
            IgDeal.PaymentTruth.CONFIRMED,
            IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
        }
    )
    confirmed_amount = Decimal(decision.confirmed_amount or 0).quantize(
        Decimal("0.01")
    )
    provider_confirmed_amount = Decimal(
        payment_truth["provider_confirmed_amount"] or "0"
    ).quantize(Decimal("0.01"))
    effective_confirmed_amount = (
        provider_confirmed_amount
        if provider_verified
        else confirmed_amount
    )
    is_partial_payment = bool(
        effective_confirmed_amount > 0
        and effective_confirmed_amount < order_total
    )
    preserve_existing_paid_status = bool(
        not provider_verified and order.payment_status == "paid"
    )
    payload = dict(order.payment_payload or {})
    payload.update({
        "manual_payment_preset": (
            "provider_prepayment"
            if provider_verified and is_partial_payment
            else
            "manager_prepayment"
            if decision.verification_scope == decision.VerificationScope.PREPAYMENT
            and not provider_verified
            else "paid_full"
            if preserve_existing_paid_status
            else "unpaid_full"
        ),
        "manual_payment_evidence_confirmed": True,
        "provider_payment_confirmed": provider_verified,
        "manager_payment_decision_id": decision.pk,
        "manager_confirmed_amount": f"{confirmed_amount:.2f}",
        "manager_verification_scope": decision.verification_scope,
        "manager_verification_source": decision.verification_source,
        "manager_amount_source": decision.amount_source or "",
        "manager_amount_evidence_message_ids": (
            decision.amount_evidence_message_ids or []
        ),
        "manager_payment_currency": decision.currency or "UAH",
        "effective_confirmed_amount": f"{effective_confirmed_amount:.2f}",
        "paid_value": f"{provider_confirmed_amount:.2f}",
        "negotiated_order_total": payment_truth["order_total"],
    })
    order.pay_type = "prepayment" if is_partial_payment else "online_full"
    if provider_verified:
        order.payment_status = "prepaid" if is_partial_payment else "paid"
        order.payment_provider = order.payment_provider or "monobank"
    elif preserve_existing_paid_status:
        order.payment_status = "paid"
    else:
        order.payment_status = "unpaid"
    order.payment_payload = payload
    order.save(update_fields=[
        "pay_type",
        "payment_status",
        "payment_provider",
        "payment_payload",
        "updated",
    ])
    locked_review.order = order
    locked_review.save(update_fields=["order", "updated_at"])
    from management.services.ig_order_assignments import _advance_stage_from_order

    _advance_stage_from_order(client, order)
    if locked_review.deal_id:
        locked_review.deal.order = order
        locked_review.deal.status = IgDeal.Status.ORDER_CREATED
        locked_review.deal.order_truth_updated_at = timezone.now()
        locked_review.deal.save(update_fields=[
            "order", "status", "order_truth_updated_at", "updated_at",
        ])
    IgOrderLinkEvent.objects.get_or_create(
        order=order,
        review=locked_review,
        event_kind="linked",
        defaults={
            "client": client,
            "actor": actor,
            "reason_code": reason_code,
            "mismatch_snapshot": mismatches,
        },
    )
    append_episode_event(
        episode,
        dedupe_key=f"episode:{episode.pk}:review:{locked_review.pk}:linked:{order.pk}",
        event_type="existing_order_linked",
        source="manager",
        evidence={
            "review_id": locked_review.pk,
            "order_id": order.pk,
            "reason_code": reason_code,
            "reason_note": note,
            "mismatches": mismatches,
        },
    )
    return order
