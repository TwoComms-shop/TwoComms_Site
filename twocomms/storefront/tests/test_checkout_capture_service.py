"""Concurrency-safe terminal transitions for checkout captures."""

from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from orders.models import CheckoutCapture
from storefront.services.checkout_capture import mark_checkout_capture_converted


class CheckoutCaptureTransitionTests(TestCase):
    def test_absent_capture_creates_pii_free_terminal_marker(self):
        result = mark_checkout_capture_converted("absent-session")

        self.assertEqual(result, 1)
        capture = CheckoutCapture.objects.get(session_key="absent-session")
        self.assertTrue(capture.converted)
        self.assertEqual(capture.full_name, "")
        self.assertEqual(capture.phone, "")
        self.assertEqual(capture.email, "")
        self.assertEqual(capture.cart_snapshot, {})
        self.assertEqual(capture.cart_total, Decimal("0"))
        self.assertIsNone(capture.user_id)

    def test_active_capture_changes_only_converted(self):
        notified_at = timezone.now()
        capture = CheckoutCapture.objects.create(
            session_key="active-session",
            full_name="Active Buyer",
            phone="+380501112233",
            email="active@example.com",
            cart_snapshot={"item": {"product_id": 12, "qty": 2}},
            cart_total=Decimal("250.50"),
            converted=False,
            admin_notified_at=notified_at,
        )
        before = CheckoutCapture.objects.filter(pk=capture.pk).values().get()

        result = mark_checkout_capture_converted(capture.session_key)

        self.assertEqual(result, 1)
        after = CheckoutCapture.objects.filter(pk=capture.pk).values().get()
        self.assertEqual(after, {**before, "converted": True})

    def test_terminal_capture_is_byte_for_byte_noop(self):
        capture = CheckoutCapture.objects.create(
            session_key="terminal-session",
            full_name="Terminal Buyer",
            phone="+380501112233",
            email="terminal@example.com",
            cart_snapshot={"item": {"product_id": 12, "qty": 2}},
            cart_total=Decimal("250.50"),
            converted=True,
            recovery_sent_at=timezone.now(),
        )
        before = CheckoutCapture.objects.filter(pk=capture.pk).values().get()

        result = mark_checkout_capture_converted(capture.session_key)

        self.assertEqual(result, 0)
        after = CheckoutCapture.objects.filter(pk=capture.pk).values().get()
        self.assertEqual(after, before)

    def test_unique_insert_race_retries_active_row_inside_outer_atomic(self):
        capture = CheckoutCapture.objects.create(
            session_key="racing-session",
            phone="+380501112233",
            converted=False,
        )
        original_update = QuerySet.update
        update_calls = []

        def first_update_misses(queryset, **kwargs):
            update_calls.append(kwargs)
            if len(update_calls) == 1:
                return 0
            return original_update(queryset, **kwargs)

        with transaction.atomic():
            with patch.object(
                QuerySet,
                "update",
                autospec=True,
                side_effect=first_update_misses,
            ):
                result = mark_checkout_capture_converted(capture.session_key)

            self.assertEqual(result, 1)
            self.assertTrue(
                CheckoutCapture.objects.filter(
                    pk=capture.pk,
                    converted=True,
                ).exists()
            )

        self.assertEqual(len(update_calls), 2)
