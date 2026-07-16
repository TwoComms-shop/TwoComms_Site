from django.test import TestCase
from django.urls import reverse

from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product


class OfferIdCanonicalizationTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name='Футболки', slug='shirts-offer-id')
        self.product = Product.objects.create(
            title='Футболка TwoComms',
            slug='shirt-offer-id',
            category=category,
            price=800,
            status='published',
        )
        black = Color.objects.create(name='Чорний', primary_hex='#000000')
        white = Color.objects.create(name='Білий', primary_hex='#FFFFFF')
        self.default_variant = ProductColorVariant.objects.create(
            product=self.product,
            color=black,
            is_default=True,
            order=0,
        )
        self.white_variant = ProductColorVariant.objects.create(
            product=self.product,
            color=white,
            order=1,
        )

    def test_missing_variant_uses_default_variant_color(self):
        implicit = self.product.get_offer_id(None, 'M')
        explicit = self.product.get_offer_id(self.default_variant.pk, 'M')

        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit, f'TC-{self.product.pk:04d}-ЧОРНИЙ-M')

    def test_explicit_non_default_variant_remains_authoritative(self):
        offer_id = self.product.get_offer_id(self.white_variant.pk, 'L')

        self.assertEqual(offer_id, f'TC-{self.product.pk:04d}-БІЛИЙ-L')

    def test_cart_without_variant_returns_default_variant_feed_id(self):
        response = self.client.post(
            reverse('cart_add'),
            {'product_id': self.product.pk, 'qty': 1, 'size': 'M'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['item']['offer_id'],
            self.default_variant.get_offer_id('M'),
        )

    def test_product_without_variants_retains_feed_fallback(self):
        product = Product.objects.create(
            title='Товар без кольору',
            slug='colorless-offer-id',
            category=self.product.category,
            price=500,
            status='published',
        )

        self.assertEqual(product.get_offer_id(None, 'S'), f'TC-{product.pk:04d}-ЧЕРНЫЙ-S')
