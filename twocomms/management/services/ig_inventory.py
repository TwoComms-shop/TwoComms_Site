"""MariaDB-safe inventory reservations for assisted checkout invoices."""
from __future__ import annotations

import hashlib
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone


class InventoryReservationError(ValueError):
    """A payable proposal cannot be created from an unavailable allocation."""

    def __init__(self, reason):
        self.reason = str(reason or "inventory_unavailable")[:128]
        super().__init__(self.reason)


def uncertain_inventory_reservation_q():
    """Potential bank effects and paid review cannot become free by ageing.

    Trusted terminalization explicitly changes reservation state to RELEASED.
    A provider dispatch digest remains durable evidence even if a local clock
    or generation label later changes; ACTIVE rows retain that obligation.
    """
    from management.models import IgCheckoutInventoryReservation as Reservation, IgCheckoutInvoiceGeneration as Generation
    effect = (Q(invoice_generation__isnull=False) & ~Q(invoice_generation__provider_request_digest="")) | Q(
        invoice_generation__state__in=[Generation.State.PROVIDER_INFLIGHT, Generation.State.PROVIDER_AMBIGUOUS,
            Generation.State.AMBIGUITY_REVIEW, Generation.State.LATE_PROVIDER_REVIEW,
            Generation.State.INVOICE_CREATED, Generation.State.WINNER_CLAIMED,
            Generation.State.PAID_WINNER, Generation.State.LATE_PAID_REVIEW, Generation.State.RESOURCE_REVIEW])
    return Q(state__in=[Reservation.State.ACTIVE, Reservation.State.OVERBOOKED_REVIEW]) & effect


def protected_inventory_reservation_q(at):
    from management.models import IgCheckoutInventoryReservation as Reservation
    return (Q(state=Reservation.State.ACTIVE, expires_at__gt=at)
        | Q(state=Reservation.State.PAID_COMMITTED) | uncertain_inventory_reservation_q())


def _fingerprint(proposal, allocation_key, *, generation=None):
    generation_identity = (
        f":generation:{generation.pk}"
        if generation is not None
        else ""
    )
    return hashlib.sha256(
        (
            f"ig-inventory:v2:{proposal.pk}:{proposal.revision}:"
            f"{allocation_key}{generation_identity}"
        ).encode()
    ).hexdigest()


def _allocation_for_item(item, *, lock=True):
    from management.services.ig_availability import AllocationSpec, resolve_allocation

    return resolve_allocation(
        AllocationSpec(
            product_id=item.product_id,
            color_variant_id=item.color_variant_id,
            size=item.size,
            fit_code=item.fit_code,
            quantity=int(item.quantity or 0),
        ),
        lock=lock,
    )


def _allocation_key(allocation):
    if allocation.source == "warehouse":
        return f"warehouse:stock_item:{allocation.stock_item_id}"
    if allocation.source == "catalog_variant":
        return f"catalog_variant:variant:{allocation.color_variant_id}"
    return "untracked"


def _lock_allocation(allocation):
    """Lock one allocation identity after all identities have been ordered.

    Resolving every line with ``SELECT FOR UPDATE`` in proposal-item order lets
    two baskets acquire the same rows in opposite orders.  Resolve first,
    then lock the stable identity order here to avoid MariaDB deadlocks.
    """
    if allocation.source == "warehouse":
        from warehouse.models import StockItem

        return StockItem.objects.select_for_update().filter(
            pk=allocation.stock_item_id,
        ).first()
    if allocation.source == "catalog_variant":
        from productcolors.models import ProductColorVariant

        return ProductColorVariant.objects.select_for_update().filter(
            pk=allocation.color_variant_id,
        ).first()
    return None


