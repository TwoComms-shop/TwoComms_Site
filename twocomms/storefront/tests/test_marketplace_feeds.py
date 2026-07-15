from collections import defaultdict
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductStatus


G_NS = {"g": "http://base.google.com/ns/1.0"}


@override_settings(
    SITE_BASE_URL="https://twocomms.shop",
    FEED_BASE_URL="https://twocomms.shop",
    ROZETKA_CATEGORY_RZ_ID_MAP={"futbolki": "4637839"},
)
class MarketplaceFeedServiceTests(TestCase):
    def setUp(self):
        super().setUp()
        merchant_patcher = patch("storefront.signals.generate_google_merchant_feed_task.apply_async")
        indexnow_patcher = patch("storefront.signals.enqueue_indexnow_urls")
        self.addCleanup(merchant_patcher.stop)
        self.addCleanup(indexnow_patcher.stop)
        merchant_patcher.start()
        indexnow_patcher.start()

        self.category = Category.objects.create(name="Футболки", slug="futbolki", is_active=True)
        self.product = Product.objects.create(
            title="Чорна футболка унісекс TwoComms «Довіряй своїй божевільній ідеї»",
            slug="twocomms-test-shirt",
            category=self.category,
            price=1500,
            discount_percent=20,
            full_description="Український опис товару з характером.",
            description="Український опис товару з характером.",
            seo_schema={"video_link": "https://twocomms.shop/media/products/test-shirt.mp4"},
            main_image="products/test-shirt-main.jpg",
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Чорний", primary_hex="#000000")
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            sku="TWC-TEST-BLACK",
            barcode="4006381333931",
            stock=7,
        )
        Product.objects.create(
            title="Draft Product",
            slug="draft-product",
            category=self.category,
            price=999,
            status=ProductStatus.DRAFT,
        )

    def test_rozetka_feed_contains_required_fields_for_every_size_offer(self):
        from storefront.services.marketplace_feeds import build_rozetka_feed_xml

        root = ET.fromstring(build_rozetka_feed_xml(base_url="https://twocomms.shop"))
        categories = root.find("shop/categories")
        category = categories.find("category")
        offers = root.findall("shop/offers/offer")

        self.assertEqual(category.attrib["id"], str(self.category.id))
        self.assertEqual(category.attrib["rz_id"], "4637839")
        self.assertEqual(len(offers), 5)

        articles = {offer.findtext("article") for offer in offers}
        offer_ids = {offer.attrib["id"] for offer in offers}
        self.assertEqual(articles, {"TWC-TEST-BLACK"})
        self.assertNotIn("TWC-TEST-BLACK", offer_ids)

        first = offers[0]
        self.assertEqual(first.attrib["available"], "true")
        self.assertEqual(first.findtext("price"), "1200")
        self.assertEqual(first.findtext("price_old"), "1500")
        self.assertEqual(first.findtext("currencyId"), "UAH")
        self.assertEqual(first.findtext("categoryId"), str(self.category.id))
        self.assertEqual(first.findtext("vendor"), "TwoComms")
        # stock_quantity має бути floor=100 (FEED_MIN_QUANTITY), бо TwoComms працює як
        # made-to-order DTF — реальне variant.stock у БД може бути 0/7/3, але фід
        # завжди декларує наявність ≥100, інакше Rozetka / Kasta / Prom показують
        # товар як «sold out».
        self.assertEqual(first.findtext("stock_quantity"), "100")
        self.assertTrue(first.findtext("picture").startswith("https://twocomms.shop/media/products/"))
        self.assertIn("Чорний", first.findtext("name_ua"))
        self.assertIn("Черная футболка унисекс TwoComms", first.findtext("name"))
        self.assertNotIn("Чорна", first.findtext("name"))
        self.assertIn("Эксклюзивный дизайн", first.findtext("description"))
        self.assertIn("Черная футболка унисекс TwoComms", first.findtext("description"))
        self.assertNotIn("Довіряй", first.findtext("description"))
        self.assertNotIn("Український опис", first.findtext("description"))

        params = {(param.attrib["name"], param.text) for param in first.findall("param")}
        self.assertIn(("Розмір", first.findtext("param")), params)
        self.assertIn(("Колір", "Чорний"), params)
        self.assertIn(("Країна-виробник товару", "Україна"), params)

    def test_google_feed_uses_current_merchant_attributes_and_valid_identifiers(self):
        from storefront.services.marketplace_feeds import build_google_merchant_feed_xml

        root = ET.fromstring(build_google_merchant_feed_xml(base_url="https://twocomms.shop"))
        items = root.findall("./channel/item")

        self.assertEqual(root.tag, "rss")
        self.assertEqual(len(items), 5)

        first = items[0]
        self.assertEqual(first.findtext("g:item_group_id", namespaces=G_NS), f"TC-GROUP-{self.product.id}")
        self.assertTrue(first.findtext("g:id", namespaces=G_NS).startswith("TC-"))
        self.assertIn("Чорний", first.findtext("g:title", namespaces=G_NS))
        self.assertEqual(first.findtext("g:availability", namespaces=G_NS), "in_stock")
        self.assertEqual(first.findtext("g:price", namespaces=G_NS), "1500.00 UAH")
        self.assertEqual(first.findtext("g:sale_price", namespaces=G_NS), "1200.00 UAH")
        self.assertEqual(first.findtext("g:brand", namespaces=G_NS), "TwoComms")
        self.assertEqual(first.findtext("g:gtin", namespaces=G_NS), "4006381333931")
        self.assertEqual(first.findtext("g:mpn", namespaces=G_NS), f"TWC-TEST-BLACK-{self.product.id}")
        self.assertEqual(first.findtext("g:google_product_category", namespaces=G_NS), "1604")
        self.assertEqual(first.findtext("g:condition", namespaces=G_NS), "new")
        self.assertEqual(
            first.findtext("g:link", namespaces=G_NS),
            f"https://twocomms.shop/product/{self.product.slug}/black/s/",
        )
        self.assertEqual(
            first.findtext("g:video_link", namespaces=G_NS),
            "https://twocomms.shop/media/products/test-shirt.mp4",
        )
        self.assertGreaterEqual(len(first.findall("g:product_detail", namespaces=G_NS)), 5)
        self.assertGreaterEqual(len(first.findall("g:product_highlight", namespaces=G_NS)), 4)

    def test_feed_routes_return_dynamic_xml(self):
        response = self.client.get("/rozetka-feed.xml", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        self.assertIn(b"<yml_catalog", response.content)

        google = self.client.get("/google_merchant_feed.xml", secure=True)
        self.assertEqual(google.status_code, 200)
        self.assertIn(b"<g:id>", google.content)

        kasta = self.client.get("/kasta-feed.xml", secure=True)
        self.assertEqual(kasta.status_code, 200)
        self.assertIn(b"<name_ru>", kasta.content)
        self.assertIn(b"<yml_catalog", kasta.content)

        buyme = self.client.get("/buyme-feed.xml", secure=True)
        self.assertEqual(buyme.status_code, 200)
        self.assertIn(b'group_id="buyme-', buyme.content)
        self.assertIn(b"<priceDrop>", buyme.content)
        self.assertIn(b"<yml_catalog", buyme.content)

    @override_settings(FEED_BASE_URL="")
    def test_feed_routes_fallback_to_configured_https_origin_on_http_request(self):
        response = self.client.get(
            "/google_merchant_feed.xml",
            secure=False,
            HTTP_HOST="twocomms.shop",
            SERVER_PORT="80",
            **{"wsgi.url_scheme": "http"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"https://twocomms.shop", response.content)
        self.assertNotIn(b"http://twocomms.shop", response.content)

    def test_bezzet_products_feed_keeps_zero_variant_stock_available(self):
        from storefront.services.marketplace_feeds import build_uaprom_products_feed_xml

        self.variant.stock = 0
        self.variant.save(update_fields=["stock"])

        root = ET.fromstring(build_uaprom_products_feed_xml(base_url="https://twocomms.shop"))
        offers = root.findall("shop/offers/offer")

        self.assertEqual(len(offers), 5)
        self.assertTrue(all(offer.attrib["available"] == "true" for offer in offers))
        self.assertTrue(all(offer.findtext("stock_quantity") == "100" for offer in offers))
        self.assertTrue(all(offer.findtext("quantity_in_stock") == "100" for offer in offers))
        self.assertTrue(all(offer.findtext("picture") for offer in offers))

    def test_all_marketplace_feeds_keep_zero_variant_stock_available(self):
        """Регресійний тест для багу 2026-05-13: коли variant.stock=0 (made-to-order),
        Rozetka / Kasta / Prom раніше віддавали stock_quantity=0, через що Bezzet
        та інші marketplaces позначали товари як «sold out» попри available="true".
        """
        from storefront.services.marketplace_feeds import (
            build_buyme_feed_xml,
            build_google_merchant_feed_xml,
            build_kasta_feed_xml,
            build_prom_feed_xml,
            build_rozetka_feed_xml,
            build_uaprom_products_feed_xml,
        )

        # variant.stock=0 у setUp вже передбачено для іншого тесту, тут зануляємо явно
        self.variant.stock = 0
        self.variant.save(update_fields=["stock"])

        for label, build in (
            ("rozetka", build_rozetka_feed_xml),
            ("kasta", build_kasta_feed_xml),
            ("prom", build_prom_feed_xml),
            ("bezzet", build_uaprom_products_feed_xml),
        ):
            with self.subTest(feed=label):
                root = ET.fromstring(build(base_url="https://twocomms.shop"))
                offers = root.findall("shop/offers/offer")
                self.assertEqual(len(offers), 5, f"{label}: expected 5 offers")
                for offer in offers:
                    self.assertEqual(offer.attrib.get("available"), "true", label)
                    sq = offer.findtext("stock_quantity") or ""
                    self.assertTrue(sq.isdigit(), f"{label}: stock_quantity not numeric ({sq!r})")
                    self.assertGreaterEqual(int(sq), 100, f"{label}: stock_quantity < 100")

        # BuyMe використовує quantity_in_stock замість stock_quantity
        root = ET.fromstring(build_buyme_feed_xml(base_url="https://twocomms.shop"))
        for offer in root.findall("shop/offers/offer"):
            self.assertEqual(offer.attrib.get("available"), "true")
            qis = offer.findtext("quantity_in_stock") or ""
            self.assertTrue(qis.isdigit(), f"buyme quantity_in_stock not numeric ({qis!r})")
            self.assertGreaterEqual(int(qis), 100)

        # Google Merchant: лише g:availability, без stock_quantity
        root = ET.fromstring(build_google_merchant_feed_xml(base_url="https://twocomms.shop"))
        ns = {"g": "http://base.google.com/ns/1.0"}
        items = root.findall("./channel/item")
        for item in items:
            self.assertEqual(item.findtext("g:availability", namespaces=ns), "in_stock")

    def test_bezzet_products_feed_groups_by_color_to_avoid_duplicate_sizes(self):
        from storefront.services.marketplace_feeds import build_uaprom_products_feed_xml

        white = Color.objects.create(name="Білий", primary_hex="#FFFFFF")
        ProductColorVariant.objects.create(
            product=self.product,
            color=white,
            sku="TWC-TEST-WHITE",
            barcode="",
            stock=5,
        )

        root = ET.fromstring(build_uaprom_products_feed_xml(base_url="https://twocomms.shop"))
        offers = root.findall("shop/offers/offer")

        sizes_by_group = defaultdict(list)
        colors_by_group = defaultdict(set)
        for offer in offers:
            params = {param.attrib.get("name"): param.text for param in offer.findall("param")}
            group_id = offer.attrib["group_id"]
            sizes_by_group[group_id].append(params["Розмір"])
            colors_by_group[group_id].add(params["Колір"])

        self.assertEqual(len(offers), 10)
        self.assertEqual(len(sizes_by_group), 2)
        for group_id, sizes in sizes_by_group.items():
            self.assertEqual(len(sizes), len(set(sizes)), group_id)
            self.assertEqual(len(colors_by_group[group_id]), 1, group_id)

    def test_products_feed_bot_request_does_not_set_analytics_cookie(self):
        response = self.client.get(
            "/products_feed.xml",
            secure=True,
            HTTP_USER_AGENT="BezzetBot/1.0 (+feed import)",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        self.assertIn(b"<yml_catalog", response.content)
        self.assertNotIn("twc_vid", response.cookies)

    def test_kasta_feed_uses_kasta_specific_grouping_names_and_descriptions(self):
        from storefront.services.marketplace_feeds import build_kasta_feed_xml

        white = Color.objects.create(name="Білий", primary_hex="#FFFFFF")
        ProductColorVariant.objects.create(
            product=self.product,
            color=white,
            sku="TWC-TEST-WHITE",
            barcode="",
            stock=3,
        )
        self.product.full_description = "Український опис з посиланням https://example.com/video та зайвою ціною 1200 грн."
        self.product.description = self.product.full_description
        self.product.save(update_fields=["full_description", "description"])

        root = ET.fromstring(build_kasta_feed_xml(base_url="https://twocomms.shop"))
        categories = root.findall("shop/categories/category")
        offers = root.findall("shop/offers/offer")

        self.assertEqual(len(offers), 10)
        self.assertTrue(all(category.attrib.get("rz_id") for category in categories))

        articles = {offer.findtext("article") for offer in offers}
        offer_ids = {offer.attrib["id"] for offer in offers}
        name_ua_values = {offer.findtext("name_ua") for offer in offers}
        name_ru_values = {offer.findtext("name_ru") for offer in offers}

        self.assertEqual(len(articles), 1)
        self.assertNotIn("TWC-TEST-BLACK", articles)
        self.assertTrue(all(offer_id not in articles for offer_id in offer_ids))
        self.assertEqual(name_ua_values, {"Чорна футболка унісекс TwoComms «Довіряй своїй божевільній ідеї»"})
        self.assertEqual(name_ru_values, {"Черная футболка унисекс TwoComms «Доверяй своей безумной идее»"})
        self.assertTrue(all("Чорний" not in name for name in name_ua_values))
        self.assertTrue(all("Чорна" not in name for name in name_ru_values))

        first = offers[0]
        self.assertEqual(first.findtext("price"), "1200")
        self.assertEqual(first.findtext("price_old"), "1500")
        self.assertEqual(first.findtext("vendor"), "TwoComms")
        self.assertEqual(first.findtext("state"), "new")
        self.assertLessEqual(len(first.findtext("description_ua")), 5000)
        self.assertIn("Черная футболка унисекс TwoComms", first.findtext("description"))
        self.assertNotIn("Чорна", first.findtext("description"))
        self.assertNotIn("https://", first.findtext("description_ua"))
        self.assertNotIn("грн", first.findtext("description_ua").lower())

        params = {(param.attrib["name"], param.text) for param in first.findall("param")}
        self.assertIn(("Колір", "Чорний"), params)
        self.assertIn(("Розмір", "S"), params)
        self.assertIn(("Країна виробництва", "Україна"), params)
        self.assertTrue(all(name and value for name, value in params))

    def test_buyme_feed_groups_variants_uses_drop_discount_and_removes_brands(self):
        from storefront.services.marketplace_feeds import build_buyme_feed_xml

        white = Color.objects.create(name="Білий", primary_hex="#FFFFFF")
        ProductColorVariant.objects.create(
            product=self.product,
            color=white,
            sku="NIKE-TWC-TEST-WHITE",
            barcode="",
            stock=0,
        )
        self.product.title = "Чорна футболка унісекс TwoComms Nike «Довіряй своїй божевільній ідеї»"
        self.product.full_description = "TwoComms Nike опис з посиланням https://example.com і ціною 1200 грн."
        self.product.description = self.product.full_description
        self.product.recommended_price = 1800
        self.product.discount_percent = 17
        self.product.save(update_fields=["title", "full_description", "description", "recommended_price", "discount_percent"])

        xml_payload = build_buyme_feed_xml(base_url="https://twocomms.shop")
        root = ET.fromstring(xml_payload)
        categories = root.findall("shop/categories/category")
        offers = root.findall("shop/offers/offer")
        non_image_text = " ".join(
            filter(
                None,
                [
                    element.text
                    for element in root.iter()
                    if element.tag != "picture"
                ],
            )
        ).lower()

        self.assertEqual(len(categories), 1)
        self.assertEqual(len(offers), 10)
        self.assertNotIn("twocomms", non_image_text)
        self.assertNotIn("nike", non_image_text)
        self.assertNotIn(b"<vendor>", xml_payload)

        group_ids = {offer.attrib.get("group_id") for offer in offers}
        offer_ids = {offer.attrib["id"] for offer in offers}
        names = {offer.findtext("name") for offer in offers}

        self.assertEqual(len(group_ids), 2)
        for group_id in group_ids:
            grouped_offers = [offer for offer in offers if offer.attrib.get("group_id") == group_id]
            grouped_colors = {
                param.text
                for offer in grouped_offers
                for param in offer.findall("param")
                if param.attrib.get("name") == "Колір"
            }
            grouped_sizes = {
                param.text
                for offer in grouped_offers
                for param in offer.findall("param")
                if param.attrib.get("name") == "Розмір"
            }
            self.assertEqual(len(grouped_colors), 1, group_id)
            self.assertEqual(grouped_sizes, {"S", "M", "L", "XL", "XXL"}, group_id)
        self.assertEqual(len(offer_ids), 10)
        self.assertEqual(names, {"Брендова футболка унісекс «Довіряй своїй божевільній ідеї»"})

        first = offers[0]
        self.assertEqual(first.attrib["available"], "true")
        self.assertEqual(first.findtext("price"), "1800.00")
        self.assertEqual(first.findtext("priceDrop"), "1260.00")
        self.assertEqual(first.findtext("currencyId"), "UAH")
        self.assertEqual(first.findtext("categoryId"), str(self.category.id))
        self.assertEqual(first.findtext("quantity_in_stock"), "100")
        zero_stock_offer = next(offer for offer in offers if offer.attrib["id"].startswith("buyme-1-v2"))
        self.assertEqual(zero_stock_offer.attrib["available"], "true")
        self.assertEqual(zero_stock_offer.findtext("quantity_in_stock"), "100")
        self.assertIsNone(first.find("url"))
        self.assertTrue(first.findtext("picture").startswith("https://twocomms.shop/media/products/"))
        self.assertLessEqual(len(first.findtext("description_ua")), 3000)
        self.assertIn("Брендова футболка", first.findtext("description_ua"))
        self.assertNotIn("TwoComms", first.findtext("description_ua"))

        params = {(param.attrib["name"], param.text) for param in first.findall("param")}
        self.assertIn(("Колір", "Чорний"), params)
        self.assertIn(("Розмір", "S"), params)
        self.assertIn(("Країна виробництва", "Україна"), params)

    def test_management_commands_write_distinct_marketplace_files(self):
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            google_path = tmp_path / "google.xml"
            rozetka_path = tmp_path / "rozetka.xml"
            kasta_path = tmp_path / "kasta.xml"
            buyme_path = tmp_path / "buyme.xml"
            prom_path = tmp_path / "prom.xml"

            call_command("generate_google_merchant_feed", output=str(google_path), base_url="https://twocomms.shop")
            call_command("generate_rozetka_feed", output=str(rozetka_path), base_url="https://twocomms.shop")
            call_command("generate_kasta_feed", output=str(kasta_path), base_url="https://twocomms.shop")
            call_command("generate_buyme_feed", output=str(buyme_path), base_url="https://twocomms.shop")
            call_command("generate_prom_feed", output=str(prom_path), base_url="https://twocomms.shop")

            google_xml = google_path.read_text(encoding="utf-8")
            rozetka_xml = rozetka_path.read_text(encoding="utf-8")
            kasta_xml = kasta_path.read_text(encoding="utf-8")
            buyme_xml = buyme_path.read_text(encoding="utf-8")
            prom_xml = prom_path.read_text(encoding="utf-8")

        self.assertIn("<rss", google_xml)
        self.assertIn("<g:id>", google_xml)
        self.assertIn("<yml_catalog", rozetka_xml)
        self.assertIn("<article>TWC-TEST-BLACK</article>", rozetka_xml)
        self.assertNotIn("<g:id>", rozetka_xml)
        self.assertIn("<yml_catalog", kasta_xml)
        self.assertIn("<name_ru>Черная футболка унисекс TwoComms", kasta_xml)
        self.assertIn("<article>TC", kasta_xml)
        self.assertNotIn("<article>TWC-TEST-BLACK</article>", kasta_xml)
        self.assertNotIn("<g:id>", kasta_xml)
        self.assertIn("<yml_catalog", buyme_xml)
        self.assertIn('group_id="buyme-', buyme_xml)
        self.assertIn("<price>1500.00</price>", buyme_xml)
        self.assertIn("<priceDrop>1050.00</priceDrop>", buyme_xml)
        self.assertIn("<picture>https://twocomms.shop/media/products/", buyme_xml)
        self.assertNotIn("TwoComms</", buyme_xml)
        self.assertNotIn("<vendor>", buyme_xml)
        self.assertNotIn("<g:id>", buyme_xml)
        self.assertIn("<oldprice>1500</oldprice>", prom_xml)
        self.assertNotIn("<article>", prom_xml)

    def test_snapshot_regenerator_refreshes_every_file_backed_marketplace_feed(self):
        with TemporaryDirectory() as tmp_dir:
            media_root = Path(tmp_dir)

            def write_snapshot(command_name, *, output, **kwargs):
                Path(output).write_text(command_name, encoding="utf-8")

            with override_settings(MEDIA_ROOT=media_root), patch(
                "storefront.management.commands.regenerate_feeds_if_dirty.call_command",
                side_effect=write_snapshot,
            ) as generate:
                call_command("regenerate_feeds_if_dirty", force=True)

            generated = {
                call.args[0]: Path(call.kwargs["output"]).name
                for call in generate.call_args_list
            }
            self.assertTrue((media_root / "google-merchant-v2.xml").exists())
            self.assertEqual(
                (media_root / "google-merchant-v2.xml").read_bytes(),
                (media_root / "google-merchant-v3.xml").read_bytes(),
            )

        self.assertEqual(
            generated,
            {
                "generate_google_merchant_feed": "google-merchant-v3.xml",
                "generate_rozetka_feed": "rozetka-feed.xml",
                "generate_kasta_feed": "kasta-feed.xml",
                "generate_buyme_feed": "buyme-feed.xml",
                "generate_prom_feed": "prom-feed.xml",
                "generate_instagram_feed": "instagram-feed.xml",
            },
        )
