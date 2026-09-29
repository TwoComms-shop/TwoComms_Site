from decimal import Decimal
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from product_catalog.models import (
    GarmentFlow, GarmentFlowCategory, ProductOptionProfile,
    VariantCombinationProfile, VariantDetails, VariantFitRule, VariantSizeRule,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import (
    Category, MarketplaceFeed, MarketplaceFeedProductRule,
    Product, ProductFitOption, ProductStatus,
)
from storefront.services.marketplace_feeds import (
    build_google_merchant_feed_xml, build_meta_catalog_feed_xml,
    build_profile_offers, iter_feed_offers,
)
from storefront.utils.analytics_helpers import get_offer_id


G = {"g": "http://base.google.com/ns/1.0"}


@override_settings(SITE_BASE_URL="https://twocomms.shop", FEED_BASE_URL="https://twocomms.shop")
class MarketplaceFeedConfigurationTests(TestCase):
    def setUp(self):
        cache.clear()
        for target in (
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            "storefront.signals.enqueue_indexnow_urls",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.category = Category.objects.create(name="Футболки", slug="futbolki")
        self.product = Product.objects.create(
            title="Футболка конфігурація", slug="feed-configuration",
            category=self.category, price=1250, status=ProductStatus.PUBLISHED,
            main_image="products/configuration.jpg",
        )
        self.classic = ProductFitOption.objects.create(
            product=self.product, code="classic", label="Класична", order=0, is_default=True,
        )
        self.oversize = ProductFitOption.objects.create(
            product=self.product, code="oversize", label="Оверсайз", order=1,
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Чорний", primary_hex="#000000"),
            slug="black", sku="UNCHANGED-SKU", stock=0,
        )
        ProductOptionProfile.objects.create(
            product=self.product, option_key="fit=oversize",
            option_values={"fit": "oversize"}, price_delta=250,
        )

    def offers(self):
        return iter_feed_offers()

    def assert_pdp_price(self, offer):
        response = self.client.get(urlsplit(offer.product_url).path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(offer.price), response.context["selected_variant_price"])
        return response

    def test_color_with_disabled_default_fit_uses_oversize_price_and_existing_id(self):
        VariantFitRule.objects.create(variant=self.variant, fit_code="classic", is_enabled=False)
        offer = self.offers()[0]
        self.assertEqual(offer.price, 1500)
        self.assertEqual(offer.base_price, 1500)
        self.assertEqual(offer.google_offer_id, get_offer_id(self.product.pk, self.variant.pk, offer.size, "Чорний"))
        self.assertEqual(offer.sku, "UNCHANGED-SKU")
        self.assertEqual(offer.product_url, f"https://twocomms.shop/product/feed-configuration/black/{offer.size.lower()}/")
        response = self.assert_pdp_price(offer)
        self.assertEqual(response.context["preselected_fit_code"], "oversize")

    def test_product_default_fit_is_used_even_when_another_fit_is_cheaper(self):
        self.oversize.is_default = True
        self.oversize.save()
        offer = self.offers()[0]
        self.assertEqual(offer.price, 1500)
        self.assert_pdp_price(offer)

    def test_override_is_final_base_then_material_and_fit_are_added_once(self):
        self.product.discount_percent = 20
        self.product.save(update_fields=["discount_percent"])
        self.variant.price_override = 1400
        self.variant.save(update_fields=["price_override"])
        VariantDetails.objects.create(variant=self.variant, price_delta=300)
        VariantFitRule.objects.create(variant=self.variant, fit_code="classic", is_enabled=False)
        offer = self.offers()[0]
        self.assertEqual((offer.base_price, offer.price), (1950, 1950))
        self.assert_pdp_price(offer)
        for builder in (build_google_merchant_feed_xml, build_meta_catalog_feed_xml):
            item = ET.fromstring(builder()).find("channel/item")
            self.assertEqual(item.findtext("g:price", namespaces=G), "1950.00 UAH")
            self.assertIsNone(item.find("g:sale_price", G))

    def test_discount_rounding_and_surcharges_match_pdp_in_google_and_instagram(self):
        self.product.price = 1501
        self.product.discount_percent = 17
        self.product.save(update_fields=["price", "discount_percent"])
        VariantDetails.objects.create(variant=self.variant, price_delta=300)
        VariantFitRule.objects.create(variant=self.variant, fit_code="classic", is_enabled=False)
        offer = self.offers()[0]
        self.assertEqual((offer.base_price, offer.price), (2051, 1795))
        self.assert_pdp_price(offer)
        google = ET.fromstring(build_google_merchant_feed_xml()).findall("channel/item")
        instagram = ET.fromstring(build_meta_catalog_feed_xml()).findall("channel/item")
        fields = ("id", "link", "price", "sale_price")
        self.assertEqual(
            [[item.findtext(f"g:{field}", namespaces=G) for field in fields] for item in google],
            [[item.findtext(f"g:{field}", namespaces=G) for field in fields] for item in instagram],
        )
        self.assertEqual(google[0].findtext("g:price", namespaces=G), "2051.00 UAH")
        self.assertEqual(google[0].findtext("g:sale_price", namespaces=G), "1795.00 UAH")

    def test_size_restrictions_survive_stock_floor_and_profile_force_in_stock(self):
        VariantSizeRule.objects.create(variant=self.variant, size="S", is_enabled=False)
        VariantSizeRule.objects.create(variant=self.variant, size="M", stock=0)
        VariantSizeRule.objects.create(variant=self.variant, size="L", is_enabled=False)
        VariantSizeRule.objects.create(variant=self.variant, size="L", fit_code="classic", is_enabled=True)
        offers = {offer.size: offer for offer in self.offers()}
        self.assertFalse(offers["S"].available)
        self.assertEqual(offers["S"].export_quantity, 0)
        self.assertFalse(offers["M"].available)
        self.assertTrue(offers["L"].available)
        self.assertEqual(offers["L"].export_quantity, 100)
        feed = MarketplaceFeed.objects.create(
            name="Forced stock", slug="forced-feed-stock", adapter="google",
            rules={"availability": {"mode": "force_in_stock", "quantity": 999}},
        )
        MarketplaceFeedProductRule.objects.create(
            feed=feed, product=self.product, availability="in_stock", quantity=888,
        )
        profiled = {offer.size: offer for offer in build_profile_offers(feed)}
        self.assertFalse(profiled["S"].available)
        self.assertEqual(profiled["S"].export_quantity, 0)
        self.assertTrue(profiled["L"].available)
        self.assertEqual(profiled["L"].export_quantity, 888)
        self.assertEqual(profiled["L"].price, offers["L"].price)
        for builder, unavailable in (
            (build_google_merchant_feed_xml, "out_of_stock"),
            (build_meta_catalog_feed_xml, "out of stock"),
        ):
            item = ET.fromstring(builder(feed=feed)).find("channel/item")
            self.assertEqual(item.findtext("g:availability", namespaces=G), unavailable)

    def test_disabled_exact_combination_uses_same_default_fallback_as_pdp(self):
        flow = GarmentFlow.objects.create(
            code="feed-lining", name="Feed lining", axes=[{
                "code": "lining", "label": "Утеплення", "options": [
                    {"code": "fleece", "label": "Фліс", "default": True},
                    {"code": "no_fleece", "label": "Без флісу"},
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.category)
        ProductOptionProfile.objects.create(
            product=self.product, option_key="lining=fleece",
            option_values={"lining": "fleece"}, price_delta=100,
        )
        VariantCombinationProfile.objects.create(
            variant=self.variant, combination_key="fit=classic;lining=fleece",
            option_values={"fit": "classic", "lining": "fleece"}, is_active=False,
        )
        offer = self.offers()[0]
        self.assertEqual(offer.price, 1600)
        response = self.assert_pdp_price(offer)
        self.assertEqual(response.context["product_option_context"]["selected_values"], {"fit": "oversize", "lining": "fleece"})

    def test_all_disabled_fits_remain_unavailable_without_removing_sku_ids(self):
        for code in ("classic", "oversize"):
            VariantFitRule.objects.create(variant=self.variant, fit_code=code, is_enabled=False)
        offers = self.offers()
        self.assertEqual(len(offers), 5)
        self.assertTrue(all(not offer.available and offer.export_quantity == 0 for offer in offers))

    def test_no_color_legacy_offer_uses_exact_pdp_discount_rounding(self):
        self.variant.delete()
        self.product.price = 1501
        self.product.discount_percent = 17
        self.product.save(update_fields=["price", "discount_percent"])
        offer = self.offers()[0]
        self.assertEqual(offer.price, 1245)
        self.assert_pdp_price(offer)