@transaction.atomic
def reserve_proposal_inventory(
    proposal,
    *,
    expires_at=None,
    require_policy=False,
    generation=None,
):
    from management.models import IgCheckoutInventoryReservation
    from product_catalog.models import ProductInventoryPolicy
    from warehouse.models import StockItem
    from productcolors.models import ProductColorVariant

    from management.services.ig_checkout_payment import _prelock_assisted_proposal
    assisted_owner = _prelock_assisted_proposal(proposal)
    proposal = type(proposal).objects.select_for_update().get(pk=proposal.pk)
    if assisted_owner is not None:
        proposal._ig_assisted_execution_id = assisted_owner.pk
    if generation is not None:
        from management.models import IgCheckoutInvoiceGeneration

        generation = IgCheckoutInvoiceGeneration.objects.select_for_update().get(
            pk=generation.pk,
            proposal_id=proposal.pk,
        )
    expiry = expires_at or (
        generation.expires_at if generation is not None else proposal.expires_at
    )
    from django.db import connection, OperationalError
    try:
        items = list(proposal.items.select_for_update(
            nowait=connection.features.has_select_for_update_nowait,
        ).order_by("pk"))
    except OperationalError as exc:
        if exc.args and exc.args[0] in {1205, 3572}:
            raise InventoryReservationError("inventory_allocation_busy") from exc
        raise

    groups = {}
    custom_rows = {}
    if assisted_owner is not None or any(item.product_id is None for item in items):
        from management.services.ig_checkout_payment import _assisted_proposal_validation, _assisted_custom_rows
        from management.services.ig_checkout_payment import CheckoutPaymentError
        try:
            assisted = _assisted_proposal_validation(proposal, fresh=True)
            custom_rows = _assisted_custom_rows(assisted)
        except CheckoutPaymentError as exc:
            raise InventoryReservationError(exc.code) from exc
    for item in items:
        if item.product_id is None:
            if item.pk not in custom_rows:
                raise InventoryReservationError("assisted_custom_resource_missing")
            # Finite physical approval is consumed by its OneToOne execution.
            # It is not a catalogue policy or a fabricated stock reservation.
            continue
        policy = ProductInventoryPolicy.objects.filter(product_id=item.product_id).first()
        if policy is None:
            if require_policy:
                raise InventoryReservationError("inventory_policy_missing")
            if not item.color_variant_id:
                continue
            # Legacy callers created reservations before ProductInventoryPolicy
            # existed. Keep that catalog-variant behavior explicit and isolated
            # from the new proposal path, which requires a policy row.
            from management.services.ig_availability import StockAllocation

            allocation = StockAllocation(
                source="catalog_variant",
                color_variant_id=item.color_variant_id,
                quantity=int(item.quantity or 0),
            )
        elif policy.source == ProductInventoryPolicy.Source.UNTRACKED:
            continue
        else:
            # Do not lock in proposal-item order.  The identities are locked in
            # deterministic order below after this first, non-locking pass.
            decision = _allocation_for_item(item, lock=False)
            if decision.status.value != "allocatable" or decision.allocation is None:
                raise InventoryReservationError(decision.reason)
            allocation = decision.allocation
        key = _allocation_key(allocation)
        group = groups.setdefault(
            # Physical warehouse ownership is the stock pool, even when two
            # distinct catalogue variants/products map to it. This identity
            # must equal the fingerprint's identity or bulk_create can emit
            # two same-generation rows with the same unique fingerprint.
            key,
            {
                "allocation": allocation,
                "allocation_key": key,
                "items": [],
                "quantity": 0,
            },
        )
        group["items"].append(item)
        group["quantity"] += int(item.quantity or 0)

    now = timezone.now()
    transferred_hold_ids = []
    for group in sorted(groups.values(), key=lambda value: (
        value["allocation"].source,
        value["allocation"].stock_item_id or value["allocation"].color_variant_id or 0,
    )):
        allocation = group["allocation"]
        required = group["quantity"]
        locked_target = _lock_allocation(allocation)
        # A source-bound offer may already hold this exact basket before its
        # first invoice generation exists. Transfer that finite hold under the
        # same proposal/allocation locks; counting it twice rejects the last
        # unit, while excluding every proposal row hides competing invoices.
        transfer_quantity = 0
        group_transfer_ids = []
        if generation is not None:
            hold = IgCheckoutInventoryReservation.objects.select_for_update().filter(
                reservation_fingerprint=_fingerprint(proposal, group["allocation_key"]),
                proposal=proposal, invoice_generation__isnull=True,
                state=IgCheckoutInventoryReservation.State.ACTIVE, expires_at__gt=now,
            ).first()
            if hold is not None:
                if (hold.allocation_key != group["allocation_key"] or hold.quantity != required
                    or sorted(hold.line_ids or []) != sorted(item.pk for item in group["items"])):
                    raise InventoryReservationError("inventory_proposal_hold_changed")
                transfer_quantity = int(hold.quantity)
                transferred_hold_ids.append(hold.pk)
                group_transfer_ids.append(hold.pk)
        if allocation.source == "warehouse":
            from warehouse.services.inventory import protected_stock_quantity

            stock_item = locked_target
            available = int(stock_item.quantity or 0) if stock_item else 0
            reserved = protected_stock_quantity(
                stock_item_id=allocation.stock_item_id,
                at=now,
                exclude_proposal_id=(proposal.pk if generation is None else None),
                exclude_generation_id=(
                    generation.pk if generation is not None else None
                ),
                lock=True,
            )
        else:
            variant = locked_target
            available = int(variant.stock or 0) if variant else 0
            reserved = _catalog_variant_protected_quantity(
                color_variant_id=allocation.color_variant_id, at=now,
                exclude_proposal_id=proposal.pk if generation is None else None,
                exclude_generation_id=generation.pk if generation is not None else None,
                lock=True,
            )
        reserved = int(reserved) - transfer_quantity
        if reserved < 0:
            raise InventoryReservationError("inventory_proposal_hold_changed")
        if available <= 0:
            raise InventoryReservationError("insufficient_reserved_stock")
        if int(reserved) + required > available:
            raise InventoryReservationError("insufficient_reserved_stock")
        if allocation.source == "catalog_variant":
            from management.services.ig_inventory_size_capacity import (
                SizeCapacityError, check_catalog_size_capacity,
            )
            try:
                check_catalog_size_capacity(
                    allocation.color_variant_id, group["items"], now=now,
                    exclude_proposal_id=proposal.pk if generation is None else None,
                    exclude_generation_id=generation.pk if generation is not None else None,
                    transfer_reservation_ids=group_transfer_ids,
                )
            except SizeCapacityError as exc:
                raise InventoryReservationError(exc.reason) from exc

    reservations = []
    for group in groups.values():
        allocation = group["allocation"]
        group_items = group["items"]
        fingerprint = _fingerprint(
            proposal,
            group["allocation_key"],
            generation=generation,
        )
        existing = IgCheckoutInventoryReservation.objects.select_for_update().filter(
            reservation_fingerprint=fingerprint,
        ).first()
        if existing is not None:
            if existing.state == IgCheckoutInventoryReservation.State.ACTIVE:
                existing.expires_at = expiry
                existing.quantity = group["quantity"]
                existing.line_ids = [item.pk for item in group_items]
                existing.save(update_fields=["expires_at", "quantity", "line_ids", "updated_at"])
            continue
        reservations.append(IgCheckoutInventoryReservation(
            proposal=proposal,
            invoice_generation=generation,
            item=group_items[0],
            product_id=group_items[0].product_id,
            color_variant_id=group_items[0].color_variant_id,
            allocation_source=allocation.source,
            stock_item_id=allocation.stock_item_id,
            allocation_key=group["allocation_key"],
            line_ids=[item.pk for item in group_items],
            quantity=group["quantity"],
            reservation_fingerprint=fingerprint,
            expires_at=expiry,
        ))
    if reservations:
        IgCheckoutInventoryReservation.objects.bulk_create(reservations)
    if transferred_hold_ids:
        IgCheckoutInventoryReservation.objects.filter(
            pk__in=transferred_hold_ids, state=IgCheckoutInventoryReservation.State.ACTIVE,
        ).update(state=IgCheckoutInventoryReservation.State.RELEASED,
            release_reason="transferred_to_invoice_generation", updated_at=now)
    return reservations


@transaction.atomic
def release_proposal_inventory(proposal, *, reason="released"):
    from management.models import IgCheckoutInventoryReservation

    now = timezone.now()
    return IgCheckoutInventoryReservation.objects.filter(
        proposal=proposal,
        state=IgCheckoutInventoryReservation.State.ACTIVE,
    ).exclude(uncertain_inventory_reservation_q()).update(
        state=IgCheckoutInventoryReservation.State.RELEASED,
        released_at=now,
        release_reason=str(reason or "released")[:128],
        updated_at=now,
    )


