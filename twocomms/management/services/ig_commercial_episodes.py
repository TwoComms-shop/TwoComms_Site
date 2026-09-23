from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import RLock
from weakref import WeakValueDictionary

from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from orders.fulfillment_truth import (
    nova_poshta_delivery_confirmed_at,
)


_EPISODE_LOCKS = WeakValueDictionary()
_EPISODE_LOCKS_GUARD = RLock()
_HELD_DB_LOCKS = ContextVar("ig_episode_db_locks", default=frozenset())

FALSE_HISTORICAL_PURCHASE_REASON = "false_historical_purchase_corrected"


@contextmanager
def commercial_episode_client_lock(client_id: int):
    """Serialize episode/open-order decisions across MariaDB workers."""
    lock_name = f"twocomms:ig-episode:{int(client_id)}"
    if connection.vendor in {"mysql", "mariadb"}:
        held_locks = _HELD_DB_LOCKS.get()
        if lock_name in held_locks:
            yield
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 15)", [lock_name])
            acquired = cursor.fetchone()[0]
        if acquired != 1:
            raise RuntimeError("Could not acquire Instagram commercial episode lock")
        token = _HELD_DB_LOCKS.set(held_locks | {lock_name})
        try:
            yield
        finally:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
            except Exception:
                pass
            _HELD_DB_LOCKS.reset(token)
        return
    with _EPISODE_LOCKS_GUARD:
        lock = _EPISODE_LOCKS.setdefault(int(client_id), RLock())
    with lock:
        yield


class OrderResolutionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedClientOrder:
    order: object
    episode: object | None
    attribution: object | None
    match_kind: str


def review_has_false_historical_purchase_correction(review) -> bool:
    """Return whether an audited owner correction retired this review.

    A corrected historical review is a tombstone, not a duplicate payment
    attempt.  Runtime reconciliation must never turn it back into a commercial
    episode merely because its original source row still exists for audit.
    """
    if review is None or not getattr(review, "pk", None):
        return False
    if str(getattr(review, "status", "") or "") != "superseded":
        return False
    if str(getattr(review, "supersede_reason", "") or "") == (
        FALSE_HISTORICAL_PURCHASE_REASON
    ):
        return True
    try:
        return review.decisions.filter(
            decision="manager_rejected",
            reason_code=FALSE_HISTORICAL_PURCHASE_REASON,
        ).exists()
    except Exception:
        return False


def derive_current_episode_stage(client, episode=None) -> str:
    """Derive the current funnel stage from current-episode evidence only.

    This deliberately seeds the monotonic reducer at ``NEW`` instead of the
    mutable client stage.  It is used when a false historical-payment decision
    poisoned that stage with ``paid/order_created/done``.  Stage snapshots are
    not inputs either: an episode opened while the poisoned stage was current
    necessarily copied the same bad value.
    """
    from management.ig_bot_models import (
        IgClient,
        IgConversationSignal,
        IgDeal,
    )
    from management.services.bot_payment_truth import current_payment_confirmation
    from management.services.bot_sales_classifier import observed_stage_target
    from management.services.ig_funnel_reset import current_message_floor

    if not client or not getattr(client, "pk", None):
        return IgClient.Stage.NEW
    if bool(getattr(client, "manager_takeover", False)):
        return IgClient.Stage.LEAD_TO_MANAGER
    if episode is None:
        episode = getattr(client, "current_commercial_episode", None)
    if episode is None or (
        episode.client_id != client.pk
        or episode.open_slot != 1
        or episode.state != episode.State.ACTIVE
    ):
        return IgClient.Stage.NEW

    deal = getattr(episode, "deal", None) if episode.deal_id else None
    order = getattr(episode, "intended_order", None) if episode.intended_order_id else None
    if order is None and deal is not None and deal.order_id:
        order = deal.order

    boundary = max(
        int(current_message_floor(client) or 0),
        int(episode.opened_watermark_message_id or 0),
    )
    signal_types = set(
        IgConversationSignal.objects.filter(
            client_id=client.pk,
            message_id__gte=boundary,
        )
        .exclude(signal_type=IgConversationSignal.Type.MANAGER_TAKEOVER)
        .values_list("signal_type", flat=True)
    )
    product_snapshot = (
        episode.product_snapshot if isinstance(episode.product_snapshot, list) else []
    )
    has_product = bool(product_snapshot or getattr(client, "current_product_id", None))
    has_size = bool(
        getattr(client, "current_size", "")
        or any(
            isinstance(row, dict) and str(row.get("size") or "").strip()
            for row in product_snapshot
        )
    )
    if deal is not None and not has_product:
        has_product = deal.items.exists()
    if deal is not None and not has_size:
        has_size = deal.items.exclude(size="").exists()

    verified_payment = bool(current_payment_confirmation(client).get("confirmed"))
    payment_pending = bool(
        deal is not None and deal.status == IgDeal.Status.AWAITING_PAYMENT
    )
    order_created = bool(order is not None)
    target = observed_stage_target(
        IgClient.Stage.NEW,
        signal_types=signal_types,
        intent=str(getattr(client, "intent", "") or ""),
        has_product=has_product,
        has_size=has_size,
        payment_pending=payment_pending,
        verified_payment=verified_payment,
        order_created=order_created,
    )
    if verified_payment and order is not None and str(order.status or "") == "done":
        return IgClient.Stage.DONE
    return target


