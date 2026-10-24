"""An exact private quote's unit is not a new shortage or restock opt-in."""
from datetime import timedelta
from copy import deepcopy
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.models import IgCheckoutProposal, IgCheckoutAccessToken, IgCheckoutInventoryReservation, IgClient
from management.services import ig_inventory_availability as inventory
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_subscriptions as restock
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.tests_ig_restock_subscriptions import _RestockFixture
from management import tests_ig_fulfillment_question_debt_20261024 as question_fixture
from management import tests_ig_revision_live as live_fixture


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=20000, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class OwnHoldConsumerTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    prepare_question = question_fixture.NativeFeasibilityQuestionTests.prepare_question
    execute = question_fixture.NativeFeasibilityQuestionTests.execute
    _generate = live_fixture.RevisionLiveTests._generate

    def setUp(self):
        super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count, self.actual_hash_override = 0, "observed"
        self.provider_failure = False

    def stock(self, quantity):
        type(self.variant).objects.filter(pk=self.variant.pk).update(stock=quantity)
        type(self.rule).objects.filter(pk=self.rule.pk).update(stock=quantity, updated_at=self.now)

    def private_hold(self, *, expected_holds=1):
        from management.services.ig_fulfillment_checkout import prepare_fulfillment_checkout
        self.canonical("Хочу купити")
        request = self.prepare_question()
        manager = get_user_model().objects.create_superuser(username="own-hold-manager", password="test")
        decision = reviews.apply_fulfillment_decision(request.pk, actor=manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), now=self.now,
            decisions_by_line=[{"line_id": row["line_id"], "availability": "available", "checked_quantity": 1,
                "supply_basis": "accessible_unrecorded"} for row in request.envelope["lines"]])
        self.assertTrue(decision["ready"], decision)
        with transaction.atomic():
            self.stock(1)
            prepared = prepare_fulfillment_checkout(request, reviews.Decision.objects.get(pk=decision["decision_id"]),
                actor=manager, now=self.now)
        self.assertEqual(prepared["status"], "proposal_ready", prepared)
        self.proposal = IgCheckoutProposal.objects.get(pk=prepared["proposal_id"])
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 0))
        holds = IgCheckoutInventoryReservation.objects.all()
        self.assertEqual(holds.count(), expected_holds)
        self.assertTrue(all(hold.quantity == 1 for hold in holds))
        return request

    def assent(self, text="Беру. Оформіть замовлення."):
        self.buyer = self.source(text)
        self.revision, self.token = _RestockFixture.revision(self, self.buyer)
        return self.revision

    def cart(self):
        captured = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(captured["status"], "captured", captured)
        return captured

    def held_ids(self, *, captured=None, cart=None):
        cart = self.cart() if cart is None else cart
        return inventory.owned_checkout_capacity_line_ids(self.client_row.pk, current_source_cart=cart,
            inventory_capture=captured, now=self.now)

    def test_plain_assent_defers_false_restock_and_reuses_one_private_quote_after_date_release(self):
        self.private_hold()
        self.assent()
        first, validations, factories, prepares, sends = self.execute("Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(first.state, "completed", (first.reasons, validations, factories, prepares, sends))
        self.assertIn((False, ("checkout_fulfillment_question_unanswered",)), validations)
        self.assertEqual((factories, prepares, sends), (0, 0, 0))
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertEqual(reviews.Request.objects.count(), 1)
        self.assertIn("existing_quote_capacity", json.dumps(self.generation_calls[-1]["system_instruction"]))
        self.assertIn("unanswered customer questions still block checkout", json.dumps(self.generation_calls[-1]["system_instruction"]))
        _RestockFixture.revision(self, self.source("Дата відправки неважлива."))
        self.assent()
        result, _, factories, prepares, sends = self.execute("Перевірте деталі оформлення:", paylink=True)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, sends), (1, 1, 1))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 1))
        self.assertEqual(IgCheckoutProposal.objects.get().pk, self.proposal.pk)
        active = IgCheckoutInventoryReservation.objects.filter(state="active")
        self.assertEqual((active.count(), active.get().quantity), (1, 1))
        self.assertEqual(reviews.Request.objects.count(), 1)
        self.assertFalse(restock.Invitation.objects.exists())
        prompt = json.dumps(self.generation_calls[-1]["system_instruction"])
        self.assertIn("existing_quote_capacity", prompt)
        self.assertIn("Continue that same quote without repeating an availability check", prompt)

    def test_both_negative_consumers_keep_the_exact_native_hold_without_effects(self):
        self.private_hold()
        self.assent()
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = restock.prepare_request(self.revision, self.buyer, self.settings, now=self.now)
            review = reviews.prepare_fulfillment_review(self.revision, settings_obj=self.settings, now=self.now)
        provider.assert_not_called()
        self.assertFalse(result.handled, result)
        self.assertFalse(review.get("created"), review)
        self.assertEqual(reviews.Request.objects.count(), 1)
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 0))
        self.assertEqual(IgCheckoutInventoryReservation.objects.filter(state="active").count(), 1)

    def test_projection_is_read_only_and_does_not_rewrite_negative_inventory_or_grant_claims(self):
        self.private_hold()
        cart = self.cart()
        captured = inventory.capture_inventory_availability(self.client_row.pk, current_source_cart=cart, now=self.now)
        self.assertEqual(captured["lines"][0]["status"], "check_required")
        self.assertEqual(captured["lines"][0]["free_quantity"], 0)
        original = inventory._json(captured)
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            ids = self.held_ids(captured=captured, cart=cart)
        provider.assert_not_called()
        self.assertEqual(ids, frozenset(row["line_id"] for row in cart["lines"]))
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT")
            and "FOR UPDATE" not in row["sql"].upper() for row in queries))
        self.assertEqual(inventory._json(captured), original)
        projected = inventory.inventory_prompt_value(captured)
        self.assertEqual(projected["existing_quote_capacity"]["line_ids"], list(ids))
        self.assertEqual(projected["lines"][0]["status"], "check_required")
        for private in ("witness", "proposal_id", "signature", "reservation_fingerprint", "live_checkout_hold_ids"):
            self.assertNotIn(private, json.dumps(projected))
        from management.services.ig_reply_authority import apply_inventory_availability_context
        from management.services.ig_reply_truth import ReplyTruthContext
        context = apply_inventory_availability_context(ReplyTruthContext(), captured)
        self.assertFalse(context.inventory_availability_authorities)
        self.assertFalse(context.payment_confirmed)
        self.assertFalse(context.order_created)
        self.assertFalse(IgCheckoutAccessToken.objects.exists())

    def test_tampered_annotation_and_stale_physical_capacity_cannot_authorize_continuation(self):
        self.private_hold()
        captured = inventory.capture_inventory_availability(self.client_row.pk, now=self.now)
        self.assertTrue(inventory.owned_quote_capacity_line_ids(captured))
        changed = deepcopy(captured)
        changed["owned_quote_capacity"]["line_ids"].append("foreign-line")
        with self.assertNumQueries(0):
            self.assertFalse(inventory.owned_quote_capacity_line_ids(changed))
            self.assertNotIn("existing_quote_capacity", inventory.inventory_prompt_value(changed))
        self.stock(0)
        self.assertEqual(inventory.validate_inventory_capture(self.client_row.pk, captured, now=self.now),
            "inventory_availability_changed")

    def test_existing_quote_annotation_does_not_grant_positive_stock_prose(self):
        self.private_hold()
        _RestockFixture.revision(self, self.source("Дата відправки неважлива."))
        self.assent()
        result, _, factories, prepares, sends = self.execute("Restock Aurora чорний розмір M є в наявності.")
        self.assertNotEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, sends), (0, 0, 0))
        self.assertFalse(IgCheckoutAccessToken.objects.exists())

    def test_capacity_lost_after_real_prompt_before_validation_blocks_send(self):
        self.private_hold()
        _RestockFixture.revision(self, self.source("Дата відправки неважлива."))
        self.assent()
        self.before_validate = lambda: self.stock(0)
        result, _, factories, prepares, sends = self.execute("Перевірте деталі оформлення:", paylink=True)
        self.assertNotEqual(result.state, "completed", result.reasons)
        self.assertEqual((factories, prepares, sends), (0, 0, 0))
        self.assertFalse(IgCheckoutAccessToken.objects.exists())

    def test_negative_parent_rebind_retains_same_quote_without_positive_stock_rights(self):
        self.private_hold()
        from management.services.ig_reply_authority import inventory_availability_authorities
        parent = inventory.capture_inventory_availability(self.client_row.pk, now=self.now)
        rebound = inventory.capture_owned_checkout_inventory(self.client_row.pk, parent,
            proposal_id=self.proposal.pk, now=self.now)
        self.assertEqual(rebound["status"], "captured", rebound)
        self.assertEqual(rebound["owned_checkout"]["parent_capture"], parent)
        self.assertTrue(inventory.owned_quote_capacity_line_ids(rebound))
        self.assertFalse(inventory_availability_authorities(rebound))
        self.assertEqual(inventory.validate_inventory_capture(self.client_row.pk, rebound, now=self.now), "")
        self.assertEqual(inventory.capture_owned_checkout_inventory(self.client_row.pk, parent,
            proposal_id=self.proposal.pk + 1, now=self.now)["status"], "unknown")

    def test_replenishing_one_of_two_real_owned_pools_cannot_expand_negative_parent_authority(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        from management.services.ig_reply_authority import inventory_availability_authorities
        sibling, variant = self.products[1], self.variants[1]
        ProductInventoryPolicy.objects.create(product=sibling, source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=1, is_enabled=True)
        type(variant).objects.filter(pk=variant.pk).update(stock=1)
        self.canonical("Хочу купити")
        self.canonical(f"добавьте {sibling.title}, количество 1, размер L, цвет pink, крій classic")
        self.private_hold(expected_holds=2)
        parent = inventory.capture_inventory_availability(self.client_row.pk, now=self.now)
        self.assertEqual([row["status"] for row in parent["lines"]], ["check_required", "check_required"])
        self.assertTrue(inventory.owned_quote_capacity_line_ids(parent))
        self.assertFalse(inventory_availability_authorities(parent))
        self.stock(2)
        current = inventory.capture_inventory_availability(self.client_row.pk, now=self.now)
        self.assertTrue(inventory.owned_quote_capacity_line_ids(current))
        self.assertEqual([row["status"] for row in current["lines"]], ["available", "check_required"])
        self.assertEqual(len(inventory_availability_authorities(current)), 1)
        rebound = inventory.capture_owned_checkout_inventory(self.client_row.pk, parent,
            proposal_id=self.proposal.pk, now=self.now)
        self.assertEqual(rebound, {"status": "unknown", "reason": "inventory_owned_quote_changed"})
        self.assertFalse(inventory_availability_authorities(rebound))
        self.assertEqual((IgCheckoutProposal.objects.count(), IgCheckoutAccessToken.objects.count()), (1, 0))
        self.assertEqual(IgCheckoutInventoryReservation.objects.filter(state="active").count(), 2)

    def test_changed_target_cannot_rebind_an_original_negative_parent(self):
        self.private_hold()
        parent = inventory.capture_inventory_availability(self.client_row.pk, now=self.now)
        self.canonical("Кількість 2")
        rebound = inventory.capture_owned_checkout_inventory(self.client_row.pk, parent,
            proposal_id=self.proposal.pk, now=self.now)
        self.assertEqual(rebound["status"], "unknown", rebound)

    def test_expired_quote_cannot_rebind_its_original_negative_parent(self):
        self.private_hold()
        parent = inventory.capture_inventory_availability(self.client_row.pk, now=self.now)
        future = self.proposal.expires_at + timedelta(seconds=1)
        IgCheckoutInventoryReservation.objects.filter(proposal=self.proposal).update(
            expires_at=future + timedelta(minutes=5))
        rebound = inventory.capture_owned_checkout_inventory(self.client_row.pk, parent,
            proposal_id=self.proposal.pk, now=future)
        self.assertEqual(rebound["status"], "unknown", rebound)

    def test_captured_normal_inventory_without_eligible_holds_performs_no_extra_sql(self):
        self.stock(1)
        cart = self.cart()
        for quantity in (1, 0):
            self.stock(quantity)
            captured = inventory.capture_inventory_availability(self.client_row.pk, current_source_cart=cart, now=self.now)
            with self.subTest(quantity=quantity), self.assertNumQueries(0):
                self.assertFalse(self.held_ids(captured=captured, cart=cart))

    def test_native_admin_and_shared_context_keep_existing_read_bounds_with_owned_unit(self):
        self.private_hold()
        from management.services.ig_admin_state_capture import current_admin_state, MAX_READ_QUERIES
        from management.services.ig_restock_waiting_list import waiting_list_projection
        with CaptureQueriesContext(connection) as queries:
            captured = current_admin_state(self.client_row.pk, now=self.now)
        self.assertEqual(captured.status, "captured", captured.as_dict())
        self.assertLessEqual(sum(row["sql"].lstrip().upper().startswith("SELECT") for row in queries), MAX_READ_QUERIES)
        self.assertEqual(captured.read_queries, MAX_READ_QUERIES)
        slot = captured.state.as_dict()["slots"][inventory.SLOT]
        self.assertEqual((slot["status"], slot["authority"], slot["omission_reason"]),
            ("unknown", "none", "inventory_query_bound"))
        self.assertIsNone(slot["value"])
        with CaptureQueriesContext(connection) as queries:
            shared = waiting_list_projection(self.client_row.pk, now=self.now, limit=1,
                include_manager_review=False, max_read_queries=24)
        self.assertLessEqual(len(queries), 24)
        self.assertLessEqual(shared["diagnostics"]["read_queries"], 24)

    def test_expired_quote_cannot_borrow_its_still_protected_hold(self):
        self.private_hold()
        self.now = self.proposal.expires_at + timedelta(seconds=1)
        IgCheckoutInventoryReservation.objects.filter(proposal=self.proposal).update(
            expires_at=self.now + timedelta(minutes=5))
        self.assertFalse(self.held_ids())

    def test_zero_physical_capacity_cannot_be_replaced_by_the_hold(self):
        self.private_hold()
        self.stock(0)
        self.assertFalse(self.held_ids())

    def test_unknown_inventory_mapping_keeps_manager_review(self):
        self.private_hold()
        from product_catalog.models import ProductInventoryPolicy
        ProductInventoryPolicy.objects.filter(product=self.product).update(source="warehouse")
        self.assertFalse(self.held_ids())

    def test_changed_quantity_keeps_manager_review(self):
        self.private_hold()
        self.canonical("Кількість 2")
        self.assertEqual(self.cart()["lines"][0]["fields"]["quantity"]["value"], 2)
        self.assertFalse(self.held_ids())

    def test_changed_color_keeps_configuration_review(self):
        self.private_hold()
        self.canonical("Колір pink")
        self.assertEqual(self.cart()["lines"][0]["fields"]["color"]["value"], "pink")
        self.assertFalse(self.held_ids())

    def test_added_other_sku_cannot_share_the_original_quote_hold(self):
        self.private_hold()
        self.canonical("добавьте Storm, количество 1, размер L, цвет pink, крій classic")
        self.assertEqual(len(self.cart()["lines"]), 2)
        self.assertFalse(self.held_ids())

    def test_foreign_hold_remains_protected_when_only_one_physical_unit_remains(self):
        self.private_hold()
        self.stock(2)
        from management.services.ig_checkout import create_or_update_proposal
        other = IgClient.get_or_create_for_sender("foreign-own-hold-consumer")
        create_or_update_proposal(client=other, pay_type="online_full", item_specs=[{
            "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1,
            "size": "M", "fit_option_code": "classic"}])
        self.stock(1)
        self.assertFalse(self.held_ids())
        self.assertEqual(IgCheckoutInventoryReservation.objects.filter(state="active").count(), 2)

    def test_source_edit_between_witnesses_cannot_reuse_a_stale_quote(self):
        self.private_hold()
        from management.models import InstagramBotMessage
        original = inventory._owned_checkout_witness
        calls = []
        def edited(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                InstagramBotMessage.objects.filter(pk=self.choice.pk).update(text="changed original color choice")
            return original(*args, **kwargs)
        with patch.object(inventory, "_owned_checkout_witness", side_effect=edited):
            self.assertFalse(self.held_ids())
        self.assertGreaterEqual(len(calls), 2)
