from decimal import Decimal

from django.apps import apps
from django.test import TestCase

from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product

from fable5.models import (
    GarmentFlow,
    GarmentFlowCategory,
    ProductOptionProfile,
    VariantCombinationProfile,
    VariantDetails,
)


class GenericProductOptionTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Hoodies", slug="hoodie-options")
        self.flow = GarmentFlow.objects.create(
            code="hoodie-options",
            name="Hoodie",
            axes=[
                {
                    "code": "lining",
                    "label": "Утеплення",
                    "options": [
                        {
                            "code": "fleece",
                            "label": "Фліс",
                            "description": "Теплий ворс усередині",
                            "icon": "fleece",
                        },
                        {
                            "code": "no_fleece",
                            "label": "Без флісу",
                            "description": "Легша основа",
                            "icon": "layers",
                            "disabled": True,
                            "disabled_reason": "Тимчасово недоступно",
                        },
                    ],
                }
            ],
        )
        GarmentFlowCategory.objects.create(flow=self.flow, category=self.category)
        self.product = Product.objects.create(
            title="Hoodie option test",
            slug="hoodie-option-test",
            category=self.category,
            price=1000,
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=Color.objects.create(name="Black", primary_hex="#111111"),
            slug="black",
        )

    def test_hoodie_lining_exposes_disabled_no_fleece(self):
        from fable5.services import product_option_context

        payload = product_option_context(self.product, variant=self.variant)

        self.assertEqual(len(payload["axes"]), 1)
        lining = payload["axes"][0]
        self.assertEqual(lining["code"], "lining")
        self.assertEqual(lining["selected_value"], "fleece")
        self.assertTrue(lining["choices"][0]["is_enabled"])
        self.assertFalse(lining["choices"][1]["is_enabled"])
        self.assertEqual(lining["choices"][1]["reason"], "Тимчасово недоступно")
        self.assertTrue(lining["is_fixed"])
        self.assertEqual(lining["presentation"], "auto")

    def test_cards_presentation_keeps_single_enabled_lining_as_cards(self):
        from fable5.services import product_option_context

        presentation_model = apps.get_model("fable5", "ProductOptionAxisPresentation")
        presentation_model.objects.create(
            product=self.product,
            axis_code="lining",
            presentation="cards",
        )

        lining = product_option_context(
            self.product,
            variant=self.variant,
        )["axes"][0]

        self.assertFalse(lining["is_fixed"])
        self.assertEqual(lining["presentation"], "cards")

    def test_product_profile_can_disable_an_axis_choice(self):
        from fable5.services import product_option_context

        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="lining=fleece",
            option_values={"lining": "fleece"},
            is_active=False,
            price_delta_reason="Фліс закінчився",
        )

        payload = product_option_context(self.product, variant=self.variant)
        choices = payload["axes"][0]["choices"]

        self.assertFalse(choices[0]["is_enabled"])
        self.assertEqual(choices[0]["reason"], "Фліс закінчився")
        self.assertEqual(payload["axes"][0]["selected_value"], "")

    def test_option_price_uses_exact_combination_before_product_option(self):
        from fable5.services import variant_public_context

        VariantDetails.objects.create(
            variant=self.variant,
            price_delta=400,
            price_delta_reason="Thermo fabric",
        )
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="lining=fleece",
            option_values={"lining": "fleece"},
            price_delta=100,
            price_delta_reason="Fleece blank",
        )
        VariantCombinationProfile.objects.create(
            variant=self.variant,
            combination_key="lining=fleece",
            option_values={"lining": "fleece"},
            price_delta=290,
            price_delta_reason="Black fleece blank",
        )

        context = variant_public_context(
            self.variant,
            option_values={"lining": "fleece"},
        )

        self.assertEqual(context["final_price"], Decimal("1290"))
        self.assertEqual(context["price_delta"], Decimal("290"))
        self.assertEqual(context["price_delta_reason"], "Black fleece blank")
        self.assertEqual(context["option_values"], {"lining": "fleece"})
        self.assertEqual(context["price_breakdown"]["material_delta"], Decimal("400"))
        self.assertEqual(context["price_breakdown"]["option_delta"], Decimal("100"))
        self.assertEqual(context["price_breakdown"]["combination_override"], Decimal("290"))

    def test_material_and_independent_option_deltas_are_additive(self):
        from fable5.services import variant_public_context

        VariantDetails.objects.create(
            variant=self.variant,
            price_delta=400,
            price_delta_reason="Thermo fabric",
        )
        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="fit=oversize",
            option_values={"fit": "oversize"},
            price_delta=200,
            price_delta_reason="Oversize blank",
        )

        context = variant_public_context(
            self.variant,
            option_values={"fit": "oversize"},
        )

        self.assertEqual(context["price_delta"], Decimal("600"))
        self.assertEqual(context["final_price"], Decimal("1600"))
        self.assertEqual(
            context["price_breakdown"],
            {
                "material_delta": Decimal("400"),
                "option_delta": Decimal("200"),
                "combination_override": None,
                "total_delta": Decimal("600"),
                "option_components": {"fit=oversize": Decimal("200")},
            },
        )

    def test_disabled_option_selection_is_rejected(self):
        from fable5.services import variant_allows_options

        self.assertFalse(
            variant_allows_options(self.variant, {"lining": "no_fleece"})
        )
        self.assertTrue(
            variant_allows_options(self.variant, {"lining": "fleece"})
        )
