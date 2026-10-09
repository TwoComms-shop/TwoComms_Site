"""Durable Instagram Direct fulfillment notifications for assigned orders.

The assignment projection is the only source of current ownership.  This
worker deliberately creates a new event for every assignment version so an
unlink/relink cannot replay a message to the previous customer.
"""
from __future__ import annotations

import logging
import hashlib
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import BigIntegerField, Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from orders.fulfillment_truth import (
    NOVA_POSHTA_DELIVERY_SUCCESS_CODES,
    nova_poshta_order_fulfillment_confirmed,
)
from management.services.ig_order_links import order_fulfillment_payment_verified
from management.services.ig_delivery_receipts import (
    normalize_provider_message_id,
    normalize_provider_message_ids,
)
from management.services.ig_post_purchase_invitation import (
    INVITATION_PAYLOAD_KEY,
    post_purchase_invitation_block_reason,
    post_purchase_invitation_snapshot,
    post_purchase_invitation_text,
)

logger = logging.getLogger(__name__)

LEASE_SECONDS = 300
RETRY_MINUTES = 15
# F-OPS-005: нетерминальное состояние без верхней границы — это тихая потеря.
# Двенадцать часов и 40 попыток дают менеджеру рабочий день, после чего событие
# становится его задачей, а не бесконечным ретраем.
MAX_WAITING_ATTEMPTS = 40
MAX_WAITING_DURATION = timedelta(hours=12)
RESPONSE_WINDOW = timedelta(hours=23)
SUPERSEDED_ERROR = "superseded by current order fulfillment state"
CANONICAL_LIFECYCLE_ERROR = (
    "canonical Instagram lifecycle event owns this automated checkout"
)
_CANONICAL_LIFECYCLE_ASSIGNMENT_SOURCES = frozenset(
    {
        "provider_auto",
        "checkout_auto",
        "manager_payment_review",
    }
)


def _locale(client) -> str:
    language = str(getattr(client, "language", "") or "").lower()
    if language.startswith("en"):
        return "en"
    if language.startswith("ru"):
        return "ru"
    return "uk"


def _message(kind: str, locale: str, order, tracking: str, *, exchange_size: str = "") -> str:
    number = order.order_number or str(order.pk)
    tracking_url = f"https://novaposhta.ua/tracking/?cargo_number={tracking}"
    if kind == "exchange_shipped":
        size = str(exchange_size or "").strip()
        if locale == "en":
            what = f"Your exchange for size {size}" if size else "Your exchange"
            return (
                f"A tracking number has been created for {what.lower()}. "
                f"New Nova Poshta tracking number: {tracking}. "
                f"Estimated delivery: 1-3 business days. Track it here: {tracking_url}."
            )
        if locale == "ru":
            what = f"Замена на размер {size}" if size else "Замена"
            return (
                f"Создана ТТН для: {what}. "
                f"Новый номер ТТН Новой Почты: {tracking}. "
                "Ориентировочный срок доставки: 1-3 рабочих дня. "
                f"Отследить: {tracking_url}."
            )
        what = f"Заміна на розмір {size}" if size else "Заміна"
        return (
            f"Створено ТТН для: {what}. "
            f"Нова ТТН Нової Пошти: {tracking}. "
            "Орієнтовний термін доставки: 1-3 робочі дні. "
            f"Відстежити: {tracking_url}."
        )
    if kind == "payment_confirmed":
        from management.services.ig_order_amounts import order_amounts

        payable = order_amounts(order)["payable"]
        payload = getattr(order, "payment_payload", None)
        payload = payload if isinstance(payload, dict) else {}
        if payload.get("manager_verification_scope") == "prepayment" or getattr(order, "payment_status", "") in {"prepaid", "partial"}:
            raw_paid = payload.get("manager_confirmed_amount") or payload.get("paid_value")
            if not raw_paid and callable(getattr(order, "get_prepayment_amount", None)):
                raw_paid = order.get_prepayment_amount()
            try:
                paid = Decimal(str(raw_paid or 0)).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                paid = Decimal("0.00")
            remaining = max(payable - paid, Decimal("0.00"))
            if locale == "en":
                return f"Prepayment received, thank you: {paid} UAH. Order #{number}: {payable} UAH, remaining: {remaining} UAH. We will send the tracking number when it is created."
            if locale == "ru":
                return f"Предоплата получена, спасибо: {paid} грн. Заказ №{number}: {payable} грн, остаток: {remaining} грн. Пришлём номер ТТН после её создания."
            return f"Передоплату отримали, дякуємо: {paid} грн. Замовлення №{number}: {payable} грн, залишок: {remaining} грн. Надішлемо номер ТТН після її створення."
        if locale == "en":
            return (
                f"Payment received, thank you. Order #{number}, {payable} UAH. "
                "We are packing it now and will send the tracking number as soon "
                "as the parcel is handed over. If anything needs changing, reply "
                "here within the next couple of hours."
            )
        if locale == "ru":
            return (
                f"Оплата получена, спасибо. Заказ №{number}, {payable} грн. "
                "Собираем и пришлём номер ТТН сразу после отправки. "
                "Если нужно что-то поменять — напишите здесь в ближайшие пару часов."
            )
        return (
            f"Оплату отримали, дякуємо. Замовлення №{number}, {payable} грн. "
            "Збираємо і надішлемо номер ТТН відразу після відправки. "
            "Якщо потрібно щось змінити — напишіть тут найближчі пару годин."
        )
    if kind == "ttn_assigned":
        if locale == "en":
            return (
                f"Nova Poshta tracking number for order #{number} has been created: {tracking}. "
                f"Estimated delivery time is 1-3 business days. Track its status here: {tracking_url}"
            )
        if locale == "ru":
            return (
                f"Создана ТТН Новой Почты для заказа №{number}: {tracking}. "
                f"Ориентировочный срок доставки - 1-3 рабочих дня. "
                f"Следить за статусом: {tracking_url}"
            )
        return (
            f"Створено ТТН Нової Пошти для замовлення №{number}: {tracking}. "
            f"Орієнтовний термін доставки - 1-3 робочі дні. "
            f"Стежити за статусом: {tracking_url}"
        )
    return post_purchase_invitation_text(locale, order)


