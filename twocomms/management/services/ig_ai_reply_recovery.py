"""Durable recovery for an unanswered Instagram AI turn.

This service intentionally has a smaller surface than the normal live-reply
worker.  It represents one source inbound and crosses Meta's non-idempotent
Send API at most once.  A provider receipt is therefore required before the
intent is considered delivered; every unconfirmed result goes to manual review
instead of being replayed.
"""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from management.models import (
    IgAiReplyRecoveryJob,
    IgClient,
    IgClientDegradationEpisode,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_reply_boundary import (
    capture_reply_permission,
    customer_send_boundary,
)
from management.services.ig_delivery_receipts import normalize_provider_message_id
from management.services.ig_response_control import ValidatedResponse, parse_legacy_response
from management.services.instagram_bot import (
    HISTORY_LIMIT,
    acquire_client_automation_lease,
    gemini_generate,
    notify_manager,
    release_client_automation_lease,
    send_text,
)


RESPONSE_WINDOW = timedelta(hours=23)
JOB_LEASE_DURATION = timedelta(minutes=5)
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_RETRY_BASE_SECONDS = 20
RECOVERY_RETRY_MAX_SECONDS = 300
# Пауза між перевірками стану інциденту. Спроба НЕ витрачається: курсор просто
# чекає, поки провайдер перейде в `RECOVERING`/`CLOSED`.
RECOVERY_INCIDENT_WAIT_SECONDS = 45
# ``send_text`` splits at 950 characters. Recovery deliberately fits one
# customer message into one non-idempotent Meta request.
MAX_RECOVERY_REPLY_CHARS = 950
# Тексти вибачень і вся семантика «одне вибачення на хід» живуть в одному
# місці — `ig_apology_policy`. Раніше код і prompt вибачались незалежно, і
# клієнт отримував два вибачення підряд.
from management.services.ig_apology_policy import (  # noqa: E402
    APOLOGY_EN as RECOVERY_APOLOGY_EN,
    APOLOGY_RU as RECOVERY_APOLOGY_RU,
    APOLOGY_UK as RECOVERY_APOLOGY_UK,
    apply_apology_policy,
)

_TERMINAL_STATUSES = frozenset({
    IgAiReplyRecoveryJob.Status.SENT,
    IgAiReplyRecoveryJob.Status.CANCELLED,
    IgAiReplyRecoveryJob.Status.AMBIGUOUS,
    IgAiReplyRecoveryJob.Status.FAILED,
})


def _active_opt_out(client: IgClient | None) -> bool:
    return bool(
        client
        and client.opted_out_at
        and (not client.opted_in_at or client.opted_in_at < client.opted_out_at)
    )


def _source_event_at(source: InstagramBotMessage):
    return source.provider_created_at or source.created_at or timezone.now()


def _window_deadline(source: InstagramBotMessage, client: IgClient):
    anchor = client.meta_window_anchor or _source_event_at(source)
    return anchor + RESPONSE_WINDOW if anchor else None


def _trim_draft(text: str) -> str:
    """Keep recovery to one Meta text request without silently making controls."""
    if not isinstance(text, str):
        return ""
    parsed = parse_legacy_response(text)
    if not parsed.reply_text or parsed.error == "invalid_reply_text":
        return ""
    clean = parsed.reply_text.strip()
    if len(clean.encode("utf-8")) <= MAX_RECOVERY_REPLY_CHARS:
        return clean
    # Meta's limit is byte-based.  Slice bytes and decode losslessly so a
    # Ukrainian/Russian multibyte character never creates a second chunk.
    head = clean.encode("utf-8")[: MAX_RECOVERY_REPLY_CHARS - 3].decode(
        "utf-8", errors="ignore"
    )
    shortened = head.rsplit(" ", 1)[0].strip()
    return f"{shortened or head}..."


def _ensure_recovery_apology(
    draft: str,
    source_text: str,
    *,
    apology_already_delivered: bool = False,
) -> tuple[str, int]:
    """Сума вибачень у логічному ході ≤ 1 (holding і recovery разом).

    Повертає (текст, число вибачень у цьому тексті), щоб епізод рахував
    вибачення по факту надісланого тексту, а не по флагах.
    """
    clean = _trim_draft(draft)
    if not clean:
        return "", 0
    from management.services.bot_sales_classifier import detect_language

    language = detect_language(source_text) or detect_language(clean)
    if language not in {"uk", "ru", "en"}:
        language = "uk"
    normalized, apologies = apply_apology_policy(
        clean,
        language=language,
        apology_already_delivered=apology_already_delivered,
    )
    trimmed = _trim_draft(normalized)
    if not trimmed:
        return "", 0
    return trimmed, apologies


def _apology_already_delivered(job: IgAiReplyRecoveryJob) -> bool:
    """Чи витрачене вибачення ходу вже доставленим holding-ом."""
    episode = getattr(job, "degradation_episode", None)
    if episode is not None and int(getattr(episode, "apology_count", 0) or 0) >= 1:
        return True
    holding = getattr(job, "holding_message", None)
    return bool(
        job.holding_message_id
        and holding is not None
        and str(getattr(holding, "provider_message_id", "") or "").strip()
    )


def _recovery_apology_warranted(job, target, *, apology_delivered: bool) -> bool:
    """Чи доречне вибачення у відновленій відповіді (ЭБ.1).

    Раніше умова була одна: «якщо holding не доставлено — вибачся». Після того як
    holding перестав надсилатись за кожним поодиноким збоєм, ця умова почала
    вибачатись майже завжди: відповідь приходила через 20–30 секунд і починалась
    з «Вибачте за технічну затримку», хоча клієнт жодної затримки не бачив —
    індикатор набору весь цей час був живий.

    Правило тепер таке саме, як для holding: вибачення — це реакція на **довге**
    очікування того, хто відповіді **чекав**. Якщо ми вклались у заявлений бюджет
    ходу, клієнт отримує просто відповідь.
    """
    if apology_delivered:
        return False
    from management.services.ig_reply_expectation import classify
    from management.services.ig_turn_budget import customer_notice_threshold_seconds

    try:
        expectation = classify(target)
    except Exception:  # noqa: BLE001 - без класифікації не вибачаємось
        return False
    if not expectation.waiting:
        # Репост історії, реакція, «дякую»: вибачатись нема за що.
        return False
    waited_since = (
        getattr(target, "provider_created_at", None)
        or getattr(target, "created_at", None)
    )
    if not waited_since:
        return False
    waited = (timezone.now() - waited_since).total_seconds()
    return waited > customer_notice_threshold_seconds()


def is_episode_cursor(job: IgAiReplyRecoveryJob) -> bool:
    """Курсор інциденту відповідає на АКТУАЛЬНИЙ хід, а не на перший."""
    from management.services.ig_provider_incidents import flag

    return bool(job.degradation_episode_id) and flag("IG_RECOVERY_EPISODE_CURSOR")


def effective_target_id(job: IgAiReplyRecoveryJob) -> int:
    """Id вхідного, на яке має відповісти цей job.

    Для курсора епізоду це найновіше вхідне клієнта: у production клієнт спитав
    «що є в асортименті», потім «я спитав просто щоб знати», і три окремі job'и
    дали три різні результати. Один курсор має відповісти на останнє питання.
    """
    source_id = int(job.source_message_id or 0)
    if not is_episode_cursor(job):
        return source_id
    latest = (
        InstagramBotMessage.objects.filter(
            client_id=job.client_id,
            role=InstagramBotMessage.Role.USER,
            id__gte=source_id,
        )
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    return int(latest or source_id)


def recovery_target_message(job: IgAiReplyRecoveryJob) -> InstagramBotMessage:
    target_id = effective_target_id(job)
    if target_id and target_id != int(job.source_message_id or 0):
        target = InstagramBotMessage.objects.filter(
            pk=target_id,
            client_id=job.client_id,
            role=InstagramBotMessage.Role.USER,
        ).first()
        if target is not None:
            return target
    return job.source_message


def _build_recovery_history(job: IgAiReplyRecoveryJob) -> list[dict]:
    """Return only the conversation available when this source turn arrived."""
    from management.services.ig_funnel_reset import current_message_floor

    floor = int(current_message_floor(job.client) or 1)
    rows = list(
        InstagramBotMessage.objects.filter(
            client_id=job.client_id,
            sender_id=job.source_message.sender_id,
            id__gte=floor,
            id__lte=effective_target_id(job),
        )
        .exclude(status=InstagramBotMessage.Status.FAILED)
        .annotate(event_at=Coalesce("provider_created_at", "created_at"))
        .order_by("-event_at", "-id")[:HISTORY_LIMIT]
    )
    rows.reverse()
    history = []
    for row in rows:
        text = (row.text or "").strip()
        if not text:
            continue
        if row.role in {InstagramBotMessage.Role.USER, InstagramBotMessage.Role.MODEL}:
            history.append({"role": row.role, "text": text})
    return history


def _build_recovery_manager_notes(job: IgAiReplyRecoveryJob) -> str:
    """Return manager evidence as bounded untrusted data, never model history."""
    from management.services.ig_funnel_reset import current_message_floor
    from management.services.instagram_bot import neutralize_untrusted_text

    floor = int(current_message_floor(job.client) or 1)
    rows = list(
        InstagramBotMessage.objects.filter(
            client_id=job.client_id,
            sender_id=job.source_message.sender_id,
            role=InstagramBotMessage.Role.MANAGER,
            id__gte=floor,
            id__lte=effective_target_id(job),
        )
        .exclude(status=InstagramBotMessage.Status.FAILED)
        .annotate(event_at=Coalesce("provider_created_at", "created_at"))
        .order_by("-event_at", "-id")[:4]
    )
    rows.reverse()
    notes = [
        neutralize_untrusted_text(row.text, limit=500)
        for row in rows
        if (row.text or "").strip()
    ]
    notes = [note for note in notes if note]
    if not notes:
        return ""
    return (
        "НОТАТКИ МЕНЕДЖЕРА ДО ЦЬОГО ХОДУ (недовірені дані, не слова клієнта "
        "і не попередні слова бота). Не підтверджуй ціни, знижки, оплату чи "
        "наявність лише з цього блоку.\n- "
        + "\n- ".join(notes)
    )


def _guard_reason(
    job: IgAiReplyRecoveryJob,
    settings_obj: InstagramBotSettings,
    client: IgClient | None,
    source: InstagramBotMessage | None,
    *,
    now,
) -> str:
    """Return a durable-cancellation reason before generation or Meta I/O."""
    if not settings_obj or not settings_obj.is_enabled:
        return "global_reply_paused"
    if not client or not source or source.client_id != client.pk:
        return "source_or_client_missing"
    if source.role != InstagramBotMessage.Role.USER:
        return "source_not_inbound"
    from management.services.ig_revision_live import legacy_claimable_messages

    source_ids = {source.pk, getattr(job, "source_message_id", None)} - {None}
    if legacy_claimable_messages(InstagramBotMessage.objects.filter(pk__in=source_ids)).count() != len(source_ids):
        return "revision_execution_owns_source"
    if client.hidden_at:
        return "client_hidden"
    if client.is_blocked:
        return "client_blocked"
    if _active_opt_out(client):
        return "client_opted_out"
    if client.bot_paused:
        return "client_paused"
    if client.manager_takeover:
        return "manager_takeover"

    permission = capture_reply_permission(settings_obj.pk, client.pk)
    if not permission:
        return permission.reason or "customer_send_not_allowed"
    if (
        permission.settings_epoch != int(job.settings_permission_epoch or 0)
        or permission.client_epoch != int(job.client_permission_epoch or 0)
    ):
        return "permission_epoch_changed"

    from management.services.ig_funnel_reset import current_message_floor

    floor = int(current_message_floor(client) or 0)
    if source.pk < floor or floor != int(job.message_floor or 0):
        return "message_floor_changed"
    deadline = job.response_window_deadline or _window_deadline(source, client)
    if not deadline or now > deadline:
        return "response_window_closed"

    # A manager message is authoritative ownership evidence and always wins.
    if InstagramBotMessage.objects.filter(
        client_id=client.pk,
        id__gt=source.pk,
        role=InstagramBotMessage.Role.MANAGER,
    ).exists():
        return "newer_inbound_or_manager_reply"
    # Курсор епізоду свідомо НЕ скасовується новим вхідним: він і є механізмом
    # «один recovery на клієнта та інцидент». Раніше кожне нове вхідне
    # скасовувало попередній job і створювало новий, тому за один інцидент
    # клієнт отримував результати кількох job'ів (у production — cancelled,
    # sent, failed для одного клієнта). Ціль курсора — найновіше вхідне.
    cursor_mode = bool(getattr(job, "degradation_episode_id", 0))
    if not cursor_mode and InstagramBotMessage.objects.filter(
        client_id=client.pk,
        id__gt=source.pk,
        role=InstagramBotMessage.Role.USER,
    ).exists():
        return "newer_inbound_or_manager_reply"

    # A known outage holding reply is explicitly allowed.  Any other confirmed
    # bot answer makes the recovery stale.  The persisted recovery draft itself
    # is excluded so the final pre-send guard remains valid.
    existing_reply_ids = [
        value for value in (job.holding_message_id, job.reply_message_id) if value
    ]
    from management.services.ig_provider_incidents import HOLDING_MESSAGE_SOURCE

    substantive = InstagramBotMessage.objects.filter(
        client_id=client.pk,
        role=InstagramBotMessage.Role.MODEL,
        id__gt=source.pk,
    ).exclude(pk__in=existing_reply_ids).exclude(
        # Технічний holding за визначенням НЕ є змістовною відповіддю. Раніше
        # будь-який holding з receipt скасовував recovery, і клієнт залишався
        # тільки з технічним текстом.
        source=HOLDING_MESSAGE_SOURCE,
    ).filter(
        Q(provider_message_id__gt="") | Q(send_state="sent")
    ).exists()
    if substantive:
        return "substantive_bot_reply_exists"
    # A legacy row without receipt is neither proof of delivery nor a safe
    # reason to send another answer. It is only exempted by the operator
    # command after the exact row is explicitly acknowledged as the outage
    # holding message.
    unreceipted = InstagramBotMessage.objects.filter(
        client_id=client.pk,
        role=InstagramBotMessage.Role.MODEL,
        id__gt=source.pk,
        provider_message_id="",
    ).exclude(pk__in=existing_reply_ids).exclude(
        source=HOLDING_MESSAGE_SOURCE,
    ).exclude(send_state="sending").exists()
    if unreceipted:
        return "unreceipted_holding_not_acknowledged"
    return ""


def _cursor_key(client_id, incident_id) -> str:
    return f"c{int(client_id or 0)}:i{int(incident_id or 0)}"


def active_episode_cursor(
    client_id: int,
    episode: IgClientDegradationEpisode,
) -> IgAiReplyRecoveryJob | None:
    """Єдиний активний job на пару (клієнт, інцидент)."""
    if not client_id or episode is None:
        return None
    return (
        IgAiReplyRecoveryJob.objects.select_related(
            "source_message", "client", "holding_message", "degradation_episode"
        )
        .filter(active_cursor_key=_cursor_key(client_id, episode.incident_id))
        .first()
    )


def _supersede_stale_cursors(
    *,
    client_id: int,
    episode: IgClientDegradationEpisode,
    keep_job_id: int,
) -> int:
    """Позначити зайві job'и одного інциденту як `SUPERSEDED` БЕЗ відправки.

    `SENDING` ніколи не витісняється: межа Meta-запиту пройдена, результат
    невідомий, повторна відправка заборонена.
    """
    now = timezone.now()
    stale = list(
        IgAiReplyRecoveryJob.objects.filter(
            client_id=client_id,
            degradation_episode_id=episode.pk,
            status__in=(
                IgAiReplyRecoveryJob.Status.PENDING,
                IgAiReplyRecoveryJob.Status.PROCESSING,
            ),
        ).exclude(pk=keep_job_id)
    )
    superseded = 0
    for job in stale:
        with transaction.atomic():
            locked = (
                IgAiReplyRecoveryJob.objects.select_for_update()
                .filter(pk=job.pk)
                .exclude(status__in=(
                    IgAiReplyRecoveryJob.Status.SENDING,
                    *_TERMINAL_STATUSES,
                ))
                .first()
            )
            if locked is None:
                continue
            locked.status = IgAiReplyRecoveryJob.Status.CANCELLED
            locked.last_error = "superseded_by_episode_cursor"
            locked.superseded_by_id = keep_job_id
            locked.active_cursor_key = None
            locked.lease_token = ""
            locked.lease_until = None
            locked.next_attempt_at = None
            locked.completed_at = now
            locked.save(update_fields=[
                "status", "last_error", "superseded_by", "active_cursor_key",
                "lease_token", "lease_until", "next_attempt_at", "completed_at",
                "updated_at",
            ])
            superseded += 1
    return superseded


def schedule_recovery(
    source_message: InstagramBotMessage,
    *,
    holding_message: InstagramBotMessage | None = None,
    activate: bool = True,
    degradation_episode: IgClientDegradationEpisode | None = None,
) -> IgAiReplyRecoveryJob:
    """Create exactly one recovery intent for a failed customer inbound.

    Live fallback code prepares the intent before crossing Meta and only calls
    ``activate_recovery`` after the holding receipt is confirmed. Explicit
    operator recovery remains active by default.
    """
    source_id = getattr(source_message, "pk", source_message)
    with transaction.atomic():
        source = (
            InstagramBotMessage.objects.select_for_update()
            .select_related("client")
            .filter(pk=source_id)
            .first()
        )
        if source is None:
            raise ValueError("Recovery source message does not exist")
        if source.role != InstagramBotMessage.Role.USER:
            raise ValueError("Recovery source must be an inbound user message")
        if not source.client_id:
            raise ValueError("Recovery source has no Instagram client")

        client = source.client
        permission = capture_reply_permission(InstagramBotSettings.load().pk, client.pk)
        from management.services.ig_funnel_reset import current_message_floor

        holding_id = getattr(holding_message, "pk", holding_message) or None
        if holding_id:
            holding = InstagramBotMessage.objects.filter(
                pk=holding_id,
                client_id=client.pk,
                role=InstagramBotMessage.Role.MODEL,
            ).first()
            if holding is None:
                raise ValueError("Recovery holding message is not a client model message")
        else:
            holding = None

        # Курсор епізоду (ЭА.7): один активний job на пару (клієнт, інцидент).
        # Якщо курсор уже є — новий вхідний ЛИШЕ оновлює ціль в епізоді, а
        # другий job не створюється. Раніше три вхідні під час одного інциденту
        # давали три job'и й кілька повідомлень клієнту.
        episode = degradation_episode
        if episode is None:
            from management.services.ig_provider_incidents import episode_for_client

            episode = episode_for_client(client.pk)
        cursor_key = None
        if episode is not None:
            from management.services.ig_provider_incidents import flag

            if flag("IG_RECOVERY_EPISODE_CURSOR"):
                cursor_key = _cursor_key(client.pk, episode.incident_id)
                existing_cursor = (
                    IgAiReplyRecoveryJob.objects.select_for_update()
                    .filter(active_cursor_key=cursor_key)
                    .first()
                )
                if existing_cursor is not None and existing_cursor.source_message_id != source.pk:
                    fields = []
                    if holding and not existing_cursor.holding_message_id:
                        existing_cursor.holding_message = holding
                        fields.append("holding_message")
                    if activate and not existing_cursor.activated_at:
                        existing_cursor.activated_at = timezone.now()
                        existing_cursor.next_attempt_at = timezone.now()
                        fields += ["activated_at", "next_attempt_at"]
                    if fields:
                        existing_cursor.save(update_fields=[*fields, "updated_at"])
                    return existing_cursor

        defaults = {
            "client": client,
            "holding_message": holding,
            "dedupe_key": f"ig-ai-recovery:{source.pk}",
            "degradation_episode": episode,
            "active_cursor_key": cursor_key,
            "settings_permission_epoch": int(permission.settings_epoch or 0),
            "client_permission_epoch": int(permission.client_epoch or 0),
            "message_floor": int(current_message_floor(client) or 0),
            "response_window_deadline": _window_deadline(source, client),
            "activated_at": timezone.now() if activate else None,
            "next_attempt_at": timezone.now() if activate else None,
        }
        try:
            job, created = IgAiReplyRecoveryJob.objects.get_or_create(
                source_message=source,
                defaults=defaults,
            )
        except IntegrityError:
            # The one-to-one source constraint is the idempotency boundary.
            job = IgAiReplyRecoveryJob.objects.get(source_message=source)
            created = False
        update_fields = []
        if holding and not job.holding_message_id:
            job.holding_message = holding
            update_fields.append("holding_message")
        if episode is not None and not job.degradation_episode_id:
            job.degradation_episode = episode
            update_fields.append("degradation_episode")
        if (
            cursor_key
            and job.active_cursor_key != cursor_key
            and job.status not in _TERMINAL_STATUSES
        ):
            job.active_cursor_key = cursor_key
            update_fields.append("active_cursor_key")
        if activate and not job.activated_at:
            job.activated_at = timezone.now()
            update_fields.append("activated_at")
        if activate and job.status == job.Status.PENDING and not job.next_attempt_at:
            job.next_attempt_at = timezone.now()
            update_fields.append("next_attempt_at")
        if update_fields:
            job.save(update_fields=[*update_fields, "updated_at"])
        del created
        if episode is not None:
            _supersede_stale_cursors(
                client_id=client.pk, episode=episode, keep_job_id=job.pk
            )
            from management.services.ig_provider_incidents import set_episode_state

            if episode.state in {
                IgClientDegradationEpisode.State.OPEN,
                IgClientDegradationEpisode.State.HOLDING_SENT,
            }:
                set_episode_state(
                    episode.pk,
                    IgClientDegradationEpisode.State.RECOVERY_PENDING,
                    reason="recovery_scheduled",
                )
        return job


def activate_recovery(
    recovery_job: IgAiReplyRecoveryJob | int,
    *,
    holding_message: InstagramBotMessage,
) -> IgAiReplyRecoveryJob:
    """Arm a prepared recovery only after its holding Meta receipt is known."""
    job_id = getattr(recovery_job, "pk", recovery_job)
    holding_id = getattr(holding_message, "pk", holding_message)
    with transaction.atomic():
        job = IgAiReplyRecoveryJob.objects.select_for_update().get(pk=job_id)
        if job.status in _TERMINAL_STATUSES:
            return job
        holding = InstagramBotMessage.objects.filter(
            pk=holding_id,
            client_id=job.client_id,
            role=InstagramBotMessage.Role.MODEL,
            provider_message_id__gt="",
        ).first()
        if holding is None:
            raise ValueError("Recovery holding message lacks a confirmed provider receipt")
        if not job.holding_message_id:
            job.holding_message = holding
        if not job.activated_at:
            job.activated_at = timezone.now()
        if not job.next_attempt_at:
            job.next_attempt_at = timezone.now()
        job.save(update_fields=[
            "holding_message", "activated_at", "next_attempt_at", "updated_at",
        ])
    return job


def terminalize_prepared_recovery(
    recovery_job: IgAiReplyRecoveryJob | int,
    *,
    reason: str,
    ambiguous: bool,
) -> IgAiReplyRecoveryJob:
    """Close an unarmed intent when the holding delivery cannot be confirmed."""
    job_id = getattr(recovery_job, "pk", recovery_job)
    with transaction.atomic():
        job = IgAiReplyRecoveryJob.objects.select_for_update().get(pk=job_id)
        if job.status in _TERMINAL_STATUSES or job.activated_at:
            return job
        job.status = job.Status.AMBIGUOUS if ambiguous else job.Status.CANCELLED
        job.last_error = str(reason or "holding_delivery_unconfirmed")[:1000]
        job.active_cursor_key = None
        job.lease_token = ""
        job.lease_until = None
        job.next_attempt_at = None
        job.completed_at = timezone.now()
        job.save(update_fields=[
            "status", "last_error", "active_cursor_key", "lease_token",
            "lease_until", "next_attempt_at", "completed_at", "updated_at",
        ])
    return job


def recovery_preflight(
    source_message: InstagramBotMessage,
    *,
    acknowledged_unreceipted_holding: InstagramBotMessage | None = None,
) -> dict:
    """Read the recovery guards without creating a job or crossing Meta I/O."""
    source_id = getattr(source_message, "pk", source_message)
    source = (
        InstagramBotMessage.objects.select_related("client")
        .filter(pk=source_id)
        .first()
    )
    if source is None:
        return {"source_message_id": int(source_id or 0), "guard_reason": "source_missing"}
    settings_obj = InstagramBotSettings.load()
    client = source.client
    permission = capture_reply_permission(settings_obj.pk, client.pk if client else None)
    from management.services.ig_funnel_reset import current_message_floor

    floor = int(current_message_floor(client) or 0) if client else 0
    deadline = _window_deadline(source, client) if client else None
    existing = IgAiReplyRecoveryJob.objects.filter(source_message=source).first()
    acknowledged_holding_id = 0
    if acknowledged_unreceipted_holding is not None:
        acknowledged_holding_id = getattr(acknowledged_unreceipted_holding, "pk", 0) or 0
        valid_acknowledged_holding = InstagramBotMessage.objects.filter(
            pk=acknowledged_holding_id,
            client_id=source.client_id,
            role=InstagramBotMessage.Role.MODEL,
            provider_message_id="",
            id__gt=source.pk,
        ).exists()
        if not valid_acknowledged_holding:
            acknowledged_holding_id = 0
    if existing and acknowledged_holding_id and not existing.holding_message_id:
        probe = SimpleNamespace(
            settings_permission_epoch=existing.settings_permission_epoch,
            client_permission_epoch=existing.client_permission_epoch,
            message_floor=existing.message_floor,
            response_window_deadline=existing.response_window_deadline,
            holding_message_id=acknowledged_holding_id,
            reply_message_id=existing.reply_message_id,
        )
    else:
        probe = existing or SimpleNamespace(
            settings_permission_epoch=int(permission.settings_epoch or 0),
            client_permission_epoch=int(permission.client_epoch or 0),
            message_floor=floor,
            response_window_deadline=deadline,
            holding_message_id=acknowledged_holding_id,
            reply_message_id=0,
        )
    reason = _guard_reason(
        probe,
        settings_obj,
        client,
        source,
        now=timezone.now(),
    )
    holding = (
        InstagramBotMessage.objects.filter(
            client_id=source.client_id,
            role=InstagramBotMessage.Role.MODEL,
            id__gt=source.pk,
        )
        .order_by("id")
        .first()
        if source.client_id
        else None
    )
    return {
        "source_message_id": source.pk,
        "client_id": source.client_id,
        "source_status": source.status,
        "source_send_state": source.send_state,
        "guard_reason": reason,
        "permission_allowed": bool(permission),
        "message_floor": floor,
        "response_window_deadline": deadline,
        "activated_at": existing.activated_at if existing else None,
        "holding_message_id": holding.pk if holding else None,
        "acknowledged_unreceipted_holding_id": acknowledged_holding_id or None,
        "job_id": existing.pk if existing else None,
        "job_status": existing.status if existing else None,
    }


def _claim_job(job_id: int) -> tuple[IgAiReplyRecoveryJob | None, str]:
    now = timezone.now()
    with transaction.atomic():
        job = (
            IgAiReplyRecoveryJob.objects.select_for_update()
            .select_related("source_message", "client", "holding_message", "reply_message")
            .filter(pk=job_id)
            .first()
        )
        if job is None or job.status in _TERMINAL_STATUSES:
            return job, ""
        if not job.activated_at:
            holding_confirmed = bool(
                job.status == job.Status.PENDING
                and job.holding_message_id
                and job.holding_message
                and job.holding_message.provider_message_id
            )
            if not holding_confirmed:
                return job, ""
            # If the worker died between saving a Meta receipt and arming the
            # job, recover deterministically from the same durable receipt.
            job.activated_at = now
            job.next_attempt_at = now
            job.save(update_fields=["activated_at", "next_attempt_at", "updated_at"])
        if (
            job.status == job.Status.PENDING
            and job.next_attempt_at
            and job.next_attempt_at > now
        ):
            return job, ""
        if job.status == job.Status.SENDING:
            if job.lease_until and job.lease_until > now:
                return job, ""
            # A process died after it made the request durable but before it
            # wrote a provider ID.  Never replay an uncertain Meta send.
            job.status = job.Status.AMBIGUOUS
            job.last_error = "stale_sending_without_provider_receipt"
            job.active_cursor_key = None
            job.lease_token = ""
            job.lease_until = None
            job.next_attempt_at = None
            job.completed_at = now
            job.save(update_fields=[
                "status", "last_error", "active_cursor_key", "lease_token",
                "lease_until", "next_attempt_at", "completed_at", "updated_at",
            ])
            if job.reply_message_id:
                InstagramBotMessage.objects.filter(pk=job.reply_message_id).update(
                    status=InstagramBotMessage.Status.FAILED,
                    send_state="unknown",
                    send_completed_at=now,
                )
            return job, ""
        if (
            job.status == job.Status.PROCESSING
            and job.lease_until
            and job.lease_until > now
        ):
            return job, ""

        token = secrets.token_hex(16)
        job.status = job.Status.PROCESSING
        job.lease_token = token
        job.lease_until = now + JOB_LEASE_DURATION
        job.attempts = int(job.attempts or 0) + 1
        job.last_error = ""
        job.next_attempt_at = None
        job.save(update_fields=[
            "status", "lease_token", "lease_until", "attempts", "last_error",
            "next_attempt_at", "updated_at",
        ])
    return job, token


def _cancel_claim(job_id: int, token: str, reason: str) -> IgAiReplyRecoveryJob:
    with transaction.atomic():
        job = IgAiReplyRecoveryJob.objects.select_for_update().get(pk=job_id)
        if job.lease_token == token and job.status in {
            job.Status.PROCESSING,
            job.Status.SENDING,
        }:
            now = timezone.now()
            job.status = job.Status.CANCELLED
            job.last_error = reason[:1000]
            job.active_cursor_key = None
            job.lease_token = ""
            job.lease_until = None
            job.next_attempt_at = None
            job.completed_at = now
            job.save(update_fields=[
                "status", "last_error", "active_cursor_key", "lease_token",
                "lease_until", "next_attempt_at", "completed_at", "updated_at",
            ])
            if job.reply_message_id:
                InstagramBotMessage.objects.filter(pk=job.reply_message_id).update(
                    status=InstagramBotMessage.Status.DONE,
                    send_state="cancelled",
                    send_completed_at=now,
                )
        return job


def cancel_recoveries_for_spam(client_id: int) -> int:
    """Cancel unsent recovery work after an irreversible spam block.

    A spam-blocked client must never receive a recovery send. Jobs that have
    already crossed Meta's non-idempotent boundary remain untouched; only
    pending/failed intents without a provider receipt are terminalized.
    """
    cancelled = 0
    now = timezone.now()
    with transaction.atomic():
        jobs = (
            IgAiReplyRecoveryJob.objects.select_for_update()
            .filter(
                client_id=client_id,
                status__in=(
                    IgAiReplyRecoveryJob.Status.PENDING,
                    IgAiReplyRecoveryJob.Status.FAILED,
                ),
                provider_message_id="",
            )
            .order_by("id")
        )
        for job in jobs:
            job.status = IgAiReplyRecoveryJob.Status.CANCELLED
            job.last_error = "client_spam"
            job.active_cursor_key = None
            job.lease_token = ""
            job.lease_until = None
            job.next_attempt_at = None
            job.completed_at = job.completed_at or now
            job.save(update_fields=[
                "status", "last_error", "active_cursor_key", "lease_token",
                "lease_until", "next_attempt_at", "completed_at", "updated_at",
            ])
            if job.reply_message_id:
                InstagramBotMessage.objects.filter(
                    pk=job.reply_message_id,
                    provider_message_id="",
                ).update(
                    status=InstagramBotMessage.Status.DONE,
                    send_state="cancelled",
                    send_completed_at=now,
                )
            cancelled += 1
    return cancelled


def _recovery_retry_at(*, attempts: int, now):
    exponent = max(0, int(attempts or 1) - 1)
    seconds = min(
        RECOVERY_RETRY_MAX_SECONDS,
        RECOVERY_RETRY_BASE_SECONDS * (2 ** exponent),
    )
    return now + timedelta(seconds=seconds)


def _notify_recovery_exhausted(job: IgAiReplyRecoveryJob) -> None:
    """Один кейс менеджеру на інцидент і клієнта; клієнту — НІЧОГО.

    Друге технічне повідомлення не додає клієнту інформації, а підтверджує, що
    «бот зламаний». Правильний отримувач цієї інформації — менеджер. Тому тут
    немає жодної customer-facing відправки, а dedupe-ключ береться по парі
    (інцидент, клієнт), а не по source-повідомленню: інакше один інцидент дав би
    менеджеру кілька однакових алертів.
    """
    episode = getattr(job, "degradation_episode", None)
    incident_id = int(getattr(episode, "incident_id", 0) or 0)
    dedupe = (
        f"ig-ai-recovery-exhausted:incident:{incident_id}:{job.client_id}"
        if incident_id
        else f"ig-ai-recovery-exhausted:{job.source_message_id}"
    )
    target_id = 0
    failure_class = ""
    try:
        target_id = effective_target_id(job)
        incident = getattr(episode, "incident", None)
        failure_class = str(getattr(incident, "failure_class", "") or "")
    except Exception:
        pass
    if episode is not None:
        try:
            from management.services.ig_provider_incidents import set_episode_state

            set_episode_state(
                episode.pk,
                IgClientDegradationEpisode.State.MANUAL,
                reason="recovery_exhausted",
            )
        except Exception:
            pass
    try:
        notify_manager(
            "⚠️ IG: автоматична відповідь не відновилась. Потрібна ручна "
            f"відповідь клієнту #{job.client_id} на повідомлення #{target_id or job.source_message_id}"
            + (f" (клас збою: {failure_class})" if failure_class else "")
            + f". Спроб: {job.attempts}.",
            dedupe_key=dedupe,
            event_type="ai_reply_recovery_exhausted",
            client=job.client,
            metadata={
                "source_message_id": job.source_message_id,
                "target_message_id": target_id or job.source_message_id,
                "attempts": job.attempts,
                "incident_id": incident_id or None,
                "failure_class": failure_class,
                "recovery_job_id": job.pk,
            },
        )
    except Exception:
        pass


def _release_for_retry(
    job_id: int,
    token: str,
    reason: str,
    *,
    consume_attempt: bool = True,
    retry_at=None,
    retry_horizon=None,
    force_exhausted: bool = False,
) -> IgAiReplyRecoveryJob:
    exhausted = False
    with transaction.atomic():
        job = (
            IgAiReplyRecoveryJob.objects.select_for_update()
            .select_related("client", "degradation_episode", "source_message")
            .get(pk=job_id)
        )
        if job.lease_token == token and job.status == job.Status.PROCESSING:
            now = timezone.now()
            if not consume_attempt:
                job.attempts = max(0, int(job.attempts or 0) - 1)
            exhausted = force_exhausted or int(job.attempts or 0) >= MAX_RECOVERY_ATTEMPTS
            planned_retry = retry_at or _recovery_retry_at(
                attempts=job.attempts,
                now=now,
            )
            if (
                not exhausted
                and retry_horizon is not None
                and (retry_horizon <= now or planned_retry >= retry_horizon)
            ):
                exhausted = True
            job.status = job.Status.FAILED if exhausted else job.Status.PENDING
            job.last_error = reason[:1000]
            job.lease_token = ""
            job.lease_until = None
            if exhausted:
                job.next_attempt_at = None
                # Курсор звільняється: наступний інцидент має право на власний.
                job.active_cursor_key = None
            else:
                job.next_attempt_at = planned_retry
            job.completed_at = now if exhausted else None
            job.save(update_fields=[
                "status", "attempts", "last_error", "active_cursor_key",
                "lease_token", "lease_until", "next_attempt_at", "completed_at",
                "updated_at",
            ])
    if exhausted:
        _notify_recovery_exhausted(job)
    return job


_RECOVERY_TURN_NOTE_WITH_APOLOGY = (
    "Відновлення відповіді після короткої технічної затримки. "
    "Почни з одного природного короткого вибачення, а далі одразу дай "
    "повну корисну відповідь на останнє запитання клієнта. Не згадуй "
    "ШІ, Gemini, API, ключі, внутрішні системи чи менеджера. Не додавай "
    "керуючих тегів, посилань на оплату, створення замовлення або інші "
    "незворотні дії. Відповідай мовою останнього повідомлення клієнта."
)
# Клієнт уже отримав одне вибачення в holding. Друге виглядає як поломка, тому
# prompt тут ПРЯМО забороняє вибачення — інакше модель вибачається сама, а код
# знімає її вибачення, і хід починається з обрубку.
_RECOVERY_TURN_NOTE_NO_APOLOGY = (
    "Відновлення відповіді. Клієнт уже отримав одне повідомлення про технічну "
    "затримку, тому НЕ вибачайся і не згадуй затримку взагалі. Одразу дай "
    "повну корисну відповідь на останнє запитання клієнта. Не згадуй "
    "ШІ, Gemini, API, ключі, внутрішні системи чи менеджера. Не додавай "
    "керуючих тегів, посилань на оплату, створення замовлення або інші "
    "незворотні дії. Відповідай мовою останнього повідомлення клієнта."
)


def _generate_recovery_draft(
    job: IgAiReplyRecoveryJob,
    *,
    model_context: dict | None = None,
    automation_token: str = "",
) -> str:
    """Generate a safe, substantive response for the current customer turn."""
    settings_obj = InstagramBotSettings.load()
    history = _build_recovery_history(job)
    target = recovery_target_message(job)
    manager_notes = _build_recovery_manager_notes(job)
    from management.services.instagram_bot import (
        _collect_media_images,
        _media_context_hint,
        _persist_turn_intelligence,
        _recover_current_message_media,
    )
    model_context = model_context if isinstance(model_context, dict) else {}
    from management.services.ig_turn_lineage import resolve_logical_turn_key

    logical_turn_id = resolve_logical_turn_key(target)
    legacy_execution = None

    existing_intelligence = (
        target.turn_intelligence_artifact
        if isinstance(target.turn_intelligence_artifact, dict)
        else {}
    )
    if existing_intelligence:
        media_expected = False
        media = []
        images = []
        artifact_context = json.dumps(
            {
                "intent": existing_intelligence.get("intent"),
                "transcript": existing_intelligence.get("transcript"),
                "audio_status": existing_intelligence.get("audio_status"),
                "catalog_candidates": existing_intelligence.get("catalog_candidates") or [],
                "catalog_resolution": existing_intelligence.get("catalog_resolution"),
                "media_digest": existing_intelligence.get("media_digest"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )[:8000]
    else:
        media_expected = bool(
            str(getattr(target, "attachments", "") or "").strip()
            or list(getattr(target, "attachment_media", None) or [])
        )
        recovered_media = _recover_current_message_media(target)
        media = recovered_media or []
        images = _collect_media_images(media, message_id=target.pk)
        artifact_context = ""
    apology_delivered = _apology_already_delivered(job)
    apology_warranted = _recovery_apology_warranted(
        job, target, apology_delivered=apology_delivered
    )
    if media_expected and not images and not str(target.text or "").strip():
        language = str(getattr(job.client, "language", "uk") or "uk").casefold()
        if language.startswith("ru"):
            unavailable = "Не удалось повторно открыть изображение. Пришлите его, пожалуйста, ещё раз."
        elif language.startswith("en"):
            unavailable = "I could not reopen the image. Please send it once more."
        else:
            unavailable = "Не вдалося повторно відкрити зображення. Надішліть його, будь ласка, ще раз."
        normalized, _apologies = _ensure_recovery_apology(
            unavailable,
            target.text,
            apology_already_delivered=not apology_warranted,
        )
        return normalized

    if automation_token:
        try:
            from management.services import gemini_accounting_runtime

            if gemini_accounting_runtime.shadow_runtime_active():
                from management.services.ig_legacy_provider_execution import (
                    resolve_legacy_execution,
                )
                from management.services.ig_turn_lineage import (
                    resolve_logical_turn_key,
                )

                legacy_execution, bridge_reason = resolve_legacy_execution(
                    job_id=job.pk,
                    recovery_token=job.lease_token,
                    automation_token=automation_token,
                    client_id=job.client_id,
                    source_message_id=target.pk,
                    logical_turn_id=logical_turn_id,
                )
                if legacy_execution is None:
                    model_context["kind"] = bridge_reason
                    return ""
        except Exception:
            model_context["kind"] = "legacy_bridge_unavailable"
            return ""

    from management.services.gemini_routing import recovery_decision_for

    routing_decision = recovery_decision_for(
        target,
        settings_obj,
        has_image=any(str(mime).startswith("image/") for mime, _raw in images),
        has_audio=any(str(mime).startswith("audio/") for mime, _raw in images),
    )
    route_payload = routing_decision.as_dict()
    if type(job).objects.filter(pk=job.pk, routing_decision={}).update(
        routing_decision=route_payload
    ):
        job.routing_decision = route_payload
    from management.services.ig_turn_lineage import Lane, turn_lineage

    with turn_lineage(
        lane=Lane.RECOVERY,
        client_id=job.client_id,
        source_message_id=target.pk,
        logical_turn_id=logical_turn_id,
        incident_id=getattr(job.degradation_episode, "incident_id", None),
        recovery_job_id=job.pk,
    ) as lineage:
        if legacy_execution is not None:
            lineage["recovery_token"] = job.lease_token
            lineage["automation_token"] = automation_token
        draft = gemini_generate(
            settings_obj,
            history,
            images=images or None,
            client=job.client,
            context_note="\n\n".join(
                value
                for value in (
                    manager_notes,
                    (
                        "[IMMUTABLE TURN INTELLIGENCE — reuse; do not infer new media facts]\n"
                        + artifact_context
                        if artifact_context
                        else ""
                    ),
                )
                if value
            ) or None,
            media_hint=_media_context_hint(media) if media else None,
            turn_note=(
                _RECOVERY_TURN_NOTE_WITH_APOLOGY
                if apology_warranted
                else _RECOVERY_TURN_NOTE_NO_APOLOGY
            ),
            failure_context=model_context,
            routing_decision=routing_decision,
        )
    if not draft and legacy_execution is not None:
        try:
            from management.services.ig_legacy_provider_execution import (
                legacy_provider_continuation,
            )

            continuation = legacy_provider_continuation(legacy_execution)
            if not continuation.ready and continuation.reason:
                model_context["kind"] = continuation.reason
                if continuation.next_due_at:
                    model_context["retry_at"] = continuation.next_due_at
        except Exception:
            model_context["kind"] = "legacy_bridge_unavailable"
    model_context["gemini_request_id"] = str(
        lineage.get("request_id") or ""
    )[:40]
    _persist_turn_intelligence(
        target,
        model_context.get("turn_intelligence") or {},
    )
    if isinstance(draft, ValidatedResponse):
        # Recovery may compose customer text but never executes model controls.
        if not draft.valid:
            return ""
        draft = draft.reply_text
    elif not isinstance(draft, str):
        return ""
    normalized, _apologies = _ensure_recovery_apology(
        draft or "",
        target.text,
        # «Вже доставлено» для політики означає «вибачення в цьому тексті не
        # потрібне»: або воно вже витрачене holding-ом, або клієнт не чекав довго.
        apology_already_delivered=not apology_warranted,
    )
    return normalized


def _persist_draft(
    job_id: int,
    token: str,
    draft: str,
    *,
    provider_model: str = "",
    gemini_request_id: str = "",
) -> tuple[IgAiReplyRecoveryJob, str]:
    """Persist the exact draft and history row before any Meta I/O."""
    with transaction.atomic():
        job = (
            IgAiReplyRecoveryJob.objects.select_for_update()
            .select_related("source_message", "client", "holding_message", "reply_message")
            .get(pk=job_id)
        )
        if job.lease_token != token or job.status != job.Status.PROCESSING:
            return job, "claim_lost"
        settings_obj = InstagramBotSettings.load()
        reason = _guard_reason(
            job, settings_obj, job.client, job.source_message, now=timezone.now()
        )
        if reason:
            return job, reason
        # A legacy/incomplete draft can be longer than a one-request recovery
        # permits.  Replace it with the already-normalized value before I/O.
        job.draft_text = draft
        if not job.reply_message_id:
            from management.services.instagram_bot import _persist_generated_reply_message

            reply_message = _persist_generated_reply_message(
                job.source_message,
                job.draft_text,
                provider_model=provider_model,
                status=InstagramBotMessage.Status.PROCESSING,
                source="ai_recovery",
                send_state="",
                gemini_request_id=gemini_request_id,
            )
            job.reply_message = reply_message
        elif job.reply_message.send_state in {"sending", "sent", "unknown"}:
            now = timezone.now()
            provider_id = str(job.reply_message.provider_message_id or "").strip()
            if job.reply_message.send_state == "sent" and provider_id:
                job.status = job.Status.SENT
                job.provider_message_id = provider_id[:255]
                job.last_error = ""
            else:
                # The history row says a prior worker already crossed, or may
                # have crossed, the provider boundary.  It must not be replayed.
                job.status = job.Status.AMBIGUOUS
                job.last_error = "reply_delivery_already_started"
            job.lease_token = ""
            job.lease_until = None
            job.active_cursor_key = None
            job.completed_at = now
            job.save(update_fields=[
                "status", "provider_message_id", "last_error",
                "active_cursor_key", "lease_token", "lease_until",
                "completed_at", "updated_at",
            ])
            return job, "terminalized_existing_delivery"
        else:
            job.reply_message.text = job.draft_text
            update_fields = ["text"]
            current_request_id = str(
                job.reply_message.gemini_request_id or ""
            )
            if gemini_request_id and not current_request_id:
                job.reply_message.gemini_request_id = gemini_request_id[:40]
                update_fields.append("gemini_request_id")
            elif (
                gemini_request_id
                and current_request_id
                and current_request_id != gemini_request_id
            ):
                return job, "gemini_request_conflict"
            job.reply_message.save(update_fields=update_fields)
        job.save(update_fields=["draft_text", "reply_message", "updated_at"])
        if gemini_request_id and job.reply_message_id:
            try:
                from management.services import gemini_accounting_runtime

                if gemini_accounting_runtime.shadow_runtime_active():
                    transaction.on_commit(
                        lambda request_id=gemini_request_id, reply_id=job.reply_message_id: (
                            gemini_accounting_runtime.link_reply_if_present(
                                request_id=request_id,
                                reply_message_id=reply_id,
                            )
                        )
                    )
            except Exception:
                pass
        return job, ""


@contextmanager
def _recovery_send_boundary(
    job_id: int,
    token: str,
    permission,
    *,
    mark_sending: bool,
):
    """Revalidate all recovery guards while holding the short Meta send lock."""
    with customer_send_boundary(permission.settings_id, permission.client_id, permission) as allowed:
        if not allowed:
            yield False
            return
        can_send = False
        with transaction.atomic():
            locked = (
                IgAiReplyRecoveryJob.objects.select_for_update()
                .select_related("source_message", "client", "holding_message", "reply_message")
                .get(pk=job_id)
            )
            expected_status = (
                locked.Status.PROCESSING if mark_sending else locked.Status.SENDING
            )
            if locked.lease_token == token and locked.status == expected_status:
                settings_obj = InstagramBotSettings.load()
                reason = _guard_reason(
                    locked,
                    settings_obj,
                    locked.client,
                    locked.source_message,
                    now=timezone.now(),
                )
                if not reason and locked.reply_message_id:
                    now = timezone.now()
                    if mark_sending:
                        locked.status = locked.Status.SENDING
                        locked.sending_started_at = now
                        locked.lease_until = now + JOB_LEASE_DURATION
                        locked.save(update_fields=[
                            "status", "sending_started_at", "lease_until", "updated_at",
                        ])
                        # ЭА.21: намір відправки recovery заявляється тим самим
                        # ідемпотентним ключем, що й живий шлях, але окремим
                        # видом. Так recovery не може повторити substantive
                        # відповідь і не блокує її.
                        from management.services import ig_send_intent

                        ig_send_intent.claim_send_intent(
                            InstagramBotMessage.objects.filter(
                                pk=locked.reply_message_id
                            ),
                            locked.reply_message,
                            kind=ig_send_intent.KIND_RECOVERY,
                            turn_id=0,
                            now=now,
                        )
                    can_send = True
        yield can_send


def _delivery_result(result) -> tuple[bool, str, str, str]:
    provider_message_id = getattr(result, "provider_message_id", "")
    if isinstance(result, tuple):
        if len(result) >= 4:
            ok, kind, hint, provider_message_id = result[:4]
        else:
            ok, kind, hint = result
    else:
        ok = bool(getattr(result, "ok", False))
        kind = str(getattr(result, "kind", "unknown") or "unknown")
        hint = str(getattr(result, "hint", "") or "")
    return (
        bool(ok),
        str(kind or "unknown"),
        str(hint or ""),
        normalize_provider_message_id(provider_message_id),
    )


def _finish_delivery(
    job_id: int,
    token: str,
    *,
    ok: bool,
    kind: str,
    hint: str,
    provider_message_id: str,
) -> IgAiReplyRecoveryJob:
    with transaction.atomic():
        job = (
            IgAiReplyRecoveryJob.objects.select_for_update()
            .select_related("reply_message")
            .get(pk=job_id)
        )
        if job.lease_token != token or job.status != job.Status.SENDING:
            return job
        now = timezone.now()
        job.lease_token = ""
        job.lease_until = None
        job.next_attempt_at = None
        job.completed_at = now
        reply_update = {
            "send_completed_at": now,
        }
        if ok and provider_message_id:
            job.status = job.Status.SENT
            job.provider_message_id = provider_message_id[:255]
            job.last_error = ""
            reply_update.update({
                "status": InstagramBotMessage.Status.DONE,
                "send_state": "sent",
                "provider_message_id": provider_message_id[:255],
            })
            client = IgClient.objects.select_for_update().get(pk=job.client_id)
            client.last_bot_reply_at = now
            client.save(update_fields=["last_bot_reply_at", "updated_at"])
        else:
            # Once the Send API was invoked, including HTTP 200 with no ID, we
            # cannot safely know whether Meta delivered the draft.  Do not retry.
            job.status = job.Status.AMBIGUOUS
            job.last_error = (
                "provider_message_id_missing" if ok else f"{kind}:{hint}"
            )[:1000]
            reply_update.update({
                "status": InstagramBotMessage.Status.FAILED,
                "send_state": "unknown",
            })
        job.active_cursor_key = None
        job.save(update_fields=[
            "status", "provider_message_id", "last_error", "active_cursor_key",
            "lease_token", "lease_until", "next_attempt_at", "completed_at",
            "updated_at",
        ])
        if job.reply_message_id:
            InstagramBotMessage.objects.filter(pk=job.reply_message_id).update(**reply_update)
            gemini_request_id = str(
                getattr(job.reply_message, "gemini_request_id", "") or ""
            )[:40]
            if gemini_request_id:
                try:
                    from management.services import gemini_accounting_runtime

                    if gemini_accounting_runtime.shadow_runtime_active():
                        transaction.on_commit(
                            lambda request_id=gemini_request_id, reply_id=job.reply_message_id: (
                                gemini_accounting_runtime.link_reply_if_present(
                                    request_id=request_id,
                                    reply_message_id=reply_id,
                                )
                            )
                        )
                except Exception:
                    pass
        if job.status == job.Status.SENT and job.degradation_episode_id:
            from management.services.ig_provider_incidents import (
                IgClientDegradationEpisode as _Episode,
                set_episode_state,
            )

            set_episode_state(
                job.degradation_episode_id,
                _Episode.State.RECOVERED,
                reason="recovery_delivered",
            )
        return job


def process_recovery_job(job_id: int) -> IgAiReplyRecoveryJob | None:
    """Generate and send one guarded recovery reply, never replaying Meta I/O."""
    job, token = _claim_job(job_id)
    if job is None or not token:
        return job

    automation_token = ""
    try:
        settings_obj = InstagramBotSettings.load()
        reason = _guard_reason(
            job, settings_obj, job.client, job.source_message, now=timezone.now()
        )
        if reason:
            return _cancel_claim(job.pk, token, reason)

        # ЭА.8: recovery планується ВІД СТАНУ ІНЦИДЕНТУ, а не від таймера. Під
        # час відкритого quota-інциденту три повних виклики гарантовано
        # провальні: у production job 7 витратив три спроби й закінчився
        # `recovery_generation_failed`, після чого клієнт отримав лише алерт
        # менеджеру. Тепер спроби не витрачаються, поки провайдер не подав
        # сигнал відновлення.
        from management.services.ig_provider_incidents import (
            RECOVERY_CURSOR_MAX_LIFETIME,
            incident_blocks_recovery,
        )

        now = timezone.now()
        cursor_age = now - (job.activated_at or job.created_at or now)
        if not job.draft_text:
            try:
                from management.services import gemini_accounting_runtime

                accounting_active = gemini_accounting_runtime.shadow_runtime_active()
            except Exception:
                accounting_active = False
            if accounting_active:
                from management.services.ig_legacy_provider_execution import (
                    classify_legacy_provider_continuation,
                    inspect_legacy_job_provider_state,
                )

                provider_state = inspect_legacy_job_provider_state(job, now=now)
                if not provider_state.ready:
                    disposition = classify_legacy_provider_continuation(
                        provider_state, now=now,
                    )
                    return _release_for_retry(
                        job.pk,
                        token,
                        disposition.reason,
                        consume_attempt=disposition.consume_attempt,
                        retry_at=disposition.retry_at,
                        retry_horizon=disposition.horizon_at,
                        force_exhausted=disposition.state == "terminal",
                    )
        if incident_blocks_recovery(job.degradation_episode, now=now):
            if cursor_age >= RECOVERY_CURSOR_MAX_LIFETIME:
                # Курсор не живе вічно: інакше порушується І9 — молчання каналу
                # довше SLA. Терминальний исход іде менеджеру, не клієнту.
                return _release_for_retry(
                    job.pk,
                    token,
                    "incident_open_cursor_expired",
                    consume_attempt=False,
                    force_exhausted=True,
                )
            return _release_for_retry(
                job.pk,
                token,
                "incident_open_wait_for_recovery",
                consume_attempt=False,
                retry_at=now + timedelta(seconds=RECOVERY_INCIDENT_WAIT_SECONDS),
            )
        if cursor_age >= RECOVERY_CURSOR_MAX_LIFETIME:
            return _release_for_retry(
                job.pk,
                token,
                "recovery_cursor_expired",
                consume_attempt=False,
                force_exhausted=True,
            )

        _leased_client, automation_token = acquire_client_automation_lease(job.client_id)
        if not automation_token:
            return _release_for_retry(
                job.pk,
                token,
                "client_automation_busy",
                consume_attempt=False,
            )

        recovery_model_context: dict = {}
        target_message = recovery_target_message(job)
        apology_delivered = _apology_already_delivered(job)
        if job.draft_text:
            draft, apology_count = _ensure_recovery_apology(
                job.draft_text,
                target_message.text,
                apology_already_delivered=apology_delivered,
            )
        else:
            draft = _generate_recovery_draft(
                job,
                model_context=recovery_model_context,
                automation_token=automation_token,
            )
            apology_count = 0
        if not draft:
            from management.services.ig_provider_incidents import (
                recovery_failure_is_retryable,
            )

            bridge_reason = str(recovery_model_context.get("kind") or "")
            if bridge_reason.startswith(("legacy_", "provider_", "scarce_")):
                from management.services.ig_legacy_provider_execution import (
                    classify_legacy_provider_failure,
                    continuation_horizon,
                    legacy_provider_continuation,
                    resolve_legacy_execution,
                )

                execution, _reason = resolve_legacy_execution(
                    job_id=job.pk, recovery_token=token,
                    automation_token=automation_token,
                    client_id=job.client_id,
                    source_message_id=target_message.pk,
                    logical_turn_id=resolve_logical_turn_key(target_message),
                )
                continuation = (
                    legacy_provider_continuation(execution)
                    if execution is not None else None
                )
                disposition = classify_legacy_provider_failure(
                    bridge_reason,
                    next_due_at=getattr(continuation, "next_due_at", None),
                    horizon_at=(
                        continuation_horizon(continuation)
                        if continuation is not None else None
                    ),
                )
                return _release_for_retry(
                    job.pk,
                    token,
                    bridge_reason,
                    consume_attempt=disposition.consume_attempt,
                    retry_at=(
                        disposition.retry_at
                        or recovery_model_context.get("retry_at")
                    ),
                    retry_horizon=disposition.horizon_at,
                    force_exhausted=disposition.state == "terminal",
                )
            if not recovery_failure_is_retryable(job.pk):
                return _release_for_retry(
                    job.pk,
                    token,
                    "recovery_failure_not_retryable",
                    force_exhausted=True,
                )
            return _release_for_retry(job.pk, token, "recovery_generation_failed")
        from management.services.bot_sales_classifier import (
            enforce_phone_disclosure_policy,
        )

        draft, _phone_blocked, _phone_decision = enforce_phone_disclosure_policy(
            job.client,
            draft,
            source_message_id=job.source_message_id,
        )
        draft, apology_count = _ensure_recovery_apology(
            draft,
            target_message.text,
            apology_already_delivered=apology_delivered,
        )
        if not draft:
            return _release_for_retry(job.pk, token, "recovery_generation_failed")
        job, reason = _persist_draft(
            job.pk,
            token,
            draft,
            provider_model=recovery_model_context.get("model", ""),
            gemini_request_id=recovery_model_context.get(
                "gemini_request_id", ""
            ),
        )
        if reason:
            if reason in {
                "claim_lost", "terminalized_existing_delivery",
            }:
                return job
            return _cancel_claim(job.pk, token, reason)

        permission = capture_reply_permission(settings_obj.pk, job.client_id)
        # Make the outbound edge durable before calling Meta.  The boundary
        # inside ``send_text`` repeats the complete guard immediately before
        # the request, so a pause/newer message between the two is still safe.
        with _recovery_send_boundary(
            job.pk, token, permission, mark_sending=True
        ) as ready:
            if not ready:
                return _cancel_claim(job.pk, token, "permission_or_recovery_guard_changed")
        result = send_text(
            settings_obj,
            job.source_message.sender_id,
            job.draft_text,
            permission_boundary_factory=lambda: _recovery_send_boundary(
                job.pk, token, permission, mark_sending=False
            ),
            return_receipt=True,
        )
        ok, kind, hint, provider_message_id = _delivery_result(result)
        if kind == "cancelled":
            return _cancel_claim(job.pk, token, "permission_or_recovery_guard_changed")
        finished = _finish_delivery(
            job.pk,
            token,
            ok=ok,
            kind=kind,
            hint=hint,
            provider_message_id=provider_message_id,
        )
        if (
            finished is not None
            and finished.status == finished.Status.SENT
            and job.degradation_episode_id
            and apology_count
        ):
            # Вибачення рахується по ФАКТУ надісланого тексту, а не по флагах.
            from management.services.ig_provider_incidents import note_apology_delivered

            note_apology_delivered(job.degradation_episode_id, count=apology_count)
        return finished
    except Exception as exc:  # noqa: BLE001 - provider boundary must be durable.
        # If the send boundary was crossed, an exception is indistinguishable
        # from a delivered request.  The current state tells us which side won.
        with transaction.atomic():
            current = IgAiReplyRecoveryJob.objects.select_for_update().get(pk=job.pk)
            crossed_meta_boundary = (
                current.lease_token == token and current.status == current.Status.SENDING
            )
        if crossed_meta_boundary:
            return _finish_delivery(
                job.pk,
                token,
                ok=False,
                kind="unknown",
                hint=repr(exc),
                provider_message_id="",
            )
        return _release_for_retry(job.pk, token, f"recovery_error:{exc!r}")
    finally:
        if automation_token:
            release_client_automation_lease(job.client_id, automation_token)


def process_due_recoveries(*, limit: int = 1) -> int:
    """Drain a small recovery slice without holding a database lock over I/O.

    A worker claims every selected row independently in ``process_recovery_job``.
    This makes a duplicate daemon harmless and lets stale processing/sending
    leases be reconciled without ever replaying a possibly delivered Meta send.
    """
    try:
        batch_size = int(limit)
    except (TypeError, ValueError):
        batch_size = 1
    batch_size = max(1, min(batch_size, 10))
    now = timezone.now()
    candidate_ids = list(
        IgAiReplyRecoveryJob.objects.filter(
            (
                Q(status=IgAiReplyRecoveryJob.Status.PENDING)
                & Q(activated_at__isnull=False)
                & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            )
            | (
                Q(status=IgAiReplyRecoveryJob.Status.PENDING)
                & Q(activated_at__isnull=True)
                & Q(holding_message__provider_message_id__gt="")
            )
            | (
                Q(status=IgAiReplyRecoveryJob.Status.PROCESSING)
                & (Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            )
            | (
                Q(status=IgAiReplyRecoveryJob.Status.SENDING)
                & (Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            )
        )
        .order_by("id")
        .values_list("id", flat=True)[:batch_size]
    )
    processed = 0
    for job_id in candidate_ids:
        if process_recovery_job(job_id) is not None:
            processed += 1
    return processed
