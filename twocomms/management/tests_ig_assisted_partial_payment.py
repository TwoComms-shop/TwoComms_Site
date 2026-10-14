"""Native selected choices, paid history and uncertain finite stock obligations."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management import tests_ig_checkout_generation_runtime as generation_fixture
from management.models import IgCheckoutInvoiceGeneration as Generation, IgCheckoutInventoryReservation as Reservation, IgClient
from management.services import ig_assisted_checkout as assisted
from management.services import ig_checkout_generation as generations
from management.services import ig_inventory as inventory
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.models import Order, PaymentAttempt


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeSelectedPaymentTests(PartialCommerceFixture, TransactionTestCase):
    def invoice(self, prepared):
        proposal = prepared["proposal"]
        with override_settings(ROOT_URLCONF="twocomms.urls"):
            opened = self.client.get(prepared["checkout_url"], HTTP_HOST="twocomms.shop")
            self.assertEqual(opened.status_code, 302, opened.content)
            with patch("storefront.views.monobank._monobank_api_request", return_value={
                    "invoiceId": f"native-selected-payment-{proposal.pk}",
                    "pageUrl": f"https://pay.example/selected-{proposal.pk}"}) as bank:
                response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                    self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="1"),
                    HTTP_ACCEPT="application/json", HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 200, response.content)
        bank.assert_called_once()
        return Generation.objects.get(proposal=proposal, active_slot=1)

    def settle(self, generation):
        from storefront.views.monobank import _apply_payment_attempt_status
        with patch("management.services.instagram_bot._provider_http", return_value=(200,
                json.dumps({"message_id": f"native-selected-paid-{generation.pk}"}))):
            return _apply_payment_attempt_status(generation.payment_attempt, "success",
                payload={"paidAmount": int(generation.payment_amount * 100)}, source="provider_pull")

    def test_off_zero_native_selection_pays_only_ready_goods_and_duplicate_callback_reuses_one_order(self):
        case = self.selected_case()
        prepared = self.prepare_selection(case["selection"])
        generation = self.invoice(prepared)
        attempt = generation.payment_attempt
        self.assertEqual(generation.payment_amount, Decimal("1590.00"))
        self.assertEqual(len(attempt.cart_snapshot["cart"]), 1)
        self.assertEqual(attempt.cart_snapshot["source_cart_provenance"]["schema"], "ig-checkout-cart-provenance.v3")
        self.assertFalse(Reservation.objects.exists())
        order, created = self.settle(generation)
        self.assertTrue(created)
        self.assertEqual((order.items.get().title, order.items.get().qty, order.total_sum),
            ("Фіолетове худі", 1, Decimal("1590.00")))
        replay, created = self.settle(generation)
        self.assertFalse(created)
        self.assertEqual(replay.pk, order.pk)
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count()), (1, 1))
        self.assertTrue(assisted.validate_assisted_order(order, now=self.now)["ready"])

    def test_remainder_new_choice_and_second_payment_conserve_original_lines_and_first_order_history(self):
        from management.services.ig_assisted_scopes import intention_coverage
        case = self.selected_case()
        prepared = self.prepare_selection(case["selection"])
        first_generation = self.invoice(prepared)
        first, created = self.settle(first_generation)
        self.assertTrue(created)
        first.refresh_from_db()
        frozen = (deepcopy(first.payment_payload), list(first.items.order_by("pk").values()))
        remainder = self.paid_remainder_case(case["request"])
        second = remainder["order"]
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual((first.total_sum, second.total_sum), (Decimal("1590.00"), Decimal("1490.00")))
        self.assertEqual(second.items.get().title, "Оливкове худі")
        self.assertEqual(remainder["selection"].selected_line_ids, case["selection"].deferred_line_ids)
        coverage = intention_coverage(self.client_row.pk, case["request"].intention_id, now=self.now)
        self.assertTrue(coverage["ready"], coverage)
        self.assertEqual(set(coverage["settled_line_ids"]),
            set(case["selection"].selected_line_ids + case["selection"].deferred_line_ids))
        self.assertFalse(coverage["inflight_line_ids"])
        replay, created = self.settle(first_generation)
        self.assertFalse(created)
        self.assertEqual(replay.pk, first.pk)
        first.refresh_from_db()
        self.assertEqual((first.payment_payload, list(first.items.order_by("pk").values())), frozen)
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count()), (2, 2))
        self.assertFalse(Reservation.objects.exists())

    def test_changed_actual_choice_source_blocks_invoice_before_any_bank_request(self):
        case = self.selected_case()
        prepared = self.prepare_selection(case["selection"])
        case["source"].text += " Але хочу інший колір."
        case["source"].save(update_fields=["text"])
        with override_settings(ROOT_URLCONF="twocomms.urls"), \
                patch("storefront.views.monobank._monobank_api_request") as bank:
            self.client.get(prepared["checkout_url"], HTTP_HOST="twocomms.shop")
            response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": prepared["proposal"].public_id}),
                self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="1"),
                HTTP_ACCEPT="application/json", HTTP_HOST="twocomms.shop")
        self.assertNotEqual(response.status_code, 200, response.content)
        bank.assert_not_called()
        self.assertFalse(Generation.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_wait_choice_cannot_enter_hosted_payment_or_allocate_deferred_goods(self):
        case = self.selected_case(action="together")
        self.assertIsNone(case["selection"])
        current = assisted.current_assisted_offer(self.client_row, revision=case["revision"], now=self.now)
        self.assertFalse(current["can_prepare_checkout"])
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(Order.objects.exists())

    def changed_terms_draft(self, case):
        import uuid
        context = self.read()
        items = context["editable_items"]
        selected_id = case["selection"].selected_line_ids[0]
        for item in items:
            if item["line_id"] == selected_id:
                item["unit_price"] = "1690.00"
        terms = deepcopy(case["selection"].shipping_terms)
        terms["now"]["customer_maximum"] = "140.00"
        return self.post({"action": "draft", "operation_id": str(uuid.uuid4()),
            "request_id": str(case["request"].pk), **context["cas"], "items": items, "shipping_terms": terms})

    def test_new_native_manager_price_and_delivery_cap_require_new_customer_choice_before_bank(self):
        import uuid
        from management.models import IgFollowUpTask
        from management.ig_assisted_commerce_models import IgAssistedCommerceDecision
        from management.services import ig_assisted_partial as partial
        case = self.selected_case()
        original = deepcopy(case["decision"].envelope)
        drafted = self.changed_terms_draft(case)
        self.assertEqual(drafted.status_code, 200, drafted.content)
        # The API intentionally sanitizes generic implementation exceptions.
        # These transparent wrappers retain the real producers and make this
        # regression distinguish request validation from post-write projection.
        from management import assisted_commerce_views as api
        from management.services import ig_assisted_commerce as commerce
        failures = []
        def observed(function):
            def call(*args, **kwargs):
                try:
                    return function(*args, **kwargs)
                except (ValueError, TypeError, KeyError) as exc:
                    failures.append(f"{function.__name__}: {type(exc).__name__}: {exc}")
                    raise
            return call
        with patch.object(commerce, "record_assisted_decision", side_effect=observed(commerce.record_assisted_decision)), \
                patch.object(api, "_context", side_effect=observed(api._context)):
            approved = self.post({"action": "approve", "operation_id": str(uuid.uuid4()),
                "request_id": str(case["request"].pk), **drafted.json()["commerce"]["cas"]})
        self.assertEqual(approved.status_code, 200, (approved.content, failures))
        with patch("storefront.views.monobank._monobank_api_request") as bank:
            denied = assisted.prepare_assisted_checkout(self.client_row.pk, actor=self.manager,
                request_id=case["request"].pk, decision_id=case["decision"].pk,
                expected_request_digest=case["request"].envelope_hmac,
                expected_decision_digest=case["decision"].envelope_hmac,
                operation_id=uuid.uuid4(), revision_id=case["revision"].pk, settings_id=self.settings.pk,
                verified_selection=case["selection"])
        self.assertFalse(denied["ready"], denied)
        bank.assert_not_called()
        self.assertFalse(Generation.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        case["decision"].refresh_from_db()
        self.assertEqual(case["decision"].envelope, original)
        decision = IgAssistedCommerceDecision.objects.get(pk=approved.json()["decision_id"])
        context = approved.json()["commerce"]["partial_intention"]
        issued = self.post({"action": "issue_partial_offer", "operation_id": str(uuid.uuid4()),
            "request_id": str(case["request"].pk), "decision_id": str(decision.pk),
            "expected_request_digest": case["request"].envelope_hmac,
            "expected_decision_digest": decision.envelope_hmac,
            "expected_coverage_digest": context["coverage_digest"], "expected_terms_digest": context["terms_digest"]})
        self.assertEqual(issued.status_code, 200, issued.content)
        task = IgFollowUpTask.objects.get(pk=issued.json()["offer"]["task_id"])
        count, _physical = self.send_card(task)
        self.assertEqual(count, 1)
        card = partial.card_for_task(task, self.settings, now=self.now)
        self.assertTrue(card["ready"], card)
        button = next(button for button in card["buttons"] if button.payload == card["actions"]["partial"])
        self.now += timedelta(seconds=1)
        source = self.source(button.title, payload=button.payload, at=self.now)
        source.reply_to_provider_message_id = task.provider_message_id
        source.save(update_fields=["reply_to_provider_message_id"])
        revision, _token = self.revision(source, now=self.now)
        consumed = partial.consume_card_choice(source, revision, self.settings, now=self.now)
        self.assertTrue(consumed["ready"], consumed)
        self.assertEqual(consumed["selection"].shipping_terms["now"]["customer_maximum"], "140.00")
        generation = self.invoice(self.prepare_selection(consumed["selection"]))
        self.assertEqual(generation.payment_amount, Decimal("1690.00"))
        self.assertFalse(Order.objects.exists())

    def test_existing_invoice_blocks_manager_price_or_cap_overwrite_without_second_bank_request(self):
        case = self.selected_case()
        prepared = self.prepare_selection(case["selection"])
        generation = self.invoice(prepared)
        frozen = deepcopy(generation.payment_attempt.cart_snapshot)
        original = deepcopy(case["decision"].envelope)
        with patch("storefront.views.monobank._monobank_api_request") as bank:
            drafted = self.changed_terms_draft(case)
        self.assertEqual(drafted.status_code, 409, drafted.content)
        self.assertEqual(drafted.json()["reason"], "existing_invoice_commitment_requires_review")
        bank.assert_not_called()
        case["decision"].refresh_from_db()
        generation.payment_attempt.refresh_from_db()
        self.assertEqual(case["decision"].envelope, original)
        self.assertEqual(generation.payment_attempt.cart_snapshot, frozen)
        self.assertEqual(Generation.objects.count(), 1)
        self.assertFalse(Order.objects.exists())


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeUncertainInventoryTests(TransactionTestCase):
    # Reuse producers through module-qualified references; imported concrete
    # test classes would otherwise be rediscovered as another test suite.
    _item = generation_fixture.CheckoutGenerationRuntimeTests._item
    _delivery_payload = generation_fixture.CheckoutGenerationRuntimeTests._delivery_payload
    _open = generation_fixture.CheckoutGenerationRuntimeTests._open
    _create_authenticated_promo_invoice = generation_fixture.CheckoutGenerationRuntimeTests._create_authenticated_promo_invoice

    def setUp(self):
        super().setUp()
        generation_fixture.CheckoutGenerationRuntimeTests.setUp(self)

    def uncertain_case(self, *, late_identity=False):
        from management.services.ig_checkout import create_or_update_proposal
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        from productcolors.models import Color, ProductColorVariant
        color = Color.objects.create(name="Uncertain stock", primary_hex="#224422")
        variant = ProductColorVariant.objects.create(product=self.product, color=color, stock=2)
        ProductInventoryPolicy.objects.create(product=self.product, source="catalog_variant")
        rule = VariantSizeRule.objects.create(variant=variant, fit_code=self.fit.code, size="M", stock=2, is_enabled=True)
        self.now = timezone.now()
        def bank_response(*_args, **_kwargs):
            # The actual request digest has already committed at this point.
            live = Generation.objects.get(proposal=proposal, active_slot=1)
            self.assertTrue(live.provider_request_digest)
            if not late_identity:
                raise TimeoutError("Provider received create; no response is known")
            # The timer can fire while that HTTP request is in flight. Its
            # subsequently returned real identity enters the native late-result
            # gate; it cannot restore the invoice or release the hold.
            self.now = live.expires_at + timedelta(seconds=1)
            result = generations.terminalize_generation_attempt(live.payment_attempt_id,
                terminal_status=PaymentAttempt.Status.EXPIRED, reason="timer_during_create_http",
                now=self.now, require_due=True)
            self.assertEqual(result["outcome"], "provider_ambiguous", result)
            return {"invoiceId": f"native-late-identity-{proposal.pk}",
                "pageUrl": f"https://pay.example/late-{proposal.pk}"}
        with patch("django.utils.timezone.now", side_effect=lambda: self.now):
            proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full",
                item_specs=[{**self._item(), "color_variant_id": variant.pk}])
            self._open(proposal)
            with patch("storefront.views.monobank._monobank_api_request", side_effect=bank_response) as bank:
                response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                    self._delivery_payload(payment_choice="online_full"), HTTP_ACCEPT="application/json")
            self.assertNotEqual(response.status_code, 200, response.content)
            bank.assert_called_once()
            generation = Generation.objects.get(proposal=proposal)
            self.assertIn(generation.state, {Generation.State.PROVIDER_AMBIGUOUS, Generation.State.LATE_PROVIDER_REVIEW})
            self.assertTrue(generation.provider_request_digest)
            attempt = generation.payment_attempt
            self.now = generation.expires_at + timedelta(seconds=1)
            at = self.now
            result = generations.terminalize_generation_attempt(attempt.pk, terminal_status=PaymentAttempt.Status.EXPIRED,
                reason="local_timer_without_provider_result", now=at, require_due=True)
        self.assertEqual(result["outcome"], "provider_ambiguous", result)
        generation.refresh_from_db()
        hold = proposal.inventory_reservations.get(invoice_generation=generation)
        self.assertEqual(hold.state, Reservation.State.ACTIVE)
        self.assertLess(hold.expires_at, at)
        return proposal, generation, attempt, variant, rule, hold, at

    def test_expired_unknown_bank_effect_keeps_catalog_and_size_capacity_and_recount_minimum(self):
        from management.services import ig_fulfillment_stock_sync as sync
        from management.services.ig_inventory_size_capacity import check_catalog_size_capacity, SizeCapacityError
        proposal, generation, _attempt, variant, rule, hold, at = self.uncertain_case()
        with patch("django.utils.timezone.now", return_value=at):
            self.assertEqual(inventory.release_expired_proposal_inventory(), 0)
            self.assertEqual(inventory.release_proposal_inventory(proposal), 0)
            self.assertEqual(inventory.release_generation_inventory(generation), 0)
        self.assertEqual(inventory._catalog_variant_protected_quantity(color_variant_id=variant.pk, at=at), 1)
        with transaction.atomic():
            protected, rows = sync._reservations("catalog_variant", variant.pk, at, lock=True)
            sizes, _facts = sync._size_protection({"identity": variant.pk, "reservations": rows}, set(), at, lock=True)
            self.assertEqual((protected, sizes[str(rule.pk)]), (1, 1))
            self.assertTrue(next(row for row in rows if row["id"] == hold.pk)["_uncertain_payment_protected"])
            # Use the actual frozen native item identity/configuration for
            # the prospective two-unit capacity probe. A missing product ID
            # would reject malformed scope before consulting any hold.
            requested = deepcopy(generation.payment_attempt.cart_snapshot["cart"][0])
            requested.update(qty=2, quantity=2)
            self.assertEqual(requested["product_id"], self.product.pk)
            with self.assertRaises(SizeCapacityError) as blocked:
                check_catalog_size_capacity(variant.pk, [requested], at)
            self.assertEqual(blocked.exception.reason, "insufficient_catalog_size_stock")
        from management.services.ig_checkout import create_or_update_proposal, CheckoutConfigurationError
        other = IgClient.objects.create(igsid="unknown-expired-second-buyer")
        with patch("django.utils.timezone.now", return_value=at), \
                override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0):
            with self.assertRaises(CheckoutConfigurationError) as denied:
                create_or_update_proposal(client=other, pay_type="online_full",
                    item_specs=[{**self._item(), "color_variant_id": variant.pk, "qty": 2}])
            self.assertEqual(denied.exception.code, "insufficient_stock")
        hold.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.ACTIVE)
        self.assertFalse(Order.objects.exists())

    def test_trusted_exact_terminal_proof_can_release_same_uncertain_hold(self):
        from management.services.ig_fulfillment_stock_sync import _reservations
        proposal, generation, attempt, variant, _rule, hold, at = self.uncertain_case(late_identity=True)
        with patch("django.utils.timezone.now", return_value=at):
            result = generations.terminalize_generation_attempt(attempt.pk,
                terminal_status=PaymentAttempt.Status.FAILED, reason="trusted_provider_failure",
                terminal_source="provider_pull", provider_status="failure", now=at,
                provider_payload={"status": "failure", "invoiceId": attempt.monobank_invoice_id,
                    "merchantPaymInfo": {"reference": attempt.reference}, "ccy": 980})
        self.assertEqual(result["outcome"], "terminalized", result)
        self.assertEqual(result["released_inventory"], 1)
        hold.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.RELEASED)
        self.assertEqual(inventory._catalog_variant_protected_quantity(color_variant_id=variant.pk, at=at), 0)
        self.assertEqual(_reservations("catalog_variant", variant.pk, at)[0], 0)
        self.assertFalse(Order.objects.exists())

    def test_failed_label_and_cleared_local_flag_cannot_release_original_native_timeout(self):
        _proposal, generation, attempt, variant, _rule, hold, at = self.uncertain_case()
        self.assertTrue(generation.events.filter(kind="provider_ambiguous").exists())
        # Model a stale local status/JSON writer, preserving the actual durable
        # dispatch digest and original native ambiguity journal.
        Generation.objects.filter(pk=generation.pk).update(state=Generation.State.FAILED)
        local_state = deepcopy(attempt.event_state)
        local_state.pop("invoice_creation_ambiguous", None)
        PaymentAttempt.objects.filter(pk=attempt.pk).update(status=PaymentAttempt.Status.FAILED, event_state=local_state)
        with patch("django.utils.timezone.now", return_value=at):
            self.assertEqual(inventory.release_generation_inventory(generation), 0)
            # Even a stale writer restoring its former in-flight label cannot
            # erase the original UNKNOWN journal and mint a negative receipt.
            Generation.objects.filter(pk=generation.pk).update(state=Generation.State.PROVIDER_INFLIGHT)
            PaymentAttempt.objects.filter(pk=attempt.pk).update(status=PaymentAttempt.Status.PROCESSING)
            with transaction.atomic():
                generations._persist_provider_failure(attempt.pk, ambiguous=False, reason="stale classified failure")
            self.assertEqual(inventory.release_generation_inventory(generation), 0)
        hold.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(hold.state, Reservation.State.ACTIVE)
        self.assertNotIn("provider_create_refusal", attempt.event_state)
        self.assertEqual(inventory._catalog_variant_protected_quantity(color_variant_id=variant.pk, at=at), 1)
        self.assertFalse(Order.objects.exists())

    def test_actual_classified_http_400_create_refusal_seals_exact_negative_receipt_and_releases(self):
        from unittest.mock import Mock
        from management.services.ig_checkout import create_or_update_proposal
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        from productcolors.models import Color, ProductColorVariant
        color = Color.objects.create(name="Classified refusal", primary_hex="#442244")
        variant = ProductColorVariant.objects.create(product=self.product, color=color, stock=2)
        ProductInventoryPolicy.objects.create(product=self.product, source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code=self.fit.code, size="M", stock=2, is_enabled=True)
        self.now = timezone.now()
        response = Mock(status_code=400)
        response.json.return_value = {"errText": "Native test: rejected create parameters"}
        with patch("django.utils.timezone.now", side_effect=lambda: self.now), \
                override_settings(MONOBANK_TOKEN="synthetic-native-refusal-token"):
            proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full",
                item_specs=[{**self._item(), "color_variant_id": variant.pk}])
            self._open(proposal)
            # Exercise the actual classifier; only its physical HTTP is fake.
            with patch("storefront.views.monobank.requests.post", return_value=response) as provider:
                posted = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                    self._delivery_payload(payment_choice="online_full"), HTTP_ACCEPT="application/json")
            self.assertEqual(posted.status_code, 400, posted.content)
            provider.assert_called_once()
            generation = Generation.objects.get(proposal=proposal)
            attempt = generation.payment_attempt
            self.assertTrue(generation.provider_request_digest)
            self.assertEqual(generation.state, Generation.State.FAILED)
            self.assertTrue(generations._create_refusal_proof_matches(generation, attempt))
            self.assertEqual(generation.inventory_reservations.get().state, Reservation.State.RELEASED)
            self.assertEqual(inventory._catalog_variant_protected_quantity(color_variant_id=variant.pk, at=self.now), 0)
        self.assertFalse(Order.objects.exists())
