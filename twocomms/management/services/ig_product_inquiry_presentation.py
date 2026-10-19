"""Fit illustrations retain the discussed print and grant no product choice."""
from copy import deepcopy
from dataclasses import dataclass

from django.utils.crypto import constant_time_compare, salted_hmac

from management.services.ig_revision_outbox import _digest

SCHEMA = "fit-illustration.v1"
_LABELS = {
    "uk": {"classic": "Класичний крій", "oversize": "Оверсайз", "unknown": "Фото принта",
        "focus": "Обговорюваний принт", "example": "Інший принт — лише приклад посадки", "blank": "Чиста основа: preview конструктора, без обговорюваного принта"},
    "ru": {"classic": "Классический крой", "oversize": "Оверсайз", "unknown": "Фото принта",
        "focus": "Обсуждаемый принт", "example": "Другой принт — только пример посадки", "blank": "Чистая основа: preview конструктора, без обсуждаемого принта"},
    "en": {"classic": "Classic fit", "oversize": "Oversize", "unknown": "Print photo",
        "focus": "The discussed print", "example": "Another print, only an example of the fit", "blank": "Blank base: constructor preview, without the discussed print"},
}


@dataclass(frozen=True)
class FitIllustrationPreparation:
    handled: bool = False
    effects: tuple = ()
    copy_hint: str = ""
    reasons: tuple = ()

    @property
    def ready(self):
        return self.handled and bool(self.effects) and not self.reasons


def _caption(item, locale):
    words = _LABELS[locale]
    label = words.get(item["fit_code"], words["unknown"])
    if item.get("view"):
        label += " · " + {"uk": {"front": "Спереду", "back": "Ззаду"},
            "ru": {"front": "Спереди", "back": "Сзади"}, "en": {"front": "Front", "back": "Back"}}[locale].get(item["view"], "")
    return label, words[item["role"]]


def _payloads(manifest):
    native = {"recipient": {"id": manifest["scope"]["igsid"]}, "message": {"attachment": {
        "type": "template", "payload": {"template_type": "generic", "elements": [
            {"title": _caption(row, manifest["locale"])[0],
             "subtitle": _caption(row, manifest["locale"])[1], "image_url": row["url"]}
            for row in manifest["items"]]}}}}
    images = [{"recipient": {"id": manifest["scope"]["igsid"]}, "message": {"attachment": {
        "type": "image", "payload": {"url": row["url"], "is_reusable": True}}}}
        for row in manifest["items"]]
    labels = "\n".join(f"{index + 1}. " + " — ".join(_caption(row, manifest["locale"]))
        for index, row in enumerate(manifest["items"]))
    # The same fallback group requires this disclosure SENT before any photo.
    # A crash/UNKNOWN preface can therefore never expose an unlabeled foreign
    # design while its explanation is still undelivered.
    fallback = [{"recipient": {"id": manifest["scope"]["igsid"]}, "message": {"text": labels}}, *images]
    if manifest["scope"]["namespace"].startswith("legacy_page:"):
        native["messaging_type"] = "RESPONSE"
        for row in fallback:
            row["messaging_type"] = "RESPONSE"
    return native, fallback


def _manifest_digest(manifest):
    return _digest({key: value for key, value in manifest.items() if key not in {"digest", "hmac"}})


def _seal_manifest(manifest):
    native, fallback = _payloads(manifest)
    manifest["native_payload_digest"] = _digest(native)
    manifest["fallback_payload_digests"] = [_digest(row) for row in fallback]
    manifest["digest"] = _manifest_digest(manifest)
    manifest["hmac"] = salted_hmac(SCHEMA, manifest["digest"], algorithm="sha256").hexdigest()
    return manifest


def _effects(manifest):
    native, fallback = _payloads(manifest)
    rows = [("native", "template", "template", manifest.get("native_part_index", 0), native)]
    rows.extend(("fallback", "template_fallback", "fallback" if index == 0 else "image", index, payload)
        for index, payload in enumerate(fallback))
    result = []
    for delivery, group, kind, index, payload in rows:
        row = {"group": group, "kind": kind, "payload": payload, "projection_metadata": {
            "fit_illustration": deepcopy(manifest), "delivery": delivery, "part_index": index,
            "prepared_payload_digest": _digest(payload)}}
        if delivery == "fallback":
            row["activation"] = {"group": "template", "part_index": manifest.get("native_part_index", 0), "failure_code": "provider_rejected"}
        result.append(row)
    return tuple(result)


