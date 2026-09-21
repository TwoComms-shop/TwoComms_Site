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


def _row(lock=True, now=None):
    from management.models import IgWorkerLaneState
    from django.db import IntegrityError, connection

    qs = IgWorkerLaneState.objects
    if lock:
        qs = qs.select_for_update()
    try:
        row = qs.get(lane_key=LANE_KEY)
    except IgWorkerLaneState.DoesNotExist:
        # Insert directly so ``updated_at`` keeps the caller-provided clock;
        # Django's auto_now pre_save would consume the clock a second time.
        seed_now = now or timezone.now()
        table = connection.ops.quote_name(IgWorkerLaneState._meta.db_table)
        try:
            # Keep a uniqueness race inside a savepoint so a losing MySQL
            # initializer can still read the winner in the outer transaction.
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO {table} "
                        "(lane_key, owner_kind, owner_token, generation, "
                        "claim_frozen, freeze_generation, freeze_reason, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [LANE_KEY, "", "", 0, False, 0, "", seed_now],
                    )
        except IntegrityError:
            # Another owner initialized the singleton row concurrently.
            pass
        row = qs.get(lane_key=LANE_KEY)
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
        row = _row(now=now)
        # A frozen lane is recoverable only through the evidence-gated
        # cooperative successor path. Direct acquisition here would permit a
        # second writer without reclaiming or fencing the previous claims.
        if row.claim_frozen:
            return None
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
        # Keep the caller-provided clock authoritative.  Including the
        # auto_now field here would invoke timezone.now() a second time,
        # which both adds needless drift and breaks bounded clock injection in
        # one-shot consumers.
        row.save(update_fields=["owner_kind", "owner_token", "generation", "owner_started_at", "heartbeat_at", "lease_until"])
        type(row).objects.filter(pk=row.pk).update(updated_at=now)
        result = {"lane_key": row.lane_key, "owner_kind": row.owner_kind, "owner_token": row.owner_token, "generation": row.generation, "lease_until": row.lease_until}
        return result


