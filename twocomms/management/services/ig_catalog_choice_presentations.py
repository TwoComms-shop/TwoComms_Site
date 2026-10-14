"""Signed catalog choices carried by existing immutable delivery effects.

Preparation is read-only. A payload is authority only when its exact native or
conditional fallback effect has a confirmed provider receipt. Photographs and
configuration rules establish presentation identity, never stock or price.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from collections.abc import Mapping

from django.utils.crypto import constant_time_compare, salted_hmac

from management.services.ig_revision_outbox import _digest
from management.services.ig_revision_catalog_assets import _Unavailable

SCHEMA_V1 = "catalog-choice-presentation.v1"
SCHEMA = "catalog-choice-presentation.v2"
PREFIX = "CC1:"
_PAYLOAD = re.compile(r"CC1:([1-9][0-9]{0,17}):([0-9]{1,2}):([0-9a-f]{32})")
MAX_CARDS = 3
_MANIFEST_KEYS = frozenset({"schema", "invitation_id", "scope", "cards", "actions",
    "presentation_kind", "context_digest", "native_payload_digest", "fallback_payload_digests", "manifest_digest"})
_V1_MANIFEST_KEYS = (_MANIFEST_KEYS - {"fallback_payload_digests"}) | {"fallback_payload_digest"}
_LABELS = {
    "uk": ("Обрати", "Таблиця розмірів", "Оберіть товар:", "Обрати {number}"),
    "ru": ("Выбрать", "Таблица размеров", "Выберите товар:", "Выбрать {number}"),
    "en": ("Choose", "Size chart", "Choose a product:", "Choose {number}"),
}


@dataclass(frozen=True)
class CatalogChoicePreparation:
    handled: bool = False
    effects: tuple = ()
    copy_hint: str = ""
    reasons: tuple = ()
    manifest: dict | None = None

    @property
    def ready(self):
        return self.handled and bool(self.effects) and not self.reasons


def _signature(invitation_id, index, context_digest, *, schema=SCHEMA):
    return salted_hmac(schema, f"{invitation_id}:{index}:{context_digest}",
        algorithm="sha256").hexdigest()[:32]


def _context(manifest):
    return {key: manifest[key] for key in ("schema", "invitation_id", "scope", "cards", "actions", "presentation_kind")}


def _source_configuration(client, product_id):
    """Read accepted active-line wishes; no nonexistent legacy fit/defaults."""
    from management.services.ig_commerce_projection import source_preferences_for

    if getattr(client, "current_product_id", None) != product_id:
        return {"option_values": None, "fit_code": None}
    preferences = source_preferences_for(client)
    values, evidence = preferences.get("values") or {}, preferences.get("evidence") or {}
    if values.get("product_id") != product_id or "product_id" not in evidence:
        return {"option_values": None, "fit_code": None}
    fit = values.get("fit_option_code") if "fit_option_code" in evidence else None
    return {"option_values": {"fit": fit} if fit else None, "fit_code": fit or None}


def _payload_kind(payload):
    message = payload.get("message") or {}
    if "text" in message:
        return "text"
    return "image" if (message.get("attachment") or {}).get("type") == "image" else "template"


def _scope(revision, settings_row, locale):
    from management.models import IgCommerceSelectionSession
    from management.services.ig_conversation_routes import conversation_route_reset_floor

    client = revision.client
    session = IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1,
        commercial_episode_id=client.current_commercial_episode_id).first()
    if client.privacy_erasure_started_at is not None:
        raise ValueError("catalog_choice_privacy_unavailable")
    sources = (revision.bundle_snapshot or {}).get("sources") or []
    namespaces = {row.get("source_namespace") for row in sources}
    if len(namespaces) != 1 or not next(iter(namespaces), None):
        raise ValueError("catalog_choice_namespace_invalid")
    lines = (session.lines or []) if session is not None else []
    index = int(session.active_index or 0) if session is not None else 0
    line = lines[index] if 0 <= index < len(lines) and isinstance(lines[index], dict) else {}
    publication = getattr(settings_row, "active_instruction_publication", None)
    if publication is None:
        raise ValueError("catalog_choice_publication_unavailable")
    return {"client_id": client.pk, "igsid": client.igsid,
        "episode_id": client.current_commercial_episode_id, "session_id": session.pk if session else None,
        "generation": session.generation if session else 0, "selection_revision": session.revision if session else 0,
        "candidate_generation": session.candidate_generation if session else 0,
        "line_id": str(line.get("line_id") or ""), "recipient_id": str(line.get("recipient_id") or "self"),
        "selection_digest": _digest({"lines": lines, "active_index": index}),
        "reset_floor": conversation_route_reset_floor(client.pk),
        "namespace": next(iter(namespaces)), "revision_snapshot_digest": revision.snapshot_digest,
        "source_message_ids": [row["message_id"] for row in sources],
        "settings_id": settings_row.pk, "settings_epoch": settings_row.reply_permission_epoch,
        "client_epoch": client.reply_permission_epoch,
        "publication_id": publication.pk, "publication_version": publication.version,
        "publication_hash": publication.snapshot_hash, "locale": locale, "privacy_clear": True}


def _configured_card(client, product_id, capture=None, *, require_photo=True):
    from management.services.ig_catalog_configured_choices import capture_configured_choices
    from management.services import ig_revision_catalog_assets as assets

    selected = assets._selected_variant(client, (product_id,))
    selector = {"variant_id": selected or None, **_source_configuration(client, product_id)}
    captured = deepcopy(capture) if isinstance(capture, Mapping) else capture_configured_choices(product_id, **selector)
    if not isinstance(captured, dict) or captured.get("product_id") != product_id:
        raise ValueError("catalog_choice_configuration_unavailable")
    if require_photo and selected and captured.get("variant_id") != selected:
        raise ValueError("catalog_choice_selected_variant_mismatch")
    # The capture owns selectors. Never default to a different fit or colour.
    selector = {"variant_id": captured.get("variant_id") or None,
        "option_values": captured.get("option_values") or None, "fit_code": captured.get("fit_code") or None}
    photo = captured.get("photo_ref") or {}
    if require_photo and not photo.get("url"):
        raise ValueError("catalog_choice_photo_unavailable")
    product = assets._product(product_id)
    photo_variant = int(photo.get("variant_id") or 0)
    reference = None
    if photo.get("url"):
        asset = assets._find_asset(product, assets._name_for_url(photo["url"]), photo_variant, allow_generic=not selector["variant_id"])
        reference, _payload, _projection = assets._descriptor(client, product, asset, 0, False)
        if reference["url_digest"] != _digest(photo["url"]):
            raise ValueError("catalog_choice_photo_changed")
    return {"product_id": product_id, "title": str(product.title or "TwoComms")[:200],
        "image_url": photo.get("url") or "", "asset_ref": reference,
        "asset_selected_variant_id": photo_variant, "configuration_selector": selector,
        "configuration_digest": captured.get("configuration_digest") or ""}


def build_catalog_choice_effects(invitation_id, scope, cards, *, presentation_kind="products", actions=None, _legacy=False):
    """Pure builder used by the producer and causal tests; no source admission."""
    from management.services import ig_message_templates as tpl

    cards = deepcopy(list(cards))
    if not 1 <= len(cards) <= MAX_CARDS or len({row["product_id"] for row in cards}) != len(cards):
        raise ValueError("catalog_choice_cardinality_invalid")
    locale = scope.get("locale") if scope.get("locale") in _LABELS else "uk"
    choose, chart, head, numbered = _LABELS[locale]
    if presentation_kind not in {"products", "color", "size"}:
        raise ValueError("catalog_choice_presentation_kind_invalid")
    if presentation_kind == "products":
        actions = []
        for card in cards:
            actions.extend(({"kind": "product", "product_id": card["product_id"], "value": card["product_id"],
                "label": choose}, {"kind": "chart", "product_id": card["product_id"], "label": chart}))
        ordered = scope.get("ordered_product_ids") or [row["product_id"] for row in cards]
        page = int(scope.get("page") or 0)
        if not 0 <= page <= 3 or ordered[page * MAX_CARDS:(page + 1) * MAX_CARDS] != [row["product_id"] for row in cards]:
            raise ValueError("catalog_choice_page_order_invalid")
        if (page + 1) * MAX_CARDS < len(ordered):
            more = {"uk": "Показати ще", "ru": "Показать ещё", "en": "Show more"}[locale]
            actions.append({"kind": "page", "product_id": cards[0]["product_id"], "page": page + 1,
                "value": page + 1, "label": more})
    else:
        actions = deepcopy(list(actions or []))
        if len(cards) != 1 or not 1 <= len(actions) <= 13:
            raise ValueError("catalog_choice_configuration_bound")
        if any(row.get("kind") not in {presentation_kind, "chart"}
            or row.get("product_id") != cards[0]["product_id"] or not row.get("label") for row in actions):
            raise ValueError("catalog_choice_configuration_action_invalid")
    schema = SCHEMA_V1 if _legacy else SCHEMA
    manifest = {"schema": schema, "invitation_id": int(invitation_id), "scope": deepcopy(scope),
        "cards": cards, "actions": actions, "presentation_kind": presentation_kind}
    manifest["context_digest"] = _digest(_context(manifest))
    payloads = [f"{PREFIX}{invitation_id}:{index}:{_signature(invitation_id, index, manifest['context_digest'], schema=schema)}"
        for index in range(len(actions))]
    if presentation_kind == "products":
        elements = tuple(tpl.TemplateCard(title=tpl.display_short(row["title"]), image_url=row["image_url"],
            buttons=(tpl.TemplateButton(tpl.BUTTON_POSTBACK, choose, payload=payloads[index * 2]),
                     tpl.TemplateButton(tpl.BUTTON_POSTBACK, chart, payload=payloads[index * 2 + 1])))
            for index, row in enumerate(cards))
        text = head + "\n" + "\n".join(f"{index + 1}. {tpl.display_short(row['title'])}" for index, row in enumerate(cards))
        more = tuple(tpl.QuickReply(action["label"], payloads[index])
            for index, action in enumerate(actions) if action["kind"] == "page")
        normalized = tpl.normalize_template(tpl.GenericTemplate(cards=elements, fallback_text=text, quick_replies=more))
        if set(normalized.degraded_fields) - {"title_truncated"}:
            raise ValueError("catalog_choice_template_degraded")
        message = tpl.template_message_payload(normalized)
        quick = tuple(tpl.QuickReply((numbered.format(number=index // 2 + 1)
            if action["kind"] == "product" else f"{chart} {index // 2 + 1}" if action["kind"] == "chart" else action["label"]), payloads[index])
            for index, action in enumerate(actions))
    else:
        heads = {"color": {"uk": "Оберіть колір:", "ru": "Выберите цвет:", "en": "Choose a colour:"},
            "size": {"uk": "Оберіть розмір:", "ru": "Выберите размер:", "en": "Choose a size:"}}
        text = tpl.display_short(cards[0]["title"]) + "\n" + heads[presentation_kind][locale]
        quick = tuple(tpl.QuickReply(tpl.display_short(row["label"])[:20], payloads[index])
            for index, row in enumerate(actions))
        if len(actions) <= 3:
            message = tpl.button_template_message_payload(tpl.normalize_button_template(tpl.ButtonTemplate(
                text=text, buttons=tuple(tpl.TemplateButton(tpl.BUTTON_POSTBACK, row["label"], payload=payloads[index])
                    for index, row in enumerate(actions)), fallback_text=text)))
        else:
            message = tpl.quick_reply_message_payload(tpl.normalize_quick_reply_message(tpl.QuickReplyMessage(text, quick)))
    native = {"recipient": {"id": scope["igsid"]}, "message": message}
    fallback_text = {"recipient": {"id": scope["igsid"]},
        "message": tpl.quick_reply_message_payload(tpl.normalize_quick_reply_message(tpl.QuickReplyMessage(text, quick)))}
    fallbacks = []
    if presentation_kind == "products" and not _legacy:
        for card in cards:
            fallbacks.append({"recipient": {"id": scope["igsid"]}, "message": {"attachment": {
                "type": "image", "payload": {"url": card["image_url"], "is_reusable": True}}}})
    fallbacks.append(fallback_text)
    if scope["namespace"].startswith("legacy_page:"):
        native["messaging_type"] = "RESPONSE"
        for payload in fallbacks:
            payload["messaging_type"] = "RESPONSE"
    manifest["native_payload_digest"] = _digest(native)
    if _legacy:
        manifest["fallback_payload_digest"] = _digest(fallback_text)
    else:
        manifest["fallback_payload_digests"] = [_digest(payload) for payload in fallbacks]
    manifest["manifest_digest"] = _digest(manifest)
    effects = []
    parts = [("native", "template", "template" if _legacy else _payload_kind(native), 0, native)]
    parts.extend(("fallback", "template_fallback", "image" if _payload_kind(payload) == "image" else "fallback",
        index, payload) for index, payload in enumerate(fallbacks))
    for delivery, group, kind, part_index, payload in parts:
        row = {"group": group, "kind": kind, "payload": payload,
            "projection_metadata": {"catalog_choice": deepcopy(manifest), "delivery": delivery,
                "prepared_payload_digest": _digest(payload)}}
        if not _legacy:
            row["projection_metadata"]["part_index"] = part_index
        if delivery == "fallback":
            row["activation"] = {"group": "template", "part_index": 0, "failure_code": "provider_rejected"}
        effects.append(row)
    return CatalogChoicePreparation(True, tuple(effects), "", (), manifest)


def prepare_catalog_choice_presentations(revision, response, settings_row, *, configured_choices_capture=None):
    """Replace SHOW_PRODUCTS only for ordinary presentation, preserving checkout."""
    from management.services.ig_catalog_media import parse_product_ids

    control = response.control
    if control.get("paylink") or control.get("order") or "show_products" not in control:
        return CatalogChoicePreparation()
    try:
        ids = parse_product_ids(control["show_products"])
        if not ids:
            raise ValueError("catalog_choice_products_invalid")
        # One bounded first page. Its remainder is explicit in the result;
        # a later page must be a separately sealed presentation revision.
        page = tuple(dict.fromkeys(ids))[:MAX_CARDS]
        captures = configured_choices_capture or {}
        locale = str(getattr(response, "reply_language", "") or getattr(revision.client, "language", "") or "uk")
        if locale == "ua":
            locale = "uk"
        scope = _scope(revision, settings_row, locale)
        scope["ordered_product_ids"] = list(dict.fromkeys(ids))
        scope["page"] = 0
        cards = [_configured_card(revision.client, product_id,
            captures.get(product_id) or captures.get(str(product_id))) for product_id in page]
        return build_catalog_choice_effects(revision.pk, scope, cards)
    except (ValueError, TypeError, KeyError, _Unavailable) as exc:
        return CatalogChoicePreparation(True, reasons=(str(exc),))


def prepare_catalog_configuration_choices(revision, response, settings_row, *, kind, capture=None):
    """Caller owns when to ask. Expose enabled configured rules without stock claims."""
    from management.services.ig_catalog_configured_choices import capture_configured_choices
    from management.services import ig_revision_catalog_assets as assets

    if kind not in {"color", "size"} or response.control.get("paylink") or response.control.get("order"):
        return CatalogChoicePreparation()
    try:
        product_id = int(revision.client.current_product_id or 0)
        if product_id <= 0:
            return CatalogChoicePreparation()
        selected = assets._selected_variant(revision.client, (product_id,)) if kind == "size" else None
        captured = deepcopy(capture) if isinstance(capture, dict) else capture_configured_choices(product_id,
            variant_id=selected or None, **_source_configuration(revision.client, product_id))
        rows = captured.get("colors") if kind == "color" else captured.get("sizes")
        if not rows or len(rows) > 13:
            return CatalogChoicePreparation()
        locale = str(getattr(response, "reply_language", "") or revision.client.language or "uk")
        locale = "uk" if locale == "ua" else locale
        scope = _scope(revision, settings_row, locale)
        actions = []
        for row in rows:
            if kind == "color":
                action = {"kind": kind, "product_id": product_id, "variant_id": row["variant_id"],
                    "value": row["variant_id"], "label": row["canonical_name"]}
            else:
                action = {"kind": kind, "product_id": product_id, "size": row["value"],
                    "value": row["value"], "label": row["value"]}
            actions.append(action)
        if kind == "size" and len(actions) < 13:
            actions.append({"kind": "chart", "product_id": product_id,
                "label": _LABELS.get(locale, _LABELS["uk"])[1]})
        card = _configured_card(revision.client, product_id, captured, require_photo=False)
        return build_catalog_choice_effects(revision.pk, scope, [card], presentation_kind=kind, actions=actions)
    except (ValueError, KeyError, TypeError, _Unavailable) as exc:
        return CatalogChoicePreparation(True, reasons=(str(exc),))


def prepare_catalog_choice_page(revision, response, settings_row, *, choice, configured_choices_capture=None):
    """More uses its delivered ordered remainder, never a new model ranking."""
    try:
        original = choice["manifest"]
        action = choice["action"]
        page = action["page"]
        if not choice.get("ready") or action["kind"] != "page" or page != int(original["scope"].get("page") or 0) + 1:
            raise ValueError("catalog_choice_page_invalid")
        ordered = original["scope"]["ordered_product_ids"]
        window = ordered[page * MAX_CARDS:(page + 1) * MAX_CARDS]
        if not window:
            raise ValueError("catalog_choice_page_exhausted")
        scope = _scope(revision, settings_row, original["scope"]["locale"])
        scope.update(ordered_product_ids=list(ordered), page=page,
            origin_manifest_digest=choice["manifest_digest"], origin_invitation_id=choice["invitation_id"],
            origin_effect_id=choice["effect_id"])
        captures = configured_choices_capture or {}
        cards = [_configured_card(revision.client, product_id,
            captures.get(product_id) or captures.get(str(product_id))) for product_id in window]
        return build_catalog_choice_effects(revision.pk, scope, cards)
    except (KeyError, ValueError, TypeError, _Unavailable) as exc:
        return CatalogChoicePreparation(True, reasons=(str(exc),))


def prepare_catalog_chart_effect(revision, response, settings_row, *, capture=None, choice=None):
    """A genuine exact-configuration chart image, with no product-photo projection."""
    from management.services.ig_catalog_configured_choices import capture_configured_choices
    from management.services import ig_revision_catalog_assets as assets
    from storefront.models import SizeGrid

    if response.control.get("paylink") or response.control.get("order"):
        return CatalogChoicePreparation()
    try:
        if choice and (not choice.get("ready") or choice.get("action", {}).get("kind") != "chart"):
            raise ValueError("catalog_choice_chart_source_invalid")
        product_id = int((choice or {}).get("action", {}).get("product_id") or revision.client.current_product_id or 0)
        selected = assets._selected_variant(revision.client, (product_id,))
        captured = deepcopy(capture) if isinstance(capture, dict) else capture_configured_choices(product_id,
            variant_id=selected or None, **_source_configuration(revision.client, product_id))
        grid = captured.get("grid") or {}
        photo = grid.get("photo_ref") or {}
        if not grid.get("grid_id") or not photo.get("url") or len(captured.get("configurations") or []) != 1:
            return CatalogChoicePreparation()
        row = SizeGrid.objects.filter(pk=grid["grid_id"]).first()
        if row is None or str(row.image.name or "") != grid.get("image_name") or assets._url(row.image) != photo["url"]:
            raise ValueError("catalog_choice_chart_changed")
        locale = str(getattr(response, "reply_language", "") or revision.client.language or "uk")
        scope = _scope(revision, settings_row, "uk" if locale == "ua" else locale)
        if choice:
            scope.update(origin_manifest_digest=choice["manifest_digest"],
                origin_invitation_id=choice["invitation_id"], origin_effect_id=choice["effect_id"])
        payload = {"recipient": {"id": scope["igsid"]}, "message": {
            "attachment": {"type": "image", "payload": {"url": photo["url"], "is_reusable": True}}}}
        if scope["namespace"].startswith("legacy_page:"):
            payload["messaging_type"] = "RESPONSE"
        manifest = {"schema": "catalog-choice-chart.v1", "invitation_id": revision.pk, "scope": scope,
            "product_id": product_id, "configuration_selector": {"variant_id": captured.get("variant_id") or None,
                "option_values": captured.get("option_values") or None, "fit_code": captured.get("fit_code") or None},
            "configuration_digest": captured["configuration_digest"], "grid_id": grid["grid_id"],
            "grid_source": grid["source"], "image_name_digest": _digest(grid["image_name"]),
            "image_url": photo["url"], "url_digest": _digest(photo["url"]), "payload_digest": _digest(payload),
            "origin_manifest_digest": (choice or {}).get("manifest_digest", "")}
        manifest["chart_digest"] = _digest(manifest)
        manifest["hmac"] = salted_hmac("catalog-choice-chart.v1", manifest["chart_digest"], algorithm="sha256").hexdigest()
        return CatalogChoicePreparation(True, ({"group": "template", "kind": "image", "payload": payload,
            "projection_metadata": {"catalog_chart": manifest, "prepared_payload_digest": _digest(payload)}},), manifest=manifest)
    except (KeyError, ValueError, TypeError, assets._Unavailable) as exc:
        return CatalogChoicePreparation(True, reasons=(str(exc),))


def validate_catalog_chart_metadata(metadata, *, payload, group, kind):
    try:
        if set(metadata) != {"catalog_chart", "prepared_payload_digest"} or (group, kind) != ("template", "image"):
            return False
        chart = metadata["catalog_chart"]
        if chart.get("schema") != "catalog-choice-chart.v1":
            return False
        body = {key: value for key, value in chart.items() if key not in {"chart_digest", "hmac"}}
        return (chart["chart_digest"] == _digest(body)
            and constant_time_compare(chart["hmac"], salted_hmac("catalog-choice-chart.v1", chart["chart_digest"], algorithm="sha256").hexdigest())
            and _digest(payload) == metadata["prepared_payload_digest"] == chart["payload_digest"]
            and payload["recipient"]["id"] == chart["scope"]["igsid"]
            and payload["message"] == {"attachment": {"type": "image", "payload": {"url": chart["image_url"], "is_reusable": True}}}
            and _digest(chart["image_url"]) == chart["url_digest"])
    except (KeyError, TypeError, ValueError):
        return False


def catalog_chart_reference(chart):
    return {key: deepcopy(value) for key, value in chart.items() if key != "image_url"}


def validate_catalog_chart_reference(reference, client, settings_row):
    from management.services.ig_catalog_configured_choices import capture_configured_choices
    from management.services import ig_revision_catalog_assets as assets
    from storefront.models import SizeGrid

    try:
        reason = current_catalog_choice_scope(reference, client, settings_row)
        if reason:
            return reason
        captured = capture_configured_choices(reference["product_id"], **reference["configuration_selector"], metadata_only=True)
        grid = captured.get("grid") or {}
        row = SizeGrid.objects.filter(pk=reference["grid_id"]).first()
        if (captured.get("configuration_digest") != reference["configuration_digest"]
            or grid.get("grid_id") != reference["grid_id"] or grid.get("source") != reference["grid_source"]
            or _digest(grid.get("image_name")) != reference["image_name_digest"]
            or row is None or _digest(assets._url(row.image)) != reference["url_digest"]):
            return "catalog_choice_chart_changed"
        restored = {**reference, "image_url": assets._url(row.image)}
        body = {key: value for key, value in restored.items() if key not in {"chart_digest", "hmac"}}
        if (reference["chart_digest"] != _digest(body)
            or not constant_time_compare(reference["hmac"], salted_hmac("catalog-choice-chart.v1",
                reference["chart_digest"], algorithm="sha256").hexdigest())):
            return "catalog_choice_chart_signature_invalid"
        return ""
    except (KeyError, ValueError, TypeError, assets._Unavailable):
        return "catalog_choice_chart_unavailable"


def validate_catalog_choice_metadata(metadata, *, payload, group, kind):
    """Strict JSON shape, seal, payload identity and exact action correspondence."""
    if not isinstance(metadata, dict):
        return False
    manifest = metadata.get("catalog_choice")
    delivery = metadata.get("delivery")
    legacy = isinstance(manifest, dict) and manifest.get("schema") == SCHEMA_V1
    keys = {"catalog_choice", "delivery", "prepared_payload_digest"} | (set() if legacy else {"part_index"})
    if (set(metadata) != keys or not isinstance(manifest, dict)
        or set(manifest) != (_V1_MANIFEST_KEYS if legacy else _MANIFEST_KEYS)
        or manifest.get("schema") not in {SCHEMA, SCHEMA_V1} or delivery not in {"native", "fallback"}
        or group != ("template" if delivery == "native" else "template_fallback")):
        return False
    try:
        part_index = metadata.get("part_index", 0)
        if isinstance(part_index, bool) or not isinstance(part_index, int) or not 0 <= part_index <= MAX_CARDS:
            return False
        expected_digest = (manifest["native_payload_digest"] if delivery == "native" else
            manifest["fallback_payload_digest"] if legacy else manifest["fallback_payload_digests"][part_index])
        if (manifest["manifest_digest"] != _digest({key: value for key, value in manifest.items() if key != "manifest_digest"})
            or manifest["context_digest"] != _digest(_context(manifest))
            or _digest(payload) != metadata["prepared_payload_digest"]
            or _digest(payload) != expected_digest):
            return False
        rebuilt = build_catalog_choice_effects(manifest["invitation_id"], manifest["scope"], manifest["cards"],
            presentation_kind=manifest["presentation_kind"], actions=manifest["actions"], _legacy=legacy)
        expected = rebuilt.effects[0 if delivery == "native" else 1 + part_index]
        return expected["kind"] == kind and expected["payload"] == payload and expected["projection_metadata"] == metadata
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return False


def current_catalog_choice_scope(manifest, client, settings_row, *, allow_product_continuation=False):
    """Current admission/pre-send guard. Historical replay deliberately omits it."""
    from management.models import IgCommerceSelectionSession
    from management.services.ig_conversation_routes import conversation_route_reset_floor

    scope = manifest["scope"]
    publication = getattr(settings_row, "active_instruction_publication", None)
    if (client.pk != scope["client_id"] or client.igsid != scope["igsid"]
        or client.privacy_erasure_started_at is not None or client.reply_permission_epoch != scope["client_epoch"]
        or conversation_route_reset_floor(client.pk) != scope["reset_floor"]
        or settings_row.pk != scope["settings_id"] or settings_row.reply_permission_epoch != scope["settings_epoch"]
        or publication is None or (publication.pk, publication.version, publication.snapshot_hash) !=
            (scope["publication_id"], scope["publication_version"], scope["publication_hash"])):
        return "catalog_choice_scope_changed"
    if scope["session_id"] is None:
        session = IgCommerceSelectionSession.objects.filter(client=client, open_slot=1, state="open").first()
        if session is None:
            return "" if client.current_commercial_episode_id == scope["episode_id"] else "catalog_choice_episode_changed"
        if (not allow_product_continuation or session.commercial_episode_id != client.current_commercial_episode_id
            or (scope["episode_id"] is not None and scope["episode_id"] != session.commercial_episode_id)
            or not _own_product_continuation(session, manifest)):
            return "catalog_choice_session_changed"
        return ""
    if client.current_commercial_episode_id != scope["episode_id"]:
        return "catalog_choice_episode_changed"
    session = IgCommerceSelectionSession.objects.filter(pk=scope["session_id"], client=client,
        commercial_episode_id=scope["episode_id"], generation=scope["generation"], open_slot=1).first()
    if session is None:
        return "catalog_choice_session_changed"
    if session.revision != scope["selection_revision"]:
        if not allow_product_continuation or not _own_product_continuation(session, manifest):
            return "catalog_choice_selection_changed"
    elif (_digest({"lines": session.lines or [], "active_index": int(session.active_index or 0)}) != scope["selection_digest"]
        or session.candidate_generation != scope["candidate_generation"]):
        return "catalog_choice_selection_changed"
    return ""


def _own_product_continuation(session, manifest):
    from management.services.ig_catalog_choice_actions import validated_choice_transition

    scope = manifest["scope"]
    rows = list(session.transitions.filter(from_revision__gte=scope["selection_revision"])
        .order_by("from_revision", "pk")[:33])
    if not rows or len(rows) > 32:
        return False
    revision = scope["selection_revision"]
    digest = scope["selection_digest"]
    for row in rows:
        if (row.from_revision != revision
            or _digest({"lines": row.previous_snapshot.get("lines") or [],
                "active_index": int(row.previous_snapshot.get("active_index") or 0)}) != digest
            or not validated_choice_transition(row, invitation_id=manifest["invitation_id"],
                manifest_digest=manifest["manifest_digest"])):
            return False
        revision = row.to_revision
        digest = _digest({"lines": row.next_snapshot.get("lines") or [],
            "active_index": int(row.next_snapshot.get("active_index") or 0)})
    return revision == session.revision and digest == _digest({"lines": session.lines or [],
        "active_index": int(session.active_index or 0)})


def catalog_choice_reference(manifest):
    """Content-free selector compatible with the outbox's 4096-byte bindings."""
    return {"invitation_id": manifest["invitation_id"], "manifest_digest": manifest["manifest_digest"],
        "presentation_kind": manifest["presentation_kind"],
        "scope": deepcopy(manifest["scope"]), "cards": [{key: deepcopy(row[key]) for key in
            ("product_id", "asset_ref", "asset_selected_variant_id", "configuration_selector", "configuration_digest")}
            for row in manifest["cards"]]}


