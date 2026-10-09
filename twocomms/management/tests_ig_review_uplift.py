"""Native signed review uplift, delivery history, and checkout serialization."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import os
from threading import Barrier, Event
from types import MethodType
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.ig_bot_models import IgClient, IgUgcReward, IgUgcRewardDelivery, IgUgcRewardLifetime
from management.services.ig_review_reward import apply_review_uplift, effective_reward_percent
from management.services.ig_ugc_rewards import process_external_ugc_reward_delivery, ugc_identity_already_rewarded
from management.tests_ig_marketing_consent import _MarketingConsentFixture
from orders.models import Order, PaymentAttempt
from orders.promo_reservations import PromoReservationError, consume_payment_attempt_promo, reserve_promo_for_checkout
from reviews.models import Review, ReviewPurchaseInvitation, ReviewRewardUplift
from reviews.tests.test_purchase_review_incentive import _PurchaseReviewFixture
from storefront.models import PromoCode, PromoCodeGuestUsage


def _enable_test_business_consent(test):
    from management.models import InstagramBotSettings

    test.enterContext(override_settings(IG_POST_PURCHASE_BUSINESS_CONSENT_ENABLED=True))
    test.enterContext(patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"}))
    test.settings_row = InstagramBotSettings.load()
    test.settings_row.ig_user_id = "review-consent-provider"
    test.settings_row.save(update_fields=["ig_user_id", "updated_at"])
    test.serial = 0


class _ReviewUpliftFixture(_PurchaseReviewFixture, _MarketingConsentFixture):
    def _approved_without_worker(self):
        from management.ig_bot_models import IgUgcRewardLifecycleJob

        review = self._bound_review()
        # Moderation is native; defer only the asynchronous on_commit worker.
        with patch("management.services.ig_review_reward.process_review_uplift_job",
                   return_value={"state": "deferred"}) as worker:
            self._approve(review)
        job = IgUgcRewardLifecycleJob.objects.get(source=f"review_uplift:{review.pk:x}")
        worker.assert_called_once_with(job.pk, using="default")
        self.assertFalse(ReviewRewardUplift.objects.exists())
        return review

    def _unchanged_ten(self, reward, expiry):
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.discount_percent, 10)
        self.assertEqual(reward.promo_code.discount_value, Decimal("10.00"))
        self.assertEqual(reward.promo_code.valid_until, expiry)
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertEqual(PromoCode.objects.count(), 1)

    def _send(self, delivery, mid):
        from management.services.instagram_bot import ProviderDeliveryReceipt

        receipt = ProviderDeliveryReceipt(ok=True, kind="", provider_message_id=mid,
            provider_message_ids=(mid,), planned_chunk_count=1, delivered_chunk_count=1,
            request_text=delivery.message_snapshot)
        def transport(settings, recipient, text, **kwargs):
            self.assertEqual(text, delivery.message_snapshot)
            self.assertEqual(recipient, self.ig_client.igsid)
            self.assertTrue(kwargs["return_receipt"])
            with kwargs["provider_request_boundary_factory"](delivered_chunk_count=0,
                    provider_message_ids=(), planned_chunk_count=1) as allowed:
                self.assertTrue(allowed, "Fresh price/source/review authority must permit this actual send")
                kwargs["provider_message_callback"](mid)
            return receipt

        with (patch("management.services.instagram_bot.send_text", side_effect=transport) as send,
              patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("Unexpected Meta I/O")) as http):
            state = process_external_ugc_reward_delivery(delivery.pk)
        delivery.refresh_from_db()
        self.assertEqual(state, IgUgcRewardDelivery.State.SENT, delivery.last_error)
        send.assert_called_once()
        http.assert_not_called()
        self.assertEqual(delivery.provider_message_ids, [mid])
        return send.call_args


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    STORAGES={"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
              "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class ReviewUpliftAuthorityTests(_ReviewUpliftFixture, TestCase):
    def test_pending_review_has_no_uplift_authority(self):
        reward = self._award()
        expiry = reward.promo_code.valid_until
        review = self._bound_review()
        result = apply_review_uplift(review.pk)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "proof_invalid")
        self._unchanged_ten(reward, expiry)

    def test_real_guest_reservation_blocks_uplift_without_changing_original_discount(self):
        reward = self._award()
        expiry = reward.promo_code.valid_until
        review = self._approved_without_worker()
        reservation = reserve_promo_for_checkout(code=reward.promo_code.code, user=None, total_amount=Decimal("1000"))
        self.assertEqual(reservation.discount, Decimal("100"))
        result = apply_review_uplift(review.pk)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "promo_reserved")
        self._unchanged_ten(reward, expiry)
        self.assertEqual(PromoCodeGuestUsage.objects.get(promo_code_id=reward.promo_code_id).state, "reserved")

    def test_consumed_guest_checkout_never_grants_another_five(self):
        reward = self._award()
        expiry = reward.promo_code.valid_until
        review = self._approved_without_worker()
        reservation = reserve_promo_for_checkout(code=reward.promo_code.code, user=None, total_amount=Decimal("1000"))
        purchased = Order.objects.create(order_number="REVIEW-NEXT-01", full_name="Guest",
            phone="0991234567", total_sum=900, payment_status="paid")
        attempt = PaymentAttempt.objects.create(order=purchased, promo_code=reservation.promo,
            gross_amount=1000, discount_amount=reservation.discount, payable_amount=900,
            payment_amount=900, event_state=reservation.event_state)
        self.assertTrue(consume_payment_attempt_promo(attempt, order=purchased))
        attempt.save(update_fields=["event_state"])
        self.assertEqual(PromoCodeGuestUsage.objects.get(promo_code_id=reward.promo_code_id).state, "consumed")
        result = apply_review_uplift(review.pk)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "promo_consumed")
        self._unchanged_ten(reward, expiry)

    def test_original_expiration_is_exclusive_and_never_renewed_by_review(self):
        reward = self._award()
        expiry = reward.promo_code.valid_until
        review = self._approved_without_worker()
        result = apply_review_uplift(review.pk, now=expiry)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "promo_expired")
        self._unchanged_ten(reward, expiry)

    def test_service_case_keeps_honest_review_but_blocks_bonus_and_send(self):
        from management.tests_ig_w4_ugc_reward import UgcRewardTests

        reward = self._award()
        expiry = reward.promo_code.valid_until
        with self.captureOnCommitCallbacks(execute=True):
            UgcRewardTests._open_service_case(self, "uplift-block", order=self.order)
        review = self._approved_without_worker()
        result = apply_review_uplift(review.pk)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "service_case_open")
        self.assertEqual(review.rating, 1)
        self.assertEqual(review.status, "approved")
        self._unchanged_ten(reward, expiry)
        with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Incentive during complaint")) as send:
            process_external_ugc_reward_delivery(reward.deliveries.get(generation=1).pk)
        send.assert_not_called()

    def test_real_external_five_percent_grant_is_not_a_ten_percent_base(self):
        from management.services.ig_ugc_rewards import award_external_ugc_reward
        from management.tests_ig_ugc_external_reward import ExternalUGCRewardTests

        self._owned_image = MethodType(ExternalUGCRewardTests._owned_image, self)
        assessment = ExternalUGCRewardTests._create_assessment(self, "review-base-five", client=self.ig_client)
        reward, created = award_external_ugc_reward(client=self.ig_client, assessment=assessment,
            actor=self.actor, review_note="Verified external story", discount_percent=5)
        self.assertTrue(created)
        review = self._approved_without_worker()
        result = apply_review_uplift(review.pk)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "base_not_10")
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.discount_value, Decimal("5"))
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertEqual(PromoCode.objects.count(), 1)

    def test_processing_base_message_requires_reconciliation_before_uplift(self):
        self._assert_uncertain_delivery_blocks("processing")

    def test_ambiguous_base_message_requires_reconciliation_and_is_never_replayed(self):
        self._assert_uncertain_delivery_blocks("ambiguous")

    def _assert_uncertain_delivery_blocks(self, state):
        reward = self._award()
        expiry = reward.promo_code.valid_until
        review = self._approved_without_worker()
        delivery = reward.deliveries.get(generation=1)
        original = delivery.message_snapshot
        delivery.state = state
        delivery.attempts = 1
        delivery.save(update_fields=["state", "attempts", "updated_at"])
        result = apply_review_uplift(review.pk)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "delivery_reconciliation_required")
        self._unchanged_ten(reward, expiry)
        if state == "ambiguous":
            with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Ambiguous replay")) as send:
                self.assertEqual(process_external_ugc_reward_delivery(delivery.pk), "ambiguous")
            send.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.message_snapshot, original)
        self.assertEqual(reward.deliveries.count(), 1)

    def test_sent_ten_history_is_immutable_and_fifteen_uses_new_delivery_generation(self):
        reward = self._award()
        old = reward.deliveries.get(generation=1)
        original = (old.message_snapshot, old.discount_percent_snapshot, old.valid_until_snapshot)
        self._send(old, "review-base-ten-mid")
        review = self._bound_review()
        self._approve(review)
        new = reward.deliveries.get(generation=2)
        self.assertEqual(new.discount_percent_snapshot, 15)
        self.assertEqual(new.valid_until_snapshot, original[2])
        self._send(new, "review-uplift-fifteen-mid")
        old.refresh_from_db()
        self.assertEqual((old.message_snapshot, old.discount_percent_snapshot, old.valid_until_snapshot), original)
        self.assertEqual(old.state, "sent")
        self.assertEqual(old.provider_message_ids, ["review-base-ten-mid"])
        with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Sent replay")) as send:
            self.assertEqual(process_external_ugc_reward_delivery(new.pk), "sent")
        send.assert_not_called()
        self.assertEqual(reward.deliveries.count(), 2)

    def test_unsent_base_is_superseded_without_rewriting_its_snapshot(self):
        reward = self._award()
        old = reward.deliveries.get(generation=1)
        original = old.message_snapshot
        review = self._bound_review()
        self._approve(review)
        old.refresh_from_db()
        self.assertEqual(old.state, "failed")
        self.assertEqual(old.last_error, "superseded_by_reward_generation")
        self.assertEqual(old.message_snapshot, original)
        self.assertEqual(old.attempts, 0)
        new = reward.deliveries.get(generation=2)
        with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Superseded replay")) as send:
            process_external_ugc_reward_delivery(old.pk)
        send.assert_not_called()
        result = apply_review_uplift(review.pk)
        self.assertTrue(result["applied"], result)
        self.assertFalse(result["created"])
        self.assertEqual(result["delivery_id"], new.pk)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)

    def test_rejection_holds_both_checkout_channels_and_reapproval_keeps_original_component(self):
        reward = self._award()
        review = self._bound_review()
        self._approve(review)
        component = ReviewRewardUplift.objects.get(reward=reward)
        original = (component.pk, component.proof_signature, reward.promo_code.code, reward.promo_code.valid_until)
        delivery_id = reward.deliveries.get(generation=2).pk
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("admin_review_action", args=[review.pk]), {"action": "reject"}, secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        for user in (None, self.actor):
            with self.subTest(authenticated=user is not None), self.assertRaises(PromoReservationError):
                reserve_promo_for_checkout(code=reward.promo_code.code, user=user, total_amount=Decimal("1000"))
        self._approve(review)
        self._approve(review)
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        component.refresh_from_db()
        self.assertEqual((component.pk, component.proof_signature, reward.promo_code.code, reward.promo_code.valid_until), original)
        self.assertEqual(effective_reward_percent(reward), 15)
        self.assertTrue(reward.promo_code.is_active)
        self.assertEqual(reward.deliveries.get(generation=2).pk, delivery_id)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        self.assertEqual(reserve_promo_for_checkout(code=reward.promo_code.code, user=self.actor,
            total_amount=Decimal("1000")).discount, Decimal("150"))

    def test_initial_insert_forged_component_cannot_authorize_authenticated_or_guest_fifteen(self):
        reward = self._award()
        review = self._approved_without_worker()
        ReviewRewardUplift.objects.create(reward=reward, review=review, added_percent=5,
            proof_snapshot={"schema": "ugc-star-review-uplift.v1"}, proof_digest="0" * 64,
            signing_key_id="untrusted", proof_signature="0" * 64)
        reward.promo_code.discount_value = 15
        reward.promo_code.save(update_fields=["discount_value", "updated_at"])
        for user in (None, self.actor):
            with self.subTest(authenticated=user is not None), self.assertRaises(PromoReservationError):
                reserve_promo_for_checkout(code=reward.promo_code.code, user=user, total_amount=Decimal("1000"))
        self.assertEqual(PromoCodeGuestUsage.objects.count(), 0)
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.current_uses, 0)

    def test_v2_freezes_purchased_product_link_and_rechecks_reservation_at_dispatch(self):
        from management.services.ig_post_purchase_invitation import (
            INVITATION_PAYLOAD_KEY, post_purchase_invitation_block_reason, post_purchase_invitation_snapshot)

        _enable_test_business_consent(self)
        self.grant_business_consent(self.ig_client, self.order, self.assignment)
        reward = self._award()
        metadata, text = post_purchase_invitation_snapshot(self.ig_client, self.order, "uk", assignment=self.assignment)
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["mode"], "review_and_uplift")
        self.assertIn("15%", text)
        self.assertEqual(len(metadata["reviews"]), 1)
        self.assertIn(metadata["reviews"][0]["url"], text)
        payload = {INVITATION_PAYLOAD_KEY: metadata}
        self.assertEqual(post_purchase_invitation_block_reason(self.ig_client, self.order, payload,
            text, "uk", assignment=self.assignment), "")
        reserve_promo_for_checkout(code=reward.promo_code.code, user=None, total_amount=Decimal("1000"))
        self.assertEqual(post_purchase_invitation_block_reason(self.ig_client, self.order, payload,
            text, "uk", assignment=self.assignment), "post_purchase_eligibility_unknown")
        self.assertFalse(ReviewRewardUplift.objects.exists())

    def test_late_review_rejection_before_first_provider_request_sends_no_reward(self):
        self._assert_review_rejection_at_provider_boundary(partial=False)

    def test_partial_reward_receipt_before_review_rejection_remains_ambiguous_without_replay(self):
        self._assert_review_rejection_at_provider_boundary(partial=True)

    def _assert_review_rejection_at_provider_boundary(self, *, partial):
        from management.services.instagram_bot import ProviderDeliveryReceipt

        reward = self._award()
        review = self._bound_review()
        self._approve(review)
        delivery = reward.deliveries.get(generation=2)
        original = delivery.message_snapshot
        mids = ("review-uplift-partial-mid",) if partial else ()

        def transport(settings, recipient, text, **kwargs):
            self.assertEqual(text, original)
            factory = kwargs["provider_request_boundary_factory"]
            if partial:
                with factory(delivered_chunk_count=0, provider_message_ids=(), planned_chunk_count=2) as allowed:
                    self.assertTrue(allowed)
                    kwargs["provider_message_callback"](mids[0])
            with self.captureOnCommitCallbacks(execute=True):
                current = Review.objects.get(pk=review.pk)
                current.mark_rejected(by=self.actor, note="Withdraw publication before the next provider request")
            with factory(delivered_chunk_count=int(partial), provider_message_ids=mids,
                    planned_chunk_count=2 if partial else 1) as allowed:
                self.assertFalse(allowed, "Withdrawn review cannot authorize the next coupon request")
            return ProviderDeliveryReceipt(ok=False, kind="cancelled", provider_message_ids=mids,
                planned_chunk_count=2 if partial else 1, delivered_chunk_count=int(partial))

        with (patch("management.services.instagram_bot.send_text", side_effect=transport) as send,
              patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("Unexpected Meta I/O")) as http):
            state = process_external_ugc_reward_delivery(delivery.pk)
        send.assert_called_once()
        http.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(state, "ambiguous" if partial else "failed", delivery.last_error)
        self.assertEqual(delivery.provider_message_ids, list(mids))
        self.assertEqual(delivery.message_snapshot, original)
        self.assertIsNotNone(delivery.completed_at)
        if partial:
            self.assertIn("partial delivery", delivery.last_error)
        with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Terminal delivery replay")) as send:
            process_external_ugc_reward_delivery(delivery.pk)
        send.assert_not_called()
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class ReviewUpliftSerializationTests(_ReviewUpliftFixture, TransactionTestCase):
    def _native_only(self):
        if connection.vendor != "mysql":
            self.skipTest("Native InnoDB lock serialization requires MariaDB")

    def test_duplicate_workers_create_exactly_one_component_and_one_generation(self):
        self._native_only()
        reward = self._award()
        review = self._approved_without_worker()
        barrier = Barrier(2)

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return apply_review_uplift(review.pk)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertTrue(all(row["applied"] for row in results), results)
        self.assertEqual(sum(row["created"] for row in results), 1)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        self.assertEqual(reward.deliveries.filter(generation=2).count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 1)

    def test_checkout_lock_wins_before_uplift_and_preserves_reserved_ten(self):
        self._native_only()
        reward = self._award()
        review = self._approved_without_worker()
        reached_lock, finished = Event(), Event()

        def worker():
            close_old_connections()
            def observe(execute, sql, params, many, context):
                if "management_igclient" in sql.lower() and "for update" in sql.lower():
                    reached_lock.set()
                return execute(sql, params, many, context)
            try:
                with connection.execute_wrapper(observe):
                    return apply_review_uplift(review.pk)
            finally:
                finished.set()
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                reservation = reserve_promo_for_checkout(code=reward.promo_code.code,
                    user=None, total_amount=Decimal("1000"))
                self.assertEqual(reservation.discount, Decimal("100"))
                future = pool.submit(worker)
                self.assertTrue(reached_lock.wait(15), "Uplift never reached the actual client FOR UPDATE")
                self.assertFalse(finished.is_set(), "Uplift must wait for the checkout lock")
            result = future.result(timeout=15)
        self.assertFalse(result["applied"], result)
        self.assertEqual(result["reason"], "promo_reserved")
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.discount_value, Decimal("10"))
        self.assertEqual(reward.promo_code.current_uses, 1)
        self.assertFalse(ReviewRewardUplift.objects.exists())

    def test_actual_privacy_erasure_purges_review_authority_and_retains_lifetime_tombstone(self):
        from management.bot_views import _delete_direct_bot_records

        reward = self._award()
        review = self._bound_review()
        self._approve(review)
        identity, client_id, promo_id = self.ig_client.igsid, self.ig_client.pk, reward.promo_code_id
        lifetime = IgUgcRewardLifetime.objects.get(reward_id=reward.pk)
        digest = lifetime.identity_digest
        result = _delete_direct_bot_records(exact_client_ids=[client_id])
        self.assertEqual(result["clients"], 1, result)
        self.assertFalse(IgClient.objects.filter(pk=client_id).exists())
        self.assertFalse(Review.objects.filter(pk=review.pk).exists())
        self.assertFalse(ReviewPurchaseInvitation.objects.exists())
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertFalse(IgUgcReward.objects.exists())
        marker = IgUgcRewardLifetime.objects.get(identity_digest=digest)
        self.assertIsNotNone(marker.consumed_at)
        self.assertIsNone(marker.client_id)
        self.assertIsNone(marker.reward_id)
        recreated = IgClient.get_or_create_for_sender(identity)
        self.assertTrue(ugc_identity_already_rewarded(recreated))
        promo = PromoCode.objects.get(pk=promo_id)
        self.assertFalse(promo.is_active)
        with self.assertRaises(PromoReservationError):
            reserve_promo_for_checkout(code=promo.code, user=None, total_amount=Decimal("1000"))

    def test_checkout_holds_review_lock_until_reservation_commits_then_rejection_blocks_new_invoice(self):
        self._native_only()
        reward = self._award()
        review = self._bound_review()
        self._approve(review)
        reached_review_lock, finished = Event(), Event()

        def reject():
            close_old_connections()
            def observe(execute, sql, params, many, context):
                if (sql.lstrip().lower().startswith("select")
                        and "reviews_review" in sql.lower() and "for update" in sql.lower()):
                    reached_review_lock.set()
                return execute(sql, params, many, context)
            try:
                with connection.execute_wrapper(observe):
                    current = Review.objects.get(pk=review.pk)
                    actor = get_user_model().objects.get(pk=self.actor.pk)
                    current.mark_rejected(by=actor, note="Revoke publication after reservation")
                return current.status
            finally:
                finished.set()
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                reservation = reserve_promo_for_checkout(code=reward.promo_code.code,
                    user=None, total_amount=Decimal("1000"))
                self.assertEqual(reservation.discount, Decimal("150"))
                future = pool.submit(reject)
                self.assertTrue(reached_review_lock.wait(15), "Moderation never reached the actual review SELECT FOR UPDATE")
                self.assertFalse(finished.is_set(), "Rejection must wait for checkout's purchase-review proof lock")
                attempt = PaymentAttempt.objects.create(promo_code=reservation.promo,
                    gross_amount=1000, discount_amount=150, payable_amount=850,
                    payment_amount=850, event_state=reservation.event_state)
            self.assertEqual(future.result(timeout=15), "rejected")
        attempt.refresh_from_db()
        self.assertEqual(attempt.discount_amount, Decimal("150"))
        self.assertEqual(attempt.event_state["promo_reservation"]["state"], "reserved")
        with self.assertRaises(PromoReservationError):
            reserve_promo_for_checkout(code=reward.promo_code.code, user=self.actor,
                total_amount=Decimal("1000"))


class _V2InvitationOwnerContract(_MarketingConsentFixture):
    """Reuse native owner fixtures, without inheriting their existing tests."""
    canonical = False

    def setUp(self):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        from orders.models import OrderItem
        from storefront.models import Category, Product

        ReviewOnlyOwnerContract.setUp(self)
        self.delivered_at = timezone.now() - timedelta(hours=1)
        self.order.tracking_provider_event_at = self.delivered_at
        self.order.tracking_terminal_at = self.delivered_at
        self.order.save(update_fields=["tracking_provider_event_at", "tracking_terminal_at"])
        category = Category.objects.create(name="V2 purchased clothing", slug="v2-purchased-clothing")
        self.product = Product.objects.create(title="V2 purchased garment", slug="v2-purchased-garment",
            category=category, status="published", price=790)
        self.item = OrderItem.objects.create(order=self.order, product=self.product, title=self.product.title,
            qty=1, size="L", unit_price=790, line_total=790)
        self.actor = get_user_model().objects.create_superuser(username="v2-ugc-reviewer",
            email="v2@example.com", password="test-password")
        from management.services.ig_order_assignments import link_order_to_client
        _enable_test_business_consent(self)
        assignment = link_order_to_client(self.order, client=self.customer, actor=self.actor)
        self.grant_business_consent(self.customer, self.order, assignment)

    def _delivered(self, order):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        return ReviewOnlyOwnerContract._delivered(self, order)

    def _event(self):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        return ReviewOnlyOwnerContract._event(self)

    def _text(self, event):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        return ReviewOnlyOwnerContract._text(self, event)

    def _request_boundary(self, kwargs, **kwargs2):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        return ReviewOnlyOwnerContract._request_boundary(self, kwargs, **kwargs2)

    def _start(self, kwargs):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        return ReviewOnlyOwnerContract._start(self, kwargs)

    def _transport_success(self, *args, **kwargs):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        return ReviewOnlyOwnerContract._transport_success(self, *args, **kwargs)

    def _dispatch(self, event, **kwargs):
        from management.tests_ig_post_purchase_review_only import ReviewOnlyOwnerContract
        return ReviewOnlyOwnerContract._dispatch(self, event, **kwargs)

    def _award_owner(self):
        from management.models import InstagramBotMessage
        from management.services.ig_order_assignments import link_order_to_client
        from management.services.ig_ugc_rewards import award_ugc_reward

        link_order_to_client(self.order, client=self.customer, actor=self.actor)
        evidence = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", text="Відмітила вас у сторіс", attachments="https://cdn.example/v2-story-proof.jpg",
            status="done", provider_created_at=self.delivered_at + timedelta(minutes=1))
        reward, created = award_ugc_reward(client=self.customer, order=self.order, actor=self.actor,
            evidence_message_id=evidence.pk, review_note="Verified native post-delivery mention")
        self.assertTrue(created)
        return reward

    def _assert_v2_sent(self, event, mode):
        metadata = event.payload["post_purchase_invitation"]
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["mode"], mode)
        self.assertEqual(metadata["reviews"][0]["order_item_id"], self.item.pk)
        self.assertEqual(metadata["reviews"][0]["product_id"], self.product.pk)
        link = metadata["reviews"][0]["url"]
        frozen = self._text(event)
        self.assertIn(link, frozen)
        state, send = self._dispatch(event, transport=self._transport_success)
        self.assertEqual(state, "sent", event.last_error)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sent_text, frozen)
        self.assertEqual(event.provider_message_id, "mid.review-only.success")
        self.assertIsNotNone(event.completed_at)
        state, replay = self._dispatch(event)
        # Legacy deliver_event reports an unclaimable terminal row as skipped;
        # the durable business result remains SENT in either owner.
        self.assertEqual(state, "sent" if self.canonical else "skipped", event.last_error)
        self.assertEqual(event.state, "sent", event.last_error)
        self.assertEqual(event.provider_message_id, "mid.review-only.success")
        replay.assert_not_called()
        self.assertEqual(self._text(event), frozen)

    def test_potential_base_v2_owner_sends_frozen_purchased_product_link_once(self):
        event = self._event()
        self._assert_v2_sent(event, "review_and_reward")
        self.assertIn("10%", self._text(event))
        self.assertIn("15%", self._text(event))
        self.assertFalse(PromoCode.objects.exists())
        self.assertFalse(IgUgcRewardLifetime.objects.exists())

    def test_unused_base_ten_v2_owner_sends_uplift_invitation_with_real_receipt(self):
        reward = self._award_owner()
        event = self._event()
        self._assert_v2_sent(event, "review_and_uplift")
        self.assertIn("15%", self._text(event))
        self.assertEqual(event.payload["post_purchase_invitation"]["reward_id"], reward.pk)
        self.assertEqual(PromoCode.objects.count(), 1)
        reward.promo_code.refresh_from_db()
        self.assertEqual(reward.promo_code.discount_value, Decimal("10"))
        self.assertFalse(ReviewRewardUplift.objects.exists())

    def test_late_reservation_at_actual_provider_boundary_blocks_v2_without_receipt(self):
        reward = self._award_owner()
        event = self._event()
        self.assertEqual(event.payload["post_purchase_invitation"]["version"], 2)
        original = self._text(event)

        def transport(_settings, recipient, text, **kwargs):
            self.assertEqual(recipient, self.customer.igsid)
            self.assertEqual(text, original)
            self._start(kwargs)
            reservation = reserve_promo_for_checkout(code=reward.promo_code.code,
                user=None, total_amount=Decimal("1000"))
            self.assertEqual(reservation.discount, Decimal("100"))
            with self._request_boundary(kwargs) as permitted:
                self.assertFalse(permitted, "Reservation changed incentive eligibility before Meta I/O")
            return False, "permission_boundary", "post_purchase_eligibility_unknown"

        state, send = self._dispatch(event, transport=transport)
        self.assertEqual(send.call_count, 1)
        self.assertNotEqual(state, "sent")
        self.assertFalse(event.provider_message_id)
        self.assertEqual(self._text(event), original)
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertEqual(PromoCodeGuestUsage.objects.get(promo_code_id=reward.promo_code_id).state, "reserved")


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class LegacyV2PurchaseInvitationTests(_V2InvitationOwnerContract, TestCase):
    pass


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class CanonicalV2PurchaseInvitationTests(_V2InvitationOwnerContract, TestCase):
    canonical = True


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class AfterAwardReviewInvitationTests(_ReviewUpliftFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.ig_client.language = "uk"
        self.ig_client.save(update_fields=["language", "updated_at"])
        _enable_test_business_consent(self)

    def _reward_after_consent(self):
        self.consent_invitation, self.consent_answer = self.grant_business_consent(
            self.ig_client, self.order, self.assignment,
            text="Дякую за отримане замовлення, хочу отримувати ваші новинки та пропозиції.")
        self.assertEqual(self.consent_invitation.locale, "uk")
        # Current, admitted, owned English input outranks the Ukrainian profile
        # and the older invitation's locale when new reward copy is produced.
        self.evidence = self.consent_source(self.ig_client,
            text="I have tagged TwoComms in my story while wearing the shirt. Please reply in English.",
            attachments="https://cdn.example/after-award-owned-story-proof.jpg")
        self.assertGreater(self.evidence.pk, self.consent_answer.pk)
        reward = self._award()
        delivery = reward.deliveries.get(generation=1)
        snapshot = delivery.invitation_snapshot
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["locale"], "en")
        self.assertTrue(snapshot["language_proof"])
        self.assertEqual(snapshot["consent"]["invitation_id"], str(self.consent_invitation.pk))
        self.assertEqual(snapshot["consent"]["source_message_id"], self.consent_answer.pk)
        metadata = snapshot["post_purchase_invitation"]
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["mode"], "review_and_uplift")
        self.assertEqual(metadata["business_consent"], snapshot["consent"])
        self.assertIn("Your personal 10%", delivery.message_snapshot)
        self.assertIn("15%", delivery.message_snapshot)
        self.assertIn(metadata["reviews"][0]["url"], delivery.message_snapshot)
        return reward, delivery

    def _revoke(self):
        from management.services.ig_marketing_consent import handle_consent_reply, has_post_purchase_consent

        source = self.consent_source(self.ig_client, text="Opt out", payload=self.consent_invitation.revoke_payload)
        outcome = handle_consent_reply(source)
        self.assertEqual(outcome.reason, "business_consent_revoke")
        self.assertFalse(has_post_purchase_consent(self.ig_client, self.order.pk))

    def _assert_rating_uplifts_original_code(self, rating):
        reward, delivered = self._reward_after_consent()
        original = (reward.promo_code.code, reward.promo_code.valid_until,
                    delivered.message_snapshot, delivered.invitation_snapshot)
        self.assertEqual(self.ig_client.language, "uk")
        self._send(delivered, f"after-award-ten-rating-{rating}")
        ref = delivered.invitation_snapshot["post_purchase_invitation"]["reviews"][0]
        response = self.web.get(ref["url"], secure=True)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(response["Location"], reverse("product", args=[self.product.slug]) + "#product-reviews")
        response = self._post_review(rating=str(rating))
        self.assertEqual(response.status_code, 200, response.content)
        review = Review.objects.get(pk=response.json()["review_id"])
        self.assertEqual(str(review.purchase_invitation_id), ref["invitation_id"])
        self.assertEqual(review.rating, rating)
        self._approve(review)
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        self.assertEqual(effective_reward_percent(reward), 15)
        self.assertEqual((reward.promo_code.code, reward.promo_code.valid_until), original[:2])
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        delivered.refresh_from_db()
        self.assertEqual(delivered.state, "sent")
        self.assertEqual((delivered.message_snapshot, delivered.invitation_snapshot), original[2:])
        new = reward.deliveries.get(generation=2)
        self.assertEqual(new.discount_percent_snapshot, 15)
        self.assertEqual(new.invitation_snapshot, {})
        self._send(new, f"after-award-fifteen-rating-{rating}")
        with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Historical10 replay")) as send:
            self.assertEqual(process_external_ugc_reward_delivery(delivered.pk), "sent")
        send.assert_not_called()
        reservation = reserve_promo_for_checkout(code=reward.promo_code.code, user=None, total_amount=Decimal("1000"))
        self.assertEqual(reservation.discount, Decimal("150"))

    def test_one_star_review_from_sent_reward_link_uplifts_same_code(self):
        self._assert_rating_uplifts_original_code(1)

    def test_two_star_review_from_sent_reward_link_uplifts_same_code(self):
        self._assert_rating_uplifts_original_code(2)

    def test_three_star_review_from_sent_reward_link_uplifts_same_code(self):
        self._assert_rating_uplifts_original_code(3)

    def test_four_star_review_from_sent_reward_link_uplifts_same_code(self):
        self._assert_rating_uplifts_original_code(4)

    def test_five_star_review_from_sent_reward_link_uplifts_same_code(self):
        self._assert_rating_uplifts_original_code(5)

    def test_no_business_consent_sends_owed_ten_only_without_marketing_links(self):
        reward = self._award()
        delivery = reward.deliveries.get(generation=1)
        self.assertEqual(delivery.invitation_snapshot, {})
        self.assertIn("10%", delivery.message_snapshot)
        self.assertNotIn("15%", delivery.message_snapshot)
        self.assertNotIn("/reviews/invitation/", delivery.message_snapshot)
        self.assertFalse(ReviewPurchaseInvitation.objects.exists())
        self._send(delivery, "owed-ten-without-marketing-consent")

    def test_revoked_consent_before_claim_retains_frozen_offer_without_sending(self):
        reward, delivery = self._reward_after_consent()
        original = (delivery.message_snapshot, delivery.invitation_snapshot)
        self._revoke()
        with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Revoked marketing consent")) as send:
            state = process_external_ugc_reward_delivery(delivery.pk)
        send.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(state, "failed", delivery.last_error)
        self.assertEqual(delivery.provider_message_ids, [])
        self.assertIsNotNone(delivery.completed_at)
        self.assertEqual((delivery.message_snapshot, delivery.invitation_snapshot), original)
        self.assertEqual(PromoCode.objects.count(), 1)

    def test_consent_revocation_at_first_provider_boundary_sends_no_reward(self):
        self._assert_consent_revocation_at_provider_boundary(partial=False)

    def test_partial_reward_receipt_then_consent_revocation_stays_ambiguous_without_replay(self):
        self._assert_consent_revocation_at_provider_boundary(partial=True)

    def _assert_consent_revocation_at_provider_boundary(self, *, partial):
        from management.services.instagram_bot import ProviderDeliveryReceipt

        reward, delivery = self._reward_after_consent()
        original = (delivery.message_snapshot, delivery.invitation_snapshot)
        mids = ("after-award-consent-partial-mid",) if partial else ()

        def transport(settings, recipient, text, **kwargs):
            self.assertEqual(text, original[0])
            factory = kwargs["provider_request_boundary_factory"]
            if partial:
                with factory(delivered_chunk_count=0, provider_message_ids=(), planned_chunk_count=2) as permitted:
                    self.assertTrue(permitted)
                    kwargs["provider_message_callback"](mids[0])
            self._revoke()
            with factory(delivered_chunk_count=int(partial), provider_message_ids=mids,
                         planned_chunk_count=2 if partial else 1) as permitted:
                self.assertFalse(permitted, "Revoked consent blocks the physical provider request")
            return ProviderDeliveryReceipt(ok=False, kind="cancelled", provider_message_ids=mids,
                planned_chunk_count=2 if partial else 1, delivered_chunk_count=int(partial))

        with (patch("management.services.instagram_bot.send_text", side_effect=transport) as send,
              patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("Unexpected Meta I/O")) as http):
            state = process_external_ugc_reward_delivery(delivery.pk)
        send.assert_called_once()
        http.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(state, "ambiguous" if partial else "failed", delivery.last_error)
        self.assertEqual(delivery.provider_message_ids, list(mids))
        self.assertIsNotNone(delivery.completed_at)
        self.assertEqual((delivery.message_snapshot, delivery.invitation_snapshot), original)
        with patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Terminal consent failure replay")) as send:
            process_external_ugc_reward_delivery(delivery.pk)
        send.assert_not_called()
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertFalse(ReviewRewardUplift.objects.exists())


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class ReviewUpliftDurabilityTests(_ReviewUpliftFixture, TransactionTestCase):
    """Approval and recovery are committed SQL transactions, without an outer test transaction."""
    def _source(self, review):
        return f"review_uplift:{review.pk:x}"

    def _durable_job(self, review):
        from management.ig_bot_models import IgUgcRewardLifecycleJob

        job = IgUgcRewardLifecycleJob.objects.get(source=self._source(review))
        self.assertEqual(job.client_id, self.ig_client.pk)
        self.assertEqual(job.order_id, self.order.pk)
        return job

    def _approve_with_lost_fast_callback(self, review):
        with patch("management.services.ig_review_reward.process_review_uplift_job",
                   return_value={"state": "deferred"}) as fast_worker:
            self._approve(review)
        job = self._durable_job(review)
        fast_worker.assert_called_once_with(job.pk, using="default")
        review.refresh_from_db()
        self.assertEqual(review.status, "approved")
        self.assertEqual(review.moderated_by_id, self.actor.pk)
        self.assertIsNotNone(review.moderated_at)
        self.assertFalse(connection.in_atomic_block)
        return job

    def _recover(self, job):
        from management.services.ig_ugc_rewards import process_linked_ugc_reward_lifecycle_job

        with (patch("management.services.instagram_bot.send_text", side_effect=AssertionError("Reconciliation must not send Meta")) as send,
              patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("Reconciliation provider I/O")) as http):
            result = process_linked_ugc_reward_lifecycle_job(job.pk)
        send.assert_not_called()
        http.assert_not_called()
        return result

    def test_lost_approval_callback_recovers_from_existing_durable_lifecycle_owner_once(self):
        from management.ig_bot_models import IgUgcRewardLifecycleJob

        reward = self._award()
        original = (reward.promo_code_id, reward.promo_code.code, reward.promo_code.valid_until)
        base = reward.deliveries.get(generation=1)
        self._send(base, "review-durable-base-mid")
        base_text = base.message_snapshot
        review = self._bound_review()
        job = self._approve_with_lost_fast_callback(review)
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertEqual(reward.promo_code.discount_value, Decimal("10"))
        result = self._recover(job)
        self.assertEqual(result["state"], "done", result)
        self.assertFalse(IgUgcRewardLifecycleJob.objects.filter(pk=job.pk).exists())
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        self.assertEqual(effective_reward_percent(reward), 15)
        self.assertEqual((reward.promo_code_id, reward.promo_code.code, reward.promo_code.valid_until), original)
        self.assertEqual(reward.deliveries.filter(generation=2).count(), 1)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        base.refresh_from_db()
        self.assertEqual(base.state, "sent")
        self.assertEqual(base.message_snapshot, base_text)
        self.assertEqual(base.provider_message_ids, ["review-durable-base-mid"])
        self.assertEqual(self._recover(job)["state"], "missing")
        self.assertEqual(reward.deliveries.filter(generation=2).count(), 1)

    def test_enqueue_failure_rolls_back_native_staff_approval_and_inserted_job_together(self):
        from management.ig_bot_models import IgUgcRewardLifecycleJob
        from management.services.ig_review_reward import queue_review_uplift_job

        self._award()
        review = self._bound_review()

        def insert_then_fail(review_id, *, using=None):
            job = queue_review_uplift_job(review_id, using=using)
            self.assertTrue(IgUgcRewardLifecycleJob.objects.filter(pk=job.pk).exists())
            raise RuntimeError("Injected database enqueue failure after native INSERT")

        with patch("management.services.ig_review_reward.queue_review_uplift_job", side_effect=insert_then_fail):
            with self.assertRaisesRegex(RuntimeError, "enqueue failure"):
                review.mark_approved(by=self.actor, note="Genuine staff moderation")
        review.refresh_from_db()
        self.assertEqual(review.status, "pending")
        self.assertIsNone(review.moderated_by_id)
        self.assertIsNone(review.moderated_at)
        self.assertFalse(IgUgcRewardLifecycleJob.objects.filter(source=self._source(review)).exists())
        self.assertFalse(ReviewRewardUplift.objects.exists())

    def test_operational_recovery_failure_keeps_job_with_backoff_then_retries_successfully(self):
        from management.ig_bot_models import IgUgcRewardLifecycleJob

        reward = self._award()
        review = self._bound_review()
        job = self._approve_with_lost_fast_callback(review)
        original_due = job.due_at
        with patch("management.services.ig_review_reward.apply_review_uplift",
                   side_effect=RuntimeError("Temporary worker failure")):
            result = self._recover(job)
        self.assertEqual(result["state"], "failed", result)
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1)
        self.assertGreater(job.due_at, original_due)
        self.assertTrue(job.last_error_kind)
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertEqual(self._recover(job)["state"], "done")
        self.assertFalse(IgUgcRewardLifecycleJob.objects.filter(pk=job.pk).exists())
        reward.refresh_from_db()
        self.assertEqual(effective_reward_percent(reward), 15)

    def test_lost_rejection_callback_durable_job_holds_existing_fifteen_without_new_code(self):
        reward = self._award()
        review = self._bound_review()
        self._approve(review)
        component = ReviewRewardUplift.objects.get(reward=reward)
        original = (component.pk, component.proof_signature, reward.promo_code.code, reward.promo_code.valid_until)
        with patch("management.services.ig_review_reward.process_review_uplift_job", return_value={"state": "deferred"}):
            review.mark_rejected(by=self.actor, note="Withdraw approval")
        job = self._durable_job(review)
        result = self._recover(job)
        self.assertEqual(result["state"], "done", result)
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        component.refresh_from_db()
        self.assertEqual(reward.lifecycle_state, "held")
        self.assertFalse(reward.promo_code.is_active)
        self.assertEqual((component.pk, component.proof_signature, reward.promo_code.code, reward.promo_code.valid_until), original)
        self.assertEqual(ReviewRewardUplift.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        for user in (None, self.actor):
            with self.subTest(authenticated=user is not None), self.assertRaises(PromoReservationError):
                reserve_promo_for_checkout(code=reward.promo_code.code, user=user, total_amount=Decimal("1000"))

    def test_pending_review_creates_no_job_or_independent_five_percent_coupon(self):
        from management.ig_bot_models import IgUgcRewardLifecycleJob

        review = self._bound_review()
        self.assertEqual(review.status, "pending")
        self.assertFalse(IgUgcRewardLifecycleJob.objects.filter(source=self._source(review)).exists())
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_approved_review_job_without_ugc_finishes_without_independent_five(self):
        review = self._bound_review()
        job = self._approve_with_lost_fast_callback(review)
        self.assertEqual(self._recover(job)["state"], "done")
        self.assertFalse(ReviewRewardUplift.objects.exists())
        self.assertFalse(PromoCode.objects.exists())
        self.assertFalse(IgUgcRewardLifetime.objects.exists())
        reward = self._award()
        reward.refresh_from_db()
        self.assertEqual(effective_reward_percent(reward), 15)
        self.assertEqual(PromoCode.objects.count(), 1)

    def test_reserved_ten_keeps_durable_retry_and_never_issues_independent_five(self):
        reward = self._award()
        original_expiry = reward.promo_code.valid_until
        review = self._bound_review()
        job = self._approve_with_lost_fast_callback(review)
        reservation = reserve_promo_for_checkout(code=reward.promo_code.code,
            user=None, total_amount=Decimal("1000"))
        self.assertEqual(reservation.discount, Decimal("100"))
        result = self._recover(job)
        self.assertEqual(result["state"], "failed", result)
        job.refresh_from_db()
        self.assertGreaterEqual(job.attempts, 1)
        self._unchanged_ten(reward, original_expiry)
        self.assertEqual(reward.deliveries.filter(generation=2).count(), 0)

    def test_consumed_ten_job_finishes_without_coupon_renewal_or_standalone_five(self):
        reward = self._award()
        original_expiry = reward.promo_code.valid_until
        review = self._bound_review()
        job = self._approve_with_lost_fast_callback(review)
        reservation = reserve_promo_for_checkout(code=reward.promo_code.code,
            user=None, total_amount=Decimal("1000"))
        purchased = Order.objects.create(order_number="DURABLE-NEXT-01", full_name="Guest",
            phone="0991234567", total_sum=900, payment_status="paid")
        attempt = PaymentAttempt.objects.create(order=purchased, promo_code=reservation.promo,
            gross_amount=1000, discount_amount=100, payable_amount=900, payment_amount=900,
            event_state=reservation.event_state)
        self.assertTrue(consume_payment_attempt_promo(attempt, order=purchased))
        attempt.save(update_fields=["event_state"])
        result = self._recover(job)
        self.assertEqual(result["state"], "done", result)
        self._unchanged_ten(reward, original_expiry)
        self.assertEqual(PromoCodeGuestUsage.objects.get(promo_code_id=reward.promo_code_id).state, "consumed")
