"""
Checkout-adjacent tests for the current storefront contract.

Covers:
- order_create
- order_success
- confirm_payment
- cart/promo endpoints used by the checkout flow
"""

from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.http import HttpResponseRedirect
from django.test import TestCase
from django.urls import reverse

from orders.models import Order, OrderItem
from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from storefront.models import Category, Product, PromoCode, PromoCodeUsage


class CheckoutTestSupport(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name='Test Category',
            slug='test-category',
        )
        self.product = Product.objects.create(
            title='Test Product',
            slug='test-product',
            category=self.category,
            price=130,
            status='published',
        )

        self.order_create_url = reverse('order_create')
        self.order_success_url_name = 'order_success'
        self.confirm_payment_url = reverse('confirm_payment')
        self.cart_summary_url = reverse('cart_summary')
        self.apply_promo_url = reverse('apply_promo_code')
        self.remove_promo_url = reverse('remove_promo_code')

    def set_cart(self, *, product=None, qty=2, size='M', fit_option_code='', fit_option_label=''):
        product = product or self.product
        session = self.client.session
        key = f'{product.id}:{size}'
        if fit_option_code:
            key = f'{key}:default:{fit_option_code}'
        session['cart'] = {
            key: {
                'product_id': product.id,
                'qty': qty,
                'size': size,
                'fit_option_code': fit_option_code,
                'fit_option_label': fit_option_label,
            }
        }
        session.save()

    def delivery_payload(
        self,
        *,
        city_label='м. Київ, Київ',
        city_ref='delivery-city-ref',
        settlement_ref='settlement-ref',
        warehouse_label='Відділення №1, Київ',
        warehouse_ref='warehouse-ref',
    ):
        return {
            'city': 'довільне введення',
            'np_office': 'довільне введення',
            'np_settlement_ref': 'spoofed-settlement-ref',
            'np_city_ref': 'spoofed-city-ref',
            'np_city_token': build_city_choice_token(
                {
                    'label': city_label,
                    'settlement_ref': settlement_ref,
                    'city_ref': city_ref,
                }
            ),
            'np_warehouse_ref': 'spoofed-warehouse-ref',
            'np_warehouse_token': build_warehouse_choice_token(
                {
                    'label': warehouse_label,
                    'ref': warehouse_ref,
                    'kind': 'branch',
                    'city_ref': city_ref,
                }
            ),
            'canonical_city': city_label,
            'canonical_np_office': warehouse_label,
            'canonical_settlement_ref': settlement_ref,
            'canonical_city_ref': city_ref,
            'canonical_warehouse_ref': warehouse_ref,
        }

    def make_user(self, *, username='buyer', pay_type='full'):
        user = User.objects.create_user(
            username=username,
            email=f'{username}@example.com',
            password='testpass123',
        )
        profile = user.userprofile
        profile.full_name = 'Profile Buyer'
        profile.phone = '+380991234567'
        profile.city = 'Київ'
        profile.np_office = 'Відділення №1'
        profile.pay_type = pay_type
        profile.save()
        return user

    def make_fake_order_item_class(self):
        manager = Mock()

        class FakeOrderItem:
            objects = manager

            def __init__(self, **kwargs):
                self.order = kwargs['order']
                self.product = kwargs['product']
                self.color_variant = kwargs.get('color_variant')
                self.size = kwargs.get('size', '')
                self.qty = kwargs.get('qty', kwargs.get('quantity'))
                self.unit_price = kwargs.get('unit_price', kwargs.get('price'))
                self.line_total = kwargs.get('line_total', self.qty * self.unit_price)
                self.raw_kwargs = kwargs

        return FakeOrderItem, manager


