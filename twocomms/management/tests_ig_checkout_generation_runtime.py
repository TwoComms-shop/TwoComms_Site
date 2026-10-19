import copy
import hashlib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import (
    IgCheckoutAccessToken,
    IgCheckoutInvoiceGeneration,
    IgCheckoutInventoryReservation,
    IgCheckoutProposal,
    IgCheckoutProposalItem,
    IgBotNotification,
    IgClient,
    IgDeal,
    IgFollowUpTask,
    IgPaymentEvent,
    IgPaymentProjection,
    InstagramBotMessage,
)
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_checkout_policy import (
    PREPAY_200_QUICK_REPLY,
    resolve_payment_policy,
)
from management.services.ig_commercial_episodes import ensure_episode_for_deal
from orders.models import Order, PaymentAttempt
from orders.nova_poshta_checkout import (
    build_city_choice_token,
    build_warehouse_choice_token,
)
from storefront.models import Category, Product, ProductFitOption, PromoCode, PromoCodeUsage


def displayed_revision_payload(response):
    """Read only the revision inputs actually displayed by the hosted GET."""
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if tag == "input" and attributes.get("name") in {"expected_revision", "expected_digest", "delivery_policy_digest"}:
                name, value = attributes["name"], attributes.get("value", "")
                if name in self.values and self.values[name] != value:
                    raise AssertionError("Hosted forms displayed conflicting quote revisions")
                self.values[name] = value
    parser = Inputs()
    parser.feed(response.content.decode("utf-8"))
    return parser.values


