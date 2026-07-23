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
модель для Instagram-чату за замовчуванням gemini-3.6-flash, із керованим
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
import threading
import time
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from management.models import CallAIAnalysis, CallRecord, Client
from management.models import normalize_phone as model_normalize_phone
from management.services import gemini_keys
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
# Жорсткий стеля часу на ВЕСЬ перебір пулу для чату: краще швидко впасти у фолбек/
# ретрай повідомлення, ніж тримати клієнта (і чергу) у «processing» хвилинами.
CHAT_DEADLINE_SECONDS = 75.0
MANAGEMENT_TEXT_DEADLINE_SECONDS = 75.0

# This is an application policy, not a provider claim about token equivalence.
# Gemini 3.x consumes the level; Gemini 2.5 fallbacks consume the budget table.
REASONING_POLICY_VERSION = "2026-07-23.v1"
_REASONING_BUDGETS = {"minimal": 0, "low": 1024, "medium": 4096, "high": 8192}
_REASONING_POLICIES = {
    "health_probe": "low",
    "customer_chat": "medium",
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


def reasoning_policy(task: str) -> dict:
    """Return the validated, versioned reasoning policy for a provider task."""
    key = str(task or "").strip().lower()
    if key not in _REASONING_POLICIES:
        raise ValueError(f"Unknown Gemini reasoning task: {key or '<empty>'}")
    level = _REASONING_POLICIES[key]
    return {
        "task": key,
        "level": level,
        "thinking_budget": _REASONING_BUDGETS[level],
        "policy_version": REASONING_POLICY_VERSION,
    }



class CallAIAnalysisError(Exception):
    """Помилка рівня ШІ-аналізу (конфіг/аудіо/Gemini)."""


class _GeminiTransient(Exception):
    """Тимчасова помилка (503/500/таймаут) — модель перевантажена, ретрай + глобальний overload-кеш."""


class _GeminiEmpty(Exception):
    """Порожня відповідь (finishReason=STOP/MAX_TOKENS без тексту) — проблема цього
    конкретного запиту (мало вихідних токенів через thinking), НЕ перевантаження
    моделі. Ретраїмо ту саму комбінацію, але НЕ метимо модель глобально overloaded."""


class _Gemini429(Exception):
    """429 quota/rate. Викликач вирішує: кулдаун ключа чи пропуск моделі."""


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


def _call_combo(key_name: str, key_value: str, model: str, payload: dict,
                n_attempts: int, grounded: bool, log: list, parse: bool = True,
                timeout: tuple | None = None, log_cb=None) -> tuple[str, dict | None]:
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
        t0 = time.monotonic()
        try:
            policy = reasoning_policy(payload.get("_reasoning_task", "customer_chat"))
            request_payload = _payload_for_model(
                model, payload, reasoning_task=policy["task"]
            )
            request_payload.pop("_reasoning_task", None)
            parsed, usage = _gemini_call_once(
                model, request_payload, key_value, parse=parse, timeout=timeout
            )
        except _GeminiTransient as exc:
            dt = time.monotonic() - t0
            log.append(f"{key_name}/{model}: transient {exc} (#{attempt + 1})")
            _emit(f"{key_name}/{model}: ⚠ перевантаження/503 ({dt:.1f}с) → інша модель")
            gemini_keys.mark_model_overloaded(model)
            if attempt < n_attempts - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
            continue
        except _GeminiEmpty as exc:
            dt = time.monotonic() - t0
            log.append(f"{key_name}/{model}: empty {exc} (#{attempt + 1})")
            _emit(f"{key_name}/{model}: ⚠ порожня відповідь ({dt:.1f}с)")
            if attempt < n_attempts - 1:
                time.sleep(BACKOFF_BASE)
            continue
        except _Gemini429 as exc:
            dt = time.monotonic() - t0
            if gemini_keys.is_key_level_429(model, grounded):
                if track:
                    scope, secs = gemini_keys.parse_429(str(exc))
                    gemini_keys.mark_429(key_name, scope, secs, error=str(exc))
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
            raise CallAIAnalysisError(f"Помилка запиту до Gemini: {exc}")
        else:
            dt = time.monotonic() - t0
            if track:
                gemini_keys.mark_success(key_name)
            log.append(f"{key_name}/{model}: ok")
            _emit(f"{key_name}/{model}: ✅ відповідь за {dt:.1f}с")
            usage = usage if isinstance(usage, dict) else {}
            return ("ok", {
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
            })
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
    payload["_reasoning_task"] = policy["task"]
    log: list[str] = []
    n_attempts = gemini_keys.attempts_per_model(role)
    rounds = gemini_keys.max_rounds(role)
    models = gemini_keys.model_chain(role, model_override)
    call_timeout = timeout or (CHAT_TIMEOUT if role == "chat" else GEMINI_TIMEOUT)
    if deadline_seconds is None:
        deadline_seconds = CHAT_DEADLINE_SECONDS if role == "chat" else None
    t_start = time.monotonic()

    def _over_deadline() -> bool:
        return deadline_seconds is not None and (time.monotonic() - t_start) >= deadline_seconds

    def _emit(msg: str):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    aborted = False
    for round_idx in range(rounds):
        if _over_deadline():
            aborted = True
            break
        if rounds > 1:
            _emit(f"коло {round_idx + 1}/{rounds} (моделі: {', '.join(models)})")

        if manual_key:
            for model in models:
                if _over_deadline():
                    aborted = True
                    break
                if gemini_keys.is_model_overloaded(model):
                    continue
                status, res = _call_combo("(manual)", manual_key, model, payload,
                                          n_attempts, grounded, log, parse, call_timeout, log_cb)
                if status == "ok":
                    return res
                if status == "key_429":
                    break
            if aborted:
                break

        for key_name, key_value, model in gemini_keys.iter_attempts(role):
            if _over_deadline():
                aborted = True
                break
            status, res = _call_combo(key_name, key_value, model, payload,
                                      n_attempts, grounded, log, parse, call_timeout, log_cb)
            if status == "ok":
                return res
        if aborted:
            break

        if round_idx < rounds - 1:
            gemini_keys.clear_model_overload()
            time.sleep(gemini_keys.ROUND_BACKOFF_BASE * (2 ** round_idx))

    if aborted:
        _emit(f"⏱ дедлайн {deadline_seconds:.0f}с вичерпано — припиняю перебір")
        raise CallAIAnalysisError(
            f"Перебір Gemini перервано по дедлайну ({deadline_seconds:.0f}с). Спроби: "
            + "; ".join(log)
        )
    raise CallAIAnalysisError(
        "Усі ключі/моделі Gemini недоступні (квота/перевантаження). Спроби: "
        + "; ".join(log)
    )


def gemini_generate_json(system_instruction: str, user_text: str, *,
                         role: str = "management", max_output_tokens: int = 4096,
                         reasoning_task: str | None = None) -> dict:
    """Текстовий JSON-запит до Gemini. Пул ключів ролі + цепочка моделей."""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
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
                         reasoning_task: str | None = None) -> dict:
    """Текстовий (не-JSON) запит для діалогового бота. Пул ключів ролі + цепочка
    моделей. У result['parsed'] — сирий текст відповіді моделі.
    log_cb (опц.) отримує короткі рядки про кожну спробу (для консолі бота)."""
    bounded_management = role == "management"
    return _run_with_pool(
        role,
        payload,
        manual_key=(manual_key or "").strip() or None,
        parse=False,
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


def _gemini_call_once(model: str, payload: dict, key: str, *, parse: bool = True,
                      timeout: tuple | None = None) -> tuple:
    """Один виклик generateContent. Повертає (parsed_json|text, usage) або кидає
    типізовану помилку (_GeminiTransient / _Gemini429 / _GeminiModelUnavailable / _GeminiFatal).
    parse=False → повертає сирий текст замість JSON (для діалогового бота)."""
    url = f"{GENAI_BASE}/models/{model}:generateContent"
    try:
        resp = requests.post(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            timeout=timeout or GEMINI_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise _GeminiTransient(f"timeout: {exc}") from exc
    except requests.RequestException as exc:
        raise _GeminiTransient(f"transport: {exc}") from exc

    code = resp.status_code
    if code != 200:
        snippet = resp.text[:400]
        if code in (503, 500, 502, 504):
            raise _GeminiTransient(f"HTTP {code}")
        if code == 429:
            raise _Gemini429(snippet)
        if code in (404, 403):
            raise _GeminiModelUnavailable(f"HTTP {code}: {snippet}")
        # 400 та інші — проблема нашого запиту.
        raise _GeminiFatal(f"HTTP {code}: {snippet}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise _GeminiTransient("невалідний JSON-конверт") from exc

    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
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
        raise _GeminiEmpty(f"порожня відповідь (finishReason={reason})")

    if parse:
        try:
            parsed = _parse_model_json(text)
        except CallAIAnalysisError as exc:
            # Невалідний/обрізаний JSON (часто у grounded без json-mime) — трактуємо
            # як порожній: ретрай тієї ж комбінації, далі наступний ключ. Не fatal.
            raise _GeminiEmpty(f"unparseable JSON: {exc}") from exc
    else:
        parsed = text
    usage = dict(data.get("usageMetadata") or {})
    usage["_finish_reason"] = str(cand.get("finishReason") or "")[:32]
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
            analysis.result["_meta"] = out.get("meta") or {}
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


# ---------------------------------------------------------------------------
# Миттєвий фоновий аналіз при збереженні клієнта (без cron, непомітно для менеджера)
# ---------------------------------------------------------------------------
_BG_DEADLINE_SECONDS = 300   # максимум чекаємо готовності запису
_BG_FIRST_DELAY = 20         # перша пауза (даємо дзвінку завершитись)
_BG_MAX_DELAY = 60


def schedule_call_analysis(general_call_id: str) -> None:
    """Запускає аналіз дзвінка у фоновому потоці одразу після збереження клієнта.

    Менеджер не чекає й не бачить процес — відповідь повертається миттєво, а
    розбір зʼявиться згодом (коли провайдер віддасть запис). Безпечно: будь-яка
    помилка глушиться, воркер не падає.
    """
    gcid = (str(general_call_id or "")).strip()
    if not gcid:
        return
    try:
        if not BinotelClient.is_configured():
            return
    except Exception:
        return
    try:
        t = threading.Thread(target=_bg_analyze, args=(gcid,), daemon=True)
        t.start()
    except Exception:
        logger.exception("schedule_call_analysis: failed to start thread")


def _recording_ready(gcid: str) -> bool:
    """Дешева перевірка готовності запису (без створення ERROR-аналізу)."""
    try:
        client = BinotelClient.from_settings()
        data = client.call_record(gcid)
        return bool(client.extract_record_url(data))
    except (BinotelError, BinotelNotConfigured):
        return False
    except Exception:
        return False


def _bg_analyze(gcid: str) -> None:
    from django.db import close_old_connections

    close_old_connections()
    try:
        deadline = time.monotonic() + _BG_DEADLINE_SECONDS
        delay = _BG_FIRST_DELAY
        while time.monotonic() < deadline:
            time.sleep(delay)
            close_old_connections()
            # Уже проаналізовано іншим шляхом? — виходимо.
            rec = CallRecord.objects.filter(provider="binotel", external_call_id=gcid).first()
            if rec and rec.ai_analyses.filter(status=CallAIAnalysis.Status.DONE).exists():
                return
            if _recording_ready(gcid):
                try:
                    analysis = analyze_call(gcid, force=False)
                    if analysis.status == CallAIAnalysis.Status.DONE:
                        CallRecord.objects.filter(
                            provider="binotel", external_call_id=gcid
                        ).update(ai_status=CallRecord.AiStatus.DONE)
                    return
                except Exception:
                    logger.info("bg call analysis failed for %s", gcid)
                    return
            delay = min(_BG_MAX_DELAY, int(delay * 1.5))
    except Exception:
        logger.exception("bg call analysis loop error for %s", gcid)
    finally:
        close_old_connections()
