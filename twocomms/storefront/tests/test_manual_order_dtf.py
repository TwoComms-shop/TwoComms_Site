import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from orders.models import Order, OrderItem
from orders.nova_poshta_documents import build_order_payment_snapshot
from orders.order_edit_diff import build_order_edit_diff, snapshot_order
from storefront.views.manual_orders import (
    _build_order_item, _manual_payment_controls, _resolve_delivery, _DeliveryError,
)


class DtfFilmValidationTests(SimpleTestCase):
    def film(self, **values):
        return _build_order_item(
            {'kind': 'dtf_film', 'film_length_m': '1,25', **values},
            order=Order(), products_map={}, variants_map={},
        )

    def test_fractional_metres_default_rate_and_fixed_identity(self):
        item = self.film(qty=99, title='Інша назва', size='XXL', color_name='Чорний')
        self.assertEqual(item.film_length_m, Decimal('1.25'))
        self.assertEqual(item.unit_price, Decimal('320.00'))
        self.assertEqual(item.line_total, Decimal('400.00'))
        self.assertEqual(item.qty, 1)
        self.assertEqual(item.title, 'DTF плівка TwoComms')
        self.assertTrue(item.is_dtf_film)
        self.assertTrue(item.is_custom)
        self.assertEqual(item.film_length_display, '1.25')
        self.assertEqual(item.size, '')

    def test_money_uses_decimal_round_half_up(self):
        item = self.film(film_length_m='0.01', unit_price='320.50')
        self.assertEqual(item.line_total, Decimal('3.21'))

    def test_invalid_length_never_silently_rounds_or_clamps(self):
        for value in ('', '0', '-1', '1.234', 'NaN', 'Infinity', '1e2', '1,2.3', '1000000', {}, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.film(film_length_m=value)

    def test_invalid_rate_and_oversized_line_rejected(self):
        for value in ('-1', 'NaN', '2.001', '1e100', '10000000000'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.film(unit_price=value)
        with self.assertRaises(ValueError):
            self.film(film_length_m='999999.99', unit_price='9999999999.99')

    def test_model_rejects_invalid_film_without_writing(self):
        item = OrderItem(item_kind='dtf_film', film_length_m=Decimal('NaN'), unit_price=320)
        with self.assertRaises(ValidationError):
            item.save()

    def test_clothing_still_uses_integer_quantity(self):
        item = _build_order_item(
            {'kind': 'custom', 'title': 'Термофутболка', 'qty': 3, 'unit_price': '850', 'size': 'XL'},
            order=Order(), products_map={}, variants_map={},
        )
        self.assertFalse(item.is_dtf_film)
        self.assertEqual(item.item_kind, 'clothing')
        self.assertEqual(item.line_total, Decimal('2550'))
        self.assertEqual(item.size, 'XL')

    def test_handover_needs_description_and_not_postal_address(self):
        delivery = _resolve_delivery({'delivery_method': 'handover', 'handover_details': 'Передати Олегу на зустрічі'})
        self.assertEqual(delivery['city'], '')
        self.assertEqual(delivery['np_office'], '')
        self.assertEqual(delivery['delivery_method'], 'handover')
        with self.assertRaises(_DeliveryError) as failure:
            _resolve_delivery({'delivery_method': 'handover', 'handover_details': '  '})
        self.assertEqual(failure.exception.field, 'handover_details')


class ManualPaymentControlTests(SimpleTestCase):
    def controls(self, preset='partial_manual', order=None, **data):
        return _manual_payment_controls(
            data, order=order or Order(), preset_key=preset, merchandise_total=Decimal('800.00'),
        )

    def test_partial_payment_and_delivery_controls_are_independent(self):
        controls = self.controls(paid_amount='123,45', delivery_payer_type='Sender', delivery_payment_method='NonCash')
        self.assertEqual(controls, {
            'payer_type': 'Sender', 'payment_method': 'NonCash',
            'cod_enabled': True, 'paid_amount': '123.45',
        })

    def test_invalid_partial_payment_is_rejected(self):
        for amount in ('0', '800', '801', '-1', '1.234', 'NaN', '1e1'):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.controls(paid_amount=amount)

    def test_paid_order_cannot_collect_cod_again(self):
        with self.assertRaises(ValueError):
            self.controls('paid_full', paid_amount='800', cod_enabled=True)

    def test_unpaid_transfer_can_have_no_cod(self):
        controls = self.controls('unpaid_full', cod_enabled=False, paid_amount='0')
        self.assertFalse(controls['cod_enabled'])
        self.assertEqual(controls['paid_amount'], '0.00')

    def test_manual_payment_fields_cannot_replace_reviewed_authority(self):
        order = Order(payment_payload={
            'manual_payment_evidence_confirmed': True,
            'manager_confirmed_amount': '315.00',
            'instagram_delivery_contract': {'payer_type': 'Sender'},
        })
        self.assertIsNone(self.controls('manager_prepayment', order=order, paid_amount='1', delivery_payer_type='Recipient'))
        with self.assertRaises(ValueError):
            self.controls('partial_manual', order=order, paid_amount='1')

    def test_cannot_manufacture_provider_confirmation_using_preset(self):
        with self.assertRaises(ValueError):
            self.controls('provider_prepayment', paid_amount='315')

    def test_failed_provider_invoice_is_not_payment_authority(self):
        order = Order(payment_provider='monobank_pay', payment_invoice_id='invoice-failed', payment_status='unpaid',
                      payment_payload={'paid_value': '100.00', 'monobank_status': 'failure'})
        with self.assertRaises(ValueError):
            self.controls('provider_prepayment', order=order)


class ManualDtfOrderEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(username='dtf-manual-staff', is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def payload(self, **values):
        return {
            'full_name': 'Олег Тестовий', 'phone': '+380500234363',
            'delivery_method': 'manual', 'city': 'Харків', 'np_office': 'Відділення №4',
            'payment_preset': 'partial_manual', 'paid_amount': '100',
            'delivery_payer_type': 'Sender', 'delivery_payment_method': 'NonCash', 'cod_enabled': True,
            'items': [{'kind': 'dtf_film', 'film_length_m': '1,25'}],
            **values,
        }

    def create(self, **values):
        with mock.patch('storefront.views.manual_orders.telegram_notifier.send_new_order_notification'):
            return self.client.post(reverse('manual_order_create'), json.dumps(self.payload(**values)), content_type='application/json')

    def edit(self, order, **values):
        payload = self.payload(**{'delivery_method': 'keep', **values})
        with mock.patch('storefront.views.manual_orders.telegram_notifier.update_order_notification_message'), mock.patch(
            'storefront.views.manual_orders.telegram_notifier.send_order_edit_notification',
        ) as notification:
            response = self.client.post(reverse('manual_order_edit', args=[order.pk]), json.dumps(payload), content_type='application/json')
        return response, notification

    def test_create_partial_dtf_order_and_edit_data_round_trip(self):
        response = self.create()
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        item = order.items.get()
        self.assertEqual((item.item_kind, item.qty, item.line_total), ('dtf_film', 1, Decimal('400.00')))
        self.assertEqual(order.delivery_method, 'manual')
        self.assertEqual(order.payment_payload['paid_value'], '100.00')
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual(snapshot['paid_amount'], '100.00')
        self.assertEqual(snapshot['cod_amount'], '300.00')
        self.assertEqual(snapshot['delivery_payer_type'], 'Sender')
        data = self.client.get(reverse('manual_order_edit_data', args=[order.pk])).json()['order']
        self.assertEqual(data['payment_preset'], 'partial_manual')
        self.assertEqual(data['paid_amount'], '100.00')
        self.assertEqual(data['delivery_payment_method'], 'NonCash')
        self.assertEqual(data['items'][0]['film_length_m'], '1.25')

    def test_handover_persists_without_np_address(self):
        response = self.create(
            delivery_method='handover', handover_details='Передати клієнту Олегу в офісі',
            city='', np_office='', payment_preset='paid_full', paid_amount='400', cod_enabled=False,
        )
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.delivery_method, 'handover')
        self.assertEqual(order.handover_details, 'Передати клієнту Олегу в офісі')
        self.assertFalse(order.np_city_ref)
        self.assertEqual((order.city, order.np_office), ('', ''))

    def test_invalid_amount_and_film_input_roll_back_creation(self):
        for values in ({'paid_amount': '400'}, {'items': [{'kind': 'dtf_film', 'film_length_m': '1.234'}]}, {'paid_amount': 'NaN'}):
            with self.subTest(values=values):
                response = self.create(**values)
                self.assertEqual(response.status_code, 422, response.content)
                self.assertFalse(Order.objects.exists())

    def test_unpaid_cod_off_keeps_remaining_debt(self):
        response = self.create(payment_preset='unpaid_full', paid_amount='0', cod_enabled=False)
        self.assertEqual(response.status_code, 200, response.content)
        snapshot = build_order_payment_snapshot(Order.objects.get(pk=response.json()['order_id']))
        self.assertEqual(snapshot['cod_amount'], '0.00')
        self.assertEqual(snapshot['remaining_amount'], '400.00')

    def test_shipped_order_cannot_be_changed_to_handover(self):
        response = self.create()
        order = Order.objects.get(pk=response.json()['order_id'])
        order.tracking_number = '20450000000001'
        order.nova_poshta_document_ref = 'existing-document'
        order.save(update_fields=['tracking_number', 'nova_poshta_document_ref'])
        response, _ = self.edit(order, delivery_method='handover', handover_details='Передати в офісі', cod_enabled=False)
        self.assertEqual(response.status_code, 422, response.content)
        order.refresh_from_db()
        self.assertEqual(order.delivery_method, 'manual')
        self.assertEqual(order.nova_poshta_document_ref, 'existing-document')

    def test_film_length_edit_is_structured_and_bad_payment_rolls_back(self):
        response = self.create()
        order = Order.objects.get(pk=response.json()['order_id'])
        response, notification = self.edit(order, items=[{'kind': 'dtf_film', 'film_length_m': '2.5'}])
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertEqual(order.total_sum, Decimal('800.00'))
        change = notification.call_args.args[1]['items']['changed'][0]
        self.assertEqual((change['old_film_length_m'], change['new_film_length_m']), (Decimal('1.25'), Decimal('2.50')))
        response, _ = self.edit(order, paid_amount='999')
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(order.items.get().film_length_m, Decimal('2.50'))

    def test_model_save_recalculates_film_without_changing_clothing(self):
        response = self.create()
        item = Order.objects.get(pk=response.json()['order_id']).items.get()
        item.film_length_m = Decimal('2.50')
        item.qty = 12
        item.save(update_fields=['film_length_m', 'qty'])
        item.refresh_from_db()
        self.assertEqual(item.line_total, Decimal('800.00'))
        self.assertEqual(item.qty, 1)

    def test_payment_control_only_change_is_in_edit_diff(self):
        response = self.create()
        order = Order.objects.get(pk=response.json()['order_id'])
        before = snapshot_order(order)
        response, _ = self.edit(order, delivery_payment_method='Cash')
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        diff = build_order_edit_diff(before, snapshot_order(order))
        self.assertTrue(diff['has_changes'])
        self.assertEqual(diff['payment']['new_shipment_payment']['payment_method'], 'Cash')

    def test_legacy_full_paid_default_controls_are_semantic_no_op(self):
        response = self.create(payment_preset='paid_full', paid_amount='400', cod_enabled=False)
        order = Order.objects.get(pk=response.json()['order_id'])
        order.source, order.payment_payload = 'web', None
        order.save(update_fields=['source', 'payment_payload'])
        before = snapshot_order(order)
        response, notification = self.edit(
            order, payment_preset='paid_full', paid_amount='400', cod_enabled=False,
            delivery_payer_type='Recipient', delivery_payment_method='Cash',
        )
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertFalse(build_order_edit_diff(before, snapshot_order(order))['has_changes'])
        self.assertFalse(notification.call_args.args[1]['has_changes'])

    def test_legacy_unpaid_cod_disable_is_real_change(self):
        response = self.create(payment_preset='cod', paid_amount='0', cod_enabled=True)
        order = Order.objects.get(pk=response.json()['order_id'])
        order.source, order.payment_payload = 'web', None
        order.save(update_fields=['source', 'payment_payload'])
        before = snapshot_order(order)
        self.assertTrue(before['shipment_payment']['cod_enabled'])
        response, _ = self.edit(order, payment_preset='cod', paid_amount='0', cod_enabled=False)
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        diff = build_order_edit_diff(before, snapshot_order(order))
        self.assertTrue(diff['has_changes'])
        self.assertFalse(diff['payment']['new_shipment_payment']['cod_enabled'])

    def test_provider_invoice_preserves_confirmed_amount_without_new_flag(self):
        response = self.create()
        order = Order.objects.get(pk=response.json()['order_id'])
        order.pay_type, order.payment_status = 'prepayment', 'prepaid'
        order.payment_provider, order.payment_invoice_id = 'monobank_pay', 'confirmed-invoice'
        order.payment_payload = {'paid_value': '100.00', 'monobank_status': 'success'}
        order.save()
        initial = self.client.get(reverse('manual_order_edit_data', args=[order.pk])).json()['order']
        self.assertEqual(initial['payment_preset'], 'provider_prepayment')
        response, _ = self.edit(order, payment_preset='provider_prepayment', paid_amount='1')
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertEqual(order.payment_payload['paid_value'], '100.00')
        self.assertNotIn('manual_shipment_payment', order.payment_payload)
        self.assertEqual(build_order_payment_snapshot(order)['paid_amount'], '100.00')

    def test_second_film_rate_change_has_distinct_diff_group(self):
        response = self.create()
        order = Order.objects.get(pk=response.json()['order_id'])
        OrderItem.objects.create(order=order, item_kind='dtf_film', film_length_m='1.00', unit_price='300.00')
        before = snapshot_order(order)
        film = order.items.get(unit_price=Decimal('300.00'))
        film.unit_price = Decimal('310.00')
        film.save(update_fields=['unit_price'])
        diff = build_order_edit_diff(before, snapshot_order(order))
        self.assertTrue(diff['has_changes'])
        self.assertEqual(len(diff['items']['removed']), 1)
        self.assertEqual(len(diff['items']['added']), 1)
        self.assertEqual(diff['items']['removed'][0]['unit_price'], Decimal('300.00'))
        self.assertEqual(diff['items']['added'][0]['unit_price'], Decimal('310.00'))

    def test_clothing_and_film_need_separate_packaging_and_orders(self):
        response = self.create(items=[
            {'kind': 'dtf_film', 'film_length_m': '1.25'},
            {'kind': 'custom', 'title': 'Термофутболка', 'unit_price': '850', 'qty': 1},
        ])
        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn('різне пакування', response.json()['message'])
        self.assertFalse(Order.objects.exists())

    def test_customer_prepaid_carriage_is_separate_from_partial_goods_payment(self):
        response = self.create(delivery_payment_mode='customer_prepaid', delivery_charge_amount='120', delivery_paid_confirmed=True)
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        contract = order.payment_payload['delivery_payment']
        self.assertEqual(contract['payment_confirmation']['confirmed_amount'], '220.00')
        self.assertEqual(contract['payment_confirmation']['allocated_delivery_amount'], '120.00')
        self.assertEqual(order.payment_payload['manual_shipment_payment']['paid_amount'], '100.00')
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual((snapshot['paid_amount'], snapshot['cod_amount'], snapshot['remaining_amount']), ('220.00', '300.00', '300.00'))
        initial = self.client.get(reverse('manual_order_edit_data', args=[order.pk])).json()['order']
        self.assertEqual((initial['paid_amount'], initial['merchandise_paid_amount']), ('220.00', '100.00'))
        response, _ = self.edit(order, delivery_payment_mode='customer_prepaid', delivery_charge_amount='120')
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertEqual(order.payment_payload['manual_shipment_payment']['paid_amount'], '100.00')
        self.assertEqual(build_order_payment_snapshot(order)['paid_amount'], '220.00')

    def test_paid_goods_and_paid_carriage_have_no_cod_or_duplicate_collection(self):
        response = self.create(payment_preset='paid_full', paid_amount='400', cod_enabled=False,
            delivery_payment_mode='customer_prepaid', delivery_charge_amount='120')
        self.assertEqual(response.status_code, 200, response.content)
        snapshot = build_order_payment_snapshot(Order.objects.get(pk=response.json()['order_id']))
        self.assertEqual((snapshot['paid_amount'], snapshot['cod_amount'], snapshot['payable_total']), ('520.00', '0.00', '520.00'))

    def test_prepaid_carriage_with_unpaid_goods_keeps_goods_debt(self):
        response = self.create(payment_preset='cod', paid_amount='0', cod_enabled=True,
            delivery_payment_mode='customer_prepaid', delivery_charge_amount='120', delivery_paid_confirmed=True)
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.payment_payload['delivery_payment']['payment_confirmation']['confirmed_amount'], '120.00')
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual((snapshot['paid_amount'], snapshot['cod_amount'], snapshot['remaining_amount']), ('120.00', '400.00', '400.00'))

    def test_canonical_mode_derives_payer_and_legacy_sender_maps_merchant_free(self):
        response = self.create(delivery_payment_mode='carrier_recipient', delivery_charge_amount='0', delivery_payer_type='Sender')
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.payment_payload['manual_shipment_payment']['payer_type'], 'Recipient')
        self.assertEqual(build_order_payment_snapshot(order)['delivery_payer_type'], 'Recipient')
        legacy = self.create()
        order = Order.objects.get(pk=legacy.json()['order_id'])
        self.assertEqual(order.payment_payload['delivery_payment']['mode'], 'merchant_free')

    def test_fixed_200_transfer_allocates_carriage_before_goods(self):
        response = self.create(payment_preset='prepaid_200', paid_amount='80', cod_enabled=True,
            delivery_payment_mode='customer_prepaid', delivery_charge_amount='120')
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.payment_payload['manual_shipment_payment']['paid_amount'], '80.00')
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual((snapshot['paid_amount'], snapshot['cod_amount']), ('200.00', '320.00'))

    def test_new_customer_prepaid_mode_needs_explicit_carriage_confirmation(self):
        response = self.create(payment_preset='cod', paid_amount='0', cod_enabled=True,
            delivery_payment_mode='customer_prepaid', delivery_charge_amount='120')
        self.assertEqual(response.status_code, 422, response.content)
        self.assertFalse(Order.objects.exists())

    def test_legacy_payer_only_edit_preserves_valid_prepaid_carriage_contract(self):
        response = self.create(delivery_payment_mode='customer_prepaid', delivery_charge_amount='120', delivery_paid_confirmed=True)
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        response, _ = self.edit(order, delivery_payer_type='Sender')
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        contract = order.payment_payload['delivery_payment']
        self.assertEqual((contract['mode'], contract['delivery_amount']), ('customer_prepaid', '120.00'))
        self.assertEqual(contract['payment_confirmation']['confirmed_amount'], '220.00')
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual((snapshot['paid_amount'], snapshot['cod_amount']), ('220.00', '300.00'))
