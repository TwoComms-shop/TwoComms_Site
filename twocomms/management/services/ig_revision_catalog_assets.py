"""Bounded current catalog asset REFERENCES, not byte/content fingerprints.

All validation uses persisted image names, row relationships and pure filesystem
storage URL construction. No image opens, size/stat calls, HTTP or HEAD requests
occur while building or checking these bindings.
"""
from __future__ import annotations

import re
from typing import Mapping
from urllib.parse import unquote

from django.core.files.storage import FileSystemStorage

from management.services.ig_catalog_media import MAX_CATALOG_MEDIA, _absolute_url, parse_product_ids
from management.services.ig_revision_outbox import _digest


CLAIM = "catalog_asset_references"
MAX_PRODUCTS = 8
MAX_MATCHES = 32
_REF_KEYS = frozenset({
    "asset_kind", "asset_id", "product_id", "variant_id", "mode", "part_index",
    "url_digest", "payload_digest", "projection_digest", "reference_digest",
    "legacy_response",
})
_HASH = re.compile(r"[0-9a-f]{64}")


class _Unavailable(Exception):
    pass


def _id(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise _Unavailable("catalog_asset_selector_invalid")
    return value


def _url(field):
    # Call the known pure implementation, not an arbitrary storage backend's
    # overridden method that could perform remote I/O during the final CAS.
    storage = field.storage
    if storage.__class__ is not FileSystemStorage:
        raise _Unavailable("catalog_asset_storage_unsupported")
    name = str(field.name or "")
    if not name or len(name) > 1024:
        raise _Unavailable("catalog_asset_reference_missing")
    return _absolute_url(FileSystemStorage.url(storage, name))


def _name_for_url(url):
    from storefront.models import Product

    storage = Product._meta.get_field("main_image").storage
    if storage.__class__ is not FileSystemStorage:
        raise _Unavailable("catalog_asset_storage_unsupported")
    marker = "__revision_asset_reference__.jpg"
    prefix = _absolute_url(FileSystemStorage.url(storage, marker))[:-len(marker)]
    if not isinstance(url, str) or len(url) > 4096 or not url.startswith(prefix):
        raise _Unavailable("catalog_asset_url_unavailable")
    name = unquote(url[len(prefix):])
    if not name or len(name) > 1024 or "?" in name or "#" in name or ".." in name.split("/"):
        raise _Unavailable("catalog_asset_url_unavailable")
    if _absolute_url(FileSystemStorage.url(storage, name)) != url:
        raise _Unavailable("catalog_asset_url_unavailable")
    return name


def _requested(client, control, used):
    if "show_products" not in control:
        requested = tuple(dict.fromkeys(used))
    elif control["show_products"] is True:
        requested = (int(client.current_product_id),) if client.current_product_id else ()
    else:
        requested = parse_product_ids(control.get("show_products")) or ()
    if not requested or len(requested) > MAX_PRODUCTS or not set(used).issubset(requested):
        raise _Unavailable("catalog_asset_products_invalid")
    return requested


def _selected_variant(client, requested):
    """Bounded version of the catalog selector's current color-name resolver."""
    from productcolors.models import ProductColorVariant

    if len(requested) != 1 or client.current_product_id != requested[0]:
        return 0
    color_text = str(client.current_color or "").strip().casefold()
    if not color_text:
        return 0
    variants = list(ProductColorVariant.objects.filter(product_id=requested[0]).select_related("color").order_by("order", "id")[:MAX_MATCHES + 1])
    if len(variants) > MAX_MATCHES:
        raise _Unavailable("catalog_asset_variant_limit")
    matched = []
    for variant in variants:
        names = {str(getattr(variant.color, key, "") or "").strip().casefold() for key in ("name", "slug")}
        names.discard("")
        if color_text in names or any(name in color_text or color_text in name for name in names):
            matched.append(variant.pk)
    return matched[0] if len(matched) == 1 else 0


def _product(product_id):
    from storefront.models import Product, ProductStatus

    product = Product.objects.filter(pk=product_id, status=ProductStatus.PUBLISHED).only("id", "title", "status", "main_image", "home_card_image").first()
    if product is None:
        raise _Unavailable("catalog_asset_product_unavailable")
    return product


def _display_fallback(product_id, image):
    from productcolors.models import ProductColorImage, ProductColorVariant

    first_variant = ProductColorVariant.objects.filter(product_id=product_id).order_by("order", "id").values_list("pk", flat=True).first()
    if image.variant_id != first_variant:
        return False
    first_image = ProductColorImage.objects.filter(variant_id=first_variant).order_by("order", "id").values_list("pk", flat=True).first()
    return image.pk == first_image


def _product_fallback_allowed(product_id, selected):
    from productcolors.models import ProductColorImage

    candidates = ProductColorImage.objects.filter(variant__product_id=product_id)
    candidates = candidates.filter(variant_id=selected) if selected else candidates.filter(variant__stock__gt=0)
    return not candidates.exclude(image="").exists()


def _variant_mode(product, image, selected):
    if image.variant.product_id != product.pk:
        raise _Unavailable("catalog_asset_owner_changed")
    if selected and image.variant_id == selected:
        return "selected_variant"
    if not selected and image.variant.stock > 0:
        return "available_variant"
    # select_catalog_media may use Product.display_image as a generic fallback
    # even when that first variant has no stock. Preserve that exact relation.
    if not product.main_image and _display_fallback(product.pk, image) and _product_fallback_allowed(product.pk, selected):
        return "display_fallback"
    raise _Unavailable("catalog_asset_variant_unavailable")


def _find_asset(product, name, selected):
    from productcolors.models import ProductColorImage
    from storefront.models import ProductImage

    variants = list(ProductColorImage.objects.filter(variant__product_id=product.pk, image=name).select_related("variant").order_by("variant__order", "variant_id", "order", "id")[:MAX_MATCHES + 1])
    images = list(ProductImage.objects.filter(product_id=product.pk, image=name).order_by("order", "id")[:MAX_MATCHES + 1])
    if len(variants) > MAX_MATCHES or len(images) > MAX_MATCHES:
        raise _Unavailable("catalog_asset_reference_limit")
    # This follows the selector's variant-before-product, duplicate-URL order.
    for image in variants:
        if (selected and image.variant_id == selected) or (not selected and image.variant.stock > 0):
            return "variant_image", image.pk, image.variant_id, _variant_mode(product, image, selected), image.image
    if not _product_fallback_allowed(product.pk, selected):
        raise _Unavailable("catalog_asset_fallback_unavailable")
    if images:
        return "product_image", images[0].pk, 0, "product", images[0].image
    for name_key in ("main_image", "home_card_image"):
        field = getattr(product, name_key)
        if str(field.name or "") == name:
            return name_key, product.pk, 0, "product", field
    for image in variants:
        if not product.main_image and _display_fallback(product.pk, image):
            return "variant_image", image.pk, image.variant_id, "display_fallback", image.image
    raise _Unavailable("catalog_asset_reference_missing")


def _load_asset(product, descriptor, selected):
    from productcolors.models import ProductColorImage
    from storefront.models import ProductImage

    kind, asset_id = descriptor["asset_kind"], _id(descriptor["asset_id"])
    if kind in {"main_image", "home_card_image", "product_image"} and not _product_fallback_allowed(product.pk, selected):
        raise _Unavailable("catalog_asset_fallback_unavailable")
    if kind in {"main_image", "home_card_image"}:
        if asset_id != product.pk or descriptor["variant_id"] != 0:
            raise _Unavailable("catalog_asset_owner_changed")
        return kind, asset_id, 0, "product", getattr(product, kind)
    if kind == "product_image":
        image = ProductImage.objects.filter(pk=asset_id, product_id=product.pk).first()
        if image is None or descriptor["variant_id"] != 0:
            raise _Unavailable("catalog_asset_owner_changed")
        return kind, asset_id, 0, "product", image.image
    if kind == "variant_image":
        image = ProductColorImage.objects.filter(pk=asset_id, variant_id=descriptor["variant_id"], variant__product_id=product.pk).select_related("variant").first()
        if image is None:
            raise _Unavailable("catalog_asset_owner_changed")
        mode = _variant_mode(product, image, selected)
        if descriptor["mode"] == "display_fallback" and not product.main_image and _display_fallback(product.pk, image) and _product_fallback_allowed(product.pk, selected):
            mode = "display_fallback"
        return kind, asset_id, image.variant_id, mode, image.image
    raise _Unavailable("catalog_asset_selector_invalid")


def _descriptor(client, product, asset, index, legacy):
    kind, asset_id, variant_id, mode, image_field = asset
    url = _url(image_field)
    payload = {"recipient": {"id": client.igsid}, "message": {"attachment": {"type": "image", "payload": {"url": url, "is_reusable": True}}}}
    if legacy:
        payload["messaging_type"] = "RESPONSE"
    projection = {"part_index": index, "product_id": product.pk, "title": str(product.title or "TwoComms")[:200]}
    reference = {
        "asset_kind": kind, "asset_id": asset_id, "product_id": product.pk,
        "variant_id": variant_id, "mode": mode, "part_index": index,
        "url_digest": _digest(url), "payload_digest": _digest(payload),
        "projection_digest": _digest(projection), "legacy_response": legacy,
    }
    reference["reference_digest"] = _digest({**reference, "image_name_digest": _digest(str(image_field.name))})
    return reference, payload, projection


def prepare_catalog_asset_control(client, effects, *, control=None):
    """Bind only prepared catalog parts; a response without images needs no claim."""
    rows = [row for row in effects or () if isinstance(row, Mapping) and row.get("group") == "catalog_media"]
    if not rows:
        return {}, ()
    if len(rows) > MAX_CATALOG_MEDIA:
        return {}, ("catalog_asset_part_limit",)
    try:
        used = []
        for index, row in enumerate(rows):
            metadata = row.get("projection_metadata")
            if row.get("kind") != "image" or not isinstance(metadata, Mapping) or set(metadata) != {"part_index", "product_id", "title"} or metadata.get("part_index") != index or isinstance(metadata.get("part_index"), bool):
                raise _Unavailable("catalog_asset_projection_invalid")
            used.append(_id(metadata["product_id"]))
        requested = _requested(client, dict(control or {}), used)
        selected = _selected_variant(client, requested)
        refs = []
        products = {product_id: _product(product_id) for product_id in set(used)}
        for index, row in enumerate(rows):
            product = products[used[index]]
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                raise _Unavailable("catalog_asset_payload_invalid")
            try:
                url = payload["message"]["attachment"]["payload"]["url"]
            except (KeyError, TypeError):
                raise _Unavailable("catalog_asset_payload_invalid")
            name = _name_for_url(url)
            asset = _find_asset(product, name, selected)
            reference, expected_payload, expected_projection = _descriptor(client, product, asset, index, payload.get("messaging_type") == "RESPONSE")
            if payload != expected_payload or dict(row["projection_metadata"]) != expected_projection:
                raise _Unavailable("catalog_asset_preparation_mismatch")
            refs.append(reference)
        return {"catalog_asset_refs": refs, "catalog_requested_products": list(requested), "catalog_selected_variant_id": selected}, ()
    except _Unavailable as exc:
        return {}, (str(exc),)
    except Exception:
        return {}, ("catalog_asset_unavailable",)


def build_catalog_asset_reference_binding(client, control):
    """Rebuild one claim from exact DB rows without reading asset bytes."""
    from management.services.ig_revision_authority import _base_binding

    refs = control.get("catalog_asset_refs")
    requested = control.get("catalog_requested_products")
    try:
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_CATALOG_MEDIA or not isinstance(requested, list) or not 1 <= len(requested) <= MAX_PRODUCTS:
            raise _Unavailable("catalog_asset_selector_invalid")
        requested = [_id(value) for value in requested]
        if len(set(requested)) != len(requested):
            raise _Unavailable("catalog_asset_selector_invalid")
        selected = _id(control.get("catalog_selected_variant_id"), zero=True)
        if selected != _selected_variant(client, requested):
            raise _Unavailable("catalog_asset_selection_changed")
        products = {}
        rebuilt = []
        for index, ref in enumerate(refs):
            if not isinstance(ref, Mapping) or set(ref) != _REF_KEYS or ref.get("part_index") != index or isinstance(ref.get("part_index"), bool) or type(ref.get("legacy_response")) is not bool:
                raise _Unavailable("catalog_asset_selector_invalid")
            if any(not _HASH.fullmatch(str(ref.get(key) or "")) for key in ("url_digest", "payload_digest", "projection_digest", "reference_digest")):
                raise _Unavailable("catalog_asset_selector_invalid")
            product_id = _id(ref["product_id"])
            _id(ref["variant_id"], zero=True)
            if product_id not in requested:
                raise _Unavailable("catalog_asset_products_invalid")
            if product_id not in products:
                products[product_id] = _product(product_id)
            product = products[product_id]
            asset = _load_asset(product, ref, selected)
            current, _payload, _projection = _descriptor(client, product, asset, index, ref["legacy_response"])
            if dict(ref) != current:
                raise _Unavailable("catalog_asset_reference_changed")
            rebuilt.append(current)
        selector = {"catalog_asset_refs": rebuilt, "catalog_requested_products": requested, "catalog_selected_variant_id": selected}
        return _base_binding(
            CLAIM, client, None, selector, {"asset_references": rebuilt, "selected_variant_id": selected},
            product_ids=sorted(products), part_count=len(rebuilt),
            asset_reference_digest=_digest(rebuilt),
        ), ""
    except _Unavailable as exc:
        return None, str(exc)
    except Exception:
        return None, "catalog_asset_unavailable"
