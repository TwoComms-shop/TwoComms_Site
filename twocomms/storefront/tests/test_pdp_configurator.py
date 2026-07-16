from unittest.mock import patch

from django.core.cache import cache, caches
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from fable5.models import (
    ColorProfile,
    GarmentFlow,
    GarmentFlowCategory,
    ProductOptionProfile,
    VariantCombinationProfile,
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
        response = self.client.get(self.url)
        html = response.content.decode()

        self.assertEqual(html.count("data-pdp-material-story"), 1)
        self.assertNotIn("data-generic-premium-fabric", html)
        self.assertEqual(html.count("Змінює відтінок під дією тепла."), 1)
        self.assertIn('data-material-story-kind="thermo"', html)
        self.assertEqual(
            response.context["selected_variant_merchandising"]["material_story"],
            {
                "kind": "thermo",
                "title": "Термохромна тканина",
                "copy": "Змінює відтінок під дією тепла.",
                "icon": "thermo",
            },
        )

    def test_material_story_uses_fleece_context_without_product_description_fallback(self):
        self.product.description = "Загальний опис товару не є історією матеріалу."
        self.product.save(update_fields=["description"])
        ColorProfile.objects.filter(color=self.color).delete()

        response = self.client.get(self.url)
        html = response.content.decode()
        story = response.context["selected_variant_merchandising"]["material_story"]

        self.assertEqual(story["kind"], "fleece")
        self.assertEqual(story["title"], "Флісова основа")
        self.assertNotEqual(story["copy"], self.product.description)
        self.assertIn('data-material-story-kind="fleece"', html)

    def test_versioned_pdp_assets_use_one_fresh_release_key(self):
        html = self.client.get(self.url).content.decode()

        self.assertIn("css/product-detail.css?v=20260716-pdp-v2", html)
        self.assertIn("css/product-seo-landing.css?v=20260716-pdp-v2", html)
        self.assertIn("js/product-detail.js?v=20260716-pdp-v3", html)
        self.assertIn("js/telegram-verify.js?v=20260716-pdp-v2", html)
        self.assertNotIn("20260715-fable5-v1", html)
        self.assertNotIn("20260716-configurator-v1", html)

    def test_exact_inactive_combination_is_serialized_as_unavailable(self):
        VariantFitRule.objects.filter(
            variant=self.variant,
            fit_code="classic",
        ).update(is_enabled=True, reason="")
        ProductOptionProfile.objects.filter(
            product=self.product,
            option_key="lining=no_fleece",
        ).update(is_active=True, price_delta_reason="")
        key = "fit=oversize;lining=fleece"
        VariantCombinationProfile.objects.create(
            variant=self.variant,
            combination_key=key,
            option_values={"fit": "oversize", "lining": "fleece"},
            is_active=False,
        )

        response = self.client.get(self.url)
        configuration = response.context["color_variants"][0]["configurations"][key]

        self.assertFalse(configuration["is_available"])
        self.assertTrue(configuration["size_availability"])
        self.assertFalse(any(configuration["size_availability"].values()))

    @patch("storefront.views.product.logger")
    @patch(
        "fable5.services.variant_allows_purchase",
        side_effect=ValidationError("resolver unavailable"),
    )
    def test_configurator_error_logs_and_preserves_legacy_size_availability(
        self,
        _allows_purchase,
        logger_mock,
    ):
        response = self.client.get(self.url)
        sizes = {
            item["value"]: item["is_available"]
            for item in response.context["product_size_options"]
        }

        self.assertEqual(response.status_code, 200)
        logger_mock.exception.assert_called()
        self.assertFalse(sizes["XXL"])
        self.assertIn('data-restock-size="XXL"', response.content.decode())

    @patch("storefront.views.product.logger")
    @patch("fable5.services.variant_allows_options")
    def test_over_cap_flow_skips_cartesian_resolvers_and_returns_safe_state(
        self,
        allows_options_mock,
        logger_mock,
    ):
        from storefront.views.product import MAX_PRODUCT_OPTION_COMBINATIONS

        option_count = 6
        self.flow.axes = [
            {
                "code": f"axis_{axis}",
                "label": f"Axis {axis}",
                "options": [
                    {"code": f"value_{index}", "label": f"Value {index}"}
                    for index in range(option_count)
                ],
            }
            for axis in range(3)
        ]
        self.flow.save(update_fields=["axes"])
        self.assertGreater(option_count ** 3 * 2, MAX_PRODUCT_OPTION_COMBINATIONS)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        allows_options_mock.assert_not_called()
        logger_mock.exception.assert_called()
        self.assertEqual(
            response.context["color_variants"][0]["configurator_error"],
            "combination_limit",
        )
        self.assertIn(
            "data-configurator-error-state>",
            response.content.decode(),
        )
        sizes = {
            item["value"]: item["is_available"]
            for item in response.context["product_size_options"]
        }
        self.assertFalse(sizes["XXL"])

    def test_normal_legacy_empty_error_state_stays_hidden(self):
        html = self.client.get(self.url).content.decode()

        self.assertIn("data-configurator-error-state hidden", html)

    def test_restock_modal_exposes_all_contact_channels(self):
        html = self.client.get(self.url).content.decode()

        self.assertIn('id="productRestockModal"', html)
        for channel in ("telegram", "phone", "email", "whatsapp"):
            self.assertIn(f'data-restock-channel="{channel}"', html)
        self.assertIn(reverse("restock_subscribe"), html)
        for summary in ("product", "color", "options", "size"):
            self.assertIn(f'data-restock-selected-{summary}', html)
        self.assertIn("data-restock-size-select", html)

    def test_all_sizes_unavailable_renders_one_primary_notify_action(self):
        first_response = self.client.get(self.url)
        for size in first_response.context["available_sizes"]:
            VariantSizeRule.objects.update_or_create(
                variant=self.variant,
                fit_code="oversize",
                size=size,
                defaults={"is_enabled": False},
            )

        html = self.client.get(self.url).content.decode()

        self.assertIn("data-restock-empty-state>", html)
        self.assertNotIn("data-restock-empty-state hidden", html)
        self.assertEqual(html.count("data-restock-primary"), 1)
        self.assertIn("Наразі всі розміри розібрано", html)
        self.assertIn('data-restock-size=""', html)

    def test_gallery_has_position_dots_and_accessible_live_status(self):
        html = self.client.get(self.url).content.decode()

        self.assertIn("data-gallery-dots", html)
        self.assertIn("data-gallery-status", html)
        self.assertIn('role="status"', html)
        self.assertIn('aria-live="polite"', html)

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
