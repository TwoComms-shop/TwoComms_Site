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
FIT_PHOTO_SCHEMA = "fit-photo-capability.v1"
_REF_KEYS = frozenset({
    "asset_kind", "asset_id", "product_id", "variant_id", "mode", "part_index",
    "url_digest", "payload_digest", "projection_digest", "reference_digest",
    "legacy_response",
})
_HASH = re.compile(r"[0-9a-f]{64}")


def _explicit_photo_fit(text):
    """A merchant-authored photo description can depict one cut, never two.

    Asset filenames and available/default product options supply no evidence
    about the cut shown in a photograph.
    """
    text = str(text or "").strip()
    if not text or "?" in text:
        return ""
    # A design called “Classic” is not a declaration of the depicted cut.
    text = re.sub(r'«[^»\n]*»|“[^”\n]*”|„[^“\n]*“|‹[^›\n]*›|"[^"\n]*"|‘[^’\n]*’|(?<!\w)\'[^\'\n]*\'(?!\w)',
                  lambda match: " " * len(match.group()), text)
    patterns = {"classic": r"\b(?:classic|regular)\b|\bкласичн\w*|\bклассическ\w*",
                "oversize": r"\boversiz(?:e|ed)\b|\bоверсайз\w*"}
    matches = [(code, match) for code, pattern in patterns.items()
               for match in re.finditer(pattern, text, re.I)]
    if len({code for code, _match in matches}) != 1:
        return ""
    for _code, match in matches:
        before = text[max(0, match.start() - 70):match.start()]
        if re.search(r"\b(?:не|ні|not|no|без|without)\b[^.!;\n]{0,65}$", before, re.I):
            return ""
        after = text[match.end():match.end() + 45]
        if not (re.match(r"\s*[-–—:]?\s*(?:футболк\w*|худ[иі]|t[ -]?shirts?|hoodies?|fits?|cuts?|крій|кро[йюїієе]\w*|посадк\w*)\b", after, re.I)
                or re.search(r"\b(?:fits?|cuts?|крій|кро[йюїієе]\w*|посадк\w*)\s*[-–—:]?\s*$", before, re.I)):
            return ""
    return matches[0][0]


def _photo_garment(text):
    kinds = [kind for kind, pattern in (("tshirt", r"футболк\w*|\bt-?shirts?\b"),
                                       ("hoodie", r"худ[иі]|\bhoodies?\b"))
             if re.search(pattern, str(text or ""), re.I)]
    return kinds[0] if len(kinds) == 1 else ""


def _fit_photo(product, *, kind, asset_id, variant_id, field, alt):
    url = _url(field)
    fit = _explicit_photo_fit(alt)
    return {"asset_kind": kind, "asset_id": asset_id, "product_id": product.pk,
        "variant_id": variant_id, "title": str(product.title or "TwoComms")[:200],
        "fit_code": fit, "fit_basis": "native_image_alt" if fit else "unverified",
        "alt_digest": _digest(str(alt or "")), "garment_type": _photo_garment(alt),
        "image_name_digest": _digest(str(field.name)), "url_digest": _digest(url), "url": url}


def _fit_product_photos(product):
    from productcolors.models import ProductColorImage
    from storefront.models import ProductImage

    variants = list(ProductColorImage.objects.filter(variant__product_id=product.pk)
        .exclude(image="").order_by("variant__order", "variant_id", "order", "id")[:MAX_MATCHES + 1])
    generic = list(ProductImage.objects.filter(product_id=product.pk).exclude(image="")
        .order_by("order", "id")[:MAX_MATCHES + 1])
    if len(variants) + len(generic) > MAX_MATCHES:
        raise _Unavailable("fit_photo_reference_limit")
    photos = []
    for row in variants:
        photos.append(_fit_photo(product, kind="variant_image", asset_id=row.pk,
            variant_id=row.variant_id, field=row.image, alt=row.alt_text))
    for row in generic:
        photos.append(_fit_photo(product, kind="product_image", asset_id=row.pk,
            variant_id=0, field=row.image, alt=row.alt_text))
    for kind in ("main_image", "home_card_image"):
        field = getattr(product, kind)
        if field:
            photos.append(_fit_photo(product, kind=kind, asset_id=product.pk, variant_id=0,
                field=field, alt=product.main_image_alt if kind == "main_image" else ""))
    # Prefer the explicitly described native row when a cover repeats its URL.
    seen, result = set(), []
    for photo in sorted(photos, key=lambda row: not bool(row["fit_code"])):
        if photo["url"] not in seen:
            seen.add(photo["url"])
            result.append(photo)
    return result


