"""Create the canonical oversize guide and fill only missing assignments."""

from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from fable5.default_size_guides import OVERSIZE_GUIDE_DATA
from fable5.models import ProductOptionSizeGrid, SizeGridProfile
from storefront.models import Product, SizeGrid


OPTION_KEY = "fit=oversize"
ASSET_RELATIVE_PATH = Path("twocomms_django_theme/static/img/size-guides/oversize-tshirt.webp")


class Command(BaseCommand):
    help = "Create the shared oversize size guide and assign it to missing product fits."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report changes without writing them.")
        parser.add_argument("--no-input", action="store_true", help="Compatibility flag for deploy scripts.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        asset_path = Path(settings.BASE_DIR) / ASSET_RELATIVE_PATH
        if not asset_path.is_file():
            self.stderr.write(self.style.ERROR(f"Missing optimized asset: {asset_path}"))
            return

        t_shirt_category = (
            Q(category__name__icontains="футбол")
            | Q(category__name__icontains="t-shirt")
            | Q(category__slug__icontains="shirt")
            | Q(category__slug__icontains="tshirt")
        )
        products = (
            Product.objects
            .filter(
                t_shirt_category,
                fit_options__code="oversize",
                fit_options__is_active=True,
            )
            .select_related("catalog")
            .distinct()
            .order_by("catalog_id", "id")
        )
        catalog_ids = list(
            products
            .exclude(catalog_id__isnull=True)
            .values_list("catalog_id", flat=True)
            .distinct()
        )
        stats = {"catalogs": 0, "grids_created": 0, "grids_updated": 0, "assignments_created": 0, "skipped": 0}

        for catalog_id in catalog_ids:
            catalog_products = products.filter(catalog_id=catalog_id)
            catalog = catalog_products.first().catalog
            profile = (
                SizeGridProfile.objects
                .filter(size_grid__catalog_id=catalog_id, option_key=OPTION_KEY)
                .select_related("size_grid")
                .order_by("size_grid__order", "size_grid_id")
                .first()
            )
            if profile is None:
                grid = SizeGrid(
                    catalog=catalog,
                    name="Оверсайз — футболка (стандарт)",
                    guide_data=OVERSIZE_GUIDE_DATA,
                    is_active=True,
                    order=900,
                )
                profile = SizeGridProfile(size_grid=grid, option_key=OPTION_KEY, garment_code="tshirt", is_active=True)
                stats["grids_created"] += 1
                action = "create"
            else:
                grid = profile.size_grid
                changed = False
                if not grid.guide_data or not grid.guide_data.get("rows"):
                    grid.guide_data = OVERSIZE_GUIDE_DATA
                    changed = True
                if not profile.is_active:
                    profile.is_active = True
                    changed = True
                if changed:
                    stats["grids_updated"] += 1
                action = "update" if changed else "reuse"

            needs_image = not grid.image
            if needs_image and not dry_run:
                image_bytes = asset_path.read_bytes()
                grid.image.save("oversize-tshirt.webp", ContentFile(image_bytes), save=False)
                if action == "reuse":
                    stats["grids_updated"] += 1
                    action = "update"
            elif needs_image and action == "reuse":
                stats["grids_updated"] += 1
                action = "update"

            missing = catalog_products.filter(
                fit_options__code="oversize",
                fit_options__is_active=True,
            ).exclude(
                fable5_size_grid_assignments__option_key=OPTION_KEY,
            )
            missing_count = missing.count()
            stats["assignments_created"] += missing_count
            stats["catalogs"] += 1
            self.stdout.write(f"{catalog.slug}: {action}, missing assignments={missing_count}")
            if dry_run:
                continue
            with transaction.atomic():
                grid.save()
                profile.size_grid = grid
                profile.option_key = OPTION_KEY
                profile.garment_code = profile.garment_code or "tshirt"
                profile.is_active = True
                profile.save()
                ProductOptionSizeGrid.objects.bulk_create(
                    [
                        ProductOptionSizeGrid(product=product, option_key=OPTION_KEY, size_grid=grid)
                        for product in missing.iterator()
                    ],
                    ignore_conflicts=True,
                )

        uncategorized = products.filter(catalog__isnull=True).exclude(
            fable5_size_grid_assignments__option_key=OPTION_KEY,
        )
        if uncategorized.exists():
            global_profiles = (
                SizeGridProfile.objects
                .filter(
                    option_key=OPTION_KEY,
                    is_active=True,
                    size_grid__is_active=True,
                    size_grid__image__isnull=False,
                )
                .select_related("size_grid")
                .order_by("size_grid__order", "size_grid_id")
            )
            global_profile = (
                global_profiles
                .filter(
                    Q(size_grid__catalog__name__icontains="футбол")
                    | Q(size_grid__catalog__slug__icontains="shirt")
                    | Q(size_grid__catalog__slug__icontains="tshirt")
                )
                .first()
                or global_profiles.first()
            )
            if global_profile is None:
                self.stderr.write(self.style.ERROR("No canonical oversize grid exists for uncategorized T-shirts."))
                return
            count = uncategorized.count()
            if not dry_run:
                ProductOptionSizeGrid.objects.bulk_create(
                    [
                        ProductOptionSizeGrid(product=product, option_key=OPTION_KEY, size_grid=global_profile.size_grid)
                        for product in uncategorized.iterator()
                    ],
                    ignore_conflicts=True,
                )
            stats["assignments_created"] += count
            self.stdout.write(f"uncategorized: reuse grid={global_profile.size_grid_id}, missing assignments={count}")

        self.stdout.write(
            self.style.SUCCESS(
                "Oversize guides: "
                + ", ".join(f"{key}={value}" for key, value in stats.items())
                + (" (dry-run)" if dry_run else "")
            )
        )
