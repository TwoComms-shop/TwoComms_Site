"""Two daemon-owned customer slots without an executor backlog.

Selection and all durable source/client claims remain inside the native
consumer. The main daemon never waits for provider work during a normal tick.
Closing admission is separate from draining: a timeout cannot cancel HTTP or
prove that a physical send has finished.
"""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import nullcontext
import threading

from django.db import close_old_connections, connections


MAX_CUSTOMER_WORKERS = 2


class CustomerExecutionFailed(RuntimeError):
    pass


def customer_job_budget_seconds():
    from management.services.ig_revision_outbox import MAX_EFFECTS
    from management.services.ig_turn_budget import HEARTBEAT_SAFETY_MARGIN_SECONDS
    from management.services.ig_turn_revisions import OVERALL_DEADLINE_SECONDS
    from management.services.instagram_bot import HTTP_TIMEOUT

    return OVERALL_DEADLINE_SECONDS + MAX_EFFECTS * HTTP_TIMEOUT + 5 + HEARTBEAT_SAFETY_MARGIN_SECONDS


def runnable_customer_work():
    """DB-only queue admission; no graph, token discovery or provider probe."""
    from django.db.models import Q
    from django.utils import timezone
    from management.models import IgCustomerTurnRevision
    from management.services.ig_revision_execution import (
        available_client_automation_q, due_revision_ids, expired_revision_debt_ids, finalization_due_ids,
    )
    from management.services.ig_revision_holding import parked_collaboration_revision_ids
    from management.services.ig_revision_recovery import due_preparation_recovery_ids, due_recovery_revision_ids
    from management.services.ig_revision_rollout import revision_execution_rollout

    rollout = revision_execution_rollout()
    if not rollout.enabled:
        return False
    now = timezone.now()
    if IgCustomerTurnRevision.objects.filter(state="claimed", lease_until__lte=now).filter(
        Q(overall_deadline__gt=now) | Q(recovery_state="execution"),
    ).filter(available_client_automation_q(now=now)).filter(
        Q(delivery_effects__isnull=False) | Q(active_slot=1),
    ).exclude(claim_token__startswith="finalize:").exclude(claim_token__startswith="debt:").exists():
        return True
    return bool(finalization_due_ids(now=now, limit=1)
        or due_revision_ids(now=now, limit=1, cutover_at=rollout.cutover_at)
        or due_preparation_recovery_ids(now=now, limit=1, available_only=True)
        or due_recovery_revision_ids(now=now, limit=1, available_only=True)
        or expired_revision_debt_ids(now=now, limit=1, cutover_at=rollout.cutover_at)
        or parked_collaboration_revision_ids(limit=1))


def run_one_customer_job():
    """Own the thread's DB connection/settings; native consumer owns leases."""
    from management.models import InstagramBotSettings
    from management.services import instagram_bot as bot
    from management.services.ig_db_circuit import require_database_ready
    from management.services.ig_revision_live import revision_execution_enabled

    close_old_connections()
    try:
        require_database_ready(lane="customer_execution")
        settings_row = InstagramBotSettings.load()
        if (not settings_row.is_enabled or not revision_execution_enabled()
            or bot.maintenance_status()["active"]):
            return 0
        return bot.process_pending(settings_row, max_items=1)
    finally:
        # Django connections are thread-local. Never retain an idle connection
        # in an executor worker after its job or close another thread's owner.
        connections.close_all()


