"""Read-only operational observations, separate from the release drain contract.

Counts classified through source/lease proof are bounded samples. They are not
queue totals or dispatch authorization. No claim, provider admission, materializer,
singleton bootstrap, alert or repair is called by this module.
"""
from datetime import timedelta

from django.db import DatabaseError
from django.db.models import CharField, Exists, Max, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Concat
from django.utils import timezone

from management.models import (
    CallRecord, IgAiReplyRecoveryJob, IgBotNotification, IgClient, IgConversationAnalysisEvent, IgConversationAnalysisJob,
    IgCustomerTurnRevision, IgFollowUpTask, IgRevisionDeliveryEffect,
    IgTurnRevisionSource, InstagramBotMessage, InstagramBotSettings,
    InstagramBotTaskHeartbeat,
)
from management.services.ig_typed_memory import shadow_enabled

SAMPLE_LIMIT = 50
BUCKETS = ("runnable", "processing", "manual", "manager_owned", "deferred", "failed", "historical", "unknown", "attention")
# Waiting age is measured from the original due/created time, not updated_at:
# repeated deferral and unrelated daemon cycles must not erase starvation.
REPLY_STALL_SECONDS = 300
SERVICE_STALL_SECONDS = 900
HISTORICAL_FAILURE_SECONDS = 24 * 60 * 60


def _age(now, moment):
    return max(0, int((now - moment).total_seconds())) if moment else None


def _sample(query, classify, *, now, threshold, progress=None, exact_attention=0, complete_risk_scan=False, progress_kind="durable_completion"):
    rows = list(query[:SAMPLE_LIMIT + 1])
    counts = dict.fromkeys(BUCKETS, 0)
    oldest_due = None
    for row in rows[:SAMPLE_LIMIT]:
        bucket, due = classify(row)
        counts[bucket] += 1
        if bucket == "runnable" and due is not None:
            oldest_due = min(oldest_due, due) if oldest_due else due
    due_age = _age(now, oldest_due)
    stalled = bool(due_age is not None and due_age > threshold)
    attention = bool(exact_attention or counts["failed"] or counts["unknown"] or counts["attention"])
    truncated = len(rows) > SAMPLE_LIMIT
    coverage_complete = not truncated or complete_risk_scan
    return {
        "available": True, "healthy": coverage_complete and not (attention or stalled),
        "state": "stalled" if stalled else "attention" if attention else "coverage_incomplete" if not coverage_complete else "observed",
        "counts": counts, "sampled": True, "sample_size": min(len(rows), SAMPLE_LIMIT),
        "sample_limit": SAMPLE_LIMIT, "has_more": truncated,
        "risk_coverage_complete": coverage_complete,
        "attention_total": exact_attention,
        "oldest_runnable_age_seconds": due_age, "stall_after_seconds": threshold,
        "progress_age_seconds": _age(now, progress),
        "progress_evidence": progress_kind if progress else "unavailable",
    }


def _permission_blocked(client, *, settings_row, allowed, revision=None):
    return bool(
        not settings_row.is_enabled or (allowed and client.igsid not in allowed)
        or client.hidden_at or client.is_blocked or client.bot_paused
        or client.manager_takeover or client.privacy_erasure_started_at
        or client.stage == IgClient.Stage.SPAM
        or (client.opted_out_at and (not client.opted_in_at or client.opted_out_at >= client.opted_in_at))
        or (revision and client.reply_permission_epoch != revision.permission_epoch)
    )


def _manual_owned(revision):
    # Match the existing durable case identity and owner; a bare 'manual' state
    # without an accountable owner must remain attention.
    return IgFollowUpTask.objects.filter(
        client_id=revision.client_id, event_key=f"ig-revision-debt:{revision.pk}",
        kind="manager_task", reason="revision_case:execution_debt",
        manager_context__revision_id=revision.pk, manager_context__owner="manager",
    ).exclude(status__in=("completed", "cancelled")).exists()