@transaction.atomic
def release_generation_inventory(generation, *, reason="released"):
    from management.models import (
        IgCheckoutInventoryReservation,
        IgCheckoutInvoiceGeneration,
    )

    generation = IgCheckoutInvoiceGeneration.objects.select_for_update().get(
        pk=generation.pk
    )
    attempt = generation.payment_attempt
    from management.services.ig_checkout_generation import (
        _provider_terminal_proof_matches, _cancelled_pre_dispatch_proof_matches,
        _create_refusal_proof_matches, _retry_source_is_safe,
    )
    terminal = generation.state in {generation.State.FAILED, generation.State.CANCELLED, generation.State.EXPIRED}
    no_effect = (terminal and not generation.provider_invoice_id and not attempt.monobank_invoice_id
        and not attempt.invoice_url and not (attempt.event_state or {}).get("invoice_creation_ambiguous")
        and not generation.provider_request_digest)
    proved = (_provider_terminal_proof_matches(generation, attempt, {"failure", "rejected", "reversed", "cancelled", "expired"})
        or _cancelled_pre_dispatch_proof_matches(generation, attempt)
        or _create_refusal_proof_matches(generation, attempt)) if terminal else False
    selected = IgCheckoutInvoiceGeneration.objects.filter(pk=generation.pk,
        proposal__assisted_commerce_execution__scope="partial").exists()
    ordinary_expired = (not selected and generation.state == generation.State.EXPIRED
        and _retry_source_is_safe(generation, attempt))
    if (generation.winner_slot == 1 or attempt.order_id or attempt.checkout_winner_claimed
        or Decimal(str(attempt.paid_amount or 0)) > 0 or not (no_effect or proved or ordinary_expired)):
        return 0
    now = timezone.now()
    return IgCheckoutInventoryReservation.objects.filter(
        invoice_generation_id=generation.pk,
        state=IgCheckoutInventoryReservation.State.ACTIVE,
    ).update(
        state=IgCheckoutInventoryReservation.State.RELEASED,
        released_at=now,
        release_reason=str(reason or "released")[:128],
        updated_at=now,
    )


@transaction.atomic
def release_attempt_inventory(attempt, *, reason="payment_terminal"):
    """Release only a proposal-backed attempt; retail attempts are untouched."""
    from orders.models import PaymentAttempt

    locked = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
    if locked.order_id or locked.status not in {
        PaymentAttempt.Status.FAILED,
        PaymentAttempt.Status.EXPIRED,
        PaymentAttempt.Status.CANCELLED,
    }:
        return 0
    try:
        generation = locked.instagram_checkout_generation
    except Exception:
        generation = None
    if generation is not None:
        return release_generation_inventory(generation, reason=reason)
    try:
        proposal = locked.instagram_checkout_proposal
    except Exception:
        return 0
    return release_proposal_inventory(proposal, reason=reason)


@transaction.atomic
def consume_proposal_inventory(proposal, *, order=None):
    """Consume legacy catalog reservations; warehouse rows commit only.

    The payment path calls :func:`commit_proposal_inventory` directly. This
    wrapper remains for older callers and preserves its idempotent return value.
    """
    result = commit_proposal_inventory(proposal, order=order)
    # Preserve the legacy helper's persisted value for older reconciliation
    # callers. Verified payment uses commit_proposal_inventory directly and
    # therefore records the new FULFILLED lifecycle state.
    from management.models import IgCheckoutInventoryReservation

    IgCheckoutInventoryReservation.objects.filter(
        proposal=proposal,
        allocation_source__in=["catalog_variant", ""],
        state=IgCheckoutInventoryReservation.State.FULFILLED,
    ).update(state=IgCheckoutInventoryReservation.State.CONSUMED)
    return result


def _catalog_variant_protected_quantity(
    *,
    color_variant_id,
    at,
    exclude_proposal_id=None,
    exclude_generation_id=None,
    lock=False,
):
    from management.models import IgCheckoutInventoryReservation

    reservations = IgCheckoutInventoryReservation.objects.filter(
        allocation_source__in=["catalog_variant", ""],
        color_variant_id=color_variant_id,
    ).filter(protected_inventory_reservation_q(at))
    if exclude_proposal_id is not None:
        reservations = reservations.exclude(proposal_id=exclude_proposal_id)
    if exclude_generation_id is not None:
        reservations = reservations.exclude(
            invoice_generation_id=exclude_generation_id
        )
    if lock:
        from django.db import connection, OperationalError
        try:
            # A proposal editor may hold its old reservations before it
            # reaches the stock parent. Never wait on that reverse edge.
            return sum(quantity - fulfilled for quantity, fulfilled in reservations.select_for_update(
                nowait=connection.features.has_select_for_update_nowait,
            ).order_by("pk").values_list("quantity", "fulfilled_quantity"))
        except OperationalError as exc:
            if exc.args and exc.args[0] in {1205, 3572}:
                raise InventoryReservationError("inventory_allocation_busy") from exc
            raise
    return int(reservations.aggregate(total=Sum(F("quantity") - F("fulfilled_quantity")))["total"] or 0)


def _payment_inventory_targets(candidate_rows):
    from productcolors.models import ProductColorVariant
    from warehouse.models import StockItem

    variant_ids = sorted(
        set(
            candidate_rows.filter(
                allocation_source__in=["catalog_variant", ""],
                color_variant_id__isnull=False,
            ).values_list("color_variant_id", flat=True)
        )
    )
    stock_item_ids = sorted(
        set(
            candidate_rows.filter(
                allocation_source="warehouse",
                stock_item_id__isnull=False,
            ).values_list("stock_item_id", flat=True)
        )
    )
    # reserve_proposal_inventory orders allocation keys alphabetically, so
    # catalog variants must be locked before warehouse rows here as well.
    variants = {
        row.pk: row
        for row in ProductColorVariant.objects.select_for_update()
        .filter(pk__in=variant_ids)
        .order_by("pk")
    }
    stock_items = {
        row.pk: row
        for row in StockItem.objects.select_for_update()
        .filter(pk__in=stock_item_ids)
        .order_by("pk")
    }
    return variants, stock_items


def _reservation_target_key(reservation):
    if reservation.allocation_source == "warehouse":
        return "warehouse", reservation.stock_item_id
    return "catalog_variant", reservation.color_variant_id


