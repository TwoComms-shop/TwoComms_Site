"""Бізнес-операції зі складом: запис рухів, коригування, списання."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from warehouse.models import (
    MovementReason,
    PrintColorVariant,
    StockItem,
    StockMovement,
    WriteOffRequest,
)


def protected_stock_quantity(
    *,
    stock_item_id: int,
    at=None,
    exclude_proposal_id: Optional[int] = None,
    exclude_generation_id: Optional[int] = None,
    exclude_paid_order_id: Optional[int] = None,
    lock: bool = False,
) -> int:
    """Return warehouse units protected by live or paid IG commitments.

    ACTIVE reservations with a possible bank effect stay protected until a
    trusted terminal result; ordinary holds expire with their proposal. A PAID_COMMITTED row is a
    durable obligation until warehouse fulfillment, regardless of its
    original checkout TTL. A write-off may exclude only paid commitments that
    belong to the exact order being fulfilled.
    """
    from management.models import IgCheckoutInventoryReservation

    current_time = at or timezone.now()
    reservations = IgCheckoutInventoryReservation.objects.filter(
        allocation_source="warehouse",
        stock_item_id=stock_item_id,
    )
    from management.services.ig_inventory import protected_inventory_reservation_q
    reservations = reservations.filter(protected_inventory_reservation_q(current_time))
    if exclude_proposal_id is not None:
        reservations = reservations.exclude(proposal_id=exclude_proposal_id)
    if exclude_generation_id is not None:
        reservations = reservations.exclude(
            invoice_generation_id=exclude_generation_id
        )
    if exclude_paid_order_id is not None:
        reservations = reservations.exclude(
            state=IgCheckoutInventoryReservation.State.PAID_COMMITTED,
            order_id=exclude_paid_order_id,
        )
    if lock:
        # Under the already locked allocation parent, use a current read.
        # A plain aggregate in MariaDB REPEATABLE READ can miss the preceding
        # transaction's new reservation after waiting for that parent lock.
        from django.db import connection, OperationalError
        try:
            return sum(quantity - fulfilled for quantity, fulfilled in reservations.select_for_update(
                nowait=connection.features.has_select_for_update_nowait,
            ).order_by("pk").values_list("quantity", "fulfilled_quantity"))
        except OperationalError as exc:
            if exc.args and exc.args[0] in {1205, 3572}:
                from management.services.ig_inventory import InventoryReservationError
                raise InventoryReservationError("inventory_allocation_busy") from exc
            raise
    return int(reservations.aggregate(total=Sum(F("quantity") - F("fulfilled_quantity")))["total"] or 0)


def weighted_average_cost(
    *,
    old_qty: int,
    old_cost: Decimal,
    add_qty: int,
    add_cost: Decimal,
) -> Decimal:
    """Розрахунок середньозваженої собівартості при прибутті нової партії.

    Формула: (old_qty × old_cost + add_qty × add_cost) / (old_qty + add_qty)

    Якщо ``old_qty == 0`` — повертає ``add_cost`` (стартова собівартість).
    Якщо ``add_qty == 0`` — повертає ``old_cost`` (нічого не змінюється).
    Округлення до 2 знаків після коми (стандартна точність грн).
    """
    if old_qty == 0 and add_qty == 0:
        return old_cost
    if old_qty == 0:
        return Decimal(add_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if add_qty == 0:
        return Decimal(old_cost)
    total_value = (Decimal(old_qty) * Decimal(old_cost)) + (Decimal(add_qty) * Decimal(add_cost))
    new_qty = Decimal(old_qty + add_qty)
    return (total_value / new_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@transaction.atomic
def adjust_stock_item(
    *,
    stock_item: StockItem,
    delta: int,
    user=None,
    reason: str = MovementReason.MANUAL_ADD,
    comment: str = "",
    order=None,
    write_off_request: Optional[WriteOffRequest] = None,
    cost_price_override: Optional[Decimal] = None,
) -> StockMovement:
    """Змінити кількість StockItem на delta та зафіксувати StockMovement.

    Поведінка cost_price:
    - delta > 0 (прихід) і ``cost_price_override`` задано →
      нова cost_price = weighted average (old × old_qty + override × delta) / total.
      Це зберігає історію витрат: якщо було 5 шт по 500, додаємо 7 по 550 —
      середня стане ~529 грн, а не 550. Старі гроші не «забуваються».
    - delta > 0 без override → cost_price лишається без змін.
    - delta < 0 (списання/продаж) → cost_price НЕ змінюється
      (override ігнорується, бо ми не «купуємо» позицію).
    """
    if delta == 0:
        raise ValueError("Delta cannot be zero")

    requested_stock_item = stock_item
    # Callers may hold a stale instance (for example two warehouse workers
    # submitting the same order concurrently).  Always calculate from the
    # row-level lock, never from the caller's snapshot.
    stock_item = StockItem.objects.select_for_update().get(pk=stock_item.pk)
    old_qty = stock_item.quantity
    new_qty = old_qty + delta
    if new_qty < 0:
        raise ValueError(
            f"Недостатньо залишку: маємо {old_qty}, спроба списати {-delta}"
        )
    if delta < 0:
        if order is not None:
            from management.models import IgSplitOrderPlan
            if IgSplitOrderPlan.objects.filter(order_id=order.pk).exists():
                from management.services.ig_inventory import InventoryReservationError
                raise InventoryReservationError("split_parcel_dispatch_required")
        protected_quantity = protected_stock_quantity(
            stock_item_id=stock_item.pk,
            exclude_paid_order_id=getattr(order, "pk", None),
            lock=True,
        )
        if new_qty < protected_quantity:
            raise ValueError(
                "Недостатньо вільного залишку: "
                f"маємо {old_qty}, захищено резервами {protected_quantity}, "
                f"спроба списати {-delta}"
            )

    stock_item.quantity = new_qty
    if cost_price_override is not None and delta > 0:
        stock_item.cost_price = weighted_average_cost(
            old_qty=old_qty,
            old_cost=stock_item.cost_price,
            add_qty=delta,
            add_cost=Decimal(cost_price_override),
        )
        # Запам'ятовуємо ціну саме цієї (останньої) партії, щоб у UI
        # підсвітити розбіжність із середньозваженою ціною.
        stock_item.last_cost_price = Decimal(cost_price_override)
    stock_item.save(update_fields=["quantity", "cost_price", "last_cost_price", "updated_at"])

    # Keep the caller's object coherent with the row that was actually locked.
    # This matters for JSON endpoints that serialize it after the adjustment.
    requested_stock_item.quantity = stock_item.quantity
    requested_stock_item.cost_price = stock_item.cost_price
    requested_stock_item.last_cost_price = stock_item.last_cost_price
    requested_stock_item.updated_at = stock_item.updated_at

    movement = StockMovement.objects.create(
        content_type=ContentType.objects.get_for_model(StockItem),
        object_id=stock_item.pk,
        delta=delta,
        quantity_after=new_qty,
        reason=reason,
        comment=comment,
        order=order,
        write_off_request=write_off_request,
        created_by=user,
    )
    if delta > 0:
        def observe_signed_restock_subscriptions(*, stock_item_id=stock_item.pk,
                movement_id=movement.pk, occurred_at=movement.created_at):
            from management.services.ig_restock_subscriptions import observe_inventory_event
            observe_inventory_event(source={"kind": "warehouse_movement", "stock_item_id": stock_item_id,
                "movement_id": movement_id, "occurred_at": occurred_at})

        # Reverse write-offs may restore paid commitments later in the same
        # transaction. The observer sees the fully committed protected stock.
        transaction.on_commit(observe_signed_restock_subscriptions, robust=True)
    return movement


@transaction.atomic
def adjust_print_variant(
    *,
    variant: PrintColorVariant,
    delta: int,
    user=None,
    reason: str = MovementReason.PRINT_ADD,
    comment: str = "",
    order=None,
    write_off_request: Optional[WriteOffRequest] = None,
    cost_price_override: Optional[Decimal] = None,
) -> StockMovement:
    """Змінити кількість PrintColorVariant та зафіксувати StockMovement."""
    if delta == 0:
        raise ValueError("Delta cannot be zero")

    variant = PrintColorVariant.objects.select_for_update().get(pk=variant.pk)
    old_qty = variant.quantity
    new_qty = old_qty + delta
    if new_qty < 0:
        raise ValueError(
            f"Недостатньо принтів: маємо {old_qty}, спроба списати {-delta}"
        )
    variant.quantity = new_qty
    if cost_price_override is not None and delta > 0:
        variant.cost_price = weighted_average_cost(
            old_qty=old_qty,
            old_cost=variant.cost_price,
            add_qty=delta,
            add_cost=Decimal(cost_price_override),
        )
    variant.save(update_fields=["quantity", "cost_price", "updated_at"])

    return StockMovement.objects.create(
        content_type=ContentType.objects.get_for_model(PrintColorVariant),
        object_id=variant.pk,
        delta=delta,
        quantity_after=new_qty,
        reason=reason,
        comment=comment,
        order=order,
        write_off_request=write_off_request,
        created_by=user,
    )


@transaction.atomic
def reverse_write_off(*, write_off_request, user=None) -> int:
    """Відміняє продаж/списання за заявкою: повертає залишки і ставить CANCELLED.

    Для кожного руху-списання (delta < 0), привʼязаного до заявки, створює
    зворотний рух (delta > 0, reason=RETURN) і повертає кількість на склад.
    Ідемпотентно: якщо заявка вже скасована — нічого не робить.

    Returns:
        Кількість відмінених позицій.
    """
    from warehouse.models import WriteOffRequest

    write_off_request = WriteOffRequest.objects.select_for_update().get(
        pk=write_off_request.pk,
    )

    if write_off_request.status == WriteOffRequest.STATUS_CANCELLED:
        return 0

    order = write_off_request.order
    order_no = getattr(order, "order_number", "") if order else ""
    # Матеріалізуємо список ДО створення зворотних рухів, щоб не зациклитись.
    originals = list(
        write_off_request.movements.select_for_update()
        .select_related("content_type")
        .filter(delta__lt=0)
    )

    reversed_count = 0
    for mv in originals:
        target = mv.target
        if target is None:
            continue
        reverse_delta = -mv.delta  # додатнє — повертаємо на склад
        comment = f"Відміна продажу · Замовлення #{order_no}"
        if isinstance(target, StockItem):
            adjust_stock_item(
                stock_item=target,
                delta=reverse_delta,
                user=user,
                reason=MovementReason.RETURN,
                comment=comment,
                order=order,
                write_off_request=write_off_request,
            )
            reversed_count += 1
        elif isinstance(target, PrintColorVariant):
            adjust_print_variant(
                variant=target,
                delta=reverse_delta,
                user=user,
                reason=MovementReason.RETURN,
                comment=comment,
                order=order,
                write_off_request=write_off_request,
            )
            reversed_count += 1
        else:
            # Розхідники (ConsumableItem) — через окремий сервіс.
            try:
                from warehouse.services.consumables import adjust_consumable

                adjust_consumable(
                    consumable=target,
                    delta=reverse_delta,
                    user=user,
                    reason=MovementReason.RETURN,
                    comment=comment,
                    order=order,
                    write_off_request=write_off_request,
                )
                reversed_count += 1
            except Exception:
                continue

    write_off_request.status = WriteOffRequest.STATUS_CANCELLED
    write_off_request.save(update_fields=["status", "updated_at"])
    # An Instagram paid allocation is only fulfilled by this write-off. A
    # reversal returns it to the paid-committed state so a later replacement
    # write-off can be audited without creating a second reservation.
    try:
        from management.models import IgCheckoutInventoryReservation

        IgCheckoutInventoryReservation.objects.filter(
            write_off_request=write_off_request,
            state=IgCheckoutInventoryReservation.State.FULFILLED,
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
    except ImportError:
        # Warehouse remains usable when the optional Instagram app is absent
        # in migration/management commands.
        pass
    return reversed_count


def set_stock_quantity(
    *,
    stock_item: StockItem,
    new_quantity: int,
    user=None,
    reason: str = MovementReason.RECOUNT,
    comment: str = "",
    cost_price_override: Optional[Decimal] = None,
) -> Optional[StockMovement]:
    """Встановити абсолютну кількість (для інвентаризації)."""
    if new_quantity < 0:
        raise ValueError("Quantity cannot be negative")

    requested_stock_item = stock_item
    stock_item = StockItem.objects.select_for_update().get(pk=stock_item.pk)
    delta = new_quantity - stock_item.quantity
    if delta == 0 and cost_price_override is None:
        requested_stock_item.quantity = stock_item.quantity
        requested_stock_item.cost_price = stock_item.cost_price
        requested_stock_item.last_cost_price = stock_item.last_cost_price
        requested_stock_item.updated_at = stock_item.updated_at
        return None
    if delta == 0 and cost_price_override is not None:
        # тільки оновлення собівартості, не створюємо movement.
        # Ручна корекція ціни вирівнює середню й останню — спред зникає.
        stock_item.cost_price = cost_price_override
        stock_item.last_cost_price = cost_price_override
        stock_item.save(update_fields=["cost_price", "last_cost_price", "updated_at"])
        requested_stock_item.quantity = stock_item.quantity
        requested_stock_item.cost_price = stock_item.cost_price
        requested_stock_item.last_cost_price = stock_item.last_cost_price
        requested_stock_item.updated_at = stock_item.updated_at
        return None
    return adjust_stock_item(
        stock_item=stock_item,
        delta=delta,
        user=user,
        reason=reason,
        comment=comment,
        cost_price_override=cost_price_override,
    )


def set_print_variant_quantity(
    *,
    variant: PrintColorVariant,
    new_quantity: int,
    user=None,
    reason: str = MovementReason.RECOUNT,
    comment: str = "",
    cost_price_override: Optional[Decimal] = None,
) -> Optional[StockMovement]:
    if new_quantity < 0:
        raise ValueError("Quantity cannot be negative")
    delta = new_quantity - variant.quantity
    if delta == 0 and cost_price_override is None:
        return None
    if delta == 0 and cost_price_override is not None:
        variant.cost_price = cost_price_override
        variant.save(update_fields=["cost_price", "updated_at"])
        return None
    return adjust_print_variant(
        variant=variant,
        delta=delta,
        user=user,
        reason=reason,
        comment=comment,
        cost_price_override=cost_price_override,
    )