def _fit_example_photos(product, missing, garment):
    """Same-garment merchant-described illustrations; never catalog choices."""
    from productcolors.models import ProductColorImage
    from storefront.models import Product, ProductImage, ProductStatus
    from django.db.models import Q

    if not missing or not garment:
        return []
    descriptions = Q(alt_text__iregex=r"classic|regular|[Кк][Лл][Аа][Сс][Ии][Чч]|[Кк][Лл][Аа][Сс][Сс][Ии][Чч][Ее][Сс]|oversiz|[Оо][Вв][Ее][Рр][Сс][Аа][Йй][Зз]")
    variants = list(ProductColorImage.objects.filter(descriptions,
        variant__product__category_id=product.category_id, variant__product__status=ProductStatus.PUBLISHED)
        .exclude(variant__product_id=product.pk).exclude(image="").select_related("variant__product")
        .order_by("variant__product_id", "order", "id")[:MAX_MATCHES])
    generic = list(ProductImage.objects.filter(descriptions,
        product__category_id=product.category_id, product__status=ProductStatus.PUBLISHED)
        .exclude(product_id=product.pk).exclude(image="").select_related("product")
        .order_by("product_id", "order", "id")[:MAX_MATCHES])
    examples, found = [], set()
    for row in [*variants, *generic]:
        fit = _explicit_photo_fit(row.alt_text)
        if fit not in missing or fit in found or _photo_garment(row.alt_text) != garment:
            continue
        owner = row.variant.product if isinstance(row, ProductColorImage) else row.product
        photo = _fit_photo(owner, kind="variant_image" if isinstance(row, ProductColorImage) else "product_image",
            asset_id=row.pk, variant_id=row.variant_id if isinstance(row, ProductColorImage) else 0,
            field=row.image, alt=row.alt_text)
        photo["illustration_only"] = True
        examples.append(photo)
        found.add(fit)
    return examples


