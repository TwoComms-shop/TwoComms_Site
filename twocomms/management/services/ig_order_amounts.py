from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value if value is not None else "0"))
        if not amount.is_finite():
            return Decimal("0.00")
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    return amount.quantize(Decimal("0.01"))


def order_amounts(order) -> dict[str, Decimal]:
    """Return the order subtotal, discount and actual payable total.

    ``Order.total_sum`` is the pre-discount subtotal in the current order
    contract. Payment reconciliation must therefore use the final payable
    amount, never a customer-specific constant and never the subtotal alone.
    """
    if order is None:
        zero = Decimal("0.00")
        return {"subtotal": zero, "discount": zero, "payable": zero,
                "merchandise_payable": zero, "delivery": zero, "delivery_contract_valid": None}
    subtotal = _money(getattr(order, "total_sum", None))
    discount = max(_money(getattr(order, "discount_amount", None)), Decimal("0.00"))
    merchandise = max(subtotal - discount, Decimal("0.00"))
    payload = getattr(order, "payment_payload", None)
    item_rows = None
    contract = payload.get("delivery_payment") if isinstance(payload, dict) else None
    if isinstance(contract, dict) and contract.get("items_digest"):
        cached = getattr(order, "_prefetched_objects_cache", {}).get("items")
        if cached is not None:
            item_rows = list(cached)
        elif isinstance(getattr(order, "items", None), (list, tuple)):
            item_rows = order.items
        elif hasattr(getattr(order, "items", None), "all"):
            # The pure reader accepts local rows only; this established getter
            # owns the optional current quantity read, without provider IO.
            item_rows = list(order.items.all())
    from orders.services.delivery_payment import delivery_payment_snapshot
    snapshot = delivery_payment_snapshot(order, item_rows=item_rows)
    delivery = snapshot["delivery_amount"]
    valid = snapshot["valid"]
    return {"subtotal": subtotal, "discount": discount, "payable": merchandise + delivery,
            "merchandise_payable": merchandise, "delivery": delivery, "delivery_contract_valid": valid,
            "delivery_payment_snapshot": snapshot}
