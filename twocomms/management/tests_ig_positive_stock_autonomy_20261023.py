"""Exact normal retail stock grants progress without a manager or effects."""
from copy import deepcopy
from datetime import timedelta
from dataclasses import replace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.tests_ig_restock_subscriptions import _RestockFixture
from management.services import ig_inventory_availability as inventory
from management.services.ig_reply_authority import apply_inventory_availability_context
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.models import IgFollowUpTask, IgCheckoutInventoryReservation
from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
from product_catalog.models import ProductInventoryPolicy, VariantBlankLink, VariantSizeRule
from warehouse.models import StorageCategory, StorageSubcategory, StockItem


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativePositiveStockAutonomyTests(_RestockFixture, TestCase):
    def positive(self, quantity=5):
        self.rule.stock = self.variant.stock = quantity
        self.rule.save(update_fields=["stock", "updated_at"])
        self.variant.save(update_fields=["stock"])

    def cart(self):
        result = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(result["status"], "captured", result)
        return result

    def observe(self, *, standalone=False):
        cart = None if standalone else self.cart()
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            result = inventory.capture_inventory_availability(self.client_row.pk, current_source_cart=cart, now=self.now)
        provider.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries), queries)
        self.assertTrue(inventory.verified_inventory_capture(result))
        self.assertEqual(result["status"], "captured", result)
        return result

    def test_exact_positive_stock_is_readonly_and_normal_selection_needs_no_manager(self):
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        self.positive()
        capture = self.observe()
        self.assertEqual([row["status"] for row in capture["lines"]], ["available"])
        revision, _ = self.revision(self.choice)
        result = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertEqual(result["reason"], "inventory_available", result)
        self.assertFalse(result["ready"])
        self.assertFalse(IgFulfillmentReviewRequest.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        context = apply_inventory_availability_context(ReplyTruthContext(), capture)
        self.assertEqual(context.fulfillment_review_status, "")
        self.assertEqual(context.inventory_availability_authorities[0].supply_basis, "inventory_observation")
        self.assertTrue(validate_reply_truth("Restock Aurora, black, size M is in stock.", context=context).valid)
        self.assertFalse(validate_reply_truth("Restock Aurora, white, size M is in stock.", context=context).valid)
        self.assertFalse(validate_reply_truth("Restock Aurora, black, size L is in stock.", context=context).valid)
        self.assertFalse(validate_reply_truth("Restock Aurora, black, size M is not available.", context=context).valid)
        self.assertFalse(validate_reply_truth("It will arrive in 7 days.", context=context).valid)
        for prefix in ("The manager already checked: ", "Менеджер уже перевірив склад: ", "I physically checked: ",
                "Я фізично перевірив склад: ", "Я физически проверил склад: "):
            self.assertFalse(validate_reply_truth(prefix + "Restock Aurora, black, size M is in stock.", context=context).valid)

    def test_price_subject_metadata_is_exact_per_line_and_cleared_without_capture(self):
        from dataclasses import replace
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_reply_truth import ReplyTruthContext
        self.assertEqual(ReplyTruthContext().source_price_subjects, ())
        source = self.source("Яка ціна?")
        revision, _ = self.revision(source)
        plan = capture_response_plan(self.client_row, revision=revision)
        contexts = plan.line_truth_contexts(ReplyTruthContext())
        context, = contexts.values()
        subject, = context.source_price_subjects
        self.assertEqual(subject.product_id, self.product.pk)
        self.assertEqual(subject.capture_digest, plan.source_cart_capture["capture_digest"])
        self.assertTrue(subject.source_message_ids)
        self.assertEqual(plan.truth_context(context).source_price_subjects, ())
        self.assertTrue(all(not item.source_price_subjects for item in
            replace(plan, source_cart_capture={}).line_truth_contexts(context).values()))
        self.assertNotIn("source_price_subjects", str(plan.prompt_projection()))
        self.assertNotIn("product_kind", str(plan.prompt_projection()))
        self.assertNotIn("product_kind", str(replace(plan, line_plans=()).prompt_projection()))
        variant = self.variants[1]
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical(f"добавьте {self.products[1].title}, количество 1, размер L, цвет pink, крій classic")
        source = self.source("Яка ціна обох?")
        revision, _ = self.revision(source)
        multi = capture_response_plan(self.client_row, revision=revision)
        derived = multi.line_truth_contexts(context)
        self.assertEqual(len(derived), 2)
        for line in multi.line_plans:
            row = line.as_dict()
            captured, = derived[line.line_id].source_price_subjects
            self.assertEqual((captured.line_id, captured.product_id), (line.line_id, row["choices"]["product_id"]))
            self.assertEqual(captured.title, row["configuration"]["product_title"])

    def test_reported_or_historical_notification_words_do_not_force_positive_stock_review(self):
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        self.positive()
        for text in ('Менеджер написав: «Повідомте, коли ця позиція буде в наявності».',
                'Раніше просила повідомте, коли ця позиція буде в наявності.'):
            revision, _ = self.revision(self.source(text))
            result = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
            self.assertFalse(result["created"], result)
            self.assertFalse(IgFulfillmentReviewRequest.objects.exists())
        self.assertEqual(self.observe()["lines"][0]["status"], "available")

    def test_genuine_payment_confirmation_does_not_claim_a_stock_check(self):
        context = ReplyTruthContext(payment_confirmed=True)
        for text in ("Менеджер підтвердив оплату.", "Manager confirmed payment."):
            self.assertNotIn("unverified_availability", validate_reply_truth(text, context=context).reasons)

    def test_zero_unknown_and_missing_mapping_never_grant_negative_stock_authority(self):
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        capture = self.observe()
        self.assertEqual(capture["lines"][0]["status"], "check_required")
        context = apply_inventory_availability_context(ReplyTruthContext(), capture)
        self.assertFalse(context.inventory_availability_authorities)
        self.assertFalse(validate_reply_truth("Restock Aurora, black, size M is not available.", context=context).valid)
        revision, _ = self.revision(self.source("Чи є M у наявності?"))
        result = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(result["ready"], result)
        self.assertEqual(IgFulfillmentReviewRequest.objects.count(), 1)
        self.positive()
        policy = ProductInventoryPolicy.objects.get(product=self.product)
        policy.source = "warehouse"
        policy.save(update_fields=["source", "updated_at"])
        missing = self.observe()
        self.assertEqual(missing["lines"][0]["status"], "check_required")

    def warehouse(self, quantity=1):
        policy = ProductInventoryPolicy.objects.get(product=self.product)
        policy.source = "warehouse"
        policy.save(update_fields=["source", "updated_at"])
        category = StorageCategory.objects.create(name="Retail inventory", slug="retail-inventory")
        subcategory = StorageSubcategory.objects.create(category=category, name="Retail classic", slug="retail-classic")
        VariantBlankLink.objects.create(variant=self.variant, option_key="fit=classic", storage_subcategory=subcategory)
        self.stock = StockItem.objects.create(subcategory=subcategory, color=self.variant.color, size="M", quantity=quantity)
        return subcategory

    def test_same_blank_pool_cannot_cover_two_different_prints_with_one_unit(self):
        subcategory = self.warehouse()
        second, variant = self.products[1], self.variants[1]
        variant.color = self.variant.color
        variant.save(update_fields=["color"])
        ProductInventoryPolicy.objects.create(product=second, source="warehouse")
        VariantBlankLink.objects.create(variant=variant, option_key="fit=classic", storage_subcategory=subcategory)
        self.canonical(f"добавьте {second.title}, количество 1, размер M, цвет black, крій classic")
        capture = self.observe()
        self.assertEqual(len(capture["lines"]), 2)
        self.assertEqual({row["pool_id"] for row in capture["lines"]}, {f"warehouse:{self.stock.pk}"})
        self.assertEqual({row["status"] for row in capture["lines"]}, {"check_required"})
        self.stock.quantity = 2
        self.stock.save(update_fields=["quantity"])
        self.assertEqual({row["status"] for row in self.observe()["lines"]}, {"available"})

    def test_distinct_m_l_units_can_cover_two_requested_sizes(self):
        from management.services.ig_admin_state_capture import current_admin_state
        self.warehouse()
        StockItem.objects.create(subcategory=self.stock.subcategory, color=self.variant.color, size="L", quantity=1)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер L, цвет black, крій classic")
        capture = self.observe()
        self.assertEqual(len(capture["lines"]), 2)
        self.assertEqual({row["status"] for row in capture["lines"]}, {"available"})
        self.assertEqual(len({row["pool_id"] for row in capture["lines"]}), 2)
        observations = []
        original = inventory.capture_inventory_availability
        def recorded(*args, **kwargs):
            value = original(*args, **kwargs)
            observations.append(value)
            return value
        with patch.object(inventory, "capture_inventory_availability", side_effect=recorded):
            admin = current_admin_state(self.client_row.pk, now=self.now)
        self.assertEqual(admin.status, "captured", admin)
        slot = admin.state.as_dict()["slots"][inventory.SLOT]
        self.assertEqual(slot["status"], "confirmed", (admin.read_queries, capture["query_count"], observations, slot))
        self.assertEqual({row["status"] for row in slot["value"]["lines"]}, {"available"})

    def test_positive_warehouse_with_zero_ledger_size_reaches_quote(self):
        from management.services.ig_revision_checkout import checkout_authority_control
        self.warehouse(quantity=2)
        self.assertEqual(self.rule.stock, 0)
        from product_catalog.size_grid_services import resolve_effective_sizes
        self.assertNotIn("M", {row["size"] for row in resolve_effective_sizes(self.product, "fit=classic", variant=self.variant)})
        self.client_row.refresh_from_db()
        control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=self.cart())
        self.assertFalse(reasons, reasons)
        self.assertEqual(len(control["items"]), 1)
        self.assertFalse(IgFulfillmentReviewRequest.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        from product_catalog.services import variant_allows_purchase, variant_allows_size
        self.assertFalse(variant_allows_purchase(self.product, self.variant, fit_code="classic", size="M"))
        self.assertFalse(variant_allows_size(self.variant, "M", "classic"))
        from management.services.ig_checkout import create_or_update_proposal
        proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full", item_specs=control["items"],
            source_cart_binding=control["source_cart_binding"], source_cart_capture=control["source_cart_capture"],
            checkout_owner_scope=control["checkout_owner_scope"])
        self.assertEqual(proposal.items.count(), 1)
        hold = proposal.inventory_reservations.get()
        self.assertEqual((hold.allocation_source, hold.stock_item_id, hold.quantity), ("warehouse", self.stock.pk, 1))

    def test_catalogue_zero_size_stays_blocked_for_quote(self):
        from management.services.ig_revision_checkout import checkout_authority_control
        self.client_row.refresh_from_db()
        control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=self.cart())
        self.assertTrue(reasons)
        self.assertFalse(control)
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())

    def test_unknown_sibling_keeps_positive_exact_line_and_only_that_manager_scope(self):
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        self.positive()
        second, variant = self.products[1], self.variants[1]
        variant.stock = 0
        variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=second, source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=0, is_enabled=True)
        source = self.canonical(f"добавьте {second.title}, количество 1, размер L, цвет pink, крій classic")
        capture = self.observe()
        self.assertEqual([row["status"] for row in capture["lines"]], ["available", "check_required"])
        revision, _ = self.revision(source)
        result = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(result["ready"], result)
        request = IgFulfillmentReviewRequest.objects.get(pk=result["request_id"])
        self.assertEqual([row["target"]["product_id"] for row in request.envelope["lines"]], [second.pk])
        context = apply_inventory_availability_context(ReplyTruthContext(), capture)
        self.assertTrue(validate_reply_truth("Restock Aurora, black, size M is in stock.", context=context).valid)
        self.assertFalse(validate_reply_truth("All items are in stock.", context=context).valid)

    def test_current_configuration_stock_and_source_changes_invalidate_frozen_fact(self):
        self.positive()
        captured = self.observe(standalone=True)
        self.assertEqual(inventory.validate_inventory_capture(self.client_row.pk, captured, now=self.now), "")
        self.variant.stock = 4
        self.variant.save(update_fields=["stock"])
        self.assertEqual(inventory.validate_inventory_capture(self.client_row.pk, captured, now=self.now), "inventory_availability_changed")
        tampered = deepcopy(captured)
        tampered["lines"][0]["target"]["quantity"] = 9
        self.assertFalse(inventory.verified_inventory_capture(tampered))
        self.assertFalse(apply_inventory_availability_context(ReplyTruthContext(), tampered).inventory_availability_authorities)

    def test_live_and_paid_protected_holds_reduce_available_quantity(self):
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        self.positive(2)
        other = IgClient.get_or_create_for_sender("retail-other-reservation")
        proposal = create_or_update_proposal(client=other, pay_type="online_full", item_specs=[{
            "product_id": self.product.pk, "color_variant_id": self.variant.pk,
            "qty": 2, "size": "M", "fit_option_code": "classic"}])
        reservation = proposal.inventory_reservations.get(state="active")
        self.assertEqual(self.observe()["lines"][0]["status"], "check_required")
        IgCheckoutInventoryReservation.objects.filter(pk=reservation.pk).update(
            state="paid_committed", expires_at=self.now - timedelta(days=1))
        self.assertEqual(self.observe()["lines"][0]["status"], "check_required")

    def test_catalogue_pool_and_exact_size_caps_cover_m_and_l_independently(self):
        from management.services.ig_admin_state_capture import current_admin_state
        self.positive(2)
        self.rule.stock = 1
        self.rule.save(update_fields=["stock", "updated_at"])
        VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="L", stock=1, is_enabled=True)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер L, цвет black, крій classic")
        capture = self.observe()
        self.assertEqual([row["status"] for row in capture["lines"]], ["available", "available"])
        admin = current_admin_state(self.client_row.pk, now=self.now)
        self.assertEqual(admin.status, "captured", admin)
        slot = admin.state.as_dict()["slots"][inventory.SLOT]
        self.assertEqual(slot["status"], "confirmed", (admin.read_queries, slot))

    def test_pending_zero_review_does_not_hide_restocked_positive_inventory(self):
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        source = self.source("Чи є M у наявності?")
        revision, _ = self.revision(source)
        initial = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(initial["ready"], initial)
        self.positive()
        source = self.source("Чи є M у наявності зараз?")
        revision, _ = self.revision(source)
        result = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertEqual(result["reason"], "inventory_available", result)
        self.assertEqual(self.observe()["lines"][0]["status"], "available")
        self.assertEqual(IgFulfillmentReviewRequest.objects.count(), 1)

    def test_two_positive_catalogue_items_reach_ordinary_quote_and_proposal_without_review(self):
        from django.db import transaction
        from management.services.ig_revision_checkout import checkout_authority_control
        from management.services.ig_checkout import create_or_update_proposal
        self.positive(2)
        VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="L", stock=1, is_enabled=True)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер L, цвет black, крій classic")
        self.client_row.refresh_from_db()
        cart = self.cart()
        control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=cart)
        self.assertFalse(reasons, reasons)
        self.assertEqual(len(control["items"]), 2)
        self.assertFalse(IgFulfillmentReviewRequest.objects.exists())
        with transaction.atomic(), patch("management.services.instagram_bot._provider_http") as provider:
            proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full",
                item_specs=control["items"], source_cart_binding=control["source_cart_binding"],
                source_cart_capture=control["source_cart_capture"], checkout_owner_scope=control["checkout_owner_scope"])
        provider.assert_not_called()
        self.assertEqual(proposal.items.count(), 2)
        self.assertEqual(sum(proposal.inventory_reservations.values_list("quantity", flat=True)), 2)
        self.assertFalse(proposal.payment_attempt_id)
        self.assertFalse(proposal.winner_invoice_generation_id)
        self.assertFalse(IgFulfillmentReviewRequest.objects.exists())

    def test_zero_inventory_budget_performs_no_sql(self):
        with self.assertNumQueries(0):
            result = inventory.capture_inventory_availability(self.client_row.pk, max_read_queries=0, now=self.now)
        self.assertEqual(result["reason"], "inventory_query_bound")
        self.assertEqual(result["query_count"], 0)

    def test_positive_stock_question_cannot_be_discharged_by_unrelated_acknowledgement(self):
        from management.services.ig_response_plan import capture_response_plan, _pending_stock_answer_covered
        self.positive()
        source = self.source("Is this hoodie in size M in stock?")
        revision, _ = self.revision(source)
        plan = capture_response_plan(self.client_row, revision=revision)
        self.assertEqual(plan.pending_stock_questions.get("mode"), "inventory_available")
        self.assertFalse(_pending_stock_answer_covered(plan, "Thanks!", source.pk))
        self.assertTrue(_pending_stock_answer_covered(plan, "Restock Aurora, black, size M is in stock.", source.pk))

    def test_owned_checkout_hold_is_exact_and_never_excludes_foreign_holds(self):
        from django.db import transaction
        from management.models import IgClient
        from management.services.ig_revision_checkout import checkout_authority_control
        from management.services.ig_checkout import create_or_update_proposal
        self.positive(3)
        parent = self.observe()
        control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=self.cart())
        self.assertFalse(reasons)
        with transaction.atomic():
            proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full", item_specs=control["items"],
                source_cart_binding=control["source_cart_binding"], source_cart_capture=control["source_cart_capture"],
                checkout_owner_scope=control["checkout_owner_scope"])
        owned = inventory.capture_owned_checkout_inventory(self.client_row.pk, parent, proposal_id=proposal.pk, now=self.now)
        self.assertEqual(owned["status"], "captured", owned)
        self.assertTrue(inventory.verified_inventory_capture(owned))
        self.assertEqual(inventory.validate_inventory_capture(self.client_row.pk, owned, now=self.now), "")
        other = IgClient.get_or_create_for_sender("foreign-live-inventory-hold")
        create_or_update_proposal(client=other, pay_type="online_full", item_specs=[{
            "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1,
            "size": "M", "fit_option_code": "classic"}])
        self.assertEqual(inventory.validate_inventory_capture(self.client_row.pk, owned, now=self.now), "inventory_owned_parent_changed")


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=20000, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeInventoryCheckoutDeliveryTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.variant.stock = self.rule.stock = 3
        self.variant.save(update_fields=["stock"])
        self.rule.save(update_fields=["stock", "updated_at"])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = "observed"
        self.provider_failure = False
        VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="L", stock=2, is_enabled=True)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер L, цвет black, крій classic")
        self.canonical("Хочу купити")
        self.source_row = self.source("Беру. Оформлюйте замовлення.")
        self.revision, self.token = self.revision(self.source_row)
        self.parsed = {"reply_text": "Restock Aurora чорний розмір M є в наявності. Restock Aurora чорний розмір L є в наявності. Перевірте товари та деталі оплати за посиланням:",
            "controls": [{"kind": "paylink", "value": "full"}]}

    def _generate(self, *args, **kwargs):
        from management.tests_ig_revision_live import RevisionLiveTests
        return RevisionLiveTests._generate(self, *args, **kwargs)

    def execute(self):
        from management.tests_ig_revision_live import RevisionLiveTests
        with patch("management.services.ig_presence.start_presence", return_value=None):
            return RevisionLiveTests._execute(self)

    def test_positive_availability_and_checkout_create_one_owned_hold_and_send_once(self):
        from management.models import IgCheckoutProposal, IgCheckoutAccessToken
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_control import ValidatedResponse, ResponseControl
        from management.services.ig_reply_authority import build_reply_truth_context
        from management.services.ig_response_cart_plan import scoped_claim_clauses, matching_line_ids
        plan = capture_response_plan(self.customer, revision=self.revision)
        capture = inventory.capture_inventory_availability(self.customer.pk, current_source_cart=plan.source_cart_capture, now=self.now)
        context = apply_inventory_availability_context(build_reply_truth_context(self.customer), capture)
        candidate = ValidatedResponse(self.parsed["reply_text"], (ResponseControl("paylink", "full"),))
        details = [(clause, matching_line_ids(clause, plan.line_plans),
            {key: validate_reply_truth(clause, context=value).reasons for key, value in plan.line_truth_contexts(context).items()})
            for clause in scoped_claim_clauses(candidate.reply_text)]
        self.assertFalse(plan.validate_multiline_claims(candidate, context), details)
        result, generated, http = self.execute()
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts.get("response_coverage")))
        self.assertEqual((generated.call_count, http.call_count), (1, 1))
        self.assertEqual(IgCheckoutProposal.objects.count(), 1)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
        self.assertEqual(IgCheckoutInventoryReservation.objects.filter(state="active").count(), 1)
        self.assertEqual(IgCheckoutProposal.objects.get().items.count(), 2)
        self.assertEqual(IgCheckoutInventoryReservation.objects.get(state="active").quantity, 2)
        effect = self.revision.delivery_effects.get(state="sent")
        self.assertIn("є в наявності", effect.payload["message"]["text"])
        self.assertIn("https://", effect.payload["message"]["text"])
        fact = next(row for row in effect.fact_bindings if row["claim"] == "retail_inventory_availability")
        self.assertIn("inventory_availability_reference", fact["selector"])
        replay, generated, http = self.execute()
        self.assertEqual(replay.state, "completed", replay.reasons)
        generated.assert_not_called()
        http.assert_not_called()

    def test_source_change_after_owned_hold_before_transport_sends_nothing(self):
        from management.services import ig_revision_live as live
        original = live._drain_effects
        def change(*args, **kwargs):
            self.assertTrue(IgCheckoutInventoryReservation.objects.filter(state="active").exists())
            self.source("Actually please change the size to L.")
            return original(*args, **kwargs)
        with patch.object(live, "_drain_effects", side_effect=change) as drain:
            result, generated, http = self.execute()
        drain.assert_called_once()
        self.assertEqual(generated.call_count, 1)
        self.assertNotEqual(result.state, "completed")
        http.assert_not_called()

    def test_foreign_hold_after_owned_hold_before_transport_sends_nothing(self):
        from management.services import ig_revision_live as live
        from management.services.ig_checkout import create_or_update_proposal
        from management.models import IgClient
        original = live._drain_effects
        def reserve(*args, **kwargs):
            self.assertTrue(IgCheckoutInventoryReservation.objects.filter(state="active").exists())
            other = IgClient.get_or_create_for_sender("foreign-checkout-send-race")
            create_or_update_proposal(client=other, pay_type="online_full", item_specs=[{
                "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1,
                "size": "M", "fit_option_code": "classic"}])
            return original(*args, **kwargs)
        with patch.object(live, "_drain_effects", side_effect=reserve) as drain:
            result, generated, http = self.execute()
        drain.assert_called_once()
        self.assertEqual(generated.call_count, 1)
        self.assertNotEqual(result.state, "completed")
        http.assert_not_called()

    def test_stock_consumption_still_covering_own_quantity_cannot_refresh_frozen_parent(self):
        from management.services import ig_revision_live as live
        original = live._drain_effects
        def consume(*args, **kwargs):
            self.assertEqual(IgCheckoutInventoryReservation.objects.get(state="active").quantity, 2)
            type(self.variant).objects.filter(pk=self.variant.pk).update(stock=2)
            return original(*args, **kwargs)
        with patch.object(live, "_drain_effects", side_effect=consume) as drain:
            result, generated, http = self.execute()
        drain.assert_called_once()
        self.assertEqual(generated.call_count, 1)
        self.assertNotEqual(result.state, "completed")
        http.assert_not_called()
