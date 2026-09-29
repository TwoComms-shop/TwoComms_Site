import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings, RequestFactory
from django.urls import reverse
from django.utils import timezone

from orders.models import Order, PaymentAttempt
from product_catalog.models import MerchCollection, ProductMerchCollection, ProductOptionProfile
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, PromoCode
from storefront.services.brigade_commerce import calculate_brigade_cart_pricing, get_product_brigade_policy


class BrigadeCommerceTests(TestCase):
    def setUp(self):
        cache.clear()
        for target in (
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            "storefront.signals.enqueue_indexnow_urls",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tees = Category.objects.create(name="Tees", slug="tshirts")
        self.hoodies = Category.objects.create(name="Hoodies", slug="hoodie")
        self.tee = Product.objects.create(title="225 tee", slug="225-tshirt", category=self.tees, price=880, status="published")
        self.hoodie = Product.objects.create(title="225 hoodie", slug="225-hoodie", category=self.hoodies, price=1995, status="published")
        self.ordinary = Product.objects.create(title="Ordinary tee", slug="regular-tee", category=self.tees, price=1000, discount_percent=10, status="published")
        self.variant = ProductColorVariant.objects.create(product=self.tee, color=Color.objects.create(name="Black", primary_hex="#000000"), price_override=880)
        self.delivery = SimpleNamespace(city="Київ", np_office="Відділення №1", settlement_ref="s", city_ref="c", warehouse_ref="w")

    def cart(self, tees=0, hoodies=0, ordinary=0):
        cart = {}
        for key, product, qty in (("tee", self.tee, tees), ("hoodie", self.hoodie, hoodies), ("ordinary", self.ordinary, ordinary)):
            if qty:
                cart[f"{product.pk}:M"] = {"product_id": product.pk, "size": "M", "qty": qty}
        return cart

    def store_cart(self, cart):
        session = self.client.session
        session["cart"] = cart
        session.save()

    def invoice(self, pay_type="online_full"):
        with patch("storefront.views.monobank.resolve_delivery_selection", return_value=self.delivery), patch("storefront.views.monobank._monobank_api_request", return_value={"invoiceId": "brigade-invoice", "pageUrl": "https://pay.example/brigade"}) as provider, patch("storefront.views.monobank.record_initiate_checkout"):
            response = self.client.post(reverse("monobank_create_invoice"), data=json.dumps({"full_name": "Test Buyer", "phone": "+380501234567", "pay_type": pay_type}), content_type="application/json")
        return response, provider

    def test_exact_legacy_product_slugs_are_guarded_before_assignment(self):
        self.assertTrue(get_product_brigade_policy(self.tee)["is_brigade"])
        self.assertFalse(get_product_brigade_policy(self.ordinary)["is_brigade"])

    def test_future_brigade_taxonomy_requires_full_payment_without_225_offer(self):
        collection = MerchCollection.objects.create(slug="new-brigade", kind="brigade", name_uk="Нова бригада")
        ProductMerchCollection.objects.create(product=self.ordinary, collection=collection)
        quote = calculate_brigade_cart_pricing(self.cart(ordinary=3))
        self.assertTrue(quote.full_payment_only)
        self.assertEqual(quote.subtotal, Decimal("2700.00"))
        self.assertEqual(quote.promo_eligible_subtotal, 0)
        self.assertEqual(quote.offer_discount_total, 0)

    def test_offer_matrix_and_no_duplicate_discount(self):
        for tees, hoodies, expected in ((1, 0, 880), (2, 0, 1600), (3, 0, 2400), (0, 1, 1995), (0, 2, 3700), (1, 1, 2650), (2, 1, 3450), (3, 2, 6100), (1, 3, 6350)):
            with self.subTest(tees=tees, hoodies=hoodies):
                quote = calculate_brigade_cart_pricing(self.cart(tees, hoodies))
                self.assertEqual(quote.subtotal, Decimal(expected))

    def test_offer_preserves_variant_override_and_option_surcharge(self):
        ProductOptionProfile.objects.create(product=self.tee, option_key="fit=oversize", option_values={"fit": "oversize"}, price_delta=150)
        cart = self.cart(tees=3)
        item = next(iter(cart.values()))
        item.update(color_variant_id=self.variant.pk, option_values={"fit": "oversize"}, price=1)
        quote = calculate_brigade_cart_pricing(cart)
        line = next(iter(quote.lines.values()))
        self.assertEqual(line.standalone_unit_price, Decimal("1030.00"))
        self.assertEqual(line.offer_unit_price, Decimal("950.00"))
        self.assertEqual(line.line_total, Decimal("2850.00"))

    def test_quantity_threshold_spans_distinct_cart_options(self):
        cart = self.cart(tees=1)
        second = dict(next(iter(cart.values())), qty=2, size="L", color_variant_id=self.variant.pk)
        cart[f"{self.tee.pk}:L:{self.variant.pk}"] = second
        quote = calculate_brigade_cart_pricing(cart)
        self.assertEqual(quote.subtotal, Decimal("2400.00"))
        self.assertEqual(sum(line.discounted_qty for line in quote.lines.values()), 3)

    def test_promo_base_excludes_brigade_even_without_quantity_offer(self):
        quote = calculate_brigade_cart_pricing(self.cart(tees=1, ordinary=2))
        self.assertEqual(quote.subtotal, Decimal("2680.00"))
        self.assertEqual(quote.promo_eligible_subtotal, Decimal("1800.00"))

    def test_existing_site_discount_chooses_best_price_without_stacking(self):
        self.tee.discount_percent = 20
        self.tee.save(update_fields=["discount_percent"])
        for qty in (1, 3):
            quote = calculate_brigade_cart_pricing(self.cart(tees=qty))
            self.assertEqual(quote.subtotal, Decimal("704.00") * qty)
            self.assertEqual(quote.offer_discount_total, 0)
        self.tee.discount_percent = 5
        self.tee.save(update_fields=["discount_percent"])
        quote = calculate_brigade_cart_pricing(self.cart(tees=3))
        self.assertEqual(quote.subtotal, Decimal("2400.00"))
        self.assertEqual(quote.offer_discount_total, Decimal("108.00"))

    def test_mixed_partial_payment_is_rejected_before_provider_or_attempt(self):
        self.store_cart(self.cart(tees=1, ordinary=1))
        for pay_type in ("prepay_200", "cod"):
            response, provider = self.invoice(pay_type)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error_code"], "brigade_full_payment_required")
            provider.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_frozen_partial_bundle_prices_and_invoice_amount_are_exact(self):
        self.store_cart(self.cart(tees=2, hoodies=1))
        response, provider = self.invoice()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"], response.content)
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.payment_amount, Decimal("3450.00"))
        entries = attempt.cart_snapshot["cart"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(Decimal(item["unit_price"]) * item["qty"] for item in entries), attempt.gross_amount)
        payload = provider.call_args.kwargs["json_payload"]
        self.assertEqual(payload["amount"], 345000)
        self.assertEqual(sum(item["sum"] for item in payload["merchantPaymInfo"]["basketOrder"]), payload["amount"])

    def test_cart_api_summary_mutations_and_checkout_use_same_quote(self):
        cart = self.cart(tees=3, hoodies=1)
        self.store_cart(cart)
        api = self.client.get(reverse("cart_items_api")).json()
        self.assertEqual(api["subtotal"], 4250.0)
        self.assertFalse(api["prepay_allowed"])
        self.assertEqual(self.client.get(reverse("cart_summary")).json()["total"], 4250.0)
        tee_key = next(key for key, item in cart.items() if item["product_id"] == self.tee.pk)
        updated = self.client.post(reverse("update_cart"), {"cart_key": tee_key, "qty": 2}).json()
        self.assertEqual(updated["total"], 3450.0)
        hoodie_key = next(key for key, item in cart.items() if item["product_id"] == self.hoodie.pk)
        removed = self.client.post(reverse("cart_remove"), {"key": hoodie_key}).json()
        self.assertEqual(removed["total"], 1600.0)
        self.assertEqual(removed["brigade_discount_total"], 160.0)
        updated = self.client.post(reverse("update_cart"), {"cart_key": tee_key, "qty": 1}).json()
        self.assertEqual(updated["total"], 880.0)
        self.assertEqual(updated["brigade_discount_total"], 0)

    def test_promo_cart_and_payment_reservation_use_only_ordinary_lines(self):
        user = get_user_model().objects.create_user(username="brigade-buyer", password="test")
        self.client.force_login(user)
        from accounts.models import UserProfile
        UserProfile.objects.get_or_create(user=user)
        promo = PromoCode.objects.create(code="BRIGADE-MIX", discount_type="percentage", discount_value=10, is_active=True, valid_from=timezone.now()-timedelta(days=1), valid_until=timezone.now()+timedelta(days=1), one_time_per_user=False)
        self.store_cart(self.cart(tees=3, ordinary=1))
        applied = self.client.post(reverse("apply_promo_code"), {"promo_code": promo.code}).json()
        self.assertTrue(applied["success"], applied)
        self.assertEqual(applied["discount"], 90.0)
        self.assertEqual(self.client.get(reverse("cart_items_api")).json()["total"], 3210.0)
        response, provider = self.invoice()
        self.assertTrue(response.json()["success"], response.content)
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.discount_amount, Decimal("90.00"))
        self.assertEqual(attempt.payment_amount, Decimal("3210.00"))

    def test_payment_method_cannot_be_changed_to_partial_for_brigade_order(self):
        from orders.models import OrderItem
        user = get_user_model().objects.create_user(username="order-owner")
        self.client.force_login(user)
        order = Order.objects.create(user=user, full_name="Owner", phone="380501234567", city="Київ", np_office="1", total_sum=880, pay_type="online_full")
        OrderItem.objects.create(order=order, product=self.tee, title=self.tee.title, qty=1, unit_price=880, line_total=880)
        response = self.client.post(reverse("update_payment_method"), {"order_id": order.pk, "payment_method": "partial"})
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.pay_type, "online_full")

    def test_instagram_assisted_quote_uses_same_bundle_and_rejects_prepayment(self):
        from management.models import IgClient
        from management.services.ig_checkout import validate_checkout_items, CheckoutConfigurationError
        client = IgClient.get_or_create_for_sender("brigade-test")
        specs = list(self.cart(tees=2, hoodies=1).values())
        quote = validate_checkout_items(client=client, item_specs=specs)
        self.assertEqual(quote.catalog_total, Decimal("3450.00"))
        with self.assertRaises(CheckoutConfigurationError) as error:
            validate_checkout_items(client=client, item_specs=specs, pay_type="prepayment", requested_payment_amount=200)
        self.assertEqual(error.exception.code, "brigade_full_payment_required")

    def test_legacy_assisted_invoice_guard_rejects_historical_partial_proposal(self):
        from django.contrib.auth.models import AnonymousUser
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_checkout_payment import lock_proposal_details, CheckoutPaymentError
        client = IgClient.get_or_create_for_sender("brigade-legacy-test")
        proposal = create_or_update_proposal(client=client, item_specs=list(self.cart(tees=1).values()), pay_type="online_full")
        proposal.pay_type = "prepayment"
        proposal.save(update_fields=["pay_type"])
        request = RequestFactory().post("/checkout/")
        request.user = AnonymousUser()
        request.session = self.client.session
        with self.assertRaises(CheckoutPaymentError) as error:
            lock_proposal_details(proposal, payload={}, request=request)
        self.assertEqual(error.exception.code, "brigade_full_payment_required")
        self.assertFalse(PaymentAttempt.objects.exists())

    @override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
    def test_v2_assisted_invoice_rejects_forged_partial_before_generation(self):
        from django.contrib.auth.models import AnonymousUser
        from management.models import IgClient, IgCheckoutInvoiceGeneration
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_checkout_generation import _prepare_generation
        from management.services.ig_checkout_payment import CheckoutPaymentError
        client = IgClient.get_or_create_for_sender("brigade-v2-test")
        proposal = create_or_update_proposal(client=client, item_specs=list(self.cart(tees=1).values()), pay_type="online_full")
        self.assertEqual(proposal.payment_policy, proposal.PaymentPolicy.FULL_ONLY)
        request = RequestFactory().post("/checkout/")
        request.user = AnonymousUser()
        request.session = self.client.session
        with self.assertRaises(CheckoutPaymentError) as error:
            _prepare_generation(proposal, payload={"payment_choice": "prepay_200_cod"}, request=request)
        self.assertEqual(error.exception.code, "brigade_full_payment_required")
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())

    def test_assisted_context_hides_existing_partial_url_without_mutating_history(self):
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        from storefront.views.ig_checkout import _proposal_context
        client = IgClient.get_or_create_for_sender("brigade-context-test")
        proposal = create_or_update_proposal(client=client, item_specs=list(self.cart(tees=1).values()), pay_type="online_full")
        attempt = PaymentAttempt.objects.create(fingerprint="brigade-old-partial", pay_type="prepayment", gross_amount=880, payable_amount=880, payment_amount=200, status="processing", invoice_url="https://pay.example/historical-partial")
        proposal.pay_type = "prepayment"
        proposal.requested_payment_amount = 200
        proposal.payment_attempt = attempt
        proposal.status = proposal.Status.INVOICE_CREATED
        proposal.save(update_fields=["pay_type", "requested_payment_amount", "payment_attempt", "status"])
        request = RequestFactory().get("/checkout/")
        request.session = self.client.session
        context = _proposal_context(proposal, request=request)
        self.assertEqual(context["payment_url"], "")
        self.assertFalse(context["payable"])
        self.assertFalse(context["reissue_allowed"])
        self.assertEqual(context["proposal"]["charge_now"], "200.00")
        attempt.refresh_from_db()
        self.assertEqual(attempt.invoice_url, "https://pay.example/historical-partial")
        self.assertEqual(attempt.status, "processing")
        proposal.status = proposal.Status.PAID
        context = _proposal_context(proposal, request=request)
        self.assertEqual(context["checkout_state"], "paid")
        self.assertEqual(context["proposal"]["charge_now"], "200.00")

    @override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
    def test_existing_v2_policy_cannot_display_partial_option_for_current_brigade(self):
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        from storefront.views.ig_checkout import _proposal_context
        client = IgClient.get_or_create_for_sender("brigade-context-v2-test")
        proposal = create_or_update_proposal(client=client, item_specs=list(self.cart(tees=1).values()), pay_type="online_full")
        proposal.payment_policy = proposal.PaymentPolicy.FULL_OR_200_COD
        request = RequestFactory().get("/checkout/")
        request.session = self.client.session
        context = _proposal_context(proposal, request=request, form_values={"payment_choice": "prepay_200_cod"})
        self.assertEqual([option["value"] for option in context["payment_options"]], ["online_full"])
        self.assertTrue(context["payment_options"][0]["selected"])