def _revision_lane(*, now, settings_row, allowed):
    from management.services.ig_revision_execution import due_revision_ids, finalization_due_ids
    from management.services.ig_revision_rollout import revision_execution_rollout
    from management.services.ig_turn_revisions import replay_snapshot

    rollout = revision_execution_rollout(now=now)
    due = set(due_revision_ids(now=now, limit=SAMPLE_LIMIT, cutover_at=rollout.cutover_at)) if rollout.enabled else set()
    finalizing = set(finalization_due_ids(now=now, limit=SAMPLE_LIMIT))
    effects = IgRevisionDeliveryEffect.objects.filter(revision_id=OuterRef("pk"))
    manager_owned_source = IgTurnRevisionSource.objects.filter(
        revision_id=OuterRef("pk"),
        ordinal=1,
        message__status=InstagramBotMessage.Status.DONE,
    )
    owner = IgFollowUpTask.objects.filter(
        client_id=OuterRef("client_id"),
        event_key=Concat(Value("ig-revision-debt:"), Cast(OuterRef("pk"), CharField())),
        kind="manager_task", reason="revision_case:execution_debt",
        manager_context__owner="manager", manager_context__revision_id=OuterRef("pk"),
    ).exclude(status__in=("completed", "cancelled"))
    query = IgCustomerTurnRevision.objects.filter(active_slot=1).exclude(
        state__in=("processed", "superseded"),
    ).annotate(
        uncertain=Exists(effects.filter(state="unknown")),
        failed_effect=Exists(effects.filter(state="definite_failed")),
        manager_owned_source=Exists(manager_owned_source),
        manual_owner=Exists(owner),
    ).select_related("client", "turn").order_by("overall_deadline", "id")

    def classify(row):
        if row.uncertain:
            return "unknown", None
        if row.failed_effect:
            return "failed", None
        if row.recovery_state == "manual":
            from management.services.ig_revision_recovery import execution_resume_is_current
            if execution_resume_is_current(row, now=now):
                return "runnable", row.recovery_due_at or row.updated_at
            return ("manual", None) if _manual_owned(row) else ("attention", None)
        # A legacy inbound head can remain collecting after its source was
        # consumed just as the manager explicitly took over the client. That
        # is owned work for the manager, not actionable bot attention. Manual
        # resume heads stay visible until their own execution state resolves.
        if (
            row.state == "collecting"
            and row.origin == IgCustomerTurnRevision.Origin.INBOUND
            and row.overall_deadline <= now
            and row.client.bot_paused
            and row.client.manager_takeover
            and row.manager_owned_source
        ):
            return "manager_owned", None
        if row.overall_deadline <= now and row.recovery_state not in {"waiting", "execution"}:
            if row.pk in finalizing:
                return "runnable", row.updated_at
            return ("manual", None) if _manual_owned(row) else ("attention", None)
        if _permission_blocked(row.client, settings_row=settings_row, allowed=allowed, revision=row):
            return "deferred", None
        if row.recovery_state in {"waiting", "execution"}:
            if row.recovery_due_at and row.recovery_due_at > now:
                return "deferred", None
            from management.services.ig_revision_recovery import (
                execution_resume_is_current, recovery_lineage_for_authority, _eligibility,
            )
            if row.recovery_state == "execution":
                return ("runnable", row.recovery_due_at or row.updated_at) if execution_resume_is_current(row, now=now) else ("attention", None)
            lineage, reason = recovery_lineage_for_authority(row)
            if reason or any(_eligibility(row, row.client, lineage, now)):
                return "attention", None
            from management.services.ig_revision_provider_execution import inspect_revision_provider_execution
            continuation = inspect_revision_provider_execution(row, now=now)
            if continuation.ready:
                return "runnable", row.recovery_due_at or row.updated_at
            return ("deferred", None) if continuation.next_due_at and continuation.next_due_at > now else ("attention", None)
        if row.overall_deadline <= now:
            if row.pk in finalizing:
                return "runnable", row.updated_at
            return ("manual", None) if _manual_owned(row) else ("attention", None)
        if row.source_count <= 0 or row.erasure_started_at_snapshot:
            return "attention", None
        if row.state in {"sealed", "claimed"} and replay_snapshot(row.pk) is None:
            return "attention", None
        if row.turn.terminal_reason or row.turn.claim_state in {"processed", "superseded"} or row.origin == "manual_resume":
            from management.services.ig_revision_manual_resume import manual_resume_authority_current
            if not manual_resume_authority_current(row):
                return "attention", None
        if row.state in {"preparing", "claimed"}:
            if row.claim_token and row.lease_until and row.lease_until > now:
                return "processing", None
            if row.state == "claimed":
                if row.pk in finalizing:
                    return "runnable", row.updated_at
                return "attention", None
        if row.pk in due:
            return "runnable", row.quiet_deadline
        if row.state == "collecting" and row.quiet_deadline > now:
            return "deferred", None
        # The bounded canonical selector may not have inspected this head.
        # Never convert absence from its result into proof of runnable work.
        return "deferred" if not rollout.enabled else "attention", None

    manager_owned = Q(
        state="collecting", origin=IgCustomerTurnRevision.Origin.INBOUND,
        overall_deadline__lte=now, recovery_state="",
        client__bot_paused=True, client__manager_takeover=True,
        manager_owned_source=True,
    )
    bad = query.filter(Q(uncertain=True) | Q(failed_effect=True)
                       | (Q(overall_deadline__lte=now, manual_owner=False, recovery_state="")
                          & ~Q(pk__in=finalizing) & ~manager_owned)).count()
    progress = IgCustomerTurnRevision.objects.aggregate(at=Max("processed_at"))["at"]
    return _sample(query, classify, now=now, threshold=REPLY_STALL_SECONDS, progress=progress, exact_attention=bad, progress_kind="terminal_progress")


