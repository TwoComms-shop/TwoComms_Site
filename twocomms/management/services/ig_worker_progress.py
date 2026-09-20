"""Bounded, process-local observations of independent daemon workers.

Completing an iteration proves that the consumer can run again, not that a
business effect succeeded. Durable queue outcomes remain authoritative.
"""
from contextlib import contextmanager
import math
import threading
import time


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


class WorkerProgressRegistry:
    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.rows = {}

    def reset(self):
        now = self.clock()
        with self.lock:
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


WORKERS = WorkerProgressRegistry()


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
