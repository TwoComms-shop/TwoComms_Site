"""
Формування посилань на оплату Monobank для угод IG-бота.

Переюзає низькорівневий клієнт storefront.views.monobank._monobank_api_request
з ОКРЕМИМ acquiring-токеном (settings.MONOBANK_ACQUIRING_TOKEN — той самий, що
для накладних менеджерів). Замовлення НЕ створюється тут (рішення Q2: лише після
оплати) — зберігаємо invoice_id на IgDeal, статус підхопить вебхук (Task 17).
"""
from __future__ import annotations

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
    deal.payment_status = "checking"
    deal.status = deal.Status.AWAITING_PAYMENT
    deal.save(update_fields=["invoice_id", "invoice_url", "payment_status", "status", "updated_at"])
    try:
        from management.services import bot_followups, ig_meta_events

        bot_followups.schedule_payment_followup(deal)
        ig_meta_events.log_or_send("InitiateCheckout", client=deal.client, deal=deal)
    except Exception:
        pass
    return {"ok": True, "invoice_id": invoice_id, "invoice_url": invoice_url}


# ---------------------------------------------------------------------------
# Статуси оплати угоди (Task 17). Замовлення створюється окремо (Task 18/19).
# ---------------------------------------------------------------------------
MONO_SUCCESS = {"success", "hold"}
MONO_PENDING = {"processing", "created"}
MONO_FAILURE = {"failure", "expired", "reversed", "canceled", "cancelled", "rejected"}


def apply_payment_status(deal, status_value, payload=None) -> str:
    """Застосовує статус Monobank до угоди (ідемпотентно). На успіх — позначає
    оплачено/передоплата, фіксує paid_at і просуває клієнта на стадію PAID.
    Створення замовлення підключається в Task 19 (через hook нижче)."""
    from django.utils import timezone

    status = (status_value or "").lower()

    if payload is not None:
        hist = deal.payment_payload if isinstance(deal.payment_payload, dict) else {}
        hist.setdefault("history", []).append(
            {"ts": timezone.now().isoformat(), "status": status}
        )
        deal.payment_payload = hist

    already_paid = deal.status in (deal.Status.PAID, deal.Status.ORDER_CREATED)

    if status in MONO_SUCCESS:
        target = "prepaid" if deal.pay_type == deal.PayType.PREPAY_200 else "paid"
        deal.payment_status = target
        if not already_paid:
            deal.status = deal.Status.PAID
        if deal.paid_at is None:
            deal.paid_at = timezone.now()
        deal.save(update_fields=["payment_status", "status", "paid_at", "payment_payload", "updated_at"])
        if not already_paid:
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
    elif status in MONO_PENDING:
        deal.payment_status = "checking"
        deal.save(update_fields=["payment_status", "payment_payload", "updated_at"])
    elif status in MONO_FAILURE:
        if not already_paid:
            deal.payment_status = "unpaid"
            deal.save(update_fields=["payment_status", "payment_payload", "updated_at"])
    else:
        deal.save(update_fields=["payment_payload", "updated_at"])
    return status


def _on_deal_paid(deal) -> None:
    """Хук «угоду оплачено» → пост-оплатний потік (створення замовлення/збір даних)."""
    try:
        from management.services import bot_orders

        bot_orders.on_deal_paid(deal)
    except Exception:
        pass


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
