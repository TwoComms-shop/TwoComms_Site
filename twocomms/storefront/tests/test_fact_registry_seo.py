from decimal import Decimal

from django.test import Client, TestCase, override_settings


class PublicFactRegistrySeoTests(TestCase):
    def test_homepage_cache_version_changes_when_public_fact_contract_changes(self):
        from storefront.views.catalog import HOME_SEO_FACTS_CACHE_VERSION

        self.assertEqual(HOME_SEO_FACTS_CACHE_VERSION, "seo-facts-v3-20260813-geo")

    def test_organization_schema_does_not_publish_unverified_foundation_or_postal_address(self):
        from storefront.seo_utils import StructuredDataGenerator

        schema = StructuredDataGenerator.generate_organization_schema()

        self.assertNotIn("foundingDate", schema)
        self.assertNotIn("foundingLocation", schema)
        self.assertNotIn("address", schema)

    @override_settings(FREE_SHIPPING_THRESHOLD="2750")
    def test_llms_txt_uses_checkout_shipping_threshold(self):
        response = Client().get("/llms.txt")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("orders of 2750 UAH and above ship for free", body)
        self.assertNotIn("orders of 3000 UAH and above ship for free", body)

    def test_llms_files_omit_unowned_brand_and_delivery_claims(self):
        client = Client()
        for path in ("/llms.txt", "/llms-full.txt"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                body = response.content.decode("utf-8")
                for marker in (
                    "Founded: 2022",
                    "Catalogue price range: approximately",
                    "Payment methods:",
                    "Production / handling time: 1–2 business days",
                    "Standard shipping window: 1–3 business days",
                    "DTF print lead time: 3–5 business days",
                    "Return policy: 14-day return window",
                    "Return method: return by Nova Poshta",
                    "Loyalty program:",
                    "City: Харків",
                    "Виробництво в Україні (Made in UA).",
                    "Частина прибутку йде на потреби ЗСУ.",
                    "DTF-друк, що витримує тривалу експлуатацію.",
                    "Нова Пошта по всій Україні (1–3 робочі дні).",
                    "## FAQ (зведений каталог)",
                ):
                    self.assertNotIn(marker, body)
                self.assertIn("custom-print/", body)
                self.assertIn("Origin city: Kharkiv / Харків, Ukraine", body)
                self.assertIn("Founder: Артем Синіло", body)
                self.assertIn("Founder story (external source)", body)
                self.assertIn("https://mva.gov.ua/", body)

    def test_llms_full_uses_public_product_eligibility(self):
        from storefront.models import Category, Product

        category = Category.objects.create(
            name="LLMS eligibility", slug="llms-eligibility", is_active=True
        )
        Product.objects.create(
            title="Zero price draft", slug="zero-price-llms", category=category,
            price=0, status="published",
        )
        Product.objects.create(
            title="Public price item", slug="public-price-llms", category=category,
            price=100, status="published",
        )

        response = Client().get("/llms-full.txt")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("zero-price-llms", body)
        self.assertIn("public-price-llms", body)

    def test_fact_registry_records_owner_source_locale_and_effective_date(self):
        from storefront.services.fact_registry import PUBLIC_FACTS_VERSION, PUBLIC_FACTS

        self.assertTrue(PUBLIC_FACTS_VERSION)
        shipping = PUBLIC_FACTS["free_shipping_threshold"]
        self.assertEqual(shipping.owner, "checkout_settings")
        self.assertEqual(shipping.locale, "all")
        self.assertTrue(shipping.source)
        self.assertTrue(shipping.effective_date)

    def test_contacts_schema_does_not_publish_unverified_store_coordinates_or_hours(self):
        response = Client().get("/contacts/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn('"geo":', body)
        self.assertNotIn('"openingHoursSpecification":', body)
        self.assertNotIn('"postalCode":', body)

    def test_standard_pages_do_not_publish_unverified_exact_geo_coordinates(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn('name="geo.position"', body)
        self.assertNotIn('name="ICBM"', body)
        self.assertIn('name="geo.region"', body)
        self.assertIn('name="geo.placename"', body)

    def test_legacy_product_seo_block_uses_checkout_owned_shipping_threshold(self):
        from storefront.services.product_seo_block import build_product_seo_block

        product = type("ProductStub", (), {
            "title": "Registry test product",
            "slug": "registry-test-product",
            "category": type("CategoryStub", (), {"name": "Футболки"})(),
            "material": "",
        })()
        block = build_product_seo_block(product, language_code="uk")
        delivery = next(section for section in block["sections"] if section["id"] == "delivery")
        text = " ".join(delivery["paragraphs"])
        faq_text = " ".join(item["answer"] for item in block["faq"])
        self.assertNotIn("2500", text + faq_text)
