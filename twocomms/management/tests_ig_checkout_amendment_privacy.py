"""Real holder witnesses are erased only by the committed privacy owner."""
from copy import deepcopy
import json
import uuid
from unittest.mock import patch

from django.db import DatabaseError, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management import tests_ig_checkout_amendment_successors as _successor_fixture
from management import tests_ig_checkout_service as _standard_fixture
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture
from management.models import (IgClient, IgCheckoutAccessToken, IgCheckoutProposal,
    IgCheckoutCartAmendment, IgCheckoutCartAmendmentResolution,
    IgCommerceSelectionTransition, IgCommerceSelectionSession, IgCommerceTurnDecision, IgFollowUpTask,
    IgCheckoutInvoiceGeneration, InstagramBotMessage)
from management.ig_assisted_commerce_models import (IgAssistedCommerceRequest,
    IgAssistedCommerceDecision, IgAssistedCommerceExecution, IgAssistedCommerceImage)
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_checkout_cart_amendments import checkout_cart_state
from management.services.ig_fulfillment_privacy import (purge_fulfillment_data,
    CHECKOUT_PRIVACY_TOMBSTONE, retain_checkout_privacy_tombstone)
from management.services.ig_private_media import private_media_storage
from management.bot_views import _delete_direct_bot_records
from orders.models import Order


def _assert_tombstone(test, owner):
    original_sender = owner.igsid
    row = IgClient.objects.get(pk=owner.pk)
    test.assertIsNotNone(row.privacy_erasure_started_at)
    test.assertIsNotNone(row.hidden_at)
    test.assertEqual(row.hidden_reason, CHECKOUT_PRIVACY_TOMBSTONE)
    test.assertTrue(row.bot_paused)
    test.assertTrue(row.is_blocked)
    test.assertTrue(row.igsid.startswith("erased:"))
    test.assertFalse(IgClient.objects.filter(igsid=original_sender).exists())
    for field in ("username", "display_name", "profile_pic_url", "avatar_local",
            "phone", "phone_normalized", "memory_summary", "ad_ref", "ad_id", "ad_title", "delivery_error"):
        test.assertEqual(getattr(row, field), "", field)
    for field in ("sales_context", "referral_payload", "memory_provenance", "memory_producer_state",
            "memory_claim_snapshot", "conversion_flags"):
        test.assertEqual(getattr(row, field), {}, field)
    test.assertIsNone(row.current_commercial_episode_id)
    test.assertFalse(IgCommerceSelectionSession.objects.filter(client_id=owner.pk).exists())
    test.assertFalse(IgCommerceTurnDecision.objects.filter(session__client_id=owner.pk).exists())
    test.assertFalse(IgFollowUpTask.objects.filter(client_id=owner.pk).exists())
    return row


