"""Read a bounded, sanitized projection of the Instagram supervisor state.

The supervisor is a stdlib process and publishes ``tmp/ig_bot_supervisor_state.json``.
This reader deliberately does not report process liveness: a PID in a file is
only metadata until a caller independently verifies the process identity.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any


STATE_FILENAME = "ig_bot_supervisor_state.json"
MAX_STATE_BYTES = 32 * 1024
DEFAULT_STALE_AFTER_SECONDS = 180.0
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_MAX_PID = 2**31 - 1


def _empty_observation() -> dict[str, object]:
    return {
        "available": False,
        "status": "unobserved",
        "observed_at": None,
        "supervisor_release_sha": None,
        "child_release_sha": None,
        "restart_count": None,
        "child_pid": None,
        "child_pid_matches_expected": None,
    }


def _safe_sha(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _SHA_RE.fullmatch(value) else None


def _safe_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum <= value <= _MAX_PID:
        return value
    return None


def _safe_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_STATE_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_supervisor_observation(
    runtime_root: str | Path,
    *,
    expected_child_pid: int | None = None,
    now: float | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, object]:
    """Return sanitized supervisor metadata without asserting process liveness.

    ``expected_child_pid`` is an optional PID obtained by a separate observer.
    A matching number is reported as correspondence only; it does not mean the
    process is alive or that the PID has not been recycled.  ``status`` is one
    of ``current``, ``stale``, ``mismatch``, or ``unobserved``.
    """

    result = _empty_observation()
    try:
        root = Path(runtime_root)
    except (TypeError, ValueError):
        return result
    payload = _read_payload(root / "tmp" / STATE_FILENAME)
    if payload is None:
        return result

    observed_at = _safe_timestamp(payload.get("observed_at"))
    if observed_at is None:
        return result

    checked_at = time.time() if now is None else _safe_timestamp(now)
    if checked_at is None:
        checked_at = time.time()
    threshold = _safe_timestamp(stale_after_seconds)
    if threshold is None:
        threshold = DEFAULT_STALE_AFTER_SECONDS

    child_pid = _safe_int(payload.get("child_pid"), minimum=1)
    expected_pid = _safe_int(expected_child_pid, minimum=1)
    pid_matches: bool | None = None
    if expected_child_pid is not None:
        pid_matches = child_pid is not None and expected_pid is not None and child_pid == expected_pid

    result.update(
        {
            "available": True,
            "status": "stale" if max(0.0, checked_at - observed_at) > threshold else "current",
            "observed_at": observed_at,
            "supervisor_release_sha": _safe_sha(payload.get("supervisor_release_sha")),
            "child_release_sha": _safe_sha(payload.get("child_release_sha")),
            "restart_count": _safe_int(payload.get("restart_count"), minimum=0),
            "child_pid": child_pid,
            "child_pid_matches_expected": pid_matches,
        }
    )
    if pid_matches is False:
        result["status"] = "mismatch"
    return result


__all__ = ["read_supervisor_observation"]
