"""Evidence-bound manual payment review for Instagram conversations."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
import hashlib
import json

from django.conf import settings
from django.db import transaction
from django.utils import timezone


_PAYMENT_EVIDENCE_RE = re.compile(
    r"(?:\bоплат\w*\b|\bпередоплат\w*\b|\bплатіж\w*\b|\bплатеж\w*\b|\bоплач\w*\b|\bоплатила\b|\bоплатив\b|\bчек(?:а|у|ом)?\b|\bквитанц\w*\b|\breceipt\b|\bpaid\b)",
    re.IGNORECASE,
)
_NON_EVIDENCE_RE = re.compile(
    r"(?:посилання|ссылка|лінк|линк|як оплатити|как оплатить|оплата доступна|payment link)",
    re.IGNORECASE,
)
_AFFIRMATION_RE = re.compile(
    r"(?:я\s+(?:вже\s+)?оплат\w*|оплат\w*\s+(?:вже\s+)?зроб\w*|переказ\w*\s+зроб\w*|\bчек(?:а|у|ом)?\b|\bквитанц\w*\b|receipt|paid)",
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

_PRODUCT_MEDIA_TYPES = {"ig_post", "share", "ig_reel", "reel", "story_mention", "story"}


def _raw_media_by_mid(client) -> dict[str, list[dict]]:
    """Recover media that Meta kept only in the raw webhook event.

    Instagram sometimes sends an ``ig_post`` in a separate event with the same
    message id while the normalized message row has an empty ``attachments``
    field. Raw events are the source evidence; this helper only reads them.
    """
    if not client or not getattr(client, "igsid", ""):
        return {}
    try:
        from management.models import InstagramBotRawEvent
        from management.services.instagram_bot import _iter_events
    except Exception:
        return {}
    recovered: dict[str, list[dict]] = {}
    rows = InstagramBotRawEvent.objects.filter(sender_id=client.igsid).order_by("-id")[:240]
    for event in rows:
        try:
            payload = json.loads(event.payload or "{}")
        except (TypeError, ValueError):
            continue
        try:
            events = _iter_events(payload)
            for sender_id, _recipient_id, message, _referral in events:
                if sender_id != client.igsid:
                    continue
                mid = str(message.get("mid") or "").strip()
                if not mid:
                    continue
                for attachment in message.get("attachments") or []:
                    if not isinstance(attachment, dict):
                        continue
                    payload_data = attachment.get("payload")
                    if not isinstance(payload_data, dict):
                        continue
                    url = str(payload_data.get("url") or "").strip()
                    if not url or not url.startswith(("https://", "http://")):
                        continue
                    item = {
                        "url": url[:1200],
                        "type": str(attachment.get("type") or "image")[:32],
                        "title": str(payload_data.get("title") or "")[:700],
                        "ig_post_media_id": str(payload_data.get("ig_post_media_id") or "")[:80],
                        "raw_event_id": event.pk,
                    }
                    existing = recovered.setdefault(mid, [])
                    if not any(row.get("url") == item["url"] for row in existing):
                        existing.append(item)
        except Exception:
            continue
    return recovered


def _existing_media(raw_attachments: str) -> list[dict]:
    try:
        urls = json.loads(raw_attachments or "[]")
    except (TypeError, ValueError):
        urls = []
    if not isinstance(urls, list):
        urls = []
        for candidate in re.findall(r"https?://[^\s\"'\]]+", raw_attachments or ""):
            urls.append(candidate)
    return [
        {"url": str(url)[:1200], "type": "image", "title": "", "raw_event_id": None}
        for url in urls
        if isinstance(url, str) and url.startswith(("https://", "http://"))
    ]


def _role_for_media(item: dict, *, payment_context: bool, explicit_claim: bool) -> str:
    media_type = str(item.get("type") or "image").casefold()
    if media_type in _PRODUCT_MEDIA_TYPES:
        return "product"
    if payment_context or explicit_claim:
        return "receipt"
    return "other"


def _augment_messages_with_raw_media(client, messages) -> list[dict]:
    raw_by_mid = _raw_media_by_mid(client)
    result = []
    for raw in list(messages or ()):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        media = list(item.get("media") or []) if isinstance(item.get("media"), list) else []
        media.extend(_existing_media(str(item.get("attachments") or "")))
        mid = str(item.get("mid") or "").strip()
        for attachment in raw_by_mid.get(mid, []):
            if not any(row.get("url") == attachment.get("url") for row in media):
                media.append(attachment)
        # Keep the old attachments contract intact for callers that only know
        # how to consume a JSON list of URLs, while exposing structured media
        # evidence to the review UI and catalog matcher.
        item["media"] = media[:8]
        if media and not item.get("attachments"):
            item["attachments"] = json.dumps(
                [row["url"] for row in media if row.get("url")], ensure_ascii=False
            )
        result.append(item)
    return result


def _persist_review_media(media: list[dict]) -> list[dict]:
    """Download bounded image evidence to our media storage for durable review.

    Signed Meta URLs can expire; ``local_url`` is best effort and the original
    URL remains in evidence for audit. Non-image or failed downloads are never
    sent into catalog matching.
    """
    try:
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        from management.services.instagram_bot import download_image
    except Exception:
        return media
    enriched = []
    for item in media[:8]:
        row = dict(item)
        url = str(row.get("url") or "")
        if not url:
            enriched.append(row)
            continue
        try:
            downloaded = download_image(url)
            if downloaded:
                mime, raw = downloaded
                suffix = ".jpg" if mime == "image/jpeg" else ".bin"
                digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                path = f"ig_payment_reviews/{digest}{suffix}"
                if not default_storage.exists(path):
                    default_storage.save(path, ContentFile(raw))
                row["local_url"] = default_storage.url(path)
                row["mime"] = mime[:64]
                row["bytes"] = len(raw)
        except Exception:
            pass
        enriched.append(row)
    return enriched


def _catalog_match_for_media(media: list[dict]) -> dict:
    product_media = [row for row in media if row.get("role") == "product" and row.get("url")]
    if not product_media:
        return {}
    try:
        from management.services.instagram_bot import download_image
        from management.services import bot_vision
        images = []
        for row in product_media[:3]:
            image = download_image(str(row["url"]))
            if image:
                images.append(image)
        match = bot_vision.match(images) if images else {"product_id": None, "confidence": 0, "reason": "image_download_failed"}
    except Exception as exc:
        return {"status": "error", "reason": str(exc)[:180]}
    pid = match.get("product_id")
    result = {
        "status": "matched" if pid else "unresolved",
        "product_id": pid,
        "confidence": match.get("confidence", 0),
        "reason": match.get("reason", ""),
        "source_message_ids": sorted({int(row.get("message_id")) for row in product_media if str(row.get("message_id") or "").isdigit()}),
    }
    if not pid:
        return result
    try:
        from storefront.models import Product
        from productcolors.models import ProductColorVariant
        product = Product.objects.filter(pk=pid).first()
        if not product:
            return result
        result.update({
            "title": product.title,
            "slug": product.slug,
            "catalog_price": str(getattr(product, "final_price", None) or product.price),
            "url": f"https://twocomms.shop/product/{product.slug}/",
        })
        variants = []
        for variant in ProductColorVariant.objects.filter(product=product).select_related("color")[:20]:
            variants.append({"id": variant.pk, "color": getattr(variant.color, "name", "") or "", "sku": variant.sku or ""})
        result["variant_candidates"] = variants
        if len(variants) == 1:
            result["color_variant_id"] = variants[0]["id"]
    except Exception:
        pass
    return result


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
        raw_media = raw.get("media") if isinstance(raw.get("media"), list) else _existing_media(attachments)
        if not text and not attachments and not raw_media:
            continue
        try:
            message_id = int(raw.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        media = [dict(item) for item in raw_media if isinstance(item, dict) and item.get("url")]
        explicit_claim = bool(_AFFIRMATION_RE.search(text)) and not _NON_EVIDENCE_RE.search(text)
        for media_item in media:
            media_item["message_id"] = message_id
            media_item["role"] = _role_for_media(
                media_item,
                payment_context=conversation_payment_context,
                explicit_claim=explicit_claim,
            )
        context_messages.append({
            "message_id": message_id,
            "role": role,
            "quote": raw_text[:500],
            "attachments": attachments[:500],
            "media": media[:8],
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
            is_receipt_attachment = bool(attachments) and (
                conversation_payment_context or explicit_claim
            ) and not any(item.get("role") == "product" for item in media)
            is_receipt_attachment = is_receipt_attachment or any(item.get("role") == "receipt" for item in media)
            if explicit_claim or is_receipt_attachment:
                evidence.append({
                    "message_id": message_id,
                    "role": role,
                    "quote": text[:300],
                    "attachments": attachments[:500],
                    "media": media[:8],
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
    media_audit = [
        media_item
        for context in context_messages
        for media_item in context.get("media", [])
    ]
    order_draft = {
        "items": order_items,
        "quoted_total": quoted_total,
        "currency": "UAH",
        "amount_source_message_id": amount_evidence[-1]["message_id"] if amount_evidence else None,
        "uncertainty_reasons": uncertainty_reasons,
        "packaging_preference": packaging_preference,
        "delivery": delivery,
        "context_messages": context_messages[-80:],
        "media": media_audit[:40],
    }
    return {
        "needs_review": bool(evidence),
        "provider_confirmed": False,
        "message_ids": [item["message_id"] for item in evidence if item["message_id"]],
        "evidence": evidence[-20:],
        "amount_evidence": amount_evidence[-20:],
        "order_draft": order_draft,
        "media": media_audit[:40],
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
            catalog = item.get("catalog") if isinstance(item.get("catalog"), dict) else {}
            product_label = catalog.get("title") or item.get("title") or item.get("fit") or "Товар"
            variant_label = ""
            variant_id = catalog.get("color_variant_id")
            for variant in catalog.get("variant_candidates") or []:
                if variant.get("id") == variant_id and variant.get("color"):
                    variant_label = f" · {variant['color']}"
                    break
            lines.append(
                f"• {product_label}{variant_label} · "
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
    catalog_match = evidence.get("catalog_match") if isinstance(evidence.get("catalog_match"), dict) else {}
    if catalog_match.get("status") == "matched":
        lines.append(
            f"Зображення товару: {catalog_match.get('title') or 'збіг з каталогом'} "
            f"({round(float(catalog_match.get('confidence') or 0) * 100)}% впевненості)."
        )
    elif catalog_match:
        lines.append("Зображення товару: точного збігу з каталогом не знайдено — перевірте вручну.")
    media = evidence.get("media") if isinstance(evidence.get("media"), list) else []
    receipts = [item for item in media if item.get("role") == "receipt"]
    products = [item for item in media if item.get("role") == "product"]
    lines.append(f"Вкладення: чеків {len(receipts)}, зображень товару {len(products)}.")
    base = getattr(settings, "MANAGEMENT_BASE_URL", "https://management.twocomms.shop").rstrip("/")
    lines.append(f"Відкрити review: {base}/bot/?payment_review={review.pk}")
    return "\n".join(lines)


def _review_keyboard(review) -> dict:
    base = getattr(settings, "MANAGEMENT_BASE_URL", "https://management.twocomms.shop").rstrip("/")
    return {"inline_keyboard": [[
        {"text": "Перейти до підтвердження", "url": f"{base}/bot/?payment_review={review.pk}"},
    ]]}


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
            {"id": row.pk, "mid": row.mid, "role": row.role, "text": row.text, "attachments": row.attachments}
            for row in rows
        ]
    messages = _augment_messages_with_raw_media(client, messages)
    extracted = extract_payment_review_evidence(messages)
    if not extracted["needs_review"]:
        return None
    enriched_media = _persist_review_media(extracted.get("media") or [])
    for item in enriched_media:
        item["message_id"] = item.get("message_id") or None
    for context in extracted.get("order_draft", {}).get("context_messages", []):
        context_media = context.get("media") or []
        for context_item in context_media:
            for item in enriched_media:
                if item.get("url") == context_item.get("url"):
                    context_item.update({key: value for key, value in item.items() if key in {"local_url", "mime", "bytes"}})
    extracted["media"] = enriched_media
    extracted["order_draft"]["media"] = enriched_media
    catalog_match = _catalog_match_for_media(enriched_media)
    extracted["catalog_match"] = catalog_match
    if catalog_match.get("status") == "matched":
        for item in extracted["order_draft"].get("items", []):
            item["product_id"] = catalog_match.get("product_id")
            item["color_variant_id"] = catalog_match.get("color_variant_id")
            item["catalog"] = {
                "product_id": catalog_match.get("product_id"),
                "title": catalog_match.get("title", ""),
                "slug": catalog_match.get("slug", ""),
                "catalog_price": catalog_match.get("catalog_price", ""),
                "color_variant_id": catalog_match.get("color_variant_id"),
                "variant_candidates": catalog_match.get("variant_candidates", []),
            }
        extracted["order_draft"]["uncertainty_reasons"] = [
            reason for reason in extracted["order_draft"].get("uncertainty_reasons", [])
            if reason != "catalog_product_not_identified"
        ]
    extracted["media_audit_v2"] = True
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
                    "media": extracted.get("media", []),
                    "catalog_match": extracted.get("catalog_match", {}),
                    "media_audit_v2": True,
                    "deal": _deal_payload(deal),
                },
                "watermark_message_id": watermark,
            },
        )
    from management.services.instagram_bot import notify_manager

    if not created and isinstance(review.evidence, dict) and not review.evidence.get("media_audit_v2"):
        review.evidence = {
            **review.evidence,
            "messages": extracted["evidence"],
            "amount_evidence": extracted["amount_evidence"],
            "order_draft": extracted["order_draft"],
            "media": extracted.get("media", []),
            "catalog_match": extracted.get("catalog_match", {}),
            "media_audit_v2": True,
        }
        review.save(update_fields=["evidence", "updated_at"])

    notify_manager(
        _alert_text(review, client),
        dedupe_key=review.dedupe_key,
        event_type="payment_review",
        client=client,
        reply_markup=_review_keyboard(review),
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
