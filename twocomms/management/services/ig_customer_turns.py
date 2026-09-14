"""Одне поняття ходу клієнта для трьох механізмів (Э0.6).

Три різні механізми потребують відповіді на питання «це один логічний хід чи
кілька?»:

* Э2.2 — burst клієнта не має давати кілька відповідей;
* Э2.11 — дублюючий webhook без `mid` не має давати другу строку;
* Э3.6 — provenance відповіді мусить назвати ВСІ вхідні, на які вона відповідає.

Роздільні реалізації дали б три неузгоджені механізми дедуплікації. Найгірший
випадок — burst із двох повідомлень, де одне задубльоване провайдером, — не
обробив би правильно жоден з них.

**Про величину debounce.** Свідомо взято мале фіксоване значення, а НЕ виведене
з поточної метрики `messages-per-turn`: та метрика описує поведінку системи, яка
ще не склеює ходи, і нічого не доводить про оптимальну затримку. Правильний шлях —
почати з малого, зміряти вплив на конверсію і на час до першої відповіді,
коригувати за даними.

Цей модуль **записує** ходи і вміє відповісти, чи хід готовий до обробки, але сам
не змінює порядок обробки черги: перехід воркера на хід як одиницю виконання — це
Э2.2 за окремим флагом. Розділення зроблено свідомо: спочатку поняття стає
вимірюваним, потім змінюється поведінка.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping

from django.db import IntegrityError, transaction
from django.utils import timezone

from management.models import (
    IgCustomerTurn,
    IgTurnMessage,
    InstagramBotMessage,
)

# Мале фіксоване вікно склейки. Не виводити з метрики поточної поведінки.
TURN_DEBOUNCE = timedelta(seconds=6)
# Верхня межа очікування. Пряме питання мусить отримати відповідь у розумний час,
# навіть якщо клієнт продовжує друкувати. Точний SLA — Э0.7; поки що записане
# допущення, а не виміряне значення.
MAX_TURN_WAIT = timedelta(seconds=20)


def effective_max_wait_seconds() -> float:
    """Фактичний максимум очікування склейки — джерело істини для бюджету ходу.

    Э2.2B, prerequisite. `ig_turn_budget.turn_phases()` раніше оголошував фазу
    очікування як `MAX_TURN_WAIT` (20 с), тоді як реальний дедлайн завжди
    дорівнює `min(now + TURN_DEBOUNCE, now + MAX_TURN_WAIT)` і не продовжується
    при attach. Тобто оголошений бюджет був на 14 с більший за реальний.

    Наслідок був не косметичний: `customer_notice_threshold_seconds()` =
    очікування + генерація, тому технічний текст клієнту стримувався на 14 с
    довше за фактичний дедлайн, а вікно живості демона було настільно ж менш
    чутливим.

    Функція повертає **зв'язок**, а не число: якщо `TURN_DEBOUNCE` зміниться або
    буде піднятий вище `MAX_TURN_WAIT`, бюджет поїде разом з ним. Коли з'явиться
    typed wait policy (Э2.2B Phase 3), тут стане `max(silent hard cap)` увімкнених
    класів, обмежений глобальною межею.
    """
    if not _flag("IG_TURN_DEBOUNCE", True):
        # Флаг вимкнений — хід не збирається, воркер бере рядок одразу.
        return 0.0
    return float(min(TURN_DEBOUNCE, MAX_TURN_WAIT).total_seconds())


@dataclass(frozen=True)
class TurnAttachment:
    """Результат прив'язки вхідного до ходу."""

    turn: IgCustomerTurn
    created: bool
    attached: bool
    reason: str = ""
    revision_id: int = 0
    successor_required: bool = False


def _flag(name: str, default: bool) -> bool:
    from management.services.ig_provider_incidents import flag

    return flag(name, default)


def message_dedupe_key(row: InstagramBotMessage) -> str:
    """Найстійкіша доступна ідентичність повідомлення.

    Порядок навмисний: native `mid` провайдера → provider object id вкладення →
    синтетичний ключ. Підписані media URL одноразові, тому вони НЕ є
    ідентичністю: саме через них повтор того самого вкладення з новим підписом
    давав другу строку (Э2.11).
    """
    mid = str(getattr(row, "mid", "") or "").strip()
    if mid:
        return f"mid:{mid}"
    attachment_id = _provider_attachment_object_id(row)
    if attachment_id:
        return f"object:{attachment_id}"
    synthetic = str(getattr(row, "synthetic_event_key", "") or "").strip()
    if synthetic:
        return f"synthetic:{synthetic}"
    return f"row:{getattr(row, 'pk', '') or ''}"


