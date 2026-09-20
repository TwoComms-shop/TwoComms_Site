"""Bounded low-priority refresh state, separate from customer/analysis jobs."""
from datetime import timedelta
import json
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max, OuterRef, Q, Subquery
from django.utils import timezone

from management.models import (
    IgClient, IgConversationAnalysisJob, IgFunnelResetAudit,
    IgJourneyTraceRefreshControl as Control, IgJourneyTraceRefreshJob as Job,
    InstagramBotLog, InstagramBotMessage,
)

SWEEP_SECONDS = 15
BATCH_SIZE = 100
DEBOUNCE_SECONDS = 30
MIN_INTERVAL_SECONDS = 120
LEASE_SECONDS = 180
_ROLES = ("user", "manager", "model")
_RETRYABLE = frozenset({"ordinary_analysis_busy", "customer_reply_busy", "normal_work_busy",
    "trace_lease_busy", "refresh_budget", "refresh_disabled", "settings_missing",
    "key_project_mapping_missing", "sender_not_allowed", "database_unavailable", "refresh_not_due"})


def automatic_enabled():
    return bool(getattr(settings, "IG_JOURNEY_TRACE_REFRESH_ENABLED", True)) and Control.objects.filter(pk=1, enabled=True).exists()


def configure_refresh(*, enabled, actor_id, max_starts_per_hour=None):
    """Audited operator activation; no historical client population or provider."""
    if type(enabled) is not bool or type(actor_id) is not int or actor_id <= 0:
        raise ValueError("invalid_refresh_configuration")
    if max_starts_per_hour is not None and (type(max_starts_per_hour) is not int or not 1 <= max_starts_per_hour <= 24):
        raise ValueError("refresh_budget_must_be_1_to_24")
    if not get_user_model().objects.filter(pk=actor_id, is_active=True, is_staff=True).exists():
        raise ValueError("active_staff_actor_required")
    with transaction.atomic():
        control, _ = Control.objects.select_for_update().get_or_create(pk=1)
        now = timezone.now()
        if enabled and not control.enabled:
            latest = InstagramBotMessage.objects.order_by("-id").values_list("id", flat=True).first() or 0
            control.activation_watermark = control.scan_cursor = latest
            control.activated_at = now
            control.repair_cursor = 0
            control.repair_at = None
        control.enabled = enabled
        control.changed_by_id = actor_id
        if max_starts_per_hour is not None:
            control.max_starts_per_hour = max_starts_per_hour
        # Disabling never releases an in-flight lease: its owner still finishes.
        control.save()
        result = {"enabled": enabled, "activation_watermark": control.activation_watermark,
                  "scan_cursor": control.scan_cursor, "max_starts_per_hour": control.max_starts_per_hour,
                  "actor_id": actor_id}
        InstagramBotLog.objects.create(event="journey_trace_refresh_configuration", detail=json.dumps(result, sort_keys=True))
        return result


def normal_work_waiting():
    now = timezone.now()
    return IgConversationAnalysisJob.objects.filter(
        Q(status="processing") | Q(status="pending", due_at__lte=now, next_attempt_at__lte=now),
    ).exists()


def _schedule_batch(latest_by_client, now):
    if not latest_by_client:
        return 0
    # Lock clients before refresh jobs; erasure uses the same order. All source
    # discovery and reset-floor reads are grouped, never a per-client query.
    clients = list(IgClient.objects.select_for_update().filter(
        pk__in=latest_by_client, hidden_at__isnull=True, privacy_erasure_started_at__isnull=True,
        is_blocked=False,
    ).exclude(stage=IgClient.Stage.SPAM).values_list("pk", flat=True))
    floors = dict(IgFunnelResetAudit.objects.filter(client_id__in=clients).values("client_id").annotate(
        floor=Max("reset_after_message_id")).values_list("client_id", "floor"))
    jobs = {job.client_id: job for job in Job.objects.select_for_update().filter(client_id__in=clients)}
    create, update = [], []
    for client_id in clients:
        watermark = latest_by_client[client_id]
        floor = int(floors.get(client_id) or 0) + 1
        if watermark < floor:
            continue
        job = jobs.get(client_id)
        if job and job.requested_watermark >= watermark and job.reset_floor == floor:
            continue
        due = now + timedelta(seconds=DEBOUNCE_SECONDS)
        if job is None:
            create.append(Job(client_id=client_id, requested_watermark=watermark, reset_floor=floor, due_at=due))
            continue
        job.requested_watermark = max(watermark, job.requested_watermark)
        job.reset_floor = floor
        job.generation += 1
        job.status = Job.Status.PENDING
        job.due_at = max(due, (job.last_attempt_at + timedelta(seconds=MIN_INTERVAL_SECONDS)) if job.last_attempt_at else due)
        job.updated_at = now
        update.append(job)
    Job.objects.bulk_create(create)
    if update:
        Job.objects.bulk_update(update, ["requested_watermark", "reset_floor", "generation", "status", "due_at", "updated_at"])
    return len(create) + len(update)