def _catalog_payment_size_plans(proposal, reservations, *, now, generation_id=None):
    """Bind each finite size commitment to its actual complete item set.

    A reservation's first item does not represent a mixed-size group. Old
    single-item rows remain usable only when that item proves the full count.
    A changed or incomplete allocation goes to resource review, never to a
    guessed size write-off. Allocation parents have already been locked.
    """
    from management.services.ig_inventory_size_capacity import (
        SizeCapacityError, check_catalog_size_capacity,
    )

    from django.db import connection
    items = {row.pk: row for row in proposal.items.select_for_update(
        nowait=connection.features.has_select_for_update_nowait,
    ).order_by("pk")}
    groups = {}
    invalid = set()
    used = set()
    for row in reservations:
        if row.allocation_source not in {"catalog_variant", ""} or not row.color_variant_id:
            continue
        identity = row.color_variant_id
        ids = row.line_ids or [row.item_id]
        valid = (isinstance(ids, list) and bool(ids)
            and all(type(value) is int and value in items for value in ids)
            and len(set(ids)) == len(ids) and not used.intersection(ids))
        selected = [items[value] for value in ids] if valid else []
        if (not valid or any(item.color_variant_id != identity for item in selected)
            or sum(int(item.quantity) for item in selected) != int(row.quantity)):
            invalid.add(identity)
            continue
        used.update(ids)
        groups.setdefault(identity, []).extend(selected)
    plans, reasons = {}, {identity: "inventory_size_allocation_changed" for identity in invalid}
    for identity, selected in sorted(groups.items()):
        if identity in invalid:
            continue
        try:
            plans[identity] = check_catalog_size_capacity(identity, selected, now=now,
                exclude_proposal_id=proposal.pk if generation_id is None else None,
                exclude_generation_id=generation_id)
        except SizeCapacityError as exc:
            if exc.reason in {"catalog_size_item_busy", "catalog_size_reservation_busy"}:
                # Payment owners catch ValueError as a real resource failure.
                # Contention must roll back/retry their transaction, never
                # terminalize a valid paid customer's protected allocation.
                from django.db import OperationalError
                raise OperationalError(1205, "Temporary catalogue allocation contention") from exc
            reasons[identity] = "late_payment_size_stock_reallocated"
    return plans, reasons


def _payment_review_reason(
    reservation,
    *,
    stock_item,
    color_variant,
    paid_at,
    now,
    required_quantity=None,
    generation_id=None,
):
    from management.models import IgCheckoutInventoryReservation

    if reservation.state in {
        IgCheckoutInventoryReservation.State.RELEASED,
        IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW,
    }:
        return reservation.release_reason or "late_payment_overbooked"
    if reservation.expires_at and reservation.expires_at <= paid_at:
        return "late_payment_expired_active"
    required = int(required_quantity or reservation.quantity or 0)
    try:
        if reservation.allocation_source == "warehouse":
            from warehouse.services.inventory import protected_stock_quantity

            available = int(stock_item.quantity or 0) if stock_item is not None else 0
            protected_elsewhere = protected_stock_quantity(
                stock_item_id=reservation.stock_item_id,
                at=now,
                exclude_proposal_id=(
                    reservation.proposal_id if generation_id is None else None
                ),
                exclude_generation_id=generation_id,
                lock=True,
            )
        else:
            available = int(color_variant.stock or 0) if color_variant is not None else 0
            protected_elsewhere = _catalog_variant_protected_quantity(
                color_variant_id=reservation.color_variant_id,
                at=now,
                exclude_proposal_id=(
                    reservation.proposal_id if generation_id is None else None
                ),
                exclude_generation_id=generation_id,
                lock=True,
            )
    except InventoryReservationError as exc:
        if exc.reason == "inventory_allocation_busy":
            from django.db import OperationalError
            raise OperationalError(1205, "Temporary paid allocation contention") from exc
        raise
    if protected_elsewhere + required > available:
        return "late_payment_stock_reallocated"
    return ""


def _current_legacy_revision_rows(proposal, candidate_rows):
    """Historical revision holds cannot become this revision's paid basket."""
    from management.models import IgCheckoutInventoryReservation as Reservation
    from product_catalog.models import ProductInventoryPolicy
    all_rows = Reservation.objects.filter(proposal=proposal, invoice_generation__isnull=True)
    keys = list(all_rows.order_by().values_list("allocation_key", flat=True).distinct()[:201])
    if len(keys) > 200:
        raise InventoryReservationError("inventory_revision_scope_unbounded")
    fingerprints = [_fingerprint(proposal, key) for key in keys]
    current = candidate_rows.filter(invoice_generation__isnull=True,
        reservation_fingerprint__in=fingerprints)
    if not current.exists():
        # A repeated paid commit is idempotent; a missing current hold for a
        # tracked basket must not borrow a released older revision's evidence.
        committed = all_rows.filter(reservation_fingerprint__in=fingerprints,
            state__in=[Reservation.State.PAID_COMMITTED, Reservation.State.FULFILLED, Reservation.State.CONSUMED]).exists()
        products = list(proposal.items.filter(product_id__isnull=False).values_list("product_id", flat=True))
        policies = {row.product_id: row.source for row in ProductInventoryPolicy.objects.filter(product_id__in=products)}
        tracked = any(source != ProductInventoryPolicy.Source.UNTRACKED for source in policies.values())
        legacy_tracked = proposal.items.filter(color_variant_id__isnull=False).exclude(product_id__in=policies).exists()
        if not committed and (tracked or legacy_tracked):
            raise InventoryReservationError("inventory_current_revision_hold_missing")
    return current


