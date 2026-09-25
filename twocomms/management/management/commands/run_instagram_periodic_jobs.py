"""Run all cron-owned Instagram lanes in one bounded Django process."""

from __future__ import annotations

import signal
import threading
import time
from contextlib import contextmanager

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from management.models import InstagramBotTaskHeartbeat
from management.services.ig_runtime_ownership import PERIODIC_LANES
from management.services.ig_task_health import mark_task_failed


DEFAULT_BUDGET_SECONDS = 540
MAX_BUDGET_SECONDS = 570


class PeriodicLaneTimeout(BaseException):
    """Non-swallowable deadline used only around one in-process cron lane."""


@contextmanager
def periodic_lane_deadline(seconds: float):
    """Interrupt a hung lane without starting another heavyweight Django.

    ``BaseException`` is intentional: the legacy services contain broad
    ``except Exception`` recovery blocks which must not swallow this owner
    deadline and keep the account-wide lock for the full outer 600 seconds.
    Django transaction contexts still unwind for ``BaseException``.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("periodic lane deadline requires the main thread")
    bounded = max(0.001, float(seconds))
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def timed_out(_signum, _frame):
        raise PeriodicLaneTimeout(f"periodic lane exceeded {bounded:.3f}s")

    signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, bounded)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0 or previous_timer[1] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _call_auto_analysis_enabled() -> bool:
    try:
        from management.services.call_auto_analysis import is_call_auto_analysis_enabled

        return bool(is_call_auto_analysis_enabled())
    except Exception:
        # This optional lane fails closed without starving payment/fulfillment.
        return False


def _lane_enabled(lane) -> bool:
    if lane.optional_gate == "call_auto_analysis":
        return _call_auto_analysis_enabled()
    return True


def due_periodic_lanes(*, now=None, selected_lane: str = "", force: bool = False):
    """Return due lanes from one heartbeat query.

    The child command writes ``last_started_at`` at its real work boundary.
    Using that durable timestamp preserves each lane's former cadence while a
    single every-minute coordinator replaces five independent cron processes.
    """
    now = now or timezone.now()
    lanes = tuple(
        lane
        for lane in PERIODIC_LANES
        if (not selected_lane or lane.task_key == selected_lane) and _lane_enabled(lane)
    )
    if force:
        return lanes
    rows = {
        row.task_key: row.last_started_at
        for row in InstagramBotTaskHeartbeat.objects.filter(
            task_key__in=[lane.task_key for lane in lanes]
        ).only("task_key", "last_started_at")
    }
    due = [
        lane
        for lane in lanes
        if rows.get(lane.task_key) is None
        or (now - rows[lane.task_key]).total_seconds() >= lane.interval_seconds
    ]
    manifest_order = {lane.task_key: index for index, lane in enumerate(PERIODIC_LANES)}
    due.sort(
        key=lambda lane: (
            # Preserve the notification backstop and formerly starved tracking
            # lane ahead of repair work; all other lanes remain oldest-first.
            0 if lane.task_key == "manager_notification_backstop" else
            1 if lane.task_key == "nova_poshta_tracking" else 2,
            rows[lane.task_key].timestamp()
            if rows.get(lane.task_key) is not None
            else float("-inf"),
            manifest_order[lane.task_key],
        )
    )
    return tuple(due)


class Command(BaseCommand):
    help = "Run due Instagram periodic lanes sequentially under one cron owner."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lane",
            choices=[lane.task_key for lane in PERIODIC_LANES],
            default="",
            help="Run or inspect one declared lane.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignore cadence for an explicitly requested operational run.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print due lane keys without running their commands.",
        )
        parser.add_argument(
            "--budget-seconds",
            type=int,
            default=DEFAULT_BUDGET_SECONDS,
            help="Do not start another lane after this coordinator budget.",
        )

    def handle(self, *args, **options):
        selected_lane = str(options.get("lane") or "")
        force = bool(options.get("force"))
        if force and not selected_lane:
            raise CommandError("--force requires --lane")
        budget = int(options.get("budget_seconds") or DEFAULT_BUDGET_SECONDS)
        if not 1 <= budget <= MAX_BUDGET_SECONDS:
            raise CommandError(
                f"--budget-seconds must be between 1 and {MAX_BUDGET_SECONDS}"
            )
        from management.services.ig_db_circuit import (
            DbCircuitOpen, require_database_ready, record_db_failure,
        )
        try:
            require_database_ready(lane="periodic_dispatch")
        except DbCircuitOpen:
            self.stdout.write("completed=- deferred=db_circuit failed=- timed_out=-")
            return
        try:
            from management.services.ig_daemon_health import (
                alert_daemon_runtime_health,
            )

            with periodic_lane_deadline(min(15, budget)):
                alert_daemon_runtime_health()
        except (PeriodicLaneTimeout, Exception) as exc:
            # Runtime-health delivery is important but must never starve the
            # payment/fulfillment repair lanes it is meant to supervise.
            close_old_connections()
            if record_db_failure(exc, lane="periodic_health"):
                self.stdout.write("completed=- deferred=db_circuit failed=- timed_out=-")
                return
        try:
            due = due_periodic_lanes(selected_lane=selected_lane, force=force)
        except Exception as exc:
            if not record_db_failure(exc, lane="periodic_claim"):
                raise
            close_old_connections()
            self.stdout.write("completed=- deferred=db_circuit failed=- timed_out=-")
            return
        if options.get("dry_run"):
            self.stdout.write("due=" + ",".join(lane.task_key for lane in due))
            return

        started = time.monotonic()
        completed = []
        deferred = []
        failures = []
        timed_out = []
        for position, lane in enumerate(due):
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                deferred.append(lane.task_key)
                continue
            lane_deadline = min(float(lane.deadline_seconds), remaining)
            try:
                with periodic_lane_deadline(lane_deadline):
                    call_command(lane.command, **lane.command_options())
            except PeriodicLaneTimeout as exc:
                # BaseException bypasses legacy broad catches, so the owner
                # records the typed failure explicitly before rotating lanes.
                mark_task_failed(lane.task_key, exc)
                failures.append((lane.task_key, exc.__class__.__name__))
                timed_out.append(lane.task_key)
            except Exception as exc:
                # The child command owns its typed heartbeat/failure record.
                # Continue so one broken lane cannot starve payment or delivery.
                failures.append((lane.task_key, exc.__class__.__name__))
                if record_db_failure(exc, lane=lane.task_key):
                    deferred.extend(item.task_key for item in due[position + 1:])
                    break
            else:
                completed.append(lane.task_key)
            finally:
                close_old_connections()

        summary = (
            f"completed={','.join(completed) or '-'} "
            f"deferred={','.join(deferred) or '-'} "
            f"failed={','.join(key for key, _kind in failures) or '-'} "
            f"timed_out={','.join(timed_out) or '-'}"
        )
        self.stdout.write(summary)
        if failures:
            safe_failure_kinds = ",".join(
                f"{key}:{kind}" for key, kind in failures
            )
            raise CommandError(f"periodic lane failures: {safe_failure_kinds}")