def _legacy_lane(*, now, settings_row, allowed):
    from management.services.ig_revision_live import legacy_claimable_messages
    query = legacy_claimable_messages(InstagramBotMessage.objects.filter(
        role="user", status__in=("pending", "processing"),
    )).select_related("client").order_by("created_at", "id")
    from management.services.ig_revision_rollout import revision_execution_rollout
    rollout = revision_execution_rollout(now=now)
    if rollout.enabled:
        # A post-cutover collecting head has not yet acquired sticky ownership,
        # but already belongs to the canonical lane, not two logical queues.
        source_ids = IgTurnRevisionSource.objects.filter(
            revision__active_slot=1, revision__created_at__gte=rollout.cutover_at,
        ).values("message_id")
        query = query.exclude(pk__in=Subquery(source_ids))

    def classify(row):
        if row.client is None or row.sender_id != row.client.igsid:
            return "attention", None
        if _age(now, row.created_at) > REPLY_STALL_SECONDS:
            return "attention", None
        if _permission_blocked(row.client, settings_row=settings_row, allowed=allowed):
            return "deferred", None
        if row.status == "processing":
            started = row.processing_started_at or row.created_at
            return ("processing", None) if _age(now, started) <= REPLY_STALL_SECONDS else ("attention", None)
        if _age(now, row.created_at) > REPLY_STALL_SECONDS:
            return "attention", None
        from management.services.ig_customer_turns import has_open_undue_turn
        if has_open_undue_turn(row, now=now):
            return "deferred", None
        return "runnable", row.created_at

    return _sample(query, classify, now=now, threshold=REPLY_STALL_SECONDS)


def _outbound_lane(*, now):
    query = InstagramBotMessage.objects.filter(role="model").filter(
        Q(status__in=("pending", "processing")) | Q(send_state="unknown"),
    ).order_by("created_at", "id")

    def classify(row):
        if row.send_state == "unknown":
            return "unknown", None
        # These rows are legacy delivery evidence, not an independent worker
        # authorization. Do not count unsent transcript rows as runnable sends.
        if row.status == "processing" and _age(now, row.send_started_at or row.processing_started_at or row.created_at) <= REPLY_STALL_SECONDS:
            return "processing", None
        return "attention", None

    return _sample(query, classify, now=now, threshold=REPLY_STALL_SECONDS,
                   exact_attention=query.filter(send_state="unknown").count())


def _delivery_lane(*, now):
    # UNKNOWN survives supersession and cannot be hidden by active_slot=1 or
    # a newer successful reply. A definite failure on an old replaced plan is
    # historical; keep that failure only while its original head remains active.
    query = IgRevisionDeliveryEffect.objects.filter(
        Q(state="unknown") | Q(state="definite_failed", revision__active_slot=1),
    ).order_by("created_at", "id")
    return _sample(query, lambda row: ("unknown" if row.state == "unknown" else "failed", None),
                   now=now, threshold=REPLY_STALL_SECONDS, exact_attention=query.count(), complete_risk_scan=True)


def _binotel_lane(*, now):
    from management.services.call_auto_analysis import is_call_auto_analysis_enabled
    from management.services.call_ai_queue import analysis_queue_category, STALE_ANALYSIS_LOCK_MINUTES
    if not is_call_auto_analysis_enabled():
        return {"available": True, "healthy": True, "state": "disabled", "sampled": True,
                "counts": dict.fromkeys(BUCKETS, 0), "has_more": False,
                "progress_age_seconds": None, "progress_evidence": "unavailable"}
    query = CallRecord.objects.filter(provider="binotel", ai_status__in=("pending", "running", "error")).order_by("created_at", "id")

    def classify(row):
        if row.ai_status == "error":
            return "failed", None
        if row.ai_status == "running":
            lease = row.ai_locked_at + timedelta(minutes=STALE_ANALYSIS_LOCK_MINUTES) if row.ai_locked_at else None
            return ("processing", None) if lease and lease > now else ("attention", None)
        category = analysis_queue_category(row.payload, row.duration_seconds)
        if category != "eligible":
            return ("attention", None) if _age(now, row.created_at) > SERVICE_STALL_SECONDS else ("deferred", None)
        return "runnable", row.created_at

    return _sample(query, classify, now=now, threshold=SERVICE_STALL_SECONDS,
                   exact_attention=query.filter(ai_status="error").count())


