"""One creation contains independently configured garments and shared contact/gift."""
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import json
import logging
import re
from types import SimpleNamespace
from uuid import uuid4

from django.db import transaction
from django.http import QueryDict
from django.utils.datastructures import MultiValueDict
from PIL import Image

from dtf.utils import ALLOWED_HELP_EXTS, build_safe_upload_name, get_limits, validate_uploaded_file

from storefront.custom_print_config import GIFT_SERVICE, build_placement_specs, normalize_custom_print_snapshot, normalize_gift_extras
from storefront.forms import CustomPrintLeadForm
from storefront.models import CustomPrintLead, CustomPrintLeadAttachment, CustomPrintModerationStatus

logger = logging.getLogger(__name__)
MAX_CREATION_ITEMS = 10
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class CreationValidationError(ValueError):
    def __init__(self, errors=None, item_errors=None):
        self.errors = errors or {}
        self.item_errors = item_errors or {}
        super().__init__("Invalid custom-print creation")

    def payload(self):
        return {"ok": False, "errors": self.errors, "item_errors": self.item_errors}


@dataclass
class CustomPrintCreation:
    items: list
    contact: dict
    gift: dict
    order_purpose: str
    submission_type: str
    forms: list = field(default_factory=list)
    gift_image: object = None
    id: str = field(default_factory=lambda: str(uuid4()))


def _form_errors(form):
    return {key: [error["message"] for error in values] for key, values in form.errors.get_json_data().items()}


def _plain_contact(raw):
    return {key: str(raw.get(key) or "").strip() for key in ("name", "channel", "value")}


