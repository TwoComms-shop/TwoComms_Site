import logging
import mimetypes
import os
from datetime import timedelta
from html import escape
from pathlib import Path

from django.utils import timezone

from orders.telegram_notifications import TelegramNotifier
from storefront.custom_print_config import (
    ADDON_LABELS,
    FABRIC_LABELS,
    FIT_LABELS,
    PRODUCT_LABELS,
    SERVICE_LABELS,
    SIZE_GRID,
    TELEGRAM_MANAGER_URL,
    TRIAGE_LABELS,
    ZONE_LABELS,
    build_placement_specs,
    resolve_color_label,
    resolve_fabric_badge,
    resolve_fabric_label,
    resolve_fit_label,
)


logger = logging.getLogger(__name__)

MAIN_PUBLIC_BASE_URL = "https://twocomms.shop"

# Минимальный интервал между Telegram-уведомлениями для одного лида.
# Защищает от шторма (race-conditions, двойные сабмиты, повторные рендеры корзины).
NOTIFICATION_THROTTLE_SECONDS = 90

# Telegram ограничивает подпись к документу 1024 символами.
TELEGRAM_CAPTION_LIMIT = 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_env(*keys: str) -> str:
    for key in keys:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _build_notifier() -> TelegramNotifier:
    return TelegramNotifier(
        bot_token=_first_env("DTF_TG_BOT_TOKEN"),
        chat_id=_first_env("DTF_TG_CHAT_ID", "TELEGRAM_CHAT_ID"),
        admin_id=_first_env("DTF_TG_ADMIN_ID", "TELEGRAM_ADMIN_ID"),
        async_enabled=False,
    )


def _placement_specs_for_lead(lead) -> list[dict]:
    placement_specs = getattr(lead, "placement_specs_json", None) or []
    if placement_specs:
        return [spec for spec in placement_specs if isinstance(spec, dict)]
    return [
        {
            "zone": zone,
            "placement_key": zone,
            "label": ZONE_LABELS.get(zone, zone),
        }
        for zone in (getattr(lead, "placements", None) or [])
    ]


def _format_placement_descriptor(spec: dict, *, include_text: bool) -> str:
    placement_key = spec.get("placement_key") or spec.get("zone")
    label = spec.get("label") or ZONE_LABELS.get(
        placement_key or spec.get("zone"), spec.get("zone") or "—"
    )
    parts = [label]
    if spec.get("size_preset"):
        parts.append(str(spec["size_preset"]).upper())
    elif spec.get("zone") == "sleeve":
        parts.append("текст" if spec.get("mode") == "full_text" else "A6")
    if include_text and spec.get("text"):
        parts.append(str(spec["text"]))
    return " · ".join(part for part in parts if part)


def _placement_descriptor_by_key(lead) -> dict[str, str]:
    mapping = {}
    for spec in _placement_specs_for_lead(lead):
        placement_key = spec.get("placement_key") or spec.get("zone")
        if not placement_key:
            continue
        mapping[placement_key] = _format_placement_descriptor(spec, include_text=False)
    return mapping


ZONE_EMOJI = {
    "front": "🔵",
    "back": "🔴",
    "sleeve": "🟡",
    "sleeve_left": "🟡",
    "sleeve_right": "🟡",
    "custom": "🟣",
}

# Формати, в яких прозорість неможлива в принципі.
_NO_ALPHA_SUFFIXES = {".jpg", ".jpeg", ".jfif", ".bmp", ".heic", ".heif"}
# Векторні/шарові формати — прозорість перевіряє дизайнер, не алармимо.
_DESIGN_SUFFIXES = {".svg", ".ai", ".eps", ".pdf", ".psd", ".tif", ".tiff"}


def _image_transparency_warning(file_path: str) -> str:
    """Повертає текст попередження, якщо у растровому файлі немає прозорості."""
    suffix = Path(file_path).suffix.lower()
    if suffix in _DESIGN_SUFFIXES:
        return ""
    if suffix in _NO_ALPHA_SUFFIXES:
        return f"формат {suffix.lstrip('.').upper()} не підтримує прозорий фон"
    if suffix in {".png", ".webp", ".gif", ".avif"}:
        try:
            from PIL import Image

            with Image.open(file_path) as im:
                has_alpha_channel = (
                    im.mode in ("RGBA", "LA", "PA")
                    or "transparency" in im.info
                )
                if not has_alpha_channel:
                    return "немає альфа-каналу (прозорого фону)"
                im.thumbnail((160, 160))
                alpha = im.convert("RGBA").getchannel("A")
                lo, _hi = alpha.getextrema()
                if lo >= 255:
                    return "усі пікселі непрозорі — фон буде надруковано"
        except Exception:
            return ""
    return ""


