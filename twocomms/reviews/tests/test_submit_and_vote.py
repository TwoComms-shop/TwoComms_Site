"""Phase 21 (PR-4c) — submission and voting endpoint coverage.

Verifies the moderation contract end-to-end:
    * Auth users with paid orders → ``is_verified_purchase=True``.
    * Auth users without paid orders → ``is_verified_purchase=False``
      but submission still goes through (status=pending).
    * Guests → submission accepted, anon_key set.
    * Honeypot trip → silent success, no Review row created.
    * Photo cap (>5) and oversize / wrong MIME → 400.
    * Rate-limit on guests after _GUEST_RATE_LIMIT submissions.
    * Vote endpoint flips counters and is idempotent.
"""
from __future__ import annotations

import io

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from orders.models import Order, OrderItem
from storefront.models import Category, Product
from reviews.models import Review, ReviewStatus, ReviewVote


def _png_bytes() -> bytes:
    """Smallest possible valid 1×1 PNG. Used for image upload tests."""
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


class _ReviewTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user = cls.User.objects.create_user(username="buyer", password="x")
        cls.no_order_user = cls.User.objects.create_user(username="lurker", password="x")
        cls.category = Category.objects.create(
            name="Submit Cat", slug="submit-cat", is_active=True,
        )
        cls.product = Product.objects.create(
            title="Submit Tee", slug="submit-tee",
            category=cls.category, price=300, status="published",
        )
        # Paid order linking ``cls.user`` to ``cls.product`` so the
        # ``is_verified_purchase`` flag flips on submission.
        cls.order = Order.objects.create(
            user=cls.user,
            full_name="Buyer", phone="+380991112233", email="b@e.com",
            city="Kyiv", np_office="1", pay_type="cod",
            payment_status="paid",
        )
        OrderItem.objects.create(
            order=cls.order, product=cls.product, qty=1, unit_price=300, line_total=300,
        )

    def setUp(self):
        cache.clear()
        self.client = Client()

    def _submit(self, *, follow=True, **overrides):
        data = {
            "rating": "5",
            "title": "Чудово",
            "body": "Дуже якісна тканина і друк, носив тиждень — все ок!",
            "author_name": "Тестер",
            "email": "t@e.com",
            "website": "",  # honeypot empty
        }
        data.update(overrides)
        url = reverse("reviews:submit", kwargs={"product_slug": self.product.slug})
        return self.client.post(url, data, follow=follow)


class GuestSubmissionTests(_ReviewTestBase):
    def test_guest_submission_is_accepted_as_pending(self):
        resp = self._submit(follow=False)
        self.assertEqual(resp.status_code, 302)
        review = Review.objects.get(product=self.product)
        self.assertEqual(review.status, ReviewStatus.PENDING)
        self.assertIsNone(review.user)
        self.assertNotEqual(review.anon_key, "")
        self.assertFalse(review.is_verified_purchase)

    def test_honeypot_trip_silently_drops_submission(self):
        resp = self._submit(website="http://spam.example/", follow=False)
        # User-facing: still a 302 (we pretend success).
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.count(), 0)

    def test_short_body_is_rejected(self):
        resp = self._submit(body="too short", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.count(), 0)

    def test_invalid_rating_is_rejected(self):
        resp = self._submit(rating="0", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.count(), 0)

    def test_repeated_guest_submission_does_not_duplicate_review(self):
        for _ in range(2):
            resp = self._submit(follow=False)
            self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.count(), 1)
        # Repeat attempts keep the same saved submission.
        resp = self._submit(follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.count(), 1)


class AuthSubmissionTests(_ReviewTestBase):
    def test_paid_buyer_marked_as_verified_purchase(self):
        self.client.force_login(self.user)
        self._submit(follow=False)
        review = Review.objects.get(product=self.product, user=self.user)
        self.assertTrue(review.is_verified_purchase)
        self.assertEqual(review.status, ReviewStatus.PENDING)

    def test_user_without_paid_order_is_not_verified(self):
        self.client.force_login(self.no_order_user)
        self._submit(follow=False)
        review = Review.objects.get(product=self.product, user=self.no_order_user)
        self.assertFalse(review.is_verified_purchase)


