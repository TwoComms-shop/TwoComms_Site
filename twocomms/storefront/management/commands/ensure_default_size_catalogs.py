import json

from django.core.management.base import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from storefront.models import Catalog, CatalogOption, CatalogOptionValue, Product, SizeGrid
from storefront.services.size_guides import SIZE_GUIDE_PRESETS


CATALOG_CONFIG = {
    "hoodie": {
        "catalog_name": "Худі",
        "catalog_slug": "hoodie-default",
        "description": "Базовий каталог і сітка розмірів для худі.",
        "order": 10,
        "category_slugs": {"hoodie"},
        "guide_name": "Hoodie size guide",
        "size_values": [
            ("XS", "XS"),
            ("S", "S"),
            ("M", "M"),
            ("L", "L"),
            ("XL", "XL"),
            ("XXL", "XXL"),
        ],
    },
    "basic_tshirt": {
        "catalog_name": "Футболки",
        "catalog_slug": "basic-tshirts",
        "description": "Базовий каталог і сітка розмірів для футболок.",
        "order": 20,
        "category_slugs": {"tshirts", "futbolki"},
        "guide_name": "Basic tee size guide",
        "size_values": [
            ("S", "S"),
            ("M", "M"),
            ("L", "L"),
            ("XL", "XL"),
            ("XXL", "2XL"),
        ],
    },
}


class Command(BaseCommand):
    help = "Ensures DB-backed default catalogs, size options, and size grids exist for hoodie and t-shirt products."

    def _upsert_catalog(self, profile_key, config):
        catalog, created = Catalog.objects.get_or_create(
            slug=config["catalog_slug"],
            defaults={
                "name": config["catalog_name"],
                "description": config["description"],
                "order": config["order"],
                "is_active": True,
            },
        )
        changed = False
        for field, value in {
            "name": config["catalog_name"],
            "description": config["description"],
            "order": config["order"],
            "is_active": True,
        }.items():
            if getattr(catalog, field) != value:
                setattr(catalog, field, value)
                changed = True
        if changed:
            catalog.save(update_fields=["name", "description", "order", "is_active", "updated_at"])
        self.stdout.write(
            f"[{profile_key}] catalog {catalog.slug} "
            + ("created" if created else "verified")
        )
        return catalog

    def _upsert_size_option(self, profile_key, catalog, config):
        option, created = CatalogOption.objects.get_or_create(
            catalog=catalog,
            name="Розмір",
            defaults={
                "option_type": CatalogOption.OptionType.SIZE,
                "is_required": True,
                "order": 0,
                "help_text": "Оберіть розмір",
            },
        )
        changed = False
        for field, value in {
            "option_type": CatalogOption.OptionType.SIZE,
            "is_required": True,
            "order": 0,
            "help_text": "Оберіть розмір",
        }.items():
            if getattr(option, field) != value:
                setattr(option, field, value)
                changed = True
        if changed:
            option.save(update_fields=["option_type", "is_required", "order", "help_text"])

        for index, (value, display_name) in enumerate(config["size_values"]):
            option_value, value_created = CatalogOptionValue.objects.get_or_create(
                option=option,
                value=value,
                defaults={
                    "display_name": display_name,
                    "order": index,
                    "is_default": index == 0,
                },
            )
            value_changed = False
            for field, field_value in {
                "display_name": display_name,
                "order": index,
                "is_default": index == 0,
            }.items():
                if getattr(option_value, field) != field_value:
                    setattr(option_value, field, field_value)
                    value_changed = True
            if value_changed:
                option_value.save(update_fields=["display_name", "order", "is_default"])
            if value_created:
                self.stdout.write(f"[{profile_key}] size value {value} created")

        self.stdout.write(
            f"[{profile_key}] size option "
            + ("created" if created else "verified")
        )

    def _upsert_size_grid(self, profile_key, catalog, config):
        # gettext_lazy metadata must be materialized before JSONField storage.
        guide_data = json.loads(json.dumps(SIZE_GUIDE_PRESETS[profile_key], cls=DjangoJSONEncoder))
        guide_data["profile_key"] = profile_key
        description = guide_data.get("intro", "")

        size_grid, created = SizeGrid.objects.get_or_create(
            catalog=catalog,
            name=config["guide_name"],
            defaults={
                "description": description,
                "guide_data": guide_data,
                "is_active": True,
                "order": 0,
            },
        )

        changed = False
        for field, value in {
            "description": description,
            "guide_data": guide_data,
            "is_active": True,
            "order": 0,
        }.items():
            if getattr(size_grid, field) != value:
                setattr(size_grid, field, value)
                changed = True
        if changed:
            size_grid.save(update_fields=["description", "guide_data", "is_active", "order", "updated_at"])

        self.stdout.write(
            f"[{profile_key}] size grid {size_grid.name} "
            + ("created" if created else "verified")
        )

    def _assign_products(self, profile_key, catalog, config):
        updated = (
            Product.objects.filter(category__slug__in=config["category_slugs"], catalog__isnull=True)
            .update(catalog=catalog)
        )
        self.stdout.write(f"[{profile_key}] products linked: {updated}")

    @transaction.atomic
    def handle(self, *args, **options):
        for profile_key, config in CATALOG_CONFIG.items():
            catalog = self._upsert_catalog(profile_key, config)
            self._upsert_size_option(profile_key, catalog, config)
            self._upsert_size_grid(profile_key, catalog, config)
            self._assign_products(profile_key, catalog, config)

        self.stdout.write(self.style.SUCCESS("Default size catalogs are in place."))
