import json
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from orders.models import Order
from orders.nova_poshta_documents import normalize_checkout_phone, normalize_phone, normalize_phone_for_np
from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from storefront.custom_print_config import SESSION_CUSTOM_CART_KEY
from storefront.models import Category, CustomPrintLead, CustomPrintModerationStatus, Product


class PhoneNormalizationTests(TestCase):
    def test_normalize_phone_accepts_common_ukrainian_shortcuts(self):
        samples = (
            '0939693920',
            '80939693920',
            '8939693920',
            '939693920',
            '380939693920',
            '+380939693920',
            '00380939693920',
        )

        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(normalize_phone(sample), '+380939693920')
                self.assertEqual(normalize_checkout_phone(sample), '+380939693920')
                self.assertEqual(normalize_phone_for_np(sample), '380939693920')

    def test_normalize_phone_preserves_explicit_international_number(self):
        self.assertEqual(normalize_phone('+55 11 91234-5678'), '+5511912345678')
        self.assertEqual(normalize_phone('0055 11 91234-5678'), '+5511912345678')

    def test_normalize_phone_does_not_reinterpret_explicit_short_e164(self):
        self.assertEqual(normalize_phone('+380808020'), '')
        self.assertEqual(normalize_checkout_phone('+380808020'), '')
        self.assertEqual(normalize_phone_for_np('+380808020'), '')

    def test_checkout_phone_remains_ukraine_only_until_nova_poshta_waybill_support_is_confirmed(self):
        self.assertEqual(normalize_phone('+1 (202) 555-0125'), '+12025550125')
        self.assertEqual(normalize_checkout_phone('+1 (202) 555-0125'), '')
        self.assertEqual(normalize_phone_for_np('+1 (202) 555-0125'), '')