def _decimal(value, default="0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _money(value) -> Decimal:
    return _decimal(value, "0").quantize(Decimal("0.01"))


def _latest_review_for_deal(deal):
    if deal is None:
        return None
    prefetched = getattr(deal, "_prefetched_objects_cache", {}).get(
        "payment_confirmation_reviews"
    )
    if prefetched is not None:
        return sorted(prefetched, key=lambda row: row.pk, reverse=True)[0] if prefetched else None
    return deal.payment_confirmation_reviews.order_by("-id").first()


def payment_truth_snapshot(
    *,
    episode=None,
    deal=None,
    review=None,
    order=None,
    projection=None,
    decision=None,
    allow_deal_review_fallback: bool = True,
) -> dict:
    """Build source-qualified money truth without mutating commercial state."""
    from management.ig_bot_models import (
        IgDeal,
        IgPaymentProjection,
        IgPaymentReviewDecision,
    )

    deal = deal or getattr(episode, "deal", None)
    review = review or getattr(episode, "primary_payment_review", None)
    order = order or getattr(episode, "intended_order", None)
    if deal is None and review is not None:
        deal = getattr(review, "deal", None)
    if review is None and allow_deal_review_fallback:
        review = _latest_review_for_deal(deal)
    if order is None and review is not None:
        order = getattr(review, "order", None)
    if order is None and deal is not None:
        order = getattr(deal, "order", None)

    evidence = (
        review.evidence
        if review is not None and isinstance(review.evidence, dict)
        else {}
    )
    draft = (
        evidence.get("order_draft")
        if isinstance(evidence.get("order_draft"), dict)
        else {}
    )
    deal_total = _money(getattr(deal, "amount", None))
    quoted_total = _money(draft.get("quoted_total"))
    from management.services.ig_order_amounts import order_amounts

    linked_amounts = order_amounts(order)
    linked_order_subtotal = linked_amounts["subtotal"]
    linked_order_discount = linked_amounts["discount"]
    linked_order_total = linked_amounts["payable"]
    negotiated_total = deal_total if deal_total > 0 else quoted_total
    negotiated_total_source = (
        "deal_negotiated_total" if deal_total > 0
        else "conversation_quoted_total" if quoted_total > 0
        else "unknown"
    )
    if decision is None:
        decision = review.decisions.order_by("-id").first() if review is not None else None
    decision_total = _money(getattr(decision, "order_total_amount", None))
    if negotiated_total <= 0 and decision_total > 0:
        negotiated_total = decision_total
        negotiated_total_source = (
            getattr(decision, "order_total_source", "") or "manager_input"
        )
    if linked_order_total > 0:
        order_total = linked_order_total
        order_total_source = "linked_order_final_total"
    elif deal_total > 0:
        order_total = deal_total
        order_total_source = "deal_negotiated_total"
    elif quoted_total > 0:
        order_total = quoted_total
        order_total_source = "conversation_quoted_total"
    elif decision_total > 0:
        order_total = decision_total
        order_total_source = (
            getattr(decision, "order_total_source", "") or "manager_input"
        )
    else:
        order_total = Decimal("0.00")
        order_total_source = "unknown"

    requested = Decimal("0.00")
    requested_source = "unknown"
    if deal is not None:
        explicit_requested = _money(getattr(deal, "requested_payment_amount", None))
        if explicit_requested > 0:
            requested = explicit_requested
            requested_source = "deal_payment_request"
        elif deal.pay_type == deal.PayType.PREPAY_200:
            requested = _money(deal.payable_amount())
            requested_source = "legacy_fixed_200"
        elif deal.pay_type == deal.PayType.ONLINE_FULL and deal_total > 0:
            requested = deal_total
            requested_source = "deal_total_fallback"

    if projection is None and deal is not None:
        # Never trust a reverse OneToOne cache here: the same deal instance is
        # reused across provider webhook transitions and could otherwise keep
        # the previous amount/truth in the episode snapshot.
        projection = IgPaymentProjection.objects.filter(deal_id=deal.pk).first()
    provider_truth = getattr(projection, "truth", "") or ""
    provider_gross = _money(getattr(projection, "gross_amount", None))
    provider_refunded = _money(getattr(projection, "refunded_amount", None))
    provider_paid = Decimal("0.00")
    if projection is not None and provider_truth in {
        IgDeal.PaymentTruth.CONFIRMED,
        IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
    }:
        provider_paid = _money(projection.net_paid_amount)

    manager_truth = getattr(decision, "decision", "") or ""
    manager_paid = Decimal("0.00")
    if (
        decision is not None
        and manager_truth == IgPaymentReviewDecision.Decision.MANAGER_VERIFIED
    ):
        manager_paid = _money(decision.confirmed_amount)

    provider_terminal_conflict = bool(
        manager_paid > 0
        and provider_truth in {
            IgDeal.PaymentTruth.REFUNDED,
            IgDeal.PaymentTruth.REVERSED,
            IgDeal.PaymentTruth.FAILED,
            IgDeal.PaymentTruth.CANCELLED,
        }
    )
    provider_amount_mismatch = bool(
        provider_paid > 0 and requested > 0 and provider_paid != requested
    )
    provider_partial_refund = bool(
        provider_truth == IgDeal.PaymentTruth.PARTIALLY_REFUNDED
    )
    needs_reconciliation = bool(
        getattr(projection, "needs_reconciliation", False)
        or (provider_paid > 0 and manager_paid > 0 and provider_paid != manager_paid)
        or provider_amount_mismatch
        or provider_partial_refund
        or provider_terminal_conflict
    )
    effective_paid = (
        Decimal("0.00")
        if needs_reconciliation
        else provider_paid if provider_paid > 0 else manager_paid
    )
    remaining_basis = (
        provider_paid
        if needs_reconciliation
        and provider_paid > 0
        and manager_paid == 0
        else effective_paid
    )
    remaining = (
        max(order_total - remaining_basis, Decimal("0.00"))
        if order_total > 0
        else None
    )
    if provider_truth == IgDeal.PaymentTruth.PARTIALLY_REFUNDED:
        reconciliation_state = "provider_partially_refunded"
    elif provider_truth == IgDeal.PaymentTruth.REFUNDED:
        reconciliation_state = "provider_refunded"
    elif provider_truth == IgDeal.PaymentTruth.REVERSED:
        reconciliation_state = "provider_reversed"
    elif provider_terminal_conflict:
        reconciliation_state = "provider_terminal_conflict"
    elif getattr(projection, "needs_reconciliation", False):
        reconciliation_state = "provider_projection_reconciliation"
    elif needs_reconciliation:
        reconciliation_state = "amount_conflict"
    elif provider_paid > 0:
        reconciliation_state = "provider_verified"
    elif manager_paid > 0:
        reconciliation_state = "manager_verified_provider_unverified"
    elif manager_truth == IgPaymentReviewDecision.Decision.MANAGER_REJECTED:
        reconciliation_state = "manager_rejected"
    else:
        reconciliation_state = "unverified"

    currency = (
        getattr(decision, "currency", "")
        or getattr(deal, "currency", "")
        or draft.get("currency")
        or "UAH"
    )
    return {
        "episode_id": getattr(episode, "pk", None),
        "episode_sequence": getattr(episode, "sequence", None),
        "current": bool(episode is not None and episode.open_slot == 1),
        "deal_id": getattr(deal, "pk", None),
        "review_id": getattr(review, "pk", None),
        "decision_id": getattr(decision, "pk", None),
        "manager_decision_id": getattr(decision, "pk", None),
        "projection_id": getattr(projection, "pk", None),
        "order_id": getattr(order, "pk", None),
        "currency": str(currency)[:8],
        "negotiated_order_total": (
            f"{negotiated_total:.2f}" if negotiated_total > 0 else ""
        ),
        "negotiated_order_total_source": negotiated_total_source,
        "actual_order_total": (
            f"{linked_order_total:.2f}" if linked_order_total > 0 else ""
        ),
        "order_subtotal": (
            f"{linked_order_subtotal:.2f}" if linked_order_subtotal > 0 else ""
        ),
        "order_discount_amount": f"{linked_order_discount:.2f}",
        "order_total": f"{order_total:.2f}" if order_total > 0 else "",
        "order_total_source": order_total_source,
        "requested_payment_amount": f"{requested:.2f}",
        "requested_payment_source": requested_source,
        "requested_payment_evidence_ids": (
            list(getattr(deal, "requested_payment_evidence_ids", None) or [])
            if deal is not None
            else []
        ),
        "provider_truth": provider_truth,
        "provider_source": "monobank_projection" if projection is not None else "none",
        "provider_gross_amount": f"{provider_gross:.2f}",
        "provider_refunded_amount": f"{provider_refunded:.2f}",
        "provider_confirmed_amount": f"{provider_paid:.2f}",
        "manager_truth": manager_truth,
        "manager_scope": getattr(decision, "verification_scope", "") or "",
        "manager_source": getattr(decision, "verification_source", "") or "",
        "manager_amount_source": getattr(decision, "amount_source", "") or "",
        "manager_amount_evidence_ids": list(
            getattr(decision, "amount_evidence_message_ids", None) or []
        ),
        "manager_confirmed_amount": f"{manager_paid:.2f}",
        "confirmed_paid_amount": "" if needs_reconciliation else f"{effective_paid:.2f}",
        "remaining_amount": (
            ""
            if (needs_reconciliation and manager_paid > 0) or remaining is None
            else f"{remaining:.2f}"
        ),
        "needs_reconciliation": needs_reconciliation,
        "reconciliation_state": reconciliation_state,
        "review_status": getattr(review, "status", "") or "",
        "authoritative_for_fulfillment": bool(
            not needs_reconciliation
            and effective_paid > 0
            and (
                provider_paid > 0
                or (
                    manager_truth == IgPaymentReviewDecision.Decision.MANAGER_VERIFIED
                    and getattr(decision, "verification_scope", "")
                    in {
                        IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
                        IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
                    }
                )
            )
        ),
    }


def client_payment_truth_state(client) -> dict:
    """Return every scoped payment ledger and select the exact current cycle."""
    episodes = list(
        client.commercial_episodes.select_related(
            "deal",
            "deal__payment_projection",
            "primary_payment_review",
            "intended_order",
        )
        .prefetch_related(
            "deal__payment_confirmation_reviews",
            "primary_payment_review__decisions",
        )
        .order_by("sequence", "id")
    )
    rows = []
    chronology = []
    seen_deal_ids = set()
    seen_review_ids = set()
    for episode in episodes:
        row = payment_truth_snapshot(episode=episode)
        rows.append(row)
        review = getattr(episode, "primary_payment_review", None)
        deal = getattr(episode, "deal", None)
        occurred_at = (
            getattr(review, "created_at", None)
            or getattr(deal, "created_at", None)
            or getattr(episode, "opened_at", None)
        )
        chronology.append((row, occurred_at, episode.sequence, episode.pk))
        if row["deal_id"]:
            seen_deal_ids.add(row["deal_id"])
        if row["review_id"]:
            seen_review_ids.add(row["review_id"])

    deals = list(
        client.deals.exclude(pk__in=seen_deal_ids)
        .select_related("payment_projection", "order")
        .prefetch_related("payment_confirmation_reviews__decisions")
        .order_by("created_at", "id")
    )
    for deal in deals:
        row = payment_truth_snapshot(deal=deal)
        rows.append(row)
        chronology.append((row, getattr(deal, "created_at", None), 0, deal.pk))
        seen_deal_ids.add(deal.pk)
        if row["review_id"]:
            seen_review_ids.add(row["review_id"])

    reviews = list(
        client.payment_confirmation_reviews.exclude(pk__in=seen_review_ids)
        .select_related("deal", "deal__payment_projection", "order")
        .prefetch_related("decisions")
        .order_by("created_at", "id")
    )
    for review in reviews:
        row = payment_truth_snapshot(review=review)
        rows.append(row)
        chronology.append((row, getattr(review, "created_at", None), 0, review.pk))

    current_episode_id = getattr(client, "current_commercial_episode_id", None)
    current = next(
        (row for row in rows if row["episode_id"] == current_episode_id),
        None,
    )
    if current is None:
        current = next((row for row in rows if row["current"]), None)
    if current is None and chronology:
        current = max(
            chronology,
            key=lambda item: (
                item[1].timestamp() if item[1] is not None else 0,
                item[2],
                item[3],
            ),
        )[0]
    return {
        "current_payment_truth": current or {},
        "payment_truth": rows,
    }


def _item_snapshot(deal) -> list[dict]:
    if not deal:
        return []
    return [
        {
            "product_id": item.product_id,
            "color_variant_id": item.color_variant_id,
            "title": item.title,
            "size": item.size or "",
            "fit_option_code": item.fit_option_code or "",
            "fit_option_label": item.fit_option_label or "",
            "option_values": item.option_values or {},
            "option_labels": item.option_labels or {},
            "qty": item.qty,
            "unit_price": str(item.unit_price),
            "line_total": str(item.line_total),
            "price_source": item.price_source or "",
            "price_evidence_message_ids": item.price_evidence_message_ids or [],
        }
        for item in deal.items.all().order_by("id")
    ]


def append_episode_event(
    episode,
    *,
    dedupe_key: str,
    event_type: str,
    from_state: str = "",
    to_state: str = "",
    stage: str = "",
    source: str = "",
    evidence: dict | None = None,
):
    from management.ig_bot_models import IgCommercialEpisodeEvent

    event, _created = IgCommercialEpisodeEvent.objects.get_or_create(
        dedupe_key=str(dedupe_key)[:160],
        defaults={
            "episode": episode,
            "event_type": str(event_type)[:40],
            "from_state": str(from_state or "")[:32],
            "to_state": str(to_state or "")[:32],
            "stage": str(stage or "")[:32],
            "source": str(source or "")[:40],
            "evidence": evidence if isinstance(evidence, dict) else {},
        },
    )
    if event.episode_id != episode.pk:
        raise ValueError("Ключ події вже належить іншому комерційному епізоду")
    return event


_STAGE_EVENT_SOURCES = (
    ("checkout_invoice_created", "checkout_proposal"),
    ("checkout_proposal", "checkout_proposal"),
    ("payment", "payment_review"),
    ("payment_review_", "payment_review"),
    ("historical_payment_review_", "payment_review"),
    ("instagram_checkout_paid", "order_truth"),
    ("order_linked", "order_truth"),
    ("bot", "stage_fsm"),
    ("classifier:", "stage_fsm"),
    ("stage_fsm:", "stage_fsm"),
)


def append_client_stage_transition_event(client, *, stage_event_id: int,
                                         from_stage: str, to_stage: str,
                                         reason: str, episode_id: int | None = None):
    """Bind an authoritative client-stage write to its current episode."""
    try:
        stage_event_id = int(stage_event_id)
    except (TypeError, ValueError):
        return None
    source = next((value for prefix, value in _STAGE_EVENT_SOURCES
                   if str(reason or "").startswith(prefix)), "")
    if not source or not str(from_stage or "") or not str(to_stage or "") or from_stage == to_stage:
        return None
    episode_id = episode_id or getattr(client, "current_commercial_episode_id", None)
    if not episode_id:
        return None
    from management.ig_bot_models import IgCommercialEpisode
    episode = IgCommercialEpisode.objects.filter(pk=episode_id, client_id=client.pk).first()
    if episode is None:
        return None
    return append_episode_event(
        episode,
        dedupe_key=f"episode:{episode.pk}:client-stage:{stage_event_id}",
        event_type="stage_transition",
        from_state=str(from_stage)[:32], to_state=str(to_stage)[:32],
        stage=str(to_stage)[:32], source=source,
        evidence={"stage_event_id": stage_event_id, "episode_id": episode.pk},
    )


def _next_sequence(client_id: int) -> int:
    from management.ig_bot_models import IgCommercialEpisode

    current = IgCommercialEpisode.objects.filter(client_id=client_id).aggregate(
        value=Max("sequence")
    )["value"]
    return int(current or 0) + 1


def _new_episode(
    client,
    *,
    materialization_key: str,
    repeat_kind: str,
    deal=None,
    review=None,
    evidence_message_ids=None,
    confidence=Decimal("0"),
    analysis_model="",
    analysis_prompt_version="",
    opened_watermark_message_id=0,
    make_current=True,
):
    from management.ig_bot_models import IgCommercialEpisode

    episode = IgCommercialEpisode.objects.create(
        client=client,
        sequence=_next_sequence(client.pk),
        open_slot=1 if make_current else None,
        materialization_key=str(materialization_key)[:96],
        repeat_kind=repeat_kind,
        deal=deal,
        primary_payment_review=review,
        stage_snapshot={
            "stage": client.stage,
            "stage_label": client.get_stage_display(),
            "captured_at": timezone.now().isoformat(),
        },
        product_snapshot=_item_snapshot(deal),
        price_snapshot={
            "negotiated_total": str(getattr(deal, "amount", "") or ""),
            "currency": getattr(deal, "currency", "") or "UAH",
        },
        payment_snapshot={
            "deal_payment_truth": getattr(deal, "payment_truth", "") or "",
            "deal_paid_amount": str(getattr(deal, "paid_amount", "") or "0"),
            "requested_payment_amount": str(
                getattr(deal, "requested_payment_amount", "") or ""
            ),
            "requested_payment_evidence_ids": (
                getattr(deal, "requested_payment_evidence_ids", None) or []
            ),
            "review_id": getattr(review, "pk", None),
            "review_status": getattr(review, "status", "") or "",
        },
        fulfillment_snapshot={
            "delivery_status": getattr(deal, "delivery_status", "") or "",
            "delivery_source": getattr(deal, "delivery_source", "") or "",
        },
        repeat_evidence_message_ids=sorted(
            {int(value) for value in (evidence_message_ids or []) if str(value).isdigit()}
        ),
        repeat_confidence=_decimal(confidence).quantize(Decimal("0.0001")),
        analysis_model=str(analysis_model or "")[:80],
        analysis_prompt_version=str(analysis_prompt_version or "")[:80],
        opened_watermark_message_id=max(0, int(opened_watermark_message_id or 0)),
    )
    if make_current:
        client.current_commercial_episode = episode
        client.save(update_fields=["current_commercial_episode", "updated_at"])
    append_episode_event(
        episode,
        dedupe_key=f"episode:{episode.pk}:opened",
        event_type="opened",
        to_state=episode.state,
        stage=client.stage,
        source="conversation_analysis" if evidence_message_ids else "commercial_flow",
        evidence={
            "repeat_kind": repeat_kind,
            "message_ids": episode.repeat_evidence_message_ids,
        },
    )
    return episode


def ensure_open_episode_for_locked_client(client, *, materialization_prefix: str):
    """Return the client's open episode while the caller owns its row lock."""
    from management.ig_bot_models import IgCommercialEpisode

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("ensure_open_episode_for_locked_client requires transaction.atomic")
    current = (
        IgCommercialEpisode.objects.select_for_update()
        .filter(client_id=client.pk, open_slot=1)
        .first()
    )
    if current:
        if client.current_commercial_episode_id != current.pk:
            client.current_commercial_episode = current
            client.save(update_fields=["current_commercial_episode", "updated_at"])
        return current
    sequence = _next_sequence(client.pk)
    return _new_episode(
        client,
        materialization_key=f"{materialization_prefix}:{client.pk}:{sequence}",
        repeat_kind=IgCommercialEpisode.RepeatKind.FIRST_PURCHASE,
    )


def ensure_episode_for_deal(deal):
    from management.ig_bot_models import IgClient, IgCommercialEpisode

    with commercial_episode_client_lock(deal.client_id):
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=deal.client_id)
            existing = IgCommercialEpisode.objects.select_for_update().filter(deal=deal).first()
            if existing:
                return existing
            current = IgCommercialEpisode.objects.select_for_update().filter(
                client=client,
                open_slot=1,
            ).first()
            if current and not current.deal_id and not current.intended_order_id:
                current.deal = deal
                current.product_snapshot = _item_snapshot(deal)
                current.price_snapshot = {
                    "negotiated_total": str(deal.amount or ""),
                    "currency": deal.currency or "UAH",
                }
                current.save(update_fields=["deal", "product_snapshot", "price_snapshot", "updated_at"])
                return current
            if current:
                _mark_superseded_funnel_dropoff(
                    current,
                    source="deal_materialization",
                    reason_code="new_deal_episode",
                )
                current.open_slot = None
                current.save(update_fields=["open_slot", "updated_at"])
            return _new_episode(
                client,
                materialization_key=f"ig-deal:{deal.pk}",
                repeat_kind=(
                    IgCommercialEpisode.RepeatKind.REORDER
                    if IgCommercialEpisode.objects.filter(client=client, intended_order__isnull=False).exists()
                    else IgCommercialEpisode.RepeatKind.FIRST_PURCHASE
                ),
                deal=deal,
            )