def _build_attachment_caption(
    lead, placement_key: str, index: int, total: int, *, transparency_note: str = "", file_name: str = ""
) -> str:
    """HTML-підпис до файла: одразу видно, на яку зону цей макет."""
    descriptor = _placement_descriptor_by_key(lead).get(placement_key) or ZONE_LABELS.get(
        placement_key, placement_key or "Файл"
    )
    zone_root = (placement_key or "").split("_")[0]
    emoji = ZONE_EMOJI.get(placement_key) or ZONE_EMOJI.get(zone_root) or "📎"
    lead_number = getattr(lead, "lead_number", "") or f"CP-{getattr(lead, 'pk', '')}"
    lines = [
        f"{emoji} <b>{escape(descriptor.upper())}</b> — файл {index}/{total}",
        f"<code>{escape(lead_number)}</code>",
    ]
    if file_name:
        lines.append(f"📎 <code>{escape(file_name)}</code>")
    if transparency_note:
        lines.append(f"⚠️ <i>{escape(transparency_note)}</i>")
    return "\n".join(lines)


def _is_image_attachment(attachment) -> bool:
    file_name = getattr(getattr(attachment, "file", None), "name", "")
    mime_type, _ = mimetypes.guess_type(file_name)
    if mime_type:
        return mime_type.startswith("image/")
    return Path(file_name).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _collect_attachment_payloads(lead) -> list[dict]:
    placement_index = {
        (spec.get("placement_key") or spec.get("zone")): idx
        for idx, spec in enumerate(_placement_specs_for_lead(lead))
    }
    attachments = list(getattr(lead.attachments, "all", lambda: [])())
    ordered_attachments = sorted(
        attachments,
        key=lambda attachment: (
            placement_index.get(getattr(attachment, "placement_zone", ""), 999),
            getattr(attachment, "sort_order", 0),
        ),
    )
    existing = []
    for attachment in ordered_attachments:
        file_path = getattr(getattr(attachment, "file", None), "path", "")
        if not file_path or not os.path.exists(file_path):
            continue
        existing.append(attachment)

    payloads = []
    total = len(existing)
    for index, attachment in enumerate(existing, start=1):
        file_path = getattr(getattr(attachment, "file", None), "path", "")
        transparency_note = _image_transparency_warning(file_path)
        payloads.append(
            {
                "path": file_path,
                "is_image": _is_image_attachment(attachment),
                "caption": _build_attachment_caption(
                    lead,
                    getattr(attachment, "placement_zone", ""),
                    index,
                    total,
                    transparency_note=transparency_note,
                    file_name=Path(file_path).name,
                ),
            }
        )
    return payloads


def _build_admin_panel_link(lead) -> str:
    return f"{MAIN_PUBLIC_BASE_URL}/admin-panel/?section=custom_print_orders&lead={lead.pk}"


def _build_moderation_action_url(lead, action: str) -> str:
    """Build absolute signed URL for telegram moderation action (approve/reject)."""
    from storefront.views.static_pages import _custom_print_action_signature

    lead.ensure_moderation_token()
    token = lead.moderation_token
    signature = _custom_print_action_signature(lead.pk, action, token)
    return (
        f"{MAIN_PUBLIC_BASE_URL}/custom-print/moderation/{lead.pk}/{action}/"
        f"?token={token}&sig={signature}"
    )


def _normalize_phone_digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit() or ch == "+")


def _build_contact_links(lead) -> dict[str, str]:
    """Возвращает набор готовых ссылок для быстрой связи с клиентом."""
    channel = (getattr(lead, "contact_channel", "") or "").strip().lower()
    raw_value = (getattr(lead, "contact_value", "") or "").strip()
    cleaned = raw_value.lstrip("@").replace(" ", "")

    links: dict[str, str] = {}

    # 1) Если клиент подтвердил Telegram через бота — используем его данные с приоритетом
    verified_id = getattr(lead, "telegram_verified_user_id", None)
    verified_username = (getattr(lead, "telegram_verified_username", "") or "").strip()
    verified_phone = (getattr(lead, "telegram_verified_phone", "") or "").strip()
    if verified_username:
        links["telegram"] = f"https://t.me/{verified_username.lstrip('@')}"
    elif verified_id:
        links["telegram"] = f"tg://user?id={verified_id}"
    if verified_phone:
        links["phone"] = f"tel:{_normalize_phone_digits(verified_phone)}"

    # 2) Канальные ссылки из contact_value (если ещё не выставлены)
    if "telegram" not in links and channel == "telegram" and cleaned:
        if cleaned.startswith("http"):
            links["telegram"] = cleaned
        else:
            links["telegram"] = f"https://t.me/{cleaned.lstrip('@')}"
    if channel == "whatsapp":
        digits = _normalize_phone_digits(raw_value).lstrip("+")
        if digits:
            links["whatsapp"] = f"https://wa.me/{digits}"
    if "phone" not in links and channel == "phone":
        digits = _normalize_phone_digits(raw_value)
        if digits:
            links["phone"] = f"tel:{digits}"

    # 3) Если в contact_value лежит номер при любом канале — даём кнопку звонка
    digits = _normalize_phone_digits(raw_value)
    if digits and "phone" not in links and len(digits) >= 9:
        links.setdefault("phone", f"tel:{digits}")

    return links