@override_settings(
    IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
)
class CheckoutGenerationRuntimeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client_row = IgClient.get_or_create_for_sender(
            "checkout-generation-runtime"
        )
        category = Category.objects.create(
            name="Generation runtime",
            slug="generation-runtime",
        )
        self.product = Product.objects.create(
            title="Generation shirt",
            slug="generation-shirt",
            category=category,
            price=Decimal("900.00"),
            status="published",
        )
        self.fit = ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Classic",
            is_default=True,
            is_active=True,
        )

    def _item(self):
        return {
            "product_id": self.product.pk,
            "qty": 1,
            "fit_option_code": self.fit.code,
            "size": "M",
        }

    def _delivery_payload(self, **values):
        payload = {
            "full_name": "Іван Петренко",
            "phone": "+380501112233",
            "email": "buyer@example.com",
            "city": "Київ",
            "np_settlement_ref": "settlement-ref",
            "np_city_ref": "city-ref",
            "np_city_token": build_city_choice_token({
                "label": "Київ",
                "settlement_ref": "settlement-ref",
                "city_ref": "city-ref",
            }),
            "np_office": "Відділення №12",
            "np_warehouse_ref": "warehouse-ref",
            "np_warehouse_token": build_warehouse_choice_token({
                "label": "Відділення №12",
                "ref": "warehouse-ref",
                "kind": "branch",
                "city_ref": "city-ref",
            }),
        }
        # This remains the captured displayed page, never a fresh model read.
        # Explicit stale/missing overrides below therefore stay meaningful.
        payload.update(getattr(self, "_hosted_revision_payload", {}))
        payload.update(values)
        return payload

    def _open(self, proposal):
        raw, token = IgCheckoutAccessToken.issue(proposal=proposal)
        response = self.client.get(
            reverse("ig_checkout_token_entry", kwargs={"token": raw})
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response["Location"])
        self._hosted_revision_payload = displayed_revision_payload(page)
        return response, token

    def _create_invoice(self, proposal, *, invoice_id, payment_choice="online_full"):
        self._open(proposal)
        url = reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id})
        consent = {}
        if payment_choice != "online_full":
            # The hosted GET selects full payment. A different customer choice
            # must first display its actual terms, then accept any fresh loss.
            preview = self.client.post(url, {**self._hosted_revision_payload,
                "action": "promo_preview", "payment_choice": payment_choice},
                HTTP_ACCEPT="application/json")
            self.assertEqual(preview.status_code, 200, preview.content)
            displayed = preview.json()["cart_state"]
            shipping = displayed.get("free_delivery") or {}
            self._hosted_revision_payload = {
                "expected_revision": str(displayed["revision"]),
                "expected_digest": displayed["digest"],
                "delivery_policy_digest": shipping.get("ack_digest", ""),
            }
            if shipping.get("ack_required"):
                consent["delivery_policy_ack"] = "1"
        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": invoice_id,
                "pageUrl": f"https://pay.example/{invoice_id}",
            },
        ):
            response = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                self._delivery_payload(payment_choice=payment_choice, **consent),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        return (
            IgCheckoutInvoiceGeneration.objects.filter(proposal=proposal)
            .order_by("-generation")
            .first()
        )

    def _create_authenticated_promo_invoice(self, *, code):
        from product_catalog.models import ProductInventoryPolicy
        from productcolors.models import Color, ProductColorVariant

        user = get_user_model().objects.create_user(
            username=f"promo-owner-{code.casefold()}"
        )
        self.client.force_login(user)
        promo = PromoCode.objects.create(
            code=code,
            discount_type="percentage",
            discount_value=Decimal("10.00"),
            max_uses=10,
            one_time_per_user=True,
        )
        color = Color.objects.create(
            name=f"{code} color",
            primary_hex="#222222",
        )
        variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            stock=2,
        )
        ProductInventoryPolicy.objects.create(
            product=self.product,
            source=ProductInventoryPolicy.Source.CATALOG_VARIANT,
        )
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[{**self._item(), "color_variant_id": variant.pk}],
            allow_promo=True,
        )
        self._open(proposal)
        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": f"v2-{code.casefold()}",
                "pageUrl": f"https://pay.example/v2-{code.casefold()}",
            },
        ):
            response = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                self._delivery_payload(promo_code=promo.code),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal)
        return proposal, generation, generation.payment_attempt, promo, variant

    def _apply_exact_provider_terminal(self, generation, *, status="failure"):
        from management.services.ig_checkout_generation import (
            apply_generation_provider_status,
        )

        attempt = generation.payment_attempt
        return apply_generation_provider_status(
            attempt.pk,
            status,
            payload={
                "status": status,
                "invoiceId": attempt.monobank_invoice_id,
                "merchantPaymInfo": {"reference": attempt.reference},
                "ccy": 980,
            },
            source="provider_pull",
        )

    def _provider_create_with_webhook_winner(self, *, timeout_after_webhook):
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)

        class AmbiguousTimeout(RuntimeError):
            ambiguous = True

        def provider_interleaving(*_args, **_kwargs):
            generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal)
            order, created = _apply_payment_attempt_status(
                generation.payment_attempt,
                "success",
                payload={"paidAmount": 90000},
                source="provider_pull",
            )
            self.assertIsNotNone(order)
            self.assertTrue(created)
            if timeout_after_webhook:
                raise AmbiguousTimeout("transport timed out after webhook winner")
            return {
                "invoiceId": "settled-after-webhook",
                "pageUrl": "https://pay.example/settled-after-webhook",
            }

        with patch(
            "storefront.views.monobank._monobank_api_request",
            side_effect=provider_interleaving,
        ):
            response = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                self._delivery_payload(),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 400)
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal)
        attempt = generation.payment_attempt
        proposal.refresh_from_db()
        generation.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(proposal.status, proposal.Status.PAID)
        self.assertEqual(proposal.winner_invoice_generation_id, generation.pk)
        self.assertEqual(generation.state, generation.State.PAID_WINNER)
        self.assertEqual(attempt.status, PaymentAttempt.Status.CONVERTED)
        self.assertIsNotNone(attempt.order_id)
        self.assertEqual(Order.objects.count(), 1)
        ignored = [
            event
            for event in generation.events.all()
            if (event.payload or {}).get("ignored_late_transport_outcome")
        ]
        self.assertEqual(len(ignored), 1)
        if timeout_after_webhook:
            self.assertFalse(generation.provider_invoice_id)
            self.assertFalse(attempt.invoice_url)
        else:
            self.assertEqual(
                generation.provider_invoice_id,
                "settled-after-webhook",
            )
            self.assertEqual(
                attempt.invoice_url,
                "https://pay.example/settled-after-webhook",
            )
            self.assertEqual(
                ignored[0].payload["attempt_reference"],
                attempt.reference,
            )
        return proposal, generation, attempt

    def test_canary_proposal_is_12_hours_and_creates_no_stock_or_reminder(self):
        now = timezone.now()
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="prepayment",
            requested_payment_amount=Decimal("200.00"),
            item_specs=[self._item()],
        )

        self.assertTrue(proposal.assisted_checkout_v2)
        self.assertEqual(proposal.payment_policy, proposal.PaymentPolicy.FULL_ONLY)
        self.assertEqual(proposal.pay_type, proposal.PayType.ONLINE_FULL)
        self.assertEqual(proposal.requested_payment_amount, proposal.quoted_total)
        self.assertGreater(proposal.expires_at, now + timedelta(hours=11, minutes=59))
        self.assertLess(proposal.expires_at, now + timedelta(hours=12, minutes=1))
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        self.assertFalse(
            IgFollowUpTask.objects.filter(
                event_key__startswith="proposal_expired:"
            ).exists()
        )
        entry, _token = self._open(proposal)
        page = self.client.get(entry["Location"])
        self.assertTrue(page.context["cart_state"]["can_edit"])
        self.assertContains(page, "Ваш кошик можна змінити")
        self.assertContains(page, "захищене посилання діють 12 годин")
        self.assertContains(page, "Після кожної зміни перевіряємо суму й умови доставки")
        self.assertContains(page, "Якщо потрібне нове погодження, покажемо це перед оплатою")
        self.assertNotContains(page, "Товари й ціна зафіксовані")
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_initial_proposal_without_generation_keeps_editable_checkout_form(self):
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)
        page = self.client.get(
            reverse(
                "ig_checkout_proposal",
                kwargs={"proposal_id": proposal.public_id},
            )
        )
        self.assertEqual(page.context["checkout_state"], "ready")
        self.assertTrue(page.context["payable"])
        self.assertFalse(page.context["reissue_allowed"])
        self.assertContains(page, 'name="full_name"')
        self.assertContains(page, 'name="phone"')
        self.assertContains(page, 'name="np_city_token"')
        self.assertContains(page, 'name="np_warehouse_token"')
        self.assertContains(page, 'data-payment-submit')

    def test_create_response_after_webhook_winner_only_settles_identity(self):
        self._provider_create_with_webhook_winner(timeout_after_webhook=False)

    def test_create_timeout_after_webhook_winner_is_ignored_transport(self):
        self._provider_create_with_webhook_winner(timeout_after_webhook=True)

    def test_old_create_settlement_after_newer_winner_never_mutates_paid_proposal(self):
        from management.services.ig_checkout_generation import (
            _prepare_generation,
            _persist_provider_failure,
            _persist_provider_success,
        )
        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        request = RequestFactory().post("/internal-checkout-prepare/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session.save()
        request.user = AnonymousUser()
        _locked, first, first_attempt, _values, reused = _prepare_generation(
            proposal,
            request=request,
            payload=self._delivery_payload(),
        )
        self.assertFalse(reused)
        self.assertEqual(
            terminalize_payment_attempt(
                first_attempt.pk,
                terminal_status=PaymentAttempt.Status.CANCELLED,
                reason="checkout_session_reset",
                source="checkout_session_reset",
                require_due=False,
            ).outcome,
            "terminalized",
        )
        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": "newer-winner",
                "pageUrl": "https://pay.example/newer-winner",
            },
        ):
            created = self.client.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(first.generation)},
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(created.status_code, 200)
        second = IgCheckoutInvoiceGeneration.objects.get(
            proposal=proposal,
            generation=2,
        )
        winner_order, _ = _apply_payment_attempt_status(
            second.payment_attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )

        _proposal, _generation, _attempt, accepted = _persist_provider_success(
            first.payment_attempt_id,
            invoice_id="late-old-create",
            invoice_url="https://pay.example/late-old-create",
            invoice_payload={"validity": 1500},
            creation={
                "invoiceId": "late-old-create",
                "pageUrl": "https://pay.example/late-old-create",
            },
        )
        self.assertFalse(accepted)
        first.refresh_from_db()
        first.payment_attempt.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(first.state, first.State.LATE_PROVIDER_REVIEW)
        self.assertEqual(first.provider_invoice_id, "late-old-create")
        self.assertEqual(
            first.payment_attempt.invoice_url,
            "https://pay.example/late-old-create",
        )
        self.assertEqual(proposal.status, proposal.Status.PAID)
        self.assertEqual(proposal.winner_invoice_generation_id, second.pk)
        proposal.deal.refresh_from_db()
        self.assertEqual(proposal.deal.order_id, winner_order.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(winner_order.pk, Order.objects.get().pk)

        _persist_provider_success(
            first.payment_attempt_id,
            invoice_id="conflicting-late-id",
            invoice_url="https://pay.example/conflicting-late-id",
            invoice_payload={"validity": 1500},
            creation={
                "invoiceId": "conflicting-late-id",
                "pageUrl": "https://pay.example/conflicting-late-id",
            },
        )
        first.refresh_from_db()
        first.payment_attempt.refresh_from_db()
        self.assertEqual(first.provider_invoice_id, "late-old-create")
        self.assertEqual(
            first.payment_attempt.monobank_invoice_id,
            "late-old-create",
        )

        outcome = _persist_provider_failure(
            first.payment_attempt_id,
            ambiguous=True,
            reason="timeout after newer winner",
        )
        self.assertEqual(outcome, "ignored_after_winner")
        first.refresh_from_db()
        second.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(first.state, first.State.LATE_PROVIDER_REVIEW)
        self.assertEqual(second.state, second.State.PAID_WINNER)
        self.assertEqual(proposal.status, proposal.Status.PAID)
        self.assertEqual(Order.objects.count(), 1)

    def test_only_latest_owned_user_question_unlocks_200_cod(self):
        old = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Можно 200 грн предоплаты, остальное наложкой?",
        )
        later = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Добре",
        )
        rejected = resolve_payment_policy(
            client=self.client_row,
            evidence_message_ids=[old.pk],
        )
        self.assertEqual(rejected.policy, IgCheckoutProposal.PaymentPolicy.FULL_ONLY)

        later.quick_reply_payload = PREPAY_200_QUICK_REPLY
        later.save(update_fields=["quick_reply_payload"])
        allowed = resolve_payment_policy(
            client=self.client_row,
            evidence_message_ids=[later.pk],
        )
        self.assertEqual(
            allowed.policy,
            IgCheckoutProposal.PaymentPolicy.FULL_OR_200_COD,
        )
        self.assertEqual(allowed.evidence_kind, "quick_reply")

    @patch("storefront.views.monobank._monobank_api_request")
    def test_generation_invoice_has_1500_validity_and_stable_series(self, provider):
        provider.return_value = {
            "invoiceId": "v2-invoice-1",
            "pageUrl": "https://pay.example/v2-invoice-1",
        }
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)

        response = self.client.post(
            reverse(
                "ig_checkout_proposal",
                kwargs={"proposal_id": proposal.public_id},
            ),
            self._delivery_payload(payment_choice="online_full"),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        generation = IgCheckoutInvoiceGeneration.objects.get(
            proposal=proposal,
            generation=1,
        )
        attempt = generation.payment_attempt
        self.assertEqual(generation.generation, 1)
        self.assertEqual(generation.active_slot, 1)
        self.assertEqual(generation.state, generation.State.INVOICE_CREATED)
        self.assertEqual(attempt.checkout_generation, 1)
        self.assertEqual(attempt.checkout_series_key, generation.series_key)
        self.assertEqual(attempt.invoice_payload["request"]["validity"], 1500)
        self.assertEqual(provider.call_count, 1)
        self.assertGreater(generation.expires_at, timezone.now() + timedelta(minutes=24))
        self.assertLess(generation.expires_at, timezone.now() + timedelta(minutes=26))

    def test_success_status_requires_exact_explicit_provider_amount(self):
        from storefront.views.monobank import (
            _apply_payment_attempt_status,
            _resolve_attempt_invoice_status,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(proposal, invoice_id="v2-exact-amount")
        attempt = generation.payment_attempt
        base = {
            "status": "success",
            "invoiceId": attempt.monobank_invoice_id,
            "merchantPaymInfo": {"reference": attempt.reference},
            "ccy": 980,
        }
        invalid_payloads = (
            dict(base),
            {**base, "paidAmount": "90000.0"},
            {**base, "paidAmount": "9" * 19},
            {**base, "paidAmount": 89999},
            {**base, "paidAmount": 90000, "finalAmount": 89999},
            {**base, "paidAmount": 90000, "ccy": 840},
            {
                **base,
                "paidAmount": 90000,
                "merchantPaymInfo": {"reference": "wrong-reference"},
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch(
                "storefront.views.monobank._monobank_api_request",
                return_value=payload,
            ):
                status, _provider_payload = _resolve_attempt_invoice_status(
                    attempt,
                    attempt.monobank_invoice_id,
                )
                self.assertEqual(status, "processing")

        no_order, no_created = _apply_payment_attempt_status(
            attempt,
            "success",
            payload={},
            source="webhook",
        )
        self.assertIsNone(no_order)
        self.assertFalse(no_created)
        attempt.refresh_from_db()
        generation.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.PROCESSING)
        self.assertFalse(attempt.checkout_winner_claimed)
        self.assertIsNone(attempt.order_id)
        self.assertTrue(
            (attempt.event_state or {}).get(
                "payment_amount_reconciliation_pending"
            )
        )
        self.assertIsNone(generation.winner_slot)
        amount_review = generation.events.filter(
            kind="provider_ambiguous",
            payload__amount_valid=False,
        )
        self.assertEqual(amount_review.count(), 1)
        self.assertFalse(IgPaymentEvent.objects.filter(deal=proposal.deal).exists())
        self.assertFalse(IgPaymentProjection.objects.filter(deal=proposal.deal).exists())

        valid_order, valid_created = _apply_payment_attempt_status(
            attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )
        self.assertTrue(valid_created)
        self.assertIsNotNone(valid_order)

    @patch("storefront.views.monobank._monobank_api_request")
    def test_provider_crossed_failure_without_identity_cannot_retry(self, provider):
        class DeterministicProviderError(RuntimeError):
            ambiguous = False

        provider.side_effect = DeterministicProviderError(
            "provider returned a deterministic error without invoice identity"
        )
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        first = self.client.post(
            url,
            self._delivery_payload(),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(first.status_code, 400)
        generation = IgCheckoutInvoiceGeneration.objects.get(
            proposal=proposal,
            generation=1,
        )
        first_attempt = generation.payment_attempt
        self.assertEqual(generation.state, generation.State.FAILED)
        self.assertTrue(generation.provider_request_digest)
        self.assertFalse(generation.provider_invoice_id)
        self.assertFalse(first_attempt.monobank_invoice_id)
        retry_page = self.client.get(url)
        self.assertEqual(
            retry_page.context["checkout_state"],
            "cancellation_ambiguous",
        )
        self.assertFalse(retry_page.context["reissue_allowed"])
        self.assertFalse(retry_page.context["payable"])
        self.assertNotContains(retry_page, first_attempt.full_name)
        self.assertNotContains(retry_page, first_attempt.phone)
        self.assertNotContains(retry_page, first_attempt.email)
        self.assertNotContains(retry_page, 'name="full_name"')
        self.assertNotContains(retry_page, 'name="np_city_token"')
        self.assertNotContains(retry_page, 'data-payment-submit')
        second = self.client.post(
            url,
            {**getattr(self, "_hosted_revision_payload", {}),
                "reissue_generation": "1",
                "full_name": "Чужий Отримувач",
                "phone": "+380991234567",
                "email": "attacker@example.com",
                "city": "Львів",
                "np_office": "Чуже відділення",
                "payment_choice": "prepay_200_cod",
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error"], "provider_ambiguous")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            IgCheckoutInvoiceGeneration.objects.filter(proposal=proposal).count(),
            1,
        )

    def test_forged_200_cod_post_is_rejected_for_full_only_policy(self):
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)
        response = self.client.post(
            reverse(
                "ig_checkout_proposal",
                kwargs={"proposal_id": proposal.public_id},
            ),
            self._delivery_payload(payment_choice="prepay_200_cod"),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "payment_choice")
        self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())

    def test_explicit_cancelled_pre_dispatch_proof_allows_locked_retry(self):
        from management.services.ig_checkout_generation import _prepare_generation
        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)
        request = RequestFactory().post("/internal-checkout-prepare/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session.save()
        request.user = AnonymousUser()
        _locked, generation, attempt, _values, reused = _prepare_generation(
            proposal,
            request=request,
            payload=self._delivery_payload(),
        )
        self.assertFalse(reused)
        self.assertEqual(generation.state, generation.State.PROVIDER_INFLIGHT)
        self.assertFalse(generation.provider_request_digest)
        self.assertFalse(generation.provider_invoice_id)
        self.assertFalse(attempt.monobank_invoice_id)

        outcome = terminalize_payment_attempt(
            attempt.pk,
            terminal_status=PaymentAttempt.Status.CANCELLED,
            reason="checkout_session_reset",
            source="checkout_session_reset",
            require_due=False,
        )
        self.assertEqual(outcome.outcome, "terminalized")
        generation.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(generation.state, generation.State.CANCELLED)
        self.assertEqual(attempt.status, PaymentAttempt.Status.CANCELLED)
        proof = attempt.event_state["provider_boundary"]
        self.assertEqual(proof["state"], "cancelled_pre_dispatch")
        self.assertEqual(proof["generation_id"], generation.pk)

        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        page = self.client.get(url)
        self.assertEqual(page.context["checkout_state"], "generation_retryable")
        self.assertTrue(page.context["reissue_allowed"])
        self.assertNotContains(page, attempt.full_name)
        self.assertNotContains(page, attempt.phone)
        self.assertNotContains(page, 'name="full_name"')
        self.assertNotContains(page, 'data-payment-submit')
        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": "v2-predispatch-retry",
                "pageUrl": "https://pay.example/v2-predispatch-retry",
            },
        ) as provider:
            response = self.client.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}),
                    "reissue_generation": str(generation.generation),
                    "full_name": "Чужий Отримувач",
                    "phone": "+380991234567",
                },
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        provider.assert_called_once()
        replacement = PaymentAttempt.objects.get(
            checkout_series_key=generation.series_key,
            checkout_generation=2,
        )
        self.assertEqual(replacement.full_name, attempt.full_name)
        self.assertEqual(replacement.phone, attempt.phone)
        self.assertEqual(replacement.city, attempt.city)
        self.assertEqual(replacement.np_office, attempt.np_office)

    def test_cancelled_generation_all_grants_show_masked_server_retry_only(self):
        from management.services.ig_checkout_generation import (
            apply_generation_provider_status,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-cancelled-retry",
        )
        original = generation.payment_attempt
        share = self.client.post(
            reverse(
                "ig_checkout_share_token",
                kwargs={"proposal_id": proposal.public_id},
            )
        )
        self.assertEqual(share.status_code, 200)
        share_token = (
            urlparse(share.json()["url"]).path.rstrip("/").rsplit("/", 1)[-1]
        )
        forwarded = Client()
        self.assertEqual(
            forwarded.get(
                reverse("ig_checkout_token_entry", kwargs={"token": share_token})
            ).status_code,
            302,
        )
        apply_generation_provider_status(
            original.pk,
            "cancelled",
            payload={
                "status": "cancelled",
                "invoiceId": original.monobank_invoice_id,
                "merchantPaymInfo": {"reference": original.reference},
                "ccy": 980,
            },
            source="provider_pull",
        )
        generation.refresh_from_db()
        original.refresh_from_db()
        self.assertEqual(generation.state, generation.State.CANCELLED)
        self.assertEqual(original.status, PaymentAttempt.Status.CANCELLED)
        self.assertEqual(
            original.event_state["provider_terminal"]["provider_status"],
            "cancelled",
        )
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        for browser in (self.client, forwarded):
            page = browser.get(url)
            self.assertEqual(page.context["checkout_state"], "generation_retryable")
            self.assertTrue(page.context["reissue_allowed"])
            self.assertFalse(page.context["payable"])
            self.assertNotContains(page, original.full_name)
            self.assertNotContains(page, original.phone)
            self.assertNotContains(page, original.email)
            self.assertNotContains(page, 'name="full_name"')
            self.assertNotContains(page, 'name="phone"')
            self.assertNotContains(page, 'name="np_city_token"')
            self.assertNotContains(page, 'name="np_warehouse_token"')
            self.assertNotContains(page, 'data-payment-submit')

        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": "v2-cancelled-retry-2",
                "pageUrl": "https://pay.example/v2-cancelled-retry-2",
            },
        ) as provider:
            retried = forwarded.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}),
                    "reissue_generation": str(generation.generation),
                    "full_name": "Чужий Отримувач",
                    "phone": "+380991234567",
                    "payment_choice": "prepay_200_cod",
                },
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(retried.status_code, 200, retried.content)
        provider.assert_called_once()
        replacement = PaymentAttempt.objects.get(
            checkout_series_key=generation.series_key,
            checkout_generation=2,
        )
        self.assertEqual(replacement.full_name, original.full_name)
        self.assertEqual(replacement.phone, original.phone)
        self.assertEqual(replacement.email, original.email)
        self.assertEqual(replacement.city, original.city)
        self.assertEqual(replacement.np_office, original.np_office)
        self.assertEqual(
            replacement.instagram_checkout_generation.payment_choice,
            generation.payment_choice,
        )

    def test_local_reset_preserves_active_provider_invoice_and_never_reissues(self):
        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-active-local-reset",
        )
        attempt = generation.payment_attempt
        outcome = terminalize_payment_attempt(
            attempt.pk,
            terminal_status=PaymentAttempt.Status.CANCELLED,
            reason="checkout_session_reset",
            source="checkout_session_reset",
            require_due=False,
        )
        self.assertEqual(outcome.outcome, "provider_active")
        generation.refresh_from_db()
        attempt.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(generation.state, generation.State.INVOICE_CREATED)
        self.assertEqual(generation.active_slot, 1)
        self.assertEqual(attempt.status, PaymentAttempt.Status.PROCESSING)
        self.assertEqual(
            proposal.current_invoice_generation_id,
            generation.pk,
        )
        self.assertEqual(proposal.payment_attempt_id, attempt.pk)
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        page = self.client.get(url)
        self.assertEqual(page.context["checkout_state"], "pending")
        self.assertEqual(page.context["payment_url"], attempt.invoice_url)
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            replay = self.client.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        provider.assert_not_called()
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["reused"])
        self.assertEqual(replay.json()["invoice_url"], attempt.invoice_url)
        self.assertEqual(
            IgCheckoutInvoiceGeneration.objects.filter(proposal=proposal).count(),
            1,
        )

    def test_provider_terminal_proof_tampering_fails_closed(self):
        from management.services.ig_checkout_generation import (
            _retry_source_is_safe,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-proof-integrity",
        )
        attempt = generation.payment_attempt
        self._apply_exact_provider_terminal(generation, status="cancelled")
        generation.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(_retry_source_is_safe(generation, attempt))
        original_event_state = copy.deepcopy(attempt.event_state)
        original_proof = copy.deepcopy(
            original_event_state["provider_terminal"]
        )
        tampered_proofs = {
            "arbitrary_evidence": {
                **original_proof,
                "evidence_digest": "X" * 64,
            },
            "missing_timestamp": {
                key: value
                for key, value in original_proof.items()
                if key != "observed_at"
            },
            "invalid_timestamp": {
                **original_proof,
                "observed_at": "not-a-timestamp",
            },
            "future_timestamp": {
                **original_proof,
                "observed_at": (
                    timezone.now() + timedelta(minutes=1)
                ).isoformat(),
            },
            "wrong_reference": {
                **original_proof,
                "attempt_reference": "wrong-reference",
            },
            "wrong_currency": {**original_proof, "currency": "840"},
            "wrong_status": {
                **original_proof,
                "provider_status": "failure",
            },
            "wrong_source": {**original_proof, "source": "local_forged"},
            "cross_generation": {
                **original_proof,
                "generation_id": generation.pk + 999,
            },
            "cross_token": {
                **original_proof,
                "provider_call_token_digest": "0" * 64,
            },
            "cross_invoice": {
                **original_proof,
                "provider_invoice_id_digest": "1" * 64,
            },
            "modified_hmac": {**original_proof, "proof_hmac": "2" * 64},
            "wrong_schema": {**original_proof, "schema_version": 999},
            "wrong_key_id": {**original_proof, "hmac_key_id": "unknown"},
            "missing_hmac": {
                key: value
                for key, value in original_proof.items()
                if key != "proof_hmac"
            },
            "unknown_field": {**original_proof, "unexpected": "value"},
        }
        for name, proof in tampered_proofs.items():
            with self.subTest(name=name):
                attempt.event_state = {
                    **copy.deepcopy(original_event_state),
                    "provider_terminal": proof,
                }
                self.assertFalse(_retry_source_is_safe(generation, attempt))

        attempt.event_state = copy.deepcopy(original_event_state)
        self.assertTrue(_retry_source_is_safe(generation, attempt))
        persisted = copy.deepcopy(original_event_state)
        persisted["provider_terminal"]["proof_hmac"] = "3" * 64
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            event_state=persisted
        )
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            page = self.client.get(url)
            status = self.client.get(
                reverse(
                    "ig_checkout_status",
                    kwargs={"proposal_id": proposal.public_id},
                )
            )
            posted = self.client.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        provider.assert_not_called()
        self.assertEqual(page.context["checkout_state"], "cancellation_ambiguous")
        self.assertEqual(status.json()["state"], "cancellation_ambiguous")
        self.assertEqual(posted.status_code, 400)
        self.assertEqual(posted.json()["error"], "provider_ambiguous")

    def test_retry_identity_matrix_requires_exact_match_or_predispatch_proof(self):
        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-exact-retry-identity",
        )
        attempt = generation.payment_attempt
        expired_at = timezone.now() - timedelta(seconds=1)
        IgCheckoutInvoiceGeneration.objects.filter(pk=generation.pk).update(
            expires_at=expired_at
        )
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            invoice_expires_at=expired_at
        )
        self.assertEqual(
            terminalize_payment_attempt(
                attempt.pk,
                terminal_status=PaymentAttempt.Status.EXPIRED,
                reason="invoice_expired",
                source="system_expiry",
                require_due=True,
            ).outcome,
            "terminalized",
        )
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        exact_page = self.client.get(url)
        self.assertEqual(
            exact_page.context["checkout_state"],
            "generation_expired_reissuable",
        )
        self.assertTrue(exact_page.context["reissue_allowed"])

        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            monobank_invoice_id="v2-mismatched-attempt-identity"
        )
        mismatch_outcome = terminalize_payment_attempt(
            attempt.pk,
            terminal_status=PaymentAttempt.Status.EXPIRED,
            reason="invoice_expired",
            source="system_expiry",
            require_due=True,
        )
        self.assertEqual(mismatch_outcome.outcome, "provider_ambiguous")
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            mismatch_page = self.client.get(url)
            mismatch_status = self.client.get(
                reverse(
                    "ig_checkout_status",
                    kwargs={"proposal_id": proposal.public_id},
                )
            )
            mismatch_post = self.client.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        provider.assert_not_called()
        self.assertEqual(
            mismatch_page.context["checkout_state"],
            "cancellation_ambiguous",
        )
        self.assertFalse(mismatch_page.context["reissue_allowed"])
        self.assertEqual(
            mismatch_status.json()["state"],
            "cancellation_ambiguous",
        )
        self.assertEqual(mismatch_post.status_code, 400)
        self.assertEqual(mismatch_post.json()["error"], "provider_ambiguous")

        cancelled_client = IgClient.get_or_create_for_sender(
            "checkout-empty-cancelled-identity"
        )
        cancelled_proposal = create_or_update_proposal(
            client=cancelled_client,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        cancelled_generation = self._create_invoice(
            cancelled_proposal,
            invoice_id="v2-empty-cancelled-identity",
        )
        cancelled_attempt = cancelled_generation.payment_attempt
        IgCheckoutInvoiceGeneration.objects.filter(
            pk=cancelled_generation.pk
        ).update(
            state=IgCheckoutInvoiceGeneration.State.CANCELLED,
            provider_invoice_id=None,
            active_slot=None,
        )
        PaymentAttempt.objects.filter(pk=cancelled_attempt.pk).update(
            status=PaymentAttempt.Status.CANCELLED,
            monobank_invoice_id="",
            invoice_url="",
            event_state={
                key: value
                for key, value in (cancelled_attempt.event_state or {}).items()
                if key != "provider_boundary"
            },
        )
        empty_outcome = terminalize_payment_attempt(
            cancelled_attempt.pk,
            terminal_status=PaymentAttempt.Status.CANCELLED,
            reason="checkout_session_reset",
            source="checkout_session_reset",
            require_due=False,
        )
        self.assertEqual(empty_outcome.outcome, "provider_ambiguous")
        cancelled_proposal.refresh_from_db()
        self.assertEqual(
            cancelled_proposal.status,
            cancelled_proposal.Status.MANAGER_REVIEW,
        )
        cancelled_url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": cancelled_proposal.public_id},
        )
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            empty_page = self.client.get(cancelled_url)
            empty_status = self.client.get(
                reverse(
                    "ig_checkout_status",
                    kwargs={"proposal_id": cancelled_proposal.public_id},
                )
            )
            empty_post = self.client.post(
                cancelled_url,
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(cancelled_generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        provider.assert_not_called()
        self.assertEqual(
            empty_page.context["checkout_state"],
            "cancellation_ambiguous",
        )
        self.assertFalse(empty_page.context["reissue_allowed"])
        self.assertEqual(empty_status.json()["state"], "cancellation_ambiguous")
        self.assertEqual(empty_post.status_code, 400)
        self.assertEqual(empty_post.json()["error"], "provider_ambiguous")

    def test_forged_local_provider_terminal_evidence_is_rejected(self):
        from management.services.ig_checkout_generation import (
            apply_generation_provider_status,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-forged-terminal-source",
        )
        attempt = generation.payment_attempt
        apply_generation_provider_status(
            attempt.pk,
            "cancelled",
            payload={
                "status": "cancelled",
                "invoiceId": attempt.monobank_invoice_id,
                "merchantPaymInfo": {"reference": attempt.reference},
                "ccy": 980,
            },
            source="local_forged",
        )
        generation.refresh_from_db()
        attempt.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(generation.state, generation.State.PROVIDER_AMBIGUOUS)
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        self.assertNotIn("provider_terminal", attempt.event_state or {})
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            page = self.client.get(url)
            status = self.client.get(
                reverse(
                    "ig_checkout_status",
                    kwargs={"proposal_id": proposal.public_id},
                )
            )
            posted = self.client.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        provider.assert_not_called()
        self.assertEqual(page.context["checkout_state"], "cancellation_ambiguous")
        self.assertEqual(status.json()["state"], "cancellation_ambiguous")
        self.assertEqual(posted.status_code, 400)
        self.assertEqual(posted.json()["error"], "provider_ambiguous")

    def test_direct_200_policy_creates_prepay_200_attempt(self):
        message = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Чи можна 200 грн передоплати, а решту післяплатою?",
        )
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="prepayment",
            requested_payment_amount=Decimal("200.00"),
            item_specs=[self._item()],
            evidence={"message_ids": [message.pk]},
        )
        self.assertEqual(
            proposal.payment_policy,
            proposal.PaymentPolicy.FULL_OR_200_COD,
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-prepay-200",
            payment_choice="prepay_200_cod",
        )
        self.assertEqual(
            generation.payment_attempt.pay_type,
            PaymentAttempt.PayType.PREPAY_200,
        )
        self.assertEqual(generation.payment_attempt.payment_amount, Decimal("200.00"))
        self.assertEqual(
            generation.payment_attempt.invoice_payload["request"]["validity"],
            1500,
        )

    def test_verified_winner_materializes_one_idempotent_order(self):
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(proposal, invoice_id="v2-winner")
        attempt = generation.payment_attempt
        order, created = _apply_payment_attempt_status(
            attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )

        self.assertTrue(created)
        self.assertIsNotNone(order)
        generation.refresh_from_db()
        attempt.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(generation.state, generation.State.PAID_WINNER)
        self.assertEqual(generation.winner_slot, 1)
        self.assertTrue(attempt.checkout_winner_claimed)
        self.assertEqual(attempt.order_id, order.pk)
        self.assertEqual(proposal.winner_invoice_generation_id, generation.pk)
        self.assertEqual(proposal.status, proposal.Status.PAID)
        self.assertTrue(order.checkout_idempotency_key)

        replay, replay_created = _apply_payment_attempt_status(
            attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )
        self.assertEqual(replay.pk, order.pk)
        self.assertFalse(replay_created)
        self.assertEqual(Order.objects.count(), 1)


    def test_late_paid_loser_never_creates_second_order_or_downgrades_winner(self):
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        first = self._create_invoice(proposal, invoice_id="v2-old")
        self._apply_exact_provider_terminal(first, status="failure")
        proposal.refresh_from_db()
        second = self._create_invoice(proposal, invoice_id="v2-new")
        winner_order, _created = _apply_payment_attempt_status(
            second.payment_attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )
        loser_order, loser_created = _apply_payment_attempt_status(
            first.payment_attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )

        self.assertIsNone(loser_order)
        self.assertFalse(loser_created)
        first.refresh_from_db()
        second.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(first.state, first.State.LATE_PAID_REVIEW)
        self.assertEqual(second.state, second.State.PAID_WINNER)
        self.assertEqual(proposal.winner_invoice_generation_id, second.pk)
        self.assertEqual(proposal.status, proposal.Status.PAID)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(winner_order.pk, Order.objects.get().pk)

    def test_old_negative_callback_cannot_downgrade_paid_winner(self):
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        first = self._create_invoice(proposal, invoice_id="v2-negative-old")
        self._apply_exact_provider_terminal(first, status="failure")
        proposal.refresh_from_db()
        second = self._create_invoice(proposal, invoice_id="v2-negative-winner")
        _apply_payment_attempt_status(
            second.payment_attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )
        _apply_payment_attempt_status(
            first.payment_attempt,
            "expired",
            payload={},
            source="provider_pull",
        )

        proposal.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(proposal.status, proposal.Status.PAID)
        self.assertEqual(proposal.winner_invoice_generation_id, second.pk)
        self.assertEqual(second.state, second.State.PAID_WINNER)

    def test_late_old_success_with_released_stock_enters_review_not_order(self):
        from product_catalog.models import ProductInventoryPolicy
        from productcolors.models import Color, ProductColorVariant
        from storefront.views.monobank import _apply_payment_attempt_status

        color = Color.objects.create(name="Generation black", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            stock=2,
        )
        ProductInventoryPolicy.objects.create(
            product=self.product,
            source=ProductInventoryPolicy.Source.CATALOG_VARIANT,
        )
        item = {**self._item(), "color_variant_id": variant.pk}
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[item],
        )
        first = self._create_invoice(proposal, invoice_id="v2-stock-old")
        first_reservation = first.inventory_reservations.get()
        self._apply_exact_provider_terminal(first, status="failure")
        first_reservation.refresh_from_db()
        self.assertEqual(first_reservation.state, first_reservation.State.RELEASED)
        proposal.refresh_from_db()
        second = self._create_invoice(proposal, invoice_id="v2-stock-new")

        missing_order, missing_created = _apply_payment_attempt_status(
            first.payment_attempt,
            "success",
            payload={},
            source="provider_pull",
        )
        self.assertIsNone(missing_order)
        self.assertFalse(missing_created)
        first.refresh_from_db()
        self.assertNotEqual(first.state, first.State.RESOURCE_REVIEW)
        self.assertFalse(IgPaymentEvent.objects.filter(deal=proposal.deal).exists())

        order, created = _apply_payment_attempt_status(
            first.payment_attempt,
            "success",
            payload={
                "paidAmount": int(
                    Decimal(first.payment_attempt.payment_amount) * 100
                )
            },
            source="provider_pull",
        )

        self.assertIsNone(order)
        self.assertFalse(created)
        first.refresh_from_db()
        second.refresh_from_db()
        proposal.refresh_from_db()
        variant.refresh_from_db()
        self.assertEqual(first.state, first.State.RESOURCE_REVIEW)
        self.assertIsNone(first.winner_slot)
        self.assertEqual(second.state, second.State.CANCELLED)
        self.assertIsNone(second.active_slot)
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(variant.stock, 2)

    def test_local_generation_expiry_does_not_expire_12_hour_proposal(self):
        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(proposal, invoice_id="v2-local-expiry")
        expired_at = timezone.now() - timedelta(seconds=1)
        IgCheckoutInvoiceGeneration.objects.filter(pk=generation.pk).update(
            expires_at=expired_at
        )
        PaymentAttempt.objects.filter(pk=generation.payment_attempt_id).update(
            invoice_expires_at=expired_at
        )

        outcome = terminalize_payment_attempt(
            generation.payment_attempt_id,
            terminal_status=PaymentAttempt.Status.EXPIRED,
            reason="invoice_expired",
            source="system_expiry",
            require_due=True,
        )

        self.assertEqual(outcome.outcome, "terminalized")
        generation.refresh_from_db()
        proposal.refresh_from_db()
        self.deal = proposal.deal
        self.deal.refresh_from_db()
        self.assertEqual(generation.state, generation.State.EXPIRED)
        self.assertIsNone(generation.active_slot)
        self.assertIn(proposal.status, {proposal.Status.READY, proposal.Status.VIEWED})
        self.assertGreater(proposal.expires_at, timezone.now() + timedelta(hours=11))
        self.assertEqual(self.deal.active_checkout_proposal_id, proposal.pk)
        self.deal.np_warehouse_kind = "postomat"
        self.deal.save(update_fields=["np_warehouse_kind", "updated_at"])
        page = self.client.get(
            reverse(
                "ig_checkout_proposal",
                kwargs={"proposal_id": proposal.public_id},
            )
        )
        self.assertEqual(page.context["checkout_state"], "generation_expired_reissuable")
        self.assertFalse(page.context["payable"])
        self.assertTrue(page.context["reissue_allowed"])
        self.assertEqual(page.context["form_values"], {})
        self.assertNotContains(page, "Іван Петренко")
        self.assertNotContains(page, "+380501112233")
        self.assertNotContains(page, "buyer@example.com")
        self.assertNotContains(page, "Відділення №12")
        self.assertContains(page, "Рахунок завершився — можна створити новий")
        status = self.client.get(
            reverse(
                "ig_checkout_status",
                kwargs={"proposal_id": proposal.public_id},
            )
        )
        self.assertEqual(status.json()["state"], "reissue")

        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": "v2-reissued",
                "pageUrl": "https://pay.example/v2-reissued",
            },
        ) as provider:
            first = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
            second = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(provider.call_count, 1)
        rows = list(
            IgCheckoutInvoiceGeneration.objects.filter(proposal=proposal)
            .order_by("generation")
        )
        self.assertEqual([row.state for row in rows], [
            rows[0].State.EXPIRED,
            rows[1].State.INVOICE_CREATED,
        ])
        self.assertEqual(
            IgCheckoutInvoiceGeneration.objects.filter(
                proposal=proposal,
                active_slot=1,
            ).count(),
            1,
        )

    def test_elapsed_generation_before_cron_reissues_without_provider_on_get(self):
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(proposal, invoice_id="v2-precron-expiry")
        expired_at = timezone.now() - timedelta(seconds=1)
        IgCheckoutInvoiceGeneration.objects.filter(pk=generation.pk).update(
            expires_at=expired_at
        )
        PaymentAttempt.objects.filter(pk=generation.payment_attempt_id).update(
            invoice_expires_at=expired_at
        )
        with patch("storefront.views.monobank._monobank_api_request") as provider:
            page = self.client.get(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                )
            )
            status = self.client.get(
                reverse(
                    "ig_checkout_status",
                    kwargs={"proposal_id": proposal.public_id},
                )
            )
        provider.assert_not_called()
        self.assertEqual(page.context["checkout_state"], "generation_expired_reissuable")
        self.assertEqual(status.json()["state"], "reissue")

        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": "v2-precron-reissue",
                "pageUrl": "https://pay.example/v2-precron-reissue",
            },
        ) as provider:
            response = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.call_count, 1)
        generation.refresh_from_db()
        self.assertEqual(generation.state, generation.State.EXPIRED)
        self.assertIsNone(generation.active_slot)

    def test_forwarded_grant_reissue_never_exposes_or_replaces_locked_recipient(self):
        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-private-reissue",
        )
        original = generation.payment_attempt
        original_values = {
            "full_name": original.full_name,
            "phone": original.phone,
            "email": original.email,
            "city": original.city,
            "np_office": original.np_office,
            "np_settlement_ref": original.np_settlement_ref,
            "np_city_ref": original.np_city_ref,
            "np_warehouse_ref": original.np_warehouse_ref,
        }
        original_warehouse_kind = original.event_state[
            "recipient_lock_warehouse_kind"
        ]
        share = self.client.post(
            reverse(
                "ig_checkout_share_token",
                kwargs={"proposal_id": proposal.public_id},
            )
        )
        self.assertEqual(share.status_code, 200)
        share_path = urlparse(share.json()["url"]).path.rstrip("/")
        share_token = share_path.rsplit("/", 1)[-1]
        forwarded = Client()
        entry = forwarded.get(
            reverse("ig_checkout_token_entry", kwargs={"token": share_token})
        )
        self.assertEqual(entry.status_code, 302)

        expired_at = timezone.now() - timedelta(seconds=1)
        IgCheckoutInvoiceGeneration.objects.filter(pk=generation.pk).update(
            expires_at=expired_at
        )
        PaymentAttempt.objects.filter(pk=original.pk).update(
            invoice_expires_at=expired_at
        )
        outcome = terminalize_payment_attempt(
            original.pk,
            terminal_status=PaymentAttempt.Status.EXPIRED,
            reason="invoice_expired",
            source="system_expiry",
            require_due=True,
        )
        self.assertEqual(outcome.outcome, "terminalized")

        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as passive_provider:
            page = forwarded.get(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                )
            )
        passive_provider.assert_not_called()
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["reissue_allowed"])
        self.assertFalse(page.context["payable"])
        self.assertNotContains(page, original_values["full_name"])
        self.assertNotContains(page, original_values["phone"])
        self.assertNotContains(page, original_values["email"])
        self.assertNotContains(page, original_values["city"])
        self.assertNotContains(page, original_values["np_office"])
        self.assertNotContains(page, original_values["np_city_ref"])
        self.assertNotContains(page, 'name="full_name"')
        self.assertNotContains(page, 'name="phone"')
        self.assertNotContains(page, 'name="np_city_token"')

        attacker_payload = {
            **displayed_revision_payload(page),
            "full_name": "Чужий Отримувач",
            "phone": "+380991234567",
            "email": "attacker@example.com",
            "city": "Львів",
            "np_office": "Чуже відділення",
            "payment_choice": "prepay_200_cod",
            "promo_code": "FORGED",
        }
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            blocked = forwarded.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                attacker_payload,
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.json()["error"], "recipient_locked")
        provider.assert_not_called()
        attacker_payload["reissue_generation"] = str(generation.generation)
        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value={
                "invoiceId": "v2-private-reissue-2",
                "pageUrl": "https://pay.example/v2-private-reissue-2",
            },
        ) as provider:
            response = forwarded.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                attacker_payload,
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        provider.assert_called_once()
        replacement = (
            PaymentAttempt.objects.filter(
                checkout_series_key=generation.series_key,
                checkout_generation=2,
            )
            .select_related("instagram_checkout_generation")
            .get()
        )
        for field, expected in original_values.items():
            self.assertEqual(getattr(replacement, field), expected, field)
        self.assertEqual(
            replacement.event_state["recipient_lock_source_attempt_id"],
            original.pk,
        )
        self.assertEqual(
            replacement.event_state["recipient_lock_source_generation"],
            generation.generation,
        )
        self.assertEqual(
            replacement.instagram_checkout_generation.payment_choice,
            generation.payment_choice,
        )
        proposal.deal.refresh_from_db()
        self.assertEqual(proposal.deal.np_full_name, original_values["full_name"])
        self.assertEqual(proposal.deal.np_phone, original_values["phone"])
        self.assertEqual(proposal.deal.np_city, original_values["city"])
        self.assertEqual(proposal.deal.np_office, original_values["np_office"])
        self.assertEqual(
            proposal.deal.np_settlement_ref,
            original_values["np_settlement_ref"],
        )
        self.assertEqual(
            proposal.deal.np_city_ref,
            original_values["np_city_ref"],
        )
        self.assertEqual(
            proposal.deal.np_warehouse_ref,
            original_values["np_warehouse_ref"],
        )
        self.assertEqual(
            proposal.deal.np_warehouse_kind,
            original_warehouse_kind,
        )

    def test_elapsed_uncertain_generation_state_matrix_never_offers_reissue(self):
        uncertain_states = (
            (IgCheckoutInvoiceGeneration.State.PLANNED, False),
            (IgCheckoutInvoiceGeneration.State.PROVIDER_INFLIGHT, False),
            (IgCheckoutInvoiceGeneration.State.PROVIDER_AMBIGUOUS, False),
            (IgCheckoutInvoiceGeneration.State.INVOICE_CREATED, True),
            (IgCheckoutInvoiceGeneration.State.EXPIRED, True),
        )
        for index, (state, clear_provider_identity) in enumerate(
            uncertain_states,
            start=1,
        ):
            with self.subTest(
                state=state,
                clear_provider_identity=clear_provider_identity,
            ):
                proposal = create_or_update_proposal(
                    client=IgClient.get_or_create_for_sender(
                        f"checkout-generation-state-{index}"
                    ),
                    pay_type="online_full",
                    item_specs=[self._item()],
                )
                generation = self._create_invoice(
                    proposal,
                    invoice_id=f"v2-uncertain-{index}",
                )
                expired_at = timezone.now() - timedelta(seconds=1)
                generation_update = {
                    "state": state,
                    "expires_at": expired_at,
                }
                if clear_provider_identity:
                    generation_update["provider_invoice_id"] = None
                IgCheckoutInvoiceGeneration.objects.filter(
                    pk=generation.pk
                ).update(**generation_update)
                PaymentAttempt.objects.filter(
                    pk=generation.payment_attempt_id
                ).update(invoice_expires_at=expired_at)
                url = reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                )
                status_url = reverse(
                    "ig_checkout_status",
                    kwargs={"proposal_id": proposal.public_id},
                )
                with patch(
                    "storefront.views.monobank._monobank_api_request"
                ) as provider:
                    page = self.client.get(url)
                    status = self.client.get(status_url)
                    posted = self.client.post(
                        url,
                        {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                        HTTP_ACCEPT="application/json",
                    )
                provider.assert_not_called()
                self.assertEqual(
                    page.context["checkout_state"],
                    "cancellation_ambiguous",
                )
                self.assertFalse(page.context["reissue_allowed"])
                self.assertEqual(status.json()["state"], "cancellation_ambiguous")
                self.assertEqual(posted.status_code, 400)
                self.assertEqual(posted.json()["error"], "provider_ambiguous")
                self.assertEqual(
                    IgCheckoutInvoiceGeneration.objects.filter(
                        proposal=proposal
                    ).count(),
                    1,
                )

    def test_cron_terminalization_preserves_no_identity_inflight_ambiguity(self):
        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(
            proposal,
            invoice_id="v2-inflight-lost-identity",
        )
        attempt = generation.payment_attempt
        expired_at = timezone.now() - timedelta(seconds=1)
        IgCheckoutInvoiceGeneration.objects.filter(pk=generation.pk).update(
            state=IgCheckoutInvoiceGeneration.State.PROVIDER_INFLIGHT,
            provider_invoice_id=None,
            expires_at=expired_at,
        )
        PaymentAttempt.objects.filter(pk=attempt.pk).update(
            monobank_invoice_id="",
            invoice_url="",
            invoice_expires_at=expired_at,
        )
        outcome = terminalize_payment_attempt(
            attempt.pk,
            terminal_status=PaymentAttempt.Status.EXPIRED,
            reason="invoice_expired",
            source="system_expiry",
            require_due=True,
        )
        self.assertEqual(outcome.outcome, "provider_ambiguous")
        generation.refresh_from_db()
        attempt.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(
            generation.state,
            generation.State.PROVIDER_AMBIGUOUS,
        )
        self.assertNotEqual(generation.state, generation.State.EXPIRED)
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        self.assertTrue(
            (attempt.event_state or {}).get("invoice_creation_ambiguous")
        )
        self.assertEqual(attempt.status, PaymentAttempt.Status.PROCESSING)
        self.assertIsNotNone(generation.ambiguity_review_due_at)
        self.assertEqual(
            IgBotNotification.objects.filter(
                dedupe_key=(
                    "ig-checkout-generation-ambiguity:"
                    f"{generation.pk}:terminalization_provider_identity_unknown"
                )
            ).count(),
            1,
        )
        repeated = terminalize_payment_attempt(
            attempt.pk,
            terminal_status=PaymentAttempt.Status.EXPIRED,
            reason="invoice_expired",
            source="system_expiry",
            require_due=True,
        )
        self.assertEqual(repeated.outcome, "provider_ambiguous")
        self.assertEqual(
            IgBotNotification.objects.filter(
                dedupe_key=(
                    "ig-checkout-generation-ambiguity:"
                    f"{generation.pk}:terminalization_provider_identity_unknown"
                )
            ).count(),
            1,
        )
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        status_url = reverse(
            "ig_checkout_status",
            kwargs={"proposal_id": proposal.public_id},
        )
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            page = self.client.get(url)
            status = self.client.get(status_url)
            posted = self.client.post(
                url,
                {**getattr(self, "_hosted_revision_payload", {}), "reissue_generation": str(generation.generation)},
                HTTP_ACCEPT="application/json",
            )
        provider.assert_not_called()
        self.assertEqual(page.context["checkout_state"], "cancellation_ambiguous")
        self.assertFalse(page.context["reissue_allowed"])
        self.assertEqual(status.json()["state"], "cancellation_ambiguous")
        self.assertEqual(posted.status_code, 400)
        self.assertEqual(posted.json()["error"], "provider_ambiguous")
        self.assertEqual(
            IgCheckoutInvoiceGeneration.objects.filter(proposal=proposal).count(),
            1,
        )

    def test_open_pending_page_reload_contract_includes_generation_reissue(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "instagram-checkout.js"
        ).read_text(encoding="utf-8")
        self.assertIn("generationExpiresAt", source)
        self.assertIn('"reissue"', source)
        self.assertIn("window.location.reload()", source)

    def test_provider_create_ambiguity_is_bounded_review_and_never_blind_retried(self):
        from management.services.ig_checkout_generation import (
            resolve_due_generation_ambiguities,
        )

        class AmbiguousTimeout(RuntimeError):
            ambiguous = True

        from product_catalog.models import ProductInventoryPolicy
        from productcolors.models import Color, ProductColorVariant

        user = get_user_model().objects.create_user(username="s2b-ambiguity-owner")
        self.client.force_login(user)
        promo = PromoCode.objects.create(
            code="S2BAMB",
            discount_type="percentage",
            discount_value=Decimal("5.00"),
            max_uses=10,
            one_time_per_user=True,
        )
        color = Color.objects.create(name="Ambiguity black", primary_hex="#222222")
        variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            stock=2,
        )
        ProductInventoryPolicy.objects.create(
            product=self.product,
            source=ProductInventoryPolicy.Source.CATALOG_VARIANT,
        )
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[{**self._item(), "color_variant_id": variant.pk}],
            allow_promo=True,
        )
        self._open(proposal)
        url = reverse(
            "ig_checkout_proposal",
            kwargs={"proposal_id": proposal.public_id},
        )
        with patch(
            "storefront.views.monobank._monobank_api_request",
            side_effect=AmbiguousTimeout("timeout after dispatch"),
        ) as provider:
            first = self.client.post(
                url,
                self._delivery_payload(promo_code=promo.code),
                HTTP_ACCEPT="application/json",
            )
            second = self.client.post(
                url,
                self._delivery_payload(promo_code=promo.code),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(first.status_code, 400)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(provider.call_count, 1)
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal)
        attempt = generation.payment_attempt
        reservation = generation.inventory_reservations.get()
        self.assertEqual(generation.state, generation.State.PROVIDER_AMBIGUOUS)
        self.assertEqual(generation.active_slot, 1)
        self.assertTrue(generation.provider_request_digest)
        self.assertGreater(generation.ambiguity_review_due_at, generation.expires_at)
        event = generation.events.get(kind="provider_ambiguous")
        self.assertEqual(event.payload["attempt_reference"], attempt.reference)
        self.assertEqual(event.payload["request_digest"], generation.provider_request_digest)
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key__startswith="ig-checkout-generation-ambiguity:"
            ).exists()
        )

        due = generation.ambiguity_review_due_at + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=due):
            outcome = resolve_due_generation_ambiguities(now=due)
        self.assertEqual(outcome["resolved"], 1)
        generation.refresh_from_db()
        proposal.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(generation.state, generation.State.AMBIGUITY_REVIEW)
        self.assertIsNone(generation.active_slot)
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        self.assertEqual(attempt.status, PaymentAttempt.Status.FAILED)
        reservation.refresh_from_db()
        promo.refresh_from_db()
        self.assertEqual(reservation.state, reservation.State.ACTIVE)
        self.assertEqual(outcome["released_inventory"], 0)
        review_event = generation.events.get(kind="provider_ambiguous",
            event_key=f"checkout-generation:{generation.pk}:provider_ambiguous:bounded-review:{attempt.pk}")
        self.assertFalse(review_event.payload["resources_released"])
        self.assertTrue(review_event.payload["resources_held_for_review"])
        self.assertEqual(review_event.payload["released_inventory"], 0)
        from management.services.ig_inventory import (
            _catalog_variant_protected_quantity, release_expired_proposal_inventory,
        )
        self.assertLess(reservation.expires_at, due)
        self.assertEqual(_catalog_variant_protected_quantity(color_variant_id=variant.pk, at=due), 1)
        with patch("django.utils.timezone.now", return_value=due):
            self.assertEqual(release_expired_proposal_inventory(), 0)
            other = IgClient.objects.create(igsid="ambiguity-review-protected-second-buyer")
            from management.services.ig_checkout import CheckoutConfigurationError
            with override_settings(IG_ASSISTED_CHECKOUT_V2="off", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0), \
                    self.assertRaises(CheckoutConfigurationError) as denied:
                create_or_update_proposal(client=other, pay_type="online_full",
                    item_specs=[{**self._item(), "color_variant_id": variant.pk, "qty": 2}])
            self.assertEqual(denied.exception.code, "insufficient_stock")
        reservation.refresh_from_db()
        self.assertEqual(reservation.state, reservation.State.ACTIVE)
        self.assertEqual(promo.current_uses, 0)

        with patch("django.utils.timezone.now", return_value=due), \
                patch("storefront.views.monobank._monobank_api_request") as retry:
            blocked = self.client.post(
                url,
                self._delivery_payload(promo_code=promo.code),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(blocked.status_code, 400)
        retry.assert_not_called()

    def test_authenticated_v2_promo_transient_reconciles_once_without_webhook(self):
        from django.db.models.signals import pre_save
        from management.services.ig_checkout_reconciliation import (
            reconcile_ig_checkout,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal, generation, attempt, promo, variant = (
            self._create_authenticated_promo_invoice(code="AUTHPROMORETRY")
        )
        paid_minor = int(Decimal(attempt.payment_amount) * 100)

        def reject_usage(sender, instance, **kwargs):
            raise RuntimeError("synthetic authenticated promo persistence failure")

        dispatch_uid = "test-auth-v2-promo-first-persistence-failure"
        pre_save.connect(
            reject_usage,
            sender=PromoCodeUsage,
            weak=False,
            dispatch_uid=dispatch_uid,
        )
        try:
            order, created = _apply_payment_attempt_status(
                attempt,
                "success",
                payload={"status": "success", "paidAmount": paid_minor},
                source="webhook",
            )
        finally:
            pre_save.disconnect(
                sender=PromoCodeUsage,
                dispatch_uid=dispatch_uid,
            )
        self.assertIsNone(order)
        self.assertFalse(created)
        attempt.refresh_from_db()
        generation.refresh_from_db()
        variant.refresh_from_db()
        self.assertTrue(
            (attempt.event_state or {}).get("promo_consumption_pending")
        )
        self.assertEqual(
            (attempt.event_state or {}).get("promo_consumption_retry_count"),
            1,
        )
        self.assertEqual(generation.state, generation.State.WINNER_CLAIMED)
        self.assertEqual(variant.stock, 1)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(PromoCodeUsage.objects.filter(promo_code=promo).count(), 0)

        provider_payload = {
            "status": "success",
            "invoiceId": attempt.monobank_invoice_id,
            "merchantPaymInfo": {"reference": attempt.reference},
            "ccy": 980,
            "paidAmount": paid_minor,
        }
        with patch(
            "storefront.views.monobank._monobank_api_request",
            return_value=provider_payload,
        ) as provider:
            reconcile_ig_checkout(limit=20, pull_ambiguous=True)
        provider.assert_called_once()
        attempt.refresh_from_db()
        generation.refresh_from_db()
        proposal.refresh_from_db()
        variant.refresh_from_db()
        self.assertIsNotNone(attempt.order_id)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            PromoCodeUsage.objects.filter(
                promo_code=promo,
                order_id=attempt.order_id,
            ).count(),
            1,
        )
        self.assertFalse(
            (attempt.event_state or {}).get("promo_consumption_pending")
        )
        self.assertEqual(generation.state, generation.State.PAID_WINNER)
        self.assertEqual(proposal.status, proposal.Status.PAID)
        self.assertEqual(variant.stock, 1)
        self.assertEqual(IgPaymentEvent.objects.filter(deal=proposal.deal).count(), 1)
        self.assertEqual(
            IgPaymentProjection.objects.filter(deal=proposal.deal).count(),
            1,
        )
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            reconcile_ig_checkout(limit=20, pull_ambiguous=True)
        provider.assert_not_called()
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(PromoCodeUsage.objects.filter(promo_code=promo).count(), 1)

    def test_authenticated_v2_promo_repeated_failure_enters_manual_review(self):
        from django.db.models.signals import pre_save
        from management.services.ig_checkout_reconciliation import (
            reconcile_ig_checkout,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal, generation, attempt, promo, variant = (
            self._create_authenticated_promo_invoice(code="AUTHPROMOREVIEW")
        )
        paid_minor = int(Decimal(attempt.payment_amount) * 100)

        def reject_usage(sender, instance, **kwargs):
            raise RuntimeError("persistent authenticated promo write failure")

        dispatch_uid = "test-auth-v2-promo-repeated-persistence-failure"
        pre_save.connect(
            reject_usage,
            sender=PromoCodeUsage,
            weak=False,
            dispatch_uid=dispatch_uid,
        )
        try:
            _apply_payment_attempt_status(
                attempt,
                "success",
                payload={"status": "success", "paidAmount": paid_minor},
                source="webhook",
            )
            provider_payload = {
                "status": "success",
                "invoiceId": attempt.monobank_invoice_id,
                "merchantPaymInfo": {"reference": attempt.reference},
                "ccy": 980,
                "paidAmount": paid_minor,
            }
            with patch(
                "storefront.views.monobank._monobank_api_request",
                return_value=provider_payload,
            ) as provider:
                reconcile_ig_checkout(limit=20, pull_ambiguous=True)
            provider.assert_called_once()
        finally:
            pre_save.disconnect(
                sender=PromoCodeUsage,
                dispatch_uid=dispatch_uid,
            )
        attempt.refresh_from_db()
        generation.refresh_from_db()
        proposal.refresh_from_db()
        variant.refresh_from_db()
        self.assertFalse(
            (attempt.event_state or {}).get("promo_consumption_pending")
        )
        self.assertTrue(
            (attempt.event_state or {}).get("promo_consumption_manual_review")
        )
        self.assertEqual(
            (attempt.event_state or {}).get("promo_consumption_retry_count"),
            2,
        )
        self.assertEqual(generation.state, generation.State.RESOURCE_REVIEW)
        self.assertEqual(
            generation.review_reason,
            "promo_consumption_persistence_failed",
        )
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        self.assertEqual(attempt.status, PaymentAttempt.Status.PAID)
        self.assertEqual(variant.stock, 1)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(PromoCodeUsage.objects.filter(promo_code=promo).count(), 0)
        self.assertEqual(IgPaymentEvent.objects.filter(deal=proposal.deal).count(), 1)
        self.assertEqual(
            IgPaymentProjection.objects.filter(deal=proposal.deal).count(),
            1,
        )
        self.assertTrue(
            IgBotNotification.objects.filter(
                dedupe_key=(
                    "ig-checkout-generation-review:"
                    f"{generation.pk}:promo_consumption_persistence_failed"
                )
            ).exists()
        )
        with patch(
            "storefront.views.monobank._monobank_api_request"
        ) as provider:
            reconcile_ig_checkout(limit=20, pull_ambiguous=True)
        provider.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_late_pending_status_cannot_downgrade_winner(self):
        from management.services.ig_checkout_generation import (
            apply_generation_provider_status,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        generation = self._create_invoice(proposal, invoice_id="v2-pending-winner")
        order, _created = _apply_payment_attempt_status(
            generation.payment_attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )
        apply_generation_provider_status(
            generation.payment_attempt_id,
            "processing",
            payload={},
            source="provider_pull",
        )
        generation.payment_attempt.refresh_from_db()
        generation.refresh_from_db()
        self.assertEqual(generation.payment_attempt.status, PaymentAttempt.Status.CONVERTED)
        self.assertEqual(generation.payment_attempt.order_id, order.pk)
        self.assertEqual(generation.state, generation.State.PAID_WINNER)

    def test_late_provider_create_success_is_reviewed_without_repointing(self):
        from management.services.ig_checkout_generation import (
            terminalize_generation_attempt,
        )

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)
        raced = {}

        def late_success(*_args, **_kwargs):
            generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal)
            terminal_outcome = terminalize_generation_attempt(
                generation.payment_attempt_id,
                terminal_status=PaymentAttempt.Status.FAILED,
                reason="concurrent_terminalization",
            )
            raced["terminal_outcome"] = terminal_outcome["outcome"]
            return {
                "invoiceId": "v2-late-create",
                "pageUrl": "https://pay.example/v2-late-create",
            }

        with patch(
            "storefront.views.monobank._monobank_api_request",
            side_effect=late_success,
        ):
            response = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                self._delivery_payload(),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 400)
        generation = IgCheckoutInvoiceGeneration.objects.get(
            proposal=proposal,
            generation=1,
        )
        proposal.refresh_from_db()
        self.assertEqual(generation.state, generation.State.LATE_PROVIDER_REVIEW)
        self.assertIsNone(generation.active_slot)
        self.assertEqual(raced["terminal_outcome"], "provider_ambiguous")
        self.assertEqual(generation.provider_invoice_id, "v2-late-create")
        self.assertEqual(
            IgCheckoutInvoiceGeneration.objects.filter(proposal=proposal).count(),
            1,
        )
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        self.assertIsNone(proposal.current_invoice_generation_id)
        with patch("storefront.views.monobank._monobank_api_request") as provider:
            blocked = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                self._delivery_payload(),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(blocked.status_code, 400)
        provider.assert_not_called()
        status = self.client.get(
            reverse(
                "ig_checkout_status",
                kwargs={"proposal_id": proposal.public_id},
            )
        )
        self.assertEqual(status.json()["ui_state"], "cancellation_ambiguous")

    def test_authenticated_stale_promo_generation_enters_resource_review(self):
        from storefront.views.monobank import _apply_payment_attempt_status

        user = get_user_model().objects.create_user(username="s2b-promo-owner")
        self.client.force_login(user)
        promo = PromoCode.objects.create(
            code="S2BAUTH",
            discount_type="percentage",
            discount_value=Decimal("5.00"),
            max_uses=10,
            one_time_per_user=True,
        )
        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
            allow_promo=True,
        )
        self._open(proposal)
        with patch(
            "storefront.views.monobank._monobank_api_request",
            side_effect=[
                {"invoiceId": "promo-old", "pageUrl": "https://pay/promo-old"},
                {"invoiceId": "promo-new", "pageUrl": "https://pay/promo-new"},
            ],
        ):
            url = reverse(
                "ig_checkout_proposal",
                kwargs={"proposal_id": proposal.public_id},
            )
            first_response = self.client.post(
                url,
                self._delivery_payload(promo_code=promo.code),
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(first_response.status_code, 200)
            first = IgCheckoutInvoiceGeneration.objects.get(
                proposal=proposal,
                generation=1,
            )
            first_owner = first.promo_reservation_generation
            self._apply_exact_provider_terminal(first, status="failure")
            proposal.refresh_from_db()
            second_response = self.client.post(
                url,
                self._delivery_payload(promo_code=promo.code),
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(second_response.status_code, 200)
        second = IgCheckoutInvoiceGeneration.objects.get(
            proposal=proposal,
            generation=2,
        )
        self.assertTrue(first_owner)
        self.assertTrue(second.promo_reservation_generation)
        self.assertNotEqual(first_owner, second.promo_reservation_generation)

        order, created = _apply_payment_attempt_status(
            first.payment_attempt,
            "success",
            payload={
                "paidAmount": int(
                    Decimal(first.payment_attempt.payment_amount) * 100
                )
            },
            source="provider_pull",
        )
        self.assertIsNone(order)
        self.assertFalse(created)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, first.State.RESOURCE_REVIEW)
        self.assertEqual(second.state, second.State.CANCELLED)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PromoCodeUsage.objects.filter(promo_code=promo).exists())
        payment_event = IgPaymentEvent.objects.get(deal=proposal.deal)
        from management.models import provider_evidence_signature

        self.assertEqual(
            payment_event.evidence["signature"],
            provider_evidence_signature(
                deal_id=proposal.deal_id,
                client_id=proposal.client_id,
                provider=payment_event.provider,
                source=payment_event.source,
                invoice_id=payment_event.invoice_id,
                provider_status=payment_event.provider_status,
                payload_digest=payment_event.payload_digest,
            ),
        )
        projection = IgPaymentProjection.objects.get(deal=proposal.deal)
        self.assertEqual(projection.truth, proposal.deal.PaymentTruth.CONFIRMED)
        self.assertEqual(projection.last_event_id, payment_event.pk)

    def test_ambiguity_then_manager_order_late_success_never_creates_second_order(self):
        from management.services.ig_checkout_generation import (
            resolve_due_generation_ambiguities,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        class AmbiguousTimeout(RuntimeError):
            ambiguous = True

        proposal = create_or_update_proposal(
            client=self.client_row,
            pay_type="online_full",
            item_specs=[self._item()],
        )
        self._open(proposal)
        with patch(
            "storefront.views.monobank._monobank_api_request",
            side_effect=AmbiguousTimeout("dispatch timeout"),
        ):
            response = self.client.post(
                reverse(
                    "ig_checkout_proposal",
                    kwargs={"proposal_id": proposal.public_id},
                ),
                self._delivery_payload(),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 400)
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=proposal)
        IgCheckoutInvoiceGeneration.objects.filter(pk=generation.pk).update(
            ambiguity_review_due_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(
            resolve_due_generation_ambiguities(now=timezone.now())["resolved"],
            1,
        )
        manager_order = Order.objects.create(
            full_name="Manager resolved",
            phone="+380501112233",
            city="Kyiv",
            np_office="Branch 1",
            pay_type="online_full",
            payment_status="paid",
            total_sum=Decimal("900.00"),
            source="manual",
            sale_source="Instagram",
        )
        deal = proposal.deal
        deal.order = manager_order
        deal.status = deal.Status.ORDER_CREATED
        deal.payment_status = "paid"
        deal.save(update_fields=["order", "status", "payment_status", "updated_at"])

        late_order, created = _apply_payment_attempt_status(
            generation.payment_attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )
        self.assertIsNone(late_order)
        self.assertFalse(created)
        generation.refresh_from_db()
        generation.payment_attempt.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(generation.state, generation.State.LATE_PAID_REVIEW)
        self.assertIsNone(generation.payment_attempt.order_id)
        self.assertEqual(proposal.status, proposal.Status.MANAGER_REVIEW)
        self.assertEqual(Order.objects.count(), 1)
        payment_event = IgPaymentEvent.objects.get(
            event_key=f"attempt:{generation.payment_attempt_id}:verified"
        )
        self.assertEqual(payment_event.provider_status, "success")
        projection = IgPaymentProjection.objects.get(deal=deal)
        self.assertEqual(projection.last_event_id, payment_event.pk)

        replay_order, replay_created = _apply_payment_attempt_status(
            generation.payment_attempt,
            "success",
            payload={"paidAmount": 90000},
            source="provider_pull",
        )
        self.assertIsNone(replay_order)
        self.assertFalse(replay_created)
        self.assertEqual(Order.objects.count(), 1)


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeHostedRevisionCASTests(TestCase):
    """Real displayed-page CAS survives an actual authorized basket edit."""
    _delivery_payload = CheckoutGenerationRuntimeTests._delivery_payload

    def setUp(self):
        from management import tests_ig_checkout_service as quote_fixture
        from django.test import Client
        quote_fixture.InstagramCheckoutConfigurationTests.setUp(self)
        self.customer = self.client
        self.http = Client(HTTP_HOST="twocomms.shop")
        self.proposal = create_or_update_proposal(client=self.customer, pay_type="online_full",
            item_specs=[quote_fixture.InstagramCheckoutConfigurationTests._valid_item(self)])
        raw, _token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        entry = self.http.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}))
        self.assertEqual(entry.status_code, 302)
        self.url = entry["Location"]
        self.page = self.http.get(self.url)
        self.assertEqual(self.page.status_code, 200)
        self.shown = displayed_revision_payload(self.page)
        self.assertEqual(set(self.shown), {"expected_revision", "expected_digest", "delivery_policy_digest"})

    def amend(self):
        import json
        import uuid
        state = self.page.context["cart_state"]
        response = self.http.post(self.url, json.dumps({"action": "cart_amendment",
            "operation_id": str(uuid.uuid4()), "operation": "quantity", "quantity": 2,
            "line_id": state["items"][0]["line_id"], **self.shown,
            "expected_selection_revision": state["selection_revision"]}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["cart_state"]["total"], "1900.00")
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.items.get().quantity, 2)

    def test_old_page_invoice_and_reissue_cas_reject_after_native_cart_edit_without_any_provider_effect(self):
        old = copy.deepcopy(self.shown)
        with patch("storefront.views.monobank._monobank_api_request") as bank:
            self.amend()
            for body in ({**self._delivery_payload(payment_choice="online_full"), **old},
                    {**old, "reissue_generation": "1"}):
                response = self.http.post(self.url, body, HTTP_ACCEPT="application/json")
                self.assertEqual(response.status_code, 409, response.content)
                self.assertEqual(response.json()["error"], "revision_changed")
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())
        self.assertFalse(Order.objects.exists())
        current = self.http.get(self.url)
        shown = displayed_revision_payload(current)
        self.assertNotEqual(shown, old)
        self.assertEqual(current.context["cart_state"]["total"], "1900.00")
        with patch("storefront.views.monobank._monobank_api_request", return_value={
                "invoiceId": "native-refreshed-quote", "pageUrl": "https://pay.example/refreshed-quote"}) as bank:
            response = self.http.post(self.url, {**self._delivery_payload(payment_choice="online_full"), **shown},
                HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        bank.assert_called_once()
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=self.proposal)
        self.assertEqual(generation.payment_amount, Decimal("1900.00"))
        self.assertEqual(generation.payment_attempt.cart_snapshot["cart"][0]["qty"], 2)
        self.assertFalse(Order.objects.exists())

    def test_missing_displayed_revision_cannot_create_attempt_or_cross_provider(self):
        payload = self._delivery_payload(payment_choice="online_full")
        payload.pop("expected_revision", None)
        payload.pop("expected_digest", None)
        with patch("storefront.views.monobank._monobank_api_request") as bank:
            response = self.http.post(self.url, payload, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"], "revision_changed")
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())