class NovaPoshtaCheckoutValidationTests(TestCase):
    def setUp(self):
        self.feed_task_patcher = patch('storefront.signals.generate_google_merchant_feed_task.apply_async')
        self.feed_task_patcher.start()
        self.addCleanup(self.feed_task_patcher.stop)

        self.cart_url = reverse('cart')
        self.order_create_url = reverse('order_create')
        self.monobank_create_invoice_url = reverse('monobank_create_invoice')
        self.profile_setup_url = reverse('profile_setup')

        self.category = Category.objects.create(name='Test Category', slug='test-category')
        self.product = Product.objects.create(
            title='Test Product',
            slug='test-product',
            category=self.category,
            price=100,
        )

        self.user = User.objects.create_user(
            username='np-user',
            email='np@example.com',
            password='testpass123',
        )
        self.profile = UserProfile.objects.get(user=self.user)
        self.profile.phone = '+380991234567'
        self.profile.full_name = 'Stored User'
        self.profile.city = 'Старе місто'
        self.profile.np_office = 'Старе відділення'
        self.profile.pay_type = 'online_full'
        self.profile.save()

    def _set_cart(self):
        session = self.client.session
        session['cart'] = {
            'line-1': {
                'product_id': self.product.id,
                'qty': 2,
                'size': 'M',
            }
        }
        session.save()

    def _delivery_payload(self):
        city_label = 'м. Київ, Київ'
        city_ref = 'delivery-city-ref'
        settlement_ref = 'settlement-ref'
        warehouse_label = 'Відділення №22, Київ, вул. Тестова, 1'
        return {
            'city': 'довільний текст, який має бути проігнорований',
            'np_office': 'ще один довільний текст',
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
                    'ref': 'warehouse-ref',
                    'kind': 'branch',
                    'city_ref': city_ref,
                }
            ),
            'canonical_city': city_label,
            'canonical_np_office': warehouse_label,
        }

    def test_profile_update_requires_signed_nova_poshta_selection(self):
        self.client.login(username='np-user', password='testpass123')

        response = self.client.post(
            self.cart_url,
            {
                'form_type': 'update_profile',
                'full_name': 'Updated User',
                'phone': '+380661112233',
                'city': 'Київ',
                'np_office': 'Відділення №1',
                'pay_type': 'online_full',
            },
            secure=True,
            follow=True,
        )

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.full_name, 'Stored User')
        self.assertEqual(self.profile.city, 'Старе місто')
        self.assertEqual(self.profile.np_office, 'Старе відділення')
        messages = [message.message for message in response.context['messages']]
        self.assertIn('Оберіть місто зі списку Нової пошти.', messages)

    def test_profile_update_saves_canonical_nova_poshta_values(self):
        self.client.login(username='np-user', password='testpass123')
        delivery = self._delivery_payload()

        response = self.client.post(
            self.cart_url,
            {
                'form_type': 'update_profile',
                'full_name': 'Updated User',
                'phone': '+380661112233',
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
            follow=True,
        )

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.full_name, 'Updated User')
        self.assertEqual(self.profile.phone, '+380661112233')
        self.assertEqual(self.profile.city, delivery['canonical_city'])
        self.assertEqual(self.profile.np_office, delivery['canonical_np_office'])
        self.assertEqual(self.profile.np_settlement_ref, 'settlement-ref')
        self.assertEqual(self.profile.np_city_ref, 'delivery-city-ref')
        self.assertEqual(self.profile.np_warehouse_ref, 'warehouse-ref')
        messages = [message.message for message in response.context['messages']]
        self.assertIn('Дані доставки успішно оновлено!', messages)

    def test_profile_update_normalizes_ukrainian_phone_without_country_code(self):
        self.client.login(username='np-user', password='testpass123')
        delivery = self._delivery_payload()

        response = self.client.post(
            self.cart_url,
            {
                'form_type': 'update_profile',
                'full_name': 'Updated User',
                'phone': '0939693920',
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
            follow=True,
        )

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone, '+380939693920')
        messages = [message.message for message in response.context['messages']]
        self.assertIn('Дані доставки успішно оновлено!', messages)

    def test_profile_update_rejects_foreign_phone_for_delivery_flow(self):
        self.client.login(username='np-user', password='testpass123')
        delivery = self._delivery_payload()

        response = self.client.post(
            self.cart_url,
            {
                'form_type': 'update_profile',
                'full_name': 'Updated User',
                'phone': '+5511912345678',
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
            follow=True,
        )

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone, '+380991234567')
        messages = [message.message for message in response.context['messages']]
        self.assertIn('Вкажіть коректний український номер телефону. Можна без +380.', messages)

    def test_profile_setup_allows_blank_delivery_fields(self):
        self.client.login(username='np-user', password='testpass123')

        response = self.client.post(
            self.profile_setup_url,
            {
                'full_name': 'Updated User',
                'phone': '0939693920',
                'email': 'updated@example.com',
                'telegram': '@updated_user',
                'instagram': '@updated_insta',
                'city': '',
                'np_office': '',
                'pay_type': 'partial',
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, '')
        self.assertEqual(self.profile.np_office, '')
        self.assertEqual(self.profile.np_settlement_ref, '')
        self.assertEqual(self.profile.np_city_ref, '')
        self.assertEqual(self.profile.np_warehouse_ref, '')
        self.assertEqual(self.profile.phone, '+380939693920')

    def test_profile_setup_requires_signed_nova_poshta_selection_when_delivery_present(self):
        self.client.login(username='np-user', password='testpass123')

        response = self.client.post(
            self.profile_setup_url,
            {
                'full_name': 'Updated User',
                'phone': '+380661112233',
                'city': 'Київ',
                'np_office': 'Відділення №1',
                'pay_type': 'partial',
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, 'Старе місто')
        self.assertEqual(self.profile.np_office, 'Старе відділення')
        self.assertFormError(
            response.context['form'],
            'city',
            'Оберіть місто зі списку Нової пошти.',
        )

    def test_profile_setup_saves_canonical_nova_poshta_values(self):
        self.client.login(username='np-user', password='testpass123')
        delivery = self._delivery_payload()

        response = self.client.post(
            self.profile_setup_url,
            {
                'full_name': 'Updated User',
                'phone': '+380661112233',
                'city': delivery['city'],
                'np_office': delivery['np_office'],
                'np_settlement_ref': delivery['np_settlement_ref'],
                'np_city_ref': delivery['np_city_ref'],
                'np_city_token': delivery['np_city_token'],
                'np_warehouse_ref': delivery['np_warehouse_ref'],
                'np_warehouse_token': delivery['np_warehouse_token'],
                'pay_type': 'partial',
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.city, delivery['canonical_city'])
        self.assertEqual(self.profile.np_office, delivery['canonical_np_office'])
        self.assertEqual(self.profile.np_settlement_ref, 'settlement-ref')
        self.assertEqual(self.profile.np_city_ref, 'delivery-city-ref')
        self.assertEqual(self.profile.np_warehouse_ref, 'warehouse-ref')

    def test_order_create_requires_signed_nova_poshta_selection(self):
        self._set_cart()

        response = self.client.post(
            self.order_create_url,
            {
                'full_name': 'Guest User',
                'phone': '+380991234567',
                'city': 'Київ',
                'np_office': 'Відділення №1',
                'pay_type': 'cash',
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(Order.objects.count(), 0)
        messages = [message.message for message in response.context['messages']]
        self.assertIn('Оберіть місто зі списку Нової пошти.', messages)

    def test_order_create_uses_signed_delivery_choice_instead_of_raw_text(self):
        self._set_cart()
        delivery = self._delivery_payload()

        response = self.client.post(
            self.order_create_url,
            {
                'full_name': 'Guest User',
                'phone': '+380991234567',
                'city': delivery['city'],
                'np_office': delivery['np_office'],
                'np_settlement_ref': delivery['np_settlement_ref'],
                'np_city_ref': delivery['np_city_ref'],
                'np_city_token': delivery['np_city_token'],
                'np_warehouse_ref': delivery['np_warehouse_ref'],
                'np_warehouse_token': delivery['np_warehouse_token'],
                'pay_type': 'cash',
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.city, delivery['canonical_city'])
        self.assertEqual(order.np_office, delivery['canonical_np_office'])
        self.assertEqual(order.np_settlement_ref, 'settlement-ref')
        self.assertEqual(order.np_city_ref, 'delivery-city-ref')
        self.assertEqual(order.np_warehouse_ref, 'warehouse-ref')
        self.assertEqual(order.phone, '+380991234567')

    def test_order_create_normalizes_phone_without_country_code(self):
        self._set_cart()
        delivery = self._delivery_payload()

        response = self.client.post(
            self.order_create_url,
            {
                'full_name': 'Guest User',
                'phone': '80939693920',
                'city': delivery['city'],
                'np_office': delivery['np_office'],
                'np_settlement_ref': delivery['np_settlement_ref'],
                'np_city_ref': delivery['np_city_ref'],
                'np_city_token': delivery['np_city_token'],
                'np_warehouse_ref': delivery['np_warehouse_ref'],
                'np_warehouse_token': delivery['np_warehouse_token'],
                'pay_type': 'cash',
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        order = Order.objects.get()
        self.assertEqual(order.phone, '+380939693920')

    def test_order_create_rejects_explicit_foreign_phone(self):
        self._set_cart()
        delivery = self._delivery_payload()

        response = self.client.post(
            self.order_create_url,
            {
                'full_name': 'Guest User',
                'phone': '+5511912345678',
                'city': delivery['city'],
                'np_office': delivery['np_office'],
                'np_settlement_ref': delivery['np_settlement_ref'],
                'np_city_ref': delivery['np_city_ref'],
                'np_city_token': delivery['np_city_token'],
                'np_warehouse_ref': delivery['np_warehouse_ref'],
                'np_warehouse_token': delivery['np_warehouse_token'],
                'pay_type': 'cash',
            },
            secure=True,
            follow=True,
        )

        self.assertEqual(Order.objects.count(), 0)
        messages = [message.message for message in response.context['messages']]
        self.assertIn('Вкажіть коректний український номер телефону. Можна без +380.', messages)

    def test_monobank_create_invoice_rejects_unsigned_delivery_payload(self):
        self._set_cart()

        response = self.client.post(
            self.monobank_create_invoice_url,
            data=json.dumps(
                {
                    'full_name': 'Guest User',
                    'phone': '+380991234567',
                    'city': 'Київ',
                    'np_office': 'Відділення №1',
                    'pay_type': 'online_full',
                }
            ),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            secure=True,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                'success': False,
                'field': 'city',
                'error': 'Оберіть місто зі списку Нової пошти.',
            },
        )

    def test_monobank_create_invoice_rejects_foreign_phone_in_checkout_flow(self):
        self._set_cart()
        delivery = self._delivery_payload()

        response = self.client.post(
            self.monobank_create_invoice_url,
            data=json.dumps(
                {
                    'full_name': 'Guest User',
                    'phone': '+5511912345678',
                    'city': delivery['city'],
                    'np_office': delivery['np_office'],
                    'np_settlement_ref': delivery['np_settlement_ref'],
                    'np_city_ref': delivery['np_city_ref'],
                    'np_city_token': delivery['np_city_token'],
                    'np_warehouse_ref': delivery['np_warehouse_ref'],
                    'np_warehouse_token': delivery['np_warehouse_token'],
                    'pay_type': 'online_full',
                }
            ),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            secure=True,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {
                'success': False,
                'field': 'phone',
                'error': 'Вкажіть коректний український номер телефону. Можна без +380.',
            },
        )

    @patch('storefront.views.monobank.get_facebook_conversions_service')
    @patch('orders.telegram_notifications.TelegramNotifier.send_new_order_notification')
    @patch('storefront.views.monobank.record_lead')
    @patch('storefront.views.monobank.record_initiate_checkout')
    @patch('storefront.views.monobank.link_order_to_utm')
    @patch('storefront.views.monobank._monobank_api_request')
    def test_monobank_create_invoice_includes_approved_custom_print_without_regular_cart(
        self,
        monobank_request_mock,
        _link_order_mock,
        _checkout_mock,
        _record_lead_mock,
        _notify_mock,
        facebook_service_mock,
    ):
        delivery = self._delivery_payload()
        lead = CustomPrintLead.objects.create(
            service_kind='ready',
            product_type='hoodie',
            placements=['front'],
            quantity=2,
            client_kind='personal',
            size_mode='single',
            pricing_snapshot_json={'final_total': '2450.50', 'product_label': 'Худі'},
            name='Custom Client',
            contact_channel='telegram',
            contact_value='@custom_client',
            brief='Кастомний худі',
            source='custom_print_cart',
            moderation_status=CustomPrintModerationStatus.APPROVED,
            approved_price='2450.50',
        )
        session = self.client.session
        session[SESSION_CUSTOM_CART_KEY] = {
            f'custom:{lead.pk}': {
                'lead_id': lead.pk,
                'moderation_status': CustomPrintModerationStatus.APPROVED,
            }
        }
        session.save()

        monobank_request_mock.return_value = {
            'invoiceId': 'mono-invoice-1',
            'pageUrl': 'https://pay.monobank.test/invoice-1',
        }
        facebook_service = Mock()
        facebook_service_mock.return_value = facebook_service

        response = self.client.post(
            self.monobank_create_invoice_url,
            data=json.dumps(
                {
                    'full_name': 'Guest User',
                    'phone': '0991112233',
                    'city': delivery['city'],
                    'np_office': delivery['np_office'],
                    'np_settlement_ref': delivery['np_settlement_ref'],
                    'np_city_ref': delivery['np_city_ref'],
                    'np_city_token': delivery['np_city_token'],
                    'np_warehouse_ref': delivery['np_warehouse_ref'],
                    'np_warehouse_token': delivery['np_warehouse_token'],
                    'pay_type': 'online_full',
                }
            ),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])

        order = Order.objects.get()
        lead.refresh_from_db()
        self.assertEqual(order.phone, '+380991112233')
        self.assertEqual(order.total_sum, Decimal('2450.50'))
        self.assertEqual(order.items.count(), 0)
        self.assertEqual(lead.order_id, order.pk)
        self.assertEqual(order.payment_payload['custom_print_lead_ids'], [lead.pk])
        self.assertEqual(order.payment_status, 'checking')
        monobank_request_mock.assert_called_once()
        facebook_service.send_add_payment_info_event.assert_called_once()
