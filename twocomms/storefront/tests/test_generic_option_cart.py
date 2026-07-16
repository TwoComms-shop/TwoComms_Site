import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache, caches
from django.test import TestCase
from django.urls import reverse

from fable5.models import (
    GarmentFlow,
    GarmentFlowCategory,
    ProductOptionProfile,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product


class GenericOptionCartTests(TestCase):
    def setUp(self):
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
            name="Cart hoodies",
            slug="cart-hoodies",
            is_active=True,
        )
        flow = GarmentFlow.objects.create(
            code="cart-hoodie",
            name="Hoodie",
            axes=[{
                "code": "lining",
                "label": "Утеплення",
                "options": [
                    {"code": "fleece", "label": "Фліс"},
                    {
                        "code": "no_fleece",
                        "label": "Без флісу",
                        "disabled": True,
                    },
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=flow, category=self.category)
        self.product = Product.objects.create(
            title="Cart hoodie",
            slug="cart-hoodie-options",
            category=self.category,
            price=1000,
            status="published",
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Black", primary_hex="#111111"),
            slug="black",
            is_default=True,
        )
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="lining=fleece",
            option_values={"lining": "fleece"},
            price_delta=250,
            price_delta_reason="Флісова основа",
        )
        self.url = reverse("cart_add")

    def _post(self, option_values):
        return self.client.post(
            self.url,
            {
                "product_id": self.product.id,
                "size": "M",
                "qty": 1,
                "color_variant_id": self.variant.id,
                "option_values": json.dumps(option_values),
            },
        )

    def test_add_to_cart_rejects_disabled_option(self):
        response = self._post({"lining": "no_fleece"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_add_to_cart_stores_normalized_options_and_resolved_price(self):
        response = self._post({" LINING ": " FLEECE "})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        item = next(iter(self.client.session["cart"].values()))
        self.assertEqual(item["option_values"], {"lining": "fleece"})
        self.assertEqual(item["option_labels"], {"Утеплення": "Фліс"})
        self.assertEqual(response.json()["item"]["item_price"], 1250.0)
        self.assertEqual(response.json()["cart_total"], 1250.0)

    def test_different_generic_options_create_distinct_cart_keys(self):
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="lining=no_fleece",
            option_values={"lining": "no_fleece"},
            price_delta=0,
            is_active=True,
        )
        self.flow = GarmentFlow.objects.get(code="cart-hoodie")
        axes = self.flow.axes
        axes[0]["options"][1].pop("disabled")
        self.flow.axes = axes
        self.flow.save(update_fields=["axes"])

        self.assertEqual(self._post({"lining": "fleece"}).status_code, 200)
        self.assertEqual(self._post({"lining": "no_fleece"}).status_code, 200)

        self.assertEqual(len(self.client.session["cart"]), 2)

    def test_order_item_models_expose_generic_option_snapshots(self):
        from orders.models import DropshipperOrderItem, OrderItem

        self.assertEqual(OrderItem._meta.get_field("option_values").default(), {})
        self.assertEqual(OrderItem._meta.get_field("option_labels").default(), {})
        self.assertEqual(
            DropshipperOrderItem._meta.get_field("option_values").default(), {}
        )

    def test_order_item_formats_generic_options_without_duplicate_fit(self):
        from orders.models import OrderItem

        item = OrderItem(
            option_values={"fit": "oversize", "lining": "fleece"},
            option_labels={"Посадка": "Оверсайз", "Утеплення": "Фліс"},
            fit_option_code="oversize",
            fit_option_label="Оверсайз",
        )

        self.assertEqual(item.generic_option_labels, ["Утеплення: Фліс"])

    def test_authoritative_price_accepts_generic_options(self):
        from fable5.services import effective_cart_unit_price

        self.assertEqual(
            effective_cart_unit_price(
                self.product,
                self.variant,
                option_values={"lining": "fleece"},
            ),
            Decimal("1250"),
        )

    def test_main_javascript_posts_checked_generic_option_values(self):
        javascript = (
            Path(__file__).resolve().parents[2]
            / "twocomms_django_theme"
            / "static"
            / "js"
            / "main.js"
        ).read_text(encoding="utf-8")

        self.assertIn("[data-product-option-axis]:checked", javascript)
        self.assertIn("body.append('option_values', JSON.stringify(optionValues))", javascript)