class CreateOrderTests(CheckoutTestSupport):
    def test_create_order_guest_cod_creates_order_and_clears_cart(self):
        self.set_cart()
        delivery = self.delivery_payload(
            city_label='м. Львів, Львів',
            city_ref='lviv-city-ref',
            settlement_ref='lviv-settlement-ref',
            warehouse_label='Відділення №5, Львів',
            warehouse_ref='lviv-warehouse-ref',
        )
        fake_order_item_class, fake_manager = self.make_fake_order_item_class()

        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            response = self.client.post(
                self.order_create_url,
                {
                    'full_name': 'Guest Buyer',
                    'phone': '+380501112233',
                    'city': delivery['city'],
                    'np_office': delivery['np_office'],
                    'np_settlement_ref': delivery['np_settlement_ref'],
                    'np_city_ref': delivery['np_city_ref'],
                    'np_city_token': delivery['np_city_token'],
                    'np_warehouse_ref': delivery['np_warehouse_ref'],
                    'np_warehouse_token': delivery['np_warehouse_token'],
                    'pay_type': 'cod',
                },
                secure=True,
            )

        order = Order.objects.get()
        self.assertRedirects(
            response,
            reverse(self.order_success_url_name, kwargs={'order_id': order.id}),
            fetch_redirect_response=False,
        )
        self.assertIsNone(order.user)
        self.assertEqual(order.full_name, 'Guest Buyer')
        self.assertEqual(order.phone, '+380501112233')
        self.assertEqual(order.city, delivery['canonical_city'])
        self.assertEqual(order.np_office, delivery['canonical_np_office'])
        self.assertEqual(order.np_settlement_ref, delivery['canonical_settlement_ref'])
        self.assertEqual(order.np_city_ref, delivery['canonical_city_ref'])
        self.assertEqual(order.np_warehouse_ref, delivery['canonical_warehouse_ref'])
        self.assertEqual(order.pay_type, 'cod')
        self.assertEqual(order.total_sum, Decimal('260'))
        self.assertEqual(self.client.session.get('cart'), {})

        bulk_items = fake_manager.bulk_create.call_args.args[0]
        self.assertEqual(len(bulk_items), 1)
        self.assertEqual(bulk_items[0].product, self.product)
        self.assertEqual(bulk_items[0].qty, 2)
        self.assertEqual(bulk_items[0].unit_price, self.product.final_price)
        self.assertEqual(bulk_items[0].line_total, self.product.final_price * 2)

    def test_create_order_snapshots_fit_option_on_order_item(self):
        self.set_cart(fit_option_code='classic', fit_option_label='Класичний')
        delivery = self.delivery_payload()
        fake_order_item_class, fake_manager = self.make_fake_order_item_class()

        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            response = self.client.post(
                self.order_create_url,
                {
                    'full_name': 'Guest Buyer',
                    'phone': '+380501112233',
                    'city': delivery['city'],
                    'np_office': delivery['np_office'],
                    'np_settlement_ref': delivery['np_settlement_ref'],
                    'np_city_ref': delivery['np_city_ref'],
                    'np_city_token': delivery['np_city_token'],
                    'np_warehouse_ref': delivery['np_warehouse_ref'],
                    'np_warehouse_token': delivery['np_warehouse_token'],
                    'pay_type': 'cod',
                },
                secure=True,
            )

        self.assertEqual(response.status_code, 302)
        bulk_items = fake_manager.bulk_create.call_args.args[0]
        self.assertEqual(bulk_items[0].raw_kwargs['fit_option_code'], 'classic')
        self.assertEqual(bulk_items[0].raw_kwargs['fit_option_label'], 'Класичний')

    def _cod_post_payload(self, delivery, full_name='Promo Buyer', phone='+380631112233'):
        return {
            'full_name': full_name,
            'phone': phone,
            'city': delivery['city'],
            'np_office': delivery['np_office'],
            'np_settlement_ref': delivery['np_settlement_ref'],
            'np_city_ref': delivery['np_city_ref'],
            'np_city_token': delivery['np_city_token'],
            'np_warehouse_ref': delivery['np_warehouse_ref'],
            'np_warehouse_token': delivery['np_warehouse_token'],
            'pay_type': 'cod',
        }

    def test_create_order_cod_applies_promo_discount(self):
        """W1-4а (CRO-046): COD-заказ с промо имеет discount_amount > 0."""
        promo = PromoCode.objects.create(
            code='COD10',
            discount_type='fixed',
            discount_value=Decimal('10'),
            is_active=True,
        )
        user = self.make_user(username='promo-cod-user', pay_type='cod')
        self.client.force_login(user)
        self.set_cart()
        session = self.client.session
        session['promo_code_id'] = promo.id
        session.save()

        delivery = self.delivery_payload()
        fake_order_item_class, _ = self.make_fake_order_item_class()

        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            response = self.client.post(
                self.order_create_url, self._cod_post_payload(delivery), secure=True
            )

        order = Order.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(order.discount_amount, Decimal('10'))
        self.assertEqual(order.promo_code_id, promo.id)
        # W1-4б: usage recorded and limits burned
        self.assertEqual(PromoCodeUsage.objects.filter(promo_code=promo, user=user).count(), 1)
        promo.refresh_from_db()
        self.assertEqual(promo.current_uses, 1)
        # Session promo keys cleared after placement
        self.assertIsNone(self.client.session.get('promo_code_id'))

    def test_create_order_cod_rejects_reused_one_time_promo(self):
        """W1-4б: повторное использование one_time_per_user кода отклоняется."""
        promo = PromoCode.objects.create(
            code='ONCE10',
            discount_type='fixed',
            discount_value=Decimal('10'),
            is_active=True,
            one_time_per_user=True,
        )
        user = self.make_user(username='once-user', pay_type='cod')
        PromoCodeUsage.objects.create(user=user, promo_code=promo)
        self.client.force_login(user)
        self.set_cart()
        session = self.client.session
        session['promo_code_id'] = promo.id
        session.save()

        delivery = self.delivery_payload()
        fake_order_item_class, _ = self.make_fake_order_item_class()

        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            self.client.post(self.order_create_url, self._cod_post_payload(delivery), secure=True)

        order = Order.objects.get()
        self.assertEqual(order.discount_amount or Decimal('0'), Decimal('0'))
        self.assertIsNone(order.promo_code_id)
        self.assertEqual(PromoCodeUsage.objects.filter(promo_code=promo).count(), 1)

    def test_create_order_missing_product_aborts_without_order(self):
        """W1-5а (CRO-047): исчезнувший товар → сообщение + редирект,
        заказ НЕ создаётся (раньше товар молча выбрасывался из заказа)."""
        self.set_cart()
        # Товар удалён после наполнения корзины
        missing_id = self.product.id
        self.product.delete()
        session = self.client.session
        session['cart'] = {
            f'{missing_id}:M': {'product_id': missing_id, 'qty': 1, 'size': 'M'}
        }
        session.save()

        delivery = self.delivery_payload()
        response = self.client.post(
            self.order_create_url, self._cod_post_payload(delivery), secure=True
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('cart'))
        self.assertEqual(Order.objects.count(), 0)
        # Недоступная позиция удалена из корзины
        self.assertEqual(self.client.session.get('cart'), {})

    def test_create_order_zero_total_aborts_without_order(self):
        """W1-5б (CRO-047): заказ на 0 грн не создаётся."""
        free_product = Product.objects.create(
            title='Free Product',
            slug='free-product',
            category=self.category,
            price=0,
            status='published',
        )
        self.set_cart(product=free_product, qty=1)

        delivery = self.delivery_payload()
        fake_order_item_class, _ = self.make_fake_order_item_class()
        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            response = self.client.post(
                self.order_create_url, self._cod_post_payload(delivery), secure=True
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('cart'))
        self.assertEqual(Order.objects.count(), 0)

    def test_create_order_double_submit_creates_single_order(self):
        """W1-14 (NEW-514): двойной сабмит той же корзины в окне 30s не
        создаёт второй заказ, а редиректит на уже созданный."""
        user = self.make_user(username='double-submit-user', pay_type='cod')
        self.client.force_login(user)
        self.set_cart()

        delivery = self.delivery_payload()
        payload = self._cod_post_payload(delivery, full_name='Double Buyer')
        fake_order_item_class, _ = self.make_fake_order_item_class()

        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            first = self.client.post(self.order_create_url, payload, secure=True)
            # Эмулируем double-submit: корзина восстановлена (повторный POST
            # приходит до того, как клиент увидел очистку корзины).
            self.set_cart()
            second = self.client.post(self.order_create_url, payload, secure=True)

        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            second['Location'],
            reverse(self.order_success_url_name, kwargs={'order_id': order.id}),
        )

    def test_create_order_authenticated_uses_profile_data(self):
        self.set_cart()
        delivery = self.delivery_payload(
            city_label='м. Одеса, Одеса',
            city_ref='odesa-city-ref',
            settlement_ref='odesa-settlement-ref',
            warehouse_label='Відділення №99, Одеса',
            warehouse_ref='odesa-warehouse-ref',
        )
        # Online pay types are handled by the Monobank button flow, so the
        # profile-driven form submit path uses COD here.
        user = self.make_user(pay_type='cod')
        self.client.force_login(user)
        fake_order_item_class, _ = self.make_fake_order_item_class()

        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            response = self.client.post(
                self.order_create_url,
                {
                    'full_name': '',
                    'phone': '',
                    'city': delivery['city'],
                    'np_office': delivery['np_office'],
                    'np_settlement_ref': delivery['np_settlement_ref'],
                    'np_city_ref': delivery['np_city_ref'],
                    'np_city_token': delivery['np_city_token'],
                    'np_warehouse_ref': delivery['np_warehouse_ref'],
                    'np_warehouse_token': delivery['np_warehouse_token'],
                    'pay_type': '',
                },
                secure=True,
            )

        order = Order.objects.get(user=user)
        self.assertRedirects(
            response,
            reverse(self.order_success_url_name, kwargs={'order_id': order.id}),
            fetch_redirect_response=False,
        )
        self.assertEqual(order.full_name, 'Profile Buyer')
        self.assertEqual(order.phone, '+380991234567')
        self.assertEqual(order.city, delivery['canonical_city'])
        self.assertEqual(order.np_office, delivery['canonical_np_office'])
        self.assertEqual(order.np_settlement_ref, delivery['canonical_settlement_ref'])
        self.assertEqual(order.np_city_ref, delivery['canonical_city_ref'])
        self.assertEqual(order.np_warehouse_ref, delivery['canonical_warehouse_ref'])
        self.assertEqual(order.pay_type, 'cod')

    def test_create_order_guest_online_full_redirects_to_cart(self):
        """Онлайн-опла��а стартует ТОЛЬКО через кнопку Monobank в корзине —
        прямой POST с online-типом не должен создавать неоплаченный заказ."""
        self.set_cart()
        delivery = self.delivery_payload(
            city_label='м. Київ, Київ',
            city_ref='kyiv-city-ref',
            settlement_ref='kyiv-settlement-ref',
            warehouse_label='Відділення №7, Київ',
            warehouse_ref='kyiv-warehouse-ref',
        )
        fake_order_item_class, _ = self.make_fake_order_item_class()

        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            response = self.client.post(
                self.order_create_url,
                {
                    'full_name': 'Card Buyer',
                    'phone': '+380671112233',
                    'city': delivery['city'],
                    'np_office': delivery['np_office'],
                    'np_settlement_ref': delivery['np_settlement_ref'],
                    'np_city_ref': delivery['np_city_ref'],
                    'np_city_token': delivery['np_city_token'],
                    'np_warehouse_ref': delivery['np_warehouse_ref'],
                    'np_warehouse_token': delivery['np_warehouse_token'],
                    'pay_type': 'online_full',
                },
                secure=True,
            )

        self.assertRedirects(response, reverse('cart'), fetch_redirect_response=False)
        self.assertFalse(Order.objects.exists())

    def test_create_order_empty_cart_redirects_to_cart(self):
        response = self.client.post(
            self.order_create_url,
            {
                'full_name': 'Empty Cart Buyer',
                'phone': '+380501112233',
                'city': 'Київ',
                'np_office': 'Відділення №1',
                'pay_type': 'cod',
            },
        )

        self.assertRedirects(response, reverse('cart'), fetch_redirect_response=False)
        self.assertFalse(Order.objects.exists())

    def test_create_order_ignores_cart_promo_session_keys(self):
        self.set_cart()
        delivery = self.delivery_payload(
            city_label='м. Харків, Харків',
            city_ref='kharkiv-city-ref',
            settlement_ref='kharkiv-settlement-ref',
            warehouse_label='Відділення №3, Харків',
            warehouse_ref='kharkiv-warehouse-ref',
        )
        promo = PromoCode.objects.create(
            code='TEST10',
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            is_active=True,
        )
        session = self.client.session
        session['promo_code_id'] = promo.id
        session['promo_code_data'] = {
            'code': promo.code,
            'discount': 26.0,
        }
        session.save()

        fake_order_item_class, _ = self.make_fake_order_item_class()
        with patch('storefront.views.checkout.OrderItem', fake_order_item_class):
            response = self.client.post(
                self.order_create_url,
                {
                    'full_name': 'Promo Buyer',
                    'phone': '+380501112233',
                    'city': delivery['city'],
                    'np_office': delivery['np_office'],
                    'np_settlement_ref': delivery['np_settlement_ref'],
                    'np_city_ref': delivery['np_city_ref'],
                    'np_city_token': delivery['np_city_token'],
                    'np_warehouse_ref': delivery['np_warehouse_ref'],
                    'np_warehouse_token': delivery['np_warehouse_token'],
                    'pay_type': 'cod',
                },
                secure=True,
            )

        order = Order.objects.get()
        self.assertRedirects(
            response,
            reverse(self.order_success_url_name, kwargs={'order_id': order.id}),
            fetch_redirect_response=False,
        )
        self.assertIsNone(order.promo_code)
        self.assertEqual(order.discount_amount, Decimal('0'))
        self.assertNotIn('promo_code_id', self.client.session)
        self.assertNotIn('promo_code_data', self.client.session)


