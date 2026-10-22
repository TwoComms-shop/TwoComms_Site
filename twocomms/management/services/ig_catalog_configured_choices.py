"""Bounded read-only catalog configuration; never inventory or checkout truth."""
from __future__ import annotations

from copy import copy
import hashlib
from itertools import product as combinations
import json
from pathlib import Path

from django.core.files.storage import FileSystemStorage

from management.services.ig_catalog_compatibility import (
    _fallback_grid, _prefetched, _usable_grid, resolve_configuration_size_contract,
)
from management.services.ig_catalog_media import (
    CatalogMediaItem, _absolute_url, _trusted_media_item,
)

MAX_COLORS = 32
MAX_CONFIGURATIONS = 128
MAX_SIZES = 32


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _reference_values(value):
    """Physical file metadata is checked by the asset owner, outside final CAS."""
    if isinstance(value, dict):
        return {k: _reference_values(v) for k, v in value.items() if k != "photo_ref"}
    if isinstance(value, list):
        return [_reference_values(v) for v in value]
    return value


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid_selector")
    if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
        raise ValueError("invalid_selector")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("invalid_selector")
    return parsed


def _photo_ref(field, *, product_id, variant_id=None, source="product"):
    """Only stat real local public assets; never invoke remote storage I/O."""
    try:
        if not field or not isinstance(field.storage, FileSystemStorage):
            return None
        path = Path(field.path)
        if not path.is_file():
            return None
        import mimetypes
        url = _absolute_url(field.url)
        size = path.stat().st_size
        mime = mimetypes.guess_type(str(field.name))[0] or ""
        if not _trusted_media_item(CatalogMediaItem(url, "", "", product_id,
                                                  mime_type=mime, size_bytes=size)):
            return None
        return {"url": url, "asset_name": str(field.name), "mime_type": mime,
                "size_bytes": size, "product_id": product_id,
                "variant_id": variant_id, "source": source}
    except (OSError, ValueError, AttributeError, NotImplementedError):
        return None


def _first_photo(owner, product_id, variant_id=None):
    for image in _prefetched(owner, "images")[:4]:
        photo = _photo_ref(image.image, product_id=product_id,
                           variant_id=variant_id, source="variant" if variant_id else "product")
        if photo:
            return photo
    if variant_id is None:
        for name in ("main_image", "home_card_image", "display_image"):
            photo = _photo_ref(getattr(owner, name, None), product_id=product_id)
            if photo:
                return photo
    return None


def _configuration_grid(product, variant, option_key):
    for owner, source in ((variant, "variant_assignment"), (product, "product_assignment")):
        assignment = next((row for row in _prefetched(owner, "product_catalog_size_grid_assignments")
                           if row.option_key == option_key), None)
        if assignment is not None:
            return _usable_grid(assignment.size_grid), source
    direct = _usable_grid(getattr(product, "size_grid", None))
    if direct is not None:
        return direct, "product_direct"
    grid = _fallback_grid(product, option_key)
    if grid is not None:
        try:
            declared_key = str(grid.product_catalog_profile.option_key or "")
        except AttributeError:
            declared_key = ""
        # Compatibility's last-resort unrelated fit must not become an image
        # presented as the customer's exact size chart.
        if declared_key and declared_key != option_key:
            return None, "unrelated_catalog_grid"
    return grid, "catalog_exact_or_neutral" if grid is not None else "missing"