def _consumer_heartbeat_lane(task_key, *, now, threshold):
    """Observe an isolated consumer without deriving liveness from daemon pulse."""
    row = InstagramBotTaskHeartbeat.objects.filter(task_key=task_key).first()
    if row is None:
        return {
            "available": True, "healthy": True, "state": "unobserved",
            "counts": dict.fromkeys(BUCKETS, 0), "sampled": True,
            "has_more": False, "risk_coverage_complete": True,
            "progress_age_seconds": None, "progress_evidence": "unobserved",
        }
    reference = row.last_succeeded_at or row.last_started_at or row.first_expected_at
    age = _age(now, reference)
    failed = bool(row.last_failed_at and (not row.last_succeeded_at or row.last_failed_at >= row.last_succeeded_at))
    stale = age is not None and age > threshold
    state = "failed" if failed else "stalled" if stale else "observed"
    return {
        "available": True, "healthy": not (failed or stale), "state": state,
        "counts": dict.fromkeys(BUCKETS, 0), "sampled": True,
        "has_more": False, "risk_coverage_complete": True,
        "progress_age_seconds": age, "stall_after_seconds": threshold,
        "progress_evidence": "task_heartbeat",
        "last_error_kind": row.last_error_kind,
        "consecutive_failures": row.consecutive_failures,
    }


def _typed_memory_lane(*, now):
    if not shadow_enabled():
        return {"available": True, "healthy": True, "state": "disabled",
                "counts": dict.fromkeys(BUCKETS, 0), "sampled": True,
                "has_more": False, "risk_coverage_complete": True,
                "progress_age_seconds": None, "progress_evidence": "disabled"}
    return _consumer_heartbeat_lane("ig_typed_memory_reconcile", now=now, threshold=1800)


def _trace_refresh_lane(*, now):
    from management.models import IgJourneyTraceRefreshControl
    control = IgJourneyTraceRefreshControl.objects.filter(pk=1).values("enabled").first()
    if control is None or not control["enabled"]:
        return {"available": True, "healthy": True, "state": "disabled",
                "counts": dict.fromkeys(BUCKETS, 0), "sampled": True,
                "has_more": False, "risk_coverage_complete": True,
                "progress_age_seconds": None, "progress_evidence": "disabled"}
    return _consumer_heartbeat_lane("ig_trace_refresh", now=now, threshold=90)