def capture_fit_photo_capability(product_id, *, metadata_only=False):
    """Read native depicted-cut evidence independently of stock/selection.

    Both modes use DB references and pure local URL construction only. File
    existence/size is checked separately when preparing physical attachments.
    """
    del metadata_only
    from management.services.ig_catalog_configured_choices import capture_configured_choices
    from storefront.models import Product, ProductStatus

    result = {"schema": FIT_PHOTO_SCHEMA, "product_id": product_id, "title": "",
        "supported_fits": [], "photos": [], "missing_fit_photos": [],
        "examples": [], "illustrations": [], "example_product_ids": [], "capability_digest": "", "error": ""}
    try:
        product_id = _id(product_id)
        product = Product.objects.select_related("category").filter(pk=product_id, status=ProductStatus.PUBLISHED).first()
        if product is None:
            raise _Unavailable("fit_photo_product_unavailable")
        configured = capture_configured_choices(product_id, metadata_only=True)
        fits = sorted({row["fit_code"] for row in configured.get("configurations", ())
                       if row.get("fit_code") in {"classic", "oversize"}})
        photos = _fit_product_photos(product)
        if product.category.slug in {"hoodie", "hoodies"}:
            # Owner Q02: every current actual hoodie photo depicts the same
            # slightly oversize base. Constructor key aliases prove no second cut.
            fits = ["oversize"] if configured.get("configurations") else []
            photos = [{**row, "fit_code": "oversize", "fit_basis": "owner-brief-2026-10-18-current-hoodie-base"} for row in photos]
        missing = [fit for fit in fits if not any(row["fit_code"] == fit for row in photos)]
        garment = _photo_garment(product.title) or _photo_garment(product.category.name)
        if not garment:
            kinds = {row["garment_type"] for row in photos if row["garment_type"]}
            garment = next(iter(kinds)) if len(kinds) == 1 else ""
        examples = _fit_example_photos(product, missing, garment)
        from management.services.ig_fit_illustration_assets import blank_fit_illustrations
        illustrations = blank_fit_illustrations(product, fits)
        result.update(title=str(product.title or "TwoComms")[:200], supported_fits=fits, photos=photos,
            missing_fit_photos=missing, examples=examples, illustrations=illustrations,
            example_product_ids=list(dict.fromkeys(row["product_id"] for row in examples)),
            configuration_digest=configured.get("configuration_digest", ""))
        result["capability_digest"] = _digest({key: value for key, value in result.items()
                                               if key != "capability_digest"})
    except (_Unavailable, ValueError, TypeError) as exc:
        result["error"] = str(exc)
    return result


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
    """An admitted current native color wins; raw variant hints never do."""
    from productcolors.models import ProductColorVariant

    if len(requested) != 1:
        return 0
    if getattr(client, "pk", None):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_catalog_choice_actions import admitted_native_color_variants

        capture = capture_current_selection_lines(client.pk)
        active = capture.get("active_line_id")
        rows = [row for row in capture.get("lines") or [] if row.get("line_id") == active
            and (row.get("fields", {}).get("product_id") or {}).get("status") == "confirmed"
            and (row.get("fields", {}).get("product_id") or {}).get("value") == requested[0]]
        if capture.get("status") == "captured" and capture.get("coverage_complete") and len(rows) == 1:
            native = admitted_native_color_variants(capture, rows).get(active)
            if native is not None:
                if native.get("status") != "verified":
                    raise _Unavailable("catalog_asset_native_variant_unverified")
                return _id(native["variant_id"])
    if client.current_product_id != requested[0]:
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


def _find_asset(product, name, selected, *, allow_generic=False):
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
    if not allow_generic and not _product_fallback_allowed(product.pk, selected):
        raise _Unavailable("catalog_asset_fallback_unavailable")
    if images:
        return "product_image", images[0].pk, 0, "presentation_generic" if allow_generic else "product", images[0].image
    for name_key in ("main_image", "home_card_image"):
        field = getattr(product, name_key)
        if str(field.name or "") == name:
            return name_key, product.pk, 0, "presentation_generic" if allow_generic else "product", field
    for image in variants:
        if not product.main_image and _display_fallback(product.pk, image):
            return "variant_image", image.pk, image.variant_id, "display_fallback", image.image
    raise _Unavailable("catalog_asset_reference_missing")


