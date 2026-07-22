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

Демон-singleton тримається через кеш-heartbeat: другий демон не стартує, поки
живий перший. Кожна ітерація викликає close_old_connections() — інакше на
shared-MySQL (wait_timeout=60) з'являється "MySQL server has gone away".
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from management.models import InstagramBotSettings
from management.services import bot_followups
from management.services import instagram_bot as bot

HB_KEY = "ig_bot_daemon_hb"            # heartbeat демона (epoch seconds)
HB_ALIVE_WINDOW = 45                   # демон вважається живим, якщо hb свіжіший
SPAWN_LOCK_KEY = "ig_bot_spawn_lock"
DAEMON_LOCK_KEY = "ig_bot_daemon_lock"
CONV_REFRESH_EVERY = 120               # фонове оновлення списку тредів, c

# Cron may invoke manage.py from an arbitrary working directory. Resolve the
# entry point from this command module and keep the child in the Django root.
MANAGE_PY_PATH = str(Path(__file__).resolve().parents[3] / "manage.py")
PROJECT_ROOT = str(Path(MANAGE_PY_PATH).parent)
PID_FILE = os.path.join(PROJECT_ROOT, "tmp", "ig_bot.pid")


def _daemon_alive() -> bool:
    hb = cache.get(HB_KEY)
    return bool(hb and (time.time() - float(hb)) < HB_ALIVE_WINDOW)


def _restart_sentinel_mtime() -> float:
    """mtime файлу tmp/restart.txt — маркер деплою (його torkає кожен git pull).
    Демон стежить за ним і перезавантажується, коли код оновлено."""
    try:
        return os.path.getmtime(os.path.join(PROJECT_ROOT, "tmp", "restart.txt"))
    except OSError:
        return 0.0


def _conv_refresher(stop_event: threading.Event):
    """Фоновий потік: рідко оновлює список тредів (важкий ~25 c виклик),
    тільки коли увімкнено резервний поллінг."""
    while not stop_event.is_set():
        try:
            close_old_connections()
            s = InstagramBotSettings.load()
            if s.is_enabled and s.receive_via_poll:
                token = bot.get_page_token(s)
                if token:
                    bot.refresh_conv_ids(s, token)
        except Exception as exc:
            try:
                bot.log("warning", "conv_refresh", repr(exc))
            except Exception:
                pass
        stop_event.wait(CONV_REFRESH_EVERY)


class Command(BaseCommand):
    help = "Раннер Instagram-бота (демон / watchdog / одиночний прохід)."

    def add_arguments(self, parser):
        parser.add_argument("--forever", action="store_true", help="Постійний демон.")
        parser.add_argument("--ensure", action="store_true", help="Watchdog: підняти демона, якщо мертвий.")
        parser.add_argument("--once", action="store_true", help="Один прохід.")

    def handle(self, *args, **opts):
        if opts["once"]:
            res = bot.poll_once(InstagramBotSettings.load())
            self.stdout.write(f"poll_once: {res}")
            return

        if opts["ensure"]:
            return self._ensure()

        if opts["forever"]:
            return self._forever()

        self.stdout.write("Вкажіть режим: --forever | --ensure | --once")

    # ------------------------------------------------------------------
    def _ensure(self):
        if _daemon_alive():
            self.stdout.write("daemon alive — ok")
            return
        # Захист від подвійного спавну.
        if not cache.add(SPAWN_LOCK_KEY, "1", 30):
            self.stdout.write("spawn in progress — skip")
            return
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
                    start_new_session=True,  # відв'язуємо (setsid) — переживе cron
                    cwd=PROJECT_ROOT,
                    env=os.environ.copy(),
                )
            bot.log("info", "daemon_spawn", "watchdog підняв демона")
            self.stdout.write("daemon spawned")
        except Exception as exc:
            self.stdout.write(f"spawn failed: {exc!r}")

    # ------------------------------------------------------------------
    def _forever(self):
        # Singleton: не стартуємо другий демон поверх живого.
        if _daemon_alive():
            self.stdout.write("daemon already alive — exit")
            return
        owner = f"{os.getpid()}:{time.time_ns()}"
        if not cache.add(DAEMON_LOCK_KEY, owner, HB_ALIVE_WINDOW * 3):
            self.stdout.write("daemon start lock held — exit")
            return
        cache.set(HB_KEY, time.time(), HB_ALIVE_WINDOW * 3)
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
                if _restart_sentinel_mtime() != start_sentinel:
                    bot.log("info", "daemon_reload",
                            "restart.txt змінено — демон перезавантажується для нового коду")
                    break
                enabled = False
                try:
                    s = InstagramBotSettings.load()
                    enabled = s.is_enabled
                    interval = max(2, s.poll_interval_seconds or 3)
                    if enabled:
                        # 1) Воркер черги — дешево (локальна БД), щоразу.
                        bot.process_pending(s)
                        bot_followups.process_due_followups(s)
                        # 2) Резервний інгест із IG — лише якщо увімкнено й настав час.
                        now = time.time()
                        if s.receive_via_poll and (now - last_poll) >= interval:
                            bot.poll_ingest(s)
                            bot.process_pending(s)
                            bot_followups.process_due_followups(s)
                            last_poll = now
                    # heartbeat для UI навіть коли зупинено (агент онлайн)
                    s.heartbeat_at = tz.now()
                    s.save(update_fields=["heartbeat_at"])
                except Exception as exc:
                    bot.log("error", "daemon_loop", repr(exc))
                finally:
                    cache.set(HB_KEY, time.time(), HB_ALIVE_WINDOW * 3)
                    cache.set(DAEMON_LOCK_KEY, owner, HB_ALIVE_WINDOW * 3)
                # працює — кожні ~1.5 c (низька латентність черги); зупинено — рідше
                time.sleep(1.5 if enabled else 5)
        finally:
            stop_event.set()
            # Звільняємо heartbeat одразу, щоб watchdog підняв новий демон без
            # очікування TTL (інакше до 45 c простою після деплою).
            try:
                cache.delete(HB_KEY)
            except Exception:
                pass
            try:
                if cache.get(DAEMON_LOCK_KEY) == owner:
                    cache.delete(DAEMON_LOCK_KEY)
            except Exception:
                pass
