"""
Формування посилань на оплату Monobank для угод IG-бота.

Переюзає низькорівневий клієнт storefront.views.monobank._monobank_api_request
з ОКРЕМИМ acquiring-токеном (settings.MONOBANK_ACQUIRING_TOKEN — той самий, що
для накладних менеджерів). Замовлення НЕ створюється тут (рішення Q2: лише після
оплати) — зберігаємо invoice_id на IgDeal, статус підхопить вебхук (Task 17).
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from django.conf import settings

SITE = (getattr(settings, "BOT_PUBLIC_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
WEBHOOK_PATH = "/payments/monobank/webhook/"
RETURN_PATH = "/"


def _destination(deal) -> str:
    items = list(deal.items.all())
    if items:
        names = ", ".join((i.title or "товар") for i in items[:3])
        return f"TwoComms: {names}"[:280]
    return "Замовлення TwoComms"


def create_payment_link(deal, *, force: bool = False) -> dict:
    """Створює invoice Monobank для угоди. Ідемпотентно: якщо invoice вже є —
    повертає його. Повертає {ok, invoice_id, invoice_url, error?, reused?}."""
    if deal.invoice_id and deal.invoice_url and not force:
        return {
            "ok": True,
            "invoice_id": deal.invoice_id,
            "invoice_url": deal.invoice_url,
            "reused": True,
        }

    amount = deal.payable_amount()
    try:
        amount = Decimal(amount or 0)
    except Exception:
        amount = Decimal("0")
    if amount <= 0:
        return {"ok": False, "error": "zero_amount"}

    token = (
        getattr(settings, "MONOBANK_ACQUIRING_TOKEN", None)
        or getattr(settings, "MONOBANK_TOKEN", None)
    )

    payload = {
        "amount": int(amount * 100),  # копійки
        "ccy": 980,  # UAH
        "merchantPaymInfo": {
            "reference": f"IGDEAL-{deal.id}",
            "destination": _destination(deal),
        },
        "redirectUrl": SITE + RETURN_PATH,
        "webHookUrl": SITE + WEBHOOK_PATH,
    }

    from storefront.views.monobank import MonobankAPIError, _monobank_api_request

    try:
        data = _monobank_api_request(
            "POST", "/api/merchant/invoice/create", json_payload=payload, token=token
        )
    except MonobankAPIError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # мережа тощо
        return {"ok": False, "error": repr(exc)}

    result = data.get("result") or data
    invoice_id = result.get("invoiceId")
    invoice_url = result.get("pageUrl")
    if not invoice_id or not invoice_url:
        return {"ok": False, "error": "bad_response"}

    deal.invoice_id = invoice_id
    deal.invoice_url = invoice_url
    deal.save(update_fields=[
        "invoice_id", "invoice_url", "updated_at",
    ])
    apply_payment_status(
        deal,
        "created",
        payload={
            "invoiceId": invoice_id,
            "status": "created",
            "amount": int(amount * 100),
        },
        source="invoice_create",
    )
    deal.refresh_from_db()
    try:
        from management.services import bot_followups, ig_meta_events

        if deal.payment_truth == deal.PaymentTruth.PENDING:
            bot_followups.schedule_payment_followup(deal)
            ig_meta_events.log_or_send("InitiateCheckout", client=deal.client, deal=deal)
    except Exception:
        pass
    return {"ok": True, "invoice_id": invoice_id, "invoice_url": invoice_url}


# ---------------------------------------------------------------------------
# Статуси оплати угоди (Task 17). Замовлення створюється окремо (Task 18/19).
# ---------------------------------------------------------------------------
MONO_SUCCESS = {"success"}
MONO_PENDING = {"processing", "created", "hold"}
MONO_REVERSED = {"reversed"}
MONO_CANCELLED = {"canceled", "cancelled", "expired"}
MONO_FAILURE = {"failure", "rejected"}


def _minor_amount(value):
    if value in (None, ""):
        return None
    try:
        return (Decimal(str(value)) / Decimal("100")).quantize(Decimal("0.01"))
    except Exception:
        return None


def _provider_datetime(value):
    if not value:
        return None
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    from django.utils import timezone

    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _payment_event(deal, status: str, payload: dict, *, source: str, amount_valid):
    """Persist one PII-free provider observation and return it."""
    from management.models import IgPaymentEvent

    relevant = {
        "invoiceId": payload.get("invoiceId") or deal.invoice_id,
        "status": status,
        "amount": payload.get("amount"),
        "finalAmount": payload.get("finalAmount"),
        "modifiedDate": payload.get("modifiedDate"),
        "cancelList": payload.get("cancelList") or [],
    }
    canonical = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    event_key = hashlib.sha256(
        f"monobank:{deal.pk}:{payload_digest}".encode("utf-8")
    ).hexdigest()
    gross = _minor_amount(payload.get("amount"))
    final = _minor_amount(payload.get("finalAmount"))
    refunded = max(Decimal("0"), gross - final) if gross is not None and final is not None else None
    cancel_evidence = [
        {
            "status": str(item.get("status") or "")[:32],
            "amount": item.get("amount"),
            "modifiedDate": item.get("modifiedDate"),
        }
        for item in (payload.get("cancelList") or [])
        if isinstance(item, dict)
    ][:20]
    event, _created = IgPaymentEvent.objects.get_or_create(
        event_key=event_key,
        defaults={
            "deal": deal,
            "client": deal.client,
            "provider": "monobank",
            "source": (source or "provider")[:32],
            "invoice_id": (payload.get("invoiceId") or deal.invoice_id or "")[:128],
            "provider_status": status[:32],
            "provider_modified_at": _provider_datetime(payload.get("modifiedDate")),
            "gross_amount": gross,
            "final_amount": final,
            "refunded_amount": refunded,
            "amount_valid": amount_valid,
            "currency": deal.currency or "UAH",
            "evidence": {
                "status": status,
                "amount": payload.get("amount"),
                "finalAmount": payload.get("finalAmount"),
                "modifiedDate": payload.get("modifiedDate"),
                "cancelList": cancel_evidence,
            },
            "payload_digest": payload_digest,
        },
    )
    return event, _created


def apply_payment_status(deal, status_value, payload=None, *, source="provider") -> str:
    """Append provider evidence and serialize its authoritative projection."""
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    from management.models import IgPaymentProjection

    status = (status_value or "").strip().lower()
    payload = payload if isinstance(payload, dict) else {}
    expected = Decimal(deal.payable_amount() or 0).quantize(Decimal("0.01"))
    gross = _minor_amount(payload.get("amount"))
    final = _minor_amount(payload.get("finalAmount"))
    amount_valid = None
    if status in MONO_SUCCESS | MONO_PENDING | MONO_REVERSED:
        amount_valid = bool(
            gross is not None
            and gross == expected
            and (final is None or Decimal("0") <= final <= gross)
        )

    became_verified = False
    became_negative = False
    recalculate_aggregates = False
    try:
        with transaction.atomic():
            projection, _ = IgPaymentProjection.objects.get_or_create(
                deal_id=deal.pk,
                defaults={"client_id": deal.client_id},
            )
    except IntegrityError:
        # A concurrent webhook/poll may have won the unique one-to-one insert.
        projection = IgPaymentProjection.objects.get(deal_id=deal.pk)

    with transaction.atomic():
        projection = IgPaymentProjection.objects.select_for_update().get(pk=projection.pk)
        event, event_created = _payment_event(
            deal, status, payload, source=source, amount_valid=amount_valid
        )
        if not event_created:
            transaction.on_commit(
                lambda projection_id=projection.pk: reconcile_payment_projection(projection_id)
            )
            return status

        event_time = event.provider_modified_at
        if (
            event_time
            and projection.provider_modified_at
            and event_time < projection.provider_modified_at
        ):
            return status

        previous_truth = projection.truth
        was_verified = previous_truth in {
            deal.PaymentTruth.CONFIRMED,
            deal.PaymentTruth.PARTIALLY_REFUNDED,
        }
        terminal_truths = {
            deal.PaymentTruth.REFUNDED,
            deal.PaymentTruth.REVERSED,
        }
        applied = False

        if status in MONO_SUCCESS and amount_valid and previous_truth not in terminal_truths:
            final = gross if final is None else final
            projection.gross_amount = gross
            projection.refunded_amount = gross - final
            projection.paid_at = projection.paid_at or timezone.now()
            if final <= 0:
                projection.truth = deal.PaymentTruth.REFUNDED
            elif final < gross:
                projection.truth = deal.PaymentTruth.PARTIALLY_REFUNDED
            else:
                projection.truth = deal.PaymentTruth.CONFIRMED
            applied = True
        elif status in MONO_REVERSED and amount_valid:
            projection.truth = deal.PaymentTruth.REVERSED
            if gross is not None:
                projection.gross_amount = max(projection.gross_amount, gross)
                projection.refunded_amount = max(
                    projection.refunded_amount,
                    gross - (final or Decimal("0")),
                )
            applied = True
        elif status in MONO_PENDING:
            if projection.truth not in {
                deal.PaymentTruth.CONFIRMED,
                deal.PaymentTruth.PARTIALLY_REFUNDED,
                deal.PaymentTruth.REFUNDED,
                deal.PaymentTruth.REVERSED,
            }:
                projection.truth = deal.PaymentTruth.PENDING
                applied = True
        elif status in MONO_FAILURE | MONO_CANCELLED:
            if not was_verified and projection.truth not in terminal_truths:
                projection.truth = (
                    deal.PaymentTruth.CANCELLED
                    if status in MONO_CANCELLED
                    else deal.PaymentTruth.FAILED
                )
                applied = True

        if not applied:
            return status

        projection.provider_modified_at = event_time or projection.provider_modified_at
        projection.last_event = event
        projection.needs_reconciliation = True
        projection.reconciled_at = None
        projection.save()
        is_verified = projection.truth in {
            deal.PaymentTruth.CONFIRMED,
            deal.PaymentTruth.PARTIALLY_REFUNDED,
        }
        became_verified = is_verified and not was_verified
        became_negative = projection.truth in terminal_truths and previous_truth != projection.truth
        if became_negative:
            _reconcile_reversed_order(deal, truth=projection.truth)
            _ensure_reversal_review_outbox(deal, projection.truth)

    try:
        _sync_legacy_payment_mirror(projection)
        _mark_projection_reconciled(projection.pk)
    except Exception:
        # Authoritative InnoDB truth is already committed. The bounded cron
        # reconciliation repairs non-transactional legacy mirrors.
        pass
    if became_verified:
        try:
            from management.models import IgClient

            deal.client.set_stage(IgClient.Stage.PAID, reason="payment")
        except Exception:
            pass
        try:
            from management.services import bot_followups, ig_meta_events

            bot_followups.cancel_pending(deal.client, reason="payment_paid")
            ig_meta_events.log_or_send("Purchase", client=deal.client, deal=deal, order=deal.order)
        except Exception:
            pass
        _on_deal_paid(deal)
    elif became_negative:
        try:
            from management.services import ig_meta_events

            ig_meta_events.log_payment_reversal(client=deal.client, deal=deal, order=deal.order)
        except Exception:
            pass
        try:
            from management.services import instagram_bot

            instagram_bot.notify_manager(
                _reversal_review_text(deal),
                dedupe_key=f"ig_payment_reversed:{deal.pk}:{projection.truth}",
                event_type="payment_reversed_review",
                client=deal.client,
            )
        except Exception:
            pass
    return status


def _on_deal_paid(deal) -> None:
    """Хук «угоду оплачено» → пост-оплатний потік (створення замовлення/збір даних)."""
    try:
        from management.services import bot_orders

        bot_orders.on_deal_paid(deal)
    except Exception:
        pass


def _reconcile_reversed_order(deal, *, truth: str | None = None) -> None:
    """Fail closed after a full refund/reversal without erasing order history."""
    fresh_deal = deal.__class__.objects.filter(pk=deal.pk).first()
    if not fresh_deal or not fresh_deal.order_id:
        return
    from django.utils import timezone
    from orders.models import Order

    deal.order_id = fresh_deal.order_id
    order = Order.objects.filter(pk=fresh_deal.order_id).first()
    if not order:
        return
    payload = dict(order.payment_payload or {})
    payload["ig_payment_reconciliation"] = {
        "deal_id": deal.pk,
        "truth": truth or deal.payment_truth,
        "reconciled_at": timezone.now().isoformat(),
        "automatic_fulfillment_blocked": True,
    }
    order.payment_payload = payload
    order.payment_status = "unpaid"
    update_fields = ["payment_payload", "payment_status"]
    if order.status in {"new", "prep"}:
        order.status = "cancelled"
        update_fields.append("status")
    order.save(update_fields=update_fields)


def _reversal_review_text(deal) -> str:
    return (
        f"⚠️ IG: оплату за угодою #{deal.pk} повернено або скасовано. "
        "Замовлення заблоковано для автоматичного відправлення; потрібна ручна перевірка."
    )


def _ensure_reversal_review_outbox(deal, truth: str) -> None:
    """Persist operator work in the same transaction as terminal truth."""
    from management.models import IgBotNotification

    IgBotNotification.objects.get_or_create(
        dedupe_key=f"ig_payment_reversed:{deal.pk}:{truth}",
        defaults={
            "client": deal.client,
            "event_type": "payment_reversed_review",
            "payload": {"text": _reversal_review_text(deal), "chat_id": ""},
            "status": IgBotNotification.Status.PENDING,
        },
    )


def _sync_legacy_payment_mirror(projection) -> None:
    """Idempotently project authoritative truth into MyISAM summary rows."""
    from django.utils import timezone
    from management.services.bot_payment_truth import recalculate_client_payment_aggregates

    deal = projection.deal
    mirror_status = {
        deal.PaymentTruth.CONFIRMED: "prepaid" if deal.pay_type == deal.PayType.PREPAY_200 else "paid",
        deal.PaymentTruth.PARTIALLY_REFUNDED: "partially_refunded",
        deal.PaymentTruth.REFUNDED: "refunded",
        deal.PaymentTruth.REVERSED: "reversed",
        deal.PaymentTruth.PENDING: "checking",
        deal.PaymentTruth.CANCELLED: "cancelled",
        deal.PaymentTruth.FAILED: "unpaid",
    }.get(projection.truth, "unpaid")
    mirror = {
        "payment_truth": projection.truth,
        "payment_status": mirror_status,
        "paid_amount": projection.gross_amount,
        "refunded_amount": projection.refunded_amount,
        "payment_truth_updated_at": projection.provider_modified_at or timezone.now(),
    }
    if projection.paid_at:
        mirror["paid_at"] = projection.paid_at
    if projection.truth in {
        deal.PaymentTruth.CONFIRMED,
        deal.PaymentTruth.PARTIALLY_REFUNDED,
    } and deal.status not in (deal.Status.PAID, deal.Status.ORDER_CREATED):
        mirror["status"] = deal.Status.PAID
    elif projection.truth == deal.PaymentTruth.PENDING and deal.status not in (
        deal.Status.PAID,
        deal.Status.ORDER_CREATED,
    ):
        mirror["status"] = deal.Status.AWAITING_PAYMENT
    deal.__class__.objects.filter(pk=deal.pk).update(**mirror)
    for field, value in mirror.items():
        setattr(deal, field, value)
    recalculate_client_payment_aggregates(deal.client)


def _mark_projection_reconciled(projection_id: int) -> None:
    from django.utils import timezone
    from management.models import IgPaymentProjection

    IgPaymentProjection.objects.filter(pk=projection_id).update(
        needs_reconciliation=False,
        reconciled_at=timezone.now(),
    )


def reconcile_payment_projection(projection_id: int) -> bool:
    """Repair one projection's order/outbox and legacy mirrors idempotently."""
    from django.db import transaction
    from management.models import IgPaymentProjection

    with transaction.atomic():
        projection = (
            IgPaymentProjection.objects.select_related("deal__client")
            .select_for_update()
            .filter(pk=projection_id)
            .first()
        )
        if not projection:
            return False
        if projection.truth in {
            projection.deal.PaymentTruth.REFUNDED,
            projection.deal.PaymentTruth.REVERSED,
        }:
            _reconcile_reversed_order(projection.deal, truth=projection.truth)
            _ensure_reversal_review_outbox(projection.deal, projection.truth)
    _sync_legacy_payment_mirror(projection)
    _mark_projection_reconciled(projection.pk)
    return True