def _primary_contact_link(lead) -> str:
    """Возвращает основную ссылку для кнопки «Звʼязатися з клієнтом»."""
    links = _build_contact_links(lead)
    channel = (getattr(lead, "contact_channel", "") or "").strip().lower()
    return links.get(channel) or next(iter(links.values()), TELEGRAM_MANAGER_URL)


def _channel_display(lead) -> str:
    channel = (getattr(lead, "contact_channel", "") or "").strip().lower()
    return {"telegram": "Telegram", "whatsapp": "WhatsApp", "phone": "Телефон"}.get(
        channel, getattr(lead, "get_contact_channel_display", lambda: "")() or "—"
    )


def _channel_emoji(lead) -> str:
    channel = (getattr(lead, "contact_channel", "") or "").strip().lower()
    return {"telegram": "✈️", "whatsapp": "💬", "phone": "📞"}.get(channel, "📨")


def _pricing_text(lead) -> str:
    snapshot = getattr(lead, "pricing_snapshot_json", None) or {}
    if not snapshot:
        return "Уточнюється менеджером"

    final_total = snapshot.get("final_total")
    estimate_required = snapshot.get("estimate_required")
    design_price = snapshot.get("design_price")
    print_price = snapshot.get("print_price")
    zones_price = snapshot.get("zones_price")
    gift_price = snapshot.get("gift_price")
    discount_percent = snapshot.get("discount_percent")

    parts = []
    if snapshot.get("base_price") not in (None, ""):
        parts.append(f"база {snapshot['base_price']} грн")
    if design_price:
        parts.append(f"дизайнер +{design_price} грн")
    if print_price:
        parts.append(f"формати друку +{print_price} грн")
    if zones_price:
        parts.append(f"додаткові зони +{zones_price} грн")
    if gift_price:
        parts.append(f"подарунок +{gift_price} грн")
    if discount_percent:
        parts.append(f"знижка {discount_percent}%")
    if estimate_required:
        parts.append("потрібен точний прорахунок")
    elif final_total not in (None, ""):
        parts.append(f"разом {final_total} грн")
    return ", ".join(parts) or "Уточнюється менеджером"


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------


def _info_reply_markup(lead):
    if lead is None:
        return None

    rows = [[
        {"text": "🗂 Відкрити в панелі", "url": _build_admin_panel_link(lead)},
    ]]

    contact_url = _primary_contact_link(lead)
    if contact_url and contact_url != TELEGRAM_MANAGER_URL:
        emoji = _channel_emoji(lead)
        rows.append([
            {"text": f"{emoji} Звʼязатися з клієнтом", "url": contact_url},
        ])

    return {"inline_keyboard": rows}


def _moderation_reply_markup(lead):
    """Approve / Reject / Contact / Open keyboard for the manager's chat."""
    if lead is None:
        return None
    approve_url = _build_moderation_action_url(lead, "approve")
    reject_url = _build_moderation_action_url(lead, "reject")
    links = _build_contact_links(lead)

    rows = [
        [
            {"text": "✅ Погодити", "url": approve_url},
            {"text": "❌ Відхилити", "url": reject_url},
        ]
    ]
    contact_row = []
    if links.get("phone"):
        contact_row.append({"text": "📞 Зателефонувати", "url": links["phone"]})
    if links.get("telegram"):
        contact_row.append({"text": "✈️ Telegram", "url": links["telegram"]})
    if links.get("whatsapp"):
        contact_row.append({"text": "💬 WhatsApp", "url": links["whatsapp"]})
    if contact_row:
        # По 2 кнопки в строку максимум
        for i in range(0, len(contact_row), 2):
            rows.append(contact_row[i : i + 2])
    rows.append([{"text": "🗂 Відкрити в панелі", "url": _build_admin_panel_link(lead)}])
    return {"inline_keyboard": rows}


def _info_reply_markup_full(lead):
    """Inline keyboard for «новая заявка» (без approve/reject) с быстрыми контактами."""
    if lead is None:
        return None
    links = _build_contact_links(lead)
    rows = []
    contact_row = []
    if links.get("phone"):
        contact_row.append({"text": "📞 Зателефонувати", "url": links["phone"]})
    if links.get("telegram"):
        contact_row.append({"text": "✈️ Telegram", "url": links["telegram"]})
    if links.get("whatsapp"):
        contact_row.append({"text": "💬 WhatsApp", "url": links["whatsapp"]})
    if contact_row:
        for i in range(0, len(contact_row), 2):
            rows.append(contact_row[i : i + 2])
    rows.append([{"text": "🗂 Відкрити в панелі", "url": _build_admin_panel_link(lead)}])
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Message body
# ---------------------------------------------------------------------------


def _bold(label: str, value: str | int) -> str:
    """Single-line `• <b>Label:</b> value` block."""
    return f"• <b>{escape(str(label))}:</b> {escape(str(value))}"


def _section_header(emoji: str, title: str) -> str:
    return f"{emoji} <b>{escape(title)}</b>"


