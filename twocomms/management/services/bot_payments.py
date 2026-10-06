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
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

SITE = (getattr(settings, "BOT_PUBLIC_BASE_URL", "") or "https://twocomms.shop").rstrip("/")
WEBHOOK_PATH = "/payments/monobank/webhook/"
RETURN_PATH = "/"
# IMP-050. Сутки — компромисс между «клиент успеет заплатить утром» и
# «ссылка не живёт неделю без нашего ведома». Каскад добивки строится на
# T+23ч, то есть на последнем часе, когда ссылка ещё жива.
INVOICE_VALIDITY_SECONDS = 86400
PAYMENT_POLL_CADENCE_SECONDS = 240
PAYMENT_POLL_BATCH_MAX = 50
# One provider request may take 30 seconds. Keep the mutex alive for the
# complete worst-case batch so a slow provider cannot create overlap.
PAYMENT_POLL_LOCK_SECONDS = 1800
PAYMENT_POLL_CADENCE_KEY = "ig_payment_poll_backstop:cadence"
PAYMENT_POLL_LOCK_KEY = "ig_payment_poll_backstop:lock"
SUPERSEDED_INVOICE_MAX_AGE = timedelta(days=7)
SUPERSEDED_INVOICE_POLL_INTERVAL = timedelta(minutes=15)


def _ensure_invoice_lifecycle(
    deal,
    invoice_id: str,
    *,
    superseded_at=None,
    expires_at=None,
):
    """Materialize one invoice identity without changing payment truth."""
    from management.models import IgDealInvoiceLifecycle

    invoice_id = str(invoice_id or "").strip()
    if not invoice_id:
        return None
    now = timezone.now()
    lifecycle, created = IgDealInvoiceLifecycle.objects.get_or_create(
        invoice_id=invoice_id,
        defaults={
            "deal": deal,
            "superseded_at": superseded_at,
            "expires_at": expires_at,
            "next_poll_at": now,
        },
    )
    if lifecycle.deal_id != deal.pk:
        logger.error(
            "invoice lifecycle belongs to another deal: invoice=%s deal=%s existing=%s",
            invoice_id,
            deal.pk,
            lifecycle.deal_id,
        )
        return None
    update_fields = []
    if superseded_at is not None and lifecycle.superseded_at is None:
        lifecycle.superseded_at = superseded_at
        update_fields.append("superseded_at")
    if expires_at is not None and lifecycle.expires_at is None:
        lifecycle.expires_at = expires_at
        update_fields.append("expires_at")
    if created:
        return lifecycle
    if update_fields:
        lifecycle.save(update_fields=[*update_fields, "updated_at"])
    return lifecycle


def _mark_invoice_lifecycle(invoice_id: str, provider_status: str, *, now=None, error=""):
    from management.models import IgDealInvoiceLifecycle

    lifecycle = IgDealInvoiceLifecycle.objects.filter(invoice_id=invoice_id).first()
    if lifecycle is None:
        return None
    now = now or timezone.now()
    status = str(provider_status or "").strip().lower()
    if status in MONO_SUCCESS:
        lifecycle.status = IgDealInvoiceLifecycle.Status.PAID
        lifecycle.terminal_at = lifecycle.terminal_at or now
    elif status in MONO_FAILURE:
        lifecycle.status = IgDealInvoiceLifecycle.Status.FAILED
        lifecycle.terminal_at = lifecycle.terminal_at or now
    elif status in MONO_CANCELLED:
        lifecycle.status = (
            IgDealInvoiceLifecycle.Status.EXPIRED
            if status == "expired"
            else IgDealInvoiceLifecycle.Status.CANCELLED
        )
        lifecycle.terminal_at = lifecycle.terminal_at or now
    elif status in MONO_REVERSED:
        lifecycle.status = IgDealInvoiceLifecycle.Status.CANCELLED
        lifecycle.terminal_at = lifecycle.terminal_at or now
    lifecycle.provider_status = status[:32]
    lifecycle.last_polled_at = now
    lifecycle.next_poll_at = (
        None
        if lifecycle.status != IgDealInvoiceLifecycle.Status.OPEN
        else now + SUPERSEDED_INVOICE_POLL_INTERVAL
    )
    lifecycle.poll_attempts = int(lifecycle.poll_attempts or 0) + 1
    lifecycle.last_error = str(error or "")[:500]
    lifecycle.save(update_fields=[
        "status", "provider_status", "last_polled_at", "next_poll_at",
        "terminal_at", "poll_attempts", "last_error", "updated_at",
    ])
    return lifecycle