def _photo_field(photo):
    from productcolors.models import ProductColorImage
    from storefront.models import Product, ProductImage

    kind = photo["asset_kind"]
    if kind == "variant_image":
        row = ProductColorImage.objects.filter(pk=photo["asset_id"], variant_id=photo["variant_id"],
            variant__product_id=photo["product_id"]).first()
        return row.image if row else None
    if kind == "product_image":
        row = ProductImage.objects.filter(pk=photo["asset_id"], product_id=photo["product_id"]).first()
        return row.image if row else None
    if kind in {"main_image", "home_card_image"} and photo["asset_id"] == photo["product_id"]:
        row = Product.objects.filter(pk=photo["product_id"]).first()
        return getattr(row, kind) if row else None
    return None


def _copy(context, locale, items):
    missing = (context.get("fit_photo_capability") or {}).get("missing_fit_photos") or []
    names = _LABELS[locale]
    title = str(context["focus"]["title"])
    messages = []
    for fit in missing:
        label = names[fit]
        messages.append({
            "uk": f"Для принта «{title}» передбачено {label.lower()}, але окремого підтвердженого фото цього крою немає.",
            "ru": f"Для принта «{title}» предусмотрен {label.lower()}, но отдельного подтверждённого фото этого кроя нет.",
            "en": f"The discussed print “{title}” has a {label.lower()} option, but no confirmed separate photo of that cut.",
        }[locale])
    if any(row["role"] == "example" for row in items):
        messages.append({
            "uk": "Інший принт на картці — лише приклад посадки для порівняння. Обговорюваний принт залишається тим самим.",
            "ru": "Другой принт на карточке — только пример посадки для сравнения. Обсуждаемый принт остаётся прежним.",
            "en": "The other print on the card only illustrates the fit for comparison. The discussed print stays the same.",
        }[locale])
    if any(row["role"] == "blank" for row in items):
        messages.append({"uk": "Це чиста основа з preview конструктора — приклад крою, а не фото обговорюваного принта чи індивідуального заміру.",
            "ru": "Это чистая основа из preview конструктора — пример кроя, а не фото обсуждаемого принта или индивидуального замера.",
            "en": "This blank base is a constructor preview illustrating the cut, not a photo of the discussed print or an individual measurement."}[locale])
    return " ".join(messages)


def _chart_fits_unavailable(product_id, fits):
    from management.services.ig_catalog_configured_choices import capture_configured_choices
    if not isinstance(fits, (list, tuple)) or not 1 <= len(fits) <= 2 or any(fit not in {"classic", "oversize"} for fit in fits):
        return False
    for fit in fits:
        captured = capture_configured_choices(product_id, fit_code=fit, metadata_only=True)
        grid = captured.get("grid") or {}
        if grid.get("grid_id") and grid.get("image_name") and len(captured.get("configurations") or []) == 1:
            return False
    return True