def prepare_creation(raw, uploads=None, *, submission_type="lead", verification=None, partial=False):
    """Validate all item forms before any row or upload is persisted."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raise CreationValidationError({"creation_json": ["Некоректні дані замовлення."]}) from None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise CreationValidationError({"creation_json": ["Некоректний формат замовлення."]})
    if raw.get("mode", "personal") != "personal" or raw.get("order_purpose") not in {"personal", "gift"}:
        raise CreationValidationError({"mode": ["Оберіть формат «Для себе» або «На подарунок»."]})
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_CREATION_ITEMS:
        raise CreationValidationError({"items": [f"Додайте від 1 до {MAX_CREATION_ITEMS} різних виробів."]})
    if not isinstance(raw.get("contact", {}), dict) or not isinstance(raw.get("gift", {}), dict):
        raise CreationValidationError({"creation_json": ["Некоректні контактні або подарункові дані."]})
    contact = _plain_contact(raw.get("contact") or {})
    gift_raw = raw.get("gift") or {}
    if not isinstance(gift_raw.get("enabled", False), bool):
        raise CreationValidationError({"gift": ["Некоректний вибір подарункової упаковки."]})
    for name in ("box", "delivery", "certificate", "wrapping"):
        option = gift_raw.get(name, {})
        if not isinstance(option, dict) or not isinstance(option.get("enabled", False), bool):
            raise CreationValidationError({"gift": ["Некоректні подарункові опції."]})
    if (gift_raw.get("box") or {}).get("enabled") and (not isinstance(gift_raw["box"].get("content_type", "text"), str) or gift_raw["box"].get("content_type", "text") not in {"text", "image"}):
        raise CreationValidationError({"gift_box": ["Оберіть текст або зображення для коробки."]})
    if (gift_raw.get("delivery") or {}).get("enabled") and (not isinstance(gift_raw["delivery"].get("method"), str) or gift_raw["delivery"].get("method") not in {"branch", "courier"}):
        raise CreationValidationError({"gift_delivery": ["Оберіть доставку у відділення або курʼєром."]})
    wrapping = gift_raw.get("wrapping") or {}
    if wrapping.get("enabled"):
        for field, choices in (("paper", "papers"), ("style", "styles")):
            if not isinstance(wrapping.get(field, "brand"), str) or wrapping.get(field, "brand") not in GIFT_SERVICE["wrapping"][choices]:
                raise CreationValidationError({"gift_wrapping": ["Оберіть коректні побажання до святкового пакування."]})
        preference = wrapping.get("preference", "")
        if not isinstance(preference, str) or len(preference) > GIFT_SERVICE["wrapping"]["max_preference_length"]:
            raise CreationValidationError({"gift_wrapping_preference": ["Побажання до пакування можуть містити не більше 240 символів."]})
    certificate = gift_raw.get("certificate") or {}
    if certificate.get("enabled"):
        message_mode, message = certificate.get("message_mode", "blank"), certificate.get("message", "")
        if message_mode not in ("blank", "write") or not isinstance(message, str):
            raise CreationValidationError({"gift_certificate_message": ["Оберіть, хто напише привітання на листівці."]})
        if message_mode == "write" and len(message) > GIFT_SERVICE["certificate"]["max_message_length"]:
            raise CreationValidationError({"gift_certificate_message": ["Привітання на листівці може містити не більше 240 символів."]})
        if message_mode == "write" and not message.strip() and not partial:
            raise CreationValidationError({"gift_certificate_message": ["Додайте привітання, яке має написати наша команда."]})
    gift = normalize_gift_extras(gift_raw)
    if gift["box"]["enabled"] and gift["box"]["content_type"] == "text" and not gift["box"]["text"] and not partial:
        raise CreationValidationError({"gift_box_text": ["Додайте текст для друку всередині коробки."]})
    items, item_errors, ids = [], {}, set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise CreationValidationError({"items": ["Некоректні дані виробу."]})
        item_id = item.get("id")
        if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id) or item_id in ids:
            raise CreationValidationError({"items": ["Вироби повинні мати різні коректні ідентифікатори."]})
        ids.add(item_id)
        snapshot = item.get("snapshot")
        if not isinstance(snapshot, dict):
            item_errors[item_id] = {"snapshot": ["Некоректні налаштування виробу."]}
            continue
        if any(not isinstance(snapshot.get(key, {}), dict) for key in ("product", "print", "artwork", "order", "notes", "pricing", "ui")):
            item_errors[item_id] = {"snapshot": ["Некоректні налаштування виробу."]}
            continue
        snapshot = deepcopy(snapshot)
        snapshot.update(mode="personal", order_purpose=raw["order_purpose"], contact=contact, submission_type=submission_type)
        snapshot.setdefault("order", {})["gift"] = False
        snapshot["order"]["gift_text"] = ""
        snapshot.setdefault("pricing", {})["gift_price"] = 0
        try:
            normalized = normalize_custom_print_snapshot(snapshot)
        except (AttributeError, TypeError, ValueError, OverflowError):
            item_errors[item_id] = {"snapshot": ["Некоректні налаштування виробу."]}
            continue
        items.append({"id": item_id, "snapshot": normalized, "raw": snapshot})
    if item_errors:
        raise CreationValidationError(item_errors=item_errors)
    uploads = uploads or MultiValueDict()
    allowed_upload_keys = {f"{kind}:{item_id}" for item_id in ids for kind in ("files", "garment_photo")}
    allowed_upload_keys.add("gift_image")
    if set(uploads) - allowed_upload_keys:
        raise CreationValidationError({"files": ["Файли не привʼязані до виробів цього замовлення."]})
    creation = CustomPrintCreation(items, contact, gift, raw["order_purpose"], submission_type)
    images = uploads.getlist("gift_image")
    image_box = gift["box"]["enabled"] and gift["box"]["content_type"] == "image"
    if images and (not image_box or len(images) != 1):
        raise CreationValidationError({"gift_image": ["Додайте одне зображення для обраної персоналізованої коробки."]})
    if image_box and not partial:
        if not images:
            raise CreationValidationError({"gift_image": ["Завантажте зображення для друку всередині коробки повторно."]})
        image = images[0]
        try:
            validate_uploaded_file(image, allowed_exts=ALLOWED_HELP_EXTS, max_file_mb=get_limits()["max_file_mb"], strict_magic=True)
            with Image.open(image) as decoded:
                if decoded.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("unsupported gift image")
                decoded.verify()
            image.seek(0)
        except Exception:
            raise CreationValidationError({"gift_image": ["Завантажте коректне зображення PNG, JPEG або WebP."]}) from None
        creation.gift_image = image
        gift["box"]["image_name"] = image.name
    elif image_box:
        gift["box"]["needs_reupload"] = True
    # Each shared extra is owned by one line. Browser item totals exclude extras.
    if has_gift_extras(gift):
        owner = items[0]["snapshot"]
        owner["order"].update(gift=deepcopy(gift), gift_text=gift["box"]["text"])
        box_price = gift["box"]["price"] if gift["box"]["enabled"] else 0
        known_price = (box_price or 0) + gift["delivery"]["price"] + gift["certificate"]["price"] + gift["wrapping"]["price"]
        owner["pricing"].update(creation_base_total=owner["pricing"].get("final_total"), gift_price=known_price,
                                 gift_box_price=box_price, delivery_price=gift["delivery"]["price"], certificate_price=gift["certificate"]["price"], wrapping_price=gift["wrapping"]["price"])
        if owner["pricing"].get("final_total") is not None:
            owner["pricing"]["final_total"] += known_price
        if gift["box"]["estimate_required"]:
            owner["pricing"].update(final_total=None, estimate_required=True, estimate_reason="Ціну персоналізованої коробки узгодить менеджер.")
            if submission_type == "cart" and not partial:
                raise CreationValidationError({"gift_box_price": ["Персоналізовану коробку спочатку має прорахувати менеджер. Надішліть заявку менеджеру."]})
    if partial:
        return creation
    for item in items:
        snapshot, raw_snapshot = item["snapshot"], item["raw"]
        product, order, artwork, print_data, notes = (snapshot[key] for key in ("product", "order", "artwork", "print", "notes"))
        data = QueryDict(mutable=True)
        data.update({
            "service_kind": (raw_snapshot.get("artwork") or {}).get("service_kind") or "", "product_type": (raw_snapshot.get("product") or {}).get("type") or product["type"],
            "quantity": (raw_snapshot.get("order") or {}).get("quantity", order["quantity"]),
            "size_mode": order["size_mode"], "sizes_note": order["sizes_note"],
            "client_kind": "personal", "fit": product["fit"], "fabric": product["fabric"], "color_choice": product["color"],
            "placement_note": print_data["placement_note"], "brief": notes["brief"], "garment_note": notes["garment_note"],
            "name": contact["name"], "contact_channel": contact["channel"], "contact_value": contact["value"],
            "file_triage_status": artwork["triage_status"], "exit_step": snapshot["ui"]["current_step"],
        })
        data.setlist("placements", (raw_snapshot.get("print") or {}).get("zones") or [])
        files = list(uploads.getlist(f"files:{item['id']}"))
        photos = list(uploads.getlist(f"garment_photo:{item['id']}"))
        if len(photos) > 1:
            item_errors[item["id"]] = {"garment_photo": ["Додайте одне фото виробу."]}
            continue
        regular_meta = [meta for meta in artwork["files"] if meta.get("zone") != "garment_reference"]
        file_indices = [meta.get("file_index") for meta in regular_meta]
        if (files or regular_meta) and (len(regular_meta) != len(files) or set(file_indices) != set(range(len(files)))):
            item_errors[item["id"]] = {"files": ["Карта файлів не відповідає файлам цього виробу."]}
            continue
        specs = build_placement_specs(snapshot)
        allowed_keys = {spec["placement_key"] for spec in specs}
        if any(meta.get("placement_key") not in allowed_keys or meta.get("role") not in {"design", "reference"} for meta in regular_meta):
            item_errors[item["id"]] = {"files": ["Оберіть коректну зону та роль кожного файлу."]}
            continue
        if artwork["service_kind"] in {"ready", "adjust"}:
            required_keys = {spec["placement_key"] for spec in specs if spec.get("requires_artwork_file")}
            present_keys = {meta["placement_key"] for meta in regular_meta}
            if not regular_meta:
                present_keys = {spec["placement_key"] for spec in specs if spec.get("file_index", len(files)) < len(files)}
            if required_keys - present_keys:
                item_errors[item["id"]] = {"files": ["Додайте файли для всіх зон, де потрібен макет."]}
                continue
        if photos:
            snapshot["artwork"]["files"] = [meta for meta in artwork["files"] if meta.get("zone") != "garment_reference"]
            snapshot["artwork"]["files"].append({"name": photos[0].name, "zone": "garment_reference", "placement_key": "garment_reference", "role": "reference", "status": "reference-only", "file_index": len(files)})
        for key, value in (verification or {}).items():
            if key.startswith("telegram_"):
                data[key] = value
        data["config_draft_json"] = json.dumps(snapshot)
        data["pricing_snapshot_json"] = json.dumps(snapshot["pricing"])
        data["placement_specs_json"] = json.dumps(build_placement_specs(snapshot))
        form = CustomPrintLeadForm(data, MultiValueDict({"files": files + photos}), require_artwork_files=submission_type == "cart")
        if not form.is_valid():
            item_errors[item["id"]] = _form_errors(form)
        elif product["type"] != "customer_garment" and order["size_mode"] != "manager" and sum(order["size_breakdown"].values()) != form.cleaned_data["quantity"]:
            item_errors[item["id"]] = {"sizes_note": ["Кількість виробів за розмірами повинна дорівнювати кількості цієї позиції."]}
        creation.forms.append(form)
    if item_errors:
        common_fields = {"name", "contact_channel", "contact_value"}
        common = {}
        for errors in item_errors.values():
            for key in common_fields & errors.keys():
                common[key] = errors.pop(key)
        item_errors = {key: errors for key, errors in item_errors.items() if errors}
        raise CreationValidationError(common, item_errors)
    return creation


def has_gift_extras(gift):
    return any((gift.get(key) or {}).get("enabled") for key in ("box", "delivery", "certificate", "wrapping"))


def _partial_lead(snapshot):
    product, order, artwork, print_data, notes, contact = (snapshot[key] for key in ("product", "order", "artwork", "print", "notes", "contact"))
    return CustomPrintLead.objects.create(
        service_kind=artwork["service_kind"] or "design", product_type=product["type"], placements=print_data["zones"],
        placement_note=print_data["placement_note"], quantity=order["quantity"], size_mode=order["size_mode"], sizes_note=order["sizes_note"],
        client_kind="personal", fit=product["fit"], fabric=product["fabric"], color_choice=product["color"],
        garment_note=notes["garment_note"], file_triage_status=artwork["triage_status"], exit_step=snapshot["ui"]["current_step"],
        placement_specs_json=build_placement_specs(snapshot), pricing_snapshot_json=snapshot["pricing"], config_draft_json=snapshot,
        name=contact["name"], contact_channel=contact["channel"], contact_value=contact["value"], brief=notes["brief"],
        source="custom_print_safe_exit", moderation_status=CustomPrintModerationStatus.DRAFT,
    )


def cleanup_creation_uploads(saved_uploads):
    for storage, name in saved_uploads:
        try:
            storage.delete(name)
        except Exception:
            logger.exception("Could not clean up failed custom-print upload")


def save_creation(creation, *, cart_builder=None, analytics=None):
    """Save all leads, group metadata and cart projections in one DB transaction."""
    leads, saved_uploads = [], []
    try:
        with transaction.atomic():
            for index, item in enumerate(creation.items):
                if creation.submission_type == "safe_exit":
                    lead = _partial_lead(item["snapshot"])
                else:
                    lead = creation.forms[index].save(
                        source="custom_print_cart" if creation.submission_type == "cart" else "main_custom_print",
                        moderation_status=CustomPrintModerationStatus.AWAITING_REVIEW if creation.submission_type == "cart" else None,
                        saved_uploads=saved_uploads,
                    )
                if creation.submission_type == "cart":
                    lead.ensure_moderation_token()
                leads.append(lead)
            lead_ids = [lead.pk for lead in leads]
            if creation.gift_image is not None:
                attachment = CustomPrintLeadAttachment(lead=leads[0], placement_zone="gift_box", attachment_role="gift_reference", sort_order=1000)
                attachment.file.save(build_safe_upload_name("gift-box", creation.gift_image.name), creation.gift_image, save=False)
                saved_uploads.append((attachment.file.storage, attachment.file.name))
                attachment.save()
                creation.gift["box"]["attachment"] = {"id": attachment.pk, "lead_id": leads[0].pk, "file_name": attachment.file.name}
            for index, lead in enumerate(leads):
                snapshot = lead.config_draft_json
                snapshot["creation"] = {
                    "id": creation.id, "item_id": creation.items[index]["id"], "item_index": index,
                    "item_count": len(leads), "lead_ids": lead_ids, "gift_owner_lead_id": lead_ids[0] if has_gift_extras(creation.gift) else None,
                    "gift": creation.gift,
                }
                if index == 0 and has_gift_extras(creation.gift):
                    snapshot["order"]["gift"] = deepcopy(creation.gift)
                if analytics:
                    snapshot["analytics"] = analytics
                lead.config_draft_json = snapshot
                lead.save(update_fields=["config_draft_json"])
            cart_items = {f"custom:{lead.pk}": cart_builder(lead) for lead in leads} if cart_builder else {}
        return leads, cart_items
    except Exception:
        cleanup_creation_uploads(saved_uploads)
        raise


def creation_analytics_lead(leads):
    """Aggregate event projection; preserve every stored item's payable price."""
    first = leads[0]
    values = [(lead.pricing_snapshot_json or {}).get("final_total") for lead in leads]
    total = sum(Decimal(str(value)) for value in values) if all(value is not None for value in values) else None
    draft = deepcopy(first.config_draft_json)
    draft["pricing"] = {**draft.get("pricing", {}), "final_total": float(total) if total is not None else None, "base_price": None}
    return SimpleNamespace(pk=first.pk, lead_number=first.lead_number, name=first.name, contact_channel=first.contact_channel,
                           contact_value=first.contact_value, quantity=sum(lead.quantity for lead in leads), config_draft_json=draft)


