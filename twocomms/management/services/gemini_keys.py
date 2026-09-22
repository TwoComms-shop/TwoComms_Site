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
import copy
import hashlib
import hmac
import logging
import os
import re
import secrets
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from management.models import GeminiKeyState, GeminiModelState, GeminiRequestAttempt
from management.services import gemini_model_registry

logger = logging.getLogger("management.gemini_keys")

PT = ZoneInfo("America/Los_Angeles")

# Пули ключів за ролями: own (основні) + borrow (позичання у менш пріоритетної ролі).
DEFAULT_ROLE_KEY_POOLS = {
    # Customer replies are the only workload allowed to borrow every configured
    # key. Background CRM/checker work must never consume the two chat-reserved
    # aliases and leave a live Instagram message without an answer.
    "chat": {"own": ["GEMINI_API", "GEMINI_API2"], "borrow": ["GEMINI_API3", "GEMINI_API4", "GEMINI_API5", "GEMINI_API6"]},
    # ЭБ.2: фоновые роли идут по пулу НАВСТРЕЧУ чату — от последнего ключа к
    # первому. Прежде `management` владел API3/API4, то есть ровно тем, что чат
    # занимает первым после своих двух: под нагрузкой они сталкивались сразу.
    # Теперь встреча происходит в середине пула и только под реальным давлением,
    # а первое заимствование чата (API3) — последний резерв фона.
    "management": {"own": ["GEMINI_API6", "GEMINI_API5"], "borrow": ["GEMINI_API4", "GEMINI_API3"]},
    "checker": {"own": ["GEMINI_API6", "GEMINI_API5"], "borrow": ["GEMINI_API4", "GEMINI_API3"]},
}

# Цепочки моделей за ролями — ЛИШЕ безкоштовні моделі, з деградацією до меншої.
# Платні моделі (pro-preview тощо) свідомо НЕ включаємо: на free-tier ключах вони
# завжди дають 429-платно → марна трата запиту й часу (вимога продукту: біллінгові
# моделі одразу пропускати). Якщо пріоритетна модель недоступна — плавно
# спускаємось до меншої безкоштовної (3.7-flash → 3.6-flash → 3.5-flash →
# 2.5-flash-lite). Ротація КЛЮЧІВ (API3→API4→…) — через model-major перебір в
# iter_attempts: пріоритетна модель пробується на ВСІХ ключах перш ніж спуститись.
DEFAULT_ROLE_MODEL_CHAINS = {
    "chat": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"],
    "management": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
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
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
}

CHAT_MODEL_ALLOWLIST = frozenset({
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
})
DEFAULT_CHAT_MODEL = "gemini-3.7-flash"

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
DEFAULT_PROJECT_IDENTITIES = {
    alias: f"gemini-project-{index}"
    for index, alias in enumerate(ALL_KEYS, start=1)
}

MODEL_OVERLOAD_SECONDS = 60    # 503 → модель «перевантажена» ~1 хв (швидке відновлення для чату)
DEFAULT_MINUTE_COOLDOWN = 60   # per-minute 429 без retryDelay
TOPUP_COOLDOWN_SECONDS = 6 * 3600  # платний проект без коштів
KEY_LEASE_SECONDS = 70
# Аудіо-аналіз (роль `management`) свідомо має довший бюджет за чат: розбір
# телефонної розмови триває десятки секунд. Спільний лізинг 70 с обрізав його до
# 62 с read, тому `GEMINI_TIMEOUT = (10, 90)` фізично не міг бути виданий —
# налаштування виглядало діючим і не діяло. Лізинг для довгих ролей береться
# окремою константою, інакше зміна одного числа тихо ламає інше.
LONG_JOB_LEASE_SECONDS = 120
LONG_JOB_ROLES = frozenset({"management", "checker"})


def lease_seconds_for(role: str) -> int:
    """Тривалість лізингу ключа для ролі.

    Чат тримає ключ коротко — його бюджет ходу 35–45 с, і довгий лізинг
    блокував би ключ для інших клієнтів. Фонові ролі з довгими викликами
    (аудіо) потребують більшого, інакше їхній timeout обрізається лізингом.
    """
    return (
        LONG_JOB_LEASE_SECONDS
        if str(role or "") in LONG_JOB_ROLES
        else KEY_LEASE_SECONDS
    )
AUTH_KEY_QUARANTINE_SECONDS = 6 * 3600
PERMISSION_PROJECT_QUARANTINE_SECONDS = 6 * 3600

# In-process кеш перевантажених моделей: {model: datetime_until_utc}.
_model_overload: dict[str, datetime.datetime] = {}

# Модель повернула 429-платно (потрібен біллінг) → позначаємо недоступною на цей
# період і ОДРАЗУ пропускаємо (не б'ємо її на решті ключів і в наступних викликах).
# Cross-process через Django cache + локальний кеш процесу.
PAID_MODEL_SKIP_SECONDS = 30 * 60
_model_unavailable: dict[str, datetime.datetime] = {}

_RETRY_RE = re.compile(
    r'(?:"retryDelay"\s*:\s*"|retryDelay=)(\d+(?:\.\d+)?)s',
    re.IGNORECASE,
)


def key_project_groups() -> dict[str, str]:
    """Return non-secret key-alias -> Google project-group mapping.

    Quotas are project-scoped. Unknown aliases deliberately remain isolated and
    visible as unknown instead of guessing that similarly named keys share a
    project. Configure ``GEMINI_KEY_PROJECT_GROUPS`` as a dict or as
    ``GEMINI_API=project-a,GEMINI_API2=project-a``.
    """
    configured = getattr(settings, "GEMINI_KEY_PROJECT_GROUPS", None)
    if configured is None:
        configured = os.environ.get("GEMINI_KEY_PROJECT_GROUPS", "")
    if isinstance(configured, dict):
        pairs = configured.items()
    else:
        pairs = []
        for part in str(configured or "").split(","):
            alias, separator, group = part.partition("=")
            if separator:
                pairs.append((alias, group))
    # The owner confirmed that the six production aliases are six independent
    # Google projects.  Stable non-secret identities make that contract usable
    # even when an environment-specific mapping was omitted.  Deployments can
    # still override a pair explicitly (for example during key rotation).
    result = dict(DEFAULT_PROJECT_IDENTITIES)
    for alias, group in pairs:
        alias = str(alias or "").strip()
        group = str(group or "").strip()
        if alias in ALL_KEYS and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", group):
            result[alias] = group
    return result


