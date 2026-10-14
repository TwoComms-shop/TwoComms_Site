"""Native full approval/purchase/proposal producers; no authority mocks."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import uuid

from django.conf import settings
from django.db import transaction
from django.test import TransactionTestCase, override_settings

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
from management.models import IgCheckoutAccessToken, IgCheckoutProposal, IgCheckoutInventoryReservation
from management.services import ig_assisted_checkout as checkout
from management.tests_ig_assisted_commerce import AssistedCommerceFixture as CoreAssistedCommerceFixture
from management.tests_ig_assisted_stock_sync import AssistedStockSyncFixture
from orders.models import Order, PaymentAttempt
from storefront.models import Product


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, ROOT_URLCONF="twocomms.urls")
class NativeAssistedCheckoutTests(CoreAssistedCommerceFixture, TransactionTestCase):
    def _case(self, *, availability="ready", buy=True):
        request, _draft, decision, revision = self.approved_case(availability=availability)
        source = None
        if buy:
            self.now += timedelta(seconds=1)
            source = self.canonical("Хочу купити", at=self.now)
            revision, _claim = self.revision(source, now=self.now)
        return request, decision, revision, source

    def _prepare(self, request, decision, *, operation=None, **changes):
        args = {"actor": self.manager, "request_id": request.pk, "decision_id": decision.pk,
            "expected_request_digest": request.envelope_hmac, "expected_decision_digest": decision.envelope_hmac,
            "operation_id": operation or uuid.uuid4(), **changes}
        with patch("django.utils.timezone.now", return_value=self.now), \
                patch("management.services.instagram_bot._provider_http") as provider:
            result = checkout.prepare_assisted_checkout(self.client_row.pk, **args)
        provider.assert_not_called()
        return result

    def test_actual_approval_and_purchase_prepare_one_custom_proposal_no_fake_stock_or_order(self):
        request, decision, _revision, buy = self._case()
        before_products = Product.objects.count()
        result = self._prepare(request, decision)
        self.assertTrue(result["ready"], result)
        self.assertFalse(result["replayed"])
        self.assertTrue(result["token"])
        execution = Execution.objects.get(proposal_id=result["proposal_id"])
        item = result["proposal"].items.get()
        self.assertEqual((execution.purchase_source_id, execution.scope), (buy.pk, "full"))
        self.assertEqual((item.product_id, item.color_variant_id, item.size, item.quantity), (None, None, "L", 1))
        self.assertEqual((item.product_title, item.color_label, item.quoted_unit_price),
            ("Фіолетове худі", "Фіолетовий", Decimal("1500.00")))
        self.assertEqual(Product.objects.count(), before_products)
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))

    def test_duplicate_native_operation_returns_same_proposal_without_reminting_token(self):
        request, decision, _revision, _buy = self._case()
        operation = uuid.uuid4()
        first = self._prepare(request, decision, operation=operation)
        self.assertTrue(first["ready"], first)
        count = IgCheckoutAccessToken.objects.count()
        replay = self._prepare(request, decision, operation=operation)
        self.assertTrue(replay["ready"], replay)
        self.assertTrue(replay["replayed"])
        self.assertIsNone(replay["token"])
        self.assertEqual(replay["proposal_id"], first["proposal_id"])
        self.assertEqual(replay["checkout_url"], first["checkout_url"])
        self.assertEqual(IgCheckoutAccessToken.objects.count(), count)
        self.assertEqual((Execution.objects.count(), IgCheckoutProposal.objects.count()), (1, 1))
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_lost_response_recovers_same_bearer_from_native_digest_not_envelope(self):
        request, decision, _revision, _buy = self._case()
        operation = uuid.uuid4()
        first = self._prepare(request, decision, operation=operation)
        self.assertTrue(first["ready"], first)
        execution = Execution.objects.get(proposal_id=first["proposal_id"])
        self.assertNotIn(first["token"], str(execution.envelope))
        with override_settings(SECRET_KEY="rotated-assisted-key", SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]):
            replay = self._prepare(request, decision, operation=operation)
        self.assertTrue(replay["ready"], replay)
        self.assertEqual(replay["checkout_url"], first["checkout_url"])
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_revoked_bearer_is_not_reminted_on_operation_replay(self):
        request, decision, _revision, _buy = self._case()
        operation = uuid.uuid4()
        first = self._prepare(request, decision, operation=operation)
        self.assertTrue(first["ready"], first)
        IgCheckoutAccessToken.objects.filter(proposal_id=first["proposal_id"]).update(revoked_at=self.now)
        replay = self._prepare(request, decision, operation=operation)
        self.assertFalse(replay["ready"])
        self.assertEqual(replay["reason"], "assisted_access_token_unavailable")
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_different_operation_cannot_consume_same_finite_approval_twice(self):
        request, decision, _revision, _buy = self._case()
        first = self._prepare(request, decision)
        self.assertTrue(first["ready"], first)
        second = self._prepare(request, decision)
        self.assertFalse(second["ready"])
        self.assertEqual(second["reason"], "assisted_intention_already_executed")
        self.assertEqual((Execution.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 1, 1))

    def test_information_only_manager_approval_never_authorizes_purchase(self):
        request, decision, revision, _buy = self._case(buy=False)
        with patch("django.utils.timezone.now", return_value=self.now):
            offered = checkout.current_assisted_offer(self.client_row, revision=revision, now=self.now)
        self.assertTrue(offered["ready"], offered)
        self.assertFalse(offered["can_prepare_checkout"])
        self.assertEqual(offered["prepare_reason"], "checkout_purchase_authority_missing")
        result = self._prepare(request, decision)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "checkout_purchase_authority_missing")
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())

    def test_wait_scope_cannot_become_partial_invoice_or_hold(self):
        request, decision, _revision, _buy = self._case(availability="wait")
        result = self._prepare(request, decision)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "assisted_full_ready_required")
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

    def test_source_change_blocks_new_effect_but_preserves_frozen_quote_evidence(self):
        request, decision, _revision, buy = self._case()
        first = self._prepare(request, decision)
        self.assertTrue(first["ready"], first)
        proposal = first["proposal"]
        buy.text = "Передумав, інший розмір"
        buy.save(update_fields=["text"])
        with patch("django.utils.timezone.now", return_value=self.now):
            fresh = checkout.validate_assisted_proposal(proposal, now=self.now, fresh=True)
            historical = checkout.validate_assisted_proposal(proposal, now=self.now, fresh=False)
        self.assertFalse(fresh["ready"])
        self.assertTrue(historical["ready"], historical)
        self.assertEqual(historical["custom_rows"][0]["proposal_item_id"], proposal.items.get().pk)

    def test_expiry_blocks_new_invoice_without_erasing_original_terms(self):
        request, decision, _revision, _buy = self._case()
        first = self._prepare(request, decision)
        self.assertTrue(first["ready"], first)
        later = self.now + timedelta(hours=25)
        with patch("django.utils.timezone.now", return_value=later):
            fresh = checkout.validate_assisted_proposal(first["proposal"], now=later, fresh=True)
            historical = checkout.validate_assisted_proposal(first["proposal"], now=later, fresh=False)
        self.assertFalse(fresh["ready"])
        self.assertTrue(historical["ready"], historical)
        self.assertEqual(historical["custom_rows"][0]["unit_price"], "1500.00")

    def test_persisted_proposal_row_tamper_is_rejected_in_both_modes(self):
        request, decision, _revision, _buy = self._case()
        first = self._prepare(request, decision)
        self.assertTrue(first["ready"], first)
        first["proposal"].items.update(color_label="Чорний")
        with patch("django.utils.timezone.now", return_value=self.now):
            for fresh in (True, False):
                result = checkout.validate_assisted_proposal(first["proposal"], now=self.now, fresh=fresh)
                self.assertFalse(result["ready"])
                self.assertEqual(result["reason"], "assisted_custom_row_changed")

    def test_existing_execution_guard_rebuilds_readonly_and_exports_owned_capture(self):
        from management.services.ig_checkout import rebuild_assisted_quote_from_execution
        request, decision, revision, _buy = self._case()
        first = self._prepare(request, decision)
        self.assertTrue(first["ready"], first)
        before = (Execution.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count())
        with patch("django.utils.timezone.now", return_value=self.now):
            offered = checkout.current_assisted_offer(self.client_row, revision=revision, now=self.now)
            quote = rebuild_assisted_quote_from_execution(client=self.client_row,
                verified_execution=first["verified_execution"])
        self.assertTrue(offered["ready"], offered)
        self.assertFalse(offered["can_prepare_checkout"])
        self.assertEqual(offered["source_cart_binding"], first["source_cart_binding"])
        self.assertEqual(quote.digest, first["proposal"].items_digest)
        self.assertEqual((Execution.objects.count(), IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), before)

    def test_bot_cannot_prepare_without_real_claimed_publication_and_purchase(self):
        request, decision, _revision, _buy = self._case()
        result = self._prepare(request, decision, actor=None, revision_id=None, settings_id=self.settings.pk)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "bot_checkout_claim_missing")
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_proof_dict_cannot_replace_native_execution_capability(self):
        request, decision, _revision, _buy = self._case()
        first = self._prepare(request, decision)
        self.assertTrue(first["ready"], first)
        with self.assertRaisesMessage(checkout.AssistedCheckoutError, "verified_execution_requires_native_producer"):
            checkout.revalidate_verified_assisted_execution(deepcopy(first["proof"]), client=self.client_row)

    def test_lock_helper_requires_canonical_transaction_and_never_grants_absent_execution(self):
        with self.assertRaisesMessage(checkout.AssistedCheckoutError, "assisted_owner_transaction_required"):
            checkout.lock_assisted_checkout_owner(123)
        with transaction.atomic():
            self.assertIsNone(checkout.lock_assisted_checkout_owner(123))


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, ROOT_URLCONF="twocomms.urls", IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0)
class NativeUntouchedLegacyAssistedUpgradeTests(CoreAssistedCommerceFixture, TransactionTestCase):
    _prepare = NativeAssistedCheckoutTests._prepare

    def _legacy_then_approval(self):
        from management.services.ig_checkout import create_or_update_proposal
        self.variant.stock = self.rule.stock = 2
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full",
            item_specs=[{"product_id": self.product.pk, "color_variant_id": self.variant.pk,
                "qty": 1, "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}}])
        self.assertFalse(proposal.assisted_checkout_v2)
        self.assertEqual(proposal.payment_policy, IgCheckoutProposal.PaymentPolicy.LEGACY)
        self.client_row.refresh_from_db()
        request, _draft, decision, _revision = self.approved_case()
        self.now += timedelta(seconds=1)
        source = self.canonical("Хочу купити", at=self.now)
        self.revision(source, now=self.now)
        return proposal, request, decision

    def test_native_old_legacy_offer_upgrade_keeps_owner_and_pins_policy_at_off_zero(self):
        from management.services.ig_checkout_policy import PROPOSAL_TTL_SECONDS
        proposal, request, decision = self._legacy_then_approval()
        identity = (proposal.pk, proposal.public_id, proposal.deal_id, proposal.commercial_episode_id)
        original_revision = proposal.revision
        operation = uuid.uuid4()
        result = self._prepare(request, decision, operation=operation)
        self.assertTrue(result["ready"], result)
        proposal.refresh_from_db()
        self.assertEqual((proposal.pk, proposal.public_id, proposal.deal_id, proposal.commercial_episode_id), identity)
        self.assertTrue(proposal.assisted_checkout_v2)
        self.assertEqual(proposal.payment_policy, IgCheckoutProposal.PaymentPolicy.FULL_ONLY)
        self.assertTrue(proposal.custom_print_full_only)
        self.assertEqual(proposal.expires_at, self.now + timedelta(seconds=PROPOSAL_TTL_SECONDS))
        self.assertEqual(proposal.revision, original_revision + 1)
        self.assertEqual(proposal.items.get().product_title, "Фіолетове худі")
        self.assertIsNone(proposal.items.get().product_id)
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)
        self.assertEqual(Execution.objects.count(), 1)
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())
        replay = self._prepare(request, decision, operation=operation)
        self.assertTrue(replay["ready"], replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["proposal_id"], proposal.pk)
        self.assertEqual(IgCheckoutAccessToken.objects.filter(proposal=proposal).count(), 1)

    def test_detached_historical_attempt_blocks_native_upgrade_without_any_new_effect(self):
        proposal, request, decision = self._legacy_then_approval()
        # A persisted historical attempt remains a commitment even if its
        # proposal fast pointer was lost; never infer safety from that pointer.
        PaymentAttempt.objects.create(fingerprint="legacy-assisted-history", full_name="Клієнт",
            phone="+380671234567", city="Київ", np_office="1", pay_type="online_full",
            status=PaymentAttempt.Status.FAILED,
            cart_snapshot={"checkout_surface": "instagram_proposal", "proposal_id": str(proposal.public_id)})
        before = (proposal.revision, proposal.items_digest, proposal.expires_at)
        result = self._prepare(request, decision)
        self.assertFalse(result["ready"], result)
        proposal.refresh_from_db()
        self.assertFalse(proposal.assisted_checkout_v2)
        self.assertEqual((proposal.revision, proposal.items_digest, proposal.expires_at), before)
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        self.assertFalse(Order.objects.exists())


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, ROOT_URLCONF="twocomms.urls")
class NativeAssistedOfferStockTests(AssistedStockSyncFixture, TransactionTestCase):
    def _buy(self):
        source = self.canonical("Хочу купити всі товари в кошику", at=self.now)
        revision, _claim = self.revision(source, now=self.now)
        return revision

    def _offer(self, revision):
        with patch("django.utils.timezone.now", return_value=self.now), \
                patch("management.services.instagram_bot._provider_http") as provider:
            result = checkout.current_assisted_offer(self.client_row, revision=revision, now=self.now)
        provider.assert_not_called()
        return result

    def test_actual_zero_catalog_offer_is_blocked_until_explicit_recount_then_becomes_ready(self):
        request, _draft, decision, _revision = self.approved_catalog_case()
        revision = self._buy()
        before = self._offer(revision)
        self.assertFalse(before["ready"], before)
        self.assertFalse(before["can_prepare_checkout"])
        self.assertEqual(before["reason"], "assisted_stock_recount_required")
        self.assertTrue(before["prepare_message"])
        self.assertNotIn("verified_decision", before)
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        preview = self.preview(request, decision)
        synced = self.sync(request, decision, preview)
        self.assertTrue(synced["ready"], synced)
        after = self._offer(revision)
        self.assertTrue(after["ready"], after)
        self.assertTrue(after["can_prepare_checkout"], after)
        self.assertEqual(after["verified_decision"].approved_total, decision.approved_total)
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

    def test_actual_positive_catalog_offer_keeps_full_purchase_authority_without_recount(self):
        self.variant.stock = self.rule.stock = 2
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        _request, _draft, _decision, _revision = self.approved_catalog_case()
        offered = self._offer(self._buy())
        self.assertTrue(offered["ready"], offered)
        self.assertTrue(offered["can_prepare_checkout"], offered)
        self.assertEqual(offered["prepare_message"], "")
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_price_change_after_approval_removes_offer_authority_requires_fresh_approval(self):
        self.variant.stock = self.rule.stock = 2
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        _request, _draft, decision, _revision = self.approved_catalog_case()
        revision = self._buy()
        self.assertTrue(self._offer(revision)["can_prepare_checkout"])
        original = deepcopy(decision.envelope)
        self.variant.price_override += 100
        self.variant.save(update_fields=["price_override"])
        offered = self._offer(revision)
        self.assertFalse(offered["ready"], offered)
        self.assertFalse(offered["can_prepare_checkout"])
        self.assertEqual(offered["reason"], "assisted_catalog_price_changed")
        self.assertIn("повторно", offered["prepare_message"])
        self.assertNotIn("verified_decision", offered)
        decision.refresh_from_db()
        self.assertEqual(decision.envelope, original)
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_actual_price_recovery_requires_new_request_version_and_new_manager_approval(self):
        from management.services import ig_assisted_commerce as commerce
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.variant.stock = self.rule.stock = 2
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        old_request, _draft, old_decision, _revision = self.approved_catalog_case()
        revision = self._buy()
        self.assertTrue(self._offer(revision)["can_prepare_checkout"])
        old_terms = deepcopy(old_decision.envelope)
        self.variant.price_override += 100
        self.variant.save(update_fields=["price_override"])
        stale = self._offer(revision)
        self.assertFalse(stale["ready"], stale)
        self.assertEqual(stale["reason"], "assisted_catalog_price_changed")
        with patch("django.utils.timezone.now", return_value=self.now):
            capture = capture_current_selection_lines(self.client_row.pk, now=self.now)
            request, created = commerce.create_assisted_request(revision.pk,
                settings_id=self.settings.pk, operation_id=uuid.uuid4(),
                expected_revision_digest=revision.snapshot_digest,
                expected_full_capture_digest=capture["capture_digest"],
                intention_id=old_request.intention_id, expected_request_version=old_request.version,
                expected_request_digest=old_request.envelope_hmac, now=self.now)
        self.assertTrue(created)
        self.assertEqual(request.intention_id, old_request.intention_id)
        self.assertEqual(request.version, old_request.version + 1)
        self.assertEqual(request.created_at, old_request.created_at)
        self.assertEqual(request.revision_id, revision.pk)
        self.assertEqual(request.envelope["snapshot"]["full_capture"], capture)
        pending = self._offer(revision)
        self.assertFalse(pending["ready"])
        self.assertEqual(pending["reason"], "approved_decision_missing")
        self.assertFalse(pending["can_prepare_checkout"])
        self.assertEqual(pending["request"].pk, request.pk)
        self.assertNotIn("verified_decision", pending)
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        items = commerce._editable_items(request.envelope["snapshot"])
        self.assertEqual(len(items), 1)
        self.assertEqual(Decimal(items[0]["unit_price"]), old_decision.approved_total + Decimal("100.00"))
        for item in items:
            item.update(availability="ready", checked_quantity=item["quantity"],
                supply_basis="warehouse_checked", source_location="Менеджер повторно перевірив точну річ")
            for answer in item["question_answers"]:
                answer.update(state="confirmed", answer="Менеджер повторно перевірив актуальні умови")
        draft, _created = self.record(request, items=items)
        decision, approved = self.record(request, status="approved", latest=draft)
        self.assertTrue(approved)
        restored = self._offer(revision)
        self.assertTrue(restored["ready"], restored)
        self.assertTrue(restored["can_prepare_checkout"], restored)
        self.assertEqual(restored["verified_decision"].request_id, request.pk)
        self.assertEqual(restored["verified_decision"].decision_id, decision.pk)
        self.assertNotEqual(decision.pk, old_decision.pk)
        old_decision.refresh_from_db()
        self.assertEqual(old_decision.envelope, old_terms)
        with patch("django.utils.timezone.now", return_value=self.now), \
                patch("management.services.instagram_bot._provider_http") as provider:
            old_checkout = checkout.prepare_assisted_checkout(self.client_row.pk, actor=self.manager,
                request_id=old_request.pk, decision_id=old_decision.pk,
                expected_request_digest=old_request.envelope_hmac,
                expected_decision_digest=old_decision.envelope_hmac, operation_id=uuid.uuid4())
        provider.assert_not_called()
        self.assertFalse(old_checkout["ready"])
        self.assertEqual(old_checkout["reason"], "request_superseded")
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

        # A same-clock same-source refresh must choose the native version
        # head, even when its UUID sorts before an old approved request.
        with patch("django.utils.timezone.now", return_value=self.now):
            current_capture = capture_current_selection_lines(self.client_row.pk, now=self.now)
            head, created = commerce.create_assisted_request(revision.pk,
                settings_id=self.settings.pk, operation_id=uuid.uuid4(),
                expected_revision_digest=revision.snapshot_digest,
                expected_full_capture_digest=current_capture["capture_digest"],
                intention_id=request.intention_id, expected_request_version=request.version,
                expected_request_digest=request.envelope_hmac, now=self.now)
        self.assertTrue(created)
        self.assertEqual((head.created_at, head.source_id), (request.created_at, request.source_id))
        self.assertEqual(head.version, request.version + 1)
        same_source_pending = self._offer(revision)
        self.assertEqual(same_source_pending["reason"], "approved_decision_missing")
        self.assertEqual(same_source_pending["request"].pk, head.pk)

        # A genuinely newer own source creates a distinct intention. Its
        # version one wins the frozen-clock tie against the older version three.
        newer_revision = self._buy()
        with patch("django.utils.timezone.now", return_value=self.now):
            newer_capture = capture_current_selection_lines(self.client_row.pk, now=self.now)
            newer, created = commerce.create_assisted_request(newer_revision.pk,
                settings_id=self.settings.pk, operation_id=uuid.uuid4(),
                expected_revision_digest=newer_revision.snapshot_digest,
                expected_full_capture_digest=newer_capture["capture_digest"], now=self.now)
        self.assertTrue(created)
        self.assertNotEqual(newer.intention_id, head.intention_id)
        self.assertEqual(newer.created_at, head.created_at)
        self.assertGreater(newer.source_id, head.source_id)
        self.assertEqual(newer.version, 1)
        newer_pending = self._offer(newer_revision)
        self.assertEqual(newer_pending["reason"], "approved_decision_missing")
        self.assertEqual(newer_pending["request"].pk, newer.pk)
        self.assertFalse(newer_pending["can_prepare_checkout"])
        self.assertNotIn("verified_decision", newer_pending)
        with patch("django.utils.timezone.now", return_value=self.now), \
                patch("management.services.instagram_bot._provider_http") as provider:
            manager_view = commerce.operator_context(self.client_row.pk, actor=self.manager, now=self.now)
        provider.assert_not_called()
        self.assertEqual(manager_view["request"]["id"], str(newer.pk))
        self.assertEqual(manager_view["request"]["version"], 1)
        self.assertIsNone(manager_view["approved_head"])
        self.assertFalse(manager_view["can_prepare"])
        self.assertFalse(Execution.objects.exists())
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_foreign_real_hold_prevents_raw_positive_stock_from_authorizing_our_offer(self):
        foreign_proposal, foreign_hold = self.foreign_hold()
        self.variant.stock = self.rule.stock = 1
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        _request, _draft, _decision, _revision = self.approved_catalog_case()
        offered = self._offer(self._buy())
        self.assertFalse(offered["ready"], offered)
        self.assertEqual(offered["reason"], "assisted_stock_recount_required")
        self.assertFalse(offered["can_prepare_checkout"])
        foreign_hold.refresh_from_db()
        self.assertEqual(foreign_hold.state, IgCheckoutInventoryReservation.State.ACTIVE)
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)
        self.assertEqual(foreign_proposal.pk, IgCheckoutProposal.objects.get().pk)
        self.assertFalse(Execution.objects.exists())

    def test_exact_size_zero_blocks_offer_even_with_positive_variant_pool(self):
        self.variant.stock = 2
        self.variant.save(update_fields=["stock"])
        _request, _draft, _decision, _revision = self.approved_catalog_case()
        offered = self._offer(self._buy())
        self.assertFalse(offered["ready"], offered)
        self.assertEqual(offered["reason"], "assisted_stock_recount_required")
        self.assertFalse(IgCheckoutProposal.objects.exists())
