"""
Раннер Instagram-бота.

Режими:
  --forever   Постійний демон: онлайн весь час, опитує інбокс кожні N секунд.
              Це основний режим — агент «живе» постійно, а не запускається кроном.
  --ensure    Ручний/deploy fallback: якщо stdlib supervisor активний, лише
              чекає його child; інакше зберігає старий bounded spawn path.
              Production cron використовує scripts/instagram_bot_supervisor.py,
              тому щохвилини не завантажує Django для liveness-перевірки.
  --once      Один прохід опитування (для діагностики).

Демон-singleton тримається через OS advisory lock: другий демон не стартує,
навіть якщо FileBasedCache очищений або недоступний. Кожна ітерація викликає close_old_connections() — інакше на
shared-MySQL (wait_timeout=60) з'являється "MySQL server has gone away".
"""
import json
import os
import fcntl
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from contextlib import contextmanager

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection
from management.services.ig_db_circuit import (
    DbCircuitOpen, record_db_failure, require_database_ready,
)
from django.utils import timezone

from management.models import InstagramBotSettings
from management.services import bot_followups
from management.services import instagram_bot as bot
from management.services.ig_task_health import (
    check_task_health,
    ensure_task_expectations,
    task_heartbeat,
)
from management.services.ig_maintenance import (
    DEFAULT_MAINTENANCE_SECONDS,
    MAINTENANCE_FILE,
    MaintenanceLeaseConflict,
    activate_maintenance,
    deactivate_maintenance,
    maintenance_status,
    runtime_root,
)

# Process pulse proves only that the daemon process and its pulse thread are
# alive. Main progress is a separate key and advances only at work-cycle
# boundaries, so a fresh pulse can no longer disguise a stuck main loop.
PROCESS_PULSE_KEY = "ig_bot_daemon_hb"  # backwards-compatible cache key
MAIN_PROGRESS_KEY = "ig_bot_daemon_main_progress"
HB_KEY = PROCESS_PULSE_KEY


def _heartbeat_alive_window() -> int:
    """Окно живості ВИВОДИТЬСЯ з бюджету ходу, а не задається незалежно (Э2.10).

    Було: `HB_ALIVE_WINDOW = 45`, тоді як бюджет складного ходу — близько 116 с
    (очікування burst + генерація + пауза набору + чотири чанки відправки). Тобто
    штатна довга відповідь гарантовано виглядала смертю демона, і watchdog піднімав
    новий процес посеред живої роботи.

    Друге наслідство серйозніше за шум: якщо процес перезапускається ПІСЛЯ
    провайдерського виклику, але до запису receipt, результат відправки стає
    невідомим. Тобто хибне срабатывание watchdog створювало реальну
    невизначеність доставки для клієнта.
    """
    try:
        from management.services.ig_turn_budget import heartbeat_alive_window_seconds

        return heartbeat_alive_window_seconds()
    except Exception:
        # Деградація: без модуля бюджету краще взяти явно консервативне значення,
        # ніж повернутись до 45 с, які й були причиною дефекту.
        return 150


HB_ALIVE_WINDOW = _heartbeat_alive_window()
SPAWN_LOCK_KEY = "ig_bot_spawn_lock"
DAEMON_LOCK_KEY = "ig_bot_daemon_lock"
CONV_REFRESH_EVERY = 120               # фонове оновлення списку тредів, c
CONV_REFRESH_PROGRESS_EVERY = 5        # швидко завершуємо resumable scan
ANALYSIS_RECONCILE_EVERY = 600         # bounded repair of missed scheduling, c
ANALYSIS_RECONCILE_BATCH = 100
RELOAD_LOCK_WAIT_SECONDS = 45
MAX_RELOAD_LOCK_WAIT_SECONDS = 300
DAEMON_START_WAIT_SECONDS = 15
DAEMON_READY_WAIT_SECONDS = 8
DAEMON_START_PENDING_SECONDS = 120
TASK_HEALTH_CHECK_EVERY = 60

# Cron may invoke manage.py from an arbitrary working directory. Resolve the
# entry point from this command module and keep the child in the Django root.
PROJECT_ROOT = str(runtime_root())
MANAGE_PY_PATH = str(Path(PROJECT_ROOT) / "manage.py")
PID_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot.pid")
SPAWN_LOCK_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot_spawn.lock")
DAEMON_LOCK_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot_daemon.lock")
STARTING_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot_starting.json")
SUPERVISOR_LOCK_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot_supervisor.lock")


