"""ЭА.10 — типізовані класи відказів провайдера: окремі счётчики і circuit.

Чому цей модуль існує окремо від `ig_provider_incidents`.

`_gemini_call_once()` уже давно розрізняє класи відказів — і це добра база. Але
верхній шар зводив їх усіх до одного `provider_outage`, і рішення, видиме
клієнту, приймалось за однією категорією. У production це виглядало так: 429
(квота), 503 (тимчасова недоступність), ReadTimeout (відповідь не прийшла) і 400
(контракт запиту) давали ОДИН і той самий шлях UX. А дії потрібні різні:

    quota           → cooldown області квоти, RetryInfo
    unavailable     → короткий бэкофф і наступний кандидат
    timeout         → питання про наш таймаут, а не про здоров'я провайдера
    connect         → транспорт/DNS: інший кандидат, але не «модель впала»
    empty           → бюджет виводу цього запиту, ретрай тієї ж пари
    invalid_payload → НАШ дефект: заборона повтору тим самим payload (ЭА.20)
    auth            → конфігурація: кейс менеджеру, без ретраїв
    not_found       → конфігурація моделі/проекту: кейс менеджеру, без ретраїв
    unknown         → не вигадувати рішення, тримати окремий счётчик

Два принципові рішення реалізації.

1. Счётчики і circuit ВИВОДЯТЬСЯ з durable журналу спроб (`GeminiRequestAttempt`),
   а не з окремого мутабельного стану. Журнал уже зберігає роль, клас (через
   `failure_kind`/`http_code`), область (модель, alias, project-group) і час, і
   він уже переживає рестарт демона. Похідний стан не можна «розсинхронити»: два
   процеси не зіпсують один одному счётчик, а `timeout` фізично не може
   інкрементувати `unavailable`, бо в кожного класу власна вибірка подій.
   Наслідок, який тут важливий: наблюдаемость безпечна — счётчики пишуться
   завжди, навіть коли прапорець вимикає ПОВЕДІНКУ circuit.

2. Circuit відкривається за ПОРОГОВОЮ політикою (N відказів у вікні), а не за
   одним довільним відказом. Один 503 — це не деградація провайдера, це один
   невдалий виклик; відкривати на ньому circuit означає зупиняти пул на власному
   шумі. Half-open дає РІВНО одну пробну спробу (деталі — ЭА.12).

Класи `invalid_payload`, `auth`, `not_found` не є деградацією ДОСТУПНОСТІ:
провайдер справний, зламані ми або конфігурація. Тому вони не відкривають
circuit провайдера й не мають придушувати holding іншим клієнтам.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

# --- Закритий перелік класів -------------------------------------------------
QUOTA = "quota"
UNAVAILABLE = "unavailable"
TIMEOUT = "timeout"
CONNECT = "connect"
INVALID_PAYLOAD = "invalid_payload"
AUTH = "auth"
NOT_FOUND = "not_found"
EMPTY = "empty"
UNKNOWN = "unknown"

# Перелік закритий: невідомий kind стає `unknown`, а не новим класом. Вісім
# класів прийшли з ЭА.10; `empty` дев'ятий, бо він уже існує в колонці
# `IgProviderIncident.failure_class` і описує реальну окрему причину (порожній
# вивід через з'їдений thinking-бюджет), яку не можна змішувати з `unavailable`.
FAILURE_CLASSES = (
    QUOTA,
    UNAVAILABLE,
    TIMEOUT,
    CONNECT,
    INVALID_PAYLOAD,
    AUTH,
    NOT_FOUND,
    EMPTY,
    UNKNOWN,
)

# Не деградація доступності провайдера: очікування їх не лікує.
NON_AVAILABILITY_CLASSES = frozenset({INVALID_PAYLOAD, AUTH, NOT_FOUND})
# Конфігурація, а не затримка: менеджер, без ретраїв і без клієнтського тексту.
CONFIGURATION_CLASSES = frozenset({AUTH, NOT_FOUND})

# Стани circuit.
CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

SCOPE_GLOBAL = "global"

# Один рядок телеметрії — це «спроба, що дійшла до провайдера і не вдалась».
# `timeout_ambiguous` теж відказ: відповідь не прийшла в бюджет ходу.
FAILED_OUTCOMES = ("failed", "timeout_ambiguous")

# Скільки подій максимум читаємо на один розрахунок: вікна короткі, але один
# інцидент може дати сотні рядків, і запит не має ставати важким.
MAX_EVENTS_PER_WINDOW = 400


def flag(name: str, default: bool = True) -> bool:
    """Feature-флаг етапу: та сама конвенція, що `ig_provider_incidents.flag`.

    Імпорт лінивий, бо `ig_provider_incidents` імпортує цей модуль у своїх
    функціях; на рівні модулів циклу немає ні в одному напрямку.
    """
    from management.services.ig_provider_incidents import flag as _flag

    return _flag(name, default)


# --- Класифікація ------------------------------------------------------------
# Ключі — це ВСІ `failure_kind`, які реально пишуть у телеметрію: живий пул
# (`call_ai_analysis`), shadow-облік (`gemini_accounting_runtime.classify_failure`)
# і діагностичні проби (`gemini_probe`). Якщо перелік розійдеться з рантаймом,
# невідомий kind не зникне — він потрапить у `unknown` і буде видно в счётчику.
_KIND_TO_CLASS = {
    # квота
    "quota_429": QUOTA,
    "quota": QUOTA,
    # недоступність
    "http_5xx": UNAVAILABLE,
    "provider_error": UNAVAILABLE,
    "overloaded": UNAVAILABLE,
    # таймаут
    "read_timeout": TIMEOUT,
    "http_408": TIMEOUT,
    "timeout": TIMEOUT,
    # транспорт
    "transport": CONNECT,
    "transport_error": CONNECT,
    # наш payload
    "invalid_payload": INVALID_PAYLOAD,
    "request_error": INVALID_PAYLOAD,
    "request_too_large": INVALID_PAYLOAD,
    "safety_blocked": INVALID_PAYLOAD,
    # ключ / доступ
    "invalid_key": AUTH,
    "permission_denied": AUTH,
    "forbidden": AUTH,
    "unauthenticated": AUTH,
    # модель/проект не знайдені
    "model_not_found": NOT_FOUND,
    "model_unavailable": NOT_FOUND,
    # порожньо / нерозбірливо
    "empty": EMPTY,
    "invalid_response": EMPTY,
    "malformed_response": EMPTY,
    "reachable_empty": EMPTY,
}


def classify(failure_kind: str = "", http_code=None) -> str:
    """Типізований клас відказу за kind, з падінням на HTTP-код.

    404 навмисно НЕ зводиться до `invalid_payload`: 400 означає, що ми
    надіслали недопустиме тіло (лікується контрактом payload), а 404 — що
    моделі/проекту не існує для цього ключа (лікується конфігурацією і
    людиною). Одна дія на два різні дефекти — це і був дефект верхнього шару.
    """
    kind = str(failure_kind or "").strip().lower()
    mapped = _KIND_TO_CLASS.get(kind)
    if mapped:
        return mapped
    try:
        code = int(http_code) if http_code else 0
    except (TypeError, ValueError):
        code = 0
    if code == 429:
        return QUOTA
    if code in {401, 403}:
        return AUTH
    if code == 404:
        return NOT_FOUND
    if code == 400:
        return INVALID_PAYLOAD
    if code == 408:
        return TIMEOUT
    if 500 <= code < 600:
        return UNAVAILABLE
    return UNKNOWN


# --- Порогова політика на клас ----------------------------------------------
@dataclass(frozen=True)
class ClassPolicy:
    """Політика одного класу: порог, вікно, охолодження і дозволені дії."""

    failure_class: str
    threshold: int
    window_seconds: int
    cooldown_seconds: int
    provider_circuit: bool
    retry_allowed: bool
    manager_case: bool
    customer_notice: bool
    decision: str


# Пороги свідомо різні. 429 приходить за ~0.3 с і кілька 429 підряд справді
# означають вичерпану область квоти. ReadTimeout коштує десятки секунд ходу,
# тому його вікно довше, а охолодження коротше: ми питаємо про НАШ таймаут, а не
# караємо провайдера. Конфігураційні класи мають порог 1: другий виклик з тим
# самим ключем/моделлю нічого не з'ясує, а лише спалить бюджет ходу.
_POLICIES = {
    QUOTA: ClassPolicy(QUOTA, 3, 120, 60, True, True, False, True, "cooldown_quota_scope"),
    UNAVAILABLE: ClassPolicy(UNAVAILABLE, 3, 120, 45, True, True, False, True, "short_backoff"),
    TIMEOUT: ClassPolicy(TIMEOUT, 3, 180, 30, True, True, False, True, "revisit_timeout_budget"),
    CONNECT: ClassPolicy(CONNECT, 3, 120, 30, True, True, False, True, "rotate_transport"),
    # Порожній вивід — проблема бюджету саме цього запиту, тому ретрай тієї ж
    # пари допустимий, а circuit провайдера — ні.
    EMPTY: ClassPolicy(EMPTY, 5, 300, 15, False, True, False, True, "retry_same_pair"),
    # Наш дефект. Circuit відкривається на КОНКРЕТНОМУ варіанті payload (ЭА.20),
    # а не на провайдері, і той самий payload не повторюється ніколи.
    INVALID_PAYLOAD: ClassPolicy(
        INVALID_PAYLOAD, 1, 3600, 0, False, False, False, False, "stop_payload_variant"
    ),
    AUTH: ClassPolicy(AUTH, 1, 900, 300, False, False, True, False, "manager_case_auth"),
    NOT_FOUND: ClassPolicy(
        NOT_FOUND, 1, 900, 300, False, False, True, False, "manager_case_not_found"
    ),
    UNKNOWN: ClassPolicy(UNKNOWN, 5, 300, 30, True, True, False, True, "observe_unknown"),
}


def policy(failure_class: str) -> ClassPolicy:
    return _POLICIES.get(str(failure_class or ""), _POLICIES[UNKNOWN])


@dataclass(frozen=True)
class FailureDecision:
    """Рішення за класом відказу — стабільний код, а не один `provider_outage`."""

    failure_class: str
    decision: str
    retry_allowed: bool
    retry_same_payload: bool
    provider_circuit: bool
    payload_circuit: bool
    manager_case: bool
    customer_notice_allowed: bool
    cooldown_seconds: int


def decide(failure_kind: str = "", http_code=None) -> FailureDecision:
    """Типізоване рішення для одного відказу.

    Функція чиста: вона не читає БД і не залежить від історії. Історію
    (пороги, circuit) додає `circuit_state()`.
    """
    failure_class = classify(failure_kind, http_code)
    rule = policy(failure_class)
    return FailureDecision(
        failure_class=failure_class,
        decision=rule.decision,
        retry_allowed=rule.retry_allowed,
        retry_same_payload=failure_class != INVALID_PAYLOAD and rule.retry_allowed,
        provider_circuit=rule.provider_circuit,
        payload_circuit=failure_class == INVALID_PAYLOAD,
        manager_case=rule.manager_case,
        customer_notice_allowed=rule.customer_notice,
        cooldown_seconds=rule.cooldown_seconds,
    )


# --- Області (scope) ---------------------------------------------------------
# Область записується РАЗОМ з класом, бо «429 на одній парі (проект, модель)» і
# «429 на всіх шести проектах» — це різні події з різними діями. Формат мітки
# збігається з `IgProviderIncident.observed_scopes`, щоб дві підсистеми говорили
# однією мовою.
def scope_label(*, model: str = "", project_group: str = "", key_name: str = "") -> str:
    """Найточніша відома область одного відказу."""
    if key_name:
        return f"alias:{str(key_name)[:40]}"
    if model:
        return f"model:{str(model)[:40]}"
    if project_group:
        return f"project_group:{str(project_group)[:40]}"
    return SCOPE_GLOBAL


def _scope_filter(scope: str) -> dict:
    """Перевести мітку області у фільтр журналу спроб."""
    value = str(scope or SCOPE_GLOBAL)
    if value in {"", SCOPE_GLOBAL}:
        return {}
    prefix, _, rest = value.partition(":")
    rest = rest.strip()
    if not rest:
        return {}
    if prefix == "model":
        return {"model": rest}
    if prefix == "alias":
        return {"key_name": rest}
    if prefix == "project_group":
        return {"project_group": rest}
    # Невідомий префікс не має тихо перетворюватись у глобальну область:
    # інакше вузький запит непомітно порахував би весь трафік роли.
    return {"key_name": value}


def _events(role: str, *, scope: str, since, until=None) -> list:
    """Події одного (role, scope) у вікні — джерело всіх счётчиків і circuit."""
    from management.models import GeminiRequestAttempt

    query = GeminiRequestAttempt.objects.filter(
        role=str(role or "")[:20],
        created_at__gte=since,
        not_attempted_reason="",
        **_scope_filter(scope),
    )
    if until is not None:
        query = query.filter(created_at__lte=until)
    return list(
        query.order_by("-created_at", "-id").values(
            "outcome", "failure_kind", "http_code", "created_at"
        )[:MAX_EVENTS_PER_WINDOW]
    )


def failure_counts(
    role: str = "chat",
    *,
    scope: str = SCOPE_GLOBAL,
    window_seconds: int = 0,
    now=None,
) -> dict:
    """Окремий счётчик на кожен клас — завжди, незалежно від прапорців.

    Повертає ВСІ дев'ять класів, у тому числі нульові: розподіл класів за добу
    (метрика ЭА.10) має бути читабельним без домислів «а чи був цей клас взагалі».
    """
    now = now or timezone.now()
    window = int(window_seconds or 0) or max(
        rule.window_seconds for rule in _POLICIES.values()
    )
    since = now - timedelta(seconds=window)
    counts = {name: 0 for name in FAILURE_CLASSES}
    for event in _events(role, scope=scope, since=since, until=now):
        if str(event["outcome"] or "") not in FAILED_OUTCOMES:
            continue
        counts[classify(event["failure_kind"], event["http_code"])] += 1
    return counts


@dataclass(frozen=True)
class CircuitState:
    """Стан circuit одного (role, class, scope)."""

    role: str
    failure_class: str
    scope: str
    state: str
    failures: int
    threshold: int
    window_seconds: int
    cooldown_seconds: int
    last_failure_at: object = None
    retry_at: object = None
    probe_allowed: bool = False
    reason: str = ""

    @property
    def blocked(self) -> bool:
        """Чи заборонено зараз викликати цю область через цей клас."""
        return self.state == OPEN


def circuit_state(
    role: str = "chat",
    failure_class: str = UNKNOWN,
    *,
    scope: str = SCOPE_GLOBAL,
    now=None,
) -> CircuitState:
    """Circuit по (role, class, scope): відкривається порогом, не одним відказом.

    Стан ВИВОДИТЬСЯ з журналу спроб, тому він однаковий у будь-якому процесі й
    переживає рестарт демона. Послідовність рішень:

    * успіх ПІСЛЯ серії відказів закриває circuit одразу — провайдер відповів;
    * менше `threshold` відказів у вікні — це шум, circuit закритий;
    * порог досягнутий і охолодження ще йде — circuit відкритий;
    * охолодження вийшло — half-open з РІВНО однією пробною спробою: якщо в цій
      області вже була спроба після `retry_at`, проба витрачена.
    """
    now = now or timezone.now()
    failure_class = str(failure_class or UNKNOWN)
    rule = policy(failure_class)
    since = now - timedelta(seconds=rule.window_seconds)
    events = _events(role, scope=scope, since=since, until=now)
    failures = [
        event
        for event in events
        if str(event["outcome"] or "") in FAILED_OUTCOMES
        and classify(event["failure_kind"], event["http_code"]) == failure_class
    ]
    last_failure_at = failures[0]["created_at"] if failures else None

    def _state(state: str, *, reason: str, probe_allowed: bool = False, retry_at=None):
        return CircuitState(
            role=str(role or ""),
            failure_class=failure_class,
            scope=str(scope or SCOPE_GLOBAL),
            state=state,
            failures=len(failures),
            threshold=rule.threshold,
            window_seconds=rule.window_seconds,
            cooldown_seconds=rule.cooldown_seconds,
            last_failure_at=last_failure_at,
            retry_at=retry_at,
            probe_allowed=probe_allowed,
            reason=reason,
        )

    if not rule.provider_circuit:
        # Наш дефект або конфігурація: провайдер справний. Счётчик лишається
        # видимим, але circuit провайдера цей клас не відкриває ніколи.
        return _state(CLOSED, reason="not_a_provider_circuit")
    if not flag("IG_TYPED_FAILURE_CIRCUIT"):
        return _state(CLOSED, reason="circuit_disabled")
    if len(failures) < rule.threshold:
        return _state(CLOSED, reason="below_threshold")
    success_at = next(
        (
            event["created_at"]
            for event in events
            if str(event["outcome"] or "") == "succeeded"
        ),
        None,
    )
    if success_at is not None and last_failure_at is not None and success_at > last_failure_at:
        return _state(CLOSED, reason="success_after_failures")
    retry_at = last_failure_at + timedelta(seconds=rule.cooldown_seconds)
    if now < retry_at:
        return _state(OPEN, reason="threshold_reached", retry_at=retry_at)
    # Half-open віддає РІВНО одну пробну спробу. «Проба витрачена» — це факт із
    # журналу (будь-яка спроба цієї області після `retry_at`), а не окремий
    # мутабельний прапорець, який два процеси могли б спалити двічі.
    probe_used = any(event["created_at"] >= retry_at for event in events)
    if probe_used:
        return _state(OPEN, reason="probe_spent", retry_at=retry_at)
    return _state(
        HALF_OPEN, reason="cooldown_elapsed", probe_allowed=True, retry_at=retry_at
    )


def open_provider_circuits(role: str = "chat", *, scope: str = SCOPE_GLOBAL, now=None) -> list:
    """Усі відкриті circuit роли — по одному на клас, без змішування."""
    now = now or timezone.now()
    states = []
    for failure_class in FAILURE_CLASSES:
        if not policy(failure_class).provider_circuit:
            continue
        state = circuit_state(role, failure_class, scope=scope, now=now)
        if state.state != CLOSED:
            states.append(state)
    return states


# --- Конфігураційний відказ ходу: кейс менеджеру, а не «технічна затримка» ----
def configuration_failure_for_turn(source_message_id, *, role: str = "chat") -> str:
    """Клас конфігураційного відказу, яким ЗАКІНЧИВСЯ цей хід (або "").

    Беремо клас ОСТАННЬОГО невдалого виклику цього ходу, а не «будь-який
    конфігураційний клас у ході». Різниця важлива в обидві сторони:

    * один 403 на одному проекті, а далі 429 на решті — це реальна деградація
      квоти, і клієнт має право на одне технічне повідомлення;
    * timeout на першому кандидаті, а далі 401 на всіх — це конфігурація, і
      «перепрошую за технічну затримку» тут прямий обман: затримки немає, є
      несправний ключ, і чекати марно.

    Саме другий випадок і робив старий шлях: `active_incident()` не бачив
    конфігураційних класів, тому рішення падало у гілку «немає інциденту» і
    клієнт отримував техтекст.
    """
    try:
        message_id = int(source_message_id or 0)
    except (TypeError, ValueError):
        message_id = 0
    if message_id <= 0:
        return ""
    from management.models import GeminiRequestAttempt

    row = (
        GeminiRequestAttempt.objects.filter(
            role=str(role or "")[:20],
            source_message_id=message_id,
            outcome__in=FAILED_OUTCOMES,
            not_attempted_reason="",
        )
        .order_by("-id")
        .values("failure_kind", "http_code")
        .first()
    )
    if not row:
        return ""
    failure_class = classify(row.get("failure_kind") or "", row.get("http_code"))
    return failure_class if failure_class in CONFIGURATION_CLASSES else ""


def open_configuration_manager_case(
    *,
    client_id,
    source_message_id,
    failure_class: str,
    logical_turn_id: str = "",
) -> bool:
    """Ідемпотентно відкрити кейс менеджеру на конфігураційний відказ.

    Придушити техтекст недостатньо: клієнт не має залишитись у тишині через
    зламаний ключ. Кейс створюється РІВНО один на (клієнт, source-повідомлення),
    тому повторний виклик gate у тому самому ході не плодить завдань і алертів.
    Телеметрія й нотифікація ніколи не ламають хід: будь-яка помилка тут
    поглинається, бо вона не має права зробити клієнту гірше, ніж уже є.
    """
    try:
        client_pk = int(client_id or 0)
        message_pk = int(source_message_id or 0)
    except (TypeError, ValueError):
        return False
    if client_pk <= 0 or message_pk <= 0:
        return False
    safe_class = failure_class if failure_class in CONFIGURATION_CLASSES else ""
    if not safe_class:
        return False
    try:
        from management.models import IgFollowUpTask

        reason = f"ig_provider_config:{safe_class}:{message_pk}"[:120]
        _task, created = IgFollowUpTask.objects.get_or_create(
            client_id=client_pk,
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason=reason,
            defaults={
                "due_at": timezone.now(),
                "status": IgFollowUpTask.Status.SKIPPED,
                "skip_reason": "human_agent_required",
                "message_text": (
                    "Конфігурація Gemini не дозволяє відповісти. Потрібна ручна "
                    f"відповідь на повідомлення ID {message_pk}."
                ),
                "last_error": f"provider_configuration:{safe_class}",
            },
        )
        if not created:
            return False
        from management.services.ig_alerts import format_technical_alert
        from management.services.instagram_bot import notify_manager

        notify_manager(
            format_technical_alert(
                "⚠️ IG: конфігурація Gemini блокує відповідь",
                event_type="provider_configuration_failure",
                client_id=client_pk,
                message_id=message_pk,
                failure_kind=safe_class,
                instruction_code="fix_key_or_model",
            ),
            dedupe_key=f"ig_provider_config:{safe_class}:{message_pk}",
            event_type="provider_configuration_failure",
        )
        return True
    except Exception:  # pragma: no cover - телеметрія не ламає хід
        import logging

        logging.getLogger(__name__).debug(
            "configuration manager case unavailable", exc_info=True
        )
        return False
