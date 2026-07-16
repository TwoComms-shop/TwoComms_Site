"""
Красивий email-лист (квитанція про оплату / чек) для замовлень TwoComms.

Будує HTML+text лист у стилі сайту (темна тема, акцент #8B5CF6),
з товарами, деталями оплати, промокодом, кнопками Instagram/Telegram
та бонус-блоком за відмітку в сторіс.

Особливості сумісності з поштовими клієнтами:
- усі зображення посилаються на АБСОЛЮТНІ HTTPS URL (SITE_BASE_URL),
  бо Gmail/Outlook блокують відносні та http-ресурси;
- верстка на таблицях + inline-стилі (без зовнішнього CSS);
- лист можна слати поза HTTP-запитом (webhook), тому URL будуються
  через settings.SITE_BASE_URL, а не request.build_absolute_uri.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string

logger = logging.getLogger('orders.email_receipt')

# Канонічні контакти бренду (використовуються в шаблоні листа)
INSTAGRAM_URL = "https://www.instagram.com/twocomms/"
TELEGRAM_URL = "https://t.me/twocomms"
SUPPORT_EMAIL = "cooperation@twocomms.shop"
# Промокод-бонус за відмітку в сторіс (10% на наступне замовлення)
STORIES_BONUS_PROMO = "STORIES10"


def _site_base_url() -> str:
    base = (getattr(settings, "SITE_BASE_URL", "") or "").strip() or "https://twocomms.shop"
    if not base.endswith("/"):
        base += "/"
    return base


def _abs_url(path_or_url) -> str:
    """Повертає абсолютний https URL для статики/медіа."""
    if not path_or_url:
        return ""
    value = str(path_or_url)
    if value.startswith(("http://", "https://")):
        # форсуємо https для коректного відображення у поштових клієнтах
        if value.startswith("http://"):
            return "https://" + value[len("http://"):]
        return value
    return urljoin(_site_base_url(), value.lstrip("/"))


def _safe_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _fmt_money(value) -> str:
    """Форматує суму як ціле число грн з розділювачем тисяч (1 299)."""
    dec = _safe_decimal(value)
    try:
        quant = dec.quantize(Decimal("1"))
    except (InvalidOperation, ValueError):
        quant = dec
    return f"{int(quant):,}".replace(",", " ")


def _order_item_image_url(item) -> str:
    """Абсолютний URL зображення позиції замовлення."""
    img = None
    try:
        variant = getattr(item, "color_variant", None)
        if variant is not None:
            first = variant.images.first()
            if first and getattr(first, "image", None):
                img = first.image
    except Exception:
        img = None

    if img is None:
        product = getattr(item, "product", None)
        if product is not None:
            try:
                img = product.main_image or product.display_image
            except Exception:
                img = getattr(product, "main_image", None)

    try:
        if img and getattr(img, "url", None):
            return _abs_url(img.url)
    except Exception:
        pass
    return ""


def _pay_type_label(order) -> str:
    mapping = {
        "prepay_200": "Передплата 200 грн (решта при отриманні)",
        "online_full": "Онлайн оплата (повна сума)",
        "full": "Онлайн оплата (повна сума)",
        "cod": "Оплата при отриманні",
        "partial": "Передплата (решта при отриманні)",
    }
    return mapping.get(order.pay_type, order.get_pay_type_display() if hasattr(order, "get_pay_type_display") else order.pay_type)


def _status_block(order) -> dict:
    """Повертає заголовок/текст/колір для блоку статусу оплати."""
    status = order.payment_status
    if status == "paid":
        return {
            "kind": "paid",
            "accent": "#10b981",
            "emoji": "✅",
            "title": "Оплата успішно пройшла",
            "text": "Дякуємо за покупку! Замовлення оплачено та передано в обробку. "
                    "Ми почнемо підготовку найближчим часом і повідомимо про відправлення.",
        }
    if status in ("prepaid", "partial"):
        return {
            "kind": "prepaid",
            "accent": "#8B5CF6",
            "emoji": "✅",
            "title": "Передплату внесено",
            "text": "Дякуємо! Передплату отримано. Залишок ви сплачуєте при отриманні "
                    "посилки на відділенні Нової Пошти. Ми вже готуємо ваше замовлення.",
        }
    return {
        "kind": "processing",
        "accent": "#8B5CF6",
        "emoji": "🧾",
        "title": "Замовлення прийнято",
        "text": "Дякуємо за замовлення! Заявку передано в обробку. "
                "Менеджер звʼяжеться з вами для підтвердження деталей.",
    }


def build_order_receipt_context(order) -> dict:
    """Збирає контекст для шаблону листа-квитанції."""
    items = []
    subtotal = Decimal("0")
    for it in order.items.all():
        line_total = _safe_decimal(it.line_total)
        subtotal += line_total
        # Деталі позиції: розмір / посадка / колір
        meta_parts = []
        if it.size:
            meta_parts.append(f"Розмір: {it.size}")
        fit = getattr(it, "fit_label", "") or ""
        if fit:
            meta_parts.append(f"Посадка: {fit}")
        meta_parts.extend(getattr(it, "generic_option_labels", []) or [])
        color = getattr(it, "color_name", None)
        if color:
            meta_parts.append(f"Колір: {color}")
        items.append({
            "title": it.title,
            "meta": " • ".join(meta_parts),
            "qty": it.qty,
            "line_total_display": _fmt_money(line_total),
            "image_url": _order_item_image_url(it),
        })

    custom_leads = []
    try:
        for lead in order.custom_print_leads.all():
            price = _safe_decimal(lead.final_price_value)
            subtotal += price
            custom_leads.append({
                "number": lead.lead_number,
                "type": lead.get_product_type_display() if hasattr(lead, "get_product_type_display") else "",
                "qty": getattr(lead, "quantity", 1),
                "price_display": _fmt_money(price),
            })
    except Exception:
        custom_leads = []

    discount = _safe_decimal(order.discount_amount)
    total = _safe_decimal(order.total_sum)

    is_prepaid = order.pay_type == "prepay_200" or order.payment_status in ("prepaid", "partial")
    prepayment = _safe_decimal(order.get_prepayment_amount()) if hasattr(order, "get_prepayment_amount") else Decimal("0")
    remaining = _safe_decimal(order.get_remaining_amount()) if hasattr(order, "get_remaining_amount") else total

    status = _status_block(order)

    context = {
        "order": order,
        "order_number": order.order_number,
        "full_name": order.full_name,
        "phone": order.phone,
        "city": order.city,
        "np_office": order.np_office,
        "pay_type_label": _pay_type_label(order),
        "items": items,
        "custom_leads": custom_leads,
        "has_items": bool(items or custom_leads),
        "subtotal_display": _fmt_money(subtotal),
        "discount_display": _fmt_money(discount),
        "has_discount": discount > 0,
        "total_display": _fmt_money(total),
        "is_prepaid": is_prepaid,
        "prepayment_display": _fmt_money(prepayment),
        "remaining_display": _fmt_money(remaining),
        "promo_code": getattr(order.promo_code, "code", "") if order.promo_code_id else "",
        "status": status,
        # Посилання / бренд
        "logo_url": _abs_url("/static/img/favicon-192x192.png"),
        "site_url": _site_base_url(),
        "instagram_url": INSTAGRAM_URL,
        "telegram_url": TELEGRAM_URL,
        "support_email": SUPPORT_EMAIL,
        "my_orders_url": urljoin(_site_base_url(), "my-orders/"),
        "stories_bonus_promo": STORIES_BONUS_PROMO,
        "currency": "грн",
    }
    return context


def _build_text_version(ctx: dict) -> str:
    lines = []
    lines.append(f"TwoComms — {ctx['status']['title']}")
    lines.append("")
    lines.append(f"Замовлення № {ctx['order_number']}")
    lines.append(ctx["status"]["text"])
    lines.append("")
    if ctx["is_prepaid"]:
        lines.append(f"Внесена передплата: {ctx['prepayment_display']} грн")
        lines.append(f"Залишок при отриманні: {ctx['remaining_display']} грн")
    lines.append(f"Сума замовлення: {ctx['total_display']} грн")
    if ctx["has_discount"]:
        promo = f" (промокод {ctx['promo_code']})" if ctx["promo_code"] else ""
        lines.append(f"Знижка: -{ctx['discount_display']} грн{promo}")
    lines.append("")
    lines.append("Доставка:")
    lines.append(f"  {ctx['full_name']}")
    lines.append(f"  {ctx['phone']}")
    lines.append(f"  {ctx['city']}, {ctx['np_office']}")
    lines.append(f"  Оплата: {ctx['pay_type_label']}")
    lines.append("")
    if ctx["items"]:
        lines.append("Товари:")
        for it in ctx["items"]:
            meta = f" ({it['meta']})" if it["meta"] else ""
            lines.append(f"  • {it['title']}{meta} × {it['qty']} — {it['line_total_display']} грн")
    for lead in ctx["custom_leads"]:
        lines.append(f"  • Кастомний виріб {lead['number']} × {lead['qty']} — {lead['price_display']} грн")
    lines.append("")
    lines.append("Відмітьте нас у сторіс Instagram з відкритого профілю — і отримайте")
    lines.append(f"10% знижки на наступне замовлення (промокод {ctx['stories_bonus_promo']}).")
    lines.append("")
    lines.append(f"Instagram: {ctx['instagram_url']}")
    lines.append(f"Telegram: {ctx['telegram_url']}")
    lines.append(f"Сайт: {ctx['site_url']}")
    lines.append("")
    lines.append("TwoComms — одяг для свідомих.")
    return "\n".join(lines)


def build_order_receipt_email(order) -> dict:
    """Будує subject/preheader/html/text листа для замовлення."""
    ctx = build_order_receipt_context(order)

    if ctx["is_prepaid"]:
        subject = f"TwoComms · Передплату за замовлення {ctx['order_number']} отримано"
    elif order.payment_status == "paid":
        subject = f"TwoComms · Оплату за замовлення {ctx['order_number']} отримано"
    else:
        subject = f"TwoComms · Замовлення {ctx['order_number']} прийнято"

    preheader = (
        f"Квитанція по замовленню {ctx['order_number']} на суму {ctx['total_display']} грн. "
        "Деталі, товари та бонус за відмітку в сторіс — усередині."
    )
    ctx["subject"] = subject
    ctx["preheader"] = preheader

    html = render_to_string("orders/emails/order_receipt.html", ctx)
    text = _build_text_version(ctx)

    return {
        "subject": subject,
        "preheader": preheader,
        "html": html,
        "text": text,
    }


def _is_valid_email(value) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    try:
        validate_email(value)
        return True
    except ValidationError:
        return False


def send_order_receipt_email(order, *, force: bool = False, recipient: str | None = None):
    """
    Відправляє лист-квитанцію на email замовлення (order.email) або на ``recipient``.

    Повертає (ok: bool, error: str|None).
    Не дублює відправку (флаг у payment_payload['receipt_email_sent']),
    якщо тільки не force=True.
    """
    to_email = (recipient or order.email or "").strip()
    if not _is_valid_email(to_email):
        return False, "no_valid_email"

    # Захист від повторної відправки
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    already_sent = bool(payload.get("receipt_email_sent"))
    if already_sent and not force:
        return True, None

    try:
        built = build_order_receipt_email(order)
    except Exception as exc:  # pragma: no cover - безпека рендеру
        logger.exception("Failed to build receipt email for order %s: %s", order.pk, exc)
        return False, "build_failed"

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or f"TwoComms <{SUPPORT_EMAIL}>"
    reply_to = [SUPPORT_EMAIL]

    try:
        msg = EmailMultiAlternatives(
            subject=built["subject"],
            body=built["text"],
            from_email=from_email,
            to=[to_email],
            reply_to=reply_to,
            headers={"X-Entity-Ref-ID": order.order_number or str(order.pk)},
        )
        msg.attach_alternative(built["html"], "text/html")
        msg.send(fail_silently=False)
    except Exception as exc:
        logger.warning("Failed to send receipt email for order %s to %s: %s", order.pk, to_email, exc)
        return False, str(exc)

    # Позначаємо у payment_payload, що лист відправлено
    try:
        if not isinstance(order.payment_payload, dict):
            order.payment_payload = {}
        order.payment_payload["receipt_email_sent"] = True
        order.payment_payload["receipt_email_to"] = to_email
        order.save(update_fields=["payment_payload"])
    except Exception:
        logger.warning("Receipt email sent but failed to flag order %s", order.pk)

    logger.info("Receipt email sent for order %s to %s", order.order_number, to_email)
    return True, None