@contextmanager
def _try_process_lock(path: str):
    """Yield an open lock handle, or ``None`` when another process owns it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            yield None
            return
        yield handle
    finally:
        if not handle.closed:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _process_lock_held(path: str) -> bool:
    with _try_process_lock(path) as handle:
        return handle is None


def _supervisor_active() -> bool:
    """Keep supervisor ownership explicit and independently mockable."""
    os.makedirs(os.path.dirname(SUPERVISOR_LOCK_FILE), exist_ok=True)
    with open(SUPERVISOR_LOCK_FILE, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        try:
            return False
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _wait_for_lock(path: str, *, held: bool, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _process_lock_held(path) is held:
            return True
        time.sleep(0.1)
    return _process_lock_held(path) is held


def _wait_for_daemon_ready(*, timeout: float = DAEMON_READY_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _daemon_alive() and _daemon_code_current():
            return True
        time.sleep(0.1)
    return _daemon_alive() and _daemon_code_current()


def _read_starting_child() -> dict:
    try:
        with open(STARTING_FILE, encoding="utf-8") as marker:
            payload = json.load(marker)
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _clear_starting_child(*, expected_pid: int | None = None) -> None:
    if expected_pid is not None:
        try:
            if int(_read_starting_child().get("pid") or 0) != int(expected_pid):
                return
        except (TypeError, ValueError):
            return
    try:
        os.unlink(STARTING_FILE)
    except FileNotFoundError:
        pass


def _record_starting_child(pid) -> None:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    os.makedirs(os.path.dirname(STARTING_FILE), exist_ok=True)
    temporary = f"{STARTING_FILE}.tmp.{os.getpid()}"
    payload = {
        "pid": pid,
        "started_at": time.time(),
        "sentinel": _restart_sentinel_mtime(),
    }
    try:
        with open(temporary, "w", encoding="utf-8") as marker:
            json.dump(payload, marker, separators=(",", ":"))
            marker.flush()
            os.fsync(marker.fileno())
        os.replace(temporary, STARTING_FILE)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _starting_child_state(*, now: float | None = None) -> str:
    payload = _read_starting_child()
    try:
        pid = int(payload.get("pid") or 0)
        started_at = float(payload.get("started_at"))
        sentinel = float(payload.get("sentinel"))
    except (TypeError, ValueError):
        _clear_starting_child()
        return "absent"
    if pid <= 0:
        _clear_starting_child()
        return "absent"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _clear_starting_child(expected_pid=pid)
        return "absent"
    except PermissionError:
        # Same-account production should not hit this, but never spawn over a
        # process whose liveness cannot be disproved.
        return "stale"
    age = (time.time() if now is None else now) - started_at
    if (
        0 <= age <= DAEMON_START_PENDING_SECONDS
        and sentinel == _restart_sentinel_mtime()
    ):
        return "current"
    return "stale"


def _bounded_reload_lock_wait(value: int | float | None) -> float:
    """Validate the operator-controlled drain wait without allowing infinity."""
    if value is None:
        return float(RELOAD_LOCK_WAIT_SECONDS)
    try:
        wait_seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise CommandError("maintenance lock wait must be a number") from exc
    if not 0 <= wait_seconds <= MAX_RELOAD_LOCK_WAIT_SECONDS:
        raise CommandError(
            "maintenance lock wait must be between 0 and "
            f"{MAX_RELOAD_LOCK_WAIT_SECONDS} seconds"
        )
    return wait_seconds


# =============================================================================
# ЭА.14 — операции «в полёте»: прогресс внутри длинной работы, а не после неё
# =============================================================================
#
# Что было не так. Пульс процесса обновлял фоновый поток каждые 10 секунд, и это
# доказывало ровно одно: процесс и его планировщик потоков живы. Прогресс же
# двигался ТОЛЬКО на границе `_run_work_cycle()`. Значит один провайдерский ход
# длиной 34–44 секунды (а по заявленному бюджету Э2.10 — до 102) не оставлял
# внутри себя ни одного следа продвижения, и надзор не мог отличить «работает
# долго» от «висит». Из одного признака выводились два взаимоисключающих
# действия, поэтому живой процесс заменялся: в источнике `daemon_spawn` почти
# равен `daemon_start` (≈101 за сутки).
#
# Решение — реестр операций «в полёте» с тремя наблюдаемыми величинами:
#
#   inflight_operation      что именно выполняется сейчас (имя полосы);
#   progress_at             когда последний раз что-то РЕАЛЬНО продвинулось;
#   last_completed_cycle_at когда последний раз завершился полный рабочий цикл.
#
# Ключевое свойство, без которого весь механизм был бы вреден: `progress_at`
# двигают только явные шаги работы (начало/шаг/конец операции и граница цикла).
# Фоновый пульс его НЕ трогает. Иначе свежий пульс маскировал бы зависший цикл —
# ровно та ошибка, от которой защищает отдельный ключ `MAIN_PROGRESS_KEY`.
#
# Почему это кэш, а не поля `InstagramBotTaskHeartbeat`. Долгая операция обязана
# отчитываться вне транзакции и не удерживая row-lock (раздел 9.3 источника:
# внешний I/O внутри `select_for_update` запрещён). Кэш даёт запись без
# транзакции и без миграции; durable-поля — отдельный шаг со своей миграцией.
DEFAULT_OPERATION_LEASE_SECONDS = 300.0


class _InflightOperations:
    """Потокобезопасный реестр операций «в полёте» одного процесса демона.

    Реестр, а не одно значение, потому что ЭА.15 требует СВОЙ pulse у каждой
    вынесенной полосы: без разделения надзор видел бы прогресс уведомлений там,
    где стоит клиентская обработка, и наоборот.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lanes: dict[str, dict] = {}
        self._progress_at = 0.0
        self._last_completed_cycle_at = 0.0

    def reset(self) -> None:
        """Только для тестов: реестр живёт столько же, сколько процесс демона."""
        with self._lock:
            self._lanes.clear()
            self._progress_at = 0.0
            self._last_completed_cycle_at = 0.0

    def begin(self, name: str, *, lease_seconds: float | None = None) -> None:
        started_at = time.time()
        lease = float(lease_seconds or DEFAULT_OPERATION_LEASE_SECONDS)
        with self._lock:
            lane = self._lanes.get(name)
            if lane is None:
                self._lanes[name] = {
                    "started_at": started_at,
                    "progress_at": started_at,
                    "lease_until": started_at + lease,
                    "depth": 1,
                }
            else:
                # Вложенный вход в ту же полосу не открывает вторую операцию:
                # иначе выход из внутренней снял бы отчётность внешней.
                lane["depth"] += 1
                lane["progress_at"] = started_at
                lane["lease_until"] = started_at + lease
            self._progress_at = max(self._progress_at, started_at)

    def beat(self, name: str, *, lease_seconds: float | None = None) -> None:
        """Шаг внутри долгой операции: единственный законный способ подтвердить
        прогресс, не дожидаясь её конца."""
        observed_at = time.time()
        lease = float(lease_seconds or DEFAULT_OPERATION_LEASE_SECONDS)
        with self._lock:
            lane = self._lanes.get(name)
            if lane is not None:
                lane["progress_at"] = observed_at
                lane["lease_until"] = observed_at + lease
            self._progress_at = max(self._progress_at, observed_at)

    def end(self, name: str) -> None:
        finished_at = time.time()
        with self._lock:
            lane = self._lanes.get(name)
            if lane is not None:
                lane["depth"] -= 1
                if lane["depth"] <= 0:
                    self._lanes.pop(name, None)
            self._progress_at = max(self._progress_at, finished_at)

    def note_completed_cycle(self) -> None:
        """Граница полного рабочего цикла — самое сильное доказательство хода."""
        finished_at = time.time()
        with self._lock:
            self._last_completed_cycle_at = finished_at
            self._progress_at = max(self._progress_at, finished_at)

    def snapshot(self) -> dict:
        """Наблюдаемое состояние для payload-а пульса; без клиентских данных."""
        with self._lock:
            names = sorted(self._lanes, key=lambda key: self._lanes[key]["started_at"])
            # Самая старая операция — та, которая рискует выглядеть зависанием.
            oldest = names[0] if names else ""
            lease_until = min(
                (lane["lease_until"] for lane in self._lanes.values()),
                default=0.0,
            )
            return {
                "inflight_operation": oldest[:64],
                "inflight_operations": ",".join(names)[:200],
                "progress_at": self._progress_at,
                "last_completed_cycle_at": self._last_completed_cycle_at,
                "operation_lease_until": lease_until,
            }


_INFLIGHT = _InflightOperations()


def reset_inflight_operations() -> None:
    """Точка сброса реестра для тестов (в production вызывать незачем)."""
    _INFLIGHT.reset()


def operation_pulse_enabled() -> bool:
    """ЭА.14 «Откат»: без флага payload остаётся прежним, надзор — прежним."""
    try:
        from management.services.ig_task_health import daemon_supervision_enabled

        return bool(daemon_supervision_enabled())
    except Exception:
        return True


