"""
ШІ-аналіз записів розмов (Binotel + Gemini).

Потік (синхронний — щоб на тестовій фазі бачити реальну швидкість):

  1. Апсерт CallRecord за generalCallID (авторитетні дані з stats/call-details,
     не довіряємо фронту). Матч клієнта за номером, менеджера — за internalNumber.
  2. Якщо вже є готовий аналіз і не force — повертаємо кеш.
  3. Тягнемо mp3 server-side через BinotelClient.fetch_record_stream (обхід
     15-хв посилання та mixed-content), читаємо в памʼять, перевіряємо розмір.
  4. Шлемо аудіо inline у Gemini generateContent з рубрикою оцінки у стилі
     Mosaic (адаптованою під один дзвінок) + опційний B2B-контекст менеджера.
     Просимо строгий JSON (responseMimeType=application/json).
  5. Парсимо, зберігаємо CallAIAnalysis (done) з метриками прогону.

Аудіо локально НЕ зберігається — лише структурований розбор та метрики.
Ключ Gemini — з ENV GEMINI_API (той самий, що використовує Instagram-бот),
модель для Instagram-чату за замовчуванням gemini-3.7-flash, із керованим
fallback для інших ролей. Бібліотека google.generativeai НЕ
потрібна — прямий REST-виклик (як у services/instagram_bot.py).
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import time
import uuid
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from management.models import (
    CallAIAnalysis,
    CallRecord,
    Client,
    InstagramBotSettings,
)
from management.models import normalize_phone as model_normalize_phone
from management.services import gemini_hedge, gemini_keys, gemini_quota, gemini_scoreboard
from management.services.call_ai_queue import METADATA_PENDING, analysis_queue_category
from management.services.binotel import (
    BinotelClient,
    BinotelError,
    BinotelNotConfigured,
    parse_webhook_call_details,
)

logger = logging.getLogger("binotel")

GENAI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Ланцюги моделей та пули ключів централізовані у services/gemini_keys.py
# (роль 'management' для аудіо/денного аудиту, 'chat' для бота, 'checker' для grounding).

# Inline-ліміт Gemini — 20 МБ на весь запит. Лишаємо запас на JSON/base64-оверхед
# (base64 додає ~33%), тож кап на сирий mp3 ставимо консервативно.
MAX_AUDIO_BYTES = 14 * 1024 * 1024

GEMINI_TIMEOUT = (10, 90)  # (connect, read) — аудіо-аналіз може йти десятки секунд
CHAT_TIMEOUT = (8, 25)      # діалоговий бот: коротка відповідь — не висимо на завислій моделі
MANAGEMENT_TEXT_TIMEOUT = (8, 25)  # допоміжні text-виклики IG-бота, не аудіо
BACKOFF_BASE = 2.0          # секунди, експоненційно між ретраями transient
BACKGROUND_LEASE_SAFETY_SECONDS = 1.0
BACKGROUND_MIN_CALL_SECONDS = 0.2
# Жорсткий стеля часу на ВЕСЬ перебір пулу для чату: краще швидко впасти у фолбек/
# ретрай повідомлення, ніж тримати клієнта (і чергу) у «processing» хвилинами.
CHAT_DEADLINE_SECONDS = 75.0
MANAGEMENT_TEXT_DEADLINE_SECONDS = 75.0

# Live chat has a separate SLA from the longer-running generic pool.  Keep a
# usable interval for a quality fallback; a tuple timeout is a *total* budget,
# because requests can consume both the connect and read portions.
CHAT_ORDINARY_DEADLINE_SECONDS = 35.0
CHAT_COMPLEX_DEADLINE_SECONDS = 45.0
CHAT_MIN_CALL_SECONDS = 2.0
# Live SLA permits at most two slow calls on the primary model before preserving
# the remaining deadline for the next model tier.  All six projects remain in
# the immutable candidate plan; uncalled primary candidates are recorded as
# ``sla_model_budget`` rather than silently disappearing.  The fallback tier is
# bounded separately for the same deadline/quota reason.
CHAT_PRIMARY_ATTEMPT_LIMIT = 2
CHAT_FALLBACK_ATTEMPT_LIMIT = 2
# The old hedge pre-leased an entire model pool and could classify already
# dispatched calls as not-attempted after an early winner.  Keep it disabled
# until the later permit/winner FSM owns real provider completion.
ENABLE_LEGACY_CHAT_HEDGE = False
CHAT_COMPLEX_TASKS = frozenset({
    "product_decision",
    "size_fit_decision",
    "catalog_match",
    "media_analysis",
    "payment_decision",
    "order_decision",
})

# This is an application policy, not a provider claim about token equivalence.
# Gemini 3.x consumes the level; Gemini 2.5 fallbacks consume the budget table.
REASONING_POLICY_VERSION = "2026-07-23.v1"
_REASONING_BUDGETS = {"minimal": 0, "low": 1024, "medium": 4096, "high": 8192}
_REASONING_POLICIES = {
    "health_probe": "low",
    "customer_chat": "low",
    "follow_cta_copy": "low",
    "ugc_evidence_assessment": "high",
    "product_decision": "high",
    "size_fit_decision": "high",
    "catalog_match": "high",
    "media_analysis": "high",
    "payment_decision": "high",
    "order_decision": "high",
    "customer_intelligence": "high",
    "conversion_analysis": "high",
    "conversation_reanalysis": "high",
    "memory_summary": "medium",
    "reporting_summary": "medium",
}
_REASONING_OUTPUT_CAPS = {
    "customer_chat": 1536,
    "follow_cta_copy": 256,
    "ugc_evidence_assessment": 2048,
    "product_decision": 4096,
    "size_fit_decision": 4096,
    "catalog_match": 4096,
    "media_analysis": 4096,
    "payment_decision": 4096,
    "order_decision": 4096,
}
_REASONING_THINKING_BUDGET_OVERRIDES = {
    # This task needs one short sentence. Disabling 2.5 fallback thinking keeps
    # its bounded output budget available for customer-facing text, while
    # Gemini 3.x still receives the quality-first ``low`` thinking level.
    "follow_cta_copy": 0,
}

_BOUNDED_PROVIDER_REASONS = frozenset({
    "API_KEY_INVALID",
    "INVALID_ARGUMENT",
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "NOT_FOUND",
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "INTERNAL",
})
_HTTP_CODE_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)


def reasoning_policy(task: str) -> dict:
    """Return the validated, versioned reasoning policy for a provider task."""
    key = str(task or "").strip().lower()
    if key not in _REASONING_POLICIES:
        raise ValueError(f"Unknown Gemini reasoning task: {key or '<empty>'}")
    level = _REASONING_POLICIES[key]
    return {
        "task": key,
        "level": level,
        "thinking_budget": _REASONING_THINKING_BUDGET_OVERRIDES.get(
            key, _REASONING_BUDGETS[level]
        ),
        "max_output_tokens": _REASONING_OUTPUT_CAPS.get(key),
        "policy_version": REASONING_POLICY_VERSION,
    }



class CallAIAnalysisError(Exception):
    """Помилка рівня ШІ-аналізу (конфіг/аудіо/Gemini)."""


class _GeminiTransient(Exception):
    """Тимчасова помилка (503/500/таймаут) — модель перевантажена, ретрай + глобальний overload-кеш."""


class _GeminiAdmissionRejected(Exception):
    """Canonical source/lane ownership changed before provider dispatch."""

    def __init__(self, message: str, *, reason: str = "stale_provider_boundary"):
        super().__init__(message)
        bounded = str(reason or "").strip().casefold()
        self.reason = (
            bounded
            if re.fullmatch(r"[a-z][a-z0-9_]{0,47}", bounded)
            else "stale_provider_boundary"
        )


class _GeminiDispatchBudgetExhausted(Exception):
    """Validated live request has no remaining actual provider dispatch."""

    def __init__(self, message: str, *, reason: str = "provider_dispatch_budget"):
        super().__init__(message)
        self.reason = str(reason or "provider_dispatch_budget")[:32]


class _GeminiResultInvalid(Exception):
    """Provider returned a parsed result rejected by application validation."""


class _GeminiEmpty(Exception):
    """Порожня відповідь (finishReason=STOP/MAX_TOKENS без тексту) — проблема цього
    конкретного запиту (мало вихідних токенів через thinking), НЕ перевантаження
    моделі. Ретраїмо ту саму комбінацію, але НЕ метимо модель глобально overloaded."""


class _Gemini429(Exception):
    """Typed 429 quota/rate evidence with a provider-scoped retry boundary."""

    def __init__(
        self,
        message: str = "RESOURCE_EXHAUSTED",
        *,
        scope: str = "",
        retry_after_seconds: int = 0,
        provider_reason: str = "RESOURCE_EXHAUSTED",
        provider_quota_metric: str = "",
        provider_quota_id: str = "",
        provider_quota_dimensions: dict | None = None,
    ):
        super().__init__(message)
        self.scope = scope if scope in {"minute", "day", "topup", "unknown"} else ""
        self.retry_after_seconds = max(0, int(retry_after_seconds or 0))
        self.provider_reason = str(provider_reason or "RESOURCE_EXHAUSTED")[:80]
        self.http_code = 429
        self.provider_quota_metric = str(provider_quota_metric or "")[:16]
        self.provider_quota_id = str(provider_quota_id or "")[:120]
        self.provider_quota_dimensions = (
            dict(provider_quota_dimensions)
            if isinstance(provider_quota_dimensions, dict)
            else {}
        )


def _quota_scope_and_retry(exc: Exception) -> tuple[str, int]:
    """Return typed quota scope, retaining compatibility with older callers."""
    scope = str(getattr(exc, "scope", "") or "")
    seconds = max(0, int(getattr(exc, "retry_after_seconds", 0) or 0))
    if scope in {"minute", "day", "topup", "unknown"}:
        if not seconds and scope == "minute":
            seconds = gemini_keys.DEFAULT_MINUTE_COOLDOWN
        elif not seconds and scope == "topup":
            seconds = gemini_keys.TOPUP_COOLDOWN_SECONDS
        return scope, seconds
    return gemini_keys.parse_429(str(exc))


class _GeminiModelUnavailable(Exception):
    """Модель недоступна на цьому проекті (404/403) — перейти до наступної моделі."""


class _GeminiFatal(Exception):
    """Невиправна помилка запиту (400 — проблема у нашому payload). Зупиняємось."""


# ---------------------------------------------------------------------------
# Рубрика оцінки (system_instruction)
# ---------------------------------------------------------------------------
# Осі — дух Mosaic, перекладений на один дзвінок. Кожна 0..100, ваги в сумі = 1.0.
RUBRIC_AXES = [
    {"key": "rapport", "title": "Встановлення контакту", "weight": 0.15},
    {"key": "needs_discovery", "title": "Виявлення потреб", "weight": 0.25},
    {"key": "value_presentation", "title": "Презентація рішення та цінності", "weight": 0.20},
    {"key": "objection_handling", "title": "Робота із запереченнями", "weight": 0.15},
    {"key": "closing_next_step", "title": "Закриття / наступний крок", "weight": 0.15},
    {"key": "communication_quality", "title": "Якість комунікації", "weight": 0.10},
]


def _build_system_instruction() -> str:
    axes_lines = "\n".join(
        f"  - {a['key']} ({a['title']}, вага {a['weight']})" for a in RUBRIC_AXES
    )
    return (
        "Ти — досвідчений керівник відділу продажів та QA-аналітик дзвінків бренду "
        "TwoComms (B2B-продаж одягу мілітарі/стрітстайл оптовим клієнтам та магазинам). "
        "Тобі дають АУДІОЗАПИС реальної телефонної розмови менеджера з клієнтом. "
        "Повністю прослухай розмову від початку до кінця, зроби транскрипт із розміткою "
        "ролей (Менеджер / Клієнт), зрозумій, про якого клієнта йдеться і чого він хоче.\n\n"
        "Оціни роботу МЕНЕДЖЕРА за такими осями (кожна 0..100):\n"
        f"{axes_lines}\n\n"
        "Загальний бал overall_score (0..100) — зважена сума осей за вказаними вагами.\n"
        "verdict: 'pass' (>=75 і немає грубих провалів), 'coaching' (50..74 або є що "
        "підтягнути), 'fail' (<50 або критичні помилки).\n\n"
        "ВАЖЛИВО — це ВИХІДНИЙ холодний дзвінок. Клієнт вважається 'мертвим' "
        "(conversion_intent='dead') ЛИШЕ якщо він прямо відмовив назавжди, це не та "
        "людина/не той профіль, або товар йому категорично не потрібен. Якщо є будь-який "
        "шанс (думає, дорого, зайнятий, попросив передзвонити, не підняв) — це "
        "'needs_followup', клієнта треба дотискати. 'convert' — готовий до замовлення/оплати.\n\n"
        "Тобі також дають СНІМОК того, що менеджер зафіксував у CRM після дзвінка "
        "(результат, час наступного дзвінка, XML, нотатка). Порівняй його з РЕАЛЬНОЮ "
        "розмовою і знайди розбіжності (discrepancies): напр. домовились на завтра 12:00, "
        "а менеджер поставив інший час/дату; клієнт просив передзвонити, а менеджер "
        "закрив як неконверсійного; менеджер позначив XML підключеним, хоча в розмові "
        "цього не було. Час домовленостей став відносним ('завтра','післязавтра') — "
        "розв'яжи відносно дати дзвінка, яку тобі дано, і поверни ISO (YYYY-MM-DDTHH:MM).\n\n"
        "Будь конкретним і спирайся на фрази з розмови. Якщо аудіо нерозбірливе або "
        "розмови фактично немає (автовідповідач, тиша, гудки) — постав низькі бали, "
        "познач це у summary і поверни verdict 'fail'.\n\n"
        "Відповідай СУВОРО валідним JSON (без markdown, без ```), українською, за схемою:\n"
        "{\n"
        '  "client_identification": "хто клієнт і що йому треба (1-3 речення)",\n'
        '  "summary": "стисле резюме розмови (3-6 речень)",\n'
        '  "transcript": "повний транскрипт з ролями Менеджер:/Клієнт:",\n'
        '  "overall_score": <number 0..100>,\n'
        '  "verdict": "pass|coaching|fail",\n'
        '  "axes": [ {"key": "<ключ осі>", "title": "<назва>", "score": <0..100>, '
        '"comment": "обґрунтування з прикладами"} ],\n'
        '  "discussed_well": ["що менеджер зробив добре", "..."],\n'
        '  "missed_topics": ["важливі речі/потреби, які НЕ обговорили", "..."],\n'
        '  "recommendations": ["конкретна порада менеджеру", "..."],\n'
        '  "extracted_facts": {\n'
        '     "agreed_next_contact_iso": "YYYY-MM-DDTHH:MM або null",\n'
        '     "agreed_next_contact_text": "як про це домовились словами, або null",\n'
        '     "conversion_intent": "convert|needs_followup|dead",\n'
        '     "conversion_intent_reason": "чому саме так",\n'
        '     "xml_connected": true|false|null,\n'
        '     "payment_agreed": true|false|null\n'
        "  },\n"
        '  "discrepancies": [ {"field": "next_call|conversion|xml|other", '
        '"manager_value": "що зберіг менеджер", "ai_value": "що було насправді", '
        '"severity": "info|warn|high", "note": "пояснення", "quote": "цитата з розмови"} ]\n'
        "}\n"
        "Якщо снімку CRM немає або розбіжностей немає — поверни discrepancies як []."
    )


def _resolve_gemini_key() -> str:
    """Ключ Gemini: ENV GEMINI_API (як в Instagram-боті) або settings.
    Використовується лише для перевірки наявності хоч якогось ключа (day_report_audit).
    Реальний підбір ключа/моделі — у services/gemini_keys.py."""
    key = (os.environ.get("GEMINI_API", "") or "").strip()
    if not key:
        key = (getattr(settings, "GEMINI_API_KEY", "") or "").strip()
    return key


# ---------------------------------------------------------------------------
# CallRecord upsert
# ---------------------------------------------------------------------------
def _match_client(external_number: str):
    norm = model_normalize_phone(external_number or "")
    if not norm:
        return None
    client = Client.objects.filter(phone_normalized=norm).order_by("id").first()
    if client:
        return client
    last7 = norm[-7:] if len(norm) >= 7 else norm
    if last7:
        return Client.objects.filter(phone_last7=last7).order_by("id").first()
    return None


def _resolve_manager(client: BinotelClient, internal_number: str):
    """internalNumber -> email співробітника Binotel -> Django User. Best-effort."""
    internal_number = (str(internal_number or "")).strip()
    if not internal_number:
        return None
    try:
        data = client.list_of_employees()
    except BinotelError:
        return None
    email = ""
    for _key, emp in (data.get("listOfEmployees") or {}).items():
        endpoint = emp.get("endpointData") or {}
        if str(endpoint.get("internalNumber") or "") == internal_number:
            email = (emp.get("email") or "").strip()
            break
    if not email:
        return None
    User = get_user_model()
    return User.objects.filter(email__iexact=email).order_by("id").first()


def upsert_call_record(client: BinotelClient, general_call_id: str) -> CallRecord:
    """Створює/оновлює CallRecord за авторитетними даними stats/call-details."""
    gcid = (str(general_call_id or "")).strip()
    if not gcid:
        raise CallAIAnalysisError("Потрібен generalCallID.")

    parsed = {}
    try:
        data = client.call_details(gcid)
        details = data.get("callDetails")
        if isinstance(details, dict):
            # call-details повертає {generalCallID: {...}}
            entry = details.get(gcid) or details.get(str(gcid))
            if isinstance(entry, dict):
                parsed = parse_webhook_call_details(entry)
    except BinotelError as exc:
        logger.info("call-ai: call-details failed for %s: %s", gcid, exc)

    started_at = None
    if parsed.get("start_time"):
        try:
            started_at = timezone.datetime.fromtimestamp(
                int(parsed["start_time"]), tz=timezone.get_current_timezone()
            )
        except (TypeError, ValueError, OSError):
            started_at = None

    matched_client = _match_client(parsed.get("external_number") or "")
    manager = _resolve_manager(client, parsed.get("internal_number") or "")

    # Зберігаємо вже точно привʼязані значення (з CallSession через webhook):
    # матч по номеру може вказати на іншого клієнта зі спільним номером/фазою,
    # тож НЕ перезаписуємо те, що вже встановлено.
    existing = CallRecord.objects.filter(provider="binotel", external_call_id=gcid).first()
    if existing and existing.matched_client_id:
        matched_client = None  # не чіпати наявну привʼязку
    if existing and existing.manager_id:
        manager = None

    defaults = {
        "phone": parsed.get("external_number") or "",
        "direction": parsed.get("direction") or CallRecord.Direction.UNKNOWN,
        "duration_seconds": int(parsed.get("bill_seconds") or 0),
    }
    if started_at:
        defaults["started_at"] = started_at
    if matched_client:
        defaults["matched_client"] = matched_client
    if manager:
        defaults["manager"] = manager
    if parsed:
        defaults["payload"] = parsed

    record, _created = CallRecord.objects.update_or_create(
        provider="binotel",
        external_call_id=gcid,
        defaults=defaults,
    )
    return record


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def _payload_for_model(
    model: str, payload: dict, *, reasoning_task: str = "customer_chat"
) -> dict:
    """Normalize model-specific generation settings without mutating the caller."""
    policy = reasoning_policy(reasoning_task)
    normalized = copy.deepcopy(payload)
    generation = normalized.get("generationConfig")
    if not isinstance(generation, dict):
        generation = {}
        normalized["generationConfig"] = generation
    thinking = generation.get("thinkingConfig")
    if not isinstance(thinking, dict):
        thinking = {}
        generation["thinkingConfig"] = thinking
    if policy["max_output_tokens"] is not None:
        generation["maxOutputTokens"] = policy["max_output_tokens"]
    if str(model or "").startswith("gemini-3"):
        # Gemini 3.x documents thinkingLevel; sending thinkingBudget can turn a
        # valid health/chat request into HTTP 400. Gemini 3 reasoning is also
        # optimized for provider-default sampling; explicit low temperature can
        # degrade complex answers or cause loops.
        for sampling_key in ("temperature", "topP", "topK", "top_p", "top_k"):
            generation.pop(sampling_key, None)
        thinking.pop("thinkingBudget", None)
        thinking["thinkingLevel"] = policy["level"]
    elif str(model or "").startswith("gemini-2.5"):
        # This numeric mapping is our versioned fallback policy, not an official
        # Google equivalence. Never send both controls to a Gemini 2.5 model.
        thinking.pop("thinkingLevel", None)
        thinking["thinkingBudget"] = policy["thinking_budget"]
    return normalized


def _bounded_pool_timeout(
    configured: tuple,
    *,
    remaining_deadline: float | None,
    tracked_key: bool,
    role: str = "",
) -> tuple[float, float] | None:
    """Fit connect+read inside the job deadline and project-key lease."""
    allowance = float(sum(configured))
    if remaining_deadline is not None:
        allowance = min(allowance, max(0.0, float(remaining_deadline)))
    if tracked_key:
        # Лізинг залежить від ролі: аудіо-аналіз має довший, інакше його
        # налаштований timeout (10, 90) обрізався б спільним чатовим лізингом
        # до 62 с read — тобто налаштування виглядало діючим і не діяло.
        allowance = min(
            allowance,
            float(gemini_keys.lease_seconds_for(role)) - BACKGROUND_LEASE_SAFETY_SECONDS,
        )
    if allowance < BACKGROUND_MIN_CALL_SECONDS:
        return None
    configured_total = max(float(sum(configured)), 0.001)
    connect = min(float(configured[0]), allowance * float(configured[0]) / configured_total)
    connect = max(0.05, connect)
    read = allowance - connect
    if read <= 0:
        return None
    return connect, read


def _deadline_sleep(delay: float, *, deadline: float | None) -> bool:
    """Sleep no longer than the remaining overall budget."""
    bounded = max(0.0, float(delay))
    if deadline is not None:
        bounded = min(bounded, max(0.0, deadline - time.monotonic()))
    if bounded <= 0:
        return False
    time.sleep(bounded)
    return True


def _call_combo(key_name: str, key_value: str, model: str, payload: dict,
                n_attempts: int, grounded: bool, log: list, parse: bool = True,
                timeout: tuple | None = None, log_cb=None, *, role: str,
                deadline: float | None, accounting_observer=None,
                candidate_index: int = 0) -> tuple[str, dict | None]:
    """Один (key, model) кандидат із ретраями на transient.

    Повертає ('ok', result) | ('key_429', None) | ('model_skip', None).
    Кидає CallAIAnalysisError на 400 (fatal). Веде облік стану ключа/моделі.
    log_cb (опц.) отримує короткий рядок про КОЖНУ спробу (для консолі бота).
    """
    track = key_name in gemini_keys.ALL_KEYS

    def _emit(msg: str):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    for attempt in range(n_attempts):
        if deadline is not None and time.monotonic() >= deadline:
            if accounting_observer is not None:
                accounting_observer.record_not_attempted(
                    key_name=key_name,
                    model=model,
                    candidate_index=candidate_index,
                    reason="deadline",
                )
            return ("model_skip", None)
        t0 = time.monotonic()
        lease_token = None
        retry_delay = None
        legacy_quota_reserved = False
        quota_dispatch_at = None
        try:
            policy = reasoning_policy(payload.get("_reasoning_task", "customer_chat"))
            request_payload = _payload_for_model(
                model, payload, reasoning_task=policy["task"]
            )
            request_payload.pop("_reasoning_task", None)
            if track:
                lease_token = gemini_keys.acquire_key_lease(key_name, role=role)
                if not lease_token:
                    log.append(f"{key_name}/{model}: lease_busy")
                    _emit(f"{key_name}/{model}: lease busy")
                    if accounting_observer is not None:
                        accounting_observer.record_not_attempted(
                            key_name=key_name,
                            model=model,
                            candidate_index=candidate_index,
                            reason="lease_busy",
                        )
                    return ("model_skip", None)
            remaining = None if deadline is None else deadline - time.monotonic()
            effective_timeout = _bounded_pool_timeout(
                timeout or (CHAT_TIMEOUT if role == "chat" else GEMINI_TIMEOUT),
                remaining_deadline=remaining,
                tracked_key=track,
                role=role,
            )
            if effective_timeout is None:
                if accounting_observer is not None:
                    accounting_observer.record_not_attempted(
                        key_name=key_name,
                        model=model,
                        candidate_index=candidate_index,
                        reason="deadline",
                    )
                return ("model_skip", None)
            attempt_boundary = (
                accounting_observer.attempt(
                    key_name=key_name,
                    model=model,
                    candidate_index=candidate_index,
                )
                if accounting_observer is not None
                else None
            )
            if (
                attempt_boundary is not None
                and attempt_boundary.validate_ownership() is not True
            ):
                raise CallAIAnalysisError(
                    "Gemini provider dispatch rejected: stale request ownership."
                )
            # ЭБ.4: занимаем запрос пары (ключ, модель) ДО вызова. Иначе два
            # потока демона израсходуют один и тот же последний из двадцати.
            quota_dispatch_at = timezone.now()
            if key_name in gemini_keys.ALL_KEYS and not gemini_quota.try_reserve(
                key_name, model, now=quota_dispatch_at
            ):
                log.append(f"{key_name}/{model}: денна квота пари вичерпана (облік)")
                _emit(f"{key_name}/{model}: 🚫 квота пари вичерпана (локальний облік)")
                if accounting_observer is not None:
                    accounting_observer.record_not_attempted(
                        key_name=key_name,
                        model=model,
                        candidate_index=candidate_index,
                        reason="quota_exhausted",
                    )
                return ("model_skip", None)
            if key_name in gemini_keys.ALL_KEYS:
                legacy_quota_reserved = True
            call_kwargs = {"parse": parse, "timeout": effective_timeout}
            if attempt_boundary is not None:
                call_kwargs["attempt_boundary"] = attempt_boundary
            parsed, usage = _gemini_call_once(
                model, request_payload, key_value, **call_kwargs
            )
            if key_name in gemini_keys.ALL_KEYS:
                gemini_quota.settle(
                    key_name,
                    model,
                    int((usage or {}).get("totalTokenCount") or 0),
                    dispatch_at=quota_dispatch_at,
                )
        except _GeminiAdmissionRejected as exc:
            if legacy_quota_reserved:
                gemini_quota.cancel_reservation(
                    key_name,
                    model,
                    dispatch_at=quota_dispatch_at,
                )
            raise CallAIAnalysisError(
                "Gemini provider dispatch rejected: stale request ownership."
            ) from exc
        except _GeminiTransient as exc:
            dt = time.monotonic() - t0
            log.append(f"{key_name}/{model}: transient {exc} (#{attempt + 1})")
            text = str(exc).lower()
            if text.startswith("timeout:"):
                kind = "read timeout"
            elif text.startswith("transport:"):
                kind = "transport error"
            else:
                kind = "HTTP 5xx"
                gemini_keys.mark_model_overloaded(model)
            _emit(f"{key_name}/{model}: ⚠ {kind} ({dt:.1f}с) → інша модель")
            if attempt < n_attempts - 1:
                retry_delay = BACKOFF_BASE * (2 ** attempt)
        except _GeminiEmpty as exc:
            dt = time.monotonic() - t0
            log.append(f"{key_name}/{model}: empty {exc} (#{attempt + 1})")
            _emit(f"{key_name}/{model}: ⚠ порожня відповідь ({dt:.1f}с)")
            if attempt < n_attempts - 1:
                retry_delay = BACKOFF_BASE
        except _Gemini429 as exc:
            dt = time.monotonic() - t0
            if gemini_keys.is_key_level_429(model, grounded):
                if track:
                    scope, secs = _quota_scope_and_retry(exc)
                    gemini_keys.mark_429(
                        key_name, scope, secs, error=str(exc), model=model
                    )
                    log.append(f"{key_name}/{model}: 429 → кулдаун ключа ({scope})")
                    _emit(f"{key_name}/{model}: 🚫 429 квота → кулдаун ключа ({scope}, {dt:.1f}с)")
                else:
                    log.append(f"{key_name}/{model}: 429 (ручний ключ)")
                    _emit(f"{key_name}/{model}: 🚫 429 (ручний ключ, {dt:.1f}с)")
                return ("key_429", None)
            log.append(f"{key_name}/{model}: 429 → модель не free, skip")
            _emit(f"{key_name}/{model}: 🚫 429 (модель платна) → скіп моделі ({dt:.1f}с)")
            gemini_keys.mark_model_unavailable(model)
            return ("model_skip", None)
        except _GeminiModelUnavailable as exc:
            dt = time.monotonic() - t0
            log.append(f"{key_name}/{model}: unavailable {exc}")
            _emit(f"{key_name}/{model}: ⚠ недоступна ({dt:.1f}с) → інша модель")
            return ("model_skip", None)
        except _GeminiFatal as exc:
            _emit(f"{key_name}/{model}: ❌ фатальна помилка запиту")
            if accounting_observer is not None:
                accounting_observer.resolve_failure("fatal_payload")
            raise CallAIAnalysisError(f"Помилка запиту до Gemini: {exc}")
        else:
            dt = time.monotonic() - t0
            if track:
                gemini_keys.mark_success(key_name)
            log.append(f"{key_name}/{model}: ok")
            _emit(f"{key_name}/{model}: ✅ відповідь за {dt:.1f}с")
            usage = usage if isinstance(usage, dict) else {}
            result = {
                "parsed": parsed, "raw": parsed, "usage": usage, "model": model,
                "meta": {
                    "key": key_name,
                    "used_model": model,
                    "attempts": list(log),
                    "reasoning_task": policy["task"],
                    "reasoning_level": policy["level"],
                    "reasoning_budget": policy["thinking_budget"],
                    "reasoning_policy_version": policy["policy_version"],
                    "finish_reason": str(usage.get("_finish_reason") or "")[:32],
                    "thoughts_tokens": int(
                        usage.get("thoughtsTokenCount")
                        or usage.get("thoughts_token_count")
                        or 0
                    ),
                    "candidates_tokens": int(
                        usage.get("candidatesTokenCount")
                        or usage.get("candidates_token_count")
                        or 0
                    ),
                    "latency_ms": max(0, int((time.monotonic() - t0) * 1000)),
                },
            }
            if accounting_observer is not None:
                result["meta"]["request_id"] = accounting_observer.request_id
            return ("ok", result)
        finally:
            if lease_token:
                try:
                    gemini_keys.release_key_lease(key_name, lease_token)
                except Exception:
                    logger.warning(
                        "gemini background key lease release failed", exc_info=True
                    )
        if retry_delay is not None:
            _deadline_sleep(retry_delay, deadline=deadline)
    return ("model_skip", None)  # transient вичерпано


def _run_with_pool(role: str, payload: dict, *, manual_key: str | None = None,
                   grounded: bool = False, parse: bool = True,
                   timeout: tuple | None = None, deadline_seconds: float | None = None,
                   log_cb=None, model_override: str | None = None,
                   reasoning_task: str | None = None) -> dict:
    """Прогоняє payload через пул ключів ролі та цепочку моделей.

    Кругова стратегія: у кожному КРУЗІ — ручний ключ (якщо є) першим, далі весь
    пул (own→borrow, sticky-впорядкований) по одному виклику на (key,model).
    Якщо круг не дав 200 — експоненційна затримка (2с,4с,...) + скидання
    overload-кешу моделей, і новий круг. Усього до max_rounds(role) кругів
    (чат=3), тільки потім помилка.

    deadline_seconds: жорстка стеля часу на весь перебір (None → CHAT_DEADLINE для
    ролі chat, інакше без стелі). Захищає від багатохвилинних зависань.
    log_cb: колбек, що отримує короткі рядки про кожну спробу (для консолі бота).
    """
    task = reasoning_task or ("customer_chat" if role == "chat" else "reporting_summary")
    policy = reasoning_policy(task)
    payload = copy.deepcopy(payload)
    manual_key = str(manual_key or "").strip() or None
    if manual_key and not gemini_keys.manual_key_allowed(role, manual_key):
        manual_key = None
    if manual_key and gemini_keys.configured_alias_for_secret(manual_key):
        # A configured credential already participates in the project pool.
        # Calling it once as ``manual`` and again through its alias would spend
        # the same project quota twice under different labels.
        manual_key = None
    payload["_reasoning_task"] = policy["task"]
    log: list[str] = []
    n_attempts = gemini_keys.attempts_per_model(role)
    rounds = gemini_keys.max_rounds(role)
    models = gemini_keys.task_model_chain(role, task, model_override)
    call_timeout = timeout or (CHAT_TIMEOUT if role == "chat" else GEMINI_TIMEOUT)
    if deadline_seconds is None:
        deadline_seconds = CHAT_DEADLINE_SECONDS if role == "chat" else None
    t_start = time.monotonic()
    deadline = None if deadline_seconds is None else t_start + deadline_seconds
    # One ambiguous provider boundary is conservative spend.  Rotate durable
    # analysis to the next project rather than retrying the same project/model
    # in another inner attempt or round.  This policy is deliberately
    # independent from accounting mode so shadow never changes execution.
    single_boundary_rotation = bool(
        role != "chat"
        and getattr(
            settings,
            "IG_GEMINI_ANALYSIS_SINGLE_BOUNDARY_ROTATION",
            True,
        )
    )
    if single_boundary_rotation:
        n_attempts = 1
        rounds = 1
    frozen_candidate_rows = (
        list(gemini_keys.iter_attempts(
            role,
            model_chain_override=models,
            include_skipped=True,
        ))
        if single_boundary_rotation
        else None
    )
    frozen_execution_candidates = (
        [
            tuple(row[:3])
            for row in frozen_candidate_rows
            if len(row) < 4 or not row[3]
        ]
        if frozen_candidate_rows is not None
        else None
    )

    accounting_observer = None
    planning_candidates = []
    accounting_ownership_blocked = False
    unsafe_shadow_retry_configuration = False
    try:
        from management.services import gemini_accounting_runtime

        if gemini_accounting_runtime.shadow_runtime_active():
            unsafe_shadow_retry_configuration = not single_boundary_rotation
            planning_candidates = list(
                frozen_candidate_rows
                if frozen_candidate_rows is not None
                else gemini_keys.iter_attempts(
                    role,
                    model_chain_override=models,
                    include_skipped=True,
                )
            )
            if not unsafe_shadow_retry_configuration:
                accounting_plan = gemini_accounting_runtime.generic_candidate_plan(
                    role=role,
                    models=models,
                    manual_key=manual_key,
                    execution_candidates=planning_candidates,
                )
                accounting_observer = gemini_accounting_runtime.begin_request(
                    request_id=None,
                    role=role,
                    reasoning_task=task,
                    candidate_plan=accounting_plan,
                    deadline_seconds=deadline_seconds,
                )
                accounting_ownership_blocked = bool(
                    getattr(accounting_observer, "provider_blocked", False)
                )
                if not getattr(accounting_observer, "enabled", False):
                    accounting_observer = None
    except Exception:
        accounting_observer = None
    if unsafe_shadow_retry_configuration:
        raise CallAIAnalysisError(
            "Gemini shadow requires single-boundary background rotation."
        )
    if accounting_ownership_blocked:
        raise CallAIAnalysisError(
            "Gemini provider dispatch rejected: source/lane already owned."
        )
    def _over_deadline() -> bool:
        return deadline_seconds is not None and (time.monotonic() - t_start) >= deadline_seconds

    def _emit(msg: str):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    def _call_graph_owned(*args, **kwargs):
        """Terminalize the owned graph before propagating any gateway exit."""
        try:
            return _call_combo(*args, **kwargs)
        except Exception as exc:
            if accounting_observer is not None:
                detail = str(exc).casefold()
                reason = (
                    "ownership_conflict"
                    if "ownership" in detail or "stale request" in detail
                    else "gateway_exception"
                )
                accounting_observer.resolve_failure(reason)
            raise

    aborted = False
    for round_idx in range(rounds):
        if _over_deadline():
            aborted = True
            break
        if rounds > 1:
            _emit(f"коло {round_idx + 1}/{rounds} (моделі: {', '.join(models)})")

        attempted_this_round = False
        if manual_key:
            for model in models:
                if _over_deadline():
                    aborted = True
                    break
                if gemini_keys.is_model_overloaded(model):
                    if accounting_observer is not None:
                        accounting_observer.record_not_attempted(
                            key_name="(manual)",
                            model=model,
                            candidate_index=accounting_observer.candidate_index(
                                "(manual)", model
                            ),
                            reason="model_overload",
                        )
                    continue
                attempted_this_round = True
                status, res = _call_graph_owned(
                    "(manual)", manual_key, model, payload,
                    n_attempts, grounded, log, parse, call_timeout,
                    log_cb, role=role, deadline=deadline,
                    accounting_observer=accounting_observer,
                    candidate_index=(
                        accounting_observer.candidate_index("(manual)", model)
                        if accounting_observer is not None else 0
                    ),
                )
                if status == "ok":
                    return res
                if _over_deadline():
                    aborted = True
                    break
                if status == "key_429":
                    break
            if aborted:
                break

        # Single-boundary mode freezes one execution snapshot for both off and
        # shadow.  Shadow persists that exact order; it never performs a second
        # availability read that could drift between plan and provider I/O.
        # Legacy rollback (off + flag false) keeps the per-round iterator.
        seen_fingerprints_by_model: dict[str, set[bytes]] = {}
        seen_projects_by_model: dict[str, set[str]] = {}
        execution_candidates = (
            frozen_execution_candidates
            if frozen_execution_candidates is not None
            else gemini_keys.iter_attempts(role, model_chain_override=models)
        )
        for key_name, key_value, model in execution_candidates:
            if _over_deadline():
                aborted = True
                break
            fingerprint = gemini_keys.credential_fingerprint(key_value)
            project_identity = gemini_keys.project_group(key_name)
            seen_fingerprints = seen_fingerprints_by_model.setdefault(model, set())
            seen_projects = seen_projects_by_model.setdefault(model, set())
            duplicate = bool(fingerprint and fingerprint in seen_fingerprints) or bool(
                project_identity and project_identity in seen_projects
            )
            if duplicate:
                if accounting_observer is not None:
                    accounting_observer.record_not_attempted(
                        key_name=key_name,
                        model=model,
                        candidate_index=accounting_observer.candidate_index(
                            key_name,
                            model,
                        ),
                        reason="duplicate",
                    )
                log.append(f"{key_name}/{model}: duplicate project credential")
                _emit(f"{key_name}/{model}: duplicate project credential → skip")
                continue
            if fingerprint:
                seen_fingerprints.add(fingerprint)
            if project_identity:
                seen_projects.add(project_identity)
            attempted_this_round = True
            status, res = _call_graph_owned(
                key_name, key_value, model, payload,
                n_attempts, grounded, log, parse, call_timeout,
                log_cb, role=role, deadline=deadline,
                accounting_observer=accounting_observer,
                candidate_index=(
                    accounting_observer.candidate_index(key_name, model)
                    if accounting_observer is not None else 0
                ),
            )
            if status == "ok":
                return res
            if _over_deadline():
                aborted = True
                break
        if aborted:
            break

        # A previous 429 may have put every alias into cooldown. Sleeping and
        # starting another empty round only delays the deterministic fallback.
        if not attempted_this_round:
            break

        if round_idx < rounds - 1:
            gemini_keys.clear_model_overload()
            if not _deadline_sleep(
                gemini_keys.ROUND_BACKOFF_BASE * (2 ** round_idx),
                deadline=deadline,
            ) and deadline is not None:
                aborted = True
                break

    if aborted:
        if accounting_observer is not None:
            accounting_observer.resolve_failure("deadline")
        _emit(f"⏱ дедлайн {deadline_seconds:.0f}с вичерпано — припиняю перебір")
        raise CallAIAnalysisError(
            f"Перебір Gemini перервано по дедлайну ({deadline_seconds:.0f}с). Спроби: "
            + "; ".join(log)
        )
    if accounting_observer is not None:
        accounting_observer.resolve_failure("exhausted")
    raise CallAIAnalysisError(
        "Усі ключі/моделі Gemini недоступні (квота/перевантаження). Спроби: "
        + "; ".join(log)
    )


def _chat_deadline_seconds(reasoning_task: str) -> float:
    return (
        CHAT_COMPLEX_DEADLINE_SECONDS
        if reasoning_task in CHAT_COMPLEX_TASKS
        else CHAT_ORDINARY_DEADLINE_SECONDS
    )


def _chat_timeout(remaining: float, *, preserve_fallback: bool) -> tuple[float, float] | None:
    """Return a connect/read tuple whose *sum* fits the live SLA."""
    allowance = remaining - (CHAT_MIN_CALL_SECONDS if preserve_fallback else 0.0)
    if allowance < CHAT_MIN_CALL_SECONDS:
        return None
    # Halve the available primary phase per attempt. This prevents the first
    # request from consuming the complete strong-model phase on a read timeout.
    if preserve_fallback:
        allowance /= 2.0
    allowance = min(float(sum(CHAT_TIMEOUT)), allowance)
    connect = min(float(CHAT_TIMEOUT[0]), max(0.25, allowance * 0.2))
    read = allowance - connect
    if read <= 0:
        return None
    return (connect, read)


def _chat_key_failure(exc: Exception) -> bool:
    detail = str(exc).upper()
    return "API_KEY_INVALID" in detail or "HTTP 401" in detail


def _bounded_provider_reason(exc: Exception) -> str:
    """Keep only a known provider classification out of a typed error string."""
    typed_reason = str(getattr(exc, "provider_reason", "") or "").upper()
    if typed_reason in _BOUNDED_PROVIDER_REASONS:
        return typed_reason
    detail = str(exc).upper()
    for reason in _BOUNDED_PROVIDER_REASONS:
        if reason in detail:
            return reason
    return ""


def _transient_failure_details(exc: Exception) -> tuple[str, int | None]:
    """Classify transport/HTTP transient errors without retaining provider text."""
    detail = str(exc)
    match = _HTTP_CODE_RE.search(detail)
    if match:
        code = int(match.group(1))
        if code == 408:
            return "http_408", code
        if 500 <= code < 600:
            return "http_5xx", code
    if detail.lower().startswith("timeout:"):
        return "read_timeout", None
    return "transport", None


def _chat_result(*, parsed, usage: dict | None, model: str, key_name: str,
                 attempts: list[str], policy: dict, started_at: float) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    return {
        "parsed": parsed,
        "raw": parsed,
        "usage": usage,
        "model": model,
        "meta": {
            "key": key_name,
            "used_model": model,
            "attempts": list(attempts),
            "reasoning_task": policy["task"],
            "reasoning_level": policy["level"],
            "reasoning_budget": policy["thinking_budget"],
            "reasoning_policy_version": policy["policy_version"],
            "finish_reason": str(usage.get("_finish_reason") or "")[:32],
            "thoughts_tokens": int(
                usage.get("thoughtsTokenCount") or usage.get("thoughts_token_count") or 0
            ),
            "candidates_tokens": int(
                usage.get("candidatesTokenCount") or usage.get("candidates_token_count") or 0
            ),
            "latency_ms": max(0, int((time.monotonic() - started_at) * 1000)),
        },
    }


def _classify_hedge_error(exc: BaseException) -> tuple:
    """Перекласти виключення hedged-виклику в (failure_kind, http_code, decision).

    Класифікація живе тут, а не в потоці: усі записи в БД і зміни стану ключів
    робляться з головного потоку, бо Django-конекшени не поділяються між потоками.
    """
    if isinstance(exc, _GeminiTransient):
        kind, http_code = _transient_failure_details(exc)
        return kind, http_code, "degrade_model"
    if isinstance(exc, _GeminiEmpty):
        return "empty", None, "retry_or_degrade"
    if isinstance(exc, _Gemini429):
        return "quota_429", 429, "cooldown_project"
    if isinstance(exc, _GeminiModelUnavailable):
        kind = "model_not_found" if "404" in str(exc) else "permission_denied"
        return kind, 404 if kind == "model_not_found" else 403, (
            "skip_model" if kind == "model_not_found" else "rotate_key"
        )
    if isinstance(exc, _GeminiFatal):
        kind = "invalid_key" if _chat_key_failure(exc) else "invalid_payload"
        match = _HTTP_CODE_RE.search(str(exc))
        code = int(match.group(1)) if match else (401 if kind == "invalid_key" else 400)
        return kind, code, "rotate_key" if kind == "invalid_key" else "stop_payload"
    return "transport", None, "degrade_model"


def _apply_hedge_key_state(
    key_name: str, kind: str, http_code, model: str, *, error: Exception | None = None
) -> None:
    """Оновити стан ключа за результатом hedged-спроби (головний потік)."""
    if key_name not in gemini_keys.ALL_KEYS:
        return
    try:
        if kind == "quota_429":
            if gemini_keys.is_key_level_429(model, False):
                scope, seconds = _quota_scope_and_retry(error or _Gemini429())
                gemini_keys.mark_429(key_name, scope, seconds, model=model)
                gemini_keys.record_key_failure(
                    key_name, failure_kind="quota_429", http_code=429
                )
            return
        if kind == "permission_denied":
            gemini_keys.quarantine_key(
                key_name,
                failure_kind=kind,
                http_code=403,
                project_scope=True,
                seconds=gemini_keys.PERMISSION_PROJECT_QUARANTINE_SECONDS,
            )
            return
        if kind == "invalid_key":
            gemini_keys.quarantine_key(
                key_name, failure_kind=kind, http_code=http_code or 401,
                project_scope=False,
            )
            return
        if kind == "model_not_found":
            gemini_keys.record_key_failure(key_name, failure_kind=kind, http_code=404)
            return
        gemini_keys.record_key_failure(
            key_name, failure_kind=kind, http_code=http_code
        )
    except Exception:
        logger.debug("gemini key state update unavailable", exc_info=True)


def _freeze_live_candidate_plan(
    raw_plan: list[dict],
    *,
    models: list[str],
    manual_key: str | None = None,
) -> list[dict]:
    """Freeze the real model-major dispatch order before V2 persistence.

    The legacy scoreboard is allowed to reorder executable candidates of the
    primary model.  Previously that happened *after* ``GeminiRequest`` stored
    its immutable plan, so ``candidate_index`` described the pre-scoreboard
    order rather than the order the gateway actually used.  This helper makes
    the one adaptive decision up front and then assigns immutable indexes to
    that exact order.  Known pre-dispatch skips remain in the same model tier
    after its executable candidates and never reach provider I/O.

    Secret values stay in these short-lived in-process rows.  The accounting
    sanitizer has an exact allowlist and removes both ``key_name`` and
    ``key_value`` before persistence.
    """
    normalized_models = [str(model or "").strip() for model in models if model]
    rows: list[dict] = []
    manual_value = str(manual_key or "").strip()

    for model_position, model in enumerate(normalized_models):
        executable: list[dict] = []
        manual_skip_reason = (
            "circuit_open"
            if manual_value and gemini_keys.model_circuit_open(model)
            else ""
        )
        if manual_value:
            executable.append({
                "key_name": "(manual)",
                "key_value": manual_value,
                "model": model,
                "project_identity": "",
                "identity_status": "unknown",
                "skip_reason": manual_skip_reason,
            })
        model_rows = [
            dict(item)
            for item in raw_plan
            if str(item.get("model") or "") == model
        ]
        executable.extend(
            item for item in model_rows if not str(item.get("skip_reason") or "")
        )
        executable = [
            item for item in executable if not str(item.get("skip_reason") or "")
        ]

        if model_position == 0 and executable:
            original = list(executable)
            tuples = [
                (item["key_name"], item["key_value"], item["model"])
                for item in original
            ]
            try:
                ordered = gemini_scoreboard.order_candidates(tuples, role="chat")
                lookup = {
                    (item["key_name"], item["model"]): item
                    for item in original
                }
                ordered_keys = [(item[0], item[2]) for item in ordered]
                if (
                    len(ordered_keys) == len(original)
                    and len(set(ordered_keys)) == len(original)
                    and set(ordered_keys) == set(lookup)
                ):
                    executable = [lookup[key] for key in ordered_keys]
            except Exception:
                logger.debug("gemini scoreboard unavailable", exc_info=True)

        rows.extend(executable)
        if manual_value and manual_skip_reason:
            rows.append({
                "key_name": "(manual)",
                "key_value": manual_value,
                "model": model,
                "project_identity": "",
                "identity_status": "unknown",
                "skip_reason": "circuit_open",
            })
        rows.extend(
            item for item in model_rows if str(item.get("skip_reason") or "")
        )

    for candidate_index, item in enumerate(rows, start=1):
        item["candidate_index"] = candidate_index
    return rows


def _run_chat_with_pool(payload: dict, *, manual_key: str | None = None,
                        parse: bool = False, log_cb=None,
                        model_override: str | None = None,
                        model_chain_override: list[str] | tuple[str, ...] | None = None,
                        reasoning_task: str = "customer_chat",
                        deadline_seconds: float | None = None,
                        routing_decision=None,
                        result_validator=None,
                        repair_payload_factory=None,
                        max_actual_dispatches: int | None = None,
                        request_policy_manifest=None,
                        legacy_provider_root: bool = False) -> dict:
    """Run one live reply through a deadline-aware, quality-first pool.

    The generic runner is intentionally not reused here: its three rounds and
    backoff are valid for background jobs, but were the direct cause of a
    customer reply spending 75 seconds on three equal 25-second timeouts.
    """
    policy = reasoning_policy(reasoning_task)
    original_payload = copy.deepcopy(payload)
    original_payload["_reasoning_task"] = policy["task"]
    working_payload = copy.deepcopy(original_payload)
    dispatch_budget = None
    if result_validator is not None:
        from management.services.ig_provider_dispatch_budget import (
            ProviderDispatchBudget,
        )

        dispatch_budget = ProviderDispatchBudget(
            max_dispatches=(
                2 if max_actual_dispatches is None else max_actual_dispatches
            )
        )
    elif repair_payload_factory is not None or max_actual_dispatches is not None:
        raise ValueError(
            "repair and actual-dispatch limits require a result validator"
        )
    # Preserve the opted-in route policy when a durable remaining allowance
    # later clips the local budget to one or two calls.
    extended_dispatch_route = bool(dispatch_budget and dispatch_budget.max_dispatches > 2)
    manual_key = str(manual_key or "").strip() or None
    if manual_key and not gemini_keys.manual_key_allowed("chat", manual_key):
        manual_key = None
    if manual_key and gemini_keys.configured_alias_for_secret(manual_key):
        # The same credential already exists in the six-project plan. Retrying
        # it as "manual" would bypass its quota/cooldown state and spend the
        # same provider request twice under two labels.
        manual_key = None

    if model_chain_override is not None:
        models = [
            str(model or "").strip()
            for model in model_chain_override
            if gemini_keys.is_allowed_chat_model(str(model or "").strip())
        ]
    else:
        models = gemini_keys.task_model_chain("chat", policy["task"], model_override)
    if not models:
        raise CallAIAnalysisError("Не налаштована модель Gemini для live chat.")
    attempts: list[str] = []
    last_actual_failure_kind = ""
    request_id = uuid.uuid4().hex
    # Ланцюг «вхідне → спроби → holding → recovery → receipt» відновлюється по
    # цьому id: рядок відповіді зберігає його, а кожна спроба — власний lane та
    # порядковий номер (ЭА.1).
    try:
        from management.services.ig_turn_lineage import bind_request_id

        bind_request_id(request_id)
    except Exception:
        logger.debug("turn lineage unavailable", exc_info=True)
    attempt_counter = [0]
    started_at = time.monotonic()
    effective_deadline_seconds = (
        max(1.0, float(deadline_seconds))
        if deadline_seconds is not None
        else _chat_deadline_seconds(policy["task"])
    )
    deadline = started_at + effective_deadline_seconds

    def _emit(message: str) -> None:
        if log_cb:
            try:
                log_cb(message)
            except Exception:
                pass

    raw_candidate_plan = gemini_keys.live_chat_candidate_plan(
        model_chain_override=models
    )
    candidate_plan = _freeze_live_candidate_plan(
        raw_candidate_plan,
        models=models,
        manual_key=manual_key,
    )
    def _refresh_executable_candidates() -> tuple[list[tuple[str, str, str]], dict, dict]:
        executable = [
            (item["key_name"], item["key_value"], item["model"])
            for item in candidate_plan
            if not item["skip_reason"]
        ]
        indexes = {
            (item["key_name"], item["model"]): int(item["candidate_index"])
            for item in candidate_plan
        }
        scarcity = {
            int(item["candidate_index"]): item.get("scarce")
            for item in candidate_plan
            if "scarce" in item
        }
        return executable, indexes, scarcity

    candidates, candidate_indexes, candidate_scarcity = (
        _refresh_executable_candidates()
    )
    accounting_shadow_active = (
        str(getattr(settings, "GEMINI_ACCOUNTING_V2_MODE", "off") or "off")
        .strip()
        .casefold()
        == "shadow"
    )
    accounting_ownership_blocked = False
    accounting_block_reason = ""
    try:
        from management.services import gemini_accounting_runtime

        accounting_shadow_active = gemini_accounting_runtime.shadow_runtime_active()
        accounting_observer = gemini_accounting_runtime.begin_request(
            request_id=request_id,
            role="chat",
            reasoning_task=policy["task"],
            candidate_plan=candidate_plan,
            deadline_seconds=effective_deadline_seconds,
            routing_decision=routing_decision,
            request_policy_manifest=request_policy_manifest,
            legacy_provider_root=legacy_provider_root,
        )
        accounting_ownership_blocked = bool(
            getattr(accounting_observer, "provider_blocked", False)
        )
        accounting_block_reason = str(
            getattr(accounting_observer, "block_reason", "") or ""
        )
        if not getattr(accounting_observer, "enabled", False):
            if request_policy_manifest is not None and accounting_shadow_active and not accounting_ownership_blocked:
                accounting_ownership_blocked = True
                accounting_block_reason = "policy_manifest_unavailable"
            accounting_observer = None
    except Exception:
        accounting_observer = None
        if request_policy_manifest is not None:
            accounting_ownership_blocked = True
            accounting_block_reason = "policy_manifest_unavailable"
    if accounting_ownership_blocked:
        error = CallAIAnalysisError(
            "Gemini provider dispatch rejected: "
            + (accounting_block_reason or "request_ownership_conflict")
        )
        error.failure_kind = accounting_block_reason or "request_ownership_conflict"
        if accounting_block_reason.startswith("policy_manifest_"):
            error.policy_readiness = accounting_block_reason
        raise error
    provider_continuation = (
        getattr(accounting_observer, "provider_continuation", None)
        if accounting_observer is not None
        else None
    )
    if (legacy_provider_root or request_policy_manifest is not None) and provider_continuation is None and dispatch_budget is not None:
        # Older/unowned callers retain the old local allowance. Only a durable
        # pre-dispatch proof enables the wider live route, never a boolean alone.
        dispatch_budget = ProviderDispatchBudget(max_dispatches=min(2, dispatch_budget.max_dispatches))
        extended_dispatch_route = False
    if provider_continuation is not None:
        # Revision continuations carry a secret-free route frozen by the root
        # revision. Reattach only the matching local secret and execute in that
        # canonical order; a current scoreboard/quota-profile change may narrow
        # the route but cannot reorder or reclassify its scarcity tier.
        continuation_reason = str(
            getattr(provider_continuation, "reason", "") or ""
        )[:64]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", continuation_reason):
            continuation_reason = ""
        continuation_http_remaining = int(
            getattr(provider_continuation, "http_remaining", 0) or 0
        )
        if (
            not bool(getattr(provider_continuation, "ready", False))
            or continuation_http_remaining <= 0
        ):
            stop_reason = continuation_reason or (
                "provider_dispatch_budget"
                if continuation_http_remaining <= 0
                else "provider_candidates_exhausted"
            )
            accounting_observer.resolve_failure(stop_reason)
            error = CallAIAnalysisError(
                f"Gemini provider continuation rejected: {stop_reason}."
            )
            error.failure_kind = stop_reason
            if stop_reason == "provider_wait":
                from management.services.ig_legacy_provider_execution import continuation_horizon

                error.provider_next_due_at = provider_continuation.next_due_at
                error.provider_horizon_at = continuation_horizon(provider_continuation)
            raise error
        allowed_models = set(models)
        local_by_identity = {
            (
                item["key_name"],
                item["model"],
                str(item.get("project_identity") or ""),
            ): item
            for item in candidate_plan
        }
        continued_plan = []
        for frozen in provider_continuation.candidate_plan:
            identity = (
                str(frozen.get("key_name") or ""),
                str(frozen.get("model") or ""),
                str(frozen.get("project_identity") or ""),
            )
            local = local_by_identity.get(identity)
            if local is None:
                merged = {
                    "key_name": identity[0],
                    "key_value": "",
                    "model": identity[1],
                    "project_identity": identity[2],
                }
                missing_identity = True
            else:
                merged = dict(local)
                missing_identity = False
            merged.update({
                "candidate_index": int(frozen["candidate_index"]),
                "identity_status": str(frozen.get("identity_status") or "unknown"),
                "skip_reason": (
                    "provider_candidate_identity_changed"
                    if missing_identity
                    else str(frozen.get("skip_reason") or local.get("skip_reason") or "")
                ),
                "scarce": bool(frozen.get("scarce", True)),
            })
            continued_plan.append(merged)
        candidate_plan = continued_plan
        models = []
        for frozen in provider_continuation.candidate_plan:
            frozen_model = str(frozen.get("model") or "")
            if frozen_model in allowed_models and frozen_model not in models:
                models.append(frozen_model)
        candidates, candidate_indexes, candidate_scarcity = (
            _refresh_executable_candidates()
        )
        if dispatch_budget is not None:
            dispatch_budget = ProviderDispatchBudget(
                max_dispatches=min(
                    dispatch_budget.max_dispatches,
                    continuation_http_remaining,
                ),
                max_scarce_dispatches=max(
                    0,
                    min(
                        2,
                        int(provider_continuation.scarce_remaining or 0),
                    ),
                ),
            )

    def _audit_not_attempted(key_name: str, model: str, reason: str,
                             candidate_index: int) -> None:
        """Кандидат, якого НЕ викликали, теж отримує рядок.

        Без цього «шість страхуючих ключів» неможливо ні підтвердити, ні
        опровергнути: у телеметрії залишались лише реально виконані запити.
        """
        try:
            if accounting_observer is not None:
                return accounting_observer.record_not_attempted(
                    key_name=key_name,
                    model=model,
                    reason=reason,
                    candidate_index=accounting_observer.candidate_index(
                        key_name, model
                    ),
                )
            gemini_keys.record_attempt(
                request_id=request_id,
                role="chat",
                key_name=key_name,
                model=model,
                outcome="not_attempted",
                decision="skip_candidate",
                remaining_deadline_ms=max(0, int((deadline - time.monotonic()) * 1000)),
                candidate_index=candidate_index,
                not_attempted_reason=reason,
            )
        except Exception:
            logger.debug("gemini attempt audit unavailable", exc_info=True)

    audited_candidate_indexes: set[int] = set()
    dispatched_candidate_indexes: set[int] = set()
    model_not_found_projects: dict[str, set[str]] = {}

    def _audit_skip(key_name: str, model: str, reason: str, candidate_index: int) -> None:
        if candidate_index <= 0:
            _audit_not_attempted(key_name, model, reason, candidate_index)
            return
        if candidate_index in audited_candidate_indexes:
            return
        _audit_not_attempted(key_name, model, reason, candidate_index)
        audited_candidate_indexes.add(candidate_index)

    for planned in candidate_plan:
        if planned["skip_reason"]:
            _audit_skip(
                planned["key_name"],
                planned["model"],
                planned["skip_reason"],
                int(planned["candidate_index"]),
            )

    def _audit_remaining(reason: str, *, model: str = "") -> None:
        if accounting_observer is not None:
            # Canonical shadow settlement/terminalization owns the batched
            # remainder. Persist the exact stop reason in one bulk operation;
            # a model-scoped SLA stop must not be flattened to the later
            # request-wide ``exhausted`` resolution.
            accounting_observer.record_remaining(reason, model_filter=model)
            return
        for planned in candidate_plan:
            index = int(planned["candidate_index"])
            if planned["skip_reason"] or index in dispatched_candidate_indexes:
                continue
            if model and planned["model"] != model:
                continue
            _audit_skip(planned["key_name"], planned["model"], reason, index)

    def _call(key_name: str, key_value: str, model: str, *, preserve_fallback: bool,
              candidate_index: int = 0, candidate_scarce: bool | None = None):
        nonlocal last_actual_failure_kind, working_payload
        if dispatch_budget is not None:
            budget_stop = dispatch_budget.dispatch_block_reason(
                model, scarce=candidate_scarce
            )
            if budget_stop == "scarce_model_budget":
                _audit_remaining(budget_stop, model=model)
                return None, budget_stop
            if budget_stop:
                _audit_remaining(budget_stop)
                if accounting_observer is not None:
                    accounting_observer.resolve_failure(budget_stop)
                error = CallAIAnalysisError(
                    "Gemini validated reply exhausted its provider dispatch budget."
                )
                error.failure_kind = (
                    last_actual_failure_kind or "provider_dispatch_budget"
                )
                raise error
        if gemini_keys.model_circuit_open(model):
            attempts.append(f"{key_name}/{model}: model_circuit_open")
            _emit(f"{key_name}/{model}: model circuit open")
            _audit_skip(key_name, model, "circuit_open", candidate_index)
            return None, "model_circuit_open"
        remaining = deadline - time.monotonic()
        timeout = _chat_timeout(remaining, preserve_fallback=preserve_fallback)
        if timeout is None:
            _audit_skip(key_name, model, "deadline", candidate_index)
            return None, "deadline"
        lease_token = None
        if key_name in gemini_keys.ALL_KEYS:
            if not gemini_keys.is_available(key_name, model=model):
                attempts.append(f"{key_name}/{model}: quarantined")
                _emit(f"{key_name}/{model}: quarantined")
                _audit_skip(key_name, model, "quarantine", candidate_index)
                return None, "quarantined"
            lease_token = gemini_keys.acquire_key_lease(key_name, role="chat")
            if not lease_token:
                attempts.append(f"{key_name}/{model}: lease_busy")
                _emit(f"{key_name}/{model}: lease busy")
                _audit_skip(key_name, model, "lease_busy", candidate_index)
                return None, "lease_busy"
        attempt_counter[0] += 1
        attempt_index = attempt_counter[0]
        if candidate_index:
            dispatched_candidate_indexes.add(candidate_index)

        def _release() -> None:
            if lease_token:
                try:
                    gemini_keys.release_key_lease(key_name, lease_token)
                except Exception:
                    logger.debug("gemini key lease release unavailable", exc_info=True)
        request_payload = _payload_for_model(
            model, working_payload, reasoning_task=policy["task"]
        )
        request_payload.pop("_reasoning_task", None)
        call_started_at = time.monotonic()
        attempt_boundary = None

        def _audit(outcome: str, *, failure_kind: str = "", http_code: int | None = None,
                   provider_reason: str = "", decision: str = "",
                   usage: dict | None = None, error_detail: str = "") -> None:
            try:
                gemini_keys.record_attempt(
                    request_id=request_id,
                    role="chat",
                    key_name=key_name,
                    model=model,
                    outcome=outcome,
                    failure_kind=failure_kind,
                    http_code=http_code,
                    provider_reason=provider_reason,
                    decision=decision,
                    error_detail=error_detail,
                    latency_ms=int((time.monotonic() - call_started_at) * 1000),
                    remaining_deadline_ms=max(0, int((deadline - time.monotonic()) * 1000)),
                    usage=usage,
                    attempt_index=attempt_index,
                    candidate_index=candidate_index,
                    existing_attempt_id=(
                        getattr(attempt_boundary, "attempt_id", None)
                        if attempt_boundary is not None else None
                    ),
                )
            except Exception:
                logger.debug("gemini attempt audit unavailable", exc_info=True)

        attempt_boundary = (
            accounting_observer.attempt(
                key_name=key_name,
                model=model,
                candidate_index=accounting_observer.candidate_index(
                    key_name, model
                ),
            )
            if accounting_observer is not None
            else None
        )
        if (
            attempt_boundary is not None
            and attempt_boundary.validate_ownership() is not True
        ):
            _release()
            raise CallAIAnalysisError(
                "Gemini provider dispatch rejected: stale request ownership."
            )
        quota_dispatch_at = timezone.now()
        legacy_quota_reserved = False
        if key_name in gemini_keys.ALL_KEYS and not gemini_quota.try_reserve(
            key_name, model, now=quota_dispatch_at
        ):
            # ЭБ.4: пара исчерпана по локальному учёту — не тратим ход на 429,
            # который провайдер вернул бы через несколько сотен миллисекунд.
            attempts.append(f"{key_name}/{model}: quota_exhausted_local")
            _emit(f"{key_name}/{model}: квота пари вичерпана (локальний облік)")
            _audit_skip(key_name, model, "quota_exhausted", candidate_index)
            _release()
            return None, "quota"
        if key_name in gemini_keys.ALL_KEYS:
            legacy_quota_reserved = True
        try:
            call_kwargs = {"parse": parse, "timeout": timeout}
            if attempt_boundary is not None:
                call_kwargs["attempt_boundary"] = attempt_boundary
            if dispatch_budget is not None:
                call_kwargs["dispatch_budget"] = dispatch_budget
                call_kwargs["dispatch_scarce"] = candidate_scarce
                call_kwargs["defer_attempt_success"] = True
            parsed, usage = _gemini_call_once(
                model, request_payload, key_value, **call_kwargs
            )
            if key_name in gemini_keys.ALL_KEYS:
                gemini_quota.settle(
                    key_name,
                    model,
                    int((usage or {}).get("totalTokenCount") or 0),
                    dispatch_at=quota_dispatch_at,
                )
        except _GeminiAdmissionRejected as exc:
            if legacy_quota_reserved:
                gemini_quota.cancel_reservation(
                    key_name,
                    model,
                    dispatch_at=quota_dispatch_at,
                )
            _release()
            block_reason = str(
                getattr(exc, "reason", "") or "stale_provider_boundary"
            )[:48]
            error = CallAIAnalysisError(
                f"Gemini provider dispatch rejected: {block_reason}."
            )
            error.failure_kind = block_reason
            raise error from exc
        except _GeminiDispatchBudgetExhausted as exc:
            if legacy_quota_reserved:
                gemini_quota.cancel_reservation(
                    key_name,
                    model,
                    dispatch_at=quota_dispatch_at,
                )
            if candidate_index:
                dispatched_candidate_indexes.discard(candidate_index)
            budget_stop = str(
                getattr(exc, "reason", "provider_dispatch_budget")
                or "provider_dispatch_budget"
            )[:24]
            _audit_skip(key_name, model, budget_stop, candidate_index)
            _audit_remaining(
                budget_stop,
                model=model if budget_stop == "scarce_model_budget" else "",
            )
            _release()
            if budget_stop == "scarce_model_budget":
                return None, budget_stop
            if accounting_observer is not None:
                accounting_observer.resolve_failure(budget_stop)
            error = CallAIAnalysisError(
                "Gemini validated reply exhausted its provider dispatch budget."
            )
            error.failure_kind = last_actual_failure_kind or "provider_dispatch_budget"
            raise error from exc
        except _GeminiTransient as exc:
            kind, transient_http_code = _transient_failure_details(exc)
            last_actual_failure_kind = kind
            attempts.append(f"{key_name}/{model}: {kind}: {exc}")
            if key_name in gemini_keys.ALL_KEYS:
                gemini_keys.record_key_failure(
                    key_name,
                    failure_kind=kind,
                    http_code=transient_http_code,
                    latency_ms=int((time.monotonic() - call_started_at) * 1000),
                )
            _audit(
                "failed", failure_kind=kind,
                http_code=transient_http_code,
                provider_reason=_bounded_provider_reason(exc),
                decision="degrade_model",
            )
            _emit(f"{key_name}/{model}: {kind} ({time.monotonic() - call_started_at:.1f}с)")
            _release()
            return None, "transient"
        except _GeminiEmpty as exc:
            last_actual_failure_kind = "empty"
            attempts.append(f"{key_name}/{model}: empty: {exc}")
            _audit("failed", failure_kind="empty", decision="retry_or_degrade")
            _emit(f"{key_name}/{model}: empty response")
            _release()
            return None, "empty"
        except _Gemini429 as exc:
            last_actual_failure_kind = "quota_429"
            if key_name in gemini_keys.ALL_KEYS and gemini_keys.is_key_level_429(model, False):
                scope, seconds = _quota_scope_and_retry(exc)
                gemini_keys.mark_429(
                    key_name, scope, seconds, error=str(exc), model=model
                )
                gemini_keys.record_key_failure(key_name, failure_kind="quota_429", http_code=429)
            # ЭА.10/ЭА.11: `cooldown_project` без відомої project-group — це
            # неперевірюване твердження про область квоти. Коли mapping не дав
            # ідентичності проекту, рішення описується як кулдаун КЛЮЧА: це те,
            # що ми справді зробили.
            _audit(
                "failed", failure_kind="quota_429", http_code=429,
                provider_reason=_bounded_provider_reason(exc),
                decision=(
                    "cooldown_project"
                    if gemini_keys.project_group(key_name)
                    else "cooldown_key"
                ),
            )
            attempts.append(f"{key_name}/{model}: quota_429")
            _emit(f"{key_name}/{model}: quota_429")
            _release()
            return None, "quota"
        except _GeminiModelUnavailable as exc:
            attempts.append(f"{key_name}/{model}: model_unavailable: {exc}")
            kind = "model_not_found" if "404" in str(exc) else "permission_denied"
            last_actual_failure_kind = kind
            if key_name in gemini_keys.ALL_KEYS:
                if kind == "permission_denied":
                    gemini_keys.quarantine_key(
                        key_name,
                        failure_kind=kind,
                        http_code=403,
                        project_scope=True,
                        seconds=gemini_keys.PERMISSION_PROJECT_QUARANTINE_SECONDS,
                    )
                else:
                    gemini_keys.record_key_failure(
                        key_name, failure_kind=kind, http_code=404
                    )
            model_not_found_global = False
            if kind == "model_not_found":
                project_identity = (
                    gemini_keys.project_group(key_name)
                    or str(key_name or "")
                )
                evidence = model_not_found_projects.setdefault(model, set())
                evidence.add(project_identity)
                model_not_found_global = len(evidence) >= 2
                if model_not_found_global:
                    gemini_keys.open_model_circuit(
                        model,
                        reason=kind,
                        project=project_identity,
                    )
            _audit(
                "failed", failure_kind=kind,
                http_code=404 if kind == "model_not_found" else 403,
                provider_reason=_bounded_provider_reason(exc),
                decision=(
                    "skip_model"
                    if model_not_found_global
                    else "rotate_project"
                    if kind == "model_not_found"
                    else "rotate_key"
                ),
            )
            _emit(f"{key_name}/{model}: model unavailable")
            _release()
            return None, (
                "model_not_found_global"
                if model_not_found_global
                else "model_not_found_project"
                if kind == "model_not_found"
                else kind
            )
        except _GeminiFatal as exc:
            from management.services import gemini_payload_contract

            kind = "invalid_key" if _chat_key_failure(exc) else "invalid_payload"
            last_actual_failure_kind = kind
            http_match = _HTTP_CODE_RE.search(str(exc))
            provider_http_code = int(http_match.group(1)) if http_match else (
                401 if kind == "invalid_key" else 400
            )
            if kind == "invalid_key" and key_name in gemini_keys.ALL_KEYS:
                gemini_keys.quarantine_key(
                    key_name,
                    failure_kind=kind,
                    http_code=provider_http_code,
                    project_scope=False,
                )
            # ЭА.20: варіант контракту й поле схеми — єдине, що робить 400
            # розбірливим пізніше. Без цього рядка телеметрія знову зберігала б
            # лише слово `invalid_payload`.
            contract_detail = str(getattr(exc, "contract_detail", "") or "")
            variant_retry_available = (
                kind == "invalid_payload"
                and gemini_payload_contract.retry_variant_available(request_payload)
            )
            _audit(
                "failed", failure_kind=kind,
                http_code=provider_http_code,
                provider_reason=_bounded_provider_reason(exc),
                decision=(
                    "rotate_key"
                    if kind == "invalid_key"
                    else "retry_simplified_payload"
                    if variant_retry_available
                    else "stop_payload"
                ),
                error_detail=contract_detail,
            )
            if _chat_key_failure(exc):
                attempts.append(f"{key_name}/{model}: invalid_key")
                _emit(f"{key_name}/{model}: invalid key")
                _release()
                return None, "invalid_key"
            _release()
            if variant_retry_available:
                # Той самий payload не повторюємо: це гарантований другий відказ.
                # Дозволений РІВНО один ретрай заздалегідь визначеним спрощеним
                # варіантом — його підставить preflight наступного кандидата, а
                # ліміт повільних спроб моделі лишає це однією спробою.
                attempts.append(f"{key_name}/{model}: invalid_payload_variant_retry")
                _emit(f"{key_name}/{model}: payload contract simplified for retry")
                return None, "invalid_payload_variant"
            _audit_remaining("fatal_payload")
            if accounting_observer is not None:
                accounting_observer.resolve_failure("fatal_payload")
            raise CallAIAnalysisError(f"Помилка запиту до Gemini: {exc}") from exc

        if result_validator is not None:
            from management.services.ig_provider_dispatch_budget import (
                ValidationDecision,
                normalize_validation_decision,
                sanitized_validation_usage,
            )

            try:
                validation = normalize_validation_decision(
                    result_validator(
                        parsed,
                        usage=sanitized_validation_usage(usage),
                    )
                )
            except Exception:
                logger.warning("Gemini result validator failed closed")
                validation = ValidationDecision(
                    valid=False,
                    reason_codes=("validator_error",),
                )
            if not validation.valid:
                last_actual_failure_kind = "invalid_response"
                error = _GeminiResultInvalid(
                    ",".join(validation.reason_codes)
                )
                if attempt_boundary is not None:
                    attempt_boundary.failed(
                        error,
                        usage=usage,
                        failure_kind="invalid_response",
                    )
                _release()
                repaired_payload = None
                repair_ready = bool(
                    repair_payload_factory is not None
                    and dispatch_budget is not None
                    and dispatch_budget.consume_repair()
                )
                if repair_ready and accounting_observer is not None:
                    reserve_repair = getattr(
                        accounting_observer, "reserve_provider_repair", None
                    )
                    if callable(reserve_repair):
                        repair_ready = bool(reserve_repair(
                            key_name=key_name,
                            model=model,
                            candidate_index=candidate_index,
                        ))
                    elif provider_continuation is not None:
                        # A durable revision must prove the graph-wide repair
                        # reservation. Legacy observers without that contract
                        # retain their request-local one-repair behavior.
                        repair_ready = False
                if repair_ready:
                    repair_source = copy.deepcopy(working_payload)
                    repair_source.pop("_reasoning_task", None)
                    try:
                        repaired_payload = repair_payload_factory(
                            repair_source,
                            parsed,
                            validation.reason_codes,
                        )
                    except Exception:
                        logger.warning("Gemini repair payload factory failed closed")
                rotate_model_ready = bool(
                    not isinstance(repaired_payload, dict)
                    and dispatch_budget is not None
                    and dispatch_budget.remaining_dispatches > 0
                    and extended_dispatch_route
                )
                _audit(
                    "failed",
                    failure_kind="invalid_response",
                    provider_reason="result_validation_failed",
                    decision=(
                        "repair_result"
                        if isinstance(repaired_payload, dict)
                        else "rotate_model"
                        if rotate_model_ready
                        else "stop_result"
                    ),
                    error_detail=",".join(validation.reason_codes)[:120],
                )
                attempts.append(f"{key_name}/{model}: invalid_response")
                _emit(f"{key_name}/{model}: result validation failed")
                if isinstance(repaired_payload, dict):
                    working_payload = copy.deepcopy(repaired_payload)
                    working_payload["_reasoning_task"] = policy["task"]
                    # A failed attempt does not consume its immutable candidate.
                    # Reuse that exact candidate under the observer's next unique
                    # attempt index so repair does not depend on a spare API key.
                    try:
                        return _call(
                            key_name,
                            key_value,
                            model,
                            preserve_fallback=preserve_fallback,
                            candidate_index=candidate_index,
                            candidate_scarce=candidate_scarce,
                        )
                    finally:
                        # The repair may include the rejected model output. It
                        # belongs only to this same-candidate call and must not
                        # become context for another key or model after any
                        # success, typed failure, or exception.
                        working_payload = copy.deepcopy(original_payload)
                working_payload = copy.deepcopy(original_payload)
                if rotate_model_ready:
                    _audit_remaining(
                        "result_validation_failed", model=model
                    )
                    return None, "invalid_response_model"
                _audit_remaining("result_validation_failed")
                if accounting_observer is not None:
                    accounting_observer.resolve_failure(
                        "result_validation_failed"
                    )
                raise CallAIAnalysisError(
                    "Gemini reply failed deterministic result validation."
                )
            if attempt_boundary is not None:
                attempt_boundary.succeeded(usage)

        if key_name in gemini_keys.ALL_KEYS:
            gemini_keys.record_key_success(
                key_name, latency_ms=int((time.monotonic() - call_started_at) * 1000)
            )
        gemini_keys.record_model_success(model)
        _audit("succeeded", usage=usage)
        attempts.append(f"{key_name}/{model}: ok")
        _emit(f"{key_name}/{model}: ok ({time.monotonic() - call_started_at:.1f}с)")
        result = _chat_result(
            parsed=parsed, usage=usage, model=model, key_name=key_name,
            attempts=attempts, policy=policy, started_at=call_started_at,
        )
        if accounting_observer is not None:
            result["meta"]["request_id"] = accounting_observer.request_id
        if lease_token:
            _release()
        return result, "ok"

    def _hedged_primary(primary_candidates: list, primary_model: str):
        """Legacy-only Lite hedge, unavailable to active V2 accounting.

        The executable V2 contract is the sequential branch below: all six
        projects remain in the immutable plan, but only
        ``CHAT_PRIMARY_ATTEMPT_LIMIT`` slow primary calls may cross provider I/O
        before quality fallback.  This helper stays quarantined behind the
        disabled legacy flag until it can own canonical concurrent boundaries.
        """
        if not primary_candidates:
            return None
        leased: list = []
        prepared: list = []
        for key_name, key_value, model in primary_candidates:
            if key_name in gemini_keys.ALL_KEYS:
                if not gemini_keys.is_available(key_name, model=model):
                    attempts.append(f"{key_name}/{model}: quarantined")
                    _audit_skip(
                        key_name,
                        model,
                        "quarantine",
                        candidate_indexes.get((key_name, model), 0),
                    )
                    continue
                token = gemini_keys.acquire_key_lease(key_name, role="chat")
                if not token:
                    attempts.append(f"{key_name}/{model}: lease_busy")
                    _audit_skip(
                        key_name,
                        model,
                        "lease_busy",
                        candidate_indexes.get((key_name, model), 0),
                    )
                    continue
                leased.append((key_name, token))
            prepared.append((key_name, key_value, model))
        if not prepared:
            return None

        def call_one(key_name: str, key_value: str, model: str, timeout):
            request_payload = _payload_for_model(
                model, working_payload, reasoning_task=policy["task"]
            )
            request_payload.pop("_reasoning_task", None)
            quota_dispatch_at = timezone.now()
            if key_name in gemini_keys.ALL_KEYS and not gemini_quota.try_reserve(
                key_name, model, now=quota_dispatch_at
            ):
                raise _Gemini429("local quota ledger: pair exhausted")
            parsed, usage = _gemini_call_once(
                model, request_payload, key_value, parse=parse, timeout=timeout
            )
            if key_name in gemini_keys.ALL_KEYS:
                gemini_quota.settle(
                    key_name,
                    model,
                    int((usage or {}).get("totalTokenCount") or 0),
                    dispatch_at=quota_dispatch_at,
                )
            return parsed, usage

        try:
            wave = gemini_hedge.run_hedged(
                prepared,
                call_one=call_one,
                deadline_monotonic=deadline,
                expected_latency_ms=lambda key, model: (
                    gemini_scoreboard.expected_latency_ms(key, model, role="chat")
                ),
                # A 404 is model-wide. A 403 can be project permission state
                # and must rotate to another project on the same model.
                aborts_wave=lambda exc: (
                    isinstance(exc, _GeminiModelUnavailable)
                    and "404" in str(exc)
                ),
                max_in_flight=2,
            )
        finally:
            for key_name, token in leased:
                try:
                    gemini_keys.release_key_lease(key_name, token)
                except Exception:
                    logger.debug("gemini lease release unavailable", exc_info=True)

        # Телеметрія і стан ключів — тільки з головного потоку.
        winner_payload = None
        for outcome in wave.outcomes:
            attempt_counter[0] += 1
            frozen_candidate_index = candidate_indexes.get(
                (outcome.key_name, outcome.model), outcome.candidate_index
            )
            if outcome.skipped_reason:
                _audit_skip(
                    outcome.key_name, outcome.model,
                    outcome.skipped_reason,
                    frozen_candidate_index,
                )
                continue
            if frozen_candidate_index:
                dispatched_candidate_indexes.add(frozen_candidate_index)
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if outcome.succeeded:
                parsed, usage = outcome.result
                if key_in_pool := outcome.key_name in gemini_keys.ALL_KEYS:
                    gemini_keys.record_key_success(
                        outcome.key_name, latency_ms=outcome.latency_ms
                    )
                del key_in_pool
                gemini_keys.record_model_success(outcome.model)
                gemini_keys.record_attempt(
                    request_id=request_id, role="chat", key_name=outcome.key_name,
                    model=outcome.model, outcome="succeeded",
                    latency_ms=outcome.latency_ms,
                    remaining_deadline_ms=remaining_ms, usage=usage,
                    attempt_index=attempt_counter[0],
                    candidate_index=frozen_candidate_index,
                )
                attempts.append(f"{outcome.key_name}/{outcome.model}: ok")
                _emit(
                    f"{outcome.key_name}/{outcome.model}: ok "
                    f"({outcome.latency_ms / 1000:.1f}с, hedged)"
                )
                if wave.winner is outcome:
                    winner_payload = _chat_result(
                        parsed=parsed, usage=usage, model=outcome.model,
                        key_name=outcome.key_name, attempts=attempts,
                        policy=policy, started_at=started_at,
                    )
                continue
            kind, http_code, decision = _classify_hedge_error(outcome.error)
            _apply_hedge_key_state(
                outcome.key_name,
                kind,
                http_code,
                outcome.model,
                error=outcome.error,
            )
            gemini_keys.record_attempt(
                request_id=request_id, role="chat", key_name=outcome.key_name,
                model=outcome.model, outcome="failed", failure_kind=kind,
                http_code=http_code,
                provider_reason=_bounded_provider_reason(outcome.error),
                decision=decision, latency_ms=outcome.latency_ms,
                remaining_deadline_ms=remaining_ms,
                attempt_index=attempt_counter[0],
                candidate_index=frozen_candidate_index,
            )
            attempts.append(f"{outcome.key_name}/{outcome.model}: {kind}")
            _emit(f"{outcome.key_name}/{outcome.model}: {kind} (hedged)")
        # Скорборд читає свіжу телеметрію: наступний хід уже знає, хто відповів.
        try:
            gemini_scoreboard.invalidate("chat")
        except Exception:
            pass
        return winner_payload

    primary = models[0]
    # The adaptive order was frozen before the immutable request graph was
    # created.  Never ask the scoreboard for a second, potentially different
    # order after persistence.
    primary_attempts = [candidate for candidate in candidates if candidate[2] == primary]

    # ЭБ.2: hedging лечит РАЗБРОС ЛАТЕНТНОСТИ, а не исчерпанную квоту. Под
    # квотой он вредит: 429 приходит за ~0.3 с, а следующий ключ волны стартует
    # через 1.5 с и получает тот же 429 — один ход остужает несколько ключей, и
    # следующий клиент начинает с меньшим пулом. Если по этой модели уже есть
    # остывшая пара (ключ, модель), волну не открываем: идём одним кандидатом.
    #
    # ЭБ.4 усиливает правило арифметикой квот. Волна из трёх ключей стоит 3 из 20
    # суточных запросов сильной модели — двадцать таких ходов съедают половину
    # дневного бюджета ради двадцати ответов. На lite (500/сутки на ключ) та же
    # волна стоит 0.6% бюджета. Поэтому hedging допустим ТОЛЬКО на дешёвом тире.
    # The legacy hedge is forbidden on scarce 20-RPD models.  It also remains
    # structurally disabled while accounting V2 shadow is active: the legacy
    # worker threads do not own RequestObserver boundaries and therefore cannot
    # produce a canonical parent/winner/late-loser graph.  A future hedge may be
    # enabled only after that concurrent linkage is implemented explicitly.
    hedging_affordable = bool(
        ENABLE_LEGACY_CHAT_HEDGE
        and primary == "gemini-3.5-flash-lite"
        and not accounting_shadow_active
        and dispatch_budget is None
    )
    quota_pressure = gemini_keys.model_quota_pressure("chat", primary)
    if quota_pressure or not hedging_affordable:
        # Sequential SLA path: the immutable plan retains every project for
        # evidence, fast key-specific failures may rotate immediately, and at
        # most two slow primary calls cross provider I/O before quality fallback.
        primary_slow_calls = 0
        for key_name, key_value, _model in primary_attempts:
            index = candidate_indexes.get((key_name, primary), 0)
            result, state = _call(
                key_name, key_value, primary,
                preserve_fallback=True, candidate_index=index,
                candidate_scarce=candidate_scarcity.get(index),
            )
            if result:
                _audit_remaining("winner_found")
                return result
            if state == "deadline":
                _audit_remaining("deadline")
                if accounting_observer is not None:
                    accounting_observer.resolve_failure("deadline")
                raise CallAIAnalysisError(
                    "Перебір Gemini перервано по live дедлайну. Спроби: "
                    + "; ".join(attempts)
                )
            if state in {
                "invalid_response_model",
                "model_not_found_global",
                "model_circuit_open",
                "scarce_model_budget",
            }:
                _audit_remaining("model_terminal", model=primary)
                break
            if state not in {
                "invalid_key",
                "permission_denied",
                "model_not_found_project",
                "quota",
                "lease_busy",
                "quarantined",
            }:
                primary_slow_calls += 1
                if primary_slow_calls >= CHAT_PRIMARY_ATTEMPT_LIMIT:
                    _audit_remaining("sla_model_budget", model=primary)
                    break
    else:
        hedged_result = _hedged_primary(primary_attempts, primary)
        if hedged_result is not None:
            _audit_remaining("winner_found")
            return hedged_result

    # A slow primary-model/transport fault gets one quality fallback phase.  A
    # fast key failure may still walk the full key list below without spending
    # the live budget on six long timeouts.
    for model in models[1:]:
        slow_fallback_calls = 0
        for key_name, key_value, _ in (candidate for candidate in candidates if candidate[2] == model):
            fallback_index = candidate_indexes.get((key_name, model), 0)
            result, state = _call(
                key_name, key_value, model,
                preserve_fallback=False, candidate_index=fallback_index,
                candidate_scarce=candidate_scarcity.get(fallback_index),
            )
            if result:
                _audit_remaining("winner_found")
                return result
            if state == "deadline":
                _audit_remaining("deadline")
                if accounting_observer is not None:
                    accounting_observer.resolve_failure("deadline")
                raise CallAIAnalysisError(
                    "Перебір Gemini перервано по live дедлайну. Спроби: "
                    + "; ".join(attempts)
                )
            if state in {
                "invalid_response_model",
                "model_not_found_global",
                "model_circuit_open",
                "scarce_model_budget",
            }:
                _audit_remaining("model_terminal", model=model)
                break
            # На fallback-моделях паралелізм недоречний: кожен зайвий виклик —
            # це витрата безкоштовної квоти на гіршу відповідь. Але дві повільні
            # спроби підряд означають, що і ця модель зараз не відповідає, тому
            # йдемо до наступної, а не тримаємо клієнта.
            if state not in {
                "invalid_key",
                "permission_denied",
                "model_not_found_project",
                "quota",
                "lease_busy",
                "quarantined",
            }:
                slow_fallback_calls += 1
                if slow_fallback_calls >= CHAT_FALLBACK_ATTEMPT_LIMIT:
                    _audit_remaining("sla_model_budget", model=model)
                    break

    if accounting_observer is not None:
        accounting_observer.resolve_failure("exhausted")
    if dispatch_budget is not None:
        error = CallAIAnalysisError(
            "Усі Gemini-кандидати для validated live chat недоступні."
        )
        error.failure_kind = last_actual_failure_kind or "provider_dispatch_budget"
        raise error
    raise CallAIAnalysisError(
        "Усі Gemini-кандидати для live chat недоступні. Спроби: "
        + "; ".join(attempts)
    )


def gemini_generate_json(system_instruction: str, user_text: str, *,
                         role: str = "management", max_output_tokens: int = 4096,
                         reasoning_task: str | None = None,
                         timeout: tuple[float, float] | None = None,
                         deadline_seconds: float | None = None,
                         images: list[tuple[str, bytes]] | None = None,
                         image_labels: list[dict] | None = None) -> dict:
    """Текстовий JSON-запит до Gemini. Пул ключів ролі + цепочка моделей."""
    parts = [{"text": user_text}]
    labels = image_labels if isinstance(image_labels, list) else []
    for image_index, (mime, raw) in enumerate((images or [])[:8]):
        try:
            encoded = base64.b64encode(raw).decode()
            label = labels[image_index] if image_index < len(labels) and isinstance(labels[image_index], dict) else None
            if label is not None:
                parts.append({
                    "text": (
                        f"INLINE_IMAGE index={image_index} "
                        f"original_index={label.get('original_index', image_index)}"
                    )
                })
            parts.append({
                "inline_data": {
                    "mime_type": str(mime or "image/jpeg"),
                    "data": encoded,
                }
            })
        except Exception:
            continue
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "temperature": 0.25,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        },
    }
    return _run_with_pool(
        role,
        payload,
        timeout=timeout or (MANAGEMENT_TEXT_TIMEOUT if role == "management" else None),
        deadline_seconds=(
            deadline_seconds
            if deadline_seconds is not None
            else (MANAGEMENT_TEXT_DEADLINE_SECONDS if role == "management" else None)
        ),
        reasoning_task=reasoning_task or (
            "customer_intelligence" if role in {"management", "checker"} else "customer_chat"
        ),
    )


def gemini_generate_grounded(
    system_instruction: str,
    user_text: str,
    *,
    role: str = "checker",
    api_key: str | None = None,
    max_output_tokens: int = 12288,
    reasoning_task: str = "conversion_analysis",
) -> dict:
    """Grounded (Google Search) JSON-запит до Gemini для AI-чекера.

    Grounding несумісний з responseMimeType=json, тому просимо строгий JSON у
    промпті, а _gemini_call_once парсить його з тексту. Безкоштовний grounding є
    лише на gemini-2.5-flash — на gen-3 моделях 429 трактується як model-skip
    (модель платна), без кулдауну ключа. Ручний api_key пробується першим.

    maxOutputTokens=8192 + обмежений thinkingBudget: 2.5-flash витрачає ~1200
    токенів на thinking + tool-use, тож лишаємо запас на сам JSON-вивід (інакше
    finishReason=STOP з порожнім текстом).
    """
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }
    return _run_with_pool(
        role,
        payload,
        manual_key=(api_key or "").strip() or None,
        grounded=True,
        reasoning_task=reasoning_task,
    )


def gemini_generate_text(payload: dict, *, role: str = "chat",
                         manual_key: str | None = None, log_cb=None,
                         model_override: str | None = None,
                         model_chain_override: list[str] | tuple[str, ...] | None = None,
                         reasoning_task: str | None = None,
                         parse: bool = False,
                         deadline_seconds: float | None = None,
                         routing_decision=None,
                         result_validator=None,
                         repair_payload_factory=None,
                         max_actual_dispatches: int | None = None,
                         request_policy_manifest=None,
                         legacy_provider_root: bool = False) -> dict:
    """Текстовий (не-JSON) запит для діалогового бота. Пул ключів ролі + цепочка
    моделей. У result['parsed'] — сирий текст відповіді моделі.
    log_cb (опц.) отримує короткі рядки про кожну спробу (для консолі бота)."""
    if role == "chat":
        return _run_chat_with_pool(
            payload,
            manual_key=(manual_key or "").strip() or None,
            parse=parse,
            log_cb=log_cb,
            model_override=model_override,
            model_chain_override=model_chain_override,
            reasoning_task=reasoning_task or "customer_chat",
            deadline_seconds=deadline_seconds,
            routing_decision=routing_decision,
            result_validator=result_validator,
            repair_payload_factory=repair_payload_factory,
            max_actual_dispatches=max_actual_dispatches,
            request_policy_manifest=request_policy_manifest,
            legacy_provider_root=legacy_provider_root,
        )
    if (
        result_validator is not None
        or repair_payload_factory is not None
        or max_actual_dispatches is not None
        or request_policy_manifest is not None
        or legacy_provider_root
    ):
        raise ValueError("result validation is supported only for live chat")
    bounded_management = role == "management"
    return _run_with_pool(
        role,
        payload,
        manual_key=(manual_key or "").strip() or None,
        parse=parse,
        timeout=MANAGEMENT_TEXT_TIMEOUT if bounded_management else None,
        deadline_seconds=(
            MANAGEMENT_TEXT_DEADLINE_SECONDS if bounded_management else None
        ),
        log_cb=log_cb,
        model_override=model_override,
        reasoning_task=reasoning_task or (
            "customer_chat" if role == "chat" else "reporting_summary"
        ),
    )


def _gemini_analyze(audio_bytes: bytes, mime: str, manager_context: str, manager_snapshot: str = "") -> dict:
    """Шле аудіо в Gemini (роль management) з ретраями та фолбеком моделей/ключів."""
    payload = _build_payload(audio_bytes, mime, manager_context, manager_snapshot)
    return _run_with_pool(
        "management", payload, reasoning_task="customer_intelligence"
    )


def _build_payload(audio_bytes: bytes, mime: str, manager_context: str, manager_snapshot: str = "") -> dict:
    text = "Проаналізуй цей запис телефонної розмови за наданою рубрикою. "
    if manager_context.strip():
        text += (
            "Додатковий B2B-контекст від менеджера (підхід до клієнта, його потреби, "
            "домовленості) — врахуй його при оцінці:\n" + manager_context.strip() + "\n\n"
        )
    else:
        text += "Додаткового контексту від менеджера немає.\n\n"
    if manager_snapshot.strip():
        text += (
            "СНІМОК CRM (що менеджер зафіксував після дзвінка) — порівняй із реальною "
            "розмовою і заповни discrepancies:\n" + manager_snapshot.strip()
        )
    else:
        text += "Снімку CRM немає — discrepancies поверни як []."
    user_parts = [
        {"text": text},
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio_bytes).decode()}},
    ]
    return {
        "contents": [{"role": "user", "parts": user_parts}],
        "system_instruction": {"parts": [{"text": _build_system_instruction()}]},
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    }


def _provider_error_details(response) -> dict:
    """Extract bounded provider classifications and typed RetryInfo/QuotaFailure."""
    try:
        payload = response.json()
    except (TypeError, ValueError, AttributeError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return {
            "summary": "UNKNOWN",
            "provider_reason": "UNKNOWN",
            "quota_scope": "",
            "retry_after_seconds": 0,
            "provider_quota_metric": "",
            "provider_quota_id": "",
            "provider_quota_dimensions": {},
            "error_code": 0,
            "error_status": "",
            "field_path": "",
        }
    parts = []
    status = str(error.get("status") or "").strip()
    if status:
        parts.append(status[:80])
    quota_scope = ""
    retry_after_seconds = 0
    provider_quota_metric = ""
    provider_quota_id = ""
    provider_quota_dimensions: dict[str, str] = {}
    message_class = str(error.get("message") or "").casefold()
    compact_message = message_class.replace("_", "").replace(" ", "")
    if (
        "prepayment" in message_class
        or "creditsaredepleted" in compact_message
        or "billingaccount" in compact_message
    ):
        quota_scope = "topup"
    elif "perday" in compact_message:
        quota_scope = "day"
    elif (
        "perminute" in compact_message
        or "tokensperminute" in compact_message
        or "requestsperminute" in compact_message
    ):
        quota_scope = "minute"
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        reason = str(detail.get("reason") or "").strip()
        if reason and reason not in parts:
            parts.append(reason[:80])
        retry_delay = str(detail.get("retryDelay") or "").strip()
        if retry_delay:
            parts.append(f"retryDelay={retry_delay[:24]}")
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", retry_delay)
            if match:
                retry_after_seconds = int(float(match.group(1))) + 2
        for violation in detail.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            quota_id = str(violation.get("quotaId") or "").strip()
            quota_metric = str(violation.get("quotaMetric") or "").strip()
            if quota_metric and re.fullmatch(r"[A-Za-z0-9_.:-]{1,16}", quota_metric):
                provider_quota_metric = quota_metric
            if quota_id and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", quota_id):
                provider_quota_id = quota_id
            dimensions = violation.get("quotaDimensions")
            if isinstance(dimensions, dict):
                allowed_dimension_names = {"model", "location", "region", "tier"}
                provider_quota_dimensions = {
                    str(key)[:40]: str(value)[:80]
                    for key, value in list(dimensions.items())[:8]
                    if str(key).casefold() in allowed_dimension_names
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", str(key))
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(value))
                }
            normalized = quota_id.casefold().replace("_", "").replace("-", "")
            if quota_scope != "topup" and "perday" in normalized:
                quota_scope = "day"
                provider_quota_metric = provider_quota_metric or "rpd"
            elif quota_scope != "topup" and not quota_scope and (
                "perminute" in normalized
                or "tokensperminute" in normalized
                or "requestsperminute" in normalized
            ):
                quota_scope = "minute"
            if "tokensperminute" in normalized:
                provider_quota_metric = "tpm"
            elif "requestsperminute" in normalized:
                provider_quota_metric = "rpm"
            if quota_id:
                parts.append(f"quota={quota_id[:64]}")
    if not quota_scope and status == "RESOURCE_EXHAUSTED":
        quota_scope = "unknown"
    summary = ":".join(dict.fromkeys(parts))[:240] or "UNKNOWN"
    # ЭА.20: до цієї правки причина 400 зводилась до рядка `INVALID_ARGUMENT` —
    # достатньо, щоб порахувати відказ, і недостатньо, щоб назвати поле. Тому
    # окремо (і лише окремо) дістаються три обмежені факти: код, статус і шлях
    # поля з `details[].fieldViolations[].field`. Тіло цілком не зберігається.
    from management.services.gemini_payload_contract import bounded_error_facts

    facts = bounded_error_facts(error)
    return {
        "summary": summary,
        "provider_reason": status[:80] or "UNKNOWN",
        "quota_scope": quota_scope,
        "retry_after_seconds": retry_after_seconds,
        "provider_quota_metric": provider_quota_metric,
        "provider_quota_id": provider_quota_id,
        "provider_quota_dimensions": provider_quota_dimensions,
        "error_code": facts["code"],
        "error_status": facts["status"],
        "field_path": facts["field"],
    }


def _safe_provider_error_summary(response) -> str:
    """Backward-compatible bounded summary for non-quota errors."""
    return str(_provider_error_details(response)["summary"])


PROVIDER_REQUEST_MAX_BYTES = 20_000_000


def _inline_part_count(payload: dict) -> int:
    return sum(
        1
        for content in payload.get("contents") or []
        if isinstance(content, dict)
        for part in content.get("parts") or []
        if isinstance(part, dict) and "inline_data" in part
    )


def _final_provider_body(payload: dict) -> tuple[bytes, int, int]:
    """Serialize the final normalized payload and fail before network if large."""
    body = json.dumps(payload).encode("utf-8")
    if len(body) > PROVIDER_REQUEST_MAX_BYTES:
        raise _GeminiFatal("provider payload exceeds 20,000,000 bytes")
    return body, _inline_part_count(payload), 0


def _final_inline_content_hashes(body: bytes) -> list[str]:
    """Observe inline bytes in the exact serialized request passed to HTTP."""
    import base64
    import hashlib

    try:
        payload = json.loads(body)
        return [
            hashlib.sha256(base64.b64decode(part["inline_data"]["data"], validate=True)).hexdigest()
            for content in payload.get("contents") or []
            if isinstance(content, dict)
            for part in content.get("parts") or []
            if isinstance(part, dict) and "inline_data" in part
        ]
    except (TypeError, ValueError, KeyError) as exc:
        raise _GeminiFatal("final provider inline body is invalid") from exc


def _gemini_call_once(model: str, payload: dict, key: str, *, parse: bool = True,
                      timeout: tuple | None = None, attempt_boundary=None,
                      dispatch_budget=None,
                      dispatch_scarce: bool | None = None,
                      defer_attempt_success: bool = False) -> tuple:
    """Один виклик generateContent. Повертає (parsed_json|text, usage) або кидає
    типізовану помилку (_GeminiTransient / _Gemini429 / _GeminiModelUnavailable / _GeminiFatal).
    parse=False → повертає сирий текст замість JSON (для діалогового бота)."""
    url = f"{GENAI_BASE}/models/{model}:generateContent"
    # ЭА.20: контракт перевіряється ДО серіалізації тіла. Недопустиме тіло не має
    # доходити до провайдера взагалі — 400 коштує повного ходу клієнта, а причина
    # (недокументоване ключове слово схеми) відома нам заздалегідь. Тут же
    # застосовується заборона повторити варіант, який уже отримав 400.
    from management.services import gemini_payload_contract

    payload, contract_report = gemini_payload_contract.guard_payload(
        payload, model=model
    )
    if contract_report.blocked:
        error = _GeminiFatal(
            "HTTP 400: payload contract variant already rejected "
            f"({contract_report.reason})"
        )
        error.http_code = 400
        error.provider_reason = "INVALID_ARGUMENT"
        error.contract_fingerprint = contract_report.fingerprint
        error.contract_detail = f"contract:{contract_report.fingerprint}:{contract_report.reason}"[:120]
        if attempt_boundary is not None:
            attempt_boundary.cancelled_pre_dispatch(error)
        raise error
    try:
        body, request_inline_count, request_trimmed_inline = _final_provider_body(payload)
        request_inline_content_hashes = _final_inline_content_hashes(body)
        if len(request_inline_content_hashes) != request_inline_count:
            raise _GeminiFatal("final provider inline count is invalid")
    except _GeminiFatal as error:
        if attempt_boundary is not None:
            attempt_boundary.cancelled_pre_dispatch(error)
        raise
    if attempt_boundary is not None:
        admitted = attempt_boundary.before_provider(
            serialized_bytes=len(body),
            inline_count=request_inline_count,
        )
        if admitted is not True:
            block_reason = str(
                getattr(attempt_boundary, "provider_block_reason", "") or ""
            )
            error = _GeminiAdmissionRejected(
                "provider boundary rejected",
                reason=block_reason or "stale_provider_boundary",
            )
            attempt_boundary.cancelled_pre_dispatch(error)
            raise error
    if dispatch_budget is not None and not dispatch_budget.consume_dispatch(
        model, scarce=dispatch_scarce
    ):
        reason = dispatch_budget.dispatch_block_reason(
            model, scarce=dispatch_scarce
        )
        error = _GeminiDispatchBudgetExhausted(
            "actual provider dispatch budget exhausted",
            reason=reason or "provider_dispatch_budget",
        )
        if attempt_boundary is not None:
            attempt_boundary.cancelled_pre_dispatch(error)
        raise error
    try:
        from management.services.ig_db_circuit import release_idle_connection

        release_idle_connection()
        resp = requests.post(
            url,
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            timeout=timeout or GEMINI_TIMEOUT,
        )
    except requests.Timeout as exc:
        error = _GeminiTransient(f"timeout: {exc}")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error from exc
    except requests.RequestException as exc:
        error = _GeminiTransient(f"transport: {exc}")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error from exc

    code = resp.status_code
    if code != 200:
        details = _provider_error_details(resp)
        summary = str(details["summary"])
        if code == 408 or 500 <= code < 600:
            error = _GeminiTransient(f"HTTP {code}: {summary}")
            error.http_code = code
            error.provider_reason = str(details["provider_reason"])
            if attempt_boundary is not None:
                attempt_boundary.failed(error)
            raise error
        if code == 429:
            error = _Gemini429(
                summary,
                scope=str(details["quota_scope"]),
                retry_after_seconds=int(details["retry_after_seconds"] or 0),
                provider_reason=str(details["provider_reason"]),
                provider_quota_metric=str(details["provider_quota_metric"]),
                provider_quota_id=str(details["provider_quota_id"]),
                provider_quota_dimensions=details["provider_quota_dimensions"],
            )
            if attempt_boundary is not None:
                attempt_boundary.failed(error)
            raise error
        if code in (404, 403):
            error = _GeminiModelUnavailable(f"HTTP {code}: {summary}")
            error.http_code = code
            error.provider_reason = str(details["provider_reason"])
            if attempt_boundary is not None:
                attempt_boundary.failed(error)
            raise error
        # 400 та інші — проблема нашого запиту.
        error = _GeminiFatal(f"HTTP {code}: {summary}")
        error.http_code = code
        error.provider_reason = str(details["provider_reason"])
        error.error_status = str(details["error_status"])
        error.schema_field_path = str(details["field_path"])
        error.contract_fingerprint = contract_report.fingerprint
        # ЭА.20: 400 — доказ саме для ЦЬОГО варіанта тіла. Подія відкриває circuit
        # варіанта (а не провайдера) і фіксує обмежену атрибуцію: без неї «точне
        # поле не доказано» лишалось би вічним висновком розбору.
        if code == 400 and contract_report.has_contract:
            gemini_payload_contract.record_invalid_payload(
                fingerprint=contract_report.fingerprint,
                variant=contract_report.variant,
                model=model,
                facts={
                    "code": int(details["error_code"] or code),
                    "status": str(details["error_status"]),
                    "field": str(details["field_path"]),
                },
                unsupported=contract_report.unsupported,
            )
        error.contract_detail = (
            f"contract:{contract_report.fingerprint}:"
            f"{str(details['field_path']) or str(details['error_status']) or 'unattributed'}"
        )[:120]
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error

    try:
        data = resp.json()
    except ValueError as exc:
        error = _GeminiTransient("невалідний JSON-конверт")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error from exc

    if not isinstance(data, dict):
        error = _GeminiTransient("invalid JSON envelope shape")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error

    candidates = data.get("candidates") or []
    if not isinstance(candidates, list):
        error = _GeminiTransient("invalid JSON envelope candidates")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error
    cand = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    content = cand.get("content") or {}
    if not isinstance(content, dict):
        error = _GeminiTransient("invalid JSON envelope content")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error
    parts = content.get("parts") or []
    if not isinstance(parts, list):
        error = _GeminiTransient("invalid JSON envelope parts")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error
    # Thought parts are provider-internal and must never leak into a customer
    # answer, JSON parser, logs, or CRM memory.
    text = "".join(
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
    ).strip()
    if not text:
        # Порожньо: часто finishReason=MAX_TOKENS/STOP, коли thinking зʼїв бюджет
        # виводу. Це проблема запиту, а не перевантаження моделі → _GeminiEmpty.
        reason = cand.get("finishReason") or "невідомо"
        error = _GeminiEmpty(f"порожня відповідь (finishReason={reason})")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error

    if parse:
        try:
            parsed = _parse_model_json(text)
        except CallAIAnalysisError as exc:
            # Невалідний/обрізаний JSON (часто у grounded без json-mime) — трактуємо
            # як порожній: ретрай тієї ж комбінації, далі наступний ключ. Не fatal.
            error = _GeminiEmpty(f"unparseable JSON: {exc}")
            if attempt_boundary is not None:
                attempt_boundary.failed(error)
            raise error from exc
    else:
        parsed = text
    raw_usage = data.get("usageMetadata") or {}
    if not isinstance(raw_usage, dict):
        error = _GeminiTransient("invalid JSON envelope usage")
        if attempt_boundary is not None:
            attempt_boundary.failed(error)
        raise error
    usage = dict(raw_usage)
    usage["_finish_reason"] = str(cand.get("finishReason") or "")[:32]
    usage["_request_inline_count"] = request_inline_count
    usage["_request_inline_content_hashes"] = request_inline_content_hashes
    usage["_request_trimmed_inline"] = request_trimmed_inline
    usage["_request_serialized_bytes"] = len(body)
    if attempt_boundary is not None and not defer_attempt_success:
        attempt_boundary.succeeded(usage)
    return parsed, usage


def _parse_model_json(text: str) -> dict:
    """Парсить JSON від моделі, страхуючись від ```json-фенсів, зайвого тексту,
    grounding-цитат та trailing-ком (часті помилки моделі у grounded-режимі)."""
    t = text.strip()
    if t.startswith("```"):
        # прибрати ```json ... ```
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    candidates = [t]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(t[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except ValueError:
            # прибрати trailing-коми перед } або ] (часта помилка LLM)
            cleaned = re.sub(r",(\s*[}\]])", r"\1", cand)
            try:
                return json.loads(cleaned)
            except ValueError:
                continue
    raise CallAIAnalysisError("Не вдалося розпарсити JSON-відповідь моделі.")


# ---------------------------------------------------------------------------
# Нормалізація результату
# ---------------------------------------------------------------------------
def _to_score(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")
    if d < 0:
        d = Decimal("0")
    if d > 100:
        d = Decimal("100")
    return d.quantize(Decimal("0.01"))


def _normalize_verdict(value) -> str:
    v = (str(value or "")).strip().lower()
    if v in {"pass", "coaching", "fail"}:
        return v
    return CallAIAnalysis.Verdict.UNKNOWN


def _as_list(value) -> list:
    if isinstance(value, list):
        return [x for x in value if x not in (None, "")]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _build_manager_snapshot(record: CallRecord) -> str:
    """Текстовий снімок того, що менеджер зафіксував у CRM (для сверки ШІ)."""
    call_dt = record.started_at or record.created_at
    lines = []
    if call_dt:
        lines.append(
            "Дата та час дзвінка: " + timezone.localtime(call_dt).strftime("%Y-%m-%d %H:%M")
            + " (домовленості 'завтра/післязавтра' рахуй від цієї дати)."
        )
    client = record.matched_client
    if not client:
        return "\n".join(lines)
    try:
        lines.append(f"Результат у CRM: {client.get_call_result_display()} ({client.call_result})")
    except Exception:
        lines.append(f"Результат у CRM: {client.call_result}")
    if client.next_call_at:
        lines.append("Наступний дзвінок призначено на: " + timezone.localtime(client.next_call_at).strftime("%Y-%m-%dT%H:%M"))
    else:
        lines.append("Наступний дзвінок НЕ призначено.")
    ctx = client.call_result_context or {}
    if ctx.get("xml_platform") or ctx.get("xml_resource_url"):
        lines.append(f"Позначено XML: {ctx.get('xml_platform', '')} {ctx.get('xml_resource_url', '')}".strip())
    if (client.manager_note or "").strip():
        lines.append("Нотатка менеджера: " + client.manager_note.strip()[:500])
    return "\n".join(lines)


def _normalize_discrepancies(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for d in value:
        if not isinstance(d, dict):
            continue
        sev = (str(d.get("severity") or "info")).strip().lower()
        if sev not in {"info", "warn", "high"}:
            sev = "info"
        out.append({
            "field": str(d.get("field") or "other")[:32],
            "manager_value": str(d.get("manager_value") or "")[:300],
            "ai_value": str(d.get("ai_value") or "")[:300],
            "severity": sev,
            "note": str(d.get("note") or "")[:500],
            "quote": str(d.get("quote") or "")[:300],
        })
    return out


# ---------------------------------------------------------------------------
# Публічний вхід
# ---------------------------------------------------------------------------
def analyze_call(
    general_call_id: str,
    *,
    manager_context: str = "",
    force: bool = False,
    created_by=None,
) -> CallAIAnalysis:
    """Аналізує запис розмови та зберігає CallAIAnalysis. Повертає об'єкт."""
    gcid = (str(general_call_id or "")).strip()
    if not gcid:
        raise CallAIAnalysisError("Потрібен generalCallID.")

    try:
        client = BinotelClient.from_settings()
    except BinotelNotConfigured as exc:
        raise CallAIAnalysisError(str(exc)) from exc

    record = upsert_call_record(client, gcid)

    if not force:
        existing = (
            record.ai_analyses.filter(status=CallAIAnalysis.Status.DONE)
            .order_by("-created_at")
            .first()
        )
        if existing:
            return existing

    analysis = CallAIAnalysis.objects.create(
        call_record=record,
        status=CallAIAnalysis.Status.RUNNING,
        manager_context=(manager_context or "").strip(),
        created_by=created_by if getattr(created_by, "is_authenticated", False) else None,
        model=(gemini_keys.role_model_chains().get("management") or ["gemini-2.5-flash"])[0],
    )

    started = time.monotonic()
    try:
        upstream, _url = client.fetch_record_stream(gcid)
        try:
            audio = upstream.content  # читаємо повністю в памʼять
        finally:
            upstream.close()

        size = len(audio or b"")
        if size <= 0:
            raise CallAIAnalysisError("Порожній аудіофайл запису.")
        if size > MAX_AUDIO_BYTES:
            raise CallAIAnalysisError(
                f"Запис завеликий ({size // (1024*1024)} МБ) для inline-аналізу. "
                "Потрібен Files API (буде додано пізніше)."
            )

        out = _gemini_analyze(audio, "audio/mpeg", analysis.manager_context, _build_manager_snapshot(record))
        parsed = out["parsed"]
        usage = out["usage"]

        analysis.status = CallAIAnalysis.Status.DONE
        analysis.model = out["model"]
        analysis.overall_score = _to_score(parsed.get("overall_score"))
        analysis.verdict = _normalize_verdict(parsed.get("verdict"))
        analysis.transcript = str(parsed.get("transcript") or "")
        analysis.summary = str(parsed.get("summary") or "")
        analysis.client_identification = str(parsed.get("client_identification") or "")
        analysis.axes = parsed.get("axes") if isinstance(parsed.get("axes"), list) else []
        analysis.discussed_well = _as_list(parsed.get("discussed_well"))
        analysis.missed_topics = _as_list(parsed.get("missed_topics"))
        analysis.recommendations = _as_list(parsed.get("recommendations"))
        analysis.extracted_facts = parsed.get("extracted_facts") if isinstance(parsed.get("extracted_facts"), dict) else {}
        analysis.discrepancies = _normalize_discrepancies(parsed.get("discrepancies"))
        analysis.result = parsed if isinstance(parsed, dict) else {"_raw": parsed}
        if isinstance(analysis.result, dict):
            persisted_meta = dict(out.get("meta") or {})
            persisted_meta.pop("request_id", None)
            analysis.result["_meta"] = persisted_meta
        analysis.audio_bytes = size
        analysis.prompt_tokens = int(usage.get("promptTokenCount") or 0)
        analysis.output_tokens = int(
            usage.get("candidatesTokenCount") or usage.get("totalTokenCount") or 0
        )
        analysis.elapsed_ms = int((time.monotonic() - started) * 1000)
        analysis.save()
        record.qa_status = CallRecord.QaStatus.REVIEWED
        record.save(update_fields=["qa_status", "updated_at"])
        try:
            notify_discrepancies(record, analysis)
        except Exception:
            logger.exception("call-ai: notify_discrepancies failed for %s", gcid)
    except (CallAIAnalysisError, BinotelError) as exc:
        analysis.status = CallAIAnalysis.Status.ERROR
        analysis.error = str(exc)[:2000]
        analysis.elapsed_ms = int((time.monotonic() - started) * 1000)
        analysis.save()
        logger.info("call-ai: analysis failed for %s: %s", gcid, exc)
    except Exception as exc:  # будь-яка неочікувана помилка — фіксуємо, не валимо view
        analysis.status = CallAIAnalysis.Status.ERROR
        analysis.error = f"Несподівана помилка: {exc}"[:2000]
        analysis.elapsed_ms = int((time.monotonic() - started) * 1000)
        analysis.save()
        logger.exception("call-ai: unexpected error for %s", gcid)

    return analysis


def serialize_analysis(analysis: CallAIAnalysis) -> dict:
    """Готує словник для JSON-відповіді у тест-вкладку."""
    record = analysis.call_record
    return {
        "id": analysis.id,
        "status": analysis.status,
        "model": analysis.model,
        "overall_score": float(analysis.overall_score),
        "verdict": analysis.verdict,
        "verdict_label": analysis.get_verdict_display(),
        "transcript": analysis.transcript,
        "summary": analysis.summary,
        "client_identification": analysis.client_identification,
        "axes": analysis.axes or [],
        "discussed_well": analysis.discussed_well or [],
        "missed_topics": analysis.missed_topics or [],
        "recommendations": analysis.recommendations or [],
        "manager_context": analysis.manager_context,
        "error": analysis.error,
        "audio_bytes": analysis.audio_bytes,
        "elapsed_ms": analysis.elapsed_ms,
        "prompt_tokens": analysis.prompt_tokens,
        "output_tokens": analysis.output_tokens,
        "created_at": timezone.localtime(analysis.created_at).strftime("%d.%m.%Y %H:%M:%S"),
        "call_record_id": record.id,
        "matched_client_id": record.matched_client_id,
        "manager_name": (
            record.manager.get_full_name() or record.manager.username
            if record.manager_id
            else ""
        ),
    }


# ---------------------------------------------------------------------------
# Попередження менеджеру про розбіжності (Фаза 3)
# ---------------------------------------------------------------------------
_FIELD_LABELS = {
    "next_call": "Час наступного дзвінка",
    "conversion": "Статус клієнта (конверсія)",
    "xml": "Підключення XML",
    "other": "Інше",
}


def notify_discrepancies(record: CallRecord, analysis: CallAIAnalysis) -> None:
    """Створює in-app попередження менеджеру (потребує «ОК»), якщо ШІ знайшов
    значущі розбіжності між розмовою і тим, що зафіксував менеджер.

    Менеджеру НЕ показуємо бали — лише суть розбіжності й що перевірити.
    Ідемпотентно: одне попередження на аналіз.
    """
    from django.conf import settings as dj_settings
    from management.models import ManagerNotification

    manager = record.manager
    if not manager:
        return
    serious = [d for d in (analysis.discrepancies or []) if d.get("severity") in {"warn", "high"}]
    if not serious:
        return
    if ManagerNotification.objects.filter(related_analysis=analysis, requires_ack=True).exists():
        return

    lines = []
    for d in serious[:4]:
        label = _FIELD_LABELS.get(d.get("field"), d.get("field") or "Деталь")
        note = d.get("note") or ""
        mv = d.get("manager_value") or "—"
        av = d.get("ai_value") or "—"
        lines.append(f"• {label}: ви зафіксували «{mv}», а в розмові — «{av}». {note}".strip())
    client = record.matched_client
    title = "Перевірте обробку дзвінка"
    if client:
        title = f"Перевірте дзвінок: {client.shop_name}"[:255]
    body = (
        "ШІ-аналіз розмови знайшов можливі неточності в тому, що ви зберегли:\n"
        + "\n".join(lines)
        + "\n\nПеревірте картку клієнта й виправте за потреби."
    )
    base = (getattr(dj_settings, "MANAGEMENT_BASE_URL", "") or "").rstrip("/")
    action_url = f"{base}/?client={client.id}" if client else ""

    ManagerNotification.objects.create(
        user=manager,
        kind=ManagerNotification.Kind.SYSTEM,
        level=ManagerNotification.Level.WARNING,
        title=title,
        body=body,
        requires_ack=True,
        related_client=client,
        related_analysis=analysis,
        action_url=action_url,
    )


def schedule_call_analysis(general_call_id: str) -> None:
    """Persist an idempotent intent for the bounded analysis command.

    The request path only stores the provider call ID. Binotel polling, audio
    download, Gemini calls, retries, and stale-lock recovery are owned by
    ``run_call_ai_analyses``.
    """
    gcid = (str(general_call_id or "")).strip()
    from management.services.call_auto_analysis import is_call_auto_analysis_enabled

    if not gcid or not is_call_auto_analysis_enabled():
        return
    with transaction.atomic():
        InstagramBotSettings.objects.select_for_update().filter(pk=1).first()
        if not is_call_auto_analysis_enabled():
            return
        record, created = CallRecord.objects.select_for_update().get_or_create(
            provider="binotel",
            external_call_id=gcid,
            defaults={"ai_status": CallRecord.AiStatus.PENDING},
        )
        if (
            not created
            and record.ai_status == CallRecord.AiStatus.NONE
            and analysis_queue_category(record.payload, record.duration_seconds) == METADATA_PENDING
        ):
            CallRecord.objects.filter(
                pk=record.pk,
                ai_status=CallRecord.AiStatus.NONE,
            ).update(
                ai_status=CallRecord.AiStatus.PENDING,
                ai_locked_at=None,
                updated_at=timezone.now(),
            )
