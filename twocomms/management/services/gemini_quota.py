"""Бюджеты free-tier по паре (ключ, модель) и маршрутизация по тирам (ЭБ.4).

**Замер, с которого всё началось** (консоль AI Studio, один ключ, 29.08.2026):

    Gemini 3.6 Flash        RPM 5/5    TPM 13.03K/250K   RPD 21/20   ← превышено
    Gemini 3.7 Flash        RPM 3/5    TPM 20.86K/250K   RPD 27/20   ← превышено
    Gemini 3.5 Flash        RPM 1/5    TPM 5/250K        RPD 1/20
    Gemini 3.5 Flash Lite   RPM 1/15   TPM 5/250K        RPD 1/500

Каждый из шести ключей — отдельный Google-проект, то есть шесть независимых
наборов этих бюджетов. На весь пул за сутки: **120** запросов на 3.7-flash,
120 на 3.6, 120 на 3.5-flash и **3000** на 3.5-flash-lite.

**Что эти числа доказывают.** Прежняя цепочка чата начиналась с 3.7 на КАЖДОМ
ходе. При 20-25 клиентах в день и 5-8 репликах на диалог это 100-200 генераций
только на ответы — то есть дневной бюджет самой дефицитной модели выгорал до
обеда, и каждый следующий ход получал 429. Скриншот это и показывает: 27/20 на
3.7 при 1/500 на lite. Мы тратили самый скудный ресурс на самую частую операцию.

**Решение — квота как средство изоляции.** Лимиты принадлежат паре
(проект, модель), поэтому выбор модели сам по себе разделяет потребителей: они
физически не могут съесть бюджет друг друга. Отсюда тиры:

    LITE      3.5-flash-lite   3000/сутки  ← обычный ответ клиенту (самая частая)
    STRONG    3.7-flash         120/сутки  ← решения: товар, размер, оплата, медиа
    ANALYSIS  3.6-flash         120/сутки  ← разбор диалога, память, оценка UGC
    SPILLOVER 3.5-flash         120/сутки  ← общий резерв, когда тир исчерпан

**Рахівник дорадчий.** Квоту может потратить другой процесс или другая сессия,
поэтому реальный 429 всегда главнее локальных цифр (он закрывает пару через
`gemini_keys.mark_429`). Обратная ошибка дешёвая: мы лишь раньше уйдём на другую
пару. При любой проблеме с БД рахівник отвечает «можно» — сбой учёта не должен
лишать клиента ответа.
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone

PT = ZoneInfo("America/Los_Angeles")

# Бюджеты одной пары (ключ, модель) в сутки Pacific. Числа — из консоли квот, а
# не из документации: у free-tier они меняются, и источником истины должен быть
# наблюдаемый лимит. Переопределяются `GEMINI_MODEL_BUDGETS` в настройках.
DEFAULT_MODEL_BUDGETS = {
    "gemini-3.7-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini-3.6-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini-3.5-flash": {"rpm": 5, "tpm": 250_000, "rpd": 20},
    "gemini-3.5-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "gemini-3.1-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 500},
    "gemini-2.5-flash": {"rpm": 10, "tpm": 250_000, "rpd": 250},
    "gemini-2.5-flash-lite": {"rpm": 15, "tpm": 250_000, "rpd": 1000},
}

# Тиры и модель каждого тира. Порядок внутри значения — цепочка деградации:
# сначала своя модель тира, затем общий резерв, и только потом чужие тиры.
# Порог, ниже которого параллельная волна не оправдана: при 20 запросах в сутки
# волна из трёх ключей стоит 15% дневного бюджета модели ради одного ответа.
HEDGE_MIN_RPD = 100

TIER_LITE = "lite"
TIER_STRONG = "strong"
TIER_ANALYSIS = "analysis"
TIER_GROUNDED = "grounded"

DEFAULT_TIER_CHAINS = {
    TIER_LITE: ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash"],
    TIER_STRONG: ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
    TIER_ANALYSIS: ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
    # Grounding бесплатен только на 2.5 — цепочка не смешивается с остальными.
    TIER_GROUNDED: ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
}

# Задача → тир. Ключ — `reasoning_task`, который уже проставлен на каждом вызове,
# поэтому отдельный классификатор сложности не нужен.
DEFAULT_TASK_TIERS = {
    # Частая операция: короткий ответ клиенту в переписке.
    "customer_chat": TIER_LITE,
    # Решения, где глубина меняет ИСХОД, а не формулировку.
    "product_decision": TIER_STRONG,
    "size_fit_decision": TIER_STRONG,
    "catalog_match": TIER_STRONG,
    "payment_decision": TIER_STRONG,
    "order_decision": TIER_STRONG,
    "media_analysis": TIER_STRONG,
    # Разбор и извлечение фактов: вызовов мало, польза от глубины высокая.
    "customer_intelligence": TIER_ANALYSIS,
    "conversation_reanalysis": TIER_ANALYSIS,
    "journey_trace_reconstruction": TIER_ANALYSIS,
    "memory_summary": TIER_ANALYSIS,
    "ugc_evidence_assessment": TIER_ANALYSIS,
    "reporting_summary": TIER_ANALYSIS,
    "follow_cta_copy": TIER_ANALYSIS,
}


def model_budgets() -> dict:
    configured = getattr(django_settings, "GEMINI_MODEL_BUDGETS", None)
    if isinstance(configured, dict) and configured:
        merged = dict(DEFAULT_MODEL_BUDGETS)
        for model, budget in configured.items():
            if isinstance(budget, dict):
                merged[str(model)] = {**merged.get(str(model), {}), **budget}
        return merged
    return dict(DEFAULT_MODEL_BUDGETS)


def budget_for(model: str) -> dict:
    """Бюджет пары. Неизвестная модель — без ограничений (учёт не мешает работе)."""
    return model_budgets().get(str(model or ""), {})


def tier_chains() -> dict:
    configured = getattr(django_settings, "GEMINI_TIER_CHAINS", None)
    if isinstance(configured, dict) and configured:
        merged = dict(DEFAULT_TIER_CHAINS)
        for tier, chain in configured.items():
            if isinstance(chain, (list, tuple)) and chain:
                merged[str(tier)] = [str(model) for model in chain]
        return merged
    return {tier: list(chain) for tier, chain in DEFAULT_TIER_CHAINS.items()}


def task_tiers() -> dict:
    configured = getattr(django_settings, "GEMINI_TASK_TIERS", None)
    if isinstance(configured, dict) and configured:
        merged = dict(DEFAULT_TASK_TIERS)
        merged.update({str(task): str(tier) for task, tier in configured.items()})
        return merged
    return dict(DEFAULT_TASK_TIERS)


def tier_for_task(reasoning_task: str, *, role: str = "chat") -> str:
    """Тир для задачи. Неизвестная задача чата идёт в LITE, фоновая — в ANALYSIS.

    Неизвестное имя не должно молча получать самую дефицитную модель: цена
    ошибки — выгоревший за полдня бюджет 3.7.
    """
    task = str(reasoning_task or "").strip()
    mapped = task_tiers().get(task)
    if mapped:
        return mapped
    if role == "checker":
        return TIER_GROUNDED
    return TIER_LITE if role == "chat" else TIER_ANALYSIS


def chain_for_task(reasoning_task: str, *, role: str = "chat") -> list:
    return list(tier_chains().get(tier_for_task(reasoning_task, role=role), []))


def pacific_day(now: datetime.datetime | None = None) -> datetime.date:
    """Сутки Pacific: именно по ним провайдер скидывает RPD."""
    now = now or timezone.now()
    return now.astimezone(PT).date()


def capacity_snapshot(
    key_names,
    models,
    *,
    now=None,
) -> dict[tuple[str, str], dict]:
    """Bulk, read-only quota projection for candidate planning.

    Missing rows mean zero local usage.  Planning must never ``get_or_create``
    the full 6×model matrix: zero-usage rows are not provider evidence and the
    N+1 writes added latency before every customer reply.
    """
    now = now or timezone.now()
    keys = tuple(dict.fromkeys(str(value) for value in key_names if value))
    model_names = tuple(dict.fromkeys(str(value) for value in models if value))
    if not keys or not model_names:
        return {}
    from management.models import GeminiModelQuotaUsage

    try:
        rows = list(
            GeminiModelQuotaUsage.objects.filter(
                day_date=pacific_day(now),
                key_name__in=keys,
                model__in=model_names,
            )
        )
    except DatabaseError:
        rows = []
    by_pair = {(row.key_name, row.model): row for row in rows}
    snapshot: dict[tuple[str, str], dict] = {}
    for key_name in keys:
        for model in model_names:
            budget = budget_for(model)
            if not budget:
                snapshot[(key_name, model)] = {
                    "rpd": None,
                    "rpm": None,
                    "tpm": None,
                }
                continue
            row = by_pair.get((key_name, model))
            minute_requests, minute_tokens = (
                _minute_state(row, now) if row is not None else (0, 0)
            )
            requests = int(row.requests or 0) if row is not None else 0
            rpd_budget = int(budget.get("rpd") or 0)
            rpm_budget = int(budget.get("rpm") or 0)
            tpm_budget = int(budget.get("tpm") or 0)
            snapshot[(key_name, model)] = {
                "rpd": max(0, rpd_budget - requests) if rpd_budget else None,
                "rpm": max(0, rpm_budget - minute_requests) if rpm_budget else None,
                "tpm": max(0, tpm_budget - minute_tokens) if tpm_budget else None,
            }
    return snapshot


def _minute_state(row, now) -> tuple[int, int]:
    """Счётчики текущей минуты; устаревшее окно считается нулевым."""
    started = row.minute_started_at
    if not started or (now - started).total_seconds() >= 60:
        return 0, 0
    return int(row.minute_requests or 0), int(row.minute_tokens or 0)


def remaining(key_name: str, model: str, *, now=None) -> dict:
    """Сколько ещё можно этой парой. `None` в поле = лимит не объявлен."""
    now = now or timezone.now()
    budget = budget_for(model)
    if not budget:
        return {"rpd": None, "rpm": None, "tpm": None}
    return capacity_snapshot([key_name], [model], now=now).get(
        (str(key_name), str(model)),
        {"rpd": None, "rpm": None, "tpm": None},
    )


def has_capacity(key_name: str, model: str, *, now=None, snapshot=None) -> bool:
    left = (
        snapshot.get((str(key_name), str(model)), {})
        if isinstance(snapshot, dict)
        else remaining(key_name, model, now=now)
    )
    for field in ("rpd", "rpm"):
        value = left.get(field)
        if value is not None and value <= 0:
            return False
    return True


def try_reserve(key_name: str, model: str, *, now=None) -> bool:
    """Занять один запрос пары под ЭТОТ вызов.

    Атомарно, чтобы два потока демона не израсходовали один и тот же остаток:
    строка берётся `select_for_update` внутри транзакции. При любой проблеме с
    БД разрешаем вызов — сбой бухгалтерии не должен лишать клиента ответа.
    """
    now = now or timezone.now()
    budget = budget_for(model)
    if not budget:
        return True
    from management.models import GeminiModelQuotaUsage

    day = pacific_day(now)
    try:
        with transaction.atomic():
            GeminiModelQuotaUsage.objects.get_or_create(
                key_name=str(key_name), model=str(model), day_date=day
            )
            row = (
                GeminiModelQuotaUsage.objects.select_for_update()
                .filter(key_name=str(key_name), model=str(model), day_date=day)
                .first()
            )
            if row is None:
                return True
            rpd = int(budget.get("rpd") or 0)
            if rpd and int(row.requests or 0) >= rpd:
                return False
            minute_requests, _tokens = _minute_state(row, now)
            rpm = int(budget.get("rpm") or 0)
            if rpm and minute_requests >= rpm:
                return False
            fields = ["requests", "minute_requests", "updated_at"]
            row.requests = int(row.requests or 0) + 1
            if minute_requests == 0:
                row.minute_started_at = now
                row.minute_requests = 1
                row.minute_tokens = 0
                fields += ["minute_started_at", "minute_tokens"]
            else:
                row.minute_requests = minute_requests + 1
            row.save(update_fields=fields)
            return True
    except DatabaseError:
        return True


def settle(
    key_name: str,
    model: str,
    tokens: int,
    *,
    now=None,
    dispatch_at=None,
) -> None:
    """Дописать израсходованные токены к уже занятому запросу."""
    if not tokens:
        return
    now = now or timezone.now()
    dispatch_at = dispatch_at or now
    if not budget_for(model):
        return
    from management.models import GeminiModelQuotaUsage

    try:
        GeminiModelQuotaUsage.objects.filter(
            key_name=str(key_name),
            model=str(model),
            day_date=pacific_day(dispatch_at),
        ).update(
            tokens=F("tokens") + int(tokens),
            minute_tokens=F("minute_tokens") + int(tokens),
            updated_at=now,
        )
    except DatabaseError:
        return


def cancel_reservation(key_name: str, model: str, *, dispatch_at=None) -> None:
    """Return one local request reservation that never crossed provider I/O."""
    dispatch_at = dispatch_at or timezone.now()
    if not budget_for(model):
        return
    from management.models import GeminiModelQuotaUsage

    try:
        with transaction.atomic():
            row = (
                GeminiModelQuotaUsage.objects.select_for_update()
                .filter(
                    key_name=str(key_name),
                    model=str(model),
                    day_date=pacific_day(dispatch_at),
                )
                .first()
            )
            if row is None:
                return
            fields = ["updated_at"]
            if int(row.requests or 0) > 0:
                row.requests = int(row.requests) - 1
                fields.append("requests")
            minute_started = row.minute_started_at
            if (
                minute_started is not None
                and 0 <= (dispatch_at - minute_started).total_seconds() < 60
                and int(row.minute_requests or 0) > 0
            ):
                row.minute_requests = int(row.minute_requests) - 1
                fields.append("minute_requests")
            row.save(update_fields=fields)
    except DatabaseError:
        return


def order_keys_by_remaining(
    key_names,
    model: str,
    *,
    now=None,
    snapshot=None,
) -> list:
    """Ключи по остатку суточной квоты, от большего к меньшему.

    При 20 запросах в сутки важно РАСТЯГИВАТЬ бюджет по проектам, а не выжимать
    первый ключ до нуля: иначе всплеск сложных ходов упирается в лимит 5 RPM
    одного проекта, имея свободными остальные пять.
    """
    now = now or timezone.now()
    keys = list(key_names)
    if not isinstance(snapshot, dict):
        snapshot = capacity_snapshot(keys, [model], now=now)
    scored = []
    for index, key_name in enumerate(keys):
        left = snapshot.get((str(key_name), str(model)), {})
        rpd = left.get("rpd")
        scored.append((-(rpd if rpd is not None else 10**6), index, key_name))
    scored.sort()
    return [item[2] for item in scored]


def usage_snapshot(*, now=None) -> list:
    """Читаемый срез для админки: без единого запроса к провайдеру."""
    now = now or timezone.now()
    from management.models import GeminiModelQuotaUsage

    try:
        rows = list(
            GeminiModelQuotaUsage.objects.filter(
                day_date=pacific_day(now)
            ).order_by("model", "key_name")
        )
    except DatabaseError:
        return []
    snapshot = []
    for row in rows:
        budget = budget_for(row.model)
        minute_requests, minute_tokens = _minute_state(row, now)
        snapshot.append({
            "key_name": row.key_name,
            "model": row.model,
            "requests": int(row.requests or 0),
            "rpd": budget.get("rpd"),
            "tokens": int(row.tokens or 0),
            "minute_requests": minute_requests,
            "rpm": budget.get("rpm"),
            "minute_tokens": minute_tokens,
            "tpm": budget.get("tpm"),
        })
    return snapshot
