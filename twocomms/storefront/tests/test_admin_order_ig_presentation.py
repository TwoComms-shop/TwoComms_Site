"""Admin readback of real manual Instagram orders and safe option labels."""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.ig_bot_models import IgClient, IgOrderAssignment, IgOrderAttribution
from management.services.ig_order_assignments import unlink_order_from_client
from orders.models import Order
from orders.services.customer_order_presentation import customer_item_option_labels
from storefront.views.admin import _build_orders_context


@override_settings(
    ROOT_URLCONF='twocomms.urls',
    SECURE_SSL_REDIRECT=False,
    NOVA_POSHTA_FALLBACK_ENABLED=False,
    COMPRESS_ENABLED=False,
    COMPRESS_OFFLINE=False,
)
class AdminInstagramOrderPresentationTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            'admin-ig-order-presentation', is_staff=True,
        )
        self.client.force_login(self.manager)
        self.ig_client = IgClient.get_or_create_for_sender('admin-ig-order-customer')
        self.ig_client.username = 'custom_buyer'
        self.ig_client.display_name = 'Custom buyer'
        self.ig_client.save(update_fields=['username', 'display_name', 'updated_at'])

    def _create_order(self):
        token = signing.dumps(
            {'client_id': self.ig_client.pk}, salt='storefront.manual-order.ig-client',
        )
        with patch('storefront.views.manual_orders.telegram_notifier.send_new_order_notification'):
            response = self.client.post(
                reverse('manual_order_create'),
                data={
                    'full_name': 'Custom buyer', 'phone': '0501112233',
                    'delivery_method': 'manual', 'city': 'Київ', 'np_office': 'Відділення 1',
                    'payment_preset': 'cod', 'sale_source': 'Instagram',
                    'ig_client_id': str(self.ig_client.pk), 'ig_client_token': token,
                    'items': [{
                        'kind': 'custom', 'title': 'Футболка з принтом',
                        'unit_price': '790', 'qty': 1, 'size': 'M', 'color_name': 'Білий',
                        'fit_option_label': 'Оверсайз',
                        'option_values': {'primary_reference_message_id': 4321},
                        'option_labels': {
                            'Принт': 'Власний малюнок', 'Посадка': 'Оверсайз',
                            '_reference_message_ids': '4321',
                        },
                    }],
                },
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        return Order.objects.get(pk=response.json()['order_id'])

    def _presented_order(self, order):
        request = RequestFactory().get('/admin-panel/', {'section': 'orders'})
        request.user = self.manager
        context = _build_orders_context(request)
        projected = next(row for row in context['orders'] if row.pk == order.pk)
        return projected, context

    def test_manual_producer_assignment_and_custom_facts_are_visible_immediately(self):
        order = self._create_order()
        assignment = IgOrderAssignment.objects.get(order=order)
        self.assertEqual(assignment.source, IgOrderAssignment.Source.MANAGER_CREATED)
        self.assertFalse(IgOrderAttribution.objects.filter(order=order).exists())
        projected, context = self._presented_order(order)
        self.assertEqual(projected.instagram_identity['username'], 'custom_buyer')
        self.assertIn(f'client_id={self.ig_client.pk}', projected.instagram_identity['url'])
        item = projected.items.all()[0]
        self.assertEqual(item.generic_option_labels, ['Принт: Власний малюнок'])
        self.assertEqual(projected.payment_status, 'unpaid')
        self.assertIsNone(projected.user_id)
        html = render_to_string('partials/admin_orders_section.html', context)
        self.assertIn('@custom_buyer', html)
        self.assertIn('Принт: Власний малюнок', html)
        self.assertIn('Білий', html)
        self.assertNotIn('_reference_message_ids', html)

    def test_matching_attribution_retains_its_creation_description(self):
        order = self._create_order()
        attribution = IgOrderAttribution.objects.create(
            order=order, client=self.ig_client,
            creation_mode='manager_review', payment_source='manager_verified',
        )
        projected, _context = self._presented_order(order)
        self.assertEqual(
            projected.instagram_identity['creation_mode'],
            attribution.get_creation_mode_display(),
        )

    def test_explicit_unlink_does_not_revive_attribution_history(self):
        order = self._create_order()
        IgOrderAttribution.objects.create(
            order=order, client=self.ig_client, creation_mode='linked_existing',
        )
        unlink_order_from_client(
            order, client=self.ig_client, actor=self.manager,
            reason_code='incorrect_client', reason='Manager removed incorrect ownership',
        )
        projected, _context = self._presented_order(order)
        self.assertIsNone(projected.instagram_identity)

    def test_conflicting_historical_owner_is_not_presented_as_current_client(self):
        order = self._create_order()
        other = IgClient.get_or_create_for_sender('foreign-admin-order-customer')
        IgOrderAttribution.objects.create(
            order=order, client=other, creation_mode='linked_existing',
        )
        projected, _context = self._presented_order(order)
        self.assertIsNone(projected.instagram_identity)

    def test_hidden_current_client_is_not_disclosed(self):
        order = self._create_order()
        self.ig_client.hidden_at = timezone.now()
        self.ig_client.save(update_fields=['hidden_at', 'updated_at'])
        projected, _context = self._presented_order(order)
        self.assertIsNone(projected.instagram_identity)

    def test_erasing_current_client_is_not_disclosed_before_hidden_flag_is_set(self):
        order = self._create_order()
        self.ig_client.privacy_erasure_started_at = timezone.now()
        self.ig_client.save(update_fields=['privacy_erasure_started_at', 'updated_at'])
        self.assertIsNone(self.ig_client.hidden_at)
        projected, context = self._presented_order(order)
        self.assertIsNone(projected.instagram_identity)
        html = render_to_string('partials/admin_orders_section.html', context)
        self.assertNotIn('@custom_buyer', html)
        self.assertNotIn(f'client_id={self.ig_client.pk}', html)

    def test_erasing_client_cannot_receive_new_manual_order_from_signed_old_context(self):
        from storefront.views.manual_orders import _resolve_ig_client_context, _manual_order_mutation
        token = signing.dumps({'client_id': self.ig_client.pk}, salt='storefront.manual-order.ig-client')
        stale_client = _resolve_ig_client_context(self.ig_client.pk, token)
        self.ig_client.privacy_erasure_started_at = timezone.now()
        self.ig_client.save(update_fields=['privacy_erasure_started_at', 'updated_at'])
        with self.assertRaises(ValueError):
            _resolve_ig_client_context(self.ig_client.pk, token)
        with self.assertRaises(ValueError):
            with _manual_order_mutation(None, stale_client):
                self.fail('Erasing owner passed the locked manual-order barrier')
        self.assertFalse(Order.objects.exists())
        self.assertFalse(IgOrderAssignment.objects.exists())

    def test_option_text_is_escaped_in_actual_admin_card(self):
        order = self._create_order()
        item = order.items.get()
        item.option_labels = {'Принт': '<script>alert(1)</script>'}
        item.save(update_fields=['option_labels'])
        _projected, context = self._presented_order(order)
        html = render_to_string('partials/admin_orders_section.html', context)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)


class CustomerItemOptionLabelTests(SimpleTestCase):
    def test_only_explicit_human_labels_are_projected(self):
        item = SimpleNamespace(
            fit_option_label='Оверсайз', fit_option_code='oversize',
            option_values={'secret': 'never display', 'primary_reference_message_id': 4321},
            option_labels={
                'Принт': 'Логотип', 'Посадка': 'Оверсайз',
                'primary_reference_message_id': '4321', '_internal': 'private',
                'source_digest': 'abc', 'Empty': ' ', 'Object': {'id': 7},
            },
        )
        self.assertEqual(customer_item_option_labels(item), ['Принт: Логотип'])

    def test_malformed_label_container_has_no_customer_facts(self):
        item = SimpleNamespace(
            fit_option_label='', fit_option_code='', option_labels=['internal'],
        )
        self.assertEqual(customer_item_option_labels(item), [])
