"""Durable Instagram post-sale lifecycle events.

The order, payment, and Nova Poshta integrations own business truth.  This
module only projects committed truth into one idempotent Direct message.  It
never treats Telegram delivery or an AI response as evidence of payment,
shipment, or delivery.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from management.models import (
    IgCheckoutProposal,
    IgClient,
    IgDeal,
    IgLifecycleEvent,
    IgOrderAssignment,
    IgOrderAssignmentEvent,
    IgOrderAttribution,
    IgPaymentProjection,
    InstagramBotMessage,
)
from management.services.ig_delivery_receipts import (
    normalize_provider_message_id,
    normalize_provider_message_ids,
)
from management.services.ig_post_purchase_invitation import (
    INVITATION_BLOCK_REASONS,
    INVITATION_PAYLOAD_KEY,
    post_purchase_invitation_block_reason,
    post_purchase_invitation_snapshot,
    post_purchase_invitation_text,
)
from orders.fulfillment_truth import nova_poshta_order_fulfillment_confirmed
from orders.models import Order

logger = logging.getLogger("management.ig_lifecycle")

RESPONSE_WINDOW = timedelta(hours=23)
LEASE_DURATION = timedelta(minutes=5)
PERMISSION_DEFERRAL_DELAY = timedelta(minutes=15)
PERMISSION_DEFERRAL_DURATION = timedelta(hours=12)
NP_TRACKING_URL = "https://novaposhta.ua/tracking/?cargo_number="
MESSAGE_SNAPSHOT_KEY = "message_snapshot"
LIFECYCLE_MESSAGE_KEY_PREFIX = "ig-lifecycle:"
STALE_ASSIGNMENT_ERROR = "order assignment no longer belongs to lifecycle client"
PAYMENT_NOT_VERIFIED_ERROR = "payment_not_verified"
STANDARD_RESPONSE_WINDOW_CLOSED = "standard_response_window_closed"
ORDER_MISSING_ERROR = "order_missing"
PARCEL_ALREADY_RECEIVED_ERROR = "parcel_already_received"
TRACKING_NUMBER_CHANGED_ERROR = "tracking_number_changed"
CARRIER_DELIVERY_NOT_CONFIRMED_ERROR = "carrier delivery not confirmed"
CUSTOMER_SEND_NOT_ALLOWED_ERROR = "customer_send_not_allowed"
PROVIDER_MESSAGE_ID_MISSING_ERROR = "provider_message_id_missing"
PROVIDER_IO_STARTED_PREFIX = "provider_io_started:"
LEASE_EXPIRED_AFTER_PROVIDER_IO_ERROR = (
    "processing lease expired after provider I/O started; "
    "delivery outcome requires manager review"
)
PROVIDER_MARKER_BEFORE_LEASE_ERROR = (
    "provider I/O marker exists before a new lease"
)
LEASE_WITHOUT_EXPIRY_ERROR = (
    "processing lease has no expiry; delivery outcome requires manager review"
)
LEGACY_LEASE_MARKER_ERROR = (
    "legacy processing lease lacks the current claim marker; "
    "delivery outcome requires manager review"
)
LIFECYCLE_PROJECTION_STAGE = {
    IgLifecycleEvent.Kind.PAYMENT_VERIFIED: 1,
    IgLifecycleEvent.Kind.TTN_CREATED: 2,
    IgLifecycleEvent.Kind.PARCEL_ARRIVED: 3,
    IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED: 4,
}
VERIFIED_PAYMENT_TRUTHS = {
    IgDeal.PaymentTruth.CONFIRMED,
    IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
}
PRECLAIM_CANCELLABLE_STATES = {
    IgLifecycleEvent.State.PENDING,
    IgLifecycleEvent.State.WAITING_WINDOW,
}
PROVIDER_BOUNDARY_CLAIM_MARKER = "provider_boundary_v1"
RECOVERABLE_CANCELLATION_REASONS = frozenset({
    PAYMENT_NOT_VERIFIED_ERROR,
    STALE_ASSIGNMENT_ERROR,
    TRACKING_NUMBER_CHANGED_ERROR,
    CARRIER_DELIVERY_NOT_CONFIRMED_ERROR,
})
TRANSIENT_PERMISSION_REASONS = {
    "client_paused",
    "global_reply_paused",
    "manager_takeover",
    "permission_epoch_changed",
    "permission_transition_pending",
}
# Э0.3: единственный реестр ФИКСИРОВАННЫХ строк `last_error`, которые пишет этот
# модуль. Воронка терминальных причин (`ig_lifecycle_reasons`) обязана дать тип
# каждой из них, и тест сравнивает реестр с картой кодов — поэтому новую строку
# нельзя завести, не дав ей типа. Строки, склеенные из `kind`/`hint` провайдера,
# реестром сознательно НЕ покрываются: у них префиксные правила, а незнакомый
# `kind` попадает в явный бакет `unknown`, а не исчезает из знаменателя.
LAST_ERROR_REASONS = frozenset({
    PAYMENT_NOT_VERIFIED_ERROR,
    STALE_ASSIGNMENT_ERROR,
    STANDARD_RESPONSE_WINDOW_CLOSED,
    ORDER_MISSING_ERROR,
    PARCEL_ALREADY_RECEIVED_ERROR,
    TRACKING_NUMBER_CHANGED_ERROR,
    CARRIER_DELIVERY_NOT_CONFIRMED_ERROR,
    CUSTOMER_SEND_NOT_ALLOWED_ERROR,
    PROVIDER_MESSAGE_ID_MISSING_ERROR,
    LEASE_EXPIRED_AFTER_PROVIDER_IO_ERROR,
    PROVIDER_MARKER_BEFORE_LEASE_ERROR,
    LEASE_WITHOUT_EXPIRY_ERROR,
    LEGACY_LEASE_MARKER_ERROR,
    *TRANSIENT_PERMISSION_REASONS,
    *INVITATION_BLOCK_REASONS,
})


def _locale(value: str | None) -> str:
    code = str(value or "uk").lower().replace("_", "-").split("-", 1)[0]
    return code if code in {"uk", "ru", "en"} else "uk"


def _tracking_digest(value: str) -> str:
    return hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()[:24]


def _lifecycle_message_key(event_key: str) -> str:
    """Return a bounded unique key for the lifecycle conversation outbox row."""
    digest = hashlib.sha256(str(event_key or "").encode("utf-8")).hexdigest()
    return f"{LIFECYCLE_MESSAGE_KEY_PREFIX}{digest}"[:64]


def _lifecycle_message_queryset(event_key: str):
    return InstagramBotMessage.objects.filter(
        synthetic_event_key=_lifecycle_message_key(event_key),
        role=InstagramBotMessage.Role.MODEL,
        source="lifecycle",
    )


def _lifecycle_message_has_provider_io(message) -> bool:
    if message is None:
        return False
    return bool(
        message.send_started_at
        or message.provider_message_id
        or message.send_state in {"sending", "sent", "unknown"}
        or message.status in {
            InstagramBotMessage.Status.DONE,
            InstagramBotMessage.Status.FAILED,
        }
    )


def _mark_lifecycle_message_unknown(message, *, when=None) -> None:
    if message is None:
        return
    when = when or timezone.now()
    InstagramBotMessage.objects.filter(pk=message.pk).update(
        status=InstagramBotMessage.Status.FAILED,
        send_state="unknown",
        processed_at=when,
    )
    message.status = InstagramBotMessage.Status.FAILED
    message.send_state = "unknown"
    message.processed_at = when


def _checkpoint_lifecycle_provider_receipt(
    event_id: int,
    lease: str,
    provider_message_id: str,
) -> None:
    """Persist one confirmed Meta receipt before any later fallible step."""
    receipt_id = normalize_provider_message_id(provider_message_id)
    if not receipt_id:
        raise ValueError("provider receipt checkpoint requires a message ID")

    with transaction.atomic():
        event = (
            IgLifecycleEvent.objects.select_for_update()
            .filter(
                pk=event_id,
                lease_token=lease,
                state=IgLifecycleEvent.State.PROCESSING,
            )
            .first()
        )
        if event is None:
            raise RuntimeError("lifecycle lease lost before receipt checkpoint")
        lifecycle_message = (
            _lifecycle_message_queryset(event.event_key)
            .select_for_update()
            .first()
        )
        if not _lifecycle_message_has_provider_io(lifecycle_message):
            raise RuntimeError("lifecycle outbox marker missing before receipt checkpoint")

        receipt_ids = list(normalize_provider_message_ids([
            *(lifecycle_message.delivery_provider_message_ids or []),
            receipt_id,
        ]))
        from management.services.instagram_bot import _split_for_send

        delivered_count = max(
            len(receipt_ids),
            int(lifecycle_message.delivery_delivered_chunk_count or 0),
        )
        planned_count = max(
            delivered_count,
            int(lifecycle_message.delivery_planned_chunk_count or 0),
            len(_split_for_send(lifecycle_message.text)),
        )
        first_receipt_id = receipt_ids[0]
        InstagramBotMessage.objects.filter(pk=lifecycle_message.pk).update(
            provider_message_id=first_receipt_id[:255],
            delivery_original_text=lifecycle_message.text,
            delivery_planned_chunk_count=planned_count,
            delivery_delivered_chunk_count=delivered_count,
            delivery_provider_message_ids=receipt_ids,
        )
        if not event.provider_message_id:
            event.provider_message_id = first_receipt_id[:128]
            event.save(update_fields=["provider_message_id", "updated_at"])


def _context_for_order(order):
    """Return the exact proposal/client/deal/episode/attribution context."""
    attribution = IgOrderAttribution.objects.select_related(
        "client", "deal"
    ).filter(order_id=order.pk).first()
    proposal = IgCheckoutProposal.objects.select_related(
        "client", "deal", "commercial_episode", "payment_attempt"
    ).filter(payment_attempt__order_id=order.pk).first()
    if attribution is None or proposal is None:
        return None
    if attribution.client_id != proposal.client_id or attribution.deal_id != proposal.deal_id:
        logger.warning(
            "IG lifecycle context mismatch for order %s: attribution=%s proposal=%s",
            order.pk,
            attribution.pk,
            proposal.pk,
        )
        return None
    episode = proposal.commercial_episode
    return {
        "attribution": attribution,
        "proposal": proposal,
        "client": proposal.client,
        "deal": proposal.deal,
        "episode": episode,
    }


def _assignment_belongs_to_client(order_id, client_id, *, for_update=False):
    assignments = IgOrderAssignment.objects
    if for_update:
        assignments = assignments.select_for_update()
    assignment = assignments.filter(order_id=order_id).only(
        "client_id", "unassigned_at"
    ).first()
    if assignment is None:
        return False
    return assignment.client_id == client_id and assignment.unassigned_at is None


def _assignment_snapshot_for_client(order_id, client_id, *, for_update=False):
    assignments = IgOrderAssignment.objects
    if for_update:
        assignments = assignments.select_for_update()
    assignment = (
        assignments.filter(
            order_id=order_id,
            client_id=client_id,
            unassigned_at__isnull=True,
        )
        .only("pk", "version")
        .first()
    )
    if assignment is None:
        return None
    return {
        "assignment_id": assignment.pk,
        "assignment_version": assignment.version,
    }


def _assignment_matches_event(event, *, for_update=False):
    payload = event.payload or {}
    try:
        assignment_id = int(payload.get("assignment_id"))
        assignment_version = int(payload.get("assignment_version"))
    except (TypeError, ValueError):
        return False
    if assignment_id <= 0 or assignment_version <= 0:
        return False
    assignments = IgOrderAssignment.objects
    if for_update:
        assignments = assignments.select_for_update()
    return assignments.filter(
        pk=assignment_id,
        order_id=event.order_id,
        client_id=event.client_id,
        version=assignment_version,
        unassigned_at__isnull=True,
    ).exists()


def _event_key(order, kind, payload):
    if kind == IgLifecycleEvent.Kind.PAYMENT_VERIFIED:
        return f"payment:{payload['attempt_id']}:verified"
    if kind == IgLifecycleEvent.Kind.TTN_CREATED:
        return f"ttn:{order.pk}:{_tracking_digest(payload['tracking_number'])}"
    if kind == IgLifecycleEvent.Kind.PARCEL_ARRIVED:
        # Одне нагадування на ТТН: якщо перевізник повторно віддасть код 7 (а він
        # віддає його на кожному опитуванні), другого повідомлення клієнту не буде.
        return f"arrived:{order.pk}:{_tracking_digest(payload.get('tracking_number') or '')}"
    return f"delivered:{order.pk}"


def _message_for(kind, locale, order, payload) -> str:
    locale = _locale(locale)
    if kind == IgLifecycleEvent.Kind.PAYMENT_VERIFIED:
        amount = payload.get("amount") or ""
        recipient = str(order.full_name or "").strip()
        phone = str(order.phone or "").strip()
        city = str(order.city or "").strip()
        office = str(order.np_office or "").strip()
        copies = {
            "uk": (
                f"Дякуємо, оплату отримано. Замовлення #{order.order_number} готуємо для {recipient}. "
                f"Доставка: {city}, {office}. Телефон для зв'язку: {phone}. "
                "Я надішлю ТТН сюди, щойно її буде створено."
            ),
            "ru": (
                f"Спасибо, оплату получили. Заказ #{order.order_number} готовим для {recipient}. "
                f"Доставка: {city}, {office}. Телефон для связи: {phone}. "
                "Я пришлю ТТН сюда, как только она будет создана."
            ),
            "en": (
                f"Thank you, payment received. Order #{order.order_number} is being prepared for {recipient}. "
                f"Delivery: {city}, {office}. Contact phone: {phone}. "
                "I will send the tracking number here as soon as it is created."
            ),
        }
        if amount:
            copies["uk"] += f" Сума: {amount} грн."
            copies["ru"] += f" Сумма: {amount} грн."
            copies["en"] += f" Amount: {amount} UAH."
        return copies[locale]
    if kind == IgLifecycleEvent.Kind.TTN_CREATED:
        ttn = str(payload.get("tracking_number") or "").strip()
        track_url = f"{NP_TRACKING_URL}{ttn}"
        copies = {
            "uk": f"Ваше замовлення #{order.order_number} підготовлено до відправлення.\nТТН: {ttn}\nВідстежити: {track_url}",
            "ru": f"Ваш заказ #{order.order_number} подготовлен к отправке.\nТТН: {ttn}\nОтследить: {track_url}",
            "en": f"Your order #{order.order_number} is ready for shipment.\nTracking number: {ttn}\nTrack it: {track_url}",
        }
        return copies[locale]
    if kind == IgLifecycleEvent.Kind.PARCEL_ARRIVED:
        ttn = str(payload.get("tracking_number") or "").strip()
        office = str(order.np_office or "").strip()
        city = str(order.city or "").strip()
        where = ", ".join(part for part in (city, office) if part)
        # Строк зберігання СВІДОМО не називається: він залежить від типу
        # відправлення, і назвати неперевірену дату гірше, ніж не називати
        # жодної. Повідомляємо факт і пропонуємо нагадати.
        copies = {
            "uk": (
                f"Ваша посилка із замовленням #{order.order_number} вже у відділенні"
                + (f" ({where})" if where else "")
                + f".\nТТН: {ttn}"
                "\nЯкщо забрали — напишіть «забрав», і я закрию замовлення. "
                "Якщо ще ні — можу нагадати пізніше, щоб посилка не поїхала назад."
            ),
            "ru": (
                f"Ваша посылка с заказом #{order.order_number} уже в отделении"
                + (f" ({where})" if where else "")
                + f".\nТТН: {ttn}"
                "\nЕсли забрали — напишите «забрал», и я закрою заказ. "
                "Если ещё нет — могу напомнить позже, чтобы посылка не уехала назад."
            ),
            "en": (
                f"Your parcel for order #{order.order_number} has arrived at the branch"
                + (f" ({where})" if where else "")
                + f".\nTracking number: {ttn}"
                "\nIf you already picked it up, reply \"picked up\" and I will close the order. "
                "If not yet, I can remind you later so the parcel is not returned."
            ),
        }
        return copies[locale]
    return post_purchase_invitation_text(locale, order)


def _message(event: IgLifecycleEvent) -> str:
    final_text = str(getattr(event, "final_text", "") or "").strip()
    if final_text:
        return final_text
    payload = event.payload or {}
    snapshot = payload.get(MESSAGE_SNAPSHOT_KEY)
    if isinstance(snapshot, str) and snapshot:
        return snapshot
    return _message_for(event.kind, event.locale, event.order, payload)


def _base_message(event: IgLifecycleEvent) -> str:
    """Return the committed lifecycle copy, excluding optional follow text."""
    # Keep the existing `_message` seam available to receipt tests and
    # operational callers that intentionally provide a deterministic override
    # while the event has not yet been materialized.
    if not str(getattr(event, "final_text", "") or "").strip():
        return _message(event)
    payload = event.payload or {}
    snapshot = payload.get(MESSAGE_SNAPSHOT_KEY)
    if isinstance(snapshot, str) and snapshot:
        return snapshot
    return _message_for(event.kind, event.locale, event.order, payload)


def _lifecycle_follow_authorization(event: IgLifecycleEvent):
    """Return the reserved payment CTA snapshot for the final send boundary.

    A lifecycle retry may already have persisted ``event.final_text`` and left
    its follow decision in ``RESERVED``. Reconstructing the immutable
    authorization object in that case lets the same provider boundary
    revalidate the optional clause before every Meta request.
    """
    if getattr(event, "kind", "") != IgLifecycleEvent.Kind.PAYMENT_VERIFIED:
        return None
    try:
        from management.ig_bot_models import IgFollowCtaDecision
        from management.services.ig_follow_cta import (
            AuthorizedFollowCta,
            authorize_follow_cta,
            finalize_follow_delivery,
        )

        decision = (
            IgFollowCtaDecision.objects.filter(
                lifecycle_event_id=event.pk,
                opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
                state__in=(
                    IgFollowCtaDecision.State.PREPARED,
                    IgFollowCtaDecision.State.RESERVED,
                ),
            )
            .order_by("-id")
            .first()
        )
        if decision is None:
            return None
        base_text = _base_message(event)
        if decision.state == IgFollowCtaDecision.State.RESERVED:
            # The lifecycle event owns the frozen text after the first
            # materialization. A mismatched snapshot is deliberately not
            # reconstructed; normal lifecycle finalization will release it.
            if (
                not decision.lease_token
                or decision.base_text != base_text
                or not decision.final_text
                or (
                    event.final_text
                    and decision.final_text != event.final_text
                )
            ):
                return None
            return AuthorizedFollowCta(
                decision_id=decision.pk,
                text=decision.candidate_text,
                base_text=decision.base_text,
                final_text=decision.final_text,
                lease_token=decision.lease_token,
            )
        authorized = authorize_follow_cta(
            decision.pk,
            current_base_text=base_text,
            now=timezone.now(),
        )
        if authorized is None:
            finalize_follow_delivery(
                decision.pk,
                outcome="cancelled_before_io",
                now=timezone.now(),
            )
            return None
        return authorized
    except Exception:
        logger.exception("Unable to authorize prepared lifecycle follow CTA %s", getattr(event, "pk", None))
        return None


def _prepared_follow_text(event: IgLifecycleEvent) -> str:
    """Authorize one already-prepared payment CTA without provider I/O."""
    authorized = _lifecycle_follow_authorization(event)
    return authorized.final_text if authorized is not None else ""


def _lifecycle_follow_snapshot(event: IgLifecycleEvent):
    """Return the active optional CTA and safe base fallback for this event.

    ``final_text`` is immutable once materialized, but the reserved follow
    decision can be cancelled by another worker before the provider boundary.
    A non-base payment snapshot without an active reservation therefore has to
    downgrade to the committed lifecycle copy rather than leak stale CTA text.
    """
    base_text = _base_message(event)
    final_text = str(getattr(event, "final_text", "") or "").strip()
    if (
        getattr(event, "kind", "") != IgLifecycleEvent.Kind.PAYMENT_VERIFIED
        or not final_text
        or final_text == base_text
    ):
        return None, ""
    try:
        from management.ig_bot_models import IgFollowCtaDecision
        from management.services.ig_follow_cta import finalize_follow_delivery

        decision = (
            IgFollowCtaDecision.objects.filter(
                lifecycle_event_id=event.pk,
                opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
            )
            .order_by("-id")
            .first()
        )
        # If there is no decision, preserve a caller-provided immutable
        # payment copy. A known follow decision, however, makes this an
        # optional CTA and stale state must fail closed to the base message.
        if decision is None:
            return None, ""
        authorized = _lifecycle_follow_authorization(event)
        if authorized is not None:
            return authorized, base_text
        if (
            decision.state == IgFollowCtaDecision.State.RESERVED
            and decision.lease_token
        ):
            finalize_follow_delivery(
                decision.pk,
                outcome="cancelled_before_io",
                lease_token=decision.lease_token,
                now=timezone.now(),
            )
        return None, base_text
    except Exception:
        logger.exception(
            "Unable to load lifecycle follow snapshot for event %s",
            getattr(event, "pk", None),
        )
        return None, base_text


def _replace_lifecycle_message_snapshot(event_id: int, lease: str, text: str) -> None:
    """Record the actual base text when the optional CTA is downgraded."""
    normalized = str(text or "").strip()
    if not normalized:
        return
    event = (
        IgLifecycleEvent.objects.filter(
            pk=event_id,
            lease_token=lease,
            state=IgLifecycleEvent.State.PROCESSING,
        )
        .only("event_key")
        .first()
    )
    if event is None:
        return
    InstagramBotMessage.objects.filter(
        synthetic_event_key=_lifecycle_message_key(event.event_key),
        role=InstagramBotMessage.Role.MODEL,
        source="lifecycle",
    ).update(text=normalized)


def materialize_lifecycle_follow_text(event: IgLifecycleEvent) -> str:
    """Choose a final lifecycle snapshot; only verified payment may append CTA."""
    existing = str(getattr(event, "final_text", "") or "").strip()
    if existing:
        return existing
    base = _base_message(event)
    if getattr(event, "kind", "") != IgLifecycleEvent.Kind.PAYMENT_VERIFIED:
        return base
    return _prepared_follow_text(event) or base


def _persist_lifecycle_final_text(event_id: int, text: str):
    """Set the one-time final text before the provider boundary is claimed."""
    with transaction.atomic():
        event = IgLifecycleEvent.objects.select_for_update().get(pk=event_id)
        normalized = str(text or "").strip() or _base_message(event)
        if event.final_text and event.final_text != normalized:
            raise ValueError("IgLifecycleEvent final_text is immutable")
        if not event.final_text:
            event.final_text = normalized
            event.save(update_fields=["final_text", "updated_at"])
        return event


def _finalize_lifecycle_follow_decision(event_id: int, lifecycle_state: str) -> None:
    """Project the mandatory lifecycle receipt onto its optional CTA decision."""
    try:
        from management.ig_bot_models import IgFollowCtaDecision
        from management.services.ig_follow_cta import finalize_follow_delivery

        decision = (
            IgFollowCtaDecision.objects.filter(
                lifecycle_event_id=event_id,
                opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
                state=IgFollowCtaDecision.State.RESERVED,
            )
            .order_by("-id")
            .first()
        )
        if decision is None:
            return
        event = IgLifecycleEvent.objects.filter(pk=event_id).first()
        lifecycle_message = (
            _lifecycle_message_queryset(event.event_key).first()
            if event is not None
            else None
        )
        provider_ids = normalize_provider_message_ids(
            lifecycle_message.delivery_provider_message_ids
            if lifecycle_message is not None
            else []
        )
        if event is not None and event.provider_message_id:
            provider_id = normalize_provider_message_id(event.provider_message_id)
            if provider_id and provider_id not in provider_ids:
                provider_ids = (provider_id, *provider_ids)
        if lifecycle_state == IgLifecycleEvent.State.SENT:
            outcome = "sent"
        elif (
            lifecycle_state == IgLifecycleEvent.State.AMBIGUOUS
            or _lifecycle_message_has_provider_io(lifecycle_message)
        ):
            outcome = "ambiguous"
        elif lifecycle_state in {
            IgLifecycleEvent.State.CANCELLED,
            IgLifecycleEvent.State.FAILED,
            IgLifecycleEvent.State.MANAGER_REVIEW,
        }:
            outcome = "cancelled_before_io"
        else:
            # A retryable pre-provider lifecycle failure retains the exact
            # frozen text and reservation for that same event generation.
            return
        finalize_follow_delivery(
            decision.pk,
            outcome=outcome,
            provider_message_ids=provider_ids,
            now=timezone.now(),
        )
    except Exception:
        logger.exception("Unable to finalize lifecycle follow decision %s", event_id)


@transaction.atomic
def ensure_lifecycle_event(order, kind, *, payload=None, due_at=None):
    """Create one event generation from committed order truth.

    A cancelled generation remains immutable audit evidence. Once its business
    truth is restored, a new deterministic generation is materialized instead
    of silently reviving or permanently suppressing the original event.
    """
    initial_context = _context_for_order(order)
    if initial_context is None:
        return None, False
    projection = (
        IgPaymentProjection.objects.select_for_update()
        .filter(
            deal_id=initial_context["deal"].pk,
            client_id=initial_context["client"].pk,
        )
        .first()
    )
    order = Order.objects.select_for_update().filter(pk=order.pk).first()
    if order is None:
        return None, False
    context = _context_for_order(order)
    if (
        context is None
        or context["deal"].pk != initial_context["deal"].pk
        or context["client"].pk != initial_context["client"].pk
    ):
        return None, False
    assignment_snapshot = _assignment_snapshot_for_client(
        order.pk,
        context["client"].pk,
        for_update=True,
    )
    if assignment_snapshot is None:
        return None, False
    payload = dict(payload or {})
    payload.update(assignment_snapshot)
    cancellation_reason = _business_truth_cancellation_reason(
        kind=kind,
        payload=payload,
        payment_truth=projection.truth if projection is not None else None,
        order=order,
        assignment_matches=True,
    )
    if cancellation_reason:
        return None, False
    key = _event_key(order, kind, payload)
    locale = _locale(getattr(context["client"], "language", "uk"))
    if kind == IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED:
        invitation, message = post_purchase_invitation_snapshot(context["client"], order, locale)
        payload[INVITATION_PAYLOAD_KEY] = invitation
        payload[MESSAGE_SNAPSHOT_KEY] = message
    else:
        payload[MESSAGE_SNAPSHOT_KEY] = _message_for(kind, locale, order, payload)
    defaults = {
        "kind": kind,
        "client": context["client"],
        "deal": context["deal"],
        "proposal": context["proposal"],
        "order": order,
        "commercial_episode": context["episode"],
        "attribution": context["attribution"],
        "locale": locale,
        "payload": payload,
        "due_at": due_at or timezone.now(),
    }
    canonical = (
        IgLifecycleEvent.objects.select_for_update().filter(event_key=key).first()
    )
    if canonical is None:
        return IgLifecycleEvent.objects.get_or_create(
            event_key=key,
            defaults=defaults,
        )
    if canonical.state != IgLifecycleEvent.State.CANCELLED:
        return canonical, False

    retry_prefix = f"{key}:retry:"
    latest = (
        IgLifecycleEvent.objects.select_for_update()
        .filter(event_key__startswith=retry_prefix)
        .order_by("-pk")
        .first()
    )
    if latest is not None and latest.state != IgLifecycleEvent.State.CANCELLED:
        return latest, False
    previous = latest or canonical
    if previous.last_error not in RECOVERABLE_CANCELLATION_REASONS:
        return previous, False
    replacement = IgLifecycleEvent.objects.create(
        event_key=f"{retry_prefix}{previous.pk}",
        **defaults,
    )
    return replacement, True


def _response_window_open(client, now):
    # Э2.6: вікно відкриває тільки повідомлення КЛІЄНТА. `last_message_at`
    # змішує вхідні й вихідні, тому власне повідомлення бота «відкривало» вікно,
    # якого не було, і відправка отримувала відмову провайдера.
    anchor = getattr(client, "meta_window_anchor", None)
    return bool(anchor and now <= anchor + RESPONSE_WINDOW)


def _queue_manager_task(event: IgLifecycleEvent) -> None:
    """Persist one actionable, deterministic task for an out-of-window event."""
    from management.models import IgFollowUpTask

    reason = f"ig_lifecycle:{event.event_key}"
    IgFollowUpTask.objects.get_or_create(
        client=event.client,
        deal=event.deal,
        kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason=reason,
        defaults={
            "due_at": timezone.now(),
            "status": IgFollowUpTask.Status.PENDING,
            "level": 0,
            "message_text": _message(event),
            "meta_window_deadline": timezone.now(),
        },
    )


def _notify_lifecycle_delivery_review(event: IgLifecycleEvent) -> None:
    try:
        from management.services.ig_alerts import format_operator_alert
        from management.services.instagram_bot import notify_manager

        notify_manager(
            format_operator_alert(
                "⚠️ IG: не вдалося доставити lifecycle-подію",
                event_type="ig_lifecycle_delivery_review",
                client_id=event.client_id,
                deal_id=event.deal_id,
                proposal_id=event.proposal_id,
                lifecycle_event_id=event.pk,
                status="delivery_failed",
                instruction_code="ig_lifecycle_delivery_review",
            ),
            dedupe_key=f"ig-lifecycle:delivery:{event.event_key}",
            event_type="ig_lifecycle_delivery_review",
            client=event.client,
        )
    except Exception:
        logger.exception("Unable to alert manager for lifecycle event %s", event.pk)


def _notify_lifecycle_window_review(event: IgLifecycleEvent) -> None:
    try:
        from management.services.ig_alerts import format_operator_alert
        from management.services.instagram_bot import notify_manager

        notify_manager(
            format_operator_alert(
                "⚠️ IG: lifecycle-подія потребує відповіді менеджера",
                event_type="ig_lifecycle_window_review",
                client_id=event.client_id,
                deal_id=event.deal_id,
                proposal_id=event.proposal_id,
                lifecycle_event_id=event.pk,
                status="response_window_closed",
                instruction_code="ig_lifecycle_window_review",
            ),
            dedupe_key=f"ig-lifecycle:window:{event.event_key}",
            event_type="ig_lifecycle_window_review",
            client=event.client,
        )
    except Exception:
        logger.exception(
            "Unable to create manager review for lifecycle event %s",
            event.pk,
        )


def _publish_lifecycle_window_review(event_id: int) -> None:
    event = (
        IgLifecycleEvent.objects.select_related("client", "deal", "order")
        .filter(pk=event_id)
        .first()
    )
    if event is None or event.state != IgLifecycleEvent.State.MANAGER_REVIEW:
        return
    _project_order_channel(event)
    _queue_manager_task(event)
    _notify_lifecycle_window_review(event)


def _notify_lifecycle_permission_review(event: IgLifecycleEvent, reason: str) -> None:
    try:
        from management.services.ig_alerts import format_operator_alert
        from management.services.instagram_bot import notify_manager

        notify_manager(
            format_operator_alert(
                "⚠️ IG: lifecycle-подія довго очікує дозволу на відправку",
                event_type="ig_lifecycle_permission_review",
                client_id=event.client_id,
                deal_id=event.deal_id,
                proposal_id=event.proposal_id,
                lifecycle_event_id=event.pk,
                status=reason,
                instruction_code="ig_lifecycle_permission_review",
            ),
            dedupe_key=f"ig-lifecycle:permission:{event.event_key}",
            event_type="ig_lifecycle_permission_review",
            client=event.client,
        )
    except Exception:
        logger.exception(
            "Unable to alert manager about deferred lifecycle event %s",
            event.pk,
        )


def _cancel_event(event_id: int, reason: str, *, lease: str) -> str:
    with transaction.atomic():
        event = IgLifecycleEvent.objects.select_for_update().get(pk=event_id)
        if event.state in {
            IgLifecycleEvent.State.SENT,
            IgLifecycleEvent.State.CANCELLED,
        }:
            state = event.state
        elif (
            event.state != IgLifecycleEvent.State.PROCESSING
            or event.lease_token != lease
        ):
            state = event.state
        else:
            event.state = IgLifecycleEvent.State.CANCELLED
            event.lease_token = ""
            event.lease_expires_at = None
            event.last_error = reason[:1000]
            event.save(update_fields=[
                "state", "lease_token", "lease_expires_at", "last_error", "updated_at",
            ])
            state = event.state
    _project_order_channel(event)
    return state


def _apply_permission_deferral(event: IgLifecycleEvent, reason: str, *, now) -> bool:
    """Release a pre-provider lease without consuming delivery retry budget."""
    event.lease_token = ""
    event.lease_expires_at = None
    event.attempts = max(0, int(event.attempts or 0) - 1)
    event.last_error = reason[:1000]
    timed_out = bool(
        event.created_at
        and now - event.created_at >= PERMISSION_DEFERRAL_DURATION
    )
    if timed_out:
        event.state = IgLifecycleEvent.State.MANAGER_REVIEW
        event.due_at = now
    else:
        event.state = IgLifecycleEvent.State.WAITING_WINDOW
        event.due_at = now + PERMISSION_DEFERRAL_DELAY
    return timed_out


def _defer_event_for_permission(event_id: int, reason: str, *, lease: str) -> str:
    """Defer a temporary permission denial or escalate an overdue event."""
    now = timezone.now()
    needs_manager_review = False
    with transaction.atomic():
        event = IgLifecycleEvent.objects.select_for_update().get(pk=event_id)
        if (
            event.state == IgLifecycleEvent.State.PROCESSING
            and event.lease_token == lease
        ):
            needs_manager_review = _apply_permission_deferral(
                event,
                reason,
                now=now,
            )
            event.save(
                update_fields=[
                    "state",
                    "lease_token",
                    "lease_expires_at",
                    "attempts",
                    "last_error",
                    "due_at",
                    "updated_at",
                ]
            )
        state = event.state
    _project_order_channel(event)
    if needs_manager_review:
        _queue_manager_task(event)
        _notify_lifecycle_permission_review(event, reason)
    return state


def _escalate_event_to_manager_review(
    event: IgLifecycleEvent,
    event_id: int,
    *,
    reason: str,
    lease: str,
    now,
) -> str:
    """Отдать событие человеку, не потратив бюджет доставки.

    Вынесено из `dispatch_lifecycle_event`, потому что теперь этот же исход
    возвращает объект решения Э0.4 (`escalate`), и две копии кода разошлись бы
    в первом же изменении.
    """
    with transaction.atomic():
        owned = IgLifecycleEvent.objects.select_for_update().get(pk=event_id)
        if owned.lease_token != lease:
            return owned.state
        owned.state = IgLifecycleEvent.State.MANAGER_REVIEW
        owned.lease_token = ""
        owned.lease_expires_at = None
        owned.last_error = reason
        owned.due_at = now
        owned.save(
            update_fields=[
                "state",
                "lease_token",
                "lease_expires_at",
                "last_error",
                "due_at",
                "updated_at",
            ]
        )
    _project_order_channel(owned)
    _queue_manager_task(event)
    _notify_lifecycle_window_review(event)
    return IgLifecycleEvent.State.MANAGER_REVIEW


def _legacy_lifecycle_verdict(event: IgLifecycleEvent, permission, *, now) -> str:
    """Исход старых проверок в словаре Э0.4 — только чтобы считать расхождение.

    Порядок повторяет порядок ветвей ниже; дополнительных запросов не делает.
    """
    from management.services.ig_outgoing_policy import ALLOW, BLOCK, DEFER, ESCALATE

    if not permission:
        reason = str(getattr(permission, "reason", "") or "")
        return DEFER if reason in TRANSIENT_PERMISSION_REASONS else BLOCK
    if not _response_window_open(event.client, now):
        return ESCALATE
    return ALLOW


def _lifecycle_outgoing_request(event: IgLifecycleEvent, permission, *, settings):
    """Собрать вход политики из уже прочитанных строк.

    Никаких новых запросов: `event.client` уже select_related, а транспорт
    читается из настроек. `quiet_hours` не передаём сознательно — таймзоны
    клиента в модели нет, а выдумывать её значило бы задержать транзакционное
    подтверждение по несуществующим данным. Жалобу и открытую заявку тоже пока
    не передаём: их владелец — Э6, и регистрировать вход, которого нет, значило
    бы соврать в audit.
    """
    from management.services.ig_outgoing_policy import (
        CaseRiskState,
        FrequencyState,
        OutgoingRequest,
        VERIFIED_CONTRACT_VERSION,
        EVENT_KIND_PURPOSE,
    )
    from management.services.instagram_bot import provider_transport

    kind = str(getattr(event.kind, "value", event.kind) or "")
    event_kind = f"lifecycle.{kind}"
    return OutgoingRequest(
        platform_contract_version=VERIFIED_CONTRACT_VERSION,
        event_kind=event_kind,
        message_purpose=EVENT_KIND_PURPOSE.get(event_kind, ""),
        channel_app_type=provider_transport(settings),
        latest_user_provider_ts=getattr(event.client, "meta_window_anchor", None),
        frequency_state=FrequencyState(),
        case_risk_state=CaseRiskState(
            permission_reason=str(getattr(permission, "reason", "") or ""),
        ),
    )


def _apply_lifecycle_policy_decision(
    event: IgLifecycleEvent,
    event_id: int,
    decision,
    permission,
    *,
    lease: str,
    now,
) -> str | None:
    """Исполнить решение политики. `None` означает «продолжай».

    `allow` НЕ отправляет: он лишь пропускает поток дальше, к финальной
    revalidation, durable claim и receipt-first, которые остаются на месте.

    Durable `last_error` сознательно остаётся в реестре `LAST_ERROR_REASONS`:
    точный `reason_code` политики уходит в метрику и лог, а не в новое
    нетипизированное состояние. Исход, для которого у этого потока пока нет
    durable-представления (тихие часы, frequency, жалоба), не исполняется —
    поток возвращается к старой проверке с предупреждением, а не изобретает
    состояние.
    """
    from management.services.ig_outgoing_policy import ALLOW, BLOCK, DEFER, ESCALATE

    if decision.decision == ALLOW:
        return None
    if decision.decision == BLOCK:
        permission_reason = str(getattr(permission, "reason", "") or "")
        return _cancel_event(
            event_id,
            permission_reason or CUSTOMER_SEND_NOT_ALLOWED_ERROR,
            lease=lease,
        )
    if decision.decision == DEFER:
        if decision.reason_code in TRANSIENT_PERMISSION_REASONS:
            return _defer_event_for_permission(
                event_id,
                decision.reason_code,
                lease=lease,
            )
        logger.warning(
            "lifecycle has no durable form for policy defer %s; falling back",
            decision.reason_code,
        )
        return None
    if decision.decision == ESCALATE:
        if decision.reason_code == "outside_standard_window":
            return _escalate_event_to_manager_review(
                event,
                event_id,
                reason=STANDARD_RESPONSE_WINDOW_CLOSED,
                lease=lease,
                now=now,
            )
        logger.warning(
            "lifecycle has no durable form for policy escalate %s; falling back",
            decision.reason_code,
        )
        return None
    return None


def _transient_permission_reason(kind: str, hint: str, failure_boundary: str) -> str:
    """Normalize only definite pre-provider permission denials into retryable control state."""
    if kind != "cancelled":
        return ""
    normalized_hint = str(hint or "").strip()
    if normalized_hint in TRANSIENT_PERMISSION_REASONS:
        return normalized_hint
    boundary_reason = str(failure_boundary or "").rsplit(":", 1)[-1]
    if boundary_reason in TRANSIENT_PERMISSION_REASONS:
        return boundary_reason
    if "permission epoch changed" in normalized_hint.lower():
        return "permission_epoch_changed"
    return ""


@dataclass(frozen=True)
class _LifecycleDeliveryResult:
    ok: bool
    kind: str
    hint: str
    provider_message_id: str
    provider_message_ids: tuple[str, ...]
    planned_chunk_count: int
    delivered_chunk_count: int
    failure_boundary: str
    receipt_present: bool


def _delivery_result(result):
    """Normalize legacy tuples and optional structured provider receipts."""
    provider_message_id = normalize_provider_message_id(
        getattr(result, "provider_message_id", "")
    )
    provider_message_ids: tuple[str, ...] = ()
    planned_chunk_count = 0
    delivered_chunk_count = 0
    failure_boundary = ""
    receipt_present = not isinstance(result, tuple)
    if isinstance(result, tuple):
        if len(result) >= 4:
            ok, kind, hint, provider_message_id = result[:4]
        else:
            ok, kind, hint = result
    else:
        ok = bool(getattr(result, "ok", False))
        kind = str(getattr(result, "kind", "unknown") or "unknown")
        hint = str(getattr(result, "hint", "") or "")
        provider_message_ids = normalize_provider_message_ids(
            getattr(result, "provider_message_ids", ())
        )
        try:
            planned_chunk_count = max(
                0, int(getattr(result, "planned_chunk_count", 0) or 0)
            )
        except (TypeError, ValueError):
            planned_chunk_count = 0
        try:
            delivered_chunk_count = max(
                0, int(getattr(result, "delivered_chunk_count", 0) or 0)
            )
        except (TypeError, ValueError):
            delivered_chunk_count = 0
        failure_boundary = str(getattr(result, "failure_boundary", "") or "")[:64]
    if provider_message_id and provider_message_id not in provider_message_ids:
        provider_message_ids = (provider_message_id, *provider_message_ids)
    if not receipt_present and provider_message_id:
        planned_chunk_count = 1
        delivered_chunk_count = 1
    return _LifecycleDeliveryResult(
        ok=bool(ok),
        kind=str(kind or "unknown"),
        hint=str(hint or ""),
        provider_message_id=provider_message_id,
        provider_message_ids=provider_message_ids,
        planned_chunk_count=planned_chunk_count,
        delivered_chunk_count=delivered_chunk_count,
        failure_boundary=failure_boundary,
        receipt_present=receipt_present,
    )


def _delivery_result_after_provider_exception(
    event_id: int,
    exc: Exception,
) -> _LifecycleDeliveryResult:
    """Recover the latest durable receipt without exposing provider details."""
    event = IgLifecycleEvent.objects.filter(pk=event_id).first()
    lifecycle_message = (
        _lifecycle_message_queryset(event.event_key).first()
        if event is not None
        else None
    )
    provider_message_ids = normalize_provider_message_ids(
        lifecycle_message.delivery_provider_message_ids
        if lifecycle_message is not None
        else []
    )
    delivered_chunk_count = max(
        len(provider_message_ids),
        int(
            lifecycle_message.delivery_delivered_chunk_count
            if lifecycle_message is not None
            else 0
        ),
    )
    planned_chunk_count = max(
        delivered_chunk_count,
        int(
            lifecycle_message.delivery_planned_chunk_count
            if lifecycle_message is not None
            else 0
        ),
    )
    provider_message_id = normalize_provider_message_id(
        (event.provider_message_id if event is not None else "")
        or (
            lifecycle_message.provider_message_id
            if lifecycle_message is not None
            else ""
        )
        or (provider_message_ids[0] if provider_message_ids else "")
    )
    provider_io_started = _lifecycle_message_has_provider_io(lifecycle_message)
    return _LifecycleDeliveryResult(
        ok=False,
        kind="unknown" if provider_io_started else "retryable",
        hint=exc.__class__.__name__,
        provider_message_id=provider_message_id,
        provider_message_ids=provider_message_ids,
        planned_chunk_count=planned_chunk_count,
        delivered_chunk_count=delivered_chunk_count,
        failure_boundary=(
            f"chunk:{delivered_chunk_count + 1}:provider_exception"
            if provider_io_started
            else "preflight:provider_exception"
        ),
        receipt_present=provider_io_started,
    )


def _project_order_channel(event: IgLifecycleEvent) -> None:
    """Expose Direct outcome beside the other independent payment channels."""
    state_map = {
        IgLifecycleEvent.State.SENT: "sent",
        IgLifecycleEvent.State.AMBIGUOUS: "ambiguous",
        IgLifecycleEvent.State.FAILED: "failed",
        IgLifecycleEvent.State.CANCELLED: "disabled",
        IgLifecycleEvent.State.MANAGER_REVIEW: "pending",
        IgLifecycleEvent.State.WAITING_WINDOW: "pending",
        IgLifecycleEvent.State.PENDING: "pending",
        IgLifecycleEvent.State.PROCESSING: "pending",
    }
    try:
        from storefront.views.utils import _record_post_payment_channel

        _record_post_payment_channel(
            event.order_id,
            "instagram_lifecycle",
            state_map.get(event.state, "unknown"),
            error=event.last_error,
            metadata={
                "provider_message_id": event.provider_message_id,
                "lifecycle_event_id": event.pk,
                "kind": event.kind,
                "event_key": event.event_key,
                "lifecycle_stage": LIFECYCLE_PROJECTION_STAGE.get(event.kind, 0),
                "lifecycle_event_updated_at": event.updated_at.isoformat(),
            },
            monotonic_metadata_key="lifecycle_event_id",
            monotonic_stage_key="lifecycle_stage",
            monotonic_revision_key="lifecycle_event_updated_at",
        )
    except Exception:
        logger.exception(
            "Unable to persist Instagram channel state for lifecycle event %s", event.pk
        )


def _business_truth_cancellation_reason(
    *,
    kind: str,
    payload: dict,
    payment_truth: str | None,
    order,
    assignment_matches: bool,
) -> str:
    # «Посилка у відділенні» — фізичний факт, він не залежить від того, чи
    # підтверджена оплата. Вимагати тут verified payment означало б відключити
    # нагадування саме для наложки, тобто для випадку, де незабрана посилка
    # коштує найдорожче. Прив'язка замовлення до цього клієнта нижче
    # (`assignment_matches`) залишається обов'язковою.
    if (
        kind != IgLifecycleEvent.Kind.PARCEL_ARRIVED
        and payment_truth not in VERIFIED_PAYMENT_TRUTHS
    ):
        return PAYMENT_NOT_VERIFIED_ERROR
    if order is None:
        return ORDER_MISSING_ERROR
    if kind == IgLifecycleEvent.Kind.PARCEL_ARRIVED:
        # Гонка між оновленням трекінгу і відправкою: якщо клієнт уже забрав,
        # нагадування скасовується БЕЗ звернення до провайдера.
        if nova_poshta_order_fulfillment_confirmed(order):
            return PARCEL_ALREADY_RECEIVED_ERROR
        if str(order.tracking_number or "").strip() != str(
            payload.get("tracking_number") or ""
        ).strip():
            return TRACKING_NUMBER_CHANGED_ERROR
    if (
        kind == IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED
        and not nova_poshta_order_fulfillment_confirmed(order)
    ):
        return CARRIER_DELIVERY_NOT_CONFIRMED_ERROR
    if (
        kind == IgLifecycleEvent.Kind.TTN_CREATED
        and str(order.tracking_number or "").strip()
        != str(payload.get("tracking_number") or "").strip()
    ):
        return TRACKING_NUMBER_CHANGED_ERROR
    if not assignment_matches:
        return STALE_ASSIGNMENT_ERROR
    return ""


def _lifecycle_quick_replies(event: IgLifecycleEvent) -> tuple:
    if event.kind != IgLifecycleEvent.Kind.PARCEL_ARRIVED:
        return ()
    try:
        from management.services.ig_postback_router import parcel_quick_replies

        return parcel_quick_replies(event.order_id)
    except Exception:
        logger.exception(
            "Unable to build parcel quick replies for lifecycle event %s", event.pk
        )
        return ()


def _post_purchase_cancellation_reason(event, order) -> str:
    if event.kind != IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED:
        return ""
    return post_purchase_invitation_block_reason(
        event.client, order, event.payload, _base_message(event), event.locale,
        event.final_text,
    )


def _preflight_cancellation_reason(event: IgLifecycleEvent) -> str:
    payment_truth = (
        IgPaymentProjection.objects.filter(
            deal_id=event.deal_id,
            client_id=event.client_id,
        )
        .values_list("truth", flat=True)
        .first()
    )
    order = Order.objects.filter(pk=event.order_id).first()
    reason = _business_truth_cancellation_reason(
        kind=event.kind,
        payload=event.payload or {},
        payment_truth=payment_truth,
        order=order,
        assignment_matches=_assignment_matches_event(event),
    )
    return reason or _post_purchase_cancellation_reason(event, order)


def _mark_event_ambiguous(event_id: int, reason: str, *, lease: str) -> str:
    with transaction.atomic():
        event = IgLifecycleEvent.objects.select_for_update().get(pk=event_id)
        if event.state in {
            IgLifecycleEvent.State.SENT,
            IgLifecycleEvent.State.CANCELLED,
        }:
            return event.state
        if (
            event.state != IgLifecycleEvent.State.PROCESSING
            or event.lease_token != lease
        ):
            return event.state
        lifecycle_message = (
            _lifecycle_message_queryset(event.event_key).select_for_update().first()
        )
        event.state = IgLifecycleEvent.State.AMBIGUOUS
        event.lease_token = ""
        event.lease_expires_at = None
        event.last_error = reason[:1000]
        event.due_at = timezone.now()
        event.save(
            update_fields=[
                "state",
                "lease_token",
                "lease_expires_at",
                "last_error",
                "due_at",
                "updated_at",
            ]
        )
        _mark_lifecycle_message_unknown(lifecycle_message)
    _project_order_channel(event)
    _queue_manager_task(event)
    _notify_lifecycle_delivery_review(event)
    return event.state


def _start_lifecycle_provider_io(
    event_id: int,
    lease: str,
    *,
    deal_id: int,
    order_id: int,
    client_id: int,
    assignment_id: int | None,
    assignment_version: int | None,
) -> bool:
    """Commit the lifecycle outbox marker in canonical lock order."""
    with transaction.atomic():
        projection = (
            IgPaymentProjection.objects.select_for_update()
            .filter(deal_id=deal_id, client_id=client_id)
            .first()
        )
        locked_order = Order.objects.select_for_update().filter(pk=order_id).first()
        assignment_matches = False
        if assignment_id and assignment_version:
            assignment_matches = (
                IgOrderAssignment.objects.select_for_update()
                .filter(
                    pk=assignment_id,
                    order_id=order_id,
                    client_id=client_id,
                    version=assignment_version,
                    unassigned_at__isnull=True,
                )
                .exists()
            )
        current_event = (
            IgLifecycleEvent.objects.select_for_update()
            .filter(
                pk=event_id,
                lease_token=lease,
                state=IgLifecycleEvent.State.PROCESSING,
            )
            .first()
        )
        if current_event is None:
            return False
        cancellation_reason = _business_truth_cancellation_reason(
            kind=current_event.kind,
            payload=current_event.payload or {},
            payment_truth=projection.truth if projection is not None else None,
            order=locked_order,
            assignment_matches=assignment_matches,
        )
        cancellation_reason = cancellation_reason or _post_purchase_cancellation_reason(
            current_event, locked_order,
        )
        if cancellation_reason:
            current_event.state = IgLifecycleEvent.State.CANCELLED
            current_event.lease_token = ""
            current_event.lease_expires_at = None
            current_event.last_error = cancellation_reason
            current_event.save(
                update_fields=[
                    "state",
                    "lease_token",
                    "lease_expires_at",
                    "last_error",
                    "updated_at",
                ]
            )
            return False
        fresh_now = timezone.now()
        fresh_client = (
            IgClient.objects.only("last_message_at")
            .filter(pk=client_id)
            .first()
        )
        if not _response_window_open(fresh_client, fresh_now):
            current_event.state = IgLifecycleEvent.State.MANAGER_REVIEW
            current_event.lease_token = ""
            current_event.lease_expires_at = None
            current_event.last_error = STANDARD_RESPONSE_WINDOW_CLOSED
            current_event.due_at = fresh_now
            current_event.save(
                update_fields=[
                    "state",
                    "lease_token",
                    "lease_expires_at",
                    "last_error",
                    "due_at",
                    "updated_at",
                ]
            )
            transaction.on_commit(
                lambda event_id=current_event.pk: _publish_lifecycle_window_review(
                    event_id
                )
            )
            return False
        lifecycle_message = (
            _lifecycle_message_queryset(current_event.event_key)
            .select_for_update()
            .first()
        )
        expected_text = _message(current_event)
        if lifecycle_message is not None and (
            lifecycle_message.client_id != current_event.client_id
            or lifecycle_message.sender_id != current_event.client.igsid
            or lifecycle_message.text != expected_text
        ):
            current_event.state = IgLifecycleEvent.State.AMBIGUOUS
            current_event.lease_token = ""
            current_event.lease_expires_at = None
            current_event.last_error = "lifecycle outbox identity mismatch"
            current_event.due_at = timezone.now()
            current_event.save(
                update_fields=[
                    "state",
                    "lease_token",
                    "lease_expires_at",
                    "last_error",
                    "due_at",
                    "updated_at",
                ]
            )
            _mark_lifecycle_message_unknown(lifecycle_message)
            return False
        if _lifecycle_message_has_provider_io(lifecycle_message):
            current_event.state = IgLifecycleEvent.State.AMBIGUOUS
            current_event.lease_token = ""
            current_event.lease_expires_at = None
            current_event.last_error = "provider I/O marker already exists"
            current_event.due_at = timezone.now()
            current_event.save(
                update_fields=[
                    "state",
                    "lease_token",
                    "lease_expires_at",
                    "last_error",
                    "due_at",
                    "updated_at",
                ]
            )
            _mark_lifecycle_message_unknown(lifecycle_message)
            return False
        started_at = timezone.now()
        if lifecycle_message is None:
            InstagramBotMessage.objects.create(
                sender_id=current_event.client.igsid,
                client_id=current_event.client_id,
                role=InstagramBotMessage.Role.MODEL,
                text=expected_text,
                status=InstagramBotMessage.Status.PROCESSING,
                source="lifecycle",
                synthetic_event_key=_lifecycle_message_key(current_event.event_key),
                send_state="sending",
                send_started_at=started_at,
            )
        else:
            InstagramBotMessage.objects.filter(pk=lifecycle_message.pk).update(
                status=InstagramBotMessage.Status.PROCESSING,
                send_state="sending",
                send_started_at=started_at,
                send_completed_at=None,
                processed_at=None,
            )
    return True


@contextmanager
def _lifecycle_provider_request_boundary(
    event_id: int,
    lease: str,
    *,
    deal_id: int,
    order_id: int,
    client_id: int,
    assignment_id: int | None,
    assignment_version: int | None,
    delivered_chunk_count: int = 0,
    provider_message_ids: tuple[str, ...] = (),
    planned_chunk_count: int = 0,
):
    """Lock and revalidate business truth around one bounded Meta request."""
    projected_event = None
    needs_manager_review = False
    window_review_required = False
    with transaction.atomic():
        projection = (
            IgPaymentProjection.objects.select_for_update()
            .filter(deal_id=deal_id, client_id=client_id)
            .first()
        )
        locked_order = Order.objects.select_for_update().filter(pk=order_id).first()
        assignment = None
        if assignment_id and assignment_version:
            assignment = (
                IgOrderAssignment.objects.select_for_update()
                .filter(
                    pk=assignment_id,
                    order_id=order_id,
                    client_id=client_id,
                    version=assignment_version,
                    unassigned_at__isnull=True,
                )
                .first()
            )
        current_event = (
            IgLifecycleEvent.objects.select_for_update()
            .filter(
                pk=event_id,
                lease_token=lease,
                state=IgLifecycleEvent.State.PROCESSING,
            )
            .first()
        )
        if current_event is None:
            yield False
        else:
            fresh_client = (
                IgClient.objects.only("last_message_at")
                .filter(pk=client_id)
                .first()
            )
            window_closed = not _response_window_open(
                fresh_client,
                timezone.now(),
            )
            lifecycle_message = (
                _lifecycle_message_queryset(current_event.event_key)
                .select_for_update()
                .first()
            )
            cancellation_reason = _business_truth_cancellation_reason(
                kind=current_event.kind,
                payload=current_event.payload or {},
                payment_truth=projection.truth if projection is not None else None,
                order=locked_order,
                assignment_matches=assignment is not None,
            )
            cancellation_reason = cancellation_reason or _post_purchase_cancellation_reason(
                current_event, locked_order,
            )
            marker_identity_matches = bool(
                lifecycle_message
                and lifecycle_message.client_id == current_event.client_id
                and lifecycle_message.sender_id == current_event.client.igsid
                and lifecycle_message.text == _message(current_event)
                and _lifecycle_message_has_provider_io(lifecycle_message)
            )
            terminal_reason = cancellation_reason or (
                STANDARD_RESPONSE_WINDOW_CLOSED if window_closed else ""
            )
            if not terminal_reason and not marker_identity_matches:
                terminal_reason = "lifecycle outbox marker missing or mismatched"
                needs_manager_review = True

            if terminal_reason:
                receipt_ids = normalize_provider_message_ids(
                    provider_message_ids
                )
                delivered_count = max(
                    len(receipt_ids),
                    max(0, int(delivered_chunk_count or 0)),
                )
                planned_count = max(
                    delivered_count,
                    max(0, int(planned_chunk_count or 0)),
                )
                failure_boundary = (
                    f"chunk:{delivered_count + 1}:provider_request_rejected"
                )[:64]
                if receipt_ids:
                    current_event.provider_message_id = receipt_ids[0][:128]
                has_partial_delivery = delivered_count > 0
                if window_closed and not has_partial_delivery and not needs_manager_review:
                    current_event.state = IgLifecycleEvent.State.MANAGER_REVIEW
                    current_event.last_error = STANDARD_RESPONSE_WINDOW_CLOSED
                    current_event.due_at = timezone.now()
                    window_review_required = True
                    if lifecycle_message is not None:
                        completed_at = timezone.now()
                        InstagramBotMessage.objects.filter(
                            pk=lifecycle_message.pk
                        ).update(
                            status=InstagramBotMessage.Status.DONE,
                            send_state="cancelled",
                            processed_at=completed_at,
                            send_completed_at=completed_at,
                            delivery_original_text=lifecycle_message.text,
                            delivery_planned_chunk_count=planned_count,
                            delivery_delivered_chunk_count=0,
                            delivery_provider_message_ids=[],
                            delivery_failure_boundary=failure_boundary,
                        )
                elif needs_manager_review or has_partial_delivery:
                    current_event.state = IgLifecycleEvent.State.AMBIGUOUS
                    current_event.last_error = (
                        f"partial delivery before {terminal_reason}"
                        if has_partial_delivery
                        else terminal_reason
                    )[:1000]
                    current_event.due_at = timezone.now()
                    needs_manager_review = True
                    if lifecycle_message is not None:
                        InstagramBotMessage.objects.filter(
                            pk=lifecycle_message.pk
                        ).update(
                            delivery_original_text=lifecycle_message.text,
                            delivery_planned_chunk_count=planned_count,
                            delivery_delivered_chunk_count=delivered_count,
                            delivery_provider_message_ids=list(receipt_ids),
                            delivery_failure_boundary=failure_boundary,
                            provider_message_id=(
                                receipt_ids[0][:255] if receipt_ids else ""
                            ),
                        )
                    _mark_lifecycle_message_unknown(lifecycle_message)
                else:
                    current_event.state = IgLifecycleEvent.State.CANCELLED
                    current_event.last_error = terminal_reason[:1000]
                    if lifecycle_message is not None:
                        completed_at = timezone.now()
                        InstagramBotMessage.objects.filter(
                            pk=lifecycle_message.pk
                        ).update(
                            status=InstagramBotMessage.Status.DONE,
                            send_state="cancelled",
                            processed_at=completed_at,
                            send_completed_at=completed_at,
                            delivery_original_text=lifecycle_message.text,
                            delivery_planned_chunk_count=planned_count,
                            delivery_delivered_chunk_count=0,
                            delivery_provider_message_ids=[],
                            delivery_failure_boundary=failure_boundary,
                        )
                current_event.lease_token = ""
                current_event.lease_expires_at = None
                current_event.save(
                    update_fields=[
                        "state",
                        "lease_token",
                        "lease_expires_at",
                        "provider_message_id",
                        "last_error",
                        "due_at",
                        "updated_at",
                    ]
                )
                projected_event = current_event
                yield False
            else:
                # The transaction remains open while the single provider request
                # is in flight, so these exact rows cannot change after validation.
                yield True

    if projected_event is not None:
        _project_order_channel(projected_event)
        if window_review_required:
            _queue_manager_task(projected_event)
            _notify_lifecycle_window_review(projected_event)
        elif needs_manager_review:
            _queue_manager_task(projected_event)
            _notify_lifecycle_delivery_review(projected_event)


@contextmanager
def _lifecycle_follow_provider_request_boundary(
    authorized_follow,
    base_text: str,
    *,
    event_id: int,
    lease: str,
    deal_id: int,
    order_id: int,
    client_id: int,
    assignment_id: int | None,
    assignment_version: int | None,
    delivered_chunk_count: int = 0,
    provider_message_ids: tuple[str, ...] = (),
    planned_chunk_count: int = 0,
):
    """Hold lifecycle truth and optional follow truth for one Meta request.

    The follow clause is optional. If it becomes invalid after lifecycle
    authorization, its boundary supplies the immutable base reply as a safe
    replacement while the mandatory lifecycle boundary remains active.
    """
    with _lifecycle_provider_request_boundary(
        event_id,
        lease,
        deal_id=deal_id,
        order_id=order_id,
        client_id=client_id,
        assignment_id=assignment_id,
        assignment_version=assignment_version,
        delivered_chunk_count=delivered_chunk_count,
        provider_message_ids=provider_message_ids,
        planned_chunk_count=planned_chunk_count,
    ) as lifecycle_allowed:
        if not lifecycle_allowed:
            yield lifecycle_allowed
            return

        from management.services.instagram_bot import ProviderRequestBoundaryResult

        if authorized_follow is None:
            if not base_text:
                yield lifecycle_allowed
                return
            _replace_lifecycle_message_snapshot(event_id, lease, base_text)
            yield ProviderRequestBoundaryResult(
                allowed=False,
                replacement_text=base_text,
                reason="follow_decision_not_active",
            )
            return

        from management.services.ig_follow_cta import follow_provider_request_boundary

        with follow_provider_request_boundary(
            authorized_follow,
            now=timezone.now(),
        ) as follow_allowed:
            if follow_allowed:
                yield True
                return
            replacement_text = str(
                getattr(follow_allowed, "replacement_text", "") or ""
            ).strip() or base_text
            if replacement_text:
                _replace_lifecycle_message_snapshot(
                    event_id,
                    lease,
                    replacement_text,
                )
                yield ProviderRequestBoundaryResult(
                    allowed=False,
                    replacement_text=replacement_text,
                    reason=(
                        str(getattr(follow_allowed, "reason", "") or "").strip()
                        or "follow_provider_boundary_rejected"
                    ),
                )
            else:
                yield follow_allowed


def dispatch_lifecycle_event(event_id: int) -> str:
    """Lease and deliver one event; return its durable state value."""
    now = timezone.now()
    lease = secrets.token_hex(24)
    claimed = False
    with transaction.atomic():
        event = (
            IgLifecycleEvent.objects.select_for_update()
            .filter(pk=event_id)
            .first()
        )
        if event is None:
            return "missing"
        if event.state == IgLifecycleEvent.State.SENT:
            return event.state
        ambiguous_event = None
        if event.state in {
            IgLifecycleEvent.State.CANCELLED,
            IgLifecycleEvent.State.MANAGER_REVIEW,
            IgLifecycleEvent.State.AMBIGUOUS,
            IgLifecycleEvent.State.FAILED,
        }:
            return event.state
        if (
            event.state == IgLifecycleEvent.State.PROCESSING
            and event.lease_expires_at
            and event.lease_expires_at > now
        ):
            return event.state
        lifecycle_message = (
            _lifecycle_message_queryset(event.event_key).select_for_update().first()
        )
        provider_io_started = _lifecycle_message_has_provider_io(lifecycle_message)
        if provider_io_started:
            was_processing = event.state == IgLifecycleEvent.State.PROCESSING
            event.state = IgLifecycleEvent.State.AMBIGUOUS
            event.lease_token = ""
            event.lease_expires_at = None
            event.last_error = (
                LEASE_EXPIRED_AFTER_PROVIDER_IO_ERROR
                if was_processing
                else PROVIDER_MARKER_BEFORE_LEASE_ERROR
            )
            event.due_at = now
            event.save(
                update_fields=[
                    "state",
                    "lease_token",
                    "lease_expires_at",
                    "last_error",
                    "due_at",
                    "updated_at",
                ]
            )
            _mark_lifecycle_message_unknown(lifecycle_message, when=now)
            ambiguous_event = event
        elif event.state == IgLifecycleEvent.State.PROCESSING:
            if event.lease_expires_at is None:
                event.state = IgLifecycleEvent.State.AMBIGUOUS
                event.lease_token = ""
                event.last_error = LEASE_WITHOUT_EXPIRY_ERROR
                event.due_at = now
                event.save(
                    update_fields=[
                        "state",
                        "lease_token",
                        "last_error",
                        "due_at",
                        "updated_at",
                    ]
                )
                ambiguous_event = event
            elif event.last_error != PROVIDER_BOUNDARY_CLAIM_MARKER:
                event.state = IgLifecycleEvent.State.AMBIGUOUS
                event.lease_token = ""
                event.lease_expires_at = None
                event.last_error = LEGACY_LEASE_MARKER_ERROR
                event.due_at = now
                event.save(
                    update_fields=[
                        "state",
                        "lease_token",
                        "lease_expires_at",
                        "last_error",
                        "due_at",
                        "updated_at",
                    ]
                )
                ambiguous_event = event
            else:
                # The durable marker is committed immediately before the first
                # provider HTTP call. Its absence proves this lease never
                # crossed the delivery boundary, so reclaim cannot duplicate.
                event.lease_token = lease
                event.lease_expires_at = now + LEASE_DURATION
                event.attempts += 1
                event.last_error = PROVIDER_BOUNDARY_CLAIM_MARKER
                event.save(
                    update_fields=[
                        "lease_token",
                        "lease_expires_at",
                        "attempts",
                        "last_error",
                        "updated_at",
                    ]
                )
                claimed = True
        elif event.due_at > now:
            return event.state
        else:
            event.state = IgLifecycleEvent.State.PROCESSING
            event.lease_token = lease
            event.lease_expires_at = now + LEASE_DURATION
            event.attempts += 1
            event.last_error = PROVIDER_BOUNDARY_CLAIM_MARKER
            event.save(
                update_fields=[
                    "state",
                    "lease_token",
                    "lease_expires_at",
                    "attempts",
                    "last_error",
                    "updated_at",
                ]
            )
            claimed = True

    event = (
        IgLifecycleEvent.objects.select_related("client", "order")
        .filter(pk=event_id)
        .first()
    )
    if event is None:
        return "missing"
    if claimed:
        lifecycle_message = _lifecycle_message_queryset(event.event_key).first()
        if _lifecycle_message_has_provider_io(lifecycle_message):
            return _mark_event_ambiguous(
                event_id,
                PROVIDER_MARKER_BEFORE_LEASE_ERROR,
                lease=lease,
            )
        cancellation_reason = _preflight_cancellation_reason(event)
        if cancellation_reason:
            return _cancel_event(event_id, cancellation_reason, lease=lease)
    if ambiguous_event is not None:
        event = (
            IgLifecycleEvent.objects.select_related("client", "order")
            .filter(pk=event_id)
            .first()
        ) or event
        _project_order_channel(ambiguous_event)
        _queue_manager_task(ambiguous_event)
        _notify_lifecycle_delivery_review(ambiguous_event)
        return ambiguous_event.state

    try:
        from management.models import InstagramBotSettings
        from management.services.instagram_bot import send_text
        from management.services.ig_reply_boundary import (
            customer_send_boundary,
            reply_execution_boundary,
        )

        settings = InstagramBotSettings.load()
        with reply_execution_boundary(settings.pk, event.client_id) as permission:
            # Э0.4: единый объект решения. При выключенном флаге здесь не
            # выполняется ни одного лишнего запроса и ни одной записи, а
            # решают ветви ниже — они же остаются финальной revalidation даже
            # когда решает политика.
            from management.services.ig_outgoing_gate import (
                evaluate as _policy_evaluate,
                policy_enabled as _policy_enabled,
            )

            policy_decision = None
            policy_decides = False
            if _policy_enabled():
                policy_decision, policy_decides = _policy_evaluate(
                    _lifecycle_outgoing_request(event, permission, settings=settings),
                    now=now,
                    legacy_decision=_legacy_lifecycle_verdict(
                        event, permission, now=now
                    ),
                )
            if policy_decides and policy_decision is not None:
                policy_state = _apply_lifecycle_policy_decision(
                    event,
                    event_id,
                    policy_decision,
                    permission,
                    lease=lease,
                    now=now,
                )
                if policy_state is not None:
                    return policy_state

            if not permission:
                if permission.reason in TRANSIENT_PERMISSION_REASONS:
                    return _defer_event_for_permission(
                        event_id,
                        permission.reason,
                        lease=lease,
                    )
                return _cancel_event(
                    event_id,
                    permission.reason or CUSTOMER_SEND_NOT_ALLOWED_ERROR,
                    lease=lease,
                )

            if not _response_window_open(event.client, now):
                return _escalate_event_to_manager_review(
                    event,
                    event_id,
                    reason=STANDARD_RESPONSE_WINDOW_CLOSED,
                    lease=lease,
                    now=now,
                )

            cancellation_reason = _preflight_cancellation_reason(event)
            if cancellation_reason:
                return _cancel_event(event_id, cancellation_reason, lease=lease)

            # Optional follow copy is fully local at dispatch. Any Meta/Gemini
            # preparation belongs to a prior worker; this boundary only
            # authorizes an already prepared decision and freezes the exact
            # text before the first provider request.
            final_text = materialize_lifecycle_follow_text(event)
            event = _persist_lifecycle_final_text(event.pk, final_text)
            authorized_follow, follow_base_text = _lifecycle_follow_snapshot(event)

            payload = event.payload or {}
            try:
                assignment_id = int(payload.get("assignment_id"))
                assignment_version = int(payload.get("assignment_version"))
            except (TypeError, ValueError):
                assignment_id = assignment_version = None
            result = send_text(
                settings,
                event.client.igsid,
                event.final_text or _base_message(event),
                # «Посилка у відділенні» — єдина подія, де від клієнта потрібна
                # ДІЯ, а не інформація. Кнопка «Забрав» / «Нагадати пізніше»
                # робить відповідь одним натисканням і, головне, саме натискання
                # переоткриває 24-годинне вікно Meta — тому наступне нагадування
                # легальне без окремого протоколу згоди.
                quick_replies=_lifecycle_quick_replies(event),
                permission_boundary_factory=lambda: customer_send_boundary(
                    settings.pk, event.client_id, permission
                ),
                provider_io_started_callback=lambda: _start_lifecycle_provider_io(
                    event_id,
                    lease,
                    deal_id=event.deal_id,
                    order_id=event.order_id,
                    client_id=event.client_id,
                    assignment_id=assignment_id,
                    assignment_version=assignment_version,
                ),
                provider_request_boundary_factory=lambda *, delivered_chunk_count=0,
                provider_message_ids=(), planned_chunk_count=0: (
                    _lifecycle_follow_provider_request_boundary(
                        authorized_follow,
                        follow_base_text,
                        event_id=event_id,
                        lease=lease,
                        deal_id=event.deal_id,
                        order_id=event.order_id,
                        client_id=event.client_id,
                        assignment_id=assignment_id,
                        assignment_version=assignment_version,
                        delivered_chunk_count=delivered_chunk_count,
                        provider_message_ids=provider_message_ids,
                        planned_chunk_count=planned_chunk_count,
                    )
                ),
                provider_message_callback=lambda message_id: (
                    _checkpoint_lifecycle_provider_receipt(
                        event_id,
                        lease,
                        message_id,
                    )
                ),
                return_receipt=True,
            )
            delivery = _delivery_result(result)
    except Exception as exc:
        delivery = _delivery_result_after_provider_exception(event_id, exc)

    ok = delivery.ok
    kind = delivery.kind
    hint = delivery.hint
    provider_message_id = delivery.provider_message_id
    transient_permission_reason = _transient_permission_reason(
        kind,
        hint,
        delivery.failure_boundary,
    )
    receipt_incomplete = bool(
        ok
        and delivery.receipt_present
        and (
            not delivery.provider_message_ids
            or len(delivery.provider_message_ids) < delivery.delivered_chunk_count
            or delivery.delivered_chunk_count < delivery.planned_chunk_count
        )
    )
    if receipt_incomplete:
        ok = False
        kind = "unknown"
        hint = PROVIDER_MESSAGE_ID_MISSING_ERROR
    needs_manager_review = (not ok and kind in {"unknown", "transient", "permanent"})
    permission_review_required = False
    with transaction.atomic():
        owned = IgLifecycleEvent.objects.select_for_update().get(pk=event_id)
        lease_lost = owned.lease_token != lease
        if not lease_lost:
            lifecycle_message = (
                _lifecycle_message_queryset(owned.event_key).select_for_update().first()
            )
            provider_io_started = _lifecycle_message_has_provider_io(lifecycle_message)
            owned.lease_token = ""
            owned.lease_expires_at = None
            if provider_message_id:
                owned.provider_message_id = provider_message_id[:128]
            if ok and provider_message_id:
                owned.state = IgLifecycleEvent.State.SENT
                owned.completed_at = timezone.now()
                owned.last_error = ""
            elif ok:
                owned.state = IgLifecycleEvent.State.AMBIGUOUS
                owned.last_error = PROVIDER_MESSAGE_ID_MISSING_ERROR
                owned.due_at = timezone.now() + timedelta(hours=6)
            elif provider_io_started:
                owned.state = IgLifecycleEvent.State.AMBIGUOUS
                owned.last_error = (
                    f"{PROVIDER_IO_STARTED_PREFIX}{kind}:{hint}"[:1000]
                )
                owned.due_at = timezone.now() + timedelta(hours=6)
            elif transient_permission_reason:
                permission_review_required = _apply_permission_deferral(
                    owned,
                    transient_permission_reason,
                    now=timezone.now(),
                )
            elif kind == "cancelled":
                owned.state = IgLifecycleEvent.State.CANCELLED
                owned.last_error = f"{kind}:{hint}"[:1000]
            elif kind == "retryable" and owned.attempts < 3:
                owned.state = IgLifecycleEvent.State.PENDING
                owned.due_at = timezone.now() + timedelta(minutes=2 ** owned.attempts)
                owned.last_error = f"{kind}:{hint}"[:1000]
            elif kind in {"unknown", "transient"}:
                owned.state = IgLifecycleEvent.State.AMBIGUOUS
                owned.last_error = f"{kind}:{hint}"[:1000]
                owned.due_at = timezone.now() + timedelta(hours=6)
            else:
                owned.state = IgLifecycleEvent.State.FAILED
                owned.last_error = f"{kind}:{hint}"[:1000]
            if lifecycle_message is not None:
                message_updates = {
                    "delivery_original_text": lifecycle_message.text,
                    "delivery_planned_chunk_count": delivery.planned_chunk_count,
                    "delivery_delivered_chunk_count": delivery.delivered_chunk_count,
                    "delivery_provider_message_ids": list(
                        delivery.provider_message_ids
                    ),
                    "delivery_failure_boundary": (
                        delivery.failure_boundary
                        or (
                            f"chunk:{delivery.delivered_chunk_count + 1}:provider_message_id_missing"
                            if receipt_incomplete
                            else ""
                        )
                    )[:64],
                }
                if ok and provider_message_id:
                    completed_at = owned.completed_at or timezone.now()
                    message_updates.update(
                        status=InstagramBotMessage.Status.DONE,
                        send_state="sent",
                        send_completed_at=completed_at,
                        processed_at=completed_at,
                        provider_message_id=provider_message_id[:255],
                    )
                elif provider_io_started:
                    message_updates.update(
                        status=InstagramBotMessage.Status.FAILED,
                        send_state="unknown",
                        processed_at=timezone.now(),
                    )
                InstagramBotMessage.objects.filter(pk=lifecycle_message.pk).update(
                    **message_updates
                )
            owned.save(update_fields=[
                "state", "lease_token", "lease_expires_at", "provider_message_id",
                "attempts", "completed_at", "last_error", "due_at", "updated_at",
            ])
        final_state = owned.state
    _project_order_channel(owned)
    _finalize_lifecycle_follow_decision(event_id, final_state)
    if lease_lost:
        if final_state in {
            IgLifecycleEvent.State.AMBIGUOUS,
            IgLifecycleEvent.State.FAILED,
        }:
            _queue_manager_task(owned)
            _notify_lifecycle_delivery_review(owned)
        return final_state
    if needs_manager_review or final_state in {
        IgLifecycleEvent.State.AMBIGUOUS,
        IgLifecycleEvent.State.FAILED,
    }:
        _queue_manager_task(owned)
        _notify_lifecycle_delivery_review(owned)
    elif permission_review_required:
        _queue_manager_task(owned)
        _notify_lifecycle_permission_review(owned, transient_permission_reason)
    return final_state


def dispatch_due_lifecycle_events(limit: int = 50) -> int:
    """Bounded recovery hook for cron/daemon workers after request loss."""
    now = timezone.now()
    event_ids = list(
        IgLifecycleEvent.objects.filter(
            due_at__lte=now,
        ).filter(
            Q(state__in=[
                IgLifecycleEvent.State.PENDING,
                IgLifecycleEvent.State.WAITING_WINDOW,
            ])
            | Q(
                state=IgLifecycleEvent.State.PROCESSING,
                lease_expires_at__isnull=False,
                lease_expires_at__lte=now,
            )
            | Q(
                state=IgLifecycleEvent.State.PROCESSING,
                lease_expires_at__isnull=True,
            )
        ).order_by("due_at", "id").values_list("id", flat=True)[:limit]
    )
    delivered = 0
    for event_id in event_ids:
        if dispatch_lifecycle_event(event_id) == IgLifecycleEvent.State.SENT:
            delivered += 1
    return delivered