class CustomerExecutionPool:
    def __init__(self, stop_event, *, run_one=None, has_work=None, maintenance=None,
                 operation=None, executor=None, capacity=None, max_workers=MAX_CUSTOMER_WORKERS):
        if max_workers != MAX_CUSTOMER_WORKERS:
            raise ValueError("customer execution has exactly two bounded slots")
        self.stop_event = stop_event
        self.run_one = run_one or run_one_customer_job
        self.has_work = has_work or runnable_customer_work
        if maintenance is None:
            from management.services.ig_maintenance import maintenance_status
            maintenance = maintenance_status
        self.maintenance = maintenance
        self.operation = operation or (lambda _slot: nullcontext())
        if capacity is None:
            from management.services.ig_db_circuit import active_slot_cap
            capacity = lambda: active_slot_cap(configured_cap=MAX_CUSTOMER_WORKERS)
        self.capacity = capacity
        self.executor = executor or ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ig-customer")
        self.lock = threading.RLock()
        self.futures = {}
        self.closing = False
        self.closed = False
        self.completed = 0
        self.last_error = ""
        from management.services.ig_worker_progress import WORKERS
        WORKERS.activate_optional(f"customer_execution_{slot}" for slot in range(MAX_CUSTOMER_WORKERS))

    def _run(self, slot):
        from management.services.ig_worker_progress import worker_iteration

        with worker_iteration(f"customer_execution_{slot}"), self.operation(slot):
            if self.stop_event.is_set() or self.closing or self.maintenance()["active"]:
                return 0
            return self.run_one()

    def _reap(self, *, raise_errors=True):
        from management.services.ig_db_circuit import DbCircuitOpen, record_db_failure

        failures = []
        with self.lock:
            for slot, future in list(self.futures.items()):
                if not future.done():
                    continue
                del self.futures[slot]
                try:
                    future.result()  # done() above: never waits for HTTP.
                except BaseException as exc:
                    self.last_error = type(exc).__name__[:80]
                    try:
                        deferred = isinstance(exc, DbCircuitOpen) or (
                            isinstance(exc, Exception) and record_db_failure(exc, lane="customer_execution"))
                    except Exception:
                        # The future is conclusively done; failure reporting
                        # cannot abandon other real in-flight customer jobs.
                        deferred = isinstance(exc, DbCircuitOpen)
                    if deferred:
                        failures.append(DbCircuitOpen("customer execution database deferred"))
                    else:
                        self.closing = True
                        self.stop_event.set()
                        failures.append(CustomerExecutionFailed("customer execution failed: " + self.last_error))
                else:
                    self.completed += 1
        if failures and raise_errors:
            # A fatal result must not be hidden by an unrelated DB deferral.
            raise next((row for row in failures if isinstance(row, CustomerExecutionFailed)), failures[0])

    def tick(self, *, admit=True):
        from management.services.ig_worker_progress import worker_iteration

        self._reap()
        with self.lock:
            if self.closed or self.closing or self.stop_event.is_set():
                return self.snapshot()
            allowed = bool(admit and not self.maintenance()["active"])
            capacity = None
            for slot in range(MAX_CUSTOMER_WORKERS):
                if slot in self.futures:
                    continue
                if not allowed or not self.has_work():
                    # A real empty queue poll proves this free consumer slot
                    # can run; never pulse a slot whose actual future is busy.
                    with worker_iteration(f"customer_execution_{slot}"):
                        pass
                    continue
                if capacity is None:
                    capacity = max(0, min(MAX_CUSTOMER_WORKERS, int(self.capacity())))
                if len(self.futures) >= capacity:
                    with worker_iteration(f"customer_execution_{slot}"):
                        pass
                    continue
                try:
                    self.futures[slot] = self.executor.submit(self._run, slot)
                except Exception as exc:
                    self.last_error = type(exc).__name__[:80]
                    self.closing = True
                    self.stop_event.set()
                    raise CustomerExecutionFailed("customer execution submission failed") from exc
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {"slots": MAX_CUSTOMER_WORKERS, "in_flight": len(self.futures),
                "closing": self.closing, "closed": self.closed,
                "completed": self.completed, "last_error": self.last_error}

    def close_admission(self):
        with self.lock:
            self.closing = True

    def drain(self, *, timeout=1.0):
        """Return actual completion; timeout leaves every live future owned."""
        self.close_admission()
        self._reap(raise_errors=False)
        with self.lock:
            pending = tuple(self.futures.values())
        if pending:
            wait(pending, timeout=max(0.0, float(timeout)))
            self._reap(raise_errors=False)
        with self.lock:
            if self.futures:
                return False
            if not self.closed:
                self.executor.shutdown(wait=True)  # All futures are done.
                self.closed = True
                from management.services.ig_worker_progress import WORKERS
                WORKERS.deactivate_optional(f"customer_execution_{slot}" for slot in range(MAX_CUSTOMER_WORKERS))
            return True
