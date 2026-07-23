"""Update the canonical classic T-shirt guide and fill missing assignments."""

from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from fable5.default_size_guides import CLASSIC_GUIDE_DATA
from fable5.models import ProductOptionSizeGrid, SizeGridProfile
from storefront.models import Product, SizeGrid


OPTION_KEY = "fit=classic"
ASSET_RELATIVE_PATH = Path("twocomms_django_theme/static/img/size-guides/classic-tshirt.webp")


class Command(BaseCommand):
    help = "Update canonical classic T-shirt grids and assign only missing classic fits."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--no-input", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        asset_path = Path(settings.BASE_DIR) / ASSET_RELATIVE_PATH
        if not asset_path.is_file():
            raise CommandError(f"Missing optimized asset: {asset_path}")

        tshirt_filter = (
            Q(category__name__icontains="футбол")
            | Q(category__name__icontains="t-shirt")
            | Q(category__slug__icontains="shirt")
            | Q(category__slug__icontains="tshirt")
        )
        products = (
            Product.objects.filter(
                tshirt_filter,
                fit_options__code="classic",
                fit_options__is_active=True,
            )
            .select_related("catalog")
            .distinct()
            .order_by("catalog_id", "id")
        )
        catalog_ids = list(
            products.exclude(catalog_id__isnull=True)
            .values_list("catalog_id", flat=True)
            .distinct()
        )
        stats = {
            "catalogs": 0,
            "grids_created": 0,
            "grids_updated": 0,
            "assignments_created": 0,
            "skipped": 0,
        }
        canonical_grids = []

        for catalog_id in catalog_ids:
            catalog_products = products.filter(catalog_id=catalog_id)
            catalog = catalog_products.first().catalog
            grid = (
                SizeGrid.objects.filter(
                    catalog_id=catalog_id,
                    fable5_profile__option_key=OPTION_KEY,
                )
                .order_by("order", "id")
                .first()
            )
            if grid is None:
                grid = (
                    SizeGrid.objects.filter(
                        catalog_id=catalog_id,
                        fable5_product_assignments__option_key=OPTION_KEY,
                    )
                    .order_by("order", "id")
                    .first()
                )
            if grid is None:
                grid = SizeGrid(
                    catalog=catalog,
                    name="Класична футболка — CRC FS-101",
                    order=0,
                    is_active=True,
                )
                stats["grids_created"] += 1
                action = "create"
            else:
                action = "reuse"

            changed = grid.guide_data != CLASSIC_GUIDE_DATA or not grid.is_active
            grid.guide_data = CLASSIC_GUIDE_DATA
            grid.is_active = True
            needs_image = not grid.image
            if needs_image and not dry_run:
                grid.image.save(
                    "classic-tshirt.webp",
                    ContentFile(asset_path.read_bytes()),
                    save=False,
                )
            if changed or needs_image:
                if action == "reuse":
                    stats["grids_updated"] += 1
                action = "update" if action == "reuse" else action

            missing = catalog_products.exclude(
                fable5_size_grid_assignments__option_key=OPTION_KEY,
            )
            missing_count = missing.count()
            stats["catalogs"] += 1
            stats["assignments_created"] += missing_count
            self.stdout.write(f"{catalog.slug}: {action}, missing assignments={missing_count}")
            if dry_run:
                canonical_grids.append(grid)
                continue

            with transaction.atomic():
                grid.save()
                profile, _ = SizeGridProfile.objects.get_or_create(size_grid=grid)
                profile.option_key = OPTION_KEY
                profile.garment_code = profile.garment_code or "tshirt"
                profile.is_active = True
                profile.save()
                ProductOptionSizeGrid.objects.bulk_create(
                    [
                        ProductOptionSizeGrid(
                            product=product,
                            option_key=OPTION_KEY,
                            size_grid=grid,
                        )
                        for product in missing.iterator()
                    ],
                    ignore_conflicts=True,
                )
            canonical_grids.append(grid)

        uncategorized = products.filter(catalog__isnull=True).exclude(
            fable5_size_grid_assignments__option_key=OPTION_KEY,
        )
        if uncategorized.exists() and canonical_grids:
            grid = canonical_grids[0]
            count = uncategorized.count()
            if not dry_run and grid.pk:
                ProductOptionSizeGrid.objects.bulk_create(
                    [
                        ProductOptionSizeGrid(
                            product=product,
                            option_key=OPTION_KEY,
                            size_grid=grid,
                        )
                        for product in uncategorized.iterator()
                    ],
                    ignore_conflicts=True,
                )
            stats["assignments_created"] += count
            self.stdout.write(f"uncategorized: reuse grid={grid.pk or 'new'}, missing assignments={count}")

        self.stdout.write(
            self.style.SUCCESS(
                "T-shirt classic guides: "
                + ", ".join(f"{key}={value}" for key, value in stats.items())
                + (" (dry-run)" if dry_run else "")
            )
        )