def _load_asset(product, descriptor, selected):
    from productcolors.models import ProductColorImage
    from storefront.models import ProductImage

    kind, asset_id = descriptor["asset_kind"], _id(descriptor["asset_id"])
    if kind in {"main_image", "home_card_image", "product_image"} and descriptor.get("mode") != "presentation_generic" and not _product_fallback_allowed(product.pk, selected):
        raise _Unavailable("catalog_asset_fallback_unavailable")
    if kind in {"main_image", "home_card_image"}:
        if asset_id != product.pk or descriptor["variant_id"] != 0:
            raise _Unavailable("catalog_asset_owner_changed")
        return kind, asset_id, 0, "presentation_generic" if descriptor.get("mode") == "presentation_generic" else "product", getattr(product, kind)
    if kind == "product_image":
        image = ProductImage.objects.filter(pk=asset_id, product_id=product.pk).first()
        if image is None or descriptor["variant_id"] != 0:
            raise _Unavailable("catalog_asset_owner_changed")
        return kind, asset_id, 0, "presentation_generic" if descriptor.get("mode") == "presentation_generic" else "product", image.image
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
    illustrations = [row for row in effects or () if isinstance(row, Mapping)
        and "fit_illustration" in (row.get("projection_metadata") or {})]
    if illustrations:
        from management.services.ig_product_inquiry_presentation import (
            fit_illustration_reference, validate_fit_illustration_metadata,
        )
        manifest = illustrations[0]["projection_metadata"]["fit_illustration"]
        if any(row["projection_metadata"]["fit_illustration"] != manifest or not
            validate_fit_illustration_metadata(row["projection_metadata"], payload=row.get("payload"),
                group=row.get("group"), kind=row.get("kind")) for row in illustrations):
            return {}, ("fit_illustration_effect_invalid",)
        if (len(illustrations) != len(manifest["items"]) + 2 or sum(
            row["projection_metadata"]["delivery"] == "native" for row in illustrations) != 1
            or any(row.get("group") == "catalog_media" or "catalog_choice" in (row.get("projection_metadata") or {})
                   for row in effects or ())):
            return {}, ("fit_illustration_effect_invalid",)
        # The chart is an independent signed asset, even when both owners
        # share one ordered native template delivery group.
        remainder = [row for row in effects or () if row not in illustrations]
        other, reasons = prepare_catalog_asset_control(client, remainder, control=control)
        if reasons:
            return {}, reasons
        if other and set(other) not in ({"catalog_chart_reference"}, {"catalog_chart_references"}):
            return {}, ("fit_illustration_effect_invalid",)
        return {"fit_illustration_reference": fit_illustration_reference(manifest), **other}, ()
    charts = [row for row in effects or () if isinstance(row, Mapping)
        and "catalog_chart" in (row.get("projection_metadata") or {})]
    if charts:
        from management.services.ig_catalog_choice_presentations import catalog_chart_reference, validate_catalog_chart_metadata
        if not 1 <= len(charts) <= 2 or any("catalog_choice" in (row.get("projection_metadata") or {}) for row in effects or ()):
            return {}, ("catalog_choice_chart_effect_invalid",)
        if any(not validate_catalog_chart_metadata(row["projection_metadata"], payload=row.get("payload"), group=row.get("group"), kind=row.get("kind")) for row in charts):
            return {}, ("catalog_choice_chart_effect_invalid",)
        references = [catalog_chart_reference(row["projection_metadata"]["catalog_chart"]) for row in charts]
        return ({"catalog_chart_reference": references[0]} if len(references) == 1 else {"catalog_chart_references": references}), ()
    choices = [row for row in effects or () if isinstance(row, Mapping)
        and "catalog_choice" in (row.get("projection_metadata") or {})]
    if choices:
        from management.services.ig_catalog_choice_presentations import SCHEMA_V1, validate_catalog_choice_metadata

        if not choices or any(not validate_catalog_choice_metadata(row["projection_metadata"],
            payload=row.get("payload"), group=row.get("group"), kind=row.get("kind")) for row in choices):
            return {}, ("catalog_choice_effects_invalid",)
        manifests = [row["projection_metadata"]["catalog_choice"] for row in choices]
        manifest = manifests[0]
        expected_fallback = 1 if manifest["schema"] == SCHEMA_V1 else len(manifest["fallback_payload_digests"])
        native = [row for row in choices if row["projection_metadata"]["delivery"] == "native"]
        fallback = [row for row in choices if row["projection_metadata"]["delivery"] == "fallback"]
        if (any(value != manifest for value in manifests) or len(native) != 1 or len(fallback) != expected_fallback
            or [row["projection_metadata"].get("part_index", 0) for row in fallback] != list(range(expected_fallback))):
            return {}, ("catalog_choice_effects_invalid",)
        return {"catalog_choice_presentation": manifest}, ()
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


