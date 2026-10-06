from decimal import Decimal
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, RequestFactory, override_settings

from orders.nova_poshta_documents import (
    NovaPoshtaDocumentError,
    NovaPoshtaDocumentService,
    NovaPoshtaInvalidDescriptionError,
    NovaPoshtaResolvedPoint,
    build_order_payment_snapshot,
    build_shipment_contract_token,
    build_waybill_description,
    validate_shipment_contract_token,
)
from orders.forms import TelegramNovaPoshtaWaybillForm
from orders.services.delivery_payment import build_delivery_payment_contract
from storefront.views.order_actions import _fallback_waybill_initial, _waybill_action_block_reason, telegram_order_np_waybill_action


class DtfWaybillContractTests(SimpleTestCase):
    """Pure shipment-contract tests; every provider operation is mocked."""

    def order(self, *, film=True, **overrides):
        values = {
            "items": [SimpleNamespace(item_kind="dtf_film", film_length_m=Decimal("2.50"), qty=1)] if film else [SimpleNamespace(title="Худі", qty=1)],
            "custom_print_leads": [], "delivery_method": "nova_poshta",
            "total_sum": Decimal("1000"), "discount_amount": Decimal("100"),
            "payment_payload": {}, "payment_status": "unpaid", "pay_type": "cod",
            "full_name": "Тестовий Клієнт", "phone": "+380991112233", "city": "Київ",
            "np_office": "Відділення №4", "status": "new", "tracking_number": "", "nova_poshta_document_ref": "",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def manual_payload(self, *, cod=True, paid="150.50", payer="Sender", method="NonCash"):
        return {"manual_shipment_payment": {"payer_type": payer, "payment_method": method, "cod_enabled": cod, "paid_amount": paid}}

    def point(self, kind="branch"):
        return NovaPoshtaResolvedPoint("Київ", "Відділення №4", "s", "c", "w", kind)

    def tube(self):
        return {"Ref": "mock-tube60-ref", "Description": "Тубус 60 прямокутний з наповнювачем", "Length": "600.0", "Width": "160.0", "Height": "120.0", "PackagingForPlace": "1"}

    def test_film_description_uses_metres_and_width(self):
        self.assertEqual(build_waybill_description(self.order()), "DTF плівка бренду TwoComms, 2.5 м, ширина 60 см")

    def test_mixed_description_preserves_both_goods(self):
        order = self.order()
        order.items.extend([SimpleNamespace(item_kind="dtf_film", film_length_m=Decimal("1.25"), qty=1), SimpleNamespace(item_kind="clothing", qty=2)])
        order.custom_print_leads = [SimpleNamespace(quantity=1)]
        description = build_waybill_description(order)
        self.assertEqual(description, "DTF плівка бренду TwoComms, 3.75 м, ширина 60 см; одяг 3 шт.")
        self.assertLessEqual(len(description), 100)

    def test_clothing_description_is_unchanged(self):
        self.assertEqual(build_waybill_description(self.order(film=False)), "Одяг бренду TwoComms, Худі")

    def test_film_initial_uses_provider_tube_and_weight(self):
        service = NovaPoshtaDocumentService()
        with (patch.object(service, "_resolve_default_sender_point", return_value=self.point()), patch.object(service, "_resolve_film_packaging", return_value=self.tube())):
            initial = service.build_initial_payload(self.order())
        self.assertEqual((initial["weight"], initial["length_cm"], initial["width_cm"], initial["height_cm"]), ("2.0", "60", "16", "12"))
        self.assertEqual(initial["packaging_type"], "np_tube_60")
        self.assertEqual(initial["packaging_ref"], "mock-tube60-ref")

    def test_clothing_package_defaults_are_unchanged(self):
        initial = NovaPoshtaDocumentService().build_package_defaults(self.order(film=False))
        self.assertEqual((initial["weight"], initial["length_cm"], initial["width_cm"], initial["height_cm"]), ("1.0", "30", "20", "8"))

    def test_new_manual_clothing_preselects_np_bag_without_changing_legacy_defaults(self):
        service = NovaPoshtaDocumentService()
        manual = self.order(film=False, source="manual", payment_payload=self.manual_payload())
        self.assertEqual(service.build_package_defaults(manual)["packaging_type"], "np_clothing_bag")
        for order in (self.order(film=False, source="manual"), self.order(film=False, source="website", payment_payload=self.manual_payload())):
            self.assertEqual(service.build_package_defaults(order)["packaging_type"], "own_packaging")

    def test_unpaid_online_order_balance_is_independent_of_cod(self):
        snapshot = build_order_payment_snapshot(self.order(pay_type="online_full"))
        self.assertEqual(snapshot["cod_amount"], "0.00")
        self.assertEqual(snapshot["remaining_amount"], "900.00")

    def test_confirmed_partial_online_order_balance_keeps_established_paid_amount(self):
        snapshot = build_order_payment_snapshot(self.order(pay_type="online_full", payment_status="prepaid", get_prepayment_amount=lambda: Decimal("123.45")))
        self.assertEqual(snapshot["paid_amount"], "123.45")
        self.assertEqual(snapshot["remaining_amount"], "776.55")
        self.assertEqual(snapshot["cod_amount"], "0.00")

    def test_partial_manual_payment_subtracts_actual_confirmed_amount(self):
        snapshot = build_order_payment_snapshot(self.order(payment_status="prepaid", pay_type="prepayment", payment_payload=self.manual_payload()))
        self.assertEqual(snapshot["declared_cost"], "900.00")
        self.assertEqual(snapshot["paid_amount"], "150.50")
        self.assertEqual(snapshot["cod_amount"], "749.50")
        self.assertEqual(snapshot["delivery_payer_type"], "Sender")
        self.assertEqual(snapshot["delivery_payment_method"], "NonCash")

    def test_explicit_no_cod_retains_unpaid_merchandise_balance(self):
        snapshot = build_order_payment_snapshot(self.order(payment_status="prepaid", pay_type="prepayment", payment_payload=self.manual_payload(cod=False)))
        self.assertEqual(snapshot["remaining_amount"], "749.50")
        self.assertEqual(snapshot["cod_amount"], "0.00")

    def test_unpaid_contract_does_not_claim_payment_from_payload(self):
        snapshot = build_order_payment_snapshot(self.order(payment_payload=self.manual_payload(paid="500")))
        self.assertEqual(snapshot["paid_amount"], "0.00")
        self.assertEqual(snapshot["cod_amount"], "900.00")

    def test_paid_manual_order_has_no_cod(self):
        snapshot = build_order_payment_snapshot(self.order(payment_status="paid", payment_payload=self.manual_payload()))
        self.assertEqual(snapshot["cod_amount"], "0.00")
        self.assertEqual(snapshot["paid_amount"], "900.00")

    def canonical_policy(self, mode="customer_prepaid", *, goods_paid="100.00", fee="70.00"):
        return build_delivery_payment_contract(
            mode=mode, merchandise_total="900.00", delivery_amount=fee if mode == "customer_prepaid" else "0",
            actor_id=1, confirmed_amount=str(Decimal(goods_paid) + Decimal(fee)) if mode == "customer_prepaid" else None,
            allocated_delivery_amount=fee if mode == "customer_prepaid" else None,
        )

    def test_canonical_customer_paid_delivery_is_received_once_and_excluded_from_cod(self):
        for status, goods_paid, expected_paid, expected_cod in (("prepaid", "100.00", "170.00", "800.00"), ("unpaid", "0.00", "70.00", "900.00"), ("paid", "900.00", "970.00", "0.00")):
            with self.subTest(status=status):
                payload = self.manual_payload(paid=goods_paid, payer="Recipient")
                payload["delivery_payment"] = self.canonical_policy(goods_paid=goods_paid)
                snapshot = build_order_payment_snapshot(self.order(payment_status=status, payment_payload=payload))
                self.assertTrue(snapshot["manual_shipment_payment_valid"])
                self.assertEqual(snapshot["delivery_payer_type"], "Sender")
                self.assertEqual(snapshot["paid_amount"], expected_paid)
                self.assertEqual(snapshot["cod_amount"], expected_cod)
                self.assertEqual(snapshot["remaining_amount"], expected_cod)

    def test_canonical_shipping_payer_overrides_legacy_manual_metadata(self):
        for mode, expected_payer in (("carrier_recipient", "Recipient"), ("merchant_free", "Sender")):
            with self.subTest(mode=mode):
                payload = self.manual_payload(payer="Sender" if expected_payer == "Recipient" else "Recipient")
                payload["delivery_payment"] = self.canonical_policy(mode)
                snapshot = build_order_payment_snapshot(self.order(payment_status="prepaid", payment_payload=payload))
                self.assertEqual(snapshot["delivery_payer_type"], expected_payer)
                self.assertEqual(snapshot["paid_amount"], "150.50")
                self.assertEqual(snapshot["cod_amount"], "749.50")

    @override_settings(NOVA_POSHTA_API_KEY="mocked-key")
    def test_unknown_canonical_shipping_policy_blocks_provider_mutations(self):
        payload = self.manual_payload()
        payload["delivery_payment"] = {"schema": "unknown", "mode": "merchant_free"}
        order = self.order(payment_payload=payload)
        service = NovaPoshtaDocumentService()
        self.assertTrue(build_order_payment_snapshot(order)["delivery_payment_requires_manual"])
        with patch.object(service, "_request") as request_mock:
            with self.assertRaisesRegex(NovaPoshtaDocumentError, "Уточніть"):
                service.create_waybill(order, {})
        request_mock.assert_not_called()

    def test_canonical_shipping_change_invalidates_signed_manual_form(self):
        payload = self.manual_payload()
        payload["delivery_payment"] = self.canonical_policy("merchant_free")
        order = self.order(payment_payload=payload)
        token = build_shipment_contract_token(order)
        order.payment_payload["delivery_payment"] = self.canonical_policy("carrier_recipient")
        with self.assertRaisesRegex(NovaPoshtaDocumentError, "Оновіть сторінку"):
            validate_shipment_contract_token(order, token)

    def test_canonical_review_shipping_cannot_be_weakened_by_manual_metadata(self):
        payload = self.manual_payload(payer="Recipient", paid="0")
        payload.update({"instagram_payment_review_id": 1, "manager_payment_decision_id": 2,
                        "manager_confirmed_amount": "970.00", "manual_payment_evidence_confirmed": True,
                        "manager_verification_scope": "full_payment"})
        payload["delivery_payment"] = build_delivery_payment_contract(
            mode="customer_prepaid", merchandise_total="900.00", delivery_amount="70.00",
            actor_id=3, authority="payment_review", review_id=1, decision_id=2,
            evidence_message_ids=[4], confirmed_amount="970.00",
        )
        snapshot = build_order_payment_snapshot(self.order(payment_payload=payload))
        self.assertTrue(snapshot["delivery_payment_locked"])
        self.assertFalse(snapshot["manual_shipment_payment_valid"])
        self.assertEqual(snapshot["delivery_payer_type"], "Sender")
        self.assertEqual(snapshot["paid_amount"], "970.00")
        self.assertEqual(snapshot["cod_amount"], "0.00")
        self.assertFalse(snapshot["cod_enabled"])

    def test_malformed_manual_contract_cannot_claim_authority(self):
        for invalid_paid in ("NaN", "Infinity", "-1", "oops"):
            with self.subTest(invalid_paid=invalid_paid):
                snapshot = build_order_payment_snapshot(self.order(payment_payload=self.manual_payload(paid=invalid_paid)))
                self.assertFalse(snapshot["manual_shipment_payment_valid"])
                self.assertEqual(snapshot["delivery_payer_type"], "Recipient")

    def test_instagram_review_contract_has_precedence(self):
        payload = self.manual_payload(payer="Recipient")
        payload.update({"manager_confirmed_amount": "970.00", "manual_payment_evidence_confirmed": True, "manager_verification_scope": "full_payment", "manager_payment_decision_id": 2,
                        "instagram_delivery_contract": {"review_id": 1, "decision_id": 2, "actor_id": 3, "evidence_message_ids": [4], "prepaid": True, "payer_type": "Sender", "merchandise_total": "900.00", "delivery_amount": "70.00", "payable_total": "970.00"}})
        with patch("management.services.ig_order_links.order_fulfillment_payment_verified", return_value=True):
            snapshot = build_order_payment_snapshot(self.order(payment_payload=payload))
        self.assertFalse(snapshot["manual_shipment_payment_valid"])
        self.assertTrue(snapshot["delivery_prepaid"])
        self.assertEqual(snapshot["delivery_payer_type"], "Sender")
        self.assertEqual(snapshot["cod_amount"], "0.00")

    def test_lookup_failure_fallback_keeps_film_and_payment_contract(self):
        initial = _fallback_waybill_initial(NovaPoshtaDocumentService(), self.order(payment_status="prepaid", payment_payload=self.manual_payload(cod=False)))
        self.assertEqual(initial["length_cm"], "60")
        self.assertEqual(initial["payer_type"], "Sender")
        self.assertEqual(initial["payment_method"], "NonCash")
        self.assertEqual(initial["cod_amount"], "")
        self.assertTrue(initial["shipment_contract_token"])

    def test_contract_token_rejects_changed_payer_or_method_before_provider_calls(self):
        order = self.order(payment_payload=self.manual_payload(payer="Recipient", method="Cash"))
        service = NovaPoshtaDocumentService()
        initial = _fallback_waybill_initial(service, order)
        order.payment_payload = self.manual_payload(payer="Sender", method="NonCash")
        with patch.object(service, "_request") as request_mock, patch.object(service, "_resolve_sender_profile") as profile_mock:
            with self.assertRaisesRegex(NovaPoshtaDocumentError, "Оновіть сторінку"):
                service.create_waybill(order, initial)
        request_mock.assert_not_called()
        profile_mock.assert_not_called()

    def test_contract_token_rejects_changed_goods_even_when_total_unchanged(self):
        order = self.order(payment_payload=self.manual_payload())
        token = build_shipment_contract_token(order)
        order.items[0].film_length_m = Decimal("3.00")
        with self.assertRaisesRegex(NovaPoshtaDocumentError, "Оновіть сторінку"):
            validate_shipment_contract_token(order, token)

    def test_contract_token_rejects_changed_recipient_address(self):
        order = self.order(payment_payload=self.manual_payload())
        token = build_shipment_contract_token(order)
        order.np_warehouse_ref = "changed-warehouse"
        with self.assertRaisesRegex(NovaPoshtaDocumentError, "Оновіть сторінку"):
            validate_shipment_contract_token(order, token)

    def test_manual_contract_requires_valid_untampered_token(self):
        order = self.order(payment_payload=self.manual_payload())
        token = build_shipment_contract_token(order)
        validate_shipment_contract_token(order, token)
        for invalid in ("", token + "tampered"):
            with self.subTest(invalid=invalid), self.assertRaises(NovaPoshtaDocumentError):
                validate_shipment_contract_token(order, invalid)

    def test_legacy_orders_do_not_require_shipment_contract_token(self):
        order = self.order()
        self.assertEqual(build_shipment_contract_token(order), "")
        validate_shipment_contract_token(order, "")

    @override_settings(NOVA_POSHTA_API_KEY="mocked-key")
    def test_stale_post_is_rejected_against_locked_order_before_service_create(self):
        order = self.order(pk=37, payment_payload=self.manual_payload(payer="Recipient", method="Cash"))
        initial = _fallback_waybill_initial(NovaPoshtaDocumentService(), order)
        initial["token"] = "mock-valid-action-token"
        request = RequestFactory().post("/mock-waybill/", initial)
        order.payment_payload = self.manual_payload(payer="Sender", method="NonCash")
        with (patch("storefront.views.order_actions.verify_order_action_token", return_value=True),
              patch("storefront.views.order_actions.get_object_or_404", return_value=order),
              patch("storefront.views.order_actions.transaction.atomic", return_value=nullcontext()),
              patch("storefront.views.order_actions.Order.objects.select_for_update") as locked_query,
              patch("storefront.views.order_actions._render_waybill_action_page") as render_page,
              patch.object(NovaPoshtaDocumentService, "create_waybill") as create_waybill):
            locked_query.return_value.select_related.return_value.prefetch_related.return_value.get.return_value = order
            telegram_order_np_waybill_action(request, order_id=37, action="create-np-waybill")
        create_waybill.assert_not_called()
        locked_query.assert_called_once_with()
        self.assertIn("Оновіть сторінку", render_page.call_args.kwargs["error_message"])

    def test_non_np_order_is_blocked_before_provider_calls(self):
        service = NovaPoshtaDocumentService()
        for delivery in ("handover", "manual"):
            with self.subTest(delivery=delivery):
                order = self.order(delivery_method=delivery)
                self.assertIn("лише", _waybill_action_block_reason(order))
                with patch.object(service, "_request") as request_mock:
                    with self.assertRaises(NovaPoshtaDocumentError):
                        service.create_waybill(order, {})
                    request_mock.assert_not_called()

    def test_mixed_order_is_blocked_before_lookup_or_provider_mutations(self):
        service = NovaPoshtaDocumentService()
        for custom in (False, True):
            order = self.order()
            if custom:
                order.custom_print_leads = [SimpleNamespace(quantity=1)]
            else:
                order.items.append(SimpleNamespace(item_kind="clothing", qty=1))
            with self.subTest(custom=custom), patch.object(service, "_request") as request_mock, patch.object(service, "_resolve_default_sender_point") as sender_mock:
                for operation in (lambda: service.build_initial_payload(order), lambda: service.create_waybill(order, {})):
                    with self.assertRaisesRegex(NovaPoshtaDocumentError, "окремими"):
                        operation()
                self.assertIn("окремими", _waybill_action_block_reason(order))
                request_mock.assert_not_called()
                sender_mock.assert_not_called()

    @override_settings(NOVA_POSHTA_API_KEY="mocked-key")
    def test_packaging_lookup_failure_blocks_before_creating_counterparty(self):
        service = NovaPoshtaDocumentService()
        with (patch.object(service, "_resolve_sender_profile") as profile, patch.object(service, "_resolve_film_packaging", side_effect=NovaPoshtaDocumentError("Довідник недоступний"))):
            with self.assertRaisesRegex(NovaPoshtaDocumentError, "недоступний"):
                service.create_waybill(self.order(), {"length_cm": "60", "width_cm": "16", "height_cm": "12"})
        profile.assert_not_called()

    def test_packaging_resolver_uses_only_catalog_tube60_for_place(self):
        service = NovaPoshtaDocumentService()
        bare_tube = dict(self.tube(), Ref="bare-tube-ref", Description="Тубус 60 Прямокутний", PackagingForPlace="0")
        tube120 = dict(self.tube(), Ref="tube120-ref", Description="Тубус 120 Прямокутний")
        with patch.object(service, "_request", return_value={"data": [bare_tube, tube120, self.tube()]}) as request_mock:
            selected = service._resolve_film_packaging()
        self.assertEqual(selected["Ref"], "mock-tube60-ref")
        request_mock.assert_called_once_with("Common", "getPackList", {"PackForSale": "1"})

    def test_missing_or_ambiguous_catalog_packaging_has_no_invented_ref(self):
        service = NovaPoshtaDocumentService()
        for entries in ([], [self.tube(), dict(self.tube(), Ref="other-ref")]):
            with self.subTest(entries=entries), patch.object(service, "_request", return_value={"data": entries}):
                with self.assertRaises(NovaPoshtaDocumentError):
                    service._resolve_film_packaging()

    def bags(self):
        return [{"Ref": "mock-bag2-ref", "Description": "Пакет для одягу 2кг", "Length": "390", "Width": "290", "Height": "70", "PackagingForPlace": "1"},
                {"Ref": "mock-bag4-ref", "Description": "Великий пакет для одягу (4 кг)", "Length": "460", "Width": "340", "Height": "100", "PackagingForPlace": "1"}]

    def test_selected_clothing_bag_fits_entered_weight_and_dimensions(self):
        service = NovaPoshtaDocumentService()
        payload = {"weight": "1", "length_cm": "30", "width_cm": "20", "height_cm": "7"}
        with patch.object(service, "_request", return_value={"data": self.bags()}):
            self.assertEqual(service._resolve_clothing_packaging(payload)["Ref"], "mock-bag2-ref")
            payload["height_cm"] = "8"
            self.assertEqual(service._resolve_clothing_packaging(payload)["Ref"], "mock-bag4-ref")
            payload["weight"] = "5"
            with self.assertRaises(NovaPoshtaDocumentError):
                service._resolve_clothing_packaging(payload)

    def test_form_packaging_choices_follow_goods_type(self):
        film_form = TelegramNovaPoshtaWaybillForm(order=self.order())
        clothing_form = TelegramNovaPoshtaWaybillForm(order=self.order(film=False))
        self.assertEqual([choice[0] for choice in film_form.fields["packaging_type"].choices], ["np_tube_60"])
        self.assertEqual([choice[0] for choice in clothing_form.fields["packaging_type"].choices], ["own_packaging", "np_clothing_bag"])

    @override_settings(NOVA_POSHTA_API_KEY="mocked-key")
    def test_mock_provider_receives_dimensions_payment_and_verified_catalog_packaging(self):
        service = NovaPoshtaDocumentService()
        order = self.order(payment_status="prepaid", payment_payload=self.manual_payload(cod=False))
        initial = _fallback_waybill_initial(service, order)
        initial["cod_amount"] = "900"  # Old/stale UI cannot re-enable COD.
        with (patch.object(service, "_resolve_sender_profile", return_value={"sender_ref": "sender", "contact_ref": "contact", "phone": "380991112233"}),
              patch.object(service, "_resolve_film_packaging", return_value=self.tube()),
              patch.object(service, "_resolve_point", return_value=self.point()),
              patch.object(service, "_create_recipient_counterparty", return_value=("recipient", "contact")),
              patch.object(service, "_request", side_effect=[NovaPoshtaInvalidDescriptionError(), {"data": [{"Ref": "doc", "IntDocNumber": "20400000000000"}]}]) as request_mock):
            service.create_waybill(order, initial)
        properties = request_mock.call_args.args[2]
        self.assertEqual(properties["OptionsSeat"][0]["volumetricLength"], "60")
        self.assertEqual(properties["OptionsSeat"][0]["packRef"], "mock-tube60-ref")
        self.assertEqual(properties["Weight"], "2")
        self.assertEqual(properties["PayerType"], "Sender")
        self.assertEqual(properties["PaymentMethod"], "NonCash")
        self.assertIn("DTF", properties["Description"])
        self.assertIn("2.5 м", properties["Description"])
        self.assertNotIn("PackingNumber", properties)
        self.assertNotIn("AfterpaymentOnGoodsCost", properties)

    @override_settings(NOVA_POSHTA_API_KEY="mocked-key")
    def test_fresh_manual_form_cannot_override_saved_delivery_payer(self):
        service = NovaPoshtaDocumentService()
        order = self.order(payment_payload=self.manual_payload(payer="Sender", method="NonCash"))
        initial = _fallback_waybill_initial(service, order)
        initial.update({"payer_type": "Recipient", "payment_method": "Cash"})
        with (patch.object(service, "_resolve_sender_profile", return_value={"sender_ref": "sender", "contact_ref": "contact", "phone": "380991112233"}),
              patch.object(service, "_resolve_film_packaging", return_value=self.tube()),
              patch.object(service, "_resolve_point", return_value=self.point()),
              patch.object(service, "_create_recipient_counterparty", return_value=("recipient", "contact")),
              patch.object(service, "_request", return_value={"data": [{"Ref": "doc", "IntDocNumber": "20400000000000"}]}) as request_mock):
            with self.assertRaisesRegex(NovaPoshtaDocumentError, "збереженим умовам"):
                service.create_waybill(order, initial)
        request_mock.assert_not_called()

    @override_settings(NOVA_POSHTA_API_KEY="mocked-key")
    def test_direct_film_creation_defaults_to_catalog_dimensions_and_two_kg(self):
        service = NovaPoshtaDocumentService()
        order = self.order()
        initial = _fallback_waybill_initial(service, order)
        for field_name in ("weight", "length_cm", "width_cm", "height_cm"):
            initial.pop(field_name)
        with (patch.object(service, "_resolve_sender_profile", return_value={"sender_ref": "sender", "contact_ref": "contact", "phone": "380991112233"}),
              patch.object(service, "_resolve_film_packaging", return_value=self.tube()),
              patch.object(service, "_resolve_point", return_value=self.point()),
              patch.object(service, "_create_recipient_counterparty", return_value=("recipient", "contact")),
              patch.object(service, "_request", return_value={"data": [{"Ref": "doc", "IntDocNumber": "20400000000000"}]}) as request_mock):
            service.create_waybill(order, initial)
        properties = request_mock.call_args.args[2]
        seat = properties["OptionsSeat"][0]
        self.assertEqual(properties["Weight"], "2")
        self.assertEqual((seat["volumetricLength"], seat["volumetricWidth"], seat["volumetricHeight"]), ("60", "16", "12"))
        self.assertEqual(seat["packRef"], "mock-tube60-ref")
