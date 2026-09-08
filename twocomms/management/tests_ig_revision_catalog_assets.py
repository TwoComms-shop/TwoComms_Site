import json
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.test import TransactionTestCase, override_settings

from management import tests_ig_revision_delivery as delivery_fixtures
from management.services.ig_catalog_media import prepare_catalog_media, select_catalog_media
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_ASSET_REFERENCES, build_revision_authority_bindings, check_fact_bindings,
)
from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control
from management.services.ig_revision_outbox import claim_next_effect, mark_provider_started, plan_revision_effects
from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import Category, Product, ProductImage


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class RevisionCatalogAssetTests(TransactionTestCase):
    reset_sequences = True
    setUp_base = delivery_fixtures.RevisionDeliveryTests.setUp

    def setUp(self):
        self.setUp_base()
        self.category = Category.objects.create(name="Asset references", slug="revision-asset-references")
        self.products = [Product.objects.create(
            category=self.category, title=f"Reference product {index}",
            slug=f"revision-reference-product-{index}", status="published", price=900,
            main_image=f"products/revision_reference_{index}.jpg",
        ) for index in range(4)]

    def _effects(self, products=None, *, color_variant_id=None):
        products = products or self.products
        # Preparation owns file-byte validation. These metadata-only fixtures
        # model that pipeline without creating any real files.
        with patch.object(FileSystemStorage, "size", return_value=100):
            selection = select_catalog_media([product.pk for product in products], color_variant_id=color_variant_id)
        prepared = prepare_catalog_media(self.settings, self.client_row.igsid, selection)
        self.assertFalse(prepared.error)
        return tuple({"group": "catalog_media", "kind": "image", "payload": payload, "projection_metadata": prepared.product_refs[index]} for index, payload in enumerate(prepared.payloads))

    def _build(self, effects, *, control=None):
        control, reasons = prepare_catalog_asset_control(self.client_row, effects, control=control)
        self.assertFalse(reasons, reasons)
        authority = build_revision_authority_bindings(self.client_row, claims=(CLAIM_CATALOG_ASSET_REFERENCES,), control=control, settings_obj=self.settings)
        self.assertTrue(authority.ready, authority.reasons)
        return authority

    def _valid(self, authority):
        return check_fact_bindings(authority.fact_bindings, revision=self.revision, client=self.client_row, settings_obj=self.settings)

    def test_four_exact_prepared_parts_are_db_only_inside_atomic(self):
        effects = self._effects()
        self.assertEqual(len(effects), 4)
        with (
            patch.object(FileSystemStorage, "open", side_effect=AssertionError("asset file open")) as opened,
            patch.object(FileSystemStorage, "size", side_effect=AssertionError("asset file stat")) as sized,
            patch.object(FileSystemStorage, "exists", side_effect=AssertionError("asset exists")) as exists,
            patch.object(FileSystemStorage, "path", side_effect=AssertionError("asset path")) as path,
            patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("network")) as http,
            transaction.atomic(),
        ):
            authority = self._build(effects)
            self.assertTrue(self._valid(authority))
        for forbidden in (opened, sized, exists, path, http):
            forbidden.assert_not_called()
        binding = authority.fact_bindings[0]
        self.assertEqual(binding["subjects"]["part_count"], 4)
        refs = binding["selector"]["catalog_asset_refs"]
        self.assertEqual([row["product_id"] for row in refs], [product.pk for product in self.products])
        self.assertEqual([row["part_index"] for row in refs], [0, 1, 2, 3])
        self.assertNotIn("https://", json.dumps(binding))
        self.assertNotIn("content_hash", json.dumps(binding))
        self.assertTrue(all(len(row["reference_digest"]) == 64 for row in refs))

    def test_used_image_path_and_product_publication_drift_invalidates(self):
        authority = self._build(self._effects())
        product = self.products[0]
        Product.objects.filter(pk=product.pk).update(main_image="products/reassigned.jpg")
        self.assertFalse(self._valid(authority))
        Product.objects.filter(pk=product.pk).update(main_image=product.main_image.name, status="draft")
        self.assertFalse(self._valid(authority))

    def test_unrelated_catalog_changes_do_not_invalidate_used_references(self):
        authority = self._build(self._effects())
        other = Product.objects.create(category=self.category, title="Unrelated", slug="unrelated-asset-product", status="published", price=100, main_image="products/unrelated.jpg")
        Product.objects.filter(pk=other.pk).update(status="draft", title="Changed", main_image="products/changed.jpg", price=200)
        Product.objects.filter(pk=self.products[0].pk).update(price=901)
        self.assertTrue(self._valid(authority))

    def test_product_image_owner_change_and_deletion_are_not_rebound(self):
        product = self.products[0]
        extra = ProductImage.objects.create(product=product, image="products/extra/reference.jpg")
        authority = self._build(self._effects([product]))
        ProductImage.objects.filter(pk=extra.pk).update(product=self.products[1])
        self.assertFalse(self._valid(authority))
        ProductImage.objects.filter(pk=extra.pk).update(product=product)
        self.assertTrue(self._valid(authority))
        extra.delete()
        self.assertFalse(self._valid(authority))

    def test_selected_variant_relation_and_color_choice_are_bound(self):
        product = self.products[0]
        color = Color.objects.create(name="Exact black", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(product=product, color=color, stock=0)
        image = ProductColorImage.objects.create(variant=variant, image="product_colors/reference_black.jpg")
        self.client_row.current_product = product
        self.client_row.current_color = color.name
        self.client_row.save(update_fields=["current_product", "current_color"])
        authority = self._build(self._effects([product], color_variant_id=variant.pk), control={"show_products": str(product.pk)})
        ref = authority.fact_bindings[0]["selector"]["catalog_asset_refs"][0]
        self.assertEqual(ref["mode"], "selected_variant")
        self.assertTrue(self._valid(authority))
        foreign_variant = ProductColorVariant.objects.create(product=self.products[1], color=color, stock=1)
        ProductColorImage.objects.filter(pk=image.pk).update(variant=foreign_variant)
        self.assertFalse(self._valid(authority))
        ProductColorImage.objects.filter(pk=image.pk).update(variant=variant)
        self.client_row.current_color = "unknown color"
        self.client_row.save(update_fields=["current_color"])
        self.assertFalse(self._valid(authority))

    def test_display_image_and_home_card_fallbacks_follow_actual_relations(self):
        product = self.products[0]
        Product.objects.filter(pk=product.pk).update(main_image="", home_card_image="products/home_cards/reference.jpg")
        product.refresh_from_db()
        color = Color.objects.create(name="Fallback blue", primary_hex="#1111ff")
        variant = ProductColorVariant.objects.create(product=product, color=color, stock=0)
        ProductColorImage.objects.create(variant=variant, image="product_colors/display_reference.jpg")
        authority = self._build(self._effects([product]))
        refs = authority.fact_bindings[0]["selector"]["catalog_asset_refs"]
        self.assertEqual([ref["asset_kind"] for ref in refs], ["home_card_image", "variant_image"])
        self.assertEqual(refs[1]["mode"], "display_fallback")
        self.assertTrue(self._valid(authority))

    def test_wrong_product_projection_or_part_order_is_rejected(self):
        effects = list(self._effects())
        effects[0] = {**effects[0], "projection_metadata": {**effects[0]["projection_metadata"], "product_id": self.products[1].pk, "title": self.products[1].title}}
        _control, reasons = prepare_catalog_asset_control(self.client_row, effects)
        self.assertTrue(reasons)
        effects = list(self._effects())
        effects[0] = {**effects[0], "projection_metadata": {**effects[0]["projection_metadata"], "part_index": 1}}
        self.assertTrue(prepare_catalog_asset_control(self.client_row, effects)[1])
        self.assertEqual(prepare_catalog_asset_control(self.client_row, ()), ({}, ()))
        self.assertEqual(prepare_catalog_asset_control(self.client_row, ({"group": "substantive_text"},)), ({}, ()))

    def test_current_asset_check_blocks_provider_start_after_deletion(self):
        product = self.products[0]
        extra = ProductImage.objects.create(product=product, image="products/extra/start_reference.jpg")
        effects = self._effects([product])
        authority = self._build(effects)
        plan = plan_revision_effects(self.revision.pk, self.revision_token,
            source_message_id=self.source.pk, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest=authority.authority_digest,
            effects=effects, fact_bindings=authority.fact_bindings, fact_checker=check_fact_bindings,
        )
        self.assertTrue(plan.created, plan.reasons)
        claim = claim_next_effect(self.revision.pk, self.revision_token, "catalog_media")
        extra.delete()
        started = mark_provider_started(claim.effect.pk, claim.token, self.revision_token, fact_checker=check_fact_bindings)
        self.assertEqual(started.reason, "fact_binding_unavailable")
        started.effect.refresh_from_db()
        self.assertIsNone(started.effect.provider_started_at)
        self.assertEqual(started.effect.state, "cancelled")
