"""Phase 21 (PR-4c3) — signal handlers.

Verifies:
    * On creating a pending Review, the Telegram notifier is called
      iff token+chat are configured (mocked).
    * Status transition pending→approved fires IndexNow exactly once
      with the correct product URL.
    * Re-saving an already-approved review does NOT re-fire IndexNow.
    * Failures in the external service NEVER raise out of save().
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from storefront.models import Category, Product
from reviews.models import Review, ReviewStatus


@override_settings(
    SITE_BASE_URL="https://twocomms.shop",
    TELEGRAM_BOT_TOKEN="fake-token",
    TELEGRAM_CHAT_ID="111",
)
class ReviewSignalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user = cls.User.objects.create_user(username="signal", password="x")
        cls.category = Category.objects.create(
            name="Sig Cat", slug="sig-cat", is_active=True,
        )
        cls.product = Product.objects.create(
            title="Signal Tee", slug="signal-tee",
            category=cls.category, price=300, status="published",
        )

    def _make_review(self, **kwargs):
        defaults = dict(
            product=self.product,
            user=self.user,
            author_name="X",
            rating=5,
            body="x" * 30,
        )
        defaults.update(kwargs)
        return Review.objects.create(**defaults)

    @patch("reviews.signals._send_pending_telegram")
    def test_pending_creation_fires_telegram_notifier(self, mock_send):
        with self.captureOnCommitCallbacks(execute=True):
            self._make_review(status=ReviewStatus.PENDING)
        self.assertTrue(mock_send.called)

    @patch("reviews.signals._send_pending_telegram")
    def test_status_update_does_not_re_fire_pending_notify(self, mock_send):
        with self.captureOnCommitCallbacks(execute=True):
            review = self._make_review(status=ReviewStatus.PENDING)
        mock_send.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            review.mark_approved()
        self.assertFalse(mock_send.called)

    @patch("reviews.signals._submit_indexnow_for_product")
    def test_pending_to_approved_pings_indexnow_once(self, mock_indexnow):
        review = self._make_review(status=ReviewStatus.PENDING)
        mock_indexnow.assert_not_called()
        with self.captureOnCommitCallbacks(execute=True):
            review.mark_approved()
        mock_indexnow.assert_called_once_with(self.product)

    @patch("reviews.signals._submit_indexnow_for_product")
    def test_re_saving_approved_does_not_re_ping_indexnow(self, mock_indexnow):
        with self.captureOnCommitCallbacks(execute=True):
            review = self._make_review(status=ReviewStatus.APPROVED)
        # First create already approved → ping fires once.
        self.assertEqual(mock_indexnow.call_count, 1)
        review.body = (review.body or "") + " edit"
        with self.captureOnCommitCallbacks(execute=True):
            review.save(update_fields=["body", "updated_at"])
        # No additional ping — status didn't change.
        self.assertEqual(mock_indexnow.call_count, 1)

    @patch("reviews.signals._send_pending_telegram", side_effect=RuntimeError("boom"))
    def test_telegram_exception_does_not_break_save(self, _mock_send):
        # Must not raise.
        with self.captureOnCommitCallbacks(execute=True):
            review = self._make_review(status=ReviewStatus.PENDING)
        self.assertEqual(review.status, ReviewStatus.PENDING)

    @patch("reviews.signals._submit_indexnow_for_product", side_effect=RuntimeError("boom"))
    def test_indexnow_exception_does_not_break_save(self, _mock_indexnow):
        review = self._make_review(status=ReviewStatus.PENDING)
        # Must not raise on transition.
        with self.captureOnCommitCallbacks(execute=True):
            review.mark_approved()
        self.assertEqual(review.status, ReviewStatus.APPROVED)
