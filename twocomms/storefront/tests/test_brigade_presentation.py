from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.translation import override

from product_catalog.models import ProductOptionProfile, VariantCombinationProfile, VariantDetails
from product_catalog.services import product_option_context
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption
from storefront.services.brigade_presentation import brigade_copy
from storefront.services.brigade_commerce import get_product_brigade_policy
from storefront.templatetags.brigade_tags import brigade_product_offer


class FitPricePresentationTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name='Футболки', slug='tshirts')
        self.product = Product.objects.create(title='Fit price', slug='fit-price', category=category, price=1100)
        color = Color.objects.create(name='Black', primary_hex='#000000')
        self.variant = ProductColorVariant.objects.create(product=self.product, color=color)
        for code, delta in [('classic', 0), ('oversize', 150)]:
            ProductFitOption.objects.create(product=self.product, code=code, label=code, is_default=code == 'classic')
            ProductOptionProfile.objects.create(product=self.product, option_key=f'fit={code}', option_values={'fit': code}, price_delta=delta)

    def prices(self):
        context = product_option_context(self.product, variant=self.variant)
        fit = next(axis for axis in context['axes'] if axis['code'] == 'fit')
        return {choice['code']: choice['unit_price'] for choice in fit['choices']}

    def test_fit_selector_shows_full_prices_not_only_surcharge(self):
        self.assertEqual(self.prices(), {'classic': 1100, 'oversize': 1250})

    def test_fit_selector_keeps_real_variant_and_material_price(self):
        self.variant.price_override = 1200
        self.variant.save(update_fields=['price_override'])
        VariantDetails.objects.create(variant=self.variant, price_delta=20)
        self.assertEqual(self.prices(), {'classic': 1220, 'oversize': 1370})

    def test_fit_prices_include_selected_axes_and_exact_combination_override(self):
        VariantDetails.objects.create(variant=self.variant, price_delta=20)
        ProductOptionProfile.objects.create(
            product=self.product, option_key='lining=fleece',
            option_values={'lining': 'fleece'}, price_delta=200,
        )
        VariantCombinationProfile.objects.create(
            variant=self.variant, combination_key='fit=classic;lining=fleece',
            option_values={'fit': 'classic', 'lining': 'fleece'}, price_delta=75,
        )
        context = product_option_context(
            self.product, variant=self.variant, option_values={'lining': 'fleece'},
        )
        fit = next(axis for axis in context['axes'] if axis['code'] == 'fit')
        self.assertEqual(
            {choice['code']: choice['unit_price'] for choice in fit['choices']},
            {'classic': 1175, 'oversize': 1470},
        )

    def test_offer_query_count_does_not_grow_with_candidate_colors(self):
        self.product.slug = '225-tshirt'
        self.product.price = 880
        self.product.status = 'published'
        self.product.save(update_fields=['slug', 'price', 'status'])
        hoodies = Category.objects.create(name='Худі', slug='hoodie')
        hoodie = Product.objects.create(
            title='225 hoodie', slug='225-hoodie', category=hoodies,
            price=1995, status='published',
        )
        ProductColorVariant.objects.create(product=hoodie, color=self.variant.color)
        get_product_brigade_policy(self.product)

        def read_offer():
            with CaptureQueriesContext(connection) as queries:
                offer = brigade_product_offer(
                    {'request': RequestFactory().get('/')}, self.product,
                )
            self.assertEqual(offer['set_offer_price'], 2650)
            self.offer_queries = [query['sql'] for query in queries]
            return len(queries)

        initial_count = read_offer()
        for index in range(3):
            color = Color.objects.create(name=f'Extra {index}', primary_hex='#112233')
            ProductColorVariant.objects.create(product=self.product, color=color)
            ProductColorVariant.objects.create(product=hoodie, color=color)
        self.assertEqual(read_offer(), initial_count, self.offer_queries)
        self.assertLessEqual(initial_count, 20)
        hoodie.discount_percent = 20
        hoodie.save(update_fields=['discount_percent'])
        self.assertIsNone(brigade_product_offer(
            {'request': RequestFactory().get('/')}, self.product,
        ))

    def test_new_payment_and_offer_copy_exists_in_all_storefront_languages(self):
        for language, marker in [('uk', 'від 2 футболок'), ('ru', 'от 2 футболок'), ('en', 'buy 2+ tees')]:
            with self.subTest(language=language), override(language):
                words = brigade_copy()
                self.assertIn(marker, words['rules'])
                self.assertTrue(words['full_payment'])
                self.assertEqual(set(words), set(brigade_copy('uk')))