def _uses_canonical_lifecycle(assignment) -> bool:
    """Return whether the assignment has the full assisted-checkout context.

    The assignment source alone is not sufficient: legacy provider and
    manager-review links use the same source values but have no checkout
    proposal, so their fulfillment messages must remain on this worker.
    """
    if str(getattr(assignment, "source", "") or "") not in _CANONICAL_LIFECYCLE_ASSIGNMENT_SOURCES:
        return False

    from management.models import IgCheckoutProposal, IgOrderAttribution

    proposal = (
        IgCheckoutProposal.objects.filter(payment_attempt__order_id=assignment.order_id)
        .values("client_id", "deal_id")
        .first()
    )
    if not proposal or proposal["client_id"] != assignment.client_id:
        return False
    return IgOrderAttribution.objects.filter(
        order_id=assignment.order_id,
        client_id=proposal["client_id"],
        deal_id=proposal["deal_id"],
    ).exists()


def _cancel_redundant_events(assignment, *, now):
    from management.ig_bot_models import IgOrderCustomerEvent

    active_states = (
        IgOrderCustomerEvent.State.PENDING,
        IgOrderCustomerEvent.State.FAILED,
        IgOrderCustomerEvent.State.WAITING_WINDOW,
    )
    return IgOrderCustomerEvent.objects.filter(
        assignment_id=assignment.pk,
        state__in=active_states,
    ).update(
        state=IgOrderCustomerEvent.State.CANCELLED,
        lease_token="",
        lease_expires_at=None,
        last_error=CANONICAL_LIFECYCLE_ERROR,
        due_at=now,
        updated_at=now,
    )


def _exchange_replacement(order, tracking: str):
    """Return the journal row proving this tracking number is an exchange leg."""
    from management.ig_bot_models import IgOrderShipment

    return (
        IgOrderShipment.objects.filter(
            order_id=order.pk,
            tracking_number=tracking,
            direction=IgOrderShipment.Direction.OUTBOUND,
            purpose=IgOrderShipment.Purpose.EXCHANGE_REPLACEMENT,
        )
        .select_related("post_sale_case")
        .first()
    )