class OrderSuccessTests(CheckoutTestSupport):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.create(
            full_name='Success Buyer',
            phone='+380991112233',
            email='buyer@example.com',
            city='Київ',
            np_office='Відділення №4',
            pay_type='cod',
            payment_status='paid',
            total_sum=Decimal('260.00'),
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            title=self.product.title,
            size='M',
            qty=2,
            unit_price=Decimal('130.00'),
            line_total=Decimal('260.00'),
        )
        self.success_url = reverse(self.order_success_url_name, kwargs={'order_id': self.order.id})

    def grant_session_ownership(self, order=None):
        """W1-2: order_success is owner-only now; mark session as the buyer's."""
        order = order or self.order
        session = self.client.session
        session['recent_order_ids'] = [order.id]
        session.save()

    def test_order_success_renders_order_details(self):
        self.grant_session_ownership()
        response = self.client.get(self.success_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['order'].pk, self.order.pk)
        self.assertContains(response, self.order.order_number)
        self.assertContains(response, self.order.full_name)
        self.assertContains(response, self.product.title)
        self.assertEqual(response.context['order'].total_sum, self.order.total_sum)

    def test_order_success_returns_404_for_unknown_order(self):
        self.grant_session_ownership()
        response = self.client.get(
            reverse(self.order_success_url_name, kwargs={'order_id': self.order.id + 999})
        )

        self.assertEqual(response.status_code, 404)

    def test_order_success_denies_anonymous_stranger(self):
        """W1-2 (CRO-044): страница успеха содержит PII — перебор id должен
        давать 404 для чужой сессии."""
        response = self.client.get(self.success_url)

        self.assertEqual(response.status_code, 404)

    def test_order_success_denies_other_authenticated_user(self):
        owner = self.make_user(username='order-owner')
        self.order.user = owner
        self.order.save(update_fields=['user'])

        stranger = self.make_user(username='stranger')
        self.client.force_login(stranger)
        response = self.client.get(self.success_url)

        self.assertEqual(response.status_code, 404)

    def test_order_success_allows_order_owner_user(self):
        owner = self.make_user(username='order-owner2')
        self.order.user = owner
        self.order.save(update_fields=['user'])

        self.client.force_login(owner)
        response = self.client.get(self.success_url)

        self.assertEqual(response.status_code, 200)

    def test_order_success_allows_matching_session_key(self):
        # Prime a session, then bind the order to it.
        session = self.client.session
        session['touch'] = True
        session.save()
        self.order.session_key = session.session_key
        self.order.save(update_fields=['session_key'])

        response = self.client.get(self.success_url)

        self.assertEqual(response.status_code, 200)

    def test_order_success_preview_requires_staff(self):
        response = self.client.get(reverse('order_success_preview'), follow=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])


