"""
Раннер Instagram-бота.

Режими:
  --forever   Постійний демон: онлайн весь час, опитує інбокс кожні N секунд.
              Це основний режим — агент «живе» постійно, а не запускається кроном.
  --ensure    Watchdog: якщо демон живий (свіжий heartbeat) — нічого не робить;
              якщо помер (рестарт сервера/деплой/збій) — піднімає демона
              відв'язаним процесом. Саме цей режим чіпляємо в cron раз на хвилину —
              cron НЕ робить запитів до API, лише підстраховує, що демон живий.
  --once      Один прохід опитування (для діагностики).

Демон-singleton тримається через OS advisory lock: другий демон не стартує,
навіть якщо FileBasedCache очищений або недоступний. Кожна ітерація викликає close_old_connections() — інакше на
shared-MySQL (wait_timeout=60) з'являється "MySQL server has gone away".
"""
import os
import fcntl
import subprocess
import sys
import threading
import time
from pathlib import Path
from contextlib import contextmanager

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from management.models import InstagramBotSettings
from management.services import bot_followups
from management.services import instagram_bot as bot
from management.services.ig_maintenance import (
    DEFAULT_MAINTENANCE_SECONDS,
    MAINTENANCE_FILE,
    MaintenanceLeaseConflict,
    activate_maintenance,
    deactivate_maintenance,
    maintenance_status,
)

HB_KEY = "ig_bot_daemon_hb"            # heartbeat демона (epoch seconds)
HB_ALIVE_WINDOW = 45                   # демон вважається живим, якщо hb свіжіший
SPAWN_LOCK_KEY = "ig_bot_spawn_lock"
DAEMON_LOCK_KEY = "ig_bot_daemon_lock"
CONV_REFRESH_EVERY = 120               # фонове оновлення списку тредів, c
RELOAD_LOCK_WAIT_SECONDS = 45

# Cron may invoke manage.py from an arbitrary working directory. Resolve the
# entry point from this command module and keep the child in the Django root.
MANAGE_PY_PATH = str(Path(__file__).resolve().parents[3] / "manage.py")
PROJECT_ROOT = str(Path(MANAGE_PY_PATH).parent)
PID_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot.pid")
SPAWN_LOCK_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot_spawn.lock")
DAEMON_LOCK_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot_daemon.lock")


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