@transaction.atomic
def commit_proposal_inventory(
    proposal,
    *,
    order=None,
    paid_at=None,
    generation=None,
):
    from management.models import IgCheckoutInventoryReservation

    now = timezone.now()
    paid_at = paid_at or now
    from management.services.ig_checkout_payment import _prelock_assisted_proposal
    assisted_owner = _prelock_assisted_proposal(proposal)
    proposal = type(proposal).objects.select_for_update().get(pk=proposal.pk)
    if assisted_owner is not None and generation is not None:
        from management.services.ig_assisted_checkout import admit_assisted_settlement
        admitted = admit_assisted_settlement(generation.payment_attempt, now=now)
        if not isinstance(admitted, dict) or admitted.get("ready") is not True:
            raise InventoryReservationError((admitted or {}).get("reason") or "assisted_scope_overlap")
    candidate_rows = IgCheckoutInventoryReservation.objects.filter(
        proposal=proposal,
        state__in=[
            IgCheckoutInventoryReservation.State.ACTIVE,
            IgCheckoutInventoryReservation.State.RELEASED,
        ],
    )
    generation_id = getattr(generation, "pk", None)
    if generation_id is not None:
        candidate_rows = candidate_rows.filter(
            invoice_generation_id=generation_id
        )
    else:
        candidate_rows = _current_legacy_revision_rows(proposal, candidate_rows)
    variants, stock_items = _payment_inventory_targets(candidate_rows)
    reservations = list(
        candidate_rows.select_for_update().order_by("pk")
    )
    if not reservations:
        return 0
    required_by_target = {}
    for reservation in reservations:
        key = _reservation_target_key(reservation)
        required_by_target[key] = (
            required_by_target.get(key, 0) + int(reservation.quantity or 0)
        )
    consumed_by_variant = {}
    catalog_rows = []
    size_plans, size_reasons = _catalog_payment_size_plans(
        proposal, reservations, now=now, generation_id=generation_id,
    )
    for reservation in reservations:
        size_reason = (size_reasons.get(reservation.color_variant_id, "")
            if reservation.allocation_source in {"catalog_variant", ""} else "")
        review_reason = size_reason or _payment_review_reason(
            reservation,
            stock_item=stock_items.get(reservation.stock_item_id),
            color_variant=variants.get(reservation.color_variant_id),
            paid_at=paid_at,
            now=now,
            required_quantity=required_by_target[_reservation_target_key(reservation)],
            generation_id=generation_id,
        )
        if review_reason:
            reservation.state = IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW
            reservation.release_reason = review_reason
            if order is not None:
                reservation.order_id = order.pk
            reservation.updated_at = now
            reservation.save(
                update_fields=["state", "release_reason", "order", "updated_at"]
            )
            continue
        if reservation.allocation_source == "warehouse":
            reservation.state = IgCheckoutInventoryReservation.State.PAID_COMMITTED
            reservation.paid_committed_at = now
            if order is not None:
                reservation.order_id = order.pk
            reservation.updated_at = now
            reservation.save(
                update_fields=["state", "paid_committed_at", "order", "updated_at"]
            )
        elif reservation.color_variant_id:
            catalog_rows.append(reservation)
            consumed_by_variant[reservation.color_variant_id] = (
                consumed_by_variant.get(reservation.color_variant_id, 0)
                + int(reservation.quantity or 0)
            )
    for variant_id, quantity in consumed_by_variant.items():
        variant = variants.get(variant_id)
        if variant is None or int(variant.stock or 0) < quantity:
            raise ValueError("reserved_stock_changed")
        variant.stock = int(variant.stock) - quantity
        variant.save(update_fields=["stock"])
        from product_catalog.models import VariantSizeRule
        for rule_id, size_quantity in size_plans.get(variant_id, {}).items():
            changed = VariantSizeRule.objects.filter(pk=rule_id,
                variant_id=variant_id, stock__gte=size_quantity).update(
                    stock=F("stock") - size_quantity, updated_at=now)
            if changed != 1:
                raise InventoryReservationError("reserved_size_stock_changed")

    updates = {
        "state": IgCheckoutInventoryReservation.State.FULFILLED,
        "fulfilled_quantity": F("quantity"),
        "consumed_at": now,
        "fulfilled_at": now,
        "updated_at": now,
    }
    if order is not None:
        # The reservation model intentionally has no order FK in 0116; the
        # payment attempt/proposal remains the durable ownership link.
        updates["release_reason"] = f"order:{order.pk}"[:128]
        updates["order_id"] = order.pk
    if catalog_rows:
        IgCheckoutInventoryReservation.objects.filter(
            pk__in=[reservation.pk for reservation in catalog_rows],
            state__in=[
                IgCheckoutInventoryReservation.State.ACTIVE,
                IgCheckoutInventoryReservation.State.RELEASED,
            ],
        ).update(**updates)
    return len(reservations)


def commit_generation_inventory(generation, *, order=None, paid_at=None):
    return commit_proposal_inventory(
        generation.proposal,
        order=order,
        paid_at=paid_at,
        generation=generation,
    )


@transaction.atomic
def mark_overbooked_proposal_inventory(
    proposal,
    *,
    order=None,
    reason="late_payment_overbooked",
    paid_at=None,
):
    """Move unsafe payment commitments into explicit inventory review.

    Provider payment truth is durable even when the reservation expired.  The
    payment binder must not silently attach that payment to an order; this
    marker is the hand-off to a human who can source stock or refund safely.
    """
    from management.models import IgCheckoutInventoryReservation, IgFollowUpTask

    now = timezone.now()
    paid_at = paid_at or now
    proposal = type(proposal).objects.select_for_update().get(pk=proposal.pk)
    candidate_rows = IgCheckoutInventoryReservation.objects.filter(
        proposal=proposal,
        allocation_source__in=["warehouse", "catalog_variant", ""],
        state__in=[
            IgCheckoutInventoryReservation.State.ACTIVE,
            IgCheckoutInventoryReservation.State.RELEASED,
            IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW,
        ],
    )
    missing_current = False
    try:
        candidate_rows = _current_legacy_revision_rows(proposal, candidate_rows)
    except InventoryReservationError as exc:
        if exc.reason != "inventory_current_revision_hold_missing":
            raise
        missing_current = True
        candidate_rows = candidate_rows.none()
    variants, stock_items = _payment_inventory_targets(candidate_rows)
    rows = list(
        candidate_rows.select_for_update().order_by("pk")
    )
    required_by_target = {}
    for row in rows:
        key = _reservation_target_key(row)
        required_by_target[key] = (
            required_by_target.get(key, 0) + int(row.quantity or 0)
        )
    review_rows = []
    _size_plans, size_reasons = _catalog_payment_size_plans(proposal, rows, now=now)
    for row in rows:
        size_reason = (size_reasons.get(row.color_variant_id, "")
            if row.allocation_source in {"catalog_variant", ""} else "")
        review_reason = size_reason or _payment_review_reason(
            row,
            stock_item=stock_items.get(row.stock_item_id),
            color_variant=variants.get(row.color_variant_id),
            paid_at=paid_at,
            now=now,
            required_quantity=required_by_target[_reservation_target_key(row)],
        )
        if review_reason:
            row.release_reason = (
                review_reason
                if row.state == IgCheckoutInventoryReservation.State.ACTIVE
                else str(reason or review_reason)[:128]
            )
            review_rows.append(row)
    if not review_rows and not missing_current:
        return 0
    for row in review_rows:
        row.state = IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW
        if order is not None:
            row.order_id = order.pk
        row.updated_at = now
        row.save(update_fields=["state", "release_reason", "order", "updated_at"])

    deal = proposal.deal
    task, _created = IgFollowUpTask.objects.get_or_create(
        event_key=f"inventory-overbooked:{proposal.pk}",
        defaults={
            "client": proposal.client,
            "deal": deal,
            "due_at": now,
            "status": IgFollowUpTask.Status.SKIPPED,
            "kind": IgFollowUpTask.Kind.MANAGER_TASK,
            "reason": "inventory_overbooked_review",
            "trigger": IgFollowUpTask.Trigger.EVENT,
            "event_occurred_at": now,
            "event_payload": {
                "proposal_id": proposal.pk,
                "reservation_ids": [row.pk for row in review_rows],
                "inventory_reason": "inventory_current_revision_hold_missing" if missing_current else "inventory_overbooked_review",
            },
            "skip_reason": "human_agent_required",
            "message_text": (
                f"Угода #{deal.pk}: оплату підтверджено, але резерв складу вже звільнений. "
                "Потрібно перевірити наявність, заміну або повернення коштів."
            ),
            "last_error": f"reservation_ids={','.join(str(row.pk) for row in review_rows)}",
        },
    )
    if not _created:
        task.last_error = f"reservation_ids={','.join(str(row.pk) for row in review_rows)}"
        task.save(update_fields=["last_error", "updated_at"])

    def _notify():
        try:
            from management.services.instagram_bot import notify_manager

            notify_manager(
                f"📦 IG: угода #{deal.pk} має оплату після звільнення резерву. "
                "Потрібна перевірка складу або повернення коштів.",
                dedupe_key=f"inventory_overbooked_review:{proposal.pk}",
                event_type="inventory_overbooked_review",
                client=proposal.client,
            )
        except Exception:
            return

    transaction.on_commit(_notify)
    # The event key and notification dedupe keep side effects idempotent. The
    # return value must remain positive on replay so payment binders cannot
    # mistake an existing review state for permission to attach the order.
    return max(len(review_rows), int(missing_current))