def discover_refresh_requests():
    """At most 100 message rows + 100 repair clients, no provider work."""
    if not automatic_enabled():
        return {"status": "disabled", "scheduled": 0}
    with transaction.atomic():
        control = Control.objects.select_for_update().get(pk=1)
        if not control.enabled:
            return {"status": "disabled", "scheduled": 0}
        now = timezone.now()
        rows = list(InstagramBotMessage.objects.filter(id__gt=control.scan_cursor).order_by("id").values(
            "id", "client_id", "role")[:BATCH_SIZE])
        latest = {}
        for row in rows:
            if row["client_id"] and row["role"] in _ROLES:
                latest[row["client_id"]] = max(latest.get(row["client_id"], 0), row["id"])
        if rows:
            control.scan_cursor = rows[-1]["id"]
        # IDs can commit out of order. A bounded independent client sweep repairs
        # missed commits; activation floor prevents implicit historical backfill.
        repaired = 0
        if control.repair_at is None or control.repair_at <= now - timedelta(seconds=60):
            newest = InstagramBotMessage.objects.filter(client_id=OuterRef("pk"), role__in=_ROLES).order_by("-id").values("id")[:1]
            repair = list(IgClient.objects.filter(pk__gt=control.repair_cursor).order_by("pk").annotate(
                newest=Subquery(newest)).values("pk", "newest")[:BATCH_SIZE])
            control.repair_cursor = repair[-1]["pk"] if repair else 0
            control.repair_at = now
            for row in repair:
                if (row["newest"] or 0) > control.activation_watermark:
                    latest[row["pk"]] = max(latest.get(row["pk"], 0), row["newest"])
                    repaired += 1
        scheduled = _schedule_batch(latest, now)
        control.save(update_fields=["scan_cursor", "repair_cursor", "repair_at", "changed_at"])
        return {"status": "scanned", "messages": len(rows), "repair_clients": repaired, "scheduled": scheduled}


def start_trace_attempt(*, client_id, fingerprint, watermark, reset_floor, automatic):
    """Reserve one actual provider start. No transaction survives this function."""
    if automatic and normal_work_waiting():
        return "", "normal_work_busy"
    with transaction.atomic():
        control, _ = Control.objects.select_for_update().get_or_create(pk=1)
        now = timezone.now()
        if automatic and (not control.enabled or not getattr(settings, "IG_JOURNEY_TRACE_REFRESH_ENABLED", True)):
            return "", "refresh_disabled"
        if control.lease_token and control.lease_until and control.lease_until > now:
            return "", "trace_lease_busy"
        job = Job.objects.select_for_update().filter(client_id=client_id).first()
        if automatic:
            if not job or watermark <= control.activation_watermark:
                return "", "refresh_not_due"
            if job.attempted_fingerprint == fingerprint:
                return "", "fingerprint_already_attempted"
            if job.due_at > now or (job.last_attempt_at and job.last_attempt_at > now - timedelta(seconds=MIN_INTERVAL_SECONDS)):
                return "", "refresh_not_due"
            if job.reset_floor != reset_floor or job.requested_watermark != watermark:
                return "", "sources_changed"
        if control.budget_started_at is None or control.budget_started_at <= now - timedelta(hours=1):
            control.budget_started_at, control.budget_starts = now, 0
        if control.budget_starts >= control.max_starts_per_hour:
            return "", "refresh_budget"
        token = uuid.uuid4().hex
        control.lease_token, control.lease_until = token, now + timedelta(seconds=LEASE_SECONDS)
        control.budget_starts += 1
        control.save(update_fields=["lease_token", "lease_until", "budget_started_at", "budget_starts", "changed_at"])
        if job:
            job.attempted_fingerprint = fingerprint
            job.last_attempt_at = now
            job.attempted_generation = job.generation
            job.lease_token, job.lease_until = token, control.lease_until
            job.status = Job.Status.PROCESSING
            job.last_reason = ""
            job.save()
        return token, ""