def ensure_episode_for_review(review, *, isolate_from_current: bool = False):
    from management.ig_bot_models import IgClient, IgCommercialEpisode, IgOrderAttribution

    if review_has_false_historical_purchase_correction(review):
        raise ValueError(
            "Виправлена хибна історична покупка не може створювати комерційний епізод."
        )
    with commercial_episode_client_lock(review.client_id):
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=review.client_id)
            current = IgCommercialEpisode.objects.select_for_update().filter(
                client=client,
                open_slot=1,
            ).first()
            episode_deal = review.deal if review.deal_id else None
            existing = IgCommercialEpisode.objects.select_for_update().filter(
                primary_payment_review=review
            ).first()
            if existing:
                if not isolate_from_current or not current or existing.pk != current.pk:
                    return existing
                existing.primary_payment_review = None
                existing.payment_snapshot = payment_truth_snapshot(
                    episode=existing,
                    deal=existing.deal if existing.deal_id else None,
                    allow_deal_review_fallback=False,
                )
                existing.payment_snapshot["captured_at"] = timezone.now().isoformat()
                existing.save(
                    update_fields=["primary_payment_review", "payment_snapshot", "updated_at"]
                )
                append_episode_event(
                    existing,
                    dedupe_key=f"episode:{existing.pk}:review:{review.pk}:historical-detached",
                    event_type="review_detached",
                    source="historical_resolution",
                    evidence={"review_id": review.pk},
                )
            if review.order_id:
                existing = IgCommercialEpisode.objects.select_for_update().filter(
                    intended_order_id=review.order_id,
                    client_id=review.client_id,
                ).first()
                if existing:
                    append_episode_event(
                        existing,
                        dedupe_key=f"episode:{existing.pk}:review:{review.pk}:attached",
                        event_type="review_attached",
                        source="payment_review",
                        evidence={"review_id": review.pk, "watermark": review.watermark_message_id},
                    )
                    return existing
            if review.deal_id:
                existing = IgCommercialEpisode.objects.select_for_update().filter(
                    deal_id=review.deal_id
                ).first()
                if existing:
                    if not isolate_from_current or not current or existing.pk != current.pk:
                        if not existing.primary_payment_review_id:
                            existing.primary_payment_review = review
                            existing.save(update_fields=["primary_payment_review", "updated_at"])
                        return existing
                    episode_deal = None
            current_is_compatible = bool(
                not isolate_from_current
                and current
                and not current.intended_order_id
                and current.primary_payment_review_id in {None, review.pk}
                and (
                    current.deal_id in {None, review.deal_id}
                    if review.deal_id
                    else current.deal_id is None
                )
                and (
                    not int(current.opened_watermark_message_id or 0)
                    or not int(review.watermark_message_id or 0)
                    or int(review.watermark_message_id)
                    >= int(current.opened_watermark_message_id)
                )
            )
            if current_is_compatible:
                changed = []
                if not current.primary_payment_review_id:
                    current.primary_payment_review = review
                    changed.append("primary_payment_review")
                if review.deal_id and not current.deal_id:
                    current.deal_id = review.deal_id
                    changed.append("deal")
                if changed:
                    changed.append("updated_at")
                    current.save(update_fields=changed)
                return current
            review_is_historical = bool(
                isolate_from_current
                or (
                    current
                    and int(current.opened_watermark_message_id or 0) > 0
                    and int(review.watermark_message_id or 0) > 0
                    and int(review.watermark_message_id)
                    < int(current.opened_watermark_message_id)
                )
            )
            if current and not review_is_historical:
                _mark_superseded_funnel_dropoff(
                    current,
                    source="payment_review",
                    reason_code="new_review_episode",
                )
                current.open_slot = None
                current.save(update_fields=["open_slot", "updated_at"])
            episode = _new_episode(
                client,
                materialization_key=f"ig-review:{review.pk}",
                repeat_kind=(
                    IgCommercialEpisode.RepeatKind.REORDER
                    if IgOrderAttribution.objects.filter(client=client).exists()
                    else IgCommercialEpisode.RepeatKind.FIRST_PURCHASE
                ),
                deal=episode_deal,
                review=review,
                opened_watermark_message_id=review.watermark_message_id,
                make_current=not review_is_historical,
            )
            return episode


