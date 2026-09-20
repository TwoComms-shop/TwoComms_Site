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
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, connections, transaction
from django.db.models import Q
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
DELIVERY_SEEN_WAIT_SECONDS = 5.0
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
    config_fingerprint: str
    refresh_seconds: float = 0.0
    visibility: str = "ui_unverified_refresh_disabled"


CAPABILITY_TYPING_REFRESH = "typing_refresh"
VERIFIED_CAPABILITY_TTL = timedelta(hours=24)
DEFINITIVE_DENIAL_COOLDOWN = timedelta(hours=24)
RATE_LIMIT_COOLDOWN = timedelta(minutes=15)
VERIFICATION_EVIDENCE_KIND = "tester_observation"
_EVIDENCE_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256_text(namespace, *values):
    digest = hashlib.sha256()
    digest.update(namespace.encode())
    for value in values:
        digest.update(b"\0")
        digest.update(str(value or "").encode())
    return digest.hexdigest()


def _presence_configured_token(settings_row, transport):
    """Use configured credentials only; this never performs token discovery."""
    from management.services import instagram_bot as bot

    if transport == bot.INSTAGRAM_LOGIN_TRANSPORT:
        return bot.resolve_instagram_login_token() or ""
    return bot.resolve_direct_token(settings_row) or ""


def capability_profile(settings_row):
    from management.services import instagram_bot as bot

    # Refresh remains opt-in; a configured cadence is not capability proof.
    # A single accepted sender action is transport evidence, not UI proof.
    try:
        refresh = float(os.environ.get("IG_PRESENCE_REFRESH_SECONDS", "0"))
    except ValueError:
        refresh = 0.0
    refresh = min(30.0, max(5.0, refresh)) if refresh > 0 else 0.0
    transport = bot.provider_transport(settings_row)
    account = bot._provider_account_id(settings_row) or ""
    token = _presence_configured_token(settings_row, transport)
    account_key = _sha256_text("ig-presence-account-v1", account)
    config_fingerprint = _sha256_text(
        "ig-presence-config-v1", transport, bot.GRAPH_VERSION, account,
        _sha256_text("ig-presence-token-v1", token),
    )
    return PresenceCapability(
        transport, bot.GRAPH_VERSION, account_key, config_fingerprint, refresh,
        "ui_unverified_refresh_configured" if refresh else "ui_unverified_refresh_disabled",
    )


def _capability_identity(profile):
    return {
        "transport": profile.route,
        "graph_version": profile.api_version,
        "account_key": profile.account_key,
        "capability": CAPABILITY_TYPING_REFRESH,
    }


def _authorized_operator_id(operator_id):
    try:
        operator_id = int(operator_id)
    except (TypeError, ValueError):
        raise ValueError("typing refresh verification requires an active staff operator")
    if operator_id <= 0:
        raise ValueError("typing refresh verification requires an active staff operator")
    authorized = get_user_model().objects.filter(
        pk=operator_id, is_active=True,
    ).filter(Q(is_staff=True) | Q(is_superuser=True)).exists()
    if not authorized:
        raise ValueError("typing refresh verification requires an active staff operator")
    return operator_id


def _validated_evidence(evidence_kind, evidence_ref):
    if evidence_kind != VERIFICATION_EVIDENCE_KIND or not _EVIDENCE_REF_RE.fullmatch(str(evidence_ref or "")):
        raise ValueError("typing refresh verification requires a bounded tester evidence reference")
    return evidence_kind, evidence_ref


def typing_refresh_authorized(profile, *, now=None):
    """Fail closed unless a matching, explicit verification remains live."""
    if not profile.refresh_seconds:
        return False
    from management.models import IgPresenceCapability

    now = now or timezone.now()
    try:
        return IgPresenceCapability.objects.filter(
            **_capability_identity(profile),
            config_fingerprint=profile.config_fingerprint,
            status=IgPresenceCapability.Status.VERIFIED,
            expires_at__gt=now,
        ).filter(Q(denied_until__isnull=True) | Q(denied_until__lte=now)).exists()
    except DatabaseError:
        logger.info("ig_presence outcome=capability_unavailable")
        return False