def _invoice_expired(deal, *, now=None) -> bool:
    """Whether the stored payment link is past its recorded lifetime.

    A deal created before this field existed has no expiry recorded. Treating
    that as expired would reissue links for every legacy deal, so absence of a
    date means "not known to be dead", not "dead".
    """
    expires_at = getattr(deal, "invoice_expires_at", None)
    if not expires_at:
        return False
    return (now or timezone.now()) >= expires_at


def invoice_link_state(deal, *, now=None) -> dict:
    """Answer «is the payment link still alive?» with a fact, not an assumption.

    Exists because the bot used to tell customers «посилання ще активне» without
    checking anything. ``unknown`` is a deliberate outcome: for a link issued
    before the TTL field existed we do not know, and saying so is cheaper than
    guessing wrong in front of a customer.
    """
    now = now or timezone.now()
    from management.models import IgDeal

    if deal is None:
        return {"status": "none", "expired": False, "expires_at": None}
    if deal.status in (IgDeal.Status.PAID, IgDeal.Status.ORDER_CREATED) or str(
        deal.payment_status or ""
    ) in {"paid", "prepaid"}:
        return {"status": "paid", "expired": False, "expires_at": None}
    if not deal.invoice_id or not deal.invoice_url:
        return {"status": "none", "expired": False, "expires_at": None}
    expires_at = getattr(deal, "invoice_expires_at", None)
    if not expires_at:
        return {"status": "unknown", "expired": False, "expires_at": None}
    expired = now >= expires_at
    return {
        "status": "expired" if expired else "live",
        "expired": expired,
        "expires_at": expires_at,
    }


def _destination(deal) -> str:
    items = list(deal.items.all())
    if items:
        names = ", ".join((i.title or "товар") for i in items[:3])
        return f"TwoComms: {names}"[:280]
    return "Замовлення TwoComms"