def fulfill_split_parcel_inventory(parcel, dispatch, actor, now=None):
    """Consume only a signed parcel's paid allocation, inside its dispatch transaction.

    ``dispatch`` is an unsaved UUID identity. The caller must seal the returned
    receipt into its immutable envelope and insert that dispatch before the
    enclosing transaction commits. Existing receipts are the per-parcel ledger;
    catalogue stock was already consumed by verified payment.
    """
    from django.db import connection

    if not connection.in_atomic_block:
        raise InventoryReservationError("split_inventory_requires_dispatch_transaction")
    # Preserve a savepoint even if a caller converts a typed inventory failure
    # into a result inside its outer dispatch transaction.
    with transaction.atomic():
        return _fulfill_split_parcel_inventory(parcel, dispatch, actor, now=now)


def _fulfill_split_parcel_inventory(parcel, dispatch, actor, now=None):
    from django.contrib.contenttypes.models import ContentType
    from management.models import IgCheckoutInventoryReservation as Reservation, IgCheckoutProposalItem, IgSplitParcelDispatch
    from management.services.ig_split_execution import validated_split_plan
    from warehouse.models import MovementReason, StockItem, StockMovement
    from warehouse.services.inventory import protected_stock_quantity

    now = now or timezone.now()
    validation = validated_split_plan(parcel.plan_id, now=now)
    if not validation.get("ready"):
        raise InventoryReservationError(validation.get("reason") or "split_plan_invalid")
    plan, order = validation["plan"], validation["order"]
    actual_parcel = next((row for row in validation["parcels"] if row.pk == parcel.pk), None)
    if actual_parcel is None or dispatch.parcel_id != actual_parcel.pk:
        raise InventoryReservationError("split_inventory_parcel_mismatch")
    if not getattr(actor, "pk", None) or dispatch.actor_id != actor.pk:
        raise InventoryReservationError("split_inventory_actor_mismatch")
    # The executor locks the order/plan/parcel before entering this hook. The
    # current read also makes accidental repeated calls before sealing fail.
    prior_dispatches = list(IgSplitParcelDispatch.objects.select_for_update()
        .filter(parcel__plan_id=plan.pk).order_by("pk"))
    existing = next((row for row in prior_dispatches if row.parcel_id == actual_parcel.pk), None)
    if existing is not None:
        if existing.pk != dispatch.pk:
            raise InventoryReservationError("split_parcel_already_dispatched")
        receipt = existing.envelope.get("inventory_receipt", {})
        if (receipt.get("schema") != "ig-split-inventory-receipt.v1"
                or receipt.get("dispatch_id") != str(existing.pk)
                or receipt.get("parcel_id") != str(actual_parcel.pk)):
            raise InventoryReservationError("split_inventory_receipt_invalid")
        return {"ready": True, "proof": receipt}
    if not dispatch._state.adding:
        raise InventoryReservationError("split_inventory_receipt_missing")

    owner = plan.envelope
    proposal_id = owner.get("proposal_id")
    generation_id = owner.get("invoice_generation_id")
    if not proposal_id or "invoice_generation_id" not in owner:
        raise InventoryReservationError("split_inventory_owner_missing")
    items = actual_parcel.envelope.get("items", [])
    if not items:
        raise InventoryReservationError("split_inventory_manifest_empty")
    proposal_items = {row.pk: row for row in IgCheckoutProposalItem.objects.filter(proposal_id=proposal_id)}
    requested = {}
    for item in items:
        item_id, quantity = item.get("proposal_item_id"), item.get("quantity")
        if (type(item_id) is not int or type(quantity) is not int or quantity <= 0
                or item_id not in proposal_items):
            raise InventoryReservationError("split_inventory_manifest_invalid")
        requested[item_id] = requested.get(item_id, 0) + quantity
    candidates = Reservation.objects.filter(proposal_id=proposal_id,
        invoice_generation_id=generation_id, order_id=order.pk)
    # Allocation parent locks are globally ordered before reservation locks.
    stock_ids = sorted(set(candidates.filter(allocation_source="warehouse")
        .values_list("stock_item_id", flat=True)) - {None})
    stocks = {row.pk: row for row in StockItem.objects.select_for_update()
        .filter(pk__in=stock_ids).order_by("pk")}
    reservations = list(candidates.select_for_update().order_by("pk"))
    mapping, selected = {}, []
    for row in reservations:
        line_ids = row.line_ids or [row.item_id]
        if (not line_ids or any(type(identity) is not int or identity not in proposal_items for identity in line_ids)
                or len(set(line_ids)) != len(line_ids)
                or sum(proposal_items[identity].quantity for identity in line_ids) != row.quantity):
            raise InventoryReservationError("split_inventory_line_ownership_invalid")
        for identity in line_ids:
            if identity in mapping:
                raise InventoryReservationError("split_inventory_line_ownership_ambiguous")
            mapping[identity] = row
        quantity = sum(requested.get(identity, 0) for identity in line_ids)
        if quantity:
            selected.append((row, quantity, sorted(identity for identity in line_ids if identity in requested)))
    if set(requested) - set(mapping):
        raise InventoryReservationError("split_inventory_reservation_missing")

    previous = {}
    for prior in prior_dispatches:
        receipt = prior.envelope.get("inventory_receipt", {})
        if (receipt.get("schema") != "ig-split-inventory-receipt.v1"
                or receipt.get("dispatch_id") != str(prior.pk)
                or receipt.get("order_id") != order.pk
                or receipt.get("plan_id") != str(plan.pk)):
            raise InventoryReservationError("split_inventory_receipt_invalid")
        for entry in receipt.get("rows", []):
            identity, quantity = entry.get("reservation_id"), entry.get("quantity")
            if type(identity) is not int or type(quantity) is not int or quantity <= 0:
                raise InventoryReservationError("split_inventory_receipt_invalid")
            previous[identity] = previous.get(identity, 0) + quantity
    for row, quantity, _line_ids in selected:
        if row.allocation_source == "warehouse":
            if (row.state != Reservation.State.PAID_COMMITTED or row.stock_item_id not in stocks
                    or row.fulfilled_quantity != previous.get(row.pk, 0)
                    or quantity > row.quantity - row.fulfilled_quantity):
                raise InventoryReservationError("split_inventory_commitment_mismatch")
        elif row.allocation_source in {"catalog_variant", ""}:
            if row.state not in {Reservation.State.FULFILLED, Reservation.State.CONSUMED} or row.fulfilled_quantity != row.quantity:
                raise InventoryReservationError("split_catalogue_payment_not_consumed")
        else:
            raise InventoryReservationError("split_inventory_source_unsupported")

    proof = {"schema": "ig-split-inventory-receipt.v1", "dispatch_id": str(dispatch.pk),
        "parcel_id": str(actual_parcel.pk), "plan_id": str(plan.pk), "order_id": order.pk,
        "proposal_id": proposal_id, "invoice_generation_id": generation_id, "rows": [], "catalogue_rows": []}
    # Reduce commitments before checking the protected physical remainder. All
    # changes, movements, and the immutable dispatch receipt commit together.
    for row, quantity, line_ids in selected:
        if row.allocation_source != "warehouse":
            proof["catalogue_rows"].append({"reservation_id": row.pk, "quantity": quantity,
                "proposal_item_ids": line_ids, "consumed_at_payment": True})
            continue
        row.fulfilled_quantity += quantity
        row.state = Reservation.State.FULFILLED if row.fulfilled_quantity == row.quantity else Reservation.State.PAID_COMMITTED
        row.fulfilled_at = now if row.state == Reservation.State.FULFILLED else None
        row.consumed_at = row.consumed_at or now
        row.save(update_fields=["fulfilled_quantity", "state", "fulfilled_at", "consumed_at", "updated_at"])
    stock_ct = ContentType.objects.get_for_model(StockItem)
    for row, quantity, line_ids in selected:
        if row.allocation_source != "warehouse":
            continue
        stock = stocks[row.stock_item_id]
        after = stock.quantity - quantity
        if after < 0 or after < protected_stock_quantity(stock_item_id=stock.pk, at=now, lock=True):
            raise InventoryReservationError("fulfillment_stock_shortfall")
        stock.quantity = after
        stock.save(update_fields=["quantity", "updated_at"])
        movement = StockMovement.objects.create(content_type=stock_ct, object_id=stock.pk,
            delta=-quantity, quantity_after=after, reason=MovementReason.ORDER_WRITE_OFF,
            comment=f"Instagram split parcel {actual_parcel.pk} · dispatch {dispatch.pk}", order=order, created_by=actor)
        row.stock_movement = movement
        row.save(update_fields=["stock_movement", "updated_at"])
        proof["rows"].append({"reservation_id": row.pk, "stock_item_id": stock.pk,
            "movement_id": movement.pk, "quantity": quantity, "fulfilled_quantity": row.fulfilled_quantity,
            "proposal_item_ids": line_ids})
    return {"ready": True, "proof": proof}


