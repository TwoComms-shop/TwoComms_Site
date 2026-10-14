"""Reusable actual API/card/source/hosted producers for Engineering40."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
import json
import hashlib
import uuid
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.db import connection
from django.urls import reverse

from management.models import IgFollowUpTask, InstagramBotMessage, IgCustomerTurnRevision, IgCheckoutInvoiceGeneration
from management.ig_assisted_commerce_models import IgAssistedCommerceDecision as Decision
from management.services import ig_assisted_partial as partial
from management.tests_ig_assisted_commerce_api import _AssistedApiFixture
from management import tests_ig_checkout_generation_runtime as _checkout_fixture


class PartialCommerceFixture(_AssistedApiFixture):
    def setUp(self):
        super().setUp()
        clock = patch("django.utils.timezone.now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def partial_case(self, *, line_count=2):
        self.assertIn(line_count, (2, 3))
        self.canonical("цвет purple, размер L")
        source = self.canonical("добавьте Storm, количество 1, размер L, цвет olive, крій classic")
        if line_count == 3:
            source = self.canonical("добавьте Storm, количество 2, размер M, цвет purple, крій classic")
        self.revision_row, _claim = self.revision(source)
        request, _operation, context = self.produced_request()
        items = context["editable_items"]
        self.assertEqual(len(items), line_count)
        for index, item in enumerate(items):
            self.assertEqual(item["item_kind"], "custom_apparel")
            item.update(title="Оливкове худі" if item["color"] == "olive" else "Фіолетове худі", unit_price="1590.00" if index == 0 else "1490.00",
                availability="ready" if index == 0 else "wait", checked_quantity=item["quantity"] if index == 0 else None,
                supply_basis="accessible_unrecorded" if index == 0 else "incoming_confirmed", source_location="Перевірено менеджером")
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Менеджер перевірив: " + str(question["requested_value"])}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        terms = {"now": {"payer": "customer", "customer_maximum": "120.00", "currency": "UAH"},
            "later": {"payer": "customer", "customer_maximum": "150.00", "currency": "UAH"},
            "together": {"payer": "merchant", "customer_maximum": "0.00", "currency": "UAH"}}
        draft = self.post({"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **context["cas"], "items": items, "shipping_terms": terms})
        self.assertEqual(draft.status_code, 200, draft.content)
        approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
            **draft.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        decision = Decision.objects.get(pk=approved.json()["decision_id"])
        return {"request": request, "decision": decision, "original_revision": self.revision_row,
            "commerce": approved.json()["commerce"]}

    def offer_case(self, *, line_count=2):
        result = self.partial_case() if line_count == 2 else self.partial_case(line_count=line_count)
        context = result["commerce"]["partial_intention"]
        self.assertTrue(context["can_issue"], context)
        payload = {"action": "issue_partial_offer", "operation_id": str(uuid.uuid4()),
            "request_id": str(result["request"].pk), "decision_id": str(result["decision"].pk),
            "expected_request_digest": result["request"].envelope_hmac,
            "expected_decision_digest": result["decision"].envelope_hmac,
            "expected_coverage_digest": context["coverage_digest"], "expected_terms_digest": context["terms_digest"]}
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        return {**result, "task": IgFollowUpTask.objects.get(pk=response.json()["offer"]["task_id"]),
            "offer_operation": payload, "offer_response": response}

    def approved_remainder_case(self, request, *, ready_only_first=False):
        remainder, adoption, resume_task, source, revision, token = self.resume_case(request)
        context = self.read()
        self.assertEqual(context["request"]["id"], str(remainder.pk))
        items = context["editable_items"]
        self.assertGreaterEqual(len(items), 1)
        for index, item in enumerate(items):
            self.assertEqual(item["item_kind"], "custom_apparel")
            ready = not ready_only_first or index == 0
            item.update(title="Оливкове худі" if item["color"] == "olive" else "Фіолетове худі", unit_price="1490.00",
                availability="ready" if ready else "wait", checked_quantity=item["quantity"] if ready else None,
                supply_basis="accessible_unrecorded" if ready else "incoming_confirmed",
                source_location="Нова фізична перевірка менеджера")
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Нова перевірка: " + str(question["requested_value"])}
                for question in remainder.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        terms = {"now": {"payer": "customer", "customer_maximum": "120.00", "currency": "UAH"},
            "later": {"payer": "customer", "customer_maximum": "150.00", "currency": "UAH"},
            "together": {"payer": "merchant", "customer_maximum": "0.00", "currency": "UAH"}}
        drafted = self.post({"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(remainder.pk),
            **context["cas"], "items": items, "shipping_terms": terms})
        self.assertEqual(drafted.status_code, 200, drafted.content)
        approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(remainder.pk),
            **drafted.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        decision = Decision.objects.get(pk=approved.json()["decision_id"])
        partial_context = approved.json()["commerce"]["partial_intention"]
        issued = self.post({"action": "issue_partial_offer", "operation_id": str(uuid.uuid4()),
            "request_id": str(remainder.pk), "decision_id": str(decision.pk),
            "expected_request_digest": remainder.envelope_hmac, "expected_decision_digest": decision.envelope_hmac,
            "expected_coverage_digest": partial_context["coverage_digest"], "expected_terms_digest": partial_context["terms_digest"]})
        self.assertEqual(issued.status_code, 200, issued.content)
        task = IgFollowUpTask.objects.get(pk=issued.json()["offer"]["task_id"])
        count, _ = self.send_card(task)
        self.assertEqual(count, 1)
        card = partial.card_for_task(task, self.settings, now=self.now)
        button = next(button for button in card["buttons"] if button.payload == card["actions"]["partial"])
        self.now += timedelta(seconds=1)
        purchase = self.source(button.title, payload=button.payload, at=self.now)
        purchase.reply_to_provider_message_id = task.provider_message_id
        purchase.save(update_fields=["reply_to_provider_message_id"])
        purchase_revision, purchase_token = self.revision(purchase, now=self.now)
        consumed = partial.consume_card_choice(purchase, purchase_revision, self.settings, now=self.now)
        self.assertTrue(consumed.get("ready"), consumed)
        return {"request": remainder, "decision": decision, "selection": consumed["selection"], "task": task,
            "source": purchase, "revision": purchase_revision, "token": purchase_token,
            "adoption": adoption, "resume_source": source, "resume_task": resume_task}

    def paid_remainder_case(self, request, *, ready_only_first=False):
        result = self.approved_remainder_case(request, ready_only_first=ready_only_first)
        prepared = self.prepare_selection(result["selection"])
        return {**result, "prepared": prepared, "order": self.pay_prepared(prepared)}

    def send_card(self, task, *, outcome="sent"):
        from management.services import instagram_bot as bot
        from management.services.ig_assisted_partial_delivery import process_due_assisted_partial_offers
        self.now += timedelta(seconds=1)
        from management.services import ig_assisted_commerce as commerce
        from management.services.ig_commerce_projection import capture_current_selection_lines
        captured_before = capture_current_selection_lines(self.client_row.pk, now=self.now)
        head_source_id = captured_before["source_watermark"]["message_id"]
        head = IgCustomerTurnRevision.objects.get(client=self.client_row, active_slot=1,
            sources__message_id=head_source_id)
        # The accepted current choice is completed by the live turn before a
        # later card is sent. Complete that exact native head, which may be
        # newer than the original manager request, and retain historical heads.
        InstagramBotMessage.objects.filter(pk__in=head.sources.values_list("message_id", flat=True),
            client=self.client_row, role="user", status="pending").update(status="done")
        IgCustomerTurnRevision.objects.filter(pk=head.pk, client=self.client_row).update(
            state="processed", claim_token="", lease_until=None)
        captured_after = capture_current_selection_lines(self.client_row.pk, now=self.now)
        def changed_paths(before, after, prefix=""):
            if isinstance(before, dict) and isinstance(after, dict):
                return [path for key in sorted(set(before) | set(after))
                    for path in changed_paths(before.get(key), after.get(key), prefix + "." + key)]
            if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
                return [path for index, (left, right) in enumerate(zip(before, after))
                    for path in changed_paths(left, right, prefix + f"[{index}]")]
            return [] if before == after else [prefix]
        self.card_terminal_capture_diagnostic = {
            "configuration_matches": commerce._configuration_capture(captured_before) == commerce._configuration_capture(captured_after),
            "changed_paths": changed_paths(captured_before, captured_after),
            "source_head_matches": captured_before.get("source_watermark") == captured_after.get("source_watermark")}
        physical = []
        def provider(_settings, _url, *, data, **kwargs):
            physical.append(json.loads(data))
            if outcome == "unknown":
                raise TimeoutError("Actual request reached transport; result unknown")
            return 200, json.dumps({"message_id": f"native-assisted-card:{task.pk}:{len(physical)}"})
        with patch("django.utils.timezone.now", return_value=self.now), \
                patch.object(bot, "get_page_token", return_value="synthetic-assisted-partial-token"), \
                patch.object(bot, "_provider_http", side_effect=provider):
            count = process_due_assisted_partial_offers(self.settings, now=self.now)
        task.refresh_from_db()
        from management.services.ig_delivery_plan import build_delivery_plan
        planned = build_delivery_plan(task.message_text)
        self.assertTrue(planned.complete)
        self.assertEqual(len(physical), len(planned.chunks) if outcome == "sent" else 1,
            {"count": count, "status": task.status, "skip_reason": task.skip_reason,
             "last_error": task.last_error, "card": partial.card_for_task(task, self.settings, now=self.now).get("reason"),
             "terminal_capture": self.card_terminal_capture_diagnostic})
        if outcome == "sent":
            self.assertEqual([part["message"]["text"] for part in physical], list(planned.chunks))
            self.assertTrue(physical[-1]["message"].get("quick_replies"))
            self.assertTrue(all(not part["message"].get("quick_replies") for part in physical[:-1]))
        self.assertEqual(physical[0]["recipient"], {"id": self.client_row.igsid})
        return count, physical

    def selected_case(self, *, action="partial", line_count=2):
        result = self.offer_case() if line_count == 2 else self.offer_case(line_count=line_count)
        task = result["task"]
        count, _physical = self.send_card(task)
        self.assertEqual(count, 1)
        card = partial.card_for_task(task, self.settings, now=self.now)
        self.assertTrue(card["ready"], card)
        button = next(button for button in card["buttons"] if button.payload == card["actions"][action])
        self.now += timedelta(seconds=1)
        source = self.source(button.title, payload=button.payload, at=self.now)
        source.reply_to_provider_message_id = task.provider_message_id
        source.save(update_fields=["reply_to_provider_message_id"])
        revision, token = self.revision(source, now=self.now)
        consumed = partial.consume_card_choice(source, revision, self.settings, now=self.now)
        self.assertTrue(consumed["ready"], consumed)
        return {**result, "revision": revision, "source": source, "selection": consumed.get("selection"), "choice_result": consumed, "token": token}


    _delivery_payload = _checkout_fixture.CheckoutGenerationRuntimeTests._delivery_payload

    def prepare_selection(self, selection, actor=None):
        from management.services.ig_assisted_checkout import prepare_assisted_checkout
        source_revision_id = IgCustomerTurnRevision.objects.filter(sources__message_id=selection.purchase_source_id).order_by("-pk").values_list("pk", flat=True).first()
        with override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False), \
                patch("management.services.instagram_bot._provider_http") as provider:
            result = prepare_assisted_checkout(self.client_row.pk, actor=actor or self.manager,
                request_id=selection.request_id, decision_id=selection.decision_id,
                expected_request_digest=selection.request_digest, expected_decision_digest=selection.decision_digest,
                operation_id=uuid.uuid4(), revision_id=source_revision_id, settings_id=self.settings.pk,
                verified_selection=selection)
        provider.assert_not_called()
        self.assertTrue(result.get("ready"), result)
        self.client_row.refresh_from_db()
        return result

    def pay_prepared(self, result):
        from storefront.views.monobank import _apply_payment_attempt_status
        proposal = result["proposal"]
        with override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False):
            response = self.client.get(result["checkout_url"], HTTP_HOST="twocomms.shop")
            self.assertEqual(response.status_code, 302, response.content)
            with patch("storefront.views.monobank._monobank_api_request", return_value={
                "invoiceId": f"partial-native-{proposal.pk}", "pageUrl": f"https://pay.example/partial-{proposal.pk}"}) as bank:
                posted = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                    self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="1"),
                    HTTP_ACCEPT="application/json", HTTP_HOST="twocomms.shop")
            self.assertEqual(posted.status_code, 200, posted.content)
            bank.assert_called_once()
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal, active_slot=1)
        callbacks = self.captureOnCommitCallbacks(execute=True) if hasattr(self, "captureOnCommitCallbacks") else nullcontext()
        with patch("management.services.instagram_bot._provider_http", return_value=(200,
                json.dumps({"message_id": f"native-partial-paid-{proposal.pk}"}))), callbacks:
            order, created = _apply_payment_attempt_status(generation.payment_attempt, "success",
                payload={"paidAmount": int(generation.payment_amount * 100)}, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        return order

    def resume_case(self, request):
        from management.services.ig_assisted_scopes import intention_coverage
        coverage = intention_coverage(self.client_row.pk, request.intention_id, now=self.now)
        self.assertTrue(coverage.get("ready"), coverage)
        task, _ = partial.issue_continuation_offer(self.client_row.pk, actor=self.manager,
            request_id=request.pk, expected_request_digest=request.envelope_hmac,
            expected_coverage_digest=coverage["coverage_digest"], operation_id=uuid.uuid4(), now=self.now)
        count, _ = self.send_card(task)
        self.assertEqual(count, 1)
        card = partial.card_for_task(task, self.settings, now=self.now)
        self.assertTrue(card.get("ready"), card)
        button = next(button for button in card["buttons"] if button.payload == card["actions"]["resume"])
        self.now += timedelta(seconds=1)
        source = self.source(button.title, payload=button.payload, at=self.now)
        source.reply_to_provider_message_id = task.provider_message_id
        source.save(update_fields=["reply_to_provider_message_id"])
        revision, token = self.revision(source, now=self.now)
        consumed = partial.consume_card_choice(source, revision, self.settings, now=self.now)
        self.assertTrue(consumed.get("ready"), consumed)
        from management.services import ig_assisted_commerce as commerce
        Request = commerce._models()[0]
        remainder = Request.objects.get(pk=consumed["residual_request_id"])
        self.client_row.refresh_from_db()
        return remainder, consumed["adoption"], task, source, revision, token


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeAssistedPartialSourceTests(PartialCommerceFixture, TransactionTestCase):
    def test_native_sent_receipt_and_partial_source_have_separate_purchase_capability(self):
        from management.services.ig_assisted_partial_delivery import capture_delivered_card, validate_card_choice_source
        result = self.selected_case()
        native, reason = capture_delivered_card(result["task"], self.settings, now=self.now)
        self.assertIsNotNone(native, reason)
        self.assertEqual(native.receipt["native_mid"], result["task"].provider_message_id)
        accepted = validate_card_choice_source(result["source"], result["revision"], self.settings, now=self.now)
        self.assertEqual(accepted.action, "partial")
        self.assertIsNotNone(result["selection"])

    def test_together_is_waiting_and_never_creates_purchase_or_restock_subscription(self):
        from management.models import IgCheckoutProposal
        from management.ig_restock_subscription_models import IgRestockSubscription
        result = self.selected_case(action="together")
        self.assertIsNone(result["selection"])
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgRestockSubscription.objects.exists())

    def test_unknown_card_has_no_receipt_or_blind_retry(self):
        from management.services.ig_assisted_partial_delivery import capture_delivered_card, process_due_assisted_partial_offers
        result = self.offer_case()
        count, _ = self.send_card(result["task"], outcome="unknown")
        self.assertEqual(count, 0)
        self.assertEqual(result["task"].status, "ambiguous")
        self.assertIsNone(capture_delivered_card(result["task"], self.settings, now=self.now)[0])
        with patch("management.services.instagram_bot._provider_http") as provider:
            self.assertEqual(process_due_assisted_partial_offers(self.settings, now=self.now), 0)
        provider.assert_not_called()

    def test_changed_native_source_is_not_reinterpreted_as_adoption(self):
        from management.services.ig_assisted_partial_delivery import validate_card_choice_source, AssistedPartialSourceError
        result = self.selected_case()
        result["source"].text += " Але змініть колір на чорний."
        result["source"].save(update_fields=["text"])
        with self.assertRaises(AssistedPartialSourceError):
            validate_card_choice_source(result["source"], result["revision"], self.settings, now=self.now)

    def test_sealed_source_with_structural_payload_and_wrong_mac_has_no_native_choice(self):
        from management.services.ig_assisted_partial_delivery import validate_card_choice_source, AssistedPartialSourceError
        result = self.selected_case()
        card = partial.card_for_task(result["task"], self.settings, now=self.now, require_current=False)
        payload = card["actions"]["partial"]
        altered = payload[:-1] + ("e" if payload[-1] == "f" else "f")
        title = next(button.title for button in card["buttons"] if button.payload == payload)
        self.now += timedelta(seconds=1)
        source = self.source(title, payload=altered, at=self.now)
        source.reply_to_provider_message_id = result["task"].provider_message_id
        source.save(update_fields=["reply_to_provider_message_id"])
        revision, _token = self.revision(source, now=self.now)
        with self.assertRaises(AssistedPartialSourceError) as caught:
            validate_card_choice_source(source, revision, self.settings, now=self.now, require_current=False)
        self.assertEqual(caught.exception.reason, "assisted_card_choice_source_changed")


    def test_paid_first_scope_resume_adopts_only_residual_into_one_fresh_owner(self):
        from management.models import IgCommercialEpisode, IgCommerceSelectionTransition
        from management.services.ig_assisted_continuation_sources import verify_assisted_adoption_transition
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_assisted_scopes import intention_coverage
        result = self.selected_case()
        first = self.pay_prepared(self.prepare_selection(result["selection"]))
        old_items = list(first.items.values("pk", "product_id", "qty", "size"))
        old_episode = IgCommercialEpisode.objects.get(intended_order=first)
        remainder, adoption, task, source, revision, token = self.resume_case(result["request"])
        self.assertEqual(remainder.intention_id, result["request"].intention_id)
        self.assertNotEqual(self.client_row.current_commercial_episode_id, old_episode.pk)
        actual = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(actual["status"], "captured", actual)
        self.assertEqual(len(actual["lines"]), 1)
        self.assertTrue(all(field["authority"] == "assisted_continuation" for field in actual["lines"][0]["fields"].values()))
        self.assertNotIn("purchase_requested", actual["lines"][0]["fields"])
        self.assertTrue(verify_assisted_adoption_transition(adoption["transition_id"], now=self.now)["ready"])
        before = IgCommercialEpisode.objects.filter(client=self.client_row).count()
        again = partial.consume_card_choice(source, revision, self.settings, now=self.now)
        self.assertTrue(again["ready"], again)
        self.assertEqual(again["residual_request_id"], str(remainder.pk))
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.client_row).count(), before)
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(action="assisted_residual_adoption", source_message=source).count(), 1)
        self.assertEqual(list(first.items.values("pk", "product_id", "qty", "size")), old_items)
        coverage = intention_coverage(self.client_row.pk, remainder.intention_id, now=self.now)
        self.assertEqual(len(coverage["settled_line_ids"]), 1)
        self.assertEqual(len(coverage["deferred_line_ids"]), 1)

    def test_new_approval_and_new_purchase_pay_only_residual_without_changing_first_order(self):
        from management.services.ig_assisted_scopes import intention_coverage
        result = self.selected_case()
        first = self.pay_prepared(self.prepare_selection(result["selection"]))
        original_items = list(first.items.values("pk", "product_id", "qty", "size"))
        later = self.paid_remainder_case(result["request"])
        second = later["order"]
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.items.count(), 1)
        self.assertEqual(second.items.count(), 1)
        self.assertEqual(list(first.items.values("pk", "product_id", "qty", "size")), original_items)
        coverage = intention_coverage(self.client_row.pk, result["request"].intention_id, now=self.now)
        self.assertEqual(len(coverage["settled_line_ids"]), 2)
        self.assertFalse(coverage["deferred_line_ids"])

    def test_three_native_paid_scopes_conserve_original_lines_and_bounded_history(self):
        from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
        from management.services import ig_assisted_checkout as checkout, ig_assisted_commerce as commerce
        from management.services.ig_assisted_scopes import intention_coverage, original_intention_source_history

        first = self.selected_case(line_count=3)
        root = first["request"]
        original_envelope = deepcopy(root.envelope)
        original_quantities = dict(first["selection"].coverage["original_quantities"])
        self.assertEqual(len(original_quantities), 3)
        self.assertEqual(sorted(original_quantities.values()), [1, 1, 2])
        original_history = original_intention_source_history(root, now=self.now)
        self.assertTrue(original_history["ready"], original_history)

        first_order = self.pay_prepared(self.prepare_selection(first["selection"]))
        first_order.refresh_from_db()
        old_first_payload = deepcopy(first_order.payment_payload)
        old_first_items = list(first_order.items.values("pk", "product_id", "qty", "size"))
        second = self.paid_remainder_case(root, ready_only_first=True)
        second_order = second["order"]
        second_order.refresh_from_db()
        old_second_payload = deepcopy(second_order.payment_payload)
        old_second_items = list(second_order.items.values("pk", "product_id", "qty", "size"))
        after_second = intention_coverage(self.client_row.pk, root.intention_id, now=self.now)
        self.assertTrue(after_second["ready"], after_second)
        self.assertEqual(len(after_second["settled_line_ids"]), 2)
        self.assertEqual(len(after_second["deferred_line_ids"]), 1)
        third = self.paid_remainder_case(second["request"])
        third_order = third["order"]

        orders = (first_order, second_order, third_order)
        self.assertEqual(len({order.pk for order in orders}), 3)
        all_selected = set()
        for order in orders:
            execution = Execution.objects.get(order=order)
            body = execution.envelope["snapshot"]
            selected = set(body["selected_line_ids"])
            self.assertEqual(execution.intention_id, root.intention_id)
            self.assertEqual(len(selected), 1)
            self.assertTrue(all_selected.isdisjoint(selected))
            all_selected.update(selected)
            self.assertNotIn("quote_snapshot", body)
            self.assertNotIn("source_cart_binding", body)
            self.assertLessEqual(len(commerce._json(execution.envelope).encode("utf-8")), commerce.MAX_JSON_BYTES)
            quote_revision = execution.proposal.revisions.get(pk=body["checkout_revision_id"])
            self.assertLessEqual(len(json.dumps(quote_revision.snapshot, ensure_ascii=False,
                sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")), 1024 * 1024)
            for digest_key, native_value in (("quote_snapshot_digest", quote_revision.snapshot),
                    ("binding_digest", quote_revision.snapshot["source_cart_binding"])):
                canonical_json = json.dumps(native_value, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
                self.assertEqual(body[digest_key], hashlib.sha256(canonical_json).hexdigest())
            checked = checkout.validate_assisted_order(order, now=self.now)
            self.assertTrue(checked["ready"], checked)
            self.assertEqual({row["line_id"] for row in checked["item_map"]}, selected)
            self.assertEqual(order.items.count(), 1)
            self.assertEqual(sum(order.items.values_list("qty", flat=True)),
                sum(original_quantities[line_id] for line_id in selected))

        self.assertEqual(all_selected, set(original_quantities))
        coverage = intention_coverage(self.client_row.pk, root.intention_id, now=self.now)
        self.assertTrue(coverage["ready"], coverage)
        self.assertEqual(coverage["original_quantities"], original_quantities)
        self.assertEqual(set(coverage["settled_line_ids"]), all_selected)
        self.assertFalse(coverage["deferred_line_ids"])
        self.assertFalse(coverage["inflight_line_ids"])
        self.assertEqual(len(coverage["orders"]), 3)
        history = original_intention_source_history(third["request"], now=self.now)
        self.assertTrue(history["ready"], history)
        self.assertEqual(history["original_lines"], original_history["original_lines"])
        self.assertEqual(history["origin_full_capture"], original_history["origin_full_capture"])
        self.assertEqual(len(history["request_history"]), 3)
        root.refresh_from_db()
        self.assertEqual(root.envelope, original_envelope)
        for order, payload, items in ((first_order, old_first_payload, old_first_items),
                (second_order, old_second_payload, old_second_items)):
            order.refresh_from_db()
            self.assertEqual(order.payment_payload, payload)
            self.assertEqual(list(order.items.values("pk", "product_id", "qty", "size")), items)