class _OperationHandle:
    """Дескриптор операции: позволяет отчитаться о шаге, не зная реестра."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def beat(self, *, lease_seconds: float | None = None) -> None:
        _INFLIGHT.beat(self.name, lease_seconds=lease_seconds)


@contextmanager
def operation_pulse(name: str, *, lease_seconds: float | None = None):
    """Объявить операцию «в полёте» на время блока.

    Никогда не глотает исключение вызывающего и никогда не делает I/O: реестр —
    это память процесса, а публикация в кэш остаётся делом фонового пульса. Так
    долгая операция отчитывается о себе, не входя ни в транзакцию, ни в lock.
    """
    _INFLIGHT.begin(name, lease_seconds=lease_seconds)
    try:
        yield _OperationHandle(name)
    finally:
        _INFLIGHT.end(name)


def _daemon_alive() -> bool:
    hb = cache.get(PROCESS_PULSE_KEY)
    try:
        heartbeat_at = float(hb.get("at")) if isinstance(hb, dict) else float(hb)
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(heartbeat_at and (time.time() - heartbeat_at) < HB_ALIVE_WINDOW)


def _publish_process_pulse(*, owner: str, start_sentinel: float, state: str) -> None:
    """Опубликовать пульс процесса и снимок операций «в полёте».

    `at` двигается каждым тиком фонового потока и доказывает только живость
    процесса. Поля `progress_at` / `last_completed_cycle_at` берутся из реестра и
    двигаются лишь при реальном продвижении — поэтому свежий пульс здесь не может
    выдать зависший цикл за работу (ЭА.14).
    """
    payload = {
        "at": time.time(),
        "sentinel": start_sentinel,
        "kind": "process_pulse",
        "state": state,
        "owner": owner,
        "pid": os.getpid(),
    }
    if operation_pulse_enabled():
        payload.update(_INFLIGHT.snapshot())
    cache.set(PROCESS_PULSE_KEY, payload, HB_ALIVE_WINDOW * 3)
    cache.set(DAEMON_LOCK_KEY, owner, HB_ALIVE_WINDOW * 3)


def _publish_main_progress(
    *,
    owner: str,
    start_sentinel: float,
    cycle: int,
    state: str,
    error_kind: str = "",
) -> None:
    payload = {
        "at": time.time(),
        "sentinel": start_sentinel,
        "kind": "main_progress",
        "state": state,
        "owner": owner,
        "pid": os.getpid(),
        "cycle": max(0, int(cycle)),
        "error_kind": str(error_kind or "")[:80],
    }
    if operation_pulse_enabled():
        snapshot = _INFLIGHT.snapshot()
        # `at` остаётся границей цикла (прежний контракт наблюдаемости), а
        # `progress_at` добавляется рядом: надзор смотрит на самое свежее
        # доказательство хода, не теряя различия между ними.
        payload["progress_at"] = snapshot["progress_at"]
        payload["last_completed_cycle_at"] = snapshot["last_completed_cycle_at"]
        payload["inflight_operation"] = snapshot["inflight_operation"]
    cache.set(MAIN_PROGRESS_KEY, payload, HB_ALIVE_WINDOW * 3)


# Маркери деплою. Демон стежить за НАЙНОВІШИМ з них.
#
# ЭБ.3, знайдено при деплої 2026-08-29. Демон читав лише
# `<django_root>/tmp/restart.txt`, а `git pull` і cPanel-Passenger торкають
# `<repo_root>/tmp/restart.txt` — інший файл на рівень вище. Тому задеплоєний
# код НЕ доходив до живого демона: heartbeat свіжий, `_daemon_code_current()`
# порівнює pid-файл із маркером, якого деплой не змінював, watchdog бачить
# «daemon alive — ok» і нічого не робить. Демон міг тижнями виконувати старий
# код, а виправлення виглядали як «не працюють».
_RESTART_SENTINELS = (
    os.path.join(PROJECT_ROOT, "tmp", "restart.txt"),
    os.path.join(os.path.dirname(PROJECT_ROOT), "tmp", "restart.txt"),
)


def _restart_sentinel_mtime() -> float:
    """Найновіший mtime серед маркерів деплою.

    Максимум, а не перший знайдений: торкання будь-якого з двох файлів мусить
    призводити до перезавантаження, інакше механізм знову стане тихо неробочим.
    """
    newest = 0.0
    for path in _RESTART_SENTINELS:
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


def _daemon_code_current() -> bool:
    """A fresh heartbeat from a process started before deploy is not usable."""
    try:
        return os.path.getmtime(PID_FILE) >= _restart_sentinel_mtime()
    except OSError:
        return False


def _conversation_refresh_wait_seconds(s: InstagramBotSettings) -> int:
    """Use a short cadence only while a resumable scan is making progress."""
    if (
        str(getattr(s, "conversation_discovery_cursor", "") or "").strip()
        and not bot._current_ingress_degradation(s)
    ):
        return CONV_REFRESH_PROGRESS_EVERY
    return CONV_REFRESH_EVERY


def _raise_disconnected_database(exc: Exception, *, lane: str) -> None:
    """Bubble a shared DB deferral past legacy log-and-continue handlers."""
    if isinstance(exc, DbCircuitOpen):
        raise exc
    if _database_disconnected(exc, lane=lane):
        raise DbCircuitOpen("background database work deferred") from exc


def _database_disconnected(exc: Exception, *, lane: str) -> bool:
    if not record_db_failure(exc, lane=lane):
        return False
    close_old_connections()
    return True


def _conv_refresher(stop_event: threading.Event):
    """Фоновий потік: рідко оновлює список тредів (важкий ~25 c виклик),
    тільки коли увімкнено резервний поллінг."""
    while not stop_event.is_set():
        wait_seconds = CONV_REFRESH_EVERY
        try:
            close_old_connections()
            require_database_ready(lane="conv_refresher")
            s = InstagramBotSettings.load()
            if s.receive_via_poll and bot._provider_account_id(s):
                token = bot.get_page_token(s)
                if token:
                    bot.refresh_conv_ids(s, token)
                    s.refresh_from_db(fields=["conversation_discovery_cursor"])
                    wait_seconds = _conversation_refresh_wait_seconds(s)
        except DbCircuitOpen:
            pass
        except Exception as exc:
            if _database_disconnected(exc, lane="conv_refresher"):
                stop_event.wait(2)
                continue
            try:
                bot.log("warning", "conv_refresh", repr(exc))
            except Exception:
                pass
        stop_event.wait(wait_seconds)


def _analysis_worker(stop_event: threading.Event):
    """Drain durable CRM-analysis jobs without coupling them to reply enablement."""
    from management.services.bot_conversation_analysis import (
        process_due_analysis,
        reconcile_analysis_jobs,
    )
    from management.services.ig_analysis_events import process_due_analysis_events

    last_reconcile_at = None
    while not stop_event.is_set():
        try:
            close_old_connections()
            require_database_ready(lane="analysis_worker")
            if not maintenance_status(path=MAINTENANCE_FILE)["active"]:
                monotonic_now = time.monotonic()
                if (
                    last_reconcile_at is None
                    or monotonic_now - last_reconcile_at >= ANALYSIS_RECONCILE_EVERY
                ):
                    try:
                        reconcile_result = reconcile_analysis_jobs(
                            limit=ANALYSIS_RECONCILE_BATCH
                        )
                        if isinstance(reconcile_result, dict):
                            graph_count = int(
                                reconcile_result.get(
                                    "request_graphs_reconciled",
                                    0,
                                )
                                or 0
                            )
                            if graph_count > 0:
                                bot.log(
                                    "info",
                                    "gemini_request_graphs_reconciled",
                                    f"reconciled={graph_count}",
                                )
                        from management.services.ig_typed_memory import (
                            reconcile_typed_memory,
                        )

                        reconcile_typed_memory(limit=ANALYSIS_RECONCILE_BATCH)
                    except Exception as exc:
                        _raise_disconnected_database(exc, lane="analysis_worker")
                        try:
                            bot.log(
                                "error",
                                "conversation_analysis_reconcile",
                                repr(exc),
                            )
                        except Exception:
                            pass
                    else:
                        last_reconcile_at = monotonic_now
                try:
                    process_due_analysis(limit=1)
                except Exception as exc:
                    _raise_disconnected_database(exc, lane="analysis_worker")
                    try:
                        bot.log("error", "conversation_analysis_due", repr(exc))
                    except Exception:
                        pass
                try:
                    event_result = process_due_analysis_events(limit=1)
                    terminal_rejected = int(event_result.get("rejected", 0) or 0)
                    terminal_failed = int(event_result.get("failed", 0) or 0)
                    if terminal_rejected or terminal_failed:
                        bot.log(
                            "error" if terminal_failed else "warning",
                            "conversation_analysis_events_terminal",
                            f"rejected={terminal_rejected} failed={terminal_failed}",
                        )
                except Exception as exc:
                    _raise_disconnected_database(exc, lane="analysis_worker")
                    try:
                        bot.log("error", "conversation_analysis_events", repr(exc))
                    except Exception:
                        pass
        except DbCircuitOpen:
            pass
        except Exception as exc:
            if _database_disconnected(exc, lane="analysis_worker"):
                stop_event.wait(2)
                continue
            try:
                bot.log("error", "conversation_analysis", repr(exc))
            except Exception:
                pass
        finally:
            close_old_connections()
        stop_event.wait(5)


# Прибирання «зависших» інцидентів — housekeeping, а не гарячий шлях: рішення
# про holding і про recovery самі перевіряють вікно склейки. Тому воно живе у
# фоновому потоці й не додає латентності живій відповіді клієнту.
INCIDENT_SWEEP_INTERVAL_SECONDS = 60


def _ai_reply_recovery_worker(stop_event: threading.Event):
    """Drain one failed live-reply recovery independently of deep analysis."""
    from management.services.ig_ai_reply_recovery import process_due_recoveries

    last_sweep = 0.0
    while not stop_event.is_set():
        worked = False
        try:
            close_old_connections()
            require_database_ready(lane="ai_reply_recovery_worker")
            if (
                not maintenance_status(path=MAINTENANCE_FILE)["active"]
                and time.monotonic() - last_sweep >= INCIDENT_SWEEP_INTERVAL_SECONDS
            ):
                last_sweep = time.monotonic()
                try:
                    from management.services.ig_provider_incidents import (
                        close_stale_incidents,
                    )

                    close_stale_incidents()
                except Exception as exc:
                    _raise_disconnected_database(exc, lane="ai_reply_recovery_worker")
                    bot.log("warning", "provider_incident_sweep", repr(exc))
            if not maintenance_status(path=MAINTENANCE_FILE)["active"]:
                worked = bool(process_due_recoveries(limit=1))
        except DbCircuitOpen:
            pass
        except Exception as exc:
            if _database_disconnected(exc, lane="ai_reply_recovery_worker"):
                stop_event.wait(2)
                continue
            try:
                bot.log("error", "ai_reply_recovery", repr(exc))
            except Exception:
                pass
        finally:
            close_old_connections()
        if stop_event.wait(0.5 if worked else 2):
            break


def _permission_transition_worker(stop_event: threading.Event):
    """Apply durable reply-permission changes outside the webhook thread."""
    from management.services.ig_permission_transitions import (
        process_due_permission_transitions,
    )

    while not stop_event.is_set():
        worked = False
        try:
            close_old_connections()
            require_database_ready(lane="permission_transition_worker")
            if not maintenance_status(path=MAINTENANCE_FILE)["active"]:
                worked = bool(process_due_permission_transitions(limit=1))
        except DbCircuitOpen:
            pass
        except Exception as exc:
            if _database_disconnected(exc, lane="permission_transition_worker"):
                stop_event.wait(2)
                continue
            try:
                bot.log(
                    "error",
                    "permission_transition",
                    exc.__class__.__name__,
                )
            except Exception:
                pass
        finally:
            close_old_connections()
        if stop_event.wait(0.25 if worked else 1):
            break


def _inbox_refresh_worker(stop_event: threading.Event):
    """Drain administrator-requested inbox recovery outside the reply loop."""
    from management.services.ig_inbox_refresh import process_refresh_slice

    while not stop_event.is_set():
        worked = False
        try:
            close_old_connections()
            require_database_ready(lane="inbox_refresh_worker")
            if not maintenance_status(path=MAINTENANCE_FILE)["active"]:
                result = process_refresh_slice()
                worked = bool(result.get("worked")) if isinstance(result, dict) else False
        except DbCircuitOpen:
            pass
        except Exception as exc:
            if _database_disconnected(exc, lane="inbox_refresh_worker"):
                stop_event.wait(2)
                continue
            try:
                bot.log("error", "inbox_refresh", repr(exc))
            except Exception:
                pass
        finally:
            close_old_connections()
        if stop_event.wait(0.25 if worked else 2):
            break


def _checkout_lifecycle_worker(stop_event: threading.Event):
    """Drain payment/TTN/delivery events without coupling them to inbox replies."""
    from management.services.ig_lifecycle import dispatch_due_lifecycle_events

    while not stop_event.is_set():
        try:
            close_old_connections()
            require_database_ready(lane="checkout_lifecycle_worker")
            if not maintenance_status(path=MAINTENANCE_FILE)["active"]:
                dispatch_due_lifecycle_events(limit=10)
        except DbCircuitOpen:
            pass
        except Exception as exc:
            if _database_disconnected(exc, lane="checkout_lifecycle_worker"):
                stop_event.wait(2)
                continue
            try:
                bot.log("error", "ig_checkout_lifecycle", repr(exc))
            except Exception:
                pass
        finally:
            close_old_connections()
        if stop_event.wait(5):
            break


def _follow_intelligence_worker(stop_event: threading.Event):
    """Drain demand-created follow jobs and mandatory UGC delivery fairly."""
    from management.services.ig_follow_reconcile import (
        reconcile_follow_intelligence_once,
    )

    while not stop_event.is_set():
        worked = False
        try:
            close_old_connections()
            require_database_ready(lane="follow_intelligence_worker")
            if not maintenance_status(path=MAINTENANCE_FILE)["active"]:
                counts = reconcile_follow_intelligence_once(limit=10)
                worked = bool(
                    int(counts.get("payment_selected", 0) or 0)
                    + int(counts.get("follow_selected", 0) or 0)
                    + int(counts.get("ugc_selected", 0) or 0)
                )
        except DbCircuitOpen:
            pass
        except Exception as exc:
            if _database_disconnected(exc, lane="follow_intelligence_worker"):
                stop_event.wait(2)
                continue
            try:
                bot.log("error", "ig_follow_intelligence", repr(exc))
            except Exception:
                pass
        finally:
            close_old_connections()
        if stop_event.wait(0.5 if worked else 5):
            break


def _reconcile_commercial_episodes_after_reload():
    """Repair source rows written by old workers during the deploy window."""
    from django.core.management import call_command

    close_old_connections()
    try:
        call_command("reconcile_ig_commercial_episodes", passes=3)
    finally:
        close_old_connections()


# Пульс живості демона. Оновлюється фоновим потоком, поки основний цикл робить
# довгу провайдерську роботу. `HB_PULSE_INTERVAL` свідомо значно менший за
# `HB_ALIVE_WINDOW`, щоб один пропущений тик не виглядав смертю процесу.
HB_PULSE_INTERVAL = 10


def _progress_pulse(stop_event, owner: str, start_sentinel) -> None:
    """Publish process liveness without claiming that main work progressed.

    Це advisory-нагляд: збій кешу тут ніколи не має зупиняти цикл відповідей.
    """
    while not stop_event.wait(HB_PULSE_INTERVAL):
        try:
            _publish_process_pulse(
                owner=owner,
                start_sentinel=start_sentinel,
                state="running",
            )
        except Exception:
            # Наступний тик спробує знову; помилку видно у логах кешу.
            pass


@contextmanager
def _daemon_runtime_hooks(stop_event: threading.Event):
    """Convert termination signals and uncaught worker errors into clean drain."""
    previous_handlers = {}
    previous_thread_hook = threading.excepthook
    outcome = {"signal": None, "thread_exception": ""}

    def request_stop(signum, _frame):
        outcome["signal"] = int(signum)
        stop_event.set()

    def thread_failed(args):
        outcome["thread_exception"] = str(
            getattr(args.exc_type, "__name__", "BaseException")
        )[:80]
        stop_event.set()
        try:
            bot.log(
                "error",
                "daemon_thread_exception",
                f"thread={str(getattr(args.thread, 'name', ''))[:80]} "
                f"kind={outcome['thread_exception']}",
            )
        except Exception:
            pass

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
        threading.excepthook = thread_failed
        yield outcome
    finally:
        threading.excepthook = previous_thread_hook
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


# =============================================================================
# ЭА.15 — изоляция клиентской полосы от обслуживающих задач цикла
# =============================================================================
#
# Что было не так. В одном цикле последовательно выполнялись drain уведомлений
# менеджеру, обновление профилей и только ЗАТЕМ обработка входящих. То есть ответ
# клиенту стоял в очереди за обслуживающими задачами, и время его начала зависело
# от их объёма: медленный Telegram или большой батч профилей прямо увеличивали
# задержку живого ответа. Ни одна из этих задач не имеет отношения к тому
# сообщению, которое клиент ждёт прямо сейчас.
#
# Честная граница. Полная сервисная декомпозиция — вариант C раздела 10 источника,
# и источник прямо советует НЕ брать его как первый шаг. Поэтому здесь сделан
# минимум: клиентская полоса не ждёт обслуживающих задач.
#
# Почему НЕ отдельный поток на уведомления. Соблазн очевиден, но `IgBotNotification`
# уже пишет фоновый `_ai_reply_recovery_worker`. Вынос drain-а в свой поток создал
# бы второго писателя одной строки — то есть заменил бы задержку ответа на гонку
# доставки уведомлений. Поэтому обслуживающие полосы остаются на том же потоке,
# что и цикл, но ПОСЛЕ клиентской полосы и с собственным бюджетом. Требование
# «своя частота либо свой поток» выполняется через свой интервал и свой бюджет.
CUSTOMER_LANE = "customer_inbound"
SERVICE_LANE_NOTIFICATIONS = "manager_notifications"
SERVICE_LANE_PROFILES = "profile_refresh"
SERVICE_LANE_RECLAIM_GUARD = "reclaim_lease_guard"

# Бюджет одной обслуживающей полосы на цикл. Величины разные, потому что цена
# перерасхода разная: уведомления — сетевой I/O с внешним лимитом, профили —
# батч к Meta, guard — один запрос по индексу.
SERVICE_LANE_BUDGET_SECONDS = {
    SERVICE_LANE_NOTIFICATIONS: 10.0,
    SERVICE_LANE_PROFILES: 15.0,
    SERVICE_LANE_RECLAIM_GUARD: 5.0,
}
# Перерасход бюджета не прерывает уже начатый вызов (прервать чужой сетевой вызов
# извне нельзя), но снимает у полосы право на следующие циклы. Так одна полоса не
# может занимать цикл подряд: она получает не больше одного цикла из четырёх.
SERVICE_LANE_DEFER_CYCLES = 3
# Guard над reclaim-ом — наблюдение, а не горячий путь: раз в минуту достаточно.
RECLAIM_GUARD_EVERY_SECONDS = 60.0


class _ServiceLaneScheduler:
    """Свой интервал, свой бюджет и своя телеметрия у каждой полосы.

    Состояние живёт в процессе демона, как и сам цикл. `reset()` существует для
    тестов: без него порядок выполнения тестов влиял бы на решения планировщика.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cycle = 0
        self._deferred_until: dict[str, int] = {}
        self._last_ran_at: dict[str, float] = {}
        self._last_duration_ms: dict[str, int] = {}

    def reset(self) -> None:
        with self._lock:
            self._cycle = 0
            self._deferred_until.clear()
            self._last_ran_at.clear()
            self._last_duration_ms.clear()

    def begin_cycle(self) -> int:
        with self._lock:
            self._cycle += 1
            return self._cycle

    def timings(self) -> dict:
        """Замер ЭА.15: сколько времени занимает каждая полоса. Без клиентских данных."""
        with self._lock:
            return {
                "lane_ms": dict(self._last_duration_ms),
                "lane_deferred": sorted(
                    name
                    for name, until in self._deferred_until.items()
                    if until > self._cycle
                ),
            }

    def may_run(self, name: str, min_interval_seconds: float = 0.0) -> bool:
        with self._lock:
            if self._deferred_until.get(name, 0) > self._cycle:
                return False
            if min_interval_seconds > 0:
                last = self._last_ran_at.get(name)
                if last is not None and (time.monotonic() - last) < min_interval_seconds:
                    return False
            return True

    def record(self, name: str, duration_seconds: float, budget_seconds: float) -> bool:
        """Вернуть True, если полоса вышла за бюджет и должна быть отложена."""
        with self._lock:
            self._last_ran_at[name] = time.monotonic()
            self._last_duration_ms[name] = int(max(0.0, duration_seconds) * 1000)
            if duration_seconds > budget_seconds:
                self._deferred_until[name] = self._cycle + SERVICE_LANE_DEFER_CYCLES
                return True
            self._deferred_until.pop(name, None)
            return False


