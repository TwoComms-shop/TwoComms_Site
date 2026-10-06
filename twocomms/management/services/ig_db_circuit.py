"""Shared DB-disconnect circuit and cross-process background slot admission."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import secrets
import time
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import connections

from twocomms.db_resilience import is_mysql_disconnect_error


BACKOFF_SECONDS = (1, 2, 5, 15, 30)
_STATE_PREFIX = "ig_db_circuit:v1:"
_CAPACITY_PREFIX = "ig_db_capacity:v1:"
CAPACITY_TTL_SECONDS = 600
_retained_session_connections = ContextVar("ig_retained_session_connections", default=frozenset())


class DbCircuitOpen(RuntimeError):
    pass


class DbActiveCapacityError(RuntimeError):
    pass


class DbCapacityUnavailable(DbCircuitOpen):
    pass


def _lock_root() -> Path:
    configured = str(getattr(settings, "IG_DB_CIRCUIT_LOCK_DIR", "") or "").strip()
    return Path(configured) if configured else Path(settings.BASE_DIR) / "tmp" / "ig_db_circuit"


def _key(scope: str) -> str:
    return _STATE_PREFIX + _capacity_scope(scope)


def _capacity_scope(scope: str) -> str:
    settings_dict = connections[scope].settings_dict
    # MariaDB's user connection cap spans databases on the same account.
    material = "\x1f".join(str(settings_dict.get(key) or "") for key in ("HOST", "PORT", "USER"))
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def capacity_snapshot(scope: str = "default") -> dict:
    """TTL-bound read-only capacity, retaining no grants or credentials."""
    key = _CAPACITY_PREFIX + _capacity_scope(scope)
    cached = cache.get(key)
    if isinstance(cached, dict):
        return cached
    try:
        connection = connections[scope]
        if connection.in_atomic_block or connection.vendor != "mysql":
            return {"known": False, "user_cap": None, "own_connections": None}
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@max_user_connections, @@max_connections")
            user_cap, server_cap = cursor.fetchone()
            cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
            grants = [str(row[0] or "") for row in cursor.fetchall()]
            grant_caps = [int(value) for value in re.findall(r"MAX_USER_CONNECTIONS\s+(\d+)", " ".join(grants), re.I)]
            if grant_caps:
                user_cap = min([int(user_cap or 0) or grant_caps[0], *grant_caps])
            cursor.execute("SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER = SUBSTRING_INDEX(CURRENT_USER(), '@', 1)")
            own_connections = cursor.fetchone()[0]
        snapshot = {"known": bool(int(user_cap or 0)), "user_cap": int(user_cap or 0), "server_cap": int(server_cap or 0), "own_connections": int(own_connections or 0)}
        cache.set(key, snapshot, timeout=CAPACITY_TTL_SECONDS)
        return snapshot
    except Exception as exc:
        disconnected = record_db_failure(exc, scope=scope, lane="capacity_probe")
        cache.set(key, {"known": False, "user_cap": None, "own_connections": None}, timeout=60)
        if disconnected:
            raise DbCapacityUnavailable("database capacity probe unavailable") from exc
        return {"known": False, "user_cap": None, "own_connections": None}


def require_database_ready(scope: str = "default", *, lane: str = "") -> dict:
    """Admit reads; a cached capacity value can never prove recovery."""
    token = admit_read_probe(scope)
    if token:
        try:
            with connections[scope].cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            record_db_failure(exc, scope=scope, lane=lane or "read_probe")
            release_idle_connection(using=scope)
            raise DbCircuitOpen("database recovery probe failed") from exc
        record_read_success(scope, probe_token=token)
    # Another lane may have failed while this probe/read was finishing.
    status = circuit_status(scope)
    if status["open"] or status["failures"]:
        raise DbCircuitOpen("database circuit changed during read admission")
    try:
        return capacity_snapshot(scope)
    except DbCapacityUnavailable as exc:
        release_idle_connection(using=scope)
        raise DbCircuitOpen("database capacity read deferred") from exc


def _lock_path(name: str) -> Path:
    safe = "".join(char if char.isalnum() or char in "_-" else "_" for char in name)
    return _lock_root() / f"{safe}.lock"


@contextmanager
def _exclusive(name: str):
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state(scope: str) -> dict:
    value = cache.get(_key(scope), {})
    return value if isinstance(value, dict) else {}


def circuit_status(scope: str = "default", *, now: float | None = None) -> dict:
    now = time.monotonic() if now is None else float(now)
    state = _state(scope)
    retry_at = float(state.get("retry_at") or 0)
    return {
        "open": retry_at > now,
        "retry_at": retry_at,
        "failures": int(state.get("failures") or 0),
        "last_lane": str(state.get("last_lane") or "")[:64],
    }


def admit_read_probe(scope: str = "default", *, now: float | None = None) -> str | None:
    """Allow a normal read or elect exactly one half-open read-only probe."""
    now = time.monotonic() if now is None else float(now)
    with _exclusive(f"circuit-{_capacity_scope(scope)}"):
        state = _state(scope)
        retry_at = float(state.get("retry_at") or 0)
        if retry_at > now:
            raise DbCircuitOpen("database circuit is cooling down")
        if state.get("probe_token") and float(state.get("probe_until") or 0) > now:
            raise DbCircuitOpen("database half-open probe already in flight")
        if retry_at:
            token = f"{int(state.get('generation') or 0)}:{secrets.token_hex(8)}"
            state["probe_token"] = token
            state["probe_until"] = now + 30
            cache.set(_key(scope), state, timeout=60)
            return token
        return None


def record_read_success(scope: str = "default", *, probe_token: str | None = None) -> None:
    if not probe_token:
        return
    with _exclusive(f"circuit-{_capacity_scope(scope)}"):
        state = _state(scope)
        if state.get("probe_token") == probe_token:
            cache.delete(_key(scope))


def record_db_failure(exc, *, scope: str = "default", lane: str = "", now: float | None = None) -> bool:
    """Record only MySQL disconnects; callers never retry a write from here."""
    if not is_mysql_disconnect_error(exc):
        return False
    now = time.monotonic() if now is None else float(now)
    with _exclusive(f"circuit-{_capacity_scope(scope)}"):
        previous = _state(scope)
        failures = min(int(previous.get("failures") or 0) + 1, len(BACKOFF_SECONDS))
        base = BACKOFF_SECONDS[failures - 1]
        delay = base * (0.8 + random.random() * 0.4)
        cache.set(_key(scope), {
            "failures": failures,
            "retry_at": now + delay,
            "generation": int(previous.get("generation") or 0) + 1,
            "last_lane": str(lane or "")[:64],
            "probe_token": "",
            "probe_until": 0,
        }, timeout=max(60, int(delay) + 60))
    return True


def active_slot_cap(*, configured_cap: int | None = None, measured_user_cap: int | None = None) -> int:
    """Leave four DB slots for web/operators; unknown capacity admits one lane."""
    if configured_cap is None:
        try:
            configured_cap = int(getattr(settings, "IG_DB_BACKGROUND_ACTIVE_CAP", 4))
        except (TypeError, ValueError):
            configured_cap = 1
    configured_cap = max(1, min(int(configured_cap), 32))
    if measured_user_cap is None:
        snapshot = capacity_snapshot()
        measured_user_cap = snapshot.get("user_cap") if snapshot.get("known") else 0
    if not measured_user_cap:
        return 1
    return max(0, min(configured_cap, int(measured_user_cap) - 4))


@contextmanager
def db_active_slot(*, configured_cap: int | None = None, measured_user_cap: int | None = None):
    cap = active_slot_cap(configured_cap=configured_cap, measured_user_cap=measured_user_cap)
    if cap <= 0:
        raise DbActiveCapacityError("no database background slots available")
    handles = []
    try:
        for index in range(cap):
            path = _lock_path(f"slot-{index}")
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            handles.append(handle)
            yield index
            return
        raise DbActiveCapacityError("database background slot cap reached")
    finally:
        for handle in handles:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


@contextmanager
def retain_session_connection(*, using: str = "default"):
    """An explicit session-lock owner retains its connection until release.

    This is independent of transaction.atomic: MariaDB advisory locks belong
    to the physical connection and disappear if an idle connection is closed.
    Nested owners restore the previous alias set on every exit.
    """
    token = _retained_session_connections.set(_retained_session_connections.get() | {using})
    try:
        yield
    finally:
        _retained_session_connections.reset(token)


def release_idle_connection(*, using: str = "default") -> bool:
    """Drop only a non-atomic connection without an active session-lock owner."""
    connection = connections[using]
    if connection.in_atomic_block or using in _retained_session_connections.get():
        return False
    connection.close()
    return True


def perform_read_probe(scope: str, probe_token: str | None) -> None:
    """Only a fresh successful SELECT 1 can close a half-open circuit."""
    if not probe_token:
        return
    try:
        with connections[scope].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        record_db_failure(exc, scope=scope, lane="half_open_probe")
        raise DbCircuitOpen("database half-open probe failed") from exc
    record_read_success(scope, probe_token=probe_token)
