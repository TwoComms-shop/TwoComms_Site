"""Native manager API and captured source authority for split shipping."""
from copy import deepcopy
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest as Request, IgFulfillmentReviewDecision as Decision
from management.ig_split_shipping_models import IgSplitShippingOffer as Offer, IgSplitShippingChoice as Choice
from management.models import IgClient, IgCustomerTurn, IgTurnMessage, IgFollowUpTask
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_split_shipping as shipping
from management.services.ig_admin_state_capture import current_admin_state
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_fulfillment_context import (
    SLOT, fulfillment_slot, retained_fulfillment_frame, validate_retained_fulfillment,
)
from management.services.ig_revision_postbacks import apply_revision_postback
from management.tests_ig_restock_subscriptions import _RestockFixture
from management import tests_ig_split_shipping as shipping_tests
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=20000,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, ROOT_URLCONF="management.urls")
class NativeSplitApiContextTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    # Reuse only causal helpers; inheriting the suite would run its tests twice.
    manager_offer = shipping_tests.NativeSplitShippingTests.manager_offer
    options = shipping_tests.NativeSplitShippingTests.options
    offer = shipping_tests.NativeSplitShippingTests.offer
    complete_input = shipping_tests.NativeSplitShippingTests.complete_input
    send = shipping_tests.NativeSplitShippingTests.send
    answer = shipping_tests.NativeSplitShippingTests.answer
    sent_offer = shipping_tests.NativeSplitShippingTests.sent_offer
    no_commerce_effects = shipping_tests.NativeSplitShippingTests.no_commerce_effects

    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser(
            username="split-api-manager", email="split-api@example.test", password="test")
        self.canonical("количество 2")
        second, variant = self.products[1], self.variants[1]
        variant.stock = 0
        variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=second, source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        self.canonical("Хочу купити")
        self.client = Client(HTTP_HOST="management.twocomms.shop")
        self.client.force_login(self.manager)

    def endpoint(self, owner=None):
        return reverse("management_bot_split_shipping_api", args=[(owner or self.client_row).pk])

    def decision_without_legacy_hint(self):
        source = self.source("Це подарунок. Встигнете відправити до 20.10.2026?")
        revision, _token = self.revision(source)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(result["ready"], result)
        request = Request.objects.get(pk=result["request_id"])
        payload = {"request_id": str(request.pk), "expected_request_digest": request.envelope_hmac,
            "operation_id": str(uuid.uuid4()), "decisions_by_line": [{"line_id": line["line_id"],
                "availability": "available", "checked_quantity": line["target"]["quantity"],
                "supply_basis": "accessible_unrecorded"} for line in request.envelope["lines"]]}
        response = self.client.post(reverse("management_bot_fulfillment_review_api", args=[self.client_row.pk]),
            json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"], response.json())
        decision = Decision.objects.get(pk=response.json()["decision_id"])
        self.assertIsNone(decision.envelope["split_offer"])
        return request, decision

    def issue_payload(self, request, decision):
        return {"action": "issue_offer", "expected_decision_digest": decision.envelope_hmac,
            "operation_id": str(uuid.uuid4()), "options": self.options(request)}

    def capture(self, *, at):
        result = current_admin_state(self.client_row.pk, now=at)
        self.assertEqual(result.status, "captured", result)
        frame = retained_fulfillment_frame(result.state)
        self.assertIsNotNone(frame)
        self.assertEqual(frame["slots"][SLOT]["status"], "confirmed")
        return frame

    def test_actual_manager_first_offer_api_needs_no_legacy_split_hint_and_exposes_full_current_dto(self):
        request, decision = self.decision_without_legacy_hint()
        response = self.client.get(self.endpoint())
        self.assertEqual(response.status_code, 200, response.content)
        dto = response.json()["shipping"]
        self.assertTrue(dto["can_issue_offer"], dto)
        self.assertEqual(dto["decision_digest"], decision.envelope_hmac)
        self.assertEqual(len(dto["lines"]), 2)
        self.assertEqual([line["quantity"] for line in dto["lines"]], [2, 2])
        self.assertEqual({line["line_id"] for line in dto["lines"]},
            {line["line_id"] for line in request.envelope["lines"]})
        self.assertIsNone(dto["offer"])
        self.assertIsNone(dto["choice"])
        payload = self.issue_payload(request, decision)
        with patch("management.services.instagram_bot._provider_http") as provider:
            response = self.client.post(self.endpoint(), json.dumps(payload), content_type="application/json")
        provider.assert_not_called()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"], response.json())
        self.assertEqual(response.json()["shipping"]["status"], "pending")
        self.assertEqual(Offer.objects.count(), 1)
        self.no_commerce_effects()

    def test_authenticated_operation_replay_reuses_offer_but_changed_cost_on_same_operation_rejects(self):
        request, decision = self.decision_without_legacy_hint()
        payload = self.issue_payload(request, decision)
        first = self.client.post(self.endpoint(), json.dumps(payload), content_type="application/json")
        second = self.client.post(self.endpoint(), json.dumps(payload), content_type="application/json")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json()["offer_id"], second.json()["offer_id"])
        changed = {**payload, "options": self.options(request, cap="80.00")}
        rejected = self.client.post(self.endpoint(), json.dumps(changed), content_type="application/json")
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertEqual(Offer.objects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="split_shipping_offer").count(), 1)

    def test_strict_csrf_rejects_missing_token_and_accepts_authenticated_token(self):
        request, decision = self.decision_without_legacy_hint()
        strict = Client(enforce_csrf_checks=True, HTTP_HOST="management.twocomms.shop")
        strict.force_login(self.manager)
        payload = self.issue_payload(request, decision)
        response = strict.post(self.endpoint(), json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Offer.objects.exists())
        token = get_token(RequestFactory().get(self.endpoint()))
        strict.cookies["csrftoken"] = token
        response = strict.post(self.endpoint(), json.dumps(payload), content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Offer.objects.count(), 1)

    def test_pii_viewer_can_read_but_cannot_issue_offer_and_unprivileged_user_cannot_read(self):
        request, decision = self.decision_without_legacy_hint()
        viewer = get_user_model().objects.create_user(username="split-pii-viewer", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(content_type__app_label="management",
            codename="view_ig_conversation_pii"))
        viewer_client = Client(HTTP_HOST="management.twocomms.shop")
        viewer_client.force_login(viewer)
        self.assertEqual(viewer_client.get(self.endpoint()).status_code, 200)
        self.assertEqual(viewer_client.post(self.endpoint(), json.dumps(self.issue_payload(request, decision)),
            content_type="application/json").status_code, 403)
        outsider = get_user_model().objects.create_user(username="split-no-capabilities")
        outsider_client = Client(HTTP_HOST="management.twocomms.shop")
        outsider_client.force_login(outsider)
        self.assertEqual(outsider_client.get(self.endpoint()).status_code, 403)
        self.assertFalse(Offer.objects.exists())

    def test_foreign_client_digest_and_erasing_client_cannot_issue_offer(self):
        request, decision = self.decision_without_legacy_hint()
        foreign = IgClient.objects.create(igsid="foreign-split-api-client")
        response = self.client.post(self.endpoint(foreign), json.dumps(self.issue_payload(request, decision)), content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Offer.objects.exists())
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now)
        self.assertEqual(self.client.get(self.endpoint()).status_code, 410)
        self.assertEqual(self.client.post(self.endpoint(), json.dumps(self.issue_payload(request, decision)),
            content_type="application/json").status_code, 410)
        self.assertFalse(Offer.objects.exists())

    def test_duplicate_json_keys_and_extra_schema_fields_are_rejected_without_manager_debt(self):
        request, decision = self.decision_without_legacy_hint()
        payload = self.issue_payload(request, decision)
        encoded = json.dumps(payload)
        duplicate = encoded[:-1] + ', "action": "issue_offer"}'
        for body in (duplicate, json.dumps({**payload, "client_id": self.client_row.pk}),
                json.dumps({**payload, "options": {**payload["options"], "extra_delivery_amount": "70.00"}})):
            with self.subTest(body=body):
                response = self.client.post(self.endpoint(), body, content_type="application/json")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Offer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason="split_shipping_offer").exists())

    def test_context_pending_offer_and_actual_native_receipt_never_claim_customer_choice(self):
        offer = self.offer()
        pending = self.capture(at=self.now)
        shipping_value = pending["slots"][SLOT]["value"]["split_shipping"]
        self.assertEqual(shipping_value["status"], "pending")
        self.assertIsNone(shipping_value["proof"]["receipt"])
        self.assertIsNone(shipping_value["proof"]["choice"])
        count, physical = self.send(offer)
        self.assertEqual((count, len(physical)), (1, 1))
        sent = self.capture(at=self.now + timedelta(seconds=1))
        shipping_value = sent["slots"][SLOT]["value"]["split_shipping"]
        self.assertEqual(shipping_value["status"], "delivered")
        self.assertEqual(shipping_value["proof"]["receipt"]["native_mid"], "native-split-offer:" + offer.pk.hex)
        self.assertIsNone(shipping_value["proof"]["choice"])

    def test_chosen_context_adds_exact_original_native_choice_source_below_capture_watermark(self):
        offer, _receipt = self.sent_offer()
        result, source, _revision = self.answer(offer)
        self.assertTrue(result.ready, result)
        frame = self.capture(at=self.now + timedelta(seconds=3))
        slot = frame["slots"][SLOT]
        chosen = slot["value"]["split_shipping"]["proof"]["choice"]
        choice = Choice.objects.get(pk=result.choice_id)
        self.assertEqual(chosen["source"], choice.envelope["source"])
        self.assertEqual(chosen["source"]["message_id"], source.pk)
        refs = [ref for ref in slot["source_refs"] if ref["id"] == source.pk]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["digest"], choice.envelope["source"]["digest"])
        watermark = frame["boundary"]["source_watermark"]
        self.assertLessEqual(refs[0]["id"], watermark["message_id"])
        self.assertEqual(refs[0]["event_at"], source.provider_created_at.isoformat())

    def test_new_cost_version_invalidates_retained_captured_shipping_terms(self):
        offer, _receipt = self.sent_offer()
        _chosen, _source, revision = self.answer(offer)
        frame = self.capture(at=self.now + timedelta(seconds=3))
        self.assertEqual(validate_retained_fulfillment(revision, frame, now=self.now + timedelta(seconds=3)), "")
        now = self.now + timedelta(seconds=4)
        with patch("django.utils.timezone.now", return_value=now):
            shipping.issue_offer(self.client_row.pk, actor=self.manager,
                expected_decision_digest=offer.decision.envelope_hmac, operation_id=uuid.uuid4(),
                options=self.options(offer.decision.request, cap="80.00"), now=now)
        self.assertEqual(validate_retained_fulfillment(revision, frame, now=now), "split_shipping_terms_changed")

    def test_future_choice_is_omitted_from_old_capture_while_original_manager_facts_remain(self):
        offer, _receipt = self.sent_offer()
        older = self.capture(at=self.now + timedelta(seconds=1))
        result, _source, _revision = self.answer(offer)
        self.assertTrue(result.ready, result)
        now = self.now + timedelta(seconds=3)
        cart = capture_current_selection_lines(self.client_row.pk, now=now)
        current_review = reviews.fulfillment_review_projection(self.client_row.pk, now=now, current_source_cart=cart)
        later_shipping = shipping.context_snapshot(self.client_row.pk, self.settings,
            now=now, current_source_cart=cart, current_review=current_review)
        self.assertEqual(later_shipping["status"], "chosen", later_shipping)
        # The real earlier manager capture retains its own purchase scope.
        # Attach only the later signed shipping proof to isolate the temporal
        # component guard from the separate changed-purchase-scope guard.
        projection = deepcopy(older["slots"][SLOT]["value"])
        projection["split_shipping"] = later_shipping
        slot = fulfillment_slot(projection, boundary=older["boundary"], captured_at=self.now + timedelta(seconds=1))
        self.assertEqual(slot["status"], "confirmed", slot)
        self.assertEqual(slot["value"]["decision"]["id"], str(offer.decision_id))
        self.assertNotIn("split_shipping", slot["value"])
        self.assertEqual(slot["value"]["split_shipping_omission"], "shipping_after_capture")

    def test_context_snapshot_is_read_only_and_uses_actual_signed_receipt_and_choice(self):
        offer, _receipt = self.sent_offer()
        selected, _source, _revision = self.answer(offer)
        self.assertTrue(selected.ready, selected)
        now = self.now + timedelta(seconds=3)
        cart = capture_current_selection_lines(self.client_row.pk, now=now)
        review = reviews.fulfillment_review_projection(self.client_row.pk, now=now, current_source_cart=cart)
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            snapshot = shipping.context_snapshot(self.client_row.pk, self.settings, now=now,
                current_source_cart=cart, current_review=review)
        provider.assert_not_called()
        self.assertEqual(snapshot["status"], "chosen")
        self.assertTrue(snapshot["proof"]["receipt"])
        self.assertEqual(snapshot["proof"]["choice"]["id"], str(selected.choice_id))
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper() for row in queries))

    def test_explicit_manager_capabilities_preserve_chosen_capture_within_native_query_limit(self):
        offer, _receipt = self.sent_offer()
        selected, _source, _revision = self.answer(offer)
        self.assertTrue(selected.ready, selected)
        permissions = list(Permission.objects.filter(content_type__app_label="management",
            codename__in=["operate_ig_bot", "view_ig_conversation_pii"]))
        self.assertEqual(len(permissions), 2)
        self.manager.user_permissions.add(*permissions)
        self.manager.is_superuser = False
        self.manager.save(update_fields=["is_superuser"])
        # Real permission-backend reads remain inside the existing capture
        # budget; a different legitimate actor role cannot erase native consent.
        with CaptureQueriesContext(connection) as queries:
            frame = self.capture(at=self.now + timedelta(seconds=3))
        slot = frame["slots"][SLOT]
        statements = [row["sql"].lstrip().upper() for row in queries]
        self.assertLessEqual(sum(sql.startswith("SELECT") for sql in statements), 96)
        for sql in statements:
            self.assertIn(sql.split(None, 1)[0], {"SELECT", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "ROLLBACK"})
            self.assertNotIn("FOR UPDATE", sql)
            self.assertNotIn("LOCK IN SHARE MODE", sql)
        self.assertEqual(slot["status"], "confirmed")
        shipping_value = slot["value"]["split_shipping"]
        self.assertEqual(shipping_value["status"], "chosen")
        self.assertEqual(shipping_value["proof"]["choice"]["id"], str(selected.choice_id))
        self.assertNotIn("split_shipping_omission", slot["value"])

    def test_global_revision_postback_applies_native_choice_and_replays_same_source_once(self):
        offer, _receipt = self.sent_offer()
        payload = shipping.encode_choice(offer, "separate")
        _text, buttons = shipping.render_offer(offer)
        button = next(row for row in buttons if row.payload == payload)
        now = self.now + timedelta(seconds=2)
        source = self.source(button.title, payload=payload, at=now)
        revision, token = self.revision(source, now=now)
        args = {"source_message_id": source.pk, "settings_id": self.settings.pk,
            "settings_permission_epoch": self.settings.reply_permission_epoch,
            "publication": self.binding, "now": now}
        with patch("management.services.instagram_bot._provider_http") as provider:
            first = apply_revision_postback(revision.pk, token, **args)
            second = apply_revision_postback(revision.pk, token, **args)
        provider.assert_not_called()
        self.assertTrue(first.handled, first)
        self.assertTrue(first.ready, first)
        self.assertTrue(second.ready, second)
        self.assertEqual(Choice.objects.count(), 1)
        self.assertEqual(first.receipt["source_action_receipt_id"], second.receipt["source_action_receipt_id"])
        self.no_commerce_effects()

    def test_global_revision_postback_obsolete_cost_button_requires_context_without_creating_choice(self):
        offer, _receipt = self.sent_offer()
        now = self.now + timedelta(seconds=2)
        with patch("django.utils.timezone.now", return_value=now):
            shipping.issue_offer(self.client_row.pk, actor=self.manager,
                expected_decision_digest=offer.decision.envelope_hmac, operation_id=uuid.uuid4(),
                options=self.options(offer.decision.request, cap="80.00"), now=now)
        payload = shipping.encode_choice(offer, "separate")
        _text, buttons = shipping.render_offer(offer)
        source = self.source(next(row.title for row in buttons if row.payload == payload),
            payload=payload, at=self.now + timedelta(seconds=3))
        revision, token = self.revision(source)
        result = apply_revision_postback(revision.pk, token, source_message_id=source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, now=source.provider_created_at)
        self.assertTrue(result.handled, result)
        self.assertTrue(result.ready, result)
        self.assertTrue(result.requires_model, result)
        self.assertFalse(result.receipt)
        self.assertFalse(Choice.objects.exists())
        self.no_commerce_effects()

    def test_global_revision_postback_mixed_choice_and_customer_cost_objection_creates_no_choice(self):
        from management.services.ig_turn_revisions import (
            create_collecting_revision, claim_revision_preparation, seal_revision, claim_sealed_revision,
        )
        offer, _receipt = self.sent_offer()
        payload = shipping.encode_choice(offer, "separate")
        _text, buttons = shipping.render_offer(offer)
        now = self.now + timedelta(seconds=2)
        source = self.source(next(row.title for row in buttons if row.payload == payload), payload=payload, at=now)
        objection = self.source("Суму доставки ще не погоджую", at=now)
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=source,
            window_started_at=now, window_deadline=now)
        for index, row in enumerate((source, objection), 1):
            IgTurnMessage.objects.create(turn=turn, message=row, ordinal=index, role="user")
        created = create_collecting_revision(turn, [source, objection], now=now, bypass_quiet=True)
        prepared = claim_revision_preparation(created.revision.pk, now=now)
        sealed = seal_revision(created.revision.pk, prepared.token, now=now)
        claimed = claim_sealed_revision(sealed.revision.pk, now=now)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        result = apply_revision_postback(claimed.revision.pk, claimed.token, source_message_id=source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, now=now)
        self.assertTrue(result.handled, result)
        self.assertTrue(result.ready, result)
        self.assertTrue(result.requires_model, result)
        self.assertFalse(result.receipt)
        self.assertFalse(Choice.objects.exists())
        self.no_commerce_effects()
