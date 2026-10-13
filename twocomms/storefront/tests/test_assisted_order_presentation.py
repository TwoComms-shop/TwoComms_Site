"""Actual order/account surfaces never promote raw photo-map metadata."""
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from orders.models import Order, OrderItem
from orders.services.customer_order_presentation import prepare_order_item_presentation
from storefront.views.admin import _build_orders_context
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture


@override_settings(ROOT_URLCONF='twocomms.urls', SECURE_SSL_REDIRECT=False,
    COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False)
class AssistedOrderPresentationTests(TestCase):
    def setUp(self):
        self.buyer = get_user_model().objects.create_user('custom-presentation-buyer')
        self.foreign = get_user_model().objects.create_user('custom-presentation-other')
        self.manager = get_user_model().objects.create_user('custom-presentation-staff', is_staff=True)
        self.order = Order.objects.create(user=self.buyer, full_name='Order buyer', phone='380501112233',
            city='Київ', np_office='Відділення 1', source='manual', total_sum=Decimal('1940.00'))
        self.item = OrderItem.objects.create(order=self.order, title='Фіолетове худі для покупця',
            is_custom=True, size='L', color_name_custom='Фіолетовий', qty=2,
            unit_price=Decimal('970.00'), line_total=Decimal('1940.00'),
            option_values={'internal_reference': 'private-value'},
            option_labels={'Принт': '<script>design</script>', '_reference_ids': 'private-value'})

    def _untrusted_metadata(self):
        return {'execution_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()),
            'decision_id': str(uuid.uuid4()), 'proof_digest': 'a' * 64,
            'item_map': [{'proposal_item_id': 77, 'order_item_id': self.item.pk,
                'line_id': 'forged-line', 'photo_asset_id': str(uuid.uuid4()), 'photo_digest': 'b' * 64}]}

    def test_owned_account_card_shows_real_custom_facts_and_safe_branded_fallback(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse('my_orders'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Фіолетове худі для покупця')
        self.assertContains(response, 'Фіолетовий')
        self.assertContains(response, 'Розмір: L')
        self.assertContains(response, '2 шт')
        self.assertContains(response, 'img/logo.svg')
        self.assertContains(response, '&lt;script&gt;design&lt;/script&gt;')
        self.assertNotContains(response, '<script>design</script>')
        self.assertNotContains(response, 'private-value')

    def test_foreign_account_never_receives_order_or_private_map_validation(self):
        self.order.payment_payload = {'ig_assisted_commerce': self._untrusted_metadata()}
        self.order.save(update_fields=['payment_payload'])
        orders = list(Order.objects.filter(pk=self.order.pk).prefetch_related('items'))
        with self.assertNumQueries(0):
            prepared = prepare_order_item_presentation(orders, audience='account', actor=self.foreign)
        self.assertEqual(prepared[0].items.all()[0].assisted_image_url, '')
        self.client.force_login(self.foreign)
        response = self.client.get(reverse('my_orders'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.item.title)

    def test_raw_photo_map_cannot_generate_protected_account_url(self):
        metadata = self._untrusted_metadata()
        self.order.payment_payload = {'ig_assisted_commerce': metadata}
        self.order.save(update_fields=['payment_payload'])
        orders = Order.objects.filter(pk=self.order.pk).prefetch_related('items')
        prepared = prepare_order_item_presentation(orders, audience='account', actor=self.buyer)
        self.assertEqual(prepared[0].items.all()[0].assisted_image_url, '')
        self.client.force_login(self.buyer)
        response = self.client.get(reverse('my_orders'))
        self.assertNotContains(response, metadata['item_map'][0]['photo_asset_id'])
        self.assertNotContains(response, f'/my/orders/{self.order.pk}/items/{self.item.pk}/image/')

    def test_actual_admin_context_renders_same_custom_facts_and_safe_options(self):
        request = RequestFactory().get('/admin-panel/', {'section': 'orders'})
        request.user = self.manager
        context = _build_orders_context(request)
        html = render_to_string('partials/admin_orders_section.html', context)
        self.assertIn(self.item.title, html)
        self.assertIn('Фіолетовий', html)
        self.assertIn('2 шт', html)
        self.assertIn('img/logo.svg', html)
        self.assertIn('&lt;script&gt;design&lt;/script&gt;', html)
        self.assertNotIn('private-value', html)


@override_settings(ROOT_URLCONF='twocomms.urls', SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2='enforced',
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False,
    INDEXNOW_ENABLED=False, COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False)
class NativeAssistedPhotoPresentationTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def test_real_paid_photo_order_projects_protected_staff_url_without_private_asset_identity(self):
        self.client.force_login(self.manager)
        result = self._paid_custom_order(photo=True)
        request = RequestFactory().get('/admin-panel/', {'section': 'orders'})
        request.user = self.manager
        context = _build_orders_context(request)
        order = next(row for row in context['orders'] if row.pk == result['order'].pk)
        item, = order.items.all()
        path = reverse('management_assisted_order_item_image',
            kwargs={'order_id': order.pk, 'item_id': item.pk}, urlconf='management.urls')
        self.assertTrue(item.assisted_image_url.endswith(path))
        html = render_to_string('partials/admin_orders_section.html', context)
        self.assertIn(item.assisted_image_url, html)
        self.assertIn('Фіолетове худі', html)
        self.assertIn('Фіолетовий', html)
        self.assertNotIn(str(result['image'].pk), html)
        self.assertNotIn(result['image'].storage_key, html)

    def test_real_authenticated_payer_sees_actual_photo_and_foreign_account_cannot_read_it(self):
        self.client.force_login(self.manager)
        result = self._paid_custom_order(photo=True)
        order, item = result['order'], result['items'][0]
        self.assertEqual(order.user_id, self.manager.pk)
        path = reverse('account_assisted_order_item_image',
            kwargs={'order_id': order.pk, 'item_id': item.pk})
        response = self.client.get(reverse('my_orders'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, path)
        self.assertContains(response, 'Фіолетове худі')
        self.assertNotContains(response, str(result['image'].pk))
        photo = self.client.get(path)
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo['Content-Type'], result['image'].mime_type)
        self.assertIn('no-store', photo['Cache-Control'])
        outsider = get_user_model().objects.create_user('native-photo-foreign-buyer')
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(path).status_code, 404)
