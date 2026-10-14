"""Actual native selected quote readback through the bearer hosted checkout."""
from decimal import Decimal
from copy import deepcopy
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture
from orders.models import Order, PaymentAttempt


@override_settings(ROOT_URLCONF='twocomms.urls_management', SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2='off', IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0)
class NativeSelectedHostedShippingTests(PartialCommerceFixture, TransactionTestCase):
    def post(self, payload):
        # Boundary cases are genuine manager draft inputs; all source capture,
        # approval, delivered choice and capability verification remain native.
        ceiling = getattr(self, 'customer_ceiling_input', None)
        if ceiling is not None and payload.get('action') == 'draft':
            payload = deepcopy(payload)
            payload['shipping_terms']['now']['customer_maximum'] = ceiling
        return super().post(payload)

    def _assert_native_ceiling_readback(self, ceiling):
        self.customer_ceiling_input = ceiling
        data = self.selected_case()
        result = self.prepare_selection(data['selection'])
        with override_settings(ROOT_URLCONF='twocomms.urls'), \
            patch('storefront.views.monobank._monobank_api_request') as bank:
            entry = self.client.get(result['checkout_url'], HTTP_HOST='twocomms.shop')
            self.assertEqual(entry.status_code, 302, entry.content)
            response = self.client.get(reverse('ig_checkout_proposal', kwargs={'proposal_id': result['proposal'].public_id}),
                {'lang': 'en'}, HTTP_HOST='twocomms.shop')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.context['assisted_selected_shipping']['customer_maximum'], ceiling)
        self.assertEqual(response.context['proposal']['charge_now'], '1590.00')
        self.assertContains(response, ceiling + ' UAH')
        bank.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())

    def test_explicit_zero_customer_ceiling_is_shown_without_inventing_free_carrier_quote(self):
        self._assert_native_ceiling_readback('0.00')

    def test_maximum_accepted_customer_ceiling_does_not_become_goods_invoice_amount(self):
        self._assert_native_ceiling_readback('1000000.00')

    def test_actual_selected_offer_get_localizes_accepted_cap_and_unpaid_remainder_without_charging_either(self):
        data = self.selected_case()
        result = self.prepare_selection(data['selection'])
        proposal = result['proposal']
        expected = {'shipping_scope': 'now', 'payer': 'customer', 'customer_maximum': '120.00',
            'currency': 'UAH', 'deferred_count': 1}
        localized = {'uk': ('Погоджені умови доставки', 'ще не оплачена'),
            'ru': ('Согласованные условия доставки', 'ещё не оплачены'),
            'en': ('Agreed delivery terms', 'have not been paid for')}
        with override_settings(ROOT_URLCONF='twocomms.urls'), \
            patch('storefront.views.monobank._monobank_api_request') as bank, \
            patch('management.services.instagram_bot._provider_http') as instagram:
            entry = self.client.get(result['checkout_url'], HTTP_HOST='twocomms.shop')
            self.assertEqual(entry.status_code, 302, entry.content)
            for locale, phrases in localized.items():
                with self.subTest(locale=locale):
                    response = self.client.get(reverse('ig_checkout_proposal', kwargs={'proposal_id': proposal.public_id}),
                        {'lang': locale}, HTTP_HOST='twocomms.shop')
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.context['html_lang'], locale)
                    self.assertEqual(response.context['assisted_selected_shipping'], expected)
                    self.assertEqual(response.context['proposal']['charge_now'], '1590.00')
                    self.assertContains(response, '120.00 UAH')
                    for phrase in phrases:
                        self.assertContains(response, phrase)
                    self.assertContains(response, 'name="assisted_terms_accepted"')
                    self.assertNotContains(response, 'Оливкове худі')
        bank.assert_not_called()
        instagram.assert_not_called()
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())
        proposal.refresh_from_db()
        self.assertEqual(proposal.quoted_total, Decimal('1590.00'))

    def test_selected_historical_readback_retains_original_now_terms_after_real_paid_remainder_checkout(self):
        data = self.selected_case()
        first = self.prepare_selection(data['selection'])
        self.pay_prepared(first)
        self.paid_remainder_case(data['request'])
        with override_settings(ROOT_URLCONF='twocomms.urls'):
            response = self.client.get(reverse('ig_checkout_proposal', kwargs={'proposal_id': first['proposal'].public_id}),
                {'lang': 'en'}, HTTP_HOST='twocomms.shop')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.context['assisted_selected_shipping'], {'shipping_scope': 'now',
            'payer': 'customer', 'customer_maximum': '120.00', 'currency': 'UAH', 'deferred_count': 1})
        self.assertEqual(response.context['checkout_state'], 'paid')


@override_settings(ROOT_URLCONF='twocomms.urls', SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2='off', IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=0)
class NativeFullHostedShippingCompatibilityTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def test_full39_native_offer_does_not_gain_selected_cap_or_deferred_claims(self):
        result = self._custom_proposal()
        entry = self.client.get(result['checkout_url'], HTTP_HOST='twocomms.shop')
        self.assertEqual(entry.status_code, 302, entry.content)
        response = self.client.get(reverse('ig_checkout_proposal', kwargs={'proposal_id': result['proposal'].public_id}),
            {'lang': 'en'}, HTTP_HOST='twocomms.shop')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.context['assisted_selected_shipping'])
        self.assertNotContains(response, 'id="ig-assisted-shipping-title"')
        self.assertContains(response, 'name="assisted_terms_accepted"')
        self.assertFalse(PaymentAttempt.objects.exists())
        self.assertFalse(Order.objects.exists())