def _chart_selector(reference):
    if reference.get("origin_manifest_digest"):
        raise ValueError("catalog_combined_asset_origin_unsupported")
    return {"digest": reference["chart_digest"], "hmac": reference["hmac"],
        "fit_code": (reference.get("configuration_selector") or {}).get("fit_code"),
        "comparison": (reference.get("presentation_context") or {}).get("schema") == "discussed-fit-comparison.v1",
        "native_part_index": reference.get("native_part_index", 0),
        "unavailable_requested_fits": reference.get("unavailable_requested_fits", [])}


def _restore_charts(*, revision_id, client_id, settings_id, selectors):
    from types import SimpleNamespace
    from management.models import IgCustomerTurnRevision, InstagramBotSettings
    from management.services.ig_catalog_choice_presentations import prepare_catalog_chart_effect, catalog_chart_reference
    from management.services.ig_product_focus import capture_fit_chart_comparison
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 2:
        raise ValueError("catalog_combined_asset_reference_invalid")
    revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=revision_id, client_id=client_id).first()
    settings_row = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(pk=settings_id).first()
    if revision is None or settings_row is None:
        raise ValueError("catalog_choice_chart_context_changed")
    references = []
    for selector in selectors:
        context = capture_fit_chart_comparison(revision, selector["fit_code"]) if selector["comparison"] else None
        prepared = prepare_catalog_chart_effect(revision, SimpleNamespace(control={}, reply_text=""), settings_row,
            presentation_context=context, native_part_index=selector["native_part_index"], metadata_only=True, _single=True,
            unavailable_requested_fits=selector["unavailable_requested_fits"])
        if not prepared.ready or len(prepared.effects) != 1:
            raise ValueError("catalog_choice_chart_changed")
        reference = catalog_chart_reference(prepared.effects[0]["projection_metadata"]["catalog_chart"])
        if _chart_selector(reference) != selector:
            raise ValueError("catalog_choice_chart_signature_invalid")
        references.append(reference)
    return references


def _compact_combined_reference(control):
    from copy import deepcopy
    fit = deepcopy(control["fit_illustration_reference"])
    charts = control.get("catalog_chart_references") or [control["catalog_chart_reference"]]
    scope = fit.pop("scope")
    fit["item_selectors"] = [{key: row[key] for key in ("asset_kind", "asset_id", "product_id", "role")} for row in fit.pop("items")]
    focus = fit.pop("focus")
    fit["focus_selector"] = {"product_id": focus["product_id"], "digest": _digest(focus)}
    return {"schema": "combined-catalog-assets.v1", "scope": scope, "fit": fit,
        "charts": [_chart_selector(reference) for reference in charts]}


