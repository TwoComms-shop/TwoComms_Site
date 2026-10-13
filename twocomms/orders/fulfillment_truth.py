"""Pure delivery truth shared by order and customer-notification services."""

NOVA_POSHTA_DELIVERY_SUCCESS_CODES = frozenset({9, 10, 11})


def nova_poshta_delivery_confirmed(order) -> bool:
    from management.services.ig_split_tracking import has_split_plan, split_delivery_confirmed
    if has_split_plan(order):
        return split_delivery_confirmed(order)
    tracking_number = str(getattr(order, "tracking_number", "") or "").strip()
    try:
        status_code = int(getattr(order, "tracking_status_code", None))
    except (TypeError, ValueError):
        return False
    return bool(
        tracking_number
        and status_code in NOVA_POSHTA_DELIVERY_SUCCESS_CODES
        and getattr(order, "tracking_terminal_at", None) is not None
    )


def nova_poshta_order_fulfillment_confirmed(order) -> bool:
    """Return whether the order and carrier agree that delivery completed."""

    return bool(
        str(getattr(order, "status", "") or "") == "done"
        and nova_poshta_delivery_confirmed(order)
    )


def nova_poshta_delivery_confirmed_at(order):
    """Return the best persisted carrier-backed delivery timestamp."""

    if not nova_poshta_order_fulfillment_confirmed(order):
        return None
    from management.services.ig_split_tracking import has_split_plan, split_fulfilled_at
    if has_split_plan(order):
        return split_fulfilled_at(order)
    return (
        getattr(order, "tracking_provider_event_at", None)
        or getattr(order, "tracking_terminal_at", None)
    )
