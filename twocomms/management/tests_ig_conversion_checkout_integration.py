"""Native invoice boundaries for combined rewards and actual delivery choices."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.models import IgCheckoutAccessToken, InstagramBotMessage
from management.services.ig_checkout import create_or_update_proposal
from management import tests_ig_checkout_generation_runtime as runtime_fixtures
from management import tests_ig_checkout_service as configuration_fixtures
from orders.models import PaymentAttempt
from orders.tests import test_promo_atomicity as webshop_fixtures
from orders import tests_shipping_policy as shipping_fixtures
from storefront.models import PromoCode, UserPromoCode


@override_settings(IG_ASSISTED_CHECKOUT_V2='enforced', IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class HostedCombinedRewardsTests(TestCase):
    _valid_item = configuration_fixtures.InstagramCheckoutConfigurationTests._valid_item
    _delivery_payload = runtime_fixtures.CheckoutGenerationRuntimeTests._delivery_payload

    def setUp(self):
        cache.clear()
        configuration_fixtures.InstagramCheckoutConfigurationTests.setUp(self)
        self.ig_client = self.client
        self.client = Client()
        self.user = get_user_model().objects.create_user(username='combined-checkout-owner')
        self.client.force_login(self.user)
        self.codes = [PromoCode.objects.create(code=code, discount_type=kind, discount_value=value,
            one_time_per_user=True, max_uses=20) for code, kind, value in (
                ('FIXED100', 'fixed', 100), ('REWARD10A', 'percentage', 10), ('REWARD10B', 'percentage', 10))]
        self.expiry = timezone.now() + timedelta(days=7)
        UserPromoCode.objects.create(user=self.user, promo_code=self.codes[1], survey_key='earned-reward-1',
            source='points_reward', expires_at=self.expiry)
        message = InstagramBotMessage.objects.create(client=self.ig_client, sender_id=self.ig_client.igsid,
            role='user', text='Чи можна 200 грн передоплати, а решту післяплатою?')
        self.proposal = create_or_update_proposal(client=self.ig_client, pay_type='online_full',
            item_specs=[self._valid_item(qty=4)], allow_promo=True, evidence={'message_ids': [message.pk]})
        self.raw, self.token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        opened = self.client.get(reverse('ig_checkout_token_entry', kwargs={'token': self.raw}))
        self.url = opened['Location']
        self._hosted_revision_payload = runtime_fixtures.displayed_revision_payload(self.client.get(self.url))

    def preview(self, **changes):
        payload = {**self._hosted_revision_payload, 'action': 'promo_preview',
            'promo_code': 'REWARD10B, FIXED100, REWARD10A', 'payment_choice': 'online_full', **changes}
        return self.client.post(self.url, payload, HTTP_ACCEPT='application/json')

    def test_preview_and_invoice_share_composition_and_exact_loss_consent(self):
        preview = self.preview()
        self.assertEqual(preview.status_code, 200, preview.content)
        state = preview.json()['cart_state']
        self.assertEqual(state['total'], '2997.00')
        self.assertEqual(state['discount'], '803.00')
        self.assertEqual(state['promo_codes'], ['FIXED100', 'REWARD10A', 'REWARD10B'])
        self.assertEqual(state['promo_details'][1]['expires_at'], self.expiry.isoformat())
        self.assertTrue(state['free_delivery']['ack_required'])
        self.assertFalse(state['free_delivery']['eligible'])
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertEqual(list(PromoCode.objects.values_list('current_uses', flat=True)), [0, 0, 0])
        provider = Mock(return_value={'invoiceId': 'combined-native', 'pageUrl': 'https://pay.example/combined-native'})
        payload = self._delivery_payload(promo_code=', '.join(state['promo_codes']),
            payment_choice='online_full', delivery_policy_digest=state['free_delivery']['ack_digest'])
        with patch('storefront.views.monobank._monobank_api_request', provider):
            refused = self.client.post(self.url, payload, HTTP_ACCEPT='application/json')
            self.assertEqual(refused.json()['error'], 'delivery_policy_ack')
            self.assertFalse(PaymentAttempt.objects.exists())
            payload['delivery_policy_ack'] = '1'
            accepted = self.client.post(self.url, payload, HTTP_ACCEPT='application/json')
        self.assertEqual(accepted.status_code, 200, accepted.content)
        provider.assert_called_once()
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.payable_amount, Decimal('2997'))
        self.assertEqual(attempt.discount_amount, Decimal('803'))
        reservations = attempt.event_state['promo_reservations']
        self.assertEqual([row['promo_id'] for row in reservations], [promo.pk for promo in self.codes])
        self.assertEqual(len({row['reservation_generation'] for row in reservations}), 1)
        self.assertEqual(attempt.cart_snapshot['shipping']['payment_mode'], 'online_full')
        self.assertEqual(attempt.cart_snapshot['shipping']['payer_type'], 'Recipient')
        self.assertEqual(attempt.cart_snapshot['delivery_policy_ack']['digest'], state['free_delivery']['ack_digest'])
        self.assertEqual(sum(Decimal(row['discount_amount']) for row in reservations), attempt.discount_amount)

    def test_mode_preview_removes_free_and_return_to_full_clears_loss_ack(self):
        deposit = self.preview(promo_code='', payment_choice='prepay_200_cod')
        self.assertEqual(deposit.status_code, 200, deposit.content)
        state = deposit.json()['cart_state']
        self.assertFalse(state['free_delivery']['eligible'])
        self.assertEqual(state['free_delivery']['progress'], 100)
        self.assertTrue(state['free_delivery']['ack_required'])
        self.assertEqual(state['charge_now'], '200.00')
        full = self.preview(promo_code='', payment_choice='online_full').json()['cart_state']
        self.assertTrue(full['free_delivery']['eligible'])
        self.assertFalse(full['free_delivery']['ack_required'])
        self.assertNotEqual(full['free_delivery']['ack_digest'], state['free_delivery']['ack_digest'])

    def test_deposit_at_or_below_200_is_full_in_preview_invoice_and_shipping(self):
        self.shirt.price = Decimal('150.00')
        self.shirt.save(update_fields=['price'])
        self.proposal = create_or_update_proposal(client=self.ig_client, pay_type='online_full',
            item_specs=[self._valid_item(qty=1)], allow_promo=True,
            evidence={'message_ids': [self.proposal.payment_policy_evidence_message_id]})
        self.raw, self.token = IgCheckoutAccessToken.issue(proposal=self.proposal)
        opened = self.client.get(reverse('ig_checkout_token_entry', kwargs={'token': self.raw}))
        self.url = opened['Location']
        self._hosted_revision_payload = runtime_fixtures.displayed_revision_payload(self.client.get(self.url))
        preview = self.preview(promo_code='', payment_choice='prepay_200_cod')
        self.assertEqual(preview.status_code, 200, preview.content)
        state = preview.json()['cart_state']
        self.assertEqual(state['total'], '150.00')
        self.assertEqual(state['charge_now'], '150.00')
        self.assertFalse(state['is_prepayment'])
        self.assertEqual(state['free_delivery']['payment_mode'], 'online_full')
        with patch('storefront.views.monobank._monobank_api_request', return_value={
                'invoiceId': 'small-full', 'pageUrl': 'https://pay.example/small-full'}):
            invoice = self.client.post(self.url, self._delivery_payload(promo_code='', payment_choice='prepay_200_cod',
                delivery_policy_digest=state['free_delivery']['ack_digest']), HTTP_ACCEPT='application/json')
        self.assertEqual(invoice.status_code, 200, invoice.content)
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.pay_type, 'online_full')
        self.assertEqual(attempt.cart_snapshot['shipping']['payment_mode'], 'online_full')

    def test_duplicate_foreign_expired_rights_fail_without_reserving(self):
        self.assertEqual(self.preview(promo_code='REWARD10A, REWARD10A').status_code, 400)
        other = get_user_model().objects.create_user(username='other-reward-owner')
        self.client.force_login(other)
        self.client.get(reverse('ig_checkout_token_entry', kwargs={'token': self.raw}))
        self.assertEqual(self.preview(promo_code='REWARD10A').status_code, 400)
        self.client.force_login(self.user)
        self.client.get(reverse('ig_checkout_token_entry', kwargs={'token': self.raw}))
        UserPromoCode.objects.filter(promo_code=self.codes[1]).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.preview(promo_code='REWARD10A').status_code, 400)
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertEqual(sum(PromoCode.objects.values_list('current_uses', flat=True)), 0)

    def test_preview_requires_link_grant_and_exact_revision(self):
        self.assertEqual(Client().post(self.url, {'action': 'promo_preview'}).status_code, 404)
        self.assertEqual(self.preview(expected_revision=self.proposal.revision + 1).json()['error'], 'revision_changed')


class WebshopCombinedRewardsTests(TestCase):
    setUp = webshop_fixtures.PromoAtomicityTests.setUp
    _set_cart_promo = webshop_fixtures.PromoAtomicityTests._set_cart_promo
    _invoice_payload = webshop_fixtures.PromoAtomicityTests._invoice_payload
    _post_invoice = webshop_fixtures.PromoAtomicityTests._post_invoice

    def test_cart_and_invoice_share_the_same_discounted_batch(self):
        fixed = PromoCode.objects.create(code='CARTFIXED100', discount_type='fixed', discount_value=100)
        percent = PromoCode.objects.create(code='CARTPERCENT10', discount_type='percentage', discount_value=10)
        self._set_cart_promo(fixed)
        applied = self.client.post(reverse('apply_promo_code'), {'promo_code': 'CARTPERCENT10, CARTFIXED100'})
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(applied.json()['total'], 720.0)
        self.assertEqual(self.client.session['promo_code_ids'], [fixed.pk, percent.pk])
        provider = Mock(return_value={'invoiceId': 'webshop-combined', 'pageUrl': 'https://pay.example/webshop-combined'})
        invoice = self._post_invoice(provider)
        self.assertEqual(invoice.status_code, 200, invoice.content)
        provider.assert_called_once()
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.payable_amount, Decimal('720'))
        self.assertEqual(attempt.discount_amount, Decimal('180'))
        self.assertEqual([row['promo_id'] for row in attempt.event_state['promo_reservations']], [fixed.pk, percent.pk])

    def test_threshold_progress_is_separate_from_unknown_payment_eligibility(self):
        from storefront.views.cart import _cart_shipping_metadata
        partial = _cart_shipping_metadata(Decimal('200'))
        self.assertEqual(partial['shipping_progress'], 7)
        target = _cart_shipping_metadata(Decimal('3000'))
        self.assertEqual(target['shipping_progress'], 100)
        self.assertFalse(target['shipping_free'])
        self.assertTrue(target['shipping_conditional'])
        self.assertIn('повної оплати', target['shipping_summary'])

    def test_expired_right_clears_entire_batch_on_cart_reprice(self):
        from storefront.views.cart import _session_cart_promos
        from django.test import RequestFactory
        fixed = PromoCode.objects.create(code='CARTOWNED', discount_type='fixed', discount_value=100)
        other = PromoCode.objects.create(code='CARTOTHER', discount_type='percentage', discount_value=10)
        UserPromoCode.objects.create(user=self.user, promo_code=fixed, survey_key='expired', expires_at=timezone.now() - timedelta(seconds=1))
        request = RequestFactory().get('/cart/')
        request.user, request.session = self.user, {'promo_code_id': fixed.pk, 'promo_code_ids': [fixed.pk, other.pk]}
        primary, discount, promos = _session_cart_promos(request, Decimal('900'))
        self.assertIsNone(primary)
        self.assertEqual(discount, 0)
        self.assertEqual(promos, ())
        self.assertNotIn('promo_code_ids', request.session)


class HistoricalShippingCompatibilityTests(TestCase):
    setUp = shipping_fixtures.NativeThresholdOrderTests.setUp
    seal = shipping_fixtures.NativeThresholdOrderTests.seal

    def test_literal_frozen_v1_floor_progress_retains_native_shipment_authority(self):
        from orders.services.shipping_policy import verify_shipping_snapshot
        from orders.services.delivery_payment import threshold_shipping_cart_digest, delivery_payment_snapshot
        from orders.nova_poshta_documents import build_order_payment_snapshot
        cart = [{'product_id': self.item.product_id, 'title': self.item.title,
            'qty': 1, 'unit_price': '1550.00', 'line_total': '1550.00'}]
        # This is a historical persisted DTO, not output from today's builder.
        literal = {'schema': 'shipping-policy.v1', 'policy_version': 'threshold-after-discounts.v1',
            'basis': 'approved_merchandise_after_discounts', 'threshold': '3000.00',
            'basis_total': '1550.00', 'gross_total': '1550.00', 'discount_amount': '0.00',
            'currency': 'UAH', 'cart_digest': threshold_shipping_cart_digest(cart), 'owner': {},
            'evaluated_at': '2026-10-14T11:00:00+00:00', 'remaining': '1450.00',
            'progress': 51, 'eligible': False, 'automatic': True, 'authority': 'automatic_threshold',
            'status': 'remaining', 'mode': 'carrier_recipient', 'payer_type': 'Recipient'}
        self.assertEqual(verify_shipping_snapshot(literal), literal)
        self.proposal.catalog_total = self.proposal.quoted_total = Decimal('1550.00')
        self.proposal.negotiated_discount = Decimal('0.00')
        self.proposal.save(update_fields=['catalog_total', 'quoted_total', 'negotiated_discount'])
        self.item.unit_price = self.item.line_total = Decimal('1550.00')
        self.item.save(update_fields=['unit_price', 'line_total'])
        self.order.total_sum, self.order.discount_amount = Decimal('1550.00'), Decimal('0.00')
        self.order.save(update_fields=['total_sum', 'discount_amount'])
        self.attempt.gross_amount = self.attempt.payable_amount = Decimal('1550.00')
        self.attempt.discount_amount = Decimal('0.00')
        self.attempt.cart_snapshot = {'checkout_surface': 'instagram_proposal',
            'proposal_id': str(self.proposal.public_id), 'cart': cart, 'shipping': literal}
        self.attempt.save(update_fields=['gross_amount', 'payable_amount', 'discount_amount', 'cart_snapshot'])
        self.seal()
        delivery = delivery_payment_snapshot(self.order, item_rows=list(self.order.items.all()))
        self.assertTrue(delivery['valid'], delivery)
        self.assertFalse(delivery['requires_manual'])
        self.assertEqual(delivery['payer_type'], 'Recipient')
        shipment = build_order_payment_snapshot(self.order)
        self.assertTrue(shipment['delivery_payment_contract_valid'])
        self.assertFalse(shipment['delivery_payment_requires_manual'])
        self.assertEqual(shipment['delivery_payer_type'], 'Recipient')