def sync_episode_payment(*, review=None, deal=None, isolate_from_current: bool = False):
    """Refresh one episode from exact provider and manager payment ledgers."""
    if review is None and deal is None:
        return None
    if review is not None:
        episode = ensure_episode_for_review(
            review,
            isolate_from_current=isolate_from_current,
        )
        deal = deal or getattr(review, "deal", None)
    else:
        episode = ensure_episode_for_deal(deal)
        review = getattr(episode, "primary_payment_review", None)

    snapshot = payment_truth_snapshot(
        episode=episode,
        deal=deal,
        review=review,
        order=getattr(episode, "intended_order", None),
    )
    snapshot["captured_at"] = timezone.now().isoformat()
    with transaction.atomic():
        locked = episode.__class__.objects.select_for_update().get(pk=episode.pk)
        locked.payment_snapshot = snapshot
        locked.save(update_fields=["payment_snapshot", "updated_at"])
        append_episode_event(
            locked,
            dedupe_key=(
                f"episode:{locked.pk}:payment:d{snapshot['decision_id'] or 0}:"
                f"p{snapshot['projection_id'] or 0}:"
                f"{snapshot['provider_truth']}:{snapshot['provider_confirmed_amount']}:"
                f"{snapshot['manager_confirmed_amount']}:{snapshot['reconciliation_state']}"
            ),
            event_type="payment_updated",
            source=(
                "payment_reconciliation"
                if snapshot["needs_reconciliation"]
                else "provider_projection"
                if snapshot["provider_confirmed_amount"] != "0.00"
                else "manager_decision"
            ),
            evidence={
                "decision_id": snapshot["decision_id"],
                "projection_id": snapshot["projection_id"],
                "needs_reconciliation": snapshot["needs_reconciliation"],
            },
        )
    return locked


