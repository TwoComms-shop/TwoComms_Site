"""Evidence-bound objection detection, method verification and prompt state."""
from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from management.models import IgObjection, IgObjectionAttempt


PATTERNS = (
    (
        IgObjection.Type.CHEAPER_ELSEWHERE,
        re.compile(r"\b(дешевш\w*|дешевле|cheaper\s+elsewhere)\b", re.I),
    ),
    (
        IgObjection.Type.PRICE,
        re.compile(
            r"\b(дорог\w*|дорогувато|задорого|занадто\s+дорог\w*|"
            r"не\s+по\s+кишен\w*|не\s+по\s+карман\w*|expensive|too\s+much)\b",
            re.I,
        ),
    ),
    (
        IgObjection.Type.PREPAYMENT_TRUST,
        re.compile(
            r"\b(не\s+довір\w*|не\s+довер\w*|боюс\w*|страшно|шахра\w*|"
            r"мошенн\w*)\b.{0,80}\b(передоплат\w*|предоплат\w*|оплат\w*)\b",
            re.I,
        ),
    ),
    (
        IgObjection.Type.SIZE_RISK,
        re.compile(
            r"\b(боюс\w*|не\s+впевнен\w*|не\s+уверен\w*|раптом|вдруг|"
            r"не\s+знаю)\b.{0,70}\b(підійд\w*|подойд\w*|розмір\w*|"
            r"размер\w*|сяде|сядет)\b",
            re.I,
        ),
    ),
    (
        IgObjection.Type.DEFECT_RISK,
        re.compile(
            r"\b(боюс\w*|страшно|раптом|вдруг|а\s+якщо|а\s+если)\b"
            r".{0,80}\b(брак\w*|дефект\w*|пошкодж\w*|поврежд\w*)\b",
            re.I,
        ),
    ),
    (
        IgObjection.Type.DELIVERY_TIME,
        re.compile(
            r"(?:\b(занадто|надто|слишком)\s+(довго|долго)\b|"
            r"\b(не\s+встиг\w*|не\s+успе\w*)\b)",
            re.I,
        ),
    ),
    (
        IgObjection.Type.PRINT_QUALITY,
        re.compile(
            r"(?:\b(принт\w*|друк\w*|печат\w*)\b.{0,90}"
            r"\b(потріск\w*|тріск\w*|треск\w*|зліз\w*|слез\w*|"
            r"зітр\w*|стир\w*|пран\w*)\b|"
            r"\b(боюс\w*|страшно|раптом|вдруг)\b.{0,70}"
            r"\b(потріск\w*|треск\w*|зліз\w*|слез\w*)\b)",
            re.I,
        ),
    ),
    (
        IgObjection.Type.OUT_OF_STOCK,
        re.compile(
            r"\b(немає|нет|відсутн\w*|отсутств\w*)\b.{0,60}"
            r"\b(мого\s+)?(розмір\w*|размер\w*|кольор\w*|цвет\w*|"
            r"варіант\w*|вариант\w*|наявност\w*|наличи\w*)\b",
            re.I,
        ),
    ),
    (
        IgObjection.Type.PAYDAY,
        re.compile(r"\b(після|после)\s+(зарплат\w*|авансу?)\b", re.I),
    ),
    (
        IgObjection.Type.COMPARE_BRAND,
        re.compile(
            r"(?:\b(порівню\w*|сравнива\w*|сравню\w*)\b.{0,60}\bбренд\w*\b|"
            r"\b(інш\w*|друг\w*)\s+бренд\w*\b)",
            re.I,
        ),
    ),
    (
        IgObjection.Type.ASK_PARTNER,
        re.compile(
            r"\b(спита\w*|спрошу|порад\w*|посовет\w*)\b.{0,60}"
            r"\b(дружин\w*|жен\w*|чоловік\w*|муж\w*|партнер\w*)\b",
            re.I,
        ),
    ),
    (
        IgObjection.Type.THINKING,
        re.compile(
            r"\b(подумаю|подумаємо|подумаем|поміркую|пізніше|позже|"
            r"ще\s+подума\w*)\b",
            re.I,
        ),
    ),
)


def _value_breakdown(text: str) -> bool:
    markers = re.findall(
        r"тканин\w*|футер\w*|dtf|друк\w*|принт\w*|шиємо|шьем|"
        r"щільн\w*|плотн\w*|виробниц\w*|производств\w*",
        text,
        re.I,
    )
    return len(set(marker.casefold() for marker in markers)) >= 2 and not re.search(
        r"знижк\w*|скидк\w*", text, re.I
    )


