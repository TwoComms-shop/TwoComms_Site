from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from orders.delivery_display import get_order_nova_poshta_point
from orders.telegram_notifications import TelegramNotifier


class ManualDeliveryPresentationTests(SimpleTestCase):
    def test_handover_ignores_stale_carrier_fields_and_escapes_description(self):
        order = SimpleNamespace(delivery_method='handover', handover_details='Петру <біля офісу>', city='Київ', np_office='Поштомат №12345')
        point = get_order_nova_poshta_point(order)
        self.assertEqual(point.kind, 'handover')
        self.assertEqual(point.number, '')
        self.assertEqual(point.city, '')
        self.assertIn('Петру &lt;біля офісу&gt;', point.telegram_text)
        self.assertNotIn('Поштомат', point.telegram_text)
        self.assertNotIn('номер не вказано', point.telegram_text)
        self.assertNotIn('Місто:', point.telegram_pre_lines)

    def test_manual_address_does_not_guess_nova_poshta_branch(self):
        point = get_order_nova_poshta_point(SimpleNamespace(delivery_method='manual', city='Львів', np_office='Пункт видачі №8'))
        self.assertEqual(point.kind, 'manual')
        self.assertEqual(point.number, '')
        self.assertIn('Доставка вручну', point.telegram_text)
        self.assertNotIn('номер не вказано', point.telegram_text)

    def test_legacy_order_defaults_to_nova_poshta(self):
        point = get_order_nova_poshta_point(SimpleNamespace(city='Київ', np_office='Поштомат №12345'))
        self.assertEqual(point.kind, 'postomat')
        self.assertEqual(point.number, '12345')


class ManualFilmTelegramPresentationTests(SimpleTestCase):
    def test_non_carrier_orders_do_not_build_nova_poshta_actions(self):
        notifier = TelegramNotifier()
        for method in ('manual', 'handover'):
            order = SimpleNamespace(pk=1, status='new', delivery_method=method)
            with self.subTest(method=method), patch.object(notifier, '_build_storage_action_button', return_value=None), patch('orders.telegram_notifications.build_order_action_url') as build:
                self.assertIsNone(notifier._build_order_management_reply_markup(order))
                build.assert_not_called()

    def test_handover_preserves_storage_action(self):
        notifier = TelegramNotifier()
        button = {'text': 'Склад', 'url': 'https://example.com/stock'}
        with patch.object(notifier, '_build_storage_action_button', return_value=button):
            result = notifier._build_order_management_reply_markup(SimpleNamespace(pk=1, status='new', delivery_method='handover'))
        self.assertEqual(result, {'inline_keyboard': [[button]]})

    def test_payment_card_uses_persisted_payer_and_method(self):
        snapshot = {
            'pay_type': 'prepayment', 'payment_status': 'prepaid',
            'gross_total_value': Decimal('400'), 'discount_amount_value': Decimal('0'),
            'payable_total_value': Decimal('400'), 'prepayment_amount_value': Decimal('100'),
            'remaining_amount_value': Decimal('300'),
            'paid_amount_value': Decimal('100'), 'cod_enabled': False,
            'cod_amount_value': Decimal('0'), 'delivery_payer_type': 'Sender',
            'delivery_payment_method': 'NonCash',
        }
        order = SimpleNamespace(delivery_method='nova_poshta', payment_payload={'manual_shipment_payment': {
            'payer_type': 'Sender', 'payment_method': 'NonCash', 'cod_enabled': False, 'paid_amount': '100.00',
        }})
        with patch('orders.telegram_notifications.build_order_payment_snapshot', return_value=snapshot):
            message = TelegramNotifier()._format_payment_info(order)
        self.assertIn('Доставку оплачує: Відправник · Безготівково', message)
        self.assertIn('Накладений платіж: ні', message)
        self.assertIn('Внесено: 100 грн', message)
        self.assertIn('Залишок до оплати: 300 грн', message)
        self.assertNotIn('при отриманні', message)

    def test_new_film_card_uses_metres_and_per_metre_price(self):
        item = SimpleNamespace(is_dtf_film=True, film_length_m=Decimal('1.25'), film_length_display='1,25', qty=1, line_total=Decimal('400'), unit_price=Decimal('320'), title='DTF-плівка')
        manager = MagicMock()
        manager.all.return_value = [item]
        manager.count.return_value = 1
        order = SimpleNamespace(order_number='TEST', items=manager, full_name='Клієнт', phone='0500000000', created=datetime(2026, 10, 6), tracking_number='', payment_payload={}, delivery_method='handover', handover_details='Клієнту в офісі', promo_code=None)
        order.get_payment_status_display = lambda: 'Оплачено'
        order.get_status_display = lambda: 'В обробці'
        snapshot = {'gross_total_value': Decimal('400'), 'discount_amount_value': Decimal('0'), 'payable_total_value': Decimal('400')}
        notifier = TelegramNotifier()
        with patch('orders.telegram_notifications.build_order_payment_snapshot', return_value=snapshot), patch.object(notifier, '_format_payment_info', return_value='Оплата'), patch.object(notifier, '_build_prints_summary', return_value=''), patch.object(notifier, '_build_writeoff_status', return_value=''):
            message = notifier.format_order_message(order)
        self.assertIn('1,25 м', message)
        self.assertIn('320 грн/м', message)
        self.assertIn('ширина 60 см', message)
        self.assertIn('Клієнту в офісі', message)
        self.assertNotIn('Количество:', message)
        self.assertNotIn('шт.', message)
        self.assertNotIn('НОВА ПОШТА', message)

    def test_film_edit_diff_shows_length_and_shipment_payment_change(self):
        diff = {'items': {'added': [{'label': 'DTF-плівка', 'qty': 1, 'item_kind': 'dtf_film', 'film_length_m': Decimal('1.25')}], 'changed': [{'label': 'DTF-плівка', 'old_qty': 1, 'new_qty': 1, 'old_price': Decimal('320'), 'new_price': Decimal('330'), 'item_kind': 'dtf_film', 'old_film_length_m': Decimal('1'), 'new_film_length_m': Decimal('1.25')}]}, 'payment': {'old': ('cod', 'unpaid'), 'new': ('prepayment', 'prepaid'), 'new_shipment_payment': {'payer_type': 'Sender', 'payment_method': 'NonCash', 'cod_enabled': False, 'paid_amount': '100.00'}}}
        message = TelegramNotifier().format_order_edit_message(SimpleNamespace(order_number='TEST', delivery_method='nova_poshta'), diff)
        self.assertIn('1.25 м', message)
        self.assertIn('довжина 1 → 1.25 м', message)
        self.assertIn('330 грн/м', message)
        self.assertIn('Доставку оплачує відправник · безготівково', message)
        self.assertNotIn('×1', message)
