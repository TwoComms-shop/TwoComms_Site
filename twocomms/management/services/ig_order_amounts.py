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
    delivery = Decimal("0.00")
    payload = getattr(order, "payment_payload", None)
    contract = payload.get("instagram_delivery_contract") if isinstance(payload, dict) else None
    valid = None
    if isinstance(contract, dict):
        delivery_amount = _money(contract.get("delivery_amount"))
        ids = [contract.get(key) for key in ("review_id", "decision_id", "actor_id")]
        evidence_ids = contract.get("evidence_message_ids")
        valid = bool(
            all(type(value) is int and value > 0 for value in ids)
            and isinstance(evidence_ids, list) and evidence_ids
            and all(type(value) is int and value > 0 for value in evidence_ids)
            and type(contract.get("prepaid")) is bool
            and contract.get("payer_type") == ("Sender" if contract.get("prepaid") else "Recipient")
            and _money(contract.get("merchandise_total")) == merchandise
            and delivery_amount > 0
            and _money(contract.get("payable_total")) == merchandise + delivery_amount
        )
        if valid:
            delivery = delivery_amount
    return {"subtotal": subtotal, "discount": discount, "payable": merchandise + delivery,
            "merchandise_payable": merchandise, "delivery": delivery, "delivery_contract_valid": valid}