def verify_typing_refresh_capability(
    settings_row, *, verified_by_id, evidence_kind, evidence_ref, now=None,
):
    """Record manual, opaque proof that this exact configuration may refresh."""
    verified_by_id = _authorized_operator_id(verified_by_id)
    evidence_kind, evidence_ref = _validated_evidence(evidence_kind, evidence_ref)
    profile = capability_profile(settings_row)
    now = now or timezone.now()
    from management.models import IgPresenceCapability

    try:
        with transaction.atomic():
            capability, _ = IgPresenceCapability.objects.select_for_update().get_or_create(
                **_capability_identity(profile),
                defaults={"config_fingerprint": profile.config_fingerprint},
            )
            capability.config_fingerprint = profile.config_fingerprint
            capability.status = IgPresenceCapability.Status.VERIFIED
            capability.verified_at = now
            capability.expires_at = now + VERIFIED_CAPABILITY_TTL
            capability.verified_by_id = verified_by_id
            capability.evidence_kind = evidence_kind
            capability.evidence_ref = evidence_ref
            capability.denied_at = None
            capability.denied_until = None
            capability.denied_error_kind = ""
            capability.denied_error_code = ""
            capability.invalidated_at = None
            capability.invalidated_by_id = None
            capability.invalidation_reason = ""
            capability.last_error_kind = ""
            capability.last_error_code = ""
            capability.last_error_at = None
            capability.save()
            return capability
    except (DatabaseError, IntegrityError):
        logger.info("ig_presence outcome=capability_verification_unavailable")
        raise


def invalidate_typing_refresh_capability(
    settings_row, *, invalidated_by_id, reason, now=None,
):
    """Explicitly revoke refresh authority without touching advisory presence."""
    invalidated_by_id = _authorized_operator_id(invalidated_by_id)
    if not str(reason or "").strip() or len(str(reason).strip()) > 255:
        raise ValueError("typing refresh invalidation requires a bounded reason")
    profile = capability_profile(settings_row)
    now = now or timezone.now()
    from management.models import IgPresenceCapability

    try:
        with transaction.atomic():
            capability, _ = IgPresenceCapability.objects.select_for_update().get_or_create(
                **_capability_identity(profile),
                defaults={"config_fingerprint": profile.config_fingerprint},
            )
            capability.config_fingerprint = profile.config_fingerprint
            capability.status = IgPresenceCapability.Status.INVALIDATED
            capability.expires_at = None
            capability.invalidated_at = now
            capability.invalidated_by_id = invalidated_by_id
            capability.invalidation_reason = str(reason).strip()
            capability.save()
            return capability
    except (DatabaseError, IntegrityError):
        logger.info("ig_presence outcome=capability_invalidation_unavailable")
        raise


def _record_typing_refresh_outcome(profile, result, *, now=None):
    """Persist only negative sender evidence; HTTP acceptance never verifies."""
    if result.ok or result.action != "typing_on":
        return
    if result.kind not in {
        "missing_account", "missing_token", "unsupported_or_denied", "rate_limited",
    }:
        return
    from management.models import IgPresenceCapability

    now = now or timezone.now()
    try:
        with transaction.atomic():
            capability, _ = IgPresenceCapability.objects.select_for_update().get_or_create(
                **_capability_identity(profile),
                defaults={"config_fingerprint": profile.config_fingerprint},
            )
            if capability.config_fingerprint != profile.config_fingerprint:
                # Configuration drift never inherits a prior verification.
                capability.config_fingerprint = profile.config_fingerprint
                capability.status = IgPresenceCapability.Status.UNKNOWN
                capability.expires_at = None
                capability.verified_at = None
                capability.verified_by_id = None
                capability.evidence_kind = ""
                capability.evidence_ref = ""
                capability.denied_at = None
                capability.denied_until = None
                capability.denied_error_kind = ""
                capability.denied_error_code = ""
                capability.invalidated_at = None
                capability.invalidated_by_id = None
                capability.invalidation_reason = ""
            capability.last_error_kind = result.kind
            capability.last_error_code = str(result.http_status or "")[:64]
            capability.last_error_at = now
            if result.kind in {"missing_account", "missing_token", "unsupported_or_denied"}:
                capability.status = IgPresenceCapability.Status.DENIED
                capability.expires_at = None
                capability.denied_at = now
                capability.denied_until = now + DEFINITIVE_DENIAL_COOLDOWN
                capability.denied_error_kind = result.kind
                capability.denied_error_code = str(result.http_status or "")[:64]
            elif result.kind == "rate_limited":
                # A rate limit pauses an existing verification; it is not a
                # policy denial and must recover after its shorter cooldown.
                capability.denied_until = now + RATE_LIMIT_COOLDOWN
            capability.save()
    except (DatabaseError, IntegrityError):
        # This runs only in the advisory worker. Never let capability storage
        # hold up a substantive send, and never permit refresh on failure.
        logger.info("ig_presence outcome=capability_persistence_unavailable")


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
                        # IG-only Meta SDK create_message lists these uppercase
                        # literal enum values; legacy Messenger uses lowercase.
                        json={"recipient": {"id": recipient_id}, "sender_action": (
                            action.upper() if bot.provider_transport(settings_row) == bot.INSTAGRAM_LOGIN_TRANSPORT else action
                        )},
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


