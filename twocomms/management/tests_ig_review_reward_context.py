"""Read-only canonical reward facts, privacy, and captured prompt/admin parity."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
import re
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from management.services.ig_client_state_card import (
    assemble_client_state, client_state_admin_payload, render_client_state_prompt,
)
from management.services.ig_review_reward import apply_review_uplift
from management.services.ig_review_reward_context import SLOT, VERSION, capture_review_reward_context
from management.tests_ig_review_uplift import _ReviewUpliftFixture
from orders.models import Order, PaymentAttempt
from orders.promo_reservations import consume_payment_attempt_promo, reserve_promo_for_checkout
from reviews.models import ReviewStatus


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    STORAGES={"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
              "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class CapturedReviewRewardContextTests(_ReviewUpliftFixture, TestCase):
    def boundary(self):
        return {"client_id": self.ig_client.pk, "episode_id": self.ig_client.current_commercial_episode_id,
            "order_id": self.order.pk, "line_id": "", "recipient_id": "self", "reset_floor": 1,
            "source_namespace": "instagram_login:reward-context", "reset_id": None, "erasure_epoch": "",
            "source_watermark": {"message_id": self.evidence.pk,
                "event_at": self.evidence.provider_created_at.isoformat()}}

    def captured(self, *, at=None, boundary=None):
        queries = []
        def read_only(execute, sql, params, many, context):
            queries.append(sql)
            if re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|ALTER|CREATE|DROP)\b|FOR\s+UPDATE", sql, re.I):
                raise AssertionError("Reward context attempted a write or lock")
            return execute(sql, params, many, context)
        with ExitStack() as stack:
            stack.enter_context(connection.execute_wrapper(read_only))
            mocks = [stack.enter_context(patch(path)) for path in (
                "management.services.call_ai_analysis.gemini_generate_text",
                "management.services.instagram_bot._provider_http", "management.services.instagram_bot.send_text")]
            slot = capture_review_reward_context(self.ig_client.pk, boundary=boundary or self.boundary(), captured_at=at or timezone.now())
        for mock in mocks:
            mock.assert_not_called()
        self.assertTrue(queries)
        self.assertFalse(any(re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|ALTER|CREATE|DROP)\b|FOR\s+UPDATE", sql, re.I)
            for sql in queries))
        return slot

    def uplift(self):
        reward = self._award()
        review = self._approved_without_worker()
        result = apply_review_uplift(review.pk)
        self.assertTrue(result["applied"], result)
        reward.refresh_from_db()
        reward.promo_code.refresh_from_db()
        return reward, review

    def test_absent_reward_exposes_only_finite_policy_without_new_entitlement(self):
        slot = self.captured()
        value = slot["value"]
        self.assertIs(value["reward_present"], False)
        self.assertIsNone(value["available_percent"])
        self.assertEqual(value["policy"]["accepted_star_ratings"], [1, 2, 3, 4, 5])
        self.assertFalse(value["policy"]["new_coupon"])
        self.assertFalse(value["policy"]["standalone_review_5_percent_coupon"])
        self.assertFalse(value["policy"]["extends_expiry"])

    def test_native_unused_ten_is_read_only_and_contains_no_private_payload(self):
        reward = self._award()
        slot = self.captured()
        value = slot["value"]
        self.assertEqual((value["base_percent"], value["effective_percent"], value["available_percent"]), (10, 10, 10))
        self.assertEqual(value["state"], "active")
        self.assertEqual(value["original_valid_until"], reward.promo_code.valid_until.isoformat())
        serialized = json.dumps(slot)
        for private in (reward.promo_code.code, reward.review_note, self.ig_client.igsid, self.order.order_number):
            self.assertNotIn(private, serialized)

    def test_real_one_star_uplift_has_same_original_expiry_and_validated_fifteen(self):
        reward, review = self.uplift()
        value = self.captured()["value"]
        self.assertEqual(review.rating, 1)
        self.assertEqual((value["base_percent"], value["effective_percent"], value["available_percent"]), (10, 15, 15))
        self.assertEqual(value["component_status"], "validated")
        self.assertEqual(value["original_valid_until"], (reward.issued_at + timedelta(days=90)).isoformat())
        self.assertNotIn(review.body, json.dumps(value))

    def test_reserved_reward_never_reports_an_available_discount(self):
        reward = self._award()
        reserve_promo_for_checkout(code=reward.promo_code.code, user=None, total_amount=Decimal("1000"))
        value = self.captured()["value"]
        self.assertEqual(value["state"], "reserved")
        self.assertIsNone(value["available_percent"])

    def test_used_reward_never_reports_an_available_discount(self):
        reward = self._award()
        reservation = reserve_promo_for_checkout(code=reward.promo_code.code, user=None, total_amount=Decimal("1000"))
        purchased = Order.objects.create(order_number="CONTEXT-NEXT-01", full_name="Guest", phone="0991234567",
            total_sum=900, payment_status="paid")
        attempt = PaymentAttempt.objects.create(order=purchased, promo_code=reservation.promo, gross_amount=1000,
            discount_amount=reservation.discount, payable_amount=900, payment_amount=900, event_state=reservation.event_state)
        self.assertTrue(consume_payment_attempt_promo(attempt, order=purchased))
        attempt.save(update_fields=["event_state"])
        value = self.captured()["value"]
        self.assertEqual(value["state"], "used")
        self.assertIsNone(value["available_percent"])

    def test_expiry_is_exclusive_and_does_not_extend_original_ninety_days(self):
        reward = self._award()
        value = self.captured(at=reward.promo_code.valid_until)["value"]
        self.assertEqual(value["state"], "expired")
        self.assertIsNone(value["available_percent"])
        self.assertEqual(value["original_valid_until"], reward.promo_code.valid_until.isoformat())

    def test_native_staff_rejection_fails_unknown_without_available_fifteen(self):
        reward, review = self.uplift()
        original_proof = (review.purchase_content_digest, review.purchase_proof_signature)
        review.mark_rejected(by=self.actor, note="Native moderation withdrawal")
        review.refresh_from_db()
        self.assertEqual(review.status, ReviewStatus.REJECTED)
        self.assertEqual(review.moderated_by_id, self.actor.pk)
        self.assertIsNotNone(review.moderated_at)
        self.assertEqual((review.purchase_content_digest, review.purchase_proof_signature), original_proof)
        value = self.captured()["value"]
        self.assertEqual(value["state"], "unknown")
        self.assertIsNone(value["reward_present"])
        self.assertIsNone(value["effective_percent"])
        self.assertIsNone(value["available_percent"])

    def test_lifetime_discount_survives_new_order_but_its_review_cannot_uplift_old_source_order(self):
        reward = self._award()
        new_order = Order.objects.create(order_number="CONTEXT-DIFFERENT-01", full_name="Guest",
            phone="0991234567", total_sum=1000, payment_status="unpaid")
        boundary = self.boundary()
        boundary["order_id"] = new_order.pk
        value = self.captured(boundary=boundary)["value"]
        self.assertEqual(value["benefit_scope"], "client_lifetime")
        self.assertEqual(value["source_order_id"], reward.order_id)
        self.assertIs(value["review_uplift_source_order_matches"], False)
        self.assertEqual(value["available_percent"], 10)
        self.assertTrue(value["policy"]["same_source_order_required_for_delivered_order_uplift"])
        boundary["order_id"] = None
        self.assertIsNone(self.captured(boundary=boundary)["value"]["review_uplift_source_order_matches"])

    def test_frozen_state_and_admin_share_same_capture_after_reward_changes(self):
        reward = self._award()
        at = timezone.now()
        slot = self.captured(at=at)
        state = assemble_client_state(boundary=self.boundary(), components={"slots": {SLOT: slot}}, captured_at=at)
        before = deepcopy(state.as_dict())
        reward.promo_code.is_active = False
        reward.promo_code.save(update_fields=["is_active"])
        with patch("management.services.ig_review_reward_context.capture_review_reward_context",
                side_effect=AssertionError("Consumer reread")) as reread:
            rendered = render_client_state_prompt(state, budget=4000)
            admin = client_state_admin_payload(state)
        reread.assert_not_called()
        self.assertIn(SLOT, rendered.included)
        self.assertEqual(admin["slots"][SLOT]["value"], slot["value"])
        self.assertEqual(state.as_dict(), before)
        self.assertIn('"available_percent":10', rendered.text)
        self.assertEqual(self.captured()["value"]["state"], "held")


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=4000)
class ReviewRewardActualCaptureIntegrationTests(TestCase):
    def test_actual_prepared_state_and_manifest_use_one_benefit_capture(self):
        from management.tests_ig_turn_context_integration import TurnContextConsumerIntegrationTests
        from management.services import ig_review_reward_context as service
        fixture_owner = TurnContextConsumerIntegrationTests(methodName="runTest")
        fixture_owner.setUp()
        self.addCleanup(fixture_owner.doCleanups)
        with patch.object(service, "capture_review_reward_context", wraps=service.capture_review_reward_context) as reader:
            fixture = fixture_owner.fixture()
        self.assertEqual(reader.call_count, 1)
        value = fixture.prepared.state.as_dict()["slots"][SLOT]["value"]
        self.assertEqual(value["policy"]["version"], VERSION)
        self.assertEqual(fixture.prepared.request_metadata["view_versions"]["ugc_review_benefit"], VERSION)
        fixture_owner.requests = []
        with patch.object(service, "capture_review_reward_context", side_effect=AssertionError("Consumer reread")) as reread, patch(
                "management.services.call_ai_analysis.gemini_generate_text", side_effect=fixture_owner.transport) as provider:
            _result, failure = fixture_owner.consume(fixture)
        reread.assert_not_called()
        self.assertEqual(provider.call_count, 1, failure)
        payload, manifest, _kwargs = fixture_owner.requests[0]
        self.assertIn("same_existing_unused_10_percent_coupon_only", str(payload))
        self.assertEqual(manifest["request_context"]["view_versions"]["ugc_review_benefit"], VERSION)
