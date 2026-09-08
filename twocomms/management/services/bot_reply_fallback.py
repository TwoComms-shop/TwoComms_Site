"""Deterministic, privacy-safe customer replies when Gemini is unavailable."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from management.models import IgDeal, IgFollowUpTask


ORDER_REFERENCE_RE = re.compile(r"\b(TWC[A-Z0-9-]{5,30})\b", re.I)


def _language(row) -> str:
    from management.services.bot_sales_classifier import detect_language

    detected = detect_language(row.text or "")
    if detected in {"uk", "ru", "en"}:
        return detected
    client_language = str(getattr(row.client, "language", "") or "")
    return client_language if client_language in {"uk", "ru", "en"} else "uk"


def _order_reference(text: str) -> str:
    match = ORDER_REFERENCE_RE.search(str(text or ""))
    return match.group(1).upper() if match else ""


def _linked_order(client, reference: str):
    if not client or not reference:
        return None
    from management.services.ig_commercial_episodes import (
        OrderResolutionError,
        resolve_client_order,
    )

    try:
        return resolve_client_order(client, reference).order
    except OrderResolutionError:
        return None


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _order_payment_state(client, order) -> dict:
    """Return source-qualified payment state for exactly this client/order.

    ``Order.payment_status`` is a legacy display field and can remain ``paid``
    after a reversal or refund.  Customer-facing fallback replies must use the
    Instagram payment ledger instead.
    """
    if not client or not order:
        return ""
    from management.services.bot_payment_truth import verified_payment_deals

    deals = IgDeal.objects.filter(client_id=client.pk, order_id=order.pk).select_related(
        "payment_projection"
    )
    if not deals.exists():
        return {}
    deal = (
        deals.exclude(payment_projection__isnull=True)
        .order_by("-payment_projection__updated_at", "-id")
        .first()
    ) or deals.order_by("-payment_truth_updated_at", "-id").first()
    from management.services.ig_commercial_episodes import payment_truth_snapshot

    state = payment_truth_snapshot(deal=deal, order=order)
    truth = str(state.get("provider_truth") or deal.payment_truth or "")
    if not state.get("provider_truth") and not verified_payment_deals(
        deals.filter(pk=deal.pk)
    ).exists():
        truth = str(deal.payment_truth or "")
    paid_amount = _money(state.get("confirmed_paid_amount"))
    if paid_amount <= 0 and truth in {
        IgDeal.PaymentTruth.CONFIRMED,
        IgDeal.PaymentTruth.PARTIALLY_REFUNDED,
    }:
        paid_amount = max(
            _money(deal.paid_amount) - _money(deal.refunded_amount),
            Decimal("0.00"),
        )
    order_total = _money(state.get("order_total"))
    remaining = (
        max(order_total - paid_amount, Decimal("0.00"))
        if order_total > 0 and paid_amount > 0
        else Decimal("0.00")
    )
    legacy_full_payment_mismatch = bool(
        truth == IgDeal.PaymentTruth.CONFIRMED
        and deal.pay_type == IgDeal.PayType.ONLINE_FULL
        and order_total > 0
        and paid_amount != order_total
    )
    return {
        **state,
        "truth": truth,
        "pay_type": deal.pay_type,
        "paid_amount": paid_amount,
        "order_total_amount": order_total,
        "remaining_amount_value": remaining,
        "needs_reconciliation": bool(
            state.get("needs_reconciliation")
            or legacy_full_payment_mismatch
            or (truth == IgDeal.PaymentTruth.CONFIRMED and paid_amount <= 0)
        ),
    }


def _order_reply(order, language: str, *, payment_state: dict) -> str:
    number = order.order_number
    tracking = str(order.tracking_number or "").strip()
    paid_amount = _money(payment_state.get("paid_amount"))
    remaining = _money(payment_state.get("remaining_amount_value"))
    prepayment = bool(
        paid_amount > 0
        and remaining > 0
        and payment_state.get("pay_type")
        in {IgDeal.PayType.PREPAYMENT, IgDeal.PayType.PREPAY_200}
    )
    status = order.status or "new"

    if language == "en":
        payment = " is fully paid and"
        if prepayment:
            payment = (
                f" has a confirmed prepayment of {paid_amount:.2f} UAH, with "
                f"{remaining:.2f} UAH remaining, and"
            )
        states = {
            "new": " is currently being processed",
            "prep": " is being prepared for shipment",
            "ship": " has been shipped",
            "done": " is marked as received",
            "cancelled": " is marked as cancelled",
        }
        reply = f"Order {number}{payment}{states.get(status, ' is being processed')}."
        if tracking:
            return f"{reply} Tracking number: {tracking}."
        if status in {"new", "prep"}:
            return f"{reply} It has not been marked as shipped yet."
        return reply

    if language == "ru":
        payment = " полностью оплачен и"
        if prepayment:
            payment = (
                f" имеет подтвержденную предоплату {paid_amount:.2f} грн, осталось "
                f"оплатить {remaining:.2f} грн, и"
            )
        states = {
            "new": " сейчас в обработке",
            "prep": " готовится к отправке",
            "ship": " уже отправлен",
            "done": " отмечен как полученный",
            "cancelled": " отмечен как отмененный",
        }
        reply = f"Заказ {number}{payment}{states.get(status, ' находится в обработке')}."
        if tracking:
            return f"{reply} ТТН: {tracking}."
        if status in {"new", "prep"}:
            return f"{reply} Отправка пока не подтверждена."
        return reply

    payment = " повністю оплачено й"
    if prepayment:
        payment = (
            f" має підтверджену передоплату {paid_amount:.2f} грн, залишилося "
            f"сплатити {remaining:.2f} грн, і"
        )
    states = {
        "new": " зараз в обробці",
        "prep": " готується до відправлення",
        "ship": " вже відправлено",
        "done": " позначено як отримане",
        "cancelled": " позначено як скасоване",
    }
    reply = f"Замовлення {number}{payment}{states.get(status, ' перебуває в обробці')}."
    if tracking:
        return f"{reply} ТТН: {tracking}."
    if status in {"new", "prep"}:
        return f"{reply} Відправлення ще не підтверджене."
    return reply


def _handoff_reply(kind: str, language: str) -> str:
    if language == "en":
        if kind == "collaboration":
            return (
                "Thank you for reaching out about a collaboration. I've passed your "
                "proposal to our manager, who will review it and reply here."
            )
        if kind in {"order_unverified", "order_payment_unverified"}:
            return (
                "Thanks for your message. I couldn't safely verify this order against "
                "this Instagram conversation, so I've passed it to a manager for a "
                "manual check. They will reply here shortly."
            )
        if kind == "order_partial_refund":
            return (
                "A partial refund is recorded for this order. I have passed the "
                "conversation to a manager so they can confirm the remaining details."
            )
        return (
            "Thanks for your message. I'm temporarily unable to prepare a detailed "
            "answer, so I've passed it to a manager. They will reply here shortly."
        )
    if language == "ru":
        if kind == "collaboration":
            return (
                "Спасибо за предложение о сотрудничестве. Я передала его менеджеру, "
                "он изучит детали и ответит вам здесь."
            )
        if kind in {"order_unverified", "order_payment_unverified"}:
            return (
                "Спасибо за сообщение. Я не могу безопасно подтвердить связь этого "
                "заказа с перепиской, поэтому передала запрос менеджеру на ручную проверку."
            )
        if kind == "order_partial_refund":
            return (
                "По этому заказу зафиксирован частичный возврат. Я передала переписку "
                "менеджеру, чтобы он подтвердил оставшиеся детали."
            )
        return (
            "Спасибо за сообщение. Сейчас я не могу подготовить точный ответ, поэтому "
            "передала вопрос менеджеру. Он ответит вам здесь."
        )
    if kind == "collaboration":
        return (
            "Дякую за пропозицію співпраці. Я передала її менеджеру, він перегляне "
            "деталі та відповість вам тут."
        )
    if kind in {"order_unverified", "order_payment_unverified"}:
        return (
            "Дякую за повідомлення. Я не можу безпечно підтвердити зв'язок цього "
            "замовлення з перепискою, тому передала запит менеджеру на ручну перевірку."
        )
    if kind == "order_partial_refund":
        return (
            "За цим замовленням зафіксовано часткове повернення. Я передала переписку "
            "менеджеру, щоб він підтвердив решту деталей."
        )
    return (
        "Дякую за повідомлення. Зараз я не можу підготувати точну відповідь, тому "
        "передала питання менеджеру. Він відповість вам тут."
    )


def _outage_holding_reply(language: str) -> str:
    """A truthful short hold while the durable AI recovery prepares the answer."""
    if language == "en":
        return (
            "Sorry for the technical delay. I'm restoring the details now and "
            "will reply here shortly."
        )
    if language == "ru":
        return (
            "Извините за техническую задержку. Я восстанавливаю детали и "
            "скоро отвечу вам здесь."
        )
    return (
        "Перепрошую за технічну затримку. Я відновлюю деталі й "
        "невдовзі відповім вам тут."
    )


def is_generic_provider_outage(
    row, *, failure_kind: str = "", next_due_at=None, horizon_at=None,
) -> bool:
    """Whether a bounded provider interruption may receive automatic recovery."""
    if failure_kind == "provider_wait":
        from management.services.ig_legacy_provider_execution import classify_legacy_provider_failure

        if classify_legacy_provider_failure(
            failure_kind, next_due_at=next_due_at, horizon_at=horizon_at,
        ).state != "wait":
            return False
    elif failure_kind != "provider_outage":
        return False
    reference = _order_reference(row.text)
    if reference:
        return False
    from management.services.bot_sales_classifier import COLLAB_RE, SUPPORT_RE

    return not bool(SUPPORT_RE.search(row.text or "") or COLLAB_RE.search(row.text or ""))


def _queue_manager_handoff(row, *, kind: str, reference: str = "") -> None:
    client = row.client
    reason = f"ai_fallback:{kind}:{row.pk}"
    message_text = (
        f"Gemini недоступний. Потрібна ручна відповідь на повідомлення "
        f"ID {row.pk}. Відкрийте діалог у CRM."
    )
    if client:
        task, created = IgFollowUpTask.objects.get_or_create(
            client=client,
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason=reason,
            defaults={
                "due_at": timezone.now(),
                "status": IgFollowUpTask.Status.SKIPPED,
                "skip_reason": "human_agent_required",
                "message_text": message_text,
                "last_error": "gemini_unavailable",
            },
        )
        if not created:
            changed = []
            for field, value in (
                ("status", IgFollowUpTask.Status.SKIPPED),
                ("skip_reason", "human_agent_required"),
                ("message_text", message_text),
                ("last_error", "gemini_unavailable"),
            ):
                if getattr(task, field) != value:
                    setattr(task, field, value)
                    changed.append(field)
            if changed:
                changed.append("updated_at")
                task.save(update_fields=changed)

    from management.services.instagram_bot import notify_manager

    from management.services.ig_alerts import format_technical_alert

    notify_manager(
        format_technical_alert(
            "⚠️ IG: Gemini недоступний; потрібна ручна перевірка",
            event_type="ai_reply_fallback",
            client_id=getattr(client, "pk", None),
            message_id=row.pk,
            failure_kind=kind,
            instruction_code="fallback_ready",
        ),
        dedupe_key=f"ig_ai_fallback:{row.pk}",
        event_type="ai_reply_fallback",
        client=client,
    )


def build_ai_failure_fallback(
    row,
    *,
    provider_outage: bool = False,
    holding_decision=None,
) -> tuple[str, bool]:
    """Build one useful response without inventing product, order, or payment facts.

    ``holding_decision`` — рішення з `ig_provider_incidents.holding_decision()`.
    Технічний holding відправляється ТІЛЬКИ коли воно дозволяє: раніше цей текст
    вибирався для будь-якого generic-сбою з `provider_outage`, без перевірок
    «уже надсилали», «є відкритий інцидент», «є активна recovery», «є takeover».
    """
    language = _language(row)
    reference = _order_reference(row.text)
    from management.services.bot_sales_classifier import COLLAB_RE, SUPPORT_RE

    if SUPPORT_RE.search(row.text or ""):
        kind = "support"
        _queue_manager_handoff(row, kind=kind, reference=reference)
        return _handoff_reply(kind, language), True

    order = _linked_order(row.client, reference)
    if order is not None:
        payment_state = _order_payment_state(row.client, order)
        if (
            payment_state.get("truth") == IgDeal.PaymentTruth.CONFIRMED
            and not payment_state.get("needs_reconciliation")
        ):
            return _order_reply(order, language, payment_state=payment_state), False
        if payment_state.get("truth") == IgDeal.PaymentTruth.PARTIALLY_REFUNDED:
            kind = "order_partial_refund"
        else:
            kind = "order_payment_unverified"
    else:
        kind = "order_unverified" if reference else "generic"

    if not reference:
        kind = "collaboration" if COLLAB_RE.search(row.text or "") else "generic"
    if kind == "generic" and row.client_id and provider_outage:
        if holding_decision is not None and not holding_decision.should_send:
            # Придушено: клієнту не надсилається жодного технічного тексту.
            # Викликаючий шар терминалізує хід і, якщо він вимагає відповіді,
            # покладається на єдиний курсор відновлення.
            return "", False
        return _outage_holding_reply(language), False
    _queue_manager_handoff(row, kind=kind, reference=reference)
    return _handoff_reply(kind, language), True
