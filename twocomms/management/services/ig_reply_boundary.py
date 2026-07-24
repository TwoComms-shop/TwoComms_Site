"""Bounded permission barrier for Instagram customer-facing automation.

Generation/analysis must never hold a process-wide lock.  A worker captures
the durable global/client epochs before provider work, then takes the short
send lock only for the final revalidation and Meta request.  Stop/pause/
takeover use the same short lock while incrementing their epoch, so a committed
boundary cannot be followed by a customer send from an older worker.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from django.core.cache import cache

from management.services.ig_maintenance import _exclusive_file_lock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPLY_PERMISSION_LOCK_FILE = str(PROJECT_ROOT / "tmp" / "ig_bot_reply_boundary.lock")
# Keep the old path during rolling restarts so old and new processes still
# serialize the transition edge.  New code no longer holds it during Gemini.
REPLY_BOUNDARY_LOCK_FILE = REPLY_PERMISSION_LOCK_FILE
_WAIT_COUNTER = "ig_reply_barrier_waits"
_ABORT_COUNTER = "ig_reply_barrier_aborts"


def _increment_counter(key: str) -> None:
    try:
        if not cache.add(key, 1, timeout=None):
            cache.incr(key)
    except Exception:
        pass


@dataclass(frozen=True)
class ReplyPermission:
    settings_id: int | None
    settings_epoch: int
    client_id: int | None
    client_epoch: int
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def _client_allowed(client) -> tuple[bool, str]:
    if not client:
        return False, "client_missing"
    if client.hidden_at:
        return False, "hidden"
    if client.bot_paused:
        return False, "client_paused"
    if client.manager_takeover:
        return False, "manager_takeover"
    if client.is_blocked:
        return False, "blocked"
    return True, ""


def capture_reply_permission(settings_id: int | None, client_id: int | None) -> ReplyPermission:
    """Read the current permission truth without holding a cross-request lock."""
    from management.models import IgClient, InstagramBotSettings

    settings = (
        InstagramBotSettings.objects.filter(pk=settings_id)
        .only("id", "is_enabled", "reply_permission_epoch")
        .first()
        if settings_id
        else None
    )
    settings_epoch = int(getattr(settings, "reply_permission_epoch", 0) or 0)
    if not settings or not settings.is_enabled:
        return ReplyPermission(
            settings_id=settings_id,
            settings_epoch=settings_epoch,
            client_id=client_id,
            client_epoch=0,
            allowed=False,
            reason="global_reply_paused",
        )
    if not client_id:
        return ReplyPermission(
            settings_id=settings_id,
            settings_epoch=settings_epoch,
            client_id=None,
            client_epoch=0,
            allowed=True,
        )
    client = (
        IgClient.objects.filter(pk=client_id)
        .only(
            "id",
            "reply_permission_epoch",
            "bot_paused",
            "manager_takeover",
            "is_blocked",
            "hidden_at",
        )
        .first()
    )
    client_epoch = int(getattr(client, "reply_permission_epoch", 0) or 0)
    client_allowed, reason = _client_allowed(client)
    return ReplyPermission(
        settings_id=settings_id,
        settings_epoch=settings_epoch,
        client_id=client_id,
        client_epoch=client_epoch,
        allowed=client_allowed,
        reason=reason,
    )


@contextmanager
def pause_reply_boundary(*, lock_path: str = REPLY_PERMISSION_LOCK_FILE):
    """Own a short permission transition edge.

    Callers must increment the relevant durable epoch inside this context.
    The lock is never held during Gemini or other provider work.
    """
    with _exclusive_file_lock(lock_path):
        yield


@contextmanager
def reply_execution_boundary(
    settings_id: int | None,
    client_id: int | None,
    *,
    lock_path: str = REPLY_PERMISSION_LOCK_FILE,
):
    """Capture permission epochs before a potentially slow generation call."""
    del lock_path  # compatibility with the former long-held lock API
    yield capture_reply_permission(settings_id, client_id)


@contextmanager
def customer_send_boundary(
    settings_id: int | None,
    client_id: int | None,
    permission: ReplyPermission | None = None,
    *,
    lock_path: str = REPLY_PERMISSION_LOCK_FILE,
):
    """Revalidate epochs and serialize only the external customer send."""
    started = time.monotonic()
    waited_long = False
    aborted = False
    try:
        with _exclusive_file_lock(lock_path):
            waited_long = time.monotonic() - started >= 0.01
            current = capture_reply_permission(settings_id, client_id)
            same_generation = bool(
                permission
                and current.settings_epoch == permission.settings_epoch
                and current.client_epoch == permission.client_epoch
            )
            allowed = bool(current and (permission is None or same_generation))
            aborted = not allowed
            yield allowed
    finally:
        # Redis/cache telemetry must never extend the permission lock.
        if waited_long:
            _increment_counter(_WAIT_COUNTER)
        if aborted:
            _increment_counter(_ABORT_COUNTER)


def reply_barrier_telemetry() -> dict[str, int]:
    """Small operational counters safe to expose in the management status API."""
    result: dict[str, int] = {}
    for name, key in (("waits", _WAIT_COUNTER), ("aborts", _ABORT_COUNTER)):
        try:
            result[name] = int(cache.get(key) or 0)
        except Exception:
            result[name] = 0
    return result
