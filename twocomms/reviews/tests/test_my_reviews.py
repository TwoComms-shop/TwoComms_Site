"""Phase 21 (R12) — coverage for the personal cabinet «Мої відгуки» page.

Verifies:
    * Anonymous users are redirected to the login page (login_required).
    * The view lists only the logged-in user's reviews, irrespective of
      status (approved / pending / rejected) so users always see what
      they've submitted.
    * Other users' reviews never leak into the page.
    * The ``counts`` dict accurately aggregates per-status totals.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from storefront.models import Category, Product
from reviews.models import Review, ReviewStatus


class MyReviewsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(username="me", password="x")
        cls.other = User.objects.create_user(username="other", password="x")
        cls.category = Category.objects.create(
            name="MyReviews Cat", slug="myreviews-cat", is_active=True,
        )
        cls.product = Product.objects.create(
            title="MR Tee", slug="mr-tee",
            category=cls.category, price=300, status="published",
        )
        # Own reviews — one of each status.
        Review.objects.create(
            user=cls.user, product=cls.product, rating=5,
            body="great", status=ReviewStatus.APPROVED,
        )
        Review.objects.create(
            user=cls.user, product=cls.product, rating=4,
            body="ok", status=ReviewStatus.PENDING,
        )
        Review.objects.create(
            user=cls.user, product=cls.product, rating=1,
            body="bad", status=ReviewStatus.REJECTED,
            moderation_note="off-topic",
        )
        # Other user's review — must not leak in.
        Review.objects.create(
            user=cls.other, product=cls.product, rating=5,
            body="other", status=ReviewStatus.APPROVED,
        )

    def test_anonymous_redirected_to_login(self):
        client = Client()
        response = client.get(reverse("reviews:my_reviews"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_lists_only_current_user_reviews_all_statuses(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("reviews:my_reviews"))
        self.assertEqual(response.status_code, 200)
        bodies = [r.body for r in response.context["reviews"]]
        self.assertIn("great", bodies)
        self.assertIn("ok", bodies)
        self.assertIn("bad", bodies)
        self.assertNotIn("other", bodies)

    def test_counts_aggregate_by_status(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("reviews:my_reviews"))
        counts = response.context["counts"]
        self.assertEqual(counts["approved"], 1)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["rejected"], 1)

    def test_rejected_review_keeps_internal_moderation_note_private(self):
        client = Client()
        client.force_login(self.user)
        response = client.get(reverse("reviews:my_reviews"))
        self.assertNotContains(response, "off-topic")
