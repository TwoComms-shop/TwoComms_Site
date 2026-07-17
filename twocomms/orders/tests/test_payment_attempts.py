from decimal import Decimal
import json
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from orders.models import Order, OrderItem, PaymentAttempt
from storefront.models import Category, Product
from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token


class PaymentAttemptLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='attempt-buyer', password='x')
        category = Category.objects.create(name='Attempt category', slug='attempt-category')
        product = Product.objects.create(
            title='Snapshot shirt', slug='snapshot-shirt', category=category,
            price=900, status='published',
        )
        self.snapshot = {
            'cart': [{
                'product_id': product.pk,
                'title': 'Snapshot shirt',
                'qty': 1,
                'size': 'M',
                'fit_option_code': '',
                'fit_option_label': '',
                'color_variant_id': None,
                'option_values': {},
                'option_labels': {},
                'unit_price': '900.00',
                'line_total': '900.00',
            }],
            'custom_print_lead_ids': [],
        }

    def test_attempt_is_not_an_order_and_final_amount_is_discounted(self):
        attempt = PaymentAttempt.objects.create(
            fingerprint='fingerprint-1',
            user=self.user,
            session_key='session-1',
            full_name='Buyer',
            phone='+380501112233',
            city='Kyiv',
            np_office='Branch 1',
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            cart_snapshot=self.snapshot,
            gross_amount=Decimal('1000.00'),
            discount_amount=Decimal('100.00'),
            payable_amount=Decimal('900.00'),
            payment_amount=Decimal('900.00'),
        )

        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(attempt.final_amount, Decimal('900.00'))
        self.assertEqual(attempt.purchase_event_id, f'attempt-{attempt.pk}-purchase')

    def test_terminal_attempt_cannot_be_converted_twice(self):
        attempt = PaymentAttempt.objects.create(
            fingerprint='fingerprint-2',
            full_name='Buyer', phone='+380501112234', city='Kyiv', np_office='Branch 1',
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.CONVERTED,
            cart_snapshot=self.snapshot,
            gross_amount=Decimal('900.00'),
            payable_amount=Decimal('900.00'),
            payment_amount=Decimal('900.00'),
        )
        self.assertTrue(attempt.is_terminal)
        self.assertFalse(attempt.can_materialize)

    def test_verified_success_materializes_one_order_from_snapshot(self):
        from orders.payment_attempts import materialize_payment_attempt

        attempt = PaymentAttempt.objects.create(
            fingerprint='fingerprint-3',
            user=self.user,
            session_key='session-3',
            full_name='Buyer', phone='+380501112235', city='Kyiv', np_office='Branch 1',
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            cart_snapshot=self.snapshot,
            gross_amount=Decimal('1000.00'),
            discount_amount=Decimal('100.00'),
            payable_amount=Decimal('900.00'),
            payment_amount=Decimal('900.00'),
            monobank_invoice_id='inv-3',
        )

        first, created = materialize_payment_attempt(
            attempt.pk, status='success',
            payload={'status': 'success', 'paidAmount': 90000},
        )
        second, created_again = materialize_payment_attempt(
            attempt.pk, status='success',
            payload={'status': 'success', 'paidAmount': 90000},
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.total_sum, Decimal('1000.00'))
        self.assertEqual(first.discount_amount, Decimal('100.00'))
        self.assertEqual(first.final_total, Decimal('900.00'))
        from orders.facebook_conversions_service import FacebookConversionsService
        self.assertEqual(FacebookConversionsService.__new__(FacebookConversionsService)._extract_paid_amount(first), 900.0)

    def test_invoice_endpoint_only_creates_attempt(self):
        session = self.client.session
        session['cart'] = {
            'line-1': {'product_id': self.snapshot['cart'][0]['product_id'], 'qty': 1, 'size': 'M'},
        }
        session.save()
        payload = {
            'full_name': 'Invoice Buyer',
            'phone': '+380501112236',
            'city': 'Kyiv',
            'np_office': 'Branch 1',
            'np_city_token': build_city_choice_token({'label': 'Kyiv', 'settlement_ref': 'settlement-1', 'city_ref': 'city-1'}),
            'np_warehouse_token': build_warehouse_choice_token({'label': 'Branch 1', 'ref': 'warehouse-1', 'kind': 'branch', 'city_ref': 'city-1'}),
            'pay_type': 'online_full',
        }
        with patch('storefront.views.monobank._monobank_api_request', return_value={
                'invoiceId': 'invoice-attempt-1', 'pageUrl': 'https://pay.example/1',
            }), patch('orders.facebook_conversions_service.get_facebook_conversions_service') as fb, patch(
                'orders.telegram_notifications.TelegramNotifier.send_payment_attempt_notification', return_value=True,
            ):
            fb.return_value.send_add_payment_info_event.return_value = True
            response = self.client.post(
                reverse('monobank_create_invoice'), data=json.dumps(payload),
                content_type='application/json', secure=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(PaymentAttempt.objects.count(), 1)

    def test_invoice_endpoint_rejects_cod_without_fallback(self):
        session = self.client.session
        session['cart'] = {
            'line-1': {'product_id': self.snapshot['cart'][0]['product_id'], 'qty': 1, 'size': 'M'},
        }
        session.save()
        payload = {
            'full_name': 'COD Buyer', 'phone': '+380501112237', 'city': 'Kyiv', 'np_office': 'Branch 1',
            'np_city_token': build_city_choice_token({'label': 'Kyiv', 'settlement_ref': 'settlement-1', 'city_ref': 'city-1'}),
            'np_warehouse_token': build_warehouse_choice_token({'label': 'Branch 1', 'ref': 'warehouse-1', 'kind': 'branch', 'city_ref': 'city-1'}),
            'pay_type': 'cod',
        }
        response = self.client.post(
            reverse('monobank_create_invoice'), data=json.dumps(payload),
            content_type='application/json', secure=True,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PaymentAttempt.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)

    def test_prepaid_order_uses_purchase_event_without_lead(self):
        from storefront.views.utils import _send_post_payment_events

        order = Order.objects.create(
            user=self.user,
            full_name='Buyer', phone='+380501112238', city='Kyiv', np_office='Branch 1',
            pay_type='prepay_200', total_sum=Decimal('1000.00'),
            discount_amount=Decimal('100.00'), payment_status='prepaid',
            payment_payload={},
        )
        fb = Mock(enabled=True)
        fb.send_purchase_event.return_value = True
        fb.send_lead_event.return_value = True
        tiktok = Mock(enabled=False)
        with patch('orders.facebook_conversions_service.get_facebook_conversions_service', return_value=fb), \
                patch('orders.telegram_notifications.TelegramNotifier.send_admin_payment_status_update'), \
                patch('orders.telegram_notifications.TelegramNotifier.send_new_order_notification', return_value=False), \
                patch('orders.tiktok_events_service.get_tiktok_events_service', return_value=tiktok):
            _send_post_payment_events(order.pk, 'unpaid', 'prepay_200')

        fb.send_purchase_event.assert_called_once()
        fb.send_lead_event.assert_not_called()
        order.refresh_from_db()
        self.assertTrue(order.payment_payload['facebook_events']['purchase_sent'])
        self.assertNotIn('lead_sent', order.payment_payload.get('facebook_events', {}))

    def test_failed_attempt_never_creates_order(self):
        from storefront.views.monobank import _apply_payment_attempt_status

        attempt = PaymentAttempt.objects.create(
            fingerprint='fingerprint-failed', full_name='Buyer', phone='+380501112239',
            city='Kyiv', np_office='Branch 1', pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            cart_snapshot=self.snapshot, gross_amount=Decimal('900.00'),
            payable_amount=Decimal('900.00'), payment_amount=Decimal('900.00'),
        )
        _apply_payment_attempt_status(attempt, 'failure', payload={'status': 'failure'})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.FAILED)
        self.assertIsNone(attempt.order_id)
        self.assertFalse(Order.objects.exists())

    def test_success_amount_mismatch_stays_processing(self):
        from storefront.views.monobank import _resolve_attempt_invoice_status

        attempt = PaymentAttempt.objects.create(
            fingerprint='fingerprint-mismatch', full_name='Buyer', phone='+380501112240',
            city='Kyiv', np_office='Branch 1', pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            cart_snapshot=self.snapshot, gross_amount=Decimal('900.00'),
            payable_amount=Decimal('900.00'), payment_amount=Decimal('900.00'),
        )
        with patch('storefront.views.monobank._monobank_api_request', return_value={
            'status': 'success', 'paidAmount': 89999,
        }):
            status, payload = _resolve_attempt_invoice_status(attempt, 'inv-mismatch')
        self.assertEqual(status, 'processing')
        self.assertEqual(payload['paidAmount'], 89999)
        self.assertFalse(Order.objects.exists())

    def test_verified_webhook_materializes_once_and_duplicate_is_noop(self):
        from storefront.views.monobank import monobank_webhook

        attempt = PaymentAttempt.objects.create(
            fingerprint='fingerprint-webhook', full_name='Webhook Buyer',
            phone='+380501112242', city='Kyiv', np_office='Branch 1',
            pay_type=PaymentAttempt.PayType.PREPAY_200, cart_snapshot=self.snapshot,
            gross_amount=Decimal('900.00'), payable_amount=Decimal('900.00'),
            payment_amount=Decimal('200.00'), monobank_invoice_id='inv-webhook',
            status=PaymentAttempt.Status.PROCESSING,
        )
        payload = {
            'invoiceId': 'inv-webhook',
            'result': {'reference': attempt.reference, 'status': 'success'},
        }
        patches = [
            patch('storefront.views.monobank._webhook_signature_ok', return_value=True),
            patch('storefront.views.monobank._monobank_api_request', return_value={
                'status': 'success', 'paidAmount': 20000,
            }),
            patch('storefront.views.monobank._dispatch_post_payment_events'),
        ]
        with patches[0], patches[1], patches[2]:
            first = self.client.post(
                reverse('monobank_webhook'), data=json.dumps(payload),
                content_type='application/json', secure=True,
            )
            second = self.client.post(
                reverse('monobank_webhook'), data=json.dumps(payload),
                content_type='application/json', secure=True,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderItem.objects.count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.PREPAID)
        self.assertIsNotNone(attempt.order_id)

    def test_admin_attempts_view_is_opt_in_and_shows_discount_breakdown(self):
        staff = User.objects.create_user(username='attempt-admin', password='x', is_staff=True)
        PaymentAttempt.objects.create(
            fingerprint='fingerprint-admin', full_name='Admin Buyer', phone='+380501112241',
            city='Kyiv', np_office='Branch 1', pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            cart_snapshot=self.snapshot, gross_amount=Decimal('1000.00'),
            discount_amount=Decimal('100.00'), payable_amount=Decimal('900.00'),
            payment_amount=Decimal('900.00'), status=PaymentAttempt.Status.PROCESSING,
        )
        self.client.force_login(staff)
        normal = self.client.get(reverse('admin_panel'), {'section': 'orders'}, secure=True)
        self.assertEqual(normal.status_code, 200)
        self.assertContains(normal, 'Очікування оплати')
        attempts = self.client.get(
            reverse('admin_panel'),
            {'section': 'orders', 'view': 'payment_attempts'},
            secure=True,
        )
        self.assertEqual(attempts.status_code, 200)
        self.assertContains(attempts, 'Admin Buyer')
        self.assertContains(attempts, '1000')
        self.assertContains(attempts, '900')