def _event_specs(assignment, *, now):
    order = assignment.order
    client = assignment.client
    locale = _locale(client)
    tracking = str(order.tracking_number or "").strip()
    # IMP-021: подтверждение оплаты идёт тем же durable-путём, что и ТТН, —
    # там уже есть идемпотентность по event_key, guard'ы, lease и локализация.
    # Оно не зависит от того, сформулирует ли модель нужную фразу.
    #
    # Только до появления ТТН: как только посылка уехала, клиенту нужен номер
    # для отслеживания, а «оплату отримали» постфактум читается как сбой
    # автоматики. Один шаг воронки — одно сообщение.
    if (
        not tracking
        and order_fulfillment_payment_verified(order)
        and order.status not in {"done", "cancelled"}
    ):
        yield {
            "kind": "payment_confirmed",
            "event_key": (
                f"ig-assignment:{assignment.pk}:v{assignment.version}"
                f":payment:{order.payment_status}"
            ),
            "message": _message("payment_confirmed", locale, order, tracking),
            "payload": {
                "order_number": order.order_number or str(order.pk),
                "payment_status": str(order.payment_status or ""),
            },
        }
    if tracking and order.status not in {"done", "cancelled"}:
        # Exactly one outbound kind per current tracking number, so a replacement
        # never produces both «замовлення відправлено» and «заміну відправлено».
        replacement = _exchange_replacement(order, tracking)
        if replacement is not None:
            case = replacement.post_sale_case
            size = str(getattr(case, "requested_size", "") or "")
            yield {
                "kind": "exchange_shipped",
                "event_key": (
                    f"ig-assignment:{assignment.pk}:v{assignment.version}"
                    f":exchange-ttn:{tracking}"
                ),
                "message": _message(
                    "exchange_shipped", locale, order, tracking, exchange_size=size
                ),
                "payload": {
                    "tracking_number": tracking,
                    "tracking_url": f"https://novaposhta.ua/tracking/?cargo_number={tracking}",
                    "post_sale_case_id": replacement.post_sale_case_id,
                    "exchange_size": size,
                },
            }
        else:
            yield {
                "kind": "ttn_assigned",
                "event_key": f"ig-assignment:{assignment.pk}:v{assignment.version}:ttn:{tracking}",
                "message": _message("ttn_assigned", locale, order, tracking),
                "payload": {"tracking_number": tracking, "tracking_url": f"https://novaposhta.ua/tracking/?cargo_number={tracking}"},
            }
    if nova_poshta_order_fulfillment_confirmed(order):
        invitation, message = post_purchase_invitation_snapshot(
            client, order, locale, assignment=assignment,
        )
        yield {
            "kind": "delivered_review",
            "event_key": f"ig-assignment:{assignment.pk}:v{assignment.version}:delivered-review",
            "message": message,
            "payload": {
                INVITATION_PAYLOAD_KEY: invitation,
                "order_number": order.order_number or str(order.pk),
                "tracking_number": tracking,
                "tracking_status_code": int(order.tracking_status_code),
                "tracking_terminal_at": order.tracking_terminal_at.isoformat(),
            },
        }


def ensure_assignment_events(assignment, *, now=None, queue_consent=True):
    """Materialize all currently eligible durable events for one assignment."""
    from management.ig_bot_models import IgOrderCustomerEvent

    now = now or timezone.now()
    if not assignment.client_id or assignment.unassigned_at is not None:
        return []
    if queue_consent:
        from management.services.ig_marketing_consent import queue_post_purchase_consent

        queue_post_purchase_consent(assignment.client, assignment.order, assignment, now=now)
    if _uses_canonical_lifecycle(assignment):
        _cancel_redundant_events(assignment, now=now)
        return []
    if assignment.version == 1 and assignment.last_reason_code == "legacy_attribution":
        return []
    created = []
    for spec in _event_specs(assignment, now=now):
        event, was_created = IgOrderCustomerEvent.objects.get_or_create(
            event_key=spec["event_key"],
            defaults={
                "assignment": assignment,
                "assignment_version": assignment.version,
                "order": assignment.order,
                "client": assignment.client,
                "kind": spec["kind"],
                "locale": _locale(assignment.client),
                "message_snapshot": spec["message"],
                "payload": spec["payload"],
                "due_at": now,
            },
        )
        if was_created:
            created.append(event)
    return created