class ConfirmPaymentTests(CheckoutTestSupport):
    def test_confirm_payment_redirects_to_my_orders(self):
        response = self.client.get(self.confirm_payment_url)

        self.assertRedirects(response, reverse('my_orders'), fetch_redirect_response=False)

    def test_confirm_payment_follow_redirect_for_authenticated_user(self):
        user = self.make_user(username='history-user')
        self.client.force_login(user)

        response = self.client.get(self.confirm_payment_url, follow=True)

        self.assertRedirects(response, reverse('my_orders'))
        self.assertContains(response, 'Мої замовлення')


class PromoAndCartTests(CheckoutTestSupport):
    def test_cart_summary_uses_current_cart_session_shape(self):
        discounted_product = Product.objects.create(
            title='Discounted Product',
            slug='discounted-product',
            category=self.category,
            price=130,
            discount_percent=10,
            status='published',
        )
        self.set_cart(product=discounted_product)

        response = self.client.get(self.cart_summary_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['count'], 2)
        self.assertEqual(response.json()['total'], 234.0)

    def test_apply_promo_code_requires_authenticated_user(self):
        self.set_cart()
        promo = PromoCode.objects.create(
            code='LOGIN10',
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            is_active=True,
        )

        response = self.client.post(self.apply_promo_url, {'promo_code': promo.code})

        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()['auth_required'])
        self.assertNotIn('promo_code_id', self.client.session)

    def test_apply_promo_code_stores_session_data_for_authenticated_user(self):
        self.set_cart()
        user = self.make_user(username='promo-user')
        self.client.force_login(user)
        promo = PromoCode.objects.create(
            code='SAVE10',
            discount_type='percentage',
            discount_value=Decimal('10.00'),
            is_active=True,
        )

        response = self.client.post(self.apply_promo_url, {'promo_code': 'save10'})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['promo_code'], 'SAVE10')
        self.assertEqual(data['discount'], 26.0)
        self.assertEqual(data['total'], 234.0)
        self.assertEqual(self.client.session['promo_code_id'], promo.id)
        self.assertEqual(self.client.session['promo_code_data']['code'], promo.code)

    def test_remove_promo_code_clears_session_keys(self):
        self.set_cart()
        session = self.client.session
        session['promo_code_id'] = 123
        session['promo_code_data'] = {'code': 'SAVE10', 'discount': 26.0}
        session.save()

        response = self.client.post(self.remove_promo_url)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['discount'], 0.0)
        self.assertEqual(data['total'], 260.0)
        self.assertNotIn('promo_code_id', self.client.session)
        self.assertNotIn('promo_code_data', self.client.session)