def size_measurements(grid, *, product_id, variant_id=None, option_key="", allowed_sizes=None):
    """Bound exact guide cells and their own semantics, never fabricate a grid."""
    from copy import deepcopy
    import re
    from django.core.exceptions import ValidationError
    from product_catalog.size_grid_services import normalize_size_grid_payload
    from storefront.services.size_guide_authority import is_unverified_legacy_hoodie_seed

    if grid is None:
        return {}
    raw = getattr(grid, "guide_data", {}) or {}
    if is_unverified_legacy_hoodie_seed(raw):
        return {}
    if (not isinstance(raw, dict) or not isinstance(raw.get("rows"), list)
        or not isinstance(raw.get("columns"), list) or len(raw["rows"]) > MAX_SIZES
        or len(raw["columns"]) > 12):
        return {}
    try:
        guide = normalize_size_grid_payload(deepcopy(raw))
    except (ValidationError, TypeError, ValueError):
        return {}
    columns = [{"key": row["key"], "label": row["label"][:160]} for row in guide["columns"] if row["key"] != "size"]
    rows = []
    for row in guide["rows"]:
        if allowed_sizes is not None and row["size"] not in allowed_sizes:
            continue
        cells = {column["key"]: row.get(column["key"], "")[:80] for column in columns}
        # Empty or prose-only guides are not numeric measurement evidence.
        if any(re.search(r"\d", value) for value in cells.values()):
            rows.append({"size": row["size"], **cells})
    if not rows:
        return {}
    notes = {key: [str(value)[:300] for value in guide.get(key, ())[:8]] for key in ("notes", "fit_notes")}
    intro = guide.get("intro", "")[:600]
    semantics_text = " ".join([intro, *notes["notes"], *notes["fit_notes"]])
    unit = str(raw.get("unit") or raw.get("units") or "")[:30]
    if not unit and re.search(r"(?<!\w)(?:см|cm)(?!\w)", semantics_text, re.I):
        unit = "cm"
    flat = bool(re.search(r"розкладен\w*|разложен\w*|laid\s+flat|flat[- ]garment", semantics_text, re.I))
    body = bool(re.search(r"замір\w*\s+тіла|мерк\w*\s+тел\w*|body\s+measurements?", semantics_text, re.I))
    result = {"schema": "size-measurements.v1", "product_id": product_id, "variant_id": variant_id,
        "option_key": option_key, "grid_id": getattr(grid, "pk", None), "columns": columns, "rows": rows,
        "unit": unit or "unspecified", "measurement_semantics": "flat_garment" if flat and not body else "body" if body and not flat else "unspecified",
        "intro": intro, **notes, "tolerance_source_text": [text for text in [intro, *notes["notes"], *notes["fit_notes"]]
            if re.search(r"±|похиб\w*|погреш\w*|допуск\w*|tolerance", text, re.I)][:8], "legend": [{"label": str(item.get("label", ""))[:160],
            "description": str(item.get("description", ""))[:300]} for item in guide.get("legend", [])[:12]],
        "comparison_guidance": "Compare the same named measurement and units on a comfortable garment. Do not equate width or half-chest with body circumference. If semantics/units are unspecified, clarify before recommending a size. Explain adjacent sizes; do not guarantee fit.",
        "selection_authority": False, "stock_authority": False}
    result["guide_version"] = _digest(raw)
    return result


def _configuration(product, variant, values, *, metadata_only=False):
    from product_catalog.content_resolution import build_combination_key
    from product_catalog.size_grid_services import normalize_size_value

    key = build_combination_key(values)
    fit = values.get("fit", "")
    grid, source = _configuration_grid(product, variant, key)
    contract_product = product
    if source == "unrelated_catalog_grid":
        # Keep catalog size options and enabled rules, remove only the unsafe
        # unrelated grid from this read-only compatibility view.
        contract_product = copy(product)
        contract_product.catalog = copy(product.catalog)
        contract_product.catalog._prefetched_objects_cache = {
            **product.catalog._prefetched_objects_cache, "size_grids": []}
    row = {"option_key": key, "fit_code": fit}
    sizes, has_contract = resolve_configuration_size_contract(contract_product, variant, row)
    if len(sizes) > MAX_SIZES:
        raise ValueError("size_configuration_too_large")
    product_rules = {normalize_size_value(rule.size): rule for rule in
                     _prefetched(product, "product_catalog_size_rules") if rule.option_key == key}
    variant_rules = {(rule.fit_code, normalize_size_value(rule.size)): rule for rule in
                     _prefetched(variant, "product_catalog_size_rules")}
    choices = []
    for size in sizes:
        pr = product_rules.get(size)
        vr = variant_rules.get((fit, size)) or variant_rules.get(("", size))
        choices.append({"value": size, "product_rule_id": getattr(pr, "pk", None),
                        "variant_rule_id": getattr(vr, "pk", None),
                        "grid_id": getattr(grid, "pk", None)})
    grid_ref = {"grid_id": getattr(grid, "pk", None), "source": source,
                "image_name": str(getattr(getattr(grid, "image", None), "name", "") or ""),
                "measurements": size_measurements(grid, product_id=product.pk, variant_id=getattr(variant, "pk", None),
                    option_key=key, allowed_sizes={row["value"] for row in choices}),
                "photo_ref": None if metadata_only else _photo_ref(getattr(grid, "image", None), product_id=product.pk,
                                        variant_id=getattr(variant, "pk", None), source="size_grid")}
    result = {"variant_id": getattr(variant, "pk", None), "option_values": values,
              "option_key": key, "fit_code": fit, "sizes": choices, "grid": grid_ref,
              "has_size_contract": has_contract}
    result["configuration_digest"] = _digest({**_reference_values(result), "guide_data": getattr(grid, "guide_data", {}),
        "product_rules": [(r.pk, r.option_key, r.size, r.is_enabled) for r in
                          _prefetched(product, "product_catalog_size_rules")],
        "variant_rules": [(r.pk, r.fit_code, r.size, r.is_enabled) for r in
                          _prefetched(variant, "product_catalog_size_rules")]})
    return result


