"""Native manager review source/decision and closed estimate authority contracts."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest as Request, IgFulfillmentReviewDecision as Decision
from management.models import IgFollowUpTask, IgBotNotification, AdminAuditLog
from management.services import ig_fulfillment_reviews as review
from management.tests_ig_restock_subscriptions import _RestockFixture


class FulfillmentDecisionSchemaTests(SimpleTestCase):
    def setUp(self):
        from django.utils import timezone
        self.now = timezone.now()
        self.target = {"product_id": 1, "color_variant_id": 2, "size": "M", "fit_option_code": "classic",
            "option_values": {"fit": "classic"}, "quantity": 2}
        self.request = SimpleNamespace(envelope={"lines": [{"line_id": "a", "target": self.target}]})
        observed = {"version": 1, "status": "unavailable", "target": self.target, "evidence": {"physical_quantity": 0}, "reason": "insufficient_free_stock"}
        observer = patch("management.services.ig_restock_availability.observe_target", return_value=SimpleNamespace(as_dict=lambda: deepcopy(observed)))
        observer.start()
        self.addCleanup(observer.stop)
        config = patch.object(review, "_configuration", return_value={"status": "valid", "target": self.target, "configuration_digest": "c" * 64})
        config.start()
        self.addCleanup(config.stop)

    def test_checked_accessible_supply_overrides_accounting_zero_without_minting_it(self):
        rows, split = review._decisions([{"line_id": "a", "availability": "available", "checked_quantity": 2,
            "supply_basis": "accessible_unrecorded", "source_location": "Нова пошта"}], self.request, self.now)
        self.assertEqual(rows[0]["availability"], "available")
        self.assertEqual(rows[0]["configuration_digest"], "c" * 64)
        self.assertIsNone(split)

    def test_twenty_position_manager_alert_keeps_all_rows_and_instruction_within_sender_limit(self):
        request = SimpleNamespace(pk=uuid.uuid4(), client_id=1, client=object(), envelope_hmac="a" * 64,
            envelope={"lines": [{"label": "Назва товару " * 20, "observation": {"status": "unavailable"}}
                for _ in range(20)], "customer_requirements": [{"raw_quote": "Важливий строк " * 50}]})
        with patch.object(review, "build_fulfillment_callback", return_value="render-test"), \
                patch("management.services.instagram_bot.notify_manager") as notify:
            review._queue_manager(request)
        text = notify.call_args.args[0]
        self.assertLessEqual(len(text), 3500)
        self.assertTrue(all(f"\n{index}. " in text for index in range(1, 21)))
        self.assertIn("Усі позиції та побажання — у картці", text)
        self.assertIn("Прибуття перевізника не гарантується", text)
        self.assertFalse(notify.call_args.kwargs["deliver_immediately"])

    def test_incoming_supply_and_zero_or_bool_count_never_become_ready(self):
        for changes in ({"supply_basis": "incoming_confirmed"}, {"checked_quantity": 0}, {"checked_quantity": True}, {"checked_quantity": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review._decisions([{ "line_id": "a", "availability": "available", "checked_quantity": 2,
                    "supply_basis": "warehouse_checked", **changes}], self.request, self.now)
        rows, _ = review._decisions([{"line_id": "a", "availability": "wait", "supply_basis": "incoming_confirmed",
            "dispatch": {"min_days": 1, "max_days": 3, "unit": "business"}}], self.request, self.now)
        self.assertEqual(rows[0]["availability"], "wait")
        self.assertEqual(rows[0]["dispatch"]["anchor_at"], self.now.isoformat())

    def test_deleted_configuration_and_arrival_field_are_rejected(self):
        with patch.object(review, "_configuration", return_value={"status": "invalid", "target": self.target}), self.assertRaises(ValueError):
            review._decisions([{"line_id": "a", "availability": "available", "checked_quantity": 2,
                "supply_basis": "accessible_unrecorded"}], self.request, self.now)
        with self.assertRaises(ValueError):
            review._decisions([{"line_id": "a", "availability": "wait", "arrival_guaranteed": True}], self.request, self.now)

    def test_estimate_has_explicit_unit_and_manager_clock_not_carrier_arrival(self):
        for timing in ({"min_days": True, "max_days": 3, "unit": "calendar"},
                {"min_days": 3, "max_days": 1, "unit": "calendar"},
                {"min_days": 1, "max_days": 3, "unit": "hours"},
                {"min_days": 1, "max_days": 3, "unit": "calendar", "arrival": True}):
            with self.subTest(timing=timing), self.assertRaises(ValueError):
                review._timing(timing, self.now)

    def test_split_requires_all_lines_exactly_once_and_explicit_payer_cost(self):
        self.request.envelope["lines"].append({"line_id": "b", "target": {**self.target, "quantity": 1}})
        rows = [{"line_id": value, "availability": "wait"} for value in ("a", "b")]
        split = {"groups": [["a"], ["b"]], "extra_delivery_amount": "70.00", "payer": "customer", "feasible": True}
        result = review._decisions({"lines": rows, "split_offer": split}, self.request, self.now)
        self.assertEqual(result[1], split)
        for changes in ({"groups": [["a"], ["a"]]}, {"payer": "unknown"}, {"extra_delivery_amount": "NaN"}, {"feasible": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                review._decisions({"lines": rows, "split_offer": {**split, **changes}}, self.request, self.now)

    def test_catalog_readback_is_threaded_only_with_a_cart_and_send_default_is_fresh(self):
        request = SimpleNamespace(envelope_hmac="request-proof", envelope={"lines": [{"line_id": "a", "target": self.target}]})
        decision = SimpleNamespace(created_at=self.now, envelope={"request_digest": "request-proof", "lines": [
            {"line_id": "a", "availability": "available", "configuration_digest": "c" * 64}]})
        observed = [{"status": "valid", "target": self.target, "configuration_digest": "c" * 64}]
        cart, readback = {"capture_digest": "cart"}, {"prepared": True}
        with patch.object(review, "_verified", return_value=True), \
                patch("management.services.ig_restock_availability.observe_configurations", return_value=observed) as observe:
            self.assertEqual(review._decision_reason(request, decision, now=self.now), "")
            observe.assert_called_once_with([self.target], now=self.now)
            observe.reset_mock()
            self.assertEqual(review._decision_reason(request, decision, now=self.now, _catalog_readback=readback), "")
            observe.assert_called_once_with([self.target], now=self.now)
            observe.reset_mock()
            self.assertEqual(review._decision_reason(request, decision, now=self.now,
                _catalog_readback=readback, current_source_cart=cart), "")
            observe.assert_called_once_with([self.target], now=self.now, _catalog_readback=readback, source_cart=cart)


class FulfillmentContextRequirementTests(SimpleTestCase):
    def hints(self, texts):
        from management.services.ig_fulfillment_requirements import extract_requirements
        return [extract_requirements(text) for text in texts]

    def test_gift_only_and_recipient_only_sources_have_context_without_eta(self):
        for text in ("Це подарунок", "Это подарок", "This is a gift"):
            with self.subTest(text=text):
                hints = self.hints([text])
                self.assertFalse(hints[0]["request_detected"])
                self.assertEqual(review._context_requirements(hints), {"gift": True})
        for text in ("Для мами", "Для мамы", "For my mother"):
            with self.subTest(text=text):
                self.assertEqual(review._context_requirements(self.hints([text])), {"recipient_hint": "mother"})

    def test_explicit_delivery_preference_is_captured_without_date_or_stock_query(self):
        for text, expected in (("Усе разом однією посилкою", "together"),
                ("Всё вместе одной посылкой", "together"), ("All together in one parcel", "together"),
                ("Окремими посилками", "split"), ("Отдельными посылками", "split"), ("Separate shipments", "split")):
            with self.subTest(text=text):
                hints = self.hints([text])
                self.assertFalse(hints[0]["request_detected"])
                self.assertEqual(review._context_requirements(hints), {"delivery_preference": expected})

    def test_neutral_source_never_creates_context_requirement(self):
        self.assertEqual(review._context_requirements(self.hints(["Дякую", "Спасибо", "Thank you"])), {})

    def test_latest_explicit_changes_replace_only_the_changed_context_field(self):
        hints = self.hints(["Це подарунок для мами", "Усе разом", "Не подарунок", "Окремими посилками"])
        self.assertEqual(review._context_requirements(hints), {
            "gift": False, "recipient_hint": "mother", "delivery_preference": "split"})

    def test_same_requirement_in_different_language_has_same_business_context(self):
        first = review._context_requirements(self.hints(["Це подарунок для мами", "Усе разом"]))
        repeated = review._context_requirements(self.hints(["This is a gift for my mother", "All together"]))
        self.assertEqual(first, repeated)


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeFulfillmentReviewTests(_RestockFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(username="fulfillment-manager", email="manager@example.test", password="test")

    def case(self, text="Встигнете відправити до 20.10.2026? Це подарунок"):
        source = self.source(text)
        revision, _ = self.revision(source)
        with patch("management.services.instagram_bot._provider_http") as http:
            result = review.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        http.assert_not_called()
        self.assertTrue(result["ready"], result)
        return Request.objects.get(pk=result["request_id"]), revision

    def decide(self, request, *, operation_id=None, **line_changes):
        rows = [{"line_id": line["line_id"], "availability": "available", "checked_quantity": 1,
            "supply_basis": "accessible_unrecorded", **line_changes} for line in request.envelope["lines"]]
        return review.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=operation_id or uuid.uuid4(),
            decisions_by_line=rows, now=self.now)

    def test_native_source_case_is_immutable_manager_task_and_idempotent_outbox(self):
        request, revision = self.case()
        again = review.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now + timedelta(seconds=1))
        self.assertFalse(again["created"])
        self.assertEqual(str(request.pk), again["request_id"])
        self.assertEqual(request.task.kind, "manager_task")
        self.assertEqual(request.task.status, "skipped")
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)
        self.assertTrue(request.envelope["customer_requirements"][0]["gift"])
        with self.assertRaises(ValueError):
            request.save()
        with self.assertRaises(ValueError):
            Request.objects.filter(pk=request.pk).update(envelope={})

    def _ordinary_inquiry(self):
        source = self.source("Ця позиція є в наявності?")
        revision, _ = self.revision(source)
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = review.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        provider.assert_not_called()
        return result

    def test_positive_exact_catalog_inquiry_requires_manager_review(self):
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="fulfillment_review").count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)

    def test_positive_exact_warehouse_inquiry_requires_manager_review(self):
        from management.tests_ig_fulfillment_stock_sync import NativeFulfillmentStockSyncTests
        stock = NativeFulfillmentStockSyncTests.warehouse(self)
        stock.quantity = 5
        stock.save(update_fields=["quantity"])
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)

    def test_zero_catalog_inquiry_still_creates_source_bound_review(self):
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        self.assertTrue(review._verified(request, "request"))
        self.assertEqual(request.envelope["lines"][0]["observation"]["status"], "unavailable")

    def test_zero_warehouse_inquiry_still_requires_manager_review(self):
        from management.tests_ig_fulfillment_stock_sync import NativeFulfillmentStockSyncTests
        NativeFulfillmentStockSyncTests.warehouse(self)
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        self.assertEqual(request.envelope["lines"][0]["observation"]["status"], "unavailable")

    def test_missing_warehouse_mapping_inquiry_still_requires_manager_review(self):
        from product_catalog.models import ProductInventoryPolicy
        ProductInventoryPolicy.objects.filter(product=self.product).update(source="warehouse")
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        self.assertEqual(request.envelope["lines"][0]["observation"]["status"], "unknown")

    def test_null_exact_size_inquiry_still_requires_manager_review(self):
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.stock = None
        self.rule.save(update_fields=["stock"])
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        self.assertEqual(request.envelope["lines"][0]["observation"]["status"], "unknown")

    def test_unsupported_inventory_inquiry_still_requires_manager_review(self):
        from product_catalog.models import ProductInventoryPolicy
        ProductInventoryPolicy.objects.filter(product=self.product).update(source="untracked")
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        self.assertEqual(request.envelope["lines"][0]["observation"]["status"], "unknown")

    def test_positive_inquiry_preserves_current_source_bound_deadline(self):
        first, _ = self.case("Встигнете відправити до 20.10.2026?")
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        result = self._ordinary_inquiry()
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        self.assertTrue(any(hint["deadline_date"] == "2026-10-20"
            for hint in request.envelope["customer_requirements"]))
        self.assertTrue(any(proof["message_id"] == first.source_id for proof in request.envelope["sources"]))

    def test_latest_own_english_source_ignores_quoted_ukrainian_for_review_locale(self):
        request, _ = self.case('Can you dispatch this by 20.10.2026? "Це подарунок для мами"')
        self.assertEqual(request.envelope["locale"], "en")
        self.assertEqual(request.envelope["reply_language"]["source_message_id"], request.source_id)

    def test_current_explicit_english_reply_request_wins_over_ukrainian_profile(self):
        request, _ = self.case("Встигнете відправити до 20.10.2026? Reply in English please")
        self.assertEqual(request.envelope["locale"], "en")
        self.assertEqual(request.envelope["reply_language"]["basis"], "current_source_requested")

    def test_real_batched_review_uses_latest_own_source_language(self):
        from management.models import IgCustomerTurn, IgTurnMessage
        from management.services.ig_turn_revisions import (create_collecting_revision,
            claim_revision_preparation, seal_revision, claim_sealed_revision)
        rows = [self.source("Потрібен подарунок до 20.10.2026"),
            self.source("Can you tell me if this is available?")]
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=rows[0],
            window_started_at=self.now, window_deadline=self.now)
        for ordinal, row in enumerate(rows, 1):
            IgTurnMessage.objects.create(turn=turn, message=row, ordinal=ordinal, role="user")
        collected = create_collecting_revision(turn, rows, now=self.now, bypass_quiet=True)
        self.assertIsNotNone(collected.revision, collected.reason)
        prepared = claim_revision_preparation(collected.revision.pk, now=self.now)
        sealed = seal_revision(collected.revision.pk, prepared.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(sealed.revision.pk, now=self.now)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = review.prepare_fulfillment_review(claimed.revision, settings_obj=self.settings, now=self.now)
        provider.assert_not_called()
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        self.assertEqual(request.envelope["locale"], "en")
        self.assertEqual(request.envelope["reply_language"]["source_message_id"], rows[-1].pk)

    def test_display_label_preserves_signed_historical_quantity_label(self):
        request, _ = self.case()
        line = request.envelope["lines"][0]
        self.assertIn("× 1", line["label"])
        self.assertNotIn("× 1", line["display_label"])
        self.assertEqual(line["display_label"], review._display_label(line["observation"], 0))

    def test_one_final_decision_idempotent_operation_and_only_one_customer_effect(self):
        request, _ = self.case()
        operation = uuid.uuid4()
        result = self.decide(request, operation_id=operation)
        self.assertTrue(result["ready"], result)
        self.assertFalse(self.decide(request, operation_id=operation)["created"])
        self.assertEqual(self.decide(request)["reason"], "decision_already_recorded")
        self.assertEqual(Decision.objects.filter(request=request).count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(reason=review.UPDATE_REASON).count(), 1)
        self.assertEqual(AdminAuditLog.objects.filter(action="ig_fulfillment_decision").count(), 1)
        task = IgFollowUpTask.objects.get(reason=review.UPDATE_REASON)
        self.assertTrue(review.validated_update_task(task, self.settings, now=self.now)["ready"])
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.stock, 0)

    def test_bad_digest_and_actor_or_unsent_telegram_cannot_decide(self):
        request, _ = self.case()
        other = get_user_model().objects.create_user(username="no-fulfillment-capability")
        args = {"expected_request_digest": request.envelope_hmac, "operation_id": uuid.uuid4(),
            "decisions_by_line": [{"line_id": line["line_id"], "availability": "unavailable"} for line in request.envelope["lines"]], "now": self.now}
        self.assertEqual(review.apply_fulfillment_decision(request.pk, actor=other, **args)["reason"], "actor_forbidden")
        self.assertEqual(review.apply_fulfillment_decision(request.pk, actor=self.manager,
            **{**args, "expected_request_digest": "0" * 64})["reason"], "request_digest_changed")
        self.assertEqual(review.apply_fulfillment_decision(request.pk, actor=self.manager,
            telegram_receipt={"chat_id": "1", "message_id": "2", "version": 0}, **args)["reason"], "telegram_receipt_unverified")
        self.assertFalse(Decision.objects.exists())

    def test_callback_is_short_signed_and_source_tamper_invalidates_decision(self):
        request, _ = self.case()
        value = review.build_fulfillment_callback(request, "13")
        self.assertLessEqual(len(value.encode()), 64)
        self.assertEqual(review.parse_fulfillment_callback(value)["request_id"], str(request.pk))
        self.assertIsNone(review.parse_fulfillment_callback(value[:-1] + ("0" if value[-1] != "0" else "1")))
        request.source.text = "інший дедлайн"
        request.source.save(update_fields=["text"])
        self.assertFalse(self.decide(request)["ready"])

    def test_standing_request_has_explicit_client_bound_staff_projection_no_second_update(self):
        _, subscription = self.accepted()
        from management.services.ig_restock_availability import observe_target
        observed = observe_target(subscription.accepted_answer.invitation.target, now=self.now).as_dict()
        # Exercise the separate legacy standing owner explicitly. Fresh original
        # CART reuse is covered causally in tests_ig_fulfillment_reuse.
        with patch.object(review, "_cart_restock_review", return_value={"eligible": False}):
            result = review.ensure_restock_manager_review(subscription, observed, now=self.now)
        self.assertTrue(result["created"], result)
        request = Request.objects.get(pk=result["request_id"])
        projection = review.fulfillment_review_projection(self.client_row.pk, request_id=request.pk, now=self.now)
        self.assertEqual(projection["status"], "pending")
        self.assertEqual(projection["current_request"]["scope"], "restock")
        self.assertEqual(review.fulfillment_review_projection(self.client_row.pk + 999, request_id=request.pk, now=self.now)["status"], "none")
        self.assertTrue(self.decide(request)["ready"])
        self.assertFalse(IgFollowUpTask.objects.filter(reason=review.UPDATE_REASON).exists())
        self.assertTrue(review.restock_manager_admission(subscription, observed, now=self.now)["ready"])

    def test_neutral_source_keeps_manager_case_without_another_telegram_alert(self):
        request, _ = self.case()
        source = self.source("Дякую")
        revision, _ = self.revision(source)
        result = review.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertFalse(result["created"], result)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)
        self.assertEqual(review.fulfillment_review_projection(self.client_row.pk, now=self.now)["status"], "pending")

    def test_available_exact_cart_captures_gift_recipient_and_logistics_without_date(self):
        self.rule.stock = self.variant.stock = 5
        self.rule.save(update_fields=["stock", "updated_at"])
        self.variant.save(update_fields=["stock"])
        request, _ = self.case("Це подарунок для мами. Усе разом однією посилкою")
        self.assertEqual(review._context_requirements(request.envelope["customer_requirements"]), {
            "gift": True, "recipient_hint": "mother", "delivery_preference": "together"})
        projection = review.fulfillment_review_projection(self.client_row.pk, now=self.now)
        self.assertEqual(projection["current_request"]["id"], str(request.pk))
        self.assertEqual(projection["status"], "pending")

    def test_repeated_gift_and_delivery_sources_reuse_same_case_without_manager_spam(self):
        self.rule.stock = self.variant.stock = 5
        self.rule.save(update_fields=["stock", "updated_at"])
        self.variant.save(update_fields=["stock"])
        first, _ = self.case("Це подарунок для мами. Усе разом однією посилкою")
        for text in ("This is a gift for my mother. All together in one parcel",
                "Это подарок для мамы. Всё вместе одной посылкой"):
            source = self.source(text)
            revision, _ = self.revision(source)
            result = review.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
            self.assertFalse(result["created"], result)
            self.assertEqual(result["request_id"], str(first.pk))
            self.assertEqual(result["reason"], "unchanged_customer_requirements")
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 1)
        self.assertEqual(review.fulfillment_review_projection(self.client_row.pk, now=self.now)["status"], "pending")

    def test_changed_logistics_and_gift_withdrawal_capture_new_sources(self):
        first, _ = self.case("Це подарунок. Усе разом")
        second, _ = self.case("Не подарунок. Окремими посилками")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(review._context_requirements(second.envelope["customer_requirements"]), {
            "gift": False, "delivery_preference": "split"})
        self.assertEqual(Request.objects.count(), 2)
        self.assertEqual(IgBotNotification.objects.filter(event_type="fulfillment_review").count(), 2)

    def test_changed_context_cannot_reuse_old_checked_availability_decision(self):
        request, _ = self.case("Це подарунок. Усе разом")
        self.assertTrue(self.decide(request)["ready"])
        source = self.source("Окремими посилками")
        _revision, _ = self.revision(source)
        self.assertEqual(review.fulfillment_review_projection(self.client_row.pk, now=self.now)["status"], "stale")

    def test_available_projection_rechecks_actual_fit_even_with_preloaded_cart(self):
        request, _ = self.case()
        self.assertTrue(self.decide(request)["ready"])
        from management.services.ig_commerce_projection import capture_current_selection_lines
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        before = review.fulfillment_review_projection(self.client_row.pk, now=self.now, current_source_cart=cart)
        self.assertEqual(before["status"], "reviewed", before)
        self.product.fit_options.filter(code="classic").update(is_active=False)
        after = review.fulfillment_review_projection(self.client_row.pk, now=self.now, current_source_cart=cart)
        self.assertEqual(after["status"], "pending", after)
        self.assertEqual(after["reason"], "configuration_changed")
        self.assertIsNone(after["decision"])
        task = IgFollowUpTask.objects.get(reason=review.UPDATE_REASON)
        self.assertFalse(review.validated_update_task(task, self.settings, now=self.now)["ready"])

    def test_available_projection_keeps_checked_commitment_when_only_stock_changes(self):
        request, _ = self.case()
        self.assertTrue(self.decide(request)["ready"])
        from management.services.ig_commerce_projection import capture_current_selection_lines
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        before = review.fulfillment_review_projection(self.client_row.pk, now=self.now, current_source_cart=cart)
        self.assertEqual(before["status"], "reviewed", before)
        self.rule.stock = None
        self.rule.save(update_fields=["stock", "updated_at"])
        self.variant.stock = 8
        self.variant.save(update_fields=["stock"])
        after = review.fulfillment_review_projection(self.client_row.pk, now=self.now, current_source_cart=cart)
        self.assertEqual(after["status"], "reviewed", after)
        self.assertEqual(after["decision"]["id"], before["decision"]["id"])
        self.assertEqual(after["decision"]["lines"][0]["configuration_digest"], before["decision"]["lines"][0]["configuration_digest"])

    def test_standing_stock_noise_keeps_proof_and_expiry_opens_only_one_new_case(self):
        _, subscription = self.accepted()
        from management.services.ig_restock_availability import observe_target
        target = subscription.accepted_answer.invitation.target
        first_observation = observe_target(target, now=self.now).as_dict()
        first = review.ensure_restock_manager_review(subscription, first_observation, now=self.now)
        request = Request.objects.get(pk=first["request_id"])
        self.assertTrue(self.decide(request)["ready"])
        first_proof = review.restock_manager_admission(subscription, first_observation, now=self.now)["proof"]
        self.rule.stock = 5
        self.rule.save(update_fields=["stock", "updated_at"])
        self.variant.stock = 5
        self.variant.save(update_fields=["stock"])
        new_observation = observe_target(target, now=self.now).as_dict()
        same = review.ensure_restock_manager_review(subscription, new_observation, now=self.now)
        self.assertFalse(same["created"])
        self.assertEqual(same["request_id"], first["request_id"])
        self.assertEqual(review.restock_manager_admission(subscription, new_observation, now=self.now)["proof"], first_proof)
        later = self.now + timedelta(hours=24, seconds=1)
        renewed = review.ensure_restock_manager_review(subscription, new_observation, now=later)
        self.assertTrue(renewed["created"], renewed)
        again = review.ensure_restock_manager_review(subscription, new_observation, now=later + timedelta(minutes=15))
        self.assertFalse(again["created"])
        self.assertEqual(again["request_id"], renewed["request_id"])
