"""Trusted design identity and truthful lazy bundle choices."""

import json
import re
from types import SimpleNamespace

from django.core.cache import cache, caches
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from product_catalog.models import (
    GarmentFlow, GarmentFlowCategory, MerchCollection, ProductMerchCollection,
    ProductOptionProfile, ProductOptionSizeGrid, ProductSizeRule, SizeGridProfile,
    VariantDetails, VariantFitRule, VariantOptionSizeGrid, VariantSizeRule,
)
from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import Catalog, Category, Product, ProductFitOption, ProductImage, SizeGrid
from storefront.services.catalog_helpers import bump_public_product_order_version
from storefront.services.garment_bundle_catalog import (
    VERIFIED_SAME_PRINT_PAIRS, garment_bundle_offer_summary,
    garment_bundle_tee_catalog, is_same_design, same_design_hoodie_ids_for,
    same_design_tee_ids_for,
)


class ReviewedBundlePrintIdentityTests(SimpleTestCase):
    def test_exact_id_and_slug_both_required_and_no_title_inference(self):
        for pair in VERIFIED_SAME_PRINT_PAIRS:
            tee = SimpleNamespace(pk=pair.tee_id, slug=pair.tee_slug)
            hoodie = SimpleNamespace(pk=pair.hoodie_id, slug=pair.hoodie_slug)
            self.assertTrue(is_same_design(tee, hoodie))
            self.assertIn(tee.pk, same_design_tee_ids_for(hoodie))
            self.assertIn(hoodie.pk, same_design_hoodie_ids_for(tee))
            self.assertFalse(is_same_design(SimpleNamespace(pk=tee.pk, slug="edited"), hoodie))
            self.assertFalse(is_same_design(tee, SimpleNamespace(pk=hoodie.pk, slug="edited")))
        self.assertEqual(len(VERIFIED_SAME_PRINT_PAIRS), 12)
        self.assertTrue(all(pair.print_id != 21 for pair in VERIFIED_SAME_PRINT_PAIRS))
        self.assertFalse(is_same_design(
            SimpleNamespace(pk=104, slug="twocomms-reality-bends-dark-neon-edition"),
            SimpleNamespace(pk=102, slug="hd-twocomms-reality-bends-future-2026"),
        ))
        self.assertFalse(is_same_design(
            SimpleNamespace(pk=91, slug="225-tshirt"), SimpleNamespace(pk=92, slug="225-hoodie"),
        ))


class GarmentBundleCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tee_category = Category.objects.create(slug="tshirts", name="Футболки")
        cls.hoodie_category = Category.objects.create(slug="hoodie", name="Худі")
        cls.catalog = Catalog.objects.create(slug="tshirts", name="Футболки")
        Product.objects.bulk_create([
            Product(pk=1, slug="classic-tshirt", title="Ordinary tee", category=cls.tee_category,
                    catalog=cls.catalog, status="published", price=1100, main_image="products/ordinary.webp"),
            Product(pk=4, slug="my-little-baby", title="Matching tee", title_en="Matching tee EN",
                    category=cls.tee_category, catalog=cls.catalog, status="published", price=1100,
                    main_image="products/matching.webp"),
            Product(pk=5, slug="my-little-baby-hd", title="Hoodie", category=cls.hoodie_category,
                    status="published", price=1995),
            Product(pk=11, slug="in-shee-hd", title="Special hoodie", category=cls.hoodie_category,
                    status="published", price=1850),
            Product(pk=91, slug="225-tshirt", title="225 tee", category=cls.tee_category,
                    catalog=cls.catalog, status="published", price=880),
            Product(pk=92, slug="225-hoodie", title="225 hoodie", category=cls.hoodie_category,
                    status="published", price=1995),
            Product(pk=110, slug="futbolka-boiova-kvitochka", title="Thermo", category=cls.tee_category,
                    catalog=cls.catalog, status="published", price=1100),
            Product(pk=113, slug="draft-tee", title="Draft", category=cls.tee_category,
                    catalog=cls.catalog, status="draft", price=1100),
            Product(pk=120, slug="another-brigade-tee", title="Other brigade", category=cls.tee_category,
                    catalog=cls.catalog, status="published", price=1100),
        ])
        cls.hoodie = Product.objects.select_related("category").get(pk=5)
        cls.tee = Product.objects.get(pk=4)
        cls.color = Color.objects.create(name="Чорний", primary_hex="#000000")
        cls.white = Color.objects.create(name="Білий", primary_hex="#FFFFFF")
        ProductColorVariant.objects.bulk_create([
            ProductColorVariant(pk=1000 + product_id, product_id=product_id,
                                color=cls.color, slug="black", is_default=True)
            for product_id in (1, 4, 91, 110, 113, 120)
        ])
        ProductColorVariant.objects.bulk_create([
            ProductColorVariant(pk=2004, product_id=4, color=cls.white, slug="white", price_override=1200),
        ])
        cls.variant = ProductColorVariant.objects.get(pk=1004)
        ProductColorImage.objects.create(variant=cls.variant, image="product_colors/matching-black.webp")
        ProductFitOption.objects.bulk_create([
            ProductFitOption(product_id=product_id, code=code, label=label,
                             is_default=code == "classic", is_active=True)
            for product_id in (1, 4, 91, 110, 113, 120)
            for code, label in (("classic", "Класична"), ("oversize", "Оверсайз"))
        ])
        ProductOptionProfile.objects.bulk_create([
            ProductOptionProfile(product_id=product_id, option_key=f"fit={code}",
                                 option_values={"fit": code}, price_delta=delta)
            for product_id in (1, 4, 91, 110, 113, 120)
            for code, delta in (("classic", 0), ("oversize", 150))
        ])
        cls.classic_grid = cls._grid("Classic", "fit=classic", ("S", "M", "L"))
        cls.oversize_grid = cls._grid("Oversize", "fit=oversize", ("M", "L", "XL"))
        brigade = MerchCollection.objects.create(slug="new-brigade", kind="brigade", name_uk="New brigade")
        ProductMerchCollection.objects.create(product_id=120, collection=brigade)

    @classmethod
    def _grid(cls, name, key, sizes):
        grid = SizeGrid.objects.create(
            catalog=cls.catalog, name=name, is_active=True,
            guide_data={"columns": [{"key": "size", "label": "Size"}],
                        "rows": [{"size": size} for size in sizes]},
        )
        SizeGridProfile.objects.create(size_grid=grid, option_key=key)
        return grid

    def setUp(self):
        cache.clear()
        caches["fragments"].clear()

    def matching_row(self, payload):
        return next(row for row in payload["products"] if row["id"] == 4)

    def test_catalog_matching_first_real_images_prices_and_no_selected_size(self):
        payload = garment_bundle_tee_catalog(self.hoodie, language="uk")
        self.assertEqual((payload["hoodie_price"], payload["hoodie_offer_price"], payload["hoodie_unit_discount"]), (1995, 1945, 50))
        self.assertEqual(payload["total_saving"], 300)
        self.assertEqual([row["id"] for row in payload["products"]], [4, 1])
        row = self.matching_row(payload)
        self.assertEqual(row["image"], "/media/products/matching.webp")
        self.assertEqual(row["base_price"], 1100)
        variant = next(variant for variant in row["variants"] if variant["id"] == 1004)
        self.assertEqual(variant["image"], "/media/product_colors/matching-black.webp")
        self.assertIn("/my-little-baby/black/", variant["url"])
        fits = {fit["code"]: fit for fit in variant["fits"]}
        self.assertEqual(fits["classic"]["sizes"], ["S", "M", "L"])
        self.assertEqual(fits["oversize"]["sizes"], ["M", "L", "XL"])
        self.assertEqual(fits["classic"]["offer_unit_price"], 850)
        self.assertEqual(fits["oversize"]["offer_unit_price"], 1000)
        self.assertEqual(fits["oversize"]["standalone_unit_price"], 1250)
        self.assertEqual((fits["classic"]["pair_total"], fits["classic"]["total_saving"]), (2795, 300))
        self.assertEqual((fits["oversize"]["pair_total"], fits["oversize"]["total_saving"]), (2945, 300))
        self.assertEqual(fits["oversize"]["option_values"], {"fit": "oversize"})
        self.assertTrue(all("selected_size" not in fit for fit in variant["fits"]))
        other = payload["products"][1]["variants"][0]
        self.assertEqual([fit["offer_unit_price"] for fit in other["fits"]], [900, 1050])
        self.assertEqual([fit["total_saving"] for fit in other["fits"]], [250, 250])
        premium = next(variant for variant in row["variants"] if variant["id"] == 2004)
        self.assertEqual([fit["offer_unit_price"] for fit in premium["fits"]], [950, 1100])
        json.dumps(payload)

    def test_disabled_product_size_color_fit_and_stock_are_not_offered(self):
        ProductSizeRule.objects.create(product_id=4, option_key="fit=classic", size="S", is_enabled=False)
        VariantSizeRule.objects.create(variant_id=1004, fit_code="classic", size="M", is_enabled=True, stock=0)
        VariantFitRule.objects.create(variant_id=1004, fit_code="oversize", is_enabled=False)
        row = self.matching_row(garment_bundle_tee_catalog(self.hoodie))
        variant = next(variant for variant in row["variants"] if variant["id"] == 1004)
        self.assertEqual(len(variant["fits"]), 1)
        self.assertEqual(variant["fits"][0]["code"], "classic")
        self.assertEqual(variant["fits"][0]["sizes"], ["L"])

    def test_explicit_variant_grid_sizes_override_default_and_remain_valid(self):
        grid = self._grid("Exact black", "fit=oversize", ("XS", "S"))
        VariantOptionSizeGrid.objects.create(variant_id=1004, option_key="fit=oversize", size_grid=grid)
        row = self.matching_row(garment_bundle_tee_catalog(self.hoodie))
        fits = next(variant for variant in row["variants"] if variant["id"] == 1004)["fits"]
        self.assertEqual(next(fit for fit in fits if fit["code"] == "oversize")["sizes"], ["XS", "S"])

    def test_no_grid_legacy_sizes_match_purchase_fallback_and_rules(self):
        from product_catalog.services import variant_allows_purchase
        from storefront.services.size_guides import resolve_product_sizes

        SizeGrid.objects.all().delete()
        Product.objects.filter(pk=4).update(catalog=None)
        ProductSizeRule.objects.create(product_id=4, option_key="fit=classic", size="S", is_enabled=False)
        VariantSizeRule.objects.create(variant_id=1004, fit_code="", size="M", is_enabled=False)
        VariantSizeRule.objects.create(variant_id=1004, fit_code="classic", size="M", is_enabled=True)
        VariantSizeRule.objects.create(variant_id=1004, fit_code="oversize", size="L", stock=0)
        row = self.matching_row(garment_bundle_tee_catalog(self.hoodie))
        fits = {fit["code"]: fit for fit in next(variant for variant in row["variants"] if variant["id"] == 1004)["fits"]}
        self.assertEqual(fits["classic"]["sizes"], ["M", "L", "XL", "XXL"])
        self.assertEqual(fits["oversize"]["sizes"], ["S", "XL", "XXL"])
        product = Product.objects.select_related("category").get(pk=4)
        legacy = resolve_product_sizes(product)
        for code, fit in fits.items():
            self.assertEqual(fit["offer_unit_price"], 850 if code == "classic" else 1000)
            for size in fit["sizes"]:
                self.assertIn(size, legacy)
                self.assertTrue(variant_allows_purchase(product, self.variant, fit_code=code, size=size, option_values=fit["option_values"]))
        summary = garment_bundle_offer_summary(self.hoodie)
        self.assertTrue(summary["eligible"])
        self.assertEqual((summary["classic_price"], summary["oversize_price"]), (850, 1000))

    def test_summary_prices_only_purchasable_fits_with_real_material_and_variant_delta(self):
        VariantFitRule.objects.bulk_create([
            VariantFitRule(variant_id=1004, fit_code="classic", is_enabled=False),
            VariantFitRule(variant_id=2004, fit_code="classic", is_enabled=False),
        ])
        VariantDetails.objects.create(variant_id=1004, price_delta=60)
        summary = garment_bundle_offer_summary(self.hoodie)
        self.assertIsNone(summary["classic_price"])
        self.assertEqual(summary["oversize_price"], 1060)

    def test_no_sellable_sizes_removes_variant_and_draft_or_brigade_hoodie_is_ineligible(self):
        for code, sizes in (("classic", ("S", "M", "L")), ("oversize", ("M", "L", "XL"))):
            VariantSizeRule.objects.bulk_create([
                VariantSizeRule(variant_id=1004, fit_code=code, size=size, is_enabled=False)
                for size in sizes
            ])
        row = self.matching_row(garment_bundle_tee_catalog(self.hoodie))
        self.assertNotIn(1004, [variant["id"] for variant in row["variants"]])
        self.hoodie.status = "draft"
        self.assertFalse(garment_bundle_tee_catalog(self.hoodie)["eligible"])
        self.assertEqual(garment_bundle_tee_catalog(Product.objects.select_related("category").get(pk=92))["kind"], "225")

    def test_225_chooser_only_225_tees_and_central_both_item_prices(self):
        from decimal import Decimal
        from storefront.services.brigade_commerce import calculate_brigade_cart_pricing

        hoodie = Product.objects.select_related("category").get(pk=92)
        tee = Product.objects.select_related("category").get(pk=91)
        variant = ProductColorVariant.objects.get(pk=1091)
        payload = garment_bundle_tee_catalog(hoodie)
        self.assertEqual(payload["kind"], "225")
        self.assertEqual([row["id"] for row in payload["products"]], [91])
        self.assertEqual((payload["hoodie_price"], payload["hoodie_offer_price"], payload["hoodie_unit_discount"]), (1995, 1850, 145))
        self.assertEqual(payload["total_saving"], 225)
        fits = payload["products"][0]["variants"][0]["fits"]
        self.assertEqual([fit["standalone_unit_price"] for fit in fits], [880, 1030])
        self.assertEqual([fit["offer_unit_price"] for fit in fits], [800, 950])
        self.assertEqual([fit["pair_total"] for fit in fits], [2650, 2800])
        self.assertEqual([fit["total_saving"] for fit in fits], [225, 225])
        for fit in fits:
            quote = calculate_brigade_cart_pricing({
                "hoodie": {"product_id": 92, "qty": 1},
                "tee": {"product_id": 91, "qty": 1, "color_variant_id": 1091,
                        "fit_option_code": fit["code"], "option_values": fit["option_values"]},
            }, products={92: hoodie, 91: tee}, variants={1091: variant})
            self.assertEqual(quote.subtotal, Decimal(str(fit["pair_total"])))
        self.assertFalse(garment_bundle_offer_summary(hoodie)["eligible"])

    def test_225_chooser_taxonomy_and_actual_lower_sale_price(self):
        collection = MerchCollection.objects.create(slug="225", kind="brigade", name_uk="225")
        ProductMerchCollection.objects.create(product_id=1, collection=collection)
        ProductMerchCollection.objects.create(product_id=113, collection=collection)
        Product.objects.filter(pk=91).update(discount_percent=30)
        hoodie = Product.objects.select_related("category").get(pk=92)
        hoodie.discount_percent = 30
        payload = garment_bundle_tee_catalog(hoodie)
        self.assertEqual({row["id"] for row in payload["products"]}, {1, 91})
        self.assertEqual((payload["hoodie_price"], payload["hoodie_offer_price"], payload["hoodie_unit_discount"]), (1396, 1396, 0))
        tee_row = next(row for row in payload["products"] if row["id"] == 91)
        fits = tee_row["variants"][0]["fits"]
        self.assertEqual([fit["offer_unit_price"] for fit in fits], [616, 766])
        self.assertEqual([fit["total_saving"] for fit in fits], [0, 0])

    def test_fixed_other_axis_is_included_with_legitimate_delta(self):
        flow = GarmentFlow.objects.create(code="tee-flow", name="Tee", axes=[
            {"code": "material", "label": "Material", "options": [{"code": "cotton", "label": "Cotton", "default": True}]},
        ])
        GarmentFlowCategory.objects.create(flow=flow, category=self.tee_category)
        ProductOptionProfile.objects.create(product_id=4, option_key="material=cotton", price_delta=30)
        VariantDetails.objects.create(variant_id=1004, price_delta=20)
        row = self.matching_row(garment_bundle_tee_catalog(self.hoodie))
        fits = next(variant for variant in row["variants"] if variant["id"] == 1004)["fits"]
        self.assertEqual(fits[0]["option_values"], {"fit": "classic", "material": "cotton"})
        self.assertEqual([fit["offer_unit_price"] for fit in fits], [900, 1050])

    def test_sale_price_wins_without_stacking_and_special_hoodie_remains1850(self):
        Product.objects.filter(pk=4).update(discount_percent=30)
        hoodie = Product.objects.select_related("category").get(pk=11)
        payload = garment_bundle_tee_catalog(hoodie)
        self.assertEqual(payload["hoodie_price"], 1850)
        self.assertEqual((payload["hoodie_offer_price"], payload["hoodie_unit_discount"]), (1800, 50))
        fits = next(variant for variant in self.matching_row(payload)["variants"] if variant["id"] == 1004)["fits"]
        self.assertEqual([fit["offer_unit_price"] for fit in fits], [770, 920])
        self.assertEqual([fit["total_saving"] for fit in fits], [50, 50])
        self.assertEqual([fit["pair_total"] for fit in fits], [2570, 2720])
        tee = Product.objects.select_related("category").get(pk=4)
        summary = garment_bundle_offer_summary(tee)
        self.assertEqual((summary["classic_price"], summary["total_saving"], summary["pair_total"]), (770, 50, 2715))

    def test_hoodie_discount_is_capped_and_savings_stay_actual(self):
        self.hoodie.price = 40
        payload = garment_bundle_tee_catalog(self.hoodie)
        self.assertEqual(payload["hoodie_offer_price"], 0.01)
        self.assertEqual(payload["hoodie_unit_discount"], 39.99)
        self.assertEqual(payload["total_saving"], 289.99)

    def test_public_cache_has_constant_cold_queries_and_warm_no_catalog_queries(self):
        with CaptureQueriesContext(connection) as cold:
            first = garment_bundle_tee_catalog(self.hoodie)
        self.assertLessEqual(len(cold), 45, [query["sql"] for query in cold])
        with self.assertNumQueries(0):
            second = garment_bundle_tee_catalog(self.hoodie)
        self.assertEqual(first, second)
        ProductOptionProfile.objects.filter(product_id=4, option_key="fit=oversize").update(price_delta=175)
        bump_public_product_order_version()
        row = self.matching_row(garment_bundle_tee_catalog(self.hoodie))
        self.assertEqual(next(fit for fit in row["variants"][0]["fits"] if fit["code"] == "oversize")["offer_unit_price"], 1025)

    def test_cheap_summary_forward_reverse_actual_price_and_locale(self):
        with CaptureQueriesContext(connection) as queries:
            summary = garment_bundle_offer_summary(self.hoodie, language="en")
        # Real category photos add one bounded query; image fallbacks stay in SQL.
        self.assertLessEqual(len(queries), 21, [re.findall(r'FROM "([^"]+)"', row["sql"]) for row in queries])
        self.assertEqual(summary["kind"], "hoodie")
        self.assertEqual(summary["tee_id"], 4)
        self.assertEqual(summary["title"], "Matching tee EN")
        self.assertEqual((summary["classic_price"], summary["oversize_price"]), (850, 1000))
        self.assertEqual((summary["hoodie_price"], summary["hoodie_offer_price"], summary["hoodie_unit_discount"]), (1995, 1945, 50))
        self.assertEqual((summary["classic_saving"], summary["oversize_saving"], summary["total_saving"]), (300, 300, 300))
        self.assertEqual((summary['same_print_saving'], summary['other_print_saving']), (300, 250))
        self.assertEqual((summary["classic_pair_total"], summary["oversize_pair_total"], summary["pair_total"]), (2795, 2945, 2795))
        tee = Product.objects.select_related("category").get(pk=4)
        reverse = garment_bundle_offer_summary(tee)
        self.assertEqual((reverse["kind"], reverse["hoodie_id"]), ("tee", 5))
        self.assertFalse(garment_bundle_offer_summary(Product.objects.select_related("category").get(pk=110))["eligible"])

    def test_summary_does_not_claim_matching_tier_when_matching_partner_is_unavailable(self):
        Product.objects.filter(pk=5).update(status='draft')
        tee = Product.objects.select_related('category').get(pk=4)
        summary = garment_bundle_offer_summary(tee)
        self.assertEqual(summary['hoodie_id'], 11)
        self.assertEqual((summary['same_print_saving'], summary['other_print_saving'], summary['total_saving']), (0, 250, 250))
        self.assertFalse(summary['is_same_design'])

    def test_summary_savings_are_floored_and_ordinary_fit_tiers_share_central_prices(self):
        self.hoodie.price = 40
        summary = garment_bundle_offer_summary(self.hoodie)
        self.assertEqual((summary['same_print_saving'], summary['other_print_saving'], summary['total_saving']), (289, 239, 289))
        fits = self.matching_row(garment_bundle_tee_catalog(self.hoodie))['variants'][0]['fits']
        self.assertEqual([(fit['same_print_saving'], fit['other_print_saving']) for fit in fits], [(289.99, 239.99), (289.99, 239.99)])

    def test_summary_other_tier_requires_real_published_other_partner(self):
        Product.objects.filter(pk=11).update(status='draft')
        tee = Product.objects.select_related('category').get(pk=4)
        summary = garment_bundle_offer_summary(tee)
        self.assertEqual((summary['same_print_saving'], summary['other_print_saving'], summary['total_saving']), (300, 0, 300))

    def test_summary_links_entire_opposite_category_in_each_language(self):
        tee = Product.objects.select_related("category").get(pk=4)
        for language, prefix in (("uk", ""), ("ru", "/ru"), ("en", "/en")):
            for product, category in ((tee, "hoodie"), (self.hoodie, "tshirts")):
                with self.subTest(language=language, category=category):
                    summary = garment_bundle_offer_summary(product, language=language)
                    self.assertEqual(summary["category_url"], f"{prefix}/catalog/{category}/")
                    self.assertEqual(summary["total_saving"], 300)
                    if category == "tshirts":
                        self.assertEqual(summary["category_previews"][0]["title"],
                                         "Matching tee EN" if language == "en" else "Matching tee")
        with self.assertNumQueries(0):
            garment_bundle_offer_summary(tee, language="en")

    def test_category_previews_prefer_reviewed_prints_and_exclude_bare_and_ineligible(self):
        from storefront.services.garment_bundle_catalog import _category_previews

        Product.objects.bulk_create([
            Product(pk=13, slug="business-money", title="Money", category=self.tee_category,
                    status="published", price=1100, main_image="products/money.webp"),
            Product(pk=16, slug="last-breath", title="Breath", category=self.tee_category,
                    status="published", price=1100, main_image="products/breath.webp"),
            Product(pk=22, slug="pokrovsk-girl", title="Girl", category=self.tee_category,
                    status="published", price=1100, main_image="products/girl.webp"),
        ])
        Product.objects.filter(pk__in=(91, 110, 113, 120)).update(main_image="products/excluded.webp")
        with CaptureQueriesContext(connection) as queries:
            previews = _category_previews("tshirts", (4,), "uk")
        self.assertEqual(len(queries), 1)
        self.assertEqual([row["id"] for row in previews], [4, 13, 16])
        self.assertEqual([row["is_same_design"] for row in previews], [True, False, False])
        self.assertTrue(all(row["image"] and row["title"] and row["url"] for row in previews))
        self.assertTrue(all({"id", "title", "image"} <= row.keys() for row in previews))

    def test_category_previews_deduplicate_images_and_use_real_variant_gallery_fallbacks(self):
        from storefront.services.garment_bundle_catalog import _category_previews

        Product.objects.bulk_create([
            Product(pk=2, slug="hoodie-classic", title="Bare", category=self.hoodie_category,
                    status="published", price=1995, main_image="products/bare.webp"),
            Product(pk=14, slug="business-money-hd", title="Duplicate", category=self.hoodie_category,
                    status="published", price=1995, main_image="products/shared.webp"),
            Product(pk=17, slug="last-breath-hd", title="Gallery", category=self.hoodie_category,
                    status="published", price=1995),
            Product(pk=23, slug="pokrovsk-girl-hd", title="Other", category=self.hoodie_category,
                    status="published", price=1995, main_image="products/other.webp"),
        ])
        variant = ProductColorVariant.objects.create(product_id=5, color=self.color, slug="black", is_default=True)
        ProductColorImage.objects.create(variant=variant, image="products/shared.webp")
        ProductImage.objects.create(product_id=17, image="products/gallery.webp")
        with self.assertNumQueries(1):
            previews = _category_previews("hoodie", (5,), "en")
        self.assertEqual([row["id"] for row in previews], [5, 17, 23])
        self.assertEqual([row["image"] for row in previews], [
            "/media/products/shared.webp", "/media/products/gallery.webp", "/media/products/other.webp",
        ])
        self.assertTrue(all(row["url"].startswith("/en/") for row in previews))

    def test_category_previews_exact_identity_and_only_actual_image_fallback(self):
        from storefront.services.garment_bundle_catalog import _category_previews

        Product.objects.bulk_create([
            Product(pk=2, slug="hoodie-classic", title="Bare", category=self.hoodie_category,
                    status="published", price=1995, main_image="products/bare.webp"),
            Product(pk=14, slug="changed-money-hd", title="Renamed", category=self.hoodie_category,
                    status="published", price=1995, main_image="products/renamed.webp"),
        ])
        previews = _category_previews("hoodie", (14,), "uk")
        self.assertEqual([row["id"] for row in previews], [14])
        self.assertFalse(previews[0]["is_same_design"])
        Product.objects.filter(pk=14).update(main_image="")
        self.assertEqual([row["id"] for row in _category_previews("hoodie", (), "uk")], [2])
        Product.objects.filter(pk=2).update(main_image="")
        self.assertEqual(_category_previews("hoodie", (), "uk"), [])