def create_payment_link(deal, *, force: bool = False) -> dict:
    """Створює invoice Monobank для угоди. Ідемпотентно: якщо invoice вже є —
    повертає його. Повертає {ok, invoice_id, invoice_url, error?, reused?}."""
    if (deal.payment_payload or {}).get("checkout_surface") == "instagram_proposal":
        from management.services.bot_orders import create_checkout_proposal_link

        items = [
            {
                "product_id": item.product_id,
                "color_variant_id": item.color_variant_id,
                "qty": item.qty,
                "size": item.size or "",
                "fit_option_code": item.fit_option_code or "",
                "option_values": dict(item.option_values or {}),
                "option_labels": dict(item.option_labels or {}),
            }
            for item in deal.items.select_related("product", "color_variant").order_by("id")
        ]
        evidence_ids = []
        for item in deal.items.all():
            for message_id in item.price_evidence_message_ids or []:
                if message_id not in evidence_ids:
                    evidence_ids.append(message_id)
        result = create_checkout_proposal_link(
            deal.client,
            deal=deal,
            pay_type=(
                "prepayment"
                if deal.pay_type == deal.PayType.PREPAYMENT
                else "online_full"
            ),
            item_specs=items,
            negotiated_total=deal.amount,
            requested_payment_amount=deal.requested_payment_amount or deal.amount,
            evidence={"message_ids": evidence_ids},
            locale=getattr(deal.client, "language", "") or "uk",
        )
        if result.get("ok"):
            return result
        return {
            "ok": False,
            "error": result.get("error", "proposal_error"),
            "missing_fields": result.get("missing_fields") or [],
            "item_index": result.get("item_index"),
        }
    if deal.invoice_id and deal.invoice_url and not force:
        # IMP-050: раньше переиспользовалась любая существующая ссылка, включая
        # мёртвую. Клиент получал «посилання ще активне» и упирался в 404,
        # а система не знала, что ссылка истекла.
        if not _invoice_expired(deal):
            try:
                from management.services import bot_followups

                bot_followups.schedule_invoice_expiry_event(deal)
            except Exception:
                pass
            return {
                "ok": True,
                "invoice_id": deal.invoice_id,
                "invoice_url": deal.invoice_url,
                "reused": True,
            }
        # Старый invoice_id уходит в историю: оплата по нему всё ещё возможна
        # физически, и без истории это станет «потерянным платежом» (F-PAY-001).
        history = list(deal.superseded_invoice_ids or [])
        if deal.invoice_id and deal.invoice_id not in history:
            history.append(deal.invoice_id)
            deal.superseded_invoice_ids = history[-20:]
            deal.save(update_fields=["superseded_invoice_ids", "updated_at"])

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
        # Без явного срока провайдер держит ссылку по своему усмотрению, и у нас
        # нет момента, про который можно сказать «посилання закінчилось».
        "validity": INVOICE_VALIDITY_SECONDS,
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
    deal.invoice_expires_at = timezone.now() + timedelta(
        seconds=INVOICE_VALIDITY_SECONDS
    )
    deal.save(update_fields=[
        "invoice_id", "invoice_url", "invoice_expires_at", "updated_at",
    ])
    _ensure_invoice_lifecycle(
        deal,
        invoice_id,
        expires_at=deal.invoice_expires_at,
    )
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
            bot_followups.schedule_invoice_expiry_event(deal)
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
    invoice_id = payload.get("invoiceId") or deal.invoice_id or ""
    source_value = (source or "provider")[:32]
    evidence = {
        "status": status,
        "amount": payload.get("amount"),
        "finalAmount": payload.get("finalAmount"),
        "modifiedDate": payload.get("modifiedDate"),
        "cancelList": cancel_evidence,
    }
    from management.ig_bot_models import provider_evidence_signature

    evidence["signature"] = provider_evidence_signature(
        deal_id=deal.pk,
        client_id=deal.client_id,
        provider="monobank",
        source=source_value,
        invoice_id=invoice_id,
        provider_status=status[:32],
        payload_digest=payload_digest,
    )
    event, _created = IgPaymentEvent.objects.get_or_create(
        event_key=event_key,
        defaults={
            "deal": deal,
            "client": deal.client,
            "provider": "monobank",
            "source": source_value,
            "invoice_id": invoice_id[:128],
            "provider_status": status[:32],
            "provider_modified_at": _provider_datetime(payload.get("modifiedDate")),
            "gross_amount": gross,
            "final_amount": final,
            "refunded_amount": refunded,
            "amount_valid": amount_valid,
            "currency": deal.currency or "UAH",
            "evidence": evidence,
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
        if became_verified:
            from management.services.ig_funnel_analytics import (
                record_verified_payment_in_transaction,
            )

            record_verified_payment_in_transaction(
                deal,
                provider_event=event,
                projection=projection,
                occurred_at=event.provider_modified_at or projection.paid_at,
            )
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
    try:
        from management.services.ig_commercial_episodes import sync_episode_payment

        sync_episode_payment(deal=deal)
    except Exception:
        pass
    if became_verified:
        try:
            from management.services.ig_objections import (
                resolve_client_objections_on_purchase,
            )

            resolve_client_objections_on_purchase(deal.client)
        except Exception as exc:
            logger.exception(
                "Could not resolve objection lifecycle for verified deal %s: %s",
                deal.pk,
                exc,
            )
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
    try:
        from management.services.bot_conversation_analysis import (
            schedule_client_truth_analysis,
        )

        schedule_client_truth_analysis(
            deal.client,
            trigger="payment_truth",
        )
    except Exception:
        # Payment truth is authoritative even if analysis scheduling is
        # temporarily unavailable; reconciliation will recover the watermark.
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
        deal.PaymentTruth.CONFIRMED: (
            "prepaid"
            if deal.pay_type in {deal.PayType.PREPAYMENT, deal.PayType.PREPAY_200}
            else "paid"
        ),
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
    apply_payment_reversal_to_stage(deal)


# Істини, після яких людина перестає бути покупцем за цією угодою.
# `failed` тут свідомо немає: невдала спроба оплати не скасовує угоду — клієнт
# може заплатити з другого разу. `partially_refunded` теж немає: часткове
# повернення не робить покупку такою, що не відбулась.
REVERSAL_TRUTHS = ("refunded", "reversed")


def apply_payment_reversal_to_stage(deal) -> bool:
    """Відкотити воронку й закрити угоду, коли гроші пішли назад.

    IMP-033, F-STATE-002 і F-STATE-003. До цього стадія лишалась `paid`, а в UI
    зʼявлялась псевдо-стадія `payment_reversed`, якої немає в переліку
    `IgClient.Stage`. Тобто система показувала людину покупцем після того, як ми
    повернули їй кошти — найдорожчий різновид неправди в цьому домені.

    Відкат явний і з причиною: у таймлайні він видимий як `regress` від
    `payment_provider`, а не як загадкова зміна даних. Нижче `checkout` не
    опускаємо: людина справді дійшла до вибору оплати, і відкидати її в «щойно
    написав» було б неправдою в іншу сторону.
    """
    truth = str(getattr(deal, "payment_truth", "") or "")
    if truth not in REVERSAL_TRUTHS:
        return False
    changed = False
    if getattr(deal, "status", None) != deal.Status.CANCELLED:
        deal.__class__.objects.filter(pk=deal.pk).update(status=deal.Status.CANCELLED)
        deal.status = deal.Status.CANCELLED
        changed = True

    client = getattr(deal, "client", None)
    if client is None or not getattr(client, "pk", None):
        return changed
    from management.models import IgClient
    from management.services.ig_funnel_fsm import regress_stage, stage_rank

    if stage_rank(client.stage) <= stage_rank(IgClient.Stage.CHECKOUT):
        return changed
    result = regress_stage(
        client,
        IgClient.Stage.CHECKOUT,
        reason=f"payment_{truth}",
        actor="payment_provider",
    )
    if result.changed:
        logger.info(
            "ig funnel regressed for client %s after payment %s: %s -> %s",
            client.pk, truth, result.from_stage, result.to_stage,
        )
    return changed or result.changed


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
    locator = IgPaymentProjection.objects.filter(pk=projection_id).values("deal_id").first()
    hosted_attempt = None
    if locator is not None:
        from management.models import IgCheckoutProposal
        hosted_attempt = IgCheckoutProposal.objects.filter(deal_id=locator["deal_id"],
            payment_attempt_id__isnull=False).order_by("-pk").values_list("payment_attempt_id", flat=True).first()

    with transaction.atomic():
        if hosted_attempt is not None:
            # Hosted ingestion holds Deal→Proposal→Generation→Attempt before
            # Projection. A joined Projection FOR UPDATE would reverse this
            # prefix and also lock unrelated client rows during repair.
            from management.services.ig_checkout_payment import _lock_attempt_proposal_graph
            _attempt, deal, _proposal = _lock_attempt_proposal_graph(hosted_attempt)
            if deal is None or deal.pk != locator["deal_id"]:
                return False
            projection = IgPaymentProjection.objects.select_for_update().filter(
                pk=projection_id, deal_id=deal.pk).first()
            if projection is not None:
                projection._state.fields_cache["deal"] = deal
        else:
            # Preserve the established non-hosted settlement path.
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
        if hosted_attempt is not None:
            # Keep the current receipt locked through every mirror write and
            # dirty-flag acknowledgement. No stale projection may escape this
            # prefix and overwrite a newer worker's completed repair.
            _sync_legacy_payment_mirror(projection)
            _mark_projection_reconciled(projection.pk)
            return True
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


def poll_deal_status(deal, *, invoice_id: str | None = None, apply: bool = True) -> str:
    """Pull-верифікація статусу invoice через acquiring-токен (захист від
    подделки вебхука) і застосування статусу. Повертає статус (lowercase).

    ``apply=False`` is mandatory for a superseded invoice: its payment must be
    surfaced to a manager, never silently attached to the deal's replacement
    configuration.
    """
    target_invoice_id = str(invoice_id or deal.invoice_id or "").strip()
    if not target_invoice_id:
        return ""
    token = (
        getattr(settings, "MONOBANK_ACQUIRING_TOKEN", None)
        or getattr(settings, "MONOBANK_TOKEN", None)
    )
    from storefront.views.monobank import MonobankAPIError, _monobank_api_request

    try:
        data = _monobank_api_request(
            "GET", "/api/merchant/invoice/status",
            params={"invoiceId": target_invoice_id}, token=token,
        )
    except Exception as exc:
        _mark_invoice_lifecycle(target_invoice_id, "", error=repr(exc))
        return ""
    status = (data.get("status") or data.get("statusCode") or "").lower()
    _mark_invoice_lifecycle(target_invoice_id, status)
    if status:
        if apply:
            apply_payment_status(deal, status, payload={**data, "invoiceId": target_invoice_id})
    return status


SUPERSEDED_INVOICE_HISTORY_LIMIT = 20


def supersede_invoice(deal) -> list[str]:
    """Запомнить текущий `invoice_id` и обнулить ссылку на оплату.

    F-PAY-001: раньше `invoice_id` просто затирался при смене товара или типа
    оплаты, а инвойс в Monobank оставался оплачиваемым. Платёж по старой
    ссылке приходил в webhook, сделка по нему не находилась, и деньги
    оказывались получены без сделки, заказа и уведомления менеджеру.

    Возвращает список изменённых полей — вызывающий включает его в
    `save(update_fields=...)`.
    """
    old = (deal.invoice_id or "").strip()
    fields = ["invoice_id", "invoice_url"]
    if old:
        _ensure_invoice_lifecycle(
            deal,
            old,
            superseded_at=timezone.now(),
            expires_at=getattr(deal, "invoice_expires_at", None),
        )
        history = [str(v) for v in (deal.superseded_invoice_ids or [])]
        if old not in history:
            history.append(old)
            deal.superseded_invoice_ids = history[-SUPERSEDED_INVOICE_HISTORY_LIMIT:]
            fields.append("superseded_invoice_ids")
    deal.invoice_id = ""
    deal.invoice_url = ""
    return fields


def _deal_for_superseded_invoice(invoice_id: str):
    """Найти сделку, для которой этот инвойс был замещён.

    Сравнение строго по элементу списка, а не по подстроке JSON: иначе
    `mono-invoice-ol` совпал бы с `mono-invoice-old`.
    """
    from management.models import IgDeal

    needle = (invoice_id or "").strip()
    if not needle:
        return None
    candidates = (
        IgDeal.objects.exclude(superseded_invoice_ids=[])
        .filter(superseded_invoice_ids__icontains=needle)
        .order_by("-id")[:50]
    )
    for deal in candidates:
        if needle in [str(v) for v in (deal.superseded_invoice_ids or [])]:
            return deal
    return None


def _materialize_superseded_invoice_lifecycles(limit: int = 100) -> None:
    """Backfill legacy JSON history into bounded lifecycle rows."""
    from management.models import IgDeal

    for deal in IgDeal.objects.exclude(superseded_invoice_ids=[]).order_by("id")[
        : max(1, int(limit or 100))
    ]:
        for invoice_id in deal.superseded_invoice_ids or []:
            invoice_id = str(invoice_id or "").strip()
            if not invoice_id:
                continue
            _ensure_invoice_lifecycle(
                deal,
                invoice_id,
                superseded_at=deal.updated_at or deal.created_at,
            )


def payment_poll_invoice_candidates(limit: int = 50):
    """Return due superseded invoices, stopping after expiry or a bounded age."""
    from django.db.models import Q
    from management.models import IgDealInvoiceLifecycle

    bounded_limit = payment_poll_limit(limit)
    _materialize_superseded_invoice_lifecycles(limit=bounded_limit * 2)
    now = timezone.now()
    IgDealInvoiceLifecycle.objects.filter(
        superseded_at__isnull=False,
        status=IgDealInvoiceLifecycle.Status.OPEN,
        expires_at__lt=now,
    ).update(
        status=IgDealInvoiceLifecycle.Status.EXPIRED,
        terminal_at=now,
        next_poll_at=None,
        updated_at=now,
    )
    IgDealInvoiceLifecycle.objects.filter(
        superseded_at__isnull=False,
        status=IgDealInvoiceLifecycle.Status.OPEN,
        expires_at__isnull=True,
        created_at__lt=now - SUPERSEDED_INVOICE_MAX_AGE,
    ).update(
        status=IgDealInvoiceLifecycle.Status.UNKNOWN,
        terminal_at=now,
        next_poll_at=None,
        updated_at=now,
    )
    return IgDealInvoiceLifecycle.objects.select_related("deal").filter(
        superseded_at__isnull=False,
        status=IgDealInvoiceLifecycle.Status.OPEN,
    ).filter(
        Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now)
    ).order_by("next_poll_at", "id")[:bounded_limit]


def handle_webhook_invoice(invoice_id, payload=None, request=None) -> bool:
    """Викликається з monobank_webhook, коли invoice не належить Order/накладній.
    Якщо це invoice угоди IG-бота — pull-верифікуємо і застосовуємо. True, якщо
    угоду знайдено й оброблено."""
    from management.models import IgDeal

    deal = IgDeal.objects.filter(invoice_id=invoice_id).order_by("-id").first()
    if deal:
        poll_deal_status(deal)
        return True

    # Платёж по замещённой ссылке. Деньги пришли, но актуальная сделка
    # описывает уже другой товар или другой тип оплаты, поэтому решение
    # обязан принять человек — автоматически применять такое нельзя.
    superseded = _deal_for_superseded_invoice(invoice_id)
    if not superseded:
        return False
    lifecycle = _ensure_invoice_lifecycle(
        superseded,
        invoice_id,
        superseded_at=timezone.now(),
    )
    import logging

    from management.services import instagram_bot

    poll_deal_status(superseded, invoice_id=invoice_id, apply=False)
    try:
        from management.services.ig_alerts import format_operator_alert

        instagram_bot.notify_manager(
            format_operator_alert(
                "⚠️ IG: оплата за заміненим посиланням",
                event_type="superseded_invoice_payment",
                client_id=superseded.client_id,
                deal_id=superseded.pk,
                task_id=getattr(lifecycle, "pk", None),
                status="manual_review_required",
                instruction_code="superseded_invoice_payment",
            ),
            dedupe_key=f"superseded-invoice:{getattr(lifecycle, 'pk', superseded.pk)}",
            event_type="superseded_invoice_payment",
            client=superseded.client if superseded.client_id else None,
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to alert manager about superseded invoice %s", invoice_id
        )
    return True


def payment_poll_candidates():
    """Return active invoices, preferring the transactional payment projection."""
    from management.models import IgDeal

    retryable_truths = (
        IgDeal.PaymentTruth.UNVERIFIED,
        IgDeal.PaymentTruth.PENDING,
        # A failed card attempt can be retried through the same live invoice.
        IgDeal.PaymentTruth.FAILED,
    )
    return (
        IgDeal.objects.filter(
            status__in=(
                IgDeal.Status.DRAFT,
                IgDeal.Status.QUOTED,
                IgDeal.Status.AWAITING_PAYMENT,
            ),
            order__isnull=True,
        )
        .filter(
            Q(payment_projection__truth__in=retryable_truths)
            | Q(payment_projection__isnull=True, payment_truth__in=retryable_truths)
        )
        .exclude(invoice_id="")
        .order_by("id")
    )


def payment_poll_limit(limit: int = 50) -> int:
    return max(1, min(int(limit or 50), PAYMENT_POLL_BATCH_MAX))


def poll_pending_deals(limit: int = 50) -> int:
    """Poll current invoices and bounded superseded invoice lifecycles."""
    bounded_limit = payment_poll_limit(limit)
    qs = payment_poll_candidates()[:bounded_limit]
    paid = 0
    for deal in qs:
        if poll_deal_status(deal) in MONO_SUCCESS:
            paid += 1
    for lifecycle in payment_poll_invoice_candidates(limit=bounded_limit):
        status = poll_deal_status(
            lifecycle.deal,
            invoice_id=lifecycle.invoice_id,
            apply=False,
        )
        if status not in MONO_SUCCESS:
            continue
        try:
            from management.services import instagram_bot
            from management.services.ig_alerts import format_operator_alert

            client = lifecycle.deal.client
            instagram_bot.notify_manager(
                format_operator_alert(
                    "⚠️ IG: оплата за заміненим посиланням",
                    event_type="superseded_invoice_payment",
                    client_id=client.pk,
                    deal_id=lifecycle.deal_id,
                    task_id=lifecycle.pk,
                    status="manual_review_required",
                    instruction_code="superseded_invoice_payment",
                ),
                dedupe_key=f"superseded-invoice:{lifecycle.pk}",
                event_type="superseded_invoice_payment",
                client=client,
            )
        except Exception:
            logger.exception("Failed to alert superseded invoice payment")
    return paid


def poll_pending_deals_locked(limit: int = 50) -> int | None:
    """Run the provider backstop once per cadence across cron and daemon."""
    if not cache.add(PAYMENT_POLL_LOCK_KEY, "1", timeout=PAYMENT_POLL_LOCK_SECONDS):
        return None
    try:
        if not cache.add(
            PAYMENT_POLL_CADENCE_KEY,
            "1",
            timeout=PAYMENT_POLL_CADENCE_SECONDS,
        ):
            return None
        try:
            return poll_pending_deals(limit=limit)
        except Exception:
            cache.delete(PAYMENT_POLL_CADENCE_KEY)
            raise
    finally:
        cache.delete(PAYMENT_POLL_LOCK_KEY)