@transaction.atomic
def fulfill_warehouse_reservation(
    reservation,
    *,
    order,
    order_item=None,
    write_off_request=None,
    user=None,
):
    """Write off one paid warehouse allocation exactly once."""
    from management.models import IgCheckoutInventoryReservation
    from warehouse.models import MovementReason, StockItem
    from warehouse.services.inventory import adjust_stock_item

    identity = IgCheckoutInventoryReservation.objects.get(pk=reservation.pk)
    if identity.allocation_source != "warehouse" or not identity.stock_item_id:
        raise InventoryReservationError("not_warehouse_allocation")
    # Allocation parents precede reservation rows in reservation/payment and
    # recount transactions. Reversing that order deadlocks a paid write-off
    # against a manager preparing another client's basket in the same pool.
    stock_item = StockItem.objects.select_for_update().get(pk=identity.stock_item_id)
    locked = IgCheckoutInventoryReservation.objects.select_for_update().get(pk=reservation.pk)
    if locked.stock_item_id != stock_item.pk or locked.allocation_source != "warehouse":
        raise InventoryReservationError("inventory_allocation_changed")
    if locked.state in {IgCheckoutInventoryReservation.State.FULFILLED,
            IgCheckoutInventoryReservation.State.CONSUMED}:
        return None
    if locked.state != IgCheckoutInventoryReservation.State.PAID_COMMITTED:
        raise InventoryReservationError("inventory_not_paid_committed")
    if locked.allocation_source != "warehouse" or not locked.stock_item_id:
        raise InventoryReservationError("not_warehouse_allocation")
    from management.models import IgSplitOrderPlan
    if IgSplitOrderPlan.objects.filter(order_id=order.pk).exists():
        raise InventoryReservationError("split_parcel_dispatch_required")
    if locked.order_id not in {None, order.pk}:
        raise InventoryReservationError("inventory_order_mismatch")
    quantity = int(locked.quantity or 0) - int(locked.fulfilled_quantity or 0)
    if quantity <= 0:
        raise InventoryReservationError("inventory_fulfillment_ledger_mismatch")
    if int(stock_item.quantity or 0) < quantity:
        locked.state = IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW
        locked.release_reason = "fulfillment_stock_shortfall"
        locked.save(update_fields=["state", "release_reason", "updated_at"])
        raise InventoryReservationError("fulfillment_stock_shortfall")
    movement = adjust_stock_item(
        stock_item=stock_item,
        delta=-quantity,
        user=user,
        reason=MovementReason.ORDER_WRITE_OFF,
        comment=f"Instagram reservation {locked.pk}",
        order=order,
        write_off_request=write_off_request,
    )
    now = timezone.now()
    locked.state = IgCheckoutInventoryReservation.State.FULFILLED
    locked.fulfilled_quantity = locked.quantity
    locked.order = order
    locked.order_item = order_item
    locked.write_off_request = write_off_request
    locked.stock_movement = movement
    locked.fulfilled_at = now
    locked.consumed_at = locked.consumed_at or now
    locked.save(update_fields=[
        "state", "order", "order_item", "write_off_request", "stock_movement",
        "fulfilled_at", "fulfilled_quantity", "consumed_at", "updated_at",
    ])
    return movement


