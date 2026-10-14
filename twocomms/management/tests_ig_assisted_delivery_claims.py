"""Delivery terms cannot become goods prices, carrier quotes or promotions."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import uuid

from django.test import SimpleTestCase

from management.services import ig_assisted_commerce as commerce
from management.services import ig_assisted_commerce_context as context
from management.services import ig_assisted_scopes as scopes
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import ValidatedResponse


class AssistedDeliveryClaimTests(SimpleTestCase):
    def frame(self, *, selected=True, pending=False, merchant_phase=None):
        # Pure instances of the reader-issued types exercise the real context
        # and selection guards. There are no provider or database substitutes.
        ready = commerce.AssistedDecisionLine(line_id="ready", recipient_id="owner", original_index=0,
            item_kind="custom_apparel", product_id=None, color_variant_id=None, title="Hoodie", color="white",
            size="M", fit_code="", quantity=1, unit_price=Decimal("850.00"), availability="ready",
            checked_quantity=1, supply_basis=None, source_location="", image_id=None, image_digest="")
        deferred = replace(ready, line_id="deferred", original_index=1, title="T-shirt", unit_price=Decimal("600.00"),
            availability="wait", checked_quantity=None)
        now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        decision = commerce.VerifiedAssistedDecision(request_id=uuid.uuid4(), decision_id=uuid.uuid4(), client_id=1,
            actor_id=2, approver_id=2, intention_id=uuid.uuid4(), request_version=1, decision_version=1,
            source_revision_id=3, request_digest="a" * 64, decision_digest="b" * 64, full_capture_digest="c" * 64,
            snapshot_digest="d" * 64, approved_total=Decimal("850.00"), approved_custom_quantity=1,
            physical_checked_at=now, expires_at=now, lines=(ready, deferred), _source_snapshot_json='{"questions":[]}',
            _current_capture_json="{}", proof_scope="unconsumed", _issuer=commerce._ISSUER)
        shipping = {phase: {"payer": "customer", "customer_maximum": amount, "currency": "UAH"}
            for phase, amount in (("now", "120.00"), ("later", "150.00"), ("together", "200.00"))}
        if merchant_phase:
            shipping[merchant_phase] = {"payer": "merchant", "customer_maximum": "0.00", "currency": "UAH"}
        quote = {"total": "850.00", "custom_quantity": 1, "scope_digest": "e" * 64,
            "terms_digest": "f" * 64, "selected_line_ids": ["ready"], "deferred_line_ids": ["deferred"],
            "line_map": [{"line_id": "ready", "current_line_id": "ready"}], "shipping_terms": shipping,
            "delivery_scope": "now"}
        selection = scopes.VerifiedAssistedSelection(decision=decision, lines=(ready,), _quote_json=json.dumps(quote),
            _coverage_json=json.dumps({"settled_line_ids": [], "cancelled_line_ids": [], "coverage_digest": "1" * 64,
                "ledger_digest": "2" * 64}), _choice_json='{"source_id":4}',
            _purchase_json='{"message_id":5,"revision_id":3}', _origin_json="{}", _current_capture_json="{}",
            root_request_id=decision.request_id, root_request_digest=decision.request_digest,
            origin_full_capture_digest=decision.full_capture_digest, execution_id=None, proof_scope="unconsumed",
            _issuer=scopes._ISSUER) if selected else None
        return context.AssistedCommerceContext(verified=decision, revision_id=3, revision_digest="3" * 64,
            cart_capture_digest="4" * 64, can_prepare_checkout=selected, selection=selection, partial_pending=pending,
            _issuer=context._ISSUER)

    def check(self, text, *, frame=None, base=None):
        return (frame or self.frame()).validate_reply(ValidatedResponse(reply_text=text), base or ReplyTruthContext())

    def test_mixed_goods_and_explicit_delivery_caps_are_independent_in_three_languages(self):
        for text in (
            "Hoodie: ціна 850 грн, доставка готової частини — до 120 грн.",
            "Hoodie: цена 850 грн, доставка готовой части — до 120 грн.",
            "Hoodie: price 850 UAH and delivery for ready items: up to 120 UAH.",
            "Hoodie: 850 грн; доставка зараз: максимум 120 грн; пізніше: максимум 150 грн.",
            "Hoodie: UAH 850; delivery now: maximum UAH 120.",
            "Hoodie 850 UAH delivery now up to 120 UAH.",
            "Hoodie в наявності, розмір M, ціна 850 грн. T-shirt залишається в очікуванні, ще не оплачено.",
            "Hoodie в наявності, розмір M, ціна 850 грн, доставка готової частини до 120 грн.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.check(text).valid, self.check(text).reasons)

    def test_selected_total_and_each_future_shipping_phase_have_separate_authority(self):
        for text in (
            "Разом за готову частину 850 грн; доставка готової частини до 120 грн.",
            "Итого за готовую часть 850 грн; доставка позже до 150 грн.",
            "Selected goods total: 850 UAH; delivery together: up to 200 UAH.",
            "Delivery now: up to 120,00 UAH; delivery later: up to 150 UAH.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.check(text).valid, self.check(text).reasons)

    def test_caps_never_license_goods_or_a_deferred_price_even_when_equal(self):
        for text in (
            "Hoodie: ціна 120 грн, доставка готової частини до 120 грн.",
            "Hoodie: цена 120 грн; доставка сейчас до 120 грн.",
            "Hoodie: price 120 UAH and delivery now: up to 120 UAH.",
            "Delivery now: up to 120 UAH; selected goods: up to 120 UAH.",
            "Delivery now: up to 120 UAH and goods: 120 UAH.",
            "T-shirt: price 850 UAH; delivery later: up to 150 UAH.",
            "T-shirt: UAH 850; delivery later: up to 150 UAH.",
            "Весь кошик: разом 850 грн; доставка разом до 200 грн.",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.check(text).valid)

    def test_actual_fee_cannot_borrow_the_cap_or_any_goods_price(self):
        for text in (
            "Доставка готової частини коштує 120 грн.",
            "Фактична доставка зараз: максимум 120 грн.",
            "Доставка сейчас: тариф 120 грн.",
            "Delivery now costs 120 UAH.",
            "Actual delivery now: up to 120 UAH.",
            "The carrier will charge up to 120 UAH for delivery now.",
            "Delivery now: 850 UAH.",
            "Delivery now: estimated 80-120 UAH.",
            "Delivery now: up to 80-120 UAH.",
            "Delivery now: from 80 to 120 UAH.",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.check(text).valid)

    def test_money_requires_exact_adopted_phase_cap_and_currency(self):
        for text in (
            "Доставка до 120 грн.", "Доставка до 150 грн.", "Delivery: up to 120 UAH.",
            "Delivery now and later: up to 120 UAH.", "Delivery now: up to 150 UAH.",
            "Delivery later: up to 120 UAH.", "Delivery now: up to 119 UAH.",
            "Delivery now: up to 120 USD.",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.check(text).valid)
        for frame in (self.frame(selected=False), self.frame(selected=False, pending=True)):
            self.assertFalse(self.check("Delivery now: up to 120 UAH.", frame=frame).valid)
            self.assertEqual(frame.delivery_authorities, ())

    def test_merchant_payer_comes_from_that_exact_phase_and_does_not_prove_promotion(self):
        frame = self.frame(merchant_phase="later")
        for text in (
            "Доставку решти оплачує TwoComms; доставка решти: для вас 0 грн.",
            "Доставка оставшихся позиций бесплатная для вас.",
            "TwoComms covers shipping for remaining items. Shipping for remaining items: you pay 0 UAH.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.check(text, frame=frame).valid, self.check(text, frame=frame).reasons)
        for text in (
            "TwoComms covers delivery now.", "Delivery now is free.",
            "Delivery later: you pay separately to the carrier.",
            "Delivery later costs 0 UAH.", "Delivery later: actual fee 0 UAH.",
            "Delivery later: free promotion.", "Доставка решти безкоштовна за акцією.",
            "Доставка позже включена в сумму товаров.", "Delivery later is prepaid.",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.check(text, frame=frame).valid)
        self.assertFalse(self.check("TwoComms covers shipping.", frame=frame).valid)
        self.assertFalse(self.check("TwoComms covers delivery later.", frame=self.frame(selected=False)).valid)

    def test_customer_carrier_payment_does_not_require_an_invented_fee(self):
        for text in (
            "Доставку готової частини оплачуєте перевізнику окремо.",
            "Доставка готовой части оплачивается перевозчику отдельно.",
            "You pay separately to the carrier for delivery of the ready items.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.check(text).valid, self.check(text).reasons)
        self.assertTrue(self.check("You pay the carrier separately for delivery.", frame=self.frame(selected=False)).valid)

    def full_native_terms(self, *, merchant_phase="later"):
        """Real immutable model/envelope signing; no database or provider."""
        from management.ig_assisted_commerce_models import IgAssistedCommerceDecision
        selected = self.frame(merchant_phase=merchant_phase)
        verified = selected.verified
        row = IgAssistedCommerceDecision(id=verified.decision_id, request_id=verified.request_id,
            client_id=verified.client_id, actor_id=verified.approver_id, version=verified.decision_version,
            status="approved", request_digest=verified.request_digest,
            full_capture_digest=verified.full_capture_digest, approved_total=verified.approved_total,
            approved_custom_quantity=verified.approved_custom_quantity,
            physical_checked_at=verified.physical_checked_at, expires_at=verified.expires_at)
        body = {"scoped_quotes": {"selected": selected.selection.quote}}
        row.key_id = next(iter(commerce._keys()))
        row.snapshot_digest = commerce._digest(body)
        row.envelope = {"version": 1, "purpose": commerce.PURPOSE, "kind": "decision",
            "columns": commerce._columns(row), "snapshot": body, "operation_input_digest": "5" * 64}
        row.envelope_hmac = commerce._sign("decision", row.envelope, row.key_id)
        verified = replace(verified, decision_digest=row.envelope_hmac, snapshot_digest=row.snapshot_digest)
        native = context._native_delivery_terms(row, verified)
        self.assertIsNotNone(native)
        return replace(selected, verified=verified, selection=None, partial_pending=True,
            can_prepare_checkout=False, _delivery_terms_json=json.dumps(native)), row

    def test_full_context_adopts_exact_signed_phases_without_adopting_a_purchase(self):
        frame, row = self.full_native_terms()
        for text in (
            "Доставку решти оплачує TwoComms; доставка решти: для вас 0 грн.",
            "Доставка оставшихся позиций бесплатная для вас.",
            "TwoComms covers shipping for remaining items. Shipping for remaining items: you pay 0 UAH.",
            "Delivery now: up to 120 UAH.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.check(text, frame=frame).valid, self.check(text, frame=frame).reasons)
        for text in ("Delivery now is free.", "Delivery later: free promotion.",
            "Delivery later is prepaid.", "Delivery now costs 120 UAH.", "Hoodie: price 120 UAH."):
            with self.subTest(text=text):
                self.assertFalse(self.check(text, frame=frame).valid)
        self.assertIsNone(frame.approved_total)
        self.assertEqual(frame.reference["delivery_terms"]["source_ref"], row.envelope_hmac)
        self.assertIsNone(context._native_delivery_terms(row, replace(frame.verified, client_id=2)))
        row.envelope["snapshot"]["scoped_quotes"]["selected"]["shipping_terms"]["now"]["payer"] = "merchant"
        self.assertIsNone(context._native_delivery_terms(row, frame.verified))

    def test_ordinary_full_context_customer_rule_is_not_a_fee_or_manager_override(self):
        frame = self.frame(selected=False)
        for text in ("Доставку оплачуєте перевізнику окремо.",
            "Доставка оплачивается перевозчику отдельно.", "You pay the carrier separately for delivery."):
            self.assertTrue(self.check(text, frame=frame).valid)
        for text in ("Delivery is free.", "TwoComms covers shipping.",
            "Delivery costs 120 UAH.", "Delivery now: up to 120 UAH.", "Delivery is prepaid."):
            self.assertFalse(self.check(text, frame=frame).valid)

    def test_masking_a_cap_preserves_stock_spec_timing_payment_and_url_guards(self):
        for text in (
            "T-shirt is available; delivery later: up to 150 UAH.",
            "Hoodie is 100% cotton; delivery now: up to 120 UAH.",
            "Hoodie arrives tomorrow; delivery now: up to 120 UAH.",
            "Hoodie is fully paid; delivery now: up to 120 UAH.",
            "Delivery now: up to 120 UAH. See https://unapproved.example/path?",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.check(text).valid)
        self.assertFalse(self.check("Delivery now is already paid; up to 120 UAH.",
            base=ReplyTruthContext(payment_confirmed=True)).valid)
        self.assertFalse(self.check("Delivery now: up to 120 UAH; already paid.",
            base=ReplyTruthContext(payment_confirmed=True)).valid)
