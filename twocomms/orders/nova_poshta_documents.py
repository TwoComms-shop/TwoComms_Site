from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import requests
from django.conf import settings

from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from orders.nova_poshta_lookup import NovaPoshtaDirectoryService

try:
    import phonenumbers
    from phonenumbers import PhoneNumberFormat
except Exception:  # pragma: no cover - optional runtime dependency
    phonenumbers = None
    PhoneNumberFormat = None

logger = logging.getLogger(__name__)

TELEGRAM_CREATE_NP_WAYBILL_ACTION = "create-np-waybill"
TELEGRAM_DELETE_NP_WAYBILL_ACTION = "delete-np-waybill"


class NovaPoshtaDocumentError(Exception):
    """Raised when a Nova Poshta waybill cannot be prepared or created."""


@dataclass(frozen=True)
class NovaPoshtaResolvedPoint:
    city_label: str
    warehouse_label: str
    settlement_ref: str
    city_ref: str
    warehouse_ref: str
    warehouse_kind: str


def _phone_parse_candidates(raw_phone: str) -> list[tuple[str, str | None]]:
    raw = str(raw_phone or "").strip()
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return []

    candidates: list[tuple[str, str | None]] = []

    def add(value: str, region: str | None = "UA") -> None:
        value = (value or "").strip()
        item = (value, region)
        if value and item not in candidates:
            candidates.append(item)

    if raw.startswith("+"):
        if digits.startswith("3800") and len(digits) == 13:
            add(f"+380{digits[4:]}", None)
        add(raw, None)
        add(f"+{digits}", None)
        return candidates

    if digits.startswith("00"):
        trimmed = digits[2:]
        if trimmed.startswith("3800") and len(trimmed) == 13:
            add(f"+380{trimmed[4:]}", None)
        add(f"+{digits[2:]}", None)
        return candidates

    if digits.startswith("3800") and len(digits) == 13:
        add(f"+380{digits[4:]}", None)
        add(f"0{digits[4:]}", "UA")
        return candidates

    if digits.startswith("380") and len(digits) == 12:
        add(f"+{digits}", None)
        add(digits, "UA")
        return candidates

    if digits.startswith("80") and len(digits) == 11:
        add(digits[1:], "UA")
        return candidates

    if digits.startswith("8") and len(digits) == 10:
        add(f"0{digits[1:]}", "UA")
        return candidates

    if digits.startswith("0") and len(digits) == 10:
        add(digits, "UA")
        return candidates

    if len(digits) == 9:
        add(digits, "UA")
        add(f"0{digits}", "UA")

    return candidates


def normalize_phone(phone: str) -> str:
    raw = str(phone or "").strip()
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""

    if phonenumbers is not None:
        for candidate, region in _phone_parse_candidates(phone):
            try:
                parsed = phonenumbers.parse(candidate, region)
            except Exception:
                continue
            if not phonenumbers.is_possible_number(parsed):
                continue
            if not phonenumbers.is_valid_number(parsed):
                continue
            try:
                return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
            except Exception:
                continue

    if raw.startswith("+"):
        if digits.startswith("3800") and len(digits) == 13:
            return f"+380{digits[4:]}"
        if digits.startswith("380"):
            return f"+{digits}" if len(digits) == 12 else ""
        if 8 <= len(digits) <= 15:
            return f"+{digits}"
        return ""

    if digits.startswith("00"):
        trimmed = digits[2:]
        if trimmed.startswith("3800") and len(trimmed) == 13:
            return f"+380{trimmed[4:]}"
        if trimmed.startswith("380"):
            return f"+{trimmed}" if len(trimmed) == 12 else ""
        if 8 <= len(trimmed) <= 15:
            return f"+{trimmed}"
        return ""

    if digits.startswith("3800") and len(digits) == 13:
        return f"+380{digits[4:]}"
    if digits.startswith("380") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("80") and len(digits) == 11:
        return f"+38{digits[1:]}"
    if digits.startswith("8") and len(digits) == 10:
        return f"+380{digits[1:]}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+38{digits}"
    if len(digits) == 9:
        return f"+380{digits}"
    return ""


def normalize_phone_for_np(phone: str) -> str:
    normalized = normalize_phone(phone)
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if digits.startswith("380") and len(digits) == 12:
        return digits
    return ""


def normalize_checkout_phone(phone: str) -> str:
    """
    Checkout currently creates domestic Nova Poshta shipments, so keep the
    stored value in E.164 while requiring a Ukrainian delivery-compatible number.
    """
    normalized = normalize_phone(phone)
    return normalized if normalize_phone_for_np(normalized) else ""


