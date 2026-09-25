"""Тести view редагування замовлення в кастомній адмінці.

Покривають: JSON-ендпоінт даних для drawer (``manual_order_edit_data``)
та відправку diff-сповіщення в Telegram при збереженні змін.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from product_catalog.models import VariantFitRule, VariantSizeRule
from orders.models import Order, OrderItem
from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from productcolors.models import Color, ProductColorVariant
from storefront.models import (
    Category,
    Product,
    ProductFitOption,
    ProductStatus,
    SiteSession,
    UTMSession,
    UserAction,
)

User = get_user_model()


class OrderEditViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username='admin-edit', password='pass12345', is_staff=True)
        cls.category = Category.objects.create(name='Футболки', slug='tshirts-edit')
        cls.product = Product.objects.create(
            title='Футболка Reality Bends', slug='reality-bends-edit',
            category=cls.category, price=880, status=ProductStatus.PUBLISHED,
        )
        cls.mint = Color.objects.create(name='Ментол', primary_hex='#9FE2BF')
        cls.black = Color.objects.create(name='Чорний', primary_hex='#000000')
        cls.variant_mint = ProductColorVariant.objects.create(
            product=cls.product, color=cls.mint, is_default=True)
        cls.variant_black = ProductColorVariant.objects.create(
            product=cls.product, color=cls.black)
        cls.classic_fit = ProductFitOption.objects.create(
            product=cls.product,
            code='classic',
            label='Класична',
            is_default=True,
            order=10,
        )
        cls.oversize_fit = ProductFitOption.objects.create(
            product=cls.product,
            code='oversize',
            label='Оверсайз',
            order=20,
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.order = Order.objects.create(
            full_name='Лагош Олег', phone='+380500234363',
            city='Харків', np_office='Відділення №4',
            pay_type='online_full', payment_status='paid',
            total_sum=Decimal('880.00'), source='web',
        )
        OrderItem.objects.create(
            order=self.order, product=self.product, color_variant=self.variant_mint,
            title=self.product.title, size='XXL', qty=1,
            fit_option_code='classic', fit_option_label='Класична',
            unit_price=Decimal('880.00'), line_total=Decimal('880.00'),
        )

    def test_edit_data_endpoint_returns_order_and_catalog(self):
        url = reverse('manual_order_edit_data', args=[self.order.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['order']['id'], self.order.id)
        self.assertEqual(len(data['order']['items']), 1)
        self.assertEqual(data['order']['items'][0]['fit_option_code'], 'classic')
        self.assertEqual(data['order']['items'][0]['fit_option_label'], 'Класична')
        self.assertTrue(any(p['id'] == self.product.id for p in data['products']))

    def test_free_payment_preset_round_trips_without_becoming_paid_full(self):
        self.order.source = 'manual'
        self.order.payment_payload = {'manual_payment_preset': 'free'}
        self.order.save(update_fields=['source', 'payment_payload'])

        response = self.client.get(reverse('manual_order_edit_data', args=[self.order.id]))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['order']['payment_preset'], 'free')

    def test_dynamic_manager_prepayment_survives_ordinary_order_edit(self):
        from orders.nova_poshta_documents import build_order_payment_snapshot

        self.order.source = 'manual'
        self.order.pay_type = 'prepayment'
        self.order.payment_status = 'unpaid'
        self.order.payment_payload = {
            'manual_payment_preset': 'manager_prepayment',
            'manual_payment_evidence_confirmed': True,
            'manager_confirmed_amount': '315.00',
            'manager_verification_scope': 'prepayment',
        }
        self.order.save(update_fields=[
            'source', 'pay_type', 'payment_status', 'payment_payload',
        ])
        initial = self.client.get(reverse('manual_order_edit_data', args=[self.order.id])).json()['order']
        payload = {
            'full_name': self.order.full_name,
            'phone': self.order.phone,
            'delivery_method': 'keep',
            'payment_preset': initial['payment_preset'],
            'items': initial['items'],
        }

        response, _ = self._edit(payload)

        self.assertEqual(response.status_code, 200, response.content)
        self.order.refresh_from_db()
        self.assertEqual(self.order.pay_type, 'prepayment')
        self.assertEqual(
            self.order.payment_payload['manual_payment_preset'],
            'manager_prepayment',
        )
        snapshot = build_order_payment_snapshot(self.order)
        self.assertEqual(snapshot['paid_amount'], '315.00')
        self.assertEqual(snapshot['cod_amount'], '565.00')

    def test_edit_data_endpoint_requires_staff(self):
        self.client.logout()
        plain = User.objects.create_user(username='plain-edit', password='pass12345')
        self.client.force_login(plain)
        url = reverse('manual_order_edit_data', args=[self.order.id])
        response = self.client.get(url)
        self.assertIn(response.status_code, (302, 403))

    def _edit(self, payload):
        url = reverse('manual_order_edit', args=[self.order.id])
        with mock.patch(
            'storefront.views.manual_orders.telegram_notifier.update_order_notification_message',
            return_value=True,
        ), mock.patch(
            'storefront.views.manual_orders.telegram_notifier.send_order_edit_notification',
            return_value=True,
        ) as edit_notify:
            response = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        return response, edit_notify

    def _delivery_edit_payload(self, **delivery_fields):
        return {
            'full_name': self.order.full_name,
            'phone': self.order.phone,
            'delivery_method': 'keep',
            'payment_preset': 'paid_full',
            'items': [{
                'kind': 'catalog',
                'item_id': self.order.items.get().id,
                'product_id': self.product.id,
                'color_variant_id': self.variant_mint.id,
                'fit_option_code': 'classic',
                'size': 'XXL',
                'qty': 1,
                'unit_price': 880,
            }],
            **delivery_fields,
        }

    def test_keep_delivery_preserves_historical_address_refs_and_tracking(self):
        self.order.np_settlement_ref = 'historical-settlement'
        self.order.np_city_ref = 'historical-city'
        self.order.np_warehouse_ref = 'historical-warehouse'
        self.order.tracking_number = '20450000000001'
        self.order.nova_poshta_document_ref = 'existing-document'
        self.order.save(update_fields=[
            'np_settlement_ref', 'np_city_ref', 'np_warehouse_ref',
            'tracking_number', 'nova_poshta_document_ref',
        ])

        response, edit_notify = self._edit(self._delivery_edit_payload(manager_comment='Уточнено'))

        self.assertEqual(response.status_code, 200, response.content)
        self.order.refresh_from_db()
        self.assertEqual((self.order.city, self.order.np_office), ('Харків', 'Відділення №4'))
        self.assertEqual(
            (self.order.np_settlement_ref, self.order.np_city_ref, self.order.np_warehouse_ref),
            ('historical-settlement', 'historical-city', 'historical-warehouse'),
        )
        self.assertEqual(self.order.tracking_number, '20450000000001')
        self.assertEqual(self.order.nova_poshta_document_ref, 'existing-document')
        self.assertIsNone(edit_notify.call_args.args[1]['delivery'])

    def test_signed_np_delivery_changes_address_refs_and_diff_but_preserves_tracking(self):
        city = {'label': 'Київ', 'settlement_ref': 'settlement-kyiv', 'city_ref': 'city-kyiv'}
        warehouse = {'label': 'Поштомат №17', 'ref': 'warehouse-17', 'kind': 'postomat', 'city_ref': 'city-kyiv'}
        self.order.tracking_number = '20450000000001'
        self.order.nova_poshta_document_ref = 'existing-document'
        self.order.status = 'ship'
        self.order.save(update_fields=['tracking_number', 'nova_poshta_document_ref', 'status'])
        payload = self._delivery_edit_payload(
            delivery_method='np', city=city['label'], np_office=warehouse['label'],
            np_settlement_ref=city['settlement_ref'], np_city_ref=city['city_ref'],
            np_city_token=build_city_choice_token(city),
            np_warehouse_ref=warehouse['ref'],
            np_warehouse_token=build_warehouse_choice_token(warehouse),
        )

        response, edit_notify = self._edit(payload)

        self.assertEqual(response.status_code, 200, response.content)
        self.order.refresh_from_db()
        self.assertEqual((self.order.city, self.order.np_office), ('Київ', 'Поштомат №17'))
        self.assertEqual(
            (self.order.np_settlement_ref, self.order.np_city_ref, self.order.np_warehouse_ref),
            ('settlement-kyiv', 'city-kyiv', 'warehouse-17'),
        )
        self.assertEqual(self.order.tracking_number, '20450000000001')
        self.assertEqual(self.order.nova_poshta_document_ref, 'existing-document')
        self.assertEqual(self.order.status, 'ship')
        self.assertEqual(edit_notify.call_args.args[1]['delivery'], {
            'old': 'Харків, Відділення №4', 'new': 'Київ, Поштомат №17',
        })

    def test_manual_delivery_replaces_address_and_clears_np_refs(self):
        self.order.np_city_ref = 'historical-city'
        self.order.np_warehouse_ref = 'historical-warehouse'
        self.order.save(update_fields=['np_city_ref', 'np_warehouse_ref'])

        response, edit_notify = self._edit(self._delivery_edit_payload(
            delivery_method='manual', city='Львів', np_office='вул. Шевченка, 2',
        ))

        self.assertEqual(response.status_code, 200, response.content)
        self.order.refresh_from_db()
        self.assertEqual((self.order.city, self.order.np_office), ('Львів', 'вул. Шевченка, 2'))
        self.assertEqual(
            (self.order.np_settlement_ref, self.order.np_city_ref, self.order.np_warehouse_ref),
            ('', '', ''),
        )
        self.assertEqual(edit_notify.call_args.args[1]['delivery']['new'], 'Львів, вул. Шевченка, 2')

    def test_invalid_np_selection_leaves_order_and_items_unchanged(self):
        before_item_id = self.order.items.get().id
        payload = self._delivery_edit_payload(
            delivery_method='np', city='Київ', np_office='Поштомат №17',
            np_city_token=build_city_choice_token({
                'label': 'Київ', 'settlement_ref': 'settlement-kyiv', 'city_ref': 'city-kyiv',
            }),
            np_warehouse_token='tampered', manager_comment='Має не зберегтися',
        )

        response, edit_notify = self._edit(payload)

        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(response.json()['field'], 'np_office')
        self.order.refresh_from_db()
        self.assertEqual((self.order.city, self.order.np_office), ('Харків', 'Відділення №4'))
        self.assertEqual(self.order.manager_comment, '')
        self.assertEqual(self.order.items.get().id, before_item_id)
        edit_notify.assert_not_called()

    def test_edit_data_detects_existing_np_document_without_tracking_number(self):
        self.order.nova_poshta_document_ref = 'existing-document'
        self.order.save(update_fields=['nova_poshta_document_ref'])

        response = self.client.get(reverse('manual_order_edit_data', args=[self.order.id]))

        self.assertEqual(response.status_code, 200, response.content)
        order = response.json()['order']
        self.assertEqual((order['city'], order['np_office']), ('Харків', 'Відділення №4'))
        self.assertTrue(order['has_tracking'])

    def test_edit_swap_color_sends_diff_notification(self):
        payload = {
            'full_name': 'Лагош Олег',
            'phone': '+380500234363',
            'delivery_method': 'keep',
            'payment_preset': 'paid_full',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant_black.id,
                    'size': 'XXL',
                    'qty': 1,
                    'unit_price': 880,
                },
            ],
        }
        response, edit_notify = self._edit(payload)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'])

        # Позиція в БД оновлена на чорний варіант.
        item = self.order.items.get()
        self.assertEqual(item.color_variant_id, self.variant_black.id)

        # Сповіщення про редагування відправлено з diff, що містить зміни.
        edit_notify.assert_called_once()
        _, kwargs = edit_notify.call_args
        diff = edit_notify.call_args.args[1]
        self.assertTrue(diff['has_changes'])

    def test_edit_updates_existing_item_fit(self):
        payload = {
            'full_name': 'Лагош Олег',
            'phone': '+380500234363',
            'delivery_method': 'keep',
            'payment_preset': 'paid_full',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant_mint.id,
                    'fit_option_code': 'oversize',
                    'size': 'XXL',
                    'qty': 1,
                    'unit_price': 880,
                },
            ],
        }

        response, _ = self._edit(payload)

        self.assertEqual(response.status_code, 200, response.content)
        item = self.order.items.get()
        self.assertEqual(item.fit_option_code, 'oversize')
        self.assertEqual(item.fit_option_label, 'Оверсайз')

    def test_edit_preserves_historical_size_that_became_unavailable(self):
        VariantSizeRule.objects.create(
            variant=self.variant_mint,
            fit_code='classic',
            size='XXL',
            is_enabled=True,
            stock=0,
        )
        data_response = self.client.get(
            reverse('manual_order_edit_data', args=[self.order.id])
        )
        payload = {
            'full_name': 'Лагош Олег',
            'phone': '+380500234363',
            'delivery_method': 'keep',
            'payment_preset': 'paid_full',
            'manager_comment': 'Лише новий коментар',
            'items': data_response.json()['order']['items'],
        }

        response, _ = self._edit(payload)

        self.assertEqual(response.status_code, 200, response.content)
        item = self.order.items.get()
        self.assertEqual(item.fit_option_code, 'classic')
        self.assertEqual(item.size, 'XXL')

        payload['items'].append({
            key: value
            for key, value in payload['items'][0].items()
            if key != 'item_id'
        })
        duplicate_response, _ = self._edit(payload)
        self.assertEqual(duplicate_response.status_code, 422, duplicate_response.content)

    def test_edit_rejects_new_unavailable_fit(self):
        VariantFitRule.objects.create(
            variant=self.variant_mint,
            fit_code='oversize',
            is_enabled=False,
        )
        payload = {
            'full_name': 'Лагош Олег',
            'phone': '+380500234363',
            'delivery_method': 'keep',
            'payment_preset': 'paid_full',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant_mint.id,
                    'fit_option_code': 'oversize',
                    'size': 'XXL',
                    'qty': 1,
                    'unit_price': 880,
                },
            ],
        }

        response, _ = self._edit(payload)

        self.assertEqual(response.status_code, 422, response.content)

    def test_edit_preserves_exact_historical_fit_after_product_fit_is_deactivated(self):
        self.classic_fit.is_active = False
        self.classic_fit.save(update_fields=['is_active'])
        data_response = self.client.get(
            reverse('manual_order_edit_data', args=[self.order.id])
        )
        payload = {
            'full_name': 'Лагош Олег',
            'phone': '+380500234363',
            'delivery_method': 'keep',
            'payment_preset': 'paid_full',
            'manager_comment': 'Лише новий коментар',
            'items': data_response.json()['order']['items'],
        }

        response, _ = self._edit(payload)

        self.assertEqual(response.status_code, 200, response.content)
        existing_item = self.order.items.get()
        self.assertEqual(existing_item.fit_option_code, 'classic')
        self.assertEqual(existing_item.fit_option_label, 'Класична')

        payload['items'].append({
            key: value
            for key, value in payload['items'][0].items()
            if key != 'item_id'
        })
        duplicate_response, _ = self._edit(payload)
        self.assertEqual(duplicate_response.status_code, 422, duplicate_response.content)

    def test_edit_without_changes_skips_diff_notification_payload(self):
        # Зберігаємо без жодних змін — diff не повинен містити змін.
        payload = {
            'full_name': 'Лагош Олег',
            'phone': '+380500234363',
            'delivery_method': 'keep',
            'payment_preset': 'paid_full',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant_mint.id,
                    'size': 'XXL',
                    'qty': 1,
                    'unit_price': 880,
                },
            ],
        }
        response, edit_notify = self._edit(payload)
        self.assertEqual(response.status_code, 200, response.content)
        diff = edit_notify.call_args.args[1]
        self.assertFalse(diff['has_changes'])

    def test_edit_paid_manual_order_to_free_removes_manual_purchase(self):
        site_session = SiteSession.objects.create(session_key='manual-free-edit')
        utm_session = UTMSession.objects.create(
            session=site_session,
            session_key='manual-free-edit',
            utm_source='instagram',
        )
        self.order.source = 'manual'
        self.order.session_key = 'manual-free-edit'
        self.order.utm_session = utm_session
        self.order.payment_payload = {'manual_payment_preset': 'paid_full'}
        self.order.save(update_fields=['source', 'session_key', 'utm_session', 'payment_payload'])
        UserAction.objects.create(
            utm_session=utm_session,
            action_type='lead',
            order_id=self.order.pk,
            order_number=self.order.order_number,
            cart_value=self.order.total_sum,
        )
        UserAction.objects.create(
            utm_session=utm_session,
            action_type='purchase',
            order_id=self.order.pk,
            order_number=self.order.order_number,
            cart_value=self.order.total_sum,
            metadata={'source': 'np_delivery'},
        )
        utm_session.mark_as_converted(conversion_type='purchase')
        payload = {
            'full_name': 'Лагош Олег',
            'phone': '+380500234363',
            'delivery_method': 'keep',
            'payment_preset': 'free',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant_mint.id,
                    'size': 'XXL',
                    'qty': 1,
                    'unit_price': 880,
                },
            ],
        }

        response, _ = self._edit(payload)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(
            UserAction.objects.filter(
                action_type='purchase',
                order_id=self.order.pk,
            ).exists()
        )
        utm_session.refresh_from_db()
        self.assertTrue(utm_session.is_converted)
        self.assertEqual(utm_session.conversion_type, 'lead')


class OrderEditButtonRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username='admin-render', password='pass12345', is_staff=True)
        cls.category = Category.objects.create(name='Футболки', slug='tshirts-render')
        cls.product = Product.objects.create(
            title='Футболка', slug='tee-render', category=cls.category,
            price=880, status=ProductStatus.PUBLISHED,
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.order = Order.objects.create(
            full_name='Клієнт Тест', phone='+380501112233',
            city='Київ', np_office='Відділення №1',
            pay_type='cod', payment_status='unpaid', total_sum=Decimal('880.00'),
        )
        OrderItem.objects.create(
            order=self.order, product=self.product, title='Футболка',
            qty=1, unit_price=Decimal('880.00'), line_total=Decimal('880.00'),
        )

    def test_orders_section_renders_edit_button_and_drawer(self):
        response = self.client.get(reverse('admin_panel') + '?section=orders')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Редагувати замовлення')
        self.assertContains(response, 'oeditDrawer')
        self.assertContains(response, 'data-edit-order="%d"' % self.order.id)

    def test_edit_drawer_contains_fit_and_thermo_controls(self):
        response = self.client.get(reverse('admin_panel') + '?section=orders')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-edit="fit_option_code"')
        self.assertContains(response, 'Термотканина')
        self.assertContains(response, 'fit_option_code: it.fit_option_code')
        self.assertContains(response, 'normalizeCatalogItem(item, prod, false, true)')
        self.assertContains(response, 'variant.sizes_by_fit')

    def test_orders_section_shows_instagram_identity_and_management_link(self):
        from management.ig_bot_models import IgClient
        from management.services.ig_order_links import create_order_attribution

        ig_client = IgClient.get_or_create_for_sender(
            '1735898131060065',
            defaults={'username': 'olena_twocomms', 'display_name': 'Олена'},
        )
        create_order_attribution(
            self.order,
            client=ig_client,
            creation_mode='linked_existing',
            payment_source='manager_verified',
            created_by=self.admin,
        )

        response = self.client.get(reverse('admin_panel') + '?section=orders')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Instagram · Олена')
        self.assertContains(response, 'UID 1735898131060065')
        self.assertContains(response, 'section=clients&amp;client_id=%d' % ig_client.pk)

    def test_orders_section_uses_episode_identity_without_attribution(self):
        from management.ig_bot_models import IgClient
        from management.services.ig_commercial_episodes import bind_episode_order, start_repeat_episode

        ig_client = IgClient.get_or_create_for_sender(
            '1735898131060099',
            defaults={'username': 'episode_buyer', 'display_name': 'Марія'},
        )
        episode = start_repeat_episode(
            ig_client,
            repeat_kind='reorder',
            evidence_message_ids=[901],
            confidence=Decimal('0.91'),
            analysis_model='gemini-test',
            analysis_prompt_version='repeat-v1',
        )
        bind_episode_order(episode, self.order, creation_mode='manager_review')

        response = self.client.get(reverse('admin_panel') + '?section=orders')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Instagram · Марія')
        self.assertContains(response, 'UID 1735898131060099')
        self.assertContains(response, 'section=clients&amp;client_id=%d' % ig_client.pk)
