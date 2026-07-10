"""Scheduled follow-ups for the Instagram Direct sales bot."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationSignal,
    IgDeal,
    IgFollowUpTask,
    InstagramBotMessage,
    InstagramBotSettings,
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")
QUIET_START = time(10, 0)
QUIET_END = time(19, 0)
META_REPLY_WINDOW = timedelta(hours=23)


def _now() -> datetime:
    return timezone.now()


def _local(dt: datetime) -> datetime:
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, KYIV_TZ)
    return dt.astimezone(KYIV_TZ)


def next_allowed_send_at(candidate: datetime) -> datetime:
    """Return the next 10:00-19:00 Kyiv slot for an automated follow-up."""
    local = _local(candidate)
    if local.time() < QUIET_START:
        local = local.replace(hour=10, minute=0, second=0, microsecond=0)
    elif local.time() >= QUIET_END:
        local = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.get_current_timezone())


def meta_window_deadline(client: IgClient) -> datetime | None:
    base = client.last_message_at or client.first_contact_at
    if not base:
        return None
    return base + META_REPLY_WINDOW


def _update_client_next(client: IgClient) -> None:
    nxt = (
        IgFollowUpTask.objects.filter(client=client, status=IgFollowUpTask.Status.PENDING)
        .order_by("due_at", "id")
        .first()
    )
    client.next_followup_at = nxt.due_at if nxt else None
    client.save(update_fields=["next_followup_at", "updated_at"])


def cancel_pending(client: IgClient, *, reason: str = "") -> int:
    if not client:
        return 0
    count = IgFollowUpTask.objects.filter(
        client=client, status=IgFollowUpTask.Status.PENDING
    ).update(
        status=IgFollowUpTask.Status.CANCELLED,
        skip_reason=(reason or "cancelled")[:255],
        updated_at=_now(),
    )
    if count:
        _update_client_next(client)
    return count


def cancel_pending_for_deal(deal: IgDeal, *, reason: str = "") -> int:
    if not deal:
        return 0
    count = IgFollowUpTask.objects.filter(
        deal=deal, status=IgFollowUpTask.Status.PENDING
    ).update(
        status=IgFollowUpTask.Status.CANCELLED,
        skip_reason=(reason or "deal_cancelled")[:255],
        updated_at=_now(),
    )
    if count and deal.client_id:
        _update_client_next(deal.client)
    return count


def _client_allows_followup(client: IgClient) -> tuple[bool, str]:
    if client.hidden_at:
        return False, "hidden"
    if client.is_blocked or client.stage == IgClient.Stage.SPAM:
        return False, "spam"
    if client.manager_takeover or client.bot_paused:
        return False, "manager_takeover"
    if client.stage in {IgClient.Stage.PAID, IgClient.Stage.ORDER_CREATED, IgClient.Stage.DONE}:
        return False, "already_converted"
    if client.primary_objection == IgClient.Objection.NO_BUY or client.lost_reason in {"no_buy", "stop"}:
        return False, "client_no_buy"
    return True, ""


def schedule_followup(
    client: IgClient,
    *,
    kind: str,
    delay: timedelta,
    reason: str,
    now: datetime | None = None,
    deal: IgDeal | None = None,
    discount_percent: int = 0,
    message_text: str = "",
    level: int | None = None,
) -> IgFollowUpTask:
    """Create one pending follow-up, adjusted for quiet hours and Meta window."""
    now = now or _now()
    due = next_allowed_send_at(now + delay)
    deadline = meta_window_deadline(client)
    status = IgFollowUpTask.Status.PENDING
    skip_reason = ""
    task_kind = kind
    if deadline and due > deadline and kind != IgFollowUpTask.Kind.MANAGER_TASK:
        task_kind = IgFollowUpTask.Kind.MANAGER_TASK
        status = IgFollowUpTask.Status.SKIPPED
        skip_reason = "meta_window_closed"

    with transaction.atomic():
        IgFollowUpTask.objects.filter(
            client=client, status=IgFollowUpTask.Status.PENDING, kind=kind
        ).update(
            status=IgFollowUpTask.Status.CANCELLED,
            skip_reason="replaced",
            updated_at=_now(),
        )
        task = IgFollowUpTask.objects.create(
            client=client,
            deal=deal,
            due_at=due,
            status=status,
            kind=task_kind,
            level=client.followup_level if level is None else level,
            reason=(reason or "")[:120],
            discount_percent=max(0, min(10, int(discount_percent or 0))),
            meta_window_deadline=deadline,
            message_text=message_text or "",
            skip_reason=skip_reason,
        )
    _update_client_next(client)
    return task


def next_discount_percent(client: IgClient, *, explicit_negotiation: bool = False) -> int:
    current = int(client.discount_offered_percent or 0)
    if current >= 10:
        return 0
    if explicit_negotiation:
        return 10 if current < 10 else 0
    if current <= 0 and int(client.followup_level or 0) >= 1:
        return 5
    return 0


def schedule_rescue_offer(client: IgClient, *, explicit_negotiation: bool = False, now: datetime | None = None) -> IgFollowUpTask | None:
    pct = next_discount_percent(client, explicit_negotiation=explicit_negotiation)
    if not pct:
        return None
    if client.stage not in {
        IgClient.Stage.PRODUCT_MATCHED,
        IgClient.Stage.CHECKOUT,
        IgClient.Stage.PAYMENT_PENDING,
    }:
        return None
    return schedule_followup(
        client,
        kind=IgFollowUpTask.Kind.RESCUE if pct == 5 else IgFollowUpTask.Kind.FINAL,
        delay=timedelta(hours=12),
        reason="discount_rescue",
        now=now,
        discount_percent=pct,
        level=int(client.followup_level or 0) + 1,
    )


def schedule_payment_followup(deal: IgDeal, *, now: datetime | None = None) -> IgFollowUpTask | None:
    if not deal or not deal.client_id:
        return None
    return schedule_followup(
        deal.client,
        kind=IgFollowUpTask.Kind.PAYMENT,
        delay=timedelta(minutes=45),
        reason="payment_link_unpaid",
        now=now,
        deal=deal,
    )


def schedule_after_inbound(client: IgClient, *, reason: str = "client_reply") -> None:
    """A client reply cancels automated reminders until the bot/manager answers again."""
    cancel_pending(client, reason=reason)


def schedule_after_bot_reply(client: IgClient, *, reply: str = "", control: dict | None = None, deal: IgDeal | None = None) -> IgFollowUpTask | None:
    if not client:
        return None
    allowed, why = _client_allows_followup(client)
    if not allowed:
        cancel_pending(client, reason=why)
        return None
    if deal and deal.status == IgDeal.Status.AWAITING_PAYMENT:
        return schedule_payment_followup(deal)
    if client.stage == IgClient.Stage.PAYMENT_PENDING:
        return schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.PAYMENT,
            delay=timedelta(minutes=45),
            reason="payment_link_unpaid",
        )
    if client.primary_objection in {IgClient.Objection.THINKING, IgClient.Objection.PRICE}:
        return schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.THINKING,
            delay=timedelta(hours=12),
            reason="thinking_or_price_hesitation",
        )
    if client.stage in {IgClient.Stage.NEW, IgClient.Stage.QUALIFYING, IgClient.Stage.PRODUCT_MATCHED, IgClient.Stage.CHECKOUT}:
        return schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            delay=timedelta(hours=2),
            reason="qualification_unanswered",
        )
    return None


def _lang(client: IgClient) -> str:
    return "ru" if (client.language or "").lower().startswith("ru") else "uk"


def compose_followup(task: IgFollowUpTask) -> str:
    client = task.client
    ru = _lang(client) == "ru"
    pct = int(task.discount_percent or 0)
    if task.kind == IgFollowUpTask.Kind.PAYMENT:
        return (
            "Напомню: ссылка на оплату еще активна. Если что-то не открывается или нужно помочь с оплатой - напишите, подскажу."
            if ru else
            "Нагадаю: посилання на оплату ще активне. Якщо щось не відкривається або треба допомогти з оплатою - напишіть, підкажу."
        )
    if pct == 10:
        return (
            "Могу предложить финальный вариант: скидка 10% на этот заказ. Если не подходит - все ок, больше не буду вас отвлекать."
            if ru else
            "Можу запропонувати фінальний варіант: знижка 10% на це замовлення. Якщо не підходить - все ок, більше не буду вас відволікати."
        )
    if pct == 5:
        return (
            "Как для первого заказа можем сделать небольшую скидку 5%. Если хотите - помогу быстро оформить."
            if ru else
            "Як для першого замовлення можемо зробити невелику знижку 5%. Якщо хочете - допоможу швидко оформити."
        )
    if task.kind == IgFollowUpTask.Kind.THINKING:
        return (
            "Хотел уточнить, получилось подумать по заказу? Если есть вопрос по размеру, ткани или оплате - подскажу коротко."
            if ru else
            "Хотів уточнити, чи вийшло подумати щодо замовлення? Якщо є питання по розміру, тканині або оплаті - коротко підкажу."
        )
    return (
        "Подскажите, пожалуйста, актуален еще заказ? Могу помочь с размером, цветом или оплатой."
        if ru else
        "Підкажіть, будь ласка, чи актуальне ще замовлення? Можу допомогти з розміром, кольором або оплатою."
    )


def _mark_skipped(task: IgFollowUpTask, reason: str) -> None:
    task.status = IgFollowUpTask.Status.SKIPPED
    task.skip_reason = (reason or "skipped")[:255]
    task.updated_at = _now()
    task.save(update_fields=["status", "skip_reason", "updated_at"])
    _update_client_next(task.client)


def _claim_due_followup(
    task_id: int, *, now: datetime, automation
) -> tuple[IgFollowUpTask, IgClient, str] | None:
    """Obtain the shared client lease and re-read one pending follow-up.

    The initial due-task list is intentionally only a list of ids. Hide can
    cancel a task after that list is read, so no stale task/client object may
    reach the send path.
    """
    candidate = IgFollowUpTask.objects.filter(
        pk=task_id,
        status=IgFollowUpTask.Status.PENDING,
        due_at__lte=now,
    ).values("client_id").first()
    if not candidate:
        return None
    client, lease_token = automation.acquire_client_automation_lease(
        candidate["client_id"]
    )
    if not client:
        # Busy means another automation owns this client and the task remains
        # pending. A real policy block keeps the prior behavior: skip it once
        # so it cannot be reconsidered forever after a pause/hide/paid state.
        fresh_client = IgClient.objects.filter(pk=candidate["client_id"]).first()
        if fresh_client:
            allowed, why = _client_allows_followup(fresh_client)
            if not allowed:
                stale_task = IgFollowUpTask.objects.select_related("client").filter(
                    pk=task_id,
                    client_id=fresh_client.id,
                    status=IgFollowUpTask.Status.PENDING,
                ).first()
                if stale_task:
                    stale_task.client = fresh_client
                    _mark_skipped(stale_task, why)
        return None
    task = IgFollowUpTask.objects.select_related("client").filter(
        pk=task_id,
        client_id=client.id,
        status=IgFollowUpTask.Status.PENDING,
        due_at__lte=now,
    ).first()
    if not task:
        automation.release_client_automation_lease(client.id, lease_token)
        return None
    task.client = client
    allowed, why = _client_allows_followup(client)
    if not allowed:
        _mark_skipped(task, why)
        automation.release_client_automation_lease(client.id, lease_token)
        return None
    return task, client, lease_token


def _renew_due_followup_claim(
    task_id: int, client_id: int, lease_token: str, *, now: datetime, automation
) -> tuple[IgFollowUpTask, IgClient] | None:
    """Last no-I/O check: task remains pending and the client remains active."""
    client = automation.renew_client_automation_lease(client_id, lease_token)
    if not client:
        return None
    task = IgFollowUpTask.objects.select_related("client").filter(
        pk=task_id,
        client_id=client.id,
        status=IgFollowUpTask.Status.PENDING,
        due_at__lte=now,
    ).first()
    if not task:
        return None
    task.client = client
    allowed, why = _client_allows_followup(client)
    if not allowed:
        _mark_skipped(task, why)
        return None
    return task, client


def process_due_followups(s: InstagramBotSettings | None = None, *, now: datetime | None = None, limit: int = 20) -> int:
    s = s or InstagramBotSettings.load()
    now = now or _now()
    sent = 0
    task_ids = list(
        IgFollowUpTask.objects
        .filter(status=IgFollowUpTask.Status.PENDING, due_at__lte=now)
        .order_by("due_at", "id")[:limit]
        .values_list("id", flat=True)
    )
    from management.services import instagram_bot

    for task_id in task_ids:
        claim = _claim_due_followup(task_id, now=now, automation=instagram_bot)
        if not claim:
            continue
        task, client, lease_token = claim
        try:
            if task.meta_window_deadline and now > task.meta_window_deadline:
                _mark_skipped(task, "meta_window_closed")
                continue
            allowed_time = next_allowed_send_at(now)
            if allowed_time > now + timedelta(seconds=1):
                task.due_at = allowed_time
                task.save(update_fields=["due_at", "updated_at"])
                _update_client_next(client)
                continue
            text = (task.message_text or "").strip() or compose_followup(task)
            renewed = _renew_due_followup_claim(
                task.id, client.id, lease_token, now=now, automation=instagram_bot
            )
            if not renewed:
                continue
            task, client = renewed
            try:
                ok, kind, hint = instagram_bot.send_text(s, client.igsid, text)
            except Exception as exc:
                ok, kind, hint = False, "transient", repr(exc)
            if not ok:
                if kind == "permanent":
                    _mark_skipped(task, hint or "send_blocked")
                continue
            msg = InstagramBotMessage.objects.create(
                sender_id=client.igsid,
                client=client,
                role=InstagramBotMessage.Role.MODEL,
                text=text,
                status=InstagramBotMessage.Status.DONE,
                source="followup",
                processed_at=now,
            )
            task.status = IgFollowUpTask.Status.SENT
            task.sent_at = now
            task.sent_message = msg
            task.save(update_fields=["status", "sent_at", "sent_message", "updated_at"])
            client.followup_level = max(int(client.followup_level or 0), int(task.level or 0) + 1)
            if task.discount_percent:
                client.discount_offered_percent = max(
                    int(client.discount_offered_percent or 0), int(task.discount_percent or 0)
                )
                try:
                    IgConversationSignal.objects.create(
                        client=client,
                        message=msg,
                        signal_type=IgConversationSignal.Type.DISCOUNT_OFFER,
                        value=str(task.discount_percent),
                        payload={"discount_percent": task.discount_percent},
                    )
                except Exception:
                    pass
            client.last_bot_reply_at = now
            client.next_followup_at = None
            client.save(update_fields=[
                "followup_level", "discount_offered_percent", "last_bot_reply_at",
                "next_followup_at", "updated_at",
            ])
            sent += 1
            if not task.discount_percent and task.kind in {
                IgFollowUpTask.Kind.QUALIFICATION,
                IgFollowUpTask.Kind.THINKING,
                IgFollowUpTask.Kind.PAYMENT,
            }:
                schedule_rescue_offer(client, now=now)
        finally:
            instagram_bot.release_client_automation_lease(client.id, lease_token)
    return sent