_SERVICE_LANES = _ServiceLaneScheduler()


def reset_service_lanes() -> None:
    """Точка сброса планировщика полос для тестов."""
    _SERVICE_LANES.reset()


def service_lane_timings() -> dict:
    """Наблюдаемость ЭА.15: длительности полос последнего цикла."""
    return _SERVICE_LANES.timings()


def _run_service_lane(
    name: str,
    work,
    *,
    error_event: str,
    error_level: str = "error",
    min_interval_seconds: float = 0.0,
) -> bool:
    """Выполнить обслуживающую полосу под собственным pulse и собственным бюджетом.

    Возвращает True, если полоса действительно выполнялась. Исключение полосы
    никогда не выходит наружу: доступность обслуживающей задачи не имеет права
    становиться глобальным выключателем ответов клиенту.
    """
    budget = float(SERVICE_LANE_BUDGET_SECONDS.get(name, 10.0))
    if not _SERVICE_LANES.may_run(name, min_interval_seconds):
        return False
    started = time.monotonic()
    try:
        with operation_pulse(name, lease_seconds=budget * 4):
            work()
    except Exception as exc:
        bot.log(error_level, error_event, repr(exc))
    finally:
        duration = time.monotonic() - started
        if _SERVICE_LANES.record(name, duration, budget):
            try:
                bot.log(
                    "warning",
                    "service_lane_overrun",
                    f"lane={name} spent={duration:.1f}s budget={budget:.1f}s "
                    f"deferred_cycles={SERVICE_LANE_DEFER_CYCLES}",
                )
            except Exception:
                pass
    return True