def _queue_assignment_consent_batch(assignments, *, limit, now):
    """Bound invitation owners independently from legacy event creation counts.

    Canonical assignments legitimately create zero legacy events. Already
    served invitation scopes are excluded in SQL so the earliest canonical
    order cannot occupy every later producer batch.
    """
    bounded = max(0, min(int(limit), 100))
    if not bounded or not getattr(settings, "IG_POST_PURCHASE_BUSINESS_CONSENT_ENABLED", False):
        return {"consent_considered": 0, "consent_queued": 0}
    from management.ig_consent_models import IgMarketingConsentInvitation
    from management.models import IgFunnelResetAudit, InstagramBotMessage, InstagramBotSettings
    from management.services.ig_marketing_consent import PURPOSE, queue_post_purchase_consent
    from management.services.instagram_bot import allowed_sender_ids, ingress_provider_namespace

    settings_obj = InstagramBotSettings.objects.order_by("pk").first()
    namespace = ingress_provider_namespace(settings_obj) if settings_obj else ""
    if settings_obj is None or not settings_obj.is_enabled or not namespace:
        return {"consent_considered": 0, "consent_queued": 0}
    latest_reset = IgFunnelResetAudit.objects.filter(client_id=OuterRef("client_id")).order_by("-pk")
    rows = assignments.filter(order__payment_status="paid",
        client__privacy_erasure_started_at__isnull=True, client__hidden_at__isnull=True,
        client__is_blocked=False, client__bot_paused=False, client__manager_takeover=False,
        client__opted_out_at__isnull=True,
    ).exclude(order__status="cancelled").annotate(
        _consent_reset_id=Coalesce(Subquery(latest_reset.values("pk")[:1]), Value(0), output_field=BigIntegerField()),
        _consent_reset_boundary=Coalesce(Subquery(latest_reset.values("reset_after_message_id")[:1]), Value(0), output_field=BigIntegerField()),
    )
    allowed_senders = allowed_sender_ids(settings_obj)
    if allowed_senders:
        rows = rows.filter(client__igsid__in=allowed_senders)
    current_source = InstagramBotMessage.objects.filter(
        client_id=OuterRef("client_id"), sender_id=OuterRef("client__igsid"),
        role="user", source__in=["webhook", "poll"], provider_namespace=namespace,
        pk__gt=OuterRef("_consent_reset_boundary"),
        provider_created_at__gt=now - RESPONSE_WINDOW, provider_created_at__lte=now,
    ).exclude(mid__isnull=True).exclude(mid="").exclude(status="failed")
    existing = IgMarketingConsentInvitation.objects.filter(
        client_id=OuterRef("client_id"), order_id=OuterRef("order_id"), assignment_id=OuterRef("pk"),
        assignment_version=OuterRef("version"), reset_audit_id=OuterRef("_consent_reset_id"), purpose=PURPOSE,
    )
    eligible = rows.annotate(_consent_has_source=Exists(current_source), _consent_exists=Exists(existing))
    eligible = eligible.filter(_consent_has_source=True, _consent_exists=False).order_by("pk")
    # This cursor controls fairness only. Cached data never admits a purchase
    # or source: the selected row still passes the real locked queue owner.
    cursor_key = "ig:consent-producer:v1:" + hashlib.sha256(
        f"{settings_obj.pk}:{namespace}".encode()).hexdigest()
    try:
        cursor = max(0, int(cache.get(cursor_key) or 0))
    except Exception:
        cursor = 0
    selected = list(eligible.filter(pk__gt=cursor)[:bounded])
    if not selected and cursor:
        selected = list(eligible.filter(pk__lte=cursor)[:bounded])
    queued = 0
    for assignment in selected:
        try:
            invitation = queue_post_purchase_consent(assignment.client, assignment.order, assignment, now=now)
        finally:
            # Permanent source/payment refusals must also relinquish the head
            # of the advisory scan. Cache outages keep the strict owner path.
            try:
                cache.set(cursor_key, assignment.pk, timeout=24 * 60 * 60)
            except Exception:
                pass
        queued += int(invitation is not None)
    return {"consent_considered": len(selected), "consent_queued": queued}


