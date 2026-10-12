"""Two actual signed purposes coordinate around native customer delivery."""
from copy import deepcopy
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.models import InstagramBotMessage, IgCustomerTurnRevision, IgFollowUpTask
from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest as Request
from management.ig_restock_subscription_models import IgRestockAvailabilityEvent as Event
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_fulfillment_delivery as cart_delivery
from management.services import ig_restock_subscriptions as restock
from management.services import ig_fulfillment_notification_coordination as coordination
from management.services.ig_restock_availability import observe_target
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True)
class NativeAvailabilityCoordinationTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="native-coordination-manager",
            email="coordination@example.test", password="test")

    def complete_input(self):
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(state="processed", active_slot=None,
            claim_token="", lease_until=None)

    def decide(self, request, *, availability="available", preparation=None, at=None):
        now = at or self.now + timedelta(seconds=2)
        rows = [{"line_id": line["line_id"], "availability": availability, "checked_quantity": 1,
            "supply_basis": "accessible_unrecorded", "preparation": preparation} for line in request.envelope["lines"]]
        result = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), decisions_by_line=rows, now=now)
        self.assertTrue(result["ready"], result)
        return result

    def pair(self, *, original_decision=True, availability="available", preparation=None):
        inv, subscription = self.accepted()
        self.complete_input()
        original = Request.objects.get(pk=inv.envelope["manager_review"]["request_id"])
        cart_task = None
        if original_decision:
            decided = self.decide(original, availability=availability, preparation=preparation)
            cart_task = IgFollowUpTask.objects.get(event_key=f"ig-fulfillment-update:{original.pk}:{decided['version']}")
        observed = observe_target(inv.target, now=self.now + timedelta(seconds=2)).as_dict()
        review = reviews.ensure_restock_manager_review(subscription, observed, now=self.now + timedelta(seconds=2))
        self.assertTrue(review.get("request_id"), review)
        standing = Request.objects.get(pk=review["request_id"])
        self.decide(standing)
        result = restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        self.assertEqual(result["created"], 1, result)
        event = Event.objects.get(subscription=subscription)
        return inv, subscription, original, cart_task, event

    def projected(self, task, *, at=None):
        result = reviews.validated_update_task(task, self.settings, now=at or self.now + timedelta(seconds=3))
        self.assertTrue(result["ready"], result)
        return result

    def send(self, purpose, *, outcome="sent", at=None, before_http=None):
        from management.services import instagram_bot as bot
        now = at or self.now + timedelta(seconds=3)
        self.complete_input()
        physical = []
        def token(_settings, **kwargs):
            if before_http:
                before_http()
            return "synthetic-native-coordination-token"
        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if outcome == "unknown":
                raise TimeoutError("actual provider request has uncertain delivery")
            return 200, json.dumps({"message_id": f"native-coordination-{purpose}"})
        with patch("django.utils.timezone.now", return_value=now), patch.object(bot, "get_page_token", side_effect=token), \
                patch.object(bot, "_provider_http", side_effect=provider):
            if purpose == "cart":
                count = cart_delivery.process_due_fulfillment_updates(self.settings, now=now, limit=20)
            else:
                count = restock.process_due_restock_notifications(self.settings, now=now, limit=20)
        return count, physical

    def test_pending_original_available_cart_has_priority_and_helpers_are_read_only(self):
        _inv, _subscription, _request, task, event = self.pair()
        projected = self.projected(task)
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            cart = coordination.cart_update_coordination(task, projected, self.settings, self.now + timedelta(seconds=3))
            standing = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=3))
        provider.assert_not_called()
        self.assertEqual(cart["reason"], "")
        self.assertEqual(standing["reason"], coordination.HOLD)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in queries))
        self.assertFalse(InstagramBotMessage.objects.filter(role="model", source="followup").exists())

    def test_actual_cart_native_mid_after_acceptance_fulfills_one_standing_interest(self):
        _inv, subscription, _request, cart_task, event = self.pair()
        count, physical = self.send("cart")
        self.assertEqual((count, len(physical)), (1, 1))
        cart_task.refresh_from_db()
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], coordination.FULFILLED, result)
        receipt = result["delegated_receipt"]
        self.assertEqual(receipt["task_id"], cart_task.pk)
        self.assertEqual(receipt["provider_message_id"], "native-coordination-cart")
        self.assertEqual(receipt["subscription_id"], str(subscription.pk))
        event.task.refresh_from_db()
        self.assertEqual(event.task.provider_message_id, "")
        count, physical = self.send("restock", at=self.now + timedelta(seconds=4))
        self.assertEqual((count, physical), (0, []))
        event.task.refresh_from_db()
        self.assertEqual(event.task.provider_message_id, "")

    def test_cart_native_checkpoint_survives_missing_crm_history_for_delegation(self):
        _inv, _subscription, _request, cart_task, event = self.pair()
        count, physical = self.send("cart")
        self.assertEqual((count, len(physical)), (1, 1))
        cart_task.refresh_from_db()
        # Remove only the optional local projection of an actual native send.
        # The native task checkpoint remains real; no consent or receipt is fabricated.
        self.assertIsNotNone(cart_task.sent_message_id)
        InstagramBotMessage.objects.get(pk=cart_task.sent_message_id).delete()
        cart_task.refresh_from_db()
        self.assertIsNone(cart_task.sent_message_id)
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], coordination.FULFILLED)
        self.assertEqual(result["delegated_receipt"]["provider_message_id"], cart_task.provider_message_id)

    def test_processing_original_cart_blocks_restock_at_actual_token_lookup(self):
        _inv, _subscription, _request, _task, event = self.pair()
        observed = []
        def inspect():
            observed.append(coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=3)))
        count, physical = self.send("cart", before_http=inspect)
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertEqual(observed[0]["reason"], coordination.HOLD)

    def test_unknown_original_cart_blocks_restock_without_second_http(self):
        _inv, _subscription, _request, task, event = self.pair()
        count, physical = self.send("cart", outcome="unknown")
        self.assertEqual((count, len(physical)), (0, 1))
        task.refresh_from_db()
        self.assertEqual(task.status, "ambiguous")
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], coordination.HOLD)
        count, physical = self.send("restock", at=self.now + timedelta(seconds=4))
        self.assertEqual((count, physical), (0, []))

    def test_actual_restock_sent_suppresses_later_plain_cart_availability_only(self):
        _inv, _subscription, request, _task, event = self.pair(original_decision=False)
        count, physical = self.send("restock")
        self.assertEqual((count, len(physical)), (1, 1))
        decided = self.decide(request, at=self.now + timedelta(seconds=4))
        task = IgFollowUpTask.objects.get(event_key=f"ig-fulfillment-update:{request.pk}:{decided['version']}")
        projected = self.projected(task, at=self.now + timedelta(seconds=4))
        result = coordination.cart_update_coordination(task, projected, self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], coordination.OWNED)
        count, physical = self.send("cart", at=self.now + timedelta(seconds=4))
        self.assertEqual((count, physical), (0, []))

    def test_restock_sent_does_not_suppress_new_exact_preparation_information(self):
        _inv, _subscription, request, _task, _event = self.pair(original_decision=False)
        count, physical = self.send("restock")
        self.assertEqual((count, len(physical)), (1, 1))
        decided = self.decide(request, preparation={"min_days": 7, "max_days": 10, "unit": "calendar"},
            at=self.now + timedelta(seconds=4))
        task = IgFollowUpTask.objects.get(event_key=f"ig-fulfillment-update:{request.pk}:{decided['version']}")
        result = coordination.cart_update_coordination(task, self.projected(task, at=self.now + timedelta(seconds=4)),
            self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], "")
        count, physical = self.send("cart", at=self.now + timedelta(seconds=4))
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertIn("7–10", physical[0]["message"]["text"])

    def test_unknown_restock_holds_later_cart_and_never_replays(self):
        _inv, _subscription, request, _task, event = self.pair(original_decision=False)
        count, physical = self.send("restock", outcome="unknown")
        self.assertEqual((count, len(physical)), (0, 1))
        event.task.refresh_from_db()
        self.assertEqual(event.task.status, "ambiguous")
        decided = self.decide(request, at=self.now + timedelta(seconds=4))
        task = IgFollowUpTask.objects.get(event_key=f"ig-fulfillment-update:{request.pk}:{decided['version']}")
        result = coordination.cart_update_coordination(task, self.projected(task, at=self.now + timedelta(seconds=4)),
            self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], coordination.HOLD)
        self.assertEqual(self.send("cart", at=self.now + timedelta(seconds=4)), (0, []))
        self.assertEqual(self.send("restock", at=self.now + timedelta(seconds=4)), (0, []))

    def test_wait_original_decision_cannot_consume_or_block_fresh_standing_availability(self):
        _inv, _subscription, _request, task, event = self.pair(availability="wait")
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=3))
        self.assertEqual(result, {"reason": ""})
        result = coordination.cart_update_coordination(task, self.projected(task), self.settings, self.now + timedelta(seconds=3))
        self.assertEqual(result, {"reason": ""})

    def test_corrupted_signed_event_never_bypasses_coordination(self):
        _inv, _subscription, _request, _task, event = self.pair()
        event.envelope = deepcopy(event.envelope)
        event.envelope["answer_hmac"] = "not-an-accepted-native-answer"
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=3))
        self.assertEqual(result["reason"], coordination.CORRUPT)
        self.assertNotIn("delegated_receipt", result)

    def test_native_mid_with_changed_body_is_not_a_delegated_receipt(self):
        _inv, _subscription, _request, task, event = self.pair()
        self.assertEqual(self.send("cart")[0], 1)
        IgFollowUpTask.objects.filter(pk=task.pk).update(message_text="unrelated customer fact")
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], coordination.CORRUPT)
        self.assertNotIn("delegated_receipt", result)

    def test_receipt_truth_survives_manager_decision_expiry_without_replay(self):
        _inv, _subscription, _request, task, event = self.pair()
        self.assertEqual(self.send("cart")[0], 1)
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(days=2))
        self.assertEqual(result["reason"], coordination.FULFILLED)
        self.assertEqual(result["delegated_receipt"]["task_id"], task.pk)

    def test_second_decision_is_rejected_and_actual_unknown_update_keeps_holding(self):
        _inv, _subscription, request, task, event = self.pair()
        count, physical = self.send("cart", outcome="unknown")
        self.assertEqual((count, len(physical)), (0, 1))
        task.refresh_from_db()
        self.assertEqual(task.status, "ambiguous")
        original_decision = reviews.Decision.objects.get(request=request)
        result = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
            decisions_by_line=[{"line_id": request.envelope["lines"][0]["line_id"],
                "availability": "wait", "checked_quantity": 1, "supply_basis": "accessible_unrecorded"}],
            now=self.now + timedelta(seconds=4))
        self.assertEqual(result, {"ok": False, "reason": "decision_already_recorded", "ready": False})
        self.assertEqual(reviews.Decision.objects.filter(request=request).count(), 1)
        original_decision.refresh_from_db()
        self.assertEqual(original_decision.envelope["lines"][0]["availability"], "available")
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=4))
        self.assertEqual(result["reason"], coordination.HOLD)
        self.assertNotIn("delegated_receipt", result)
        self.assertEqual(self.send("restock", at=self.now + timedelta(seconds=4)), (0, []))