def _format_lead_attachments_summary(lead) -> str:
    attachments = list(getattr(lead.attachments, "all", lambda: [])()) if getattr(lead, "pk", None) else []
    if not attachments:
        return "❗ клієнт не завантажив жодного файла"

    images = sum(1 for a in attachments if _is_image_attachment(a))
    docs = len(attachments) - images
    parts = []
    if images:
        parts.append(f"{images} фото")
    if docs:
        parts.append(f"{docs} документ(и)")
    return ", ".join(parts) or f"{len(attachments)} файл(ів)"


def _attachments_by_zone(lead) -> dict[str, list]:
    """Группирует attachment по placement_zone (или placement_key)."""
    if not getattr(lead, "pk", None):
        return {}
    grouped: dict[str, list] = {}
    for attachment in getattr(lead.attachments, "all", lambda: [])():
        zone = getattr(attachment, "placement_zone", "") or ""
        grouped.setdefault(zone, []).append(attachment)
    return grouped


def _sizes_display(lead) -> str:
    """Розміри замовлення: спочатку sizes_note, потім size_breakdown зі снапшота."""
    note = (getattr(lead, "sizes_note", "") or "").strip()
    if note:
        return note

    draft = getattr(lead, "config_draft_json", None) or {}
    breakdown = {}
    if isinstance(draft, dict):
        breakdown = ((draft.get("order") or {}).get("size_breakdown")) or {}
    if isinstance(breakdown, dict):
        order_index = {size: idx for idx, size in enumerate(SIZE_GRID)}
        items = []
        for size, qty in sorted(
            breakdown.items(), key=lambda kv: order_index.get(str(kv[0]).upper(), 99)
        ):
            try:
                qty = int(qty)
            except (TypeError, ValueError):
                continue
            if qty > 0:
                items.append(f"{size} — {qty} шт")
        if items:
            return ", ".join(items)

    if (getattr(lead, "size_mode", "") or "") == "manager":
        return "Уточнити з менеджером"
    return ""


def _format_quantity_sizes_block(lead) -> list[str]:
    rows = [_bold("Кількість", f"{lead.quantity} шт")]
    sizes_text = _sizes_display(lead)
    if sizes_text:
        size_mode_suffix = (
            f" ({lead.get_size_mode_display()})" if getattr(lead, "size_mode", "") else ""
        )
        rows.append(
            f"• <b>Розміри:</b> <b>{escape(sizes_text)}</b>{escape(size_mode_suffix)}"
        )
    else:
        rows.append("• ❗️ <b>Розміри не вказано — обовʼязково уточніть у клієнта!</b>")
    return rows


def _format_pricing_block(lead) -> list[str]:
    final_value = getattr(lead, "final_price_value", 0)
    try:
        from decimal import Decimal

        final_value = Decimal(str(final_value or 0))
    except Exception:
        final_value = 0

    rows = [_bold("Прорахунок", _pricing_text(lead))]
    if final_value and float(final_value) > 0:
        rows.append(_bold("Підсумок із розрахунку", f"{final_value} грн"))
    return rows


def _format_product_block(lead) -> list[str]:
    product_type = getattr(lead, "product_type", "") or ""
    fit_value = getattr(lead, "fit", "") or ""
    fabric_value = getattr(lead, "fabric", "") or ""
    color_value = getattr(lead, "color_choice", "") or ""

    parts = [escape(lead.get_product_type_display())]
    fit_label = resolve_fit_label(product_type, fit_value)
    if fit_label:
        parts.append(escape(fit_label))
    fabric_label = resolve_fabric_label(product_type, fit_value, fabric_value)
    if fabric_label:
        parts.append(escape(fabric_label))
    color_info = resolve_color_label(product_type, color_value, fabric_value)
    if color_info["label"]:
        color_text = color_info["label"]
        if color_info["hex"]:
            color_text = f"{color_text} ({color_info['hex']})"
        parts.append(escape(color_text))

    rows = [f"• <b>Виріб:</b> {' / '.join(parts)}"]

    # Окремий highlighted-рядок для тканини, щоб у Telegram-чаті
    # менеджер одразу бачив тип (преміум/термо/стандарт).
    fabric_badge = resolve_fabric_badge(fabric_value)
    if fabric_label and fabric_badge:
        emoji = fabric_badge.get("emoji") or ""
        note = fabric_badge.get("note") or ""
        fabric_line = f"• <b>Тип тканини:</b> {emoji} <b>{escape(fabric_label)}</b>"
        if note:
            fabric_line += f" <i>({escape(note)})</i>"
        rows.append(fabric_line)
    elif fabric_label:
        rows.append(f"• <b>Тип тканини:</b> <b>{escape(fabric_label)}</b>")

    # Окремий рядок для кольору з hex-кодом, щоб був чіткий візуал.
    if color_info["label"]:
        color_line = f"• <b>Колір виробу:</b> <b>{escape(color_info['label'])}</b>"
        if color_info["hex"]:
            color_line += f" <code>{escape(color_info['hex'])}</code>"
        rows.append(color_line)

    if getattr(lead, "add_ons", None):
        mapped_addons = [ADDON_LABELS.get(a, a) for a in lead.add_ons]
        if mapped_addons:
            rows.append(_bold("Доповнення", ", ".join(mapped_addons)))
    if getattr(lead, "garment_note", ""):
        rows.append(_bold("Опис виробу", lead.garment_note))
    if getattr(lead, "file_triage_status", ""):
        rows.append(_bold("Triage", TRIAGE_LABELS.get(lead.file_triage_status, lead.file_triage_status)))
    return rows


