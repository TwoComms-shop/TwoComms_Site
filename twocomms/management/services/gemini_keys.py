"""Менеджер пулів Gemini-ключів.

Розподіл навантаження за ролями з пріоритетним каскадом «основний → страховка →
позичання у менш пріоритетної ролі», цепочки моделей (gen-3 для чату/менеджменту,
2.5 для grounding чекера) та облік квот.

Ключові факти (підтверджені живими тестами + офіційні доки Gemini):
  * Квота рахується НА ПРОЕКТ (не на ключ); денний RPD скидається опівночі
    America/Los_Angeles (PT). Тож 429 уводить у кулдаун весь КЛЮЧ (усі моделі).
  * 503 (overloaded) — модель перевантажена на будь-якому ключі → короткий
    overload-кеш на МОДЕЛЬ, пробуємо наступну модель цепочки.
  * Google Search grounding безкоштовний лише на gemini-2.5-flash.

Ручні ключі (InstagramBotSettings.custom_gemini_key, LeadCheckerSettings.
gemini_api_key) обробляються на рівні викликаючого коду — вони пріоритетніші за пул.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from management.models import GeminiKeyState

logger = logging.getLogger("management.gemini_keys")

PT = ZoneInfo("America/Los_Angeles")

# Пули ключів за ролями: own (основні) + borrow (позичання у менш пріоритетної ролі).
DEFAULT_ROLE_KEY_POOLS = {
    # Own keys remain first; every role may borrow the other configured keys
    # after its own pool is exhausted, so a healthy key is never stranded.
    "chat": {"own": ["GEMINI_API", "GEMINI_API2"], "borrow": ["GEMINI_API3", "GEMINI_API4", "GEMINI_API5", "GEMINI_API6"]},
    "management": {"own": ["GEMINI_API3", "GEMINI_API4"], "borrow": ["GEMINI_API", "GEMINI_API2", "GEMINI_API5", "GEMINI_API6"]},
    "checker": {"own": ["GEMINI_API5", "GEMINI_API6"], "borrow": ["GEMINI_API", "GEMINI_API2", "GEMINI_API3", "GEMINI_API4"]},
}

# Цепочки моделей за ролями — ЛИШЕ безкоштовні моделі, з деградацією до меншої.
# Платні моделі (pro-preview тощо) свідомо НЕ включаємо: на free-tier ключах вони
# завжди дають 429-платно → марна трата запиту й часу (вимога продукту: біллінгові
# моделі одразу пропускати). Якщо пріоритетна модель недоступна — плавно
# спускаємось до меншої безкоштовної (3.6-flash → 3.5-flash → 3.1-flash-lite →
# 2.5-flash-lite). Ротація КЛЮЧІВ (API3→API4→…) — через model-major перебір в
# iter_attempts: пріоритетна модель пробується на ВСІХ ключах перш ніж спуститись.
DEFAULT_ROLE_MODEL_CHAINS = {
    "chat": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "management": ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    # Grounding (Google Search) безкоштовний ЛИШЕ на 2.5-flash / 2.5-flash-lite.
    "checker": ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
}

ATTEMPTS_PER_MODEL = {"chat": 1, "management": 2, "checker": 2}

# Скільки повних кругів перебору ВСІХ ключів×моделей робити перед помилкою.
# Чат — найвищий пріоритет: 3 круги з експоненційним backoff між ними, щоб
# дочекатися відновлення квоти/перевантаження, а не падати після першого круга.
MAX_ROUNDS = {"chat": 3, "management": 2, "checker": 1}
ROUND_BACKOFF_BASE = 2.0  # секунди між кругами: 2, 4, 8...


def attempts_per_model(role: str) -> int:
    return int(ATTEMPTS_PER_MODEL.get(role, 2))


def max_rounds(role: str) -> int:
    return int(MAX_ROUNDS.get(role, 1))

# Моделі з безкоштовною квотою генерації. 429 на НИХ = вичерпана денна квота
# проекту → кулдаун усього КЛЮЧА. 429 на інших (pro-preview тощо) = модель платна
# → це model-level skip, ключ НЕ чіпаємо.
FREE_QUOTA_MODELS = {
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
}

CHAT_MODEL_ALLOWLIST = frozenset({
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
})
DEFAULT_CHAT_MODEL = "gemini-3.6-flash"

# Безкоштовний Google Search grounding доступний лише на цих моделях.
FREE_GROUNDING_MODELS = {"gemini-2.5-flash", "gemini-2.5-flash-lite"}


def is_key_level_429(model: str, grounded: bool) -> bool:
    """Чи означає 429 на (model, grounded) вичерпання квоти КЛЮЧА (проекту).

    True → кулдаун ключа. False → модель просто платна/недоступна free для цієї
    фічі → пропускаємо лише модель, ключ лишаємо доступним.
    """
    if grounded:
        return model in FREE_GROUNDING_MODELS
    return model in FREE_QUOTA_MODELS

ALL_KEYS = ["GEMINI_API", "GEMINI_API2", "GEMINI_API3", "GEMINI_API4", "GEMINI_API5", "GEMINI_API6"]

MODEL_OVERLOAD_SECONDS = 60    # 503 → модель «перевантажена» ~1 хв (швидке відновлення для чату)
DEFAULT_MINUTE_COOLDOWN = 60   # per-minute 429 без retryDelay
TOPUP_COOLDOWN_SECONDS = 6 * 3600  # платний проект без коштів

# In-process кеш перевантажених моделей: {model: datetime_until_utc}.
_model_overload: dict[str, datetime.datetime] = {}

# Модель повернула 429-платно (потрібен біллінг) → позначаємо недоступною на цей
# період і ОДРАЗУ пропускаємо (не б'ємо її на решті ключів і в наступних викликах).
# Cross-process через Django cache + локальний кеш процесу.
PAID_MODEL_SKIP_SECONDS = 30 * 60
_model_unavailable: dict[str, datetime.datetime] = {}

_RETRY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


# ---------------------------------------------------------------------------
# Конфіг (з можливістю override через settings)
# ---------------------------------------------------------------------------
def role_key_pools() -> dict:
    return getattr(settings, "GEMINI_ROLE_KEY_POOLS", None) or DEFAULT_ROLE_KEY_POOLS


def role_model_chains() -> dict:
    return getattr(settings, "GEMINI_ROLE_MODEL_CHAINS", None) or DEFAULT_ROLE_MODEL_CHAINS


def is_allowed_chat_model(model: str) -> bool:
    return (model or "").strip() in CHAT_MODEL_ALLOWLIST


def normalize_chat_model(model: str | None) -> str:
    candidate = (model or "").strip()
    return candidate if is_allowed_chat_model(candidate) else DEFAULT_CHAT_MODEL


def model_chain(role: str, primary_model: str | None = None) -> list[str]:
    """Return a validated chain with an optional authoritative chat primary."""
    base = list(role_model_chains().get(role, ["gemini-2.5-flash"]))
    if role != "chat" or primary_model is None:
        return base
    primary = normalize_chat_model(primary_model)
    return [primary] + [model for model in base if model != primary]


# ---------------------------------------------------------------------------
# Час / скидання квоти
# ---------------------------------------------------------------------------
def next_midnight_pt(now: datetime.datetime | None = None) -> datetime.datetime:
    """Наступна північ America/Los_Angeles у UTC — момент скидання денної квоти."""
    now = now or timezone.now()
    now_pt = now.astimezone(PT)
    tomorrow = (now_pt + datetime.timedelta(days=1)).date()
    midnight_pt = datetime.datetime.combine(tomorrow, datetime.time.min, tzinfo=PT)
    return midnight_pt.astimezone(datetime.timezone.utc)


def parse_429(body: str) -> tuple[str, int]:
    """Класифікує 429 → (scope, seconds).

    Пріоритет — RetryInfo.retryDelay: Google прямо вказує, коли повторити (навіть
    для PerDay-квоти free-tier це часто короткі ~48с rolling-window, а НЕ до
    півночі). Тільки за відсутності retryDelay і явного PerDay — кулдаун до
    next_midnight_pt. 'prepayment' → topup (не відновлюється сам).

    scope: 'topup' | 'minute' (now+seconds) | 'day' (seconds>0 → now+seconds,
    seconds==0 → до півночі PT).
    """
    text = body or ""
    low = text.lower()
    compact = low.replace("_", "").replace(" ", "")
    if "prepayment" in low or "creditsaredepleted" in compact or "billingaccount" in compact:
        return ("topup", TOPUP_COOLDOWN_SECONDS)
    m = _RETRY_RE.search(text)
    if m:
        secs = int(float(m.group(1))) + 2  # +2с запас
        return ("day" if secs > 3600 else "minute", secs)
    if "perday" in compact:
        return ("day", 0)  # без retryDelay → до півночі PT
    if "perminute" in compact:
        return ("minute", DEFAULT_MINUTE_COOLDOWN)
    return ("minute", DEFAULT_MINUTE_COOLDOWN)  # безпечний дефолт (не на весь день)


# ---------------------------------------------------------------------------
# Стан ключів
# ---------------------------------------------------------------------------
def _key_value(key_name: str) -> str:
    return (os.environ.get(key_name, "") or "").strip()


def _roll_day(st: GeminiKeyState, now: datetime.datetime) -> None:
    today = now.astimezone(PT).date()
    if st.day_date != today:
        st.day_date = today
        st.requests_today = 0


def is_available(key_name: str, now: datetime.datetime | None = None) -> bool:
    now = now or timezone.now()
    st = GeminiKeyState.get(key_name)
    if not st.cooldown_until:
        return True
    return st.cooldown_until <= now


def mark_success(key_name: str, now: datetime.datetime | None = None) -> GeminiKeyState:
    now = now or timezone.now()
    st = GeminiKeyState.get(key_name)
    _roll_day(st, now)
    st.cooldown_until = None
    st.cooldown_scope = ""
    st.last_status = "ok"
    st.last_ok_at = now
    st.requests_today = (st.requests_today or 0) + 1
    st.save()
    return st


def mark_429(key_name: str, scope: str, seconds: int,
             now: datetime.datetime | None = None, error: str = "") -> GeminiKeyState:
    now = now or timezone.now()
    st = GeminiKeyState.get(key_name)
    _roll_day(st, now)
    if scope == "day" and not seconds:
        st.cooldown_until = next_midnight_pt(now)
    else:
        st.cooldown_until = now + datetime.timedelta(seconds=max(1, int(seconds)))
    st.cooldown_scope = scope
    st.last_status = f"429:{scope}"
    st.last_429_at = now
    if error:
        st.last_error = error[:500]
    st.save()
    return st


def mark_model_overloaded(model: str, seconds: int = MODEL_OVERLOAD_SECONDS,
                          now: datetime.datetime | None = None) -> None:
    now = now or timezone.now()
    _model_overload[model] = now + datetime.timedelta(seconds=max(1, int(seconds)))


def is_model_overloaded(model: str, now: datetime.datetime | None = None) -> bool:
    now = now or timezone.now()
    until = _model_overload.get(model)
    return bool(until and until > now)


def clear_model_overload() -> None:
    _model_overload.clear()


# ---------------------------------------------------------------------------
# Недоступні (платні / 404) моделі: позначити й одразу пропускати
# ---------------------------------------------------------------------------
def _skip_cache_key(model: str) -> str:
    return f"gemini_model_skip:{model}"


def mark_model_unavailable(model: str, seconds: int = PAID_MODEL_SKIP_SECONDS,
                           now: datetime.datetime | None = None) -> None:
    """Позначити модель недоступною (повернула 429-платно) на `seconds`.
    Зберігаємо і в локальному кеші процесу, і в Django cache (cross-process:
    демон бота, Passenger-воркери, cron бачать однаково)."""
    now = now or timezone.now()
    until = now + datetime.timedelta(seconds=max(1, int(seconds)))
    _model_unavailable[model] = until
    try:
        from django.core.cache import cache
        cache.set(_skip_cache_key(model), until.timestamp(), timeout=max(1, int(seconds)) + 5)
    except Exception:
        pass


def is_model_unavailable(model: str, now: datetime.datetime | None = None) -> bool:
    now = now or timezone.now()
    until = _model_unavailable.get(model)
    if until is not None:
        return until > now
    try:
        from django.core.cache import cache
        ts = cache.get(_skip_cache_key(model))
        if ts:
            return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc) > now
    except Exception:
        pass
    return False


def clear_model_unavailable() -> None:
    try:
        from django.core.cache import cache
        for m in list(_model_unavailable.keys()):
            cache.delete(_skip_cache_key(m))
    except Exception:
        pass
    _model_unavailable.clear()


# ---------------------------------------------------------------------------
# Підбір (key, model) комбінацій
# ---------------------------------------------------------------------------
def _sticky_order(key_names: list[str]) -> list[str]:
    """Сортує ключі за «липкістю»: останній успішний (last_ok_at) — першим.
    Зберігає вхідний порядок як вторинний критерій (стабільне сортування)."""
    def _last_ok(name):
        st = GeminiKeyState.get(name)
        return st.last_ok_at or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return sorted(key_names, key=_last_ok, reverse=True)


def iter_attempts(role: str):
    """Генерує (key_name, key_value, model) у порядку пріоритету.

    MODEL-MAJOR: зовнішній цикл — МОДЕЛІ цепочки, внутрішній — КЛЮЧІ. Тобто
    пріоритетну модель (gemini-3.6-flash) пробуємо на ВСІХ ключах (own→borrow,
    sticky-впорядкованих) перш ніж спуститися на нижчу модель. Вимога продукту:
    «усе, що нижче 3.6 — лише крайній випадок», тож 3.6 має вичерпатись на всьому
    пулі ключів до того, як ми торкнемось 3.1-pro/3.1-flash-lite.

    Усередині кожного тиру (own / borrow) — sticky-сортування (останній успішний
    ключ першим), щоб триматись робочого ключа.

    Overload-снапшот фіксується НА ПОЧАТОК проходу: моделі, перевантажені раніше
    (іншими запитами, ще в межах вікна 503), пропускаємо одразу. Але 503, що
    стається ПІД ЧАС цього проходу, НЕ виключає модель на решті ключів — інакше
    перша ж 503 збила б нас із 3.5 на нижчу модель, чого ми й уникаємо.
    Per-key cooldown (429) перевіряється ліниво — вичерпаний ключ пропускаємо.
    """
    pool = role_key_pools().get(role, {"own": [], "borrow": []})
    models = model_chain(role)
    ordered_keys = _sticky_order(list(pool.get("own", []))) + _sticky_order(list(pool.get("borrow", [])))
    present = [(kn, _key_value(kn)) for kn in ordered_keys]
    present = [(kn, kv) for kn, kv in present if kv]
    overloaded_at_start = {m for m in models if is_model_overloaded(m, timezone.now())}
    for model in models:
        if model in overloaded_at_start:
            continue
        if is_model_unavailable(model, timezone.now()):
            continue  # платна/недоступна модель — пропускаємо повністю
        for key_name, kv in present:
            # Платну модель, виявлену ПІД ЧАС цього проходу (на попередньому ключі),
            # одразу припиняємо пробувати на решті ключів — економимо час/квоту.
            if is_model_unavailable(model, timezone.now()):
                break
            if not is_available(key_name, timezone.now()):
                continue  # ключ у кулдауні (429) → пропускаємо для цієї моделі
            yield (key_name, kv, model)


def has_available_key(role: str, now: datetime.datetime | None = None) -> bool:
    now = now or timezone.now()
    pool = role_key_pools().get(role, {"own": [], "borrow": []})
    for key_name in list(pool.get("own", [])) + list(pool.get("borrow", [])):
        if _key_value(key_name) and is_available(key_name, now):
            return True
    return False


def soonest_cooldown(role: str, now: datetime.datetime | None = None) -> datetime.datetime | None:
    now = now or timezone.now()
    pool = role_key_pools().get(role, {"own": [], "borrow": []})
    times = []
    for key_name in list(pool.get("own", [])) + list(pool.get("borrow", [])):
        if not _key_value(key_name):
            continue
        st = GeminiKeyState.get(key_name)
        if st.cooldown_until and st.cooldown_until > now:
            times.append(st.cooldown_until)
    return min(times) if times else None


# ---------------------------------------------------------------------------
# Статус для UI
# ---------------------------------------------------------------------------
def primary_role_of(key_name: str) -> str:
    for role, pool in role_key_pools().items():
        if key_name in pool.get("own", []):
            return role
    for role, pool in role_key_pools().items():
        if key_name in pool.get("borrow", []):
            return role
    return ""


def pool_status(now: datetime.datetime | None = None) -> list[dict]:
    now = now or timezone.now()
    today_pt = now.astimezone(PT).date()
    out = []
    for key_name in ALL_KEYS:
        st = GeminiKeyState.get(key_name)
        available = (not st.cooldown_until) or st.cooldown_until <= now
        secs = 0
        if not available and st.cooldown_until:
            secs = max(0, int((st.cooldown_until - now).total_seconds()))
        requests_today = st.requests_today if st.day_date == today_pt else 0
        out.append({
            "key_name": key_name,
            "present": bool(_key_value(key_name)),
            "role": primary_role_of(key_name),
            "available": available,
            "cooldown_until": st.cooldown_until.isoformat() if st.cooldown_until else None,
            "cooldown_scope": st.cooldown_scope,
            "seconds_remaining": secs,
            "requests_today": requests_today,
            "last_status": st.last_status,
            "needs_topup": (st.cooldown_scope == "topup") and not available,
            "last_ok_at": st.last_ok_at.isoformat() if st.last_ok_at else None,
            "last_probe_at": st.last_probe_at.isoformat() if st.last_probe_at else None,
            "last_probe_status": st.last_probe_status,
            "last_probe_model": st.last_probe_model,
            "last_probe_latency_ms": st.last_probe_latency_ms,
            "last_probe_finish_reason": st.last_probe_finish_reason,
            "last_probe_http_code": st.last_probe_http_code,
            "last_probe_error": st.last_probe_error,
        })
    return out
