"""Э0.3 — типизированная воронка терминальных причин lifecycle-событий.

ВЫБРАН **ВАРИАНТ A** — `terminal disposition projection`.

Что это значит буквально: модуль отвечает на вопрос «где стоит **сейчас** каждое
предполагавшееся сообщение клиенту и почему», и НЕ отвечает на вопрос «когда и в
каком порядке оно там оказалось». `IgLifecycleEvent` хранит текущее состояние,
`attempts`, lease, provider ID и последний `last_error`, но не append-only
историю переходов. Из одной строки цепочку «создано → claimed → попытка →
receipt» с временами восстановить нельзя, поэтому:

* каждая корзина несёт `seconds_since_last_transition` (от `updated_at`) —
  это возраст ПОСЛЕДНЕГО перехода, а не доказанное время остановки;
* в отчёте есть явные `measured="current_disposition"` и `history_available=False`;
* ни одно поле отчёта не называется `stopped_at` или `stopped_for_seconds`, чтобы
  проекцию нельзя было случайно прочитать как историю.

Вариант B (append-only transition audit) сознательно не реализован: он нужен
только если A покажет, что причины неоднозначны, а это видно лишь после съёма на
production. Доля строк в бакетах `*_unknown` / `*_unclassified` /
`contradictory_evidence` и есть тот критерий: если она мала — A достаточно.

Почему проекция вычисляется на чтении, а не пишется durable-полем в диспетчере:
типизированное поле отвечало бы только за строки, созданные ПОСЛЕ деплоя, а весь
смысл Э0.3 — измерить уже накопленную production-историю (шаги «снять срез»,
«посчитать долю окна», «посчитать заказы без события»). Обратной засыпки для
причины остановки не существует: её нельзя восстановить, не выдумав. Поэтому
никакой миграции этот пункт не добавляет, а анти-дрейф обеспечивается реестром
`ig_lifecycle.LAST_ERROR_REASONS`: строку `last_error` нельзя завести в
диспетчере, не дав ей типа здесь (это проверяет тест).

Два честных ограничения, зафиксированных в коде, а не в комментарии к отчёту:

1. `state` НЕ является причиной. `manager_review` — это и закрытое окно Meta, и
   истёкший permission-deferral: два разных решения оператора. Поэтому причина
   всегда пара (state, нормализованный last_error) плюс независимая улика —
   маркер провайдерского I/O на строке outbox.
2. Причина, которую данные не подтверждают, не выдумывается. Для этого есть
   отдельные корзины: `unknown` (сочетание не распознано),
   `cancelled_unclassified` (известно, что остановка до провайдера, но не почему),
   `provider_receipt_unknown` и `lease_anomaly_ambiguous` (исход доставки
   недоказуем в обе стороны), `contradictory_evidence` (state и маркер I/O
   противоречат друг другу), `absent_unexplained` (все предпосылки события
   выполняются сейчас, а события нет).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings as django_settings
from django.utils import timezone

from management.models import (
    IgCheckoutProposal,
    IgLifecycleEvent,
    IgOrderAssignment,
    IgOrderAttribution,
    IgPaymentProjection,
    InstagramBotMessage,
)
from management.services import ig_lifecycle
from orders.fulfillment_truth import (
    NOVA_POSHTA_DELIVERY_SUCCESS_CODES,
    nova_poshta_order_fulfillment_confirmed,
)
from orders.models import Order

FUNNEL_FLAG = "IG_LIFECYCLE_REASON_FUNNEL_ENABLED"
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365
# Ключи `synthetic_event_key` подставляются одним `IN`; на production в окне
# может быть несколько тысяч событий, поэтому запрос идёт партиями.
KEY_BATCH_SIZE = 500

MEASURED = "current_disposition"
CAVEAT = (
    "Вариант A: измерено ТЕКУЩЕЕ состояние строки, а не история переходов. "
    "`seconds_since_last_transition` — возраст последнего перехода (updated_at), "
    "он НЕ доказывает, сколько событие стоит на этой причине. Причины отсутствия "
    "события выведены из текущего состояния связей заказа и помечены "
    "`inferred_from_current_state`."
)


def flag(name: str, default: bool = True) -> bool:
    """Прочитать feature-флаг пункта из Django settings (керується .env)."""
    value = getattr(django_settings, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class Stage:
    """Шаги воронки: создано → claimed → попытка → receipt."""

    NOT_CREATED = "not_created"
    CREATED = "created"
    CLAIMED = "claimed"
    BLOCKED_PRE_PROVIDER = "blocked_pre_provider"
    SEND_ATTEMPTED = "send_attempted"
    RECEIPT_UNKNOWN = "receipt_unknown"
    RECEIPT_CONFIRMED = "receipt_confirmed"
    # Шаг остановки недоказуем так же, как и причина: отдельное значение, чтобы
    # неизвестное не приклеивалось к ближайшему по смыслу шагу.
    UNKNOWN = "unknown"


class Evidence:
    """Что именно доказывают данные о доставке, а не о состоянии строки."""

    PROVEN_DELIVERED = "proven_delivered"
    PROVEN_NO_PROVIDER_IO = "proven_no_provider_io"
    # Исход провайдерского обмена недоказуем в обе стороны: либо есть durable
    # маркер I/O без receipt, либо транспорт сам вернул `unknown`.
    AMBIGUOUS_PROVIDER_IO = "ambiguous_provider_io"
    CONTRADICTORY = "contradictory"
    INFERRED_CURRENT_STATE = "inferred_from_current_state"
    UNKNOWN = "unknown"


class Reason:
    DELIVERED = "delivered"
    WINDOW_CLOSED = "window_closed"
    PERMISSION_DEFERRED = "permission_deferred"
    PERMISSION_DEFERRAL_TIMEOUT = "permission_deferral_timeout"
    PERMISSION_DENIED = "permission_denied"
    PAYMENT_NOT_VERIFIED = "payment_not_verified"
    STALE_ASSIGNMENT = "stale_assignment"
    TRACKING_NUMBER_CHANGED = "tracking_number_changed"
    PARCEL_ALREADY_RECEIVED = "parcel_already_received"
    CARRIER_NOT_CONFIRMED = "carrier_delivery_not_confirmed"
    ORDER_MISSING = "order_missing"
    SEND_BOUNDARY_REJECTED = "send_boundary_rejected"
    PROVIDER_PERMANENT_FAILURE = "provider_permanent_failure"
    PROVIDER_RECEIPT_UNKNOWN = "provider_receipt_unknown"
    LEASE_ANOMALY_AMBIGUOUS = "lease_anomaly_ambiguous"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    RETRY_SCHEDULED = "retry_scheduled"
    AWAITING_DISPATCH = "awaiting_dispatch"
    CLAIM_IN_FLIGHT = "claim_in_flight"
    CLAIM_LEASE_EXPIRED = "claim_lease_expired"
    CANCELLED_UNCLASSIFIED = "cancelled_unclassified"
    UNKNOWN = "unknown"

    # Причины ОТСУТСТВИЯ события: выводятся из текущего состояния связей заказа,
    # поэтому всегда несут `inferred_from_current_state`.
    ABSENT_NO_ATTRIBUTION = "absent_no_attribution"
    ABSENT_NO_PROPOSAL = "absent_no_proposal"
    ABSENT_CONTEXT_MISMATCH = "absent_context_mismatch"
    ABSENT_NO_ACTIVE_ASSIGNMENT = "absent_no_active_assignment"
    ABSENT_PAYMENT_NOT_VERIFIED = "absent_payment_not_verified"
    ABSENT_BUSINESS_TRUTH_BLOCKED = "absent_business_truth_blocked"
    ABSENT_UNEXPLAINED = "absent_unexplained"


# Постоянные (не транзиентные) отказы permission-гарда. Источник значений —
# `ig_reply_boundary._client_allowed()` и `capture_reply_permission()`; они
# перечислены здесь, а не импортированы, потому что там это литералы внутри
# функций. Транзиентные живут в `ig_lifecycle.TRANSIENT_PERMISSION_REASONS`.
PERSISTENT_PERMISSION_REASONS = frozenset({
    "blocked",
    "client_missing",
    "hidden",
    "opt_out",
    "sender_not_allowed",
    ig_lifecycle.CUSTOMER_SEND_NOT_ALLOWED_ERROR,
})
# Отказ границы отправки внутри `send_text`: провайдерского запроса не было.
# Источник строк — `instagram_bot.send_text()` (модуль не входит в границу Э0.3).
SEND_BOUNDARY_HINTS = frozenset({
    "provider I/O marker was not committed",
    "provider request boundary rejected the fallback",
})
# Классы исхода доставки (`kind` из `send_text`) → тип причины.
DELIVERY_KIND_REASONS = {
    "permanent": Reason.PROVIDER_PERMANENT_FAILURE,
    "link_restricted": Reason.PROVIDER_PERMANENT_FAILURE,
    "unreachable": Reason.PROVIDER_PERMANENT_FAILURE,
    "unknown": Reason.PROVIDER_RECEIPT_UNKNOWN,
    "transient": Reason.PROVIDER_RECEIPT_UNKNOWN,
    "retryable": Reason.RETRY_SCHEDULED,
}
# Фиксированные строки `last_error` → тип причины. Тест сверяет ключи этой карты
# с реестром `ig_lifecycle.LAST_ERROR_REASONS`: строка, которую диспетчер умеет
# записать, не может остаться без типа.
FIXED_REASON_CODES = {
    ig_lifecycle.STANDARD_RESPONSE_WINDOW_CLOSED: Reason.WINDOW_CLOSED,
    ig_lifecycle.PAYMENT_NOT_VERIFIED_ERROR: Reason.PAYMENT_NOT_VERIFIED,
    ig_lifecycle.STALE_ASSIGNMENT_ERROR: Reason.STALE_ASSIGNMENT,
    ig_lifecycle.TRACKING_NUMBER_CHANGED_ERROR: Reason.TRACKING_NUMBER_CHANGED,
    ig_lifecycle.PARCEL_ALREADY_RECEIVED_ERROR: Reason.PARCEL_ALREADY_RECEIVED,
    ig_lifecycle.CARRIER_DELIVERY_NOT_CONFIRMED_ERROR: Reason.CARRIER_NOT_CONFIRMED,
    ig_lifecycle.ORDER_MISSING_ERROR: Reason.ORDER_MISSING,
    ig_lifecycle.CUSTOMER_SEND_NOT_ALLOWED_ERROR: Reason.PERMISSION_DENIED,
    ig_lifecycle.PROVIDER_MESSAGE_ID_MISSING_ERROR: Reason.PROVIDER_RECEIPT_UNKNOWN,
    ig_lifecycle.LEASE_EXPIRED_AFTER_PROVIDER_IO_ERROR: Reason.PROVIDER_RECEIPT_UNKNOWN,
    ig_lifecycle.PROVIDER_MARKER_BEFORE_LEASE_ERROR: Reason.LEASE_ANOMALY_AMBIGUOUS,
    ig_lifecycle.LEASE_WITHOUT_EXPIRY_ERROR: Reason.LEASE_ANOMALY_AMBIGUOUS,
    ig_lifecycle.LEGACY_LEASE_MARKER_ERROR: Reason.LEASE_ANOMALY_AMBIGUOUS,
    **{
        reason: Reason.PERMISSION_DENIED
        for reason in ig_lifecycle.INVITATION_BLOCK_REASONS
    },
    **{
        reason: Reason.PERMISSION_DEFERRED
        for reason in ig_lifecycle.TRANSIENT_PERMISSION_REASONS
    },
}

_TERMINAL_STATES = frozenset({
    IgLifecycleEvent.State.SENT,
    IgLifecycleEvent.State.MANAGER_REVIEW,
    IgLifecycleEvent.State.AMBIGUOUS,
    IgLifecycleEvent.State.FAILED,
    IgLifecycleEvent.State.CANCELLED,
})
_PRE_PROVIDER_STATES = frozenset({
    IgLifecycleEvent.State.PENDING,
    IgLifecycleEvent.State.WAITING_WINDOW,
    IgLifecycleEvent.State.MANAGER_REVIEW,
    IgLifecycleEvent.State.CANCELLED,
})


@dataclass(frozen=True)
class LifecycleDisposition:
    """Где событие стоит СЕЙЧАС и что об этом доказуемо."""

    reason: str
    stage: str
    evidence: str
    terminal: bool
    detail: str = ""


def _no_io_evidence(provider_io_started) -> str:
    # `False` — маркер искали и не нашли: отправки провайдеру не было.
    # `None` — маркер не смотрели, и отсутствие проверки не является уликой.
    if provider_io_started is False:
        return Evidence.PROVEN_NO_PROVIDER_IO
    return Evidence.UNKNOWN


def _delivery_kind(last_error: str) -> tuple[str, str]:
    head, _, tail = str(last_error or "").partition(":")
    return head.strip(), tail.strip()


def _ambiguous_disposition(norm: str) -> LifecycleDisposition:
    if norm.startswith(ig_lifecycle.PROVIDER_IO_STARTED_PREFIX):
        kind, _ = _delivery_kind(norm[len(ig_lifecycle.PROVIDER_IO_STARTED_PREFIX):])
        return LifecycleDisposition(
            reason=Reason.PROVIDER_RECEIPT_UNKNOWN,
            stage=Stage.RECEIPT_UNKNOWN,
            evidence=Evidence.AMBIGUOUS_PROVIDER_IO,
            terminal=True,
            detail=f"provider_io:{kind}"[:64],
        )
    fixed = FIXED_REASON_CODES.get(norm)
    if fixed is not None and fixed != Reason.PERMISSION_DEFERRED:
        return LifecycleDisposition(
            reason=fixed,
            stage=Stage.RECEIPT_UNKNOWN,
            evidence=Evidence.AMBIGUOUS_PROVIDER_IO,
            terminal=True,
            detail=norm[:64],
        )
    kind, _ = _delivery_kind(norm)
    mapped = DELIVERY_KIND_REASONS.get(kind)
    if mapped is not None:
        return LifecycleDisposition(
            reason=mapped,
            stage=Stage.RECEIPT_UNKNOWN,
            evidence=Evidence.AMBIGUOUS_PROVIDER_IO,
            terminal=True,
            detail=kind[:64],
        )
    return LifecycleDisposition(
        reason=Reason.UNKNOWN,
        stage=Stage.UNKNOWN,
        evidence=Evidence.UNKNOWN,
        terminal=True,
        detail=kind[:64],
    )


def classify_disposition(
    *,
    state,
    last_error: str = "",
    provider_message_id: str = "",
    attempts: int = 0,
    due_at=None,
    lease_expires_at=None,
    provider_io_started=None,
    now=None,
) -> LifecycleDisposition:
    """Чистая функция: строка lifecycle-события → типизированная причина.

    `provider_io_started` трёхзначен сознательно: `True` — durable маркер
    провайдерского I/O есть, `False` — искали и не нашли, `None` — не смотрели.
    Разница между `False` и `None` и есть разница между доказательством и
    предположением, поэтому она не сворачивается в булево.
    """
    state = str(state or "")
    norm = str(last_error or "").strip()
    receipt = str(provider_message_id or "").strip()
    now = now or timezone.now()

    # 1. Единственный положительный исход требует receipt провайдера. `sent` без
    # provider_message_id — противоречие, а не доставка.
    if state == IgLifecycleEvent.State.SENT:
        if receipt:
            return LifecycleDisposition(
                reason=Reason.DELIVERED,
                stage=Stage.RECEIPT_CONFIRMED,
                evidence=Evidence.PROVEN_DELIVERED,
                terminal=True,
            )
        return LifecycleDisposition(
            reason=Reason.CONTRADICTORY_EVIDENCE,
            stage=Stage.RECEIPT_UNKNOWN,
            evidence=Evidence.CONTRADICTORY,
            terminal=True,
            detail="sent_without_receipt",
        )

    # 2. Улика важнее state: маркер I/O на строке, которая по своему состоянию
    # не могла дойти до провайдера, означает противоречие, а не отмену.
    if provider_io_started and state in _PRE_PROVIDER_STATES:
        return LifecycleDisposition(
            reason=Reason.CONTRADICTORY_EVIDENCE,
            stage=Stage.SEND_ATTEMPTED,
            evidence=Evidence.CONTRADICTORY,
            terminal=state in _TERMINAL_STATES,
            detail=f"io_marker_with_{state}"[:64],
        )

    if state == IgLifecycleEvent.State.AMBIGUOUS:
        return _ambiguous_disposition(norm)

    if state == IgLifecycleEvent.State.FAILED:
        kind, _ = _delivery_kind(norm)
        mapped = DELIVERY_KIND_REASONS.get(kind)
        if mapped is None:
            return LifecycleDisposition(
                reason=Reason.UNKNOWN,
                stage=Stage.UNKNOWN,
                evidence=Evidence.UNKNOWN,
                terminal=True,
                detail=kind[:64],
            )
        if mapped == Reason.PROVIDER_RECEIPT_UNKNOWN:
            return LifecycleDisposition(
                reason=mapped,
                stage=Stage.RECEIPT_UNKNOWN,
                evidence=Evidence.AMBIGUOUS_PROVIDER_IO,
                terminal=True,
                detail=kind[:64],
            )
        return LifecycleDisposition(
            reason=mapped,
            stage=Stage.SEND_ATTEMPTED,
            evidence=_no_io_evidence(provider_io_started),
            terminal=True,
            detail=kind[:64],
        )

    if state == IgLifecycleEvent.State.CANCELLED:
        kind, tail = _delivery_kind(norm)
        hint = tail if kind == "cancelled" and tail else norm
        if hint in SEND_BOUNDARY_HINTS:
            reason = Reason.SEND_BOUNDARY_REJECTED
        elif hint in ig_lifecycle.TRANSIENT_PERMISSION_REASONS:
            reason = Reason.PERMISSION_DENIED
        elif hint in PERSISTENT_PERMISSION_REASONS:
            reason = Reason.PERMISSION_DENIED
        else:
            reason = FIXED_REASON_CODES.get(hint, Reason.CANCELLED_UNCLASSIFIED)
        return LifecycleDisposition(
            reason=reason,
            stage=Stage.BLOCKED_PRE_PROVIDER,
            evidence=_no_io_evidence(provider_io_started),
            terminal=True,
            detail=hint[:64],
        )

    if state == IgLifecycleEvent.State.MANAGER_REVIEW:
        if norm == ig_lifecycle.STANDARD_RESPONSE_WINDOW_CLOSED:
            reason = Reason.WINDOW_CLOSED
        elif norm in ig_lifecycle.TRANSIENT_PERMISSION_REASONS:
            # `_apply_permission_deferral`: 12 часов отсрочек истекли.
            reason = Reason.PERMISSION_DEFERRAL_TIMEOUT
        elif norm in PERSISTENT_PERMISSION_REASONS:
            reason = Reason.PERMISSION_DENIED
        else:
            reason = FIXED_REASON_CODES.get(norm, Reason.UNKNOWN)
        return LifecycleDisposition(
            reason=reason,
            stage=Stage.BLOCKED_PRE_PROVIDER,
            evidence=_no_io_evidence(provider_io_started),
            terminal=True,
            detail=norm[:64],
        )

    if state == IgLifecycleEvent.State.WAITING_WINDOW:
        if norm in ig_lifecycle.TRANSIENT_PERMISSION_REASONS:
            reason = Reason.PERMISSION_DEFERRED
        elif not norm:
            reason = Reason.AWAITING_DISPATCH
        else:
            reason = FIXED_REASON_CODES.get(norm, Reason.UNKNOWN)
        return LifecycleDisposition(
            reason=reason,
            stage=Stage.CREATED,
            evidence=_no_io_evidence(provider_io_started),
            terminal=False,
            detail=norm[:64],
        )

    if state == IgLifecycleEvent.State.PROCESSING:
        live_lease = bool(lease_expires_at and lease_expires_at > now)
        return LifecycleDisposition(
            reason=Reason.CLAIM_IN_FLIGHT if live_lease else Reason.CLAIM_LEASE_EXPIRED,
            stage=Stage.CLAIMED,
            evidence=(
                Evidence.UNKNOWN
                if live_lease
                else _no_io_evidence(provider_io_started)
            ),
            terminal=False,
            detail="live_lease" if live_lease else "expired_lease",
        )

    if state == IgLifecycleEvent.State.PENDING:
        kind, _ = _delivery_kind(norm)
        mapped = DELIVERY_KIND_REASONS.get(kind)
        if not norm or norm == ig_lifecycle.PROVIDER_BOUNDARY_CLAIM_MARKER:
            reason, stage = Reason.AWAITING_DISPATCH, Stage.CREATED
        elif mapped is not None:
            reason, stage = mapped, Stage.SEND_ATTEMPTED
        else:
            reason, stage = (
                FIXED_REASON_CODES.get(norm, Reason.UNKNOWN),
                Stage.CREATED,
            )
        return LifecycleDisposition(
            reason=reason,
            stage=stage,
            evidence=_no_io_evidence(provider_io_started),
            terminal=False,
            detail=f"{kind or norm}:attempt{int(attempts or 0)}"[:64],
        )

    return LifecycleDisposition(
        reason=Reason.UNKNOWN,
        stage=Stage.UNKNOWN,
        evidence=Evidence.UNKNOWN,
        terminal=state in _TERMINAL_STATES,
        detail=state[:64],
    )


# --- Улика провайдерского I/O ------------------------------------------------
def _provider_io_by_event_key(event_keys) -> dict[str, bool]:
    """Для каждого события: есть ли durable маркер обращения к провайдеру.

    Ответ всегда `True`/`False`, никогда `None`: здесь мы улику ИЩЕМ, поэтому её
    отсутствие — это доказательство «отправки не было», а не «не проверяли».
    """
    keys = list(dict.fromkeys(str(key) for key in event_keys if key))
    result = {key: False for key in keys}
    by_message_key = {ig_lifecycle._lifecycle_message_key(key): key for key in keys}
    message_keys = list(by_message_key)
    for start in range(0, len(message_keys), KEY_BATCH_SIZE):
        batch = message_keys[start:start + KEY_BATCH_SIZE]
        rows = InstagramBotMessage.objects.filter(
            synthetic_event_key__in=batch,
            role=InstagramBotMessage.Role.MODEL,
            source="lifecycle",
        ).only(
            "synthetic_event_key",
            "send_started_at",
            "provider_message_id",
            "send_state",
            "status",
        )
        for message in rows.iterator(chunk_size=KEY_BATCH_SIZE):
            event_key = by_message_key.get(message.synthetic_event_key)
            if event_key is None:
                continue
            if ig_lifecycle._lifecycle_message_has_provider_io(message):
                result[event_key] = True
    return result


def classify_event(event, *, provider_io_started=None, now=None) -> LifecycleDisposition:
    """Адаптер над чистым классификатором для модели или `values()`-словаря."""
    def field(name, default=None):
        if isinstance(event, dict):
            return event.get(name, default)
        return getattr(event, name, default)

    return classify_disposition(
        state=field("state"),
        last_error=field("last_error", "") or "",
        provider_message_id=field("provider_message_id", "") or "",
        attempts=field("attempts", 0) or 0,
        due_at=field("due_at"),
        lease_expires_at=field("lease_expires_at"),
        provider_io_started=provider_io_started,
        now=now,
    )


def _percentile(values, quantile: float):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * quantile))
    return round(float(ordered[index]), 1)


def _share(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round(numerator / denominator, 6)


def _event_projection(start, now) -> dict:
    rows = list(
        IgLifecycleEvent.objects.filter(created_at__gte=start, created_at__lte=now)
        .values(
            "id",
            "event_key",
            "kind",
            "state",
            "last_error",
            "provider_message_id",
            "attempts",
            "due_at",
            "lease_expires_at",
            "updated_at",
            "order_id",
        )
    )
    provider_io = _provider_io_by_event_key(row["event_key"] for row in rows)
    by_reason: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_evidence: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    buckets: dict[tuple[str, str, str], dict] = {}
    kind_reasons: dict[str, dict[str, int]] = {}
    for row in rows:
        disposition = classify_event(
            row,
            provider_io_started=provider_io.get(row["event_key"], False),
            now=now,
        )
        kind = row["kind"]
        by_reason[disposition.reason] = by_reason.get(disposition.reason, 0) + 1
        by_stage[disposition.stage] = by_stage.get(disposition.stage, 0) + 1
        by_evidence[disposition.evidence] = by_evidence.get(disposition.evidence, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        kind_reasons.setdefault(kind, {})
        kind_reasons[kind][disposition.reason] = (
            kind_reasons[kind].get(disposition.reason, 0) + 1
        )
        key = (disposition.reason, disposition.stage, disposition.evidence)
        bucket = buckets.setdefault(key, {
            "reason": disposition.reason,
            "stage": disposition.stage,
            "evidence": disposition.evidence,
            "terminal": disposition.terminal,
            "count": 0,
            "by_kind": {},
            "details": {},
            "_ages": [],
        })
        bucket["count"] += 1
        bucket["by_kind"][kind] = bucket["by_kind"].get(kind, 0) + 1
        if disposition.detail:
            bucket["details"][disposition.detail] = (
                bucket["details"].get(disposition.detail, 0) + 1
            )
        updated_at = row["updated_at"]
        if updated_at is not None:
            bucket["_ages"].append(max(0.0, (now - updated_at).total_seconds()))
    total = len(rows)
    bucket_list = []
    for bucket in buckets.values():
        ages = bucket.pop("_ages")
        bucket["share"] = _share(bucket["count"], total)
        # Возраст ПОСЛЕДНЕГО перехода. Названия `stopped_at` здесь быть не может:
        # вариант A не знает, когда событие остановилось.
        bucket["seconds_since_last_transition"] = {
            "p50": _percentile(ages, 0.5),
            "p95": _percentile(ages, 0.95),
            "max": _percentile(ages, 1.0),
        }
        bucket_list.append(bucket)
    bucket_list.sort(key=lambda item: (-item["count"], item["reason"]))
    return {
        "denominator": total,
        "by_kind": by_kind,
        "by_reason": by_reason,
        "by_stage": by_stage,
        "by_evidence": by_evidence,
        "by_kind_reason": kind_reasons,
        "buckets": bucket_list,
        "buckets_sum_matches_denominator": sum(by_reason.values()) == total,
        "unknown": by_reason.get(Reason.UNKNOWN, 0),
    }


# --- Знаменатель со стороны заказа -------------------------------------------
# ПРАВИЛО ЕДИНИЦЫ СЧЁТА (обязательное для Э0.3, иначе «доля событий нет» не имеет
# смысла). У одного `Order` может быть несколько shipment, episode и попыток
# оплаты, поэтому единицей счёта здесь является ОДИН `Order`, а не assignment,
# episode или shipment:
#   * заказ доставлен: `nova_poshta_order_fulfillment_confirmed(order)` истинно,
#     то есть перевозчик дал код 9/10/11, есть `tracking_terminal_at`, а статус
#     заказа — `done`. Окно измерения применяется к `tracking_terminal_at`;
#   * заказ принадлежит IG-воркспейсу: есть строка `IgOrderAssignment` (она
#     OneToOne к заказу, поэтому второй единицы из неё не возникает) ИЛИ есть
#     `IgOrderAttribution`. Второе условие оставлено сознательно: заказ, у
#     которого assignment сняли, всё равно был IG-заказом, и его молчание нужно
#     объяснить, а не спрятать сужением знаменателя;
#   * «событие есть» проверяется по `IgLifecycleEvent` того же заказа БЕЗ окна по
#     `created_at`: событие могло быть создано на границе окна.
# `IgOrderAssignment` НЕ доказывает lifecycle-eligibility: eligibility требует
# ещё attribution, proposal с payment_attempt и активной привязки к тому же
# клиенту. Поэтому «нет события» разбивается на типизированные причины ниже.
DELIVERED_UNIT_OF_COUNT = (
    "one delivered Order (carrier codes 9/10/11 + tracking_terminal_at in window "
    "+ order.status=done) that belongs to the IG workspace via IgOrderAssignment "
    "or IgOrderAttribution; lifecycle events are matched per order without a "
    "created_at window"
)


def _delivered_ig_orders(start, now) -> list:
    candidates = (
        Order.objects.filter(
            status="done",
            tracking_terminal_at__gte=start,
            tracking_terminal_at__lte=now,
            tracking_status_code__in=NOVA_POSHTA_DELIVERY_SUCCESS_CODES,
        )
        .exclude(tracking_number__isnull=True)
        .exclude(tracking_number="")
        .only(
            "id",
            "status",
            "pay_type",
            "tracking_number",
            "tracking_status_code",
            "tracking_terminal_at",
        )
    )
    # Повторная проверка чистой функцией истины доставки: у отчёта и у
    # производителя события должно быть ОДНО определение «доставлено».
    delivered = [
        order
        for order in candidates.iterator(chunk_size=500)
        if nova_poshta_order_fulfillment_confirmed(order)
    ]
    if not delivered:
        return []
    order_ids = [order.pk for order in delivered]
    assigned = set()
    attributed = set()
    for start_index in range(0, len(order_ids), KEY_BATCH_SIZE):
        batch = order_ids[start_index:start_index + KEY_BATCH_SIZE]
        assigned.update(
            IgOrderAssignment.objects.filter(order_id__in=batch).values_list(
                "order_id", flat=True
            )
        )
        attributed.update(
            IgOrderAttribution.objects.filter(order_id__in=batch).values_list(
                "order_id", flat=True
            )
        )
    ig_orders = [
        order for order in delivered if order.pk in assigned or order.pk in attributed
    ]
    for order in ig_orders:
        order._ig_assigned = order.pk in assigned
        order._ig_attributed = order.pk in attributed
    return ig_orders


def _absence_reason(order) -> str:
    """Почему у доставленного IG-заказа нет lifecycle-события.

    Порядок проверок повторяет порядок предпосылок `ensure_lifecycle_event()`:
    он выходит на первом же непройденном условии, поэтому первая непройденная
    проверка и есть наиболее вероятная точка остановки. Все выводы сделаны из
    ТЕКУЩЕГО состояния связей, а не из состояния на момент доставки: связь могли
    снять позже. Поэтому корзины помечены `inferred_from_current_state`, и ни
    одна из них не выдаётся за доказанную причину.
    """
    attribution = IgOrderAttribution.objects.filter(order_id=order.pk).first()
    if attribution is None:
        return Reason.ABSENT_NO_ATTRIBUTION
    proposal = (
        IgCheckoutProposal.objects.select_related("payment_attempt")
        .filter(payment_attempt__order_id=order.pk)
        .first()
    )
    if proposal is None:
        return Reason.ABSENT_NO_PROPOSAL
    if (
        attribution.client_id != proposal.client_id
        or attribution.deal_id != proposal.deal_id
    ):
        return Reason.ABSENT_CONTEXT_MISMATCH
    snapshot = ig_lifecycle._assignment_snapshot_for_client(
        order.pk, proposal.client_id
    )
    if snapshot is None:
        return Reason.ABSENT_NO_ACTIVE_ASSIGNMENT
    truth = (
        IgPaymentProjection.objects.filter(
            deal_id=proposal.deal_id,
            client_id=proposal.client_id,
        )
        .values_list("truth", flat=True)
        .first()
    )
    blocked = ig_lifecycle._business_truth_cancellation_reason(
        kind=IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED,
        payload=dict(snapshot),
        payment_truth=truth,
        order=order,
        assignment_matches=True,
    )
    if blocked == ig_lifecycle.PAYMENT_NOT_VERIFIED_ERROR:
        return Reason.ABSENT_PAYMENT_NOT_VERIFIED
    if blocked:
        return Reason.ABSENT_BUSINESS_TRUTH_BLOCKED
    # Все предпосылки выполняются СЕЙЧАС, а события нет. Это единственная
    # корзина, которая говорит «не знаю почему», и именно её размер решает,
    # нужен ли вариант B (append-only история переходов).
    return Reason.ABSENT_UNEXPLAINED


def _delivered_projection(start, now) -> tuple[dict, dict]:
    orders = _delivered_ig_orders(start, now)
    order_ids = [order.pk for order in orders]
    delivered_kind_orders = set()
    any_kind_orders = set()
    for start_index in range(0, len(order_ids), KEY_BATCH_SIZE):
        batch = order_ids[start_index:start_index + KEY_BATCH_SIZE]
        rows = IgLifecycleEvent.objects.filter(order_id__in=batch).values_list(
            "order_id", "kind"
        )
        for order_id, kind in rows:
            any_kind_orders.add(order_id)
            if kind == IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED:
                delivered_kind_orders.add(order_id)
    absence: dict[str, int] = {}
    for order in orders:
        if order.pk in delivered_kind_orders:
            continue
        reason = _absence_reason(order)
        absence[reason] = absence.get(reason, 0) + 1
    total = len(orders)
    without_event = total - len(delivered_kind_orders)
    delivered_report = {
        "unit_of_count": DELIVERED_UNIT_OF_COUNT,
        "denominator": total,
        "with_delivered_event": len(delivered_kind_orders),
        "without_event": without_event,
        "share_without_event": _share(without_event, total),
        "without_any_event": total - len(any_kind_orders),
        "with_active_or_past_assignment": sum(
            1 for order in orders if getattr(order, "_ig_assigned", False)
        ),
        "with_attribution": sum(
            1 for order in orders if getattr(order, "_ig_attributed", False)
        ),
        "absence_reasons": absence,
        "absence_evidence": Evidence.INFERRED_CURRENT_STATE,
        "absence_sum_matches_without_event": sum(absence.values()) == without_event,
    }
    cod_orders = sum(1 for order in orders if str(order.pay_type or "") == "cod")
    cod_report = {
        "unit_of_count": DELIVERED_UNIT_OF_COUNT,
        "denominator": total,
        "cod_orders": cod_orders,
        "share": _share(cod_orders, total),
        "by_pay_type": {},
    }
    for order in orders:
        pay_type = str(order.pay_type or "") or "unknown"
        cod_report["by_pay_type"][pay_type] = (
            cod_report["by_pay_type"].get(pay_type, 0) + 1
        )
    return delivered_report, cod_report


def lifecycle_reason_funnel(*, days: int = DEFAULT_WINDOW_DAYS, now=None) -> dict:
    """Один вызов: распределение терминальных причин со знаменателями.

    Только чтение. Ничего не отправляет, ничего не пишет, ни к какому провайдеру
    не обращается — снимать этот срез на production безопасно.
    """
    if not flag(FUNNEL_FLAG):
        return {
            "enabled": False,
            "flag": FUNNEL_FLAG,
            "reason": "feature flag disabled",
        }
    days = max(1, min(int(days or DEFAULT_WINDOW_DAYS), MAX_WINDOW_DAYS))
    now = now or timezone.now()
    start = now - timedelta(days=days)
    events = _event_projection(start, now)
    delivered, cod = _delivered_projection(start, now)
    delivered_kind = IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED
    kind_reasons = events["by_kind_reason"].get(delivered_kind, {})
    window_denominator = events["by_kind"].get(delivered_kind, 0)
    window_numerator = kind_reasons.get(Reason.WINDOW_CLOSED, 0)
    # Тот же числитель, посчитанный буквально по (state, last_error), без
    # классификатора. Он публикуется рядом с типизированным сознательно: если
    # числа расходятся, значит часть строк ушла в `contradictory_evidence`
    # (маркер провайдерского I/O на строке, которая не должна была до него
    # дойти), и это находка, а не погрешность отчёта.
    raw_window_numerator = IgLifecycleEvent.objects.filter(
        created_at__gte=start,
        created_at__lte=now,
        kind=delivered_kind,
        state=IgLifecycleEvent.State.MANAGER_REVIEW,
        last_error=ig_lifecycle.STANDARD_RESPONSE_WINDOW_CLOSED,
    ).count()
    return {
        "enabled": True,
        "flag": FUNNEL_FLAG,
        "variant": "A",
        "measured": MEASURED,
        "history_available": False,
        "caveat": CAVEAT,
        "window_days": days,
        "window_start": start.isoformat(),
        "generated_at": now.isoformat(),
        "events": events,
        # Прямая проверка гипотезы NEW-CRIT-001: доля delivered-событий, которые
        # остановились именно на закрытом окне Meta, со своим знаменателем.
        "window_closed_hypothesis": {
            "kind": delivered_kind,
            "numerator": window_numerator,
            "denominator": window_denominator,
            "share": _share(window_numerator, window_denominator),
            "raw_state_last_error_match": raw_window_numerator,
            "reason_distribution": kind_reasons,
        },
        "delivered_orders": delivered,
        "cod": cod,
    }