def _format_placement_block(lead) -> list[str]:
    """Зоны нанесения с явным отображением «есть / нет файла» для каждой зоны.

    Логика:
    - Если в спецификации зоны указано «требуется файл» (`requires_artwork_file`)
      и для зоны нет attachment — добавляем красную метку «❗ файл не завантажено».
    - Если файл есть — «✅ файл прикріплено (×N)».
    - Если зона текстовая (full_text) — отдельная пометка «текст-режим, файл не потрібен».
    """
    specs = _placement_specs_for_lead(lead)
    files_by_zone = _attachments_by_zone(lead)

    rows = ["• <b>Зони друку:</b>"] if specs else ["• <b>Зони друку:</b> —"]

    for spec in specs:
        descriptor = _format_placement_descriptor(spec, include_text=True)
        placement_key = spec.get("placement_key") or spec.get("zone") or ""
        files_here = files_by_zone.get(placement_key, []) or files_by_zone.get(spec.get("zone") or "", [])
        is_text_only = spec.get("mode") == "full_text"
        requires_file = spec.get("requires_artwork_file")
        if requires_file is None:
            requires_file = not is_text_only

        if is_text_only:
            file_marker = "📝 текст-режим"
        elif files_here:
            file_marker = f"✅ файл прикріплено (×{len(files_here)})"
        elif requires_file:
            file_marker = "❗ файл не завантажено"
        else:
            file_marker = "ℹ️ файл не потрібен"

        zone_root = (placement_key or "").split("_")[0]
        zone_emoji = ZONE_EMOJI.get(placement_key) or ZONE_EMOJI.get(zone_root) or "◦"
        rows.append(f"  {zone_emoji} <b>{escape(descriptor)}</b> — <i>{file_marker}</i>")

    if getattr(lead, "placement_note", ""):
        rows.append(_bold("Примітка по зонам", lead.placement_note))

    rows.append(_bold("Файли всього", _format_lead_attachments_summary(lead)))
    total_attachments = (
        len(list(getattr(lead.attachments, "all", lambda: [])())) if getattr(lead, "pk", None) else 0
    )
    if total_attachments:
        rows.append(
            "  <i>↓ файли надіслано окремими документами (без стиснення), "
            "у підписі кожного — зона з тим самим маркером</i>"
        )

    # Аларм: «маю готовий файл», але нічого не завантажено
    service_kind = (getattr(lead, "service_kind", "") or "").strip()
    total_files = len(list(getattr(lead.attachments, "all", lambda: [])())) if getattr(lead, "pk", None) else 0
    if service_kind == "ready" and total_files == 0:
        rows.append(
            "  ⚠️ <b>Клієнт обрав «маю готовий файл», але не прикріпив його — потрібно уточнити.</b>"
        )
    elif service_kind == "adjust" and total_files == 0:
        rows.append(
            "  ⚠️ <b>Клієнт обрав «потрібно допрацювати файл», але файл відсутній — попросіть надіслати.</b>"
        )

    return rows


def _format_contact_block(lead) -> list[str]:
    raw_contact = (getattr(lead, "contact_value", "") or "").strip()
    rows = [
        _bold("Імʼя", lead.name or "—"),
        _bold("Канал звʼязку", _channel_display(lead)),
        _bold("Контакт", raw_contact or "—"),
    ]
    digits = _normalize_phone_digits(raw_contact)
    channel = (getattr(lead, "contact_channel", "") or "").strip().lower()
    if digits and len(digits) >= 9 and channel != "phone":
        rows.append(_bold("Телефон у контакті", digits))

    # Verified Telegram (через бота поделился контактом)
    if getattr(lead, "telegram_verified_at", None):
        verified_username = (getattr(lead, "telegram_verified_username", "") or "").strip()
        verified_phone = (getattr(lead, "telegram_verified_phone", "") or "").strip()
        verified_id = getattr(lead, "telegram_verified_user_id", None)
        verified_pieces = []
        if verified_username:
            verified_pieces.append(f"@{verified_username.lstrip('@')}")
        if verified_phone:
            verified_pieces.append(verified_phone)
        if verified_id:
            verified_pieces.append(f"id:{verified_id}")
        verified_text = " · ".join(verified_pieces) or "так"
        rows.append(f"• ✅ <b>Telegram підтверджено через бота:</b> {escape(verified_text)}")
    elif channel == "telegram":
        # Канал Telegram, но клиент не верифицировался через бота — это потенциальный случай,
        # когда вместо @username пишут email или непонятный текст. Подсветим менеджеру.
        rows.append(
            "• ⚠️ <i>Telegram не підтверджено через бота — перевірте контакт перед дзвінком.</i>"
        )

    if getattr(lead, "brand_name", ""):
        rows.append(_bold("Бренд / команда", lead.brand_name))
    return rows


