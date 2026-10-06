from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from orders.models import Order, OrderItem
from orders.telegram_notifications import TelegramNotifier
from productcolors.models import Color
from warehouse.models import StockItem, StorageCategory, StorageSubcategory, WriteOffRequest
from warehouse.services.matching import find_stock_items_for_order_item, order_has_dtf_film


class FilmStockMatcherTests(SimpleTestCase):
    def test_film_does_not_fall_back_to_all_garment_stock(self):
        with patch('warehouse.services.matching.StockItem.objects.select_related') as query:
            self.assertEqual(find_stock_items_for_order_item(SimpleNamespace(item_kind='dtf_film')), [])
        query.assert_not_called()

    def test_mixed_order_is_also_excluded_from_garment_writeoff(self):
        self.assertTrue(order_has_dtf_film(SimpleNamespace(items=[SimpleNamespace(item_kind='clothing'), SimpleNamespace(item_kind='dtf_film')])))
        self.assertFalse(order_has_dtf_film(SimpleNamespace(items=[SimpleNamespace(item_kind='clothing')])))


@override_settings(ROOT_URLCONF='twocomms.urls_storage', SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver', 'storage.twocomms.shop'])
class FilmWriteOffRouteTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('film_stock_admin', 'film@example.com', 'pw')
        self.client.force_login(self.user)
        color = Color.objects.create(name='Film guard black', primary_hex='#000000')
        category = StorageCategory.objects.create(name='Garments', slug='film-guard-garments')
        sub = StorageSubcategory.objects.create(category=category, name='Classic')
        self.stock = StockItem.objects.create(subcategory=sub, color=color, size='M', quantity=5)
        self.order = Order.objects.create(full_name='Клієнт', phone='+380501112233', city='', np_office='', delivery_method='handover', handover_details='Клієнту в офісі', total_sum=400)
        self.item = OrderItem.objects.create(order=self.order, item_kind='dtf_film', film_length_m=Decimal('1.25'), unit_price=320, line_total=400, title='DTF')
        self.wo = WriteOffRequest.objects.create(order=self.order)

    def test_existing_token_get_is_blocked_before_opening_or_garment_choices(self):
        response = self.client.get(f'/order/{self.wo.token}/write-off/', HTTP_HOST='storage.twocomms.shop')
        self.assertEqual(response.status_code, 409)
        self.assertIn('DTF-плівка не є одягом', response.content.decode())
        self.wo.refresh_from_db()
        self.assertIsNone(self.wo.opened_at)
        self.assertEqual(self.wo.status, WriteOffRequest.STATUS_PENDING)

    def test_forged_post_cannot_deduct_garments_or_complete_existing_request(self):
        prefix = f'item_{self.item.pk}_'
        with patch('warehouse.views.write_off.adjust_stock_item') as adjust, patch('warehouse.views.write_off.adjust_print_variant') as adjust_print:
            response = self.client.post(f'/order/{self.wo.token}/write-off/submit/', {prefix+'stock_id': self.stock.pk, prefix+'qty': 1}, HTTP_HOST='storage.twocomms.shop')
        self.assertEqual(response.status_code, 409)
        adjust.assert_not_called()
        adjust_print.assert_not_called()
        self.stock.refresh_from_db()
        self.wo.refresh_from_db()
        self.assertEqual(self.stock.quantity, 5)
        self.assertEqual(self.wo.status, WriteOffRequest.STATUS_PENDING)
        self.assertIsNone(self.wo.completed_at)
        self.assertFalse(self.wo.movements.exists())

    def test_mixed_order_blocks_before_first_garment_deduction(self):
        garment = OrderItem.objects.create(order=self.order, title='Custom garment', unit_price=100, line_total=100, qty=1, is_custom=True)
        prefix = f'item_{garment.pk}_'
        with patch('warehouse.views.write_off.adjust_stock_item') as adjust:
            response = self.client.post(f'/order/{self.wo.token}/write-off/submit/', {prefix+'stock_id': self.stock.pk, prefix+'qty': 1}, HTTP_HOST='storage.twocomms.shop')
        self.assertEqual(response.status_code, 409)
        adjust.assert_not_called()
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WriteOffRequest.STATUS_PENDING)

    def test_telegram_stock_button_is_absent_for_film(self):
        with patch('warehouse.services.order_links.build_storage_writeoff_url') as build:
            self.assertIsNone(TelegramNotifier()._build_storage_action_button(self.order))
        build.assert_not_called()

    @override_settings(ROOT_URLCONF='twocomms.urls')
    def test_staff_entry_does_not_issue_film_stock_url(self):
        from django.urls import reverse
        with patch('storefront.views.admin_order_actions.build_storage_writeoff_url') as build:
            response = self.client.get(reverse('admin_order_warehouse_action', args=[self.order.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('section=orders', response.url)
        build.assert_not_called()
