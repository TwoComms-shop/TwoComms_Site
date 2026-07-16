"""Shared state transitions for abandoned-checkout captures."""

from django.db import IntegrityError, transaction

from orders.models import CheckoutCapture


def mark_checkout_capture_converted(session_key):
    """Persist a terminal marker while preserving any existing capture data."""
    if not session_key:
        return 0

    updated = CheckoutCapture.objects.filter(
        session_key=session_key,
        converted=False,
    ).update(converted=True)
    if updated:
        return updated

    if CheckoutCapture.objects.filter(
        session_key=session_key,
        converted=True,
    ).exists():
        return 0

    try:
        # The savepoint keeps a unique-key race from poisoning a caller's
        # surrounding COD transaction.
        with transaction.atomic():
            CheckoutCapture.objects.create(
                session_key=session_key,
                converted=True,
            )
    except IntegrityError:
        updated = CheckoutCapture.objects.filter(
            session_key=session_key,
            converted=False,
        ).update(converted=True)
        if updated:
            return updated
        if CheckoutCapture.objects.filter(
            session_key=session_key,
            converted=True,
        ).exists():
            return 0
        raise
    return 1