def _reclaim_lease_guard() -> None:
    """Guardrail ЭА.14: строки, которые абсолютный порог отобрал бы у живого владельца.

    Первичный отбор в `instagram_bot.reclaim_stale_processing()` идёт по
    абсолютному возрасту `processing_started_at`, а операционный lease там —
    только вторичный предохранитель (`client_automation_busy`). Пока первичный
    ключ не переведён на lease, важно хотя бы ВИДЕТЬ расхождение: строка старше
    конфигурационного порога, но её операционный lease ещё не истёк. Каждая такая
    строка — кандидат на отбор у живого владельца, а значит на неопределённую
    доставку клиенту.
    """
    from django.conf import settings as django_settings

    from management.models import InstagramBotMessage
    from management.services.ig_task_health import (
        operational_reclaim_age_seconds,
        processing_lease_expired,
    )

    try:
        absolute_threshold = int(
            getattr(django_settings, "IG_BOT_STALE_PROCESSING_SECONDS", 300)
        )
    except (TypeError, ValueError):
        absolute_threshold = 300
    now = timezone.now()
    absolute_cutoff = now - timedelta(seconds=max(1, absolute_threshold))
    lease_age = operational_reclaim_age_seconds()
    candidates = list(
        InstagramBotMessage.objects.filter(
            role=InstagramBotMessage.Role.USER,
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at__lt=absolute_cutoff,
        )
        .select_related("client")
        .order_by("processing_started_at")[:50]
    )
    protected = 0
    for row in candidates:
        client = row.client if row.client_id else None
        renewed_at = getattr(client, "automation_lease_until", None)
        if not processing_lease_expired(
            claimed_at=row.processing_started_at,
            progress_at=renewed_at,
            now=now,
            lease_seconds=lease_age,
        ):
            protected += 1
    if protected:
        bot.log(
            "warning",
            "reclaim_lease_conflict",
            f"rows={protected} absolute_threshold={absolute_threshold}s "
            f"operational_lease={lease_age}s",
        )


def _run_legacy_work_cycle(settings_obj, last_poll: float) -> tuple[bool, float]:
    """Порядок цикла до ЭА.15 — сохранён целиком для отката по флагу."""
    enabled = bool(settings_obj.is_enabled)
    interval = max(2, settings_obj.poll_interval_seconds or 3)
    try:
        bot.drain_manager_notifications(limit=10)
    except Exception as exc:
        # Manager-alert availability must never become a global customer-reply
        # kill switch. The next cycle retries and the error remains visible in
        # the operational log/status surface.
        bot.log("error", "notification_outbox", repr(exc))
    profile_key = f"ig_profile_batch:{bot._provider_owner_id(settings_obj)}"
    if cache.add(profile_key, "1", timeout=bot.PROFILE_REFRESH_INTERVAL):
        try:
            bot.refresh_profiles_batch(settings_obj)
        except Exception as exc:
            bot.log("warning", "profile_refresh_batch", repr(exc))
    if enabled:
        bot.process_pending(settings_obj)
        if maintenance_status(path=MAINTENANCE_FILE)["active"]:
            return enabled, last_poll
        bot_followups.process_due_followups(settings_obj)
    now = time.time()
    if settings_obj.receive_via_poll and (now - last_poll) >= interval:
        poll_result = bot.poll_ingest(settings_obj)
        if isinstance(poll_result, dict) and settings_obj.pk:
            poll_ok = bool(poll_result.get("ok")) and not bool(
                poll_result.get("degraded") or poll_result.get("refresh_pending")
            )
            if poll_ok:
                settings_obj.last_poll_at = timezone.now()
                if settings_obj.last_error.startswith("polling:"):
                    settings_obj.last_error = ""
            else:
                reason = (
                    poll_result.get("error")
                    or poll_result.get("reason")
                    or "provider_unavailable"
                )
                settings_obj.last_error = f"polling:{reason}"[:2000]
            settings_obj.save(update_fields=["last_poll_at", "last_error", "updated_at"])
        if enabled:
            bot.process_pending(settings_obj)
            if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                return enabled, last_poll
            bot_followups.process_due_followups(settings_obj)
        last_poll = now
    return enabled, last_poll


def _service_task_isolation_enabled() -> bool:
    """ЭА.15 «Откат»: флаг на новый порядок цикла."""
    try:
        from management.services.ig_provider_incidents import flag

        return flag("IG_BOT_SERVICE_TASK_ISOLATION", True)
    except Exception:
        return True


def _customer_lane(settings_obj, last_poll: float) -> tuple[bool, float, bool]:
    """Клиентская полоса: приём входящих и ответ. Ничего обслуживающего внутри.

    Третий элемент результата — признак «цикл прерван по maintenance»: тогда
    обслуживающие полосы запускать нельзя, иначе граница обслуживания не была бы
    границей.
    """
    enabled = bool(settings_obj.is_enabled)
    interval = max(2, settings_obj.poll_interval_seconds or 3)
    with operation_pulse(CUSTOMER_LANE) as pulse:
        if enabled:
            bot.process_pending(settings_obj)
            pulse.beat()
            if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                return enabled, last_poll, True
            bot_followups.process_due_followups(settings_obj)
            pulse.beat()
        now = time.time()
        if settings_obj.receive_via_poll and (now - last_poll) >= interval:
            poll_result = bot.poll_ingest(settings_obj)
            pulse.beat()
            if isinstance(poll_result, dict) and settings_obj.pk:
                poll_ok = bool(poll_result.get("ok")) and not bool(
                    poll_result.get("degraded") or poll_result.get("refresh_pending")
                )
                if poll_ok:
                    settings_obj.last_poll_at = timezone.now()
                    if settings_obj.last_error.startswith("polling:"):
                        settings_obj.last_error = ""
                else:
                    reason = (
                        poll_result.get("error")
                        or poll_result.get("reason")
                        or "provider_unavailable"
                    )
                    settings_obj.last_error = f"polling:{reason}"[:2000]
                settings_obj.save(
                    update_fields=["last_poll_at", "last_error", "updated_at"]
                )
            if enabled:
                bot.process_pending(settings_obj)
                pulse.beat()
                if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                    return enabled, last_poll, True
                bot_followups.process_due_followups(settings_obj)
                pulse.beat()
            last_poll = now
    return enabled, last_poll, False