def canonicalize_order_pay_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"prepay_200", "prepay", "prepaid", "partial", "partial_payment", "prepay200"}:
        return "prepay_200"
    if raw == "cod":
        return "cod"
    return "online_full"


def canonicalize_payment_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw == "partial":
        return "prepaid"
    if raw in {"unpaid", "checking", "prepaid", "paid"}:
        return raw
    return "unpaid"


def get_payment_status_label(value: Any) -> str:
    return {
        "unpaid": "Не оплачено",
        "checking": "На перевірці",
        "prepaid": "Внесена передплата",
        "paid": "Оплачено повністю",
    }.get(canonicalize_payment_status(value), "Не оплачено")


def build_order_payment_snapshot(order) -> dict[str, Any]:
    gross_total = NovaPoshtaDocumentService._as_money(getattr(order, "total_sum", 0))
    discount_amount = NovaPoshtaDocumentService._as_money(getattr(order, "discount_amount", 0))
    payable_total = max(gross_total - discount_amount, Decimal("0.00"))
    pay_type = canonicalize_order_pay_type(getattr(order, "pay_type", ""))
    payment_status = canonicalize_payment_status(getattr(order, "payment_status", ""))

    prepayment_amount = Decimal("0.00")
    get_prepayment_amount = getattr(order, "get_prepayment_amount", None)
    if callable(get_prepayment_amount):
        prepayment_amount = NovaPoshtaDocumentService._as_money(get_prepayment_amount())
    elif pay_type == "prepay_200":
        prepayment_amount = Decimal("200.00")

    if payment_status == "paid":
        cod_amount = Decimal("0.00")
    elif pay_type == "prepay_200":
        cod_amount = max(payable_total - prepayment_amount, Decimal("0.00"))
    elif pay_type == "cod":
        cod_amount = payable_total
    else:
        cod_amount = Decimal("0.00")

    if payment_status == "paid":
        paid_amount = payable_total
    elif payment_status == "prepaid":
        paid_amount = prepayment_amount
    else:
        paid_amount = Decimal("0.00")

    return {
        "payment_status": payment_status,
        "payment_status_label": get_payment_status_label(payment_status),
        "pay_type": pay_type,
        # total_sum remains a compatibility alias, now with the unambiguous
        # meaning expected by payment and delivery consumers: amount due.
        "total_sum": f"{payable_total:.2f}",
        "total_sum_value": payable_total,
        "gross_total": f"{gross_total:.2f}",
        "gross_total_value": gross_total,
        "discount_amount": f"{discount_amount:.2f}",
        "discount_amount_value": discount_amount,
        "payable_total": f"{payable_total:.2f}",
        "payable_total_value": payable_total,
        "paid_amount": f"{paid_amount:.2f}",
        "paid_amount_value": paid_amount,
        "declared_cost": f"{payable_total:.2f}",
        "declared_cost_value": payable_total,
        "prepayment_amount": f"{prepayment_amount:.2f}",
        "prepayment_amount_value": prepayment_amount,
        "cod_amount": f"{cod_amount:.2f}",
        "cod_amount_value": cod_amount,
        "remaining_amount": f"{cod_amount:.2f}",
        "remaining_amount_value": cod_amount,
    }


def _infer_warehouse_kind(warehouse_label: str, *, fallback: str = "branch") -> str:
    normalized = str(warehouse_label or "").strip().lower()
    if "поштомат" in normalized or "postomat" in normalized:
        return "postomat"
    return fallback if fallback in {"branch", "postomat"} else "branch"


def _build_point_tokens(
    *,
    city_label: str,
    settlement_ref: str,
    city_ref: str,
    warehouse_label: str,
    warehouse_ref: str,
    warehouse_kind: str,
) -> tuple[str, str]:
    city_token = ""
    warehouse_token = ""

    if city_label and (settlement_ref or city_ref):
        try:
            city_token = build_city_choice_token(
                {
                    "label": city_label,
                    "settlement_ref": settlement_ref or city_ref,
                    "city_ref": city_ref or settlement_ref,
                }
            )
        except Exception:
            city_token = ""

    if warehouse_label and warehouse_ref:
        try:
            warehouse_token = build_warehouse_choice_token(
                {
                    "label": warehouse_label,
                    "ref": warehouse_ref,
                    "kind": _infer_warehouse_kind(warehouse_label, fallback=warehouse_kind),
                    "city_ref": city_ref,
                },
                fallback_city_ref=city_ref,
            )
        except Exception:
            warehouse_token = ""

    return city_token, warehouse_token