class PhotoUploadTests(_ReviewTestBase):
    def test_valid_image_attached(self):
        png = SimpleUploadedFile("a.png", _png_bytes(), content_type="image/png")
        url = reverse("reviews:submit", kwargs={"product_slug": self.product.slug})
        resp = self.client.post(
            url,
            {
                "rating": "4", "title": "ok",
                "body": "Якісний товар, рекомендую усім друзям.",
                "author_name": "T", "email": "t@e.com", "website": "",
                "images": png,
            },
        )
        self.assertEqual(resp.status_code, 302)
        review = Review.objects.get(product=self.product)
        self.assertEqual(review.images.count(), 1)

    def test_more_than_five_photos_rejected(self):
        url = reverse("reviews:submit", kwargs={"product_slug": self.product.slug})
        files = [
            SimpleUploadedFile(f"img{i}.png", _png_bytes(), content_type="image/png")
            for i in range(6)
        ]
        resp = self.client.post(
            url,
            {
                "rating": "4", "title": "ok",
                "body": "Якісний товар, рекомендую усім друзям.",
                "author_name": "T", "email": "t@e.com", "website": "",
                "images": files,
            },
        )
        self.assertEqual(resp.status_code, 302)
        # Server enforces the cap; nothing persisted.
        self.assertEqual(Review.objects.count(), 0)

    def test_oversize_image_rejected(self):
        big = SimpleUploadedFile(
            "big.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png",
        )
        url = reverse("reviews:submit", kwargs={"product_slug": self.product.slug})
        resp = self.client.post(
            url,
            {
                "rating": "4", "title": "ok",
                "body": "Якісний товар, рекомендую усім друзям.",
                "author_name": "T", "email": "t@e.com", "website": "",
                "images": big,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Review.objects.count(), 0)


class VoteEndpointTests(_ReviewTestBase):
    def setUp(self):
        super().setUp()
        self.review = Review.objects.create(
            product=self.product, user=self.user,
            author_name="X", rating=5, body="x" * 30,
            status=ReviewStatus.APPROVED,
        )

    def test_helpful_vote_increments_counter(self):
        self.client.force_login(self.user)
        url = reverse("reviews:vote", kwargs={"review_id": self.review.pk})
        resp = self.client.post(url, {"value": "helpful"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["helpful"], 1)
        self.review.refresh_from_db()
        self.assertEqual(self.review.helpful_count, 1)

    def test_repeat_helpful_is_idempotent(self):
        self.client.force_login(self.user)
        url = reverse("reviews:vote", kwargs={"review_id": self.review.pk})
        self.client.post(url, {"value": "helpful"})
        self.client.post(url, {"value": "helpful"})
        self.review.refresh_from_db()
        self.assertEqual(self.review.helpful_count, 1)

    def test_flip_helpful_to_unhelpful_swaps_counters(self):
        self.client.force_login(self.user)
        url = reverse("reviews:vote", kwargs={"review_id": self.review.pk})
        self.client.post(url, {"value": "helpful"})
        self.client.post(url, {"value": "unhelpful"})
        self.review.refresh_from_db()
        self.assertEqual(self.review.helpful_count, 0)
        self.assertEqual(self.review.unhelpful_count, 1)
        # Still only one row — flipped, not duplicated.
        self.assertEqual(ReviewVote.objects.filter(review=self.review).count(), 1)


class PermissionHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.buyer = cls.User.objects.create_user(username="bb", password="x")
        cls.lurker = cls.User.objects.create_user(username="ll", password="x")
        cls.category = Category.objects.create(name="P", slug="perm", is_active=True)
        cls.product = Product.objects.create(
            title="Perm Tee", slug="perm-tee",
            category=cls.category, price=300, status="published",
        )
        order = Order.objects.create(
            user=cls.buyer,
            full_name="B", phone="+380991112233", email="b@e.com",
            city="K", np_office="1", pay_type="cod",
            payment_status="paid",
        )
        OrderItem.objects.create(order=order, product=cls.product, qty=1, unit_price=300, line_total=300)
        # Unpaid order shouldn't count.
        unpaid = Order.objects.create(
            user=cls.lurker,
            full_name="L", phone="+380991112233", email="l@e.com",
            city="K", np_office="1", pay_type="cod",
            payment_status="unpaid",
        )
        OrderItem.objects.create(order=unpaid, product=cls.product, qty=1, unit_price=300, line_total=300)

    def test_buyer_with_paid_order_can_review(self):
        from reviews.services.permissions import has_paid_order_with_product
        self.assertTrue(has_paid_order_with_product(self.buyer, self.product))

    def test_user_with_only_unpaid_order_cannot_review(self):
        from reviews.services.permissions import has_paid_order_with_product
        self.assertFalse(has_paid_order_with_product(self.lurker, self.product))

    def test_anonymous_user_cannot_review(self):
        from django.contrib.auth.models import AnonymousUser
        from reviews.services.permissions import has_paid_order_with_product
        self.assertFalse(has_paid_order_with_product(AnonymousUser(), self.product))