SECTION_DIVIDER = "─────────────────"


def _format_created_at(lead) -> str:
    created_at = getattr(lead, "created_at", None)
    if not created_at:
        return ""
    try:
        return timezone.localtime(created_at).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def _build_lead_message(lead, *, header_emoji: str, header_title: str, intro_lines: list[str] | None = None) -> str:
    """Compact, ordered, manager-friendly Telegram message."""
    intro_lines = intro_lines or []
    lead_number = lead.lead_number or f"CP-{lead.pk}"
    created_text = _format_created_at(lead)
    header_meta = f"<code>{escape(lead_number)}</code>"
    if created_text:
        header_meta += f" · 🕐 {escape(created_text)}"

    parts = [
        f"{header_emoji} <b>{escape(header_title.upper())}</b>",
        header_meta,
    ]

    if intro_lines:
        parts.extend(["", *intro_lines])

    parts.extend([
        SECTION_DIVIDER,
        _section_header("👤", "Клієнт"),
        *_format_contact_block(lead),
        SECTION_DIVIDER,
        _section_header("👕", "Виріб"),
        *_format_product_block(lead),
        SECTION_DIVIDER,
        _section_header("🎨", "Друк і макети"),
        *_format_placement_block(lead),
        SECTION_DIVIDER,
        _section_header("📦", "Кількість і розміри"),
        *_format_quantity_sizes_block(lead),
        SECTION_DIVIDER,
        _section_header("💰", "Ціна"),
        *_format_pricing_block(lead),
    ])

    if getattr(lead, "brief", ""):
        brief = lead.brief.strip()
        if brief:
            parts.extend([
                SECTION_DIVIDER,
                _section_header("📝", "Бриф"),
                f"<blockquote>{escape(brief[:1500])}</blockquote>",
            ])

    if getattr(lead, "client_kind", "") == "brand":
        parts.append("")
        parts.append(_bold("Сценарій", lead.get_client_kind_display()))
        if getattr(lead, "business_kind", ""):
            parts.append(_bold("B2B", lead.get_business_kind_display()))

    parts.append("")
    parts.append(f'🔗 <a href="{_build_admin_panel_link(lead)}">Відкрити заявку в панелі</a>')

    return "\n".join(parts)


def _build_message(lead) -> str:
    """Backward-compatible wrapper (used in tests)."""
    return _build_lead_message(
        lead,
        header_emoji="🆕",
        header_title="Нова заявка на кастомний принт",
    )


def _build_safe_exit_message(snapshot: dict, lead=None) -> str:
    artwork = snapshot.get("artwork") or {}
    order = snapshot.get("order") or {}
    ui = snapshot.get("ui") or {}
    contact = snapshot.get("contact") or {}
    channel = (contact.get("channel") or "").strip()
    channel_label = {
        "telegram": "Telegram",
        "whatsapp": "WhatsApp",
        "phone": "Телефон",
    }.get(channel, "Не вказано")

    parts = [
        "🚪 <b>Кастомний принт: пользователь покинул конфігуратор</b>",
    ]
    if lead is not None:
        parts.append(f"<code>{escape(lead.lead_number)}</code>")

    parts.extend([
        "",
        _section_header("👤", "Контакт"),
        _bold("Імʼя", (contact.get("name") or "").strip() or "—"),
        _bold("Канал звʼязку", channel_label),
        _bold("Контакт", (contact.get("value") or "").strip() or "—"),
        "",
        _section_header("👕", "Замовлення"),
        _bold("Виріб", _snapshot_product_label(snapshot)),
        _bold("Послуга", SERVICE_LABELS.get(artwork.get("service_kind"), artwork.get("service_kind") or "—")),
        _bold("Зони", _snapshot_placements_text(snapshot)),
        _bold("Кількість", str(order.get("quantity") or "—")),
        _bold("Розміри", order.get("sizes_note") or "—"),
        "",
        _section_header("💰", "Прорахунок"),
        _bold("Розрахунок", _snapshot_pricing_text(snapshot)),
        _bold("Поточний крок", ui.get("current_step") or "—"),
    ])

    if order.get("gift"):
        parts.append("")
        parts.append("🎁 <b>Подарунок:</b> так")

    if lead is not None:
        parts.append("")
        parts.append(f'🔗 <a href="{_build_admin_panel_link(lead)}">Відкрити заявку в панелі</a>')
    else:
        parts.append("")
        parts.append(f'<a href="{TELEGRAM_MANAGER_URL}">Відкрити чат менеджера</a>')

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Snapshot helpers (для safe-exit без записанного лида)
# ---------------------------------------------------------------------------