def renew_owner(*, owner_token, generation, lease_seconds=DEFAULT_LEASE_SECONDS, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        row = _row()
        # An expired or frozen generation has lost mutation authority.  It must
        # not revive itself after a successor has been admitted or recovery has
        # deliberately stopped new claims.
        if (
            row.owner_token != str(owner_token)
            or int(row.generation) != int(generation)
            or not row.lease_until
            or row.lease_until <= now
            or row.claim_frozen
        ):
            return False
        row.heartbeat_at = now
        row.lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
        row.save(update_fields=["heartbeat_at", "lease_until"])
        type(row).objects.filter(pk=row.pk).update(updated_at=now)
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
        row.save(update_fields=["claim_frozen", "freeze_generation", "frozen_at", "freeze_reason", "recovery_deadline_at"])
        type(row).objects.filter(pk=row.pk).update(updated_at=now)
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
        row.save(update_fields=["owner_kind", "owner_token", "lease_until", "heartbeat_at"])
        type(row).objects.filter(pk=row.pk).update(updated_at=now)
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
    """Return only an explicitly bound owner.

    Analysis queue functions are shared by the daemon and management commands.
    Creating a manual owner as a side effect of a queue call made an unscoped
    command an implicit second writer, so callers must bind/acquire ownership
    explicitly now.
    """
    return current_owner()


def owner_claim_admission(*, now=None):
    owner = ensure_owner(now=now)
    if not owner:
        return None
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
        row.save(update_fields=["claim_frozen", "freeze_reason", "frozen_at", "recovery_deadline_at", "freeze_generation"])
        type(row).objects.filter(pk=row.pk).update(updated_at=now)
        return True


def recover_frozen_lane(*, evidence, owner_kind=MANUAL_OWNER,
                        owner_token=None, lease_seconds=DEFAULT_LEASE_SECONDS,
                        limit=100, now=None):
    """Admit one successor and reclaim only expired claims from old generations.

    ``freeze_claims`` is the first half of this protocol.  The second half is
    intentionally cooperative: a caller must present non-empty evidence from
    its supervisor/maintenance observation, wait for the bounded recovery
    deadline, and then perform owner admission, stale-claim reclamation, and
    unfreeze while holding the same lane-row transaction lock.  No provider is
    called here and a current-generation claim is never reclaimed.
    """
    if not evidence:
        return None
    now = now or timezone.now()
    token = str(owner_token or secrets.token_hex(16))[:64]
    lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
    bounded_limit = max(1, min(int(limit), 1000))
    from django.db.models import Case, CharField, F, PositiveSmallIntegerField, Value, When
    from management.models import IgConversationAnalysisJob

    with transaction.atomic():
        row = _row()
        if not row.claim_frozen:
            return None
        if row.recovery_deadline_at and row.recovery_deadline_at > now:
            return None
        if row.owner_token and row.lease_until and row.lease_until > now:
            return None
        previous_generation = int(row.generation or 0)
        successor_generation = previous_generation + 1
        row.owner_kind = str(owner_kind)[:32]
        row.owner_token = token
        row.generation = successor_generation
        row.owner_started_at = now
        row.heartbeat_at = now
        row.lease_until = lease_until
        row.claim_frozen = False
        row.freeze_reason = ""
        row.frozen_at = None
        row.recovery_deadline_at = None
        row.freeze_generation = previous_generation
        row.save(update_fields=[
            "owner_kind", "owner_token", "generation", "owner_started_at",
            "heartbeat_at", "lease_until", "claim_frozen", "freeze_reason",
            "frozen_at", "recovery_deadline_at", "freeze_generation",
        ])
        type(row).objects.filter(pk=row.pk).update(updated_at=now)
        stale = IgConversationAnalysisJob.objects.filter(
            status=IgConversationAnalysisJob.Status.PROCESSING,
            lease_until__lt=now,
            claim_generation__lt=successor_generation,
        ).order_by("pk")[:bounded_limit]
        stale_ids = list(stale.values_list("pk", flat=True))
        reclaimed = 0
        if stale_ids:
            reclaimed = IgConversationAnalysisJob.objects.filter(pk__in=stale_ids).update(
                status=Case(
                    When(
                        revision__gt=F("claimed_revision"),
                        then=Value(IgConversationAnalysisJob.Status.PENDING),
                    ),
                    When(
                        attempts__gte=5,
                        then=Value(IgConversationAnalysisJob.Status.FAILED),
                    ),
                    default=Value(IgConversationAnalysisJob.Status.PENDING),
                    output_field=CharField(max_length=16),
                ),
                lease_token="",
                lease_until=None,
                next_attempt_at=now,
                attempts=Case(
                    When(revision__gt=F("claimed_revision"), then=Value(0)),
                    default=F("attempts"),
                    output_field=PositiveSmallIntegerField(),
                ),
                last_error=Case(
                    When(
                        revision__gt=F("claimed_revision"),
                        then=Value("cooperative_recovery"),
                    ),
                    When(
                        attempts__gte=5,
                        then=Value("cooperative_recovery_retry_exhausted"),
                    ),
                    default=Value("cooperative_recovery"),
                    output_field=CharField(max_length=1000),
                ),
                claimed_watermark_message_id=0,
                claimed_revision=0,
                claimed_materiality_event_highwater=0,
                claimed_materiality_digest="",
                claimed_authority_digest="",
                claimed_artifact_digest="",
            )
        return {
            "owner_kind": row.owner_kind,
            "owner_token": row.owner_token,
            "generation": successor_generation,
            "previous_generation": previous_generation,
            "reclaimed": reclaimed,
            "evidence": evidence,
        }


def recover_expired_claims(**kwargs):
    """Compatibility name for the cooperative frozen-lane recovery API."""
    return recover_frozen_lane(**kwargs)
