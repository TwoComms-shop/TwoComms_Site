"""Durable supervision for the Instagram bot's production cron boundary.

Cron itself cannot tell us that another entry disappeared.  Each scheduled
command therefore records its last successful run here, while the long-lived
bot daemon checks freshness and emits one bounded incident alert per hour.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic, time

from django.db import DatabaseError, OperationalError, ProgrammingError
from django.db.models import Case, F, Subquery, Value, When
from django.utils import timezone

from management.models import InstagramBotTaskHeartbeat


DEGRADED_ALERT_THRESHOLD = 3
NOVA_PROVIDER_DEGRADED = "nova_poshta_provider_degraded"


class _TaskHeartbeatState:
    """Per-run outcome shared by scheduled commands and the heartbeat wrapper."""

    def __init__(self, task_key: str):
        self.task_key = task_key
        self.degraded_reason = ""
        self.skipped = False

    def mark_skipped(self) -> None:
        self.skipped = True

    def mark_degraded(self, reason_code: str) -> None:
        _validate_degraded_reason(self.task_key, reason_code)
        self.degraded_reason = reason_code


@dataclass(frozen=True)
class TaskSpec:
    key: str
    label: str
    expected_interval_seconds: int
    stale_after_seconds: int


# These are durable lanes owned by the single periodic coordinator plus the
# independent Nova Poshta owner. The stdlib daemon supervisor has filesystem
# state/process locks rather than a Django DB heartbeat, and the manual Gemini
# metadata diagnostic is intentionally not a scheduled health expectation.
TASK_SPECS = (
    TaskSpec("ig_checkout_reconcile", "звірка IG checkout", 120, 480),
    TaskSpec("ig_order_fulfillment", "доставка IG-подій замовлення", 120, 480),
    TaskSpec("ig_deal_payments", "backstop перевірки IG-оплат", 240, 720),
    TaskSpec("order_telegram_reconcile", "відновлення Telegram-карток замовлень", 120, 480),
    TaskSpec("nova_poshta_tracking", "оновлення статусів Нової Пошти", 300, 900),
    TaskSpec("ig_typed_memory_reconcile", "звірка typed memory", 600, 1800),
    TaskSpec("ig_trace_refresh", "оновлення journey trace", 15, 90),
    TaskSpec("binotel_call_ai_analyses", "Автоаналіз дзвінків", 300, 900),
)
MANUAL_TASK_SPECS = (
    TaskSpec("ig_daemon_watchdog", "ручний fallback watchdog Instagram-демона", 0, 0),
    TaskSpec("ig_gemini_metadata_health", "ручна діагностика Gemini API metadata", 0, 0),
)
_SPECS_BY_KEY = {
    spec.key: spec for spec in (*TASK_SPECS, *MANUAL_TASK_SPECS)
}
_CALL_AUTO_ANALYSIS_TASK_KEY = "binotel_call_ai_analyses"
_CALL_QUEUE_KEYS = ("eligible", "metadata_pending", "ineligible")
# The alert body carries the current unhealthy task set. Keep dedupe identity at
# the incident level so a second task joining the same outage does not create a
# new hourly alert bucket.
TASK_HEALTH_INCIDENT_FINGERPRINT = "scheduled_task_health_unhealthy"


def _call_auto_analysis_enabled() -> bool:
    """Read the optional task state without weakening other monitors."""
    try:
        from management.services.call_auto_analysis import (
            is_call_auto_analysis_enabled,
        )

        return bool(is_call_auto_analysis_enabled())
    except (DatabaseError, OperationalError, ProgrammingError):
        return False


def _active_task_specs() -> tuple[TaskSpec, ...]:
    if _call_auto_analysis_enabled():
        return TASK_SPECS
    return tuple(
        spec
        for spec in TASK_SPECS
        if spec.key != _CALL_AUTO_ANALYSIS_TASK_KEY
    )


def _spec(task_key: str) -> TaskSpec:
    try:
        return _SPECS_BY_KEY[task_key]
    except KeyError as exc:
        raise ValueError(f"Unknown Instagram operational task: {task_key!r}") from exc


def _upsert_expectation(spec: TaskSpec) -> InstagramBotTaskHeartbeat:
    row, created = InstagramBotTaskHeartbeat.objects.get_or_create(
        task_key=spec.key,
        defaults={
            "label": spec.label,
            "expected_interval_seconds": spec.expected_interval_seconds,
            "stale_after_seconds": spec.stale_after_seconds,
        },
    )
    if not created and (
        row.label != spec.label
        or row.expected_interval_seconds != spec.expected_interval_seconds
        or row.stale_after_seconds != spec.stale_after_seconds
    ):
        row.label = spec.label
        row.expected_interval_seconds = spec.expected_interval_seconds
        row.stale_after_seconds = spec.stale_after_seconds
        row.save(update_fields=["label", "expected_interval_seconds", "stale_after_seconds", "updated_at"])
    return row


def ensure_task_expectations() -> bool:
    """Register active tasks without deleting historical heartbeat rows."""
    try:
        for spec in _active_task_specs():
            _upsert_expectation(spec)
    except (DatabaseError, OperationalError, ProgrammingError):
        return False
    return True


def mark_task_started(task_key: str, *, at=None) -> InstagramBotTaskHeartbeat | None:
    try:
        row = _upsert_expectation(_spec(task_key))
        now = at or timezone.now()
        row.last_started_at = now
        row.save(update_fields=["last_started_at", "updated_at"])
        return row
    except (DatabaseError, OperationalError, ProgrammingError):
        return None


def mark_task_succeeded(task_key: str, *, duration_ms: int = 0, at=None) -> InstagramBotTaskHeartbeat | None:
    try:
        row = _upsert_expectation(_spec(task_key))
        now = at or timezone.now()
        row.last_started_at = row.last_started_at or now
        row.last_succeeded_at = now
        row.last_duration_ms = max(0, int(duration_ms or 0))
        row.consecutive_failures = 0
        row.last_error_kind = ""
        row.save(update_fields=[
            "last_started_at", "last_succeeded_at", "last_duration_ms",
            "consecutive_failures", "last_error_kind", "updated_at",
        ])
        return row
    except (DatabaseError, OperationalError, ProgrammingError):
        return None


def _task_failure_reason_code(exc: Exception) -> str:
    if exc.__class__.__name__ == "CommandError":
        message = str(exc or "").lower()
        if "initialization pending" in message:
            return "daemon_initialization_pending"
        if "startup exceeded" in message:
            return "daemon_startup_stale"
        if "still running" in message and "singleton lock" in message:
            return "daemon_start_pending"
        if "did not release singleton lock" in message:
            return "daemon_lock_stale"
        if "child exited with code" in message:
            return "daemon_child_exited"
        if "spawn failed" in message:
            return "daemon_spawn_failed"
        return "command_error"
    name = exc.__class__.__name__
    snake = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            snake.append("_")
        snake.append(char.lower())
    return "".join(snake)[:64] or "task_error"


def _notify_failure(
    row: InstagramBotTaskHeartbeat,
    error_kind: str,
    exc: Exception,
) -> None:
    try:
        from management.services.ig_alerts import alert_dedupe_key, format_alert
        from management.services import instagram_bot as bot

        reason_code = _task_failure_reason_code(exc)
        text = format_alert(
            "⚠️ Помилка IG cron-задачі",
            lines=(
                f"Задача: {row.label}",
                f"Тип помилки: {error_kind}",
                f"Причина: {reason_code}",
                f"Очікуваний інтервал: {row.expected_interval_seconds} с",
            ),
        )
        bot.notify_manager(
            text,
            dedupe_key=alert_dedupe_key(
                "ig_task_failure", entity_id=row.pk, window_minutes=60
            ),
            event_type="ig_task_failure",
            metadata={
                "task_key": row.task_key,
                "task_heartbeat_id": row.pk,
                "task_failure_reason": reason_code,
                "requires_human_review": False,
            },
            deliver_immediately=False,
        )
    except Exception:
        # A notification problem must not hide the original command failure.
        pass


def _notify_degraded(
    row: InstagramBotTaskHeartbeat,
    reason_code: str,
) -> None:
    """Notify only after repeated recoverable provider degradation."""
    try:
        from management.services.ig_alerts import alert_dedupe_key, format_alert
        from management.services import instagram_bot as bot

        text = format_alert(
            "⚠️ IG operations потребують уваги",
            lines=(
                f"Задача: {row.label}",
                "Тип помилки: ProviderDegraded",
                f"Причина: {reason_code}",
                f"Послідовні збої: {row.consecutive_failures}",
                f"Очікуваний інтервал: {row.expected_interval_seconds} с",
            ),
        )
        bot.notify_manager(
            text,
            dedupe_key=alert_dedupe_key(
                "ig_task_degraded", entity_id=row.pk, window_minutes=60
            ),
            event_type="ig_task_degraded",
            metadata={
                "task_key": row.task_key,
                "task_heartbeat_id": row.pk,
                "task_failure_reason": reason_code,
                "consecutive_failures": row.consecutive_failures,
                "requires_human_review": False,
            },
            deliver_immediately=False,
        )
    except Exception:
        pass


def _validate_degraded_reason(task_key: str, reason_code: str) -> None:
    # Only this explicitly reviewed provider outcome has delayed alert policy.
    if task_key != "nova_poshta_tracking" or reason_code != NOVA_PROVIDER_DEGRADED:
        raise ValueError("Unsupported degraded task outcome")


def mark_task_degraded(
    task_key: str,
    reason_code: str,
    *,
    duration_ms: int = 0,
    at=None,
) -> InstagramBotTaskHeartbeat | None:
    """Record a completed but degraded run and alert after three repeats."""
    _validate_degraded_reason(task_key, reason_code)
    try:
        row = _upsert_expectation(_spec(task_key))
        now = at or timezone.now()
        # One update compares the previous outcome before replacing it. A
        # preceding application failure must not count as a provider repeat.
        InstagramBotTaskHeartbeat.objects.filter(pk=row.pk).update(
            # MariaDB evaluates SET assignments left-to-right: compare the
            # previous reason before replacing last_error_kind below.
            consecutive_failures=Case(
                When(last_error_kind=reason_code, then=F("consecutive_failures") + 1),
                default=Value(1),
            ),
            last_started_at=row.last_started_at or now,
            last_failed_at=now,
            last_duration_ms=max(0, int(duration_ms or 0)),
            last_error_kind=reason_code,
            updated_at=now,
        )
        row.refresh_from_db()
    except (DatabaseError, OperationalError, ProgrammingError):
        return None
    if row.consecutive_failures >= DEGRADED_ALERT_THRESHOLD:
        _notify_degraded(row, row.last_error_kind)
    return row


def mark_task_failed(
    task_key: str,
    exc: Exception,
    *,
    duration_ms: int = 0,
    at=None,
) -> InstagramBotTaskHeartbeat | None:
    """Persist a failure without storing potentially sensitive exception text."""
    try:
        row = _upsert_expectation(_spec(task_key))
        now = at or timezone.now()
        error_kind = exc.__class__.__name__[:128]
        row.last_started_at = row.last_started_at or now
        row.last_failed_at = now
        row.last_duration_ms = max(0, int(duration_ms or 0))
        row.last_error_kind = error_kind
        row.save(update_fields=[
            "last_started_at", "last_failed_at", "last_duration_ms",
            "last_error_kind", "updated_at",
        ])
        InstagramBotTaskHeartbeat.objects.filter(pk=row.pk).update(
            consecutive_failures=F("consecutive_failures") + 1,
            updated_at=now,
        )
        row.refresh_from_db(fields=["consecutive_failures"])
    except (DatabaseError, OperationalError, ProgrammingError):
        return None
    _notify_failure(row, error_kind, exc)
    return row


@contextmanager
def task_heartbeat(task_key: str):
    """Record success/failure around the real cron work, never a dry-run."""
    mark_task_started(task_key)
    state = _TaskHeartbeatState(task_key)
    started = monotonic()
    try:
        yield state
    except Exception as exc:
        mark_task_failed(task_key, exc, duration_ms=round((monotonic() - started) * 1000))
        raise
    else:
        duration_ms = round((monotonic() - started) * 1000)
        if state.degraded_reason:
            mark_task_degraded(
                task_key,
                state.degraded_reason,
                duration_ms=duration_ms,
            )
        elif state.skipped:
            # A backoff/no-due pass is not evidence that the provider recovered.
            # Preserve unresolved failure and streak, while a healthy empty lane
            # still records its normal scheduler liveness.
            try:
                row = InstagramBotTaskHeartbeat.objects.filter(task_key=task_key).first()
            except (DatabaseError, OperationalError, ProgrammingError):
                return
            if row is not None and row.last_error_kind:
                return
            mark_task_succeeded(task_key, duration_ms=duration_ms)
        else:
            mark_task_succeeded(task_key, duration_ms=duration_ms)


def task_health_snapshot(*, now=None) -> dict:
    """Return a non-sensitive health summary suitable for UI and public probe."""
    now = now or timezone.now()
    active_specs = _active_task_specs()
    active_keys = {spec.key for spec in active_specs}
    try:
        rows = {
            row.task_key: row
            for row in InstagramBotTaskHeartbeat.objects.filter(
                task_key__in=active_keys
            )
        }
    except (DatabaseError, OperationalError, ProgrammingError):
        return {"available": False, "healthy": False, "tasks": [], "unhealthy_count": 0}

    tasks = []
    for spec in active_specs:
        row = rows.get(spec.key)
        if row is None:
            state = "unobserved"
            age_seconds = None
            error_kind = ""
            observed_at = None
        else:
            observed_at = row.last_succeeded_at
            failure_is_newer = bool(
                row.last_failed_at
                and (not row.last_succeeded_at or row.last_failed_at >= row.last_succeeded_at)
            )
            reference = row.last_succeeded_at or row.first_expected_at
            age_seconds = max(0, int((now - reference).total_seconds())) if reference else None
            error_kind = row.last_error_kind
            if failure_is_newer:
                if spec.key == "nova_poshta_tracking" and error_kind == NOVA_PROVIDER_DEGRADED:
                    # A stopped cron is distinct from its last provider error:
                    # never suppress a vanished owner while waiting for attempt 3.
                    last_activity = max(filter(None, (row.last_started_at, row.last_failed_at)))
                    activity_age = max(0, int((now - last_activity).total_seconds()))
                    state = "stale" if activity_age > spec.stale_after_seconds else "degraded"
                else:
                    state = "failed"
            elif not row.last_succeeded_at and age_seconds is not None and age_seconds > spec.stale_after_seconds:
                state = "not_observed"
            elif age_seconds is not None and age_seconds > spec.stale_after_seconds:
                state = "stale"
            else:
                state = "healthy"
        tasks.append({
            "key": spec.key,
            "label": spec.label,
            "state": state,
            "healthy": state == "healthy",
            "age_seconds": age_seconds,
            "stale_after_seconds": spec.stale_after_seconds,
            "last_succeeded_at": observed_at.isoformat() if observed_at else "",
            "last_error_kind": error_kind,
            "degraded": state == "degraded",
            "consecutive_failures": row.consecutive_failures if row else 0,
        })
    unhealthy = [task for task in tasks if not task["healthy"]]
    return {
        "available": True,
        "healthy": not unhealthy,
        "tasks": tasks,
        "unhealthy_count": len(unhealthy),
    }


def release_queue_snapshot() -> dict:
    """Return sanitized release-boundary queue counts, without customer data."""
    from management.models import IgBotNotification, InstagramBotMessage
    from management.ig_bot_models import IgAiReplyRecoveryJob, IgConversationAnalysisJob

    try:
        call_auto_analysis_enabled = _call_auto_analysis_enabled()
        inbound_queue = InstagramBotMessage.objects.filter(
            role=InstagramBotMessage.Role.USER,
            status__in=(InstagramBotMessage.Status.PENDING, InstagramBotMessage.Status.PROCESSING),
        )
        inbound_pending = inbound_queue.count()
        # Revision-owned sources remain pending as immutable delivery evidence.
        # They have a separate canonical/debt owner and must not be reported as
        # runnable legacy backlog or trigger the release-boundary alarm.
        from management.ig_bot_models import IgTurnRevisionSource
        from management.services.ig_revision_live import _owned_revisions

        owned_source_ids = IgTurnRevisionSource.objects.filter(
            revision_id__in=Subquery(_owned_revisions().values("id")),
        ).values("message_id")
        revision_owned_pending = inbound_queue.filter(
            pk__in=Subquery(owned_source_ids),
        ).count()
        legacy_inbound_pending = inbound_pending - revision_owned_pending
        reply_pending = InstagramBotMessage.objects.filter(
            role=InstagramBotMessage.Role.MODEL,
            status__in=(InstagramBotMessage.Status.PENDING, InstagramBotMessage.Status.PROCESSING),
        ).count()
        notification_unresolved = IgBotNotification.objects.filter(
            status__in=(
                IgBotNotification.Status.PENDING,
                IgBotNotification.Status.SENDING,
                IgBotNotification.Status.UNKNOWN,
                IgBotNotification.Status.FAILED,
                IgBotNotification.Status.DEAD_LETTER,
            )
        ).count()
        analysis_pending = IgConversationAnalysisJob.objects.filter(
            status__in=(
                IgConversationAnalysisJob.Status.PENDING,
                IgConversationAnalysisJob.Status.PROCESSING,
            )
        ).count()
        recovery_unresolved = IgAiReplyRecoveryJob.objects.filter(
            status__in=(
                IgAiReplyRecoveryJob.Status.PENDING,
                IgAiReplyRecoveryJob.Status.PROCESSING,
                IgAiReplyRecoveryJob.Status.SENDING,
                IgAiReplyRecoveryJob.Status.AMBIGUOUS,
                IgAiReplyRecoveryJob.Status.FAILED,
            )
        ).count()
        analysis_failed = IgConversationAnalysisJob.objects.filter(
            status=IgConversationAnalysisJob.Status.FAILED
        ).count()
        binotel_counts = dict.fromkeys(_CALL_QUEUE_KEYS, 0)
        binotel_stale_running = 0
        binotel_error = 0
        if call_auto_analysis_enabled:
            from django.db.models import Q

            from management.models import CallRecord
            from management.services.call_ai_queue import (
                ELIGIBLE,
                INELIGIBLE,
                METADATA_PENDING,
                STALE_ANALYSIS_LOCK_MINUTES,
                analysis_queue_category,
            )

            binotel_pending = CallRecord.objects.filter(
                provider="binotel", ai_status=CallRecord.AiStatus.PENDING,
            ).values_list("duration_seconds", "payload")
            for duration_seconds, payload in binotel_pending:
                category = analysis_queue_category(payload, duration_seconds)
                binotel_counts[category] += 1
            stale_before = timezone.now() - timedelta(minutes=STALE_ANALYSIS_LOCK_MINUTES)
            binotel_stale_running = CallRecord.objects.filter(
                Q(ai_locked_at__lte=stale_before) | Q(ai_locked_at__isnull=True),
                provider="binotel", ai_status=CallRecord.AiStatus.RUNNING,
            ).count()
            binotel_error = CallRecord.objects.filter(
                provider="binotel", ai_status=CallRecord.AiStatus.ERROR,
            ).count()
    except (DatabaseError, OperationalError, ProgrammingError) as exc:
        return {
            "available": False,
            "dangerous_backlog": 0,
            "inbound_pending": 0,
            "legacy_inbound_pending": 0,
            "revision_owned_pending": 0,
            "revision_owned_pending_reason": "preserved_revision_debt_evidence",
            "reply_pending": 0,
            "notification_unresolved": 0,
            "analysis_pending": 0,
            "recovery_unresolved": 0,
            "analysis_failed": 0,
            "binotel_eligible_pending": 0,
            "binotel_metadata_pending": 0,
            "binotel_ineligible_pending": 0,
            "binotel_stale_running": 0,
            "binotel_error": 0,
            "error": type(exc).__name__,
        }

    dangerous_backlog = (
        legacy_inbound_pending
        + reply_pending
        + notification_unresolved
        + analysis_pending
        + recovery_unresolved
        + binotel_counts["eligible"]
        + binotel_counts["metadata_pending"]
        + binotel_counts["ineligible"]
        + binotel_stale_running
    )
    return {
        "available": True,
        "dangerous_backlog": dangerous_backlog,
        "inbound_pending": inbound_pending,
        "legacy_inbound_pending": legacy_inbound_pending,
        "revision_owned_pending": revision_owned_pending,
        "revision_owned_pending_reason": "preserved_revision_debt_evidence",
        "reply_pending": reply_pending,
        "notification_unresolved": notification_unresolved,
        "analysis_pending": analysis_pending,
        "recovery_unresolved": recovery_unresolved,
        "analysis_failed": analysis_failed,
        "binotel_eligible_pending": binotel_counts["eligible"],
        "binotel_metadata_pending": binotel_counts["metadata_pending"],
        "binotel_ineligible_pending": binotel_counts["ineligible"],
        "binotel_stale_running": binotel_stale_running,
        "binotel_error": binotel_error,
    }


def check_task_health(*, now=None) -> dict:
    """Alert once per hour when one or more expected operational tasks are bad."""
    snapshot = task_health_snapshot(now=now)
    if not snapshot["available"]:
        return snapshot
    # Provider degradation has its own thresholded, recoverable alert owner.
    # It remains unhealthy in the returned snapshot; only the duplicate summary
    # is suppressed. A vanished cron is projected as stale and still alerts.
    unhealthy = [task for task in snapshot["tasks"]
                 if not task["healthy"] and task["state"] != "degraded"]
    if not unhealthy:
        return snapshot
    try:
        from management.services.ig_alerts import alert_dedupe_key, format_alert
        from management.services import instagram_bot as bot

        lines = [
            f"{task['label']}: {task['state']}"
            + (f" ({task['age_seconds']} с)" if task["age_seconds"] is not None else "")
            for task in unhealthy
        ]
        text = format_alert("🚨 IG operations потребують уваги", lines=lines)
        bot.notify_manager(
            text,
            dedupe_key=alert_dedupe_key(
                "ig_task_health",
                window_minutes=60,
                text=TASK_HEALTH_INCIDENT_FINGERPRINT,
            ),
            event_type="ig_task_health",
        )
    except Exception:
        pass
    return snapshot


# =============================================================================
# ЭА.14 — надзор за самим демоном: четыре состояния вместо одного «жив/мёртв»
# =============================================================================
#
# Почему это здесь, а не в команде демона. Решение «поднимать ли процесс» должно
# приниматься в модуле, который можно вызвать и из cron-fallback (`--ensure`), и
# из живого демона, и из теста, не запуская сам демон. До этого этапа надзор
# опирался на ОДИН булев признак `_daemon_alive()` — свежесть process-пульса, — и
# из него выводились взаимоисключающие действия: «всё хорошо» либо «заменить
# владельца singleton-lock». Отсюда и наблюдаемое в источнике равенство
# `daemon_spawn` ≈ `daemon_start` ≈ 101 в сутки: живой процесс, занятый одним
# длинным провайдерским ходом, выглядел смертью, и надзор его заменял. Заменa
# посреди хода опаснее шума в логе: если процесс уходит ПОСЛЕ вызова Meta, но до
# записи receipt, результат доставки клиенту становится неизвестным.
#
# Различаем четыре состояния, потому что ровно они требуют РАЗНЫХ действий:
#
#   process_alive_and_progressing — пульс свеж И прогресс двигается → НИЧЕГО.
#       Это главный случай, который раньше ошибочно приводил к перезапуску.
#   process_alive_but_no_progress — пульс свеж (процесс доказуемо жив), но
#       прогресса нет дольше порога → ЭСКАЛАЦИЯ, а не перезапуск. Перезапуск
#       здесь вредит: он не лечит зависание (причина останется), зато делает
#       доставку неопределённой. Человек должен увидеть «висит», а не «рестарт».
#   lock_stale_no_owner — живого владельца доказать нечем (пульса нет или он
#       устарел), независимо от того, держится ли файл-lock → поднять демона.
#       Формулировка «нет ДОКАЗУЕМОГО владельца» намеренно строже, чем «lock
#       свободен»: удерживаемый lock сам по себе доказывает существование
#       процесса, но не его работоспособность.
#   child_exited — есть прямое свидетельство завершения ребёнка (код возврата
#       или сигнал) → поднять демона, не дожидаясь истечения окна кэша. Прямое
#       свидетельство exit-а сильнее любой давности пульса.
#
# Честная граница (из источника): точный механизм завершения конкретного
# процесса не доказан. Этот код закрывает ЛОЖНЫЕ СРАБАТЫВАНИЯ надзора, а не
# «причину рестартов». Если после включения флага рестарты сохранятся — это
# отдельное расследование, а не регресс этого этапа.

DAEMON_STATE_PROGRESSING = "process_alive_and_progressing"
DAEMON_STATE_NO_PROGRESS = "process_alive_but_no_progress"
DAEMON_STATE_LOCK_STALE = "lock_stale_no_owner"
DAEMON_STATE_CHILD_EXITED = "child_exited"

DAEMON_SUPERVISION_STATES = (
    DAEMON_STATE_PROGRESSING,
    DAEMON_STATE_NO_PROGRESS,
    DAEMON_STATE_LOCK_STALE,
    DAEMON_STATE_CHILD_EXITED,
)

# Действия намеренно не совпадают один-к-одному с состояниями: смысл этапа в том,
# что «жив, но не двигается» и «владельца нет» перестают вести к одному и тому же.
DAEMON_ACTION_NONE = "none"
DAEMON_ACTION_ESCALATE = "escalate"
DAEMON_ACTION_SPAWN = "spawn"

# Сколько «окон живости» подряд отсутствие прогресса ещё считается нормой.
# Два, а не одно: одно окно уже покрывает худший заявленный ход (Э2.10), поэтому
# порог в одно окно объявлял бы зависанием штатную длинную работу — ровно та
# ошибка, из-за которой окно живости было меньше бюджета хода.
DAEMON_NO_PROGRESS_WINDOWS = 2
_DAEMON_FALLBACK_ALIVE_WINDOW_SECONDS = 150


def _supervision_flag(name: str, default: bool = True) -> bool:
    """Единая точка чтения флагов этапа (конвенция ig_provider_incidents.flag)."""
    try:
        from management.services.ig_provider_incidents import flag

        return flag(name, default)
    except Exception:
        # Отсутствие модуля флагов не должно превращать надзор в no-op: без
        # надзора демон вообще некому поднимать.
        return default


def daemon_supervision_enabled() -> bool:
    """ЭА.14 «Откат»: при выключении надзор работает как до этапа."""
    return _supervision_flag("IG_DAEMON_SUPERVISION_STATES", True)


def daemon_alive_window_seconds() -> int:
    """Окно живости пульса — выведенное, а не независимое (см. ig_turn_budget)."""
    try:
        from management.services.ig_turn_budget import heartbeat_alive_window_seconds

        return max(1, int(heartbeat_alive_window_seconds()))
    except Exception:
        return _DAEMON_FALLBACK_ALIVE_WINDOW_SECONDS


def daemon_no_progress_after_seconds() -> int:
    """Порог «прогресса нет» ВЫВОДИТСЯ из окна живости, а не задаётся отдельно.

    Независимое число здесь разошлось бы с реальным бюджетом хода при первой же
    правке таймаутов — это и есть исходный дефект Э2.10, повторять его нельзя.
    """
    return max(60, daemon_alive_window_seconds() * DAEMON_NO_PROGRESS_WINDOWS)


def _payload_moment(payload, *keys) -> float | None:
    """Самая свежая из перечисленных wall-clock метк payload-а пульса."""
    if not isinstance(payload, dict):
        return None
    newest = None
    for key in keys:
        try:
            moment = float(payload.get(key))
        except (TypeError, ValueError):
            continue
        if moment <= 0:
            continue
        newest = moment if newest is None else max(newest, moment)
    return newest


@dataclass(frozen=True)
class DaemonSupervisionVerdict:
    """Состояние надзора и вытекающее из него ЕДИНСТВЕННОЕ действие."""

    state: str
    action: str
    reason: str
    pulse_age_seconds: float | None = None
    progress_age_seconds: float | None = None
    inflight_operation: str = ""
    child_exit_code: int | None = None
    child_exit_signal: int | None = None

    @property
    def progressing(self) -> bool:
        return self.state == DAEMON_STATE_PROGRESSING

    @property
    def requires_spawn(self) -> bool:
        return self.action == DAEMON_ACTION_SPAWN

    def as_log_suffix(self) -> str:
        """Компактная строка для лога: без клиентских данных, только надзор."""
        parts = [f"state={self.state}", f"action={self.action}", f"reason={self.reason}"]
        if self.pulse_age_seconds is not None:
            parts.append(f"pulse_age={self.pulse_age_seconds:.0f}")
        if self.progress_age_seconds is not None:
            parts.append(f"progress_age={self.progress_age_seconds:.0f}")
        if self.inflight_operation:
            parts.append(f"inflight={self.inflight_operation}")
        return " ".join(parts)


def classify_daemon_supervision(
    *,
    pulse=None,
    progress=None,
    lock_held: bool = False,
    child_exit_code: int | None = None,
    child_exit_signal: int | None = None,
    now: float | None = None,
    alive_window_seconds: int | None = None,
    no_progress_after_seconds: int | None = None,
) -> DaemonSupervisionVerdict:
    """Свести наблюдения надзора к одному из четырёх состояний ЭА.14.

    Функция намеренно чистая: никакого I/O, никакого доступа к БД и к кэшу. Все
    наблюдения передаются снаружи, поэтому гонку «живой процесс vs надзор» можно
    воспроизвести в тесте, не поднимая демон.

    Порядок проверок — это приоритет ДОКАЗАТЕЛЬСТВ, а не удобство чтения:

    1. Прямое свидетельство завершения ребёнка сильнее любой давности кэша:
       мёртвого владельца нельзя «дождаться».
    2. Свежий пульс доказывает, что процесс жив. Пока он свеж, поднимать второго
       демона запрещено — это и создавало `daemon_spawn` ≈ `daemon_start`.
    3. Прогресс проверяется ОТДЕЛЬНО от пульса. Пульс обновляет фоновый поток и
       он не вправе подтверждать, что основная работа двигается; иначе свежий
       пульс скрывал бы зависший цикл.
    4. Нет свежего пульса — владельца доказать нечем, даже если файл-lock
       удерживается: удерживаемый lock доказывает существование процесса, но не
       его работоспособность.
    """
    checked_at = float(time() if now is None else now)
    # Пороги — float, а не int: усечение до целого превратило бы окно 0.4 с в 0 и
    # объявило бы живой процесс мёртвым. Тесты сжимают окна до долей секунды
    # именно поэтому — проверять контракт, а не ждать реальные две минуты.
    window = float(alive_window_seconds or daemon_alive_window_seconds())
    no_progress_after = float(
        no_progress_after_seconds or daemon_no_progress_after_seconds()
    )

    pulse_at = _payload_moment(pulse, "at")
    pulse_age = None if pulse_at is None else max(0.0, checked_at - pulse_at)
    # `progress_at` двигается только когда что-то реально продвинулось: граница
    # рабочего цикла или явный шаг операции «в полёте». Фоновый пульс его не
    # трогает — именно поэтому по нему видно зависание.
    progress_at = _payload_moment(progress, "progress_at", "at")
    inflight_progress_at = _payload_moment(pulse, "progress_at", "last_completed_cycle_at")
    if inflight_progress_at is not None:
        progress_at = (
            inflight_progress_at
            if progress_at is None
            else max(progress_at, inflight_progress_at)
        )
    progress_age = None if progress_at is None else max(0.0, checked_at - progress_at)
    inflight_operation = ""
    if isinstance(pulse, dict):
        inflight_operation = str(pulse.get("inflight_operation") or "")[:64]

    if child_exit_code is not None or child_exit_signal is not None:
        return DaemonSupervisionVerdict(
            state=DAEMON_STATE_CHILD_EXITED,
            action=DAEMON_ACTION_SPAWN,
            reason="child_exit_observed",
            pulse_age_seconds=pulse_age,
            progress_age_seconds=progress_age,
            inflight_operation=inflight_operation,
            child_exit_code=child_exit_code,
            child_exit_signal=child_exit_signal,
        )

    pulse_fresh = pulse_age is not None and pulse_age < window
    if not pulse_fresh:
        return DaemonSupervisionVerdict(
            state=DAEMON_STATE_LOCK_STALE,
            action=DAEMON_ACTION_SPAWN,
            reason="pulse_absent" if pulse_age is None else "pulse_stale",
            pulse_age_seconds=pulse_age,
            progress_age_seconds=progress_age,
            inflight_operation=inflight_operation,
        )
    if not lock_held:
        # Процесс доказуемо жив, но singleton никем не занят: это окно старта или
        # завершения. Поднимать второго нельзя — он выиграет свободный lock и мы
        # получим двух писателей одной очереди.
        return DaemonSupervisionVerdict(
            state=DAEMON_STATE_NO_PROGRESS,
            action=DAEMON_ACTION_ESCALATE,
            reason="alive_without_singleton_owner",
            pulse_age_seconds=pulse_age,
            progress_age_seconds=progress_age,
            inflight_operation=inflight_operation,
        )
    # A shared database circuit intentionally defers work without completing a
    # cycle.  Its fresh main-loop publication proves a live, bounded cooldown;
    # it must not move completed progress or become a generic stalled-daemon
    # alert.  If that publication itself stops, the normal stale-progress path
    # below remains in force even while the process-pulse thread is alive.
    deferred_at = _payload_moment(progress, "at")
    if (
        isinstance(progress, dict)
        and progress.get("state") == "db_deferred"
        and deferred_at is not None
        and max(0.0, checked_at - deferred_at) < window
    ):
        return DaemonSupervisionVerdict(
            state=DAEMON_STATE_PROGRESSING,
            action=DAEMON_ACTION_NONE,
            reason="db_deferred",
            pulse_age_seconds=pulse_age,
            progress_age_seconds=progress_age,
            inflight_operation=inflight_operation,
        )
    if progress_age is None:
        return DaemonSupervisionVerdict(
            state=DAEMON_STATE_NO_PROGRESS,
            action=DAEMON_ACTION_ESCALATE,
            reason="progress_unobserved",
            pulse_age_seconds=pulse_age,
            progress_age_seconds=None,
            inflight_operation=inflight_operation,
        )
    if progress_age >= no_progress_after:
        return DaemonSupervisionVerdict(
            state=DAEMON_STATE_NO_PROGRESS,
            action=DAEMON_ACTION_ESCALATE,
            reason="progress_stale",
            pulse_age_seconds=pulse_age,
            progress_age_seconds=progress_age,
            inflight_operation=inflight_operation,
        )
    return DaemonSupervisionVerdict(
        state=DAEMON_STATE_PROGRESSING,
        action=DAEMON_ACTION_NONE,
        reason="progress_observed",
        pulse_age_seconds=pulse_age,
        progress_age_seconds=progress_age,
        inflight_operation=inflight_operation,
    )


def escalate_daemon_supervision(verdict: DaemonSupervisionVerdict) -> bool:
    """Эскалация «жив, но не двигается»: один ограниченный алерт в час.

    Возвращает True, если алерт был поставлен в очередь. Никогда не бросает:
    проблема наблюдаемости не имеет права остановить надзор за демоном.
    """
    if verdict.action != DAEMON_ACTION_ESCALATE:
        return False
    try:
        from management.services import instagram_bot as bot
        from management.services.ig_alerts import alert_dedupe_key, format_alert

        lines = [
            f"Стан: {verdict.state}",
            f"Причина: {verdict.reason}",
        ]
        if verdict.progress_age_seconds is not None:
            lines.append(f"Без прогресу: {verdict.progress_age_seconds:.0f} с")
        if verdict.pulse_age_seconds is not None:
            lines.append(f"Вік пульсу: {verdict.pulse_age_seconds:.0f} с")
        if verdict.inflight_operation:
            lines.append(f"Операція в польоті: {verdict.inflight_operation}")
        lines.append("Дію не виконано: перезапуск живого процесу заборонено")
        bot.notify_manager(
            format_alert("⚠️ IG-демон живий, але без прогресу", lines=tuple(lines)),
            dedupe_key=alert_dedupe_key(
                "ig_daemon_no_progress",
                window_minutes=60,
                text=f"{verdict.state}:{verdict.reason}",
            ),
            event_type="ig_daemon_no_progress",
            metadata={
                "daemon_state": verdict.state,
                "daemon_reason": verdict.reason,
                "requires_human_review": True,
            },
            deliver_immediately=False,
        )
        return True
    except Exception:
        return False


# =============================================================================
# ЭА.14 — reclaim строк `processing` по ОПЕРАЦИОННОМУ lease, а не по стене
# =============================================================================
#
# Что было не так. Возврат зависших строк в очередь опирался на абсолютный возраст
# `processing_started_at` (`IG_BOT_STALE_PROCESSING_SECONDS`, по умолчанию 300 с).
# Абсолютное время не связано ни с одной операционной величиной: заявленный бюджет
# сложного хода — около 116 с (Э2.10), а один ход может законно уйти в повторы у
# провайдера. Порог 300 с — не «истёк срок владения», а «прошло много времени по
# часам», и он отбирал строку у ЖИВОГО владельца. После этого возможны два
# writer-а одной строки, а сам факт отправки клиенту становится неопределённым.
#
# Что такое операционный lease. Право владеть строкой действует, пока владелец
# ПОДТВЕРЖДАЕТ работу: пока он продлевает `IgClient.automation_lease_*` и пока его
# операция «в полёте» отдаёт прогресс. Возраст по часам сам по себе права не
# отзывает. Отсюда две величины ниже: длительность lease выводится из бюджета хода
# (а не назначается независимо), а возраст reclaim-а — из lease, с запасом.
#
# Ограничение этого коммита (осознанное, зафиксировано в отчёте этапа): сам цикл
# отбора строк живёт в `instagram_bot.reclaim_stale_processing()`, вне границы
# файлов этой задачи. Здесь реализована АВТОРИТЕТНАЯ величина lease и её проверка,
# и демон вызывает reclaim уже с lease-выведенным возрастом. Перенос ПЕРВИЧНОГО
# отбора с `processing_started_at__lt=cutoff` на проверку lease требует правки
# `instagram_bot.py` и остаётся отдельным шагом.
#
# Запись результата после reclaim уже ограждена fencing-токеном: claim обновляет
# строку только при совпадении `processing_started_at` (`_own_processing_claim`),
# а reclaim этот токен обнуляет. Тест на гонку в `tests_ig_daemon_progress`
# фиксирует именно это свойство — без него отобранная строка получала бы результат
# от прежнего владельца и клиент видел бы ответ дважды.

# Запас поверх бюджета хода: владелец может задержать продление lease на секунду
# из-за GC/диска/шума shared-хостинга. Без запаса контракт был бы формально верен,
# а поведение — на грани.
OPERATIONAL_LEASE_MARGIN_SECONDS = 60
# Reclaim обязан быть строго позже истечения lease, иначе он отбирает строку у
# владельца, который ещё вправе дописать результат.
RECLAIM_AFTER_LEASE_MARGIN_SECONDS = 60


def operational_reclaim_lease_enabled() -> bool:
    """ЭА.14 «Откат»: при выключении используется прежний абсолютный порог."""
    return _supervision_flag("IG_BOT_OPERATIONAL_RECLAIM_LEASE", True)


def operational_lease_seconds() -> int:
    """Длительность операционного lease, ВЫВЕДЕННАЯ из бюджета хода.

    Нижняя граница — настроенный `IG_BOT_AUTOMATION_LEASE_SECONDS`: конфигурация
    может сделать lease длиннее, но не короче операционной огибающей хода.
    """
    from django.conf import settings as django_settings

    # Единственный источник истины о праве владения ходом — `turn_lease_seconds()`
    # (сам выведен из бюджета Э2.10). Заводить здесь второе число нельзя: две
    # независимые длительности lease разошлись бы при первой правке таймаута, и
    # реконсиляция хода начала бы спорить с reclaim строки.
    try:
        from management.services.ig_customer_turns import turn_lease_seconds

        derived = int(float(turn_lease_seconds()) + OPERATIONAL_LEASE_MARGIN_SECONDS)
    except Exception:
        try:
            from management.services.ig_turn_budget import declared_turn_budget_seconds

            budget = float(declared_turn_budget_seconds())
        except Exception:
            budget = float(_DAEMON_FALLBACK_ALIVE_WINDOW_SECONDS)
        derived = int(budget * 2.0 + OPERATIONAL_LEASE_MARGIN_SECONDS)
    try:
        configured = int(getattr(django_settings, "IG_BOT_AUTOMATION_LEASE_SECONDS", 0))
    except (TypeError, ValueError):
        configured = 0
    return max(60, derived, configured)


def operational_reclaim_age_seconds() -> int:
    """Возраст, после которого строку можно отобрать: lease + запас.

    Это не «сколько ждать по часам», а «когда право владения точно истекло».
    При выключенном флаге возвращается прежнее конфигурационное значение, чтобы
    откат был полным, а не частичным.
    """
    from django.conf import settings as django_settings

    if not operational_reclaim_lease_enabled():
        try:
            return max(
                1,
                int(getattr(django_settings, "IG_BOT_STALE_PROCESSING_SECONDS", 300)),
            )
        except (TypeError, ValueError):
            return 300
    return operational_lease_seconds() + RECLAIM_AFTER_LEASE_MARGIN_SECONDS


def processing_lease_expired(
    *,
    claimed_at,
    progress_at=None,
    now=None,
    lease_seconds: int | None = None,
) -> bool:
    """Истёк ли операционный lease на строку.

    `progress_at` — последнее подтверждение работы владельцем (продление lease или
    шаг операции). Именно он, а не время claim-а, отзывает право владения: живой
    владелец, который продолжает подтверждать работу, строку не теряет никогда.
    """
    if claimed_at is None:
        # Строка без метки claim-а не имеет владельца, которого можно ущемить.
        return True
    reference = progress_at or claimed_at
    checked_at = now or timezone.now()
    lease = int(lease_seconds or operational_lease_seconds())
    try:
        age = (checked_at - reference).total_seconds()
    except TypeError:
        return True
    return age >= lease
