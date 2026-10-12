"""A native restock agreement may reuse only its original exact cart review."""
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings

from management.ig_fulfillment_review_models import (
    IgFulfillmentReviewDecision as Decision,
    IgFulfillmentReviewRequest as Request,
)
from management.ig_restock_subscription_models import (
    IgRestockAvailabilityEvent as Event, IgRestockInvitation, IgRestockSubscription,
)
from management.models import (
    IgBotNotification, IgClient, IgCustomerTurnRevision, IgFollowUpTask,
    IgRevisionDeliveryEffect, InstagramBotMessage,
)
from management.services import ig_fulfillment_delivery as cart_delivery
from management.services import ig_fulfillment_notification_coordination as coordination
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_subscriptions as restock
from management.services.ig_restock_availability import observe_target
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True)
class NativeInvitationReviewReuseTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(
            username="original-review-manager", email="reuse@example.test", password="test")

    def original(self, *, availability="available", pending=False, ack=False, request_source=None):
        if request_source is None:
            invitation, subscription = self.accepted(ack=ack)
        else:
            result, source, revision, token = self.request(source=request_source)
            self.assertTrue(result.invitation_id, result.reason)
            self.sent_card(revision, token, source, result)
            restock.record_delivery(revision.pk, now=self.now)
            invitation = IgRestockInvitation.objects.get(pk=result.invitation_id)
            accepted, answer_source, _revision, _token = self.reply(invitation)
            self.assertEqual(accepted.reason, "accept", accepted)
            subscription = IgRestockSubscription.objects.get(accepted_answer__source=answer_source)
        request = Request.objects.get(pk=invitation.envelope["manager_review"]["request_id"])
        self.assertEqual(request.envelope["scope"], "cart")
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)
        decision = None if pending else self.decide(request, availability=availability)
        return invitation, subscription, request, decision

    def decide(self, request, *, availability="available", at=None, actor=None):
        now = at or self.now + timedelta(seconds=2)
        rows = [{"line_id": line["line_id"], "availability": availability,
            "checked_quantity": line["target"]["quantity"], "supply_basis": "accessible_unrecorded"}
            for line in request.envelope["lines"]]
        with patch("django.utils.timezone.now", return_value=now):
            result = reviews.apply_fulfillment_decision(request.pk, actor=actor or self.manager,
                expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
                decisions_by_line=rows, now=now)
        self.assertTrue(result["ready"], result)
        return Decision.objects.get(pk=result["decision_id"])

    def observation(self, invitation, *, at=None):
        return observe_target(invitation.target, now=at or self.now + timedelta(seconds=3)).as_dict()

    def no_admission(self, invitation, subscription, *, at=None):
        now = at or self.now + timedelta(seconds=3)
        result = reviews.restock_manager_admission(subscription, self.observation(invitation, at=now), now=now)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["proof"], {})
        self.assertFalse(Event.objects.filter(subscription=subscription).exists())
        return result

    def fallback(self, invitation, subscription, original, *, at=None):
        now = at or self.now + timedelta(seconds=3)
        observed = self.observation(invitation, at=now)
        with patch("django.utils.timezone.now", return_value=now):
            result = reviews.ensure_restock_manager_review(subscription, observed, now=now)
        self.assertTrue(result["created"], result)
        self.assertNotEqual(result["request_id"], str(original.pk))
        request = Request.objects.get(pk=result["request_id"])
        self.assertEqual(request.envelope["scope"], "restock")
        self.assertEqual(request.envelope["subscription_id"], str(subscription.pk))
        again = reviews.ensure_restock_manager_review(subscription, observed, now=now)
        self.assertFalse(again["created"], again)
        self.assertEqual(again["request_id"], str(request.pk))
        self.no_admission(invitation, subscription, at=now)
        return request

    def reconcile_original(self, *, request_source=None):
        invitation, subscription, request, decision = self.original(request_source=request_source)
        now = self.now + timedelta(seconds=3)
        observed = self.observation(invitation, at=now)
        with patch("management.services.instagram_bot._provider_http") as provider:
            ensured = reviews.ensure_restock_manager_review(subscription, observed, now=now)
            admission = reviews.restock_manager_admission(subscription, observed, now=now)
            result = restock.reconcile_subscriptions(now=now)
        provider.assert_not_called()
        self.assertFalse(ensured["created"], ensured)
        self.assertTrue(ensured["reused"], ensured)
        self.assertEqual(ensured["request_id"], str(request.pk))
        self.assertTrue(admission["ready"], admission)
        self.assertEqual(result, {"checked": 1, "created": 1})
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)
        event = Event.objects.get(subscription=subscription)
        proof = event.envelope["manager_decision"]
        self.assertEqual(proof, admission["proof"])
        for name, expected in {
            "review_scope": "cart", "request_id": str(request.pk),
            "request_digest": request.envelope_hmac, "decision_id": str(decision.pk),
            "decision_version": decision.version, "decision_digest": decision.envelope_hmac,
            "subscription_id": str(subscription.pk), "target": invitation.target,
            "checked_quantity": invitation.target["quantity"],
            "line_id": invitation.envelope["selection"]["line_id"],
            "invitation_id": str(invitation.pk), "invitation_digest": invitation.envelope_hmac,
            "answer_id": str(subscription.accepted_answer.pk),
            "answer_digest": subscription.accepted_answer.envelope_hmac,
        }.items():
            self.assertEqual(proof[name], expected, name)
        self.assertTrue(restock._verified(event, "event"))
        task = IgFollowUpTask.objects.get(reason=reviews.UPDATE_REASON)
        return invitation, subscription, request, task, event

    def send(self, purpose, *, unknown=False, at=None):
        from management.services import instagram_bot as bot
        now = at or self.now + timedelta(seconds=4)
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(
            state="processed", active_slot=None, claim_token="", lease_until=None)
        physical = []
        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if unknown:
                raise TimeoutError("provider request occurred; native receipt is unknown")
            return 200, json.dumps({"message_id": "native-original-cart-availability"})
        with patch("django.utils.timezone.now", return_value=now), \
                patch.object(bot, "get_page_token", return_value="synthetic-reuse-token"), \
                patch.object(bot, "_provider_http", side_effect=provider):
            if purpose == "cart":
                count = cart_delivery.process_due_fulfillment_updates(self.settings, now=now, limit=20)
            else:
                count = restock.process_due_restock_notifications(self.settings, now=now, limit=20)
        return count, physical

    def test_original_decision_creates_signed_event_without_second_manager_case_or_alert(self):
        _invitation, _subscription, _request, _task, event = self.reconcile_original()
        self.assertEqual(event.envelope["availability"]["status"], "unavailable")
        self.assertEqual(event.envelope["manager_decision"]["supply_basis"], "accessible_unrecorded")
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 0)

    def test_loaded_reuse_event_descriptor_adds_zero_queries_to_bounded_context(self):
        from django.test.utils import CaptureQueriesContext
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_restock_context import capture_restock_interest
        _inv, _sub, request, task, event = self.reconcile_original()
        now = self.now + timedelta(seconds=3)
        # Both measurements start with the same loaded owner; otherwise the
        # baseline alone pays Django's one-time lazy client relation query.
        request = Request.objects.select_related("client").get(pk=request.pk)
        cart = capture_current_selection_lines(self.client_row.pk, now=now)
        decision = Decision.objects.get(pk=task.event_payload["decision_id"])
        with CaptureQueriesContext(connection) as baseline:
            ordinary = capture_restock_interest(request, self.settings, cart, now)
        with CaptureQueriesContext(connection) as measured, patch.object(
                reviews, "_cart_restock_review", side_effect=AssertionError("UI cannot run send admission")):
            described = capture_restock_interest(request, self.settings, cart, now, reviewed_decision=decision)
        self.assertEqual(ordinary["status"], "accepted")
        self.assertEqual(len(measured), len(baseline))
        self.assertLessEqual(len(measured), 8)
        proof = described["manager_review_reuse"]
        self.assertEqual(proof["event_id"], str(event.pk))
        self.assertEqual(proof["decision_id"], str(decision.pk))
        self.assertEqual(proof["checked_at"], decision.envelope["decided_at"])
        self.assertEqual(proof["provenance"], "availability_event")

    def test_actual_original_cart_receipt_fulfills_restock_without_second_http(self):
        _invitation, subscription, _request, task, event = self.reconcile_original()
        count, physical = self.send("cart")
        self.assertEqual((count, len(physical)), (1, 1))
        task.refresh_from_db()
        self.assertEqual(task.provider_message_id, "native-original-cart-availability")
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=5))
        self.assertEqual(result["reason"], coordination.FULFILLED, result)
        self.assertEqual(result["delegated_receipt"]["task_id"], task.pk)
        self.assertEqual(result["delegated_receipt"]["subscription_id"], str(subscription.pk))
        self.assertEqual(self.send("restock", at=self.now + timedelta(seconds=5)), (0, []))
        event.task.refresh_from_db()
        self.assertEqual(event.task.provider_message_id, "")

    def test_unknown_original_cart_receipt_holds_restock_without_second_http(self):
        _invitation, _subscription, _request, task, event = self.reconcile_original()
        count, physical = self.send("cart", unknown=True)
        self.assertEqual((count, len(physical)), (0, 1))
        task.refresh_from_db()
        self.assertEqual(task.status, "ambiguous")
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=5))
        self.assertEqual(result["reason"], coordination.HOLD, result)
        self.assertEqual(self.send("restock", at=self.now + timedelta(seconds=5)), (0, []))

    def test_two_line_cart_reuses_only_invited_line_and_full_native_update_fulfills_it(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        second_product, second_variant = self.products[1], self.variants[1]
        second_variant.stock = 0
        second_variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=second_product, source="catalog_variant")
        VariantSizeRule.objects.create(variant=second_variant, fit_code="classic", size="L",
            stock=0, is_enabled=True)
        storm_source = self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        invitation, subscription, request, task, event = self.reconcile_original(request_source=storm_source)
        self.assertEqual(len(request.envelope["lines"]), 2)
        self.assertEqual(invitation.target["product_id"], second_product.pk)
        self.assertEqual(event.envelope["manager_decision"]["checked_quantity"], 2)
        count, physical = self.send("cart")
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertIn(self.product.title, physical[0]["message"]["text"])
        self.assertIn(second_product.title, physical[0]["message"]["text"])
        result = coordination.restock_update_coordination(event, self.settings, self.now + timedelta(seconds=5))
        self.assertEqual(result["reason"], coordination.FULFILLED, result)
        self.assertEqual(result["delegated_receipt"]["task_id"], task.pk)
        self.assertEqual(result["delegated_receipt"]["subscription_id"], str(subscription.pk))
        self.assertEqual(self.send("restock", at=self.now + timedelta(seconds=5)), (0, []))

    def assert_nonavailable_reuse(self, *, availability="available", pending=False):
        invitation, subscription, request, _decision = self.original(availability=availability, pending=pending)
        now = self.now + timedelta(seconds=3)
        result = reviews.ensure_restock_manager_review(subscription, self.observation(invitation), now=now)
        self.assertFalse(result["created"], result)
        self.assertEqual(result["request_id"], str(request.pk))
        self.no_admission(invitation, subscription)
        self.assertEqual(restock.reconcile_subscriptions(now=now)["created"], 0)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)

    def test_pending_original_suppresses_second_alert_without_admitting_event(self):
        self.assert_nonavailable_reuse(pending=True)

    def test_pending_configuration_change_opens_one_standing_fallback(self):
        invitation, subscription, request, _decision = self.original(pending=True)
        now = self.now + timedelta(seconds=3)
        before = reviews._configuration(invitation.target, now=now)
        self.product.fit_options.filter(code="classic").update(label="Classic revised manager label")
        after = reviews._configuration(invitation.target, now=now)
        self.assertEqual(after["status"], "valid", after)
        self.assertEqual(after["target"], invitation.target)
        self.assertNotEqual(before["configuration_digest"], after["configuration_digest"])
        self.fallback(invitation, subscription, request, at=now)
        self.assertEqual(Request.objects.count(), 2)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 2)

    def test_pending_original_older_than_twenty_four_hours_opens_one_standing_fallback(self):
        invitation, subscription, request, _decision = self.original(pending=True)
        now = request.created_at + timedelta(hours=24, microseconds=1)
        self.fallback(invitation, subscription, request, at=now)
        self.assertEqual(Request.objects.count(), 2)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 2)

    def test_wait_original_suppresses_second_alert_without_admitting_event(self):
        self.assert_nonavailable_reuse(availability="wait")

    def test_unavailable_original_suppresses_second_alert_without_admitting_event(self):
        self.assert_nonavailable_reuse(availability="unavailable")

    def test_mutated_original_source_cannot_authorize_reuse(self):
        invitation, subscription, _request, _decision = self.original()
        invitation.request_source.text = "Це інший запит і строк"
        invitation.request_source.save(update_fields=["text"])
        self.no_admission(invitation, subscription)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=3))["created"], 0)

    def test_new_deadline_keeps_standing_fallback_but_cannot_reuse_cart_decision(self):
        invitation, subscription, request, _decision = self.original()
        self.revision(self.source("Встигнете відправити до 20.10.2026?", at=self.now + timedelta(seconds=3)))
        self.fallback(invitation, subscription, request, at=self.now + timedelta(seconds=4))

    def test_quantity_amendment_cannot_borrow_original_checked_quantity(self):
        invitation, subscription, request, _decision = self.original()
        self.canonical("количество 2", at=self.now + timedelta(seconds=3))
        self.fallback(invitation, subscription, request, at=self.now + timedelta(seconds=4))

    def test_disabled_actual_configuration_cannot_reuse_available_proof(self):
        invitation, subscription, _request, _decision = self.original()
        self.product.fit_options.filter(code="classic").update(is_active=False)
        self.no_admission(invitation, subscription)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=3))["created"], 0)

    def test_decision_at_twenty_four_hours_is_fresh_then_opens_one_fallback(self):
        invitation, subscription, request, decision = self.original()
        boundary = decision.created_at + timedelta(hours=24)
        admitted = reviews.restock_manager_admission(subscription, self.observation(invitation, at=boundary), now=boundary)
        self.assertTrue(admitted["ready"], admitted)
        self.assertEqual(admitted["proof"]["request_id"], str(request.pk))
        self.fallback(invitation, subscription, request, at=boundary + timedelta(microseconds=1))

    def test_inactive_decision_actor_requires_standing_fallback(self):
        invitation, subscription, request, _decision = self.original()
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])
        self.fallback(invitation, subscription, request)

    def test_native_revoke_cannot_reuse_original_available_decision(self):
        invitation, subscription, _request, _decision = self.original(ack=True)
        answer, _source, _revision, _token = self.reply(invitation, action="revoke",
            reply_to="native-restock-ack", at=self.now + timedelta(seconds=3))
        self.assertEqual(answer.reason, "revoke")
        subscription.refresh_from_db()
        self.no_admission(invitation, subscription, at=self.now + timedelta(seconds=4))

    def test_unknown_native_invitation_checkpoint_cannot_authorize_reuse(self):
        invitation, subscription, _request, _decision = self.original()
        IgRevisionDeliveryEffect.objects.filter(pk=subscription.accepted_answer.receipt.effect_id).update(state="unknown")
        subscription = type(subscription).objects.select_related(
            "accepted_answer__invitation", "accepted_answer__receipt__effect", "accepted_answer__source").get(pk=subscription.pk)
        self.no_admission(invitation, subscription)

    def test_permission_epoch_change_cannot_authorize_reuse(self):
        invitation, subscription, _request, _decision = self.original()
        self.settings.reply_permission_epoch += 1
        self.settings.save(update_fields=["reply_permission_epoch"])
        self.no_admission(invitation, subscription)

    def test_privacy_erasure_cannot_authorize_reuse(self):
        invitation, subscription, _request, _decision = self.original()
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now)
        self.no_admission(invitation, subscription)

    def assert_final_read_fence(self, mutate):
        invitation, subscription, request, _decision = self.original()
        now = self.now + timedelta(seconds=3)
        actual_decision_reason = reviews._decision_reason
        actual_cart_review = reviews._cart_restock_review
        mutations = []
        descriptors = []
        def change_after_actual_validation(candidate, decision, **kwargs):
            reason = actual_decision_reason(candidate, decision, **kwargs)
            self.assertEqual(reason, "")
            if not mutations:
                mutations.append(candidate.pk)
                mutate(invitation, request, now)
            return reason
        def capture_actual_descriptor(*args, **kwargs):
            result = actual_cart_review(*args, **kwargs)
            descriptors.append(result)
            return result
        for purpose in ("ensure", "admission"):
            with self.subTest(purpose=purpose), transaction.atomic():
                mutations.clear()
                descriptors.clear()
                with patch.object(reviews, "_decision_reason", side_effect=change_after_actual_validation), \
                        patch.object(reviews, "_cart_restock_review", side_effect=capture_actual_descriptor):
                    if purpose == "ensure":
                        result = reviews.ensure_restock_manager_review(subscription, self.observation(invitation), now=now)
                        self.assertFalse(result["created"], result)
                        self.assertFalse(result.get("reused", False), result)
                        self.assertEqual(result["reason"], "review_read_conflict")
                    else:
                        result = reviews.restock_manager_admission(subscription, self.observation(invitation), now=now)
                        self.assertFalse(result["ready"], result)
                        self.assertEqual(result["proof"], {})
                        self.assertEqual(result["reason"], "manager_review_stale")
                self.assertEqual(mutations, [request.pk])
                self.assertEqual(len(descriptors), 1)
                descriptor = descriptors[0]
                self.assertFalse(descriptor["eligible"], descriptor)
                self.assertFalse(descriptor["ready"], descriptor)
                self.assertEqual(descriptor["proof"], {})
                self.assertEqual(descriptor["reason"], "review_read_conflict")
                self.assertEqual(Request.objects.count(), 1)
                self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)
                self.assertFalse(Event.objects.filter(subscription=subscription).exists())
                # Each public path receives the same real pre-mutation frame.
                transaction.set_rollback(True)

    def test_original_source_mutation_between_initial_validation_and_final_read_blocks_reuse(self):
        def mutate(_invitation, request, _now):
            request.source.text = "Змінений вихідний запит після першої перевірки"
            request.source.save(update_fields=["text"])
        self.assert_final_read_fence(mutate)

    def test_cart_quantity_mutation_between_initial_validation_and_final_read_blocks_reuse(self):
        def mutate(_invitation, _request, now):
            self.canonical("количество 2", at=now)
        self.assert_final_read_fence(mutate)

    def test_settings_epoch_mutation_between_initial_validation_and_final_read_blocks_reuse(self):
        def mutate(_invitation, _request, _now):
            type(self.settings).objects.filter(pk=self.settings.pk).update(
                reply_permission_epoch=self.settings.reply_permission_epoch + 1)
        self.assert_final_read_fence(mutate)

    def test_newer_cart_scope_cannot_replace_original_invitation_bound_decision(self):
        invitation, subscription, original, _decision = self.original()
        now = self.now + timedelta(seconds=3)
        revision, _token = self.revision(self.source("Встигнете відправити до 20.10.2026?", at=now))
        with patch("django.utils.timezone.now", return_value=now):
            result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=now)
        newer = Request.objects.get(pk=result["request_id"])
        self.assertNotEqual(newer.pk, original.pk)
        self.decide(newer, at=self.now + timedelta(seconds=4))
        standing = self.fallback(invitation, subscription, original, at=self.now + timedelta(seconds=5))
        self.assertNotEqual(standing.pk, newer.pk)

    def test_later_standing_denial_prevents_reviving_original_available_proof(self):
        invitation, subscription, original, _decision = self.original()
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])
        standing = self.fallback(invitation, subscription, original)
        other_manager = get_user_model().objects.create_superuser(
            username="standing-review-manager", email="standing@example.test", password="test")
        self.decide(standing, availability="unavailable", actor=other_manager, at=self.now + timedelta(seconds=4))
        self.manager.is_active = True
        self.manager.save(update_fields=["is_active"])
        self.no_admission(invitation, subscription, at=self.now + timedelta(seconds=5))
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=5))["created"], 0)
