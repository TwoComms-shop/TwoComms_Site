"""Real CART review -> same native manager update -> actual own RS1 consent."""
from copy import deepcopy
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.db import DatabaseError, connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.models import IgCustomerTurnRevision, IgFollowUpTask, IgRevisionDeliveryEffect, InstagramBotMessage
from management.services import ig_fulfillment_delivery as delivery
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_followup as followup
from management.services import ig_restock_subscriptions as restock
from management.services import ig_restock_waiting_list as waiting
from management.tests_ig_assisted_commerce import AssistedCommerceFixture


class FollowupAcceptedCopyTests(SimpleTestCase):
    def test_each_ack_line_is_a_source_choice_without_stock_authority_in_three_languages(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
        from management.services.ig_response_cart_plan import scoped_claim_clauses
        target = {"items": [{"title": "Aurora", "color": "black", "size": "M", "fit": "classic",
            "options": {"fit": "classic"}, "quantity": 1, "recipient_id": "self"}]}
        context = ReplyTruthContext(source_chosen_sizes=("M",), source_chosen_fits=("classic",),
            source_chosen_colors=("black",))
        for locale, false_stock in (("uk", "Ця позиція у наявності."),
            ("ru", "Эта позиция в наличии."), ("en", "These items are available.")):
            with self.subTest(locale=locale):
                body = followup.accepted_copy(locale, target)
                for clause in scoped_claim_clauses(body):
                    truth = validate_reply_truth(clause, context=context)
                    self.assertTrue(truth.valid, (locale, clause, truth.reasons))
                self.assertFalse(validate_reply_truth(body + "\n" + false_stock, context=context).valid)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    SITE_BASE_URL="https://twocomms.test")