def _provider_attachment_object_id(row) -> str:
    """Native provider object id вкладення, якщо провайдер його дав."""
    media = getattr(row, "attachment_media", None)
    if not isinstance(media, list):
        return ""
    for item in media:
        if not isinstance(item, dict):
            continue
        for key in ("provider_object_id", "object_id", "attachment_id", "asset_id"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
    return ""


def bypasses_debounce(row: InstagramBotMessage) -> bool:
    """Ходи, які не можна затримувати.

    Натискання кнопки — завершена дія, а не частина набору тексту. Явний opt-out
    і запит менеджера теж не мають чекати: затримка тут це не склейка, а
    ігнорування.
    """
    if str(getattr(row, "quick_reply_payload", "") or "").strip():
        return True
    text = str(getattr(row, "text", "") or "")
    if not text:
        return False
    from management.services.bot_sales_classifier import OPT_OUT_RE, SUPPORT_RE

    return bool(OPT_OUT_RE.search(text) or SUPPORT_RE.search(text))


def _event_at(row: InstagramBotMessage):
    return getattr(row, "provider_created_at", None) or getattr(row, "created_at", None)


def _turn_source_messages(turn: IgCustomerTurn) -> list[InstagramBotMessage]:
    return list(
        InstagramBotMessage.objects.filter(turn_membership__turn_id=turn.pk)
        .order_by("turn_membership__ordinal", "turn_membership__id")
    )


def _record_shadow_revision(
    turn: IgCustomerTurn,
    *,
    now,
    bypass: bool,
    referral: Mapping | None = None,
) -> tuple[int, bool, str]:
    from management.services.ig_turn_revisions import create_collecting_revision

    messages = _turn_source_messages(turn)
    latest = messages[-1]
    metadata = {
        latest.pk: {
            "source_namespace": str(
                getattr(latest, "provider_namespace", "") or ""
            ),
            "referral": dict(referral or {}),
        }
    }
    result = create_collecting_revision(
        turn,
        messages,
        source_metadata=metadata,
        now=now,
        bypass_quiet=bypass,
        transfer_sources=True,
    )
    if not result.created or result.revision is None:
        raise RuntimeError(result.reason or "turn_revision_not_created")
    return result.revision.pk, result.successor_required, result.reason


def ensure_turn_for_inbound(
    row: InstagramBotMessage,
    *,
    now=None,
    referral: Mapping | None = None,
) -> TurnAttachment | None:
    """Прив'язати вхідне до відкритого ходу або відкрити новий.

    Дедлайн ходу фіксується від ПЕРШОГО повідомлення і не продовжується
    наступними. Інакше клієнт, який продовжує писати, ніколи не отримав би
    відповіді — і склейка перетворилась би на молчання.
    """
    if not _flag("IG_CUSTOMER_TURNS", True):
        return None
    client_id = getattr(row, "client_id", None)
    if not client_id or getattr(row, "role", "") != InstagramBotMessage.Role.USER:
        return None
    # Local collection clocks begin at durable ingress, independently of an
    # old/future provider event timestamp (which still governs channel time).
    now = now or timezone.now()
    dedupe_key = message_dedupe_key(row)
    bypass = bypasses_debounce(row)

    with transaction.atomic():
        existing = (
            IgTurnMessage.objects.select_related("turn")
            .filter(message_id=row.pk)
            .first()
        )
        if existing is not None:
            # Дублюючий webhook того самого рядка. Другої строки не буде.
            return TurnAttachment(existing.turn, False, False, "already_attached")

        open_turn = (
            IgCustomerTurn.objects.select_for_update()
            .filter(
                client_id=client_id,
                claim_state=IgCustomerTurn.ClaimState.OPEN,
                window_deadline__gt=now,
            )
            .order_by("-id")
            .first()
        )
        overflow_predecessor = None
        if open_turn is not None and not open_turn.bypass_debounce:
            keys = list(open_turn.dedupe_keys or [])
            if dedupe_key in keys:
                # Те саме вкладення з новим підписом URL: інший рядок, та сама
                # ідентичність провайдера. Другого повідомлення в ході не буде.
                return TurnAttachment(open_turn, False, False, "duplicate_identity")
            from management.services.ig_turn_revisions import (
                prospective_overflow_reason,
            )

            existing_messages = _turn_source_messages(open_turn)
            if prospective_overflow_reason([*existing_messages, row]):
                # The whole new source becomes the primary member of an ordered
                # successor.  Existing OneToOne memberships are never moved.
                overflow_predecessor = open_turn
                open_turn = None
            else:
                ordinal = int(open_turn.message_count or 0) + 1
                try:
                    IgTurnMessage.objects.create(
                        turn=open_turn,
                        message_id=row.pk,
                        ordinal=ordinal,
                        role=row.role,
                    )
                except IntegrityError:
                    return TurnAttachment(open_turn, False, False, "attach_race")
                keys.append(dedupe_key)
                open_turn.dedupe_keys = keys[:50]
                open_turn.message_count = ordinal
                if bypass:
                    # Кнопка або opt-out посеред burst-у: хід більше не чекає.
                    open_turn.bypass_debounce = True
                open_turn.save(update_fields=[
                    "dedupe_keys", "message_count", "bypass_debounce", "updated_at",
                ])
                revision_id, successor_required, _revision_reason = (
                    _record_shadow_revision(
                        open_turn,
                        now=now,
                        bypass=bypass,
                        referral=referral,
                    )
                )
                return TurnAttachment(
                    open_turn,
                    False,
                    True,
                    "attached",
                    revision_id,
                    successor_required,
                )

        deadline = now + (timedelta(0) if bypass else TURN_DEBOUNCE)
        try:
            turn = IgCustomerTurn.objects.create(
                client_id=client_id,
                episode_id=getattr(row.client, "current_commercial_episode_id", None)
                if getattr(row, "client", None) is not None
                else None,
                primary_source_message_id=row.pk,
                window_started_at=now,
                window_deadline=min(deadline, now + MAX_TURN_WAIT),
                dedupe_keys=[dedupe_key],
                message_count=1,
                bypass_debounce=bypass,
            )
        except IntegrityError:
            turn = IgCustomerTurn.objects.filter(primary_source_message_id=row.pk).first()
            if turn is None:
                return None
            return TurnAttachment(turn, False, False, "create_race")
        IgTurnMessage.objects.create(
            turn=turn, message_id=row.pk, ordinal=1, role=row.role
        )
        revision_id, successor_required, _revision_reason = _record_shadow_revision(
            turn,
            now=now,
            bypass=bypass,
            referral=referral,
        )
        reason = "overflow_successor" if overflow_predecessor else "created"
        if successor_required:
            reason = "oversize_blocked"
        return TurnAttachment(
            turn,
            True,
            True,
            reason,
            revision_id,
            successor_required,
        )


def turn_is_due(turn: IgCustomerTurn, *, now=None) -> bool:
    """Чи готовий хід до обробки."""
    if turn is None or turn.claim_state != IgCustomerTurn.ClaimState.OPEN:
        return False
    now = now or timezone.now()
    if turn.bypass_debounce:
        return True
    return now >= turn.window_deadline


def due_turn_ids(*, limit: int = 10, now=None) -> list:
    now = now or timezone.now()
    from django.db.models import Q

    return list(
        IgCustomerTurn.objects.filter(
            claim_state=IgCustomerTurn.ClaimState.OPEN,
            client__hidden_at__isnull=True,
        )
        .filter(Q(bypass_debounce=True) | Q(window_deadline__lte=now))
        .order_by("window_deadline", "id")
        .values_list("id", flat=True)[: max(1, int(limit))]
    )


def claim_turn(turn_id: int, *, now=None) -> tuple[IgCustomerTurn | None, str]:
    """Атомарний claim ходу; повертає (хід, токен) або (хід, "")."""
    now = now or timezone.now()
    token = secrets.token_hex(16)
    claimed = IgCustomerTurn.objects.filter(
        pk=turn_id, claim_state=IgCustomerTurn.ClaimState.OPEN
    ).update(
        claim_state=IgCustomerTurn.ClaimState.CLAIMED,
        claimed_at=now,
        claim_token=token,
        updated_at=now,
    )
    turn = IgCustomerTurn.objects.filter(pk=turn_id).first()
    return turn, (token if claimed == 1 else "")


def mark_turn_processed(turn_id: int, *, reason: str = "", now=None) -> None:
    now = now or timezone.now()
    fields = {
        "claim_state": IgCustomerTurn.ClaimState.PROCESSED,
        "processed_at": now,
        "claim_token": "",
        "updated_at": now,
    }
    if reason:
        fields["terminal_reason"] = str(reason)[:20]
    transitioned = IgCustomerTurn.objects.filter(pk=turn_id).exclude(
        claim_state=IgCustomerTurn.ClaimState.PROCESSED
    ).update(**fields)
    if transitioned:
        if reason:
            try:
                from management.services.ig_turn_revisions import (
                    terminalize_legacy_shadow_revision,
                )

                terminalize_legacy_shadow_revision(
                    turn_id, reason=str(reason), now=now
                )
            except Exception:
                # The revision producer is shadow-only until B03.4/B03.5. Its
                # failure cannot change the existing turn terminal outcome.
                pass
        try:
            from management.services.ig_analysis_materiality import (
                record_completed_customer_turn,
            )

            record_completed_customer_turn(turn_id)
        except Exception:
            # Shadow telemetry must never change reply/turn behavior.
            pass


def turn_id_for_message(message_id) -> int:
    """Хід, якому належить рядок, або 0. Один індексований запит по unique-ключу."""
    try:
        message_id = int(getattr(message_id, "pk", message_id) or 0)
    except (TypeError, ValueError):
        return 0
    if not message_id:
        return 0
    return int(
        IgTurnMessage.objects.filter(message_id=message_id)
        .values_list("turn_id", flat=True)
        .first()
        or 0
    )


def current_revision_id_for_message(message_id) -> int:
    """Shadow head for a future revision-aware claimant/collector."""
    turn_id = turn_id_for_message(message_id)
    if not turn_id:
        return 0
    from management.services.ig_turn_revisions import current_revision_for_turn

    revision = current_revision_for_turn(turn_id)
    return int(getattr(revision, "pk", 0) or 0)


# Статуси рядка, після яких хід уже не отримає ще однієї спроби виконання.
_TERMINAL_ROW_STATUSES = (
    InstagramBotMessage.Status.DONE,
    InstagramBotMessage.Status.FAILED,
)


def _reason_for_terminal_row(row: InstagramBotMessage) -> str:
    """Класифікувати причину терміналізації ходу по стану рядка."""
    reasons = IgCustomerTurn.TerminalReason
    send_state = str(getattr(row, "send_state", "") or "")
    if send_state in ("unknown", "ambiguous"):
        # Доставка невідома: терміналізуємо з причиною, але НЕ ретраїмо.
        return reasons.SEND_UNKNOWN
    if row.status == InstagramBotMessage.Status.DONE:
        if send_state == "sent":
            return reasons.REPLIED
        # Рядок опрацьовано без customer-visible відправки: поглинутий у ході,
        # службовий, або відповідь свідомо не потрібна.
        return reasons.NO_REPLY_NEEDED
    return reasons.FAILED


def finalize_turn_for_row(row_or_id, *, now=None) -> str:
    """Терміналізувати хід рядка, якщо сам рядок уже терміналом (Э2.2B).

    Раніше `mark_turn_processed()` викликався ЛИШЕ у деградаційній гілці
    `_claim_next()` (рядок ходу зник). На нормальному шляху хід назавжди лишався
    `CLAIMED`: 7/7 автоматично виконаних production-ходів після міграції `0173`
    саме такі. Побічний ефект був живий, а не косметичний —
    `record_completed_customer_turn()` не спрацьовував ні для одного реального
    ходу, тому телеметрія матеріальності аналізу була порожньою.

    Точка виклику одна (після виконання рядка), тому всі внутрішні гілки
    `_process_one` покриті без правки кожної з них окремо. Повертає застосовану
    причину або "" якщо нічого не змінено.
    """
    message_id = int(getattr(row_or_id, "pk", row_or_id) or 0)
    if not message_id:
        return ""
    turn_id = turn_id_for_message(message_id)
    if not turn_id:
        return ""
    # Читаємо статус з БД: виконання могло змінити його після claim.
    row = (
        InstagramBotMessage.objects.filter(pk=message_id)
        .only("id", "status", "send_state")
        .first()
    )
    if row is None:
        mark_turn_processed(
            turn_id, reason=IgCustomerTurn.TerminalReason.ROW_MISSING, now=now
        )
        return IgCustomerTurn.TerminalReason.ROW_MISSING
    if row.status not in _TERMINAL_ROW_STATUSES:
        # Рядок повернувся в pending (провайдерський backoff) — хід ще живий.
        return ""
    reason = _reason_for_terminal_row(row)
    mark_turn_processed(turn_id, reason=reason, now=now)
    return reason


def turn_lease_seconds() -> float:
    """Скільки хід має право лишатись `CLAIMED` до реконсиляції.

    Виводиться з оголошеного бюджету ходу (Э2.10), а не задається окремо:
    незалежне число розійшлося б з бюджетом при першій же правці таймауту.
    """
    try:
        from management.services.ig_turn_budget import declared_turn_budget_seconds

        declared = float(declared_turn_budget_seconds())
    except Exception:
        declared = 120.0
    # Запас на квантування тіка демона і на реанімацію рядка в `processing`.
    return max(60.0, declared * 2.0)


def stale_claimed_turns(*, now=None, limit: int = 200) -> list:
    """Класифікувати застарілі `CLAIMED` ходи БЕЗ запису (dry-run).

    Масовий слепий `CLAIMED → PROCESSED` заборонений: він приховав би саме ті
    випадки, які треба побачити — перетнуту межу відправки і невідому доставку.
    Тому інвентаризація і застосування — різні кроки.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=turn_lease_seconds())
    turns = list(
        IgCustomerTurn.objects.filter(
            claim_state=IgCustomerTurn.ClaimState.CLAIMED,
        )
        .filter(claimed_at__lt=cutoff)
        .order_by("claimed_at", "id")[: max(1, int(limit))]
    )
    reasons = IgCustomerTurn.TerminalReason
    report = []
    for turn in turns:
        rows = list(
            InstagramBotMessage.objects.filter(
                pk__in=turn_message_ids(turn) or [turn.primary_source_message_id]
            ).only("id", "status", "send_state")
        )
        if not rows:
            report.append({"turn_id": turn.pk, "reason": reasons.ROW_MISSING, "rows": []})
            continue
        states = {str(r.send_state or "") for r in rows}
        statuses = {r.status for r in rows}
        if states & {"unknown", "ambiguous"}:
            reason = reasons.SEND_UNKNOWN
        elif states & {"sending"}:
            # Межа провайдера перетнута, receipt невідомий: не ретраїти.
            reason = reasons.SEND_UNKNOWN
        elif statuses <= set(_TERMINAL_ROW_STATUSES):
            reason = (
                reasons.REPLIED if states & {"sent"} else reasons.NO_REPLY_NEEDED
            )
        else:
            reason = reasons.LEASE_EXPIRED
        report.append(
            {
                "turn_id": turn.pk,
                "client_id": turn.client_id,
                "claimed_at": turn.claimed_at,
                "reason": reason,
                "rows": [
                    {"id": r.pk, "status": r.status, "send_state": r.send_state}
                    for r in rows
                ],
            }
        )
    return report


def reconcile_stale_claimed_turns(*, now=None, limit: int = 200, apply: bool = False) -> dict:
    """Lease-aware реконсиляція `CLAIMED`; `apply=False` нічого не пише."""
    report = stale_claimed_turns(now=now, limit=limit)
    counts: dict = {}
    for entry in report:
        counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
        if apply:
            mark_turn_processed(entry["turn_id"], reason=entry["reason"], now=now)
    return {"scanned": len(report), "counts": counts, "applied": bool(apply), "entries": report}


def turn_message_ids(turn: IgCustomerTurn) -> list:
    """Усі source-повідомлення ходу в порядку надходження (для provenance Э3.6)."""
    return list(
        IgTurnMessage.objects.filter(turn_id=getattr(turn, "pk", turn))
        .order_by("ordinal", "id")
        .values_list("message_id", flat=True)
    )


def claimable_row_id(turn: IgCustomerTurn) -> int:
    """Рядок, який обробляє воркер: НАЙНОВІШЕ вхідне ходу.

    Відповідати треба на актуальний хід клієнта. У production-сценарії «хочу
    худі» → «чорне» → «розмір L» перше повідомлення вже неповне: відповідь на
    нього виглядала б так, ніби бот не прочитав решту.
    """
    ids = turn_message_ids(turn)
    return int(ids[-1]) if ids else int(turn.primary_source_message_id or 0)


def absorb_turn_messages(turn: IgCustomerTurn, *, keep_message_id: int) -> int:
    """Позначити решту рядків ходу поглинутими, НЕ видаляючи їх.

    Кожен сирий рядок лишається в CRM як evidence: оператор мусить бачити, що
    саме написав клієнт, навіть якщо відповідь була одна.
    """
    turn_id = getattr(turn, "pk", turn)
    ids = [value for value in turn_message_ids(turn) if int(value) != int(keep_message_id)]
    if not ids:
        return 0
    now = timezone.now()
    return InstagramBotMessage.objects.filter(
        pk__in=ids,
        role=InstagramBotMessage.Role.USER,
    ).exclude(
        # Рядок, який уже перетнув межу відправки, чіпати не можна.
        send_state__in=("sending", "sent", "unknown"),
    ).update(
        status=InstagramBotMessage.Status.DONE,
        consumed_by_turn_id=turn_id,
        processed_at=now,
        processing_started_at=None,
    )


def due_turn_for_claim(*, now=None):
    """Найсвіжіший готовий хід і рядок, який треба обробити.

    Повертає `(turn, row_id)` або `(None, 0)`. Порядок — за тією ж логікою, що й
    у старому `_claim_next`: спочатку найактивніша розмова.
    """
    if not _flag("IG_TURN_DEBOUNCE", True):
        return None, 0
    now = now or timezone.now()
    from django.db.models import Q

    due = (
        IgCustomerTurn.objects.filter(
            claim_state=IgCustomerTurn.ClaimState.OPEN,
            client__hidden_at__isnull=True,
        )
        .filter(Q(bypass_debounce=True) | Q(window_deadline__lte=now))
        .exclude(
            client__automation_lease_token__gt="",
            client__automation_lease_until__gt=now,
        )
    )
    turn = _starving_turn(due, now=now)
    if turn is None:
        turn = due.order_by("-window_started_at", "-id").first()
    if turn is None:
        return None, 0
    return turn, claimable_row_id(turn)


def _starving_turn(due, *, now):
    """Хід, який чекає довше потолка віку, — його не має обходити свіжий (Э2.8).

    Порядок «найсвіжіше першим» правильний для інтерактивності, але без верхньої
    межі очікування він дозволяє безперервному потоку нових повідомлень тримати
    старий хід нижче голови черги необмежено довго. Голодують саме дорогі
    діалоги: клієнт, який чекає давно, з більшою ймовірністю в середині воронки.

    Тому вибір двофазний. Спочатку — найстаріший хід за потолком віку (при
    рівному віці перемагає вища стадія воронки), і лише якщо голодуючих немає,
    працює звичайний свіжий порядок. Потолок виведений з бюджету ходу — див.
    `ig_queue_priority.age_ceiling_seconds()`.
    """
    from management.services import ig_queue_priority

    if not ig_queue_priority.starvation_enabled():
        return None
    ceiling = ig_queue_priority.age_ceiling_seconds()
    cutoff = now - timedelta(seconds=ceiling)
    return (
        due.filter(window_started_at__lt=cutoff)
        .annotate(stage_rank=ig_queue_priority.stage_rank_cases())
        .order_by("window_started_at", "-stage_rank", "id")
        .first()
    )


def has_open_undue_turn(row: InstagramBotMessage, *, now=None) -> bool:
    """Чи належить рядок ходу, який ще збирає повідомлення.

    Потрібно для деградації: рядок без ходу (запис не вдався) мусить лишатись
    клеймабельним, інакше збій телеметрії зробив би чергу мертвою.
    """
    if not _flag("IG_TURN_DEBOUNCE", True):
        return False
    now = now or timezone.now()
    membership = (
        IgTurnMessage.objects.filter(message_id=getattr(row, "pk", row))
        .select_related("turn")
        .first()
    )
    if membership is None:
        return False
    turn = membership.turn
    if turn.claim_state != IgCustomerTurn.ClaimState.OPEN:
        return False
    return not turn_is_due(turn, now=now)


def messages_per_turn(*, days: int = 7) -> dict:
    """Метрика Э0.6: скільки повідомлень у ході насправді.

    Знаменник — ходи, числитель — повідомлення. Окремо повертається розподіл,
    щоб побачити не тільки середнє: burst-и рідкі, і середнє їх приховує.
    """
    from collections import Counter

    since = timezone.now() - timedelta(days=max(1, int(days)))
    counts = list(
        IgCustomerTurn.objects.filter(created_at__gte=since).values_list(
            "message_count", flat=True
        )
    )
    if not counts:
        return {"turns": 0, "messages": 0, "avg": 0.0, "distribution": {}}
    distribution = dict(sorted(Counter(counts).items()))
    return {
        "turns": len(counts),
        "messages": sum(counts),
        "avg": round(sum(counts) / len(counts), 3),
        "distribution": distribution,
    }
