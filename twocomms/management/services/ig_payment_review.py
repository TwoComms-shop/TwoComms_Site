"""Evidence-bound manual payment review for Instagram conversations."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone


_PAYMENT_EVIDENCE_RE = re.compile(
    r"(?:\bоплат\w*\b|\bпередоплат\w*\b|\bплатіж\w*\b|\bплатеж\w*\b|\bоплач\w*\b|\bоплатила\b|\bоплатив\b|\bчек\b|\bквитанц\w*\b|\breceipt\b|\bpaid\b)",
    re.IGNORECASE,
)
_NON_EVIDENCE_RE = re.compile(
    r"(?:посилання|ссылка|лінк|линк|як оплатити|как оплатить|оплата доступна|payment link)",
    re.IGNORECASE,
)
_AFFIRMATION_RE = re.compile(
    r"(?:я\s+(?:вже\s+)?оплат\w*|оплат\w*\s+(?:вже\s+)?зроб\w*|переказ\w*\s+зроб\w*|чек|квитанц|receipt|paid)",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?<!\d)(\d{2,6}(?:[.,]\d{1,2})?)\s*(?:грн|uah|₴)", re.IGNORECASE)
_FIT_RE = re.compile(
    r"(?P<fit>базов\w*|класич\w*|classic|basic|оверсайз\w*|oversize)"
    r"(?:\s+(?:розмір|size))?\s*(?P<size>2xl|xxl|xl|l|m|s|xs|2xs)\b",
    re.IGNORECASE,
)
_QTY_RE = re.compile(r"\b(\d+)\s+(?:футбол\w*|шт\.?|штук\w*)\b", re.IGNORECASE)
_CUSTOMER_ROLES = {"user", "customer", "client"}
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?380|0)\d{9}(?!\d)")
_OFFICE_RE = re.compile(r"(?P<kind>поштомат|відділен\w*|відд\.?|office)\s*№?\s*(?P<number>\d{1,8})", re.IGNORECASE)
_NAME_STOPWORDS = {
    "в різні",
    "по повній передоплаті",
    "по повній оплаті",
    "потрібна оплата",
}


def next_review_status(status: str, action: str) -> str:
    """Apply the monotonic manager decision state machine."""
    if status == "pending" and action == "confirm":
        return "confirmed"
    if status == "confirmed" and action == "cancel":
        return "cancelled"
    if status in {"confirmed", "cancelled"}:
        return status
    if status == "pending" and action == "cancel":
        return "cancelled"
    return status


def extract_payment_review_evidence(messages) -> dict:
    """Classify customer payment evidence and preserve negotiated order facts.

    Manager payment instructions are context only. They can contribute a quoted
    conversation amount, but never create a payment review by themselves.
    """
    evidence = []
    amount_evidence = []
    order_items = []
    conversation_payment_context = False
    customer_messages = []
    context_messages = []
    raw_messages = list(messages or ())
    # Context can arrive after the customer attachment (for example, a
    # manager posts the payment amount after the receipt). Pre-scan the whole
    # bounded transcript so evidence classification is order-independent.
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("text") or "").split())
        if text and _PAYMENT_EVIDENCE_RE.search(text) and not _NON_EVIDENCE_RE.search(text):
            conversation_payment_context = True
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "unknown").strip().lower()
        raw_text = str(raw.get("text") or "")
        text = " ".join(raw_text.split())
        attachments = str(raw.get("attachments") or "").strip()
        if not text and not attachments:
            continue
        try:
            message_id = int(raw.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        context_messages.append({
            "message_id": message_id,
            "role": role,
            "quote": raw_text[:500],
            "attachments": attachments[:500],
        })
        amounts = _AMOUNT_RE.findall(text)
        for amount in amounts:
            try:
                normalized = Decimal(amount.replace(",", ".")).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                continue
            amount_evidence.append({
                "message_id": message_id,
                "role": role,
                "amount": str(normalized).rstrip("0").rstrip("."),
                "quote": text[:300],
            })
        if role in _CUSTOMER_ROLES:
            customer_messages.append(text)
            for match in _FIT_RE.finditer(text):
                fit_raw = match.group("fit").lower()
                fit = "oversize" if ("оверсайз" in fit_raw or fit_raw == "oversize") else "classic"
                order_items.append({
                    "title": "Оверсайз" if fit == "oversize" else "Базова футболка",
                    "fit": fit,
                    "size": match.group("size").upper(),
                    "qty": 1,
                    "product_id": None,
                    "color_variant_id": None,
                    "unit_price": None,
                    "source_message_id": message_id,
                })
            is_explicit_claim = bool(_AFFIRMATION_RE.search(text)) and not _NON_EVIDENCE_RE.search(text)
            is_receipt_attachment = bool(attachments) and (conversation_payment_context or is_explicit_claim)
            if is_explicit_claim or is_receipt_attachment:
                evidence.append({
                    "message_id": message_id,
                    "role": role,
                    "quote": text[:300],
                    "attachments": attachments[:500],
                })

    # A single explicit quantity describes the only extracted line; numbered
    # lines remain independent so classic and oversize are never collapsed.
    if len(order_items) == 1 and customer_messages:
        quantity_match = _QTY_RE.search(" ".join(customer_messages))
        if quantity_match:
            order_items[0]["qty"] = int(quantity_match.group(1))
    uncertainty_reasons = []
    if order_items:
        uncertainty_reasons.append("catalog_product_not_identified")
    if order_items and not amount_evidence:
        uncertainty_reasons.append("conversation_price_not_found")
    quoted_total = amount_evidence[-1]["amount"] if amount_evidence else ""
    manager_package_context = any(
        context["role"] not in _CUSTOMER_ROLES
        and re.search(r"пакет|zip|зіп", context["quote"], re.IGNORECASE)
        for context in context_messages
    )
    packaging_preference = ""
    if manager_package_context:
        customer_packaging_text = " ".join(
            context["quote"] for context in context_messages if context["role"] in _CUSTOMER_ROLES
        ).casefold()
        if "різн" in customer_packaging_text:
            packaging_preference = "Окремі пакети"
        elif "один" in customer_packaging_text:
            packaging_preference = "Один пакет"
    delivery = {"full_name": "", "phone": "", "city": "", "office": ""}
    for context in context_messages:
        if context["role"] not in _CUSTOMER_ROLES:
            continue
        quote = context["quote"]
        phone_match = _PHONE_RE.search(quote.replace(" ", ""))
        if phone_match and not delivery["phone"]:
            delivery["phone"] = phone_match.group(0)
        office_match = _OFFICE_RE.search(quote)
        if office_match and not delivery["office"]:
            delivery["office"] = f"{office_match.group('kind').capitalize()} {office_match.group('number')}"
        if "," in quote and not delivery["city"]:
            candidate = quote.split(",", 1)[0].strip()
            if 2 <= len(candidate) <= 100 and not any(char.isdigit() for char in candidate):
                delivery["city"] = candidate
        # Names are accepted only from the same customer message as a phone
        # number. This prevents short follow-ups such as "В різні" from
        # overwriting an already extracted recipient name. Slash-separated
        # delivery details are common in Instagram messages.
        segments = re.split(r"[/\n]+", quote) if phone_match else []
        for line in segments:
            candidate = " ".join(line.split()).strip(" .,:;()")
            if (
                not delivery["full_name"]
                and len(candidate.split()) in {2, 3}
                and not _PHONE_RE.search(candidate.replace(" ", ""))
                and not _FIT_RE.search(candidate)
                and not _AMOUNT_RE.search(candidate)
                and not _OFFICE_RE.search(candidate)
                and candidate.casefold() not in _NAME_STOPWORDS
                and not any(word in candidate.lower() for word in ("принт", "футбол", "передоплат", "оплат"))
            ):
                delivery["full_name"] = candidate
    order_draft = {
        "items": order_items,
        "quoted_total": quoted_total,
        "currency": "UAH",
        "amount_source_message_id": amount_evidence[-1]["message_id"] if amount_evidence else None,
        "uncertainty_reasons": uncertainty_reasons,
        "packaging_preference": packaging_preference,
        "delivery": delivery,
        "context_messages": context_messages[-80:],
    }
    return {
        "needs_review": bool(evidence),
        "provider_confirmed": False,
        "message_ids": [item["message_id"] for item in evidence if item["message_id"]],
        "evidence": evidence[-20:],
        "amount_evidence": amount_evidence[-20:],
        "order_draft": order_draft,
    }


def _deal_payload(deal) -> dict:
    if not deal:
        return {"deal_id": None, "items": [], "amount": "0", "delivery": {}}
    return {
        "deal_id": deal.pk,
        "amount": str(deal.amount or 0),
        "currency": deal.currency or "UAH",
        "items": [
            {
                "product_id": item.product_id,
                "color_variant_id": item.color_variant_id,
                "title": item.title,
                "size": item.size,
                "qty": item.qty,
                "unit_price": str(item.unit_price or 0),
            }
            for item in deal.items.select_related("product", "color_variant").all()
        ],
        "delivery": {
            "full_name": deal.np_full_name or "",
            "phone": deal.np_phone or "",
            "city": deal.np_city or "",
            "office": deal.np_office or "",
        },
    }


def _alert_text(review, client) -> str:
    evidence = review.evidence if isinstance(review.evidence, dict) else {}
    draft = evidence.get("order_draft") if isinstance(evidence.get("order_draft"), dict) else {}
    items = draft.get("items") or []
    amount = draft.get("quoted_total") or "не вказано"
    lines = [
        "⚠️ Instagram: потрібна перевірка заяви про оплату",
        f"Клієнт: {client.display_name or client.username or client.igsid} (IGSID {client.igsid})",
        f"Review #{review.pk}",
        "Оплата: не підтверджена provider ledger; потрібне ручне рішення.",
        f"Сума з переписки: {amount} грн",
    ]
    if items:
        lines.append("Позиції з переписки:")
        for item in items:
            lines.append(
                f"• {item.get('title') or item.get('fit') or 'Товар'} · "
                f"{item.get('size') or 'розмір не вказано'} · {item.get('qty') or 1} шт."
            )
    else:
        lines.append("Позиції з переписки: не визначені")
    packaging = draft.get("packaging_preference") or ""
    if packaging:
        lines.append(f"Пакування: {packaging}")
    delivery = draft.get("delivery") if isinstance(draft.get("delivery"), dict) else {}
    delivery_text = ", ".join(
        value for value in (delivery.get("full_name"), delivery.get("phone"), delivery.get("city"), delivery.get("office")) if value
    )
    if delivery_text:
        lines.append(f"Доставка: {delivery_text}")
    reasons = draft.get("uncertainty_reasons") or []
    if reasons:
        labels = {
            "catalog_product_not_identified": "товар не зіставлено з каталогом; виберіть його вручну",
            "conversation_price_not_found": "ціну з переписки не знайдено",
        }
        lines.append("Потрібно уточнити: " + "; ".join(labels.get(reason, reason) for reason in reasons))
    lines.append(f"Відкрити review: {getattr(settings, 'MANAGEMENT_BASE_URL', 'https://management.twocomms.shop').rstrip('/')}/bot/")
    return "\n".join(lines)


def create_payment_review(client, *, watermark: int = 0, messages=None):
    """Persist an idempotent review and enqueue its management alert.

    The alert uses the existing notification outbox. No customer message,
    Meta event, provider call, or order is created here.
    """
    if not client or client.hidden_at:
        return None
    from management.ig_bot_models import IgPaymentConfirmationReview
    from management.models import InstagramBotMessage

    if messages is None:
        rows = list(
            InstagramBotMessage.objects.filter(client_id=client.pk)
            .order_by("-id")[:80]
        )
        rows.reverse()
        messages = [
            {"id": row.pk, "role": row.role, "text": row.text, "attachments": row.attachments}
            for row in rows
        ]
    extracted = extract_payment_review_evidence(messages)
    if not extracted["needs_review"]:
        return None
    watermark = int(watermark or max(extracted["message_ids"] or [0]))
    deal = client.deals.order_by("-id").first()
    dedupe_key = f"ig-payment-review:{client.pk}:{watermark}"
    with transaction.atomic():
        review, created = IgPaymentConfirmationReview.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "client": client,
                "deal": deal,
                "evidence": {
                    "messages": extracted["evidence"],
                    "amount_evidence": extracted["amount_evidence"],
                    "order_draft": extracted["order_draft"],
                    "deal": _deal_payload(deal),
                },
                "watermark_message_id": watermark,
            },
        )
    from management.services.instagram_bot import notify_manager

    notify_manager(
        _alert_text(review, client),
        dedupe_key=review.dedupe_key,
        event_type="payment_review",
        client=client,
    )
    return review


def confirm_review(review, *, actor):
    from management.ig_bot_models import IgPaymentConfirmationReview

    with transaction.atomic():
        locked = IgPaymentConfirmationReview.objects.select_for_update().get(pk=review.pk)
        if locked.status == IgPaymentConfirmationReview.Status.PENDING:
            locked.status = IgPaymentConfirmationReview.Status.CONFIRMED
            locked.confirmed_by = actor
            locked.confirmed_at = timezone.now()
            locked.save(update_fields=["status", "confirmed_by", "confirmed_at", "updated_at"])
        return locked


def cancel_review(review, *, actor, reason=""):
    from management.ig_bot_models import IgPaymentConfirmationReview

    with transaction.atomic():
        locked = IgPaymentConfirmationReview.objects.select_for_update().get(pk=review.pk)
        if locked.status == IgPaymentConfirmationReview.Status.PENDING:
            locked.status = IgPaymentConfirmationReview.Status.CANCELLED
            locked.cancelled_by = actor
            locked.cancelled_at = timezone.now()
            locked.cancellation_reason = (reason or "")[:500]
            locked.save(update_fields=["status", "cancelled_by", "cancelled_at", "cancellation_reason", "updated_at"])
        return locked
