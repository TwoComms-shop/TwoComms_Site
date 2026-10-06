"""Atomic current Instagram order ownership and its audit trail.

The immutable payment attribution remains the commercial history.  This
module owns the mutable operational projection used by the manager workspace
and customer-fulfillment workers.
"""

from __future__ import annotations

import uuid
from functools import wraps

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class OrderAssignmentError(ValueError):
    """Base error for a rejected assignment mutation."""


class AssignmentConflict(OrderAssignmentError):
    """The order has a different active Instagram owner."""


class AssignmentVersionConflict(OrderAssignmentError):
    """The manager action was based on a stale drawer version."""


class AssignmentNotFound(OrderAssignmentError):
    """No current assignment exists for the requested order."""


def _assignment_mutation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        from management.ig_bot_models import _ig_order_assignment_mutation_scope

        with _ig_order_assignment_mutation_scope():
            return function(*args, **kwargs)

    return wrapped


def _uuid(value):
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _actor_source(actor, requested):
    from management.ig_bot_models import IgOrderAssignmentEvent

    if requested:
        return requested
    return (
        IgOrderAssignmentEvent.ActorSource.MANAGEMENT_USER
        if actor is not None
        else IgOrderAssignmentEvent.ActorSource.AUTOMATION
    )


def _assignment_event_snapshot(assignment, *, reason_code, reason):
    return {
        "assignment_version": assignment.version,
        "source": assignment.source,
        "reason_code": reason_code or "",
        "reason": reason or "",
    }


def _replayed_event(operation_id):
    if operation_id is None:
        return None
    from management.ig_bot_models import IgOrderAssignmentEvent

    return (
        IgOrderAssignmentEvent.objects.select_related("assignment")
        .filter(operation_id=operation_id)
        .first()
    )


def _legacy_owner_conflict(order_id, client_id):
    """Reject stale legacy ownership before the projection is backfilled."""

    from management.ig_bot_models import (
        IgDeal,
        IgOrderAttribution,
        IgPaymentConfirmationReview,
    )

    attribution = (
        IgOrderAttribution.objects.select_for_update()
        .filter(order_id=order_id)
        .first()
    )
    if attribution and attribution.client_id != client_id:
        return True
    if IgDeal.objects.filter(order_id=order_id).exclude(client_id=client_id).exists():
        return True
    if IgPaymentConfirmationReview.objects.filter(order_id=order_id).exclude(client_id=client_id).exists():
        return True
    return False


def current_assignment_for_order(order):
    """Return the active projection, excluding an explicitly unlinked order."""

    from management.ig_bot_models import IgOrderAssignment

    order_id = getattr(order, "pk", order)
    return (
        IgOrderAssignment.objects.select_related("client", "assigned_by")
        .filter(order_id=order_id, client_id__isnull=False, unassigned_at__isnull=True)
        .first()
    )


def _locked_order(order):
    from orders.models import Order

    order_id = getattr(order, "pk", order)
    queryset = Order.objects.select_for_update()
    try:
        return queryset.get(pk=order_id)
    except (Order.DoesNotExist, ValueError, TypeError):
        return queryset.get(order_number=str(order_id).strip())