def _restore_combined_reference(reference):
    from copy import deepcopy
    from management.models import IgCustomerTurnRevision
    from management.services.ig_product_focus import capture_product_focus
    if set(reference) != {"schema", "scope", "fit", "charts"} or reference["schema"] != "combined-catalog-assets.v1":
        raise ValueError("catalog_combined_asset_reference_invalid")
    fit = deepcopy(reference["fit"])
    fit["scope"] = deepcopy(reference["scope"])
    revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=fit["revision_id"], client_id=fit["scope"]["client_id"]).first()
    if revision is None:
        raise ValueError("fit_illustration_source_changed")
    focus = capture_product_focus(revision, source_cart_capture=(revision.generation_proposal or {}).get("source_cart_capture"))
    selector = fit.pop("focus_selector")
    if selector != {"product_id": focus.get("product_id"), "digest": _digest(focus)} or focus.get("status") != "focused":
        raise ValueError("fit_illustration_focus_changed")
    fit["focus"] = focus
    capability = capture_fit_photo_capability(focus["product_id"], metadata_only=True)
    if capability.get("error") or capability.get("capability_digest") != fit["capability_digest"]:
        raise ValueError("fit_illustration_capability_changed")
    selectors = fit.pop("item_selectors")
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 3:
        raise ValueError("catalog_combined_asset_reference_invalid")
    items = []
    for selector in selectors:
        if set(selector) != {"asset_kind", "asset_id", "product_id", "role"}:
            raise ValueError("catalog_combined_asset_reference_invalid")
        role = selector["role"]
        candidates = capability["photos"] if role == "focus" else capability["examples"] if role == "example" else capability.get("illustrations", []) if role == "blank" else []
        matches = [row for row in candidates if all(row.get(key) == selector[key] for key in ("asset_kind", "asset_id", "product_id"))]
        if len(matches) != 1:
            raise ValueError("fit_illustration_asset_changed")
        items.append({**{key: value for key, value in matches[0].items() if key != "url"}, "role": role})
    fit["items"] = items
    charts = _restore_charts(revision_id=fit["revision_id"], client_id=fit["scope"]["client_id"], settings_id=fit["scope"]["settings_id"], selectors=reference["charts"])
    control = {"fit_illustration_reference": fit, **({"catalog_chart_reference": charts[0]} if len(charts) == 1 else {"catalog_chart_references": charts})}
    if _compact_combined_reference(control) != reference:
        raise ValueError("catalog_combined_asset_reference_invalid")
    return control