METHOD_PATTERNS = {
    "value_breakdown": _value_breakdown,
    "risk_reversal_exchange": lambda text: bool(
        re.search(r"14\s*(?:дн|днів|дней|days)", text, re.I)
        and re.search(r"обмін\w*|обмен\w*|поверн\w*|вернут\w*", text, re.I)
    ),
    "size_consult": lambda text: bool(
        "?" in text
        and re.search(r"зріст|рост|обхват|замір|замер|ваг\w*|вес", text, re.I)
    ),
    "explain_prepay_purpose": lambda text: bool(
        re.search(r"передоплат\w*|предоплат\w*", text, re.I)
        and re.search(
            r"пошт\w*|почт\w*|викуп\w*|выкуп\w*|отриман\w*|получен\w*",
            text,
            re.I,
        )
    ),
    "soft_isolate": lambda text: bool(
        "?" in text
        and re.search(
            r"ціна|цена|розмір|размер|принт|що\s+саме|что\s+именно",
            text,
            re.I,
        )
    ),
    "delivery_timeline": lambda text: bool(
        re.search(r"1\s*[-–]\s*3", text)
        and re.search(r"достав\w*|відправ\w*|отправ\w*|нова\s+пошт\w*", text, re.I)
    ),
    "print_quality": lambda text: bool(
        re.search(r"dtf|друк\w*|принт\w*", text, re.I)
        and re.search(r"пран\w*|стир\w*|тріск\w*|треск\w*", text, re.I)
    ),
    "alternative_offer": lambda text: bool(
        re.search(
            r"схож\w*|похож\w*|альтернатив\w*|інш\w*\s+(?:модел|варіант)|"
            r"друг\w*\s+(?:модел|вариант)",
            text,
            re.I,
        )
    ),
    "social_proof": lambda text: bool(
        re.search(
            r"twocomms\.shop|відгук\w*|отзыв\w*|українськ\w*\s+бренд|"
            r"украинск\w*\s+бренд",
            text,
            re.I,
        )
    ),
    "payday_timing": lambda text: bool(
        re.search(r"зарплат\w*|аванс\w*", text, re.I)
        and re.search(r"нагада\w*|напомн\w*|після|после|дат\w*|числ\w*", text, re.I)
    ),
    "partner_summary": lambda text: bool(
        re.search(r"коротк\w*|підсум\w*|итог\w*|основн\w*", text, re.I)
        and re.search(r"перешл\w*|надісл\w*|покаж\w*|дружин\w*|чоловік\w*|партнер\w*", text, re.I)
    ),
}


METHODS_BY_TYPE = {
    IgObjection.Type.PRICE: {"value_breakdown"},
    IgObjection.Type.THINKING: {"soft_isolate"},
    IgObjection.Type.SIZE_RISK: {"size_consult", "risk_reversal_exchange"},
    IgObjection.Type.PREPAYMENT_TRUST: {"explain_prepay_purpose", "social_proof"},
    IgObjection.Type.DEFECT_RISK: {"risk_reversal_exchange"},
    IgObjection.Type.DELIVERY_TIME: {"delivery_timeline"},
    IgObjection.Type.CHEAPER_ELSEWHERE: {"value_breakdown"},
    IgObjection.Type.PRINT_QUALITY: {"print_quality"},
    IgObjection.Type.OUT_OF_STOCK: {"alternative_offer"},
    IgObjection.Type.PAYDAY: {"payday_timing"},
    IgObjection.Type.COMPARE_BRAND: {"social_proof", "value_breakdown"},
    IgObjection.Type.ASK_PARTNER: {"partner_summary"},
}


def detect_objection_type(text: str) -> str:
    types = detect_objection_types(text)
    return types[0] if types else ""


def detect_objection_types(text: str) -> list[str]:
    from management.services.ig_turn_intelligence import current_objections

    kinds = {item["kind"] for item in current_objections([{"message_id": 0, "text": str(text or "")}])}
    return [str(kind) for kind, _pattern in PATTERNS if str(kind) in kinds]


def _message_floor(client) -> int:
    from management.services.ig_funnel_reset import current_message_floor

    return max(0, int(current_message_floor(client) or 0))


