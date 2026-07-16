import importlib
import json
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from fable5.models import (
    GarmentFlow,
    GarmentFlowCategory,
    ProductOptionProfile,
)
from storefront.models import Category, Product
from warehouse.models import Print


class GenericOptionEditorTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Editor hoodies", slug="editor-hoodies")
        self.flow = GarmentFlow.objects.create(
            code="editor-hoodie",
            name="Hoodie",
            axes=[{
                "code": "lining",
                "label": "Утеплення",
                "options": [
                    {"code": "fleece", "label": "Фліс"},
                    {"code": "no_fleece", "label": "Без флісу", "disabled": True},
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=self.flow, category=self.category)
        self.product = Product.objects.create(
            title="Editor hoodie",
            slug="editor-hoodie",
            category=self.category,
            price=1400,
        )
        self.other = Product.objects.create(
            title="Other hoodie",
            slug="other-editor-hoodie",
            category=self.category,
            price=1400,
        )
        self.print_a = Print.objects.create(name="Print A")
        self.print_b = Print.objects.create(name="Print B")
        self.print_a.default_products.add(self.product, self.other)
        self.print_b.default_products.add(self.product)
        self.staff = get_user_model().objects.create_user(
            username="generic-option-editor",
            password="password",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_bootstrap_exposes_flow_axes_and_selected_prints(self):
        response = self.client.get(reverse("fable5_product_edit", args=[self.product.pk]))

        self.assertEqual(response.status_code, 200)
        bootstrap = response.context["bootstrap"]
        product = bootstrap["product"]
        self.assertEqual(product["option_axes"][0]["code"], "lining")
        self.assertCountEqual(product["print_ids"], [self.print_a.id, self.print_b.id])
        self.assertCountEqual(
            [row["id"] for row in bootstrap["dictionaries"]["prints"]],
            [self.print_a.id, self.print_b.id],
        )

    def test_print_picker_uses_linked_product_image_as_preview_fallback(self):
        self.product.main_image = SimpleUploadedFile(
            "linked-product.gif",
            b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
            content_type="image/gif",
        )
        self.product.save(update_fields=["main_image"])

        response = self.client.get(reverse("fable5_product_edit", args=[self.product.pk]))

        row = next(
            item
            for item in response.context["bootstrap"]["dictionaries"]["prints"]
            if item["id"] == self.print_a.id
        )
        self.assertIn("linked-product", row["image_url"])
        self.assertEqual(row["image_source"], "product")

    def test_product_save_updates_option_profiles_and_print_m2m(self):
        response = self.client.post(
            reverse("fable5_api_product_save"),
            data={
                "payload": json.dumps({
                    "id": self.product.id,
                    "title": self.product.title,
                    "category_id": self.category.id,
                    "price": self.product.price,
                    "print_ids": [self.print_b.id],
                    "option_profiles": [
                        {
                            "option_values": {"lining": "fleece"},
                            "is_active": True,
                            "price_delta": 180,
                            "price_delta_reason": "Флісова основа",
                        },
                        {
                            "option_values": {"lining": "no_fleece"},
                            "is_active": False,
                            "price_delta": 0,
                            "price_delta_reason": "Тимчасово недоступно",
                        },
                    ],
                })
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(self.product.warehouse_default_prints.values_list("id", flat=True)),
            [self.print_b.id],
        )
        self.assertTrue(self.print_a.default_products.filter(id=self.other.id).exists())
        fleece = ProductOptionProfile.objects.get(
            product=self.product,
            option_key="lining=fleece",
        )
        no_fleece = ProductOptionProfile.objects.get(
            product=self.product,
            option_key="lining=no_fleece",
        )
        self.assertEqual(fleece.price_delta, 180)
        self.assertTrue(fleece.is_active)
        self.assertFalse(no_fleece.is_active)

    def test_hoodie_seed_enables_only_fleece(self):
        self.flow.code = "hoodie"
        self.flow.save(update_fields=["code"])
        migration = importlib.import_module("fable5.migrations.0006_seed_hoodie_lining_profiles")

        migration.seed_hoodie_lining_profiles(apps, None)

        profiles = {
            row.option_key: row
            for row in ProductOptionProfile.objects.filter(product=self.product)
        }
        self.assertTrue(profiles["lining=fleece"].is_active)
        self.assertFalse(profiles["lining=no_fleece"].is_active)

    def test_editor_renders_generic_option_and_print_workspaces(self):
        response = self.client.get(reverse("fable5_product_edit", args=[self.product.pk]))
        html = response.content.decode()

        self.assertIn('id="f-option-profiles"', html)
        self.assertIn('id="f-product-prints"', html)
        self.assertIn('id="f-print-search"', html)

    def test_editor_javascript_collects_options_and_multiple_print_ids(self):
        javascript = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "fable5"
            / "editor.js"
        ).read_text(encoding="utf-8")

        self.assertIn("function renderOptionProfiles()", javascript)
        self.assertIn("function collectOptionProfiles()", javascript)
        self.assertIn("function renderProductPrints()", javascript)
        self.assertIn("option_profiles: collectOptionProfiles()", javascript)
        self.assertIn("print_ids: collectPrintIds()", javascript)