def _capture_product(product, variants, selected, variant_id, *, metadata_only=False):
    from product_catalog.content_resolution import build_combination_key
    from product_catalog.services import product_option_context

    rows, colors = [], []
    for variant in variants or [None]:
        if variant_id is not None and getattr(variant, "pk", None) != variant_id:
            continue
        axes = product_option_context(product, variant=variant).get("axes") or []
        if len(axes) > 8:
            raise ValueError("option_configuration_too_large")
        groups = [[choice for choice in axis.get("choices", ()) if choice.get("is_enabled")]
                  for axis in axes]
        fit_options = {r.code: r for r in _prefetched(product, "fit_options")}
        fit_notes = {r.fit_code: r for r in _prefetched(product, "product_catalog_fit_notes")}
        for axis, group in zip(axes, groups):
            if axis["code"] == "fit":
                group[:] = [choice for choice in group
                    if (choice["code"] not in fit_options or fit_options[choice["code"]].is_active)
                    and (choice["code"] not in fit_notes or fit_notes[choice["code"]].is_enabled)]
        count = 1
        for group in groups:
            count *= len(group)
        if count > MAX_CONFIGURATIONS or (groups and not all(groups)):
            raise ValueError("option_configuration_unavailable")
        exact_combinations = {r.combination_key: r for r in
                              _prefetched(variant, "product_catalog_combinations")}
        variant_rows = []
        for choices in combinations(*groups) if groups else [()]:
            values = {axis["code"]: choice["code"] for axis, choice in zip(axes, choices)}
            if any(values.get(k) != v for k, v in selected.items()):
                continue
            override = exact_combinations.get(build_combination_key(values))
            if override is not None and not override.is_active:
                continue
            variant_rows.append(_configuration(product, variant, values, metadata_only=metadata_only))
            if len(rows) + len(variant_rows) > MAX_CONFIGURATIONS:
                raise ValueError("option_configuration_too_large")
        rows.extend(variant_rows)
        if variant is not None and variant_rows:
            name = str(getattr(variant.color, "name", "") or "").strip()[:100]
            colors.append({"variant_id": variant.pk, "canonical_name": name, "label": name,
                "configured": True, "photo_ref": None if metadata_only else _first_photo(variant, product.pk, variant.pk),
                "configuration_digest": _digest([r["configuration_digest"] for r in variant_rows])})
    return rows, colors


def capture_configured_choices(product_id, *, variant_id=None, option_values=None, fit_code=None,
                               metadata_only=False):
    """Capture published configured choices including zero-stock enabled rows.

    A caller must retain selectors and digest and recapture before applying a
    choice. This DTO grants no stock, price, payment or checkout authority.
    """
    result = {"schema": "ig_configured_choices_v1", "product_id": None,
              "variant_id": None, "option_values": {}, "fit_code": "",
              "configured": True, "availability": "unverified", "colors": [],
              "configurations": [], "sizes": [], "grid": {}, "photo_ref": None,
              "configuration_digest": "", "error": ""}
    try:
        from product_catalog.content_resolution import normalize_option_values
        from management.services.ig_catalog_pricing import prepare_pricing_context
        from storefront.models import Product, ProductStatus

        product_id = _positive_id(product_id)
        variant_id = _positive_id(variant_id) if variant_id is not None else None
        selected = normalize_option_values(option_values)
        if fit_code is not None:
            fit = normalize_option_values({"fit": fit_code})["fit"]
            if selected.get("fit", fit) != fit:
                raise ValueError("conflicting_fit_selector")
            selected["fit"] = fit
        result.update(product_id=product_id, variant_id=variant_id, option_values=selected,
                      fit_code=selected.get("fit", ""))
        product = Product.objects.filter(pk=product_id, status=ProductStatus.PUBLISHED).select_related(
            "catalog", "size_grid").prefetch_related("images").first()
        if product is None:
            raise ValueError("product_unavailable")
        variants = list(product.color_variants.select_related("color").prefetch_related("images")
                        .order_by("order", "id")[:MAX_COLORS + 1])
        if len(variants) > MAX_COLORS:
            raise ValueError("color_configuration_too_large")
        prepare_pricing_context([product], variants)
        rows, colors = _capture_product(product, variants, selected, variant_id, metadata_only=metadata_only)
        result.update(configurations=rows, colors=colors)
        exact_variant = next((v for v in variants if v.pk == variant_id), None)
        result["photo_ref"] = None if metadata_only else _first_photo(exact_variant, product_id, variant_id) if exact_variant else (
            _first_photo(product, product_id) if variant_id is None else None)
        if len(rows) == 1:
            result.update(sizes=rows[0]["sizes"], grid=rows[0]["grid"])
        else:
            result["error"] = "configuration_ambiguous" if rows else "configuration_unavailable"
        image_bindings = [("product", product.pk, str(getattr(image.image, "name", "") or ""))
                          for image in _prefetched(product, "images")[:4]]
        image_bindings.extend(("product", product.pk, str(getattr(getattr(product, name, None), "name", "") or ""))
                              for name in ("main_image", "home_card_image", "display_image"))
        for variant in variants:
            image_bindings.extend(("variant", variant.pk, str(getattr(image.image, "name", "") or ""))
                                  for image in _prefetched(variant, "images")[:4])
        result["configuration_digest"] = _digest({
            **_reference_values({k: v for k, v in result.items() if k != "configuration_digest"}),
            "image_bindings": image_bindings})
    except (TypeError, ValueError) as exc:
        result["error"] = str(exc)[:100]
    except Exception:
        result["error"] = "configuration_capture_failed"
    return result
