"""Bidirectional mini-cart hints use real quantities and selected unit prices."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache, caches
from django.template import Context, Template
from django.test import TestCase
from django.utils.translation import override

from product_catalog.models import MerchCollection, ProductMerchCollection, ProductOptionProfile
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption
from storefront.services.brigade_commerce import calculate_brigade_cart_pricing
from storefront.templatetags.garment_bundle_tags import garment_mini_cart_offer


class GarmentMiniCartOfferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        tees = Category.objects.create(slug='tshirts', name='Tees')
        hoodies = Category.objects.create(slug='hoodie', name='Hoodies')
        Product.objects.bulk_create([
            Product(pk=4, slug='my-little-baby', title='Baby tee', category=tees, price=1100, status='published'),
            Product(pk=5, slug='my-little-baby-hd', title='Baby hoodie', category=hoodies, price=1995, status='published'),
            Product(pk=13, slug='business-money', title='Money tee', category=tees, price=1100, status='published'),
            Product(pk=14, slug='business-money-hd', title='Money hoodie', category=hoodies, price=1850, status='published'),
            Product(pk=91, slug='225-tshirt', title='225 tee', category=tees, price=880, status='published'),
            Product(pk=92, slug='225-hoodie', title='225 hoodie', category=hoodies, price=1995, status='published'),
            Product(pk=110, slug='futbolka-boiova-kvitochka', title='Thermo', category=tees, price=1100, status='published'),
            Product(pk=113, slug='draft-tee', title='Draft', category=tees, price=1100, status='draft'),
            Product(pk=120, slug='future-brigade', title='Brigade', category=hoodies, price=1995, status='published'),
        ])
        color = Color.objects.create(name='Black', primary_hex='#000000')
        ProductColorVariant.objects.bulk_create([
            ProductColorVariant(pk=1000 + product_id, product_id=product_id, color=color, slug='black', is_default=True)
            for product_id in (4, 13)
        ])
        ProductFitOption.objects.bulk_create([
            ProductFitOption(product_id=product_id, code=code, label=code, is_active=True, is_default=code == 'classic')
            for product_id in (4, 13) for code in ('classic', 'oversize')
        ])
        ProductOptionProfile.objects.bulk_create([
            ProductOptionProfile(product_id=product_id, option_key=f'fit={code}',
                                 option_values={'fit': code}, price_delta=delta)
            for product_id in (4, 13) for code, delta in (('classic', 0), ('oversize', 150))
        ])
        brigade = MerchCollection.objects.create(slug='future-brigade', kind='brigade', name_uk='Brigade')
        ProductMerchCollection.objects.create(product_id=120, collection=brigade)

    def setUp(self):
        cache.clear()
        caches['fragments'].clear()
        self.enterContext(override('uk'))
        self.products = Product.objects.select_related('category').in_bulk()
        self.variants = ProductColorVariant.objects.in_bulk()

    def context(self, cart):
        quote = calculate_brigade_cart_pricing(cart, products=self.products, variants=self.variants)
        items = [{
            'key': key, 'product': self.products[row['product_id']], 'qty': row['qty'],
            'fit_option_code': row.get('fit_option_code', ''),
            'color_variant': self.variants.get(row.get('color_variant_id')),
            'brigade_pricing': quote.lines[key].public_metadata(),
            'bundle_pricing': quote.lines[key].bundle_metadata(),
        } for key, row in cart.items()]
        return Context({'items': items, **quote.public_metadata(),
                        'request': SimpleNamespace(session={'cart': cart})})

    @staticmethod
    def row(product_id, qty=1, **kwargs):
        return {'product_id': product_id, 'qty': qty, **kwargs}

    def sale_variants(self):
        color = Color.objects.create(name='White', primary_hex='#FFFFFF')
        ProductColorVariant.objects.bulk_create([
            ProductColorVariant(pk=2004, product_id=4, color=color, slug='sale', price_override=800),
            ProductColorVariant(pk=2005, product_id=5, color=color, slug='sale', price_override=40),
        ])
        self.variants = ProductColorVariant.objects.in_bulk()

    def test_hoodie_invites_tees_and_tee_invites_hoodies(self):
        for product_id, kind, category in ((5, 'hoodie', 'tshirts'), (4, 'tee', 'hoodie')):
            with self.subTest(kind=kind):
                offer = garment_mini_cart_offer(self.context({'source': self.row(product_id)}))
                self.assertEqual((offer['kind'], offer['category_url'], offer['total_saving']),
                                 (kind, f'/catalog/{category}/', 300))
                self.assertEqual((offer['same_print_saving'], offer['other_print_saving']), (300, 250))
                self.assertEqual(offer['source_product_id'], product_id)
                self.assertEqual(offer['source_title'], self.products[product_id].title)
                self.assertEqual(offer['source_product_url'], f'/product/{self.products[product_id].slug}/')
        with override('en'):
            offer = garment_mini_cart_offer(self.context({'tee': self.row(4)}))
            self.assertEqual(offer['source_product_url'], '/en/product/my-little-baby/')
            rendered = Template('{% load garment_bundle_tags %}{% garment_mini_cart_offer as offer %}{{ offer.category_url }}').render(
                self.context({'tee': self.row(4)}))
        self.assertEqual(rendered, '/en/catalog/hoodie/')

    def test_fully_paired_items_and_225_or_future_brigade_do_not_trigger(self):
        for cart in (
            {'h': self.row(5), 't': self.row(4)},
            {'h': self.row(5), 't': self.row(4), '225h': self.row(92), '225t': self.row(91), 'brigade': self.row(120)},
            {'225h': self.row(92), '225t': self.row(91)},
            {'thermo': self.row(110), 'draft': self.row(113), 'brigade': self.row(120)},
            {},
        ):
            with self.subTest(cart=cart):
                self.assertIsNone(garment_mini_cart_offer(self.context(cart)))

    def test_partial_quantity_counts_both_pair_keys_and_multiple_allocations(self):
        for cart, kind in (
            ({'h': self.row(5, 3), 't': self.row(4)}, 'hoodie'),
            ({'h': self.row(5), 't': self.row(4, 3)}, 'tee'),
            ({'h': self.row(5, 2), 't': self.row(4), 'other': self.row(13)}, None),
        ):
            with self.subTest(kind=kind):
                context = self.context(cart)
                offer = garment_mini_cart_offer(context)
                if kind is None:
                    self.assertEqual(len(context['bundle_pairs']), 2)
                    self.assertIsNone(offer)
                else:
                    self.assertEqual(offer['kind'], kind)
                    self.assertEqual(offer['total_saving'], 300)

    def test_other_design_pairs_do_not_leave_an_invitation_for_paired_units(self):
        context = self.context({'h': self.row(14), 't': self.row(4)})
        self.assertEqual(context['bundle_pairs'][0]['kind'], 'other_design')
        self.assertIsNone(garment_mini_cart_offer(context))

    def test_existing_site_sale_and_selected_variant_never_inflate_savings(self):
        self.products[4].discount_percent = 30
        Product.objects.filter(pk=4).update(discount_percent=30)
        offer = garment_mini_cart_offer(self.context({'t': self.row(4)}))
        self.assertEqual(offer['total_saving'], 50)
        self.products[4].discount_percent = None
        Product.objects.filter(pk=4).update(discount_percent=None)
        cache.clear()
        self.sale_variants()
        offer = garment_mini_cart_offer(self.context({'t': self.row(4, color_variant_id=2004)}))
        self.assertEqual(offer['total_saving'], 50)
        self.assertEqual((offer['same_print_saving'], offer['other_print_saving']), (50, 50))
        ProductColorVariant.objects.filter(pk=2004).delete()
        cache.clear()
        offer = garment_mini_cart_offer(self.context({'h': self.row(5, color_variant_id=2005)}))
        self.assertEqual(offer['total_saving'], 289)
        self.assertEqual(offer['other_print_saving'], 239)

    def test_different_pdp_and_cart_source_explains_different_savings(self):
        from storefront.templatetags.garment_bundle_tags import garment_product_offer

        Product.objects.filter(pk=14).update(status='draft')
        pdp = garment_product_offer(self.products[13])
        self.assertEqual((pdp['same_print_saving'], pdp['other_print_saving'], pdp['total_saving']), (0, 250, 250))
        mini = garment_mini_cart_offer(self.context({'earlier': self.row(4), 'current': self.row(13)}))
        self.assertEqual(mini['source_product_id'], 4)
        # Only Baby hoodie remains published, so this source has no real
        # different-print hoodie candidate; the matching invitation is300.
        self.assertEqual((mini['same_print_saving'], mini['other_print_saving'], mini['total_saving']), (300, 0, 300))

    def test_selected_fit_and_option_delta_uses_actual_unpaired_unit(self):
        self.sale_variants()
        offer = garment_mini_cart_offer(self.context({'t': self.row(
            4, color_variant_id=2004, fit_option_code='oversize', option_values={'fit': 'oversize'},
        )}))
        self.assertEqual(offer['total_saving'], 50)

    def test_first_useful_item_is_chosen_without_multiplying_by_quantity(self):
        self.sale_variants()
        offer = garment_mini_cart_offer(self.context({
            'cheap': self.row(4, 7, color_variant_id=2004), 'normal': self.row(13),
        }))
        self.assertEqual(offer['tee_id'], 4)
        self.assertEqual(offer['total_saving'], 50)

    def test_large_cart_summary_work_is_bounded(self):
        context = self.context({f'tee-{index}': self.row(4) for index in range(50)})
        with patch('storefront.templatetags.garment_bundle_tags.garment_product_offer', return_value=None) as summary:
            self.assertIsNone(garment_mini_cart_offer(context))
        self.assertEqual(summary.call_count, 3)

    def test_zero_benefit_does_not_trigger(self):
        self.products[4].price = 800
        self.products[5].price = Decimal('0.01')
        Product.objects.filter(pk=4).update(price=800)
        Product.objects.filter(pk=5).update(price=Decimal('0.01'))
        offer = garment_mini_cart_offer(self.context({'t': self.row(4)}))
        self.assertIsNone(offer)

    def test_promos_are_suppressed_even_when_threshold_currently_gives_no_discount(self):
        for fields, session in (
            ({'applied_promo': 'VOUCHER'}, {}), ({'promo_code': object()}, {}),
            ({'discount': Decimal('1')}, {}), ({'discount': Decimal('0')}, {'promo_code_id': 17}),
        ):
            with self.subTest(fields=fields, session=session):
                context = self.context({'t': self.row(4)})
                context.update(fields)
                context['request'].session.update(session)
                with self.assertNumQueries(0):
                    self.assertIsNone(garment_mini_cart_offer(context))

    def test_cart_items_alias_remains_supported(self):
        context = self.context({'h': self.row(5)})
        context['cart_items'] = context['items']
        del context['items']
        self.assertEqual(garment_mini_cart_offer(context)['kind'], 'hoodie')
