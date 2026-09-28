"""One presentation contract for bound and separately linked order delivery."""
from orders.fulfillment_truth import nova_poshta_delivery_confirmed_at


def delivery_progress(order, evidence_ref):
    delivered = bool(nova_poshta_delivery_confirmed_at(order))
    cancelled = order.status == 'cancelled'
    try:
        code = int(order.tracking_status_code or 0)
    except (TypeError, ValueError):
        code = 0
    at_branch = bool(order.tracking_number and order.tracking_provider_event_at and code == 7)
    return {
        'step': 0 if cancelled else 4 if delivered else 3 if at_branch else 2 if order.status == 'ship' else 1,
        'cancelled': cancelled, 'evidence_refs': [evidence_ref],
        'completion_unverified': order.status == 'done' and not delivered,
        'carrier_pending_while_shipped': order.status == 'ship' and code == 1,
    }
