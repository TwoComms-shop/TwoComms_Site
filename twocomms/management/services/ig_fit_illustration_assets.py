"""Approved constructor previews illustrate a base, never an exact printed item."""
from pathlib import Path

from django.conf import settings
from django.templatetags.static import static

VERSION = "owner-brief-2026-10-18-constructor-previews.v1"


def blank_fit_illustrations(product, fits):
    from storefront.custom_print_config import CUSTOM_REF_PREVIEW_ASSETS
    from management.services.ig_catalog_media import _absolute_url
    from management.services.ig_revision_outbox import _digest

    from management.services.ig_product_focus import information_category

    category = information_category(getattr(getattr(product, "category", None), "slug", ""))
    garment = {"tshirts": "tshirt", "hoodies": "hoodie"}.get(category)
    if not garment:
        return []
    result = []
    for fit in fits:
        if fit not in {"classic", "oversize"} or (garment == "hoodie" and fit != "oversize"):
            continue
        config_fit = "regular" if fit == "classic" else fit
        source_key = f"{garment}:{config_fit}"
        assets = CUSTOM_REF_PREVIEW_ASSETS.get(source_key, {}).get("black", {})
        for side in ("front", "back"):
            asset = assets.get(side, {}).get("webp", "")
            if not asset.startswith("/static/img/configurator/custom-ref/"):
                continue
            relative = asset.removeprefix("/static/")
            try:
                url = _absolute_url(static(relative))
            except (ValueError, OSError):
                url = _absolute_url(asset)
            result.append({"asset_kind": "blank_constructor_preview", "asset_id": f"{source_key}:{side}",
                "product_id": product.pk, "variant_id": 0, "fit_code": fit,
                "fit_basis": VERSION, "base_key": source_key, "asset_version": VERSION,
                "garment_type": garment, "view": side, "static_path": relative,
                "url": url, "url_digest": _digest(url), "illustration_only": True,
                "exact_design_photo": False})
    return result


def blank_illustration_available(row):
    """Physical check occurs only in preparation, outside the metadata/CAS lane."""
    relative = str(row.get("static_path") or "")
    if not relative.startswith("img/configurator/custom-ref/") or ".." in Path(relative).parts:
        return False
    roots = [Path(settings.BASE_DIR) / "static", Path(settings.BASE_DIR) / "twocomms_django_theme" / "static"]
    roots.extend(Path(value[1] if isinstance(value, (tuple, list)) else value) for value in getattr(settings, "STATICFILES_DIRS", ()))
    if getattr(settings, "STATIC_ROOT", ""):
        roots.append(Path(settings.STATIC_ROOT))
    return any((root / relative).is_file() for root in roots)
