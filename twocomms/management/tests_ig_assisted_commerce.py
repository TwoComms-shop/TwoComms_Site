"""Actual source/revision/canonical-basket producers for assisted manager terms."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from management.ig_assisted_commerce_models import (
    IgAssistedCommerceRequest as Request, IgAssistedCommerceDecision as Decision,
    IgAssistedCommerceExecution as Execution,
)
from management.models import IgClient, IgCheckoutProposal, IgCheckoutInventoryReservation
from management.services import ig_assisted_commerce as assisted
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.tests_ig_restock_subscriptions import _RestockFixture
from storefront.models import Product


class AssistedCommerceFixture(_RestockFixture):
    """Reusable real producer fixture; no hand-sealed authority dictionaries."""
    def setUp(self):
        super().setUp()
        self.manager = get_user_model().objects.create_superuser("assisted-commerce-manager", password="test")

    def request_case(self, *, custom=True):
        source = self.canonical("цвет purple, размер L") if custom else self.source("Ця позиція підходить")
        revision, _token = self.revision(source)
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(cart["status"], "captured", cart)
        request, created = assisted.create_assisted_request(revision.pk, settings_id=self.settings.pk,
            operation_id=uuid.uuid4(), expected_revision_digest=revision.snapshot_digest,
            expected_full_capture_digest=cart["capture_digest"], now=self.now)
        self.assertTrue(created)
        return request, revision

    def items_for(self, request, *, availability="ready", unit_price="1500.00"):
        data = request.envelope["snapshot"]
        return [{"line_id": line["line_id"], "recipient_id": line["recipient_id"], "original_index": line["original_index"],
            "item_kind": "custom_apparel", "product_id": None, "color_variant_id": None,
            "title": "Фіолетове худі", "color": "Фіолетовий", "size": "L", "fit_code": "",
            "quantity": line["source_choices"].get("quantity", 1), "unit_price": unit_price, "currency": "UAH",
            "availability": availability, "checked_quantity": 1 if availability == "ready" else None,
            "supply_basis": "accessible_unrecorded" if availability == "ready" else "incoming_confirmed",
            "source_location": "Менеджер перевірив точну річ", "option_values": {}, "preparation": None,
            "dispatch": None, "image_id": None, "image_digest": "",
            "question_answers": [{"question_id": question["question_id"], "state": "confirmed",
                "answer": "Підтверджено: " + str(question["requested_value"])}
                for question in data["questions"] if question["line_id"] == line["line_id"]]} for line in data["lines"]]

    def record(self, request, *, status="draft", items=None, operation_id=None, latest=None, **changes):
        args = {"actor": self.manager, "operation_id": operation_id or uuid.uuid4(), "status": status,
            "expected_request_version": request.version, "expected_request_digest": request.envelope_hmac,
            "expected_full_capture_digest": request.full_capture_digest,
            "expected_decision_version": latest.version if latest else 0,
            "expected_draft_digest": latest.draft_digest if latest else "", "items": items, "now": self.now,
            **changes}
        return assisted.record_assisted_decision(request.pk, **args)

    def approved_case(self, *, availability="ready"):
        request, revision = self.request_case()
        draft, _ = self.record(request, items=self.items_for(request, availability=availability))
        decision, created = self.record(request, status="approved", latest=draft)
        self.assertTrue(created)
        return request, draft, decision, revision

    def verified(self, request, decision, **changes):
        return assisted.load_verified_assisted_decision(request.pk, decision.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac,
            now=changes.pop("now", self.now), **changes)


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeAssistedCommerceTests(AssistedCommerceFixture, TestCase):
    def _untouched_legacy_offer(self):
        from management.services.ig_checkout import create_or_update_proposal
        self.variant.stock = self.rule.stock = 4
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        with override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0):
            proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full", item_specs=[{
                "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1,
                "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}}])
        self.client_row.refresh_from_db()
        self.assertFalse(proposal.assisted_checkout_v2)
        return proposal

    def test_real_untouched_legacy_ready_and_viewed_offer_can_capture_owned_request_without_upgrade(self):
        proposal = self._untouched_legacy_offer()
        original_revision, original_expiry = proposal.revision, proposal.expires_at
        for state in (IgCheckoutProposal.Status.READY, IgCheckoutProposal.Status.VIEWED):
            with self.subTest(state=state):
                proposal.status = state
                proposal.save(update_fields=["status", "updated_at"])
                request, _revision = self.request_case(custom=False)
                self.assertEqual(request.deal_id, proposal.deal_id)
                self.assertEqual(request.episode_id, proposal.commercial_episode_id)
                self.assertEqual(request.envelope["snapshot"]["owner_scope"]["deal"]["active_checkout_proposal_id"], proposal.pk)
                proposal.refresh_from_db()
                self.assertFalse(proposal.assisted_checkout_v2)
                self.assertEqual((proposal.revision, proposal.expires_at), (original_revision, original_expiry))
                self.assertIsNone(proposal.payment_attempt_id)
        self.assertFalse(Decision.objects.exists())
        self.assertFalse(Execution.objects.exists())

    def test_detached_native_payment_attempt_blocks_legacy_request_capture_without_deletion(self):
        proposal = self._untouched_legacy_offer()
        from orders.models import PaymentAttempt
        attempt = PaymentAttempt.objects.create(fingerprint="assisted-detached-old-attempt", pay_type="online_full",
            gross_amount=proposal.quoted_total, payable_amount=proposal.quoted_total,
            payment_amount=proposal.quoted_total, status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"proposal_id": str(proposal.public_id)})
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "existing_invoice_commitment_requires_review"):
            self.request_case(custom=False)
        self.assertTrue(PaymentAttempt.objects.filter(pk=attempt.pk).exists())
        proposal.refresh_from_db()
        self.assertFalse(proposal.assisted_checkout_v2)
        self.assertIsNone(proposal.payment_attempt_id)
        self.assertFalse(Request.objects.exists())

    def test_native_signed_columns_remain_exact_after_auto_created_timestamp_and_reload(self):
        request, _revision = self.request_case()
        columns = deepcopy(request.envelope["columns"])
        snapshot = deepcopy(request.envelope["snapshot"])
        self.assertIsNotNone(request.created_at)
        self.assertNotIn("created_at", columns)
        self.assertNotIn("envelope", columns)
        self.assertNotIn("envelope_hmac", columns)
        self.assertEqual(columns, assisted._columns(request))
        request.refresh_from_db()
        self.assertEqual(columns, assisted._columns(request))
        self.assertEqual(snapshot, request.envelope["snapshot"])
        self.assertIsInstance(request.envelope["snapshot"]["reply_language"]["excluded_languages"], list)
        self.assertTrue(assisted.verify_assisted_evidence(request, "request"))
        draft, _ = self.record(request, items=self.items_for(request))
        decision, _ = self.record(request, status="approved", latest=draft)
        for row in (draft, decision):
            signed = deepcopy(row.envelope["columns"])
            row.refresh_from_db()
            self.assertEqual(signed, assisted._columns(row))
            self.assertNotIn("created_at", signed)
            self.assertTrue(assisted.verify_assisted_evidence(row, "decision"))

    def test_real_custom_source_draft_approval_has_no_stock_payment_or_catalog_mutation(self):
        before_products = Product.objects.count()
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, draft, decision, _ = self.approved_case()
            verified = self.verified(request, decision)
        provider.assert_not_called()
        self.assertTrue(assisted.verify_assisted_evidence(request, "request"))
        self.assertTrue(assisted.verify_assisted_evidence(draft, "decision"))
        self.assertTrue(assisted.verify_assisted_evidence(decision, "decision"))
        self.assertIsInstance(verified, assisted.VerifiedAssistedDecision)
        self.assertEqual(verified.approved_total, Decimal("1500.00"))
        self.assertEqual(verified.approved_custom_quantity, 1)
        self.assertIsNone(verified.lines[0].product_id)
        self.assertEqual(Product.objects.count(), before_products)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        self.assertFalse(Execution.objects.exists())

    def test_request_captures_questions_without_turning_them_into_facts(self):
        request, _revision = self.request_case()
        self.assertTrue(request.envelope["snapshot"]["questions"])
        for question in request.envelope["snapshot"]["questions"]:
            self.assertEqual(question["source_message_id"], request.source_id)
            for key in ("catalog_authority", "checkout_authority", "physical_authority", "price_authority"):
                self.assertIs(question[key], False)
        self.assertFalse(Decision.objects.exists())

    def test_append_edit_and_exact_draft_approval_cas_preserve_history(self):
        request, _revision = self.request_case()
        first_items = self.items_for(request)
        first, _ = self.record(request, items=first_items)
        changed = deepcopy(first_items)
        changed[0]["unit_price"] = "1600.00"
        second, _ = self.record(request, items=changed, latest=first)
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "decision_head_changed"):
            self.record(request, status="approved", latest=first)
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "approved_draft_changed"):
            self.record(request, status="approved", latest=second, items=first_items)
        approved, _ = self.record(request, status="approved", latest=second)
        self.assertEqual(approved.approved_total, Decimal("1600.00"))
        first.refresh_from_db()
        self.assertEqual(first.envelope["snapshot"]["items"][0]["unit_price"], "1500.00")
        self.assertEqual(list(Decision.objects.filter(request=request).order_by("version").values_list("version", flat=True)), [1, 2, 3])

    def test_same_operation_replays_only_exact_current_decision(self):
        request, _revision = self.request_case()
        operation = uuid.uuid4()
        items = self.items_for(request)
        draft, created = self.record(request, items=items, operation_id=operation)
        replay, created_again = self.record(request, items=items, operation_id=operation)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(draft.pk, replay.pk)
        changed = deepcopy(items)
        changed[0]["unit_price"] = "1900.00"
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "operation_conflict"):
            self.record(request, items=changed, operation_id=operation)
        approved, _ = self.record(request, status="approved", latest=draft)
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "decision_operation_superseded"):
            self.record(request, items=items, operation_id=operation)
        self.assertTrue(assisted.verify_assisted_evidence(approved, "decision"))

    def test_wait_remains_visible_unknown_timing_uncharged_and_cannot_prepare(self):
        request, _draft, decision, _revision = self.approved_case(availability="wait")
        verified = self.verified(request, decision)
        self.assertEqual(verified.approved_total, Decimal("0.00"))
        self.assertEqual(verified.approved_custom_quantity, 0)
        self.assertEqual(verified.lines[0].ready_capacity, 0)
        self.assertIsNone(verified.lines[0].as_dict()["dispatch"])
        context = assisted.operator_context(request.client_id, actor=self.manager, now=self.now)
        self.assertEqual(context["status"], "current", context)
        self.assertFalse(context["can_prepare"])
        self.assertEqual(context["can_prepare_reason"], "full_ready_required")
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_invalid_finite_price_qty_physical_check_and_line_identity_are_rejected(self):
        request, _revision = self.request_case()
        for change in ({"unit_price": "NaN"}, {"unit_price": "Infinity"}, {"unit_price": "1.999"},
                {"unit_price": -10}, {"quantity": True}, {"checked_quantity": 0}, {"checked_quantity": True},
                {"supply_basis": "incoming_confirmed"}, {"product_id": self.product.pk}, {"original_index": 1},
                {"size": "S" * 17}, {"fit_code": "f" * 51}):
            with self.subTest(change=change):
                items = self.items_for(request)
                items[0].update(change)
                with self.assertRaises(assisted.AssistedDecisionError):
                    self.record(request, items=items)
        self.assertFalse(Decision.objects.exists())

    def test_checked_surplus_does_not_increase_granted_capacity(self):
        request, _revision = self.request_case()
        items = self.items_for(request)
        items[0]["checked_quantity"] = 5
        draft, _ = self.record(request, items=items)
        decision, _ = self.record(request, status="approved", latest=draft)
        verified = self.verified(request, decision)
        self.assertEqual((verified.lines[0].checked_quantity, verified.lines[0].ready_capacity), (5, 1))
        self.assertEqual(verified.approved_custom_quantity, 1)

    def test_resolved_catalog_source_cannot_be_reclassified_as_custom_supply(self):
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock"])
        request, _revision = self.request_case(custom=False)
        self.assertTrue(request.envelope["snapshot"]["lines"][0]["catalog_item_spec"])
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "resolved_catalog_cannot_be_custom"):
            self.record(request, items=self.items_for(request))
        self.assertFalse(Decision.objects.exists())

    def test_actual_catalog_terms_and_photo_cannot_be_changed_by_manager(self):
        request, _revision = self.request_case(custom=False)
        canonical = assisted._editable_items(request.envelope["snapshot"])
        canonical[0].update(availability="ready", checked_quantity=10, supply_basis="warehouse_checked")
        canonical[0]["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed", "answer": "Перевірено"}
            for question in request.envelope["snapshot"]["questions"] if question["line_id"] == canonical[0]["line_id"]]
        for changes in ({"title": "Інша назва"}, {"color": "purple"}, {"size": "L"}, {"fit_code": "oversize"},
                {"quantity": 2}, {"unit_price": "1.00"}, {"option_values": {"fit": "oversize"}},
                {"color_variant_id": self.variants[1].pk}, {"image_id": str(uuid.uuid4()), "image_digest": "a" * 64}):
            with self.subTest(changes=changes):
                items = deepcopy(canonical)
                items[0].update(changes)
                with self.assertRaisesMessage(assisted.AssistedDecisionError, "catalog_terms_changed"):
                    self.record(request, items=items)
        self.assertFalse(Decision.objects.exists())
        draft, _ = self.record(request, items=canonical)
        approved, _ = self.record(request, status="approved", latest=draft)
        self.assertEqual(self.verified(request, approved).lines[0].title, self.product.title)
        context = assisted.operator_context(request.client_id, actor=self.manager, now=self.now)
        self.assertFalse(context["item_controls"][0]["can_edit_terms"])
        self.assertFalse(context["item_controls"][0]["can_edit_photo"])

    def test_actual_custom_offer_terms_remain_editable_without_rewriting_source_wishes(self):
        request, _revision = self.request_case()
        original = deepcopy(request.envelope["snapshot"])
        items = self.items_for(request)
        items[0].update(title="Індивідуальна модель", color="Фіолетовий", size="XL", fit_code="special",
            quantity=2, checked_quantity=2, unit_price="2000.00")
        draft, _ = self.record(request, items=items)
        approved, _ = self.record(request, status="approved", latest=draft)
        line = self.verified(request, approved).lines[0]
        self.assertEqual((line.title, line.size, line.fit_code, line.quantity, line.unit_price),
            ("Індивідуальна модель", "XL", "special", 2, Decimal("2000.00")))
        request.refresh_from_db()
        self.assertEqual(original, request.envelope["snapshot"])
        context = assisted.operator_context(request.client_id, actor=self.manager, now=self.now)
        self.assertTrue(context["item_controls"][0]["can_edit_terms"])
        self.assertTrue(context["item_controls"][0]["can_edit_photo"])

    def test_initial_catalog_hint_uses_ordinary_quantity_tier_across_distinct_recipients(self):
        self.category.slug = "tshirts"
        self.category.save(update_fields=["slug"])
        self.product.slug, self.product.price = "225-tshirt", 880
        self.product.save(update_fields=["slug", "price"])
        self.variant.price_override, self.variant.stock, self.rule.stock = 880, 2, 2
        self.variant.save(update_fields=["price_override", "stock"])
        self.rule.save(update_fields=["stock"])
        # Classify the catalog before creating this customer's native choices.
        # Changing the garment category after the base fixture's original
        # ADD correctly makes that old line's semantic replay unavailable.
        self.client_row = IgClient.objects.create(igsid="assisted-tier-source-owner", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
        self.canonical(f"добавьте {self.product.title} для друга, количество 1, размер M, цвет black, крій classic")
        capture = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertTrue(capture["coverage_complete"], capture)
        request, _revision = self.request_case(custom=False)
        specs = [row["catalog_item_spec"] for row in request.envelope["snapshot"]["lines"]]
        self.assertEqual([spec["recipient_id"] for spec in specs], ["self", "friend"])
        self.assertEqual([spec["qty"] for spec in specs], [1, 1])
        self.assertEqual([spec["unit_price"] for spec in specs], ["800.00", "800.00"])
        items = assisted._editable_items(request.envelope["snapshot"])
        for item in items:
            item.update(availability="ready", checked_quantity=1, supply_basis="warehouse_checked")
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed", "answer": "Перевірено"}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        draft, _ = self.record(request, items=items)
        approved, _ = self.record(request, status="approved", latest=draft)
        from management.services.ig_checkout import build_assisted_quote
        quote = build_assisted_quote(client=self.client_row, verified_decision=self.verified(request, approved))
        self.assertEqual([item.catalog_unit_price for item in quote.items], [Decimal("800.00"), Decimal("800.00")])
        self.assertEqual(quote.quoted_total, approved.approved_total)
        self.assertEqual(quote.quoted_total, Decimal("1600.00"))

    def test_full_mixed_intention_keeps_catalog_source_price_hint_separate_from_custom_approval(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        self.canonical("цвет purple, размер L")
        other, variant = self.products[1], self.variants[1]
        variant.stock = 3
        variant.save(update_fields=["stock"])
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="M", stock=3, is_enabled=True)
        ProductInventoryPolicy.objects.create(product=other, source="catalog_variant")
        self.canonical(f"добавьте {other.title}, количество 1, размер M, цвет pink, крій classic")
        request, _revision = self.request_case(custom=False)
        self.assertEqual(len(request.envelope["snapshot"]["lines"]), 2)
        items = self.items_for(request)
        catalog = request.envelope["snapshot"]["lines"][1]["catalog_item_spec"]
        self.assertTrue(catalog)
        items[1].update(item_kind="catalog_apparel", product_id=catalog["product_id"],
            color_variant_id=catalog["color_variant_id"], title=other.title, color="Pink", size="M",
            fit_code="classic", option_values=catalog["option_values"], unit_price=catalog["unit_price"],
            supply_basis="warehouse_checked")
        draft, _ = self.record(request, items=items)
        decision, _ = self.record(request, status="approved", latest=draft)
        verified = self.verified(request, decision)
        self.assertEqual([line.item_kind for line in verified.lines], ["custom_apparel", "catalog_apparel"])
        self.assertEqual(verified.approved_custom_quantity, 1)
        self.assertEqual(verified.approved_total, Decimal("1500.00") + other.price)
        self.assertIsNone(verified.lines[0].product_id)
        self.assertEqual(verified.lines[1].product_id, other.pk)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_frozen_execution_scope_preserves_original_terms_and_historical_cap_cannot_revalidate_live(self):
        request, _draft, decision, _revision = self.approved_case()
        current = self.verified(request, decision)
        self.assertEqual(current.proof_scope, "unconsumed")
        execution = assisted.verified_assisted_decision_from_evidence(request, decision)
        self.assertEqual(execution.proof_scope, "execution")
        self.assertEqual(assisted.revalidate_verified_assisted_decision(execution).lines, execution.lines)
        get_user_model().objects.filter(pk=self.manager.pk).update(is_active=False)
        historical = assisted.verified_assisted_decision_from_evidence(request, decision, historical=True)
        self.assertEqual(historical.proof_scope, "historical")
        self.assertEqual(historical.approved_total, Decimal("1500.00"))
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "verified_decision_requires_native_producer"):
            assisted.revalidate_verified_assisted_decision(historical)

    def test_rejected_head_does_not_rewrite_historical_approval(self):
        request, _draft, decision, _revision = self.approved_case()
        old = deepcopy(decision.envelope)
        rejected, _ = self.record(request, status="rejected", latest=decision, rejection_reason="Кількість не підтверджена")
        self.assertEqual(rejected.approved_total, Decimal("0.00"))
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "approved_decision_changed"):
            self.verified(request, decision)
        decision.refresh_from_db()
        self.assertEqual(decision.envelope, old)

    def test_source_change_epoch_erasure_and_ttl_invalidate_new_effect_only(self):
        request, _draft, decision, _revision = self.approved_case()
        original_hmac = decision.envelope_hmac
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "request_expired"):
            self.verified(request, decision, now=self.now + timedelta(hours=25))
        IgClient.objects.filter(pk=request.client_id).update(reply_permission_epoch=self.client_row.reply_permission_epoch + 1)
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "permission_epoch_changed"):
            self.verified(request, decision)
        IgClient.objects.filter(pk=request.client_id).update(reply_permission_epoch=self.client_row.reply_permission_epoch,
            privacy_erasure_started_at=self.now)
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "client_unavailable"):
            self.verified(request, decision)
        self.assertEqual(Decision.objects.get(pk=decision.pk).envelope_hmac, original_hmac)
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_paused_business_edit_keeps_bot_send_permission_separate(self):
        request, _revision = self.request_case()
        business_owner = deepcopy(request.envelope["snapshot"]["owner_scope"])
        self.assertNotIn("bot_paused", business_owner["client"])
        self.assertNotIn("manager_takeover", business_owner["client"])
        IgClient.objects.filter(pk=request.client_id).update(bot_paused=True, manager_takeover=True)
        self.settings.is_enabled = False
        self.settings.save(update_fields=["is_enabled"])
        draft, _ = self.record(request, items=self.items_for(request))
        decision, _ = self.record(request, status="approved", latest=draft)
        self.assertEqual(self.verified(request, decision).source_proof["owner_scope"], business_owner)
        context = assisted.operator_context(request.client_id, actor=self.manager, now=self.now)
        self.assertEqual(context["status"], "current", context)
        self.assertTrue(context["can_prepare"])
        self.assertFalse(context["bot_effects_allowed"])
        self.assertEqual(context["approved_head"]["id"], str(decision.pk))

    def test_actual_new_purchase_with_unchanged_choices_keeps_physical_decision_current(self):
        request, _draft, decision, _revision = self.approved_case()
        buy = self.canonical("Хочу купити")
        self.revision(buy)
        verified = self.verified(request, decision)
        self.assertEqual(verified.request_digest, request.envelope_hmac)
        self.assertNotEqual(verified.current_capture["capture_digest"], verified.full_capture_digest)
        self.assertEqual(verified.current_capture["source_watermark"]["message_id"], buy.pk)
        self.assertEqual(verified.source_proof["source_id"], request.source_id)

    def test_new_quantity_source_cannot_reuse_checked_approval(self):
        request, _draft, decision, _revision = self.approved_case()
        self.canonical("змініть кількість на 2")
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "source_configuration_or_head_changed"):
            self.verified(request, decision)

    def test_meta_reviewer_deny_and_fresh_actor_revocation_do_not_change_history(self):
        from django.contrib.auth.models import Group
        from management.bot_access import META_REVIEWER_GROUP_NAME
        request, _draft, decision, _revision = self.approved_case()
        old = deepcopy(decision.envelope)
        executor = get_user_model().objects.create_superuser("second-assisted-executor", password="test")
        self.manager.groups.add(Group.objects.get_or_create(name=META_REVIEWER_GROUP_NAME)[0])
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "actor_forbidden"):
            assisted.load_verified_assisted_decision(request.pk, decision.pk, actor=executor,
                expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=self.now)
        self.assertEqual(Decision.objects.get(pk=decision.pk).envelope, old)
        self.assertFalse(Execution.objects.exists())

    def test_original_approver_permission_removal_denies_a_different_authorized_operator(self):
        from django.contrib.auth.models import Permission
        permissions = list(Permission.objects.filter(content_type__app_label="management",
            codename__in=["operate_ig_bot", "view_ig_conversation_pii"]))
        self.assertEqual(len(permissions), 2)
        get_user_model().objects.filter(pk=self.manager.pk).update(is_superuser=False)
        self.manager.user_permissions.add(*permissions)
        request, _draft, decision, _revision = self.approved_case()
        executor = get_user_model().objects.create_superuser("assisted-new-authorized-operator", password="test")
        self.manager.user_permissions.remove(next(row for row in permissions if row.codename == "operate_ig_bot"))
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "actor_forbidden"):
            assisted.load_verified_assisted_decision(request.pk, decision.pk, actor=executor,
                expected_request_digest=request.envelope_hmac, expected_decision_digest=decision.envelope_hmac, now=self.now)
        self.assertTrue(assisted.verify_assisted_evidence(decision, "decision"))
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_actual_source_body_rewrite_invalidates_original_request(self):
        request, _draft, decision, _revision = self.approved_case()
        request.source.text = "Інші параметри"
        request.source.save(update_fields=["text"])
        with self.assertRaisesMessage(assisted.AssistedDecisionError, "source_changed"):
            self.verified(request, decision)
        self.assertTrue(assisted.verify_assisted_evidence(decision, "decision"))
