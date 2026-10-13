"""Source question -> native review, without stock/checkout authority."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from management.services import ig_product_inquiries as inquiries
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_response_control import ResponseControl, ValidatedResponse
from management.tests_ig_restock_subscriptions import _RestockFixture


class ProductInquirySourceTests(SimpleTestCase):
    def sources(self, *texts):
        return [{"message_id": index + 10, "role": "user", "text": text, "source_digest": str(index) * 64}
                for index, text in enumerate(texts)]

    def capture(self, *texts, sizes=("S", "M", "L"), colors=("Black",), **kwargs):
        return inquiries.capture_product_inquiries(self.sources(*texts),
            cart_capture={"active_line_id": "shirt", "lines": [{"line_id": "shirt", "product_id": 12}]},
            readiness_by_line={"shirt": {"product": {"id": 12}, "size": {"available": list(sizes)},
                                        "color": {"options": [{"name": value} for value in colors]}}}, **kwargs)

    def test_new_colors_are_source_wishes_not_substituted_catalog_colors(self):
        for text, value in (("Хочу оливковую футболку", "olive"), ("Хочу фіолетову футболку", "purple"),
                            ("Хочу бежевую футболку", "beige"), ("Хочу футболку бордо", "burgundy"),
                            ("Хочу помаранчеву футболку", "orange")):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).field_updates["color"], value)
                disposition = self.capture(text)
                self.assertEqual(disposition["reason"], "product_configuration_review")
                self.assertEqual(disposition["inquiries"][0]["requested_value"], value)
                self.assertFalse(disposition["inquiries"][0]["checkout_authority"])

    def test_exact_catalog_values_need_no_product_review(self):
        for text in ("Хочу чёрную футболку M", "Есть размер M?", "Is the purple shirt available?"):
            self.assertEqual(self.capture(text, colors=("Black", "Purple"))["status"], "none", text)
        self.assertEqual(self.capture("Есть размер 3XL?", sizes=("M", "XXXL"))["status"], "none")

    def test_size_question_remains_question_and_is_reviewed_without_choice_mutation(self):
        for text, wanted in (("Есть XXS?", "XXS"), ("Есть 3XL?", "XXXL")):
            self.assertNotIn("size", parse_turn(text).field_updates)
            self.assertEqual(self.capture(text)["inquiries"][0]["requested_value"], wanted)
        self.assertEqual(parse_turn("Хочу 3XL").field_updates["size"], "XXXL")

    def test_product_hardware_and_composition_inquiries_are_qualified(self):
        for text in ("Які люверси на худі?", "Какие люверсы?", "What eyelets does this hoodie have?",
                     "What material are its grommets?", "Яка резинка на худі?", "What fabric is the cuff?",
                     "Какой состав ткани?", "Який склад?"):
            with self.subTest(text=text):
                self.assertEqual(self.capture(text)["reason"], "product_information_review")
        for text in ("Which button should I click to pay?", "На складе уже есть мой заказ?",
                     "Не интересует состав ткани", "Раньше хотел оливковую футболку", 'Принт "purple zipper"',
                     "Вчера спрашивал какой состав ткани", "Спасибо", "Я буду на складе"):
            self.assertEqual(self.capture(text)["status"], "none", text)

    def test_correction_withdraws_earlier_unsupported_wish(self):
        self.assertEqual(self.capture("Хочу оливковую футболку", "Нет, хочу чёрную")["status"], "none")
        disposition = self.capture("Не чёрную, а фиолетовую футболку хочу")
        self.assertEqual([row["requested_value"] for row in disposition["inquiries"]], ["purple"])
        for text in ("Не хочу чорну, хочу фіолетову", "Не чорне, а фіолетове худі", "Краще фіолетову",
                     "Actually purple instead of black", "olive instead"):
            wanted = "olive" if "olive" in text else "purple"
            self.assertEqual([row["requested_value"] for row in self.capture(text)["inquiries"]], [wanted], text)

    def test_spec_answer_cannot_hide_behind_manager_control_or_later_check(self):
        disposition = self.capture("Какие люверсы на худи?")
        for text in ("Худи с металлическими люверсами. Уточню у менеджера.",
                     "It has metal eyelets; I will check with our manager.", "Да, металлические, уточню размер.",
                     "Худи с люверсами, так, позже уточню.", "It does. I'll check with our manager.",
                     "I will check with our manager, the fabric is 100% cotton.",
                     "Уточню у менеджера, манжети з бавовни.", "I will check: the fabric is cotton."):
            response = ValidatedResponse(text, (ResponseControl("manager", True),))
            self.assertEqual(inquiries.validate_inquiry_response(disposition, response), "product_information_unverified", text)
        response = ValidatedResponse(inquiries.inquiry_reply(disposition), (ResponseControl("manager", True),))
        self.assertEqual(inquiries.validate_inquiry_response(disposition, response), "")
        self.assertIn("SOURCE-BOUND PRODUCT INQUIRIES", inquiries.inquiry_prompt(disposition))
        for text in ("Я уточню состав у менеджера.", "Я перевірю склад тканини у менеджера.",
                     "I'll check the fabric composition with our manager.", "I will check the fabric composition with our manager."):
            self.assertEqual(inquiries.validate_inquiry_response(disposition,
                ValidatedResponse(text, (ResponseControl("manager", True),))), "", text)

    def test_subscribe_is_not_rib_and_localized_colors_are_human_names(self):
        self.assertEqual(self.capture("Can I subscribe to restock updates?")["status"], "none")
        for text, word in (("Хочу фіолетове худі", "фіолетовий"), ("Ответьте на русском. Хочу оливковую футболку", "оливковый")):
            rendered = inquiries.inquiry_reply(self.capture(text))
            self.assertIn(word, rendered)
            self.assertNotRegex(rendered, r"olive|purple")

    def test_unverified_configuration_has_no_manufacturing_or_arrival_window(self):
        disposition = self.capture("Хочу фіолетове худі")
        for text in ("Фіолетове худі зробимо й відправимо за 1–3 дні, уточню у менеджера.",
                     "We will make it in 10 days; I will check with our manager.",
                     "Ви отримаєте фіолетове худі завтра, менеджер перевірить."):
            self.assertEqual(inquiries.validate_inquiry_response(disposition,
                ValidatedResponse(text, (ResponseControl("manager", True),))), "product_inquiry_fulfillment_unverified", text)
        self.assertEqual(inquiries.validate_inquiry_response(disposition,
            ValidatedResponse(inquiries.inquiry_reply(disposition), (ResponseControl("manager", True),))), "")

    def test_known_color_resolution_requires_verified_variant_and_exact_source_choice(self):
        source = self.sources("Хочу чёрную футболку")
        cart = {"active_line_id": "shirt", "lines": [{"line_id": "shirt", "product_id": 12}]}
        color = {"options": [{"name": "#111111"}], "resolved_choice": "black", "selected_variant_id": 17,
                 "verified_resolution": True}
        state = {"shirt": {"product": {"id": 12}, "color": color}}
        self.assertEqual(inquiries.capture_product_inquiries(source, cart_capture=cart, readiness_by_line=state)["status"], "none")
        color["selected_variant_id"] = None
        self.assertEqual(inquiries.capture_product_inquiries(source, cart_capture=cart, readiness_by_line=state)["status"], "manager_required")

    def test_multi_line_size_correction_does_not_erase_other_lines_pending_wish(self):
        for reverse in (False, True):
            texts = ("I want XXXL for this hoodie", "I want M for this shirt")
            sources = self.sources(*(reversed(texts) if reverse else texts))
            bad_id = next(row["message_id"] for row in sources if "XXXL" in row["text"])
            good_id = next(row["message_id"] for row in sources if "XXXL" not in row["text"])
            cart = {"active_line_id": "b", "lines": [{"line_id": "a", "product_id": 12,
                    "fields": {"size": {"source": {"source_message_id": bad_id}}}},
                    {"line_id": "b", "product_id": 13, "fields": {"size": {"source": {"source_message_id": good_id}}}}]}
            states = {line: {"size": {"available": ["M"]}} for line in ("a", "b")}
            result = inquiries.capture_product_inquiries(sources, cart_capture=cart, readiness_by_line=states)
            self.assertEqual([(row["line_id"], row["requested_value"]) for row in result["inquiries"]], [("a", "XXXL")])

    def test_configuration_review_does_not_grant_stock_or_checkout(self):
        disposition = self.capture("Хочу оливковую футболку")
        response = ValidatedResponse("Оливковая есть в наличии, менеджер проверит.", (ResponseControl("manager", True),))
        self.assertEqual(inquiries.validate_inquiry_response(disposition, response), "product_inquiry_availability_unverified")
        response = ValidatedResponse(inquiries.inquiry_reply(disposition), (ResponseControl("manager", True), ResponseControl("paylink", True)))
        self.assertEqual(inquiries.validate_inquiry_response(disposition, response), "product_inquiry_checkout_unverified")

    def test_source_and_line_change_remove_capture_and_newer_cart_abstains(self):
        sources = self.sources("What eyelets does this hoodie have?")
        snapshot = hashlib.sha256(json.dumps(sources).encode()).hexdigest()
        revision = SimpleNamespace(bundle_snapshot={"sources": sources}, snapshot_digest=snapshot,
                                   client_id=5, client=SimpleNamespace(language="en"), action_receipts={}, generation_proposal={})
        capability = inquiries.ProductInquiryCapture(snapshot, json.dumps(inquiries.source_refs(sources)),
            json.dumps({"scope": {"client_id": 5}, "active_line_id": "a", "lines": [{"line_id": "a", "product_id": 12}]}), "{}", "en")
        self.assertIsNotNone(capability.recompute(revision))
        revision.bundle_snapshot["sources"][0]["text"] = "Thanks"
        self.assertIsNone(capability.recompute(revision))
        result = inquiries.capture_product_inquiries(sources, cart_capture={"fence": {"source_ids": [999]}})
        self.assertEqual(result["gap"], "product_inquiry_cart_newer_than_source")
        self.assertEqual(inquiries.capture_product_inquiries([sources[0], sources[0]])["gap"], "product_inquiry_source_bound")


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeProductInquiryTests(_RestockFixture, TestCase):
    def product_case(self, text):
        from management.services.ig_response_plan import capture_response_plan
        source = self.source(text)
        revision, token = self.revision(source)
        revision.client = self.client_row
        plan = capture_response_plan(self.client_row, revision=revision)
        disposition = inquiries.product_inquiries_for_revision(revision, response_plan=plan)
        self.assertEqual(disposition["status"], "manager_required", disposition)
        return revision, token, plan, disposition

    def test_native_case_is_same_source_owned_task_telegram_and_card_on_replay(self):
        from management.services.ig_revision_intents import ensure_product_inquiry_review_case, manager_case_reason
        from management.models import IgFollowUpTask, IgBotNotification
        revision, _token, _plan, disposition = self.product_case("Які люверси на цьому худі?")
        self.assertEqual(manager_case_reason(revision), "product_information_review")
        with patch("management.services.instagram_bot._provider_http") as http, transaction.atomic():
            first, notification = ensure_product_inquiry_review_case(revision, disposition)
            second, again = ensure_product_inquiry_review_case(revision, disposition)
        http.assert_not_called()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(notification.pk, again.pk)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:product_information_review").count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(pk=notification.pk).count(), 1)
        self.assertEqual(first.manager_context["product_inquiries"], disposition)
        self.assertIn("Які люверси на цьому худі?", first.message_text)
        self.assertEqual(second.message_text.count("Запит клієнта:"), 1)
        self.assertFalse(first.manager_context["authority"]["checkout_authorized"])
        with transaction.atomic(), self.assertRaises(ValueError):
            ensure_product_inquiry_review_case(revision, {**disposition, "inquiries": []})

    def test_coverage_recomputes_native_capability_and_rejects_raw_injection(self):
        from management.services.ig_response_debt import record_response_coverage, response_coverage
        revision, token, plan, disposition = self.product_case("Какой состав ткани?")
        coverage = {"version": 1, "plan_digest": plan.digest, "covered": [], "remaining": ["inquiry"],
                    "disposition": "recovery", "product_inquiry_capture": deepcopy(disposition)}
        self.assertFalse(record_response_coverage(revision.pk, token, coverage))
        coverage["product_inquiry_capture"] = revision._product_inquiry_capture
        self.assertTrue(record_response_coverage(revision.pk, token, coverage))
        revision.refresh_from_db()
        self.assertEqual(response_coverage(revision)["product_inquiries"], disposition)
        self.assertEqual(inquiries.product_inquiries_for_revision(revision), disposition)

    def test_native_subscription_question_does_not_create_product_review(self):
        from management.services.ig_response_plan import capture_response_plan
        source = self.source("Can I subscribe to restock updates?")
        revision, _token = self.revision(source)
        revision.client = self.client_row
        plan = capture_response_plan(self.client_row, revision=revision)
        self.assertEqual(inquiries.product_inquiries_for_revision(revision, response_plan=plan)["status"], "none")

    def test_product_case_gate_rejects_closed_task_and_changed_exact_context(self):
        from management.services.ig_revision_intents import ensure_product_inquiry_review_case, product_review_receipt_reason
        from management.services.ig_revision_live import _revision_response_purpose_reason
        from management.services.ig_response_debt import record_response_coverage
        revision, token, plan, disposition = self.product_case("What eyelets does this hoodie have?")
        coverage = {"version": 1, "plan_digest": plan.digest, "covered": [], "remaining": ["inquiry"],
                    "disposition": "recovery", "product_inquiry_capture": revision._product_inquiry_capture}
        self.assertTrue(record_response_coverage(revision.pk, token, coverage))
        with transaction.atomic():
            task, notification = ensure_product_inquiry_review_case(revision, disposition)
        revision.refresh_from_db()
        revision.generation_proposal_digest = "d" * 64
        revision.generation_proposal = {"source_cart_capture": plan.source_cart_capture}
        revision.save(update_fields=["generation_proposal", "generation_proposal_digest"])
        self.assertEqual(_revision_response_purpose_reason(revision), "product_inquiry_handoff_missing")
        revision.action_receipts = {**revision.action_receipts, "manager_handoff": {
            "case_kind": disposition["reason"], "task_id": task.pk, "notification_id": notification.pk,
            "snapshot_digest": revision.snapshot_digest, "generation_proposal_digest": "d" * 64}}
        revision.save(update_fields=["action_receipts", "generation_proposal", "generation_proposal_digest"])
        self.assertEqual(product_review_receipt_reason(revision, task=task, notification=notification), "")
        detached = deepcopy(revision)
        detached.action_receipts["manager_handoff"]["case_kind"] = "customer_manager_request"
        self.assertEqual(product_review_receipt_reason(detached, task=task, notification=notification), "product_inquiry_handoff_invalid")
        for status in ("completed", "cancelled"):
            task.status = status
            task.save(update_fields=["status"])
            self.assertEqual(product_review_receipt_reason(revision, task=task, notification=notification), "product_inquiry_handoff_no_longer_open")
            self.assertEqual(_revision_response_purpose_reason(revision), "product_inquiry_handoff_no_longer_open")
        task.status = "skipped"
        task.manager_context = {**task.manager_context, "product_inquiries": {**disposition, "inquiries": []}}
        task.save(update_fields=["status", "manager_context"])
        self.assertEqual(product_review_receipt_reason(revision, task=task, notification=notification), "product_inquiry_handoff_invalid")
        self.assertEqual(_revision_response_purpose_reason(revision), "product_inquiry_handoff_invalid")


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
                   IG_REVISION_OPTIONAL_HOLDING_ENABLED=True)
class NativeProductInquiryHoldingTests(_RestockFixture, TransactionTestCase):
    def test_conclusive_failed_graph_uses_same_product_case_and_natural_holding(self):
        from management.models import GeminiRequest, IgFollowUpTask, IgBotNotification
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_revision_holding import record_technical_holding, holding_receipt_valid
        source = self.source("What eyelets does this hoodie have?")
        revision, token = self.revision(source)
        revision.client = self.client_row
        plan = capture_response_plan(self.client_row, revision=revision)
        disposition = inquiries.product_inquiries_for_revision(revision, response_plan=plan)
        GeminiRequest.objects.create(request_id=f"product-inquiry-{revision.pk}",
            logical_turn_id=f"ig-revision:{revision.pk}", client_id=revision.client_id,
            lane="live", task_class="simple_live", terminal_resolution="failed", accounting_mode="shadow")
        with patch("management.services.instagram_bot._provider_http") as http:
            result = record_technical_holding(revision.pk, token, settings_id=self.settings.pk,
                product_inquiry_capture=revision._product_inquiry_capture)
        http.assert_not_called()
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.receipt["reply_text"], inquiries.inquiry_reply(disposition))
        self.assertNotIn("failed", result.receipt["reply_text"])
        task = IgFollowUpTask.objects.get(pk=result.receipt["task_id"])
        self.assertEqual(task.reason, "revision_case:product_information_review")
        self.assertEqual(task.manager_context["product_inquiries"], disposition)
        self.assertEqual(IgBotNotification.objects.get(pk=result.receipt["notification_id"]).status, "pending")
        revision.refresh_from_db()
        self.assertTrue(holding_receipt_valid(revision))
        again = record_technical_holding(revision.pk, token, settings_id=self.settings.pk)
        self.assertTrue(again.ready, again.reason)
        self.assertEqual(again.receipt["task_id"], result.receipt["task_id"])
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:product_information_review").count(), 1)

    def test_failed_graph_restart_reconstructs_known_product_size_inquiry(self):
        from management.models import GeminiRequest, IgFollowUpTask
        from management.services.ig_revision_holding import record_technical_holding
        from management.services.ig_response_plan import capture_response_plan
        source = self.source("Do you have XL?")
        revision, token = self.revision(source)
        revision.client = self.client_row
        current_plan = capture_response_plan(self.client_row, revision=revision)
        known_sizes = current_plan.readiness_snapshot["size"]["available"]
        self.assertTrue(known_sizes)
        self.assertNotIn("XL", known_sizes)
        self.assertEqual(inquiries.product_inquiries_for_revision(revision, response_plan=current_plan)["reason"], "product_configuration_review")
        GeminiRequest.objects.create(request_id=f"product-inquiry-restart-{revision.pk}",
            logical_turn_id=f"ig-revision:{revision.pk}", client_id=revision.client_id,
            lane="live", task_class="simple_live", terminal_resolution="failed", accounting_mode="shadow")
        result = record_technical_holding(revision.pk, token, settings_id=self.settings.pk)
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(result.receipt["product_inquiries"]["reason"], "product_configuration_review")
        self.assertEqual(IgFollowUpTask.objects.get(pk=result.receipt["task_id"]).reason, "revision_case:product_configuration_review")
        self.assertIn("XL", result.receipt["reply_text"])