def ensure_episode_for_attribution(attribution):
    """Create the durable episode for attribution-only/manual Instagram orders."""
    from management.ig_bot_models import IgClient, IgCommercialEpisode

    with commercial_episode_client_lock(attribution.client_id):
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=attribution.client_id)
            existing = IgCommercialEpisode.objects.select_for_update().filter(
                order_attribution=attribution
            ).first()
            if existing:
                return existing
            existing = IgCommercialEpisode.objects.select_for_update().filter(
                intended_order_id=attribution.order_id,
            ).first()
            if existing:
                if existing.client_id != client.pk:
                    raise ValueError("Замовлення вже належить іншому Instagram-клієнту")
                if not existing.order_attribution_id:
                    existing.order_attribution = attribution
                    existing.save(update_fields=["order_attribution", "updated_at"])
                return existing
            current = IgCommercialEpisode.objects.select_for_update().filter(
                client=client,
                open_slot=1,
            ).first()
            if current:
                _mark_superseded_funnel_dropoff(
                    current,
                    source="order_attribution",
                    reason_code="new_attribution_episode",
                )
                current.open_slot = None
                current.save(update_fields=["open_slot", "updated_at"])
            episode = _new_episode(
                client,
                materialization_key=f"ig-attribution:{attribution.pk}",
                repeat_kind=(
                    IgCommercialEpisode.RepeatKind.REORDER
                    if IgCommercialEpisode.objects.filter(
                        client=client,
                        intended_order__isnull=False,
                    ).exists()
                    else IgCommercialEpisode.RepeatKind.FIRST_PURCHASE
                ),
            )
            episode.order_attribution = attribution
            episode.save(update_fields=["order_attribution", "updated_at"])
        return bind_episode_order(
            episode,
            attribution.order,
            attribution=attribution,
            creation_mode=attribution.creation_mode,
            payment_source=attribution.payment_source,
        )


def reconcile_missing_commercial_episode_sources(*, passes: int = 3) -> dict:
    """Attach only source rows that have no runtime episode yet.

    The daemon used to rerun migration ``0106`` on every start.  That migration
    is a historical reconstruction: it is allowed to rewrite existing episode
    state from legacy rows, which makes it unsuitable as an online reconciler.
    In particular it could overwrite an append-only owner correction and then
    materialize the retired review as a new ``lost`` episode.

    This online path is intentionally monotonic.  It invokes the normal locked
    materializers only for genuinely missing, still-actionable source rows and
    never rewrites an existing episode projection.
    """
    from management.ig_bot_models import (
        IgCommercialEpisode,
        IgDeal,
        IgOrderAttribution,
        IgPaymentConfirmationReview,
    )

    bounded_passes = max(1, min(int(passes or 1), 10))
    processed = {"deals": 0, "reviews": 0, "attributions": 0}

    def missing_sources():
        episode_rows = IgCommercialEpisode.objects.all()
        episode_attribution_ids = episode_rows.exclude(
            order_attribution_id__isnull=True
        ).values("order_attribution_id")
        attribution_review_ids = IgOrderAttribution.objects.filter(
            pk__in=episode_attribution_ids,
            payment_review_id__isnull=False,
        ).values("payment_review_id")
        materialized_reviews = (
            Q(
                pk__in=episode_rows.exclude(
                    primary_payment_review_id__isnull=True
                ).values("primary_payment_review_id")
            )
            | Q(
                deal_id__isnull=False,
                deal_id__in=episode_rows.exclude(deal_id__isnull=True).values(
                    "deal_id"
                ),
            )
            | Q(
                order_id__isnull=False,
                order_id__in=episode_rows.exclude(
                    intended_order_id__isnull=True
                ).values("intended_order_id"),
            )
            | Q(pk__in=attribution_review_ids)
        )
        deals = list(
            IgDeal.objects.filter(commercial_episode__isnull=True)
            .exclude(status=IgDeal.Status.CANCELLED)
            .order_by("id")[:500]
        )
        reviews = list(
            IgPaymentConfirmationReview.objects.filter(
                status__in=(
                    IgPaymentConfirmationReview.Status.PENDING,
                    IgPaymentConfirmationReview.Status.CONFIRMED,
                ),
            )
            .exclude(materialized_reviews)
            .order_by("id")[:500]
        )
        attributions = list(
            IgOrderAttribution.objects.filter(commercial_episode__isnull=True)
            .order_by("id")[:500]
        )
        return deals, reviews, attributions

    for _unused in range(bounded_passes):
        deals, reviews, attributions = missing_sources()
        if not any((deals, reviews, attributions)):
            break
        for deal in deals:
            ensure_episode_for_deal(deal)
            processed["deals"] += 1
        for review in reviews:
            # The status filter rejects every superseded review; keep the
            # explicit predicate as a fail-closed guard if statuses evolve.
            if review_has_false_historical_purchase_correction(review):
                continue
            ensure_episode_for_review(review)
            processed["reviews"] += 1
        for attribution in attributions:
            ensure_episode_for_attribution(attribution)
            processed["attributions"] += 1

    deals, reviews, attributions = missing_sources()
    return {
        **processed,
        "remaining": {
            "deals": len(deals),
            "reviews": len(reviews),
            "attributions": len(attributions),
        },
    }


