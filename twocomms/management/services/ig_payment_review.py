"""Evidence-bound manual payment review for Instagram conversations."""
from __future__ import annotations

import re
from datetime import datetime

from django.db import transaction
from django.utils import timezone


_PAYMENT_EVIDENCE_RE = re.compile(
    r"(?:\bоплат\w*\b|\bплатіж\w*\b|\bплатеж\w*\b|\bоплач\w*\b|\bоплатила\b|\bоплатив\b|\bчек\b|\bквитанц\w*\b|\breceipt\b|\bpaid\b)",
    re.IGNORECASE,
)
_NON_EVIDENCE_RE = re.compile(
    r"(?:посилання|ссылка|лінк|линк|як оплатити|как оплатить|оплата доступна|payment link)",
    re.IGNORECASE,
)
_AFFIRMATION_RE = re.compile(
    r"(?:я\s+(?:вже\s+)?оплат\w*|оплат\w*\s+(?:вже\s+)?|переказ\w*\s+зроб\w*|чек|квитанц|receipt|paid)",
    re.IGNORECASE,
)


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
    """Classify conversational payment claims without asserting payment truth."""
    evidence = []
    for raw in messages or ():
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("text") or "").split())
        attachments = str(raw.get("attachments") or "").strip()
        if not text and not attachments:
            continue
        haystack = f"{text} {attachments}".strip()
        if not _PAYMENT_EVIDENCE_RE.search(haystack) or _NON_EVIDENCE_RE.search(text):
            continue
        if not _AFFIRMATION_RE.search(haystack) and not attachments:
            continue
        evidence.append({
            "message_id": int(raw.get("id") or 0),
            "role": str(raw.get("role") or "unknown"),
            "quote": text[:300],
            "attachments": attachments[:500],
        })
    return {
        "needs_review": bool(evidence),
        "provider_confirmed": False,
        "message_ids": [item["message_id"] for item in evidence if item["message_id"]],
        "evidence": evidence[-20:],
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


def create_payment_review(client, *, watermark: int = 0, messages=None):
    """Persist a review alert; no Telegram or provider call is performed."""
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
                "evidence": {"messages": extracted["evidence"], "deal": _deal_payload(deal)},
                "watermark_message_id": watermark,
            },
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
