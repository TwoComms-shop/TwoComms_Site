"""Genuine assisted approval -> original native update -> owned ALL-list consent."""
from copy import deepcopy
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings

from management.models import (
    IgCheckoutInventoryReservation, IgCheckoutProposal, IgCustomerTurnRevision,
    IgFollowUpTask, IgRevisionDeliveryEffect, InstagramBotMessage,
)
from management.services import ig_assisted_commerce as commerce
from management.services import ig_fulfillment_delivery as delivery
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_followup as followup
from management.services import ig_restock_subscriptions as restock
from management.services import ig_restock_waiting_list as waiting
from management.tests_ig_restock_waiting_list import RestockWaitingListFixture
from orders.models import Order, PaymentAttempt
from product_catalog.models import VariantSizeRule
from productcolors.models import ProductColorVariant


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    SITE_BASE_URL="https://twocomms.test")
class NativeAssistedRestockFollowupTests(RestockWaitingListFixture, TransactionTestCase):
    reset_sequences = True

    def approved_update(self, *, mixed=False, locale=None, ready_index=None, title="Худі", photo=False):
        """Every fixture uses the actual source, request, draft and CAS approval."""
        if locale is None and ready_index is None and title == "Худі" and not photo:
            request, decision = self.waiting_source_case(mixed=mixed)
        else:
            from management.services.ig_commerce_projection import capture_current_selection_lines
            self.canonical("цвет purple, размер L")
            self.canonical("добавьте Restock Aurora, количество 1, размер M, цвет black, крій classic" if mixed else
                "добавьте Storm, количество 1, размер L, цвет olive, крій classic")
            source = self.source({
                "uk": "Будь ласка, відповідайте українською. Перевірте всі позиції.",
                "ru": "Пожалуйста, отвечайте по-русски. Проверьте все позиции.",
                "en": "Please reply in English. Check all items.",
            }[locale or "uk"])
            revision, _token = self.revision(source)
            capture = capture_current_selection_lines(self.client_row.pk, now=self.now)
            request, _created = commerce.create_assisted_request(revision.pk, settings_id=self.settings.pk,
                operation_id=uuid.uuid4(), expected_revision_digest=revision.snapshot_digest,
                expected_full_capture_digest=capture["capture_digest"], now=self.now)
            items = commerce._editable_items(request.envelope["snapshot"])
            for index, row in enumerate(items):
                ready = index == ready_index
                row.update(availability="ready" if ready else "wait", checked_quantity=row["quantity"] if ready else None,
                    supply_basis="accessible_unrecorded" if ready else "incoming_confirmed",
                    source_location="Менеджер перевірив точні позиції")
                if row["item_kind"] == "custom_apparel":
                    row.update(title=title, unit_price="1500.00")
                    if photo:
                        from management.tests_ig_assisted_commerce_models import NativeAssistedCommerceModelsTests
                        image = NativeAssistedCommerceModelsTests.produced_image(self, request)
                        row.update(image_id=str(image.pk), image_digest=image.content_digest)
                row["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                    "answer": "Параметри підтверджено; готовність ще не підтверджена."}
                    for question in request.envelope["snapshot"]["questions"] if question["line_id"] == row["line_id"]]
            draft, _created = self.record(request, items=items)
            decision, _created = self.record(request, status="approved", latest=draft)
        task = IgFollowUpTask.objects.filter(event_key=f"ig-assisted-manager-update:{request.pk}:{decision.version}").first()
        return {"request": request, "decision": decision, "task": task}

    def dispatch_update(self, *, missing_mid=False, unknown=False, before_http=None, now=None):
        from management.services import instagram_bot as bot
        now = now or self.now
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(
            state="processed", active_slot=None, claim_token="", lease_until=None)
        physical = []

        def lookup(_settings, **kwargs):
            if before_http:
                before_http()
            return "synthetic-assisted-followup-token"

        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if unknown:
                raise TimeoutError("Native assisted update reached provider; result unknown")
            return 200, json.dumps({} if missing_mid else {"message_id": "native-assisted-manager-update"})

        with patch("django.utils.timezone.now", return_value=now), \
                patch.object(bot, "get_page_token", side_effect=lookup), \
                patch.object(bot, "_provider_http", side_effect=provider):
            count = delivery.process_due_fulfillment_updates(self.settings, now=now)
        return count, physical

    def sent_update(self, **kwargs):
        case = self.approved_update(**kwargs)
        self.assertIsNotNone(case["task"], "Genuine ALL-WAIT approval must enqueue its original manager update")
        count, physical = self.dispatch_update()
        self.assertEqual((count, len(physical)), (1, 1))
        invitation = restock.Invitation.objects.get(request_source=case["request"].source)
        receipt = restock.Receipt.objects.get(invitation=invitation)
        return {**case, "invitation": invitation, "receipt": receipt, "physical": physical}

    def accept_update(self, **kwargs):
        case = self.sent_update(**kwargs)
        result, answer, _revision, _token = self.reply(case["invitation"], reply_to=case["receipt"].provider_message_id)
        self.assertEqual(result.reason, "accept", result)
        return {**case, "subscription": restock.Subscription.objects.get(accepted_answer__source=answer)}

    def inventory(self):
        return (list(ProductColorVariant.objects.order_by("pk").values_list("pk", "stock")),
            list(VariantSizeRule.objects.order_by("pk").values_list("pk", "stock")))

    def captured_cart(self, *, now=None):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        capture = capture_current_selection_lines(self.client_row.pk, now=now or self.now)
        self.assertEqual(capture["status"], "captured", capture)
        self.assertTrue(capture["coverage_complete"], capture)
        return capture

    @staticmethod
    def cart_quantities(capture):
        return [row["fields"]["quantity"]["value"] for row in capture["lines"]]

    def change_second_quantity(self, *, now):
        before = self.captured_cart(now=now)
        self.assertEqual(self.cart_quantities(before), [1, 1], before)
        self.canonical("измените позицию 2, количество 3", at=now)
        after = self.captured_cart(now=now)
        self.assertEqual([row["line_id"] for row in after["lines"]], [row["line_id"] for row in before["lines"]])
        self.assertEqual(self.cart_quantities(after), [1, 3], after)
        self.assertNotEqual(commerce._configuration_capture(before), commerce._configuration_capture(after))
        return before, after

    def context_boundary(self, source, now):
        return {"client_id": self.client_row.pk, "episode_id": self.client_row.current_commercial_episode_id,
            "order_id": None, "line_id": "new-cart", "recipient_id": "self", "reset_floor": 1,
            "source_namespace": source.provider_namespace, "reset_id": None, "erasure_epoch": "",
            "source_watermark": {"message_id": source.pk, "event_at": now.isoformat()}}

    def native_accept_and_cancel(self, locale, *, photo=False):
        from management.services import instagram_bot as bot
        from management.services.ig_revision_live import execute_claimed_revision
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        inventory = self.inventory()
        before_effects = IgRevisionDeliveryEffect.objects.count()
        case = self.sent_update(locale=locale, mixed=True, photo=photo)
        invitation, receipt = case["invitation"], case["receipt"]
        self.assertEqual(invitation.locale, locale)
        self.assertIsNone(receipt.effect_id)
        self.assertEqual(receipt.followup_task_id, case["task"].pk)
        self.assertEqual(IgRevisionDeliveryEffect.objects.count(), before_effects)
        self.assertEqual(case["physical"][0]["message"]["quick_replies"], [restock._native_button(invitation)])
        self.assertFalse(restock.Subscription.objects.exists())
        original_envelope, original_target = deepcopy(invitation.envelope), deepcopy(invitation.target)
        physical = []

        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            return 200, json.dumps({"message_id": f"native-assisted-agreement-{len(physical)}"})

        with patch.object(bot, "get_page_token", return_value="synthetic-assisted-answer-token"), \
                patch.object(bot, "_provider_http", side_effect=provider), \
                patch("management.services.call_ai_analysis.gemini_generate_text",
                    side_effect=AssertionError("RS1 agreement must bypass Gemini")) as gemini:
            answer = self.source(invitation.button_title, payload=invitation.accept_payload,
                at=self.now + timedelta(seconds=1))
            answer.reply_to_provider_message_id = receipt.provider_message_id
            answer.save(update_fields=["reply_to_provider_message_id"])
            revision, token = self.revision(answer)
            with patch("django.utils.timezone.now", return_value=answer.provider_created_at):
                accepted = execute_claimed_revision(revision.pk, token, self.settings)
            self.assertEqual(accepted.state, "completed", accepted.reasons)
            subscription = restock.Subscription.objects.get(accepted_answer__source=answer)
            self.assertEqual(subscription.state, "active")
            ack = IgRevisionDeliveryEffect.objects.get(revision=revision, source_message=answer)
            self.assertEqual(ack.payload, physical[0])
            self.assertEqual(physical[0]["message"]["quick_replies"], [restock._native_button(invitation, "revoke")])
            cancel = self.source("Cancel", payload=invitation.revoke_payload, at=self.now + timedelta(seconds=2))
            cancel.reply_to_provider_message_id = ack.provider_message_id
            cancel.save(update_fields=["reply_to_provider_message_id"])
            revision, token = self.revision(cancel)
            with patch("django.utils.timezone.now", return_value=cancel.provider_created_at):
                revoked = execute_claimed_revision(revision.pk, token, self.settings)
            self.assertEqual(revoked.state, "completed", revoked.reasons)
            subscription.refresh_from_db()
            self.assertEqual(subscription.state, "revoked")
            self.assertEqual(len(physical), 2)
            gemini.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual((invitation.envelope, invitation.target), (original_envelope, original_target))
        self.assertEqual(self.inventory(), inventory)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        return {**case, "subscription": subscription, "latest_source": cancel}

    def test_custom_all_wait_approval_owns_task_receipt_without_fake_fulfillment_or_revision_effect(self):
        before_requests = reviews.Request.objects.count()
        before_effects = IgRevisionDeliveryEffect.objects.count()
        case = self.sent_update()
        task, request, decision = case["task"], case["request"], case["decision"]
        self.assertEqual((task.kind, task.reason, task.trigger), ("fulfillment", "fulfillment_manager_update", "event"))
        self.assertEqual(task.event_payload["source_kind"], "assisted_manager_update.v1")
        self.assertEqual(task.event_payload["request_id"], str(request.pk))
        self.assertEqual(task.event_payload["decision_id"], str(decision.pk))
        self.assertEqual(task.event_payload["decision_version"], decision.version)
        self.assertEqual(task.event_payload["request_digest"], request.envelope_hmac)
        self.assertEqual(task.event_payload["decision_digest"], decision.envelope_hmac)
        self.assertEqual({item["item_kind"] for item in case["invitation"].target["items"]}, {"custom_apparel"})
        self.assertEqual(len(case["invitation"].target["items"]), 2)
        self.assertEqual(reviews.Request.objects.count(), before_requests)
        self.assertEqual(IgRevisionDeliveryEffect.objects.count(), before_effects)
        self.assertEqual(case["receipt"].followup_task_id, task.pk)
        self.assertIsNone(case["receipt"].effect_id)
        self.assertTrue(restock._valid_receipt(case["invitation"], case["receipt"]))

    def test_mixed_all_wait_preserves_native_kinds_and_one_positive_all_button(self):
        case = self.sent_update(mixed=True)
        invitation = case["invitation"]
        self.assertEqual({item["item_kind"] for item in invitation.target["items"]}, {"custom_apparel", "catalog_apparel"})
        self.assertEqual(sum(item["quantity"] for item in invitation.target["items"]), 2)
        self.assertTrue(waiting.is_list(invitation.target))
        self.assertEqual(case["physical"][0], followup._plan(invitation))
        self.assertEqual(len(case["physical"][0]["message"]["quick_replies"]), 1)
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertEqual(self.dispatch_update(), (0, []))

    def test_uk_native_acceptance_ack_and_cancel_bypass_gemini(self):
        self.native_accept_and_cancel("uk")

    def test_ru_native_acceptance_ack_and_cancel_bypass_gemini(self):
        self.native_accept_and_cancel("ru")

    def test_en_native_acceptance_ack_and_cancel_bypass_gemini(self):
        self.native_accept_and_cancel("en")

    def test_replayed_approval_creates_exactly_one_customer_update(self):
        case = self.approved_update()
        request, decision = case["request"], case["decision"]
        draft = commerce._models()[1].objects.get(request=request, version=decision.version - 1)
        replay, created = self.record(request, status="approved", latest=draft, operation_id=decision.operation_id)
        self.assertFalse(created)
        self.assertEqual(replay.pk, decision.pk)
        self.assertEqual(IgFollowUpTask.objects.filter(event_key=f"ig-assisted-manager-update:{request.pk}:{decision.version}").count(), 1)
        self.assertEqual(self.dispatch_update()[0], 1)
        self.assertEqual(self.dispatch_update(), (0, []))

    def test_ready_wait_partial_approval_never_mints_all_wait_optin(self):
        self.approved_update(mixed=True, ready_index=0)
        self.dispatch_update()
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_unsigned_customer_wish_and_manager_draft_do_not_enqueue_optin(self):
        request, _revision = self.request_case()
        draft, _created = self.record(request, items=self.items_for(request, availability="wait"))
        self.assertEqual(draft.status, "draft")
        self.assertFalse(IgFollowUpTask.objects.filter(event_key__startswith="ig-assisted-manager-update:").exists())
        self.assertEqual(self.dispatch_update(), (0, []))
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_changed_quantity_before_delivery_cannot_reuse_old_assisted_approval(self):
        self.approved_update()
        self.change_second_quantity(now=self.now + timedelta(seconds=1))
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(seconds=1)), (0, []))
        self.assertFalse(restock.Receipt.objects.exists())

    def test_new_cart_item_before_delivery_cannot_disappear_from_all_consent(self):
        self.approved_update()
        before = self.captured_cart()
        self.assertEqual(self.cart_quantities(before), [1, 1], before)
        self.canonical("добавьте Restock Aurora, количество 1, размер M, цвет black, крій classic",
            at=self.now + timedelta(seconds=1))
        after = self.captured_cart(now=self.now + timedelta(seconds=1))
        self.assertEqual(len(after["lines"]), len(before["lines"]) + 1, after)
        self.assertEqual(self.cart_quantities(after), [1, 1, 1], after)
        self.assertEqual([row["line_id"] for row in after["lines"][:-1]], [row["line_id"] for row in before["lines"]])
        self.assertNotEqual(commerce._configuration_capture(before), commerce._configuration_capture(after))
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(seconds=1)), (0, []))
        self.assertFalse(restock.Receipt.objects.exists())

    def test_new_recipient_before_delivery_requires_new_approval(self):
        self.approved_update()
        before = self.captured_cart()
        self.assertEqual(self.cart_quantities(before), [1, 1], before)
        self.canonical("добавьте Restock Aurora для друга, количество 1, размер M, цвет black, крій classic",
            at=self.now + timedelta(seconds=1))
        after = self.captured_cart(now=self.now + timedelta(seconds=1))
        self.assertEqual(len(after["lines"]), len(before["lines"]) + 1, after)
        self.assertEqual(self.cart_quantities(after), [1, 1, 1], after)
        self.assertEqual([row["recipient_id"] for row in before["lines"]], ["self", "self"])
        self.assertEqual(after["lines"][-1]["recipient_id"], "friend", after)
        self.assertNotEqual(commerce._configuration_capture(before), commerce._configuration_capture(after))
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(seconds=1)), (0, []))
        self.assertFalse(restock.Receipt.objects.exists())

    def test_native_catalog_color_change_before_delivery_cannot_retarget_mixed_offer(self):
        self.approved_update(mixed=True)
        before = self.captured_cart()
        self.assertEqual(self.cart_quantities(before), [1, 1], before)
        original_pk, original_color = self.variant.pk, self.variant.color_id
        self.edit_native_variant_color()
        after = self.captured_cart()
        self.assertEqual(self.cart_quantities(after), self.cart_quantities(before), after)
        self.assertEqual(self.variant.pk, original_pk)
        self.assertNotEqual(self.variant.color_id, original_color)
        self.assertEqual(self.dispatch_update(), (0, []))
        self.assertFalse(restock.Receipt.objects.exists())

    def test_manager_revocation_at_actual_http_boundary_blocks_delivery(self):
        case = self.approved_update()
        def revoke():
            self.manager.is_active = False
            self.manager.save(update_fields=["is_active"])
        self.assertEqual(self.dispatch_update(before_http=revoke), (0, []))
        case["task"].refresh_from_db()
        self.assertNotEqual(case["task"].status, "sent")
        self.assertFalse(restock.Receipt.objects.exists())

    def test_expired_current_decision_cannot_authorize_new_native_send(self):
        case = self.approved_update()
        self.assertEqual(self.dispatch_update(now=case["decision"].expires_at + timedelta(seconds=1)), (0, []))
        self.assertFalse(restock.Receipt.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_missing_mid_is_unknown_and_never_replayed(self):
        case = self.approved_update()
        count, physical = self.dispatch_update(missing_mid=True)
        case["task"].refresh_from_db()
        self.assertEqual((count, len(physical), case["task"].status), (0, 1, "ambiguous"))
        self.assertFalse(restock.Receipt.objects.exists())
        invitation = restock.Invitation.objects.get(request_source=case["request"].source)
        response, _source, _revision, _token = self.reply(invitation)
        self.assertNotEqual(response.reason, "accept", response)
        bare = self.source("Так", at=self.now + timedelta(seconds=2))
        self.assertFalse(restock.handle_reply(bare, settings_obj=self.settings).handled)
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(minutes=16)), (0, []))

    def test_unknown_physical_outcome_is_not_consent_and_never_replayed(self):
        case = self.approved_update()
        count, physical = self.dispatch_update(unknown=True)
        case["task"].refresh_from_db()
        self.assertEqual((count, len(physical), case["task"].status), (0, 1, "ambiguous"))
        self.assertFalse(restock.Receipt.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(minutes=16)), (0, []))

    def test_original_native_checkpoint_repairs_after_new_cart_and_decision_expiry(self):
        case = self.approved_update()
        with patch.object(followup, "record_task_delivery", return_value=False):
            self.assertEqual(self.dispatch_update()[0], 1)
        self.assertFalse(restock.Receipt.objects.exists())
        self.change_second_quantity(now=self.now + timedelta(seconds=1))
        later = case["decision"].expires_at + timedelta(seconds=1)
        self.assertEqual(self.dispatch_update(now=later), (0, []))
        receipt = restock.Receipt.objects.get(followup_task=case["task"])
        self.assertEqual(receipt.provider_message_id, "native-assisted-manager-update")
        self.assertTrue(restock._valid_receipt(receipt.invitation, receipt))
        self.assertIsNone(receipt.effect_id)

    def test_corrupted_original_source_cannot_repair_historical_task_receipt(self):
        case = self.approved_update()
        with patch.object(followup, "record_task_delivery", return_value=False):
            self.assertEqual(self.dispatch_update()[0], 1)
        source = case["request"].source
        source.text = "Змінені початкові параметри"
        source.save(update_fields=["text"])
        self.assertFalse(followup.record_task_delivery(case["task"].pk, now=self.now + timedelta(seconds=1)))
        self.assertFalse(restock.Receipt.objects.exists())

    def test_accepted_list_survives_new_cart_and_waits_for_own_all_physical_review(self):
        case = self.accept_update(mixed=True)
        original = deepcopy(case["invitation"].target)
        self.change_second_quantity(now=self.now + timedelta(seconds=1))
        initial = self.now + timedelta(seconds=2)
        self.assertEqual(restock.reconcile_subscriptions(now=initial)["created"], 0)
        physical_request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(physical_request.envelope["scope"], "restock_list")
        later = initial + restock.CHECK_INTERVAL
        self.assertEqual(restock.reconcile_subscriptions(now=later)["created"], 1)
        count, physical = RestockWaitingListFixture.dispatch(self, now=later + timedelta(seconds=1))
        self.assertEqual((count, len(physical)), (1, 1))
        case["invitation"].refresh_from_db()
        case["subscription"].refresh_from_db()
        self.assertEqual(case["invitation"].target, original)
        self.assertEqual(case["subscription"].state, "sent")

    def test_partial_physical_review_and_estimated_date_cannot_notify_all(self):
        case = self.accept_update()
        initial = self.now + timedelta(seconds=2)
        self.assertEqual(restock.reconcile_subscriptions(now=initial)["created"], 0)
        _request, checked = self.physically_check(case, unavailable_index=1,
            preparation={"min_days": 7, "max_days": 10, "unit": "calendar"})
        self.assertTrue(checked["ready"], checked)
        later = initial + restock.CHECK_INTERVAL
        self.assertEqual(restock.reconcile_subscriptions(now=later)["created"], 0)
        self.assertEqual(RestockWaitingListFixture.dispatch(self, now=later), (0, []))
        self.assertFalse(restock.AvailabilityEvent.objects.exists())

    def test_all_physical_review_notification_does_not_create_order_payment_or_stock(self):
        inventory = self.inventory()
        case = self.accept_update()
        initial = self.now + timedelta(seconds=2)
        self.assertEqual(restock.reconcile_subscriptions(now=initial)["created"], 0)
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        later = initial + restock.CHECK_INTERVAL
        self.assertEqual(restock.reconcile_subscriptions(now=later)["created"], 1)
        count, physical = RestockWaitingListFixture.dispatch(self, now=later + timedelta(seconds=1))
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertEqual(RestockWaitingListFixture.dispatch(self, now=later + timedelta(seconds=2)), (0, []))
        self.assertEqual(self.inventory(), inventory)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

    def explicit_accepted_update(self):
        """41A explicit native agreement while its 41C update is still pending."""
        case = self.accepted_list()
        task = IgFollowUpTask.objects.get(
            event_key=f"ig-assisted-manager-update:{case['request'].pk}:{case['decision'].version}")
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.attempt_count, 0)
        receipt = restock.Receipt.objects.get(invitation=case["invitation"])
        self.assertIsNotNone(receipt.effect_id)
        self.assertIsNone(receipt.followup_task_id)
        self.assertEqual(case["subscription"].state, "active")
        return {**case, "task": task, "receipt": receipt}

    def test_explicit_41a_acceptance_suppresses_pending_original_wait_update_while_active(self):
        case = self.explicit_accepted_update()
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(seconds=2)), (0, []))
        case["task"].refresh_from_db()
        case["subscription"].refresh_from_db()
        self.assertNotEqual(case["task"].status, "sent")
        self.assertEqual(case["subscription"].state, "active")
        self.assertFalse(restock.Receipt.objects.filter(followup_task=case["task"]).exists())
        self.assertFalse(restock.AvailabilityEvent.objects.exists())

    def test_pending_original_wait_update_does_not_block_explicit_41a_all_ready_notification(self):
        case = self.explicit_accepted_update()
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        case["task"].refresh_from_db()
        self.assertEqual(case["task"].status, "pending")
        count, physical = RestockWaitingListFixture.dispatch(self)
        self.assertEqual((count, len(physical)), (1, 1))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "sent")
        event = restock.AvailabilityEvent.objects.get(subscription=case["subscription"])
        self.assertNotEqual(event.task_id, case["task"].pk)
        self.assertTrue(event.task.provider_message_id)
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(seconds=4)), (0, []))
        case["task"].refresh_from_db()
        self.assertNotEqual(case["task"].status, "sent")
        self.assertFalse(restock.Receipt.objects.filter(followup_task=case["task"]).exists())
        self.assertEqual(RestockWaitingListFixture.dispatch(self, now=self.now + timedelta(seconds=5)), (0, []))

    def test_unrelated_accepted_catalog_list_can_notify_beside_genuine_custom_wait_task(self):
        case = self.catalogue_accepted_list()
        original = deepcopy(case["invitation"].target)
        # Preserve the old agreement and form a different real customer wish:
        # Aurora/purple and Storm/olive replace Aurora/black and Storm/pink.
        self.canonical("удалите позицию 2")
        self.canonical("цвет purple, размер L")
        update = self.approved_update()
        self.assertIsNotNone(update["task"])
        from management.services import ig_assisted_manager_update as assisted_update
        current = assisted_update.validated_update_task(update["task"], self.settings, now=self.now)
        self.assertTrue(current["ready"], current)
        new_target = assisted_update.frozen_target(update["task"])
        self.assertNotEqual(new_target, original)
        self.assertTrue(all(row["catalog_target"] is None for row in new_target["items"]))
        self.assertTrue(all(row["catalog_target"] is not None for row in original["items"]))
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        count, physical = RestockWaitingListFixture.dispatch(self)
        self.assertEqual((count, len(physical)), (1, 1))
        update["task"].refresh_from_db()
        case["subscription"].refresh_from_db()
        case["invitation"].refresh_from_db()
        self.assertEqual(update["task"].status, "pending")
        self.assertEqual(case["subscription"].state, "sent")
        self.assertEqual(case["invitation"].target, original)

    def test_corrupted_typed_wait_task_evidence_keeps_all_ready_notification_fail_closed(self):
        from management.services import ig_assisted_manager_update as assisted_update
        case = self.explicit_accepted_update()
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        task = case["task"]
        original_event = deepcopy(task.event_payload)
        task.manager_context = deepcopy(task.manager_context)
        capture = task.manager_context[assisted_update.CONTEXT_KEY]
        signature = capture["hmac"]
        capture["hmac"] = format(int(signature[0], 16) ^ 1, "x") + signature[1:]
        task.save(update_fields=["manager_context", "updated_at"])
        task.refresh_from_db()
        self.assertEqual(task.event_payload, original_event)
        self.assertFalse(assisted_update.validated_update_history(task)["ready"])
        self.assertEqual(RestockWaitingListFixture.dispatch(self), (0, []))
        self.assertEqual(self.dispatch_update(now=self.now + timedelta(seconds=4)), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")
        event = restock.AvailabilityEvent.objects.get(subscription=case["subscription"])
        self.assertNotEqual(event.task.status, "sent")
        self.assertFalse(event.task.provider_message_id)

    def test_old_mixed_agreement_cannot_suppress_new_wait_after_same_pk_catalog_configuration_edit(self):
        from management.services import ig_assisted_manager_update as assisted_update
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_restock_waiting_list_review import configuration_reason
        case = self.accept_update(mixed=True)
        frozen = deepcopy(case["invitation"].target)
        old_variant_pk = self.variant.pk
        self.edit_native_variant_color()
        self.assertEqual(self.variant.pk, old_variant_pk)
        later = self.now + timedelta(seconds=2)
        source = self.canonical("измените позицию 2, цвет pink", at=later)
        revision, _token = self.revision(source)
        capture = capture_current_selection_lines(self.client_row.pk, now=later)
        self.assertEqual(self.cart_quantities(capture), [1, 1], capture)
        self.assertEqual(capture["lines"][1]["fields"]["color"]["value"], "pink", capture)
        request, _created = commerce.create_assisted_request(revision.pk, settings_id=self.settings.pk,
            operation_id=uuid.uuid4(), expected_revision_digest=revision.snapshot_digest,
            expected_full_capture_digest=capture["capture_digest"], now=later)
        items = commerce._editable_items(request.envelope["snapshot"])
        for row in items:
            row.update(availability="wait", checked_quantity=None, supply_basis="incoming_confirmed",
                source_location="Менеджер повторно перевірив змінену конфігурацію")
            if row["item_kind"] == "custom_apparel":
                row.update(title="Худі", unit_price="1500.00")
            row["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Параметри підтверджено; готовність ще не підтверджена."}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == row["line_id"]]
        draft, _created = self.record(request, items=items, now=later)
        decision, _created = self.record(request, status="approved", latest=draft, now=later)
        task = IgFollowUpTask.objects.get(event_key=f"ig-assisted-manager-update:{request.pk}:{decision.version}")
        self.assertTrue(assisted_update.validated_update_task(task, self.settings, now=later)["ready"])
        self.assertTrue(restock._valid_receipt(case["invitation"], case["receipt"]))
        self.assertTrue(configuration_reason(case["invitation"], now=later))
        count, physical = self.dispatch_update(now=later)
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertEqual(len(physical[0]["message"]["quick_replies"]), 1)
        task.refresh_from_db()
        case["invitation"].refresh_from_db()
        case["subscription"].refresh_from_db()
        self.assertEqual(task.status, "sent")
        self.assertEqual(case["invitation"].target, frozen)
        self.assertEqual(case["subscription"].state, "active")
        self.assertTrue(restock.Receipt.objects.filter(followup_task=task).exists())

    def test_typed_native_agreement_reaches_actual_read_model_and_shared_gemini_context(self):
        from management.services.ig_admin_state_capture import current_admin_state
        from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
        from management.services.ig_restock_waiting_list_context import SUMMARY, DETAILS, capture_waiting_list_context
        case = self.accept_update(mixed=True)
        answer = case["subscription"].accepted_answer.source
        now = self.now + timedelta(seconds=3)
        self.assertLessEqual(answer.provider_created_at, now)
        self.assertLessEqual(case["subscription"].accepted_answer.answered_at, now)
        boundary = {"client_id": self.client_row.pk, "episode_id": self.client_row.current_commercial_episode_id,
            "order_id": None, "line_id": "new-cart", "recipient_id": "self", "reset_floor": 1,
            "source_namespace": answer.provider_namespace, "reset_id": None, "erasure_epoch": "",
            "source_watermark": {"message_id": answer.pk, "event_at": now.isoformat()}}
        with patch("django.utils.timezone.now", return_value=now), \
                patch("management.services.instagram_bot._provider_http") as provider, \
                patch("management.services.call_ai_analysis.gemini_generate_text") as gemini:
            projected = waiting.waiting_list_projection(self.client_row.pk, now=now)
            slots = capture_waiting_list_context(self.client_row.pk, boundary=boundary, captured_at=now)
            state = assemble_client_state(boundary=boundary, components={"slots": slots}, captured_at=now)
            prompt = render_client_state_prompt(state, budget=6000)
            admin = current_admin_state(self.client_row.pk, now=now)
        provider.assert_not_called()
        gemini.assert_not_called()
        self.assertEqual(len(projected["lists"]), 1, projected)
        agreement = projected["lists"][0]
        self.assertEqual(agreement["agreement"]["state"], "accepted")
        self.assertTrue(agreement["agreement"]["invitation_receipt_confirmed"])
        self.assertEqual({row["item_kind"] for row in agreement["items"]}, {"catalog_apparel", "custom_apparel"})
        self.assertTrue(slots[SUMMARY]["value"].get("lists"), slots)
        self.assertEqual(slots[SUMMARY]["value"]["lists"][0]["state"], "accepted", slots)
        self.assertIn(DETAILS, slots)
        self.assertIn("accepted", prompt.text)
        for item in case["invitation"].target["items"]:
            self.assertIn(item["title"], prompt.text)
        self.assertEqual(admin.status, "captured", admin.as_dict())
        self.assertTrue(admin.state.as_dict()["slots"][SUMMARY]["value"].get("lists"), admin.as_dict())
        self.assertEqual(admin.state.as_dict()["slots"][SUMMARY]["value"]["lists"][0]["state"], "accepted")
        actual_prompt = render_client_state_prompt(admin.state, budget=2400)
        self.assertFalse(actual_prompt.oversized, actual_prompt.as_dict())
        self.assertIn(SUMMARY, actual_prompt.included)
        self.assertIn('"state":"accepted"', actual_prompt.text)
        for secret in ("RS1:", "envelope_hmac", "provider_message_id"):
            self.assertNotIn(secret, json.dumps(projected))

    def test_fresh_context_revalidates_changed_original_source_without_reusing_earlier_read_case(self):
        from management.services.ig_restock_waiting_list_context import SUMMARY, capture_waiting_list_context
        case = self.accept_update(mixed=True)
        answer = case["subscription"].accepted_answer.source
        first_at = self.now + timedelta(seconds=3)
        with patch("django.utils.timezone.now", return_value=first_at):
            first = capture_waiting_list_context(self.client_row.pk,
                boundary=self.context_boundary(answer, first_at), captured_at=first_at)
        self.assertTrue(first[SUMMARY]["value"].get("lists"), first)
        self.assertEqual(first[SUMMARY]["value"]["lists"][0]["state"], "accepted", first)
        source = case["request"].source
        source.text = "Змінені початкові параметри після першого читання"
        source.save(update_fields=["text"])
        second_at = first_at + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=second_at), \
                patch("management.services.instagram_bot._provider_http") as provider:
            second = capture_waiting_list_context(self.client_row.pk,
                boundary=self.context_boundary(answer, second_at), captured_at=second_at)
        provider.assert_not_called()
        self.assertFalse(any(row.get("state") == "accepted" for row in second[SUMMARY]["value"].get("lists", [])), second)
        self.assertEqual(first[SUMMARY]["value"]["lists"][0]["state"], "accepted", first)

    def test_native_historical_read_case_rejects_wrong_task_binding_and_closed_reuse(self):
        from management.services import ig_assisted_manager_update as assisted_update
        case = self.sent_update(mixed=True)
        task = case["task"]
        task.refresh_from_db()
        unit = assisted_update.capture_read_case(task)
        try:
            request, decision, projection = unit.resolve(task)
            self.assertEqual(request.pk, case["request"].pk)
            self.assertEqual(decision.pk, case["decision"].pk)
            self.assertTrue(projection["ready"])
            self.assertEqual(unit.source_snapshot(request).pk, request.source_id)
            wrong = deepcopy(task)
            wrong.event_key += ":wrong-owner"
            with self.assertRaises(ValueError):
                unit.resolve(wrong)
            self.assertFalse(assisted_update.validated_update_history(wrong, read_case=unit)["ready"])
            self.assertTrue(assisted_update.validated_update_history(task, read_case=unit)["ready"])
        finally:
            unit.close()
        with self.assertRaises(ValueError):
            unit.resolve(task)
        with self.assertRaises(ValueError):
            unit.source_snapshot(case["request"])
        self.assertFalse(assisted_update.validated_update_history(task, read_case=unit)["ready"])

    def assert_cancelled_context_budget(self, case):
        from django.test.utils import CaptureQueriesContext
        from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
        from management.services.ig_restock_waiting_list_context import SUMMARY, capture_waiting_list_context
        now = self.now + timedelta(seconds=3)
        boundary = self.context_boundary(case["latest_source"], now)
        with patch("django.utils.timezone.now", return_value=now), \
                patch("management.services.instagram_bot._provider_http") as provider:
            projected = waiting.waiting_list_projection(self.client_row.pk, now=now, limit=1,
                include_manager_review=False, max_read_queries=24)
            with CaptureQueriesContext(connection) as queries:
                slots = capture_waiting_list_context(self.client_row.pk, boundary=boundary, captured_at=now)
        provider.assert_not_called()
        self.assertTrue(projected["coverage_complete"], projected)
        self.assertEqual(projected["lists"][0]["agreement"]["state"], "revoked", projected)
        self.assertLessEqual(projected["diagnostics"]["read_queries"], 24, projected)
        self.assertLessEqual(len(queries), 24)
        self.assertTrue(slots[SUMMARY]["value"].get("lists"), slots)
        self.assertEqual(slots[SUMMARY]["value"]["lists"][0]["state"], "revoked", slots)
        state = assemble_client_state(boundary=boundary, components={"slots": slots}, captured_at=now)
        prompt = render_client_state_prompt(state, budget=2400)
        self.assertFalse(prompt.oversized, prompt.as_dict())
        self.assertIn('"state":"revoked"', prompt.text)

    def test_actual_native_cancelled_agreement_reaches_shared_context_inside_24_reads(self):
        self.assert_cancelled_context_budget(self.native_accept_and_cancel("uk"))

    def test_actual_photographed_native_cancelled_agreement_reaches_shared_context_inside_24_reads(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(prefix="assisted-cancelled-list-private-") as private_root, \
                override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            case = self.native_accept_and_cancel("uk", photo=True)
            images = [row["image_id"] for row in case["decision"].envelope["snapshot"]["items"] if row["image_id"]]
            self.assertEqual(len(images), 1)
            self.assertEqual(commerce._models()[3].objects.filter(pk__in=images, request=case["request"]).count(), 1)
            self.assert_cancelled_context_budget(case)

    def test_real_owned_photos_in_all_wait_agreement_keep_shared_context_inside_24_reads(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from django.test.utils import CaptureQueriesContext
        from management.services.ig_restock_waiting_list_context import SUMMARY, capture_waiting_list_context
        with TemporaryDirectory(prefix="assisted-waiting-list-private-") as private_root, \
                override_settings(IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())):
            case = self.accept_update(photo=True)
            images = [row["image_id"] for row in case["decision"].envelope["snapshot"]["items"]]
            self.assertEqual(len(set(images)), 2)
            self.assertNotIn(None, images)
            self.assertEqual(commerce._models()[3].objects.filter(pk__in=images, request=case["request"]).count(), 2)
            answer = case["subscription"].accepted_answer.source
            now = self.now + timedelta(seconds=3)
            with patch("django.utils.timezone.now", return_value=now), \
                    patch("management.services.instagram_bot._provider_http") as provider:
                projected = waiting.waiting_list_projection(self.client_row.pk, now=now, limit=1,
                    include_manager_review=False, max_read_queries=24)
                with CaptureQueriesContext(connection) as queries:
                    slots = capture_waiting_list_context(self.client_row.pk,
                        boundary=self.context_boundary(answer, now), captured_at=now)
            provider.assert_not_called()
            self.assertTrue(projected["coverage_complete"], projected)
            self.assertEqual(projected["lists"][0]["agreement"]["state"], "accepted", projected)
            self.assertLessEqual(projected["diagnostics"]["read_queries"], 24, projected)
            self.assertLessEqual(len(queries), 24)
            self.assertTrue(slots[SUMMARY]["value"].get("lists"), slots)
            self.assertEqual(slots[SUMMARY]["value"]["lists"][0]["state"], "accepted", slots)
            self.assertFalse(Order.objects.exists())
            self.assertFalse(PaymentAttempt.objects.exists())

    def test_long_complete_custom_list_falls_back_without_truncated_consent(self):
        title = "Фізично очікувана особлива модель " * 5
        self.approved_update(title=title)
        count, physical = self.dispatch_update()
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertNotIn("quick_replies", physical[0]["message"])
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Receipt.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_punctuation_and_fit_survive_native_offer_and_actual_response_plan(self):
        from types import SimpleNamespace
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        from management.services.ig_restock_ack_copy import attributed_label, list_label
        from management.services.ig_response_cart_plan import scoped_claim_clauses
        from management.services.ig_response_plan import capture_response_plan
        title = "Aurora, limited; edition"
        case = self.sent_update(title=title, locale="en")
        body = case["physical"][0]["message"]["text"]
        self.assertIn(attributed_label("en", list_label(case["invitation"].target)), body)
        self.assertIn(title, body.replace("Your choice: ", ""))
        self.assertIn("classic", body)
        self.assertEqual(body.count("classic"), sum(item["fit"] == "classic" for item in case["invitation"].target["items"]))
        context = ReplyTruthContext(source_chosen_sizes=("M", "L"), source_chosen_fits=("classic",),
            source_chosen_colors=("purple", "olive"))
        accepted = restock._fresh_copy_for(case["invitation"], "accepted")
        for clause in scoped_claim_clauses(accepted):
            truth = validate_reply_truth(clause, context=context)
            self.assertTrue(truth.valid, (clause, truth.reasons))
        capture = capture_current_selection_lines(self.client_row.pk, now=self.now)
        plan = capture_response_plan(self.client_row, revision=case["request"].revision, source_cart_capture=capture)
        self.assertEqual(plan.validate_multiline_claims(SimpleNamespace(reply_text=accepted, control={}), context), "")
        self.assertFalse(validate_reply_truth(body + "\nThese items are available.", context=context).valid)