def bind_episode_order(
    episode,
    order,
    *,
    attribution=None,
    creation_mode="",
    payment_source="",
    override_snapshot=None,
):
    from management.ig_bot_models import IgCommercialEpisode
    from orders.models import Order

    with commercial_episode_client_lock(episode.client_id):
        with transaction.atomic():
            locked = IgCommercialEpisode.objects.select_for_update().get(pk=episode.pk)
            order = Order.objects.select_for_update().get(pk=order.pk)
            if attribution is not None and attribution.client_id != locked.client_id:
                raise ValueError("Атрибуція належить іншому Instagram-клієнту")
            from management.ig_bot_models import IgOrderAttribution
            order_attribution = IgOrderAttribution.objects.filter(order=order).first()
            if order_attribution is not None:
                if order_attribution.client_id != locked.client_id:
                    raise ValueError("Замовлення належить іншому Instagram-клієнту")
            deal = locked.deal if locked.deal_id else None
            if deal and deal.client_id != locked.client_id:
                raise ValueError("Угода належить іншому Instagram-клієнту")
            review = locked.primary_payment_review if locked.primary_payment_review_id else None
            if review and review.client_id != locked.client_id:
                raise ValueError("Перевірка оплати належить іншому Instagram-клієнту")
            owner = IgCommercialEpisode.objects.select_for_update().filter(
                intended_order=order
            ).exclude(pk=locked.pk).first()
            if owner:
                raise ValueError("Замовлення вже належить іншому комерційному епізоду")
            if locked.intended_order_id and locked.intended_order_id != order.pk:
                raise ValueError("Комерційний епізод уже має інше замовлення")
            if attribution is None:
                attribution = getattr(order, "instagram_attribution", None)
            if attribution:
                other = IgCommercialEpisode.objects.filter(
                    order_attribution=attribution
                ).exclude(pk=locked.pk).first()
                if other:
                    raise ValueError("Атрибуція вже належить іншому комерційному епізоду")
                if (
                    locked.order_attribution_id
                    and locked.order_attribution_id != attribution.pk
                ):
                    raise ValueError("Комерційний епізод уже має іншу атрибуцію")
                locked.order_attribution = attribution
            if locked.intended_order_id == order.pk:
                if attribution and locked.order_attribution_id != attribution.pk:
                    locked.order_attribution = attribution
                if attribution:
                    locked.save(update_fields=["order_attribution", "updated_at"])
                _record_order_funnel_facts(
                    locked,
                    order,
                    source=creation_mode or "order_resolution",
                )
                return locked
            previous_state = locked.state
            locked.intended_order = order
            target_state, outcome, closed_at, fulfillment_snapshot = (
                _fulfillment_projection(order)
            )
            locked.state = target_state
            locked.outcome = outcome
            locked.closed_at = closed_at
            locked.fulfillment_snapshot = fulfillment_snapshot
            if closed_at:
                locked.open_slot = None
            locked.save(update_fields=[
                "intended_order",
                "order_attribution",
                "state",
                "outcome",
                "closed_at",
                "open_slot",
                "fulfillment_snapshot",
                "updated_at",
            ])
            if closed_at:
                from management.ig_bot_models import IgClient

                IgClient.objects.filter(
                    pk=locked.client_id,
                    current_commercial_episode_id=locked.pk,
                ).update(current_commercial_episode_id=None, updated_at=timezone.now())
            append_episode_event(
                locked,
                dedupe_key=f"episode:{locked.pk}:order:{order.pk}:bound",
                event_type="order_bound",
                from_state=previous_state,
                to_state=locked.state,
                source=creation_mode or "order_resolution",
                evidence={
                    "order_id": order.pk,
                    "order_number": order.order_number,
                    "creation_mode": creation_mode,
                    "payment_source": payment_source,
                    "override": override_snapshot if isinstance(override_snapshot, dict) else {},
                },
            )
            _record_order_funnel_facts(
                locked,
                order,
                source=creation_mode or "order_resolution",
            )
            return locked


def _fulfillment_projection(order):
    from management.ig_bot_models import IgCommercialEpisode

    snapshot = {
        "order_status": order.status or "",
        "payment_status": order.payment_status or "",
        "tracking_number": order.tracking_number or "",
        "shipment_status": order.shipment_status or "",
        "shipment_updated_at": (
            order.shipment_status_updated.isoformat()
            if order.shipment_status_updated
            else ""
        ),
        "tracking_status_code": order.tracking_status_code,
        "tracking_provider_event_at": (
            order.tracking_provider_event_at.isoformat()
            if order.tracking_provider_event_at
            else ""
        ),
        "tracking_terminal_at": (
            order.tracking_terminal_at.isoformat()
            if order.tracking_terminal_at
            else ""
        ),
    }
    delivered_at = nova_poshta_delivery_confirmed_at(order)
    if delivered_at is not None:
        return (
            IgCommercialEpisode.State.FULFILLED,
            "fulfilled",
            delivered_at,
            snapshot,
        )
    if order.status == "cancelled":
        return (
            IgCommercialEpisode.State.CANCELLED,
            "cancelled",
            timezone.now(),
            snapshot,
        )
    return (
        IgCommercialEpisode.State.ORDER_CREATED,
        "order_linked",
        None,
        snapshot,
    )


def _mark_superseded_funnel_dropoff(episode, *, source: str, reason_code: str):
    """Record replacement of an open commercial cycle before closing its slot."""
    from management.ig_bot_models import IgFunnelStepEvent
    from management.services.ig_funnel_analytics import (
        record_episode_step_event_in_transaction,
    )

    record_episode_step_event_in_transaction(
        episode,
        event_type=IgFunnelStepEvent.Type.DROP_OFF,
        event_key=f"ig-drop-off:{episode.pk}:superseded:{reason_code}",
        occurred_at=timezone.now(),
        stage=(episode.stage_snapshot or {}).get("stage", ""),
        actor=source,
        evidence={
            "kind": "superseded",
            "reason_code": reason_code,
            "is_recoverable": False,
        },
        is_backfilled=False,
    )


