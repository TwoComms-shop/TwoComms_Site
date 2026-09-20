"""Bounded, process-local observations of independent daemon workers.

Completing an iteration proves that the consumer can run again, not that a
business effect succeeded. Durable queue outcomes remain authoritative.
"""
from contextlib import contextmanager
import math
import threading
import time

from django.core.cache import cache


# Conservative observation limits, not permission to extend a durable job lease.
# No automatic restart is driven by these limits.
WORKER_LIMITS = {
    "conversation_refresh": 300.0,
    "journey_trace_refresh": 300.0,
    "analysis": 600.0,
    "reply_recovery": 600.0,
    "permission_transition": 300.0,
    "inbox_refresh": 600.0,
    "checkout_lifecycle": 300.0,
    "follow_intelligence": 600.0,
}
PROCESS_PULSE_KEY = "ig_bot_daemon_hb"
DAEMON_OWNER = "instagram_daemon"


class WorkerProgressRegistry:
    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.rows = {}
        self.initialized = False

    def reset(self):
        now = self.clock()
        with self.lock:
            self.initialized = True
            self.rows = {
                name: {"state": "starting", "progress_at": now,
                       "started_at": None, "completed_at": None,
                       "deadline_at": now + limit, "error_kind": ""}
                for name, limit in WORKER_LIMITS.items()
            }

    def begin(self, name):
        limit = WORKER_LIMITS[name]
        now = self.clock()
        with self.lock:
            previous = self.rows.get(name, {})
            self.rows[name] = {
                "state": "running", "progress_at": now, "started_at": now,
                "completed_at": previous.get("completed_at"),
                "deadline_at": now + limit, "error_kind": "",
            }

    def finish(self, name, error_kind=""):
        now = self.clock()
        with self.lock:
            row = self.rows[name]
            row.update(state="failed" if error_kind else "idle", progress_at=now,
                       completed_at=now, deadline_at=now + WORKER_LIMITS[name],
                       error_kind=error_kind)

    def snapshot(self):
        with self.lock:
            return {name: dict(row) for name, row in self.rows.items()}

    def admission_allowed(self, name):
        """Freeze only new claims after an observed lane failure/stall.

        Existing leases remain owned by their durable token and are reclaimed by
        their normal expiry path; this gate never edits queue rows.
        """
        row = self.snapshot().get(name)
        # Unit callers and one-shot diagnostics do not own daemon worker state;
        # they retain the pre-existing behavior until a daemon publishes a
        # generation. Once initialized, missing/stale state fails closed.
        if not self.initialized:
            return True
        if not row:
            return False
        now = self.clock()
        return row.get("state") in {"starting", "running", "idle"} and now < float(row.get("deadline_at") or 0)


WORKERS = WorkerProgressRegistry()


def _shared_worker_admission(name):
    """Read the daemon's cross-process observation without granting ownership."""
    try:
        payload = cache.get(PROCESS_PULSE_KEY)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("owner") != DAEMON_OWNER:
        return None
    try:
        observed_at = float(payload.get("at"))
        if time.time() - observed_at >= 150:
            return False
    except (TypeError, ValueError):
        return False
    row = (payload.get("worker_lanes") or {}).get(name)
    if not isinstance(row, dict):
        return False
    try:
        progress_at = float(row.get("progress_at"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(progress_at):
        return False
    state = row.get("state")
    return state in {"starting", "running", "idle"} and time.time() < progress_at + WORKER_LIMITS[name]


def worker_claim_admission(name):
    local = WORKERS.admission_allowed(name)
    if WORKERS.initialized:
        if not local:
            return False
        if name == "analysis":
            try:
                from management.services.ig_analysis_lane import current_owner, owner_snapshot
                snapshot = owner_snapshot()
                if snapshot.get("claim_frozen"):
                    return False
                active = snapshot.get("lease_until")
                bound = current_owner() or {}
                if active and snapshot.get("owner_token") and (
                    bound.get("owner_token") != snapshot.get("owner_token")
                    or int(bound.get("generation") or 0) != int(snapshot.get("generation") or 0)
                ):
                    return False
            except Exception:
                pass
        return local
    if name == "analysis":
        try:
            from management.services.ig_analysis_lane import claim_admission
            durable = claim_admission
        except Exception:
            durable = None
        # Calls without an explicit owner are one-shot diagnostics; the daemon
        # worker passes ownership through its durable context before claiming.
        # A durable frozen lane always fails closed even for such callers.
        if durable is not None:
            try:
                from management.services.ig_analysis_lane import recovery_admission
                if recovery_admission():
                    return False
            except Exception:
                pass
    shared = _shared_worker_admission(name)
    # Direct one-shot callers have no daemon pulse and retain legacy behavior;
    # an observed daemon must, however, fail closed on missing/stale lane data.
    return local if shared is None else bool(shared)


@contextmanager
def worker_iteration(name):
    WORKERS.begin(name)
    try:
        yield
    except BaseException as exc:
        WORKERS.finish(name, type(exc).__name__[:80])
        raise
    else:
        WORKERS.finish(name)


def _moment(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def worker_health_snapshot(payload, *, now):
    """Read only; a fresh publisher cannot advance a worker's own progress."""
    payload = payload if isinstance(payload, dict) else {}
    lanes = {}
    for name, limit in WORKER_LIMITS.items():
        row = payload.get(name)
        row = row if isinstance(row, dict) else {}
        progress = _moment(row.get("progress_at"))
        started = _moment(row.get("started_at"))
        completed = _moment(row.get("completed_at"))
        # Derive from the fixed limit; malformed cache cannot grant infinite life.
        deadline = progress + limit if progress is not None else None
        state = row.get("state")
        if progress is None or progress > now + 30 or not isinstance(state, str) or state not in {"starting", "running", "idle", "failed"}:
            state = "unobserved"
        elif state != "failed" and now >= deadline:
            state = "stalled"
        lanes[name] = {
            "state": state,
            "healthy": state in {"starting", "running", "idle"},
            "progress_age_seconds": max(0.0, now - progress) if progress is not None else None,
            "iteration_age_seconds": max(0.0, now - started) if row.get("state") == "running" and started is not None else None,
            "completed_age_seconds": max(0.0, now - completed) if completed is not None else None,
            "observation_limit_seconds": limit,
            "deadline_at": deadline,
            "progress_evidence": "worker_iteration" if progress is not None else "unobserved",
        }
    return {"healthy": all(row["healthy"] for row in lanes.values()), "lanes": lanes}
