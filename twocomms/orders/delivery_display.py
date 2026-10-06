"""Human-friendly Nova Poshta delivery point presentation.

The order schema intentionally stores the provider's canonical point label in
``np_office``.  This module keeps presentation concerns out of templates and
notifications while remaining backwards compatible with older labels that do
not contain an explicit point type.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any


_POSTOMAT_RE = re.compile(r"(?:поштомат|почтомат|постамат|postomat|parcel\s*locker|locker)\s*(?:№|#)?\s*(\d{1,8})?", re.IGNORECASE)
_BRANCH_RE = re.compile(r"(?:відділен\w*|відд\.?|отделен\w*|office|branch)\s*(?:№|#)?\s*(\d{1,8})?", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?:№|#)\s*(\d{1,8})\b")
_NOVA_POSHTA_RE = re.compile(r"(?:нова|новая)\s+пошт\w*|новапошт\w*", re.IGNORECASE)
_STANDALONE_NUMBER_RE = re.compile(r"^\s*(?:№|#)?\s*(\d{1,8})\s*$")
_TERSE_LONG_NUMBER_RE = re.compile(r"\b(\d{4,8})\b")
_POINT_PREFIX_RE = re.compile(
    r"^\s*(?:поштомат|почтомат|постамат|postomat|parcel\s*locker|locker|відділен\w*|відд\.?|отделен\w*|office|branch)"
    r"\s*(?:№|#)?\s*\d{0,8}\s*(?:\([^)]*\))?\s*[,.:;\-–—]*\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NovaPoshtaPointDisplay:
    city: str
    raw_label: str
    kind: str
    kind_label: str
    icon: str
    number: str
    title: str
    address: str

    @property
    def telegram_text(self) -> str:
        """Compact, escaped HTML-safe block for Telegram messages."""
        city = html.escape(self.city or "—")
        if self.kind == "handover":
            return f"{self.icon} <b>{html.escape(self.kind_label)}</b>\n   {html.escape(self.address)}"
        if self.kind == "missing":
            city_line = f"\n   📍 {city}" if self.city else ""
            return f"{self.icon} <b>{html.escape(self.kind_label)}</b>{city_line}"
        title = html.escape(self.title or self.raw_label or "—")
        address = html.escape(self.address or self.raw_label or "—")
        if self.number:
            number_badge = f" · <b>№ {html.escape(self.number)}</b>"
        elif self.kind in {"address", "manual", "handover"}:
            number_badge = ""
        else:
            number_badge = " · <b>номер не вказано</b>"
        return (
            f"{self.icon} <b>{html.escape(self.kind_label)}</b>{number_badge}\n"
            f"   📍 {city}\n"
            f"   {title}\n"
            f"   <i>{address}</i>"
        )

    @property
    def telegram_pre_lines(self) -> str:
        """Fixed-width-friendly lines used by the admin order card."""
        city = html.escape(self.city or "—")
        if self.kind == "handover":
            return f"│     {self.icon} {html.escape(self.kind_label)}\n│     {html.escape(self.address)}\n"
        if self.kind == "missing":
            city_line = f"│     📍 Місто: {city}\n" if self.city else ""
            return f"│     {self.icon} {html.escape(self.kind_label)}\n{city_line}"
        address = html.escape(self.raw_label or self.address or "—")
        number_line = f"│     Номер: № {html.escape(self.number)}\n" if self.number else ""
        return (
            f"│     {self.icon} Тип: {html.escape(self.kind_label)}\n"
            f"{number_line}"
            f"│     📍 Місто: {city}\n"
            f"│     Адреса: {address}\n"
        )


def build_nova_poshta_point(city: Any = "", label: Any = "", *, kind: Any = "") -> NovaPoshtaPointDisplay:
    """Build a display model from current or legacy order delivery fields."""
    city_text = " ".join(str(city or "").split())
    raw_label = " ".join(str(label or "").split())
    standalone_number_match = _STANDALONE_NUMBER_RE.match(raw_label)
    terse_number_match = _TERSE_LONG_NUMBER_RE.search(raw_label) if len(raw_label) <= 20 else None
    normalized_kind = str(kind or "").strip().lower()
    if not raw_label:
        normalized_kind = "missing"
    elif normalized_kind not in {"branch", "postomat", "address", "missing"}:
        if _POSTOMAT_RE.search(raw_label.lower()):
            normalized_kind = "postomat"
        elif _BRANCH_RE.search(raw_label.lower()) or _NOVA_POSHTA_RE.search(raw_label.lower()):
            normalized_kind = "branch"
        elif standalone_number_match:
            normalized_kind = "postomat" if len(standalone_number_match.group(1)) >= 4 else "branch"
        elif terse_number_match:
            normalized_kind = "postomat"
        else:
            normalized_kind = "address"

    matcher = _POSTOMAT_RE if normalized_kind == "postomat" else _BRANCH_RE
    match = matcher.search(raw_label)
    marker_match = match or _NOVA_POSHTA_RE.search(raw_label)
    number = (match.group(1) if match and match.group(1) else "")
    if not number:
        number_match = _NUMBER_RE.search(raw_label)
        number = number_match.group(1) if number_match else ""
    if not number and marker_match:
        # Covers labels such as "Відділення: 4" and
        # "Поштомат Нова Пошта 21586" without guessing from street numbers.
        tail_match = re.match(r"^[^\d]{0,48}(\d{1,8})\b", raw_label[marker_match.end():])
        number = tail_match.group(1) if tail_match else ""
    if not number and normalized_kind == "postomat":
        leading_number = re.match(r"^\s*(\d{4,8})\b", raw_label)
        number = leading_number.group(1) if leading_number else (terse_number_match.group(1) if terse_number_match else "")
    # Very old manual orders sometimes stored only "1" / "21586".
    if normalized_kind == "address":
        number = ""
    elif not number and standalone_number_match:
        number = standalone_number_match.group(1)

    is_postomat = normalized_kind == "postomat"
    is_address = normalized_kind == "address"
    is_missing = normalized_kind == "missing"
    kind_label = (
        "Дані доставки не вказані"
        if is_missing
        else "Поштомат"
        if is_postomat
        else "Адресна доставка"
        if is_address
        else "Відділення"
    )
    icon = "⚠️" if is_missing else "📮" if is_postomat else "📍" if is_address else "🏢"
    if is_missing:
        title = kind_label
    elif number:
        title = f"{kind_label} № {number}"
    else:
        title = "Адреса доставки" if is_address else kind_label
    address = "" if is_missing else _POINT_PREFIX_RE.sub("", raw_label, count=1).strip()
    address = address or raw_label or ("" if is_missing else title)
    return NovaPoshtaPointDisplay(
        city=city_text,
        raw_label=raw_label,
        kind=normalized_kind,
        kind_label=kind_label,
        icon=icon,
        number=number,
        title=title,
        address=address,
    )


def get_order_nova_poshta_point(order: Any) -> NovaPoshtaPointDisplay:
    """Resolve the recipient point for an Order-like object."""
    method = getattr(order, "delivery_method", "nova_poshta") or "nova_poshta"
    if method in {"manual", "handover"}:
        is_handover = method == "handover"
        label = str(getattr(order, "handover_details", "") or "") if is_handover else str(getattr(order, "np_office", "") or "")
        title = "Передача з рук у руки" if is_handover else "Доставка вручну"
        return NovaPoshtaPointDisplay(
            city="" if is_handover else str(getattr(order, "city", "") or ""),
            raw_label=label,
            kind=method,
            kind_label=title,
            icon="🤝" if is_handover else "📍",
            number="",
            title=title,
            address=label or ("Місце й час узгоджуються" if is_handover else "Адреса не вказана"),
        )
    label = getattr(order, "np_office", "")
    explicit_kind = getattr(order, "np_warehouse_kind", "")
    if not explicit_kind and getattr(order, "np_warehouse_ref", "") and not (
        _POSTOMAT_RE.search(str(label or "")) or _BRANCH_RE.search(str(label or ""))
    ):
        explicit_kind = "branch"
    return build_nova_poshta_point(
        getattr(order, "city", ""),
        label,
        kind=explicit_kind,
    )


def get_dropshipper_nova_poshta_point(order: Any) -> NovaPoshtaPointDisplay:
    """Resolve the recipient point for a DropshipperOrder-like object."""
    label = getattr(order, "client_np_address", "")
    explicit_kind = getattr(order, "client_np_warehouse_kind", "")
    if not explicit_kind and getattr(order, "client_np_warehouse_ref", "") and not (
        _POSTOMAT_RE.search(str(label or "")) or _BRANCH_RE.search(str(label or ""))
    ):
        explicit_kind = "branch"
    return build_nova_poshta_point(
        getattr(order, "client_np_city", "") or getattr(order, "client_city", ""),
        label,
        kind=explicit_kind,
    )