def lease_is_current(token, *, automatic=False):
    query = Control.objects.filter(pk=1, lease_token=token, lease_until__gt=timezone.now())
    return query.filter(enabled=True).exists() if automatic else query.exists()


def finish_trace_attempt(*, client_id, token, report, automatic):
    """Token-fenced finish cannot erase a newer generation or another lease."""
    with transaction.atomic():
        control = Control.objects.select_for_update().filter(pk=1).first()
        job = Job.objects.select_for_update().filter(client_id=client_id).first()
        now = timezone.now()
        if token and control and control.lease_token == token:
            control.lease_token, control.lease_until = "", None
            control.save(update_fields=["lease_token", "lease_until", "changed_at"])
        if not job or (token and job.lease_token != token) or (not token and job.lease_token):
            return
        reason = report.get("reason", "")
        success = report.get("status") in {"recorded", "existing"}
        newer = token and job.generation != job.attempted_generation
        if newer:
            job.status = Job.Status.PENDING
            job.due_at = max(job.due_at, now + timedelta(seconds=MIN_INTERVAL_SECONDS))
        elif success:
            job.status = Job.Status.DONE
            job.last_snapshot_id = report.get("snapshot_id")
        elif not token and automatic and reason in _RETRYABLE:
            job.status = Job.Status.PENDING
            job.due_at = now + timedelta(seconds=SWEEP_SECONDS if "busy" in reason else MIN_INTERVAL_SECONDS)
        else:
            job.status = Job.Status.FAILED
        job.last_reason = reason if reason in _RETRYABLE | {
            "", "provider_failed", "invalid_trace", "sources_changed", "snapshot_conflict", "source_unavailable",
            "client_missing", "hidden", "privacy_erasure", "blocked", "opt_out", "no_human_text",
            "existing_snapshot_invalid", "fingerprint_already_attempted", "lease_expired", "internal_failure",
        } else "internal_failure"
        job.lease_token, job.lease_until = "", None
        job.save()


def refresh_tick():
    """One sweep and at most one provider start, invoked by an isolated thread."""
    result = discover_refresh_requests()
    if result["status"] == "disabled":
        return result
    now = timezone.now()
    # A crashed call consumed its fingerprint. Expiry is terminal for that input,
    # not an invitation to retry it on every poll. New generations remain due.
    Job.objects.filter(status="processing", lease_until__lte=now).update(
        status="failed", lease_token="", lease_until=None, last_reason="lease_expired")
    Job.objects.filter(status="pending", lease_until__lte=now).update(lease_token="", lease_until=None)
    if normal_work_waiting():
        return {**result, "reason": "normal_work_busy"}
    control = Control.objects.get(pk=1)
    if control.lease_token and control.lease_until and control.lease_until > now:
        return {**result, "reason": "trace_lease_busy"}
    client_id = Job.objects.filter(status="pending", due_at__lte=now,
        requested_watermark__gt=control.activation_watermark, lease_token="").order_by("due_at", "pk").values_list("client_id", flat=True).first()
    if client_id:
        from management.services.ig_journey_trace_generation import generate_journey_trace
        result["client"] = generate_journey_trace(client_id, apply=True, _automatic=True)
    return result


def read_refresh_coverage(client_id):
    """Optional current-client presentation metadata; one read, no work on GET."""
    from django.db import DatabaseError
    if type(client_id) is not int or client_id <= 0:
        return {"status": "missing_source"}
    try:
        row = Job.objects.filter(client_id=client_id).values(
            "status", "requested_watermark", "last_reason", "last_snapshot_id", "due_at").first()
    except DatabaseError:
        return {"status": "unavailable"}
    if row is None:
        return {"status": "missing_source"}
    if row["status"] not in Job.Status.values:
        return {"status": "unavailable"}
    row["due_at"] = row["due_at"].isoformat() if row["due_at"] else None
    # The stored field is finite on writes; do not expose arbitrary DB tampering.
    allowed = _RETRYABLE | {"", "provider_failed", "invalid_trace", "sources_changed", "snapshot_conflict",
        "source_unavailable", "client_missing", "hidden", "privacy_erasure", "blocked", "opt_out",
        "no_human_text", "existing_snapshot_invalid", "fingerprint_already_attempted", "lease_expired", "internal_failure"}
    if row["last_reason"] not in allowed:
        row["last_reason"] = "internal_failure"
    return row