def _wait_for_lock(path: str, *, held: bool, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _process_lock_held(path) is held:
            return True
        time.sleep(0.1)
    return _process_lock_held(path) is held


def _daemon_alive() -> bool:
    hb = cache.get(HB_KEY)
    try:
        heartbeat_at = float(hb.get("at")) if isinstance(hb, dict) else float(hb)
    except (TypeError, ValueError, AttributeError):
        return False
    return bool(heartbeat_at and (time.time() - heartbeat_at) < HB_ALIVE_WINDOW)


def _restart_sentinel_mtime() -> float:
    """mtime файлу tmp/restart.txt — маркер деплою (його torkає кожен git pull).
    Демон стежить за ним і перезавантажується, коли код оновлено."""
    try:
        return os.path.getmtime(os.path.join(PROJECT_ROOT, "tmp", "restart.txt"))
    except OSError:
        return 0.0


def _daemon_code_current() -> bool:
    """A fresh heartbeat from a process started before deploy is not usable."""
    try:
        return os.path.getmtime(PID_FILE) >= _restart_sentinel_mtime()
    except OSError:
        return False


def _conv_refresher(stop_event: threading.Event):
    """Фоновий потік: рідко оновлює список тредів (важкий ~25 c виклик),
    тільки коли увімкнено резервний поллінг."""
    while not stop_event.is_set():
        try:
            close_old_connections()
            s = InstagramBotSettings.load()
            if s.receive_via_poll:
                token = bot.get_page_token(s)
                if token:
                    bot.refresh_conv_ids(s, token)
        except Exception as exc:
            try:
                bot.log("warning", "conv_refresh", repr(exc))
            except Exception:
                pass
        stop_event.wait(CONV_REFRESH_EVERY)


def _run_work_cycle(settings_obj, last_poll: float) -> tuple[bool, float]:
    """Run durable operational work, then reply work only when enabled."""
    enabled = bool(settings_obj.is_enabled)
    interval = max(2, settings_obj.poll_interval_seconds or 3)
    try:
        bot.drain_manager_notifications(limit=10)
    except Exception as exc:
        # Manager-alert availability must never become a global customer-reply
        # kill switch. The next cycle retries and the error remains visible in
        # the operational log/status surface.
        bot.log("error", "notification_outbox", repr(exc))
    if enabled:
        bot.process_pending(settings_obj)
        bot_followups.process_due_followups(settings_obj)
    now = time.time()
    if settings_obj.receive_via_poll and (now - last_poll) >= interval:
        bot.poll_ingest(settings_obj)
        if enabled:
            bot.process_pending(settings_obj)
            bot_followups.process_due_followups(settings_obj)
        last_poll = now
    return enabled, last_poll


class Command(BaseCommand):
    help = "Раннер Instagram-бота (демон / watchdog / одиночний прохід)."

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

    def handle(self, *args, **opts):
        selected = sum(
            bool(opts.get(name))
            for name in ("once", "ensure", "forever")
        )
        selected += int(opts.get("maintenance_on") is not None)
        selected += int(opts.get("maintenance_off") is not None)
        if selected > 1:
            raise CommandError("choose exactly one daemon mode")
        if opts.get("maintenance_on") is not None:
            return self._maintenance_on(opts["maintenance_on"])
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
        if opts["once"]:
            if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                raise CommandError("maintenance active — --once refused")
            res = bot.poll_once(InstagramBotSettings.load())
            self.stdout.write(f"poll_once: {res}")
            return

        if opts["ensure"]:
            return self._ensure()

        if opts["forever"]:
            return self._forever()

        self.stdout.write(
            "Вкажіть режим: --forever | --ensure | --once | --maintenance-on | --maintenance-off"
        )

    def _maintenance_on(self, duration_seconds: int):
        try:
            payload = activate_maintenance(
                path=MAINTENANCE_FILE,
                duration_seconds=duration_seconds,
                actor="run_instagram_bot",
            )
        except MaintenanceLeaseConflict as exc:
            raise CommandError(str(exc)) from exc
        # The daemon observes the lease before its next work cycle. Do not
        # report a safe maintenance boundary until its OS singleton is free.
        if _process_lock_held(DAEMON_LOCK_FILE) and not _wait_for_lock(
            DAEMON_LOCK_FILE,
            held=False,
            timeout=RELOAD_LOCK_WAIT_SECONDS,
        ):
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
            if _process_lock_held(DAEMON_LOCK_FILE):
                if _daemon_code_current():
                    self.stdout.write("daemon alive — ok")
                    return
                # Old code sees restart.txt and exits within at most one idle
                # loop. Never spawn while it still owns the process lock.
                if not _wait_for_lock(
                    DAEMON_LOCK_FILE,
                    held=False,
                    timeout=RELOAD_LOCK_WAIT_SECONDS,
                ):
                    raise CommandError("stale daemon did not release singleton lock during reload")
            log_dir = os.path.join(PROJECT_ROOT, "tmp")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "ig_bot_daemon.log")
            try:
                with open(log_path, "a") as logf:
                    subprocess.Popen(
                        [sys.executable, MANAGE_PY_PATH, "run_instagram_bot", "--forever"],
                        stdout=logf,
                        stderr=logf,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        cwd=PROJECT_ROOT,
                        env=os.environ.copy(),
                    )
                if not _wait_for_lock(DAEMON_LOCK_FILE, held=True, timeout=3.0):
                    raise CommandError("daemon child exited before acquiring singleton lock")
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
        owner = f"{os.getpid()}:{time.time_ns()}"
        cache.set(HB_KEY, {"at": time.time(), "sentinel": _restart_sentinel_mtime()}, HB_ALIVE_WINDOW * 3)
        try:
            os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
            with open(PID_FILE, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
        bot.log("success", "daemon_start", f"Демон онлайн (pid {os.getpid()}).")

        # Sentinel: запам'ятовуємо mtime tmp/restart.txt (його torkає кожен деплой).
        # Якщо файл змінився — демон штатно виходить, watchdog (--ensure) підніме
        # процес із НОВИМ кодом. Без цього --forever крутив би старий код у пам'яті.
        start_sentinel = _restart_sentinel_mtime()

        # Фоновий потік для важкого /conversations (поза гарячим циклом).
        stop_event = threading.Event()
        refresher = threading.Thread(target=_conv_refresher, args=(stop_event,), daemon=True)
        refresher.start()

        from django.utils import timezone as tz

        last_poll = 0.0
        try:
            while True:
                close_old_connections()  # лікує "MySQL server has gone away"
                if maintenance_status(path=MAINTENANCE_FILE)["active"]:
                    bot.log("info", "daemon_maintenance", "Maintenance активний — daemon зупинено")
                    break
                if _restart_sentinel_mtime() != start_sentinel:
                    bot.log("info", "daemon_reload",
                            "restart.txt змінено — демон перезавантажується для нового коду")
                    break
                enabled = False
                try:
                    s = InstagramBotSettings.load()
                    enabled, last_poll = _run_work_cycle(s, last_poll)
                    # heartbeat для UI навіть коли зупинено (агент онлайн)
                    s.heartbeat_at = tz.now()
                    s.save(update_fields=["heartbeat_at"])
                except Exception as exc:
                    bot.log("error", "daemon_loop", repr(exc))
                finally:
                    cache.set(HB_KEY, {"at": time.time(), "sentinel": start_sentinel}, HB_ALIVE_WINDOW * 3)
                    cache.set(DAEMON_LOCK_KEY, owner, HB_ALIVE_WINDOW * 3)
                # працює — кожні ~1.5 c (низька латентність черги); зупинено — рідше
                time.sleep(1.5 if enabled else 5)
        finally:
            stop_event.set()
            # Звільняємо heartbeat одразу, щоб watchdog підняв новий демон без
            # очікування TTL (інакше до 45 c простою після деплою).
            try:
                if cache.get(DAEMON_LOCK_KEY) == owner:
                    cache.delete(HB_KEY)
                    cache.delete(DAEMON_LOCK_KEY)
            except Exception:
                pass