def _run_service_lanes(settings_obj) -> None:
    """Обслуживающие полосы — ПОСЛЕ клиентской, каждая со своим pulse и бюджетом."""
    _run_service_lane(
        SERVICE_LANE_NOTIFICATIONS,
        lambda: bot.drain_manager_notifications(limit=10),
        error_event="notification_outbox",
    )
    profile_key = f"ig_profile_batch:{bot._provider_owner_id(settings_obj)}"
    if cache.add(profile_key, "1", timeout=bot.PROFILE_REFRESH_INTERVAL):
        _run_service_lane(
            SERVICE_LANE_PROFILES,
            lambda: bot.refresh_profiles_batch(settings_obj),
            error_event="profile_refresh_batch",
            error_level="warning",
        )
    _run_service_lane(
        SERVICE_LANE_RECLAIM_GUARD,
        _reclaim_lease_guard,
        error_event="reclaim_lease_guard",
        error_level="warning",
        min_interval_seconds=RECLAIM_GUARD_EVERY_SECONDS,
    )


def _revision_receipt_tick(settings_obj):
    """DB-only receipts remain observable in poll-only, pause and flag rollback."""
    if not cache.add("ig_revision_receipt_tick:v1", True, timeout=2):
        return
    from management.services.ig_revision_echo_integration import reconcile_pending_revision_echoes
    from management.services.ig_revision_execution import finalization_due_ids, finalize_sent_revision_effects

    reconcile_pending_revision_echoes(settings_obj, limit=5)
    for revision_id in finalization_due_ids(limit=5):
        finalize_sent_revision_effects(revision_id)


def _run_work_cycle(settings_obj, last_poll: float) -> tuple[bool, float]:
    """Один рабочий цикл. Порядок полос задан ЯВНО, и вот почему именно такой.

    1. КЛИЕНТСКАЯ полоса — первая. Это единственная задача, у которой есть
       внешний наблюдатель, ждущий прямо сейчас. Всё, что стоит перед ней,
       превращается в задержку живого ответа; ни уведомление менеджеру, ни
       обновление профилей не становятся от ожидания хуже, а ответ клиенту —
       становится.
    2. ОБСЛУЖИВАЮЩИЕ полосы — после, каждая со своим pulse и своим бюджетом на
       цикл. Свой pulse обязателен: без него ЭА.14 видел бы прогресс уведомлений
       и считал, что двигается клиентская обработка. Свой бюджет обязателен:
       иначе одна медленная полоса снова заняла бы цикл целиком и вернула бы
       задержку ответа через заднюю дверь.
    3. maintenance проверяется ВНУТРИ клиентской полосы и прерывает цикл до
       обслуживающих полос: иначе граница обслуживания не была бы границей.

    Порядок до ЭА.15 сохранён в `_run_legacy_work_cycle` и включается флагом.
    """
    require_database_ready(lane="daemon_cycle")
    try:
        _revision_receipt_tick(settings_obj)
    except DbCircuitOpen:
        raise
    except Exception as exc:
        from management.services.ig_db_circuit import record_db_failure

        if record_db_failure(exc, lane="revision_receipts"):
            close_old_connections()
            raise DbCircuitOpen("revision receipt work deferred") from exc
        if cache.add("ig_revision_receipt_error_notice", True, timeout=60):
            bot.log("error", "revision_receipt_tick", type(exc).__name__)

    # Ingress is part of the customer lane, including while replies are
    # disabled: echoes and opt-outs must still acquire their permission fence.
    if (
        bool(getattr(settings, "IG_WEBHOOK_INBOX_ENABLED", True))
        and bot._provider_account_id(settings_obj)
    ):
        from management.services.ig_webhook_inbox import drain_webhook_inbox

        try:
            drain_webhook_inbox(settings_obj, limit=25)
        except DbCircuitOpen:
            raise
        except Exception as exc:
            from management.services.ig_db_circuit import record_db_failure

            if record_db_failure(exc, lane="webhook_inbox"):
                close_old_connections()
                raise DbCircuitOpen("inbox database work deferred") from exc
            if cache.add("ig_inbox_drain_error_notice", True, timeout=60):
                bot.log("error", "webhook_inbox_drain", type(exc).__name__)
            return bool(settings_obj.is_enabled), last_poll
    if not _service_task_isolation_enabled():
        return _run_legacy_work_cycle(settings_obj, last_poll)
    _SERVICE_LANES.begin_cycle()
    enabled, last_poll, interrupted = _customer_lane(settings_obj, last_poll)
    if interrupted:
        return enabled, last_poll
    _run_service_lanes(settings_obj)
    return enabled, last_poll


def daemon_supervision_verdict(
    *,
    lock_held: bool | None = None,
    child_exit_code: int | None = None,
    child_exit_signal: int | None = None,
):
    """Свести наблюдения этого хоста к одному из четырёх состояний ЭА.14.

    Наблюдения собираются здесь (кэш + файл-lock), а решение принимает чистая
    функция в `ig_task_health`. Разделение сделано ради теста: гонку «живой
    процесс vs watchdog» иначе пришлось бы воспроизводить, поднимая демон.
    """
    from management.services.ig_task_health import classify_daemon_supervision

    if lock_held is None:
        lock_held = _process_lock_held(DAEMON_LOCK_FILE)
    try:
        pulse = cache.get(PROCESS_PULSE_KEY)
    except Exception:
        pulse = None
    try:
        progress = cache.get(MAIN_PROGRESS_KEY)
    except Exception:
        progress = None
    return classify_daemon_supervision(
        pulse=pulse if isinstance(pulse, dict) else None,
        progress=progress if isinstance(progress, dict) else None,
        lock_held=bool(lock_held),
        child_exit_code=child_exit_code,
        child_exit_signal=child_exit_signal,
        alive_window_seconds=HB_ALIVE_WINDOW,
    )


def observe_daemon_supervision(
    *,
    lock_held: bool | None = None,
    child_exit_code: int | None = None,
    child_exit_signal: int | None = None,
):
    """Действие watchdog зависит от состояния, а не от одного признака живости.

    Ключевое различие ЭА.14 и есть здесь: `process_alive_and_progressing` →
    НИЧЕГО; `process_alive_but_no_progress` → эскалация без перезапуска
    (перезапуск не лечит зависание, зато делает доставку клиенту неопределённой);
    `lock_stale_no_owner` и `child_exited` → поднять демона.

    Возвращает verdict или None, если флаг этапа выключен (тогда watchdog ведёт
    себя как до этапа — один булев признак `_daemon_alive()`).
    """
    from management.services.ig_task_health import (
        DAEMON_ACTION_ESCALATE,
        daemon_supervision_enabled,
        escalate_daemon_supervision,
    )

    if not daemon_supervision_enabled():
        return None
    verdict = daemon_supervision_verdict(
        lock_held=lock_held,
        child_exit_code=child_exit_code,
        child_exit_signal=child_exit_signal,
    )
    if verdict.action == DAEMON_ACTION_ESCALATE:
        try:
            bot.log("error", "daemon_no_progress", verdict.as_log_suffix())
        except Exception:
            pass
        escalate_daemon_supervision(verdict)
    return verdict


