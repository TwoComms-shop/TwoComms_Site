"""Shared state transitions for abandoned-checkout captures."""

from orders.models import CheckoutCapture


def mark_checkout_capture_converted(session_key):
    """Mark an existing active capture terminal without touching other fields."""
    if not session_key:
        return 0
    return CheckoutCapture.objects.filter(
        session_key=session_key,
        converted=False,
    ).update(converted=True)