def validate_catalog_choice_assets(manifest, client):
    """Exact current row ownership and configuration checks; no provider calls."""
    from management.services import ig_revision_catalog_assets as assets
    from management.services.ig_catalog_configured_choices import capture_configured_choices

    try:
        for card in manifest["cards"]:
            product = assets._product(card["product_id"])
            if card["asset_ref"] is not None:
                asset = assets._load_asset(product, card["asset_ref"], card["asset_selected_variant_id"])
                reference, _payload, _projection = assets._descriptor(client, product, asset, 0, False)
                if reference != card["asset_ref"] or (card.get("image_url") and _digest(card["image_url"]) != reference["url_digest"]):
                    return "catalog_choice_asset_changed"
            capture = capture_configured_choices(card["product_id"], **card["configuration_selector"], metadata_only=True)
            if capture.get("configuration_digest") != card["configuration_digest"]:
                return "catalog_choice_configuration_changed"
        return ""
    except (ValueError, TypeError, KeyError, assets._Unavailable):
        return "catalog_choice_asset_unavailable"


def delivered_catalog_choice(client, source, *, fresh=True):
    """Resolve an own signed action solely from a confirmed existing outbox part."""
    from management.models import IgRevisionDeliveryEffect, InstagramBotSettings

    refused = lambda reason: {"ready": False, "reason": reason}
    match = _PAYLOAD.fullmatch(str(getattr(source, "quick_reply_payload", "") or ""))
    if not match:
        return refused("catalog_choice_payload_invalid")
    invitation_id, index, signature = int(match[1]), int(match[2]), match[3]
    if (source.client_id != client.pk or source.sender_id != client.igsid or source.role != "user"
        or source.source != "webhook" or source.status == "failed"
        or not (getattr(source, "provider_message_id", "") or getattr(source, "mid", ""))):
        return refused("catalog_choice_source_unverified")
    effects = list(IgRevisionDeliveryEffect.objects.filter(revision_id=invitation_id,
        revision__client_id=client.pk, group__in=("template", "template_fallback"))
        .select_related("revision").order_by("order_index")[:MAX_CARDS + 3])
    native = next((row for row in effects if (row.projection_metadata or {}).get("delivery") == "native"), None)
    fallbacks = [row for row in effects if (row.projection_metadata or {}).get("delivery") == "fallback"]
    if native is None or not fallbacks:
        return refused("catalog_choice_receipt_missing")
    manifest = native.projection_metadata.get("catalog_choice") or {}
    expected_count = 1 if manifest.get("schema") == SCHEMA_V1 else len(manifest.get("fallback_payload_digests") or [])
    if (len(effects) != 1 + expected_count or len(fallbacks) != expected_count
        or [row.part_index for row in fallbacks] != list(range(expected_count))
        or any(row.part_count != expected_count for row in fallbacks)):
        return refused("catalog_choice_receipt_missing")
    for effect in effects:
        if (effect.payload_digest != _digest(effect.payload) or effect.projection_digest != _digest(effect.projection_metadata)
            or not validate_catalog_choice_metadata(effect.projection_metadata,
                payload=effect.payload, group=effect.group, kind=effect.kind)):
            return refused("catalog_choice_receipt_changed")
    if any(row.projection_metadata["catalog_choice"] != manifest for row in fallbacks):
        return refused("catalog_choice_receipt_changed")
    scope = manifest["scope"]
    for row in effects:
        if (row.revision_id != invitation_id or row.revision_snapshot_digest != scope["revision_snapshot_digest"]
            or row.client_permission_epoch != scope["client_epoch"]
            or row.recipient_igsid != scope["igsid"] or row.provider_namespace != scope["namespace"]
            or row.settings_id_snapshot != scope["settings_id"] or row.settings_permission_epoch != scope["settings_epoch"]
            or row.publication_id != scope["publication_id"] or row.publication_version != scope["publication_version"]
            or row.publication_hash != scope["publication_hash"] or row.plan_digest != native.plan_digest):
            return refused("catalog_choice_receipt_binding_changed")
    if native.state == "sent" and any(row.state in {"sent", "provider_started", "unknown"} for row in fallbacks):
        return refused("catalog_choice_receipt_conflict")
    if native.state == "sent" and native.provider_message_id:
        effect = native
    elif (native.state == "definite_failed" and native.failure_code == "provider_rejected"
        and native.provider_http_status is not None and 400 <= native.provider_http_status < 500
        and all(row.state == "sent" and row.provider_message_id and row.terminal_at is not None
            and row.activation_group == "template" and row.activation_part_index == native.part_index
            and row.activation_failure_code == "provider_rejected" for row in fallbacks)
        and len({row.provider_message_id for row in fallbacks}) == len(fallbacks)):
        effect = fallbacks[-1]
    else:
        return refused("catalog_choice_not_delivered")
    if (scope["client_id"] != client.pk or scope["igsid"] != client.igsid
        or scope["namespace"] != source.provider_namespace or source.pk <= max(scope["source_message_ids"])
        or source.pk < scope["reset_floor"] or effect.terminal_at is None
        or source.provider_created_at is None or source.provider_created_at.replace(microsecond=0) <
            (getattr(effect, "provider_started_at", None) or effect.terminal_at).replace(microsecond=0)
        or native.revision_snapshot_digest != scope["revision_snapshot_digest"]
        or native.revision.snapshot_digest != scope["revision_snapshot_digest"]
        or native.client_permission_epoch != scope["client_epoch"]
        or native.recipient_igsid != scope["igsid"] or native.provider_namespace != scope["namespace"]
        or index >= len(manifest["actions"])
        or not constant_time_compare(signature, _signature(invitation_id, index, manifest["context_digest"], schema=manifest["schema"]))):
        return refused("catalog_choice_source_mismatch")
    action = manifest["actions"][index]
    if fresh:
        settings_row = InstagramBotSettings.objects.select_related("active_instruction_publication").filter(pk=scope["settings_id"]).first()
        if settings_row is None:
            return refused("catalog_choice_settings_missing")
        reason = current_catalog_choice_scope(manifest, client, settings_row,
            allow_product_continuation=action["kind"] in {"product", "chart", "page"}) or validate_catalog_choice_assets(manifest, client)
        if reason:
            return refused(reason)
    return {"ready": True, "reason": "", "manifest_digest": manifest["manifest_digest"],
        "invitation_id": invitation_id, "action": deepcopy(action), "scope": deepcopy(scope),
        "manifest": deepcopy(manifest), "effect_id": effect.pk, "provider_message_id": effect.provider_message_id}
