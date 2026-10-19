"""Threshold boundary and native paid-Order ownership regressions."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from orders.services.shipping_policy import (
    LEGACY_POLICY_VERSION, ShippingPolicyError, build_shipping_snapshot, loses_automatic_free_shipping,
    verify_shipping_snapshot,
)
from orders.services.delivery_payment import (
    build_delivery_payment_contract, build_threshold_delivery_contract,
    delivery_payment_snapshot, threshold_shipping_cart_digest,
)


class ShippingPolicyArithmeticTests(SimpleTestCase):
    def snapshot(self, net, **values):
        return build_shipping_snapshot(merchandise_total=net,
            evaluated_at='2026-10-14T11:00:00+00:00', payment_mode='online_full', **values)

    def test_inclusive_threshold_cents_and_progress_do_not_claim_early_success(self):
        for net, eligible, remaining in [('2999.99', False, '0.01'),
                ('3000.00', True, '0.00'), ('3000.01', True, '0.00')]:
            with self.subTest(net=net):
                row = self.snapshot(net, threshold='3000.00')
                self.assertEqual((row['eligible'], row['remaining']), (eligible, remaining))
                self.assertEqual(row['payer_type'], 'Sender' if eligible else 'Recipient')
                self.assertEqual(row['progress'], 100 if eligible else 99)

    def test_discounts_use_whole_goods_total_not_requested_deposit(self):
        self.assertTrue(self.snapshot('3000', gross_total='3100', discount_amount='100')['eligible'])
        self.assertFalse(self.snapshot('2999', gross_total='3100', discount_amount='101')['eligible'])
        self.assertFalse(self.snapshot('200')['eligible'])
        with self.assertRaises(ShippingPolicyError):
            self.snapshot('3000', gross_total='3000', discount_amount='100')

    def test_unapproved_and_carrier_money_are_not_a_policy_input(self):
        # Writer supplies the approved goods basis, not combined estimates.
        row = self.snapshot('2800', gross_total='3000', discount_amount='200')
        self.assertEqual((row['eligible'], row['remaining']), (False, '200.00'))
        for invalid in (True, '-1', 'NaN', 'Infinity', '3000.001'):
            with self.subTest(invalid=invalid), self.assertRaises(ShippingPolicyError):
                self.snapshot(invalid)

    def test_frozen_setting_and_tampered_snapshot(self):
        row = self.snapshot('3000', threshold='3000')
        with override_settings(FREE_SHIPPING_THRESHOLD='4000'):
            self.assertTrue(verify_shipping_snapshot(row)['eligible'])
            self.assertFalse(self.snapshot('3000')['eligible'])
        with self.assertRaises(ShippingPolicyError):
            verify_shipping_snapshot({**row, 'payer_type': 'Recipient'})

    def test_loss_requires_automatic_origin(self):
        before, after = self.snapshot('3000'), self.snapshot('2999')
        self.assertTrue(loses_automatic_free_shipping(before, after))
        self.assertFalse(loses_automatic_free_shipping({**before, 'authority': 'manual_manager'}, after))
        self.assertFalse(loses_automatic_free_shipping(after, before))


class NativeThresholdOrderTests(TestCase):
    def setUp(self):
        from management.models import IgClient, IgDeal, IgCommercialEpisode, IgCheckoutProposal
        from orders.models import Order, OrderItem, PaymentAttempt
        from storefront.models import Category, Product
        category = Category.objects.create(name='Threshold goods', slug='threshold-goods')
        product = Product.objects.create(title='Threshold shirt', slug='threshold-shirt',
            category=category, price='3100', status='published')
        client = IgClient.get_or_create_for_sender('threshold-native-order')
        deal = IgDeal.objects.create(client=client, amount='3000')
        episode = IgCommercialEpisode.objects.create(client=client, deal=deal,
            sequence=1, materialization_key='threshold-native-episode')
        self.proposal = IgCheckoutProposal.objects.create(client=client, deal=deal,
            commercial_episode=episode, catalog_total='3100', quoted_total='3000',
            negotiated_discount='100', requested_payment_amount='200', items_digest='a'*64)
        self.order = Order.objects.create(full_name='Threshold buyer', phone='0500000000',
            city='Київ', np_office='1', total_sum='3100', discount_amount='100',
            pay_type='prepay_200', payment_status='prepaid')
        self.item = OrderItem.objects.create(order=self.order, product=product,
            title=product.title, qty=1, unit_price='3100', line_total='3100')
        cart = [{'product_id': product.pk, 'title': product.title,
            'qty': 1, 'unit_price': '3100.00', 'line_total': '3100.00'}]
        shipping = build_shipping_snapshot(merchandise_total='3000', gross_total='3100',
            discount_amount='100', cart_digest=threshold_shipping_cart_digest(cart),
            policy_version=LEGACY_POLICY_VERSION)
        self.attempt = PaymentAttempt.objects.create(fingerprint='b'*64,
            full_name=self.order.full_name, phone=self.order.phone, city=self.order.city,
            np_office='1', pay_type='prepay_200', gross_amount='3100', discount_amount='100',
            payable_amount='3000', payment_amount='200', paid_amount='200', order=self.order,
            status='converted', cart_snapshot={'checkout_surface': 'instagram_proposal',
                'proposal_id': str(self.proposal.public_id), 'cart': cart, 'shipping': shipping})
        self.proposal.payment_attempt = self.attempt
        self.proposal.save(update_fields=['payment_attempt'])
        self.seal()

    def seal(self, generation=None):
        contract = build_threshold_delivery_contract(self.attempt,
            proposal=self.proposal, generation=generation)
        self.attempt.cart_snapshot['threshold_delivery_contract'] = contract
        self.attempt.save(update_fields=['cart_snapshot'])
        self.order.payment_payload = {'delivery_payment': deepcopy(contract)}
        self.order.save(update_fields=['payment_payload'])
        return contract

    def snapshot(self):
        return delivery_payment_snapshot(self.order, item_rows=list(self.order.items.all()))

    def test_exact_threshold_sender_preserves_only_200_goods_payment(self):
        row = self.snapshot()
        self.assertEqual((row['valid'], row['authority'], row['payer_type']),
            (True, 'automatic_threshold', 'Sender'))
        self.assertEqual(row['merchandise_total'], Decimal('3000'))
        self.assertEqual(row['delivery_amount'], Decimal('0'))
        self.assertFalse(row['delivery_prepaid'])
        self.attempt.refresh_from_db(fields=['paid_amount'])
        self.assertEqual(self.attempt.paid_amount, Decimal('200'))

    def test_paid_payer_stays_frozen_after_settings_change(self):
        with override_settings(FREE_SHIPPING_THRESHOLD='9999'):
            self.assertEqual((self.snapshot()['valid'], self.snapshot()['payer_type']), (True, 'Sender'))

    def test_order_payload_copy_has_no_native_reverse_ownership(self):
        from orders.models import Order
        copied = Order.objects.create(full_name='Other buyer', phone='0500000001',
            city='Київ', np_office='1', total_sum='3100', discount_amount='100',
            payment_payload=deepcopy(self.order.payment_payload))
        row = delivery_payment_snapshot(copied, item_rows=[self.item])
        self.assertFalse(row['valid'])
        self.assertEqual(row['reason'], 'threshold_shipping_native_binding_changed')

    def test_same_gross_changed_quantity_and_changed_discount_fail_closed(self):
        self.item.qty, self.item.unit_price = 2, Decimal('1550')
        self.item.save(update_fields=['qty', 'unit_price'])
        self.assertFalse(self.snapshot()['valid'])
        self.item.qty, self.item.unit_price = 1, Decimal('3100')
        self.item.save(update_fields=['qty', 'unit_price'])
        self.order.discount_amount = Decimal('101')
        self.assertFalse(self.snapshot()['valid'])

    def test_same_price_changed_configuration_cannot_reuse_promotion_proof(self):
        self.item.option_values = {'material': 'another-material'}
        self.item.save(update_fields=['option_values'])
        self.assertFalse(self.snapshot()['valid'])

    def test_changed_native_attempt_snapshot_or_missing_items_require_review(self):
        row = delivery_payment_snapshot(self.order)
        self.assertFalse(row['valid'])
        self.assertTrue(row['requires_manual'])
        snapshot = deepcopy(self.attempt.cart_snapshot)
        snapshot['threshold_delivery_contract']['shipping']['threshold'] = '2000.00'
        self.attempt.cart_snapshot = snapshot
        self.attempt.save(update_fields=['cart_snapshot'])
        self.assertEqual(self.snapshot()['reason'], 'threshold_shipping_native_binding_changed')

    def test_generation_must_be_the_native_paid_winner(self):
        from management.models import IgCheckoutInvoiceGeneration
        generation = IgCheckoutInvoiceGeneration.objects.create(proposal=self.proposal,
            generation=1, series_key='threshold-series', proposal_revision=self.proposal.revision,
            payment_attempt=self.attempt, payment_amount='200', state='paid_winner',
            winner_slot=1, provider_call_token='c'*64, expires_at=timezone.now()+timedelta(hours=1))
        self.proposal.winner_invoice_generation = generation
        self.proposal.save(update_fields=['winner_invoice_generation'])
        self.seal(generation)
        self.assertTrue(self.snapshot()['valid'])
        generation.winner_slot = None
        generation.save(update_fields=['winner_slot'])
        self.assertEqual(self.snapshot()['reason'], 'threshold_shipping_winner_changed')

    def test_explicit_manager_free_is_distinct_and_remains_below_threshold(self):
        self.order.total_sum, self.order.discount_amount = Decimal('850'), Decimal('0')
        self.order.payment_payload = {'delivery_payment': build_delivery_payment_contract(
            mode='merchant_free', merchandise_total='850', actor_id=41)}
        row = delivery_payment_snapshot(self.order)
        self.assertTrue(row['valid'])
        self.assertEqual(row['authority'], 'manual_manager')
        self.assertNotIn('shipping', row)