def _snapshot_product_label(snapshot: dict) -> str:
    product = snapshot.get("product") or {}
    product_type = product.get("type") or "hoodie"
    base = PRODUCT_LABELS.get(product_type, product_type)
    details = []
    fit = (product.get("fit") or "").strip()
    fabric = (product.get("fabric") or "").strip()
    color = (product.get("color") or "").strip()
    if fit:
        details.append(resolve_fit_label(product_type, fit))
    if fabric:
        details.append(resolve_fabric_label(product_type, fit, fabric))
    if color:
        color_info = resolve_color_label(product_type, color, fabric)
        if color_info["label"]:
            details.append(color_info["label"])
    if not details:
        return base
    return f"{base} · {' / '.join(filter(None, details))}"


def _snapshot_placements_text(snapshot: dict) -> str:
    print_payload = snapshot.get("print") or {}
    add_ons = print_payload.get("add_ons") or []
    parts = []
    placement_specs = build_placement_specs(snapshot)
    if placement_specs:
        items = []
        for spec in placement_specs:
            label = spec.get("label") or ZONE_LABELS.get(
                spec.get("placement_key") or spec.get("zone"), spec.get("zone") or "—"
            )
            if spec.get("size_preset"):
                label = f"{label} · {spec['size_preset']}"
            elif spec.get("zone") == "sleeve":
                label = f"{label} · {'текст' if spec.get('mode') == 'full_text' else 'A6'}"
            if spec.get("text"):
                label = f"{label} ({spec['text']})"
            items.append(label)
        parts.append(", ".join(items))
    else:
        zones = [ZONE_LABELS.get(zone, zone) for zone in (print_payload.get("zones") or [])]
        if zones:
            parts.append(", ".join(zones))
    placement_note = (print_payload.get("placement_note") or "").strip()
    if placement_note:
        parts.append(f"Примітка: {placement_note}")

    mapped_addons = [ADDON_LABELS.get(a, a) for a in add_ons]
    if mapped_addons:
        parts.append(f"Add-ons: {', '.join(mapped_addons)}")
    return " | ".join(parts) or "—"


def _snapshot_pricing_text(snapshot: dict) -> str:
    pricing = snapshot.get("pricing") or {}
    parts = []
    if pricing.get("base_price") not in (None, ""):
        parts.append(f"база {pricing['base_price']} грн")
    if pricing.get("design_price") not in (None, "", 0):
        parts.append(f"дизайн +{pricing['design_price']} грн")
    if pricing.get("discount_percent") not in (None, "", 0):
        parts.append(f"знижка {pricing['discount_percent']}%")
    if pricing.get("estimate_required"):
        parts.append("потрібен менеджерський прорахунок")
    elif pricing.get("final_total") not in (None, ""):
        parts.append(f"разом {pricing['final_total']} грн")
    return ", ".join(parts) or "Уточнюється менеджером"


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


def _claim_notification_slot(lead, *, scope: str) -> bool:
    """Атомарно резервирует «слот» уведомления для лида.

    Возвращает True, если можно отправлять (и пишет timestamp), False — если
    в течение NOTIFICATION_THROTTLE_SECONDS уже было уведомление того же scope.

    Использует .filter().update() с условием, что запись не менялась с момента
    последнего чтения — это даёт защиту даже от параллельных запросов.
    """
    if not lead or not getattr(lead, "pk", None):
        return True

    try:
        from storefront.models import CustomPrintLead  # local import to avoid cycles
    except Exception:
        return True

    now = timezone.now()
    threshold = now - timedelta(seconds=NOTIFICATION_THROTTLE_SECONDS)

    try:
        # Атомарно: разрешаем отправку если last_notification_at NULL или старше threshold.
        updated = CustomPrintLead.objects.filter(pk=lead.pk).filter(
            models_q_filter_or_old(threshold)
        ).update(
            last_notification_at=now,
            notification_count=models_F_increment(),
        )
        # Если записи нет в БД (тесты с моками или удалённый лид) — не блокируем.
        if updated == 0:
            exists = CustomPrintLead.objects.filter(pk=lead.pk).exists()
            if not exists:
                return True
    except Exception:
        # Не блокируем уведомление, если БД временно недоступна (тесты,
        # отсутствие миграции и т.п.) — лучше отправить, чем потерять.
        logger.exception("Custom print notification slot check failed for lead %s", lead.pk)
        return True

    if updated:
        # Синхронизируем in-memory объект, чтобы дальше не было путаницы
        lead.last_notification_at = now
        lead.notification_count = (lead.notification_count or 0) + 1
        return True

    logger.info(
        "Skip duplicate custom-print notification for lead %s scope=%s (throttled)",
        lead.pk,
        scope,
    )
    return False


def models_q_filter_or_old(threshold):
    """Q(last_notification_at__isnull=True) | Q(last_notification_at__lt=threshold)."""
    from django.db.models import Q

    return Q(last_notification_at__isnull=True) | Q(last_notification_at__lt=threshold)


def models_F_increment():
    from django.db.models import F

    return F("notification_count") + 1


# ---------------------------------------------------------------------------
# Compact attachment delivery
# ---------------------------------------------------------------------------


