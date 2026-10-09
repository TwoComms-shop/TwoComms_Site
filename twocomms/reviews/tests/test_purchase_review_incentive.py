"""Private purchase authority, honest ratings, and the actual checkout benefit."""
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.ig_bot_models import IgClient, IgFunnelResetAudit, IgUgcReward, IgUgcRewardLifetime
from orders.models import Order, OrderItem, PaymentAttempt
from orders.promo_reservations import PromoReservationError, reserve_promo_for_checkout
from reviews.models import Review, ReviewPurchaseInvitation, ReviewRewardUplift
from reviews.services.purchase_invites import (
    PurchaseReviewError, SESSION_KEY, eligible_purchase_review_items,
    ensure_purchase_review_invitation, invitation_token, invitation_url, validate_bound_review,
)
from storefront.models import Category, Product, PromoCode, PromoCodeGuestUsage


class _PurchaseReviewFixture:
    """Shared native sources; never fabricate a purchase or UGC entitlement."""
    def setUp(self):
        from management.tests_ig_w4_ugc_reward import UgcRewardTests

        UgcRewardTests.setUp(self)
        self.client.force_login(self.actor)
        cache.clear()
        self.web = Client(enforce_csrf_checks=True)
        from management.models import InstagramBotSettings
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.save(update_fields=["is_enabled", "updated_at"])
        self.ig_client.last_user_message_at = timezone.now()
        self.ig_client.save(update_fields=["last_user_message_at", "updated_at"])
        self.category = Category.objects.create(name="Purchase review", slug="purchase-review")
        self.product = Product.objects.create(title="Purchase review garment", slug="purchase-review-garment",
            category=self.category, price=Decimal("1000.00"), status="published")
        self.item = OrderItem.objects.create(order=self.order, product=self.product,
            title=self.product.title, qty=1, size="L", unit_price="1000.00", line_total="1000.00")
        self.submit_url = reverse("reviews:submit", args=[self.product.slug])
        self.state_url = reverse("reviews:state", args=[self.product.slug])
        self.review_data = {"kind": "review", "rating": "1", "author_name": "Ніка",
            "body": "Посадка для мене завелика, тканина приємна, але крій не підійшов.",
            "email": "", "website": ""}
        self.invitation = None

    def _invite(self, *, expires_at=None):
        self.invitation = ensure_purchase_review_invitation(client=self.ig_client,
            order=self.order, order_item=self.item, expires_at=expires_at)
        return self.invitation

    def _capture(self, invitation=None, *, browser=None):
        invitation = invitation or self.invitation or self._invite()
        browser = browser or self.web
        token = invitation_token(invitation)
        response = browser.get(reverse("reviews:purchase_invitation", kwargs={"token": token}), secure=True)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(response["Location"], reverse("product", args=[self.product.slug]) + "#product-reviews")
        return response

    def _post_review(self, *, browser=None, **changes):
        browser = browser or self.web
        csrf = browser.get(self.state_url, secure=True).json()["csrf"]
        return browser.post(self.submit_url, {**self.review_data, **changes}, secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest", HTTP_X_CSRFTOKEN=csrf, HTTP_ORIGIN="https://testserver")

    def _bound_review(self, **changes):
        self._capture()
        response = self._post_review(**changes)
        self.assertEqual(response.status_code, 200, response.content)
        return Review.objects.get(pk=response.json()["review_id"])

    def _approve(self, review, *, execute_callbacks=True):
        # Exercise the same moderator endpoint used by the management UI.
        callbacks = self.captureOnCommitCallbacks(execute=execute_callbacks) if hasattr(self, "captureOnCommitCallbacks") else nullcontext()
        with callbacks:
            response = self.client.post(reverse("admin_review_action", args=[review.pk]),
                {"action": "approve", "note": "Опублікувати чесну оцінку без вимоги позитивного відгуку."}, secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        review.refresh_from_db()
        self.assertEqual(review.status, "approved")
        return review

    def _award(self):
        from management.services.ig_ugc_rewards import award_ugc_reward

        reward, created = award_ugc_reward(client=self.ig_client, order=self.order, actor=self.actor,
            evidence_message_id=self.evidence.pk, review_note="Verified post-delivery story evidence")
        self.assertTrue(created)
        return reward


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class PurchaseReviewCapabilityTests(_PurchaseReviewFixture, TestCase):
    def test_logged_in_unrelated_review_does_not_consume_invited_owner_identity(self):
        from django.contrib.auth import get_user_model
        from reviews.services.purchase_invites import invitation_submission_identity

        account = get_user_model().objects.create_user(username="unrelated-reviewer", password="test-password")
        self.web.force_login(account)
        public = self._post_review(email="unrelated@example.com")
        self.assertEqual(public.status_code, 200, public.content)
        unrelated = Review.objects.get(pk=public.json()["review_id"])
        self.assertEqual(unrelated.user_id, account.pk)
        self.assertIsNone(unrelated.purchase_invitation_id)
        self._capture()
        private = self._post_review()
        self.assertEqual(private.status_code, 200, private.content)
        invited = Review.objects.get(pk=private.json()["review_id"])
        self.assertIsNone(invited.user_id)
        self.assertEqual(invited.purchase_invitation_id, self.invitation.pk)
        self.assertEqual(invited.submission_identity, invitation_submission_identity(self.invitation))
        self.assertNotEqual(invited.submission_identity, unrelated.submission_identity)
        self.assertTrue(invited.is_verified_purchase)
        self.assertEqual(Review.objects.count(), 2)

    def test_private_link_exchanges_capability_and_cleans_url_without_account_login(self):
        invitation = self._invite()
        token = invitation_token(invitation)
        response = self._capture(invitation)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("noindex", response["X-Robots-Tag"])
        self.assertNotIn("_auth_user_id", self.web.session)
        self.assertEqual(self.web.session[SESSION_KEY][str(self.product.pk)]["id"], str(invitation.pk))
        self.assertNotIn(token, str(dict(self.web.session)))
        clean = self.web.get(response["Location"], secure=True)
        self.assertEqual(clean.status_code, 200)
        html = clean.content.decode()
        for secret in (token, invitation.token_hash, invitation.identity_digest,
                       self.order.phone, self.order.full_name, self.order.order_number, self.ig_client.igsid):
            self.assertNotIn(secret, html)
        self.assertEqual(Review.objects.count(), 0)

    def test_one_star_guest_review_is_bound_pending_verified_and_moderation_preserves_rating(self):
        review = self._bound_review(rating="1")
        self.assertEqual(review.status, "pending")
        self.assertEqual(review.rating, 1)
        self.assertIsNone(review.user_id)
        self.assertEqual(review.email, "")
        self.assertEqual(review.purchase_invitation_id, self.invitation.pk)
        self.assertTrue(review.is_verified_purchase)
        self.assertTrue(review.is_incentivized_review)
        self.assertTrue(review.purchase_proof_signature)
        validate_bound_review(review, require_approved=False)
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertFalse(PromoCode.objects.exists())
        self._approve(review)
        self.assertEqual(review.rating, 1)
        proof = validate_bound_review(review, client=self.ig_client, order=self.order)
        self.assertEqual(proof["rating"], 1)
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertFalse(PromoCode.objects.exists(), "Review alone must never issue a standalone 5% code")



    def test_blank_rating_and_comment_cannot_consume_purchase_review_capability(self):
        self._capture()
        for data in ({"rating": ""}, {"kind": "comment", "rating": "5"}):
            with self.subTest(data=data):
                response = self._post_review(**data)
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Review.objects.exists())
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertFalse(PromoCode.objects.exists())


    def test_invalid_signature_never_installs_session_authority(self):
        invitation = self._invite()
        token = invitation_token(invitation)
        altered = token[:-1] + ("A" if token[-1] != "A" else "B")
        response = self.web.get(reverse("reviews:purchase_invitation", kwargs={"token": altered}), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(SESSION_KEY, self.web.session)
        for secret in (str(invitation.pk), self.order.phone, self.order.order_number, self.ig_client.igsid):
            self.assertNotIn(secret, response.content.decode())

    def test_private_session_has_no_authority_for_another_product(self):
        self._capture()
        other = Product.objects.create(title="Another garment", slug="another-review-garment",
            category=self.category, price=500, status="published")
        csrf = self.web.get(reverse("reviews:state", args=[other.slug]), secure=True).json()["csrf"]
        response = self.web.post(reverse("reviews:submit", args=[other.slug]),
            {**self.review_data, "email": "another@example.com"}, secure=True,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest", HTTP_X_CSRFTOKEN=csrf, HTTP_ORIGIN="https://testserver")
        self.assertEqual(response.status_code, 200, response.content)
        review = Review.objects.get(product=other)
        self.assertIsNone(review.purchase_invitation_id)
        self.assertFalse(review.is_verified_purchase)
        self.assertFalse(review.purchase_proof_signature)

    def test_expired_invitation_is_rejected_without_mutating_immutable_proof(self):
        expiry = timezone.now() + timedelta(minutes=1)
        invitation = self._invite(expires_at=expiry)
        token = invitation_token(invitation)
        with patch("django.utils.timezone.now", return_value=expiry):
            response = self.web.get(reverse("reviews:purchase_invitation", kwargs={"token": token}), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(SESSION_KEY, self.web.session)
        invitation.refresh_from_db()
        self.assertEqual(invitation.expires_at, expiry)

    def test_reset_after_capture_revokes_post_authority_and_cannot_fall_back_to_unverified_review(self):
        self._capture()
        IgFunnelResetAudit.objects.create(client=self.ig_client,
            reset_after_message_id=self.evidence.pk, actor=self.actor, reason="New purchase context")
        response = self._post_review()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Review.objects.exists())

    def test_erasure_after_capture_revokes_post_authority(self):
        self._capture()
        self.ig_client.privacy_erasure_started_at = timezone.now()
        self.ig_client.save(update_fields=["privacy_erasure_started_at", "updated_at"])
        response = self._post_review()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Review.objects.exists())

    def test_assignment_version_change_revokes_post_authority(self):
        from management.services.ig_order_assignments import unlink_order_from_client

        self._capture()
        unlink_order_from_client(self.order, client=self.ig_client, actor=self.actor,
            expected_version=self.assignment.version, reason_code="manager_correction", reason="Wrong purchase owner")
        response = self._post_review()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Review.objects.exists())

    def test_service_allows_honest_review_without_bonus_but_refund_revokes_purchase_authority(self):
        from management.tests_ig_w4_ugc_reward import UgcRewardTests

        invitation = self._invite()
        token = invitation_token(invitation)
        case = UgcRewardTests._open_service_case(self, "review-invite", order=self.order)
        self._capture(invitation)
        private_state = self.web.get(self.state_url, secure=True).json()["purchase_review_context"]
        self.assertFalse(private_state["bonus_available"])
        response = self._post_review()
        self.assertEqual(response.status_code, 200, response.content)
        review = Review.objects.get(pk=response.json()["review_id"])
        self.assertEqual(review.status, "pending")
        self.assertEqual(review.rating, 1)
        self.assertTrue(review.is_verified_purchase)
        self.assertEqual(review.purchase_invitation_id, invitation.pk)
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertFalse(PromoCode.objects.exists())
        case.status = "completed"
        case.save(update_fields=["status", "updated_at"])
        self.order.payment_status = "refunded"
        self.order.save(update_fields=["payment_status"])
        response = self.web.get(reverse("reviews:purchase_invitation", kwargs={"token": token}), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Review.objects.count(), 1)

    def test_custom_and_dtf_lines_cannot_create_catalog_review_authority(self):
        custom = OrderItem.objects.create(order=self.order, product=self.product, title="Custom garment",
            qty=1, unit_price="100.00", line_total="100.00", is_custom=True)
        film = OrderItem.objects.create(order=self.order, product=self.product, title="DTF film",
            qty=1, unit_price="320.00", line_total="320.00", item_kind="dtf_film", film_length_m="1.00")
        for item in (custom, film):
            with self.subTest(item=item.pk), self.assertRaises(PurchaseReviewError):
                ensure_purchase_review_invitation(client=self.ig_client, order=self.order, order_item=item)
        self.assertEqual([row.pk for row in eligible_purchase_review_items(client=self.ig_client, order=self.order)], [self.item.pk])
        self.assertFalse(ReviewPurchaseInvitation.objects.exists())


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class PurchaseReviewCheckoutIncentiveTests(_PurchaseReviewFixture, TestCase):
    def test_ugc_before_review_uplifts_same_single_use_code_with_original_expiry(self):
        from management.services.ig_review_reward import apply_review_uplift, effective_reward_percent

        reward = self._award()
        base_code = reward.promo_code.code
        original_expiry = reward.promo_code.valid_until
        review = self._bound_review(rating="1")
        self._approve(review)
        result = apply_review_uplift(review.pk)
        self.assertTrue(result["applied"], result)
        self.assertFalse(result["created"], "The moderation callback must already have applied the component")
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.discount_percent, 10)
        self.assertEqual(effective_reward_percent(reward), 15)
        self.assertEqual(reward.promo_code.code, base_code)
        self.assertEqual(reward.promo_code.discount_value, Decimal("15.00"))
        self.assertEqual(reward.promo_code.valid_until, original_expiry)
        self.assertEqual(reward.promo_code.max_uses, 1)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        self.assertEqual(IgUgcReward.objects.count(), 1)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        reservation = reserve_promo_for_checkout(code=base_code, user=None, total_amount=Decimal("1000.00"))
        self.assertEqual(reservation.discount, Decimal("150.00"))
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.current_uses, 1)
        usage = PromoCodeGuestUsage.objects.get(promo_code=reward.promo_code)
        self.assertEqual(usage.state, "reserved")
        self.assertEqual(reservation.event_state["promo_reservation"]["guest_usage_id"], usage.pk)
        with self.assertRaises(PromoReservationError):
            reserve_promo_for_checkout(code=base_code, user=None, total_amount=Decimal("1000.00"))


    def test_approved_review_before_ugc_never_issues_five_and_initial_ugc_grant_has_only_fifteen_message(self):
        from management.services.ig_review_reward import effective_reward_percent

        review = self._bound_review(rating="2")
        self._approve(review)
        self.assertFalse(PromoCode.objects.exists())
        self.assertFalse(ReviewRewardUplift.objects.exists())
        reward = self._award()
        reward.refresh_from_db()
        self.assertEqual(reward.discount_percent, 10)
        self.assertEqual(effective_reward_percent(reward), 15)
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        deliveries = list(reward.deliveries.all())
        pending = [row for row in deliveries if row.state in {"pending", "waiting_window"}]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].discount_percent_snapshot, 15)
        self.assertIn("15%", pending[0].message_snapshot)
        self.assertEqual(reserve_promo_for_checkout(code=reward.promo_code.code,
            user=None, total_amount=Decimal("1000.00")).discount, Decimal("150.00"))


    def test_duplicate_capability_submission_has_one_review_and_one_component(self):
        self._award()
        review = self._bound_review()
        duplicate = self._post_review()
        self.assertEqual(duplicate.status_code, 409, duplicate.content)
        self._approve(review)
        self._approve(review)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)


    def test_guest_online_checkout_reserves_the_actual_fifteen_percent_amount(self):
        from storefront.tests.test_ugc_guest_promo import UGCGuestCheckoutViewTests

        reward = self._award()
        review = self._bound_review()
        self._approve(review)
        session = self.web.session
        session["cart"] = {f"{self.product.pk}:L": {"product_id": self.product.pk, "qty": 1, "size": "L"}}
        session.save()
        csrf = self.web.get(self.state_url, secure=True).json()["csrf"]
        applied = self.web.post(reverse("apply_promo_code"), {"promo_code": reward.promo_code.code},
            secure=True, HTTP_X_CSRFTOKEN=csrf, HTTP_ORIGIN="https://testserver")
        self.assertEqual(applied.status_code, 200, applied.content)
        payload = UGCGuestCheckoutViewTests._delivery(self)
        payload.update(full_name="Guest Online Buyer", phone="0991234567", pay_type="online_full")
        with (
            patch("storefront.views.monobank._monobank_api_request") as provider,
            patch("storefront.views.monobank.get_facebook_conversions_service") as facebook,
            patch("storefront.views.monobank.record_lead"),
            patch("storefront.views.monobank.record_initiate_checkout"),
            patch("storefront.views.monobank.link_order_to_utm"),
            patch("orders.telegram_notifications.TelegramNotifier.send_new_order_notification", return_value=True),
        ):
            provider.return_value = {"invoiceId": "review-uplift-invoice", "pageUrl": "https://pay.example/review-uplift-invoice"}
            facebook.return_value = Mock()
            response = self.web.post(reverse("monobank_create_invoice"), data=json.dumps(payload),
                content_type="application/json", secure=True, HTTP_X_CSRFTOKEN=csrf, HTTP_ORIGIN="https://testserver")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(provider.call_count, 1)
        attempt = PaymentAttempt.objects.get(monobank_invoice_id="review-uplift-invoice")
        self.assertIsNone(attempt.user_id)
        self.assertEqual(attempt.gross_amount, Decimal("1000.00"))
        self.assertEqual(attempt.discount_amount, Decimal("150.00"))
        self.assertEqual(attempt.payable_amount, Decimal("850.00"))
        self.assertEqual(attempt.payment_amount, Decimal("850.00"))
        self.assertEqual(attempt.promo_code_id, reward.promo_code_id)
        self.assertEqual(attempt.event_state["promo_reservation"]["state"], "reserved")
        self.assertEqual(PromoCodeGuestUsage.objects.get(promo_code_id=reward.promo_code_id).state, "reserved")
