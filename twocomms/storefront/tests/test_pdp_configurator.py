from unittest.mock import patch

from django.core.cache import cache, caches
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fable5.models import (
    ColorProfile,
    GarmentFlow,
    GarmentFlowCategory,
    ProductOptionProfile,
    VariantFitRule,
    VariantSizeRule,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption


class ProductConfiguratorRenderTests(TestCase):
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
            name="Худі",
            slug="hoodie-configurator",
            is_active=True,
        )
        self.flow = GarmentFlow.objects.create(
            code="hoodie-configurator",
            name="Худі",
            axes=[{
                "code": "lining",
                "label": "Утеплення",
                "options": [
                    {
                        "code": "fleece",
                        "label": "З флісом",
                        "description": "Тепла м'яка основа",
                        "default": True,
                        "icon": "layers",
                    },
                    {
                        "code": "no_fleece",
                        "label": "Без флісу",
                        "description": "Легша основа",
                        "icon": "wind",
                    },
                ],
            }],
        )
        GarmentFlowCategory.objects.create(flow=self.flow, category=self.category)
        self.product = Product.objects.create(
            title="Термохромне худі",
            slug="thermo-hoodie-configurator",
            category=self.category,
            price=1400,
            status="published",
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Класична",
            description="Прямий силует",
            is_active=True,
            is_default=True,
            order=0,
        )
        ProductFitOption.objects.create(
            product=self.product,
            code="oversize",
            label="Оверсайз",
            description="Вільний силует",
            is_active=True,
            order=1,
        )
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="lining=fleece",
            option_values={"lining": "fleece"},
            is_active=True,
            price_delta=150,
            price_delta_reason="Флісова основа",
        )
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="lining=no_fleece",
            option_values={"lining": "no_fleece"},
            is_active=False,
            price_delta_reason="Незабаром",
        )
        self.color = Color.objects.create(name="Термо-зелена", primary_hex="#a2ab92")
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=self.color,
            slug="thermo-green",
            is_default=True,
        )
        ColorProfile.objects.create(
            color=self.color,
            is_thermo=True,
            description="Змінює відтінок під дією тепла.",
        )
        VariantFitRule.objects.create(
            variant=self.variant,
            fit_code="classic",
            is_enabled=False,
            reason="Для цього кольору доступний лише оверсайз",
        )
        VariantFitRule.objects.create(
            variant=self.variant,
            fit_code="oversize",
            is_enabled=True,
        )
        VariantSizeRule.objects.create(
            variant=self.variant,
            fit_code="oversize",
            size="XXL",
            is_enabled=False,
        )
        self.url = reverse("product", kwargs={"slug": self.product.slug})

    def test_disabled_options_and_unavailable_sizes_stay_visible(self):
        response = self.client.get(self.url)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-product-option-choice="no_fleece"', html)
        self.assertIn('data-product-option-choice="classic"', html)
        self.assertIn('data-restock-size="XXL"', html)
        self.assertIn('aria-disabled="true"', html)

    def test_material_story_is_single_and_replaces_generic_premium_badge(self):
        html = self.client.get(self.url).content.decode()

        self.assertEqual(html.count("data-pdp-material-story"), 1)
        self.assertNotIn("data-generic-premium-fabric", html)
        self.assertEqual(html.count("Змінює відтінок під дією тепла."), 1)

    def test_restock_modal_exposes_all_contact_channels(self):
        html = self.client.get(self.url).content.decode()

        self.assertIn('id="productRestockModal"', html)
        for channel in ("telegram", "phone", "email", "whatsapp"):
            self.assertIn(f'data-restock-channel="{channel}"', html)
        self.assertIn(reverse("restock_subscribe"), html)

    def test_context_exposes_generic_option_axes_and_selected_values(self):
        response = self.client.get(self.url)

        axes = response.context["product_option_context"]["axes"]
        self.assertEqual([axis["code"] for axis in axes], ["lining", "fit"])
        self.assertEqual(
            response.context["product_option_context"]["selected_values"],
            {"fit": "oversize", "lining": "fleece"},
        )

    def test_fit_rows_without_applicable_garment_flow_stay_legacy_hidden(self):
        legacy_category = Category.objects.create(
            name="Лонгсліви",
            slug="legacy-longsleeve-with-fit-row",
            is_active=True,
        )
        legacy_product = Product.objects.create(
            title="Лонгслів зі старою посадкою",
            slug="legacy-longsleeve-with-fit-row",
            category=legacy_category,
            price=1100,
            status="published",
        )
        ProductFitOption.objects.create(
            product=legacy_product,
            code="classic",
            label="Класична",
            is_active=True,
            is_default=True,
        )

        response = self.client.get(
            reverse("product", kwargs={"slug": legacy_product.slug})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            "fit",
            [axis["code"] for axis in response.context["product_option_context"]["axes"]],
        )
        self.assertNotContains(response, 'data-fit-selector', html=False)

    def test_staff_sees_direct_fable5_edit_link_but_customer_does_not(self):
        edit_url = reverse("fable5_product_edit", args=[self.product.pk])

        anonymous_html = self.client.get(self.url).content.decode()
        self.assertNotIn('data-admin-product-edit', anonymous_html)
        self.assertNotIn(edit_url, anonymous_html)

        staff = get_user_model().objects.create_user(
            username="pdp-editor",
            password="password",
            is_staff=True,
        )
        self.client.force_login(staff)
        staff_html = self.client.get(self.url).content.decode()
        self.assertIn('data-admin-product-edit', staff_html)
        self.assertIn(edit_url, staff_html)
