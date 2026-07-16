import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, Category, Product, ProductFitOption, SizeGrid
from warehouse.models import StorageCategory, StorageSubcategory

from fable5.models import (
    ProductOptionSizeGrid,
    VariantBlankLink,
    VariantCombinationProfile,
    VariantCombinationProfileI18n,
    VariantOptionSizeGrid,
    VariantSizeRule,
)


class VariantResourceResolutionTests(TestCase):
    def setUp(self):
        self.catalog = Catalog.objects.create(name="T-shirts", slug="resource-tshirts")
        self.category = Category.objects.create(name="T-shirts", slug="resource-category")
        self.product = Product.objects.create(
            title="Resource product",
            slug="resource-product",
            category=self.category,
            catalog=self.catalog,
            price=1000,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="oversize",
            label="Оверсайз",
            is_active=True,
            is_default=True,
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Thermo green", primary_hex="#82956f"),
        )
        self.shared_grid = self._grid("Shared")
        self.color_grid = self._grid("Thermo")
        ProductOptionSizeGrid.objects.create(
            product=self.product,
            option_key="fit=oversize",
            size_grid=self.shared_grid,
        )

    def _grid(self, name):
        return SizeGrid.objects.create(
            catalog=self.catalog,
            name=name,
            is_active=True,
            guide_data={
                "columns": [{"key": "size", "label": "Розмір"}],
                "rows": [{"size": "M"}, {"size": "L"}],
            },
        )

    def test_color_grid_overrides_shared_grid_for_one_fit(self):
        from fable5.size_grid_services import resolve_option_size_grid

        VariantOptionSizeGrid.objects.create(
            variant=self.variant,
            option_key="fit=oversize",
            size_grid=self.color_grid,
        )

        self.assertEqual(
            resolve_option_size_grid(self.product, "fit=oversize"),
            self.shared_grid,
        )
        self.assertEqual(
            resolve_option_size_grid(
                self.product,
                "fit=oversize",
                variant=self.variant,
            ),
            self.color_grid,
        )

    def test_variant_blank_link_is_fit_specific(self):
        storage_category = StorageCategory.objects.create(name="T-shirts")
        blank = StorageSubcategory.objects.create(
            category=storage_category,
            name="CRC thermo green",
        )
        link = VariantBlankLink.objects.create(
            variant=self.variant,
            option_key="fit=oversize",
            storage_subcategory=blank,
        )

        self.assertEqual(link.storage_subcategory, blank)
        self.assertEqual(link.option_key, "fit=oversize")

    def test_comparison_includes_grid_assigned_only_to_one_color(self):
        from fable5.size_grid_services import build_size_grid_comparison

        ProductOptionSizeGrid.objects.filter(product=self.product).delete()
        VariantOptionSizeGrid.objects.create(
            variant=self.variant,
            option_key="fit=oversize",
            size_grid=self.color_grid,
        )

        comparison = build_size_grid_comparison(
            self.product,
            variants=[self.variant],
        )

        self.assertEqual(len(comparison), 1)
        self.assertEqual(comparison[0]["option_key"], "fit=oversize")
        self.assertEqual(comparison[0]["variants"][0]["grid_id"], self.color_grid.pk)


