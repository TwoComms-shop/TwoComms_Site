from copy import deepcopy
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, override_settings
from management.models import IgClient, IgCommercialEpisode, IgCommerceSelectionSession, IgCommerceSelectionTransition, IgFunnelResetAudit, InstagramBotMessage
from management.services.ig_journey_cart import selection_cart, summarize_cart, _price


def ready(product=7):
    return {'has_product': True, 'applicability_known': True, 'product': {'id': product, 'title': 'Худі', 'kind': 'Худі'},
            'fit': {}, 'color': {}, 'size': {}, 'options': {}}


class CartTotalsTests(SimpleTestCase):
    @override_settings(FREE_SHIPPING_THRESHOLD='3000')
    def test_quantity_and_distinct_positions_threshold_boundary(self):
        rows = [{'quantity': 2, 'subtotal': '2000.00'}, {'quantity': 1, 'subtotal': '1000.00'}]
        cart = summarize_cart(rows, scope={})
        self.assertEqual((cart['line_count'], cart['item_count']), (2, 3))
        self.assertEqual(cart['estimated_total'], '3000.00')
        self.assertTrue(cart['shipping']['eligible_estimate'])
        rows[1]['subtotal'] = '999.50'
        self.assertEqual(summarize_cart(rows, scope={})['shipping']['remaining'], '0.50')

    def test_unknown_line_never_yields_misleading_total_or_shipping(self):
        cart = summarize_cart([{'quantity': 2, 'subtotal': '4000'}, {'quantity': None, 'subtotal': None}], scope={})
        self.assertIsNone(cart['estimated_total'])
        self.assertIsNone(cart['item_count'])
        self.assertIsNone(cart['shipping']['remaining'])
        self.assertFalse(cart['shipping']['eligible_estimate'])

    @override_settings(FREE_SHIPPING_THRESHOLD='2750')
    def test_threshold_comes_from_checkout(self):
        self.assertEqual(summarize_cart([{'quantity': 1, 'subtotal': '2600'}], scope={})['shipping']['remaining'], '150')


class CurrentCartTests(TestCase):
    def setUp(self):
        self.customer = IgClient.get_or_create_for_sender('journey-cart')
        self.episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1, materialization_key='cart-1')
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=['current_commercial_episode'])
        self.lines = [{'line_id': 'a', 'product_id': 7, 'quantity': 2}, {'line_id': 'b', 'product_id': 7, 'quantity': 1}, {'line_id': 'c', 'product_id': 8, 'quantity': 1}]
        self.session = IgCommerceSelectionSession.objects.create(client=self.customer, commercial_episode=self.episode, generation=1, lines=self.lines, revision=1, active_index=2)
        self.message = InstagramBotMessage.objects.create(client=self.customer, sender_id='journey-cart', role='user', text='дві такі, ще одну та іншу')
        after = self.session.snapshot()
        before = deepcopy(after); before.update(lines=[], revision=0)
        self.transition = IgCommerceSelectionTransition.objects.create(session=self.session, source_message=self.message, action='product_selected', from_revision=0, to_revision=1, previous_snapshot=before, next_snapshot=after, source_order_key='1')

    def project(self):
        return selection_cart(client_id=self.customer.pk, episode_id=self.episode.pk)

    @patch('management.services.ig_journey_cart._price', return_value='1000.00')
    @patch('management.services.ig_journey_readiness.selection_readiness', side_effect=lambda **kw: ready(kw['product_id']))
    def test_distinct_identical_lines_preserved_quantity_cached_and_active(self, read, price):
        result = self.project()
        self.assertEqual((result['line_count'], result['item_count'], result['ready_count']), (3, 4, 3))
        self.assertEqual([line['line_id'] for line in result['lines']], ['a', 'b', 'c'])
        self.assertTrue(result['lines'][2]['active'])
        self.assertEqual(result['estimated_total'], '4000.00')
        self.assertEqual(read.call_count, 2)

    @patch('management.services.ig_journey_cart._price', return_value='1000.00')
    @patch('management.services.ig_journey_readiness.selection_readiness', return_value=ready())
    def test_unproven_or_missing_quantity_is_not_guessed(self, read, price):
        # Session-only edit without a corresponding transition invalidates all evidence.
        self.session.lines[0]['quantity'] = 9
        self.session.save(update_fields=['lines'])
        self.assertIsNone(self.project())
        self.session.lines[0].pop('quantity')
        self.session.revision = 2
        self.session.save(update_fields=['lines', 'revision'])
        fresh = InstagramBotMessage.objects.create(client=self.customer, sender_id='journey-cart', role='user', text='кількість уточню')
        IgCommerceSelectionTransition.objects.create(session=self.session, source_message=fresh, action='quantity_changed', from_revision=1, to_revision=2, previous_snapshot=IgCommerceSelectionTransition.objects.values_list('next_snapshot', flat=True).get(pk=self.transition.pk), next_snapshot=self.session.snapshot(), source_order_key='2')
        cart = self.project()
        self.assertIsNone(cart['lines'][0]['quantity'])
        self.assertIsNone(cart['estimated_total'])

    def test_reset_history_foreign_lines_and_erasure_are_fenced(self):
        self.assertIsNone(selection_cart(client_id=self.customer.pk, episode_id=self.episode.pk+1))
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.message.pk)
        self.assertIsNone(self.project())

    @patch('management.services.ig_journey_cart._price', return_value='1000.00')
    def test_concurrent_change_discards_cart(self, price):
        def mutate(**kwargs):
            from management.services import ig_journey_readiness as scope
            with patch.object(scope.connection, 'execute_wrappers', []):
                IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(revision=2)
            return ready()
        with patch('management.services.ig_journey_readiness.selection_readiness', side_effect=mutate):
            self.assertIsNone(self.project())

    def test_real_catalog_price_is_same_checkout_calculation(self):
        from storefront.models import Product, Category, ProductStatus
        from management.services.ig_journey_selection import selection_fields
        category = Category.objects.create(name='Худі', slug='cart-price')
        product = Product.objects.create(title='Худі', slug='cart-price', category=category, price=1500, status=ProductStatus.PUBLISHED)
        state = ready(product.pk)
        fields = selection_fields(state, scope={}, evidence_refs=[{'kind': 'message', 'id': self.message.pk}])
        self.assertEqual(_price(state, fields), '1500.00')
        fields['total'] = None
        self.assertIsNone(_price(state, fields))

    def test_catalog_budget_bounds_queries_and_keeps_unknown_lines(self):
        from django.db import connection
        def heavy(**kwargs):
            for _ in range(170):
                with connection.cursor() as cursor:
                    cursor.execute('SELECT 1')
            return ready()
        with patch('management.services.ig_journey_readiness.selection_readiness', side_effect=heavy):
            cart = self.project()
        self.assertEqual(len(cart['lines']), 3)
        self.assertTrue(all(line['fields'] is None for line in cart['lines']))
        self.assertIsNone(cart['estimated_total'])
        self.assertTrue(all(line['reason'] == 'catalog_read_budget' for line in cart['lines']))