class RestockFollowupTests(AssistedCommerceFixture, TransactionTestCase):
    reset_sequences = True

    def manager_wait(self, *, source_text="Встигнете відправити до 20.10.2026? Це подарунок", positive_stock=False, availability="wait"):
        if positive_stock:
            self.variant.stock = 8
            self.variant.save(update_fields=["stock"])
            self.rule.stock = 8
            self.rule.save(update_fields=["stock"])
        source = self.source(source_text)
        revision, _token = self.revision(source)
        prepared = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        request = reviews.Request.objects.get(pk=prepared["request_id"])
        result = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
            decisions_by_line=[{"line_id": row["line_id"], "availability": availability} for row in request.envelope["lines"]],
            now=self.now)
        self.assertTrue(result["ready"], result)
        task = IgFollowUpTask.objects.get(event_key=f"ig-fulfillment-update:{request.pk}:{result['version']}")
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(state="processed", active_slot=None, claim_token="", lease_until=None)
        return task, request, source

    def prepared_offer(self, task, *, text="Менеджер перевірив позиції."):
        projection = reviews.validated_update_task(task, self.settings, now=self.now)
        self.assertTrue(projection["ready"], projection)
        with transaction.atomic():
            result = followup.prepare_task_offer(task, projection, self.settings, text=text, now=self.now)
            if result["ready"]:
                delivery._freeze_task_update(task, projection, result["body"])
                task.save(update_fields=["manager_context", "updated_at"])
        return result, projection

    def dispatch(self, *, missing_mid=False, during_http=None):
        from management.services import instagram_bot as bot
        physical = []
        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if during_http:
                during_http()
            return 200, json.dumps({} if missing_mid else {"message_id": "native-manager-wait-card"})
        with patch.object(bot, "get_page_token", return_value="synthetic-followup-token"), \
                patch.object(bot, "_provider_http", side_effect=provider):
            sent = delivery.process_due_fulfillment_updates(self.settings, now=self.now)
        return sent, physical

    def test_real_manager_wait_native_task_receipt_and_own_click_creates_one_subscription(self):
        task, request, source = self.manager_wait(positive_stock=True)
        before_effects = IgRevisionDeliveryEffect.objects.count()
        sent, physical = self.dispatch()
        self.assertEqual(sent, 1)
        self.assertEqual(len(physical), 1)
        inv = restock.Invitation.objects.get(request_source=source)
        receipt = restock.Receipt.objects.get(invitation=inv)
        self.assertIsNone(receipt.effect_id)
        self.assertEqual(receipt.followup_task_id, task.pk)
        self.assertEqual(IgRevisionDeliveryEffect.objects.count(), before_effects)
        self.assertEqual(inv.envelope["manager_review"]["request_id"], str(request.pk))
        self.assertEqual(len(physical[0]["message"]["quick_replies"]), 1)
        self.assertEqual(physical[0], followup._plan(inv))
        self.assertFalse(restock.Subscription.objects.exists())
        response, answer, _revision, _token = self.reply(inv, reply_to=receipt.provider_message_id)
        self.assertEqual(response.reason, "accept", response)
        self.assertEqual(restock.Subscription.objects.filter(accepted_answer__source=answer).count(), 1)
        self.assertTrue(restock._valid_receipt(inv, receipt))
        projected = waiting.waiting_list_projection(self.client_row.pk, now=answer.provider_created_at)
        self.assertEqual(projected["lists"][0]["agreement"]["state"], "accepted", projected)
        sent_again, physical_again = self.dispatch()
        self.assertEqual((sent_again, physical_again), (0, []))

    def test_no_wait_sends_original_service_update_without_invitation(self):
        task, _request, _source = self.manager_wait(availability="unavailable")
        sent, physical = self.dispatch()
        self.assertEqual(sent, 1)
        self.assertNotIn("quick_replies", physical[0]["message"])
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_actual_revision_postback_reducer_accepts_task_receipt_without_gemini(self):
        from management.services import instagram_bot as bot
        from management.services.ig_revision_live import execute_claimed_revision
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        task, _request, source = self.manager_wait()
        self.assertEqual(self.dispatch()[0], 1)
        inv = restock.Invitation.objects.get(request_source=source)
        answer = self.source(inv.button_title, payload=inv.accept_payload, at=self.now + timedelta(seconds=1))
        answer.reply_to_provider_message_id = "native-manager-wait-card"
        answer.save(update_fields=["reply_to_provider_message_id"])
        revision, token = self.revision(answer)
        physical = []
        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            return 200, json.dumps({"message_id": "native-manager-wait-acceptance"})
        with patch("django.utils.timezone.now", return_value=answer.provider_created_at), \
                patch.object(bot, "get_page_token", return_value="synthetic-postback-token"), \
                patch.object(bot, "_provider_http", side_effect=provider), \
                patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("RS1 is deterministic")):
            executed = execute_claimed_revision(revision.pk, token, self.settings)
        self.assertEqual(executed.state, "completed", executed.reasons)
        self.assertEqual(restock.Subscription.objects.filter(accepted_answer__source=answer).count(), 1)
        self.assertEqual(len(physical), 1)
        receipt = restock.Receipt.objects.get(invitation=inv)
        self.assertEqual(receipt.followup_task_id, task.pk)
        self.assertIsNone(receipt.effect_id)

    def test_complete_two_wait_lines_preserve_quantities_in_one_immutable_offer(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        product, variant = self.products[1], self.variants[1]
        ProductInventoryPolicy.objects.get_or_create(product=product, defaults={"source": "catalog_variant"})
        VariantSizeRule.objects.get_or_create(variant=variant, fit_code="classic", size="L", defaults={"stock": 0, "is_enabled": True})
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        task, _request, _source = self.manager_wait()
        result, _projection = self.prepared_offer(task, text="Checked")
        self.assertTrue(result["ready"], result)
        inv = restock.Invitation.objects.get(pk=result["invitation_id"])
        self.assertEqual(len(inv.target["items"]), 2)
        self.assertEqual(sorted(item["quantity"] for item in inv.target["items"]), [1, 2])
        self.assertEqual(len(result["quick_replies"]), 1)
        self.assertLessEqual(len(result["body"].encode()), 950)

    def test_oversized_offer_does_not_leave_half_invitation_or_clip_list(self):
        task, _request, _source = self.manager_wait()
        result, _projection = self.prepared_offer(task, text="я" * 460)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "waiting_list_single_card_bound")
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertNotIn(followup.TASK_KEY, task.manager_context or {})

    def test_bare_yes_and_click_before_native_task_receipt_cannot_subscribe(self):
        task, _request, _source = self.manager_wait()
        result, _projection = self.prepared_offer(task)
        self.assertTrue(result["ready"], result)
        inv = restock.Invitation.objects.get(pk=result["invitation_id"])
        response, _source, _revision, _token = self.reply(inv)
        self.assertEqual(response.reason, "answer_not_current")
        self.assertFalse(restock.Subscription.objects.exists())
        bare = self.source("Так", at=self.now + timedelta(seconds=2))
        self.assertFalse(restock.handle_reply(bare, settings_obj=self.settings).handled)

    def test_missing_native_mid_is_unknown_and_never_replayed(self):
        task, _request, _source = self.manager_wait()
        sent, physical = self.dispatch(missing_mid=True)
        task.refresh_from_db()
        self.assertEqual((sent, len(physical), task.status), (0, 1, "ambiguous"))
        self.assertFalse(restock.Receipt.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertEqual(self.dispatch(), (0, []))

    def test_current_guard_rejects_manager_revocation_and_changed_whole_cart(self):
        task, _request, _source = self.manager_wait()
        result, projection = self.prepared_offer(task)
        self.assertTrue(result["ready"], result)
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])
        current = followup.task_offer(task, projection, self.settings, text=result["body"], now=self.now)
        self.assertEqual(current["reason"], "manager_actor_forbidden")
        self.manager.is_active = True
        self.manager.save(update_fields=["is_active"])
        self.canonical("количество 2", at=self.now + timedelta(seconds=1))
        current = followup.task_offer(task, projection, self.settings, text=result["body"], now=self.now + timedelta(seconds=1))
        self.assertFalse(current["ready"])
        self.assertFalse(restock.Subscription.objects.exists())

    def test_historical_receipt_recovers_after_checkpoint_without_second_native_send(self):
        task, _request, _source = self.manager_wait()
        result, _projection = self.prepared_offer(task)
        self.assertTrue(result["ready"], result)
        task.status, task.attempt_count = "sent", 1
        task.provider_message_id, task.sent_at = "native-checkpoint-before-crash", self.now
        task.message_text = result["body"]
        task.save(update_fields=["status", "attempt_count", "provider_message_id", "sent_at", "message_text", "updated_at"])
        self.assertTrue(followup.record_task_delivery(task.pk, now=self.now + timedelta(days=1)))
        receipt = restock.Receipt.objects.get(followup_task=task)
        self.assertTrue(restock._valid_receipt(receipt.invitation, receipt))
        self.assertEqual(self.dispatch(), (0, []))

    def test_payload_change_after_checkpoint_cannot_seal_task_receipt(self):
        task, _request, _source = self.manager_wait()
        result, _projection = self.prepared_offer(task)
        self.assertTrue(result["ready"], result)
        task.status, task.attempt_count = "sent", 1
        task.provider_message_id, task.sent_at = "native-mutated-checkpoint", self.now
        task.message_text = result["body"] + " changed"
        task.save(update_fields=["status", "attempt_count", "provider_message_id", "sent_at", "message_text", "updated_at"])
        self.assertFalse(followup.record_task_delivery(task.pk, now=self.now))
        self.assertFalse(restock.Receipt.objects.exists())

    def test_three_languages_freeze_single_positive_button_and_existing_offer_reuses_identity(self):
        for locale, question in (("uk", "Встигнете відправити до 20.10.2026? Це подарунок"),
            ("ru", "Успеете отправить до 20.10.2026? Это подарок"),
            ("en", "Can you dispatch before 20.10.2026? This is a gift")):
            with self.subTest(locale=locale):
                task, _request, _source = self.manager_wait(source_text=question)
                result, projection = self.prepared_offer(task, text="Manager update")
                self.assertTrue(result["ready"], result)
                inv = restock.Invitation.objects.get(pk=result["invitation_id"])
                self.assertEqual(inv.locale, locale)
                self.assertEqual(len(result["quick_replies"]), 1)
                self.assertEqual(result["quick_replies"][0].payload, inv.accept_payload)
                repeated = followup.prepare_task_offer(task, projection, self.settings, text=result["body"], now=self.now)
                self.assertEqual(repeated, result)
                # Separate genuine basket generation makes the next language
                # a new manager case without rewriting any evidence.
                self.canonical("количество " + str(2 + len(restock.Invitation.objects.all())), at=self.now)

    def _raw_receipt_clone_rejected(self, receipt, **changes):
        fields = [field.column for field in restock.Receipt._meta.concrete_fields if not field.primary_key]
        quote = connection.ops.quote_name
        table = quote(restock.Receipt._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {','.join(quote(field) for field in fields)} FROM {table} WHERE id=%s", [receipt.pk])
            values = dict(zip(fields, cursor.fetchone()))
        values.update(changes)
        # The BEFORE INSERT producer guard must reject the invalid provenance
        # before this deliberate clone can reach its one-to-one uniqueness.
        with self.assertRaisesMessage(DatabaseError, "Restock native receipt source guard"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"INSERT INTO {table} ({','.join(quote(field) for field in fields)}) VALUES ({','.join(['%s'] * len(fields))})",
                    [values[field] for field in fields])

    def test_database_xor_neither_and_both_native_origins_are_rejected(self):
        task, _request, source = self.manager_wait()
        self.assertEqual(self.dispatch()[0], 1)
        receipt = restock.Receipt.objects.get(invitation__request_source=source)
        self._raw_receipt_clone_rejected(receipt, effect_id=None, followup_task_id=None)
        # No revision effect is created: an invalid raw reference must never
        # make two simultaneously populated origin columns acceptable.
        self._raw_receipt_clone_rejected(receipt, effect_id=task.pk, followup_task_id=task.pk)

    def test_database_rejects_changed_native_task_state_mid_body_pointer_and_time(self):
        task, _request, source = self.manager_wait()
        self.assertEqual(self.dispatch()[0], 1)
        receipt = restock.Receipt.objects.get(invitation__request_source=source)
        task.refresh_from_db()
        pointer = deepcopy(task.manager_context)
        pointer[followup.TASK_KEY]["invitation_hmac"] = "f" * 64
        cases = ({"status": "pending"}, {"status": "ambiguous"}, {"provider_message_id": "different-native-mid"},
            {"message_text": task.message_text + " changed"}, {"manager_context": pointer},
            {"sent_at": task.sent_at + timedelta(seconds=1)}, {"attempt_count": 0})
        for changes in cases:
            with self.subTest(changes=changes), transaction.atomic():
                IgFollowUpTask.objects.filter(pk=task.pk).update(**changes)
                self._raw_receipt_clone_rejected(receipt)
                transaction.set_rollback(True)
        self._raw_receipt_clone_rejected(receipt, delivered_at=receipt.delivered_at + timedelta(seconds=1))

    def test_database_denies_rewrite_of_valid_task_receipt(self):
        _task, _request, source = self.manager_wait()
        self.assertEqual(self.dispatch()[0], 1)
        receipt = restock.Receipt.objects.get(invitation__request_source=source)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE management_igrestockreceipt SET provider_message_id=%s WHERE id=%s",
                    ["rewritten-native-mid", receipt.pk])
        receipt.refresh_from_db()
        self.assertEqual(receipt.provider_message_id, "native-manager-wait-card")

    def test_manager_revocation_between_permission_capture_and_http_blocks_physical_send(self):
        from management.services import ig_reply_boundary
        task, _request, _source = self.manager_wait()
        capture = ig_reply_boundary.capture_reply_permission
        def revoke_after_capture(*args, **kwargs):
            permission = capture(*args, **kwargs)
            self.manager.is_active = False
            self.manager.save(update_fields=["is_active"])
            return permission
        with patch.object(ig_reply_boundary, "capture_reply_permission", side_effect=revoke_after_capture):
            sent, physical = self.dispatch()
        self.assertEqual((sent, physical), (0, []))
        task.refresh_from_db()
        self.assertNotEqual(task.status, "sent")
        self.assertFalse(restock.Receipt.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_already_frozen_plain_wait_update_keeps_its_body_without_new_offer(self):
        task, _request, _source = self.manager_wait()
        projection = reviews.validated_update_task(task, self.settings, now=self.now)
        plain = delivery.render_manager_update(projection, copy_version=2)
        delivery._freeze_task_update(task, projection, plain)
        task.save(update_fields=["manager_context", "updated_at"])
        sent, physical = self.dispatch()
        self.assertEqual(sent, 1)
        self.assertEqual(physical[0]["message"], {"text": plain})
        self.assertFalse(restock.Invitation.objects.exists())