def fulfill_order_inventory_reservations(order, *, write_off_request=None, user=None):
    """Attach warehouse write-off movements to paid IG allocations.

    The warehouse UI creates movements first. This adapter binds those exact
    movements to the corresponding paid reservations and is idempotent on
    repeated Telegram/admin callbacks. If no movement exists yet, it performs
    the guarded write-off itself.
    """
    from management.models import IgCheckoutInventoryReservation
    from django.contrib.contenttypes.models import ContentType
    from warehouse.models import StockItem, StockMovement, WriteOffRequest

    try:
        with transaction.atomic():
            from management.models import IgSplitOrderPlan
            if IgSplitOrderPlan.objects.filter(order_id=order.pk).exists():
                raise InventoryReservationError("split_parcel_dispatch_required")
            if write_off_request is not None:
                # The callback may hold an instance loaded before a reversal.
                # Re-read it under the transaction lock so a repeated callback
                # cannot recreate a cancelled write-off.
                write_off_request = WriteOffRequest.objects.select_for_update().get(
                    pk=write_off_request.pk,
                )
                if write_off_request.status == WriteOffRequest.STATUS_CANCELLED:
                    return 0
            stock_item_ct = ContentType.objects.get_for_model(StockItem)
            candidate_rows = IgCheckoutInventoryReservation.objects.filter(
                order=order, allocation_source="warehouse",
                state=IgCheckoutInventoryReservation.State.PAID_COMMITTED,
            )
            parent_ids = sorted(set(candidate_rows.values_list("stock_item_id", flat=True)) - {None})
            for identity in parent_ids:
                StockItem.objects.select_for_update().get(pk=identity)
            reservations = list(
                IgCheckoutInventoryReservation.objects.select_for_update().filter(
                    order=order,
                    allocation_source="warehouse",
                    state=IgCheckoutInventoryReservation.State.PAID_COMMITTED,
                ).order_by("pk")
            )
            if any(row.stock_item_id not in parent_ids for row in reservations):
                raise InventoryReservationError("inventory_allocation_changed")
            fulfilled = 0
            for reservation in reservations:
                movement = None
                if write_off_request is not None:
                    movement_queryset = StockMovement.objects.select_for_update().filter(
                        order=order,
                        write_off_request=write_off_request,
                        content_type=stock_item_ct,
                        delta__lt=0,
                    )
                    movement = movement_queryset.filter(
                        object_id=reservation.stock_item_id,
                        delta=-(int(reservation.quantity) - int(reservation.fulfilled_quantity)),
                    ).order_by("-pk").first()
                    if movement is None and movement_queryset.exists():
                        raise InventoryReservationError("warehouse_writeoff_mismatch")
                if movement is None:
                    try:
                        movement = fulfill_warehouse_reservation(
                            reservation,
                            order=order,
                            write_off_request=write_off_request,
                            user=user,
                        )
                    except InventoryReservationError as exc:
                        exc.reservation_id = reservation.pk
                        raise
                    if movement is None:
                        continue
                now = timezone.now()
                IgCheckoutInventoryReservation.objects.filter(
                    pk=reservation.pk,
                    state=IgCheckoutInventoryReservation.State.PAID_COMMITTED,
                ).update(
                    state=IgCheckoutInventoryReservation.State.FULFILLED,
                    fulfilled_quantity=F("quantity"),
                    order=order,
                    write_off_request=write_off_request,
                    stock_movement=movement,
                    fulfilled_at=now,
                    consumed_at=now,
                    updated_at=now,
                )
                fulfilled += 1
            return fulfilled
    except InventoryReservationError as exc:
        # Mark the shortage inside the current transaction. Callers already
        # inside a larger atomic block receive ``reservation_id`` and repeat
        # this update after that outer rollback, so the marker remains durable.
        if (
            exc.reason == "fulfillment_stock_shortfall"
            and getattr(exc, "reservation_id", None)
        ):
            IgCheckoutInventoryReservation.objects.filter(
                pk=exc.reservation_id,
                state=IgCheckoutInventoryReservation.State.PAID_COMMITTED,
            ).update(
                state=IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW,
                release_reason="fulfillment_stock_shortfall",
                updated_at=timezone.now(),
            )
        raise


@transaction.atomic
def restore_order_inventory_reservations(write_off_request):
    """Clear a reversed write-off link so the paid reservation can be retried."""
    from management.models import IgCheckoutInventoryReservation

    return IgCheckoutInventoryReservation.objects.filter(
        write_off_request=write_off_request,
        state__in=[
            IgCheckoutInventoryReservation.State.PAID_COMMITTED,
            IgCheckoutInventoryReservation.State.FULFILLED,
        ],
    ).update(
        state=IgCheckoutInventoryReservation.State.PAID_COMMITTED,
        fulfilled_quantity=0,
        write_off_request=None,
        stock_movement=None,
        order_item=None,
        fulfilled_at=None,
        consumed_at=None,
        updated_at=timezone.now(),
    )


@transaction.atomic
def release_expired_proposal_inventory(*, limit=500):
    from management.models import IgCheckoutInventoryReservation
    from django.db.models import Q

    now = timezone.now()
    ids = list(IgCheckoutInventoryReservation.objects.filter(
        state=IgCheckoutInventoryReservation.State.ACTIVE,
        expires_at__lte=now,
    ).filter(
        Q(proposal__payment_attempt__isnull=True)
        | Q(proposal__payment_attempt__status__in=["failed", "cancelled", "expired"])
        | Q(proposal__status__in=["cancelled", "expired", "revoked", "superseded"])
    ).exclude(uncertain_inventory_reservation_q()).order_by("expires_at", "pk").values_list("pk", flat=True)[:limit])
    if not ids:
        return 0
    return IgCheckoutInventoryReservation.objects.filter(
        pk__in=ids,
        state=IgCheckoutInventoryReservation.State.ACTIVE,
        expires_at__lte=now,
    ).exclude(uncertain_inventory_reservation_q()).update(
        state=IgCheckoutInventoryReservation.State.RELEASED,
        released_at=now,
        release_reason="expired",
        updated_at=now,
    )