def split_person_name(full_name: str) -> dict[str, str]:
    parts = [part for part in str(full_name or "").strip().split() if part]
    if not parts:
        return {"first_name": "", "middle_name": "", "last_name": ""}
    if len(parts) == 1:
        return {"first_name": parts[0], "middle_name": "", "last_name": ""}
    if len(parts) == 2:
        return {"first_name": parts[1], "middle_name": "", "last_name": parts[0]}
    return {
        "first_name": parts[1],
        "middle_name": " ".join(parts[2:]),
        "last_name": parts[0],
    }


def build_waybill_description(order) -> str:
    items = list(getattr(order, "items", []).all() if hasattr(getattr(order, "items", None), "all") else getattr(order, "items", []) or [])
    total_qty = sum(int(getattr(item, "qty", 0) or 0) for item in items)
    if total_qty == 1 and len(items) == 1:
        title = str(getattr(items[0], "title", "") or "товар").strip()
        return f"Одяг бренду TwoComms, {title}"[:120]
    custom_items = list(
        getattr(order, "custom_print_leads", []).all()
        if hasattr(getattr(order, "custom_print_leads", None), "all")
        else getattr(order, "custom_print_leads", []) or []
    )
    custom_qty = sum(int(getattr(item, "quantity", 0) or 0) for item in custom_items)
    if custom_qty == 1 and len(custom_items) == 1:
        product_label = getattr(custom_items[0], "get_product_type_display", None)
        if callable(product_label):
            title = product_label()
        else:
            title = getattr(custom_items[0], "product_type", "") or "кастомний виріб"
        return f"Одяг бренду TwoComms, {title}"[:120]
    if total_qty > 1:
        return f"Одяг бренду TwoComms, у кількості {total_qty} шт."
    if custom_qty > 1:
        return f"Одяг бренду TwoComms, кастомних виробів {custom_qty} шт."
    return "Одяг бренду TwoComms"


