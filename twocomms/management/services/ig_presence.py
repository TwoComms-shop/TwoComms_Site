"""Advisory Instagram presence shared by revision and legacy execution.

Only this worker owns sender-action I/O. Reply workers never join it or wait for
typing visibility. A private per-channel flock serializes presence across local
processes, independently of every substantive send/permission lock. Its retained
generation record prevents late cleanup from disabling a later owner, including
after that owner's durable automation lease has already been released.

Socket budgets do not promise cancellation of DNS or an in-flight request. The
fixed worker limit bounds damage from stuck transports; nothing is replayed on
restart. This flock design assumes the same single-host deployment as the reply
boundary. HTTP acceptance is neither visible UI proof nor customer read evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from django.core.cache import cache
from django.db import connections
from django.utils import timezone
import requests

from management.services.ig_maintenance import _exclusive_file_lock, FileLockTimeout

logger = logging.getLogger(__name__)
CONNECT_SECONDS = 0.5
READ_SECONDS = 1.0
LOCK_SECONDS = 0.05
MAX_WORKERS = 8
MAX_SESSION_SECONDS = 120.0
MIN_ACTION_INTERVAL = 0.25
PRESENCE_DIRECTORY = Path(__file__).resolve().parents[2] / "tmp" / "ig_presence"


@dataclass(frozen=True)
class SenderActionResult:
    ok: bool
    http_status: int
    kind: str
    action: str = ""
    latency_ms: int = 0


@dataclass(frozen=True)
class PresenceCapability:
    route: str
    api_version: str
    account_key: str
    refresh_seconds: float = 0.0
    visibility: str = "ui_unverified_refresh_disabled"


def capability_profile(settings_row):
    from management.services import instagram_bot as bot

    namespace = bot.ingress_provider_namespace(settings_row)
    # A tested account/version may opt into refresh. No Messenger TTL is assumed.
    try:
        refresh = float(os.environ.get("IG_PRESENCE_REFRESH_SECONDS", "0"))
    except ValueError:
        refresh = 0.0
    refresh = min(30.0, max(5.0, refresh)) if refresh > 0 else 0.0
    return PresenceCapability(
        bot.provider_transport(settings_row), bot.GRAPH_VERSION,
        hashlib.sha256(namespace.encode()).hexdigest()[:16], refresh,
        "ui_unverified_refresh_configured" if refresh else "ui_unverified_refresh_disabled",
    )


def _cached_token(settings_row):
    """No token refresh or /me/accounts discovery on this advisory lane."""
    from management.services import instagram_bot as bot

    if bot.provider_transport(settings_row) == bot.INSTAGRAM_LOGIN_TRANSPORT:
        raw = bot.resolve_instagram_login_token()
        state = cache.get(bot._instagram_login_token_cache_key(raw)) if raw else None
        return (state.get("token") or raw) if isinstance(state, dict) else raw
    raw = bot.resolve_direct_token(settings_row)
    if not raw:
        return ""
    effective = cache.get(bot._long_lived_token_cache_key(raw)) or raw
    key, _ = bot._page_token_cache_keys(settings_row, effective)
    return cache.get(key) or ""


def send_sender_action(settings_row, recipient_id, action):
    """Bounded socket transport; call only from the advisory worker."""
    from management.services import instagram_bot as bot

    started = time.monotonic()
    action = action if action in {"mark_seen", "typing_on", "typing_off"} else "unknown"
    status, kind = 0, "invalid_action"
    try:
        if action != "unknown":
            account = bot._provider_account_id(settings_row)
            token = _cached_token(settings_row) if account else ""
            kind = "missing_account" if not account else "missing_token"
            if token:
                url = bot._provider_url(settings_row, f"/{account}/messages")
                host = "graph.instagram.com" if bot.provider_transport(settings_row) == bot.INSTAGRAM_LOGIN_TRANSPORT else "graph.facebook.com"
                if urlsplit(url).netloc != host or not bot._valid_provider_request_url(url, host):
                    raise ValueError("presence_url_policy")
                # No retries, redirects, response body, generic provider retries,
                # or receipt projection. Keep bearer/body out of diagnostics.
                with requests.Session() as http:
                    with http.post(
                        url, headers={"Authorization": f"Bearer {token}"},
                        json={"recipient": {"id": recipient_id}, "sender_action": action},
                        timeout=(CONNECT_SECONDS, READ_SECONDS),
                        allow_redirects=False, stream=True,
                    ) as response:
                        status = int(response.status_code)
                kind = ("accepted" if 200 <= status < 300 else
                        "rate_limited" if status == 429 else
                        "unsupported_or_denied" if status in {400, 401, 403, 404} else "provider")
    except requests.Timeout:
        status, kind = -1, "timeout"
    except Exception:
        status, kind = -1, "transport"
    result = SenderActionResult(
        kind == "accepted", status, kind, action,
        round((time.monotonic() - started) * 1000),
    )
    logger.info("ig_presence action=%s outcome=%s http=%s latency_ms=%s",
                result.action, result.kind, result.http_status, result.latency_ms)
    return result


@contextmanager
def _channel_state(key):
    # Never unlink these lock files: replacing an inode splits mutual exclusion.
    PRESENCE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = PRESENCE_DIRECTORY / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    with _exclusive_file_lock(str(path), timeout_seconds=LOCK_SECONDS):
        with path.open("r+") as handle:
            try:
                state = json.loads(handle.read(4096))
                if not isinstance(state, dict):
                    state = {}
            except ValueError:
                state = {}
            try:
                yield state
            finally:
                handle.seek(0)
                json.dump(state, handle, separators=(",", ":"))
                handle.truncate()
                handle.flush()


class PresenceSession:
    def __init__(self, controller, *, key, transport, guard, source_watermark,
                 refresh_seconds=0, state_boundary=_channel_state, owner_check=None):
        self.controller, self.key = controller, key
        self.transport, self.guard = transport, guard
        self.source_watermark = int(source_watermark or 0)
        self.owner_check = owner_check
        self.owner_version = 0
        self.generation = secrets.token_hex(16)
        self.refresh_seconds = refresh_seconds
        self.state_boundary = state_boundary
        self.stopped = threading.Event()
        self.suspended = threading.Event()
        self.finished = threading.Event()
        self.consecutive_failures = 0
        self.typing_attempted = False
        self.started = time.monotonic()
        self.thread = None

    def update(self, *, source_watermark=None, owner_check=None, resume=False):
        if source_watermark is not None:
            self.source_watermark = max(self.source_watermark, int(source_watermark))
        if owner_check is not None:
            self.owner_check = owner_check
            self.owner_version += 1
        if resume:
            self.suspended.clear()

    def suspend(self):
        """Keep the generation during a local claim handoff without new I/O."""
        self.suspended.set()
        self.owner_version += 1

    def stop(self):
        # Setting an event cannot block on HTTP, a file lock, or database I/O.
        self.stopped.set()

    def _allowed(self, cleanup=False, check_owner=True):
        if not (self.controller.cleanup_allowed(self) if cleanup else self.controller.is_current(self)):
            return False
        if not cleanup and (self.stopped.is_set() or
                            time.monotonic() - self.started >= MAX_SESSION_SECONDS):
            return False
        if not self.guard(cleanup):
            return False
        if cleanup or not check_owner or self.owner_check is None:
            return True
        version = self.owner_version
        allowed = self.owner_check()
        if version != self.owner_version:
            # A local prep->claim handoff may complete during the database read.
            # Recheck its new predicate rather than killing the shared lifecycle.
            return bool(self.suspended.is_set() or self.owner_check())
        return bool(allowed)

    def _action(self, action):
        cleanup = action == "typing_off"
        with self.state_boundary(self.key) as state:
            if not cleanup and self.suspended.is_set():
                raise _PresenceSuspended()
            allowed = self._allowed(cleanup)
            if not cleanup and self.suspended.is_set():
                raise _PresenceSuspended()
            if not allowed:
                return False
            if cleanup:
                if state.get("generation") != self.generation:
                    return False
            else:
                if state.get("cooldown_until", 0) > time.time():
                    return False
                delay = max(0.0, min(MIN_ACTION_INTERVAL, state.get("next_action_at", 0) - time.time()))
                if delay and self.stopped.wait(delay):
                    return False
                allowed = self._allowed()
                if self.suspended.is_set():
                    raise _PresenceSuspended()
                if not allowed:
                    return False
                state["generation"] = self.generation
                state["watermark"] = self.source_watermark
                state["next_action_at"] = time.time() + MIN_ACTION_INTERVAL
            if action == "typing_on":
                # Timeout is ambiguous too; a late accepted on needs cleanup.
                self.typing_attempted = True
            result = self.transport(action)
            if result.ok:
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                if result.kind in {"missing_account", "missing_token", "unsupported_or_denied", "rate_limited"}:
                    state["cooldown_until"] = time.time() + (60 if result.kind == "rate_limited" else 300)
                    return False
            return self.consecutive_failures < 3

    def _perform(self, action):
        while not self.stopped.is_set():
            if not self._allowed(check_owner=not self.suspended.is_set()):
                return False
            if self.suspended.is_set():
                self.stopped.wait(0.05)
                continue
            try:
                return self._action(action)
            except _PresenceSuspended:
                continue
        return False

    def _run(self):
        try:
            if not self._perform("mark_seen"):
                return
            if self.stopped.wait(MIN_ACTION_INTERVAL) or not self._perform("typing_on"):
                return
            next_refresh = time.monotonic() + self.refresh_seconds
            while not self.stopped.wait(0.25):
                if not self._allowed(check_owner=not self.suspended.is_set()):
                    break
                if self.suspended.is_set():
                    continue
                if self.refresh_seconds and time.monotonic() >= next_refresh:
                    if not self._perform("typing_on"):
                        break
                    next_refresh = time.monotonic() + self.refresh_seconds
        except FileLockTimeout:
            logger.info("ig_presence outcome=lane_busy")
        except Exception:
            logger.info("ig_presence outcome=advisory_unavailable")
        finally:
            # Executes after the in-flight call returns, not after a timed join.
            if self.typing_attempted:
                try:
                    self._action("typing_off")
                except Exception:
                    logger.info("ig_presence outcome=cleanup_unavailable")
            try:
                connections.close_all()  # Django connections are thread-local.
            finally:
                self.controller.finished(self)
                self.finished.set()


class _PresenceSuspended(Exception):
    pass


class PresenceController:
    def __init__(self, max_workers=MAX_WORKERS):
        self._slots = threading.BoundedSemaphore(max_workers)
        self._lock = threading.Lock()
        self._sessions = {}

    def is_current(self, session):
        with self._lock:
            return self._sessions.get(session.key) is session

    def cleanup_allowed(self, session):
        with self._lock:
            # If a successor exited without any accepted presence action, the
            # old generation record can still authorize its late cleanup. A
            # successful newer action has its own retained generation fence.
            return self._sessions.get(session.key) in (None, session)

    def start(self, **kwargs):
        if not self._slots.acquire(blocking=False):
            logger.info("ig_presence outcome=worker_capacity")
            return None
        session = PresenceSession(self, **kwargs)
        with self._lock:
            prior = self._sessions.get(session.key)
            if prior:
                prior.stop()
            self._sessions[session.key] = session
        try:
            session.thread = threading.Thread(target=session._run, name="ig-presence", daemon=True)
            session.thread.start()
        except Exception:
            self.finished(session)
            return None
        return session

    def finished(self, session):
        with self._lock:
            if self._sessions.get(session.key) is session:
                self._sessions.pop(session.key)
        self._slots.release()


_controller = PresenceController()


def start_presence(settings_row, *, client_id, recipient_id, owner_token,
                   source_watermark, permission=None, owner_check=None):
    """Start only for an admitted, durably claimed preparation/execution.

    owner_token is the client's automation lease. owner_check additionally fences
    the revision preparation/execution token; update it when the claim rotates.
    Call stop before dispatch and in finally. Do not start for queued/replay work.
    """
    from management.models import IgClient, InstagramBotSettings
    from management.services import instagram_bot as bot
    from management.services.ig_reply_boundary import capture_reply_permission

    try:
        if not client_id or not recipient_id or not owner_token:
            return None
        permission = permission if permission is not None else capture_reply_permission(settings_row.pk, client_id)
        if not permission:
            return None
        namespace = bot.ingress_provider_namespace(settings_row)
        if not namespace:
            return None
        profile = capability_profile(settings_row)

        def guard(cleanup):
            fresh = InstagramBotSettings.objects.filter(pk=settings_row.pk).first()
            if fresh is None or bot.ingress_provider_namespace(fresh) != namespace:
                return False
            client = IgClient.objects.filter(pk=client_id).values(
                "automation_lease_token", "automation_lease_until", "igsid",
            ).first()
            if not client or client["igsid"] != recipient_id:
                return False
            if cleanup:
                # _action also requires the retained generation record. Empty
                # durable ownership alone is deliberately insufficient.
                return client["automation_lease_token"] in {"", owner_token}
            if client["automation_lease_token"] != owner_token or not client["automation_lease_until"] or client["automation_lease_until"] <= timezone.now():
                return False
            current = capture_reply_permission(settings_row.pk, client_id)
            return bool(current and current.settings_epoch == permission.settings_epoch and current.client_epoch == permission.client_epoch)

        logger.info("ig_presence route=%s version=%s account_key=%s capability=%s",
                    profile.route, profile.api_version, profile.account_key, profile.visibility)
        return _controller.start(
            key=f"{namespace}:{client_id}",
            transport=lambda action: send_sender_action(settings_row, recipient_id, action),
            guard=guard, source_watermark=source_watermark,
            refresh_seconds=profile.refresh_seconds, owner_check=owner_check,
        )
    except Exception:
        logger.info("ig_presence outcome=start_unavailable")
        return None
