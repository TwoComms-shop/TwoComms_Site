"""Детермінований роутер натискань кнопок: дія FSM в обхід моделі (Э1.4).

Натискання кнопки — це не «текст, схожий на намір», а точне однозначне
твердження клієнта про те, що він вибрав. Проганяти його через модель означає
додати ймовірність там, де її немає, і витратити провайдерський бюджет на
відомий заздалегідь результат. Тому payload читається тут, ДО будь-якого
звернення до моделі, і невідома дія просто повертає `None` — тоді хід іде
звичайним шляхом, а не ламається.

Payload версіонований (`twc:1:<action>:...`, див. `ig_message_templates`):
натискання на старій карточці після зміни семантики не виконає не ту дію,
яку клієнт бачив на екрані.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from django.utils import timezone

from management.models import InstagramBotMessage
from management.services.ig_message_templates import QuickReply, build_payload, parse_payload


ACTION_PARCEL = "parcel"
PARCEL_PICKED_UP = "got"
PARCEL_REMIND_LATER = "later"
ACTION_DIAGNOSTIC = "diagnostic"
DIAGNOSTIC_INOUT = "inout"
ACTION_PREVIEW = "preview"
_PREVIEW_CODE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

# Скільки чекати до повторного нагадування, якщо клієнт натиснув «нагадати».
REMIND_LATER_DELAY_HOURS = 20


@dataclass(frozen=True)
class PostbackOutcome:
    """Результат детермінованої дії: текст клієнту і причина для журналу."""

    action: str
    reply_text: str
    reason: str
    quick_replies: tuple = ()
    handled: bool = True


def parcel_quick_replies(order_id) -> tuple:
    """Кнопки під повідомленням «посилка у відділенні»."""
    return (
        QuickReply("Забрав ✅", build_payload(ACTION_PARCEL, PARCEL_PICKED_UP, str(order_id))),
        QuickReply("Нагадати пізніше", build_payload(ACTION_PARCEL, PARCEL_REMIND_LATER, str(order_id))),
    )


def inout_test_quick_replies(client_id) -> tuple:
    """One safe, client-bound button for an explicit owner In/Out check.

    The action has no commerce or CRM side effects.  Binding the payload to the
    recipient prevents a copied/stale diagnostic payload from being handled in
    another conversation.
    """
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id.isdigit() or int(normalized_client_id) <= 0:
        raise ValueError("diagnostic quick reply requires a positive client id")
    return (
        QuickReply(
            "IN ✅",
            build_payload(ACTION_DIAGNOSTIC, DIAGNOSTIC_INOUT, normalized_client_id),
        ),
    )


def build_preview_payload(client_id, variant: str, choice: str) -> str:
    """Build an inert, client-bound payload for visual format comparisons."""
    normalized_client_id = str(client_id or "").strip()
    normalized_variant = str(variant or "").strip().lower()
    normalized_choice = str(choice or "").strip().lower()
    if not normalized_client_id.isdigit() or int(normalized_client_id) <= 0:
        raise ValueError("preview payload requires a positive client id")
    if not _PREVIEW_CODE_RE.fullmatch(normalized_variant):
        raise ValueError("preview variant code is invalid")
    if not _PREVIEW_CODE_RE.fullmatch(normalized_choice):
        raise ValueError("preview choice code is invalid")
    return build_payload(
        ACTION_PREVIEW,
        normalized_variant,
        normalized_choice,
        normalized_client_id,
    )


def dispatch_postback(row: InstagramBotMessage) -> PostbackOutcome | None:
    """Виконати відому дію по натисканню кнопки або повернути None.

    None означає «це не наша кнопка» — хід продовжує звичайний шлях. Тихо
    проковтнути невідомий payload було б гірше: клієнт натиснув і не отримав
    нічого.
    """
    from management.services.ig_marketing_consent import PREFIX, handle_consent_reply

    if str(getattr(row, "quick_reply_payload", "") or "").startswith(PREFIX):
        return handle_consent_reply(row)
    parsed = parse_payload(getattr(row, "quick_reply_payload", "") or "")
    if not parsed or not row.client_id:
        return None
    action = parsed.get("action")
    args = parsed.get("args") or ()
    if action == ACTION_DIAGNOSTIC:
        return _handle_diagnostic(row, args)
    if action == ACTION_PREVIEW:
        return _handle_preview(row, args)
    if action != ACTION_PARCEL:
        return None
    if not args:
        return None
    intent = args[0]
    order_id = args[1] if len(args) > 1 else ""
    if intent == PARCEL_PICKED_UP:
        return _handle_parcel_picked_up(row, order_id)
    if intent == PARCEL_REMIND_LATER:
        return _handle_parcel_remind_later(row, order_id)
    return None


def _handle_diagnostic(row, args: tuple) -> PostbackOutcome | None:
    """Confirm a real Meta round trip without Gemini or business mutation."""
    if len(args) != 2 or args[0] != DIAGNOSTIC_INOUT:
        return None
    expected_client_id = str(args[1] or "").strip()
    if expected_client_id != str(row.client_id):
        return None
    language = _language(row)
    texts = {
        "uk": (
            "IN отримано ✅ Вхідний webhook і Router V2 спрацювали. "
            "OUT: це підтвердження надіслано без Gemini."
        ),
        "ru": (
            "IN получен ✅ Входящий webhook и Router V2 сработали. "
            "OUT: это подтверждение отправлено без Gemini."
        ),
        "en": (
            "IN received ✅ The inbound webhook and Router V2 worked. "
            "OUT: this confirmation was sent without Gemini."
        ),
    }
    return PostbackOutcome(
        action=f"{ACTION_DIAGNOSTIC}:{DIAGNOSTIC_INOUT}",
        reply_text=texts[language],
        reason="diagnostic_inout_roundtrip",
    )


def _handle_preview(row, args: tuple) -> PostbackOutcome | None:
    """Acknowledge a visual preview tap without granting consent or mutating CRM."""
    if len(args) != 3:
        return None
    variant, choice, expected_client_id = (str(value or "").strip() for value in args)
    if (
        expected_client_id != str(row.client_id)
        or not _PREVIEW_CODE_RE.fullmatch(variant)
        or not _PREVIEW_CODE_RE.fullmatch(choice)
    ):
        return None
    language = _language(row)
    texts = {
        "uk": (
            f"Демо-натискання отримано ✅ Варіант {variant}. "
            "Налаштування не змінено — це лише візуальний preview."
        ),
        "ru": (
            f"Демо-нажатие получено ✅ Вариант {variant}. "
            "Настройки не изменены — это только визуальный preview."
        ),
        "en": (
            f"Preview tap received ✅ Variant {variant}. "
            "No settings were changed; this is a visual preview only."
        ),
    }
    return PostbackOutcome(
        action=f"{ACTION_PREVIEW}:{variant}:{choice}",
        reply_text=texts[language],
        reason=f"visual_preview:{variant}:{choice}",
    )


def _language(row) -> str:
    from management.services.bot_sales_classifier import detect_language

    detected = detect_language(row.text or "")
    if detected in {"uk", "ru", "en"}:
        return detected
    client_language = str(getattr(row.client, "language", "") or "")
    return client_language if client_language in {"uk", "ru", "en"} else "uk"


def _handle_parcel_picked_up(row, order_id: str) -> PostbackOutcome:
    """Клієнт підтвердив отримання: більше жодних нагадувань про цю посилку.

    Це твердження клієнта, а не факт перевізника, тому статус замовлення тут НЕ
    змінюється: істина про доставку належить трекінгу. Але нагадування
    скасовуються одразу — інакше клієнт, який щойно написав «забрав», отримав би
    нагадування забрати.
    """
    language = _language(row)
    cancelled = _cancel_parcel_reminders(row.client_id, order_id)
    texts = {
        "uk": "Дякую, що повідомили! Замовлення позначаю як отримане, більше не нагадуватиму. Якщо з речами щось не так — напишіть, розберемось.",
        "ru": "Спасибо, что сообщили! Отмечаю заказ как полученный, больше не буду напоминать. Если с вещами что-то не так — напишите, разберёмся.",
        "en": "Thanks for letting me know! I am marking the order as received and will not remind you again. If anything is wrong with the items, just tell me.",
    }
    return PostbackOutcome(
        action=f"{ACTION_PARCEL}:{PARCEL_PICKED_UP}",
        reply_text=texts[language],
        reason=f"parcel_picked_up:{order_id}:cancelled={cancelled}",
    )


def _handle_parcel_remind_later(row, order_id: str) -> PostbackOutcome:
    """Клієнт попросив нагадати. Натискання САМО переоткриває вікно Meta.

    Це і є механізм, який робить нагадування легальним без окремого протоколу
    згоди: натискання — це вхідне повідомлення клієнта, тому наступні 24 години
    бот має право писати. Тому нагадування планується в межах цього вікна, а не
    через кілька суток.
    """
    from datetime import timedelta

    from management.models import IgFollowUpTask

    language = _language(row)
    due_at = timezone.now() + timedelta(hours=REMIND_LATER_DELAY_HOURS)
    texts = {
        "uk": "Добре, нагадаю про посилку. Якщо заберете раніше — просто напишіть «забрав», і я закрию питання.",
        "ru": "Хорошо, напомню про посылку. Если заберёте раньше — просто напишите «забрал», и я закрою вопрос.",
        "en": "Sure, I will remind you about the parcel. If you pick it up sooner, just reply \"picked up\" and I will close it.",
    }
    reminder_texts = {
        "uk": "Нагадую про посилку у відділенні — щоб її не відправили назад. Забрали? Напишіть «забрав».",
        "ru": "Напоминаю о посылке в отделении — чтобы её не отправили назад. Забрали? Напишите «забрал».",
        "en": "A reminder about your parcel at the branch, so it is not returned. Picked it up? Reply \"picked up\".",
    }
    IgFollowUpTask.objects.update_or_create(
        client_id=row.client_id,
        kind=IgFollowUpTask.Kind.MANAGER_TASK,
        reason=f"parcel_reminder:{order_id}",
        defaults={
            "due_at": due_at,
            "status": IgFollowUpTask.Status.PENDING,
            "trigger": IgFollowUpTask.Trigger.REACTIVE,
            "message_text": reminder_texts[language],
            "skip_reason": "",
            "last_error": "",
        },
    )
    return PostbackOutcome(
        action=f"{ACTION_PARCEL}:{PARCEL_REMIND_LATER}",
        reply_text=texts[language],
        reason=f"parcel_reminder_scheduled:{order_id}",
    )


def _cancel_parcel_reminders(client_id, order_id: str) -> int:
    from management.models import IgFollowUpTask, IgLifecycleEvent

    now = timezone.now()
    cancelled = IgFollowUpTask.objects.filter(
        client_id=client_id,
        reason=f"parcel_reminder:{order_id}",
        status=IgFollowUpTask.Status.PENDING,
    ).update(
        status=IgFollowUpTask.Status.CANCELLED,
        skip_reason="customer_confirmed_pickup",
        updated_at=now,
    )
    # Ще не надіслана подія «посилка у відділенні» теж скасовується: інакше
    # клієнт, який щойно написав «забрав», отримає нагадування забрати.
    IgLifecycleEvent.objects.filter(
        client_id=client_id,
        kind=IgLifecycleEvent.Kind.PARCEL_ARRIVED,
        state__in=(
            IgLifecycleEvent.State.PENDING,
            IgLifecycleEvent.State.WAITING_WINDOW,
        ),
    ).update(
        state=IgLifecycleEvent.State.CANCELLED,
        last_error="customer_confirmed_pickup",
        updated_at=now,
    )
    return int(cancelled or 0)