def _send_attachments(notifier: TelegramNotifier, lead) -> bool:
    """Отправляет каждый файл отдельным документом (sendDocument).

    Почему документы, а не фото/альбомы:
    1. sendPhoto/media group пережимает изображение — для печати это
       недопустимо, менеджер должен получить оригинал без потери качества.
    2. В альбомах Telegram подписи к отдельным элементам не видны в ленте —
       менеджер не понимал, какой макет для какой зоны. У отдельного
       документа подпись всегда видна прямо под файлом.
    """
    payloads = _collect_attachment_payloads(lead)
    if not payloads:
        return False

    success = False
    for payload in payloads:
        success = notifier.send_admin_document(
            payload["path"],
            caption=payload["caption"],
            filename=Path(payload["path"]).name,
            parse_mode="HTML",
        ) or success
    return success


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify_new_custom_print_lead(lead) -> bool:
    """Заявка прийшла з кнопки «Надіслати менеджеру»."""
    try:
        if not _claim_notification_slot(lead, scope="new_lead"):
            return False
        notifier = _build_notifier()
        if not notifier.is_configured():
            logger.warning("Custom print Telegram notifier is not configured.")
            return False

        message = _build_lead_message(
            lead,
            header_emoji="🆕",
            header_title="Нова заявка на кастомний принт",
            intro_lines=[
                "👉 Зв'яжіться з клієнтом якнайшвидше — нижче кнопки для дзвінка / месенджера.",
            ],
        )
        success = notifier.send_admin_message(
            message,
            parse_mode="HTML",
            reply_markup=_info_reply_markup_full(lead),
        )
        success = _send_attachments(notifier, lead) or success
        return success
    except Exception as exc:
        logger.warning("Custom print Telegram notify failed: %s", exc, exc_info=True)
        return False


def notify_custom_print_moderation_request(lead) -> bool:
    """Клієнт додав кастом у кошик / просить погодити."""
    try:
        if not _claim_notification_slot(lead, scope="moderation_request"):
            return False
        notifier = _build_notifier()
        if not notifier.is_configured():
            logger.warning("Custom print moderation notifier is not configured.")
            return False

        lead.ensure_moderation_token()

        try:
            from decimal import Decimal

            final_price = Decimal(str(getattr(lead, "final_price_value", 0) or 0))
        except Exception:
            final_price = 0

        intro = [
            "<b>🛒 Клієнт чекає погодження кастомного кошика.</b>",
            "Натисніть «✅ Погодити», щоб клієнт зміг сплатити, або «❌ Відхилити» з коментарем.",
        ]
        if final_price and float(final_price) > 0:
            intro.append(f"💵 <b>Запитувана сума:</b> {escape(str(final_price))} грн")

        message = _build_lead_message(
            lead,
            header_emoji="🛒",
            header_title="Кастом-кошик: потрібна модерація",
            intro_lines=intro,
        )
        success = notifier.send_admin_message(
            message,
            parse_mode="HTML",
            reply_markup=_moderation_reply_markup(lead),
        )
        success = _send_attachments(notifier, lead) or success
        return success
    except Exception as exc:
        logger.warning("Custom print moderation notify failed: %s", exc, exc_info=True)
        return False


def notify_custom_print_moderation_result(lead) -> bool:
    """Notify the manager about manual approve/reject decision."""
    try:
        notifier = _build_notifier()
        if not notifier.is_configured():
            return False
        status = getattr(lead, "moderation_status", "")
        if status == "approved":
            emoji = "✅"
            title = "Заявку погоджено"
        elif status == "rejected":
            emoji = "❌"
            title = "Заявку відхилено"
        else:
            return False

        price_line = ""
        if getattr(lead, "approved_price", None):
            price_line = f"\n• <b>Фінальна ціна:</b> {lead.approved_price} грн"

        note_line = ""
        note = (getattr(lead, "manager_note", "") or "").strip()
        if note:
            note_line = f"\n• <b>Коментар:</b> {escape(note)}"

        message = (
            f"{emoji} <b>{escape(title)}</b>\n"
            f"<code>{escape(getattr(lead, 'lead_number', '') or f'CP-{lead.pk}')}</code>"
            f"{price_line}{note_line}\n\n"
            f'🔗 <a href="{_build_admin_panel_link(lead)}">Відкрити заявку в панелі</a>'
        )
        return notifier.send_admin_message(
            message,
            parse_mode="HTML",
            reply_markup=_info_reply_markup(lead),
        )
    except Exception as exc:
        logger.warning("Custom print moderation-result notify failed: %s", exc, exc_info=True)
        return False


def notify_custom_print_safe_exit(*, snapshot: dict, lead=None) -> bool:
    try:
        if lead is not None and not _claim_notification_slot(lead, scope="safe_exit"):
            return False
        notifier = _build_notifier()
        if not notifier.is_configured():
            logger.warning("Custom print safe-exit notifier is not configured.")
            return False

        return notifier.send_admin_message(
            _build_safe_exit_message(snapshot, lead=lead),
            parse_mode="HTML",
            reply_markup=_info_reply_markup(lead) if lead is not None else None,
        )
    except Exception as exc:
        logger.warning("Custom print safe-exit notify failed: %s", exc, exc_info=True)
        return False
