import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from orders.models import PaymentAttempt
from product_catalog.models import ProductOptionProfile, MerchCollection, ProductMerchCollection
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, PromoCode
from storefront.services.brigade_commerce import calculate_brigade_cart_pricing


class GarmentBundleTests(TestCase):
    def setUp(self):
        cache.clear()
        for target in ('storefront.signals.generate_google_merchant_feed_task.apply_async', 'storefront.signals.enqueue_indexnow_urls'):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        tees = Category.objects.create(name='Tees', slug='tshirts')
        hoodies = Category.objects.create(name='Hoodies', slug='hoodie')
        self.tee = Product.objects.create(pk=4, title='Baby tee', slug='my-little-baby', category=tees, price=1100, status='published')
        self.hoodie = Product.objects.create(pk=5, title='Baby hoodie', slug='my-little-baby-hd', category=hoodies, price=1995, status='published')
        self.other = Product.objects.create(pk=13, title='Money tee', slug='business-money', category=tees, price=1100, status='published')
        self.other_hoodie = Product.objects.create(pk=14, title='Money hoodie', slug='business-money-hd', category=hoodies, price=1850, status='published')

    def row(self, product, qty=1, **kwargs):
        return {'product_id': product.pk, 'qty': qty, 'size': 'M', **kwargs}

    def quote(self, tees=1, hoodies=1):
        return calculate_brigade_cart_pricing({'t': self.row(self.tee, tees), 'h': self.row(self.hoodie, hoodies)})

    def post(self, payload):
        self.client.session  # The chooser GET establishes this cookie first.
        with patch('storefront.views.cart.record_add_to_cart'):
            return self.client.post(reverse('cart_bundle_add'), json.dumps(payload), content_type='application/json')

    def payload(self, **kwargs):
        return {'request_id': 'bundle-request-1', 'mode': 'pair', 'hoodie': self.row(self.hoodie), 'tee': self.row(self.tee), **kwargs}

    def test_same_other_and_actual_hoodie_prices(self):
        self.assertEqual(self.quote().subtotal, Decimal('2795'))
        self.assertEqual(self.quote().lines['h'].line_total, Decimal('1945'))
        self.assertEqual(self.quote().public_metadata()['bundle_discount_total'], 300)
        quote = calculate_brigade_cart_pricing({'t': self.row(self.tee), 'h': self.row(self.other_hoodie)})
        self.assertEqual(quote.subtotal, Decimal('2700'))
        self.assertEqual(quote.lines['h'].line_total, Decimal('1800'))
        pair = quote.bundle_allocations[0].public_metadata()
        self.assertEqual(pair['hoodie_discount_amount'], 50)
        self.assertEqual(pair['tee_discount_amount'], 200)
        self.assertEqual(quote.bundle_allocations[0].kind, 'other_design')

    def test_exact_partial_quantity_and_promotional_scope(self):
        quote = self.quote(tees=3)
        self.assertEqual(quote.subtotal, Decimal('4995'))
        self.assertEqual(quote.lines['t'].line_total, Decimal('3050'))
        self.assertEqual(quote.lines['t'].snapshot_parts(), [(1, Decimal('850')), (2, Decimal('1100'))])
        self.assertEqual(quote.promo_eligible_subtotal, Decimal('2200'))
        self.assertEqual(quote.public_metadata()['bundle_paired_tee_qty'], 1)

    def test_unpaired_hoodie_remains_promo_eligible(self):
        quote = self.quote(hoodies=3)
        self.assertEqual(quote.promo_eligible_subtotal, Decimal('3990'))
        self.assertEqual(dict(quote.bundle_hoodie_capacity)['h'], 2)
        self.assertEqual(quote.lines['h'].snapshot_parts(), [(1, Decimal('1945')), (2, Decimal('1995'))])

    def test_option_deltas_are_retained(self):
        ProductOptionProfile.objects.create(product=self.tee, option_key='fit=oversize', option_values={'fit': 'oversize'}, price_delta=150)
        cart = {'t': self.row(self.tee, option_values={'fit': 'oversize'}), 'h': self.row(self.hoodie)}
        self.assertEqual(calculate_brigade_cart_pricing(cart).lines['t'].line_total, Decimal('1000'))
        cart['h'] = self.row(self.other_hoodie)
        self.assertEqual(calculate_brigade_cart_pricing(cart).lines['t'].line_total, Decimal('1050'))

    def test_classic_and_oversize_pair_totals_for_same_other_design(self):
        ProductOptionProfile.objects.create(product=self.tee, option_key='fit=oversize', option_values={'fit': 'oversize'}, price_delta=150)
        unknown = Product.objects.create(slug='unknown-hd', title='Unknown hoodie', category=self.hoodie.category, price=1995, status='published')
        for hoodie, options, expected in ((self.hoodie, {}, 2795), (self.hoodie, {'fit': 'oversize'}, 2945),
                                           (unknown, {}, 2845), (unknown, {'fit': 'oversize'}, 2995)):
            with self.subTest(hoodie=hoodie.slug, options=options):
                quote = calculate_brigade_cart_pricing({'h': self.row(hoodie), 't': self.row(self.tee, option_values=options)})
                self.assertEqual(quote.subtotal, Decimal(expected))

    def test_225_atomic_pair_and_tee_only_use_225_rules_without_extra_50(self):
        tee = Product.objects.create(slug='225-tshirt', title='225 tee', category=self.tee.category, price=880, status='published')
        hoodie = Product.objects.create(slug='225-hoodie', title='225 hoodie', category=self.hoodie.category, price=1995, status='published')
        ProductOptionProfile.objects.create(product=tee, option_key='fit=oversize', option_values={'fit': 'oversize'}, price_delta=150)
        payload = self.payload(hoodie=self.row(hoodie), tee=self.row(tee))
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['total'], 2650)
        self.assertEqual(response.json()['offer_kind'], '225')
        self.assertTrue(response.json()['full_payment_only'])
        self.assertEqual(response.json()['bundle_discount_total'], 0)
        self.assertEqual([row['item_price'] for row in response.json()['added_items']], [1850, 800])
        session = self.client.session
        session['cart'] = {'225h': self.row(hoodie)}
        session.save()
        response = self.post({'request_id': '225-tee-request', 'mode': 'tee_only', 'hoodie_key': '225h',
                              'tee': self.row(tee, option_values={'fit': 'oversize'})})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['total'], 2800)
        self.assertTrue(response.json()['full_payment_only'])

    def test_atomic_225_cannot_pair_with_ordinary_or_other_brigade(self):
        tee = Product.objects.create(slug='225-tshirt', title='225 tee', category=self.tee.category, price=880, status='published')
        hoodie = Product.objects.create(slug='225-hoodie', title='225 hoodie', category=self.hoodie.category, price=1995, status='published')
        for hoodie_spec, tee_spec in ((self.row(hoodie), self.row(self.tee)), (self.row(self.hoodie), self.row(tee))):
            self.assertEqual(self.post(self.payload(hoodie=hoodie_spec, tee=tee_spec)).status_code, 400)
        self.assertNotIn('cart', self.client.session)

    def test_225_tee_only_options_do_not_repeat_existing_hoodie_discount(self):
        tee = Product.objects.create(slug='225-tshirt', title='225 tee', category=self.tee.category, price=880, status='published')
        hoodie = Product.objects.create(slug='225-hoodie', title='225 hoodie', category=self.hoodie.category, price=1995, status='published')
        for qty, expected_extra, expected_total in ((1, 145, 2650), (2, 0, 4500)):
            with self.subTest(qty=qty):
                session = self.client.session
                session['cart'] = {'225h': self.row(hoodie, qty)}
                session.save()
                options = self.client.get(reverse('cart_bundle_options'), {'hoodie_id': hoodie.pk, 'mode': 'tee_only', 'hoodie_key': '225h'})
                self.assertEqual(options.status_code, 200, options.content)
                self.assertEqual(options.json()['hoodie_unit_discount'], expected_extra)
                response = self.post({'request_id': f'225-extra-{qty}', 'mode': 'tee_only', 'hoodie_key': '225h', 'tee': self.row(tee)})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['total'], expected_total)
                # A subsequent PDP pair catalog must retain its full offer.
                pair_options = self.client.get(reverse('cart_bundle_options'), {'hoodie_id': hoodie.pk}).json()
                self.assertEqual(pair_options['hoodie_unit_discount'], 145)

    def test_hoodie_offer_preserves_legitimate_option_deltas_and_positive_floor(self):
        from storefront.services.garment_bundle import garment_bundle_hoodie_offer_price, hoodie_offer_price
        ProductOptionProfile.objects.create(product=self.hoodie, option_key='material=premium', option_values={'material': 'premium'}, price_delta=300)
        cart = {'t': self.row(self.tee), 'h': self.row(self.hoodie, option_values={'material': 'premium'})}
        quote = calculate_brigade_cart_pricing(cart)
        self.assertEqual(quote.lines['h'].line_total, Decimal('2245'))
        self.assertEqual(quote.public_metadata()['bundle_discount_total'], 300)
        self.assertEqual(garment_bundle_hoodie_offer_price(self.hoodie, option_values={'material': 'premium'}), Decimal('2245'))
        self.assertEqual(hoodie_offer_price(Decimal('30')), Decimal('0.01'))

    def test_no_positive_pair_benefit_is_rejected_without_mutation(self):
        self.tee.discount_percent = 30
        self.tee.save()
        self.hoodie.price = Decimal('0.01')
        self.hoodie.save()
        self.assertFalse(self.quote().bundle_allocations)
        self.assertEqual(self.post(self.payload()).status_code, 400)
        self.assertNotIn('cart', self.client.session)

    def test_better_site_price_does_not_consume_pair(self):
        self.tee.discount_percent = 30
        self.tee.save()
        quote = calculate_brigade_cart_pricing({'t': self.row(self.tee), 'o': self.row(self.other), 'h': self.row(self.hoodie)})
        self.assertEqual(quote.lines['t'].line_total, Decimal('770'))
        self.assertFalse(quote.lines['t'].bundle_allocations)
        self.assertEqual(quote.lines['o'].line_total, Decimal('900'))
        self.assertEqual(quote.promo_eligible_subtotal, Decimal('770'))

    def test_cheaper_variant_override_is_sale_not_negative_surcharge(self):
        variant = ProductColorVariant.objects.create(product=self.tee, color=Color.objects.create(name='Black', primary_hex='#000000'), price_override=800)
        cart = {'t': self.row(self.tee, color_variant_id=variant.pk), 'h': self.row(self.hoodie)}
        quote = calculate_brigade_cart_pricing(cart)
        self.assertEqual(quote.lines['t'].line_total, Decimal('800'))
        self.assertEqual(quote.public_metadata()['bundle_discount_total'], 50)
        ProductOptionProfile.objects.create(product=self.tee, option_key='material=premium', option_values={'material': 'premium'}, price_delta=400)
        cart['t']['option_values'] = {'material': 'premium'}
        quote = calculate_brigade_cart_pricing(cart)
        self.assertEqual(quote.lines['t'].line_total, Decimal('1200'))
        self.assertEqual(quote.public_metadata()['bundle_discount_total'], 50)

    def test_discounted_hoodie_keeps_actual_site_price(self):
        self.hoodie.discount_percent = 20
        self.hoodie.save()
        self.assertEqual(self.quote().subtotal, Decimal('2396'))

    def test_global_matching_maximizes_savings_independent_of_cart_order(self):
        cart = {'money_hood': self.row(self.other_hoodie), 'baby_hood': self.row(self.hoodie), 'baby': self.row(self.tee), 'money': self.row(self.other)}
        quote = calculate_brigade_cart_pricing(cart)
        reversed_quote = calculate_brigade_cart_pricing(dict(reversed(list(cart.items()))))
        self.assertEqual(quote.subtotal, Decimal('5445'))
        self.assertEqual(quote.bundle_allocations, reversed_quote.bundle_allocations)
        self.assertTrue(all(row.kind == 'same_design' for row in quote.bundle_allocations))

    def test_matching_residual_reassigns_to_better_pair(self):
        # Baby offers 850 vs 900, but a 20% site-discount leaves only 30/0 saving.
        # Money still saves 250/200: both matched designs remain globally best.
        self.tee.discount_percent = 20
        self.tee.save()
        cart = {'h': self.row(self.hoodie), 'oh': self.row(self.other_hoodie), 't': self.row(self.tee), 'o': self.row(self.other)}
        quote = calculate_brigade_cart_pricing(cart)
        self.assertEqual(sum(row.discount_amount for row in quote.bundle_allocations), Decimal('380'))

    def test_brigade_and_thermo_are_excluded(self):
        collection = MerchCollection.objects.create(slug='future', kind='brigade', name_uk='Future')
        ProductMerchCollection.objects.create(product=self.tee, collection=collection)
        quote = self.quote()
        self.assertFalse(quote.bundle_allocations)
        self.assertTrue(quote.full_payment_only)
        thermo = Product.objects.create(pk=110, slug='thermo', title='Thermo', category=self.other.category, price=1100, status='published')
        self.assertFalse(calculate_brigade_cart_pricing({'t': self.row(thermo), 'h': self.row(self.hoodie)}).bundle_allocations)

    def test_pair_endpoint_and_idempotent_replay(self):
        payload = self.payload()
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['total'], 2795)
        self.assertEqual(response.json()['added_items'][1]['item_price'], 850)
        self.assertTrue(self.post(payload).json()['replayed'])
        self.assertEqual(sum(row['qty'] for row in self.client.session['cart'].values()), 2)
        payload['tee']['size'] = 'L'
        self.assertEqual(self.post(payload).status_code, 409)

    def test_invalid_second_item_leaves_session_unchanged(self):
        session = self.client.session
        session['cart'] = {'h': self.row(self.hoodie)}
        session.save()
        payload = self.payload()
        payload['tee']['size'] = 'INVALID'
        self.assertEqual(self.post(payload).status_code, 400)
        self.assertEqual(self.client.session['cart'], {'h': self.row(self.hoodie)})
        self.assertNotIn('garment_bundle_requests', self.client.session)

    def test_unpublished_and_foreign_variants_rejected_atomically(self):
        self.tee.status = 'draft'
        self.tee.save()
        self.assertEqual(self.post(self.payload()).status_code, 400)
        self.tee.status = 'published'
        self.tee.save()
        foreign = ProductColorVariant.objects.create(product=self.other, color=Color.objects.create(name='Black', primary_hex='#000000'))
        payload = self.payload()
        payload['tee']['variant_id'] = foreign.pk
        self.assertEqual(self.post(payload).status_code, 400)
        self.assertNotIn('cart', self.client.session)

    def test_tee_only_requires_available_sponsor(self):
        payload = {'request_id': 'tee-request-1', 'mode': 'tee_only', 'hoodie_key': 'h', 'tee': self.row(self.tee)}
        self.assertEqual(self.post(payload).status_code, 400)
        session = self.client.session
        session['cart'] = {'h': self.row(self.hoodie)}
        session.save()
        self.assertEqual(self.post(payload).json()['total'], 2795)
        payload['request_id'] = 'tee-request-2'
        self.assertEqual(self.post(payload).status_code, 400)

    def test_new_pair_accepts_better_existing_sponsor_allocation(self):
        session = self.client.session
        session['cart'] = {'h': self.row(self.hoodie)}
        session.save()
        payload = self.payload(hoodie=self.row(self.other_hoodie))
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['added_items'][1]['item_price'], 850)
        self.assertEqual(response.json()['total'], 4645)
        self.assertEqual(sum(row['qty'] for row in self.client.session['cart'].values()), 3)

    def test_added_item_value_uses_new_tier_not_old_cheapest_tier(self):
        session = self.client.session
        session['cart'] = {'h': self.row(self.hoodie), '4:M:default': self.row(self.tee)}
        session.save()
        response = self.post(self.payload(hoodie=self.row(self.other_hoodie)))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['added_items'][1]['item_price'], 900)

    def test_locked_endpoint_reloads_stale_session_for_different_request_ids(self):
        from importlib import import_module
        from django.conf import settings
        from storefront.views.cart import add_bundle_to_cart
        store = import_module(settings.SESSION_ENGINE).SessionStore
        session = self.client.session
        first = RequestFactory().post('/cart/bundle/add/', json.dumps(self.payload()), content_type='application/json')
        second = RequestFactory().post('/cart/bundle/add/', json.dumps(self.payload(request_id='bundle-request-2')), content_type='application/json')
        first.session = store(session_key=session.session_key)
        second.session = store(session_key=session.session_key)
        second.session.get('cart')  # Authentication can preload this stale state.
        with patch('storefront.views.cart.record_add_to_cart'):
            self.assertEqual(add_bundle_to_cart(first).status_code, 200)
            self.assertEqual(add_bundle_to_cart(second).status_code, 200)
        saved = store(session_key=session.session_key)
        self.assertEqual(sum(row['qty'] for row in saved['cart'].values()), 4)
        self.assertEqual(len(saved['garment_bundle_requests']), 2)
        self.assertFalse(second.session.modified)

    def test_authenticated_full_middleware_preserves_newer_account_cart(self):
        from django.contrib.auth import get_user_model
        from accounts.models import UserCart
        user = get_user_model().objects.create_user(username='bundle-sync-buyer')
        self.client.force_login(user)
        first = self.post(self.payload())
        self.assertEqual(first.status_code, 200)
        account_cart = UserCart.objects.get(user=user)
        self.assertEqual(sum(row['qty'] for row in account_cart.cart_data.values()), 2)
        account_cart.cart_data = {**account_cart.cart_data, 'other-device': self.row(self.other_hoodie)}
        account_cart.save(update_fields=['cart_data', 'updated_at'])
        # Session row still contains two units; normal middleware hydrates
        # three. The locked view must repeat hydration after its forced reload.
        response = self.post(self.payload(request_id='bundle-request-2'))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['count'], 5)
        self.assertFalse(response.wsgi_request.session.modified)
        account_cart.refresh_from_db()
        self.assertEqual(sum(row['qty'] for row in account_cart.cart_data.values()), 5)
        from accounts.cart_sync import persist_session_to_db
        with patch.object(UserCart, 'save') as late_save:
            persist_session_to_db(response.wsgi_request)
        late_save.assert_not_called()

    def test_analytics_failure_runs_after_commit_and_cannot_lose_cart(self):
        from django.db import IntegrityError
        with self.captureOnCommitCallbacks(execute=True), patch('storefront.views.cart.record_add_to_cart', side_effect=IntegrityError('analytics failed')):
            response = self.post_without_analytics_patch(self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sum(row['qty'] for row in self.client.session['cart'].values()), 2)
        self.assertEqual(len(self.client.session['garment_bundle_requests']), 1)

    def post_without_analytics_patch(self, payload):
        self.client.session
        return self.client.post(reverse('cart_bundle_add'), json.dumps(payload), content_type='application/json')

    def test_account_cart_failure_rolls_back_without_success_receipt(self):
        from django.contrib.auth import get_user_model
        from accounts.models import UserCart
        from django.db import IntegrityError
        user = get_user_model().objects.create_user(username='bundle-sync-failure')
        self.client.force_login(user)
        UserCart.objects.get_or_create(user=user)
        original_save = UserCart.save
        def fail_update(instance, *args, **kwargs):
            if kwargs.get('update_fields'):
                raise IntegrityError('account cart failed')
            return original_save(instance, *args, **kwargs)
        with patch.object(UserCart, 'save', fail_update), self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(IntegrityError):
                self.post(self.payload())
        self.assertEqual(callbacks, [])
        self.assertNotIn('garment_bundle_requests', self.client.session)
        self.assertFalse(self.client.session.get('cart'))

    def test_cheaper_tee_keeps_better_price_with_real_hoodie_saving(self):
        self.tee.discount_percent = 30
        self.tee.save()
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['total'], 2715)
        self.assertEqual(response.json()['bundle_discount_total'], 50)
        self.assertEqual(response.json()['added_items'][1]['item_price'], 770)

    def test_quantity_cap_rejects_instead_of_silently_clamping(self):
        session = self.client.session
        session['cart'] = {'4:M:default': self.row(self.tee, 50)}
        session.save()
        self.assertEqual(self.post(self.payload()).status_code, 400)
        self.assertEqual(self.client.session['cart']['4:M:default']['qty'], 50)
        session = self.client.session
        session['cart'] = {'4:M:default': self.row(self.tee, 49)}
        session.save()
        self.assertEqual(self.post(self.payload()).status_code, 200)
        self.assertEqual(self.client.session['cart']['4:M:default']['qty'], 50)

    def test_cart_distinct_line_limit_validates_both_items_before_write(self):
        session = self.client.session
        original = {f'existing-{index}': self.row(self.other) for index in range(99)}
        session['cart'] = original
        session.save()
        self.assertEqual(self.post(self.payload()).status_code, 400)
        self.assertEqual(self.client.session['cart'], original)
        session = self.client.session
        session['cart'] = {**dict(list(original.items())[:98])}
        session.save()
        self.assertEqual(self.post(self.payload()).status_code, 200)
        self.assertEqual(len(self.client.session['cart']), 100)

    def test_instagram_revision_and_payment_snapshot_exact_partial_quantity(self):
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_checkout_payment import _snapshot, _proposal_basket, _proposal_brigade_policy
        client = IgClient.get_or_create_for_sender('general-bundle-exact')
        proposal = create_or_update_proposal(client=client, item_specs=[self.row(self.tee, 3), self.row(self.hoodie)], pay_type="online_full")
        frozen = _snapshot(proposal)['cart']
        tees = [row for row in frozen if row['product_id'] == self.tee.pk]
        self.assertEqual([(row['qty'], Decimal(row['unit_price'])) for row in tees], [(1, Decimal('850')), (2, Decimal('1100'))])
        self.assertEqual(sum(Decimal(row['line_total']) for row in frozen), Decimal('4995'))
        self.assertEqual(sum(row['sum'] for row in _proposal_basket(proposal)), 499500)
        self.assertEqual(_proposal_brigade_policy(proposal), (False, Decimal('2200')))

    def test_instagram_price_change_invalidates_frozen_invoice(self):
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_checkout_payment import _revalidate_frozen_proposal, CheckoutPaymentError
        client = IgClient.get_or_create_for_sender('general-bundle-drift')
        proposal = create_or_update_proposal(client=client, item_specs=[self.row(self.tee, 3), self.row(self.hoodie)], pay_type="online_full")
        _revalidate_frozen_proposal(proposal)
        self.hoodie.price = 2100
        self.hoodie.save()
        with self.assertRaises(CheckoutPaymentError):
            _revalidate_frozen_proposal(proposal)

    def test_paid_historical_proposal_policy_survives_missing_product(self):
        from management.models import IgClient
        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_checkout_payment import _proposal_brigade_policy
        client = IgClient.get_or_create_for_sender('general-bundle-historical')
        proposal = create_or_update_proposal(client=client, item_specs=[self.row(self.tee, 3), self.row(self.hoodie)], pay_type='online_full')
        proposal.items.filter(product=self.tee).update(product=None)
        self.assertEqual(_proposal_brigade_policy(proposal)[0], False)

    def test_cart_api_reprices_removal_and_partial_line_promo(self):
        from django.contrib.auth import get_user_model
        from accounts.models import UserProfile
        user = get_user_model().objects.create_user(username='bundle-promo-buyer')
        UserProfile.objects.get_or_create(user=user)
        self.client.force_login(user)
        session = self.client.session
        session['cart'] = {'t': self.row(self.tee, 3), 'h': self.row(self.hoodie)}
        session.save()
        api = self.client.get(reverse('cart_items_api')).json()
        self.assertEqual(api['total'], 4995)
        self.assertEqual(api['promo_eligible_subtotal'], 2200)
        self.assertEqual(api['bundle_paired_tee_qty'], 1)
        promo = PromoCode.objects.create(code='ORDINARY-BUNDLE', discount_type='percentage', discount_value=10, is_active=True,
                                        valid_from=timezone.now()-timedelta(days=1), valid_until=timezone.now()+timedelta(days=1), one_time_per_user=False)
        applied = self.client.post(reverse('apply_promo_code'), {'promo_code': promo.code}).json()
        self.assertTrue(applied['success'], applied)
        self.assertEqual(applied['discount'], 220)
        self.assertEqual(self.client.get(reverse('cart_items_api')).json()['total'], 4775)
        mini = self.client.get(reverse('cart_mini')).context
        full = self.client.get(reverse('cart')).context
        api = self.client.get(reverse('cart_items_api')).json()
        self.assertEqual(mini['discount'], Decimal('220'))
        self.assertEqual(mini['approved_total'], Decimal('4775'))
        self.assertEqual(mini['approved_total'], full['approved_total'])
        self.assertEqual(mini['approved_total'], Decimal(str(api['approved_total'])))
        self.assertTrue(api['shipping_free'])
        removed = self.client.post(reverse('cart_remove'), {'key': 'h'}).json()
        self.assertEqual(removed['bundle_paired_tee_qty'], 0)
        self.assertEqual(removed['total'], 2970)
        api = self.client.get(reverse('cart_items_api')).json()
        self.assertFalse(api['shipping_free'])
        self.assertEqual(api['shipping_remaining'], 30)
        self.assertFalse(self.client.get(reverse('cart_mini')).context['shipping_free'])
        self.assertFalse(self.client.get(reverse('cart')).context['shipping_free'])

    def test_pending_custom_estimate_is_separate_from_net_shipping_total(self):
        from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
        session = self.client.session
        session['cart'] = {'t': self.row(self.tee), 'h': self.row(self.hoodie)}
        session[SESSION_CUSTOM_CART_KEY] = {'custom:pending': {'quantity': 1, 'product_type': 'tshirt',
            'size': 'M', 'final_total': '1000', 'unit_total': '1000', 'moderation_status': 'draft'}}
        session.save()
        mini = self.client.get(reverse('cart_mini')).context
        api = self.client.get(reverse('cart_items_api')).json()
        full = self.client.get(reverse('cart')).context
        for context in (mini, api, full):
            self.assertEqual(Decimal(str(context['approved_total'])), Decimal('2795'))
            self.assertFalse(context['shipping_free'])
            self.assertEqual(Decimal(str(context['pending_custom_total'])), Decimal('1000'))

    def test_monobank_exact_groups_and_order_snapshot(self):
        session = self.client.session
        session['cart'] = {'t': self.row(self.tee, 3), 'h': self.row(self.hoodie)}
        session.save()
        delivery = SimpleNamespace(city='Київ', np_office='Відділення №1', settlement_ref='s', city_ref='c', warehouse_ref='w')
        with patch('storefront.views.monobank.resolve_delivery_selection', return_value=delivery), patch('storefront.views.monobank._monobank_api_request', return_value={'invoiceId': 'bundle-invoice', 'pageUrl': 'https://pay.example/bundle'}) as provider, patch('storefront.views.monobank.record_initiate_checkout'):
            response = self.client.post(reverse('monobank_create_invoice'), json.dumps({'full_name': 'Test Buyer', 'phone': '+380501234567', 'pay_type': 'online_full'}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        attempt = PaymentAttempt.objects.get()
        self.assertEqual(attempt.payable_amount, Decimal('4995'))
        frozen = attempt.cart_snapshot['cart']
        self.assertEqual(sum(Decimal(row['unit_price']) * row['qty'] for row in frozen), Decimal('4995'))
        payload = provider.call_args.kwargs['json_payload']
        self.assertEqual(payload['amount'], 499500)
        self.assertEqual(sum(row['sum'] for row in payload['merchantPaymInfo']['basketOrder']), 499500)
        from orders.payment_attempts import materialize_payment_attempt
        order, created = materialize_payment_attempt(attempt.pk, status='success', payload={'amount': 499500}, source='test')
        self.assertTrue(created)
        self.assertEqual(order.items.count(), 3)
        self.assertEqual(sum(item.unit_price * item.qty for item in order.items.all()), Decimal('4995'))
