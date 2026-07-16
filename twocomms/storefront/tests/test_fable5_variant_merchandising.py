"""Public contracts for Fable 5 color merchandising metadata."""

import html
import json
import re
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache, caches
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from fable5.models import (
    ColorProfile,
    ProductOptionProfile,
    VariantCombinationProfile,
    VariantCombinationProfileI18n,
    VariantDetails,
    VariantFitRule,
    VariantOptionSizeGrid,
    VariantSizeRule,
)
from fable5.services import effective_cart_unit_price, variant_public_context
from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid
from storefront.services.catalog_helpers import (
    build_color_preview_map,
    get_detailed_color_variants,
)


class Fable5VariantMerchandisingTests(TestCase):
    LEGACY_UK_MARKER = "ЛЕГАСІ УК ВАРІАНТ"

    def setUp(self):
        super().setUp()
        cache.clear()
        caches["fragments"].clear()
        for target in (
            "storefront.signals.generate_google_merchant_feed_task.apply_async",
            "storefront.signals.enqueue_indexnow_urls",
        ):
            patcher = patch(target)
            self.addCleanup(patcher.stop)
            patcher.start()

        self.category = Category.objects.create(
            name="Футболки",
            slug="tshirts-fable5",
            is_active=True,
        )
        self.product = Product.objects.create(
            title="Футболка Бойова квіточка",
            slug="fable5-thermo-shirt",
            category=self.category,
            price=1090,
            status="published",
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Класична",
            is_active=True,
            is_default=True,
            order=0,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="oversize",
            label="Оверсайз",
            is_active=True,
            is_default=False,
            order=1,
        )
        self.thermo_color = Color.objects.create(
            name="Термо-зелена",
            primary_hex="#A2AB92",
        )
        self.thermo_variant = ProductColorVariant.objects.create(
            product=self.product,
            color=self.thermo_color,
            is_default=True,
            order=0,
            price_override=1450,
        )
        ColorProfile.objects.create(color=self.thermo_color, is_thermo=True)
        VariantDetails.objects.create(
            variant=self.thermo_variant,
            price_delta=0,
            price_delta_reason="",
        )
        VariantFitRule.objects.create(
            variant=self.thermo_variant,
            fit_code="classic",
            is_enabled=False,
            reason="",
        )
        VariantFitRule.objects.create(
            variant=self.thermo_variant,
            fit_code="oversize",
            is_enabled=True,
            reason="",
        )

    def _create_localized_no_fit_hoodie(self):
        product = Product.objects.create(
            title="Худі «UK fallback»",
            slug="fable5-localized-no-fit-hoodie",
            category=self.category,
            price=1690,
            status="published",
        )
        localized_values = {
            "uk": {
                "title": "Худі «UK fallback»",
                "seo_title": "UK fallback SEO",
                "seo_description": "UK fallback SEO description",
                "full_description": "UK fallback marketing",
            },
            "ru": {
                "title": "Худи «RU fallback»",
                "seo_title": "RU fallback SEO",
                "seo_description": "RU fallback SEO description",
                "full_description": "RU fallback marketing",
            },
            "en": {
                "title": 'Hoodie "EN fallback"',
                "seo_title": "EN fallback SEO",
                "seo_description": "EN fallback SEO description",
                "full_description": "EN fallback marketing",
            },
        }
        for language, values in localized_values.items():
            for field, value in values.items():
                setattr(product, f"{field}_{language}", value)
        product.save()

        color = Color.objects.create(name="Чорний", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(
            product=product,
            color=color,
            slug="legacy-uk-color",
            is_default=True,
            order=0,
        )
        VariantDetails.objects.create(
            variant=variant,
            display_name=f"{self.LEGACY_UK_MARKER} display",
            marketing_html=f"{self.LEGACY_UK_MARKER} marketing",
            seo_title=f"{self.LEGACY_UK_MARKER} SEO title",
            seo_description=f"{self.LEGACY_UK_MARKER} SEO description",
        )
        return product, localized_values

    def _restore_active_language_after_test(self):
        previous_language = translation.get_language()
        if previous_language is None:
            self.addCleanup(translation.deactivate)
        else:
            self.addCleanup(translation.activate, previous_language)

    def test_detailed_variants_memo_is_isolated_by_language(self):
        product, localized_values = self._create_localized_no_fit_hoodie()

        russian = get_detailed_color_variants(product, lang="ru")[0]
        english = get_detailed_color_variants(product, lang="en")[0]

        for language, payload in (("ru", russian), ("en", english)):
            expected = localized_values[language]
            with self.subTest(language=language):
                self.assertEqual(payload["display_name"], expected["title"])
                self.assertEqual(payload["seo_title"], expected["seo_title"])
                self.assertEqual(
                    payload["seo_description"],
                    expected["seo_description"],
                )
                self.assertEqual(
                    payload["marketing_html"],
                    expected["full_description"],
                )
                self.assertNotIn(self.LEGACY_UK_MARKER, str(payload))

    def test_localized_pdp_and_variants_api_do_not_leak_legacy_uk_content(self):
        self._restore_active_language_after_test()
        product, localized_values = self._create_localized_no_fit_hoodie()

        for language in ("ru", "en"):
            expected = localized_values[language]
            with self.subTest(language=language):
                response = self.client.get(
                    f"/{language}/product/{product.slug}/"
                )

                self.assertEqual(response.status_code, 200)
                h1_match = re.search(
                    r"<h1[^>]*data-pdp-product-title[^>]*>(.*?)</h1>",
                    response.content.decode(),
                    flags=re.DOTALL,
                )
                self.assertIsNotNone(h1_match)
                self.assertEqual(
                    html.unescape(h1_match.group(1).strip()),
                    expected["title"],
                )
                self.assertEqual(
                    response.context["selected_variant_merchandising"]["display_name"],
                    expected["title"],
                )
                self.assertNotContains(response, self.LEGACY_UK_MARKER)

                with translation.override(language):
                    variants_url = reverse(
                        "get_product_variants",
                        args=[product.pk],
                    )
                api_response = self.client.get(variants_url)
                self.assertEqual(api_response.status_code, 200)
                payload = api_response.json()["variants"][0]
                self.assertEqual(payload["display_name"], expected["title"])
                self.assertNotIn(self.LEGACY_UK_MARKER, str(payload))

    def test_localized_color_pdp_json_ld_uses_product_fallbacks(self):
        self._restore_active_language_after_test()
        product, localized_values = self._create_localized_no_fit_hoodie()

        for language in ("ru", "en"):
            expected = localized_values[language]
            variant_path = (
                f"/{language}/product/{product.slug}/legacy-uk-color/"
            )
            with self.subTest(language=language):
                response = self.client.get(variant_path)

                self.assertEqual(response.status_code, 200)
                json_ld_payloads = [
                    json.loads(payload)
                    for payload in re.findall(
                        r'<script type="application/ld\+json">(.*?)</script>',
                        response.content.decode(),
                        flags=re.DOTALL,
                    )
                ]
                graph_nodes = [
                    node
                    for payload in json_ld_payloads
                    for node in payload.get("@graph", [payload])
                ]
                variant_node = next(
                    node
                    for node in graph_nodes
                    if node.get("@type") == "Product"
                    and node.get("url", "").endswith(variant_path)
                )
                self.assertEqual(variant_node["name"], expected["title"])
                self.assertEqual(
                    variant_node["description"],
                    expected["seo_description"],
                )
                self.assertNotIn(
                    self.LEGACY_UK_MARKER,
                    json.dumps(variant_node, ensure_ascii=False),
                )

    def test_public_context_uses_variant_price_and_safe_thermo_defaults(self):
        context = variant_public_context(self.thermo_variant)

        self.assertEqual(context["final_price"], Decimal("1450"))
        self.assertEqual(context["price_delta_reason"], "Термохромна тканина")
        self.assertEqual(
            context["thermo_note"],
            "Реагує на тепло — змінює відтінок",
        )
        self.assertEqual(
            context["fit_rules"]["classic"]["reason"],
            "Для цього кольору доступний лише оверсайз",
        )

    def test_authoritative_price_resolves_selected_fit_override(self):
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=200,
            price_delta_reason="Окремий крій oversize",
        )

        self.assertEqual(
            effective_cart_unit_price(
                self.product,
                self.thermo_variant,
                fit_code="oversize",
            ),
            Decimal("1650"),
        )

    def test_color_preview_map_exposes_thermo_price_and_fit_state(self):
        preview = build_color_preview_map([self.product])[self.product.pk][0]

        self.assertTrue(preview["is_thermo"])
        self.assertEqual(preview["final_price"], Decimal("1450"))
        self.assertEqual(preview["price_reason"], "Термохромна тканина")
        self.assertFalse(preview["fit_rules"]["classic"]["is_enabled"])
        self.assertTrue(preview["fit_rules"]["oversize"]["is_enabled"])

    def test_color_preview_uses_lowest_purchasable_color_fit_price(self):
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=200,
            price_delta_reason="Окремий крій oversize",
        )

        preview = build_color_preview_map([self.product])[self.product.pk][0]

        self.assertEqual(preview["final_price"], Decimal("1650"))
        self.assertEqual(self.product.card_price_min, Decimal("1650"))

    def test_home_card_shows_lowest_price_and_per_color_price_metadata(self):
        white = Color.objects.create(name="Біла", primary_hex="#FFFFFF")
        ProductColorVariant.objects.create(
            product=self.product,
            color=white,
            is_default=False,
            order=1,
        )

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        rendered = next(p for p in response.context["products"] if p.pk == self.product.pk)
        self.assertEqual(rendered.card_price_min, Decimal("1090"))
        self.assertEqual(rendered.card_price_max, Decimal("1450"))
        self.assertContains(response, 'data-variant-price="1450"', html=False)
        self.assertContains(response, 'data-variant-price="1090"', html=False)
        self.assertContains(response, "Від 1090 грн")
        self.assertContains(response, 'data-thermo-flame', html=False)

    def test_pdp_uses_selected_variant_price_flame_and_effective_fit(self):
        response = self.client.get(
            reverse("product", kwargs={"slug": self.product.slug})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_variant_price"], Decimal("1450"))
        self.assertContains(response, 'data-thermo-flame', html=False)
        self.assertContains(response, "1450 грн")
        self.assertContains(response, "Оверсайз")
        self.assertNotContains(response, 'value="classic"', html=False)
        self.assertContains(response, "Термохромна тканина")

    def test_cart_uses_variant_price_and_rejects_disabled_fit(self):
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=200,
            price_delta_reason="Окремий крій oversize",
        )
        rejected = self.client.post(
            reverse("cart_add"),
            {
                "product_id": self.product.pk,
                "color_variant_id": self.thermo_variant.pk,
                "size": "M",
                "fit_option": "classic",
                "qty": 1,
            },
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(rejected.json()["ok"])
        self.assertEqual(self.client.session.get("cart", {}), {})

        accepted = self.client.post(
            reverse("cart_add"),
            {
                "product_id": self.product.pk,
                "color_variant_id": self.thermo_variant.pk,
                "size": "M",
                "fit_option": "oversize",
                "qty": 1,
            },
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["item"]["item_price"], 1650.0)
        self.assertEqual(accepted.json()["cart_total"], 1650.0)

    def test_pdp_payload_contains_merchandising_for_each_allowed_fit(self):
        profile = VariantCombinationProfile.objects.create(
            variant=self.thermo_variant,
            combination_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=250,
            price_delta_reason="Посилений oversize крій",
        )
        VariantCombinationProfileI18n.objects.create(
            profile=profile,
            lang="uk",
            display_name="Термозелена футболка oversize",
            seo_title="Термозелена oversize футболка | TwoComms",
            seo_description="Окрема SEO-сторінка кольору та посадки oversize.",
        )

        response = self.client.get(reverse("product", kwargs={"slug": self.product.slug}))

        payload = response.context["color_variants"][0]["merchandising_by_fit"]
        self.assertNotIn("classic", payload)
        self.assertEqual(payload["oversize"]["final_price"], Decimal("1700"))
        self.assertEqual(
            payload["oversize"]["seo_title"],
            "Термозелена oversize футболка | TwoComms",
        )
        self.assertEqual(response.context["selected_variant_price"], Decimal("1700"))

    def test_cart_rejects_disabled_color_fit_size(self):
        VariantSizeRule.objects.create(
            variant=self.thermo_variant,
            fit_code="oversize",
            size="M",
            is_enabled=False,
        )

        response = self.client.post(
            reverse("cart_add"),
            {
                "product_id": self.product.pk,
                "color_variant_id": self.thermo_variant.pk,
                "size": "M",
                "fit_option": "oversize",
                "qty": 1,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_color_path_uses_per_color_seo_with_product_fallbacks(self):
        details = VariantDetails.objects.get(variant=self.thermo_variant)
        details.display_name = "Термозелена Бойова квіточка"
        details.seo_title = "Термозелена футболка Бойова квіточка | TwoComms"
        details.seo_description = "Термохромна тканина, що змінює відтінок від тепла."
        details.save()

        response = self.client.get(
            reverse(
                "product",
                kwargs={
                    "slug": self.product.slug,
                    "v1": self.thermo_variant.slug,
                },
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["variant_page_title"],
            details.seo_title,
        )
        self.assertEqual(
            response.context["variant_page_description"],
            details.seo_description,
        )
        self.assertContains(response, "Термозелена Бойова квіточка")
        self.assertContains(response, '"price": "1450"', html=False)
        self.assertContains(response, '"name": "Термозелена Бойова квіточка"', html=False)

    def test_color_fit_path_uses_exact_combination_seo_and_price(self):
        profile = VariantCombinationProfile.objects.create(
            variant=self.thermo_variant,
            combination_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=250,
            price_delta_reason="Посилений oversize крій",
        )
        content = VariantCombinationProfileI18n.objects.create(
            profile=profile,
            lang="uk",
            display_name="Термозелена футболка oversize",
            seo_title="Термозелена oversize футболка | TwoComms",
            seo_description="Окрема SEO-сторінка кольору та посадки oversize.",
        )

        response = self.client.get(reverse(
            "product",
            kwargs={
                "slug": self.product.slug,
                "v1": self.thermo_variant.slug,
                "v2": "oversize",
            },
        ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_variant_price"], Decimal("1700"))
        self.assertEqual(response.context["variant_page_title"], content.seo_title)
        self.assertContains(response, "Термозелена футболка oversize")
        self.assertContains(response, '"price": "1700"', html=False)

    def test_color_grid_only_size_path_is_not_replaced_by_legacy_default(self):
        catalog = Catalog.objects.create(name="Thermo catalog", slug="thermo-catalog")
        self.product.catalog = catalog
        self.product.save(update_fields=["catalog"])
        grid = SizeGrid.objects.create(
            catalog=catalog,
            name="Thermo 3XL",
            is_active=True,
            guide_data={
                "columns": [{"key": "size", "label": "Розмір"}],
                "rows": [{"size": "3XL"}],
            },
        )
        VariantOptionSizeGrid.objects.create(
            variant=self.thermo_variant,
            option_key="fit=oversize",
            size_grid=grid,
        )

        response = self.client.get(reverse(
            "product",
            kwargs={
                "slug": self.product.slug,
                "v1": self.thermo_variant.slug,
                "v2": "3xl",
                "v3": "oversize",
            },
        ))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["preselected_size"], "3XL")

    def test_impossible_color_fit_path_returns_404(self):
        response = self.client.get(
            reverse(
                "product",
                kwargs={
                    "slug": self.product.slug,
                    "v1": self.thermo_variant.slug,
                    "v2": "classic",
                },
            )
        )

        self.assertEqual(response.status_code, 404)