def prepare_fit_illustration(revision, response, settings_row, *, context=None, native_part_index=0, unavailable_chart_fits=()):
    """Use actual focused assets or explicitly labeled native fit examples."""
    if type(native_part_index) is not int or not 0 <= native_part_index <= 16:
        return FitIllustrationPreparation(True, reasons=("fit_illustration_native_slot_invalid",))
    if response.control.get("paylink") or response.control.get("order"):
        return FitIllustrationPreparation()
    if context is None:
        from management.services.ig_fit_consultation import capture_fit_consultation
        context = capture_fit_consultation(revision)
    if not context or not context.get("photos_requested") or not context.get("focus") or context.get("assortment_requested"):
        return FitIllustrationPreparation()
    from management.services.ig_catalog_choice_presentations import _presentation_locale
    from management.services.ig_catalog_configured_choices import _photo_ref
    from management.services.ig_revision_catalog_assets import capture_fit_photo_capability

    capability = capture_fit_photo_capability(context["focus"]["product_id"], metadata_only=True)
    if capability.get("error") or capability.get("capability_digest") != (context.get("fit_photo_capability") or {}).get("capability_digest"):
        return FitIllustrationPreparation(True, reasons=("fit_illustration_capability_changed",))
    if unavailable_chart_fits:
        from management.services.ig_fit_consultation import requested_chart_fits
        requested = requested_chart_fits((revision.bundle_snapshot or {}).get("sources") or [], capability.get("supported_fits", ()))
        if (not isinstance(unavailable_chart_fits, (list, tuple)) or not 1 <= len(unavailable_chart_fits) <= 2
            or not set(unavailable_chart_fits).issubset(requested) or not _chart_fits_unavailable(context["focus"]["product_id"], unavailable_chart_fits)):
            return FitIllustrationPreparation(True, reasons=("fit_illustration_chart_absence_changed",))
    locale = _presentation_locale(revision, response)
    selected = []
    def usable_photo(row):
        if row.get("asset_kind") == "blank_constructor_preview":
            from management.services.ig_fit_illustration_assets import blank_illustration_available
            return blank_illustration_available(row)
        actual = _photo_ref(_photo_field(row), product_id=row["product_id"], variant_id=row["variant_id"] or None)
        return bool(actual and actual["url"] == row["url"])

    fits = context.get("requested_fits") or capability["supported_fits"]
    for fit in fits:
        candidates = [(row, "focus") for row in capability["photos"] if row["fit_code"] == fit]
        candidates += [(row, "example") for row in capability["examples"] if row["fit_code"] == fit]
        candidates += [(row, "blank") for row in capability.get("illustrations", []) if row["fit_code"] == fit]
        for row, role in candidates:
            if usable_photo(row):
                selected.append({**row, "role": role})
                break
    if not any(row["role"] == "focus" for row in selected) and capability["photos"]:
        fallback_photo = next((row for row in capability["photos"] if usable_photo(row)), None)
        if fallback_photo:
            selected.insert(0, {**fallback_photo, "role": "focus"})
    # One base request can show both approved constructor views. For a
    # comparison, preserve one illustration per requested cut first.
    if len(fits) == 1 and any(row["role"] == "blank" for row in selected):
        selected += [{**row, "role": "blank"} for row in capability.get("illustrations", [])
            if row["fit_code"] == fits[0] and row not in [{k: v for k, v in chosen.items() if k != "role"} for chosen in selected]
            and usable_photo(row)]
    usable = selected[:3]
    if not usable:
        return FitIllustrationPreparation(True, reasons=("fit_illustration_photo_unavailable",))
    namespaces = {row.get("source_namespace") for row in revision.bundle_snapshot.get("sources", ())}
    if len(namespaces) != 1 or not next(iter(namespaces)):
        return FitIllustrationPreparation(True, reasons=("fit_illustration_namespace_invalid",))
    publication = settings_row.active_instruction_publication
    manifest = {"schema": SCHEMA, "revision_id": revision.pk, "focus": deepcopy(context["focus"]),
        "capability_digest": capability["capability_digest"], "locale": locale, "items": usable,
        "scope": {"client_id": revision.client_id, "igsid": revision.client.igsid,
            "revision_snapshot_digest": revision.snapshot_digest, "namespace": next(iter(namespaces)),
            "settings_id": settings_row.pk, "settings_epoch": settings_row.reply_permission_epoch,
            "client_epoch": revision.permission_epoch, "publication_id": publication.pk,
            "publication_version": publication.version, "publication_hash": publication.snapshot_hash}}
    if native_part_index:
        manifest["native_part_index"] = native_part_index
    if unavailable_chart_fits:
        manifest["unavailable_chart_fits"] = list(unavailable_chart_fits)
    _seal_manifest(manifest)
    return FitIllustrationPreparation(True, _effects(manifest), _copy(context, locale, usable))