def _record_order_funnel_facts(episode, order, *, source: str):
    """Record order/TTN/delivery facts from canonical locked Order truth."""
    from management.ig_bot_models import IgFunnelStepEvent
    from management.services.ig_funnel_analytics import (
        delivery_funnel_event_key,
        record_episode_step_event_in_transaction,
    )

    # ``orders.Order`` uses ``created``/``updated``.  Keep the fallback for
    # legacy order-like objects used by import/reconciliation callers.
    order_created_at = getattr(order, "created", None) or getattr(order, "created_at", None)
    order_updated_at = getattr(order, "updated", None) or getattr(order, "updated_at", None)
    record_episode_step_event_in_transaction(
        episode,
        event_type=IgFunnelStepEvent.Type.ORDER_CREATED,
        event_key=f"ig-order-created:{order.pk}",
        occurred_at=order_created_at,
        stage="order_created",
        actor=source or "order_truth",
        evidence={
            "order_id": order.pk,
            "order_number": order.order_number,
            "creation_mode": source or "order_truth",
        },
    )
    tracking_number = str(order.tracking_number or "").strip()
    if tracking_number:
        record_episode_step_event_in_transaction(
            episode,
            event_type=IgFunnelStepEvent.Type.TTN_CREATED,
            event_key=f"ig-ttn-created:{order.pk}:{tracking_number}",
            occurred_at=order.shipment_status_updated or order_updated_at,
            stage="order_created",
            actor=source or "order_truth",
            evidence={
                "order_id": order.pk,
                "tracking_number": tracking_number,
            },
        )
    delivered_at = nova_poshta_delivery_confirmed_at(order)
    if delivered_at is not None:
        record_episode_step_event_in_transaction(
            episode,
            event_type=IgFunnelStepEvent.Type.DELIVERED,
            event_key=delivery_funnel_event_key(episode, order),
            occurred_at=delivered_at,
            stage="done",
            actor=source or "order_truth",
            evidence={
                "order_id": order.pk,
                "tracking_number": tracking_number,
                "shipment_status": order.shipment_status or "",
                "tracking_status_code": order.tracking_status_code,
                "tracking_terminal_at": order.tracking_terminal_at.isoformat(),
            },
        )


def sync_episode_fulfillment(order_or_id, *, source="order_truth"):
    """Refresh exactly one episode from canonical Order/Nova Poshta truth."""
    from management.ig_bot_models import IgClient, IgCommercialEpisode
    from orders.models import Order

    order_id = int(getattr(order_or_id, "pk", None) or order_or_id)
    episode = IgCommercialEpisode.objects.filter(intended_order_id=order_id).first()
    if not episode:
        return None
    with commercial_episode_client_lock(episode.client_id):
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=episode.client_id)
            locked = IgCommercialEpisode.objects.select_for_update().get(pk=episode.pk)
            current_owner = (
                IgCommercialEpisode.objects.select_for_update()
                .filter(client_id=locked.client_id, open_slot=1)
                .exclude(pk=locked.pk)
                .first()
            )
            order = Order.objects.select_for_update().get(pk=order_id)
            _record_order_funnel_facts(locked, order, source=source)
            previous_state = locked.state
            previous_snapshot = locked.fulfillment_snapshot or {}
            target_state, outcome, closed_at, snapshot = _fulfillment_projection(order)
            target_open_slot = None if closed_at or current_owner else 1
            target_current_id = (
                current_owner.pk
                if current_owner
                else locked.pk if target_open_slot == 1 else None
            )
            changed = bool(
                previous_state != target_state
                or locked.outcome != outcome
                or previous_snapshot != snapshot
                or bool(locked.closed_at) != bool(closed_at)
                or locked.open_slot != target_open_slot
                or client.current_commercial_episode_id != target_current_id
            )
            if not changed:
                return locked
            locked.state = target_state
            locked.outcome = outcome
            locked.closed_at = closed_at
            locked.fulfillment_snapshot = snapshot
            locked.open_slot = target_open_slot
            locked.save(update_fields=[
                "state",
                "outcome",
                "closed_at",
                "open_slot",
                "fulfillment_snapshot",
                "updated_at",
            ])
            if client.current_commercial_episode_id != target_current_id:
                client.current_commercial_episode_id = target_current_id
                client.save(update_fields=["current_commercial_episode", "updated_at"])
            event_fingerprint = hashlib.sha256(
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
            append_episode_event(
                locked,
                dedupe_key=(
                    f"episode:{locked.pk}:order:{order.pk}:truth:{event_fingerprint}"
                ),
                event_type="fulfillment_updated",
                from_state=previous_state,
                to_state=target_state,
                source=source,
                evidence={"order_id": order.pk, **snapshot},
            )
            return locked


def start_repeat_episode(
    client,
    *,
    repeat_kind: str,
    evidence_message_ids,
    confidence,
    analysis_model: str,
    analysis_prompt_version: str,
    _lock_held: bool = False,
    preserve_client_stage: bool = False,
):
    from management.ig_bot_models import IgClient, IgCommercialEpisode

    valid_kinds = {
        value for value, _label in IgCommercialEpisode.RepeatKind.choices
    } - {IgCommercialEpisode.RepeatKind.FIRST_PURCHASE}
    if repeat_kind not in valid_kinds:
        raise ValueError("Невідомий тип повторного замовлення")
    evidence_ids = sorted(
        {int(value) for value in (evidence_message_ids or []) if str(value).isdigit()}
    )
    if not evidence_ids:
        raise ValueError("Повторне замовлення потребує доказ повідомлення клієнта")
    key = repeat_materialization_key(client.pk, evidence_ids)
    lock_context = nullcontext() if _lock_held else commercial_episode_client_lock(client.pk)
    with lock_context:
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=client.pk)
            episodes = list(
                IgCommercialEpisode.objects.select_for_update()
                .filter(client=client)
                .order_by("-sequence", "-id")
            )
            evidence_set = set(evidence_ids)

            def is_same_repeat_analysis(episode):
                known = {
                    int(value)
                    for value in (episode.repeat_evidence_message_ids or [])
                    if str(value).isdigit()
                }
                if evidence_set.issubset(known):
                    return True
                if not known.issubset(evidence_set):
                    return False
                # Once commercial state has been attached, a newly observed
                # repeat-order message starts a new cycle. Otherwise an older
                # deal, price or invoice could leak into the next purchase.
                return not any((
                    episode.deal_id,
                    episode.primary_payment_review_id,
                    episode.order_attribution_id,
                    episode.intended_order_id,
                ))

            overlapping = next(
                (
                    episode for episode in episodes
                    if episode.repeat_kind == repeat_kind
                    and is_same_repeat_analysis(episode)
                ),
                None,
            )
            if overlapping:
                known_ids = {
                    int(value)
                    for value in (overlapping.repeat_evidence_message_ids or [])
                    if str(value).isdigit()
                }
                new_ids = sorted(evidence_set.difference(known_ids))
                if new_ids and overlapping.open_slot == 1:
                    overlapping.repeat_evidence_message_ids = sorted(
                        known_ids.union(evidence_set)
                    )
                    overlapping.opened_watermark_message_id = max(
                        int(overlapping.opened_watermark_message_id or 0),
                        max(new_ids),
                    )
                    overlapping.save(update_fields=[
                        "repeat_evidence_message_ids",
                        "opened_watermark_message_id",
                        "updated_at",
                    ])
                    append_episode_event(
                        overlapping,
                        dedupe_key=f"episode:{overlapping.pk}:repeat-evidence:{hashlib.sha256(json.dumps(new_ids).encode()).hexdigest()[:20]}",
                        event_type="repeat_evidence_extended",
                        source="conversation_analysis",
                        evidence={"message_ids": new_ids},
                    )
                current = next((episode for episode in episodes if episode.open_slot == 1), None)
                expected_current_id = current.pk if current else None
                if client.current_commercial_episode_id != expected_current_id:
                    client.current_commercial_episode_id = expected_current_id
                    client.save(update_fields=["current_commercial_episode", "updated_at"])
                return overlapping
            existing = IgCommercialEpisode.objects.select_for_update().filter(
                materialization_key=key
            ).first()
            if existing:
                current = IgCommercialEpisode.objects.select_for_update().filter(
                    client=client,
                    open_slot=1,
                ).first()
                expected_current_id = current.pk if current else None
                if client.current_commercial_episode_id != expected_current_id:
                    client.current_commercial_episode_id = expected_current_id
                    client.save(update_fields=["current_commercial_episode", "updated_at"])
                return existing
            current = IgCommercialEpisode.objects.select_for_update().filter(
                client=client,
                open_slot=1,
            ).first()
            if current:
                _mark_superseded_funnel_dropoff(
                    current,
                    source="conversation_analysis",
                    reason_code="repeat_episode",
                )
                current.open_slot = None
                current.save(update_fields=["open_slot", "updated_at"])
            episode = _new_episode(
                client,
                materialization_key=key,
                repeat_kind=repeat_kind,
                evidence_message_ids=evidence_ids,
                confidence=confidence,
                analysis_model=analysis_model,
                analysis_prompt_version=analysis_prompt_version,
                opened_watermark_message_id=max(evidence_ids),
            )
            from management.services.ig_commerce_projection import (
                start_new_session_for_episode,
            )

            start_new_session_for_episode(client, episode)
            if (
                not preserve_client_stage
                and client.stage not in {IgClient.Stage.NEW, IgClient.Stage.QUALIFYING}
            ):
                previous_stage = client.stage
                client.stage = IgClient.Stage.QUALIFYING
                client.stage_updated_at = timezone.now()
                client.save(update_fields=["stage", "stage_updated_at", "updated_at"])
                # Э3.2: журнал эпизода — не замена таймлайну клиента. Раньше
                # событие писалось только в журнал эпизода, поэтому в карточке
                # клиента стадия менялась без evidence.
                from management.models import IgClientStageEvent

                IgClientStageEvent.objects.create(
                    client=client,
                    from_stage=previous_stage or "",
                    to_stage=IgClient.Stage.QUALIFYING,
                    reason=f"episode_started:{episode.pk}",
                )
                append_episode_event(
                    episode,
                    dedupe_key=f"episode:{episode.pk}:stage:{client.stage}:start",
                    event_type="stage_transition",
                    from_state=previous_stage,
                    to_state=client.stage,
                    stage=client.stage,
                    source="repeat_intent",
                    evidence={"message_ids": evidence_ids},
                )
    return episode