@transaction.atomic
@_assignment_mutation
def link_order_to_client(
    order,
    *,
    client,
    actor=None,
    source=None,
    actor_source=None,
    operation_id=None,
    expected_version=None,
    reason_code="",
    reason="",
):
    """Link one exact order to one Instagram client under a row lock.

    A repeated link to the same current client is intentionally a no-op.  A
    client-supplied operation id replays the original event and never creates a
    second audit row.
    """

    from management.ig_bot_models import (
        IgOrderAssignment,
        IgOrderAssignmentEvent,
    )

    operation_id = _uuid(operation_id)
    source = source or IgOrderAssignment.Source.MANAGER_MANUAL
    actor_source = _actor_source(actor, actor_source)
    locked_order = _locked_order(order)
    if locked_order.status == "cancelled":
        raise OrderAssignmentError("Скасоване замовлення не можна прив'язати")
    if client.hidden_at:
        raise OrderAssignmentError("Прихованого Instagram-клієнта не можна прив'язати")
    replay = _replayed_event(operation_id)
    if replay is not None:
        if replay.order_id != locked_order.pk:
            raise OrderAssignmentError("Operation id belongs to another order")
        if replay.kind not in {
            IgOrderAssignmentEvent.Kind.LINKED,
            IgOrderAssignmentEvent.Kind.AUTO_CONFIRMED,
        } or replay.to_client_id != client.pk:
            raise OrderAssignmentError("Operation id belongs to another assignment action")
        replayed_assignment = replay.assignment
        if (
            replayed_assignment.version != replay.assignment_version
            or replayed_assignment.client_id != client.pk
            or replayed_assignment.unassigned_at is not None
        ):
            raise AssignmentVersionConflict("Повторена операція вже не є поточною")
        return replayed_assignment

    assignment = (
        IgOrderAssignment.objects.select_for_update()
        .filter(order_id=locked_order.pk)
        .first()
    )
    if assignment is None:
        if expected_version is not None:
            raise AssignmentVersionConflict("Версія прив'язки вже змінилася")
        if _legacy_owner_conflict(locked_order.pk, client.pk):
            raise AssignmentConflict("Замовлення вже прив'язано до іншого Instagram-клієнта")
        assignment = IgOrderAssignment.objects.create(
            order=locked_order,
            client=client,
            source=source,
            assigned_by=actor,
            assigned_at=timezone.now(),
            version=1,
            last_reason_code=reason_code or "",
            last_reason=reason or "",
        )
        previous_client_id = None
    else:
        if expected_version is not None and assignment.version != int(expected_version):
            raise AssignmentVersionConflict("Версія прив'язки вже змінилася")
        if assignment.client_id and assignment.client_id != client.pk:
            raise AssignmentConflict("Замовлення вже прив'язано до іншого Instagram-клієнта")
        if assignment.client_id == client.pk and assignment.unassigned_at is None:
            transaction.on_commit(lambda order_id=locked_order.pk: _kick_fulfillment(order_id))
            return assignment
        previous_client_id = assignment.client_id
        assignment.client = client
        assignment.source = source
        assignment.assigned_by = actor
        assignment.assigned_at = timezone.now()
        assignment.unassigned_at = None
        assignment.version += 1
        assignment.last_reason_code = reason_code or ""
        assignment.last_reason = reason or ""
        assignment.save(update_fields=[
            "client",
            "source",
            "assigned_by",
            "assigned_at",
            "unassigned_at",
            "version",
            "last_reason_code",
            "last_reason",
            "updated_at",
        ])

    IgOrderAssignmentEvent.objects.create(
        operation_id=operation_id or uuid.uuid4(),
        assignment=assignment,
        order=locked_order,
        kind=(
            IgOrderAssignmentEvent.Kind.AUTO_CONFIRMED
            if actor_source == IgOrderAssignmentEvent.ActorSource.AUTOMATION
            else IgOrderAssignmentEvent.Kind.LINKED
        ),
        from_client_id=previous_client_id,
        to_client=client,
        actor=actor,
        actor_source=actor_source,
        assignment_source=source,
        reason_code=reason_code or "",
        reason=reason or "",
        assignment_version=assignment.version,
        snapshot=_assignment_event_snapshot(
            assignment,
            reason_code=reason_code,
            reason=reason,
        ),
    )
    _advance_stage_from_order(client, locked_order)
    transaction.on_commit(lambda order_id=locked_order.pk: _kick_fulfillment(order_id))
    return assignment


def _advance_stage_from_order(client, order) -> None:
    """Move the CRM stage when a real paid order becomes ours.

    F-STATE-009: the stage was only ever recomputed while classifying an inbound
    message, so an order paid on the website and linked by a manager left the
    client at `new`. On production that is exactly client #303: 3428 UAH paid,
    parcel shipped, stage `new`, readiness 0.

    Never regresses: a client already at `done` stays there.
    """
    from management.models import IgClient

    if not client or not getattr(client, "pk", None) or order is None:
        return
    from management.services.ig_order_links import order_fulfillment_payment_verified

    if not order_fulfillment_payment_verified(order):
        return
    target = (
        IgClient.Stage.DONE
        if str(getattr(order, "status", "") or "") == "done"
        else IgClient.Stage.ORDER_CREATED
    )
    order_index = list(IgClient.FUNNEL_ORDER)
    try:
        current_rank = [item.value for item in order_index].index(client.stage)
        target_rank = [item.value for item in order_index].index(target)
    except ValueError:
        current_rank, target_rank = -1, 0
    if current_rank >= target_rank:
        return
    try:
        client.set_stage(target, reason="order_linked")
    except Exception:
        logger.exception("Could not advance stage for client %s", client.pk)


