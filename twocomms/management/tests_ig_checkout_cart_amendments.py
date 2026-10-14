"""Native source, rollback and stale-page contracts for hosted basket editing."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import (IgCheckoutAccessToken, IgCheckoutCartAmendment, IgCheckoutRevision,
    IgCommerceSelectionTransition, IgCheckoutInvoiceGeneration)
from management.tests_ig_checkout_service import InstagramCheckoutConfigurationTests
from management.services.ig_checkout import create_or_update_proposal, CheckoutConfigurationError
from management.services.ig_checkout_cart_amendments import (amend_checkout_cart, checkout_cart_state,
    native_capture_for, verify_native_transition, CheckoutAmendmentError)
from management.services.ig_checkout_payment import require_checkout_revision, CheckoutPaymentError
from orders.models import PaymentAttempt


class NativeCheckoutCartAmendmentTests(TestCase):
    _valid_item = InstagramCheckoutConfigurationTests._valid_item

    def setUp(self):
        InstagramCheckoutConfigurationTests.setUp(self)
        self.proposal = create_or_update_proposal(client=self.client, pay_type="online_full",
            item_specs=[self._valid_item()])
        _raw, self.token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        self.grant = {"proposal_id": str(self.proposal.public_id), "token_id": self.token.pk,
            "grant_id": "native-test-grant", "revision": self.proposal.revision,
            "expires_at": int(self.proposal.expires_at.timestamp())}
        self.request = SimpleNamespace()

    def _payload(self, *, quantity=2, operation="quantity", operation_id=None):
        state = checkout_cart_state(self.proposal, self.token)
        return {"operation_id": str(operation_id or uuid.uuid4()), "operation": operation,
            "line_id": state["items"][0]["line_id"], "quantity": quantity if operation == "quantity" else "",
            "expected_revision": state["revision"], "expected_digest": state["digest"],
            "expected_selection_revision": state["selection_revision"]}

    def _amend(self, payload):
        result = amend_checkout_cart(self.proposal, request=self.request, grant=self.grant, payload=payload)
        self.proposal.refresh_from_db()
        return result

    def test_quantity_commits_one_native_fact_quote_and_all_canonical_totals_without_bank(self):
        previous = deepcopy(self.proposal.revisions.get(revision=1).snapshot)
        result = self._amend(self._payload())
        self.assertEqual(result["total"], "1900.00")
        self.assertEqual(self.proposal.deal.items.get().qty, 2)
        self.assertEqual(self.proposal.revision, 2)
        self.assertEqual(self.proposal.revisions.get(revision=1).snapshot, previous)
        row = IgCommerceSelectionTransition.objects.select_related("session", "checkout_amendment").get(checkout_amendment__isnull=False)
        self.assertIsNone(row.source_message_id)
        body = verify_native_transition(row)
        self.assertEqual(body["actor"], "checkout_link_holder")
        capture = native_capture_for(self.client.pk)
        self.assertEqual(capture["lines"][0]["fields"]["quantity"]["value"], 2)
        self.assertNotIn("source_message_id", capture["lines"][0]["fields"]["quantity"]["source"])
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())

    def test_same_operation_replay_does_not_create_second_transition_or_revision(self):
        payload = self._payload()
        self._amend(payload)
        replay = self._amend(payload)
        self.assertTrue(replay["replayed"])
        self.assertEqual(IgCheckoutCartAmendment.objects.count(), 1)
        self.assertEqual(self.proposal.revisions.count(), 2)

    def test_replay_a_after_b_preserves_current_revision_digest_and_selection_cas(self):
        payload_a = self._payload(quantity=2)
        self._amend(payload_a)
        result_b = self._amend(self._payload(quantity=3))
        replay_a = self._amend(payload_a)
        self.assertEqual(replay_a["revision"], result_b["revision"])
        self.assertEqual(replay_a["digest"], result_b["digest"])
        self.assertEqual(replay_a["selection_revision"], result_b["selection_revision"])
        self.assertEqual(replay_a["applied_revision"], 2)
        self.assertEqual(self._amend(self._payload(quantity=4))["total"], "3800.00")

    def test_stale_mutation_and_stale_invoice_cas_fail_before_effects(self):
        stale = self._payload()
        self._amend(self._payload(quantity=3))
        with self.assertRaises(CheckoutPaymentError):
            self._amend(stale)
        with self.assertRaises(CheckoutPaymentError):
            require_checkout_revision(self.proposal, stale)
        self.assertEqual(IgCheckoutCartAmendment.objects.count(), 1)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_share_grant_can_pay_but_cannot_change_customer_basket(self):
        _raw, share = IgCheckoutAccessToken.issue(proposal=self.proposal, kind="share")
        grant = {**self.grant, "token_id": share.pk}
        state = checkout_cart_state(self.proposal, share)
        self.assertFalse(state["can_edit"])
        self.assertTrue(state["can_pay"])
        with self.assertRaises(CheckoutAmendmentError):
            amend_checkout_cart(self.proposal, request=self.request, grant=grant, payload=self._payload())
        self.assertFalse(IgCheckoutCartAmendment.objects.exists())

    def test_expired_foreign_or_revoked_token_never_advertises_payability(self):
        self.token.expires_at = timezone.now() - timedelta(seconds=1)
        self.assertFalse(checkout_cart_state(self.proposal, self.token)["can_pay"])
        self.token.expires_at = timezone.now() + timedelta(minutes=1)
        self.token.revoked_at = timezone.now()
        self.assertFalse(checkout_cart_state(self.proposal, self.token)["can_pay"])

    def test_exhausted_stock_rolls_back_receipt_selection_and_all_economic_rows(self):
        before = deepcopy(self.proposal.revisions.get(revision=1).snapshot)
        with self.assertRaises(CheckoutConfigurationError):
            self._amend(self._payload(quantity=6))
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.revision, 1)
        self.assertEqual(self.proposal.revisions.get().snapshot, before)
        self.assertEqual(self.proposal.items.get().quantity, 1)
        self.assertEqual(self.proposal.deal.items.get().qty, 1)
        self.assertFalse(IgCheckoutCartAmendment.objects.exists())

    def test_revision_write_failure_rolls_back_native_transition_and_deal(self):
        with patch.object(IgCheckoutRevision.objects, "create", side_effect=RuntimeError("write failed")):
            with self.assertRaises(RuntimeError):
                self._amend(self._payload())
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.revision, 1)
        self.assertEqual(self.proposal.items.get().quantity, 1)
        self.assertEqual(self.proposal.deal.items.get().qty, 1)
        self.assertFalse(IgCheckoutCartAmendment.objects.exists())

    def test_last_removal_closes_payment_and_retains_original_revision(self):
        result = self._amend(self._payload(operation="remove"))
        self.assertEqual(result["total"], "0.00")
        self.assertFalse(result["can_pay"])
        self.assertEqual(self.proposal.status, "cancelled")
        self.assertEqual(self.proposal.revision, 1)
        self.assertEqual(self.proposal.items.get().quantity, 1)
        self.proposal.deal.refresh_from_db()
        self.proposal.commercial_episode.refresh_from_db()
        self.assertEqual(self.proposal.deal.amount, Decimal("0.00"))
        self.assertEqual(self.proposal.deal.status, "cancelled")
        self.assertEqual(self.proposal.commercial_episode.product_snapshot, [])
        self.assertEqual(self.proposal.revisions.get(revision=1).snapshot["quoted_total"], "950.00")
        self.assertEqual(native_capture_for(self.client.pk)["removed"][0]["disposition"], "cancelled_by_checkout_holder")

    def test_reusing_uuid_with_changed_quantity_is_conflict(self):
        payload = self._payload()
        self._amend(payload)
        with self.assertRaises(CheckoutAmendmentError):
            self._amend({**payload, "quantity": 3})
        self.assertEqual(IgCheckoutCartAmendment.objects.count(), 1)

    def test_native_body_result_and_transition_tampering_fail_closed(self):
        from management.services.ig_checkout_cart_amendments import _sign
        self._amend(self._payload())
        row = IgCommerceSelectionTransition.objects.select_related("session", "checkout_amendment").get(checkout_amendment__isnull=False)
        original_body = deepcopy(row.checkout_amendment.source_body)
        row.checkout_amendment.source_body["next_snapshot"]["lines"][0]["quantity"] = 5
        with self.assertRaises(CheckoutAmendmentError):
            verify_native_transition(row)
        row.checkout_amendment.source_body = original_body
        row.checkout_amendment.result = {**row.checkout_amendment.result, "operation_id": str(uuid.uuid4())}
        row.checkout_amendment.result_hmac = _sign("result", row.checkout_amendment.result)
        with self.assertRaises(CheckoutAmendmentError):
            verify_native_transition(row)

    def test_legacy_draft_different_color_fit_options_cannot_be_overwritten(self):
        from management.models import IgCommerceSelectionSession
        session = IgCommerceSelectionSession.objects.create(client=self.client,
            commercial_episode=self.proposal.commercial_episode, generation=1,
            lines=[{"line_id": "real-existing-line", "product_id": self.shirt.pk, "quantity": 1,
                "size": "M", "recipient_id": "self", "fit_option_code": "oversize",
                "color_variant_id": self.black.pk, "color": self.black.color.name, "option_values": {}}])
        with self.assertRaises(CheckoutAmendmentError):
            self._amend(self._payload())
        session.refresh_from_db()
        self.assertEqual(session.lines[0]["color_variant_id"], self.black.pk)
        self.assertFalse(IgCheckoutCartAmendment.objects.exists())

    def test_capture_detects_final_owner_or_session_race(self):
        from management.services import ig_admin_state_capture
        self._amend(self._payload())
        first = ig_admin_state_capture._owner_fence(self.client.pk)
        changed = deepcopy(first)
        changed["owner"]["current_commercial_episode_id"] = None
        with patch.object(ig_admin_state_capture, "_owner_fence", side_effect=[first, changed]):
            capture = native_capture_for(self.client.pk)
        self.assertEqual(capture["status"], "unavailable")
        self.assertEqual(capture["reason"], "hosted_capture_changed")

    def test_native_quantity_then_real_chat_color_survives_unified_capture(self):
        import os
        from django.db import transaction
        from dataclasses import replace
        from management.models import InstagramBotMessage, InstagramBotSettings
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.tests_ig_turn_capture import RevisionTurnCaptureTests
        with patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"}):
            settings_row, _ = InstagramBotSettings.objects.get_or_create(pk=1)
            settings_row.ig_user_id = "owner-1"
            settings_row.save(update_fields=["ig_user_id"])
            self._amend(self._payload())
            source = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
                provider_namespace="instagram_login:owner-1", role="user", source="webhook",
                text="Чорний колір", mid="actual-own-color-after-hosted-edit", provider_created_at=timezone.now())
            with transaction.atomic():
                decision = apply_turn(self.client, source, parse_turn(source.text), expected_revision=1)
            self.assertTrue(decision.accepted)
            capture = capture_current_selection_lines(self.client.pk)
            self.assertEqual(capture["status"], "captured", capture)
            self.assertEqual(capture["lines"][0]["fields"]["quantity"]["value"], 2)
            self.client_row = self.client
            self.now = timezone.now()
            self.settings_row = SimpleNamespace(reply_permission_epoch=0, turn_intelligence_mode="unified",
                gemini_routing_mode="adaptive", pinned_chat_model="", pinned_until=None)
            self.publication = {"id": 1, "version": 1, "hash": "a" * 64}
            revision, collection, boundary = RevisionTurnCaptureTests.seal(self, source)
            boundary.response_plan = replace(boundary.response_plan, source_cart_capture=capture)
            result = RevisionTurnCaptureTests.capture(self, revision, collection, boundary)
            self.assertTrue(result)

    def test_delayed_older_ig_quantity_keeps_newer_site_fact_and_source_text(self):
        from django.db import transaction
        from management.models import InstagramBotMessage
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        older_at = timezone.now() - timedelta(seconds=5)
        self._amend(self._payload(quantity=2))
        source = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            text="3 штуки", mid="delayed-own-quantity", provider_created_at=older_at)
        with transaction.atomic():
            first = apply_turn(self.client, source, parse_turn(source.text))
            second = apply_turn(self.client, source, parse_turn(source.text))
        self.assertEqual(first.pk, second.pk)
        self.assertTrue(first.is_stale)
        self.assertFalse(first.accepted)
        source.refresh_from_db()
        self.assertEqual(source.text, "3 штуки")
        capture = native_capture_for(self.client.pk)
        self.assertEqual(capture["lines"][0]["fields"]["quantity"]["value"], 2)

    def test_real_neutral_faq_keeps_native_payment_authority_but_actual_quantity_change_denies(self):
        import os
        from django.db import transaction
        from management.models import InstagramBotMessage, InstagramBotSettings
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_checkout_payment import _revalidate_frozen_proposal
        with patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"}):
            settings_row, _ = InstagramBotSettings.objects.get_or_create(pk=1)
            settings_row.ig_user_id = "owner-1"
            settings_row.save(update_fields=["ig_user_id"])
            self._amend(self._payload(quantity=2))
            source = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
                provider_namespace="instagram_login:owner-1", role="user", source="webhook",
                text="Які умови доставки?", mid="actual-neutral-faq-after-site", provider_created_at=timezone.now())
            with transaction.atomic():
                apply_turn(self.client, source, parse_turn(source.text))
                _revalidate_frozen_proposal(self.proposal)
            source.refresh_from_db()
            self.assertEqual(source.text, "Які умови доставки?")
            self.assertEqual(native_capture_for(self.client.pk)["lines"][0]["fields"]["quantity"]["value"], 2)
            changed = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
                provider_namespace="instagram_login:owner-1", role="user", source="webhook",
                text="3 штуки", mid="actual-quantity-after-site-faq", provider_created_at=timezone.now())
            with transaction.atomic():
                apply_turn(self.client, changed, parse_turn(changed.text))
                with self.assertRaises(CheckoutPaymentError):
                    _revalidate_frozen_proposal(self.proposal)

    def test_automatic_free_delivery_reprices_from_full_discounted_goods_and_records_loss(self):
        self.assertFalse(checkout_cart_state(self.proposal, self.token)["free_delivery"]["eligible"])
        achieved = self._amend(self._payload(quantity=4))
        self.assertTrue(achieved["free_delivery"]["eligible"])
        self.assertEqual(achieved["free_delivery"]["remaining"], "0.00")
        lost = self._amend(self._payload(quantity=3))
        self.assertFalse(lost["free_delivery"]["eligible"])
        self.assertEqual(lost["free_delivery"]["remaining"], "150.00")
        self.assertEqual(lost["free_delivery"]["payer_type"], "Recipient")
        self.assertTrue(lost["free_delivery"]["ack_required"])

    def test_lost_automatic_shipping_requires_exact_fresh_ack_before_attempt_and_freezes_contract(self):
        from management.services.ig_checkout_payment import _validate_payload, _snapshot, freeze_checkout_shipping
        self._amend(self._payload(quantity=4))
        current = self._amend(self._payload(quantity=3))
        payload = {"full_name": "Actual Customer", "phone": "+380501234567"}
        delivery = SimpleNamespace(city="Київ", np_office="Відділення 1", settlement_ref="s", city_ref="c",
            warehouse_ref="w", warehouse_kind="branch")
        with patch("management.services.ig_checkout_payment.resolve_delivery_selection", return_value=delivery):
            with self.assertRaises(CheckoutPaymentError) as rejected:
                _validate_payload(self.proposal, payload)
            self.assertEqual(rejected.exception.code, "delivery_policy_ack")
            self.assertFalse(PaymentAttempt.objects.exists())
            with self.assertRaises(CheckoutPaymentError):
                _validate_payload(self.proposal, {**payload, "delivery_policy_ack": "1", "delivery_policy_digest": "a" * 64})
            values = _validate_payload(self.proposal, {**payload, "delivery_policy_ack": "1",
                "delivery_policy_digest": current["free_delivery"]["ack_digest"]})
        attempt = PaymentAttempt.objects.create(fingerprint=uuid.uuid4().hex, cart_snapshot=_snapshot(self.proposal),
            gross_amount=self.proposal.catalog_total, discount_amount=values["promo_discount"],
            payable_amount=values["order_payable"], payment_amount=values["payment_amount"])
        freeze_checkout_shipping(attempt, self.proposal,
            shipping_ack=values["shipping_ack"], shipping_policy=values["shipping_policy"])
        attempt.refresh_from_db()
        contract = attempt.cart_snapshot["threshold_delivery_contract"]
        self.assertEqual(contract["shipping"]["basis_total"], "2850.00")
        self.assertEqual(contract["payer_type"], "Recipient")
        self.assertEqual(contract["shipping"]["owner"]["attempt_id"], attempt.pk)
        self.assertEqual(attempt.cart_snapshot["delivery_policy_ack"]["digest"], current["free_delivery"]["ack_digest"])
        self.proposal.payment_attempt = attempt
        self.proposal.status = self.proposal.Status.DETAILS_LOCKED
        self.proposal.details_locked_at = timezone.now()
        self.proposal.save(update_fields=["payment_attempt", "status", "details_locked_at", "updated_at"])
        with self.settings(FREE_SHIPPING_THRESHOLD="2000"):
            frozen_view = checkout_cart_state(self.proposal, self.token)
        self.assertEqual(frozen_view["free_delivery"]["threshold"], "3000.00")
        self.assertEqual(frozen_view["free_delivery"]["payer_type"], "Recipient")
        self.assertFalse(frozen_view["free_delivery"]["eligible"])

    def test_actual_get_then_threshold_drift_post_requires_new_visible_policy_without_bank(self):
        from django.test import Client
        from django.urls import reverse
        self._amend(self._payload(quantity=4))
        raw, _token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        browser = Client(HTTP_HOST="twocomms.shop")
        with self.settings(FREE_SHIPPING_THRESHOLD="3000"):
            entry = browser.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}))
            self.assertEqual(entry.status_code, 302)
            page = browser.get(entry["Location"])
            self.assertEqual(page.status_code, 200)
            shown = page.context["cart_state"]
            self.assertTrue(shown["free_delivery"]["eligible"])
        payload = {"full_name": "Actual Customer", "phone": "+380501234567",
            "expected_revision": shown["revision"], "expected_digest": shown["digest"],
            "delivery_policy_digest": shown["free_delivery"]["ack_digest"]}
        delivery = SimpleNamespace(city="Київ", np_office="Відділення 1", settlement_ref="s", city_ref="c",
            warehouse_ref="w", warehouse_kind="branch")
        with self.settings(FREE_SHIPPING_THRESHOLD="4000"), patch(
                "management.services.ig_checkout_payment.resolve_delivery_selection", return_value=delivery), patch(
                "storefront.views.monobank._monobank_api_request") as bank:
            response = browser.post(entry["Location"], payload, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        result = response.json()
        self.assertEqual(result["error"], "delivery_policy_changed")
        self.assertEqual(result["cart_state"]["free_delivery"]["payer_type"], "Recipient")
        self.assertTrue(result["cart_state"]["free_delivery"]["ack_required"])
        self.assertFalse(PaymentAttempt.objects.exists())
        bank.assert_not_called()

    @patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
    def test_legacy_repeated_native_quantity_changes_pay_only_current_revision_stock_and_order(self):
        from django.test import Client
        from django.urls import reverse
        from management.models import IgCheckoutInventoryReservation, InstagramBotSettings, InstagramBotMessage
        from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests, displayed_revision_payload
        from storefront.views.monobank import _apply_payment_attempt_status
        self.assertFalse(self.proposal.assisted_checkout_v2)
        settings_row, _ = InstagramBotSettings.objects.get_or_create(pk=1)
        settings_row.ig_user_id = "owner-1"
        settings_row.save(update_fields=["ig_user_id"])
        InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            text="Які умови доставки?", mid="actual-own-source-before-native-legacy-payment", provider_created_at=timezone.now())
        original_raw, original_token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        original_browser = Client(HTTP_HOST="twocomms.shop")
        original_entry = original_browser.get(reverse("ig_checkout_token_entry", kwargs={"token": original_raw}))
        self.assertEqual(original_entry.status_code, 302)
        original_page = original_browser.get(original_entry["Location"])
        self.assertEqual(original_page.status_code, 200)
        original_shown = displayed_revision_payload(original_page)
        self._amend(self._payload(quantity=4))
        self._amend(self._payload(quantity=3))
        self._amend(self._payload(quantity=4))
        self._amend(self._payload(quantity=3))
        change = IgCheckoutCartAmendment.objects.get(proposal=self.proposal, from_revision=2)
        self.assertEqual(change.source_body["previous_quote_lines"][0]["quantity"], 4)
        self.assertEqual(change.source_body["previous_quote_lines"][0]["title"], "Футболка Київ")
        self.assertEqual(change.source_body["requested_quote_lines"][0]["quantity"], 3)
        self.assertEqual(change.source_body["requested_quote_lines"][0]["title"], "Футболка Київ")
        original_holds = list(IgCheckoutInventoryReservation.objects.filter(proposal=self.proposal,
            state="released").values("pk", "state", "quantity", "release_reason"))
        raw, _token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        browser = Client(HTTP_HOST="twocomms.shop")
        entry = browser.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}))
        self.assertEqual(entry.status_code, 302)
        page = browser.get(entry["Location"])
        self.assertEqual(page.status_code, 200)
        shown = displayed_revision_payload(page)
        payload = CheckoutGenerationRuntimeTests._delivery_payload(SimpleNamespace(_hosted_revision_payload=shown),
            delivery_policy_ack="1")
        with patch("storefront.views.monobank._monobank_api_request", return_value={
                "invoiceId": "legacy-native-current-quote", "pageUrl": "https://pay.example/legacy-native-current-quote"}):
            invoiced = browser.post(entry["Location"], payload, HTTP_ACCEPT="application/json")
        self.assertEqual(invoiced.status_code, 200, invoiced.content)
        self.proposal.refresh_from_db()
        attempt = self.proposal.payment_attempt
        order, created = _apply_payment_attempt_status(attempt, "success",
            payload={"paidAmount": int(attempt.payment_amount * 100)}, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        self.proposal.refresh_from_db()
        self.proposal.deal.refresh_from_db()
        self.proposal.commercial_episode.refresh_from_db()
        self.assertEqual(self.proposal.status, "paid")
        self.assertEqual(self.proposal.deal.order_id, order.pk)
        self.assertEqual(self.proposal.commercial_episode.intended_order_id, order.pk)
        self.assertEqual(order.items.get().qty, 3)
        self.assertEqual(order.total_sum, Decimal("2850.00"))
        readback = original_browser.get(original_entry["Location"])
        self.assertEqual(readback.status_code, 200, readback.content)
        self.assertEqual(readback.context["cart_state"]["total"], "2850.00")
        self.assertEqual(readback.context["cart_state"]["paid_via_invoice"], "2850.00")
        self.assertEqual(readback.context["paid_via_invoice"], "2850.00")
        self.assertFalse(readback.context["cart_state"]["is_prepayment"])
        self.assertEqual(readback.context["state_title"], "Оплату отримано")
        self.assertFalse(readback.context["cart_state"]["can_edit"])
        self.assertFalse(readback.context["cart_state"]["can_pay"])
        stale_payload = CheckoutGenerationRuntimeTests._delivery_payload(SimpleNamespace(_hosted_revision_payload=original_shown))
        with patch("storefront.views.monobank._monobank_api_request") as no_new_bank:
            stale_invoice = original_browser.post(original_entry["Location"], stale_payload, HTTP_ACCEPT="application/json")
        self.assertEqual(stale_invoice.status_code, 409)
        self.assertEqual(stale_invoice.json()["error"], "revision_changed")
        no_new_bank.assert_not_called()
        from management.services.ig_admin_state_capture import current_admin_state
        admin_state = current_admin_state(self.client.pk).as_dict()
        self.assertEqual(admin_state["status"], "captured", admin_state)
        current_state = admin_state["state"]
        self.assertEqual(current_state["source_cart"]["scope"]["order_id"], order.pk)
        self.assertEqual(current_state["slots"]["order.completed"]["value"]["money"]["payable"], "2850.00")
        self.assertEqual(current_state["slots"]["order.completed"]["value"]["items"][0]["quantity"], 3)
        history = current_state["slots"]["order.completed"]["value"]["checkout_changes"]
        economic_change = next(row for row in history if row["amendment_id"] == str(change.pk))
        self.assertEqual(economic_change["before"][0]["quantity"], 4)
        self.assertEqual(economic_change["after"][0]["quantity"], 3)
        self.assertEqual(economic_change["before"][0]["title"], "Футболка Київ")
        self.assertEqual(economic_change["after"][0]["title"], "Футболка Київ")
        self.assertNotIn("source_message_id", current_state["source_cart"]["lines"][0]["fields"]["quantity"]["source"])
        self.blue.refresh_from_db()
        self.assertEqual(self.blue.stock, 2)
        self.assertEqual(list(IgCheckoutInventoryReservation.objects.filter(pk__in=[row["pk"] for row in original_holds])
            .values("pk", "state", "quantity", "release_reason")), original_holds)
        second_order, second_created = _apply_payment_attempt_status(attempt, "success",
            payload={"paidAmount": int(attempt.payment_amount * 100)}, source="provider_pull")
        self.assertFalse(second_created)
        self.assertEqual(second_order.pk, order.pk)
        self.blue.refresh_from_db()
        self.assertEqual(self.blue.stock, 2)

    def test_legacy_current_released_or_missing_hold_remains_review_without_using_old_revision(self):
        from management.models import IgCheckoutInventoryReservation, IgFollowUpTask
        from management.services.ig_inventory import (release_proposal_inventory,
            mark_overbooked_proposal_inventory, commit_proposal_inventory, InventoryReservationError)
        self._amend(self._payload(quantity=3))
        release_proposal_inventory(self.proposal, reason="actual_current_terminal_release")
        self.assertGreater(mark_overbooked_proposal_inventory(self.proposal), 0)
        self.blue.refresh_from_db()
        self.assertEqual(self.blue.stock, 5)
        with self.assertRaises(InventoryReservationError):
            commit_proposal_inventory(self.proposal)
        self.assertEqual(IgFollowUpTask.objects.get(event_key=f"inventory-overbooked:{self.proposal.pk}").reason,
            "inventory_overbooked_review")

    def test_missing_current_legacy_fingerprint_fails_safe_with_old_released_hold_present(self):
        from management.models import IgCheckoutInventoryReservation, IgFollowUpTask
        from management.services.ig_inventory import (_fingerprint,
            mark_overbooked_proposal_inventory, commit_proposal_inventory, InventoryReservationError)
        self._amend(self._payload(quantity=3))
        current = IgCheckoutInventoryReservation.objects.get(proposal=self.proposal, state="active")
        self.assertEqual(current.reservation_fingerprint, _fingerprint(self.proposal, current.allocation_key))
        # Local corruption fixture: the current reservation is absent while
        # genuine released prior-revision evidence remains in the same ledger.
        current.delete()
        self.assertTrue(IgCheckoutInventoryReservation.objects.filter(proposal=self.proposal, state="released").exists())
        self.assertGreater(mark_overbooked_proposal_inventory(self.proposal), 0)
        task = IgFollowUpTask.objects.get(event_key=f"inventory-overbooked:{self.proposal.pk}")
        self.assertEqual(task.event_payload["inventory_reason"], "inventory_current_revision_hold_missing")
        with self.assertRaises(InventoryReservationError):
            commit_proposal_inventory(self.proposal)
        self.blue.refresh_from_db()
        self.assertEqual(self.blue.stock, 5)

    @override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
    @patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
    def test_native_v2_200_receipt_keeps_full_goods_total_and_uses_actual_attempt_payment_type(self):
        from django.test import Client
        from django.urls import reverse
        from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
        from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests, displayed_revision_payload
        from storefront.views.monobank import _apply_payment_attempt_status
        settings_row, _ = InstagramBotSettings.objects.get_or_create(pk=1)
        settings_row.ig_user_id = "owner-1"
        settings_row.save(update_fields=["ig_user_id"])
        self.shirt.price = Decimal("3000.00")
        self.shirt.save(update_fields=["price"])
        buyer = IgClient.get_or_create_for_sender("native-bank-deposit-receipt-dto")
        source = InstagramBotMessage.objects.create(client=buyer, sender_id=buyer.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            text="Чи можна 200 грн передоплати, а решту післяплатою?", mid="actual-own-200-deposit-question",
            provider_created_at=timezone.now())
        proposal = create_or_update_proposal(client=buyer, pay_type="prepayment",
            requested_payment_amount=Decimal("200.00"), item_specs=[self._valid_item()],
            evidence={"message_ids": [source.pk]})
        self.assertEqual(proposal.payment_policy, proposal.PaymentPolicy.FULL_OR_200_COD)
        self.assertEqual(proposal.pay_type, "online_full")
        raw, token = IgCheckoutAccessToken.issue(proposal=proposal)
        browser = Client(HTTP_HOST="twocomms.shop")
        entry = browser.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}))
        self.assertEqual(entry.status_code, 302)
        page = browser.get(entry["Location"])
        self.assertEqual(page.status_code, 200)
        shown = displayed_revision_payload(page)
        payload = CheckoutGenerationRuntimeTests._delivery_payload(SimpleNamespace(_hosted_revision_payload=shown),
            payment_choice="prepay_200_cod")
        with patch("storefront.views.monobank._monobank_api_request", return_value={
                "invoiceId": "native-200-dto", "pageUrl": "https://pay.example/native-200-dto"}):
            invoiced = browser.post(entry["Location"], payload, HTTP_ACCEPT="application/json")
        self.assertEqual(invoiced.status_code, 200, invoiced.content)
        proposal.refresh_from_db()
        attempt = proposal.payment_attempt
        self.assertEqual(attempt.pay_type, "prepay_200")
        self.assertEqual(attempt.payment_amount, Decimal("200.00"))
        order, created = _apply_payment_attempt_status(attempt, "success", payload={"paidAmount": 20000}, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        paid_page = browser.get(entry["Location"])
        self.assertEqual(paid_page.status_code, 200)
        dto = paid_page.context["cart_state"]
        self.assertEqual(dto["total"], "3000.00")
        self.assertEqual(dto["charge_now"], "200.00")
        self.assertEqual(dto["paid_via_invoice"], "200.00")
        self.assertTrue(dto["is_prepayment"])
        self.assertTrue(paid_page.context["proposal"]["is_prepayment"])
        self.assertEqual(paid_page.context["paid_via_invoice"], "200.00")
        self.assertEqual(paid_page.context["state_title"], "Оплату отримано")
        self.assertFalse(dto["can_pay"])