class NovaPoshtaDocumentService:
    API_URL = "https://api.novaposhta.ua/v2.0/json/"
    REQUEST_TIMEOUT = 20
    SENDER_CITY_QUERY = "Харків"
    SENDER_WAREHOUSE_QUERY = "138"
    DEFAULT_WEIGHT = Decimal("1")
    DEFAULT_LENGTH_CM = Decimal("30")
    DEFAULT_WIDTH_CM = Decimal("20")
    DEFAULT_HEIGHT_CM = Decimal("8")

    def __init__(self) -> None:
        self.api_key = getattr(settings, "NOVA_POSHTA_API_KEY", "") or ""
        self.api_url = getattr(settings, "NOVA_POSHTA_API_URL", self.API_URL) or self.API_URL
        self.api_url = self.api_url.rstrip("/") + "/"
        self.directory = NovaPoshtaDirectoryService()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def build_initial_payload(self, order) -> dict[str, Any]:
        sender_point = self._resolve_default_sender_point()
        payment_snapshot = build_order_payment_snapshot(order)
        recipient_city = getattr(order, "city", "") or ""
        recipient_settlement_ref = getattr(order, "np_settlement_ref", "") or ""
        recipient_city_ref = getattr(order, "np_city_ref", "") or ""
        recipient_warehouse = getattr(order, "np_office", "") or ""
        recipient_warehouse_ref = getattr(order, "np_warehouse_ref", "") or ""
        recipient_city_token, recipient_warehouse_token = _build_point_tokens(
            city_label=recipient_city,
            settlement_ref=recipient_settlement_ref,
            city_ref=recipient_city_ref,
            warehouse_label=recipient_warehouse,
            warehouse_ref=recipient_warehouse_ref,
            warehouse_kind=_infer_warehouse_kind(recipient_warehouse),
        )
        sender_city_token, sender_warehouse_token = _build_point_tokens(
            city_label=sender_point.city_label,
            settlement_ref=sender_point.settlement_ref,
            city_ref=sender_point.city_ref,
            warehouse_label=sender_point.warehouse_label,
            warehouse_ref=sender_point.warehouse_ref,
            warehouse_kind=sender_point.warehouse_kind,
        )
        recipient_phone = normalize_phone(getattr(order, "phone", "") or "") or (getattr(order, "phone", "") or "")

        return {
            "recipient_full_name": getattr(order, "full_name", "") or "",
            "recipient_phone": recipient_phone,
            "recipient_city": recipient_city,
            "recipient_settlement_ref": recipient_settlement_ref,
            "recipient_city_ref": recipient_city_ref,
            "recipient_city_token": recipient_city_token,
            "recipient_warehouse": recipient_warehouse,
            "recipient_warehouse_ref": recipient_warehouse_ref,
            "recipient_warehouse_token": recipient_warehouse_token,
            "sender_city": sender_point.city_label,
            "sender_settlement_ref": sender_point.settlement_ref,
            "sender_city_ref": sender_point.city_ref,
            "sender_city_token": sender_city_token,
            "sender_warehouse": sender_point.warehouse_label,
            "sender_warehouse_ref": sender_point.warehouse_ref,
            "sender_warehouse_token": sender_warehouse_token,
            "description": build_waybill_description(order),
            "declared_cost": payment_snapshot["declared_cost"],
            "weight": "1.0",
            "seats_amount": "1",
            "length_cm": f"{self.DEFAULT_LENGTH_CM}",
            "width_cm": f"{self.DEFAULT_WIDTH_CM}",
            "height_cm": f"{self.DEFAULT_HEIGHT_CM}",
            "cod_amount": payment_snapshot["cod_amount"] if payment_snapshot["cod_amount_value"] > 0 else "",
            "payer_type": "Recipient",
            "payment_method": "Cash",
        }

    def create_waybill(self, order, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured():
            raise NovaPoshtaDocumentError("NOVA_POSHTA_API_KEY не налаштований.")

        sender_profile = self._resolve_sender_profile()
        sender_point = self._resolve_point(
            city_label=payload.get("sender_city", ""),
            settlement_ref=payload.get("sender_settlement_ref", ""),
            city_ref=payload.get("sender_city_ref", ""),
            warehouse_label=payload.get("sender_warehouse", ""),
            warehouse_ref=payload.get("sender_warehouse_ref", ""),
            preferred_kind="branch",
            point_role="відправника",
        )
        recipient_point = self._resolve_point(
            city_label=payload.get("recipient_city", ""),
            settlement_ref=payload.get("recipient_settlement_ref", ""),
            city_ref=payload.get("recipient_city_ref", ""),
            warehouse_label=payload.get("recipient_warehouse", ""),
            warehouse_ref=payload.get("recipient_warehouse_ref", ""),
            preferred_kind="all",
            point_role="одержувача",
        )

        recipient_phone = normalize_phone_for_np(payload.get("recipient_phone", ""))
        if len(recipient_phone) != 12 or not recipient_phone.startswith("380"):
            raise NovaPoshtaDocumentError("Вкажіть коректний телефон одержувача у форматі +380XXXXXXXXX.")

        recipient_name = split_person_name(payload.get("recipient_full_name", ""))
        if not recipient_name["first_name"]:
            raise NovaPoshtaDocumentError("Вкажіть ПІБ одержувача.")

        recipient_ref, recipient_contact_ref = self._create_recipient_counterparty(
            recipient_point.city_ref,
            recipient_name,
            recipient_phone,
        )
        if not recipient_contact_ref:
            recipient_contact_ref = self._create_recipient_contact(
                recipient_ref,
                recipient_name,
                recipient_phone,
            )

        dimensions = self._normalize_dimensions(
            payload.get("length_cm"),
            payload.get("width_cm"),
            payload.get("height_cm"),
        )
        weight = self._normalize_decimal(payload.get("weight"), fallback=self.DEFAULT_WEIGHT, minimum=Decimal("0.1"))
        declared_cost = self._as_money(payload.get("declared_cost"))
        seats_amount = int(str(payload.get("seats_amount") or "1").strip() or "1")
        description = str(payload.get("description") or "").strip() or build_waybill_description(order)
        cod_amount = self._as_money(payload.get("cod_amount") or "0")
        self._validate_waybill_package(
            recipient_point=recipient_point,
            dimensions=dimensions,
            declared_cost=declared_cost,
            seats_amount=seats_amount,
        )

        method_properties = {
            "PayerType": str(payload.get("payer_type") or "Recipient").strip() or "Recipient",
            "PaymentMethod": str(payload.get("payment_method") or "Cash").strip() or "Cash",
            "DateTime": date.today().strftime("%d.%m.%Y"),
            "CargoType": "Parcel",
            "Weight": self._format_decimal(weight),
            "ServiceType": "WarehouseWarehouse",
            "SeatsAmount": str(max(seats_amount, 1)),
            "Description": description[:120],
            "Cost": self._format_money(declared_cost),
            "CitySender": sender_point.city_ref,
            "Sender": sender_profile["sender_ref"],
            "SenderAddress": sender_point.warehouse_ref,
            "ContactSender": sender_profile["contact_ref"],
            "SendersPhone": sender_profile["phone"],
            "CityRecipient": recipient_point.city_ref,
            "Recipient": recipient_ref,
            "RecipientAddress": recipient_point.warehouse_ref,
            "ContactRecipient": recipient_contact_ref,
            "RecipientsPhone": recipient_phone,
            "RecipientType": "PrivatePerson",
            "RecipientName": str(payload.get("recipient_full_name") or "").strip(),
            "VolumeGeneral": self._format_volume(*dimensions),
            "OptionsSeat": [
                {
                    "weight": self._format_decimal(weight),
                    "volumetricLength": self._format_decimal(dimensions[0], trim_zeroes=True),
                    "volumetricWidth": self._format_decimal(dimensions[1], trim_zeroes=True),
                    "volumetricHeight": self._format_decimal(dimensions[2], trim_zeroes=True),
                    "volumetricVolume": self._format_volume(*dimensions),
                }
            ],
        }

        if cod_amount > 0:
            method_properties["AfterpaymentOnGoodsCost"] = self._format_money(cod_amount)

        response = self._request("InternetDocument", "save", method_properties)
        result = next(iter(response.get("data") or []), None) or {}
        tracking_number = str(result.get("IntDocNumber") or "").strip()
        document_ref = str(result.get("Ref") or "").strip()
        if not tracking_number:
            self._delete_incomplete_waybill_safely(document_ref)
            raise NovaPoshtaDocumentError("Nova Poshta API не повернув номер ТТН.")
        if not document_ref:
            raise NovaPoshtaDocumentError("Nova Poshta API не повернув Ref створеної накладної.")

        return {
            "tracking_number": tracking_number,
            "document_ref": document_ref,
            "recipient_ref": recipient_ref,
            "recipient_contact_ref": recipient_contact_ref,
            "recipient_point": recipient_point,
            "sender_point": sender_point,
            "warnings": [str(item).strip() for item in response.get("warnings") or [] if str(item).strip()],
        }

    def delete_waybill(self, document_ref: str) -> dict[str, Any]:
        if not self.is_configured():
            raise NovaPoshtaDocumentError("NOVA_POSHTA_API_KEY не налаштований.")

        normalized_ref = str(document_ref or "").strip()
        if not normalized_ref:
            raise NovaPoshtaDocumentError("Не вказано Ref накладної Нова пошта для видалення.")

        response = self._request(
            "InternetDocument",
            "delete",
            {
                "DocumentRefs": normalized_ref,
            },
        )
        deleted = next(iter(response.get("data") or []), None)
        if not isinstance(deleted, dict):
            raise NovaPoshtaDocumentError("Nova Poshta API не підтвердив видалення накладної.")
        deleted_error = str(deleted.get("Error") or deleted.get("Errors") or "").strip()
        if deleted_error:
            raise NovaPoshtaDocumentError(deleted_error)
        deleted_ref = str(deleted.get("Ref") or "").strip()
        if not deleted_ref or deleted_ref != normalized_ref:
            raise NovaPoshtaDocumentError("Nova Poshta API не підтвердив видалення саме цієї накладної.")

        return {
            "document_ref": deleted_ref,
            "warnings": [str(item).strip() for item in response.get("warnings") or [] if str(item).strip()],
        }

    def _delete_incomplete_waybill_safely(self, document_ref: str) -> None:
        normalized_ref = str(document_ref or "").strip()
        if not normalized_ref:
            return
        try:
            self.delete_waybill(normalized_ref)
        except Exception:
            logger.exception(
                "Failed to delete Nova Poshta waybill after incomplete create response for document %s",
                normalized_ref,
            )

    def _resolve_sender_profile(self) -> dict[str, str]:
        configured_ref = (getattr(settings, "NOVA_POSHTA_SENDER_COUNTERPARTY_REF", "") or "").strip()
        configured_contact_ref = (getattr(settings, "NOVA_POSHTA_SENDER_CONTACT_REF", "") or "").strip()
        configured_phone = normalize_phone_for_np(getattr(settings, "NOVA_POSHTA_SENDER_PHONE", "") or "")
        if configured_ref and configured_contact_ref and configured_phone:
            return {
                "sender_ref": configured_ref,
                "contact_ref": configured_contact_ref,
                "phone": configured_phone,
            }

        senders = self._request(
            "Counterparty",
            "getCounterparties",
            {
                "CounterpartyProperty": "Sender",
                "Page": "1",
            },
        )
        sender = next(iter(senders.get("data") or []), None) or {}
        sender_ref = str(sender.get("Ref") or "").strip()
        if not sender_ref:
            raise NovaPoshtaDocumentError("У Nova Poshta API не знайдено відправника FOP/компанії.")

        contacts = self._request(
            "Counterparty",
            "getCounterpartyContactPersons",
            {"Ref": sender_ref},
        )
        contact = next(iter(contacts.get("data") or []), None) or {}
        contact_ref = str(contact.get("Ref") or "").strip()
        phone = normalize_phone_for_np(contact.get("Phones") or sender.get("Phone") or "")
        if not contact_ref or not phone:
            raise NovaPoshtaDocumentError(
                "Не вдалося отримати контактні дані відправника з Nova Poshta API."
            )

        return {
            "sender_ref": sender_ref,
            "contact_ref": contact_ref,
            "phone": phone,
        }

    def _resolve_default_sender_point(self) -> NovaPoshtaResolvedPoint:
        sender_city = (getattr(settings, "NOVA_POSHTA_SENDER_CITY", "") or self.SENDER_CITY_QUERY).strip()
        sender_warehouse = (getattr(settings, "NOVA_POSHTA_SENDER_WAREHOUSE", "") or self.SENDER_WAREHOUSE_QUERY).strip()
        return self._resolve_point(
            city_label=sender_city,
            settlement_ref=(getattr(settings, "NOVA_POSHTA_SENDER_SETTLEMENT_REF", "") or "").strip(),
            city_ref=(getattr(settings, "NOVA_POSHTA_SENDER_CITY_REF", "") or "").strip(),
            warehouse_label=sender_warehouse,
            warehouse_ref=(getattr(settings, "NOVA_POSHTA_SENDER_WAREHOUSE_REF", "") or "").strip(),
            preferred_kind="branch",
            point_role="відправника",
        )

    def _resolve_point(
        self,
        *,
        city_label: str,
        settlement_ref: str,
        city_ref: str,
        warehouse_label: str,
        warehouse_ref: str,
        preferred_kind: str,
        point_role: str,
    ) -> NovaPoshtaResolvedPoint:
        normalized_city = str(city_label or "").strip()
        normalized_warehouse = str(warehouse_label or "").strip()
        normalized_settlement_ref = str(settlement_ref or "").strip()
        normalized_city_ref = str(city_ref or "").strip()
        normalized_warehouse_ref = str(warehouse_ref or "").strip()

        if not normalized_city_ref and not normalized_settlement_ref:
            city = self._pick_city_candidate(normalized_city, point_role=point_role)
            normalized_settlement_ref = city.get("settlement_ref", "")
            normalized_city_ref = city.get("city_ref", "")
            normalized_city = city.get("label", normalized_city)

        warehouse = self._pick_warehouse_candidate(
            normalized_warehouse,
            settlement_ref=normalized_settlement_ref,
            city_ref=normalized_city_ref,
            warehouse_ref=normalized_warehouse_ref,
            preferred_kind=preferred_kind,
            point_role=point_role,
        )
        warehouse_city_ref = str(warehouse.get("city_ref") or "").strip()
        if not normalized_city_ref and warehouse_city_ref:
            normalized_city_ref = warehouse_city_ref
        if not normalized_city_ref:
            city = self._pick_city_candidate(normalized_city, point_role=point_role)
            normalized_settlement_ref = normalized_settlement_ref or city.get("settlement_ref", "")
            normalized_city_ref = city.get("city_ref", "") or normalized_city_ref
            normalized_city = city.get("label", normalized_city)
        if not normalized_city_ref:
            raise NovaPoshtaDocumentError(f"Не вдалося визначити Ref міста {point_role} в довіднику Нової пошти.")
        return NovaPoshtaResolvedPoint(
            city_label=normalized_city,
            warehouse_label=warehouse.get("label", normalized_warehouse or normalized_city),
            settlement_ref=normalized_settlement_ref,
            city_ref=normalized_city_ref,
            warehouse_ref=warehouse.get("ref", ""),
            warehouse_kind=warehouse.get("kind", "branch"),
        )

    def _pick_city_candidate(self, query: str, *, point_role: str) -> dict[str, str]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise NovaPoshtaDocumentError(f"Не вказано місто {point_role}.")

        items = self.directory.search_settlements(normalized_query, limit=10)
        if not items:
            raise NovaPoshtaDocumentError(f"Не вдалося знайти місто {point_role} в довіднику Нової пошти.")

        normalized_target = self._normalize_name(normalized_query)
        exact = [
            item for item in items
            if self._normalize_name(item.get("label")) == normalized_target
            or self._normalize_name(item.get("main_description")) == normalized_target
        ]
        if exact:
            return exact[0]

        for item in items:
            label = self._normalize_name(item.get("label"))
            if normalized_target and normalized_target in label:
                return item
        return items[0]

    def _pick_warehouse_candidate(
        self,
        query: str,
        *,
        settlement_ref: str,
        city_ref: str,
        warehouse_ref: str,
        preferred_kind: str,
        point_role: str,
    ) -> dict[str, str]:
        normalized_query = str(query or "").strip()
        normalized_warehouse_ref = str(warehouse_ref or "").strip()

        # When the order already carries a warehouse Ref (picked from the Nova
        # Poshta directory at checkout), we can ship using it directly: the
        # `InternetDocument.save` call only needs CityRecipient + RecipientAddress.
        # The directory `getWarehouses` endpoint is eventually-consistent and
        # intermittently returns empty/partial data, so we keep this trusted
        # fallback and never hard-fail when we already know the Ref.
        known_ref_candidate: dict[str, str] | None = None
        if normalized_warehouse_ref:
            known_ref_candidate = {
                "ref": normalized_warehouse_ref,
                "label": normalized_query,
                "kind": _infer_warehouse_kind(normalized_query) if normalized_query else "branch",
                "number": "".join(ch for ch in normalized_query if ch.isdigit()),
                "short_address": "",
                "description": "",
                "city_ref": str(city_ref or "").strip(),
            }
            items = self.directory.search_warehouses(
                settlement_ref=settlement_ref,
                city_ref=city_ref,
                kind="all",
                limit=50,
            )
            for item in items:
                if str(item.get("ref") or "").strip() == normalized_warehouse_ref:
                    return item

        if not normalized_query:
            if known_ref_candidate:
                return known_ref_candidate
            raise NovaPoshtaDocumentError(f"Не вказано відділення/поштомат {point_role}.")

        items = self.directory.search_warehouses(
            settlement_ref=settlement_ref,
            city_ref=city_ref,
            query=normalized_query,
            kind=preferred_kind if preferred_kind in {"branch", "postomat"} else "all",
            limit=25,
        )
        if not items and preferred_kind != "all":
            items = self.directory.search_warehouses(
                settlement_ref=settlement_ref,
                city_ref=city_ref,
                query=normalized_query,
                kind="all",
                limit=25,
            )
        if not items:
            if known_ref_candidate:
                return known_ref_candidate
            raise NovaPoshtaDocumentError(
                f"Не вдалося знайти відділення/поштомат {point_role} в довіднику Нової пошти."
            )

        # Prefer the directory entry that matches the trusted Ref (keeps the real
        # warehouse kind for postomat package validation).
        if normalized_warehouse_ref:
            for item in items:
                if str(item.get("ref") or "").strip() == normalized_warehouse_ref:
                    return item

        number = "".join(ch for ch in normalized_query if ch.isdigit())
        if number:
            for item in items:
                if str(item.get("number") or "").strip() == number:
                    return item

        normalized_target = self._normalize_name(normalized_query)
        exact = [
            item for item in items
            if normalized_target in {
                self._normalize_name(item.get("label")),
                self._normalize_name(item.get("description")),
                self._normalize_name(item.get("short_address")),
            }
        ]
        if exact:
            return exact[0]

        for item in items:
            haystack = " ".join(
                self._normalize_name(item.get(key))
                for key in ("label", "description", "short_address")
            )
            if normalized_target and normalized_target in haystack:
                return item
        if known_ref_candidate:
            return known_ref_candidate
        return items[0]

    def _create_recipient_counterparty(
        self,
        city_ref: str,
        person_name: dict[str, str],
        phone: str,
    ) -> tuple[str, str]:
        response = self._request(
            "Counterparty",
            "save",
            {
                "CounterpartyProperty": "Recipient",
                "CityRef": city_ref,
                "CounterpartyType": "PrivatePerson",
                "FirstName": person_name["first_name"],
                "MiddleName": person_name["middle_name"],
                "LastName": person_name["last_name"],
                "Phone": phone,
            },
        )
        recipient = next(iter(response.get("data") or []), None) or {}
        recipient_ref = str(recipient.get("Ref") or "").strip()
        if not recipient_ref:
            raise NovaPoshtaDocumentError("Nova Poshta API не повернув Ref одержувача.")
        contact_ref = self._find_nested_ref(recipient.get("ContactPerson"))
        return recipient_ref, contact_ref

    def _create_recipient_contact(
        self,
        counterparty_ref: str,
        person_name: dict[str, str],
        phone: str,
    ) -> str:
        response = self._request(
            "ContactPerson",
            "save",
            {
                "CounterpartyRef": counterparty_ref,
                "FirstName": person_name["first_name"],
                "MiddleName": person_name["middle_name"],
                "LastName": person_name["last_name"],
                "Phone": phone,
            },
        )
        contact = next(iter(response.get("data") or []), None) or {}
        contact_ref = str(contact.get("Ref") or "").strip()
        if not contact_ref:
            contacts = self._request(
                "Counterparty",
                "getCounterpartyContactPersons",
                {"Ref": counterparty_ref},
            )
            contact = next(iter(contacts.get("data") or []), None) or {}
            contact_ref = str(contact.get("Ref") or "").strip()
        if not contact_ref:
            raise NovaPoshtaDocumentError("Nova Poshta API не повернув контактну особу одержувача.")
        return contact_ref

    def _request(self, model_name: str, called_method: str, method_properties: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "apiKey": self.api_key,
            "modelName": model_name,
            "calledMethod": called_method,
            "methodProperties": method_properties or {},
        }
        try:
            response = requests.post(self.api_url, json=payload, timeout=self.REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as exc:
            logger.warning("Nova Poshta document request failed for %s.%s: %s", model_name, called_method, exc)
            raise NovaPoshtaDocumentError("Не вдалося зв’язатися з Nova Poshta API.") from exc
        except ValueError as exc:
            logger.warning("Nova Poshta document request returned invalid JSON for %s.%s", model_name, called_method)
            raise NovaPoshtaDocumentError("Nova Poshta API повернув некоректну відповідь.") from exc

        errors = [str(item).strip() for item in data.get("errors") or [] if str(item).strip()]
        if errors:
            raise NovaPoshtaDocumentError("; ".join(errors))
        if not data.get("success"):
            raise NovaPoshtaDocumentError("Nova Poshta API не підтвердив створення накладної.")
        return data

    def _validate_waybill_package(
        self,
        *,
        recipient_point: NovaPoshtaResolvedPoint,
        dimensions: tuple[Decimal, Decimal, Decimal],
        declared_cost: Decimal,
        seats_amount: int,
    ) -> None:
        if seats_amount != 1:
            raise NovaPoshtaDocumentError("Автоматичне створення ТТН зараз підтримує тільки одне місце.")

        if recipient_point.warehouse_kind != "postomat":
            return

        length_cm, width_cm, height_cm = dimensions
        if declared_cost > Decimal("10000"):
            raise NovaPoshtaDocumentError("Для поштомата оголошена вартість не може перевищувати 10000 грн.")
        if length_cm > Decimal("60") or width_cm > Decimal("40") or height_cm > Decimal("30"):
            raise NovaPoshtaDocumentError(
                "Для поштомата габарити не можуть перевищувати 60x40x30 см."
            )

    @staticmethod
    def _normalize_name(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @classmethod
    def _find_nested_ref(cls, value: Any) -> str:
        if isinstance(value, dict):
            ref = str(value.get("Ref") or "").strip()
            if ref:
                return ref
            for nested in value.values():
                found = cls._find_nested_ref(nested)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = cls._find_nested_ref(item)
                if found:
                    return found
        return ""

    @staticmethod
    def _normalize_decimal(
        value: Any,
        *,
        fallback: Decimal,
        minimum: Decimal = Decimal("0"),
    ) -> Decimal:
        try:
            normalized = Decimal(str(value if value not in (None, "") else fallback))
        except (InvalidOperation, ValueError, TypeError):
            normalized = fallback
        if normalized < minimum:
            return fallback
        return normalized

    @staticmethod
    def _as_money(value: Any) -> Decimal:
        try:
            normalized = Decimal(str(value if value not in (None, "") else "0"))
        except (InvalidOperation, ValueError, TypeError):
            normalized = Decimal("0")
        return normalized.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _normalize_dimensions(self, length: Any, width: Any, height: Any) -> tuple[Decimal, Decimal, Decimal]:
        return (
            self._normalize_decimal(length, fallback=self.DEFAULT_LENGTH_CM, minimum=Decimal("1")),
            self._normalize_decimal(width, fallback=self.DEFAULT_WIDTH_CM, minimum=Decimal("1")),
            self._normalize_decimal(height, fallback=self.DEFAULT_HEIGHT_CM, minimum=Decimal("1")),
        )

    @staticmethod
    def _format_decimal(value: Decimal, *, trim_zeroes: bool = False) -> str:
        text = format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        if trim_zeroes:
            return text.rstrip("0").rstrip(".") or "0"
        return text.rstrip("0").rstrip(".") if "." in text else text

    @classmethod
    def _format_money(cls, value: Decimal) -> str:
        return cls._format_decimal(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @classmethod
    def _format_volume(cls, length_cm: Decimal, width_cm: Decimal, height_cm: Decimal) -> str:
        volume = (length_cm * width_cm * height_cm) / Decimal("1000000")
        return cls._format_decimal(volume.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
