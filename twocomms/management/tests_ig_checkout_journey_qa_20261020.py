"""Customer chat → hosted edits → winner settlement across recipient boundaries.

Only the bank transport is mocked. Source reduction, catalogue authority,
checkout revisions, native amendment receipts and payment settlement are real.
"""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import uuid

from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from management import tests_ig_checkout_generation_runtime as runtime
from management import tests_ig_response_cart_fence as carts
from management import tests_ig_source_cart_catalog as catalog
from management.models import IgCheckoutAccessToken, IgCommerceSelectionTransition, InstagramBotMessage
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_checkout_cart_amendments import amend_checkout_cart, checkout_cart_state
from management.services.ig_checkout_payment import CheckoutPaymentError, _revalidate_frozen_proposal
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_revision_checkout import checkout_authority_control
from orders.models import Order, PaymentAttempt
from product_catalog.models import ProductInventoryPolicy


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class CheckoutJourneyQA20261020Tests(carts.CartFenceFixture, TestCase):
    _open = runtime.CheckoutGenerationRuntimeTests._open
    _delivery_payload = runtime.CheckoutGenerationRuntimeTests._delivery_payload

    def setUp(self):
        super().setUp()
        catalog.SourceCartCatalogTests.setUp(self)
        self.client_row = self.customer
        self.http = Client()
        self.variants[0].stock = 10
        self.variants[0].save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=self.products[0],
            source=ProductInventoryPolicy.Source.CATALOG_VARIANT)

    def chat(self, text):
        self.now = timezone.now()
        source, decision = self.reduce(text, at=self.now)
        return source, decision

    def quote_two_recipients(self):
        url = f"https://twocomms.shop/product/{self.products[0].slug}/"
        self.chat(f"add {url} black size M fit classic")
        self.chat(f"add {url} black size L fit classic for friend")
        self.chat("Хочу замовити")
        cart = capture_current_selection_lines(self.customer.pk)
        control, reasons = checkout_authority_control(self.customer, {}, source_cart_capture=cart)
        self.assertFalse(reasons, reasons)
        with transaction.atomic():
            self.proposal = create_or_update_proposal(client=self.customer, pay_type="online_full",
                item_specs=control["items"], source_cart_binding=control["source_cart_binding"],
                source_cart_capture=control["source_cart_capture"],
                checkout_owner_scope=control["checkout_owner_scope"])
        _raw, self.token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        self.grant = {"proposal_id": str(self.proposal.public_id), "token_id": self.token.pk,
            "grant_id": "recipient-journey", "revision": self.proposal.revision,
            "expires_at": int(self.proposal.expires_at.timestamp())}
        return cart

    def remove(self, index):
        state = checkout_cart_state(self.proposal, self.token)
        result = amend_checkout_cart(self.proposal, request=SimpleNamespace(), grant=self.grant,
            payload={"operation_id": str(uuid.uuid4()), "operation": "remove",
                "line_id": state["items"][index]["line_id"], "quantity": "",
                "expected_revision": state["revision"], "expected_digest": state["digest"],
                "expected_selection_revision": state["selection_revision"]})
        self.proposal.refresh_from_db()
        return result

    def test_removed_recipient_correction_cannot_resize_remaining_same_sku_or_revive_removed_line(self):
        original = self.quote_two_recipients()
        self.remove(0)
        before = capture_current_selection_lines(self.customer.pk)
        self.assertEqual([(row["recipient_id"], row["fields"]["size"]["value"])
            for row in before["lines"]], [("friend", "L")])
        removed_id = original["lines"][0]["line_id"]
        source, decision = self.chat("змініть розмір для себе на M")
        from management.tests_ig_turn_capture import RevisionTurnCaptureTests
        revision, _collection, _boundary = RevisionTurnCaptureTests.seal(self, source)
        after = capture_current_selection_lines(self.customer.pk)
        self.assertFalse(decision.accepted, decision.result_payload)
        self.assertEqual([(row["line_id"], row["recipient_id"], row["fields"]["size"]["value"])
            for row in after["lines"]], [(before["lines"][0]["line_id"], "friend", "L")])
        self.assertIn(removed_id, [row["line_id"] for row in after.get("removed", [])])
        self.assertEqual(after["removed"][0]["source"]["authority"], "checkout_link_holder")
        self.assertNotIn("source_message_id", after["removed"][0]["source"])
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_turn_intelligence import capture_digest, source_cart_current_facts
        self.assertEqual(after["capture_digest"], capture_digest({key: value for key, value in after.items()
            if key != "capture_digest"}))
        plan = capture_response_plan(self.customer, revision=revision, source_cart_capture=after)
        self.assertEqual(plan.source_cart_capture, after)
        self.assertEqual(source_cart_current_facts(plan.source_cart_capture)["removed"], after["removed"])
        self.assertFalse(PaymentAttempt.objects.exists())
        source.refresh_from_db()
        self.assertEqual(source.text, "змініть розмір для себе на M")

    def test_vague_assent_then_exact_survivor_size_correction_keeps_site_removal_and_closes_old_quote(self):
        self.quote_two_recipients()
        self.remove(1)
        before = capture_current_selection_lines(self.customer.pk)
        with transaction.atomic():
            _revalidate_frozen_proposal(self.proposal)
        user_fields = ("product_id", "size", "color", "fit_option_code", "quantity")
        facts = {key: before["lines"][0]["fields"][key]["value"] for key in user_fields}
        self.chat("звичайно")
        neutral = capture_current_selection_lines(self.customer.pk)
        self.assertEqual({key: neutral["lines"][0]["fields"][key]["value"] for key in user_fields}, facts)
        source, decision = self.chat("Насправді розмір L")
        self.assertTrue(decision.accepted, decision.result_payload)
        after = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(len(after["lines"]), 1)
        self.assertEqual(after["lines"][0]["recipient_id"], "self")
        self.assertEqual(after["lines"][0]["fields"]["size"]["value"], "L")
        self.assertEqual(after["lines"][0]["fields"]["size"]["source"]["source_message_id"], source.pk)
        self.assertEqual([{key: value for key, value in row.items() if key != "source"}
            for row in after.get("removed", [])], before["removed"])
        with transaction.atomic(), self.assertRaises(CheckoutPaymentError):
            _revalidate_frozen_proposal(self.proposal)
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_same_sku_friend_survives_hosted_removal_and_only_current_winner_is_paid_once(self):
        from storefront.views.monobank import _apply_payment_attempt_status
        from management.services.bot_payment_truth import confirmed_purchase_units
        self.quote_two_recipients()
        original_snapshot = deepcopy(self.proposal.revisions.get(revision=1).snapshot)
        self.remove(0)
        saved, self.client = self.client, self.http
        try:
            generation = runtime.CheckoutGenerationRuntimeTests._create_invoice(self, self.proposal,
                invoice_id="recipient-journey-current-winner")
        finally:
            self.client = saved
        attempt = generation.payment_attempt
        self.assertEqual(attempt.payment_amount, Decimal("790.00"))
        self.assertEqual(confirmed_purchase_units(self.customer), [])
        order, created = _apply_payment_attempt_status(attempt, "success",
            payload={"paidAmount": 79000}, source="provider_pull")
        self.assertTrue(created)
        self.assertEqual([(row.product_id, row.size, row.qty) for row in order.items.all()],
            [(self.products[0].pk, "L", 1)])
        self.customer.refresh_from_db()
        units = confirmed_purchase_units(self.customer)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["order_id"], order.pk)
        replay_order, replay_created = _apply_payment_attempt_status(attempt, "success",
            payload={"paidAmount": 79000}, source="provider_pull")
        self.assertEqual(replay_order.pk, order.pk)
        self.assertFalse(replay_created)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(len(confirmed_purchase_units(self.customer)), 1)
        self.assertEqual(self.proposal.revisions.get(revision=1).snapshot, original_snapshot)
        receipt = IgCommerceSelectionTransition.objects.get(checkout_amendment__isnull=False)
        self.assertEqual(receipt.next_snapshot["lines"][0]["recipient_id"], "friend")

    def test_changed_original_source_of_removed_recipient_cannot_borrow_signed_removal_history(self):
        original = self.quote_two_recipients()
        original_source_id = original["lines"][0]["fields"]["size"]["source"]["source_message_id"]
        self.remove(0)
        self.chat("звичайно")
        accepted = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(len(accepted.get("removed", [])), 1)
        InstagramBotMessage.objects.filter(pk=original_source_id).update(text="Хочу іншу річ розміру XXL")
        rejected = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(rejected["status"], "unavailable", rejected)
        self.assertEqual(rejected["reason"], "native_removal_source_changed")
        self.assertEqual(rejected["lines"], [])
        self.assertFalse(PaymentAttempt.objects.exists())


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class CheckoutBootstrapFixtureTests(carts.CartFenceFixture, TestCase):
    def setUp(self):
        from management.models import BotPolicyPublication, InstagramBotSettings
        from management.tests_ig_policy_helpers import ensure_test_instruction_publication

        ensure_test_instruction_publication()
        self.seeded_settings = InstagramBotSettings.load()
        self.bootstrap = self.seeded_settings.active_instruction_publication
        self.bootstrap_state = BotPolicyPublication.objects.filter(pk=self.bootstrap.pk).values().get()
        self.seeded_settings.ig_user_id = "bootstrap-owner"
        self.seeded_settings.reply_permission_epoch = 9
        self.seeded_settings.save(update_fields=["ig_user_id", "reply_permission_epoch"])
        super().setUp()

    def test_cart_fixture_reuses_bootstrap_singleton_and_keeps_publication_immutable(self):
        from management.models import BotPolicyPublication, InstagramBotSettings

        self.seeded_settings.refresh_from_db()
        self.assertEqual(InstagramBotSettings.objects.count(), 1)
        self.assertEqual(self.seeded_settings.ig_user_id, "cart-source-owner")
        self.assertEqual(self.seeded_settings.reply_permission_epoch, 9)
        self.assertEqual(self.seeded_settings.active_instruction_publication_id, self.bootstrap.pk)
        self.assertEqual(self.namespace, "instagram_login:cart-source-owner")
        self.assertEqual(BotPolicyPublication.objects.filter(pk=self.bootstrap.pk).values().get(), self.bootstrap_state)
        with self.assertRaisesRegex(ValueError, "append-only"):
            BotPolicyPublication.objects.filter(pk=self.bootstrap.pk).update(note="fixture rewrite")
        self.bootstrap.refresh_from_db()
        self.assertEqual(self.bootstrap.note, self.bootstrap_state["note"])
        source, decision = self.reduce("додайте чорну футболку розмір M")
        self.assertTrue(decision.accepted, decision.result_payload)
        self.assertEqual(source.provider_namespace, self.namespace)
