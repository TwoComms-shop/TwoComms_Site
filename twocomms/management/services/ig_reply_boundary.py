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

from management.services.ig_maintenance import FileLockTimeout, _exclusive_file_lock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPLY_PERMISSION_LOCK_FILE = str(PROJECT_ROOT / "tmp" / "ig_bot_reply_boundary.lock")
# Keep the old path during rolling restarts so old and new processes still
# serialize the transition edge.  New code no longer holds it during Gemini.
REPLY_BOUNDARY_LOCK_FILE = REPLY_PERMISSION_LOCK_FILE
_WAIT_COUNTER = "ig_reply_barrier_waits"
_ABORT_COUNTER = "ig_reply_barrier_aborts"
MAX_WEB_TRANSITION_WAIT_SECONDS = 1.0


class ReplyBoundaryTimeout(TimeoutError):
    """A web-triggered permission transition could not acquire the send edge."""


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
    if getattr(client, "privacy_erasure_started_at", None) is not None:
        return False, "privacy_erasure_started"
    if client.hidden_at:
        return False, "hidden"
    if client.opted_out_at and (
        not client.opted_in_at or client.opted_out_at > client.opted_in_at
    ):
        return False, "opt_out"
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
    from management.services.ig_permission_transitions import permission_transition_blocks

    settings = (
        InstagramBotSettings.objects.filter(pk=settings_id)
        .only("id", "is_enabled", "allowed_senders", "reply_permission_epoch", "ig_user_id", "page_id")
        .first()
        if settings_id
        else None
    )
    settings_epoch = int(getattr(settings, "reply_permission_epoch", 0) or 0)
    if permission_transition_blocks(settings_id=settings_id, client_id=client_id):
        client_epoch = 0
        if client_id:
            client_epoch = int(
                IgClient.objects.filter(pk=client_id)
                .values_list("reply_permission_epoch", flat=True)
                .first()
                or 0
            )
        return ReplyPermission(
            settings_id=settings_id,
            settings_epoch=settings_epoch,
            client_id=client_id,
            client_epoch=client_epoch,
            allowed=False,
            reason="permission_transition_pending",
        )
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
            "igsid",
            "reply_permission_epoch",
            "bot_paused",
            "manager_takeover",
            "is_blocked",
            "hidden_at",
            "opted_out_at",
            "opted_in_at",
            "privacy_erasure_started_at",
        )
        .first()
    )
    client_epoch = int(getattr(client, "reply_permission_epoch", 0) or 0)
    from management.services.instagram_bot import allowed_sender_ids

    allowed_senders = allowed_sender_ids(settings)
    if allowed_senders and str(getattr(client, "igsid", "") or "") not in allowed_senders:
        return ReplyPermission(
            settings_id=settings_id,
            settings_epoch=settings_epoch,
            client_id=client_id,
            client_epoch=client_epoch,
            allowed=False,
            reason="sender_not_allowed",
        )
    client_allowed, reason = _client_allowed(client)
    if client_allowed:
        from management.services.instagram_bot import ingress_provider_namespace
        from management.services.ig_revision_echo_integration import uses_revision_echo_scope
        from management.services.ig_revision_echo import revision_echo_blocks

        namespace = ingress_provider_namespace(settings)
        if uses_revision_echo_scope(namespace, client.igsid) and revision_echo_blocks(client.pk, namespace):
            client_allowed, reason = False, "echo_attribution_pending"
    return ReplyPermission(
        settings_id=settings_id,
        settings_epoch=settings_epoch,
        client_id=client_id,
        client_epoch=client_epoch,
        allowed=client_allowed,
        reason=reason,
    )


@contextmanager
def pause_reply_boundary(
    *,
    lock_path: str = REPLY_PERMISSION_LOCK_FILE,
    timeout_seconds: float | None = None,
):
    """Own a short permission transition edge.

    Callers must increment the relevant durable epoch inside this context.
    The lock is never held during Gemini or other provider work.
    """
    if timeout_seconds is not None and not (
        0 <= float(timeout_seconds) <= MAX_WEB_TRANSITION_WAIT_SECONDS
    ):
        raise ValueError("web permission transition timeout must be between 0 and 1 second")
    try:
        with _exclusive_file_lock(lock_path, timeout_seconds=timeout_seconds):
            yield
    except FileLockTimeout as exc:
        raise ReplyBoundaryTimeout("reply permission boundary is busy") from exc


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
            if allowed:
                yield current
            else:
                yield ReplyPermission(
                    settings_id=current.settings_id,
                    settings_epoch=current.settings_epoch,
                    client_id=current.client_id,
                    client_epoch=current.client_epoch,
                    allowed=False,
                    reason=current.reason or "permission_epoch_changed",
                )
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
