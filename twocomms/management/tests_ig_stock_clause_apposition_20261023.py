"""Configuration commas retain exact line identity, never sibling authority."""
from types import SimpleNamespace

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_ig_response_cart_plan as cart_fixture
from management import tests_ig_positive_stock_autonomy_20261023 as stock_fixture
from management import tests_ig_live_conversation_contract_20261022 as live_fixture
from management.tests_ig_restock_subscriptions import _RestockFixture
from management.services.ig_response_cart_plan import scoped_claim_clauses, line_clauses
from management.services.ig_reply_truth import ReplyTruthContext
from product_catalog.models import VariantSizeRule


class StockConfigurationClauseTests(SimpleTestCase):
    def test_product_color_size_stock_chain_is_one_contextual_claim(self):
        for text in ("Restock Aurora, чорний, розмір M є в наявності.",
                     "Restock Aurora, black, size M is in stock.",
                     "Restock Aurora, колір чорний розмір M є в наявності."):
            self.assertEqual(list(scoped_claim_clauses(text)), [text.rstrip('.').replace(', ', ' ')])

    def test_new_product_recipient_ordinal_and_assertion_start_fresh_clauses(self):
        head = "Restock Aurora, black, size M is in stock"
        for tail in ("Storm, pink, size L is in stock.",
                     "For bob Restock Aurora, pink, size L is in stock.",
                     "Item 2 Restock Aurora, pink, size L is in stock.",
                     "we have received your payment."):
            self.assertEqual(list(scoped_claim_clauses(head + ", " + tail)),
                [head.replace(', ', ' '), " " + tail.rstrip('.').replace(', ', ' ')])
        self.assertEqual(list(scoped_claim_clauses(head + "; Storm, pink, size L is in stock.")),
            [head.replace(', ', ' '), " Storm pink size L is in stock"])

    def test_parenthetical_configuration_and_decimal_prices_keep_existing_boundaries(self):
        self.assertEqual(list(scoped_claim_clauses("Classic (black, size M) costs 699,00 UAH, Storm costs 1299,50 UAH.")),
            ["Classic (black, size M) costs 699,00 UAH", " Storm costs 1299,50 UAH"])

    def recipient_plan(self):
        fixture = cart_fixture.ResponseCartPlanTests()
        return fixture.plan(fixture.capture(duplicate=True))

    def test_same_product_recipient_identity_stays_on_each_configuration(self):
        plan = self.recipient_plan()
        text = "You selected Classic for alice, black, size M. You selected Classic for bob, pink, size L."
        clauses = line_clauses(text, plan.line_plans)
        self.assertEqual(clauses["first"], ["You selected Classic for alice black size M"])
        self.assertEqual(clauses["second"], ["You selected Classic for bob pink size L"])
        self.assertEqual(plan.validate_multiline_claims(SimpleNamespace(reply_text=text, control={}), ReplyTruthContext()), "")

    def test_swapped_recipient_configuration_and_independent_unbound_claims_are_rejected(self):
        plan = self.recipient_plan()
        for text in ("You selected Classic for alice, pink, size L. You selected Classic for bob, black, size M.",
                     "You selected Classic for alice, black, size L.",
                     "You selected Classic for alice, black, size M, the color is pink.",
                     "You selected Classic for alice, black, size M, for bob the size is M."):
            with self.subTest(text=text):
                self.assertTrue(plan.validate_multiline_claims(SimpleNamespace(reply_text=text, control={}), ReplyTruthContext()))

    def test_balanced_parenthetical_foreign_recipient_cannot_become_an_independent_price_owner(self):
        from decimal import Decimal
        fixture = cart_fixture.ResponseCartPlanTests()
        plan = fixture.plan(fixture.capture(duplicate=True), contexts={
            key: ReplyTruthContext(authorized_prices=(Decimal("900"),), allowed_sizes=("M", "L"))
            for key in ("first", "second")})
        for text in ("item 1 for alice (M, bob L) costs 900 UAH.",
                     "item 1 for alice (M, for bob L) costs 900 UAH.",
                     "item 1 for alice (M, item 2 L) costs 900 UAH."):
            with self.subTest(text=text):
                self.assertEqual(list(scoped_claim_clauses(text)), [text.rstrip('.')])
                self.assertTrue(plan.validate_multiline_claims(fixture.response(text), ReplyTruthContext()))
                self.assertFalse(any(line_clauses(text, plan.line_plans).values()))

    def test_foreign_product_parenthetical_and_unbalanced_syntax_keep_price_unverified(self):
        from decimal import Decimal
        fixture = cart_fixture.ResponseCartPlanTests()
        plan = fixture.plan(contexts={
            "first": ReplyTruthContext(authorized_prices=(Decimal("900"),), allowed_sizes=("M",)),
            "second": ReplyTruthContext(authorized_prices=(Decimal("1290"),), allowed_sizes=("L",))})
        for text in ("Classic (size M, Storm costs 900 UAH).", "Classic (size M, Storm) costs 900 UAH.",
                     "item 1 (M, item 2 — L) costs 900 UAH.", ") Classic (M, regular fit) costs 900 UAH."):
            with self.subTest(text=text):
                self.assertTrue(plan.validate_multiline_claims(fixture.response(text), ReplyTruthContext()))
        self.assertEqual(len(list(scoped_claim_clauses("Classic (M, regular fit), Storm (L, oversize fit) costs 1290 UAH."))), 2)
        self.assertEqual(len(list(scoped_claim_clauses("Classic (M, (regular fit, black)) costs 900 UAH."))), 1)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=20000, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeStockConfigurationAppositionTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True
    _generate = live_fixture.NativeLiveConversationContractTests._generate
    execute = stock_fixture.NativeInventoryCheckoutDeliveryTests.execute

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
        self.large = VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="L", stock=2, is_enabled=True)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер L, цвет black, крій classic")
        self.canonical("Хочу купити")
        self.source_row = self.source("Беру. Оформлюйте замовлення.")
        self.revision, self.token = self.revision(self.source_row)
        self.parsed = {"reply_text": "Restock Aurora, чорний, розмір M є в наявності. Restock Aurora, чорний, розмір L є в наявності. Перевірте товари та деталі оплати за посиланням:",
            "controls": [{"kind": "paylink", "value": "full"}]}

    def test_two_exact_comma_stock_claims_reach_one_owned_hold_and_sent_receipt(self):
        from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation
        from management.services import ig_inventory_availability as inventory
        from management.services.ig_reply_authority import apply_inventory_availability_context, build_reply_truth_context
        from management.services.ig_reply_truth import validate_reply_truth
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_cart_plan import matching_line_ids
        plan = capture_response_plan(self.customer, revision=self.revision)
        capture = inventory.capture_inventory_availability(self.customer.pk, current_source_cart=plan.source_cart_capture, now=self.now)
        context = apply_inventory_availability_context(build_reply_truth_context(self.customer), capture)
        candidate = SimpleNamespace(reply_text=self.parsed["reply_text"], control={})
        details = [(clause, matching_line_ids(clause, plan.line_plans), validate_reply_truth(clause, context=context).reasons)
            for clause in scoped_claim_clauses(candidate.reply_text)]
        self.assertFalse(plan.validate_multiline_claims(candidate, context), details)
        result, generated, http = self.execute()
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts))
        self.assertEqual((generated.call_count, http.call_count), (1, 1))
        sent = self.revision.delivery_effects.get(state="sent")
        self.assertIn("Restock Aurora, чорний, розмір M є в наявності.", sent.payload["message"]["text"])
        self.assertEqual(IgCheckoutProposal.objects.get().items.count(), 2)
        self.assertEqual(IgCheckoutInventoryReservation.objects.get(state="active").quantity, 2)
        replay, generated, http = self.execute()
        self.assertEqual(replay.state, "completed", replay.reasons)
        generated.assert_not_called()
        http.assert_not_called()

    def test_wrong_color_cannot_borrow_the_sibling_or_stock_authority(self):
        self.parsed["reply_text"] = self.parsed["reply_text"].replace("чорний", "білий", 1)
        self._expected_rejection = "response_plan_line_claim_unverified"
        result, _generated, http = self.execute()
        self.assertNotEqual(result.state, "completed", result.reasons)
        self.assertIn(self._expected_rejection, self._observed_rejection)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.filter(state="sent").exists())

    def test_unknown_sibling_cannot_borrow_the_positive_size_fact(self):
        from management.services import ig_inventory_availability as inventory
        from management.services.ig_reply_authority import apply_inventory_availability_context, build_reply_truth_context
        from management.services.ig_response_plan import capture_response_plan
        self.large.stock = 0
        self.large.save(update_fields=["stock", "updated_at"])
        plan = capture_response_plan(self.customer, revision=self.revision)
        capture = inventory.capture_inventory_availability(self.customer.pk, current_source_cart=plan.source_cart_capture, now=self.now)
        self.assertEqual({row["status"] for row in capture["lines"]}, {"available", "check_required"})
        context = apply_inventory_availability_context(build_reply_truth_context(self.customer), capture)
        candidate = SimpleNamespace(reply_text=self.parsed["reply_text"], control={})
        self.assertEqual(plan.validate_multiline_claims(candidate, context), "response_plan_line_claim_unverified")
        # Native execution may deliver its honest static stock-review holding
        # response. It must never admit this false model claim or checkout.
        from management.models import IgCheckoutProposal, IgCheckoutInventoryReservation
        _result, generated, _http = self.execute()
        generated.assert_not_called()
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        for effect in self.revision.delivery_effects.filter(state="sent"):
            self.assertNotIn("розмір L є в наявності", str(effect.payload))
