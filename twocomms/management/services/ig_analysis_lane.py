"""Durable ownership and claim-admission fence for conversation analysis."""

from datetime import timedelta
import secrets
from contextvars import ContextVar
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

LANE_KEY = "conversation_analysis"
DAEMON_OWNER = "daemon"
MANUAL_OWNER = "manual"
DEFAULT_LEASE_SECONDS = 90
DEFAULT_RECOVERY_SECONDS = 900
_OWNER = ContextVar("ig_analysis_lane_owner", default=None)


def _row(lock=True):
    from management.models import IgWorkerLaneState

    qs = IgWorkerLaneState.objects
    if lock:
        qs = qs.select_for_update()
    row, _created = qs.get_or_create(lane_key=LANE_KEY)
    return row


def ensure_lane(*, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        return _row()


def acquire_owner(*, owner_kind=DAEMON_OWNER, owner_token=None,
                  lease_seconds=DEFAULT_LEASE_SECONDS, now=None):
    """Acquire or renew the sole owner; active ownership is never stolen."""
    now = now or timezone.now()
    token = str(owner_token or secrets.token_hex(16))[:64]
    lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
    with transaction.atomic():
        row = _row()
        active = bool(row.owner_token and row.lease_until and row.lease_until > now)
        if active and not (row.owner_kind == owner_kind and row.owner_token == token):
            return None
        if not active or row.owner_token != token:
            row.generation = int(row.generation or 0) + 1
            row.owner_started_at = now
        row.owner_kind = str(owner_kind)[:32]
        row.owner_token = token
        row.heartbeat_at = now
        row.lease_until = lease_until
        row.save(update_fields=["owner_kind", "owner_token", "generation", "owner_started_at", "heartbeat_at", "lease_until", "updated_at"])
        result = {"lane_key": row.lane_key, "owner_kind": row.owner_kind, "owner_token": row.owner_token, "generation": row.generation, "lease_until": row.lease_until}
        return result


def renew_owner(*, owner_token, generation, lease_seconds=DEFAULT_LEASE_SECONDS, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        if row.owner_token != str(owner_token) or int(row.generation) != int(generation):
            return False
        row.heartbeat_at = now
        row.lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        row.save(update_fields=["heartbeat_at", "lease_until", "updated_at"])
        return True


def freeze_claims(*, owner_token, generation, reason, recovery_seconds=DEFAULT_RECOVERY_SECONDS, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        if row.owner_token != str(owner_token) or int(row.generation) != int(generation):
            return False
        row.claim_frozen = True
        row.freeze_generation = row.generation
        row.frozen_at = now
        row.freeze_reason = str(reason or "lane_stalled")[:128]
        row.recovery_deadline_at = now + timedelta(seconds=max(1, int(recovery_seconds)))
        row.save(update_fields=["claim_frozen", "freeze_generation", "frozen_at", "freeze_reason", "recovery_deadline_at", "updated_at"])
        return True


def release_owner(*, owner_token, generation, now=None):
    """Release only the exact owner generation; preserve a newer freeze/owner."""
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        if row.owner_token != str(owner_token) or int(row.generation) != int(generation):
            return False
        row.owner_kind = ""
        row.owner_token = ""
        row.lease_until = None
        row.heartbeat_at = now
        row.save(update_fields=["owner_kind", "owner_token", "lease_until", "heartbeat_at", "updated_at"])
        return True


def claim_admission(*, owner_token, generation, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        return bool(
            row.owner_token == str(owner_token)
            and int(row.generation) == int(generation)
            and row.lease_until and row.lease_until > now
            and not row.claim_frozen
        )


def recovery_admission(*, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        return bool(row.claim_frozen and row.recovery_deadline_at and row.recovery_deadline_at > now)


@contextmanager
def owner_scope(*, owner_kind=DAEMON_OWNER, owner_token=None,
                lease_seconds=DEFAULT_LEASE_SECONDS, now=None):
    owner = acquire_owner(
        owner_kind=owner_kind,
        owner_token=owner_token,
        lease_seconds=lease_seconds,
        now=now,
    )
    if owner is None:
        yield None
        return
    marker = _OWNER.set(owner)
    try:
        yield owner
    finally:
        _OWNER.reset(marker)


def current_owner():
    return _OWNER.get()


def owner_snapshot():
    row = _row(lock=False)
    return {
        "owner_kind": row.owner_kind,
        "owner_token": row.owner_token,
        "generation": int(row.generation or 0),
        "claim_frozen": bool(row.claim_frozen),
        "lease_until": row.lease_until,
        "freeze_generation": int(row.freeze_generation or 0),
        "freeze_reason": row.freeze_reason,
    }


def bind_owner(owner):
    return _OWNER.set(owner)


def unbind_owner(marker):
    _OWNER.reset(marker)


def ensure_owner(*, owner_kind=MANUAL_OWNER, now=None):
    """Return the scoped owner or a bounded manual owner for one-shot callers."""
    return current_owner() or acquire_owner(owner_kind=owner_kind, now=now)


def owner_claim_admission(*, now=None):
    owner = ensure_owner(now=now)
    if not owner:
        return None
    if current_owner() is None:
        _OWNER.set(owner)
    allowed = claim_admission(
        owner_token=owner["owner_token"],
        generation=owner["generation"],
        now=now,
    )
    return owner if allowed else None


@contextmanager
def mutation_guard(*, owner_token, generation, now=None):
    """Hold the lane row lock while a caller performs its queue CAS update."""
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        allowed = bool(
            row.owner_token == str(owner_token)
            and int(row.generation) == int(generation)
            and row.lease_until and row.lease_until > now
            and not row.claim_frozen
        )
        yield allowed


def unfreeze_claims(*, owner_token, generation, now=None):
    """Clear a freeze only after the bounded recovery window has elapsed."""
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        if (
            row.owner_token != str(owner_token)
            or int(row.generation) != int(generation)
            or not row.claim_frozen
            or (row.recovery_deadline_at and row.recovery_deadline_at > now)
        ):
            return False
        row.claim_frozen = False
        row.freeze_reason = ""
        row.frozen_at = None
        row.recovery_deadline_at = None
        row.freeze_generation = row.generation
        row.save(update_fields=["claim_frozen", "freeze_reason", "frozen_at", "recovery_deadline_at", "freeze_generation", "updated_at"])
        return True