def reconcile_payment_projections(limit: int = 100) -> int:
    """Bounded recovery pass for projection/order/outbox/MyISAM mirrors."""
    from management.models import IgPaymentProjection

    bounded_limit = max(1, min(int(limit or 100), 1000))
    projection_ids = list(
        IgPaymentProjection.objects.filter(needs_reconciliation=True).order_by("updated_at", "id")
        .values_list("id", flat=True)[:bounded_limit]
    )
    repaired = 0
    for projection_id in projection_ids:
        try:
            repaired += int(reconcile_payment_projection(projection_id))
        except Exception:
            continue
    return repaired


def poll_deal_status(deal) -> str:
    """Pull-верифікація статусу invoice через acquiring-токен (захист від
    підробки вебхука) і застосування статусу. Повертає статус (lowercase)."""
    if not deal.invoice_id:
        return ""
    token = (
        getattr(settings, "MONOBANK_ACQUIRING_TOKEN", None)
        or getattr(settings, "MONOBANK_TOKEN", None)
    )
    from storefront.views.monobank import MonobankAPIError, _monobank_api_request

    try:
        data = _monobank_api_request(
            "GET", "/api/merchant/invoice/status",
            params={"invoiceId": deal.invoice_id}, token=token,
        )
    except (MonobankAPIError, Exception):
        return ""
    status = (data.get("status") or data.get("statusCode") or "").lower()
    if status:
        apply_payment_status(deal, status, payload=data)
    return status


def handle_webhook_invoice(invoice_id, payload=None, request=None) -> bool:
    """Викликається з monobank_webhook, коли invoice не належить Order/накладній.
    Якщо це invoice угоди IG-бота — pull-верифікуємо і застосовуємо. True, якщо
    угоду знайдено й оброблено."""
    from management.models import IgDeal

    deal = IgDeal.objects.filter(invoice_id=invoice_id).order_by("-id").first()
    if not deal:
        return False
    poll_deal_status(deal)
    return True


def poll_pending_deals(limit: int = 50) -> int:
    """Backstop-поллінг угод, що очікують оплату (якщо вебхук не дійшов).
    Повертає к-сть угод, що стали оплаченими."""
    from management.models import IgDeal

    qs = IgDeal.objects.filter(status=IgDeal.Status.AWAITING_PAYMENT).exclude(invoice_id="")[:limit]
    paid = 0
    for deal in qs:
        if poll_deal_status(deal) in MONO_SUCCESS:
            paid += 1
    return paid