def validate_fit_illustration_metadata(metadata, *, payload, group, kind):
    try:
        if set(metadata) != {"fit_illustration", "delivery", "part_index", "prepared_payload_digest"}:
            return False
        manifest = metadata["fit_illustration"]
        if (manifest["schema"] != SCHEMA or not 1 <= len(manifest["items"]) <= 3
            or manifest["locale"] not in _LABELS or _manifest_digest(manifest) != manifest["digest"]
            or not constant_time_compare(manifest["hmac"], salted_hmac(SCHEMA, manifest["digest"], algorithm="sha256").hexdigest())):
            return False
        native, fallback = _payloads(manifest)
        if (_digest(native) != manifest["native_payload_digest"]
            or [_digest(row) for row in fallback] != manifest["fallback_payload_digests"]):
            return False
        index = metadata["part_index"]
        if type(index) is not int or index < 0:
            return False
        if metadata["delivery"] == "native":
            expected, expected_group, expected_kind = native, "template", "template"
            if type(manifest.get("native_part_index", 0)) is not int or not 0 <= manifest.get("native_part_index", 0) <= 16 or index != manifest.get("native_part_index", 0):
                return False
        elif metadata["delivery"] == "fallback" and index < len(fallback):
            expected, expected_group = fallback[index], "template_fallback"
            expected_kind = "fallback" if index == 0 else "image"
        else:
            return False
        return (group, kind) == (expected_group, expected_kind) and payload == expected and _digest(payload) == metadata["prepared_payload_digest"]
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def fit_illustration_reference(manifest):
    reference = deepcopy(manifest)
    for row in reference["items"]:
        row.pop("url", None)
    return reference


def validate_fit_illustration_reference(reference, client, settings_row):
    """DB-only replay of native fit/asset ownership at every final send CAS."""
    from management.models import IgCustomerTurnRevision
    from management.services.ig_product_focus import capture_product_focus
    from management.services.ig_revision_catalog_assets import capture_fit_photo_capability

    try:
        scope = reference["scope"]
        publication = settings_row.active_instruction_publication
        if (scope["client_id"] != client.pk or scope["igsid"] != client.igsid
            or scope["client_epoch"] != client.reply_permission_epoch or scope["settings_id"] != settings_row.pk
            or scope["settings_epoch"] != settings_row.reply_permission_epoch
            or (scope["publication_id"], scope["publication_version"], scope["publication_hash"]) !=
               (publication.pk, publication.version, publication.snapshot_hash)):
            return "fit_illustration_scope_changed"
        revision = IgCustomerTurnRevision.objects.select_related("client").filter(pk=reference["revision_id"], client=client).first()
        if revision is None or revision.snapshot_digest != scope["revision_snapshot_digest"]:
            return "fit_illustration_source_changed"
        if capture_product_focus(revision,
            source_cart_capture=(revision.generation_proposal or {}).get("source_cart_capture")) != reference["focus"]:
            return "fit_illustration_focus_changed"
        capability = capture_fit_photo_capability(reference["focus"]["product_id"], metadata_only=True)
        if capability.get("error") or capability["capability_digest"] != reference["capability_digest"]:
            return "fit_illustration_capability_changed"
        if reference.get("unavailable_chart_fits") and not _chart_fits_unavailable(reference["focus"]["product_id"], reference["unavailable_chart_fits"]):
            return "fit_illustration_chart_absence_changed"
        restored = deepcopy(reference)
        for row in restored["items"]:
            role = row["role"]
            candidates = capability["photos"] if role == "focus" else capability["examples"] if role == "example" else capability.get("illustrations", []) if role == "blank" else []
            matches = [photo for photo in candidates if {key: value for key, value in photo.items() if key != "url"}
                       == {key: value for key, value in row.items() if key != "role"}]
            if len(matches) != 1:
                return "fit_illustration_asset_changed"
            row["url"] = matches[0]["url"]
        native, _fallback = _payloads(restored)
        metadata = {"fit_illustration": restored, "delivery": "native", "part_index": restored.get("native_part_index", 0),
                    "prepared_payload_digest": _digest(native)}
        if not validate_fit_illustration_metadata(metadata, payload=native, group="template", kind="template"):
            return "fit_illustration_manifest_changed"
        return ""
    except (KeyError, TypeError, ValueError, AttributeError):
        return "fit_illustration_reference_invalid"