def _current_objections(client):
    # Opening an episode happens in a locked transaction. Callers commonly
    # retain the pre-transaction client object, whose FK cache still points to
    # no episode. Scope by the persisted pointer so a following message can
    # resolve the objection that was just opened.
    from management.models import IgClient
    from management.services.ig_turn_snapshot import prompt_cached

    # Э8.5: указатель эпизода читается дважды за сборку промпта — из
    # playbook-тегов и из блока возражений. Кэш на время сборки; вне неё
    # (в том числе при открытии эпизода в этом же ходе) чтение свежее.
    current_client = prompt_cached(
        f"episode_pointer:{getattr(client, 'pk', None)}",
        lambda: IgClient.objects.only("id", "current_commercial_episode")
        .filter(pk=getattr(client, "pk", None))
        .first(),
    )
    if current_client is None:
        return IgObjection.objects.none()
    qs = IgObjection.objects.filter(
        client_id=current_client.pk,
        opened_watermark_message_id__gte=_message_floor(current_client),
    )
    episode_id = int(
        getattr(current_client, "current_commercial_episode_id", 0) or 0
    )
    if episode_id:
        qs = qs.filter(episode_id=episode_id)
    else:
        qs = qs.filter(episode__isnull=True)
    return qs


def _dedupe_key(client, objection_type: str) -> str:
    episode_id = int(getattr(client, "current_commercial_episode_id", 0) or 0)
    episode_key = f"episode:{episode_id}" if episode_id else "noepisode"
    return (
        f"ig-objection:{client.pk}:{episode_key}:floor:{_message_floor(client)}:"
        f"{objection_type}"
    )


def observe_inbound_objection(
    client,
    message,
    objection_type: str,
    *,
    readiness: int,
    readiness_before: int | None = None,
) -> IgObjection | None:
    if not objection_type or not getattr(message, "pk", None):
        return None
    key = _dedupe_key(client, objection_type)
    readiness_after = max(0, int(readiness or 0))
    initial_readiness = (
        readiness_after
        if readiness_before is None
        else max(0, int(readiness_before or 0))
    )
    with transaction.atomic():
        from management.models import IgClient
        from management.services.ig_commercial_episodes import (
            ensure_open_episode_for_locked_client,
        )
        from management.services.ig_funnel_analytics import (
            record_client_step_event_in_transaction,
        )

        locked_client = IgClient.objects.select_for_update().get(pk=client.pk)
        episode = ensure_open_episode_for_locked_client(
            locked_client,
            materialization_prefix="ig-funnel",
        )
        row = IgObjection.objects.select_for_update().filter(dedupe_key=key).first()
        if row is None:
            row = IgObjection.objects.create(
                client=client,
                episode_id=episode.pk,
                objection_type=objection_type,
                first_message=message,
                last_message=message,
                readiness_before=initial_readiness,
                readiness_after=readiness_after,
                opened_watermark_message_id=message.pk,
                dedupe_key=key,
            )
            record_client_step_event_in_transaction(
                locked_client,
                event_type="objection_raised",
                event_key=f"ig-objection-raised:{row.pk}:{message.pk}",
                occurred_at=message.provider_created_at or message.created_at,
                stage=locked_client.stage,
                actor="customer",
                evidence={
                    "objection_id": row.pk,
                    "objection_type": objection_type,
                    "message_id": message.pk,
                },
            )
            return row
        if row.last_message_id == message.pk:
            return row

        previous_state = row.state
        row.last_message = message
        row.repeat_count = int(row.repeat_count or 0) + 1
        row.readiness_after = readiness_after
        row.outcome = IgObjection.Outcome.UNRESOLVED
        row.resolved_at = None
        row.state = IgObjection.State.OPEN
        row.is_true_objection = True
        if previous_state == IgObjection.State.HANDLED:
            last_attempt = row.attempts.order_by("-id").first()
            if last_attempt and last_attempt.result == IgObjectionAttempt.Result.PENDING:
                last_attempt.result = IgObjectionAttempt.Result.RE_OBJECTED
                last_attempt.client_response_message = message
                last_attempt.readiness_after = readiness_after
                last_attempt.save(update_fields=[
                    "result", "client_response_message", "readiness_after",
                ])
        row.save(update_fields=[
            "last_message", "repeat_count", "readiness_after", "state",
            "is_true_objection", "outcome", "resolved_at", "updated_at",
        ])
        record_client_step_event_in_transaction(
            locked_client,
            event_type="objection_raised",
            event_key=f"ig-objection-raised:{row.pk}:{message.pk}",
            occurred_at=message.provider_created_at or message.created_at,
            stage=locked_client.stage,
            actor="customer",
            evidence={
                "objection_id": row.pk,
                "objection_type": objection_type,
                "message_id": message.pk,
            },
        )
        return row