def explicit_project_groups() -> dict[str, str]:
    """Return only operator-configured mappings, never the safe default labels."""
    configured = getattr(settings, "GEMINI_KEY_PROJECT_GROUPS", None)
    if configured is None:
        configured = os.environ.get("GEMINI_KEY_PROJECT_GROUPS", "")
    if isinstance(configured, dict):
        pairs = configured.items()
    else:
        pairs = []
        for part in str(configured or "").split(","):
            alias, separator, group = part.partition("=")
            if separator:
                pairs.append((alias, group))
    result = {}
    for alias, group in pairs:
        alias = str(alias or "").strip()
        group = str(group or "").strip()
        if alias in ALL_KEYS and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", group):
            result[alias] = group
    return result


_CREDENTIAL_HMAC_DOMAIN = b"twocomms/gemini-accounting/credential/v1\0"


def credential_fingerprint(key_value: str | None) -> bytes:
    """Return a domain-separated in-memory HMAC; callers must never persist it."""
    value = str(key_value or "").strip()
    if not value:
        return b""
    configured = str(
        getattr(settings, "GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY", "") or ""
    ).strip()
    fallback = str(getattr(settings, "SECRET_KEY", "") or "")
    key = (configured or fallback).encode("utf-8")
    if not key:
        return b""
    return hmac.new(
        key,
        _CREDENTIAL_HMAC_DOMAIN + value.encode("utf-8"),
        hashlib.sha256,
    ).digest()


def project_group(key_name: str) -> str:
    return key_project_groups().get(key_name, "")


CHAT_RESERVED_ALIASES = frozenset(("GEMINI_API", "GEMINI_API2"))
CHAT_SHARED_RESERVE_ALIASES = frozenset(("GEMINI_API3", "GEMINI_API4"))
CHAT_LAST_RESERVE_ALIASES = frozenset(("GEMINI_API5", "GEMINI_API6"))


def _background_reserved_alias(key_name: str) -> bool:
    """Keep background work off reserved chat aliases and shared identities."""
    if key_name in CHAT_RESERVED_ALIASES:
        return True
    group = project_group(key_name)
    if group and any(project_group(alias) == group for alias in CHAT_RESERVED_ALIASES):
        return True
    value = _key_value(key_name)
    return bool(value and any(value == _key_value(alias) for alias in CHAT_RESERVED_ALIASES))


def manual_key_allowed(role: str, key_value: str | None) -> bool:
    """Prevent persisted background keys from reusing chat-reserved quota."""
    value = str(key_value or "").strip()
    if not value:
        return False
    if role == "chat":
        return True
    aliases = [alias for alias in ALL_KEYS if _key_value(alias) == value]
    return not any(_background_reserved_alias(alias) for alias in aliases)


# ---------------------------------------------------------------------------
# Конфіг (з можливістю override через settings)
# ---------------------------------------------------------------------------
def role_key_pools() -> dict:
    """Return validated pools with live-chat isolation enforced at runtime.

    Deployment settings are allowed to add ordering preferences, but cannot
    reintroduce the two chat-reserved aliases into background roles.  This is
    deliberately enforced after settings loading so a stale cPanel override
    cannot undo the quota boundary.
    """
    configured = getattr(settings, "GEMINI_ROLE_KEY_POOLS", None)
    configured = configured if isinstance(configured, dict) else {}
    pools = copy.deepcopy(DEFAULT_ROLE_KEY_POOLS)
    for role in DEFAULT_ROLE_KEY_POOLS:
        raw = configured.get(role)
        if not isinstance(raw, dict):
            continue
        for tier in ("own", "borrow"):
            values = raw.get(tier)
            if isinstance(values, (list, tuple)):
                pools[role][tier] = list(values) + pools[role][tier]

    def clean(values):
        result = []
        for alias in values:
            alias = str(alias or "").strip()
            if alias in ALL_KEYS and alias not in result:
                result.append(alias)
        return result

    for role, pool in pools.items():
        pool["own"] = clean(pool.get("own", []))
        pool["borrow"] = clean(pool.get("borrow", []))
        pool["borrow"] = [alias for alias in pool["borrow"] if alias not in pool["own"]]
        if role == "chat":
            # Chat always has access to all six configured aliases, with the
            # two primary aliases first. Missing env values are filtered later.
            ordered = clean(DEFAULT_ROLE_KEY_POOLS["chat"]["own"] + pool["own"])
            borrowed = clean(DEFAULT_ROLE_KEY_POOLS["chat"]["borrow"] + pool["borrow"])
            pool["own"] = ordered[:2]
            pool["borrow"] = [alias for alias in borrowed if alias not in pool["own"]]
        else:
            pool["own"] = [alias for alias in pool["own"] if not _background_reserved_alias(alias)]
            pool["borrow"] = [alias for alias in pool["borrow"] if not _background_reserved_alias(alias)]
    return pools


