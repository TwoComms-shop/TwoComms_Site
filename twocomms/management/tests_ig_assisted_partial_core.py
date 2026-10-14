"""Scope/ancestry proofs through actual source, card and paid producers."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import uuid

from django.test import TransactionTestCase, override_settings
from django.db import transaction
from unittest.mock import patch

from management.ig_assisted_commerce_models import IgAssistedCommerceRequest as Request, IgAssistedCommerceDecision as Decision
from management.models import IgCheckoutProposal, IgClient, IgCheckoutInventoryReservation
from management.services import ig_assisted_commerce as commerce
from management.services import ig_assisted_scopes as scopes
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeAssistedPartialCoreTests(PartialCommerceFixture, TransactionTestCase):
    def test_native_catalog_selected_quote_does_not_borrow_deferred_recipient_quantity_discount(self):
        self.category.slug = "tshirts"
        self.category.save(update_fields=["slug"])
        self.product.slug, self.product.price = "225-tshirt", 880
        self.product.save(update_fields=["slug", "price"])
        self.variant.price_override, self.variant.stock, self.rule.stock = 880, 2, 2
        self.variant.save(update_fields=["price_override", "stock"])
        self.rule.save(update_fields=["stock"])
        self.client_row = IgClient.objects.create(igsid="partial-tier-actual-owner", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
        source = self.canonical(f"добавьте {self.product.title} для друга, количество 1, размер M, цвет black, крій classic")
        self.revision_row, _token = self.revision(source)
        request, _operation, context = self.produced_request()
        items = context["editable_items"]
        self.assertEqual([item["unit_price"] for item in items], ["800.00", "800.00"])
        terms = {scope: {"payer": "customer", "customer_maximum": "100.00", "currency": "UAH"}
            for scope in ("now", "later", "together")}
        for index, item in enumerate(items):
            item.update(availability="ready" if index == 0 else "wait", checked_quantity=1 if index == 0 else None,
                supply_basis="warehouse_checked" if index == 0 else "incoming_confirmed")
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed", "answer": "Перевірено"}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        draft = self.post({"action": "draft", "request_id": str(request.pk), "operation_id": str(uuid.uuid4()),
            **context["cas"], "items": items, "shipping_terms": terms})
        self.assertEqual(draft.status_code, 200, draft.content)
        approved = self.post({"action": "approve", "request_id": str(request.pk), "operation_id": str(uuid.uuid4()),
            **draft.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        decision = Decision.objects.get(pk=approved.json()["decision_id"])
        quote = decision.envelope["snapshot"]["scoped_quotes"]["selected"]
        self.assertEqual(quote["total"], "880.00")
        self.assertEqual(quote["lines"][0]["unit_price"], "880.00")
        self.assertEqual(decision.approved_total, Decimal("800.00"))
        from management.services.ig_checkout import validate_checkout_items
        ordinary = validate_checkout_items(client=self.client_row, item_specs=[{
            "product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1,
            "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}}])
        self.assertEqual(ordinary.quoted_total, Decimal(quote["total"]))
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_native_partial_decision_seals_price_and_all_explicit_shipping_buckets(self):
        result = self.partial_case()
        decision = result["decision"]
        quote = decision.envelope["snapshot"]["scoped_quotes"]["selected"]
        self.assertEqual((quote["scope"], quote["total"], quote["custom_quantity"]), ("partial", "1590.00", 1))
        self.assertEqual(len(quote["selected_line_ids"]), 1)
        self.assertEqual(len(quote["deferred_line_ids"]), 1)
        self.assertIsNone(decision.envelope["snapshot"]["items"][1]["dispatch"])
        self.assertEqual(set(quote["shipping_terms"]), {"now", "later", "together"})
        self.assertEqual(quote["shipping_terms"]["together"], {"payer": "merchant", "customer_maximum": "0.00", "currency": "UAH"})
        self.assertTrue(commerce.verify_assisted_evidence(decision, "decision"))
        self.assertFalse(IgCheckoutProposal.objects.exists())

    def test_shipping_input_has_no_payer_or_maximum_defaults_or_nonfinite_amounts(self):
        result = self.partial_case()
        terms = result["decision"].envelope["snapshot"]["scoped_quotes"]["selected"]["shipping_terms"]
        for changes in ({"payer": "unknown"}, {"customer_maximum": True}, {"customer_maximum": "NaN"},
                {"customer_maximum": "Infinity"}, {"currency": "USD"}, {"customer_delivery_cap": "10.00"}):
            with self.subTest(changes=changes):
                raw = deepcopy(terms)
                raw["now"].update(changes)
                with self.assertRaises(commerce.AssistedDecisionError):
                    scopes.parse_shipping_terms(raw)
        for raw in ({}, {"now": terms["now"]}, {**terms, "later": {"payer": "merchant", "currency": "UAH"}},
                {**terms, "later": {"payer": "merchant", "customer_maximum": "1.00", "currency": "UAH"}}):
            with self.subTest(raw=raw), self.assertRaises(commerce.AssistedDecisionError):
                scopes.parse_shipping_terms(raw)

    def test_actual_sent_card_selection_conserves_full_intention_without_charging_wait(self):
        result = self.selected_case()
        value = result["selection"]
        self.assertIsInstance(value, scopes.VerifiedAssistedSelection)
        self.assertEqual(value.approved_total, Decimal("1590.00"))
        self.assertEqual(value.approved_custom_quantity, 1)
        self.assertEqual(value.choice_source_id, result["source"].pk)
        self.assertEqual(value.purchase_source_id, result["source"].pk)
        self.assertEqual(len(value.lines), 1)
        self.assertEqual(len(value.decision.lines), 2)
        self.assertEqual(value.coverage["inflight_line_ids"], [])
        self.assertEqual(value.coverage["selected_line_ids"], value.selected_line_ids)
        self.assertEqual(value.coverage["deferred_line_ids"], value.deferred_line_ids)
        self.assertEqual(set(value.coverage["original_quantities"]), set(value.selected_line_ids + value.deferred_line_ids))
        self.assertEqual(set(scopes.selection_evidence(value)), set(scopes.SELECTION_EVIDENCE_KEYS))
        self.assertFalse(IgCheckoutProposal.objects.exists())
        with self.assertRaisesMessage(commerce.AssistedDecisionError, "verified_selection_requires_native_producer"):
            replace(value, _issuer=object())

    def test_native_selection_read_reuses_parent_once_and_closes_the_read_unit(self):
        from management.services import ig_assisted_partial as workflow
        result = self.selected_case()
        choice = workflow.verified_partial_choice_for_source(result["source"].pk, now=self.now)
        units = []
        original_close = scopes.AssistedSelectionReadUnit.close
        def close(unit):
            units.append(unit)
            return original_close(unit)
        with patch.object(commerce, "load_verified_assisted_decision", wraps=commerce.load_verified_assisted_decision) as reader, \
                patch.object(scopes.AssistedSelectionReadUnit, "close", autospec=True, side_effect=close):
            fresh = scopes.verified_selection_from_choice(choice, actor=self.manager, now=self.now)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(scopes.selection_evidence(fresh), scopes.selection_evidence(result["selection"]))
        self.assertEqual(len(units), 1)
        with self.assertRaisesMessage(commerce.AssistedDecisionError, "selection_read_unit_inactive"):
            units[0].coverage(result["request"], now=self.now)
        with self.assertRaisesMessage(commerce.AssistedDecisionError, "native_selection_read_unit_required"):
            scopes.AssistedSelectionReadUnit(self.client_row.pk, self.now, object())

    def test_together_choice_is_honest_wait_without_selected_purchase_capability(self):
        result = self.selected_case(action="together")
        self.assertIsNone(result["selection"])
        self.assertFalse(IgCheckoutProposal.objects.exists())
        coverage = scopes.intention_coverage(self.client_row.pk, result["request"].intention_id, now=self.now)
        self.assertTrue(coverage["ready"], coverage)
        self.assertEqual(coverage["selected_line_ids"], [])
        self.assertEqual(coverage["settled_line_ids"], [])
        self.assertEqual(len(coverage["deferred_line_ids"]), 2)

    def test_actual_paid_selected_scope_then_resume_has_fresh_R2_same_intention_and_no_reused_readiness(self):
        first = self.selected_case()
        prepared = self.prepare_selection(first["selection"])
        order = self.pay_prepared(prepared)
        old_order = list(order.items.values("id", "qty", "size", "unit_price"))
        remainder, _adoption, _task, source, _revision, _token = self.resume_case(first["request"])
        self.assertEqual(remainder.intention_id, first["request"].intention_id)
        self.assertEqual(remainder.version, first["request"].version + 1)
        self.assertEqual(remainder.source_id, source.pk)
        self.assertEqual(remainder.envelope["snapshot"]["ancestry"]["root_request_id"], str(first["request"].pk))
        self.assertFalse(Decision.objects.filter(request=remainder).exists())
        items = commerce._editable_items(remainder.envelope["snapshot"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["availability"], "clarify")
        self.assertIsNone(items[0]["checked_quantity"])
        self.assertIsNone(items[0]["image_id"])
        self.assertIsNone(items[0]["unit_price"])
        self.assertEqual(list(order.items.values("id", "qty", "size", "unit_price")), old_order)
        verified = scopes.validate_request_ancestry(remainder, client=self.client_row, now=self.now)
        self.assertTrue(verified["ready"], verified)
        self.assertEqual(verified["coverage"]["settled_line_ids"], first["selection"].selected_line_ids)

    def test_native_residual_source_completion_rebinds_only_bookkeeping_fence(self):
        first = self.selected_case()
        self.pay_prepared(self.prepare_selection(first["selection"]))
        remainder, _adoption, _task, source, _revision, _token = self.resume_case(first["request"])
        original = deepcopy(remainder.envelope)
        source.status = "done"
        source.save(update_fields=["status"])
        with transaction.atomic():
            client = IgClient.objects.select_for_update().get(pk=self.client_row.pk)
            data, _task, current = commerce._current_request(remainder, client, self.now)
        self.assertEqual(data, original["snapshot"])
        self.assertNotEqual(current["fence"]["source_digest"], original["snapshot"]["full_capture"]["fence"]["source_digest"])
        self.assertEqual(commerce._configuration_capture(current),
            commerce._configuration_capture(original["snapshot"]["full_capture"]))
        changed = deepcopy(current)
        changed["capture_digest"] = original["snapshot"]["full_capture"]["capture_digest"]
        changed["fence"]["source_digest"] = original["snapshot"]["full_capture"]["fence"]["source_digest"]
        self.assertEqual(changed, original["snapshot"]["full_capture"])
        remainder.refresh_from_db()
        self.assertEqual(remainder.envelope, original)
        self.assertFalse(Decision.objects.filter(request=remainder).exists())
        # Failed source evidence never receives the completion exception.
        source.status = "failed"
        source.save(update_fields=["status"])
        with transaction.atomic(), self.assertRaises(commerce.AssistedDecisionError):
            commerce._current_request(remainder, IgClient.objects.select_for_update().get(pk=self.client_row.pk), self.now)

    def test_actual_two_paid_scopes_have_disjoint_complete_quantity_coverage(self):
        first = self.selected_case()
        order1 = self.pay_prepared(self.prepare_selection(first["selection"]))
        second = self.paid_remainder_case(first["request"])
        coverage = scopes.intention_coverage(self.client_row.pk, first["request"].intention_id, now=self.now)
        self.assertTrue(coverage["ready"], coverage)
        self.assertEqual(set(coverage["settled_line_ids"]), set(coverage["original_quantities"]))
        self.assertEqual(coverage["inflight_line_ids"], [])
        self.assertEqual(coverage["deferred_line_ids"], [])
        self.assertEqual({row["order_id"] for row in coverage["orders"]}, {order1.pk, second["order"].pk})
        self.assertEqual(second["selection"].delivery_scope, "later")
        self.assertFalse(set(first["selection"].selected_line_ids) & set(second["selection"].selected_line_ids))
        self.assertEqual(second["selection"].line_map[0]["current_index"], 0)
        self.assertEqual(second["selection"].line_map[0]["original_index"], 1)
        denied = scopes.admit_selected_scope(first["selection"], now=self.now)
        self.assertFalse(denied["ready"], denied)


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativeAssistedPartialCatalogRecountTests(PartialCommerceFixture, TransactionTestCase):
    def partial_case(self):
        # Keep the base fixture's genuine Black/M catalog wish and zero pool.
        source = self.canonical("добавьте Storm, количество 1, размер L, цвет olive, крій classic")
        self.revision_row, _claim = self.revision(source)
        request, _operation, context = self.produced_request()
        items = context["editable_items"]
        self.assertEqual([item["item_kind"] for item in items], ["catalog_apparel", "custom_apparel"])
        items[0].update(availability="ready", checked_quantity=1, supply_basis="warehouse_checked",
            source_location="Менеджер фізично перевірив Black/M")
        items[1].update(title="Оливкове худі", unit_price="1490.00", availability="wait",
            checked_quantity=None, supply_basis="incoming_confirmed")
        for item in items:
            item["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed", "answer": "Перевірено"}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == item["line_id"]]
        terms = {"now": {"payer": "customer", "customer_maximum": "120.00", "currency": "UAH"},
            "later": {"payer": "customer", "customer_maximum": "150.00", "currency": "UAH"},
            "together": {"payer": "merchant", "customer_maximum": "0.00", "currency": "UAH"}}
        draft = self.post({"action": "draft", "request_id": str(request.pk), "operation_id": str(uuid.uuid4()),
            **context["cas"], "items": items, "shipping_terms": terms})
        self.assertEqual(draft.status_code, 200, draft.content)
        approved = self.post({"action": "approve", "request_id": str(request.pk), "operation_id": str(uuid.uuid4()),
            **draft.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, approved.content)
        decision = Decision.objects.get(pk=approved.json()["decision_id"])
        return {"request": request, "decision": decision, "original_revision": self.revision_row,
            "commerce": approved.json()["commerce"]}

    def test_real_zero_catalog_and_custom_wait_choice_recount_then_hosted_paid_catalog_only(self):
        from management.services import ig_assisted_stock_sync as stock
        from management.services.ig_assisted_checkout import prepare_assisted_checkout
        from orders.models import Order, PaymentAttempt
        result = self.selected_case()
        selection, request, decision = result["selection"], result["request"], result["decision"]
        self.assertEqual(selection.lines[0].item_kind, "catalog_apparel")
        self.assertEqual(len(selection.deferred_line_ids), 1)
        before = deepcopy(request.envelope), deepcopy(decision.envelope)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        with patch("management.services.instagram_bot._provider_http") as provider:
            blocked = prepare_assisted_checkout(self.client_row.pk, actor=self.manager, request_id=request.pk,
                decision_id=decision.pk, expected_request_digest=request.envelope_hmac,
                expected_decision_digest=decision.envelope_hmac, operation_id=uuid.uuid4(),
                revision_id=result["revision"].pk, settings_id=self.settings.pk, verified_selection=selection)
        provider.assert_not_called()
        self.assertFalse(blocked["ready"], blocked)
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        preview = stock.projection(self.client_row.pk, actor=self.manager, request_id=request.pk,
            decision_id=decision.pk, now=self.now)
        self.assertTrue(preview["eligible"], preview)
        self.assertEqual(len(preview["pools"]), 1)
        self.assertEqual(preview["pools"][0]["line_ids"], selection.current_selected_line_ids)
        counted = self.post({"action": "stock_sync", "request_id": str(request.pk), "decision_id": str(decision.pk),
            "operation_id": str(uuid.uuid4()), "expected_request_digest": request.envelope_hmac,
            "expected_decision_digest": decision.envelope_hmac, "expected_preview_digest": preview["preview_digest"],
            "counts_by_pool": {preview["pools"][0]["pool_id"]: {"total": 1, "size_counts": {str(self.rule.pk): 1}}}})
        self.assertEqual(counted.status_code, 200, counted.content)
        request.refresh_from_db()
        decision.refresh_from_db()
        self.assertEqual((request.envelope, decision.envelope), before)
        prepared = self.prepare_selection(selection)
        proposal = prepared["proposal"]
        self.assertEqual(list(proposal.items.values_list("product_id", flat=True)), [self.product.pk])
        reservation = proposal.inventory_reservations.get()
        self.assertEqual((reservation.quantity, reservation.state, reservation.allocation_source),
            (1, IgCheckoutInventoryReservation.State.ACTIVE, "catalog_variant"))
        order = self.pay_prepared(prepared)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(list(order.items.values_list("product_id", "is_custom", "qty")), [(self.product.pk, False, 1)])
        self.assertEqual(order.total_sum, selection.approved_total)
        from management.services.ig_inventory import protected_inventory_reservation_q
        reservations = IgCheckoutInventoryReservation.objects.filter(proposal=proposal)
        self.assertEqual(set(reservations.values_list("allocation_source", flat=True)), {"catalog_variant"})
        self.assertEqual(set(reservations.values_list("product_id", flat=True)), {self.product.pk})
        protected = reservations.filter(protected_inventory_reservation_q(self.now))
        self.assertFalse(protected.exists())
        self.assertEqual(list(reservations.filter(state=IgCheckoutInventoryReservation.State.FULFILLED)
            .values_list("quantity", "fulfilled_quantity")), [(1, 1)])
        selected_proposal_item_ids = list(proposal.items.order_by("pk").values_list("pk", flat=True))
        self.assertEqual(len(selected_proposal_item_ids), len(selection.selected_line_ids))
        for row in reservations:
            self.assertEqual(row.line_ids, selected_proposal_item_ids)
        self.assertTrue(reservations.filter(state=IgCheckoutInventoryReservation.State.RELEASED).exists())
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))
        coverage = scopes.intention_coverage(self.client_row.pk, request.intention_id, now=self.now)
        self.assertTrue(coverage["ready"], coverage)
        self.assertEqual(coverage["settled_line_ids"], selection.selected_line_ids)
        self.assertEqual(coverage["deferred_line_ids"], selection.deferred_line_ids)
        self.assertEqual(coverage["inflight_line_ids"], [])
