"""Apply the reviewed October prices, never an arithmetic price increment."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from product_catalog.models import (
    MerchCollection,
    ProductMerchCollection,
    ProductOptionProfile,
    VariantCombinationProfile,
    VariantDetails,
)
from productcolors.models import ProductColorVariant
from storefront.models import Product, ProductFitOption
from storefront.services.catalog_helpers import bump_public_product_order_version
from storefront.services.feeds_queue import are_feeds_dirty, mark_feeds_dirty
from storefront.services.october_2026_prices import (
    COLLECTION_SLUG,
    FIT_PRICE_DELTAS,
    FIT_SNAPSHOT,
    MANIFEST_VERSION,
    OPTION_PRICES,
    PRICE_CHANGES,
    THERMO_OLD_REASON,
)


def fit_profile_snapshot(change, *, target=False):
    profiles = {
        key: {"option_key": key, "price_delta": delta, "is_active": active,
              "option_values": {"fit": key.split("=", 1)[1]}}
        for _, product_id, key, delta, active in OPTION_PRICES
        if product_id == change.product_id
    }
    if target:
        for _, product_id, code, active in FIT_SNAPSHOT:
            if product_id == change.product_id and active:
                key = f"fit={code}"
                profiles[key] = {"option_key": key, "price_delta": FIT_PRICE_DELTAS[code],
                                 "is_active": True, "option_values": {"fit": code}}
    return [profiles[key] for key in sorted(profiles)]


def current_fit_profiles(rows, product_id):
    return [
        {"option_key": row.option_key, "price_delta": row.price_delta,
         "is_active": row.is_active, "option_values": row.option_values}
        for row in sorted(rows, key=lambda row: row.option_key)
        if row.product_id == product_id and row.option_key.startswith("fit=")
    ]


def expected_snapshot(change, *, target=False):
    """Only fields owned by this rollout; identity/status are separate guards."""
    snapshot = {
        "price": change.new_price if target else change.old_price,
        "discount_percent": None if target else change.old_discount,
        "variants": [
            {
                "id": variant_id,
                "price_override": (
                    change.new_price if target and change.product_id in (91, 110)
                    else override
                ),
            }
            for variant_id, override in change.variants
        ],
    }
    if change.category == "tshirts":
        snapshot["fit_profiles"] = fit_profile_snapshot(change, target=target)
    if change.product_id == 110:
        snapshot["material"] = {
            "variant_id": 81,
            "price_delta": 0 if target else 400,
            "price_delta_reason": "" if target else THERMO_OLD_REASON,
        }
    if change.product_id in (91, 92):
        snapshot["collection_225_assigned"] = target
    return snapshot


class Command(BaseCommand):
    help = (
        "Audit the fixed October 2026 price manifest as JSON. "
        "Default is read-only; --apply validates and changes every row atomically."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        apply = options["apply"]
        report = {
            "manifest": MANIFEST_VERSION,
            "generated_at": timezone.now().isoformat(),
            "mode": "apply" if apply else "dry_run",
            "currency": "UAH",
            "scope": "54 reviewed published tshirts/hoodies; other products untouched",
            "warnings": [{
                "code": "thermo_cost_uncertain",
                "product_id": 110,
                "reviewed_effective_price": 1450,
                "current_effective_price": None,
                "target_effective_price": 1250,
                "message": (
                    "The reviewed thermochromic variant charged 1050 + 400 = 1450. "
                    "The approved offered oversize price is 1100 + 150 = 1250; special material cost/margin "
                    "has not been verified. Material marketing is preserved."
                ),
            }],
            "products": [],
            "guard_failures": [],
            "post_commit": {"public_cache": "not_needed", "marketplace_feeds": "not_needed"},
        }
        try:
            with transaction.atomic():
                self._run(report, apply=apply)
        except CommandError:
            report["status"] = "blocked"
            report["applied_product_count"] = 0
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise
        report["status"] = "applied" if apply else "ready"
        report["applied_product_count"] = report["pending_product_count"] if apply else 0
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))

    def _run(self, report, *, apply):
        ids = [change.product_id for change in PRICE_CHANGES]
        tee_ids = [change.product_id for change in PRICE_CHANGES if change.category == "tshirts"]
        # Lock parent rows before dependent rows in a deterministic order. The
        # parent locks also prevent a concurrent FK variant insert on InnoDB.
        products = {
            row.pk: row for row in Product.objects.select_for_update()
            .filter(pk__in=ids).order_by("pk")
        }
        variants = list(
            ProductColorVariant.objects.select_for_update()
            .filter(product_id__in=ids).order_by("pk")
        )
        variants_by_product = {product_id: [] for product_id in ids}
        for row in variants:
            variants_by_product[row.product_id].append(row)
        variant_ids = [row.pk for row in variants]
        details = {
            row.variant_id: row for row in VariantDetails.objects.select_for_update()
            .filter(variant_id__in=variant_ids).order_by("pk")
        }
        option_rows = list(
            ProductOptionProfile.objects.select_for_update()
            .filter(product_id__in=ids).order_by("pk")
        )
        fits = list(
            ProductFitOption.objects.select_for_update()
            .filter(product_id__in=tee_ids).order_by("pk")
        )
        combinations = list(
            VariantCombinationProfile.objects.select_for_update()
            .filter(variant_id__in=variant_ids).order_by("pk")
        )
        assignments = list(
            ProductMerchCollection.objects.select_for_update()
            .filter(product_id__in=(91, 92)).order_by("pk")
        )
        collection = (
            MerchCollection.objects.select_for_update()
            .filter(slug=COLLECTION_SLUG).first()
        )
        assigned = {
            row.product_id for row in assignments
            if collection is not None and row.collection_id == collection.pk
        }
        failures = report["guard_failures"]
        actual_fits = [(row.pk, row.product_id, row.code, row.is_active) for row in fits]
        if actual_fits != sorted(FIT_SNAPSHOT):
            failures.append({"reason": "offered garment fits changed", "actual": actual_fits,
                             "expected": sorted(FIT_SNAPSHOT)})
        if collection is not None and (
            collection.kind != MerchCollection.Kind.BRIGADE or not collection.is_active
        ):
            failures.append({"collection": COLLECTION_SLUG, "reason": "not an active brigade"})
        actual_options = [
            (row.pk, row.product_id, row.option_key, row.price_delta, row.is_active)
            for row in option_rows if row.price_delta is not None
            and not (row.product_id in tee_ids and row.option_key.startswith("fit="))
        ]
        expected_options = sorted(row for row in OPTION_PRICES if row[1] not in tee_ids)
        if actual_options != expected_options:
            failures.append({
                "reason": "option pricing differs from reviewed snapshot",
                "expected": expected_options,
                "actual": actual_options,
            })
        original_ids = {(product_id, key): row_id for row_id, product_id, key, _, _ in OPTION_PRICES}
        for row in option_rows:
            original_id = original_ids.get((row.product_id, row.option_key))
            if original_id is not None and row.pk != original_id:
                failures.append({"reason": "reviewed fit profile identity changed", "product_id": row.product_id,
                                 "option_key": row.option_key, "id": row.pk, "expected_id": original_id})
        priced_combinations = [row for row in combinations if row.price_delta is not None]
        if priced_combinations:
            failures.append({
                "reason": "unexpected variant combination pricing rows",
                "ids": [row.pk for row in priced_combinations],
            })
        for variant_id, row in details.items():
            if variant_id != 81 and row.price_delta != 0:
                failures.append({
                    "variant_id": variant_id,
                    "reason": "unexpected material price delta",
                    "actual": row.price_delta,
                })

        pending = []
        for change in PRICE_CHANGES:
            product = products.get(change.product_id)
            if product is None:
                failures.append({"id": change.product_id, "reason": "missing product"})
                continue
            if (product.slug, product.category.slug, product.status) != (
                change.slug, change.category, "published"
            ):
                failures.append({"id": product.pk, "reason": "identity/category/status changed"})
            current = {
                "price": product.price,
                "discount_percent": product.discount_percent,
                "variants": [
                    {"id": row.pk, "price_override": row.price_override}
                    for row in variants_by_product[product.pk]
                ],
            }
            if change.category == "tshirts":
                current["fit_profiles"] = current_fit_profiles(option_rows, product.pk)
            if product.pk == 110:
                material = details.get(81)
                current["material"] = None if material is None else {
                    "variant_id": 81,
                    "price_delta": material.price_delta,
                    "price_delta_reason": material.price_delta_reason,
                }
                thermo_variant = next((row for row in variants_by_product[110] if row.pk == 81), None)
                if thermo_variant is not None and material is not None:
                    report["warnings"][0]["current_effective_price"] = (
                        thermo_variant.price_override if thermo_variant.price_override is not None
                        else product.final_price
                    ) + material.price_delta + next(
                        (row.price_delta or 0 for row in option_rows
                         if row.product_id == 110 and row.option_key == "fit=oversize" and row.is_active), 0
                    )
            if product.pk in (91, 92):
                current["collection_225_assigned"] = product.pk in assigned
            old = expected_snapshot(change)
            target = expected_snapshot(change, target=True)
            state = "already_applied" if current == target else "pending" if current == old else "conflict"
            report["products"].append({
                "id": product.pk,
                "slug": product.slug,
                "state": state,
                "before": current,
                "after": target,
            })
            if state == "conflict":
                failures.append({
                    "id": product.pk,
                    "reason": "price/variant/material/225 assignment changed",
                    "expected_old": old,
                    "expected_target": target,
                    "actual": current,
                })
            elif state == "pending":
                pending.append(change)
        report["pending_product_count"] = len(pending)
        report["pending_fit_profile_creations"] = sum(
            profile["option_key"] not in {
                row.option_key for row in option_rows if row.product_id == change.product_id
            }
            for change in pending if change.category == "tshirts"
            for profile in fit_profile_snapshot(change, target=True)
        )
        report["pending_fit_profile_updates"] = sum(
            row.price_delta != profile["price_delta"]
            for change in pending if change.category == "tshirts"
            for profile in fit_profile_snapshot(change, target=True)
            for row in option_rows
            if row.product_id == change.product_id and row.option_key == profile["option_key"]
        )
        report["already_applied_product_count"] = sum(
            row["state"] == "already_applied" for row in report["products"]
        )
        report["preserved_other_option_prices"] = actual_options
        report["ordinary_tee_prices"] = {"classic": 1100, "oversize": 1250}
        variant_225 = next((row for row in variants_by_product[91] if row.pk == 17), None)
        product_225 = products.get(91)
        actual_225_base = None if product_225 is None or variant_225 is None else (
            variant_225.price_override if variant_225.price_override is not None else product_225.final_price
        )
        deltas_225 = {
            row.option_key: row.price_delta or 0 for row in option_rows if row.product_id == 91
        }
        report["actual_225_prices"] = {
            "before": {
                "classic": None if actual_225_base is None else actual_225_base + deltas_225.get("fit=classic", 0),
                "oversize": None if actual_225_base is None else actual_225_base + deltas_225.get("fit=oversize", 0),
            },
            "after": {"classic": 880, "oversize": 1030},
        }
        if failures:
            raise CommandError("Price guards failed; no product prices were changed. See JSON audit.")
        if not apply or not pending:
            return

        # Mutate only the declared price fields; QuerySet.update deliberately
        # avoids full-model save hooks changing publication/SEO/marketing data.
        for change in pending:
            old = expected_snapshot(change)
            target = expected_snapshot(change, target=True)
            updated = Product.objects.filter(
                pk=change.product_id, slug=change.slug,
                status="published", price=old["price"],
                discount_percent=old["discount_percent"],
            ).update(price=target["price"], discount_percent=None)
            if updated != 1:
                raise CommandError("Concurrent price edit detected; the entire rollout was rolled back.")
            if change.category == "tshirts":
                existing_by_key = {row.option_key: row for row in option_rows if row.product_id == change.product_id}
                for target_profile in target["fit_profiles"]:
                    key = target_profile["option_key"]
                    profile = existing_by_key.get(key)
                    if profile is None:
                        # A unique key plus get_or_create guards absent profiles
                        # against another editor inserting during the rollout.
                        profile, created = ProductOptionProfile.objects.get_or_create(
                            product_id=change.product_id, option_key=key,
                            defaults={field: value for field, value in target_profile.items() if field != "option_key"},
                        )
                        if not created:
                            raise CommandError("Concurrent fit profile insert detected; the entire rollout was rolled back.")
                    elif profile.price_delta != target_profile["price_delta"]:
                        updated = ProductOptionProfile.objects.filter(
                            pk=profile.pk, product_id=change.product_id, option_key=key,
                            price_delta=profile.price_delta, is_active=profile.is_active,
                        ).update(price_delta=target_profile["price_delta"])
                        if updated != 1:
                            raise CommandError("Concurrent fit price edit detected; the entire rollout was rolled back.")
            if change.product_id in (91, 110):
                variant_id, old_override = change.variants[0]
                updated = ProductColorVariant.objects.filter(
                    pk=variant_id, product_id=change.product_id,
                    price_override=old_override,
                ).update(price_override=change.new_price)
                if updated != 1:
                    raise CommandError("Concurrent variant edit detected; the entire rollout was rolled back.")
            if change.product_id == 110:
                updated = VariantDetails.objects.filter(
                    variant_id=81, price_delta=400,
                    price_delta_reason=THERMO_OLD_REASON,
                ).update(price_delta=0, price_delta_reason="")
                if updated != 1:
                    raise CommandError("Concurrent material edit detected; the entire rollout was rolled back.")
            if change.product_id in (91, 92):
                if collection is None:
                    collection, _ = MerchCollection.objects.get_or_create(
                        slug=COLLECTION_SLUG,
                        defaults={
                            "kind": MerchCollection.Kind.BRIGADE,
                            "name_uk": "225 ОШП", "name_ru": "225 ОШП", "name_en": "225 Assault Regiment",
                            "parent": MerchCollection.objects.filter(slug="brigades").first(),
                        },
                    )
                    if collection.kind != MerchCollection.Kind.BRIGADE or not collection.is_active:
                        raise CommandError("Concurrent collection edit detected; the entire rollout was rolled back.")
                ProductMerchCollection.objects.get_or_create(
                    product_id=change.product_id, collection=collection,
                )

        # Confirm exact persisted values before committing and describing them
        # as applied in the audit. Any discrepancy rolls back all prior rows.
        for change in pending:
            actual = Product.objects.filter(pk=change.product_id).values("price", "discount_percent").get()
            actual["variants"] = list(
                ProductColorVariant.objects.filter(product_id=change.product_id)
                .order_by("pk").values("id", "price_override")
            )
            if change.category == "tshirts":
                actual["fit_profiles"] = current_fit_profiles(
                    list(ProductOptionProfile.objects.filter(product_id=change.product_id)), change.product_id,
                )
            if change.product_id == 110:
                actual["material"] = VariantDetails.objects.filter(variant_id=81).values(
                    "variant_id", "price_delta", "price_delta_reason",
                ).get()
            if change.product_id in (91, 92):
                actual["collection_225_assigned"] = ProductMerchCollection.objects.filter(
                    product_id=change.product_id, collection__slug=COLLECTION_SLUG,
                ).exists()
            if actual != expected_snapshot(change, target=True):
                raise CommandError("Persisted price verification failed; the entire rollout was rolled back.")

        # These existing mechanisms invalidate home/catalog/PDP entries and
        # request the normal debounced feed rebuild; they send no messages.
        report["post_commit"] = {"public_cache": "scheduled", "marketplace_feeds": "scheduled"}

        def refresh_public_prices():
            try:
                report["post_commit"]["public_cache_version"] = bump_public_product_order_version()
                report["post_commit"]["public_cache"] = "invalidated"
            except Exception:
                report["post_commit"]["public_cache"] = "failed"
                report["warnings"].append({
                    "code": "cache_refresh_failed", "message": "Prices committed; public cache refresh needs retry.",
                })
            try:
                mark_feeds_dirty(reason=MANIFEST_VERSION)
                dirty = are_feeds_dirty()
                report["post_commit"]["marketplace_feeds"] = "dirty" if dirty else "failed"
                if not dirty:
                    report["warnings"].append({
                        "code": "feed_refresh_failed", "message": "Prices committed; marketplace feed queue needs retry.",
                    })
            except Exception:
                report["post_commit"]["marketplace_feeds"] = "failed"
                report["warnings"].append({
                    "code": "feed_refresh_failed", "message": "Prices committed; marketplace feed queue needs retry.",
                })

        transaction.on_commit(refresh_public_prices)
