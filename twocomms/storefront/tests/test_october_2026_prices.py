"""Guarded October rollout against an isolated copy of reviewed pricing rows."""

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from product_catalog.models import (
    MerchCollection,
    ProductMerchCollection,
    ProductOptionProfile,
    VariantCombinationProfile,
    VariantDetails,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption
from storefront.services.october_2026_prices import (
    OPTION_PRICES,
    FIT_SNAPSHOT,
    PRICE_CHANGES,
    THERMO_OLD_REASON,
)


class October2026PricesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        categories = {
            slug: Category.objects.create(name=slug, slug=slug)
            for slug in ("tshirts", "hoodie", "long-sleeve")
        }
        Product.objects.bulk_create([
            Product(
                pk=change.product_id, title=change.slug, slug=change.slug,
                category=categories[change.category], price=change.old_price,
                discount_percent=change.old_discount, status="published",
                full_description="Preserved description", seo_title="Preserved SEO",
                drop_price=555, priority=12,
            )
            for change in PRICE_CHANGES
        ])
        cls.colors = [
            Color.objects.create(name="black", primary_hex="#000000"),
            Color.objects.create(name="white", primary_hex="#ffffff"),
        ]
        ProductColorVariant.objects.bulk_create([
            ProductColorVariant(
                pk=variant_id, product_id=change.product_id,
                color=cls.colors[index], slug=cls.colors[index].name,
                price_override=override, is_default=index == 0,
                metadata={"preserve": True}, stock=7,
            )
            for change in PRICE_CHANGES
            for index, (variant_id, override) in enumerate(change.variants)
        ])
        ProductOptionProfile.objects.bulk_create([
            ProductOptionProfile(
                pk=row_id, product_id=product_id, option_key=key,
                option_values={"fit": key.split("=", 1)[1]},
                price_delta=delta, is_active=active,
            )
            for row_id, product_id, key, delta, active in OPTION_PRICES
        ])
        ProductFitOption.objects.bulk_create([
            ProductFitOption(pk=row_id, product_id=product_id, code=code,
                             label=code, is_active=active, is_default=(code == "classic" and active) or product_id == 110 and code == "oversize")
            for row_id, product_id, code, active in FIT_SNAPSHOT
        ])
        cls.material = VariantDetails.objects.create(
            variant_id=81, price_delta=400, price_delta_reason=THERMO_OLD_REASON,
            marketing_html="<p>Термохромна тканина реагує на тепло</p>",
            seo_title="Material SEO",
        )
        cls.brigades = MerchCollection.objects.create(
            slug="brigades", kind="theme", name_uk="Бригади",
        )
        cls.collection = MerchCollection.objects.create(
            slug="225", kind="brigade", name_uk="225 ОШП", parent=cls.brigades,
        )
        cls.other_collection = MerchCollection.objects.create(
            slug="existing", kind="theme", name_uk="Existing collection",
        )
        cls.other_assignment = ProductMerchCollection.objects.create(
            product_id=91, collection=cls.other_collection,
            display_label="Owner label", order=11,
        )
        # These rows are deliberately outside the manifest and must survive
        # byte-for-byte, including an unrelated longsleeve option surcharge.
        Product.objects.bulk_create([
            Product(pk=97, title="Archived", slug="dff", category=categories["hoodie"],
                    price=2080, discount_percent=10, status="archived"),
            Product(pk=99, title="Longsleeve", slug="long-sleeve-control",
                    category=categories["long-sleeve"], price=1400,
                    discount_percent=12, status="published"),
            Product(pk=113, title="Draft tee", slug="yachiach", category=categories["tshirts"],
                    price=0, status="draft"),
            Product(pk=115, title="Draft hoodie", slug="chschs", category=categories["hoodie"],
                    price=0, status="draft"),
        ])
        ProductOptionProfile.objects.create(
            pk=55, product_id=99, option_key="fit=classic", price_delta=35,
        )
        # Production's price inventory excludes inherited (NULL) price rows.
        ProductOptionProfile.objects.create(
            product_id=2, option_key="lining=fleece", price_delta=None,
            price_delta_reason="Availability content remains",
        )
        VariantCombinationProfile.objects.create(
            variant_id=30, combination_key="lining=fleece", price_delta=None,
            price_delta_reason="Owner content remains",
        )

    def run_command(self, *, apply=False):
        output = StringIO()
        call_command("apply_october_2026_prices", apply=apply, stdout=output)
        return json.loads(output.getvalue())

    def database_snapshot(self):
        return {
            "products": list(Product.objects.order_by("pk").values()),
            "variants": list(ProductColorVariant.objects.order_by("pk").values()),
            "details": list(VariantDetails.objects.order_by("pk").values()),
            "options": list(ProductOptionProfile.objects.order_by("pk").values()),
            "fits": list(ProductFitOption.objects.order_by("pk").values()),
            "combinations": list(VariantCombinationProfile.objects.order_by("pk").values()),
            "collections": list(MerchCollection.objects.order_by("pk").values()),
            "assignments": list(ProductMerchCollection.objects.order_by("pk").values()),
        }

    def assert_blocked_without_changes(self):
        before = self.database_snapshot()
        output = StringIO()
        with self.assertRaisesMessage(CommandError, "Price guards failed"):
            call_command("apply_october_2026_prices", apply=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["applied_product_count"], 0)
        self.assertTrue(report["guard_failures"])
        self.assertEqual(self.database_snapshot(), before)

    def test_default_dry_run_writes_nothing_and_reports_actual_prices(self):
        before = self.database_snapshot()
        report = self.run_command()
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["pending_product_count"], 54)
        self.assertEqual(report["pending_fit_profile_creations"], 46)
        self.assertEqual(report["pending_fit_profile_updates"], 6)
        self.assertEqual(report["already_applied_product_count"], 0)
        self.assertEqual(report["applied_product_count"], 0)
        self.assertEqual(len(report["products"]), 54)
        self.assertEqual(report["actual_225_prices"]["after"], {"classic": 880, "oversize": 1030})
        self.assertEqual(report["warnings"][0]["current_effective_price"], 1450)
        self.assertEqual(report["warnings"][0]["target_effective_price"], 1250)

    def test_apply_fixed_targets_preserves_every_unowned_field(self):
        before = self.database_snapshot()
        report = self.run_command(apply=True)
        after = self.database_snapshot()
        self.assertEqual(report["applied_product_count"], 54)
        changes = {change.product_id: change for change in PRICE_CHANGES}
        for old, new in zip(before["products"], after["products"]):
            expected = old.copy()
            if old["id"] in changes:
                expected.update(price=changes[old["id"]].new_price, discount_percent=None)
            self.assertEqual(new, expected)
        for old, new in zip(before["variants"], after["variants"]):
            expected = old.copy()
            if old["id"] in (17, 81):
                expected["price_override"] = 880 if old["id"] == 17 else 1100
            self.assertEqual(new, expected)
        expected_material = before["details"][0].copy()
        expected_material.update(price_delta=0, price_delta_reason="")
        self.assertEqual(after["details"], [expected_material])
        after_options = {row["id"]: row for row in after["options"]}
        for old in before["options"]:
            expected = old.copy()
            if old["product_id"] in changes and changes[old["product_id"]].category == "tshirts" and old["option_key"] == "fit=oversize":
                expected["price_delta"] = 150
            self.assertEqual(after_options[old["id"]], expected)
        self.assertEqual(len(after["options"]) - len(before["options"]), 46)
        self.assertEqual(before["fits"], after["fits"])
        self.assertEqual(before["combinations"], after["combinations"])
        self.assertEqual(before["collections"], after["collections"])
        self.other_assignment.refresh_from_db()
        self.assertEqual(self.other_assignment.display_label, "Owner label")
        self.assertEqual(self.other_assignment.order, 11)
        self.assertEqual(
            set(ProductMerchCollection.objects.filter(collection=self.collection).values_list("product_id", flat=True)),
            {91, 92},
        )
        special = Product.objects.get(pk=11)
        self.assertEqual(special.price, 1850)
        self.assertFalse(special.has_discount)

    def test_second_apply_is_a_complete_noop(self):
        self.run_command(apply=True)
        before = self.database_snapshot()
        report = self.run_command(apply=True)
        self.assertEqual(report["applied_product_count"], 0)
        self.assertEqual(report["already_applied_product_count"], 54)
        self.assertEqual(report["warnings"][0]["current_effective_price"], 1250)
        self.assertEqual(report["actual_225_prices"]["before"], {"classic": 880, "oversize": 1030})
        self.assertEqual(self.database_snapshot(), before)

    def test_all_ordinary_tees_are_1100_and_hoodie_exception_is_explicit(self):
        self.assertTrue(all(change.new_price == 1100 for change in PRICE_CHANGES if change.category == "tshirts" and change.product_id != 91))
        self.assertTrue(all(change.new_price == (1850 if change.product_id == 11 else 1995) for change in PRICE_CHANGES if change.category == "hoodie"))

    def test_ordinary_classic_and_oversize_effective_prices_and_thermo_oversize(self):
        from product_catalog.services import effective_cart_unit_price

        self.run_command(apply=True)
        for change in PRICE_CHANGES:
            if change.category != "tshirts":
                continue
            product = Product.objects.get(pk=change.product_id)
            for variant in ProductColorVariant.objects.filter(product=product):
                for fit in ProductFitOption.objects.filter(product=product, is_active=True):
                    expected = (880 if fit.code == "classic" else 1030) if product.pk == 91 else (1100 if fit.code == "classic" else 1250)
                    self.assertEqual(effective_cart_unit_price(product, variant, fit_code=fit.code), expected)
        self.assertFalse(ProductFitOption.objects.get(product_id=110, code="classic").is_active)

    def test_admin_fit_price_edit_blocks_every_change(self):
        ProductOptionProfile.objects.filter(pk=54).update(price_delta=55)
        self.assert_blocked_without_changes()

    def test_admin_fit_availability_edit_blocks_every_change(self):
        ProductFitOption.objects.filter(product_id=1, code="oversize").update(is_active=False)
        self.assert_blocked_without_changes()

    def test_unreviewed_null_fit_profile_blocks_every_change(self):
        ProductOptionProfile.objects.create(product_id=1, option_key="fit=classic", price_delta=None)
        self.assert_blocked_without_changes()

    def test_concurrent_missing_fit_profile_insert_rolls_back_without_overwrite(self):
        before = self.database_snapshot()
        manager = ProductOptionProfile.objects

        def race_create(**kwargs):
            profile = manager.create(
                product_id=kwargs["product_id"], option_key=kwargs["option_key"],
                price_delta=999, price_delta_reason="Owner inserted concurrently",
            )
            return profile, False

        output = StringIO()
        with patch.object(manager, "get_or_create", side_effect=race_create):
            with self.assertRaisesMessage(CommandError, "Concurrent fit profile insert"):
                call_command("apply_october_2026_prices", apply=True, stdout=output)
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(json.loads(output.getvalue())["applied_product_count"], 0)

    def test_commit_refreshes_public_cache_and_enqueues_feeds_only_after_apply(self):
        module = "storefront.management.commands.apply_october_2026_prices"
        with patch(f"{module}.bump_public_product_order_version", return_value=123) as bump:
            with patch(f"{module}.mark_feeds_dirty") as mark:
                with patch(f"{module}.are_feeds_dirty", return_value=True):
                    with self.captureOnCommitCallbacks(execute=True):
                        self.run_command()
                    bump.assert_not_called()
                    mark.assert_not_called()
                    with self.captureOnCommitCallbacks(execute=True):
                        self.run_command(apply=True)
                    bump.assert_called_once()
                    mark.assert_called_once_with(reason="october-2026-v1")
                    with self.captureOnCommitCallbacks(execute=True):
                        self.run_command(apply=True)
                    self.assertEqual(bump.call_count, 1)
                    self.assertEqual(mark.call_count, 1)

    def test_missing_collection_is_not_created_by_dry_run(self):
        self.collection.delete()
        self.run_command()
        self.assertFalse(MerchCollection.objects.filter(slug="225").exists())
        self.run_command(apply=True)
        collection = MerchCollection.objects.get(slug="225")
        self.assertEqual(collection.kind, "brigade")
        self.assertEqual(collection.parent, self.brigades)
        self.assertEqual(collection.product_assignments.count(), 2)
        self.run_command(apply=True)
        self.assertEqual(MerchCollection.objects.filter(slug="225").count(), 1)

    def test_admin_product_price_edit_blocks_every_change(self):
        Product.objects.filter(pk=112).update(price=1091)
        self.assert_blocked_without_changes()

    def test_admin_variant_price_edit_blocks_every_change(self):
        ProductColorVariant.objects.filter(pk=17).update(price_override=801)
        self.assert_blocked_without_changes()

    def test_new_unreviewed_variant_blocks_every_change(self):
        ProductColorVariant.objects.create(
            product_id=1, color=self.colors[1], price_override=1234,
        )
        self.assert_blocked_without_changes()

    def test_admin_material_reason_edit_blocks_every_change(self):
        VariantDetails.objects.filter(variant_id=81).update(price_delta_reason="Updated by owner")
        self.assert_blocked_without_changes()

    def test_admin_225_oversize_delta_edit_blocks_every_change(self):
        ProductOptionProfile.objects.filter(pk=66).update(price_delta=151)
        self.assert_blocked_without_changes()

    def test_new_unreviewed_material_or_combination_blocks_every_change(self):
        VariantDetails.objects.create(variant_id=29, price_delta=80)
        VariantCombinationProfile.objects.create(
            variant_id=29, combination_key="fit=oversize", price_delta=35,
        )
        self.assert_blocked_without_changes()

    def test_draft_status_transition_blocks_every_change(self):
        Product.objects.filter(pk=112).update(status="draft")
        self.assert_blocked_without_changes()

    def test_missing_reviewed_product_blocks_every_change(self):
        Product.objects.filter(pk=112).delete()
        self.assert_blocked_without_changes()

    def test_late_concurrent_guard_failure_rolls_back_prior_updates(self):
        before = self.database_snapshot()
        from django.db.models.query import QuerySet

        original_update = QuerySet.update

        def race_update(queryset, **kwargs):
            if queryset.model is ProductColorVariant and kwargs == {"price_override": 1100}:
                return 0
            return original_update(queryset, **kwargs)

        output = StringIO()
        with patch.object(QuerySet, "update", race_update):
            with self.assertRaisesMessage(CommandError, "Concurrent variant edit"):
                call_command("apply_october_2026_prices", apply=True, stdout=output)
        self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(json.loads(output.getvalue())["applied_product_count"], 0)