def _job_lane(model, *, now, statuses, progress_field, due_field="next_attempt_at", threshold=SERVICE_STALL_SECONDS, analysis=False, recovery=False):
    query = model.objects.filter(status__in=statuses).order_by("created_at", "id")
    from management.services.bot_conversation_analysis import MAX_ATTEMPTS
    model_fields = {field.name for field in model._meta.get_fields()}
    has_lease = "lease_until" in model_fields

    def classify(row):
        if row.status in {"unknown", "ambiguous"}:
            return "unknown", None
        if row.status in {"failed", "dead_letter"}:
            if analysis and row.updated_at and row.updated_at <= now - timedelta(seconds=HISTORICAL_FAILURE_SECONDS):
                return "historical", None
            return "failed", None
        if row.status in {"processing", "sending"}:
            lease = getattr(row, "lease_until", None)
            if model is IgBotNotification:
                from management.services.instagram_bot import NOTIFICATION_STALE_SENDING_SECONDS
                lease = row.last_attempt_at + timedelta(seconds=NOTIFICATION_STALE_SENDING_SECONDS) if row.last_attempt_at else None
            return ("processing", None) if lease and lease > now else ("attention", None)
        due = getattr(row, due_field, None) or row.created_at
        if analysis:
            from management.services.bot_conversation_analysis import MAX_ATTEMPTS
            if row.attempts >= MAX_ATTEMPTS:
                return "attention", None
            due = max(due, row.due_at)
        if recovery and row.activated_at is None and not (row.holding_message_id and row.holding_message.provider_message_id):
            return "deferred", None
        if due > now:
            return "deferred", None
        # A retry moves the next opportunity, not the original waiting age.
        return "runnable", row.due_at if analysis else row.created_at

    bad_q = Q(status__in=("failed", "dead_letter", "unknown", "ambiguous"))
    if analysis:
        bad_q = (
            Q(status__in=("unknown", "ambiguous"))
            | Q(status__in=("failed", "dead_letter"), updated_at__gt=now - timedelta(seconds=HISTORICAL_FAILURE_SECONDS))
        )
    if model is not IgBotNotification and has_lease:
        bad_q |= Q(status__in=("processing", "sending")) & (Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    elif model is IgBotNotification:
        from management.services.instagram_bot import NOTIFICATION_STALE_SENDING_SECONDS
        bad_q |= Q(status="sending") & (Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lte=now - timedelta(seconds=NOTIFICATION_STALE_SENDING_SECONDS)))
    if analysis:
        bad_q |= Q(status="pending", attempts__gte=MAX_ATTEMPTS)
        # Count stalled due work across the whole queue: a preceding future or
        # manual row must not conceal it beyond the classification sample.
        bad_q |= Q(status="pending", due_at__lt=now - timedelta(seconds=threshold), next_attempt_at__lte=now)
    elif not recovery:
        bad_q |= Q(status="pending", created_at__lt=now - timedelta(seconds=threshold)) & (Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
    bad = query.filter(bad_q).count()
    progress = model.objects.aggregate(at=Max(progress_field))["at"]
    return _sample(query, classify, now=now, threshold=threshold, progress=progress, exact_attention=bad,
                   complete_risk_scan=not recovery,
                   progress_kind="terminal_progress" if progress_field == "completed_at" else "durable_completion")


def operational_lane_snapshot(*, now=None):
    """Sanitized GET observations; errors are unavailable, never empty/healthy."""
    now = now or timezone.now()
    try:
        settings_row = InstagramBotSettings.objects.filter(pk=1).first()
        if settings_row is None:
            return {"available": False, "healthy": False, "reason": "settings_unavailable", "lanes": {}}
        from management.services.instagram_bot import allowed_sender_ids, ingress_status
        from management.services.ig_daemon_health import daemon_runtime_health_snapshot
        from management.services.ig_maintenance import maintenance_status
        from management.services.ig_permission_transitions import permission_transition_snapshot
        allowed = allowed_sender_ids(settings_row)
        daemon = daemon_runtime_health_snapshot(now_epoch=now.timestamp())
        maintenance = maintenance_status(now=now.timestamp())["active"]
        pause_pending = permission_transition_snapshot()["global_pause_pending"]
        enabled = bool(settings_row.is_enabled)
        bot_state = "disabled" if not enabled else "maintenance" if maintenance else "pause_pending" if pause_pending else "running" if daemon["process_online"] and daemon["main_healthy"] and ingress_status(settings_row, now=now)["healthy"] else "unavailable"
        lanes = {
            "customer_revisions": _revision_lane(now=now, settings_row=settings_row, allowed=allowed),
            "legacy_inbound": _legacy_lane(now=now, settings_row=settings_row, allowed=allowed),
            "legacy_outbound": _outbound_lane(now=now),
            "revision_delivery": _delivery_lane(now=now),
            "manager_notifications": _job_lane(IgBotNotification, now=now, statuses=("pending", "sending", "unknown", "failed", "dead_letter"), progress_field="sent_at"),
            "conversation_analysis": _job_lane(IgConversationAnalysisJob, now=now, statuses=("pending", "processing", "failed"), progress_field="analyzed_at", analysis=True),
            "analysis_materialization": _job_lane(IgConversationAnalysisEvent, now=now, statuses=("pending", "failed"), progress_field="applied_at"),
            "reply_recovery": _job_lane(IgAiReplyRecoveryJob, now=now, statuses=("pending", "processing", "sending", "ambiguous", "failed"), progress_field="completed_at", recovery=True),
            "binotel_analysis": _binotel_lane(now=now),
            "typed_memory": _typed_memory_lane(now=now),
            "trace_refresh": _trace_refresh_lane(now=now),
        }
        workers_healthy = daemon.get("workers_healthy", True)
        if bot_state == "running" and not workers_healthy:
            bot_state = "worker_stalled"
        return {
            "available": True, "healthy": bot_state in {"running", "disabled"} and all(lane["healthy"] for lane in lanes.values()),
            "bot_state": bot_state, "lanes": lanes,
            "consumer": {key: daemon[key] for key in ("process_online", "main_healthy", "process_age_seconds", "main_age_seconds", "stalled_reason")},
            "worker_lanes": daemon.get("worker_lanes", {}),
            "release_generation": daemon.get("release_generation"),
            "supervisor": daemon.get("supervisor", {}),
            "unobserved_lanes": sorted(
                name for name, row in daemon.get("worker_lanes", {}).items()
                if row.get("state") == "unobserved"
            ),
        }
    except (DatabaseError, OSError, ValueError, TypeError):
        return {"available": False, "healthy": False, "reason": "observation_unavailable", "lanes": {}}