def _claim_event(event_id, *, now):
    from management.ig_bot_models import IgOrderCustomerEvent

    token = uuid.uuid4().hex
    with transaction.atomic():
        event = IgOrderCustomerEvent.objects.select_for_update().select_related(
            "assignment", "order", "client"
        ).get(pk=event_id)
        if event.state in {
            IgOrderCustomerEvent.State.SENT,
            IgOrderCustomerEvent.State.CANCELLED,
            IgOrderCustomerEvent.State.MANAGER_REVIEW,
            IgOrderCustomerEvent.State.AMBIGUOUS,
        }:
            return None
        if event.due_at > now:
            return None
        if event.state == IgOrderCustomerEvent.State.PROCESSING:
            if event.lease_expires_at and event.lease_expires_at > now:
                return None
            event.state = IgOrderCustomerEvent.State.AMBIGUOUS
            event.lease_token = ""
            event.lease_expires_at = None
            event.last_error = (
                "processing lease expired; delivery outcome requires manager review"
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
            return event
        event.state = IgOrderCustomerEvent.State.PROCESSING
        event.attempts += 1
        event.lease_token = token
        event.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        event.last_error = ""
        event.save(update_fields=["state", "attempts", "lease_token", "lease_expires_at", "last_error", "updated_at"])
        return event


def _finish(
    event,
    *,
    token,
    state,
    now,
    error="",
    due_at=None,
    provider_message_id="",
):
    from management.ig_bot_models import IgOrderCustomerEvent

    updates = dict(
        state=state,
        lease_token="",
        lease_expires_at=None,
        last_error=(error or "")[:1000],
        due_at=due_at or now,
        completed_at=now if state == IgOrderCustomerEvent.State.SENT else None,
        updated_at=now,
    )
    receipt_id = normalize_provider_message_id(provider_message_id)
    if receipt_id:
        updates["provider_message_id"] = receipt_id
    updated = IgOrderCustomerEvent.objects.filter(
        pk=event.pk,
        lease_token=token,
    ).update(**updates)
    return bool(updated)


def _checkpoint_provider_receipt(event_id, token, provider_message_id):
    """Persist one Meta receipt while the legacy delivery lease is owned."""
    from management.ig_bot_models import IgOrderCustomerEvent

    receipt_id = normalize_provider_message_id(provider_message_id)
    if not receipt_id:
        raise ValueError("provider receipt checkpoint requires a message ID")
    with transaction.atomic():
        event = (
            IgOrderCustomerEvent.objects.select_for_update()
            .filter(
                pk=event_id,
                lease_token=token,
                state=IgOrderCustomerEvent.State.PROCESSING,
            )
            .first()
        )
        if event is None:
            raise RuntimeError("order customer event lease lost before receipt checkpoint")
        receipt_ids = list(normalize_provider_message_ids([
            *(event.delivery_provider_message_ids or []),
            receipt_id,
        ]))
        IgOrderCustomerEvent.objects.filter(pk=event.pk).update(
            provider_message_id=receipt_ids[0],
            delivery_provider_message_ids=receipt_ids,
        )


def _active_opt_out(client) -> bool:
    return bool(
        client.opted_out_at
        and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
    )


def _inside_response_window(client, *, now) -> bool:
    return bool(
        client.meta_window_anchor
        and now <= client.meta_window_anchor + RESPONSE_WINDOW
    )


def _matches_current_fulfillment(event, order) -> bool:
    """Return whether the durable snapshot still matches the live order."""
    tracking = str(getattr(order, "tracking_number", "") or "").strip()
    if event.kind in {"ttn_assigned", "exchange_shipped"}:
        event_tracking = str((event.payload or {}).get("tracking_number") or "").strip()
        return bool(
            order.status not in {"done", "cancelled"}
            and tracking
            and event_tracking == tracking
        )
    if event.kind == "payment_confirmed":
        # Появившаяся ТТН делает неотправленное подтверждение оплаты устаревшим:
        # теперь актуально сообщение с номером, а не с фактом оплаты.
        return bool(
            not tracking
            and order.status not in {"done", "cancelled"}
            and order_fulfillment_payment_verified(order)
        )
    if event.kind == "delivered_review":
        return nova_poshta_order_fulfillment_confirmed(order)
    return False


@contextmanager
def _event_send_boundary(
    event,
    *,
    token,
    settings_id,
    permission,
    now,
    boundary_state,
):
    """Revalidate and lock current ownership immediately around Meta I/O."""
    from management.ig_bot_models import IgOrderAssignment, IgOrderCustomerEvent
    from management.services.ig_reply_boundary import customer_send_boundary
    from orders.models import Order

    with customer_send_boundary(settings_id, event.client_id, permission) as permitted:
        if not permitted:
            yield False
            return
        with transaction.atomic():
            order = (
                Order.objects.select_for_update()
                .filter(pk=event.order_id)
                .first()
            )
            assignment = (
                IgOrderAssignment.objects.select_for_update()
                .select_related("client")
                .filter(pk=event.assignment_id)
                .first()
            )
            current_event = (
                IgOrderCustomerEvent.objects.select_for_update()
                .filter(
                    pk=event.pk,
                    lease_token=token,
                    state=IgOrderCustomerEvent.State.PROCESSING,
                )
                .first()
            )
            client = assignment.client if assignment else None
            fulfillment_current = bool(
                current_event
                and order
                and _matches_current_fulfillment(current_event, order)
            )
            if current_event and order and not fulfillment_current:
                boundary_state["superseded"] = True
            canonical_handoff = bool(
                current_event
                and assignment
                and _uses_canonical_lifecycle(assignment)
            )
            if canonical_handoff:
                boundary_state["canonical_handoff"] = True
            invitation_reason = ""
            if current_event and order and client and current_event.kind == "delivered_review":
                invitation_reason = post_purchase_invitation_block_reason(
                    client, order, current_event.payload,
                    current_event.message_snapshot, current_event.locale,
                    assignment=assignment,
                )
                boundary_state["invitation_reason"] = invitation_reason
            eligible_without_window = bool(
                current_event
                and assignment
                and fulfillment_current
                and not canonical_handoff
                and not invitation_reason
                and assignment.client_id == event.client_id
                and assignment.unassigned_at is None
                and assignment.version == event.assignment_version
                and client
                and not client.hidden_at
                and not client.is_blocked
                and not _active_opt_out(client)
            )
            fresh_now = timezone.now()
            window_open = bool(
                client and _inside_response_window(client, now=fresh_now)
            )
            if eligible_without_window and not window_open:
                boundary_state["window_closed"] = True
                current_event.state = IgOrderCustomerEvent.State.MANAGER_REVIEW
                current_event.lease_token = ""
                current_event.lease_expires_at = None
                current_event.last_error = "standard response window is closed"
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
            current = eligible_without_window and window_open
            yield current


def deliver_event(event_id, *, send=True, now=None):
    """Claim and optionally deliver one event; safe to retry after crashes."""
    from management.ig_bot_models import IgOrderCustomerEvent
    from management.models import InstagramBotSettings

    now = now or timezone.now()
    settings_obj = InstagramBotSettings.load()
    if not settings_obj.is_enabled:
        return "paused"
    event = _claim_event(event_id, now=now)
    if event is None:
        return "skipped"
    if event.state == IgOrderCustomerEvent.State.AMBIGUOUS:
        return "ambiguous"
    token = event.lease_token
    event = (
        IgOrderCustomerEvent.objects.select_related("assignment", "order", "client")
        .get(pk=event.pk)
    )
    assignment = event.assignment
    client = event.client
    if _uses_canonical_lifecycle(assignment):
        _finish(
            event,
            token=token,
            state=IgOrderCustomerEvent.State.CANCELLED,
            now=now,
            error=CANONICAL_LIFECYCLE_ERROR,
        )
        return "cancelled"
    if (
        not assignment.client_id
        or assignment.client_id != client.pk
        or assignment.unassigned_at is not None
        or assignment.version != event.assignment_version
    ):
        _finish(event, token=token, state=IgOrderCustomerEvent.State.CANCELLED, now=now, error="assignment is no longer current")
        return "cancelled"
    if not _matches_current_fulfillment(event, event.order):
        _finish(
            event,
            token=token,
            state=IgOrderCustomerEvent.State.CANCELLED,
            now=now,
            error=SUPERSEDED_ERROR,
        )
        return "cancelled"
    if client.hidden_at or client.is_blocked or _active_opt_out(client):
        _finish(event, token=token, state=IgOrderCustomerEvent.State.CANCELLED, now=now, error="client hidden or opted out")
        return "cancelled"
    if event.kind == "delivered_review":
        invitation_reason = post_purchase_invitation_block_reason(
            client, event.order, event.payload, event.message_snapshot, event.locale,
            assignment=assignment,
        )
        if invitation_reason:
            _finish(event, token=token, state=IgOrderCustomerEvent.State.CANCELLED,
                    now=now, error=invitation_reason)
            return "cancelled"
    if client.bot_paused or client.manager_takeover:
        # F-OPS-005: раньше здесь был безусловный ретрай каждые 15 минут без
        # верхней границы. На проде событие с ТТН провисело 53 попытки (~13 ч),
        # клиент оплатил 3428 грн и номер не получил, а состояние
        # `waiting_window` выглядело как «всё под контролем».
        stuck_too_long = bool(
            event.attempts >= MAX_WAITING_ATTEMPTS
            or (now - event.created_at) >= MAX_WAITING_DURATION
        )
        if stuck_too_long:
            _finish(
                event,
                token=token,
                state=IgOrderCustomerEvent.State.MANAGER_REVIEW,
                now=now,
                error=(
                    "менеджер тримає діалог довше "
                    f"{int(MAX_WAITING_DURATION.total_seconds() // 3600)} год — "
                    "надішліть ТТН вручну"
                ),
            )
            return "manager_review"
        _finish(event, token=token, state=IgOrderCustomerEvent.State.WAITING_WINDOW, now=now, due_at=now + timedelta(minutes=RETRY_MINUTES), error="manager currently owns the conversation")
        return "paused"
    if not send:
        _finish(event, token=token, state=IgOrderCustomerEvent.State.PENDING, now=now)
        return "planned"
    if not _inside_response_window(client, now=now):
        _finish(
            event,
            token=token,
            state=IgOrderCustomerEvent.State.MANAGER_REVIEW,
            now=now,
            error="standard response window is closed",
        )
        return "manager_review"
    from management.services import instagram_bot
    from management.services.ig_reply_boundary import capture_reply_permission

    permission = capture_reply_permission(settings_obj.pk, client.pk)
    provider_message_ids = []
    boundary_state = {
        "superseded": False,
        "canonical_handoff": False,
        "window_closed": False,
        "invitation_reason": "",
    }

    def checkpoint_provider_receipt(message_id):
        _checkpoint_provider_receipt(event.pk, token, message_id)
        provider_message_ids.append(normalize_provider_message_id(message_id))

    ok, kind, hint = instagram_bot.send_text(
        settings_obj,
        client.igsid,
        event.message_snapshot,
        permission_boundary_factory=lambda: _event_send_boundary(
            event,
            token=token,
            settings_id=settings_obj.pk,
            permission=permission,
            now=now,
            boundary_state=boundary_state,
        ),
        provider_message_callback=checkpoint_provider_receipt,
        allow_url_fallback=True,
    )
    provider_message_id = provider_message_ids[0] if provider_message_ids else ""
    if boundary_state["window_closed"]:
        return "manager_review"
    if ok and provider_message_id:
        finished = _finish(
            event,
            token=token,
            state=IgOrderCustomerEvent.State.SENT,
            now=now,
            provider_message_id=provider_message_id,
        )
        return "sent" if finished else "ambiguous"
    if ok:
        _finish(
            event,
            token=token,
            state=IgOrderCustomerEvent.State.AMBIGUOUS,
            now=now,
            error="Meta returned success without a provider message_id",
        )
        return "ambiguous"
    if kind in {"unknown", "transient"}:
        _finish(
            event,
            token=token,
            state=IgOrderCustomerEvent.State.AMBIGUOUS,
            now=now,
            error=hint,
            provider_message_id=provider_message_id,
        )
        return "ambiguous"
    if kind == "cancelled":
        if boundary_state["invitation_reason"]:
            error = boundary_state["invitation_reason"]
            if provider_message_ids:
                _finish(event, token=token, state=IgOrderCustomerEvent.State.AMBIGUOUS,
                        now=now, error=f"partial delivery before {error}",
                        provider_message_id=provider_message_id)
                return "ambiguous"
        elif boundary_state["canonical_handoff"]:
            error = CANONICAL_LIFECYCLE_ERROR
        elif boundary_state["superseded"]:
            error = SUPERSEDED_ERROR
        else:
            error = hint
        _finish(event, token=token, state=IgOrderCustomerEvent.State.CANCELLED, now=now, error=error)
        return "cancelled"
    if kind in {"permanent", "policy", "link_restricted"}:
        _finish(event, token=token, state=IgOrderCustomerEvent.State.MANAGER_REVIEW, now=now, error=hint)
        return "manager_review"
    _finish(event, token=token, state=IgOrderCustomerEvent.State.FAILED, now=now, due_at=now + timedelta(minutes=RETRY_MINUTES), error=hint)
    return "failed"


def reconcile_order_customer_events(*, order_id=None, limit=100, send=True, now=None):
    """Materialize and process eligible assignment notifications."""
    from management.ig_bot_models import IgOrderAssignment, IgOrderCustomerEvent

    now = now or timezone.now()
    assignments = IgOrderAssignment.objects.filter(
        client__isnull=False,
        unassigned_at__isnull=True,
    ).select_related("order", "client")
    if order_id is not None:
        assignments = assignments.filter(order_id=order_id)
    stats = {
        "created": 0,
        "sent": 0,
        "failed": 0,
        "cancelled": 0,
        "ambiguous": 0,
        "manager_review": 0,
        "paused": 0,
        "skipped": 0,
    }
    stats.update(_queue_assignment_consent_batch(assignments, limit=limit, now=now))
    materialized = 0
    for assignment in assignments.order_by("id").iterator(chunk_size=200):
        created = len(ensure_assignment_events(assignment, now=now, queue_consent=False))
        stats["created"] += created
        if created:
            materialized += 1
            if materialized >= limit:
                break

    invalid_delivery_reviews = IgOrderCustomerEvent.objects.filter(
        kind=IgOrderCustomerEvent.Kind.DELIVERED_REVIEW,
        state__in=(
            IgOrderCustomerEvent.State.PENDING,
            IgOrderCustomerEvent.State.WAITING_WINDOW,
        ),
    ).filter(
        ~Q(order__status="done")
        | Q(order__tracking_number__isnull=True)
        | Q(order__tracking_number="")
        | ~Q(order__tracking_status_code__in=NOVA_POSHTA_DELIVERY_SUCCESS_CODES)
        | Q(order__tracking_terminal_at__isnull=True)
    )
    if order_id is not None:
        invalid_delivery_reviews = invalid_delivery_reviews.filter(order_id=order_id)
    stats["cancelled"] += invalid_delivery_reviews.update(
        state=IgOrderCustomerEvent.State.CANCELLED,
        lease_token="",
        lease_expires_at=None,
        last_error="carrier delivery not confirmed",
        due_at=now,
        updated_at=now,
    )

    active_events = IgOrderCustomerEvent.objects.exclude(
        state__in=(
            IgOrderCustomerEvent.State.SENT,
            IgOrderCustomerEvent.State.CANCELLED,
            IgOrderCustomerEvent.State.MANAGER_REVIEW,
            IgOrderCustomerEvent.State.AMBIGUOUS,
        )
    )
    if order_id is not None:
        active_events = active_events.filter(order_id=order_id)
    stale_fulfillment_ids = []
    for event in active_events.select_related("order").iterator(chunk_size=200):
        if not _matches_current_fulfillment(event, event.order):
            stale_fulfillment_ids.append(event.pk)
    if stale_fulfillment_ids:
        stats["cancelled"] += IgOrderCustomerEvent.objects.filter(
            pk__in=stale_fulfillment_ids,
        ).exclude(
            state__in=(
                IgOrderCustomerEvent.State.SENT,
                IgOrderCustomerEvent.State.CANCELLED,
                IgOrderCustomerEvent.State.MANAGER_REVIEW,
                IgOrderCustomerEvent.State.AMBIGUOUS,
                IgOrderCustomerEvent.State.PROCESSING,
            )
        ).update(
            state=IgOrderCustomerEvent.State.CANCELLED,
            lease_token="",
            lease_expires_at=None,
            last_error=SUPERSEDED_ERROR,
            due_at=now,
            updated_at=now,
        )
    ineligible = active_events.exclude(
        state=IgOrderCustomerEvent.State.PROCESSING,
    ).filter(
        Q(assignment__client_id__isnull=True)
        | Q(assignment__unassigned_at__isnull=False)
        | ~Q(assignment__client_id=F("client_id"))
        | ~Q(assignment__version=F("assignment_version"))
        | Q(client__hidden_at__isnull=False)
        | Q(client__is_blocked=True)
        | (
            Q(client__opted_out_at__isnull=False)
            & (
                Q(client__opted_in_at__isnull=True)
                | Q(client__opted_in_at__lt=F("client__opted_out_at"))
            )
        )
    )
    stats["cancelled"] += ineligible.update(
        state=IgOrderCustomerEvent.State.CANCELLED,
        lease_token="",
        lease_expires_at=None,
        last_error="assignment or customer is no longer eligible",
        due_at=now,
        updated_at=now,
    )

    expired_processing = active_events.filter(
        state=IgOrderCustomerEvent.State.PROCESSING,
    ).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
    )
    stats["ambiguous"] += expired_processing.update(
        state=IgOrderCustomerEvent.State.AMBIGUOUS,
        lease_token="",
        lease_expires_at=None,
        last_error="processing lease expired; delivery outcome requires manager review",
        due_at=now,
        updated_at=now,
    )

    if not send:
        return stats

    events = IgOrderCustomerEvent.objects.filter(
        state__in=(IgOrderCustomerEvent.State.PENDING, IgOrderCustomerEvent.State.FAILED, IgOrderCustomerEvent.State.WAITING_WINDOW),
        due_at__lte=now,
    ).order_by("due_at", "id")
    if order_id is not None:
        events = events.filter(order_id=order_id)
    for event in events[:limit]:
        result = deliver_event(event.pk, send=send, now=now)
        stats[result] = stats.get(result, 0) + 1
    return stats


def kick_order_fulfillment(order_id):
    """Compatibility hook; the guarded cron is the only reconciliation owner."""
    return None
