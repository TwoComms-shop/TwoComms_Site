"""Bounded, process-independent maintenance lease for the Instagram daemon."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAINTENANCE_FILE = str(PROJECT_ROOT / "tmp" / "ig_bot_maintenance.json")
MAINTENANCE_LOCK_FILE = str(PROJECT_ROOT / "tmp" / "ig_bot_maintenance.lock")
NOTIFICATION_SEND_LOCK_FILE = str(PROJECT_ROOT / "tmp" / "ig_bot_notification_send.lock")
DEFAULT_MAINTENANCE_SECONDS = 15 * 60
MAX_MAINTENANCE_SECONDS = 60 * 60
MAX_CLOCK_SKEW_SECONDS = 300


class MaintenanceLeaseConflict(RuntimeError):
    """Raised when another active owner controls the maintenance lease."""


@contextmanager
def _exclusive_file_lock(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _bounded_duration(value: int | float, *, max_seconds: int) -> int:
    return max(30, min(int(value), int(max_seconds)))


def maintenance_status(
    *,
    path: str = MAINTENANCE_FILE,
    now: float | None = None,
    max_seconds: int = MAX_MAINTENANCE_SECONDS,
) -> dict:
    """Return lease truth; malformed files fail safe for a bounded mtime window."""
    checked_at = float(time.time() if now is None else now)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return {"active": False, "state": "absent", "expires_at": None, "actor": ""}
    except OSError as exc:
        return {
            "active": True,
            "state": "unreadable_active",
            "expires_at": checked_at + int(max_seconds),
            "actor": "",
            "error": type(exc).__name__,
        }

    try:
        with open(path, encoding="utf-8") as lease_file:
            payload = json.load(lease_file)
        expires_at = float(payload["expires_at"])
        started_at = float(payload["started_at"])
        lease_id = str(payload["lease_id"])
        values_are_finite = math.isfinite(started_at) and math.isfinite(expires_at)
        bounds_are_valid = 0 < expires_at - started_at <= int(max_seconds)
        clock_is_valid = started_at <= checked_at + MAX_CLOCK_SKEW_SECONDS
        if not values_are_finite or not bounds_are_valid or not clock_is_valid or not lease_id:
            raise ValueError("invalid lease bounds")
        active = checked_at < expires_at
        return {
            "active": active,
            "state": "active" if active else "stale",
            "lease_id": lease_id,
            "started_at": started_at,
            "expires_at": expires_at,
            "remaining_seconds": max(0, round(expires_at - checked_at, 1)),
            "actor": str(payload.get("actor") or "")[:80],
            "pid": payload.get("pid"),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        # ctime changes when an attacker/corrupt tool future-dates mtime, so
        # the earlier filesystem timestamp keeps malformed markers bounded.
        filesystem_started_at = min(float(stat.st_mtime), float(stat.st_ctime))
        expires_at = filesystem_started_at + int(max_seconds)
        active = checked_at < expires_at
        return {
            "active": active,
            "state": "malformed_active" if active else "malformed_stale",
            "expires_at": expires_at,
            "remaining_seconds": max(0, round(expires_at - checked_at, 1)),
            "actor": "",
            "error": type(exc).__name__,
        }


def _write_payload(path: str, payload: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".ig-maintenance-", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=True, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def activate_maintenance(
    *,
    path: str = MAINTENANCE_FILE,
    lock_path: str = MAINTENANCE_LOCK_FILE,
    send_lock_path: str = NOTIFICATION_SEND_LOCK_FILE,
    duration_seconds: int = DEFAULT_MAINTENANCE_SECONDS,
    actor: str = "management-command",
    now: float | None = None,
    max_seconds: int = MAX_MAINTENANCE_SECONDS,
) -> dict:
    """Atomically publish a bounded, single-owner maintenance lease."""
    started_at = float(time.time() if now is None else now)
    duration = _bounded_duration(duration_seconds, max_seconds=max_seconds)
    with _exclusive_file_lock(lock_path):
        with _exclusive_file_lock(send_lock_path):
            existing = maintenance_status(path=path, now=started_at, max_seconds=max_seconds)
            if existing["active"]:
                raise MaintenanceLeaseConflict(
                    f"maintenance already active: {existing.get('lease_id') or existing['state']}"
                )
            payload = {
                "version": 1,
                "lease_id": uuid.uuid4().hex,
                "started_at": started_at,
                "expires_at": started_at + duration,
                "actor": str(actor or "management-command")[:80],
                "pid": os.getpid(),
            }
            _write_payload(path, payload)
            return payload


def deactivate_maintenance(
    *,
    lease_id: str,
    path: str = MAINTENANCE_FILE,
    lock_path: str = MAINTENANCE_LOCK_FILE,
) -> bool:
    """Release only the exact lease owned by the supplied token."""
    with _exclusive_file_lock(lock_path):
        current = maintenance_status(path=path)
        current_id = str(current.get("lease_id") or "")
        if not current_id or current_id != str(lease_id or ""):
            raise MaintenanceLeaseConflict("maintenance lease owner mismatch")
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False


@contextmanager
def notification_send_boundary(
    *,
    lease_path: str = MAINTENANCE_FILE,
    send_lock_path: str = NOTIFICATION_SEND_LOCK_FILE,
):
    """Serialize the last maintenance check with Telegram provider I/O."""
    with _exclusive_file_lock(send_lock_path):
        yield not maintenance_status(path=lease_path)["active"]