class VariantResourceEditorApiTests(VariantResourceResolutionTests):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="variant-resources",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_variant_save_persists_color_grid_and_storage_blank(self):
        storage_category = StorageCategory.objects.create(name="T-shirts blanks")
        blank = StorageSubcategory.objects.create(
            category=storage_category,
            name="CRC thermo green",
        )

        response = self.client.post(
            reverse("fable5_api_variant_save"),
            data=json.dumps({
                "product_id": self.product.pk,
                "id": self.variant.pk,
                "color": {"id": self.variant.color_id},
                "size_grids": [{
                    "option_key": "fit=oversize",
                    "size_grid_id": self.color_grid.pk,
                }],
                "blank_links": [{
                    "option_key": "fit=oversize",
                    "storage_subcategory_id": blank.pk,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            VariantOptionSizeGrid.objects.get(variant=self.variant).size_grid,
            self.color_grid,
        )
        self.assertEqual(
            VariantBlankLink.objects.get(variant=self.variant).storage_subcategory,
            blank,
        )
        self.assertEqual(response.json()["variant"]["size_grids"][0]["size_grid_id"], self.color_grid.pk)
        self.assertEqual(response.json()["variant"]["blank_links"][0]["storage_subcategory_id"], blank.pk)

    def test_variant_save_persists_sparse_color_fit_seo_and_price(self):
        response = self.client.post(
            reverse("fable5_api_variant_save"),
            data=json.dumps({
                "product_id": self.product.pk,
                "id": self.variant.pk,
                "color": {"id": self.variant.color_id},
                "combinations": [{
                    "option_values": {"fit": "oversize"},
                    "is_active": True,
                    "price_delta": 250,
                    "price_delta_reason": "Окремий крій",
                    "content": {
                        "display_name": "Thermo oversize",
                        "seo_title": "Thermo oversize SEO",
                        "seo_description": "Окремий опис посадки та кольору",
                    },
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        profile = VariantCombinationProfile.objects.get(variant=self.variant)
        self.assertEqual(profile.combination_key, "fit=oversize")
        self.assertEqual(profile.price_delta, 250)
        self.assertEqual(profile.i18n.get(lang="uk").seo_title, "Thermo oversize SEO")
        payload = response.json()["variant"]["combinations"][0]
        self.assertEqual(payload["content"]["display_name"], "Thermo oversize")

    def test_variant_save_without_sizes_preserves_existing_inventory_rules(self):
        VariantSizeRule.objects.bulk_create([
            VariantSizeRule(
                variant=self.variant,
                fit_code="",
                size="M",
                is_enabled=True,
                stock=7,
                note="shared stock",
            ),
            VariantSizeRule(
                variant=self.variant,
                fit_code="",
                size="XXL",
                is_enabled=False,
                stock=0,
                note="disabled",
            ),
        ])
        before = list(
            VariantSizeRule.objects.filter(variant=self.variant)
            .order_by("fit_code", "size")
            .values("fit_code", "size", "is_enabled", "stock", "note")
        )

        response = self.client.post(
            reverse("fable5_api_variant_save"),
            data=json.dumps({
                "product_id": self.product.pk,
                "id": self.variant.pk,
                "color": {"id": self.variant.color_id},
                "price_override": 1350,
                "details": {
                    "display_name": "Thermo green updated",
                    "seo_title": "Thermo green SEO",
                },
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        after = list(
            VariantSizeRule.objects.filter(variant=self.variant)
            .order_by("fit_code", "size")
            .values("fit_code", "size", "is_enabled", "stock", "note")
        )
        self.assertEqual(after, before)

    def test_stock_only_variant_save_preserves_default_ownership(self):
        self.variant.is_default = True
        self.variant.save(update_fields=["is_default"])
        other = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Black", primary_hex="#111111"),
            is_default=False,
        )

        response = self.client.post(
            reverse("fable5_api_variant_save"),
            data=json.dumps({
                "product_id": self.product.pk,
                "id": self.variant.pk,
                "color": {"id": self.variant.color_id},
                "sizes": [{
                    "fit_code": "",
                    "size": "M",
                    "is_enabled": True,
                    "stock": 4,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.variant.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(self.variant.is_default)
        self.assertFalse(other.is_default)

    def test_variant_save_preserves_unsubmitted_combination_translations(self):
        profile = VariantCombinationProfile.objects.create(
            variant=self.variant,
            combination_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=100,
        )
        VariantCombinationProfileI18n.objects.create(
            profile=profile,
            lang="ru",
            display_name="Термо оверсайз",
            seo_title="Русский SEO title",
        )

        response = self.client.post(
            reverse("fable5_api_variant_save"),
            data=json.dumps({
                "product_id": self.product.pk,
                "id": self.variant.pk,
                "color": {"id": self.variant.color_id},
                "combinations": [{
                    "option_values": {"fit": "oversize"},
                    "is_active": True,
                    "price_delta": 250,
                    "content": {
                        "display_name": "Thermo oversize",
                        "seo_title": "Український SEO title",
                    },
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual(profile.price_delta, 250)
        self.assertEqual(profile.i18n.get(lang="ru").seo_title, "Русский SEO title")
        self.assertEqual(
            response.json()["variant"]["combinations"][0]["content_by_lang"]["ru"]["seo_title"],
            "Русский SEO title",
        )

    def test_cart_rejects_size_outside_color_override_grid(self):
        VariantOptionSizeGrid.objects.create(
            variant=self.variant,
            option_key="fit=oversize",
            size_grid=self.color_grid,
        )

        response = self.client.post(
            reverse("cart_add"),
            {
                "product_id": self.product.pk,
                "color_variant_id": self.variant.pk,
                "fit_option": "oversize",
                "size": "S",
                "qty": 1,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