@transaction.atomic
@_assignment_mutation
def unlink_order_from_client(
    order,
    *,
    client,
    actor=None,
    actor_source=None,
    operation_id=None,
    expected_version=None,
    reason_code="",
    reason="",
):
    """Clear the current owner without deleting projection or audit history."""

    from management.ig_bot_models import (
        IgOrderAssignment,
        IgOrderAssignmentEvent,
    )

    operation_id = _uuid(operation_id)
    actor_source = _actor_source(actor, actor_source)
    locked_order = _locked_order(order)
    replay = _replayed_event(operation_id)
    if replay is not None:
        if replay.order_id != locked_order.pk:
            raise OrderAssignmentError("Operation id belongs to another order")
        if replay.kind != IgOrderAssignmentEvent.Kind.UNLINKED:
            raise OrderAssignmentError("Operation id belongs to another assignment action")
        if replay.from_client_id != client.pk:
            raise OrderAssignmentError("Operation id belongs to another Instagram client")
        replayed_assignment = replay.assignment
        if (
            replayed_assignment.version != replay.assignment_version
            or replayed_assignment.client_id is not None
            or replayed_assignment.unassigned_at is None
        ):
            raise AssignmentVersionConflict("Повторена операція вже не є поточною")
        return replayed_assignment
    assignment = (
        IgOrderAssignment.objects.select_for_update()
        .filter(order_id=locked_order.pk)
        .first()
    )
    if assignment is None:
        raise AssignmentNotFound("У замовлення немає поточної прив'язки")
    if expected_version is not None and assignment.version != int(expected_version):
        raise AssignmentVersionConflict("Версія прив'язки вже змінилася")
    if assignment.client_id is None or assignment.unassigned_at is not None:
        transaction.on_commit(lambda order_id=locked_order.pk: _kick_fulfillment(order_id))
        return assignment
    if assignment.client_id != client.pk:
        raise AssignmentConflict("Замовлення прив'язано до іншого Instagram-клієнта")
    if not str(reason_code or "").strip() or not str(reason or "").strip():
        raise ValueError("Для відв'язки потрібні код і пояснення причини")

    previous_client_id = assignment.client_id
    assignment.client = None
    assignment.unassigned_at = timezone.now()
    assignment.version += 1
    assignment.last_reason_code = str(reason_code).strip()
    assignment.last_reason = str(reason).strip()
    assignment.save(update_fields=[
        "client",
        "unassigned_at",
        "version",
        "last_reason_code",
        "last_reason",
        "updated_at",
    ])
    from management.ig_bot_models import IgOrderCustomerEvent

    IgOrderCustomerEvent.objects.filter(
        assignment=assignment,
        assignment_version__lt=assignment.version,
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
        last_error="assignment was explicitly unlinked",
        updated_at=timezone.now(),
    )
    IgOrderAssignmentEvent.objects.create(
        operation_id=operation_id or uuid.uuid4(),
        assignment=assignment,
        order=locked_order,
        kind=IgOrderAssignmentEvent.Kind.UNLINKED,
        from_client_id=previous_client_id,
        to_client=None,
        actor=actor,
        actor_source=actor_source,
        assignment_source=assignment.source,
        reason_code=assignment.last_reason_code,
        reason=assignment.last_reason,
        assignment_version=assignment.version,
        snapshot=_assignment_event_snapshot(
            assignment,
            reason_code=assignment.last_reason_code,
            reason=assignment.last_reason,
        ),
    )
    transaction.on_commit(lambda order_id=locked_order.pk: _kick_fulfillment(order_id))
    return assignment


def _kick_fulfillment(order_id):
    """Import lazily so assignment mutations stay usable during migrations."""
    try:
        from management.services.ig_order_fulfillment import kick_order_fulfillment

        kick_order_fulfillment(order_id)
    except Exception:
        # The durable event can still be picked up by the reconciliation
        # command/cron; a wake-up failure must never roll back ownership.
        return None


def assignment_source_for_attribution(*, creation_mode, payment_source):
    from management.ig_bot_models import IgOrderAssignment

    if str(payment_source or "").startswith("provider_") or creation_mode == "provider_auto":
        return IgOrderAssignment.Source.PROVIDER_AUTO
    if creation_mode in {"checkout_auto", "direct_checkout"}:
        return IgOrderAssignment.Source.CHECKOUT_AUTO
    if creation_mode == "manager_review":
        return IgOrderAssignment.Source.MANAGER_PAYMENT_REVIEW
    return IgOrderAssignment.Source.MANAGER_MANUAL
