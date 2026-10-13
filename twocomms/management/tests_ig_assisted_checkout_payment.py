"""Real approval -> hosted invoice -> paid winner -> canonical custom Order."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid
import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from management.ig_assisted_commerce_models import IgAssistedCommerceExecution as Execution
from management.models import IgCheckoutInvoiceGeneration, IgCheckoutInventoryReservation, IgOrderAssignment
from management.services import ig_assisted_checkout as checkout
from management.services.ig_assisted_commerce_media import upload_assisted_image
from management.tests_ig_assisted_commerce import AssistedCommerceFixture
from management.tests_ig_assisted_commerce_media import raster
from management.tests_ig_checkout_generation_runtime import CheckoutGenerationRuntimeTests
from orders.models import Order, PaymentAttempt
from productcolors.models import ProductColorVariant
from storefront.models import Product


class AssistedCheckoutPaymentFixture(AssistedCommerceFixture):
    """Reusable native producer; only external transports and the clock are replaced."""
    _delivery_payload = CheckoutGenerationRuntimeTests._delivery_payload
    _open = CheckoutGenerationRuntimeTests._open

    def setUp(self):
        super().setUp()
        private = TemporaryDirectory(prefix="twocomms-assisted-payment-")
        self.addCleanup(private.cleanup)
        private_root = Path(private.name).resolve()
        private_root.chmod(0o700)
        private_settings = override_settings(IG_PRIVATE_MEDIA_ROOT=str(private_root))
        private_settings.enable()
        self.addCleanup(private_settings.disable)

    def _custom_proposal(self, *, photo=False, mixed=False):
        if mixed:
            ProductColorVariant.objects.filter(pk=self.variant.pk).update(stock=2)
            type(self.rule).objects.filter(pk=self.rule.pk).update(stock=2)
            self.canonical("добавьте Storm, количество 1, размер L, цвет purple, крій classic")
            request, _revision = self.request_case(custom=False)
        else:
            request, _revision = self.request_case()
        image = None
        if photo:
            image, created = upload_assisted_image(request.pk, actor=self.manager, operation_id=uuid.uuid4(),
                expected_request_version=request.version, expected_request_digest=request.envelope_hmac,
                expected_full_capture_digest=request.full_capture_digest,
                upload=SimpleUploadedFile("actual-approved-photo.jpg", raster(), content_type="image/jpeg"), now=self.now)
            self.assertTrue(created)
        items = self.items_for(request)
        if mixed:
            self.assertEqual(len(items), 2)
            from management.services.ig_assisted_commerce import _editable_items
            first = _editable_items(request.envelope["snapshot"])[0]
            self.assertEqual(first["item_kind"], "catalog_apparel")
            first.update(availability="ready", checked_quantity=first["quantity"],
                supply_basis="warehouse_checked", source_location="Менеджер перевірив точну річ",
                question_answers=items[0]["question_answers"])
            items[0] = first
        if image:
            items[-1].update(image_id=str(image.pk), image_digest=image.content_digest)
        draft, _ = self.record(request, items=items)
        decision, _ = self.record(request, status="approved", latest=draft)
        self.now += timedelta(seconds=1)
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        buy = self.canonical("Хочу купити всі товари в кошику" if mixed else "Хочу купити", at=self.now)
        revision, _claim = self.revision(buy, now=self.now)
        with patch("management.services.instagram_bot._provider_http") as provider:
            prepared = checkout.prepare_assisted_checkout(self.client_row.pk, actor=self.manager,
                request_id=request.pk, decision_id=decision.pk, expected_request_digest=request.envelope_hmac,
                expected_decision_digest=decision.envelope_hmac, operation_id=uuid.uuid4(),
                revision_id=revision.pk, settings_id=self.settings.pk)
        provider.assert_not_called()
        self.assertTrue(prepared["ready"], prepared)
        return {**prepared, "request": request, "decision": decision, "image": image, "buy": buy,
            "execution": Execution.objects.get(proposal_id=prepared["proposal_id"])}

    def _invoice_custom(self, result, *, payload=None, invoice_id=None):
        proposal = result["proposal"]
        self._open(proposal)
        with patch("storefront.views.monobank._monobank_api_request", return_value={
            "invoiceId": invoice_id or f"assisted-native-{proposal.pk}",
            "pageUrl": "https://pay.example/assisted-native",
        }) as bank:
            response = self.client.post(reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id}),
                payload if payload is not None else self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="1"),
                HTTP_ACCEPT="application/json")
        return response, bank

    def _paid_custom_order(self, *, photo=False, mixed=False):
        result = self._custom_proposal(photo=photo, mixed=mixed)
        response, bank = self._invoice_custom(result)
        self.assertEqual(response.status_code, 200, response.content)
        bank.assert_called_once()
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=result["proposal"])
        attempt = generation.payment_attempt
        self.assertFalse(Order.objects.exists())
        from storefront.views.monobank import _apply_payment_attempt_status
        # TestCase consumers retain an outer transaction; execute its real
        # on-commit receipt, while TransactionTestCase commits it ordinarily.
        callbacks = (self.captureOnCommitCallbacks(execute=True)
            if hasattr(self, "captureOnCommitCallbacks") else nullcontext())
        with patch("management.services.instagram_bot._provider_http", return_value=(200,
                json.dumps({"message_id": f"native-assisted-paid-{result['proposal_id']}"}))) as instagram:
            with callbacks, transaction.atomic():
                order, created = _apply_payment_attempt_status(attempt, "success",
                    payload={"paidAmount": int(attempt.payment_amount * 100)}, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        instagram.assert_called_once()
        physical = json.loads(instagram.call_args.kwargs["data"])
        self.assertEqual(physical["recipient"], {"id": self.client_row.igsid})
        self.assertEqual(physical["message"]["text"],
            f"Дякуємо, оплату отримано. Замовлення #{order.order_number} готуємо для {order.full_name}. "
            f"Доставка: {order.city}, {order.np_office}. Телефон для зв'язку: {order.phone}. "
            "Я надішлю ТТН сюди, щойно її буде створено. "
            f"Сума: {attempt.payment_amount:.2f} грн.")
        for row in (result["proposal"], result["execution"], generation, attempt, order):
            row.refresh_from_db()
        return {**result, "generation": generation, "attempt": attempt, "order": order,
            "items": list(order.items.order_by("pk"))}


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeAssistedCheckoutPaymentTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def test_custom_paid_winner_has_actual_order_items_assignment_and_original_photo_identity(self):
        product_count = Product.objects.count()
        result = self._paid_custom_order(photo=True)
        item, = result["items"]
        order, attempt, generation, execution = (result[key] for key in ("order", "attempt", "generation", "execution"))
        self.assertEqual((item.product_id, item.color_variant_id, item.is_custom), (None, None, True))
        self.assertEqual((item.title, item.color_name_custom, item.size, item.qty, item.unit_price),
            ("Фіолетове худі", "Фіолетовий", "L", 1, Decimal("1500.00")))
        self.assertEqual((order.total_sum, order.payment_status), (Decimal("1500.00"), "paid"))
        self.assertEqual((generation.state, attempt.status), (generation.State.PAID_WINNER, attempt.Status.CONVERTED))
        self.assertEqual(execution.order_id, order.pk)
        self.assertIsNone(execution.active_slot)
        self.assertIsNotNone(execution.settled_at)
        self.assertTrue(IgOrderAssignment.objects.filter(order=order, client=self.client_row, unassigned_at__isnull=True).exists())
        metadata, = order.payment_payload["assisted_custom_items"]
        self.assertEqual((metadata["order_item_id"], metadata["photo_asset_id"], metadata["photo_digest"]),
            (item.pk, str(result["image"].pk), result["image"].content_digest))
        self.assertEqual(Product.objects.count(), product_count)
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        checked = checkout.validate_assisted_order(order, now=self.now)
        self.assertTrue(checked["ready"], checked)

    def test_mixed_paid_order_consumes_only_real_catalog_pool_once_on_duplicate_success(self):
        result = self._paid_custom_order(mixed=True)
        self.assertEqual(len(result["items"]), 2)
        catalog, custom = result["items"]
        self.assertEqual((catalog.product_id, catalog.is_custom, custom.product_id, custom.is_custom),
            (self.product.pk, False, None, True))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
        self.assertEqual(result["order"].total_sum, Decimal("2290.00"))
        reservation = IgCheckoutInventoryReservation.objects.get(invoice_generation=result["generation"])
        self.assertEqual(reservation.product_id, self.product.pk)
        self.assertEqual((reservation.state, reservation.quantity, reservation.fulfilled_quantity),
            (reservation.State.FULFILLED, 1, 1))
        proposal_hold = IgCheckoutInventoryReservation.objects.get(proposal=result["proposal"], invoice_generation__isnull=True)
        self.assertEqual((proposal_hold.product_id, proposal_hold.state, proposal_hold.release_reason),
            (self.product.pk, proposal_hold.State.RELEASED, "transferred_to_invoice_generation"))
        custom_ids = list(result["proposal"].items.filter(product_id__isnull=True).values_list("pk", flat=True))
        for hold in IgCheckoutInventoryReservation.objects.filter(proposal=result["proposal"]):
            self.assertEqual(hold.product_id, self.product.pk)
            self.assertFalse(set(hold.line_ids or []) & set(custom_ids))
        from storefront.views.monobank import _apply_payment_attempt_status
        order, created = _apply_payment_attempt_status(result["attempt"], "success",
            payload={"paidAmount": 229000}, source="provider_pull")
        self.assertFalse(created)
        self.assertEqual(order.pk, result["order"].pk)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
        self.assertEqual(list(order.items.values_list("pk", flat=True)), [item.pk for item in result["items"]])

    def test_missing_actual_checkbox_rejects_before_attempt_or_bank_effect(self):
        result = self._custom_proposal()
        response, bank = self._invoice_custom(result, payload=self._delivery_payload(payment_choice="online_full"))
        self.assertEqual(response.status_code, 400, response.content)
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())
        result["execution"].refresh_from_db()
        self.assertEqual(result["execution"].active_slot, 1)

    def test_source_changed_before_invoice_has_no_bank_effect(self):
        result = self._custom_proposal()
        result["buy"].text = "Передумав, інший розмір"
        result["buy"].save(update_fields=["text"])
        response, bank = self._invoice_custom(result)
        self.assertEqual(response.status_code, 400, response.content)
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_native_source_change_after_preparation_rejects_before_bank_as_known_failed_generation(self):
        result = self._custom_proposal()
        from management.services import ig_checkout_generation as generations
        original_guard = generations._persist_provider_dispatch_evidence
        changed_sources = []

        def change_then_validate(attempt_id, invoice_payload):
            generation = IgCheckoutInvoiceGeneration.objects.get(payment_attempt_id=attempt_id)
            self.assertEqual(generation.state, generation.State.PROVIDER_INFLIGHT)
            self.assertEqual(generation.active_slot, 1)
            self.assertEqual(generation.proposal_id, result["proposal_id"])
            # Admit a real customer action through the canonical reducer at
            # the exact preparation -> provider-dispatch boundary.
            changed_sources.append(self.canonical("змініть кількість на 2", at=self.now))
            return original_guard(attempt_id, invoice_payload)

        with patch.object(generations, "_persist_provider_dispatch_evidence", side_effect=change_then_validate) as guard:
            response, bank = self._invoice_custom(result)
        self.assertEqual(response.status_code, 400, response.content)
        guard.assert_called_once()
        bank.assert_not_called()
        self.assertEqual(len(changed_sources), 1)
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=result["proposal"])
        self.assertEqual(generation.state, generation.State.FAILED)
        self.assertIsNone(generation.active_slot)
        self.assertFalse(generation.provider_invoice_id)
        self.assertFalse(generation.provider_request_digest)
        attempt = generation.payment_attempt
        self.assertEqual(attempt.status, attempt.Status.FAILED)
        self.assertFalse((attempt.event_state or {}).get("invoice_creation_ambiguous"))
        self.assertIsNone(attempt.paid_amount)
        self.assertFalse(Order.objects.exists())
        result["execution"].refresh_from_db()
        self.assertEqual(result["execution"].active_slot, 1)
        self.assertIsNone(result["execution"].released_at)
        self.assertIsNone(result["execution"].order_id)

    def test_paid_changed_custom_quantity_holds_resource_review_without_order_or_releasing_execution(self):
        result = self._custom_proposal()
        response, bank = self._invoice_custom(result)
        self.assertEqual(response.status_code, 200, response.content)
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=result["proposal"])
        attempt = generation.payment_attempt
        snapshot = deepcopy(attempt.cart_snapshot)
        snapshot["cart"][0]["qty"] = 2
        attempt.cart_snapshot = snapshot
        attempt.save(update_fields=["cart_snapshot", "updated"])
        from storefront.views.monobank import _apply_payment_attempt_status
        order, created = _apply_payment_attempt_status(attempt, "success",
            payload={"paidAmount": 150000}, source="provider_pull")
        self.assertIsNone(order)
        self.assertFalse(created)
        generation.refresh_from_db()
        result["execution"].refresh_from_db()
        self.assertEqual(generation.state, generation.State.RESOURCE_REVIEW)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(result["execution"].active_slot, 1)
        self.assertIsNone(result["execution"].released_at)

    def test_original_paid_proof_survives_expired_approval_without_reconstructing_current_quote(self):
        result = self._custom_proposal()
        response, _bank = self._invoice_custom(result)
        self.assertEqual(response.status_code, 200, response.content)
        generation = IgCheckoutInvoiceGeneration.objects.get(proposal=result["proposal"])
        attempt = generation.payment_attempt
        from storefront.views.monobank import _apply_payment_attempt_status
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(days=2)):
            fresh = checkout.validate_assisted_proposal(result["proposal"], fresh=True)
            self.assertFalse(fresh["ready"])
            order, created = _apply_payment_attempt_status(attempt, "success",
                payload={"paidAmount": 150000}, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        item = order.items.get()
        self.assertEqual((item.title, item.qty, item.unit_price), ("Фіолетове худі", 1, Decimal("1500.00")))
        result["execution"].refresh_from_db()
        self.assertEqual(result["execution"].order_id, order.pk)

    def test_trusted_cancelled_reissue_keeps_original_ack_and_recipient_without_new_checkbox(self):
        result = self._custom_proposal()
        response, _bank = self._invoice_custom(result)
        self.assertEqual(response.status_code, 200, response.content)
        first = IgCheckoutInvoiceGeneration.objects.get(proposal=result["proposal"])
        attempt = first.payment_attempt
        from management.services.ig_checkout_generation import apply_generation_provider_status
        with transaction.atomic():
            apply_generation_provider_status(attempt.pk, "cancelled", payload={"status": "cancelled",
                "invoiceId": attempt.monobank_invoice_id, "reference": attempt.reference, "ccy": 980}, source="provider_pull")
        first.refresh_from_db()
        self.assertEqual(first.state, first.State.CANCELLED)
        # The canonical reissue payload contains no recipient or checkbox.
        response, bank = self._invoice_custom(result, invoice_id="assisted-native-reissue",
            payload={"reissue_generation": str(first.generation)})
        self.assertEqual(response.status_code, 200, response.content)
        bank.assert_called_once()
        second = IgCheckoutInvoiceGeneration.objects.get(proposal=result["proposal"], generation=2)
        self.assertEqual(second.payment_attempt.cart_snapshot["assisted_terms_ack"], attempt.cart_snapshot["assisted_terms_ack"])
        self.assertEqual((second.payment_attempt.full_name, second.payment_attempt.phone), (attempt.full_name, attempt.phone))

    def test_unknown_provider_boundary_does_not_replay_or_release_custom_approval(self):
        result = self._custom_proposal()
        self._open(result["proposal"])
        url = reverse("ig_checkout_proposal", kwargs={"proposal_id": result["proposal"].public_id})
        with patch("storefront.views.monobank._monobank_api_request", side_effect=TimeoutError("synthetic unknown boundary")) as bank:
            response = self.client.post(url, self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="1"),
                HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400, response.content)
        bank.assert_called_once()
        with patch("storefront.views.monobank._monobank_api_request") as retry:
            repeated = self.client.post(url, {"reissue_generation": "1"}, HTTP_ACCEPT="application/json")
        self.assertEqual(repeated.status_code, 400, repeated.content)
        retry.assert_not_called()
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), 1)
        result["execution"].refresh_from_db()
        self.assertEqual(result["execution"].active_slot, 1)
        self.assertIsNone(result["execution"].released_at)
        self.assertFalse(Order.objects.exists())


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="off",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeAssistedCheckoutOffModeTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def test_native_custom_off_zero_producer_hosted_invoice_paid_winner_creates_one_order(self):
        result = self._paid_custom_order(photo=True)
        self.assertTrue(result["proposal"].assisted_checkout_v2)
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count(), IgCheckoutInvoiceGeneration.objects.count()), (1, 1, 1))
        self.assertEqual(result["execution"].order_id, result["order"].pk)
        self.assertTrue(result["items"][0].is_custom)
        from orders.checkout_series import AssistedCheckoutV2Disabled, build_checkout_series_identity
        # The public ordinary identity API still respects the disabled rollout.
        with self.assertRaises(AssistedCheckoutV2Disabled):
            build_checkout_series_identity(result["proposal"].public_id, generation=2)

    def test_native_mixed_off_zero_paid_winner_keeps_catalog_stock_once_and_duplicate_one_order(self):
        result = self._paid_custom_order(mixed=True)
        self.assertEqual((Order.objects.count(), result["order"].items.count()), (1, 2))
        self.assertEqual(result["order"].total_sum, Decimal("2290.00"))
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
        from storefront.views.monobank import _apply_payment_attempt_status
        order, created = _apply_payment_attempt_status(result["attempt"], "success",
            payload={"paidAmount": 229000}, source="provider_pull")
        self.assertFalse(created)
        self.assertEqual(order.pk, result["order"].pk)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 1)
        self.assertEqual((Order.objects.count(), PaymentAttempt.objects.count(), Execution.objects.count()), (1, 1, 1))

    def test_ordinary_v2_model_flag_and_forged_payload_cannot_bypass_off_or_shadow(self):
        ProductColorVariant.objects.filter(pk=self.variant.pk).update(stock=2)
        type(self.rule).objects.filter(pk=self.rule.pk).update(stock=2)
        from management.services.ig_checkout import create_or_update_proposal
        with override_settings(IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100):
            proposal = create_or_update_proposal(client=self.client_row, pay_type="online_full", allow_promo=False,
                item_specs=[{"product_id": self.product.pk, "color_variant_id": self.variant.pk,
                    "qty": 1, "size": "M", "fit_option_code": "classic", "option_values": {"fit": "classic"}}])
        self.assertTrue(proposal.assisted_checkout_v2)
        self.assertFalse(Execution.objects.filter(proposal=proposal).exists())
        forged = self._delivery_payload(payment_choice="online_full", assisted_terms_accepted="1",
            assisted_checkout_v2="1", assisted_execution='{"ready":true,"execution_id":"forged"}')
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode), override_settings(IG_ASSISTED_CHECKOUT_V2=mode):
                response, bank = self._invoice_custom({"proposal": proposal}, payload=forged)
                self.assertEqual(response.status_code, 400, response.content)
                bank.assert_not_called()
                self.assertFalse(PaymentAttempt.objects.exists())
                self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())
                self.assertFalse(Order.objects.exists())
