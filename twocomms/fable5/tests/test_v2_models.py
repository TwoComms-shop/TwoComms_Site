import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from productcolors.models import Color, ProductColorVariant
from storefront.models import Catalog, Category, Product, SizeGrid

from fable5.models import VariantDetails


class Fable5V2ModelTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Футболки", slug="tshirts-v2")
        self.catalog = Catalog.objects.create(name="Футболки v2", slug="tshirts-v2")
        self.product = Product.objects.create(
            title="CRC",
            slug="crc-v2-model-test",
            category=self.category,
            catalog=self.catalog,
            price=1200,
        )
        self.color = Color.objects.create(name="Чорний", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=self.color,
            slug="black",
        )
        self.details = VariantDetails.objects.create(variant=self.variant)
        self.grid = SizeGrid.objects.create(
            catalog=self.catalog,
            name="Футболка класика",
            guide_data={"columns": [{"key": "size", "label": "Розмір"}], "rows": [{"size": "M"}]},
        )

    def test_variant_details_i18n_is_unique_per_language(self):
        from fable5.models import VariantDetailsI18n

        VariantDetailsI18n.objects.create(details=self.details, lang="uk", display_name="CRC чорна")

        with self.assertRaises(IntegrityError), transaction.atomic():
            VariantDetailsI18n.objects.create(details=self.details, lang="uk", display_name="Duplicate")

    def test_product_option_profile_is_unique_per_option_key(self):
        from fable5.models import ProductOptionProfile

        ProductOptionProfile.objects.create(
            product=self.product,
            option_key="fit=oversize",
            option_values={"fit": "oversize"},
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductOptionProfile.objects.create(
                product=self.product,
                option_key="fit=oversize",
                option_values={"fit": "oversize"},
            )

    def test_variant_combination_is_unique_per_variant_and_key(self):
        from fable5.models import VariantCombinationProfile

        VariantCombinationProfile.objects.create(
            variant=self.variant,
            combination_key="fit=classic",
            option_values={"fit": "classic"},
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            VariantCombinationProfile.objects.create(
                variant=self.variant,
                combination_key="fit=classic",
                option_values={"fit": "classic"},
            )

    def test_product_grid_and_size_rules_are_unique_in_their_scope(self):
        from fable5.models import ProductOptionSizeGrid, ProductSizeRule

        ProductOptionSizeGrid.objects.create(
            product=self.product,
            option_key="fit=classic",
            size_grid=self.grid,
        )
        ProductSizeRule.objects.create(
            product=self.product,
            option_key="fit=classic",
            size="S",
            is_enabled=False,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductOptionSizeGrid.objects.create(
                product=self.product,
                option_key="fit=classic",
                size_grid=self.grid,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductSizeRule.objects.create(
                product=self.product,
                option_key="fit=classic",
                size="S",
                is_enabled=True,
            )

    def test_garment_flow_uses_explicit_category_through_model(self):
        from fable5.models import GarmentFlow, GarmentFlowCategory

        flow = GarmentFlow.objects.create(code="tshirt-v2", name="Футболка", axes=[])
        GarmentFlowCategory.objects.create(flow=flow, category=self.category)

        self.assertEqual(list(flow.categories.all()), [self.category])
        self.assertIs(GarmentFlow.categories.through, GarmentFlowCategory)

    def test_editor_state_tracks_revision_and_unconstrained_editor(self):
        from fable5.models import ProductEditorState

        staff = get_user_model().objects.create_user(username="fable-v2-editor", is_staff=True)
        state = ProductEditorState.objects.create(product=self.product, updated_by=staff)

        self.assertEqual(state.revision, 0)
        self.assertFalse(ProductEditorState._meta.get_field("updated_by").db_constraint)

    def test_seed_migration_copies_legacy_uk_content_without_deleting_source(self):
        from fable5.models import GarmentFlow, VariantDetailsI18n

        self.details.display_name = "CRC чорна legacy"
        self.details.seo_title = "CRC чорна — купити"
        self.details.save(update_fields=["display_name", "seo_title"])
        migration = importlib.import_module("fable5.migrations.0003_seed_garment_flows")

        migration.seed_v2_defaults(apps, None)

        localized = VariantDetailsI18n.objects.get(details=self.details, lang="uk")
        self.details.refresh_from_db()
        self.assertEqual(localized.display_name, "CRC чорна legacy")
        self.assertEqual(localized.seo_title, "CRC чорна — купити")
        self.assertEqual(self.details.display_name, "CRC чорна legacy")
        self.assertTrue(GarmentFlow.objects.filter(code="tshirt", is_active=True).exists())
