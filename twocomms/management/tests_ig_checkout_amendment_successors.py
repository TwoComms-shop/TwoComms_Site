"""Actual hosted-holder edits -> fresh approval -> new immutable checkout."""
from copy import deepcopy
from decimal import Decimal
import json
import uuid
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from management.models import (IgCheckoutAccessToken, IgCheckoutCartAmendment,
    IgCheckoutCartAmendmentResolution, IgCheckoutInvoiceGeneration)
from management.services import ig_assisted_commerce as commerce
from management.services import ig_checkout_amendment_successors as successors
from management.services.ig_checkout_cart_amendments import checkout_cart_state
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture
from orders.models import Order, PaymentAttempt


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeCheckoutAmendmentSuccessorTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def pending(self, *, mixed=False, photo=False, operation="quantity"):
        original = self._custom_proposal(mixed=mixed, photo=photo)
        proposal = original["proposal"]
        frozen = {"request": deepcopy(original["request"].envelope), "decision": deepcopy(original["decision"].envelope),
            "execution": deepcopy(original["execution"].envelope), "revision": proposal.revision,
            "digest": proposal.items_digest, "items": list(proposal.items.order_by("pk").values()),
            "quote": deepcopy(proposal.revisions.get(revision=proposal.revision).snapshot)}
        _response, token = self._open(proposal)
        state = checkout_cart_state(proposal, token)
        item = state["items"][-1]
        payload = {"action": "cart_amendment", "operation_id": str(uuid.uuid4()), "operation": operation,
            "line_id": item["line_id"], "quantity": 2 if operation == "quantity" else "", "expected_revision": state["revision"],
            "expected_digest": state["digest"], "expected_selection_revision": state["selection_revision"]}
        with patch("storefront.views.monobank._monobank_api_request") as bank, \
                patch("management.services.instagram_bot._provider_http") as provider:
            response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                json.dumps(payload), content_type="application/json", HTTP_ACCEPT="application/json")
        bank.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "cancelled" if operation == "remove" and not mixed else "manager_review")
        amendment = IgCheckoutCartAmendment.objects.get(proposal=proposal)
        request = commerce._models()[0].objects.filter(
            envelope__snapshot__checkout_amendment__amendment_id=str(amendment.pk))
        return {**original, "amendment": amendment, "new_request": request.first(), "root_token": token, "frozen": frozen,
            "pending_state": response.json()["cart_state"] if "cart_state" in response.json() else response.json()}

    def approve(self, case):
        request = case["new_request"]
        items = commerce._editable_items(request.envelope["snapshot"])
        original_items = {row["line_id"]: row for row in case["decision"].envelope["snapshot"]["items"]}
        for row in items:
            old = original_items[row["line_id"]]
            row.update(availability="ready", checked_quantity=row["quantity"],
                supply_basis="accessible_unrecorded" if row["item_kind"] == "custom_apparel" else "warehouse_checked",
                source_location="Менеджер заново перевірив точну кількість")
            if row["item_kind"] == "custom_apparel":
                row.update(title=old["title"], color=old["color"], unit_price="1590.00",
                    image_id=old.get("image_id"), image_digest=old.get("image_digest", ""))
            row["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Перевірено нову кількість та конфігурацію."}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == row["line_id"]]
        terms = {phase: {"payer": "customer", "customer_maximum": "120.00", "currency": "UAH"}
            for phase in ("now", "later", "together")}
        draft, _ = self.record(request, items=items, shipping_terms=terms)
        decision, _ = self.record(request, status="approved", latest=draft)
        result = successors.prepare_amendment_successor(self.client_row.pk, actor=self.manager,
            request_id=request.pk, decision_id=decision.pk, expected_request_digest=request.envelope_hmac,
            expected_decision_digest=decision.envelope_hmac, operation_id=uuid.uuid4(), now=self.now)
        self.assertTrue(result["ready"], result)
        return {**result, "request": request, "decision": decision}

    def assert_old_immutable(self, case):
        for key in ("request", "decision", "execution"):
            case[key].refresh_from_db()
            self.assertEqual(case[key].envelope, case["frozen"][key])
        proposal = case["proposal"]
        proposal.refresh_from_db()
        self.assertEqual((proposal.revision, proposal.items_digest), (case["frozen"]["revision"], case["frozen"]["digest"]))
        self.assertEqual(list(proposal.items.order_by("pk").values()), case["frozen"]["items"])
        self.assertEqual(proposal.revisions.get(revision=proposal.revision).snapshot, case["frozen"]["quote"])

    def pending_child(self, prepared, *, quantity=3):
        from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
        proposal = prepared["proposal"]
        execution = IgAssistedCommerceExecution.objects.get(proposal=proposal)
        original = {**prepared, "request": execution.request, "decision": execution.decision, "execution": execution}
        frozen = {"request": deepcopy(execution.request.envelope), "decision": deepcopy(execution.decision.envelope),
            "execution": deepcopy(execution.envelope), "revision": proposal.revision,
            "digest": proposal.items_digest, "items": list(proposal.items.order_by("pk").values()),
            "quote": deepcopy(proposal.revisions.get(revision=proposal.revision).snapshot)}
        _response, token = self._open(proposal)
        state = checkout_cart_state(proposal, token)
        response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
            json.dumps({"action": "cart_amendment", "operation_id": str(uuid.uuid4()), "operation": "quantity",
                "line_id": state["items"][-1]["line_id"], "quantity": quantity, "expected_revision": state["revision"],
                "expected_digest": state["digest"], "expected_selection_revision": state["selection_revision"]}),
            content_type="application/json", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "manager_review")
        amendment = IgCheckoutCartAmendment.objects.get(proposal=proposal)
        request = commerce._models()[0].objects.get(envelope__snapshot__checkout_amendment__amendment_id=str(amendment.pk))
        return {**original, "amendment": amendment, "new_request": request, "root_token": token, "frozen": frozen}

    def test_saved_custom_wish_blocks_payment_without_inventing_source_price_or_new_order(self):
        case = self.pending()
        self.assert_old_immutable(case)
        self.assertNotEqual(case["new_request"].intention_id, case["request"].intention_id)
        state = checkout_cart_state(case["proposal"], case["root_token"])
        self.assertTrue(state["review_needed"])
        self.assertFalse(state["can_pay"])
        self.assertIsNone(state["total"])
        self.assertIsNone(state["charge_now"])
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())
        transition = case["amendment"].selection_transition
        self.assertIsNone(transition.source_message_id)
        self.assertEqual(case["new_request"].source_id, case["request"].source_id)

    def test_actual_fresh_manager_approval_creates_new_quote_and_resolves_original_owned_link(self):
        case = self.pending(photo=True)
        child = self.approve(case)
        self.assert_old_immutable(case)
        self.assertNotEqual(child["proposal_id"], case["proposal_id"])
        self.assertEqual(child["proposal"].quoted_total, Decimal("3180.00"))
        self.assertEqual(child["proposal"].items.get().quantity, 2)
        self.assertEqual(case["proposal"].status, "superseded")
        resolved = successors.resolve_amendment_successor(case["proposal"], case["root_token"], now=self.now)
        self.assertTrue(resolved["ready"])
        self.assertEqual(resolved["proposal"].pk, child["proposal_id"])
        self.assertEqual(resolved["token"].proposal_id, child["proposal_id"])
        self.assertEqual(IgCheckoutCartAmendmentResolution.objects.count(), 1)
        from management.services.ig_assisted_scopes import intention_coverage
        old_coverage = intention_coverage(self.client_row.pk, case["request"].intention_id, now=self.now)
        self.assertFalse(old_coverage["ready"])
        self.assertEqual(old_coverage["reason"], "intention_transferred_to_checkout_successor")
        self.assertFalse(Order.objects.exists())
        response = self.client.get(reverse("ig_checkout_proposal", kwargs={"proposal_id": case["proposal"].public_id}))
        self.assertIn(response.status_code, (200, 302))

    def test_mixed_successor_retains_catalog_line_and_reapproves_only_actual_new_custom_quantity(self):
        case = self.pending(mixed=True)
        child = self.approve(case)
        self.assert_old_immutable(case)
        self.assertEqual(list(child["proposal"].items.order_by("position").values_list("quantity", flat=True)), [1, 2])
        self.assertEqual(child["proposal"].quoted_total, Decimal("3970.00"))

    def test_remove_sole_custom_scope_records_cancellation_without_zero_order_or_new_request(self):
        from management.services.ig_assisted_scopes import intention_coverage
        case = self.pending(operation="remove")
        self.assertIsNone(case["new_request"])
        self.assert_old_immutable(case)
        state = checkout_cart_state(case["proposal"], case["root_token"])
        self.assertEqual(state["items"], [])
        self.assertEqual(state["total"], "0.00")
        self.assertFalse(state["can_pay"])
        self.assertFalse(state["review_needed"])
        coverage = intention_coverage(self.client_row.pk, case["execution"].intention_id, now=self.now)
        self.assertTrue(coverage["ready"], coverage)
        self.assertEqual(coverage["cancelled_line_ids"], case["execution"].envelope["snapshot"]["selected_line_ids"])
        self.assertEqual(coverage["deferred_line_ids"], [])
        self.assertFalse(Order.objects.exists())

    def test_later_owned_faq_does_not_replace_the_native_holder_wish_or_prevent_fresh_approval(self):
        case = self.pending()
        self.source("Як доглядати за худі?")
        child = self.approve(case)
        self.assertTrue(child["ready"])
        self.assert_old_immutable(case)

    @patch.dict("os.environ", {"MANAGEMENT_TG_BOT_TOKEN": "test-token", "MANAGEMENT_TG_ADMIN_CHAT_ID": "123"}, clear=False)
    def test_native_pending_wish_commits_one_manager_alert_and_existing_drain_delivers_exact_quantity(self):
        from management.models import IgBotNotification
        from management.services import instagram_bot as bot
        with patch("management.services.instagram_bot._http", return_value=(200,
                json.dumps({"ok": True, "result": {"message_id": 811}}))) as telegram:
            case = self.pending()
            telegram.assert_not_called()
            key = "checkout-amendment-review:" + str(case["amendment"].pk)
            row = IgBotNotification.objects.get(dedupe_key=key)
            self.assertEqual(row.status, "pending")
            self.assertEqual(row.client_id, self.client_row.pk)
            self.assertTrue(row.payload["requires_human_review"])
            self.assertIn("кількість 2 (раніше 1)", row.payload["text"])
            self.assertIn("нова сума ще не затверджена", row.payload["text"])
            request, created = successors.create_amendment_review_request(case["amendment"].pk,
                operation_id=uuid.uuid4(), now=self.now)
            self.assertFalse(created)
            self.assertEqual(request.pk, case["new_request"].pk)
            self.assertEqual(IgBotNotification.objects.filter(dedupe_key=key).count(), 1)
            bot.drain_manager_notifications(limit=20)
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")
        sent = [json.loads(call.kwargs["data"]) for call in telegram.call_args_list]
        self.assertTrue(any("кількість 2 (раніше 1)" in value.get("text", "") for value in sent))
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_actual_later_quantity_transition_invalidates_the_pending_native_wish(self):
        case = self.pending()
        self.canonical("количество 3")
        with self.assertRaises(commerce.AssistedDecisionError):
            successors.verify_amendment_request(case["new_request"], self.client_row, self.now)

    def test_mixed_remove_alert_records_explicit_removed_custom_row_without_claiming_a_new_price(self):
        from management.models import IgBotNotification
        case = self.pending(mixed=True, operation="remove")
        row = IgBotNotification.objects.get(dedupe_key="checkout-amendment-review:" + str(case["amendment"].pk))
        self.assertIn("Вилучено з оформлення: Фіолетове худі, кількість 1", row.payload["text"])
        self.assertIn("нова сума ще не затверджена", row.payload["text"])
        self.assertEqual(len(case["new_request"].envelope["snapshot"]["lines"]), 1)
        self.assert_old_immutable(case)

    def test_original_share_link_keeps_pay_only_scope_on_approved_child(self):
        case = self.pending()
        child = self.approve(case)
        _raw, share = IgCheckoutAccessToken.issue(proposal=case["proposal"], kind="share")
        resolved = successors.resolve_amendment_successor(case["proposal"], share, now=self.now)
        self.assertTrue(resolved["ready"])
        self.assertEqual((resolved["token"].kind, resolved["token"].proposal_id), ("share", child["proposal_id"]))
        self.assertLessEqual(resolved["token"].expires_at, share.expires_at)
        self.assertFalse(checkout_cart_state(child["proposal"], resolved["token"])["can_edit"])

    def test_original_owned_link_resolves_two_approved_successors_and_share_remains_pay_only(self):
        original = self.pending()
        first = self.approve(original)
        second_case = self.pending_child(first)
        second = self.approve(second_case)
        self.assert_old_immutable(original)
        self.assert_old_immutable(second_case)
        result = successors.resolve_amendment_successor(original["proposal"], original["root_token"], now=self.now)
        self.assertEqual((result["proposal"].pk, len(result["proof"]["chain"])), (second["proposal_id"], 2))
        self.assertEqual(second["proposal"].quoted_total, Decimal("4770.00"))
        _raw, share = IgCheckoutAccessToken.issue(proposal=original["proposal"], kind="share")
        shared = successors.resolve_amendment_successor(original["proposal"], share, now=self.now)
        self.assertEqual((shared["proposal"].pk, shared["token"].kind), (second["proposal_id"], "share"))
        self.assertLessEqual(shared["token"].expires_at, share.expires_at)
        self.assertFalse(checkout_cart_state(shared["proposal"], shared["token"])["can_edit"])

    def test_original_link_shows_latest_pending_wish_instead_of_the_prior_payable_child(self):
        original = self.pending()
        first = self.approve(original)
        pending = self.pending_child(first)
        result = successors.resolve_amendment_successor(original["proposal"], original["root_token"], now=self.now)
        self.assertEqual(result["proposal"].pk, pending["proposal"].pk)
        self.assertTrue(result["proof"]["pending_review"])
        state = checkout_cart_state(result["proposal"], result["token"])
        self.assertTrue(state["review_needed"])
        self.assertIsNone(state["total"])
        self.assertIsNone(state["charge_now"])
        self.assertFalse(state["can_pay"])

    def test_root_link_resolution_does_not_survive_a_real_client_permission_epoch_change(self):
        original = self.pending()
        self.approve(original)
        self.client_row.reply_permission_epoch += 1
        self.client_row.save(update_fields=["reply_permission_epoch"])
        with self.assertRaises(commerce.AssistedDecisionError):
            successors.resolve_amendment_successor(original["proposal"], original["root_token"], now=self.now)

    def test_absent_native_head_uses_existing_owner_read_without_an_absence_query_or_digest_change(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from management.services import ig_admin_state_capture, ig_checkout_cart_amendments, ig_commerce_projection
        self._custom_proposal()
        original = ig_admin_state_capture._owner_fence(self.client_row.pk)
        metadata = {}
        annotated = ig_admin_state_capture._owner_fence(self.client_row.pk, _native_head_out=metadata)
        self.assertEqual(annotated, original)
        self.assertFalse(metadata["has_native_head"])
        self.assertNotIn("_native_checkout_head", annotated["session"])
        with CaptureQueriesContext(connection) as queries, patch.object(ig_checkout_cart_amendments,
                "native_capture_for", wraps=ig_checkout_cart_amendments.native_capture_for) as native:
            captured = ig_commerce_projection.capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(captured["status"], "captured", captured)
        native.assert_not_called()
        self.assertLessEqual(sum(row["sql"].lstrip().upper().startswith("SELECT") for row in queries), 64)
        with patch.object(ig_checkout_cart_amendments, "native_capture_for",
                wraps=ig_checkout_cart_amendments.native_capture_for) as native:
            supplied = ig_commerce_projection.capture_current_selection_lines(self.client_row.pk, now=self.now,
                _owner_snapshot=original)
        self.assertEqual(supplied["status"], "captured", supplied)
        native.assert_called_once()

    def test_real_native_head_still_runs_its_signed_source_and_owner_revalidator(self):
        from management.services import ig_admin_state_capture, ig_checkout_cart_amendments, ig_commerce_projection
        case = self.pending()
        original = ig_admin_state_capture._owner_fence(self.client_row.pk)
        metadata = {}
        self.assertEqual(ig_admin_state_capture._owner_fence(self.client_row.pk, _native_head_out=metadata), original)
        self.assertTrue(metadata["has_native_head"])
        with patch.object(ig_checkout_cart_amendments, "native_capture_for",
                wraps=ig_checkout_cart_amendments.native_capture_for) as native:
            captured = ig_commerce_projection.capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(captured["status"], "captured", captured)
        self.assertEqual(captured["fence"]["native_source_hmac"], case["amendment"].source_hmac)
        native.assert_called_once()

    def test_actual_native_head_change_between_owner_read_and_revalidation_cannot_fall_back_to_old_wish(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, get_ident
        from django.db import connection, close_old_connections
        from management.services import ig_admin_state_capture, ig_commerce_projection
        if connection.vendor != "mysql":
            self.skipTest("Concurrent native-head ownership requires the real MariaDB fixture")
        self.pending()
        observed, changed = Event(), Event()
        reader_thread = get_ident()
        actual_owner = ig_admin_state_capture._owner_fence
        def after_actual_owner(client_id, **kwargs):
            value = actual_owner(client_id, **kwargs)
            metadata = kwargs.get("_native_head_out")
            if get_ident() == reader_thread and metadata is not None and metadata.get("has_native_head"):
                observed.set()
                if not changed.wait(10):
                    raise AssertionError("Actual source writer did not complete the bounded race")
            return value
        def actual_source_write():
            close_old_connections()
            try:
                if not observed.wait(10):
                    raise AssertionError("Actual owner read did not reach the bounded race")
                self.canonical("количество 3")
            finally:
                changed.set()
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as worker, patch.object(ig_admin_state_capture, "_owner_fence",
                side_effect=after_actual_owner):
            future = worker.submit(actual_source_write)
            captured = ig_commerce_projection.capture_current_selection_lines(self.client_row.pk, now=self.now)
            future.result(timeout=15)
        self.assertEqual(captured["status"], "unavailable", captured)
        self.assertEqual(captured["reason"], "native_selection_head_changed")
        self.assertFalse(captured["coverage_complete"])

    def test_same_basket_price_change_never_uses_old_acknowledgement_for_bank(self):
        case = self.pending()
        child = self.approve(case)
        self._open(child["proposal"])
        payload = self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="")
        with patch("storefront.views.monobank._monobank_api_request") as bank:
            response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": child["proposal"].public_id}), payload,
                HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_approved_native_successor_remains_payable_after_literal_neutral_faq_with_fresh_hosted_ack(self):
        case = self.pending()
        child = self.approve(case)
        self.source("Як доглядати за худі?")
        response, bank = self._invoice_custom(child)
        self.assertEqual(response.status_code, 200, response.content)
        bank.assert_called_once()
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertFalse(Order.objects.exists())

    def test_real_quantity_change_after_successor_approval_denies_bank_even_with_positive_hosted_ack(self):
        case = self.pending()
        child = self.approve(case)
        self.canonical("количество 3")
        response, bank = self._invoice_custom(child)
        self.assertIn(response.status_code, (400, 409), response.content)
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_stale_original_page_acknowledgement_returns_successor_state_before_bank(self):
        case = self.pending()
        self.approve(case)
        payload = self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="1")
        payload.update(expected_revision=case["frozen"]["revision"], expected_digest=case["frozen"]["digest"])
        with patch("storefront.views.monobank._monobank_api_request") as bank:
            response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": case["proposal"].public_id}),
                payload, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 409)
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_actual_successor_paid_order_survives_shared_deal_payment_and_later_source_change(self):
        from storefront.views.monobank import _apply_payment_attempt_status
        from management.services.ig_assisted_checkout import validate_assisted_order, validate_assisted_execution_history
        from django.db import transaction
        case = self.pending(photo=True)
        child = self.approve(case)
        response, bank = self._invoice_custom(child)
        self.assertEqual(response.status_code, 200, response.content)
        bank.assert_called_once()
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=child["proposal"])
        with patch("management.services.instagram_bot._provider_http", return_value=(200,
                json.dumps({"message_id": "native-amendment-paid-confirmation"}))), transaction.atomic():
            order, created = _apply_payment_attempt_status(generation.payment_attempt, "success",
                payload={"paidAmount": int(generation.payment_attempt.payment_amount * 100)}, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        self.assertEqual((order.items.get().qty, order.total_sum), (2, Decimal("3180.00")))
        self.assert_old_immutable(case)
        self.assertTrue(validate_assisted_order(order, now=self.now)["ready"])
        self.assertTrue(validate_assisted_execution_history(case["execution"], now=self.now)["ready"])
        self.source("Наступне окреме запитання")
        self.assertTrue(validate_assisted_order(order, now=self.now)["ready"])


from management.tests_ig_assisted_partial_sources import PartialCommerceFixture


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativePartialCheckoutAmendmentCancellationTests(PartialCommerceFixture, TransactionTestCase):
    def _open(self, proposal):
        from management.services.ig_assisted_checkout import validate_assisted_proposal
        from management.tests_ig_checkout_generation_runtime import displayed_revision_payload
        checked = validate_assisted_proposal(proposal, now=self.now, fresh=True)
        self.assertTrue(checked["ready"], checked)
        raw, token = IgCheckoutAccessToken.issue(proposal=proposal)
        response = self.client.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}), HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 302, response.content)
        page = self.client.get(response["Location"], HTTP_HOST="twocomms.shop")
        self.assertEqual(page.status_code, 200, page.content)
        self._hosted_revision_payload = displayed_revision_payload(page)
        return response, token

    def cancelled_wait_case(self):
        with override_settings(ROOT_URLCONF="twocomms.urls_management"):
            case = self.selected_case()
        prepared = self.prepare_selection(case["selection"])
        proposal = prepared["proposal"]
        _response, token = self._open(proposal)
        state = checkout_cart_state(proposal, token)
        response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
            json.dumps({"action": "cart_amendment", "operation_id": str(uuid.uuid4()), "operation": "remove",
                "line_id": state["items"][0]["line_id"], "quantity": "", "expected_revision": state["revision"],
                "expected_digest": state["digest"], "expected_selection_revision": state["selection_revision"]}),
            content_type="application/json", HTTP_ACCEPT="application/json", HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        receipt = IgCheckoutCartAmendment.objects.get(proposal=proposal)
        return {**case, "prepared": prepared, "proposal": proposal, "amendment": receipt, "root_token": token}

    def test_native_cancelled_selected_scope_can_reapprove_only_wait_then_fresh_hosted_ack_pays_new_order(self):
        case = self.cancelled_wait_case()
        old_request, old_decision = deepcopy(case["request"].envelope), deepcopy(case["decision"].envelope)
        old_quote = deepcopy(case["proposal"].revisions.get(revision=case["proposal"].revision).snapshot)
        with override_settings(ROOT_URLCONF="twocomms.urls_management"):
            request, _payload, context = self.produced_request()
        self.assertNotEqual(request.intention_id, case["request"].intention_id)
        manifest = request.envelope["snapshot"]["checkout_amendment"]
        self.assertEqual(manifest["cancelled_selected_line_ids"], list(case["selection"].selected_line_ids))
        self.assertEqual(manifest["remaining_deferred_line_ids"], list(case["selection"].deferred_line_ids))
        self.assertEqual(manifest["delivery_scope"], "later")
        self.assertEqual(len(context["editable_items"]), 1)
        items = context["editable_items"]
        items[0].update(title="Оливкове худі", unit_price="1490.00", availability="ready",
            checked_quantity=items[0]["quantity"], supply_basis="accessible_unrecorded", source_location="Нова фізична перевірка WAIT")
        items[0]["question_answers"] = [{"question_id": row["question_id"], "state": "confirmed", "answer": "Перевірено заново."}
            for row in request.envelope["snapshot"]["questions"] if row["line_id"] == items[0]["line_id"]]
        terms = {phase: {"payer": "customer", "customer_maximum": "150.00", "currency": "UAH"}
            for phase in ("now", "later", "together")}
        with override_settings(ROOT_URLCONF="twocomms.urls_management"):
            drafted = self.post({"action": "draft", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
                **context["cas"], "items": items, "shipping_terms": terms})
            self.assertEqual(drafted.status_code, 200, drafted.content)
            approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()), "request_id": str(request.pk),
                **drafted.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        decision = commerce._models()[1].objects.get(pk=approved.json()["decision_id"])
        prepared = successors.prepare_amendment_successor(self.client_row.pk, actor=self.manager, request_id=request.pk,
            decision_id=decision.pk, expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac,
            operation_id=uuid.uuid4(), now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        self.assertEqual((prepared["proposal"].items.get().product_title, prepared["proposal"].quoted_total),
            ("Оливкове худі", Decimal("1490.00")))
        result = successors.resolve_amendment_successor(case["proposal"], case["root_token"], now=self.now)
        self.assertEqual(result["proposal"].pk, prepared["proposal_id"])
        self.assertFalse(Order.objects.exists())
        order = self.pay_prepared(prepared)
        self.assertEqual((order.items.get().qty, order.total_sum), (1, Decimal("1490.00")))
        case["request"].refresh_from_db()
        case["decision"].refresh_from_db()
        self.assertEqual(case["request"].envelope, old_request)
        self.assertEqual(case["decision"].envelope, old_decision)
        self.assertEqual(case["proposal"].revisions.get(revision=case["proposal"].revision).snapshot, old_quote)

    def test_cancellation_receipt_cannot_authorize_wait_when_origin_status_changed(self):
        from management.services.ig_checkout_cart_amendments import verify_cancelled_amendment
        case = self.cancelled_wait_case()
        case["proposal"].status = "manager_review"
        case["proposal"].save(update_fields=["status", "updated_at"])
        self.assertFalse(verify_cancelled_amendment(case["amendment"].pk, now=self.now)["ready"])
        with self.assertRaises(commerce.AssistedDecisionError):
            successors.create_amendment_review_request(case["amendment"].pk, operation_id=uuid.uuid4(), now=self.now)

    def test_removing_all_hosted_ready_rows_cancels_only_selected_units_and_preserves_wait(self):
        from management.services.ig_assisted_scopes import intention_coverage
        from management.ig_assisted_commerce_models import IgAssistedCommerceExecution
        with override_settings(ROOT_URLCONF="twocomms.urls_management"):
            case = self.selected_case()
        prepared = self.prepare_selection(case["selection"])
        proposal = prepared["proposal"]
        original_items = list(proposal.items.order_by("pk").values())
        original_revision, original_digest = proposal.revision, proposal.items_digest
        _response, token = self._open(proposal)
        state = checkout_cart_state(proposal, token)
        payload = {"action": "cart_amendment", "operation_id": str(uuid.uuid4()), "operation": "remove",
            "line_id": state["items"][0]["line_id"], "quantity": "", "expected_revision": state["revision"],
            "expected_digest": state["digest"], "expected_selection_revision": state["selection_revision"]}
        response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
            json.dumps(payload), content_type="application/json", HTTP_ACCEPT="application/json", HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 200, response.content)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, "cancelled")
        self.assertEqual((proposal.revision, proposal.items_digest), (original_revision, original_digest))
        self.assertEqual(list(proposal.items.order_by("pk").values()), original_items)
        execution = IgAssistedCommerceExecution.objects.get(proposal=proposal)
        coverage = intention_coverage(self.client_row.pk, execution.intention_id, now=self.now)
        self.assertTrue(coverage["ready"], coverage)
        self.assertEqual(coverage["cancelled_line_ids"], list(case["selection"].selected_line_ids))
        self.assertEqual(coverage["deferred_line_ids"], list(case["selection"].deferred_line_ids))
        self.assertFalse(Order.objects.exists())