def _hosted_quantity(test, browser, proposal):
    """Issue a real bearer and enter the public page before its CAS edit."""
    raw, token = IgCheckoutAccessToken.issue(proposal=proposal)
    entry = browser.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}))
    test.assertEqual(entry.status_code, 302, entry.content)
    page = browser.get(entry["Location"])
    test.assertEqual(page.status_code, 200, page.content)
    state = checkout_cart_state(proposal, token)
    payload = {"action": "cart_amendment", "operation_id": str(uuid.uuid4()),
        "operation": "quantity", "line_id": state["items"][0]["line_id"], "quantity": 2,
        "expected_revision": state["revision"], "expected_digest": state["digest"],
        "expected_selection_revision": state["selection_revision"]}
    with patch("storefront.views.monobank._monobank_api_request") as bank, \
            patch("management.services.instagram_bot._provider_http") as provider:
        response = browser.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
            json.dumps(payload), content_type="application/json", HTTP_ACCEPT="application/json")
    test.assertEqual(response.status_code, 200, response.content)
    bank.assert_not_called()
    provider.assert_not_called()
    return IgCheckoutCartAmendment.objects.get(proposal=proposal)


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeStandardAmendmentPrivacyTests(TransactionTestCase):
    _valid_item = _standard_fixture.InstagramCheckoutConfigurationTests._valid_item

    def setUp(self):
        _standard_fixture.InstagramCheckoutConfigurationTests.setUp(self)
        self.client_row = self.client
        self.client = Client()

    def test_real_standard_holder_witness_erases_before_client_and_retry_preserves_foreign_holder(self):
        own = create_or_update_proposal(client=self.client_row, pay_type="online_full",
            item_specs=[self._valid_item()])
        receipt = _hosted_quantity(self, self.client, own)
        own.refresh_from_db()
        financial_before = (own.revision, own.items_digest, own.quoted_total,
            list(own.items.order_by("pk").values()), list(own.revisions.order_by("pk").values()))
        transition_id = receipt.selection_transition.pk
        foreign = IgClient.get_or_create_for_sender("privacy-foreign-checkout-holder")
        foreign_proposal = create_or_update_proposal(client=foreign, pay_type="online_full",
            item_specs=[self._valid_item()])
        foreign_receipt = _hosted_quantity(self, Client(), foreign_proposal)
        foreign_before = deepcopy(foreign_receipt.source_body)
        with patch("management.services.instagram_bot._provider_http") as provider, \
                patch("storefront.views.monobank._monobank_api_request") as bank:
            result = _delete_direct_bot_records(exact_client_ids=[self.client_row.pk])
            retry = _delete_direct_bot_records(exact_client_ids=[self.client_row.pk])
        provider.assert_not_called()
        bank.assert_not_called()
        self.assertEqual((result["clients"], retry["clients"]), (1, 0))
        _assert_tombstone(self, self.client_row)
        own.refresh_from_db()
        self.assertEqual((own.revision, own.items_digest, own.quoted_total,
            list(own.items.order_by("pk").values()), list(own.revisions.order_by("pk").values())), financial_before)
        self.assertFalse(IgCheckoutAccessToken.objects.filter(proposal_id=own.pk, revoked_at__isnull=True).exists())
        self.assertFalse(IgCheckoutCartAmendment.objects.filter(pk=receipt.pk).exists())
        self.assertFalse(IgCommerceSelectionTransition.objects.filter(pk=transition_id).exists())
        foreign_receipt.refresh_from_db()
        self.assertEqual(foreign_receipt.source_body, foreign_before)
        self.assertTrue(IgCheckoutProposal.objects.filter(pk=foreign_proposal.pk).exists())
        self.assertTrue(IgCheckoutAccessToken.objects.filter(proposal_id=foreign_proposal.pk).exists())
        foreign.refresh_from_db()
        self.assertIsNone(foreign.privacy_erasure_started_at)


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeCheckoutAmendmentPrivacyTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    # Borrow only real producers, not the other TestCase's test methods.
    pending = _successor_fixture.NativeCheckoutAmendmentSuccessorTests.pending
    approve = _successor_fixture.NativeCheckoutAmendmentSuccessorTests.approve

    def _ledger_state(self):
        return {model._meta.label: list(model.objects.order_by("pk").values()) for model in (
            IgCheckoutCartAmendmentResolution, IgCheckoutCartAmendment,
            IgCommerceSelectionTransition, IgAssistedCommerceExecution,
            IgAssistedCommerceDecision, IgAssistedCommerceRequest, IgAssistedCommerceImage)}

    def _assert_owner_erased(self, case, child):
        _assert_tombstone(self, self.client_row)
        self.assertFalse(IgCheckoutCartAmendment.objects.filter(pk=case["amendment"].pk).exists())
        self.assertFalse(IgCheckoutCartAmendmentResolution.objects.filter(amendment_id=case["amendment"].pk).exists())
        self.assertFalse(IgCommerceSelectionTransition.objects.filter(checkout_amendment_id=case["amendment"].pk).exists())
        for model in (IgAssistedCommerceExecution, IgAssistedCommerceDecision,
                IgAssistedCommerceRequest, IgAssistedCommerceImage, InstagramBotMessage):
            self.assertFalse(model.objects.filter(client_id=self.client_row.pk).exists())
        self.assertEqual(IgCheckoutProposal.objects.filter(pk__in=[case["proposal_id"], child["proposal_id"]]).count(), 2)
        self.assertFalse(IgCheckoutAccessToken.objects.filter(proposal_id__in=[case["proposal_id"], child["proposal_id"]],
            revoked_at__isnull=True).exists())

    def test_resolved_custom_photo_paid_order_keeps_economics_while_all_holder_metadata_erases(self):
        from storefront.views.monobank import _apply_payment_attempt_status
        case = self.pending(photo=True)
        child = self.approve(case)
        response, bank = self._invoice_custom(child)
        self.assertEqual(response.status_code, 200, response.content)
        bank.assert_called_once()
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=child["proposal"])
        with patch("management.services.instagram_bot._provider_http", return_value=(200,
                json.dumps({"message_id": "native-privacy-successor-paid"}))), transaction.atomic():
            order, created = _apply_payment_attempt_status(generation.payment_attempt, "success",
                payload={"paidAmount": int(generation.payment_attempt.payment_amount * 100)}, source="provider_pull")
        self.assertTrue(created)
        economic_before = (order.total_sum, order.payment_status, list(order.items.order_by("pk").values()))
        retained_graph = (child["proposal"], generation, generation.payment_attempt)
        graph_before = {row._meta.label: type(row).objects.filter(pk=row.pk).values().get()
            for row in retained_graph}
        storage = private_media_storage()
        self.assertTrue(storage.exists(case["image"].storage_key))
        foreign = IgClient.get_or_create_for_sender("privacy-foreign-custom-owner")
        with patch("management.services.instagram_bot._provider_http") as provider, \
                patch("storefront.views.monobank._monobank_api_request") as bank:
            result = _delete_direct_bot_records(exact_client_ids=[self.client_row.pk])
            retry = _delete_direct_bot_records(exact_client_ids=[self.client_row.pk])
        provider.assert_not_called()
        bank.assert_not_called()
        self.assertEqual((result["clients"], retry["clients"]), (1, 0))
        self._assert_owner_erased(case, child)
        self.assertFalse(storage.exists(case["image"].storage_key))
        order = Order.objects.get(pk=order.pk)
        self.assertEqual((order.total_sum, order.payment_status, list(order.items.order_by("pk").values())), economic_before)
        self.assertEqual({row._meta.label: type(row).objects.filter(pk=row.pk).values().get()
            for row in retained_graph}, graph_before)
        foreign.refresh_from_db()
        self.assertIsNone(foreign.privacy_erasure_started_at)

    def test_helper_rejects_unfenced_or_mixed_owner_scope_before_bytes_or_ledgers(self):
        case = self.pending(photo=True)
        self.approve(case)
        before = self._ledger_state()
        with self.assertRaises(ValueError):
            purge_fulfillment_data([self.client_row.pk])
        with transaction.atomic(), self.assertRaises(ValueError):
            purge_fulfillment_data([self.client_row.pk])
        with transaction.atomic(), self.assertRaises(ValueError):
            retain_checkout_privacy_tombstone(self.client_row)
        foreign = IgClient.get_or_create_for_sender("privacy-unfenced-foreign-owner")
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=timezone.now())
        with transaction.atomic(), self.assertRaises(ValueError):
            purge_fulfillment_data([self.client_row.pk, foreign.pk])
        self.assertEqual(self._ledger_state(), before)
        self.assertTrue(private_media_storage().exists(case["image"].storage_key))
        foreign.refresh_from_db()
        self.assertIsNone(foreign.privacy_erasure_started_at)

    def test_image_bytes_failure_preserves_every_native_pointer_and_committed_fence_for_retry(self):
        case = self.pending(photo=True)
        child = self.approve(case)
        before = self._ledger_state()
        with patch("management.services.ig_assisted_commerce_media.delete_asset_bytes",
                side_effect=OSError("native private storage failure")) as delete_bytes:
            with self.assertRaises(OSError):
                _delete_direct_bot_records(exact_client_ids=[self.client_row.pk])
        delete_bytes.assert_called_once()
        self.client_row.refresh_from_db()
        self.assertIsNotNone(self.client_row.privacy_erasure_started_at)
        self.assertEqual(self._ledger_state(), before)
        self.assertTrue(private_media_storage().exists(case["image"].storage_key))
        result = _delete_direct_bot_records(exact_client_ids=[self.client_row.pk])
        self.assertEqual(result["clients"], 1)
        self._assert_owner_erased(case, child)
        self.assertFalse(private_media_storage().exists(case["image"].storage_key))

    def test_native_delete_denies_without_fence_and_previously_valid_resolution_cannot_reinsert_after_fence(self):
        case = self.pending()
        child = self.approve(case)
        resolution = IgCheckoutCartAmendmentResolution.objects.get(amendment=case["amendment"])
        # Retain the actual producer's signed row, with all phase/owner/token
        # predicates still valid. Only the client privacy state will change.
        values = {field.attname: deepcopy(getattr(resolution, field.attname))
            for field in resolution._meta.concrete_fields}
        transition_id = case["amendment"].selection_transition.pk
        transition_before = IgCommerceSelectionTransition.objects.filter(pk=transition_id).values().get()
        decision = IgCommerceTurnDecision.objects.filter(session__client_id=self.client_row.pk).order_by("pk").first()
        self.assertIsNotNone(decision)
        decision_before = IgCommerceTurnDecision.objects.filter(pk=decision.pk).values().get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            IgCommerceTurnDecision.objects.filter(pk=decision.pk)._raw_delete("default")
        with self.assertRaises(DatabaseError), transaction.atomic():
            IgCommerceSelectionTransition.objects.filter(pk=transition_id)._raw_delete("default")
        foreign = IgClient.get_or_create_for_sender("privacy-fenced-foreign-transition-owner")
        IgClient.objects.filter(pk=foreign.pk).update(privacy_erasure_started_at=timezone.now())
        with self.assertRaises(DatabaseError), transaction.atomic():
            IgCommerceTurnDecision.objects.filter(pk=decision.pk)._raw_delete("default")
        with self.assertRaises(DatabaseError), transaction.atomic():
            IgCommerceSelectionTransition.objects.filter(pk=transition_id)._raw_delete("default")
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(pk=transition_id).values().get(), transition_before)
        self.assertEqual(IgCommerceTurnDecision.objects.filter(pk=decision.pk).values().get(), decision_before)
        with self.assertRaises(DatabaseError), transaction.atomic():
            IgCheckoutCartAmendmentResolution.objects.filter(pk=resolution.pk)._raw_delete("default")
        self.assertTrue(IgCheckoutCartAmendmentResolution.objects.filter(pk=resolution.pk).exists())
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=timezone.now())
        with transaction.atomic():
            self.assertEqual(IgCheckoutCartAmendmentResolution.objects.filter(pk=resolution.pk)._raw_delete("default"), 1)
            with self.assertRaises(DatabaseError), transaction.atomic():
                IgCheckoutCartAmendmentResolution.objects.create(**values)
        self.assertFalse(IgCheckoutCartAmendmentResolution.objects.filter(pk=resolution.pk).exists())
        self.assertTrue(IgCheckoutCartAmendment.objects.filter(pk=case["amendment"].pk).exists())
        financial_before = list(IgCheckoutProposal.objects.filter(client=self.client_row).order_by("pk").values())
        _delete_direct_bot_records(exact_client_ids=[self.client_row.pk])
        self._assert_owner_erased(case, child)
        self.assertFalse(IgCommerceTurnDecision.objects.filter(pk=decision.pk).exists())
        self.assertEqual(list(IgCheckoutProposal.objects.filter(client_id=self.client_row.pk).order_by("pk").values()), financial_before)