def role_model_chains() -> dict:
    configured = getattr(settings, "GEMINI_ROLE_MODEL_CHAINS", None)
    result = copy.deepcopy(DEFAULT_ROLE_MODEL_CHAINS)
    if isinstance(configured, dict):
        for role, models in configured.items():
            if isinstance(models, (list, tuple)):
                result[str(role)] = list(models)
    # Keep legacy role-chain compatibility. Per-task RoutingDecision owns the
    # actual live/analysis order and never reads this role-level primary.
    chat_models = [
        str(model).strip()
        for model in result.get("chat", [])
        if is_allowed_chat_model(str(model).strip())
    ]
    if not chat_models:
        chat_models = list(DEFAULT_ROLE_MODEL_CHAINS["chat"])
    result["chat"] = [DEFAULT_CHAT_MODEL] + [
        model for model in chat_models if model != DEFAULT_CHAT_MODEL
    ]
    management_models = [
        str(model).strip()
        for model in result.get("management", [])
        if str(model).strip() in FREE_QUOTA_MODELS
    ]
    if not management_models:
        management_models = list(DEFAULT_ROLE_MODEL_CHAINS["management"])
    # Background/management callers have their own chain.  Prepending the
    # legacy chat default here made 3.7 steal the first slot even when the
    # configured management policy explicitly started at 3.8.
    result["management"] = list(dict.fromkeys(management_models))
    return result


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

    scope: 'topup' | 'minute' | 'day' | 'unknown'. Unknown quota accounting is
    conservatively blocked for that project/model until Pacific reset.
    """
    text = body or ""
    low = text.lower()
    compact = low.replace("_", "").replace(" ", "")
    if "prepayment" in low or "creditsaredepleted" in compact or "billingaccount" in compact:
        return ("topup", TOPUP_COOLDOWN_SECONDS)
    per_day = "perday" in compact
    m = _RETRY_RE.search(text)
    if m:
        secs = int(float(m.group(1))) + 2  # +2с запас
        return ("day" if per_day or secs > 3600 else "minute", secs)
    if per_day:
        return ("day", 0)  # без retryDelay → до півночі PT
    if "perminute" in compact:
        return ("minute", DEFAULT_MINUTE_COOLDOWN)
    return ("unknown", 0)


# ---------------------------------------------------------------------------
# Стан ключів
# ---------------------------------------------------------------------------
def _key_value(key_name: str) -> str:
    return (os.environ.get(key_name, "") or "").strip()


def configured_alias_for_secret(key_value: str | None) -> str:
    """Resolve a custom credential to one configured alias without exposing it."""
    candidate = str(key_value or "").strip()
    if not candidate:
        return ""
    candidate_fingerprint = credential_fingerprint(candidate)
    for alias in ALL_KEYS:
        configured = _key_value(alias)
        configured_fingerprint = credential_fingerprint(configured)
        if (
            candidate_fingerprint
            and configured_fingerprint
            and secrets.compare_digest(candidate_fingerprint, configured_fingerprint)
        ):
            return alias
    return ""


def _roll_day(st: GeminiKeyState, now: datetime.datetime) -> None:
    today = now.astimezone(PT).date()
    if st.day_date != today:
        st.day_date = today
        st.requests_today = 0


def _model_cooldown_until(state: GeminiKeyState, model: str):
    """Кулдаун пары (ключ, модель), если он ещё действует."""
    raw = (getattr(state, "model_cooldowns", None) or {}).get(str(model or ""))
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def is_available(
    key_name: str, now: datetime.datetime | None = None, *, model: str = ""
) -> bool:
    """Доступен ли ключ — и, если модель названа, доступна ли эта пара.

    ЭБ.2: без `model` поведение прежнее (кулдаун всего ключа). С моделью
    учитывается ещё и кулдаун пары: 429 по дневной квоте 3.7-flash больше не
    закрывает 3.5-flash-lite на том же ключе, потому что у Google лимит free-tier
    объявлен на модель, а не на проект целиком.
    """
    now = now or timezone.now()
    st = GeminiKeyState.get(key_name)
    if st.cooldown_until and st.cooldown_until > now:
        return False
    if model:
        until = _model_cooldown_until(st, model)
        if until and until > now:
            return False
    return True


def _project_aliases(key_name: str) -> list[str]:
    group = project_group(key_name)
    if not group:
        return [key_name]
    return [alias for alias in ALL_KEYS if project_group(alias) == group]


def _locked_key_states(aliases: list[str]) -> list[GeminiKeyState]:
    for alias in aliases:
        GeminiKeyState.objects.get_or_create(key_name=alias)
    return list(
        GeminiKeyState.objects.select_for_update()
        .filter(key_name__in=aliases)
        .order_by("key_name")
    )


def acquire_key_lease(
    key_name: str,
    *,
    role: str,
    now: datetime.datetime | None = None,
    seconds: int | None = None,
) -> str | None:
    """Claim a key or its known Google-project siblings without provider I/O."""
    if key_name not in ALL_KEYS or not _key_value(key_name):
        return None
    if seconds is None:
        seconds = lease_seconds_for(role)
    now = now or timezone.now()
    aliases = _project_aliases(key_name)
    token = secrets.token_hex(16)
    with transaction.atomic():
        states = _locked_key_states(aliases)
        if any(
            state.lease_token and state.lease_until and state.lease_until > now
            for state in states
        ):
            return None
        lease_until = now + datetime.timedelta(seconds=max(1, int(seconds)))
        for state in states:
            state.lease_token = token
            state.lease_until = lease_until
            state.lease_role = str(role or "")[:20]
            state.save(update_fields=["lease_token", "lease_until", "lease_role", "updated_at"])
    return token


def release_key_lease(key_name: str, token: str) -> bool:
    """Release exactly the lease token that acquired the project group."""
    if key_name not in ALL_KEYS or not token:
        return False
    aliases = _project_aliases(key_name)
    with transaction.atomic():
        states = _locked_key_states(aliases)
        if not states or any(state.lease_token != token for state in states):
            return False
        for state in states:
            state.lease_token = ""
            state.lease_until = None
            state.lease_role = ""
            state.save(update_fields=["lease_token", "lease_until", "lease_role", "updated_at"])
    return True


def ordered_key_candidates(role: str, now: datetime.datetime | None = None) -> list[str]:
    """Return present, uncooled, unleased aliases in priority/sticky order."""
    now = now or timezone.now()
    ordered = _ordered_role_keys(role)
    seen_groups: set[str] = set()
    out: list[str] = []
    for key_name in ordered:
        if not _key_value(key_name) or not is_available(key_name, now):
            continue
        group = project_group(key_name)
        if group and group in seen_groups:
            continue
        st = GeminiKeyState.get(key_name)
        if st.lease_token and st.lease_until and st.lease_until > now:
            continue
        out.append(key_name)
        if group:
            seen_groups.add(group)
    return out


def record_key_success(
    key_name: str,
    *,
    latency_ms: int = 0,
    now: datetime.datetime | None = None,
) -> GeminiKeyState:
    state = mark_success(key_name, now=now)
    latency_ms = max(0, int(latency_ms or 0))
    state.last_http_code = 200
    state.last_failure_kind = ""
    state.consecutive_failures = 0
    if latency_ms:
        state.latency_ewma_ms = latency_ms if not state.latency_ewma_ms else int(
            (state.latency_ewma_ms * 0.7) + (latency_ms * 0.3)
        )
    state.save(update_fields=[
        "last_http_code", "last_failure_kind", "consecutive_failures",
        "latency_ewma_ms", "updated_at",
    ])
    return state


def record_key_failure(
    key_name: str,
    *,
    failure_kind: str,
    http_code: int | None = None,
    latency_ms: int = 0,
    now: datetime.datetime | None = None,
) -> GeminiKeyState:
    now = now or timezone.now()
    state = GeminiKeyState.get(key_name)
    state.last_status = str(failure_kind or "failure")[:32]
    state.last_failure_kind = str(failure_kind or "failure")[:32]
    state.last_http_code = int(http_code) if http_code else None
    state.last_error = state.last_failure_kind
    state.consecutive_failures = min(65535, int(state.consecutive_failures or 0) + 1)
    latency_ms = max(0, int(latency_ms or 0))
    if latency_ms:
        state.latency_ewma_ms = latency_ms if not state.latency_ewma_ms else int(
            (state.latency_ewma_ms * 0.7) + (latency_ms * 0.3)
        )
    state.save(update_fields=[
        "last_status", "last_failure_kind", "last_http_code", "last_error",
        "consecutive_failures", "latency_ewma_ms", "updated_at",
    ])
    return state


def quarantine_key(
    key_name: str,
    *,
    failure_kind: str,
    http_code: int | None = None,
    project_scope: bool = False,
    seconds: int = AUTH_KEY_QUARANTINE_SECONDS,
    now: datetime.datetime | None = None,
) -> GeminiKeyState:
    """Temporarily remove a known-bad key, or its known project, from rotation."""
    now = now or timezone.now()
    aliases = _project_aliases(key_name) if project_scope else [key_name]
    until = now + datetime.timedelta(seconds=max(1, int(seconds)))
    scope = "permission" if project_scope else "auth"
    with transaction.atomic():
        states = _locked_key_states(aliases)
        by_name = {state.key_name: state for state in states}
        for state in states:
            if not state.cooldown_until or state.cooldown_until < until:
                state.cooldown_until = until
            state.cooldown_scope = scope
            state.last_status = str(failure_kind or "quarantined")[:32]
            state.last_failure_kind = str(failure_kind or "quarantined")[:32]
            state.last_http_code = int(http_code) if http_code else None
            state.last_error = state.last_failure_kind
            state.consecutive_failures = min(
                65535, int(state.consecutive_failures or 0) + 1
            )
            state.save(update_fields=[
                "cooldown_until", "cooldown_scope", "last_status",
                "last_failure_kind", "last_http_code", "last_error",
                "consecutive_failures", "updated_at",
            ])
        return by_name[key_name]


def open_model_circuit(
    model: str,
    *,
    reason: str,
    seconds: int = MODEL_OVERLOAD_SECONDS,
    project: str = "",
    now: datetime.datetime | None = None,
) -> GeminiModelState:
    now = now or timezone.now()
    with transaction.atomic():
        state, _created = GeminiModelState.objects.select_for_update().get_or_create(
            model_name=str(model or "")[:80]
        )
        until = now + datetime.timedelta(seconds=max(1, int(seconds)))
        if not state.circuit_until or state.circuit_until < until:
            state.circuit_until = until
        state.circuit_reason = str(reason or "")[:32]
        state.transient_failures = min(65535, int(state.transient_failures or 0) + 1)
        state.last_failure_project = str(project or "")[:80]
        state.last_failure_at = now
        state.save()
    return state


def model_circuit_open(model: str, now: datetime.datetime | None = None) -> bool:
    now = now or timezone.now()
    state = GeminiModelState.objects.filter(model_name=str(model or "")[:80]).first()
    return bool(state and state.circuit_until and state.circuit_until > now)


def record_model_success(model: str, now: datetime.datetime | None = None) -> GeminiModelState:
    now = now or timezone.now()
    state, _created = GeminiModelState.objects.get_or_create(model_name=str(model or "")[:80])
    state.circuit_until = None
    state.circuit_reason = ""
    state.transient_failures = 0
    state.last_ok_at = now
    state.save()
    return state


def record_attempt(
    *,
    request_id: str,
    role: str,
    key_name: str,
    model: str,
    outcome: str,
    failure_kind: str = "",
    http_code: int | None = None,
    provider_reason: str = "",
    decision: str = "",
    latency_ms: int = 0,
    remaining_deadline_ms: int = 0,
    usage: dict | None = None,
    error_detail: str = "",
    attempt_index: int = 0,
    candidate_index: int = 0,
    not_attempted_reason: str = "",
    existing_attempt_id: int | None = None,
) -> GeminiRequestAttempt:
    """Persist only bounded classifications, never the supplied raw detail.

    Lineage ходу (клієнт, source-повідомлення, logical turn, lane) приходить з
    контексту, який задає викликаючий шар: він єдиний знає, що це за хід. Тут
    же реєструється стан деградації провайдера — це єдина точка, куди сходяться
    ВСІ провайдерські спроби всіх ролей, тому інцидент не можна пропустити.
    """
    usage = usage if isinstance(usage, dict) else {}
    from management.services.ig_turn_lineage import current_context

    lineage = current_context()
    group = project_group(key_name)[:80]
    existing_id = max(0, int(existing_attempt_id or 0))
    if existing_id:
        attempt = GeminiRequestAttempt.objects.filter(
            pk=existing_id,
            request_id=str(request_id or "")[:40],
        ).first()
        # The shadow FSM already owns provider outcome, failure, usage and
        # timing. Legacy chat audit may enrich only routing/deadline metadata.
        mutable = {
            "decision": str(decision or "")[:48],
            "remaining_deadline_ms": max(0, int(remaining_deadline_ms or 0)),
        }
        # ЭА.20: атрибуція відказу (контракт payload + поле схеми) — це
        # routing-метадані, а не власність shadow-FSM: він їх не пише взагалі.
        # Без цього рядка причина 400 губилась саме в тому режимі, у якому вона
        # сталась у production, і «точне поле не доказано» лишалось назавжди.
        if str(error_detail or "").strip():
            mutable["error_detail"] = str(error_detail)[:120]
        updated = (
            GeminiRequestAttempt.objects.filter(pk=attempt.pk).update(**mutable)
            if attempt is not None
            else 0
        )
        attempt = (
            GeminiRequestAttempt.objects.get(pk=existing_id)
            if updated
            else None
        )
    else:
        attempt = None
    if attempt is None:
        attempt = GeminiRequestAttempt.objects.create(
            request_id=str(request_id or "")[:40],
            role=str(role or "")[:20],
            key_name=str(key_name or "")[:40],
            project_group=group,
            model=str(model or "")[:80],
            outcome=str(outcome or "")[:24],
            failure_kind=str(failure_kind or "")[:32],
            http_code=int(http_code) if http_code else None,
            provider_reason=str(provider_reason or "")[:80],
            decision=str(decision or "")[:48],
            latency_ms=max(0, int(latency_ms or 0)),
            remaining_deadline_ms=max(0, int(remaining_deadline_ms or 0)),
            prompt_tokens=max(0, int(usage.get("promptTokenCount") or 0)),
            thoughts_tokens=max(0, int(usage.get("thoughtsTokenCount") or 0)),
            candidates_tokens=max(0, int(usage.get("candidatesTokenCount") or 0)),
            error_detail=str(error_detail or failure_kind or "")[:120],
            logical_turn_id=str(lineage.get("logical_turn_id") or "")[:64],
            source_message_id=lineage.get("source_message_id") or None,
            client_id=lineage.get("client_id") or None,
            lane=str(lineage.get("lane") or "")[:16],
            attempt_index=max(0, int(attempt_index or 0)),
            candidate_index=max(0, int(candidate_index or 0)),
            not_attempted_reason=str(not_attempted_reason or "")[:24],
            incident_id=lineage.get("incident_id") or None,
            recovery_job_id=lineage.get("recovery_job_id") or None,
        )
    canonical_runtime_attempt = bool(existing_id and attempt.request_graph_id)
    if not canonical_runtime_attempt or attempt.provider_started_at is not None:
        _register_provider_state(
            role=attempt.role if canonical_runtime_attempt else role,
            outcome=attempt.outcome if canonical_runtime_attempt else outcome,
            failure_kind=(
                attempt.failure_kind if canonical_runtime_attempt else failure_kind
            ),
            http_code=attempt.http_code if canonical_runtime_attempt else http_code,
            model=attempt.model if canonical_runtime_attempt else model,
            project_group=(
                attempt.project_group if canonical_runtime_attempt else group
            ),
            key_name=attempt.key_name if canonical_runtime_attempt else key_name,
            not_attempted_reason=(
                attempt.not_attempted_reason
                if canonical_runtime_attempt
                else not_attempted_reason
            ),
        )
    return attempt


def _register_provider_state(
    *,
    role: str,
    outcome: str,
    failure_kind: str,
    http_code,
    model: str,
    project_group: str,
    key_name: str,
    not_attempted_reason: str = "",
) -> None:
    """Оновити durable стан деградації; телеметрія ніколи не ламає хід."""
    if not_attempted_reason:
        # Кандидат, якого не викликали, не є доказом ані збою, ані здоров'я.
        return
    try:
        from management.services import ig_provider_incidents

        if str(outcome or "") == "succeeded":
            ig_provider_incidents.register_provider_success(role=role)
        else:
            ig_provider_incidents.register_provider_failure(
                role=role,
                failure_kind=failure_kind,
                http_code=http_code,
                model=model,
                project_group=project_group,
                key_name=key_name,
            )
    except Exception:
        logger.debug("provider incident registration unavailable", exc_info=True)


def _active_project_cooldown(
    states: list[GeminiKeyState],
    *,
    now: datetime.datetime,
) -> tuple[datetime.datetime, str] | None:
    active = [
        state for state in states
        if state.cooldown_until and state.cooldown_until > now
    ]
    if not active:
        return None
    strongest = max(active, key=lambda state: state.cooldown_until)
    return strongest.cooldown_until, strongest.cooldown_scope


def mark_success(key_name: str, now: datetime.datetime | None = None) -> GeminiKeyState:
    now = now or timezone.now()
    aliases = _project_aliases(key_name)
    with transaction.atomic():
        states = _locked_key_states(aliases)
        by_name = {state.key_name: state for state in states}
        st = by_name[key_name]
        _roll_day(st, now)
        active = (
            _active_project_cooldown(states, now=now)
            if len(aliases) > 1
            else None
        )
        if active:
            st.cooldown_until, st.cooldown_scope = active
            st.last_status = "ok:project_cooldown"
        else:
            st.cooldown_until = None
            st.cooldown_scope = ""
            st.last_status = "ok"
        st.last_ok_at = now
        st.requests_today = (st.requests_today or 0) + 1
        st.save()
        return st


def _apply_429_state(
    st: GeminiKeyState,
    scope: str,
    seconds: int,
    now: datetime.datetime,
    error: str = "",
    model: str = "",
) -> GeminiKeyState:
    _roll_day(st, now)
    if scope in {"day", "unknown"}:
        reset_at = next_midnight_pt(now)
        retry_at = (
            now + datetime.timedelta(seconds=max(1, int(seconds)))
            if seconds
            else reset_at
        )
        # A PerDay quota cannot reopen before the Pacific calendar reset even
        # when RetryInfo advertises a short generic retry delay.  Conversely a
        # provider delay longer than the reset remains authoritative.
        proposed_until = max(reset_at, retry_at)
    else:
        proposed_until = now + datetime.timedelta(seconds=max(1, int(seconds)))
    # ЭБ.2: минутный и дневной лимиты free-tier объявлены на пару (проект,
    # модель). Кулдаун всего ключа оставляем для `topup`: там закончились деньги
    # проекта, и это действительно про все модели сразу.
    if model and scope in {"day", "minute", "unknown"}:
        cooldowns = dict(getattr(st, "model_cooldowns", None) or {})
        current = _model_cooldown_until(st, model)
        if not current or proposed_until > current:
            cooldowns[str(model)] = proposed_until.isoformat()
        # Заодно выбрасываем истёкшие записи: словарь не должен расти вечно.
        st.model_cooldowns = {
            name: value
            for name, value in cooldowns.items()
            if name == str(model)
            or (_model_cooldown_until(st, name) or now) > now
        }
    elif not st.cooldown_until or proposed_until > st.cooldown_until:
        st.cooldown_until = proposed_until
        st.cooldown_scope = scope
    st.last_status = (
        "429:accounting_unknown" if scope == "unknown" else f"429:{scope}"
    )
    st.last_429_at = now
    if error:
        st.last_error = error[:500]
    st.save()
    return st


def mark_429(key_name: str, scope: str, seconds: int,
             now: datetime.datetime | None = None, error: str = "",
             model: str = "") -> GeminiKeyState:
    """Записать 429. С `model` кулдаун получает пара (ключ, модель) — см. ЭБ.2."""
    now = now or timezone.now()
    aliases = _project_aliases(key_name)
    with transaction.atomic():
        states = _locked_key_states(aliases)
        by_name = {state.key_name: state for state in states}
        for state in states:
            _apply_429_state(state, scope, seconds, now, error=error, model=model)
        return by_name[key_name]


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
def _sticky_order(
    key_names: list[str],
    *,
    states: dict[str, GeminiKeyState] | None = None,
) -> list[str]:
    """Сортує ключі за «липкістю»: останній успішний (last_ok_at) — першим.
    Зберігає вхідний порядок як вторинний критерій (стабільне сортування)."""
    def _last_ok(name):
        st = states.get(name) if states is not None else GeminiKeyState.get(name)
        return (
            st.last_ok_at
            if st is not None and st.last_ok_at
            else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        )
    return sorted(key_names, key=_last_ok, reverse=True)


def _ordered_role_keys(
    role: str,
    *,
    states: dict[str, GeminiKeyState] | None = None,
) -> list[str]:
    """Keep chat reserve tiers intact while preferring recent success within each."""
    pool = role_key_pools().get(role, {"own": [], "borrow": []})
    own = _sticky_order(list(pool.get("own", [])), states=states)
    borrowed = list(pool.get("borrow", []))
    if role != "chat":
        return own + _sticky_order(borrowed, states=states)
    shared = _sticky_order([
        alias for alias in borrowed if alias in CHAT_SHARED_RESERVE_ALIASES
    ], states=states)
    last = _sticky_order([
        alias for alias in borrowed if alias in CHAT_LAST_RESERVE_ALIASES
    ], states=states)
    remaining = _sticky_order([
        alias
        for alias in borrowed
        if alias not in CHAT_SHARED_RESERVE_ALIASES
        and alias not in CHAT_LAST_RESERVE_ALIASES
    ], states=states)
    return own + shared + last + remaining


def iter_attempts(
    role: str,
    model_chain_override: list[str] | None = None,
    *,
    include_skipped: bool = False,
):
    """Генерує (key_name, key_value, model) у порядку пріоритету.

    MODEL-MAJOR: зовнішній цикл — МОДЕЛІ цепочки, внутрішній — КЛЮЧІ. Тобто
    пріоритетну модель (gemini-3.7-flash) пробуємо на ВСІХ ключах (own→borrow,
    sticky-впорядкованих) перш ніж спуститися на нижчу модель. Вимога продукту:
    3.7 має вичерпатись на всьому пулі ключів до переходу на 3.6 та нижчі
    резервні моделі.

    Усередині кожного тиру (own / borrow) — sticky-сортування (останній успішний
    ключ першим), щоб триматись робочого ключа.

    Overload-снапшот фіксується НА ПОЧАТОК проходу: моделі, перевантажені раніше
    (іншими запитами, ще в межах вікна 503), пропускаємо одразу. Але 503, що
    стається ПІД ЧАС цього проходу, НЕ виключає модель на решті ключів — інакше
    перша ж 503 збила б нас із 3.5 на нижчу модель, чого ми й уникаємо.
    Per-key cooldown (429) перевіряється ліниво — вичерпаний ключ пропускаємо.
    """
    models = list(
        model_chain_override if model_chain_override is not None else model_chain(role)
    )
    now = timezone.now()
    pool = role_key_pools().get(role, {"own": [], "borrow": []})
    role_aliases = list(dict.fromkeys(
        [*pool.get("own", ()), *pool.get("borrow", ())]
    ))
    key_states = {
        state.key_name: state
        for state in GeminiKeyState.objects.filter(key_name__in=role_aliases)
    }
    ordered_keys = _ordered_role_keys(role, states=key_states)
    present = [(kn, _key_value(kn)) for kn in ordered_keys]
    present = [(kn, kv) for kn, kv in present if kv]
    overloaded_at_start = {m for m in models if is_model_overloaded(m, now)}
    from management.services import gemini_quota

    aliases = [key_name for key_name, _value in present]
    legacy_quota_snapshot = gemini_quota.capacity_snapshot(
        aliases,
        models,
        now=now,
    )
    explicit_identities = explicit_project_groups()
    ranking_identities = {
        alias: explicit_identities.get(alias, "") for alias in aliases
    }
    v2_ranking_snapshot = {}
    try:
        from management.services import gemini_accounting_runtime

        v2_ranking_snapshot = gemini_accounting_runtime.project_ranking_snapshot(
            project_identities=ranking_identities.values(),
            models=models,
            now=now,
        )
    except Exception:
        # Ranking is advisory. Legacy health/quota order remains the rollback
        # path for mode=off, missing profiles and accounting read failures.
        v2_ranking_snapshot = {}

    values = dict(present)
    for model in models:
        if model in overloaded_at_start:
            if include_skipped:
                for key_name in aliases:
                    yield (key_name, values[key_name], model, "model_overload")
            continue
        if is_model_unavailable(model, timezone.now()):
            if include_skipped:
                for key_name in aliases:
                    yield (key_name, values[key_name], model, "model_unavailable")
            continue  # платна/недоступна модель — пропускаємо повністю
        # ЭБ.4: у пари (ключ, модель) є денний ліміт (20 на старших моделях), тому
        # ключі беремо в порядку ЗАЛИШКУ квоти — інакше перший ключ вичерпується
        # до нуля, а решта проєктів стоїть невикористаною.
        by_remaining = gemini_quota.order_keys_by_remaining(
            aliases,
            model,
            now=now,
            snapshot=legacy_quota_snapshot,
        )
        if v2_ranking_snapshot:
            by_remaining = gemini_accounting_runtime.order_project_aliases(
                by_remaining,
                model=model,
                project_identities=ranking_identities,
                snapshot=v2_ranking_snapshot,
                legacy_snapshot=legacy_quota_snapshot,
            )
        for key_name in by_remaining:
            # Платну модель, виявлену ПІД ЧАС цього проходу (на попередньому ключі),
            # одразу припиняємо пробувати на решті ключів — економимо час/квоту.
            if is_model_unavailable(model, timezone.now()):
                break
            skip_reason = ""
            state = key_states.get(key_name)
            if state is not None and (
                (state.cooldown_until and state.cooldown_until > now)
                or (
                    (model_until := _model_cooldown_until(state, model))
                    and model_until > now
                )
            ):
                skip_reason = "quota_cooldown"
            decision = gemini_accounting_runtime.project_candidate_decision(
                key_name,
                model=model,
                project_identity=ranking_identities.get(key_name, ""),
                snapshot=v2_ranking_snapshot,
                legacy_snapshot=legacy_quota_snapshot,
            ) if v2_ranking_snapshot else {"eligible": True}
            if not skip_reason and not decision["eligible"]:
                skip_reason = str(
                    decision.get("block_reason") or "quota_exhausted"
                )[:24]
            if not skip_reason and not gemini_quota.has_capacity(
                key_name,
                model,
                now=now,
                snapshot=legacy_quota_snapshot,
            ):
                skip_reason = "quota_exhausted"
            if skip_reason:
                if include_skipped:
                    yield (key_name, values[key_name], model, skip_reason)
                continue
            yield (
                (key_name, values[key_name], model, "")
                if include_skipped
                else (key_name, values[key_name], model)
            )


def iter_live_chat_attempts(model_chain_override: list[str] | None = None):
    """Yield live-chat candidates once per known project and open model circuit."""
    for candidate in live_chat_candidate_plan(model_chain_override):
        if not candidate["skip_reason"]:
            yield (
                candidate["key_name"],
                candidate["key_value"],
                candidate["model"],
            )


def live_chat_candidate_plan(model_chain_override: list[str] | None = None) -> list[dict]:
    """Return the complete model-major plan, including durable skip reasons.

    Unlike ``iter_live_chat_attempts`` this projection does not make cooled or
    locally exhausted pairs disappear.  With six configured projects the
    caller therefore receives six rows for every model and can prove whether a
    candidate was called, skipped by policy, or skipped by the live deadline.
    Secret values are used only in-process and must not be serialized.
    """
    models = list(
        model_chain_override
        if model_chain_override is not None
        else model_chain("chat")
    )
    now = timezone.now()
    key_states = {
        state.key_name: state
        for state in GeminiKeyState.objects.filter(key_name__in=ALL_KEYS)
    }
    ordered_aliases = _ordered_role_keys("chat", states=key_states)
    identities = key_project_groups()
    explicit_identities = explicit_project_groups()
    from management.services import gemini_quota

    quota_snapshot = gemini_quota.capacity_snapshot(
        ordered_aliases,
        models,
        now=now,
    )
    ranking_identities = {
        alias: explicit_identities.get(alias, "") for alias in ordered_aliases
    }
    v2_ranking_snapshot = {}
    try:
        from management.services import gemini_accounting_runtime

        v2_ranking_snapshot = gemini_accounting_runtime.project_ranking_snapshot(
            project_identities=ranking_identities.values(),
            models=models,
            now=now,
        )
    except Exception:
        v2_ranking_snapshot = {}
    model_states = {
        state.model_name: state
        for state in GeminiModelState.objects.filter(model_name__in=models)
    }

    def pair_available(key_name: str, model: str) -> bool:
        state = key_states.get(key_name)
        if state is None:
            return True
        if state.cooldown_until and state.cooldown_until > now:
            return False
        model_until = _model_cooldown_until(state, model)
        return not (model_until and model_until > now)

    plan: list[dict] = []
    candidate_index = 0
    for model in models:
        ordered = gemini_quota.order_keys_by_remaining(
            ordered_aliases,
            model,
            now=now,
            snapshot=quota_snapshot,
        )
        if v2_ranking_snapshot:
            ordered = gemini_accounting_runtime.order_project_aliases(
                ordered,
                model=model,
                project_identities=ranking_identities,
                snapshot=v2_ranking_snapshot,
                legacy_snapshot=quota_snapshot,
            )
        seen_projects: set[str] = set()
        seen_credentials: list[bytes] = []
        model_skip = ""
        if is_model_overloaded(model):
            model_skip = "model_overload"
        elif is_model_unavailable(model):
            model_skip = "model_unavailable"
        elif (
            (model_state := model_states.get(model)) is not None
            and model_state.circuit_until
            and model_state.circuit_until > now
        ):
            model_skip = "circuit_open"
        for key_name in ordered:
            candidate_index += 1
            key_value = _key_value(key_name)
            key_fingerprint = credential_fingerprint(key_value)
            identity = str(identities.get(key_name) or "")
            skip_reason = model_skip
            if not key_value:
                skip_reason = "unconfigured"
            elif any(
                secrets.compare_digest(key_fingerprint, existing)
                for existing in seen_credentials
            ) if key_fingerprint else False:
                skip_reason = "duplicate_credential"
            elif identity and identity in seen_projects:
                skip_reason = "duplicate_project"
            elif not skip_reason and not pair_available(key_name, model):
                skip_reason = "quota_cooldown"
            elif not skip_reason and v2_ranking_snapshot:
                decision = gemini_accounting_runtime.project_candidate_decision(
                    key_name,
                    model=model,
                    project_identity=ranking_identities.get(key_name, ""),
                    snapshot=v2_ranking_snapshot,
                    legacy_snapshot=quota_snapshot,
                )
                if not decision["eligible"]:
                    skip_reason = (
                        str(decision.get("block_reason") or "quota_exhausted")[:24]
                    )
            if not skip_reason and not gemini_quota.has_capacity(
                key_name,
                model,
                now=now,
                snapshot=quota_snapshot,
            ):
                skip_reason = "quota_exhausted"
            plan.append({
                "candidate_index": candidate_index,
                "key_name": key_name,
                "key_value": key_value,
                "project_identity": identity,
                "identity_status": (
                    "unconfigured"
                    if not key_value
                    else "duplicate"
                    if skip_reason in {"duplicate_credential", "duplicate_project"}
                    else "known"
                    if key_name in explicit_identities
                    else "assumed"
                ),
                "model": model,
                "skip_reason": skip_reason,
            })
            if identity and key_value:
                seen_projects.add(identity)
            if key_fingerprint and not any(
                secrets.compare_digest(key_fingerprint, existing)
                for existing in seen_credentials
            ):
                seen_credentials.append(key_fingerprint)
    return plan


def task_model_chain(
    role: str,
    reasoning_task: str = "",
    model_override: str | None = None,
) -> list:
    """Цепочка моделей для КОНКРЕТНОЙ задачи, а не для роли целиком (ЭБ.4).

    Прежняя цепочка чата начиналась с 3.7 на каждом ходе. При лимите 20 запросов
    в сутки на пару (проект, модель) это означало, что дневной бюджет лучшей
    модели выгорал на обычных «яка ціна?» до обеда, а сложные решения и разбор
    диалога оставались без неё вообще.

    Квота принадлежит паре (проект, модель), поэтому ВЫБОР МОДЕЛИ и есть средство
    изоляции: обычный ответ живёт на 3.5-flash-lite (500/сутки на ключ), решения —
    на 3.7-flash, разбор — на 3.6-flash, общий резерв — 3.5-flash. Потребители
    физически не могут съесть бюджет друг друга.

    Явный `model_override` уважаем без изменений: он приходит от оператора.
    """
    if model_override:
        return model_chain(role, model_override)
    from management.services import gemini_quota

    chain = [
        model
        for model in gemini_quota.chain_for_task(reasoning_task, role=role)
        if role != "chat" or is_allowed_chat_model(model)
    ]
    return chain or model_chain(role)


def model_quota_pressure(role: str, model: str, now: datetime.datetime | None = None) -> bool:
    """Есть ли по этой модели хотя бы один остывший ключ пула роли (ЭБ.2).

    Признак того, что мы упёрлись в квоту, а не в медленную модель. Проверяем
    именно состояние пула, а не список выживших кандидатов: кандидаты остывшую
    пару уже отфильтровали, и по ним давление невидимо.
    """
    now = now or timezone.now()
    pool = role_key_pools().get(role, {"own": [], "borrow": []})
    for key_name in list(pool.get("own", [])) + list(pool.get("borrow", [])):
        if not _key_value(key_name):
            continue
        if not is_available(key_name, now, model=model):
            return True
    return False


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


def pool_status(now: datetime.datetime | None = None, *, read_only: bool = False) -> list[dict]:
    now = now or timezone.now()
    today_pt = now.astimezone(PT).date()
    explicit_identities = explicit_project_groups()
    if read_only:
        states = {
            state.key_name: state
            for state in GeminiKeyState.objects.filter(key_name__in=ALL_KEYS)
        }
    else:
        states = {key_name: GeminiKeyState.get(key_name) for key_name in ALL_KEYS}
    out = []
    for key_name in ALL_KEYS:
        st = states.get(key_name) or GeminiKeyState(key_name=key_name)
        present = bool(_key_value(key_name))
        # ``available`` is a legacy cooldown-only field.  Consumers that need
        # the current configured/leased truth must use ``health_state`` below.
        available = (not st.cooldown_until) or st.cooldown_until <= now
        project_busy = any(
            sibling.lease_token
            and sibling.lease_until
            and sibling.lease_until > now
            for alias in _project_aliases(key_name)
            if (sibling := states.get(alias)) is not None
        )
        if not present:
            health_state = "unconfigured"
        elif not available:
            health_state = "cooldown"
        elif project_busy:
            health_state = "busy"
        else:
            health_state = "available"
        secs = 0
        if not available and st.cooldown_until:
            secs = max(0, int((st.cooldown_until - now).total_seconds()))
        requests_today = st.requests_today if st.day_date == today_pt else 0
        out.append({
            "key_name": key_name,
            "present": present,
            "role": primary_role_of(key_name),
            "project_group": project_group(key_name),
            "project_identity_known": key_name in explicit_identities,
            "available": available,
            "health_state": health_state,
            "current_status": health_state,
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
