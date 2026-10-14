"""Native source -> manager terms -> real card -> own partial choice."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import Client, TransactionTestCase, override_settings

from management.bot_access import (OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION,
    META_REVIEWER_GROUP_NAME)
from management.models import IgFollowUpTask, IgCheckoutProposal, InstagramBotMessage
from management.services import ig_assisted_partial as partial
from management.tests_ig_clients_ui import _operator


from management.tests_ig_assisted_partial_sources import PartialCommerceFixture


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test",
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativePartialCommerceApiTests(PartialCommerceFixture, TransactionTestCase):
    def test_card_locale_hint_does_not_admit_changed_foreign_mixed_or_copied_sources(self):
        actual_source = self.canonical

        def ukrainian_source(text, **kwargs):
            return actual_source(text + ". Please reply in Ukrainian.", **kwargs)

        with patch.object(self, "canonical", side_effect=ukrainian_source):
            data = self.offer_case()
        self.assertEqual(self.send_card(data["task"])[0], 1)
        card = partial.card_for_task(data["task"], self.settings, now=self.now)
        self.assertTrue(card["ready"], card)
        self.assertEqual(card["proof"]["locale"], "uk")
        button = next(button for button in card["buttons"] if button.payload == card["actions"]["partial"])
        cases = {
            "changed_role": {"role": InstagramBotMessage.Role.MODEL},
            "foreign_sender": {"sender_id": "another-instagram-customer"},
            "mixed_source": {"text": button.title + ". Please reply in English."},
            "copied_old_receipt": {"reply_to_provider_message_id": "different-old-delivered-card"},
        }
        with patch("management.services.instagram_bot._provider_http") as provider:
            for name, changes in cases.items():
                with self.subTest(name=name):
                    self.now += timedelta(seconds=1)
                    source = self.source(button.title, payload=button.payload, at=self.now)
                    source.reply_to_provider_message_id = data["task"].provider_message_id
                    before_seal = {field: value for field, value in changes.items() if field not in {"role", "sender_id"}}
                    for field, value in before_seal.items():
                        setattr(source, field, value)
                    source.save(update_fields=["reply_to_provider_message_id", *before_seal])
                    revision, _token = self.revision(source, now=self.now)
                    after_seal = {field: value for field, value in changes.items() if field in {"role", "sender_id"}}
                    for field, value in after_seal.items():
                        setattr(source, field, value)
                    if after_seal:
                        source.save(update_fields=list(after_seal))
                    result = partial.consume_card_choice(source, revision, self.settings, now=self.now)
                    self.assertFalse(result["ready"], result)
                    self.assertIsNone(result.get("selection"))
                    data["task"].refresh_from_db()
                    self.assertNotIn(partial.CHOICE_KEY, data["task"].manager_context)
                    self.assertFalse(IgCheckoutProposal.objects.exists())
            provider.assert_not_called()

    def test_native_continuation_locale_survives_into_new_remainder_card_with_stale_profile(self):
        self.client_row.language = "ru"
        self.client_row.save(update_fields=["language"])
        actual_source = self.canonical

        def english_source(text, **kwargs):
            return actual_source(text + ". Please reply in English.", **kwargs)

        with patch.object(self, "canonical", side_effect=english_source):
            initial = self.selected_case()
        self.assertEqual(partial.selection_reply_language(initial["selection"]), "en")
        prepared = self.prepare_selection(initial["selection"])
        self.pay_prepared(prepared)
        actual_consume = partial.consume_card_choice

        def consume_with_stale_resume_profile(source, revision, settings_obj, **kwargs):
            parsed = partial.parse_card_payload(source.quick_reply_payload)
            if parsed and parsed[1] == "resume":
                resume_card = partial.card_for_task(IgFollowUpTask.objects.get(pk=parsed[0]), require_current=False)
                self.assertTrue(resume_card["ready"], resume_card)
                self.assertEqual(resume_card["proof"]["locale"], "en")
                # Stage the hint after real S2 ingress but before its native
                # adoption captures R2. A post-R2 owner change must invalidate
                # that request rather than weakening its ownership fence.
                self.client_row.language = "ru"
                self.client_row.save(update_fields=["language"])
            return actual_consume(source, revision, settings_obj, **kwargs)

        with patch.object(partial, "consume_card_choice", side_effect=consume_with_stale_resume_profile):
            remainder = self.approved_remainder_case(initial["request"])
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.language, "ru")
        card = partial.card_for_task(remainder["task"], self.settings, now=self.now)
        self.assertTrue(card["ready"], card)
        self.assertEqual(card["proof"]["locale"], "en")
        self.assertNotEqual(remainder["task"].pk, initial["task"].pk)
        self.assertEqual(partial.selection_reply_language(remainder["selection"]), "en")
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.language, "ru")

    def test_mixed_remainder_card_discloses_actual_later_cap_for_ready_items(self):
        first = self.selected_case(line_count=3)
        initial_card = partial.card_for_task(first["task"], require_current=False)
        self.assertTrue(initial_card["ready"], initial_card)
        self.pay_prepared(self.prepare_selection(first["selection"]))
        remainder = self.approved_remainder_case(first["request"], ready_only_first=True)
        card = partial.card_for_task(remainder["task"], self.settings, now=self.now)
        self.assertTrue(card["ready"], card)
        quote = card["proof"]["quote"]
        self.assertEqual(quote["delivery_scope"], "later")
        self.assertEqual(len(quote["selected_line_ids"]), 1)
        self.assertEqual(len(quote["deferred_line_ids"]), 1)
        self.assertEqual(quote["shipping_terms"]["now"]["customer_maximum"], "120.00")
        self.assertEqual(quote["shipping_terms"]["later"]["customer_maximum"], "150.00")
        copy = partial.COPY[card["proof"]["locale"]]
        ready_fee = f"{copy['ready']} — {copy['fee']} 150.00 UAH."
        self.assertIn(ready_fee, card["text"].splitlines())
        self.assertNotIn(f"{copy['ready']} — {copy['fee']} 120.00 UAH.", card["text"].splitlines())
        self.assertEqual(remainder["selection"].delivery_scope, "later")
        frozen_initial = partial.card_for_task(first["task"], require_current=False)
        self.assertTrue(frozen_initial["ready"], frozen_initial)
        self.assertEqual(frozen_initial["text"], initial_card["text"])

    def test_manager_issue_replay_creates_one_card_without_purchase_or_provider(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = self.offer_case()
            replay = self.post(result["offer_operation"])
            self.assertEqual(replay.status_code, 200, replay.content)
            self.assertFalse(replay.json()["created"])
            self.assertEqual(IgFollowUpTask.objects.filter(reason=partial.PARTIAL_REASON).count(), 1)
            self.assertFalse(IgCheckoutProposal.objects.exists())
            self.assertEqual(replay.json()["offer"]["delivery"]["status"], "pending")
            self.assertIsNone(replay.json()["offer"]["choice"])
            blind = {**result["offer_operation"], "operation_id": str(uuid.uuid4())}
            self.assertEqual(self.post(blind).status_code, 409)
            provider.assert_not_called()

    def test_actual_own_button_creates_only_selected_cap_and_waiting_button_has_no_purchase(self):
        result = self.selected_case(action="together")
        self.assertIsNone(result["selection"])
        context = self.read()["partial_intention"]
        self.assertFalse(context["can_prepare_selected"])
        self.assertEqual(context["offer"]["choice"]["action"], "together")
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_actual_selected_prepare_is_scope_bound_and_exact_operation_replays_original_checkout(self):
        result = self.selected_case()
        projection = self.read()["partial_intention"]
        self.assertTrue(projection["can_prepare_selected"], projection)
        offer = projection["offer"]
        payload = {"action": "prepare_selected", "operation_id": str(uuid.uuid4()),
            "request_id": str(result["request"].pk), "decision_id": str(result["decision"].pk),
            "expected_request_digest": result["request"].envelope_hmac,
            "expected_decision_digest": result["decision"].envelope_hmac,
            "expected_coverage_digest": projection["coverage_digest"], "expected_terms_digest": projection["terms_digest"],
            "task_id": result["task"].pk, "expected_choice_digest": offer["choice"]["choice_digest"]}
        with patch("management.services.instagram_bot._provider_http") as provider:
            response = self.post(payload)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["checkout"]["ready"])
            proposal = IgCheckoutProposal.objects.get(pk=response.json()["checkout"]["proposal_id"])
            self.assertEqual(list(proposal.items.values_list("product_title", "quantity")), [("Фіолетове худі", 1)])
            replay = self.post(payload)
            self.assertEqual(replay.status_code, 200, replay.content)
            self.assertTrue(replay.json()["checkout"]["replayed"])
            self.assertEqual(response.json()["checkout"]["checkout_url"], replay.json()["checkout"]["checkout_url"])
            self.assertEqual(IgCheckoutProposal.objects.count(), 1)
            changed = {**payload, "expected_choice_digest": "0" * 64}
            self.assertEqual(self.post(changed).status_code, 409)
            provider.assert_not_called()

    def test_new_approved_terms_return_current_context_and_require_a_new_native_choice(self):
        from management.ig_assisted_commerce_models import IgAssistedCommerceDecision as Decision
        old = self.selected_case()
        context = self.read()
        items = context["editable_items"]
        items[0]["unit_price"] = "1670.00"
        terms = context["partial_intention"]["shipping_terms"]
        terms["now"]["customer_maximum"] = "135.00"
        with patch("management.services.instagram_bot._provider_http") as provider:
            drafted = self.post({"action": "draft", "operation_id": str(uuid.uuid4()),
                "request_id": str(old["request"].pk), **context["cas"], "items": items, "shipping_terms": terms})
            self.assertEqual(drafted.status_code, 200, drafted.content)
            approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()),
                "request_id": str(old["request"].pk), **drafted.json()["commerce"]["cas"]})
            self.assertEqual(approved.status_code, 200, approved.content)
            current = approved.json()["commerce"]["partial_intention"]
            decision = Decision.objects.get(pk=approved.json()["decision_id"])
            self.assertNotEqual(decision.pk, old["decision"].pk)
            self.assertEqual(current["decision_id"], str(decision.pk))
            self.assertEqual(current["reason"], "choice_required")
            self.assertFalse(current["can_prepare_selected"])
            self.assertTrue(current["can_issue"], current)
            self.assertIsNone(current["offer"])
            self.assertEqual(current["offer_history"][0]["decision_id"], str(old["decision"].pk))
            old_choice = partial.consume_card_choice(old["source"], old["revision"], self.settings, now=self.now)
            self.assertFalse(old_choice["ready"], old_choice)
            self.assertIsNone(old_choice.get("selection"))
            issued = self.post({"action": "issue_partial_offer", "operation_id": str(uuid.uuid4()),
                "request_id": str(old["request"].pk), "decision_id": str(decision.pk),
                "expected_request_digest": old["request"].envelope_hmac,
                "expected_decision_digest": decision.envelope_hmac,
                "expected_coverage_digest": current["coverage_digest"], "expected_terms_digest": current["terms_digest"]})
            self.assertEqual(issued.status_code, 200, issued.content)
            new_task = IgFollowUpTask.objects.get(pk=issued.json()["offer"]["task_id"])
            self.assertNotEqual(new_task.pk, old["task"].pk)
            self.assertFalse(IgCheckoutProposal.objects.exists())
            provider.assert_not_called()
        self.assertEqual(self.send_card(new_task)[0], 1)
        card = partial.card_for_task(new_task, self.settings, now=self.now)
        self.assertTrue(card["ready"], card)
        button = next(button for button in card["buttons"] if button.payload == card["actions"]["partial"])
        self.now += timedelta(seconds=1)
        source = self.source(button.title, payload=button.payload, at=self.now)
        source.reply_to_provider_message_id = new_task.provider_message_id
        source.save(update_fields=["reply_to_provider_message_id"])
        revision, _token = self.revision(source, now=self.now)
        chosen = partial.consume_card_choice(source, revision, self.settings, now=self.now)
        self.assertTrue(chosen["ready"], chosen)
        self.assertEqual(chosen["selection"].decision_id, decision.pk)
        self.assertNotEqual(chosen["selection"].terms_digest, old["selection"].terms_digest)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_delivered_and_paid_card_copy_survives_future_template_changes(self):
        from management.services.ig_assisted_checkout import validate_assisted_order, validate_assisted_execution_history
        from management.services.ig_assisted_partial_delivery import capture_delivered_card
        from management.services.ig_assisted_scopes import intention_coverage
        data = self.selected_case()
        original = partial.card_for_task(data["task"], require_current=False)
        self.assertTrue(original["ready"], original)
        prepared = self.prepare_selection(data["selection"])
        order = self.pay_prepared(prepared)
        execution = partial.core._models()[2].objects.get(order=order)
        native_copy = partial._verified("partial_card", data["task"].manager_context[partial.CARD_KEY])
        self.assertEqual(native_copy["rendered_copy"]["text"], original["text"])
        self.assertEqual([row["title"] for row in native_copy["rendered_copy"]["buttons"]],
            [button.title for button in original["buttons"]])
        locale = original["proof"]["locale"]
        replacement = {"heading": "New future card wording", "partial": "Order now", "wait": "Wait for all",
            "continuation": "Future remaining-item invitation", "resume": "Continue"}
        with patch.dict(partial.COPY[locale], replacement):
            after = partial.card_for_task(data["task"], require_current=False)
            self.assertTrue(after["ready"], after)
            self.assertEqual((after["text"], after["buttons"], after["proof"]),
                (original["text"], original["buttons"], original["proof"]))
            receipt, reason = capture_delivered_card(data["task"], require_current=False)
            self.assertIsNotNone(receipt, reason)
            self.assertTrue(validate_assisted_order(order)["ready"])
            self.assertTrue(validate_assisted_execution_history(execution)["ready"])
            coverage = intention_coverage(self.client_row.pk, data["request"].intention_id, now=self.now)
            future, created = partial.issue_continuation_offer(self.client_row.pk, actor=self.manager,
                request_id=data["request"].pk, expected_request_digest=data["request"].envelope_hmac,
                expected_coverage_digest=coverage["coverage_digest"], operation_id=uuid.uuid4(), now=self.now)
            self.assertTrue(created)
            new = partial.card_for_task(future, self.settings, now=self.now)
            self.assertTrue(new["ready"], new)
            self.assertIn(replacement["continuation"], new["text"])
            self.assertEqual(new["buttons"][0].title, replacement["resume"])
            self.assertNotEqual(new["proof"]["card_digest"], original["proof"]["card_digest"])

    def test_unknown_delivery_cannot_be_reissued_or_accepted(self):
        result = self.offer_case()
        count, _physical = self.send_card(result["task"], outcome="unknown")
        self.assertEqual(count, 0)
        self.assertEqual(result["task"].status, "ambiguous")
        retry = {**result["offer_operation"], "operation_id": str(uuid.uuid4())}
        self.assertEqual(self.post(retry).status_code, 409)
        self.assertFalse(self.read()["partial_intention"]["can_prepare_selected"])
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_same_intention_unknown_delivery_blocks_a_fresh_approved_decision_but_old_operation_is_readonly(self):
        result = self.offer_case()
        self.send_card(result["task"], outcome="unknown")
        context = self.read()
        terms = context["partial_intention"]["shipping_terms"]
        terms["now"]["customer_maximum"] = "121.00"
        draft = self.post({"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(result["request"].pk),
            **context["cas"], "items": context["editable_items"], "shipping_terms": terms})
        self.assertEqual(draft.status_code, 200, draft.content)
        approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(result["request"].pk),
            **draft.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        projection = approved.json()["commerce"]["partial_intention"]
        self.assertFalse(projection["can_issue"])
        new = {**result["offer_operation"], "operation_id": str(uuid.uuid4()), "decision_id": approved.json()["decision_id"],
            "expected_decision_digest": projection["decision_digest"], "expected_terms_digest": projection["terms_digest"]}
        with patch("management.services.instagram_bot._provider_http") as provider:
            denied = self.post(new)
            self.assertEqual(denied.status_code, 409, denied.content)
            self.assertEqual(denied.json()["reason"], "provider_outcome_unknown_no_retry")
            replay = self.post(result["offer_operation"])
            self.assertEqual(replay.status_code, 200, replay.content)
            self.assertFalse(replay.json()["created"])
            self.assertEqual(replay.json()["offer"]["delivery"]["status"], "ambiguous")
            self.assertEqual(IgFollowUpTask.objects.filter(reason=partial.PARTIAL_REASON).count(), 1)
            provider.assert_not_called()

    def test_shipping_review_action_checks_exact_native_order_and_replays_without_another_carrier_request(self):
        from orders.nova_poshta_service import NovaPoshtaService
        from management.models import IgClient
        from management.services import ig_assisted_partial_shipping as shipping
        data = self.selected_case()
        prepared = self.prepare_selection(data["selection"])
        order = self.pay_prepared(prepared)
        reference = str(uuid.uuid4())
        order = shipping.preserve_assisted_waybill_failure(order.pk, created={"document_ref": reference,
            "tracking_number": "20450000000001", "recipient_ref": str(uuid.uuid4()),
            "recipient_contact_ref": str(uuid.uuid4())}, provider_cancelled=False)
        contract = shipping.assisted_shipping_contract(order)
        payload = {"action": "resolve_shipping_review", "operation_id": str(uuid.uuid4()), "order_id": order.pk,
            "expected_document_ref": reference, "expected_terms_digest": contract["terms_digest"]}
        with patch.object(NovaPoshtaService, "get_tracking_info", return_value={"Number": "20450000000001",
                "Ref": reference, "Cost": "150.00", "PayerType": "Sender"}) as carrier:
            foreign = IgClient.objects.create(igsid="partial-review-foreign")
            response = self.client.post(self.endpoint(foreign), json.dumps(payload), content_type="application/json")
            self.assertEqual(response.status_code, 409)
            carrier.assert_not_called()
            response = self.post(payload)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["shipping_review"]["ready"])
            carrier.assert_called_once()
            replay = self.post(payload)
            self.assertEqual(replay.status_code, 200, replay.content)
            self.assertTrue(replay.json()["shipping_review"]["replayed"])
            carrier.assert_called_once()
            changed = {**payload, "expected_document_ref": str(uuid.uuid4())}
            self.assertEqual(self.post(changed).status_code, 409)
            carrier.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(shipping.assisted_shipping_contract(order), contract)
        self.assertEqual(shipping.assert_assisted_shipping_allowed(order)["customer_charge"], "0.00")

    def test_actual_permissions_csrf_and_privacy_deny_card_actions(self):
        result = self.offer_case()
        viewer = _operator("partial-view-only", VIEW_IG_CONVERSATION_PII_PERMISSION)
        reviewer = _operator("partial-reviewer", OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        reviewer.groups.add(Group.objects.create(name=META_REVIEWER_GROUP_NAME))
        for actor in (viewer, reviewer):
            self.client.force_login(actor)
            self.assertEqual(self.post(result["offer_operation"]).status_code, 403)
        strict = Client(enforce_csrf_checks=True, HTTP_HOST="management.twocomms.shop")
        strict.force_login(self.manager)
        self.assertEqual(strict.post(self.endpoint(), json.dumps(result["offer_operation"]), content_type="application/json").status_code, 403)
        self.client.force_login(self.manager)
        self.client_row.privacy_erasure_started_at = self.now
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.post(result["offer_operation"]).status_code, 410)