class Command(BaseCommand):
    help = "Раннер Instagram-бота (демон / watchdog / одиночний прохід)."

    @staticmethod
    def _guard_runtime_database() -> None:
        """Never let a production-like bot silently operate on SQLite."""
        if not settings.DEBUG and connection.vendor == "sqlite":
            raise CommandError(
                "run_instagram_bot refuses SQLite when DEBUG=False; "
                "configure the CloudLinux-bound MariaDB runtime"
            )

    def add_arguments(self, parser):
        parser.add_argument("--forever", action="store_true", help="Постійний демон.")
        parser.add_argument("--ensure", action="store_true", help="Watchdog: підняти демона, якщо мертвий.")
        parser.add_argument("--once", action="store_true", help="Один прохід.")
        parser.add_argument(
            "--maintenance-on",
            nargs="?",
            const=DEFAULT_MAINTENANCE_SECONDS,
            type=int,
            metavar="SECONDS",
            help="Зупинити daemon і заблокувати watchdog на обмежений час.",
        )
        parser.add_argument(
            "--maintenance-off",
            metavar="LEASE_ID",
            help="Зняти лише власний maintenance lease; потім запустіть --ensure.",
        )
        parser.add_argument(
            "--maintenance-lease-id",
            metavar="LEASE_ID",
            help="Внутрішній token для атомарного maintenance-on від release orchestrator.",
        )
        parser.add_argument(
            "--maintenance-wait-seconds",
            type=float,
            metavar="SECONDS",
            help=(
                "Максимальний час очікування штатного звільнення daemon lock "
                f"(0..{MAX_RELOAD_LOCK_WAIT_SECONDS})."
            ),
        )

    def handle(self, *args, **opts):
        selected = sum(
            bool(opts.get(name))
            for name in ("once", "ensure", "forever")
        )
        selected += int(opts.get("maintenance_on") is not None)
        selected += int(opts.get("maintenance_off") is not None)
        if selected > 1:
            raise CommandError("choose exactly one daemon mode")
        if opts.get("maintenance_lease_id") and opts.get("maintenance_on") is None:
            raise CommandError("--maintenance-lease-id requires --maintenance-on")
        if opts.get("maintenance_on") is not None:
            return self._maintenance_on(
                opts["maintenance_on"],
                requested_lease_id=opts.get("maintenance_lease_id"),
                wait_seconds=opts.get("maintenance_wait_seconds"),
            )
        if opts.get("maintenance_off") is not None:
            try:
                deactivate_maintenance(
                    lease_id=opts["maintenance_off"],
                    path=MAINTENANCE_FILE,
                )
            except MaintenanceLeaseConflict as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write("maintenance disabled")
            return
        if (
            opts.get("once")
            or opts.get("ensure")
            or opts.get("forever")
        ):
            self._guard_runtime_database()
        if opts.get("once"):
            if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                raise CommandError("maintenance active — --once refused")
            res = bot.poll_once(InstagramBotSettings.load())
            self.stdout.write(f"poll_once: {res}")
            return

        if opts.get("ensure"):
            with task_heartbeat("ig_daemon_watchdog"):
                return self._ensure()

        if opts.get("forever"):
            return self._forever()

        self.stdout.write(
            "Вкажіть режим: --forever | --ensure | --once | --maintenance-on | --maintenance-off"
        )

    def _maintenance_on(
        self,
        duration_seconds: int,
        *,
        requested_lease_id: str | None = None,
        wait_seconds: int | float | None = None,
    ):
        bounded_wait = _bounded_reload_lock_wait(wait_seconds)
        try:
            payload = activate_maintenance(
                path=MAINTENANCE_FILE,
                duration_seconds=duration_seconds,
                actor="run_instagram_bot",
                requested_lease_id=requested_lease_id,
            )
        except (MaintenanceLeaseConflict, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        # The daemon observes the lease before its next work cycle. Do not
        # report a safe maintenance boundary until its OS singleton is free.
        if _process_lock_held(DAEMON_LOCK_FILE) and not _wait_for_lock(
            DAEMON_LOCK_FILE,
            held=False,
            timeout=bounded_wait,
        ):
            try:
                deactivate_maintenance(
                    path=MAINTENANCE_FILE,
                    lease_id=str(payload["lease_id"]),
                )
            except MaintenanceLeaseConflict:
                # Another owner replaced the marker after activation. Exact
                # token cleanup must preserve that newer lease.
                pass
            except OSError as exc:
                raise CommandError(
                    "daemon did not stop and owned maintenance lease cleanup failed"
                ) from exc
            raise CommandError("daemon did not stop after maintenance activation")
        self.stdout.write(
            f"maintenance active lease_id={payload['lease_id']} "
            f"expires_at={payload['expires_at']:.0f}"
        )

    # ------------------------------------------------------------------
    def _ensure(self):
        with _try_process_lock(SPAWN_LOCK_FILE) as spawn_lock:
            if spawn_lock is None:
                self.stdout.write("spawn in progress — skip")
                return
            if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                self.stdout.write("maintenance active — watchdog skip")
                return
            # The stdlib supervisor is the preferred daemon parent because it
            # can wait for the child and attribute exit code/signal/uptime.
            # Keep this management mode as a deployment/manual fallback, but
            # never race the supervisor for child ownership.
            if _supervisor_active():
                if _daemon_code_current() and _daemon_alive():
                    # ЭА.14: свежий пульс доказывает живость, но не движение.
                    # Если прогресса нет дольше порога — эскалация, а НЕ замена
                    # владельца: перезапуск не лечит зависание, зато превращает
                    # уже сделанный вызов Meta в неизвестный результат доставки.
                    observe_daemon_supervision(lock_held=True)
                    self.stdout.write("daemon alive under supervisor — ok")
                    return
                if _wait_for_daemon_ready(timeout=DAEMON_START_WAIT_SECONDS):
                    self.stdout.write("daemon ready under supervisor — ok")
                    return
                # Guardrail: «start pending» не имеет права длиться вечно молча.
                # Ребёнок supervisor-а может быть жив и при этом не двигаться —
                # это состояние обязано быть видно человеку.
                observe_daemon_supervision()
                self.stdout.write("supervisor active — daemon start pending")
                return
            if _process_lock_held(DAEMON_LOCK_FILE):
                # A held singleton lock only proves that a process exists. A
                # hung worker can keep the lock while its heartbeat is stale;
                # let the bounded reload path recover it instead of reporting
                # a false healthy daemon to cron.
                if _daemon_code_current() and _daemon_alive():
                    # Тот же контракт для fallback-пути: прогресс есть — ничего
                    # не делаем, прогресса нет — эскалируем, но не подменяем
                    # живого владельца singleton-lock.
                    observe_daemon_supervision(lock_held=True)
                    _clear_starting_child()
                    self.stdout.write("daemon alive — ok")
                    return
                starting_state = _starting_child_state()
                if starting_state == "current":
                    self.stdout.write("daemon starting — pending")
                    return
                if starting_state == "stale":
                    raise CommandError(
                        "daemon startup exceeded pending window while holding singleton lock"
                    )
                # Old code sees restart.txt and exits within at most one idle
                # loop. Never spawn while it still owns the process lock.
                if not _wait_for_lock(
                    DAEMON_LOCK_FILE,
                    held=False,
                    timeout=RELOAD_LOCK_WAIT_SECONDS,
                ):
                    raise CommandError("stale daemon did not release singleton lock during reload")
            starting_state = _starting_child_state()
            if starting_state == "current":
                self.stdout.write("daemon starting — pending")
                return
            if starting_state == "stale":
                raise CommandError("daemon startup exceeded pending window without singleton lock")
            log_dir = os.path.join(PROJECT_ROOT, "tmp")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "ig_bot_daemon.log")
            try:
                with open(log_path, "a") as logf:
                    child = subprocess.Popen(
                        [sys.executable, MANAGE_PY_PATH, "run_instagram_bot", "--forever"],
                        stdout=logf,
                        stderr=logf,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        cwd=PROJECT_ROOT,
                        env=os.environ.copy(),
                    )
                _record_starting_child(getattr(child, "pid", 0))
                if not _wait_for_lock(
                    DAEMON_LOCK_FILE,
                    held=True,
                    timeout=DAEMON_START_WAIT_SECONDS,
                ):
                    # A cron/watchdog process can win the singleton race while
                    # our child exits normally. Reconcile lock + heartbeat once
                    # more before reporting failure.
                    if _process_lock_held(DAEMON_LOCK_FILE) and _daemon_alive():
                        _clear_starting_child(
                            expected_pid=getattr(child, "pid", None)
                        )
                        self.stdout.write("daemon alive — ok")
                        return
                    return_code = child.poll()
                    if return_code is None:
                        self.stdout.write("daemon starting — pending")
                        return
                    _clear_starting_child(
                        expected_pid=getattr(child, "pid", None)
                    )
                    raise CommandError(
                        f"daemon child exited with code {return_code} "
                        "before acquiring singleton lock"
                    )
                if not _wait_for_daemon_ready(timeout=DAEMON_READY_WAIT_SECONDS):
                    return_code = child.poll()
                    if return_code is None:
                        self.stdout.write("daemon starting — pending")
                        return
                    _clear_starting_child(
                        expected_pid=getattr(child, "pid", None)
                    )
                    raise CommandError(
                        f"daemon child exited with code {return_code} "
                        "during initialization"
                    )
                _clear_starting_child(
                    expected_pid=getattr(child, "pid", None)
                )
                bot.log("info", "daemon_spawn", "watchdog підняв демона")
                self.stdout.write("daemon spawned")
            except CommandError:
                raise
            except Exception as exc:
                raise CommandError(f"daemon spawn failed: {exc!r}") from exc

    # ------------------------------------------------------------------
    def _forever(self):
        if maintenance_status(path=MAINTENANCE_FILE)["active"]:
            self.stdout.write("maintenance active — daemon exit")
            return
        with _try_process_lock(DAEMON_LOCK_FILE) as daemon_lock:
            if daemon_lock is None:
                self.stdout.write("daemon already alive — exit")
                return
            return self._forever_locked()

    def _forever_locked(self):
        if maintenance_status(path=MAINTENANCE_FILE)["active"]:
            self.stdout.write("maintenance active — daemon exit")
            return
        owner = f"{os.getpid()}:{time.time_ns()}"
        start_sentinel = _restart_sentinel_mtime()
        stop_event = threading.Event()
        # ЭА.14. Порядок здесь — исправление, а не косметика. Раньше первым шагом
        # был `_reconcile_commercial_episodes_after_reload()`, и лишь ПОСЛЕ его
        # возврата процесс публиковал пульс и писал pid-файл. Всё это время
        # процесс уже держал singleton-lock, но не был наблюдаем: `_daemon_alive()`
        # ложен (пульса нет), `_daemon_code_current()` ложен (mtime pid-файла
        # старше маркера деплоя). Ровно эта комбинация ведёт watchdog в ветку
        # замены владельца — то есть окно старта выглядело зависшим демоном.
        # Реконсиляция релизного окна ограничена тремя проходами и на живой базе
        # занимает секунды, но её длительность не наша: она зависит от объёма
        # данных, поэтому наблюдаемость обязана начинаться ДО неё.
        _publish_process_pulse(
            owner=owner,
            start_sentinel=start_sentinel,
            state="starting",
        )
        _publish_main_progress(
            owner=owner,
            start_sentinel=start_sentinel,
            cycle=0,
            state="starting",
        )
        try:
            os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
            with open(PID_FILE, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
        # Пульс живости стартует раньше любой работы: пока идёт реконсиляция,
        # процесс уже обязан доказывать, что он жив. Поток остановится в `finally`
        # ниже даже если реконсиляция бросит исключение.
        progress_pulse = threading.Thread(
            name="ig-process-pulse",
            target=_progress_pulse,
            args=(stop_event, owner, start_sentinel),
            daemon=True,
        )
        progress_pulse.start()
        try:
            # This process starts only after the previous daemon released the
            # singleton lock. Reconcile once with the newly deployed code before
            # any notification, analysis, payment, or reply work can run.
            with operation_pulse("release_reconcile") as reconcile_pulse:
                _reconcile_commercial_episodes_after_reload()
                reconcile_pulse.beat()
        except BaseException:
            stop_event.set()
            progress_pulse.join(timeout=1)
            raise
        _clear_starting_child(expected_pid=os.getpid())
        bot.log("success", "daemon_start", f"Демон онлайн (pid {os.getpid()}).")

        # Sentinel: запам'ятовуємо mtime tmp/restart.txt (його torkає кожен деплой).
        # Якщо файл змінився — демон штатно виходить, watchdog (--ensure) підніме
        # процес із НОВИМ кодом. Без цього --forever крутив би старий код у пам'яті.
        # Фоновий потік для важкого /conversations (поза гарячим циклом).
        # `stop_event` і потік пульсу вже створені вище: наглядність мусить
        # починатись до реконсиляції релізного вікна, а не після неї.
        refresher = threading.Thread(
            name="ig-conversation-refresh",
            target=_conv_refresher,
            args=(stop_event,),
            daemon=True,
        )
        refresher.start()
        analysis_worker = threading.Thread(
            name="ig-analysis",
            target=_analysis_worker,
            args=(stop_event,),
            daemon=True,
        )
        analysis_worker.start()
        recovery_worker = threading.Thread(
            name="ig-reply-recovery",
            target=_ai_reply_recovery_worker,
            args=(stop_event,),
            daemon=True,
        )
        recovery_worker.start()
        permission_transition_worker = threading.Thread(
            name="ig-permission-transition",
            target=_permission_transition_worker,
            args=(stop_event,),
            daemon=True,
        )
        permission_transition_worker.start()
        inbox_refresh_worker = threading.Thread(
            name="ig-inbox-refresh",
            target=_inbox_refresh_worker,
            args=(stop_event,),
            daemon=True,
        )
        inbox_refresh_worker.start()
        lifecycle_worker = threading.Thread(
            name="ig-checkout-lifecycle",
            target=_checkout_lifecycle_worker,
            args=(stop_event,),
            daemon=True,
        )
        lifecycle_worker.start()
        follow_intelligence_worker = threading.Thread(
            name="ig-follow-intelligence",
            target=_follow_intelligence_worker,
            args=(stop_event,),
            daemon=True,
        )
        follow_intelligence_worker.start()
        # Process pulse remains fresh during a long provider call, proving only
        # process liveness. MAIN_PROGRESS_KEY is advanced by the main loop and
        # independently exposes a stuck cycle instead of triggering a duplicate
        # daemon over a valid OS singleton. Потік уже запущено до реконсиляції.

        from django.utils import timezone as tz

        last_poll = 0.0
        last_task_health_check = 0.0
        task_expectations_registered = False
        workers = (
            refresher,
            analysis_worker,
            recovery_worker,
            permission_transition_worker,
            inbox_refresh_worker,
            lifecycle_worker,
            follow_intelligence_worker,
            progress_pulse,
        )
        cycle = 0
        with _daemon_runtime_hooks(stop_event) as shutdown:
            try:
                while not stop_event.is_set():
                    close_old_connections()  # лікує "MySQL server has gone away"
                    if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                        bot.log("info", "daemon_maintenance", "Maintenance активний — daemon зупинено")
                        break
                    if _restart_sentinel_mtime() != start_sentinel:
                        bot.log("info", "daemon_reload",
                                "restart.txt змінено — демон перезавантажується для нового коду")
                        break
                    cycle += 1
                    enabled = False
                    cycle_state = "idle"
                    _publish_main_progress(
                        owner=owner,
                        start_sentinel=start_sentinel,
                        cycle=cycle,
                        state="running",
                    )
                    try:
                        require_database_ready(lane="main_loop")
                        if not task_expectations_registered:
                            task_expectations_registered = ensure_task_expectations()
                        s = InstagramBotSettings.load()
                        enabled, last_poll = _run_work_cycle(s, last_poll)
                        # Завершённый цикл — самое сильное доказательство хода;
                        # только оно двигает `last_completed_cycle_at`. Фоновый
                        # пульс этого сделать не вправе, иначе свежий пульс снова
                        # маскировал бы зависший цикл (ЭА.14).
                        _INFLIGHT.note_completed_cycle()
                        if time.monotonic() - last_task_health_check >= TASK_HEALTH_CHECK_EVERY:
                            check_task_health()
                            last_task_health_check = time.monotonic()
                        # DB progress remains useful, but is not process-liveness proof.
                        s.heartbeat_at = tz.now()
                        s.save(update_fields=["heartbeat_at"])
                    except DbCircuitOpen:
                        cycle_state = "db_deferred"
                        _publish_main_progress(
                            owner=owner, start_sentinel=start_sentinel,
                            cycle=cycle, state=cycle_state,
                        )
                    except Exception as exc:
                        if record_db_failure(exc, lane="main_loop"):
                            cycle_state = "db_deferred"
                            close_old_connections()
                            _publish_main_progress(
                                owner=owner, start_sentinel=start_sentinel,
                                cycle=cycle, state=cycle_state,
                            )
                            continue
                        cycle_state = "error"
                        bot.log("error", "daemon_loop", repr(exc))
                        _publish_main_progress(
                            owner=owner,
                            start_sentinel=start_sentinel,
                            cycle=cycle,
                            state=cycle_state,
                            error_kind=exc.__class__.__name__,
                        )
                    else:
                        _publish_main_progress(
                            owner=owner,
                            start_sentinel=start_sentinel,
                            cycle=cycle,
                            state=cycle_state,
                        )
                    finally:
                        _publish_process_pulse(
                            owner=owner,
                            start_sentinel=start_sentinel,
                            state="running",
                        )
                    # Low-latency reply loop; Event.wait makes signals prompt.
                    stop_event.wait(1.5 if enabled else 5)
            finally:
                stop_event.set()
                for worker in workers:
                    if worker is not threading.current_thread():
                        worker.join(timeout=1)
                if shutdown.get("signal"):
                    try:
                        bot.log(
                            "info",
                            "daemon_signal",
                            f"signal={shutdown['signal']}",
                        )
                    except Exception:
                        pass
                # Release both independent liveness channels immediately.
                try:
                    if cache.get(DAEMON_LOCK_KEY) == owner:
                        cache.delete(PROCESS_PULSE_KEY)
                        cache.delete(MAIN_PROGRESS_KEY)
                        cache.delete(DAEMON_LOCK_KEY)
                except Exception:
                    pass