def repeat_materialization_key(client_id: int, evidence_message_ids) -> str:
    """Stable repeat identity derived only from durable customer evidence."""
    evidence_ids = sorted({
        int(value)
        for value in (evidence_message_ids or [])
        if str(value).isdigit()
    })
    if not evidence_ids:
        raise ValueError("Повторне замовлення потребує доказ повідомлення клієнта")
    canonical = json.dumps(
        {
            "version": "ig-repeat-evidence-v1",
            "client_id": int(client_id),
            "evidence_message_ids": evidence_ids,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"ig-repeat:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


# Compatibility alias used by the bounded API/test contract. Keep the stored
# field name explicit so older callers cannot accidentally mutate history.
def _episode_evidence_ids(episode):
    return list(episode.repeat_evidence_message_ids or [])


def _client_order_queryset(client):
    from orders.models import Order

    return Order.objects.filter(
        Q(instagram_attribution__client=client)
        | Q(ig_deals__client=client)
        | Q(instagram_commercial_episode__client=client)
    ).distinct()


def resolve_client_order(client, reference="") -> ResolvedClientOrder:
    value = str(reference or "").strip()
    queryset = _client_order_queryset(client).select_related(
        "instagram_attribution",
        "instagram_commercial_episode",
    )
    match_kind = "latest_unique"
    if value:
        by_number = queryset.filter(order_number=value)
        by_ttn = queryset.filter(tracking_number=value)
        matches = list((by_number | by_ttn).distinct()[:2])
        if not matches:
            raise OrderResolutionError("order_not_found", "Замовлення клієнта не знайдено")
        if len(matches) > 1:
            raise OrderResolutionError("ambiguous_order", "Знайдено кілька замовлень; уточніть номер")
        order = matches[0]
        match_kind = "order_number" if order.order_number == value else "ttn"
    else:
        matches = list(queryset.order_by("-created", "-id")[:2])
        if not matches:
            raise OrderResolutionError("order_not_found", "У клієнта немає прив'язаних замовлень")
        if len(matches) > 1:
            raise OrderResolutionError(
                "ambiguous_order",
                "У клієнта кілька замовлень; потрібен точний номер або ТТН",
            )
        order = matches[0]
    return ResolvedClientOrder(
        order=order,
        episode=getattr(order, "instagram_commercial_episode", None),
        attribution=getattr(order, "instagram_attribution", None),
        match_kind=match_kind,
    )


def episode_payload(episode) -> dict:
    order = episode.intended_order
    attribution = episode.order_attribution
    events = list(episode.events.order_by("created_at", "id")[:80])
    from management.services.ig_order_amounts import order_amounts

    amounts = order_amounts(order)
    return {
        "id": episode.pk,
        "sequence": episode.sequence,
        "state": episode.state,
        "state_label": episode.get_state_display(),
        "current": episode.open_slot == 1,
        "repeat_kind": episode.repeat_kind,
        "repeat_kind_label": episode.get_repeat_kind_display(),
        "repeat_confidence": str(episode.repeat_confidence),
        "repeat_evidence_message_ids": _episode_evidence_ids(episode),
        "evidence_message_ids": _episode_evidence_ids(episode),
        "opened_at": episode.opened_at.isoformat() if episode.opened_at else "",
        "closed_at": episode.closed_at.isoformat() if episode.closed_at else "",
        "stage": episode.stage_snapshot or {},
        "products": episode.product_snapshot or [],
        "price": episode.price_snapshot or {},
        "payment": episode.payment_snapshot or {},
        "fulfillment": episode.fulfillment_snapshot or {},
        "deal_id": episode.deal_id,
        "review_id": episode.primary_payment_review_id,
        "attribution_id": episode.order_attribution_id,
        "creation_mode": attribution.creation_mode if attribution else "",
        "payment_source": attribution.payment_source if attribution else "",
        "order": ({
            "id": order.pk,
            "number": order.order_number,
            "created_at": order.created.isoformat() if order.created else "",
            "amount": f"{amounts['payable']:.2f}",
            "subtotal": f"{amounts['subtotal']:.2f}",
            "discount_amount": f"{amounts['discount']:.2f}",
            "payment_status": order.payment_status or "",
            "payment_status_label": order.get_payment_status_display(),
            "status": order.status or "",
            "status_label": order.get_status_display(),
            "tracking_number": order.tracking_number or "",
            "shipment_status": order.shipment_status or "",
            "shipment_updated_at": (
                order.shipment_status_updated.isoformat()
                if order.shipment_status_updated
                else ""
            ),
        } if order else {}),
        "events": [
            {
                "type": event.event_type,
                "from": event.from_state,
                "to": event.to_state,
                "stage": event.stage,
                "source": event.source,
                "evidence": event.evidence or {},
                "created_at": event.created_at.isoformat() if event.created_at else "",
            }
            for event in events
        ],
    }


def client_episode_payload(client, *, limit=20) -> dict:
    episodes = list(
        client.commercial_episodes.select_related(
            "deal",
            "primary_payment_review",
            "order_attribution",
            "intended_order",
        ).prefetch_related("events").order_by("-sequence", "-id")[:limit]
    )
    items = [episode_payload(episode) for episode in episodes]
    current = next((item for item in items if item["current"]), None)
    physical_count = _client_order_queryset(client).count()
    return {
        "count": client.commercial_episodes.count(),
        "physical_order_count": physical_count,
        "current": current,
        "items": items,
        "has_more": client.commercial_episodes.count() > len(items),
    }