def creation_response(creation, leads):
    return {"ok": True, "creation_id": creation.id, "lead_number": leads[0].lead_number if leads else None,
            "lead_numbers": [lead.lead_number for lead in leads], "lead_ids": [lead.pk for lead in leads],
            "items": [{"id": item["id"], "lead_id": lead.pk, "lead_number": lead.lead_number} for item, lead in zip(creation.items, leads)],
            "item_count": len(creation.items), "total_quantity": sum(item["snapshot"]["order"]["quantity"] for item in creation.items)}


def custom_print_checkout_payload(leads):
    """Freeze only extras whose approved charge owner is included in checkout."""
    by_id = {lead.pk: lead for lead in leads}
    groups = {}
    for lead in leads:
        meta = (lead.config_draft_json or {}).get("creation") or {}
        if not meta.get("id"):
            continue
        owner_id = meta.get("gift_owner_lead_id")
        group = groups.setdefault(meta["id"], {"id": meta["id"], "owner_lead_id": owner_id,
                            "included_lead_ids": [], "gift": deepcopy(meta.get("gift") or {}), "applied": False})
        group["included_lead_ids"].append(lead.pk)
        if owner_id in by_id:
            owner = by_id[owner_id]
            if owner.moderation_status != CustomPrintModerationStatus.APPROVED:
                raise CreationValidationError({"gift_delivery": ["Подарункові опції спочатку має погодити менеджер."]})
            group.update(applied=True, owner_status="approved", owner_price=str(owner.final_price_value), source=owner.source)
            delivery = group["gift"].get("delivery") or {}
            if delivery.get("enabled"):
                fee = Decimal(str(delivery.get("price") or 0))
                recorded = Decimal(str((owner.pricing_snapshot_json or {}).get("delivery_price") or 0))
                if fee <= 0 or fee != recorded or owner.final_price_value <= fee:
                    raise CreationValidationError({"gift_delivery": ["Суму доставки потрібно звірити з погодженою ціною позиції."]})
    methods = {(group["gift"].get("delivery") or {}).get("method") for group in groups.values()
               if group["applied"] and (group["gift"].get("delivery") or {}).get("enabled")}
    if len(methods) > 1:
        raise CreationValidationError({"gift_delivery": ["Для одного замовлення оберіть однаковий спосіб доставки подарунків або узгодьте його з менеджером."]})
    return {"version": 1, "groups": list(groups.values())}


def attach_custom_print_checkout(order, *, leads=None, creation_data=None, lead_ids=None):
    from orders.services.delivery_payment import build_custom_print_delivery_contract
    data = creation_data if creation_data is not None else custom_print_checkout_payload(leads or [])
    if not data.get("groups"):
        return
    payload = dict(order.payment_payload or {})
    payload["custom_print_creation"] = deepcopy(data)
    payload["custom_print_lead_ids"] = list(lead_ids) if lead_ids is not None else [lead.pk for lead in leads or []]
    contract = build_custom_print_delivery_contract(gross_total=order.total_sum, discount_amount=order.discount_amount or 0,
                                                   creation_data=data, order_id=order.pk)
    if contract:
        payload["delivery_payment"] = contract
    order.payment_payload = payload
    order.save(update_fields=["payment_payload"])
