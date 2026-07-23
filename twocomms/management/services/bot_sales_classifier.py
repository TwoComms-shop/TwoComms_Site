"""Deterministic sales-signal classifier for the Instagram Direct bot.

This is a lightweight pre/post processor around Gemini. It must be cheap enough
to run for every inbound and manager echo message, and conservative enough not
to invent product/price facts.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Iterable

from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationAnalysisSnapshot,
    IgConversationSignal,
    InstagramBotMessage,
)

ANALYSIS_RULES_VERSION = "2026-07-23.v1"


UK_HINTS = (
    "ціна", "скільки", "розмір", "подарунок", "передоплат", "наклад", "відправ",
    "дякую", "хочу", "можна", "собі", "друк", "футболк",
)
RU_HINTS = (
    "цена", "сколько", "размер", "подарок", "предоплат", "налож", "отправ",
    "спасибо", "хочу", "можно", "себе", "печать", "футболк",
)
NO_BUY_RE = re.compile(
    r"\b(не\s+(буду|хочу|надо|треба|куплю|покупать|купувати)|"
    r"больше\s+не\s+пишите|більше\s+не\s+пишіть|відмов|отказ|стоп)\b",
    re.I,
)
THINKING_RE = re.compile(r"\b(подумаю|подумаємо|думаю|подума|позже|пізніше|потом)\b", re.I)
PRICE_RE = re.compile(r"\b(дорого|дорогувато|цена|ціна|сколько|скільки|price|вартість)\b", re.I)
PREPAY_RE = re.compile(r"\b(предоплат|передоплат|налож|наклад|післяплат|без\s+пред|без\s+перед)\b", re.I)
SIZE_RE = re.compile(r"\b(размер|розмір|сітка|сетка|оверсайз|regular|регуляр|xs|s|m|l|xl|xxl)\b", re.I)
CUSTOM_RE = re.compile(r"\b(кастом|custom|свой\s+принт|власн\w*\s+принт|dtf|дтф|печать|друк|принт)\b", re.I)
PAYMENT_RE = re.compile(r"\b(оплат|платеж|платіж|ссылка|посилання|линк|лінк|карта|монобанк)\b", re.I)
DELIVERY_RE = re.compile(r"\b(достав|відправ|отправ|нова\s+пошта|новая\s+почта|нп|відділен|отделен)\b", re.I)
GIFT_RE = re.compile(r"\b(подарок|подарунок|на\s+подар|в\s+подар)\b", re.I)
SELF_RE = re.compile(r"\b(себе|собі|для\s+себя|для\s+себе)\b", re.I)
PHONE_RE = re.compile(r"(?:\+?38)?0\d{9}")
QTY_RE = re.compile(r"\b(?:x|х|×)?\s*(\d{1,2})\s*(?:шт|штук|pcs|од)\b", re.I)
SIZE_TOKEN_RE = re.compile(r"\b(xs|s|m|l|xl|xxl|xxxl|2xl|3xl)\b", re.I)
COLOR_WORDS = {
    "чорн": "black",
    "черн": "black",
    "білий": "white",
    "бел": "white",
    "олив": "olive",
    "хакі": "khaki",
    "хаки": "khaki",
    "сір": "gray",
    "сер": "gray",
}


def _contains_any(text: str, terms: Iterable[str]) -> int:
    low = text.lower()
    return sum(1 for term in terms if term in low)


def detect_language(text: str) -> str:
    low = (text or "").lower()
    if re.search(r"[їєіґ]", low):
        return "uk"
    uk = _contains_any(low, UK_HINTS)
    ru = _contains_any(low, RU_HINTS)
    if uk > ru:
        return "uk"
    if ru > uk:
        return "ru"
    return "uk" if any(ch in low for ch in "іїєґ") else "ru"


def _signal(client, signal_type: str, *, message=None, confidence: float = 0.9, value: str = "", payload=None):
    return IgConversationSignal.objects.create(
        client=client,
        message=message if isinstance(message, InstagramBotMessage) else None,
        signal_type=signal_type,
        confidence=Decimal(str(confidence)),
        value=(value or "")[:255],
        payload=payload or {},
    )


def _extract_context(text: str) -> dict:
    low = (text or "").lower()
    ctx: dict = {}
    qty = QTY_RE.search(low)
    if qty:
        try:
            ctx["quantity"] = max(1, min(99, int(qty.group(1))))
        except Exception:
            pass
    size = SIZE_TOKEN_RE.search(low)
    if size:
        ctx["size"] = size.group(1).upper()
    for stem, color in COLOR_WORDS.items():
        if stem in low:
            ctx["color"] = color
            break
    if GIFT_RE.search(low):
        ctx["gift"] = True
    if SELF_RE.search(low):
        ctx["self_purchase"] = True
    return ctx


def _analysis_band(client: IgClient, result: dict) -> str:
    paid_stages = {IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE}
    if client.stage in paid_stages:
        return IgConversationAnalysisSnapshot.Band.PAID
    if result.get("no_buy"):
        return IgConversationAnalysisSnapshot.Band.LOST
    if client.stage == IgClient.Stage.SPAM:
        return IgConversationAnalysisSnapshot.Band.LOST
    if client.stage in {IgClient.Stage.CHECKOUT, IgClient.Stage.PAYMENT_PENDING}:
        return IgConversationAnalysisSnapshot.Band.CHECKOUT
    if IgConversationSignal.Type.CHECKOUT_STARTED in result.get("signals", []):
        return IgConversationAnalysisSnapshot.Band.HIGH_INTENT
    if int(result.get("readiness") or 0) >= 40 or client.current_product_id:
        return IgConversationAnalysisSnapshot.Band.QUALIFIED
    if result.get("signals") or client.intent != IgClient.Intent.UNKNOWN:
        return IgConversationAnalysisSnapshot.Band.EXPLORING
    return IgConversationAnalysisSnapshot.Band.COLD


def _record_analysis_snapshot(
    client: IgClient,
    message: InstagramBotMessage | None,
    result: dict,
    *,
    role: str,
) -> IgConversationAnalysisSnapshot:
    """Persist one rules snapshot per client/message/rules watermark."""
    band = _analysis_band(client, result)
    readiness = max(0, min(100, int(result.get("readiness") or 0)))
    if band == IgConversationAnalysisSnapshot.Band.PAID:
        probability = Decimal("1.0000")
        confidence = Decimal("1.0000")
    elif band in {
        IgConversationAnalysisSnapshot.Band.LOST,
        IgConversationAnalysisSnapshot.Band.OPTED_OUT,
    }:
        probability = Decimal("0.0000")
        confidence = Decimal("0.9500")
    else:
        probability = (Decimal(readiness) / Decimal("100")).quantize(Decimal("0.0001"))
        confidence = min(
            Decimal("0.9000"),
            Decimal("0.5500") + Decimal("0.0500") * len(result.get("signals", [])),
        )

    source_role = role or getattr(message, "role", "") or "unknown"
    evidence = [{
        "source_role": source_role,
        "message_id": getattr(message, "pk", None),
        "manager_evidence": source_role == InstagramBotMessage.Role.MANAGER,
        "signals": list(result.get("signals", [])),
        "intent": result.get("intent") or IgClient.Intent.UNKNOWN,
        "objection": result.get("objection") or IgClient.Objection.NONE,
        "legacy_readiness": readiness,
    }]
    uncertainties = []
    if not client.current_product_id:
        uncertainties.append("product")
    if not client.current_size:
        uncertainties.append("size")
    if (
        result.get("intent") == IgClient.Intent.PAYMENT
        and band != IgConversationAnalysisSnapshot.Band.PAID
    ):
        uncertainties.append("payment_unverified")
    if source_role == InstagramBotMessage.Role.MANAGER:
        uncertainties.append("manager_evidence_not_customer_intent")

    message_key = getattr(message, "pk", None) or "none"
    dedupe_key = f"rules:{ANALYSIS_RULES_VERSION}:{client.pk}:{message_key}"
    snapshot, _created = IgConversationAnalysisSnapshot.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            "client": client,
            "last_analyzed_message": message if isinstance(message, InstagramBotMessage) else None,
            "score_band": band,
            "purchase_probability": probability,
            "confidence": confidence,
            "evidence": evidence,
            "uncertainties": uncertainties,
            "analysis_model": "rules",
            "rules_version": ANALYSIS_RULES_VERSION,
            "trigger": "message",
        },
    )
    return snapshot


def classify_message(client: IgClient, *, message: InstagramBotMessage | None = None, text: str | None = None, role: str = "") -> dict:
    """Classify a single message and persist CRM state/signals.

    Returns a small dict for callers that need immediate routing decisions.
    """
    if not client:
        return {"signals": [], "readiness": 0}
    text = (text if text is not None else getattr(message, "text", "")) or ""
    low = text.lower()
    role = role or getattr(message, "role", "") or ""
    is_manager = role == InstagramBotMessage.Role.MANAGER
    lang = (
        client.language or "uk"
        if is_manager
        else detect_language(text) if text.strip() else (client.language or "uk")
    )

    signals: list[str] = []
    intent = client.intent or IgClient.Intent.UNKNOWN
    objection = client.primary_objection or IgClient.Objection.NONE
    readiness = int(client.buying_readiness or 0)
    sales_context = dict(client.sales_context or {})

    ctx = _extract_context(text)
    if ctx:
        sales_context.update(ctx)
    if ctx.get("quantity"):
        client.current_qty = ctx["quantity"]
    if ctx.get("size"):
        client.current_size = ctx["size"][:16]
    if ctx.get("color"):
        client.current_color = ctx["color"][:64]

    def add(sig: str, *, conf: float = 0.9, value: str = ""):
        signals.append(sig)
        try:
            _signal(client, sig, message=message, confidence=conf, value=value)
        except Exception:
            pass

    if is_manager:
        add(IgConversationSignal.Type.MANAGER_TAKEOVER, conf=1.0)

    if not is_manager and (client.ad_id or client.ad_ref or client.referral_payload):
        add(IgConversationSignal.Type.AD_REPLY, conf=0.85, value=client.ad_id or client.ad_ref)

    no_buy = bool(not is_manager and NO_BUY_RE.search(low))
    if no_buy:
        objection = IgClient.Objection.NO_BUY
        client.lost_reason = "no_buy"
        add(IgConversationSignal.Type.LOST, conf=0.95, value="no_buy")
        try:
            client.set_stage(IgClient.Stage.COLD, reason="no_buy")
        except Exception:
            client.stage = IgClient.Stage.COLD

    if not is_manager and CUSTOM_RE.search(low):
        intent = IgClient.Intent.CUSTOM_PRINT
        readiness += 30
        add(IgConversationSignal.Type.CUSTOM_PRINT, conf=0.9)
    elif not is_manager and (PAYMENT_RE.search(low) or PHONE_RE.search(low)):
        intent = IgClient.Intent.PAYMENT
        readiness += 40
        add(IgConversationSignal.Type.CHECKOUT_STARTED, conf=0.8)
    elif not is_manager and SIZE_RE.search(low):
        intent = IgClient.Intent.SIZE
        readiness += 20
    elif not is_manager and PRICE_RE.search(low):
        intent = IgClient.Intent.PRICE
        readiness += 20
    elif not is_manager and text.strip() and not no_buy:
        intent = IgClient.Intent.PRODUCT if intent == IgClient.Intent.UNKNOWN else intent
        readiness += 10

    if not is_manager and PRICE_RE.search(low):
        objection = IgClient.Objection.PRICE
        readiness += 12
        add(IgConversationSignal.Type.PRICE_OBJECTION, conf=0.85)
    if not is_manager and PREPAY_RE.search(low):
        objection = IgClient.Objection.PREPAYMENT
        readiness += 10
        add(IgConversationSignal.Type.PREPAYMENT_OBJECTION, conf=0.9)
    if not is_manager and SIZE_RE.search(low):
        if objection == IgClient.Objection.NONE:
            objection = IgClient.Objection.SIZE
        readiness += 8
        add(IgConversationSignal.Type.SIZE_CONCERN, conf=0.8)
    if not is_manager and THINKING_RE.search(low):
        objection = IgClient.Objection.THINKING
        readiness = max(readiness, 25)
    if not is_manager and GIFT_RE.search(low):
        add(IgConversationSignal.Type.GIFT, conf=0.85)
        readiness += 10
    if not is_manager and SELF_RE.search(low):
        add(IgConversationSignal.Type.SELF_PURCHASE, conf=0.75)
        readiness += 8

    readiness = max(0, min(100, readiness))
    client.language = lang
    client.intent = intent
    client.primary_objection = objection
    client.buying_readiness = readiness
    client.sales_context = sales_context
    fields = [
        "language",
        "intent",
        "primary_objection",
        "buying_readiness",
        "lost_reason",
        "current_size",
        "current_color",
        "current_qty",
        "sales_context",
        "updated_at",
    ]
    if is_manager:
        client.last_manager_message_at = timezone.now()
        fields.append("last_manager_message_at")
    try:
        client.save(update_fields=fields)
    except Exception:
        client.save()
    result = {
        "language": lang,
        "intent": intent,
        "objection": objection,
        "readiness": readiness,
        "signals": signals,
        "no_buy": no_buy,
        "sales_context": sales_context,
    }
    try:
        snapshot = _record_analysis_snapshot(client, message, result, role=role)
        result["analysis_snapshot_id"] = snapshot.pk
    except Exception:
        result["analysis_snapshot_id"] = None
    return result