def observe_inbound_progress(
    client,
    message,
    *,
    objection_type: str = "",
    objection_types: tuple[str, ...] | list[str] | set[str] | None = None,
    readiness: int,
    commercial_progress: bool = False,
    purchase_progress: bool = False,
    abandoned: bool = False,
) -> None:
    states = (
        (IgObjection.State.OPEN, IgObjection.State.HANDLED)
        if abandoned or purchase_progress
        else (IgObjection.State.HANDLED,)
    )
    qs = _current_objections(client).filter(state__in=states)
    current_types = {str(value) for value in (objection_types or ()) if value}
    if objection_type:
        current_types.add(str(objection_type))
    if current_types:
        qs = qs.exclude(objection_type__in=current_types)
    readiness_after = max(0, int(readiness or 0))
    for row in qs.order_by("-updated_at", "-id")[:3]:
        attempt = row.attempts.order_by("-id").first()
        baseline = int(
            getattr(attempt, "readiness_before", None)
            if attempt is not None
            else row.readiness_after
            or 0
        )
        if abandoned:
            row.state = IgObjection.State.ABANDONED
            row.outcome = IgObjection.Outcome.LOST
        elif purchase_progress:
            row.state = IgObjection.State.RESOLVED
            row.outcome = IgObjection.Outcome.PURCHASED
        elif commercial_progress or readiness_after > baseline:
            row.state = IgObjection.State.RESOLVED
            row.outcome = IgObjection.Outcome.UNRESOLVED
        else:
            continue
        row.readiness_after = readiness_after
        row.resolved_at = timezone.now()
        row.save(update_fields=[
            "state", "outcome", "readiness_after", "resolved_at", "updated_at",
        ])
        if attempt and attempt.result == IgObjectionAttempt.Result.PENDING:
            attempt.result = (
                IgObjectionAttempt.Result.PURCHASED
                if purchase_progress
                else (
                    IgObjectionAttempt.Result.IGNORED
                    if abandoned
                    else IgObjectionAttempt.Result.ACCEPTED
                )
            )
            attempt.client_response_message = message
            attempt.readiness_after = readiness_after
            attempt.save(update_fields=[
                "result", "client_response_message", "readiness_after",
            ])


def record_reply_attempt(
    client,
    reply_message,
    control: dict,
    clean_text: str,
) -> IgObjectionAttempt | None:
    if not getattr(reply_message, "pk", None):
        return None
    with transaction.atomic():
        from management.models import IgClient
        from management.services.ig_funnel_analytics import (
            record_client_step_event_in_transaction,
        )

        locked_client = IgClient.objects.select_for_update().get(pk=client.pk)
        existing = IgObjectionAttempt.objects.filter(reply_message=reply_message).first()
        if existing is not None:
            return existing
        open_rows = list(
            _current_objections(locked_client)
            .select_for_update()
            .filter(state=IgObjection.State.OPEN)
            .order_by("-updated_at", "-id")[:3]
        )
        if not open_rows:
            return None

        raw = str((control or {}).get("objhandle") or "").strip().lower()
        claimed_type, separator, method = raw.partition(":")
        objection = open_rows[0]
        verified = False
        if not raw:
            method = "none"
            reason = "missing_objhandle"
        elif not separator or not claimed_type or not method:
            method = method or "none"
            reason = "invalid_objhandle"
        else:
            matching = next(
                (row for row in open_rows if row.objection_type == claimed_type),
                None,
            )
            if matching is None:
                reason = "objection_type_mismatch"
            else:
                objection = matching
                allowed_methods = METHODS_BY_TYPE.get(objection.objection_type, set())
                validator = METHOD_PATTERNS.get(method)
                if method not in allowed_methods:
                    reason = "method_not_allowed_for_type"
                elif validator is None or not validator(str(clean_text or "")):
                    reason = "method_fingerprint_mismatch"
                else:
                    verified = True
                    reason = "fingerprint_verified"

        attempt = IgObjectionAttempt.objects.create(
            objection=objection,
            method=(method or "none")[:48],
            verified=verified,
            verification_reason=reason,
            reply_message=reply_message,
            result=(
                IgObjectionAttempt.Result.PENDING
                if verified
                else IgObjectionAttempt.Result.IGNORED
            ),
            readiness_before=objection.readiness_after,
            readiness_after=objection.readiness_after,
        )
        objection.attempts_count = int(objection.attempts_count or 0) + 1
        if verified:
            objection.state = IgObjection.State.HANDLED
            objection.resolution_method = method
        objection.save(update_fields=[
            "attempts_count", "state", "resolution_method", "updated_at",
        ])
        if verified:
            record_client_step_event_in_transaction(
                locked_client,
                event_type="objection_handled",
                event_key=f"ig-objection-handled:{attempt.pk}",
                occurred_at=reply_message.provider_created_at or reply_message.created_at,
                stage=locked_client.stage,
                actor="bot",
                evidence={
                    "objection_id": objection.pk,
                    "attempt_id": attempt.pk,
                    "reply_message_id": reply_message.pk,
                    "method": method,
                    "verified": True,
                },
            )
        return attempt


