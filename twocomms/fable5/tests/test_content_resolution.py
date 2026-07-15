from django.test import TestCase

from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product

from fable5.models import (
    ProductOptionProfile,
    ProductOptionProfileI18n,
    VariantCombinationProfile,
    VariantCombinationProfileI18n,
    VariantDetails,
    VariantDetailsI18n,
)


class ContentResolutionTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Футболки resolver", slug="tshirts-resolver")
        self.product = Product.objects.create(
            title="CRC base",
            slug="crc-content-resolution",
            category=category,
            price=1200,
            short_description="Base short",
            full_description="Base full",
            seo_title="Base SEO",
            seo_description="Base SEO description",
        )
        self.product.title_uk = "CRC база УК"
        self.product.title_ru = "CRC база RU"
        self.product.title_en = "CRC base EN"
        self.product.seo_title_uk = "CRC SEO УК"
        self.product.seo_title_ru = "CRC SEO RU"
        self.product.seo_title_en = "CRC SEO EN"
        self.product.save()
        color = Color.objects.create(name="Чорний", primary_hex="#101010")
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            slug="black",
        )
        self.details = VariantDetails.objects.create(
            variant=self.variant,
            display_name="CRC чорна legacy",
            seo_title="CRC black legacy SEO",
        )

    def test_option_values_are_normalized_and_key_is_stable(self):
        from fable5.content_resolution import build_combination_key, normalize_option_values

        normalized = normalize_option_values({" Lining ": " Fleece ", "FIT": " OverSize "})

        self.assertEqual(normalized, {"fit": "oversize", "lining": "fleece"})
        self.assertEqual(build_combination_key(normalized), "fit=oversize;lining=fleece")

    def test_invalid_option_tokens_are_rejected(self):
        from fable5.content_resolution import normalize_option_values

        for payload in ({"fit=x": "classic"}, {"fit": "over;size"}, ["classic"]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_option_values(payload)

    def test_exact_combination_wins_in_ukrainian(self):
        from fable5.content_resolution import resolve_merchandising_context

        combination = VariantCombinationProfile.objects.create(
            variant=self.variant,
            combination_key="fit=oversize",
            option_values={"fit": "oversize"},
        )
        VariantCombinationProfileI18n.objects.create(
            profile=combination,
            lang="uk",
            display_name="CRC чорна оверсайз",
        )

        context = resolve_merchandising_context(
            self.product,
            variant=self.variant,
            option_values={"fit": "oversize"},
            lang="uk",
        )

        self.assertEqual(context["display_name"], "CRC чорна оверсайз")
        self.assertEqual(context["sources"]["display_name"], "combination:uk")

    def test_blank_exact_russian_falls_to_russian_fit_profile(self):
        from fable5.content_resolution import resolve_merchandising_context

        option = ProductOptionProfile.objects.create(
            product=self.product,
            option_key="fit=oversize",
            option_values={"fit": "oversize"},
        )
        ProductOptionProfileI18n.objects.create(
            profile=option,
            lang="ru",
            display_name="CRC оверсайз RU",
        )
        combination = VariantCombinationProfile.objects.create(
            variant=self.variant,
            combination_key="fit=oversize",
            option_values={"fit": "oversize"},
        )
        VariantCombinationProfileI18n.objects.create(
            profile=combination,
            lang="ru",
            display_name="",
        )
        VariantCombinationProfileI18n.objects.create(
            profile=combination,
            lang="uk",
            display_name="CRC чорна оверсайз УК",
        )

        context = resolve_merchandising_context(
            self.product,
            variant=self.variant,
            option_values={"fit": "oversize"},
            lang="ru",
        )

        self.assertEqual(context["display_name"], "CRC оверсайз RU")
        self.assertEqual(context["sources"]["display_name"], "option:fit=oversize:ru")

    def test_requested_product_language_wins_before_ukrainian_override(self):
        from fable5.content_resolution import resolve_merchandising_context

        VariantDetailsI18n.objects.create(
            details=self.details,
            lang="uk",
            display_name="CRC чорна УК",
        )

        context = resolve_merchandising_context(
            self.product,
            variant=self.variant,
            option_values={"fit": "classic"},
            lang="en",
        )

        self.assertEqual(context["display_name"], "CRC base EN")
        self.assertEqual(context["sources"]["display_name"], "product:en")

    def test_color_requested_language_precedes_product_language(self):
        from fable5.content_resolution import resolve_variant_text

        VariantDetailsI18n.objects.create(
            details=self.details,
            lang="ru",
            seo_title="CRC черная SEO RU",
        )

        value = resolve_variant_text(self.variant, "seo_title", "ru")

        self.assertEqual(value, "CRC черная SEO RU")

    def test_legacy_color_fields_remain_ukrainian_fallback(self):
        from fable5.content_resolution import resolve_variant_text

        self.assertEqual(
            resolve_variant_text(self.variant, "display_name", "uk"),
            "CRC чорна legacy",
        )

    def test_product_without_fable_rows_keeps_product_values(self):
        from fable5.content_resolution import resolve_merchandising_context

        other = Product.objects.create(
            title="Plain product",
            slug="plain-product-resolution",
            category=self.product.category,
            price=500,
            seo_title="Plain SEO",
        )

        context = resolve_merchandising_context(other, lang="uk")

        self.assertEqual(context["display_name"], "Plain product")
        self.assertEqual(context["seo_title"], "Plain SEO")
        self.assertEqual(context["sources"]["display_name"], "product:canonical")

    def test_exact_profile_without_price_override_inherits_fit_delta(self):
        from fable5.content_resolution import resolve_merchandising_context

        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=300,
        )
        VariantCombinationProfile.objects.create(
            variant=self.variant,
            combination_key="fit=oversize",
            option_values={"fit": "oversize"},
        )

        context = resolve_merchandising_context(
            self.product,
            variant=self.variant,
            option_values={"fit": "oversize"},
            lang="uk",
        )

        self.assertEqual(context["price_delta"], 300)
        self.assertEqual(context["sources"]["price_delta"], "option:fit=oversize")