def build_catalog_asset_reference_binding(client, control):
    """Rebuild one claim from exact DB rows without reading asset bytes."""
    from management.services.ig_revision_authority import _base_binding

    if "combined_asset_reference" in control:
        try:
            return build_catalog_asset_reference_binding(client, _restore_combined_reference(control["combined_asset_reference"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return None, str(exc) if isinstance(exc, ValueError) else "catalog_combined_asset_reference_invalid"

    if "catalog_chart_reference_set" in control:
        try:
            reference = control["catalog_chart_reference_set"]
            charts = _restore_charts(revision_id=reference["revision_id"], client_id=reference["client_id"],
                settings_id=reference["settings_id"], selectors=reference["charts"])
            return build_catalog_asset_reference_binding(client, {"catalog_chart_references": charts})
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return None, str(exc) if isinstance(exc, ValueError) else "catalog_combined_asset_reference_invalid"

    if "catalog_chart_references" in control or ("fit_illustration_reference" in control and "catalog_chart_reference" in control):
        try:
            charts = control.get("catalog_chart_references") or [control["catalog_chart_reference"]]
            if not isinstance(charts, list) or not 1 <= len(charts) <= 2:
                return None, "catalog_choice_chart_effect_invalid"
            fit = control.get("fit_illustration_reference")
            common = fit["scope"] if fit else charts[0]["scope"]
            revision_id = fit["revision_id"] if fit else charts[0]["invitation_id"]
            if any(reference["invitation_id"] != revision_id or (fit and reference["product_id"] != fit["focus"]["product_id"])
                or any(common.get(key) != reference["scope"].get(key) for key in ("client_id", "igsid", "namespace", "revision_snapshot_digest", "settings_id", "settings_epoch", "client_epoch", "publication_id", "publication_version", "publication_hash")) for reference in charts):
                return None, "catalog_combined_asset_scope_changed"
            if len({(row.get("configuration_selector") or {}).get("fit_code") for row in charts}) != len(charts):
                return None, "catalog_choice_chart_effect_invalid"
            bindings = []
            if fit:
                binding, reason = build_catalog_asset_reference_binding(client, {"fit_illustration_reference": fit})
                if reason:
                    return None, reason
                bindings.append(binding)
            for reference in charts:
                binding, reason = build_catalog_asset_reference_binding(client, {"catalog_chart_reference": reference})
                if reason:
                    return None, reason
                bindings.append(binding)
            selector = ({"combined_asset_reference": _compact_combined_reference(control)} if fit else {"catalog_chart_reference_set": {
                "schema": "chart-reference-set.v1", "revision_id": revision_id, "client_id": common["client_id"],
                "settings_id": common["settings_id"], "charts": [_chart_selector(row) for row in charts]}})
            subjects = [binding["subjects"] for binding in bindings]
            return _base_binding(CLAIM, client, None, selector, bindings,
                product_ids=sorted({pk for subject in subjects for pk in subject["product_ids"]}),
                part_count=sum(subject["part_count"] for subject in subjects),
                asset_reference_digest=_digest([subject["asset_reference_digest"] for subject in subjects])), ""
        except (KeyError, TypeError, ValueError):
            return None, "catalog_combined_asset_reference_invalid"

    if "fit_illustration_reference" in control:
        from management.models import InstagramBotSettings
        from management.services.ig_product_inquiry_presentation import validate_fit_illustration_reference

        reference = control["fit_illustration_reference"]
        try:
            settings_row = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(
                pk=reference["scope"]["settings_id"]).first()
            if settings_row is None:
                return None, "fit_illustration_settings_missing"
            reason = validate_fit_illustration_reference(reference, client, settings_row)
            if reason:
                return None, reason
            return _base_binding(CLAIM, client, None, {"fit_illustration_reference": reference}, reference,
                product_ids=sorted({row["product_id"] for row in reference["items"]}),
                part_count=len(reference["items"]), asset_reference_digest=reference["digest"]), ""
        except (KeyError, TypeError, ValueError):
            return None, "fit_illustration_reference_invalid"

    if "catalog_chart_reference" in control:
        from management.models import InstagramBotSettings
        from management.services.ig_catalog_choice_presentations import validate_catalog_chart_reference
        reference = control["catalog_chart_reference"]
        try:
            settings_row = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(pk=reference["scope"]["settings_id"]).first()
            if settings_row is None:
                return None, "catalog_choice_settings_missing"
            reason = validate_catalog_chart_reference(reference, client, settings_row)
            if reason:
                return None, reason
            return _base_binding(CLAIM, client, None, {"catalog_chart_reference": reference}, reference,
                product_ids=[reference["product_id"]], part_count=1,
                asset_reference_digest=reference["chart_digest"]), ""
        except (KeyError, TypeError, ValueError):
            return None, "catalog_choice_chart_invalid"

    if "catalog_choice_presentation" in control or "catalog_choice_reference" in control:
        from management.models import InstagramBotSettings
        from management.services.ig_catalog_choice_presentations import (
            SCHEMA_V1, build_catalog_choice_effects, catalog_choice_reference, current_catalog_choice_scope, validate_catalog_choice_assets,
        )

        try:
            if "catalog_choice_presentation" in control:
                manifest = control["catalog_choice_presentation"]
                rebuilt = build_catalog_choice_effects(manifest["invitation_id"], manifest["scope"], manifest["cards"],
                    presentation_kind=manifest["presentation_kind"], actions=manifest["actions"], _legacy=manifest["schema"] == SCHEMA_V1,
                    _schema=manifest["schema"])
                if rebuilt.manifest != manifest:
                    return None, "catalog_choice_manifest_changed"
                reference = catalog_choice_reference(manifest)
            else:
                reference = control["catalog_choice_reference"]
            settings_row = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(pk=reference["scope"]["settings_id"]).first()
            if settings_row is None:
                return None, "catalog_choice_settings_missing"
            reason = current_catalog_choice_scope(reference, client, settings_row) or validate_catalog_choice_assets(reference, client)
            if reason:
                return None, reason
            return _base_binding(CLAIM, client, None, {"catalog_choice_reference": reference}, reference,
                product_ids=[row["product_id"] for row in reference["cards"]], part_count=1,
                asset_reference_digest=_digest([row["asset_ref"] for row in reference["cards"]])), ""
        except (KeyError, TypeError, ValueError):
            return None, "catalog_choice_manifest_invalid"

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