def _report_session(summary):
    """One compact record per lifecycle, never a database write per refresh."""
    from management.services import instagram_bot as bot

    detail = " ".join(f"{key}={value}" for key, value in summary.items())
    bot.log("info", "presence_summary", detail)
    # The standard incident helper omits INFO detail. These finite fields are
    # deliberately safe for the operational file as well as the admin console.
    bot._INCIDENT_LOGGER.info("ig_presence %s", detail)


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
                 refresh_seconds=0, state_boundary=_channel_state, owner_check=None,
                 completion_guard=None, report=None, capability=None,
                 refresh_authorized=None, typing_result_callback=None):
        self.controller, self.key = controller, key
        self.transport, self.guard = transport, guard
        self.source_watermark = int(source_watermark or 0)
        self.owner_check = owner_check
        self.completion_guard = completion_guard
        self.report = report
        self.capability = capability
        self.refresh_authorized = refresh_authorized
        self.typing_result_callback = typing_result_callback
        self.owner_version = 0
        self.generation = secrets.token_hex(16)
        self.refresh_seconds = refresh_seconds
        self.state_boundary = state_boundary
        self.stopped = threading.Event()
        self.suspended = threading.Event()
        self.finished = threading.Event()
        self.dispatching = threading.Event()
        self.delivery_resolved = threading.Event()
        self.delivery_confirmed = False
        self.delivery_confirmed_at = 0.0
        self.cancelled = False
        self.consecutive_failures = 0
        self.failure_counts = {}
        self.typing_attempted = False
        self.typing_accepted = False
        self.seen_accepted = False
        self.outcomes = {}
        self.action_counts = {}
        self.total_latency_ms = 0
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
        if not self.delivery_confirmed:
            self.cancelled = True
        self.delivery_resolved.set()
        self.stopped.set()

    def begin_dispatch(self):
        """Stop typing now, while allowing confirmed delivery to finish seen."""
        self.dispatching.set()
        self.stopped.set()

    def complete_delivery(self):
        """Called only after substantive SENT evidence, never on mere planning."""
        if not self.cancelled:
            self.delivery_confirmed = True
            self.delivery_confirmed_at = time.monotonic()
        self.delivery_resolved.set()
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

    def _completed_seen_current(self, state):
        # The channel flock keeps this generation record stable during checks.
        # Local ownership and the deadline can still change while DB reads run.
        return bool(
            not self.cancelled and self.delivery_confirmed
            and time.monotonic() - self.delivery_confirmed_at <= DELIVERY_SEEN_WAIT_SECONDS
            and self.controller.is_current(self)
            and (state.get("generation") in {None, self.generation}
                 or int(state.get("watermark") or 0) < self.source_watermark)
        )

    def _completed_seen_allowed(self, state):
        if not self._completed_seen_current(state) or self.completion_guard is None:
            return False
        if not self.completion_guard():
            return False
        # Permission/source checks may block. They cannot extend the five-second
        # delivery window or retain an owner replaced while those checks ran.
        return self._completed_seen_current(state)

    def _action(self, action, *, completed_seen=False, periodic=False):
        cleanup = action == "typing_off"
        with self.state_boundary(self.key) as state:
            if completed_seen:
                if action != "mark_seen" or not self._completed_seen_allowed(state):
                    return False
            if not cleanup and not completed_seen and self.suspended.is_set():
                raise _PresenceSuspended()
            allowed = completed_seen or self._allowed(cleanup)
            if not cleanup and not completed_seen and self.suspended.is_set():
                raise _PresenceSuspended()
            if not allowed:
                return False
            if cleanup:
                if state.get("generation") != self.generation:
                    return False
            else:
                cooldowns = state.setdefault("action_cooldowns", {})
                if cooldowns.get(action, 0) > time.time():
                    return False
                next_actions = state.setdefault("next_actions", {})
                delay = max(0.0, min(MIN_ACTION_INTERVAL, next_actions.get(action, 0) - time.time()))
                if delay and not completed_seen and self.stopped.wait(delay):
                    return False
                allowed = self._completed_seen_allowed(state) if completed_seen else self._allowed()
                if not completed_seen and self.suspended.is_set():
                    raise _PresenceSuspended()
                if not allowed:
                    return False
                state["generation"] = self.generation
                state["watermark"] = self.source_watermark
                next_actions[action] = time.time() + MIN_ACTION_INTERVAL
            if action == "typing_on":
                # Timeout is ambiguous too; a late accepted on needs cleanup.
                self.typing_attempted = True
            result = self.transport(action)
            if action == "typing_on" and periodic and self.typing_result_callback is not None:
                try:
                    self.typing_result_callback(result)
                except Exception:
                    logger.info("ig_presence outcome=capability_persistence_unavailable")
            self.outcomes[action] = result.kind
            self.action_counts[action] = self.action_counts.get(action, 0) + 1
            self.total_latency_ms += max(0, int(getattr(result, "latency_ms", 0)))
            if result.ok:
                self.failure_counts[action] = 0
                if action == "typing_on":
                    self.typing_accepted = True
                elif action == "mark_seen":
                    self.seen_accepted = True
            else:
                self.failure_counts[action] = self.failure_counts.get(action, 0) + 1
                if (action == "typing_on" and not self.typing_accepted
                        and result.kind in {"missing_account", "missing_token", "unsupported_or_denied", "rate_limited"}):
                    # A definite rejection did not start typing. Preserve
                    # cleanup only for a prior acceptance or ambiguous timeout.
                    self.typing_attempted = False
                if result.kind in {"missing_account", "missing_token", "unsupported_or_denied", "rate_limited"}:
                    state.setdefault("action_cooldowns", {})[action] = time.time() + (60 if result.kind == "rate_limited" else 300)
                    return False
            self.consecutive_failures = self.failure_counts[action]
            return self.consecutive_failures < 3

    def _perform(self, action, *, periodic=False):
        while not self.stopped.is_set():
            if not self._allowed(check_owner=not self.suspended.is_set()):
                return False
            if self.suspended.is_set():
                self.stopped.wait(0.05)
                continue
            try:
                return self._action(action, periodic=periodic)
            except _PresenceSuspended:
                continue
        return False

    def _run(self):
        try:
            # An unsupported seen action must not suppress independent typing.
            self._perform("mark_seen")
            if not self._perform("typing_on"):
                return
            next_refresh = time.monotonic() + self.refresh_seconds
            while not self.stopped.wait(0.25):
                if not self._allowed(check_owner=not self.suspended.is_set()):
                    break
                if self.suspended.is_set():
                    continue
                if self.typing_accepted and self.refresh_seconds and time.monotonic() >= next_refresh:
                    if self.refresh_authorized is None or not self.refresh_authorized():
                        next_refresh = time.monotonic() + self.refresh_seconds
                        continue
                    if not self._perform("typing_on", periodic=True):
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
            # Fast delivery can beat thread startup. Retain one bounded seen
            # attempt only for real SENT, while typing stays irreversibly off.
            if self.dispatching.is_set() and not self.seen_accepted and not self.cancelled:
                self.delivery_resolved.wait(DELIVERY_SEEN_WAIT_SECONDS)
                if self.delivery_confirmed:
                    try:
                        self._action("mark_seen", completed_seen=True)
                    except Exception:
                        logger.info("ig_presence outcome=completed_seen_unavailable")
            try:
                if self.report:
                    self.report({
                        "route": getattr(self.capability, "route", "unknown"),
                        "version": getattr(self.capability, "api_version", "unknown"),
                        "account_key": getattr(self.capability, "account_key", "unknown"),
                        "our_mark_seen": self.outcomes.get("mark_seen", "not_attempted"),
                        "typing": self.outcomes.get("typing_on", "not_attempted"),
                        "off": self.outcomes.get("typing_off", "not_attempted"),
                        "typing_requests": self.action_counts.get("typing_on", 0),
                        "latency_ms": self.total_latency_ms,
                        "cancelled": int(self.cancelled),
                        "late_seen_authorized": int(self.delivery_confirmed),
                        "ui": "unverified",
                    })
            except Exception:
                logger.info("ig_presence outcome=summary_unavailable")
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
                   source_watermark, permission=None, owner_check=None,
                   completion_check=None):
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

        def completed_guard():
            if not guard(True) or completion_check is None:
                return False
            current = capture_reply_permission(settings_row.pk, client_id)
            return bool(current and current.settings_epoch == permission.settings_epoch
                        and current.client_epoch == permission.client_epoch and completion_check())

        logger.info("ig_presence route=%s version=%s account_key=%s capability=%s",
                    profile.route, profile.api_version, profile.account_key, profile.visibility)
        return _controller.start(
            key=f"{namespace}:{client_id}",
            transport=lambda action: send_sender_action(settings_row, recipient_id, action),
            guard=guard, source_watermark=source_watermark,
            refresh_seconds=profile.refresh_seconds, owner_check=owner_check,
            completion_guard=completed_guard,
            report=_report_session, capability=profile,
            refresh_authorized=(lambda: typing_refresh_authorized(profile)) if profile.refresh_seconds else None,
            typing_result_callback=lambda result: _record_typing_refresh_outcome(profile, result),
        )
    except Exception:
        logger.info("ig_presence outcome=start_unavailable")
        return None
