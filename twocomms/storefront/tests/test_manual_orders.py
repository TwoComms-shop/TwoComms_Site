"""Тести ручного створення замовлень адміністратором."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from orders.models import Order, OrderItem
from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from product_catalog.models import (
    ColorProfile,
    ProductOptionSizeGrid,
    VariantFitRule,
    VariantSizeRule,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import (
    Catalog,
    Category,
    Product,
    ProductFitOption,
    ProductStatus,
    SizeGrid,
    UserAction,
)

User = get_user_model()


def _delivery_payload():
    """Будує валідні підписані токени НП (місто + відділення)."""
    city_item = {
        'label': 'Київ',
        'settlement_ref': 'settle-ref-1',
        'city_ref': 'city-ref-1',
    }
    wh_item = {
        'label': 'Відділення №1',
        'ref': 'wh-ref-1',
        'kind': 'branch',
        'city_ref': 'city-ref-1',
    }
    return {
        'city': 'Київ',
        'np_office': 'Відділення №1',
        'np_settlement_ref': 'settle-ref-1',
        'np_city_ref': 'city-ref-1',
        'np_city_token': build_city_choice_token(city_item),
        'np_warehouse_ref': 'wh-ref-1',
        'np_warehouse_token': build_warehouse_choice_token(wh_item, fallback_city_ref='city-ref-1'),
    }


@override_settings(
    SECURE_SSL_REDIRECT=False,
    NOVA_POSHTA_FALLBACK_ENABLED=False,
    COMPRESS_ENABLED=False,
    COMPRESS_OFFLINE=False,
    CACHES={
        'default': {
            'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
        },
        'fragments': {
            'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
        },
    },
    STORAGES={
        'default': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
        },
    },
)
class ManualOrderCreateTests(TestCase):
    def test_manual_order_template_preserves_missing_review_prices_until_manager_input(self):
        template = (
            Path(__file__).parents[2]
            / 'twocomms_django_theme/templates/pages/admin_manual_order.html'
        ).read_text(encoding='utf-8')

        self.assertIn("it.unit_price === '' ? '' : it.unit_price", template)
        self.assertIn("if (val === '') { it.unit_price = ''; }", template)
        self.assertIn("Ціни всіх позицій мають бути більшими за нуль.", template)
        self.assertIn("Розподілено ", template)
        self.assertIn("із ", template)
        self.assertIn("Залишок: ", template)

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username='admin', password='pass12345', is_staff=True,
        )
        cls.category = Category.objects.create(name='Футболки', slug='tshirts-mo')
        cls.product = Product.objects.create(
            title='Базова футболка',
            slug='basic-tee-mo',
            category=cls.category,
            price=900,
            status=ProductStatus.PUBLISHED,
        )
        cls.color = Color.objects.create(name='Чорний', primary_hex='#000000')
        cls.variant = ProductColorVariant.objects.create(
            product=cls.product, color=cls.color, is_default=True,
        )
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
        cls.thermo_color = Color.objects.create(name='Термо-зелена', primary_hex='#29A36A')
        cls.thermo_variant = ProductColorVariant.objects.create(
            product=cls.product,
            color=cls.thermo_color,
            price_override=1200,
        )
        ColorProfile.objects.create(color=cls.thermo_color, is_thermo=True)
        VariantFitRule.objects.create(
            variant=cls.thermo_variant,
            fit_code='classic',
            is_enabled=False,
            reason='Термотканина доступна лише в оверсайз посадці',
        )
        VariantFitRule.objects.create(
            variant=cls.thermo_variant,
            fit_code='oversize',
            is_enabled=True,
        )
        cls.url = reverse('manual_order_create')

    def setUp(self):
        self.client.force_login(self.admin)

    def _post(self, payload):
        with mock.patch(
            'storefront.views.manual_orders.telegram_notifier.send_new_order_notification',
            return_value=True,
        ) as notify:
            response = self.client.post(
                self.url,
                data=json.dumps(payload),
                content_type='application/json',
            )
        return response, notify

    def test_get_renders_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Створення замовлення вручну')
        self.assertContains(response, 'id="moPriceOverride"')
        self.assertContains(response, 'price_override_code')
        self.assertContains(response, 'price_override_reason')

    def test_confirmed_review_prefills_matched_catalog_product(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision

        ig_client = IgClient.get_or_create_for_sender('manual-review-prefill')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-prefill',
            evidence={
                'order_draft': {
                    'quoted_total': '790.00',
                    'delivery': {'full_name': 'Яна Ніколаєнко', 'phone': '0502034719'},
                    'items': [{
                        'product_id': self.product.pk,
                        'color_variant_id': self.variant.pk,
                        'title': 'Базова футболка',
                        'unit_price': '790.00',
                        'qty': 1,
                        'size': 'S',
                        'catalog': {
                            'product_id': self.product.pk,
                            'title': self.product.title,
                            'url': f'https://twocomms.shop/product/{self.product.slug}/',
                        },
                    }],
                },
            },
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            verification_scope='full_payment',
            confirmed_amount='790.00',
        )
        with mock.patch(
            'storefront.views.manual_orders.render',
            return_value=HttpResponse(),
        ) as render_mock:
            response = self.client.get(self.url, {'ig_payment_review': review.pk})
        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        initial = json.loads(context['order_initial_json'])
        self.assertEqual(initial['items'][0]['kind'], 'catalog')
        self.assertEqual(initial['items'][0]['product_id'], self.product.pk)
        self.assertEqual(initial['items'][0]['color_variant_id'], self.variant.pk)
        self.assertEqual(initial['items'][0]['unit_price'], 790.0)
        self.assertEqual(initial['payment_preset'], 'unpaid_full')

    def test_ambiguous_multi_item_review_keeps_each_price_for_manual_input(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision
        from storefront.views.manual_orders import _build_ig_review_initial

        ig_client = IgClient.get_or_create_for_sender('manual-review-ambiguous-prices')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-ambiguous-prices',
            evidence={
                'order_draft': {
                    'quoted_total': '2100.00',
                    'uncertainty_reasons': ['conversation_price_allocation_required'],
                    'items': [
                        {'product_id': self.product.pk, 'title': self.product.title, 'qty': 1},
                        {'product_id': self.product.pk, 'title': self.product.title, 'qty': 1},
                    ],
                },
            },
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            confirmed_amount='2100.00',
        )

        initial = _build_ig_review_initial(review)

        self.assertEqual([row['unit_price'] for row in initial['items']], ['', ''])
        self.assertTrue(all(row['price_requires_input'] for row in initial['items']))

    def test_confirmed_review_normalizes_ukrainian_fit_label_to_catalog_code(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision

        ig_client = IgClient.get_or_create_for_sender('manual-review-fit-label')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-fit-label',
            evidence={
                'order_draft': {
                    'quoted_total': '900.00',
                    'items': [{
                        'product_id': self.product.pk,
                        'title': self.product.title,
                        'unit_price': '900.00',
                        'qty': 1,
                        'size': 'XS',
                        'fit': 'Оверсайз',
                    }],
                },
            },
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            verification_scope='full_payment',
            confirmed_amount='900.00',
        )

        with mock.patch(
            'storefront.views.manual_orders.render',
            return_value=HttpResponse(),
        ) as render_mock:
            response = self.client.get(self.url, {'ig_payment_review': review.pk})

        self.assertEqual(response.status_code, 200)
        initial = json.loads(render_mock.call_args.args[2]['order_initial_json'])
        self.assertEqual(initial['items'][0]['fit_option_code'], 'oversize')
        self.assertEqual(initial['items'][0]['fit_option_label'], 'Оверсайз')

    def test_confirmed_receipt_review_creates_unverified_order_without_purchase(self):
        from management.ig_bot_models import IgOrderAttribution, IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision

        ig_client = IgClient.get_or_create_for_sender('manual-review-unverified')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-unverified',
            evidence={'order_draft': {'quoted_total': '790.00'}},
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            verification_scope='full_payment',
            confirmed_amount='790.00',
        )
        payload = {
            'payment_review_id': review.pk,
            'full_name': 'Яна Ніколаєнко',
            'phone': '0502034719',
            'delivery_method': 'manual',
            'city': 'Харків',
            'np_office': 'Поштомат 21586',
            'payment_preset': 'paid_full',
            'items': [{
                'kind': 'catalog',
                'product_id': self.product.pk,
                'color_variant_id': self.variant.pk,
                'size': 'S',
                'qty': 1,
                'unit_price': 790,
            }],
        }
        response, _notify = self._post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.payment_status, 'unpaid')
        self.assertEqual(order.pay_type, 'online_full')
        self.assertTrue(order.payment_payload['manual_payment_evidence_confirmed'])
        self.assertFalse(order.payment_payload['provider_payment_confirmed'])
        self.assertEqual(order.payment_payload['manager_confirmed_amount'], '790.00')
        self.assertEqual(order.payment_payload['manager_verification_scope'], 'full_payment')
        self.assertEqual(order.payment_payload['manager_amount_source'], 'manager_input')
        self.assertEqual(order.payment_payload['negotiated_order_total'], '790.00')
        self.assertEqual(order.payment_payload['effective_confirmed_amount'], '790.00')
        self.assertFalse(
            UserAction.objects.filter(action_type='purchase', order_id=order.pk).exists()
        )
        attribution = IgOrderAttribution.objects.get(order=order)
        self.assertEqual(attribution.client, ig_client)
        self.assertEqual(attribution.creation_mode, 'manager_review')
        self.assertEqual(attribution.payment_source, 'manager_verified')

        from orders.nova_poshta_documents import build_order_payment_snapshot

        fulfillment = build_order_payment_snapshot(order)
        self.assertEqual(fulfillment['payment_status'], 'paid')
        self.assertEqual(fulfillment['paid_amount'], '790.00')
        self.assertEqual(fulfillment['cod_amount'], '0.00')

    def test_manager_verified_dynamic_prepayment_drives_real_nova_poshta_balance(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision
        from orders.nova_poshta_documents import build_order_payment_snapshot

        ig_client = IgClient.get_or_create_for_sender('manual-review-dynamic-prepay')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-dynamic-prepay',
            evidence={'order_draft': {'quoted_total': '790.00'}},
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            verification_scope='prepayment',
            confirmed_amount='315.00',
        )
        payload = {
            'payment_review_id': review.pk,
            'full_name': 'Яна Ніколаєнко',
            'phone': '0502034719',
            'delivery_method': 'manual',
            'city': 'Харків',
            'np_office': 'Поштомат 21586',
            'items': [{
                'kind': 'catalog',
                'product_id': self.product.pk,
                'color_variant_id': self.variant.pk,
                'size': 'S',
                'qty': 1,
                'unit_price': 790,
            }],
        }

        response, _notify = self._post(payload)

        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.pay_type, 'prepayment')
        self.assertEqual(order.payment_status, 'unpaid')
        self.assertEqual(order.payment_payload['manager_confirmed_amount'], '315.00')
        self.assertEqual(order.payment_payload['manager_verification_scope'], 'prepayment')
        self.assertEqual(order.get_prepayment_amount(), Decimal('315.00'))
        fulfillment = build_order_payment_snapshot(order)
        self.assertEqual(fulfillment['payment_status'], 'prepaid')
        self.assertEqual(fulfillment['prepayment_amount'], '315.00')
        self.assertEqual(fulfillment['paid_amount'], '315.00')
        self.assertEqual(fulfillment['cod_amount'], '475.00')

    def test_review_episode_idempotency_never_reuses_different_existing_items(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_commercial_episodes import ensure_episode_for_review
        from management.services.ig_payment_review import record_review_decision

        ig_client = IgClient.get_or_create_for_sender('manual-review-idempotency-guard')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-idempotency-guard',
            evidence={'order_draft': {'quoted_total': '790.00'}},
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            verification_scope='full_payment',
            confirmed_amount='790.00',
        )
        episode = ensure_episode_for_review(review)
        incompatible = Order.objects.create(
            full_name='Яна Ніколаєнко',
            phone='+380502034719',
            city='Харків',
            np_office='Поштомат 21586',
            total_sum=Decimal('790.00'),
            checkout_idempotency_key=f'ig-episode:{episode.pk}',
        )
        OrderItem.objects.create(
            order=incompatible,
            title='Інший товар',
            size='XL',
            qty=1,
            unit_price=Decimal('790.00'),
            line_total=Decimal('790.00'),
        )
        payload = {
            'payment_review_id': review.pk,
            'full_name': 'Яна Ніколаєнко',
            'phone': '0502034719',
            'delivery_method': 'manual',
            'city': 'Харків',
            'np_office': 'Поштомат 21586',
            'items': [{
                'kind': 'catalog',
                'product_id': self.product.pk,
                'color_variant_id': self.variant.pk,
                'size': 'S',
                'qty': 1,
                'unit_price': 790,
            }],
        }

        response, _notify = self._post(payload)

        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn('incompatible order', response.json()['message'])
        review.refresh_from_db()
        self.assertIsNone(review.order_id)

    def test_multi_item_review_cannot_replace_negotiated_total_with_catalog_total(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision

        ig_client = IgClient.get_or_create_for_sender('manual-review-multi-total-guard')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-multi-total-guard',
            evidence={
                'order_draft': {
                    'quoted_total': '2100.00',
                    'items': [
                        {'product_id': self.product.pk, 'title': self.product.title, 'qty': 1},
                        {'product_id': self.product.pk, 'title': self.product.title, 'qty': 1},
                    ],
                },
            },
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            confirmed_amount='2100.00',
        )
        payload = {
            'payment_review_id': review.pk,
            'full_name': 'Яна Ніколаєнко',
            'phone': '0502034719',
            'delivery_method': 'manual',
            'city': 'Харків',
            'np_office': 'Поштомат 21586',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.pk,
                    'color_variant_id': self.variant.pk,
                    'size': 'S',
                    'qty': 1,
                    'unit_price': 1090,
                },
                {
                    'kind': 'catalog',
                    'product_id': self.product.pk,
                    'color_variant_id': self.variant.pk,
                    'size': 'XS',
                    'qty': 1,
                    'unit_price': 1090,
                },
            ],
        }

        response, notify = self._post(payload)

        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn('2100.00', response.json()['message'])
        self.assertIn('2180.00', response.json()['message'])
        self.assertFalse(Order.objects.exists())
        notify.assert_not_called()

    def test_multi_item_price_override_keeps_quoted_and_actual_totals(self):
        from management.ig_bot_models import IgOrderAttribution, IgPaymentConfirmationReview
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision

        ig_client = IgClient.get_or_create_for_sender('manual-review-multi-total-override')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-multi-total-override',
            evidence={'order_draft': {'quoted_total': '2100.00', 'items': [{}, {}]}},
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            confirmed_amount='2100.00',
        )
        payload = {
            'payment_review_id': review.pk,
            'full_name': 'Яна Ніколаєнко',
            'phone': '0502034719',
            'delivery_method': 'manual',
            'city': 'Харків',
            'np_office': 'Поштомат 21586',
            'price_override_code': 'manager_repriced_after_review',
            'price_override_reason': 'Менеджер погодив іншу конфігурацію після первинного review.',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.pk,
                    'color_variant_id': self.variant.pk,
                    'size': 'S',
                    'qty': 1,
                    'unit_price': 1090,
                },
                {
                    'kind': 'catalog',
                    'product_id': self.product.pk,
                    'color_variant_id': self.variant.pk,
                    'size': 'XS',
                    'qty': 1,
                    'unit_price': 1090,
                },
            ],
        }

        response, _notify = self._post(payload)

        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        attribution = IgOrderAttribution.objects.get(order=order)
        self.assertEqual(order.total_sum, Decimal('2180.00'))
        self.assertEqual(attribution.negotiated_total, Decimal('2100.00'))
        self.assertEqual(
            order.payment_payload['instagram_price_override'],
            {
                'quoted_total': '2100.00',
                'actual_order_total': '2180.00',
                'code': 'manager_repriced_after_review',
                'reason': 'Менеджер погодив іншу конфігурацію після первинного review.',
                'actor_id': self.admin.pk,
            },
        )

    def test_legacy_confirmed_review_without_decision_does_not_prefill_order(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient

        ig_client = IgClient.get_or_create_for_sender('manual-review-legacy-prefill')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-legacy-prefill',
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            evidence={'order_draft': {'items': [{'product_id': self.product.pk}]}},
        )

        with mock.patch(
            'storefront.views.manual_orders.render',
            return_value=HttpResponse(),
        ) as render_mock:
            response = self.client.get(self.url, {'ig_payment_review': review.pk})

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        self.assertEqual(context['order_initial_json'], '')

    def test_legacy_confirmed_review_without_decision_cannot_create_order(self):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.models import IgClient

        ig_client = IgClient.get_or_create_for_sender('manual-review-legacy-create')
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            dedupe_key='manual-review-legacy-create',
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            evidence={},
        )
        payload = {
            'payment_review_id': review.pk,
            'full_name': 'Яна Ніколаєнко',
            'phone': '0502034719',
            'delivery_method': 'manual',
            'city': 'Харків',
            'np_office': 'Поштомат 21586',
            'items': [{
                'kind': 'catalog',
                'product_id': self.product.pk,
                'color_variant_id': self.variant.pk,
                'size': 'S',
                'qty': 1,
                'unit_price': 790,
            }],
        }

        response, _notify = self._post(payload)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(Order.objects.exists())

    def test_reconciliation_conflict_cannot_create_manual_order(self):
        from management.ig_bot_models import (
            IgDeal,
            IgPaymentConfirmationReview,
            IgPaymentProjection,
        )
        from management.models import IgClient
        from management.services.ig_payment_review import record_review_decision

        ig_client = IgClient.get_or_create_for_sender('manual-review-reconciliation')
        deal = IgDeal.objects.create(
            client=ig_client,
            amount=Decimal('790.00'),
            requested_payment_amount=Decimal('790.00'),
        )
        review = IgPaymentConfirmationReview.objects.create(
            client=ig_client,
            deal=deal,
            dedupe_key='manual-review-reconciliation',
            evidence={'order_draft': {'quoted_total': '790.00'}},
        )
        record_review_decision(
            review,
            actor=self.admin,
            decision='manager_verified',
            verification_scope='prepayment',
            confirmed_amount='315.00',
        )
        IgPaymentProjection.objects.create(
            deal=deal,
            client=ig_client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal('790.00'),
        )
        payload = {
            'payment_review_id': review.pk,
            'full_name': 'Яна Ніколаєнко',
            'phone': '0502034719',
            'delivery_method': 'manual',
            'city': 'Харків',
            'np_office': 'Поштомат 21586',
            'items': [{
                'kind': 'catalog',
                'product_id': self.product.pk,
                'color_variant_id': self.variant.pk,
                'size': 'S',
                'qty': 1,
                'unit_price': 790,
            }],
        }

        response, _notify = self._post(payload)

        self.assertEqual(response.status_code, 409)
        self.assertIn('звірка', response.json()['message'].lower())
        self.assertFalse(Order.objects.exists())

    def test_create_form_contains_fit_and_thermo_controls(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-edit="fit_option_code"')
        self.assertContains(response, 'Термотканина')
        self.assertContains(response, 'fit_option_code: it.fit_option_code')
        self.assertContains(response, 'normalizeCatalogItem(item, product, false, true)')
        self.assertContains(response, 'variant.sizes_by_fit')

    def test_product_payload_exposes_fit_and_thermo_variant_choices(self):
        VariantSizeRule.objects.create(
            variant=self.thermo_variant,
            fit_code='oversize',
            size='M',
            is_enabled=False,
        )
        VariantSizeRule.objects.create(
            variant=self.thermo_variant,
            fit_code='oversize',
            size='L',
            is_enabled=True,
            stock=0,
        )
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        products = json.loads(response.context['products_json'])
        product = next(item for item in products if item['id'] == self.product.id)
        self.assertEqual(
            [(item['code'], item['label']) for item in product['fits']],
            [('classic', 'Класична'), ('oversize', 'Оверсайз')],
        )
        self.assertEqual(product['default_fit_code'], 'classic')
        self.assertTrue(product['sizes_by_fit']['classic'])
        self.assertTrue(product['sizes_by_fit']['oversize'])

        normal = next(item for item in product['variants'] if item['id'] == self.variant.id)
        self.assertFalse(normal['is_thermo'])
        self.assertEqual(normal['available_fit_codes'], ['classic', 'oversize'])

        thermo = next(item for item in product['variants'] if item['id'] == self.thermo_variant.id)
        self.assertTrue(thermo['is_thermo'])
        self.assertEqual(thermo['available_fit_codes'], ['oversize'])
        self.assertEqual(thermo['prices_by_fit']['oversize'], 1200)
        self.assertIn('M', normal['sizes_by_fit']['oversize'])
        self.assertNotIn('M', thermo['sizes_by_fit']['oversize'])
        self.assertNotIn('L', thermo['sizes_by_fit']['oversize'])

    def test_product_payload_skips_fit_grid_work_for_products_without_fits(self):
        Product.objects.create(
            title='Худі без посадки',
            slug='hoodie-without-fit',
            category=self.category,
            price=1500,
            status=ProductStatus.PUBLISHED,
        )

        with mock.patch(
            'product_catalog.size_grid_services.build_size_grid_comparison',
            wraps=__import__(
                'product_catalog.size_grid_services',
                fromlist=['build_size_grid_comparison'],
            ).build_size_grid_comparison,
        ) as build_comparison:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(build_comparison.call_count, 1)

    def test_product_payload_has_bounded_queries_for_production_tshirt_shape(self):
        catalog = Catalog.objects.create(name='Футболки SQL', slug='tshirts-query-budget')
        grid_payload = {
            'columns': [{'key': 'size', 'label': 'Розмір'}],
            'rows': [{'size': size} for size in ('S', 'M', 'L', 'XL', 'XXL')],
        }
        classic_grid = SizeGrid.objects.create(
            catalog=catalog,
            name='Classic query grid',
            guide_data=grid_payload,
        )
        oversize_grid = SizeGrid.objects.create(
            catalog=catalog,
            name='Oversize query grid',
            guide_data=grid_payload,
        )

        products = [self.product]
        self.product.catalog = catalog
        self.product.save(update_fields=['catalog'])
        for index in range(30):
            product = Product.objects.create(
                title=f'Футболка SQL {index}',
                slug=f'tshirt-query-budget-{index}',
                category=self.category,
                catalog=catalog,
                price=900,
                status=ProductStatus.PUBLISHED,
            )
            ProductFitOption.objects.bulk_create([
                ProductFitOption(
                    product=product,
                    code='classic',
                    label='Класична',
                    is_default=True,
                    order=10,
                ),
                ProductFitOption(
                    product=product,
                    code='oversize',
                    label='Оверсайз',
                    order=20,
                ),
            ])
            ProductColorVariant.objects.create(
                product=product,
                color=self.color,
                is_default=True,
            )
            if index < 4:
                ProductColorVariant.objects.create(
                    product=product,
                    color=self.thermo_color,
                )
            products.append(product)

        ProductOptionSizeGrid.objects.bulk_create([
            ProductOptionSizeGrid(
                product=product,
                option_key=f'fit={fit_code}',
                size_grid=grid,
            )
            for product in products
            for fit_code, grid in (
                ('classic', classic_grid),
                ('oversize', oversize_grid),
            )
        ])

        from storefront.views.manual_orders import _build_products_payload

        with CaptureQueriesContext(connection) as queries:
            payload = _build_products_payload()

        self.assertEqual(len(payload), 31)
        self.assertLessEqual(len(queries), 120, len(queries))

    def test_create_catalog_order(self):
        payload = {
            'full_name': 'Іваненко Іван Іванович',
            'phone': '0501234567',
            'payment_preset': 'cod',
            'sale_source': 'Instagram',
            'manager_comment': 'Терміново',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant.id,
                    'size': 'M',
                    'qty': 2,
                    'unit_price': 850,
                },
            ],
        }
        payload.update(_delivery_payload())
        response, notify = self._post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data['success'], data)

        order = Order.objects.get(order_number=data['order_number'])
        self.assertEqual(order.source, 'manual')
        self.assertEqual(order.created_by, self.admin)
        self.assertEqual(order.sale_source, 'Instagram')
        self.assertEqual(order.manager_comment, 'Терміново')
        self.assertEqual(order.pay_type, 'cod')
        self.assertEqual(order.payment_status, 'unpaid')
        self.assertEqual(order.city, 'Київ')
        self.assertEqual(order.np_office, 'Відділення №1')
        self.assertEqual(order.np_warehouse_ref, 'wh-ref-1')
        self.assertEqual(order.total_sum, Decimal('1700.00'))

        item = order.items.get()
        self.assertEqual(item.product_id, self.product.id)
        self.assertEqual(item.qty, 2)
        self.assertEqual(item.unit_price, Decimal('850.00'))
        self.assertFalse(item.is_custom)
        notify.assert_called_once()

    def test_create_keeps_same_size_classic_and_oversize_as_separate_lines(self):
        payload = {
            'full_name': 'Іваненко Іван Іванович',
            'phone': '0501234567',
            'payment_preset': 'cod',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant.id,
                    'fit_option_code': 'classic',
                    'size': 'S',
                    'qty': 1,
                    'unit_price': 900,
                },
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.variant.id,
                    'fit_option_code': 'oversize',
                    'size': 'S',
                    'qty': 1,
                    'unit_price': 900,
                },
            ],
        }
        payload.update(_delivery_payload())

        response, _ = self._post(payload)

        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(
            list(order.items.order_by('id').values_list('fit_option_code', 'fit_option_label', 'size')),
            [('classic', 'Класична', 'S'), ('oversize', 'Оверсайз', 'S')],
        )

    def test_create_rejects_fit_disabled_for_selected_thermo_variant(self):
        payload = {
            'full_name': 'Іваненко Іван Іванович',
            'phone': '0501234567',
            'payment_preset': 'cod',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.thermo_variant.id,
                    'fit_option_code': 'classic',
                    'size': 'M',
                    'qty': 1,
                    'unit_price': 1200,
                },
            ],
        }
        payload.update(_delivery_payload())

        response, _ = self._post(payload)

        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn('недоступна', response.json()['message'])

    def test_create_defaults_missing_thermo_fit_to_oversize(self):
        payload = {
            'full_name': 'Іваненко Іван Іванович',
            'phone': '0501234567',
            'payment_preset': 'cod',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.thermo_variant.id,
                    'size': 'M',
                    'qty': 1,
                    'unit_price': 1200,
                },
            ],
        }
        payload.update(_delivery_payload())

        response, _ = self._post(payload)

        self.assertEqual(response.status_code, 200, response.content)
        item = Order.objects.get(pk=response.json()['order_id']).items.get()
        self.assertEqual(item.fit_option_code, 'oversize')
        self.assertEqual(item.fit_option_label, 'Оверсайз')

    def test_create_rejects_size_disabled_for_selected_color_and_fit(self):
        VariantSizeRule.objects.create(
            variant=self.thermo_variant,
            fit_code='oversize',
            size='M',
            is_enabled=False,
        )
        payload = {
            'full_name': 'Іваненко Іван Іванович',
            'phone': '0501234567',
            'payment_preset': 'cod',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.thermo_variant.id,
                    'fit_option_code': 'oversize',
                    'size': 'M',
                    'qty': 1,
                    'unit_price': 1200,
                },
            ],
        }
        payload.update(_delivery_payload())

        response, _ = self._post(payload)

        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn('Розмір', response.json()['message'])

    def test_create_rejects_zero_stock_size_for_selected_color_and_fit(self):
        VariantSizeRule.objects.create(
            variant=self.thermo_variant,
            fit_code='oversize',
            size='M',
            is_enabled=True,
            stock=0,
        )
        payload = {
            'full_name': 'Іваненко Іван Іванович',
            'phone': '0501234567',
            'payment_preset': 'cod',
            'items': [
                {
                    'kind': 'catalog',
                    'product_id': self.product.id,
                    'color_variant_id': self.thermo_variant.id,
                    'fit_option_code': 'oversize',
                    'size': 'M',
                    'qty': 1,
                    'unit_price': 1200,
                },
            ],
        }
        payload.update(_delivery_payload())

        response, _ = self._post(payload)

        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn('Розмір', response.json()['message'])

    def test_create_custom_item_order(self):
        payload = {
            'full_name': 'Петренко Петро',
            'phone': '+380671112233',
            'payment_preset': 'paid_full',
            'items': [
                {
                    'kind': 'custom',
                    'title': 'Термо-футболка XL',
                    'unit_price': 1200,
                    'qty': 1,
                    'size': 'XL',
                    'color_name': 'Сірий',
                },
            ],
        }
        payload.update(_delivery_payload())
        response, _ = self._post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data['success'], data)

        order = Order.objects.get(order_number=data['order_number'])
        self.assertEqual(order.pay_type, 'online_full')
        self.assertEqual(order.payment_status, 'paid')
        item = order.items.get()
        self.assertIsNone(item.product_id)
        self.assertTrue(item.is_custom)
        self.assertEqual(item.title, 'Термо-футболка XL')
        self.assertEqual(item.color_name_custom, 'Сірий')
        self.assertEqual(item.color_name, 'Сірий')
        self.assertEqual(order.total_sum, Decimal('1200.00'))
        self.assertEqual(
            UserAction.objects.filter(action_type='purchase', order_id=order.pk).count(),
            1,
        )

    def test_free_manual_order_does_not_record_purchase(self):
        payload = {
            'full_name': 'Подарунок Клієнту',
            'phone': '+380671112233',
            'payment_preset': 'free',
            'items': [
                {
                    'kind': 'custom',
                    'title': 'Подарункова футболка',
                    'unit_price': 1200,
                    'qty': 1,
                },
            ],
        }
        payload.update(_delivery_payload())

        response, _ = self._post(payload)

        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        self.assertEqual(order.payment_status, 'paid')
        self.assertFalse(
            UserAction.objects.filter(action_type='purchase', order_id=order.pk).exists()
        )

    def test_invalid_phone_rejected(self):
        payload = {
            'full_name': 'Хтось',
            'phone': '123',
            'items': [{'kind': 'custom', 'title': 'X', 'unit_price': 10, 'qty': 1}],
        }
        payload.update(_delivery_payload())
        response, _ = self._post(payload)
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.json()['success'])

    def test_missing_delivery_rejected(self):
        payload = {
            'full_name': 'Хтось',
            'phone': '0501234567',
            'items': [{'kind': 'custom', 'title': 'X', 'unit_price': 10, 'qty': 1}],
        }
        response, _ = self._post(payload)
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.json()['success'])

    def test_manual_delivery_without_np(self):
        payload = {
            'full_name': 'Сидоренко Сидір',
            'phone': '0631112233',
            'delivery_method': 'manual',
            'city': 'Львів',
            'np_office': 'Укрпошта, вул. Сихівська 5',
            'payment_preset': 'cod',
            'items': [
                {'kind': 'custom', 'title': 'Худі ручне', 'unit_price': 1500, 'qty': 1},
            ],
        }
        response, notify = self._post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data['success'], data)
        order = Order.objects.get(order_number=data['order_number'])
        self.assertEqual(order.city, 'Львів')
        self.assertEqual(order.np_office, 'Укрпошта, вул. Сихівська 5')
        self.assertEqual(order.np_warehouse_ref, '')
        self.assertEqual(order.np_city_ref, '')
        notify.assert_called_once()

    def test_manual_delivery_requires_city_and_office(self):
        payload = {
            'full_name': 'Без адреси',
            'phone': '0501234567',
            'delivery_method': 'manual',
            'city': '',
            'np_office': '',
            'items': [{'kind': 'custom', 'title': 'X', 'unit_price': 10, 'qty': 1}],
        }
        response, _ = self._post(payload)
        self.assertEqual(response.status_code, 422)
        self.assertFalse(response.json()['success'])

    def test_empty_items_rejected(self):
        payload = {'full_name': 'Хтось', 'phone': '0501234567', 'items': []}
        payload.update(_delivery_payload())
        response, _ = self._post(payload)
        self.assertEqual(response.status_code, 422)

    def test_non_staff_redirected(self):
        self.client.logout()
        user = User.objects.create_user(username='plain', password='pass12345')
        self.client.force_login(user)
        response = self.client.get(self.url)
        # staff_member_required перенаправляє не-адмінів на сторінку логіну адмінки
        self.assertIn(response.status_code, (302, 403))

    def _receipt_contract_review(self, *, delivery=False):
        from management.models import IgClient
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_payment_review import record_review_decision

        customer = IgClient.get_or_create_for_sender('receipt-order-contract')
        draft = {
            'quoted_total': '850.00',
            'merchandise_total': '850.00',
            'amount_source_message_id': 3282,
            'items': [{
                'title': 'Футболка TWOCOMMS 1654',
                'fit': 'oversize', 'size': 'L', 'qty': 1,
                'unit_price': '850.00', 'color_name': 'Білий',
                'option_values': {'garment': 'tshirt', 'print': 'TWOCOMMS 1654'},
                'option_labels': {'Тип': 'Футболка', 'Принт': 'TWOCOMMS 1654'},
                'reference_message_ids': [3241],
            }],
        }
        if delivery:
            draft.update(delivery_amount='120.00', payable_total='970.00')
        review = IgPaymentConfirmationReview.objects.create(
            client=customer, dedupe_key='receipt-order-contract',
            evidence={'order_draft': draft},
        )
        record_review_decision(
            review, actor=self.admin, decision='manager_verified',
            verification_scope='full_payment',
            confirmed_amount='970.00' if delivery else '850.00',
        )
        return review

    def _receipt_contract_payload(self, review):
        return {
            'payment_review_id': review.pk,
            'full_name': 'Іван Іванов', 'phone': '0931112233',
            'delivery_method': 'manual', 'city': 'Київ', 'np_office': 'Відділення №4',
            'sale_source': 'Instagram', 'payment_preset': 'paid_full',
            'items': [{
                'kind': 'custom', 'title': 'Футболка TWOCOMMS 1654',
                'size': 'L', 'fit_option_code': 'oversize', 'fit_option_label': 'Оверсайз',
                'color_name': 'Білий', 'qty': 1, 'unit_price': '850.00',
                'option_values': {'garment': 'tshirt', 'print': 'TWOCOMMS 1654', '_reference_message_ids': [3241]},
                'option_labels': {'Тип': 'Футболка', 'Принт': 'TWOCOMMS 1654'},
            }],
        }

    def test_review_order_keeps_merchandise_shipping_and_paid_authority_separate(self):
        from orders.nova_poshta_documents import build_order_payment_snapshot
        from management.services.ig_order_amounts import order_amounts
        from management.models import IgClient

        review = self._receipt_contract_review(delivery=True)
        response, _notify = self._post(self._receipt_contract_payload(review))
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        item = order.items.get()
        self.assertEqual(order.total_sum, Decimal('850.00'))
        self.assertEqual(item.unit_price, Decimal('850.00'))
        self.assertEqual(item.color_name_custom, 'Білий')
        self.assertEqual(item.fit_option_code, 'oversize')
        self.assertEqual(item.option_values['_reference_message_ids'], [3241])
        self.assertEqual(order.payment_status, 'unpaid')
        self.assertFalse(order.payment_payload['provider_payment_confirmed'])
        self.assertEqual(order.payment_payload['manager_confirmed_amount'], '970.00')
        self.assertEqual(order_amounts(order)['payable'], Decimal('970.00'))
        snapshot = build_order_payment_snapshot(order)
        self.assertEqual(snapshot['declared_cost'], '850.00')
        self.assertEqual(snapshot['payable_total'], '970.00')
        self.assertEqual(snapshot['cod_amount'], '0.00')
        self.assertEqual(snapshot['delivery_payer_type'], 'Sender')
        review.client.refresh_from_db()
        self.assertEqual(review.client.stage, IgClient.Stage.ORDER_CREATED)

    def test_compatible_existing_episode_order_is_reused_without_appending_items(self):
        from management.services.ig_commercial_episodes import ensure_episode_for_review

        review = self._receipt_contract_review()
        episode = ensure_episode_for_review(review)
        existing = Order.objects.create(
            full_name='Іван Іванов', phone='+380931112233', city='Київ',
            np_office='Відділення №4', total_sum=Decimal('850.00'),
            checkout_idempotency_key=f'ig-episode:{episode.pk}',
        )
        item_payload = self._receipt_contract_payload(review)['items'][0]
        OrderItem.objects.create(
            order=existing, title=item_payload['title'], size='L',
            fit_option_code='oversize', fit_option_label='Оверсайз',
            color_name_custom='Білий', is_custom=True,
            option_values=item_payload['option_values'], option_labels=item_payload['option_labels'],
            qty=1, unit_price=Decimal('850.00'), line_total=Decimal('850.00'),
        )
        response, notify = self._post(self._receipt_contract_payload(review))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['order_id'], existing.pk)
        self.assertEqual(existing.items.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.subtotal, existing.total_sum)
        review.refresh_from_db()
        self.assertEqual(review.order_id, existing.pk)
        notify.assert_not_called()

    def test_custom_review_initial_preserves_color_fit_print_and_reference(self):
        from storefront.views.manual_orders import _build_ig_review_initial

        review = self._receipt_contract_review()
        item = _build_ig_review_initial(review)['items'][0]
        self.assertEqual(item['kind'], 'custom')
        self.assertEqual(item['color_name'], 'Білий')
        self.assertEqual(item['fit_option_code'], 'oversize')
        self.assertEqual(item['option_values']['print'], 'TWOCOMMS 1654')
        self.assertEqual(item['option_values']['_reference_message_ids'], [3241])

    def test_paid_manual_manager_action_has_actor_audit(self):
        payload = self._receipt_contract_payload(type('Review', (), {'pk': ''})())
        response, _notify = self._post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        order = Order.objects.get(pk=response.json()['order_id'])
        audit = order.payment_payload['manual_payment_action']
        self.assertEqual(audit['actor_id'], self.admin.pk)
        self.assertEqual(audit['source'], 'management_user')
        self.assertEqual(audit['payment_status'], 'paid')
        self.assertTrue(audit['recorded_at'])