def resolve_client_objections_on_purchase(client) -> int:
    rows = list(
        _current_objections(client)
        .filter(state__in=(IgObjection.State.OPEN, IgObjection.State.HANDLED))
        .order_by("-updated_at", "-id")[:6]
    )
    now = timezone.now()
    for row in rows:
        row.state = IgObjection.State.RESOLVED
        row.outcome = IgObjection.Outcome.PURCHASED
        row.resolved_at = now
        row.save(update_fields=["state", "outcome", "resolved_at", "updated_at"])
        attempt = row.attempts.order_by("-id").first()
        if attempt and attempt.result == IgObjectionAttempt.Result.PENDING:
            attempt.result = IgObjectionAttempt.Result.PURCHASED
            attempt.save(update_fields=["result"])
    return len(rows)


def objection_tags_for_client(client) -> set[str]:
    if not getattr(client, "pk", None):
        return set()
    return {
        f"objection_{value}"
        for value in _current_objections(client)
        .filter(state__in=(IgObjection.State.OPEN, IgObjection.State.HANDLED))
        .values_list("objection_type", flat=True)
    }


def _prompt_allowed(client) -> bool:
    if not getattr(client, "pk", None):
        return False
    from management.models import IgClient

    if client.intent == IgClient.Intent.SUPPORT:
        return False
    if client.primary_objection == IgClient.Objection.NO_BUY:
        return False
    if client.manager_takeover or client.stage in {
        IgClient.Stage.LEAD_TO_MANAGER,
        IgClient.Stage.PAID,
        IgClient.Stage.ORDER_CREATED,
        IgClient.Stage.DONE,
    }:
        return False
    if client.opted_out_at and (
        not client.opted_in_at or client.opted_in_at < client.opted_out_at
    ):
        return False
    try:
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        if client_has_confirmed_purchase(client):
            return False
    except Exception:
        pass
    return True


def objection_prompt_note(client) -> str:
    if not _prompt_allowed(client):
        return ""
    qs = _current_objections(client).prefetch_related("attempts")
    active = list(
        qs.filter(state__in=(IgObjection.State.OPEN, IgObjection.State.HANDLED))
        .order_by("-updated_at", "-id")[:3]
    )
    closed = list(
        qs.filter(state__in=(IgObjection.State.RESOLVED, IgObjection.State.ABANDONED))
        .order_by("-updated_at", "-id")[:3]
    )
    rows = active + closed
    if not rows:
        return ""

    lines = ["[ЗАПЕРЕЧЕННЯ - службове, клієнт цього не бачить]"]
    for row in rows:
        attempts = list(row.attempts.all())[:3]
        history = ", ".join(
            f"{attempt.method}={attempt.result}"
            for attempt in reversed(attempts)
        )
        suffix = f"; спроби: {history}" if history else ""
        lines.append(
            f"- {row.objection_type}: {row.state}, повтор #{row.repeat_count}{suffix}"
        )
    lines.extend([
        "Не повторюй метод, який вже не спрацював; зміни кут відповіді.",
        "Спочатку перевір, чи це справжня причина. Знижкою керує система.",
        "На 3-є повторення не тисни: постав [MANAGER].",
        "Після фактичної відпрацювання додай [OBJHANDLE:тип:метод].",
    ])
    return "\n".join(lines)
