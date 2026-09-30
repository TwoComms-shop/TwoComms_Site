from decimal import Decimal
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from product_catalog.models import (
    GarmentFlow, GarmentFlowCategory, ProductOptionProfile, ProductOptionProfileI18n,
    VariantCombinationProfile, VariantDetails, VariantFitRule, VariantSizeRule,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import (
    Category, MarketplaceFeed, MarketplaceFeedProductRule,
    Product, ProductFitOption, ProductStatus,
)
from storefront.services.marketplace_feeds import (
    build_google_merchant_feed_xml, build_meta_catalog_feed_xml,
    build_profile_offers, iter_feed_offers, build_uaprom_products_feed_xml,
    build_prom_feed_xml, build_kasta_feed_xml,
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

    def bezzet(self, *, feed=None):
        return ET.fromstring(build_uaprom_products_feed_xml(feed=feed)).findall("shop/offers/offer")

    def use_current_tee_prices(self):
        self.product.price = 1100
        self.product.save(update_fields=["price"])
        ProductOptionProfile.objects.filter(product=self.product, option_key="fit=oversize").update(price_delta=150)

    def test_bezzet_both_fits_have_live_uk_ru_prices_and_request_confirmation(self):
        self.use_current_tee_prices()
        first = self.bezzet()[0]
        self.assertTrue(first.findtext("name").endswith("[Classic / Oversize]"))
        self.assertTrue(first.findtext("name_ru").endswith("[Classic / Oversize]"))
        self.assertEqual(first.findtext("price"), "1100")
        self.assertEqual(first.attrib["id"], f"{self.product.pk}-{self.variant.pk}-S")
        self.assertEqual(first.attrib["group_id"], f"{self.product.pk}-{self.variant.pk}")
        for field in ("description_ua", "description_ru"):
            description = first.findtext(field)
            self.assertIn("Classic — 1100 грн", description)
            self.assertIn("Oversize — 1250 грн", description)
            self.assertIn("Менеджер", description)
            self.assertIn("заявка", description)
            self.assertNotIn("г/м", description)
            self.assertNotIn("175", description)
        response = self.client.get(urlsplit(first.findtext("url")).path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_variant_price"], Decimal("1100"))

    def test_bezzet_oversize_only_does_not_advertise_unavailable_classic(self):
        self.use_current_tee_prices()
        VariantFitRule.objects.create(variant=self.variant, fit_code="classic", is_enabled=False)
        first = self.bezzet()[0]
        self.assertTrue(first.findtext("name").endswith("[Oversize]"))
        self.assertEqual(first.findtext("price"), "1250")
        for field in ("description_ua", "description_ru"):
            self.assertIn("Oversize — 1250 грн", first.findtext(field))
            self.assertNotIn("Classic", first.findtext(field))
            self.assertNotIn("1100", first.findtext(field))
        self.assertTrue(first.findtext("url").endswith("/oversize/"))

    def test_bezzet_classic_price_link_can_differ_from_merchant_default_without_changing_merchant(self):
        self.use_current_tee_prices()
        self.oversize.is_default = True
        self.oversize.save()
        first = self.bezzet()[0]
        self.assertEqual(first.findtext("price"), "1100")
        self.assertTrue(first.findtext("url").endswith("/classic/"))
        response = self.client.get(urlsplit(first.findtext("url")).path)
        self.assertEqual(response.context["selected_variant_price"], Decimal("1100"))
        self.assertEqual(self.offers()[0].price, 1250)
        for builder in (build_google_merchant_feed_xml, build_meta_catalog_feed_xml):
            item = ET.fromstring(builder()).find("channel/item")
            self.assertEqual(item.findtext("g:price", namespaces=G), "1250.00 UAH")

    def test_bezzet_size_specific_alternate_fit_and_staff_sold_out_rules(self):
        self.use_current_tee_prices()
        VariantSizeRule.objects.create(variant=self.variant, size="S", fit_code="classic", is_enabled=False)
        VariantSizeRule.objects.create(variant=self.variant, size="M", is_enabled=False)
        offers = self.bezzet()
        first = offers[0]
        self.assertTrue(first.findtext("name").endswith("[Oversize]"))
        self.assertEqual(first.findtext("price"), "1250")
        self.assertEqual(first.attrib["available"], "true")
        self.assertEqual(first.findtext("stock_quantity"), "100")
        unavailable = next(offer for offer in offers if offer.attrib["id"].endswith("-M"))
        self.assertEqual(unavailable.attrib["available"], "false")
        self.assertEqual(unavailable.findtext("stock_quantity"), "0")
        feed = MarketplaceFeed.objects.create(
            name="BZ sold out", slug="bz-sold-out", adapter="bezzet",
            rules={"availability": {"mode": "force_out_of_stock"}},
        )
        forced = self.bezzet(feed=feed)[0]
        self.assertEqual(forced.attrib["available"], "false")
        self.assertEqual(forced.findtext("stock_quantity"), "0")
        feed.rules = {"availability": {"mode": "force_in_stock", "quantity": 999}}
        feed.save(update_fields=["rules"])
        forced_unavailable = next(offer for offer in self.bezzet(feed=feed) if offer.attrib["id"].endswith("-M"))
        self.assertEqual(forced_unavailable.attrib["available"], "false")
        self.assertEqual(forced_unavailable.findtext("stock_quantity"), "0")

    def test_bezzet_fit_fabric_copy_uses_only_localized_fit_owner_metadata(self):
        self.use_current_tee_prices()
        profile = ProductOptionProfile.objects.get(product=self.product, option_key="fit=oversize")
        ProductOptionProfileI18n.objects.create(
            profile=profile, lang="uk", marketing_text="Перевірена тканина: 190 г/м².",
        )
        ProductOptionProfileI18n.objects.create(
            profile=profile, lang="ru", marketing_text="Проверенная ткань: 190 г/м².",
        )
        first = self.bezzet()[0]
        self.assertIn("Перевірена тканина: 190 г/м²", first.findtext("description_ua"))
        self.assertIn("Проверенная ткань: 190 г/м²", first.findtext("description_ru"))
        self.assertNotIn("Перевірена", first.findtext("description_ru"))
        self.assertNotIn("160 г", first.findtext("description_ua"))

    def test_bezzet_225_prices_and_full_payment_terms_are_from_current_contract(self):
        self.use_current_tee_prices()
        self.product.slug = "225-tshirt"
        self.product.save(update_fields=["slug"])
        self.variant.price_override = 880
        self.variant.save(update_fields=["price_override"])
        first = self.bezzet()[0]
        self.assertEqual(first.findtext("price"), "880")
        for field in ("description_ua", "description_ru"):
            self.assertIn("Classic — 880 грн", first.findtext(field))
            self.assertIn("Oversize — 1030 грн", first.findtext(field))
            self.assertNotIn("175", first.findtext(field))
            self.assertNotIn("передоплат", first.findtext(field))
        self.assertIn("повною оплатою", first.findtext("description_ua"))
        self.assertIn("полной оплате", first.findtext("description_ru"))

    def test_bezzet_presentation_does_not_leak_into_prom_or_kasta(self):
        self.use_current_tee_prices()
        for builder in (build_prom_feed_xml, build_kasta_feed_xml):
            first = ET.fromstring(builder()).find("shop/offers/offer")
            self.assertEqual(first.findtext("price"), "1100")
            self.assertNotIn("Classic / Oversize", first.findtext("name"))
            self.assertNotIn("Менеджер", first.findtext("description"))
        self.assertEqual(ProductFitOption.objects.filter(product=self.product).count(), 2)

    def test_bezzet_dynamic_endpoint_returns_updated_xml_without_snapshot_command(self):
        self.use_current_tee_prices()
        response = self.client.get("/products_feed.xml", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        first = ET.fromstring(response.content).find("shop/offers/offer")
        self.assertEqual(first.findtext("price"), "1100")
        self.assertTrue(first.findtext("name").endswith("[Classic / Oversize]"))
