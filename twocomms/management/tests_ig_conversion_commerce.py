"""Current commercial consent, actual payment mode and receipt regressions."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import IgCheckoutProposal, IgClient, InstagramBotMessage
from management.services.ig_checkout import CheckoutConfigurationError, create_or_update_proposal
from management.services.ig_checkout_policy import resolve_payment_policy, payment_choice_for_post
from orders.services.shipping_policy import (
    LEGACY_POLICY_VERSION, POLICY_VERSION, ShippingPolicyError,
    build_shipping_snapshot, loses_automatic_free_shipping, verify_shipping_snapshot,
)


class ActualPaymentShippingTests(SimpleTestCase):
    def snapshot(self, total='3000', **values):
        return build_shipping_snapshot(merchandise_total=total,
            evaluated_at='2026-10-19T12:00:00+00:00', **values)

    def test_threshold_cross_payment_choice_matrix(self):
        for total in ('2999', '3000', '3001'):
            for mode in ('online_full', 'prepay_200_cod', 'unknown'):
                with self.subTest(total=total, mode=mode):
                    row = self.snapshot(total, payment_mode=mode)
                    expected = Decimal(total) >= 3000 and mode == 'online_full'
                    self.assertEqual(row['eligible'], expected)
                    self.assertEqual(row['payer_type'], 'Sender' if expected else 'Recipient')
                    self.assertEqual(row['conditional'], Decimal(total) >= 3000 and mode == 'unknown')
                    self.assertEqual(verify_shipping_snapshot(row), row)

    def test_discount_and_mode_loss_require_fresh_consent(self):
        full = self.snapshot(payment_mode='online_full')
        prepay = self.snapshot(payment_mode='prepay_200')
        discounted = self.snapshot('2999', gross_total='3100', discount_amount='101', payment_mode='online_full')
        self.assertTrue(loses_automatic_free_shipping(full, prepay))
        self.assertTrue(loses_automatic_free_shipping(full, discounted))
        self.assertFalse(loses_automatic_free_shipping(prepay, full))
        self.assertFalse(self.snapshot()['eligible'])

    def test_frozen_legacy_terms_and_frozen_v2_threshold(self):
        legacy = self.snapshot(policy_version=LEGACY_POLICY_VERSION)
        self.assertTrue(legacy['eligible'])
        self.assertNotIn('payment_mode', legacy)
        full = self.snapshot(payment_mode='online_full')
        with override_settings(FREE_SHIPPING_THRESHOLD='9999'):
            self.assertEqual(verify_shipping_snapshot(legacy), legacy)
            self.assertEqual(verify_shipping_snapshot(full), full)
        with self.assertRaises(ShippingPolicyError):
            verify_shipping_snapshot({**full, 'payment_mode': 'prepay_200_cod'})

    def test_manual_receipt_does_not_invent_preparation_or_deadline(self):
        from management.services.ig_order_fulfillment import _message
        order = SimpleNamespace(pk=1, order_number='TEST-1', total_sum='950', discount_amount='0',
            payment_payload={}, payment_status='paid')
        for locale, forbidden in [('uk', ('Збираємо', 'пару годин')),
                ('ru', ('Собираем', 'пару часов')), ('en', ('packing', 'couple of hours'))]:
            text = _message('payment_confirmed', locale, order, '')
            self.assertIn('950.00', text)
            for phrase in forbidden:
                self.assertNotIn(phrase, text)


class NativeActualPaymentShippingTests(TestCase):
    def setUp(self):
        from management.models import IgCommercialEpisode, IgDeal
        from orders.models import Order, OrderItem, PaymentAttempt
        from orders.services.delivery_payment import threshold_shipping_cart_digest
        from storefront.models import Category, Product
        category = Category.objects.create(name='Native commerce', slug='native-conversion-commerce')
        product = Product.objects.create(category=category, title='Native shirt',
            slug='native-conversion-commerce-shirt', price='3100', status='published')
        customer = IgClient.get_or_create_for_sender('native-conversion-commerce')
        deal = IgDeal.objects.create(client=customer, amount='3000')
        episode = IgCommercialEpisode.objects.create(client=customer, deal=deal, sequence=1,
            materialization_key='native-conversion-commerce')
        self.proposal = IgCheckoutProposal.objects.create(client=customer, deal=deal,
            commercial_episode=episode, catalog_total='3100', quoted_total='3000',
            negotiated_discount='100', requested_payment_amount='3000', items_digest='a'*64)
        self.order = Order.objects.create(full_name='Native commerce buyer', phone='0500000000',
            city='Київ', np_office='1', total_sum='3100', discount_amount='100',
            pay_type='online_full', payment_status='paid')
        self.item = OrderItem.objects.create(order=self.order, product=product,
            title=product.title, qty=1, unit_price='3100', line_total='3100')
        self.cart = [{'product_id': product.pk, 'title': product.title,
            'qty': 1, 'unit_price': '3100.00', 'line_total': '3100.00'}]
        self.cart_digest = threshold_shipping_cart_digest(self.cart)
        self.attempt = PaymentAttempt.objects.create(fingerprint='c'*64, full_name=self.order.full_name,
            phone=self.order.phone, city=self.order.city, np_office='1', pay_type='online_full',
            gross_amount='3100', discount_amount='100', payable_amount='3000', payment_amount='3000',
            paid_amount='3000', order=self.order, status='converted',
            cart_snapshot={'checkout_surface': 'instagram_proposal', 'proposal_id': str(self.proposal.public_id),
                'cart': self.cart})
        self.proposal.payment_attempt = self.attempt
        self.proposal.save(update_fields=['payment_attempt'])
        self.seal()

    def seal(self, *, payment_mode='online_full', policy_version=POLICY_VERSION):
        from orders.services.delivery_payment import build_threshold_delivery_contract
        self.attempt.cart_snapshot['shipping'] = build_shipping_snapshot(merchandise_total='3000',
            gross_total='3100', discount_amount='100', cart_digest=self.cart_digest,
            payment_mode=payment_mode, policy_version=policy_version)
        contract = build_threshold_delivery_contract(self.attempt, proposal=self.proposal)
        self.attempt.cart_snapshot['threshold_delivery_contract'] = contract
        self.attempt.save(update_fields=['cart_snapshot', 'pay_type', 'payment_amount', 'paid_amount'])
        self.order.payment_payload = {'delivery_payment': deepcopy(contract)}
        self.order.save(update_fields=['payment_payload', 'pay_type', 'payment_status'])

    def snapshot(self):
        from orders.services.delivery_payment import delivery_payment_snapshot
        return delivery_payment_snapshot(self.order, item_rows=[self.item])

    def test_full_payment_is_required_at_fulfillment(self):
        self.assertEqual((self.snapshot()['valid'], self.snapshot()['payer_type']), (True, 'Sender'))
        self.order.payment_status = 'prepaid'
        self.attempt.paid_amount = '200'
        self.attempt.save(update_fields=['paid_amount'])
        row = self.snapshot()
        self.assertFalse(row['valid'])
        self.assertEqual(row['reason'], 'threshold_shipping_full_payment_unverified')

    def test_deposit_never_unlocks_free_delivery_even_after_remainder_paid(self):
        self.attempt.pay_type = self.order.pay_type = 'prepay_200'
        self.attempt.payment_amount = self.attempt.paid_amount = '200'
        self.order.payment_status = 'prepaid'
        self.seal(payment_mode='prepay_200')
        self.assertEqual((self.snapshot()['valid'], self.snapshot()['payer_type']), (True, 'Recipient'))
        self.attempt.paid_amount = '3000'
        self.attempt.save(update_fields=['paid_amount'])
        self.order.payment_status = 'paid'
        self.assertEqual(self.snapshot()['payer_type'], 'Recipient')

    def test_actual_mode_and_generation_must_match_frozen_mode(self):
        from orders.services.delivery_payment import build_threshold_delivery_contract, DeliveryPaymentError
        self.attempt.pay_type = 'prepay_200'
        with self.assertRaises(DeliveryPaymentError):
            build_threshold_delivery_contract(self.attempt, proposal=self.proposal)
        self.attempt.pay_type = 'online_full'
        generation = SimpleNamespace(pk=1, proposal_id=self.proposal.pk, payment_attempt_id=self.attempt.pk,
            proposal_revision=self.proposal.revision, payment_choice='prepay_200_cod')
        with self.assertRaises(DeliveryPaymentError):
            build_threshold_delivery_contract(self.attempt, proposal=self.proposal, generation=generation)

    def test_old_frozen_paid_contract_keeps_prior_shipping_terms(self):
        self.attempt.pay_type = self.order.pay_type = 'prepay_200'
        self.attempt.payment_amount = self.attempt.paid_amount = '200'
        self.order.payment_status = 'prepaid'
        self.seal(policy_version=LEGACY_POLICY_VERSION)
        with override_settings(FREE_SHIPPING_THRESHOLD='9999'):
            self.assertEqual((self.snapshot()['valid'], self.snapshot()['payer_type']), (True, 'Sender'))


@override_settings(IG_ASSISTED_CHECKOUT_V2='enforced', IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class CurrentCommercialConsentTests(TestCase):
    def setUp(self):
        from storefront.models import Category, Product, ProductFitOption
        self.customer = IgClient.get_or_create_for_sender('conversion-commerce-customer')
        category = Category.objects.create(name='Commerce', slug='conversion-commerce')
        self.product = Product.objects.create(category=category, title='Commerce shirt',
            slug='conversion-commerce-shirt', price='950', status='published')
        ProductFitOption.objects.create(product=self.product, code='classic', label='Classic', is_default=True)
        self.spec = {'product_id': self.product.pk, 'qty': 1, 'size': 'M', 'fit_option_code': 'classic'}

    def message(self, role, text):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role=role, text=text, status='done')

    def decide(self, row, **values):
        return resolve_payment_policy(client=self.customer, evidence_message_ids=[row.pk], **values)

    def offer(self):
        self.message('user', 'Боюсь, що розмір не підійде, це подарунок.')
        return self.message('model', 'За таблицею ширина M 54 см; можете оглянути на пошті. '
            'Передоплата 200 грн, залишок післяплатою. Передоплата зараховується у вартість речі. '
            'Якщо відмовитесь від звичайної посилки після огляду, ці 200 грн не повертаються. '
            'За післяплату перевізник стягує окрему комісію.')

    def test_short_yes_accepts_consulted_current_offer_and_persists_on_proposal(self):
        self.offer()
        accepted = self.message('user', 'Так, підходить')
        decision = self.decide(accepted)
        self.assertEqual(decision.policy, 'full_or_200_cod')
        self.assertEqual(decision.evidence_kind, 'offer_acceptance')
        proposal = create_or_update_proposal(client=self.customer, pay_type='online_full',
            item_specs=[self.spec], evidence={'message_ids': [accepted.pk]})
        self.assertEqual(payment_choice_for_post(proposal, 'prepay_200_cod'), 'prepay_200_cod')

    def test_direct_cod_request_does_not_need_repeated_number(self):
        row = self.message('user', 'Можна наложкою?')
        self.assertEqual(self.decide(row).policy, 'full_or_200_cod')
        self.assertEqual(self.decide(row, custom_print_full_only=True).policy, 'full_only')

    def test_arbitrary_historical_phrase_refusal_and_cart_change_do_not_unlock(self):
        self.message('model', 'Передоплата 200 грн, залишок післяплатою.')
        row = self.message('user', 'Так, підходить')
        self.assertEqual(self.decide(row).policy, 'full_only')
        self.offer()
        for text in ('Ні, не підходить', 'Так, а можна інший розмір?', 'Раніше було так, підходить'):
            row = self.message('user', text)
            self.assertEqual(self.decide(row).policy, 'full_only')
        self.offer()
        self.message('user', 'Хочу іншу річ')
        row = self.message('user', 'Так')
        self.assertEqual(self.decide(row).policy, 'full_only')

    def test_stale_offer_and_mutated_offer_are_not_payment_authority(self):
        offer = self.offer()
        row = self.message('user', 'Так')
        decision = self.decide(row)
        proposal = SimpleNamespace(assisted_checkout_v2=True, custom_print_full_only=False,
            payment_policy=decision.policy, payment_policy_evidence_message_id=row.pk,
            payment_policy_evidence_kind=decision.evidence_kind, payment_policy_evidence_digest=decision.evidence_digest,
            client=self.customer, client_id=self.customer.pk)
        offer.text = 'Тепер тільки повна оплата'
        offer.save(update_fields=['text'])
        with self.assertRaises(ValueError):
            payment_choice_for_post(proposal, 'prepay_200_cod')
        offer = self.offer()
        InstagramBotMessage.objects.filter(pk=offer.pk).update(created_at=timezone.now() - timedelta(hours=13))
        row = self.message('user', 'Так')
        self.assertEqual(self.decide(row).policy, 'full_only')

    def test_negated_historical_question_and_customer_no_cannot_authorize_discount(self):
        for text in ('Не 800 грн, а 950 грн', 'Не800грн, а950грн', 'Раніше ця футболка коштувала 800 грн?',
                'Можу запропонувати 800 грн?', 'Колись було 800 грн'):
            with self.subTest(text=text):
                offer = self.message('manager', text)
                row = self.message('user', 'Так, оформлюйте')
                with self.assertRaises(CheckoutConfigurationError) as caught:
                    create_or_update_proposal(client=self.customer, pay_type='online_full', item_specs=[self.spec],
                        negotiated_total='800', evidence={'message_ids': [offer.pk, row.pk]})
                self.assertEqual(caught.exception.code, 'invalid_price_evidence')
        offer = self.message('manager', 'Пропоную цю футболку за 800 грн')
        row = self.message('user', 'Так, не згоден')
        with self.assertRaises(CheckoutConfigurationError):
            create_or_update_proposal(client=self.customer, pay_type='online_full', item_specs=[self.spec],
                negotiated_total='800', evidence={'message_ids': [offer.pk, row.pk]})

    def test_positive_current_offer_and_acceptance_authorize_exact_total(self):
        offer = self.message('manager', 'Пропоную цю футболку за 800 грн')
        row = self.message('user', 'Так, оформлюйте')
        proposal = create_or_update_proposal(client=self.customer, pay_type='online_full', item_specs=[self.spec],
            negotiated_total='800', evidence={'message_ids': [offer.pk, row.pk]})
        self.assertEqual(proposal.quoted_total, Decimal('800'))

    def test_acceptance_can_repeat_exact_price_but_cannot_change_it_or_configuration(self):
        from management.services.ig_checkout_policy import accepts_current_commercial_offer
        self.assertTrue(accepts_current_commercial_offer('Домовились, 800 грн', amount=Decimal('800')))
        self.assertFalse(accepts_current_commercial_offer('Домовились, 700 грн', amount=Decimal('800')))
        offer = self.message('manager', 'Пропоную футболку розмір L за 800 грн')
        row = self.message('user', 'Так, оформлюйте')
        with self.assertRaises(CheckoutConfigurationError):
            create_or_update_proposal(client=self.customer, pay_type='online_full', item_specs=[self.spec],
                negotiated_total='800', evidence={'message_ids': [offer.pk, row.pk]})

    def test_short_acceptance_remains_bound_to_exact_sealed_customer_source(self):
        from management.services.ig_revision_checkout import _prepay_policy
        self.offer()
        row = self.message('user', 'Так, підходить')
        source = {'message_id': row.pk, 'role': 'user', 'text': row.text, 'quick_reply_payload': ''}
        decision, reasons = _prepay_policy(self.customer, [source])
        self.assertEqual(reasons, ())
        self.assertEqual(decision.evidence_kind, 'offer_acceptance')
        source['text'] = 'Ні'
        self.assertEqual(_prepay_policy(self.customer, [source])[1], ('checkout_payment_source_changed',))

    def test_selected_evidence_cannot_hide_new_basket_or_different_manager_offer(self):
        offer = self.message('manager', 'Пропоную цю футболку за 800 грн')
        self.message('user', 'Тоді іншу футболку L')
        row = self.message('user', 'Так, оформлюйте')
        with self.assertRaises(CheckoutConfigurationError):
            create_or_update_proposal(client=self.customer, pay_type='online_full', item_specs=[self.spec],
                negotiated_total='800', evidence={'message_ids': [offer.pk, row.pk]})

    def test_delivery_discount_and_wishlist_amounts_are_not_goods_price_authority(self):
        for text, attempted in (
                ('Ця футболка 950 грн, доставка 80 грн', '80'),
                ('Знижка 100 грн, ціна 850 грн', '100'),
                ('Доставка 80 грн', '80'),
                ('Знижка 100 грн', '100'),
                ('Передоплата 200 грн, ціна 850 грн', '200'),
                ('Футболка 850 грн і доставка 80 грн', '80'),
                ('Бажана ціна футболки 800 грн', '800'),
                ('Хочу одну футболку за 800 грн', '800'),
                ('Бюджет клієнта 800 грн', '800'),
                ('Бюджет клієнта на футболку 800 грн', '800'),
                ('За 2 футболки разом 800 грн', '800'),
                ('Футболка 800 грн або 850 грн', '800')):
            with self.subTest(text=text, attempted=attempted):
                offer = self.message('manager', text)
                accepted = self.message('user', 'Так, оформлюйте')
                with self.assertRaises(CheckoutConfigurationError) as caught:
                    create_or_update_proposal(client=self.customer, pay_type='online_full', item_specs=[self.spec],
                        negotiated_total=attempted, evidence={'message_ids': [offer.pk, accepted.pk]})
                self.assertEqual(caught.exception.code, 'invalid_price_evidence')

    def test_clear_goods_quote_is_separate_from_discount_or_delivery_amount(self):
        self.product.price = '1000'
        self.product.save(update_fields=['price'])
        for index, (text, expected) in enumerate((
                ('Ця футболка 950 грн, доставка 80 грн', '950'),
                ('Знижка 100 грн, ціна 850 грн', '850'),
                ('Доставка 80 грн, футболка 850 грн', '850'))):
            with self.subTest(text=text):
                self.customer = IgClient.get_or_create_for_sender(f'conversion-goods-price-{index}')
                offer = self.message('manager', text)
                accepted = self.message('user', 'Так, оформлюйте')
                proposal = create_or_update_proposal(client=self.customer, item_specs=[self.spec],
                    negotiated_total=expected, evidence={'message_ids': [offer.pk, accepted.pk]},
                    pay_type='online_full')
                self.assertEqual(proposal.quoted_total, Decimal(expected))

    def test_consistent_unit_and_total_quotes_bind_current_quantity(self):
        offer = self.message('manager', 'За 2 футболки: кожна 850 грн; разом 1700 грн')
        accepted = self.message('user', 'Так, оформлюйте')
        proposal = create_or_update_proposal(client=self.customer, item_specs=[{**self.spec, 'qty': 2}],
            negotiated_total='1700', evidence={'message_ids': [offer.pk, accepted.pk]}, pay_type='online_full')
        self.assertEqual(proposal.quoted_total, Decimal('1700'))

    def test_explicit_per_item_quote_preserves_approved_unit_pricing(self):
        offer = self.message('manager', 'За одну футболку 850 грн')
        accepted = self.message('user', 'Так, оформлюйте')
        proposal = create_or_update_proposal(client=self.customer, item_specs=[{**self.spec, 'qty': 2}],
            negotiated_total='1700', evidence={'message_ids': [offer.pk, accepted.pk]}, pay_type='online_full')
        self.assertEqual(proposal.quoted_total, Decimal('1700'))

    def test_simple_unambiguous_manager_price_is_still_an_offer(self):
        offer = self.message('manager', '800 грн')
        accepted = self.message('user', 'Так, оформлюйте')
        proposal = create_or_update_proposal(client=self.customer, item_specs=[self.spec], negotiated_total='800',
            evidence={'message_ids': [offer.pk, accepted.pk]}, pay_type='online_full')
        self.assertEqual(proposal.quoted_total, Decimal('800'))
