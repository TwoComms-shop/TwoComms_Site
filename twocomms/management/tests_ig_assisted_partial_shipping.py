"""Carrier arithmetic and actual native paid-order shipping boundaries."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
import json
import uuid

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse

from management.services import ig_assisted_partial_shipping as shipping
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture


class CarrierCeilingArithmeticTests(SimpleTestCase):
    def test_customer_maximum_is_not_an_invoice_or_carrier_estimate(self):
        accepted = {'payer': 'customer', 'customer_maximum': '120.00', 'currency': 'UAH'}
        original = deepcopy(accepted)
        actual = shipping.assess_carrier_charge(accepted, payer_type='Recipient', carrier_cost='80.50')
        self.assertEqual(actual['customer_charge'], '80.50')
        self.assertEqual(actual['carrier_cost'], '80.50')
        self.assertFalse(actual['merchant_funded'])
        self.assertEqual(accepted, original)
        with self.assertRaises(shipping.AssistedShippingError) as denied:
            shipping.assess_carrier_charge(accepted, payer_type='Recipient', carrier_cost='120.01')
        self.assertEqual(denied.exception.reason, 'assisted_customer_shipping_maximum_exceeded')

    def test_explicit_merchant_zero_prohibits_any_recipient_charge(self):
        accepted = {'payer': 'merchant', 'customer_maximum': '0.00', 'currency': 'UAH'}
        with self.assertRaises(shipping.AssistedShippingError):
            shipping.assess_carrier_charge(accepted, payer_type='Recipient', carrier_cost='0.00')
        actual = shipping.assess_carrier_charge(accepted, payer_type='Sender', carrier_cost='110.00')
        self.assertEqual(actual['customer_charge'], '0.00')
        self.assertEqual(actual['carrier_cost'], '110.00')
        self.assertTrue(actual['merchant_funded'])

    def test_merchant_can_honestly_pay_entire_over_cap_fee_without_fictitious_difference(self):
        accepted = {'payer': 'customer', 'customer_maximum': '80.00', 'currency': 'UAH'}
        actual = shipping.assess_carrier_charge(accepted, payer_type='Sender', carrier_cost='150.00')
        self.assertEqual(actual['customer_charge'], '0.00')
        self.assertEqual(actual['carrier_cost'], '150.00')
        self.assertTrue(actual['merchant_funded'])

    def test_unknown_nonfinite_or_unbounded_recipient_cost_cannot_dispatch(self):
        accepted = {'payer': 'customer', 'customer_maximum': '120.00', 'currency': 'UAH'}
        for amount in (None, '', True, 'NaN', 'Infinity', '-1.00', '1000000.01', Decimal('NaN')):
            with self.subTest(amount=amount), self.assertRaises(shipping.AssistedShippingError):
                shipping.assess_carrier_charge(accepted, payer_type='Recipient', carrier_cost=amount)
        self.assertEqual(shipping.assess_carrier_charge(accepted, payer_type='Recipient', carrier_cost=80.5)['customer_charge'], '80.50')


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, ROOT_URLCONF='twocomms.urls', IG_ASSISTED_CHECKOUT_V2='off',
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0)
class NativePartialShippingAuthorityTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def test_existing_native_full_paid_order_retains_ordinary_shipping_without_partial_contract(self):
        data = self._paid_custom_order()
        self.assertIsNone(shipping.assisted_shipping_contract(data['order']))
        self.assertIsNone(shipping.assert_assisted_shipping_allowed(data['order'], tracking_number='manual-full39'))

    def test_raw_partial_metadata_cannot_upgrade_actual_full_paid_shipping_authority(self):
        from orders.nova_poshta_documents import NovaPoshtaDocumentService, NovaPoshtaDocumentError
        data = self._paid_custom_order()
        order = data['order']
        order.payment_payload = {**order.payment_payload, 'ig_assisted_shipping': {
            'schema': shipping.SCHEMA, 'payer': 'merchant', 'customer_maximum': '0.00', 'currency': 'UAH'}}
        with patch.object(NovaPoshtaDocumentService, '_request') as carrier:
            with self.assertRaises(shipping.AssistedShippingError) as rejected:
                shipping.assisted_shipping_contract(order)
            self.assertEqual(rejected.exception.reason, 'assisted_shipping_native_order_missing')
            with self.assertRaises(NovaPoshtaDocumentError):
                NovaPoshtaDocumentService().create_waybill(order, {})
        carrier.assert_not_called()

    def test_actual_full39_paid_order_rejects_destructive_manual_editor_without_changing_receiver_or_rows(self):
        from orders.models import Order
        data = self._paid_custom_order()
        order = data['order']
        before = Order.objects.filter(pk=order.pk).values().get()
        before_items = list(order.items.order_by('pk').values())
        self.client.force_login(self.manager)
        with override_settings(ROOT_URLCONF='twocomms.urls', SECURE_SSL_REDIRECT=False):
            response = self.client.post(reverse('manual_order_edit', args=[order.pk]),
                json.dumps({'full_name': 'Інший одержувач', 'phone': '+380631111111',
                    'payment_preset': 'free', 'items': [{'is_custom': True, 'title': 'Підміна', 'qty': 9}]}),
                content_type='application/json', HTTP_HOST='twocomms.shop')
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn('management_url', response.json())
        self.assertEqual(Order.objects.filter(pk=order.pk).values().get(), before)
        self.assertEqual(list(order.items.order_by('pk').values()), before_items)


@override_settings(ROOT_URLCONF='twocomms.urls_management', SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2='off', IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0)
class NativeSelectedShippingTests(PartialCommerceFixture, TransactionTestCase):
    def _paid_selected(self):
        data = self.selected_case()
        prepared = self.prepare_selection(data['selection'])
        return self.pay_prepared(prepared), data

    def _tracking(self, *, cost='80.50', payer='Recipient', number='20450000000001', reference=None):
        return {'Number': number, 'PayerType': payer, 'Cost': cost, 'StatusCode': '1',
            'Status': 'Накладна створена', **({'Ref': reference} if reference else {})}

    def test_real_selected_paid_order_pins_only_now_cap_without_invoicing_wait_or_delivery(self):
        from management.models import IgSplitOrderPlan
        from orders.nova_poshta_documents import build_order_payment_snapshot
        order, data = self._paid_selected()
        accepted = shipping.assisted_shipping_contract(order)
        self.assertEqual((accepted['shipping_scope'], accepted['payer'], accepted['customer_maximum']),
            ('now', 'customer', '120.00'))
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.total_sum, Decimal('1590.00'))
        self.assertEqual(order.items.get().title, 'Фіолетове худі')
        self.assertFalse(IgSplitOrderPlan.objects.filter(order=order).exists())
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual(snapshot['delivery_charge_amount'], '0.00')
        self.assertEqual(snapshot['delivery_payer_type'], 'Recipient')
        self.assertIn('120.00', snapshot['delivery_payment_label'])
        self.assertEqual(snapshot['payable_total_value'], Decimal('1590.00'))
        self.assertEqual(accepted['selected_line_ids'], list(data['selection'].selected_line_ids))

    def test_shared_manual_ttn_writer_verifies_actual_under_cap_fee_and_preserves_paid_goods(self):
        from orders.status_management import apply_order_status_update
        from orders.nova_poshta_service import NovaPoshtaService
        order, _data = self._paid_selected()
        original = (order.total_sum, order.payment_status, order.items.get().pk)
        with patch.object(NovaPoshtaService, 'get_tracking_info', return_value=self._tracking()), \
            patch('management.services.instagram_bot._provider_http', return_value=(200, json.dumps({'message_id': 'native-cap-ttn'}))):
            result = apply_order_status_update(order.pk, status='ship', tracking_number='20450000000001',
                require_tracking_number=True)
        order.refresh_from_db()
        self.assertEqual(result['order'].tracking_number, '20450000000001')
        self.assertEqual((order.total_sum, order.payment_status, order.items.get().pk), original)
        self.assertEqual(shipping.assert_assisted_shipping_allowed(order)['customer_charge'], '80.50')
        self.assertTrue(shipping.assisted_shipping_dispatch_ready(order))

    def test_manual_over_cap_carrier_fee_rejects_before_tracking_or_dispatch_mutation(self):
        from orders.status_management import apply_order_status_update
        from orders.nova_poshta_service import NovaPoshtaService
        order, _data = self._paid_selected()
        order.refresh_from_db()
        before = (order.status, order.tracking_number, deepcopy(order.payment_payload))
        with patch.object(NovaPoshtaService, 'get_tracking_info', return_value=self._tracking(cost='120.01')):
            with self.assertRaises(ValidationError):
                apply_order_status_update(order.pk, status='ship', tracking_number='20450000000001',
                    require_tracking_number=True)
        order.refresh_from_db()
        self.assertEqual((order.status, order.tracking_number, order.payment_payload), before)
        self.assertEqual((order.total_sum, order.payment_status), (Decimal('1590.00'), 'paid'))

    def test_failed_native_cancellation_retains_reference_until_exact_manager_carrier_resolution(self):
        from orders.nova_poshta_service import NovaPoshtaService
        order, _data = self._paid_selected()
        reference = str(uuid.uuid4())
        actual = {'document_ref': reference, 'tracking_number': '20450000000001',
            'recipient_ref': str(uuid.uuid4()), 'recipient_contact_ref': str(uuid.uuid4())}
        order = shipping.preserve_assisted_waybill_failure(order.pk, created=actual, provider_cancelled=False)
        self.assertEqual((order.nova_poshta_document_ref, order.tracking_number),
            (reference, actual['tracking_number']))
        self.assertFalse(shipping.assisted_shipping_dispatch_ready(order))
        terms_digest = shipping.assisted_shipping_contract(order)['terms_digest']
        with patch.object(NovaPoshtaService, 'get_tracking_info',
                return_value=self._tracking(cost='150.00', payer='Sender', reference=reference)):
            result = shipping.resolve_assisted_shipping_review(order.pk, actor=self.manager,
                expected_document_ref=reference, expected_terms_digest=terms_digest)
        self.assertTrue(result['ready'], result)
        order.refresh_from_db()
        self.assertEqual((order.nova_poshta_document_ref, order.tracking_number),
            (reference, actual['tracking_number']))
        self.assertTrue(shipping.assisted_shipping_dispatch_ready(order))
        self.assertEqual(shipping.assert_assisted_shipping_allowed(order)['customer_charge'], '0.00')
        self.assertEqual(shipping.assisted_shipping_contract(order)['customer_maximum'], '120.00')

    def test_authenticated_parent_projection_contains_actual_order_only_and_hides_foreign_account(self):
        from django.contrib.auth import get_user_model
        from orders.services.customer_order_presentation import prepare_order_item_presentation
        order, _data = self._paid_selected()
        prepare_order_item_presentation([order], audience='staff', actor=self.manager)
        self.assertIsNotNone(order.assisted_group)
        self.assertEqual([row['order_number'] for row in order.assisted_group['orders']], [order.order_number])
        self.assertIn('120.00', order.assisted_group['shipping_label'])
        self.assertNotIn('Оливкове', str(order.assisted_group))
        foreign = get_user_model().objects.create_user('partial-shipping-foreign', password='test')
        prepare_order_item_presentation([order], audience='account', actor=foreign)
        self.assertIsNone(order.assisted_group)

    def test_native_remainder_paid_order_links_two_actual_orders_without_changing_original_payment_or_items(self):
        from orders.models import Order
        from orders.services.customer_order_presentation import prepare_order_item_presentation
        first, data = self._paid_selected()
        original = Order.objects.filter(pk=first.pk).values().get()
        original_items = list(first.items.order_by('pk').values())
        later = self.paid_remainder_case(data['request'])
        second = later['order']
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual((second.items.count(), second.items.get().title, second.total_sum),
            (1, 'Оливкове худі', Decimal('1490.00')))
        accepted = shipping.assisted_shipping_contract(second)
        self.assertEqual((accepted['shipping_scope'], accepted['customer_maximum']), ('later', '150.00'))
        first.refresh_from_db()
        self.assertEqual(Order.objects.filter(pk=first.pk).values().get(), original)
        self.assertEqual(list(first.items.order_by('pk').values()), original_items)
        prepare_order_item_presentation([first, second], audience='staff', actor=self.manager)
        expected_numbers = [first.order_number, second.order_number]
        for order in (first, second):
            self.assertEqual([row['order_number'] for row in order.assisted_group['orders']], expected_numbers)
        self.assertEqual(first.assisted_group['original_requested_at'], second.assisted_group['original_requested_at'])
        prepare_order_item_presentation([first, second], audience='account', actor=self.manager)
        self.assertEqual([row['order_number'] for row in first.assisted_group['orders']], expected_numbers)

    def test_native_selected_paid_editor_is_readonly_and_forged_submit_cannot_replace_items_or_receiver(self):
        from orders.models import Order
        order, _data = self._paid_selected()
        original = Order.objects.filter(pk=order.pk).values().get()
        original_items = list(order.items.order_by('pk').values())
        with override_settings(ROOT_URLCONF='twocomms.urls', SECURE_SSL_REDIRECT=False):
            read = self.client.get(reverse('manual_order_edit_data', args=[order.pk]), HTTP_HOST='twocomms.shop')
            self.assertEqual(read.status_code, 200, read.content)
            self.assertTrue(read.json()['edit_lock'])
            self.assertTrue(read.json()['order']['delivery_payment_locked'])
            posted = self.client.post(reverse('manual_order_edit', args=[order.pk]),
                json.dumps({'full_name': 'Інший одержувач', 'phone': '+380631111111',
                    'payment_preset': 'free', 'delivery_payment_mode': 'customer_prepaid',
                    'delivery_charge_amount': '999.00', 'items': [{'is_custom': True, 'title': 'Підміна', 'qty': 9}]}),
                content_type='application/json', HTTP_HOST='twocomms.shop')
        self.assertEqual(posted.status_code, 409, posted.content)
        self.assertEqual(Order.objects.filter(pk=order.pk).values().get(), original)
        self.assertEqual(list(order.items.order_by('pk').values()), original_items)
